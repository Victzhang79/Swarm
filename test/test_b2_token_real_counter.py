"""B2（round22, P0）：单任务 token 硬上限可绕过且与真实用量脱节。

根因：check_task_token_limit 仅在 merge/dispatch 查一次，且用 len(text)//4 启发式估算，
不读实际记账；analyze/plan/verify/handle_failure 等大量 LLM 调用不在检查范围。

治本（深修·无 schema 迁移风险）：
  1) usage_tracker 加 per-task 真实累计（ContextVar 归属 + 内存计数），record() 每次累加；
  2) check_task_token_limit 用 max(真实累计, 估算)——真实值主导，估算作 floor 兜底；
  3) 闸门节点边界扩到主 LLM 节点（在 runner，本测试覆盖 store/tracker 核心逻辑）。

llm_token_usage 表按 (project,kind,provider,model) 聚合、无 task_id → 不做高风险 schema
迁移；单进程拓扑下 ContextVar 归属可靠（worker 子任务经 gather 继承上下文）。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from swarm.models import usage_tracker
from swarm.project import store


def test_record_increments_per_task_counter():
    usage_tracker.clear_task_total("t-b2c")
    usage_tracker.set_current_task("t-b2c")
    try:
        usage_tracker.record("p", "local", "prov", "m", prompt_tokens=100, completion_tokens=50)
        assert usage_tracker.get_task_total_tokens("t-b2c") == 150
    finally:
        usage_tracker.set_current_task(None)
        usage_tracker.clear_task_total("t-b2c")


def test_limit_uses_real_recorded_over_estimate(monkeypatch):
    """真实累计 >> 估算且超限 → 判超（不再被 len//4 低估绕过）。"""
    usage_tracker.clear_task_total("t-b2")
    with usage_tracker._lock:
        usage_tracker._task_token_totals["t-b2"] = 999_999
    monkeypatch.setattr(store, "update_task", lambda *a, **k: None)
    fake_cfg = MagicMock(); fake_cfg.max_task_tokens = 1000
    monkeypatch.setattr("swarm.config.settings.get_config", lambda: fake_cfg)
    try:
        ok, usage = store.check_task_token_limit("t-b2", description="x")
        assert ok is False, usage
        assert usage.get("real_recorded") == 999_999, usage
        assert usage.get("total") == 999_999, usage
    finally:
        usage_tracker.clear_task_total("t-b2")


def test_limit_ok_when_under(monkeypatch):
    usage_tracker.clear_task_total("t-b2b")
    monkeypatch.setattr(store, "update_task", lambda *a, **k: None)
    fake_cfg = MagicMock(); fake_cfg.max_task_tokens = 1_000_000
    monkeypatch.setattr("swarm.config.settings.get_config", lambda: fake_cfg)
    ok, usage = store.check_task_token_limit("t-b2b", description="short")
    assert ok is True, usage


def test_limit_falls_back_to_estimate_when_no_real(monkeypatch):
    """无真实累计时退回估算（回归：大 merged_diff 仍能触发估算超限）。"""
    usage_tracker.clear_task_total("t-b2d")
    monkeypatch.setattr(store, "update_task", lambda *a, **k: None)
    fake_cfg = MagicMock(); fake_cfg.max_task_tokens = 10
    monkeypatch.setattr("swarm.config.settings.get_config", lambda: fake_cfg)
    ok, usage = store.check_task_token_limit("t-b2d", description="", merged_diff="x" * 4000)
    assert ok is False, usage  # 4000/4=1000 > 10
    assert usage.get("estimate_total", 0) >= 1000, usage


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
