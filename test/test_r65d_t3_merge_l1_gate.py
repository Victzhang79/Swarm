"""R65D-T3 交付面闸：MERGE 只收 L1 通过的产物——L1-fail 绝不合入交付树。

round65d 实锤（task b583df8f 三路交叉印证·C 路毒树四株）：10:59:02 被冻成"完成态"的
st-26/st-30-1/st-31（全 L1-fail）滞留 subtask_results，终局 MERGE(4 diff/17801 chars)
照单全收 → PARTIAL 交付树带毒：SCOPE_OBJECTION 拒工书 Map.java 当源码落盘 /
SysLoginController 编译必死 typo / LoginService 无依赖 import / 8 控制器 import
不存在模块。round47 教训：毒残留会被下一轮读树取证机制当权威复制回去。

治本（与 #60 配对：冻结是因【处置层已治】，合入是果【本闸 fail-closed】）：
- merge 节点在组装 subtask_diffs 时剔除 l1_passed=False 的输出（单一事实源
  shared.l1_passed，give-up 桩/revert 的 l1_passed=True 不受影响）；
- 被剔 sid 并入 abandoned + pop 出 subtask_results（D7 孤儿剔除同口径，终态诚实
  PARTIAL）+ ERROR 响亮 + degraded_reasons merge_rejected_l1_fail:<sid> 机读留痕；
- 足迹复核：被剔 diff 的文件清单入日志（毒株落点可审计，round47 面）。
"""
from __future__ import annotations

from swarm.brain import nodes
from swarm.types import WorkerOutput

DIFF_GOOD = (
    "--- /dev/null\n+++ b/mod/src/main/java/com/x/Good.java\n"
    "@@ -0,0 +1,2 @@\n+package com.x;\n+public class Good {}\n"
)
DIFF_POISON = (
    "--- /dev/null\n+++ b/mod/src/main/java/com/x/Map.java\n"
    "@@ -0,0 +1,1 @@\n+SCOPE_OBJECTION: 该类型属 java.util，拒绝新建\n"
)


def _state(results):
    return {"subtask_results": results, "rebase_subtask_ids": [],
            "merge_conflicts": [], "failed_subtask_ids": []}


def test_merge_rejects_l1_fail_output():
    """★毒树拦截★：L1-fail 的产物绝不进 merged_diff；被剔 sid 并入 abandoned、
    pop 出完成态、degraded_reasons 机读留痕。"""
    state = _state({
        "st-ok": WorkerOutput(subtask_id="st-ok", diff=DIFF_GOOD, summary="",
                              l1_passed=True),
        "st-poison": WorkerOutput(subtask_id="st-poison", diff=DIFF_POISON,
                                  summary="", l1_passed=False,
                                  l1_details={"verify_failed": "grep ..."}),
    })
    out = nodes.merge(state)
    assert "Good.java" in out["merged_diff"], "L1 通过的产物照常交付"
    assert "SCOPE_OBJECTION" not in out["merged_diff"], \
        "L1-fail 毒株绝不合入交付树（round65d Map.java 拒工书当源码落盘）"
    assert "st-poison" in set(out.get("abandoned_subtask_ids") or []), \
        "被剔 sid 必须并入 abandoned（终态诚实 PARTIAL，D7 同口径）"
    assert "st-poison" not in (out.get("subtask_results")
                               if "subtask_results" in out
                               else state["subtask_results"]), \
        "被剔 sid 必须 pop 出完成态（不再算 DONE）"
    assert any(str(d) == "merge_rejected_l1_fail:st-poison"
               for d in (out.get("degraded_reasons") or [])), \
        f"必须机读留痕: {out.get('degraded_reasons')}"


def test_merge_keeps_giveup_stub_output():
    """对照面：阶梯三桩（give_up_mode=stub，l1_passed=True）是合法交付物，照常合入。"""
    stub_diff = ("--- /dev/null\n+++ b/mod/src/main/java/com/x/Stub.java\n"
                 "@@ -0,0 +1,2 @@\n+package com.x;\n+public class Stub {}\n")
    state = _state({
        "st-stub": WorkerOutput(subtask_id="st-stub", diff=stub_diff, summary="",
                                l1_passed=True,
                                l1_details={"give_up_mode": "stub"}),
    })
    out = nodes.merge(state)
    assert "Stub.java" in out["merged_diff"], "桩产物是合法交付物"
    assert not (out.get("degraded_reasons") or []), "桩绝不误剔"


def test_merge_all_rejected_escalates_not_clean_empty():
    """★猎手 HIGH 锁★：全员被剔=空交付，绝不当干净合并放行（merge_diffs([]) 会判
    success、COMPLEX 路径确定性检查全跳、裸 LLM 可给空 diff 盖章）→ escalate 人工。"""
    state = _state({
        "st-p1": WorkerOutput(subtask_id="st-p1", diff=DIFF_POISON, summary="",
                              l1_passed=False),
        "st-p2": WorkerOutput(subtask_id="st-p2", diff=DIFF_GOOD, summary="",
                              l1_passed=False),
    })
    out = nodes.merge(state)
    assert out.get("failure_escalated") is True, \
        f"全员被剔必须 escalate（绝不称全部完成）: {out.get('failure_escalated')}"
    assert out.get("failure_strategy") == "escalate"
    assert out.get("verification_failure") == "merge_l1_reject_mass"
    assert out.get("l2_passed") is False
    assert "SCOPE_OBJECTION" not in out["merged_diff"]


def test_merge_small_reject_with_survivors_no_escalate():
    """对照面：少量剔除且有幸存产物（阈值内）→ 照常交付幸存者，不 escalate
    （诚实 PARTIAL 由 abandoned 语义承担）。"""
    state = _state({
        "st-ok": WorkerOutput(subtask_id="st-ok", diff=DIFF_GOOD, summary="",
                              l1_passed=True),
        "st-poison": WorkerOutput(subtask_id="st-poison", diff=DIFF_POISON,
                                  summary="", l1_passed=False),
    })
    out = nodes.merge(state)
    assert out.get("failure_escalated") is False, \
        "阈值内剔除不升级（幸存产物照常交付）"
    assert "Good.java" in out["merged_diff"]


def test_merge_all_pass_behavior_unchanged():
    """对照面：全员 L1 通过时行为零变化（无 degraded、无 abandoned 注入）。"""
    state = _state({
        "st-a": WorkerOutput(subtask_id="st-a", diff=DIFF_GOOD, summary="",
                             l1_passed=True),
    })
    out = nodes.merge(state)
    assert "Good.java" in out["merged_diff"]
    assert not any(str(d).startswith("merge_rejected_l1_fail")
                   for d in (out.get("degraded_reasons") or []))
    assert "st-a" not in set(out.get("abandoned_subtask_ids") or [])
