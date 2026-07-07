"""P0-10 多用户隔离 D16/D19/D20/D21 治本回归（DEEP_READ_REGISTER_2026-07-07）。

D16 跨用户项目劫持：create_project path 冲突不再静默 UPDATE 受害项目，
     store 层抛 ProjectPathConflictError（路由决定成员幂等/拒绝）。
D19 改密吊销 token：update_user_password 置 token_revoked → 旧 token 立即认证失败。
D20 并发预处理竞态：claim_preprocess_slot DB CAS in-flight 守卫（stale 可重入）+
     超时取消标志让 to_thread 内的 Qdrant 写入线程自查退出、不再回写进度。
D21 级联删除补 swarm_project_members + 上传文件清理（路径校验防穿越）+ 孤儿批次 GC。

DB 测试沿用仓内既有模式（test_delete_project_cascade_12_5 / test_create_project_conflict）：
触真实 PG、_test_ 前缀隔离、try/finally 兜底清理、PG 不可达则 skip。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import psycopg
import pytest

from swarm.config.settings import DatabaseConfig


def _pg_available() -> bool:
    try:
        with psycopg.connect(DatabaseConfig().postgres_uri, connect_timeout=3):
            return True
    except Exception:
        return False


_PG_OK = _pg_available()
requires_pg = pytest.mark.skipif(not _PG_OK, reason="PG 不可达")


def _conn():
    return psycopg.connect(DatabaseConfig().postgres_uri, autocommit=True)


def _cleanup_projects(*, ids: tuple[str, ...] = (), paths: tuple[str, ...] = ()) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            for pid in ids:
                for tbl, col in (
                    ("swarm_project_members", "project_id"),
                    ("task_records", "project_id"),
                    ("preprocess_progress", "project_id"),
                    ("projects", "id"),
                ):
                    try:
                        cur.execute(f"DELETE FROM {tbl} WHERE {col} = %s", (pid,))
                    except Exception:
                        pass
            for p in paths:
                try:
                    cur.execute("DELETE FROM projects WHERE path = %s", (p,))
                except Exception:
                    pass


# ══════════════════════════════════════════════════════════════════
# D16 — create_project path 冲突：不改写、抛冲突信号
# ══════════════════════════════════════════════════════════════════

@requires_pg
def test_d16_store_path_conflict_raises_and_preserves_victim():
    from swarm.project.store import (
        ProjectPathConflictError,
        create_project,
        ensure_tables,
        get_project,
    )

    ensure_tables()
    path = f"/tmp/_test_d16_{uuid.uuid4().hex[:8]}"
    id_a = f"_test_d16_a_{uuid.uuid4().hex[:8]}"
    id_b = f"_test_d16_b_{uuid.uuid4().hex[:8]}"
    try:
        victim = create_project(id_a, "victim", path, description="secret-desc",
                                config={"sandbox_template": "victim-tpl"})
        assert victim["id"] == id_a

        # 攻击者：同 path、新 id、恶意 name/config → 必须拒绝，绝不静默 UPDATE
        with pytest.raises(ProjectPathConflictError) as exc_info:
            create_project(id_b, "attacker", path, description="pwned",
                           config={"sandbox_template": "evil-tpl"})
        assert exc_info.value.existing["id"] == id_a

        # 受害项目一字未改
        after = get_project(id_a)
        assert after["name"] == "victim"
        assert after["description"] == "secret-desc"
        assert after["config"].get("sandbox_template") == "victim-tpl"
    finally:
        _cleanup_projects(ids=(id_a, id_b), paths=(path,))


def test_d16_router_reuse_gate_is_member_or_admin(monkeypatch):
    """路由分层决策：既有项目只有 admin 或该项目成员可幂等复用，其他人拒绝。"""
    import swarm.api.routers.project as proj_router
    from swarm.auth.store import SwarmUser

    admin = SwarmUser(id="u-adm", username="a", display_name=None, global_role="admin")
    member = SwarmUser(id="u-mem", username="m", display_name=None, global_role="developer")
    outsider = SwarmUser(id="u-out", username="o", display_name=None, global_role="developer")

    def fake_role(project_id, user_id, conn_str=None):
        return "developer" if user_id == "u-mem" else None

    monkeypatch.setattr("swarm.auth.store.get_project_member_role", fake_role)
    assert proj_router._caller_may_reuse_existing_project(admin, "p1") is True
    assert proj_router._caller_may_reuse_existing_project(member, "p1") is True
    assert proj_router._caller_may_reuse_existing_project(outsider, "p1") is False


def test_d16_router_reuse_gate_fail_closed_on_db_error(monkeypatch):
    """成员查询失败 → fail-closed 拒绝（非 admin）。"""
    import swarm.api.routers.project as proj_router
    from swarm.auth.store import SwarmUser

    dev = SwarmUser(id="u-x", username="x", display_name=None, global_role="developer")

    def boom(project_id, user_id, conn_str=None):
        raise RuntimeError("db down")

    monkeypatch.setattr("swarm.auth.store.get_project_member_role", boom)
    assert proj_router._caller_may_reuse_existing_project(dev, "p1") is False


# ══════════════════════════════════════════════════════════════════
# D19 — 改密吊销 token
# ══════════════════════════════════════════════════════════════════

@requires_pg
def test_d19_password_change_revokes_existing_token():
    from swarm.auth.store import (
        create_user,
        ensure_auth_tables,
        get_user_by_token,
        update_user_password,
    )

    ensure_auth_tables()
    username = f"_test_d19_{uuid.uuid4().hex[:8]}"
    user = create_user(username=username, password="oldpw123")
    token = user.api_token
    assert token, "create_user 应返回一次性明文 token"
    try:
        assert get_user_by_token(token) is not None, "改密前 token 应有效"
        update_user_password(user.id, "newpw456")
        assert get_user_by_token(token) is None, \
            "D19 回归：改密后旧 token 仍认证通过（被盗 token 改密后仍永久有效）"
    finally:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM swarm_users WHERE id = %s", (user.id,))


# ══════════════════════════════════════════════════════════════════
# D20 — 预处理 in-flight 守卫（CAS）+ 超时取消标志
# ══════════════════════════════════════════════════════════════════

@requires_pg
def test_d20_claim_preprocess_slot_cas_and_stale_reentry():
    from swarm.project.store import (
        claim_preprocess_slot,
        create_project,
        ensure_tables,
        get_progress,
        update_project,
    )

    ensure_tables()
    pid = f"_test_d20_claim_{uuid.uuid4().hex[:8]}"
    path = f"/tmp/{pid}"
    try:
        create_project(pid, "d20", path)

        # 首次认领成功，且进度行已重置（started_at 刷新）
        assert claim_preprocess_slot(pid, stale_after_sec=3600) is True
        prog = get_progress(pid)
        assert prog is not None and prog["started_at"] is not None

        # in-flight：第二次认领被拒（守卫生效）
        assert claim_preprocess_slot(pid, stale_after_sec=3600) is False

        # 崩溃残留（PREPROCESSING 卡死超过 stale 窗口）→ 可重入，不永拒
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE projects SET updated_at = NOW() - interval '2 hours' WHERE id = %s",
                    (pid,),
                )
        assert claim_preprocess_slot(pid, stale_after_sec=3600) is True

        # 正常终态（READY/ERROR）→ 可再次触发
        update_project(pid, status="READY")
        assert claim_preprocess_slot(pid, stale_after_sec=3600) is True

        # 不存在的项目 → False（fail-closed）
        assert claim_preprocess_slot(f"_test_d20_nx_{uuid.uuid4().hex}", stale_after_sec=3600) is False
    finally:
        _cleanup_projects(ids=(pid,))


def test_d20_timeout_sets_cancel_flag_and_unregisters(monkeypatch, tmp_path):
    """超时路径：取消标志被置位（供 to_thread 线程自查退出），且事后注册表不泄漏。"""
    import swarm.project.preprocess as pp
    import swarm.project.store as pstore

    calls: list[dict] = []
    monkeypatch.setattr(pstore, "upsert_progress", lambda pid, **kw: calls.append(kw) or {})
    monkeypatch.setattr(pstore, "update_project", lambda pid, **kw: calls.append(kw) or {})
    monkeypatch.setattr(pp, "_preprocess_timeout_sec", lambda: 0.3)

    captured: dict = {}

    async def hang_scan(project_id, project_path):
        captured["event"] = pp._CANCEL_EVENTS.get(project_id)
        await asyncio.sleep(30)

    monkeypatch.setattr(pp, "_phase_scan", hang_scan)

    pid = f"_test_d20_to_{uuid.uuid4().hex[:8]}"
    asyncio.run(pp.preprocess_project(pid, str(tmp_path)))

    ev = captured.get("event")
    assert ev is not None, "运行期应注册取消事件"
    assert ev.is_set(), "D20 回归：超时后未置取消标志，to_thread 线程会继续回写进度"
    assert pid not in pp._CANCEL_EVENTS, "结束后注册表应清理（不泄漏）"
    # 超时后项目置 ERROR（原有行为不回归）
    assert any(kw.get("status") == "ERROR" for kw in calls)


def test_d20_store_vectors_exits_early_when_cancelled():
    """取消后 _store_vectors_qdrant 不再触 Qdrant、不再写进度（阶段边界自查退出）。"""
    import swarm.project.preprocess as pp

    pid = f"_test_d20_vec_{uuid.uuid4().hex[:8]}"
    ev = pp._register_cancel_event(pid)
    ev.set()
    progress_writes: list = []
    try:
        # 已取消 → 顶部自查直接返回；若走到 QdrantClient 连接（本测试环境无 Qdrant）会抛错/挂起
        pp._store_vectors_qdrant(
            pid,
            [{"name": "S", "file_path": "a.py", "start_line": 1}],
            [[0.1, 0.2]],
            2,
            progress_callback=lambda p, m: progress_writes.append((p, m)),
        )
    finally:
        pp._unregister_cancel_event(pid, ev)
    assert progress_writes == [], "取消后不得再写进度"


# ══════════════════════════════════════════════════════════════════
# D21 — 级联补 swarm_project_members + uploads 清理/GC
# ══════════════════════════════════════════════════════════════════

@requires_pg
def test_d21_delete_project_clears_member_rows():
    from swarm.auth.store import create_user, ensure_auth_tables, set_project_member
    from swarm.project.store import create_project, delete_project, ensure_tables

    ensure_tables()
    ensure_auth_tables()
    pid = f"_test_d21_mem_{uuid.uuid4().hex[:8]}"
    path = f"/tmp/{pid}"
    username = f"_test_d21_{uuid.uuid4().hex[:8]}"
    user = create_user(username=username, password="pw123456")
    try:
        create_project(pid, "d21", path)
        set_project_member(pid, user.id, "owner")

        assert delete_project(pid) is True

        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM swarm_project_members WHERE project_id = %s", (pid,)
                )
                assert cur.fetchone()[0] == 0, \
                    "D21 回归：删项目后成员行残留（污染 list_user_project_ids 白名单）"
    finally:
        _cleanup_projects(ids=(pid,))
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM swarm_users WHERE id = %s", (user.id,))


@requires_pg
def test_d21_delete_task_removes_uploads_with_traversal_guard(monkeypatch, tmp_path):
    import swarm.project.store as pstore
    from swarm.project.store import create_project, create_task, delete_task, ensure_tables

    ensure_tables()
    uploads_root = tmp_path / "uploads"
    batch = uploads_root / "batch1"
    batch.mkdir(parents=True)
    good = batch / "doc.txt"
    good.write_text("hello")
    outside = tmp_path / "outside.txt"  # uploads 根之外 → 绝不能被删
    outside.write_text("must-survive")

    monkeypatch.setattr(pstore, "_uploads_root_path", lambda: uploads_root)

    pid = f"_test_d21_up_{uuid.uuid4().hex[:8]}"
    tid = f"_test_d21_task_{uuid.uuid4().hex[:8]}"
    try:
        create_project(pid, "d21up", f"/tmp/{pid}")
        create_task(tid, pid, "t", uploaded_files=[str(good), str(outside),
                                                   str(uploads_root / ".." / "outside.txt")])

        assert delete_task(tid) is True

        assert not good.exists(), "删任务应清理其 uploads 文件"
        assert not batch.exists(), "空批次目录应一并删除"
        assert outside.exists(), "越界路径（uploads 根之外）绝不能被删（防穿越）"
    finally:
        _cleanup_projects(ids=(pid,))


@requires_pg
def test_d21_delete_task_keeps_files_still_referenced(monkeypatch, tmp_path):
    import swarm.project.store as pstore
    from swarm.project.store import create_project, create_task, delete_task, ensure_tables

    ensure_tables()
    uploads_root = tmp_path / "uploads"
    batch = uploads_root / "shared"
    batch.mkdir(parents=True)
    shared = batch / "shared.txt"
    shared.write_text("shared")
    monkeypatch.setattr(pstore, "_uploads_root_path", lambda: uploads_root)

    pid = f"_test_d21_sh_{uuid.uuid4().hex[:8]}"
    t1 = f"_test_d21_t1_{uuid.uuid4().hex[:8]}"
    t2 = f"_test_d21_t2_{uuid.uuid4().hex[:8]}"
    try:
        create_project(pid, "d21sh", f"/tmp/{pid}")
        create_task(t1, pid, "t1", uploaded_files=[str(shared)])
        create_task(t2, pid, "t2", uploaded_files=[str(shared)])

        assert delete_task(t1) is True
        assert shared.exists(), "仍被其它任务引用的上传文件不得删除（最小破坏面）"

        assert delete_task(t2) is True
        assert not shared.exists(), "最后一个引用删除后文件应清理"
    finally:
        _cleanup_projects(ids=(pid,))


@requires_pg
def test_d21_gc_orphan_upload_batches(monkeypatch, tmp_path):
    import swarm.project.store as pstore
    from swarm.project.store import (
        create_project,
        create_task,
        ensure_tables,
        gc_orphan_upload_batches,
    )

    ensure_tables()
    uploads_root = tmp_path / "uploads"
    orphan_old = uploads_root / "orphan_old"
    orphan_new = uploads_root / "orphan_new"
    referenced_old = uploads_root / "referenced_old"
    for d in (orphan_old, orphan_new, referenced_old):
        d.mkdir(parents=True)
        (d / "f.txt").write_text("x")
    old_ts = time.time() - 30 * 86400
    os.utime(orphan_old, (old_ts, old_ts))
    os.utime(referenced_old, (old_ts, old_ts))
    monkeypatch.setattr(pstore, "_uploads_root_path", lambda: uploads_root)

    pid = f"_test_d21_gc_{uuid.uuid4().hex[:8]}"
    tid = f"_test_d21_gct_{uuid.uuid4().hex[:8]}"
    try:
        create_project(pid, "d21gc", f"/tmp/{pid}")
        create_task(tid, pid, "t", uploaded_files=[str(referenced_old / "f.txt")])

        removed = gc_orphan_upload_batches(max_age_days=7)

        assert removed == 1
        assert not orphan_old.exists(), "超龄孤儿批次应被 GC"
        assert orphan_new.exists(), "未超龄目录不删（防误伤进行中的上传→建任务窗口）"
        assert referenced_old.exists(), "仍被 task_records 引用的批次绝不能删"

        # 关闭开关（<=0）→ 不删
        assert gc_orphan_upload_batches(max_age_days=0) == 0
    finally:
        _cleanup_projects(ids=(pid,))


@requires_pg
def test_d21_delete_project_cleans_task_uploads(monkeypatch, tmp_path):
    import swarm.project.store as pstore
    from swarm.project.store import create_project, create_task, delete_project, ensure_tables

    ensure_tables()
    uploads_root = tmp_path / "uploads"
    batch = uploads_root / "projbatch"
    batch.mkdir(parents=True)
    f = batch / "doc.md"
    f.write_text("x")
    monkeypatch.setattr(pstore, "_uploads_root_path", lambda: uploads_root)

    pid = f"_test_d21_pj_{uuid.uuid4().hex[:8]}"
    tid = f"_test_d21_pjt_{uuid.uuid4().hex[:8]}"
    try:
        create_project(pid, "d21pj", f"/tmp/{pid}")
        create_task(tid, pid, "t", uploaded_files=[str(f)])

        assert delete_project(pid) is True
        assert not f.exists(), "删项目应级联清理其任务的 uploads 文件"
    finally:
        _cleanup_projects(ids=(pid,))


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
