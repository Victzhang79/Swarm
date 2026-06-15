"""P2-1 回归测试：Java 同 package 类自动纳入 readable（避免同模块编译必败）。

复现 task 0f93f1fc：StringUtils.java 引用同包 Constants/StrFormatter/CharsetKit，
但不在可读 scope → mvn compile cannot find symbol → 编译注定失败。
"""
from __future__ import annotations

import os
import tempfile

from swarm.brain.contract_utils import enrich_java_package_readable
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan


def _sub(sid, *, writable=None, create=None, readable=None):
    return SubTask(
        id=sid, description=f"t {sid}",
        difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
        scope=FileScope(writable=writable or [], create_files=create or [], readable=readable or []),
    )


def _make_java_project():
    """构造 ruoyi-like 包目录，含多个同包 .java。"""
    root = tempfile.mkdtemp(prefix="p2_1_java_")
    pkg = os.path.join(root, "ruoyi-common/src/main/java/com/ruoyi/common/utils")
    os.makedirs(pkg, exist_ok=True)
    for name in ["StringUtils.java", "Constants.java", "StrFormatter.java", "CharsetKit.java"]:
        with open(os.path.join(pkg, name), "w") as f:
            f.write("package com.ruoyi.common.utils;\n")
    return root


def test_same_package_classes_added_to_readable():
    root = _make_java_project()
    rel = "ruoyi-common/src/main/java/com/ruoyi/common/utils"
    plan = TaskPlan(
        subtasks=[_sub("st-2", writable=[f"{rel}/StringUtils.java"])],
        parallel_groups=[],
    )
    changed = enrich_java_package_readable(plan, root)
    assert changed is True
    s = plan.subtasks[0]
    # 同包其它类应进 readable
    assert f"{rel}/Constants.java" in s.scope.readable
    assert f"{rel}/StrFormatter.java" in s.scope.readable
    assert f"{rel}/CharsetKit.java" in s.scope.readable
    # 自己的写目标不重复进 readable
    assert f"{rel}/StringUtils.java" not in s.scope.readable
    print("  ✅ P2-1: 同 package 的 Constants/StrFormatter/CharsetKit 自动入 readable")


def test_no_project_path_noop():
    plan = TaskPlan(subtasks=[_sub("st-1", writable=["a/B.java"])], parallel_groups=[])
    assert enrich_java_package_readable(plan, None) is False
    print("  ✅ P2-1: 无 project_path → no-op")


def test_non_java_target_skipped():
    root = _make_java_project()
    plan = TaskPlan(subtasks=[_sub("st-1", writable=["foo/bar.py"])], parallel_groups=[])
    assert enrich_java_package_readable(plan, root) is False
    print("  ✅ P2-1: 非 Java 写目标 → 跳过")


if __name__ == "__main__":
    tests = [
        test_same_package_classes_added_to_readable,
        test_no_project_path_noop,
        test_non_java_target_skipped,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t(); passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {type(e).__name__}: {e}"); failed += 1
    print(f"\n=== P2-1 Java 同包入域: {passed}/{passed+failed} passed ===")
    import sys
    sys.exit(1 if failed else 0)
