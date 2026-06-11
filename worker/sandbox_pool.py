"""热沙箱池 (HotSandboxPool) — 生产级沙箱预热复用池

替代旧 SandboxPool (worker/sandbox.py:800) 的危险半成品 stub。
特性：预热复用 + 健康检查 + TTL/空闲回收 + 容量上限 + 线程安全 + 后台 reaper。

设计铁律：
- threading.Lock 保护所有 _pool / 计数器读改写
- 锁内不做耗时 SDK 调用（先锁内记账，锁外 create/kill/run_code）
- 健康探针失败一定 kill（不泄漏）
- 任何 SDK 调用 try/except 失败记日志不崩
- 按 template_id 分桶（key = template_id or ""）
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _PoolEntry:
    """池内一个待命沙箱的记账信息。"""
    sandbox: Any
    template_id: str
    created_at: float
    last_used_at: float


class HotSandboxPool:
    """热沙箱池 — 预热复用 + 健康检查 + TTL/空闲回收 + 容量上限 + 线程安全。

    接口契约（与规格一一对应）：
        __init__(manager, *, max_idle_per_template=2, max_total=8,
                 ttl_seconds=600, idle_seconds=300, reap_interval=60)
        acquire(template_id=None, *, project_id=None, task_id=None) -> sandbox
        release(sandbox, *, reusable=True)
        reap() -> dict  {"killed": n, "kept": m}
        drain()
        stats() -> dict  {idle_by_template, total, borrowed, created_total, …}
        start_reaper() / stop_reaper()
    """

    def __init__(
        self,
        manager: Any,
        *,
        max_idle_per_template: int = 2,
        max_total: int = 8,
        ttl_seconds: int = 600,
        idle_seconds: int = 300,
        reap_interval: int = 60,
    ) -> None:
        self._manager = manager
        self._max_idle_per_template = max_idle_per_template
        self._max_total = max_total
        self._ttl_seconds = ttl_seconds
        self._idle_seconds = idle_seconds
        self._reap_interval = reap_interval

        # 按 template_id 分桶的待命池：template_id -> [_PoolEntry, …]
        self._pool: dict[str, list[_PoolEntry]] = {}
        # 当前借出数
        self._borrowed: int = 0
        # 历史创建总数
        self._created_total: int = 0
        # 临时沙箱(超 max_total 时创建,不进池) sid 集合
        self._temp_sids: set[str] = set()
        # sandbox_id -> 首次创建时间(跨多次 acquire/release 保持，用于 TTL 判定)
        self._created_at: dict[str, float] = {}

        self._lock = threading.Lock()

        # Reaper 线程控制
        self._reaper_stop = threading.Event()
        self._reaper_thread: threading.Thread | None = None

    # ── 内部辅助 ──────────────────────────────────────

    def _bucket_key(self, template_id: str | None) -> str:
        return template_id or ""

    def _is_expired_ttl(self, entry: _PoolEntry, now: float) -> bool:
        return (now - entry.created_at) > self._ttl_seconds

    def _is_expired_idle(self, entry: _PoolEntry, now: float) -> bool:
        return (now - entry.last_used_at) > self._idle_seconds

    def _kill_one(self, sandbox_id: str) -> None:
        """锁外调用：kill 一个沙箱，异常不外泄。"""
        try:
            self._manager.kill(sandbox_id)
        except Exception:
            logger.warning("Failed to kill sandbox %s during pool operation", sandbox_id, exc_info=True)

    def _create_sandbox(
        self, template_id: str | None, *, project_id: str | None = None,
        task_id: str | None = None,
    ) -> Any:
        """锁外调用：创建沙箱，异常不外泄（失败时 re-raise 让 acquire 处理）。"""
        sandbox = self._manager.create(
            template_id, project_id=project_id, task_id=task_id, source="pool"
        )
        return sandbox

    def _health_check(self, sandbox: Any) -> bool:
        """锁外调用：健康探针。优先 shell 端点(run_command)——不依赖 Jupyter
        kernel(自建语言镜像无 kernel，run_code 探针会误判沙箱全死)。
        """
        try:
            rc = getattr(self._manager, "run_command", None)
            if rc is not None:
                result = rc(sandbox, "echo ok", timeout=5)
            else:
                result = self._manager.run_code(sandbox, "print(1)", timeout=5)
            return bool(result.success)
        except Exception:
            return False

    def _clean_workspace(self, sandbox: Any) -> bool:
        """锁外调用：清空沙箱工作区(防跨任务文件污染)。manager 无此能力则视为成功(不阻断)。"""
        fn = getattr(self._manager, "clean_workspace", None)
        if fn is None:
            return True
        try:
            return bool(fn(sandbox))
        except Exception:
            logger.warning("pool _clean_workspace 异常", exc_info=True)
            return False

    def _pool_size_for(self, key: str) -> int:
        """必须持锁调用。"""
        return len(self._pool.get(key, []))

    # ── 公开接口 ──────────────────────────────────────

    def acquire(
        self,
        template_id: str | None = None,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
    ) -> Any:
        """取一个健康沙箱。

        流程：
        1. 池内有 → 锁内取出候选（记账），锁外健康探针
           - 探针成功 → register_sandbox_meta → 返回
           - 探针失败 → kill 丢弃 → 重新 acquire（递归一次）
        2. 池空 → create
        3. 超 max_total → 创建临时沙箱（不进池）+ warning
        """
        key = self._bucket_key(template_id)

        # ── 尝试从池内取（跳过 TTL/空闲已过期的，它们留给 reap 清理）──
        candidate_entry: _PoolEntry | None = None
        expired_sids: list[str] = []
        now = time.monotonic()
        with self._lock:
            bucket = self._pool.get(key)
            if bucket:
                # 从队首取第一个未过期的；过期的收集起来锁外 kill
                while bucket:
                    entry = bucket.pop(0)
                    if self._is_expired_ttl(entry, now) or self._is_expired_idle(entry, now):
                        expired_sids.append(entry.sandbox.sandbox_id)
                        continue
                    candidate_entry = entry
                    break
                if not bucket:
                    self._pool.pop(key, None)

        # 锁外 kill 过期沙箱
        for sid in expired_sids:
            self._kill_one(sid)
            with self._lock:
                self._created_at.pop(sid, None)

        if candidate_entry is not None:
            sandbox = candidate_entry.sandbox
            # 锁外做健康探针
            healthy = self._health_check(sandbox)
            if healthy:
                # 取用前再清一次 workspace（双保险：防归还清理失败的残留）。
                # 清理失败则弃用该沙箱、新建，绝不把脏沙箱交给任务。
                if not self._clean_workspace(sandbox):
                    logger.warning("复用沙箱 %s 取用前清理失败，弃用并新建", sandbox.sandbox_id)
                    self._kill_one(sandbox.sandbox_id)
                    with self._lock:
                        self._created_at.pop(sandbox.sandbox_id, None)
                    return self._create_and_return(template_id, project_id, task_id, key)
                # 探针+清理成功：更新 meta 并返回
                try:
                    self._manager.register_sandbox_meta(
                        sandbox.sandbox_id,
                        project_id=project_id,
                        task_id=task_id,
                        source="pool",
                    )
                except Exception:
                    logger.warning("register_sandbox_meta failed for %s", sandbox.sandbox_id, exc_info=True)
                with self._lock:
                    self._borrowed += 1
                return sandbox
            else:
                # 探针失败：kill 丢弃，走 create 路径(不再从池取，避免递归循环)
                logger.warning("Health check failed for pooled sandbox %s, killing and creating new", sandbox.sandbox_id)
                self._kill_one(sandbox.sandbox_id)
                with self._lock:
                    self._created_at.pop(sandbox.sandbox_id, None)
                return self._create_and_return(template_id, project_id, task_id, key)

        # ── 池空，创建新沙箱 ──
        return self._create_and_return(template_id, project_id, task_id, key)

    def _create_and_return(
        self,
        template_id: str | None,
        project_id: str | None,
        task_id: str | None,
        key: str,
    ) -> Any:
        """锁外创建沙箱并返回。如果超 max_total 则创建临时（不进池）。"""
        is_temp = False
        with self._lock:
            total_in_system = self._borrowed + sum(len(b) for b in self._pool.values())
            if total_in_system >= self._max_total:
                is_temp = True
                logger.warning(
                    "Sandbox pool at capacity (%d/%d), creating temporary sandbox",
                    total_in_system, self._max_total,
                )

        sandbox = self._create_sandbox(template_id, project_id=project_id, task_id=task_id)

        with self._lock:
            self._created_total += 1
            # 记录首次创建时间（跨 acquire/release 保持，TTL 据此判定）
            self._created_at[sandbox.sandbox_id] = time.monotonic()
            if not is_temp:
                self._borrowed += 1
            # 标记临时沙箱（release 时自动 kill）
            if is_temp:
                self._temp_sids.add(sandbox.sandbox_id)

        if is_temp:
            # 注册 meta（虽然临时，也需要绑定 task）
            try:
                self._manager.register_sandbox_meta(
                    sandbox.sandbox_id,
                    project_id=project_id,
                    task_id=task_id,
                    source="pool-temp",
                )
            except Exception:
                logger.warning("register_sandbox_meta failed for temp sandbox %s", sandbox.sandbox_id, exc_info=True)

        return sandbox

    def release(self, sandbox: Any, *, reusable: bool = True) -> None:
        """归还沙箱。

        reusable 且未超 max_idle_per_template 且未超龄 → 回池刷新 last_used_at；
        否则 kill。
        临时沙箱始终 kill。
        """
        sid = getattr(sandbox, "sandbox_id", None) or str(sandbox)
        key = self._bucket_key(getattr(sandbox, "template_id", None) or "")

        # 检查是否临时沙箱
        with self._lock:
            is_temp = sid in self._temp_sids
            if is_temp:
                self._temp_sids.discard(sid)
                # 临时沙箱不计入 borrowed

        if is_temp:
            self._kill_one(sid)
            with self._lock:
                self._created_at.pop(sid, None)
            return

        should_kill = False
        try_pool = False

        with self._lock:
            self._borrowed = max(0, self._borrowed - 1)

            now = time.monotonic()
            created_at = self._created_at.get(sid, now)
            ttl_expired = (now - created_at) > self._ttl_seconds

            if not reusable or ttl_expired:
                should_kill = True
            elif self._pool_size_for(key) >= self._max_idle_per_template:
                should_kill = True
            else:
                try_pool = True  # 候选回池，但需先（锁外）清理 workspace

        # 锁外清理 workspace（慢 SDK 调用不持锁）：清理成功才回池，失败则 kill
        if try_pool:
            cleaned = self._clean_workspace(sandbox)
            if not cleaned:
                logger.warning("归还沙箱 %s workspace 清理失败，弃用不回池(防污染)", sid)
                should_kill = True
            else:
                with self._lock:
                    # 二次校验容量（清理期间可能有并发归还）
                    bucket = self._pool.setdefault(key, [])
                    if len(bucket) >= self._max_idle_per_template:
                        should_kill = True
                    else:
                        bucket.append(_PoolEntry(
                            sandbox=sandbox,
                            template_id=key,
                            created_at=created_at,
                            last_used_at=time.monotonic(),
                        ))
                        try:
                            self._manager.register_sandbox_meta(
                                sid, project_id=None, task_id=None, source="pool-idle"
                            )
                        except Exception:
                            logger.warning("register_sandbox_meta cleanup failed for %s", sid, exc_info=True)

        if should_kill:
            self._kill_one(sid)
            with self._lock:
                self._created_at.pop(sid, None)

    def reap(self) -> dict:
        """回收超 TTL / 空闲 / 不健康的沙箱。

        返回 {"killed": n, "kept": m}。
        锁内收集待杀列表，锁外逐一 kill（单个失败不中断）。
        """
        to_kill: list[str] = []
        to_keep: list[_PoolEntry] = []
        now = time.monotonic()

        with self._lock:
            for key in list(self._pool.keys()):
                bucket = self._pool[key]
                surviving: list[_PoolEntry] = []
                for entry in bucket:
                    if self._is_expired_ttl(entry, now) or self._is_expired_idle(entry, now):
                        to_kill.append(entry.sandbox.sandbox_id)
                    else:
                        surviving.append(entry)
                        to_keep.append(entry)
                if surviving:
                    self._pool[key] = surviving
                else:
                    del self._pool[key]

        # 锁外 kill
        for sid in to_kill:
            self._kill_one(sid)
        with self._lock:
            for sid in to_kill:
                self._created_at.pop(sid, None)

        return {"killed": len(to_kill), "kept": len(to_keep)}

    def drain(self) -> None:
        """清空池，全部 kill。单个失败不中断。"""
        all_sids: list[str] = []
        with self._lock:
            for key in list(self._pool.keys()):
                for entry in self._pool[key]:
                    all_sids.append(entry.sandbox.sandbox_id)
            self._pool.clear()

        for sid in all_sids:
            self._kill_one(sid)
        with self._lock:
            for sid in all_sids:
                self._created_at.pop(sid, None)

    def stats(self) -> dict:
        """可观测指标快照。"""
        with self._lock:
            idle_by_template = {k: len(v) for k, v in self._pool.items()}
            total_idle = sum(len(v) for v in self._pool.values())
            return {
                "idle_by_template": idle_by_template,
                "total_idle": total_idle,
                "borrowed": self._borrowed,
                "total": self._borrowed + total_idle,
                "created_total": self._created_total,
                "max_total": self._max_total,
                "max_idle_per_template": self._max_idle_per_template,
                "ttl_seconds": self._ttl_seconds,
                "idle_seconds": self._idle_seconds,
            }

    # ── 后台 reaper ──────────────────────────────────

    def start_reaper(self) -> None:
        """启动 daemon reaper 线程，周期 reap。"""
        if self._reaper_thread is not None and self._reaper_thread.is_alive():
            return
        self._reaper_stop.clear()
        self._reaper_thread = threading.Thread(
            target=self._reaper_loop,
            name="sandbox-pool-reaper",
            daemon=True,
        )
        self._reaper_thread.start()
        logger.info("Sandbox pool reaper started (interval=%ds)", self._reap_interval)

    def stop_reaper(self) -> None:
        """停止 reaper 线程。"""
        self._reaper_stop.set()
        if self._reaper_thread is not None:
            self._reaper_thread.join(timeout=5)
            self._reaper_thread = None
        logger.info("Sandbox pool reaper stopped")

    def _reaper_loop(self) -> None:
        """Reaper 主循环：周期 reap，异常自愈。"""
        while not self._reaper_stop.is_set():
            try:
                result = self.reap()
                if result["killed"] > 0:
                    logger.info("Reaper killed %d sandbox(es), kept %d", result["killed"], result["kept"])
            except Exception:
                logger.warning("Reaper error (self-healing)", exc_info=True)
            self._reaper_stop.wait(timeout=self._reap_interval)


# ── 模块级单例 ──────────────────────────────────────
_pool_singleton: HotSandboxPool | None = None
_pool_lock = threading.Lock()


def get_sandbox_pool() -> HotSandboxPool:
    """获取进程级热沙箱池单例（按 SandboxConfig 参数配置）。"""
    global _pool_singleton
    if _pool_singleton is not None:
        return _pool_singleton
    with _pool_lock:
        if _pool_singleton is None:
            from swarm.config.settings import get_config
            from swarm.worker.sandbox import get_sandbox_manager

            cfg = get_config().sandbox
            _pool_singleton = HotSandboxPool(
                get_sandbox_manager(),
                max_idle_per_template=getattr(cfg, "pool_max_idle_per_template", 2),
                max_total=getattr(cfg, "pool_max_total", 8),
                ttl_seconds=getattr(cfg, "pool_ttl_seconds", 600),
                idle_seconds=getattr(cfg, "pool_idle_seconds", 300),
                reap_interval=getattr(cfg, "pool_reap_interval", 60),
            )
    return _pool_singleton


def pool_enabled() -> bool:
    """热池是否启用（SWARM_SANDBOX_POOL_ENABLED，默认 false 稳妥起步）。"""
    import os

    return os.environ.get("SWARM_SANDBOX_POOL_ENABLED", "false").lower() in ("true", "1", "yes")
