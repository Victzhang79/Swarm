"""round26 Step1 行为测试：_PromptBuildingMixin 外置后，prompt 构建方法在
WorkerExecutor 实例上经 MRO 仍可寻址且行为不变。

真·行为测试——断言 prompt 【内容/结构】而非实现结构（不 inspect.getsource）。覆盖此前无
专门行为测试的 5 个 builder：locate / code / verify / produce / scope_ops_hint。
纯方法，不触沙箱/网络。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.worker.executor import WorkerExecutor  # noqa: E402
from swarm.worker.executor_prompts import _PromptBuildingMixin  # noqa: E402
from swarm.types import FileScope, SubTask  # noqa: E402


def _ex(scope: FileScope, *, snippets: str = "") -> WorkerExecutor:
    st = SubTask(id="st-1", description="做个功能", scope=scope, context_snippets=snippets)
    return WorkerExecutor(subtask=st)


def test_mixin_wired_into_mro():
    """混入类真的在 WorkerExecutor 的 MRO 上（外置未断链）。"""
    assert _PromptBuildingMixin in WorkerExecutor.__mro__
    assert issubclass(WorkerExecutor, _PromptBuildingMixin)


def test_scope_ops_hint_classifies_operations():
    ex = _ex(FileScope(writable=["a.py"], create_files=["b.py"], delete_files=["c.py"]))
    hint = ex._scope_ops_hint()
    assert "【修改现有文件】" in hint and "a.py" in hint
    assert "【新建文件】" in hint and "b.py" in hint
    assert "【删除文件】" in hint and "c.py" in hint


def test_scope_ops_hint_free_creation_mode():
    """allow_any + 无显式清单 → 自由创建模式提示。"""
    ex = _ex(FileScope(allow_any=True))
    assert "自由创建模式" in ex._scope_ops_hint()


def test_locate_prompt_injects_context_snippets():
    """有预读片段时 locate prompt 追加片段块；无则不追加。"""
    with_snip = _ex(FileScope(writable=["a.py"]), snippets="def foo(): ...")
    p = with_snip._build_locate_prompt()
    # 📎 标记 + 片段正文只在有 context_snippets 时出现（静态 prompt 本身含"预读代码上下文"字样）。
    assert "Phase 1" in p and "def foo(): ..." in p and "📎" in p

    no_snip = _ex(FileScope(writable=["a.py"]))
    assert "📎" not in no_snip._build_locate_prompt()


def test_code_prompt_embeds_locate_result_and_ops():
    ex = _ex(FileScope(writable=["a.py"]))
    p = ex._build_code_prompt("定位到了 a.py 第 10 行")
    assert "Phase 2" in p
    assert "定位到了 a.py 第 10 行" in p
    assert "【修改现有文件】" in p  # scope_ops_hint 被嵌入


def test_verify_prompt_forbids_heavy_build():
    ex = _ex(FileScope(writable=["a.py"]))
    p = ex._build_verify_prompt()
    assert "Phase 3" in p and "L1_RESULT" in p
    assert "禁止运行重型构建" in p  # worker 不自跑 mvn/gradle


def test_produce_prompt_has_output_contract():
    ex = _ex(FileScope(writable=["a.py"]))
    p = ex._build_produce_prompt()
    assert "SUMMARY:" in p and "CONFIDENCE:" in p and "NOTES:" in p


def test_batch_code_prompt_scopes_to_batch_and_marks_done():
    ex = _ex(FileScope(writable=["a.py", "b.py"]))
    p = ex._build_batch_code_prompt("loc", ["a.py"], ["done.py"], 2, 3)
    assert "分批 2/3" in p
    assert "a.py" in p  # 本批（batch 参数）
    assert "done.py" in p  # 已完成（勿重做）
