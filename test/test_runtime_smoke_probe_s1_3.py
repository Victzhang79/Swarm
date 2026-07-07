#!/usr/bin/env python3
"""S1-3（task#17）：沙箱运行时冒烟探针执行器 —— 行为测试（禁 getsource）。

覆盖面（对齐 docs/RUNTIME_SMOKE_DESIGN.md §1.4/§2/§6 与主循环裁决）：
- 探针主形态 = 单次 run_command 内自包含 bash 脚本（起后台+轮询探活+收割日志+必杀进程组）；
- 三分类器只吃 (exit_code/进程存活, 日志文本, 探活序列) 通用三元组，栈词汇只在数据表；
- infra 失败≠冒烟失败（run_command 异常/标记缺失 → skipped 未执行，对齐 D31 ran/ok 先例）；
- 探活工具缺失（PROBE_TOOL_MISSING）→ skipped，环境缺失绝不伪装代码失败；
- 超时/无形态命中 → skipped+degraded，绝不默认判代码错；歧义双命中 → 保守 skipped。

测试全部走 stub 沙箱（fake run_command 预置输出），不触真沙箱/DB。
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.nodes.runtime_smoke import (  # noqa: E402
    DEFAULT_PREPARE_TIMEOUT_SEC,
    DEFAULT_SMOKE_TIMEOUT_SEC,
    RUN_TIMEOUT_BUFFER_SEC,
    build_project_symbols,
    build_smoke_script,
    classify_smoke_outcome,
    normalize_language_key,
    parse_smoke_markers,
    resolve_prepare_timeout_sec,
    resolve_smoke_timeout_sec,
    run_runtime_smoke,
)


# ───────────────────────── stub 沙箱 ─────────────────────────

class _StubSandbox:
    sandbox_id = "sbx-smoke-test"


class _StubManager:
    """fake run_command：预置 stdout/stderr/error 或抛异常，并记录调用参数。"""

    def __init__(self, stdout: str = "", stderr: str = "", error: str | None = None,
                 exc: Exception | None = None):
        self._stdout = stdout
        self._stderr = stderr
        self._error = error
        self._exc = exc
        self.calls: list[dict] = []

    def run_command(self, sandbox, command, timeout=120, _count_failures=True,
                    _skip_blacklist=False):
        self.calls.append({
            "command": command, "timeout": timeout,
            "_skip_blacklist": _skip_blacklist,
        })
        if self._exc is not None:
            raise self._exc
        return SimpleNamespace(
            stdout=self._stdout, stderr=self._stderr,
            error=self._error, success=self._error is None,
        )


def _smoke_output(probe=("refused", "refused"), app_rc="alive", log="",
                  tool="curl", done=True, tool_missing=False,
                  prepare_rc=None, port_busy=False) -> str:
    """按脚本标记口径伪造一份沙箱 stdout（F1 prepare_rc / F4 port_busy 可注入）。"""
    lines = []
    if tool_missing:
        lines += ["PROBE_TOOL_MISSING", "__SMOKE_PROBE_TOOL__MISSING"]
    else:
        lines.append(f"__SMOKE_PROBE_TOOL__{tool}")
    if port_busy:
        # F4 提前退出形态：PORT_BUSY + 空日志段 + DONE，不含 start/probe 阶段
        lines += ["__SMOKE_PORT_BUSY__", "__SMOKE_PHASE__collect",
                  "__SMOKE_LOG_TAIL_BEGIN__", "__SMOKE_LOG_TAIL_END__", "__SMOKE_DONE__"]
        return "\n".join(lines)
    if prepare_rc is not None:
        lines += ["__SMOKE_PHASE__prepare", f"__SMOKE_PREPARE_RC__{prepare_rc}"]
        if prepare_rc != 0:
            # F1 提前退出形态：prepare 日志尾走主日志标记段 + DONE，不起应用
            lines += ["__SMOKE_PHASE__collect", "__SMOKE_LOG_TAIL_BEGIN__"]
            if log:
                lines.append(log)
            lines += ["__SMOKE_LOG_TAIL_END__", "__SMOKE_DONE__"]
            return "\n".join(lines)
    lines.append("__SMOKE_PHASE__start")
    lines.append("__SMOKE_PHASE__probe")
    lines += [f"__SMOKE_PROBE__{p}" for p in probe]
    lines.append("__SMOKE_PHASE__collect")
    lines.append(f"__SMOKE_APP_RC__{app_rc}")
    lines.append("__SMOKE_LOG_TAIL_BEGIN__")
    if log:
        lines.append(log)
    lines.append("__SMOKE_LOG_TAIL_END__")
    if done:
        lines.append("__SMOKE_DONE__")
    return "\n".join(lines)


def _run(manager, *, language_key=None, script="<script>"):
    return asyncio.run(run_runtime_smoke(
        manager, _StubSandbox(), script, language_key=language_key))


# ───────────────────────── passed ─────────────────────────

def test_passed_when_probe_ok():
    mgr = _StubManager(stdout=_smoke_output(
        probe=("refused", "ok"), app_rc="alive",
        log="Started DemoApplication in 8.2 seconds"))
    res = _run(mgr, language_key="java")
    assert res.status == "passed"
    assert res.classification == "started"
    assert res.details.get("ran") is True


def test_passed_even_with_noisy_class1_log():
    """探活通过优先于日志形态——起来了就是起来了，噪声日志不翻案。"""
    mgr = _StubManager(stdout=_smoke_output(
        probe=("ok",), log="WARN ClassNotFoundException: optional.Thing ignored"))
    res = _run(mgr, language_key="java")
    assert res.status == "passed"


# ───────────────────────── 类1 代码错误 → failed ─────────────────────────

# F2 语义收紧适配说明：原三条 test_class1_*_failed 中的 import/模块缺失样例
# （ClassNotFoundException/Cannot find module/ModuleNotFoundError…）已按符号归属改判——
# 沙箱不保证装项目第三方运行时依赖，无 project_symbols 佐证时保守 dependency_missing。
# 原始意图（纯语法/类格式故障 = 确定性代码错 → failed 可回灌）由下列样例完整保留。

@pytest.mark.parametrize("log", [
    "java.lang.UnsupportedClassVersionError: com/acme/Main has been compiled by a more recent version",
    "java.lang.NoSuchMethodError: com.acme.Foo.bar()V",
])
def test_class1_java_failed(log):
    mgr = _StubManager(stdout=_smoke_output(probe=("exited",), app_rc="1", log=log))
    res = _run(mgr, language_key="java")
    assert res.status == "failed"
    assert res.classification == "code_error"
    assert res.log_tail  # log_tail 回传


def test_class1_node_syntax_failed():
    mgr = _StubManager(stdout=_smoke_output(
        probe=("exited",), app_rc="1", log="SyntaxError: Unexpected token '}'"))
    res = _run(mgr, language_key="node")
    assert res.status == "failed"
    assert res.classification == "code_error"


def test_class1_python_syntax_failed():
    log = "  File \"app.py\", line 12\n    def broken(\nSyntaxError: '(' was never closed"
    mgr = _StubManager(stdout=_smoke_output(probe=("exited",), app_rc="1", log=log))
    res = _run(mgr, language_key="python")
    assert res.status == "failed"
    assert res.classification == "code_error"


# ───────────── F2：import 缺失按符号归属裁决 ─────────────

@pytest.mark.parametrize("lang,log", [
    ("node", "Error: Cannot find module 'express'"),
    ("node", "Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'fastify'"),
    ("python", "ModuleNotFoundError: No module named 'flask_sqlalchemy'"),
    ("java", "Exception in thread \"main\" java.lang.ClassNotFoundException: com.mysql.cj.jdbc.Driver"),
])
def test_import_missing_third_party_is_dependency_missing(lang, log):
    """第三方符号缺失（或无 project_symbols 佐证）= 环境：沙箱不装运行时依赖是常态。"""
    mgr = _StubManager(stdout=_smoke_output(probe=("exited",), app_rc="1", log=log))
    res = _run(mgr, language_key=lang)
    assert res.status == "skipped", f"{lang}: {log}"
    assert res.classification == "dependency_missing"
    assert res.details.get("import_missing_hits")  # 符号留痕可观测


def test_import_missing_project_internal_python_is_code_error(tmp_path):
    """项目内模块缺失（tmp 树里真有同名 .py）→ 维持 code_error failed（可回灌）。"""
    (tmp_path / "app_core.py").write_text("x = 1\n", encoding="utf-8")
    symbols = build_project_symbols(str(tmp_path))
    res = classify_smoke_outcome(
        1, "ModuleNotFoundError: No module named 'app_core'", ["exited"],
        language_key="python", project_symbols=symbols)
    assert res.status == "failed"
    assert res.classification == "code_error"


def test_import_missing_java_fqn_internal_vs_external(tmp_path):
    src = tmp_path / "src" / "main" / "java" / "com" / "acme" / "util"
    src.mkdir(parents=True)
    (src / "Helper.java").write_text("package com.acme.util;\n", encoding="utf-8")
    symbols = build_project_symbols(str(tmp_path))
    # 项目内 FQN（路径化后前缀命中源码路径）→ failed
    res = classify_smoke_outcome(
        1, "java.lang.NoClassDefFoundError: com/acme/util/Helper", ["exited"],
        language_key="java", project_symbols=symbols)
    assert res.status == "failed" and res.classification == "code_error"
    # 第三方 FQN → skipped dependency_missing
    res2 = classify_smoke_outcome(
        1, "java.lang.ClassNotFoundException: org.postgresql.Driver", ["exited"],
        language_key="java", project_symbols=symbols)
    assert res2.status == "skipped" and res2.classification == "dependency_missing"


def test_import_missing_without_project_symbols_is_conservative():
    """无 project_symbols 传入 → 无法解析归属 → 保守 dependency_missing（不冤枉代码）。"""
    res = classify_smoke_outcome(
        1, "ModuleNotFoundError: No module named 'app_core'", ["exited"],
        language_key="python", project_symbols=None)
    assert res.status == "skipped"
    assert res.classification == "dependency_missing"


def test_import_missing_node_relative_path_is_code_error():
    """相对路径 import 缺失 = 项目自身文件缺失 → 代码错（不需要符号索引也能定内）。"""
    res = classify_smoke_outcome(
        1, "Error: Cannot find module './routes/user'", ["exited"],
        language_key="node", project_symbols={"paths": set(), "basenames": set(), "top": set()})
    assert res.status == "failed"
    assert res.classification == "code_error"


def test_class1_go_panic_failed():
    mgr = _StubManager(stdout=_smoke_output(
        probe=("exited",), app_rc="2",
        log="panic: runtime error: invalid memory address or nil pointer dereference"))
    res = _run(mgr, language_key="go")
    assert res.status == "failed"
    assert res.classification == "code_error"


def test_class1_rust_panic_failed():
    mgr = _StubManager(stdout=_smoke_output(
        probe=("exited",), app_rc="101",
        log="thread 'main' panicked at src/main.rs:10:5:\nindex out of bounds"))
    res = _run(mgr, language_key="rust")
    assert res.status == "failed"
    assert res.classification == "code_error"


# ───────────── F5：panic 形态子表（推翻 §6.3"panic 默认类1"赌注） ─────────────

def test_rust_unwrap_on_err_is_code_error():
    mgr = _StubManager(stdout=_smoke_output(
        probe=("exited",), app_rc="101",
        log="thread 'main' panicked: called `Result::unwrap()` on an `Err` value"))
    res = _run(mgr, language_key="rust")
    assert res.status == "failed"
    assert res.classification == "code_error"


@pytest.mark.parametrize("lang,log", [
    ("go", "panic: required key DATABASE_URL missing"),
    ("go", "panic: open /etc/app/config.yaml: no such file or directory"),
    ("rust", "thread 'main' panicked: missing DATABASE_URL environment variable"),
])
def test_panic_env_morphology_is_env_skipped(lang, log):
    """配置/文件缺失 panic 在无外部服务沙箱是高频环境形态 → 类2 skipped 不冤枉代码。"""
    mgr = _StubManager(stdout=_smoke_output(probe=("exited",), app_rc="2", log=log))
    res = _run(mgr, language_key=lang)
    assert res.status == "skipped", f"{lang}: {log}"
    assert res.classification == "env_missing"


@pytest.mark.parametrize("lang,log", [
    ("go", "panic: something domain-specific went wrong"),
    ("rust", "thread 'main' panicked at src/main.rs:3:5:\nboom"),
])
def test_bare_panic_without_subform_is_inconclusive(lang, log):
    """裸 panic 无子形态命中 → inconclusive skipped（绝不默认判代码错）。"""
    mgr = _StubManager(stdout=_smoke_output(probe=("exited",), app_rc="2", log=log))
    res = _run(mgr, language_key=lang)
    assert res.status == "skipped", f"{lang}: {log}"
    assert res.classification == "inconclusive"


# ───────────────── 类2 外部依赖缺失/环境 → skipped ─────────────────

@pytest.mark.parametrize("lang,log", [
    ("java", "Caused by: java.net.ConnectException: Connection refused\n"
             "  at com.zaxxer.hikari.pool.HikariPool.createPoolEntry"),
    ("java", "Unable to acquire JDBC Connection"),
    ("java", "Web server failed to start. Port 8080 was already in use."),
    ("node", "Error: connect ECONNREFUSED 10.60.0.5:5432"),
    ("node", "Error: listen EADDRINUSE: address already in use :::3000"),
    ("python", "sqlalchemy.exc.OperationalError: (psycopg2.OperationalError) could not connect"),
    ("go", "dial tcp 10.60.0.5:6379: connect: connection refused"),
    ("go", "listen tcp :8080: bind: address already in use"),
    ("rust", "Error: Connection refused (os error 111)"),
])
def test_class2_env_missing_skipped(lang, log):
    mgr = _StubManager(stdout=_smoke_output(probe=("refused", "timeout"), log=log))
    res = _run(mgr, language_key=lang)
    assert res.status == "skipped", f"{lang}: {log}"
    assert res.classification == "env_missing"


# ───────────── 类3 超时/无形态 → skipped + degraded ─────────────

def test_timeout_no_morphology_is_inconclusive_skipped():
    mgr = _StubManager(stdout=_smoke_output(
        probe=("refused", "refused", "timeout"), app_rc="alive",
        log="INFO warming up JIT..."))
    res = _run(mgr, language_key="java")
    assert res.status == "skipped"
    assert res.classification == "inconclusive"
    assert res.details.get("degraded") is True


def test_process_exit_without_any_pattern_is_not_code_error():
    """exit!=0 且无表命中 → 绝不默认判代码错（设计 §6.3 通用兜底）。"""
    mgr = _StubManager(stdout=_smoke_output(
        probe=("exited",), app_rc="137", log="Killed"))
    res = _run(mgr, language_key="java")
    assert res.status == "skipped"
    assert res.classification == "inconclusive"


# ───────────── 歧义双命中 → 保守 skipped ─────────────

def test_ambiguous_both_families_skipped():
    # F2 适配：原样例（ClassNotFoundException: com.mysql...Driver + Connection refused）
    # 中的 CNFE 已按符号归属改判为环境（第三方 Driver 缺失），不再构成"代码×环境"歧义
    # ——见 test_import_plus_env_double_hit_still_skipped。真歧义改用纯代码故障形态
    # （NoSuchMethodError）+ 环境形态双命中；原始意图不变：歧义绝不判 failed。
    log = ("java.lang.NoSuchMethodError: com.acme.Foo.bar()V\n"
           "Caused by: java.net.ConnectException: Connection refused")
    mgr = _StubManager(stdout=_smoke_output(probe=("exited",), app_rc="1", log=log))
    res = _run(mgr, language_key="java")
    assert res.status == "skipped"
    assert res.classification == "ambiguous"


def test_import_plus_env_double_hit_still_skipped():
    """旧歧义样例（第三方类缺失+连接拒绝）在 F2 下两个信号同向指环境 → skipped 不变，
    分类收敛到 env_missing（比 ambiguous 更准确，skip 方向安全）。"""
    log = ("java.lang.ClassNotFoundException: com.mysql.cj.jdbc.Driver\n"
           "Caused by: java.net.ConnectException: Connection refused")
    mgr = _StubManager(stdout=_smoke_output(probe=("exited",), app_rc="1", log=log))
    res = _run(mgr, language_key="java")
    assert res.status == "skipped"
    assert res.classification == "env_missing"


# ───────────── 探活工具缺失 → skipped ─────────────

def test_probe_tool_missing_skipped():
    mgr = _StubManager(stdout=_smoke_output(probe=(), tool_missing=True))
    res = _run(mgr)
    assert res.status == "skipped"
    assert res.classification == "probe_tool_missing"


# ───────────── infra 失败 ≠ 冒烟失败（D31 ran/ok 口径） ─────────────

def test_run_command_exception_is_not_executed():
    mgr = _StubManager(exc=RuntimeError("envd 502 Bad Gateway"))
    res = _run(mgr)
    assert res.status == "skipped"
    assert res.classification == "not_executed"
    assert res.details.get("ran") is False


def test_markers_missing_is_not_executed():
    mgr = _StubManager(stdout="bash: unexpected EOF", error="ConnectionError: envd")
    res = _run(mgr)
    assert res.status == "skipped"
    assert res.classification == "not_executed"


def test_manager_without_run_command_is_not_executed():
    res = asyncio.run(run_runtime_smoke(object(), _StubSandbox(), "<script>"))
    assert res.status == "skipped"
    assert res.classification == "not_executed"


# ───────────── 进程活+bind成功+TCP不通 → 沙箱网络异常 skipped ─────────────

def test_alive_bind_success_but_unreachable_is_network_anomaly():
    mgr = _StubManager(stdout=_smoke_output(
        probe=("refused", "refused", "timeout"), app_rc="alive",
        log="Tomcat started on port 8080 (http) with context path ''"))
    res = _run(mgr, language_key="java")
    assert res.status == "skipped"
    assert res.classification == "network_anomaly"


def test_network_anomaly_carries_port_mismatch_hint():
    """F6：疑似端口推导错配——message 带提示、details 带探测端口值；classification 不变。"""
    res = classify_smoke_outcome(
        "alive", "Tomcat started on port 9090 (http)", ["refused", "timeout"],
        language_key="java", probe_port=8080)
    assert res.status == "skipped"
    assert res.classification == "network_anomaly"  # skip 方向已安全，分类不动
    assert "端口推导错配" in res.message
    assert "8080" in res.message
    assert res.details.get("probe_port") == 8080


# ───────────── F4：探针假绿收紧 ─────────────

def test_port_busy_precheck_early_exit_is_skipped():
    """起应用前端口已有 listener → 脚本提前退出（不起应用），环境问题 skipped。"""
    mgr = _StubManager(stdout=_smoke_output(port_busy=True))
    res = _run(mgr, language_key="java")
    assert res.status == "skipped"
    assert res.classification == "port_busy"
    assert res.details.get("ran") is True


def test_probe_ok_but_process_exited_is_stale_listener_suspected():
    """探活曾 ok 但进程已退（app_rc=数字）→ 应答者身份存疑，绝不 passed 假绿。"""
    mgr = _StubManager(stdout=_smoke_output(probe=("ok",), app_rc="1", log="died late"))
    res = _run(mgr, language_key="java")
    assert res.status == "skipped"
    assert res.classification == "stale_listener_suspected"


def test_script_prechecks_port_before_start():
    """脚本必须在起应用【之前】做端口预检（PORT_BUSY 标记先于 start 阶段）。"""
    script = build_smoke_script("run-app", 8080)
    assert "__SMOKE_PORT_BUSY__" in script
    assert script.index("__SMOKE_PORT_BUSY__") < script.index("__SMOKE_PHASE__start")


# ───────────── F1：prepare（构建产物）阶段 ─────────────

def test_script_with_prepare_contains_markers_and_runs_before_start():
    script = build_smoke_script(
        "java -jar target/*.jar", 8080, prepare_cmd="mvn -q -DskipTests package")
    assert "__SMOKE_PREPARE_RC__" in script
    assert "mvn -q -DskipTests package" in script
    # prepare 先于起应用；且失败提前退出路径内收尾标记完整（LOG 段 + DONE）
    assert script.index("__SMOKE_PREPARE_RC__") < script.index("__SMOKE_PHASE__start")
    prepare_fail_block = script[script.index("__SMOKE_PREPARE_RC__"):
                                script.index("__SMOKE_PHASE__start")]
    for mark in ("__SMOKE_LOG_TAIL_BEGIN__", "__SMOKE_LOG_TAIL_END__", "__SMOKE_DONE__"):
        assert mark in prepare_fail_block, f"prepare 失败提前退出缺收尾标记 {mark}"


def test_script_without_prepare_has_no_prepare_marker():
    script = build_smoke_script("npm start", 3000)
    assert "__SMOKE_PREPARE_RC__" not in script


def test_executor_prepare_failed_is_skipped_with_log_tail():
    """prepare 非 0 → skipped prepare_failed（L2 已证编译过，环境不冤枉代码）+ 日志尾留痕。"""
    mgr = _StubManager(stdout=_smoke_output(
        prepare_rc=1, log="[ERROR] Failed to execute goal maven-jar-plugin"))
    res = _run(mgr, language_key="java")
    assert res.status == "skipped"
    assert res.classification == "prepare_failed"
    assert res.details.get("prepare_rc") == 1
    assert "maven-jar-plugin" in res.details.get("prepare_log_tail", "")


def test_executor_prepare_ok_flows_into_classification():
    """prepare rc=0 → 正常起应用与三分类，prepare_rc 留痕不干扰结论。"""
    mgr = _StubManager(stdout=_smoke_output(prepare_rc=0, probe=("ok",), app_rc="alive"))
    res = _run(mgr, language_key="java")
    assert res.status == "passed"
    assert res.details.get("prepare_rc") == 0


def test_executor_timeout_includes_prepare_budget():
    mgr = _StubManager(stdout=_smoke_output(probe=("ok",)))
    asyncio.run(run_runtime_smoke(
        mgr, _StubSandbox(), "<script>", timeout_sec=60, prepare_timeout_sec=600))
    assert mgr.calls[0]["timeout"] == 60 + RUN_TIMEOUT_BUFFER_SEC + 600


def test_prepare_timeout_env_invalid_falls_back_default(monkeypatch):
    monkeypatch.setenv("SWARM_RUNTIME_SMOKE_PREPARE_TIMEOUT_SEC", "abc")
    assert resolve_prepare_timeout_sec() == DEFAULT_PREPARE_TIMEOUT_SEC
    monkeypatch.setenv("SWARM_RUNTIME_SMOKE_PREPARE_TIMEOUT_SEC", "-1")
    assert resolve_prepare_timeout_sec() == DEFAULT_PREPARE_TIMEOUT_SEC
    monkeypatch.setenv("SWARM_RUNTIME_SMOKE_PREPARE_TIMEOUT_SEC", "900")
    assert resolve_prepare_timeout_sec() == 900
    monkeypatch.delenv("SWARM_RUNTIME_SMOKE_PREPARE_TIMEOUT_SEC", raising=False)
    assert resolve_prepare_timeout_sec() == DEFAULT_PREPARE_TIMEOUT_SEC


# ───────────── 执行器调用契约 ─────────────

def test_executor_calls_run_command_with_buffer_and_skip_blacklist():
    mgr = _StubManager(stdout=_smoke_output(probe=("ok",)))
    res = asyncio.run(run_runtime_smoke(
        mgr, _StubSandbox(), "<script>", timeout_sec=60))
    assert res.status == "passed"
    call = mgr.calls[0]
    assert call["timeout"] == 60 + RUN_TIMEOUT_BUFFER_SEC
    assert call["_skip_blacklist"] is True
    assert call["command"] == "<script>"


# ───────────── env 超时解析 ─────────────

def test_timeout_env_invalid_falls_back_default(monkeypatch):
    monkeypatch.setenv("SWARM_RUNTIME_SMOKE_TIMEOUT_SEC", "abc")
    assert resolve_smoke_timeout_sec() == DEFAULT_SMOKE_TIMEOUT_SEC
    monkeypatch.setenv("SWARM_RUNTIME_SMOKE_TIMEOUT_SEC", "-5")
    assert resolve_smoke_timeout_sec() == DEFAULT_SMOKE_TIMEOUT_SEC
    monkeypatch.setenv("SWARM_RUNTIME_SMOKE_TIMEOUT_SEC", "0")
    assert resolve_smoke_timeout_sec() == DEFAULT_SMOKE_TIMEOUT_SEC


def test_timeout_env_valid_value(monkeypatch):
    monkeypatch.setenv("SWARM_RUNTIME_SMOKE_TIMEOUT_SEC", "240")
    assert resolve_smoke_timeout_sec() == 240
    monkeypatch.delenv("SWARM_RUNTIME_SMOKE_TIMEOUT_SEC", raising=False)
    assert resolve_smoke_timeout_sec() == DEFAULT_SMOKE_TIMEOUT_SEC


# ───────────── 脚本生成器（对产物文本的行为断言，允许） ─────────────

def test_script_contains_cleanup_and_all_markers():
    script = build_smoke_script("mvn spring-boot:run", 8080,
                                health_path="/actuator/health", timeout_sec=120)
    # 进程清理：trap + 进程组 kill + pkill 兜底
    assert "trap" in script
    assert 'kill -- -"$SMOKE_PID"' in script
    assert 'pkill -P "$SMOKE_PID"' in script
    # 结构化标记完整性
    for mark in ("__SMOKE_PHASE__", "__SMOKE_PROBE_TOOL__", "__SMOKE_PROBE__",
                 "__SMOKE_APP_RC__", "__SMOKE_LOG_TAIL_BEGIN__",
                 "__SMOKE_LOG_TAIL_END__", "__SMOKE_DONE__", "PROBE_TOOL_MISSING"):
        assert mark in script, f"missing marker {mark}"
    # 探活工具自适应链：curl → /dev/tcp → python3 → MISSING
    assert "command -v curl" in script
    assert "/dev/tcp/127.0.0.1" in script
    assert "command -v python3" in script
    # 参数落位
    assert "8080" in script
    assert "/actuator/health" in script
    assert "mvn spring-boot:run" in script
    assert "SMOKE_TIMEOUT=120" in script
    # 日志尾部限行
    assert "tail -n 200" in script


def test_script_timeout_from_env_with_invalid_fallback(monkeypatch):
    monkeypatch.setenv("SWARM_RUNTIME_SMOKE_TIMEOUT_SEC", "not-a-number")
    script = build_smoke_script("npm start", 3000)
    assert f"SMOKE_TIMEOUT={DEFAULT_SMOKE_TIMEOUT_SEC}" in script


def test_script_quotes_start_cmd_safely():
    script = build_smoke_script("python3 app.py --name \"o'brien\"", 5000)
    # shlex 安全引用后原样可见（不被 bash 展开撕裂）
    assert "app.py" in script
    assert "SMOKE_START_CMD=" in script


# ───────────── 语言键归一（通用多栈，仅数据表含栈词汇） ─────────────

@pytest.mark.parametrize("backend,expected", [
    ("Spring Boot (java)", "java"),
    ("Django (python)", "python"),          # 'django' 不得误配 'go'
    ("Gin (go)", "go"),
    ("Express (javascript/typescript)", "node"),
    ("rust", "rust"),
    ("kotlin", "java"),                      # JVM 族类加载形态同源
    ("未判明", None),
    ("", None),
    (None, None),
])
def test_normalize_language_key(backend, expected):
    assert normalize_language_key(backend) == expected


# ───────────── 标记解析器 ─────────────

def test_parse_markers_roundtrip():
    out = _smoke_output(probe=("refused", "ok"), app_rc="alive",
                        log="line1\nline2", tool="devtcp")
    parsed = parse_smoke_markers(out)
    assert parsed["probe_sequence"] == ["refused", "ok"]
    assert parsed["probe_tool"] == "devtcp"
    assert parsed["app_rc"] == "alive"
    assert parsed["done"] is True
    assert "line1" in parsed["log_tail"] and "line2" in parsed["log_tail"]


def test_parse_markers_numeric_rc():
    out = _smoke_output(probe=("exited",), app_rc="137")
    assert parse_smoke_markers(out)["app_rc"] == 137


# ───────────── 分类器纯函数直测（不经执行器） ─────────────

def test_classifier_no_language_key_uses_generic_only():
    """未知语言：只有 generic env 表兜底；无 generic 代码错表 → 不误杀。"""
    res = classify_smoke_outcome("1", "some unknown crash text", ["exited"],
                                 language_key=None)
    assert res.status == "skipped"
    assert res.classification == "inconclusive"
    res2 = classify_smoke_outcome("1", "Connection refused", ["exited"],
                                  language_key=None)
    assert res2.status == "skipped"
    assert res2.classification == "env_missing"
