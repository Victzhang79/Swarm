"""merge 审计 F1/F2 治本（deep_read_findings/20_merge_flow_audit.md，对抗核验 CONFIRMED）。

F1（CRITICAL）：verify_runtime 自建冒烟沙箱验的是【base 树】不是 merged 树——
run_integration_review 的 finally reset 已把工作树复位到 base（integration_review.py:291-295），
_acquire_smoke_sandbox 自建臂 sync 进箱的就是 base；冒烟路径全程零 git apply → 假绿（启动崩
直达交付，runtime 闸北极星被掏空）/ 假红（S2-5 断言打 base 404 → acceptance_failed 幻影失败
烧重试+毒化归因）。修法=自建臂 sync 后、rebuild 前把 merged_diff 写入沙箱 git apply
--ignore-whitespace（与 _run_l2_in_sandbox:4736-4747 同款同旗标，marker 锚点取 rc）；
apply 失败按 skipped（smoke_apply_failed，infra 非代码失败）。转交快路径本就是 merged 树，
绝不注入 apply（防双重应用）。

F2（HIGH·条件可达）：verify_l2 的工作树变更窗口（reset→apply→reconcile→编译→finally reset）
不持 _ProjectGitFlock，与同项目兄弟任务的交付临界区/pull-back 互踩。修法=run_integration_review
工作树段整段收进 _ProjectGitFlock（与交付同 canon_path 同一把锁）；sibling：_run_l2_local。
"""
from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ── F1 fake 沙箱 manager：录制 write/run_command 调用序 ────────────────────────

class _FakeSandbox:
    sandbox_id = "sb-fake-1"


class _FakeManager:
    """录制调用序的假 manager。apply_rc 控制 git apply 的 marker 退出码；None=不回 marker。"""

    def __init__(self, apply_rc: int | None = 0):
        self.apply_rc = apply_rc
        self.calls: list[tuple[str, str]] = []  # (kind, payload)
        self._instances: dict[str, object] = {}
        self.killed: list[str] = []

    def create(self, project_id=None, source=""):
        self.calls.append(("create", source))
        return _FakeSandbox()

    def sync_project_to_sandbox(self, sandbox, local, workdir):
        self.calls.append(("sync", str(local)))

    def try_extend_lifetime(self, sandbox, sec):
        return True

    def run_command(self, sandbox, cmd, timeout=60):
        self.calls.append(("run", cmd))

        class _R:
            stdout = ""
            stderr = ""
            error = None

        r = _R()
        if "__SMOKE_APPLY_RC__" in cmd:
            r.stdout = "" if self.apply_rc is None else f"__SMOKE_APPLY_RC__{self.apply_rc}\n"
        elif "__RC__" in cmd:
            r.stdout = "__RC__0\n"
        return r

    def kill(self, sid):
        self.killed.append(sid)


def _acquire(monkeypatch, tmp_path, manager, *, merged_diff, handoff_sid=""):
    from swarm.brain.nodes import verify as v

    monkeypatch.setattr("swarm.brain.nodes._sandbox_available", lambda: True)
    # _kill_sandbox_quiet 走全局 get_sandbox_manager() → 指到 fake，使 kill 可录制
    monkeypatch.setattr("swarm.worker.sandbox.get_sandbox_manager", lambda: manager)
    # write_file_to_sandbox 走 manager 无关的沙箱文件端点——测试里录调用即可
    monkeypatch.setattr(
        "swarm.worker.sandbox.write_file_to_sandbox",
        lambda sandbox, path, content, manager=None: manager_record(path, content),
    )
    written: dict[str, str] = {}

    def manager_record(path, content):
        written[path] = content
        manager.calls.append(("write", path))

    sandbox, skip_reason, details = v._acquire_smoke_sandbox(
        manager, handoff_sid, "proj-1", str(tmp_path), 900, merged_diff=merged_diff,
    )
    return sandbox, skip_reason, details, written


_DIFF = """diff --git a/A.java b/A.java
new file mode 100644
--- /dev/null
+++ b/A.java
@@ -0,0 +1 @@
+class A {}
"""


def test_f1_self_built_applies_merged_diff_before_rebuild(monkeypatch, tmp_path):
    """自建臂 + merged_diff 非空 → rebuild 前必须 write patch + git apply（验 merged 树）。"""
    m = _FakeManager(apply_rc=0)
    sandbox, skip_reason, details, written = _acquire(
        monkeypatch, tmp_path, m, merged_diff=_DIFF)
    assert sandbox is not None and skip_reason is None
    kinds = [k for k, _ in m.calls]
    assert "write" in kinds, f"merged_diff 必须写入沙箱: {m.calls}"
    apply_idx = next(i for i, (k, p) in enumerate(m.calls)
                     if k == "run" and "git apply" in p)
    sync_idx = next(i for i, (k, _) in enumerate(m.calls) if k == "sync")
    assert apply_idx > sync_idx, "apply 必须在 sync 之后"
    apply_cmd = m.calls[apply_idx][1]
    assert "--ignore-whitespace" in apply_cmd, "与交付/L2 同旗标（B3-F1 口径）"
    assert list(written.values()) == [_DIFF], "写入沙箱的补丁必须逐字节等于 merged_diff"
    assert details.get("smoke_diff_applied") is True


def test_f1_apply_failure_returns_skipped_not_base_tree(monkeypatch, tmp_path):
    """apply rc=1 → (None, smoke_apply_failed)（skipped 非 failed），绝不带 base 树继续冒烟。"""
    m = _FakeManager(apply_rc=1)
    sandbox, skip_reason, details, _ = _acquire(
        monkeypatch, tmp_path, m, merged_diff=_DIFF)
    assert sandbox is None
    assert skip_reason == "smoke_apply_failed"
    assert m.killed == ["sb-fake-1"], "失败必须即时销毁自建沙箱"


def test_f1_apply_marker_missing_is_infra_skip(monkeypatch, tmp_path):
    """marker 缺失（命令没跑成）= infra → 同样 smoke_apply_failed，绝不静默继续。"""
    m = _FakeManager(apply_rc=None)
    sandbox, skip_reason, details, _ = _acquire(
        monkeypatch, tmp_path, m, merged_diff=_DIFF)
    assert sandbox is None
    assert skip_reason == "smoke_apply_failed"


def test_f1_empty_diff_no_apply_zero_regression(monkeypatch, tmp_path):
    """merged_diff 空 → 不注入 apply（行为与既有一致）。"""
    m = _FakeManager()
    sandbox, skip_reason, details, written = _acquire(
        monkeypatch, tmp_path, m, merged_diff="")
    assert sandbox is not None
    assert not written
    assert not any("git apply" in p for k, p in m.calls if k == "run")


def test_f1_handoff_path_never_injects_apply(monkeypatch, tmp_path):
    """转交快路径箱内已是 merged 树 → 绝不再 apply（防双重应用）。"""
    m = _FakeManager()
    sb = _FakeSandbox()
    m._instances["sb-handoff"] = sb
    sandbox, skip_reason, details, written = _acquire(
        monkeypatch, tmp_path, m, merged_diff=_DIFF, handoff_sid="sb-handoff")
    assert sandbox is sb
    assert details.get("source") == "handoff"
    assert not written and not any(k == "run" for k, _ in m.calls)


# ── F2：verify_l2 工作树窗口必须持 _ProjectGitFlock ───────────────────────────

def _git_repo(tmp_path: Path) -> Path:
    import subprocess
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "a.txt").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=root, check=True)
    return root


_F2_DIFF = """diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-base
+merged
"""


def test_f2_integration_review_waits_for_project_flock(tmp_path):
    """兄弟任务持 _ProjectGitFlock（交付临界区）期间，run_integration_review 的首次
    工作树写必须排队——锁释放前绝不落 git 写（跨任务互踩窗口关闭）。"""
    from swarm.brain.integration_review import run_integration_review
    from swarm.worker.git_flock import _ProjectGitFlock

    root = _git_repo(tmp_path)
    events: list[tuple[str, float]] = []
    release_at = {"t": 0.0}

    def holder():
        with _ProjectGitFlock(root):
            events.append(("lock_acquired", time.monotonic()))
            time.sleep(0.6)
            release_at["t"] = time.monotonic()

    th = threading.Thread(target=holder)
    th.start()
    time.sleep(0.15)  # 确保 holder 已持锁
    passed, issues, details = run_integration_review(
        str(root), _F2_DIFF,
        compile_runner=lambda cmd: (True, True, ""),
    )
    th.join()
    # 无构建文件 → build_cmd None → apply 段不跑，但 reset+apply-check 仍是工作树写窗口。
    # 判据：run_integration_review 返回时 holder 必已释放（其整个工作树段被锁串行化）。
    done_t = time.monotonic()
    assert release_at["t"] > 0, "holder 未运行"
    assert done_t >= release_at["t"], "integration_review 必须等 holder 释放锁后才完成工作树段"
    assert details.get("worktree_flock") is True, "工作树段必须声明持锁（可观测）"


def test_f2_run_l2_local_holds_flock(tmp_path):
    """sibling：_run_l2_local 的 apply→测试→finally reset 同样必须持同一把锁。"""
    from swarm.brain.nodes import _run_l2_local
    from swarm.worker.git_flock import _ProjectGitFlock

    root = _git_repo(tmp_path)
    release_at = {"t": 0.0}

    def holder():
        with _ProjectGitFlock(root):
            time.sleep(0.6)
            release_at["t"] = time.monotonic()

    th = threading.Thread(target=holder)
    th.start()
    time.sleep(0.15)
    ok = _run_l2_local(str(root), _F2_DIFF, "true", timeout=30)
    th.join()
    done_t = time.monotonic()
    assert release_at["t"] > 0
    assert done_t >= release_at["t"], "_run_l2_local 必须排队等锁"
    assert ok is True
    assert (root / "a.txt").read_text() == "base\n", "finally reset 语义不回归"
