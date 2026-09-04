"""per-project git 文件锁 —— 从 worker/executor.py 抽出（round26 god-file 治理）。

自足叶模块：只依赖 stdlib（fcntl/hashlib/tempfile，__init__ 内 lazy import）与
`swarm.git_base.canon_path`（同样 lazy）。executor.py re-export `_ProjectGitFlock`
与 `_warn_git_flock_fail_open_once`，使既有代码/测试仍可经 executor 命名空间导入
`_ProjectGitFlock`（sandbox.py / brain.nodes / test_wave3_gitlock）可寻址不变，调用路径保持兼容。
"""

from __future__ import annotations

import errno
import logging
import math
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_git_flock_fail_open_warned = False
_DEFAULT_ACQUIRE_TIMEOUT_S = 30.0
_ACQUIRE_TIMEOUT_ENV = "SWARM_GIT_FLOCK_ACQUIRE_TIMEOUT_SEC"


def _configured_acquire_timeout_s(default: float = _DEFAULT_ACQUIRE_TIMEOUT_S) -> float:
    """读取独立的跨进程 git 锁等待预算；坏值不能关闭有界等待。"""
    raw = os.environ.get(_ACQUIRE_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 0.0
    if math.isfinite(value) and value > 0:
        return value
    logger.warning("%s=%r 非法，回退默认 %.0fs", _ACQUIRE_TIMEOUT_ENV, raw, default)
    return default


class ProjectGitLockError(RuntimeError):
    """项目 git 锁基础设施故障；禁止降级为无锁写。"""


class ProjectGitLockTimeout(ProjectGitLockError, TimeoutError):
    """同项目 git 临界区争用超过有界等待时间。"""

    def __init__(self, lock_path: Path, timeout_s: float) -> None:
        self.lock_path = lock_path
        self.timeout_s = timeout_s
        super().__init__(f"等待项目 git 锁超时（{timeout_s:.3f}s）: {lock_path}")


def _warn_git_flock_fail_open_once(reason: str) -> None:
    """#15：git flock 降级无锁时首次打 WARNING（多 worker 并发共享工作树下=脏 diff 风险，须可观测）。"""
    global _git_flock_fail_open_warned
    if not _git_flock_fail_open_warned:
        _git_flock_fail_open_warned = True
        logger.warning(
            "[GitFlock] 文件锁降级为无锁（%s）。同项目并发 worker 共享 git 工作树/索引时"
            "存在脏 diff/假通风险。Windows 等无 fcntl 平台属预期；类 Unix 上出现请排查。",
            reason,
        )


class _ProjectGitFlock:
    """per-project 文件锁，串行化同一 project_path 的 git 临界操作（reset / add -N + diff）。

    TD2606-B5/C5/M5：dispatch 用 asyncio.gather 并发跑 worker，全部共享同一本地 git 工作树/索引。
    原 flock 只包 _reset_scope_to_head 的 git checkout；`git add -N`（改共享 index）+ `git diff`
    未锁 → 并发 worker 的 intent-to-add 泄漏进彼此 diff、reset 与 diff 互踩（脏 diff/假通/重试死循环）。
    此锁把所有 git 临界操作串行化（操作短暂；沙箱内 CODING/编译不持锁、仍并行）。
    仅 fcntl 明确不可用（如 Windows）时降级无锁；锁文件构造或运行期 flock 故障均
    fail-loud，绝不冒险写共享树。已存在的锁发生争用时以非阻塞轮询等待，默认最多 30 秒；
    等待预算独立于持锁任务的 L2/Worker
    墙钟，超时交给任务失败/重试阶梯，绝不陪跑数分钟。超时抛
    ``ProjectGitLockTimeout``，绝不无锁进入。
    """

    DEFAULT_ACQUIRE_TIMEOUT_S = _DEFAULT_ACQUIRE_TIMEOUT_S
    ACQUIRE_POLL_INTERVAL_S = 0.05

    def __init__(self, local_root: object, *, acquire_timeout_s: float | None = None) -> None:
        self._lock_f = None
        self._fcntl = None
        self._lock_path: Path | None = None
        raw_timeout = (
            _configured_acquire_timeout_s(self.DEFAULT_ACQUIRE_TIMEOUT_S)
            if acquire_timeout_s is None
            else float(acquire_timeout_s)
        )
        if not math.isfinite(raw_timeout):
            raise ValueError("git flock acquire_timeout_s 必须是有限数值")
        self._acquire_timeout_s = max(0.0, raw_timeout)
        try:
            import fcntl
        except ImportError:
            _warn_git_flock_fail_open_once("当前平台无 fcntl")
            return

        try:
            import hashlib
            import tempfile as _tf
            # ★B6 复核 #1/L-4★：锁键规范化【单一事实源】canon_path——worker 传 resolve() 路径、
            # 交付传 DB 原始串，二者拼法差就是两把锁 → 同项目 git 写并行互踩。与交付 asyncio 锁字典
            # 共用 canon_path，连 resolve() 异常 fallback 都同源，不再各处分裂。
            from swarm.git_base import canon_path
            proj_hash = hashlib.sha1(canon_path(local_root).encode()).hexdigest()[:16]
            lock_path = Path(_tf.gettempdir()) / f"swarm_git_{proj_hash}.lock"
            self._lock_f = open(lock_path, "w")  # noqa: SIM115
            self._fcntl = fcntl
            self._lock_path = lock_path
        except Exception as exc:  # noqa: BLE001
            self._lock_f = None
            logger.error("[GIT_FLOCK] 构造项目 git 锁失败，拒绝无锁写", exc_info=True)
            raise ProjectGitLockError("构造项目 git 锁失败") from exc

    def __enter__(self) -> "_ProjectGitFlock":
        # DR-05-F1(#87)：实例级持锁标志，供临界区/provenance 查询"该批 diff 是否无锁产出"。
        self._locked = False
        if self._lock_f is not None and self._fcntl is not None:
            acquire_timeout_s = max(
                0.0,
                float(getattr(self, "_acquire_timeout_s", self.DEFAULT_ACQUIRE_TIMEOUT_S)),
            )
            started = time.monotonic()
            deadline = started + acquire_timeout_s
            transient_failures = 0
            while True:
                try:
                    self._fcntl.flock(
                        self._lock_f,
                        self._fcntl.LOCK_EX | self._fcntl.LOCK_NB,
                    )
                    self._locked = True
                    break
                except OSError as exc:
                    if isinstance(exc, BlockingIOError) or exc.errno in {
                        errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK,
                    }:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            lock_path = getattr(self, "_lock_path", None) or Path("<unknown>")
                            logger.error(
                                "[GIT_FLOCK] 等待项目 git 锁超时 %.3fs，拒绝无锁进入临界区: %s",
                                acquire_timeout_s, lock_path,
                            )
                            self._close_lock_file()
                            raise ProjectGitLockTimeout(
                                lock_path, acquire_timeout_s,
                            ) from exc
                        time.sleep(min(self.ACQUIRE_POLL_INTERVAL_S, remaining))
                        continue
                    transient_failures += 1
                    if transient_failures < 3:
                        time.sleep(0.1 * transient_failures)
                        continue
                    logger.error(
                        "[GIT_FLOCK] flock(LOCK_EX) 运行时失败，拒绝无锁进入 git 临界区: %s",
                        type(exc).__name__,
                    )
                    self._close_lock_file()
                    raise ProjectGitLockError("获取项目 git 锁失败") from exc
                except Exception as exc:  # noqa: BLE001
                    transient_failures += 1
                    if transient_failures < 3:
                        time.sleep(0.1 * transient_failures)
                        continue
                    logger.error(
                        "[GIT_FLOCK] flock(LOCK_EX) 运行时异常，拒绝无锁进入 git 临界区: %s",
                        type(exc).__name__,
                    )
                    self._close_lock_file()
                    raise ProjectGitLockError("获取项目 git 锁失败") from exc
        return self

    def _close_lock_file(self) -> None:
        lock_f = self._lock_f
        self._lock_f = None
        if lock_f is not None:
            try:
                lock_f.close()
            except Exception:  # noqa: BLE001 — 超时主异常优先，关闭失败仅失去本地兜底
                logger.warning("[GIT_FLOCK] 锁等待失败后的文件句柄关闭异常", exc_info=True)

    def __exit__(self, *exc: object) -> bool:
        if self._lock_f is not None and self._fcntl is not None:
            # P2-3：unlock 与 close 各自护栏——unlock 抛异常不得跳过 close（句柄泄漏；
            # 进程退出前锁本身随 fd 关闭释放，close 是兜底的资源边界）
            try:
                self._fcntl.flock(self._lock_f, self._fcntl.LOCK_UN)
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._close_lock_file()
        return False
