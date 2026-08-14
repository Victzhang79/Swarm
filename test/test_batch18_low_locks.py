#!/usr/bin/env python3
"""30 号文批18 LOW 合批锁：G-4 工具层接线负路径 / G-5 G1 双弱观测面 / G-6 已知值映射派生
/ E-2 resume 冷启动留痕 / E-3 统计 degraded 键 / E-4 未知 dispatch mode 告警。

- G-4：scope_guard 判据层有牙（_path_scope_match 正反断言）而工具层接线负路径零覆盖——
  `file_tools.py` 的 `err = require_writable(path)` 一行被删，越界写直接落盘，全仓零测试
  断言 ⛔。L1 scope_violations 是事后闸（污染已发生）。
- G-5：G1 两弱观测面零覆盖——③e 全图汇聚点 warn（plan_validator）与泄压阀
  `module_coherence:skipped(disabled)` degraded 标记（validate_plan 节点）。机制活着
  （探针实证）但删了没有任何测试会红。
- G-6：security_scan 已知值档位映射——semgrep/gosec 无锁；治法=mapper 清单从模块派生
  （新增 mapper 未配已知值用例即红），已知值表按名索引。
"""
from __future__ import annotations

import pytest

from swarm.types import (
    Complexity,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
)


# ── G-4：工具层接线负路径 ─────────────────────────────────

@pytest.fixture
def _narrow_scope():
    from swarm.tools.scope_guard import clear_scope, set_scope
    set_scope(FileScope(writable=["ok.py"], readable=["ok.py"]))
    yield
    clear_scope()


def test_write_file_rejects_out_of_scope(_narrow_scope):
    """write_file 越界写必须 ⛔ 拒绝。牙齿：删 `err = require_writable(path)` 一行即红。"""
    from swarm.tools.file_tools import write_file
    out = write_file.invoke({"path": "evil_out_of_scope.py", "content": "x = 1"})
    assert out.startswith("⛔"), f"越界写必须 ⛔ 拒绝，实际: {out[:100]}"


def test_patch_file_rejects_out_of_scope(_narrow_scope):
    """patch_file 同闸（file_tools.py:455 同型行）。"""
    from swarm.tools.file_tools import patch_file
    out = patch_file.invoke({"path": "evil_out_of_scope.py", "old_string": "a",
                             "new_string": "b"})
    assert out.startswith("⛔"), f"越界 patch 必须 ⛔ 拒绝，实际: {out[:100]}"


# ── G-5a：③e 全图汇聚点 warn（R67-12 观测面）──────────────

def _aux_convergence_plan(n=10):
    """10 子任务中 st-aux 是纯辅助产物（.sql 非类路径源码）却依赖 6 个（>半数）。"""
    subs = [
        SubTask(id=f"st-{i}", description=f"d{i}", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=[], readable=[],
                                create_files=[f"src/main/java/M{i}.java"]),
                depends_on=[])
        for i in range(9)
    ]
    subs.append(SubTask(
        id="st-aux", description="ddl", difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=[], readable=[], create_files=["docs/schema.sql"]),
        depends_on=[f"st-{i}" for i in range(6)]))
    return TaskPlan(subtasks=subs, parallel_groups=[[s.id for s in subs]])


def test_g1_aux_convergence_point_warns():
    """③e：纯辅助文件子任务依赖过半 = 全图汇聚点，必须 warn（round67 st-14 连坐实证）。
    删掉 :1585-1596 的 warn 块本锁红。"""
    from swarm.brain.plan_validator import validate_module_coherence
    r = validate_module_coherence(_aux_convergence_plan())
    assert any("汇聚点" in w and "st-aux" in w for w in r.warnings), \
        f"汇聚点观测面静默: {r.warnings}"


def test_g1_aux_leaf_no_false_warning():
    """反向：辅助文件子任务零依赖（独立叶）不误报。"""
    from swarm.brain.plan_validator import validate_module_coherence
    plan = _aux_convergence_plan()
    plan.subtasks[-1].depends_on = []
    r = validate_module_coherence(plan)
    assert not any("汇聚点" in w for w in r.warnings)


# ── G-5b：泄压阀 degraded 标记（绝不静默关闸）─────────────

class _FakeLLM:
    async def ainvoke(self, messages):
        class _R:
            content = '{"valid": true, "issues": []}'
        return _R()


@pytest.mark.asyncio
async def test_coherence_gate_off_leaves_degraded_marker(monkeypatch):
    """SWARM_MODULE_COHERENCE_GATE=0 泄压时必须在 plan_validation_warnings 留
    `module_coherence:skipped(disabled)`（round62 级复发与「闸跑了没抓到」日志上不可分）。
    删掉 :3789 的 append 本锁红。"""
    for k in ("SWARM_VALIDATE_PLAN_LLM_GATE", "SWARM_VALIDATE_PLAN_COMPLETENESS_GATE",
              "SWARM_PLAN_COVERAGE_GATE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SWARM_MODULE_COHERENCE_GATE", "0")
    import swarm.brain.nodes as nodes
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda *a, **k: _FakeLLM())
    st = SubTask(id="st-1", description="d", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["a.py"], readable=[]), covers=["req-1"])
    out = await nodes.validate_plan({
        "plan": TaskPlan(subtasks=[st], parallel_groups=[["st-1"]]),
        "task_description": "t", "complexity": Complexity.MEDIUM,
        "plan_retry_count": 0,
        "requirement_items": [{"id": "req-1", "text": "功能一", "kind": "functional",
                               "source_quote": "功能一", "source": "description"}],
    })
    assert "module_coherence:skipped(disabled)" in (out.get("plan_validation_warnings") or []), \
        f"泄压阀 degraded 标记缺失: {out.get('plan_validation_warnings')}"


# ── G-6：已知值档位映射派生遍历 ───────────────────────────

def test_all_severity_mappers_have_known_value_cases():
    """mapper 清单从模块派生——新增第八个 `_map_*_severity` 而未配已知值用例即红
    （手抄清单的漏项病：GS-1/G-6 同族）。"""
    import swarm.worker.security_scan as ss
    mappers = {name: fn for name, fn in vars(ss).items()
               if name.startswith("_map_") and name.endswith("_severity") and callable(fn)}
    known_cases = {
        "_map_bandit_severity": ("HIGH", ss.Severity.HIGH),
        "_map_semgrep_severity": ("ERROR", ss.Severity.HIGH),
        "_map_gosec_severity": ("LOW", ss.Severity.LOW),
        "_map_spotbugs_severity": ("1", ss.Severity.HIGH),
        "_map_pip_audit_severity": ("critical", ss.Severity.CRITICAL),
        "_map_npm_severity": ("info", ss.Severity.INFO),
        "_map_vuln_severity": ("low", ss.Severity.LOW),
    }
    assert set(mappers) == set(known_cases), \
        f"mapper 集合与已知值用例表漂移: 模块有 {sorted(mappers)}，表有 {sorted(known_cases)}"
    for name, (raw, want) in known_cases.items():
        assert mappers[name](raw) is want, f"{name}({raw!r}) 已知值档位漂移"


# ── E-2：resume 冷启动留痕（degrade 账 + WARNING）────────────

def test_resume_cold_start_leaves_degrade_marker(monkeypatch, caplog):
    """E-2：resume 一律 per-task 云端 token 闸冷启动（段末清零、段内从 0 起算）——
    必须 record_degrade('usage_tracker.task_total_cold_start') + WARNING，否则
    「闸已复位」与「从未超限」不可分。删掉 runner.py 的 E-2 块本锁红。
    哨兵异常把 resume_task 截停在 E-2 块紧后（_set_workspace），不进真执行。"""
    import asyncio
    import logging

    from swarm.brain import runner
    from swarm.infra import degrade

    seen: list[str] = []
    monkeypatch.setattr(degrade, "record_degrade", lambda c: seen.append(c))
    monkeypatch.setattr(runner.store, "get_task",
                        lambda tid: {"id": tid, "project_id": "p-e2"})

    class _Sentinel(Exception):
        pass

    def _boom(pid):
        raise _Sentinel(pid)

    monkeypatch.setattr(runner, "_set_workspace", _boom)
    tid = "t-e2-cold-start-lock"
    try:
        with caplog.at_level(logging.WARNING):
            with pytest.raises(_Sentinel):
                asyncio.run(runner.resume_task(tid, "approve"))
    finally:
        runner._task_running.discard(tid)
        runner._task_queues.pop(tid, None)
    assert "usage_tracker.task_total_cold_start" in seen, \
        f"resume 冷启动未记 degrade 账: {seen}"
    assert any("冷启动" in r.message for r in caplog.records), \
        "resume 冷启动缺 WARNING（零机读信号族）"


def test_resume_planning_cold_start_leaves_degrade_marker(monkeypatch, caplog):
    """批18 hunter R1-M1：resume_planning 是第二条 resume 路径（段末 finally 同样
    clear_task_total ⇒ 冷启动），必须与 resume_task 同源留痕（抽
    `_record_task_total_cold_start` 双路径共接，防再次分叉）。删掉 runner.py
    resume_planning 里的 helper 调用行本锁红。哨兵异常同法截停在 _set_workspace。"""
    import asyncio
    import logging

    from swarm.brain import runner
    from swarm.infra import degrade

    seen: list[str] = []
    monkeypatch.setattr(degrade, "record_degrade", lambda c: seen.append(c))
    monkeypatch.setattr(runner.store, "get_task",
                        lambda tid: {"id": tid, "project_id": "p-e2b"})

    class _Sentinel(Exception):
        pass

    def _boom(pid):
        raise _Sentinel(pid)

    monkeypatch.setattr(runner, "_set_workspace", _boom)
    tid = "t-e2b-cold-start-lock"
    try:
        with caplog.at_level(logging.WARNING):
            with pytest.raises(_Sentinel):
                asyncio.run(runner.resume_planning(tid, {"action": "skip"}))
    finally:
        runner._task_running.discard(tid)
        runner._task_queues.pop(tid, None)
    assert "usage_tracker.task_total_cold_start" in seen, \
        f"resume_planning 冷启动未记 degrade 账（半落地回潮）: {seen}"
    assert any("冷启动" in r.message for r in caplog.records), \
        "resume_planning 冷启动缺 WARNING"


# ── E-3：token 统计 degraded 机读键 ─────────────────────────

def test_usage_stats_degraded_on_ensure_failure(monkeypatch):
    """E-3：建表失败必须 degraded=True（与「真零用量」机读可辨），且结构完整不崩。"""
    from swarm.models import usage_tracker as ut
    monkeypatch.setattr(ut, "_ensure_table", lambda: False)
    out = ut.get_token_usage_stats()
    assert out["degraded"] is True
    assert out["grand_total"]["total_tokens"] == 0  # 全零桶仍在，WebUI 不崩


def test_usage_stats_degraded_on_read_error(monkeypatch):
    """E-3：读取异常路径同样 degraded=True（except 分支置位）。"""
    from swarm.models import usage_tracker as ut
    monkeypatch.setattr(ut, "_ensure_table", lambda: True)

    def _boom():
        raise RuntimeError("pg down")

    monkeypatch.setattr(ut, "_pool", _boom)
    out = ut.get_token_usage_stats()
    assert out["degraded"] is True


# ── E-4：未知 dispatch mode 告警一次 ────────────────────────

def test_unknown_dispatch_mode_warns_and_falls_back(monkeypatch, caplog):
    """E-4：SWARM_WORKER_DISPATCH_MODE typo 静默回退零告警 = 枚举解析失败静默 ACCEPT
    同族（queue 真落地时 typo=白做）。删掉 else 内 WARNING 块本锁红。"""
    import logging

    from swarm.infra import worker_dispatcher as wd
    wd.reset_worker_dispatcher()
    monkeypatch.setenv("SWARM_WORKER_DISPATCH_MODE", "queues")  # typo
    try:
        with caplog.at_level(logging.WARNING):
            d = wd.get_worker_dispatcher()
        assert isinstance(d, wd.InProcessDispatcher)
        assert any("未知取值" in r.message and "queues" in r.message
                   for r in caplog.records), \
            f"未知 mode 未告警: {[r.message for r in caplog.records]}"
    finally:
        wd.reset_worker_dispatcher()


def test_known_dispatch_mode_no_typo_warning(monkeypatch, caplog):
    """反向钉：合法取值 inprocess 绝不误报 typo 告警（防过宽告警把正常面淹了）。"""
    import logging

    from swarm.infra import worker_dispatcher as wd
    wd.reset_worker_dispatcher()
    monkeypatch.setenv("SWARM_WORKER_DISPATCH_MODE", "inprocess")
    try:
        with caplog.at_level(logging.WARNING):
            wd.get_worker_dispatcher()
        assert not any("未知取值" in r.message for r in caplog.records)
    finally:
        wd.reset_worker_dispatcher()


# ── 批17 LEAD：usage_tracker inline 迁移 → v8 ──────────────

def test_v8_migration_gated_and_inline_migrate_removed():
    """批17 LEAD 收口：usage_tracker 的 inline `_MIGRATE`（ADD COLUMN IF NOT EXISTS）
    必须消失（P0-C：改列型绝不 inline 进 ensure_table），补列职责迁 v8 迁移。
    锁三面：①inline 块不复活；②v8 登记在册；③to_regclass 门控（表不存在 no-op）。"""
    from swarm.models import usage_tracker as ut
    assert not hasattr(ut, "_MIGRATE"), \
        "inline ADD COLUMN 回潮——改列必须走 infra/migrations/runner.py 登记册（P0-C）"
    from swarm.infra.migrations import runner as mig
    names = [n for _, n, _ in mig._MIGRATIONS]
    assert "usage_total_duration_ms" in names, f"v8 未登记: {names}"

    class _Cur:
        def __init__(self, exists):
            self.exists = exists
            self.ddl: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, *p):
            if "to_regclass" not in sql:
                self.ddl.append(sql)

        def fetchone(self):
            return ("llm_token_usage",) if self.exists else None

    class _Conn:
        def __init__(self, exists):
            self.cur = _Cur(exists)

        def cursor(self):
            return self.cur

    c_absent = _Conn(False)
    mig._migration_v8_usage_total_duration_ms(c_absent)
    assert c_absent.cur.ddl == [], "表不存在时必须 no-op（同 v7 to_regclass 门控）"
    c_present = _Conn(True)
    mig._migration_v8_usage_total_duration_ms(c_present)
    assert any("total_duration_ms" in s for s in c_present.cur.ddl), \
        f"表存在时必须补列: {c_present.cur.ddl}"
