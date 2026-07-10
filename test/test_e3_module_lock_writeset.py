"""E3（登记册 §六，发版前回查补漏）：模块锁纸面互斥治本行为锁。

旧两处纸面互斥：①锁 key 只取 paths[0] 顶层目录——计划写 x+y 只锁 x，另一任务锁 y
后双方在对方没锁的那半并发写同一 git 树；②升级失败保留旧锁照跑——旧 "default" 与
他人模块键不同串=零互斥。治：锁全部写集（组合锁 all-or-nothing 有序获取）+ 升级
冲突 fail-loud（调用方有界等待，绝不静默照跑）。
"""

from __future__ import annotations

import pytest

from swarm.infra.redis_client import (
    ModuleLock,
    ModuleLockUpgradeConflict,
    MultiModuleLock,
    module_keys_from_plan,
    upgrade_module_lock,
)


def _plan(*paths):
    return {"subtasks": [
        {"scope": {"writable": list(paths), "create_files": []}}]}


# ─────────────── 写集键派生 ───────────────


def test_e3_keys_cover_full_writeset():
    keys = module_keys_from_plan(_plan("mod-x/src/A.java", "mod-y/pom.xml",
                                       "mod-x/src/B.java"))
    assert keys == ["mod-x", "mod-y"], (
        "旧实现只取 paths[0] 顶层目录——写 x+y 只锁 x（纸面互斥）")


def test_e3_keys_include_create_files_and_fallbacks():
    plan = {"subtasks": [{"scope": {"writable": [], "create_files": ["new-mod/pom.xml"]}}]}
    assert module_keys_from_plan(plan) == ["new-mod"], "create_files 也是写集"
    assert module_keys_from_plan({"subtasks": [{"scope": {"writable": ["README.md"]}}]}) \
        == ["root"], "根文件归 root 桶（旧语义保留）"
    assert module_keys_from_plan(None) == ["default"]
    assert module_keys_from_plan({"subtasks": []}) == ["default"]


# ─────────────── 组合锁互斥（内存兜底路径，无需 Redis） ───────────────


def test_e3_overlapping_writesets_mutually_exclude(monkeypatch):
    monkeypatch.setattr("swarm.infra.redis_client.get_redis", lambda: None)
    a = MultiModuleLock("proj-1", ["mod-x", "mod-y"])
    b = MultiModuleLock("proj-1", ["mod-y", "mod-z"])
    assert a.acquire() is True
    assert b.acquire() is False, (
        "写集重叠（都写 mod-y）必须互斥——旧单键 'mod-x' vs 'mod-y' 字符串不等=零互斥")
    a.release()
    assert b.acquire() is True, "对方释放后可获取（排队语义成立）"
    b.release()


def test_e3_all_or_nothing_rollback(monkeypatch):
    monkeypatch.setattr("swarm.infra.redis_client.get_redis", lambda: None)
    holder = ModuleLock("proj-2", "mod-y")
    assert holder.acquire()
    c = MultiModuleLock("proj-2", ["mod-x", "mod-y"])
    assert c.acquire() is False, "任一键被占=整体失败"
    # 回滚验证：mod-x 必须已被释放（否则半持死锁面）
    probe = ModuleLock("proj-2", "mod-x")
    assert probe.acquire() is True, "组合锁失败后已获取部分必须回滚释放"
    probe.release()
    holder.release()


def test_e3_disjoint_writesets_run_in_parallel(monkeypatch):
    monkeypatch.setattr("swarm.infra.redis_client.get_redis", lambda: None)
    a = MultiModuleLock("proj-3", ["mod-x"])
    b = MultiModuleLock("proj-3", ["mod-y"])
    assert a.acquire() and b.acquire(), "不相交写集照常并行（E3 不牺牲并行度）"
    a.release(); b.release()


# ─────────────── 升级冲突 fail-loud ───────────────


def test_e3_upgrade_conflict_raises_not_silent_keep(monkeypatch):
    monkeypatch.setattr("swarm.infra.redis_client.get_redis", lambda: None)
    holder = ModuleLock("proj-4", "mod-y")
    assert holder.acquire()
    task_lock = ModuleLock("proj-4", "default")
    assert task_lock.acquire()
    with pytest.raises(ModuleLockUpgradeConflict):
        upgrade_module_lock(task_lock, "proj-4", _plan("mod-y/src/A.java"))
    # 冲突时原锁必须仍被持有（调用方等待期间互斥面不塌）
    probe = ModuleLock("proj-4", "default")
    assert probe.acquire() is False, "升级冲突后原锁不得被误释放"
    holder.release(); task_lock.release()


def test_e3_upgrade_success_swaps_to_writeset_lock(monkeypatch):
    monkeypatch.setattr("swarm.infra.redis_client.get_redis", lambda: None)
    task_lock = ModuleLock("proj-5", "default")
    assert task_lock.acquire()
    new_lock = upgrade_module_lock(task_lock, "proj-5",
                                   _plan("mod-x/src/A.java", "mod-y/pom.xml"))
    assert new_lock.module_key == "mod-x+mod-y"
    assert new_lock.renew() is True, "组合锁 renew 接口与单锁同语义（runner 搭车续期）"
    # 旧 default 已释放
    probe = ModuleLock("proj-5", "default")
    assert probe.acquire() is True
    probe.release(); new_lock.release()
