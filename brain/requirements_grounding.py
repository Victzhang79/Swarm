"""需求引文的连续匹配与前向平铺接地。"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("swarm.brain.requirements_extract")

_QUOTE_MIN_TILE_CHARS = 2
_QUOTE_COVER_MIN = 0.85
_QUOTE_MAX_LEN = 600


def _quote_grounding_params() -> tuple[int, float]:
    """读取有界接地阈值；非法配置告警并回退安全默认值。"""
    def _one(env: str, default, cast, lo=None, hi=None):
        raw = os.environ.get(env, "") or ""
        try:
            value = cast(raw) if raw.strip() else default
        except (ValueError, TypeError):
            logger.warning("[EXTRACT_REQ] %s 配置非法(%r)——回退默认 %r", env, raw, default)
            return default
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            logger.warning(
                "[EXTRACT_REQ] %s 越界(%r，须∈[%r,%r])——回退默认 %r",
                env, value, lo, hi, default,
            )
            return default
        return value

    return (
        _one("SWARM_QUOTE_MIN_TILE_CHARS", _QUOTE_MIN_TILE_CHARS, int, lo=1),
        _one("SWARM_QUOTE_COVER_MIN", _QUOTE_COVER_MIN, float, lo=0.0, hi=1.0),
    )


def quote_grounded_spans(quote: str, source: str) -> list[tuple[int, int]]:
    """返回引文实际消费的源区间；连续匹配优先，否则按源顺序平铺。"""
    if not quote or not source:
        return []
    direct = source.find(quote)
    if direct >= 0:
        return [(direct, direct + len(quote))]
    if len(quote) > _QUOTE_MAX_LEN:
        return []
    min_tile, cover_min = _quote_grounding_params()
    index = 0
    covered = 0
    source_cursor = 0
    spans: list[tuple[int, int]] = []
    while index < len(quote):
        best_length = 0
        best_position = -1
        end = index + 1
        while end <= len(quote):
            position = source.find(quote[index:end], source_cursor)
            if position < 0:
                break
            best_length = end - index
            best_position = position
            end += 1
        if best_length >= min_tile:
            covered += sum(1 for char in quote[index:index + best_length] if char.isalnum())
            spans.append((best_position, best_position + best_length))
            source_cursor = best_position + best_length
            index += best_length
        else:
            index += 1
    total = sum(1 for char in quote if char.isalnum())
    return spans if total > 0 and covered / total >= cover_min else []


def quote_is_grounded(quote: str, source: str) -> bool:
    return bool(quote_grounded_spans(quote, source))


__all__ = ["quote_grounded_spans", "quote_is_grounded"]
