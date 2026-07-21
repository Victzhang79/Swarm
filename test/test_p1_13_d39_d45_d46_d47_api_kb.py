"""P1-13 D39/D45/D46/D47 行为测试（深读登记册 2026-07-07）。

D39：kb_update_events 卡死恢复——stale processing 有界重置 pending、failed 有界重试、
     重试耗尽转 failed 可观测；claim 时落 claimed_at。
D45：模板构建脚本 health 闸门——容器起不来（cid 空）视为 FAIL 拒发；tpl 空不得 grep 空模式误报成功。
D46：上传 body DoS——Content-Length 预检 413 / 缺失 411（fail-closed）；size 缺失分块读增量校验超限即断。
D47：legacy key 常量时间比较（行为保持）；/api/sandbox/status 非 admin 裁剪内部基建字段；
     .env 读改写全程文件锁；preprocess 模型名走路由配置不写死。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent


# ── D39 ────────────────────────────────────────────────────────────


async def _pg_conn():
    import psycopg

    from swarm.config.settings import DatabaseConfig
    from swarm.infra.db import pg_connect_timeout_kwargs

    try:
        return await psycopg.AsyncConnection.connect(
            DatabaseConfig().postgres_uri, autocommit=True, **pg_connect_timeout_kwargs()
        )
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PG 不可用: {exc}")


async def test_d39_reconcile_stale_processing_and_failed_retry(monkeypatch):
    """stale processing → 有界重置 pending；重试耗尽 → failed 可观测；failed → 有界重试；
    新鲜 processing 不动。"""
    from swarm.knowledge.updater import EVENT_QUEUE_DDL, KnowledgeUpdater

    conn = await _pg_conn()
    pid = f"p1-13-d39-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("SWARM_KB_STALE_PROCESSING_SEC", "60")
    monkeypatch.setenv("SWARM_KB_FAILED_MAX_RETRIES", "3")
    try:
        async with conn.cursor() as cur:
            await cur.execute(EVENT_QUEUE_DDL)
            payload = '{"changes": []}'
            # r1: stale processing（claimed_at 1h 前）额度未尽 → pending
            await cur.execute(
                "INSERT INTO kb_update_events (project_id, event_type, payload_json, status, retry_count, claimed_at)"
                " VALUES (%s, 'push', %s::jsonb, 'processing', 0, now() - interval '1 hour') RETURNING id",
                (pid, payload))
            r1 = (await cur.fetchone())[0]
            # r2: stale processing 重试耗尽 → failed
            await cur.execute(
                "INSERT INTO kb_update_events (project_id, event_type, payload_json, status, retry_count, claimed_at)"
                " VALUES (%s, 'push', %s::jsonb, 'processing', 99, now() - interval '1 hour') RETURNING id",
                (pid, payload))
            r2 = (await cur.fetchone())[0]
            # r3: 新鲜 processing（刚 claim）→ 不动
            await cur.execute(
                "INSERT INTO kb_update_events (project_id, event_type, payload_json, status, retry_count, claimed_at)"
                " VALUES (%s, 'push', %s::jsonb, 'processing', 0, now()) RETURNING id",
                (pid, payload))
            r3 = (await cur.fetchone())[0]
            # r4: failed 额度未尽 → 有界重试回 pending
            await cur.execute(
                "INSERT INTO kb_update_events (project_id, event_type, payload_json, status, retry_count, processed_at)"
                " VALUES (%s, 'push', %s::jsonb, 'failed', 1, now() - interval '1 hour') RETURNING id",
                (pid, payload))
            r4 = (await cur.fetchone())[0]
            # r5: failed 重试耗尽 → 保持 failed
            await cur.execute(
                "INSERT INTO kb_update_events (project_id, event_type, payload_json, status, retry_count, processed_at)"
                " VALUES (%s, 'push', %s::jsonb, 'failed', 99, now() - interval '1 hour') RETURNING id",
                (pid, payload))
            r5 = (await cur.fetchone())[0]

        updater = KnowledgeUpdater()
        updater._conn = conn
        stats = await updater.reconcile_stuck_events()
        assert isinstance(stats, dict)

        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, status, retry_count FROM kb_update_events WHERE project_id=%s", (pid,))
            rows = {r[0]: (r[1], r[2]) for r in await cur.fetchall()}
        assert rows[r1][0] == "pending" and rows[r1][1] == 1, f"stale processing 未重置: {rows[r1]}"
        assert rows[r2][0] == "failed", f"耗尽 processing 未转 failed: {rows[r2]}"
        assert rows[r3][0] == "processing", f"新鲜 processing 被误重置: {rows[r3]}"
        assert rows[r4][0] == "pending" and rows[r4][1] == 2, f"failed 未有界重试: {rows[r4]}"
        assert rows[r5][0] == "failed", f"耗尽 failed 被无界重试: {rows[r5]}"
    finally:
        try:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM kb_update_events WHERE project_id=%s", (pid,))
        finally:
            await conn.close()


async def test_d39_claim_stamps_claimed_at(monkeypatch):
    """出队置 processing 时必须落 claimed_at——staleness 才是【处理时长】而非入队龄。"""
    from swarm.knowledge.updater import EVENT_QUEUE_DDL, KnowledgeUpdater

    conn = await _pg_conn()
    pid = f"p1-13-d39c-{uuid.uuid4().hex[:8]}"
    try:
        async with conn.cursor() as cur:
            await cur.execute(EVENT_QUEUE_DDL)
            await cur.execute(
                "INSERT INTO kb_update_events (project_id, event_type, payload_json) VALUES (%s, 'push', %s::jsonb) RETURNING id",
                (pid, '{"changes": [], "metadata": {}}'))
            rid = (await cur.fetchone())[0]

        updater = KnowledgeUpdater()
        updater._conn = conn

        async def _ok(event):
            return {"errors": []}

        updater.handle_event = _ok
        await updater.process_pending_events(batch_size=5)
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status, claimed_at FROM kb_update_events WHERE id=%s", (rid,))
            status, claimed_at = await cur.fetchone()
        assert status == "done"
        assert claimed_at is not None, "claim 未落 claimed_at（D39 回归）"
    finally:
        try:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM kb_update_events WHERE project_id=%s", (pid,))
        finally:
            await conn.close()


# ── D45 ────────────────────────────────────────────────────────────


_SCRIPT = _REPO / "cube-templates" / "build-and-create-templates.sh"


def _write_stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(0o755)


def _run_template_script(tmp_path: Path, *, run_fail: bool) -> tuple[str, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    call_log = tmp_path / "calls.log"
    call_log.write_text("")
    _write_stub(bin_dir, "docker", f"""
echo "docker $1" >> "{call_log}"
case "$1" in
  build) exit 0;;
  run) {"exit 1" if run_fail else 'echo cid123'};;
  port) echo "49983/tcp -> 0.0.0.0:12345";;
  push) exit 0;;
  rm) exit 0;;
  inspect) exit 0;;
esac
exit 0
""")
    _write_stub(bin_dir, "curl", "exit 0\n")
    _write_stub(bin_dir, "sleep", "exit 0\n")
    _write_stub(bin_dir, "cubemastercli", f"""
echo "cubemastercli $1 $2" >> "{call_log}"
if [[ "$1 $2" == "template create-from-image" ]]; then echo "submitted (no id echoed)"; fi
if [[ "$1 $2" == "template list" ]]; then echo "tpl-deadbeef READY"; fi
exit 0
""")
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["NODE"] = "1.2.3.4"
    env["REGISTRY"] = "reg.example:5000"  # 非 localhost：跳过本地 registry 自启块
    r = subprocess.run(
        ["bash", str(_SCRIPT)], capture_output=True, text=True, timeout=120, env=env,
    )
    assert r.returncode == 0, f"脚本异常退出 rc={r.returncode}\n{r.stdout}\n{r.stderr}"
    return r.stdout + r.stderr, call_log


def test_d45_container_not_starting_blocks_publish(tmp_path):
    """cid 为空（docker run 失败=最坏信号）→ 必须视为 health FAIL 拒发：不 push、不 create 模板。"""
    out, call_log = _run_template_script(tmp_path, run_fail=True)
    calls = call_log.read_text()
    assert "docker push" not in calls, "容器起不来仍 push 坏镜像（D45 回归）"
    assert "create-from-image" not in calls, "容器起不来仍 create 模板（D45 回归）"
    # RESULT 行必须是显式失败标记，不是空成功
    for line in out.splitlines():
        if line.startswith("RESULT "):
            assert "失败" in line, f"起不来的镜像未标失败: {line!r}"


def test_d45_empty_tpl_id_reports_failure_not_success(tmp_path):
    """create-from-image 输出解析不到 tpl-id → 不得 grep 空模式匹配所有行误报 READY 成功。"""
    import re

    out, _ = _run_template_script(tmp_path, run_fail=False)
    result_lines = [line for line in out.splitlines() if line.startswith("RESULT ")]
    assert result_lines, "脚本未输出 RESULT 行"
    for line in result_lines:
        m = re.search(r"exec=(\S+)", line)
        assert m, f"tpl 为空被当成功输出空 template_id: {line!r}"
        assert "失败" in m.group(1) or m.group(1).startswith("<"), f"空 tpl 未标失败: {line!r}"


# ── D46 ────────────────────────────────────────────────────────────


def test_d46_content_length_precheck_413(monkeypatch):
    """超过全局 body 上限的请求在解析 multipart 前即 413。"""
    import importlib

    from fastapi.testclient import TestClient

    monkeypatch.setenv("SWARM_RATELIMIT_DISABLED", "1")
    monkeypatch.setenv("SWARM_UPLOAD_MAX_BODY_BYTES", "2048")
    app = importlib.import_module("swarm.api.app").app
    client = TestClient(app)
    body = b"x" * 8192
    resp = client.post(
        "/api/uploads", content=body,
        headers={"content-type": "multipart/form-data; boundary=xx"},
    )
    assert resp.status_code == 413, f"超限 body 未被预检拒绝: {resp.status_code} {resp.text[:200]}"


def test_d46_missing_content_length_rejected():
    """无 Content-Length（chunked）→ fail-closed 411（不承担无界流式解析）。"""
    from fastapi import HTTPException

    from swarm.api.routers.upload import _enforce_body_limit

    class _Req:
        headers: dict = {}

    with pytest.raises(HTTPException) as ei:
        _enforce_body_limit(_Req())
    assert ei.value.status_code == 411


def test_d46_body_limit_env_invalid_falls_back_default(monkeypatch):
    from swarm.api.routers.upload import MAX_TOTAL_BYTES, _max_body_bytes

    monkeypatch.setenv("SWARM_UPLOAD_MAX_BODY_BYTES", "not-a-number")
    assert _max_body_bytes() > MAX_TOTAL_BYTES  # 回退默认（总上限+封包余量）


async def test_d46_incremental_read_stops_at_limit():
    """size 缺失（谎报）时分块读增量校验：超限即断，不整文件读进内存。"""
    from swarm.api.routers.upload import _read_upload_limited

    class _Item:
        def __init__(self):
            self.reads = 0

        async def read(self, n=-1):
            self.reads += 1
            return b"x" * (1024 * 1024)  # 每次 1MB，永不 EOF（模拟巨型文件）

    item = _Item()
    content, overflow = await _read_upload_limited(item, 2 * 1024 * 1024, 60 * 1024 * 1024)
    assert overflow is True and content is None
    assert item.reads <= 4, f"超限后仍继续读整个文件: reads={item.reads}"


async def test_d46_incremental_read_ok_under_limit():
    from swarm.api.routers.upload import _read_upload_limited

    class _Item:
        def __init__(self):
            self._chunks = [b"a" * 100, b"b" * 50, b""]

        async def read(self, n=-1):
            return self._chunks.pop(0)

    content, overflow = await _read_upload_limited(_Item(), 1024, 4096)
    assert overflow is False and content == b"a" * 100 + b"b" * 50


# ── D47 ────────────────────────────────────────────────────────────


def test_d47a_legacy_key_behavior_parity(monkeypatch):
    """常量时间比较改造后行为保持：匹配 → legacy admin；不匹配/空 → None。
    （时序特性无法用单测观测，此测试锁行为不回归。）"""
    import swarm.api.auth as auth

    class _Cfg:
        api_key = "sekret-123"

    monkeypatch.setattr(auth, "get_config", lambda: _Cfg())
    monkeypatch.setattr(auth, "get_user_by_token", lambda tok: None)
    assert auth.resolve_user("sekret-123") is auth._LEGACY_USER
    assert auth.resolve_user("sekret-124") is None
    assert auth.resolve_user("") is None


class _FakeManager:
    active_ids: list = []

    def get_sandbox_meta(self, sid):
        return None

    def sandboxes_for_project(self, pid):
        return set()


def _call_sandbox_status(role: str):
    import importlib

    import swarm.api._shared as shared
    from swarm.api.routers.sandbox import sandbox_status
    _app = importlib.import_module("swarm.api.app")  # api/__init__ 遮蔽 app 子模块，须 importlib

    class _User:
        id = "u1"
        global_role = role
        must_change_password = False

    orig_require = shared._require_user
    orig_fetch = _app._fetch_sandbox_list_from_server
    orig_mgr = _app._get_sandbox_manager
    shared._require_user = lambda req: _User()
    _app._fetch_sandbox_list_from_server = lambda: []
    _app._get_sandbox_manager = lambda: _FakeManager()
    try:
        return asyncio.run(sandbox_status(object(), project_id=None))
    finally:
        shared._require_user = orig_require
        _app._fetch_sandbox_list_from_server = orig_fetch
        _app._get_sandbox_manager = orig_mgr


def test_d47b_sandbox_status_config_trimmed_for_non_admin():
    """非 admin（viewer/member）不得拿到 api_url/proxy_base/default_template 内部基建字段。"""
    out = _call_sandbox_status("viewer")
    cfg = out.get("config") or {}
    for leak in ("api_url", "proxy_base", "default_template"):
        assert leak not in cfg, f"非 admin 泄漏内部字段 {leak}: {cfg}"
    assert "use_for_worker" in cfg  # 布尔状态仍可见


def test_d47b_sandbox_status_config_full_for_admin():
    out = _call_sandbox_status("admin")
    cfg = out.get("config") or {}
    assert "api_url" in cfg and "default_template" in cfg


def test_d47c_env_file_lock_mutual_exclusion(tmp_path):
    """env_file_lock 跨线程互斥：持锁期间另一写者的 RMW 必须阻塞。"""
    from swarm.config.settings import env_file_lock

    env_path = tmp_path / ".env"
    env_path.write_text("A=1\n")
    entered = threading.Event()
    inside = []

    def _writer():
        with env_file_lock(env_path):
            inside.append(time.monotonic())
        entered.set()

    with env_file_lock(env_path):
        t = threading.Thread(target=_writer)
        t.start()
        time.sleep(0.3)
        assert not inside, "第二个写者未被锁阻塞"
        release_ts = time.monotonic()
    entered.wait(5)
    t.join(5)
    assert inside and inside[0] >= release_ts


def test_d47c_persist_env_updates_holds_env_lock(tmp_path, monkeypatch):
    """_persist_env_updates 的读改写必须在 env_file_lock 内——外部持锁期间不得写盘。"""
    import importlib

    import swarm.api.routers.config as cfg_mod
    from swarm.config.settings import env_file_lock
    _app = importlib.import_module("swarm.api.app")  # api/__init__ 遮蔽 app 子模块，须 importlib

    env_path = tmp_path / ".env"
    env_path.write_text("EXISTING=1\n")
    monkeypatch.setattr(_app, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cfg_mod, "_reload_with_rollback", lambda *a, **k: None)

    done = threading.Event()

    def _writer():
        cfg_mod._persist_env_updates({"SWARM_D47_TEST_KEY": "v1"}, is_admin=True)
        done.set()

    with env_file_lock(env_path):
        t = threading.Thread(target=_writer)
        t.start()
        time.sleep(0.4)
        content_while_locked = env_path.read_text()
        assert "SWARM_D47_TEST_KEY" not in content_while_locked, \
            "外部持锁期间 _persist_env_updates 仍绕锁写盘（D47c 回归）"
    done.wait(5)
    t.join(5)
    assert "SWARM_D47_TEST_KEY=v1" in env_path.read_text()
    os.environ.pop("SWARM_D47_TEST_KEY", None)


def test_d47c_update_config_endpoint_holds_env_lock(tmp_path, monkeypatch):
    """PUT /api/config 的读改写同样必须在 env_file_lock 内（登记册点名端点）。"""
    import importlib

    import swarm.api.routers.config as cfg_mod
    from swarm.config.settings import env_file_lock
    _app = importlib.import_module("swarm.api.app")  # api/__init__ 遮蔽 app 子模块，须 importlib

    env_path = tmp_path / ".env"
    env_path.write_text("EXISTING=1\n")
    monkeypatch.setattr(_app, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cfg_mod, "_require_perm", lambda *a, **k: None)
    monkeypatch.setattr(cfg_mod, "_reload_with_rollback", lambda *a, **k: None)
    monkeypatch.setattr(_app, "configure_langsmith", lambda **k: None)
    import swarm.worker.sandbox as _sb
    monkeypatch.setattr(_sb, "reset_sandbox_manager", lambda: None)

    class _Req:
        headers: dict = {}

        async def json(self):
            return {"config": {"SWARM_D47_CFG_KEY": "v2"}}

    done = threading.Event()

    def _writer():
        asyncio.run(cfg_mod.update_config(_Req()))
        done.set()

    with env_file_lock(env_path):
        t = threading.Thread(target=_writer)
        t.start()
        time.sleep(0.4)
        assert "SWARM_D47_CFG_KEY" not in env_path.read_text(), \
            "外部持锁期间 update_config 仍绕锁写盘（D47c 回归）"
    done.wait(5)
    t.join(5)
    assert "SWARM_D47_CFG_KEY=v2" in env_path.read_text()
    os.environ.pop("SWARM_D47_CFG_KEY", None)


def test_d47d_preprocess_model_names_follow_routing_config(monkeypatch):
    """preprocess LLM 调用的模型名必须来自路由配置（ModelConfig），不得写死。"""
    import openai

    captured: list = []

    class _Completions:
        def create(self, **kw):
            captured.append(kw.get("model"))

            class _Msg:
                content = "summary"

            class _Choice:
                message = _Msg()

            class _Resp:
                choices = [_Choice()]

            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        def __init__(self, **kw):
            self.chat = _Chat()

    monkeypatch.setattr(openai, "OpenAI", _Client)
    monkeypatch.setenv("SWARM_MODEL_WORKER_PRIMARY", "my-local-model-x")

    from swarm.project.preprocess import _call_local_llm_impl

    out = _call_local_llm_impl({
        "tree": "", "readme": "", "key_files": [],
        "language_breakdown": {}, "file_count": 0, "line_counts": {},
    })
    assert out == "summary"
    assert captured and captured[0] == "my-local-model-x", \
        f"preprocess 模型名未走路由配置（写死）: {captured}"


def test_d47d_preprocess_fallback_model_follows_config(monkeypatch):
    """本地端点失败 → 云端回退的模型名同样走配置（brain_primary），不得写死。"""
    import openai

    captured: list = []

    class _Completions:
        def __init__(self, fail_models):
            self._fail = fail_models

        def create(self, **kw):
            captured.append(kw.get("model"))
            if len(captured) == 1:
                raise RuntimeError("local endpoint down")

            class _Msg:
                content = "cloud-summary"

            class _Choice:
                message = _Msg()

            class _Resp:
                choices = [_Choice()]

            return _Resp()

    comps = _Completions(None)

    class _Client:
        def __init__(self, **kw):
            class _Chat:
                completions = comps

            self.chat = _Chat()

    monkeypatch.setattr(openai, "OpenAI", _Client)
    monkeypatch.setenv("SWARM_MODEL_WORKER_PRIMARY", "my-local-model-x")
    monkeypatch.setenv("SWARM_MODEL_BRAIN_PRIMARY", "my-cloud-model-y")

    from swarm.project.preprocess import _call_local_llm_impl

    out = _call_local_llm_impl({
        "tree": "", "readme": "", "key_files": [],
        "language_breakdown": {}, "file_count": 0, "line_counts": {},
    })
    assert out == "cloud-summary"
    assert captured[-1] == "my-cloud-model-y", \
        f"preprocess 云端回退模型名未走配置（写死）: {captured}"


if __name__ == "__main__":
    import pytest as _pytest

    raise SystemExit(_pytest.main([__file__, "-q", "-p", "no:warnings"]))
