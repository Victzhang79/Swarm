"""环境数值的 fail-safe 解析原语。"""

from __future__ import annotations

import logging
import math
import os

logger = logging.getLogger(__name__)


def env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw not in (None, "") else int(default)
    except (TypeError, ValueError):
        logger.warning("%s=%r 非法，回退默认值 %d", name, raw, default)
        value = int(default)
    if minimum is not None and value < minimum:
        logger.warning("%s=%r 低于下限 %d，已夹紧", name, raw, minimum)
        value = minimum
    return value


def env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.environ.get(name)
    try:
        value = float(raw) if raw not in (None, "") else float(default)
    except (TypeError, ValueError):
        logger.warning("%s=%r 非法，回退默认值 %.3f", name, raw, default)
        value = float(default)
    if not math.isfinite(value):
        logger.warning("%s=%r 不是有限数，回退默认值 %.3f", name, raw, default)
        value = float(default)
    if minimum is not None and value < minimum:
        logger.warning("%s=%r 低于下限 %.3f，已夹紧", name, raw, minimum)
        value = minimum
    return value
