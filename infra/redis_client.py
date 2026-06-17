"""Redis 平台基础设施 — 可选启用，不可用时回退内存实现。"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

_redis_client: Any = None
_redis_checked = False


def redis_enabled() -> bool:
    return os.environ.get("SWARM_REDIS_ENABLED", "false").lower() in ("1", "true", "yes")


def get_redis() -> Any | None:
    global _redis_client, _redis_checked
    if _redis_checked:
        return _redis_client
    _redis_checked = True
    if not redis_enabled():
        return None
    try:
        import redis

        from swarm.config.settings import get_config

        _redis_client = redis.from_url(get_config().db.redis_uri, decode_responses=True)
        _redis_client.ping()
        logger.info("[Redis] connected")
        return _redis_client
    except Exception as exc:
        logger.warning("[Redis] unavailable, using in-memory fallback: %s", exc)
        _redis_client = None
        return None


class ModuleLock:
    """同项目同模块互斥锁（Redis SET NX + TTL）。"""

    def __init__(self, project_id: str, module_key: str, *, ttl_sec: int = 3600):
        self.project_id = project_id
        self.module_key = module_key
        self.key = f"swarm:lock:{project_id}:{module_key}"
        self.ttl_sec = ttl_sec
        # H8 修复：token 用 uuid 而非时间戳——同一时钟刻度两进程 token 会相同，
        # 导致 B 能释放 A 持有的锁。uuid4 保证全局唯一。
        self.token = uuid.uuid4().hex
        self._held = False

    def acquire(self) -> bool:
        r = get_redis()
        if r is None:
            self._held = True
            return True
        ok = r.set(self.key, self.token, nx=True, ex=self.ttl_sec)
        self._held = bool(ok)
        return self._held

    def release(self) -> None:
        if not self._held:
            return
        r = get_redis()
        if r is None:
            self._held = False
            return
        try:
            # H8 修复：get-then-del 非原子（两步间锁可能过期被他人获取，误删他人锁）。
            # 用 Lua 脚本原子比对+删除：仅当 value==自己的 token 才删。
            _release_lua = (
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end"
            )
            r.eval(_release_lua, 1, self.key, self.token)
        except Exception as exc:
            logger.debug("[ModuleLock] release: %s", exc)
        self._held = False


def upgrade_module_lock(
    lock: ModuleLock,
    project_id: str,
    plan: dict[str, Any] | None,
) -> ModuleLock:
    """plan 产出后升级为模块级锁（失败则保留原锁）。"""
    new_key = module_key_from_plan(plan)
    if new_key == lock.module_key:
        return lock
    new_lock = ModuleLock(project_id, new_key, ttl_sec=lock.ttl_sec)
    if not new_lock.acquire():
        logger.info(
            "[ModuleLock] keep %s (upgrade to %s unavailable)",
            lock.module_key,
            new_key,
        )
        return lock
    lock.release()
    logger.info("[ModuleLock] upgraded %s → %s", lock.module_key, new_key)
    return new_lock


def module_key_from_plan(plan: dict[str, Any] | None) -> str:
    if not plan:
        return "default"
    paths: list[str] = []
    for st in plan.get("subtasks") or []:
        scope = st.get("scope") or {}
        paths.extend(scope.get("writable") or [])
    if not paths:
        return "default"
    first = paths[0].replace("\\", "/")
    parts = first.split("/")
    return parts[0] if len(parts) > 1 else "root"


class TaskQueue:
    """优先级任务队列 — urgent > normal > background。

    Redis 模式：每个优先级一个 List（swarm:task_queue:urgent / :normal / :background）。
    内存 fallback：同结构三个 list。
    向后兼容：enqueue(task_id, project_id) 不传 priority 默认 normal。
    """

    # 优先级定义（从高到低）
    _PRIORITIES: list[str] = ["urgent", "normal", "background"]

    # 内存 fallback：每个优先级一个 list
    _memory: dict[str, list[str]] = {p: [] for p in _PRIORITIES}

    @staticmethod
    def enqueue(task_id: str, project_id: str, priority: str = "normal") -> None:
        """入队，priority 可选 urgent/normal/background，默认 normal。"""
        if priority not in TaskQueue._PRIORITIES:
            logger.warning("[TaskQueue] 未知优先级 %s，降级为 normal", priority)
            priority = "normal"
        r = get_redis()
        payload = json.dumps({"task_id": task_id, "project_id": project_id, "priority": priority})
        if r:
            r.rpush(f"swarm:task_queue:{priority}", payload)
        else:
            TaskQueue._memory[priority].append(payload)

    @staticmethod
    def dequeue() -> dict[str, str] | None:
        """按 urgent → normal → background 顺序出队。"""
        r = get_redis()
        if r:
            # 按优先级依次检查三个 List
            for p in TaskQueue._PRIORITIES:
                raw = r.lpop(f"swarm:task_queue:{p}")
                if raw:
                    return json.loads(raw)
            return None
        # 内存 fallback：同逻辑
        for p in TaskQueue._PRIORITIES:
            if TaskQueue._memory[p]:
                return json.loads(TaskQueue._memory[p].pop(0))
        return None

    @staticmethod
    def _clear_memory() -> None:
        """清空内存 fallback（仅测试用）。"""
        for p in TaskQueue._PRIORITIES:
            TaskQueue._memory[p].clear()


# ──────────────────────────────────────────────
# 项目数量软限制
# ──────────────────────────────────────────────

_SWARM_MAX_ACTIVE_PROJECTS: int | None = None


def get_max_active_projects() -> int:
    """读取 SWARM_MAX_ACTIVE_PROJECTS 环境变量（默认 10）。"""
    global _SWARM_MAX_ACTIVE_PROJECTS
    if _SWARM_MAX_ACTIVE_PROJECTS is None:
        _SWARM_MAX_ACTIVE_PROJECTS = int(os.environ.get("SWARM_MAX_ACTIVE_PROJECTS", "10"))
    return _SWARM_MAX_ACTIVE_PROJECTS


def check_project_limit() -> dict[str, Any]:
    """检查活跃项目数是否超过软限制。

    活跃项目 = status 非 EMPTY 的项目（即已预处理或正在处理）。
    返回 {"active": N, "limit": M, "warn": bool, "message": str}。
    需要 PG 可用；PG 不可用时返回跳过检查的结果。
    """
    limit = get_max_active_projects()
    try:
        from swarm.project.store import list_projects

        projects = list_projects()
        # 活跃项目：status != EMPTY（即已开始预处理或已完成）
        active = sum(1 for p in projects if p.get("status") != "EMPTY")
        warn = active >= limit
        msg = (
            f"活跃项目数 ({active}) 已达软限制 ({limit})，建议清理不活跃项目"
            if warn
            else f"活跃项目数 ({active}/{limit})，正常"
        )
        return {"active": active, "limit": limit, "warn": warn, "message": msg}
    except Exception as exc:
        logger.debug("[check_project_limit] 无法查询项目列表: %s", exc)
        return {"active": -1, "limit": limit, "warn": False, "message": f"无法查询项目列表: {exc}"}
