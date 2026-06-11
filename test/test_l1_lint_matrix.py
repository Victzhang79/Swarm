#!/usr/bin/env python3
"""W1 静态闸门：L1 lint 语言分派矩阵 单测。

核心断言：5 语言的 lint 分派在【工具缺失时优雅 skip】(不抛异常、不误判失败)，
工具可用时能检出 error。因 CI/本地未必装 go/cargo/checkstyle，测试不依赖工具存在。
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _tmp_project(files: dict[str, str]) -> str:
    d = tempfile.mkdtemp(prefix="swarm_lint_")
    for rel, content in files.items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
    return d


def test_lint_files_signature_preserved():
    """_lint_files 仍返回 (has_error, message, issues) 三元组。"""
    from swarm.worker.l1_pipeline import _lint_files

    d = _tmp_project({"a.py": "x = 1\n"})
    result = _lint_files(d, ["a.py"], timeout=30)
    assert isinstance(result, tuple) and len(result) == 3, result
    has_error, message, issues = result
    assert isinstance(has_error, bool)
    assert isinstance(issues, list)
    print("  ✅ _lint_files 签名 (has_error, message, issues) 保持")


def test_lint_graceful_skip_when_tool_missing():
    """工具缺失(go/cargo/checkstyle 多数环境没装)时优雅 skip，绝不抛异常/误判。"""
    from swarm.worker.l1_pipeline import _lint_go, _lint_rust, _lint_java

    # Go: 无 go.mod 或无 go 工具 → 不报 error
    dgo = _tmp_project({"main.go": "package main\nfunc main(){}\n"})
    err, msgs, issues = _lint_go(dgo, ["main.go"], timeout=20)
    assert err is False, f"Go 缺工具/无 go.mod 不应报 error: {msgs}"

    # Rust: 无 Cargo.toml 或无 cargo → 不报 error
    drs = _tmp_project({"main.rs": "fn main(){}\n"})
    err, msgs, issues = _lint_rust(drs, ["main.rs"], timeout=20)
    assert err is False, f"Rust 缺工具/无 Cargo.toml 不应报 error: {msgs}"

    # Java: 无 checkstyle → 不报 error
    djava = _tmp_project({"A.java": "class A {}\n"})
    err, msgs, issues = _lint_java(djava, ["A.java"], timeout=20)
    assert err is False, f"Java 缺 checkstyle 不应报 error: {msgs}"
    print("  ✅ Go/Rust/Java lint 工具缺失时优雅 skip（不崩、不误判）")


def test_lint_js_skips_without_config():
    """JS/TS 无 eslint 配置时跳过（对齐既有行为）。"""
    from swarm.worker.l1_pipeline import _lint_js_ts

    d = _tmp_project({"a.ts": "const x: number = 1;\n"})
    err, msgs, issues = _lint_js_ts(d, ["a.ts"], timeout=20)
    assert err is False
    assert any("eslint" in m.lower() for m in msgs)
    print("  ✅ JS/TS 无 eslint 配置时跳过")


def test_lint_dispatch_mixed_languages_no_crash():
    """混合语言文件分派不崩，返回合法三元组。"""
    from swarm.worker.l1_pipeline import _lint_files

    d = _tmp_project({
        "a.py": "x=1\n",
        "b.go": "package main\n",
        "c.rs": "fn main(){}\n",
        "D.java": "class D {}\n",
        "e.ts": "const y=2;\n",
    })
    has_error, message, issues = _lint_files(
        d, ["a.py", "b.go", "c.rs", "D.java", "e.ts"], timeout=30
    )
    assert isinstance(has_error, bool)
    assert isinstance(issues, list)
    print("  ✅ 混合语言分派不崩")


def test_lint_python_detects_syntax_error_if_ruff():
    """若 ruff 可用，Python 语法错误应被检出为 error；ruff 不可用则优雅 skip。"""

    from swarm.worker.l1_pipeline import _find_ruff_bin, _lint_python

    d = _tmp_project({"bad.py": "def broken(:\n    pass\n"})  # 语法错误
    has_error, msgs, issues = _lint_python(d, ["bad.py"], timeout=30)
    if _find_ruff_bin():
        assert has_error, "ruff 可用时应检出语法错误"
        print("  ✅ Python 语法错误被 ruff 检出为 error")
    else:
        assert has_error is False
        print("  ✅ ruff 不可用时优雅 skip（跳过断言）")


def main() -> int:
    print("=== test_l1_lint_matrix ===")
    failed = 0
    for fn in (
        test_lint_files_signature_preserved,
        test_lint_graceful_skip_when_tool_missing,
        test_lint_js_skips_without_config,
        test_lint_dispatch_mixed_languages_no_crash,
        test_lint_python_detects_syntax_error_if_ruff,
    ):
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
            import traceback

            traceback.print_exc()
    if failed:
        print(f"\n{failed} failed")
        return 1
    print("\nAll passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
