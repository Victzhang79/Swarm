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

def test_limit_elastic_per_subtask(monkeypatch):
    """round27（E2E 实测 86d24aa0 误杀）：B2 换真实累计主导后，flat 500k 在 ULTRA 规划期
    即被合法消耗打穿（58 子任务规划真实 ~826k）→ 与墙钟 P1-B 同法改弹性预算：
    有效上限 = base + per_subtask×子任务数。规划前（subtask_count=0/None）退回 base。"""
    usage_tracker.clear_task_total("t-b2e")
    with usage_tracker._lock:
        usage_tracker._task_token_totals["t-b2e"] = 826_413  # 实测规划期真实累计
    monkeypatch.setattr(store, "update_task", lambda *a, **k: None)
    fake_cfg = MagicMock()
    fake_cfg.max_task_tokens = 500_000
    fake_cfg.max_task_tokens_per_subtask = 150_000
    monkeypatch.setattr("swarm.config.settings.get_config", lambda: fake_cfg)
    try:
        # 58 子任务 → 500k + 8.7M：合法大任务不被误杀
        ok, usage = store.check_task_token_limit("t-b2e", description="x", subtask_count=58)
        assert ok is True, usage
        # 规划前（count=None）→ 只有 base：826k > 500k 仍拦（防规划自身失控）
        ok2, _ = store.check_task_token_limit("t-b2e", description="x")
        assert ok2 is False
        # 真失控（超弹性上限）仍拦：2 子任务 → 500k+300k=800k < 826k
        ok3, _ = store.check_task_token_limit("t-b2e", description="x", subtask_count=2)
        assert ok3 is False
    finally:
        usage_tracker.clear_task_total("t-b2e")


def test_limit_zero_base_disables_gate(monkeypatch):
    """base=0 维持既有"关闭闸门"语义（弹性项不改变该开关）。"""
    usage_tracker.clear_task_total("t-b2f")
    with usage_tracker._lock:
        usage_tracker._task_token_totals["t-b2f"] = 99_999_999
    monkeypatch.setattr(store, "update_task", lambda *a, **k: None)
    fake_cfg = MagicMock()
    fake_cfg.max_task_tokens = 0
    fake_cfg.max_task_tokens_per_subtask = 150_000
    monkeypatch.setattr("swarm.config.settings.get_config", lambda: fake_cfg)
    try:
        ok, _ = store.check_task_token_limit("t-b2f", description="x", subtask_count=58)
        assert ok is True
    finally:
        usage_tracker.clear_task_total("t-b2f")
