"""B3 merge/验收链深读治本（DR-03-F1..F8，task #54-#61）行为级测试。

只测【默认行为】，不 getsource/正则扫源码。覆盖：
- F6/#55 parse_marker_rc 末尾锚点退出码（无锚点子串假成功/假失败绝迹）
- F1/#54 沙箱 git apply 加 --ignore-whitespace --3way（与本地兄弟同旗标）
- F5/#61 filter_orphan_module_patches 混合补丁逐段剔孤儿、保真实
- F3/#60 折叠后 merged_diff 非法 → escalate
- F4/#59 revision() 清 verification_failure
- F2/#58 无测试命令 + project_path 空 → degraded 并携 l2_compile_unverified
- F8/#57 沙箱+本地双 None → LLM 兜底携 l2_test_downgraded_to_llm 留痕
- F7/#56 交付 apply 全失败 → delivery_incomplete → 终态 PARTIAL（既有 X-1 已覆盖，回归守卫）
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from swarm.brain.nodes.shared import parse_marker_rc


# ─────────────────────────── F6 / #55 ───────────────────────────

def test_f6_parse_marker_rc_takes_trailing_marker_not_substring():
    # 构建真失败(RC=1)但中段含字面 __RC__0（测试名/被测代码回显）→ 绝不能判成功
    out = "running test___RC__0___case ... FAILED\n...error...\n__RC__1"
    assert parse_marker_rc(out) == 1


def test_f6_parse_marker_rc_success_only_when_trailing_zero():
    assert parse_marker_rc("build ok\n__RC__0") == 0
    assert parse_marker_rc("build ok\n__RC__0\n") == 0


def test_f6_parse_marker_rc_missing_marker_is_none_infra():
    # marker 从未出现=命令没跑成(网关 5xx)=infra → None（调用方降级，绝不判成败）
    assert parse_marker_rc("gateway 502 bad gateway") is None
    assert parse_marker_rc("") is None
    assert parse_marker_rc(None) is None


def test_f6_parse_marker_rc_custom_apply_marker():
    assert parse_marker_rc("done __APPLY_RC__0", "__APPLY_RC__") == 0
    assert parse_marker_rc("patch already exists __APPLY_RC__1", "__APPLY_RC__") == 1


def test_f6_parse_marker_rc_negative_rc():
    assert parse_marker_rc("killed __RC__-9") == -9


# ─────────────────────────── F1 / #54 ───────────────────────────

def test_f1_sandbox_apply_uses_ignore_whitespace_matches_delivery():
    """沙箱 L2 git apply 必须加 --ignore-whitespace（RuoYi CRLF 铁律，与交付 apply_git_diff 同口径）；
    刻意【不加 --3way】——避免沙箱比交付更宽松导致 L2 假绿但 accept 期 422。"""
    from swarm.brain.nodes import _run_l2_in_sandbox

    apply_cmds: list[str] = []

    class _FakeResult:
        def __init__(self, stdout="", stderr=""):
            self.stdout = stdout
            self.stderr = stderr
            self.error = None

    class _FakeSandbox:
        sandbox_id = "sb-1"

    class _FakeManager:
        def create(self, **kw):
            return _FakeSandbox()

        def sync_project_to_sandbox(self, *a, **kw):
            pass

        def run_command(self, sandbox, cmd, timeout=None):
            if "git apply" in cmd:
                apply_cmds.append(cmd)
                return _FakeResult(stdout="__APPLY_RC__0")
            # test 命令
            return _FakeResult(stdout="__RC__0")

        def kill(self, *a, **kw):
            pass

    with patch("swarm.worker.sandbox.get_sandbox_manager", return_value=_FakeManager()), \
         patch("swarm.worker.sandbox.write_file_to_sandbox"):
        res = _run_l2_in_sandbox("/tmp/proj", "diff --git a/x b/x\n", "pytest", project_id="p1")

    assert res is True
    assert apply_cmds, "git apply 未被调用"
    assert "--ignore-whitespace" in apply_cmds[0]
    # 忠实预测器：不比交付 apply 更宽松 → 不加 --3way
    assert "--3way" not in apply_cmds[0]


def test_f1_sandbox_apply_infra_missing_marker_returns_none():
    """apply marker 缺失=infra → None 降级（不判 L2 失败）。"""
    from swarm.brain.nodes import _run_l2_in_sandbox

    class _FakeResult:
        stdout = "gateway 502"
        stderr = ""
        error = "502"

    class _FakeManager:
        def create(self, **kw):
            class _S:
                sandbox_id = "s"
            return _S()

        def sync_project_to_sandbox(self, *a, **kw):
            pass

        def run_command(self, *a, **kw):
            return _FakeResult()

        def kill(self, *a, **kw):
            pass

    with patch("swarm.worker.sandbox.get_sandbox_manager", return_value=_FakeManager()), \
         patch("swarm.worker.sandbox.write_file_to_sandbox"):
        res = _run_l2_in_sandbox("/tmp/proj", "diff\n", "pytest", project_id="p1")
    assert res is None


# ─────────────────────────── F5 / #61 ───────────────────────────

def _diff_for(path: str) -> str:
    return (f"diff --git a/{path} b/{path}\n"
            f"new file mode 100644\n"
            f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,1 @@\n+x\n")


def test_f5_mixed_patch_drops_orphan_segment_keeps_real():
    from swarm.brain.merge_engine import filter_orphan_module_patches

    # ruoyi-alarm 骨架缺失（orphan）；根 pom.xml 真实存在。
    # 单个补丁同时写 ruoyi-alarm/src/Foo.java（孤儿）和 pom.xml（根级，无模块前缀）。
    mixed = _diff_for("ruoyi-alarm/src/Foo.java") + _diff_for("pom.xml")
    subtask_diffs = [("st-X", mixed)]

    def base_exists(_d):
        return False  # ruoyi-alarm 不在 base

    filtered, dropped = filter_orphan_module_patches(
        subtask_diffs, base_module_exists=base_exists, is_multimodule=True)

    assert "ruoyi-alarm" in dropped
    assert "st-X" in dropped["ruoyi-alarm"]
    # 混合补丁没被整条剔（真实 pom.xml 段保留），但孤儿 src 段绝不在输出里
    assert len(filtered) == 1
    kept_text = filtered[0][1]
    assert "ruoyi-alarm/src/Foo.java" not in kept_text
    assert "pom.xml" in kept_text


def test_f5_pure_orphan_patch_dropped_entirely():
    from swarm.brain.merge_engine import filter_orphan_module_patches

    pure = _diff_for("ruoyi-alarm/src/A.java") + _diff_for("ruoyi-alarm/src/B.java")
    filtered, dropped = filter_orphan_module_patches(
        [("st-Y", pure)], base_module_exists=lambda d: False, is_multimodule=True)
    assert filtered == []
    assert "st-Y" in dropped.get("ruoyi-alarm", [])


def test_f5_mixed_patch_with_binary_segment_fails_closed_no_silent_loss():
    """复核整改：混合补丁含二进制段（split_diff_by_file 抽不到路径→静默弃）→ 绝不逐段重组
    静默丢真实二进制资源，而是 fail-closed 整条剔 sid 并记账（终态诚实 PARTIAL）。"""
    from swarm.brain.merge_engine import filter_orphan_module_patches

    binary_seg = ("diff --git a/ruoyi-ui/logo.png b/ruoyi-ui/logo.png\n"
                  "new file mode 100644\n"
                  "index 0000000..abc1234\n"
                  "Binary files /dev/null and b/ruoyi-ui/logo.png differ\n")
    mixed = (_diff_for("ruoyi-alarm/src/Foo.java")  # 孤儿
             + _diff_for("pom.xml")                 # 真实（根级）
             + binary_seg)                          # 真实二进制（split 抽不到路径）
    filtered, dropped = filter_orphan_module_patches(
        [("st-B", mixed)], base_module_exists=lambda _: False, is_multimodule=True)

    # 丢段被检测 → 整条剔并记账，绝不让二进制 logo.png 静默蒸发却显示干净 partial
    assert "st-B" in dropped.get("ruoyi-alarm", [])
    assert filtered == []  # fail-closed：整条不进 filtered（宁 PARTIAL 不静默丢产物）


def test_f5_non_orphan_patch_untouched():
    from swarm.brain.merge_engine import filter_orphan_module_patches

    # ruoyi-alarm 骨架落盘（defined）→ 非孤儿 → 补丁原样保留
    d = _diff_for("ruoyi-alarm/pom.xml") + _diff_for("ruoyi-alarm/src/A.java")
    filtered, dropped = filter_orphan_module_patches(
        [("st-Z", d)], base_module_exists=lambda _: False, is_multimodule=True)
    assert dropped == {}
    assert filtered == [("st-Z", d)]


# ─────────────────────────── F3 / #60 ───────────────────────────

def test_f3_invalid_folded_diff_escalates():
    from swarm.brain.nodes import _escalate_if_folded_diff_invalid

    out: dict = {}
    with patch("swarm.brain.merge_engine.verify_merged_patch_applies",
               return_value=(False, "context mismatch in root pom")), \
         patch("swarm.brain.nodes._get_project_path", return_value="/tmp/proj"), \
         patch("swarm.git_base.resolve_base_ref", return_value="HEAD"):
        ok = _escalate_if_folded_diff_invalid(
            out, "diff --git a/pom.xml b/pom.xml\n", "p1", "base", where="clean-merge")
    assert ok is False
    assert out["failure_escalated"] is True
    assert out["failure_strategy"] == "escalate"
    assert out["l2_passed"] is False
    assert out["verification_failure"] == "merge_apply_invalid"


def test_f3_valid_folded_diff_no_escalate():
    from swarm.brain.nodes import _escalate_if_folded_diff_invalid

    out: dict = {}
    with patch("swarm.brain.merge_engine.verify_merged_patch_applies",
               return_value=(True, "")), \
         patch("swarm.brain.nodes._get_project_path", return_value="/tmp/proj"), \
         patch("swarm.git_base.resolve_base_ref", return_value="HEAD"):
        ok = _escalate_if_folded_diff_invalid(
            out, "diff\n", "p1", "base", where="clean-merge")
    assert ok is True
    assert out == {}  # 合法 → 不写任何 escalate 键


# ─────────────────────────── F7 / #56（既有 X-1 覆盖·回归守卫）─────────────

def test_f7_delivery_apply_failed_maps_to_partial_not_done():
    """交付 apply 全失败(delivery_apply_failed degraded) → terminal_status 判 PARTIAL，非 DONE。
    （X-1 残留治本 delivery_incomplete 已覆盖 DR-03-F7；此为回归守卫。）"""
    from swarm.brain.gates import delivery_incomplete, terminal_status

    st = {"degraded_reasons": ["delivery_apply_failed"]}
    assert delivery_incomplete(st) is True
    assert terminal_status(st) == "PARTIAL"

    st2 = {"degraded_reasons": ["delivery_apply_incomplete"]}
    assert terminal_status(st2) == "PARTIAL"


def test_f7_commit_failed_stays_done_honest_boundary():
    """delivery_commit_failed 不入 delivery_incomplete（apply 已落盘，只是没 commit）→ 仍 DONE。"""
    from swarm.brain.gates import terminal_status

    st = {"degraded_reasons": ["delivery_commit_failed"]}
    assert terminal_status(st) == "DONE"


# ─────────────────────────── F4 / #59 ───────────────────────────

def test_f4_revision_clears_verification_failure():
    """revision() 返回字典必须清 verification_failure（否则上轮 escalate 态污染修订轮冤杀）。"""
    from swarm.brain import nodes

    state = {
        "task_id": "t1",
        "project_id": "p1",
        "plan": None,
        "revision_feedback": "请修订登录逻辑",
        "task_description": "登录",
        "merged_diff": "",
        "verification_failure": "merge_apply_invalid",
        "failure_escalated": True,
        "subtask_results": {},
        "base_commit": "HEAD",
    }

    def _raise():
        raise RuntimeError("no llm in test")

    with patch("swarm.brain.nodes._get_brain_llm", side_effect=_raise), \
         patch("swarm.brain.contract_utils.resolve_plan_conflicts", return_value={}), \
         patch("swarm.brain.nodes._get_project_path", return_value=""):
        out = asyncio.run(nodes.revision(state))
    assert out.get("verification_failure") is None
    assert out.get("failure_escalated") is False
    # F4 附带：三个 verify 三态闸重置为 None（="本轮未跑"哨兵，runner.py:1550 依赖；绝不用 False）
    assert out.get("l2_passed") is None
    assert out.get("l3_passed") is None
    assert out.get("runtime_smoke_passed") is None


# ─────────────────────────── F2 / #58 + F8 / #57 ───────────────────────────

def test_f2_no_test_cmd_carries_compile_unverified_degraded():
    """无测试命令 + project_path 不可得 → 返回 degraded 必含 l2_compile_unverified（不被吞）。"""
    from swarm.brain.nodes import verify as vmod

    state = {
        "task_id": "t", "project_id": "p", "complexity": "COMPLEX",
        "merged_diff": "diff --git a/x b/x\n+y\n",
        "task_description": "d", "acceptance_criteria": [],
        "subtask_results": {}, "plan": None,
    }
    with patch("swarm.brain.nodes._get_project_path", return_value=None), \
         patch.object(vmod, "_l2_test_command_from_criteria", return_value=""):
        out = asyncio.run(vmod._verify_l2_impl(state, []))
    dr = out.get("degraded_reasons") or []
    assert "l2_no_test_executed" in dr
    assert any("l2_compile_unverified" in r for r in dr), dr


def test_f8_sandbox_and_local_both_none_downgrades_to_llm_with_degraded():
    """有 test_cmd 但沙箱+本地双 None（确定性测试均未跑成）→ LLM 兜底判过，
    但必须留 l2_test_downgraded_to_llm degraded（否则'真测试没跑却 l2_passed'不可观测）。"""
    from swarm.brain.nodes import verify as vmod

    async def _fake_llm(*a, **kw):
        return True

    state = {
        "task_id": "t", "project_id": "p", "complexity": "COMPLEX",
        "merged_diff": "diff --git a/x b/x\n+y\n",
        "task_description": "d", "acceptance_criteria": ["pytest"],
        "subtask_results": {}, "plan": None, "base_commit": "HEAD",
    }
    with patch("swarm.brain.nodes._get_project_path", return_value="/tmp/proj"), \
         patch.object(vmod, "_l2_test_command_from_criteria", return_value="pytest"), \
         patch.object(vmod, "_stub_fingerprint_owner_ids", return_value=[]), \
         patch("swarm.brain.integration_review.run_integration_review",
               return_value=(True, [], {})), \
         patch("swarm.brain.nodes._try_l2_sandbox_verify", return_value=None), \
         patch("swarm.brain.nodes._try_l2_local_verify", return_value=None), \
         patch("swarm.brain.nodes._verify_l2_via_llm", side_effect=_fake_llm):
        out = asyncio.run(vmod._verify_l2_impl(state, []))
    assert out.get("l2_passed") is True
    dr = out.get("degraded_reasons") or []
    assert "l2_test_downgraded_to_llm" in dr, dr


def test_f8_negative_llm_verdict_still_carries_degraded():
    """复核整改：沙箱+本地双 None 后 LLM 兜底判【失败】时，l2_test_downgraded_to_llm
    留痕不能被 _l2_failure_state 吞掉（否则无法区分'确定性判失败'vs'纯 LLM 猜失败')。"""
    from swarm.brain.nodes import verify as vmod

    async def _fake_llm_false(*a, **kw):
        return False

    state = {
        "task_id": "t", "project_id": "p", "complexity": "COMPLEX",
        "merged_diff": "diff --git a/x b/x\n+y\n",
        "task_description": "d", "acceptance_criteria": ["pytest"],
        "subtask_results": {}, "plan": None, "base_commit": "HEAD",
    }
    with patch("swarm.brain.nodes._get_project_path", return_value="/tmp/proj"), \
         patch.object(vmod, "_l2_test_command_from_criteria", return_value="pytest"), \
         patch.object(vmod, "_stub_fingerprint_owner_ids", return_value=[]), \
         patch("swarm.brain.integration_review.run_integration_review",
               return_value=(True, [], {})), \
         patch("swarm.brain.nodes._try_l2_sandbox_verify", return_value=None), \
         patch("swarm.brain.nodes._try_l2_local_verify", return_value=None), \
         patch("swarm.brain.nodes._verify_l2_via_llm", side_effect=_fake_llm_false):
        out = asyncio.run(vmod._verify_l2_impl(state, []))
    assert out.get("l2_passed") is False
    dr = out.get("degraded_reasons") or []
    assert "l2_test_downgraded_to_llm" in dr, dr


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
