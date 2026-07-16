"""R65C 治本锁：round65c 执行期两大定案的回归锁。

#52 双毒株（权威 pom 模板通道）：
  (a) 既有模块 pom 收到「原样写入」最小模板 → worker 服从清空基线依赖（common 丢
      poi/framework 丢 web、aop starter，346 行编译错，换模型重试必同果）。
      治=owner 通道与主入口同律：完整模板只给 CREATE，既有 pom 只给缺失依赖片段+并入措辞。
  (b) dedupe_module_scaffolds 的 [MERGED-DUP] 注记（含 dup 的模板围栏）漏剥进 worker
      提示 → trivial 快路径把注记原样写进 ruoyi-alarm/pom.xml 致 XML 非法。
      治=源头剥围栏 + worker 出口单一剥离（strip_machine_annotations）。

#53 replan 吞 pending（104/107 被弃 → 假«全部完成»，L2 拦下假交付）：
  修① 桩完成的 give-up ≠ 死上游（settled-with-product 排除出不可满足集）；
  修③ 连坐规模闸（>max(10, 25%计划) → escalate 人工，绝不静默计划覆灭）；
  修④ remaining==0 且有放弃 → 诚实 PARTIAL 标签，绝不谎报全部完成；
  修⑤ MONITOR 三本账统一口径（已完成只算 L1 过 + 放弃可见）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from swarm.brain.contract_utils import (
    MERGED_DUP_DELIM,
    _inject_templates_into_pom_owners,
    dedupe_module_scaffolds,
)
from swarm.types import (
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
    WorkerOutput,
)


def _st(sid, desc="", writable=None, create=None, depends=None):
    return SubTask(id=sid, description=desc or f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   depends_on=depends or [],
                   scope=FileScope(writable=writable or [], create_files=create or []))


# ═════════════ #52 (a)：owner 通道 CREATE-only 闸 ═════════════

_ROOT_POM = """<project><modelVersion>4.0.0</modelVersion>
<groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId><version>4.7.8</version>
<packaging>pom</packaging><modules><module>mod-a</module></modules></project>"""

_BASELINE_POM = """<project><modelVersion>4.0.0</modelVersion>
<parent><groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId><version>4.7.8</version></parent>
<artifactId>mod-a</artifactId>
<dependencies><dependency><groupId>org.apache.poi</groupId><artifactId>poi-ooxml</artifactId></dependency></dependencies>
</project>"""


def _owner_plan():
    plan = TaskPlan(subtasks=[
        _st("st-owner", writable=["mod-a/pom.xml", "mod-a/src/main/java/A.java"]),
    ])
    plan.shared_contract = {"dependencies": [
        {"module": "mod-a", "artifacts": ["org.projectlombok:lombok:1.18.30"]},
    ]}
    return plan


def test_owner_existing_pom_gets_merge_snippet_not_full_rewrite(tmp_path):
    """既有模块 pom 的 owner 绝不拿「原样写入」全量模板——只拿缺失依赖片段+并入措辞
    （round65c 毒株(a)：全量模板清空基线依赖）。"""
    (tmp_path / "pom.xml").write_text(_ROOT_POM)
    (tmp_path / "mod-a").mkdir()
    (tmp_path / "mod-a" / "pom.xml").write_text(_BASELINE_POM)
    plan = _owner_plan()
    touched = _inject_templates_into_pom_owners(plan, str(tmp_path))
    desc = plan.subtasks[0].description
    assert touched == ["st-owner"]
    assert "缺失依赖片段" in desc and "并入" in desc, desc
    assert "绝不整体替换" in desc
    assert "原样写入" not in desc, "既有 pom 绝不许出现原样写入指令"
    assert "<project" not in desc, "既有 pom 的片段不得携带整份 <project> 模板"


def test_owner_new_pom_still_gets_full_template(tmp_path):
    """新建模块 pom 的 owner 保持全量权威模板（R58-3 原语义回归锁）。"""
    (tmp_path / "pom.xml").write_text(_ROOT_POM)
    plan = _owner_plan()  # mod-a/pom.xml 不在磁盘上
    touched = _inject_templates_into_pom_owners(plan, str(tmp_path))
    desc = plan.subtasks[0].description
    assert touched == ["st-owner"]
    assert "权威 pom 模板" in desc and "原样写入" in desc, desc


# ═════════════ #52 (b)：MERGED-DUP 注记双层防泄 ═════════════

def test_dedupe_merged_annotation_carries_no_template_fence():
    """dup 描述里的 ```xml 模板围栏绝不随注记并入 canon——canon 自有权威模板，
    双模板+注记文本会被 trivial 快路径原样写进文件。"""
    canon = _st("st-s1", desc="【构建脚手架】创建 mod-x/pom.xml\n```xml\n<project>A</project>\n```",
                create=["mod-x/pom.xml"])
    dup = _st("st-s2", desc="重复脚手架语义说明\n```xml\n<project>B</project>\n```",
              create=["mod-x/pom.xml"])
    plan = TaskPlan(subtasks=[canon, dup])
    merged = dedupe_module_scaffolds(plan)
    assert merged == 1
    d = plan.subtasks[0].description
    assert MERGED_DUP_DELIM in d, "注记定界符仍在（签名剥离锚点勿丢）"
    tail = d.split(MERGED_DUP_DELIM, 1)[1]
    assert "```" not in tail, f"注记段携带模板围栏=毒株(b)复发: {tail!r}"
    assert "重复脚手架语义说明" in tail, "围栏前的语义文本应保留"


def test_worker_prompt_strips_machine_annotations():
    """worker 出口单一剥离：[MERGED-DUP] 注记段绝不进提示（prompts 与 trivial 快路径共用）。"""
    from swarm.worker.prompts import build_worker_prompt, strip_machine_annotations
    desc = "写 mod-x/pom.xml\n```xml\n<project>A</project>\n```"
    dirty = desc + f"{MERGED_DUP_DELIM}dup 语义"
    assert strip_machine_annotations(dirty) == desc
    st = _st("st-p", desc=dirty, create=["mod-x/pom.xml"])
    prompt = build_worker_prompt(st)
    assert "[MERGED-DUP]" not in prompt, "注记泄漏进 worker 提示"
    assert "写 mod-x/pom.xml" in prompt


# ═════════════ #53 修①+修③：不可满足判官与连坐规模闸 ═════════════

def _wo(sid, ok, details=None):
    return WorkerOutput(
        subtask_id=sid,
        diff="--- a/X\n+++ b/X\n@@ -1 +1,2 @@\n a\n+b\n" if ok else "",
        summary="", l1_passed=ok, l1_details=details or {},
        confidence="high" if ok else "low",
    )


_BLOCKED_DETAILS = {
    "pipeline_blocked": "internal_pkg_not_built",
    "not_run_kind": "blocked",
    "blocked_on_packages": [],
    "blocked_on_modules": [],
}


def _closure_state(*, stub_completed: bool, n_dependents: int,
                   give_up_mode: str = "stub"):
    """P(give-up 上游) ← C(失败消费者) ← D1..Dn(pending 依赖闭包)。"""
    subs = [
        _st("P", create=["alarm-interface/pom.xml"]),
        _st("C", writable=["mod/src/main/java/C.java"], depends=["P"]),
    ] + [_st(f"D{i}", writable=[f"mod/src/main/java/D{i}.java"], depends=["C"])
         for i in range(n_dependents)]
    results = {"C": _wo("C", False, dict(_BLOCKED_DETAILS))}
    if stub_completed:
        # 阶梯三占位结果：stub=settled-with-product / revert=占位但产物已剥离
        results["P"] = _wo("P", True,
                           {"given_up": True, "give_up_mode": give_up_mode})
    return {
        "plan": TaskPlan(subtasks=subs),
        "failed_subtask_ids": ["C"],
        "subtask_results": results,
        "give_up_isolated_ids": ["P"],
        "abandoned_subtask_ids": [],
        "dispatch_remaining": [f"D{i}" for i in range(n_dependents)],
        "subtask_retry_counts": {},
        "project_id": "",
    }


def _run_failure(state, strategy="retry"):
    from swarm.brain.nodes import handle_failure

    async def _fake_invoke(self, msgs):
        class R:
            content = '{"strategy": "%s", "reasoning": "x"}' % strategy
        return R()

    with patch("swarm.brain.nodes._get_brain_llm") as mock_llm:
        inst = mock_llm.return_value
        inst.ainvoke = _fake_invoke.__get__(inst)
        return asyncio.run(handle_failure(state))


def test_stub_completed_giveup_is_not_dead_upstream():
    """修①：P 打了 l1_passed 桩（settled-with-product）→ C 绝不被判死上游连坐
    （round65c：0.8s BLOCKED 探针 → 102/107 被弃的引爆点）。"""
    state = _closure_state(stub_completed=True, n_dependents=40)
    out = _run_failure(state)
    assert out.get("failure_strategy") != "abandon", \
        f"桩完成上游不得触发连坐: {out.get('failure_strategy')}"
    assert not out.get("abandoned_subtask_ids"), out.get("abandoned_subtask_ids")


def test_revert_giveup_is_still_dead_upstream():
    """修①对照（复核 CRITICAL 锁）：revert 模式 give-up 同样写 l1_passed 占位但
    产物已剥离=真死上游——豁免绝不适用（round12/13 连坐语义原样保留）。"""
    state = _closure_state(stub_completed=True, n_dependents=3, give_up_mode="revert")
    out = _run_failure(state)
    assert out.get("failure_strategy") == "abandon", \
        f"revert 模式必须照旧连坐: {out.get('failure_strategy')}"


def test_mass_abandon_gate_escalates_instead_of_silent_wipe():
    """修③：真死上游触发的连坐闭包超过计划 25% → escalate 人工决策，
    pending 工作集绝不静默大额缩水。"""
    state = _closure_state(stub_completed=False, n_dependents=40)  # 闭包 41/42 >> 25%
    out = _run_failure(state)
    assert out.get("failure_strategy") == "escalate", out.get("failure_strategy")
    assert out.get("failure_escalated") is True
    assert not out.get("abandoned_subtask_ids"), "escalate 路径不得同时放弃"
    assert any("mass_abandon_gate" in r for r in (out.get("degraded_reasons") or []))


def test_small_closure_abandon_still_allowed():
    """修③对照：小闭包（≤阈值）的合法剪枝照常放弃——闸门不过度阻断。"""
    state = _closure_state(stub_completed=False, n_dependents=3)
    # 计划 5 个：cap = max(10, 1) = 10 ≥ 闭包 5 → 放行
    out = _run_failure(state)
    assert out.get("failure_strategy") == "abandon", out.get("failure_strategy")
    assert out.get("abandoned_subtask_ids")


# ═════════════ #53 修④+修⑤：诚实标签与三本账 ═════════════

def test_after_monitor_partial_label_when_abandoned(caplog):
    """修④：remaining==0 且有放弃 → WARNING«PARTIAL 交付»，绝不谎报全部完成。"""
    import logging
    from swarm.brain.graph import after_monitor
    plan = TaskPlan(subtasks=[_st("a"), _st("b"), _st("c")])
    state = {
        "plan": plan,
        "dispatch_remaining": [],
        "failed_subtask_ids": [],
        "subtask_results": {"a": _wo("a", True)},
        "abandoned_subtask_ids": ["b", "c"],
        "give_up_isolated_ids": [],
    }
    with caplog.at_level(logging.INFO, logger="swarm.brain.graph"):
        route = after_monitor(state)
    assert route == "merge"
    assert any("PARTIAL 交付" in r.message for r in caplog.records), \
        [r.message for r in caplog.records]
    assert not any("(全部完成)" in r.message for r in caplog.records), "放弃>0 时不得谎报全部完成"


def test_monitor_counts_l1_passed_and_abandoned(caplog):
    """修⑤：MONITOR 行只把 L1 通过计入已完成，且放弃数可见（三本账）。"""
    import logging
    from swarm.brain.nodes.dispatch import monitor
    state = {
        "dispatch_remaining": ["x"],
        "subtask_results": {"a": _wo("a", True), "bad": _wo("bad", False)},
        "failed_subtask_ids": [],
        "abandoned_subtask_ids": ["z1", "z2"],
        "give_up_isolated_ids": [],
    }
    with caplog.at_level(logging.INFO, logger="swarm.brain.nodes.dispatch"):
        monitor(state)
    line = next(r.message for r in caplog.records if "[MONITOR]" in r.message)
    assert "已完成(L1过)=1" in line, line
    assert "放弃=2" in line, line
