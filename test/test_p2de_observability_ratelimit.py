#!/usr/bin/env python3
"""P2-D 可观测（project_id 日志 + /metrics）+ P2-E 限流（令牌桶）回归。"""

from __future__ import annotations

import inspect


# ── P2-D：project_id 注入日志 ─────────────────────────────

def test_logging_binds_project_id():
    import logging as _logging
    from swarm import logging_config as lc

    lc.set_task_context("t-abc", project_id="proj-42")
    try:
        rec = _logging.LogRecord("swarm", _logging.INFO, __file__, 1, "hi", None, None)
        lc._ContextFilter().filter(rec)
        assert rec.project_id == "proj-42"
        # JSON formatter 也带 project_id
        out = lc._JsonFormatter().format(rec)
        assert "proj-42" in out and "project_id" in out
    finally:
        lc.clear_task_context()


def test_runner_binds_project_id():
    import inspect as _i
    from swarm.brain import runner

    src = _i.getsource(runner.run_task)
    assert "project_id=project_id" in src, "run_task 未把 project_id 绑进日志上下文（P2-D 回归）"


# ── P2-D：/metrics 导出 ───────────────────────────────────

def test_metrics_endpoint_source_shape():
    import sys
    import swarm.api.app  # noqa: F401
    appmod = sys.modules["swarm.api.app"]

    src = inspect.getsource(appmod.metrics)
    assert "_require_user" in src, "/metrics 未鉴权（防任务态计数泄漏）"
    assert "swarm_tasks_total" in src and "swarm_scheduler_inflight" in src
    assert "count_tasks_by_status" in src and "queue_stats" in src


def test_queue_stats_shape():
    from swarm.brain import scheduler

    st = scheduler.queue_stats()
    assert set(st) == {"inflight", "pending_meta", "max_concurrent"}
    assert all(isinstance(v, int) for v in st.values())


# ── P2-E：令牌桶限流 ──────────────────────────────────────

def test_token_bucket_allows_burst_then_blocks():
    from swarm.api.rate_limit import RateLimiter

    rl = RateLimiter()
    # capacity=3, rate=0（不回填）→ 前 3 个放行，第 4 个拒
    allowed = [rl.check("k", 3, 0.0)[0] for _ in range(4)]
    assert allowed == [True, True, True, False]


def test_token_bucket_refills_over_time():
    from swarm.api.rate_limit import _TokenBucket

    b = _TokenBucket(capacity=1, rate=10.0)  # 10/s 回填
    t0 = 1000.0
    assert b.take(t0)[0] is True          # 取光
    assert b.take(t0)[1] > 0              # 立即再取 → 拒 + retry_after>0
    assert b.take(t0 + 0.2)[0] is True    # 0.2s 后回填 2 个 → 放行


def test_rate_limit_dep_raises_429(monkeypatch):
    from swarm.api.rate_limit import rate_limit, _limiter
    from fastapi import HTTPException

    _limiter._reset()

    class _Req:
        class state:  # noqa: N801
            user = None
        client = type("C", (), {"host": "1.2.3.4"})()

    dep = rate_limit("tscope", capacity=1, rate=0.0)
    dep(_Req())  # 首次放行
    try:
        dep(_Req())  # 第二次 → 429
        raise AssertionError("应抛 429")
    except HTTPException as e:
        assert e.status_code == 429
        assert "Retry-After" in e.headers


def test_rate_limit_disabled_env(monkeypatch):
    from swarm.api.rate_limit import rate_limit, _limiter

    monkeypatch.setenv("SWARM_RATELIMIT_DISABLED", "1")
    _limiter._reset()

    class _Req:
        class state:  # noqa: N801
            user = None
        client = type("C", (), {"host": "1.2.3.4"})()

    dep = rate_limit("s", capacity=1, rate=0.0)
    dep(_Req()); dep(_Req()); dep(_Req())  # 全放行（限流关闭），不抛


def test_kb_endpoints_have_rate_limit():
    import inspect as _i
    from swarm.api.routers import knowledge

    src = _i.getsource(knowledge)
    assert 'rate_limit("kb_retrieve"' in src
    assert 'rate_limit("kb_ingest"' in src


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
