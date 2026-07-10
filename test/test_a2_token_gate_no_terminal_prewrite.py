#!/usr/bin/env python3
"""A2（外部深审#14 P0）—— token 闸不预写终态：FAILED/PARTIAL 归 salvage 单点裁决。

根因（TASK_REGISTER 主题 A/A2，外部深审亲核 CONFIRMED）：
  check_task_token_limit 超限时自写 status=FAILED（project/store.py 旧 :2270）→
  runner raise TaskTokenLimitExceeded → salvage 判有产物写 PARTIAL
  （runner.py:1208）被终态 CAS 拒（_partial_row is None）→ 执行期估算闸触发即
  丢全部已完成子任务（round28 T-B 经路径顺序复活）。
治本：估算闸只 raise 不写终态；诊断标记（limit_exceeded/limit）照落 token_usage
  （runner H3 复核依赖）；终态由 _finalize/salvage 单点裁决（有产物 PARTIAL/无产物 FAILED）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.project import store  # noqa: E402


def _over_limit_call():
    cfg = MagicMock()
    cfg.max_task_tokens = 100
    with patch.object(store, "estimate_token_usage", return_value={"total": 9999999}), \
         patch.object(store, "update_task", return_value={}) as mock_upd, \
         patch("swarm.config.settings.get_config", return_value=cfg):
        ok, usage = store.check_task_token_limit("t-a2", description="big")
    return ok, usage, mock_upd


# ── 超限绝不预写终态：status 不出现在 update_task 调用里 ──
def test_a2_gate_does_not_prewrite_terminal_status():
    ok, usage, mock_upd = _over_limit_call()
    assert ok is False
    mock_upd.assert_called_once()
    _, kwargs = mock_upd.call_args
    assert "status" not in kwargs, (
        "token 闸预写 status=FAILED 会让 salvage 的 PARTIAL 写被终态 CAS 拒——"
        "执行期撞闸即丢全部已完成子任务；终态必须归 salvage 单点裁决")


# ── 诊断标记照落：limit_exceeded/limit 进 token_usage（runner H3 复核依赖）──
def test_a2_gate_still_persists_diagnostic_markers():
    ok, usage, mock_upd = _over_limit_call()
    _, kwargs = mock_upd.call_args
    tu = kwargs.get("token_usage") or {}
    assert tu.get("limit_exceeded") is True, "诊断标记必须照落（H3：salvage 归因可机读）"
    assert tu.get("limit") == 100


# ── salvage FAILED 分支机读账不覆写丢闸诊断键（对抗复核 minor 全治）──
def test_a2_failed_machine_account_keeps_limit_markers():
    from swarm.brain import runner
    with patch.object(runner.store, "get_task",
                      return_value={"token_usage": {"limit_exceeded": True, "limit": 100}}):
        tu = runner._failed_machine_account("t-a2f", {}, "token_budget_exceeded")
    assert tu.get("limit_exceeded") is True and tu.get("limit") == 100, (
        "FAILED 分支整体覆写 token_usage 不得抹掉'因预算闸而死'的机读归因"
        "（H3 合并原只在 PARTIAL 分支生效）")
    assert tu.get("salvage_reason") == "token_budget_exceeded"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("A2 全部通过")
