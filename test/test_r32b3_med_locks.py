#!/usr/bin/env python3
"""★32 号文批3 突变锁★ MED 待核 5 条定级后 4 条治法的锁（E5 证伪不硬治）。

- D3-下半：本地 L2 git apply 失败=infra 返 None（原返 False=测试失败 ⇒ 假红连坐）。
- D4-上半：plan() 两外科调用点接 Exception（原只接 ImportError，symbol_surgery
  全文件零 except ⇒ 内部异常炸 plan 节点 FAILED@PLAN）。
- S4：_L2_CMD_RE 中文尾巴截断 + 补 go/cargo/gradle 族 + STACK_SPEC 漂移锁。
- S5：attribute_l2_failure 边界感知 + basename 非唯一不参与（子串/歧义误归因）。
"""
from __future__ import annotations

import importlib.util
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.types import (  # noqa: E402
    Complexity,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
)


def _st(sid, create=None, writable=None):
    return SubTask(id=sid, description=f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=writable or [], create_files=create or []))


# ──────────────────────────── D3-下半：本地 L2 apply 失败=infra ────────────────────────────

class TestD3LocalL2ApplyInfra:
    def _repo(self) -> str:
        d = tempfile.mkdtemp()
        subprocess.run(["git", "-C", d, "init", "-q"], check=True)
        subprocess.run(["git", "-C", d, "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", d, "config", "user.name", "t"], check=True)
        Path(d, "foo.txt").write_text("original\n")
        subprocess.run(["git", "-C", d, "add", "."], check=True)
        subprocess.run(["git", "-C", d, "commit", "-qm", "init"], check=True)
        return d

    def test_apply_failure_returns_none_not_false(self):
        """apply 失败（上下文对不上=基线漂移/树状态）→ None（infra 降级），
        绝不返 False 被判成测试失败（⇒ L2 假红全量 replan 连坐）。"""
        from swarm.brain.nodes import _run_l2_local
        d = self._repo()
        bad_diff = ("--- a/foo.txt\n+++ b/foo.txt\n@@ -1 +1 @@\n"
                    "-这行上下文在树里不存在\n+modified\n")
        out = _run_l2_local(d, bad_diff, "true", timeout=30)
        assert out is None, f"apply 失败=infra 应返 None，实际 {out!r}"

    def test_apply_ok_test_fail_returns_false(self):
        """方向锁：apply 成功+测试真失败仍返 False——None 只留给 infra，
        防治法过头把真测试失败也洗成降级（fail-open 反方向）。"""
        from swarm.brain.nodes import _run_l2_local
        d = self._repo()
        diff = "--- a/foo.txt\n+++ b/foo.txt\n@@ -1 +1 @@\n-original\n+modified\n"
        assert _run_l2_local(d, diff, "false", timeout=30) is False

    def test_apply_ok_test_pass_returns_true(self):
        from swarm.brain.nodes import _run_l2_local
        d = self._repo()
        diff = "--- a/foo.txt\n+++ b/foo.txt\n@@ -1 +1 @@\n-original\n+modified\n"
        assert _run_l2_local(d, diff, "true", timeout=30) is True

    def test_tool_missing_returns_none_not_false(self):
        """★R1 hunter MED-2 本地臂★：测试驱动不存在（rc=127 command not found）=infra
        → None。S4 补 go/cargo/gradle 族后 criteria 提到而本机无该工具链即撞此形——
        判 False=L2 假红 replan 空转（replan 修不了磁盘上的工具链缺失）。"""
        from swarm.brain.nodes import _run_l2_local
        d = self._repo()
        diff = "--- a/foo.txt\n+++ b/foo.txt\n@@ -1 +1 @@\n-original\n+modified\n"
        out = _run_l2_local(d, diff, "definitely-not-a-real-tool-xyz test", timeout=30)
        assert out is None, f"驱动缺失=infra 应返 None，实际 {out!r}"


# ──────────────────────────── D4-上半：外科调用点接 Exception ────────────────────────────

class _FakeLLM:
    def __init__(self, content):
        self._content = content

    async def ainvoke(self, messages):
        return type("R", (), {"content": self._content})()


_PLAN_JSON = ('{"subtasks":[{"id":"st-9","description":"x",'
              '"scope":{"writable":["a.txt"],"readable":[]},"covers":[]}],'
              '"parallel_groups":[["st-9"]]}')

_SYM_STATE_EXTRA = {
    "plan_validation_feedback": "契约符号无 owner 子任务承接 5/8: IAService",
    "plan_validation_issues": ["契约符号无 owner 子任务承接 5/8: IAService"],
}
_FP_STATE_EXTRA = {
    "plan_validation_feedback": "file_plan 文件无 owner 子任务承接: mod-c/C.java",
    "plan_validation_issues": ["file_plan 文件无 owner 子任务承接: mod-c/C.java"],
}


def _base_plan_state(extra):
    prior = TaskPlan(subtasks=[_st("st-1", create=["mod-a/src/A.java"]),
                               _st("st-2", create=["mod-b/src/B.java"])],
                     parallel_groups=[["st-1", "st-2"]])
    st = {
        "task_description": "build feature",
        "complexity": Complexity.MEDIUM,
        "requirement_items": [],
        "plan": prior,
        "shared_contract": {},
        "replan_feedback": "",
        "plan_batch_failed_modules": [],
    }
    st.update(extra)
    return st


class TestD4SurgicalExceptionDegrades:
    async def test_symbol_surgery_exception_falls_back(self, monkeypatch):
        """maybe_symbol_repair 内部抛【非 ImportError】（os.walk/解析面异常形态）→
        plan() 不炸（FAILED@PLAN），回退常规 LLM 重拆路径拿到 plan。"""
        import swarm.brain.nodes as nodes
        import swarm.brain.symbol_surgery as ss

        def _boom(*a, **k):
            raise ValueError("walk blew up")

        monkeypatch.setattr(ss, "maybe_symbol_repair", _boom)
        monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _FakeLLM(_PLAN_JSON))
        monkeypatch.setattr(nodes, "_get_project_path", lambda pid: "/tmp")
        out = await nodes.plan(_base_plan_state(_SYM_STATE_EXTRA))
        assert out.get("plan") is not None, "外科异常必须回退常规重拆，绝不炸 plan 节点"

    async def test_fileplan_surgery_exception_falls_back(self, monkeypatch):
        """缺件外科同型（改一处=半落地，两调用点各一条锁）。"""
        import swarm.brain.nodes as nodes
        import swarm.brain.symbol_surgery as ss

        def _boom(*a, **k):
            raise OSError("disk blew up")

        monkeypatch.setattr(ss, "maybe_symbol_repair", lambda *a, **k: None)
        monkeypatch.setattr(ss, "maybe_file_plan_repair", _boom)
        monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _FakeLLM(_PLAN_JSON))
        monkeypatch.setattr(nodes, "_get_project_path", lambda pid: "/tmp")
        out = await nodes.plan(_base_plan_state(_FP_STATE_EXTRA))
        assert out.get("plan") is not None, "缺件外科异常必须回退常规重拆，绝不炸 plan 节点"


# ──────────────────────────── S4：L2 命令抽取 ────────────────────────────

class TestS4L2CommandExtraction:
    """语料与被排除项同现（防 vacuous）；中文/英文/粘连/命令链全形态。"""

    CASES = [
        ("运行 mvn test 验证所有功能正常", "mvn test"),
        ("用 go test ./... 跑全部测试", "go test ./..."),
        ("cargo test 必须通过", "cargo test"),
        ("./gradlew test 全绿", "./gradlew test"),
        ("gradle test 通过", "gradle test"),
        ("pytest -q 全部通过；且不许跳过", "pytest -q"),
        ("npm test -- --watchAll=false 通过", "npm test -- --watchAll=false"),
        ("mvn test -Dtest=UserServiceTest 验证用户服务", "mvn test -Dtest=UserServiceTest"),
        ("go test ./...", "go test ./..."),
        ("cargo test -q", "cargo test -q"),
        ("npm test --silent", "npm test --silent"),
        ("python -m pytest -q --maxfail=1", "python -m pytest -q --maxfail=1"),
        ("mvn testng 不行", ""),          # 粘连边界：testng≠test
        ("确保所有接口可用", ""),           # 无命令→空串（不猜）
        ("pytest -q; rm -rf x", "pytest -q"),   # 命令链护栏 ;
        ("mvn test | tee log", "mvn test"),     # 命令链护栏 |
        # ── R1 reviewer F-1：引号段（中文过滤参数合法，截断出破引号命令=假红）──
        ("跑 pytest -k '登录用例'", "pytest -k '登录用例'"),
        ("go test -run 'Test登录' 必须通过", "go test -run 'Test登录'"),
        ('跑 pytest -k "smoke套件" 验证', 'pytest -k "smoke套件"'),
        ("pytest -k 'login' 通过", "pytest -k 'login'"),
        ("mvn test -DskipTests='否' 通过", "mvn test -DskipTests='否'"),  # 值级引用（token 内）
    ]

    def test_corpus(self):
        from swarm.brain.nodes.shared import _l2_test_command_from_criteria as f
        bad = [(c, f([c]), w) for c, w in self.CASES if f([c]) != w]
        assert not bad, "\n".join(f"{c!r} => {g!r} (want {w!r})" for c, g, w in bad)

    def test_stack_spec_test_cmds_all_recognized(self):
        """漂移锁：STACK_SPEC 每个【非空】test_cmd 必须被识别（枚举权威来源钉）。
        语料派生自 STACK_SPEC 而被突变对象在 shared.py——不随突变收缩。"""
        from swarm.brain.nodes.shared import _l2_test_command_from_criteria as f
        from swarm.stacks.spec import STACK_SPEC
        non_empty = {k: v.test_cmd for k, v in STACK_SPEC.items() if v.test_cmd}
        assert len(non_empty) >= 3, "STACK_SPEC 非空 test_cmd 数异常（前提闸）"
        miss = {k: cmd for k, cmd in non_empty.items() if not f([cmd])}
        assert not miss, f"STACK_SPEC test_cmd 不被 _L2_CMD_RE 识别: {miss}"

    def test_chinese_tail_not_in_command(self):
        """专打中文尾巴：返回值必须纯 ASCII（被排除项与被肯定项同现）。"""
        from swarm.brain.nodes.shared import _l2_test_command_from_criteria as f
        got = f(["跑 mvn test -DskipTests=false 验证全部通过"])
        assert got == "mvn test -DskipTests=false"
        assert got.isascii()


# ──────────────────────────── S5：归因边界 ────────────────────────────

class TestS5AttributionBoundaries:
    def _attribute(self, plan, blob):
        from swarm.brain.nodes.shared import attribute_l2_failure
        results = {st.id: object() for st in plan.subtasks}
        return attribute_l2_failure(
            plan, {"integration_review": {"compile_output": blob}}, results)

    def test_substring_basename_not_attributed(self):
        """Config.java ⊂ SecurityConfig.java（标识符延续）⇒ 无辜写者 st-2 不得被归因。"""
        plan = TaskPlan(subtasks=[
            _st("st-1", create=["mod-a/src/SecurityConfig.java"]),
            _st("st-2", create=["mod-b/src/Config.java"]),
            _st("st-3", create=["mod-c/src/Other.java"]),
        ])
        blob = "mod-a/src/SecurityConfig.java:12: error: cannot find symbol"
        assert self._attribute(plan, blob) == ["st-1"]

    def test_path_segment_extension_rejected(self):
        """main/App.java ⊂ mymain/App.java（段内延续）⇒ 只归 st-2。"""
        plan = TaskPlan(subtasks=[
            _st("st-1", create=["main/App.java"]),
            _st("st-2", create=["mymain/App.java"]),
            _st("st-3", create=["src/Other.java"]),
        ])
        blob = "/workspace/mymain/App.java:3: error: bad reference"
        assert self._attribute(plan, blob) == ["st-2"]

    def test_basename_unique_match_works(self):
        """basename 在写者间唯一 + 边界命中 ⇒ 正常归因（防过紧回退全 None）。"""
        plan = TaskPlan(subtasks=[
            _st("st-1", create=["mod-a/src/A.java"]),
            _st("st-2", create=["mod-b/src/B.java"]),
            _st("st-3", create=["mod-c/src/Other.java"]),
        ])
        blob = "(Other.java:42)\njava.lang.NullPointerException"
        assert self._attribute(plan, blob) == ["st-3"]

    def test_basename_ambiguous_not_used(self):
        """basename 非唯一（多模块 pom.xml 同族）⇒ 裸 basename 证据不参与；
        全路径证据不受限。歧义时归因不出=None 回退全量 replan（fail-closed）。"""
        plan = TaskPlan(subtasks=[
            _st("st-1", create=["mod-a/pom.xml"]),
            _st("st-2", create=["mod-b/pom.xml"]),
            _st("st-3", create=["mod-c/src/X.java"]),
        ])
        bare = "[ERROR] pom.xml 解析失败：XML 格式非法"
        assert self._attribute(plan, bare) is None, \
            "歧义 basename 证据不得归因（误归因不如归因不出）"
        full = "[ERROR] mod-b/pom.xml:15: error: bad XML"
        assert self._attribute(plan, full) == ["st-2"]

    def test_strict_subset_guard_intact(self):
        """既有护栏不变：全部子任务被归因 ⇒ None（退全量 replan，不连坐）。"""
        plan = TaskPlan(subtasks=[
            _st("st-1", create=["mod-a/src/A.java"]),
            _st("st-2", create=["mod-b/src/B.java"]),
        ])
        blob = ("mod-a/src/A.java:1: error\n"
                "mod-b/src/B.java:2: error\n")
        assert self._attribute(plan, blob) is None


# ──────────── R1 整改：hunter MED-1/MED-2（沙箱臂）+ reviewer F-4（import 归一） ────────────

class TestR1SandboxApplyAndToolMissing:
    """hunter MED-1/MED-2 沙箱臂：apply 失败=infra → None（D3 同型 sibling，首选路径）；
    测试驱动 command not found=infra → None（编译臂 R34-7 判据移植）。"""

    def _run(self, apply_rc, test_rc=0, test_out="", test_cmd="go test ./..."):
        from swarm.brain.nodes import _run_l2_in_sandbox

        class _R:
            def __init__(self, stdout="", stderr=""):
                self.stdout, self.stderr, self.error = stdout, stderr, None

        class _S:
            sandbox_id = "s"

        class _M:
            def create(self, **kw):
                return _S()

            def sync_project_to_sandbox(self, *a, **kw):
                pass

            def run_command(self, sandbox, cmd, timeout=None):
                if "git apply" in cmd:
                    return _R(stdout=f"patch failed __APPLY_RC__{apply_rc}")
                return _R(stdout=f"{test_out} __RC__{test_rc}")

            def kill(self, *a, **kw):
                pass

        with patch("swarm.worker.sandbox.get_sandbox_manager", return_value=_M()), \
             patch("swarm.worker.sandbox.write_file_to_sandbox"):
            return _run_l2_in_sandbox("/tmp/proj", "diff\n", test_cmd, project_id="p1")

    def test_apply_failure_returns_none_not_false(self):
        """MED-1：沙箱 apply rc≠0（基线漂移/兄弟交付改树=infra）→ None 走降级。
        原 return False=测试失败 ⇒ 首选验证路径上 D3 假红连坐照旧（半落地）。"""
        assert self._run(apply_rc=1) is None

    def test_apply_ok_test_fail_still_false(self):
        """方向锁：apply 成功+真测试失败仍 False——None 只给 infra，防治法过头。"""
        assert self._run(apply_rc=0, test_rc=1, test_out="FAIL TestX") is False

    def test_tool_missing_returns_none(self):
        """MED-2 沙箱臂：rc=127 + 驱动 command not found → None（infra）。"""
        assert self._run(apply_rc=0, test_rc=127,
                         test_out="sh: go: command not found") is None

    def test_plugin_internal_127_still_false(self):
        """方向锁（R34-7 收紧判据本意）：127 但【非驱动本身】缺失（插件内部 shell 调
        缺失二进制）=真测试失败，绝不能被宽 127 判据洗成 infra 假降级。"""
        assert self._run(apply_rc=0, test_rc=127,
                         test_out="mockgen: command not found") is False

    def test_substring_tool_name_not_missing(self):
        """★R2-close hunter H-1 反向锁★：`go: not found` ⊂ `logo: not found`——
        资源缺失类【真测试失败】输出含 assets/logo: not found，无左边界时会被洗成
        infra ⇒ LLM 兜底放行（fail-open）。判据必须带 (?<![\\w.-]) 左边界。"""
        assert self._run(apply_rc=0, test_rc=1,
                         test_out="FAIL TestAssets: assets/logo: not found") is False

    def test_dash_wording_not_found(self):
        """★R2-close hunter L-1★：dash 措辞 `sh: 1: go: not found`（无 command）同判
        infra——该臂此前零锁覆盖=死突变面。"""
        assert self._run(apply_rc=0, test_rc=127,
                         test_out="sh: 1: go: not found") is None

    def test_wrapper_no_such_file_returns_none(self):
        """★R2-close hunter H-2★：含斜杠驱动（./gradlew wrapper）缺失时 bash 报
        `No such file or directory` 而非 command not found——S4 新收 ./gradlew 族的
        缺失形态必须同判 infra，否则 greenfield Gradle 项目 L2 假红空转。"""
        assert self._run(apply_rc=0, test_rc=127,
                         test_out="bash: ./gradlew: No such file or directory",
                         test_cmd="./gradlew test") is None

    def test_python_alias_python3_wording(self):
        """★R3-close hunter F-R3-3★：沙箱 run_command 把裸 python 幂等改写成 python3
        （worker/sandbox.py:1096 `_normalize_python_cmd`，镜像 PATH 只有 python3）——
        缺 python3 时 shell 报 `python3: not found`，与原始命令首 token `python`
        不匹配会漏判 ⇒ False=假红。判据必须认 python→python3 别名。"""
        assert self._run(apply_rc=0, test_rc=127,
                         test_out="sh: 1: python3: not found",
                         test_cmd="python -m pytest") is None

    def test_python_self_wording_still_none(self):
        """方向锁（F-R3-3 防过头）：python 自身措辞 `python: command not found`
        照旧判 infra——别名扩充不得破坏原首 token 命中。"""
        assert self._run(apply_rc=0, test_rc=127,
                         test_out="bash: python: command not found",
                         test_cmd="python -m pytest") is None


class TestR1ImportRepairNormalizes:
    """reviewer F-4：_attempt_import_repair 是 parse_missing_packages 四消费点中唯一
    未归一的——changed 必须收 _norm_src_path 后的模块相对形态（docstring TD2606-C9
    契约「改动文件相对路径列表」），否则 /workspace/ 绝对形态漏过下游全部比对。"""

    def _repair(self, monkeypatch, err_path):
        from swarm.worker import l1_pipeline as l1
        seen: list[str] = []
        monkeypatch.setattr(
            l1, "_run_check_split",
            lambda cmd, cwd, timeout=0: (0, "     12 import jakarta.servlet.http.X\n", ""))
        monkeypatch.setattr(
            l1, "_run_l1_command",
            lambda cmd, cwd, timeout=0: (seen.append(cmd), (0, ""))[1])
        build_output = f"{err_path}:[5,12] package javax.servlet does not exist\n"
        return l1._attempt_import_repair("/tmp/proj", build_output, 20), seen

    def test_absolute_workspace_path_normalized(self, monkeypatch):
        # ★批4 R6 双复核 LOW-1/F3★：钉配置面（R5 钉了三处漏了本处——夹具走
        # startswith(_sandbox_workdir()+"/") 分支读真实配置，.env 设
        # SWARM_SANDBOX_REMOTE_WORKDIR 即假红，SWARM_PLAN_INJECT_ENABLE flake 同族）。
        from swarm.worker import l1_pipeline as l1
        monkeypatch.setattr(l1, "_sandbox_workdir", lambda: "/workspace")
        (n, files), seen = self._repair(monkeypatch, "/workspace/ruoyi-system/src/X.java")
        assert n == 1
        assert files == ["ruoyi-system/src/X.java"], \
            f"changed 必须收归一相对形态，实际 {files!r}"
        # R2-close reviewer MED-1：sed 目标必须吃原始 f（cwd 无关天然正确），
        # 吃 rel 会把本地绝对/误剥形态打成静默失效或错文件。
        assert seen and "/workspace/ruoyi-system/src/X.java" in seen[0], \
            f"sed 命令必须用原始 f: {seen!r}"

    def test_relative_path_unchanged(self, monkeypatch):
        """方向锁：本就相对的形态归一后不变（防治法误改正常路径）。"""
        (n, files), _ = self._repair(monkeypatch, "ruoyi-system/src/X.java")
        assert n == 1
        assert files == ["ruoyi-system/src/X.java"]

    def test_local_absolute_path_relpathed(self, monkeypatch):
        """本地绝对形态（Maven 绝对 basedir 输出，test_jvm_namespace_fix e2e 同形）：
        changed 按 project_path 求相对（_norm_src_path 只认 /workspace/ 前缀，
        本地绝对仅剥前导 "/" 的不可解析形态绝不进 changed）。"""
        (n, files), seen = self._repair(monkeypatch, "/tmp/proj/mod/src/B.java")
        assert n == 1
        assert files == ["mod/src/B.java"], f"本地绝对必须 relpath 化，实际 {files!r}"
        assert seen and "/tmp/proj/mod/src/B.java" in seen[0]

    def test_out_of_project_absolute_not_registered(self, monkeypatch, caplog):
        """★R3-close hunter F-R3-1★：项目外绝对形态（自定义 sandbox_remote_workdir
        注册配置项 / macOS /tmp→/private/tmp 符号链接分叉 / 项目外引用）relpath 产
        ../ 毒串——下游 git diff targets 无守卫会 rc=128 "outside repository" 连坐
        【整个 diff】回退 difflib（E7① executor_sync:493-497 注释点名的那类事故）。
        文件照修（sed 吃原始 f）但 changed 绝不收 ../ 形态（缺席=fail-closed）
        +WARNING 机读可辨。"""
        import logging
        # ★批4 R5 hunter LOW-10★：钉配置面——夹具 "/sandbox2/..." 在本地 .env 设了
        # SWARM_SANDBOX_REMOTE_WORKDIR=/sandbox2 时会变成"合法沙箱形态"被剥前缀 ⇒
        # 本锁假红（SWARM_PLAN_INJECT_ENABLE flake 同族：探针锁不得读真实配置）。
        from swarm.worker import l1_pipeline as l1
        monkeypatch.setattr(l1, "_sandbox_workdir", lambda: "/workspace")
        with caplog.at_level(logging.WARNING):
            (n, files), seen = self._repair(monkeypatch, "/sandbox2/mod/src/C.java")
        assert seen and "/sandbox2/mod/src/C.java" in seen[0], \
            f"项目外形态 sed 仍须就地修（吃原始 f）: {seen!r}"
        assert n == 0 and files == [], \
            f"../ 毒串绝不进 changed（git diff targets 连坐=E7①），实际 {files!r}"
        assert "项目外绝对路径" in caplog.text, "缺席必须机读可辨（WARNING）"

    def test_relative_mid_workspace_segment_not_stripped(self, monkeypatch):
        """★R3-close hunter F-R3-2★：相对路径中段含 /workspace/ 段（vendor 下同名
        目录）不得被 ^.*?/workspace/ 误剥——sed 侧已吃原始 f，changed 侧也必须保
        全路径，否则修复登记指向错根级路径=静默蒸发（sibling_dep_repair 同族）。"""
        (n, files), seen = self._repair(monkeypatch, "vendor/workspace/src/D.java")
        assert n == 1
        assert files == ["vendor/workspace/src/D.java"], \
            f"相对形中段 /workspace/ 段不得误剥，实际 {files!r}"
        assert seen and "vendor/workspace/src/D.java" in seen[0]
