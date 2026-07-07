"""P1-13 D38/D42/D43/D44 行为测试（深读登记册 2026-07-07）。

D38：选主存活校验——try_acquire 早退须看连接存活；verify_leadership 探活；app 侧 leader
     看门狗失主停调度器（防多副本双 leader 双消费）。
D42：沙箱池幽灵清理须分页拉全量存活列表（后页存活沙箱不得误判幽灵）；页数达上限 fail-closed。
D43：worker 测试文件判定与 brain shared._is_test_file_path 统一口径（latest_/contest_ 不误伤）。
D44：git add -N 占位清理放 finally——diff 异常也不得把 intent-to-add 残留共享真仓 index。
"""

from __future__ import annotations

import asyncio
import subprocess


# ── D38: coordination ──────────────────────────────────────────────


class _DeadConn:
    closed = True


class _LiveCursor:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *a, **k):
        return None

    async def fetchone(self):
        return (1,)


class _LiveConn:
    closed = False

    def cursor(self):
        return _LiveCursor()


async def test_d38_try_acquire_early_return_checks_conn_alive(monkeypatch):
    """连接已断（advisory lock 服务端已释放）+ 本地 _held 残留 + PG 不可达 →
    try_acquire 必须返回 False，不得凭旧标记谎报仍是 leader（脑裂）。"""
    from swarm.infra.coordination import PgCoordinationBackend

    be = PgCoordinationBackend("postgresql://invalid")
    be._held.add("k")
    be._conn = _DeadConn()

    async def _boom():
        raise ConnectionError("pg down")

    monkeypatch.setattr(be, "_ensure_conn", _boom)
    assert await be.try_acquire_leadership("k") is False


async def test_d38_try_acquire_early_return_ok_when_conn_alive():
    """连接存活 + 已持有 → 早退 True（会话级 advisory lock 随会话存活，无需重查）。"""
    from swarm.infra.coordination import PgCoordinationBackend

    be = PgCoordinationBackend("postgresql://invalid")
    be._held.add("k")
    be._conn = _LiveConn()
    assert await be.try_acquire_leadership("k") is True


async def test_d38_verify_leadership_dead_conn_is_lost():
    from swarm.infra.coordination import PgCoordinationBackend

    be = PgCoordinationBackend("postgresql://invalid")
    be._held.add("k")
    be._conn = _DeadConn()
    assert await be.verify_leadership("k") is False
    assert not be._held  # 失效标记被清空


async def test_d38_verify_leadership_probe_failure_is_lost():
    """连接对象自称 open 但探活查询失败（网络黑洞/服务端重启半开连接）→ 判失主。"""
    from swarm.infra.coordination import PgCoordinationBackend

    class _HalfOpenCursor(_LiveCursor):
        async def execute(self, *a, **k):
            raise ConnectionError("server closed the connection")

    class _HalfOpenConn:
        closed = False

        def cursor(self):
            return _HalfOpenCursor()

        async def close(self):
            return None

    be = PgCoordinationBackend("postgresql://invalid")
    be._held.add("k")
    be._conn = _HalfOpenConn()
    assert await be.verify_leadership("k") is False
    assert not be._held


async def test_d38_verify_leadership_alive_and_held():
    from swarm.infra.coordination import PgCoordinationBackend

    be = PgCoordinationBackend("postgresql://invalid")
    be._held.add("k")
    be._conn = _LiveConn()
    assert await be.verify_leadership("k") is True
    # 未持有的 key 恒 False
    assert await be.verify_leadership("other") is False


async def test_d38_watchdog_stops_schedulers_on_leadership_loss(monkeypatch):
    """leader 失主（PG 会话断）→ app 看门狗停调度器并回候选循环，不再双跑。"""
    import importlib

    app = importlib.import_module("swarm.api.app")
    sl = importlib.import_module("swarm.infra.scheduler_leadership")
    import swarm.brain.scheduler as brain_sched
    import swarm.knowledge.scheduler as kb_sched

    calls = {"start": 0, "stop_task": 0, "stop_kb": 0}

    async def _start():
        calls["start"] += 1

    for name in (
        "_start_memory_decay_scheduler", "_start_kb_update_scheduler",
        "_start_kb_prune_scheduler", "_start_consistency_scheduler",
        "_start_task_scheduler",
    ):
        monkeypatch.setattr(app, name, _start)

    async def _stop_task():
        calls["stop_task"] += 1

    async def _stop_kb():
        calls["stop_kb"] += 1

    monkeypatch.setattr(brain_sched, "stop_task_scheduler", _stop_task)
    monkeypatch.setattr(kb_sched, "shutdown_kb_scheduler", _stop_kb)

    class _FakeBackend:
        async def try_acquire_leadership(self, key):
            return True

        async def verify_leadership(self, key):
            return False  # 心跳即发现失主

        async def release_leadership(self, key):
            return None

        async def is_held(self, key):
            return False

    monkeypatch.setattr(sl, "_backend", _FakeBackend())
    monkeypatch.setenv("SWARM_LEADER_HEARTBEAT_SEC", "0.05")

    task = asyncio.create_task(app._run_schedulers_with_leadership())
    try:
        for _ in range(60):
            await asyncio.sleep(0.05)
            if calls["stop_task"] >= 1:
                break
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    assert calls["start"] >= 5, "leader 须启动全部调度器"
    assert calls["stop_task"] >= 1, "失主后必须停任务准入调度器（改前：启动后 return 永不校验）"
    assert calls["stop_kb"] >= 1, "失主后必须停 KB 调度器"


# ── D42: sandbox pool 幽灵清理分页 ───────────────────────────────────


class _SB:
    def __init__(self, sid):
        self.sandbox_id = sid


class _FakePaginator:
    def __init__(self, pages):
        self._pages = list(pages)

    @property
    def has_next(self):
        return bool(self._pages)

    def next_items(self):
        return self._pages.pop(0)


def _pool_obj():
    from swarm.worker.sandbox_pool import HotSandboxPool

    return object.__new__(HotSandboxPool)


def test_d42_alive_ids_paginates_all_pages(monkeypatch):
    """服务端沙箱超一页时须拉全量——否则后页存活沙箱被当幽灵剔账本且无人 kill。"""
    import e2b_code_interpreter as e2b

    monkeypatch.setattr(
        e2b.Sandbox, "list",
        staticmethod(lambda **kw: _FakePaginator([[_SB("s1")], [_SB("s2")], [_SB("s3")]])),
    )
    pool = _pool_obj()
    alive = pool._server_alive_ids()
    assert alive == {"s1", "s2", "s3"}


def test_d42_alive_ids_page_cap_fail_closed(monkeypatch):
    """分页数达安全上限仍未穷尽 → fail-closed 返回 None（本轮跳过幽灵清理），不得拿半截列表误清。"""
    import e2b_code_interpreter as e2b

    class _Endless:
        _n = 0

        @property
        def has_next(self):
            return True

        def next_items(self):
            type(self)._n += 1
            return [_SB(f"s{type(self)._n}")]

    monkeypatch.setenv("SWARM_POOL_LIST_MAX_PAGES", "3")
    monkeypatch.setattr(e2b.Sandbox, "list", staticmethod(lambda **kw: _Endless()))
    pool = _pool_obj()
    assert pool._server_alive_ids() is None


def test_d42_alive_ids_flat_list_api_still_works(monkeypatch):
    """回归护栏：旧 SDK 直接返回 .sandboxes 列表的形态不受影响。"""
    import e2b_code_interpreter as e2b

    class _Flat:
        sandboxes = [_SB("a"), _SB("b")]

    monkeypatch.setattr(e2b.Sandbox, "list", staticmethod(lambda **kw: _Flat()))
    pool = _pool_obj()
    assert pool._server_alive_ids() == {"a", "b"}


# ── D43: worker 测试文件判定统一口径 ─────────────────────────────────


def _mk_executor(scope, desc, project_path):
    from swarm.worker.executor import WorkerExecutor

    ex = object.__new__(WorkerExecutor)
    ex.effective_scope = scope

    class _St:
        description = desc

    ex.subtask = _St()
    ex.project_path = str(project_path)
    ex._log = lambda *a, **k: None
    return ex


def test_d43_latest_contest_not_stripped_as_test_files(tmp_path):
    """latest_/contest_ 前缀文件不是测试文件，未要求测试的任务不得从 scope 剔除它们；
    真测试文件（test_ 前缀段）仍剔除。与 brain shared._is_test_file_path 同口径。"""
    from swarm.types import FileScope

    scope = FileScope(
        writable=["src/latest_metrics.py", "src/service.py"],
        create_files=["src/contest_helper.py", "src/test_new.py"],
    )
    ex = _mk_executor(scope, "给服务加新指标缓存", tmp_path)
    ex._normalize_scope_create_files()
    assert "src/latest_metrics.py" in scope.writable, "latest_ 被误判测试文件剔除（D43 回归）"
    assert "src/contest_helper.py" in scope.create_files, "contest_ 被误判测试文件剔除（D43 回归）"
    assert "src/test_new.py" not in scope.create_files, "真测试文件仍须剔除"


def test_d43_go_and_spec_suffixes_covered(tmp_path):
    """统一到 brain 口径后，_test.go/.spec.js 等后缀也被识别为测试文件。"""
    from swarm.types import FileScope

    scope = FileScope(
        writable=["pkg/server.go"],
        create_files=["pkg/server_test.go", "web/app.spec.js"],
    )
    ex = _mk_executor(scope, "实现服务端点", tmp_path)
    ex._normalize_scope_create_files()
    assert scope.create_files == []
    assert scope.writable == ["pkg/server.go"]


# ── D44: add -N 占位 finally 清理 ───────────────────────────────────


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True,
    )


def _mk_diff_executor(repo, create_rel):
    from swarm.types import FileScope
    from swarm.worker.executor import WorkerExecutor

    ex = object.__new__(WorkerExecutor)
    scope = FileScope(create_files=[create_rel], writable=[])
    ex.effective_scope = scope
    ex.project_path = str(repo)
    ex._repaired_extra_paths = set()
    ex._post_sync_contents = {}
    ex._log = lambda *a, **k: None
    return ex


def test_d44_intent_to_add_cleaned_when_diff_raises(tmp_path, monkeypatch):
    """git diff 抛异常（超时）→ intent-to-add 占位必须仍被对称清理，不残留共享 index。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.py").write_text("a = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "newfile.py").write_text("x = 1\n")

    real_run = subprocess.run

    def fake_run(cmd, *a, **k):
        if isinstance(cmd, (list, tuple)) and "diff" in cmd and "--no-color" in cmd:
            raise subprocess.TimeoutExpired(cmd, 60)
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", fake_run)
    ex = _mk_diff_executor(repo, "newfile.py")
    assert ex._try_local_git_diff() is None  # 异常路径回退 difflib
    monkeypatch.undo()

    r = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--", "newfile.py"],
        capture_output=True, text=True,
    )
    assert r.stdout.strip() == "", "diff 异常后 intent-to-add 占位残留 index（D44 回归）"


def test_d44_happy_path_diff_includes_new_file_and_cleans_index(tmp_path):
    """回归护栏：正常路径 diff 含新文件全部新增行，且 diff 后 index 无 -N 残留。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.py").write_text("a = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "newfile.py").write_text("x = 1\n")

    ex = _mk_diff_executor(repo, "newfile.py")
    diff = ex._try_local_git_diff()
    assert diff is not None and "newfile.py" in diff and "+x = 1" in diff
    r = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--", "newfile.py"],
        capture_output=True, text=True,
    )
    assert r.stdout.strip() == ""


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))


def test_d44_sibling_planning_core_git_diff_cleans_intent_add(tmp_path, monkeypatch):
    """全仓 sibling 扫描命中：brain/nodes/planning_core._git_diff_for_paths 同形
    add -N → diff → reset，diff 抛异常时 reset 必须仍执行（否则占位残留真仓 index）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.py").write_text("a = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "stub.py").write_text("s = 1\n")

    from swarm.brain.nodes.planning_core import _git_diff_for_paths

    real_run = subprocess.run

    def fake_run(cmd, *a, **k):
        if isinstance(cmd, (list, tuple)) and "diff" in cmd:
            raise subprocess.TimeoutExpired(cmd, 30)
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = _git_diff_for_paths(str(repo), ["stub.py"])
    assert out == ""
    monkeypatch.undo()
    r = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--", "stub.py"],
        capture_output=True, text=True,
    )
    assert r.stdout.strip() == "", "diff 异常后 intent-to-add 占位残留 index（D44 sibling 回归）"
