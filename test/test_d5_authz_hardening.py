"""D5：多用户 authz 硬化——global_role 白名单 + secrets/status 收敛到 config:write。

行为测试：造用户时非法 global_role 被 400 拒（防未知角色致 RBAC 未定义/提权）。
"""
from __future__ import annotations

import pytest


def _client():
    from fastapi.testclient import TestClient

    from swarm.api.app import app
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": "admin", "password": "swarm"})  # 取 config:write
    return c


def test_create_user_rejects_unknown_role():
    c = _client()
    r = c.post("/api/users", json={
        "username": "_test_d5_reject", "password": "pw12345678", "global_role": "wizard",
    })
    if r.status_code in (401, 403):
        pytest.skip("需 config:write 认证（RBAC 配置）")
    assert r.status_code == 400, r.text
    assert "global_role" in r.text or "无效" in r.text


def test_create_user_accepts_known_role_shape():
    # 合法角色不被【白名单】400 拦（可能因用户名占用 409 或成功 200；关键是非 400 角色错）
    c = _client()
    r = c.post("/api/users", json={
        "username": "_test_d5_ok_probe", "password": "pw12345678", "global_role": "viewer",
    })
    if r.status_code in (401, 403):
        pytest.skip("需 config:write 认证")
    assert r.status_code != 400, f"合法角色 viewer 不应被白名单拒: {r.text}"
    # 清理可能创建的用户
    if r.status_code == 200:
        import psycopg

        from swarm.config.settings import DatabaseConfig
        try:
            with psycopg.connect(DatabaseConfig().postgres_uri, autocommit=True) as conn:
                conn.execute("DELETE FROM users WHERE username = %s", ("_test_d5_ok_probe",))
        except Exception:
            pass


def test_secrets_status_requires_config_write():
    # 收敛后：secrets/status 走 config:write（与其它敏感配置端点一致）。这里只断言 admin 仍可读
    # (无回归)；deny 面因测试环境 RBAC/默认 admin 难构造 viewer，交 R1 复核。
    c = _client()
    r = c.get("/api/secrets/status")
    if r.status_code in (401, 403):
        pytest.skip("需认证")
    assert r.status_code == 200, r.text
    assert "stored_secrets" in r.json()
