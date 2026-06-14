"""I4 单测：_l1_failure_digest 提取确定性失败证据 + _build_fix_prompt 优先用它。

fix prompt 过去只带 LLM 自报 verify_result；改为优先注入确定性 pipeline 抓到的真实失败
信号（compile_message/lint/build_output/scope，均已压缩），让修复有的放矢、不灌全量输出。
纯方法测试，不触沙箱/网络。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.worker.executor import WorkerExecutor
from swarm.types import FileScope, SubTask


def _executor():
    st = SubTask(id="st-1", description="x", scope=FileScope(writable=["a.py"]))
    return WorkerExecutor(subtask=st)


def test_digest_compile_failure():
    ex = _executor()
    d = ex._l1_failure_digest({
        "l1_2_compile_ok": False,
        "compile_message": "a.py:3: SyntaxError: invalid syntax",
    })
    assert "编译失败" in d and "SyntaxError" in d
    print("  ✅ 编译失败摘要提取")


def test_digest_scope_violation():
    ex = _executor()
    d = ex._l1_failure_digest({"scope_violations": ["evil.py"]})
    assert "scope 越权" in d and "evil.py" in d
    print("  ✅ scope 越权摘要提取")


def test_digest_lint_failure():
    ex = _executor()
    d = ex._l1_failure_digest({"lint": {"status": "error", "message": "F401 unused import"}})
    assert "lint 失败" in d and "F401" in d
    print("  ✅ lint 失败摘要提取")


def test_digest_empty_when_no_failure():
    ex = _executor()
    assert ex._l1_failure_digest({}) == ""
    assert ex._l1_failure_digest({"l1_2_compile_ok": True}) == ""
    print("  ✅ 无失败信号时摘要为空")


def test_fix_prompt_prefers_deterministic():
    """有确定性证据时，fix prompt 用它而非 LLM 自报。"""
    ex = _executor()
    p = ex._build_fix_prompt("我觉得可能编译有点问题吧", {
        "l1_2_compile_ok": False, "compile_message": "REAL: cannot find symbol Foo",
    })
    assert "REAL: cannot find symbol Foo" in p
    assert "我觉得可能编译有点问题吧" not in p  # 确定性证据优先，不带模糊自报
    print("  ✅ fix prompt 优先确定性证据")


def test_fix_prompt_fallback_to_self_report():
    """无确定性证据时，回退 LLM 自报（不丢信息）。"""
    ex = _executor()
    p = ex._build_fix_prompt("test_foo 失败：断言不通过", {})
    assert "test_foo 失败" in p
    print("  ✅ 无确定性证据回退自报")


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v", "-s"]))
