#!/usr/bin/env python3
"""R42（round42 治本批）—— C1 owner 判定的确定性命名惯例等价。

取证（task 4ab0c278，2026-07-12 FAILED@PLAN，2h51min 0 执行）：C1 硬符号三轮
22→22→21/26（81%>40%）不收敛重试耗尽。dump 终态 plan 实锤：契约符号
AlarmTaskService ↔ 计划文件 IAlarmTaskService.java + AlarmTaskServiceImpl.java
（RuoYi I 前缀/Impl 惯例）、NotifyUserService ↔ IAlarmNotifyUserService.java
（加项目前缀装饰）——**plan 按栈惯例命名没错，字面 basename 对账口径错**，
教育 LLM 三轮无解。21 个"无主"仅 2 个真缺（AppSecretService/SendLogService），
真实缺口 8% << 40% 阈值。同根因：符号外科两轮 0 命中（0/100、0/72）。
治本：basename_owns_symbol 惯例等价（精确/I 前缀/Impl 后缀/CamelCase 边界装饰
前缀），unowned_contract_symbols 文件通道 + baseline_symbol_files 存量豁免同口径。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.contract_utils import baseline_symbol_files  # noqa: E402
from swarm.brain.plan_validator import (  # noqa: E402
    basename_owns_symbol,
    unowned_contract_symbols,
)
from swarm.types import (  # noqa: E402
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
)


def _st(sid, desc="", writable=None, create=None):
    return SubTask(id=sid, description=desc or f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=writable or [], create_files=create or []))


# ── ① 等价规则单元面（round42 真实名对）──

def test_exact_and_interface_prefix():
    assert basename_owns_symbol("AlarmTaskService", "AlarmTaskService")
    assert basename_owns_symbol("IAlarmTaskService", "AlarmTaskService")


def test_impl_suffix_variants():
    assert basename_owns_symbol("AlarmTaskServiceImpl", "AlarmTaskService")
    assert basename_owns_symbol("IAlarmTaskServiceImpl", "AlarmTaskService")


def test_decorated_prefix_camelcase_boundary():
    # round42 实锤：NotifyUserService ↔ IAlarmNotifyUserService.java
    assert basename_owns_symbol("IAlarmNotifyUserService", "NotifyUserService")
    assert basename_owns_symbol("AlarmNotifyUserServiceImpl", "NotifyUserService")
    assert basename_owns_symbol("IAlarmScheduleStrategyService", "ScheduleStrategyService")


def test_boundary_guards_no_false_positive():
    # 截半个词绝不算（后缀起点非大写=非 CamelCase 词首）
    assert not basename_owns_symbol("MyalarmTaskservice", "TaskService")
    # 短符号（<8 字符）不开装饰前缀通道，防泛匹配
    assert not basename_owns_symbol("AlarmService", "Service")
    # 无关文件
    assert not basename_owns_symbol("SendLogController", "SendLogService")
    assert not basename_owns_symbol("", "X") and not basename_owns_symbol("X", "")


# ── ② C1 闸整合面：round42 死局在新口径下过闸 ──

def test_round42_death_plan_now_converges():
    """真实死局复现：I 前缀/Impl/装饰前缀文件齐备的 plan，硬符号缺口应只剩真缺的 2 个。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=[
            "alarm-core/src/main/java/com/ruoyi/alarm/service/IAlarmTaskService.java",
            "alarm-core/src/main/java/com/ruoyi/alarm/service/impl/AlarmTaskServiceImpl.java"]),
        _st("st-2", create=[
            "alarm-core/src/main/java/com/ruoyi/alarm/service/IAlarmNotifyUserService.java",
            "alarm-core/src/main/java/com/ruoyi/alarm/service/impl/AlarmNotifyUserServiceImpl.java"]),
        _st("st-3", create=[
            "alarm-schedule/src/main/java/com/ruoyi/alarm/schedule/IAlarmScheduleStrategyService.java"]),
    ], parallel_groups=[["st-1", "st-2", "st-3"]])
    symbols = ["AlarmTaskService", "NotifyUserService", "ScheduleStrategyService",
               "AppSecretService", "SendLogService"]
    unowned = unowned_contract_symbols(plan, symbols)
    assert unowned == ["AppSecretService", "SendLogService"], \
        "惯例等价后只剩真缺的 2 个（2/5=40% 边界内话术不论，round42 真实 2/26=8% 过闸）"


def test_corpus_channel_unchanged():
    """语料词边界通道行为不变（描述点名符号仍算 owner）。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", desc="实现 AlarmSendService 的发送编排", create=["x/A.java"]),
    ], parallel_groups=[["st-1"]])
    assert unowned_contract_symbols(plan, ["AlarmSendService"]) == []


# ── ③ 存量豁免同口径（棕地 I 前缀）──

def test_baseline_exemption_honors_naming_convention(tmp_path):
    d = tmp_path / "ruoyi-system" / "service"
    d.mkdir(parents=True)
    (d / "ISysRoleService.java").write_text("interface ISysRoleService {}", "utf-8")
    (d / "SysRoleServiceImpl.java").write_text("class SysRoleServiceImpl {}", "utf-8")
    hits = baseline_symbol_files(["SysRoleService", "NoSuchService"], str(tmp_path))
    assert hits == {"SysRoleService"}


# ── ④ 对抗复核整改回归（F1/F2/F3）──

def test_f2_longest_symbol_wins_masked_short_symbol_stays_unowned():
    """F2：AlarmTaskServiceImpl 只归最长者 AlarmTaskService；真缺的 TaskService
    不被装饰前缀通道吞掉（L2 子串核验兜不住这类遮蔽，必须闸口消歧）。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=[
            "alarm-core/src/svc/IAlarmTaskService.java",
            "alarm-core/src/svc/impl/AlarmTaskServiceImpl.java"]),
    ], parallel_groups=[["st-1"]])
    unowned = unowned_contract_symbols(plan, ["AlarmTaskService", "TaskService"])
    assert unowned == ["TaskService"], "短符号被长符号的文件吞掉=真缺被遮蔽"
    # 短符号有自己的惯例文件时正常归属
    plan.subtasks[0].scope.create_files.append("alarm-core/src/svc/ITaskService.java")
    assert unowned_contract_symbols(plan, ["AlarmTaskService", "TaskService"]) == []


def test_f3_baseline_exemption_no_decorated_prefix(tmp_path):
    """F3：棕地豁免关 ④ 通道——ISysUserService 不豁免新符号 UserService。"""
    d = tmp_path / "ruoyi-system"
    d.mkdir(parents=True)
    (d / "ISysUserService.java").write_text("interface ISysUserService {}", "utf-8")
    hits = baseline_symbol_files(["UserService", "SysUserService"], str(tmp_path))
    assert hits == {"SysUserService"}, "①②③ 保留（I 前缀承接 SysUserService），④ 关闭"


def test_f1_c2_dispatch_gate_same_caliber():
    """F1：C2 派发闸与 C1 同口径——I 前缀文件 owner 的符号缺席 diff 必须被抓。"""
    from swarm.brain.nodes.dispatch import _c2_missing_symbols
    st = _st("st-1", create=["alarm-core/src/svc/IAlarmTaskService.java"])
    sc = {"interfaces": [{"name": "AlarmTaskService", "methods": []}]}
    # diff 不含符号 → missing（旧字面口径下 owned=False 结构性失火）
    assert _c2_missing_symbols(st, sc, "diff --git a/x b/x\n+nothing") == \
        ["AlarmTaskService"]
    # diff 含惯例装饰名（子串覆盖符号）→ 不误报
    assert _c2_missing_symbols(
        st, sc, "+public interface IAlarmTaskService {}") == []
