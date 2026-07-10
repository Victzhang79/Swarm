#!/usr/bin/env python3
"""A1（round38c P0）—— L2 契约失败归因单调守卫：全员清零路径永久封死。

round38c 死因（TASK_REGISTER 主题 A/A1，取证 forensics_A Q1）：
  (a) _d5_attribute_owners 归因语料按 `st.id in subtask_results` 过滤 → 被弃子任务
      被排除在归因面外 → 缺失符号真 owner 是被弃者时结构性归因不出；
  (b) 空归因回退 owners=全体完成者 + failure.py 契约分支对空 failed 回填
      subtask_results.keys() → 37 个完成态 7ms 内全清零、逐字重演首轮派发。
拍板治本（a+b 守卫同批）：
  ① 语料扩全 plan（含被弃/失败）→ 命中被弃 owner 定向复活（摘出 abandoned 集）；
  ② 双侧单调守卫：verify 出口不回退全员、HANDLE_FAILURE 入口空 failed 不回填全员
     （升级人工携机读账，绝不清完成态）。
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.nodes.failure import _handle_failure_impl as handle_failure  # noqa: E402
from swarm.brain.nodes.verify import _d5_attribute_owners  # noqa: E402
from swarm.types import (  # noqa: E402
    Confidence,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskPlan,
    WorkerOutput,
)


def _sub(sid, desc="", deps=None):
    return SubTask(
        id=sid, description=desc or f"task {sid}",
        difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
        scope=FileScope(writable=[]), depends_on=deps or [],
    )


def _out(sid):
    return WorkerOutput(subtask_id=sid, diff="x", summary="",
                        confidence=Confidence.HIGH, l1_passed=True)


# ── ①a 语料含被弃者：真 owner 被弃时必须归因到它，而非回退完成者 ──
def test_a1_corpus_includes_abandoned_owner():
    plan = TaskPlan(subtasks=[
        _sub("st-done", desc="实现告警 CRUD 页面"),
        _sub("st-gone", desc="实现 IAlarmEngineService 引擎收敛状态机"),
    ], parallel_groups=[["st-done", "st-gone"]])
    results = {"st-done": _out("st-done")}  # st-gone 被弃，不在完成态

    owners, sym_owners, unattributed = _d5_attribute_owners(
        ["IAlarmEngineService"], plan, results)
    assert sym_owners.get("IAlarmEngineService") == ["st-gone"], (
        "被弃 owner 必须可归因（round38c：引擎符号真 owner 全在被弃 14 个里，"
        "旧语料过滤使归因结构性不可能命中）")
    assert owners == ["st-gone"]
    assert unattributed == []


# ── ①b verify 出口守卫：全 plan 归因不出 → owners 空 + 机读 unattributed，绝不全员 ──
def test_a1_no_all_member_fallback():
    plan = TaskPlan(subtasks=[
        _sub("st-a", desc="实现 blacklist 管理页面"),
        _sub("st-b", desc="实现 IUserService.list 接口"),
    ], parallel_groups=[["st-a", "st-b"]])
    results = {"st-a": _out("st-a"), "st-b": _out("st-b")}

    owners, sym_owners, unattributed = _d5_attribute_owners(
        ["IWholeNewService"], plan, results)
    assert owners == [], (
        "归因不出绝不回退全员——round38c 正是这条回退把 37 个完成态全列 owner 清零；"
        "无主符号走机读账升级，不走破坏性重跑")
    assert unattributed == ["IWholeNewService"]
    assert sym_owners == {}


# ── ②a HANDLE_FAILURE 入口守卫：空 failed 绝不回填全员，升级人工且完成态原样保留 ──
def test_a1_handle_failure_empty_failed_escalates_not_refill():
    state = {
        "verification_failure": "contract",
        "failed_subtask_ids": [],  # verify 归因空 → 显式空清单
        "subtask_results": {"st-1": _out("st-1"), "st-2": _out("st-2")},
        "dispatch_remaining": [],
        "l2_details": {"contract_unattributed": ["IWholeNewService"]},
    }
    out = asyncio.run(handle_failure(state))
    assert out.get("failure_strategy") == "escalate", (
        "空归因必须升级人工（诚实 PARTIAL 路径），绝不确定性全员重跑")
    assert out.get("failure_escalated") is True
    # 完成态单调：不得 pop 任何 subtask_results
    emitted = out.get("subtask_results")
    if emitted is not None:
        assert "st-1" in emitted and "st-2" in emitted, "完成态不得被清"
    # 不得把全员回填进重派队列
    assert not out.get("dispatch_remaining"), "空归因不得把任何子任务回填重派队列"


# ── ②b 定向复活：归因命中被弃 owner → 摘出 abandoned 集 + 入重派队列，兄弟完成态保留 ──
def test_a1_revive_abandoned_owner():
    state = {
        "verification_failure": "contract",
        "failed_subtask_ids": ["st-gone"],
        "subtask_results": {"st-done": _out("st-done")},
        "dispatch_remaining": [],
        "abandoned_subtask_ids": ["st-gone", "st-other"],
    }
    out = asyncio.run(handle_failure(state))
    assert out.get("failure_strategy") == "retry"
    assert "st-gone" in out.get("dispatch_remaining", []), "被弃 owner 应入重派队列"
    assert out.get("abandoned_subtask_ids") == ["st-other"], (
        "复活必须同步摘出 abandoned 集——dispatch.py 派发面过滤 abandoned_subtask_ids，"
        "不摘除则复活者永远派不出去")
    assert "st-done" in out.get("subtask_results", {}), "成功兄弟不得被清"


# ── ②c 复活补强（对抗复核#4）：give_up 打桩 owner 同样可复活（派发面过滤双集）──
def test_a1_revive_give_up_owner():
    state = {
        "verification_failure": "contract",
        "failed_subtask_ids": ["st-stub"],
        "subtask_results": {"st-done": _out("st-done")},
        "dispatch_remaining": [],
        "give_up_isolated_ids": ["st-stub", "st-keep"],
    }
    out = asyncio.run(handle_failure(state))
    assert "st-stub" in out.get("dispatch_remaining", [])
    assert out.get("give_up_isolated_ids") == ["st-keep"], (
        "阶梯三打桩 owner 复活必须摘出 give_up_isolated_ids——dispatch 过滤"
        "abandoned|give_up 双集，只摘 abandoned 则打桩 owner 复活=空转白烧编译轮")


# ── ②d 复活补强（对抗复核#4）：传递上游同弃者一并复活，否则依赖永不就绪 ──
def test_a1_revive_transitive_upstream():
    plan = TaskPlan(subtasks=[
        _sub("st-up", desc="上游 VO"),
        _sub("st-owner", desc="实现 IAlarmEngineService", deps=["st-up"]),
    ], parallel_groups=[["st-up"], ["st-owner"]])
    state = {
        "verification_failure": "contract",
        "failed_subtask_ids": ["st-owner"],
        "subtask_results": {"st-done": _out("st-done")},
        "dispatch_remaining": [],
        "plan": plan,
        "abandoned_subtask_ids": ["st-owner", "st-up", "st-other"],
    }
    out = asyncio.run(handle_failure(state))
    assert out.get("abandoned_subtask_ids") == ["st-other"], (
        "复活必须按传递闭包摘出上游同弃者——owner 上游仍被弃则依赖永不就绪，"
        "复活退化为 #R13-4 熔断前的有界空转（每轮 600s 编译白烧）")
    rem = out.get("dispatch_remaining", [])
    assert "st-owner" in rem and "st-up" in rem


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("A1 全部通过")
