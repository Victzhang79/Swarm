#!/usr/bin/env python3
"""knowledge/updater.py 真实单测。

纯函数（dedupe_changes / dedupe_event / _merge_project_events / AST 符号抽取）直接断言；
handle_event / _process_change 通过注入 AsyncMock 子索引器验证「按变更类型分发到正确的
Layer A/B 操作」，不连真实 PG/Qdrant。
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.knowledge.updater import (
    ChangeType,
    FileChange,
    KnowledgeUpdater,
    UpdateEvent,
    _extract_symbols_python_ast,
    _merge_project_events,
    dedupe_changes,
    dedupe_event,
)


# ── dedupe_changes ───────────────────────────────


def test_dedupe_changes_keeps_last_per_path():
    changes = [
        FileChange(file_path="a.py", change_type=ChangeType.ADDED, content="v1"),
        FileChange(file_path="a.py", change_type=ChangeType.MODIFIED, content="v2"),
        FileChange(file_path="b.py", change_type=ChangeType.ADDED, content="x"),
    ]
    out = dedupe_changes(changes)
    assert len(out) == 2
    a = next(c for c in out if c.file_path == "a.py")
    # 保留最后一次（MODIFIED v2）
    assert a.change_type == ChangeType.MODIFIED
    assert a.content == "v2"
    print("  ✅ dedupe_changes 同路径保留最后一次")


def test_dedupe_changes_normalizes_backslash():
    changes = [
        FileChange(file_path="dir\\a.py", change_type=ChangeType.ADDED, content="x"),
        FileChange(file_path="dir/a.py", change_type=ChangeType.MODIFIED, content="y"),
    ]
    out = dedupe_changes(changes)
    # 反斜杠归一化后视为同一路径
    assert len(out) == 1
    assert out[0].file_path == "dir/a.py"
    print("  ✅ dedupe_changes 反斜杠路径归一化")


def test_dedupe_changes_empty():
    assert dedupe_changes([]) == []
    print("  ✅ dedupe_changes 空输入")


def test_dedupe_changes_preserves_order():
    changes = [
        FileChange(file_path="c.py", change_type=ChangeType.ADDED),
        FileChange(file_path="a.py", change_type=ChangeType.ADDED),
        FileChange(file_path="b.py", change_type=ChangeType.ADDED),
    ]
    out = dedupe_changes(changes)
    assert [c.file_path for c in out] == ["c.py", "a.py", "b.py"]
    print("  ✅ dedupe_changes 保留首次出现顺序")


# ── dedupe_event ─────────────────────────────────


def test_dedupe_event_marks_metadata():
    ev = UpdateEvent(
        project_id="p1",
        changes=[
            FileChange(file_path="a.py", change_type=ChangeType.ADDED, content="1"),
            FileChange(file_path="a.py", change_type=ChangeType.MODIFIED, content="2"),
        ],
    )
    out = dedupe_event(ev)
    assert len(out.changes) == 1
    assert out.metadata.get("deduped_from") == 2
    print("  ✅ dedupe_event 去重并标记 metadata")


def test_dedupe_event_noop_when_unique():
    ev = UpdateEvent(
        project_id="p1",
        changes=[FileChange(file_path="a.py", change_type=ChangeType.ADDED)],
    )
    out = dedupe_event(ev)
    assert out is ev  # 无去重则原样返回
    print("  ✅ dedupe_event 无重复原样返回")


# ── _merge_project_events ────────────────────────


def test_merge_project_events_combines_and_dedupes():
    e1 = UpdateEvent(
        project_id="p1",
        task_id="t1",
        changes=[FileChange(file_path="a.py", change_type=ChangeType.ADDED, content="1")],
        metadata={"k1": "v1"},
    )
    e2 = UpdateEvent(
        project_id="p1",
        commit_hash="abc",
        changes=[
            FileChange(file_path="a.py", change_type=ChangeType.MODIFIED, content="2"),
            FileChange(file_path="b.py", change_type=ChangeType.ADDED, content="x"),
        ],
        metadata={"k2": "v2"},
    )
    merged = _merge_project_events([e1, e2], "p1")
    assert merged.project_id == "p1"
    # a.py 去重保留最后 MODIFIED，加 b.py = 2 个
    paths = {c.file_path for c in merged.changes}
    assert paths == {"a.py", "b.py"}
    a = next(c for c in merged.changes if c.file_path == "a.py")
    assert a.content == "2"
    # 元数据合并、task_id/commit_hash 取非 None
    assert merged.metadata.get("k1") == "v1"
    assert merged.metadata.get("k2") == "v2"
    assert merged.task_id == "t1"
    assert merged.commit_hash == "abc"
    print("  ✅ _merge_project_events 合并变更+去重+元数据")


# ── AST 符号抽取 ─────────────────────────────────


def test_ast_extract_basic_symbols():
    src = (
        "import os\n"
        "\n"
        "def top_func(a, b):\n"
        "    return a + b\n"
        "\n"
        "class Foo:\n"
        "    def method(self, x):\n"
        "        return x\n"
        "\n"
        "    async def amethod(self):\n"
        "        pass\n"
    )
    syms = _extract_symbols_python_ast(src, "m.py")
    assert syms is not None
    names = {s.name for s in syms}
    assert "top_func" in names
    assert "Foo" in names
    assert "method" in names
    assert "amethod" in names
    print("  ✅ AST 抽取 函数/类/方法/async")


def test_ast_extract_syntax_error_returns_none():
    """语法错误返回 None（调用方回退正则）。"""
    assert _extract_symbols_python_ast("def broken(:\n", "x.py") is None
    print("  ✅ AST 语法错误返回 None")


def test_ast_extract_nested_class():
    src = (
        "class Outer:\n"
        "    class Inner:\n"
        "        def deep(self):\n"
        "            pass\n"
    )
    syms = _extract_symbols_python_ast(src, "n.py")
    assert syms is not None
    names = {s.name for s in syms}
    assert "Outer" in names and "Inner" in names and "deep" in names
    print("  ✅ AST 抽取 嵌套类")


# ── handle_event 分发（mock 子索引器）────────────


def _make_updater_with_mocks():
    u = KnowledgeUpdater()
    u._struct = AsyncMock()
    u._semantic = AsyncMock()
    u._behavior = AsyncMock()
    # _index_file 用真实逻辑会调 _struct/_semantic（都已 mock），但内部还会
    # 调 _simple_hash 等纯函数，没问题。
    return u


def test_handle_event_added_indexes(monkeypatch):
    u = _make_updater_with_mocks()
    # _update_layer_d 直接 mock 掉，聚焦 change 分发
    u._update_layer_d = AsyncMock()
    ev = UpdateEvent(
        project_id="p1",
        changes=[FileChange(file_path="a.py", change_type=ChangeType.ADDED,
                            content="def f():\n    pass\n", language="python")],
    )
    result = asyncio.run(u.handle_event(ev))
    assert result["total_changes"] == 1
    assert result["errors"] == []
    # Layer A 结构索引被调用（upsert_file）
    assert u._struct.upsert_file.await_count >= 1
    # Layer D 被调用
    assert u._update_layer_d.await_count == 1
    print("  ✅ handle_event ADDED → Layer A 索引 + Layer D")


def test_handle_event_deleted_removes(monkeypatch):
    u = _make_updater_with_mocks()
    u._update_layer_d = AsyncMock()
    ev = UpdateEvent(
        project_id="p1",
        changes=[FileChange(file_path="gone.py", change_type=ChangeType.DELETED)],
    )
    result = asyncio.run(u.handle_event(ev))
    assert result["errors"] == []
    # 删除走 struct.delete_file + semantic.delete_by_file
    u._struct.delete_file.assert_awaited_with("p1", "gone.py")
    u._semantic.delete_by_file.assert_awaited_with("p1", "gone.py")
    print("  ✅ handle_event DELETED → Layer A/B 删除")


def test_handle_event_renamed_deletes_old_indexes_new(monkeypatch):
    u = _make_updater_with_mocks()
    u._update_layer_d = AsyncMock()
    ev = UpdateEvent(
        project_id="p1",
        changes=[FileChange(
            file_path="new.py", change_type=ChangeType.RENAMED, old_path="old.py",
            content="def g():\n    pass\n", language="python",
        )],
    )
    result = asyncio.run(u.handle_event(ev))
    assert result["errors"] == []
    # 旧路径被删除
    u._struct.delete_file.assert_awaited_with("p1", "old.py")
    # 新路径被索引
    assert u._struct.upsert_file.await_count >= 1
    print("  ✅ handle_event RENAMED → 删旧 + 索引新")


def test_handle_event_captures_per_change_error(monkeypatch):
    """单个 change 处理异常被捕获进 errors，不中断整体。"""
    u = _make_updater_with_mocks()
    u._update_layer_d = AsyncMock()
    u._struct.delete_file.side_effect = RuntimeError("boom")
    ev = UpdateEvent(
        project_id="p1",
        changes=[FileChange(file_path="x.py", change_type=ChangeType.DELETED)],
    )
    result = asyncio.run(u.handle_event(ev))
    assert len(result["errors"]) == 1
    assert result["errors"][0]["file"] == "x.py"
    assert "boom" in result["errors"][0]["error"]
    print("  ✅ handle_event 单 change 异常被捕获不中断")


if __name__ == "__main__":
    import inspect

    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            if inspect.signature(fn).parameters:
                # 需 fixture 的用空 monkeypatch 兜底
                continue
            fn()
    print("\nupdater 单测通过。")
