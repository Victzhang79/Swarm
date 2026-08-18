"""32 号文 A8-M1 治本锁：CAS 终态守卫只该拒【状态列】，不得连坐同批诊断载荷。

病根：`WHERE ... AND NOT (status = ANY(%s))` 作用于**整条 UPDATE**，而同批 sets 里可能
躺着 error / token_usage / duration_seconds / merge_conflicts / l3_result —— 守卫一触发
它们一起不落库。而 `update_task` docstring 自己写着"不改状态的字段级更新不受限
（终态后回填 token_usage/duration 合法）"：它们只因**搭了 status 的车**就被丢。
原实现的 WARNING 已承认这件事 ⇒ 发现了、写进日志了、没有任何消费者（血规 10④）。

治法：拆两段——status 列照旧被 CAS 拒（终态复活必须拦），其余字段用**不带守卫**的
第二条 UPDATE 落库。
"""
from __future__ import annotations

from unittest.mock import patch


class _FakeCursor:
    """记录每条 SQL；第一条（带 CAS 的）返回 None 模拟守卫命中，后续返回一行。"""

    def __init__(self, log: list, rows: list):
        self.log = log
        self.rows = rows
        self._last: object = None

    def execute(self, sql, params=None):
        norm = " ".join(sql.split())
        self.log.append((norm, params))
        # 带 CAS 的那条 → 0 行（守卫命中）；不带 CAS 的补写 → 返回一行
        self._last = None if "NOT (status = ANY" in norm else (self.rows[0] if self.rows else None)

    def fetchone(self):
        return self._last

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, log, rows):
        self.log = log
        self.rows = rows

    def cursor(self):
        return _FakeCursor(self.log, self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run(**kw) -> list[tuple[str, object]]:
    """跑一次 update_task（CAS 必命中），返回全部 SQL 日志。"""
    from swarm.project import store

    log: list = []
    with patch.object(store, "_get_conn", lambda conn_str=None: _FakeConn(log, [])), \
            patch.object(store, "get_task", lambda *a, **k: {"status": "CANCELLED"}):
        store.update_task("t-1", **kw)
    return log


def _row30(task_id: str = "t-1", status: str = "CANCELLED") -> tuple:
    """_TASK_SELECT 全 30 列的行（补写那条 UPDATE 的 RETURNING 结果）。

    刻意造**真行**而非 patch `_row_to_task`：返回值极性锁要断的是"补写命中了、
    却仍不得写回返回值"，patch 掉转换函数会把这条命题换成更弱的"转换没被调用"。
    """
    row: list = [None] * 30
    row[0] = task_id
    row[1] = "p-1"
    row[2] = "desc"
    row[3] = status          # DB 里的真状态（终态，CAS 正因它拒绝）
    row[6] = 3               # subtask_count
    row[7] = 1               # completed_subtasks
    return tuple(row)


def _run2(**kw):
    """同 _run，但补写那条 UPDATE **返回真行**，且把 update_task 的返回值一并交出。

    `_run` 传空 rows ⇒ 补写恒返 None ⇒ 极性缺陷（把补写行赋给 `row`）照样绿。
    这就是原 4 条锁漏掉它的原因：夹具让被测分支的取值域塌成单点。
    """
    from swarm.project import store

    log: list = []
    with patch.object(store, "_get_conn", lambda conn_str=None: _FakeConn(log, [_row30()])), \
            patch.object(store, "get_task", lambda *a, **k: {"status": "CANCELLED"}):
        ret = store.update_task("t-1", **kw)
    return ret, log


def test_cas_rejection_salvages_error_payload():
    """★核心锁★ CAS 拒 status 后，同批的 error 串必须由第二条 UPDATE 落库。

    `error` 是 R38-E 专为"FAILED 终态机读账、audit 不再是唯一去处"而造的字段，
    也是本条里**唯一无第二落点**的数据（token/duration 数字幸存于 task_ledger）。
    """
    log = _run(status="EXECUTING", error="worker 崩了: OOM")
    assert len(log) >= 2, (
        f"CAS 拒绝后必须再发一条【不带守卫】的 UPDATE 补写非状态字段，实得 {len(log)} 条"
    )
    guarded, salvage = log[0][0], log[1][0]
    assert "NOT (status = ANY" in guarded, "第一条必须带 CAS（终态复活仍要拦）"
    assert "NOT (status = ANY" not in salvage, "补写那条不得带守卫"
    assert "error = %s" in salvage, f"error 必须进补写，实得 SQL={salvage}"
    assert "status = %s" not in salvage, (
        "★补写绝不能带 status★ 否则终态复活防线被自己绕过"
        "（这条比「载荷丢失」更严重）"
    )
    assert any("OOM" in str(p) for p in (log[1][1] or [])), "补写参数须带 error 原文"


def test_cas_rejection_salvages_diagnostic_payload():
    """merge_conflicts / l3_result / token_usage / duration 同样必须补写。"""
    log = _run(status="EXECUTING", token_usage={"in": 1, "out": 2},
               duration_seconds=12.5, merge_conflicts=[{"f": "a.java"}],
               l3_result={"passed": False})
    assert len(log) >= 2, "必须有补写那条"
    salvage = log[1][0]
    for col in ("token_usage", "duration_seconds", "merge_conflicts", "l3_result"):
        assert f"{col} = %s" in salvage, f"{col} 必须进补写，实得 {salvage}"


def test_status_only_update_needs_no_salvage():
    """反向锁（防多余写库）：只改 status 时不该发第二条 UPDATE。

    没这条的话，"无脑总是补写"会在每次 CAS 拒绝时多打一次库（且 SET 为空必是语法错）。
    """
    log = _run(status="EXECUTING")
    assert len(log) == 1, (
        f"只有 status 时无非状态字段可补，不该发第二条 UPDATE，实得 {[l[0] for l in log]}"
    )


def test_cas_still_blocks_status_revival():
    """零回归锁：治法绝不能削弱 CAS 本体（终态复活=永久孤儿）。"""
    log = _run(status="EXECUTING", error="x")
    assert "NOT (status = ANY" in log[0][0]
    assert any(isinstance(p, list) and "CANCELLED" in p for p in (log[0][1] or [])), (
        "CAS 参数必须仍带终态清单"
    )
    # 补写那条完全不含 status 列（上面核心锁已断，这里再断参数面）
    assert all("EXECUTING" != p for p in (log[1][1] or [])), (
        "补写参数里绝不能出现目标状态值"
    )


# ──────────────────────────────────────────────
# R2（双复核整改）：返回值极性——补写命中也不得把行写回返回值
# ──────────────────────────────────────────────

def test_r2_cas_rejected_still_returns_none_even_when_salvage_hits():
    """★R2 核心锁★ 补写成功 ⇒ 返回值仍须是 None（"返 None ⇒ 状态未落库"契约）。

    缺陷形状：补写那条带 RETURNING，其 `fetchone()` 若赋回 `row`，函数末尾
    `if row` 即为真 ⇒ **CAS 拒绝被当成写入成功**返回一个 dict。
    唯一消费者 brain/runner.py `_salvage_partial_*` 的 `if _partial_row is None:`
    正靠 None 拦住"DB=CANCELLED 而用户收到 PARTIAL 部分交付通知"的假通知。
    今天那处只传 status（补写不发）才没咬到 ⇒ 坑坐在现役防线的**上游**。

    上面 4 条老锁全部只断 SQL 文本、没有一条断返回值——所以极性翻转从它们中间穿过。
    """
    ret, log = _run2(status="EXECUTING", error="worker 崩了: OOM")
    assert len(log) >= 2, "前提：必须真发了补写那条（否则本锁测的不是目标分支）"
    assert ret is None, (
        "★CAS 被拒后 update_task 必须返回 None★ 实得 "
        f"{type(ret).__name__}={ret!r}；补写落库是副作用，不是'状态写成功'——"
        "返非 None 会让 runner 发出 DB 里并不存在的 PARTIAL 通知"
    )


def test_r2_salvage_fields_still_land_when_return_is_none():
    """配对锁：极性修好之后，补写的**副作用**必须仍在（别把整块 salvage 删了也绿）。

    单有上一条时，"把 salvage 整块删掉"同样返 None ⇒ 全绿。两条合起来才钉死
    「返回值 None ∧ 字段确实落库」这一对不可分的事实。
    """
    ret, log = _run2(status="EXECUTING", error="OOM", l3_result={"passed": False})
    assert ret is None, "返回值极性（见上一条）"
    salvage = log[1][0]
    assert "NOT (status = ANY" not in salvage, "补写那条不得带守卫"
    for col in ("error", "l3_result"):
        assert f"{col} = %s" in salvage, f"{col} 必须真落库，实得 SQL={salvage}"
    assert any("OOM" in str(p) for p in (log[1][1] or [])), "补写参数须带 error 原文"


def test_r2_salvage_miss_is_machine_readable():
    """补写【未命中】（行已被删）与"本来没有非状态字段"必须在账上分得开。

    两者都会让 `_salvaged` 为空 ⇒ 日志长得一样 ⇒ 那一层可以死很久没人知道
    （血规 10④：空返回/缺席必须机读可辨）。
    """
    from swarm.project import store

    log: list = []
    # rows 为空 ⇒ 补写那条 RETURNING 也返 None，模拟"行在两条 SQL 之间被删"
    with patch.object(store, "_get_conn", lambda conn_str=None: _FakeConn(log, [])), \
            patch.object(store, "get_task", lambda *a, **k: {"status": "CANCELLED"}), \
            patch.object(store.logger, "warning") as _warn:
        ret = store.update_task("t-1", status="EXECUTING", error="x")
    assert ret is None, "未命中当然也返 None"
    assert len(log) >= 2, "前提：补写那条已发出"
    _msgs = [str(c.args) for c in _warn.call_args_list]
    assert any("未命中" in m for m in _msgs), (
        f"补写未命中必须在 WARNING 里机读可辨，实得 {_msgs}"
    )
