#!/usr/bin/env python3
"""KB 调度 / 去重 / repair 单元测试"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.knowledge.updater import (
    ChangeType,
    FileChange,
    UpdateEvent,
    dedupe_changes,
    dedupe_event,
    hydrate_event_changes,
    _merge_project_events,
)


def test_dedupe_changes_last_wins():
    changes = [
        FileChange("a.py", ChangeType.MODIFIED, content="v1"),
        FileChange("a.py", ChangeType.MODIFIED, content="v2"),
        FileChange("b.py", ChangeType.ADDED, content="b"),
    ]
    out = dedupe_changes(changes)
    assert len(out) == 2
    assert out[0].file_path == "a.py"
    assert out[0].content == "v2"
    assert out[1].file_path == "b.py"
    print("  ✅ dedupe_changes last wins")


def test_dedupe_event_metadata():
    event = UpdateEvent(
        project_id="p1",
        changes=[
            FileChange("x.py", ChangeType.MODIFIED),
            FileChange("x.py", ChangeType.DELETED),
        ],
    )
    out = dedupe_event(event)
    assert len(out.changes) == 1
    assert out.changes[0].change_type == ChangeType.DELETED
    assert out.metadata.get("deduped_from") == 2
    print("  ✅ dedupe_event metadata")


def test_hydrate_from_project_path(tmp_path: Path):
    f = tmp_path / "foo.py"
    f.write_text("print('hi')\n", encoding="utf-8")
    event = UpdateEvent(
        project_id="p1",
        changes=[FileChange("foo.py", ChangeType.MODIFIED)],
        metadata={"project_path": str(tmp_path)},
    )
    hydrated = hydrate_event_changes(event)
    assert hydrated.changes[0].content == "print('hi')\n"
    assert hydrated.changes[0].language == "python"
    print("  ✅ hydrate_event_changes")


def test_incremental_update_enqueues():
    import asyncio
    from unittest.mock import AsyncMock, patch

    from swarm.knowledge.hooks import incremental_update_from_task

    with patch("swarm.knowledge.hooks.enqueue_kb_update", new_callable=AsyncMock) as mock_eq:
        mock_eq.return_value = 42
        with patch("swarm.knowledge.hooks._build_changes") as mock_bc:
            mock_bc.return_value = [
                FileChange("a.py", ChangeType.MODIFIED, content="x"),
            ]
            result = asyncio.run(
                incremental_update_from_task("p1", "/tmp", "--- diff ---", task_id="t1")
            )
    assert result["status"] == "queued"
    assert result["event_id"] == 42
    mock_eq.assert_awaited_once()
    print("  ✅ incremental_update enqueues")


def test_merge_project_events_dedup():
    """同项目多事件合并：同文件只保留最后一次变更"""
    events = [
        UpdateEvent("p1", changes=[
            FileChange("a.py", ChangeType.ADDED, content="v1"),
            FileChange("b.py", ChangeType.MODIFIED, content="b1"),
        ]),
        UpdateEvent("p1", changes=[
            FileChange("a.py", ChangeType.DELETED),         # a.py 最终删除
            FileChange("c.py", ChangeType.ADDED, content="c"),
        ]),
    ]
    merged = _merge_project_events(events, "p1")
    assert merged.project_id == "p1"
    assert len(merged.changes) == 3
    # a.py 保留最后一条 (DELETED)
    a = [c for c in merged.changes if c.file_path == "a.py"][0]
    assert a.change_type == ChangeType.DELETED
    # 合并 metadata 应记录来源
    assert merged.metadata["batch_merged_from"] == 2
    assert merged.metadata["batch_changes_before_dedup"] == 4
    print("  ✅ _merge_project_events dedup across events")


def test_merge_project_events_single():
    """单事件合并后不变"""
    events = [
        UpdateEvent("p1", task_id="t1", changes=[
            FileChange("a.py", ChangeType.ADDED, content="v1"),
        ]),
    ]
    merged = _merge_project_events(events, "p1")
    assert len(merged.changes) == 1
    assert merged.task_id == "t1"
    # 单事件不写 batch_merged_from
    assert "batch_merged_from" not in merged.metadata
    print("  ✅ _merge_project_events single event no-op")


def test_merge_project_events_last_field_wins():
    """多事件字段取最后一个非 None 值"""
    events = [
        UpdateEvent("p1", task_id="t1", commit_hash="h1", author="alice"),
        UpdateEvent("p1", task_id="t2", commit_hash=None, author="bob"),
    ]
    merged = _merge_project_events(events, "p1")
    assert merged.task_id == "t2"
    assert merged.commit_hash == "h1"
    assert merged.author == "bob"
    print("  ✅ _merge_project_events last non-None field wins")


def test_process_pending_events_batches_by_project():
    """process_pending_events 按项目合并，handle_event 每项目调一次"""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from swarm.knowledge.updater import KnowledgeUpdater

    updater = KnowledgeUpdater()

    # 模拟两行：同项目两个事件 + 另一项目一个事件
    rows = [
        (1, "proj-a", {
            "project_id": "proj-a",
            "task_id": "t1",
            "changes": [
                {"file_path": "a.py", "change_type": "modified", "old_path": None, "language": None},
            ],
            "metadata": {"project_path": "/tmp"},
        }),
        (2, "proj-a", {
            "project_id": "proj-a",
            "task_id": "t2",
            "changes": [
                {"file_path": "a.py", "change_type": "deleted", "old_path": None, "language": None},
            ],
            "metadata": {},
        }),
        (3, "proj-b", {
            "project_id": "proj-b",
            "changes": [
                {"file_path": "b.py", "change_type": "added", "old_path": None, "language": None},
            ],
            "metadata": {"project_path": "/tmp2"},
        }),
    ]
    mock_cursor = AsyncMock()
    mock_cursor.fetchall.return_value = rows

    # cursor() 返回异步上下文管理器
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_cursor)
    ctx.__aexit__ = AsyncMock(return_value=False)

    # 用 MagicMock 而非 AsyncMock，这样 cursor() 是同步调用返回 ctx
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = ctx
    updater._conn = mock_conn

    # mock handle_event 检查调用参数
    handle_calls = []

    async def mock_handle(event):
        handle_calls.append(event)

    updater.handle_event = mock_handle

    processed = asyncio.run(updater.process_pending_events(batch_size=10))
    # 3 个事件都应被处理
    assert processed == 3
    # handle_event 应只被调用 2 次（proj-a 合并一次 + proj-b 一次）
    assert len(handle_calls) == 2
    # proj-a 合并后 a.py 应为 DELETED
    proj_a_event = [e for e in handle_calls if e.project_id == "proj-a"][0]
    assert len(proj_a_event.changes) == 1
    assert proj_a_event.changes[0].change_type == ChangeType.DELETED
    # proj-b 只有一个变更
    proj_b_event = [e for e in handle_calls if e.project_id == "proj-b"][0]
    assert len(proj_b_event.changes) == 1

    # 验证 UPDATE done 使用了 ANY 批量标记
    execute_calls = mock_cursor.execute.call_args_list
    done_calls = [c for c in execute_calls if "done" in str(c)]
    assert len(done_calls) == 2  # 两个项目各一次
    # execute 参数: (sql, (event_ids,))，所以 args[0][1] 是 event_ids list
    assert done_calls[0][0][1][0] == [1, 2]  # proj-a: event_ids=[1,2]
    assert done_calls[1][0][1][0] == [3]     # proj-b: event_ids=[3]
    print("  ✅ process_pending_events batches by project")


def main() -> int:
    import tempfile

    tests = [
        test_dedupe_changes_last_wins,
        test_dedupe_event_metadata,
        lambda: test_hydrate_from_project_path(Path(tempfile.mkdtemp())),
        test_incremental_update_enqueues,
        test_merge_project_events_dedup,
        test_merge_project_events_single,
        test_merge_project_events_last_field_wins,
        test_process_pending_events_batches_by_project,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
            import traceback

            traceback.print_exc()
    if failed:
        print(f"\n{failed}/{len(tests)} failed")
        return 1
    print(f"\n✅ 全部 {len(tests)} 项 KB scheduler 测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
