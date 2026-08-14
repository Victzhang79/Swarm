"""30 号文批24 锁：B-2b 告警节流 + 批16 LEAD xdist 缺席账聚合。

- B-2b：PG 宕期 dispatch #77 水位读/回写 + runner /progress 轮询点成对洗版
  （批15 升 WARNING 方向正确，量级不加节制=噪声洪泛淹真信号）。新原语
  `infra/log_throttle.throttled`：同 key 60s 心跳一条，放行条带抑制计数
  （条数不丢账，机读）。接线三处：dispatch 水位读/回写（抽模块级函数）、
  runner get_task_progress（inline）。
- 批16 LEAD①：xdist 下 worker 进程的 SERVICE_ABSENT 缺席账是进程私有的，
  不回传 ⇒ controller 会话末汇总漏掉 worker 里的降级。conftest 加
  pytest_sessionfinish（worker 写 workeroutput）+ pytest_testnodedown
  （controller 回收合并，optionalhook 豁免无 xdist 环境）。时序安全性已由 R1
  hunter 用 pytest-xdist 3.8.0 源码核验（workerfinished 传输在 sessionfinish
  yield 之后、testnodedown 早于 terminal_summary ⇒ 汇总前账必并入）。
  ★残留边界★：本仓不装 xdist（addopts 无 -n），真实插件接线不可在本仓证伪——
  锁覆盖=操作面逻辑（经 service_probe_internals fixture）+钩子被 pytest
  收编（插件注册锁）；worker 崩溃走 errordown 时该 worker 缺席账丢失
  （xdist 本身响亮报告崩溃，hunter L-4 判可接受）。
"""
from __future__ import annotations

import asyncio
import importlib
import logging
from pathlib import Path

import pytest

import swarm.brain.runner as runner
import swarm.infra.log_throttle as lt

# 包属性被同名函数遮蔽（swarm/brain/nodes/__init__.py re-export dispatch 函数），
# `import swarm.brain.nodes.dispatch as dsp` 拿到的是函数不是模块——须 importlib。
dsp = importlib.import_module("swarm.brain.nodes.dispatch")


# ── B-2b：throttled 原语行为锁 ─────────────────────────────


def test_throttled_first_emit_then_suppress_then_heartbeat():
    """主锁：首放=0 → 窗口内抑制（None）→ 窗口后放行且带回被抑制条数。"""
    key = "b24.unit.main"
    assert lt.throttled(key, interval=60.0, _now=1000.0) == 0, "首次必须放行且计数 0"
    assert lt.throttled(key, interval=60.0, _now=1001.0) is None, "窗口内必须抑制"
    assert lt.throttled(key, interval=60.0, _now=1002.0) is None
    # 距上次放行 60s 后：放行，且报告期间抑制了 2 条（条数不丢账）
    assert lt.throttled(key, interval=60.0, _now=1060.0) == 2, \
        "窗口后放行必须带回被抑制条数"
    # 放行后计数归零重新开始
    assert lt.throttled(key, interval=60.0, _now=1061.0) is None
    assert lt.throttled(key, interval=60.0, _now=1121.0) == 1


def test_throttled_key_isolation():
    """不同 key 互不干扰（站点常量语义）。"""
    assert lt.throttled("b24.iso.a", _now=1000.0) == 0
    assert lt.throttled("b24.iso.b", _now=1000.0) == 0, "异 key 不得被 a 的窗口抑制"
    assert lt.throttled("b24.iso.a", _now=1001.0) is None


def test_suppress_suffix_machine_readable():
    assert lt.suppress_suffix(0) == ""
    assert "已抑制 3 条" in lt.suppress_suffix(3)


# ── B-2b：dispatch 两点接线锁（批15 先例：循环深位不造重型夹具，
#         节流行为锁在抽出的模块级函数上）─────────────────


def test_dispatch_helpers_bind_log_throttle_single_source():
    """接线事实锁：dispatch 两个告警函数必须消费 log_throttle 单一事实源——
    若被人改成各写一份节流逻辑（双枚举漂移族），本锁红。"""
    assert dsp._warn_throttled is lt.throttled
    assert dsp.suppress_suffix is lt.suppress_suffix


def test_dispatch_progress_write_warn_throttled(monkeypatch, caplog):
    """回写点：None 不放行 / 0 放行无后缀 / 3 放行带抑制计数。"""
    calls = []

    def _fake(key):
        calls.append(key)
        return (0, None, 3)[len(calls) - 1]

    monkeypatch.setattr(dsp, "_warn_throttled", _fake)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            dsp._warn_progress_write_failed("t1", RuntimeError("pg down"))
    msgs = [r.getMessage() for r in caplog.records if "#77 进度回写失败" in r.getMessage()]
    assert calls == ["dispatch.progress_write"] * 3
    assert len(msgs) == 2, f"None 必须不放行: {msgs}"
    assert "已抑制" not in msgs[0] and "已抑制 3 条" in msgs[1]


def test_dispatch_progress_floor_read_warn_throttled(monkeypatch, caplog):
    """水位读点同形（独立 key，防两点共用 key 互相吞心跳）。"""
    calls = []

    def _fake(key):
        calls.append(key)
        return 0

    monkeypatch.setattr(dsp, "_warn_throttled", _fake)
    with caplog.at_level(logging.WARNING):
        dsp._warn_progress_floor_read_failed("t1", RuntimeError("pg down"))
    assert calls == ["dispatch.progress_floor_read"]
    assert any("#77 进度水位读取失败" in r.getMessage() for r in caplog.records)


# ── B-2b：runner /progress 轮询点【真驱动】接线锁 ──────────────────


def test_runner_progress_warning_throttled_end_to_end(monkeypatch, caplog):
    """真驱动 get_task_progress 失败路径两次：第一条 WARNING 放行、第二条被节流
    抑制——删节流接线（退回直打 logger.warning）本锁红。"""
    lt._reset()

    class _G:
        async def aget_state(self, config):
            raise RuntimeError("pg down")

    monkeypatch.setattr(runner, "get_compiled_brain_graph", lambda: _G())
    monkeypatch.setattr(runner.store, "get_task", lambda tid: {})
    try:
        with caplog.at_level(logging.WARNING):
            assert asyncio.run(runner.get_task_progress("t-b24")) is None
            first = [r for r in caplog.records if "[PROGRESS] 读取快照失败" in r.getMessage()]
            assert len(first) == 1, f"首次失败必须 WARNING 放行: {len(first)}"
            assert asyncio.run(runner.get_task_progress("t-b24")) is None
            second = [r for r in caplog.records if "[PROGRESS] 读取快照失败" in r.getMessage()]
            assert len(second) == 1, "60s 窗口内第二次同站失败必须被节流抑制"
    finally:
        lt._reset()  # 不留残余节流状态给后续用例


# ── 批16 LEAD①：xdist 缺席账聚合 ─────────────────────────────


def test_worker_absent_export_payload(service_probe_internals):
    """worker 侧载荷：两本账分档导出（skip 档与 hard 档绝不混——批16 M-3 分账契约）。"""
    st = service_probe_internals
    st["absent_seen"].add("b24fake_skip")
    st["failed_hard"].add("b24fake_hard")
    try:
        payload = st["export_worker_absent"]()
        assert "b24fake_skip" in payload["swarm_service_absent"]
        assert "b24fake_hard" in payload["swarm_service_failed_hard"]
        assert "b24fake_hard" not in payload["swarm_service_absent"], "两档绝不许混"
        assert payload["swarm_service_absent"] == sorted(payload["swarm_service_absent"])
    finally:
        st["absent_seen"].discard("b24fake_skip")
        st["failed_hard"].discard("b24fake_hard")


def test_controller_merge_worker_absent(service_probe_internals):
    """controller 侧回收：worker 载荷并入本会话账（汇总出口=terminal_summary 单点）。"""
    st = service_probe_internals
    try:
        st["merge_worker_absent"]({"swarm_service_absent": ["b24merge_a"],
                                   "swarm_service_failed_hard": ["b24merge_b"]})
        assert "b24merge_a" in st["absent_seen"]
        assert "b24merge_b" in st["failed_hard"]
        st["merge_worker_absent"](None)   # 无载荷不炸
        st["merge_worker_absent"]({})     # 空载荷不炸
    finally:
        st["absent_seen"].discard("b24merge_a")
        st["failed_hard"].discard("b24merge_b")


def test_xdist_hooks_registered_with_pytest(request):
    """钩子收编锁：两个钩子必须真的挂在已注册的 test/conftest.py 插件上——
    optionalhook 写错/函数名打错=钩子静默闲置（汇总漏 worker 账无人知晓），本锁红。"""
    found = set()
    for plugin in request.config.pluginmanager.get_plugins():
        p = Path(str(getattr(plugin, "__file__", "")))
        if p.name == "conftest.py" and p.parent.name == "test":
            for name in ("pytest_sessionfinish", "pytest_testnodedown"):
                if getattr(plugin, name, None) is not None:
                    found.add(name)
    assert found == {"pytest_sessionfinish", "pytest_testnodedown"}, \
        f"conftest 插件上缺钩子实现: {found}"
