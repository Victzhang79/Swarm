"""S1-5（task#21）：migration/SQL 执行验证——通道判定纯函数 + 沙箱执行器 + 启动收割。

工程定位（docs/RUNTIME_SMOKE_DESIGN.md 定案摘要第 5 点 / §1.3 / §5.4）：
  沙箱环境铁的事实（S1-1 取证）：非 root、网络封锁、五个模板全都没有 PG/MySQL/Redis 服务。
  所以真实外部 DB 的 migration 执行【必然不可行】——诚实 skipped+reason 是本模块的高频主路径；
  只有当【项目自身证据】表明它配置了嵌入式/文件型 DB（manifest 依赖 + 配置 URL 双证据）
  才存在可执行通道。用户明令：绝不拿 sqlite/H2 冒充 MySQL/PG 语义；语法级静态校验不做
  （没有引擎背书的"语法检查"是假绿源，宁可 skipped）。

三条验证通道（按 migration_kind 数据表分派，栈/框架词汇只进数据表）：
  ① runs_on_startup（flyway/liquibase × Spring Boot）：框架启动时自动执行 migration
     ——复用运行时冒烟的启动本身，不造独立命令；结论从冒烟启动日志【收割】
     （harvest_startup_migration，成功形态数据表）。
  ② 直接执行（alembic × 嵌入式 sqlalchemy.url / prisma × provider="sqlite"）：
     execute_migration 在冒烟同一沙箱内跑命令，__RC__ 口径 + infra≠失败三分类
     （对齐 runtime_smoke 的 D31 ran/ok 区分）。
  ③ 无通道（golang-migrate/raw-sql/真实外部 DB/证据不全）：不可执行 + reason 留痕。

铁律：fail-closed（推不出/证据不全→不可执行，绝不猜）；skipped 必须可观测带 reason；
环境缺失绝不伪装代码失败；判定纯函数零网络/零沙箱 IO，可完全离线单测，坏输入绝不抛。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

# 复用勿复制：backend 画像解析/配置键读取的单一权威在 smoke_derive（S1-2 纯函数层）
from brain.smoke_derive import (
    _env_lookup,
    _properties_lookup,
    _read,
    _split_backend,
    _yaml_lookup,
)
from brain.stack_detect import _NOISE_DIRS

logger = logging.getLogger(__name__)

# migration 命令执行预算（秒）：嵌入式 DB 无网络往返，300s 足够且远小于沙箱寿命
MIGRATION_EXEC_TIMEOUT_SEC = 300
_MAX_WALK_DIRS = 3000

# ══════════════════════ 数据表（唯一允许含栈/框架词汇的地方） ══════════════════════

# kind → 启动时自动执行 migration 的框架族（_split_backend 产出的小写框架名）。
# 依据：Spring Boot 对 classpath 上的 flyway/liquibase 默认启动即执行（框架契约）。
# 其他框架（Quarkus 默认 migrate-at-start=false 等）无"必然执行"证据 → 不入表，宁缺勿假绿。
_STARTUP_MIGRATION_FRAMEWORKS: dict[str, frozenset[str]] = {
    "flyway": frozenset({"spring boot"}),
    "liquibase": frozenset({"spring boot"}),
}

# 语言 → 嵌入式/文件型 DB 的 manifest 依赖 marker（小写 substring，各栈坐标形态）
_JVM_EMBEDDED_MARKERS = (
    "com.h2database", "org.hsqldb", "hsqldb", "org.apache.derby",
    "sqlite-jdbc", "org.xerial",
)
_EMBEDDED_DEP_MARKERS: dict[str, tuple[str, ...]] = {
    "java": _JVM_EMBEDDED_MARKERS,
    "kotlin": _JVM_EMBEDDED_MARKERS,
    "scala": _JVM_EMBEDDED_MARKERS,
    "javascript/typescript": ("better-sqlite3", "sql.js", '"sqlite3"'),
    # python 不在表内：sqlite3 是标准库内置，依赖侧证据天然满足（见 detect_embedded_db_evidence）
}
_PY_BUILTIN_EMBEDDED_EVIDENCE = "sqlite3 (python 标准库内置，依赖侧天然满足)"

# 嵌入式 DB URL 形态正则（IGNORECASE）。jdbc:derby://host 是网络 server 形态，故负向排除。
_EMBEDDED_URL_PATTERNS: tuple[str, ...] = (
    r"jdbc:h2:(?:mem|file)",
    r"jdbc:hsqldb:(?:mem|file)",
    r"jdbc:derby:(?!//)",
    r"jdbc:sqlite:",
    r"sqlite(?:\+\w+)?://",
    r":memory:",
)

# datasource/DB URL 配置源：(文件名, 解析格式, 键)。按序尝试，首个非空值胜出。
# 注：URL 是占位符（${...}）/外部形态时按"非嵌入式"处理——fail-closed 决策正确（不可执行）。
_DB_URL_SOURCES: tuple[tuple[str, str, Any], ...] = (
    ("application.properties", "properties", "spring.datasource.url"),
    ("application.yml", "yaml", ("spring", "datasource", "url")),
    ("application.yaml", "yaml", ("spring", "datasource", "url")),
    ("application.properties", "properties", "quarkus.datasource.jdbc.url"),
    ("alembic.ini", "properties", "sqlalchemy.url"),
    (".env", "env", "DATABASE_URL"),
    (".env", "env", "DB_URL"),
)

# 依赖证据面读取的清单文件名
_MANIFEST_FILES: tuple[str, ...] = (
    "pom.xml", "build.gradle", "build.gradle.kts", "package.json",
    "requirements.txt", "pyproject.toml", "go.mod", "Cargo.toml",
    "Gemfile", "composer.json",
)

# runs_on_startup 收割：启动日志 migration 成功形态（kind keyed，IGNORECASE）
_STARTUP_SUCCESS_PATTERNS: dict[str, tuple[str, ...]] = {
    "flyway": (
        r"Successfully applied \d+ migrations?",
        r"Successfully validated \d+ migrations?",
        r"Schema .* is up to date\. No migration necessary",
        r"No migration necessary",
    ),
    "liquibase": (
        r"Update has been successful",
        r"UPDATE SUMMARY",
        r"Database is up to date",
        r"ChangeSet .* ran successfully",
    ),
}

# runs_on_startup 收割：启动日志 migration【失败】形态（kind keyed，IGNORECASE）。
# 审A 治本：失败形态必须最先查、且无论 smoke_status——冒烟 skipped/failed 时 log_tail 里的
# migration 失败证据同样是确定性证据（Flyway/Liquibase 失败即打这些形态，不因冒烟结论而变），
# 只有成功结论才需要"冒烟 passed"背书。缺这张表 = 启动式 migration 永远没有 failed 出口。
_STARTUP_FAILURE_PATTERNS: dict[str, tuple[str, ...]] = {
    "flyway": (
        r"FlywayException",
        r"Migration checksum mismatch",
        r"Migration .{0,80}failed",
    ),
    "liquibase": (
        r"liquibase\.exception",
        r"ValidationFailedException",
        r"Unexpected error running Liquibase",
    ),
}

# 执行器 failed 判据：SQL/migration 确定性错误形态（IGNORECASE）。
# 无命中绝不默认判代码错（inconclusive skipped）——与 runtime_smoke 三分类同款原则。
_MIGRATION_ERROR_PATTERNS: tuple[str, ...] = (
    r"SQL syntax",
    r"syntax error at or near",
    r"near \"[^\"]*\": syntax error",       # sqlite 语法错误形态
    r"duplicate column",
    r"relation .* does not exist",
    r"no such table",
    r"no such column",
    r"table .* already exists",
    r"UNIQUE constraint failed",
    r"Migration checksum mismatch",
    r"checksum mismatch",
    r"FlywayException",
    r"liquibase\.exception",
    r"alembic\.util\.exc",
    r"Can't locate revision",
    r"Target database is not up to date",
    r"P3\d{3}",                              # prisma migrate 错误码族
)


# ═══════════════════════════ 结果对象 ═══════════════════════════

@dataclass(frozen=True)
class MigrationChannel:
    """通道判定结果（纯函数产物）。

    executable=True 且 runs_on_startup=True：寄生冒烟启动（command 恒 None）；
    executable=True 且 command 非空：直接执行通道（execute_migration 消费）；
    executable=False：无通道，reason 说明缺哪个证据（skipped 可观测口径）。
    """
    executable: bool
    reason: str = ""
    command: str | None = None
    runs_on_startup: bool = False
    evidence: dict[str, str] = field(default_factory=dict)


@dataclass
class MigrationVerifyResult:
    """migration 验证三态结果。status ∈ passed|failed|skipped。

    reason 词表：executed / startup_log_evidence（passed）；sql_error（failed）；
    not_executed | inconclusive | no_startup_evidence | smoke_failed | smoke_skipped |
    smoke_not_executed（均 skipped）。
    """
    status: str
    reason: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════ 工作树只读证据面 ═══════════════════════════

def _find_files(project_path: str, names: set[str] | frozenset[str]) -> dict[str, list[str]]:
    """有界 os.walk 找目标文件名 → {basename: [relpath...]（浅→深）}。IO 失败容错空。"""
    found: dict[str, list[str]] = {}
    dir_count = 0
    try:
        for root, dirs, files in os.walk(project_path or ""):
            dirs[:] = sorted(d for d in dirs if d not in _NOISE_DIRS)
            dir_count += 1
            if dir_count > _MAX_WALK_DIRS:
                break
            rel = os.path.relpath(root, project_path)
            rel = "" if rel == "." else rel.replace(os.sep, "/")
            for f in files:
                if f in names:
                    found.setdefault(f, []).append(f"{rel}/{f}" if rel else f)
    except OSError:
        pass
    for lst in found.values():
        lst.sort(key=lambda p: (p.count("/"), p))
    return found


def _classify_db_url(url: str) -> str:
    """DB URL → "embedded"|"external"。非嵌入式形态一律 external（含占位符——fail-closed）。"""
    for pat in _EMBEDDED_URL_PATTERNS:
        if re.search(pat, url, re.IGNORECASE):
            return "embedded"
    return "external"


def detect_embedded_db_evidence(project_stack: Any, project_path: str) -> tuple[bool, str, dict[str, str]]:
    """嵌入式 DB 双证据检测（纯函数）→ (ok, reason, evidence)。

    两个证据都命中才 True：①manifest 依赖含嵌入式 DB（python 的 sqlite 标准库内置，
    依赖侧天然满足）；②项目配置的 datasource/DB URL 指向嵌入式形态。缺任一 → False，
    reason ∈ external_db_url | missing_embedded_url | missing_embedded_dependency |
    no_embedded_db_evidence。任何 IO/解析失败按无证据处理，绝不抛。
    """
    evidence: dict[str, str] = {}
    try:
        _fw, lang = _split_backend(project_stack)
        files = _find_files(str(project_path or ""),
                            set(_MANIFEST_FILES) | {f for f, _, _ in _DB_URL_SOURCES})

        # ① 依赖侧证据
        dep_ev: str | None = None
        if lang == "python":
            dep_ev = _PY_BUILTIN_EMBEDDED_EVIDENCE
        else:
            manifest_text = " ".join(
                _read(str(project_path or ""), rp, limit=120_000)
                for name in _MANIFEST_FILES for rp in files.get(name, [])[:8]
            ).lower()
            for marker in _EMBEDDED_DEP_MARKERS.get(lang, ()):
                if marker in manifest_text:
                    dep_ev = f"manifest 依赖 marker: {marker}"
                    break
        if dep_ev:
            evidence["dependency"] = dep_ev

        # ② URL 侧证据（首个显式值胜出）
        url_val: str | None = None
        for filename, fmt, key in _DB_URL_SOURCES:
            for rp in files.get(filename, []):
                text = _read(str(project_path or ""), rp)
                if not text:
                    continue
                if fmt == "properties":
                    raw = _properties_lookup(text, key)
                    label = key
                elif fmt == "yaml":
                    raw = _yaml_lookup(text, key)
                    label = ".".join(key)
                else:  # env
                    raw = _env_lookup(text, key)
                    label = key
                if isinstance(raw, str) and raw.strip():
                    url_val = raw.strip().strip("'\"")
                    evidence["db_url"] = f"{rp}: {label}={url_val}"
                    break
            if url_val:
                break

        if url_val:
            if _classify_db_url(url_val) == "embedded":
                if dep_ev:
                    return True, "embedded_db_evidence", evidence
                return False, "missing_embedded_dependency", evidence
            return False, "external_db_url", evidence
        if dep_ev:
            return False, "missing_embedded_url", evidence
        return False, "no_embedded_db_evidence", evidence
    except Exception as exc:  # noqa: BLE001 — 纯函数承诺不抛，检测失败=无证据（fail-closed）
        logger.debug("[MIGRATION_VERIFY] 嵌入式 DB 证据检测异常(按无证据处理): %s", exc)
        return False, "no_embedded_db_evidence", evidence


# ═══════════════════════════ 通道判定（kind 分派） ═══════════════════════════

def _channel_startup(kind: str, framework: str, lang: str,
                     project_stack: Any, project_path: str) -> MigrationChannel:
    """flyway/liquibase：Spring Boot 场景=启动自动执行，寄生冒烟（不造独立命令）。"""
    if framework in _STARTUP_MIGRATION_FRAMEWORKS.get(kind, frozenset()):
        # 嵌入式证据仅作诊断留痕（不参与决策：真实外部 DB 时启动本身会 env_missing，
        # 收割自然跟随 skipped，不会假绿）
        ok, reason, ev = detect_embedded_db_evidence(project_stack, project_path)
        return MigrationChannel(
            True, reason="runs_on_startup", command=None, runs_on_startup=True,
            evidence={"framework": framework, "embedded_db": f"{ok}({reason})", **ev})
    return MigrationChannel(
        False, reason="no_startup_channel",
        evidence={"framework": framework or "(未判明)",
                  "detail": f"{kind} 无启动自动执行框架证据，且沙箱内无独立 CLI 引擎"})


def _channel_alembic(kind: str, framework: str, lang: str,
                     project_stack: Any, project_path: str) -> MigrationChannel:
    """alembic：仅当 alembic.ini 的 sqlalchemy.url 是嵌入式（sqlite 族）才可执行。"""
    files = _find_files(str(project_path or ""), {"alembic.ini"})
    inis = files.get("alembic.ini", [])
    if not inis:
        return MigrationChannel(False, reason="missing_alembic_ini")
    rp = inis[0]
    raw = _properties_lookup(_read(str(project_path or ""), rp), "sqlalchemy.url")
    if not raw:
        return MigrationChannel(False, reason="missing_embedded_url",
                                evidence={"alembic_ini": rp})
    url = raw.strip().strip("'\"")
    if _classify_db_url(url) != "embedded":
        return MigrationChannel(False, reason="external_db_url",
                                evidence={"alembic_ini": rp, "db_url": url})
    inidir = os.path.dirname(rp).replace(os.sep, "/")
    # script_location 常见为 ini 相对路径 → 命令进 ini 所在目录执行
    cmd = "alembic upgrade head" if not inidir \
        else f"cd {shlex.quote(inidir)} && alembic upgrade head"
    return MigrationChannel(
        True, reason="embedded_db_evidence", command=cmd,
        evidence={"alembic_ini": rp, "db_url": url,
                  "dependency": _PY_BUILTIN_EMBEDDED_EVIDENCE})


_PRISMA_PROVIDER_RE = re.compile(
    r"datasource\s+\w+\s*\{[^}]*?provider\s*=\s*\"([^\"]+)\"", re.DOTALL)


def _channel_prisma(kind: str, framework: str, lang: str,
                    project_stack: Any, project_path: str) -> MigrationChannel:
    """prisma：仅嵌入式 provider="sqlite" 才可执行（prisma 自带 sqlite 引擎）。"""
    files = _find_files(str(project_path or ""), {"schema.prisma"})
    schemas = files.get("schema.prisma", [])
    if not schemas:
        return MigrationChannel(False, reason="missing_prisma_schema")
    rp = schemas[0]
    m = _PRISMA_PROVIDER_RE.search(_read(str(project_path or ""), rp, limit=60_000))
    if not m:
        return MigrationChannel(False, reason="missing_embedded_url",
                                evidence={"schema": rp})
    provider = m.group(1).strip().lower()
    if provider != "sqlite":
        return MigrationChannel(False, reason="external_db_provider",
                                evidence={"schema": rp, "provider": provider})
    cmd = "npx prisma migrate deploy" if rp == "prisma/schema.prisma" \
        else f"npx prisma migrate deploy --schema {shlex.quote(rp)}"
    return MigrationChannel(True, reason="embedded_db_evidence", command=cmd,
                            evidence={"schema": rp, "provider": provider})


def _channel_no_engine(kind: str, framework: str, lang: str,
                       project_stack: Any, project_path: str) -> MigrationChannel:
    """golang-migrate/raw-sql：沙箱内无 CLI 引擎且无框架启动通道——语法级静态校验
    不做（无引擎背书=假绿源）→ 诚实不可执行。"""
    return MigrationChannel(False, reason="no_embedded_engine", evidence={"kind": kind})


_CHANNEL_DERIVERS: dict[str, Any] = {
    "flyway": _channel_startup,
    "liquibase": _channel_startup,
    "alembic": _channel_alembic,
    "prisma": _channel_prisma,
    "golang-migrate": _channel_no_engine,
    "raw-sql": _channel_no_engine,
}


def detect_migration_channel(migration_kind: str | None, project_stack: Any,
                             project_path: str | None) -> MigrationChannel:
    """通道判定主入口（纯函数，只读文件 IO，绝不抛）。

    输入 migration_kind（smoke_derive.detect_migration_kind 产物）+ project_stack 画像
    + 工作树根 → MigrationChannel。kind/输入坏 → 不可执行 + reason（fail-closed）。
    """
    kind = (migration_kind or "").strip().lower()
    if not kind:
        return MigrationChannel(False, reason="no_migration_detected")
    deriver = _CHANNEL_DERIVERS.get(kind)
    if deriver is None:
        return MigrationChannel(False, reason="unknown_migration_kind",
                                evidence={"kind": kind})
    framework, lang = _split_backend(project_stack)
    try:
        return deriver(kind, framework, lang, project_stack, str(project_path or ""))
    except Exception as exc:  # noqa: BLE001 — 判定异常≠代码失败，fail-closed 不可执行
        logger.warning("[MIGRATION_VERIFY] 通道判定异常(fail-closed 不可执行): %s", exc)
        return MigrationChannel(False, reason="channel_detection_error",
                                evidence={"kind": kind, "error": str(exc)[:200]})


# ═══════════════════════════ runs_on_startup 收割（纯函数） ═══════════════════════════

def harvest_startup_migration(migration_kind: str | None, smoke_status: str | None,
                              log_tail: str | None) -> MigrationVerifyResult:
    """从冒烟结果收割 migration 结论（runs_on_startup 通道，纯函数）。

    审A：启动日志命中【失败形态】（数据表）→ failed sql_error 带命中行证据——最先查、
    无论 smoke_status（失败证据是确定性的，冒烟 skipped/failed 不稀释它）；
    冒烟 passed + 启动日志命中成功形态（数据表）→ passed 带日志证据；
    冒烟 passed 但无 migration 痕迹 → skipped no_startup_evidence（不假绿）；
    冒烟 failed/skipped/没跑（且无失败形态）→ migration 跟随 skipped（smoke_failed/
    smoke_skipped/smoke_not_executed），绝不在无证据时下结论。
    """
    kind = (migration_kind or "").strip().lower()
    text = log_tail or ""
    fail_patterns = _STARTUP_FAILURE_PATTERNS.get(kind, ())
    fail_hits = [p for p in fail_patterns if re.search(p, text, re.IGNORECASE)]
    if fail_hits:
        lines = [ln.strip() for ln in text.splitlines()
                 if any(re.search(p, ln, re.IGNORECASE) for p in fail_hits)][:5]
        return MigrationVerifyResult(
            "failed", "sql_error",
            f"冒烟启动日志命中 {kind} migration 失败形态（引擎执行确定性失败，可回灌）",
            evidence={"hits": fail_hits[:3], "log_lines": lines,
                      "smoke_status": smoke_status})
    if smoke_status != "passed":
        reason = "smoke_failed" if smoke_status == "failed" else (
            "smoke_skipped" if smoke_status == "skipped" else "smoke_not_executed")
        return MigrationVerifyResult(
            "skipped", reason,
            f"冒烟未通过({smoke_status or '未执行'})，无启动日志可收割 migration 结论（跟随 skipped）")
    patterns = _STARTUP_SUCCESS_PATTERNS.get(kind, ())
    hits = [p for p in patterns if re.search(p, text, re.IGNORECASE)]
    if hits:
        lines = [ln.strip() for ln in text.splitlines()
                 if any(re.search(p, ln, re.IGNORECASE) for p in hits)][:5]
        return MigrationVerifyResult(
            "passed", "startup_log_evidence",
            f"冒烟启动日志命中 {kind} migration 成功形态（引擎实际执行过）",
            evidence={"hits": hits[:3], "log_lines": lines})
    return MigrationVerifyResult(
        "skipped", "no_startup_evidence",
        "冒烟通过但启动日志无 migration 执行痕迹（可能日志截断/静默），无引擎背书不假绿")


# ═══════════════════════════ 执行器（async，__RC__ 口径） ═══════════════════════════

async def execute_migration(
    manager: Any,
    sandbox: Any,
    command: str,
    *,
    workdir: str = "/workspace",
    timeout_sec: int = MIGRATION_EXEC_TIMEOUT_SEC,
) -> MigrationVerifyResult:
    """在冒烟同一沙箱内执行 migration 命令并三分类（唯一通道 manager.run_command）。

    exit 0 → passed；非 0 且命中 SQL/migration 确定性错误形态 → failed 带证据（可回灌）；
    非 0 无形态 → inconclusive skipped（绝不默认判代码错）；run_command 异常/__RC__
    标记缺失 → not_executed skipped（infra≠失败，对齐 D31 ran/ok 口径）。
    """
    run_command = getattr(manager, "run_command", None)
    if run_command is None or sandbox is None or not (command or "").strip():
        return MigrationVerifyResult(
            "skipped", "not_executed", "沙箱执行通道不可用，migration 未执行",
            evidence={"ran": False})
    full = f"cd {shlex.quote(workdir)} && ({command}); echo __RC__$?"
    try:
        # run_command 同步阻塞 → 线程池（R23-1 口径，contextvars 拷贝沙箱上下文照常）
        result = await asyncio.to_thread(
            run_command, sandbox, full, timeout=int(timeout_sec), _skip_blacklist=True)
    except Exception as exc:  # noqa: BLE001 — infra 异常一律未执行，不误判 migration 失败
        logger.warning("[MIGRATION_VERIFY] run_command 异常(infra)，migration 未执行: %s",
                       str(exc)[:200])
        return MigrationVerifyResult(
            "skipped", "not_executed",
            f"migration 执行异常(infra)，未执行: {str(exc)[:200]}",
            evidence={"ran": False, "error": str(exc)[:500]})

    out = (getattr(result, "stdout", "") or "") + "\n" + (getattr(result, "stderr", "") or "")
    rcs = re.findall(r"__RC__(-?\d+)", out)
    if not rcs:
        return MigrationVerifyResult(
            "skipped", "not_executed",
            "migration 执行标记缺失(__RC__)，判定为基础设施中断，未执行",
            evidence={"ran": False, "raw_excerpt": out[-800:]})
    rc = int(rcs[-1])
    if rc == 0:
        return MigrationVerifyResult(
            "passed", "executed", f"migration 命令执行通过(exit 0): {command}",
            evidence={"ran": True, "exit_code": 0, "command": command})
    hits = [p for p in _MIGRATION_ERROR_PATTERNS if re.search(p, out, re.IGNORECASE)]
    if hits:
        return MigrationVerifyResult(
            "failed", "sql_error",
            f"migration 执行失败(exit {rc})：命中 SQL/migration 错误形态 {hits[:3]}",
            evidence={"ran": True, "exit_code": rc, "hits": hits[:5],
                      "command": command, "output_tail": out[-1500:]})
    return MigrationVerifyResult(
        "skipped", "inconclusive",
        f"migration 命令非 0 退出(exit {rc})但无确定性 SQL 错误形态，"
        "不确定（绝不默认判代码错），跳过",
        evidence={"ran": True, "exit_code": rc, "command": command,
                  "output_tail": out[-800:]})
