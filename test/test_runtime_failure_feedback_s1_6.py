"""S1-6 运行时失败证据回灌修复循环 —— 行为测试（替换 S1-4 占位分支）。

覆盖面：
1. 归因命中 → 定向恢复 + 证据注入（受影响子任务 retry_guidance 收到启动日志证据）；
2. 归因不出 → replan 阶梯（与 L2 共用 replan_count 熔断计数器，绝不给 runtime 单开无界通道）；
3. migration_failed classification 同族处理（S1-5 并行面：同经 verification_failure=
   "runtime_smoke" 进入，details 的 migration* 键证据同源消费）；
4. 环境类 classification 防御：skipped 不产生 verification_failure（verify_runtime 已挡，
   环境类绝不进失败通道）；
5. gates：runtime failed 不 auto-accept；skipped(None)/passed(True) 不阻断；
6. 有界性：replan_count 超限 → escalate；targeted_recovery_counts 按子任务配额耗尽 → escalate；
7. LEARN 面锁定：degraded 轮 / runtime 失败轮不写 L6 成功模式（should_write_success 既有
   degraded_reasons + can_auto_accept_delivery 双闸已覆盖，此处测试锁定现状防回归）。
"""
import asyncio

from swarm.brain.gates import can_auto_accept_delivery
from swarm.brain.nodes import handle_failure
from swarm.brain.nodes.shared import attribute_runtime_failure, runtime_failure_evidence
from swarm.types import Complexity, FileScope, SubTask, TaskPlan, WorkerOutput


def _st(sid, *, create=None, writable=None):
    return SubTask(
        id=sid,
        description=f"subtask {sid}",
        scope=FileScope(writable=writable or [], create_files=create or []),
    )


def _plan(*subtasks):
    return TaskPlan(subtasks=list(subtasks))


def _wo(sid, l1_passed=True):
    return WorkerOutput(
        subtask_id=sid,
        diff="--- a/X\n+++ b/X\n@@ -1 +1,2 @@\n a\n+b\n" if l1_passed else "",
        summary="",
        l1_passed=l1_passed,
        l1_details={},
        confidence="high" if l1_passed else "low",
    )


def _runtime_state(details, **over):
    """runtime_smoke 失败轮的最小 state：两个 L1 已过的子任务（st-1 写 Boot 源文件）。"""
    state = {
        "verification_failure": "runtime_smoke",
        "runtime_smoke_passed": False,
        "runtime_smoke_details": details,
        "failed_subtask_ids": [],
        "subtask_results": {"st-1": _wo("st-1"), "st-2": _wo("st-2")},
        "plan": _plan(
            _st("st-1", create=["src/app/Boot.java"]),
            _st("st-2", create=["src/app/Other.java"]),
        ),
        "dispatch_remaining": [],
        "replan_count": 0,
        "subtask_retry_counts": {},
        "targeted_recovery_counts": {},
    }
    state.update(over)
    return state


def _run(state):
    return asyncio.run(handle_failure(state))


# ═════════════════ 1. 归因命中 → 定向恢复 + 证据注入 ═════════════════

_BOOT_LOG_TAIL = (
    "Exception during startup\n"
    "\tat app.Boot.main(Boot.java:12)\n"
    "FATAL: application failed to start"
)


def test_attribution_hit_targeted_recovery_and_evidence_injection():
    state = _runtime_state({
        "classification": "code_error",
        "log_tail": _BOOT_LOG_TAIL,
        "code_error_hits": ["Exception"],
    })
    out = _run(state)

    # 定向恢复形态（对齐 "l2" 定向分支）：只重做归因子任务，保留成功兄弟
    assert out["failure_strategy"] == "retry"
    assert "st-1" not in out["subtask_results"], "归因到的 st-1 应被移除待重做"
    assert "st-2" in out["subtask_results"], "成功兄弟 st-2 不可被清空"
    assert "st-1" in out["dispatch_remaining"]
    assert "st-2" not in out["dispatch_remaining"]
    assert out["failed_subtask_ids"] == []
    assert "targeted_recovery" not in out  # 3.8 死键已删；定向形态由上方断言证明

    # 证据注入：既有 retry_guidance 通道（A4 round11 机制），重派 worker 可见启动日志证据
    by_id = {s.id: s for s in out["plan"].subtasks}
    guidance = by_id["st-1"].retry_guidance
    assert "Boot.java:12" in guidance, "log_tail 证据必须注入受影响子任务"
    assert "运行时" in guidance and "非编译" in guidance, "机制说明：运行时启动失败非编译失败"
    assert not by_id["st-2"].retry_guidance, "未归因的兄弟不得被注入证据"

    # 有界记账：与 L2 共用 replan_count + 按子任务 targeted_recovery_counts
    assert out["replan_count"] == 1, "定向恢复仍自增 replan_count（与 L2 共用熔断）"
    assert out["targeted_recovery_counts"] == {"st-1": 1}
    # 专类分支旁证：绝不落 "l2" 分支（l2 分支必写 l2_passed）
    assert "l2_passed" not in out
    assert out["verification_failure"] is None, "清专类，不粘滞下一轮"
    assert out["runtime_smoke_passed"] is False


# ═════════════════ 2. 归因不出 → replan 阶梯（共用计数器） ═════════════════

def test_attribution_miss_falls_to_replan_with_shared_counter():
    state = _runtime_state({
        "classification": "code_error",
        "log_tail": "process exited with generic failure, no source reference",
        "code_error_hits": [],
    })
    out = _run(state)
    assert out["failure_strategy"] == "replan"
    assert out["replan_count"] == 1, "replan 阶梯与 L2 共用 replan_count 计数器"
    assert out["failed_subtask_ids"] == []
    assert out["verification_failure"] is None
    assert out["runtime_smoke_passed"] is False
    assert "l2_passed" not in out


def test_replan_circuit_exhausted_escalates():
    # 熔断：replan_count 已达上限 → escalate 人工终点（镜像 "l3" 保底，绝不无界）
    state = _runtime_state(
        {"classification": "code_error", "log_tail": "no source reference"},
        replan_count=99,
    )
    out = _run(state)
    assert out["failure_strategy"] == "escalate"
    assert out["failure_escalated"] is True
    assert out["verification_failure"] is None
    assert out["runtime_smoke_passed"] is False
    assert "l2_passed" not in out


def test_targeted_quota_exhausted_escalates():
    # 归因命中但该子任务定向恢复配额（targeted_recovery_counts，round29 遗漏项#2 先例）
    # 已耗尽 → escalate，绝不无限"定向重做→冒烟又挂→再定向"
    state = _runtime_state(
        {"classification": "code_error", "log_tail": _BOOT_LOG_TAIL},
        targeted_recovery_counts={"st-1": 99},
    )
    out = _run(state)
    assert out["failure_strategy"] == "escalate"
    assert out["failure_escalated"] is True
    assert out["verification_failure"] is None
    assert "l2_passed" not in out


def test_partial_quota_exhaustion_warns_for_excluded(caplog):
    # hunter：归因到多个子任务但配额【部分】耗尽——被排除者绝不静默丢弃，WARN 列出
    # fid 与已耗配额；行为不变（仅重派 eligible 者）。
    import logging as _logging
    state = _runtime_state(
        {"classification": "code_error",
         "log_tail": "at app.Boot.main(Boot.java:12)\nat app.Other.run(Other.java:7)"},
        subtask_results={"st-1": _wo("st-1"), "st-2": _wo("st-2"), "st-3": _wo("st-3")},
        plan=_plan(
            _st("st-1", create=["src/app/Boot.java"]),
            _st("st-2", create=["src/app/Other.java"]),
            _st("st-3", create=["src/app/Third.java"]),
        ),
        targeted_recovery_counts={"st-1": 99},
    )
    # caplog 直挂生产 logger 对象（d33 固化修法）：①前序测试可能关掉父链 propagate，
    # 赌 propagate 链在全量套件里必 flake；②failure.py 的 logger 名是 "swarm.brain.nodes"
    # 而非模块名——直接引用模块 logger 对象，杜绝名字猜错。
    from swarm.brain.nodes import failure as _failure_mod
    _fl_logger = _failure_mod.logger
    _fl_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(_logging.WARNING, logger=_fl_logger.name):
            out = _run(state)
    finally:
        _fl_logger.removeHandler(caplog.handler)
    assert out["failure_strategy"] == "retry"
    assert out["dispatch_remaining"] == ["st-2"], "仅重派配额未耗尽者"
    assert "st-1" in out["subtask_results"], "被排除者保持现状不清空"
    warned = [r.message for r in caplog.records if "排除" in r.getMessage()]
    assert any("st-1" in r.getMessage() for r in caplog.records if "排除" in r.getMessage()), \
        f"被排除的 st-1 必须 WARN 留痕: {warned}"


# ═════════════════ 3. migration_failed 同族处理 ═════════════════

def _verify_runtime_migration_failed_state(monkeypatch, *, sql_error_text: str) -> dict:
    """F3 治本：经【真实 verify_runtime 路径】（stub 沙箱 + stub 执行器返 migration failed）
    产出 runtime 失败 state patch——不再手工自造 runtime_smoke_details 形状（旧测试的
    手工形状掩盖了写侧 migration_verify_details / 读侧 runtime_smoke_details 的断裂）。"""
    import swarm.brain.integration_review as ir
    import swarm.brain.migration_verify as mv
    import swarm.brain.nodes as nodes_pkg
    import swarm.brain.nodes.runtime_smoke as rs
    import swarm.brain.nodes.verify as verify_mod
    import swarm.brain.smoke_derive as sd
    import swarm.worker.sandbox as ws
    from swarm.brain.migration_verify import MigrationChannel, MigrationVerifyResult
    from swarm.brain.nodes.runtime_smoke import RuntimeSmokeResult
    from swarm.brain.smoke_derive import SmokeDerivation

    class _Sb:
        sandbox_id = "sb-mig"

    class _Mgr:
        _instances: dict = {}

        def try_extend_lifetime(self, sandbox, seconds):
            return True

        def create(self, project_id=None, source=""):
            return _Sb()

        def sync_project_to_sandbox(self, sandbox, path, workdir):
            pass

        def run_command(self, sandbox, command, timeout=120, **kwargs):
            from types import SimpleNamespace
            return SimpleNamespace(stdout="__RC__0", stderr="")

        def kill(self, sandbox_id):
            pass

    monkeypatch.delenv("SWARM_RUNTIME_SMOKE_ENABLED", raising=False)
    monkeypatch.setattr(nodes_pkg, "_get_project_path", lambda pid: "/tmp/fake-project")
    monkeypatch.setattr(nodes_pkg, "_sandbox_available", lambda: True)
    monkeypatch.setattr(ws, "get_sandbox_manager", lambda: _Mgr())
    monkeypatch.setattr(ir, "_detect_build_cmd_generic", lambda p: "stub-build")
    monkeypatch.setattr(sd, "derive_runtime_smoke", lambda stack, path: SmokeDerivation(
        start_cmd="run-the-app", port=8080, migration_kind="alembic"))
    monkeypatch.setattr(mv, "detect_migration_channel",
                        lambda kind, stack, path: MigrationChannel(
                            True, reason="embedded_db_evidence",
                            command="alembic upgrade head"))

    async def _fake_smoke(manager, sandbox, script, **kwargs):
        return RuntimeSmokeResult("passed", "started", "stub-passed",
                                  details={"probe_sequence": ["ok"]})

    async def _fake_exec(manager, sandbox, command, **kw):
        return MigrationVerifyResult(
            "failed", "sql_error", f"migration 执行失败(exit 1)：{sql_error_text[:60]}",
            evidence={"ran": True, "exit_code": 1, "hits": ["syntax error"],
                      "command": command, "output_tail": sql_error_text})

    monkeypatch.setattr(rs, "run_runtime_smoke", _fake_smoke)
    monkeypatch.setattr(mv, "execute_migration", _fake_exec)
    return asyncio.run(verify_mod.verify_runtime(
        {"project_id": "p1", "project_stack": {"backend": "python"}}))


def test_migration_failed_classification_same_family(monkeypatch):
    # S1-5 并行面：classification="migration_failed" 同经 verification_failure="runtime_smoke"
    # 进入。F3 治本：state 必须由真实 verify_runtime 路径产出（写读两侧形状同源），
    # 断言归因证据里的 SQL 错误文本真能到达 handle_failure 的证据注入。
    verify_out = _verify_runtime_migration_failed_state(
        monkeypatch, sql_error_text="ERROR executing V1__init.sql: syntax error at line 3")
    assert verify_out["verification_failure"] == "runtime_smoke"
    assert verify_out["runtime_smoke_details"]["classification"] == "migration_failed"

    state = _runtime_state(
        verify_out["runtime_smoke_details"],
        plan=_plan(
            _st("st-1", create=["db/migration/V1__init.sql"]),
            _st("st-2", create=["src/app/Other.java"]),
        ),
    )
    out = _run(state)
    assert out["failure_strategy"] == "retry"
    assert "st-1" in out["dispatch_remaining"], "migration 证据归因到 SQL 写者子任务"
    assert "st-2" in out["subtask_results"]
    by_id = {s.id: s for s in out["plan"].subtasks}
    assert "V1__init.sql" in by_id["st-1"].retry_guidance, "migration 错误输出注入修复证据"
    assert "syntax error" in by_id["st-1"].retry_guidance, "SQL 错误文本必须进归因证据"


# ═════════════════ 4. 环境类不进失败通道（防御） ═════════════════

def test_skipped_states_never_produce_verification_failure():
    # verify_runtime 的 skipped 三态（env_missing/inconclusive/…）绝不产生
    # verification_failure → 不可能进 handle_failure 的 runtime 分支（环境类不进失败通道）。
    from swarm.brain.nodes.verify import _runtime_skipped_state

    for reason in ("env_missing", "inconclusive", "runtime_smoke_disabled"):
        out = _runtime_skipped_state(reason, "msg", {})
        assert "verification_failure" not in out
        assert out["runtime_smoke_passed"] is None
        assert out["degraded_reasons"] == [f"runtime_smoke_skipped:{reason}"]


# ═════════════════ 5. gates：runtime 三态 ═════════════════

_GATE_BASE = {"l2_passed": True}


def test_gate_runtime_failed_blocks_auto_accept_with_specific_reason():
    allow, reason = can_auto_accept_delivery({**_GATE_BASE, "runtime_smoke_passed": False})
    assert allow is False
    assert reason.startswith("runtime_smoke_failed"), "专类归因文案，不得冒充 l2/l3"


def test_gate_runtime_skipped_or_passed_does_not_block():
    # None=跳过（degraded_reasons 已可观测）；True=通过——都不阻断
    for val in (None, True):
        allow, reason = can_auto_accept_delivery(
            {**_GATE_BASE, "runtime_smoke_passed": val})
        assert allow is True, f"runtime_smoke_passed={val} 不得阻断: {reason}"
    # 键缺失（旧任务/未接线）同样不阻断
    allow, _ = can_auto_accept_delivery(dict(_GATE_BASE))
    assert allow is True


# ═════════════════ 6. LEARN 面锁定（已有机制覆盖，防回归） ═════════════════

_LEARN_BASE = {"l2_passed": True, "complexity": Complexity.MEDIUM}


def test_learn_positive_control_clean_success_writes():
    from swarm.memory.pattern_extractor import should_write_success
    assert should_write_success(dict(_LEARN_BASE)) is True


def test_learn_degraded_round_never_learned_as_success():
    # runtime skipped 轮：degraded_reasons 留痕 → C10 既有闸拦 L6 成功模式写入
    from swarm.memory.pattern_extractor import should_write_success
    state = {**_LEARN_BASE,
             "runtime_smoke_passed": None,
             "degraded_reasons": ["runtime_smoke_skipped:env_missing"]}
    assert should_write_success(state) is False


def test_learn_runtime_failed_round_never_learned_as_success():
    # runtime 失败轮：can_auto_accept_delivery（A7 真实成功判据）拦 L6 成功模式写入
    from swarm.memory.pattern_extractor import should_write_success
    state = {**_LEARN_BASE, "runtime_smoke_passed": False}
    assert should_write_success(state) is False


# ═════════════════ 7. 证据结构化纯函数 ═════════════════

def test_runtime_failure_evidence_excludes_infra_noise():
    # derivation_evidence/sandbox 含配置文件路径（application.yml 等），若入证据面会把
    # 其写者子任务【每轮】误归因成失败源——必须只取应用自身输出面。
    details = {
        "classification": "code_error",
        "log_tail": "at app.Boot.main(Boot.java:12)",
        "code_error_hits": ["Exception"],
        "derivation_evidence": {"config": "src/main/resources/application.yml"},
        "sandbox": {"rebuild_output": "built src/app/Other.java"},
    }
    blob = runtime_failure_evidence(details)
    assert "Boot.java" in blob
    assert "application.yml" not in blob
    assert "Other.java" not in blob


def test_attribute_runtime_failure_reuses_path_attribution():
    plan = _plan(_st("st-1", create=["src/app/Boot.java"]),
                 _st("st-2", create=["src/app/Other.java"]))
    results = {"st-1": _wo("st-1"), "st-2": _wo("st-2")}
    hit = attribute_runtime_failure(
        plan, {"log_tail": _BOOT_LOG_TAIL}, results)
    assert hit == ["st-1"]
    # 无证据 → None（调用方退 replan 阶梯）
    assert attribute_runtime_failure(plan, {}, results) is None
    # 全员命中（非真子集）→ None，不误判定向面
    both = attribute_runtime_failure(
        plan, {"log_tail": "Boot.java and Other.java both referenced"}, results)
    assert both is None
