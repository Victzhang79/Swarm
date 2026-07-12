#!/usr/bin/env python3
"""R43（round43 随跑取证）—— C1 硬对账两处补漏。

取证（task ae546ec1 首轮 VALIDATE 打回 24/37=65%）：
① GLM 把方法名塞进硬性键（apis/interfaces）：getByAppId/validateApp/registerApp…
   小写开头成员从不对应独立文件，file-owner 硬对账结构性无解 → 降软（惯例判据）。
② 契约符号带 I 前缀（IChannelAdapter/IAlarmEngineService），实现文件不带 I
   （DingTalkChannelAdapter/AlarmEngineServiceImpl）——r42 只治了"文件带 I 符号
   不带"，反方向再杀 → basename_owns_symbol 符号侧 I 前缀归一。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.plan_validator import (  # noqa: E402
    basename_owns_symbol,
    validate_contract_ownership,
)
from swarm.types import (  # noqa: E402
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
)


def _st(sid, desc="", create=None):
    return SubTask(id=sid, description=desc or f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=[], create_files=create or []))


# ── ② 符号侧 I 前缀归一 ──

def test_symbol_with_i_prefix_owned_by_impl_without_i():
    assert basename_owns_symbol("ChannelAdapter", "IChannelAdapter")
    assert basename_owns_symbol("DingTalkChannelAdapter", "IChannelAdapter")
    assert basename_owns_symbol("AlarmEngineServiceImpl", "IAlarmEngineService")


def test_symbol_i_prefix_boundary_guards():
    # 半词/小写边界不算
    assert not basename_owns_symbol("myChanneladapter", "IChannelAdapter")
    # I 后非大写 ≠ 接口惯例（如 Image / Index）
    assert not basename_owns_symbol("mage", "Image")
    assert basename_owns_symbol("Image", "Image")


# ── ① 成员形态降软 ──

def test_lowercase_member_symbols_do_not_bounce():
    """方法名混进硬键（round43 实测 8 个）不再顶爆 40% 阈值。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["m/src/IChannelAdapter.java",
                            "m/src/DingTalkChannelAdapter.java"]),
    ], parallel_groups=[["st-1"]])
    sc = {"interfaces": [{"name": "IChannelAdapter", "methods": []}],
          "apis": ["getByAppId", "validateApp", "registerApp", "resetSecret",
                   "refreshCache", "clearCache", "getCachedApp", "updateApp"]}
    r = validate_contract_ownership(plan, sc)
    assert r.valid, f"方法名应降软、接口被 I 归一承接：issues={r.issues}"
    blob = " ".join(r.warnings)
    assert "getByAppId" in blob, "降软后仍 warn 可观测（L2 全量核验不变）"


def test_f1_exact_twin_not_shadowed_by_istrip_channel():
    """复核 F1（CONFIRMED 回归）：契约同含 IChannelAdapter+ChannelAdapter 双胞胎、
    两个精确同名文件都在计划时，双双认主（精确匹配强度赢过等价通道+长度）。"""
    from swarm.brain.plan_validator import unowned_contract_symbols
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["m/src/IChannelAdapter.java"]),
        _st("st-2", create=["m/src/ChannelAdapter.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    assert unowned_contract_symbols(
        plan, ["IChannelAdapter", "ChannelAdapter"]) == []


def test_f4_l2_substring_accepts_ibase_variant():
    """复核 F4：L2 子串核验与 C1 惯例等价对称——契约 IChannelAdapter、代码只写
    ChannelAdapter 不再判缺（两张皮不位移到 L2）。"""
    from swarm.brain.integration_review import check_contract_in_diff
    sc = {"interfaces": [{"name": "IChannelAdapter", "methods": []}]}
    ok, issues = check_contract_in_diff(
        "+public class DingTalkChannelAdapter implements ChannelAdapter {", sc)
    assert ok and not issues


def test_uppercase_type_symbols_still_hard():
    """真类型缺 owner 仍硬性打回（降软规则不放水）。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["m/src/Other.java"]),
        _st("st-2", create=["m/src/Other2.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    sc = {"interfaces": [{"name": "AlarmTaskService", "methods": []},
                         {"name": "TemplateRenderService", "methods": []}]}
    r = validate_contract_ownership(plan, sc)
    assert not r.valid and "2/2" in " ".join(r.issues)
