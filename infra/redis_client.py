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


# ── H-3（外部深审 HIGH）：项目级【读写门】——default↔模块锁的层级仲裁 ──────────────
# 病根：`default`(整项目宽锁)与模块键(root/src/…)是【平级不同 Redis key】，SET NX 只让同名键
# 互斥 → `default` 只排除另一个 `default`，绝不排除模块 holder；升级后释放 default，apply 直写
# 入口(api/routers/task.py)又取 `default` → 与正持模块锁写树的 runner 并发 = 同一 git 树双写污染。
# 治：把 default↔模块 建成【读写门】——default=写者(排他:排除所有模块读者+另一写者)，模块键=读者
# (共享:不同模块并行=E3 本意；同模块仍靠各自 key 互斥)。写者在场读者不可入、读者在场写者不可入。
# 进程内 threading 层作【权威】仲裁(与 H-2 一致，单进程部署即完全正确)；Redis Lua 层叠加跨进程。
# 诚实边界：runner 规划前的占位 default 现也排他于模块写者(过保守但绝不错——升级到模块锁后即释放
# 写者、恢复并行)；acquire 失败=非阻塞让位，调用方(runner reconcile/apply 409)优雅延后重试。
class _ProjectGate:
    """单项目进程内读写门（非阻塞）。writer 排他、多 reader 共存、writer↔reader 互斥。

    对抗复核 F5：读写槽均按【owner token】记账（reader 集 / writer token），release 校验属主——
    绝不因某个未来的重复 release 静默清掉【别的 holder】的合法读/写态（与 Redis Lua 层的 token
    纪律对齐）。"""

    def __init__(self) -> None:
        self._m = threading.Lock()
        self._readers: set[str] = set()
        self._writer: str | None = None

    def acquire_shared(self, token: str) -> bool:
        with self._m:
            if self._writer is not None:
                return False
            self._readers.add(token)
            return True

    def release_shared(self, token: str) -> None:
        with self._m:
            self._readers.discard(token)

    def acquire_exclusive(self, token: str) -> bool:
        with self._m:
            if self._writer is not None or self._readers:
                return False
            self._writer = token
            return True

    def release_exclusive(self, token: str) -> None:
        with self._m:
            if self._writer == token:
                self._writer = None

    def downgrade(self, writer_token: str, reader_tokens: list[str]) -> bool:
        """原子降级：writer → N readers（同一临界区，绝不空窗）。仅当 writer 是本 token 才降。"""
        with self._m:
            if self._writer != writer_token:
                return False
            self._writer = None
            self._readers.update(reader_tokens)
            return True


_PROJECT_GATES: dict[str, _ProjectGate] = {}
_PROJECT_GATES_GUARD = threading.Lock()


def _project_gate_for(project_id: str) -> _ProjectGate:
    with _PROJECT_GATES_GUARD:
        g = _PROJECT_GATES.get(project_id)
        if g is None:
            g = _ProjectGate()
            _PROJECT_GATES[project_id] = g
        return g


def _reset_project_gates() -> None:
    """测试隔离用——清空进程内项目门注册表。"""
    with _PROJECT_GATES_GUARD:
        _PROJECT_GATES.clear()


def _pgate_key(project_id: str) -> str:
    return f"swarm:pgate:{project_id}"


def _pgate_readers_key(project_id: str) -> str:
    return f"swarm:pgate:{project_id}:r"


# Redis 跨进程读写门（Lua 原子，KEYS[1]=hash 存 writer token，KEYS[2]=zset 存 reader token→到期时刻）。
# 对抗复核 F4：readers 改用【按 token 各自到期】的 sorted-set（score=server TIME+ttl）而非单一
# 计数——每次 acquire/renew 先 ZREMRANGEBYSCORE 清掉已过期(含 crash 未 release)的 reader，杜绝
# 幽灵 +1 永久堵死 writer。writer 侧仍以 hash key TTL 兜底 crash（写者持锁期无人能并发刷 TTL）。
# renew：reader 只刷【自己】那条 score（并 fail-closed 校验无 writer 冒进）；writer 仅在仍持 token
# 时续 hash TTL。ARGV[1]=token、ARGV[2]=ttl。
_PGATE_ACQ_SHARED_LUA = (
    "local now = tonumber(redis.call('TIME')[1]) "
    "redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now) "
    "if redis.call('hget', KEYS[1], 'w') then return 0 end "
    "redis.call('ZADD', KEYS[2], now + tonumber(ARGV[2]), ARGV[1]) "
    "redis.call('EXPIRE', KEYS[2], tonumber(ARGV[2]) * 2) return 1"
)
_PGATE_ACQ_EXCL_LUA = (
    "local now = tonumber(redis.call('TIME')[1]) "
    "redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now) "
    "if redis.call('hget', KEYS[1], 'w') then return 0 end "
    "if redis.call('ZCARD', KEYS[2]) > 0 then return 0 end "
    "redis.call('hset', KEYS[1], 'w', ARGV[1]) "
    "redis.call('expire', KEYS[1], ARGV[2]) return 1"
)
_PGATE_REL_SHARED_LUA = "redis.call('ZREM', KEYS[2], ARGV[1]) return 1"
_PGATE_REL_EXCL_LUA = (
    "if redis.call('hget', KEYS[1], 'w') == ARGV[1] then redis.call('hdel', KEYS[1], 'w') end return 1"
)
# renew 搭车续期。reader：先确认无 writer 冒进（fail-closed 返 0=已丢门），再刷自己那条 score。
# writer：仅在 hash w 仍是自己的 token 时续 TTL（不误延他人）；否则返 0=已被替换（丢门）。
_PGATE_RENEW_SHARED_LUA = (
    "if redis.call('hget', KEYS[1], 'w') then return 0 end "
    "local now = tonumber(redis.call('TIME')[1]) "
    "redis.call('ZADD', KEYS[2], now + tonumber(ARGV[2]), ARGV[1]) "
    "redis.call('EXPIRE', KEYS[2], tonumber(ARGV[2]) * 2) return 1"
)
_PGATE_RENEW_EXCL_LUA = (
    "if redis.call('hget', KEYS[1], 'w') == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) end return 0"
)
# 原子降级：整项目写者(ARGV[1]=旧 writer token) → N 个模块读者(ARGV[3..]=reader tokens)。一次
# eval 内删写者+ZADD 全部读者=门态绝不空窗（无 pounce）。仅当写者仍是本 token 才降（防误改）。
_PGATE_DOWNGRADE_LUA = (
    "if redis.call('hget', KEYS[1], 'w') ~= ARGV[1] then return 0 end "
    "redis.call('hdel', KEYS[1], 'w') "
    "local now = tonumber(redis.call('TIME')[1]) "
    "for i=3,#ARGV do redis.call('ZADD', KEYS[2], now + tonumber(ARGV[2]), ARGV[i]) end "
    "redis.call('EXPIRE', KEYS[2], tonumber(ARGV[2]) * 2) return 1"
)
# 模块 key 释放（H8：原子比对+删，仅当 value==自己的 token）——release / _release_key_only 共用。
_LOCK_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


def _invalidate_redis(exc: Exception) -> None:
    """H-1（外部深审）：缓存 client 操作抛异常（Redis 连上后故障/连接断）→ 作废缓存并
    进冷却，下次 get_redis 重探。get_redis 一旦连上就缓存 client（第 86 行）从不重探，
    坏 client 的后续 IO 会持续抛异常；acquire 的 SET 又在 runner 调用点 try 之外 → 异常
    泄漏使 _task_running 残留、task_id 永判"已在执行中"死锁。作废后本次转内存兜底。"""
    global _redis_client, _redis_unavailable_at
    _redis_client = None
    _redis_unavailable_at = time.monotonic()
    logger.warning("[Redis] 缓存连接 IO 失败 → 作废重探 + 本次转内存兜底: %s", exc)


class ModuleLock:
    """同项目同模块互斥锁（Redis SET NX + TTL）。"""

    def __init__(self, project_id: str, module_key: str, *, ttl_sec: int = 3600,
                 project_wide: bool | None = None):
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
        # H-2 治本：是否【经 Redis】持有（叠加在本地锁之上的跨进程互斥层）。release 据此决定
        # 是否删 Redis key；renew 据此决定是否续 Redis TTL（纯本地锁不过期，renew 直接 no-op）。
        self._redis_held = False
        # 对抗复核 4a：renew 连续【瞬时错误】计数。瞬时（Redis 抖动/超时）容忍到阈值才判失锁，
        # 避免一次网络 blip 就杀掉多小时长任务；确认被抢（Lua 返回 0）则立即判失锁不容忍。
        self._renew_transient_fails = 0
        # ★复核 Item 1★：上次【确认续期成功】的单调时刻。瞬时容忍期间 Redis 的 TTL 仍在倒计——
        # 若容忍跨越 ~TTL 秒，锁可能已在 Redis 过期而本进程仍自认持有 → 同进程另一同模块任务可
        # acquire 成功 → 双写。故除计数外再加【墙钟闸】：容忍期超 TTL*0.8 一律判失锁。
        self._last_ok_monotonic = 0.0
        # H-3：项目读写门角色。default=写者(整项目排他)，其余模块键=读者(共享)。门是 default↔模块
        # 唯一层级仲裁点。_gate_held=进程内门槽已持；_gate_redis_held=Redis 跨进程门层已叠加。
        # 对抗复核 F3：role 优先用【显式 project_wide】而非 `module_key=="default"` 字符串比对——
        # 否则写集里真有名为 `default` 的顶层目录时(module_keys_from_plan 派生出键 "default")会被
        # 误判成整项目写者，与同组合锁内其它模块读者在同一门上写↔读自撞 → 该组合锁 100% 永远拿不到。
        # MultiModuleLock 的子锁一律显式传 project_wide=False（它们永远是模块读者）。
        self._is_project_wide = (module_key == "default") if project_wide is None else bool(project_wide)
        self._gate_held = False
        self._gate_redis_held = False

    def acquire(self) -> bool:
        # H-2 治本（外部深审 HIGH，对抗复核 F3 收口）：进程内 threading 锁作为【进程级权威
        # 互斥】始终【先】获取——它是唯一能同步 Redis 路径与 fallback 路径的同进程同 key 仲裁者。
        # 旧实现两条路径各持不同域（Redis 宕机期持本地、恢复期持 Redis），check-then-act 无论
        # 正探还是反探都留 TOCTOU 双域窗口。现统一：先拿本地锁（原子，拿不到=本进程已有持有者
        # 直接让位）；Redis 可用再叠加 SET NX 作【跨进程】互斥层，SET 失败（他进程经 Redis 持有）
        # 则回退本地锁并让位。release/renew 按 _local_held/_redis_held 对称处理。
        # hunter F1：幂等再获取——本实例已持有则直接返回 True，绝不二次 acquire threading.Lock
        # （非重入锁会失败→把 _held 置 False 却留 _local_held=True→release 早退→本地锁永久孤儿
        # 死锁）。与 release() 的 `if not self._held: return` 幂等语义对称。
        if self._held:
            return True
        # H-3：先过【项目读写门】——default=写者排他(排除所有模块读者+另一写者)，模块键=读者共享。
        # 门是 default↔模块 唯一层级仲裁点；拿不到=有互斥角色在写整项目/本项目宽锁，非阻塞让位。
        if not self._acquire_gate():
            self._held = False
            return False
        if not self._acquire_key_only():
            # 他进程经 Redis / 同进程经本地持有该 key → 释放门并让位（不留孤儿门槽）。
            self._release_gate()
            self._held = False
            return False
        self._held = True
        return True

    def _acquire_key_only(self) -> bool:
        """获取 per-key 层（进程内 key 锁 + Redis SET NX），【不碰门】。用于 acquire 与降级取模块
        key（降级时写者独占保护下调用，无争用）。不设 _held（由调用方/降级接管）。契约=永不抛只返 bool。"""
        if not _local_lock_for(self.key).acquire(blocking=False):
            # 同进程已有同 key 持有者（无论其经 Redis 还是纯本地）——非阻塞让位。
            return False
        self._local_held = True
        r = get_redis()
        if r is None:
            # B1：Redis 不可用 → 仅进程内互斥（多副本无跨进程互斥=split-brain 风险，首次 WARN）。
            _warn_lock_fail_open_once()
            self._redis_held = False
            return True
        try:
            ok = r.set(self.key, self.token, nx=True, ex=self.ttl_sec)
        except Exception as exc:  # noqa: BLE001
            # H-1：SET 抛异常（缓存 client 已坏）绝不外泄——作废坏 client，本次退化为纯进程内锁。
            _invalidate_redis(exc)
            _warn_lock_fail_open_once()
            self._redis_held = False
            return True
        if ok:
            self._redis_held = True  # 叠加跨进程互斥层
            self._last_ok_monotonic = time.monotonic()  # Item 1：墙钟基准
            return True
        # 他进程经 Redis 持有该 key → 释放已拿的本地锁并让位（不留孤儿本地锁）。
        try:
            _local_lock_for(self.key).release()
        except RuntimeError:
            pass
        self._local_held = False
        return False

    def _release_key_only(self) -> None:
        """释放 per-key 两层（Redis key + 进程内 key 锁），【不碰门】。release / 降级回滚共用。"""
        if self._redis_held:
            r = get_redis()
            if r is None:
                logger.warning(
                    "[ModuleLock] release 时 Redis 不可用，锁 %s 的 Redis key 靠 TTL(%ds)过期回收",
                    self.key, self.ttl_sec,
                )
            else:
                try:
                    r.eval(_LOCK_RELEASE_LUA, 1, self.key, self.token)
                except Exception as exc:
                    _invalidate_redis(exc)  # H-1：坏 client 作废重探（key 靠 TTL 回收）
                    logger.debug("[ModuleLock] release: %s", exc)
            self._redis_held = False
        if self._local_held:
            try:
                _local_lock_for(self.key).release()
            except RuntimeError as exc:
                logger.warning("[ModuleLock] 进程内锁释放异常(锁=%s，疑重复释放): %s", self.key, exc)
            self._local_held = False

    def _acquire_gate(self) -> bool:
        """H-3：过项目读写门（进程内权威 + Redis 跨进程叠加）。拿不到=互斥角色在场，返回 False。"""
        g = _project_gate_for(self.project_id)
        if self._is_project_wide:
            if not g.acquire_exclusive(self.token):
                return False
        elif not g.acquire_shared(self.token):
            return False
        self._gate_held = True
        r = get_redis()
        if r is not None:
            hk, zk = _pgate_key(self.project_id), _pgate_readers_key(self.project_id)
            try:
                if self._is_project_wide:
                    ok = r.eval(_PGATE_ACQ_EXCL_LUA, 2, hk, zk, self.token, self.ttl_sec)
                else:
                    ok = r.eval(_PGATE_ACQ_SHARED_LUA, 2, hk, zk, self.token, self.ttl_sec)
                if not ok:
                    # 他进程经 Redis 持有互斥角色 → 回滚进程内门槽并让位。
                    self._release_gate_local(g)
                    return False
                self._gate_redis_held = True
            except Exception as exc:  # noqa: BLE001
                # Redis 门 IO 失败 → 作废坏 client；本次退化为进程内门权威（不叠加跨进程层）。
                _invalidate_redis(exc)
        return True

    def _release_gate_local(self, g: "_ProjectGate") -> None:
        if self._is_project_wide:
            g.release_exclusive(self.token)
        else:
            g.release_shared(self.token)
        self._gate_held = False

    def _release_gate(self) -> None:
        if not self._gate_held:
            return
        g = _project_gate_for(self.project_id)
        if self._gate_redis_held:
            r = get_redis()
            if r is not None:
                hk, zk = _pgate_key(self.project_id), _pgate_readers_key(self.project_id)
                try:
                    if self._is_project_wide:
                        r.eval(_PGATE_REL_EXCL_LUA, 2, hk, zk, self.token)
                    else:
                        r.eval(_PGATE_REL_SHARED_LUA, 2, hk, zk, self.token)
                except Exception as exc:  # noqa: BLE001
                    _invalidate_redis(exc)  # 坏 client 作废；门态由 TTL/score 兜底回收
            self._gate_redis_held = False
        self._release_gate_local(g)

    def _renew_gate(self, r: Any) -> bool:
        """renew 搭车续门（仅 Redis 门层持有时）。返回 False=Redis 侧【确认丢门】（写者 token 被
        替换 / 读者遭遇写者冒进）→ 调用方 fail-closed（对抗复核 F1）。瞬时 IO 失败=容忍返 True
        （与 key renew 瞬时语义一致，门态服务器侧仍在）。无 Redis 门层=无需续，返 True。"""
        if not self._gate_redis_held or r is None:
            return True
        hk, zk = _pgate_key(self.project_id), _pgate_readers_key(self.project_id)
        try:
            if self._is_project_wide:
                ok = r.eval(_PGATE_RENEW_EXCL_LUA, 2, hk, zk, self.token, self.ttl_sec)
            else:
                ok = r.eval(_PGATE_RENEW_SHARED_LUA, 2, hk, zk, self.token, self.ttl_sec)
            if not ok:
                self._gate_redis_held = False
                logger.warning("[ModuleLock] H-3 项目门续期确认丢失(锁=%s，role=%s)→判失锁",
                               self.key, "writer" if self._is_project_wide else "reader")
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            _invalidate_redis(exc)  # 瞬时失败容忍（门态服务器侧仍在）；下次 get_redis 重探
            return True

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
        # H-3（对抗复核 F2）：项目门续期【独立于】key 的 _redis_held——两层各自的 Redis flag 可能
        # 分叉（门 eval 成功但随后 key SET 抛异常→_gate_redis_held=True 而 _redis_held=False）。
        # 门续期必须无条件先跑，否则长任务持门期间门键静默过期让互斥角色跨进程冒进。
        gate_ok = self._renew_gate(r) if r is not None else True
        if not gate_ok:
            return False  # F1：Redis 侧确认丢门 → fail-closed
        # H-2：纯进程内锁（Redis 宕机期获取或未叠加 Redis 层）不过期，key renew no-op。否则
        # renew 会对一把【从未 SET 进 Redis】的 key 跑 Lua GET → 恒返回 0 → 误判失锁（Redis
        # 恢复后尤甚）。只有 _redis_held 的锁才需续 Redis key TTL。
        if not self._redis_held:
            return True
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
            # H-1：作废坏 client 让下次 get_redis 重探（不改瞬时容忍语义——容忍逻辑照旧走下方）。
            _invalidate_redis(exc)
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
        # H-2 治本：对称释放【两层】——始终释放已持的进程级本地锁；若还叠加了 Redis 层则再原子删
        # Redis key（_release_key_only）。H-3：再释放项目读写门。顺序无关（各层各自权威）。
        self._release_key_only()
        self._release_gate()
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
    lock,
    project_id: str,
    plan: dict[str, Any] | None,
):
    """plan 产出后升级为【全部写集】的组合模块锁（E3，登记册 §六）。

    旧行为两处纸面互斥：①目标键只取 paths[0]（写 x+y 只锁 x）；②升级失败保留旧锁
    照跑——旧"default"与他人模块键不同串=零互斥，两任务并发写同一 git 树。
    新行为：目标=写集全部顶层目录的组合锁；获取失败抛 ModuleLockUpgradeConflict
    （调用方有界等待重试，绝不静默照跑）。单次尝试、不阻塞（本函数在事件循环内被
    同步调用，等待由调用方 await 节拍执行）。

    H-3（读写门治本）：旧锁若是【整项目写者】(default)，new_lock 的模块读者会被旧锁自己的写者门
    挡住（写↔读互斥）——故此路径走【原子降级】：写者独占保护下取模块 key + 原子换门（不空窗），
    绝不 acquire-before-release 自撞。失败=旧写者门原样保留，fail-loud 重试（调用方 lock 仍持有）。"""
    new_keys = module_keys_from_plan(plan)
    new_key = "+".join(new_keys)
    if new_key == lock.module_key:
        return lock
    new_lock = MultiModuleLock(project_id, new_keys, ttl_sec=lock.ttl_sec)
    if getattr(lock, "_is_project_wide", False) and getattr(lock, "_held", False):
        # 整项目写者 → 模块读者：原子降级（消除自撞与释放窗口）。
        if not new_lock.acquire_by_downgrade(lock):
            raise ModuleLockUpgradeConflict(
                f"模块锁降级冲突：{lock.module_key} → {new_key}（模块键异常争用）")
        lock.release()  # 释放旧 default 的 per-key（门已在降级内交出，release 跳过门）
    else:
        # 窄锁→更宽锁（非写者）：保持 acquire-before-release，避免释放窗口丢已持模块键。
        if not new_lock.acquire():
            raise ModuleLockUpgradeConflict(
                f"模块锁升级冲突：{lock.module_key} → {new_key}（目标键被其它任务持有）")
        lock.release()
    logger.info("[ModuleLock] upgraded %s → %s", lock.module_key, new_key)
    return new_lock


def module_keys_from_plan(plan: dict[str, Any] | None) -> list[str]:
    """E3（登记册 §六）：从计划【全部写集】（writable ∪ create_files）derive 顶层模块键。

    旧 module_key_from_plan 只取 paths[0]——计划写 x+y 两模块却只锁 x，另一任务锁 y
    后双方在对方"没锁的那半"并发写同一 git 树（纸面互斥）。返回排序去重列表；无写集
    → ["default"]（整项目宽锁，安全侧）。"""
    if not plan:
        return ["default"]
    paths: list[str] = []
    for st in plan.get("subtasks") or []:
        scope = st.get("scope") or {}
        paths.extend(scope.get("writable") or [])
        paths.extend(scope.get("create_files") or [])
    keys: set[str] = set()
    for p_ in paths:
        p_ = str(p_).replace("\\", "/").lstrip("/")
        if not p_:
            continue
        parts = p_.split("/")
        keys.add(parts[0] if len(parts) > 1 else "root")
    return sorted(keys) or ["default"]


def module_key_from_plan(plan: dict[str, Any] | None) -> str:
    """兼容入口：单键展示口径（E3 后仅用于日志/展示；互斥请用 module_keys_from_plan）。"""
    return "+".join(module_keys_from_plan(plan))


class ModuleLockUpgradeConflict(Exception):
    """E3：锁升级目标键被其它任务持有——绝不保留窄/旧锁照跑（那是纸面互斥）。

    调用方应有界等待重试（对方任务释放后即可升级）；耗尽预算按锁冲突 fail-loud，
    重试经 E1 播种低成本续跑。"""


class MultiModuleLock:
    """E3：多模块键组合锁——按排序键序 all-or-nothing 获取（有序=无死锁），任一失败
    回滚已获取部分。对外镜像 ModuleLock 接口（acquire/release/renew/module_key/ttl_sec），
    runner 的续期/释放/日志零改动消费。"""

    def __init__(self, project_id: str, keys: list[str], *, ttl_sec: int = 3600):
        _uniq = sorted(set(keys)) or ["default"]
        self.project_id = project_id
        self.module_key = "+".join(_uniq)
        self.ttl_sec = ttl_sec
        # F3：子锁一律【模块读者】——即便写集里真有名为 "default" 的顶层目录派生出键 "default"，
        # 也绝不当整项目写者（否则与同组合内其它读者在同一门自撞→该组合锁永不可达）。
        self._locks = [ModuleLock(project_id, k, ttl_sec=ttl_sec, project_wide=False) for k in _uniq]

    def acquire(self) -> bool:
        got: list[ModuleLock] = []
        for lk in self._locks:
            if lk.acquire():
                got.append(lk)
                continue
            for g in reversed(got):  # 回滚，绝不半持
                try:
                    g.release()
                except Exception:  # noqa: BLE001 — 回滚失败留痕不掩盖 acquire 失败
                    logger.warning("[ModuleLock] E3 组合锁回滚释放 %s 失败", g.module_key)
            return False
        return True

    def release(self) -> None:
        for lk in reversed(self._locks):
            try:
                lk.release()
            except Exception:  # noqa: BLE001 — 单键释放失败不挡其余（TTL 兜底）
                logger.warning("[ModuleLock] E3 组合锁释放 %s 失败（TTL 兜底）", lk.module_key)

    def renew(self) -> bool:
        ok = True
        for lk in self._locks:
            ok = lk.renew() and ok  # 全续；任一失败=失锁（与单锁语义一致 fail-closed）
        return ok

    def acquire_by_downgrade(self, old_lock: "ModuleLock") -> bool:
        """H-3（对抗复核）：从整项目【写者】old_lock 原子降级为本组合的【模块读者】。

        写者独占期无并发者能持有任何模块键/门 → ①先在写者保护下取所有模块 key（无争用）；②原子
        换门（写者→N 读者，一次临界区/一次 Lua，门态绝不空窗=无 pounce）；③把门态标记接管到各子锁。
        任一步失败=回滚已取模块 key，old_lock 的写者门【原样保留】（调用方据此 fail-loud 重试，
        绝不静默丢锁）。降级本身不应因【他人】冲突失败（写者在场无人可争）。"""
        # 1) 写者独占保护下取所有模块 key（不碰门）。
        got: list[ModuleLock] = []
        for lk in self._locks:
            if lk._acquire_key_only():
                got.append(lk)
            else:
                for g in reversed(got):
                    g._release_key_only()
                return False
        reader_tokens = [lk.token for lk in self._locks]
        # 2) Redis 门层【先】原子降级（跨进程权威）——旧锁曾叠加 Redis 门层才做。对抗复核 2nd（CONFIRMED）：
        #    Lua 返回 0 = 跨进程写者 token 已不匹配（TTL 失效期被他进程夺走）→ 绝不能视同成功（否则本
        #    进程当读者写树、他进程当写者写树=双写）。硬失败回滚模块 key，旧写者门原样保留，fail-loud 重试。
        had_redis_gate = old_lock._gate_redis_held
        redis_gate_ok = False
        if had_redis_gate:
            r = get_redis()
            if r is not None:
                try:
                    ok = r.eval(_PGATE_DOWNGRADE_LUA, 2, _pgate_key(self.project_id),
                                _pgate_readers_key(self.project_id),
                                old_lock.token, self.ttl_sec, *reader_tokens)
                    if not ok:
                        logger.warning("[ModuleLock] H-3 降级：Redis 写者 token 已不匹配(跨进程被夺/过期)"
                                       "→ 硬失败回滚，fail-loud 重试（绝不双写）")
                        for lk in reversed(got):
                            lk._release_key_only()
                        return False
                    redis_gate_ok = True
                except Exception as exc:  # noqa: BLE001
                    _invalidate_redis(exc)  # 瞬时 IO 失败 → 退化为进程内门权威（旧写者键靠 TTL 兜底）
        # 3) 进程内权威层原子换门（本进程持写者 → downgrade 恒成功；意外失败=不该发生，防御回滚）。
        g = _project_gate_for(self.project_id)
        if not g.downgrade(old_lock.token, reader_tokens):
            logger.error("[ModuleLock] H-3 降级：进程内写者门意外丢失(不该发生)——回滚")
            for lk in reversed(got):
                lk._release_key_only()
            return False
        # 4) 门态标记接管到各子锁；旧锁交出门（其 per-key :default 仍由调用方 release() 回收）。
        old_lock._gate_held = False
        old_lock._gate_redis_held = False
        for lk in self._locks:
            lk._gate_held = True
            lk._gate_redis_held = redis_gate_ok
            lk._held = True
        return True


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
    def _drain_memory_to_redis(r) -> None:
        """M-1（外部深审）：Redis 恢复后把【停机期堆积的内存条目】冲回 Redis（保优先级+FIFO），
        杜绝内存 fallback 条目在 Redis 恢复后永久滞留不可达。在 enqueue/dequeue 的 Redis 路径
        开头调——双源合一到 Redis。rpush 成功才 pop（抛异常则该条留内存，由外层 except 兜底）。"""
        for p in TaskQueue._PRIORITIES:
            mem = TaskQueue._memory[p]
            while mem:
                r.rpush(f"swarm:task_queue:{p}", mem[0])
                mem.pop(0)

    @staticmethod
    def enqueue(task_id: str, project_id: str, priority: str = "normal") -> None:
        """入队，priority 可选 urgent/normal/background，默认 normal。"""
        if priority not in TaskQueue._PRIORITIES:
            logger.warning("[TaskQueue] 未知优先级 %s，降级为 normal", priority)
            priority = "normal"
        r = get_redis()
        payload = json.dumps({"task_id": task_id, "project_id": project_id, "priority": priority})
        if r is not None:
            # M-1：RPush 无 try 时，缓存 client 已坏会抛异常外泄（submit_task 之上无兜底）→ 任务
            # 入队失败静默丢。包 try：先冲内存残留再入本条；异常转内存兜底 + 作废坏 client 重探。
            try:
                TaskQueue._drain_memory_to_redis(r)
                r.rpush(f"swarm:task_queue:{priority}", payload)
                return
            except Exception as exc:  # noqa: BLE001
                _invalidate_redis(exc)
        TaskQueue._memory[priority].append(payload)

    @staticmethod
    def dequeue() -> dict[str, str] | None:
        """按 urgent → normal → background 顺序出队。"""
        r = get_redis()
        if r is not None:
            # M-1：LPop 无 try 时坏 client 抛异常外泄消费循环；且恢复后内存残留不可达。包 try：
            # 先冲内存残留进 Redis 再统一按优先级出（双源合一保序）；异常→作废坏 client + 落内存出队。
            try:
                TaskQueue._drain_memory_to_redis(r)
                for p in TaskQueue._PRIORITIES:
                    raw = r.lpop(f"swarm:task_queue:{p}")
                    if raw:
                        return json.loads(raw)
                return None
            except Exception as exc:  # noqa: BLE001
                _invalidate_redis(exc)
        # 内存 fallback（Redis 不可用/本次 IO 失败）：同优先级逻辑
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
            # M-1：BLPOP 失败若不作废坏 client，回退的 dequeue() 会再用同一坏 client 二次抛
            # （dequeue 现自带 try 兜底，但此处直接作废可省一次无谓往返、加速重探）。
            _invalidate_redis(exc)
            logger.debug("[TaskQueue] BLPOP 失败，作废坏 client 并回退非阻塞出队: %s", exc)
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
