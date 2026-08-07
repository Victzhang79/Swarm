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

def _drive_decrypt_failure(ss, key: str, times: int) -> None:
    """真调 `times` 次 get_secret，每次前清缓存以模拟 TTL 到期/多进程各自缓存。

    ★必须清 `_cache`★：decrypt 失败分支会把 `(None, now)` 写进缓存（性能治法），
    于是背靠背第二次调用**根本不进该分支**——不清缓存就把「warn-once 生效」换成了
    更弱的「负缓存生效」，删掉整个 warn-once 也照绿（血规 10②）。
    """
    for _ in range(times):
        ss._cache.clear()
        assert ss.get_secret(key) is None, "解密失败必须返 None（调用方回退 .env）"


def test_g1_secret_store_warn_once(monkeypatch, caplog, secret_store_state, fake_secret_conn):
    """G1-1b：同 key 解密失败首次 WARNING、之后 DEBUG（round38c 621 条=52% WARNING）。

    ★行为级★：真调 `get_secret`，patch `_get_conn` 返一行密文 + `decrypt` 抛异常。
    原实现把 warn-once 的 if/else 在测试体内**重写了一遍**（生产函数从未被调用），
    `grep get_secret` = 0 ⇒ 把 `config/secret_store.py` 的 warn-once 整块删掉、
    乃至删掉整个 `get_secret`，那条测试仍绿（29 号文 T-A1）。

    `secret_store_state` fixture 负责快照+还原三份模块级状态（清 `_cache` 是本测的前提，
    但不还原会把节流集/负缓存留给后续用例＝顺序依赖 flake）。
    """
    import logging

    ss = secret_store_state
    monkeypatch.setattr(ss, "_get_conn",
                        lambda conn_str=None: fake_secret_conn(("cipher-blob",)))
    monkeypatch.setattr(ss, "decrypt",
                        lambda _c: (_ for _ in ()).throw(ValueError("bad key")))

    with caplog.at_level(logging.DEBUG, logger=ss.logger.name):
        _drive_decrypt_failure(ss, "KEY_A", 3)
    # 只认【解密失败】那条告警：同函数底部 DB 失败分支也打 warning，按数量断会零区分力
    warns = [r for r in caplog.records
             if r.levelno >= logging.WARNING and "解密失败" in r.getMessage()]
    debugs = [r for r in caplog.records
              if r.levelno == logging.DEBUG and "解密失败" in r.getMessage()]
    assert len(warns) == 1, f"同 key 只应首次 WARNING，实得 {len(warns)}"
    assert len(debugs) == 2, f"同 key 后续两次应降 DEBUG，实得 {len(debugs)}"
    assert "KEY_A" in warns[0].getMessage(), "告警必须点名 key（运维据此定位轮换问题）"

    # 节流必须【按 key】而非全局开关：换 key 应再告警一次
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=ss.logger.name):
        _drive_decrypt_failure(ss, "KEY_B", 1)
    assert [r for r in caplog.records
            if r.levelno >= logging.WARNING and "KEY_B" in r.getMessage()], \
        "换 key 必须重新告警（全局 bool 会让第二个坏 key 全程静默）"


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
