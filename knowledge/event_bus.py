"""知识库事件总线 — Redis Stream（可选）。"""

from __future__ import annotations

import json
import logging
from typing import Any

from swarm.infra.redis_client import get_redis

logger = logging.getLogger(__name__)

STREAM_KEY = "swarm:kb_events"


def publish_kb_event(event_type: str, payload: dict[str, Any]) -> bool:
    """发布知识库更新事件。Redis 不可用时静默跳过。"""
    r = get_redis()
    if r is None:
        return False
    try:
        r.xadd(
            STREAM_KEY,
            {
                "type": event_type,
                "payload": json.dumps(payload, ensure_ascii=False),
            },
            maxlen=10000,
        )
        return True
    except Exception as exc:
        logger.warning("[event_bus] publish failed: %s", exc)
        return False
