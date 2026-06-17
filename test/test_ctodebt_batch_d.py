"""SWARM_CTO_GUIDE Batch D 回归测试 — SSRF 防护 + token 生命周期能力。

覆盖：P0-SEC-04 出站 webhook SSRF 拦截策略、P0-SEC-01 revoke_user_token 存在性。
（写端点 RBAC 与 token DB 过滤需 DB，由集成测试覆盖；此处测纯逻辑。）
"""
from __future__ import annotations


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
    """P0-SEC-01：吊销能力存在（DDL 含 token_revoked/token_expires_at，lookup 已过滤）。"""
    import inspect

    from swarm.auth import store

    assert hasattr(store, "revoke_user_token")
    src = inspect.getsource(store)
    assert "token_revoked" in src and "token_expires_at" in src
    # get_user_by_token 必须过滤吊销/过期
    gsrc = inspect.getsource(store.get_user_by_token)
    assert "token_revoked = false" in gsrc
    assert "token_expires_at IS NULL OR token_expires_at > now()" in gsrc


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
