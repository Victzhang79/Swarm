#!/usr/bin/env python3
"""模型能力 API 端点测试（设计 v3 A批2）。

TestClient + mock：不发真网络/不接真 DB。RBAC 由 conftest 关闭（匿名 admin）。
覆盖：probe 触发→status 轮询→capabilities 读/改/删 全链路契约。
"""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _client():
    from fastapi.testclient import TestClient
    from swarm.api.app import app
    return TestClient(app)


_MOCK_PROBE_RESULT = {
    "provider_id": "siliconflow",
    "total": 2,
    "probed": 2,
    "errors": [],
    "capabilities": [
        {"provider_id": "siliconflow", "model_id": "m1", "context_window": 128000,
         "supports_multimodal": False, "gen_speed_tps": 40.0, "kind": "cloud",
         "source": "probed", "note": "", "probed_at": None},
    ],
}


def test_probe_unknown_provider_404():
    client = _client()
    resp = client.post("/api/models/probe", json={"provider_id": "no_such_provider"})
    assert resp.status_code == 404, resp.text
    print("  ✅ POST /probe: 未知 provider → 404")


def test_probe_missing_provider_id_400():
    client = _client()
    resp = client.post("/api/models/probe", json={})
    assert resp.status_code == 400, resp.text
    print("  ✅ POST /probe: 缺 provider_id → 400")


def test_probe_full_flow():
    """触发探测 → 轮询 status 到 done → 校验 result。"""
    client = _client()
    # siliconflow 是合成默认 provider 之一（_effective_providers 会合成它）
    with patch("swarm.models.prober.probe_provider", return_value=_MOCK_PROBE_RESULT) as mock_probe:
        resp = client.post("/api/models/probe", json={"provider_id": "siliconflow", "measure_speed": False})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] in ("started", "already_running"), body

        # 轮询 status 直到 done（后台 create_task 完成）
        deadline = time.time() + 5
        status = None
        while time.time() < deadline:
            s = client.get("/api/models/probe/status", params={"provider_id": "siliconflow"})
            assert s.status_code == 200, s.text
            status = s.json()
            if status.get("status") in ("done", "error"):
                break
            time.sleep(0.05)
        assert status is not None and status["status"] == "done", status
        assert status["result"]["probed"] == 2
        mock_probe.assert_called_once()
    print("  ✅ POST /probe → status 轮询 → done (result.probed=2)")


def test_probe_status_idle():
    client = _client()
    resp = client.get("/api/models/probe/status", params={"provider_id": "never_probed_xyz"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "idle"
    print("  ✅ GET /probe/status: 未探测 → idle")


def test_probe_scope_in_use_only_probes_in_use_models():
    """scope=in_use（默认）只把在用模型传给 probe_provider。"""
    client = _client()
    captured = {}

    def fake_probe(provider, only_models=None, **kw):
        captured["only_models"] = only_models
        return {"provider_id": provider.id, "total": len(only_models or []),
                "probed": len(only_models or []), "errors": [], "capabilities": []}

    with patch("swarm.config.settings.ModelConfig.models_in_use_for_provider",
               return_value=["model-x", "model-y"]):
        with patch("swarm.models.prober.probe_provider", side_effect=fake_probe):
            resp = client.post("/api/models/probe",
                               json={"provider_id": "siliconflow", "scope": "in_use"})
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] in ("started", "already_running")
            time.sleep(0.3)
    # 验证只把在用模型传下去（不是 None=全探）
    assert captured.get("only_models") == ["model-x", "model-y"], captured
    print("  ✅ POST /probe scope=in_use: 只探在用模型 [model-x, model-y]")


def test_probe_scope_all_probes_everything():
    """scope=all 时 only_models=None（全探）。"""
    client = _client()
    captured = {}

    def fake_probe(provider, only_models=None, **kw):
        captured["only_models"] = only_models
        return {"provider_id": provider.id, "total": 0, "probed": 0, "errors": [], "capabilities": []}

    with patch("swarm.models.prober.probe_provider", side_effect=fake_probe):
        resp = client.post("/api/models/probe",
                           json={"provider_id": "siliconflow", "scope": "all"})
        assert resp.status_code == 200, resp.text
        time.sleep(0.3)
    assert captured.get("only_models") is None, "scope=all 应传 None(全探)"
    print("  ✅ POST /probe scope=all: only_models=None (全探)")


def test_probe_no_models_in_use():
    """provider 下无在用模型 → 直接返回 no_models_in_use，不触发探测。"""
    client = _client()
    with patch("swarm.config.settings.ModelConfig.models_in_use_for_provider", return_value=[]):
        resp = client.post("/api/models/probe",
                           json={"provider_id": "siliconflow", "scope": "in_use"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "no_models_in_use"
    print("  ✅ POST /probe: 无在用模型 → no_models_in_use (不探测)")


def test_probe_auto_local_probes_all():
    """scope=auto + 本地接入点 → 全探（only_models=None）。"""
    client = _client()
    captured = {}

    def fake_probe(provider, only_models=None, **kw):
        captured["only_models"] = only_models
        captured["kind"] = provider.kind
        return {"provider_id": provider.id, "total": 0, "probed": 0, "errors": [], "capabilities": []}

    with patch("swarm.models.prober.probe_provider", side_effect=fake_probe):
        # local 是合成默认 provider（kind=local）
        resp = client.post("/api/models/probe",
                           json={"provider_id": "local", "scope": "auto"})
        assert resp.status_code == 200, resp.text
        time.sleep(0.3)
    assert captured.get("kind") == "local"
    assert captured.get("only_models") is None, "本地 auto 应全探(None)"
    print("  ✅ POST /probe auto+本地 → 全探(only_models=None)")


def test_probe_auto_cloud_probes_in_use():
    """scope=auto + 云端接入点 → 只探在用模型。"""
    client = _client()
    captured = {}

    def fake_probe(provider, only_models=None, **kw):
        captured["only_models"] = only_models
        captured["kind"] = provider.kind
        return {"provider_id": provider.id, "total": len(only_models or []),
                "probed": 0, "errors": [], "capabilities": []}

    with patch("swarm.config.settings.ModelConfig.models_in_use_for_provider",
               return_value=["cloud-m1", "cloud-m2"]):
        with patch("swarm.models.prober.probe_provider", side_effect=fake_probe):
            resp = client.post("/api/models/probe",
                               json={"provider_id": "siliconflow", "scope": "auto"})
            assert resp.status_code == 200, resp.text
            time.sleep(0.3)
    assert captured.get("kind") == "cloud"
    assert captured.get("only_models") == ["cloud-m1", "cloud-m2"], "云端 auto 应只探在用"
    print("  ✅ POST /probe auto+云端 → 只探在用模型")


def test_get_capabilities():
    client = _client()
    rows = [
        {"provider_id": "p1", "model_id": "m1", "context_window": 8192,
         "supports_multimodal": True, "gen_speed_tps": 0.0, "kind": "local",
         "source": "default", "note": "", "probed_at": None},
    ]
    with patch("swarm.models.capability_store.list_capabilities", return_value=rows) as mock_list:
        resp = client.get("/api/models/capabilities", params={"provider_id": "p1"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["count"] == 1
        assert data["capabilities"][0]["model_id"] == "m1"
        mock_list.assert_called_once_with("p1")
    print("  ✅ GET /capabilities: 按 provider 过滤")


def test_put_capability_manual():
    client = _client()
    saved = {"provider_id": "p1", "model_id": "m1", "context_window": 200000,
             "supports_multimodal": True, "gen_speed_tps": 0.0, "kind": "cloud",
             "source": "manual", "note": "人工修正", "probed_at": None}
    with patch("swarm.models.capability_store.upsert_capability", return_value=saved) as mock_up:
        resp = client.put("/api/models/capabilities", json={
            "provider_id": "p1", "model_id": "m1",
            "context_window": 200000, "supports_multimodal": True, "kind": "cloud",
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["capability"]["source"] == "manual"
        # 校验 source 被强制为 manual
        _, kwargs = mock_up.call_args
        assert kwargs["source"] == "manual"
    print("  ✅ PUT /capabilities: 人工修正 source=manual")


def test_put_capability_missing_fields_400():
    client = _client()
    resp = client.put("/api/models/capabilities", json={"provider_id": "p1"})
    assert resp.status_code == 400
    print("  ✅ PUT /capabilities: 缺 model_id → 400")


def test_delete_capability():
    client = _client()
    with patch("swarm.models.capability_store.delete_capability", return_value=True) as mock_del:
        resp = client.request("DELETE", "/api/models/capabilities",
                              params={"provider_id": "p1", "model_id": "m1"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["deleted"] is True
        mock_del.assert_called_once_with("p1", "m1")
    print("  ✅ DELETE /capabilities: 删除成功")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))
