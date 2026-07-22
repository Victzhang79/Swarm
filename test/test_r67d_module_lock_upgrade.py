"""round67d 死因治本：ModuleLock 窄组合锁→宽组合锁升级【自死锁】。

死因（task b54d4bd3）：upgrade_module_lock 的 else 分支（旧锁非 project_wide）用
`new_lock.acquire()` acquire-before-release —— new_lock 用全新 uuid token 去 acquire
【旧组合锁仍持有的重叠子键】(root/sql…)，撞上同 key 全局非重入 threading.Lock +
Redis SET NX 同键 → 100% 冲突 → 300s fail-loud（"其它任务持有"实为同任务旧锁自撞）。

治本：MultiModuleLock.rebalance_to —— kept=new∩old 复用旧子锁(不重新 acquire=消除自撞)，
added=new−old 增量 acquire，removed=old−new 成功后释放；added 失败回滚本次、旧集原样、fail-loud。

红灯先行：修复前 test_1/2/3/4 因自撞抛 ModuleLockUpgradeConflict 而失败。
"""
from __future__ import annotations

import uuid

import pytest

import swarm.infra.redis_client as rc


def _pid() -> str:
    return f"_r67d_{uuid.uuid4().hex[:8]}"


def _plan(*module_files: str) -> dict:
    """把 "root/x" 形式的写集路径打包成 plan。"""
    return {"subtasks": [{"scope": {"writable": list(module_files)}}]}


def test_upgrade_narrow_to_wide_no_self_deadlock(monkeypatch):
    """核心红灯：持有 {root,sql} 组合锁 → 升级加 common，绝不自撞旧锁的 root/sql。"""
    monkeypatch.setattr(rc, "get_redis", lambda: None)
    pid = _pid()
    old = rc.MultiModuleLock(pid, ["root", "sql"])
    assert old.acquire() is True

    new = rc.upgrade_module_lock(old, pid, _plan("root/x", "sql/y", "common/z"))
    assert new.module_key == "common+root+sql", "目标=写集全部顶层键（排序去重）"
    # 三键都在持有集：root/sql 复用旧子锁、common 新增
    assert new.module_key.split("+") == ["common", "root", "sql"]
    new.release()


def test_upgrade_shrink_releases_removed(monkeypatch):
    """写集缩小：{root,sql,common} → {root,common}，sql 必须被释放（可被新实例获取）。"""
    monkeypatch.setattr(rc, "get_redis", lambda: None)
    pid = _pid()
    old = rc.MultiModuleLock(pid, ["root", "sql", "common"])
    assert old.acquire() is True

    new = rc.upgrade_module_lock(old, pid, _plan("root/x", "common/z"))
    assert new.module_key == "common+root"
    # sql 已释放 → 新实例可拿到
    s = rc.ModuleLock(pid, "sql")
    assert s.acquire() is True, "removed 键 sql 升级后应被释放"
    s.release()
    new.release()


def test_upgrade_added_conflict_rolls_back_keeps_old(monkeypatch):
    """新增键被【真·外部持有者】占住 → fail-loud（抛 Conflict），且旧集全键原样保留不丢。"""
    monkeypatch.setattr(rc, "get_redis", lambda: None)
    pid = _pid()
    old = rc.MultiModuleLock(pid, ["root", "sql"])
    assert old.acquire() is True
    # 真外部持有者占住新增键 common
    blocker = rc.ModuleLock(pid, "common")
    assert blocker.acquire() is True

    with pytest.raises(rc.ModuleLockUpgradeConflict):
        rc.upgrade_module_lock(old, pid, _plan("root/x", "sql/y", "common/z"))

    # 旧集 root/sql 仍被 old 持有（回滚绝不误放旧集）——新实例拿不到
    assert rc.ModuleLock(pid, "root").acquire() is False, "回滚不得丢 root"
    assert rc.ModuleLock(pid, "sql").acquire() is False, "回滚不得丢 sql"
    blocker.release()
    old.release()


def test_upgraded_lock_renew_and_release_ok(monkeypatch):
    """升级后的锁 renew/release 正常消费；全键释放后可重新获取（无孤儿死锁）。"""
    monkeypatch.setattr(rc, "get_redis", lambda: None)
    pid = _pid()
    old = rc.MultiModuleLock(pid, ["root", "sql"])
    assert old.acquire() is True
    new = rc.upgrade_module_lock(old, pid, _plan("root/x", "sql/y", "common/z"))

    assert new.renew() is True, "进程内锁 renew no-op True"
    new.release()
    for k in ("root", "sql", "common"):
        m = rc.ModuleLock(pid, k)
        assert m.acquire() is True, f"释放后 {k} 应可再获取（未孤儿死锁）"
        m.release()


def test_first_upgrade_default_to_combined_still_works(monkeypatch):
    """回归护栏：default(project_wide) → 组合锁 首升走降级分支，不受本次改动影响。"""
    monkeypatch.setattr(rc, "get_redis", lambda: None)
    pid = _pid()
    old = rc.ModuleLock(pid, "default")  # project_wide
    assert old.acquire() is True
    new = rc.upgrade_module_lock(old, pid, _plan("root/x", "sql/y"))
    assert new.module_key == "root+sql"
    new.release()


def test_rebalance_reports_only_truly_failed_added_key(monkeypatch):
    """复核改1：多新增键部分冲突时，异常只报【真失败键】，绝不误报 acquire-then-rollback
    的其它新增键为"被占"（防复发误导文案——正是本次治本要消除的那类）。"""
    monkeypatch.setattr(rc, "get_redis", lambda: None)
    pid = _pid()
    old = rc.MultiModuleLock(pid, ["root"])
    assert old.acquire() is True
    # 外部真持有者占住 "zeta"（排序在 alpha 之后）——rebalance 会先成功拿 alpha 再撞 zeta 回滚 alpha
    blocker = rc.ModuleLock(pid, "zeta")
    assert blocker.acquire() is True

    with pytest.raises(rc.ModuleLockUpgradeConflict) as ei:
        rc.upgrade_module_lock(old, pid, _plan("root/x", "alpha/y", "zeta/z"))
    msg = str(ei.value)
    # 归因短语"新增模块键 X 被其它任务持有"必须只指真失败键 zeta——alpha 虽出现在目标全集
    # 展示(root → alpha+root+zeta)属正常，但绝不能进【归因】(那才是误导文案复发)。
    import re
    blame = re.search(r"新增模块键 (\S+) 被其它任务持有", msg)
    assert blame is not None, f"消息缺归因短语: {msg}"
    assert blame.group(1) == "zeta", f"归因应只指真失败键 zeta，实为 {blame.group(1)}（误导文案复发）"

    blocker.release()
    old.release()
    # alpha 已被回滚释放 → 可被新实例获取（回滚干净无泄漏）
    a = rc.ModuleLock(pid, "alpha")
    assert a.acquire() is True, "回滚应真释放 alpha（无孤儿）"
    a.release()


def test_rebalance_warns_on_unheld_kept_sublock(monkeypatch, caplog):
    """复核改3：复用 _held=False 的 kept 子锁时留 WARNING（split-brain 前兆可观测），不静默复用假持有。"""
    import logging

    monkeypatch.setattr(rc, "get_redis", lambda: None)
    pid = _pid()
    old = rc.MultiModuleLock(pid, ["root", "sql"])
    assert old.acquire() is True
    # 人为制造 kept 子锁失锁（模拟未经 renew 校验的失效）
    for lk in old._locks:
        if lk.module_key == "root":
            lk._held = False

    with caplog.at_level(logging.WARNING):
        new = rc.upgrade_module_lock(old, pid, _plan("root/x", "sql/y", "common/z"))
    assert any(
        "_held=False" in r.getMessage() or "split-brain" in r.getMessage()
        for r in caplog.records
    ), "复用失锁 kept 子锁应留 WARNING（未来违约调用方可观测）"
    new.release()


def test_second_and_third_upgrade_chain_no_deadlock(monkeypatch):
    """多次升级链（plan 节点执行 ≥3 次写集递增）：每次都走 rebalance，绝不自撞。"""
    monkeypatch.setattr(rc, "get_redis", lambda: None)
    pid = _pid()
    lock = rc.ModuleLock(pid, "default")
    assert lock.acquire() is True
    lock = rc.upgrade_module_lock(lock, pid, _plan("root/a"))          # default→{root}
    assert lock.module_key == "root"
    lock = rc.upgrade_module_lock(lock, pid, _plan("root/a", "sql/b"))  # +sql
    assert lock.module_key == "root+sql"
    lock = rc.upgrade_module_lock(lock, pid, _plan("root/a", "sql/b", "common/c"))  # +common
    assert lock.module_key == "common+root+sql"
    lock.release()
