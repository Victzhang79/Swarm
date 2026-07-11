"""主题I H-3（外部深审 HIGH）：default 与模块锁无层级互斥 → 同一 git 树双写。

病根：`default`(整项目宽锁)与模块键(root/src/…)是平级不同 Redis key，SET NX 只让同名键互斥
→ `default` 只排除另一个 `default`，绝不排除模块 holder；升级后释放 default，apply 直写入口
又取 `default` → 与正持模块锁写树的 runner 并发 = 双写污染。
治：建成【项目读写门】——default=写者(排他)，模块键=读者(共享，不同模块并行=E3 本意)。
写者↔读者互斥、多读者共存。进程内层权威（单进程即完全正确），Redis Lua 层叠加跨进程。

本测试主验【进程内权威层】（Redis 关，deploy 默认态）的层级不变量——default↔模块彻底互斥、
模块间共存；另用忠实的 fake redis 验 Redis 跨进程门层的角色分派/冲突回滚接线。
"""
from __future__ import annotations

import pytest

import swarm.infra.redis_client as rc
from swarm.infra.redis_client import ModuleLock, MultiModuleLock, _ProjectGate


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    rc._reset_project_gates()
    rc._LOCAL_LOCKS.clear()
    # 默认 Redis 关：走进程内权威门（deploy 默认态、且此层不变量确定可测）。
    monkeypatch.setattr(rc, "get_redis", lambda: None)
    yield
    rc._reset_project_gates()
    rc._LOCAL_LOCKS.clear()


# ── 进程内权威层：default↔模块 层级互斥（核心 RED 面） ──────────────────────────

def test_h3_default_excludes_module_holder():
    """模块 holder 在场 → 整项目宽锁(default)必须拿不到（旧实现=拿得到=双写）。"""
    mod = ModuleLock("p1", "src")
    assert mod.acquire() is True
    wide = ModuleLock("p1", "default")
    assert wide.acquire() is False, "模块写者在场，default 整项目宽锁绝不可入"
    mod.release()


def test_h3_module_excludes_default_holder():
    """default(整项目写者)在场 → 任何模块锁必须拿不到（旧实现=拿得到=双写）。"""
    wide = ModuleLock("p1", "default")
    assert wide.acquire() is True
    mod = ModuleLock("p1", "src")
    assert mod.acquire() is False, "整项目写者在场，模块读者绝不可入"
    wide.release()


def test_h3_two_modules_coexist():
    """不同模块=读者共享，必须并行（E3 本意，不能被门误杀）。"""
    a = ModuleLock("p1", "src")
    b = ModuleLock("p1", "web")
    assert a.acquire() is True
    assert b.acquire() is True, "不同模块读者必须共存（E3 细粒度并行）"
    a.release()
    b.release()


def test_h3_same_module_still_exclusive():
    a = ModuleLock("p1", "src")
    b = ModuleLock("p1", "src")
    assert a.acquire() is True
    assert b.acquire() is False, "同模块仍靠各自 key 互斥"
    a.release()


def test_h3_default_excludes_default():
    a = ModuleLock("p1", "default")
    b = ModuleLock("p1", "default")
    assert a.acquire() is True
    assert b.acquire() is False, "两个整项目写者互斥"
    a.release()


def test_h3_release_default_lets_module_in():
    wide = ModuleLock("p1", "default")
    assert wide.acquire() is True
    wide.release()
    mod = ModuleLock("p1", "src")
    assert mod.acquire() is True, "default 释放后模块可入（门槽已归还）"
    mod.release()


def test_h3_release_module_lets_default_in():
    mod = ModuleLock("p1", "src")
    assert mod.acquire() is True
    mod.release()
    wide = ModuleLock("p1", "default")
    assert wide.acquire() is True, "模块释放后 default 可入"
    wide.release()


def test_h3_different_projects_independent():
    """门按 project_id 隔离——A 项目的 default 不挡 B 项目的模块。"""
    a = ModuleLock("pA", "default")
    b = ModuleLock("pB", "src")
    assert a.acquire() is True
    assert b.acquire() is True, "跨项目门相互独立"
    a.release()
    b.release()


def test_h3_multimodule_readers_block_default_and_release():
    """MultiModuleLock=多读者组合；在场挡 default，全释放后 default 可入。"""
    combo = MultiModuleLock("p1", ["src", "web"])
    assert combo.acquire() is True
    wide = ModuleLock("p1", "default")
    assert wide.acquire() is False, "组合读者在场 → default 不可入"
    combo.release()
    assert wide.acquire() is True, "组合读者全释放 → 门槽清零，default 可入"
    wide.release()


def test_h3_multimodule_with_default_named_dir_still_acquirable():
    """对抗复核 F3：写集里真有名为 `default` 的顶层目录 → 组合锁含子键 "default"。子锁一律
    读者，绝不自撞（旧 role=字符串比对会把它当写者 → 与同组合内 src 读者写↔读对撞 → 永不可达）。"""
    combo = MultiModuleLock("p1", ["default", "src"])
    assert combo.acquire() is True, "含 default 命名目录的组合锁必须可获取（子锁全是读者）"
    combo.release()


# ── _ProjectGate 单元：读写门语义 ────────────────────────────────────────────

def test_h3_project_gate_rw_semantics():
    g = _ProjectGate()
    assert g.acquire_shared("r1") is True
    assert g.acquire_shared("r2") is True  # 多读者共存
    assert g.acquire_exclusive("w1") is False  # 读者在场，写者不可入
    g.release_shared("r1")
    assert g.acquire_exclusive("w1") is False  # 仍有一个读者
    g.release_shared("r2")
    assert g.acquire_exclusive("w1") is True  # 读者清零，写者可入
    assert g.acquire_shared("r3") is False  # 写者在场，读者不可入
    assert g.acquire_exclusive("w2") is False  # 写者排他
    g.release_exclusive("w2")  # F5：非属主 release 不清掉 w1 的写态
    assert g.acquire_shared("r3") is False, "非属主 release 无效，写者仍在"
    g.release_exclusive("w1")  # 属主 release 才清
    assert g.acquire_shared("r3") is True
    g.release_shared("r3")


# ── Redis 跨进程门层：忠实 fake redis 验角色分派 + 冲突回滚接线 ─────────────────

class _FakeRedis:
    """忠实实现门 Lua 用到的 hash + sorted-set + TIME 原子操作，按已知脚本分派 eval（KEYS[1]=hash，
    KEYS[2]=reader zset）。TIME 固定返回单调秒——本测试聚焦互斥语义，不触发 score 过期。"""

    def __init__(self):
        self.h: dict = {}   # hashkey -> {field: value}
        self.z: dict = {}   # zsetkey -> {member: score}
        self._now = 1000

    def _hget(self, k, f):
        return (self.h.get(k) or {}).get(f)

    def eval(self, script, numkeys, *keys_and_argv):
        hk = keys_and_argv[0]
        zk = keys_and_argv[1] if numkeys >= 2 else None
        argv = keys_and_argv[numkeys:]
        z = self.z.setdefault(zk, {}) if zk else {}
        if script is rc._PGATE_ACQ_SHARED_LUA:
            # ZREMRANGEBYSCORE 清过期（score<=now）
            for m in [m for m, s in z.items() if s <= self._now]:
                z.pop(m)
            if self._hget(hk, "w"):
                return 0
            z[argv[0]] = self._now + int(argv[1])
            return 1
        if script is rc._PGATE_ACQ_EXCL_LUA:
            for m in [m for m, s in z.items() if s <= self._now]:
                z.pop(m)
            if self._hget(hk, "w"):
                return 0
            if len(z) > 0:
                return 0
            self.h.setdefault(hk, {})["w"] = argv[0]
            return 1
        if script is rc._PGATE_REL_SHARED_LUA:
            z.pop(argv[0], None)
            return 1
        if script is rc._PGATE_REL_EXCL_LUA:
            if self._hget(hk, "w") == argv[0]:
                self.h.get(hk, {}).pop("w", None)
            return 1
        if script is rc._PGATE_RENEW_SHARED_LUA:
            if self._hget(hk, "w"):
                return 0
            z[argv[0]] = self._now + int(argv[1])
            return 1
        if script is rc._PGATE_RENEW_EXCL_LUA:
            return 1 if self._hget(hk, "w") == argv[0] else 0
        if script is rc._PGATE_DOWNGRADE_LUA:
            # argv[0]=old writer token, argv[1]=ttl, argv[2:]=reader tokens
            if self._hget(hk, "w") != argv[0]:
                return 0
            self.h.get(hk, {}).pop("w", None)
            for rt in argv[2:]:
                z[rt] = self._now + int(argv[1])
            return 1
        if script is rc._LOCK_RELEASE_LUA:
            return 1  # 模块 key（字符串锁）状态本 fake 不建模，release no-op
        raise AssertionError(f"未知脚本: {script[:40]}")

    def set(self, *a, **k):
        return True  # 模块 key SET NX：本测试聚焦门层，恒成功不干扰


def _readers(fake, pid):
    return fake.z.get(rc._pgate_readers_key(pid)) or {}


def test_h3_redis_gate_cross_process_exclusion(monkeypatch):
    """两把独立 ModuleLock（模拟跨进程各自 token）共享同一 fake redis 门态：
    模块读者持门 → default 写者经 Redis 门被拒。"""
    fake = _FakeRedis()
    monkeypatch.setattr(rc, "get_redis", lambda: fake)
    rc._reset_project_gates()

    mod = ModuleLock("p1", "src")
    assert mod.acquire() is True
    assert len(_readers(fake, "p1")) == 1, "读者 token 登记进 Redis zset"

    # 另一进程的 default：进程内门是新 registry 命不中冲突，靠 Redis 门层拒。
    rc._reset_project_gates()  # 模拟另一进程独立的进程内门
    wide = ModuleLock("p1", "default")
    assert wide.acquire() is False, "Redis 门读者在场 → 跨进程 default 写者被拒"
    # 关键：进程内门槽必须已回滚（无泄漏）——同进程再拿兼容读者应成功。
    mod2 = ModuleLock("p1", "web")
    assert mod2.acquire() is True, "Redis 冲突后进程内门槽已回滚，兼容读者仍可入"
    mod2.release()
    mod.release()
    assert len(_readers(fake, "p1")) == 0, "读者全释放，Redis zset 清空"


def test_h3_redis_reader_renew_fails_closed_if_writer_present(monkeypatch):
    """对抗复核 F1：reader renew 撞见 writer（门被跨进程夺走）→ 确认丢门，renew 返回 False。"""
    fake = _FakeRedis()
    monkeypatch.setattr(rc, "get_redis", lambda: fake)
    rc._reset_project_gates()
    mod = ModuleLock("p1", "src")
    assert mod.acquire() is True
    # 模拟：另一进程强行成为 writer（直接写 hash w）——本 reader 的 renew 必须 fail-closed。
    fake.h.setdefault(rc._pgate_key("p1"), {})["w"] = "other-proc-writer"
    assert mod.renew() is False, "reader 续期撞 writer → 判失锁（防双写）"
    assert mod._gate_redis_held is False, "确认丢门后清 _gate_redis_held"


def test_h3_redis_writer_renew_fails_closed_if_token_replaced(monkeypatch):
    """对抗复核 F1：writer renew 时 token 已被替换 → 确认丢门，renew 返回 False。"""
    fake = _FakeRedis()
    monkeypatch.setattr(rc, "get_redis", lambda: fake)
    rc._reset_project_gates()
    wide = ModuleLock("p1", "default")
    assert wide.acquire() is True
    fake.h[rc._pgate_key("p1")]["w"] = "someone-else"  # token 被夺
    assert wide.renew() is False, "writer token 被替换 → 判失锁"


def test_h3_redis_gate_renew_runs_even_if_key_redis_lost(monkeypatch):
    """对抗复核 F2：门 eval 成功但 key SET 抛异常(_redis_held=False, _gate_redis_held=True)时，
    renew 仍必须续门（不被 `if not _redis_held` 短路跳过）。"""
    fake = _FakeRedis()
    monkeypatch.setattr(rc, "get_redis", lambda: fake)
    rc._reset_project_gates()
    wide = ModuleLock("p1", "default")
    assert wide.acquire() is True
    # 人为制造 flag 分叉：key 层丢了 Redis，门层仍在。
    wide._redis_held = False
    assert wide._gate_redis_held is True
    # renew 应仍走门续期路径（token 匹配→True），不因 _redis_held=False 早退跳过门。
    assert wide.renew() is True
    # 若把门 token 夺走，renew 必须 fail-closed（证明门续期确实跑了）。
    fake.h[rc._pgate_key("p1")]["w"] = "hijacked"
    assert wide.renew() is False, "门续期确实在 _redis_held=False 下仍执行并 fail-closed"


def test_h3_redis_atomic_downgrade_writer_to_readers(monkeypatch):
    """对抗复核（升级自撞治本）：整项目写者→模块读者【原子降级】——一次 Lua 内删 writer + ZADD
    全部 reader token，门态绝不空窗。降级后 hash 无 writer、zset 有 N 个 reader。"""
    from swarm.infra.redis_client import upgrade_module_lock
    fake = _FakeRedis()
    monkeypatch.setattr(rc, "get_redis", lambda: fake)
    rc._reset_project_gates()
    wide = ModuleLock("p1", "default")
    assert wide.acquire() is True
    assert fake.h[rc._pgate_key("p1")]["w"] == wide.token, "写者已登记 Redis 门"
    plan = {"subtasks": [{"scope": {"writable": ["mod-a/x.py", "mod-b/y.py"], "create_files": []}}]}
    new_lock = upgrade_module_lock(wide, "p1", plan)
    assert new_lock.module_key == "mod-a+mod-b"
    assert not fake.h.get(rc._pgate_key("p1"), {}).get("w"), "降级后 writer 已清"
    assert len(_readers(fake, "p1")) == 2, "降级后两个模块读者登记进 zset"
    # 降级后另一进程的 default 写者被挡（读者在场）。
    rc._reset_project_gates()
    probe = ModuleLock("p1", "default")
    assert probe.acquire() is False, "降级后跨进程 default 写者被读者挡住"
    new_lock.release()
    assert len(_readers(fake, "p1")) == 0, "组合锁释放 → zset 清空"


def test_h3_redis_downgrade_hard_fails_if_writer_stolen(monkeypatch):
    """对抗复核 2nd（CONFIRMED CRITICAL）：降级时 Redis 写者 token 已被跨进程夺走（TTL 失效期）→
    _PGATE_DOWNGRADE_LUA 返回 0 → acquire_by_downgrade 必须【硬失败】(绝不视同成功当读者写树)，
    upgrade 抛 ModuleLockUpgradeConflict，旧写者进程内门原样保留（fail-loud 重试）。"""
    from swarm.infra.redis_client import ModuleLockUpgradeConflict, upgrade_module_lock
    fake = _FakeRedis()
    monkeypatch.setattr(rc, "get_redis", lambda: fake)
    rc._reset_project_gates()
    wide = ModuleLock("p1", "default")
    assert wide.acquire() is True
    # 模拟跨进程写者夺锁：Redis hash w 变成别人的 token（本进程 TTL 失效期被抢）。
    fake.h[rc._pgate_key("p1")]["w"] = "other-process-writer"
    plan = {"subtasks": [{"scope": {"writable": ["mod-a/x.py"], "create_files": []}}]}
    with pytest.raises(ModuleLockUpgradeConflict):
        upgrade_module_lock(wide, "p1", plan)
    # 旧写者进程内门原样保留（互斥面不塌）。
    assert wide._held is True and wide._gate_held is True
    # Redis 侧仍是他进程的 writer（我们绝没误改）。
    assert fake.h[rc._pgate_key("p1")]["w"] == "other-process-writer"
    assert len(_readers(fake, "p1")) == 0, "硬失败 → 绝不登记本进程读者"


if __name__ == "__main__":
    print("run via pytest")
