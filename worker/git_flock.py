"""per-project git 文件锁 —— 从 worker/executor.py 抽出（round26 god-file 治理）。

自足叶模块：只依赖 stdlib（fcntl/hashlib/tempfile，__init__ 内 lazy import）与
`swarm.git_base.canon_path`（同样 lazy）。executor.py re-export `_ProjectGitFlock`
与 `_warn_git_flock_fail_open_once`，使既有代码/测试仍可经 executor 命名空间导入
`_ProjectGitFlock`（sandbox.py / brain.nodes / test_wave3_gitlock）可寻址不变。行为逐字节等价。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_git_flock_fail_open_warned = False


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
    fcntl 不可用（如 Windows）/打开失败时降级无锁（与旧行为一致，不阻断）。
    """

    def __init__(self, local_root: object) -> None:
        self._lock_f = None
        self._fcntl = None
        try:
            import fcntl
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
        except Exception as exc:  # noqa: BLE001
            self._lock_f = None
            _warn_git_flock_fail_open_once(f"fcntl/锁文件不可用: {type(exc).__name__}")

    def __enter__(self) -> "_ProjectGitFlock":
        if self._lock_f is not None and self._fcntl is not None:
            try:
                self._fcntl.flock(self._lock_f, self._fcntl.LOCK_EX)
            except Exception as exc:  # noqa: BLE001
                _warn_git_flock_fail_open_once(f"flock(LOCK_EX) 失败: {type(exc).__name__}")
        return self

    def __exit__(self, *exc: object) -> bool:
        if self._lock_f is not None and self._fcntl is not None:
            try:
                self._fcntl.flock(self._lock_f, self._fcntl.LOCK_UN)
                self._lock_f.close()
            except Exception:  # noqa: BLE001
                pass
        return False
