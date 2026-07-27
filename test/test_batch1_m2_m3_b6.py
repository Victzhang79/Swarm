"""批次1 治本回归（deep_read_findings/21_full_sweep_20260727.md）：M-2 / M-3 / B-6。

M-2：两处 sync_project_to_sandbox（verify_runtime 自建冒烟臂 verify.py、L2 沙箱功能测试
_run_l2_in_sandbox）读共享本地工作树却不持 _ProjectGitFlock——兄弟任务交付临界区/L2
worktree phase 的锁内 reset/apply 可在 sync 半途撕树 → 箱内半态树 → 假红 replan/冒烟
假 skip。修法=只护 sync 一瞬（箱内 apply/编译不持锁）。reactor 编译站（__init__.py 第
三处 sync）在调用方 run_integration_review 已持锁的 worktree phase 内跑，不在此列。

M-3：verify_l2 的放弃者清理只 git clean（untracked）——放弃者对【tracked】文件的脏改
永留共享树，污染本任务与后续同项目任务的 L2/冒烟。修法=同清单同锁，先按钉扎 base
（resolve_base_ref/base_ref_exists，与 _reset_worktree_to_head 基线同源）分出 tracked
子集 checkout 回 base。

B-6：handle_failure 薄包装的 plan 回传条件原挂在 "dispatch_remaining" 键上——escalate
系/规模闸出口不含该键 → 就地注入 plan 的 retry_guidance 变异随 checkpoint 丢失。修法=
放宽为"result 不含 plan 且 state 有 plan 即回传"（重写同值语义无害）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import swarm.brain.nodes as nodes  # noqa: E402
from swarm.brain.nodes import verify as v  # noqa: E402
from swarm.types import (  # noqa: E402
    FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan,
)


def _git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "tracked.txt").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=root, check=True)
    return root


# ── M-2：两站 sync 必须持 _ProjectGitFlock（只护 sync 一瞬）────────────────────

class _FakeSandbox:
    sandbox_id = "sb-m2-1"


class _FakeManager:
    """录制 sync 时刻的假 manager；run_command 全 marker 成功。"""

    def __init__(self):
        self.sync_at: float = 0.0
        self.killed: list[str] = []

    def create(self, project_id=None, source="", template_id=None):
        return _FakeSandbox()

    def sync_project_to_sandbox(self, sandbox, local, workdir):
        self.sync_at = time.monotonic()

    def try_extend_lifetime(self, sandbox, sec):
        return True

    def run_command(self, sandbox, cmd, timeout=60):
        class _R:
            stdout = ""
            stderr = ""
            error = None

        r = _R()
        if "__SMOKE_APPLY_RC__" in cmd or "__APPLY_RC__" in cmd:
            r.stdout = "__APPLY_RC__0\n"
        elif "__RC__" in cmd:
            r.stdout = "__RC__0\n"
        return r

    def kill(self, sid):
        self.killed.append(sid)


def test_m2_smoke_self_built_sync_waits_for_flock(monkeypatch, tmp_path):
    """自建冒烟臂：兄弟任务持锁期间，sync 必须排队（锁释放前绝不读共享树）。"""
    from swarm.worker.git_flock import _ProjectGitFlock

    root = _git_repo(tmp_path)
    m = _FakeManager()
    monkeypatch.setattr("swarm.brain.nodes._sandbox_available", lambda: True)
    monkeypatch.setattr("swarm.worker.sandbox.get_sandbox_manager", lambda: m)

    release_at = {"t": 0.0}
    held = threading.Event()  # reviewer LOW：替代盲 sleep——holder 真持锁后才放行主线程

    def holder():
        with _ProjectGitFlock(root):
            held.set()
            time.sleep(0.6)
            release_at["t"] = time.monotonic()

    th = threading.Thread(target=holder)
    th.start()
    assert held.wait(5), "holder 未及时持锁"
    sandbox, skip_reason, details = v._acquire_smoke_sandbox(
        m, "", "proj-1", str(root), 900, merged_diff="")
    th.join()
    assert sandbox is not None and skip_reason is None, details
    assert release_at["t"] > 0, "holder 未运行"
    assert m.sync_at >= release_at["t"], "sync 必须等 holder 释放锁后才读共享树"


def test_m2_l2_sandbox_sync_waits_for_flock(monkeypatch, tmp_path):
    """L2 沙箱功能测试（_run_l2_in_sandbox）：sync 同样必须持同一把锁。"""
    from swarm.worker.git_flock import _ProjectGitFlock

    root = _git_repo(tmp_path)
    m = _FakeManager()
    monkeypatch.setattr("swarm.brain.nodes._sandbox_available", lambda: True)
    monkeypatch.setattr("swarm.worker.sandbox.get_sandbox_manager", lambda: m)
    monkeypatch.setattr(
        "swarm.worker.sandbox.write_file_to_sandbox",
        lambda sandbox, path, content, manager=None: None)

    release_at = {"t": 0.0}
    held = threading.Event()

    def holder():
        with _ProjectGitFlock(root):
            held.set()
            time.sleep(0.6)
            release_at["t"] = time.monotonic()

    th = threading.Thread(target=holder)
    th.start()
    assert held.wait(5), "holder 未及时持锁"
    ok = nodes._run_l2_in_sandbox(str(root), "diff --git a/x b/x\n", "true", timeout=30)
    th.join()
    assert ok is True
    assert release_at["t"] > 0
    assert m.sync_at >= release_at["t"], "sync 必须等 holder 释放锁后才读共享树"


# ── M-3：verify_l2 清理放弃者 tracked 脏改（与 untracked 同清单同锁）────────────

_M3_DIFF = """diff --git a/deliver.txt b/deliver.txt
new file mode 100644
--- /dev/null
+++ b/deliver.txt
@@ -0,0 +1 @@
+delivered
"""


def _st(sid: str, files: list[str]) -> SubTask:
    return SubTask(
        id=sid, description="d", difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT, scope=FileScope(writable=files),
    )


def test_m3_abandoner_tracked_dirt_rolled_back(monkeypatch, tmp_path):
    """放弃者 tracked 脏改回滚至 base + untracked 半成品删除 + 存活者文件绝不动。"""
    root = _git_repo(tmp_path)
    # 放弃者现场：tracked 脏改 + untracked 半成品
    (root / "tracked.txt").write_text("dirtied by abandoner\n")
    (root / "half.py").write_text("# half product\n")
    # 存活者现场（绝不能被清理波及）
    (root / "keep.txt").write_text("survivor output\n")

    plan = TaskPlan(
        subtasks=[_st("st-alive", ["keep.txt"]),
                  _st("st-dead", ["tracked.txt", "half.py"])],
        parallel_groups=[["st-alive"], ["st-dead"]],
    )
    state = {
        "project_id": "proj-m3",
        "merged_diff": _M3_DIFF,
        "task_description": "d",
        "plan": plan,
        "subtask_results": {},
        "abandoned_subtask_ids": ["st-dead"],
    }
    monkeypatch.setattr(nodes, "_get_project_path", lambda pid: str(root))
    monkeypatch.setattr(
        "swarm.brain.integration_review.run_integration_review",
        lambda *a, **k: (True, [], {}))

    out = asyncio.run(v.verify_l2(state))
    assert out.get("l2_passed") is True, out
    assert (root / "tracked.txt").read_text() == "base\n", \
        "M-3：放弃者 tracked 脏改必须回滚至 base"
    assert not (root / "half.py").exists(), "放弃者 untracked 半成品必须清除"
    assert (root / "keep.txt").read_text() == "survivor output\n", \
        "存活者文件绝不被清理波及"


def test_m3_non_ascii_tracked_path_rolled_back(monkeypatch, tmp_path):
    """reviewer MEDIUM 红灯：非 ASCII 路径（core.quotePath 引号串）下 tracked 回滚
    不得整批失效——ls-tree -z 关 quoting 后中文名 pathspec 精确匹配。"""
    root = _git_repo(tmp_path)
    # base 里已有中文名 tracked 文件，放弃者把它改脏
    (root / "文档.txt").write_text("base-doc\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "add doc"], cwd=root, check=True)
    (root / "文档.txt").write_text("dirtied\n")

    plan = TaskPlan(
        subtasks=[_st("st-alive", ["keep.txt"]),
                  _st("st-dead", ["文档.txt"])],
        parallel_groups=[["st-alive"], ["st-dead"]],
    )
    state = {
        "project_id": "proj-m3b",
        "merged_diff": _M3_DIFF,
        "task_description": "d",
        "plan": plan,
        "subtask_results": {},
        "abandoned_subtask_ids": ["st-dead"],
    }
    monkeypatch.setattr(nodes, "_get_project_path", lambda pid: str(root))
    monkeypatch.setattr(
        "swarm.brain.integration_review.run_integration_review",
        lambda *a, **k: (True, [], {}))

    out = asyncio.run(v.verify_l2(state))
    assert out.get("l2_passed") is True, out
    assert (root / "文档.txt").read_text() == "base-doc\n", \
        "非 ASCII tracked 路径必须同样回滚（quotePath 不得致整批失效）"


# ── B-6：escalate 出口（无 dispatch_remaining 键）必须回传 plan ────────────────

def test_b6_escalate_exit_recarries_plan():
    """重试耗尽+无完成子任务 → 最终 escalate 臂：result 无 dispatch_remaining 键，
    薄包装仍须回传 plan（就地注入的 retry_guidance 变异不再随 checkpoint 蒸发）。"""
    plan = TaskPlan(
        subtasks=[_st("st-a", ["a.py"]), _st("st-b", ["b.py"])],
        parallel_groups=[["st-a"], ["st-b"]],
    )
    state = {
        "plan": plan,
        "failed_subtask_ids": ["st-a", "st-b"],
        "subtask_results": {},  # 0 完成 → 部分交付不成立 → 最终 escalate
        "subtask_retry_counts": {"st-a": 5, "st-b": 5},  # deepest > max_retries+1
        "subtask_alternate_ever_used": {"st-a": True, "st-b": True},
        "dispatch_remaining": [],
        "degraded_reasons": [],
    }
    with patch.object(nodes, "_get_brain_llm", side_effect=RuntimeError("llm down")):
        out = asyncio.run(nodes.handle_failure(state))
    assert out.get("failure_strategy") == "escalate", out.get("failure_strategy")
    assert "dispatch_remaining" not in out, "本测试锚定的就是无该键的 escalate 出口"
    assert out.get("plan") is plan, \
        "B-6：escalate 出口必须回传 plan（就地变异靠 result 重携落账）"


if __name__ == "__main__":
    import sys
    import tempfile
    from unittest.mock import patch as _p

    class _MP:
        def __init__(self):
            self._patches = []

        def setattr(self, target, value):
            p = _p(target, value)
            p.start()
            self._patches.append(p)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mp = _MP()
        test_m2_smoke_self_built_sync_waits_for_flock(mp, tmp)
        print("  ✅ M-2 自建冒烟臂 sync 持锁排队")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mp = _MP()
        test_m2_l2_sandbox_sync_waits_for_flock(mp, tmp)
        print("  ✅ M-2 L2 沙箱功能测试 sync 持锁排队")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mp = _MP()
        test_m3_abandoner_tracked_dirt_rolled_back(mp, tmp)
        print("  ✅ M-3 放弃者 tracked 脏改回滚 base")
    test_b6_escalate_exit_recarries_plan()
    print("  ✅ B-6 escalate 出口回传 plan")
    print("\n=== 批次1 M-2/M-3/B-6: 4/4 passed ===")
    sys.exit(0)
