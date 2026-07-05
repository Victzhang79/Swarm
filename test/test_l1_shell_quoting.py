#!/usr/bin/env python3
"""L1 shell 命令拼接的文件路径安全引用（shlex.quote）行为锁。

R23-4 续（round25 #9 全局扫尾）：审计只点名 3 处 shlex.quote，实际同模式(把文件路径/pom 路径
裸引号 '{f}'/"{f}" 拼进 shell 命令串)遍布 l1_pipeline 的 go/ts import 修复、py/eslint/checkstyle
lint、maven version/dep 修复。文件名含空格/$/;/'/`() 时裸引号会破坏引号边界 → 命令注入/误执行。

行为断言（非 inspect.getsource）：给含 shell 元字符的文件名，patch 命令执行器捕获真正下发的命令串，
用 shlex.split 反解——安全引用下该文件名仍是【单个完整 token】；裸拼接则会被拆碎/破边界。
"""
from __future__ import annotations

import shlex
from unittest.mock import patch

import swarm.worker.l1_pipeline as l1

_EVIL = "a b$(touch pwned);'.go"  # 空格 + $() + ; + 单引号：裸引号必破边界
_EVIL_TS = "a b$(x);'.ts"


def test_repair_go_quotes_metachar_filename():
    captured = {}

    def _fake(cmd, *a, **k):
        captured["cmd"] = cmd
        return 0, ""

    with patch.object(l1, "_run_l1_command", _fake):
        l1._repair_go("/tmp/proj", [_EVIL], timeout=30)
    cmd = captured["cmd"]
    # 安全引用：文件名经 shlex.split 后仍是单个完整 token（shell 会当一个参数传）
    assert _EVIL in shlex.split(cmd), f"文件名未被安全引用，命令边界被破坏: {cmd}"
    # 注入片段不应作为可独立执行的命令泄漏（分号后不产生裸 token 'touch'）
    assert "touch" not in [t for t in shlex.split(cmd) if t == "touch"]


def test_repair_ts_quotes_metachar_filename():
    captured = {}

    def _fake(cmd, *a, **k):
        captured["cmd"] = cmd
        return 0, ""

    with patch.object(l1, "_run_l1_command", _fake):
        l1._repair_ts("/tmp/proj", [_EVIL_TS], timeout=30)
    cmd = captured["cmd"]
    assert _EVIL_TS in shlex.split(cmd), f"TS 文件名未被安全引用: {cmd}"


def test_compile_files_py_quotes_metachar_filename():
    captured = {}

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return _Proc()

    # _compile_files 的 py 分支用 subprocess.run(cmd, shell=True)
    with patch.object(l1.subprocess, "run", _fake_run):
        l1._compile_files("/tmp/proj", ["a b$;.py"], timeout=30)
    cmd = captured.get("cmd", "")
    assert "a b$;.py" in shlex.split(cmd), f"py 文件名未被安全引用: {cmd}"


def test_module_pom_finder_quotes_metachar_dir():
    """round25 全局扫尾续：_module_pom_for_file 的 d="{d}" shell 变量赋值也须 shlex.quote。"""
    captured = {}

    def _fake(cmd, *a, **k):
        captured["cmd"] = cmd
        return 0, "", ""

    with patch.object(l1, "_run_check_split", _fake):
        l1._module_pom_for_file("/tmp/proj", "a b$;/Foo.java", timeout=15)
    cmd = captured["cmd"]
    # 目录段含空格/$/; → d= 赋值必须安全引用（不再是裸 d="a b$;"，否则 $;、空格破坏 shell 赋值）
    assert shlex.quote("a b$;") in cmd, f"module pom finder 的 d= 未安全引用: {cmd}"


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
