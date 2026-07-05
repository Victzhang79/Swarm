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

import asyncio
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


def _ssrf_unsafe_reason(url: str) -> str | None:
    """P0-SEC-04：出站 webhook URL SSRF 防护。返回拒绝原因（None=放行）。

    策略：拒绝非 http(s)、回环(127/8、::1、localhost)、链路本地/云元数据(169.254/16，
    含 169.254.169.254 IMDS)。私有网段(10/172.16/192.168)【放行】——本部署内网服务
    (ai.bit 等)合法走内网；只拦最危险的元数据窃取/本机端口扫描面。
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return "URL 无法解析"
    if parsed.scheme not in ("http", "https"):
        return f"不允许的协议: {parsed.scheme or '(空)'}"
    host = parsed.hostname or ""
    if not host:
        return "缺少主机名"
    if host.lower() == "localhost":
        return "禁止回环地址 localhost"
    # 解析所有 A/AAAA，任一命中危险段即拒（防 DNS rebinding 首层）
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except Exception:  # noqa: BLE001 — 解析不了交给后续请求自然失败
        addrs = {host}
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_loopback:
            return f"禁止回环地址: {addr}"
        if ip.is_link_local:  # 169.254.0.0/16 (含 IMDS 169.254.169.254) / fe80::/10
            return f"禁止链路本地/元数据地址: {addr}"
    return None


async def _post_webhook(url: str, payload: dict[str, Any], *, tag: str = "") -> bool:
    """POST 一个 webhook，成功返回 True。异常/非 2xx 返回 False（不抛出）。"""
    if not url:
        return False
    # round27 perf：_ssrf_unsafe_reason 内含同步 DNS 解析（getaddrinfo 无超时，慢 DNS 可
    # 卡事件环数秒）→ 卸线程池。判定逻辑不变。
    _reason = await asyncio.to_thread(_ssrf_unsafe_reason, url)
    if _reason:
        logger.warning("Webhook 被 SSRF 防护拦截 %s: %s (url=%s)", tag, _reason, url[:80])
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
