#!/usr/bin/env python3
"""登录 UI + API 冒烟测试。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_login_modal_html():
    html = (Path(__file__).parent.parent / "api/static/index.html").read_text(encoding="utf-8")
    assert 'id="login-username"' in html, "login-username input missing"
    assert 'id="login-password"' in html, "login-password input missing"
    assert "submitLogin()" in html, "submitLogin handler missing"
    print("  ✅ login modal HTML has username + password fields")


def test_login_api_admin_swarm():
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    client = TestClient(app)
    bad = client.post("/api/auth/login", json={"username": "", "password": "swarm"})
    assert bad.status_code == 401 or bad.status_code == 422

    ok = client.post("/api/auth/login", json={"username": "admin", "password": "swarm"})
    assert ok.status_code == 200, ok.text
    data = ok.json()
    assert data["user"]["username"] == "admin"
    assert data.get("token")
    print("  ✅ POST /api/auth/login admin/swarm")


def main() -> int:
    print("=== test_auth_login ===")
    failed = 0
    for fn in (test_login_modal_html, test_login_api_admin_swarm):
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
            import traceback

            traceback.print_exc()
    if failed:
        print(f"\n{failed} failed")
        return 1
    print("\nAll passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
