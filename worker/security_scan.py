"""安全审计扫描器 — SAST / 依赖漏洞 / 密钥扫描，产出 list[SecurityFinding]。

支持 5 种语言: python / node / go / rust / java
两种模式: 阻断交付 (block_severity='critical') / 仅报告 (block_severity='none')
工具缺失一律优雅 skip (shutil.which 探测)，绝不崩。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from swarm.types import SecurityFinding, Severity

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 严重度排序辅助
# ──────────────────────────────────────────────
_SEVERITY_ORDER: dict[str, int] = {
    Severity.CRITICAL: 4,
    Severity.HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
    Severity.INFO: 0,
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
    "info": 0,
}


def _severity_gte(found: Severity, threshold: str) -> bool:
    """判断 found 严重度是否 >= threshold。"""
    return _SEVERITY_ORDER.get(found, 0) >= _SEVERITY_ORDER.get(threshold, 0)


# audit A-P0-2：跨扫描器记录"是否真的有扫描器执行过"。
# 工具缺失(FileNotFoundError→rc=-1)/超时/解析失败都返回 []，与"干净通过"无法区分。
# 用一个可变 dict 让各 helper 在【确实跑起了某个真实工具】时置 ran=True。
_SEVERITY_FAILCLOSED_TITLE = "Security scanning unavailable (fail-closed)"


class _ScanContext:
    """跨各扫描器累积执行状态。scanner_ran=True 表示【至少一个真实工具成功执行】
    （rc != -1，即非缺失/非超时/非异常）——哪怕它本身零发现。

    注意：builtin-regex 密钥兜底【不算】真实扫描器（它在工具全缺时也能跑，会掩盖
    SAST/依赖工具全缺的事实）；只有外部工具真正执行才置位。
    """

    __slots__ = ("scanner_ran",)

    def __init__(self) -> None:
        self.scanner_ran = False


# ──────────────────────────────────────────────
# 公共入口
# ──────────────────────────────────────────────
def run_security_scan(
    project_path: str,
    language: str,
    *,
    files: list[str] | None = None,
    block_severity: str = "critical",
) -> tuple[list[SecurityFinding], bool]:
    """运行三类安全扫描，返回 (findings, should_block)。

    Args:
        project_path: 项目根目录
        language: 主语言 (python/node/go/rust/java)
        files: 待扫描文件列表 (相对路径)；None=全项目
        block_severity: 阻断阈值 'critical'/'high'/'none'；
            'none'=纯报告模式不阻断

    Returns:
        (findings, should_block): should_block=True 表示存在 >= block_severity 的发现
    """
    language = language.lower().strip()
    findings: list[SecurityFinding] = []

    # audit A-P0-2：记录是否有真实扫描器执行（区分"扫过且干净" vs "工具缺失/没扫"）。
    ctx = _ScanContext()

    # (a) SAST
    findings.extend(_run_sast(project_path, language, files=files, ctx=ctx))
    # (b) 依赖漏洞
    findings.extend(_run_dependency_scan(project_path, language, ctx=ctx))
    # (c) 密钥扫描
    findings.extend(_run_secret_scan(project_path, files=files, ctx=ctx))

    # 判断是否阻断
    if block_severity == "none":
        # report-only 模式：运维明示永不阻断（即便没扫成），保持可观测不误杀。
        should_block = False
        # A-P0-2 report-mode 可见性：即便不阻断，也绝不让"根本没扫"伪装成"扫过且干净"。
        # 注入一条 INFO 级（rank 0，永不触发任何阈值）发现 + WARNING 日志，使覆盖率缺口可观测。
        if not ctx.scanner_ran:
            logger.warning(
                "Security scan: no real scanner executed for language '%s' in report-only mode "
                "(block_severity=none) — 0 coverage, NOT clean. Install scanners for real signal.",
                language,
            )
            findings.append(SecurityFinding(
                severity=Severity.INFO,
                category="sast",
                rule_id="scan-coverage-zero",
                title="Security scanning unavailable (report-only, 0 coverage)",
                file="",
                line=0,
                tool="swarm-security-gate",
                recommendation=(
                    "No security scanner ran for this language (tooling absent/failed). "
                    "Result is 'not scanned', NOT 'clean'. Install the relevant scanners "
                    "(bandit/semgrep/gosec/clippy/spotbugs, pip-audit/npm/govulncheck/cargo-audit) "
                    "to obtain real findings, or set security_block_severity to enforce blocking."
                ),
            ))
    else:
        should_block = any(_severity_gte(f.severity, block_severity) for f in findings)
        # A-P0-2 fail-closed：阻断模式下，若【没有任何真实扫描器执行】（工具全缺/全超时/全解析失败），
        # 绝不能与"真·零漏洞"混同放行。注入一条 = 阈值级别的合成发现，强制 should_block=True。
        if not ctx.scanner_ran:
            logger.warning(
                "Security scan: no real scanner executed for language '%s' in block mode "
                "(threshold=%s) — failing closed.",
                language,
                block_severity,
            )
            synthetic_sev = (
                block_severity
                if block_severity in _SEVERITY_ORDER
                else Severity.CRITICAL
            )
            findings.append(SecurityFinding(
                severity=synthetic_sev,  # type: ignore[arg-type]
                category="sast",
                rule_id="fail-closed-no-scanner",
                title=_SEVERITY_FAILCLOSED_TITLE,
                file="",
                line=0,
                tool="swarm-security-gate",
                recommendation=(
                    "No security scanner ran for this language (tooling absent/failed). "
                    "Install the relevant scanners (e.g. bandit/semgrep/gosec/clippy/spotbugs, "
                    "pip-audit/npm/govulncheck/cargo-audit/dependency-check) or set "
                    "security_block_severity=none to explicitly accept un-scanned deliveries."
                ),
            ))
            should_block = True

    logger.info(
        "Security scan done: %d findings, should_block=%s (threshold=%s, scanner_ran=%s)",
        len(findings),
        should_block,
        block_severity,
        ctx.scanner_ran,
    )
    return findings, should_block


# ──────────────────────────────────────────────
# 子进程执行辅助
# ──────────────────────────────────────────────
def _run_tool(cmd: list[str], *, cwd: str, timeout: int = 120) -> tuple[int, str, str]:
    """执行外部工具，返回 (returncode, stdout, stderr)。异常时返回 (-1, '', stderr_msg)。"""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return -1, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout ({timeout}s): {' '.join(cmd)}"
    except Exception as exc:  # noqa: BLE001
        return -1, "", str(exc)


def _safe_json_parse(raw: str) -> Any:
    """尝试解析 JSON，失败返回 None。"""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # 尝试找第一个 { 到最后一个 }
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except (json.JSONDecodeError, ValueError):
                pass
        # 尝试找 [ ... ]
        start = raw.find("[")
        end = raw.rfind("]")
        if start != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except (json.JSONDecodeError, ValueError):
                pass
        return None


def _mark_ran(ctx: "_ScanContext | None") -> None:
    """标记：某真实外部扫描器已成功执行（rc != -1）。A-P0-2 fail-closed 判据用。"""
    if ctx is not None:
        ctx.scanner_ran = True


# ──────────────────────────────────────────────
# (a) SAST 扫描
# ──────────────────────────────────────────────
def _run_sast(
    project_path: str, language: str, *, files: list[str] | None = None, ctx: "_ScanContext | None" = None
) -> list[SecurityFinding]:
    """SAST 静态分析扫描。"""
    dispatch = {
        "python": _sast_python,
        "node": _sast_node,
        "go": _sast_go,
        "rust": _sast_rust,
        "java": _sast_java,
    }
    handler = dispatch.get(language)
    if handler is None:
        logger.warning("SAST: unsupported language '%s', skipping", language)
        return []
    return handler(project_path, files=files, ctx=ctx)


def _sast_python(project_path: str, *, files: list[str] | None = None, ctx: "_ScanContext | None" = None) -> list[SecurityFinding]:
    """Python SAST: bandit -f json。"""
    if not shutil.which("bandit"):
        logger.info("SAST(python): bandit not found, skipping")
        return []

    targets = files if files else ["-r", "."]
    cmd = ["bandit", "-f", "json"] + targets
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path)
    if rc == -1:
        logger.warning("SAST(python): bandit execution failed: %s", stderr)
        return []
    _mark_ran(ctx)  # 工具已成功执行(rc!=-1)

    data = _safe_json_parse(stdout)
    if data is None:
        logger.warning("SAST(python): bandit output not valid JSON, skipping")
        return []

    findings: list[SecurityFinding] = []
    results = data.get("results", []) if isinstance(data, dict) else []
    for r in results:
        sev = _map_bandit_severity(r.get("issue_severity", ""))
        findings.append(SecurityFinding(
            severity=sev,
            category="sast",
            rule_id=r.get("test_id", ""),
            title=r.get("test_name", "bandit finding"),
            file=r.get("filename", ""),
            line=r.get("line_number", 0),
            tool="bandit",
            recommendation=r.get("issue_text", ""),
        ))
    return findings


def _map_bandit_severity(sev: str) -> Severity:
    """Bandit severity: HIGH/MEDIUM/LOW → Severity。"""
    mapping = {"HIGH": Severity.HIGH, "MEDIUM": Severity.MEDIUM, "LOW": Severity.LOW}
    return mapping.get(sev.upper(), Severity.MEDIUM)


def _sast_node(project_path: str, *, files: list[str] | None = None, ctx: "_ScanContext | None" = None) -> list[SecurityFinding]:
    """Node SAST: semgrep --json (可选)。"""
    if not shutil.which("semgrep"):
        logger.info("SAST(node): semgrep not found, skipping")
        return []

    cmd = ["semgrep", "--json", "--config", "auto", project_path]
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path, timeout=300)
    if rc == -1:
        logger.warning("SAST(node): semgrep execution failed: %s", stderr)
        return []
    _mark_ran(ctx)  # 工具已成功执行(rc!=-1)

    data = _safe_json_parse(stdout)
    if data is None:
        logger.warning("SAST(node): semgrep output not valid JSON, skipping")
        return []

    findings: list[SecurityFinding] = []
    results = data.get("results", []) if isinstance(data, dict) else []
    for r in results:
        sev = _map_semgrep_severity(r.get("extra", {}).get("severity", ""))
        findings.append(SecurityFinding(
            severity=sev,
            category="sast",
            rule_id=r.get("check_id", ""),
            title=r.get("extra", {}).get("message", "semgrep finding"),
            file=r.get("path", ""),
            line=r.get("start", {}).get("line", 0) if isinstance(r.get("start"), dict) else 0,
            tool="semgrep",
            recommendation=r.get("extra", {}).get("fix", ""),
        ))
    return findings


def _map_semgrep_severity(sev: str) -> Severity:
    mapping = {"ERROR": Severity.HIGH, "WARNING": Severity.MEDIUM, "INFO": Severity.INFO}
    return mapping.get(sev.upper(), Severity.MEDIUM)


def _sast_go(project_path: str, *, files: list[str] | None = None, ctx: "_ScanContext | None" = None) -> list[SecurityFinding]:
    """Go SAST: gosec -fmt=json。"""
    if not shutil.which("gosec"):
        logger.info("SAST(go): gosec not found, skipping")
        return []

    cmd = ["gosec", "-fmt=json", "./..."]
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path)
    if rc == -1:
        logger.warning("SAST(go): gosec execution failed: %s", stderr)
        return []
    _mark_ran(ctx)  # 工具已成功执行(rc!=-1)

    data = _safe_json_parse(stdout)
    if data is None:
        logger.warning("SAST(go): gosec output not valid JSON, skipping")
        return []

    findings: list[SecurityFinding] = []
    issues = data.get("Issues", []) if isinstance(data, dict) else []
    for issue in issues:
        sev = _map_gosec_severity(issue.get("severity", ""))
        findings.append(SecurityFinding(
            severity=sev,
            category="sast",
            rule_id=issue.get("rule_id", ""),
            title=issue.get("details", "gosec finding"),
            file=issue.get("file", ""),
            line=issue.get("line", 0),
            tool="gosec",
            recommendation="",
        ))
    return findings


def _map_gosec_severity(sev: str) -> Severity:
    mapping = {"HIGH": Severity.HIGH, "MEDIUM": Severity.MEDIUM, "LOW": Severity.LOW}
    return mapping.get(sev.upper(), Severity.MEDIUM)


def _sast_rust(project_path: str, *, files: list[str] | None = None, ctx: "_ScanContext | None" = None) -> list[SecurityFinding]:
    """Rust SAST: cargo clippy 安全规则 (warn=->deny)。"""
    if not shutil.which("cargo"):
        logger.info("SAST(rust): cargo not found, skipping")
        return []

    cmd = ["cargo", "clippy", "--message-format=json", "--", "-W", "clippy::all"]
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path, timeout=300)
    if rc == -1:
        logger.warning("SAST(rust): cargo clippy execution failed: %s", stderr)
        return []
    _mark_ran(ctx)  # 工具已成功执行(rc!=-1)

    findings: list[SecurityFinding] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        data = _safe_json_parse(line)
        if data is None:
            continue
        if not isinstance(data, dict):
            continue
        reason = data.get("reason", "")
        if reason not in ("compiler-message", "compiler-artifact"):
            continue
        msg = data.get("message", {})
        if not isinstance(msg, dict):
            continue
        level = msg.get("level", "")
        # 只关注 warning 和 error
        if level not in ("warning", "error"):
            continue
        children = msg.get("children", [])
        code = msg.get("code", {})
        if isinstance(code, dict):
            code_val = code.get("code", "")
        else:
            code_val = str(code)

        # 尝试提取安全相关: spans 中有文件位置
        spans = msg.get("spans", [])
        for sp in spans:
            sev = Severity.HIGH if level == "error" else Severity.MEDIUM
            findings.append(SecurityFinding(
                severity=sev,
                category="sast",
                rule_id=code_val or "clippy",
                title=msg.get("message", "clippy finding")[:120],
                file=sp.get("file_name", ""),
                line=sp.get("line_start", 0),
                tool="cargo-clippy",
                recommendation="",
            ))
    return findings


def _sast_java(project_path: str, *, files: list[str] | None = None, ctx: "_ScanContext | None" = None) -> list[SecurityFinding]:
    """Java SAST: spotbugs。"""
    if not shutil.which("spotbugs"):
        logger.info("SAST(java): spotbugs not found, skipping")
        return []

    cmd = ["spotbugs", "-xml", project_path]
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path, timeout=300)
    if rc == -1:
        logger.warning("SAST(java): spotbugs execution failed: %s", stderr)
        return []
    _mark_ran(ctx)  # 工具已成功执行(rc!=-1)

    # N-11 修复：spotbugs `-xml` 产 XML，原代码用 _safe_json_parse 当 JSON 解析→恒 None→
    # Java diff 永报零发现(静默失效)。改为正确解析 spotbugs XML(BugCollection/BugInstance)。
    if not stdout.strip():
        return []
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(stdout)
    except ET.ParseError as exc:
        # 解析失败显式告警(而非静默吞)——便于诊断"为何 Java 永远零发现"
        logger.warning("SAST(java): spotbugs XML 解析失败: %s", exc)
        return []

    findings: list[SecurityFinding] = []
    for bug in root.iter("BugInstance"):
        sev = _map_spotbugs_severity(str(bug.get("priority", "2")))
        short = bug.findtext("ShortMessage") or bug.findtext("Message") or "spotbugs finding"
        long_msg = bug.findtext("LongMessage") or ""
        src = bug.find("SourceLine")
        if src is not None:
            fpath = src.get("sourcefile") or src.get("sourcepath") or src.get("classname", "")
            try:
                line = int(src.get("start", "0") or 0)
            except (TypeError, ValueError):
                line = 0
        else:
            fpath, line = "", 0
        findings.append(SecurityFinding(
            severity=sev,
            category="sast",
            rule_id=bug.get("type", ""),
            title=short,
            file=fpath,
            line=line,
            tool="spotbugs",
            recommendation=long_msg,
        ))
    return findings


def _map_spotbugs_severity(sev: str) -> Severity:
    """SpotBugs priority: 1=High, 2=Medium, 3=Low。"""
    mapping = {"1": Severity.HIGH, "2": Severity.MEDIUM, "3": Severity.LOW,
               "high": Severity.HIGH, "medium": Severity.MEDIUM, "low": Severity.LOW}
    return mapping.get(sev.lower(), Severity.MEDIUM)


# ──────────────────────────────────────────────
# (b) 依赖漏洞扫描
# ──────────────────────────────────────────────
def _run_dependency_scan(project_path: str, language: str, *, ctx: "_ScanContext | None" = None) -> list[SecurityFinding]:
    """依赖漏洞扫描。"""
    dispatch = {
        "python": _dep_python,
        "node": _dep_node,
        "go": _dep_go,
        "rust": _dep_rust,
        "java": _dep_java,
    }
    handler = dispatch.get(language)
    if handler is None:
        logger.warning("Dependency scan: unsupported language '%s', skipping", language)
        return []
    return handler(project_path, ctx=ctx)


def _dep_python(project_path: str, *, ctx: "_ScanContext | None" = None) -> list[SecurityFinding]:
    """Python: pip-audit --format=json。"""
    if not shutil.which("pip-audit"):
        logger.info("Dep(python): pip-audit not found, skipping")
        return []

    cmd = ["pip-audit", "--format=json"]
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path, timeout=180)
    if rc == -1:
        logger.warning("Dep(python): pip-audit execution failed: %s", stderr)
        return []
    _mark_ran(ctx)  # 工具已成功执行(rc!=-1)

    data = _safe_json_parse(stdout)
    if data is None:
        logger.warning("Dep(python): pip-audit output not valid JSON, skipping")
        return []

    findings: list[SecurityFinding] = []
    dependencies = data.get("dependencies", []) if isinstance(data, dict) else []
    for dep in dependencies:
        vulns = dep.get("vulns", []) if isinstance(dep, dict) else []
        for v in vulns:
            sev = _map_pip_audit_severity(v.get("severity", ""))
            findings.append(SecurityFinding(
                severity=sev,
                category="dependency",
                rule_id=v.get("id", ""),
                title=f"Vulnerable dependency: {dep.get('name', '')} {dep.get('version', '')}",
                file="",
                line=0,
                tool="pip-audit",
                recommendation=v.get("description", "Upgrade dependency"),
            ))
    return findings


def _map_pip_audit_severity(sev: str) -> Severity:
    mapping = {"critical": Severity.CRITICAL, "high": Severity.HIGH,
               "medium": Severity.MEDIUM, "low": Severity.LOW}
    return mapping.get(sev.lower(), Severity.MEDIUM)


def _dep_node(project_path: str, *, ctx: "_ScanContext | None" = None) -> list[SecurityFinding]:
    """Node: npm audit --json。"""
    if not shutil.which("npm"):
        logger.info("Dep(node): npm not found, skipping")
        return []

    cmd = ["npm", "audit", "--json"]
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path, timeout=180)
    if rc == -1:
        logger.warning("Dep(node): npm audit execution failed: %s", stderr)
        return []
    _mark_ran(ctx)  # 工具已成功执行(rc!=-1)

    data = _safe_json_parse(stdout)
    if data is None:
        logger.warning("Dep(node): npm audit output not valid JSON, skipping")
        return []

    findings: list[SecurityFinding] = []
    vulnerabilities = data.get("vulnerabilities", {}) if isinstance(data, dict) else {}
    for name, info in vulnerabilities.items():
        if not isinstance(info, dict):
            continue
        sev_str = info.get("severity", "medium")
        sev = _map_npm_severity(sev_str)
        via = info.get("via", [])
        via_str = ", ".join(str(v) for v in via) if isinstance(via, list) else str(via)
        findings.append(SecurityFinding(
            severity=sev,
            category="dependency",
            rule_id=via_str[:100] if via_str else "",
            title=f"Vulnerable dependency: {name}",
            file="",
            line=0,
            tool="npm-audit",
            recommendation=f"Run 'npm audit fix' to resolve {name} vulnerability",
        ))
    return findings


def _map_npm_severity(sev: str) -> Severity:
    mapping = {"critical": Severity.CRITICAL, "high": Severity.HIGH,
               "medium": Severity.MEDIUM, "low": Severity.LOW, "info": Severity.INFO}
    return mapping.get(sev.lower(), Severity.MEDIUM)


def _dep_go(project_path: str, *, ctx: "_ScanContext | None" = None) -> list[SecurityFinding]:
    """Go: govulncheck -json。"""
    if not shutil.which("govulncheck"):
        logger.info("Dep(go): govulncheck not found, skipping")
        return []

    cmd = ["govulncheck", "-json", "./..."]
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path, timeout=180)
    if rc == -1:
        logger.warning("Dep(go): govulncheck execution failed: %s", stderr)
        return []
    _mark_ran(ctx)  # 工具已成功执行(rc!=-1)

    # govulncheck -json 是 JSONL 流。现代格式（golang.org/x/vuln v1+）每行是
    # {"config":..}/{"progress":..}/{"osv":..}/{"finding":{"osv","fixed_version","trace":[..]}}；
    # 此前只认旧格式顶层 OSV 键 → 现代输出恒 0 发现且 _mark_ran 已置位（伪装"扫过没漏洞"，
    # 安全扫描 fail-open）。双格式解析，旧格式保留兼容。
    findings: list[SecurityFinding] = []
    by_osv: dict[str, SecurityFinding] = {}  # 现代格式：同 OSV 按 module/package/function 多层各发一条 → 去重
    for line in stdout.splitlines():
        data = _safe_json_parse(line.strip())
        if data is None or not isinstance(data, dict):
            continue
        if data.get("config") is not None or data.get("progress") is not None:
            continue  # 元信息行；"osv" 行是漏洞全文，坐标在 finding 行，此处跳过
        f = data.get("finding")
        if isinstance(f, dict):
            osv = f.get("osv") or ""
            if not osv:
                continue
            trace = f.get("trace") or []
            pos: dict = {}
            if isinstance(trace, list) and trace and isinstance(trace[0], dict):
                pos = trace[0].get("position") or {}
            if not isinstance(pos, dict):
                pos = {}  # 畸形 position（非 dict truthy）不许炸整个扫描（_mark_ran 已置位=fail-open）
            try:
                line_no = int(pos.get("line") or 0)
            except (TypeError, ValueError):
                line_no = 0
            fixed = f.get("fixed_version") or ""
            fnd = SecurityFinding(
                # 现代 finding 行不带 severity（在 osv 条目里且常缺）→ 保守 MEDIUM
                severity=_map_vuln_severity(""),
                category="dependency",
                rule_id=osv,
                title=f"Go vulnerability: {osv}",
                file=str(pos.get("filename") or ""),
                line=line_no,
                tool="govulncheck",
                recommendation=(f"Upgrade to {fixed} (affected by {osv})"
                                if fixed else f"Upgrade module affected by {osv}"),
            )
            prev = by_osv.get(osv)
            if prev is None or (not prev.file and fnd.file):
                by_osv[osv] = fnd  # 保留带源码位置的最具体一条
            continue
        # 旧格式：顶层 OSV / vuln 键
        osv = data.get("OSV", "")
        if not osv:
            if "vuln" in data:
                osv = data.get("vuln", "")
        if not osv:
            continue
        sev_str = data.get("severity", "medium")
        sev = _map_vuln_severity(sev_str)
        # hunter #1：trace 键存在但为 [] 时旧写法 [{}] 默认值不生效 → IndexError 炸扫描
        # （被 audit_node 捕获但报文无上下文，且已收集的 SAST 发现全被连坐丢弃）
        _trace = data.get("trace")
        _first = (_trace[0] if isinstance(_trace, list) and _trace
                  and isinstance(_trace[0], dict) else {})
        findings.append(SecurityFinding(
            severity=sev,
            category="dependency",
            rule_id=osv,
            title=f"Go vulnerability: {osv}",
            file=_first.get("filename", ""),
            line=_first.get("line", 0),
            tool="govulncheck",
            recommendation=f"Upgrade module affected by {osv}",
        ))
    return list(by_osv.values()) + findings


def _map_vuln_severity(sev: str) -> Severity:
    mapping = {"critical": Severity.CRITICAL, "high": Severity.HIGH,
               "medium": Severity.MEDIUM, "low": Severity.LOW}
    return mapping.get(sev.lower(), Severity.MEDIUM)


def _dep_rust(project_path: str, *, ctx: "_ScanContext | None" = None) -> list[SecurityFinding]:
    """Rust: cargo audit --json。"""
    if not shutil.which("cargo"):
        logger.info("Dep(rust): cargo not found, skipping")
        return []

    # cargo audit 是子命令，先检查 cargo-audit 是否安装
    if not shutil.which("cargo-audit") and not _cargo_subcommand_available("audit"):
        logger.info("Dep(rust): cargo audit not found, skipping")
        return []

    cmd = ["cargo", "audit", "--json"]
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path, timeout=180)
    if rc == -1:
        logger.warning("Dep(rust): cargo audit execution failed: %s", stderr)
        return []
    _mark_ran(ctx)  # 工具已成功执行(rc!=-1)

    data = _safe_json_parse(stdout)
    if data is None:
        logger.warning("Dep(rust): cargo audit output not valid JSON, skipping")
        return []

    findings: list[SecurityFinding] = []
    vulnerabilities = data.get("vulnerabilities", {}) if isinstance(data, dict) else {}
    # cargo audit JSON: { vulnerabilities: { list: [...] }, ... }
    # 也可能是 { vulnerabilities: [...] }
    vuln_list = vulnerabilities.get("list", vulnerabilities) if isinstance(vulnerabilities, dict) else vulnerabilities
    if not isinstance(vuln_list, list):
        vuln_list = []

    for v in vuln_list:
        if not isinstance(v, dict):
            continue
        sev_str = v.get("severity", "medium")
        sev = _map_vuln_severity(sev_str)
        advisory = v.get("advisory", {})
        if isinstance(advisory, dict):
            rule_id = advisory.get("id", "")
            title = advisory.get("title", "cargo-audit finding")
        else:
            rule_id = str(advisory)
            title = "cargo-audit finding"
        findings.append(SecurityFinding(
            severity=sev,
            category="dependency",
            rule_id=rule_id,
            title=title,
            file="",
            line=0,
            tool="cargo-audit",
            recommendation=v.get("advisory", {}).get("url", "Upgrade crate") if isinstance(v.get("advisory"), dict) else "",
        ))
    return findings


def _cargo_subcommand_available(subcmd: str) -> bool:
    """检查 cargo 子命令是否可用。"""
    try:
        proc = subprocess.run(
            ["cargo", subcmd, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _dep_java(project_path: str, *, ctx: "_ScanContext | None" = None) -> list[SecurityFinding]:
    """Java: dependency-check。"""
    if not shutil.which("dependency-check"):
        logger.info("Dep(java): dependency-check not found, skipping")
        return []

    out_dir = str(Path(project_path) / ".dc-report")
    cmd = ["dependency-check", "--scan", project_path, "--out", out_dir, "--format", "JSON"]
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path, timeout=300)
    if rc == -1:
        logger.warning("Dep(java): dependency-check execution failed: %s", stderr)
        return []
    _mark_ran(ctx)  # 工具已成功执行(rc!=-1)

    # dependency-check JSON 报告在 out_dir 下
    report_path = Path(out_dir) / "dependency-check-report.json"
    if not report_path.exists():
        logger.warning("Dep(java): dependency-check report not found at %s", report_path)
        return []

    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Dep(java): failed to read dependency-check report: %s", exc)
        return []

    findings: list[SecurityFinding] = []
    dependencies = data.get("dependencies", []) if isinstance(data, dict) else []
    for dep in dependencies:
        if not isinstance(dep, dict):
            continue
        vulns = dep.get("vulnerabilities", []) if isinstance(dep, dict) else []
        for v in vulns:
            sev_str = v.get("severity", "medium")
            sev = _map_vuln_severity(sev_str)
            findings.append(SecurityFinding(
                severity=sev,
                category="dependency",
                rule_id=v.get("name", ""),
                title=f"Vulnerability in {dep.get('fileName', 'unknown')}: {v.get('name', '')}",
                file="",
                line=0,
                tool="dependency-check",
                recommendation=v.get("description", "Upgrade dependency"),
            ))
    return findings


# ──────────────────────────────────────────────
# (c) 密钥扫描
# ──────────────────────────────────────────────

# 内置正则兜底 — 常见密钥模式
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str], Severity]] = [
    ("OpenAI API key", re.compile(r"sk-[a-zA-Z0-9]{20,}", re.IGNORECASE), Severity.CRITICAL),
    ("AWS Access Key ID", re.compile(r"AKIA[0-9A-Z]{16}"), Severity.CRITICAL),
    ("AWS Secret Access Key", re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*[A-Za-z0-9/+=]{40}"), Severity.CRITICAL),
    ("GitHub PAT", re.compile(r"ghp_[a-zA-Z0-9]{36}"), Severity.CRITICAL),
    ("Private Key", re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----"), Severity.CRITICAL),
    ("Slack Token", re.compile(r"xox[bposa]-[0-9a-zA-Z-]{10,}"), Severity.HIGH),
    ("Google API Key", re.compile(r"AIza[0-9A-Za-z\-_]{35}"), Severity.HIGH),
    ("Stripe Key", re.compile(r"(?:sk|pk)_(?:test|live)_[0-9a-zA-Z]{24,}"), Severity.HIGH),
    ("Generic Secret Assignment", re.compile(
        r"""(?i)(?:password|passwd|secret|token|api_key|apikey|access_key|private_key)\s*[=:]\s*['"][^'"]{8,}['"]"""
    ), Severity.HIGH),
]


def _run_secret_scan(
    project_path: str, *, files: list[str] | None = None, ctx: "_ScanContext | None" = None
) -> list[SecurityFinding]:
    """密钥扫描: gitleaks > trufflehog > 内置正则兜底。

    A-P0-2：gitleaks/trufflehog 是真实外部扫描器→执行成功时标记 ctx.scanner_ran；
    builtin-regex 兜底【不】标记（工具全缺时它也能跑，不能掩盖 SAST/依赖工具全缺）。
    """
    # 先尝试 gitleaks
    findings = _secret_gitleaks(project_path, ctx=ctx)
    if findings is not None:
        return findings

    # 再尝试 trufflehog
    findings = _secret_trufflehog(project_path, ctx=ctx)
    if findings is not None:
        return findings

    # 内置正则兜底（不标记 scanner_ran）
    return _secret_builtin_regex(project_path, files=files)


def _secret_gitleaks(project_path: str, *, ctx: "_ScanContext | None" = None) -> list[SecurityFinding] | None:
    """gitleaks 密钥扫描。None=工具不可用。"""
    if not shutil.which("gitleaks"):
        return None

    report_path = str(Path(project_path) / ".gitleaks-report.json")
    cmd = ["gitleaks", "detect", "--report-format", "json", "--report-path", report_path, "--no-git"]
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path, timeout=180)
    # gitleaks exit code 1 = leaks found, 0 = no leaks, 其他=错误
    if rc not in (0, 1):
        logger.warning("Secret scan: gitleaks execution failed (rc=%d): %s", rc, stderr)
        return None
    _mark_ran(ctx)  # gitleaks 已成功执行

    try:
        data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Secret scan: gitleaks report parse failed: %s", exc)
        return []

    findings: list[SecurityFinding] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("Results", data.get("findings", []))
    else:
        items = []

    for item in items:
        if not isinstance(item, dict):
            continue
        sev_str = item.get("severity", "high")
        sev = _map_vuln_severity(sev_str) if sev_str else Severity.HIGH
        findings.append(SecurityFinding(
            severity=sev,
            category="secret",
            rule_id=item.get("ruleID", item.get("RuleID", "")),
            title=f"Secret detected: {item.get('RuleID', item.get('ruleID', 'unknown'))}",
            file=item.get("File", item.get("file", "")),
            line=item.get("StartLine", item.get("startLine", 0)),
            tool="gitleaks",
            recommendation="Rotate the exposed secret immediately and use a secrets manager",
        ))
    return findings


def _secret_trufflehog(project_path: str, *, ctx: "_ScanContext | None" = None) -> list[SecurityFinding] | None:
    """trufflehog 密钥扫描。None=工具不可用。"""
    if not shutil.which("trufflehog"):
        return None

    cmd = ["trufflehog", "filesystem", "--json", project_path]
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path, timeout=180)
    if rc == -1:
        logger.warning("Secret scan: trufflehog execution failed: %s", stderr)
        return None
    _mark_ran(ctx)  # trufflehog 已成功执行

    findings: list[SecurityFinding] = []
    for line in stdout.splitlines():
        data = _safe_json_parse(line.strip())
        if data is None or not isinstance(data, dict):
            continue
        sev_str = data.get("severity", "high")
        sev = _map_vuln_severity(sev_str) if sev_str else Severity.HIGH
        metadata = data.get("SourceMetadata", {})
        if isinstance(metadata, dict):
            fpath = metadata.get("File", "")
            line_num = metadata.get("Line", 0)
        else:
            fpath = ""
            line_num = 0
        findings.append(SecurityFinding(
            severity=sev,
            category="secret",
            rule_id=data.get("DetectorName", ""),
            title=f"Secret detected: {data.get('DetectorName', 'unknown')}",
            file=fpath,
            line=line_num,
            tool="trufflehog",
            recommendation="Rotate the exposed secret immediately",
        ))
    return findings


def _secret_builtin_regex(
    project_path: str, *, files: list[str] | None = None
) -> list[SecurityFinding]:
    """内置正则密钥扫描（兜底，不依赖外部工具）。"""
    findings: list[SecurityFinding] = []
    root = Path(project_path)

    # 确定扫描文件列表
    scan_files: list[Path] = []
    if files:
        for f in files:
            p = root / f
            if p.is_file():
                scan_files.append(p)
    else:
        # 扫描常见源码文件（排除 .git, node_modules, .venv 等）
        skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", "dist", "build", "target"}
        skip_exts = {".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".png", ".jpg", ".gif", ".pdf", ".zip", ".gz"}
        for p in root.rglob("*"):
            if any(skip in p.parts for skip in skip_dirs):
                continue
            if p.suffix in skip_exts:
                continue
            if p.is_file() and p.stat().st_size < 2_000_000:  # 2MB 限制
                scan_files.append(p)

    for fpath in scan_files:
        try:
            content = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        for line_no, line in enumerate(content.splitlines(), start=1):
            for label, pattern, sev in _SECRET_PATTERNS:
                if pattern.search(line):
                    # 避免同一行同一模式重复报告
                    findings.append(SecurityFinding(
                        severity=sev,
                        category="secret",
                        rule_id=f"builtin-secret-{label.lower().replace(' ', '-')}",
                        title=f"Potential {label} detected",
                        file=str(fpath.relative_to(root)) if fpath.is_relative_to(root) else str(fpath),
                        line=line_no,
                        tool="builtin-regex",
                        recommendation="Verify and rotate the exposed secret if valid",
                    ))
                    break  # 一行只报一个最强匹配

    return findings
