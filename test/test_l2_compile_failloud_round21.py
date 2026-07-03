#!/usr/bin/env python3
"""Blocker A (round21 全流程推演·L2 空气闸治本)：VERIFY_L2 集成编译 fail-loud + 多栈检测 + 沙箱优先。

round19 真相(推演取证)：brain host 无 mvn → 旧 `_detect_build_cmd` 把"本机没装工具"和"没有构建文件"
都当 None → 全 reactor 编译【静默跳过】→ issues 空 → L2 假绿放行【没编译过的代码】当生产级交付。

治本：① `_detect_build_cmd_generic` 据构建文件确定命令、不 gate 本机工具(多栈:mvn/gradle/go/cargo/
npm/py)；② run_integration_review 编译优先【沙箱】(compile_runner,按检测栈版本烤的工具链)、退回本机
(仅当本机有该工具)、两者都不行 → **fail-loud 拒绝假绿**(issues 非空→passed=False)。

本套验证纯确定性行为(不需真沙箱)：检测函数 + fail-loud + compile_runner 通/败。
"""
from __future__ import annotations

import importlib.util
import subprocess
import tempfile
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain import integration_review as ir  # noqa: E402


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _repo_with_pom():
    d = tempfile.TemporaryDirectory()
    root = Path(d.name)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@t"], root)
    _git(["config", "user.name", "t"], root)
    (root / "pom.xml").write_text("<project><artifactId>x</artifactId></project>\n")
    (root / "src").mkdir()
    (root / "src/A.java").write_text("class A {}\n")
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "base"], root)
    return d, root


# 修改既有 src/A.java 的合法 modify 补丁（相对 HEAD，git apply 接受）
_NEWFILE_DIFF = (
    "diff --git a/src/A.java b/src/A.java\n"
    "--- a/src/A.java\n"
    "+++ b/src/A.java\n"
    "@@ -1 +1,2 @@\n"
    " class A {}\n"
    "+// added\n"
)


# ── ① 多栈检测：据构建文件出命令，不 gate 本机工具 ──
def test_detect_build_cmd_generic_multistack():
    with tempfile.TemporaryDirectory() as dd:
        r = Path(dd)
        (r / "pom.xml").write_text("x")
        assert ir._detect_build_cmd_generic(str(r)).startswith("mvn ")
    with tempfile.TemporaryDirectory() as dd:
        r = Path(dd); (r / "go.mod").write_text("module x")
        assert "go build" in ir._detect_build_cmd_generic(str(r))
    with tempfile.TemporaryDirectory() as dd:
        r = Path(dd); (r / "Cargo.toml").write_text("[package]")
        assert "cargo build" in ir._detect_build_cmd_generic(str(r))
    with tempfile.TemporaryDirectory() as dd:
        assert ir._detect_build_cmd_generic(dd) is None  # 无构建文件 → None(合理跳过)
    print("  ✅ ① _detect_build_cmd_generic 多栈、不 gate 本机工具")


# ── ② ★核心★ 无沙箱 + 本机无工具链 → fail-loud 拒绝假绿 ──
def test_failloud_when_no_toolchain(monkeypatch):
    d, root = _repo_with_pom()
    with d:
        monkeypatch.setattr(ir, "_local_tool_available", lambda cmd: False)  # 本机无 mvn
        ok, issues, details = ir.run_integration_review(
            str(root), _NEWFILE_DIFF, None, compile_runner=None,
        )
        assert ok is False, "无法编译却放行=假绿,必须 fail-loud"
        assert details.get("compile_unverified") is True
        assert any("拒绝假绿" in i for i in issues), issues
    print("  ✅ ② 无沙箱+本机无工具链 → fail-loud(passed=False,拒绝假绿)")


# ── ③ 沙箱编译器通过 → L2 通过 ──
def test_sandbox_compile_pass(monkeypatch):
    d, root = _repo_with_pom()
    with d:
        monkeypatch.setattr(ir, "_local_tool_available", lambda cmd: False)
        runner = lambda build_cmd: (True, True, "BUILD SUCCESS")  # noqa: E731
        ok, issues, details = ir.run_integration_review(
            str(root), _NEWFILE_DIFF, None, compile_runner=runner,
        )
        assert ok is True, issues
        assert details.get("compile_env") == "sandbox"
        assert details.get("compile_ok") is True
    print("  ✅ ③ 沙箱编译通过 → L2 通过(compile_env=sandbox)")


# ── ④ 沙箱编译失败 → L2 失败(真实编译错误暴露) ──
def test_sandbox_compile_fail(monkeypatch):
    d, root = _repo_with_pom()
    with d:
        monkeypatch.setattr(ir, "_local_tool_available", lambda cmd: False)
        runner = lambda build_cmd: (True, False, "cannot find symbol XyZ")  # noqa: E731
        ok, issues, details = ir.run_integration_review(
            str(root), _NEWFILE_DIFF, None, compile_runner=runner,
        )
        assert ok is False
        assert any("集成编译失败" in i for i in issues), issues
    print("  ✅ ④ 沙箱编译失败 → L2 失败(暴露真实集成编译错误)")


# ── ⑤ 沙箱不可用(ran=False)但本机有工具 → 退回本机编译 ──
def test_fallback_to_local_when_sandbox_unavailable(monkeypatch):
    d, root = _repo_with_pom()
    with d:
        monkeypatch.setattr(ir, "_local_tool_available", lambda cmd: True)
        monkeypatch.setattr(ir, "_run_cmd", lambda p, c, timeout=0: (True, "ok"))
        runner = lambda build_cmd: (False, False, "")  # 沙箱不可用  # noqa: E731
        ok, issues, details = ir.run_integration_review(
            str(root), _NEWFILE_DIFF, None, compile_runner=runner,
        )
        assert ok is True, issues
        assert details.get("compile_env") == "local"
    print("  ✅ ⑤ 沙箱不可用→退回本机编译(compile_env=local)")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
