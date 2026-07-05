"""round26 Step3 行为测试：_L1GateMixin 外置后的 MRO 接线 + _failure_signature 纯归一。

pr-test 范式沿用：每个 mixin 补 MRO 守卫（class 声明写错会让方法从实例消失并以难懂
AttributeError 暴露）。_failure_signature 是 no-progress 早停的判重基石——错误一字不变→同
签名，行列号/路径/ANSI 抖动不算进展；直接测其归一契约（真行为，非 inspect.getsource）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.worker.executor import WorkerExecutor  # noqa: E402
from swarm.worker.executor_l1gate import _L1GateMixin  # noqa: E402


def test_l1gate_mixin_wired_into_mro():
    """★_L1GateMixin 在 WorkerExecutor 的 MRO 上，且优先于 SYNC/PROMPT（声明顺序）。"""
    assert _L1GateMixin in WorkerExecutor.__mro__
    assert issubclass(WorkerExecutor, _L1GateMixin)
    mro = [c.__name__ for c in WorkerExecutor.__mro__]
    assert mro[:2] == ["WorkerExecutor", "_L1GateMixin"]
    assert "_SandboxSyncMixin" in mro and "_PromptBuildingMixin" in mro


def test_failure_signature_ignores_linecol_path_ansi_jitter():
    """同一编译错误，只有行列号/绝对路径/ANSI 抖动 → 同签名（判为无进展）。"""
    a = WorkerExecutor._failure_signature(
        {"compile_message": "\x1b[31m/abs/proj/App.java:[10,5]: cannot find symbol Foo"}
    )
    b = WorkerExecutor._failure_signature(
        {"compile_message": "/other/path/App.java:[99,88]: cannot find symbol Foo"}
    )
    assert a and a == b  # 抖动被归一，签名稳定


def test_failure_signature_distinguishes_different_errors():
    """错误内容真变（符号名不同）→ 不同签名（有进展/新错）。"""
    a = WorkerExecutor._failure_signature({"compile_message": "App.java:[1,1]: cannot find symbol Foo"})
    c = WorkerExecutor._failure_signature({"compile_message": "App.java:[1,1]: cannot find symbol Bar"})
    assert a != c


def test_failure_signature_empty_on_no_evidence():
    """无任何失败输出 → 空签名（不误判无进展）。"""
    assert WorkerExecutor._failure_signature({}) == ""
    assert WorkerExecutor._failure_signature({"build_output": "   "}) == ""
    assert WorkerExecutor._failure_signature("not a dict") == ""  # type: ignore[arg-type]
