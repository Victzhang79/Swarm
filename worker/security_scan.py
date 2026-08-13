"""安全审计扫描器 — SAST / 依赖漏洞 / 密钥扫描，产出 list[SecurityFinding]。

支持 5 种语言: python / node / go / rust / java
两种模式: 阻断交付 (block_severity='critical') / 仅报告 (block_severity='none')
工具缺失一律优雅 skip (shutil.which 探测)，绝不崩。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from swarm.types import SecurityFinding, Severity

logger = logging.getLogger(__name__)
# G1-1d（round38c 主题G）：report-only 覆盖率缺口告警 warn-once（按 language）——每个
# AUDIT 子任务都打、同一栈重复刷屏（round38c ×9+5+3）；finding 注入已保证可观测，
# 日志仅运维提示，首次一条足矣。block-mode 的 fail-closed 决策日志不在此列（每次留痕）。
_scanner_absent_warned: set[str] = set()

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

# D7（19号文，先核有意性结论）：全部 _map_*_severity 对未知/缺失 severity 缺省 MEDIUM 是
# 【有意】而非疏漏——ECC 分级契约（DR-05-F5(#85) 对抗双复核裁定）：CRITICAL=block / HIGH=warn，
# "把通用模式提级 CRITICAL"的处方已被裁为【过激】撤销（RuoYi 基线 `CSRF_TOKEN = "csrf_token"`
# 常量名冤杀实证）。缺省 MEDIUM 同理 = FP 控制：pip-audit 默认 JSON 常无 severity 字段，依赖 CVE
# 一律提级会让 baseline 老依赖连坐杀新交付。需要更严的环境走配置收紧
# （security_block_severity=high），绝不在映射层静默提级。单点常量=语义显式化。
_UNKNOWN_SEVERITY_DEFAULT = Severity.MEDIUM

# 30 号文 C-2：外部密钥扫描器（gitleaks/trufflehog）报的、本仓正则表【认不出形态】的
# 密钥 finding 缺省档——显式 CRITICAL。★绝不复用 _UNKNOWN_SEVERITY_DEFAULT★：MEDIUM 是
# CVE 依赖那条刻意的 FP 控制契约（误报可由人放行），而密钥泄露不可撤销、闸后无人工复核
# 环节兜底——消费契约不同档（纪律 10③：共享事实源不变、消费契约随后果分档）。
_UNRECOGNIZED_SECRET_DEFAULT = Severity.CRITICAL


class _ScanContext:
    """跨各扫描器累积执行状态。scanner_ran=True 表示【至少一个真实工具成功执行】
    （rc != -1，即非缺失/非超时/非异常）——哪怕它本身零发现。

    注意：builtin-regex 密钥兜底【不算】真实扫描器（它在工具全缺时也能跑，会掩盖
    SAST/依赖工具全缺的事实）；只有外部工具真正执行才置位。

    D4（19号文）：per-category 粒度——单布尔时代任一类任一工具跑成即解除哨兵
    （java 有 spotbugs 无 dependency-check → 依赖漏洞 0 覆盖照常放行）。三类分账，
    阻断模式任一类 0 覆盖即注合成 finding。

    ★W-1★ skipped_tools 记录本趟扫描有哪些外部工具因缺失而未能执行，使"0 覆盖"
    从 category 级下探到具体工具名，供 progress/metrics/审计端消费。
    """

    __slots__ = ("scanner_ran", "sast_ran", "dep_ran", "secret_ran", "skipped_tools")

    def __init__(self) -> None:
        self.scanner_ran = False
        self.sast_ran = False
        self.dep_ran = False
        self.secret_ran = False
        self.skipped_tools: list[str] = []


# ──────────────────────────────────────────────
# 公共入口
# ──────────────────────────────────────────────
def run_security_scan(
    project_path: str,
    language: str,
    *,
    files: list[str] | None = None,
    block_severity: str = "critical",
) -> tuple[list[SecurityFinding], bool, dict[str, Any]]:
    """运行三类安全扫描，返回 (findings, should_block, scan_details)。

    Args:
        project_path: 项目根目录
        language: 主语言 (python/node/go/rust/java)
        files: 待扫描文件列表 (相对路径)；None=全项目
        block_severity: 阻断阈值 'critical'/'high'/'none'；
            'none'=纯报告模式不阻断

    Returns:
        (findings, should_block, scan_details):
        - should_block=True 表示存在 >= block_severity 的发现
        - scan_details 含 "skipped_tools"（缺失工具列表）与 "categories_ran"
          （sast/dep/secret 是否确有真实扫描器执行），使"0 覆盖"机读可辨。
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
            if language not in _scanner_absent_warned:
                _scanner_absent_warned.add(language)
                logger.warning(
                    "Security scan: no real scanner executed for language '%s' in report-only "
                    "mode (block_severity=none) — 0 coverage, NOT clean. Install scanners for "
                    "real signal.（同栈后续静默，覆盖率缺口仍以 finding 落库）",
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
        # A-P0-2 + D4 per-category fail-closed：阻断模式下，任一【类】0 覆盖（该类所有真实
        # 工具全缺/全超时/全解析失败）都不能与"真·零漏洞"混同放行——逐类注入 = 阈值级别的
        # 合成发现并强制阻断。单布尔时代的漏洞：java 有 spotbugs 无 dependency-check →
        # 依赖漏洞 0 覆盖照常放行。secret 类有内置正则兜底，实践上恒有覆盖（哨兵近于不触发，
        # 但语义保持正确：兜底失效时仍能 fail-closed）。
        _missing_cats = [
            cat
            for cat, ran in (("sast", ctx.sast_ran), ("dep", ctx.dep_ran), ("secret", ctx.secret_ran))
            if not ran
        ]
        if _missing_cats:
            logger.warning(
                "Security scan: 0 coverage for categories %s (language='%s', block mode, "
                "threshold=%s) — failing closed per-category.",
                _missing_cats,
                language,
                block_severity,
            )
            synthetic_sev = (
                block_severity
                if block_severity in _SEVERITY_ORDER
                else Severity.CRITICAL
            )
            for _cat in _missing_cats:
                findings.append(SecurityFinding(
                    severity=synthetic_sev,  # type: ignore[arg-type]
                    category=_cat,
                    rule_id=f"fail-closed-no-{_cat}-scanner",
                    title=f"{_SEVERITY_FAILCLOSED_TITLE} ({_cat} 0 coverage)",
                    file="",
                    line=0,
                    tool="swarm-security-gate",
                    recommendation=(
                        f"No real {_cat} scanner ran for this language (tooling absent/failed). "
                        "Install the relevant scanners (e.g. bandit/semgrep/gosec/clippy/spotbugs, "
                        "pip-audit/npm/govulncheck/cargo-audit/dependency-check, gitleaks/trufflehog) "
                        "or set security_block_severity=none to explicitly accept un-scanned deliveries."
                    ),
                ))
            should_block = True

    logger.info(
        "Security scan done: %d findings, should_block=%s (threshold=%s, scanner_ran=%s, "
        "sast_ran=%s, dep_ran=%s, secret_ran=%s, skipped_tools=%s)",
        len(findings),
        should_block,
        block_severity,
        ctx.scanner_ran,
        ctx.sast_ran,
        ctx.dep_ran,
        ctx.secret_ran,
        ctx.skipped_tools,
    )
    scan_details: dict[str, Any] = {
        "skipped_tools": ctx.skipped_tools,
        "categories_ran": {
            "sast": ctx.sast_ran,
            "dep": ctx.dep_ran,
            "secret": ctx.secret_ran,
        },
    }
    return findings, should_block, scan_details


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


def _mark_ran(
    ctx: "_ScanContext | None", category: str | None = None, *, aggregate: bool = True
) -> None:
    """标记：某真实扫描器已成功执行（rc != -1）。A-P0-2 fail-closed 判据用。
    category（D4 per-category 分账）："sast"/"dep"/"secret"，None=只置聚合位。
    aggregate=False（D4）：只置 category 分账位不置聚合位——内置正则密钥兜底用：
    它对 secret 类是真实覆盖，但若置聚合位会掩盖 SAST/依赖工具全缺（A-P0-2 原旨）。"""
    if ctx is not None:
        if aggregate:
            ctx.scanner_ran = True
        if category == "sast":
            ctx.sast_ran = True
        elif category == "dep":
            ctx.dep_ran = True
        elif category == "secret":
            ctx.secret_ran = True


def _mark_skipped(ctx: "_ScanContext | None", tool: str) -> None:
    """记录某个外部扫描器因本地工具缺失而未能执行。W-1 机读键来源。"""
    if ctx is not None:
        ctx.skipped_tools.append(tool)


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
        _mark_skipped(ctx, "bandit")
        return []

    # D8/H-2（批次6 R1 hunter）：files=[]（scope 内无可扫对象）≠ files=None（未限定）——
    # 空集回退全树会把 baseline 未触碰文件的旧账算到新交付头上（阻断模式连坐误杀）。
    # 空集=vacuous 覆盖：已尽扫描义务（无可扫对象），置 sast_ran 防哨兵误伤，诚实跳过。
    if files is not None and not files:
        logger.info("SAST(python): scope 内无可扫文件（空集），跳过（vacuous 覆盖，不回退全树）")
        _mark_ran(ctx, "sast")
        return []
    targets = files if files is not None else ["-r", "."]
    cmd = ["bandit", "-f", "json"] + targets
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path)
    if rc == -1:
        logger.warning("SAST(python): bandit execution failed: %s", stderr)
        return []
    data = _safe_json_parse(stdout)
    if data is None:
        # D1：输出不可解析=工具跑挂（rc≥1 崩溃形态）——不置 _mark_ran，让 fail-closed
        # 哨兵按"未扫"处理（gitleaks P2-2 同款姿势），杜绝"跑挂伪装扫过且干净"。
        logger.warning("SAST(python): bandit output not valid JSON, skipping")
        return []
    _mark_ran(ctx, "sast")  # D1：输出成功解析后才置位

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
    return mapping.get(sev.upper(), _UNKNOWN_SEVERITY_DEFAULT)


def _sast_node(project_path: str, *, files: list[str] | None = None, ctx: "_ScanContext | None" = None) -> list[SecurityFinding]:
    """Node SAST: semgrep --json (可选)。"""
    if not shutil.which("semgrep"):
        logger.info("SAST(node): semgrep not found, skipping")
        _mark_skipped(ctx, "semgrep")
        return []

    # D8：files 提供时限定扫描目标——全树扫会把 baseline 未触碰文件的旧账算到新交付头上
    # （阻断模式下连坐）。semgrep 原生接受文件/目录目标列表（cwd=project_path，相对路径直用）。
    # H-2（批次6 R1 hunter）：空集≠未限定——vacuous 覆盖置 sast_ran 后诚实跳过，不回退全树。
    if files is not None and not files:
        logger.info("SAST(node): scope 内无可扫文件（空集），跳过（vacuous 覆盖，不回退全树）")
        _mark_ran(ctx, "sast")
        return []
    targets = files if files is not None else [project_path]
    cmd = ["semgrep", "--json", "--config", "auto", *targets]
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path, timeout=300)
    if rc == -1:
        logger.warning("SAST(node): semgrep execution failed: %s", stderr)
        return []
    data = _safe_json_parse(stdout)
    if data is None:
        # D1：不可解析=跑挂（如 --config auto 离线拉不到规则）→ 不置位，哨兵按未扫处理
        logger.warning("SAST(node): semgrep output not valid JSON, skipping")
        return []
    _mark_ran(ctx, "sast")  # D1：输出成功解析后才置位

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
    return mapping.get(sev.upper(), _UNKNOWN_SEVERITY_DEFAULT)


def _sast_go(project_path: str, *, files: list[str] | None = None, ctx: "_ScanContext | None" = None) -> list[SecurityFinding]:
    """Go SAST: gosec -fmt=json。"""
    if not shutil.which("gosec"):
        logger.info("SAST(go): gosec not found, skipping")
        _mark_skipped(ctx, "gosec")
        return []

    # D8：files 提供时按【包模式】限定——gosec 只收 package pattern 不收文件清单，
    # 故取 scope 内 .go 文件的父目录去重 → ./dir/... 形式；scope 无 .go 文件=Go SAST
    # 对本子任务无对象，诚实跳过（不拿全树旧账连坐）。
    if files is not None:
        go_dirs = sorted({
            (str(Path(f).parent) if str(Path(f).parent) != "." else ".")
            for f in files if f.endswith(".go")
        })
        if not go_dirs:
            # H-2（批次6 R1）：vacuous 覆盖置 sast_ran——无可扫对象≠工具缺席，
            # 不置位会让 D4 哨兵把"scope 内无 .go"误报成"未扫"（误杀方向）。
            logger.info("SAST(go): scope 内无 .go 文件，跳过（D8 files 限定，vacuous 覆盖）")
            _mark_ran(ctx, "sast")
            return []
        patterns = [f"./{d}/..." if d != "." else "./..." for d in go_dirs]
    else:
        patterns = ["./..."]
    cmd = ["gosec", "-fmt=json", *patterns]
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path)
    if rc == -1:
        logger.warning("SAST(go): gosec execution failed: %s", stderr)
        return []
    data = _safe_json_parse(stdout)
    if data is None:
        logger.warning("SAST(go): gosec output not valid JSON, skipping")
        return []
    _mark_ran(ctx, "sast")  # D1：输出成功解析后才置位

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
    return mapping.get(sev.upper(), _UNKNOWN_SEVERITY_DEFAULT)


def _sast_rust(project_path: str, *, files: list[str] | None = None, ctx: "_ScanContext | None" = None) -> list[SecurityFinding]:
    """Rust SAST: cargo clippy 安全规则 (warn=->deny)。

    D8 诚实边界：clippy 走 cargo 编译模型只能整 crate 跑，无文件级目标参数——files
    限定不适用（baseline 连坐风险由 D8 已治的 semgrep/gosec/bandit 路径覆盖主要场景）。"""
    if not shutil.which("cargo"):
        logger.info("SAST(rust): cargo not found, skipping")
        _mark_skipped(ctx, "cargo-clippy")
        return []

    cmd = ["cargo", "clippy", "--message-format=json", "--", "-W", "clippy::all"]
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path, timeout=300)
    if rc == -1:
        logger.warning("SAST(rust): cargo clippy execution failed: %s", stderr)
        return []

    findings: list[SecurityFinding] = []
    _parsed_any = False  # D1：流式输出无单点解析——至少一行可解析/rc=0 才算"真跑成"
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        data = _safe_json_parse(line)
        if data is None:
            continue
        if not isinstance(data, dict):
            continue
        _parsed_any = True
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
        # D14：只按 is_primary 归因——非主 span 是 note/related 位置，逐 span 出 finding
        # 会把一条诊断复制成 N 条噪音。无 primary span 时回落一条（不丢信号）。
        _primary = [sp for sp in spans if sp.get("is_primary")]
        _emit = _primary if _primary else [{}]
        for sp in _emit:
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
    if rc == 0 or _parsed_any:
        _mark_ran(ctx, "sast")  # D1：rc=0（文档化成功）或输出可解析才置位——跑挂不解除哨兵
    return findings


def _sast_java(project_path: str, *, files: list[str] | None = None, ctx: "_ScanContext | None" = None) -> list[SecurityFinding]:
    """Java SAST: spotbugs。

    D8 诚实边界：spotbugs 分析编译产物（class/jar）按模块跑，无源码文件级目标参数——
    files 限定不适用。"""
    if not shutil.which("spotbugs"):
        logger.info("SAST(java): spotbugs not found, skipping")
        _mark_skipped(ctx, "spotbugs")
        return []

    cmd = ["spotbugs", "-xml", project_path]
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path, timeout=300)
    if rc == -1:
        logger.warning("SAST(java): spotbugs execution failed: %s", stderr)
        return []
    # N-11 修复：spotbugs `-xml` 产 XML，原代码用 _safe_json_parse 当 JSON 解析→恒 None→
    # Java diff 永报零发现(静默失效)。改为正确解析 spotbugs XML(BugCollection/BugInstance)。
    if not stdout.strip():
        # D1：空输出不视为"扫过"（不置 _mark_ran，spotbugs 成功时必有 XML 骨架）
        return []
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(stdout)
    except ET.ParseError as exc:
        # 解析失败显式告警(而非静默吞)——便于诊断"为何 Java 永远零发现"；
        # D1：不置 _mark_ran，哨兵按未扫处理
        logger.warning("SAST(java): spotbugs XML 解析失败: %s", exc)
        return []
    _mark_ran(ctx, "sast")  # D1：XML 成功解析后才置位

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
    return mapping.get(sev.lower(), _UNKNOWN_SEVERITY_DEFAULT)


# ──────────────────────────────────────────────
# (b) 依赖漏洞扫描
# ──────────────────────────────────────────────
def _run_dependency_scan(project_path: str, language: str, *, ctx: "_ScanContext | None" = None) -> list[SecurityFinding]:
    """依赖漏洞扫描。

    D8 诚实边界：依赖 CVE 归属项目级清单/环境（pip-audit 环境、npm audit 树、
    dependency-check jar 集），无"按 scope 文件限定"的语义——files 参数不适用；
    baseline 老依赖 CVE 的连坐治理方向是 baseline 快照 diff（独立机制，未实现）。"""
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
        _mark_skipped(ctx, "pip-audit")
        return []

    cmd = ["pip-audit", "--format=json"]
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path, timeout=180)
    if rc == -1:
        logger.warning("Dep(python): pip-audit execution failed: %s", stderr)
        return []
    data = _safe_json_parse(stdout)
    if data is None:
        logger.warning("Dep(python): pip-audit output not valid JSON, skipping")
        return []
    _mark_ran(ctx, "dep")  # D1：输出成功解析后才置位

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
    return mapping.get(sev.lower(), _UNKNOWN_SEVERITY_DEFAULT)


def _dep_node(project_path: str, *, ctx: "_ScanContext | None" = None) -> list[SecurityFinding]:
    """Node: npm audit --json。"""
    if not shutil.which("npm"):
        logger.info("Dep(node): npm not found, skipping")
        _mark_skipped(ctx, "npm-audit")
        return []

    cmd = ["npm", "audit", "--json"]
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path, timeout=180)
    if rc == -1:
        logger.warning("Dep(node): npm audit execution failed: %s", stderr)
        return []
    data = _safe_json_parse(stdout)
    if data is None:
        logger.warning("Dep(node): npm audit output not valid JSON, skipping")
        return []
    _mark_ran(ctx, "dep")  # D1：输出成功解析后才置位

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
    return mapping.get(sev.lower(), _UNKNOWN_SEVERITY_DEFAULT)


def _dep_go(project_path: str, *, ctx: "_ScanContext | None" = None) -> list[SecurityFinding]:
    """Go: govulncheck -json。"""
    if not shutil.which("govulncheck"):
        logger.info("Dep(go): govulncheck not found, skipping")
        _mark_skipped(ctx, "govulncheck")
        return []

    cmd = ["govulncheck", "-json", "./..."]
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path, timeout=180)
    if rc == -1:
        logger.warning("Dep(go): govulncheck execution failed: %s", stderr)
        return []
    _parsed_any = False  # D1：流式输出——至少一行可解析/rc=0 才算"真跑成"

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
        _parsed_any = True
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
    if rc == 0 or _parsed_any:
        _mark_ran(ctx, "dep")  # D1：rc=0 或输出可解析才置位——跑挂不解除哨兵
    return list(by_osv.values()) + findings


def _map_vuln_severity(sev: str) -> Severity:
    mapping = {"critical": Severity.CRITICAL, "high": Severity.HIGH,
               "medium": Severity.MEDIUM, "low": Severity.LOW}
    return mapping.get(sev.lower(), _UNKNOWN_SEVERITY_DEFAULT)


def _dep_rust(project_path: str, *, ctx: "_ScanContext | None" = None) -> list[SecurityFinding]:
    """Rust: cargo audit --json。"""
    if not shutil.which("cargo"):
        logger.info("Dep(rust): cargo not found, skipping")
        _mark_skipped(ctx, "cargo")
        return []

    # cargo audit 是子命令，先检查 cargo-audit 是否安装
    if not shutil.which("cargo-audit") and not _cargo_subcommand_available("audit"):
        logger.info("Dep(rust): cargo audit not found, skipping")
        _mark_skipped(ctx, "cargo-audit")
        return []

    cmd = ["cargo", "audit", "--json"]
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path, timeout=180)
    if rc == -1:
        logger.warning("Dep(rust): cargo audit execution failed: %s", stderr)
        return []
    data = _safe_json_parse(stdout)
    if data is None:
        logger.warning("Dep(rust): cargo audit output not valid JSON, skipping")
        return []
    _mark_ran(ctx, "dep")  # D1：输出成功解析后才置位

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
        _mark_skipped(ctx, "dependency-check")
        return []

    out_dir = str(Path(project_path) / ".dc-report")
    cmd = ["dependency-check", "--scan", project_path, "--out", out_dir, "--format", "JSON"]
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path, timeout=300)
    if rc == -1:
        logger.warning("Dep(java): dependency-check execution failed: %s", stderr)
        return []
    # dependency-check JSON 报告在 out_dir 下
    # D2：报告存在性/可解析检查【之前】绝不置 _mark_ran——起跑即崩（报告缺失）时
    # 置位=Java 依赖漏洞 0 覆盖假绿。报告成功读取解析后才算"真跑成"。
    report_path = Path(out_dir) / "dependency-check-report.json"
    if not report_path.exists():
        logger.warning("Dep(java): dependency-check report not found at %s", report_path)
        return []

    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Dep(java): failed to read dependency-check report: %s", exc)
        return []
    _mark_ran(ctx, "dep")  # D2：报告成功读取解析后才置位

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
# T2(ECC §A 移植)：在既有表基础上补齐 ECC opensource-sanitizer 的高置信 provider token
# （JWT/DB 连接串/GitHub fine-grained PAT/Google OAuth/Slack webhook/SendGrid/Mailgun/AWS 临时
# 凭证 ASIA）。判据：结构化 provider token=CRITICAL(误报率低、进交付即真泄露)；通用赋值/宽松式=HIGH
# (默认 block_severity=critical 不阻断，仅留痕)。全部栈无关、只匹配文本，绝不写死语言/框架。
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str], Severity]] = [
    ("OpenAI API key", re.compile(r"(?<![A-Za-z0-9])sk-[a-zA-Z0-9]{20,}", re.IGNORECASE), Severity.CRITICAL),  # D6：左边界——disk-/task- 后随长 id/hash 不再内嵌命中
    # AWS 长期(AKIA)+临时(ASIA)凭证 ID
    ("AWS Access Key ID", re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"), Severity.CRITICAL),
    # S-6b：容忍可选引号——原 `[=:]\s*[A-Za-z0-9/+=]{40}` 撞上引号即失配，回落到
    # HIGH 档的 Generic 规则（默认 block_severity=critical 下**不阻断**），于是"加个引号"
    # 就成了绕过 CRITICAL 闸的手段。而 YAML/JSON/Java 里带引号才是主流写法。
    ("AWS Secret Access Key", re.compile(
        r"""(?i)aws_secret_access_key\s*[=:]\s*['"]?[A-Za-z0-9/+=]{40}"""), Severity.CRITICAL),
    ("GitHub PAT", re.compile(r"ghp_[a-zA-Z0-9]{36}"), Severity.CRITICAL),
    # GitHub fine-grained PAT + oauth/server/user-to-server/refresh token
    ("GitHub fine-grained PAT", re.compile(r"github_pat_[A-Za-z0-9_]{22,}"), Severity.CRITICAL),
    ("GitHub OAuth/Server Token", re.compile(r"gh[ousr]_[A-Za-z0-9_]{36,}"), Severity.CRITICAL),
    ("Private Key", re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----"), Severity.CRITICAL),
    # JWT（三段 header.payload.signature，前两段各 ≥20，排除随手拼的短串）
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]{18,}\.eyJ[A-Za-z0-9_-]{18,}\.[A-Za-z0-9_-]+"), Severity.CRITICAL),
    # 带账密的数据库/消息队列连接串 [user]:pass@host（协议无关列举，非写死单一栈）。
    # 用户名可空——`redis://:password@host`（requirepass 无用户名）是 Redis/mongodb/amqp 的
    # 常见默认认证形态，故 user 段用 `*`（对抗复核 reviewer F1：原 `+` 要求非空用户名会漏报此式）。
    # D5：密码段【整体】为占位形态（${VAR}/$VAR/{{var}}/%s/%d）时不命中——占位恰是本闸
    # recommendation 教 worker 写的安全形态，命中即"照建议改仍阻断→escalate 成环"。
    ("DB Connection String with Credentials", re.compile(
        r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:@\s/]*:"
        r"(?!(?:\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\{\{[^}]*\}\}|%[sd])@)"
        r"[^@\s/]+@[^\s'\"]+"
    ), Severity.CRITICAL),
    # Google OAuth client secret
    ("Google OAuth Client Secret", re.compile(r"GOCSPX-[A-Za-z0-9_-]{10,}"), Severity.CRITICAL),
    # Slack incoming webhook（完整 URL）
    ("Slack Webhook", re.compile(
        r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+"
    ), Severity.CRITICAL),
    # SendGrid API key（固定 22.43 结构）
    ("SendGrid API Key", re.compile(r"SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}"), Severity.CRITICAL),
    # Mailgun API key（key- + 32 hex）
    ("Mailgun API Key", re.compile(r"\bkey-[0-9a-f]{32}\b"), Severity.CRITICAL),
    # DR-05-F5(#85)：★对抗双复核裁定提级 CRITICAL 处方过激，撤销★——原 finding 提议把这些提到
    # CRITICAL 以对齐 coding_standards"阻断"承诺，但：①破坏既有【刻意的 ECC 分级契约】(CRITICAL=block
    # / HIGH=warn 不 block，test_secret_gate_t2 模块级"纪律"固化 + 3 处断言)；②hunter CONFIRMED HIGH
    # 实证误报——RuoYi 基线 `CSRF_TOKEN = "csrf_token"`(常量名非密钥)会被 Generic Secret 命中→CRITICAL
    # →冤杀阻断（AUDIT 还扫 readable 未改文件）。HIGH=warn 是【刻意的 FP 控制设计】非缺陷。真缺陷=
    # coding_standards 措辞过度承诺→改措辞对齐（见 coding_standards.py:25），severity 维持原样。
    ("Slack Token", re.compile(r"xox[bposa]-[0-9a-zA-Z-]{10,}"), Severity.HIGH),
    ("Google API Key", re.compile(r"AIza[0-9A-Za-z\-_]{35}"), Severity.HIGH),
    ("Stripe Key", re.compile(r"(?:sk|pk)_(?:test|live)_[0-9a-zA-Z]{24,}"), Severity.HIGH),
    ("Generic Secret Assignment", re.compile(
        r"""(?i)(?:password|passwd|secret|token|api_key|apikey|access_key|private_key)\s*[=:]\s*['"][^'"]{8,}['"]"""
    ), Severity.HIGH),
    # ── 26 号文 S-6 召回补齐（本表是 MERGE 交付 diff / AUDIT 项目扫描 / 经验技能导入准入
    #    【三闸的唯一共享事实源】，实测下列形态召回率为 0）──
    # 1) 现代 provider key：原 `sk-[a-zA-Z0-9]{20,}` 在第一个连字符处断裂，
    #    2024 起 OpenAI 默认发的就是 `sk-proj-…`，Anthropic/OpenRouter 同带连字符。
    ("OpenAI project key", re.compile(
        r"(?<![A-Za-z0-9])sk-proj-[A-Za-z0-9_-]{20,}"), Severity.CRITICAL),
    ("Anthropic API key", re.compile(
        r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9-]{2,}-[A-Za-z0-9_-]{20,}"), Severity.CRITICAL),
    ("OpenRouter API key", re.compile(
        r"(?<![A-Za-z0-9])sk-or-v1-[A-Za-z0-9]{32,}"), Severity.CRITICAL),
    ("Groq API key", re.compile(r"(?<![A-Za-z0-9])gsk_[A-Za-z0-9]{40,}"), Severity.CRITICAL),
    ("HuggingFace token", re.compile(r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{30,}"), Severity.CRITICAL),
    ("xAI API key", re.compile(r"(?<![A-Za-z0-9])xai-[A-Za-z0-9]{40,}"), Severity.CRITICAL),
    # 2) 加密私钥头：原表只有未加密的 PEM 头
    ("Encrypted private key", re.compile(
        r"-----BEGIN (?:ENCRYPTED|RSA ENCRYPTED) PRIVATE KEY-----"), Severity.CRITICAL),
    # 3) ★无引号 kv 形态★：Java/Spring（本仓 E2E 基线 RuoYi 的栈）最常见的凭据写法，
    #    而 Generic Secret Assignment 强制要求引号 → properties/YAML/env/export 全漏。
    #    值排除 `${...}`/`{{...}}` 占位与纯注释（D5 同款抑制），长度≥6 防误报常量名。
    ("Unquoted secret assignment", re.compile(
        # 前导不用 \b：`DB_PASSWORD`/`MY_SECRET` 里下划线是单词字符，\b 不成立会整类漏掉
        r"""(?im)^[^\n#]*(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b"""
        # 值必须"像密钥"而不像代码：排除函数调用/传参（含 ()、,）与纯小写标识符
        # （`password=password,`、`api_key = resolve_credential(` 这类正常赋值实测占误报
        # 全部——全仓 18 个非 test 命中无一是真泄露），并要求至少含一个数字/大写/符号。
        r"""\s*[=:]\s*(?!['"]|\$\{|\{\{|<|\s*$)"""
        # 断言只在【值本身的字符集】内扫，不跨括号/逗号——否则 `api_key = f(\"CFG_NAME\")`
        # 里后面的大写会让断言成立（实测这是残留误报的全部来源）。
        r"""(?=[^\s'"#(),]{6,})[^\s'"#(),]*[0-9!@#$%^&*+/][^\s'"#(),]*"""
    ), Severity.HIGH),
    # 4) jdbc/query-param 口令：DB 连接串的主流写法，原表只认 `scheme://user:pass@host`
    ("DB URL password param", re.compile(
        r"""(?i)[?&](?:password|pwd)=(?!\$\{|\{\{)[^&\s'"]{6,}"""), Severity.CRITICAL),
]


def _strongest_secret_match(
    line: str,
) -> tuple[str, Severity, "re.Match[str]"] | None:
    """扫【全表】取 severity 最高的命中（并列取表内最先）；无命中 → None。

    ★#29 C-1 治本：绝不依赖 _SECRET_PATTERNS 的表内顺序★
    本表按【来源批次】组织（ECC §A 移植 → DR-05-F5 分级裁定 → 26 号文 S-6 召回补齐），
    每批上方有成段注释解释该批判据，故 HIGH 档的通用规则（#16 Generic Secret Assignment /
    #24 Unquoted secret assignment）排在 CRITICAL 档的 provider token（#17-22 / #25）**之前**。
    原实现两处 `break` 于首个匹配、注释却自称"最强匹配"——表没排序，首个 ≠ 最强。实测
    `api_key = "sk-proj-…"`（带引号是 YAML/JSON/Java 主流写法）被 Generic Secret Assignment
    遮蔽成 HIGH → should_block=False → MERGE 交付闸走"仅留痕不阻断"分支放行真 provider key。

    取 max 而非"把表排序"：排序把正确性寄托在后来人 append 时摆对位置（且会打乱批次注释），
    而本函数是**顺序无关**的——未来任何 append 都不可能重新引入该缺陷。
    并列取最先＝保留同档内"具体规则优先于通用规则"的既有语义。

    ★不改 severity 分级本身★：DR-05-F5(#85) 对抗双复核裁定的 CRITICAL=block / HIGH=warn
    是【刻意的 FP 控制设计】（RuoYi 基线 `CSRF_TOKEN = "csrf_token"` 冤杀实证），本函数只
    修"CRITICAL 被 HIGH 遮蔽"，不动任何 pattern 的档位。
    """
    best: tuple[str, Severity, re.Match[str]] | None = None
    best_rank = -1
    for label, pattern, sev in _SECRET_PATTERNS:
        m = pattern.search(line)
        if not m:
            continue
        rank = _SEVERITY_ORDER.get(sev, 0)
        if rank > best_rank:
            best, best_rank = (label, sev, m), rank
    return best


def scan_text_for_secrets(
    text: str, *, min_severity: str | None = None,
) -> list[tuple[str, str]]:
    """在任意文本里检出泄露密钥（复用 _SECRET_PATTERNS，栈无关）。

    返回 [(pattern_name, 脱敏后的命中片段), ...]；无命中 → []。供经验技能【导入准入闸】
    校验技能正文不得内嵌密钥（与 diff 扫描同一 20+ pattern 源）。

    ★min_severity（W1 复核 HIGH-2 整改）★：本表的 HIGH 档是【刻意的 FP 控制设计】——
    见 _SECRET_PATTERNS 上方注释：`Generic Secret Assignment` 会把 RuoYi 基线的
    `CSRF_TOKEN = "csrf_token"`（常量名非密钥）命中，故 DR-05-F5(#85) 对抗复核裁定
    HIGH=warn 不阻断；本函数原先丢弃 severity（`for name, pat, _sev`），调用方拿不到档位
    只能"命中即拒"。这对**有人工复核**的消费端（MERGE 走 escalate、技能导入由人放行）
    是安全的宁误报；但对**无人工复核、直接丢弃**的消费端（知识库入库闸）就是冤杀，
    且误杀不可见。故暴露档位让各消费端按自身后果严重性选阈值——
    共享模式表（单一事实源）不变，**消费契约随后果分档**。
    语义＝【最低档】（含更高档），不是精确匹配：`min_severity="high"` 返回 HIGH∪CRITICAL。
    传 None（默认）＝全档返回，保持既有调用方行为逐字不变。
    """
    out: list[tuple[str, str]] = []
    if not text:
        return out
    _floor = _SEVERITY_RANK.get(str(min_severity).lower(), 0) if min_severity else 0
    for name, pat, _sev in _SECRET_PATTERNS:
        if _floor and _SEVERITY_RANK.get(_severity_key(_sev), 0) < _floor:
            continue
        m = pat.search(text)
        if m:
            out.append((name, _redact_secret(m.group(0))))
    return out


# 档位序（W1 复核 HIGH-2）：供 min_severity 做"≥ 阈值"过滤。取值与 Severity 枚举同源，
# 未知档位按 0 处理＝不被任何 floor 过滤掉（fail-open 方向：宁可多报给调用方去判）。
# ★#29-1R F6★ 从 _SEVERITY_ORDER **派生**，不再手抄第二份字面量。
# 原先两张表并存：若新增一档只登记进其中一张，另一张对该档返回 0 ——
# 落在 _strongest_secret_match 上就是"该档全部 pattern rank 并列 0 → 取 max 退化成取表内最先"
# ＝ C-1 修的那个缺陷**原型复发且零信号**（血规 10③：共享事实源要复用，别抄第二份）。
# 派生后 info 档也在表内（值 0）：`_floor` 判真值，floor=0 与"查不到按 0"行为逐位等价。
_SEVERITY_RANK: dict[str, int] = {
    _sev_key: _rank
    for _sev, _rank in _SEVERITY_ORDER.items()
    if (_sev_key := str(getattr(_sev, "value", _sev)).lower())
}


def _severity_key(sev: object) -> str:
    """Severity 枚举/字符串 → 小写档位名（兼容 Enum.value 与裸串）。"""
    return str(getattr(sev, "value", sev)).lower()


def _redact_secret(value: str) -> str:
    """脱敏：只保留前 4 字符 + '…'（ECC 铁律：绝不在日志/报告里显示密钥全文）。"""
    v = value or ""
    if len(v) <= 4:
        return "****"
    return v[:4] + "…"


def _parse_diff_new_path(header_line: str) -> str:
    """从 `+++ b/path`（或 `+++ path`）头行抽出文件路径；`/dev/null` → 空。"""
    raw = header_line[4:].strip()  # 去掉 "+++ "
    # 去掉尾部可能的 tab+timestamp（POSIX diff 格式）
    raw = raw.split("\t", 1)[0].strip()
    if raw == "/dev/null":
        return ""
    if raw.startswith(("a/", "b/")):
        raw = raw[2:]
    return raw


_HUNK_NEW_START = re.compile(r"@@\s+-\d+(?:,\d+)?\s+\+(\d+)")


def _parse_hunk_new_start(hunk_line: str) -> int:
    """从 `@@ -a,b +c,d @@` 抽出新文件侧起始行号 c；解析失败回退 0。"""
    m = _HUNK_NEW_START.search(hunk_line)
    return int(m.group(1)) if m else 0


def scan_diff_for_secrets(
    diff: str, *, block_severity: str = "critical"
) -> tuple[list[SecurityFinding], bool]:
    """扫描 unified diff 的【新增行】，复用 _SECRET_PATTERNS 检出泄露密钥。

    T2(ECC §A) — 交付前确定性硬闸的核心：MERGE 出口对 merged_diff 调用本函数，命中
    >= block_severity 的密钥即阻断交付。设计要点：

      - **栈无关**：只解析 diff 文本 + 正则，绝不依赖语言/落盘/外部工具（与 _secret_builtin_regex
        同源正则表，但数据源是 diff 新增行而非磁盘文件）。
      - **只扫新增行**（`+` 前缀、排除 `+++` 文件头）：删除密钥(`-`)与上下文行(' ')不算——
        删掉一条硬编码密钥是好事，绝不因此阻断。
      - **文件+行号归因**：跟踪 `+++ b/<file>` 头与 `@@ +c` hunk 起点，逐新增行推进行号。
      - **脱敏**：finding 不含密钥原文，只放脱敏首 4 字符（杜绝日志/交付报告二次泄露）。
      - **宁误报不漏报**：调用方(MERGE)对命中走 escalate 人工复核(非硬丢)，误报由人一眼放行，
        漏报=密钥进交付。故这里不做激进白名单。

    Returns (findings, should_block)：should_block=True 表示存在 >= block_severity 的发现。
    """
    findings: list[SecurityFinding] = []
    current_file = ""
    new_line_no = 0  # 新文件侧行号（hunk 起点 + 新增/上下文行递推）

    for raw in (diff or "").splitlines():
        # 文件头 `+++ ` 必须先于 `+` 判定（前者以 `+` 起）
        if raw.startswith("+++ "):
            current_file = _parse_diff_new_path(raw)
            continue
        if raw.startswith("--- ") or raw.startswith("diff ") or raw.startswith("index "):
            continue
        if raw.startswith("@@"):
            new_line_no = _parse_hunk_new_start(raw)
            continue
        if raw.startswith("+"):
            content = raw[1:]
            # 一行只报一个最强匹配（#29 C-1：原 `break` 于首个匹配，而表非按 severity 排序
            # ⇒ 真 provider key 被 HIGH 档通用规则遮蔽 → should_block=False 放行）
            hit = _strongest_secret_match(content)
            if hit:
                label, sev, m = hit
                findings.append(SecurityFinding(
                    severity=sev,
                    category="secret",
                    rule_id=f"builtin-secret-{label.lower().replace(' ', '-')}",
                    title=f"Potential {label} in delivery diff (redacted: {_redact_secret(m.group(0))})",
                    file=current_file,
                    line=new_line_no,
                    tool="builtin-regex-diff",
                    recommendation=(
                        "Remove the hardcoded secret from the delivery, rotate it, "
                        "and load it from an environment variable / secrets manager"
                    ),
                ))
            new_line_no += 1
        elif raw.startswith("-"):
            # 删除行：不计入新文件行号、不扫描
            continue
        elif raw.startswith("\\"):
            # D14：`\ No newline at end of file` 是 diff 元数据而非文件内容行——
            # 计入行号会让其后所有 finding 行号归因偏移一位。
            continue
        else:
            # 上下文行（' ' 前缀或空行）：推进新文件行号、不扫描
            new_line_no += 1

    should_block = any(_severity_gte(f.severity, block_severity) for f in findings)
    return findings, should_block


def _run_secret_scan(
    project_path: str, *, files: list[str] | None = None, ctx: "_ScanContext | None" = None
) -> list[SecurityFinding]:
    """密钥扫描: gitleaks ∪ trufflehog ∪ 内置正则【并集合并】（30 号文 C-2）。

    原实现是「提前 return」兜底链：gitleaks 在场时 21 条内置 CRITICAL 正则整块不跑，
    密钥档位完全外包给外部工具的 severity 字符串——同一条真 provider key 从
    CRITICAL(阻断) 变 HIGH(放行)。C-2 治本：三路都跑、并集合并；每条外部命中过
    _regrade_external_secret_findings 做本仓分档（档位绝不外包），再按
    (file, line, rule_id) 去重取最强档。
    ★否决「把内置正则挪到 gitleaks 之前」★——只是换个顺序被顶掉，还丢掉外部工具
    的额外覆盖面（gitleaks 认得的形态本仓表可能未收录，认不出的显式落 CRITICAL）。

    覆盖记账不变（A-P0-2/D4/L-1）：外部工具真跑成由各 helper 内 _mark_ran 置位
    （聚合+category）；builtin-regex 只置 category 不置聚合（不掩盖 SAST/依赖工具全缺）。
    """
    findings: list[SecurityFinding] = []
    ext = _secret_gitleaks(project_path, ctx=ctx)
    if ext is not None:
        findings.extend(ext)
    ext = _secret_trufflehog(project_path, ctx=ctx)
    if ext is not None:
        findings.extend(ext)
    # 内置正则【恒跑】（C-2：不再被外部工具在场顶掉）
    findings.extend(_secret_builtin_regex(project_path, files=files, ctx=ctx))
    return _dedup_secret_findings(_regrade_external_secret_findings(findings, project_path))


def _regrade_external_secret_findings(
    findings: list[SecurityFinding], project_path: str
) -> list[SecurityFinding]:
    """C-2 本仓分档：外部工具（gitleaks/trufflehog）的 secret 档位不由外部字符串决定。

    取每条外部 finding 所在行过 _strongest_secret_match：
      - 本仓表命中 → 取 max(外部档, 本仓档)（本仓只升不降，外部已判更高档的保留）；
      - 本仓认不出（含文件不可读/行号缺席/行号越界）→ 显式 _UNRECOGNIZED_SECRET_DEFAULT
        （CRITICAL——密钥泄露不可撤销，与 CVE 的 MEDIUM 缺省是不同消费契约），并 WARNING
        留痕（机读可辨，绝不静默提级）。
    内置正则（tool 以 builtin 开头）本就按本仓表分档，原样通过。
    """
    root = Path(project_path)
    lines_cache: dict[str, list[str] | None] = {}

    def _line_of(f: SecurityFinding) -> tuple[str | None, str | None]:
        """取 finding 所在行。返 (行内容, None)；取不到返 (None, 根因)——
        根因机读可辨：line-missing / path-escape / file-unreadable / line-out-of-range。"""
        if not f.file or f.line <= 0:
            return None, "line-missing"
        if f.file not in lines_cache:
            p = Path(f.file)
            if not p.is_absolute():
                p = root / p
            try:
                # 批5 R1 reviewer MEDIUM：外部工具报告的 file 是【外部输入】——囚禁在项目
                # 根内（绝对路径/../ 逃逸/符号链接越界一律按不可读处理 → 认不出 → CRITICAL，
                # fail-closed 方向），绝不读项目外文件。
                rp = p.resolve()
                if not rp.is_relative_to(root.resolve()):
                    lines_cache[f.file] = None
                    return None, "path-escape"
                lines_cache[f.file] = rp.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                lines_cache[f.file] = None
        lines = lines_cache[f.file]
        if lines is None:
            return None, "file-unreadable"
        if f.line > len(lines):
            return None, "line-out-of-range"
        return lines[f.line - 1], None

    out: list[SecurityFinding] = []
    # 批5 R1 hunter LOW1+LOW3：认不出逐条 WARNING 会刷屏且「外部已 CRITICAL」时不留痕——
    # 改每趟扫描一条汇总，带根因分账（机读可辨：模式未命中/文件不可读/行号缺席或越界）。
    unrecognized: dict[str, int] = {}
    for f in findings:
        if f.tool.startswith("builtin"):
            out.append(f)
            continue
        line, miss_reason = _line_of(f)
        hit = _strongest_secret_match(line) if line is not None else None
        if hit is not None:
            _label, local_sev, _m = hit
            new_sev = (
                local_sev
                if _SEVERITY_ORDER.get(local_sev, 0) >= _SEVERITY_ORDER.get(f.severity, 0)
                else f.severity
            )
        else:
            new_sev = _UNRECOGNIZED_SECRET_DEFAULT
            unrecognized[miss_reason or "pattern-miss"] = unrecognized.get(miss_reason or "pattern-miss", 0) + 1
        if new_sev != f.severity:
            f = f.model_copy(update={"severity": new_sev})
        out.append(f)
    if unrecognized:
        total = sum(unrecognized.values())
        breakdown = ", ".join(f"{reason}={n}" for reason, n in sorted(unrecognized.items()))
        logger.warning(
            "C-2 本仓分档：%d 条外部 secret finding 本仓表认不出（%s），"
            "已显式落 %s（密钥泄露不可撤销；误报方向为刻意取舍，见 30 号文 C-2）",
            total, breakdown, _UNRECOGNIZED_SECRET_DEFAULT.value,
        )
    return out


def _dedup_secret_findings(findings: list[SecurityFinding]) -> list[SecurityFinding]:
    """C-2 并集去重：按 (file, line, rule_id) 去重，同键保留最高档（首见序稳定）。

    键含 rule_id：同一行被 gitleaks 与内置正则各报一条（rule_id 不同）时两条都留——
    「每行取最强档」已由 _regrade_external_secret_findings 的行级重分档保证。
    """
    seen: dict[tuple[str, int, str], int] = {}
    out: list[SecurityFinding] = []
    for f in findings:
        key = (f.file, f.line, f.rule_id)
        idx = seen.get(key)
        if idx is None:
            seen[key] = len(out)
            out.append(f)
        elif _SEVERITY_ORDER.get(f.severity, 0) > _SEVERITY_ORDER.get(out[idx].severity, 0):
            out[idx] = f
    return out


def _secret_gitleaks(project_path: str, *, ctx: "_ScanContext | None" = None) -> list[SecurityFinding] | None:
    """gitleaks 密钥扫描。None=工具不可用。"""
    if not shutil.which("gitleaks"):
        _mark_skipped(ctx, "gitleaks")
        return None

    # D14：报告写到项目树外的临时文件——写进项目树会把 .gitleaks-report.json 混进
    # worker 交付 diff（污染交付物/触发 scope 外变更告警）。
    fd, report_path = tempfile.mkstemp(prefix="gitleaks-report-", suffix=".json")
    os.close(fd)
    try:
        cmd = ["gitleaks", "detect", "--report-format", "json", "--report-path", report_path, "--no-git"]
        rc, stdout, stderr = _run_tool(cmd, cwd=project_path, timeout=180)
        # gitleaks exit code 1 = leaks found, 0 = no leaks, 其他=错误
        if rc not in (0, 1):
            logger.warning("Secret scan: gitleaks execution failed (rc=%d): %s", rc, stderr)
            return None
        try:
            data = json.loads(Path(report_path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # P2-2：报告解析失败不再"已扫过+零发现"（fail-open）——不置 _mark_ran，让上游
            # fail-closed 哨兵按"未扫"处理（与工具没跑同等对待），杜绝解析坏=漏洞清零假绿。
            # D3：返回 None 而非 []——None=本路【未跑成】，C-2 并集里被跳过（其余两路照跑）；
            # 若返回 [] 会被当成"扫过且干净"，且 _mark_ran 语义被污染。
            logger.warning("Secret scan: gitleaks report parse failed（按未扫落兜底链）: %s", exc)
            return None
        _mark_ran(ctx, "secret")  # gitleaks 已成功执行且报告可解析
    finally:
        Path(report_path).unlink(missing_ok=True)

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
        _mark_skipped(ctx, "trufflehog")
        return None

    cmd = ["trufflehog", "filesystem", "--json", project_path]
    rc, stdout, stderr = _run_tool(cmd, cwd=project_path, timeout=180)
    if rc == -1:
        logger.warning("Secret scan: trufflehog execution failed: %s", stderr)
        return None

    findings: list[SecurityFinding] = []
    _parsed_any = False  # D1：流式输出——至少一行可解析/rc=0 才算"真跑成"
    for line in stdout.splitlines():
        data = _safe_json_parse(line.strip())
        if data is None or not isinstance(data, dict):
            continue
        _parsed_any = True
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
    if rc != 0 and not _parsed_any:
        # D1/D3：跑挂（非 0 且输出不可解析）→ None=本路未跑成，C-2 并集里被跳过，不置位不假空
        logger.warning("Secret scan: trufflehog 输出不可解析(rc=%d)，按未扫落兜底", rc)
        return None
    _mark_ran(ctx, "secret")  # D1：rc=0 或输出可解析才置位
    return findings


def _secret_builtin_regex(
    project_path: str, *, files: list[str] | None = None, ctx: "_ScanContext | None" = None
) -> list[SecurityFinding]:
    """内置正则密钥扫描（兜底，不依赖外部工具）。

    L-1（批次6 R1）：至少实扫一个文件才置 secret_ran（category 覆盖）——0 文件置位
    会让 secret 类哨兵永久失效（假覆盖）。"""
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

    if scan_files:
        # L-1（批次6 R1 hunter）：置位绑定"至少实扫一个文件"——0 文件也置位会让
        # secret 类 per-category 哨兵永久失效（假覆盖）。
        _mark_ran(ctx, "secret", aggregate=False)
    for fpath in scan_files:
        try:
            content = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        for line_no, line in enumerate(content.splitlines(), start=1):
            # 一行只报一个最强匹配（#29 C-1：与 scan_diff_for_secrets 同源修复——原两处
            # 各自 `break` 于首个匹配，档位取决于表内顺序而非严重度）
            hit = _strongest_secret_match(line)
            if hit:
                label, sev, _m = hit
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

    return findings
