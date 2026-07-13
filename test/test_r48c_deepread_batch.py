"""round48c 深读治本批回归锁（A2/B6/C7/H1/H2/M1/D10）。

深读实锤（两路无偏取证）：三条独立死链——①僵尸重派（换备被 C-4 吞+计数签名重置）
②毒 manifest 生命周期（FAIL 产出入树复制）③900s 硬杀预留挂 TTL 虚增 2M。
"""
from __future__ import annotations

from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan


def _st(sid, create=None, writable=None, deps=None):
    return SubTask(id=sid, description=f"t {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   depends_on=list(deps or []),
                   scope=FileScope(writable=writable or [], create_files=create or []))


class TestB6DispatchPriority:
    def test_manifest_scaffold_dispatched_first(self):
        """B6：清单脚手架在 subtasks 尾部也必须首批派发。"""
        plan = TaskPlan(task_id="t", subtasks=[
            _st("st-1", create=["m/src/A.java"]),
            _st("st-2", create=["m/src/B.java"]),
            _st("st-scaffold-x", create=["x/pom.xml"]),
        ], parallel_groups=[["st-1", "st-2", "st-scaffold-x"]])
        batch = plan.get_dispatch_batch(set(), ["st-1", "st-2", "st-scaffold-x"], 2)
        assert batch[0].id == "st-scaffold-x", "清单子任务优先级 0"

    def test_producer_before_leaf(self):
        plan = TaskPlan(task_id="t", subtasks=[
            _st("st-leaf", create=["m/src/L.java"]),
            _st("st-prod", create=["m/src/P.java"]),
            _st("st-cons", create=["m/src/C.java"], deps=["st-prod"]),
        ], parallel_groups=[["st-leaf", "st-prod", "st-cons"]])
        batch = plan.get_dispatch_batch(set(), ["st-leaf", "st-prod", "st-cons"], 2)
        assert batch[0].id == "st-prod", "被依赖的生产者优先于叶子"

    def test_retry_still_deprioritized(self):
        plan = TaskPlan(task_id="t", subtasks=[
            _st("st-retry", create=["x/pom.xml"]),
            _st("st-fresh", create=["m/src/A.java"]),
        ], parallel_groups=[["st-retry", "st-fresh"]])
        batch = plan.get_dispatch_batch(
            set(), ["st-retry", "st-fresh"], 2, deprioritized={"st-retry"})
        assert [t.id for t in batch] == ["st-fresh", "st-retry"], "Fix F 语义不变"


class TestH2StripContribs:
    _HEAD = ("<project><modules><module>base</module></modules>"
             "<dependencies><dependency><groupId>g</groupId>"
             "<artifactId>keep</artifactId></dependency></dependencies></project>")

    def test_worker_added_ghost_removed_others_kept(self):
        """H2 核心：摘 worker 相对 HEAD 新增的条目；他人贡献保留。"""
        from swarm.worker.workspace_manifest import strip_worker_manifest_contribs
        worker = self._HEAD.replace(
            "</dependencies>",
            "<dependency><groupId>com.ruoyi</groupId>"
            "<artifactId>ruoyi-alarm-core</artifactId></dependency></dependencies>")
        # local = worker 毒 + 他人后来加的合法依赖
        local = worker.replace(
            "</dependencies>",
            "<dependency><groupId>org.springframework.data</groupId>"
            "<artifactId>spring-data-redis</artifactId></dependency></dependencies>")
        new_text, removed = strip_worker_manifest_contribs(
            local, worker, self._HEAD, "ruoyi-framework/pom.xml")
        assert removed == 1
        assert "ruoyi-alarm-core" not in new_text, "worker 幽灵依赖被摘"
        assert "spring-data-redis" in new_text, "他人贡献保留"
        assert "keep" in new_text, "HEAD 原有依赖保留"

    def test_worker_added_module_removed(self):
        from swarm.worker.workspace_manifest import strip_worker_manifest_contribs
        worker = self._HEAD.replace(
            "</modules>", "<module>ghost-mod</module></modules>")
        local = worker
        new_text, removed = strip_worker_manifest_contribs(
            local, worker, self._HEAD, "pom.xml")
        assert removed == 1 and "ghost-mod" not in new_text

    def test_no_additions_noop(self):
        from swarm.worker.workspace_manifest import strip_worker_manifest_contribs
        new_text, removed = strip_worker_manifest_contribs(
            self._HEAD, self._HEAD, self._HEAD, "pom.xml")
        assert removed == 0 and new_text == self._HEAD

    def test_non_pom_passthrough(self):
        from swarm.worker.workspace_manifest import strip_worker_manifest_contribs
        t, n = strip_worker_manifest_contribs("a", "b", "c", "settings.gradle")
        assert (t, n) == ("a", 0)


class TestC7InflightScope:
    def test_scope_settles_leaked_reservation(self):
        from swarm.models import ledger
        token = ledger.begin_inflight_scope()
        rid = ledger.reserve("t-c7-test", est_in=1000, est_out=500, kind="local")
        ledger.register_inflight_rid(rid)
        n = ledger.end_inflight_scope(token, settle_leaked=True)
        assert n == 1, "泄漏预留被即时结算"
        # 二次结算无副作用（rid 已 pop）
        assert ledger.end_inflight_scope(token, settle_leaked=True) == 0

    def test_normal_settle_unregisters(self):
        from swarm.models import ledger
        token = ledger.begin_inflight_scope()
        rid = ledger.reserve("t-c7-test2", est_in=100, est_out=50, kind="local")
        ledger.register_inflight_rid(rid)
        ledger.settle(rid, real_in=90, real_out=40)
        ledger.unregister_inflight_rid(rid)
        assert ledger.end_inflight_scope(token, settle_leaked=True) == 0, \
            "正常结算后作用域残留为空"


class TestM1CdPrefix:
    def test_mvn_normalize_through_prefix(self):
        from swarm.tools.build_tools import _normalize_maven_module_command
        c, ch = _normalize_maven_module_command("cd /workspace && mvn compile ruoyi-system")
        assert ch and c == "cd /workspace && mvn -pl ruoyi-system -am compile"

    def test_git_guard_through_prefix(self):
        from swarm.tools.build_tools import _guard_unhelpful_command
        assert _guard_unhelpful_command("cd /workspace && git diff HEAD") is not None
        assert _guard_unhelpful_command("cd /w && ls") is None


class TestA2LifetimeCap:
    def test_dispatch_totals_monotonic_in_registry(self):
        from swarm.brain.state import ACCOUNTING_KEY_LIFECYCLE
        assert ACCOUNTING_KEY_LIFECYCLE.get("subtask_dispatch_totals") == "monotonic"


class TestD10FallbackDedupe:
    def test_env_chains_exclude_primary(self):
        import re
        from pathlib import Path
        env = Path(__file__).resolve().parent.parent / ".env"
        if not env.is_file():
            return
        text = env.read_text("utf-8")
        for diff in ("TRIVIAL", "MEDIUM", "COMPLEX"):
            m = re.search(rf"^SWARM_MODEL_ROUTING_{diff}=(.+)$", text, re.M)
            f = re.search(rf"^SWARM_MODEL_ROUTING_{diff}_FALLBACK=(.+)$", text, re.M)
            if m and f:
                assert m.group(1).strip() not in [
                    x.strip() for x in f.group(1).split(",")], \
                    f"{diff} fallback 链不得含 primary 自身"


class TestH2ReviewFixes:
    """H2 对抗复核 #1/#2 回归锁。"""

    def test_2_sibling_entries_in_snapshot_not_stripped(self):
        """复核 #2：bootstrap 快照里已有的兄弟条目绝不被当成本 worker 新增误摘。"""
        from swarm.worker.workspace_manifest import strip_worker_manifest_contribs
        head = ("<project><dependencies><dependency><groupId>g</groupId>"
                "<artifactId>base</artifactId></dependency></dependencies></project>")
        # 快照=HEAD+兄弟条目；worker=快照+自己的毒
        snapshot = head.replace(
            "</dependencies>",
            "<dependency><groupId>s</groupId><artifactId>sibling-dep</artifactId>"
            "</dependency></dependencies>")
        worker = snapshot.replace(
            "</dependencies>",
            "<dependency><groupId>com.ruoyi</groupId><artifactId>ghost</artifactId>"
            "</dependency></dependencies>")
        # 用快照作基线（复核 #2 修复后语义）→ 只摘 ghost
        new_text, removed = strip_worker_manifest_contribs(
            worker, worker, snapshot, "pom.xml")
        assert removed == 1
        assert "sibling-dep" in new_text and "ghost" not in new_text
        # 反证：若仍用 HEAD 作基线会误摘 2 条
        _, removed_bad = strip_worker_manifest_contribs(worker, worker, head, "pom.xml")
        assert removed_bad == 2

    def test_1_rollback_requires_actual_write(self, tmp_path):
        """复核 #1：仅 scope 声明（加宽）未实际写过 → 清单绝不被删/改。"""
        import subprocess as sp
        sp.run(["git", "init", "-q", str(tmp_path)], check=True)
        (tmp_path / "pom.xml").write_text("<project/>", "utf-8")
        sp.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
        sp.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c",
                "user.name=t", "commit", "-qm", "base"], check=True)
        # 兄弟新建的模块 pom（不在 HEAD）
        (tmp_path / "x").mkdir()
        (tmp_path / "x" / "pom.xml").write_text("<project>sibling</project>", "utf-8")

        class _Host:
            project_path = str(tmp_path)
            base_ref = None
            _post_sync_contents = {}  # 本 worker 没写过任何清单
            _pre_sync_contents = {}

            class subtask:
                class scope:
                    create_files = []
                    writable = ["x/pom.xml"]  # 仅加宽声明

            def _log(self, msg):
                pass

        from swarm.worker.executor_sync import _SandboxSyncMixin
        _SandboxSyncMixin._rollback_failed_manifest_footprint(_Host(), {})
        assert (tmp_path / "x" / "pom.xml").is_file(), "仅声明未写过 → 绝不删"
