"""Wave D — 交付/API 正确性回归。

覆盖：
- A-P1-05：部分交付(abandoned_subtask_ids 非空)不得当成功 → 不写 L6 成功模式；L2 摘要 outcome=partial。
- A-P1-06：L2 无测试命令路径不再静默"已测通过"，须打 degraded 标记 l2_no_test_executed（仍放行）。
- A-P1-28：worker 进度流按 run 归属项目做 task:read 校验；无权用户 403、未知 run 404。
- A-P1-30：webhook_url(含 token) 在 _mask_config_dict 中脱敏，明文不外泄。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from swarm.types import Complexity


# ─────────────────────────── A-P1-05 ───────────────────────────

def test_partial_delivery_does_not_write_l6_success():
    """放弃了子任务(PARTIAL) → should_write_success=False，绝不写 L6 成功模式。"""
    from swarm.memory.pattern_extractor import should_write_success

    # 即便复杂度满足写 L6 门槛，只要有 abandoned 也必须拒写
    state_partial = {"complexity": Complexity.COMPLEX, "abandoned_subtask_ids": ["st-3"]}
    assert should_write_success(state_partial) is False, "PARTIAL 任务不得写 L6 成功模式"

    # 对照：无 abandoned + 真实成功信号(l2_passed)的 complex 任务正常写 L6（TD2606-A7）
    state_ok = {"complexity": Complexity.COMPLEX, "abandoned_subtask_ids": [], "l2_passed": True}
    assert should_write_success(state_ok) is True


def test_partial_delivery_l2_outcome_is_partial():
    """persist_learn_success 对 PARTIAL 任务以 outcome=partial 落 L2，不标 success，且不写 L6。"""
    from swarm.brain import learn_store

    captured: dict = {}

    class _FakeStore:
        async def connect(self):
            return None

        def transaction(self):
            store = self

            class _Tx:
                async def __aenter__(self):
                    return store

                async def __aexit__(self, *a):
                    return False

            return _Tx()

        async def write_success(self, *a, **k):  # pragma: no cover - 不应被调用
            captured["wrote_success"] = True
            return 1

        async def write_task_summary(self, project_id, summary):
            captured["task_outcome"] = summary.outcome

        async def close(self):
            return None

    state = {
        "project_id": "proj-1",
        "task_id": "t-1",
        "complexity": Complexity.COMPLEX,
        "abandoned_subtask_ids": ["st-3"],
        "merged_diff": "--- a\n+++ b\n",
    }

    with patch.object(learn_store, "MemoryStore", _FakeStore):
        meta = asyncio.run(learn_store.persist_learn_success(state, {"pattern_name": "x"}))

    assert captured.get("task_outcome") == "partial", "PARTIAL 须以 outcome=partial 落 L2"
    assert not captured.get("wrote_success"), "PARTIAL 不得写 L6 成功模式"
    assert meta.get("persisted") is True


# ─────────────────────────── A-P1-06 ───────────────────────────

def test_l2_no_test_command_sets_degraded_reason():
    """无显式测试命令路径：l2_passed=True 仍放行，但须打 degraded 标记 l2_no_test_executed。"""
    from swarm.brain.nodes import verify as verify_mod
    from swarm.types import Complexity as _C

    state = {
        "merged_diff": "--- a/x.md\n+++ b/x.md\n@@ -1 +1 @@\n-a\n+b\n",
        "plan": None,
        "task_description": "update docs",
        "project_id": "proj-1",
        "subtask_results": {},
        "complexity": _C.MEDIUM,
    }

    # 强制走"无测试命令 + integration_review 通过"分支：
    #  - effective_complexity 非 SIMPLE
    #  - 有 project_path 且 integration_review 通过
    #  - test_cmd 为空
    with patch.object(verify_mod, "effective_complexity", return_value=_C.MEDIUM), \
         patch.object(verify_mod, "_l2_test_command_from_criteria", return_value=""), \
         patch("swarm.brain.nodes._get_project_path", return_value="/tmp/proj"), \
         patch("swarm.brain.integration_review.run_integration_review",
               return_value=(True, [], {})):
        out = asyncio.run(verify_mod.verify_l2(state))

    assert out.get("l2_passed") is True, "无测试任务仍放行(不硬卡 docs/config)"
    assert "l2_no_test_executed" in (out.get("degraded_reasons") or []), \
        "无功能测试须打 degraded 标记，不得静默当已测通过"


# ─────────────────────────── A-P1-28 ───────────────────────────

def test_worker_stream_denies_non_member():
    """无 task:read 权限的用户订阅他人 worker 流 → 403。"""
    from fastapi.testclient import TestClient

    from swarm.api.app import app
    from swarm.worker import runner

    runner.register_worker_run_project("run-secret", "proj-secret")
    try:
        with patch("swarm.auth.store.user_can_on_project", return_value=False):
            client = TestClient(app)
            resp = client.get("/api/worker/run-secret/stream")
        assert resp.status_code == 403, resp.text
    finally:
        runner._worker_run_project.pop("run-secret", None)


def test_worker_stream_unknown_run_404():
    """未知 run_id(无归属映射) → 404 fail-closed，且不为任意 run 建队。"""
    from fastapi.testclient import TestClient

    from swarm.api.app import app
    from swarm.worker import runner

    runner._worker_run_project.pop("run-ghost", None)
    with patch("swarm.auth.store.user_can_on_project", return_value=True):
        client = TestClient(app)
        resp = client.get("/api/worker/run-ghost/stream")
    assert resp.status_code == 404, resp.text


def test_worker_run_records_project_mapping():
    """起 worker 即记录 run_id→project_id，供 stream 鉴权使用。"""
    from swarm.worker import runner

    captured: dict = {}

    def _fake_create_task(coro):
        coro.close()  # 不真正调度协程
        return None

    with patch.object(runner.asyncio, "create_task", _fake_create_task):
        runner.start_standalone_worker_background("run-map", "proj-map", "desc")
    try:
        assert runner.get_worker_run_project("run-map") == "proj-map"
    finally:
        runner._worker_run_project.pop("run-map", None)
        runner._worker_queues.pop("run-map", None)


# ─────────────────────────── A-P1-30 ───────────────────────────

def test_mask_config_dict_masks_webhook_url():
    """含 token 的 webhook_url 在脱敏后不得明文出现（key 名匹配）。"""
    from swarm.api._shared import _mask_config_dict

    token = "abcd1234efgh5678ijkl9012mnop3456"
    cfg = {
        "notify": {
            "webhook_url": f"https://open.feishu.cn/open-apis/bot/v2/hook/{token}",
        }
    }
    out = _mask_config_dict(cfg)
    masked = out["notify"]["webhook_url"]
    assert token not in masked, "webhook token 不得明文外泄"
    assert "…" in masked or "..." in masked


def test_mask_config_dict_masks_webhook_url_in_list():
    """notify_channels 是 list[dict]，列表内每个 dict 的 webhook_url 也须脱敏。"""
    from swarm.api._shared import _mask_config_dict

    token = "ZZZZ1111YYYY2222XXXX3333WWWW4444"
    cfg = {
        "notify_channels": [
            {"id": "c1", "type": "feishu",
             "webhook_url": f"https://open.feishu.cn/open-apis/bot/v2/hook/{token}"},
        ]
    }
    out = _mask_config_dict(cfg)
    masked = out["notify_channels"][0]["webhook_url"]
    assert token not in masked


def test_mask_config_dict_masks_url_by_known_host():
    """key 名是泛 *_url 但 host 命中已知 webhook 提供方 → 同样脱敏。"""
    from swarm.api._shared import _mask_config_dict

    token = "TTTT0000UUUU1111VVVV2222SSSS3333"
    cfg = {"slack_url": f"https://hooks.slack.com/services/T0/B0/{token}"}
    out = _mask_config_dict(cfg)
    assert token not in out["slack_url"]


def test_mask_config_dict_leaves_plain_url_untouched():
    """普通(非 webhook) URL 不误脱敏，避免破坏 base_url 等正常字段。"""
    from swarm.api._shared import _mask_config_dict

    cfg = {"local_base_url": "https://api.example.com/v1"}
    out = _mask_config_dict(cfg)
    assert out["local_base_url"] == "https://api.example.com/v1"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
