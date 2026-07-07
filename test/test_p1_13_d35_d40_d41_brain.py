"""P1-13 D35/D40/D41 行为测试（深读登记册 2026-07-07）。

D35：家族拆分分类修正——Base/Abstract 前缀须按 CamelCase 词边界匹配（BaseballXxx 不是基类）；
     _detect_parallel_impls 分组键须含完整父路径（a/impl 与 b/impl 不得并成一个假家族）。
D40：调度器 _resolve_exec_meta 缓存命中也必须复核 DB status（排队期被 cancel 的任务不得出队执行）。
D41：retry_task 走 scheduler.submit_task 统一准入（受 MAX_CONCURRENT_TASKS/沙箱就绪闸门约束），
     调度器未运行（CLI/测试环境）时保留直跑兜底。
"""

from __future__ import annotations


# ── D35 ────────────────────────────────────────────────────────────


def test_d35_base_prefix_requires_camel_boundary():
    """Base/Abstract 前缀只在 CamelCase 词边界成立：BaseballScoreService/Abstraction/Basement
    不是共享抽象；BaseNotifyService/AbstractChannel 才是。"""
    from swarm.brain.planning_nodes import _is_upstream_shared

    # 真共享抽象：保持识别
    assert _is_upstream_shared("BaseNotifyService")
    assert _is_upstream_shared("AbstractChannel")
    assert _is_upstream_shared("ChannelBase")
    assert _is_upstream_shared("NotifySupport")
    assert _is_upstream_shared("INotifyService")
    # D35 误判修复：前缀命中但后随小写 = 普通单词，不是基类
    assert not _is_upstream_shared("BaseballScoreService")
    assert not _is_upstream_shared("Basement")
    assert not _is_upstream_shared("Abstraction")
    assert not _is_upstream_shared("AbstractionLayerHelper")


def test_d35_baseball_leaf_participates_in_family():
    """BaseballXxxHandler 应算平行 leaf（旧码误判共享抽象被剔出 leaves）。"""
    from swarm.brain.planning_nodes import _detect_parallel_impls

    core = [
        "mod/handler/BaseballScoreHandler.java",
        "mod/handler/FootballScoreHandler.java",
        "mod/handler/TennisScoreHandler.java",
    ]
    detected = _detect_parallel_impls(core)
    assert detected is not None
    leaves, upstream, downstream = detected
    assert "mod/handler/BaseballScoreHandler.java" in leaves
    assert not upstream and not downstream


def test_d35_same_basename_dirs_not_merged_into_one_family():
    """a/impl 与 b/impl 是两个互不相干目录，不得按目录 basename 并成一个 6-leaf 假家族。"""
    from swarm.brain.planning_nodes import _detect_parallel_impls

    core = (
        [f"mod/a/impl/{n}Handler.java" for n in ("Slack", "Ding", "Mail")]
        + [f"mod/b/impl/{n}Sender.java" for n in ("Sms", "Push", "Wx")]
    )
    detected = _detect_parallel_impls(core)
    assert detected is not None
    leaves, _upstream, _downstream = detected
    parents = {"/".join(f.split("/")[:-1]) for f in leaves}
    assert len(parents) == 1, f"跨父路径目录被并成一个家族: {sorted(parents)}"
    assert len(leaves) == 3


def test_d35_single_impl_dir_family_still_detected():
    """回归护栏：单个约定实现目录的家族检测行为不变（含共享接口归上游）。"""
    from swarm.brain.planning_nodes import _detect_parallel_impls

    core = [
        "svc/notify/impl/SlackNotifyService.java",
        "svc/notify/impl/DingTalkNotifyService.java",
        "svc/notify/impl/MailNotifyService.java",
        "svc/notify/impl/INotifyService.java",
    ]
    detected = _detect_parallel_impls(core)
    assert detected is not None
    leaves, upstream, downstream = detected
    assert len(leaves) == 3
    assert "svc/notify/impl/INotifyService.java" in upstream


# ── D40 ────────────────────────────────────────────────────────────


def _reset_sched():
    import swarm.brain.scheduler as sched

    sched._pending_meta.clear()
    sched._inflight.clear()


def test_d40_cache_hit_rechecks_db_status_cancelled(monkeypatch):
    """排队期被 cancel（DB=CANCELLED，缓存 meta 仍在）→ 出队必须丢弃，不得执行。"""
    import swarm.brain.scheduler as sched
    from swarm.project import store

    _reset_sched()
    sched._pending_meta["tx"] = {"project_id": "p", "description": "d", "auto_accept": False}
    monkeypatch.setattr(store, "get_task", lambda tid: {
        "id": tid, "project_id": "p", "description": "d", "status": "CANCELLED",
    })
    assert sched._resolve_exec_meta("tx") is None
    # 终态任务的陈旧 meta 一并清理，不泄漏
    assert "tx" not in sched._pending_meta


def test_d40_cache_hit_submitted_still_dispatches(monkeypatch):
    """缓存命中且 DB 仍是 SUBMITTED → 正常返回缓存 meta。"""
    import swarm.brain.scheduler as sched
    from swarm.project import store

    _reset_sched()
    meta_in = {"project_id": "p", "description": "d", "auto_accept": True}
    sched._pending_meta["ty"] = dict(meta_in)
    monkeypatch.setattr(store, "get_task", lambda tid: {
        "id": tid, "project_id": "p", "description": "d", "status": "SUBMITTED",
    })
    assert sched._resolve_exec_meta("ty") == meta_in


def test_d40_cache_hit_db_error_fail_closed(monkeypatch):
    """缓存命中但 DB 状态复核失败 → fail-closed 丢弃本次出队（任务仍 SUBMITTED，排水会补），
    且不误删 meta（状态未知）。"""
    import swarm.brain.scheduler as sched
    from swarm.project import store

    _reset_sched()
    sched._pending_meta["tz"] = {"project_id": "p", "description": "d", "auto_accept": False}

    def _boom(tid):
        raise ConnectionError("pg blip")

    monkeypatch.setattr(store, "get_task", _boom)
    assert sched._resolve_exec_meta("tz") is None
    assert "tz" in sched._pending_meta  # 状态未知不删 meta


# ── D41 ────────────────────────────────────────────────────────────


async def test_d41_retry_task_goes_through_scheduler_admission(monkeypatch):
    """调度器消费循环在跑时，retry_task 必须 submit_task 入队（统一准入），不得直跑 run_task。"""
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as sched
    from swarm.project import store

    captured: dict = {}
    submitted: list = []

    monkeypatch.setattr(runner, "can_retry_task", lambda tid: (True, ""))
    monkeypatch.setattr(store, "get_task", lambda tid: {
        "id": tid, "project_id": "p", "description": "d", "queue_priority": "urgent",
    })
    monkeypatch.setattr(store, "update_task", lambda tid, **kw: captured.update(kw))
    runner._task_running.clear()

    async def _no_run(*a, **k):
        raise AssertionError("retry_task 不得绕过调度器直跑 run_task")

    monkeypatch.setattr(runner, "run_task", _no_run)
    monkeypatch.setattr(sched, "is_consumer_running", lambda: True)
    monkeypatch.setattr(
        sched, "submit_task",
        lambda tid, pid, desc, **kw: submitted.append((tid, pid, desc, kw)),
    )

    ok = await runner.retry_task("t41")
    assert ok is True
    assert captured.get("status") == "SUBMITTED"
    assert len(submitted) == 1
    tid, pid, desc, kw = submitted[0]
    assert (tid, pid, desc) == ("t41", "p", "d")
    assert kw.get("priority") == "urgent"  # 保留原优先级


async def test_d41_retry_task_direct_run_fallback_when_scheduler_down(monkeypatch):
    """调度器未运行（CLI/测试）→ 保留直跑兜底语义，不静默丢任务。"""
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as sched
    from swarm.project import store

    ran: list = []

    monkeypatch.setattr(runner, "can_retry_task", lambda tid: (True, ""))
    monkeypatch.setattr(store, "get_task", lambda tid: {"id": tid, "project_id": "p", "description": "d"})
    monkeypatch.setattr(store, "update_task", lambda tid, **kw: None)
    runner._task_running.clear()

    async def _run(*a, **k):
        ran.append((a, k))

    monkeypatch.setattr(runner, "run_task", _run)
    monkeypatch.setattr(sched, "is_consumer_running", lambda: False)
    monkeypatch.setattr(
        sched, "submit_task",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("调度器未运行不应入队")),
    )

    ok = await runner.retry_task("t41b")
    assert ok is True
    assert len(ran) == 1


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
