#!/usr/bin/env python3
"""B6 复核 #1 回归：_ProjectGitFlock 锁键规范化——worker(resolve 路径) 与交付(原始 DB 串)
必须落同一把锁，否则同项目 git 写并行互踩（Fix B 串行化失效）。
"""

from __future__ import annotations

import os
from pathlib import Path


def _lock_path_for(p):
    """构造 flock 并回读它开的锁文件路径（不同实例、同规范路径应同名）。"""
    from swarm.worker.executor import _ProjectGitFlock

    fl = _ProjectGitFlock(p)
    try:
        return fl._lock_f.name if fl._lock_f is not None else None
    finally:
        fl.__exit__()


def test_flock_key_same_for_equivalent_paths(tmp_path):
    real = tmp_path / "proj"
    real.mkdir()
    resolved = str(Path(real).resolve())
    raw_trailing = resolved + "/"
    # 相对路径（cwd 切到父目录）
    cwd = os.getcwd()
    try:
        os.chdir(str(tmp_path))
        rel = "proj"
        a = _lock_path_for(resolved)
        b = _lock_path_for(raw_trailing)
        c = _lock_path_for(rel)
    finally:
        os.chdir(cwd)
    assert a is not None
    assert a == b == c, f"等价路径必须同锁: {a} / {b} / {c}"


def test_delivery_locks_dict_key_normalized():
    """_deliver_merged_diff_serialized 的 asyncio 锁字典键也须规范化（进程内锁不分裂）。"""
    import inspect
    from swarm.brain import nodes

    src = inspect.getsource(nodes._deliver_merged_diff_serialized)
    assert "resolve()" in src or "_canon_path" in src or "os.path.realpath" in src, \
        "交付 asyncio 锁键未规范化（B6 #1 回归）"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
