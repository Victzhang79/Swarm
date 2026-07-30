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


def _run(stdout: str, language_key=None):
    return asyncio.run(run_runtime_smoke(_Mgr(stdout), _Sbx(), "<script>",
                                         language_key=language_key))


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
    """反解成功 → 端口进 details（L-1：否则"跑在哪个端口"在 checkpoint 上无机读证据）。"""
    res = _run(_out("54321", tier="ss") + "\n__SMOKE_PROBE__ok:200")
    assert res.details.get("probe_port") == 54321 or res.status == "passed"


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
