#!/usr/bin/env python3
"""R65E14-T3（#42）：tech_design advisory（未坐实 verdict=false）必须透传 degraded_reasons。

round65e14 复盘：PRD 明确要求"JWT 登录+Redis Token 黑名单（3.5）"与"前后端分离+Vue 代码生成
（3.7.1）"，tech_design 按既有设计把这两条 verdict=false 降级 advisory（grounded=False，
project_stack 权威定栈=Thymeleaf 基线）——降级本身是对的（防 LLM 自由文本臆测 block，栈由
detect_stack 磁盘 ground truth 权威）。但 planning_nodes 注释承诺"记日志 + 透传
degraded_reasons（人可见）"，实现只有 logger.info：架构级 PRD 冲突不进交付终态账/通知，
用户在交付层面对"按基线栈交付、PRD 架构要求被妥协"零感知（本轮"交付不忠实"感知的来源）。

治本：_package_tech_design_output（degraded 透传集散地，与 stage2_failed/incomplete/C3 三类
同构）把 grounded=False 的 verdict=false advisory 聚合成一条 `tech_design_advisory:` 前缀
reason 追加 degraded_reasons。行为边界（全消费面核过）：
  - gates 硬拦（can_auto_accept_plan/终态判据）均按具体标记/前缀 → 新前缀行为中立，不阻断；
  - L6 学习闸（pattern_extractor.blocking_degraded_reasons）：新前缀非信息性白名单 → 拦
    L6 成功模式写入——【方向正确】：带架构妥协的交付不该学成可复用成功模式（C10 同向）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.planning_nodes import _package_tech_design_output  # noqa: E402
from swarm.memory.pattern_extractor import blocking_degraded_reasons  # noqa: E402

_RESULT = {"architecture": "x", "data_model": ""}


def _pack(fact_issues, state=None):
    return _package_tech_design_output(
        dict(state or {}), dict(_RESULT), [{"path": "m/src/A.java"}], fact_issues, {})


# ── ① 死因本体：grounded=False 的 verdict=false → 透传 degraded_reasons ──

def test_ungrounded_false_advisory_reaches_degraded_reasons():
    fi = [
        {"claim": "PRD 要求 JWT 登录/注销 + Redis Token 黑名单（3.5 认证授权）",
         "verdict": "false", "grounded": False},
        {"claim": "PRD 要求前后端分离 + 代码生成器生成 Vue 页面（3.7.1）",
         "verdict": "false", "grounded": False},
    ]
    out = _pack(fi)
    dg = out.get("degraded_reasons") or []
    adv = [d for d in dg if str(d).startswith("tech_design_advisory:")]
    assert adv, f"advisory 必须透传 degraded_reasons（注释承诺的实现缺失=本 bug）: {dg}"
    assert "JWT" in adv[0], f"reason 应携带 claim 摘要供人读: {adv[0]}"
    assert "2" in adv[0], f"reason 应带条数: {adv[0]}"


# ── ② 保护：grounded=True 的 false 走 block 通道（after_tech_design），不进 advisory ──

def test_grounded_false_premise_not_labeled_advisory():
    fi = [{"claim": "需求点名文件 X.java", "verdict": "false", "grounded": True}]
    out = _pack(fi)
    adv = [d for d in (out.get("degraded_reasons") or [])
           if str(d).startswith("tech_design_advisory:")]
    assert not adv, f"坐实虚假前提是 block 通道的事，不得混进 advisory 透传: {adv}"


# ── ③ 保护：uncertain / true 不触发 ──

def test_uncertain_and_true_verdicts_do_not_trigger():
    fi = [
        {"claim": "密码策略 SHA512（3.5）", "verdict": "uncertain", "grounded": None},
        {"claim": "某已核实要求", "verdict": "true"},
    ]
    out = _pack(fi)
    assert not any(str(d).startswith("tech_design_advisory:")
                   for d in (out.get("degraded_reasons") or [])), \
        "uncertain/true 不是降级，不得透传"


# ── ④ 保护：无 fact_issues → degraded_reasons 不被无中生有 ──

def test_no_advisory_no_degraded_key():
    out = _pack([])
    assert not any(str(d).startswith("tech_design_advisory:")
                   for d in (out.get("degraded_reasons") or []))


# ── ⑤ 既有 degraded_reasons 追加而非覆盖（与 stage2 三类同构）──

def test_appends_to_existing_degraded_reasons():
    fi = [{"claim": "PRD 要求 Vue", "verdict": "false", "grounded": False}]
    out = _pack(fi, state={"degraded_reasons": ["prior_reason"]})
    dg = out.get("degraded_reasons") or []
    assert "prior_reason" in dg, f"追加不得覆盖既有 reasons: {dg}"
    assert any(str(d).startswith("tech_design_advisory:") for d in dg)


# ── ⑥ 行为方向锁定：advisory 前缀非信息性 → 拦 L6 成功学习（C10 同向，防架构妥协学成模式）──

def test_advisory_prefix_blocks_l6_success_learning():
    fi = [{"claim": "PRD 要求 JWT", "verdict": "false", "grounded": False}]
    out = _pack(fi)
    dg = out.get("degraded_reasons") or []
    assert blocking_degraded_reasons(dg), \
        "带架构妥协的交付不得学成 L6 可复用成功模式（若要放行 L6 须显式加信息性白名单并重新论证）"
