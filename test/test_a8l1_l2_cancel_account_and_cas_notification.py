"""32 号文 A8-L1 + A8-L2。

## A8-L1 取消是唯一没有终态机读账的终态

`runner.py` 四处 `status="CANCELLED"` 都**只写 status**（run_task / resume_task /
resume_planning 三个 CancelledError 处理器 + `cancel_task` 的 API 主动取消），
而 FAILED/PARTIAL 一律带 `_failed_machine_account` ⇒ 被取消的任务在 `task_records`
上永远没有机读账，"取消时处于什么降级状态、跑到哪一步"无处可查，复盘口径不齐。

★治法刻意**不复用** `_failed_machine_account`（血规：复用单一事实源 ≠ 复用其消费契约）★
逐条核过的两处后果差异：
1. 它发 `salvage_reason`，而取消**没有 salvage 动作** ⇒ 键名会说谎。新账发
   `cancel_reason` + `cancel_origin`（四个写点各自可辨＝治"口径不齐"本身）；
2. 它做 `dispatched_unaccounted` 守恒对账**并打 WARNING**——而"取消时有派发中未兑现的
   子任务"是取消的**必然结果、不是异常** ⇒ 直接复用会让每次人工取消都刷一条假警报，
   把真信号淹掉。新账**刻意不做**那项对账。

## A8-L2 CAS 静默失败向下游传播

`_emit_task_notification(task_id, store.get_task(task_id) or {}, "FAILED")` 三处：
第二参是**回读**的行，CAS 拒绝时拿到的是别人写的那个终态行，而第三参硬编码 "FAILED"
⇒ DB=CANCELLED 而用户收到"失败"。

★分母是机器数的，不是抄 findings★（`probes/a8l2_update_task_callsites.py`，AST 扫描
+ 自检样例）：生产面 `update_task` 调用点 **36**，其中写 status（可被 CAS 拒的全集）
**29**，治前只有 **1** 处检查返回值。findings 说的"35 处"是**所有**调用点（含不写
status 的，CAS 对它们不适用）。首跑分母还被 `build/lib/` 陈旧构建产物污染过（多出 20+
假调用点），已排除。

★治法分两层，且**刻意不逐个改那 25 处**★
- **中心层**（一处覆盖全部 29 个写点）：CAS 拒绝点加机读 degrade 键——那条 WARNING
  此前是唯一信号，只能人读（血规 10④）。拒绝**必然**经过那一处，故一处即全覆盖。
- **调用点层**：只改上述三处——它们是唯一"向用户**推送**一个与 DB 矛盾的硬编码终态"的
  形态。余下 25 处按机制分类后判为无此危害：写非终态（ANALYZING/revert_status）被拒
  **正是守卫的目的**（拦终态复活）；清扫循环写 FAILED 被拒说明目标已达成；其余是 SSE
  实时流而非持久化断言。逐个加检查是一次大范围重构、自带回归风险，收益不对称。
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _clean_degrade():
    from swarm.infra.degrade import reset_degrade_counts

    reset_degrade_counts()
    yield
    reset_degrade_counts()


# ══════════════════ A8-L1 取消终态机读账 ══════════════════

def _account(state: dict | None, origin: str = "run_task") -> dict:
    """跑一次 _cancelled_machine_account，state 快照与 ledger 都打桩。"""
    from swarm.brain import runner

    async def _snap(_tid):
        return state

    with patch.object(runner, "_best_effort_snapshot", _snap), \
         patch("swarm.models.ledger.snapshot",
               lambda _tid: {"cloud_tokens_in": 12, "llm_calls": 3}):
        return asyncio.run(runner._cancelled_machine_account("t-1", origin))


def test_cancel_account_has_reason_origin_and_spend():
    """★核心锁★ 取消账必须带：为什么(reason) / 哪条路径(origin) / 花了多少(ledger)。"""
    tu = _account({"subtask_results": {"a": 1, "b": 2},
                   "dispatch_remaining": ["c"],
                   "failed_subtask_ids": []})
    assert tu["cancel_reason"] == "user_cancelled", tu
    assert tu["cancel_origin"] == "run_task", tu
    assert tu["cloud_tokens_in"] == 12 and tu["llm_calls"] == 3, (
        f"必须带 ledger 花费快照（取消时已花的钱是复盘第一问）。实得 {tu}"
    )


def test_cancel_account_carries_progress_and_degraded():
    """跑到哪一步 + 取消那刻的降级态——复盘最想知道的两项。"""
    tu = _account({
        "subtask_results": {"a": 1, "b": 2, "c": 3},
        "dispatch_remaining": ["d", "e"],
        "failed_subtask_ids": ["f"],
        "degraded_reasons": ["ingest_partial_failure:1/3", "t4_wire_failed"],
    })
    assert tu["cancel_progress"] == {"completed": 3, "remaining": 2, "failed": 1}, tu
    assert tu["degraded_reasons"] == ["ingest_partial_failure:1/3", "t4_wire_failed"], tu
    assert tu.get("degraded_summary"), "必须带摘要（与 deliver payload 同口径）"


def test_cancel_account_does_not_reuse_failed_semantics():
    """★消费契约锁·本役核心★ 取消账**绝不**带 FAILED 专有的两样东西。

    ① `salvage_reason`：取消没有 salvage 动作，带上就是键名说谎；
    ② `dispatched_unaccounted`：那是 FAILED 侧的"派发过却无账"异常信号并**伴随
       WARNING**；而取消时必然有在飞未兑现的子任务 ⇒ 复用会让**每次**人工取消都刷一条
       假警报，把真信号淹掉。

    夹具刻意构造出"FAILED 侧会报 dispatched_unaccounted"的形状（派发总账里有 3 个 id，
    subtask_results 只有 1 个，且都不在 abandoned 里）——若有人把治法改成直接调
    `_failed_machine_account`，这条必红。
    """
    tu = _account({
        "subtask_dispatch_totals": {"a": 1, "b": 1, "c": 1},
        "subtask_results": {"a": 1},
        "abandoned_subtask_ids": [],
        "dispatch_remaining": ["b", "c"],
        "failed_subtask_ids": [],
    })
    assert "salvage_reason" not in tu, (
        f"取消没有 salvage 动作，带 salvage_reason 是键名说谎。实得 {sorted(tu)}"
    )
    assert "dispatched_unaccounted" not in tu, (
        f"★取消时有在飞未兑现子任务是必然结果、不是异常★ 带上它＝每次取消刷一条假警报。"
        f"实得 {sorted(tu)}"
    )


def test_cancel_account_never_blocks_cancellation():
    """账取不到绝不阻断取消（快照/ledger 双炸仍返可用账）。

    ★方向刻意与被观测的写入相反★：取消是用户主动动作，账是附加信息——
    为了记账而让取消失败是把主次颠倒。
    """
    from swarm.brain import runner

    async def _boom_snap(_tid):
        raise RuntimeError("checkpoint unreachable")

    def _boom_ledger(_tid):
        raise RuntimeError("ledger exploded")

    with patch.object(runner, "_best_effort_snapshot", _boom_snap), \
         patch("swarm.models.ledger.snapshot", _boom_ledger):
        tu = asyncio.run(runner._cancelled_machine_account("t-1", "api_cancel"))

    assert tu["cancel_reason"] == "user_cancelled" and tu["cancel_origin"] == "api_cancel", (
        f"双炸时仍须返回可用的最小账（至少 reason+origin）。实得 {tu}"
    )


def test_cancel_account_empty_ledger_is_silent(caplog):
    """★区分力锁★ ledger 空账不告警（与 FAILED 侧刻意不同）。

    FAILED 侧空账打 WARNING，因为那意味着"花了钱但账丢了"；取消时 entry 常已 detach，
    空账是**常态** ⇒ 后果不同、处置必须不同（同一族"复用单一事实源≠复用消费契约"）。
    """
    from swarm.brain import runner

    async def _snap(_tid):
        return {}

    with caplog.at_level(logging.WARNING), \
         patch.object(runner, "_best_effort_snapshot", _snap), \
         patch("swarm.models.ledger.snapshot", lambda _tid: {}):
        tu = asyncio.run(runner._cancelled_machine_account("t-1", "run_task"))

    assert tu["cancel_reason"] == "user_cancelled"
    assert not [r for r in caplog.records if "ledger" in r.getMessage()], (
        f"取消时空账是常态，不该告警。实得 {[r.getMessage()[:70] for r in caplog.records]}"
    )


def test_all_four_cancel_writers_are_wired():
    """★接线锁★ 四个 CANCELLED 写点**全部**带账，且 origin 各不相同。

    ★为什么必须数写点而不只测 helper★ 本仓血泪「机制存在 ≠ 被接上」：造对了原语却只接
    主调用点，是本仓反复出现的形态（加机制先数调用点，一个不落地列出来）。
    这条断：`status="CANCELLED"` 的写点数 == 带 `_cancelled_machine_account` 的写点数，
    且四个 origin 字面量互不相同（否则复盘还是分不清是哪条路径取消的＝口径不齐没治好）。
    """
    import ast
    import inspect
    from pathlib import Path

    from swarm.brain import runner

    src = Path(inspect.getfile(runner)).read_text(encoding="utf-8")
    tree = ast.parse(src)

    _cancel_writes = 0
    _accounted = 0
    _origins: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        _f = node.func
        if not (isinstance(_f, ast.Attribute) and _f.attr == "update_task"):
            continue
        _kw = {k.arg: k.value for k in node.keywords}
        _st = _kw.get("status")
        if not (isinstance(_st, ast.Constant) and _st.value == "CANCELLED"):
            continue
        _cancel_writes += 1
        _tu = _kw.get("token_usage")
        # token_usage=await _cancelled_machine_account(task_id, "<origin>")
        _inner = _tu.value if isinstance(_tu, ast.Await) else _tu
        if (isinstance(_inner, ast.Call) and isinstance(_inner.func, ast.Name)
                and _inner.func.id == "_cancelled_machine_account"):
            _accounted += 1
            for a in _inner.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    _origins.append(a.value)

    assert _cancel_writes >= 4, (
        f"CANCELLED 写点少于 4 个（findings 坐实的是 4 处）——若确有增删，同步改本锁。"
        f"实得 {_cancel_writes}"
    )
    assert _accounted == _cancel_writes, (
        f"★有 {_cancel_writes - _accounted} 个 CANCELLED 写点没接机读账★ "
        f"（接线覆盖 ≠ 机制存在：造对原语却只接主调用点是本仓反复出现的形态）"
    )
    assert len(set(_origins)) == len(_origins) == _cancel_writes, (
        f"四个 origin 必须互不相同，否则复盘仍分不清哪条路径取消的（口径不齐没治好）。"
        f"实得 {_origins}"
    )


# ══════════════════ A8-L2 CAS 拒绝不得向下游传播 ══════════════════

def test_cas_rejection_records_machine_readable_key():
    """★中心层锁★ CAS 拒绝必须留机读键——一处覆盖全部 29 个写 status 的调用点。

    治前唯一信号是 `store.py` 那条 WARNING（只能人读、要有人正好在看）。
    """
    from swarm.infra.degrade import degrade_counts
    from swarm.project import store

    class _Cur:
        def __init__(self):
            self._last = None

        def execute(self, sql, params=None):
            # 带 CAS 的那条 → 0 行（守卫命中）
            self._last = None if "NOT (status = ANY" in " ".join(sql.split()) else None

        def fetchone(self):
            return self._last

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch.object(store, "_get_conn", lambda *a, **k: _Conn()), \
         patch.object(store, "get_task", lambda *a, **k: {"status": "CANCELLED"}):
        got = store.update_task("t-1", status="FAILED")

    assert got is None, "★返回值契约★ CAS 拒绝必须返 None（A8-M1/R2 钉过的极性）"
    assert degrade_counts().get("project.store.update_task_cas_rejected") == 1, (
        f"CAS 拒绝必须记机读键（此前只有 WARNING＝无消费者）。实得 {dict(degrade_counts())}"
    )


class _FakeLock:
    def __init__(self, *a, **k):
        pass

    def acquire(self):
        return True

    def release(self):
        return None

    def renew(self):
        return True


def _drive_generic_exception(monkeypatch, *, cas_reject: bool):
    """真调 resume_task / resume_planning 的**泛异常臂**，返回 (通知列表, 写入列表)。

    夹具形状取自既有行为锁 `test_wallclock_deadline.py:133`（那条驱动 CancelledError 臂）。
    这里让 `_stream_brain_events` 抛普通 Exception ⇒ 走 `except Exception` 泛异常臂，
    即 A8-L2 三处形态所在。`update_task` 按 cas_reject 返 None（拒）或新鲜行（成）。
    """
    from swarm.brain import runner

    notified: list[tuple[str, dict, str]] = []
    updates: list[dict] = []

    async def _boom(*a, **k):
        raise RuntimeError("stream exploded")

    def _upd(_tid, **kw):
        updates.append(kw)
        if kw.get("status") and cas_reject:
            return None
        return {"id": _tid, "status": kw.get("status"), "description": "新鲜行"}

    monkeypatch.setattr("swarm.infra.redis_client.ModuleLock", _FakeLock)
    monkeypatch.setattr(runner, "_stream_brain_events", _boom)
    monkeypatch.setattr(runner, "_emit_task_notification",
                        lambda tid, rec, st: notified.append((tid, rec, st)))
    monkeypatch.setattr(runner.store, "get_task",
                        lambda tid: {"id": tid, "project_id": "p", "status": "CANCELLED"})
    monkeypatch.setattr(runner.store, "get_project", lambda pid: {"path": None})
    monkeypatch.setattr(runner.store, "update_task", _upd)
    monkeypatch.setattr("swarm.models.ledger.attach", lambda *a, **k: None)
    monkeypatch.setattr("swarm.models.ledger.detach", lambda *a, **k: None)
    monkeypatch.setattr("swarm.models.ledger.snapshot", lambda *a, **k: {})

    asyncio.run(runner.resume_task("t-a8l2", "approved"))
    return notified, updates


def test_cas_rejected_write_emits_no_false_notification(monkeypatch):
    """★A8-L2 核心行为锁★ CAS 拒绝 ⇒ **不发**通知（此前会发假 FAILED）。

    危害：CAS 拒绝时任务实际已是别的终态（如 CANCELLED），而通知第三参硬编码 "FAILED"
    ⇒ DB=CANCELLED 而用户收到"失败"＝观测面自相矛盾。
    """
    notified, updates = _drive_generic_exception(monkeypatch, cas_reject=True)

    # 前提断言：真走到了泛异常臂（确实尝试写 FAILED）
    assert any(kw.get("status") == "FAILED" for kw in updates), (
        f"前提不成立——没走到泛异常臂的 FAILED 写，本锁测不到目标形态。实得 {updates}"
    )
    assert notified == [], (
        f"★CAS 被拒时绝不能发通知★ 那会向用户推送一个与 DB 矛盾的终态。实得 {notified}"
    )


def test_accepted_write_notifies_with_the_returned_row(monkeypatch):
    """★配对锁★ 写入成功 ⇒ 照发通知，且带的是**返回值那一行**（不是回读的）。

    ★为什么断"来自返回值"而非只断"发了通知"★ 治法的另一半是消掉那次 `store.get_task`
    回读——回读在并发下拿到的可能已是别人写的行。这里让 get_task 返回一个**可区分的**
    脏行（status=CANCELLED），若实现回退去回读，带上来的 description 就不是"新鲜行"。
    """
    notified, updates = _drive_generic_exception(monkeypatch, cas_reject=False)

    assert len(notified) == 1, f"写入成功必须照发通知。实得 {notified}"
    _tid, _rec, _st = notified[0]
    assert _st == "FAILED", _st
    assert _rec.get("description") == "新鲜行", (
        f"★必须用 update_task 的返回值★ 实得的行来自回读（get_task 的脏行）⇒ 并发下会"
        f"带上别人写的终态。实得 {_rec}"
    )
    assert _rec.get("status") == "FAILED", (
        f"返回值行的 status 应是刚写成的 FAILED，而回读的脏行是 CANCELLED。实得 {_rec}"
    )


def test_all_three_notify_sites_guard_the_return_value():
    """★接线锁★ 三处同形态**全部**改到（改一处＝半落地，本仓反复出现的形态）。

    断"没有任何 `_emit_task_notification(..., store.get_task(...) ...)` 形态残留"——
    那是缺陷的语法指纹（回读 + 硬编码 status）。用 AST 而非子串，避免被注释满足。
    """
    import ast
    import inspect
    from pathlib import Path

    from swarm.brain import runner

    tree = ast.parse(Path(inspect.getfile(runner)).read_text(encoding="utf-8"))
    _stale = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_emit_task_notification"):
            continue
        # 第二参是 `store.get_task(...) or {}` / `store.get_task(...)` 即回读形态
        if len(node.args) < 2:
            continue
        _second = node.args[1]
        _cand = _second.values[0] if isinstance(_second, ast.BoolOp) else _second
        if (isinstance(_cand, ast.Call) and isinstance(_cand.func, ast.Attribute)
                and _cand.func.attr == "get_task"):
            _stale.append(node.lineno)

    assert _stale == [], (
        f"仍有 {len(_stale)} 处通知用**回读行**（行号 {_stale}）——CAS 拒绝时回读拿到的是"
        f"别人写的终态行，配硬编码 status 即向用户推送矛盾事实。三处必须同批改。"
    )


def test_cancel_paths_really_persist_the_account(monkeypatch):
    """★A8-L1 行为级接线锁★ 真调 resume_task/resume_planning 的取消臂，
    断落库的 CANCELLED 那笔**真带**机读账。

    ★为什么这条与上面那条 AST 接线锁都要有★ AST 那条证"四个写点都写了 token_usage="，
    这条证"跑起来真有账落到 update_task"——前者防漏接线，后者防"接了但运行时炸掉被
    上游 except 吞成空账"（本仓踩过：mixin 新方法被测试 stub 静默跳过，
    AttributeError 被业务 except 吞成假快照全绿）。夹具取自既有 CancelledError 行为锁。
    """
    from swarm.brain import runner

    updates: list[dict] = []

    async def _boom(*a, **k):
        raise asyncio.CancelledError()

    monkeypatch.setattr("swarm.infra.redis_client.ModuleLock", _FakeLock)
    monkeypatch.setattr(runner, "_stream_brain_events", _boom)
    monkeypatch.setattr(runner.store, "get_task",
                        lambda tid: {"id": tid, "project_id": "p"})
    monkeypatch.setattr(runner.store, "get_project", lambda pid: {"path": None})
    monkeypatch.setattr(runner.store, "update_task",
                        lambda tid, **kw: updates.append(kw))
    monkeypatch.setattr("swarm.models.ledger.attach", lambda *a, **k: None)
    monkeypatch.setattr("swarm.models.ledger.detach", lambda *a, **k: None)
    monkeypatch.setattr("swarm.models.ledger.snapshot",
                        lambda *a, **k: {"cloud_tokens_in": 7, "llm_calls": 2})

    for fn, args, want_origin in (
        (runner.resume_task, ("t-c-rt", "approved"), "resume_task"),
        (runner.resume_planning, ("t-c-rp", {"decision": "approve"}), "resume_planning"),
    ):
        updates.clear()
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(fn(*args))
        _cancel = [kw for kw in updates if kw.get("status") == "CANCELLED"]
        assert _cancel, f"{fn.__name__} 取消臂未落 CANCELLED（F3 回归）"
        _tu = _cancel[0].get("token_usage") or {}
        assert _tu.get("cancel_reason") == "user_cancelled", (
            f"★{fn.__name__} 的取消写没带机读账★（治前取消是唯一无账的终态）。实得 {_cancel[0]}"
        )
        assert _tu.get("cancel_origin") == want_origin, (
            f"origin 必须标出是哪条路径取消的（治「复盘口径不齐」本身）。实得 {_tu}"
        )
        assert _tu.get("cloud_tokens_in") == 7, (
            f"必须带 ledger 花费（取消时已花的钱）。实得 {_tu}"
        )


def test_degrade_counter_failure_does_not_break_write_semantics(monkeypatch):
    """★观测面绝不反噬写入语义★ 计数面炸掉时 `update_task` 的返回值契约不变。

    ★为什么补这条★ 突变实验发现：把 CAS 拒绝点的 `record_degrade` 异常兜底改成 `raise`
    时**所有锁仍绿**——因为没有任何锁模拟"计数本身失败"。而那个兜底正是防"为了记一笔
    观测数而让终态写入崩掉"。批 3 有同型锁（`test_degrade_helper_never_breaks_authz`），
    这里漏了 ⇒ 落单的兜底就是下一个假绿。
    """
    from swarm.project import store

    class _Cur:
        def execute(self, sql, params=None):
            pass

        def fetchone(self):
            return None          # CAS 命中

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _boom(_c):
        raise RuntimeError("counter exploded")

    monkeypatch.setattr("swarm.infra.degrade.record_degrade", _boom)
    monkeypatch.setattr(store, "_get_conn", lambda *a, **k: _Conn())
    monkeypatch.setattr(store, "get_task", lambda *a, **k: {"status": "CANCELLED"})

    # 不得抛，且返回值契约（拒绝⇒None）必须保持——那是现役防线依赖的哨兵
    assert store.update_task("t-1", status="FAILED") is None, (
        "计数面异常不得改变 CAS 拒绝的返回值契约（唯一消费者靠 `is None` 拦假通知）"
    )
