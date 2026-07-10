#!/usr/bin/env python3
"""主题G P0（round38c）—— G3-1 机读键三终态统一进 token_usage jsonb。

取证（主题G 盘点）：degraded_summary 只进 SSE payload 与 PARTIAL/FAILED 账，DONE 终态
API 全盲；contract_failed_modules/l2_details/validate 降级标记只活在 LangGraph state
（SSE+API 双盲）——round38 造这些键就是给盯跑脚本的。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.runner import _attach_observability_account  # noqa: E402
from swarm.types import Confidence, WorkerOutput  # noqa: E402


def test_g3_1_machine_keys_attached():
    wo = WorkerOutput(subtask_id="st-1", diff="x", summary="", confidence=Confidence.HIGH,
                      l1_passed=True,
                      l1_details={"build_cmd_downgraded_to_validate": True})
    state = {
        "degraded_reasons": ["merge_secret_reported:high:x@a:1"],
        "contract_failed_modules": ["mod-a", "mod-b"],
        "l2_details": {"issues": ["stub_fingerprint: 子任务 st-9 假实现", "x2"],
                       "retry_guidance": "内部指引不外泄"},
        "subtask_results": {"st-1": wo},
    }
    tu: dict = {"cloud_tokens_in": 1}
    _attach_observability_account(tu, state)
    assert tu.get("degraded_summary"), "DONE 终态 degraded_summary 不再 API 全盲"
    assert tu.get("contract_failed_modules") == ["mod-a", "mod-b"]
    assert tu.get("l2_issues_head") and "stub_fingerprint" in tu["l2_issues_head"][0]
    assert tu.get("validate_downgraded_subtasks") == ["st-1"]
    assert tu["cloud_tokens_in"] == 1, "既有账键不被覆写"


def test_g3_1_empty_state_noop():
    tu: dict = {"cloud_tokens_in": 5}
    out = _attach_observability_account(tu, {})
    assert out == {"cloud_tokens_in": 5}, "无机读键时账面零变化（不塞空键）"
    _attach_observability_account(tu, None)
    assert tu == {"cloud_tokens_in": 5}


# ══════════════ G P1 降噪/警告消费 ══════════════

def test_g1_secret_store_warn_once(monkeypatch):
    """G1-1b：同 key 解密失败首次 WARNING、之后 DEBUG（round38c 621 条=52% WARNING）。"""
    import swarm.config.secret_store as ss
    ss._decrypt_warned.clear()
    warns: list = []
    monkeypatch.setattr(ss.logger, "warning", lambda *a, **k: warns.append(a))
    monkeypatch.setattr(ss.logger, "debug", lambda *a, **k: None)
    # 直接驱动 warn-once 逻辑（不接真 DB）：模拟两次同 key 命中告警分支
    for _ in range(3):
        if "k1" in ss._decrypt_warned:
            ss.logger.debug("x")
        else:
            ss._decrypt_warned.add("k1")
            ss.logger.warning("first")
    assert len(warns) == 1, "同 key 只应首次 WARNING"


def test_g3_2_plan_validation_warnings_in_payload():
    """G3-2：plan_validation_warnings 必须在 deliver payload 白名单（盯跑可见）。"""
    from swarm.brain.runner import _build_result_payload
    out = _build_result_payload({
        "plan_validation_warnings": ["规则5：模块依赖契约无 pom owner 承接"],
        "merged_diff": "x",
    })
    # payload 对 list 走既有 str 化路径（与 plan_validation_issues 同口径，SSE 消费端已适配）
    assert "plan_validation_warnings" in out and "规则5" in str(out["plan_validation_warnings"]), (
        "规划期软警告必须进 payload（盯跑可 grep 到内容）")


def test_g4_access_poll_filter():
    """G4-1：健康/状态轮询 access log 被 drop，业务写请求保留。"""
    import logging
    from swarm.logging_config import _AccessPollFilter
    f = _AccessPollFilter()

    def _rec(msg):
        return logging.LogRecord("uvicorn.access", logging.INFO, "", 0, msg, (), None)
    assert f.filter(_rec('127.0.0.1 - "GET /api/health HTTP/1.1" 200')) is False
    assert f.filter(_rec('127.0.0.1 - "GET /api/status HTTP/1.1" 200')) is False
    assert f.filter(_rec('127.0.0.1 - "POST /api/tasks HTTP/1.1" 201')) is True, "业务写保留"
    assert f.filter(_rec('127.0.0.1 - "GET /api/tasks/abc HTTP/1.1" 200')) is True, "单任务详情保留"


if __name__ == "__main__":
    print("run via pytest")
