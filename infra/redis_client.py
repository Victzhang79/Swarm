"""Redis 平台基础设施 — 可选启用，不可用时回退内存实现。"""

from __future__ import annotations

import json
import logging
import os
import threading
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

# D14：Redis socket 超时安全默认（秒）。默认 None=无限等 → 网络黑洞（丢包挂起非 refused）
# 时 r.eval/ping 无限阻塞，会把调用方（brain 事件循环搭车的同步调用）整个卡死。
# 可经环境变量覆盖，但 <=0/非法一律回退安全默认——绝不允许配置回到无限等（fail-closed）。
_REDIS_SOCKET_CONNECT_TIMEOUT_SEC = 2.0
_REDIS_SOCKET_TIMEOUT_SEC = 3.0


def _redis_socket_connect_timeout() -> float:
    try:
        v = float(os.environ.get("SWARM_REDIS_SOCKET_CONNECT_TIMEOUT_SEC", _REDIS_SOCKET_CONNECT_TIMEOUT_SEC))
        return v if v > 0 else _REDIS_SOCKET_CONNECT_TIMEOUT_SEC
    except (TypeError, ValueError):
        return _REDIS_SOCKET_CONNECT_TIMEOUT_SEC


def _redis_socket_timeout() -> float:
    try:
        v = float(os.environ.get("SWARM_REDIS_SOCKET_TIMEOUT_SEC", _REDIS_SOCKET_TIMEOUT_SEC))
        return v if v > 0 else _REDIS_SOCKET_TIMEOUT_SEC
    except (TypeError, ValueError):
        return _REDIS_SOCKET_TIMEOUT_SEC


def _warn_lock_fail_open_once() -> None:
    global _lock_fail_open_warned
    if not _lock_fail_open_warned:
        _lock_fail_open_warned = True
        logger.warning(
            "[ModuleLock] Redis 不可用 → 模块锁降级为【进程内 threading 锁】（B1：非原 no-op；"
            "同进程内同 key 仍互斥，但无跨进程互斥）。单进程部署安全；多进程/多副本部署存在同模块"
            "并发写 split-brain 风险，请启用 Redis（SWARM_REDIS_ENABLED=true）。"
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

        # D14：所有同步 Redis IO（acquire/renew/release/rpush/lpop/ping）都靠这两个超时兜底，
        # 网络黑洞时秒级快失败（走各调用点既有的"Redis 不可用"降级路径），不再无限阻塞。
        client = redis.from_url(
            get_config().db.redis_uri,
            decode_responses=True,
            socket_connect_timeout=_redis_socket_connect_timeout(),
            socket_timeout=_redis_socket_timeout(),
        )
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


# B1：Redis 不可用(禁用 or 宕机)时的【进程内】锁回退注册表。原 fail-open 让并发任务都
# "持锁"→ 进程内双写；改退进程内锁：同 key 仍互斥(未持有者 acquire 返回 False，调用方优雅
# 延后)，且不破坏 Redis 禁用的单进程模式(无争用即刻拿到)。跨进程互斥在无 Redis 下无法保证——
# 多副本部署须启 Redis(见 B2)。key 有界(项目×模块)，不清理无碍。
_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _local_lock_for(key: str) -> threading.Lock:
    with _LOCAL_LOCKS_GUARD:
        lk = _LOCAL_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _LOCAL_LOCKS[key] = lk
        return lk


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
        # B1(R1 复核)：本锁是【经进程内 threading 锁】获取(acquire 时 Redis 不可用)还是【经 Redis】
        # 获取。release 必须按【获取方式】释放，不能看 release 当刻的 Redis 状态——否则 Redis 在
        # acquire 后 release 前宕机时，会去 release 一把本实例从未持有的进程内锁(threading.Lock 不
        # 校验属主 → 可能误放【别的任务】持有的同 key 锁 → 双写)。
        self._local_held = False
        # 对抗复核 4a：renew 连续【瞬时错误】计数。瞬时（Redis 抖动/超时）容忍到阈值才判失锁，
        # 避免一次网络 blip 就杀掉多小时长任务；确认被抢（Lua 返回 0）则立即判失锁不容忍。
        self._renew_transient_fails = 0
        # ★复核 Item 1★：上次【确认续期成功】的单调时刻。瞬时容忍期间 Redis 的 TTL 仍在倒计——
        # 若容忍跨越 ~TTL 秒，锁可能已在 Redis 过期而本进程仍自认持有 → 同进程另一同模块任务可
        # acquire 成功 → 双写。故除计数外再加【墙钟闸】：容忍期超 TTL*0.8 一律判失锁。
        self._last_ok_monotonic = 0.0

    def acquire(self) -> bool:
        r = get_redis()
        if r is None:
            # B1：Redis 不可用 → 退【进程内锁】(非原 fail-open no-op：那让并发任务都"持锁"→
            # 进程内双写)。同 key 已被本进程另一任务持有 → 非阻塞 acquire 返回 False，调用方优雅
            # 延后("模块锁被占用请稍后重试")。首次降级打一次 WARNING(多副本下无跨进程互斥,须可观测)。
            _warn_lock_fail_open_once()
            got = _local_lock_for(self.key).acquire(blocking=False)
            self._held = got
            self._local_held = got  # 经进程内锁获取 → release 也走进程内锁
            return got
        ok = r.set(self.key, self.token, nx=True, ex=self.ttl_sec)
        self._held = bool(ok)
        self._local_held = False  # 经 Redis 获取 → release 走 Redis(即便届时 Redis 挂也不碰本地锁)
        if self._held:
            self._last_ok_monotonic = time.monotonic()  # Item 1：墙钟基准
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
            else:
                self._last_ok_monotonic = time.monotonic()  # Item 1：刷新墙钟基准
            return bool(ok)
        except Exception as exc:  # noqa: BLE001
            # 对抗复核 4a：这是【瞬时】通信错误（网络超时/连接池/Redis 重启），不等于确认失锁。
            # 容忍到阈值前返回 True（不误杀长任务）；连续超阈值才判失锁（Redis 长时不可用=真降级）。
            self._renew_transient_fails += 1
            # ★复核 Item 1★：墙钟闸——即便计数未到阈值，若距上次确认续期已 > TTL*0.8，Redis 侧锁
            # 极可能已过期(另一同模块任务可 acquire→双写) → 立即判失锁，不再容忍（安全 > 长任务存活）。
            _elapsed = time.monotonic() - self._last_ok_monotonic if self._last_ok_monotonic else 0.0
            if _elapsed > self.ttl_sec * 0.8:
                logger.warning("[ModuleLock] renew 瞬时失败且距上次续期 %.0fs > TTL*0.8(%ds)，锁恐已过期→判失锁: %s",
                               _elapsed, self.ttl_sec, exc)
                return False
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
        # B1(R1 复核)：按【获取方式】释放，不看当刻 Redis 状态。
        if self._local_held:
            # 经进程内锁获取 → 释放同一把进程内锁（即便此刻 Redis 已恢复也不能走 Redis 分支，
            # 否则本地锁永不释放 → 该 key 死锁）。
            try:
                _local_lock_for(self.key).release()
            except RuntimeError as exc:
                logger.warning("[ModuleLock] 进程内锁释放异常(锁=%s，疑重复释放): %s", self.key, exc)
            self._local_held = False
            self._held = False
            return
        r = get_redis()
        if r is None:
            # 经 Redis 获取但 release 时 Redis 挂 → 无法主动删 Redis key，靠 TTL 过期回收（不去碰
            # 进程内锁——那可能是别的任务在本次 Redis 宕机窗口里持有的同 key 锁，误放会双写）。
            logger.warning(
                "[ModuleLock] release 时 Redis 不可用，锁 %s 无法主动释放，将靠 TTL(%ds)过期回收",
                self.key, self.ttl_sec,
            )
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


# D14：renew 降频间隔 = TTL 的该比例（默认 1/10）。依据：ModuleLock 默认 TTL=3600s →
# 每 360s 续期一次已绰绰有余；renew() 自身的瞬时容忍（连续 3 次失败才判失锁）在此间隔下
# 最多消耗 0.3×TTL，仍远在其墙钟闸 TTL*0.8 之内——既有失锁判定语义完整保留。
_LOCK_RENEW_INTERVAL_FRACTION = 0.1


def renew_interval_sec(ttl_sec: int) -> float:
    """renew 降频间隔（秒）。SWARM_LOCK_RENEW_INTERVAL_SEC 可覆盖（>0 才生效）；
    默认 TTL/10，下限 1s（防超小 TTL 退化为每事件 renew 空转）。"""
    raw = os.environ.get("SWARM_LOCK_RENEW_INTERVAL_SEC")
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return max(1.0, float(ttl_sec) * _LOCK_RENEW_INTERVAL_FRACTION)


class RenewPacer:
    """D14：ModuleLock renew 降频器——brain 事件循环每个图事件都会经过 renew 搭车点，
    旧实现每事件同步 renew 一次 Redis IO；本类把它降到"距上次不足 renew_interval_sec 则跳过"。

    不变量：
    - 首次见到某把锁（刚 acquire / plan 后升级换锁对象）→ 重置计时并跳过——新锁 acquire
      即满 TTL，无需立刻续期；
    - due() 返回 True 同时推进计时（调用方随后必须真正调 renew）。
    """

    def __init__(self) -> None:
        self._lock_ref: Any = None
        self._last_ts: float = 0.0

    def due(self, lock: Any, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if lock is not self._lock_ref:
            self._lock_ref = lock
            self._last_ts = now
            return False
        if (now - self._last_ts) >= renew_interval_sec(getattr(lock, "ttl_sec", 3600)):
            self._last_ts = now
            return True
        return False


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
    def supports_blocking() -> bool:
        """Redis 后端在场时支持阻塞式出队（BLPOP 事件化）；内存 fallback 不支持。"""
        return get_redis() is not None

    @staticmethod
    def dequeue_blocking(timeout: float = 2.0) -> dict[str, str] | None:
        """D58：阻塞式出队——BLPOP 三个优先级 key 一次往返（按 key 顺序即优先级顺序），
        队列空时在 Redis 侧等待 ≤timeout 秒，enqueue 即刻唤醒（事件化，替代 2s 轮询
        每 tick 3 个 LPOP）。

        约束（调用方须知）：BLPOP 会占住一条连接直到超时/有数据——必须在线程池里调
        （asyncio.to_thread），且 timeout 取小值（≤2s）保持消费循环可中断（stop 信号/
        失主停调度器在一个 timeout 内生效，绝不闷死 P1-13 的失主停机）。
        fail-closed：Redis 异常/不可用 → 回退非阻塞 dequeue()（原逐 key LPOP/内存逻辑）。
        """
        r = get_redis()
        if r is None:
            return TaskQueue.dequeue()
        try:
            keys = [f"swarm:task_queue:{p}" for p in TaskQueue._PRIORITIES]
            got = r.blpop(keys, timeout=max(1, int(timeout)))
            if not got:
                return None
            _key, raw = got
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[TaskQueue] BLPOP 失败，回退非阻塞出队: %s", exc)
            return TaskQueue.dequeue()

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
