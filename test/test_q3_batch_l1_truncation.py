"""26 号文 Q 路第三批：C-1 L1 放行被截断销毁的产物 + C-3 give-up 桩零闸门。

这两条的共性是**产物层面的假过**：账面 l1_passed=True，产物本身是坏的。
22 号文当年读的是「判定记录」，而判定记录正是本轮被证明不可信的那一层
（元教训：「L1 记录」与「diff 全文」是两个不同的证据源）。
"""
from __future__ import annotations

import pathlib
import tempfile

import pytest

from swarm.brain.nodes.planning_core import _strip_ungrounded_manifest_coords
from swarm.worker.l1_pipeline import _truncated_artifacts


def _d(body: str, path="A.java", new=False) -> str:
    head = (f"diff --git a/{path} b/{path}\n"
            + ("new file mode 100644\n--- /dev/null\n" if new else f"--- a/{path}\n")
            + f"+++ b/{path}\n@@ -1,9 +1,9 @@\n")
    return head + body


# ══════════════════════════════════════════════
# C-1：被截断销毁的产物 L1 判 PASS（四道闸全瞎）
# ══════════════════════════════════════════════

_ST8 = _d(
    "     public String toString() {\n"
    "-        return new ToStringBuilder(this,ToStringStyle.MULTI_LINE_STYLE)\n"
    "-            .toString();\n"
    "-    }\n"
    "-}\n"
    "+        return new ToStringBuilder(this,To\n"
    "\\ No newline at end of file\n",
    path="tpl/domain.java.vm")


def test_truncated_artifact_is_caught():
    """★round67m2 st-8 真实形态（26 号文 C-1）★
    修复轮撞 Agent 迭代上限 50 被强行截断，文件停在**半个标识符**上，base 105 行的最后
    两个闭合括号连同方法体一起被删。四道 L1 闸全瞎：`.vm` 是 resource 不进 javac；
    `test_cmd=null → l1_3_test_ok=true`；4 条 verify_commands 全是文件【顶部】的存在性
    grep（尾部被砍一条都不会失败）。**若任务未 escalate 会一路交付到用户手里。**"""
    hits = _truncated_artifacts(_ST8)
    assert hits and hits[0]["file"].endswith("domain.java.vm")
    assert "换行" in hits[0]["evidence"], "末行无换行符是本形态的强信号"


def test_new_file_stopped_midway_is_caught():
    """新文件写到一半（无 base）同样是截断——一个自成一体的新文件本就该收支平衡。"""
    assert _truncated_artifacts(_d("+class N {\n+  void m() {\n", path="N.java", new=True))


@pytest.mark.parametrize("body,label", [
    (" class A {\n-  void x() { }\n+  void x() { doIt(); }\n+  void y() { }\n }\n", "正常增删"),
    ("-  log(\"a\");\n+  log(\"}\");\n", "字符串里含 }"),
    ("-  // }\n+  // } 注释里的括号\n", "注释里含 }"),
])
def test_legitimate_changes_are_not_flagged(body, label):
    """★闸不能矫枉过正★：合法改动无论怎么增删都不该改变【整个文件】的定界符收支；
    字符串字面量与行注释先剔除，否则 `log("}")` 这类会造成假差额。"""
    assert not _truncated_artifacts(_d(body)), label


def test_partial_hunk_window_is_not_flagged():
    """★判据必须是"收支之差"而不是"窗口内绝对平衡"★
    hunk 窗口本就可能只覆盖半个块（st-8 的窗口里 old 侧就是 -1），
    要求窗口内绝对平衡会把所有局部改动误杀。"""
    assert not _truncated_artifacts(_d("     if (x) {\n-      a();\n+      b();\n"))


def test_indentation_languages_never_flagged():
    """纯缩进语言（Python/YAML）天然无成对定界符 → 本闸对其恒不命中。"""
    assert not _truncated_artifacts(_d("-def f(): pass\n+def g(): pass\n", path="a.py"))


def test_gate_is_wired_into_l1_and_fails_the_subtask(tmp_path):
    """批25 GS-5w 换锁：原命题=源码序断言（截断闸 index < _package_decl_mismatches
    index + reason 键存在）。
    换成行为锁：真跑 run_l1_pipeline 喂 st-8 截断 diff（scope 放行该文件）→
    子任务必须判死且 reason=truncated_artifact。
    reason 经 `_l1_failure_digest` 出口把证据带进重试 prompt，且在 `_failure_signature`
    键集内（no-progress 早停可触发）；只写 note 则 worker 全盲＝盲烧满 fix 轮。
    红条件：删掉/绕过截断闸 → pipeline 走到后续阶段（空项目编译 BLOCKED 或别的原因），
    reason≠truncated_artifact → 本测试红。"""
    from swarm.types import FileScope, SubTask
    from swarm.worker import l1_pipeline

    st = SubTask(id="st-8", description="d",
                 scope=FileScope(writable=["tpl/domain.java.vm"]), depends_on=[])
    ok, details = l1_pipeline.run_l1_pipeline(str(tmp_path), st, _ST8)
    assert ok is False, "被截断销毁的产物 L1 必须判死（26 号文 C-1）"
    assert details["reason"] == "truncated_artifact"
    assert details.get("l1_1c_not_truncated") is False


def test_split_diff_contract_is_respected():
    """★`split_diff_by_file` 返回 list[(paths, text)] 而非 dict★
    初版按 dict 写 `.items()` → 每次都抛进 fail-open 的 except，**闸从未生效**，
    而只断言"不误杀"的测试会全绿——本轮反复栽的假绿形态，故留此正向断言把门。"""
    from swarm.project.diff_apply import split_diff_by_file
    out = split_diff_by_file(_ST8)
    assert isinstance(out, list) and isinstance(out[0], tuple)


# ══════════════════════════════════════════════
# C-3：give-up 桩写构建清单，臆造坐标零闸门
# ══════════════════════════════════════════════

_STUB_POM = (
    "diff --git a/m/pom.xml b/m/pom.xml\n+++ b/m/pom.xml\n"
    "+  <parent><version>3.8.7</version></parent>\n"
    "+  <version>4.8.3</version>\n"
    "+  <version>${revision}</version>\n"
)


@pytest.fixture
def baseline_repo():
    with tempfile.TemporaryDirectory() as d:
        pathlib.Path(d, "pom.xml").write_text("<project><version>4.8.3</version></project>")
        yield d


def test_fabricated_version_is_stripped(baseline_repo):
    """★桩是全流程唯一"LLM 直接产出构建清单且不过任何确定性闸"的通路（26 号文 C-3）★
    它硬写 `l1_passed=True`、零编译零验收就解锁下游。round67m2 实证 st-3-1：桩写的父 POM
    版本 3.8.7 而 base 是 4.8.3 → 解析必失败，四道闸全瞎一路交付。
    与铁律"绝不猜依赖坐标"正面冲突，故基线查无实据的版本号确定性剥离。"""
    out = _strip_ungrounded_manifest_coords(_STUB_POM, baseline_repo, "st-3-1")
    assert "3.8.7" not in out


def test_grounded_version_and_placeholder_survive(baseline_repo):
    """真坐标（基线里存在）与 `${...}` 占位符（引用不是坐标）都必须保留——
    剥过头会把桩的骨架也毁了，而桩的价值恰恰是让下游可编译。"""
    out = _strip_ungrounded_manifest_coords(_STUB_POM, baseline_repo, "st-3-1")
    assert "4.8.3" in out and "${revision}" in out


def test_no_baseline_manifest_means_no_judgement():
    """★读不到基线就无从判定，绝不凭空剥离（fail-open）★
    非 JVM 栈自然零命中；project_path 不可读同理原样返回。"""
    with tempfile.TemporaryDirectory() as empty:
        assert _strip_ungrounded_manifest_coords(_STUB_POM, empty, "x") == _STUB_POM
    assert _strip_ungrounded_manifest_coords(_STUB_POM, None, "x") == _STUB_POM
    assert _strip_ungrounded_manifest_coords(None, "/tmp", "x") is None


def test_stub_gate_is_wired_at_the_only_stub_exit(monkeypatch, baseline_repo):
    """批25 GS-5w 换锁：原命题=源码窗口断言（剥离闸调用距桩产出点 <1500 字节，
    命题=桩产出版本被剥离）。
    换成行为锁：真跑 _give_up_preserve_build（桩唯一产出通路），mock LLM 桩产出
    含臆造坐标（父 POM 3.8.7，基线真身 4.8.3）→ 落账的 WorkerOutput.diff 里臆造
    版本必须已被剥离、真坐标保留（26 号文 C-3：桩是 LLM 产物却硬写 l1_passed=True
    解锁下游，绝不许带臆造坐标出闸）。
    红条件：删掉/绕过 _strip_ungrounded_manifest_coords 调用 → "3.8.7" 留在桩 diff
    里落账 → 本测试红。"""
    import asyncio

    from swarm.brain.nodes import planning_core
    from swarm.types import FileScope, SubTask, TaskPlan

    plan = TaskPlan(subtasks=[
        SubTask(id="st-x", description="d",
                scope=FileScope(writable=["m/pom.xml"]), depends_on=[]),
        SubTask(id="st-y", description="d",
                scope=FileScope(writable=["y.java"]), depends_on=["st-x"]),
    ])
    state = {"plan": plan, "project_id": "p", "subtask_results": {},
             "dispatch_remaining": ["st-x", "st-y"]}

    async def _fake_stub(*a, **k):
        return _STUB_POM  # LLM 产出的桩：含臆造 3.8.7 + 真坐标 4.8.3 + 占位符

    monkeypatch.setattr(planning_core, "_generate_compile_stub", _fake_stub)
    monkeypatch.setattr("swarm.project.store.get_project",
                        lambda pid: {"path": baseline_repo})

    out = asyncio.run(planning_core._give_up_preserve_build(state, ["st-x"]))
    stub_out = out["subtask_results"]["st-x"]
    assert stub_out.l1_details.get("give_up_mode") == "stub", \
        "夹具自证：必须真走到桩产出通路（而非 revert 回退）"
    assert "3.8.7" not in stub_out.diff, "桩里的臆造坐标必须在唯一产出点被剥离（C-3 回归）"
    assert "4.8.3" in stub_out.diff and "${revision}" in stub_out.diff
