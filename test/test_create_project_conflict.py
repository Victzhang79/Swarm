"""create_project path 冲突语义回归（P1-23 → D16 演进）。

历史：P1-23 时代 path 冲突走 ON CONFLICT DO UPDATE 合并 config——但这正是 D16 坐实的
跨用户项目劫持破口（任何持 project:create 者提交已存在 path 即静默改写受害项目
name/description/config 并拿到完整项目行）。

D16 治本后（默认拒绝）：store 层 path 冲突【不改写既存行】，抛 ProjectPathConflictError
（携带既存项目行），成员幂等/403 的授权决策上移到路由层。本测试钉住新语义。

触真实 PG，_test_ 前缀隔离 + try/finally 清理。需本地 PG。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from threading import Barrier
import uuid

import psycopg
import pytest

from swarm.config.settings import DatabaseConfig
from swarm.project import store as project_store
from swarm.project.store import (
    ProjectPathConflictError,
    ProjectPathLockTimeoutError,
    create_project,
    ensure_tables,
    get_project,
    normalize_project_path,
)




# ★#29-4 T-7★ 原为 `pytestmark = pytest.mark.skipif(not _pg_available(), …)`：
# 连库动作在**装饰器实参**里 ⇒ import(collection) 期求值一次，PG 抖一下就把整个
# 文件降级为 skip 而 CI 照绿。改用 `needs_service` 标记后判定推迟到 runtest setup，
# 且缺席后果由 SWARM_TEST_REQUIRE_SERVICES 决定（CI 硬失败 / 本地可见 skip）。
# 判定实现在 test/conftest.py::pytest_runtest_setup。
pytestmark = pytest.mark.needs_service("pg")

_PATH = f"/tmp/_test_p1_23_{uuid.uuid4().hex[:8]}"
_ID_A = f"_test_p1_23_a_{uuid.uuid4().hex[:8]}"
_ID_B = f"_test_p1_23_b_{uuid.uuid4().hex[:8]}"


def _cleanup():
    with psycopg.connect(DatabaseConfig().postgres_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM projects WHERE path = %s", (normalize_project_path(_PATH),))


def _cleanup_ids(*project_ids: str) -> None:
    with psycopg.connect(DatabaseConfig().postgres_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM projects WHERE id = ANY(%s)", (list(project_ids),))


def _cleanup_empty_dirs(*paths: str) -> None:
    for path in sorted({normalize_project_path(p) for p in paths}, key=len, reverse=True):
        try:
            os.rmdir(path)
        except (FileNotFoundError, OSError):
            pass


def test_create_project_path_conflict_raises_without_mutation():
    ensure_tables()
    try:
        first = create_project(_ID_A, "proj-a", _PATH, description="d1", config={"x": 1})
        assert first["id"] == _ID_A
        assert first["config"] == {"x": 1}

        # 相同 path、不同 id、新 config → D16：拒绝且既存行【一字不改】，
        # 冲突信号携带既存项目行（供路由做成员幂等/403 决策）。
        with pytest.raises(ProjectPathConflictError) as exc_info:
            create_project(_ID_B, "proj-b", _PATH, description="d2", config={"y": 2})
        assert exc_info.value.existing["id"] == _ID_A

        after = get_project(_ID_A)
        assert after["name"] == "proj-a", "冲突不得改写既存项目 name（D16 劫持破口）"
        assert after["description"] == "d1", "冲突不得改写 description"
        assert after["config"] == {"x": 1}, "冲突不得合并/改写 config"
    finally:
        _cleanup()


def test_create_project_rejects_ancestor_and_descendant_paths():
    """存储真相源拒绝双向目录重叠，不依赖路由预检。"""
    ensure_tables()
    token = uuid.uuid4().hex[:8]
    victim_id = f"_test_overlap_victim_{token}"
    ancestor_id = f"_test_overlap_ancestor_{token}"
    descendant_id = f"_test_overlap_descendant_{token}"
    victim_path = f"/tmp/_test_overlap_{token}/team/victim"
    try:
        create_project(victim_id, "victim", victim_path)
        with pytest.raises(ProjectPathConflictError) as ancestor_conflict:
            create_project(ancestor_id, "ancestor", f"/tmp/_test_overlap_{token}/team")
        assert ancestor_conflict.value.existing["id"] == victim_id
        assert ancestor_conflict.value.exact_match is False

        with pytest.raises(ProjectPathConflictError) as descendant_conflict:
            create_project(descendant_id, "descendant", f"{victim_path}/child")
        assert descendant_conflict.value.existing["id"] == victim_id
        assert descendant_conflict.value.exact_match is False
    finally:
        _cleanup_ids(victim_id, ancestor_id, descendant_id)


def test_concurrent_overlapping_project_creation_has_single_winner():
    """两个进程等价的并发写必须由存储事务串行化，不能双双越过 route TOCTOU 预检。"""
    ensure_tables()
    token = uuid.uuid4().hex[:8]
    parent_id = f"_test_overlap_parent_{token}"
    child_id = f"_test_overlap_child_{token}"
    parent_path = f"/tmp/_test_overlap_race_{token}"
    barrier = Barrier(2)

    def attempt(project_id: str, path: str):
        barrier.wait(timeout=5)
        try:
            return "created", create_project(
                project_id, project_id, path, create_directory=True,
            )
        except ProjectPathConflictError as exc:
            return "conflict", exc

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(
                lambda args: attempt(*args),
                ((parent_id, parent_path), (child_id, f"{parent_path}/child")),
            ))
        assert sorted(kind for kind, _ in outcomes) == ["conflict", "created"]
        conflict = next(value for kind, value in outcomes if kind == "conflict")
        assert conflict.exact_match is False
        winner = next(value for kind, value in outcomes if kind == "created")
        assert os.path.isdir(winner["path"])
        if winner["path"] == normalize_project_path(parent_path):
            assert not os.path.exists(f"{parent_path}/child")
    finally:
        _cleanup_ids(parent_id, child_id)
        _cleanup_empty_dirs(f"{parent_path}/child", parent_path)


def test_project_and_creator_owner_commit_in_same_transaction():
    """真实 PG：项目行成功时，创建者 OWNER 已在同一提交中可见。"""
    from swarm.auth.store import ensure_auth_tables, get_project_member_role

    ensure_tables()
    ensure_auth_tables()
    token = uuid.uuid4().hex[:8]
    project_id = f"_test_atomic_owner_project_{token}"
    user_id = f"_test_atomic_owner_user_{token}"
    username = f"_test_atomic_owner_{token}"
    project_path = f"/tmp/_test_atomic_owner_{token}"
    with psycopg.connect(DatabaseConfig().postgres_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO swarm_users (id, username, global_role) VALUES (%s, %s, 'developer')",
                (user_id, username),
            )
    try:
        created = create_project(
            project_id, "atomic-owner", project_path,
            owner_user_id=user_id, owner_role="owner", create_directory=True,
        )
        assert created["id"] == project_id
        assert get_project_member_role(project_id, user_id) == "owner"
    finally:
        with psycopg.connect(DatabaseConfig().postgres_uri, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM swarm_project_members WHERE project_id = %s", (project_id,))
                cur.execute("DELETE FROM projects WHERE id = %s", (project_id,))
                cur.execute("DELETE FROM swarm_users WHERE id = %s", (user_id,))
        _cleanup_empty_dirs(project_path)


def test_owner_insert_failure_rolls_back_project_and_greenfield_directory():
    """真实 PG：不存在的 owner FK 令成员写失败时，项目行与本次空目录都回滚。"""
    from swarm.auth.store import ensure_auth_tables

    ensure_tables()
    ensure_auth_tables()
    token = uuid.uuid4().hex[:8]
    project_id = f"_test_atomic_owner_failure_{token}"
    project_path = f"/tmp/_test_atomic_owner_failure_{token}/project"
    try:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            create_project(
                project_id, "atomic-owner-failure", project_path,
                owner_user_id=f"missing-user-{token}", owner_role="owner",
                create_directory=True,
            )
        assert get_project(project_id) is None
        assert not os.path.exists(project_path)
    finally:
        _cleanup_ids(project_id)
        _cleanup_empty_dirs(project_path, os.path.dirname(project_path))


def test_create_project_allows_independent_sibling_paths():
    ensure_tables()
    token = uuid.uuid4().hex[:8]
    first_id = f"_test_sibling_first_{token}"
    second_id = f"_test_sibling_second_{token}"
    base = f"/tmp/_test_siblings_{token}"
    try:
        first = create_project(first_id, "first", f"{base}/first")
        second = create_project(second_id, "second", f"{base}/second")
        assert first["id"] == first_id
        assert second["id"] == second_id
    finally:
        _cleanup_ids(first_id, second_id)


def test_project_table_lock_timeout_rolls_back_without_insert(monkeypatch):
    """真实 PG 锁争用：短预算后 fail-loud，候选项目行不得落库。"""
    ensure_tables()
    token = uuid.uuid4().hex[:8]
    project_id = f"_test_lock_timeout_{token}"
    project_path = f"/tmp/_test_lock_timeout_{token}"
    monkeypatch.setattr(project_store, "_PROJECT_PATH_LOCK_TIMEOUT_MS", 100)
    try:
        with psycopg.connect(DatabaseConfig().postgres_uri, autocommit=False) as blocker:
            with blocker.cursor() as cur:
                cur.execute("LOCK TABLE projects IN SHARE ROW EXCLUSIVE MODE")
                with pytest.raises(ProjectPathLockTimeoutError):
                    create_project(project_id, "lock-timeout", project_path)
                assert get_project(project_id) is None
    finally:
        _cleanup_ids(project_id)
