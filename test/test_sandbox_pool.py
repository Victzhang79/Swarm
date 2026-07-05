#!/usr/bin/env python3
"""HotSandboxPool 单元测试 — 用 mock manager，不连真沙箱。"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path

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
    """模拟 E2B Sandbox 对象。

    真实 E2B Sandbox【没有】template_id 属性——create 只把 template 用于日志/audit，
    从不写回对象。这里刻意【不设】template_id，以免掩盖 release 桶键退化的生产 bug
    （旧测试在此设了 template_id，让 getattr 反推桶键侥幸成功，掩盖了真实缺陷）。
    """
    _counter = 0

    def __init__(self, sid=None):
        if sid is None:
            FakeSandbox._counter += 1
            sid = f"sbx-{FakeSandbox._counter}"
        self.sandbox_id = sid


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
        # workspace 清理记录 + 可注入清理失败
        self._cleaned: list[str] = []
        self._clean_fail_ids: set[str] = set()

    def create(self, template_id=None, timeout=60, *, project_id=None, task_id=None, source="manual"):
        self._next_id += 1
        sbx = FakeSandbox(f"sbx-{self._next_id}")
        # 刻意不设 sbx.template_id：真实 E2B 对象无此属性（见 FakeSandbox docstring）。
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

    def run_command(self, sandbox, command, timeout=120):
        # 镜像 run_code 的健康/失败注入语义，让健康探针走 shell 端点路径也可测
        sid = getattr(sandbox, "sandbox_id", str(sandbox))
        if sid in self._unhealthy_ids:
            return CodeResult(success=False, error="unhealthy sandbox")
        if not self._run_code_success:
            return CodeResult(success=False, error=self._run_code_error or "probe failed")
        return CodeResult(stdout="ok\n", success=True)

    def clean_workspace(self, sandbox, workdir="/workspace"):
        sid = getattr(sandbox, "sandbox_id", str(sandbox))
        self._cleaned.append(sid)
        return sid not in self._clean_fail_ids

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


def test_reap_keeps_healthy(monkeypatch):
    """reap 保留未过期且健康的沙箱。"""
    mgr = FakeManager()
    pool = _make_pool(mgr, ttl_seconds=600, idle_seconds=300)
    sbx = pool.acquire("tpl-a")
    pool.release(sbx, reusable=True)

    # 隔离真实 e2b：不依赖服务端列表（否则 FakeManager 假沙箱会被当幽灵清掉）
    monkeypatch.setattr(pool, "_server_alive_ids", lambda: None)
    result = pool.reap()
    assert result["killed"] == 0
    assert result["kept"] == 1
    assert sbx.sandbox_id not in mgr._killed
    print("  ✅ reap 保留健康未过期沙箱")


def test_reap_cleans_ghost(monkeypatch):
    """reap 清理幽灵：池里有但服务端已消失的 idle 条目被剔除（无需远端 kill）。"""
    mgr = FakeManager()
    pool = _make_pool(mgr, ttl_seconds=600, idle_seconds=300)
    sbx = pool.acquire("tpl-a")
    pool.release(sbx, reusable=True)
    # 模拟服务端权威列表【不含】该沙箱（它被服务端提前回收了）→ 幽灵
    monkeypatch.setattr(pool, "_server_alive_ids", lambda: set())
    result = pool.reap()
    assert result["ghosts"] == 1, f"应识别1个幽灵, got {result}"
    assert result["kept"] == 0
    # 幽灵无需远端 kill（已不存在），不应进 _killed
    assert sbx.sandbox_id not in mgr._killed
    # 池内已剔除
    with pool._lock:
        assert not pool._pool.get("tpl-a")
    print("  ✅ reap 清理幽灵 idle 条目（服务端已消失，不误 kill）")


def test_reap_skips_ghost_when_server_list_unavailable(monkeypatch):
    """服务端列表拉取失败(None) → 跳过幽灵清理，保留沙箱（避免误杀）。"""
    mgr = FakeManager()
    pool = _make_pool(mgr, ttl_seconds=600, idle_seconds=300)
    sbx = pool.acquire("tpl-a")
    pool.release(sbx, reusable=True)
    monkeypatch.setattr(pool, "_server_alive_ids", lambda: None)  # 拉取失败
    result = pool.reap()
    assert result["ghosts"] == 0 and result["kept"] == 1
    print("  ✅ 服务端列表不可用时跳过幽灵清理(不误杀)")


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


def test_release_cleans_workspace_before_pooling():
    """[#2 修复] 归还可复用沙箱时先清理 workspace 再回池(防跨任务污染)。"""
    mgr = FakeManager()
    pool = _make_pool(mgr)
    sb = pool.acquire(project_id="p", task_id="t1")
    pool.release(sb, reusable=True)
    assert sb.sandbox_id in mgr._cleaned, "归还回池前未清理 workspace"
    assert pool.stats()["total_idle"] == 1, "清理成功应回池"
    print("  ✅ [#2] 归还回池前清理 workspace")


def test_release_clean_failure_kills_instead_of_pooling():
    """[#2 修复] 清理失败的沙箱不回池，直接 kill(绝不留脏沙箱给下个任务)。"""
    mgr = FakeManager()
    pool = _make_pool(mgr)
    sb = pool.acquire(project_id="p", task_id="t1")
    mgr._clean_fail_ids.add(sb.sandbox_id)  # 注入清理失败
    pool.release(sb, reusable=True)
    assert sb.sandbox_id in mgr._killed, "清理失败的沙箱应被 kill"
    assert pool.stats()["total_idle"] == 0, "清理失败不应回池"
    print("  ✅ [#2] 清理失败的沙箱不回池而是 kill")


def test_acquire_cleans_reused_sandbox():
    """[#2 修复] 复用沙箱取用前再清一次 workspace(双保险)。"""
    mgr = FakeManager()
    pool = _make_pool(mgr)
    sb = pool.acquire(project_id="p", task_id="t1")
    pool.release(sb, reusable=True)
    mgr._cleaned.clear()
    sb2 = pool.acquire(project_id="p2", task_id="t2")  # 复用
    assert sb2.sandbox_id == sb.sandbox_id, "应复用同一沙箱"
    assert sb2.sandbox_id in mgr._cleaned, "复用取用前未再次清理"
    print("  ✅ [#2] 复用沙箱取用前再次清理(双保险)")


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


def test_forget_borrowed_decrements_counter():
    """[回归] forget 一个借出态沙箱：borrowed 计数回退，防 kill_by_task 后账本漂移泄漏。"""
    mgr = FakeManager()
    pool = _make_pool(mgr)
    sb = pool.acquire(project_id="p", task_id="t1")
    assert pool.stats()["borrowed"] == 1
    # 模拟 cancel_task / kill_by_task 外部直接 kill 了它，再通知池 forget
    pool.forget(sb.sandbox_id)
    assert pool.stats()["borrowed"] == 0, "forget 后 borrowed 应回退到 0"
    assert sb.sandbox_id not in pool._created_at, "forget 应清 created_at"
    print("  ✅ [回归] forget 借出态沙箱回退 borrowed 计数")


def test_release_bucket_key_survives_missing_template_attr():
    """[回归·桶键退化] 真实 E2B Sandbox 对象无 template_id 属性。

    锁行为：acquire(tpl) 后 release 必须把沙箱归还到与 acquire【相同】的 (tpl) 桶，
    而不是退化到空桶 ""；随后同 template acquire 能复用同一沙箱（不新建）。
    这是生产 bug 的直接复现——旧 release 用 getattr(sandbox,"template_id") 恒得 None
    → 归还进空桶 → 复用失效。前提断言假沙箱确实无该属性，防再被 mock 掩盖。
    """
    mgr = FakeManager()
    pool = _make_pool(mgr)
    sbx = pool.acquire("tpl-x")
    assert not hasattr(sbx, "template_id"), (
        "测试前提：假沙箱须无 template_id 属性（模拟真实 E2B），否则会掩盖桶键退化 bug"
    )
    pool.release(sbx, reusable=True)

    st = pool.stats()
    assert st["idle_by_template"].get("tpl-x") == 1, (
        f"应归还到 tpl-x 桶，实际: {st['idle_by_template']}"
    )
    assert "" not in st["idle_by_template"], "退化到空桶 '' 即为 bug 复现"

    # 同 template 再 acquire 必须复用同一沙箱（证明桶键两侧一致）
    sbx2 = pool.acquire("tpl-x")
    assert sbx2.sandbox_id == sbx.sandbox_id, "同 template 应复用同一沙箱"
    assert len(mgr._created) == 1, "复用不应新建"
    print("  ✅ [回归] release 桶键在沙箱无 template_id 属性时仍与 acquire 一致")


def test_forget_idle_removes_from_pool():
    """[回归] forget 一个 idle 池内沙箱：从池桶移除死引用，不误减 borrowed。"""
    mgr = FakeManager()
    pool = _make_pool(mgr)
    sb = pool.acquire("tpl-a")
    pool.release(sb, reusable=True)  # 进 idle 池
    assert pool.stats()["total_idle"] == 1
    assert pool.stats()["borrowed"] == 0
    # 外部 kill 了这个 idle 沙箱，通知池 forget
    hit = pool.forget(sb.sandbox_id)
    assert hit, "应命中 idle 池内沙箱"
    assert pool.stats()["total_idle"] == 0, "forget 应从 idle 池移除"
    assert pool.stats()["borrowed"] == 0, "idle 沙箱 forget 不应动 borrowed"
    print("  ✅ [回归] forget idle 沙箱移除死引用、不误减 borrowed")


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
        test_release_cleans_workspace_before_pooling,
        test_release_clean_failure_kills_instead_of_pooling,
        test_acquire_cleans_reused_sandbox,
        test_forget_borrowed_decrements_counter,
        test_release_bucket_key_survives_missing_template_attr,
        test_forget_idle_removes_from_pool,
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
