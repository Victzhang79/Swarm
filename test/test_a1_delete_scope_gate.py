"""A1 治本（round22 假绿门）：纯删除子任务的交付诚实性。

两个根因，一并测：
  1) 交付漏删：delete_files 不在 _writable_files（不上传/不拉回），且【没有任何机制
     把删除传播到本地工作树】→ git diff 永远看不到删除 → 交付的 merged_diff 缺删除，
     且纯删除子任务恒空 diff。治本：_apply_local_deletions 依据"沙箱里该文件是否还在"
     把 worker 真删掉的文件在本地也 unlink，使 diff 如实显示删除。
  2) 假绿：_deterministic_l1_gate 的 expects_changes 只算 writable|create_files，漏
     delete_files → 删除-only scope 空 diff 走 BENIGN → 回退 LLM 弱信号 PASS（删除从未发生）。
     治本：expects_changes 纳入 delete_files → 未传播成功的删除(空 diff)判 False（fail-closed）。

行为测试，不焊死实现结构。
"""
from __future__ import annotations

from unittest.mock import patch

from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality
from swarm.worker.executor import WorkerExecutor


def _mk_executor(scope: FileScope, *, project_path: str = "/tmp/swarm-a1-test") -> WorkerExecutor:
    st = SubTask(
        id="st-a1-1",
        description="删除废弃的 LegacyFilter",
        difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=scope,
        intent="modify",
    )
    return WorkerExecutor(subtask=st, project_path=project_path)


# ── 根因 2：删除-only scope 空 diff 判 False（不再假绿） ──

def test_delete_only_empty_diff_gate_fails():
    """纯删除 scope + 空 diff（删除未传播/未执行）→ 确定性闸门判 False，不回退 LLM。"""
    scope = FileScope(delete_files=["com/x/LegacyFilter.java"])
    ex = _mk_executor(scope)
    with patch.object(ex, "_get_git_diff", return_value="(无变更)"):
        det_ok, details = ex._deterministic_l1_gate()
    assert det_ok is False, details
    assert details.get("reason") == "empty_diff_but_changes_expected", details


def test_delete_plus_writable_empty_diff_still_fails():
    """混合 scope（删除+可写）空 diff 同样判 False（回归保护）。"""
    scope = FileScope(writable=["com/x/A.java"], delete_files=["com/x/Old.java"])
    ex = _mk_executor(scope)
    with patch.object(ex, "_get_git_diff", return_value=""):
        det_ok, details = ex._deterministic_l1_gate()
    assert det_ok is False, details


def test_readonly_scope_empty_diff_still_benign():
    """回归：纯只读 scope（无 writable/create/delete）空 diff 仍是 BENIGN 三态 None。"""
    scope = FileScope(readable=["only_read.py"])
    ex = _mk_executor(scope)
    with patch.object(ex, "_get_git_diff", return_value="(无变更)"):
        det_ok, details = ex._deterministic_l1_gate()
    assert det_ok is None, details


def test_real_deletion_diff_not_empty_path(tmp_path):
    """删除真发生（diff 非空）→ 不落入 empty_diff 分支（交给 pipeline 裁决）。"""
    scope = FileScope(delete_files=["com/x/Old.java"])
    ex = _mk_executor(scope, project_path=str(tmp_path))
    real_del = "--- a/com/x/Old.java\n+++ /dev/null\n@@ -1 +0 @@\n-gone\n"
    with patch.object(ex, "_get_git_diff", return_value=real_del), \
         patch("swarm.worker.l1_pipeline.run_l1_pipeline", return_value=(True, {})):
        det_ok, details = ex._deterministic_l1_gate()
    # 非空 diff：绝不走 empty_diff_but_changes_expected 快速失败
    assert details.get("reason") != "empty_diff_but_changes_expected", details


# ── 根因 1：删除传播到本地工作树 ──

def test_apply_local_deletions_unlinks_when_worker_deleted(tmp_path):
    """worker 在沙箱删掉了文件（沙箱里已无）→ 本地同步 unlink，使 diff 显示删除。"""
    scope = FileScope(delete_files=["com/x/Old.java"])
    ex = _mk_executor(scope, project_path=str(tmp_path))
    target = tmp_path / "com" / "x" / "Old.java"
    target.parent.mkdir(parents=True)
    target.write_text("legacy")
    # 沙箱文件集不含该文件 → worker 已删
    deleted = ex._apply_local_deletions(tmp_path, sandbox_files=set())
    assert not target.exists(), "worker 删掉的文件应在本地 unlink"
    assert "com/x/Old.java" in deleted


def test_apply_local_deletions_keeps_when_worker_did_not_delete(tmp_path):
    """worker 没删（沙箱里文件仍在）→ 本地保留 → diff 空 → 上游 expects_changes 判未完成。"""
    scope = FileScope(delete_files=["com/x/Old.java"])
    ex = _mk_executor(scope, project_path=str(tmp_path))
    target = tmp_path / "com" / "x" / "Old.java"
    target.parent.mkdir(parents=True)
    target.write_text("legacy")
    deleted = ex._apply_local_deletions(tmp_path, sandbox_files={"com/x/Old.java"})
    assert target.exists(), "worker 未删的文件不应被本地误删"
    assert deleted == []


if __name__ == "__main__":
    import tempfile
    from pathlib import Path as _P
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        if "tmp_path" in fn.__code__.co_varnames:
            with tempfile.TemporaryDirectory() as d:
                fn(_P(d))
        else:
            fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== A1 删除交付诚实性: {len(fns)}/{len(fns)} passed ===")
