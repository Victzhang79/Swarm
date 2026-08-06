"""#29 B-2 — prepare（产物构建）失败被当环境噪声，auto_accept 自动放行未验证交付。

缺陷：`runtime_smoke.py` 对 `prepare_rc != 0` 无条件返回 skipped/prepare_failed，理由写
"L2 已证编译通过，package 阶段失败大概率是插件/缓存/环境问题"。而 skipped ⇒
`runtime_smoke_passed=None` ⇒ `gates.can_auto_accept_delivery` 按"None=跳过不算失败"
放行 ⇒ 【产物构建不出来的交付被自动接受】。

那条理由不成立：prepare 是 L2 编译闸的**严格超集**（STACK_SPEC 实测）——
    maven   compile  → package     （+资源过滤 +jar/war 装配 +spring-boot repackage +MANIFEST）
    gradle  classes  → bootJar     （+资源处理 +产物装配 +main class 解析）
差集里的失败（如 repackage 找不到唯一 main class、资源过滤炸在非法配置上）正是
"编译过但产物构建不出来"的真代码/配置缺陷，L2 结构上看不见 ⇒ "L2 已证编译通过"
不能替 prepare 背书。

治法（刻意不做代码/环境归因）：status 维持 skipped（不冤枉代码、不谎称启动失败），
但 gates 据 skip_reason=prepare_failed **硬拦 auto_accept** 交人工。
分档依据（血规 10③）：sandbox_unavailable / port_unresolved = 【观测缺口】（交付物可能
完好，硬拦会在基建抖动时 strand 全部交付）；prepare_failed = 【产物缺席】（交付能不能跑
根本没被验证过）。

★为什么不做归因★：需新造构建错误模式表（枚举完整性无权威来源，[[swarm-enumeration-
needs-authoritative-source]]）；且 `classify_smoke_outcome` 认的是【应用启动期】形态，
对构建期错误实测全归 inconclusive（不在 _LOG_DERIVED_CLASSIFICATIONS 内）⇒ 接上去是死代码。
一个确定性事实「产物没构建出来」已足以拒绝自动放行。
"""

from __future__ import annotations

import pytest

from swarm.brain.gates import can_auto_accept_delivery
from swarm.memory.pattern_extractor import blocking_degraded_reasons

_BASE = {"l2_passed": True, "l3_passed": True, "subtask_results": {}, "plan": None}


def _state(**smoke):
    return {**_BASE, **smoke}


# ══════════════════════════════════════════════════════════
# A) 缺陷本体：产物构建不出来必须硬拦
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize("rc", [1, 2, 127, -1])
def test_prepare_failed_hard_blocks_auto_accept(rc):
    """★prepare 非 0 → auto_accept 硬拦（修复前放行）★

    断言拒因【指名 prepare】而非仅 allow is False：若未来某条无关闸恰好也拦住，
    只断 False 会让这条测试替别人背书（零区分力，[[swarm-test-must-prove-wiring-not-correctness]]）。
    """
    allow, reason = can_auto_accept_delivery(_state(
        runtime_smoke_passed=None, runtime_smoke_skipped=True,
        runtime_smoke_details={"skip_reason": "prepare_failed", "prepare_rc": rc},
        degraded_reasons=["runtime_smoke_skipped:prepare_failed"],
    ))
    assert allow is False, "产物构建不出来却自动放行"
    assert "prepare_failed" in reason, f"拒因未指名 prepare: {reason}"
    assert str(rc) in reason, f"拒因未带 rc（人工无法定位）: {reason}"


def test_prepare_failed_reason_is_honest_about_attribution():
    """拒因不得谎称已知归因 —— 原实现文案称"按环境问题跳过"，那是把未知说成已知。"""
    _allow, reason = can_auto_accept_delivery(_state(
        runtime_smoke_passed=None, runtime_smoke_skipped=True,
        runtime_smoke_details={"skip_reason": "prepare_failed", "prepare_rc": 1},
        degraded_reasons=["runtime_smoke_skipped:prepare_failed"],
    ))
    assert "环境问题" not in reason, f"拒因把未知归因说成环境问题: {reason}"


# ══════════════════════════════════════════════════════════
# B) 分档边界：观测缺口照旧放行（防 strand 全部交付）
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize("skip_reason", [
    "sandbox_unavailable",
    "port_unresolved",
    "stale_listener_suspected",
    "port_busy",
    "smoke_disabled",
    "derivation_incomplete",
])
def test_observation_gap_skips_still_pass(skip_reason):
    """★其余 skip 档必须仍放行★ —— 它们是"我们没能观测"（交付物可能完好）。

    这条锁住分档边界：把 skipped 整档拧成硬拦，会在 provider/沙箱抖动时 strand
    全部交付（本仓既有哲学，runtime_smoke 模块 docstring 有分型论证）。
    """
    allow, reason = can_auto_accept_delivery(_state(
        runtime_smoke_passed=None, runtime_smoke_skipped=True,
        runtime_smoke_details={"skip_reason": skip_reason},
        degraded_reasons=[f"runtime_smoke_skipped:{skip_reason}"],
    ))
    assert allow is True, f"{skip_reason} 被误硬拦（观测缺口不该 strand 交付）: {reason}"


def test_skip_without_details_still_passes():
    """details 缺失/非 dict（旧 checkpoint）→ 不误拦（fail-open 仅限此兼容面，
    且 degraded 仍挡 L6，故不是静默假绿）。"""
    for details in (None, {}, "not-a-dict", {"skip_reason": ""}):
        allow, _r = can_auto_accept_delivery(_state(
            runtime_smoke_passed=None, runtime_smoke_skipped=True,
            runtime_smoke_details=details,
            degraded_reasons=["runtime_smoke_skipped:unknown"],
        ))
        assert allow is True, f"details={details!r} 被误拦"


def test_smoke_passed_and_failed_arms_unchanged():
    """通过/失败两臂不受影响（防改动溢出）。"""
    allow, _r = can_auto_accept_delivery(_state(
        runtime_smoke_passed=True, runtime_smoke_skipped=False,
        runtime_smoke_details={"classification": "started_ok"}, degraded_reasons=[]))
    assert allow is True
    allow, reason = can_auto_accept_delivery(_state(
        runtime_smoke_passed=False, runtime_smoke_skipped=False,
        runtime_smoke_details={"classification": "code_error"}, degraded_reasons=[]))
    assert allow is False and "runtime_smoke_failed" in reason


def test_prepare_failed_does_not_hijack_failed_arm_attribution():
    """★归因不得串味★：runtime_smoke_passed=False 时即使 details 带 prepare 痕迹，
    也必须走 failed 臂的既有分型文案（acceptance/migration/启动探活），不被本闸抢答。

    判序保障：本闸在 `rt is False` 分支【之后】，且以 `passed is None` 为前置。
    """
    allow, reason = can_auto_accept_delivery(_state(
        runtime_smoke_passed=False, runtime_smoke_skipped=False,
        runtime_smoke_details={"classification": "acceptance_failed",
                               "skip_reason": "prepare_failed", "prepare_rc": 1},
        degraded_reasons=[]))
    assert allow is False
    assert "acceptance_failed" in reason, f"failed 臂归因被 prepare 闸抢答: {reason}"


# ══════════════════════════════════════════════════════════
# C) 节点侧：机读键 + 归因诚实 + L6 阻断
# ══════════════════════════════════════════════════════════

def _drive_smoke(prepare_rc: int, extra_out: str = ""):
    """真跑 run_runtime_smoke 的 prepare 失败路径（假 manager，只喂沙箱输出文本）。

    行为级驱动，不用 getsource 断字面量（纪律 6）。
    """
    import asyncio as _aio

    from swarm.brain.nodes.runtime_smoke import (
        MARK_DONE, MARK_PREPARE_RC, run_runtime_smoke,
    )

    out = f"{MARK_PREPARE_RC}{prepare_rc}\n{extra_out}\n{MARK_DONE}\n"

    class _Result:
        stdout = out
        stderr = ""

    class _Mgr:
        @staticmethod
        def run_command(_sandbox, _script, timeout=None, _skip_blacklist=False):
            return _Result()

    return _aio.run(run_runtime_smoke(
        _Mgr(), object(), "fake script", timeout_sec=30, language_key="java"))


@pytest.mark.parametrize("rc", [1, 2, 127])
def test_node_emits_machine_readable_artifact_flag(rc):
    """★节点侧真跑：prepare 非 0 → skipped/prepare_failed + artifact_build_failed 机读键★

    （血规 10④：降级路径至少一个机读键，且该键必须有消费者——消费者见下条测试。）
    """
    res = _drive_smoke(rc)
    assert res.status == "skipped", f"prepare 失败被判成 {res.status}（不该冤枉代码）"
    assert res.classification == "prepare_failed"
    assert res.details.get("prepare_rc") == rc
    assert res.details.get("artifact_build_failed") is True, (
        f"缺 artifact_build_failed 机读键: {res.details}")


def test_node_message_does_not_claim_environment_cause():
    """★归因诚实★：节点文案不得再称"按环境问题跳过"——那是把未知归因说成已知。"""
    res = _drive_smoke(1)
    assert "按环境问题跳过" not in res.message, f"仍谎称已知是环境: {res.message}"
    assert "归因未定" in res.message, f"未如实声明归因未定: {res.message}"


def test_node_output_feeds_the_gate_end_to_end():
    """★接线闭环：节点产出的 details 直接喂 gates 必须被硬拦★

    这条把"节点写的键"与"gates 读的键"接在一起验——两侧靠 skip_reason 字符串对齐，
    任一侧改名都会在这里红（而不是等到生产上静默放行）。
    """
    res = _drive_smoke(1)
    # 复刻 _runtime_skipped_state 的组装：skip_reason 由 classification 填入
    details = {**res.details, "skip_reason": res.classification}
    allow, reason = can_auto_accept_delivery(_state(
        runtime_smoke_passed=None, runtime_smoke_skipped=True,
        runtime_smoke_details=details,
        degraded_reasons=[f"runtime_smoke_skipped:{res.classification}"],
    ))
    assert allow is False, "节点产出喂进 gates 未被硬拦 ⇒ 两侧键名/取值失配"
    assert "prepare_failed" in reason


def test_prepare_rc_zero_does_not_take_the_branch():
    """反向区分力：prepare 成功(rc=0) 不进本分支（否则闸恒触发＝零区分力）。"""
    res = _drive_smoke(0)
    assert res.classification != "prepare_failed", (
        "prepare 成功也被判 prepare_failed ⇒ 闸恒触发，会拦下全部交付")
    assert res.details.get("artifact_build_failed") is not True


def test_prepare_failed_blocks_l6_success_learning():
    """degraded runtime_smoke_skipped:prepare_failed 必须挡 L6（既有行为，回归锁）。"""
    assert blocking_degraded_reasons(["runtime_smoke_skipped:prepare_failed"]), (
        "产物构建不出来的交付会被学成成功模式")


def test_prepare_is_superset_of_l2_compile_gate():
    """★本 finding 的承重证据：prepare 命令确实做得比 L2 编译闸多★

    若哪天 STACK_SPEC 把两者改成同一条命令，"L2 通过不能替 prepare 背书"这个论证
    就不再成立，本闸的存在理由需重审 —— 这条测试是那个前提的看门人。
    单一事实源派生，不手抄栈清单（[[swarm-enumeration-needs-authoritative-source]]）。
    """
    from swarm.stacks.spec import STACK_SPEC
    checked = 0
    for key, spec in STACK_SPEC.items():
        prep = getattr(spec, "runtime_prepare_cmd", None)
        build = getattr(spec, "whole_project_build_cmd", None)
        if not prep or not build:
            continue   # 该栈无 prepare 阶段，本闸对它不适用
        checked += 1
        assert prep.strip() != build.strip(), (
            f"{key}: prepare 与 L2 编译闸命令相同 ⇒ 「L2 不能替 prepare 背书」的论证失效，"
            f"本闸需重审")
    assert checked >= 1, (
        "无任何栈定义 runtime_prepare_cmd ⇒ 本闸零生产可达，"
        "说明 STACK_SPEC 变了（或断言取错字段名）")
