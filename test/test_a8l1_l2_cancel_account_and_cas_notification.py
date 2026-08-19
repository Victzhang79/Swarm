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
    这条断：`status="CANCELLED"` 的写点数 == 带账写点数，且四个 origin 字面量互不相同
    （否则复盘还是分不清是哪条路径取消的＝口径不齐没治好）。
    ★hunter MED 后★ 带账的唯一形态＝`await _cancel_proof_machine_account`（二次取消
    防护壳，裸 `_cancelled_machine_account` 全仓只剩壳体内一处，由锁⑤单独钉）。
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
        # token_usage=await _cancel_proof_machine_account(task_id, "<origin>")（壳内才是裸函数）
        _inner = _tu.value if isinstance(_tu, ast.Await) else _tu
        if (isinstance(_inner, ast.Call) and isinstance(_inner.func, ast.Name)
                and _inner.func.id in ("_cancel_proof_machine_account",
                                       "_cancelled_machine_account")):
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


def test_every_terminal_notification_is_guarded_by_the_write_result():
    """★接线锁（自复核后**收紧过**）★ 每一处宣布终态的通知都必须被写入返回值守卫。

    ★原锁的前件太窄，漏了一整类形态——本批 1c 教训在我自己的治法里第三次复发★
    初版只认 `_emit_task_notification(..., store.get_task(...), ...)` 这**一种语法指纹**
    （回读内联）。而 `_handle_post_run` 的正常终态路径是另一种形状：
      `task_rec = store.get_task(...)`（写**之前**读）… `store.update_task(status=...)`
      … `_emit_task_notification(task_id, task_rec, _final_status)`
    第二参是**变量名**，原锁完全看不见 ⇒ 那处（宣布 DONE，且走每个成功任务的正常路径）
    一直没被治，是我首轮范围判断的真实错漏，自复核抽样实读才发现。

    现在断的是**不变量本身**：函数体内若出现"写终态 status"，那么其后的终态通知必须
    位于一个引用了该写入返回值的 `if/else` 之内。不再依赖任何单一语法指纹。
    """
    import ast
    import inspect
    from pathlib import Path

    from swarm.brain import runner

    tree = ast.parse(Path(inspect.getfile(runner)).read_text(encoding="utf-8"))

    # ★判据历经两次自我证伪，最终形态＝「行序事件流」，理由记在这里★
    # v1 只认 `_emit_task_notification(..., store.get_task(...), ...)` 这**一种语法指纹**
    #    ⇒ 漏掉"写前读的变量"整类形态（`_rec` 版），我首轮范围判断就是这么错的；
    # v2 自顶向下跟 If 作用域，但对非 If 语句（`Try`）直接 walk 整棵子树 ⇒ 绕过自己的
    #    作用域跟踪，把**已守卫**的三处误报成未守卫；
    # v3（本版）不判嵌套结构，只按**行序**判：同函数内每个通知往上找最近的写 status，
    #    看它返回值有没有被绑定、且绑定名在两者之间被 `is None` 检验过。
    #    这样 `if row is None: … return`（早返回守卫）与 `if row is None: … else: 通知`
    #    两种形态**都认**——v2 只认后者，会把 salvage PARTIAL 那处冤报。
    _bad: list[tuple[str, int, str]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        events: list[tuple[int, str, str]] = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
                _f = n.value.func
                if (isinstance(_f, ast.Attribute) and _f.attr == "update_task"
                        and any(kw.arg == "status" for kw in n.value.keywords)):
                    _nm = next((t.id for t in n.targets if isinstance(t, ast.Name)), "?")
                    events.append((n.lineno, "write_bound", _nm))
            elif isinstance(n, ast.Call):
                _f = n.func
                if (isinstance(_f, ast.Attribute) and _f.attr == "update_task"
                        and any(kw.arg == "status" for kw in n.keywords)):
                    events.append((n.lineno, "write_discard", ""))
                elif isinstance(_f, ast.Name) and _f.id == "_emit_task_notification":
                    events.append((n.lineno, "notify", ast.unparse(n)[:100]))
            elif (isinstance(n, ast.Compare) and n.ops and isinstance(n.ops[0], ast.Is)
                  and isinstance(n.left, ast.Name)
                  and isinstance(n.comparators[0], ast.Constant)
                  and n.comparators[0].value is None):
                events.append((n.lineno, "isnone", n.left.id))
        # 同一行既 bound 又 discard 时保留 bound（Assign 的 value 也会被 walk 到）
        _bl = {ln for ln, k, _ in events if k == "write_bound"}
        events = [e for e in events if not (e[1] == "write_discard" and e[0] in _bl)]
        events.sort()

        for i, (ln, kind, payload) in enumerate(events):
            if kind != "notify":
                continue
            _prev = [e for e in events[:i] if e[1] in ("write_bound", "write_discard")]
            if not _prev:
                continue                      # 本函数只发通知不写终态 ⇒ 不在范围
            _w_ln, _w_kind, _w_name = _prev[-1]
            if _w_kind == "write_discard":
                _bad.append((fn.name, ln, payload))
            elif not any(k == "isnone" and nm == _w_name
                         for l2, k, nm in events if _w_ln <= l2 <= ln):
                _bad.append((fn.name, ln, payload))

    assert _bad == [], (
        "以下终态通知**未被写入返回值守卫**——CAS 拒绝时会宣布一个没落库的终态"
        "（DB=CANCELLED 而用户收到 DONE/FAILED）：\n  "
        + "\n  ".join(f"{fname}:{ln}  {code}" for fname, ln, code in _bad)
        + "\n形态可以不同（回读内联 / 写前读的变量 / 早返回守卫），不变量只有一条："
          "先看返回值再通知。"
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


def test_cancel_task_skips_write_when_runner_already_reached_terminal(monkeypatch):
    """★独立双复核 HIGH 整改锁★ 取消窗口内 runner 已自己收尾 → 不得再写 CANCELLED。

    ★缺陷是本批三条治法叠加出来的回归（复核者发现，我逐环实测坐实）★
    ① `cancel_task` 在 `await handle` **之前**读 task，判定用的是陈旧快照；
    ② `run_task` 的 CancelledError 臂在 await 期间已写 CANCELLED + 机读账（此时 ledger
       entry 仍在 ⇒ 账里**有**成本数字），随后 finally 里 `ledger.detach`；
    ③ 回到 cancel_task，陈旧快照仍是 ANALYZING ⇒ 判定恒真 ⇒ 第二次写，而此刻
       `ledger.snapshot()` 返 {} ⇒ **A8-M1 的不带守卫补写把 `token_usage` 整列替换**
       （它是整列写不是合并）⇒ 富账被贫账覆盖、`cancel_origin` 由 run_task 变 api_cancel。
    实测被覆盖会丢：cloud_tokens_in/out、local_tokens、llm_calls、stage_spent、budget_total。
    ⇒ 而"四路径各自可辨"正是 A8-L1 的立意，**同批两条治法当场互相抵消**。

    ★判据是"已终态"而非"已 CANCELLED"★：仅改现读不够——窗口内任务真跑完 DONE/PARTIAL 时
    `DONE != "CANCELLED"` 仍会写，补写照样把 `_handle_post_run` 落的真实成本账替换成取消账
    （行读作 DONE 而 token_usage 是 cancel_reason）。本锁把 DONE 那格也钉上。
    """
    from swarm.brain import runner

    for _terminal in ("CANCELLED", "DONE", "PARTIAL", "FAILED"):
        writes: list[dict] = []
        monkeypatch.setattr(runner.store, "update_task",
                            lambda tid, **kw: writes.append(kw))
        # 现读返回"已终态"（＝runner 在 await 窗口内自己收尾了）
        monkeypatch.setattr(runner.store, "get_task",
                            lambda tid, _s=_terminal: {"id": tid, "status": _s})
        monkeypatch.setattr(runner, "_task_handles", {})
        monkeypatch.setattr(runner, "_task_queues", {})
        monkeypatch.setattr(runner, "_task_running", set())

        assert asyncio.run(runner.cancel_task("t-x")) is True, "取消仍应返回成功"
        assert writes == [], (
            f"★当前已是终态 {_terminal} 时绝不能再写★ 那次写会被 CAS 拒掉 status，但 A8-M1 的"
            f"补写仍会把 token_usage 整列替换，覆盖掉既有终态的机读账。实得 {writes}"
        )


def test_cancel_task_still_writes_when_task_is_active(monkeypatch):
    """★配对锁★ 非终态（真的还在跑）时照旧写 CANCELLED + 带账。

    没有这条，上一条用"恒不写"的实现也能全绿——那会让 API 取消彻底失效。
    """
    from swarm.brain import runner

    writes: list[dict] = []
    monkeypatch.setattr(runner.store, "update_task",
                        lambda tid, **kw: writes.append(kw))
    monkeypatch.setattr(runner.store, "get_task",
                        lambda tid: {"id": tid, "status": "ANALYZING"})
    monkeypatch.setattr(runner, "_task_handles", {})
    monkeypatch.setattr(runner, "_task_queues", {})
    monkeypatch.setattr(runner, "_task_running", set())
    monkeypatch.setattr("swarm.models.ledger.snapshot", lambda *a, **k: {})

    assert asyncio.run(runner.cancel_task("t-y")) is True
    _cancel = [kw for kw in writes if kw.get("status") == "CANCELLED"]
    assert _cancel, f"活跃任务必须落 CANCELLED（否则 API 取消失效）。实得 {writes}"
    assert (_cancel[0].get("token_usage") or {}).get("cancel_origin") == "api_cancel", (
        f"且必须带 A8-L1 的机读账、origin=api_cancel。实得 {_cancel[0]}"
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


# ══════════════════ 独立双复核 HIGH-1：CAS 拒绝而 SSE 照发成功终态 ══════════════════
#
# 缺陷形态（范围由 probes/count_success_emits.py 机器定界：8 处成功态 emit，
# 真未守卫 3 处——本文件锁 2 处生产面；worker/runner.py 2 处无 CAS 写、
# _stream_brain_events 2 处是节点进度非终态宣布，均不在本族）：
# `_final_row is None` / `_pr_row is None` 分支只守住了 `_emit_task_notification`，
# 紧随的 complete/done 事件在 if/else 之外【无条件】发 ⇒ DB=CANCELLED 而 CLI
# 打印完成面板、退出码 0（CLI 对 step=="complete" break、对 "error" exit(1)）。
# 修法：CAS 拒绝时改发 step:"error"（既有词汇表，CLI 退出码非 0）+ 机读键
# brain.runner.terminal_announce_suppressed（与泛指键 update_task_cas_rejected 分档）。


def _post_run_store(monkeypatch, *, reject_status_write: bool):
    """_handle_post_run 的 store 夹具：reject_status_write=True 时带 status 的写返 None
    （模拟收尾窗口撞 cancel 被终态守卫拒绝），不带 status 的记账写照常落。"""
    from unittest.mock import MagicMock

    from swarm.brain import runner

    store = MagicMock()
    store.get_task.return_value = {
        "id": "t-h1", "project_id": "p1", "status": "CANCELLED", "description": "x"}
    store.estimate_token_usage.return_value = {}
    store.compute_task_duration_seconds.return_value = 1.0

    def _upd(tid, **kw):
        if "status" in kw and reject_status_write:
            return None
        return {"id": tid, "status": kw.get("status")}

    store.update_task.side_effect = _upd
    monkeypatch.setattr(runner, "store", store)
    monkeypatch.setattr(runner, "_sync_task_from_state", lambda tid, st: None)
    return runner, store


def _drain(sub) -> list[dict]:
    out = []
    while not sub.empty():
        out.append(sub.get_nowait())
    return out


def test_post_run_final_write_cas_rejected_announces_error_not_complete(monkeypatch):
    """★HIGH-1 锁①★ 正常终态尾：CAS 拒绝 ⇒ 恰一个 step:"error"、绝不发 complete、
    message 如实含 DB 当前态、机读键 +1。"""
    from swarm.infra.degrade import degrade_counts

    runner, _store = _post_run_store(monkeypatch, reject_status_write=True)
    topic = runner._FanoutTopic()
    sub = topic.subscribe()
    state = {"task_description": "x", "merged_diff": "diff --git a/f b/f\n+x\n",
             "l2_passed": True}
    asyncio.run(runner._handle_post_run("t-h1", state, topic))
    events = _drain(sub)

    assert not [e for e in events if e.get("step") == "complete"], (
        f"★CAS 拒绝时绝不得发 complete（DB=CANCELLED 而 CLI 宣布完成＝HIGH-1 本尊）★"
        f"实得 {events}")
    errors = [e for e in events if e.get("step") == "error"]
    assert len(errors) == 1, (
        f"应恰一个 error 事件（既有词汇表，CLI 对它 exit(1)；不造新词）。实得 {events}")
    assert errors[0].get("status") == "error" and "CANCELLED" in (errors[0].get("message") or ""), (
        f"error 事件必须如实说明 DB 当前态。实得 {errors[0]}")
    assert degrade_counts().get("brain.runner.terminal_announce_suppressed", 0) == 1, (
        f"机读键必须恰计 1 次（与 update_task_cas_rejected 分档，可独立告警）。"
        f"实得 {degrade_counts()}")


def test_post_run_final_write_cas_accepted_still_announces_complete(monkeypatch):
    """★配对锁①★ 落库成功时 complete 照发且并 result 载荷——没有这条，
    「恒发 error、永不 complete」的实现也能让锁①全绿。"""
    runner, _store = _post_run_store(monkeypatch, reject_status_write=False)
    topic = runner._FanoutTopic()
    sub = topic.subscribe()
    state = {"task_description": "x", "merged_diff": "diff --git a/f b/f\n+x\n",
             "l2_passed": True}
    asyncio.run(runner._handle_post_run("t-h1", state, topic))
    events = _drain(sub)

    completes = [e for e in events if e.get("step") == "complete"]
    assert len(completes) == 1 and completes[0].get("status") == "done", (
        f"落库成功必须恰发一个 complete/done。实得 {events}")
    assert not [e for e in events if e.get("step") == "error"], (
        f"落库成功不得夹带 error 事件。实得 {events}")
    assert (completes[0].get("result") or {}).get("merged_diff"), (
        f"D18 协议：result 载荷并入 complete。实得 {completes[0]}")


def _reject_partial_state() -> dict:
    """驱动 _handle_post_run 进 R52-1 诚实 PARTIAL 分支的最小 state：
    human_decision=reject + 当前 plan 内 1 个 L1 通过产出（_count_completed_in_plan 口径）
    + 非 plan_invalid + 非 clarify 阻断 ⇒ _partial_eligible=True。"""
    return {
        "task_description": "x",
        "human_decision": "reject",  # HumanDecision.REJECT.value
        "plan_validation_issues": ["g1 违例样例"],
        "subtask_results": {"st-1": {"l1_passed": True, "output": "ok"}},
        "merged_diff": "diff --git a/f b/f\n+x\n",
    }


def test_post_run_reject_partial_cas_rejected_announces_error_not_partial(monkeypatch):
    """★HIGH-1 锁②（同族第二处）★ R52-1 诚实 PARTIAL 分支：CAS 拒绝 ⇒ step:"error"、
    绝不发 done/partial、audit(task_partial) 不落（没落库的终态不产生审计）。"""
    from swarm.infra.degrade import degrade_counts

    runner, _store = _post_run_store(monkeypatch, reject_status_write=True)
    monkeypatch.setattr(runner, "_sweep_unverified_footprints", lambda *a, **k: None)
    audits: list[str] = []
    monkeypatch.setattr(runner, "audit",
                        lambda event, **kw: audits.append(event))
    topic = runner._FanoutTopic()
    sub = topic.subscribe()
    asyncio.run(runner._handle_post_run("t-h1", _reject_partial_state(), topic))
    events = _drain(sub)

    assert not [e for e in events if e.get("step") in ("complete", "done")], (
        f"★CAS 拒绝时绝不得宣布部分交付（done/partial）★ 实得 {events}")
    errors = [e for e in events if e.get("step") == "error"]
    assert len(errors) == 1 and "CANCELLED" in (errors[0].get("message") or ""), (
        f"应恰一个如实说明 DB 当前态的 error 事件。实得 {events}")
    assert "task_partial" not in audits, (
        f"没落库的 PARTIAL 不得落 task_partial 审计（否则审计面与 DB 自相矛盾）。实得 {audits}")
    assert degrade_counts().get("brain.runner.terminal_announce_suppressed", 0) == 1, (
        f"机读键必须恰计 1 次。实得 {degrade_counts()}")


def test_post_run_reject_partial_cas_accepted_still_announces_partial(monkeypatch):
    """★配对锁②★ 落库成功时 done/partial 照发、audit(task_partial) 照落——
    没有这条，「恒发 error」的实现也能让锁②全绿（诚实 PARTIAL 通路整个消失）。"""
    runner, _store = _post_run_store(monkeypatch, reject_status_write=False)
    monkeypatch.setattr(runner, "_sweep_unverified_footprints", lambda *a, **k: None)
    audits: list[str] = []
    monkeypatch.setattr(runner, "audit",
                        lambda event, **kw: audits.append(event))
    topic = runner._FanoutTopic()
    sub = topic.subscribe()
    asyncio.run(runner._handle_post_run("t-h1", _reject_partial_state(), topic))
    events = _drain(sub)

    dones = [e for e in events if e.get("step") == "done" and e.get("status") == "partial"]
    assert len(dones) == 1, f"落库成功必须恰发一个 done/partial。实得 {events}"
    assert not [e for e in events if e.get("step") == "error"], (
        f"落库成功不得夹带 error 事件。实得 {events}")
    assert "task_partial" in audits, (
        f"落库成功必须落 task_partial 审计。实得 {audits}")


# ═══════════════════ hunter MED：二次取消不得让终态写缺席 ═══════════════════
#
# 三个 CancelledError 处理器 + api_cancel 都在【清理途中】继续 await（查 watchdog 登记 /
# 取机读账），第二次取消在 await 点再起 ⇒ 处理器死在写终态之前 ⇒ 用户取消静默丢失
# （任务卡活跃态，对账复活重派）。治法＝两个防护壳：查登记壳（断→复查一次→再断走人工
# 取消臂+降级键）与取账壳（断→降级账 account_lost_to_second_cancel 照旧落终态）。

async def test_second_cancel_during_salvage_check_retries_once(monkeypatch):
    """★hunter MED 锁①★ 查登记被撞断一次 ⇒ 壳复查一次——第二次取消可能正带着
    watchdog 登记（竞速的另一面），复查让它名归原处走 salvage。"""
    import swarm.brain.runner as runner

    calls: list[str] = []

    async def _fake(task_id, queue):
        calls.append(task_id)
        if len(calls) == 1:
            raise asyncio.CancelledError  # 第二次取消在 await 点再起
        return True

    monkeypatch.setattr(runner, "_maybe_salvage_watchdog_abort", _fake)
    assert await runner._maybe_salvage_watchdog_abort_proof("t-med1", None) is True
    assert len(calls) == 2, "壳必须复查一次（登记可能正由第二次取消写入）"


async def test_second_cancel_twice_falls_back_to_user_cancel_arm_with_degrade(monkeypatch):
    """★hunter MED 锁②★ 连遭两次撞断 ⇒ 走人工取消臂（False＝调用方落 CANCELLED），
    且必须机读降级键留痕——绝不无终态死，也绝不静默放弃 salvage。"""
    import swarm.brain.runner as runner
    from swarm.infra.degrade import degrade_counts, reset_degrade_counts

    async def _boom(task_id, queue):
        raise asyncio.CancelledError

    monkeypatch.setattr(runner, "_maybe_salvage_watchdog_abort", _boom)
    reset_degrade_counts()
    try:
        assert await runner._maybe_salvage_watchdog_abort_proof("t-med2", None) is False
        assert degrade_counts().get("brain.runner.cancel_salvage_check_interrupted", 0) == 1, (
            f"连遭撞断必须记独立降级键恰一次。实得键={degrade_counts()}"
        )
    finally:
        reset_degrade_counts()


async def test_second_cancel_during_account_fetch_writes_degraded_account(monkeypatch):
    """★hunter MED 锁③★ 取账被撞断 ⇒ 降级账（机读标记）照旧供终态写使用——
    账可以贫，CANCELLED 写绝不缺席。"""
    import swarm.brain.runner as runner
    from swarm.infra.degrade import degrade_counts, reset_degrade_counts

    async def _boom(task_id, origin):
        raise asyncio.CancelledError

    monkeypatch.setattr(runner, "_cancelled_machine_account", _boom)
    reset_degrade_counts()
    try:
        acct = await runner._cancel_proof_machine_account("t-med3", "run_task")
        assert acct["account_lost_to_second_cancel"] is True, (
            f"降级账必须带机读标记（终态账的'贫'要可辨）。实得={acct}"
        )
        assert acct["cancel_origin"] == "run_task" and acct["cancel_reason"] == "user_cancelled", (
            f"降级账必须保住四路可辨的 origin 与 reason。实得={acct}"
        )
        assert degrade_counts().get("brain.runner.cancel_account_lost_to_second_cancel", 0) == 1, (
            f"必须记独立降级键恰一次。实得键={degrade_counts()}"
        )
    finally:
        reset_degrade_counts()


async def test_second_cancel_shells_pass_through_when_no_second_cancel(monkeypatch):
    """★hunter MED 锁④·配对★ 无第二次取消时壳必须零改写透传——壳本身不得改变
    正常取消路径的行为（防护层把正常路径改坏＝比不治更坏）。"""
    import swarm.brain.runner as runner
    from swarm.infra.degrade import degrade_counts, reset_degrade_counts

    async def _fake_salvage(task_id, queue):
        return False

    async def _fake_account(task_id, origin):
        return {"cancel_reason": "user_cancelled", "cancel_origin": origin, "llm_calls": 7}

    monkeypatch.setattr(runner, "_maybe_salvage_watchdog_abort", _fake_salvage)
    monkeypatch.setattr(runner, "_cancelled_machine_account", _fake_account)
    reset_degrade_counts()
    try:
        assert await runner._maybe_salvage_watchdog_abort_proof("t-med4", None) is False
        acct = await runner._cancel_proof_machine_account("t-med4", "api_cancel")
        assert acct == {"cancel_reason": "user_cancelled", "cancel_origin": "api_cancel",
                        "llm_calls": 7}, f"壳必须逐字透传正常账。实得={acct}"
        assert "brain.runner.cancel_salvage_check_interrupted" not in degrade_counts()
        assert "brain.runner.cancel_account_lost_to_second_cancel" not in degrade_counts()
    finally:
        reset_degrade_counts()


def test_cancel_cleanup_callsites_all_go_through_proof_shells():
    """★hunter MED 锁⑤·接线事实（AST 机器数，非手抄）★
    裸 `_maybe_salvage_watchdog_abort` / `_cancelled_machine_account` 的 await 调用点
    全仓必须只剩【壳体内】各一处；四个生产写点必须全走防护壳（3 处处理器走
    查登记壳、3+1 个写点走取账壳）。计数与清单一处机器算——少了说明有写点没接上壳。
    """
    import ast
    from pathlib import Path

    src = Path("brain/runner.py").read_text(encoding="utf-8")
    counts: dict[str, int] = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else (
                f.attr if isinstance(f, ast.Attribute) else "")
            if name in ("_maybe_salvage_watchdog_abort", "_cancelled_machine_account",
                        "_maybe_salvage_watchdog_abort_proof", "_cancel_proof_machine_account"):
                counts[name] = counts.get(name, 0) + 1
    assert counts.get("_maybe_salvage_watchdog_abort") == 1, (
        f"裸查登记调用必须只剩壳体内一处（多了＝有处理器没接壳）：{counts}")
    assert counts.get("_cancelled_machine_account") == 1, (
        f"裸取账调用必须只剩壳体内一处（多了＝有写点没接壳）：{counts}")
    assert counts.get("_maybe_salvage_watchdog_abort_proof") == 3, (
        f"三个 CancelledError 处理器必须全走查登记壳：{counts}")
    assert counts.get("_cancel_proof_machine_account") == 4, (
        f"四个 CANCELLED 写点（3 处理器 + api_cancel）必须全走取账壳：{counts}")
