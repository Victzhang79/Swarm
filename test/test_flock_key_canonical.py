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


def test_delivery_locks_dict_key_normalized(tmp_path, monkeypatch):
    """_deliver_merged_diff_serialized 的 asyncio 锁字典键也须规范化（进程内锁不分裂）。

    ★批25 GS-5w 换锁★（原实现=getsource 断 "resolve()"/"_canon_path"/"os.path.realpath"
    任一存在=实现形态枚举锚点）。行为锁：等价路径（绝对/尾斜杠/相对）真调
    _deliver_merged_diff_serialized（内层 git 临界区打桩，不碰真仓），断
    _project_delivery_locks 只长出 1 个键——三种拼法落同一把锁。
    删什么变红：删掉 canon_path 归一（直接拿原始 proj_path 当键）→ 3 个键 → 红。
    """
    import asyncio

    from swarm.brain import nodes

    real = tmp_path / "proj"
    real.mkdir()
    resolved = str(real.resolve())

    fresh_locks: dict = {}
    monkeypatch.setattr(nodes, "_project_delivery_locks", fresh_locks)
    # 内层 git 写临界区打桩——本锁测的是【锁键归一】，不是交付本身
    monkeypatch.setattr(nodes, "_deliver_merged_diff_locked",
                        lambda *a, **k: {"ap": {"ok": True}})

    cwd = os.getcwd()
    try:
        os.chdir(str(tmp_path))
        for p in (resolved, resolved + "/", "proj"):
            asyncio.run(nodes._deliver_merged_diff_serialized(p, "", None, [], "t1"))
    finally:
        os.chdir(cwd)
    assert len(fresh_locks) == 1, \
        f"等价路径必须落同一把交付锁，实得 {len(fresh_locks)} 把: {list(fresh_locks)}"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
