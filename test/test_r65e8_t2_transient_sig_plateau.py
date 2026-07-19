"""R65E8-T2（round65e8 task b4f2fcda st-3/4/5 假阴性 storm 2h 死因·纵深）：把 B2 同签名短路
从【仅 pipeline_blocked】扩到【确定性 L1 闸失败(有 det_fail_reason)】。

死因：st-3/4/5 的 `verify_failed: cd ruoyi-framework && mvn compile -q`（无 pipeline_blocked）被判
transient，HANDLE_FAILURE 约 5 轮/2h 重试同一逐字节相同的失败签名、零进展，从不证伪 transient
（重试间世界无变化）→空转到 abandon→连坐清盘。B2 的同签名短路只认 pipeline_blocked，verify_failed
逃过→storm。治本：det_fail_reason 归一成稳定指纹参与同一 B2 连击短路；★纯 infra/网络 transient
（无 det_fail_reason）绝不追踪——同输入重试正是其正确语义（世界会变）。
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import swarm.brain.nodes as nodes  # noqa: E402
from swarm.brain.nodes.failure import _normalize_fail_sig  # noqa: E402
from swarm.types import Confidence, WorkerOutput  # noqa: E402


class _FakeResp:
    def __init__(self, content):
        self.content = content


def _fake_llm_retry():
    class _L:
        async def ainvoke(self, _msgs):
            return _FakeResp('{"strategy":"retry","reasoning":"r"}')
    return lambda: _L()


_BUILD_DFR = ("build_fail: cannot find symbol class ByteSource")
_VERIFY_DFR = ("verify_failed: cd ruoyi-framework && mvn compile -q")


def _build_failed_out(sid, dfr=_BUILD_DFR):
    """transient 的 compile 阶段 build_fail（内容型指纹、无 pipeline_blocked）——T2 追踪对象。"""
    return WorkerOutput(
        subtask_id=sid, diff="", summary="编译错误", l1_passed=False,
        confidence=Confidence.LOW,
        l1_details={"failure_class": "transient", "det_fail_reason": dfr})


def _verify_failed_out(sid, dfr=_VERIFY_DFR):
    """transient 的 verify_failed（命令键、复核 HIGH 收窄后【排除】不追踪）。"""
    return WorkerOutput(
        subtask_id=sid, diff="", summary="代码正确但验收命令假阴性", l1_passed=False,
        confidence=Confidence.LOW,
        l1_details={"failure_class": "transient", "det_fail_reason": dfr,
                    "l1_2_compile_ok": True, "l1_2_1_build_ok": True})


def _infra_out(sid):
    """纯 infra/网络 transient——【无】det_fail_reason（不该被签名追踪）。"""
    return WorkerOutput(
        subtask_id=sid, diff="", summary="Connection error", l1_passed=False,
        confidence=Confidence.LOW,
        l1_details={"failure_class": "transient", "error": "Connection error: timeout"})


def _state(out, sig_count=None, dfr=_BUILD_DFR):
    state = {
        "plan": None,
        "failed_subtask_ids": ["st-1"],
        "subtask_results": {"st-1": out},
        "subtask_retry_counts": {},
        "dispatch_remaining": [],
        "degraded_reasons": [],
    }
    if sig_count is not None:
        state["subtask_block_signatures"] = {
            "st-1": {"sig": "det|" + _normalize_fail_sig(dfr), "count": sig_count}}
    return state


# ── 核心：compile build_fail 参与同签名连击短路（内容型指纹、安全） ──
def test_t2_build_fail_first_seen_records_det_sig():
    with patch.object(nodes, "_get_brain_llm", _fake_llm_retry()):
        out = asyncio.run(nodes.handle_failure(_state(_build_failed_out("st-1"))))
    assert out.get("subtask_transient_counts", {}).get("st-1") == 1, "首见照走 transient 退避"
    rec = out.get("subtask_block_signatures", {}).get("st-1") or {}
    assert rec.get("count") == 1 and (rec.get("sig") or "").startswith("det|"), \
        f"compile build_fail 应被 det 签名追踪；实得 {rec}"


def test_t2_build_fail_second_repeat_skips_transient_storm():
    """★RED 核★ 同一 build_fail 二连不变 → 跳过 transient 退避直落 capability 阶梯（不 storm）。"""
    with patch.object(nodes, "_get_brain_llm", _fake_llm_retry()):
        out = asyncio.run(nodes.handle_failure(_state(_build_failed_out("st-1"), sig_count=1)))
    assert "subtask_transient_counts" not in out, \
        "同签名二连：transient 退避对确定性 build_fail 无意义，必须跳过（不空转 storm）"
    assert out.get("subtask_block_signatures", {}).get("st-1", {}).get("count") == 2


# ── ★复核 HIGH 收窄回归锁★ verify_failed/test_fail【排除】不追踪（命令键/样板→不可辨进展） ──
def test_t2_verify_failed_excluded_not_signed():
    """verify_failed 键在命令串、恒定不辨进展 + verify 阶段无 sibling-wait 检测 → 排除，照常退避不短路。"""
    with patch.object(nodes, "_get_brain_llm", _fake_llm_retry()):
        out = asyncio.run(nodes.handle_failure(_state(_verify_failed_out("st-1"))))
    assert out.get("subtask_transient_counts", {}).get("st-1") == 1, "verify_failed 应照常退避（不短路）"
    assert "st-1" not in (out.get("subtask_block_signatures") or {}), \
        "verify_failed 不得被 det 签名追踪（命令键无法辨进展，误短路等-sibling 的合法重试）"


def test_t2_env_off_disables_det_tracking():
    """SWARM_TRANSIENT_DET_PLATEAU=0 → build_fail 亦不追踪（ops 逃生阀）。"""
    import os as _os
    _prev = _os.environ.get("SWARM_TRANSIENT_DET_PLATEAU")
    _os.environ["SWARM_TRANSIENT_DET_PLATEAU"] = "0"
    try:
        with patch.object(nodes, "_get_brain_llm", _fake_llm_retry()):
            out = asyncio.run(nodes.handle_failure(_state(_build_failed_out("st-1"))))
    finally:
        if _prev is None:
            _os.environ.pop("SWARM_TRANSIENT_DET_PLATEAU", None)
        else:
            _os.environ["SWARM_TRANSIENT_DET_PLATEAU"] = _prev
    assert "st-1" not in (out.get("subtask_block_signatures") or {}), "env 关时不追踪"


# ── 纪律护栏：纯 infra transient 绝不被短路（同输入重试正是其正确语义） ──
def test_t2_pure_infra_transient_not_signed():
    """纯 infra/网络 transient（无 det_fail_reason）→ 不签名、照常退避重试（世界会变，绝不误短路）。"""
    with patch.object(nodes, "_get_brain_llm", _fake_llm_retry()):
        out = asyncio.run(nodes.handle_failure(_state(_infra_out("st-1"))))
    assert out.get("subtask_transient_counts", {}).get("st-1") == 1, "infra transient 应照常退避"
    assert "st-1" not in (out.get("subtask_block_signatures") or {}), \
        "纯 infra transient 不得被签名追踪（否则误把可恢复的网络抖动当假阴性短路）"


def test_t2_changed_signature_resets_count():
    """build_fail 签名【变化】（有进展：修好 bug1 暴露 bug2）→ 连击计数归 1，不短路（不误杀真进展）。"""
    with patch.object(nodes, "_get_brain_llm", _fake_llm_retry()):
        # 预置旧 build_fail 签名(count=2)，本轮 worker 产出【不同】build_fail
        st = _state(_build_failed_out("st-1", dfr="build_fail: cannot find symbol class Foo"),
                    sig_count=2, dfr=_BUILD_DFR)
        out = asyncio.run(nodes.handle_failure(st))
    rec = out.get("subtask_block_signatures", {}).get("st-1") or {}
    assert rec.get("count") == 1, f"签名变化=有进展，连击应归 1（不短路）；实得 {rec}"


# ── _normalize_fail_sig 稳定性 ──
def test_normalize_strips_ansi_and_version_jitter():
    a = _normalize_fail_sig("\x1b[1;31mERROR\x1b[m Could not find ruoyi-system:jar:4.8.3 @ line 12")
    b = _normalize_fail_sig("\x1b[1;31mERROR\x1b[m Could not find ruoyi-system:jar:4.8.4 @ line 99")
    assert a == b, f"版本号/行号/ANSI 抖动应归一为同指纹；\n a={a!r}\n b={b!r}"
    assert "\x1b" not in a and "4.8" not in a


def test_normalize_distinguishes_real_difference():
    a = _normalize_fail_sig("verify_failed: cd ruoyi-framework && mvn compile")
    b = _normalize_fail_sig("build_fail: cannot find symbol class ByteSource")
    assert a != b, "真不同的失败原因指纹必须不同（不过度归一）"
