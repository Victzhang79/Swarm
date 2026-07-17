"""R65D-W3 观测缺口批：round65d 复盘中六处「看不见/看错」的观测面。

① trivial/Phase-4 确定性闸判死零 reason（st-26 冤案 79ms 判 False 全程无一行解释）
② "拦截幻觉 PASS" 措辞=冤案框架（worker 产物本可能合规，被 H1 覆写后败于旧卷）
③ HANDLE_FAILURE 无逐失败处置总账行（掉账要等终态复盘才见）
④ clean_upload tracked 判定空集：greenfield（全新建）与 git 故障混在一条 WARN
⑤ P1 外科补齐主备双失败：auth 类错误（401/403=配置错需 ops）与瞬时错同级 WARNING
⑥ MANIFEST-SYNTH：base 根 pom 仅缺尾换行就整体跳过合成（模块注册静默丢失）
"""
from __future__ import annotations

import logging

from swarm.types import FileScope, SubTask, TaskPlan, WorkerOutput


def _wo_fail(sid, details):
    return WorkerOutput(subtask_id=sid, diff="+x", summary="", l1_passed=False,
                        l1_details=details, confidence="low")


# ── ①② 确定性判死必须带 reason；措辞去冤案化 ──

def test_det_fail_reason_extractor_covers_known_shapes():
    from swarm.worker.executor import _det_fail_reason
    assert "verify_failed" in _det_fail_reason(
        {"verify_failed": "grep -q 'jackson' mod/pom.xml"})
    assert "jackson" in _det_fail_reason(
        {"verify_failed": "grep -q 'jackson' mod/pom.xml"})
    assert "empty_diff" in _det_fail_reason(
        {"reason": "empty_diff_but_changes_expected"})
    assert "scope" in _det_fail_reason(
        {"scope_violations": ["a.py", "b.py"]}).lower()
    assert _det_fail_reason({}) != "", "空 details 也要给出兜底说明"


def test_det_conflict_log_line_behavioral():
    """★st-26 观测盲区本体★（行为级，替代 getsource 守卫——猎手裁定违反仓规）：
    det=False 而 LLM 自报通过的日志行必带机读判死依据、绝不用冤案措辞。"""
    from swarm.worker.executor import _det_conflict_log_line
    line = _det_conflict_log_line(
        {"verify_failed": "grep -q 'jackson' mod/pom.xml"}, phase="trivial")
    assert "判死依据" in line and "jackson" in line, line
    assert "拦截幻觉" not in line, "冤案措辞必须移除（败因常在闸门/考卷侧）"
    assert line.startswith("trivial: ")


def test_det_fail_reason_compile_message_and_hardening():
    """复核 HIGH 锁：单文件编译闸形态（compile_message，build_output 结构性缺席）
    必须提取到真错误；猎手 HIGH 锁：畸形 details 兜底自报绝不冒泡。"""
    from swarm.worker.executor import _det_fail_reason
    r = _det_fail_reason({"l1_2_compile_ok": False,
                          "compile_message": "SyntaxError: invalid syntax at line 3"})
    assert "SyntaxError" in r, f"compile_message 必须被提取: {r}"
    assert _det_fail_reason("not-a-dict").startswith("deterministic_gate"), \
        "非 dict 输入按空处理"
    weird = _det_fail_reason({"scope_violations": 42})
    assert "scope_violations" in weird, f"非 list 形态不崩: {weird}"


# ── ③ HANDLE_FAILURE 逐失败处置总账 ──

def test_handle_failure_emits_disposition_ledger_line(caplog):
    import asyncio

    from swarm.brain.nodes import handle_failure
    plan = TaskPlan(subtasks=[
        SubTask(id="st-r", description="d",
                scope=FileScope(writable=["src/A.java"]))])
    state = {
        "failed_subtask_ids": ["st-r"],
        "subtask_results": {"st-r": _wo_fail("st-r", {"verify_failed": "grep"})},
        "dispatch_remaining": [],
        "plan": plan,
    }
    with caplog.at_level(logging.INFO):
        asyncio.run(handle_failure(state))
    assert any("处置总账" in rec.message for rec in caplog.records), \
        "每轮失败处置必须落一行机读总账（入口 N→重派 X 放弃 Y 保留失败 Z）"


# ── ⑤ P1 外科主备双失败：auth 类升 ERROR ──

def test_p1_auth_error_classifier():
    from swarm.brain.nodes import _is_auth_shaped_error
    assert _is_auth_shaped_error(Exception("Error code: 401 - unauthorized"))
    assert _is_auth_shaped_error(Exception("403 Forbidden"))
    assert _is_auth_shaped_error(Exception("Invalid API key provided"))
    assert not _is_auth_shaped_error(Exception("timeout after 500s"))
    assert not _is_auth_shaped_error(Exception("connection reset by peer"))


# ── ⑥ MANIFEST-SYNTH 跳过必须列出受损模块 ──

def test_manifest_synth_skip_names_lost_registrations(caplog):
    """无尾换行保守跳过有真实技术依据（difflib 无 '\\ No newline' 标记支持）——
    但跳过日志必须列出【哪些新模块注册被丢弃】，否则运维看不见损失面。"""
    from swarm.brain.manifest_synth import fold_module_registrations
    base_pom = ("<project>\n    <modules>\n        <module>old-mod</module>\n"
                "    </modules>\n</project>")   # 无尾换行
    new_mod_diff = (
        "--- /dev/null\n+++ b/new-mod/pom.xml\n@@ -0,0 +1,4 @@\n"
        "+<project>\n+    <parent><artifactId>root</artifactId></parent>\n"
        "+    <artifactId>new-mod</artifactId>\n+</project>\n")
    with caplog.at_level(logging.WARNING):
        out_diff, registered = fold_module_registrations(new_mod_diff, base_pom)
    assert registered == [] and out_diff == new_mod_diff, "保守跳过语义不变"
    assert any("new-mod" in rec.message for rec in caplog.records
               if "MANIFEST-SYNTH" in rec.message), \
        "跳过必须点名受损模块（观测缺口：现在只说'跳过合成'不说丢了什么）"


def test_det_fail_reason_build_fail_not_masked_by_compile_ok():
    """R65REPLAY（回放实锤"判死依据: compile_fail: compile ok"）：build 段失败时
    compile_message 常是早段通过的 "compile ok"——必须引 build_output 真错误行，
    绝不让判死依据变成一句自相矛盾的废话。"""
    from swarm.worker.executor import _det_fail_reason
    r = _det_fail_reason({
        "l1_2_compile_ok": True,
        "compile_message": "compile ok",
        "l1_2_1_build_ok": False,
        "build_output": ("[INFO] Scanning...\n"
                         "[ERROR] Failed to execute goal on project ruoyi-alarm: "
                         "Non-resolvable parent POM\n[INFO] BUILD FAILURE"),
    })
    assert "compile ok" not in r, f"build 失败被 compile ok 遮蔽: {r}"
    assert "Non-resolvable parent POM" in r, f"build_output 真错误行未被提取: {r}"


def test_det_fail_reason_true_compile_fail_still_quoted():
    """回归锁：单文件编译真失败（compile_ok=False）仍引 compile_message。"""
    from swarm.worker.executor import _det_fail_reason
    r = _det_fail_reason({
        "l1_2_compile_ok": False,
        "compile_message": "cannot find symbol: AlarmDutySnapshot",
    })
    assert "cannot find symbol" in r
