#!/usr/bin/env python3
"""#5(b) round22：ingest 任意文件读（LFI）路径归属校验。

根因：上传端点写入侧干净，但消费侧（create_task→ingest）从不复核 uploaded_files 路径落在
uploads 目录内 → 任意 task:create 用户可传 `["/绝对路径.txt"]` 读服务器任意可读文件，
内容并入 task_description 回显。

治本：path_is_within_uploads(p)（resolve 后判归属，防 ../ 与 symlink）在 create_task 入口
拒绝越界路径 + ingest_node defense-in-depth 过滤。
"""
from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.api.routers.upload import path_is_within_uploads, _uploads_root  # noqa: E402


def test_inside_uploads_allowed():
    root = _uploads_root()
    p = root / "batch123" / "PRD.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")
    try:
        assert path_is_within_uploads(str(p)) is True
        print("  ✅ uploads 内路径 → 放行")
    finally:
        p.unlink(missing_ok=True)


def test_absolute_outside_rejected():
    assert path_is_within_uploads("/etc/hosts") is False
    assert path_is_within_uploads("/etc/passwd") is False
    print("  ✅ 绝对越界路径 → 拒绝")


def test_dotdot_traversal_rejected():
    root = _uploads_root()
    evil = str(root / ".." / ".." / "etc" / "hosts")
    assert path_is_within_uploads(evil) is False
    print("  ✅ ../ 穿越 → 拒绝")


def test_symlink_inside_pointing_outside_rejected():
    root = _uploads_root()
    link = root / "evil_link.txt"
    target = Path(tempfile.gettempdir()) / "lfi_target_round22.txt"
    target.write_text("secret")
    try:
        if link.exists() or link.is_symlink():
            link.unlink()
        os.symlink(target, link)
        # resolve 跟随 symlink → 指向 uploads 外 → 拒绝
        assert path_is_within_uploads(str(link)) is False
        print("  ✅ uploads 内指向外部的 symlink → 拒绝")
    finally:
        if link.is_symlink() or link.exists():
            link.unlink()
        target.unlink(missing_ok=True)


def test_empty_and_garbage_rejected():
    assert path_is_within_uploads("") is False
    assert path_is_within_uploads("relative/x.txt") is False  # 相对路径非 uploads 内
    print("  ✅ 空/相对垃圾路径 → 拒绝(fail-closed)")


if __name__ == "__main__":
    test_inside_uploads_allowed()
    test_absolute_outside_rejected()
    test_dotdot_traversal_rejected()
    test_symlink_inside_pointing_outside_rejected()
    test_empty_and_garbage_rejected()
    print("\n✅ #5(b) ingest LFI 路径归属校验全部通过")
