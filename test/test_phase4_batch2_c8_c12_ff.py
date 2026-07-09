"""阶段4 批2（登记册 §四）：C8 孤儿线程哨兵 / C9 动态依赖边 / C10 工具裁剪 /
C11 manifest 缓存生命周期 / C12 虚假 git add / F-F worker 面熔断接线。

C8：agent 超时后同步工具线程杀不死（wait_for 只取消 awaitable）——孤儿对已销毁沙箱烧
    请求到自身超时。治=worker deadline 进 contextvar，_run 入口哨兵（预算尽=不发命令）
    +超时钳到剩余预算。
C9：blocked 在【还有 active 生产者】的内部包=合法跨模块等待，旧 transient 退避每轮
    整条 locate/code/verify 白跑撞同一 BLOCKED。治=补动态 depends_on 边，dispatch
    依赖闸扣住到生产者 L1 过（环护栏：生产者可达消费者不补）。
C10：12 工具全集恒挂=小模型复读死循环土壤。治=按 scope/intent 确定性裁剪（只读去写
    工具；git_log/git_blame 只给 debug/audit）。
C11：缓存 key 已含 sandbox_id 但每 run 全清=同沙箱多 run 重付 5-8 趟沙箱 find。
    治=run 入口只清负缓存（True 同沙箱生命周期恒真；False 可能因脚手架落盘过期）。
C12：沙箱无 .git（by design round20#13），git add ||true 静默 no-op 却报"已锁定进度"
    =纯剧场。治=不发假命令，诚实日志（保护本就来自文件系统+内容同步 pull-back）。
F-F：worker 纯靠 with_fallbacks 顺序兜底，primary 反复超时仍每次先撞（每次白付一个
    超时窗）。治=breaker.is_open 只读探询重排链（open 到尾，不删除）+listeners 回灌
    成败证据；brain 面 allow() 探针语义不动。
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

from swarm.types import (
    Confidence,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
    WorkerOutput,
)

# ─────────────── C8：收尾哨兵 + 超时钳 ───────────────


def test_c8_sentinel_blocks_after_deadline():
    from swarm.tools.build_tools import _run, clear_worker_deadline, set_worker_deadline
    set_worker_deadline(time.monotonic() - 1)
    try:
        with patch("swarm.tools.build_tools._run_local") as mock_local, \
             patch("swarm.tools.build_tools._run_in_sandbox") as mock_sbx:
            out = _run("mvn -q compile", timeout=300)
        assert "预算已耗尽" in out, "孤儿线程的后续命令必须被哨兵拦下，不再烧到自身超时"
        mock_local.assert_not_called()
        mock_sbx.assert_not_called()
    finally:
        clear_worker_deadline()


def test_c8_timeout_clamped_to_remaining():
    from swarm.tools.build_tools import _run, clear_worker_deadline, set_worker_deadline
    set_worker_deadline(time.monotonic() + 50)
    try:
        seen = {}

        def fake_local(cmd, cwd=None, timeout=120):
            seen["timeout"] = timeout
            return "ok"

        with patch("swarm.tools.build_tools._run_local", side_effect=fake_local):
            _run("echo hi", timeout=300)
        assert seen["timeout"] <= 51, f"工具超时必须钳到剩余预算: {seen['timeout']}"
    finally:
        clear_worker_deadline()


def test_c8_no_deadline_backward_compatible():
    from swarm.tools.build_tools import _run, clear_worker_deadline
    clear_worker_deadline()
    seen = {}

    def fake_local(cmd, cwd=None, timeout=120):
        seen["timeout"] = timeout
        return "ok"

    with patch("swarm.tools.build_tools._run_local", side_effect=fake_local):
        _run("echo hi", timeout=300)
    assert seen["timeout"] == 300, "无 deadline=老行为一字不变"


# ─────────────── C9：合法跨模块等待 → 动态依赖边 ───────────────

def _wo_blocked(sid, pkgs):
    return WorkerOutput(
        subtask_id=sid, diff="", summary="", l1_passed=False,
        confidence=Confidence.LOW,
        l1_details={
            "pipeline_blocked": "internal_pkg_not_built",
            "blocked_on_packages": pkgs,
            "failure_class": "transient",
        })


def test_c9_adds_dynamic_dep_edge_for_active_producer(monkeypatch):
    import swarm.brain.nodes as nodes
    plan = TaskPlan(subtasks=[
        SubTask(id="st-p", description="生产 dto", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=["backend/src/com/acme/dto/UserDto.java"],
                                readable=[]), depends_on=[]),
        SubTask(id="st-c", description="消费 dto", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=["web/src/com/acme/web/UserCtl.java"],
                                readable=[]), depends_on=[]),
    ], parallel_groups=[["st-p", "st-c"]])
    state = {
        "complexity": "complex",
        "plan": plan,
        "failed_subtask_ids": ["st-c"],
        "subtask_results": {"st-c": _wo_blocked("st-c", ["com.acme.dto"])},
        "dispatch_remaining": ["st-p"],  # 生产者 active pending
        "subtask_retry_counts": {},
    }
    with patch.object(nodes, "_get_brain_llm",
                      side_effect=RuntimeError("no llm")), \
         patch.object(asyncio, "sleep", return_value=None):
        out = asyncio.run(nodes.handle_failure(state))
    st_c = next(s for s in plan.subtasks if s.id == "st-c")
    assert "st-p" in (st_c.depends_on or []), (
        "还有 active 生产者的合法跨模块等待必须补动态依赖边——"
        "旧 transient 退避每轮整条 locate/code/verify 白跑才撞同一 BLOCKED")
    assert out.get("plan") is plan, "补边后必须回写 plan（dispatch 依赖闸消费）"


def test_c9_no_edge_when_would_cycle(monkeypatch):
    import swarm.brain.nodes as nodes
    plan = TaskPlan(subtasks=[
        SubTask(id="st-p", description="生产", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=["backend/src/com/acme/dto/UserDto.java"],
                                readable=[]), depends_on=["st-c"]),  # 生产者依赖消费者
        SubTask(id="st-c", description="消费", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=["web/src/com/acme/web/UserCtl.java"],
                                readable=[]), depends_on=[]),
    ], parallel_groups=[["st-c", "st-p"]])
    state = {
        "complexity": "complex",
        "plan": plan,
        "failed_subtask_ids": ["st-c"],
        "subtask_results": {"st-c": _wo_blocked("st-c", ["com.acme.dto"])},
        "dispatch_remaining": ["st-p"],
        "subtask_retry_counts": {},
    }
    with patch.object(nodes, "_get_brain_llm",
                      side_effect=RuntimeError("no llm")), \
         patch.object(asyncio, "sleep", return_value=None):
        asyncio.run(nodes.handle_failure(state))
    st_c = next(s for s in plan.subtasks if s.id == "st-c")
    assert "st-p" not in (st_c.depends_on or []), "会成环的边绝不补（依赖环=派发死锁）"


# ─────────────── C10：工具面按 scope/intent 裁剪 ───────────────

def _names(tools):
    return {t.name for t in tools}


def test_c10_default_full_set_backward_compatible():
    from swarm.worker.agent import _get_worker_tools
    assert len(_get_worker_tools()) == 12, "不传参=旧全集（legacy 零回归）"


def test_c10_typical_coding_task_drops_archaeology_tools():
    from swarm.worker.agent import _get_worker_tools
    tools = _get_worker_tools(FileScope(writable=["a.py"], readable=[]), "modify")
    n = _names(tools)
    assert "git_log" not in n and "git_blame" not in n, (
        "历史考古工具只给 debug/audit——普通编码子任务 12 个全集是复读死循环土壤")
    assert "patch_file" in n and "write_file" in n
    assert len(tools) <= 10


def test_c10_debug_keeps_archaeology_readonly_drops_writes():
    from swarm.worker.agent import _get_worker_tools
    dbg = _names(_get_worker_tools(FileScope(writable=["a.py"], readable=[]), "debug"))
    assert "git_blame" in dbg, "debug 意图保留考古工具"
    ro = _names(_get_worker_tools(FileScope(writable=[], readable=["a.py"]), "audit"))
    assert "write_file" not in ro and "patch_file" not in ro, (
        "只读 scope 给写工具=诱导越权+噪声")


# ─────────────── C11：manifest 缓存只清负项 ───────────────

def test_c11_prune_keeps_positive_entries():
    from swarm.worker import l1_pipeline as lp
    lp._MANIFEST_PRESENT_CACHE.clear()
    lp._MANIFEST_PRESENT_CACHE[(0, "sb-1", ("pom.xml",))] = True
    lp._MANIFEST_PRESENT_CACHE[(0, "sb-1", ("go.mod",))] = False
    lp._prune_manifest_cache_negatives()
    assert lp._MANIFEST_PRESENT_CACHE == {(0, "sb-1", ("pom.xml",)): True}, (
        "True 同沙箱生命周期恒真（跨 run 复用省沙箱 find）；False 可能过期必须逐 run 重探")
    lp._MANIFEST_PRESENT_CACHE.clear()


# ─────────────── C12：不再发虚假 git add ───────────────

def test_c12_checkpoint_sends_no_fake_git_add():
    from swarm.worker.executor import WorkerExecutor
    st = SubTask(id="st-c12", description="x", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["a.py"], readable=[]))
    ex = WorkerExecutor(subtask=st, project_path="/tmp/swarm-c12")
    ex._sandbox = MagicMock()
    logs: list[str] = []
    with patch.object(ex, "_log", side_effect=logs.append):
        asyncio.run(ex._sandbox_checkpoint(["a.py"]))
    ex._sandbox.commands.run.assert_not_called(), (
        "沙箱无 .git（by design）——git add ||true 是静默 no-op 纯剧场")
    assert logs and "git add" not in logs[0], f"日志必须诚实（不再声称 git 锁定）: {logs}"


# ─────────────── F-F：worker 链健康重排 + 只读探询 ───────────────

class _FakeRunnable:
    def __init__(self, name):
        self.name = name
        self.fallbacks = None

    def with_listeners(self, **kw):
        return self

    def with_fallbacks(self, fbs):
        self.fallbacks = list(fbs)
        return self


def test_ff_is_open_readonly_no_probe_reservation():
    from swarm.models import breaker
    breaker._reset_for_tests()
    for _ in range(breaker._threshold()):
        breaker.record_failure("m-sick")
    assert breaker.is_open("m-sick") is True
    # 只读探询绝不预约探针：allow() 的 half-open 语义不受影响（冷却内仍 False）
    assert breaker.is_open("m-sick") is True
    assert breaker.is_open("m-healthy") is False
    breaker._reset_for_tests()


def test_ff_chain_reorders_open_primary_to_tail():
    from swarm.models import breaker
    from swarm.models.router import ModelRouter
    breaker._reset_for_tests()
    for _ in range(breaker._threshold()):
        breaker.record_failure("m-primary")
    r1, r2 = _FakeRunnable("m-primary"), _FakeRunnable("m-fb")
    chain = ModelRouter._assemble_worker_chain([("m-primary", r1), ("m-fb", r2)])
    assert chain is r2, (
        "primary 熔断中必须让健康 fallback 先上——旧行为每次白付一个超时窗才降级")
    assert chain.fallbacks == [r1], "open 模型移到链尾但不删除（全 open 仍可按序尝试）"
    breaker._reset_for_tests()


def test_ff_chain_keeps_order_when_all_healthy():
    from swarm.models import breaker
    from swarm.models.router import ModelRouter
    breaker._reset_for_tests()
    r1, r2 = _FakeRunnable("m-a"), _FakeRunnable("m-b")
    chain = ModelRouter._assemble_worker_chain([("m-a", r1), ("m-b", r2)])
    assert chain is r1 and chain.fallbacks == [r2], "全健康=原序（零行为漂移）"
