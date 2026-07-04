"""A6（round22）：并发 pull-back 非 manifest 裸写 → torn-write。

取证结论（纠偏计划）：
  - 计划原提「把整个 sync 循环套一层 _ProjectGitFlock」会【自死锁】——_ProjectGitFlock 每次
    实例化 open() 一个新 fd，fcntl.flock 锁的是 open file description；sandbox.py 的 manifest
    写入分支【内层】已各自 _ProjectGitFlock，外层再套一把（同进程不同 fd）→ 第二次 LOCK_EX
    永久阻塞。故【不套外层锁】。
  - 交付 diff 的正确性【本已保证】：executor._get_git_diff 在 diff-flock 内先把本 worker 的
    targets 全部重置回【自己 pull-back 的字节】(_post_sync_contents) 再 diff（不变量：diff 是
    (HEAD, 本 worker 自产出) 的纯函数，与他人无关）→ 别的 worker 的 last-write-wins 覆盖被重置
    洗掉，delivered diff 不受影响。
  - 唯一真实残留 = 并发裸 write_bytes 对【同一文件】的 torn-write（半截字节）。治本：原子写
    （同目录 temp + os.replace），deadlock-free、不改锁作用域。

本测试直击原子性：并发写同一文件，结果必是某次【完整】内容，绝不 torn。
"""
from __future__ import annotations

import threading

from swarm.worker.sandbox import _atomic_write_bytes


def test_atomic_write_roundtrip(tmp_path):
    p = tmp_path / "f.txt"
    _atomic_write_bytes(p, b"hello")
    assert p.read_bytes() == b"hello"


def test_atomic_write_overwrites(tmp_path):
    p = tmp_path / "f.txt"
    p.write_bytes(b"old")
    _atomic_write_bytes(p, b"new-content")
    assert p.read_bytes() == b"new-content"


def test_atomic_write_leaves_no_temp(tmp_path):
    p = tmp_path / "f.txt"
    _atomic_write_bytes(p, b"x")
    leftovers = [q.name for q in tmp_path.iterdir() if q.name.startswith(".swarm_tmp_")]
    assert leftovers == [], f"不应残留临时文件: {leftovers}"


def test_concurrent_writes_never_torn(tmp_path):
    """并发写同一文件（不同内容）→ 最终内容必是某次完整写入，绝无半截混合。"""
    p = tmp_path / "big.bin"
    A = b"A" * 200_000
    B = b"B" * 200_000

    def writer(data):
        for _ in range(30):
            _atomic_write_bytes(p, data)

    t1 = threading.Thread(target=writer, args=(A,))
    t2 = threading.Thread(target=writer, args=(B,))
    t1.start(); t2.start(); t1.join(); t2.join()
    got = p.read_bytes()
    assert got in (A, B), "并发原子写结果必是某次完整内容（非 torn）"


if __name__ == "__main__":
    import tempfile
    from pathlib import Path as _P
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        with tempfile.TemporaryDirectory() as d:
            fn(_P(d))
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== A6 原子 pull-back 写: {len(fns)}/{len(fns)} passed ===")
