"""★32 号文批2b 锁★ MED 坐实 11 条治法的区分力测试。

覆盖（治法详见 HANDOFF §11.24 / 各落点注释）：
- R1  runtime_smoke.build_project_symbols：残缺索引机读可辨（degraded 键+WARNING），
      classify dependency_missing 臂消费该键（假过方向可观测化，判向不变）。
- P1  dispatch._redispatch_hard_windows：非法/非正 WARNING+默认 24（不再静默关闸）。
- E1  executor_sync FINDING-11 补传通道：git 枚举异常/rc≠0 → WARNING（不再静默置空）。
- E2  executor_sync._rollback_failed_manifest_footprint：prune 臂/read 臂异常 → WARNING。
- D1下 learn_success 交付异常臂进 _degraded（L6 不再把没交付学成成功）。
- D2下 learn_success proj_path None 静默跳过 → WARNING+机读降级。
- D4下 merge D1 fold 异常臂进 degraded_reasons（fail-open 方向不变，机读可辨）。
- D2上 _surgical_replan_reset 第 15 张表 subtask_rebase_counts 同签名剪枝+两调用点接线。
- D3上 SWARM_PLAN_BATCH_MAX_ATTEMPTS 解析守卫（非法/非正→error+默认 2）。
- S2  shared._merge_horizontal_subtasks：harness.verify_commands/contract 并集不丢。
- S3  shared._is_test_file_path：Java 后缀大小写边界（Latest.java/Contest.java 不误杀）。
"""

from __future__ import annotations

import ast
import importlib
import logging
import os
import subprocess

import pytest

from swarm.brain.nodes import runtime_smoke as rs

# nodes/__init__ 的同名节点函数遮蔽了子模块属性，必须走 importlib 取真模块
disp = importlib.import_module("swarm.brain.nodes.dispatch")


# ═══════════════════════ R1：符号索引残缺机读可辨 ═══════════════════════

class TestR1BuildProjectSymbols:
    def test_clean_tree_not_degraded(self, tmp_path):
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
        (tmp_path / "top.py").write_text("y = 2\n")
        out = rs.build_project_symbols(str(tmp_path))
        assert out["degraded"] is False
        assert out["truncated"] is False
        assert out["walk_errors"] == []
        assert out["build_error"] == ""
        assert "pkg/mod.py" in out["paths"] and "mod.py" in out["basenames"]

    def test_walk_error_flagged_and_warns(self, tmp_path, monkeypatch, caplog):
        (tmp_path / "ok.py").write_text("x = 1\n")
        real_walk = os.walk

        def _fake_walk(root, *, onerror=None, **kw):
            if onerror is not None:
                onerror(PermissionError(13, "Permission denied", str(tmp_path / "secret")))
            yield from real_walk(root, **kw)

        monkeypatch.setattr(os, "walk", _fake_walk)
        with caplog.at_level(logging.WARNING, logger="swarm.brain.nodes.runtime_smoke"):
            out = rs.build_project_symbols(str(tmp_path))
        assert out["degraded"] is True
        assert len(out["walk_errors"]) == 1
        assert "Permission denied" in out["walk_errors"][0]
        assert any("符号索引【残缺】" in r.getMessage() for r in caplog.records), \
            "残缺索引必须至少一次 WARNING（血规 10④）"

    def test_truncation_flagged(self, tmp_path, monkeypatch):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "x.py").write_text("x = 1\n")
        monkeypatch.setattr(rs, "_PROJECT_SYMBOLS_MAX_DIRS", 0)
        out = rs.build_project_symbols(str(tmp_path))
        assert out["truncated"] is True and out["degraded"] is True

    def test_exception_flagged_not_silent(self, tmp_path, monkeypatch, caplog):
        def _boom(*_a, **_kw):
            raise RuntimeError("disk gone")
            yield  # pragma: no cover — 使其成为生成器

        monkeypatch.setattr(os, "walk", _boom)
        with caplog.at_level(logging.WARNING, logger="swarm.brain.nodes.runtime_smoke"):
            out = rs.build_project_symbols(str(tmp_path))
        assert out["degraded"] is True
        assert "RuntimeError" in out["build_error"]
        assert any("符号索引【残缺】" in r.getMessage() for r in caplog.records)


class TestR1ClassifierConsumesDegraded:
    _LOG = "Traceback ...\nModuleNotFoundError: No module named 'acme_utils'\n"

    @staticmethod
    def _symbols(**over):
        base = {"paths": set(), "basenames": set(), "top": set(),
                "degraded": False, "truncated": False, "walk_errors": [], "build_error": ""}
        base.update(over)
        return base

    def test_degraded_index_machine_readable_in_details(self, caplog):
        with caplog.at_level(logging.WARNING, logger="swarm.brain.nodes.runtime_smoke"):
            res = rs.classify_smoke_outcome(
                "exited", self._LOG, [], language_key="python",
                project_symbols=self._symbols(degraded=True, walk_errors=["/x: denied"]))
        assert res.classification == "dependency_missing" and res.status == "skipped", \
            "判向不变（保守不冤枉代码）"
        psd = res.details.get("project_symbols_degraded")
        assert psd is not None, "残缺索引判定必须机读落 details"
        assert psd["walk_errors"] == ["/x: denied"]
        assert any("证据不全" in r.getMessage() for r in caplog.records)

    def test_clean_index_no_degraded_key(self):
        res = rs.classify_smoke_outcome(
            "exited", self._LOG, [], language_key="python",
            project_symbols=self._symbols())
        assert res.classification == "dependency_missing"
        assert "project_symbols_degraded" not in res.details


# ═══════════════════════ P1：硬窗阈值非法/非正可观测 ═══════════════════════

class TestP1HardWindows:
    def test_unset_defaults_24(self, monkeypatch):
        monkeypatch.delenv("SWARM_REDISPATCH_HARD_WINDOWS", raising=False)
        assert disp._redispatch_hard_windows() == 24

    def test_valid_passthrough(self, monkeypatch, caplog):
        monkeypatch.setenv("SWARM_REDISPATCH_HARD_WINDOWS", "7")
        with caplog.at_level(logging.WARNING, logger="swarm.brain.nodes.dispatch"):
            assert disp._redispatch_hard_windows() == 7
        assert not caplog.records

    @pytest.mark.parametrize("bad", ["0", "-5", "abc", ""])
    def test_bad_values_warn_and_default(self, monkeypatch, caplog, bad):
        monkeypatch.setenv("SWARM_REDISPATCH_HARD_WINDOWS", bad)
        with caplog.at_level(logging.WARNING, logger="swarm.brain.nodes.dispatch"):
            v = disp._redispatch_hard_windows()
        assert v == 24
        if bad:  # 空串=未设置，静默默认合法；其余必须 WARNING
            assert any("SWARM_REDISPATCH_HARD_WINDOWS" in r.getMessage()
                       for r in caplog.records), \
                f"非法/非正值 {bad!r} 必须 WARNING（旧码静默折 0 关闸）"


# ═══════════════════════ E1：FINDING-11 补传通道可观测 ═══════════════════════

def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_sync_exec(tmp_path):
    """自带源码沙箱同步的最小执行体（形状照抄 test_ctodebt_bootstrap_propagation）。"""
    from unittest.mock import MagicMock

    from swarm.types import FileScope, SubTask
    from swarm.worker.executor import WorkerExecutor

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "base.java").write_text("class Base{}\n")
    _git(proj, "init", "-q")
    _git(proj, "add", "-A")
    _git(proj, "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "init")

    st = SubTask(id="st-2", description="d", scope=FileScope(
        writable=[], readable=["base.java"]))
    ex = WorkerExecutor(st, project_path=str(proj))
    ex._sandbox_has_source = True
    ex._sandbox = object()
    mgr = MagicMock()
    mgr.sync_files_to_sandbox.return_value = {"uploaded": 0, "errors": []}
    ex._sandbox_manager = mgr
    return ex


class TestE1Finding11Channel:
    def test_git_rc_nonzero_warns(self, tmp_path, caplog):
        import asyncio
        ex = _make_sync_exec(tmp_path)
        ex.base_ref = "refs/heads/definitely-not-exist-r32b2b"  # git diff rc≠0
        with caplog.at_level(logging.WARNING, logger="swarm.worker.executor_sync"):
            asyncio.run(ex._sync_to_sandbox("bootstrap"))
        assert any("FINDING-11 补传通道 git 枚举非零退出" in r.getMessage()
                   for r in caplog.records), \
            "git 枚举 rc≠0 必须 WARNING（旧码静默置空 ⇒ 父 pom 不补传零留痕）"

    def test_git_exception_warns(self, tmp_path, monkeypatch, caplog):
        import asyncio
        ex = _make_sync_exec(tmp_path)
        real_run = subprocess.run

        def _flaky_run(cmd, *a, **kw):
            if "ls-files" in cmd:
                raise subprocess.TimeoutExpired(cmd, 15)
            return real_run(cmd, *a, **kw)

        monkeypatch.setattr(subprocess, "run", _flaky_run)
        with caplog.at_level(logging.WARNING, logger="swarm.worker.executor_sync"):
            asyncio.run(ex._sync_to_sandbox("bootstrap"))
        assert any("FINDING-11 补传通道 git 枚举异常" in r.getMessage()
                   for r in caplog.records)


# ═══════════════════════ E2：回滚 prune/read 臂可观测 ═══════════════════════

class TestE2RollbackArms:
    def _host(self, tmp_path, own, snap, creates):
        class _Host:
            project_path = str(tmp_path)
            base_ref = None
            _post_sync_contents = own
            _manifest_baseline_snapshot = snap

            class subtask:
                class scope:
                    create_files = creates
                    writable = []

            def _log(self, msg):
                pass
        return _Host()

    def _git_repo_with_root_pom(self, tmp_path):
        root_pom = ("<project><modules><module>mod-a</module></modules>"
                    "</project>")
        (tmp_path / "pom.xml").write_text(root_pom, "utf-8")
        _git(tmp_path, "init", "-q")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "-c", "user.email=a@b.c", "-c", "user.name=t",
             "commit", "-q", "-m", "base")
        return root_pom

    def test_prune_exception_warns_not_silent(self, tmp_path, monkeypatch, caplog):
        self._git_repo_with_root_pom(tmp_path)
        (tmp_path / "mod-a").mkdir()
        mod_pom = "<project><artifactId>a</artifactId></project>"
        (tmp_path / "mod-a" / "pom.xml").write_text(mod_pom, "utf-8")
        host = self._host(tmp_path, {"mod-a/pom.xml": mod_pom},
                          {"mod-a/pom.xml": ""}, ["mod-a/pom.xml"])

        import swarm.worker.workspace_manifest as wm

        def _boom(*_a, **_kw):
            raise RuntimeError("prune exploded")

        monkeypatch.setattr(wm, "prune_manifest_members", _boom)
        from swarm.worker.executor_sync import _SandboxSyncMixin
        with caplog.at_level(logging.WARNING, logger="swarm.worker.executor_sync"):
            _SandboxSyncMixin._rollback_failed_manifest_footprint(host, {})
        assert any("幽灵 <module> 摘除异常" in r.getMessage() for r in caplog.records), \
            "prune 臂异常必须 WARNING（旧裸 pass ⇒ 幽灵 module 残留全员 reactor 炸零留痕）"
        assert not (tmp_path / "mod-a" / "pom.xml").is_file(), \
            "删除模块 pom 的主路径语义不变"

    @pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                        reason="root 下 chmod 000 不挡读")
    def test_read_failure_warns_not_silent(self, tmp_path, caplog):
        root_pom = ("<project><dependencies><dependency><groupId>g</groupId>"
                    "<artifactId>base</artifactId></dependency></dependencies></project>")
        (tmp_path / "pom.xml").write_text(root_pom, "utf-8")
        _git(tmp_path, "init", "-q")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "-c", "user.email=a@b.c", "-c", "user.name=t",
             "commit", "-q", "-m", "base")
        poisoned = root_pom.replace(
            "</dependencies>", "<dependency><groupId>x</groupId>"
            "<artifactId>injected</artifactId></dependency></dependencies>")
        (tmp_path / "pom.xml").write_text(poisoned, "utf-8")
        os.chmod(tmp_path / "pom.xml", 0o000)
        try:
            host = self._host(tmp_path, {"pom.xml": poisoned},
                              {"pom.xml": root_pom}, [])
            from swarm.worker.executor_sync import _SandboxSyncMixin
            with caplog.at_level(logging.WARNING, logger="swarm.worker.executor_sync"):
                _SandboxSyncMixin._rollback_failed_manifest_footprint(host, {})
        finally:
            os.chmod(tmp_path / "pom.xml", 0o644)
        assert any("读本地清单失败" in r.getMessage() for r in caplog.records), \
            "read 臂失败必须 WARNING（旧静默 continue ⇒ 毒贡献残留零留痕）"


# ═══════════════════════ D1下/D2下：learn_success 降级臂 ═══════════════════════

def _ls_state(tmp_path, **over):
    st = {
        "task_id": "t-r32b2b",
        "project_id": "p-r32b2b",
        "task_description": "做一个功能",
        "plan": None,
        "merged_diff": (
            "diff --git a/x.py b/x.py\nnew file mode 100644\n"
            "index 0000000..e69de29\n--- /dev/null\n+++ b/x.py\n"
            "@@ -0,0 +1 @@\n+print(1)\n"),
        "base_commit": None,
        "degraded_reasons": [],
    }
    st.update(over)
    return st


def _patch_ls_common(monkeypatch, tmp_path):
    """learn_success 公共打桩：SIMPLE 复杂度（绕 LLM）+ persist 落库 stub。"""
    import swarm.brain.learn_store as ls
    from swarm.brain import nodes as brain_nodes
    from swarm.types import Complexity

    monkeypatch.setattr(brain_nodes, "effective_complexity", lambda s: Complexity.SIMPLE)
    monkeypatch.setattr(brain_nodes, "_get_project_path", lambda pid: str(tmp_path))

    # ★32 号文批2b 双复核 reviewer F2★：交付成功后 learn_success 会把产出做增量索引
    # （enqueue_kb_update → PG kb_update_events 真 sink，brain/nodes/__init__.py:6632
    # 惰性 import）。本夹具命题是 degraded 记账而非增量索引，不打桩则真触盘——
    # hunter LOW-1 同型，learn_chain 侧已先治，此处对齐。
    import swarm.knowledge.hooks as _hooks
    monkeypatch.setattr(_hooks, "schedule_incremental_update", lambda *a, **k: None)

    async def _persist(state, parsed):
        return {"persisted": False, "reason": "test_stub"}

    monkeypatch.setattr(ls, "persist_learn_success", _persist)
    return brain_nodes


class TestD1D2LearnSuccess:
    async def test_delivery_exception_enters_degraded(self, tmp_path, monkeypatch):
        """D1-下半：交付链整体异常必须进 _degraded（另三条失败臂都进）——
        否则 should_write_success 看不见，'没交付成功'被 L6 学成成功。"""
        brain_nodes = _patch_ls_common(monkeypatch, tmp_path)

        async def _boom(*_a, **_kw):
            raise RuntimeError("apply exploded")

        monkeypatch.setattr(brain_nodes, "_deliver_merged_diff_serialized", _boom)
        state = _ls_state(tmp_path)
        out = await brain_nodes.learn_success(state)
        assert "delivery_commit_exception" in (state.get("degraded_reasons") or [])
        assert "delivery_commit_exception" in (out.get("degraded_reasons") or [])

    async def test_proj_path_missing_degraded(self, tmp_path, monkeypatch, caplog):
        """D2-下半：proj_path None 整块跳过 commit 必须 WARNING+机读降级。"""
        brain_nodes = _patch_ls_common(monkeypatch, tmp_path)
        monkeypatch.setattr(brain_nodes, "_get_project_path", lambda pid: None)
        with caplog.at_level(logging.WARNING, logger="swarm.brain.nodes"):
            state = _ls_state(tmp_path)
            out = await brain_nodes.learn_success(state)
        assert "delivery_project_path_missing" in (state.get("degraded_reasons") or [])
        assert "delivery_project_path_missing" in (out.get("degraded_reasons") or [])
        assert any("proj_path 解析失败" in r.getMessage() for r in caplog.records)

    async def test_clean_delivery_no_new_degraded(self, tmp_path, monkeypatch):
        """对照：正常交付不产生新降级键（防误报方向）。"""
        brain_nodes = _patch_ls_common(monkeypatch, tmp_path)

        async def _ok_deliv(*_a, **_kw):
            return {"ap": {"ok": True, "applied": ["x.py"], "failed": []},
                    "out_files": ["x.py"], "wm": {}, "wm_error": None,
                    "commit": {"ok": True, "committed": False, "reason": "无已暂存改动"}}

        monkeypatch.setattr(brain_nodes, "_deliver_merged_diff_serialized", _ok_deliv)
        state = _ls_state(tmp_path)
        out = await brain_nodes.learn_success(state)
        assert not (state.get("degraded_reasons") or [])
        assert "degraded_reasons" not in out


# ═══════════════════════ D4下：merge fold 异常臂机读降级 ═══════════════════════

def _patch_merge_engine(monkeypatch, merged_diff: str):
    """merge() 节点干净合并打桩（形状照抄 test_secret_gate_t2）。"""
    from swarm.brain import merge_engine
    from swarm.brain import nodes as brain_nodes

    def _fake_merge_diffs(subtask_diffs, *, base_reader=None, subtask_order=None, **_kw):
        return merge_engine.MergeResult(merged_diff=merged_diff, success=True)

    monkeypatch.setattr(merge_engine, "merge_diffs", _fake_merge_diffs)
    monkeypatch.setattr(
        merge_engine, "filter_orphan_module_patches",
        lambda diffs, base_module_exists=None, is_multimodule=True: (diffs, {}))
    monkeypatch.setattr(
        merge_engine, "verify_merged_patch_applies",
        lambda proj, diff, base_ref=None: (True, ""))
    monkeypatch.setattr(brain_nodes, "_make_base_reader", lambda state: (lambda p: None))


class TestD4MergeFoldDegraded:
    _DIFF = (
        "diff --git a/x.py b/x.py\nnew file mode 100644\n"
        "index 0000000..e69de29\n--- /dev/null\n+++ b/x.py\n"
        "@@ -0,0 +1 @@\n+print(1)\n")

    def _run(self, monkeypatch):
        from swarm.brain.nodes import merge
        from swarm.types import WorkerOutput

        _patch_merge_engine(monkeypatch, self._DIFF)
        state = {
            "task_id": "t-fold", "project_id": "", "base_commit": None, "plan": None,
            "subtask_results": {
                "st-1": WorkerOutput(subtask_id="st-1", diff=self._DIFF,
                                     summary="x", l1_passed=True),
            },
        }
        return merge(state)

    def test_fold_exception_enters_degraded_reasons(self, monkeypatch, caplog):
        import swarm.brain.manifest_synth as ms

        def _boom(*_a, **_kw):
            raise RuntimeError("fold exploded")

        monkeypatch.setattr(ms, "fold_module_registrations", _boom)
        with caplog.at_level(logging.ERROR, logger="swarm.brain.nodes"):
            out = self._run(monkeypatch)
        assert "merge_d1_fold_failed" in (out.get("degraded_reasons") or []), \
            "fold 异常沿用原 diff 必须机读降级（L6/人工闸不再全瞎）"
        assert out.get("merged_diff") == self._DIFF, "fail-open 沿用原 diff 方向不变"
        assert any("D1 聚合清单合成异常" in r.getMessage() for r in caplog.records)

    def test_fold_ok_no_degraded(self, monkeypatch):
        out = self._run(monkeypatch)
        assert "merge_d1_fold_failed" not in (out.get("degraded_reasons") or [])

    def test_clean_accept_arm_fold_exception_degraded(self, monkeypatch, caplog):
        """★双复核 reviewer F1★ rebase 超限温和出口是第二个 D1 fold 落点——同型
        异常臂也必须机读降级（fail-open 沿用原 diff+终态 PARTIAL 方向不变）。"""
        import swarm.brain.manifest_synth as ms
        from swarm.brain import merge_engine
        from swarm.brain import nodes as brain_nodes
        from swarm.brain.nodes import merge
        from swarm.types import WorkerOutput

        pom_diff = (
            "diff --git a/pom.xml b/pom.xml\nindex 1111111..2222222 100644\n"
            "--- a/pom.xml\n+++ b/pom.xml\n@@ -1 +1,2 @@\n <project>\n"
            "+<modules><module>m</module></modules>\n")
        merged_diff = (
            "diff --git a/x.py b/x.py\nnew file mode 100644\n"
            "index 0000000..e69de29\n--- /dev/null\n+++ b/x.py\n"
            "@@ -0,0 +1 @@\n+print(1)\n")

        def _fake_merge_diffs(subtask_diffs, *, base_reader=None, subtask_order=None, **_kw):
            return merge_engine.MergeResult(
                merged_diff=merged_diff, success=True,
                rebase_subtask_ids=["st-1"], rebase_origin={"st-1": "three_way"})

        monkeypatch.setattr(merge_engine, "merge_diffs", _fake_merge_diffs)
        monkeypatch.setattr(
            merge_engine, "filter_orphan_module_patches",
            lambda diffs, base_module_exists=None, is_multimodule=True: (diffs, {}))
        monkeypatch.setattr(
            merge_engine, "verify_merged_patch_applies",
            lambda proj, diff, base_ref=None: (True, ""))
        monkeypatch.setattr(brain_nodes, "_make_base_reader", lambda state: (lambda p: None))

        def _boom(*_a, **_kw):
            raise RuntimeError("fold exploded")

        monkeypatch.setattr(ms, "fold_module_registrations", _boom)
        state = {
            "task_id": "t-fold-ca", "project_id": "", "base_commit": None, "plan": None,
            "subtask_rebase_counts": {"st-1": 10_000},  # 恒超 max_rebase
            "subtask_results": {
                "st-1": WorkerOutput(subtask_id="st-1", diff=pom_diff,
                                     summary="x", l1_passed=True),
            },
        }
        with caplog.at_level(logging.ERROR, logger="swarm.brain.nodes"):
            out = merge(state)
        assert out.get("merge_rebase_dropped") == ["st-1"], "温和出口前提（夹具没驱动到）"
        assert "merge_d1_fold_failed" in (out.get("degraded_reasons") or []), \
            "温和出口 fold 异常也必须机读降级"
        assert out.get("merged_diff") == merged_diff, "fail-open 沿用原 diff 方向不变"
        assert any("温和出口聚合清单合成异常" in r.getMessage() for r in caplog.records)


# ═══════════════════════ D2上：rebase 计数表同签名剪枝 ═══════════════════════

def _mk_st(sid, desc, writable=()):
    from swarm.types import FileScope, SubTask
    return SubTask(id=sid, description=desc, scope=FileScope(
        writable=list(writable), create_files=[], readable=[], delete_files=[]))


class _MiniPlan:
    def __init__(self, subs):
        self.subtasks = subs
        self.finisher_attached = {}


class TestD2SurgicalRebaseCounts:
    def test_prunes_by_signature(self):
        from swarm.brain.nodes import _surgical_replan_reset

        old_plan = _MiniPlan([_mk_st("st-1", "做A", ("a.py",)),
                              _mk_st("st-2", "做B", ("b.py",))])
        # st-1 签名不变（可继承旧账）；st-2 描述变=语义新子任务（绝不继承）
        new_plan = _MiniPlan([_mk_st("st-1", "做A", ("a.py",)),
                              _mk_st("st-2", "做B改", ("b.py",))])
        out = _surgical_replan_reset(
            {}, old_plan, new_plan,
            old_rebase_counts={"st-1": 2, "st-2": 3, "st-gone": 1})
        assert out["subtask_rebase_counts"] == {"st-1": 2}, \
            "签名不一致/不在新 plan 的 rebase 计数必须剪掉（陈旧计数⇒首个 rebase 即超限）"

    def test_call_sites_wired(self):
        """接线锁（AST 数实参，非字面量）：_surgical_replan_reset 的每个生产调用点
        都必须传 old_rebase_counts——漏接=剪枝机制存在但生产不生效（半落地）。"""
        src = open("brain/nodes/__init__.py", encoding="utf-8").read()
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "id", "") == "_surgical_replan_reset"]
        assert len(calls) == 2, f"调用点数量漂移（{len(calls)}≠2）——新增调用点也必须接线"
        for c in calls:
            kws = {k.arg for k in c.keywords}
            assert "old_rebase_counts" in kws, \
                f"调用点(line {c.lineno})漏传 old_rebase_counts"


# ═══════════════════════ D3上：batch attempts 解析守卫 ═══════════════════════

class TestD3BatchAttempts:
    @pytest.mark.parametrize("raw,expect,warns", [
        (None, 2, False), ("5", 5, False), ("abc", 2, True),
        ("0", 2, True), ("-3", 2, True),
    ])
    def test_resolve(self, monkeypatch, caplog, raw, expect, warns):
        from swarm.brain.nodes import _resolve_plan_batch_max_attempts

        if raw is None:
            monkeypatch.delenv("SWARM_PLAN_BATCH_MAX_ATTEMPTS", raising=False)
        else:
            monkeypatch.setenv("SWARM_PLAN_BATCH_MAX_ATTEMPTS", raw)
        with caplog.at_level(logging.ERROR, logger="swarm.brain.nodes"):
            assert _resolve_plan_batch_max_attempts() == expect
        assert bool(caplog.records) == warns, \
            f"raw={raw!r} 非法/非正必须 error 日志（旧裸 int 炸 plan 节点/零尝试全败）"


# ═══════════════════════ S2：水平合并不丢 harness/contract ═══════════════════════

def _hst(i, vc, contract, build="mvn compile", harness=True):
    from swarm.types import FileScope, SubTask, TaskHarness
    return SubTask(
        id=f"st-{i}", description=f"功能{i}",
        scope=FileScope(writable=[f"f{i}.java"], create_files=[], readable=[],
                        delete_files=[]),
        harness=(TaskHarness(language="java", build_command=build,
                             verify_commands=vc) if harness else None),
        contract=contract, acceptance_criteria=[], covers=[], depends_on=[])


class TestS2HorizontalMerge:
    def _merge(self, subs):
        from swarm.brain.nodes.shared import _merge_horizontal_subtasks
        from swarm.types import TaskPlan
        return _merge_horizontal_subtasks(
            TaskPlan(subtasks=subs, parallel_groups=[], shared_contract={}))

    def test_verify_commands_union_not_dropped(self):
        m = self._merge([_hst(1, ["cmd a"], {}), _hst(2, ["cmd b", "cmd a"], {}),
                         _hst(3, [], {})])
        assert len(m.subtasks) == 1
        assert m.subtasks[0].harness.verify_commands == ["cmd a", "cmd b"], \
            "非 base 成员的 verify_commands 绝不许丢（丢了=那些验收 L1 永不跑）"

    def test_contract_union_not_dropped(self):
        m = self._merge([_hst(1, [], {"IA": {"x": 1}}), _hst(2, [], {"IB": {"y": 2}})])
        assert m.subtasks[0].contract == {"IA": {"x": 1}, "IB": {"y": 2}}

    def test_contract_conflict_warns_keeps_base(self, caplog):
        with caplog.at_level(logging.WARNING, logger="swarm.brain.nodes.shared"):
            m = self._merge([_hst(1, [], {"IA": {"v": "base"}}),
                             _hst(2, [], {"IA": {"v": "other"}})])
        assert m.subtasks[0].contract["IA"] == {"v": "base"}
        assert any("契约键" in r.getMessage() and "冲突" in r.getMessage()
                   for r in caplog.records), "契约冲突必须 WARNING 可观测"

    def test_harness_default_member_tolerated(self):
        """成员 harness 为默认空壳（无 verify_commands）时合并不丢另一方的断言。
        （SubTask.harness pydantic 保证非 None，None 成员形态在生产不可达。）"""
        m = self._merge([_hst(1, ["cmd a"], {}), _hst(2, ["cmd b"], {}, build="")])
        assert m.subtasks[0].harness.verify_commands == ["cmd a", "cmd b"]

    def test_scalar_first_nonempty(self):
        m = self._merge([_hst(1, [], {}, build=""), _hst(2, [], {}, build="mvn -q compile")])
        assert m.subtasks[0].harness.build_command == "mvn -q compile", \
            "base 空 scalar 时取组内首个非空（不丢唯一构建命令）"


# ═══════════════════════ S3：Java 测试文件后缀边界 ═══════════════════════

class TestS3TestFileBoundary:
    @pytest.mark.parametrize("path,expect", [
        ("src/main/java/com/x/UserTest.java", True),
        ("src/main/java/com/x/UserTests.java", True),
        ("src/main/java/com/x/Test.java", True),
        ("test.java", True),
        ("src/main/java/com/x/Latest.java", False),   # la|test —— 旧判据误杀
        ("src/main/java/com/x/Contest.java", False),  # con|test —— 旧判据误杀
        ("src/main/java/com/x/LatestTest.java", True),
        ("src/test/java/com/x/Latest.java", True),    # 目录判据不受影响
        ("foo/test_bar.py", True),
        ("foo/bar_test.go", True),
        ("foo/bar.test.ts", True),
        ("src/main/Latest.java", False),
    ])
    def test_boundary(self, path, expect):
        from swarm.brain.nodes.shared import _is_test_file_path
        assert _is_test_file_path(path) is expect

    def test_strip_unrequested_tests_keeps_latest_java(self):
        """接线锁：剥离函数消费同一边界——Latest.java（真源码）必须留在 scope。"""
        from swarm.brain.nodes.shared import _strip_unrequested_tests
        from swarm.types import FileScope, SubTask, TaskPlan

        st = SubTask(id="st-1", description="实现功能",
                     scope=FileScope(writable=["src/Latest.java", "src/UserTest.java"],
                                     create_files=[], readable=[], delete_files=[]),
                     contract={}, acceptance_criteria=[], covers=[], depends_on=[])
        plan = TaskPlan(subtasks=[st], parallel_groups=[], shared_contract={})
        out = _strip_unrequested_tests(plan, "实现一个最新列表功能")
        w = out.subtasks[0].scope.writable
        assert "src/Latest.java" in w, "Latest.java 是真源码，剥出 scope=静默丢需求"
        assert "src/UserTest.java" not in w
