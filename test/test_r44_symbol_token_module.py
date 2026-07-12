#!/usr/bin/env python3
"""R44（round44 取证）—— 符号外科模块归属词元通道（R41-5 真身治本）。

取证（task 6c8d5ee3 终态 plan 离线复跑）：契约 module=逻辑模块名
（alarm-channel/alarm-schedule…），plan 子任务模块分布=物理目录
（ruoyi-alarm×58/ruoyi-admin×20/…）——路径首段推导与逻辑名永不相交，
外科四轮 0 命中（0/100、0/72、0/76、0/47），符号类打回只剩全量重拆一条路
（round44 三轮 10/22=45% 一字不差原地打转 FAILED）。
治本：_candidates 词元通道兜底——模块名全部词元段以子串出现在写目标目录部
（小写去分隔符）才算候选；真 plan 验证 9/9 逻辑模块均有候选。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.symbol_surgery import surgical_symbol_attach  # noqa: E402
from swarm.types import (  # noqa: E402
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
)


def _st(sid, create=None, desc=""):
    return SubTask(id=sid, description=desc or f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=[], create_files=create or []))


def _plan_round44_shape():
    """复刻 round44 真实拓扑：逻辑模块代码落在 ruoyi-alarm 物理目录的功能包下。"""
    return TaskPlan(subtasks=[
        _st("st-1", create=[
            "ruoyi-alarm/src/main/java/com/ruoyi/alarm/channel/BotServiceImpl.java"]),
        _st("st-2", create=[
            "ruoyi-alarm/src/main/java/com/ruoyi/alarm/schedule/DutyServiceImpl.java",
            "ruoyi-alarm/src/main/java/com/ruoyi/alarm/schedule/RotationJob.java"]),
        _st("st-3", create=["ruoyi-admin/src/main/resources/templates/alarm/index.html"]),
    ], parallel_groups=[["st-1", "st-2", "st-3"]])


def test_token_channel_attaches_logical_module_symbols():
    plan = _plan_round44_shape()
    sc = {"interfaces": [
        {"name": "ICallbackMessageBuilder", "module": "alarm-channel", "methods": []},
        {"name": "IScheduleGroupService", "module": "alarm-schedule", "methods": []},
    ]}
    report = surgical_symbol_attach(plan, sc)
    assert report["attached"].get("ICallbackMessageBuilder") == "st-1", \
        "alarm-channel 词元 channel 命中 st-1 目录"
    assert report["attached"].get("IScheduleGroupService") == "st-2"
    assert not report["remainder"]
    assert "ICallbackMessageBuilder" in (plan.subtasks[0].contract or {}).get("symbols", [])


def test_token_channel_requires_all_segments():
    """全段命中：模块 alarm-payment 在纯 channel 目录树上无候选（不猜挂）。"""
    plan = _plan_round44_shape()
    sc = {"interfaces": [
        {"name": "IPaymentService", "module": "alarm-payment", "methods": []},
    ]}
    report = surgical_symbol_attach(plan, sc)
    assert report["attached"] == {} and report["remainder"] == ["IPaymentService"]


def test_f1_root_file_only_subtask_never_owns():
    """复核 F1：纯根文件（DDL/文档）子任务绝不成为词元候选（R39 红线族）。"""
    plan = TaskPlan(subtasks=[
        _st("st-sql", create=["alarm_channel_schema.sql"]),
        _st("st-doc", create=["alarm-channel-design.md"]),
    ], parallel_groups=[["st-sql", "st-doc"]])
    sc = {"interfaces": [
        {"name": "IBotService", "module": "alarm-channel", "methods": []},
    ]}
    report = surgical_symbol_attach(plan, sc)
    assert report["attached"] == {} and report["remainder"] == ["IBotService"]


def test_f2_cross_file_conflation_not_candidate():
    """复核 F2：跨文件拼凑词元段不算候选（须单目录全段命中）。"""
    plan = TaskPlan(subtasks=[
        _st("st-x", create=[
            "ruoyi-alarm/src/main/java/com/ruoyi/alarm/log/A.java",
            "ruoyi-system/src/main/java/com/ruoyi/system/notify/B.java"]),
    ], parallel_groups=[["st-x"]])
    sc = {"interfaces": [
        {"name": "INotifyService", "module": "alarm-notify", "methods": []},
    ]}
    report = surgical_symbol_attach(plan, sc)
    assert report["attached"] == {} and report["remainder"] == ["INotifyService"]


def test_physical_module_exact_channel_still_first():
    """物理目录精确命中仍是第一通道（词元只兜底）。"""
    plan = TaskPlan(subtasks=[
        _st("st-a", create=["alarm-channel/src/main/java/X.java"]),
        _st("st-b", create=[
            "ruoyi-alarm/src/main/java/com/ruoyi/alarm/channel/Y.java"]),
    ], parallel_groups=[["st-a", "st-b"]])
    sc = {"interfaces": [
        {"name": "IBotService", "module": "alarm-channel", "methods": []},
    ]}
    report = surgical_symbol_attach(plan, sc)
    assert report["attached"].get("IBotService") == "st-a", "精确物理模块候选优先"
