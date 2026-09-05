"""Worker 共享工作树的跨进程、跨数据库故障持久隔离协议。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from swarm.infra.cancellation import run_db_blocking_owned
from swarm.project import store

logger = logging.getLogger(__name__)
_PROCESS_FENCES: dict[str, dict[str, dict[str, Any]]] = {}
_PROCESS_FENCES_LOCK = threading.Lock()
_MAX_MARKER_BYTES = 64 * 1024


def _state_dir() -> Path:
    configured = os.environ.get("SWARM_INSTANCE_STATE_DIR", "").strip()
    return (
        Path(configured).expanduser() / "workspace_quarantine"
        if configured
        else Path.home() / ".swarm" / "workspace_quarantine"
    )


def _marker_path(project_id: str) -> Path:
    """历史单记录 marker 路径；仅用于向后兼容读取/清理。"""
    digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()
    return _state_dir() / f"{digest}.json"


def _incident_marker_path(project_id: str, token: str) -> Path:
    project_digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()
    token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return _state_dir() / f"{project_digest}.{token_digest}.json"


def _local_marker_paths(project_id: str) -> list[Path]:
    legacy = _marker_path(project_id)
    pattern = f"{legacy.stem}.*.json"
    return [legacy, *sorted(legacy.parent.glob(pattern))]


def _workspace_state_spec(project_path: str) -> tuple[Path, tuple[str, ...]]:
    """返回可安全 openat 遍历的基目录与相对组件。"""
    root = Path(project_path).resolve()
    dot_git = root / ".git"
    try:
        ordinary_git_dir = stat.S_ISDIR(os.lstat(dot_git).st_mode)
    except OSError:
        ordinary_git_dir = False
    if ordinary_git_dir:
        return root, (".git", "swarm", "workspace_quarantine")
    # linked worktree 的 .git 是可指向任意位置的文本，不信任/不解析。
    # 改放工作树父目录的指纹状态区：同一树的进程共享，又不进
    # git status / allow_any 工作区枚举。
    tree_digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
    return root.parent, (".swarm-workspace-state", tree_digest, "workspace_quarantine")


def _workspace_state_dir(project_path: str) -> Path:
    base, parts = _workspace_state_spec(project_path)
    return base.joinpath(*parts)


def _open_workspace_state_dir(project_path: str, *, create: bool) -> int | None:
    """逐段 O_NOFOLLOW 打开，拒绝中间目录符号链接与 TOCTOU 越界。"""
    base, parts = _workspace_state_spec(project_path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(base, flags)
    except FileNotFoundError:
        return None
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    os.close(fd)
                    return None
                raise
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _read_local_record(path: Path) -> dict[str, Any] | None:
    fd: int | None = None
    try:
        fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        payload = _read_bounded_regular_file(fd)
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        return {
            "active": True,
            "token": "",
            "project_path": "",
            "errors": [f"worker_quarantine_local_read_failed:{type(exc).__name__}"],
            "_sources": ["local"],
            "_local_marker": str(path),
        }
    finally:
        if fd is not None:
            os.close(fd)
    recovery_token = "invalid-" + hashlib.sha256(payload).hexdigest()
    try:
        raw = json.loads(payload.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {
            "active": True,
            "token": recovery_token,
            "project_path": "",
            "errors": [f"worker_quarantine_local_invalid:{type(exc).__name__}"],
            "_sources": ["local"],
            "_local_marker": str(path),
        }
    if not isinstance(raw, dict) or raw.get("active") is not True:
        return {
            "active": True,
            "token": recovery_token,
            "project_path": "",
            "errors": ["worker_quarantine_local_invalid"],
            "_sources": ["local"],
            "_local_marker": str(path),
        }
    return {**raw, "_sources": ["local"], "_local_marker": str(path)}


def _read_bounded_regular_file(fd: int) -> bytes:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("quarantine marker 不是普通文件")
    if info.st_size > _MAX_MARKER_BYTES:
        raise ValueError("quarantine marker 超过尺寸上限")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, min(65536, _MAX_MARKER_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_MARKER_BYTES:
            raise ValueError("quarantine marker 超过尺寸上限")
    return b"".join(chunks)


def _read_local(project_id: str) -> dict[str, Any] | None:
    records = [
        record
        for path in _local_marker_paths(project_id)
        if (record := _read_local_record(path)) is not None
    ]
    if not records:
        return None
    records.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("token") or "")))
    return {**records[0], "_pending_count": len(records)}


def _write_marker(record: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temp, flags, 0o600)
    try:
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
        remaining = memoryview(payload)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("写入 Worker quarantine 临时标记时无进展")
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(temp, path)
        # rename 只保证命名空间原子替换；目录元数据同步后才能把
        # “已返回成功”作为跨崩溃持久围栏的证据。
        dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        dir_fd = os.open(path.parent, dir_flags)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        # replace 前的任意失败不得留下无主 tmp；replace 已成功时
        # temp 已不存在，missing_ok 保持幂等。
        temp.unlink(missing_ok=True)
        raise


def _write_local(record: dict[str, Any], project_id: str) -> None:
    _write_marker(record, _incident_marker_path(project_id, str(record["token"])))


def _write_workspace(record: dict[str, Any], project_id: str, project_path: str) -> None:
    token_digest = hashlib.sha256(str(record["token"]).encode("utf-8")).hexdigest()
    project_digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()
    name = f"{project_digest}.{token_digest}.json"
    fd = _open_workspace_state_dir(project_path, create=True)
    if fd is None:
        raise OSError("Worker workspace quarantine 共享状态目录不存在")
    temp = f".{name}.{uuid.uuid4().hex}.tmp"
    file_fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(temp, flags, 0o600, dir_fd=fd)
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
        remaining = memoryview(payload)
        while remaining:
            written = os.write(file_fd, remaining)
            if written <= 0:
                raise OSError("写入 Worker workspace quarantine 无进展")
            remaining = remaining[written:]
        os.fsync(file_fd)
        os.close(file_fd)
        file_fd = None
        os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    except BaseException:
        if file_fd is not None:
            os.close(file_fd)
        try:
            os.unlink(temp, dir_fd=fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(fd)


def _clear_local(project_id: str, expected_token: str) -> bool:
    for path in _local_marker_paths(project_id):
        current = _read_local_record(path)
        if current is not None and current.get("token") == expected_token:
            path.unlink(missing_ok=True)
            dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            return True
    return True


def _read_workspace(project_id: str, project_path: str) -> dict[str, Any] | None:
    fd = _open_workspace_state_dir(project_path, create=False)
    if fd is None:
        return None
    digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest() + "."
    records: list[dict[str, Any]] = []
    try:
        for name in sorted(os.listdir(fd)):
            if not (name.startswith(digest) and name.endswith(".json")):
                continue
            recovery_token = "invalid-" + hashlib.sha256(name.encode("utf-8")).hexdigest()
            try:
                file_fd = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_NONBLOCK", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=fd,
                )
                try:
                    payload = _read_bounded_regular_file(file_fd)
                finally:
                    os.close(file_fd)
                recovery_token = "invalid-" + hashlib.sha256(payload).hexdigest()
                raw = json.loads(payload.decode("utf-8"))
                if not isinstance(raw, dict) or raw.get("active") is not True:
                    raise ValueError("invalid record")
                records.append({**raw, "_sources": ["workspace"], "_workspace_name": name})
            except Exception as exc:  # noqa: BLE001
                records.append({
                    "active": True,
                    "token": recovery_token,
                    "project_path": str(Path(project_path).resolve()),
                    "errors": [f"worker_quarantine_workspace_invalid:{type(exc).__name__}"],
                    "_sources": ["workspace"],
                    "_workspace_name": name,
                })
    finally:
        os.close(fd)
    if not records:
        return None
    records.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("token") or "")))
    return {
        **records[0],
        "_sources": ["workspace"],
        "_pending_count": len(records),
    }


def _clear_workspace(project_id: str, project_path: str, expected_token: str) -> bool:
    current = _read_workspace(project_id, project_path)
    if current is None or current.get("token") != expected_token:
        return True
    name = str(current.get("_workspace_name") or "")
    if not name or "/" in name or name in (".", ".."):
        return False
    fd = _open_workspace_state_dir(project_path, create=False)
    if fd is None:
        return True
    try:
        os.unlink(name, dir_fd=fd)
        os.fsync(fd)
        return True
    except FileNotFoundError:
        return True
    finally:
        os.close(fd)


def _redis_key(project_id: str) -> str:
    digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()
    return f"swarm:worker-quarantine:{digest}"


def _read_redis(project_id: str) -> dict[str, Any] | None:
    from swarm.infra.redis_client import get_redis, redis_enabled

    client = get_redis()
    if client is None:
        if redis_enabled():
            raise RuntimeError("Redis quarantine 后端已启用但当前不可用")
        return None
    raw_records = client.hgetall(_redis_key(project_id)) or {}
    records: list[dict[str, Any]] = []
    for token, payload in raw_records.items():
        try:
            record = json.loads(payload)
            if not isinstance(record, dict) or record.get("active") is not True:
                raise ValueError("invalid record")
            payload_token = str(record.get("token") or "")
            record = {**record, "token": str(token)}
            if payload_token != str(token):
                record["errors"] = list(record.get("errors") or []) + [
                    "worker_quarantine_redis_token_mismatch"
                ]
        except Exception as exc:  # noqa: BLE001
            record = {
                "active": True,
                "token": str(token),
                "project_path": "",
                "errors": [f"worker_quarantine_redis_invalid:{type(exc).__name__}"],
            }
        records.append({**record, "_sources": ["redis"]})
    if not records:
        return None
    records.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("token") or "")))
    return {**records[0], "_pending_count": len(records)}


def _write_redis(record: dict[str, Any], project_id: str) -> bool:
    from swarm.infra.redis_client import get_redis

    client = get_redis()
    if client is None:
        return False
    client.hset(
        _redis_key(project_id),
        str(record["token"]),
        json.dumps(record, ensure_ascii=False, sort_keys=True),
    )
    return True


def _clear_redis(project_id: str, expected_token: str) -> bool:
    from swarm.infra.redis_client import get_redis

    client = get_redis()
    if client is None:
        return False
    client.hdel(_redis_key(project_id), expected_token)
    return True


def _merge_visible_incident(
    records: list[dict[str, Any]], read_errors: list[str]
) -> dict[str, Any] | None:
    if not records:
        if read_errors:
            raise RuntimeError("Worker quarantine 后端读取失败: " + "; ".join(read_errors))
        return None
    records.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("token") or "")))
    chosen_token = str(records[0].get("token") or "")
    same = [item for item in records if str(item.get("token") or "") == chosen_token]
    chosen = same[0]
    errors = [
        str(error)
        for item in same
        for error in (item.get("errors") or [])
        if error
    ] + read_errors
    sources = list(dict.fromkeys(
        source for item in same for source in (item.get("_sources") or [])
    ))
    pending_count = max(
        max(int(item.get("_pending_count") or 1) for item in records),
        len({str(item.get("token") or "") for item in records}),
    )
    result = {
        **chosen,
        "errors": list(dict.fromkeys(errors)),
        "_sources": sources,
        "_pending_count": pending_count,
        "pending_incidents": pending_count,
    }
    local = next((item for item in same if "local" in (item.get("_sources") or [])), None)
    if local is not None:
        result["_local_marker"] = local.get("_local_marker")
    return result


def _load_sync(project_id: str, project_path: str | None = None) -> dict[str, Any] | None:
    local = _read_local(project_id)
    with _PROCESS_FENCES_LOCK:
        process_records = [
            {**record, "_sources": ["process"]}
            for record in _PROCESS_FENCES.get(project_id, {}).values()
        ]
    records = ([local] if local is not None else []) + process_records
    read_errors: list[str] = []
    if project_path:
        try:
            workspace = _read_workspace(project_id, project_path)
        except Exception as exc:  # noqa: BLE001
            workspace = None
            read_errors.append(f"quarantine_workspace_read_failed:{type(exc).__name__}")
        if workspace is not None:
            records.append(workspace)
    try:
        database = store.get_worker_workspace_quarantine(project_id)
    except Exception as exc:  # noqa: BLE001
        database = None
        read_errors.append(f"quarantine_db_read_failed:{type(exc).__name__}")
    if database is not None:
        records.append({**database, "_sources": ["db"]})
    try:
        redis_record = _read_redis(project_id)
    except Exception as exc:  # noqa: BLE001
        redis_record = None
        read_errors.append(f"quarantine_redis_read_failed:{type(exc).__name__}")
    if redis_record is not None:
        records.append(redis_record)
    return _merge_visible_incident(records, read_errors)


def persist_workspace_quarantine_sync(
    project_id: str, project_path: str, errors: list[str]
) -> dict[str, Any]:
    """每次回滚失败追加独立 incident；PG/Redis 至少一处全局可见。"""
    token = uuid.uuid4().hex
    record = {
        "active": True,
        "token": token,
        "project_path": str(Path(project_path).resolve()),
        "errors": list(dict.fromkeys(str(error) for error in errors if error)),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    # 在任何可失败 IO 之前先装载进程围栏：并行模块 Worker 不得
    # 在 local/Redis/PG 写入窗口中观测到“无隔离态”。全局后端确认后只移除
    # 本 token，不影响其他并行 incident。
    with _PROCESS_FENCES_LOCK:
        _PROCESS_FENCES.setdefault(project_id, {})[token] = dict(record)
    failures: list[str] = []
    sources: list[str] = []
    try:
        _write_local(record, project_id)
        sources.append("local")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"local:{type(exc).__name__}:{exc}")
    try:
        _write_workspace(record, project_id, project_path)
        sources.append("workspace")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"workspace:{type(exc).__name__}:{exc}")
    try:
        if _write_redis(record, project_id):
            sources.append("redis")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"redis:{type(exc).__name__}:{exc}")
    try:
        from swarm.infra.db import owned_db_timeout

        with owned_db_timeout(5.0):
            store.set_worker_workspace_quarantine(
                project_id,
                project_path,
                errors,
                token=token,
                created_at=record["created_at"],
            )
        sources.append("db")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"db:{type(exc).__name__}:{exc}")
    # Redis 是 DB 短暂故障期的跨实例桥接，但它是可选后端，可被
    # 运维停用/清空，不能单独作为“持久隔离已成功”的证明。只有 PG
    # 确认写入才能移除进程围栏并向调用方返回成功；否则即使
    # local/Redis 已落地也要 fail-loud，等 PG 恢复后重试/对账。
    if "db" not in sources:
        raise RuntimeError(
            "Worker quarantine PG 耐久持久化失败"
            + (": " + "; ".join(failures) if failures else "")
        )
    with _PROCESS_FENCES_LOCK:
        project_fences = _PROCESS_FENCES.get(project_id)
        if project_fences is not None:
            project_fences.pop(token, None)
            if not project_fences:
                _PROCESS_FENCES.pop(project_id, None)
    if failures:
        logger.warning(
            "Worker quarantine 冗余持久化降级 project=%s sources=%s failures=%s",
            project_id,
            sources,
            failures,
        )
    return {**record, "_sources": sources, "persistence_warnings": failures}


async def load_workspace_quarantine(
    project_id: str, project_path: str
) -> dict[str, Any] | None:
    record = await run_db_blocking_owned(
        _load_sync,
        project_id,
        project_path,
        operation="读取 Worker 工作树持久隔离态",
        db_timeout_s=5.0,
    )
    if record is None:
        return None
    expected_path = str(Path(project_path).resolve())
    actual_path = str(record.get("project_path") or "")
    errors = [str(item) for item in (record.get("errors") or []) if item]
    if actual_path != expected_path:
        errors.append("quarantine_project_path_mismatch")
    if not errors:
        errors.append("worker_quarantine_config_invalid")
    return {**record, "errors": list(dict.fromkeys(errors))}


async def persist_workspace_quarantine(
    project_id: str, project_path: str, errors: list[str]
) -> dict[str, Any]:
    return await run_db_blocking_owned(
        persist_workspace_quarantine_sync,
        project_id,
        project_path,
        errors,
        operation="持久化 Worker 工作树隔离态",
        db_timeout_s=5.0,
    )


async def clear_workspace_quarantine(
    project_id: str, project_path: str, expected_token: str
) -> bool:
    """持 default ModuleLock 复读后，以 token CAS 同时清除 DB 与本机围栏。"""
    from swarm.infra.redis_client import ModuleLock

    lock = ModuleLock(project_id, "default")
    if not lock.acquire():
        return False
    try:
        current = await load_workspace_quarantine(project_id, project_path)
        if current is None or current.get("token") != expected_token:
            return False
        if any(
            str(e).startswith((
                "quarantine_db_read_failed:",
                "quarantine_redis_read_failed:",
                "quarantine_workspace_read_failed:",
            ))
            for e in current["errors"]
        ):
            raise RuntimeError("全局隔离态未确认，拒绝解封")
        sources = set(current.get("_sources") or [])
        # local-first / DB-last：任何本机清理失败都保留全局 DB 围栏，避免其它节点放行。
        if "process" in sources:
            with _PROCESS_FENCES_LOCK:
                process = _PROCESS_FENCES.get(project_id, {}).get(expected_token)
                if process is None:
                    return False
        if "local" in sources and not _clear_local(project_id, expected_token):
            return False
        if "workspace" in sources and not _clear_workspace(
            project_id, project_path, expected_token
        ):
            return False
        if "redis" in sources and not _clear_redis(project_id, expected_token):
            return False
        if "process" in sources:
            with _PROCESS_FENCES_LOCK:
                project_fences = _PROCESS_FENCES.get(project_id, {})
                project_fences.pop(expected_token, None)
                if not project_fences:
                    _PROCESS_FENCES.pop(project_id, None)
        if "db" in sources and not await run_db_blocking_owned(
            store.clear_worker_workspace_quarantine,
            project_id,
            expected_token,
            operation="CAS 清除 Worker 工作树数据库隔离态",
            db_timeout_s=5.0,
        ):
            return False
        return True
    finally:
        lock.release()
