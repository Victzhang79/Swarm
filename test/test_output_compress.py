#!/usr/bin/env python3
"""工具输出智能压缩单元测试（借鉴 headroom 思路）。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_short_output_unchanged():
    """短输出原样返回，不压缩。"""
    from swarm.worker.output_compress import compress_tool_output

    text = "all tests passed\n3 passed in 0.5s"
    assert compress_tool_output(text) == text
    print("  ✅ 短输出原样返回")


def test_empty_output():
    """空输出安全处理。"""
    from swarm.worker.output_compress import compress_tool_output

    assert compress_tool_output("") == ""
    assert compress_tool_output(None) is None  # type: ignore[arg-type]
    print("  ✅ 空输出安全处理")


def test_failure_signal_preserved_at_tail():
    """关键失败信号在末尾时必须保留（headroom 核心场景）。"""
    from swarm.worker.output_compress import compress_tool_output

    # 模拟 pytest：大量收集噪声在前，FAILED 摘要在尾
    noise = "\n".join(f"collecting test_{i} ... ok" for i in range(200))
    tail = (
        "test_critical.py::test_boom FAILED\n"
        "E       AssertionError: expected 42 got 0\n"
        "=== 1 failed, 199 passed in 3.2s ==="
    )
    text = noise + "\n" + tail
    result = compress_tool_output(text, max_chars=800)

    assert "FAILED" in result, "失败信号必须保留"
    assert "AssertionError" in result, "断言错误必须保留"
    assert "1 failed" in result, "pytest 摘要必须保留"
    assert len(result) < len(text), "应当被压缩"
    assert "省略" in result, "应有省略占位标记"
    print("  ✅ 末尾失败信号被保留（vs 硬截断会丢失）")


def test_hard_truncation_would_lose_signal():
    """对比验证：旧的硬截断 [:N] 会丢失末尾信号，新压缩不会。"""
    from swarm.worker.output_compress import compress_tool_output

    noise = "\n".join(f"line {i} boring collection log" for i in range(300))
    text = noise + "\nTraceback (most recent call last)\nValueError: critical bug"

    old_truncated = text[:800]  # 模拟旧行为
    assert "ValueError" not in old_truncated, "前置条件：硬截断确实丢失信号"

    new_compressed = compress_tool_output(text, max_chars=800)
    assert "ValueError" in new_compressed, "新压缩保留关键信号"
    assert "Traceback" in new_compressed
    print("  ✅ 对比验证：硬截断丢信号，智能压缩保留")


def test_traceback_lines_preserved():
    """Python traceback 的 File 行被识别保留。"""
    from swarm.worker.output_compress import compress_tool_output

    noise = "\n".join(f"info {i}" for i in range(100))
    tb = (
        'Traceback (most recent call last)\n'
        '  File "app.py", line 42, in main\n'
        '    raise RuntimeError("boom")\n'
        'RuntimeError: boom'
    )
    text = noise + "\n" + tb
    result = compress_tool_output(text, max_chars=500)
    assert "RuntimeError" in result
    assert 'File "app.py"' in result
    print("  ✅ traceback File 行被保留")


def test_single_giant_line_fallback():
    """单行巨串退化为头尾字符窗口。"""
    from swarm.worker.output_compress import compress_tool_output

    text = "x" * 5000
    result = compress_tool_output(text, max_chars=1000)
    assert len(result) < len(text)
    assert "省略" in result
    print("  ✅ 单行巨串头尾窗口截断")


def test_no_signal_returns_head_tail():
    """无信号的长文本保留头尾结构。"""
    from swarm.worker.output_compress import compress_tool_output

    lines = "\n".join(f"benign line number {i}" for i in range(500))
    result = compress_tool_output(lines, max_chars=600)
    assert "benign line number 0" in result, "头部保留"
    assert "benign line number 499" in result, "尾部保留"
    assert "省略" in result
    print("  ✅ 无信号长文本保留头尾")


def main() -> int:
    print("\n🧪 工具输出智能压缩 单元测试\n")
    tests = [
        test_short_output_unchanged,
        test_empty_output,
        test_failure_signal_preserved_at_tail,
        test_hard_truncation_would_lose_signal,
        test_traceback_lines_preserved,
        test_single_giant_line_fallback,
        test_no_signal_returns_head_tail,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n📊 结果: {passed} 通过, {failed} 失败\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
