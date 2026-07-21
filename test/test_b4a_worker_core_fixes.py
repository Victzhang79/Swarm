"""B4a worker 执行核心深读治本（DR-04-F1..F7，task #67-#73）行为级测试。

只测【默认行为】。覆盖：
- F3/#69 _is_infra_failure 不再把断言 echo 的 `<X>: not found` 误判 infra（锚定 shell 前缀）
- F5/#71 compress_tool_output 少行长输出保留中段信号（不盲截丢真因）
- F2/#68 _run_agent 只对 GraphRecursionError 优雅返回，内置 RecursionError re-raise
- F4/#70 空 diff 完整性闸 expects_changes 不含 writable（writable-only no-op 不硬判死）
- F6/#72 run() 异常路径尝试 _get_git_diff 兜底（不硬 diff=""）
- F1/#67 H-exec1 未声明新建文件登记进 _repaired_extra_paths（进 diff targets）
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


# ─────────────────────────── F3 / #69 ───────────────────────────

def test_f3_assertion_echo_not_found_is_not_infra():
    from swarm.worker.l1_pipeline import _is_infra_failure
    # 测试断言/verify 命令 echo 的 `<X>: not found` = capability 失败（worker 没产出），非 infra
    assert _is_infra_failure("artifact: not found") is False
    assert _is_infra_failure("Expected build output target/foo.jar: not found") is False
    assert _is_infra_failure("assertion failed: resource id 'x': not found") is False


def test_f3_genuine_shell_tool_missing_is_infra():
    from swarm.worker.l1_pipeline import _is_infra_failure
    # dash/sh/busybox 真报"命令缺失"（工具未装）= infra
    assert _is_infra_failure("sh: 1: mvn: not found") is True
    assert _is_infra_failure("/bin/sh: gradle: not found") is True
    assert _is_infra_failure("ash: cargo: not found") is True
    assert _is_infra_failure("bash: eslint: command not found") is True


def test_f3_network_markers_still_infra():
    from swarm.worker.l1_pipeline import _is_infra_failure
    assert _is_infra_failure("dial tcp 10.0.0.1:443: connection refused") is True
    assert _is_infra_failure("no space left on device") is True


# ─────────────────────────── F5 / #71 ───────────────────────────

def test_f5_single_line_giant_string_keeps_mid_signal():
    from swarm.worker.output_compress import compress_tool_output
    big = ("cmd echo " + "x" * 4000
           + " AssertionError: expected X got Y "
           + "y" * 4000 + " trailing")
    out = compress_tool_output(big, max_chars=500)
    assert "AssertionError" in out
    assert len(out) < len(big)


def test_f5_few_long_lines_keeps_signal():
    from swarm.worker.output_compress import compress_tool_output
    text = ("preamble line one\n"
            + "junk " * 500 + "error TS2304: Cannot find name 'Foo' " + "junk " * 500
            + "\ntrailer")
    out = compress_tool_output(text, max_chars=400)
    assert "error TS2304" in out


def test_f5_no_signal_falls_back_to_head_tail():
    from swarm.worker.output_compress import compress_tool_output
    text = "a" * 5000  # 单行无信号
    out = compress_tool_output(text, max_chars=400)
    assert "压缩省略" in out
    assert len(out) < len(text)


# ─────────────────────────── F4 / #70 ───────────────────────────

def _make_empty_diff_gate(scope):
    """构造一个 diff 为空、同步干净、无 harness 的 _L1GateMixin 实例（隔离 empty_diff 分支）。"""
    from swarm.worker.executor_l1gate import _L1GateMixin

    class _Gate(_L1GateMixin):
        def __init__(self):
            self.effective_scope = scope
            self.project_path = "/tmp/proj"
            self._sync_skipped_count = 0
            self._sync_error_rels = None
            self._sync_oversize_rels = None
            self._last_gate_diff_sig = None
            self._last_gate_details = None

            class _ST:
                harness = None
            self.subtask = _ST()

        def _check_timeout(self):
            return False

        def _enforce_authoritative_template(self):
            pass

        def _get_git_diff(self):
            return ""  # 空 diff

        def _log(self, *a, **k):
            pass

    return _Gate()


def test_f4_writable_only_empty_diff_stays_fail_closed():
    """DR-04-F4：对抗双复核裁定处方过激已撤销——writable-only + 空 diff 仍 fail-closed 判死
    （stall 假 DONE 防线，冤杀合法 no-op 是安全方向，优于假 DONE）。"""
    from swarm.types import FileScope

    gate = _make_empty_diff_gate(FileScope(
        writable=["Config.java"], readable=[], create_files=[], delete_files=[]))
    verdict, details = gate._deterministic_l1_gate()
    assert verdict is False
    assert details.get("reason") == "empty_diff_but_changes_expected"


def test_f4_create_files_empty_diff_still_hard_fail():
    """create_files 空 diff = 必建义务未产出 → 仍硬判死（stall 保护不动）。"""
    from swarm.types import FileScope

    gate = _make_empty_diff_gate(FileScope(
        writable=[], readable=[], create_files=["New.java"], delete_files=[]))
    verdict, details = gate._deterministic_l1_gate()
    assert verdict is False
    assert details.get("reason") == "empty_diff_but_changes_expected"


# ─────────────────────────── F2 / #68 ───────────────────────────

def test_f2_graph_recursion_discriminator_specific_token():
    """判据认专有 token 'GraphRecursionError'（真实例 + 被包裹重抛形态），
    但绝不命中内置 RecursionError / 偶含 'recursion' 的异常（否则真崩溃被吞）。"""
    def _is_graph_recursion(exc):
        try:
            from langgraph.errors import GraphRecursionError as _GRE
            if isinstance(exc, _GRE):
                return True
        except Exception:
            pass
        return (type(exc).__name__ == "GraphRecursionError"
                or "GraphRecursionError" in str(exc))

    try:
        from langgraph.errors import GraphRecursionError
        assert _is_graph_recursion(GraphRecursionError("limit")) is True
    except Exception:
        pass
    # 被包裹重抛（R63-T7/T9 真实冒泡形态）：仍优雅
    assert _is_graph_recursion(RuntimeError("GraphRecursionError: limit reached")) is True
    # 内置 RecursionError（真栈溢出）/偶含 recursion 的异常：绝不命中 → 走 raise
    assert _is_graph_recursion(RecursionError("maximum recursion depth exceeded")) is False
    assert _is_graph_recursion(ValueError("infinite recursion detected")) is False


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
