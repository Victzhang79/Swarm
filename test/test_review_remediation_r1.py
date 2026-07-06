"""外部复核整改批 R1：三份审查报告坐实缺口的修复锁定。

① P0-2 残口：/api/status 知识库分量本地 fallback 只看目录存在——远端 Qdrant 配置全挂 +
   宿主机陈旧 ~/.swarm/qdrant 仍报健康，与 /api/health/ready 的 _probe_qdrant_ready 漂移。
   修：对齐 fail-closed 判定（_is_local_qdrant(url) + meta.json 才有资格作健康证据）。
② 批4c 漏清：merge 干净轮 / plan replan 重入（_surgical_replan_reset）不清 failure_escalated
   ——confirm/deliver REVISE→PLAN 路径不经 revision()/handle_failure，粘滞仍可残留。
③ P2-2 gitleaks 报告解析失败此前已 _mark_ran → "扫过+零发现" fail-open。修：解析成功才置位。
④ P2-3 git_flock.__exit__ unlock 抛异常跳过 close（句柄泄漏）。修：finally close。
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

# ── ① /api/status 远端挂 + 陈旧本地目录不得假绿 ─────────────────────────────


def test_status_remote_qdrant_down_stale_local_dir_not_green(monkeypatch):
    import importlib

    app_mod = importlib.import_module("swarm.api.app")

    import httpx

    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            raise ConnectionError("remote qdrant down")

    monkeypatch.setattr(httpx, "AsyncClient", _BoomClient)
    # 远端配置 + 宿主机存在陈旧本地存储（目录与 meta.json 都在）
    monkeypatch.setattr(
        app_mod, "get_config",
        lambda: SimpleNamespace(db=SimpleNamespace(qdrant_url="http://qdrant.internal:6333"),
                                knowledge=SimpleNamespace(embedding_model="m")),
    )
    real_exists = os.path.exists
    monkeypatch.setattr(os.path, "exists",
                        lambda p: True if "qdrant" in str(p) else real_exists(p))
    fake = types.ModuleType("fastembed")
    fake.TextEmbedding = object
    monkeypatch.setitem(sys.modules, "fastembed", fake)

    status = asyncio.run(app_mod._check_component("知识库"))
    assert "qdrant unreachable" in status["detail"], status
    assert status["status"] == "degraded", (
        f"远端 Qdrant 挂 + 陈旧本地目录必须不健康（与 /ready 对齐），实际 {status['status']}"
    )


# ── ② merge 干净轮 / replan 重入清 escalated ────────────────────────────────

_DIFF_A = """--- a/a.py
+++ b/a.py
@@ -1,1 +1,2 @@
 x = 1
+a = 1
"""
_DIFF_B = """--- a/b.py
+++ b/b.py
@@ -1,1 +1,2 @@
 y = 1
+b = 2
"""


def test_merge_clean_round_clears_stale_escalated():
    import swarm.brain.nodes as nodes
    from swarm.types import WorkerOutput

    state = {
        "failure_escalated": True,  # 上一轮 escalate 残留
        "failure_strategy": "escalate",
        "merge_conflicts": [],
        "failed_subtask_ids": [],
        "rebase_subtask_ids": [],
        "subtask_results": {
            "st-a": WorkerOutput(subtask_id="st-a", diff=_DIFF_A, summary="", l1_passed=True),
            "st-b": WorkerOutput(subtask_id="st-b", diff=_DIFF_B, summary="", l1_passed=True),
        },
    }
    out = nodes.merge(state)
    assert out.get("failure_escalated") is False, \
        "干净 merge 轮必须显式清 failure_escalated（merge_conflicts round27 修法的对称项）"


def test_surgical_replan_reset_clears_escalated():
    import swarm.brain.nodes as nodes
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan, WorkerOutput

    st = SubTask(id="st-1", description="d", difficulty=SubTaskDifficulty.MEDIUM,
                 modality=SubTaskModality.TEXT, scope=FileScope(writable=["a.py"]))
    plan = TaskPlan(subtasks=[st], parallel_groups=[["st-1"]])
    old = {"st-1": WorkerOutput(subtask_id="st-1", diff="", summary="", l1_passed=True)}
    reset = nodes._surgical_replan_reset(old, plan, plan)
    assert reset.get("failure_escalated") is False, \
        "replan 重入是 REVISE→PLAN 等路径的汇合点，必须清历史 escalate 粘滞"


# ── ③ gitleaks 解析失败按未扫处理（不 _mark_ran）────────────────────────────


def test_gitleaks_parse_failure_not_marked_ran(tmp_path, monkeypatch):
    import swarm.worker.security_scan as scan

    ctx = scan._ScanContext()
    monkeypatch.setattr(scan, "shutil",
                        SimpleNamespace(which=lambda n: "/usr/bin/" + n))
    # rc=0 但报告文件不存在 → 读取 OSError → 解析失败
    monkeypatch.setattr(scan, "_run_tool", lambda *a, **k: (0, "", ""))
    out = scan._secret_gitleaks(str(tmp_path), ctx=ctx)
    assert out == []
    assert not getattr(ctx, "scanner_ran", False), \
        "报告解析失败必须按未扫处理（fail-closed），不得置 scanner_ran 伪装零发现"


# ── ④ git_flock unlock 异常不得跳过 close ──────────────────────────────────


def test_git_flock_close_even_if_unlock_raises():
    from swarm.worker.git_flock import _ProjectGitFlock

    class _F:
        closed = False

        def close(self):
            self.closed = True

    class _Fcntl:
        LOCK_UN = 8

        @staticmethod
        def flock(_f, _op):
            raise OSError("unlock boom")

    flk = object.__new__(_ProjectGitFlock)
    f = _F()
    flk._lock_f = f
    flk._fcntl = _Fcntl()
    assert flk.__exit__(None, None, None) is False
    assert f.closed, "unlock 抛异常时 close 必须仍执行（否则句柄泄漏）"
