"""C6（round22, P2·多用户）：SSE/WS 连接后不再续检 token/成员资格。

根因：鉴权仅在连接建立时一次；建立后 token 吊销/成员移除仍能收敏感进度至断开。

治本：流循环内每心跳(~30s)重校——_stream_reauthorized 重读 token(get_user_by_token 过滤
revoked/expired) + user_can_on_project；失权即断流。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from swarm.api.routers import task as taskmod


def test_reauth_true_when_valid():
    with patch("swarm.api._shared._require_user", return_value=MagicMock()), \
         patch("swarm.auth.store.user_can_on_project", return_value=True):
        assert taskmod._stream_reauthorized(MagicMock(), {"project_id": "p1"}, "task:read") is True


def test_reauth_false_when_token_revoked():
    def _raise(_req):
        raise Exception("token revoked/expired")

    with patch("swarm.api._shared._require_user", side_effect=_raise):
        assert taskmod._stream_reauthorized(MagicMock(), {"project_id": "p1"}, "task:read") is False


def test_reauth_false_when_membership_removed():
    with patch("swarm.api._shared._require_user", return_value=MagicMock()), \
         patch("swarm.auth.store.user_can_on_project", return_value=False):
        assert taskmod._stream_reauthorized(MagicMock(), {"project_id": "p1"}, "task:read") is False


def test_reauth_false_when_task_none():
    with patch("swarm.api._shared._require_user", return_value=MagicMock()), \
         patch("swarm.auth.store.user_can_on_project", return_value=True):
        assert taskmod._stream_reauthorized(MagicMock(), None, "task:read") is False


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
