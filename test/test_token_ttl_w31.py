#!/usr/bin/env python3
"""W3.1：token 生命周期 UX — 可配 TTL + 登录返回 expires_at。"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_config_default_ttl_zero():
    from swarm.config.settings import AppConfig

    assert AppConfig().token_ttl_hours == 0, "默认应永不过期(0)，保持向后兼容"
    print("  ✅ token_ttl_hours 默认 0（永不过期）")


def test_login_response_includes_expiry_fields():
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    with TestClient(app) as client:
        ok = client.post("/api/auth/login", json={"username": "admin", "password": "swarm"})
        assert ok.status_code == 200, ok.text
        data = ok.json()
        assert "expires_at" in data, "登录响应应含 expires_at 字段(W3.1)"
        assert "token_ttl_hours" in data, "登录响应应含 token_ttl_hours 字段"
        # 默认 TTL=0 → expires_at 为 None（永不过期）
        assert data["expires_at"] is None, data["expires_at"]
    print("  ✅ 登录响应含 expires_at / token_ttl_hours（默认永不过期=None）")


def test_login_sets_expiry_when_ttl_configured():
    from datetime import datetime

    from fastapi.testclient import TestClient

    from swarm.config import settings as settings_mod

    old = os.environ.get("SWARM_TOKEN_TTL_HOURS")
    os.environ["SWARM_TOKEN_TTL_HOURS"] = "2"
    try:
        settings_mod.reload_config()
        from swarm.api.app import app

        with TestClient(app) as client:
            ok = client.post("/api/auth/login", json={"username": "admin", "password": "swarm"})
            assert ok.status_code == 200, ok.text
            data = ok.json()
            assert data["token_ttl_hours"] == 2, data
            assert data["expires_at"], "TTL>0 应返回非空 expires_at"
            # 到期时间应在未来
            exp = datetime.fromisoformat(data["expires_at"])
            now = datetime.now(exp.tzinfo)
            assert exp > now, (exp, now)
    finally:
        if old is None:
            os.environ.pop("SWARM_TOKEN_TTL_HOURS", None)
        else:
            os.environ["SWARM_TOKEN_TTL_HOURS"] = old
        # 还原配置，避免污染后续登录（把 admin token 设回永不过期）
        settings_mod.reload_config()
        from fastapi.testclient import TestClient as _TC

        from swarm.api.app import app as _app
        with _TC(_app) as _c:
            _c.post("/api/auth/login", json={"username": "admin", "password": "swarm"})
    print("  ✅ 配置 TTL>0 时登录刷新 token_expires_at 并返回未来 expires_at")


def main() -> int:
    print("=== test_token_ttl_w31 ===")
    failed = 0
    for fn in (
        test_config_default_ttl_zero,
        test_login_response_includes_expiry_fields,
        test_login_sets_expiry_when_ttl_configured,
    ):
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
            import traceback

            traceback.print_exc()
    print(f"\n{'All passed' if not failed else str(failed) + ' failed'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
