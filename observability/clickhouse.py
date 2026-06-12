"""OpenLIT/ClickHouse 可观测查询 — LLM/embed/rerank 调用延迟与量。

数据源：OpenLIT 写入 ClickHouse 的 otel_traces 表(OTEL 标准 schema)。
列：Timestamp(DateTime64 ns), SpanName, Duration(UInt64 ns), StatusCode,
    ServiceName, SpanAttributes(Map)。

通过 ClickHouse HTTP 接口查询(只读)，全部带时间窗与 LIMIT，避免全表扫。
连接失败/未配置时各函数返回 {"available": False, ...}，前端据此降级。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _cfg():
    from swarm.config.settings import get_config
    return get_config().observability


def _query(sql: str) -> list[dict[str, Any]] | None:
    """执行 ClickHouse HTTP 查询，返回 JSON rows；失败返回 None。"""
    cfg = _cfg()
    base = (cfg.clickhouse_http_url or "").strip().rstrip("/")
    if not base:
        return None
    import requests
    params = {
        "user": cfg.clickhouse_user,
        "password": cfg.clickhouse_password,
        "database": cfg.clickhouse_database,
        "default_format": "JSON",
    }
    try:
        resp = requests.post(base + "/", params=params, data=sql.encode("utf-8"),
                             timeout=cfg.query_timeout)
        if resp.status_code != 200:
            logger.warning("ClickHouse 查询失败 status=%s: %s", resp.status_code, resp.text[:200])
            return None
        return resp.json().get("data", [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("ClickHouse 查询异常: %s", exc)
        return None


def ping() -> bool:
    """探活：ClickHouse 是否可达。"""
    rows = _query("SELECT 1 AS ok")
    return bool(rows)


def latency_by_span(hours: int = 24, limit: int = 25) -> dict[str, Any]:
    """各 SpanName 的调用量 + 延迟分位(ms)，近 N 小时。"""
    sql = f"""
        SELECT SpanName AS span,
               count() AS calls,
               round(quantile(0.5)(Duration)/1e6, 1) AS p50_ms,
               round(quantile(0.95)(Duration)/1e6, 1) AS p95_ms,
               round(quantile(0.99)(Duration)/1e6, 1) AS p99_ms,
               round(max(Duration)/1e6, 1) AS max_ms,
               countIf(StatusCode='STATUS_CODE_ERROR') AS errors
        FROM otel_traces
        WHERE Timestamp > now() - INTERVAL {int(hours)} HOUR
        GROUP BY SpanName
        ORDER BY calls DESC
        LIMIT {int(limit)}
    """
    rows = _query(sql)
    if rows is None:
        return {"available": False, "rows": []}
    return {"available": True, "hours": hours, "rows": rows}


def calls_timeseries(hours: int = 24, bucket_minutes: int = 60) -> dict[str, Any]:
    """调用量时间序列（按 bucket 分桶），区分 LLM/embed/其他。"""
    sql = f"""
        SELECT toStartOfInterval(Timestamp, INTERVAL {int(bucket_minutes)} MINUTE) AS ts,
               multiIf(
                 positionCaseInsensitive(SpanName, 'embedding') > 0, 'embedding',
                 positionCaseInsensitive(SpanName, 'chat/completions') > 0, 'llm_chat',
                 positionCaseInsensitive(SpanName, 'rerank') > 0, 'rerank',
                 'other'
               ) AS kind,
               count() AS calls,
               round(avg(Duration)/1e6, 1) AS avg_ms
        FROM otel_traces
        WHERE Timestamp > now() - INTERVAL {int(hours)} HOUR
        GROUP BY ts, kind
        ORDER BY ts
    """
    rows = _query(sql)
    if rows is None:
        return {"available": False, "rows": []}
    return {"available": True, "hours": hours, "bucket_minutes": bucket_minutes, "rows": rows}


def slow_calls(hours: int = 24, threshold_ms: int = 5000, limit: int = 20) -> dict[str, Any]:
    """近 N 小时最慢的调用（>threshold_ms），定位 stall。"""
    sql = f"""
        SELECT Timestamp AS ts,
               SpanName AS span,
               round(Duration/1e6, 0) AS ms,
               StatusCode AS status,
               SpanAttributes['gen_ai.request.model'] AS model
        FROM otel_traces
        WHERE Timestamp > now() - INTERVAL {int(hours)} HOUR
          AND Duration > {int(threshold_ms)} * 1000000
        ORDER BY Duration DESC
        LIMIT {int(limit)}
    """
    rows = _query(sql)
    if rows is None:
        return {"available": False, "rows": []}
    return {"available": True, "hours": hours, "threshold_ms": threshold_ms, "rows": rows}


def summary(hours: int = 24) -> dict[str, Any]:
    """面板顶部概览卡：总调用/错误率/embed 与 llm 的 p95。"""
    sql = f"""
        SELECT
          count() AS total_calls,
          countIf(StatusCode='STATUS_CODE_ERROR') AS total_errors,
          round(quantile(0.95)(if(positionCaseInsensitive(SpanName,'embedding')>0, Duration, null))/1e6, 1) AS embed_p95_ms,
          round(quantile(0.95)(if(positionCaseInsensitive(SpanName,'chat/completions')>0, Duration, null))/1e6, 1) AS llm_p95_ms,
          round(max(if(positionCaseInsensitive(SpanName,'chat/completions')>0, Duration, 0))/1e6, 0) AS llm_max_ms
        FROM otel_traces
        WHERE Timestamp > now() - INTERVAL {int(hours)} HOUR
    """
    rows = _query(sql)
    if not rows:
        return {"available": False}
    r = rows[0]
    return {"available": True, "hours": hours, **r}
