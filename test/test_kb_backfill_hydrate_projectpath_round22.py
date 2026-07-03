#!/usr/bin/env python3
"""#4(b) round22：KB 回灌缺 project_path 静默 no-op（复现 + 治本）。

根因：dispatch._feedback_to_knowledge 构造的 UpdateEvent 无 content 又无 metadata.project_path；
hydrate_event_changes `if not project_path: return event` 原样返回（content 仍空）→ _process_change
`if change.content:` 空 → _index_file 完全跳过 → Layer A/B 一字不索引（回灌形同虚设）。

治本：hydrate 无 metadata.project_path 时从 event.project_id 兜底 _lookup_project_path
（一处覆盖所有缺 pp 的入队方）。
"""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.knowledge import updater as up  # noqa: E402
from swarm.knowledge.updater import UpdateEvent, FileChange, ChangeType, hydrate_event_changes  # noqa: E402


def _make_event(metadata):
    return UpdateEvent(
        project_id="proj-round22",
        changes=[FileChange(file_path="src/A.java", change_type=ChangeType.ADDED)],  # 无 content
        metadata=metadata,
    )


def test_missing_project_path_falls_back_to_lookup():
    proj = Path(tempfile.mkdtemp())
    (proj / "src").mkdir()
    (proj / "src/A.java").write_text("class A { void f() {} }\n")
    # metadata 缺 project_path（复现回灌场景），但 project_id 可解析
    ev = _make_event(metadata={"source": "worker_feedback"})
    with patch.object(up, "_lookup_project_path", return_value=str(proj)) as m:
        out = hydrate_event_changes(ev)
    assert m.called, "缺 metadata.project_path 时必须回退 _lookup_project_path(event.project_id)"
    assert out.changes[0].content and "class A" in out.changes[0].content, \
        "回退后 content 必须被 hydrate（复现 bug：当前为空 → 索引静默 no-op）"
    print("  ✅ 缺 project_path → 回退 lookup → content 被 hydrate")


def test_metadata_project_path_still_used():
    """不回归：metadata 带 project_path 时直接用，不调 lookup。"""
    proj = Path(tempfile.mkdtemp())
    (proj / "src").mkdir()
    (proj / "src/A.java").write_text("class A {}\n")
    ev = _make_event(metadata={"project_path": str(proj)})
    with patch.object(up, "_lookup_project_path", return_value=None) as m:
        out = hydrate_event_changes(ev)
    assert not m.called, "metadata 有 project_path 时不该多此一举调 lookup"
    assert out.changes[0].content and "class A" in out.changes[0].content
    print("  ✅ metadata 有 project_path → 直接用（不回归）")


def test_no_projectid_no_path_returns_asis():
    """兜底：既无 metadata.project_path 又无 project_id → 原样返回（不崩）。"""
    ev = UpdateEvent(project_id="", changes=[
        FileChange(file_path="src/A.java", change_type=ChangeType.ADDED)], metadata={})
    with patch.object(up, "_lookup_project_path", return_value=None):
        out = hydrate_event_changes(ev)
    assert out.changes[0].content in (None, ""), "无从解析 → content 仍空，安全返回不崩"
    print("  ✅ 无 project_id/path → 原样返回不崩")


if __name__ == "__main__":
    test_missing_project_path_falls_back_to_lookup()
    test_metadata_project_path_still_used()
    test_no_projectid_no_path_returns_asis()
    print("\n✅ #4(b) 回灌 hydrate project_path 兜底全部通过")
