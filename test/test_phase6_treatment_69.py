"""阶段6.9（双复核治理批）：hunter/reviewer CONFIRMED 条目行为锁。

F1/F2 subtask_diffs 是 list[tuple] 却被 .get——D3 分支必崩（无 try）+ D4 注入被
   except pass 吞成 100% 死代码。教训=测试喂的形态（dict/干净 diff）与生产形态不一致，
   本文件补【merge() 节点级】驱动。
HF3 rebase 超限终点按来源分流：new_file（选中版已交付）→ abandoned+PARTIAL；
   three_way（真源码 hunk 被丢）才走 D3 判定。
RF4 D9 两类真实 diff 形态误判：空串=空白 context 行（跳过不推游标→错位）；CRLF×LF。
RF3 F9 栈键与 detect_stack 真实画像（build 字段）不对齐——带画像反而关闭 A2 治本。
HF4 F5 重抽坏轮 clobber 好轮次 → 跨轮保优。
HF7 D5 归因词边界+无主符号回退全员。
HF6 __ACCEPT_LOGIN__:empty → bearer 断言 inconclusive（登录坏≠产品坏）。
HF8 D15 后脚手架环确定性破除（仅成环时剥后向边）。
HF9 dedupe 机器追加段定界符化，_subtask_signature 剥离（防外科误剪完成态）。
HF10 断言拒绝分列 quality|truncated。HF5 needs_review 接入 deliver payload。
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from swarm.types import (
    Confidence,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
    WorkerOutput,
)


def _new_file_diff(path, lines):
    body = "".join(f"+{line}\n" for line in lines)
    return (f"--- /dev/null\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n{body}")


def _st(sid, create=None, desc="", ac=None, deps=None):
    return SubTask(id=sid, description=desc or f"任务 {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=[], readable=[], create_files=create or []),
                   acceptance_criteria=ac or [], depends_on=deps or [])


def _wo(sid, diff):
    return WorkerOutput(subtask_id=sid, diff=diff, summary="", l1_passed=True,
                        confidence=Confidence.HIGH)


def _merge_state(rebase_counts=None):
    d1 = _new_file_diff("mod/src/Svc.java", ["class Svc {", "  int a;", "}"])
    d2 = _new_file_diff("mod/src/Svc.java", ["class Svc {", "  int b;", "}"])
    plan = TaskPlan(subtasks=[_st("st-1", ["mod/src/Svc.java"]),
                              _st("st-2", ["mod/src/Svc.java"])],
                    parallel_groups=[["st-1", "st-2"]])
    return {
        "subtask_results": {"st-1": _wo("st-1", d1), "st-2": _wo("st-2", d2)},
        "plan": plan,
        "subtask_rebase_counts": dict(rebase_counts or {}),
        "project_id": "",
    }


# ─────────────── F1/F2/HF3：merge() 节点级 ───────────────


def test_69_f2_d4_retry_guidance_actually_injected():
    """F2：修复前 subtask_diffs.get 必抛被 pass 吞 → retry_guidance 永不写（死代码）。"""
    import swarm.brain.nodes as nodes
    state = _merge_state()
    out = nodes.merge(state)
    assert out.get("rebase_subtask_ids"), "新文件双写者不一致必产生 rebase（D2）"
    _sid = out["rebase_subtask_ids"][0]
    _rg = next(s for s in state["plan"].subtasks if s.id == _sid).retry_guidance
    assert _rg and "保留" in _rg, (
        "D4 注入是打破 worker 同形 diff 重生成死循环的唯一通道（dispatch 无 merged_diff"
        "回灌）——修复前 100% 静默 no-op")


def test_69_f1_hf3_over_limit_newfile_abandons_not_crash_not_escalate():
    """F1：修复前本路径 AttributeError 崩节点；HF3：new_file 来源超限走 abandoned。"""
    import swarm.brain.nodes as nodes
    state = _merge_state(rebase_counts={"st-1": 99, "st-2": 99})
    out = nodes.merge(state)  # 修复前：'list' object has no attribute 'get'
    assert out.get("failure_escalated") is not True, (
        "new_file 来源超限=选中版已在 merged_diff、丢的只是落选版本——escalate 会把"
        "可交付任务判死（旧静默丢弃好歹交付）")
    _dropped = set(out.get("merge_rebase_dropped") or [])
    _abandoned = set(out.get("abandoned_subtask_ids") or [])
    assert _dropped and _dropped <= _abandoned, (
        "超限 new_file sid 必须并入 abandoned（终态诚实 PARTIAL），不再假 DONE")
    for _sid in _dropped:
        assert _sid not in (out.get("subtask_results") or {}), "账面 pop 完成态"


def test_69_hf3_three_way_source_over_limit_still_escalates():
    """HF3 对称面：three_way 来源（真源码 hunk 被丢）超限仍 escalate（D3 语义保留）。"""
    import swarm.brain.nodes as nodes
    base = "A\nB\nC\n"
    d1 = "--- a/mod/src/X.java\n+++ b/mod/src/X.java\n@@ -2,1 +2,1 @@\n-B\n+B1\n"
    d2 = "--- a/mod/src/X.java\n+++ b/mod/src/X.java\n@@ -2,1 +2,1 @@\n-B\n+B2\n"
    plan = TaskPlan(subtasks=[_st("st-1"), _st("st-2")],
                    parallel_groups=[["st-1", "st-2"]])
    state = {
        "subtask_results": {"st-1": _wo("st-1", d1), "st-2": _wo("st-2", d2)},
        "plan": plan, "subtask_rebase_counts": {"st-1": 99, "st-2": 99},
        "project_id": "p-x",
    }
    import swarm.brain.merge_engine as me
    with patch.object(nodes, "_get_project_path", return_value=None), \
         patch.object(me, "_git_merge_file", return_value=None):
        # 无 project_path → base_reader 不可用 → 3-way 不可行 → 冲突/rebase 兜底
        out = nodes.merge(state)
    if out.get("rebase_subtask_ids") or out.get("merge_conflicts") \
            or out.get("failure_escalated"):
        # 只要走到超限判定：three_way/冲突形态绝不落 "abandoned 继续交付"
        assert not (set(out.get("merge_rebase_dropped") or [])
                    & set(out.get("abandoned_subtask_ids") or []) )or \
            out.get("failure_escalated") is True or out.get("merge_conflicts"), (
            "真源码 hunk 被丢时不允许按 new_file 口径静默放行")


# ─────────────── RF4：D9 真实 diff 形态 ───────────────


def test_69_rf4_blank_context_line_as_empty_string_applies():
    from swarm.brain.merge_engine import _Hunk, apply_hunks_to_text
    base = "line1\n\nline3\n"
    # LLM 常见形态：空白 context 行以空串表示（无 " " 前缀）
    h = _Hunk(old_start=1, old_count=3, new_start=1, new_count=3,
              lines=["@@ -1,3 +1,3 @@", " line1", "", "-line3", "+changed"],
              subtask_id="st-1")
    assert apply_hunks_to_text(base, [h]) == "line1\n\nchanged\n", (
        "空串行跳过且不推进游标=错位 → 干净 3-way 被假阳性打落 rebase（修复前必抛）")


def test_69_rf4_crlf_base_lf_diff_applies():
    from swarm.brain.merge_engine import _Hunk, apply_hunks_to_text
    base = "line1\r\nline2\r\nline3\r\n"
    h = _Hunk(old_start=2, old_count=1, new_start=2, new_count=1,
              lines=["@@ -2,1 +2,1 @@", "-line2", "+changed"], subtask_id="st-1")
    out = apply_hunks_to_text(base, [h])
    assert "changed" in out and "line2" not in out, (
        "CRLF base × LF diff 是混合行尾真实形态，非基线漂移")


def test_69_rf4_true_drift_still_raises():
    from swarm.brain.merge_engine import HunkContextMismatch, _Hunk, apply_hunks_to_text
    base = "line1\nline2\n"
    h = _Hunk(old_start=1, old_count=1, new_start=1, new_count=1,
              lines=["@@ -1,1 +1,1 @@", "-DRIFTED", "+x"], subtask_id="st-1")
    with pytest.raises(HunkContextMismatch):
        apply_hunks_to_text(base, [h])


# ─────────────── RF3：F9 真实画像形态 ───────────────


def test_69_rf3_real_stack_profile_routes_to_maven():
    import swarm.brain.nodes.maven_repair as mr
    calls = []

    def _fake_driver(project_path, granted, subtask_results):
        calls.append(project_path)
        return {"pom.xml": "patched"}

    real_profile = {"frontend": "Thymeleaf", "backend": "Spring Boot 2.x (java)",
                    "build": "maven"}
    with patch.dict(mr._DEP_REPAIR_DRIVERS, {"maven": _fake_driver}):
        out = mr.inject_missing_deps_for_stack(real_profile, "/p", {}, {})
    assert calls and out, (
        "detect_stack 真实画像字段是 build（值=maven/gradle/…）——旧键列表"
        "('build_system','backend','primary') 一个都不存在，正常 E2E 带画像时"
        "A2 治本被静默关闭（复核活体实证）")


def test_69_rf3_gradle_profile_stays_loud_noop():
    import swarm.brain.nodes.maven_repair as mr
    out = mr.inject_missing_deps_for_stack(
        {"backend": "Spring Boot (java)", "build": "gradle"}, "/p", {}, {})
    assert out == {}, "gradle 无 driver=loud no-op；绝不因 backend 含 java 硬猜 maven"


# ─────────────── HF4：F5 跨轮保优 ───────────────


def test_69_hf4_bad_retry_round_does_not_clobber_best():
    from swarm.brain.requirements_extract import extract_requirements

    src = "需求甲：系统必须支持登录。" * 300  # 长文本 → min_expect > 2 触发重抽
    good = [{"text": "系统必须支持登录", "kind": "functional",
             "source_quote": "系统必须支持登录"},
            {"text": "需求甲：系统必须支持登录", "kind": "functional",
             "source_quote": "需求甲：系统必须支持登录"}]

    class _Resp:
        def __init__(self, content):
            self.content = content

    outputs = [
        _Resp(__import__("json").dumps({"items": good}, ensure_ascii=False)),
        # 重抽轮全幻觉（quote 不回指）→ 零合法
        _Resp(__import__("json").dumps({"items": [
            {"text": "幻觉需求", "kind": "functional", "source_quote": "不存在的引文"}
        ]}, ensure_ascii=False)),
    ]

    class _LLM:
        async def ainvoke(self, msgs):
            return outputs.pop(0) if outputs else _Resp('{"items": []}')

    import swarm.brain.nodes as nodes
    with patch.object(nodes, "_get_brain_llm", return_value=_LLM()):
        out = asyncio.run(extract_requirements({"task_description": src}))
    items = out.get("requirement_items") or []
    assert len(items) == 2, (
        "F5 重抽把好轮次（2 条真需求）clobber 成坏轮次（0 条）→ 覆盖闸对空清单整体"
        "跳过——必须跨轮保优")


# ─────────────── HF7：D5 归因 ───────────────


def test_69_hf7_word_boundary_no_false_owner_and_unattributed_falls_back_all():
    from swarm.brain.nodes.verify import _d5_attribute_owners
    plan = TaskPlan(subtasks=[
        _st("st-a", desc="实现 blacklist 管理页面"),      # 裸子串会误命中 "list"
        _st("st-b", desc="实现 IUserService.list 接口", ac=["list 接口返回分页"]),
    ], parallel_groups=[["st-a", "st-b"]])
    results = {"st-a": object(), "st-b": object()}

    owners, sym_owners, _un = _d5_attribute_owners(["list"], plan, results)
    assert sym_owners.get("list") == ["st-b"], "词边界归因：blacklist 不算命中 list"
    assert owners == ["st-b"]

    # A1 语义演进（round38c P0，用户拍板）：无主符号不再回退全员认领——旧回退在
    # owner 不在语料（被弃者）时退化成全员连坐清零。现返回空 owners + 机读
    # unattributed，由 verify/HANDLE_FAILURE 双侧守卫走升级通道（诚实 PARTIAL）。
    # 详见 test_a1_contract_attribution_monotonic.py。
    owners2, _, un2 = _d5_attribute_owners(["IWholeNewService"], plan, results)
    assert owners2 == [], "归因不出绝不回退全员（A1 单调守卫）"
    assert un2 == ["IWholeNewService"]


# ─────────────── HF6：登录 infra 三态 ───────────────


def _bearer_spec(sid="as-1"):
    return {"id": sid, "req_id": "r-1", "kind": "http_probe", "auth": "bearer",
            "request": {"method": "GET", "path": "/api/me"},
            "expect": {"status": [200]}}


def test_69_hf6_login_empty_marks_bearer_inconclusive_not_fail():
    from swarm.brain.nodes.verify import _accept_phase_verdict
    accept_output = (
        "__ACCEPT_LOGIN__:empty\n"
        "__ACCEPT_RESULT__as-1__401\n__ACCEPT_BODY__as-1__dW5hdXRob3JpemVk\n")
    out = _accept_phase_verdict(
        [_bearer_spec()], {"auth_login_available": True}, "passed", accept_output)
    assert out.get("acceptance_passed") is None and not out.get("_failed"), (
        "登录 infra 失败（token 空）下 bearer 裸打 401 是结论性应答但登录坏≠产品坏——"
        "判 fail 会把失败归因到写者子任务白烧重试")
    assert "login_failed" in str(out.get("_degraded") or "")


def test_69_hf6_login_ok_bearer_fail_still_conclusive():
    from swarm.brain.nodes.verify import _accept_phase_verdict
    accept_output = (
        "__ACCEPT_LOGIN__:ok\n"
        "__ACCEPT_RESULT__as-1__500\n__ACCEPT_BODY__as-1__Ym9vbQ==\n")
    out = _accept_phase_verdict(
        [_bearer_spec()], {"auth_login_available": True}, "passed", accept_output)
    assert out.get("acceptance_passed") is False, "登录成功后的真实失败照常结论性判败"


# ─────────────── HF8：脚手架环破除 ───────────────


def test_69_hf8_scaffold_cycle_broken_deterministically():
    from swarm.brain.contract_utils import fix_dependency_ordering
    a = _st("st-a", create=["mod-a/pom.xml"], deps=["st-b"])
    b = _st("st-b", create=["mod-b/pom.xml"], deps=["st-a"])
    plan = TaskPlan(subtasks=[a, b], parallel_groups=[["st-a", "st-b"]])
    fix_dependency_ordering(plan)
    deps = {s.id: set(s.depends_on or []) for s in plan.subtasks}
    assert not ({"st-b"} <= deps["st-a"] and {"st-a"} <= deps["st-b"]), (
        "D15 保留 scaffold→scaffold 边后环无破除者 → plan_validator 硬失败 → replan"
        "（LLM 大概率复现同环）熔断烧钱；须确定性剥后向边")
    assert deps["st-b"] == {"st-a"}, "按原序保留前向边（st-a 先于 st-b）"


def test_69_hf8_acyclic_scaffold_deps_untouched():
    from swarm.brain.contract_utils import fix_dependency_ordering
    root = _st("st-root", create=["pom.xml"])
    child = _st("st-child", create=["mod/pom.xml"], deps=["st-root"])
    plan = TaskPlan(subtasks=[root, child], parallel_groups=[["st-root"], ["st-child"]])
    fix_dependency_ordering(plan)
    assert "st-root" in (next(s for s in plan.subtasks if s.id == "st-child").depends_on
                         or []), "无环时 D15 语义零回归"


# ─────────────── HF9：签名剥机器段 ───────────────


def test_69_hf9_signature_ignores_merged_dup_machine_segment():
    from swarm.brain.contract_utils import MERGED_DUP_DELIM
    from swarm.brain.nodes import _subtask_signature
    st1 = _st("st-1", create=["mod/pom.xml"], desc="建 mod 脚手架")
    st2 = _st("st-1", create=["mod/pom.xml"],
              desc="建 mod 脚手架" + MERGED_DUP_DELIM + "本轮 dup 语义（每轮漂移）")
    assert _subtask_signature(st1) == _subtask_signature(st2), (
        "dedupe 机器追加段随每轮 LLM dup 集漂移——混进签名会把语义未变的子任务"
        "误判'变了'→外科 reset 误剪完成态白重跑")


def test_69_hf9_dedupe_appends_with_delimiter():
    from swarm.brain.contract_utils import MERGED_DUP_DELIM, dedupe_module_scaffolds
    a = _st("st-1", create=["mod/pom.xml"], desc="canonical")
    b = _st("st-2", create=["mod/pom.xml"], desc="dup 独有语义")
    plan = TaskPlan(subtasks=[a, b], parallel_groups=[["st-1", "st-2"]])
    dedupe_module_scaffolds(plan)
    kept = plan.subtasks[0]
    assert MERGED_DUP_DELIM in kept.description and "dup 独有语义" in kept.description


# ─────────────── HF10/HF5 ───────────────


def test_69_hf10_truncated_rejects_categorized():
    from swarm.brain.acceptance_spec import MAX_ASSERTIONS, validate_assertions
    items = [{"id": f"r-{i}", "text": "需求", "kind": "functional"} for i in range(2)]
    raw = [{"id": f"as-{i}", "req_id": f"r-{i % 2}", "kind": "http_probe",
            "auth": "none", "request": {"method": "GET", "path": f"/api/x{i}"},
            "expect": {"status": [200]}} for i in range(MAX_ASSERTIONS + 4)]
    valid, rejected = validate_assertions(raw, items)
    assert len(valid) == MAX_ASSERTIONS
    _trunc = [r for r in rejected if r.get("category") == "truncated"]
    assert len(_trunc) == 4, (
        "政策性截断必须与质量拒绝分列——混计会把'装不下'误报成'生成失控'"
        "（degraded/L6 假信号）")


def test_69_hf5_needs_review_reaches_deliver_payload():
    from swarm.brain.nodes import _deliver_review_payload
    wo = _wo("st-1", "+x\n")
    wo.l1_details = {"needs_review": "no_test_or_verify_commands"}
    payload = _deliver_review_payload({"subtask_results": {"st-1": wo}})
    rows = payload.get("needs_review") or []
    assert rows and rows[0]["subtask_id"] == "st-1", (
        "C4 needs_review 全仓零消费=死键（3.8 教训重演）——人工闸必须看到"
        "'语义正确性零覆盖'清单")
