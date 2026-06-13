"""沙箱健康防护测试：连续失败熔断 + envd 健康探活 + 错误分类。

修复背景（2026-06-13 E2E 真实验证发现）：
node 4c4g 坏镜像的沙箱 envd 故障，worker 在坏沙箱上空转死循环（186 次/10 分钟）。
本测试固化两层防护：① 借/建沙箱后 envd 探活换新 ② 运行中连续失败熔断。
"""

import importlib.util
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.worker.sandbox import SandboxManager, SandboxUnhealthyError


class _FakeConfig:
    sandbox_fail_threshold = 3  # 测试用小阈值


def _mgr():
    m = SandboxManager.__new__(SandboxManager)
    m._fail_counts = {}
    m.config = _FakeConfig()
    return m


def test_consecutive_failures_trip_breaker():
    """连续失败达阈值 → 抛 SandboxUnhealthyError（熔断）。"""
    m = _mgr()
    sid = "_test_sb_breaker"
    # 阈值-1 次不熔断
    for _ in range(_FakeConfig.sandbox_fail_threshold - 1):
        m._record_sandbox_failure(sid)
    assert m._fail_counts[sid] == _FakeConfig.sandbox_fail_threshold - 1
    # 第 N 次熔断
    with pytest.raises(SandboxUnhealthyError):
        m._record_sandbox_failure(sid)
    print("  ✅ 连续失败达阈值触发熔断")


def test_success_resets_failure_count():
    """成功操作清零失败计数（连续失败才熔断，偶发抖动可恢复）。"""
    m = _mgr()
    sid = "_test_sb_reset"
    m._record_sandbox_failure(sid)
    m._record_sandbox_failure(sid)
    assert m._fail_counts[sid] == 2
    m._record_sandbox_success(sid)
    assert m._fail_counts[sid] == 0
    # 清零后可再积累，不会因之前的失败立即熔断
    m._record_sandbox_failure(sid)
    assert m._fail_counts[sid] == 1
    print("  ✅ 成功操作清零失败计数")


def test_intermittent_failures_do_not_trip():
    """失败-成功交替（偶发抖动）不应熔断。"""
    m = _mgr()
    sid = "_test_sb_intermittent"
    for _ in range(10):
        m._record_sandbox_failure(sid)
        m._record_sandbox_success(sid)  # 每次失败后都成功一次 → 永远清零
    assert m._fail_counts[sid] == 0
    print("  ✅ 偶发抖动(失败-成功交替)不误触熔断")


def test_health_check_passes_on_marker():
    """健康探活：run_command 返回标记 → 健康。"""
    m = _mgr()

    class _OKResult:
        success = True
        stdout = "__SWARM_HEALTH_OK__\n"
        error = None

    m.run_command = lambda sb, cmd, timeout=15, _count_failures=True: _OKResult()
    fake_sb = type("SB", (), {"sandbox_id": "_test_sb_ok"})()
    assert m.health_check(fake_sb) is True
    print("  ✅ envd 探活：标记返回 → 健康")


def test_health_check_fails_on_error():
    """健康探活：run_command 失败（envd 故障）→ 不健康。"""
    m = _mgr()

    class _BadResult:
        success = False
        stdout = ""
        error = "ConnectError: 500"

    m.run_command = lambda sb, cmd, timeout=15, _count_failures=True: _BadResult()
    fake_sb = type("SB", (), {"sandbox_id": "_test_sb_bad"})()
    assert m.health_check(fake_sb) is False
    print("  ✅ envd 探活：失败 → 不健康(将触发换新沙箱)")


def test_health_check_does_not_count_toward_breaker():
    """探活的失败不计入熔断计数（_count_failures=False）。"""
    m = _mgr()
    captured = {}

    def _fake_run(sb, cmd, timeout=15, _count_failures=True):
        captured["count_failures"] = _count_failures

        class _R:
            success = False
            stdout = ""
            error = "boom"
        return _R()

    m.run_command = _fake_run
    fake_sb = type("SB", (), {"sandbox_id": "_test_sb_probe"})()
    m.health_check(fake_sb)
    assert captured["count_failures"] is False, "探活必须不计入熔断计数"
    assert m._fail_counts.get("_test_sb_probe", 0) == 0
    print("  ✅ 探活失败不污染熔断计数")


if __name__ == "__main__":
    test_consecutive_failures_trip_breaker()
    test_success_resets_failure_count()
    test_intermittent_failures_do_not_trip()
    test_health_check_passes_on_marker()
    test_health_check_fails_on_error()
    test_health_check_does_not_count_toward_breaker()
    print("\n✅ 沙箱健康防护全部测试通过")
