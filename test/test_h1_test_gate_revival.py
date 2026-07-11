"""主题H H1 · 测试门复活——LLM 编排验收断言 verify_commands 不再被结构性抹掉。

病根（round38c 52/52 test_skipped 实证）：
 ① PLAN_SYSTEM rule11 要求"harness 必填含 verify_commands"，PLAN_USER 却说"【不要】输出
    harness 字段（系统推断）"——同一条消息自相矛盾→模型两不靠→零 verify_commands。
 ② _infer_harness 默认 verify_commands 为空（只有编译门），而 harness 兜底判据把"只有
    verify_commands"的 LLM harness 当完整→跳过推断→丢掉编译门。
治：①两 prompt 归一（工具链系统推断 + LLM 专注 verify_commands，与"不主动加单测"解耦）；
    ②bootstrap_subtask_harness 不看 verify：缺 build/test/whitelist 就补推断工具链，再叠加
    LLM verify_commands（防丢编译门 + 保住验收断言）。
"""
from __future__ import annotations

from swarm.brain.nodes.shared import _strip_unrequested_tests, bootstrap_subtask_harness
from swarm.types import FileScope, SubTask, TaskHarness, TaskPlan


def _st(desc, harness=None, create=None):
    # SubTask.harness 必填（default_factory），生产从不为 None——空 harness 即"未编排"态。
    return SubTask(
        id="st-1", description=desc,
        scope=FileScope(create_files=create or ["src/main/java/com/x/StringUtils.java"]),
        harness=harness if harness is not None else TaskHarness(),
    )


# ══════════════ bootstrap_subtask_harness（核心 bug 修复）══════════════

def test_h1_verify_only_harness_gets_build_gate_and_keeps_verify():
    """LLM 只出 verify_commands（PLAN_USER 常态）→ 补推断的编译门 + 保住验收断言。"""
    vc = ["grep -q 'public String trimToNull' src/main/java/com/x/StringUtils.java"]
    st = _st("给 StringUtils 加 trimToNull 方法",
             harness=TaskHarness(verify_commands=list(vc)))
    bootstrap_subtask_harness(st, "给 StringUtils 加 trimToNull 方法")
    assert st.harness.build_command, "旧 bug：verify-only 被当完整→跳过推断→无编译门（回归）"
    assert st.harness.language == "java", "按 scope 后缀推断主导语言"
    assert st.harness.verify_commands == vc, "LLM 验收断言必须保住（叠加不丢）"


def test_h1_empty_harness_infers_toolchain_no_verify():
    st = _st("加个方法", harness=TaskHarness())  # 空 harness=未编排
    bootstrap_subtask_harness(st, "加个方法")
    assert st.harness.build_command and st.harness.verify_commands == []


def test_h1_none_harness_guard():
    """生产 SubTask.harness 从不为 None，但 helper 的 h is None 兜底也要成立（防裸对象）。"""
    from types import SimpleNamespace
    st = SimpleNamespace(harness=None, description="加个方法",
                         scope=FileScope(create_files=["a.py"]))
    bootstrap_subtask_harness(st, "加个方法")
    assert st.harness.build_command  # 推断出 python 工具链


def test_h1_full_llm_harness_respected():
    """batch 路径 LLM 给了完整 harness（build+verify）→ 原样尊重，不被推断覆盖。"""
    h = TaskHarness(language="python", build_command="python -m compileall -q .",
                    verify_commands=["python -c 'import mod'"], extra_whitelist=["python"])
    st = _st("x", harness=h)
    bootstrap_subtask_harness(st, "x")
    assert st.harness.build_command == "python -m compileall -q ."
    assert st.harness.verify_commands == ["python -c 'import mod'"]


def test_h1_verify_dedup_when_inferred_has_none():
    st = _st("x", harness=TaskHarness(verify_commands=["a", "a", "b"]))
    bootstrap_subtask_harness(st, "x")
    assert st.harness.verify_commands == ["a", "b"], "叠加去重"


# ══════════════ _strip 不误伤 verify_commands ══════════════

def test_h1_strip_unrequested_tests_preserves_verify_commands():
    """任务未要求测试→清 test_command（防 junit 缺失误判），但 verify_commands 必须留。"""
    h = TaskHarness(language="java", build_command="mvn -q compile",
                    test_command="mvn -q test", verify_commands=["grep -q foo Bar.java"])
    st = SubTask(id="st-1", description="加方法",
                 scope=FileScope(writable=["Bar.java"]), harness=h)
    plan = TaskPlan(subtasks=[st])
    out = _strip_unrequested_tests(plan, "给 Bar 加一个方法")  # 未要求测试
    h2 = out.subtasks[0].harness
    assert h2.test_command == "", "未要求测试→清 test_command"
    assert h2.verify_commands == ["grep -q foo Bar.java"], "验收断言不受'不主动加测试'牵连"


# ══════════════ prompt 契约归一（防契约再回退）══════════════

def test_h1_plan_prompts_no_longer_contradict():
    from swarm.brain import prompts as P
    # 旧矛盾：PLAN_USER 全盘"【不要】输出 harness 字段" vs PLAN_SYSTEM rule11 "harness 必填"
    assert "【不要】输出 harness 字段" not in P.PLAN_USER, "全盘禁 harness 的旧矛盾条款须移除"
    assert "verify_commands" in P.PLAN_USER, "PLAN_USER 应引导 LLM 出验收断言"
    assert "verify_commands" in P.PLAN_SYSTEM, "PLAN_SYSTEM rule11 应聚焦 verify_commands"
    # batch 路径（超大需求分批，E2E RuoYi 主路径）同样要能出 verify_commands——其子任务
    # 也流经 bootstrap_subtask_harness（plan() 装配循环），否则 H1 在真实 E2E 工作负载失明。
    assert "verify_commands" in P.PLAN_BATCH_SYSTEM, "batch 路径也应引导验收断言"


if __name__ == "__main__":
    print("run via pytest")
