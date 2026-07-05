"""round26 Step2 行为测试：_SandboxSyncMixin 外置后的 MRO 接线 + 此前零直接覆盖的
两个 SYNC 方法（_module_source_files / _normalize_jvm_namespace）。

pr-test-analyzer 复核指出：PROMPT mixin 有 MRO 守卫测试，SYNC mixin（更大更复杂）没有；
且 _module_source_files（整模块源码树同步，编译型语言 cannot-find-symbol 的直接防线）与
_normalize_jvm_namespace（jakarta/javax 确定性归一）此前零直接测试。这里补真·行为测试
（断言产出/契约，不 inspect.getsource）。纯方法，不触真沙箱/网络。
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.worker.executor import WorkerExecutor  # noqa: E402
from swarm.worker.executor_sync import _SandboxSyncMixin  # noqa: E402
from swarm.types import FileScope, SubTask, TaskHarness  # noqa: E402


def test_sync_mixin_wired_into_mro():
    """★critical：SYNC 混入类真的在 WorkerExecutor 的 MRO 上（外置未断链）。

    若 class 声明写错（漏 _SandboxSyncMixin 或顺序错），16 个 SYNC 方法会从实例上消失
    并以难懂的 AttributeError 暴露——本测试直接钉住接线。"""
    assert _SandboxSyncMixin in WorkerExecutor.__mro__
    assert issubclass(WorkerExecutor, _SandboxSyncMixin)
    # MRO 顺序：WorkerExecutor 本体优先，两个 mixin 随后。
    mro = [c.__name__ for c in WorkerExecutor.__mro__]
    assert mro[0] == "WorkerExecutor"
    assert "_SandboxSyncMixin" in mro and "_PromptBuildingMixin" in mro


def test_module_source_files_collects_module_tree_excludes_build_output(tmp_path):
    """_module_source_files：JVM 构建命令下，收改动文件所在模块 src/ 全部源码，
    排除 target/build 产物。这是"整模块编译不缺同级类"的直接防线。"""
    mod = tmp_path / "mod"
    (mod / "src" / "main" / "java" / "com").mkdir(parents=True)
    (mod / "pom.xml").write_text("<project/>")
    (mod / "src" / "main" / "java" / "com" / "App.java").write_text("class App {}")
    (mod / "src" / "main" / "java" / "com" / "Util.java").write_text("class Util {}")
    # 构建产物必须被排除
    (mod / "target" / "classes").mkdir(parents=True)
    (mod / "target" / "classes" / "App.class.java").write_text("// stray")

    st = SubTask(
        id="st-1", description="改 App",
        scope=FileScope(writable=["mod/src/main/java/com/App.java"]),
        harness=TaskHarness(language="java", build_command="mvn -pl mod -am compile"),
    )
    ex = WorkerExecutor(subtask=st, project_path=str(tmp_path))
    out = ex._module_source_files()
    assert "mod/src/main/java/com/App.java" in out
    assert "mod/src/main/java/com/Util.java" in out  # 同模块同级类必须带上
    assert not any("target" in p for p in out)  # 产物排除


def test_module_source_files_noop_without_build_command(tmp_path):
    """非编译型（无 build_command）→ 返回空，保持精准同步（不整模块上传）。"""
    (tmp_path / "a.java").write_text("class A {}")
    st = SubTask(id="st-2", description="x", scope=FileScope(writable=["a.java"]),
                 harness=TaskHarness(language="java"))  # 无 build_command
    ex = WorkerExecutor(subtask=st, project_path=str(tmp_path))
    assert ex._module_source_files() == []


def test_normalize_jvm_namespace_rewrites_javax_to_jakarta(tmp_path):
    """_normalize_jvm_namespace（local 模式）：project_stack 判定 jakarta 时，把 pull-back
    的 .java 里写错的 javax.servlet 确定性改成 jakarta.servlet，回写本地 + 更新快照。"""
    st = SubTask(id="st-3", description="x", scope=FileScope(writable=["App.java"]))
    ex = WorkerExecutor(subtask=st, project_path=str(tmp_path))
    # 权威栈 = jakarta（Spring Boot ≥3）
    ex._resolve_project_stack = lambda: {"jvm": {"servlet_namespace": "jakarta"}}
    ex._sandbox = None  # local 模式，不回传沙箱
    ex._sandbox_manager = None
    src = "import javax.servlet.http.HttpServletRequest;\nclass App {}\n"
    ex._post_sync_contents = {"App.java": src}
    (tmp_path / "App.java").write_text(src)

    asyncio.run(ex._normalize_jvm_namespace(tmp_path, "test"))

    # 快照与本地文件都应改成 jakarta.servlet
    assert "jakarta.servlet.http.HttpServletRequest" in ex._post_sync_contents["App.java"]
    assert "javax.servlet" not in ex._post_sync_contents["App.java"]
    assert "jakarta.servlet" in (tmp_path / "App.java").read_text()


def test_normalize_jvm_namespace_noop_when_stack_not_jvm(tmp_path):
    """project_stack 未判明 servlet_namespace → no-op，不动内容（fail-safe）。"""
    st = SubTask(id="st-4", description="x", scope=FileScope(writable=["App.java"]))
    ex = WorkerExecutor(subtask=st, project_path=str(tmp_path))
    ex._resolve_project_stack = lambda: {}  # 无 jvm 画像
    ex._sandbox = None
    ex._sandbox_manager = None
    src = "import javax.servlet.http.HttpServletRequest;\n"
    ex._post_sync_contents = {"App.java": src}
    asyncio.run(ex._normalize_jvm_namespace(tmp_path, "test"))
    assert ex._post_sync_contents["App.java"] == src  # 原样不动
