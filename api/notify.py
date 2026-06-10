"""可选外部 Webhook 通知模块

通过环境变量配置：
  SWARM_NOTIFY_WEBHOOK_URL — webhook 地址（未配置则静默跳过）
  SWARM_NOTIFY_FORMAT     — 消息格式: generic|feishu|slack（默认 generic）

支持飞书/钉钉 incoming webhook、Slack webhook、通用 HTTP POST。
未配置 SWARM_NOTIFY_WEBHOOK_URL 时所有调用静默跳过，不报错不阻断。
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ─── 消息格式构建 ───────────────────────────────────

def _build_payload(
    format_type: str,
    event_type: str,
    task_id: str,
    message: str,
    **kw: Any,
) -> dict[str, Any]:
    """根据格式类型构建 webhook 请求体"""
    if format_type == "feishu":
        # 飞书/钉钉 incoming webhook 格式
        return {
            "msg_type": "text",
            "content": {
                "text": f"[Swarm] {event_type} | task={task_id} | {message}",
            },
        }
    elif format_type == "slack":
        # Slack webhook 格式
        return {
            "text": f"[Swarm] {event_type} | task={task_id} | {message}",
        }
    else:
        # 通用格式
        return {
            "event_type": event_type,
            "task_id": task_id,
            "text": message,
            **kw,
        }


# ─── 核心通知函数 ───────────────────────────────────

async def notify(
    event_type: str,
    task_id: str,
    message: str,
    **kw: Any,
) -> bool:
    """发送外部 Webhook 通知

    Args:
        event_type: 事件类型（task_completed / task_failed / awaiting_review 等）
        task_id: 任务 ID
        message: 通知消息
        **kw: 附加字段（仅 generic 格式会合并到 payload）

    Returns:
        True=发送成功，False=跳过或失败
    """
    webhook_url = os.environ.get("SWARM_NOTIFY_WEBHOOK_URL", "").strip()
    if not webhook_url:
        # 未配置 → 静默跳过
        return False

    format_type = os.environ.get("SWARM_NOTIFY_FORMAT", "generic").strip().lower()
    payload = _build_payload(format_type, event_type, task_id, message, **kw)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code < 300:
                logger.info(
                    "Webhook 通知已发送: event=%s task=%s status=%d",
                    event_type, task_id, resp.status_code,
                )
                return True
            else:
                logger.warning(
                    "Webhook 通知失败: event=%s task=%s status=%d body=%s",
                    event_type, task_id, resp.status_code, resp.text[:200],
                )
                return False
    except Exception as exc:
        logger.warning(
            "Webhook 通知异常: event=%s task=%s error=%s",
            event_type, task_id, exc,
        )
        return False
