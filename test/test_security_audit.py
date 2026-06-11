#!/usr/bin/env python3
"""安全审计子系统 单元测试 — 内置正则密钥扫描 / 工具缺失降级 / block_severity 逻辑。"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ─── 内置正则密钥扫描 ───

def test_builtin_secret_sk_key():
    """内置正则能检出 sk- 开头的 OpenAI API key。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        # 写入含 sk- 密钥的文件
        (Path(tmp) / "config.py").write_text(
            'API_KEY = "sk-abc123def456ghi789jkl012mno345"\n',
            encoding="utf-8",
        )
        findings, should_block = run_security_scan(tmp, "python", block_severity="critical")
        secret_findings = [f for f in findings if f.category == "secret"]
        assert len(secret_findings) >= 1, f"应检出至少 1 个 secret, 实际: {secret_findings}"
        assert secret_findings[0].severity.value in ("critical", "high"), \
            f"sk- 密钥严重度应 >= high, 实际: {secret_findings[0].severity}"
    print("  ✅ 内置正则检出 sk- 开头密钥")


def test_builtin_secret_akia_key():
    """内置正则能检出 AKIA 开头的 AWS Access Key ID。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "settings.py").write_text(
            'AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"\n',
            encoding="utf-8",
        )
        findings, _ = run_security_scan(tmp, "python", block_severity="high")
        secret_findings = [f for f in findings if f.category == "secret"]
        assert len(secret_findings) >= 1, f"应检出 AKIA 密钥, 实际: {[f.title for f in findings]}"
        assert any("AWS" in f.title or "AKIA" in f.file or f.line > 0 for f in secret_findings), \
            "AKIA 密钥检出信息不完整"
    print("  ✅ 内置正则检出 AKIA AWS Access Key")


def test_builtin_secret_private_key():
    """内置正则能检出 -----BEGIN PRIVATE KEY-----。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "id_rsa").write_text(
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEowI...\n-----END RSA PRIVATE KEY-----\n",
            encoding="utf-8",
        )
        findings, _ = run_security_scan(tmp, "python", block_severity="critical")
        secret_findings = [f for f in findings if f.category == "secret"]
        assert len(secret_findings) >= 1, f"应检出 Private Key, 实际: {secret_findings}"
        assert any("Private Key" in f.title or "private" in f.title.lower() for f in secret_findings), \
            "Private Key 检出标题不匹配"
    print("  ✅ 内置正则检出 BEGIN PRIVATE KEY")


def test_builtin_secret_ghp_key():
    """内置正则能检出 ghp_ GitHub PAT。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "repo.sh").write_text(
            'GITHUB_TOKEN="ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"\n',
            encoding="utf-8",
        )
        findings, _ = run_security_scan(tmp, "python", block_severity="high")
        secret_findings = [f for f in findings if f.category == "secret"]
        assert len(secret_findings) >= 1, f"应检出 ghp_ 密钥, 实际: {secret_findings}"
    print("  ✅ 内置正则检出 ghp_ GitHub PAT")


def test_no_secrets_clean_file():
    """干净文件不应检出密钥。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "main.py").write_text(
            'def hello():\n    print("Hello, World!")\n',
            encoding="utf-8",
        )
        findings, _ = run_security_scan(tmp, "python", block_severity="critical")
        secret_findings = [f for f in findings if f.category == "secret"]
        assert len(secret_findings) == 0, f"干净文件不应检出密钥, 实际: {secret_findings}"
    print("  ✅ 干净文件无密钥检出")


# ─── 工具缺失优雅降级 ───

def test_no_tool_no_crash_python():
    """Python 语言：无 bandit/pip-audit，不抛异常，应通过内置正则扫描返回结果。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "clean.py").write_text("x = 1\n", encoding="utf-8")
        # 不应抛异常
        findings, should_block = run_security_scan(tmp, "python", block_severity="critical")
        # 干净文件: findings 可能有 sast/dep 空列表 + secret 空
        assert isinstance(findings, list), "findings 应为列表"
        assert isinstance(should_block, bool), "should_block 应为 bool"
    print("  ✅ Python 工具缺失不崩，优雅降级")


def test_no_tool_no_crash_go():
    """Go 语言：无 gosec/govulncheck，不抛异常。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "main.go").write_text('package main\nfunc main() {}\n', encoding="utf-8")
        findings, should_block = run_security_scan(tmp, "go", block_severity="critical")
        assert isinstance(findings, list)
        assert isinstance(should_block, bool)
    print("  ✅ Go 工具缺失不崩，优雅降级")


def test_no_tool_no_crash_rust():
    """Rust 语言：无 cargo/clippy/audit，不抛异常。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "main.rs").write_text('fn main() {}\n', encoding="utf-8")
        findings, should_block = run_security_scan(tmp, "rust", block_severity="critical")
        assert isinstance(findings, list)
        assert isinstance(should_block, bool)
    print("  ✅ Rust 工具缺失不崩，优雅降级")


def test_no_tool_no_crash_java():
    """Java 语言：无 spotbugs/dependency-check，不抛异常。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "App.java").write_text('public class App {}\n', encoding="utf-8")
        findings, should_block = run_security_scan(tmp, "java", block_severity="critical")
        assert isinstance(findings, list)
        assert isinstance(should_block, bool)
    print("  ✅ Java 工具缺失不崩，优雅降级")


def test_no_tool_no_crash_node():
    """Node 语言：无 semgrep/npm，不抛异常。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "index.js").write_text('console.log("hello");\n', encoding="utf-8")
        findings, should_block = run_security_scan(tmp, "node", block_severity="critical")
        assert isinstance(findings, list)
        assert isinstance(should_block, bool)
    print("  ✅ Node 工具缺失不崩，优雅降级")


def test_unsupported_language():
    """不支持的语言应返回空列表不崩。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        findings, should_block = run_security_scan(tmp, "cobol", block_severity="critical")
        # 内置正则仍会扫描 secret
        assert isinstance(findings, list)
    print("  ✅ 不支持的语言优雅降级")


# ─── block_severity 逻辑 ───

def test_block_severity_critical_with_critical_finding():
    """含 critical finding + block_severity='critical' → should_block=True。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "leak.py").write_text(
            'API_KEY = "sk-aaaaaaaaaaaaaaaaaaaaaaaaaa"  # OpenAI key\n',
            encoding="utf-8",
        )
        findings, should_block = run_security_scan(tmp, "python", block_severity="critical")
        # sk- 密钥被标记为 CRITICAL
        assert len(findings) > 0, "应有发现"
        # sk- 对应 severity=CRITICAL, block_severity=critical → should_block=True
        assert should_block is True, f"critical finding + critical 阈值应阻断, findings: {findings}"
    print("  ✅ critical finding + block=critical → should_block=True")


def test_block_severity_high_with_critical_finding():
    """含 critical finding + block_severity='high' → should_block=True。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "leak.py").write_text(
            'API_KEY = "sk-aaaaaaaaaaaaaaaaaaaaaaaaaa"\n',
            encoding="utf-8",
        )
        findings, should_block = run_security_scan(tmp, "python", block_severity="high")
        assert should_block is True, "critical >= high 阈值应阻断"
    print("  ✅ critical finding + block=high → should_block=True")


def test_block_severity_none_with_critical_finding():
    """含 critical finding + block_severity='none' → should_block=False (纯报告)。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "leak.py").write_text(
            'API_KEY = "sk-aaaaaaaaaaaaaaaaaaaaaaaaaa"\n',
            encoding="utf-8",
        )
        findings, should_block = run_security_scan(tmp, "python", block_severity="none")
        assert len(findings) > 0, "应有发现（纯报告模式仍然产出结果）"
        assert should_block is False, "block_severity='none' 纯报告模式不阻断"
    print("  ✅ critical finding + block=none → should_block=False (纯报告)")


def test_block_severity_critical_with_medium_finding():
    """含 medium finding + block_severity='critical' → should_block=False。"""
    from swarm.worker.security_scan import run_security_scan
    from swarm.types import Severity

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "config.py").write_text(
            'password = "MySecret12345"  # matches generic secret assignment\n',
            encoding="utf-8",
        )
        findings, should_block = run_security_scan(tmp, "python", block_severity="critical")
        # 检查：如果只有 medium/high 级别的发现，在 critical 阈值下不阻断
        has_critical = any(f.severity == Severity.CRITICAL for f in findings)
        if not has_critical:
            assert should_block is False, "无 critical finding + critical 阈值不应阻断"
        else:
            # 如果 generic secret 被标为 CRITICAL（不太可能但不排除），则 should_block=True
            print(f"  ℹ️ 发现 critical 级别发现: {[f.title for f in findings if f.severity == Severity.CRITICAL]}")
    print("  ✅ medium finding + block=critical → should_block 合理")


def test_clean_project_no_block():
    """干净项目 + 任何阈值 → should_block=False。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "main.py").write_text("x = 1\n", encoding="utf-8")
        findings, should_block = run_security_scan(tmp, "python", block_severity="critical")
        assert should_block is False, f"干净项目不应阻断, findings: {findings}"
    print("  ✅ 干净项目不阻断")


# ─── SecurityFinding 结构验证 ───

def test_finding_structure():
    """SecurityFinding 字段完整性。"""
    from swarm.worker.security_scan import run_security_scan
    from swarm.types import Severity

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "secrets.py").write_text(
            'openai_key = "sk-AbcDefGhiJklMnoPqrStuVwXyz"\n',
            encoding="utf-8",
        )
        findings, _ = run_security_scan(tmp, "python", block_severity="none")
        assert len(findings) >= 1
        f = findings[0]
        assert f.category == "secret", f"category 应为 secret, 实际: {f.category}"
        assert f.severity in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO)
        assert f.tool, "tool 不应为空"
        assert f.title, "title 不应为空"
        assert f.line > 0, f"line 应 > 0, 实际: {f.line}"
        assert f.file, f"file 不应为空, 实际: {f.file}"
    print("  ✅ SecurityFinding 结构完整")


# ─── files 参数过滤 ───

def test_files_parameter_limits_scope():
    """files 参数限制扫描范围。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        # 含密钥的文件
        (Path(tmp) / "secret.py").write_text(
            'key = "sk-AbcDefGhiJklMnoPqrStuVwXyz123456"\n',
            encoding="utf-8",
        )
        # 干净文件
        (Path(tmp) / "clean.py").write_text("x = 1\n", encoding="utf-8")
        # 只扫描 clean.py → 不应检出密钥
        findings, _ = run_security_scan(tmp, "python", files=["clean.py"], block_severity="none")
        secret_findings = [f for f in findings if f.category == "secret"]
        assert len(secret_findings) == 0, f"只扫描 clean.py 不应检出密钥, 实际: {secret_findings}"
    print("  ✅ files 参数限制扫描范围有效")


# ─── main 入口 ───

def main() -> int:
    print("\n🧪 安全审计子系统 单元测试\n")
    tests = [
        test_builtin_secret_sk_key,
        test_builtin_secret_akia_key,
        test_builtin_secret_private_key,
        test_builtin_secret_ghp_key,
        test_no_secrets_clean_file,
        test_no_tool_no_crash_python,
        test_no_tool_no_crash_go,
        test_no_tool_no_crash_rust,
        test_no_tool_no_crash_java,
        test_no_tool_no_crash_node,
        test_unsupported_language,
        test_block_severity_critical_with_critical_finding,
        test_block_severity_high_with_critical_finding,
        test_block_severity_none_with_critical_finding,
        test_block_severity_critical_with_medium_finding,
        test_clean_project_no_block,
        test_finding_structure,
        test_files_parameter_limits_scope,
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
