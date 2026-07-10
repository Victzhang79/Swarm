#!/usr/bin/env python3
"""主题I I1 高优前段（round38c 外部深审）—— #13 Node 构建链假 DONE + #15 GraphInterrupt 吞。

#13：_detect_build_cmd_generic 的 package.json 分支尾部 `|| true` 把 Node 真编译失败
全吞成 exit 0=L2 假绿假 DONE（违 DONE 铁律）。
#15：clarify 虚假前提分支的 interrupt() 被 except Exception 宽捕获——interrupt 靠抛
GraphInterrupt 暂停图等人工 resume，吞掉=交互人工澄清闸从未真正暂停过。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ══════════════ #13 ══════════════

def test_13_node_build_cmd_never_forces_success(tmp_path):
    from swarm.brain.integration_review import _detect_build_cmd_generic
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"build": "webpack"}}), encoding="utf-8")
    cmd = _detect_build_cmd_generic(str(tmp_path))
    assert cmd and "|| true" not in cmd, (
        "`|| true` 把 Node 真编译失败全吞成 exit 0=L2 假绿假 DONE（违 DONE 铁律）")
    assert "npm run build" in cmd


def test_13_node_tsconfig_fallback_and_honest_none(tmp_path):
    from swarm.brain.integration_review import _detect_build_cmd_generic
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    cmd = _detect_build_cmd_generic(str(tmp_path))
    assert cmd and "tsc --noEmit" in cmd and "|| true" not in cmd
    (tmp_path / "tsconfig.json").unlink()
    assert _detect_build_cmd_generic(str(tmp_path)) is None, (
        "纯 JS 无 build script 无 tsconfig=无确定性编译面，诚实 None（合理跳过≠强制成功）")


# ══════════════ #15 ══════════════

async def test_15_graph_interrupt_reraised(monkeypatch):
    """interrupt 抛 GraphInterrupt 族异常时 clarify 必须重抛（让 runtime 真暂停），
    绝不吞成 clarify_blocked_by_facts 终止。"""
    import langgraph.types as lgt
    from swarm.brain.planning_nodes import clarify

    class GraphInterrupt(Exception):  # 同名类模拟（判定按类名含 Interrupt）
        pass

    def _fake_interrupt(payload):
        raise GraphInterrupt("simulated pause")

    monkeypatch.setattr(lgt, "interrupt", _fake_interrupt)
    monkeypatch.delenv("SWARM_AUTO_ACCEPT", raising=False)  # 别的测试遗留会走 auto 早退
    monkeypatch.delenv("SWARM_MODEL_TIER_ENABLED", raising=False)  # tier 污染防 clarify_rounds=0 早退
    state = {
        "task_id": "t1",
        "task_description": "x",
        "tech_design_fact_issues": [
            {"claim": "存在 Foo.java", "detail": "不存在", "suggestion": "确认路径",
             "verdict": "false"}],
        "clarify_history": [], "clarify_round": 0,
        # 非 auto 模式（交互）
    }
    with pytest.raises(GraphInterrupt):
        await clarify(state)


async def test_15_non_interrupt_exception_still_degrades(monkeypatch):
    """非 Interrupt 异常保留优雅降级（blocked_by_facts 终止），不误伤原兜底。"""
    import langgraph.types as lgt
    from swarm.brain.planning_nodes import clarify

    def _fake_interrupt(payload):
        raise RuntimeError("checkpointer unavailable")

    monkeypatch.setattr(lgt, "interrupt", _fake_interrupt)
    state = {
        "task_id": "t1", "task_description": "x",
        "tech_design_fact_issues": [{"claim": "c", "detail": "d", "verdict": "false"}],
        "clarify_history": [], "clarify_round": 0,
    }
    out = await clarify(state)
    assert out.get("clarify_blocked_by_facts") is True


if __name__ == "__main__":
    print("run via pytest")
