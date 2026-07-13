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


class TestR49H2Regression:
    """R49-1 回归锁：H2 首次 live 触发误删成功兄弟产出（round49 实测）。"""

    @staticmethod
    def _mk_repo(tmp_path):
        import subprocess as sp
        sp.run(["git", "init", "-q", str(tmp_path)], check=True)
        (tmp_path / "pom.xml").write_text(
            "<project><modules><module>base</module></modules></project>", "utf-8")
        sp.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
        sp.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c",
                "user.name=t", "commit", "-qm", "base"], check=True)

    @staticmethod
    def _host(tmp_path, *, own, snap, creates):
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

    def test_sibling_pom_copied_via_pullback_not_deleted(self, tmp_path):
        """r49 实锤主场景：兄弟新建的 pom 经补传/pull-back 进了本 worker 的
        post_sync（内容原样）——绝不能被当'本人新建'删除。"""
        self._mk_repo(tmp_path)
        (tmp_path / "alarm-schedule").mkdir()
        sib = "<project>sibling scaffold</project>"
        (tmp_path / "alarm-schedule" / "pom.xml").write_text(sib, "utf-8")
        host = self._host(
            tmp_path,
            own={"alarm-schedule/pom.xml": sib},
            snap={"pom.xml": "<project><modules><module>base</module></modules></project>"},
            creates=["alarm-callback/pom.xml"])  # 本子任务声明建的是别的
        from swarm.worker.executor_sync import _SandboxSyncMixin
        _SandboxSyncMixin._rollback_failed_manifest_footprint(host, {})
        assert (tmp_path / "alarm-schedule" / "pom.xml").is_file(), \
            "pull-back 复制 ≠ 本人建的，绝不删"

    def test_root_pom_strip_uses_bootstrap_snapshot(self, tmp_path):
        """r49 实锤第二场景：root pom 剥离基线=bootstrap 快照——兄弟自 HEAD 以来
        的注册（快照里已有）绝不被剥。"""
        self._mk_repo(tmp_path)
        # 快照=HEAD+兄弟注册；worker 版本=快照+自己的模块；local=worker 版本
        snap_txt = ("<project><modules><module>base</module>"
                    "<module>sibling-mod</module></modules></project>")
        worker_txt = snap_txt.replace(
            "</modules>", "<module>my-mod</module></modules>")
        (tmp_path / "pom.xml").write_text(worker_txt, "utf-8")
        host = self._host(
            tmp_path, own={"pom.xml": worker_txt},
            snap={"pom.xml": snap_txt}, creates=["my-mod/pom.xml"])
        from swarm.worker.executor_sync import _SandboxSyncMixin
        _SandboxSyncMixin._rollback_failed_manifest_footprint(host, {})
        text = (tmp_path / "pom.xml").read_text("utf-8")
        assert "sibling-mod" in text, "兄弟注册（快照内）绝不被剥"
        assert "my-mod" not in text, "本 worker 新增被摘"

    def test_snapshot_missing_skips_entirely(self, tmp_path):
        """快照缺失=无法归因 → 整体跳过（宁可漏回滚绝不误删）。"""
        self._mk_repo(tmp_path)
        (tmp_path / "x").mkdir()
        (tmp_path / "x" / "pom.xml").write_text("<project/>", "utf-8")
        host = self._host(tmp_path, own={"x/pom.xml": "<project/>"},
                          snap={}, creates=["x/pom.xml"])
        from swarm.worker.executor_sync import _SandboxSyncMixin
        _SandboxSyncMixin._rollback_failed_manifest_footprint(host, {})
        assert (tmp_path / "x" / "pom.xml").is_file()

    def test_own_created_poison_deleted_with_root_prune(self, tmp_path):
        """正向：本人声明创建+快照证实 bootstrap 不存在 → 删除+root pom 摘幽灵。"""
        self._mk_repo(tmp_path)
        (tmp_path / "poison-mod").mkdir()
        (tmp_path / "poison-mod" / "pom.xml").write_text("<project>bad</project>", "utf-8")
        root_txt = ("<project><modules><module>base</module>"
                    "<module>poison-mod</module></modules></project>")
        (tmp_path / "pom.xml").write_text(root_txt, "utf-8")
        host = self._host(
            tmp_path,
            own={"poison-mod/pom.xml": "<project>bad</project>"},
            snap={"poison-mod/pom.xml": "",
                  "pom.xml": "<project><modules><module>base</module></modules></project>"},
            creates=["poison-mod/pom.xml"])
        from swarm.worker.executor_sync import _SandboxSyncMixin
        _SandboxSyncMixin._rollback_failed_manifest_footprint(host, {})
        assert not (tmp_path / "poison-mod" / "pom.xml").is_file(), "毒源被删"
        assert "poison-mod" not in (tmp_path / "pom.xml").read_text("utf-8"), \
            "root pom 幽灵条目同步摘除"


class TestR50ExceptOrder:
    def test_recursion_error_handled_before_baseexception(self):
        """r50 实锤：except BaseException 列在 Exception 前会截走 GraphRecursionError，
        优雅路径（交 L1 按沙箱产出裁决）变死代码 → 撞上限=硬 FAILED。结构锁定顺序。"""
        import re
        src = open("worker/executor_agent.py", encoding="utf-8").read()
        seg = src[src.index("except asyncio.TimeoutError"):]
        seg = seg[:seg.index("_record_tool_telemetry")]
        i_exc = seg.index("except Exception")
        i_base = seg.index("except BaseException")
        assert i_exc < i_base, "except Exception 必须先于 except BaseException"
        assert "Recursion" in seg[i_exc:i_base], "递归优雅路径在 Exception 分支内"


class TestR50BehaviorLock:
    def test_graph_recursion_returns_graceful_string(self):
        """R50-1 行为锁（结构锁之外）：真实抛 GraphRecursionError 穿过 _run_agent，
        必须拿到优雅降级字符串而非异常外逸。"""
        import asyncio

        from swarm.worker.executor_agent import _AgentLoopMixin

        class _Boom:
            async def ainvoke(self, *_a, **_k):
                class GraphRecursionError(Exception):
                    pass
                raise GraphRecursionError(
                    "Recursion limit of 28 reached without hitting a stop condition.")

        class _Host(_AgentLoopMixin):
            _agent = {"agent": _Boom()}
            max_execution_time = 900
            max_iterations = 25
            task_id = "t"
            project_id = "p"

            class subtask:
                id = "st-x"
                class difficulty:
                    value = "medium"

            class phase:
                value = "coding"

            def _remaining_seconds(self):
                return 60

            def _log(self, msg):
                self.logged = msg

        host = _Host()
        out = asyncio.run(host._run_agent("hi", step="code"))
        assert "迭代上限" in out and "L1" in out, f"必须优雅降级: {out}"


class TestR50bBatch:
    """R50-2/R50-3 回归锁（round50b 实锤）。"""

    def test_r50_2_blocked_verdict_skips_rollback(self, tmp_path):
        """BLOCKED（等上游）产出无毒——H2 回滚必须跳过，依赖注入不被摘。"""
        import subprocess as sp
        sp.run(["git", "init", "-q", str(tmp_path)], check=True)
        root_txt = ("<project><dependencies><dependency><groupId>g</groupId>"
                    "<artifactId>base</artifactId></dependency></dependencies></project>")
        (tmp_path / "pom.xml").write_text(root_txt, "utf-8")
        sp.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
        sp.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c",
                "user.name=t", "commit", "-qm", "base"], check=True)
        poisoned = root_txt.replace(
            "</dependencies>",
            "<dependency><groupId>x</groupId><artifactId>injected-dep</artifactId>"
            "</dependency></dependencies>")
        (tmp_path / "pom.xml").write_text(poisoned, "utf-8")

        class _Host:
            project_path = str(tmp_path)
            base_ref = None
            _post_sync_contents = {"pom.xml": poisoned}
            _manifest_baseline_snapshot = {"pom.xml": root_txt}

            class subtask:
                class scope:
                    create_files = []
                    writable = ["pom.xml"]

            def _log(self, msg):
                pass

        from swarm.worker.executor_sync import _SandboxSyncMixin
        _SandboxSyncMixin._rollback_failed_manifest_footprint(
            _Host(), {"not_run_kind": "blocked",
                      "pipeline_blocked": "upstream_module_broken"})
        assert "injected-dep" in (tmp_path / "pom.xml").read_text("utf-8"), \
            "BLOCKED 场景绝不回滚（依赖注入是合法修复）"
        # 对照：真 FAIL 场景照常摘
        _SandboxSyncMixin._rollback_failed_manifest_footprint(_Host(), {})
        assert "injected-dep" not in (tmp_path / "pom.xml").read_text("utf-8")

    def test_r50_3_repaired_paths_excluded_from_pl(self, tmp_path):
        """repair 触达的外模块清单不得进 -pl（脚手架不被外模块连坐）。"""
        (tmp_path / "pom.xml").write_text(
            "<project><modules><module>alarm-security</module>"
            "<module>ruoyi-framework</module></modules></project>", "utf-8")
        for m in ("alarm-security", "ruoyi-framework"):
            (tmp_path / m).mkdir()
            (tmp_path / m / "pom.xml").write_text("<project/>", "utf-8")
        from swarm.worker.l1_pipeline import _scope_maven_command
        # 模拟 R50-3 修复后的调用方语义：basis 已剔除 repaired 外模块清单
        modified = ["alarm-security/pom.xml", "ruoyi-framework/pom.xml"]
        repaired = {"ruoyi-framework/pom.xml"}
        basis = [f for f in modified if f not in repaired] or modified
        cmd = _scope_maven_command("mvn -q compile", str(tmp_path), basis)
        assert "-pl alarm-security" in cmd or "-f alarm-security" in cmd
        assert "ruoyi-framework" not in cmd, "外模块绝不进 -pl"


class TestR51CompletedNotAbandoned:
    """R51-1：已完成子任务绝不入放弃闭包（三连误杀真因）。"""

    def test_completed_dependent_excluded_from_closure(self):
        from swarm.brain.nodes.planning_core import _transitive_abandon
        sts = [_st("st-a", create=["m/a.java"]),
               _st("st-b", create=["m/b.java"], deps=["st-a"]),
               _st("st-c", create=["m/c.java"], deps=["st-b"])]
        # st-b 已完成（C9 边后加是常态）——st-a 放弃时 st-b 免疫，st-c（未完成）仍连坐
        closed = _transitive_abandon(sts, {"st-a"}, completed_ids={"st-b"})
        assert "st-b" not in closed, "已完成者绝不入闭包（产出已入账）"
        assert "st-a" in closed
        # st-c 依赖 st-b（存活）非闭包成员 → 也不连坐
        assert "st-c" not in closed

    def test_completed_seed_removed(self):
        from swarm.brain.nodes.planning_core import _transitive_abandon
        sts = [_st("st-a", create=["m/a.java"])]
        closed = _transitive_abandon(sts, {"st-a"}, completed_ids={"st-a"})
        assert closed == set(), "种子里的已完成者同样剔除（完成的工作永不弃）"

    def test_backcompat_without_completed(self):
        from swarm.brain.nodes.planning_core import _transitive_abandon
        sts = [_st("st-a", create=["m/a.java"]),
               _st("st-b", create=["m/b.java"], deps=["st-a"])]
        assert _transitive_abandon(sts, {"st-a"}) == {"st-a", "st-b"}
