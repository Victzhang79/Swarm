#!/usr/bin/env python3
"""R64-T4：契约↔file_plan 命名漂移的源头预防 + 残量可观测。

round64 实锤（cassette seq8-13 亲核）：契约 prompt 不含 file_plan（has_file_plan=False），
两个命名空间独立产生 → 56 个契约符号 30 个 name 对不上任何 file_plan basename
（AlarmSimpleRequest↔SimpleNotifyRequest.java、AlarmComposeUtil 无文件）→ 下游代偿链
（R48b-1 为幻影名造重复文件 / C1 软符号告警 / R62-Task5 每轮重新归一 40→59）。

治本＝①契约 per-module prompt 注入该模块 file_plan 文件名 + 命名铁律（源头对齐，栈中立）；
②pin_contract_symbol_paths 的零命中分支从全静默改 INFO 留痕（round65 漂移残量观察面）。
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.planning_nodes import (  # noqa: E402
    CONTRACT_MODULE_USER,
    _contract_module_files_block,
)

_FP = [
    {"module": "alarm-interface", "path": "alarm-interface/src/main/java/com/ruoyi/alarm/sdk/model/SimpleNotifyRequest.java"},
    {"module": "alarm-interface", "path": "alarm-interface/src/main/java/com/ruoyi/alarm/sdk/AlarmClient.java"},
    {"module": "ruoyi-alarm", "path": "ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/AlarmApp.java"},
    {"module": "ruoyi-alarm", "path": "sql/alarm.sql"},
]


def test_block_lists_only_module_files():
    """只列本模块文件名（含辅助文件），不串其它模块。"""
    block = _contract_module_files_block(_FP, "alarm-interface")
    assert "SimpleNotifyRequest.java" in block and "AlarmClient.java" in block
    assert "AlarmApp.java" not in block, "绝不串其它模块文件"


def test_block_dedup_and_cap():
    fp = [{"module": "m", "path": f"m/src/F{i}.java"} for i in range(80)]
    fp += [{"module": "m", "path": "m/src/F0.java"}]   # 重复
    block = _contract_module_files_block(fp, "m", cap=60)
    assert block.count("F0.java") == 1, "同名去重"
    assert "共 80 个" in block and "仅列前 60" in block, "超 cap 必须如实声明截断"


def test_block_empty_module_honest():
    """无清单模块 → 明示铁律不适用，绝不静默空串（LLM 会把空串当'没有约束'）。"""
    block = _contract_module_files_block(_FP, "ghost-module")
    assert "暂无已规划文件清单" in block


def test_template_formats_with_module_files():
    """★接线锁★ 模板必须含 {module_files} 占位符且全参可 format（缺参=KeyError 直红）。"""
    out = CONTRACT_MODULE_USER.format(
        task_description="t", data_model="d", skeleton="s",
        mod_idx=1, mod_total=5, mod_name="alarm-interface",
        mod_responsibility="r", consumed_by="a、b", expected_surface="e",
        module_files="- SimpleNotifyRequest.java")
    assert "SimpleNotifyRequest.java" in out
    assert "命名铁律" in out, "命名指令必须在模板里（round64 死于 LLM 无任何命名指引）"


def test_pin_full_miss_is_observable(caplog):
    """★残量观察面锁★ 契约符号零 basename 命中（真漂移）必须 INFO 留痕——round64 30 个
    漂移全走此前的全静默分支，排障只能第一性原理考古。"""
    from swarm.brain.symbol_provenance import pin_contract_symbol_paths
    from swarm.types import (
        FileScope,
        SubTask,
        SubTaskDifficulty,
        SubTaskModality,
        TaskPlan,
    )

    sc = FileScope(writable=[], readable=[], create_files=[
        "alarm-interface/src/main/java/com/ruoyi/alarm/sdk/model/SimpleNotifyRequest.java"])
    st = SubTask(id="a", description="a", difficulty=SubTaskDifficulty.MEDIUM,
                 modality=SubTaskModality.TEXT, scope=sc)
    plan = TaskPlan(subtasks=[st], parallel_groups=[["a"]])
    plan.shared_contract = {"dtos": [
        {"name": "AlarmSimpleRequest", "module": "alarm-interface", "fields": []}]}
    with caplog.at_level(logging.INFO, logger="swarm.brain.symbol_provenance"):
        pin_contract_symbol_paths(plan)
    assert any("零 basename 命中" in r.message and "AlarmSimpleRequest" in r.message
               for r in caplog.records), "真漂移必须可观测"
