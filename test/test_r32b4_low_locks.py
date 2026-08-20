"""★32 号文批4 LOW 收口锁★ 治法配套锁集（突变区分力见 scripts/r32b4_mutation_check.py）。

覆盖：S5-右界/S6/S7/S8/S9/F-2/F-3/LOW-2（shared）· P2/P4/P5（dispatch）·
E3/E4（executor_sync）· funnel（security_scan/planning_core）· A10 簇（planning_core）·
D3/D4/D5/D6（nodes/__init__）· O3/R2/R3（runtime_smoke）· L-2/R4-LOW1（l1_pipeline）。

纪律：重活函数（merge/validate 主链）用 AST 接线锁（断"机制被接上"事实，不断字面量）；
纯函数/小函数一律行为锁（真入口驱动，断言区分力指向本治法而非通用兜底）。
"""
from __future__ import annotations

import ast
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _ast(rel: str) -> ast.Module:
    return ast.parse((ROOT / rel).read_text(encoding="utf-8"))


def _func(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"函数 {name} 不存在（接线锁前提失效）")


# ═══════════════════ shared.py：S8 语言关键词边界 ═══════════════════

class TestS8GuardedKeywords:
    """S8：is_lang 回退关键词必须标识符边界匹配——子串判据三起误配
    （java⊂javascript / react⊂reactor / " go " 对 CJK 邻接双错）。"""

    def _lang(self, desc: str) -> str:
        from swarm.brain.nodes.shared import _infer_harness
        scope = SimpleNamespace(writable=[], create_files=[], readable=[])
        return _infer_harness(desc, scope).language

    def test_javascript_goes_node_not_java(self):
        """纯 javascript 描述 ⇒ node 臂（旧子串判据 java⊂javascript 撞 JVM 臂）。"""
        assert self._lang("写一个 javascript 前端校验脚本") == "node"

    def test_reactor_not_node(self):
        """Maven reactor 多模块构建 ⇒ java 臂（react⊂reactor 不得撞 node 臂）。"""
        assert self._lang("maven reactor 多模块构建，聚合 pom 管理") == "java"

    def test_go_cjk_adjacency(self):
        """CJK 邻接放行："用go开发" 判 go（旧 " go " 带空格形态漏判）。"""
        assert self._lang("用go开发一个微服务") == "go"

    def test_golang_not_swallowed_by_go_boundary(self):
        """golang 走独立关键词（go 边界拒 golang 延续，golang 关键词兜底）。"""
        assert self._lang("golang api 服务") == "go"

    def test_springframework_oneword_falls_generic(self):
        """边界语义如实登记：连写 springframework 不匹配 spring（兜底臂在场，
        误拒≠无 harness——宁窄不宽，子串放行的误配方向才是病灶）。"""
        assert self._lang("springframework 项目改造") == ""

    def test_python_still_hits(self):
        assert self._lang("用python写一个命令行工具") == "python"


# ═══════════════════ shared.py：S5-右界 / F-2 / F-3 / LOW-2 / S7 / S9 ═══════════════════

class TestS5RightBoundary:
    """S5-右界：证据引用右界补 `.`——"Config.java" 不得在 "Config.java.bak" 里假命中。"""

    def test_bak_suffix_rejected_both_arms(self):
        from swarm.brain.nodes.shared import _evidence_mentions
        blob = "备份文件 Config.java.bak 与 main/App.java.bak 可忽略"
        assert not _evidence_mentions(blob, "Config.java", is_basename=True)
        assert not _evidence_mentions(blob, "main/App.java", is_basename=False)

    def test_line_number_tail_still_matches(self):
        """方向锁：`:12` 行号尾巴仍放行（`:` 不在排除集）。"""
        from swarm.brain.nodes.shared import _evidence_mentions
        assert _evidence_mentions("Config.java:12: error: 找不到符号", "Config.java",
                                  is_basename=True)
        assert _evidence_mentions("main/App.java:3: error", "main/App.java",
                                  is_basename=False)


class TestF2L2CmdHeadFamily:
    """F-2：命令头族补 mvn 中间相位/旗标形、mvnw wrapper 形、npm run 形。"""

    def _cmd(self, criteria: str) -> str:
        from swarm.brain.nodes.shared import _l2_test_command_from_criteria
        return _l2_test_command_from_criteria([criteria])

    def test_mvn_phase_before_test(self):
        assert self._cmd("请执行 mvn clean test 验证全部功能") == "mvn clean test"

    def test_mvn_flag_before_test(self):
        assert self._cmd("运行 mvn -q test 即可") == "mvn -q test"

    def test_mvnw_wrapper(self):
        assert self._cmd("用 ./mvnw test 跑测试") == "./mvnw test"

    def test_npm_run_test(self):
        assert self._cmd("前端跑 npm run test 验证") == "npm run test"

    def test_plain_forms_unchanged(self):
        """方向锁：裸形照旧（治法只加不减）。"""
        assert self._cmd("mvn test 验证") == "mvn test"
        assert self._cmd("npm test 验证") == "npm test"

    def test_chinese_tail_still_truncated(self):
        """方向锁：批3 S4 中文尾巴截断语义不回退。"""
        assert self._cmd("mvn clean test 验证所有功能") == "mvn clean test"

    def test_value_flag_forms_recognized(self):
        """★批4 R5 reviewer LOW-4★：分值旗标形（值必随旗标）——漏识别=显式要求的
        测试被静默 skip=假过。"""
        assert self._cmd("执行 mvn -T 4 test 验证") == "mvn -T 4 test"
        assert self._cmd("执行 mvn -P prod test 验证") == "mvn -P prod test"
        assert self._cmd("执行 mvn -f pom.xml test 验证") == "mvn -f pom.xml test"

    def test_skip_test_flags_not_recognized(self):
        """★批4 R5 hunter MED-2★：跳测旗标生效形态（裸形=Maven -D 默认 true、
        =true 族；中间臂与尾巴两位置）⇒ 视同未识别（返回 ""=诚实 skip+降级账）。
        真跑 `mvn -DskipTests test` 是 rc=0 零测试 ⇒ 观测信号从 passed:unverified
        洗成 passed=假过。"""
        assert self._cmd("执行 mvn -DskipTests test 打包") == ""
        assert self._cmd("执行 mvn test -DskipTests 即可") == ""
        assert self._cmd("执行 mvn -Dmaven.test.skip=true test 即可") == ""
        assert self._cmd("执行 mvn -DskipTests=true test 即可") == ""

    def test_skip_flag_false_value_still_recognized(self):
        """方向锁（r32b3 S4 语料互证）：=false/='否' 是显式打开测试，不得被后滤
        冤杀回 skip（粗粒度"含旗标即拒"的治法自伤边）。"""
        assert self._cmd("执行 mvn test -DskipTests=false 验证") == \
            "mvn test -DskipTests=false"
        assert self._cmd("执行 mvn test -DskipTests='否' 通过") == \
            "mvn test -DskipTests='否'"

    def test_prose_words_not_swallowed_into_head(self):
        """方向锁（LOW-4 治法的另一向）：散文裸词（非旗标值）不得进命令头——
        识别不出=回退诚实 skip（绝不把 "and"/"run" 吞进头造假命令）。"""
        assert self._cmd("先 mvn build and test 再说") == ""


def _mk_plan(writers: dict[str, list[str]]):
    subs = []
    files_to_sids: dict[str, list[str]] = writers
    sid_files: dict[str, list[str]] = {}
    for f, sids in files_to_sids.items():
        for sid in sids:
            sid_files.setdefault(sid, []).append(f)
    for sid, fs in sid_files.items():
        subs.append(SimpleNamespace(
            id=sid, scope=SimpleNamespace(create_files=fs, writable=[], readable=[])))
    return SimpleNamespace(subtasks=subs)


class TestF3SuffixAmbiguous:
    """F-3：needle 是另一写者路径的段对齐后缀时，短 needle 路径臂失格（fail-closed）。"""

    def _attr(self, writers, blob):
        from swarm.brain.nodes.shared import attribute_l2_failure
        plan = _mk_plan(writers)
        details = {"integration_review": {"compile_output": blob}, "issues": []}
        results = {sid: object() for sids in writers.values() for sid in sids}
        return attribute_l2_failure(plan, details, results)

    def test_long_path_evidence_attributes_only_long(self):
        """证据打长路径 ⇒ 只归因长路径写者（短 needle 不得在长路径证据里假命中）。"""
        out = self._attr({"a/b/C.java": ["s1"], "x/a/b/C.java": ["s2"]},
                         "x/a/b/C.java:12: error: 找不到符号")
        assert out == ["s2"], f"短 needle 后缀歧义必须失格，实际 {out!r}"

    def test_short_form_evidence_fails_closed(self):
        """证据只打短形态 ⇒ 消歧不出 ⇒ None（回退全量 replan，绝不误归因）。"""
        out = self._attr({"a/b/C.java": ["s1"], "x/a/b/C.java": ["s2"]},
                         "a/b/C.java:9: error: 编译失败")
        assert out is None, f"歧义形态必须归因不出（fail-closed），实际 {out!r}"

    def test_unambiguous_path_still_attributes(self):
        """方向锁：无后缀歧义时路径臂照旧工作。"""
        out = self._attr({"a/b/C.java": ["s1"], "d/E.java": ["s2"]},
                         "d/E.java:1: error")
        assert out == ["s2"]

    def test_three_writer_misattribution_blocked(self):
        """★批4 R5 hunter LOW-3★：登记危害的真形=三写者连坐——证据打长路径时，
        无闸旧码 failed=[s1,s2] 恰为全集真子集 ⇒ s1 无辜被定向重派（两写者时
        全集守卫否决回 None，危害兑现不了）。有闸 ⇒ 短 needle 路径臂失格 +
        basename 非唯一失格 ⇒ 只归 s2。"""
        out = self._attr(
            {"a/b/C.java": ["s1"], "x/a/b/C.java": ["s2"], "d/E.java": ["s3"]},
            "x/a/b/C.java:12: error: 找不到符号")
        assert out == ["s2"], f"s1 不得被后缀歧义连坐，实际 {out!r}"


class TestLow2WritersNormalized:
    """hunter LOW-2：build_writers_by_file 的 scope 路径必须过 _norm_scope_path
    （"./src/A.java" 与 "src/A.java" 同键——否则 basename 唯一性统计全以错键进行）。"""

    def test_dot_slash_and_plain_same_key(self):
        from swarm.brain.nodes.shared import build_writers_by_file
        plan = SimpleNamespace(subtasks=[
            SimpleNamespace(id="s1", scope=SimpleNamespace(
                create_files=["./src/A.java"], writable=[], readable=[])),
            SimpleNamespace(id="s2", scope=SimpleNamespace(
                create_files=[], writable=["src/A.java"], readable=[])),
        ])
        writers = build_writers_by_file(plan)
        assert writers == {"src/A.java": ["s1", "s2"]}, f"归一后必须同键，实际 {writers!r}"

    def test_whitespace_padded_entry_same_key(self):
        """★批4 R5 双复核 LOW-3/LOW-6★：换归一源不得丢旧 `.strip()` 的空白剥离——
        " src/A.java"（LLM 产出偶发杂散空白）归一后仍带空白=同源不同键病对空白
        形态重开。"""
        from swarm.brain.nodes.shared import build_writers_by_file
        plan = SimpleNamespace(subtasks=[
            SimpleNamespace(id="s1", scope=SimpleNamespace(
                create_files=[" src/A.java "], writable=[], readable=[])),
            SimpleNamespace(id="s2", scope=SimpleNamespace(
                create_files=[], writable=["src/A.java"], readable=[])),
        ])
        writers = build_writers_by_file(plan)
        assert writers == {"src/A.java": ["s1", "s2"]}, \
            f"首尾空白必须剥离，实际 {writers!r}"


class TestS7ClauseSplit:
    """S7：裸 `并` 分隔符不得拦腰切断【并行/并发/并且/并存】。"""

    def test_bingxing_not_split(self):
        """"开发并行任务框架 a.py"：开发（create 词）与 a.py 同句 ⇒ create；
        旧裸 `并` 切成 "行任务框架 a.py" ⇒ 无意图词 ⇒ 误判 modify。"""
        from swarm.brain.nodes.shared import _classify_file_ops
        ops = _classify_file_ops("开发并行任务框架 a.py")
        assert ops["create"] == ["a.py"] and not ops["modify"], f"实际 {ops!r}"

    def test_true_bing_still_splits(self):
        """方向锁：真分隔 `并`（非四词前缀）照旧切分。"""
        from swarm.brain.nodes.shared import _classify_file_ops
        ops = _classify_file_ops("新建 a.py 并修改 b.py")
        assert ops["create"] == ["a.py"] and ops["modify"] == ["b.py"], f"实际 {ops!r}"


class TestS9StripTestsWarning:
    """S9：harness.test_command 清空失败（冻结石雕/只读代理）必须 WARNING 可闻。"""

    def test_frozen_harness_warns(self, caplog):
        from swarm.brain.nodes.shared import _strip_unrequested_tests

        class _FrozenHarness:
            test_command = "pytest -q"

            def __setattr__(self, k, v):
                raise AttributeError("frozen")

        st = SimpleNamespace(id="st-1", harness=_FrozenHarness(),
                             scope=SimpleNamespace(create_files=[], writable=[], readable=[]))
        plan = SimpleNamespace(subtasks=[st])
        with caplog.at_level(logging.WARNING):
            _strip_unrequested_tests(plan, "改一下按钮颜色")
        assert "测试剔除：harness.test_command 清空失败" in caplog.text, \
            "清空失败=测试门没拆掉，降级必须可观测"


class TestS6RebuildSingleSource:
    """S6/B-1：_merge_horizontal_subtasks 的 TaskPlan 重建必须走单源 _rebuild_plan
    （finisher_attached/symbol_cycle_pairs/symbol_exam_dropped/symbol_exam_zeroed
    四张 plan 级账全携带）——AST 接线锁：import 在场 + 真调用在场。"""

    def test_rebuild_via_single_source(self):
        fn = _func(_ast("brain/nodes/shared.py"), "_merge_horizontal_subtasks")
        imported = any(
            isinstance(n, ast.ImportFrom) and n.module == "swarm.brain.planning_nodes"
            and any(a.name == "_rebuild_plan" for a in n.names)
            for n in ast.walk(fn))
        called = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name) and n.func.id == "_rebuild_plan"
            for n in ast.walk(fn))
        # ★批4 R5 reviewer 观察项★：model_copy(update={"parallel_groups": ...}) 也要钉——
        # 只锁 _rebuild_plan 调用时，删掉 parallel_groups 恢复（合并后各子任务独立成组）
        # 锁仍绿=覆盖边界。parallel_groups 错位=调度把无依赖子任务串行化/错组。
        model_copy_pg = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == "model_copy"
            and any(kw.arg == "update"
                    and isinstance(kw.value, ast.Dict)
                    and any(isinstance(k, ast.Constant) and k.value == "parallel_groups"
                            for k in kw.value.keys)
                    for kw in n.keywords)
            for n in ast.walk(fn))
        assert imported and called and model_copy_pg, \
            "重建不走 _rebuild_plan 单源 ⇒ 四张 plan 级账在该重建点静默丢失（B-1 族）；" \
            "parallel_groups 不随合并重建 ⇒ 调度分组错位"


# ═══════════════════ dispatch.py：P2 / P4 / P5 ═══════════════════

class TestP2RollFactor:
    """P2：SWARM_DISPATCH_ROLL_FACTOR 空/负必须 WARNING+回退默认 3
    （旧 `or 0`/max(0,) 静默折 0=滚动闸被悄悄关掉）——AST 接线锁。"""

    def test_empty_and_negative_guards_wired(self):
        tree = _ast("brain/nodes/dispatch.py")
        guards = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            t = node.test
            is_empty_guard = (isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not)
                              and isinstance(t.operand, ast.Name) and t.operand.id == "_raw_roll")
            is_neg_guard = (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
                            and t.left.id == "_roll_factor"
                            and any(isinstance(c, ast.Lt) for c in t.ops))
            if is_empty_guard or is_neg_guard:
                guards.append(node)
        assert len(guards) == 2, f"空/负两道守卫必须都在场，实际 {len(guards)}"
        for g in guards:
            warns = any(isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute) and n.func.attr == "warning"
                        for n in ast.walk(g))
            fallback3 = any(isinstance(n, ast.Assign)
                            and any(isinstance(tg, ast.Name) and tg.id == "_roll_factor"
                                    for tg in n.targets)
                            and isinstance(n.value, ast.Constant) and n.value.value == 3
                            for n in ast.walk(g))
            assert warns and fallback3, "守卫臂必须 WARNING + 回退默认 3（静默折 0=闸被关账照打）"


class TestP4ChangesFromDiff:
    """P4：_changes_from_diff 行邻接单趟 + strip_diff_path 漏斗（C-quoted/删除段/新增段）。"""

    def _types(self, diff: str):
        from swarm.brain.nodes.dispatch import _changes_from_diff
        return {c.file_path: str(c.change_type) for c in _changes_from_diff(diff)}

    def test_deleted_modified_added_quoted(self):
        diff = (
            "diff --git a/old.java b/old.java\n"
            "--- a/old.java\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n-x\n"
            "diff --git a/m.java b/m.java\n"
            "--- a/m.java\n"
            "+++ b/m.java\n"
            "@@ -1 +1 @@\n-a\n+b\n"
            "diff --git \"a/pa th/x.java\" \"b/pa th/x.java\"\n"
            "--- /dev/null\n"
            "+++ \"b/pa th/x.java\"\n"
            "@@ -0,0 +1 @@\n+new\n"
        )
        types = self._types(diff)
        assert types.get("old.java", "").endswith("DELETED"), f"删除段：{types!r}"
        assert types.get("m.java", "").endswith("MODIFIED"), f"修改段：{types!r}"
        assert types.get("pa th/x.java", "").endswith("ADDED"), \
            f"C-quoted 带空格路径必须反转义（旧臂捕出 '\"b/pa' 垃圾串）：{types!r}"

    def test_hunk_body_fake_headers_rejected(self):
        """★批4 R5 双复核 LOW-1/LOW-4★：hunk 体内容行伪装头行（删除行内容以 `-- `
        起=渲染 `--- x`、新增行内容以 `++ ` 起=渲染 `+++ x`）不得产出幽灵
        FileChange（payload 非 a//b//dev/null 一律拒收，与 files_from_unified_diff
        门控同口径）。"""
        diff = (
            "diff --git a/q.sql b/q.sql\n"
            "--- a/q.sql\n"
            "+++ b/q.sql\n"
            "@@ -1,2 +1,2 @@\n"
            "--- 旧注释行\n"
            "+++ 新注释行\n"
        )
        types = self._types(diff)
        assert types.get("q.sql", "").endswith("MODIFIED"), f"真段照旧：{types!r}"
        assert "新注释行" not in types and "旧注释行" not in types, \
            f"hunk 体假头不得产出幽灵条目：{types!r}"


class TestP5NoLoopWarning:
    """P5：无运行中的事件循环时回灌跳过必须 WARNING（R54-3 同族：链路死了要有人发现）。"""

    def test_no_running_loop_warns(self, caplog):
        from swarm.brain.nodes.dispatch import _feedback_to_knowledge
        diff = ("diff --git a/x.java b/x.java\n--- a/x.java\n+++ b/x.java\n"
                "@@ -1 +1 @@\n-a\n+b\n")
        with caplog.at_level(logging.WARNING):
            _feedback_to_knowledge("p1", SimpleNamespace(id="s1"),
                                   SimpleNamespace(diff=diff))
        assert "无运行中的事件循环" in caplog.text, \
            "同步上下文（无 loop）回灌排不上 = 知识库静默滞后，必须 fail-loud"


# ═══════════════════ executor_sync.py：E3 / E4 ═══════════════════

class TestE3PerEntryIsolation:
    """E3：upstream_artifacts 并入必须逐条隔离——一条坏条目（is_file 抛错）不得
    静默中断后续全部兄弟产物，且聚合 WARNING 可闻。"""

    def test_one_bad_entry_does_not_block_siblings(self, tmp_path, monkeypatch, caplog):
        from swarm.worker.executor_sync import _SandboxSyncMixin
        (tmp_path / "good.xml").write_text("x", encoding="utf-8")
        scope = SimpleNamespace(create_files=[], delete_files=[], readable=[],
                                writable=[], upstream_artifacts=["bad.xml", "good.xml"])
        fake = SimpleNamespace(
            effective_scope=scope, project_path=str(tmp_path),
            _build_manifest_files=lambda: [], _module_source_files=lambda: [])
        real_is_file = Path.is_file

        def _boom(self):
            if self.name == "bad.xml":
                raise OSError("EACCES（py<3.13 is_file 照抛族）")
            return real_is_file(self)

        monkeypatch.setattr(Path, "is_file", _boom)
        with caplog.at_level(logging.WARNING):
            files = _SandboxSyncMixin._scope_files(fake)
        assert "good.xml" in files, f"坏条目不得连坐兄弟，实际 {files!r}"
        assert "bad.xml" not in files
        assert "upstream_artifacts 并入上传清单" in caplog.text, "聚合 WARNING 必须在场"

    def test_root_none_warns_when_artifacts_pending(self, caplog):
        """★批4 R5 双复核 LOW-7/LOW-11★：project_path 异常（Path 构造抛错）时整段
        并入静默缺席=零信号——有上游产物待并入时必须 WARNING（空返回/缺席机读可辨）。"""
        from swarm.worker.executor_sync import _SandboxSyncMixin
        scope = SimpleNamespace(create_files=[], delete_files=[], readable=[],
                                writable=[], upstream_artifacts=["x.xml"])
        fake = SimpleNamespace(
            effective_scope=scope, project_path=None,  # Path(None) 抛 TypeError
            _build_manifest_files=lambda: [], _module_source_files=lambda: [])
        with caplog.at_level(logging.WARNING):
            _SandboxSyncMixin._scope_files(fake)
        assert "project_path 异常" in caplog.text, \
            f"根路径异常+有待并入产物时必须 WARNING，实际 {caplog.text!r}"


class TestE4EnumCapWarningLevel:
    """E4：H-exec1 枚举达上限必须真 level=warning（与兄弟枚举点同档，
    缺参=降级信号淹没在 INFO 与"恰好 cap 个"不可辨）。"""

    def test_cap_logs_at_warning_level(self):
        from swarm.worker.executor_sync import _SandboxSyncMixin, _WORKSPACE_LIST_CAP
        logs: list[tuple[str, str]] = []
        n = _WORKSPACE_LIST_CAP + 1
        result = SimpleNamespace(
            error=None, stdout="\n".join(f"f{i}.java" for i in range(n)))
        fake = SimpleNamespace(
            _sandbox_manager=SimpleNamespace(run_command=lambda *a, **k: result),
            _sandbox=object(),
            _enum_oversize_rels=[],
            _log=lambda msg, level="info": logs.append((level, msg)))
        # 真实方法绑定（SimpleNamespace 不会自动带 mixin 方法）——spy 锁的是调用点
        # level 实参，被调方法本体用真的（防"测 helper 没测调用点"假绿）。
        fake._split_enum_sections = (
            lambda out, ctx: _SandboxSyncMixin._split_enum_sections(fake, out, ctx))
        files = _SandboxSyncMixin._list_sandbox_files_under(fake, ["x"])
        assert len(files) == _WORKSPACE_LIST_CAP, f"截断语义不变，实际 {len(files)}"
        assert any(lv == "warning" and "H-exec1 目录内枚举达上限" in m
                   for lv, m in logs), f"达上限必须 warning 级留痕，实际 {logs!r}"


# ═══════════════════ funnel：security_scan / planning_core ═══════════════════

class TestSecurityScanFunnel:
    """R2 观测面收口：_parse_diff_new_path 走 strip_diff_path 单源（C-quoted 反转义）。"""

    def test_quoted_space_path(self):
        from swarm.worker.security_scan import _parse_diff_new_path
        assert _parse_diff_new_path('+++ "b/pa th/x.java"') == "pa th/x.java"

    def test_plain_and_devnull(self):
        from swarm.worker.security_scan import _parse_diff_new_path
        assert _parse_diff_new_path("+++ b/plain.java") == "plain.java"
        assert _parse_diff_new_path("+++ /dev/null") == ""


class TestPlanningCoreCurFileFunnel:
    """R2 观测面收口：_strip_ungrounded_lines 的 +++ 头解析走 strip_diff_path——
    C-quoted 带空格路径的文件分桶键必须是反转义后的真名（否则盘侧动作打错文件）。"""

    def test_quoted_header_bucket_key(self):
        from swarm.brain.nodes.planning_core import _strip_ungrounded_lines
        diff = (
            "diff --git \"a/pa th/pom.xml\" \"b/pa th/pom.xml\"\n"
            "--- \"a/pa th/pom.xml\"\n"
            "+++ \"b/pa th/pom.xml\"\n"
            "@@ -1 +1 @@\n"
            "-<version>4.8.3</version>\n"
            "+<version>9.9.9</version>\n"
        )
        _kept, dropped, disk = _strip_ungrounded_lines(diff, known={"4.8.3"})
        assert dropped == ["9.9.9"], f"臆造版本必须被剥：{dropped!r}"
        assert "pa th/pom.xml" in disk, \
            f"分桶键必须是反转义真名（旧手剥得 '\"pa th/pom.xml\"' 垃圾键）：{list(disk)!r}"


# ═══════════════════ planning_core.py：A10 簇 ═══════════════════

def _stub_st():
    return SimpleNamespace(
        id="st-1",
        scope=SimpleNamespace(create_files=["mod/A.java"], writable=[],
                              upstream_artifacts=[], readable=[]),
        description="建 mod", contract=None)


class _Resp:
    def __init__(self, content: str):
        self.content = content


@pytest.mark.asyncio
class TestA10StubGenDegradeKeys:
    """A10-M1 簇：桩生成的静默 None 出口必须机读可辨（degrade 键 + WARNING）。"""

    async def _run(self, llm, tmp_path, monkeypatch, keys):
        from swarm.brain.nodes.planning_core import _generate_compile_stub
        monkeypatch.setattr("swarm.infra.degrade.record_degrade", keys.append)
        with patch("swarm.brain.nodes._get_brain_llm", return_value=llm):
            return await _generate_compile_stub({}, _stub_st(), str(tmp_path))

    async def test_llm_raises_records_key_and_warns(self, tmp_path, monkeypatch, caplog):
        class _Boom:
            async def ainvoke(self, _m):
                raise RuntimeError("LLM 瞬时故障")

        keys: list[str] = []
        with caplog.at_level(logging.WARNING):
            out = await self._run(_Boom(), tmp_path, monkeypatch, keys)
        assert out is None
        assert "brain.stub_gen.llm_or_parse_failed" in keys, f"实际 {keys!r}"
        assert "LLM 生成/解析异常" in caplog.text

    async def test_empty_files_records_parse_failed(self, tmp_path, monkeypatch, caplog):
        class _Empty:
            async def ainvoke(self, _m):
                return _Resp(json.dumps({"files": {}}))

        keys: list[str] = []
        with caplog.at_level(logging.WARNING):
            out = await self._run(_Empty(), tmp_path, monkeypatch, keys)
        assert out is None
        assert "brain.stub_gen.parse_failed" in keys, f"实际 {keys!r}"
        assert "无 files 产出" in caplog.text

    async def test_all_skipped_records_file_skipped_and_empty_written(
            self, tmp_path, monkeypatch, caplog):
        class _Junk:
            async def ainvoke(self, _m):
                return _Resp(json.dumps({"files": {
                    "../evil/X.java": "x",       # 越权出足迹
                    "mod/A.java": "",            # 空内容
                }}))

        keys: list[str] = []
        with caplog.at_level(logging.WARNING):
            out = await self._run(_Junk(), tmp_path, monkeypatch, keys)
        assert out is None
        assert keys.count("brain.stub_gen.file_skipped") == 2, f"实际 {keys!r}"
        assert "brain.stub_gen.empty_written" in keys, f"实际 {keys!r}"
        assert "拒收桩文件" in caplog.text

    async def test_written_but_empty_diff_records_no_diff(
            self, tmp_path, monkeypatch, caplog):
        """桩内容恰等于 base ⇒ git diff 为空 ⇒ no_diff 键（旧静默 return None）。"""
        import subprocess as sp

        def git(*a):
            sp.run(["git", *a], cwd=tmp_path, check=True,
                   capture_output=True)

        git("init", "-q")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        (tmp_path / "mod").mkdir()
        content = "package mod;\npublic class A {}\n"
        (tmp_path / "mod" / "A.java").write_text(content, encoding="utf-8")
        git("add", "-A")
        git("commit", "-qm", "base")
        sha = sp.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                     capture_output=True, text=True).stdout.strip()

        class _Same:
            async def ainvoke(self, _m):
                return _Resp(json.dumps({"files": {"mod/A.java": content}}))

        keys: list[str] = []
        with caplog.at_level(logging.WARNING):
            out = await self._run(_Same(), tmp_path, monkeypatch, keys)
        assert out is None
        assert "brain.stub_gen.no_diff" in keys, f"实际 {keys!r}"
        assert "git diff 为空" in caplog.text


class TestA10ImportErrorMustRaise:
    """A10 簇 sibling：_changes_from_diff 的 lazy import 放 try 之外——
    ImportError=编程错误（符号被删/改名）必须显式抛出，绝不降级静默吞掉。"""

    def test_verified_sibling_files_raises(self, monkeypatch):
        import importlib
        # 经 importlib 取真子模块——swarm.brain.nodes 包的 __init__ 里有同名 node 函数
        # 遮蔽子模块属性（import ... as 会拿到函数而非模块）。
        disp = importlib.import_module("swarm.brain.nodes.dispatch")
        from swarm.brain.nodes.planning_core import _verified_sibling_files
        monkeypatch.delattr(disp, "_changes_from_diff")
        with pytest.raises(ImportError):
            _verified_sibling_files([], {}, set(), face="protect")

    def test_clean_stub_residue_raises(self, monkeypatch, tmp_path):
        import importlib
        disp = importlib.import_module("swarm.brain.nodes.dispatch")
        from swarm.brain.nodes.planning_core import _clean_stub_residue
        monkeypatch.delattr(disp, "_changes_from_diff")
        with pytest.raises(ImportError):
            _clean_stub_residue(
                str(tmp_path), _stub_st(), "st-1",
                stub_written=["mod/A.java"], protected_base=set(),
                subtasks=[], subtask_results={}, base_ref=None,
                cascade_revert_failed=[])


# ═══════════════════ nodes/__init__.py：D3 / D4 / D5 / D6 ═══════════════════

class TestD3SpawnOSErrorIsInfra:
    """D3：本地 L2 spawn 级失败（OSError=fork/exec/资源耗尽）是 infra ⇒ None 降级，
    绝不 return False 假红 replan（replan 修不了本机 fork 失败）。"""

    def test_oserror_returns_none_with_warning(self, monkeypatch, caplog):
        import subprocess as sp
        from swarm.brain.nodes import _run_l2_local_locked
        monkeypatch.setattr("swarm.project.diff_apply.apply_git_diff",
                            lambda *a, **k: {"ok": True})
        monkeypatch.setattr("swarm.brain.integration_review._reset_worktree_to_head",
                            lambda *a, **k: [])

        def _boom(*a, **k):
            raise OSError("fork: resource temporarily unavailable")

        monkeypatch.setattr(sp, "run", _boom)
        with caplog.at_level(logging.WARNING):
            out = _run_l2_local_locked("/tmp/proj", "diff", "pytest -q")
        assert out is None, f"spawn infra 失败必须 None 降级（False=假红连坐），实际 {out!r}"
        assert "进程启动失败" in caplog.text


class TestD4RetryGuidanceExactMatch:
    """D4：段归属判据=diff --git 头行精确集合匹配（_parse_git_header_paths 单源），
    旧 `f in seg[:200]` 子串判据必须绝迹——AST 接线锁。"""

    def test_exact_header_match_wired(self):
        tree = _ast("brain/nodes/__init__.py")
        # 找到含 _d4_files 的函数（D4 所在 merge 函数）
        host = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    isinstance(n, ast.Name) and n.id == "_d4_files" for n in ast.walk(node)):
                host = node
                break
        assert host is not None, "D4 宿主函数定位失败（接线锁前提失效）"
        imported = any(
            isinstance(n, ast.ImportFrom) and n.module == "swarm.brain.merge_engine"
            and any(a.name == "_parse_git_header_paths" for a in n.names)
            for n in ast.walk(host))
        called = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name) and n.func.id == "_parse_git_header_paths"
            for n in ast.walk(host))
        no_seg_slice = not any(
            isinstance(n, ast.Subscript)
            and isinstance(n.value, ast.Name) and n.value.id == "seg"
            for n in ast.walk(host))
        assert imported and called, "精确匹配解析器未接上（quoted 文件指导静默缺席）"
        assert no_seg_slice, "seg[:200] 子串判据仍在（App.java 命中 App.java.bak 头行=注错段）"


class TestD5RevisionClearsCoverageKeys:
    """D5：修订轮 _rev_out 必须重置 12 个终态覆盖账键——否则终态账面把【上一轮】
    的 smoke/l3/migration/adversarial 说明与细节当本轮覆盖上报——AST 接线锁。
    ★批4 R6 hunter F2★：键在场不够，值极性也要钉——None=未跑 / "" / {} / 0
    （False=「跑过且未跳过」是说谎，nodes/__init__.py D5 注释点名）。"""

    KEYS = {
        "runtime_smoke_skipped", "runtime_smoke_message", "runtime_smoke_details",
        "l3_skipped", "l3_skip_reason", "l3_message",
        "migration_verify_passed", "migration_verify_details",
        "adversarial_verify_passed", "adversarial_verify_message",
        "adversarial_verify_details", "adversarial_verify_round",
    }
    # 值极性对账表：三态/skipped 键→None，message/reason 键→""，details 键→{}，round→0
    WANT_NONE = {"runtime_smoke_skipped", "l3_skipped",
                 "migration_verify_passed", "adversarial_verify_passed"}
    WANT_EMPTY_STR = {"runtime_smoke_message", "l3_skip_reason", "l3_message",
                      "adversarial_verify_message"}
    WANT_EMPTY_DICT = {"runtime_smoke_details", "migration_verify_details",
                       "adversarial_verify_details"}
    WANT_ZERO = {"adversarial_verify_round"}

    def test_rev_out_resets_all(self):
        tree = _ast("brain/nodes/__init__.py")
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "_rev_out" for t in node.targets)
                    and isinstance(node.value, ast.Dict)):
                pairs = {k.value: v for k, v in zip(node.value.keys, node.value.values)
                         if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                missing = self.KEYS - pairs.keys()
                assert not missing, f"_rev_out 漏重置 {sorted(missing)}（跨轮账面污染族）"

                def _const(key):
                    v = pairs.get(key)
                    return v.value if isinstance(v, ast.Constant) else "<非字面量>"

                for k in self.WANT_NONE:
                    v = pairs.get(k)
                    assert isinstance(v, ast.Constant) and v.value is None, (
                        f"{k} 重置值必须是 None（False=「跑过且未跳过」说谎），"
                        f"实际 {ast.dump(v) if v is not None else None}")
                for k in self.WANT_EMPTY_STR:
                    assert _const(k) == "", f"{k} 重置值必须是空串，实际 {_const(k)!r}"
                for k in self.WANT_EMPTY_DICT:
                    v = pairs.get(k)
                    assert isinstance(v, ast.Dict) and not v.keys, \
                        f"{k} 重置值必须是空 dict，实际 {ast.dump(v) if v else None}"
                for k in self.WANT_ZERO:
                    assert _const(k) == 0, f"{k} 重置值必须是 0，实际 {_const(k)!r}"
                return
        raise AssertionError("_rev_out 赋值定位失败（接线锁前提失效）")


class TestD6SimplePathClearsRoundKeys:
    """D6：SIMPLE 快速路径早返必须清两张 round 级账（state.py:389-390 生命周期登记）——
    常规放行路 :4290-4293 清，SIMPLE 路缺失 ⇒ 结构账/软审签名跨轮粘滞——AST 接线锁。"""

    def test_simple_return_has_both_keys(self):
        tree = _ast("brain/nodes/__init__.py")
        simple_ifs = [n for n in ast.walk(tree)
                      if isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
                      and any(isinstance(c, ast.Attribute) and c.attr == "SIMPLE"
                              for c in n.test.comparators)]
        ret = None
        for si in simple_ifs:
            cand = next((n for n in si.body
                         if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict)
                         and {"plan_retry_count", "plan_batch_cache"} <= {
                             k.value for k in n.value.keys
                             if isinstance(k, ast.Constant) and isinstance(k.value, str)}),
                        None)
            if cand is not None:
                ret = cand
                break
        assert ret is not None, "SIMPLE 早返 dict 定位失败（接线锁前提失效）"
        pairs = {k.value: v for k, v in zip(ret.value.keys, ret.value.values)
                 if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        assert "plan_batch_cache" in pairs, "指纹键缺席=定位到错误的 Return（锁前提失效）"
        # ★批4 R6 hunter F2★：键在场不够，值极性也要钉——""=无签名（消费点
        # bool(_prev_soft_sig) 判空跳过软审复用），{}=无结构账；陈值粘滞=跨轮污染。
        _pv = pairs.get("plan_validation_prev_structural")
        assert isinstance(_pv, ast.Dict) and not _pv.keys, \
            "SIMPLE 早返漏清 plan_validation_prev_structural（G1 retry 绑定读陈值）"
        _ps = pairs.get("plan_soft_review_sig")
        assert isinstance(_ps, ast.Constant) and _ps.value == "", \
            "SIMPLE 早返漏清 plan_soft_review_sig（软审判重读陈签名）"


# ═══════════════════ runtime_smoke.py：O3 / R2 / R3 ═══════════════════

class TestO3NodeArmEvidence:
    """O3：node 臂 `name in top` 的项目内证据必须是【可导入形态】（package.json 或
    index.* 入口）——光有同名顶层目录不算（文档/脚本目录撞名第三方包 ⇒ 冤判 code_error）。"""

    def _verdict(self, paths, top=("server",)):
        from swarm.brain.nodes.runtime_smoke import _symbol_is_project_internal
        return _symbol_is_project_internal(
            "server", "node",
            {"paths": set(paths), "basenames": set(), "top": set(top)})

    def test_workspace_package_is_internal(self):
        assert self._verdict({"server/package.json"}) is True

    def test_index_entry_is_internal(self):
        assert self._verdict({"server/index.ts"}) is True

    def test_bare_dir_not_evidence(self):
        """裸目录（文档/脚本目录）⇒ 判项目外（第三方包缺失=依赖问题非代码错）。"""
        assert self._verdict(set()) is False


class TestR2ClassificationDocDrift:
    """R2：RuntimeSmokeResult docstring 的 classification 词表必须与全部产出点
    逐一对账（单源漂移锁：产出集从源码派生，docstring 只许同步不许漂移）。"""

    def test_docstring_matches_produced_set(self):
        src = (ROOT / "brain" / "nodes" / "runtime_smoke.py").read_text(encoding="utf-8")
        produced: dict[str, set[str]] = {}
        # 通道①：位置字面量形 RuntimeSmokeResult("passed", "started", ...)
        ch1 = re.findall(r'RuntimeSmokeResult\(\s*"(\w+)"\s*,\s*"(\w+)"', src)
        for status, cls in ch1:
            produced.setdefault(status, set()).add(cls)
        # 通道②：变量喂参形 RuntimeSmokeResult("passed", _cls_name, ...)——
        # classification 字面量在变量赋值点。两种赋值形都要提取：
        #   形A 元组解包 `_cls_name, _where = ("started_health_gated", ...)`
        #     （passed 族 health-gated 三个只以此形产出，漏形A=词表永远对不上）；
        #   形B 裸赋值 `_reason = "port_ambiguous"`（skipped 族 port_resolve 三个
        #     只以此形产出——★批4 R5 双复核 MED-1★：锁曾只认形A，产出集与 docstring
        #     同源同漏三值 ⇒ declared==produced 恒绿=锁在诞生当天 vacuous green）。
        # ★被捕 var 必须至少提出一个字面量★（缺席不可辨纪律应用于锁自身：
        # 新出现的第三种喂参形若两形都不命中，assert 当场红而不是静默贡献空集）。
        for status, var in re.findall(r'RuntimeSmokeResult\(\s*"(\w+)"\s*,\s*(_\w+)', src):
            hits = set(re.findall(rf'{var}\s*,\s*\w+\s*=\s*\("(\w+)"', src))
            hits |= set(re.findall(rf'{var}\s*=\s*"(\w+)"', src))
            assert hits, (f"变量喂参形 {var!r}（{status} 族）一个字面量都派生不出"
                          "=锁对新赋值形失明——补提取形再合入")
            produced.setdefault(status, set()).update(hits)
        # ★批4 R6 双复核 F1/LOW-4★：两通道只认【位置字面量】与【下划线变量】两形——
        # 第三种喂参形（关键字实参 classification="x"/无下划线变量/**kw）两通道都不
        # 命中 ⇒ 产出集静默不增，docstring 同漏时 declared==produced 恒绿（MED-1
        # 病灶对新形态重开）。总数对账：调用点总数必须 == 两通道命中数，任何新
        # 喂参形当场红（fail-closed）。
        total_calls = len(re.findall(r"RuntimeSmokeResult\(", src))
        ch2 = re.findall(r'RuntimeSmokeResult\(\s*"(\w+)"\s*,\s*(_\w+)', src)
        assert total_calls == len(ch1) + len(ch2), (
            f"RuntimeSmokeResult 调用点 {total_calls} 个，两通道只命中 "
            f"{len(ch1)}+{len(ch2)} 个——出现第三种喂参形，补提取通道再合入")
        assert produced, "产出集派生为空=锁前提失效"
        tree = _ast("brain/nodes/runtime_smoke.py")
        doc = ""
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "RuntimeSmokeResult":
                doc = ast.get_docstring(node) or ""
                break
        assert doc, "docstring 缺席=锁前提失效"
        declared: dict[str, set[str]] = {}
        for m in re.finditer(
                r"(passed|failed|skipped)：([^：]+?)(?=\n\s*(?:passed|failed|skipped)：|\Z)",
                doc, re.S):
            declared[m.group(1)] = {t.strip() for t in m.group(2).split("|") if t.strip()}
        assert declared.keys() == produced.keys() == {"passed", "failed", "skipped"}, \
            f"status 族漂移：产出 {sorted(produced)} vs 词表 {sorted(declared)}"
        for status in produced:
            assert declared[status] == produced[status], (
                f"{status} 族 classification 漂移：产出 {sorted(produced[status])} vs "
                f"词表 {sorted(declared[status])}——加新 classification 先补 docstring")


class TestR3ResolveBudgetWired:
    """R3：端口反解段预算必须计入 run_command timeout（probe_port None 时与脚本窗口
    同一 env 单源）——AST 接线锁：resolve_budget 赋值条件 + timeout 表达式含它。"""

    def test_resolve_budget_in_timeout(self):
        fn = _func(_ast("brain/nodes/runtime_smoke.py"), "run_runtime_smoke")
        assign_ok = False
        for n in ast.walk(fn):
            if (isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "resolve_budget" for t in n.targets)
                    and isinstance(n.value, ast.IfExp)):
                body = n.value.body
                if (isinstance(body, ast.Call) and isinstance(body.func, ast.Name)
                        and body.func.id == "_resolve_positive_int_env"
                        and any(isinstance(a, ast.Name) and a.id == "PORT_RESOLVE_WINDOW_ENV"
                                for a in body.args)):
                    assign_ok = True
        timeout_uses = any(
            isinstance(n, ast.keyword) and n.arg == "timeout"
            and any(isinstance(m, ast.Name) and m.id == "resolve_budget"
                    for m in ast.walk(n.value))
            for n in ast.walk(fn))
        assert assign_ok, "resolve_budget 未按 probe_port 条件从 env 单源派生"
        assert timeout_uses, "timeout= 未计入 resolve_budget（反解段吃缓冲=误归因 not_executed）"


# ═══════════════════ l1_pipeline.py：L-2 / R4-LOW1 ═══════════════════

class TestL2ConfigurableWorkdir:
    """L-2：_norm_src_path 的 workdir 前缀必须读配置（自定义 sandbox_remote_workdir
    的沙箱产出形态不写死 /workspace/ 也能剥）。"""

    def test_custom_workdir_stripped(self, monkeypatch):
        import swarm.config.settings as settings
        from swarm.worker.l1_pipeline import _norm_src_path
        monkeypatch.setattr(
            settings, "get_config",
            lambda: SimpleNamespace(
                sandbox=SimpleNamespace(sandbox_remote_workdir="/sandbox2/wd")))
        assert _norm_src_path("/sandbox2/wd/mod/src/Y.java") == "mod/src/Y.java"

    def test_default_workdir_unchanged(self, monkeypatch):
        """方向锁：默认 /workspace/ 形态照旧（r32b3 同族锁互证）。
        ★批4 R5 hunter LOW-10★：钉配置面——锁不得读真实 .env（哪天本地设了
        SWARM_SANDBOX_REMOTE_WORKDIR 就假红，SWARM_PLAN_INJECT_ENABLE flake 同族）。"""
        from swarm.worker import l1_pipeline as l1
        monkeypatch.setattr(l1, "_sandbox_workdir", lambda: "/workspace")
        assert l1._norm_src_path("/workspace/ruoyi/src/X.java") == "ruoyi/src/X.java"

    def test_root_only_workdir_falls_back(self, monkeypatch):
        """★批4 R5 hunter LOW-10★：workdir="/" 病态配置 ⇒ rstrip 后空串 ⇒
        startswith(""+"/") 恒真=全量误剥。空串视同非法回退默认。"""
        import swarm.config.settings as settings
        from swarm.worker.l1_pipeline import _sandbox_workdir
        monkeypatch.setattr(
            settings, "get_config",
            lambda: SimpleNamespace(
                sandbox=SimpleNamespace(sandbox_remote_workdir="/")))
        assert _sandbox_workdir() == "/workspace", \
            f"\"/\" 必须回退默认（空串前缀=一切绝对路径全误剥），实际 {_sandbox_workdir()!r}"

    def test_norm_add_uses_config_workdir(self, monkeypatch):
        """★批4 R5 hunter LOW-5★：_build_error_is_reactor_missing_module 的 _norm_add
        是同文件 sibling——旧写死 "/workspace/"（并夹带凭印象的 "/repo/" 兜底），
        自定义 workdir 沙箱上绝对形态模块 token 剥不动 ⇒ 垃圾模块名进
        blocked_on_modules ⇒ brain 定点重排打空。"""
        import swarm.config.settings as settings
        from swarm.worker.l1_pipeline import _build_error_is_reactor_missing_module
        monkeypatch.setattr(
            settings, "get_config",
            lambda: SimpleNamespace(
                sandbox=SimpleNamespace(sandbox_remote_workdir="/sandbox2/wd")))
        out = _build_error_is_reactor_missing_module(
            "Child module /sandbox2/wd/mod-a/pom.xml of /sandbox2/wd/pom.xml does not exist")
        assert out == {"mod-a"}, f"自定义 workdir 绝对形态必须剥前缀，实际 {out!r}"


class TestR4RelpathFirst:
    """R4-close hunter LOW-1：本地绝对路径中段含 /workspace/ 段（~/workspace/proj/…）
    ⇒ relpath 优先归一（旧两分支全走错：relpath 支被条件挡、_norm_src_path 支误剥
    首个 /workspace/ 段 ⇒ changed 记 proj/src/…=错路径）。"""

    def _repair(self, monkeypatch, project_path: str, err_path: str):
        from swarm.worker import l1_pipeline as l1
        monkeypatch.setattr(
            l1, "_run_check_split",
            lambda cmd, cwd, timeout=0: (0, "     12 import jakarta.servlet.http.X\n", ""))
        monkeypatch.setattr(
            l1, "_run_l1_command",
            lambda cmd, cwd, timeout=0: (0, ""))
        build_output = f"{err_path}:[5,12] package javax.servlet does not exist\n"
        return l1._attempt_import_repair(project_path, build_output, 20)

    def test_mid_workspace_segment_relpathed(self, monkeypatch):
        n, files = self._repair(
            monkeypatch, "/tmp/home/workspace/proj", "/tmp/home/workspace/proj/src/D.java")
        assert n == 1 and files == ["src/D.java"], \
            f"中段 /workspace/ 不得被当沙箱前缀误剥，实际 {files!r}"

    def test_sandbox_workspace_form_still_stripped(self, monkeypatch):
        """方向锁：真沙箱形态照旧剥前缀（与 r32b3 既有锁互证）。
        ★批4 R5 hunter LOW-10★：钉配置面，不读真实 .env。"""
        from swarm.worker import l1_pipeline as l1
        monkeypatch.setattr(l1, "_sandbox_workdir", lambda: "/workspace")
        n, files = self._repair(
            monkeypatch, "/tmp/proj", "/workspace/ruoyi-system/src/X.java")
        assert n == 1 and files == ["ruoyi-system/src/X.java"], f"实际 {files!r}"
