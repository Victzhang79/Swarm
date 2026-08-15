"""SWARM_CTO_GUIDE Batch A 回归测试 — P1 静默失败/fail-closed 根因修复。

覆盖：N-01 扫描器崩溃 fail-closed、N-11 spotbugs XML 解析、N-13 零向量查询、
N-06/N-07 TransientInfraError 分类、P1-DEBT-03 错题/成功强化、P1-SQL-01 make_interval。
"""
from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from swarm.models.errors import TransientInfraError, classify_failure


# ── N-06/N-07：基础设施瞬时失败必须归类 transient（退避重试同模型，不换模型） ──
def test_transient_infra_error_classified_transient():
    assert classify_failure(TransientInfraError("sandbox upload failed")) == "transient"
    # 普通 RuntimeError 不应误判 transient
    assert classify_failure(RuntimeError("empty diff")) != "transient"


# ── N-11：spotbugs 产 XML，必须用 XML 解析（旧代码当 JSON→恒空） ──
def test_spotbugs_xml_parsed_to_findings():
    from swarm.worker import security_scan

    sample_xml = """<?xml version='1.0'?>
    <BugCollection>
      <BugInstance type='SQL_INJECTION' priority='1' category='SECURITY'>
        <ShortMessage>Possible SQL injection</ShortMessage>
        <LongMessage>Concatenated SQL string</LongMessage>
        <SourceLine classname='com.x.Dao' start='42' sourcefile='Dao.java'/>
      </BugInstance>
    </BugCollection>"""

    with patch.object(security_scan.shutil, "which", return_value="/usr/bin/spotbugs"), \
         patch.object(security_scan, "_run_tool", return_value=(0, sample_xml, "")):
        findings = security_scan._sast_java("/tmp/proj")

    assert len(findings) == 1, "spotbugs XML 应解析出 1 条发现（旧 JSON 解析恒为 0）"
    f = findings[0]
    assert f.rule_id == "SQL_INJECTION"
    assert f.line == 42
    assert "Dao.java" in f.file


def test_spotbugs_empty_or_bad_xml_graceful():
    from swarm.worker import security_scan

    with patch.object(security_scan.shutil, "which", return_value="/usr/bin/spotbugs"), \
         patch.object(security_scan, "_run_tool", return_value=(0, "", "")):
        assert security_scan._sast_java("/tmp/proj") == []
    with patch.object(security_scan.shutil, "which", return_value="/usr/bin/spotbugs"), \
         patch.object(security_scan, "_run_tool", return_value=(0, "<not xml", "")):
        assert security_scan._sast_java("/tmp/proj") == []


# ── N-13：embedding 不可用→零向量查询必须返回 []（避免随机错题/成功模式排序） ──
def test_memory_zero_vector_query_returns_empty():
    from swarm.memory.store import MemoryStore

    async def _run():
        store = MemoryStore()
        store._conn = MagicMock()  # 不应被用到（零向量在触库前短路）
        store._embed_fn = AsyncMock(return_value=[[0.0, 0.0, 0.0, 0.0]])
        assert await store.query_mistakes("p", "任意查询") == []
        assert await store.query_successes("p", "任意查询") == []

    asyncio.run(_run())


# ── query_mistakes 带 error_type 时参数顺序必须与 SQL 占位符一致（P1-DEBT-03 强化首次触发的 latent bug） ──
def test_query_mistakes_param_order_with_error_type():
    from swarm.memory.store import MemoryStore

    captured = {}

    class _FakeCur:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = list(params)
        async def fetchall(self):
            return []

    class _FakeConn:
        def cursor(self):
            return _FakeCur()

    async def _run():
        store = MemoryStore()
        store._conn = _FakeConn()
        store._embed_fn = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]])
        await store.query_mistakes("p", "x is None", top_k=1, error_type="integration_failure")

    asyncio.run(_run())
    params = captured["params"]
    # WS1/WS2 后占位符顺序(子查询现算 effective_weight，外层近因融合排序，无第二个向量位)：
    #   similarity(vector) → effective_weight(factor, as_of) → where(project_id) → error_type
    #   → outer where(threshold) → order by(rec_floor, rec_floor) → limit
    assert len(params) == 9, params
    assert params[0].startswith("["), "唯一的 %s::vector 位必须是向量串"
    assert params[1] == 0.9, "L5 衰减因子"
    assert params[2] is None, "as_of 默认 None(→ now())"
    assert params[3] == "p"
    assert params[4] == "integration_failure", "error_type 必须绑到 AND error_type=%s（而非向量/因子位）"
    assert params[5] == 0.05, "effective_weight 阈值"
    assert params[6] == 0.5 and params[7] == 0.5, "近因排序地板 RECENCY_RANK_FLOOR ×2"
    assert params[8] == 1, "top_k"
    # 强化护栏：唯一的向量位是 params[0]，error_type 绝不会落到任何 ::vector 占位符上
    assert sum(1 for p in params if isinstance(p, str) and p.startswith("[")) == 1


# ── P1-DEBT-03：错题重现/成功复用强化已有记录（occurrence_count++/reuse_count++） ──
def test_reinforce_mistake_increments_existing_on_high_similarity():
    from swarm.brain.learn_store import _maybe_reinforce_mistake

    async def _run():
        store = AsyncMock()
        store.query_mistakes = AsyncMock(return_value=[{"id": 11, "similarity": 0.97}])
        store.increment_mistake_occurrence = AsyncMock()
        mid = await _maybe_reinforce_mistake(store, "p", "TypeError", "x is None")
        assert mid == 11
        store.increment_mistake_occurrence.assert_awaited_once_with(11)

    asyncio.run(_run())


def test_reinforce_mistake_skips_low_similarity():
    from swarm.brain.learn_store import _maybe_reinforce_mistake

    async def _run():
        store = AsyncMock()
        store.query_mistakes = AsyncMock(return_value=[{"id": 11, "similarity": 0.40}])
        store.increment_mistake_occurrence = AsyncMock()
        mid = await _maybe_reinforce_mistake(store, "p", "TypeError", "x is None")
        assert mid is None
        store.increment_mistake_occurrence.assert_not_awaited()

    asyncio.run(_run())


def test_reinforce_success_increments_existing():
    from swarm.brain.learn_store import _maybe_reinforce_success

    async def _run():
        store = AsyncMock()
        store.query_successes = AsyncMock(return_value=[{"id": 5, "similarity": 0.95}])
        store.increment_success_reuse = AsyncMock()
        sid = await _maybe_reinforce_success(store, "p", "排序模式")
        assert sid == 5
        store.increment_success_reuse.assert_awaited_once_with(5)

    asyncio.run(_run())


def test_reinforce_best_effort_swallows_errors():
    """强化检查失败绝不能影响主落库——返回 None 退回插新行。"""
    from swarm.brain.learn_store import _maybe_reinforce_mistake

    async def _run():
        store = AsyncMock()
        store.query_mistakes = AsyncMock(side_effect=RuntimeError("db down"))
        assert await _maybe_reinforce_mistake(store, "p", "E", "d") is None

    asyncio.run(_run())


# ── N-01：阻断模式下扫描器崩溃必须 fail-closed（l1_passed=False）；none 模式不阻断 ──
def _make_audit_subtask():
    from swarm.types import FileScope, SubTask, TaskHarness

    return SubTask(
        id="st-audit",
        description="audit",
        scope=FileScope(writable=["a.py"], readable=[]),
        harness=TaskHarness(language="python"),
    )


def _run_audit_with_scanner_crash(block_severity: str):
    from swarm.brain import nodes

    subtask = _make_audit_subtask()
    fake_cfg = SimpleNamespace(worker=SimpleNamespace(security_block_severity=block_severity))

    def _boom(*a, **k):
        raise RuntimeError("scanner exploded")

    with patch("swarm.config.settings.get_config", return_value=fake_cfg), \
         patch("swarm.worker.security_scan.run_security_scan", side_effect=_boom):
        return asyncio.run(
            nodes._run_security_audit(subtask, "/tmp/proj", project_id="p", task_id="t")
        )


def test_security_audit_fail_closed_on_scanner_crash_blocking():
    out = _run_audit_with_scanner_crash("critical")
    assert out.l1_passed is False, "阻断模式扫描器崩溃必须 fail-closed"
    assert out.l1_details.get("fail_closed") is True


def test_security_audit_report_only_not_blocked_on_crash():
    out = _run_audit_with_scanner_crash("none")
    assert out.l1_passed is True, "report-only(none) 模式扫描器崩溃不阻断"
    assert out.l1_details.get("fail_closed") is False


# ── P1-SQL-01：make_interval 取代无法绑参的 INTERVAL '%s days' ──
def test_behavior_store_uses_make_interval():
    """天数过滤必须走绑参（P1-SQL-01：INTERVAL '%s days' 里 %s 在引号内，psycopg 不绑）。

    ★批25 GS-5w 换锁★（原实现=getsource 断 "make_interval(days => %s)" 存在 +
    "INTERVAL '%s days'" 缺席）。行为锁：mock 异步 conn 真调 get_hotspot_files(days=30)
    与 prune_old_logs(retention_days=180)，断【实际发出的 SQL】里天数是绑定参数、
    绝不以字面量拼进文本。
    删什么变红：回退成 f-string/% 拼接天数 → 天数出现在 SQL 文本/缺席 params → 红；
    回退成 INTERVAL '%s days'（占位符数与参数数错位）→ %s 计数断言红。
    """
    from swarm.knowledge.behavior_store import BehaviorStore

    executed: list[tuple[str, tuple]] = []

    class _Cur:
        rowcount = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, sql, params=None):
            executed.append((sql, tuple(params or ())))

        async def fetchall(self):
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

    store = BehaviorStore.__new__(BehaviorStore)
    store._conn = _Conn()

    asyncio.run(store.get_hotspot_files("p1", top_k=20, days=30))
    assert executed, "夹具自检：必须真发出一次查询"
    sql, params = executed[0]
    assert 30 in params, f"天数必须作为绑定参数下发: {params}"
    # 词边界收窄（批25 R1 reviewer LOW）：裸子串 "30" 会把粘连数字/版本号（v30/300/3.30）
    # 冤红——实际覆盖的是粘连形态；LIMIT 30 这类空格前导字面量仍命中=刻意保守
    # （红=逼人工重估，R2 复核认可），真牙齿是上行 30 in params + 下行 %s 计数
    assert not re.search(r"(?<![\d.])30(?![\d])", sql), "天数绝不允许以字面量拼进 SQL 文本"
    assert sql.count("%s") == len(params), "每个参数都须有对应占位符（错位=绑参破产）"

    # 同簇 sibling：prune_old_logs 两条 DELETE 同样按天绑参
    executed.clear()
    asyncio.run(store.prune_old_logs("p1", retention_days=180))
    assert len(executed) == 2, "夹具自检：prune 必须发出两条 DELETE"
    for sql, params in executed:
        assert 180 in params, f"retention_days 必须作为绑定参数下发: {params}"
        assert not re.search(r"(?<![\d.])180(?![\d])", sql), "retention_days 绝不允许拼进 SQL 文本"
        assert sql.count("%s") == len(params)


if __name__ == "__main__":
    import sys

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ✅ {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  ❌ {name}: {exc}")
    sys.exit(1 if failed else 0)
