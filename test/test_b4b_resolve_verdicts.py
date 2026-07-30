#!/usr/bin/env python3
"""B-4b V-H2 复核 H-1/M-1/M-2 的裁决锁 —— 三条整改此前**改了但没锁**。

突变实测（整改后第一轮）：把 H-1/M-1/M-2 三处逐一改回病灶，全部测试**照旧全绿** ——
与 hunter 指出的 H-2 同一种病（改对了但没有任何测试证明它被接上）。本文件补这三格。
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from swarm.brain.nodes.runtime_smoke import (
    MARK_DONE,
    MARK_LOG_BEGIN,
    MARK_LOG_END,
    MARK_PORT_RESOLVE_TIER,
    MARK_PORT_RESOLVED,
    MARK_PROBE_TOOL,
    run_runtime_smoke,
)

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_s = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_m = importlib.util.module_from_spec(_s)
_s.loader.exec_module(_m)


class _Sbx:
    sandbox_id = "sbx-vh2"


class _Mgr:
    def __init__(self, stdout: str):
        self._out = stdout

    def run_command(self, sandbox, command, timeout=120, _count_failures=True,
                    _skip_blacklist=False):
        return type("R", (), {"stdout": self._out, "stderr": "", "error": None})()


def _out(resolved: str, *, tier: str = "lsof", log: str = "", app_alive: bool = True) -> str:
    lines = [f"{MARK_PROBE_TOOL}curl", "__SMOKE_PHASE__resolve_port",
             f"{MARK_PORT_RESOLVE_TIER}{tier}", f"{MARK_PORT_RESOLVED}{resolved}",
             "__SMOKE_PHASE__collect", MARK_LOG_BEGIN, log, MARK_LOG_END]
    if app_alive:
        lines.append("__SMOKE_APP_RC__alive")
    lines.append(MARK_DONE)
    return "\n".join(lines)


def _run(stdout: str, language_key=None, project_symbols=None):
    return asyncio.run(run_runtime_smoke(_Mgr(stdout), _Sbx(), "<script>",
                                         language_key=language_key,
                                         project_symbols=project_symbols))


# ══════════════════════════════════════════════
# H-1：反解不出 ≠ 放过已观测到的崩溃
# ══════════════════════════════════════════════

def test_rust_startup_panic_is_failed_even_when_port_unresolved():
    """★H-1 本尊★ 反解不出端口，但日志尾有 Rust panic → 必须 `failed:code_error`。

    我原来的"判序在 probe 分类之前"只对 `probe_sequence` 成立（确实没探活），却**过度
    纠正**：崩溃模式只吃 `log_tail`，而它已收割在手。不过分类器 → V-H2 对目标栈**结构上
    产不出 failed**，而"启动就崩"恰是这道闸的头号理由（与 C-6"崩溃已发生 ≠ 什么都没观测
    到"直接冲突）。`failed` 是冒烟唯一的硬拦通道，降级成 skipped 就等于 auto_accept 放行。
    """
    res = _run(_out("NONE", log="thread 'main' panicked at src/main.rs:12:\n"
                                "called `Option::unwrap()` on a `None` value"),
               language_key="rust")
    assert res.status == "failed", f"启动 panic 被降级成 {res.status}:{res.classification}"
    assert res.classification == "code_error"


def test_go_nil_deref_is_failed_even_when_port_ambiguous():
    """AMBIGUOUS 分支同样要过分类器（两个出口都得接上，别只接一个）。"""
    res = _run(_out("AMBIGUOUS:8080,9090",
                    log="panic: runtime error: invalid memory address or "
                        "nil pointer dereference"),
               language_key="go")
    assert res.status == "failed" and res.classification == "code_error"


def test_clean_log_with_unresolved_port_stays_skipped():
    """对照臂：日志无崩溃形态 → 仍是 skipped（别把"没探到"一律升格成 failed＝冤杀）。"""
    res = _run(_out("NONE", log="Compiling api v0.1.0\nFinished dev profile"),
               language_key="rust")
    assert res.status == "skipped"
    assert res.classification == "port_unresolved"


# ══════════════════════════════════════════════
# M-1：没探测工具 ≠ 应用没 bind
# ══════════════════════════════════════════════

def test_all_tiers_missing_gets_its_own_reason_not_app_didnt_bind():
    """★M-1 本尊★ 四档工具全废 → `port_resolve_tooling_missing`，绝不与"应用没 bind"共名。

    共用一个 reason 会让判读的人去查应用（"是不是没监听？"），而真相是我们**没有探测
    手段**（沙箱镜像不装 iproute2/net-tools/lsof）。B-4a CRITICAL-3 同族：拒因必须如实。
    """
    res = _run(_out("NONE", tier="none_available"), language_key="go")
    assert res.status == "skipped"
    assert res.classification == "port_resolve_tooling_missing", res.classification
    assert "探测" in res.message and "不是应用没监听" in res.message
    assert res.details.get("port_resolve_tier") == "none_available"


def test_tool_present_but_no_listener_keeps_port_unresolved():
    """对照臂：工具在（lsof）但真没 listener → 仍报 `port_unresolved`（两档语义不互串）。"""
    res = _run(_out("NONE", tier="lsof"), language_key="go")
    assert res.classification == "port_unresolved"


def test_resolved_port_is_backfilled_into_details():
    """反解成功 → 端口进 details（L-1：否则"跑在哪个端口"在 checkpoint 上无机读证据）。

    ★H-2 整改★ 原断言写成 `== 54321 or res.status == "passed"`——`or` 右支恒真，
    把 `probe_port = int(_pr)` 整行删掉它照旧绿（实测 details 里根本没有这个键：
    `classify_smoke_outcome` 只在 `network_anomaly` 分支写 probe_port）。
    判据回到本仓铁律：把被测机制删掉，这条必须红。
    """
    res = _run(_out("54321", tier="ss") + "\n__SMOKE_PROBE__ok:200")
    assert res.status == "passed", res.status
    assert res.details.get("probe_port") == 54321, res.details


# ══════════════════════════════════════════════
# M-2：主动没生成 ≠ infra 输出截断
# ══════════════════════════════════════════════

def test_assertion_skip_on_reverse_resolve_path_has_its_own_label():
    """★M-2 本尊★ 反解路径上断言是**我们主动没生成**（端口构建时未知），
    reason 必须与 infra 语义的 `markers_missing` 分档。

    `markers_missing` 的注释写着"断言标记缺失（infra/输出截断）"——判读的人会去查沙箱输出
    被不被截断。而这是每一次带断言的 V-H2 冒烟的**常态**输出，复用它会让常态噪声盖过真
    infra 事故。
    """
    from swarm.brain.nodes.verify import _accept_phase_verdict

    specs = [{"id": "a1", "kind": "http_probe", "auth": "none", "req_id": "r1",
              "request": {"method": "GET", "path": "/health"},
              "expect": {"status": 200}}]
    patch = _accept_phase_verdict(specs, {}, "passed", "", None, skipped_no_port=True)
    assert patch["acceptance_passed"] is None
    assert patch["acceptance_details"]["reason"] == "port_unknown_at_build_time"
    assert patch["_degraded"] == "acceptance_skipped:port_unknown_at_build_time"


def test_real_markers_missing_keeps_the_infra_label():
    """对照臂：不是反解路径时（真 infra 截断）仍报 `markers_missing`。"""
    from swarm.brain.nodes.verify import _accept_phase_verdict

    specs = [{"id": "a1", "kind": "http_probe", "auth": "none", "req_id": "r1",
              "request": {"method": "GET", "path": "/health"},
              "expect": {"status": 200}}]
    patch = _accept_phase_verdict(specs, {}, "passed", "", None)
    assert patch["acceptance_details"]["reason"] == "markers_missing"
    assert patch["_degraded"] == "acceptance_skipped:markers_missing"


# ══════════════════════════════════════════════
# C-2 / M-2 / M-1 / M-4（reviewer 复核整改）
# ══════════════════════════════════════════════

_PROJ_SYMS = {"paths": {"src/routes/users.js"},
              "basenames": {"users.js", "users"},
              "top": {"src"}}


def test_reverse_path_project_internal_import_missing_is_code_error_failed():
    """★C-2（CRITICAL）★ 反解路径必须把 `project_symbols` 传给分类器。

    漏了它 → `_symbol_is_project_internal` 恒 None → 项目内相对 import 缺失
    （worker 漏建本地文件的常见形态）落 `dependency_missing`(skipped) 而非
    `code_error`(failed) → `can_auto_accept_delivery` 放行。H-1 承诺的硬拦通道
    对整个 import 族**结构上不可达**，差一个 kwarg。

    突变判据：删掉 `project_symbols=project_symbols` 这个实参，本条必红。
    """
    res = _run(_out("NONE", tier="ss", log="Error: Cannot find module './routes/users'"),
               language_key="node", project_symbols=_PROJ_SYMS)
    assert res.status == "failed", f"项目内 import 缺失被降级成 {res.status}/{res.classification}"
    assert res.classification == "code_error", res.classification


def test_reverse_path_external_import_missing_still_skipped():
    """C-2 对照臂：**项目外**符号缺失仍是 skipped（沙箱不装第三方依赖＝环境，不冤枉代码）。

    没有这条对照，上面那条可以被"一律判 failed"的错实现满足（零区分力）。
    """
    res = _run(_out("NONE", tier="ss", log="Error: Cannot find module 'express'"),
               language_key="node", project_symbols=_PROJ_SYMS)
    assert res.status == "skipped", res.status
    assert res.classification == "dependency_missing", res.classification


def test_reverse_path_port_busy_is_env_missing_not_port_unresolved():
    """★M-2（比原登记 O-3 宽）★ 端口被占的日志必须如实归 `env_missing`。

    原实现只采纳 `status=="failed"` → `address already in use` 被洗成
    `port_unresolved`（"应用没 bind"），而日志明写端口被占。
    突变判据：把采纳条件改回 `_cls.status == "failed"`，本条必红。
    """
    res = _run(_out("NONE", tier="ss", log="Error: listen EADDRINUSE address already in use"),
               language_key="node")
    assert res.classification == "env_missing", res.classification
    assert res.details.get("port_resolved_raw") == "NONE", (
        f"采纳分类器结论时丢了反解事实（归因不可追溯）：{res.details}")


def test_reverse_path_unattributed_crash_keeps_its_own_classification():
    """M-2 第二格：崩了但归不到代码 → `startup_crash_unattributed`，不冒充"应用没 bind"。"""
    res = _run(_out("NONE", tier="ss", log="APPLICATION FAILED TO START"))
    assert res.classification == "startup_crash_unattributed", res.classification


def test_reverse_path_no_observed_form_still_reports_port_unresolved():
    """M-2 对照臂：日志尾**真的什么形态都没有** → 照旧 `port_unresolved`。

    没有这条，M-2 可以被"一律采纳分类器"的错实现满足（那会让 port_* 三档整族失效）。
    """
    res = _run(_out("NONE", tier="ss", log="starting up..."))
    assert res.classification == "port_unresolved", res.classification


def test_tier_answered_is_a_separate_fact_from_tier_present():
    """★M-1 第二半★ "哪一档在场"与"哪一档作答"是两个键，不许互相冒充。

    `ss` 在场但因 netns/权限返空、实际由 lsof 作答时，只报在场档会让判读的人去查 ss。
    突变判据：让解析回退成用在场档填 answered，本条必红。
    """
    out = _out("54321", tier="ss") + "\n__SMOKE_PORT_RESOLVE_TIER_ANSWERED__lsof"
    out += "\n__SMOKE_PROBE__ok:200"
    res = _run(out)
    assert res.details.get("port_resolve_tier_answered") == "lsof", res.details


def test_log_derived_allowlist_excludes_probe_dependent_classifications():
    """M-2 的采纳集是**显式白名单**：预设"探活发生过"的档不许进。

    `network_anomaly` 的消息断言"探活不通"，而反解路径从未探活——采纳它＝自信且错误的
    归因。这条锁住白名单的**边界**（不是内容），突变把 network_anomaly 加进去即红。
    """
    from swarm.brain.nodes.runtime_smoke import _LOG_DERIVED_CLASSIFICATIONS
    assert "network_anomaly" not in _LOG_DERIVED_CLASSIFICATIONS
    assert "inconclusive" not in _LOG_DERIVED_CLASSIFICATIONS
    # 反解路径 + 日志自报 bind 成功 → 不采纳 network_anomaly，但它的证据键要留下来
    res = _run(_out("NONE", tier="ss", log="Now listening on: http://0.0.0.0:5000"),
               language_key="csharp")
    assert res.classification == "port_unresolved", res.classification
    assert res.details.get("bind_success_hits"), (
        f"未采纳分类器时丢了它已收割的证据（'日志自报 bind 却反解不到端口'是关键矛盾）"
        f"：{res.details}")


def test_all_bearer_assertions_on_reverse_path_blame_port_not_auth():
    """★M-5（reviewer 复核）★ 反解路径上"全 bearer"断言集必须归因**端口未知**，非 all_manual。

    `all_manual` 的语义是"鉴权边界故不自动执行"（前提＝下游人工复核），而反解路径的真相是
    端口构建时未知、**任何**断言都没生成执行片段。原判序让 M-2 的分档只覆盖到含至少一条
    `auth=none` 的集合，全 bearer 集借用了错标签 → 判读的人会去配登录，配了也没用。

    突变判据：把 `if skipped_no_port:` 那个早判删掉，本条必红。
    """
    from swarm.brain.nodes.verify import _accept_phase_verdict
    specs = [{"kind": "http_probe", "auth": "bearer", "id": "a1"},
             {"kind": "http_probe", "auth": "bearer", "id": "a2"}]
    patch = _accept_phase_verdict(specs, {}, "passed", "", skipped_no_port=True)
    assert patch["acceptance_details"]["reason"] == "port_unknown_at_build_time", \
        patch["acceptance_details"]["reason"]
    assert patch["_degraded"] == "acceptance_skipped:port_unknown_at_build_time"


def test_all_bearer_without_reverse_path_still_reports_all_manual():
    """M-5 对照臂：**非**反解路径的全 bearer 集照旧 `all_manual`（鉴权边界语义没被抹掉）。"""
    from swarm.brain.nodes.verify import _accept_phase_verdict
    specs = [{"kind": "http_probe", "auth": "bearer", "id": "a1"}]
    patch = _accept_phase_verdict(specs, {}, "passed", "", skipped_no_port=False)
    assert patch["acceptance_details"]["reason"] == "all_manual", \
        patch["acceptance_details"]["reason"]
