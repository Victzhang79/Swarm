"""可选外部 Webhook 通知模块

两种使用方式：
  1) dispatch_notification(record) —— 读 config.notify_channels，把一条系统通知
     推送到所有 enabled 且事件匹配的渠道（页面配置的多渠道，推荐）。
  2) notify(event_type, task_id, message) —— 旧的单 webhook（SWARM_NOTIFY_WEBHOOK_URL
     + SWARM_NOTIFY_FORMAT），保留向后兼容。

支持飞书/钉钉/企业微信/Slack incoming webhook + 通用 HTTP POST。
未配置任何渠道时静默跳过，不报错不阻断（通知是非关键路径）。
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
    """根据渠道类型构建 webhook 请求体。"""
    text = f"[Swarm] {event_type}" + (f" | task={task_id}" if task_id else "") + (f" | {message}" if message else "")
    if format_type == "feishu":
        # 飞书群机器人 incoming webhook
        return {"msg_type": "text", "content": {"text": text}}
    elif format_type == "dingtalk":
        # 钉钉群机器人
        return {"msgtype": "text", "text": {"content": text}}
    elif format_type == "wecom":
        # 企业微信群机器人
        return {"msgtype": "text", "text": {"content": text}}
    elif format_type == "slack":
        return {"text": text}
    else:
        # 通用：结构化 JSON
        return {"event_type": event_type, "task_id": task_id, "text": message or text, **kw}


async def _post_webhook(url: str, payload: dict[str, Any], *, tag: str = "") -> bool:
    """POST 一个 webhook，成功返回 True。异常/非 2xx 返回 False（不抛出）。"""
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
            if resp.status_code < 300:
                logger.info("Webhook 通知已发送 %s status=%d", tag, resp.status_code)
                return True
            logger.warning("Webhook 通知失败 %s status=%d body=%s", tag, resp.status_code, resp.text[:200])
            return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("Webhook 通知异常 %s error=%s", tag, exc)
        return False


# ─── 多渠道分发（推荐入口）────────────────────────────

async def dispatch_notification(record: dict[str, Any]) -> int:
    """把一条系统通知推送到所有 enabled 且事件匹配的渠道。

    Args:
        record: store.create_notification 返回的通知记录
                （含 event_type / task_id / title / message / project_id）

    Returns:
        成功推送的渠道数（0 = 未配置/无匹配/全失败，均静默）。
    """
    try:
        from swarm.config.settings import get_config
        channels = get_config().notify_channels
    except Exception as exc:  # noqa: BLE001
        logger.debug("dispatch_notification: 读取 channels 失败 %s", exc)
        return 0

    if not channels:
        return 0

    event_type = str(record.get("event_type") or "")
    task_id = str(record.get("task_id") or "")
    message = str(record.get("message") or record.get("title") or "")

    sent = 0
    for ch in channels:
        if not getattr(ch, "enabled", False):
            continue
        url = (getattr(ch, "webhook_url", "") or "").strip()
        if not url:
            continue
        # events 为空 = 订阅全部；否则按事件过滤
        subscribed = getattr(ch, "events", None) or []
        if subscribed and event_type not in subscribed:
            continue
        payload = _build_payload(getattr(ch, "type", "generic"), event_type, task_id, message)
        ok = await _post_webhook(url, payload, tag=f"channel={getattr(ch,'id','?')} event={event_type}")
        if ok:
            sent += 1
    return sent


# ─── 旧的单 webhook（向后兼容）──────────────────────

async def notify(event_type: str, task_id: str, message: str, **kw: Any) -> bool:
    """[兼容] 旧的单 webhook 推送（SWARM_NOTIFY_WEBHOOK_URL + SWARM_NOTIFY_FORMAT）。

    新代码应改用页面配置的 notify_channels + dispatch_notification。
    """
    webhook_url = os.environ.get("SWARM_NOTIFY_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return False
    format_type = os.environ.get("SWARM_NOTIFY_FORMAT", "generic").strip().lower()
    payload = _build_payload(format_type, event_type, task_id, message, **kw)
    return await _post_webhook(webhook_url, payload, tag=f"legacy event={event_type} task={task_id}")
