#!/usr/bin/env python3
"""CTO 技术债 A-P1-09/10/11：Worker L1 三项 特征化单测。

- A-P1-09：Go/Rust/checkstyle lint 命中【基础设施/工具瞬时错误】标记 → skip 非 error，
  不把"无网拉依赖/工具缺失"误判成代码能力失败而触发错误降级。
- A-P1-10：compile/lint 走沙箱优先；无沙箱时 _manifest_present / _run_check_split 本地兜底。
- A-P1-11：codegraph 查询失败的符号判 unverified(无法核实)，绝不误判 absent(不存在)。
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ── A-P1-09：基础设施错误识别 ──

def test_is_infra_failure_matches_transient():
    from swarm.worker.l1_pipeline import _is_infra_failure

    assert _is_infra_failure("go: downloading github.com/foo: dial tcp: i/o timeout")
    assert _is_infra_failure("error: failed to download `serde v1.0`")
    assert _is_infra_failure("Blocking waiting for file lock on package cache")
    assert _is_infra_failure("bash: checkstyle: command not found")
    assert _is_infra_failure("no space left on device")
    # 真代码错误不应误判为 infra
    assert not _is_infra_failure("main.go:10:2: undefined: fooBar")
    assert not _is_infra_failure("error[E0425]: cannot find value `x` in this scope")
    assert not _is_infra_failure("")
    print("  ✅ _is_infra_failure 只命中瞬时基础设施/工具错误，不误伤真代码错误")


def test_lint_go_infra_error_skips_not_error(monkeypatch_like=None):
    """go vet 输出含 infra 标记 → has_error=False(skip)，不当作代码失败。"""
    from swarm.worker import l1_pipeline as lp

    orig_ctx, orig_mani, orig_tool, orig_run = (
        lp._sandbox_ctx, lp._manifest_present, lp._find_tool, lp._run_check_split,
    )
    try:
        lp._sandbox_ctx = lambda: None
        lp._manifest_present = lambda manifests, project_path: True
        lp._find_tool = lambda name: "/usr/bin/go"
        lp._run_check_split = lambda cmd, pp, timeout=60: (
            1, "", "go: downloading github.com/x/y: dial tcp 1.2.3.4:443: i/o timeout"
        )
        has_error, msgs, issues = lp._lint_go("/proj", ["main.go"], timeout=5)
        assert has_error is False, f"infra 错误不应判 error: {msgs}"
        assert not issues
        assert any("基础设施" in m for m in msgs), msgs
    finally:
        lp._sandbox_ctx, lp._manifest_present, lp._find_tool, lp._run_check_split = (
            orig_ctx, orig_mani, orig_tool, orig_run,
        )
    print("  ✅ _lint_go infra 输出 → skip 非 error")


def test_lint_go_real_error_still_flagged():
    """go vet 输出真编译/vet 错误 → 仍判 has_error=True。"""
    from swarm.worker import l1_pipeline as lp

    orig_ctx, orig_mani, orig_tool, orig_run = (
        lp._sandbox_ctx, lp._manifest_present, lp._find_tool, lp._run_check_split,
    )
    try:
        lp._sandbox_ctx = lambda: None
        lp._manifest_present = lambda manifests, project_path: True
        lp._find_tool = lambda name: "/usr/bin/go"
        lp._run_check_split = lambda cmd, pp, timeout=60: (
            1, "", "main.go:10:2: undefined: fooBar"
        )
        has_error, msgs, issues = lp._lint_go("/proj", ["main.go"], timeout=5)
        assert has_error is True, f"真 vet 错误应判 error: {msgs}"
        assert issues and issues[0]["file"] == "main.go"
    finally:
        lp._sandbox_ctx, lp._manifest_present, lp._find_tool, lp._run_check_split = (
            orig_ctx, orig_mani, orig_tool, orig_run,
        )
    print("  ✅ _lint_go 真错误仍被检出")


def test_lint_rust_infra_error_skips():
    from swarm.worker import l1_pipeline as lp

    orig_ctx, orig_mani, orig_tool, orig_run = (
        lp._sandbox_ctx, lp._manifest_present, lp._find_tool, lp._run_check_split,
    )
    try:
        lp._sandbox_ctx = lambda: None
        lp._manifest_present = lambda manifests, project_path: True
        lp._find_tool = lambda name: "/usr/bin/cargo"
        lp._run_check_split = lambda cmd, pp, timeout=60: (
            101, "", "error: failed to download `serde`\nspurious network error"
        )
        has_error, msgs, issues = lp._lint_rust("/proj", ["main.rs"], timeout=5)
        assert has_error is False, f"infra 错误不应判 error: {msgs}"
        assert any("基础设施" in m for m in msgs), msgs
    finally:
        lp._sandbox_ctx, lp._manifest_present, lp._find_tool, lp._run_check_split = (
            orig_ctx, orig_mani, orig_tool, orig_run,
        )
    print("  ✅ _lint_rust infra 输出 → skip 非 error")


# ── A-P1-10：sandbox-first helpers 本地兜底 ──

def test_manifest_present_local_fallback(tmp_path_like=None):
    import os
    import tempfile

    from swarm.worker import l1_pipeline as lp

    d = tempfile.mkdtemp(prefix="swarm_mani_")
    with open(os.path.join(d, "go.mod"), "w") as f:
        f.write("module x\n")
    assert lp._manifest_present(("go.mod",), d) is True
    assert lp._manifest_present(("Cargo.toml",), d) is False
    print("  ✅ _manifest_present 无沙箱本地兜底正确")


def test_run_check_split_local_returns_triple():
    from swarm.worker import l1_pipeline as lp

    rc, out, err = lp._run_check_split("echo hello", "/tmp", timeout=5)
    assert rc == 0 and "hello" in out, (rc, out, err)
    print("  ✅ _run_check_split 无沙箱本地执行返回 (rc, stdout, stderr)")


# ── A-P1-11：查询失败 vs 不存在 ──

def test_symbol_hint_query_failed_is_unverified():
    from swarm.worker.symbol_resolver import MissingSymbol, build_symbol_hints

    missing = [MissingSymbol(kind="class", name="FooService")]
    hints = build_symbol_hints(missing, resolved={}, plan_create_files=[],
                               query_failed={"FooService"})
    assert len(hints) == 1
    assert hints[0].status == "unverified", hints[0].status
    assert "无法核实" in hints[0].message
    assert "不存在" not in hints[0].message.replace("「不存在」", "")
    print("  ✅ 查询失败符号 → status=unverified(无法核实)")


def test_symbol_hint_truly_absent_unchanged():
    from swarm.worker.symbol_resolver import MissingSymbol, build_symbol_hints

    missing = [MissingSymbol(kind="class", name="BarService")]
    hints = build_symbol_hints(missing, resolved={}, plan_create_files=[], query_failed=set())
    assert hints[0].status == "absent"
    assert "不存在" in hints[0].message
    print("  ✅ 真查空符号 → status=absent(向后兼容)")


def test_resolve_and_format_query_failure_not_absent():
    """indexer 抛异常 → 不渲染成"不存在"，而是"无法核实"。"""
    from swarm.worker.symbol_resolver import resolve_and_format

    class _BoomIndexer:
        async def query_symbols_by_name(self, project_id, name, symbol_type=None):
            raise RuntimeError("db connection lost")

    build_output = "Foo.java:3: error: cannot find symbol\n  symbol: class FooService\n"
    text = asyncio.run(resolve_and_format(build_output, "proj-1", _BoomIndexer(), []))
    assert "无法核实" in text, text
    assert "在整个项目中不存在" not in text, text
    print("  ✅ resolve_and_format 查询失败渲染为「无法核实」而非「不存在」")


def main() -> int:
    failed = 0
    for fn in (
        test_is_infra_failure_matches_transient,
        test_lint_go_infra_error_skips_not_error,
        test_lint_go_real_error_still_flagged,
        test_lint_rust_infra_error_skips,
        test_manifest_present_local_fallback,
        test_run_check_split_local_returns_triple,
        test_symbol_hint_query_failed_is_unverified,
        test_symbol_hint_truly_absent_unchanged,
        test_resolve_and_format_query_failure_not_absent,
    ):
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
            import traceback

            traceback.print_exc()
    print(f"\n{'FAIL' if failed else 'All passed'}: {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
