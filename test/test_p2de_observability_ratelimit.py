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


def test_rate_limiter_evicts_idle_buckets_under_cap(monkeypatch):
    """复核 F4：桶数达上限时清扫已满(闲置)桶，防 IP 轮转刷爆内存。

    IP 轮转真实场景：旧 IP 用一次即弃 → 桶经 capacity/rate 秒回填满 → 可回收。这里把旧桶
    _last 老化到过去以模拟闲置（回填公式判其满），第 4 个新主体触发清扫回收它们。"""
    import swarm.api.rate_limit as rl

    monkeypatch.setattr(rl, "_MAX_BUCKETS", 3)
    limiter = rl.RateLimiter()
    for i in range(3):
        limiter.check(f"s:ip{i}", capacity=5, rate=1000.0)
    # 老化：把 3 个旧桶 _last 拨到很久以前 → 下次清扫按回填公式判其满(闲置)可删
    for b in limiter._buckets.values():
        b._last = 0.0
    limiter.check("s:ip_new", capacity=5, rate=1000.0)
    assert len(limiter._buckets) == 1, "达上限应清扫已满闲置桶只留新桶（F4 回归）"


# ── F5：Prometheus label value 转义（行为级，两个转义站点各一条）──

def _hostile(marker: str) -> str:
    """含三种危险字符的取值：反斜杠、双引号、换行。

    换行是真正的注入面 —— 未转义时它会把一条 metric 行**劈成两行**，第二行是攻击者
    完全控制的伪造 metric，Prometheus 会当成真指标收下。

    `marker` 让**每个站点用不同的伪造 metric 名**（双复核 L-2）：共用一个名字时，
    `_assert_label_escaped` 里那条 body 级"伪造行不存在"断言会跨站点误指——A 站点没转义
    却在检查 B 站点时报错，归因错人。
    """
    return f'a\\b"c\nswarm_injected_{marker}_total 999'


_HOSTILE_STATUS = _hostile("status")
_HOSTILE_DEGRADE = _hostile("degrade")


def _assert_label_escaped(body: str, metric: str, label: str, *, marker: str) -> None:
    """断某条 metric 的 label value 三种字符都已转义，且整块仍是合法 exposition。"""
    lines = [ln for ln in body.splitlines() if ln.startswith(metric + "{")]
    assert len(lines) == 1, f"{metric} 应恰有一行（换行未转义会劈成多行）: {lines}"
    line = lines[0]
    # 三重转义逐项断（缺任一项都红，不用「至少 N 个」下界）
    assert "\\\\" in line, f"反斜杠未转义: {line}"
    assert "\\n" in line, f"换行未转义（注入面）: {line}"
    assert '\\"' in line, f"双引号未转义: {line}"
    # 注入未生效：伪造的 metric 名绝不能作为独立行出现（按站点专属 marker 判，不跨站点误指）
    assert not any(ln.startswith(f"swarm_injected_{marker}_total") for ln in body.splitlines()), \
        f"换行注入成功——{metric} 站点的伪造 metric 成了独立行"
    # label value 内部不得有裸引号（会提前闭合 label，后续内容被解析成语法垃圾）
    inner = line[line.index("{") + 1: line.rindex("}")]
    assert inner.startswith(label + '="') and inner.endswith('"'), inner
    val = inner[len(label) + 2: -1]
    assert '"' not in val.replace('\\"', ""), f"label value 内有裸引号: {inner}"


def _metrics_body(monkeypatch, *, statuses=None, degrades=None) -> str:
    import sys

    from fastapi.testclient import TestClient

    import swarm.api.app  # noqa: F401  （只为确保模块已导入）
    from swarm.infra.degrade import reset_degrade_counts

    # ★必须走 sys.modules★：`swarm.api` 包把 FastAPI 实例 re-export 成同名属性 `app`，
    # `import swarm.api.app as m` 拿到的是**那个实例**而非模块（属性遮蔽子模块）。
    appmod = sys.modules["swarm.api.app"]

    if statuses is not None:
        monkeypatch.setattr(appmod.store, "count_tasks_by_status", lambda: statuses)
    reset_degrade_counts()
    if degrades is not None:
        # 只 patch **定义模块**：`metrics()` 里是函数内 `from swarm.infra.degrade import
        # degrade_counts`（调用时解析），所以打在 `swarm.infra.degrade` 上才生效；
        # 打在 `swarm.api.app` 上是无效的（那里根本没有这个模块级名字）。
        import swarm.infra.degrade as dg
        monkeypatch.setattr(dg, "degrade_counts", lambda: degrades)
    resp = TestClient(appmod.app).get("/api/metrics")
    assert resp.status_code == 200, resp.text
    return resp.text


def test_metrics_task_status_label_escaped(monkeypatch):
    """站点① swarm_tasks_total 的 status label 三重转义。

    ★行为级★：原实现只断 `metrics` 源码里 `.replace(` 出现 **≥3** 次。实测源码共 6 次
    （两个站点各 3 次）⇒ 把**任一整个站点**改成完全不转义，剩 3 次仍满足 `>= 3` ⇒ 绿
    （29 号文 T-A3，与 `_BUILDER_VERSION >= 8` 同属已两次出事的下界断言族）。
    """
    body = _metrics_body(monkeypatch, statuses={_HOSTILE_STATUS: 3})
    _assert_label_escaped(body, "swarm_tasks_total", "status", marker="status")


def test_metrics_degrade_label_escaped(monkeypatch):
    """站点② swarm_degrade_total 的 category label 三重转义（与站点①互不背书）。"""
    body = _metrics_body(monkeypatch, degrades={_HOSTILE_DEGRADE: 7})
    _assert_label_escaped(body, "swarm_degrade_total", "category", marker="degrade")


def test_metrics_line_count_stable_under_injection(monkeypatch):
    """注入取值不得改变行数 —— 这是「未转义换行」最直接的机读判据。"""
    clean = _metrics_body(monkeypatch, statuses={"done": 1}, degrades={"a.b": 1})
    hostile = _metrics_body(monkeypatch, statuses={_HOSTILE_STATUS: 1},
                            degrades={_HOSTILE_DEGRADE: 1})
    assert len(clean.splitlines()) == len(hostile.splitlines()), \
        "敌意取值改变了行数 ⇒ 换行未转义，攻击者可凭空插入 metric 行"


def test_resume_binds_project_id_in_logs():
    """复核 F6：resume_task/resume_planning 也把 project_id 绑进日志上下文。"""
    import inspect
    from swarm.brain import runner

    for fn in (runner.resume_task, runner.resume_planning):
        src = inspect.getsource(fn)
        assert "project_id=_resume_project_id" in src, f"{fn.__name__} resume 日志缺 project_id（F6）"


def test_kb_endpoints_have_rate_limit():
    import inspect as _i
    from swarm.api.routers import knowledge

    src = _i.getsource(knowledge)
    assert 'rate_limit("kb_retrieve"' in src
    assert 'rate_limit("kb_ingest"' in src


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
