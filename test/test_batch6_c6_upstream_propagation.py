"""C6（19_worker_flow_audit）求证+补闸：writable ∩ 上游完成态产物的传播链。

求证结论：
- 产物正确性【不】由"writable 单写者"承重——各子任务产物活在 subtask_results 的
  diff 空间，MERGE 节点 3-way/rebase（brain/nodes/__init__.py:4028+）+ L2 全局编译收口；
  reset 抹 worktree 不丢上游产物（已存于 diff）。
- 真缺口=【沙箱保真】：normalize 串行化的聚合/注册类共享文件链（st-A→st-B 同改 base
  既有文件 X）中，st-B 沙箱拿到的是 base 版 X（reset 抹本地 + clean_upload 钉 base），
  对上游同文件改动全盲 → 盲改 + L1 假红。旧 normalize 注释"bootstrap 传播收口"对
  tracked writable 是空洞腿。

治法（对称 readable 补传 69d34b1b/B1 语义，栈中立）：
1. dispatch._inject_upstream_products：own writable 不再排除出 upstream_artifacts
   （零 prompt 渲染账；create_files 仍排除）→ provenance 供给；
2. executor_sync._reset_scope_to_head：writable ∩ upstream_artifacts 不参与防脏
   reset（保留本地已合并版）；
3. executor_sync._sync_to_sandbox clean_upload：同上集合上传本地已合并版而非 base 版。
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
    repo = tmp_path / "repo_c6"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "index.ts").write_text("// base routes\n")
    (repo / "solo.py").write_text("# base solo\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


# ── 1. dispatch 注入：own writable 的上游完成态改动必须进 upstream_artifacts（provenance 供给）──
def test_dispatch_injects_upstream_product_on_own_writable():
    from swarm.brain.nodes.dispatch import _inject_upstream_products
    from swarm.types import Confidence, SubTask, SubTaskDifficulty, WorkerOutput

    diff = (
        "diff --git a/index.ts b/index.ts\n"
        "--- a/index.ts\n+++ b/index.ts\n"
        "@@ -1 +1,2 @@\n// base routes\n+// st-a route\n"
        "diff --git a/new_vo.ts b/new_vo.ts\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/new_vo.ts\n@@ -0,0 +1 @@\n+export {}\n"
    )
    down = SubTask(
        id="st-b", description="d", difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=["index.ts"], create_files=["new_vo.ts"]),
    )
    results = {"st-a": WorkerOutput(subtask_id="st-a", diff=diff, summary="",
                                    confidence=Confidence.HIGH, l1_passed=True)}
    _inject_upstream_products([down], results)
    # H-1（批次6 R1 hunter）：防脏豁免消费的是【即时账 upstream_products】（每轮重算替换）
    products = list(down.scope.upstream_products or [])
    assert "index.ts" in products, (
        "上游完成态对 own writable 的改动必须进即时账——否则 reset/clean_upload "
        "钉 base 双写点把上游已合并版剥离，沙箱对上游同文件改动全盲（C6）")
    assert "new_vo.ts" not in products, "own create_files 仍排除（本任务建=无上游产物）"
    # upstream_artifacts（seed 闸/readable 补传消费）同步注入（C6 不再排除 own writable）
    assert "index.ts" in list(down.scope.upstream_artifacts or [])


def test_dispatch_upstream_products_recomputed_not_sticky():
    """H-1（批次6 R1 hunter）：即时账必须随完成态全集【重算替换】——旧轮残留会把
    本地纯脏改文件永久赦免出防脏 reset（脏叠加无法自愈）。"""
    from swarm.brain.nodes.dispatch import _inject_upstream_products
    from swarm.types import Confidence, SubTask, SubTaskDifficulty, WorkerOutput

    diff = ("diff --git a/index.ts b/index.ts\n--- a/index.ts\n+++ b/index.ts\n"
            "@@ -1 +1,2 @@\n// base\n+// st-a\n")
    down = SubTask(
        id="st-b", description="d", difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=["index.ts"]),
    )
    out = WorkerOutput(subtask_id="st-a", diff=diff, summary="",
                       confidence=Confidence.HIGH, l1_passed=True)
    _inject_upstream_products([down], {"st-a": out})
    assert down.scope.upstream_products == ["index.ts"]
    # 第二轮：上游账空了（replan/重试）→ 即时账必须重算为空，绝不留旧轮残留
    _inject_upstream_products([down], {})
    assert down.scope.upstream_products == [], "即时账必须重算替换，粘滞=防脏护栏永久失效"


# ── 2. reset：writable ∩ upstream_artifacts 保留本地已合并版；无 provenance 的仍 reset ──
def test_reset_preserves_upstream_provenance_writable(tmp_path):
    from swarm.worker.executor import WorkerExecutor

    repo = _make_repo(tmp_path)
    # 模拟上游 st-a 已合并进 worktree 的改动（本地≠base）+ 本子任务自己的脏文件
    (repo / "index.ts").write_text("// base routes\n// st-a route\n")
    (repo / "solo.py").write_text("# base solo\n# DIRTY\n")
    stub = SimpleNamespace()
    stub.project_path = str(repo)
    stub.effective_scope = FileScope(
        writable=["index.ts", "solo.py"], readable=[], create_files=[],
        upstream_products=["index.ts"],
    )
    stub._log = lambda m: None
    stub._writable_files = WorkerExecutor._writable_files.__get__(stub)
    stub._norm_rel = WorkerExecutor._norm_rel
    stub._reset_scope_to_head = WorkerExecutor._reset_scope_to_head.__get__(stub)

    n = stub._reset_scope_to_head()
    assert n == 1, f"只有无 provenance 的 solo.py 应被 reset，实得 {n}"
    assert (repo / "index.ts").read_text() == "// base routes\n// st-a route\n", (
        "带上游产物 provenance 的 writable 必须保留本地已合并版（抹回 base=沙箱对上游全盲）")
    assert (repo / "solo.py").read_text() == "# base solo\n", (
        "无 provenance 的 writable 维持防脏 reset（护栏不回退）")


# ── 3. clean_upload：provenance writable 传本地已合并版；非 provenance 传 base 版 ──
def test_clean_upload_stages_local_for_upstream_provenance(tmp_path, monkeypatch):
    from swarm.worker.executor import WorkerExecutor

    repo = _make_repo(tmp_path)
    (repo / "index.ts").write_text("// base routes\n// st-a route\n")
    (repo / "solo.py").write_text("# base solo\n# DIRTY\n")

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
    stub.effective_scope = FileScope(
        writable=["index.ts", "solo.py"], readable=[], create_files=[],
        upstream_products=["index.ts"],
    )
    stub._sandbox = object()
    stub._sandbox_manager = _Mgr()
    stub._log = lambda m: None
    stub._writable_files = WorkerExecutor._writable_files.__get__(stub)
    stub._scope_files = lambda: ["index.ts", "solo.py"]
    stub._norm_rel = WorkerExecutor._norm_rel
    stub._git_baseline_text = WorkerExecutor._git_baseline_text.__get__(stub)
    stub._snapshot_scope_local = WorkerExecutor._snapshot_scope_local.__get__(stub)
    stub._sync_to_sandbox = WorkerExecutor._sync_to_sandbox.__get__(stub)

    # 批次6 R1（reviewer M-1）：_sync_to_sandbox 定义在 executor_sync，get_config 引用
    # 属该模块命名空间——打错模块则 mock 空转（靠真实 .env 侥幸过）。
    import swarm.worker.executor_sync as sync_mod
    monkeypatch.setattr(sync_mod, "get_config", lambda: SimpleNamespace(
        sandbox=SimpleNamespace(sandbox_remote_workdir="/workspace")
    ))

    asyncio.run(stub._sync_to_sandbox("bootstrap"))

    assert captured["contents"]["index.ts"] == "// base routes\n// st-a route\n", (
        f"provenance writable 应上传本地已合并版，实际={captured['contents']['index.ts']!r}")
    assert captured["contents"]["solo.py"] == "# base solo\n", (
        f"非 provenance writable 应上传 base 干净版（防脏叠加不回退），"
        f"实际={captured['contents']['solo.py']!r}")
