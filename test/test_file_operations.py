#!/usr/bin/env python3
"""文件操作语义（create/modify/delete）+ 中文文件名抠取 回归测试。

防止两个真实 bug：
1. 中文粘连：'输出readme.md' 旧正则 → '输出readme.md'（假文件名），worker open 必崩。
2. 增删改不分：'新增 a.py 删除 b.py' 全塞 writable，worker 不知道该建还是该删。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ── 文件名抠取：中文不粘连 ───────────────────────


def test_guess_files_no_cjk_contamination():
    from swarm.brain.nodes import _guess_target_files

    assert _guess_target_files("输出一个项目框架readme.md出来") == ["readme.md"]
    assert _guess_target_files("修改 src/dotenv/parser.py 文件") == ["src/dotenv/parser.py"]
    assert _guess_target_files("没有文件名的纯中文需求") == []
    print("  ✅ 文件名抠取无中文粘连")


# ── 操作分类：create / modify / delete ──────────


def test_classify_create():
    from swarm.brain.nodes import _classify_file_ops

    ops = _classify_file_ops("输出一个项目框架readme.md出来")
    assert ops["create"] == ["readme.md"]
    assert ops["modify"] == [] and ops["delete"] == []
    print("  ✅ '输出 readme' → create")


def test_classify_mixed_ops():
    from swarm.brain.nodes import _classify_file_ops

    ops = _classify_file_ops(
        "新增一个用户登录模块 login.py 和 auth.py，删除旧的 session.py，修改 config.py"
    )
    assert set(ops["create"]) == {"login.py", "auth.py"}
    assert ops["delete"] == ["session.py"]
    assert ops["modify"] == ["config.py"]
    print("  ✅ 增删改混合需求正确分类")


def test_classify_delete_then_create():
    from swarm.brain.nodes import _classify_file_ops

    ops = _classify_file_ops("删除 old_utils.py 并创建 new_utils.py")
    assert ops["delete"] == ["old_utils.py"]
    assert ops["create"] == ["new_utils.py"]
    print("  ✅ '删除X并创建Y' 分别归类")


def test_classify_default_modify():
    from swarm.brain.nodes import _classify_file_ops

    ops = _classify_file_ops("在 parser.py 顶部加一行注释")
    assert ops["modify"] == ["parser.py"]
    assert ops["create"] == [] and ops["delete"] == []
    print("  ✅ 无增删关键词默认 modify")


# ── FileScope 操作语义 ──────────────────────────


def test_filescope_operation_semantics():
    from swarm.types import FileScope

    s = FileScope(
        writable=["a.py"], create_files=["b.py"], delete_files=["c.py"], readable=["d.py"]
    )
    # 三类都算可写权限
    assert s.is_writable("a.py") and s.is_writable("b.py") and s.is_writable("c.py")
    assert not s.is_writable("d.py")  # readable 不可写
    assert s.is_readable("d.py")
    # 操作判定
    assert s.is_create("b.py") and not s.is_create("a.py")
    assert s.is_delete("c.py") and not s.is_delete("a.py")
    assert set(s.all_write_targets()) == {"a.py", "b.py", "c.py"}
    print("  ✅ FileScope create/modify/delete 语义正确")


def test_filescope_backward_compat():
    """只传 writable/readable 的旧代码仍能工作（create/delete 默认空）。"""
    from swarm.types import FileScope

    s = FileScope(writable=["x.py"], readable=["y.py"])
    assert s.is_writable("x.py") and s.is_readable("y.py")
    assert s.create_files == [] and s.delete_files == []
    assert s.all_write_targets() == ["x.py"]
    print("  ✅ FileScope 向后兼容")


# ── _build_simple_plan 端到端 scope 填充 ────────


def test_simple_plan_populates_ops():
    from swarm.brain.nodes import _build_simple_plan

    plan = _build_simple_plan("新增 foo.py，删除 bar.py")
    scope = plan.subtasks[0].scope
    assert "foo.py" in scope.create_files
    assert "bar.py" in scope.delete_files
    print("  ✅ _build_simple_plan 正确填充 create/delete")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\n文件操作语义 单测通过。")
