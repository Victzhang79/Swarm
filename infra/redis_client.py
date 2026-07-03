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
# A-P1-13：上次「不可用」探测的时间戳（None=从未探测/当前可用）。
# 旧实现用布尔 _redis_checked 永久锁存失败 → 启动期一次瞬时抖动会让整个进程
# 永久退化为内存锁(永不重连) → 多副本 split-brain 风险。改为带冷却的重探：
# 失败后缓存 unavailable 状态 N 秒，冷却到期下次访问重新尝试连接。
_redis_unavailable_at: float | None = None

# 重探冷却（秒）。可经环境变量覆盖；默认 30s——足够吸收瞬时抖动，又不至于
# 长时间停留在退化态。非阻塞：只是决定是否在下次访问时重试。
_REDIS_REPROBE_COOLDOWN_SEC = 30.0

# #14：ModuleLock 在 Redis 不可用时降级为进程内 no-op（无跨进程互斥）。多进程部署下这是
# split-brain 风险，须可观测。仅首次降级打一次 WARNING（避免每次 acquire 刷屏）。
_lock_fail_open_warned = False


def _warn_lock_fail_open_once() -> None:
    global _lock_fail_open_warned
    if not _lock_fail_open_warned:
        _lock_fail_open_warned = True
        logger.warning(
            "[ModuleLock] Redis 不可用 → 模块锁降级为进程内 no-op（无跨进程互斥）。"
            "单进程部署可忽略；多进程/多副本部署存在同模块并发写 split-brain 风险，"
            "请启用 Redis（SWARM_REDIS_ENABLED=true）。"
        )


def redis_enabled() -> bool:
    return os.environ.get("SWARM_REDIS_ENABLED", "false").lower() in ("1", "true", "yes")


def _reprobe_cooldown() -> float:
    try:
        return float(os.environ.get("SWARM_REDIS_REPROBE_COOLDOWN_SEC", _REDIS_REPROBE_COOLDOWN_SEC))
    except (TypeError, ValueError):
        return _REDIS_REPROBE_COOLDOWN_SEC


def _renew_transient_threshold() -> int:
    """ModuleLock.renew 连续瞬时失败到此阈值才判失锁（对抗复核 4a；默认 3，SWARM_LOCK_RENEW_TRANSIENT_MAX 可调）。"""
    try:
        return max(1, int(os.environ.get("SWARM_LOCK_RENEW_TRANSIENT_MAX", "3")))
    except (TypeError, ValueError):
        return 3


def get_redis() -> Any | None:
    global _redis_client, _redis_unavailable_at
    # 已有可用连接：直接复用。
    if _redis_client is not None:
        return _redis_client
    if not redis_enabled():
        return None
    # 上次探测失败且仍在冷却窗内：暂不重试，继续用内存兜底（非阻塞）。
    if _redis_unavailable_at is not None:
        if (time.monotonic() - _redis_unavailable_at) < _reprobe_cooldown():
            return None
        # 冷却到期：清状态，下面重新尝试连接。
    try:
        import redis

        from swarm.config.settings import get_config

        client = redis.from_url(get_config().db.redis_uri, decode_responses=True)
        client.ping()
        _redis_client = client
        _redis_unavailable_at = None
        logger.info("[Redis] connected")
        return _redis_client
    except Exception as exc:
        _redis_client = None
        _redis_unavailable_at = time.monotonic()
        logger.warning("[Redis] unavailable, using in-memory fallback: %s", exc)
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
        # 对抗复核 4a：renew 连续【瞬时错误】计数。瞬时（Redis 抖动/超时）容忍到阈值才判失锁，
        # 避免一次网络 blip 就杀掉多小时长任务；确认被抢（Lua 返回 0）则立即判失锁不容忍。
        self._renew_transient_fails = 0

    def acquire(self) -> bool:
        r = get_redis()
        if r is None:
            # #14：Redis 不可用 → 锁降级为进程内 no-op（单进程默认有意行为）。
            # 但多进程/多副本部署下这意味着【无跨进程互斥】，须可观测。首次降级打一次 WARNING。
            _warn_lock_fail_open_once()
            self._held = True
            return True
        ok = r.set(self.key, self.token, nx=True, ex=self.ttl_sec)
        self._held = bool(ok)
        return self._held

    def renew(self) -> bool:
        """续期持有中的锁 TTL（原子比对+EXPIRE，仅当 value==自己的 token）。

        A-P1-14：旧实现 TTL=3600s 无续期，一次 build 持锁 > TTL 会静默失锁 →
        同模块并发写。完整的后台续期需为每把锁起一个任务（复杂，且本系统 Redis
        默认关闭、单进程），过度工程。最小正确做法：提供 renew()，由 brain 事件
        循环在已有的每节点回调里搭车调用——无额外线程/任务，进程在干活时顺带续期。
        内存兜底(r is None)下锁永不过期，renew 直接 no-op 返回 True。
        """
        if not self._held:
            return False
        r = get_redis()
        if r is None:
            return True
        try:
            _renew_lua = (
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
            )
            ok = r.eval(_renew_lua, 1, self.key, self.token, self.ttl_sec)
            self._renew_transient_fails = 0  # 成功通信 → 清零瞬时计数
            # ok=1 续期成功；ok=0 = 锁已不是自己的（被抢/已过期）→ 确认失锁，立即判否。
            if not bool(ok):
                logger.warning("[ModuleLock] renew 确认失锁(锁=%s)：value 已非本 token（被抢/过期）", self.key)
            return bool(ok)
        except Exception as exc:  # noqa: BLE001
            # 对抗复核 4a：这是【瞬时】通信错误（网络超时/连接池/Redis 重启），不等于确认失锁。
            # 容忍到阈值前返回 True（不误杀长任务）；连续超阈值才判失锁（Redis 长时不可用=真降级）。
            self._renew_transient_fails += 1
            if self._renew_transient_fails < _renew_transient_threshold():
                logger.warning("[ModuleLock] renew 瞬时失败(锁=%s，第 %d 次，阈值内容忍): %s",
                               self.key, self._renew_transient_fails, exc)
                return True
            logger.warning("[ModuleLock] renew 连续瞬时失败 %d 次(锁=%s)，判失锁: %s",
                           self._renew_transient_fails, self.key, exc)
            return False

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
