"""R65TR-T1：_git_tracked_set 三态化——"git 故障"与"候选权威不在 base 树"必须可区分。

round65 治后回放实证：clean_upload 的"tracked 判定空集但仓库确有 tracked 文件
（git 故障/超时/base_ref 异常）"WARN 59/59 每次 bootstrap 必现，但任务全程
[git_tracked_set] 真故障告警 0 条——空集全部是 ls-tree 成功的权威答案（writable
均为 plan 新建文件，钉扎基线里本不存在=棕地功能新增的预期形态）。二分诊断
（greenfield 仓库 vs 假定 git 故障）漏掉这个主流第三态，两轮（round65d 55/88、
治后回放 59/59）把定义使然刷成故障告警，噪音淹没真信号。

治本：_git_tracked_set 返回 set | None——None=git 故障（rc≠0/异常），空集=权威
"候选不在 base 树"。两处调用方（workspace reset / clean_upload）按三态分流：
None → 真 warning 级留痕+降级；成功空集 → 定义使然 INFO；有 tracked → 照常。
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

from swarm.types import FileScope


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo_tristate"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "tracked.py").write_text("# HEAD\n")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


# ── 判定层三态 ──────────────────────────────────────────────────────────


def test_git_tracked_set_success_with_tracked(tmp_path):
    from swarm.worker.executor_sync import _git_tracked_set

    repo = _make_repo(tmp_path)
    got = _git_tracked_set(repo, ["tracked.py", "ghost.py"], "HEAD")
    assert got == {"tracked.py"}


def test_git_tracked_set_success_all_untracked_is_empty_set_not_none(tmp_path):
    """棕地新建预期形态：ls-tree 成功、候选不在 base 树 → 空集（权威答案），绝非 None。"""
    from swarm.worker.executor_sync import _git_tracked_set

    repo = _make_repo(tmp_path)
    (repo / "new_a.py").write_text("x\n")
    got = _git_tracked_set(repo, ["new_a.py", "new_b.py"], "HEAD")
    assert got is not None, "成功空集是权威答案，不得与故障混淆"
    assert got == set()


def test_git_tracked_set_failure_returns_none(tmp_path):
    """git 故障（坏 ref → rc≠0）→ None，调用方才能与权威空集分流。"""
    from swarm.worker.executor_sync import _git_tracked_set

    repo = _make_repo(tmp_path)
    got = _git_tracked_set(repo, ["tracked.py"], "no-such-ref-r65tr")
    assert got is None


# ── clean_upload 调用方分流 ─────────────────────────────────────────────


def _mk_sync_stub(repo: Path, logs: list[tuple[str, str]]):
    from swarm.worker.executor import WorkerExecutor

    captured = {}

    class _Mgr:
        def sync_files_to_sandbox(self, sandbox, local_root, rel_files, remote_root):
            captured["contents"] = {}
            for rel in rel_files:
                p = Path(local_root) / rel
                captured["contents"][rel] = p.read_text() if p.is_file() else None
            return {"uploaded": len(rel_files), "errors": [], "files": rel_files}

    stub = SimpleNamespace()
    stub.project_path = str(repo)
    stub._sandbox = object()
    stub._sandbox_manager = _Mgr()
    stub._log = lambda m, level="info": logs.append((level, m))
    stub._writable_files = WorkerExecutor._writable_files.__get__(stub)
    stub._norm_rel = WorkerExecutor._norm_rel
    stub._git_baseline_text = WorkerExecutor._git_baseline_text.__get__(stub)
    stub._snapshot_scope_local = WorkerExecutor._snapshot_scope_local.__get__(stub)
    stub._sync_to_sandbox = WorkerExecutor._sync_to_sandbox.__get__(stub)
    return stub, captured


def _patch_cfg(monkeypatch):
    import swarm.worker.executor as ex_mod
    monkeypatch.setattr(ex_mod, "get_config", lambda: SimpleNamespace(
        sandbox=SimpleNamespace(sandbox_remote_workdir="/workspace")
    ))


def test_clean_upload_untracked_writables_logged_as_expected_form(tmp_path, monkeypatch):
    """棕地仓库 + writable 全新建（上一轮产物被降级 writable-modify）→
    定义使然 INFO，绝不刷"git 故障/护栏未生效"WARN；按磁盘上传照常。"""
    repo = _make_repo(tmp_path)
    (repo / "feature_new.py").write_text("attempt-1 dirty\n")

    logs: list[tuple[str, str]] = []
    stub, captured = _mk_sync_stub(repo, logs)
    stub.effective_scope = FileScope(writable=["feature_new.py"], readable=[], create_files=[])
    stub._scope_files = lambda: ["feature_new.py"]
    _patch_cfg(monkeypatch)

    asyncio.run(stub._sync_to_sandbox("bootstrap"))

    joined = " | ".join(m for _, m in logs)
    assert "git 故障" not in joined, f"权威空集不得误诊为 git 故障: {joined}"
    assert "[WARN]" not in joined, f"定义使然形态不得刷 WARN 字样: {joined}"
    assert not any(lv == "warning" for lv, _ in logs), f"不得用 warning 级别: {logs}"
    assert "定义使然" in joined or "不适用" in joined, \
        f"应有定义使然/不适用的诚实说明: {joined}"
    # untracked writable 按磁盘上传（HEAD 无此版，唯一可行行为）
    assert captured["contents"]["feature_new.py"] == "attempt-1 dirty\n"


def test_clean_upload_git_fault_is_true_warning(tmp_path, monkeypatch):
    """判定层返回 None（git 故障）→ 真 warning 级留痕（护栏确实失效），降级脏盘上传。"""
    import swarm.worker.executor_sync as ex_sync

    repo = _make_repo(tmp_path)
    (repo / "tracked.py").write_text("# HEAD\n# DIRTY\n")

    logs: list[tuple[str, str]] = []
    stub, captured = _mk_sync_stub(repo, logs)
    stub.effective_scope = FileScope(writable=["tracked.py"], readable=[], create_files=[])
    stub._scope_files = lambda: ["tracked.py"]
    _patch_cfg(monkeypatch)
    monkeypatch.setattr(ex_sync, "_git_tracked_set", lambda *a, **k: None)

    asyncio.run(stub._sync_to_sandbox("bootstrap"))

    warn_msgs = [m for lv, m in logs if lv == "warning"]
    assert warn_msgs and any("故障" in m and "护栏" in m for m in warn_msgs), \
        f"git 故障必须真 warning 级留痕: {logs}"
    # 故障降级：按脏磁盘上传（fail-open 保执行，但可观测）
    assert captured["contents"]["tracked.py"] == "# HEAD\n# DIRTY\n"


def test_clean_upload_tracked_head_version_still_works(tmp_path, monkeypatch):
    """回归锁：tracked writable 干净上传（HEAD 版）不受三态化影响。"""
    repo = _make_repo(tmp_path)
    (repo / "tracked.py").write_text("# HEAD\n# DIRTY\n")

    logs: list[tuple[str, str]] = []
    stub, captured = _mk_sync_stub(repo, logs)
    stub.effective_scope = FileScope(writable=["tracked.py"], readable=[], create_files=[])
    stub._scope_files = lambda: ["tracked.py"]
    _patch_cfg(monkeypatch)

    asyncio.run(stub._sync_to_sandbox("bootstrap"))

    assert captured["contents"]["tracked.py"] == "# HEAD\n"
    assert not any(lv == "warning" for lv, _ in logs), f"干净路径不得告警: {logs}"


# ── workspace reset 调用方分流 ──────────────────────────────────────────


def _mk_reset_stub(repo: Path, logs: list[tuple[str, str]], writable: list[str]):
    from swarm.worker.executor import WorkerExecutor

    stub = SimpleNamespace()
    stub.project_path = str(repo)
    stub.effective_scope = FileScope(writable=writable, readable=[], create_files=[])
    stub._log = lambda m, level="info": logs.append((level, m))
    stub._writable_files = WorkerExecutor._writable_files.__get__(stub)
    stub._norm_rel = WorkerExecutor._norm_rel
    stub._reset_scope_to_head = WorkerExecutor._reset_scope_to_head.__get__(stub)
    return stub


def test_workspace_reset_git_fault_warns_and_degrades(tmp_path, monkeypatch):
    """reset 调用方对 None 的对称分流：真故障 warning 留痕+跳过 reset（原实现把
    None 当空集走定义使然 INFO=真故障被静默降级）。"""
    import swarm.worker.executor_sync as ex_sync

    repo = _make_repo(tmp_path)
    (repo / "tracked.py").write_text("# HEAD\n# DIRTY\n")

    logs: list[tuple[str, str]] = []
    stub = _mk_reset_stub(repo, logs, writable=["tracked.py"])
    monkeypatch.setattr(ex_sync, "_git_tracked_set", lambda *a, **k: None)

    n = stub._reset_scope_to_head()

    assert n == 0
    warn_msgs = [m for lv, m in logs if lv == "warning"]
    assert warn_msgs and any("故障" in m for m in warn_msgs), \
        f"git 故障必须 warning 级留痕: {logs}"
    # 故障时绝不动工作树
    assert (repo / "tracked.py").read_text() == "# HEAD\n# DIRTY\n"


def test_workspace_reset_authoritative_empty_keeps_expected_form_info(tmp_path):
    """回归锁（G1-1a 语义保持）：成功空集（writable 全新建）→ 定义使然 INFO 非告警。"""
    repo = _make_repo(tmp_path)
    (repo / "brand_new.py").write_text("x\n")

    logs: list[tuple[str, str]] = []
    stub = _mk_reset_stub(repo, logs, writable=["brand_new.py"])

    n = stub._reset_scope_to_head()

    assert n == 0
    assert not any(lv == "warning" for lv, _ in logs), f"定义使然不得告警: {logs}"
    joined = " | ".join(m for _, m in logs)
    assert "定义使然" in joined


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
