#!/usr/bin/env python3
"""30 号文批17 LG-1 锁：ledger flush 写库带代际戳（快照-写库分离丢更新竞态）。

原病（探针 `30_probes/probe_30_lg_flush_detach_lost_update.py` 复跑坐实，EXIT=0）：
`flush()` 锁内快照 R0+清 dirty、锁外 `_flush_row` 写库；并发 detach() 的二次 flush
快照 R1 先落库后，flusher 的旧写 R0 放行 ⇒ DB 被旧快照【静默回滚】（800/200 覆盖
2300/700），resume 后预算闸少限 1500 个已结算 token，全程零日志零机读键。

治本（登记册方法①，根修）：行内 `seq` 单调代际戳——flush() 写路径每次快照递增；
`_flush_row` 以 `ON CONFLICT ... WHERE task_ledger.seq < EXCLUDED.seq` 让 DB 侧拒收
陈旧代际；attach 从 DB 回读延续（resume 跨进程单调）。拒收=更新的代际已落库
⇒ 成功无-op（不 re-dirty），但必打 WARNING（机读键 stale_flush_rejected）。
"""
from __future__ import annotations

import logging

import pytest

from swarm.models import ledger


@pytest.fixture
def _clean_ledger(monkeypatch):
    ledger._reset_for_tests()
    monkeypatch.setattr(ledger, "_load_row", lambda task_id: None)
    monkeypatch.setattr(ledger, "_flush_row", lambda *a, **k: True)
    yield
    ledger._reset_for_tests()


def test_flush_stamps_monotonic_seq(_clean_ledger, monkeypatch):
    """代际戳主锁：每次出库快照 seq 单调递增；读路径 snapshot() 绝不碰 seq
    （消费契约分档——读路径若也递增，第一次 flush 的 seq 会大于 1）。"""
    rows = []
    monkeypatch.setattr(ledger, "_flush_row",
                        lambda tid, row: rows.append((tid, dict(row))) or True)
    ledger.attach("t-seq", 1000)
    ledger.flush()
    ledger.snapshot("t-seq")  # 读路径：不得递增
    ledger.snapshot("t-seq")
    ledger.set_budget("t-seq", 2000)
    ledger.flush()
    seqs = [r["seq"] for _tid, r in rows]
    assert seqs == [1, 2], f"seq 必须按出库快照单调递增，实际 {seqs}"


def test_seq_survives_resume(_clean_ledger, monkeypatch):
    """resume 延续锁：DB 回读 seq=41 ⇒ 重启后第一次 flush 必须续 42（不归零）。
    不归零 = 旧进程已落库的代际不会被新进程从 1 开始的写拒收/回滚。"""
    rows = []
    monkeypatch.setattr(ledger, "_load_row", lambda task_id: {
        "cloud_tokens_in": 800, "cloud_tokens_out": 200, "local_tokens": 0,
        "llm_calls": 3, "wall_ms": 100, "replan_rounds": 0,
        "stage_spent": {}, "budget_total": 5000, "seq": 41,
    })
    monkeypatch.setattr(ledger, "_flush_row",
                        lambda tid, row: rows.append(dict(row)) or True)
    ledger.attach("t-resume", 1000)
    ledger.flush()
    assert [r["seq"] for r in rows] == [42], \
        f"resume 后 seq 必须从 DB 值延续（42），实际 {[r['seq'] for r in rows]}"


class _FakeCur:
    """rowcount=0 = DB 侧 WHERE seq < EXCLUDED.seq 拒收（陈旧代际）。"""

    def __init__(self, rowcount):
        self.rowcount = rowcount
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakePool:
    def __init__(self, cur):
        self._cur = cur

    def connection(self):
        return _FakeConn(self._cur)


def _row(**over):
    base = {
        "cloud_tokens_in": 1, "cloud_tokens_out": 1, "local_tokens": 0,
        "llm_calls": 1, "wall_ms": 0, "replan_rounds": 0,
        "stage_spent": {}, "budget_total": 0, "seq": 3,
    }
    base.update(over)
    return base


def test_stale_flush_rejected_is_success_noop_with_warning(monkeypatch, caplog):
    """陈旧拒收语义锁：rowcount=0 ⇒ 返回 True（更新的代际已落库，不是故障——
    re-dirty 会让旧快照下轮再撞一次）且必须打 WARNING（机读键 stale_flush_rejected，
    LG-1 原病就是【零日志】）。"""
    monkeypatch.setattr(ledger, "_ensure_table", lambda: True)
    cur = _FakeCur(rowcount=0)
    monkeypatch.setattr(ledger, "_pool", lambda: _FakePool(cur))
    with caplog.at_level(logging.WARNING, logger="swarm.models.ledger"):
        ok = ledger._flush_row("t-stale", _row(seq=3))
    assert ok is True
    assert any("stale_flush_rejected" in r.message for r in caplog.records), \
        "陈旧写被拒必须留 WARNING 机读键（LG-1 原病=零日志静默回滚）"
    # hunter L1：夹具不能只凭构造的 rowcount 背书——代际闸必须真在发往 DB 的 SQL 里
    # （行为契约断言：闸在 SQL 文本+参数位，不在实现细节）。
    sql, params = cur.executed[-1]
    assert "WHERE task_ledger.seq < EXCLUDED.seq" in sql
    assert params[-1] == 3, "seq 必须作为末位参数真传入"


def test_flush_error_logs_warning(monkeypatch, caplog):
    """hunter M1 锁：真故障（连接/执行异常）必须 WARNING（机读键 flush_error）——
    陈旧拒收都 WARNING 了，真故障 debug 是等级倒置（批15 B-2 判据）。返回 False 不变。"""
    monkeypatch.setattr(ledger, "_ensure_table", lambda: True)

    class _BoomPool:
        def connection(self):
            raise ConnectionError("pg down")

    monkeypatch.setattr(ledger, "_pool", lambda: _BoomPool())
    with caplog.at_level(logging.WARNING, logger="swarm.models.ledger"):
        ok = ledger._flush_row("t-down", _row(seq=1))
    assert ok is False
    assert any("flush_error" in r.message for r in caplog.records), \
        "真故障必须 WARNING（机读键 flush_error），不得 debug 级静默"


def test_detach_flushes_settle_arriving_during_flush(_clean_ledger, monkeypatch):
    """hunter M2 锁（LG 族 settle-race 收口）：detach 的 flush() 锁外写库窗口内到达的
    settle 不得随 pop 丢失——末次快照必须包含它且 seq 续增。
    牙齿：夹具在第一次 _flush_row 执行中（=锁外窗口）注入 settle；没有 pop 前末次
    快照+写穿的旧实现，这笔结算会随 pop 蒸发（rows 只有 1 条且窗口结算丢失）。"""
    rows = []
    rid_holder = {}

    def _flush_with_late_settle(tid, row):
        rows.append(dict(row))
        if len(rows) == 1:
            # 快照已出、写库进行中：窗口内到达一笔 settle
            ledger.settle(rid_holder["rid"], real_in=500, real_out=150)
        return True

    monkeypatch.setattr(ledger, "_flush_row", _flush_with_late_settle)
    ledger.attach("t-race", 100000)
    rid_holder["rid"] = ledger.reserve("t-race", est_in=10, est_out=10)
    ledger.detach("t-race")
    assert len(rows) >= 2, \
        f"窗口内 settle 必须触发 pop 前末次写穿，实际只写了 {len(rows)} 次"
    assert rows[-1]["cloud_tokens_in"] >= 500 and rows[-1]["cloud_tokens_out"] >= 150, \
        f"末次写穿必须含窗口期 settle 的结算，实际 {rows[-1]}"
    assert rows[-1]["seq"] == rows[0]["seq"] + 1, "末次写穿 seq 必须续增"


def test_flush_db_error_still_redirties(_clean_ledger, monkeypatch):
    """反向锁：真故障（_flush_row 返回 False）必须照旧 re-dirty 下轮重试——
    「陈旧拒收算成功」绝不容许把失败处理翻成假成功。"""
    monkeypatch.setattr(ledger, "_flush_row", lambda *a, **k: False)
    ledger.attach("t-err", 1000)
    ledger.flush()
    e = ledger._entries.get("t-err")
    assert e is not None and e.dirty is True, "落库失败必须保 dirty 下轮重试"


@pytest.mark.needs_service("pg")
def test_stale_write_rejected_on_real_pg(caplog):
    """★真 PG 行为锁（CI 硬失败兜底，本机有 PG 时也真跑）★：LG-1 场景端到端——
    先落新代际 seq=2（2300/700），再写旧代际 seq=1（800/200）：必须返回 True
    （无-op）+ WARNING，且 DB 行保持 seq=2 的值不被回滚。摘掉 SQL 的
    `WHERE task_ledger.seq < EXCLUDED.seq` 本锁必红（旧病复发）。"""
    import uuid
    tid = f"lg1-probe-{uuid.uuid4().hex[:12]}"
    try:
        # seq 列由版本化迁移管辖（P0-C，hunter HIGH 整改）：测试显式应用 v7 可调用，
        # 顺带锁「迁移本身幂等可用」这一接线事实。
        from swarm.infra.migrations import runner as _mig
        with ledger._pool().connection() as conn:
            _mig._migration_v7_ledger_seq(conn)
        assert ledger._flush_row(tid, _row(
            seq=2, cloud_tokens_in=2300, cloud_tokens_out=700)) is True
        with caplog.at_level(logging.WARNING, logger="swarm.models.ledger"):
            assert ledger._flush_row(tid, _row(
                seq=1, cloud_tokens_in=800, cloud_tokens_out=200)) is True
        with ledger._pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT cloud_tokens_in, cloud_tokens_out, seq "
                    "FROM task_ledger WHERE task_id=%s", (tid,))
                got = cur.fetchone()
        assert got is not None, "新代际写入后行必须存在"
        assert (int(got[0]), int(got[1]), int(got[2])) == (2300, 700, 2), \
            f"旧代际快照把 DB 回滚了（LG-1 复发）: {got}"
        assert any("stale_flush_rejected" in r.message for r in caplog.records)
    finally:
        try:
            with ledger._pool().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM task_ledger WHERE task_id=%s", (tid,))
        except Exception:  # noqa: BLE001 — 清理失败不掩盖断言结果
            pass
