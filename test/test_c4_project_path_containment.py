"""POST /api/projects 的宿主路径授权回归。

外部目录会被后续预处理读取、被 Worker 写入，因此它不是普通字符串配置，而是宿主机
资源授权。只有全局 admin 在显式开启开关后才能注册；developer 始终只能注册 workspace
内目录。测试固定走 HTTP seam，确保拒绝发生在目录创建、项目落库和成员授权之前。
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from swarm.auth.store import SwarmUser


@pytest.fixture
def project_create_api(monkeypatch, tmp_path):
    import importlib

    import swarm.api.deps as deps
    import swarm.auth.store as auth_store
    import swarm.config.settings as settings
    import swarm.infra.redis_client as redis_client

    app_module = importlib.import_module("swarm.api.app")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "get_config", lambda: SimpleNamespace(workspace_root=workspace))
    monkeypatch.setattr(
        redis_client,
        "check_project_limit",
        lambda: {"active": 0, "limit": 10, "warn": False, "message": "正常"},
    )

    class ProjectLock:
        ttl_sec = 3600

        def __init__(self, *_args):
            pass

        def acquire(self):
            return True

        def renew(self):
            return True

        def release(self):
            return None

    monkeypatch.setattr(redis_client, "ModuleLock", ProjectLock)
    monkeypatch.setattr(auth_store, "user_can_on_project", lambda user, perm, pid=None: True)

    store = MagicMock()
    def fake_create_project(**kwargs):
        # greenfield mkdir 属于 store 的原子拥有单元；route 不应提前建目录。
        if kwargs.get("create_directory"):
            Path(kwargs["path"]).mkdir(parents=True, exist_ok=True)
        return {
            "id": kwargs["project_id"],
            "name": kwargs["name"],
            "path": kwargs["path"],
        }

    store.create_project.side_effect = fake_create_project
    store.find_project_path_overlap.return_value = None
    store.claim_preprocess_slot.return_value = False
    monkeypatch.setattr(app_module, "store", store)

    set_member = MagicMock()
    monkeypatch.setattr(auth_store, "set_project_member", set_member)

    def as_role(role: str | None) -> TestClient:
        if role is not None:
            user = SwarmUser(
                id=f"u-{role}", username=role, display_name=None,
                global_role=role, must_change_password=False,
            )
            monkeypatch.setattr(deps, "get_current_user", lambda request: user)
        return TestClient(app_module.app)

    return as_role, workspace, store, set_member


def _assert_create_side_effects_absent(store, set_member) -> None:
    store.create_project.assert_not_called()
    store.claim_preprocess_slot.assert_not_called()
    set_member.assert_not_called()


def test_developer_cannot_register_external_path_even_when_switch_enabled(
    project_create_api, monkeypatch, tmp_path,
):
    as_role, _workspace, store, set_member = project_create_api
    external = tmp_path / "outside" / "existing"
    external.mkdir(parents=True)
    monkeypatch.setenv("SWARM_ALLOW_EXTERNAL_PROJECT_PATH", "true")

    response = as_role("developer").post(
        "/api/projects", json={"name": "outside", "path": str(external)},
    )

    assert response.status_code == 403, response.text
    _assert_create_side_effects_absent(store, set_member)


@pytest.mark.parametrize("alias_kind", ["symlink", "relative"])
def test_developer_cannot_bypass_external_path_with_alias(
    project_create_api, monkeypatch, tmp_path, alias_kind,
):
    as_role, workspace, store, set_member = project_create_api
    external = tmp_path / "outside" / "existing"
    external.mkdir(parents=True)
    monkeypatch.setenv("SWARM_ALLOW_EXTERNAL_PROJECT_PATH", "true")

    if alias_kind == "symlink":
        alias = workspace / "looks-inside"
        alias.symlink_to(external, target_is_directory=True)
        submitted_path = str(alias)
    else:
        monkeypatch.chdir(tmp_path)
        submitted_path = "outside/./existing/../existing"

    response = as_role("developer").post(
        "/api/projects", json={"name": alias_kind, "path": submitted_path},
    )

    assert response.status_code == 403, response.text
    _assert_create_side_effects_absent(store, set_member)


def test_developer_external_greenfield_is_rejected_before_directory_creation(
    project_create_api, monkeypatch, tmp_path,
):
    as_role, _workspace, store, set_member = project_create_api
    external = tmp_path / "outside" / "must-not-be-created"
    monkeypatch.setenv("SWARM_ALLOW_EXTERNAL_PROJECT_PATH", "true")

    response = as_role("developer").post(
        "/api/projects",
        json={"name": "outside-new", "path": str(external), "greenfield": True},
    )

    assert response.status_code == 403, response.text
    assert not external.exists()
    _assert_create_side_effects_absent(store, set_member)


@pytest.mark.parametrize("configured_value", [None, "invalid-value"])
def test_admin_external_path_requires_explicit_opt_in(
    project_create_api, monkeypatch, tmp_path, configured_value,
):
    as_role, _workspace, store, set_member = project_create_api
    external = tmp_path / "outside" / "existing"
    external.mkdir(parents=True)
    if configured_value is None:
        monkeypatch.delenv("SWARM_ALLOW_EXTERNAL_PROJECT_PATH", raising=False)
    else:
        monkeypatch.setenv("SWARM_ALLOW_EXTERNAL_PROJECT_PATH", configured_value)

    response = as_role("admin").post(
        "/api/projects", json={"name": "outside", "path": str(external)},
    )

    assert response.status_code == 403, response.text
    _assert_create_side_effects_absent(store, set_member)


def test_admin_can_register_external_path_after_explicit_opt_in(
    project_create_api, monkeypatch, tmp_path,
):
    as_role, _workspace, store, set_member = project_create_api
    external = tmp_path / "outside" / "existing"
    external.mkdir(parents=True)
    monkeypatch.setenv("SWARM_ALLOW_EXTERNAL_PROJECT_PATH", "true")

    response = as_role("admin").post(
        "/api/projects", json={"name": "outside", "path": str(external)},
    )

    assert response.status_code == 200, response.text
    assert Path(store.create_project.call_args.kwargs["path"]) == external.resolve()
    set_member.assert_not_called()


def test_rbac_disabled_anonymous_admin_still_requires_explicit_opt_in(
    project_create_api, monkeypatch, tmp_path,
):
    """RBAC 关闭时系统定义匿名调用者为 admin；外部路径仍必须由部署者显式开闸。"""
    as_role, _workspace, store, set_member = project_create_api
    external = tmp_path / "outside" / "existing"
    external.mkdir(parents=True)

    monkeypatch.delenv("SWARM_ALLOW_EXTERNAL_PROJECT_PATH", raising=False)
    denied = as_role(None).post(
        "/api/projects", json={"name": "outside", "path": str(external)},
    )
    assert denied.status_code == 403, denied.text
    store.create_project.assert_not_called()

    monkeypatch.setenv("SWARM_ALLOW_EXTERNAL_PROJECT_PATH", "true")
    allowed = as_role(None).post(
        "/api/projects", json={"name": "outside", "path": str(external)},
    )
    assert allowed.status_code == 200, allowed.text
    set_member.assert_not_called()


@pytest.mark.parametrize("reserved", ["workspace", "workdir"])
def test_developer_cannot_register_workspace_namespace_roots(
    project_create_api, monkeypatch, reserved,
):
    as_role, workspace, store, set_member = project_create_api
    workdir = workspace / "workdir"
    workdir.mkdir()
    submitted = workspace if reserved == "workspace" else workdir
    monkeypatch.setenv("SWARM_ALLOW_EXTERNAL_PROJECT_PATH", "true")

    response = as_role("developer").post(
        "/api/projects", json={"name": reserved, "path": str(submitted)},
    )

    assert response.status_code == 409, response.text
    _assert_create_side_effects_absent(store, set_member)


@pytest.mark.parametrize("relation", ["ancestor", "descendant"])
def test_existing_project_path_overlap_is_rejected_before_writes(
    project_create_api, monkeypatch, relation,
):
    as_role, workspace, store, set_member = project_create_api
    victim = workspace / "workdir" / "team" / "victim"
    victim.mkdir(parents=True)
    submitted = victim.parent if relation == "ancestor" else victim / "new-child"
    store.find_project_path_overlap.return_value = {
        "id": "victim-project", "name": "victim", "path": str(victim),
    }
    monkeypatch.delenv("SWARM_ALLOW_EXTERNAL_PROJECT_PATH", raising=False)

    response = as_role("developer").post(
        "/api/projects",
        json={"name": relation, "path": str(submitted), "greenfield": True},
    )

    assert response.status_code == 409, response.text
    if relation == "descendant":
        assert not submitted.exists(), "重叠拒绝必须发生在 greenfield mkdir 之前"
    _assert_create_side_effects_absent(store, set_member)


def test_existing_exact_project_can_only_be_reused_without_writes_by_member(
    project_create_api, monkeypatch,
):
    import swarm.auth.store as auth_store

    as_role, workspace, store, set_member = project_create_api
    existing_path = workspace / "workdir" / "existing"
    existing_path.mkdir(parents=True)
    store.find_project_path_overlap.return_value = {
        "id": "existing-project", "name": "existing", "path": str(existing_path),
    }
    monkeypatch.setattr(
        auth_store, "get_project_member_role",
        lambda project_id, user_id: "developer",
    )

    response = as_role("developer").post(
        "/api/projects", json={"name": "ignored", "path": str(existing_path)},
    )

    assert response.status_code == 200, response.text
    assert response.json()["existing"] is True
    assert response.json()["project"]["id"] == "existing-project"
    _assert_create_side_effects_absent(store, set_member)


def test_independent_sibling_projects_inside_workspace_remain_allowed(
    project_create_api, monkeypatch,
):
    as_role, workspace, store, set_member = project_create_api
    first = workspace / "workdir" / "first"
    second = workspace / "workdir" / "second"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    monkeypatch.delenv("SWARM_ALLOW_EXTERNAL_PROJECT_PATH", raising=False)
    client = as_role("developer")

    first_response = client.post(
        "/api/projects", json={"name": "first", "path": str(first)},
    )
    second_response = client.post(
        "/api/projects", json={"name": "second", "path": str(second)},
    )

    assert first_response.status_code == 200, first_response.text
    assert second_response.status_code == 200, second_response.text
    assert [call.kwargs["path"] for call in store.create_project.call_args_list] == [
        str(first.resolve()), str(second.resolve()),
    ]
    set_member.assert_not_called()
    for call in store.create_project.call_args_list:
        assert call.kwargs["owner_user_id"] == "u-developer"
        assert call.kwargs["owner_role"] == "owner"


@pytest.mark.parametrize("name", [".", ".."])
def test_greenfield_dot_names_are_rejected_before_writes(
    project_create_api, monkeypatch, name,
):
    as_role, workspace, store, set_member = project_create_api
    monkeypatch.delenv("SWARM_ALLOW_EXTERNAL_PROJECT_PATH", raising=False)

    response = as_role("developer").post(
        "/api/projects", json={"name": name, "greenfield": True},
    )

    assert response.status_code == 400, response.text
    assert not (workspace / "workdir").exists()
    _assert_create_side_effects_absent(store, set_member)


def test_developer_can_register_path_inside_workspace(project_create_api, monkeypatch):
    as_role, workspace, store, set_member = project_create_api
    inside = workspace / "existing"
    inside.mkdir()
    monkeypatch.delenv("SWARM_ALLOW_EXTERNAL_PROJECT_PATH", raising=False)

    response = as_role("developer").post(
        "/api/projects", json={"name": "inside", "path": str(inside)},
    )

    assert response.status_code == 200, response.text
    assert Path(store.create_project.call_args.kwargs["path"]) == inside.resolve()
    assert store.create_project.call_args.kwargs["owner_user_id"] == "u-developer"
    assert store.create_project.call_args.kwargs["owner_role"] == "owner"
    set_member.assert_not_called()


def test_developer_greenfield_without_path_is_created_inside_workspace(
    project_create_api, monkeypatch,
):
    as_role, workspace, store, set_member = project_create_api
    monkeypatch.delenv("SWARM_ALLOW_EXTERNAL_PROJECT_PATH", raising=False)

    response = as_role("developer").post(
        "/api/projects", json={"name": "new project", "greenfield": True},
    )

    assert response.status_code == 200, response.text
    created_path = Path(store.create_project.call_args.kwargs["path"])
    assert created_path == workspace / "workdir" / "new-project"
    assert created_path.is_dir()
    assert store.create_project.call_args.kwargs["create_directory"] is True
    assert store.create_project.call_args.kwargs["owner_user_id"] == "u-developer"
    assert store.create_project.call_args.kwargs["owner_role"] == "owner"
    set_member.assert_not_called()


def test_greenfield_directory_is_not_created_before_atomic_store_unit(
    project_create_api, monkeypatch,
):
    """路径预留必须在 store 表锁事务内，路由进入 owned 单元前目录仍不存在。"""
    as_role, workspace, store, _set_member = project_create_api
    target = workspace / "workdir" / "atomic-new"

    def atomic_create(**kwargs):
        assert kwargs["create_directory"] is True
        assert not target.exists(), "route 在取得 store 排他预留前提前创建了 greenfield 目录"
        target.mkdir(parents=True)
        return {"id": kwargs["project_id"], "name": kwargs["name"], "path": kwargs["path"]}

    store.create_project.side_effect = atomic_create
    response = as_role("developer").post(
        "/api/projects", json={"name": "atomic-new", "greenfield": True},
    )

    assert response.status_code == 200, response.text
    assert target.is_dir()


def test_concurrent_parent_child_http_creation_loser_leaves_no_directory(
    project_create_api, monkeypatch,
):
    """HTTP 并发都越过 route 预检时，store 原子真相源仍只建胜者所需目录。"""
    import swarm.auth.store as auth_store
    from swarm.project.store import ProjectPathConflictError, project_paths_overlap

    as_role, workspace, store, _set_member = project_create_api
    parent = workspace / "workdir" / "race"
    child = parent / "child"
    barrier = Barrier(2)
    lock = Lock()
    projects: list[dict] = []
    monkeypatch.setattr(auth_store, "get_project_member_role", lambda *args: None)

    def atomic_create(**kwargs):
        barrier.wait(timeout=2)
        with lock:
            conflict = next(
                (item for item in projects if project_paths_overlap(item["path"], kwargs["path"])),
                None,
            )
            if conflict is not None:
                raise ProjectPathConflictError(
                    conflict, requested_path=kwargs["path"], exact_match=False,
                )
            Path(kwargs["path"]).mkdir(parents=True, exist_ok=True)
            result = {"id": kwargs["project_id"], "name": kwargs["name"], "path": kwargs["path"]}
            projects.append(result)
            return result

    store.create_project.side_effect = atomic_create
    client = as_role("developer")

    def post(name: str, path: Path):
        return client.post(
            "/api/projects", json={"name": name, "path": str(path), "greenfield": True},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda item: post(*item), (("parent", parent), ("child", child))))

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert len(projects) == 1
    winning_path = Path(projects[0]["path"])
    assert winning_path.is_dir()
    if winning_path == parent:
        assert not child.exists(), "parent 胜出时 child loser 不得留下目录"


def test_store_member_failure_rolls_back_and_removes_only_request_created_empty_dirs(
    monkeypatch, tmp_path,
):
    """成员 INSERT 与项目 INSERT 同事务；失败时只清本请求新建的空目录。"""
    import swarm.config.settings as settings
    from swarm.project import store as project_store

    workspace = tmp_path / "workspace"
    target = workspace / "workdir" / "atomic-failure"
    executed: list[str] = []

    class Cursor:
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            executed.append(normalized)
            if "INSERT INTO swarm_project_members" in normalized:
                raise RuntimeError("member insert failed")

        def fetchall(self):
            return []

        def fetchone(self):
            return (
                "p-atomic", "atomic", str(target), "", "EMPTY", "NONE", 0.0, None,
                0, 0, {}, {}, "", None, None,
            )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Connection:
        def cursor(self):
            return Cursor()

        def transaction(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(settings, "get_config", lambda: SimpleNamespace(workspace_root=workspace))
    monkeypatch.setattr(project_store, "_get_conn", lambda conn_str=None: Connection())

    with pytest.raises(RuntimeError, match="member insert failed"):
        project_store.create_project(
            "p-atomic", "atomic", str(target), owner_user_id="u-dev",
            owner_role="owner", create_directory=True,
        )

    assert any("INSERT INTO projects" in sql for sql in executed)
    assert any("INSERT INTO swarm_project_members" in sql for sql in executed)
    assert not target.exists()
    assert not (workspace / "workdir").exists()


def test_store_failure_never_removes_preexisting_or_nonempty_directory(monkeypatch, tmp_path):
    """回滚清理不能递归删除既存目录，也不能删除期间已变为非空的目录。"""
    import swarm.config.settings as settings
    from swarm.project import store as project_store

    workspace = tmp_path / "workspace"
    preexisting = workspace / "workdir" / "preexisting"
    preexisting.mkdir(parents=True)
    sentinel = preexisting / "keep.txt"
    sentinel.write_text("keep")

    class Cursor:
        def execute(self, sql, params=None):
            if "INSERT INTO swarm_project_members" in " ".join(sql.split()):
                raise RuntimeError("member insert failed")

        def fetchall(self):
            return []

        def fetchone(self):
            return (
                "p-existing", "existing", str(preexisting), "", "EMPTY", "NONE", 0.0,
                None, 0, 0, {}, {}, "", None, None,
            )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Connection:
        cursor = lambda self: Cursor()
        transaction = lambda self: self
        __enter__ = lambda self: self
        __exit__ = lambda self, *args: False

    monkeypatch.setattr(settings, "get_config", lambda: SimpleNamespace(workspace_root=workspace))
    monkeypatch.setattr(project_store, "_get_conn", lambda conn_str=None: Connection())

    with pytest.raises(RuntimeError, match="member insert failed"):
        project_store.create_project(
            "p-existing", "existing", str(preexisting), owner_user_id="u-dev",
            owner_role="owner", create_directory=True,
        )

    assert sentinel.read_text() == "keep"


def test_store_lock_timeout_is_explicit_and_never_reaches_insert(monkeypatch, tmp_path):
    """存储公共入口在表锁超时后必须给出可识别错误，并由事务回滚而非继续 INSERT。"""
    import psycopg
    import swarm.config.settings as settings
    from swarm.project import store as project_store

    executed: list[str] = []

    class Cursor:
        def execute(self, sql, params=None):
            executed.append(" ".join(sql.split()))
            if "LOCK TABLE projects" in sql:
                raise psycopg.errors.LockNotAvailable("lock timeout")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Connection:
        def cursor(self):
            return Cursor()

        def transaction(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    workspace = tmp_path / "workspace"
    project_path = workspace / "project"
    monkeypatch.setattr(settings, "get_config", lambda: SimpleNamespace(workspace_root=workspace))
    monkeypatch.setattr(project_store, "_get_conn", lambda conn_str=None: Connection())

    with pytest.raises(project_store.ProjectPathLockTimeoutError):
        project_store.create_project("p-timeout", "timeout", str(project_path))

    assert any("lock_timeout" in sql for sql in executed)
    assert not any("INSERT INTO projects" in sql for sql in executed)


def test_project_create_api_reports_lock_timeout_without_followup_writes(
    project_create_api,
):
    from swarm.project.store import ProjectPathLockTimeoutError

    as_role, workspace, store, set_member = project_create_api
    project_path = workspace / "lock-timeout"
    project_path.mkdir()
    store.create_project.side_effect = ProjectPathLockTimeoutError(5_000)

    response = as_role("developer").post(
        "/api/projects", json={"name": "lock-timeout", "path": str(project_path)},
    )

    assert response.status_code == 503, response.text
    assert response.headers["retry-after"] == "5"
    store.claim_preprocess_slot.assert_not_called()
    set_member.assert_not_called()


@pytest.mark.asyncio
async def test_request_cancel_waits_until_project_store_write_stops(project_create_api):
    """HTTP 调用取消后，create_project 的阻塞线程必须先退出，不能成为后台遗留写者。"""
    from swarm.api.routers import project as project_router

    as_role, workspace, store, _set_member = project_create_api
    project_path = workspace / "owned"
    project_path.mkdir()
    as_role("developer")
    started = Event()
    release = Event()
    finished = Event()

    def blocking_create(**kwargs):
        assert kwargs["owner_user_id"] == "u-developer"
        assert kwargs["owner_role"] == "owner"
        started.set()
        release.wait(timeout=2)
        finished.set()
        return {"id": kwargs["project_id"], "name": kwargs["name"], "path": kwargs["path"]}

    store.create_project.side_effect = blocking_create
    task = asyncio.create_task(project_router.create_project(
        project_router.ProjectCreateRequest(name="owned", path=str(project_path)),
        request=None,
    ))
    assert await asyncio.to_thread(started.wait, 1), "未进入项目存储写入"
    task.cancel()
    try:
        await asyncio.sleep(0.05)
        assert not task.done(), "请求取消不应遗弃仍在运行的项目写线程"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_request_cancel_after_claim_still_spawns_preprocess(project_create_api, monkeypatch):
    """claim 已提交后即使请求取消，也必须完成 spawn，不能留下无执行者的 PREPROCESSING。"""
    import swarm.api.deps as deps
    from swarm.api.routers import project as project_router
    import swarm.project.preprocess as preprocess

    as_role, workspace, store, _set_member = project_create_api
    as_role("developer")
    project_path = workspace / "claim-owned"
    project_path.mkdir()
    monkeypatch.setattr(
        deps,
        "get_current_user",
        lambda request: SwarmUser(
            id="u-developer", username="developer", display_name=None,
            global_role="developer", must_change_password=False,
        ),
    )
    started = Event()
    release = Event()

    def blocking_claim(*args, **kwargs):
        started.set()
        release.wait(timeout=2)
        return True

    store.claim_preprocess_slot.side_effect = blocking_claim
    spawned = MagicMock()

    async def fake_preprocess(*_args, owned_lock, **_kwargs):
        owned_lock.release()

    def capture_spawn(coro):
        task = asyncio.create_task(coro)
        spawned(task)
        return task

    monkeypatch.setattr(preprocess, "preprocess_project", fake_preprocess)
    monkeypatch.setattr(project_router._app, "_spawn_bg", capture_spawn)
    task = asyncio.create_task(project_router.create_project(
        project_router.ProjectCreateRequest(name="claim-owned", path=str(project_path)),
        request=None,
    ))
    assert await asyncio.to_thread(started.wait, 1), "未进入预处理认领写入"
    task.cancel()
    try:
        await asyncio.sleep(0.05)
        assert not task.done(), "取消不能遗弃已开始的 claim 写线程"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    spawned.assert_called_once()


@pytest.mark.asyncio
async def test_manual_preprocess_cancel_after_claim_still_spawns(project_create_api, monkeypatch):
    """手动触发与创建自动触发共用同一不变量：claim 成功绝不能没有 spawn。"""
    from swarm.api.routers import project as project_router
    import swarm.project.preprocess as preprocess

    as_role, workspace, store, _set_member = project_create_api
    as_role("developer")
    project_path = workspace / "manual-claim"
    project_path.mkdir()
    store.get_project.return_value = {"id": "p-manual", "path": str(project_path)}
    started = Event()
    release = Event()

    def blocking_claim(*args, **kwargs):
        started.set()
        release.wait(timeout=2)
        return True

    store.claim_preprocess_slot.side_effect = blocking_claim
    spawned = MagicMock()

    async def fake_preprocess(*_args, owned_lock, **_kwargs):
        owned_lock.release()

    def capture_spawn(coro):
        task = asyncio.create_task(coro)
        spawned(task)
        return task

    monkeypatch.setattr(preprocess, "preprocess_project", fake_preprocess)
    monkeypatch.setattr(project_router._app, "_spawn_bg", capture_spawn)
    task = asyncio.create_task(project_router.trigger_preprocess("p-manual", request=None))
    assert await asyncio.to_thread(started.wait, 1), "未进入手动预处理认领写入"
    task.cancel()
    try:
        await asyncio.sleep(0.05)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    spawned.assert_called_once()


def test_manual_preprocess_lock_busy_returns_409_before_claim(project_create_api, monkeypatch):
    """合法项目锁争用不能先写 PREPROCESSING，更不能 200 后在后台置 ERROR。"""
    import swarm.infra.redis_client as redis_client

    as_role, workspace, store, _set_member = project_create_api
    project_path = workspace / "manual-busy"
    project_path.mkdir()
    store.get_project.return_value = {"id": "p-busy", "path": str(project_path)}

    class BusyLock:
        def __init__(self, *_args):
            pass

        def acquire(self):
            return False

        def release(self):
            raise AssertionError("未持锁不得释放")

    monkeypatch.setattr(redis_client, "ModuleLock", BusyLock)
    response = as_role("developer").post("/api/projects/p-busy/preprocess")

    assert response.status_code == 409, response.text
    assert "写盘" in response.json()["detail"]
    store.claim_preprocess_slot.assert_not_called()
    store.update_project.assert_not_called()


def test_auto_preprocess_lock_busy_skips_claim_without_error_write(project_create_api, monkeypatch):
    """创建后的自动路径遇到合法锁争用只跳过，不污染新项目状态。"""
    import swarm.infra.redis_client as redis_client

    as_role, workspace, store, _set_member = project_create_api
    project_path = workspace / "auto-busy"
    project_path.mkdir()

    class BusyLock:
        def __init__(self, *_args):
            pass

        def acquire(self):
            return False

        def release(self):
            raise AssertionError("未持锁不得释放")

    monkeypatch.setattr(redis_client, "ModuleLock", BusyLock)
    response = as_role("developer").post(
        "/api/projects", json={"name": "auto-busy", "path": str(project_path)},
    )

    assert response.status_code == 200, response.text
    store.claim_preprocess_slot.assert_not_called()
    store.update_project.assert_not_called()


@pytest.mark.asyncio
async def test_preprocess_spawn_cancelled_before_first_poll_recovers_claim_and_lock(
    project_create_api, monkeypatch,
):
    """child 尚未 entered 就被取消时，claim/lock 所有权仍归启动者，必须补偿。"""
    import swarm.infra.redis_client as redis_client
    from swarm.api.routers import project as project_router

    _as_role, workspace, store, _set_member = project_create_api
    project_path = workspace / "cancel-before-poll"
    project_path.mkdir()
    store.claim_preprocess_slot.return_value = True

    class TrackingLock:
        released = 0

        def __init__(self, *_args):
            pass

        def acquire(self):
            return True

        def release(self):
            type(self).released += 1

    def cancel_before_poll(coro):
        task = asyncio.create_task(coro)
        task.cancel()
        return task

    monkeypatch.setattr(redis_client, "ModuleLock", TrackingLock)
    monkeypatch.setattr(project_router._app, "_spawn_bg", cancel_before_poll)
    TrackingLock.released = 0

    with pytest.raises(RuntimeError, match="ownership handshake"):
        await project_router._claim_and_spawn_preprocess(
            "p-cancel-before-poll",
            str(project_path),
            stale_after_sec=60,
            operation_prefix="测试预处理",
        )

    assert TrackingLock.released == 1
    store.settle_cancelled_preprocess.assert_called_once_with("p-cancel-before-poll")


@pytest.mark.asyncio
async def test_preprocess_shutdown_after_entered_drains_settles_then_releases(
    project_create_api, monkeypatch,
):
    """shutdown 可重复取消，但必须等 phase/锁收尾后再持久结算 PREPROCESSING。"""
    import swarm.infra.redis_client as redis_client
    import swarm.project.preprocess as preprocess
    import swarm.project.store as project_store
    from swarm.api.routers import project as project_router

    _as_role, workspace, store, _set_member = project_create_api
    project_path = workspace / "shutdown-entered"
    project_path.mkdir()
    store.claim_preprocess_slot.return_value = True
    writer_started = Event()
    writer_finish = Event()
    settle_started = Event()
    settle_finish = Event()
    order: list[str] = []
    children: list[asyncio.Task] = []

    class TrackingLock:
        ttl_sec = 3600
        held = False

        def __init__(self, *_args):
            pass

        def acquire(self):
            if type(self).held:
                return False
            type(self).held = True
            return True

        def renew(self):
            return True

        def release(self):
            type(self).held = False
            order.append("lock-released")

    def writer():
        writer_started.set()
        writer_finish.wait(timeout=2)
        order.append("writer-returned")

    async def phases(*_args):
        await preprocess._preprocess_blocking(writer)

    def settle_cancelled(project_id):
        settle_started.set()
        assert order == ["writer-returned"]
        assert TrackingLock.held is True
        settle_finish.wait(timeout=2)
        order.append("status-error")
        return True

    def spawn(coro):
        task = asyncio.create_task(coro)
        children.append(task)
        return task

    monkeypatch.setattr(redis_client, "ModuleLock", TrackingLock)
    monkeypatch.setattr(project_store, "get_project", lambda _pid: {
        "id": _pid, "path": str(project_path), "status": "READY",
    })
    monkeypatch.setattr(preprocess, "_preprocess_project_under_lock", phases)
    monkeypatch.setattr(project_store, "settle_cancelled_preprocess", settle_cancelled)
    monkeypatch.setattr(project_router._app, "_spawn_bg", spawn)

    outcome = await project_router._claim_and_spawn_preprocess(
        "p-shutdown",
        str(project_path),
        stale_after_sec=60,
        operation_prefix="测试预处理",
    )
    assert outcome is preprocess.PreprocessStartOutcome.STARTED
    child = children[0]
    assert await asyncio.to_thread(writer_started.wait, 1)

    child.cancel()
    await asyncio.sleep(0.02)
    assert not child.done()
    writer_finish.set()
    assert await asyncio.to_thread(settle_started.wait, 1)
    assert TrackingLock("p-shutdown", "default").acquire() is False
    child.cancel()  # cleanup 期间的二次 shutdown 取消也不能遗弃 DB 结算线程
    await asyncio.sleep(0.02)
    assert not child.done()
    settle_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await child

    assert order == ["writer-returned", "status-error", "lock-released"]
    probe = TrackingLock("p-shutdown", "default")
    assert probe.acquire() is True
    probe.release()


@pytest.mark.asyncio
async def test_shutdown_error_progress_is_emitted_once_and_terminates_sse(monkeypatch):
    """双账结算出的 error progress 必须让 SSE 发出终态后立即结束。"""
    from swarm.api.routers import project as project_router

    progress = {
        "project_id": "p-shutdown",
        "phase": "error",
        "phase_progress": 0.0,
        "message": "Preprocessing cancelled during shutdown",
        "error": "preprocess_cancelled",
    }
    monkeypatch.setattr(project_router, "_require_perm", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        project_router._app,
        "store",
        SimpleNamespace(get_progress=lambda _pid: progress),
    )

    response = await project_router.stream_preprocess_progress("p-shutdown", object())
    event = await anext(response.body_iterator)
    assert event["event"] == "progress"
    assert '"phase": "error"' in event["data"]
    assert '"error": "preprocess_cancelled"' in event["data"]
    with pytest.raises(StopAsyncIteration):
        await anext(response.body_iterator)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
