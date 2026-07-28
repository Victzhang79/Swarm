"""R67L-B1（22号文批次1）行为测试：C7 上传账对【待新建 create_files】不再误杀。

round67l 三路复盘定案：create_files 语义=待新建，本地不存在是定义使然；旧实现把
全量 rel_files（含 create_files）交给 sync_files_to_sandbox，缺失照记 errors →
_upload_error_rels 入账 → L1 判确定性 FAIL——产物全对的子任务被冤杀，且与 H2 回滚
互锁（FAIL→删本地→下轮必再入账）永不收敛。

治：_sync_to_sandbox 上传清单剔除【scope.create_files 且本地不存在】条目；
writable/readable 缺失维持入账（真矛盾臂不动）；已存在的 create_files（上轮产出
续作）照传不断链。
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.types import (Confidence, FileScope, SubTask, SubTaskDifficulty,  # noqa: E402
                         SubTaskModality, TaskHarness, WorkerOutput)
from swarm.worker.executor import WorkerExecutor  # noqa: E402


def _mk(scope: FileScope, project_path: str) -> WorkerExecutor:
    st = SubTask(
        id="st-r67l", description="R67L-B1", difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT, scope=scope,
        harness=TaskHarness(language="java", build_command=""),
    )
    return WorkerExecutor(subtask=st, project_path=project_path)


def _run_upload(ex: WorkerExecutor, tmp_path: Path) -> dict:
    """驱动 _sync_to_sandbox（baked 分支、clean_upload 关闭免 git），捕获实际上传清单。"""
    captured: dict = {}

    def _fake_sync(sandbox, local_root, rel_files, remote_root=None):
        captured["rel_files"] = list(rel_files)
        captured["local_root"] = str(local_root)
        # 与真身同口径：本地不存在的条目记 errors
        root = Path(local_root)
        errors = [f"{r}: 本地文件不存在" for r in rel_files if not (root / r).is_file()]
        return {"uploaded": len(rel_files) - len(errors), "skipped": 0,
                "errors": errors, "files": list(rel_files)}

    ex._sandbox = SimpleNamespace(sandbox_id="sb-test")
    ex._sandbox_manager = SimpleNamespace(
        sync_files_to_sandbox=_fake_sync,
        append_activity=lambda *a, **k: None)
    ex._sandbox_has_source = True  # baked 分支（E2E 真实路径）
    os.environ["SWARM_WORKER_CLEAN_UPLOAD"] = "false"  # 免 staging/git（单测聚焦过滤逻辑）
    try:
        asyncio.run(ex._sync_to_sandbox("test"))
    finally:
        os.environ.pop("SWARM_WORKER_CLEAN_UPLOAD", None)
    return captured


def test_b1_pending_create_files_excluded_from_upload(tmp_path):
    """待新建 create_files（本地不存在）→ 不进上传清单、不入账（误杀根治）。"""
    ex = _mk(FileScope(create_files=["templates/alarm/task/edit.html"]), str(tmp_path))
    captured = _run_upload(ex, tmp_path)
    assert captured["rel_files"] == [], (
        f"不存在的 create_files 不得进上传清单: {captured['rel_files']}")
    assert ex._upload_error_rels == [], (
        f"待新建缺失不得入 C7 账: {ex._upload_error_rels}")


def test_b1_existing_create_files_still_uploaded(tmp_path):
    """已存在的 create_files（上轮产出续作）→ 照传不断链。"""
    target = tmp_path / "templates/alarm/task/edit.html"
    target.parent.mkdir(parents=True)
    target.write_text("<html>上轮产出</html>")
    ex = _mk(FileScope(create_files=["templates/alarm/task/edit.html"]), str(tmp_path))
    captured = _run_upload(ex, tmp_path)
    assert captured["rel_files"] == ["templates/alarm/task/edit.html"], captured["rel_files"]
    assert ex._upload_error_rels == [], ex._upload_error_rels


def test_b1_missing_writable_still_accounted(tmp_path):
    """writable（modify）本地缺失=plan 声明与磁盘事实的真矛盾 → 维持入账（真臂不动）。"""
    ex = _mk(FileScope(writable=["service/FooService.java"]), str(tmp_path))
    captured = _run_upload(ex, tmp_path)
    assert captured["rel_files"] == ["service/FooService.java"], captured["rel_files"]
    assert ex._upload_error_rels == ["service/FooService.java: 本地文件不存在"], (
        f"writable 缺失必须入账: {ex._upload_error_rels}")


def test_b1_mixed_scope_partition(tmp_path):
    """混合 scope：create 缺失跳过 + writable 缺失入账 + create 存在照传，三者同 run 分流。"""
    existing = tmp_path / "new/Exists.java"
    existing.parent.mkdir(parents=True)
    existing.write_text("class Exists {}")
    ex = _mk(
        FileScope(
            writable=["service/Gone.java"],
            create_files=["new/Pending.java", "new/Exists.java"],
        ),
        str(tmp_path),
    )
    captured = _run_upload(ex, tmp_path)
    assert "new/Pending.java" not in captured["rel_files"], captured["rel_files"]
    assert "new/Exists.java" in captured["rel_files"], captured["rel_files"]
    assert "service/Gone.java" in captured["rel_files"], captured["rel_files"]
    assert ex._upload_error_rels == ["service/Gone.java: 本地文件不存在"], (
        f"只有 writable 缺失可入账: {ex._upload_error_rels}")


# ─── R67L-B1 入口对称：trivial 路径同享 C2 置信度校正（判死不得 high 收尾）───


def _out(confidence, l1_passed, diff):
    return WorkerOutput(subtask_id="st-r67l", diff=diff, summary="s",
                        confidence=confidence, l1_passed=l1_passed)


def test_b1_c2_l1_failed_caps_confidence_low(tmp_path):
    """L1 未过 + LLM 自报 high + 有 diff → 强制 LOW（trivial/full 同口径）。"""
    ex = _mk(FileScope(writable=["A.java"]), str(tmp_path))
    out = ex._c2_calibrate_confidence(
        _out(Confidence.HIGH, False, "--- a/A\n+++ b/A\n@@ -1 +1 @@\n-x\n+y\n"))
    assert out.confidence == Confidence.LOW


def test_b1_c2_empty_diff_forces_low(tmp_path):
    """空 diff + 自报 high（撞上限空转特征）→ 强制 LOW。"""
    ex = _mk(FileScope(writable=["A.java"]), str(tmp_path))
    out = ex._c2_calibrate_confidence(_out(Confidence.HIGH, True, ""))
    assert out.confidence == Confidence.LOW


def test_b1_c2_passed_with_diff_keeps_confidence(tmp_path):
    """回归：L1 过 + 非空 diff → 自报置信度不动。"""
    ex = _mk(FileScope(writable=["A.java"]), str(tmp_path))
    out = ex._c2_calibrate_confidence(_out(Confidence.HIGH, True, "--- a/A\n+++ b/A\n@@ -1 +1 @@\n-x\n+y\n"))
    assert out.confidence == Confidence.HIGH
