#!/usr/bin/env python3
"""主题B 批一（round38c）—— B1 完成态产物全集注入 + B2 BLOCKED 失败指纹短路。

取证（forensics_B1B2_code.md）：
  B1：同步清单推导 4 处全是声明驱动，越包/跨父上游产物结构性漏传（st-13-2 八轮缺
      VO / st-3 链 bootstrap 漏传）；upstream_artifacts 跨父恒空使 seed 闸全盲。
  B2：BLOCKED 重派唯一变异是 retry_guidance 散文，同输入白跑 1+3+3 整条阶梯
      （st-3-1 七轮逐字相同签名）。
治本：
  B1=dispatch 派发时把全体 L1 通过子任务的产物清单（ADDED/MODIFIED，排除删除态防
     seed 闸假 BLOCKED；排除被弃半成品防播毒）注入 scope.upstream_artifacts
     （不进 readable：prompt 全量渲染会撑爆）；worker 补传/模块树消费端同批扩源。
  B2=(pipeline_blocked+blocked_on_*) 失败指纹：二连不变跳过 transient 退避直落
     capability 阶梯，三连不变升级人工——确定性构建闸的同输入重试必然同结果。
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import swarm.brain.nodes as nodes  # noqa: E402
from swarm.brain.nodes.dispatch import _inject_upstream_products  # noqa: E402
from swarm.types import (  # noqa: E402
    Confidence,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
    WorkerOutput,
)


def _st(sid, writable=None, create=None):
    return SubTask(id=sid, description=f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=writable or [], create_files=create or []))


def _out(sid, diff="", l1=True):
    return WorkerOutput(subtask_id=sid, diff=diff, summary="",
                        confidence=Confidence.HIGH, l1_passed=l1)


_DIFF_UPSTREAM = (
    "diff --git a/mod-a/src/main/java/com/x/vo/TwoFactorBindVO.java "
    "b/mod-a/src/main/java/com/x/vo/TwoFactorBindVO.java\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/mod-a/src/main/java/com/x/vo/TwoFactorBindVO.java\n"
    "@@ -0,0 +1 @@\n+public class TwoFactorBindVO {}\n"
    "diff --git a/mod-a/pom.xml b/mod-a/pom.xml\n"
    "--- a/mod-a/pom.xml\n"
    "+++ b/mod-a/pom.xml\n"
    "@@ -1 +1 @@\n-<old/>\n+<new/>\n"
    "diff --git a/mod-a/src/Old.java b/mod-a/src/Old.java\n"
    "--- a/mod-a/src/Old.java\n"
    "+++ /dev/null\n"
    "@@ -1 +0,0 @@\n-class Old {}\n"
)


# ── B1①：完成态产物（ADDED/MODIFIED）注入 upstream_artifacts；删除态/被弃者/自有文件排除 ──
def test_b1_injection_completed_products_only():
    down = _st("st-down", writable=["mod-b/src/Impl.java"], create=["mod-b/src/New.java"])
    results = {
        "st-up": _out("st-up", diff=_DIFF_UPSTREAM, l1=True),
        "st-bad": _out("st-bad", diff="--- a/poison.java\n+++ b/poison.java\n", l1=False),
    }
    _inject_upstream_products([down], results)
    ua = down.scope.upstream_artifacts
    assert "mod-a/src/main/java/com/x/vo/TwoFactorBindVO.java" in ua, (
        "越包/跨父上游产物必须进 upstream_artifacts（st-13-2 八轮缺 VO 的治本面）")
    assert "mod-a/pom.xml" in ua
    assert "mod-a/src/Old.java" not in ua, "删除态入清单会让 seed 闸误判 missing→假 BLOCKED"
    assert "poison.java" not in ua, "非 L1 通过（被弃半成品）绝不播毒"
    assert "mod-b/src/Impl.java" not in ua and "mod-b/src/New.java" not in ua, "自有文件不注入"


# ── B1②：幂等——重复注入不重复堆积 ──
def test_b1_injection_idempotent():
    down = _st("st-down")
    results = {"st-up": _out("st-up", diff=_DIFF_UPSTREAM)}
    _inject_upstream_products([down], results)
    first = list(down.scope.upstream_artifacts)
    _inject_upstream_products([down], results)
    assert down.scope.upstream_artifacts == first, "重复派发注入必须幂等去重"


# ── B1③：_module_source_files 收集面覆盖 upstream_artifacts 所在模块 ──
def test_b1_module_source_includes_upstream_module(tmp_path):
    (tmp_path / "m1" / "src").mkdir(parents=True)
    (tmp_path / "m1" / "pom.xml").write_text("<project/>", encoding="utf-8")
    (tmp_path / "m1" / "src" / "A.java").write_text("class A {}", encoding="utf-8")
    from swarm.worker.executor_sync import _SandboxSyncMixin
    ns = SimpleNamespace(
        project_path=str(tmp_path),
        subtask=SimpleNamespace(harness=SimpleNamespace(build_command="mvn -q compile")),
        effective_scope=SimpleNamespace(
            writable=[], create_files=[], readable=[],
            upstream_artifacts=["m1/src/A.java"]),
    )
    out = _SandboxSyncMixin._module_source_files(ns)
    assert "m1/src/A.java" in out, (
        "上游产物所在模块必须纳入模块树收集（越模块上游 VO 此前收不到）")


# ── B2：失败指纹 ──

class _FakeResp:
    def __init__(self, content):
        self.content = content


def _fake_llm_retry():
    class _L:
        async def ainvoke(self, _msgs):
            return _FakeResp('{"strategy":"retry","reasoning":"r"}')
    return lambda: _L()


def _blocked_out(sid):
    return WorkerOutput(
        subtask_id=sid, diff="", summary="上游产物缺失判 BLOCKED",
        l1_passed=False,
        l1_details={
            "pipeline_blocked": "upstream_module_broken",
            "blocked_on_files": ["mod-a/src/main/java/com/x/vo/TwoFactorBindVO.java"],
            "blocked_on_modules": ["mod-a"],
            "failure_class": "transient",
        },
    )


def _blocked_state(sig_count=None):
    state = {
        "plan": None,  # 跳过内部阻断分支的 plan 依赖逻辑，直测 transient 快路指纹
        "failed_subtask_ids": ["st-1"],
        "subtask_results": {"st-1": _blocked_out("st-1")},
        "subtask_retry_counts": {},
        "dispatch_remaining": [],
        "degraded_reasons": [],
    }
    if sig_count is not None:
        sig = ("upstream_module_broken|"
               "mod-a,mod-a/src/main/java/com/x/vo/TwoFactorBindVO.java")
        state["subtask_block_signatures"] = {"st-1": {"sig": sig, "count": sig_count}}
    return state


def test_b2_sig_first_seen_walks_transient_and_records():
    with patch.object(nodes, "_get_brain_llm", _fake_llm_retry()):
        out = asyncio.run(nodes.handle_failure(_blocked_state()))
    assert out.get("subtask_transient_counts", {}).get("st-1") == 1, "首见照走 transient 退避"
    rec = out.get("subtask_block_signatures", {}).get("st-1") or {}
    assert rec.get("count") == 1 and "upstream_module_broken|" in (rec.get("sig") or ""), (
        "指纹必须随 transient 出口持久化（跨轮计连击）")


def test_b2_sig_second_repeat_skips_transient_ladder_instead():
    with patch.object(nodes, "_get_brain_llm", _fake_llm_retry()):
        out = asyncio.run(nodes.handle_failure(_blocked_state(sig_count=1)))
    assert "subtask_transient_counts" not in out, (
        "同签名二连：transient 退避对同输入无意义，必须跳过（不 sleep 不烧 transient 表）")
    assert out.get("subtask_retry_counts", {}).get("st-1") == 1, "直落 capability 阶梯"
    assert out.get("subtask_block_signatures", {}).get("st-1", {}).get("count") == 2


def test_b2_sig_third_repeat_escalates():
    with patch.object(nodes, "_get_brain_llm", _fake_llm_retry()):
        out = asyncio.run(nodes.handle_failure(_blocked_state(sig_count=2)))
    assert out.get("failure_strategy") == "escalate", (
        "同签名三连（transient+capability 均对同输入白跑过）必须升级人工，"
        "绝不 1+3+3 七轮逐字重演（round38c st-3-1 实证）")
    assert out.get("failure_escalated") is True


# ── 对抗复核 CONFIRMED#2/#3：跨 diff 终态归并 + 本地存在性过滤（防假 BLOCKED 死刑链）──
def test_b1_injection_terminal_merge_and_existence_filter(tmp_path):
    (tmp_path / "mod-a").mkdir()
    (tmp_path / "mod-a" / "Kept.java").write_text("class Kept {}", encoding="utf-8")
    # Gone.java 故意不落盘（模拟 merge 期清理/pull-back 未物化）
    diff_create = ("--- /dev/null\n+++ b/mod-a/Kept.java\n@@ -0,0 +1 @@\n+class Kept {}\n"
                   "--- /dev/null\n+++ b/mod-a/Gone.java\n@@ -0,0 +1 @@\n+class Gone {}\n"
                   "--- /dev/null\n+++ b/mod-a/Dead.java\n@@ -0,0 +1 @@\n+class Dead {}\n")
    diff_delete = "--- a/mod-a/Dead.java\n+++ /dev/null\n@@ -1 +0,0 @@\n-class Dead {}\n"
    (tmp_path / "mod-a" / "Dead.java").write_text("class Dead {}", encoding="utf-8")
    down = _st("st-down")
    results = {
        "st-a": _out("st-a", diff=diff_create),
        "st-b": _out("st-b", diff=diff_delete),
    }
    changed = _inject_upstream_products([down], results, str(tmp_path))
    ua = down.scope.upstream_artifacts
    assert changed is True
    assert "mod-a/Kept.java" in ua
    assert "mod-a/Dead.java" not in ua, (
        "跨 diff 终态归并：后续完成者删除的路径必须从全集剔除——单 diff 内跳过 DELETED "
        "不够（st-A 创建 st-B 删除时 A 的贡献残留 → 全体待派子任务假 BLOCKED → 与 B2 "
        "指纹合谋成任务死刑，对抗复核 repro CONFIRMED）")
    assert "mod-a/Gone.java" not in ua, (
        "本地树不存在的陈旧路径绝不进 seed 闸（注入源语义=已落盘完成态产物）")


# ── 对抗复核 4b：三连不变优先部分交付（有完成产物时 abandon+PARTIAL，非 escalate 硬化）──
def test_b2_third_strike_prefers_partial_over_escalate():
    plan = TaskPlan(subtasks=[_st("st-ok"), _st("st-1")],
                    parallel_groups=[["st-ok", "st-1"]])
    blocked = WorkerOutput(
        subtask_id="st-1", diff="", summary="blocked", l1_passed=False,
        l1_details={"pipeline_blocked": "sandbox_env_probe_blocked",
                    "blocked_on_files": ["m/f.java"], "failure_class": "transient"})
    sig = "sandbox_env_probe_blocked|m/f.java"
    state = {
        "plan": plan,
        "failed_subtask_ids": ["st-1"],
        "subtask_results": {"st-ok": _out("st-ok"), "st-1": blocked},
        "subtask_retry_counts": {},
        "dispatch_remaining": [],
        "degraded_reasons": [],
        "subtask_block_signatures": {"st-1": {"sig": sig, "count": 2}},
    }
    with patch.object(nodes, "_get_brain_llm", _fake_llm_retry()):
        out = asyncio.run(nodes.handle_failure(state))
    assert out.get("failure_strategy") == "abandon", (
        "三连不变且有完成产物 → 部分交付出口（abandon+PARTIAL）优先于 escalate 硬化"
        "（对抗复核 4b）")
    assert "st-1" in out.get("abandoned_subtask_ids", [])


# ── 对抗复核#7：replan 剪枝覆盖两张新表（旧指纹/旧配额绝不粘滞给语义新子任务）──
def test_lifecycle_replan_prunes_new_tables():
    from swarm.brain.nodes import _surgical_replan_reset
    old_plan = TaskPlan(subtasks=[_st("st-1", writable=["a.java"])],
                        parallel_groups=[["st-1"]])
    new_plan = TaskPlan(subtasks=[_st("st-1", writable=["b.java"])],  # 同 id 不同 scope=语义新
                        parallel_groups=[["st-1"]])
    out = _surgical_replan_reset(
        {}, old_plan, new_plan,
        old_block_signatures={"st-1": {"sig": "x|y", "count": 2}},
        old_scope_amend_counts={"st-1": 1},
    )
    assert out.get("subtask_block_signatures") == {}, (
        "签名变=语义新子任务：旧指纹 count=2 粘滞会让新计划首个 BLOCKED 直接三连终局")
    assert out.get("subtask_scope_amend_counts") == {}, "旧 amend 配额粘滞会让新子任务永拒外科修正"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("B批一 全部通过")
