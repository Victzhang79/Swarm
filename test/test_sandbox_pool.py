#!/usr/bin/env python3
"""HotSandboxPool 单元测试 — 用 mock manager，不连真沙箱。"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

# ── swarm_bootstrap ──
_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ── FakeManager / FakeSandbox ──────────────────────────

class CodeResult:
    """模拟 SandboxManager.run_code 的返回值。"""
    def __init__(self, stdout="", stderr="", error=None, success=True):
        self.stdout = stdout
        self.stderr = stderr
        self.error = error
        self.success = success


class FakeSandbox:
    """模拟 E2B Sandbox 对象。"""
    _counter = 0

    def __init__(self, sid=None):
        if sid is None:
            FakeSandbox._counter += 1
            sid = f"sbx-{FakeSandbox._counter}"
        self.sandbox_id = sid
        self.template_id = None  # 可由测试覆写


class FakeManager:
    """模拟 SandboxManager 的接口 — create/kill/run_code/register_sandbox_meta。"""

    def __init__(self, *, run_code_success=True, run_code_error=None):
        self._next_id = 0
        self._killed: list[str] = []
        self._created: list[FakeSandbox] = []
        self._meta: dict[str, dict] = {}
        # 可配置 run_code 行为
        self._run_code_success = run_code_success
        self._run_code_error = run_code_error
        # 可以注入按 sandbox_id 的探针失败
        self._unhealthy_ids: set[str] = set()

    def create(self, template_id=None, timeout=60, *, project_id=None, task_id=None, source="manual"):
        self._next_id += 1
        sbx = FakeSandbox(f"sbx-{self._next_id}")
        sbx.template_id = template_id
        self._created.append(sbx)
        self._meta[sbx.sandbox_id] = {
            "project_id": project_id,
            "task_id": task_id,
            "source": source,
        }
        return sbx

    def kill(self, sandbox_id):
        self._killed.append(sandbox_id)

    def run_code(self, sandbox, code, timeout=30):
        sid = getattr(sandbox, "sandbox_id", str(sandbox))
        if sid in self._unhealthy_ids:
            return CodeResult(success=False, error="unhealthy sandbox")
        if not self._run_code_success:
            return CodeResult(success=False, error=self._run_code_error or "probe failed")
        return CodeResult(stdout="1\n", success=True)

    def register_sandbox_meta(self, sandbox_id, *, project_id=None, task_id=None, source="manual"):
        self._meta[sandbox_id] = {
            "project_id": project_id,
            "task_id": task_id,
            "source": source,
        }

    def get_sandbox_meta(self, sandbox_id):
        return self._meta.get(sandbox_id)


# ── helpers ──

def _make_pool(manager=None, **kw):
    from swarm.worker.sandbox_pool import HotSandboxPool
    if manager is None:
        manager = FakeManager()
    return HotSandboxPool(manager, **kw)


# ══════════════════════════════════════════════════════
# 测试用例
# ══════════════════════════════════════════════════════

def test_acquire_empty_creates_new():
    """池空时 acquire 创建新沙箱。"""
    mgr = FakeManager()
    pool = _make_pool(mgr)
    sbx = pool.acquire("tpl-a")
    assert sbx is not None
    assert sbx.sandbox_id.startswith("sbx-")
    assert len(mgr._created) == 1
    print("  ✅ acquire 池空时创建新沙箱")


def test_release_then_acquire_reuses():
    """release 后 acquire 复用同一个沙箱（不新建）。"""
    mgr = FakeManager()
    pool = _make_pool(mgr)
    sbx1 = pool.acquire("tpl-a")
    pool.release(sbx1, reusable=True)
    sbx2 = pool.acquire("tpl-a")
    assert sbx2.sandbox_id == sbx1.sandbox_id
    assert len(mgr._created) == 1, "应该复用，不应新建"
    print("  ✅ release 后 acquire 复用同一个沙箱")


def test_health_probe_failure_discards_and_creates_new():
    """健康探针失败 → acquire 丢弃坏沙箱(kill)并新建好的。"""
    mgr = FakeManager()
    pool = _make_pool(mgr)
    sbx1 = pool.acquire("tpl-a")
    sid1 = sbx1.sandbox_id
    pool.release(sbx1, reusable=True)

    # 标记池中沙箱为不健康
    mgr._unhealthy_ids.add(sid1)

    sbx2 = pool.acquire("tpl-a")
    assert sbx2.sandbox_id != sid1, "应丢弃坏沙箱，创建新的"
    assert sid1 in mgr._killed, "坏沙箱应被 kill"
    assert len(mgr._created) == 2, "应创建第二个沙箱"
    print("  ✅ 健康探针失败 → 丢弃 + kill + 新建")


def test_reap_ttl_expired():
    """reap 回收超 TTL 的沙箱（伪造过期 created_at）。"""
    from swarm.worker.sandbox_pool import _PoolEntry
    mgr = FakeManager()
    pool = _make_pool(mgr, ttl_seconds=10)
    sbx = pool.acquire("tpl-a")
    pool.release(sbx, reusable=True)

    # 伪造 entry 的 created_at 让它超 TTL
    key = "tpl-a"  # bucket key
    with pool._lock:
        bucket = pool._pool.get("tpl-a", [])
        for entry in bucket:
            entry.created_at = time.monotonic() - 100  # 100s ago > 10s TTL

    result = pool.reap()
    assert result["killed"] == 1
    assert sbx.sandbox_id in mgr._killed
    print("  ✅ reap 回收超 TTL 沙箱")


def test_reap_idle_expired():
    """reap 回收超空闲时间的沙箱（伪造过期 last_used_at）。"""
    mgr = FakeManager()
    pool = _make_pool(mgr, idle_seconds=10)
    sbx = pool.acquire("tpl-a")
    pool.release(sbx, reusable=True)

    with pool._lock:
        bucket = pool._pool.get("tpl-a", [])
        for entry in bucket:
            entry.last_used_at = time.monotonic() - 100  # 100s ago > 10s idle

    result = pool.reap()
    assert result["killed"] == 1
    assert sbx.sandbox_id in mgr._killed
    print("  ✅ reap 回收超空闲沙箱")


def test_reap_keeps_healthy():
    """reap 保留未过期且健康的沙箱。"""
    mgr = FakeManager()
    pool = _make_pool(mgr, ttl_seconds=600, idle_seconds=300)
    sbx = pool.acquire("tpl-a")
    pool.release(sbx, reusable=True)

    result = pool.reap()
    assert result["killed"] == 0
    assert result["kept"] == 1
    assert sbx.sandbox_id not in mgr._killed
    print("  ✅ reap 保留健康未过期沙箱")


def test_max_total_temp_sandbox():
    """超 max_total acquire 仍返回（临时沙箱），不进池标记。"""
    mgr = FakeManager()
    pool = _make_pool(mgr, max_total=2)

    sbx1 = pool.acquire("tpl-a")
    sbx2 = pool.acquire("tpl-a")
    # 第三个应返回临时沙箱
    sbx3 = pool.acquire("tpl-a")

    assert sbx3 is not None
    assert len(mgr._created) == 3

    # 临时沙箱 release 时应被 kill
    pool.release(sbx3, reusable=True)
    assert sbx3.sandbox_id in mgr._killed
    print("  ✅ 超 max_total acquire 仍返回临时沙箱，release 时 kill")


def test_max_idle_release_kills_excess():
    """超 max_idle_per_template 时 release kill 多余沙箱。"""
    mgr = FakeManager()
    pool = _make_pool(mgr, max_idle_per_template=1)

    sbx1 = pool.acquire("tpl-a")
    sbx2 = pool.acquire("tpl-a")
    pool.release(sbx1, reusable=True)
    # 第二个 release 时池里已有 1 个，应 kill
    pool.release(sbx2, reusable=True)

    assert sbx2.sandbox_id in mgr._killed
    print("  ✅ 超 max_idle release 时 kill 多余沙箱")


def test_release_not_reusable_kills():
    """reusable=False 时 release 直接 kill。"""
    mgr = FakeManager()
    pool = _make_pool(mgr)
    sbx = pool.acquire("tpl-a")
    pool.release(sbx, reusable=False)
    assert sbx.sandbox_id in mgr._killed
    print("  ✅ reusable=False release 直接 kill")


def test_concurrent_acquire_release():
    """多线程并发 acquire/release 不崩、账本一致。"""
    mgr = FakeManager()
    pool = _make_pool(mgr, max_total=20, max_idle_per_template=5)
    errors: list[Exception] = []

    def worker(tid):
        try:
            for _ in range(50):
                sbx = pool.acquire("tpl-a", project_id=f"p-{tid}", task_id=f"t-{tid}")
                # 模拟短暂使用
                time.sleep(0.001)
                pool.release(sbx, reusable=True)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"并发错误: {errors}"

    # 账本一致性检查
    st = pool.stats()
    assert st["borrowed"] >= 0, f"borrowed 不应为负: {st['borrowed']}"
    assert st["total_idle"] >= 0, f"total_idle 不应为负: {st['total_idle']}"
    assert st["total"] >= 0
    print("  ✅ 多线程并发 acquire/release 不崩，账本一致")


def test_concurrent_stress_no_keyerror():
    """高并发压力测试不出现 KeyError / 负数。"""
    mgr = FakeManager()
    pool = _make_pool(mgr, max_total=50, max_idle_per_template=3)
    errors: list[Exception] = []

    def worker(tid):
        try:
            for i in range(100):
                sbx = pool.acquire("tpl-a" if tid % 2 == 0 else "tpl-b")
                pool.release(sbx, reusable=(i % 3 != 0))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"并发压力错误: {errors}"
    st = pool.stats()
    assert st["borrowed"] >= 0
    assert st["total_idle"] >= 0
    for k, v in st["idle_by_template"].items():
        assert v >= 0, f"桶 {k} 有负数: {v}"
    print("  ✅ 高并发压力无 KeyError / 负数")


def test_drain_kills_all():
    """drain 清空池全部 kill。"""
    mgr = FakeManager()
    pool = _make_pool(mgr)
    sbx1 = pool.acquire("tpl-a")
    sbx2 = pool.acquire("tpl-a")
    pool.release(sbx1, reusable=True)
    pool.release(sbx2, reusable=True)

    # 池中有 2 个
    assert pool.stats()["total_idle"] == 2

    pool.drain()
    assert pool.stats()["total_idle"] == 0
    assert pool.stats()["total"] == 0
    # 两个都被 kill
    assert sbx1.sandbox_id in mgr._killed
    assert sbx2.sandbox_id in mgr._killed
    print("  ✅ drain 清空池全 kill")


def test_statsreturns_metrics():
    """stats() 返回可观测指标。"""
    mgr = FakeManager()
    pool = _make_pool(mgr, max_total=10, max_idle_per_template=2, ttl_seconds=600, idle_seconds=300)
    sbx1 = pool.acquire("tpl-a")
    sbx2 = pool.acquire("tpl-b")
    pool.release(sbx1, reusable=True)
    pool.release(sbx2, reusable=True)

    st = pool.stats()
    assert "idle_by_template" in st
    assert "total_idle" in st
    assert "borrowed" in st
    assert "total" in st
    assert "created_total" in st
    assert st["total_idle"] == 2
    assert st["borrowed"] == 0
    assert st["created_total"] == 2
    assert st["max_total"] == 10
    print("  ✅ stats() 返回完整可观测指标")


def test_reaper_start_stop():
    """start_reaper / stop_reaper 生命周期。"""
    mgr = FakeManager()
    pool = _make_pool(mgr, reap_interval=1)
    pool.start_reaper()
    assert pool._reaper_thread is not None
    assert pool._reaper_thread.is_alive()

    pool.stop_reaper()
    # 线程应已退出
    time.sleep(0.2)
    assert not pool._reaper_thread or not pool._reaper_thread.is_alive()
    print("  ✅ reaper start/stop 生命周期正常")


def test_reaper_kills_expired():
    """reaper 线程能自动回收过期沙箱。"""
    mgr = FakeManager()
    pool = _make_pool(mgr, ttl_seconds=1, idle_seconds=1, reap_interval=1)

    sbx = pool.acquire("tpl-a")
    pool.release(sbx, reusable=True)
    sid = sbx.sandbox_id

    # 伪造过期
    with pool._lock:
        for bucket in pool._pool.values():
            for entry in bucket:
                entry.created_at = time.monotonic() - 10
                entry.last_used_at = time.monotonic() - 10

    pool.start_reaper()
    time.sleep(2.5)
    pool.stop_reaper()

    assert sid in mgr._killed, "reaper 应在后台 kill 过期沙箱"
    assert pool.stats()["total_idle"] == 0
    print("  ✅ reaper 线程自动回收过期沙箱")


def test_acquire_register_meta():
    """acquire 后 register_sandbox_meta 已绑定 task。"""
    mgr = FakeManager()
    pool = _make_pool(mgr)
    sbx = pool.acquire("tpl-a", project_id="proj-1", task_id="task-1")
    meta = mgr.get_sandbox_meta(sbx.sandbox_id)
    assert meta is not None
    assert meta["task_id"] == "task-1"
    assert meta["project_id"] == "proj-1"
    print("  ✅ acquire 后 meta 绑定 task")


def test_release_then_reacquire_rebinds_meta():
    """release + 再次 acquire 时 meta 重新绑定新 task。"""
    mgr = FakeManager()
    pool = _make_pool(mgr)
    sbx1 = pool.acquire("tpl-a", project_id="p1", task_id="t1")
    pool.release(sbx1, reusable=True)
    sbx2 = pool.acquire("tpl-a", project_id="p2", task_id="t2")
    assert sbx2.sandbox_id == sbx1.sandbox_id
    meta = mgr.get_sandbox_meta(sbx2.sandbox_id)
    assert meta["task_id"] == "t2"
    assert meta["project_id"] == "p2"
    print("  ✅ 二次 acquire 时 meta 重新绑定新 task")


def test_template_bucket_isolation():
    """不同 template_id 的沙箱互不干扰。"""
    mgr = FakeManager()
    pool = _make_pool(mgr, max_idle_per_template=1)
    sbx_a = pool.acquire("tpl-a")
    sbx_b = pool.acquire("tpl-b")
    pool.release(sbx_a, reusable=True)
    pool.release(sbx_b, reusable=True)

    st = pool.stats()
    assert st["idle_by_template"]["tpl-a"] == 1
    assert st["idle_by_template"]["tpl-b"] == 1

    # 从 tpl-a 取应该拿到 sbx_a
    sbx_a2 = pool.acquire("tpl-a")
    assert sbx_a2.sandbox_id == sbx_a.sandbox_id
    print("  ✅ 不同 template 分桶隔离")


def test_drain_survives_kill_failure():
    """drain 时单个 kill 失败不中断其余。"""
    mgr = FakeManager()
    pool = _make_pool(mgr)

    sbx1 = pool.acquire("tpl-a")
    sbx2 = pool.acquire("tpl-a")
    pool.release(sbx1, reusable=True)
    pool.release(sbx2, reusable=True)

    # 让 kill 对 sbx1 抛异常
    original_kill = mgr.kill
    def flaky_kill(sid):
        if sid == sbx1.sandbox_id:
            raise RuntimeError("kill failed")
        original_kill(sid)
    mgr.kill = flaky_kill

    # drain 不应崩溃
    pool.drain()
    # sbx2 应正常被 kill（在 mgr._killed 列表里，但注意我们替换了 kill）
    # 由于 sbx1 kill 抛异常，原 _killed 只有 sbx2
    assert sbx2.sandbox_id in mgr._killed
    print("  ✅ drain 时单个 kill 失败不中断")


def test_reap_survives_kill_failure():
    """reap 时单个 kill 失败不中断。"""
    mgr = FakeManager()
    pool = _make_pool(mgr, ttl_seconds=1)

    sbx1 = pool.acquire("tpl-a")
    sbx2 = pool.acquire("tpl-a")
    pool.release(sbx1, reusable=True)
    pool.release(sbx2, reusable=True)

    # 伪造过期
    with pool._lock:
        for bucket in pool._pool.values():
            for entry in bucket:
                entry.created_at = time.monotonic() - 100

    # 让 kill 对第一个抛异常
    original_kill = mgr.kill
    fail_sid = sbx1.sandbox_id
    def flaky_kill(sid):
        if sid == fail_sid:
            raise RuntimeError("kill failed")
        original_kill(sid)
    mgr.kill = flaky_kill

    result = pool.reap()
    assert result["killed"] == 2  # 账本上记录 2 个被 kill
    print("  ✅ reap 时单个 kill 失败不中断")


def test_none_template_uses_empty_key():
    """template_id=None 时使用空字符串做桶 key。"""
    mgr = FakeManager()
    pool = _make_pool(mgr)
    sbx = pool.acquire(None)
    pool.release(sbx, reusable=True)
    st = pool.stats()
    assert "" in st["idle_by_template"]
    print("  ✅ template_id=None 用空字符串分桶")


# ── main ──

def test_acquire_skips_ttl_expired_pooled_sandbox():
    """[回归] acquire 不复用 TTL 已过期的池内沙箱(即使健康探针会通过)——
    应 kill 之并新建。守护我审核时修的 TTL-on-reuse bug。"""
    mgr = FakeManager()
    pool = _make_pool(mgr, ttl_seconds=600, idle_seconds=9999)
    # 借一个再还回池
    sb = pool.acquire(project_id="p", task_id="t1")
    pool.release(sb, reusable=True)
    old_sid = sb.sandbox_id
    # 把它的 created_at 伪造成 700s 前(超 ttl=600)
    pool._created_at[old_sid] = time.monotonic() - 700
    for entry in pool._pool.get("", []):
        if entry.sandbox.sandbox_id == old_sid:
            entry.created_at = time.monotonic() - 700
    # 再 acquire：不应复用过期的 old_sid，应 kill 它并新建
    sb2 = pool.acquire(project_id="p", task_id="t2")
    assert sb2.sandbox_id != old_sid, "复用了 TTL 过期沙箱"
    assert old_sid in mgr._killed, "过期沙箱未被 kill(泄漏)"
    print("  ✅ [回归] acquire 跳过并 kill TTL 过期池内沙箱")


def test_release_ttl_expired_does_not_pool():
    """[回归] release 一个已超 TTL 的沙箱时直接 kill，不放回池。"""
    mgr = FakeManager()
    pool = _make_pool(mgr, ttl_seconds=600)
    sb = pool.acquire(project_id="p", task_id="t1")
    # 伪造其创建时间超龄
    pool._created_at[sb.sandbox_id] = time.monotonic() - 700
    pool.release(sb, reusable=True)
    assert sb.sandbox_id in mgr._killed, "超 TTL 沙箱 release 时应 kill"
    assert pool.stats()["total_idle"] == 0, "超 TTL 沙箱不应进池"
    print("  ✅ [回归] release 超 TTL 沙箱直接 kill 不入池")


def main():
    tests = [
        test_acquire_empty_creates_new,
        test_release_then_acquire_reuses,
        test_health_probe_failure_discards_and_creates_new,
        test_reap_ttl_expired,
        test_reap_idle_expired,
        test_reap_keeps_healthy,
        test_max_total_temp_sandbox,
        test_max_idle_release_kills_excess,
        test_release_not_reusable_kills,
        test_concurrent_acquire_release,
        test_concurrent_stress_no_keyerror,
        test_drain_kills_all,
        test_statsreturns_metrics,
        test_reaper_start_stop,
        test_reaper_kills_expired,
        test_acquire_register_meta,
        test_release_then_reacquire_rebinds_meta,
        test_template_bucket_isolation,
        test_drain_survives_kill_failure,
        test_reap_survives_kill_failure,
        test_none_template_uses_empty_key,
        test_acquire_skips_ttl_expired_pooled_sandbox,
        test_release_ttl_expired_does_not_pool,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n📊 结果: {passed} 通过, {failed} 失败\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
