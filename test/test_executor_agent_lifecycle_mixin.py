"""round26 Step4 行为测试：_AgentLoopMixin / _SandboxLifecycleMixin 外置后的 MRO 接线。

最后两簇（AGENT agent 构建/运行/栈画像；LIFECYCLE 沙箱创建/执行/销毁）外置。沿用 mixin
MRO 守卫范式——class 声明写错会让方法从实例消失并以难懂 AttributeError 暴露；此处直接钉住
5 个 mixin 全部在 MRO 上、且 WorkerExecutor 本体优先。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.worker.executor import WorkerExecutor  # noqa: E402
from swarm.worker.executor_agent import _AgentLoopMixin  # noqa: E402
from swarm.worker.executor_lifecycle import _SandboxLifecycleMixin  # noqa: E402


def test_agent_and_lifecycle_mixins_wired_into_mro():
    assert _AgentLoopMixin in WorkerExecutor.__mro__
    assert _SandboxLifecycleMixin in WorkerExecutor.__mro__
    assert issubclass(WorkerExecutor, (_AgentLoopMixin, _SandboxLifecycleMixin))


def test_all_five_mixins_present_and_executor_first():
    """5 个 round26 mixin 全部在 MRO 上，WorkerExecutor 本体优先（自身方法不被 mixin 遮蔽）。"""
    mro = [c.__name__ for c in WorkerExecutor.__mro__]
    assert mro[0] == "WorkerExecutor"
    for name in ("_L1GateMixin", "_SandboxSyncMixin", "_PromptBuildingMixin",
                 "_AgentLoopMixin", "_SandboxLifecycleMixin"):
        assert name in mro, f"{name} 掉出 MRO（mixin 接线断裂）"


def test_agent_and_lifecycle_methods_addressable_on_class():
    """外置的方法经 MRO 仍可从 WorkerExecutor 类寻址（patch.object / getsource 前提）。"""
    for m in ("_run_agent", "_create_agent", "_resolve_project_stack", "_remaining_seconds"):
        assert callable(getattr(WorkerExecutor, m)), m
    for m in ("kill_sandbox",):  # 批5：create/run_in_sandbox 死代码已删（主流程直连池）
        assert callable(getattr(WorkerExecutor, m)), m
