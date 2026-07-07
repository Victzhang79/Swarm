"""S1-4（task#18）：VERIFY_RUNTIME 节点接线 — 行为测试（禁 getsource 结构焊死）。

覆盖面：
1. 节点级三态（stub manager/沙箱/推导）：开关关/推导不全/转交复用/回退自建/
   rebuild 失败→skipped 非 failed/passed/failed/skipped 写键正确/finally 必杀。
2. graph 路由级：after_verify_runtime 三态；after_verify_l2 通过态标签目标=verify_runtime；
   verify_runtime 无「静态边+条件边」双挂（confirm fan-out 血案同款拓扑断言）。
3. failure 分支（S1-6 已替换占位）：verification_failure="runtime_smoke" 走有界阶梯
   （replan_count 共用熔断→escalate），不落 "l2" 分支；完整归因面见 s1_6 测试文件。
4. L2 延活转交：编译成功+开关开→不 kill+返回 sid；编译失败→照旧 kill；续期失败→kill 不转交。
"""

from __future__ import annotations

import asyncio

import pytest

import swarm.brain.nodes.verify as verify_mod
from swarm.brain.graph import after_verify_runtime, build_brain_graph
from swarm.brain.nodes.runtime_smoke import RuntimeSmokeResult
from swarm.brain.smoke_derive import SmokeDerivation


# ───────────────────────── stub 设施 ─────────────────────────

class _StubResult:
    def __init__(self, stdout: str = "", stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr


class _StubSandbox:
    def __init__(self, sid: str):
        self.sandbox_id = sid


class _StubManager:
    """最小沙箱经理 stub：记账 create/kill/extend，run_command 输出可注入。"""

    def __init__(self, *, instances=None, extend_ok=True, remaining=None,
                 rebuild_stdout="__RC__0"):
        self._instances = dict(instances or {})
        self.extend_ok = extend_ok
        self.remaining = remaining
        self.rebuild_stdout = rebuild_stdout
        self.killed: list[str] = []
        self.created: list[str] = []
        self.extend_calls: list[tuple[str, int]] = []
        self.synced = False

    def try_extend_lifetime(self, sandbox, seconds):
        self.extend_calls.append((getattr(sandbox, "sandbox_id", "?"), int(seconds)))
        return self.extend_ok

    def remaining_lifetime(self, sandbox_id):
        return self.remaining

    def create(self, project_id=None, source=""):
        self.created.append(source)
        sb = _StubSandbox("sb-selfbuilt")
        self._instances[sb.sandbox_id] = sb
        return sb

    def sync_project_to_sandbox(self, sandbox, path, workdir):
        self.synced = True

    def run_command(self, sandbox, command, timeout=120, **kwargs):
        return _StubResult(stdout=self.rebuild_stdout)

    def kill(self, sandbox_id):
        self.killed.append(sandbox_id)
        self._instances.pop(sandbox_id, None)


_FULL_DERIVATION = SmokeDerivation(
    start_cmd="run-the-app", port=8080, health_path="/health",
    migration_kind=None, evidence={"start_cmd": "manifest 证据", "port": "配置证据"},
)


def _smoke_result(status: str, classification: str, *, log_tail: str = "") -> RuntimeSmokeResult:
    return RuntimeSmokeResult(status, classification, f"stub-{status}", log_tail=log_tail,
                              details={"probe_sequence": []})


@pytest.fixture()
def wired(monkeypatch):
    """通用接线：开关开、项目路径可得、推导完整、沙箱面全 stub。返回可变夹具。"""
    ctx = {
        "manager": _StubManager(),
        "derivation": _FULL_DERIVATION,
        "smoke": _smoke_result("passed", "started"),
        "smoke_calls": [],
    }
    monkeypatch.delenv("SWARM_RUNTIME_SMOKE_ENABLED", raising=False)

    import swarm.brain.nodes as nodes_pkg
    import swarm.brain.smoke_derive as sd
    import swarm.worker.sandbox as ws
    import swarm.brain.nodes.runtime_smoke as rs
    import swarm.brain.integration_review as ir

    monkeypatch.setattr(nodes_pkg, "_get_project_path", lambda pid: "/tmp/fake-project")
    monkeypatch.setattr(nodes_pkg, "_sandbox_available", lambda: True)
    monkeypatch.setattr(sd, "derive_runtime_smoke", lambda stack, path: ctx["derivation"])
    monkeypatch.setattr(ws, "get_sandbox_manager", lambda: ctx["manager"])
    monkeypatch.setattr(ir, "_detect_build_cmd_generic", lambda p: "stub-build")

    async def _fake_run_smoke(manager, sandbox, script, **kwargs):
        ctx["smoke_calls"].append({"sandbox": sandbox, "kwargs": kwargs})
        return ctx["smoke"]

    monkeypatch.setattr(rs, "run_runtime_smoke", _fake_run_smoke)
    return ctx


def _run_node(state: dict) -> dict:
    return asyncio.run(verify_mod.verify_runtime(state))


# ───────────────────────── 节点级：开关 / 推导 ─────────────────────────

def test_disabled_switch_skips_with_degraded(wired, monkeypatch):
    monkeypatch.setenv("SWARM_RUNTIME_SMOKE_ENABLED", "0")
    out = _run_node({"project_id": "p1"})
    assert out["runtime_smoke_passed"] is None
    assert out["runtime_smoke_skipped"] is True
    assert out["degraded_reasons"] == ["runtime_smoke_skipped:runtime_smoke_disabled"]
    assert out["runtime_smoke_message"]  # 如实说明，绝不静默


def test_disabled_switch_still_disposes_handoff_sandbox(wired, monkeypatch):
    monkeypatch.setenv("SWARM_RUNTIME_SMOKE_ENABLED", "false")
    _run_node({"project_id": "p1", "runtime_smoke_sandbox_id": "sb-l2"})
    assert "sb-l2" in wired["manager"].killed  # 早退路径也不留泄漏


def test_incomplete_derivation_skips_with_missing_and_evidence(wired):
    wired["derivation"] = SmokeDerivation(
        start_cmd=None, port=8080, evidence={"port": "配置证据"})
    out = _run_node({"project_id": "p1"})
    assert out["runtime_smoke_passed"] is None and out["runtime_smoke_skipped"] is True
    assert out["degraded_reasons"] == ["runtime_smoke_skipped:derivation_incomplete"]
    assert out["runtime_smoke_details"]["missing"] == ["start_cmd"]
    assert out["runtime_smoke_details"]["evidence"] == {"port": "配置证据"}
    assert not wired["smoke_calls"]  # 不猜：缺证据绝不起探针


def test_incomplete_derivation_missing_port_only(wired):
    wired["derivation"] = SmokeDerivation(start_cmd="x", port=None)
    out = _run_node({"project_id": "p1"})
    assert out["runtime_smoke_details"]["missing"] == ["port"]


# ───────────────────────── 节点级：沙箱获取 ─────────────────────────

def test_live_handoff_reused_no_self_build(wired):
    sb = _StubSandbox("sb-l2")
    wired["manager"] = _StubManager(instances={"sb-l2": sb})
    out = _run_node({"project_id": "p1", "runtime_smoke_sandbox_id": "sb-l2"})
    assert out["runtime_smoke_passed"] is True
    assert wired["manager"].created == []  # 复用转交，不自建
    assert wired["smoke_calls"][0]["sandbox"] is sb
    assert out["runtime_smoke_details"]["sandbox"]["source"] == "handoff"


def test_dead_handoff_falls_back_to_self_build(wired):
    wired["manager"] = _StubManager(instances={})  # 转交 sid 已不在进程内 registry
    out = _run_node({"project_id": "p1", "runtime_smoke_sandbox_id": "sb-gone"})
    assert out["runtime_smoke_passed"] is True
    assert wired["manager"].created == ["verify_runtime"]
    assert wired["manager"].synced is True  # 自建必须重新 sync 工作树
    assert out["runtime_smoke_details"]["sandbox"]["source"] == "self_built"


def test_handoff_extend_fail_and_lifetime_short_falls_back(wired):
    sb = _StubSandbox("sb-l2")
    wired["manager"] = _StubManager(instances={"sb-l2": sb}, extend_ok=False, remaining=5)
    out = _run_node({"project_id": "p1", "runtime_smoke_sandbox_id": "sb-l2"})
    assert wired["manager"].created == ["verify_runtime"]  # 寿命不足→转交不成立→自建
    assert out["runtime_smoke_details"]["sandbox"]["source"] == "self_built"


def test_rebuild_failure_is_skipped_not_failed(wired):
    wired["manager"] = _StubManager(rebuild_stdout="__RC__1 boom")
    out = _run_node({"project_id": "p1"})
    # L2 已证编译通过 → 冒烟侧重建失败是环境问题：skipped，绝不判代码 failed
    assert out["runtime_smoke_passed"] is None
    assert out["runtime_smoke_skipped"] is True
    assert "verification_failure" not in out
    assert out["degraded_reasons"] == ["runtime_smoke_skipped:rebuild_failed"]
    assert "sb-selfbuilt" in wired["manager"].killed  # 失败的自建箱即时处置
    assert not wired["smoke_calls"]


# ───────────────────────── F1：prepare 预算入沙箱寿命/执行器 ─────────────────────────

def _budget_base() -> int:
    from swarm.brain.nodes.runtime_smoke import (
        RUN_TIMEOUT_BUFFER_SEC,
        resolve_smoke_timeout_sec,
    )
    return resolve_smoke_timeout_sec() + RUN_TIMEOUT_BUFFER_SEC + 120


def test_budget_includes_prepare_when_derived(wired):
    """derivation 带 prepare_cmd → 转交沙箱续期预算加 prepare 预算（增量 package 场景 stub）。"""
    from swarm.brain.nodes.runtime_smoke import resolve_prepare_timeout_sec
    sb = _StubSandbox("sb-l2")
    wired["manager"] = _StubManager(instances={"sb-l2": sb})
    wired["derivation"] = SmokeDerivation(
        start_cmd="java -jar target/*.jar", prepare_cmd="mvn -q -DskipTests package",
        port=8080)
    _run_node({"project_id": "p1", "runtime_smoke_sandbox_id": "sb-l2"})
    assert wired["manager"].extend_calls[0][1] == _budget_base() + resolve_prepare_timeout_sec()


def test_budget_without_prepare_unchanged(wired):
    sb = _StubSandbox("sb-l2")
    wired["manager"] = _StubManager(instances={"sb-l2": sb})
    _run_node({"project_id": "p1", "runtime_smoke_sandbox_id": "sb-l2"})
    assert wired["manager"].extend_calls[0][1] == _budget_base()


def test_prepare_and_evidence_passed_through_to_executor(wired):
    """prepare_cmd 进脚本、prepare 预算/项目符号索引/探测端口透传执行器。"""
    from swarm.brain.nodes.runtime_smoke import resolve_prepare_timeout_sec
    wired["derivation"] = SmokeDerivation(
        start_cmd="java -jar target/*.jar", prepare_cmd="mvn -q -DskipTests package",
        port=8080)
    out = _run_node({"project_id": "p1"})
    assert out["runtime_smoke_passed"] is True
    kwargs = wired["smoke_calls"][0]["kwargs"]
    assert kwargs["prepare_timeout_sec"] == resolve_prepare_timeout_sec()
    assert kwargs["probe_port"] == 8080
    assert "project_symbols" in kwargs  # F2 证据面（建不出=None 也必须显式传）


# ───────────────────────── 节点级：三态写键 + finally 必杀 ─────────────────────────

def test_passed_writes_state_and_kills_sandbox(wired):
    wired["smoke"] = _smoke_result("passed", "started")
    out = _run_node({"project_id": "p1"})
    assert out["runtime_smoke_passed"] is True
    assert out["runtime_smoke_skipped"] is False
    assert "verification_failure" not in out
    assert out["runtime_smoke_sandbox_id"] == ""  # 消费后清空防跨轮粘滞
    assert "sb-selfbuilt" in wired["manager"].killed  # finally 必杀（成功也杀）


def test_failed_writes_specialized_failure_with_log_tail(wired):
    wired["smoke"] = _smoke_result("failed", "code_error", log_tail="TRACE-LINE")
    out = _run_node({"project_id": "p1"})
    assert out["runtime_smoke_passed"] is False
    assert out["runtime_smoke_skipped"] is False
    assert out["verification_failure"] == "runtime_smoke"  # 专类归因，绝不冒充 l2
    assert out["runtime_smoke_details"]["classification"] == "code_error"
    assert out["runtime_smoke_details"]["log_tail"] == "TRACE-LINE"  # 供 task#20 回灌
    assert "sb-selfbuilt" in wired["manager"].killed  # finally 必杀（失败也杀）


def test_probe_skipped_maps_to_degraded_with_classification(wired):
    wired["smoke"] = _smoke_result("skipped", "env_missing")
    out = _run_node({"project_id": "p1"})
    assert out["runtime_smoke_passed"] is None
    assert out["runtime_smoke_skipped"] is True
    assert out["degraded_reasons"] == ["runtime_smoke_skipped:env_missing"]
    assert "sb-selfbuilt" in wired["manager"].killed


def test_smoke_exception_is_skipped_and_sandbox_killed(wired, monkeypatch):
    import swarm.brain.nodes.runtime_smoke as rs

    async def _boom(*a, **k):
        raise RuntimeError("infra down")

    monkeypatch.setattr(rs, "run_runtime_smoke", _boom)
    out = _run_node({"project_id": "p1"})
    assert out["runtime_smoke_passed"] is None  # infra 异常≠冒烟失败
    assert out["degraded_reasons"] == ["runtime_smoke_skipped:node_exception"]
    assert "sb-selfbuilt" in wired["manager"].killed  # 异常路径 finally 仍杀


# ───────────────────────── graph 路由级 ─────────────────────────

def test_after_verify_runtime_three_states():
    assert after_verify_runtime({"runtime_smoke_passed": False}) == "handle_failure"
    assert after_verify_runtime({"runtime_smoke_passed": True}) == "verify_l3"
    assert after_verify_runtime(
        {"runtime_smoke_passed": None, "runtime_smoke_skipped": True}) == "verify_l3"
    assert after_verify_runtime({}) == "verify_l3"  # 旧 checkpoint 无键=按跳过放行，不误杀


def test_after_verify_l2_pass_targets_verify_runtime():
    graph = build_brain_graph()
    label_targets: dict[str, str] = {}
    for spec in graph.branches["verify_l2"].values():
        label_targets.update(spec.ends or {})
    assert label_targets["verify_l3"] == "verify_runtime", \
        "L2 通过态（标签 verify_l3）必须先进 verify_runtime 冒烟闸门"
    assert label_targets["handle_failure"] == "handle_failure"


def test_verify_runtime_no_static_edge_fanout():
    """confirm fan-out 血案同款拓扑断言：verify_runtime 出口只由条件边决定。"""
    graph = build_brain_graph()
    assert "verify_runtime" in graph.nodes
    static_targets = {dst for (src, dst) in graph.edges if src == "verify_runtime"}
    assert static_targets == set(), (
        f"verify_runtime 出现无条件静态边 {static_targets}：与条件边并存会 fan-out 并行触发，"
        f"failed 也会被静态边拽进下游（task 37460a5b 同根因）"
    )
    assert "verify_runtime" in graph.branches, "verify_runtime 必须挂 after_verify_runtime 条件边"
    ends = set()
    for spec in graph.branches["verify_runtime"].values():
        ends.update((spec.ends or {}).values())
    assert ends == {"verify_l3", "handle_failure"}, f"条件边出口不符: {ends}"


# ───────────────────────── failure 占位分支 ─────────────────────────

def test_failure_runtime_smoke_bounded_ladder_never_l2_branch():
    """S1-6 替换占位后的语义锁定（保持原占位测试意图：有界 + 绝不落 "l2" 分支）：
    归因不出（无证据）→ replan 阶梯（与 L2 共用 replan_count 熔断，绝不无界）；
    熔断耗尽 → escalate→deliver 人工终点。完整归因面见 test_runtime_failure_feedback_s1_6.py。"""
    from swarm.brain.nodes import handle_failure
    from swarm.types import FileScope, SubTask, TaskPlan

    plan = TaskPlan(subtasks=[SubTask(id="st-1", description="x",
                                      scope=FileScope(create_files=["a/A.java"]))])
    state = {
        "verification_failure": "runtime_smoke",
        "failed_subtask_ids": ["st-1"],
        "subtask_results": {},
        "plan": plan,
        "replan_count": 0,
    }
    out = asyncio.run(handle_failure(state))
    assert out["failure_strategy"] == "replan"    # 归因不出 → 有界 replan 阶梯
    assert out["replan_count"] == 1               # 与 L2 共用同一熔断计数器
    assert out["verification_failure"] is None    # 清专类，不粘滞下一轮
    # 未落 "l2" 分支的旁证：l2 分支必写 l2_passed，runtime 专类分支不碰
    assert "l2_passed" not in out

    # 熔断耗尽 → escalate 人工终点（绝不无界循环）
    out2 = asyncio.run(handle_failure({**state, "replan_count": 99}))
    assert out2["failure_strategy"] == "escalate"
    assert out2["failure_escalated"] is True
    assert out2["verification_failure"] is None
    assert "l2_passed" not in out2


# ───────────────────────── L2 延活转交（__init__ 两处）─────────────────────────

def _run_reactor(monkeypatch, manager, *, build_out="__RC__0"):
    import swarm.brain.nodes as nodes_pkg
    import swarm.worker.sandbox as ws

    manager.rebuild_stdout = build_out
    monkeypatch.setattr(nodes_pkg, "_sandbox_available", lambda: True)
    monkeypatch.setattr(ws, "get_sandbox_manager", lambda: manager)
    return nodes_pkg._run_reactor_build_in_sandbox("/tmp/fake-project", "p1", "stub-build")


def test_handoff_on_compile_success_keeps_sandbox_and_returns_sid(monkeypatch):
    monkeypatch.delenv("SWARM_RUNTIME_SMOKE_ENABLED", raising=False)
    manager = _StubManager(extend_ok=True)
    ran, ok, _out, sid = _run_reactor(monkeypatch, manager)
    assert (ran, ok) == (True, True)
    assert sid == "sb-selfbuilt"          # 转交成立：sid 回传调用方入 state
    assert manager.killed == []           # 不 kill——处置责任移交 verify_runtime
    assert manager.extend_calls, "转交前必须续期"


def test_no_handoff_on_compile_failure_kills_as_before(monkeypatch):
    monkeypatch.delenv("SWARM_RUNTIME_SMOKE_ENABLED", raising=False)
    manager = _StubManager(extend_ok=True)
    ran, ok, _out, sid = _run_reactor(monkeypatch, manager, build_out="__RC__1 err")
    assert (ran, ok) == (True, False)
    assert sid is None
    assert manager.killed == ["sb-selfbuilt"]  # 编译失败路径一行不变：照旧 finally kill


def test_no_handoff_when_extend_fails(monkeypatch):
    monkeypatch.delenv("SWARM_RUNTIME_SMOKE_ENABLED", raising=False)
    manager = _StubManager(extend_ok=False)
    ran, ok, _out, sid = _run_reactor(monkeypatch, manager)
    assert (ran, ok) == (True, True)
    assert sid is None                       # 续期失败→转交不成立
    assert manager.killed == ["sb-selfbuilt"]


def test_handoff_budget_reserves_prepare(monkeypatch):
    """F1 同口径：转交时推导未发生（prepare_cmd 未知）→ 续期预算保守恒加 prepare 预算，
    否则转交沙箱会在增量 package 中途到期、白白废掉快路径。"""
    from swarm.brain.nodes.runtime_smoke import resolve_prepare_timeout_sec
    monkeypatch.delenv("SWARM_RUNTIME_SMOKE_ENABLED", raising=False)
    manager = _StubManager(extend_ok=True)
    _run_reactor(monkeypatch, manager)
    assert manager.extend_calls[0][1] == _budget_base() + resolve_prepare_timeout_sec()


def test_no_handoff_when_smoke_disabled(monkeypatch):
    monkeypatch.setenv("SWARM_RUNTIME_SMOKE_ENABLED", "0")
    manager = _StubManager(extend_ok=True)
    ran, ok, _out, sid = _run_reactor(monkeypatch, manager)
    assert (ran, ok) == (True, True)
    assert sid is None                       # 开关关：完全回退旧行为
    assert manager.killed == ["sb-selfbuilt"]


# ───────────────────────── verify_l2 薄包装的转交收口 ─────────────────────────

def test_verify_l2_wrapper_attaches_sid_on_pass(monkeypatch):
    async def _impl(state, handoff):
        handoff.append("sb-l2")
        return {"l2_passed": True}

    monkeypatch.setattr(verify_mod, "_verify_l2_impl", _impl)
    out = asyncio.run(verify_mod.verify_l2({}))
    assert out["l2_passed"] is True
    assert out["runtime_smoke_sandbox_id"] == "sb-l2"


def test_verify_l2_wrapper_kills_sid_on_fail(monkeypatch):
    killed: list[str] = []

    async def _impl(state, handoff):
        handoff.append("sb-l2")
        return {"l2_passed": False}

    monkeypatch.setattr(verify_mod, "_verify_l2_impl", _impl)
    monkeypatch.setattr(verify_mod, "_kill_sandbox_quiet", killed.append)
    out = asyncio.run(verify_mod.verify_l2({}))
    assert "runtime_smoke_sandbox_id" not in out
    assert killed == ["sb-l2"]  # L2 未通过→verify_runtime 不会跑→本节点即时处置
