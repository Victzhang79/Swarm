#!/usr/bin/env python3
"""WebSocket 端点 + 外部通知模块 测试"""

from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ═══════════════════════════════════════════════════
# notify 模块单元测试
# ═══════════════════════════════════════════════════

def test_notify_skip_when_no_env():
    """未配置 SWARM_NOTIFY_WEBHOOK_URL → 静默跳过"""
    from swarm.api.notify import notify

    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("SWARM_NOTIFY_WEBHOOK_URL", None)
        result = asyncio.run(notify("task_completed", "t1", "done"))
        assert result is False
    print("  ✅ notify: 未配置环境变量时静默跳过")


def test_notify_generic_format():
    """配置 generic 格式 → POST 正确 payload"""
    from swarm.api.notify import notify

    async def _test():
        with patch.dict(os.environ, {
            "SWARM_NOTIFY_WEBHOOK_URL": "https://example.com/hook",
            "SWARM_NOTIFY_FORMAT": "generic",
        }):
            with patch("swarm.api.notify.httpx.AsyncClient") as mock_client_cls:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.text = "ok"

                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await notify("task_completed", "t1", "任务完成")
                assert result is True
                mock_client.post.assert_called_once()
                payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1].get("json")
                assert payload["event_type"] == "task_completed"
                assert payload["task_id"] == "t1"
                assert payload["text"] == "任务完成"

    asyncio.run(_test())
    print("  ✅ notify: generic 格式 POST 正确")


def test_notify_feishu_format():
    """配置 feishu 格式 → 飞书消息格式"""
    from swarm.api.notify import notify

    async def _test():
        with patch.dict(os.environ, {
            "SWARM_NOTIFY_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx",
            "SWARM_NOTIFY_FORMAT": "feishu",
        }):
            with patch("swarm.api.notify.httpx.AsyncClient") as mock_client_cls:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.text = "ok"

                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await notify("task_failed", "t2", "执行失败")
                assert result is True
                payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1].get("json")
                assert payload["msg_type"] == "text"
                assert "content" in payload
                assert "task_failed" in payload["content"]["text"]

    asyncio.run(_test())
    print("  ✅ notify: feishu 格式正确")


def test_notify_slack_format():
    """配置 slack 格式 → Slack 消息格式"""
    from swarm.api.notify import notify

    async def _test():
        with patch.dict(os.environ, {
            "SWARM_NOTIFY_WEBHOOK_URL": "https://hooks.slack.com/services/xxx",
            "SWARM_NOTIFY_FORMAT": "slack",
        }):
            with patch("swarm.api.notify.httpx.AsyncClient") as mock_client_cls:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.text = "ok"

                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await notify("awaiting_review", "t3", "等待审核")
                assert result is True
                payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1].get("json")
                assert "text" in payload
                assert "awaiting_review" in payload["text"]

    asyncio.run(_test())
    print("  ✅ notify: slack 格式正确")


def test_notify_http_error():
    """Webhook 返回非 2xx → 返回 False 但不抛异常"""
    from swarm.api.notify import notify

    async def _test():
        with patch.dict(os.environ, {
            "SWARM_NOTIFY_WEBHOOK_URL": "https://example.com/hook",
        }):
            with patch("swarm.api.notify.httpx.AsyncClient") as mock_client_cls:
                mock_resp = MagicMock()
                mock_resp.status_code = 500
                mock_resp.text = "internal error"

                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await notify("task_completed", "t1", "done")
                assert result is False

    asyncio.run(_test())
    print("  ✅ notify: HTTP 错误时返回 False 不抛异常")


# ═══════════════════════════════════════════════════
# WebSocket 端点集成测试
# ═══════════════════════════════════════════════════

def test_ws_task_progress_receives_events():
    """WebSocket 端点能正确接收并推送任务进度事件"""
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    task = {"id": "task-ws-1", "project_id": "p1", "status": "DISPATCHING"}

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_task.return_value = task

        # 预填充 fanout 主题（N-CW1：pub/sub 多订阅扇出 + 历史回放）
        from swarm.brain.runner import _FanoutTopic

        topic = _FanoutTopic()
        topic.publish({"step": "progress", "message": "正在执行", "progress": 50})
        topic.publish({"step": "complete", "message": "执行完成", "progress": 100})

        # subscribe_task 返回 (主题, 已回放历史的专属订阅队列)
        with patch("swarm.brain.runner.subscribe_task", return_value=(topic, topic.subscribe())):
                client = TestClient(app)
                with client.websocket_connect("/ws/tasks/task-ws-1") as ws:
                    # 接收进度事件
                    data1 = ws.receive_json()
                    assert data1["event"] == "progress"
                    assert data1["data"]["step"] == "progress"
                    assert data1["data"]["progress"] == 50

                    # 接收终止事件
                    data2 = ws.receive_json()
                    assert data2["event"] == "progress"
                    assert data2["data"]["step"] == "complete"

    print("  ✅ WebSocket /ws/tasks/{task_id} 接收进度事件")


def test_ws_task_not_found():
    """WebSocket 端点：任务不存在 → 推送 error 并关闭"""
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_task.return_value = None

        with patch("swarm.brain.runner.get_task_queue", return_value=None):
            with patch("swarm.brain.runner.register_task_queue") as mock_reg:
                mock_q = asyncio.Queue()
                mock_reg.return_value = mock_q
                client = TestClient(app)
                with client.websocket_connect("/ws/tasks/nonexistent") as ws:
                    data = ws.receive_json()
                    assert data["event"] == "error"
                    assert "not found" in data["data"]["detail"].lower()

    print("  ✅ WebSocket 404: 任务不存在时推送错误")


if __name__ == "__main__":
    test_notify_skip_when_no_env()
    test_notify_generic_format()
    test_notify_feishu_format()
    test_notify_slack_format()
    test_notify_http_error()
    test_ws_task_progress_receives_events()
    test_ws_task_not_found()
    print("\n✅ 全部 WebSocket + 通知模块测试通过")
