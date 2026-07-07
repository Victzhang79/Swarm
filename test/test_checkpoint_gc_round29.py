"""round29 运维项收尾：LangGraph checkpoint 三表 GC（TTL + 孤儿 + 子图 ns 残留）。

现状（2026-07-07 实测）：checkpoints/checkpoint_blobs/checkpoint_writes 合计 **14.4 GB**、
164 线程中 38 个孤儿，且无任何 TTL/清理机制（全仓 grep 零清理路径，hunter 遗漏项#1 复核登记）。
三类可安全清理（均"永不可 resume"）：
1. 终态任务（DONE/FAILED/PARTIAL/CANCELLED）且 updated_at 早于 TTL（默认 7 天）——终态后
   checkpoint 仅剩考古价值；中断挂起态（CONFIRMING 等非终态）不清，人工闸 resume 依赖它。
2. 孤儿线程（task_records 无对应行）——任务提交即建行，无行=永不可恢复（历史 reset/删项目遗留）。
3. worker 子图 ns 残留（checkpoint_ns 'dispatch:%'）——遗漏项#1 修复前写入的垃圾，任何任务
   （含活跃任务）都不会 resume 子图 ns。

DB 集成测试（dev 库，合成行唯一前缀，finally 清理）。
"""
from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import os

import psycopg
import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
_DSN = os.environ.get("SWARM_DB_POSTGRES_URI")
pytestmark = pytest.mark.skipif(not _DSN, reason="需要 SWARM_DB_POSTGRES_URI（dev 库）")

_PFX = f"cgc-test-{uuid.uuid4().hex[:8]}"


def _seed(conn, tid: str, status: str | None, age_days: int, ns: str = "",
          record_id: str | None = None) -> None:
    """建一条合成 checkpoint 线程（可选带 task_records 行；status=None → 孤儿；
    record_id 可与 thread 分离=模拟 retry 改写 thread_id 的语义）。"""
    if status is not None:
        conn.execute(  # task_records.project_id 有 FK → 先建合成项目行（幂等）
            """INSERT INTO projects (id, name, path) VALUES (%s, %s, '/tmp/gc-test')
               ON CONFLICT (id) DO NOTHING""",
            (f"{_PFX}-proj", f"{_PFX}-proj"))
        conn.execute(
            """INSERT INTO task_records (id, project_id, description, status, created_at, updated_at)
               VALUES (%s, %s, 'gc-test', %s,
                       now() - make_interval(days => %s), now() - make_interval(days => %s))
               ON CONFLICT (id) DO NOTHING""",
            (record_id or tid, f"{_PFX}-proj", status, age_days, age_days))
        conn.execute("UPDATE task_records SET thread_id = %s WHERE id = %s",
                     (tid, record_id or tid))
    conn.execute(
        """INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata)
           VALUES (%s, %s, %s, 'msgpack', '{}', '{}')""",
        (tid, ns, f"ckpt-{uuid.uuid4().hex[:12]}"))
    conn.execute(
        """INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type, blob)
           VALUES (%s, %s, 'ch', '1', 'msgpack', %s)""", (tid, ns, b"x"))
    conn.execute(
        """INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx,
                                          channel, type, blob, task_path)
           VALUES (%s, %s, %s, 't', 0, 'ch', 'msgpack', %s, '')""",
        (tid, ns, f"ckpt-{uuid.uuid4().hex[:12]}", b"x"))


def _threads_left(conn, tids: list[str]) -> dict[str, dict[str, int]]:
    out = {}
    for tid in tids:
        out[tid] = {
            t: conn.execute(f"SELECT count(*) FROM {t} WHERE thread_id=%s", (tid,)).fetchone()[0]
            for t in ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
        }
    return out


def _cleanup(conn, tids: list[str]) -> None:
    for t in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        conn.execute(f"DELETE FROM {t} WHERE thread_id = ANY(%s)", (tids,))
    conn.execute("DELETE FROM task_records WHERE project_id = %s", (f"{_PFX}-proj",))
    conn.execute("DELETE FROM projects WHERE id = %s", (f"{_PFX}-proj",))
    conn.commit()


def test_sweep_ttl_orphan_and_subgraph_ns():
    from swarm.infra.checkpoint_gc import sweep_stale_checkpoints

    t_old_done = f"{_PFX}-old-done"       # 终态+过期 → 清
    t_new_done = f"{_PFX}-new-done"       # 终态+未过期 → 留
    t_running = f"{_PFX}-running"         # 非终态 → 留（根 ns）
    t_orphan = f"{_PFX}-orphan"           # 无 task_records → 清
    t_run_sub = f"{_PFX}-running"         # 同 running 线程的子图 ns 行 → 清（附加 seed）
    tids = [t_old_done, t_new_done, t_running, t_orphan]

    with psycopg.connect(_DSN) as conn:
        try:
            _seed(conn, t_old_done, "DONE", age_days=30)
            _seed(conn, t_new_done, "DONE", age_days=1)
            _seed(conn, t_running, "DISPATCHING", age_days=30)
            _seed(conn, t_orphan, None, age_days=30)
            # 活跃任务的子图 ns 残留（遗漏项#1 修复前的垃圾形态）
            _seed(conn, t_run_sub, "DISPATCHING", age_days=0,
                  ns="dispatch:127d7a60-b823-a6f0-503b-c4da7459d09b|3")
            conn.commit()

            stats = sweep_stale_checkpoints(ttl_days=7)
            assert stats and not stats.get("disabled"), stats

            left = _threads_left(conn, tids)
            # 终态+过期：三表全清
            assert all(v == 0 for v in left[t_old_done].values()), left[t_old_done]
            # 孤儿：三表全清
            assert all(v == 0 for v in left[t_orphan].values()), left[t_orphan]
            # 终态+未过期：保留
            assert all(v >= 1 for v in left[t_new_done].values()), left[t_new_done]
            # 非终态：根 ns 保留、子图 ns 被清 → 每表恰剩 1 行（根 ns 那份）
            assert all(v == 1 for v in left[t_running].values()), left[t_running]
            with psycopg.connect(_DSN) as c2:
                sub = c2.execute(
                    "SELECT count(*) FROM checkpoints WHERE thread_id=%s AND checkpoint_ns <> ''",
                    (t_running,)).fetchone()[0]
            assert sub == 0, "活跃任务的子图 ns 残留必须被清"
        finally:
            _cleanup(conn, tids)


def test_retried_active_task_checkpoints_survive():
    """复核 CRITICAL 回归：retry 改写 task_records.thread_id 为 "{id}-r-xxxx" 后，
    该【活跃】任务的当前 thread 绝不能被孤儿判据误删；其旧 thread(=id) 属废弃垃圾应清。"""
    from swarm.infra.checkpoint_gc import sweep_stale_checkpoints

    rec_id = f"{_PFX}-retried"
    cur_thread = f"{rec_id}-r-deadbeef"
    with psycopg.connect(_DSN) as conn:
        try:
            # 当前活跃 run（thread=id-r-xxx，task_records.thread_id 指向它）
            _seed(conn, cur_thread, "DISPATCHING", age_days=30, record_id=rec_id)
            # 旧 run 遗留（thread=id，已无任何行认领）
            _seed(conn, rec_id, None, age_days=30)
            conn.commit()

            sweep_stale_checkpoints(ttl_days=7)

            left = _threads_left(conn, [cur_thread, rec_id])
            assert all(v >= 1 for v in left[cur_thread].values()), (
                f"被重跑的活跃任务当前 thread 被误删（CRITICAL 回归）: {left[cur_thread]}"
            )
            assert all(v == 0 for v in left[rec_id].values()), (
                f"retry 废弃的旧 thread 应作孤儿清理: {left[rec_id]}"
            )
        finally:
            _cleanup(conn, [cur_thread, rec_id])


def test_sweep_disabled_by_ttl_zero():
    from swarm.infra.checkpoint_gc import sweep_stale_checkpoints

    tid = f"{_PFX}-disabled"
    with psycopg.connect(_DSN) as conn:
        try:
            _seed(conn, tid, "DONE", age_days=30)
            conn.commit()
            stats = sweep_stale_checkpoints(ttl_days=0)
            assert stats.get("disabled") is True
            left = _threads_left(conn, [tid])
            assert all(v >= 1 for v in left[tid].values()), "禁用时不得删任何行"
        finally:
            _cleanup(conn, [tid])


def test_sweep_fail_safe_on_bad_dsn():
    from swarm.infra.checkpoint_gc import sweep_stale_checkpoints

    stats = sweep_stale_checkpoints(ttl_days=7, conn_str="postgresql://nouser@127.0.0.1:1/none")
    assert stats.get("error"), "DB 不可达必须 fail-safe 返回 error 统计，绝不抛出阻断启动"


def test_sweep_fail_safe_on_config_failure(monkeypatch):
    """hunter#2 整改：get_config 异常也必须在 fail-safe 罩内（否则裸穿 _spawn_bg 静默吞点）。"""
    import swarm.config.settings as _settings
    from swarm.infra.checkpoint_gc import sweep_stale_checkpoints

    def _boom():
        raise RuntimeError("config layer down")

    monkeypatch.setattr(_settings, "get_config", _boom)
    stats = sweep_stale_checkpoints(ttl_days=7, conn_str=None)
    assert "config layer down" in (stats.get("error") or ""), stats
