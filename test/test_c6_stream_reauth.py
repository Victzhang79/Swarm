"""C6（round22, P2·多用户）：SSE/WS 连接后不再续检 token/成员资格。

根因：鉴权仅在连接建立时一次；建立后 token 吊销/成员移除仍能收敏感进度至断开。

治本：流循环内每心跳(~30s)重校——_stream_reauthorized 重读 token(get_user_by_token 过滤
revoked/expired) + user_can_on_project；失权即断流。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from swarm.api.routers import task as taskmod


def test_reauth_true_when_valid():
    with patch("swarm.api.auth._extract_token", return_value="tok"), \
         patch("swarm.api.auth.resolve_user", return_value=MagicMock(must_change_password=False)), \
         patch("swarm.auth.store.user_can_on_project", return_value=True):
        assert taskmod._stream_reauthorized(MagicMock(), {"project_id": "p1"}, "task:read") is True


def test_reauth_false_when_token_revoked():
    with patch("swarm.api.auth._extract_token", return_value="revoked"), \
         patch("swarm.api.auth.resolve_user", return_value=None) as resolve:
        assert taskmod._stream_reauthorized(MagicMock(), {"project_id": "p1"}, "task:read") is False
    resolve.assert_called_once_with("revoked")


def test_reauth_false_when_membership_removed():
    with patch("swarm.api.auth._extract_token", return_value="tok"), \
         patch("swarm.api.auth.resolve_user", return_value=MagicMock(must_change_password=False)), \
         patch("swarm.auth.store.user_can_on_project", return_value=False):
        assert taskmod._stream_reauthorized(MagicMock(), {"project_id": "p1"}, "task:read") is False


def test_reauth_false_when_task_none():
    with patch("swarm.api.auth._extract_token", return_value="tok"), \
         patch("swarm.api.auth.resolve_user", return_value=MagicMock(must_change_password=False)), \
         patch("swarm.auth.store.user_can_on_project", return_value=True):
        assert taskmod._stream_reauthorized(MagicMock(), None, "task:read") is False


async def test_task_sse_reauth_deadline_fires_under_continuous_events(monkeypatch):
    """队列持续有事件也必须按墙钟重认证，不能只在 queue timeout 时触发。"""
    import asyncio
    from unittest.mock import AsyncMock

    class Queue:
        async def get(self):
            return {"step": "working", "message": "busy"}

    class Topic:
        def unsubscribe(self, _queue):
            pass

    times = iter([0.0, 0.2, 0.4, 0.6, 0.8])
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "time", lambda: next(times, 1.0))
    monkeypatch.setattr(taskmod, "_sse_reauth_interval_s", lambda: 0.1)
    monkeypatch.setattr(taskmod, "_stream_reauthorized", lambda *a, **k: False)
    monkeypatch.setattr(taskmod, "_require_task_access_async", AsyncMock())
    monkeypatch.setattr(taskmod._app.store, "get_task", lambda _tid: {"project_id": "p"})
    monkeypatch.setattr("swarm.brain.runner.subscribe_task", lambda _tid: (Topic(), Queue()))

    response = await taskmod.stream_task("t", MagicMock())
    first = await anext(response.body_iterator)
    assert first["event"] == "error"
    import json
    assert "认证已失效" in json.loads(first["data"])["message"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
