"""Structured execution audit log lines (grep-friendly JSON payloads)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger("swarm.audit")


def audit(event: str, **fields: Any) -> None:
    """Emit one AUDIT log line with a JSON payload."""
    payload = {"event": event, "ts": round(time.time(), 3), **fields}
    logger.info("AUDIT %s", json.dumps(payload, ensure_ascii=False, default=str))
