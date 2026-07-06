"""P1-1/P1-2（CODEWALK_AUDIT_2026-07-06 批2）：安全扫描解析器两处错格式。

P1-1：_dep_go 只认旧顶层 OSV 键——现代 govulncheck -json 是 JSONL 流，finding 在
{"finding":{...}} 对象里 → 现代输出恒 0 发现且 _mark_ran 已置位（伪装"扫过了、
没漏洞"，安全扫描 fail-open）。修：双格式解析（现代 finding + 旧顶层 OSV 兼容），
同一 OSV 的 module/package/function 多层 trace 按 OSV 去重、保留带源码位置的一条。
P1-2：semgrep 解析把 start.col 当 line 写入 SecurityFinding（错取字段）。修取 start.line。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import swarm.worker.security_scan as scan
from swarm.worker.security_scan import Severity

# hunter #4：patch scan 模块级 shutil 引用而非全局 shutil.which（防未来 xdist 并行互踩）
_FAKE_SHUTIL = SimpleNamespace(which=lambda n: "/usr/bin/" + n)

_MODERN_GOVULN = "\n".join([
    json.dumps({"config": {"protocol_version": "v1.0.0", "scanner_name": "govulncheck"}}),
    json.dumps({"progress": {"message": "Scanning your code and P dependencies..."}}),
    json.dumps({"osv": {"id": "GO-2024-2687", "summary": "HTTP/2 CONTINUATION flood"}}),
    # 同一 OSV 两层 trace：module 级（无 position）+ function 级（带 position）→ 应去重保留后者
    json.dumps({"finding": {"osv": "GO-2024-2687", "fixed_version": "v0.23.0",
                            "trace": [{"module": "golang.org/x/net", "version": "v0.19.0"}]}}),
    json.dumps({"finding": {"osv": "GO-2024-2687", "fixed_version": "v0.23.0",
                            "trace": [{"module": "golang.org/x/net",
                                       "package": "golang.org/x/net/http2",
                                       "function": "ReadFrame",
                                       "position": {"filename": "h2_bundle.go",
                                                    "line": 42, "column": 5}}]}}),
])

_OLD_GOVULN = json.dumps({"OSV": "GO-2021-0113", "severity": "high",
                          "trace": [{"filename": "main.go", "line": 10}]})


def _run_dep_go(output: str):
    with patch.object(scan, "shutil", _FAKE_SHUTIL), \
         patch.object(scan, "_run_tool", lambda *a, **k: (0, output, "")):
        return scan._dep_go("/tmp/proj")


def test_dep_go_parses_modern_finding_format():
    findings = _run_dep_go(_MODERN_GOVULN)
    assert len(findings) == 1, f"现代 finding 格式应解析出 1 条（按 OSV 去重）: {findings}"
    f = findings[0]
    assert f.rule_id == "GO-2024-2687"
    assert f.file == "h2_bundle.go" and f.line == 42, "应保留带源码位置的最具体 trace"
    assert "v0.23.0" in f.recommendation, "fixed_version 应进修复建议"
    assert f.tool == "govulncheck"


def test_dep_go_old_format_still_parsed():
    findings = _run_dep_go(_OLD_GOVULN)
    assert len(findings) == 1
    assert findings[0].rule_id == "GO-2021-0113"
    assert findings[0].severity == Severity.HIGH
    assert findings[0].file == "main.go" and findings[0].line == 10


def test_dep_go_old_format_empty_trace_no_indexerror():
    """hunter #1：trace 键存在但为 [] 时，[{}] 默认值不生效 → 旧代码 IndexError 炸扫描
    并连坐丢弃已收集的 SAST 发现。应降级为空坐标。"""
    out = json.dumps({"OSV": "GO-2022-0433", "severity": "low", "trace": []})
    findings = _run_dep_go(out)
    assert len(findings) == 1
    assert findings[0].rule_id == "GO-2022-0433"
    assert findings[0].file == "" and findings[0].line == 0


def test_dep_go_malformed_position_does_not_abort_scan():
    """reviewer MEDIUM：position 非 dict / line 非 int 的畸形行不许抛异常炸整个扫描
    （_mark_ran 已置位，炸了=零发现+scanner_ran=True 的 fail-open）。"""
    out = "\n".join([
        json.dumps({"finding": {"osv": "GO-2099-0001",
                                "trace": [{"module": "m", "position": "bad_string"}]}}),
        json.dumps({"finding": {"osv": "GO-2099-0002",
                                "trace": [{"module": "m",
                                           "position": {"filename": "f.go", "line": "not-int"}}]}}),
    ])
    findings = _run_dep_go(out)
    ids = {f.rule_id for f in findings}
    assert ids == {"GO-2099-0001", "GO-2099-0002"}, f"畸形坐标应降级为空坐标而非丢弃/抛异常: {findings}"
    assert all(f.line == 0 for f in findings)


def test_dep_go_config_progress_only_yields_nothing():
    out = "\n".join([
        json.dumps({"config": {"scanner_name": "govulncheck"}}),
        json.dumps({"progress": {"message": "no vulns"}}),
    ])
    assert _run_dep_go(out) == []


_SEMGREP_OUT = json.dumps({"results": [{
    "check_id": "javascript.lang.security.audit.eval",
    "path": "a.js",
    "start": {"line": 42, "col": 7},
    "end": {"line": 42, "col": 20},
    "extra": {"severity": "ERROR", "message": "eval detected", "fix": ""},
}]})


def test_semgrep_line_uses_start_line_not_col():
    with patch.object(scan, "shutil", _FAKE_SHUTIL), \
         patch.object(scan, "_run_tool", lambda *a, **k: (0, _SEMGREP_OUT, "")):
        findings = scan._sast_node("/tmp/proj")
    assert len(findings) == 1
    assert findings[0].line == 42, f"应取 start.line=42 而非 start.col=7，实际 {findings[0].line}"
    assert findings[0].severity == Severity.HIGH
