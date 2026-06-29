#!/usr/bin/env python3
"""read_file 跨子任务文件同步治本单测（裸文件名定位 + producer 未落地止转）。

覆盖第七轮 996 发现①：消费方反复读裸类名(无包路径)在 /workspace 根读不到 →
空转 45 次。治本=按 basename 在树中定位重读；确不存在时给【止转】信号。
本地模式(无沙箱)即可验证，_find_by_basename 走 rglob 与沙箱 find 同义。
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _setup(root: Path, allow_any: bool = True, readable=None):
    from swarm.tools.build_tools import clear_sandbox_context
    from swarm.tools.scope_guard import set_scope
    from swarm.types import FileScope

    clear_sandbox_context()  # 强制本地模式
    os.environ["SWARM_WORKSPACE_ROOT"] = str(root)
    set_scope(FileScope(readable=readable or [], allow_any=allow_any))


def test_bare_filename_resolves_to_nested_path():
    """裸类名 → 按 basename 定位到完整包路径并重读（带提示）。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        nested = root / "src/main/java/com/ruoyi/alarm/AlarmNotifyUserController.java"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_text("package com.ruoyi.alarm;\nclass X {}\n", encoding="utf-8")
        _setup(root)
        from swarm.tools.scope_guard import clear_scope
        try:
            from swarm.tools.file_tools import read_file

            out = read_file.invoke({"path": "AlarmNotifyUserController.java"})
            assert "已按文件名定位到完整路径" in out, out
            assert "src/main/java/com/ruoyi/alarm/AlarmNotifyUserController.java" in out
            assert "package com.ruoyi.alarm" in out
            print("  ✅ 裸文件名定位到完整包路径并重读")
        finally:
            clear_scope()


def test_missing_file_returns_stop_spin_signal():
    """确不存在(producer 未落地) → 明确止转信号，劝勿反复重读。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _setup(root)
        from swarm.tools.scope_guard import clear_scope
        try:
            from swarm.tools.file_tools import read_file

            out = read_file.invoke({"path": "NotYetGenerated.java"})
            assert "尚不存在" in out, out
            assert "请勿反复重读" in out, out
            print("  ✅ producer 未落地给止转信号")
        finally:
            clear_scope()


def test_missing_full_path_also_stops_spin():
    """完整包路径但文件未落地(producer 未建出) → 同样给止转信号，非通用读取失败。

    锁定治本批次#2：消费方读尚未生成的依赖产物，得到明确"勿反复重读"而非
    可重试假象的"读取失败"，配合 build 侧 internal_pkg_not_built BLOCKED 退避。
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _setup(root)
        from swarm.tools.scope_guard import clear_scope
        try:
            from swarm.tools.file_tools import read_file

            out = read_file.invoke(
                {"path": "src/main/java/com/ruoyi/alarm/AlarmNotifyUser.java"}
            )
            assert "尚不存在" in out and "请勿反复重读" in out, out
            print("  ✅ 完整路径未落地也给止转信号")
        finally:
            clear_scope()


def test_multiple_matches_lists_candidates():
    """多个同名文件 → 列候选要求用完整路径，不擅自挑一个。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for sub in ("mod_a/com/x", "mod_b/com/y"):
            p = root / sub / "Util.java"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("class Util{}\n", encoding="utf-8")
        _setup(root)
        from swarm.tools.scope_guard import clear_scope
        try:
            from swarm.tools.file_tools import read_file

            out = read_file.invoke({"path": "Util.java"})
            assert "多个同名文件" in out, out
            assert "mod_a/com/x/Util.java" in out and "mod_b/com/y/Util.java" in out
            print("  ✅ 多命中列候选不擅选")
        finally:
            clear_scope()


def test_normal_full_path_read_unaffected():
    """既有行为不回归：完整路径正常读取，不触发定位逻辑。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        p = root / "src/Foo.java"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("hello\nworld\n", encoding="utf-8")
        _setup(root)
        from swarm.tools.scope_guard import clear_scope
        try:
            from swarm.tools.file_tools import read_file

            out = read_file.invoke({"path": "src/Foo.java"})
            assert "1|hello" in out and "2|world" in out
            assert "已按文件名定位" not in out
            print("  ✅ 完整路径读取无回归")
        finally:
            clear_scope()


def main():
    print("\n🧪 read_file 跨子任务文件同步治本单测\n")
    tests = [
        test_bare_filename_resolves_to_nested_path,
        test_missing_file_returns_stop_spin_signal,
        test_missing_full_path_also_stops_spin,
        test_multiple_matches_lists_candidates,
        test_normal_full_path_read_unaffected,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n📊 结果: {passed} 通过, {failed} 失败\n")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
