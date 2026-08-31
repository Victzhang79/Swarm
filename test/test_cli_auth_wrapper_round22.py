#!/usr/bin/env python3
"""#6 round22：CLI 统一注入 auth token（复现 28 处漏带 → RBAC 开时 401）。

premise：CLI 全走 HTTP、无法绕过服务端 RBAC；真问题=多数命令的 httpx 调用漏带 _auth_headers()
→ RBAC 开启时 submit/task approve/status 等命令 401（功能坏，非安全越权）。

治本：sync httpx 调用统一走 _hget/_hpost/_hput/_hdelete 包装，始终注入 _auth_headers()。
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm import cli as _cli_pkg  # noqa: E402
cli = _cli_pkg


def test_wrappers_inject_auth_header(monkeypatch):
    monkeypatch.setenv("SWARM_TOKEN", "tok-round22")
    captured = {}

    def fake_get(url, **kw):
        captured["headers"] = kw.get("headers")
        resp = MagicMock(); resp.status_code = 200
        return resp

    with patch.object(cli.httpx, "get", side_effect=fake_get):
        cli._hget("http://x/api/status", timeout=5.0)
    assert captured["headers"].get("Authorization") == "Bearer tok-round22", \
        "sync 包装必须注入 Authorization（复现 bug：过去漏带 → 401）"
    print("  ✅ _hget 注入 Authorization")


def test_wrapper_idempotent_with_existing_headers(monkeypatch):
    monkeypatch.setenv("SWARM_TOKEN", "tok-round22")
    captured = {}

    def fake_post(url, **kw):
        captured["headers"] = kw.get("headers")
        resp = MagicMock(); resp.status_code = 200
        return resp

    with patch.object(cli.httpx, "post", side_effect=fake_post):
        # 已显式传 headers 也不冲突（幂等合并）
        cli._hpost("http://x/api/sandbox/create", headers=cli._auth_headers({"X-Foo": "1"}), json={})
    assert captured["headers"].get("Authorization") == "Bearer tok-round22"
    assert captured["headers"].get("X-Foo") == "1", "既有自定义头不丢"
    print("  ✅ 包装与既有 headers 幂等合并")


def test_wrappers_exist():
    for name in ("_hget", "_hpost", "_hput", "_hdelete"):
        assert hasattr(cli, name), f"缺 CLI auth 包装 {name}"
    print("  ✅ 四个 sync auth 包装齐备")


def test_status_accepts_api_component_list(monkeypatch):
    from click.testing import CliRunner

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "overall": "healthy",
        "components": [
            {"name": "PostgreSQL", "status": "healthy", "detail": "ok"},
            {"name": "Redis", "status": "degraded", "detail": "slow"},
        ],
    }
    monkeypatch.setattr(cli, "_hget", lambda *args, **kwargs: resp)

    result = CliRunner().invoke(cli.main, ["status"])

    assert result.exit_code == 0, result.output
    assert "PostgreSQL" in result.output and "Redis" in result.output


def test_kb_symbols_accepts_production_field_names(monkeypatch):
    from click.testing import CliRunner

    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"symbols": [{
        "symbol_name": "OrderService",
        "symbol_type": "class",
        "file_path": "src/order.py",
        "start_line": 10,
    }]}
    monkeypatch.setattr(cli, "_hget", lambda *args, **kwargs: resp)

    result = CliRunner().invoke(cli.main, ["kb", "symbols", "-p", "p1"])

    assert result.exit_code == 0, result.output
    assert "OrderService" in result.output and "src/order.py" in result.output


def test_kb_norms_renders_tag_from_api_contract(monkeypatch):
    from click.testing import CliRunner

    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"norms": [{
        "id": "n1", "tag": "python", "title": "Style", "content": "Use typing",
    }]}
    monkeypatch.setattr(cli, "_hget", lambda *args, **kwargs: resp)

    result = CliRunner().invoke(cli.main, ["kb", "norms", "-p", "p1"])

    assert result.exit_code == 0, result.output
    assert "python" in result.output and "Use typing" in result.output


if __name__ == "__main__":
    os.environ["SWARM_TOKEN"] = "tok-round22"
    test_wrappers_exist()
    print("\n✅ #6 CLI auth 包装 smoke 通过（完整断言见 pytest monkeypatch 用例）")
