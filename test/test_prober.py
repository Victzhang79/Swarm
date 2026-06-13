#!/usr/bin/env python3
"""模型探测器单测（设计 v3 A批2）。

全部用 httpx.MockTransport 拦截，不发真网络请求；纯逻辑可任何环境跑（含 CI）。
覆盖：四层 context 探测、错误消息解析、多模态判定、速度计算、provider 编排。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.config.settings import ProviderConfig
from swarm.models import capability_store as cap
from swarm.models import prober


def _provider(kind="cloud") -> ProviderConfig:
    return ProviderConfig(id="p1", label="P1", kind=kind,
                          base_url="https://api.example.com/v1", api_key="k")


def _patch_transport(monkeypatch, handler):
    """让 prober 内部所有 httpx.Client 都走 MockTransport。"""
    real_client = httpx.Client

    def _factory(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(prober.httpx, "Client", _factory)


# ── 错误消息解析（纯函数）──────────────────────────────────

def test_parse_context_from_text():
    assert prober._parse_context_from_text("This model's maximum context length is 32768 tokens") == 32768
    assert prober._parse_context_from_text("max_model_len is 8192, reduce input") == 8192
    # 噪声过滤：< 1024 的数字不取
    assert prober._parse_context_from_text("max_tokens=1 invalid") is None
    print("  ✅ 解析: 错误消息抠 context (32768/8192/噪声过滤)")


def test_parse_context_picks_max():
    txt = "context length of 4096 ... maximum context length is 131072 tokens"
    assert prober._parse_context_from_text(txt) == 131072
    print("  ✅ 解析: 多匹配取最大值")


# ── context 第 1-2 层：models 字段 ──────────────────────────

def test_context_from_model_object_toplevel():
    assert prober.context_from_model_object({"id": "m", "max_model_len": 65536}) == 65536
    assert prober.context_from_model_object({"id": "m", "context_window": 200000}) == 200000
    print("  ✅ context: 顶层字段 (max_model_len/context_window)")


def test_context_from_model_object_nested():
    obj = {"id": "m", "meta": {"n_ctx": 16384}}
    assert prober.context_from_model_object(obj) == 16384
    print("  ✅ context: 嵌套字段 (meta.n_ctx)")


def test_context_from_model_object_none():
    assert prober.context_from_model_object({"id": "m"}) is None
    print("  ✅ context: 无字段返回 None")


# ── context 第 3 层：错误消息（mock 网络）──────────────────

def test_context_from_error_400(monkeypatch):
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "maximum context length is 40960 tokens"}})
    _patch_transport(monkeypatch, handler)
    assert prober.context_from_error(_provider(), "m") == 40960
    print("  ✅ context: 第3层 400错误解析 → 40960")


def test_context_from_error_200_no_usage_returns_none(monkeypatch):
    # 超长请求成功但无 usage → 无法推断
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    _patch_transport(monkeypatch, handler)
    assert prober.context_from_error(_provider(), "m") is None
    print("  ✅ context: 第3层 200成功+无usage → None (无法推断)")


def test_context_from_error_200_with_usage_infers_lower_bound(monkeypatch):
    # 网关接受超长请求(Open WebUI/自建网关常见)：从 usage.prompt_tokens 推下界
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": None}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 205016, "completion_tokens": 1},
        })
    _patch_transport(monkeypatch, handler)
    assert prober.context_from_error(_provider(), "m") == 205016
    print("  ✅ context: 第3层 200+usage(网关不拒超长) → 下界 205016")


# ── probe_context_window 分层命中顺序 ──────────────────────

def test_probe_context_layer1_wins(monkeypatch):
    # models 字段命中 → parsed，不发请求
    called = {"n": 0}
    def handler(request):
        called["n"] += 1
        return httpx.Response(400, text="should not be called")
    _patch_transport(monkeypatch, handler)
    win, src = prober.probe_context_window(_provider(), "m", {"id": "m", "max_model_len": 8192})
    assert win == 8192 and src == cap.SOURCE_PARSED
    assert called["n"] == 0, "第1层命中不应发请求"
    print("  ✅ context: 第1层命中即停 (parsed, 不发请求)")


def test_probe_context_layer4_default(monkeypatch):
    # models 无字段 + 错误请求也失败 → 启发式默认
    def handler(request):
        raise httpx.ConnectError("unreachable")
    _patch_transport(monkeypatch, handler)
    win, src = prober.probe_context_window(_provider(kind="local"), "qwen3:7b", {"id": "qwen3:7b"})
    assert src == cap.SOURCE_DEFAULT
    assert win == 32000  # 本地小模型启发式
    print("  ✅ context: 全失败 → 第4层启发式默认 (default)")


# ── 多模态探测 ─────────────────────────────────────────────

def test_probe_multimodal_true(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "white"}}]})
    _patch_transport(monkeypatch, handler)
    assert prober.probe_multimodal(_provider(), "m") is True
    print("  ✅ 多模态: 200响应 → True")


def test_probe_multimodal_false(monkeypatch):
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "This model does not support image input"}})
    _patch_transport(monkeypatch, handler)
    assert prober.probe_multimodal(_provider(), "m") is False
    print("  ✅ 多模态: 400+不支持图像信号 → False")


def test_probe_multimodal_uncertain(monkeypatch):
    def handler(request):
        return httpx.Response(503, text="service unavailable")
    _patch_transport(monkeypatch, handler)
    assert prober.probe_multimodal(_provider(), "m") is None
    print("  ✅ 多模态: 5xx → None (不确定)")


# ── 速度探测 ───────────────────────────────────────────────

def test_probe_speed(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "1,2,3"}}],
            "usage": {"completion_tokens": 100},
        })
    _patch_transport(monkeypatch, handler)
    tps = prober.probe_speed(_provider(), "m")
    assert tps > 0
    print(f"  ✅ 速度: usage.completion_tokens → {tps} tps")


def test_probe_speed_failure(monkeypatch):
    def handler(request):
        return httpx.Response(500, text="err")
    _patch_transport(monkeypatch, handler)
    assert prober.probe_speed(_provider(), "m") == 0.0
    print("  ✅ 速度: 失败 → 0.0")


# ── list_models 容错 ───────────────────────────────────────

def test_list_models_openai(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "model-a"}, {"id": "model-b"}]})
    _patch_transport(monkeypatch, handler)
    objs, err = prober.list_models(_provider())
    assert err is None
    assert {prober._model_id_of(o) for o in objs} == {"model-a", "model-b"}
    print("  ✅ list: OpenAI 格式")


def test_list_models_401(monkeypatch):
    def handler(request):
        return httpx.Response(401, text="unauthorized")
    _patch_transport(monkeypatch, handler)
    objs, err = prober.list_models(_provider())
    assert objs == [] and "认证失败" in err
    print("  ✅ list: 401 → 认证失败提示")


# ── provider 编排（persist=False，纯内存）──────────────────

def test_probe_provider_orchestration(monkeypatch):
    def handler(request):
        url = str(request.url)
        if url.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m1", "max_model_len": 8192}]})
        # chat completions：多模态探测返回成功，速度探测返回 usage
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"completion_tokens": 50},
        })
    _patch_transport(monkeypatch, handler)
    progress = []
    result = prober.probe_provider(
        _provider(), persist=False,
        progress_cb=lambda d, t, m: progress.append((d, t, m)),
    )
    assert result["total"] == 1
    assert result["probed"] == 1
    cap_rec = result["capabilities"][0]
    assert cap_rec["context_window"] == 8192
    assert cap_rec["supports_multimodal"] is True
    assert cap_rec["gen_speed_tps"] > 0
    assert len(progress) >= 1
    print("  ✅ 编排: probe_provider 全模型探测 + 进度回调")


def test_probe_provider_only_models(monkeypatch):
    """only_models 给定时只探指定模型，不探 /models 返回的其它模型。"""
    probed_models = []

    def handler(request):
        url = str(request.url)
        if url.endswith("/models"):
            # provider 列出 5 个模型
            return httpx.Response(200, json={"data": [
                {"id": f"model-{i}"} for i in range(5)
            ]})
        # 记录被探测的模型（从请求体取 model）
        import json as _json
        body = _json.loads(request.content)
        probed_models.append(body.get("model"))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"completion_tokens": 10},
        })
    _patch_transport(monkeypatch, handler)
    # 只探 model-1 和 model-3
    result = prober.probe_provider(
        _provider(), only_models=["model-1", "model-3"], persist=False,
    )
    assert result["total"] == 2, "只应探 2 个指定模型"
    assert result["probed"] == 2
    # 被探测的模型只能是指定的两个（每个模型探多模态+速度=2次请求）
    assert set(probed_models) <= {"model-1", "model-3"}, probed_models
    assert "model-0" not in probed_models and "model-4" not in probed_models
    print("  ✅ 编排: only_models 只探指定模型 (5列出→只探2个)")


def test_probe_provider_only_models_survives_list_failure(monkeypatch):
    """列模型失败但指定了 only_models → 仍能探（用户配的模型名是对的）。"""
    def handler(request):
        url = str(request.url)
        if url.endswith("/models") or url.endswith("/api/tags"):
            return httpx.Response(500, text="models endpoint down")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"completion_tokens": 10},
        })
    _patch_transport(monkeypatch, handler)
    result = prober.probe_provider(
        _provider(), only_models=["my-model"], persist=False,
    )
    assert result["total"] == 1
    assert result["probed"] == 1
    print("  ✅ 编排: 列模型失败但 only_models 指定 → 仍探测")


def test_probe_provider_auth_failure_aborts(monkeypatch):
    """认证失败(401)是致命错误：中止探测、返回 error，不落假 default 数据。"""
    def handler(request):
        # /models 返回 401，chat 也会 401
        return httpx.Response(401, text="Api key is invalid")
    _patch_transport(monkeypatch, handler)
    # 即使指定了 only_models，认证失败也应中止（不能继续探出假数据）
    result = prober.probe_provider(
        _provider(), only_models=["m1", "m2"], persist=False,
    )
    assert result["total"] == 0
    assert result["probed"] == 0
    assert result.get("error") and "认证失败" in result["error"]
    assert result["capabilities"] == []
    print("  ✅ 编排: 401认证失败 → 中止探测+返回error (不落假数据)")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))
