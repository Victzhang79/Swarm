"""S2-5（task#26）：ACCEPT 验收断言接线 — 行为测试（禁 getsource 结构焊死）。

覆盖面：
1. build_smoke_script：assert_cmds 缺省行为逐字节不变；注入位置=探活 ok 后（SMOKE_OK
   守卫）、收割前；curl 缺失如实输出 MARK_ACCEPT_TOOL_MISSING。
2. 执行器透传：__ACCEPT_ 标记行原样进 details.accept_output（不解析）；accept 预算入
   run_command timeout。
3. 断言生成（verify_runtime 内，LLM stub）：合法/非法混合→校验剔除+degraded 留痕；
   requirement_items 空→跳过生成不调 LLM；LLM 持续失败→有界重试后降级 []+degraded；
   已有 assertions→复用不重烧 LLM。
4. accept phase：全 pass→True；一条 fail→False+并入 runtime 失败通道
   （classification=acceptance_failed+acceptance 前缀证据键）；全 manual→None+degraded+
   manual 清单；manual 绝不进脚本；冒烟 failed/skipped→断言不执行跟随 skip；
   assert 工具缺失/标记缺失→skipped 绝不假绿也不冤枉。
5. elaborate covers 继承：确定性按文件拆分与 LLM resplit 两条路径都不丢父 covers。
"""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest
import swarm.brain.nodes.verify as verify_mod
from swarm.brain.acceptance_spec import (
    DEFAULT_PROBE_MAX_TIME_SEC,
    MARK_ACCEPT_BODY,
    MARK_ACCEPT_RESULT,
)
from swarm.brain.nodes.runtime_smoke import (
    MARK_ACCEPT_TOOL_MISSING,
    RUN_TIMEOUT_BUFFER_SEC,
    RuntimeSmokeResult,
    build_smoke_script,
    resolve_smoke_timeout_sec,
    run_runtime_smoke,
)
from swarm.brain.smoke_derive import SmokeDerivation

# ───────────────────────── 工具 ─────────────────────────

_ITEM = {"id": "req-11111111", "text": "系统提供健康检查接口", "kind": "api",
         "source_quote": "健康检查", "source": "description"}

# F7 语义适配：verify 侧 validate_assertions 现在带 grounding（evidence 必须回指
# 生成语料——wired 夹具的语料=运行时推导证据行 "port=8080, health_path=/health"）。
# 夹具 spec 补 evidence 使其保持原意图（合法可执行断言），不改各测试的判定语义。
_GROUNDED_EVIDENCE = "port=8080"

_VALID_SPEC = {"id": "a1", "req_id": "req-11111111", "kind": "http_probe",
               "request": {"method": "GET", "path": "/api/ping"},
               "expect": {"status": [200]}, "auth": "none",
               "evidence": _GROUNDED_EVIDENCE}

_VALID_SPEC_2 = {"id": "a2", "req_id": "req-11111111", "kind": "http_probe",
                 "request": {"method": "GET", "path": "/api/status"},
                 "expect": {"status": [200, 204]}, "auth": "none",
                 "evidence": _GROUNDED_EVIDENCE}

_MANUAL_SPEC = {"id": "m1", "req_id": "req-11111111", "kind": "manual", "auth": "manual"}

_AUTH_MANUAL_SPEC = {"id": "a9", "req_id": "req-11111111", "kind": "http_probe",
                     "request": {"method": "GET", "path": "/api/secret"},
                     "expect": {"status": [200]}, "auth": "manual",
                     "evidence": _GROUNDED_EVIDENCE}

_INVALID_SPEC = {"id": "bad1", "req_id": "req-11111111", "kind": "http_probe",
                 "request": {"method": "GET", "path": "http://evil.example/x"},
                 "expect": {"status": [200]}, "auth": "none"}


def _accept_out(*probes: tuple[str, int, str]) -> str:
    """(id, http_code, body) → 脚本会输出的原始 __ACCEPT_* 标记行。"""
    lines: list[str] = []
    for sid, code, body in probes:
        lines.append(f"{MARK_ACCEPT_RESULT}{sid}__{code}")
        b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")
        lines.append(f"{MARK_ACCEPT_BODY}{sid}__{b64}")
    return "\n".join(lines)


def _json(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


class _FakeLLM:
    """LLM stub：payload 列表按序返回（Exception 项=该次调用抛异常），末项循环。"""

    def __init__(self, payloads: list):
        self.payloads = list(payloads)
        self.calls = 0

    async def ainvoke(self, messages):
        idx = min(self.calls, len(self.payloads) - 1)
        self.calls += 1
        p = self.payloads[idx]
        if isinstance(p, Exception):
            raise p
        return SimpleNamespace(content=p)


class _StubResult:
    def __init__(self, stdout: str = "", stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr


class _StubSandbox:
    def __init__(self, sid: str):
        self.sandbox_id = sid


class _StubManager:
    """最小沙箱经理 stub（与 S1-4/S1-5 同款）：记账 create/kill/extend。"""

    def __init__(self, *, instances=None, extend_ok=True, remaining=None,
                 rebuild_stdout="__RC__0"):
        self._instances = dict(instances or {})
        self.extend_ok = extend_ok
        self.remaining = remaining
        self.rebuild_stdout = rebuild_stdout
        self.killed: list[str] = []
        self.created: list[str] = []
        self.extend_calls: list[tuple[str, int]] = []
        self.synced = False

    def try_extend_lifetime(self, sandbox, seconds):
        self.extend_calls.append((getattr(sandbox, "sandbox_id", "?"), int(seconds)))
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


def _deriv() -> SmokeDerivation:
    return SmokeDerivation(start_cmd="run-the-app", port=8080, health_path="/health",
                           migration_kind=None, evidence={"start_cmd": "manifest 证据"})


def _smoke(status: str, classification: str, *,
           accept_output: str | None = None, log_tail: str = "") -> RuntimeSmokeResult:
    details: dict = {"probe_sequence": []}
    if accept_output is not None:
        details["accept_output"] = accept_output
    return RuntimeSmokeResult(status, classification, f"stub-{status}",
                              log_tail=log_tail, details=details)


@pytest.fixture()
def wired(monkeypatch):
    """通用接线（S1-5 同款）+ 捕获冒烟脚本原文（manual 绝不进脚本的判据）。"""
    ctx = {
        "manager": _StubManager(),
        "derivation": _deriv(),
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
        ctx["smoke_calls"].append({"script": script, "kwargs": kwargs})
        return ctx["smoke"]

    monkeypatch.setattr(rs, "run_runtime_smoke", _fake_run_smoke)
    return ctx


def _patch_llm(monkeypatch, payloads: list) -> _FakeLLM:
    import swarm.brain.nodes as nodes_pkg
    fake = _FakeLLM(payloads)
    monkeypatch.setattr(nodes_pkg, "_get_brain_llm", lambda: fake)
    return fake


def _run_node(state: dict) -> dict:
    return asyncio.run(verify_mod.verify_runtime(state))


# ───────────────────────── build_smoke_script：脚本层 ─────────────────────────

def test_script_default_byte_identical_without_assert_cmds():
    """assert_cmds 缺省/None/[] 三种调用产出逐字节一致（既有 s1_3 行为零变化）。"""
    base = build_smoke_script("run-app", 8080, "/health")
    assert build_smoke_script("run-app", 8080, "/health", assert_cmds=None) == base
    assert build_smoke_script("run-app", 8080, "/health", assert_cmds=[]) == base
    assert "__ACCEPT_" not in base  # 无断言时脚本不含任何 accept 面


def test_script_assert_block_after_probe_ok_before_collect():
    script = build_smoke_script(
        "run-app", 8080, "/health", assert_cmds=["echo CMD_ONE", "echo CMD_TWO"])
    assert "echo CMD_ONE" in script and "echo CMD_TWO" in script
    idx_probe = script.index("__SMOKE_PHASE__probe")
    idx_cmd = script.index("echo CMD_ONE")
    # 注意 F4 端口预检早退块里也有一个 collect 标记——主流程 collect 是最后一个
    idx_collect = script.rindex("__SMOKE_PHASE__collect")
    assert idx_probe < idx_cmd < idx_collect, "assert 段必须在探活之后、收割之前"
    # 探活 ok 守卫：断言只在 SMOKE_OK=1 才执行（探活未 ok 整段跳过）
    guard = script.index('if [ "$SMOKE_OK" = "1" ]; then')
    assert guard < idx_cmd
    # curl 缺失如实输出工具缺失标记（环境缺失绝不伪装断言失败）
    assert MARK_ACCEPT_TOOL_MISSING in script
    # 必杀 trap 语义零改动
    assert "trap smoke_cleanup EXIT INT TERM" in script


# ───────────────────────── 执行器：透传 + 预算 ─────────────────────────

_PASSED_STDOUT = "\n".join([
    "__SMOKE_PROBE_TOOL__curl",
    "__SMOKE_PHASE__probe",
    "__SMOKE_PROBE__ok",
    "__SMOKE_PHASE__accept",
    f"{MARK_ACCEPT_RESULT}a1__200",
    f"{MARK_ACCEPT_BODY}a1__{base64.b64encode(b'pong').decode()}",
    "__SMOKE_PHASE__collect",
    "__SMOKE_APP_RC__alive",
    "__SMOKE_LOG_TAIL_BEGIN__",
    "__SMOKE_LOG_TAIL_END__",
    "__SMOKE_DONE__",
])


class _ExecManager:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.timeouts: list[int] = []

    def run_command(self, sandbox, command, timeout=120, **kwargs):
        self.timeouts.append(timeout)
        return _StubResult(stdout=self.stdout)


def test_executor_passes_accept_marker_lines_through_raw():
    mgr = _ExecManager(_PASSED_STDOUT)
    res = asyncio.run(run_runtime_smoke(mgr, object(), "script", timeout_sec=5))
    assert res.status == "passed"
    accept_output = res.details["accept_output"]
    # 原样透传：标记行逐字保留（解析是 phase 侧 parse_probe_output 的事）
    assert f"{MARK_ACCEPT_RESULT}a1__200" in accept_output
    assert base64.b64encode(b"pong").decode() in accept_output
    # 非标记行不混入
    assert "__SMOKE_DONE__" not in accept_output


def test_executor_no_accept_output_key_when_no_markers():
    stdout = "\n".join(
        ln for ln in _PASSED_STDOUT.splitlines() if "__ACCEPT_" not in ln)
    mgr = _ExecManager(stdout)
    res = asyncio.run(run_runtime_smoke(mgr, object(), "script", timeout_sec=5))
    assert "accept_output" not in res.details  # 缺省行为不变：无标记不加键


def test_executor_accept_budget_included_in_run_timeout():
    mgr = _ExecManager(_PASSED_STDOUT)
    asyncio.run(run_runtime_smoke(mgr, object(), "script", timeout_sec=5,
                                  accept_budget_sec=33))
    assert mgr.timeouts[0] == 5 + RUN_TIMEOUT_BUFFER_SEC + 33
    mgr2 = _ExecManager(_PASSED_STDOUT)
    asyncio.run(run_runtime_smoke(mgr2, object(), "script", timeout_sec=5))
    assert mgr2.timeouts[0] == 5 + RUN_TIMEOUT_BUFFER_SEC  # 缺省行为不变


# ───────────────────────── 断言生成（verify_runtime 内） ─────────────────────────

def test_generation_valid_and_invalid_mixed_rejects_invalid(wired, monkeypatch):
    fake = _patch_llm(monkeypatch, [_json([_VALID_SPEC, _INVALID_SPEC])])
    wired["smoke"] = _smoke("passed", "started", accept_output=_accept_out(("a1", 200, "pong")))
    out = _run_node({"project_id": "p1", "requirement_items": [_ITEM]})
    assert fake.calls == 1
    specs = out["acceptance_assertions"]
    assert [s["id"] for s in specs] == ["a1"]  # 非法条目被确定性校验剔除
    assert any(r.startswith("acceptance_generation:rejected=1")
               for r in out["degraded_reasons"])  # 被拒不静默丢
    assert out["acceptance_passed"] is True


def test_generation_skipped_when_no_requirement_items(wired, monkeypatch):
    fake = _patch_llm(monkeypatch, [_json([_VALID_SPEC])])
    out = _run_node({"project_id": "p1"})
    assert fake.calls == 0  # items 空绝不烧 LLM
    assert out["acceptance_assertions"] == []
    assert out["acceptance_passed"] is None
    assert out["acceptance_details"]["reason"] == "no_requirement_items"
    assert "degraded_reasons" not in out  # 无需求条目是常态（老任务/抽取降级已各自留痕）
    assert out["runtime_smoke_passed"] is True


def test_generation_llm_failure_degrades_to_empty(wired, monkeypatch):
    fake = _patch_llm(monkeypatch, [RuntimeError("llm down")])
    out = _run_node({"project_id": "p1", "requirement_items": [_ITEM]})
    assert fake.calls == 3  # 有界重试：首发 + 2 次，绝不无界
    assert out["acceptance_assertions"] == []
    assert out["acceptance_passed"] is None
    assert any(r.startswith("acceptance_generation:empty(llm_failed")
               for r in out["degraded_reasons"])
    assert out["runtime_smoke_passed"] is True  # 生成失败绝不阻塞冒烟结论


def test_generation_reuses_existing_assertions(wired, monkeypatch):
    fake = _patch_llm(monkeypatch, [_json([_VALID_SPEC_2])])
    wired["smoke"] = _smoke("passed", "started", accept_output=_accept_out(("a1", 200, "ok")))
    out = _run_node({"project_id": "p1", "requirement_items": [_ITEM],
                     "acceptance_assertions": [dict(_VALID_SPEC)]})
    assert fake.calls == 0  # replan 重入/resume 复用，不重烧 LLM
    assert out["acceptance_passed"] is True
    assert "/api/ping" in wired["smoke_calls"][0]["script"]


# ───────────────────────── manual 边界 ─────────────────────────

def test_manual_assertions_never_enter_script(wired, monkeypatch):
    _patch_llm(monkeypatch, [_json([_VALID_SPEC, _MANUAL_SPEC, _AUTH_MANUAL_SPEC])])
    wired["smoke"] = _smoke("passed", "started", accept_output=_accept_out(("a1", 200, "ok")))
    out = _run_node({"project_id": "p1", "requirement_items": [_ITEM]})
    script = wired["smoke_calls"][0]["script"]
    assert "/api/ping" in script          # 可执行断言进脚本
    assert "/api/secret" not in script    # auth=manual 绝不生成执行片段
    assert "m1" not in script             # kind=manual 绝不进脚本
    d = out["acceptance_details"]
    assert d["manual_count"] == 2
    manual_rows = [r for r in d["assertions"] if r["verdict"] == "skipped_manual"]
    assert {r["id"] for r in manual_rows} == {"m1", "a9"}
    assert out["acceptance_passed"] is True  # 可执行的 a1 全 pass，manual 不阻塞


def test_all_manual_is_skipped_with_manual_list(wired, monkeypatch):
    _patch_llm(monkeypatch, [_json([_MANUAL_SPEC, _AUTH_MANUAL_SPEC])])
    out = _run_node({"project_id": "p1", "requirement_items": [_ITEM]})
    script = wired["smoke_calls"][0]["script"]
    assert "__ACCEPT_" not in script  # 零可执行断言 → 脚本无 accept 面（缺省形态）
    assert out["acceptance_passed"] is None
    assert out["acceptance_details"]["reason"] == "all_manual"
    assert out["acceptance_details"]["manual_count"] == 2
    assert "acceptance_skipped:all_manual" in out["degraded_reasons"]
    assert "verification_failure" not in out


# ───────────────────────── accept phase 判定 ─────────────────────────

def test_all_pass_sets_true(wired, monkeypatch):
    _patch_llm(monkeypatch, [_json([_VALID_SPEC, _VALID_SPEC_2])])
    wired["smoke"] = _smoke("passed", "started",
                            accept_output=_accept_out(("a1", 200, "pong"), ("a2", 204, "")))
    out = _run_node({"project_id": "p1", "requirement_items": [_ITEM]})
    assert out["acceptance_passed"] is True
    assert out["runtime_smoke_passed"] is True
    assert "verification_failure" not in out
    d = out["acceptance_details"]
    assert d["reason"] == "all_passed" and d["failed_count"] == 0
    # 断言片段真进了脚本（两条路径都在）
    script = wired["smoke_calls"][0]["script"]
    assert "/api/ping" in script and "/api/status" in script


def test_one_fail_folds_into_runtime_failure_channel(wired, monkeypatch):
    _patch_llm(monkeypatch, [_json([_VALID_SPEC, _VALID_SPEC_2])])
    wired["smoke"] = _smoke(
        "passed", "started",
        accept_output=_accept_out(("a1", 404, "Not Found"), ("a2", 200, "ok")))
    out = _run_node({"project_id": "p1", "requirement_items": [_ITEM]})
    assert out["acceptance_passed"] is False
    assert out["runtime_smoke_passed"] is False
    assert out["verification_failure"] == "runtime_smoke"  # 并入 runtime 失败通道
    d = out["runtime_smoke_details"]
    assert d["classification"] == "acceptance_failed"      # 专类归因（8bec098 教训：绝不误标）
    assert d["smoke_status"] == "passed"                   # 冒烟自身结论保留供审计
    # acceptance 前缀证据键（task#27 归因回灌数据源，migration F3 同构）
    assert "/api/ping" in d["acceptance_evidence"]
    assert "404" in d["acceptance_evidence"]
    assert d["acceptance_failed_count"] == 1
    # 逐条 verdict + 请求/响应证据
    rows = out["acceptance_details"]["assertions"]
    fail = next(r for r in rows if r["verdict"] == "fail")
    assert fail["id"] == "a1" and fail["http_code"] == 404
    assert fail["body_excerpt"] == "Not Found"
    assert next(r for r in rows if r["id"] == "a2")["verdict"] == "pass"


def test_smoke_failed_assertions_follow_skip(wired, monkeypatch):
    fake = _patch_llm(monkeypatch, [_json([_VALID_SPEC])])
    wired["smoke"] = _smoke("failed", "code_error", log_tail="TRACE")
    out = _run_node({"project_id": "p1", "requirement_items": [_ITEM]})
    assert fake.calls == 1  # 生成先于冒烟（断言要进脚本），但判定跟随 skip
    assert out["runtime_smoke_passed"] is False
    assert out["verification_failure"] == "runtime_smoke"
    assert out["runtime_smoke_details"]["classification"] == "code_error"  # 不被 accept 覆盖
    assert out["acceptance_passed"] is None
    assert out["acceptance_details"]["reason"] == "smoke_failed"
    assert "acceptance_skipped:smoke_failed" in out["degraded_reasons"]


def test_smoke_skipped_assertions_follow_skip(wired, monkeypatch):
    _patch_llm(monkeypatch, [_json([_VALID_SPEC])])
    wired["smoke"] = _smoke("skipped", "env_missing")
    out = _run_node({"project_id": "p1", "requirement_items": [_ITEM]})
    assert out["runtime_smoke_passed"] is None
    assert out["acceptance_passed"] is None
    assert out["acceptance_details"]["reason"] == "smoke_skipped"
    assert "runtime_smoke_skipped:env_missing" in out["degraded_reasons"]
    assert "acceptance_skipped:smoke_skipped" in out["degraded_reasons"]


def test_sandbox_unavailable_assertions_follow_skip(wired, monkeypatch):
    import swarm.brain.nodes as nodes_pkg
    _patch_llm(monkeypatch, [_json([_VALID_SPEC])])
    monkeypatch.setattr(nodes_pkg, "_sandbox_available", lambda: False)
    out = _run_node({"project_id": "p1", "requirement_items": [_ITEM]})
    assert out["acceptance_passed"] is None
    assert out["acceptance_details"]["reason"] == "smoke_not_executed"
    assert "acceptance_skipped:smoke_not_executed" in out["degraded_reasons"]


def test_assert_tool_missing_is_skipped_not_failed(wired, monkeypatch):
    _patch_llm(monkeypatch, [_json([_VALID_SPEC])])
    wired["smoke"] = _smoke("passed", "started", accept_output=MARK_ACCEPT_TOOL_MISSING)
    out = _run_node({"project_id": "p1", "requirement_items": [_ITEM]})
    assert out["acceptance_passed"] is None  # 环境缺失绝不伪装断言失败
    assert out["acceptance_details"]["reason"] == "assert_tool_missing"
    assert "acceptance_skipped:assert_tool_missing" in out["degraded_reasons"]
    assert out["runtime_smoke_passed"] is True
    assert "verification_failure" not in out


def test_markers_missing_is_skipped_not_failed(wired, monkeypatch):
    _patch_llm(monkeypatch, [_json([_VALID_SPEC])])
    wired["smoke"] = _smoke("passed", "started")  # 冒烟过了但无任何 accept 输出（infra）
    out = _run_node({"project_id": "p1", "requirement_items": [_ITEM]})
    assert out["acceptance_passed"] is None  # 不能担保 True，也不冤枉成 False
    assert out["acceptance_details"]["reason"] == "markers_missing"
    assert "acceptance_skipped:markers_missing" in out["degraded_reasons"]
    assert "verification_failure" not in out


def test_disabled_switch_early_exit_emits_acceptance_none(wired, monkeypatch):
    monkeypatch.setenv("SWARM_RUNTIME_SMOKE_ENABLED", "0")
    out = _run_node({"project_id": "p1", "requirement_items": [_ITEM]})
    # 早退路径 always-emit：不留上一轮粘滞值
    assert out["acceptance_passed"] is None
    assert out["acceptance_details"]["reason"] == "smoke_not_executed"


def test_budget_includes_accept_when_assertions_present(wired, monkeypatch):
    _patch_llm(monkeypatch, [_json([_VALID_SPEC])])
    sb = _StubSandbox("sb-l2")
    wired["manager"] = _StubManager(instances={"sb-l2": sb})
    wired["smoke"] = _smoke("passed", "started", accept_output=_accept_out(("a1", 200, "ok")))
    out = _run_node({"project_id": "p1", "requirement_items": [_ITEM],
                     "runtime_smoke_sandbox_id": "sb-l2"})
    base = resolve_smoke_timeout_sec() + RUN_TIMEOUT_BUFFER_SEC + 120
    per = DEFAULT_PROBE_MAX_TIME_SEC + verify_mod.ACCEPT_PER_ASSERT_BUFFER_SEC
    assert wired["manager"].extend_calls[0][1] == base + 1 * per  # 沙箱寿命预算含断言项
    assert wired["smoke_calls"][0]["kwargs"]["accept_budget_sec"] == 1 * per  # 执行器透传
    assert out["acceptance_passed"] is True


# ───────────────── S2 复核新增：F6 inconclusive / F7 grounding / 审MED 双失败 ─────────────────

def test_inconclusive_probe_never_counts_as_fail(wired, monkeypatch):
    """F6：000/连接失败=infra 不确定 → 不进 fail_count；全 pass+1 条 000 → None+degraded。"""
    _patch_llm(monkeypatch, [_json([_VALID_SPEC, _VALID_SPEC_2])])
    wired["smoke"] = _smoke("passed", "started",
                            accept_output=_accept_out(("a1", 200, "ok"), ("a2", 0, "")))
    out = _run_node({"project_id": "p1", "requirement_items": [_ITEM]})
    assert out["acceptance_passed"] is None, "诚实不确定，绝不冤枉成 False"
    assert out["runtime_smoke_passed"] is True
    assert "verification_failure" not in out
    d = out["acceptance_details"]
    assert d["reason"] == "inconclusive"
    assert d["failed_count"] == 0 and d["inconclusive_count"] == 1
    verdicts = {r["id"]: r["verdict"] for r in d["assertions"]}
    assert verdicts["a1"] == "pass" and verdicts["a2"] == "inconclusive"
    assert "acceptance_skipped:inconclusive=1" in out["degraded_reasons"]


def test_conclusive_fail_beats_inconclusive(wired, monkeypatch):
    """F6 回归：真 fail（拿到真实应答且不符期待）仍 False，inconclusive 不稀释。"""
    _patch_llm(monkeypatch, [_json([_VALID_SPEC, _VALID_SPEC_2])])
    wired["smoke"] = _smoke("passed", "started",
                            accept_output=_accept_out(("a1", 404, "nf"), ("a2", 0, "")))
    out = _run_node({"project_id": "p1", "requirement_items": [_ITEM]})
    assert out["acceptance_passed"] is False
    assert out["runtime_smoke_details"]["classification"] == "acceptance_failed"
    d = out["acceptance_details"]
    assert d["failed_count"] == 1 and d["inconclusive_count"] == 1
    # inconclusive 行绝不进失败证据面（不冤枉写者）
    assert "a2" not in out["runtime_smoke_details"].get("acceptance_evidence", "")


def test_generation_grounding_fabricated_evidence_coerced_manual(wired, monkeypatch):
    """F7 节点级：LLM 给出语料里不存在的 evidence → 确定性降级 manual，不进脚本。"""
    fabricated = {**_VALID_SPEC, "evidence": "凭空捏造的接口文档 GET /api/ping"}
    _patch_llm(monkeypatch, [_json([fabricated])])
    out = _run_node({"project_id": "p1", "requirement_items": [_ITEM]})
    script = wired["smoke_calls"][0]["script"]
    assert "/api/ping" not in script, "臆造断言绝不进脚本"
    assert out["acceptance_passed"] is None
    assert out["acceptance_details"]["reason"] == "all_manual"
    assert any(r.startswith("acceptance_generation:rejected=1")
               for r in out["degraded_reasons"]), "降级必须留痕"


def test_migration_and_acceptance_double_failure_keeps_acceptance_evidence(
        wired, monkeypatch):
    """审MED：migration 与 acceptance 同轮双失败——migration 通道优先定分类（既有裁决），
    但验收证据键必须并入 runtime_smoke_details（归因面不丢断言侧证据）。"""
    _patch_llm(monkeypatch, [_json([_VALID_SPEC])])
    wired["smoke"] = _smoke("passed", "started",
                            accept_output=_accept_out(("a1", 404, "Not Found")))

    async def _mig_failed(*args, **kwargs):
        return {"migration_verify_passed": False,
                "migration_verify_details": {
                    "reason": "sql_error",
                    "evidence": {"output_tail": "ERROR: syntax error at line 3",
                                 "command": "run-migration"}},
                "_failed": True, "_message": "SQL 执行失败"}

    monkeypatch.setattr(verify_mod, "_run_migration_phase", _mig_failed)
    out = _run_node({"project_id": "p1", "requirement_items": [_ITEM]})
    assert out["runtime_smoke_passed"] is False
    d = out["runtime_smoke_details"]
    assert d["classification"] == "migration_failed", "migration 通道优先（既有裁决不变）"
    assert "migration_output" in d
    # 审MED 修复点：accept 也失败时 acceptance 证据键并入
    assert "/api/ping" in d["acceptance_evidence"]
    assert d["acceptance_failed_count"] == 1
    assert out["acceptance_passed"] is False


# ───────────────────────── elaborate covers 继承 ─────────────────────────

def test_split_oversized_by_files_inherits_covers():
    from swarm.brain.planning_nodes import _split_oversized_by_files
    from swarm.types import FileScope, SubTask

    st = SubTask(
        id="st-1", description="父任务",
        scope=FileScope(create_files=["ui/a.html", "ui/b.html", "db/init.sql"]),
        covers=["req-11111111", "req-22222222"],
    )
    children = _split_oversized_by_files(st, max_files=2)
    assert len(children) >= 2, "夹具应触发真实拆分"
    for c in children:
        assert c.covers == ["req-11111111", "req-22222222"], \
            f"子任务 {c.id} 丢失父 covers（覆盖矩阵校验会白烧 plan 重试）"


def test_resplit_subtask_inherits_covers(monkeypatch):
    import swarm.brain.planning_nodes as pn
    from swarm.types import FileScope, SubTask

    fake = _FakeLLM([_json({"subtasks": [
        {"description": "part 1", "acceptance_criteria": ["ok1"],
         "writable_files": ["a.py"], "est_context_tokens": 10},
        {"description": "part 2", "acceptance_criteria": ["ok2"],
         "writable_files": ["b.py"], "est_context_tokens": 10},
    ]})])
    monkeypatch.setattr(pn, "_get_brain_llm", lambda: fake)
    st = SubTask(id="st-9", description="超预算父任务",
                 scope=FileScope(writable=["a.py", "b.py"]),
                 covers=["req-33333333"], est_context_tokens=999999)
    children = asyncio.run(pn._resplit_subtask(st, {}, budget=1000))
    assert len(children) == 2
    for c in children:
        assert c.covers == ["req-33333333"]
