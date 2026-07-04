"""A3: 行为测试锁 Go/Rust/Java line-based lint 的【解析路径】。

既有 test_l1_lint_matrix 只覆盖工具缺失时的 skip；本文件 mock _run_check_split 强制走
解析分支，锁住 issue 抽取(file/line/code/severity)+ has_error 语义，保护 A3 抽公共
line-based 模板的重构。行为测试——断言返回值不断言实现结构。
"""
from __future__ import annotations

import swarm.worker.l1_pipeline as lp


def _force_tools(monkeypatch):
    # 让工具在场（跳过 skip 分支）、manifest 在场，走真正的执行+解析路径。
    monkeypatch.setattr(lp, "_find_tool", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(lp, "_manifest_present", lambda manifests, project_path: True)
    monkeypatch.setattr(lp, "_sandbox_ctx", lambda: None)


class TestGoParse:
    def test_parses_file_line_error(self, monkeypatch):
        _force_tools(monkeypatch)
        monkeypatch.setattr(lp, "_run_check_split",
                            lambda cmd, pp, timeout=60: (1, "", "main.go:10:2: undefined: Foo"))
        err, msgs, issues = lp._lint_go("/proj", ["main.go"], timeout=20)
        assert err is True
        assert issues[0]["file"] == "main.go"
        assert issues[0]["line"] == 10
        assert issues[0]["code"] == "govet"
        assert issues[0]["severity"] == "error"

    def test_infra_failure_skips_not_error(self, monkeypatch):
        _force_tools(monkeypatch)
        monkeypatch.setattr(lp, "_run_check_split",
                            lambda cmd, pp, timeout=60: (1, "go: command not found", ""))
        err, msgs, issues = lp._lint_go("/proj", ["main.go"], timeout=20)
        assert err is False
        assert issues == []

    def test_timeout_rc124(self, monkeypatch):
        _force_tools(monkeypatch)
        monkeypatch.setattr(lp, "_run_check_split", lambda cmd, pp, timeout=60: (124, "", ""))
        err, msgs, issues = lp._lint_go("/proj", ["main.go"], timeout=20)
        assert err is False
        assert any("超时" in m for m in msgs)


class TestRustParse:
    def test_parses_error_line(self, monkeypatch):
        _force_tools(monkeypatch)
        monkeypatch.setattr(lp, "_run_check_split",
                            lambda cmd, pp, timeout=60: (1, "", "src/main.rs:2:5: error[E0425]: cannot find value"))
        err, msgs, issues = lp._lint_rust("/proj", ["src/main.rs"], timeout=20)
        assert err is True
        assert issues[0]["file"] == "src/main.rs"
        assert issues[0]["line"] == 2
        assert issues[0]["code"] == "clippy"

    def test_summary_lines_skipped_no_issue(self, monkeypatch):
        _force_tools(monkeypatch)
        # 仅摘要行(无 error[/warning[)→ 不产 issue、不报 error（rust: has_error 仅当有 issue）
        monkeypatch.setattr(lp, "_run_check_split",
                            lambda cmd, pp, timeout=60: (1, "", "warning: generated 3 warnings\nerror: aborting due to previous"))
        err, msgs, issues = lp._lint_rust("/proj", ["src/main.rs"], timeout=20)
        assert issues == []
        assert err is False


class TestJavaParse:
    def test_parses_error_bracket(self, monkeypatch):
        _force_tools(monkeypatch)
        monkeypatch.setattr(lp, "_run_check_split",
                            lambda cmd, pp, timeout=60: (1, "", "[ERROR] /src/A.java:3:1: Missing brace"))
        err, msgs, issues = lp._lint_java("/proj", ["/src/A.java"], timeout=20)
        assert err is True
        assert issues[0]["file"] == "/src/A.java"
        assert issues[0]["line"] == 3
        assert issues[0]["code"] == "checkstyle"

    def test_infra_failure_skips(self, monkeypatch):
        _force_tools(monkeypatch)
        monkeypatch.setattr(lp, "_run_check_split",
                            lambda cmd, pp, timeout=60: (127, "checkstyle: command not found", ""))
        err, msgs, issues = lp._lint_java("/proj", ["/src/A.java"], timeout=20)
        assert err is False
        assert issues == []
