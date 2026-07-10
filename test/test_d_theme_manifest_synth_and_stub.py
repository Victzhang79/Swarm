#!/usr/bin/env python3
"""主题D（round38c）—— D1 聚合清单确定性合成 + D2 跨批串链 + D3b stub 指纹红线。

取证（forensics_D_theme_code.md + forensics_C_deliverables.md）：root pom 终态半坏
（ruoyi-alarm 66 文件主模块未注册=死代码）——温和出口丢清单加性变更、"post-pass
reconcile 兜底"只在 learn_success 兑现（任务死在中途即落空）且 merged_diff 本体
永不被修补；_serialize_pom_writers 只串本批 granted=批间零依赖边竞写；worker 自产
stub（三方法全 throw"TODO: 子任务未完成"）与阶梯三桩在 diff 不可区分、无闸。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.manifest_synth import (  # noqa: E402
    _apply_section_to_text,
    fold_module_registrations,
)
from swarm.types import (  # noqa: E402
    Confidence,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
    WorkerOutput,
)

_BASE_ROOT_POM = (
    "<project>\n"
    "    <groupId>com.x</groupId>\n"
    "    <modules>\n"
    "        <module>existing-mod</module>\n"
    "    </modules>\n"
    "</project>\n"
)

_NEW_MODULE_SECTION = (
    "diff --git a/ruoyi-alarm/pom.xml b/ruoyi-alarm/pom.xml\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/ruoyi-alarm/pom.xml\n"
    "@@ -0,0 +1,4 @@\n"
    "+<project>\n"
    "+    <parent><groupId>com.x</groupId></parent>\n"
    "+    <artifactId>ruoyi-alarm</artifactId>\n"
    "+</project>\n"
)


def _st(sid, writable=None, create=None):
    return SubTask(id=sid, description=f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=writable or [], create_files=create or []))


# ── D1：新模块 pom 在 diff、根 pom 未注册 → 合成注册段 ──
def test_d1_fold_registers_new_module_no_root_section():
    folded, regs = fold_module_registrations(_NEW_MODULE_SECTION, _BASE_ROOT_POM)
    assert regs == ["ruoyi-alarm"], (
        "diff 内新建模块 pom（带 <parent>）必须被确定性补注册——round38c 66 文件主模块"
        "未注册成死代码的治本面")
    assert "diff --git a/pom.xml b/pom.xml" in folded, "根 pom 未在 diff 时应追加合成段"
    applied = _apply_section_to_text(
        _BASE_ROOT_POM,
        folded[folded.index("diff --git a/pom.xml"):])
    assert applied and "<module>ruoyi-alarm</module>" in applied, "合成段必须可 apply 且注册生效"
    assert "<module>existing-mod</module>" in applied, "既有注册不得破坏"


# ── D1：根 pom 有 diff 段（其它改动）但漏注册 → 段被替换为含注册的全文件 diff ──
def test_d1_fold_amends_existing_root_section():
    root_section = (
        "diff --git a/pom.xml b/pom.xml\n"
        "--- a/pom.xml\n"
        "+++ b/pom.xml\n"
        "@@ -1,6 +1,7 @@\n"
        " <project>\n"
        "     <groupId>com.x</groupId>\n"
        "+    <description>alarm platform</description>\n"
        "     <modules>\n"
        "         <module>existing-mod</module>\n"
        "     </modules>\n"
        " </project>\n"
    )
    folded, regs = fold_module_registrations(root_section + _NEW_MODULE_SECTION, _BASE_ROOT_POM)
    assert regs == ["ruoyi-alarm"]
    applied = _apply_section_to_text(
        _BASE_ROOT_POM,
        folded[folded.index("diff --git a/pom.xml"):folded.index("diff --git a/ruoyi-alarm")])
    assert applied is not None, "替换后的根 pom 段必须仍可干净 apply 到 base"
    assert "<module>ruoyi-alarm</module>" in applied
    assert "<description>alarm platform</description>" in applied, "原有根 pom 改动不得丢失"


# ── D1：已注册/无 <modules> 块 → 不动 ──
def test_d1_fold_noop_cases():
    already = _BASE_ROOT_POM.replace("</modules>",
                                     "        <module>ruoyi-alarm</module>\n    </modules>")
    folded, regs = fold_module_registrations(_NEW_MODULE_SECTION, already)
    assert regs == [] and folded == _NEW_MODULE_SECTION, "已注册不得重复合成"
    no_modules = "<project><groupId>com.x</groupId></project>\n"
    folded2, regs2 = fold_module_registrations(_NEW_MODULE_SECTION, no_modules)
    assert regs2 == [] and folded2 == _NEW_MODULE_SECTION, "无 <modules> 块保守跳过（loud）"


# ── D1：根 pom 段是删除形态 → 整体保守跳过（叠加 modify 段=同文件冲突段必炸） ──
def test_d1_fold_skips_on_root_pom_deletion():
    del_section = (
        "diff --git a/pom.xml b/pom.xml\n"
        "deleted file mode 100644\n"
        "--- a/pom.xml\n"
        "+++ /dev/null\n"
        "@@ -1,6 +0,0 @@\n"
        "-<project>\n-    <groupId>com.x</groupId>\n-    <modules>\n"
        "-        <module>existing-mod</module>\n-    </modules>\n-</project>\n"
    )
    folded, regs = fold_module_registrations(del_section + _NEW_MODULE_SECTION, _BASE_ROOT_POM)
    assert regs == [] and folded == del_section + _NEW_MODULE_SECTION, (
        "根 pom 删除形态必须整体跳过——折叠点在 apply-check 之后，坏合成无再校验直接进交付")


# ── D1：merged_diff 末段无尾换行（merge_engine "\n\n".join 真实形态）→ 追加/替换
# 均不得粘连损坏（复核 CONFIRMED：旗舰场景=根 pom 段被温和出口丢弃走追加路径） ──
def test_d1_fold_no_trailing_newline_merged_diff():
    no_nl = _NEW_MODULE_SECTION.rstrip("\n")  # 模拟 join 末段无尾换行
    folded, regs = fold_module_registrations(no_nl, _BASE_ROOT_POM)
    assert regs == ["ruoyi-alarm"]
    idx = folded.index("diff --git a/pom.xml")
    assert folded[idx - 1] == "\n", (
        "追加段 git 头必须独立成行——merged_diff 本体无尾换行时直接 join 会把"
        "`diff --git a/pom.xml` 粘进前段末行=补丁损坏（且折叠点在 apply-check 后无复检）")
    applied = _apply_section_to_text(_BASE_ROOT_POM, folded[idx:])
    assert applied and "<module>ruoyi-alarm</module>" in applied
    # 同族：根 pom 段本身是无尾换行末段（替换路径）——_sec.patch 不补尾换行则 git
    # apply 报 corrupt patch → 合成沉默失效
    root_section_no_nl = (
        "diff --git a/pom.xml b/pom.xml\n"
        "--- a/pom.xml\n+++ b/pom.xml\n@@ -1,6 +1,7 @@\n"
        " <project>\n     <groupId>com.x</groupId>\n"
        "+    <description>x</description>\n"
        "     <modules>\n         <module>existing-mod</module>\n"
        "     </modules>\n </project>"  # 无尾换行
    )
    folded2, regs2 = fold_module_registrations(
        _NEW_MODULE_SECTION + root_section_no_nl, _BASE_ROOT_POM)
    assert regs2 == ["ruoyi-alarm"], "根 pom 段为无尾换行末段时合成不得沉默失效"


# ── D2：跨批串链——第二批授权也必须与第一批写者建立顺序边 ──
def test_d2_serialize_pom_writers_cross_batch():
    from swarm.brain.nodes.planning_core import _serialize_pom_writers
    plan = TaskPlan(subtasks=[
        _st("st-1", writable=["mod-a/pom.xml"]),  # 第一批已授权（历史）
        _st("st-2", writable=["mod-a/pom.xml"]),  # 本批授权
    ], parallel_groups=[["st-1", "st-2"]])
    _serialize_pom_writers(plan, {"st-2": "mod-a/pom.xml"})  # 只传本批
    st2 = {s.id: s for s in plan.subtasks}["st-2"]
    assert "st-1" in (st2.depends_on or []), (
        "跨批写者必须成链——旧实现只串本批 granted，四批独立授权=批间零依赖边竞写"
        "（round38c 20:23/22:08 rebase 冲突来源）")


# ── D2：无产出放弃者绝不入链（复核 CONFIRMED：_is_ready 对其永不就绪=重派任务
# 被自己刚加的边永久扣死 → 无活跃生产者快失败） ──
def test_d2_abandoned_writer_never_enters_chain():
    from swarm.brain.nodes.planning_core import _serialize_pom_writers
    plan = TaskPlan(subtasks=[
        _st("st-1", writable=["mod-a/pom.xml"]),  # 已放弃（revert 路，无产出）
        _st("st-2", writable=["mod-a/pom.xml"]),  # 本批授权重派
    ], parallel_groups=[["st-1", "st-2"]])
    _serialize_pom_writers(plan, {"st-2": "mod-a/pom.xml"}, exclude_ids={"st-1"})
    st2 = {s.id: s for s in plan.subtasks}["st-2"]
    assert "st-1" not in (st2.depends_on or []), (
        "abandoned/give_up-revert 写者入链会把重派任务永久扣死（_is_ready 恒 False）")
    batch = plan.get_dispatch_batch(completed_ids=set(), dispatch_remaining=["st-2"],
                                    max_concurrent=5, abandoned={"st-1"})
    assert any(t.id == "st-2" for t in batch), "重派任务必须可派发，不被死边扣住"


# ── D3b：stub 指纹——非 give_up owner 定向重派，阶梯三桩豁免 ──
def _stub_diff(path="mod/src/Impl.java"):
    return (f"diff --git a/{path} b/{path}\n"
            f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,2 @@\n"
            "+public class Impl {\n"
            '+    public void f() { throw new UnsupportedOperationException("TODO: 子任务未完成"); }\n')


def test_d3b_stub_fingerprint_targets_worker_fake_impl():
    from swarm.brain.nodes.verify import _stub_fingerprint_owner_ids
    plan = TaskPlan(subtasks=[_st("st-1", create=["mod/src/Impl.java"])],
                    parallel_groups=[["st-1"]])
    # 归因按【diff 行作者】：st-1 自己的 diff 含 stub 新增行
    results = {"st-1": WorkerOutput(subtask_id="st-1", diff=_stub_diff(), summary="",
                                    confidence=Confidence.HIGH, l1_passed=True)}
    owners = _stub_fingerprint_owner_ids({}, _stub_diff(), plan, results)
    assert owners == ["st-1"], (
        "worker 自产假实现（阶梯三桩模板串、作者不在 give_up）必须定向重派——"
        "round38c AppSecretAuthInterceptor 三方法全 throw 被合入交付")


def test_d3b_giveup_stub_exempt():
    from swarm.brain.nodes.verify import _stub_fingerprint_owner_ids
    plan = TaskPlan(subtasks=[_st("st-1", create=["mod/src/Impl.java"])],
                    parallel_groups=[["st-1"]])
    results = {"st-1": WorkerOutput(subtask_id="st-1", diff=_stub_diff(), summary="",
                                    confidence=Confidence.HIGH, l1_passed=True)}
    owners = _stub_fingerprint_owner_ids(
        {"give_up_isolated_ids": ["st-1"]}, _stub_diff(), plan, results)
    assert owners == [], "阶梯三真桩（give_up 集）是诚实 PARTIAL 的设计行为，必须豁免"


def test_d3b_no_scapegoat_on_scope_overlap():
    """复核 CONFIRMED（D3b-③）：give_up 打桩者的 stub 行不得嫁祸共享该文件
    writable 的无辜存活者——它重做也消不掉别人合入的行，会死循环到 escalate。"""
    from swarm.brain.nodes.verify import _stub_fingerprint_owner_ids
    plan = TaskPlan(subtasks=[
        _st("st-giveup", create=["mod/src/Impl.java"]),
        _st("st-alive", writable=["mod/src/Impl.java"]),  # scope 重叠的无辜存活者
    ], parallel_groups=[["st-giveup", "st-alive"]])
    results = {
        # 打桩路 give_up：stub diff 在它名下（l1_passed 桩产出，在 results）
        "st-giveup": WorkerOutput(subtask_id="st-giveup", diff=_stub_diff(), summary="",
                                  confidence=Confidence.HIGH, l1_passed=True),
        # 存活者 diff 干净无 stub 行
        "st-alive": WorkerOutput(subtask_id="st-alive",
                                 diff="diff --git a/mod/src/Other.java b/mod/src/Other.java\n"
                                      "--- /dev/null\n+++ b/mod/src/Other.java\n"
                                      "@@ -0,0 +1,1 @@\n+public class Other {}\n",
                                 summary="", confidence=Confidence.HIGH, l1_passed=True),
    }
    owners = _stub_fingerprint_owner_ids(
        {"give_up_isolated_ids": ["st-giveup"]}, _stub_diff(), plan, results)
    assert owners == [], (
        "scope 声明归因会把无辜存活者定向重派且永远消不掉指纹行——归因必须按 diff 行作者")


def test_d3b_l2_targeted_injects_retry_guidance():
    """复核 CONFIRMED（D3b-①）：L2 定向恢复不注指引=重派 worker 同 prompt 同条件
    复产同 stub，replan 预算全烧盲重试——l2_details.retry_guidance 必须经既有
    retry_guidance 通道注入被归因子任务。"""
    import asyncio
    from swarm.brain.nodes import handle_failure
    st1, st2 = _st("st-1", create=["a.java"]), _st("st-2", create=["b.java"])
    plan = TaskPlan(subtasks=[st1, st2], parallel_groups=[["st-1", "st-2"]])
    wo = {sid: WorkerOutput(subtask_id=sid, diff="x", summary="",
                            confidence=Confidence.HIGH, l1_passed=True)
          for sid in ("st-1", "st-2")}
    state = {
        "verification_failure": "l2",
        "l2_targeted": True,
        "failed_subtask_ids": ["st-2"],
        "l2_details": {"retry_guidance": "禁止假实现桩，必须真实实现"},
        "plan": plan,
        "subtask_results": dict(wo),
        "dispatch_remaining": [],
        "subtask_retry_counts": {},
        "replan_count": 0,
    }
    result = asyncio.run(handle_failure(state))
    assert result["failure_strategy"] == "retry"
    assert "禁止假实现桩" in (st2.retry_guidance or ""), (
        "被归因子任务必须携带 L2 定向指引（对照运行时冒烟分支的既有证据注入通道）")
    assert not st1.retry_guidance, "成功兄弟不得被误注指引"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("主题D 全部通过")
