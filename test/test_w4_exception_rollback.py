"""#29-5 W-4：run() 异常路径必须做 H2 清单足迹回滚。

旧形态：produce/pull-back 后抛异常（StreamDegenerationError 等）冒泡到
`run()` 的 `except Exception` 块 ⇒ 取 diff 后直接 return ⇒ 毒清单留共享树 ⇒
被 bootstrap「补传上游产物」复制进后续全部沙箱（正常路径两处回滚调用点——
run() 末尾与 trivial 路径——异常路径零调用，grep 全仓坐实）。
既有测试对 `_rollback_failed_manifest_footprint` 只有 monkeypatch no-op 或直调
mixin 内部逻辑——无任何用例走 run() 异常路径断「回滚被调用过」（突变判据=零覆盖）。
"""

import asyncio
import time
from types import SimpleNamespace

from swarm.models.errors import StreamDegenerationError, TransientInfraError
from swarm.types import SubTaskDifficulty
from swarm.worker.executor import WorkerExecutor


def _mk_run_stub(produce_exc=None, rollback_exc=None):
    """驱动 WorkerExecutor.run 走 full 链到 _phase_produce 抛异常的 stub。

    五个 phase 全桩化（无沙箱/无 LLM）；finally 里的 clear_* 为真函数（contextvar
    清理无害）。返回 (stub, rollback_calls, logs)。
    """
    stub = SimpleNamespace()
    stub.subtask = SimpleNamespace(
        id="st-w4", difficulty=SubTaskDifficulty.MEDIUM, harness=None)
    stub.phase = None
    stub._l1_passed_flag = True
    stub.max_execution_time = 600
    logs: list[str] = []
    stub._log = lambda m, level="info": logs.append(str(m))

    async def _prepare():
        return None

    async def _locate():
        return (object(), None)

    async def _code(_loc):
        return None

    async def _verify():
        return (False, {}, None)

    async def _produce(*_a, **_k):
        raise produce_exc

    stub._phase_prepare = _prepare
    stub._phase_locate = _locate
    stub._phase_code = _code
    stub._phase_verify_loop = _verify
    stub._phase_produce = _produce
    stub._get_git_diff = lambda: "diff-already-on-disk"
    stub._make_output = lambda **kw: SimpleNamespace(**kw)
    stub.kill_sandbox = lambda: None
    rollback_calls: list = []

    def _rollback(details):
        rollback_calls.append(details)
        if rollback_exc is not None:
            raise rollback_exc

    stub._rollback_failed_manifest_footprint = _rollback
    stub.run = WorkerExecutor.run.__get__(stub)
    return stub, rollback_calls, logs


class TestW4ExceptionPathRollback:
    def test_exception_after_pullback_triggers_h2_rollback(self):
        """主回归锁：produce 抛 StreamDegenerationError（pull-back 后冒泡形）⇒
        run() 异常路径必须调 H2 回滚一次，且 details 与 output.l1_details 同源
        （同一异常同一台账——承重语义是同源非身份，见下方 `is` 断言注释）。"""
        stub, calls, _ = _mk_run_stub(StreamDegenerationError("链尾重复退化"))
        out = asyncio.run(stub.run())
        assert len(calls) == 1, "异常路径必须调一次 H2 回滚（旧形态零调用=毒清单留树）"
        # `is` 锁的是接线事实：同一次 exception_l1_details 计算的对象同时喂给回滚
        # 与 return（生产源码两调用点共用 _exc_details 局部量；突变 b=return 侧改回
        # 二次调用恰红）。生产 _make_output 在 _tool_telemetry 非空时做浅拷贝重打包，
        # 生产对象身份不保证——但浅拷贝保留回滚消费的全部键，承重语义是同源非身份。
        assert calls[0] is out.l1_details, "回滚 details 与 output.l1_details 必须同源"
        assert out.l1_passed is False
        assert "链尾重复退化" in out.summary

    def test_transient_exception_still_calls_rollback_with_class(self):
        """TransientInfraError 场景：异常路径无条件调回滚（R50-2 闸在函数内早返；
        transient 支路真身直调锁见 test_r48c_deepread_batch.py::
        test_r50_2_transient_class_skips_rollback——本条锁的是接线：failure_class
        键必须真实传到回滚边界供那道闸判）。"""
        stub, calls, _ = _mk_run_stub(TransientInfraError("沙箱拉取超时"))
        out = asyncio.run(stub.run())
        assert len(calls) == 1
        assert calls[0].get("failure_class") == "transient", \
            "R50-2 闸靠 failure_class 放行 transient——键必须真实传到位: " \
            f"{sorted(calls[0])}"

    def test_rollback_failure_never_masks_original_exception(self, caplog):
        """回滚自身抛异常 ⇒ 绝不盖住原始异常（output 仍带原始 summary/diff），
        且双信号留痕：任务级 _log + 进程级 logger.warning（hunter F1：更轻的
        「快照缺失 skip」都有进程级信号，回滚整体失败不能反而没有——
        prune 只摘幽灵成员不摘注入条目，回滚失败=毒 dependency 无对账层兜底）。"""
        import logging
        stub, calls, logs = _mk_run_stub(
            StreamDegenerationError("原始异常"),
            rollback_exc=RuntimeError("回滚自己也炸了"))
        with caplog.at_level(logging.WARNING, logger="swarm.worker.executor"):
            out = asyncio.run(stub.run())
        assert len(calls) == 1
        assert "原始异常" in out.summary, "回滚失败绝不改变终局判定"
        assert out.diff == "diff-already-on-disk", "DR-04-F6 的 diff 保留语义不受回滚影响"
        assert any("回滚失败" in m for m in logs), "回滚失败必须留日志（不致命但要可辨）"
        assert any("[H2] 清单足迹回滚异常" in r.message for r in caplog.records), \
            "回滚整体失败必须有进程级 WARNING（缺席可辨——信号强度倒挂纠正锁）"
