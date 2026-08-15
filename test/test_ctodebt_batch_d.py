"""SWARM_CTO_GUIDE Batch D 回归测试 — SSRF 防护 + token 生命周期能力。

覆盖：P0-SEC-04 出站 webhook SSRF 拦截策略、P0-SEC-01 revoke_user_token 存在性。
（写端点 RBAC 与 token DB 过滤需 DB，由集成测试覆盖；此处测纯逻辑。）
"""
from __future__ import annotations

import pytest


def test_ssrf_blocks_cloud_metadata():
    from swarm.api.notify import _ssrf_unsafe_reason

    assert _ssrf_unsafe_reason("http://169.254.169.254/latest/meta-data/") is not None


def test_ssrf_blocks_loopback_and_localhost():
    from swarm.api.notify import _ssrf_unsafe_reason

    assert _ssrf_unsafe_reason("http://127.0.0.1:8080/x") is not None
    assert _ssrf_unsafe_reason("http://localhost/x") is not None


def test_ssrf_blocks_non_http_scheme():
    from swarm.api.notify import _ssrf_unsafe_reason

    assert _ssrf_unsafe_reason("file:///etc/passwd") is not None
    assert _ssrf_unsafe_reason("gopher://x/") is not None


def test_ssrf_allows_public_webhook():
    from swarm.api.notify import _ssrf_unsafe_reason

    assert _ssrf_unsafe_reason("https://open.feishu.cn/open-apis/bot/v2/hook/abc") is None


def test_ssrf_empty_or_garbage():
    from swarm.api.notify import _ssrf_unsafe_reason

    assert _ssrf_unsafe_reason("not-a-url") is not None  # 无协议/主机


def test_revoke_user_token_exists():
    """P0-SEC-01：吊销能力存在（DDL 含 token_revoked/token_expires_at，lookup 已过滤）。

    ★批25 GS-5w 换锁★（lookup 过滤原实现=getsource 断 "token_revoked = false" 等
    SQL 谓词字面）。行为锁：mock 池化连接真调 get_user_by_token，断【实际发出的
    查询】的谓词形态（不只列名——R1 reviewer HIGH：列名钉不住极性）。
    删什么变红：lookup 的 WHERE 摘掉/翻转吊销或过期过滤 → 发出的 SQL 谓词形态
    不符 → 红（=被吊销/过期 token 照常认证通过的 P0 事故回归）。
    语义端到端侧：吊销半边由 test_f1_token_hash_at_rest 真库往返锁覆盖；
    过期半边由本文件 test_expired_token_rejected_end_to_end 真库锁覆盖。
    """
    import inspect

    from swarm.auth import store

    assert hasattr(store, "revoke_user_token")
    src = inspect.getsource(store)
    assert "token_revoked" in src and "token_expires_at" in src

    executed: list[str] = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            executed.append(sql)

        def fetchone(self):
            return None

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return _Cur()

    orig = store._pooled_conn
    store._pooled_conn = lambda conn_str=None: _Conn()
    try:
        assert store.get_user_by_token("tok-abc") is None
    finally:
        store._pooled_conn = orig
    assert executed, "夹具自检：lookup 必须真发出一次查询"
    sql = executed[0]
    # ★批25 R1 reviewer HIGH 整改★：列名存在性钉不住谓词极性（token_revoked 出现在
    # SQL 里 ≠ 过滤方向对——`= true` 或漏否定照样含列名）。空白归一后断【实际发出的
    # 查询】的谓词形态：`= false`（只要未吊销）与 `IS NULL OR ... > now()`（未过期）。
    norm = " ".join(sql.split()).lower()
    assert "token_revoked = false" in norm or "not token_revoked" in norm, \
        f"lookup 必须只要【未吊销】token（极性就是命题本体）: {norm}"
    assert "token_expires_at is null or token_expires_at > now()" in norm, \
        f"lookup 必须只要【未过期】token（极性翻转 < now() 即 P0-SEC-01 事故）: {norm}"


@pytest.mark.needs_service("pg")
def test_expired_token_rejected_end_to_end():
    """★批25 R1 reviewer HIGH 整改★：过期半边此前全仓无任何行为锁——
    吊销半边有 test_f1_token_hash_at_rest::test_revoke_then_relogin_rotation_restores
    真库往返，而过期只有「发出的 SQL 含 token_expires_at 列名」——`> now()` 被翻转成
    `< now()`（只认已过期 token）全绿，正是 P0-SEC-01 事故形态。
    真库往返：拨过期必须查无此人；拨回未来必须照常认证（对照臂防全拒假防护）。
    夹具形状同 F1（uuid 用户名 + finally 删行）。"""
    import uuid

    from swarm.auth import store

    uname = f"b25exp-{uuid.uuid4().hex[:8]}"
    user = store.create_user(username=uname, password="pw123456")

    def _set_expiry(expr: str):
        with store._pooled_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE swarm_users SET token_expires_at = {expr} WHERE id = %s",
                (user.id,))

    try:
        tok = user.api_token
        assert store.get_user_by_token(tok) is not None, "前提自证：新 token 必须可用"
        _set_expiry("now() - interval '1 hour'")
        assert store.get_user_by_token(tok) is None, \
            "过期 token 必须查无此人（极性翻转为 < now() 此臂照样绿→必须红）"
        _set_expiry("now() + interval '1 hour'")
        assert store.get_user_by_token(tok) is not None, \
            "未过期 token 必须照常认证（防全拒假防护臂）"
    finally:
        with store._pooled_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM swarm_users WHERE id = %s", (user.id,))


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
