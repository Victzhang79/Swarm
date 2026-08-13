#!/usr/bin/env python3
"""30 号文批5 C-2 锁：secret 扫描【并集去重 + 本仓分档】。

被锁缺陷：原 `_run_secret_scan` 是「提前 return」兜底链——gitleaks 在场时 21 条内置
CRITICAL 正则整块不跑，密钥档位外包给外部工具 severity 字符串（缺省 "high"），同一条
真 provider key 从 CRITICAL(阻断) 变 HIGH(放行)。治法：三路并集 + 外部 finding 逐条
行级重分档（_strongest_secret_match；本仓认不出的显式落 CRITICAL，与 CVE 的 MEDIUM
缺省不同消费契约）+ 按 (file, line, rule_id) 去重取最强档。
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

import pytest

import swarm.worker.security_scan as ss
from swarm.types import SecurityFinding, Severity

# 命中内置 CRITICAL 档（OpenAI project key：`sk-proj-[A-Za-z0-9_-]{20,}`）
# ★碎片化拼接★：完整字面量会被 ECC pre-commit 的密钥扫描当「generic credential
# assignment」拦下（批1 同款三连胜，夹具非真密钥）。
PROJ_KEY_LINE = 'OPENAI_KEY = "sk-proj-' + 'AbCdEfGhIjKlMnOpQrStUvWxYz1234567890"'
# 命中内置 HIGH 档（Unquoted secret assignment）
HIGH_LINE = 'password = "My' + 'Secret12345"'
# 内置表认不出的形态（无任何 pattern 命中）
ALIEN_LINE = 'MAGIC = "xyzzy' + 'plugh42"'


def _install_fake_gitleaks(monkeypatch: pytest.MonkeyPatch, report: list[dict]) -> None:
    """假装 gitleaks 在场并按 report 出结果；其余外部工具全缺席。"""
    monkeypatch.setattr(
        ss.shutil, "which", lambda tool: "/usr/bin/gitleaks" if tool == "gitleaks" else None
    )

    def _fake_run_tool(cmd, cwd=None, timeout=None):
        rp = cmd[cmd.index("--report-path") + 1]
        Path(rp).write_text(json.dumps(report), encoding="utf-8")
        return 1, "", ""  # gitleaks rc=1 = 有泄露

    monkeypatch.setattr(ss, "_run_tool", _fake_run_tool)


def _mk_tmp_project(lines: str, fname: str = "config.py") -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory()
    (Path(tmp.name) / fname).write_text(lines + "\n", encoding="utf-8")
    return tmp


# ─── 1. 承重锁：gitleaks 在场报 HIGH 的同一行，最终档仍 CRITICAL（对照 B 形状）───

def test_gitleaks_present_same_line_stays_critical(monkeypatch):
    """对照 B：gitleaks 在场、对同一行报 high（其 severity 缺省语义）——
    最终该 finding 被本仓重分档升回 CRITICAL，阻断判据成立。"""
    _install_fake_gitleaks(monkeypatch, [
        {"RuleID": "openai-api-key", "File": "config.py", "StartLine": 1, "severity": "high"},
    ])
    with _mk_tmp_project(PROJ_KEY_LINE) as tmp:
        findings, _block, _details = ss.run_security_scan(tmp, "python", block_severity="critical")
    secret = [f for f in findings if f.category == "secret"]
    gl = [f for f in secret if f.tool == "gitleaks"]
    assert gl, f"gitleaks finding 应存在: {[(f.tool, f.title) for f in secret]}"
    assert all(f.severity == Severity.CRITICAL for f in gl), \
        f"gitleaks 报 HIGH 的 provider key 行应被本仓重分档升回 CRITICAL: {[(f.rule_id, f.severity) for f in gl]}"
    assert any(
        ss._severity_gte(f.severity, "critical") for f in secret
    ), "secret 类应存在 >= critical 的 finding（旧码此处只有 HIGH ⇒ 放行）"


# ─── 2. 并集锁：gitleaks 在场时内置正则仍跑（不再被提前 return 顶掉）───

def test_gitleaks_present_builtin_still_in_toolset(monkeypatch):
    _install_fake_gitleaks(monkeypatch, [
        {"RuleID": "openai-api-key", "File": "config.py", "StartLine": 1, "severity": "high"},
    ])
    with _mk_tmp_project(PROJ_KEY_LINE) as tmp:
        findings, _b, _d = ss.run_security_scan(tmp, "python", block_severity="critical")
    tools = {f.tool for f in findings if f.category == "secret"}
    assert "builtin-regex" in tools, \
        f"gitleaks 在场时内置正则被顶掉（C-2 原洞复发）: {tools}"
    assert "gitleaks" in tools


# ─── 3. 本仓认不出的外部形态 → 显式 CRITICAL（非 CVE 的 MEDIUM 缺省）+ WARNING 留痕 ───

def test_unrecognized_external_form_explicit_critical(monkeypatch, caplog):
    _install_fake_gitleaks(monkeypatch, [
        {"RuleID": "future-token-2027", "File": "magic.py", "StartLine": 1},  # severity 缺席
    ])
    with _mk_tmp_project(ALIEN_LINE, fname="magic.py") as tmp:
        with caplog.at_level(logging.WARNING, logger="swarm.worker.security_scan"):
            findings = ss._run_secret_scan(tmp)
    alien = [f for f in findings if f.rule_id == "future-token-2027"]
    assert len(alien) == 1
    assert alien[0].severity == Severity.CRITICAL, \
        f"本仓认不出的外部密钥形态应显式落 CRITICAL（密钥泄露不可撤销）: {alien[0].severity}"
    msgs = [r.message for r in caplog.records if "认不出" in r.message]
    assert len(msgs) == 1, f"每趟扫描恰一条汇总 WARNING（hunter LOW3 防刷屏）: {msgs}"
    assert "pattern-miss=1" in msgs[0], f"根因分账机读可辨（hunter LOW1）: {msgs[0]}"


def test_unrecognized_includes_unreadable_line(monkeypatch):
    """行号缺席（0）同样走「认不出 → CRITICAL」——fail-closed 方向不豁免。"""
    _install_fake_gitleaks(monkeypatch, [
        {"RuleID": "no-line-info", "File": "magic.py", "StartLine": 0},
    ])
    with _mk_tmp_project(ALIEN_LINE, fname="magic.py") as tmp:
        findings = ss._run_secret_scan(tmp)
    alien = [f for f in findings if f.rule_id == "no-line-info"]
    assert len(alien) == 1 and alien[0].severity == Severity.CRITICAL


def test_external_critical_unrecognized_still_traced(monkeypatch, caplog):
    """hunter LOW5：外部已报 CRITICAL + 本仓未命中——档位不变但汇总留痕不打折。"""
    _install_fake_gitleaks(monkeypatch, [
        {"RuleID": "alien-critical", "File": "magic.py", "StartLine": 1, "severity": "critical"},
    ])
    with _mk_tmp_project(ALIEN_LINE, fname="magic.py") as tmp:
        with caplog.at_level(logging.WARNING, logger="swarm.worker.security_scan"):
            findings = ss._run_secret_scan(tmp)
    alien = [f for f in findings if f.rule_id == "alien-critical"]
    assert len(alien) == 1 and alien[0].severity == Severity.CRITICAL
    assert any("认不出" in r.message and "pattern-miss=1" in r.message for r in caplog.records), \
        "外部已 CRITICAL 的未命中也要进汇总留痕（原逐条 WARNING 只在提级时打，漏掉此分支）"


def test_builtin_passthrough_never_regraded(monkeypatch):
    """hunter LOW4 接线锁：builtin finding 原样通过重分档（tool 名前缀是接线事实）。
    若 builtin 的 HIGH 被误提级，RuoYi 基线 CSRF_TOKEN 冤杀族复发。"""
    monkeypatch.setattr(ss.shutil, "which", lambda tool: None)
    with _mk_tmp_project(HIGH_LINE, fname="app.py") as tmp:
        findings = ss._run_secret_scan(tmp)
    bi = [f for f in findings if f.tool == "builtin-regex"]
    assert len(bi) == 1 and bi[0].severity == Severity.HIGH, \
        f"builtin HIGH 必须原样通过（FP 控制设计，绝不提级）: {[(f.rule_id, f.severity) for f in bi]}"


# ─── 4. 重分档只升不降：外部已判 CRITICAL 而本仓只认 HIGH 的行，保留 CRITICAL ───

def test_regrade_never_downgrades_external(monkeypatch):
    _install_fake_gitleaks(monkeypatch, [
        {"RuleID": "generic-password", "File": "app.py", "StartLine": 1, "severity": "critical"},
    ])
    with _mk_tmp_project(HIGH_LINE, fname="app.py") as tmp:
        findings = ss._run_secret_scan(tmp)
    gl = [f for f in findings if f.tool == "gitleaks"]
    assert len(gl) == 1 and gl[0].severity == Severity.CRITICAL, \
        f"重分档绝不降级外部更高档: {gl[0].severity}"


def test_regrade_upgrades_medium_to_high_via_local_match(monkeypatch):
    """跨档升级锁（reviewer 点名覆盖缺口）：外部报 MEDIUM、本仓表认 HIGH → 升 HIGH。"""
    _install_fake_gitleaks(monkeypatch, [
        {"RuleID": "generic-password", "File": "app.py", "StartLine": 1, "severity": "medium"},
    ])
    with _mk_tmp_project(HIGH_LINE, fname="app.py") as tmp:
        findings = ss._run_secret_scan(tmp)
    gl = [f for f in findings if f.tool == "gitleaks"]
    assert len(gl) == 1 and gl[0].severity == Severity.HIGH, \
        f"外部 MEDIUM 命中本仓 HIGH 行应升为 HIGH: {gl[0].severity}"


# ─── 4b. 路径囚禁锁（reviewer MEDIUM）：外部报告的 file 越界 → 按不可读 → CRITICAL ───

def test_reported_path_escape_confined(monkeypatch, tmp_path):
    """gitleaks 报告 `../` 逃逸路径：绝不读项目外文件，按「认不出」显式落 CRITICAL。
    区分力设计：项目外文件内容恰命中本仓 HIGH 行、外部报 MEDIUM——若囚禁缺失，
    重分档会读到它并升 HIGH（绿错方向）；囚禁生效则不可读 → CRITICAL。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "outside.py").write_text(HIGH_LINE + "\n", encoding="utf-8")
    _install_fake_gitleaks(monkeypatch, [
        {"RuleID": "escapee", "File": "../outside.py", "StartLine": 1, "severity": "medium"},
    ])
    findings = ss._run_secret_scan(str(proj))
    esc = [f for f in findings if f.rule_id == "escapee"]
    assert len(esc) == 1 and esc[0].severity == Severity.CRITICAL, \
        f"越界路径必须按不可读 → CRITICAL（读到=囚禁失效会得 HIGH）: {esc[0].severity}"


# ─── 5. 去重锁：(file, line, rule_id) 同键取最强档；异 rule_id 同行两留 ───

def test_dedup_same_key_keeps_strongest():
    def _f(sev, rule="r1", file="a.py", line=3):
        return SecurityFinding(
            severity=sev, category="secret", rule_id=rule, title="t",
            file=file, line=line, tool="gitleaks", recommendation="r",
        )
    out = ss._dedup_secret_findings([_f(Severity.HIGH), _f(Severity.CRITICAL), _f(Severity.MEDIUM)])
    assert len(out) == 1 and out[0].severity == Severity.CRITICAL


def test_dedup_distinct_rule_ids_both_kept():
    def _f(sev, rule):
        return SecurityFinding(
            severity=sev, category="secret", rule_id=rule, title="t",
            file="a.py", line=3, tool="gitleaks", recommendation="r",
        )
    out = ss._dedup_secret_findings([_f(Severity.HIGH, "gitleaks-rule"), _f(Severity.CRITICAL, "builtin-secret-x")])
    assert len(out) == 2, "键含 rule_id：同行异规则两条都留（每行最强档由重分档保证）"


# ─── 6. 外部全缺席时行为不变：仍只有内置正则一路 ───

def test_external_absent_builtin_only_unchanged(monkeypatch):
    monkeypatch.setattr(ss.shutil, "which", lambda tool: None)
    with _mk_tmp_project(PROJ_KEY_LINE) as tmp:
        findings = ss._run_secret_scan(tmp)
    tools = {f.tool for f in findings}
    assert tools == {"builtin-regex"}, tools
    assert any(f.severity == Severity.CRITICAL for f in findings)


# ─── 7. 覆盖记账锁：并集不破坏 A-P0-2/D4 置位语义 ───

def test_ctx_marking_gitleaks_ran(monkeypatch):
    _install_fake_gitleaks(monkeypatch, [])
    with _mk_tmp_project("x = 1") as tmp:
        ctx = ss._ScanContext()
        ss._run_secret_scan(tmp, ctx=ctx)
    assert ctx.scanner_ran and ctx.secret_ran, "外部工具真跑成 → 聚合+category 都置位"


def test_ctx_marking_builtin_only(monkeypatch):
    monkeypatch.setattr(ss.shutil, "which", lambda tool: None)
    with _mk_tmp_project("x = 1") as tmp:
        ctx = ss._ScanContext()
        ss._run_secret_scan(tmp, ctx=ctx)
    assert not ctx.scanner_ran, "builtin-regex 只置 category 不置聚合（A-P0-2 原旨）"
    assert ctx.secret_ran
