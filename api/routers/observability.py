"""api/routers/observability.py — 可观测域路由 (OpenLIT/ClickHouse LLM/embed/rerank trace)。

从 swarm.observability.clickhouse 暴露只读查询为 /api/observability/*。
ClickHouse 未配置/不可达时各端点返回 {"available": false, ...}，前端据此降级显示"未配置"，
而非报错 — 保持面板在无后端数据源时仍可加载。

风格对齐 config.py：APIRouter() + `import swarm.api.app as _app` 反向引用，统一 tag。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

import swarm.api.app as _app  # noqa: F401  (保持与其它 router 一致的反向引用约定)
from swarm.observability import clickhouse as ch

router = APIRouter()

_TAG = "可观测"


@router.get("/api/observability/ping", tags=[_TAG])
async def obs_ping():
    """探活：ClickHouse 数据源是否可达。前端用它决定面板是否降级。"""
    ok = await _run(ch.ping)
    return {"available": bool(ok)}


@router.get("/api/observability/summary", tags=[_TAG])
async def obs_summary(hours: int = Query(24, ge=1, le=720)):
    """面板顶部概览卡：总调用 / 错误数 / embed & llm p95。"""
    return await _run(ch.summary, hours)


@router.get("/api/observability/latency", tags=[_TAG])
async def obs_latency(
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(25, ge=1, le=200),
):
    """各 SpanName 调用量 + 延迟分位 (p50/p95/p99/max) + 错误数。"""
    return await _run(ch.latency_by_span, hours, limit)


@router.get("/api/observability/timeseries", tags=[_TAG])
async def obs_timeseries(
    hours: int = Query(24, ge=1, le=720),
    bucket_minutes: int = Query(60, ge=1, le=1440),
):
    """调用量时间序列（按 bucket 分桶，区分 llm/embed/rerank/other）。"""
    return await _run(ch.calls_timeseries, hours, bucket_minutes)


@router.get("/api/observability/slow", tags=[_TAG])
async def obs_slow(
    hours: int = Query(24, ge=1, le=720),
    threshold_ms: int = Query(5000, ge=1),
    limit: int = Query(20, ge=1, le=200),
):
    """近 N 小时最慢调用（> threshold_ms），定位 stall。"""
    return await _run(ch.slow_calls, hours, threshold_ms, limit)


async def _run(fn, *args):
    """clickhouse.* 是同步 requests 调用，丢到线程池避免阻塞事件循环。"""
    import asyncio

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args))
