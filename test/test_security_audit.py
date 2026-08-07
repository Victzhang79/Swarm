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
        findings, should_block, _scan_details = run_security_scan(tmp, "python", block_severity="critical")
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
        findings, _, _scan_details = run_security_scan(tmp, "python", block_severity="high")
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
        findings, _, _scan_details = run_security_scan(tmp, "python", block_severity="critical")
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
        findings, _, _scan_details = run_security_scan(tmp, "python", block_severity="high")
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
        findings, _, _scan_details = run_security_scan(tmp, "python", block_severity="critical")
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
        findings, should_block, _scan_details = run_security_scan(tmp, "python", block_severity="critical")
        # 干净文件: findings 可能有 sast/dep 空列表 + secret 空
        assert isinstance(findings, list), "findings 应为列表"
        assert isinstance(should_block, bool), "should_block 应为 bool"
    print("  ✅ Python 工具缺失不崩，优雅降级")


def test_no_tool_no_crash_go():
    """Go 语言：无 gosec/govulncheck，不抛异常。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "main.go").write_text('package main\nfunc main() {}\n', encoding="utf-8")
        findings, should_block, _scan_details = run_security_scan(tmp, "go", block_severity="critical")
        assert isinstance(findings, list)
        assert isinstance(should_block, bool)
    print("  ✅ Go 工具缺失不崩，优雅降级")


def test_no_tool_no_crash_rust():
    """Rust 语言：无 cargo/clippy/audit，不抛异常。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "main.rs").write_text('fn main() {}\n', encoding="utf-8")
        findings, should_block, _scan_details = run_security_scan(tmp, "rust", block_severity="critical")
        assert isinstance(findings, list)
        assert isinstance(should_block, bool)
    print("  ✅ Rust 工具缺失不崩，优雅降级")


def test_no_tool_no_crash_java():
    """Java 语言：无 spotbugs/dependency-check，不抛异常。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "App.java").write_text('public class App {}\n', encoding="utf-8")
        findings, should_block, _scan_details = run_security_scan(tmp, "java", block_severity="critical")
        assert isinstance(findings, list)
        assert isinstance(should_block, bool)
    print("  ✅ Java 工具缺失不崩，优雅降级")


def test_no_tool_no_crash_node():
    """Node 语言：无 semgrep/npm，不抛异常。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "index.js").write_text('console.log("hello");\n', encoding="utf-8")
        findings, should_block, _scan_details = run_security_scan(tmp, "node", block_severity="critical")
        assert isinstance(findings, list)
        assert isinstance(should_block, bool)
    print("  ✅ Node 工具缺失不崩，优雅降级")


def test_unsupported_language():
    """不支持的语言应返回空列表不崩。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        findings, should_block, _scan_details = run_security_scan(tmp, "cobol", block_severity="critical")
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
        findings, should_block, _scan_details = run_security_scan(tmp, "python", block_severity="critical")
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
        findings, should_block, _scan_details = run_security_scan(tmp, "python", block_severity="high")
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
        findings, should_block, _scan_details = run_security_scan(tmp, "python", block_severity="none")
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
        findings, should_block, _scan_details = run_security_scan(tmp, "python", block_severity="critical")
        # 检查：如果只有 medium/high 级别的发现，在 critical 阈值下不阻断
        has_critical = any(f.severity == Severity.CRITICAL for f in findings)
        if not has_critical:
            assert should_block is False, "无 critical finding + critical 阈值不应阻断"
        else:
            # 如果 generic secret 被标为 CRITICAL（不太可能但不排除），则 should_block=True
            print(f"  ℹ️ 发现 critical 级别发现: {[f.title for f in findings if f.severity == Severity.CRITICAL]}")
    print("  ✅ medium finding + block=critical → should_block 合理")


_CLEAN_TOOL_OUTPUT = {
    "bandit": '{"results": []}',          # SAST 干净
    "pip-audit": '{"dependencies": []}',  # 依赖干净
}

# ★必须与 `_CLEAN_TOOL_OUTPUT` 是**两份独立枚举**★（hunter F8）：
# 初版让 `shutil.which` 直接用 `n in _CLEAN_TOOL_OUTPUT` 判放行，于是 `_run_tool` 里那句
# 「夹具未预置工具 X」的守卫**在构造上不可达**——生产每个 `_run_tool` 调用点前都有
# `which` 前置闸（security_scan.py:319/632/1237…），which 只放行输出表里的键 ⇒
# `out is None` 永不成立。看着是防线，实际是装饰。这正是
# [[swarm-fallback-must-not-share-the-gap]] 的微缩版：兜底网与主判据同源 ⇒ 缺口重合。
# 拆成两份后：谁往 which 放行表里加了工具却忘了给输出，守卫当场炸（已做可达性实验证实）。
_WHICH_ALLOW = ("bandit", "pip-audit")


def _fake_full_coverage(monkeypatch):
    """把「sast+dep 两类都有真实工具执行且干净」构造出来。

    ★不能用真 `shutil.which` 分叉★（29 号文 T-A4）：`bandit`/`pip-audit` 既不在 dev
    依赖里、CI 也无额外安装步，`which` 全为 None ⇒ 本机与 CI **都**只走 else 分支，
    `if sast_covered and dep_covered:` 那半边在**任何**环境都不执行。后果：把
    `run_security_scan` 改成「即使全类覆盖也恒阻断」（冤杀全部交付）测试仍绿。
    """
    import swarm.worker.security_scan as ss

    # which 的放行表与输出表**分开维护**（见 _WHICH_ALLOW 处的注释）
    monkeypatch.setattr(ss.shutil, "which",
                        lambda n: f"/usr/bin/{n}" if n in _WHICH_ALLOW else None)

    def _fake_run(cmd, *, cwd, timeout=120):
        out = _CLEAN_TOOL_OUTPUT.get(cmd[0])
        # 夹具必须对「冒出没预置的工具」显式炸掉：静默回 (-1,'','') 会让覆盖面缺口
        # 伪装成「工具跑挂」，正是本条要治的假绿形态。
        # 现在这句**真的可达**：which 放行表里多一个工具而输出表没跟上就会触发。
        assert out is not None, (
            f"夹具未预置工具 {cmd[0]} 的输出，但 _WHICH_ALLOW 放行了它 —— "
            "两份枚举漂了；不补输出会让覆盖面缺口伪装成「工具跑挂」"
        )
        return (0, out, "")

    monkeypatch.setattr(ss, "_run_tool", _fake_run)


def test_clean_project_full_coverage_no_block(monkeypatch):
    """全类覆盖 + 干净项目 → 不阻断（保持原意图，不误杀）。

    这是 T-A4 之前**从未被执行过**的那半边分支。
    """
    from swarm.worker.security_scan import run_security_scan

    _fake_full_coverage(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "main.py").write_text("x = 1\n", encoding="utf-8")
        findings, should_block, details = run_security_scan(tmp, "python", block_severity="critical")
    # 前提锁：夹具真把两类覆盖构造出来了（否则下面的 False 由 fail-closed 的反面偶然满足）
    assert details.get("categories_ran", {}).get("sast") is True, f"夹具前提失效: {details}"
    assert details.get("categories_ran", {}).get("dep") is True, f"夹具前提失效: {details}"
    assert should_block is False, f"全类覆盖+干净项目不应阻断, findings: {findings}"
    assert not [f for f in findings if f.rule_id.startswith("fail-closed-no-")], \
        "全类覆盖时不应再注入 fail-closed 哨兵"
    print("  ✅ 全类覆盖 + 干净项目 → 不阻断")


def test_clean_project_zero_coverage_fails_closed(monkeypatch):
    """任一类 0 覆盖 → 阻断模式必须 fail-closed，且逐类哨兵独立。"""
    import swarm.worker.security_scan as ss
    from swarm.worker.security_scan import run_security_scan

    monkeypatch.setattr(ss.shutil, "which", lambda _n: None)   # 全类工具缺失
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "main.py").write_text("x = 1\n", encoding="utf-8")
        findings, should_block, _details = run_security_scan(tmp, "python", block_severity="critical")
    assert should_block is True, "任一类 0 覆盖→阻断模式必须 fail-closed"
    assert any(f.rule_id == "fail-closed-no-dep-scanner" for f in findings), \
        "D4：依赖类 0 覆盖必须有独立哨兵（单布尔时代被 sast 工具掩盖）"
    assert any(f.rule_id == "fail-closed-no-sast-scanner" for f in findings), \
        "D4：SAST 类 0 覆盖同样要有独立哨兵"
    print("  ✅ 0 覆盖 → fail-closed + 逐类哨兵")


def test_partial_coverage_still_fails_closed(monkeypatch):
    """只有 sast 有工具、dep 无 ⇒ 仍须阻断（单布尔时代正是这一格放行的）。"""
    import swarm.worker.security_scan as ss
    from swarm.worker.security_scan import run_security_scan

    monkeypatch.setattr(ss.shutil, "which", lambda n: "/usr/bin/bandit" if n == "bandit" else None)
    monkeypatch.setattr(ss, "_run_tool", lambda cmd, *, cwd, timeout=120: (0, '{"results": []}', ""))
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "main.py").write_text("x = 1\n", encoding="utf-8")
        findings, should_block, details = run_security_scan(tmp, "python", block_severity="critical")
    assert details.get("categories_ran", {}).get("sast") is True, f"夹具前提失效: {details}"
    assert details.get("categories_ran", {}).get("dep") is False, f"夹具前提失效: {details}"
    assert should_block is True, "sast 有覆盖不能替 dep 背书（D4 per-category 的本体）"
    assert any(f.rule_id == "fail-closed-no-dep-scanner" for f in findings)
    print("  ✅ 部分覆盖 → 仍 fail-closed（逐类分账）")


def test_clean_project_report_mode_never_blocks():
    """报告模式(none)下，即便无任何扫描器执行也绝不阻断（运维明示永不阻断）。"""
    from swarm.worker.security_scan import run_security_scan
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "main.py").write_text("x = 1\n", encoding="utf-8")
        findings, should_block, _scan_details = run_security_scan(tmp, "python", block_severity="none")
        assert should_block is False, "report-only 模式永不阻断"
        assert not any(f.rule_id.startswith("fail-closed-no-") for f in findings)
    print("  ✅ 报告模式无扫描器也不阻断")


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
        findings, _, _scan_details = run_security_scan(tmp, "python", block_severity="none")
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
        findings, _, _scan_details = run_security_scan(tmp, "python", files=["clean.py"], block_severity="none")
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
        test_finding_structure,
        test_files_parameter_limits_scope,
    ]
    # 覆盖面三条（full_coverage / zero_coverage / partial_coverage）需要 monkeypatch
    # 夹具，只能在 pytest 下跑——本 __main__ 直调路径无法注入 fixture。别在这里加它们：
    # 加了会 TypeError（缺参），而这个 runner 不在任何 CI/run_all.sh 路径上。
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
