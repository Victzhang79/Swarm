#!/usr/bin/env python3
"""F1（round28，安全 P1）：API token 明文存库 → 改为 SHA256 at-rest 哈希 + 登录轮换。

根因：`swarm_users.api_token` 明文存储且 `get_user_by_token` 按明文 `WHERE api_token=%s` 查——
DB 转储/泄露即等同全体长期凭据泄露。token 是 secrets.token_urlsafe(32)=256bit 高熵随机，
用快速 SHA256 存 hash（非 PBKDF2 慢哈希，那是给低熵口令的）即可消除明文风险。

治本（单 token + 登录轮换模型，用户拍板）：
- 只存 token_hash（UNIQUE 查找键），铸造时（create_user/bootstrap/登录轮换）才见明文一次；
- 迁移 v4 回填既有明文→hash 并清空 api_token，既有 token 不失效（继续可用）；
- get_user_by_token 先 hash 再查；rotate_user_token 铸新（登录调用）。

本测试跑真实 PG（与 test_auth_login 同，经 swarm_bootstrap 载 .env）。
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.auth import store
from swarm.auth.passwords import hash_token
from swarm.infra.migrations.runner import _migration_v4_token_hash, run_migrations

# 确保 token_hash 列/迁移已就位（幂等；回填非失效——既有 token 仍可用）。
run_migrations(None)


def _raw(sql, params=None):
    with store._pooled_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            try:
                return cur.fetchone()
            except Exception:
                return None


def test_hash_token_stable_and_hex():
    t = "abc123"
    assert hash_token(t) == hash_token(t)
    assert len(hash_token(t)) == 64
    assert hash_token("") == ""
    print("  ✅ hash_token 定长十六进制稳定，空→空")


def test_create_user_stores_hash_not_plaintext():
    uname = f"f1u-{uuid.uuid4().hex[:8]}"
    user = store.create_user(username=uname, password="pw123456")
    try:
        plaintext = user.api_token
        assert plaintext, "创建应返回一次性明文 token"
        # DB 里：api_token 明文已清空，token_hash = sha256(plaintext)
        row = _raw("SELECT api_token, token_hash FROM swarm_users WHERE id=%s", (user.id,))
        assert row is not None
        assert row[0] is None, f"api_token 明文不应存库，实得 {row[0]!r}"
        assert row[1] == hash_token(plaintext), "token_hash 应为明文的 SHA256"
        # 按明文 token 能查回该用户（中间层 hash 后匹配）
        got = store.get_user_by_token(plaintext)
        assert got is not None and got.id == user.id
        # 拿 hash 当 token 查不到（防"泄 hash 即登录"）
        assert store.get_user_by_token(hash_token(plaintext)) is None
    finally:
        _raw("DELETE FROM swarm_users WHERE id=%s", (user.id,))
    print("  ✅ create_user 存 hash 不存明文，明文可查、hash 不可当 token")


def test_rotate_user_token_invalidates_old():
    uname = f"f1r-{uuid.uuid4().hex[:8]}"
    user = store.create_user(username=uname, password="pw123456")
    try:
        old = user.api_token
        new = store.rotate_user_token(user.id)
        assert new and new != old, "轮换应产生新明文"
        assert store.get_user_by_token(old) is None, "旧 token 轮换后失效"
        got = store.get_user_by_token(new)
        assert got is not None and got.id == user.id, "新 token 可用"
    finally:
        _raw("DELETE FROM swarm_users WHERE id=%s", (user.id,))
    print("  ✅ rotate_user_token 铸新 + 旧失效")


def test_migration_v4_backfills_legacy_plaintext():
    """回填：既有明文行 → token_hash 填好 + api_token 清空，且原 token 仍可用（不失效）。"""
    uid = str(uuid.uuid4())
    uname = f"f1m-{uuid.uuid4().hex[:8]}"
    legacy_token = "legacy-plaintext-token-" + uuid.uuid4().hex
    # 直插一条【旧格式】行：api_token 明文、token_hash 为空
    _raw(
        "INSERT INTO swarm_users (id, username, api_token, token_hash, global_role) "
        "VALUES (%s,%s,%s,NULL,'developer')",
        (uid, uname, legacy_token),
    )
    try:
        with store._pooled_conn() as conn:
            _migration_v4_token_hash(conn)
            conn.commit()
        row = _raw("SELECT api_token, token_hash FROM swarm_users WHERE id=%s", (uid,))
        assert row[0] is None, "回填后明文应清空"
        assert row[1] == hash_token(legacy_token), "回填 token_hash 应为原明文 SHA256"
        # 既有 token 不失效：仍能按原明文查回
        got = store.get_user_by_token(legacy_token)
        assert got is not None and got.id == uid, "回填后既有 token 仍可用"
    finally:
        _raw("DELETE FROM swarm_users WHERE id=%s", (uid,))
    print("  ✅ 迁移 v4 回填明文→hash、清空明文、既有 token 不失效")


def test_revoke_then_relogin_rotation_restores():
    """F9：吊销令牌→立即认证失败；F1 登录轮换铸新 token 并清 revoked→恢复可用。"""
    uname = f"f9-{uuid.uuid4().hex[:8]}"
    user = store.create_user(username=uname, password="pw123456")
    try:
        tok = user.api_token
        assert store.get_user_by_token(tok) is not None
        assert store.revoke_user_token(user.id) is True
        assert store.get_user_by_token(tok) is None, "吊销后 token 立即失效"
        # 模拟重新登录（rotate_user_token 同登录路径）→ 新 token 可用（清 revoked）
        new = store.rotate_user_token(user.id)
        assert store.get_user_by_token(new) is not None, "轮换后新 token 恢复可用"
    finally:
        _raw("DELETE FROM swarm_users WHERE id=%s", (user.id,))
    print("  ✅ 吊销即失效 + 重登轮换恢复")


if __name__ == "__main__":
    for fn in (
        test_hash_token_stable_and_hex,
        test_create_user_stores_hash_not_plaintext,
        test_rotate_user_token_invalidates_old,
        test_migration_v4_backfills_legacy_plaintext,
        test_revoke_then_relogin_rotation_restores,
    ):
        fn()
    print("\nF1 token hash-at-rest 单测通过。")
