"""S1-5（task#21）：migration/SQL 执行验证 — 行为测试（禁 getsource 结构焊死）。

覆盖面：
1. 通道判定纯函数（数据表驱动，各栈正反例）：嵌入式证据齐全→executable、
   只有依赖没有 URL→不可执行带 reason、真实 MySQL/PG URL→不可执行、
   flyway/liquibase 在 Spring Boot=runs_on_startup（寄生冒烟，不造独立命令）、
   golang-migrate/raw-sql=无内嵌引擎背书→不可执行。
2. runs_on_startup 收割三态：冒烟 passed+日志命中成功形态→passed；
   passed 无痕迹→skipped no_startup_evidence；冒烟没跑/skipped/failed→跟随 skipped。
3. 执行器（stub 沙箱，__RC__ 口径）：exit0→passed；非0+SQL 错误形态→failed 带证据；
   非0 无形态→inconclusive（绝不默认判代码错）；infra/标记缺失→not_executed。
4. verify_runtime 集成：无 kind→None 不 degraded；migration failed→并入 runtime
   失败通道(classification=migration_failed)；skipped→degraded_reasons 留痕；
   冒烟 failed 时 migration 跟随 skip（执行器绝不被调）。
"""

from __future__ import annotations

import asyncio

import pytest

import swarm.brain.migration_verify as mv
import swarm.brain.nodes.verify as verify_mod
from swarm.brain.migration_verify import (
    MigrationChannel,
    MigrationVerifyResult,
    detect_embedded_db_evidence,
    detect_migration_channel,
    execute_migration,
    harvest_startup_migration,
)
from swarm.brain.nodes.runtime_smoke import RuntimeSmokeResult
from swarm.brain.smoke_derive import SmokeDerivation

# ───────────────────────── 工具 ─────────────────────────

_SPRING = {"backend": "Spring Boot (java)"}
_GO = {"backend": "Gin (go)"}
_PY = {"backend": "python"}
_NODE = {"backend": "Express (javascript/typescript)"}

_POM_H2 = (
    "<project><dependencies><dependency>"
    "<groupId>com.h2database</groupId><artifactId>h2</artifactId>"
    "</dependency></dependencies></project>"
)


def _write(root, rel: str, content: str = "") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ───────────────────────── 通道判定：kind 分派 ─────────────────────────

def test_channel_none_kind(tmp_path):
    ch = detect_migration_channel(None, _SPRING, str(tmp_path))
    assert ch.executable is False and ch.runs_on_startup is False
    assert ch.reason == "no_migration_detected"


def test_channel_unknown_kind(tmp_path):
    ch = detect_migration_channel("weird-tool", _SPRING, str(tmp_path))
    assert ch.executable is False
    assert ch.reason == "unknown_migration_kind"


@pytest.mark.parametrize("kind", ["flyway", "liquibase"])
def test_channel_startup_on_spring_boot(tmp_path, kind):
    ch = detect_migration_channel(kind, _SPRING, str(tmp_path))
    assert ch.executable is True
    assert ch.runs_on_startup is True
    assert ch.command is None  # 复用冒烟启动本身，绝不造独立命令


@pytest.mark.parametrize("kind", ["flyway", "liquibase"])
def test_channel_startup_needs_framework_evidence(tmp_path, kind):
    # 非启动自动执行框架（无 flyway CLI 可用证据）→ 无通道，如实不可执行
    ch = detect_migration_channel(kind, _GO, str(tmp_path))
    assert ch.executable is False and ch.runs_on_startup is False
    assert ch.reason == "no_startup_channel"


def test_channel_alembic_sqlite_executable(tmp_path):
    _write(tmp_path, "alembic.ini",
           "[alembic]\nscript_location = migrations\nsqlalchemy.url = sqlite:///app.db\n")
    ch = detect_migration_channel("alembic", _PY, str(tmp_path))
    assert ch.executable is True and ch.runs_on_startup is False
    assert ch.command == "alembic upgrade head"
    assert "alembic.ini" in ch.evidence.get("alembic_ini", "")


def test_channel_alembic_nested_ini_command_enters_dir(tmp_path):
    _write(tmp_path, "backend/alembic.ini", "sqlalchemy.url = sqlite:///dev.sqlite3\n")
    ch = detect_migration_channel("alembic", _PY, str(tmp_path))
    assert ch.executable is True
    assert ch.command.startswith("cd backend")
    assert ch.command.endswith("alembic upgrade head")


def test_channel_alembic_real_mysql_url_not_executable(tmp_path):
    # 用户明令：绝不拿 sqlite 冒充 MySQL 语义——真实外部 DB 无执行通道
    _write(tmp_path, "alembic.ini", "sqlalchemy.url = mysql+pymysql://root@db:3306/app\n")
    ch = detect_migration_channel("alembic", _PY, str(tmp_path))
    assert ch.executable is False
    assert ch.reason == "external_db_url"


def test_channel_alembic_missing_url_not_executable(tmp_path):
    _write(tmp_path, "alembic.ini", "[alembic]\nscript_location = migrations\n")
    ch = detect_migration_channel("alembic", _PY, str(tmp_path))
    assert ch.executable is False
    assert ch.reason == "missing_embedded_url"


def test_channel_alembic_missing_ini(tmp_path):
    ch = detect_migration_channel("alembic", _PY, str(tmp_path))
    assert ch.executable is False
    assert ch.reason == "missing_alembic_ini"


def test_channel_prisma_sqlite_executable(tmp_path):
    _write(tmp_path, "prisma/schema.prisma",
           'datasource db {\n  provider = "sqlite"\n  url = "file:./dev.db"\n}\n')
    ch = detect_migration_channel("prisma", _NODE, str(tmp_path))
    assert ch.executable is True
    assert ch.command == "npx prisma migrate deploy"


def test_channel_prisma_postgres_not_executable(tmp_path):
    _write(tmp_path, "prisma/schema.prisma",
           'datasource db {\n  provider = "postgresql"\n  url = env("DATABASE_URL")\n}\n')
    ch = detect_migration_channel("prisma", _NODE, str(tmp_path))
    assert ch.executable is False
    assert ch.reason == "external_db_provider"


@pytest.mark.parametrize("kind", ["golang-migrate", "raw-sql"])
def test_channel_no_embedded_engine(tmp_path, kind):
    # 语法级静态校验不做（无引擎背书=假绿源）→ 诚实不可执行
    ch = detect_migration_channel(kind, _GO, str(tmp_path))
    assert ch.executable is False
    assert ch.reason == "no_embedded_engine"


def test_channel_never_raises_on_bad_input():
    ch = detect_migration_channel("alembic", {"backend": "python"}, None)
    assert ch.executable is False  # 容错 fail-closed，绝不抛


# ───────────────────────── 嵌入式 DB 证据（各栈正反例） ─────────────────────────

def test_embedded_java_dep_and_url(tmp_path):
    _write(tmp_path, "pom.xml", _POM_H2)
    _write(tmp_path, "src/main/resources/application.properties",
           "spring.datasource.url=jdbc:h2:mem:testdb\n")
    ok, reason, ev = detect_embedded_db_evidence(_SPRING, str(tmp_path))
    assert ok is True and reason == "embedded_db_evidence"
    assert "dependency" in ev and "db_url" in ev


def test_embedded_java_dep_without_url(tmp_path):
    _write(tmp_path, "pom.xml", _POM_H2)
    ok, reason, _ = detect_embedded_db_evidence(_SPRING, str(tmp_path))
    assert ok is False and reason == "missing_embedded_url"


def test_embedded_java_real_mysql_url(tmp_path):
    _write(tmp_path, "pom.xml", _POM_H2)
    _write(tmp_path, "src/main/resources/application.properties",
           "spring.datasource.url=jdbc:mysql://db:3306/app\n")
    ok, reason, _ = detect_embedded_db_evidence(_SPRING, str(tmp_path))
    assert ok is False and reason == "external_db_url"


def test_embedded_node_sqlite(tmp_path):
    _write(tmp_path, "package.json", '{"dependencies": {"better-sqlite3": "^9.0.0"}}')
    _write(tmp_path, ".env", "DATABASE_URL=sqlite:///data/app.db\n")
    ok, reason, _ = detect_embedded_db_evidence(_NODE, str(tmp_path))
    assert ok is True and reason == "embedded_db_evidence"


def test_embedded_python_stdlib_dep(tmp_path):
    # python 的 sqlite 是标准库内置——依赖侧证据天然满足，URL 侧仍必须命中嵌入式形态
    _write(tmp_path, ".env", "DATABASE_URL=sqlite:///db.sqlite3\n")
    ok, reason, ev = detect_embedded_db_evidence(_PY, str(tmp_path))
    assert ok is True
    assert "sqlite3" in ev["dependency"]


def test_embedded_url_without_dependency(tmp_path):
    _write(tmp_path, "pom.xml", "<project/>")
    _write(tmp_path, "application.properties", "spring.datasource.url=jdbc:h2:mem:x\n")
    ok, reason, _ = detect_embedded_db_evidence(_SPRING, str(tmp_path))
    assert ok is False and reason == "missing_embedded_dependency"


def test_embedded_no_evidence(tmp_path):
    ok, reason, _ = detect_embedded_db_evidence(_SPRING, str(tmp_path))
    assert ok is False and reason == "no_embedded_db_evidence"


# ───────────────────────── runs_on_startup 收割三态 ─────────────────────────

def test_harvest_flyway_success():
    r = harvest_startup_migration(
        "flyway", "passed",
        "INFO o.f.core.Flyway - Successfully applied 3 migrations to schema PUBLIC")
    assert r.status == "passed" and r.reason == "startup_log_evidence"
    assert r.evidence["log_lines"]  # 日志证据留痕


def test_harvest_liquibase_success():
    r = harvest_startup_migration("liquibase", "passed",
                                  "liquibase: Update has been successful.")
    assert r.status == "passed"


def test_harvest_passed_without_trace_is_skipped():
    r = harvest_startup_migration("flyway", "passed", "Tomcat started on port 8080")
    assert r.status == "skipped" and r.reason == "no_startup_evidence"


@pytest.mark.parametrize("smoke_status,reason", [
    ("failed", "smoke_failed"),
    ("skipped", "smoke_skipped"),
    (None, "smoke_not_executed"),
])
def test_harvest_follows_smoke_non_pass(smoke_status, reason):
    r = harvest_startup_migration("flyway", smoke_status, "irrelevant log")
    assert r.status == "skipped" and r.reason == reason


# ── 审A：启动日志失败形态 → failed（最先查，无论 smoke_status）──────────

_FLYWAY_FAIL_LOG = (
    "INFO  o.f.core.Flyway - Migrating schema PUBLIC to version 2\n"
    "ERROR o.s.boot.SpringApplication - Application run failed\n"
    "org.flywaydb.core.api.FlywayException: Migration V2__add_col.sql failed\n"
    "SQL State  : 42001")


@pytest.mark.parametrize("smoke_status", ["passed", "failed", "skipped", None])
def test_harvest_flyway_failure_pattern_wins_regardless_of_smoke_status(smoke_status):
    """失败形态是确定性证据——冒烟 skipped/failed 时同样成立，绝不被跟随逻辑遮蔽。"""
    r = harvest_startup_migration("flyway", smoke_status, _FLYWAY_FAIL_LOG)
    assert r.status == "failed" and r.reason == "sql_error"
    assert r.evidence["hits"]
    assert any("FlywayException" in ln for ln in r.evidence["log_lines"])


def test_harvest_liquibase_failure_pattern():
    r = harvest_startup_migration(
        "liquibase", "skipped",
        "liquibase.exception.ValidationFailedException: Validation Failed: 1 changesets check sum")
    assert r.status == "failed" and r.reason == "sql_error"


def test_harvest_failure_pattern_kind_scoped():
    # kind 不匹配的失败词不误伤（flyway 日志喂 liquibase 表 → 不命中失败形态）
    r = harvest_startup_migration("liquibase", "passed", _FLYWAY_FAIL_LOG)
    assert r.status == "skipped" and r.reason == "no_startup_evidence"


# ───────────────────────── 执行器（stub 沙箱，__RC__ 口径） ─────────────────────────

class _ExecResult:
    def __init__(self, stdout: str = "", stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr


class _ExecManager:
    def __init__(self, stdout: str = "", exc: Exception | None = None):
        self.stdout = stdout
        self.exc = exc
        self.commands: list[str] = []

    def run_command(self, sandbox, command, timeout=120, **kwargs):
        self.commands.append(command)
        if self.exc:
            raise self.exc
        return _ExecResult(stdout=self.stdout)


def _exec(mgr, command: str = "alembic upgrade head") -> MigrationVerifyResult:
    return asyncio.run(execute_migration(mgr, object(), command, workdir="/workspace"))


def test_exec_exit_zero_passed():
    r = _exec(_ExecManager(stdout="ok\n__RC__0"))
    assert r.status == "passed" and r.reason == "executed"


def test_exec_sql_error_failed_with_evidence():
    mgr = _ExecManager(stdout="sqlite3.OperationalError: duplicate column name: age\n__RC__1")
    r = _exec(mgr)
    assert r.status == "failed" and r.reason == "sql_error"
    assert r.evidence["hits"]                                # 确定性证据可回灌
    assert "duplicate column" in r.evidence["output_tail"]


def test_exec_nonzero_without_sql_pattern_is_inconclusive():
    r = _exec(_ExecManager(stdout="some totally unrelated failure\n__RC__2"))
    assert r.status == "skipped" and r.reason == "inconclusive"  # 绝不默认判代码错


def test_exec_marker_missing_is_not_executed():
    r = _exec(_ExecManager(stdout="envd upstream 502"))
    assert r.status == "skipped" and r.reason == "not_executed"  # infra≠失败


def test_exec_infra_exception_is_not_executed():
    r = _exec(_ExecManager(exc=RuntimeError("socket closed")))
    assert r.status == "skipped" and r.reason == "not_executed"


def test_exec_runs_command_in_workdir():
    mgr = _ExecManager(stdout="__RC__0")
    _exec(mgr, command="alembic upgrade head")
    assert "alembic upgrade head" in mgr.commands[0]
    assert "/workspace" in mgr.commands[0]


# ───────────────────────── verify_runtime 集成 ─────────────────────────

class _StubResult:
    def __init__(self, stdout: str = "", stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr


class _StubSandbox:
    def __init__(self, sid: str):
        self.sandbox_id = sid


class _StubManager:
    """最小沙箱经理 stub（与 S1-4 同款）：记账 create/kill/extend。"""

    def __init__(self, *, instances=None, extend_ok=True, remaining=None,
                 rebuild_stdout="__RC__0"):
        self._instances = dict(instances or {})
        self.extend_ok = extend_ok
        self.remaining = remaining
        self.rebuild_stdout = rebuild_stdout
        self.killed: list[str] = []
        self.created: list[str] = []
        self.synced = False

    def try_extend_lifetime(self, sandbox, seconds):
        return self.extend_ok

    def remaining_lifetime(self, sandbox_id):
        return self.remaining

    def create(self, project_id=None, source=""):
        self.created.append(source)
        sb = _StubSandbox("sb-selfbuilt")
        self._instances[sb.sandbox_id] = sb
        return sb

    def sync_project_to_sandbox(self, sandbox, path, workdir):
        self.synced = True

    def run_command(self, sandbox, command, timeout=120, **kwargs):
        return _StubResult(stdout=self.rebuild_stdout)

    def kill(self, sandbox_id):
        self.killed.append(sandbox_id)
        self._instances.pop(sandbox_id, None)


def _deriv(kind=None) -> SmokeDerivation:
    return SmokeDerivation(start_cmd="run-the-app", port=8080, health_path="/health",
                           migration_kind=kind, evidence={"start_cmd": "manifest 证据"})


def _smoke(status: str, classification: str, *, log_tail: str = "") -> RuntimeSmokeResult:
    return RuntimeSmokeResult(status, classification, f"stub-{status}", log_tail=log_tail,
                              details={"probe_sequence": []})


@pytest.fixture()
def wired(monkeypatch):
    ctx = {
        "manager": _StubManager(),
        "derivation": _deriv(None),
        "smoke": _smoke("passed", "started"),
        "smoke_calls": [],
    }
    monkeypatch.delenv("SWARM_RUNTIME_SMOKE_ENABLED", raising=False)

    import swarm.brain.integration_review as ir
    import swarm.brain.nodes as nodes_pkg
    import swarm.brain.nodes.runtime_smoke as rs
    import swarm.brain.smoke_derive as sd
    import swarm.worker.sandbox as ws

    monkeypatch.setattr(nodes_pkg, "_get_project_path", lambda pid: "/tmp/fake-project")
    monkeypatch.setattr(nodes_pkg, "_sandbox_available", lambda: True)
    monkeypatch.setattr(sd, "derive_runtime_smoke", lambda stack, path: ctx["derivation"])
    monkeypatch.setattr(ws, "get_sandbox_manager", lambda: ctx["manager"])
    monkeypatch.setattr(ir, "_detect_build_cmd_generic", lambda p: "stub-build")

    async def _fake_run_smoke(manager, sandbox, script, **kwargs):
        ctx["smoke_calls"].append(1)
        return ctx["smoke"]

    monkeypatch.setattr(rs, "run_runtime_smoke", _fake_run_smoke)
    return ctx


def _run_node(state: dict) -> dict:
    return asyncio.run(verify_mod.verify_runtime(state))


def test_node_no_migration_is_none_not_degraded(wired):
    out = _run_node({"project_id": "p1"})
    assert out["runtime_smoke_passed"] is True
    assert out["migration_verify_passed"] is None
    assert out["migration_verify_details"]["reason"] == "no_migration_detected"
    assert "degraded_reasons" not in out  # 没有 migration 是常态，不是降级


def test_node_migration_failed_folds_into_runtime_channel(wired, monkeypatch):
    wired["derivation"] = _deriv("alembic")
    monkeypatch.setattr(
        mv, "detect_migration_channel",
        lambda kind, stack, path: MigrationChannel(
            True, reason="embedded_db_evidence", command="alembic upgrade head"))

    async def _fake_exec(manager, sandbox, command, **kw):
        return MigrationVerifyResult("failed", "sql_error", "duplicate column",
                                     evidence={"hits": ["duplicate column"]})

    monkeypatch.setattr(mv, "execute_migration", _fake_exec)
    out = _run_node({"project_id": "p1"})
    assert out["runtime_smoke_passed"] is False
    assert out["verification_failure"] == "runtime_smoke"  # task#20 归因回灌统一消费
    assert out["runtime_smoke_details"]["classification"] == "migration_failed"
    assert out["migration_verify_passed"] is False
    assert out["migration_verify_details"]["reason"] == "sql_error"


def test_node_migration_exec_passed(wired, monkeypatch):
    wired["derivation"] = _deriv("alembic")
    monkeypatch.setattr(
        mv, "detect_migration_channel",
        lambda kind, stack, path: MigrationChannel(True, command="alembic upgrade head"))

    async def _fake_exec(manager, sandbox, command, **kw):
        return MigrationVerifyResult("passed", "executed", "ok")

    monkeypatch.setattr(mv, "execute_migration", _fake_exec)
    out = _run_node({"project_id": "p1"})
    assert out["runtime_smoke_passed"] is True
    assert out["migration_verify_passed"] is True
    assert "degraded_reasons" not in out


def test_node_startup_harvest_passed(wired):
    wired["derivation"] = _deriv("flyway")
    wired["smoke"] = _smoke(
        "passed", "started",
        log_tail="Flyway - Successfully applied 2 migrations to schema PUBLIC")
    out = _run_node({"project_id": "p1", "project_stack": {"backend": "Spring Boot (java)"}})
    assert out["runtime_smoke_passed"] is True
    assert out["migration_verify_passed"] is True
    assert out["migration_verify_details"]["reason"] == "startup_log_evidence"


def test_node_startup_without_trace_degraded(wired):
    wired["derivation"] = _deriv("flyway")
    wired["smoke"] = _smoke("passed", "started", log_tail="Tomcat started on port 8080")
    out = _run_node({"project_id": "p1", "project_stack": {"backend": "Spring Boot (java)"}})
    assert out["runtime_smoke_passed"] is True  # 冒烟结论不受影响
    assert out["migration_verify_passed"] is None
    assert out["degraded_reasons"] == ["migration_verify_skipped:no_startup_evidence"]


def test_node_smoke_failed_migration_follows_skip(wired, monkeypatch):
    wired["derivation"] = _deriv("alembic")
    wired["smoke"] = _smoke("failed", "code_error", log_tail="TRACE")
    exec_calls: list[int] = []
    monkeypatch.setattr(
        mv, "detect_migration_channel",
        lambda kind, stack, path: MigrationChannel(True, command="alembic upgrade head"))

    async def _fake_exec(*a, **k):
        exec_calls.append(1)
        return MigrationVerifyResult("passed", "executed", "must not run")

    monkeypatch.setattr(mv, "execute_migration", _fake_exec)
    out = _run_node({"project_id": "p1"})
    assert exec_calls == []  # 冒烟失败时 migration 绝不执行
    assert out["runtime_smoke_passed"] is False
    assert out["verification_failure"] == "runtime_smoke"
    assert out["runtime_smoke_details"]["classification"] == "code_error"  # 不被 migration 覆盖
    assert out["migration_verify_passed"] is None
    assert out["migration_verify_details"]["reason"] == "smoke_failed"
    assert "migration_verify_skipped:smoke_failed" in out["degraded_reasons"]


def test_node_smoke_skipped_startup_harvest_follows(wired):
    wired["derivation"] = _deriv("flyway")
    wired["smoke"] = _smoke("skipped", "env_missing")
    out = _run_node({"project_id": "p1", "project_stack": {"backend": "Spring Boot (java)"}})
    assert out["runtime_smoke_passed"] is None
    assert out["migration_verify_passed"] is None
    assert "runtime_smoke_skipped:env_missing" in out["degraded_reasons"]
    assert "migration_verify_skipped:smoke_skipped" in out["degraded_reasons"]


def test_node_not_executable_channel_degraded(wired):
    wired["derivation"] = _deriv("golang-migrate")
    out = _run_node({"project_id": "p1", "project_stack": {"backend": "Gin (go)"}})
    assert out["runtime_smoke_passed"] is True
    assert out["migration_verify_passed"] is None
    assert out["degraded_reasons"] == ["migration_verify_skipped:no_embedded_engine"]


def test_node_sandbox_unavailable_migration_follows(wired, monkeypatch):
    import swarm.brain.nodes as nodes_pkg
    monkeypatch.setattr(nodes_pkg, "_sandbox_available", lambda: False)
    wired["derivation"] = _deriv("flyway")
    out = _run_node({"project_id": "p1"})
    assert out["runtime_smoke_passed"] is None
    assert out["migration_verify_passed"] is None
    assert out["migration_verify_details"]["reason"] == "smoke_not_executed"
    assert "runtime_smoke_skipped:sandbox_unavailable" in out["degraded_reasons"]
    assert "migration_verify_skipped:smoke_not_executed" in out["degraded_reasons"]


# ── 审A 端到端：冒烟 inconclusive skipped 但日志含 FlywayException → runtime 失败通道 ──

def test_node_smoke_inconclusive_flyway_failure_blocks(wired):
    """审A 完整触发序列：冒烟没过（inconclusive skipped）但启动日志里的 migration 失败
    证据是确定性的 → harvest failed → 并入 runtime 失败通道 → gates 阻断 auto-accept。"""
    wired["derivation"] = _deriv("flyway")
    wired["smoke"] = _smoke("skipped", "inconclusive", log_tail=_FLYWAY_FAIL_LOG)
    out = _run_node({"project_id": "p1", "project_stack": {"backend": "Spring Boot (java)"}})
    assert out["runtime_smoke_passed"] is False
    assert out["verification_failure"] == "runtime_smoke"
    assert out["runtime_smoke_details"]["classification"] == "migration_failed"
    assert out["migration_verify_passed"] is False
    # F3：migration 证据以 migration 前缀键进 runtime_smoke_details（归因回灌可消费）
    assert "FlywayException" in out["runtime_smoke_details"].get("migration_output", "")
    # gates 阻断（审A 的"要端到端断言 gates 会阻断"）
    from swarm.brain.gates import can_auto_accept_delivery
    allow, reason = can_auto_accept_delivery({"l2_passed": True, **out})
    assert allow is False
    assert "runtime" in reason


# ── F3：直接执行通道失败的证据也要进 runtime_smoke_details（migration 前缀键契约）──

def test_node_migration_exec_failed_evidence_reaches_details(wired, monkeypatch):
    wired["derivation"] = _deriv("alembic")
    monkeypatch.setattr(
        mv, "detect_migration_channel",
        lambda kind, stack, path: MigrationChannel(
            True, reason="embedded_db_evidence", command="alembic upgrade head"))

    async def _fake_exec(manager, sandbox, command, **kw):
        return MigrationVerifyResult(
            "failed", "sql_error", "duplicate column",
            evidence={"ran": True, "exit_code": 1, "hits": ["duplicate column"],
                      "command": command,
                      "output_tail": "sqlite3.OperationalError: duplicate column name: age"})

    monkeypatch.setattr(mv, "execute_migration", _fake_exec)
    out = _run_node({"project_id": "p1"})
    d = out["runtime_smoke_details"]
    assert "duplicate column" in d["migration_output"]
    assert d["migration_hits"] == ["duplicate column"]
    assert d["migration_command"] == "alembic upgrade head"
    # shared.runtime_failure_evidence 的 startswith("migration") 契约真消费到 SQL 证据
    from swarm.brain.nodes.shared import runtime_failure_evidence
    blob = runtime_failure_evidence(d)
    assert "duplicate column name: age" in blob


# ── 审C：直接执行通道前给沙箱寿命续 migration 预算 ──────────────────────

def test_direct_exec_extends_lifetime_before_running(wired, monkeypatch):
    wired["derivation"] = _deriv("alembic")
    order: list = []
    manager = wired["manager"]

    def _extend(sandbox, seconds):
        order.append(("extend", int(seconds)))
        return True

    manager.try_extend_lifetime = _extend
    monkeypatch.setattr(
        mv, "detect_migration_channel",
        lambda kind, stack, path: MigrationChannel(True, command="alembic upgrade head"))

    async def _fake_exec(manager, sandbox, command, **kw):
        order.append(("exec", command))
        return MigrationVerifyResult("passed", "executed", "ok")

    monkeypatch.setattr(mv, "execute_migration", _fake_exec)
    out = _run_node({"project_id": "p1"})
    assert out["migration_verify_passed"] is True
    want = ("extend", mv.MIGRATION_EXEC_TIMEOUT_SEC + 60)
    assert want in order, f"直接执行前必须续期 migration 预算: {order}"
    assert order.index(want) < order.index(("exec", "alembic upgrade head"))


def test_direct_exec_extend_failure_does_not_block(wired, monkeypatch):
    """续期失败/异常不阻断 migration 执行（900s 默认寿命通常够），如实留痕。"""
    wired["derivation"] = _deriv("alembic")
    manager = wired["manager"]

    def _boom(sandbox, seconds):
        raise RuntimeError("set_timeout unsupported")

    manager.try_extend_lifetime = _boom
    monkeypatch.setattr(
        mv, "detect_migration_channel",
        lambda kind, stack, path: MigrationChannel(True, command="alembic upgrade head"))

    async def _fake_exec(manager, sandbox, command, **kw):
        return MigrationVerifyResult("passed", "executed", "ok")

    monkeypatch.setattr(mv, "execute_migration", _fake_exec)
    out = _run_node({"project_id": "p1"})
    assert out["migration_verify_passed"] is True  # 执行照常
    assert out["migration_verify_details"]["evidence"].get("lifetime_extended") is False
