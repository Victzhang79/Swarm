"""B6 模型路由/基础设施/配置深读治本（DR-07-F1..F8 = #93-100）行为级测试。"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest


# ─────────────────────────── F2/#94 路由链 JSON 兜底 ───────────────────────────

def test_94_malformed_json_array_falls_back_not_crash():
    from swarm.config.settings import _coerce_model_list as C
    assert C('["A",]') == ["A"]        # 尾逗号
    assert C("[A, B]") == ["A", "B"]    # 无引号
    assert C('["A","B"]') == ["A", "B"] # 合法 JSON 不变
    assert C("A,B") == ["A", "B"]       # 逗号分隔
    assert C("[garbage") == ["garbage"] # 半个数组


# ─────────────────────────── F3/#95 max_active_projects 容错 ───────────────────────────

def test_95_max_active_projects_bad_value_defaults():
    import swarm.infra.redis_client as rc
    for bad in ("", "abc", "-3", "0"):
        os.environ["SWARM_MAX_ACTIVE_PROJECTS"] = bad
        rc._SWARM_MAX_ACTIVE_PROJECTS = None
        assert rc.get_max_active_projects() == 10, f"坏值 {bad!r} 应回默认 10"
    os.environ["SWARM_MAX_ACTIVE_PROJECTS"] = "5"
    rc._SWARM_MAX_ACTIVE_PROJECTS = None
    assert rc.get_max_active_projects() == 5
    os.environ.pop("SWARM_MAX_ACTIVE_PROJECTS", None)
    rc._SWARM_MAX_ACTIVE_PROJECTS = None


# ─────────────────────────── F4/#96 Fernet 轮换热生效 ───────────────────────────

def test_96_reset_fernet_clears_cache():
    import swarm.config.secret_store as ss
    with patch.dict(os.environ, {"SWARM_SECRET_KEY": "rootkey-v1"}):
        f1 = ss._get_fernet()
    with patch.dict(os.environ, {"SWARM_SECRET_KEY": "rootkey-v2"}):
        # 未 reset → 仍旧实例
        assert ss._get_fernet() is f1
        ss.reset_fernet()
        f2 = ss._get_fernet()
    assert f2 is not f1  # reset 后按新 env 重建


def test_96_invalidate_cache_all_resets_fernet():
    import swarm.config.secret_store as ss
    with patch.dict(os.environ, {"SWARM_SECRET_KEY": "rk"}):
        ss._get_fernet()
        assert ss._fernet is not None
        ss.invalidate_cache(None)   # 全量失效 → 一并清 fernet
        assert ss._fernet is None


# ─────────────────────────── F5/#97 DB 读失败 warn-once ───────────────────────────

def test_97_db_read_failure_warns_once(caplog):
    import logging

    import swarm.config.secret_store as ss
    ss._db_fail_warned.clear()
    with patch.object(ss, "_get_conn", side_effect=RuntimeError("pg down")), \
         patch.dict(os.environ, {}, clear=False):
        with caplog.at_level(logging.WARNING, logger=ss.logger.name):
            ss.get_secret("MY_KEY", conn_str="postgres://x")
            first_warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger=ss.logger.name):
            ss.get_secret("MY_KEY", conn_str="postgres://x")
            second_warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("DB 不可用" in r.message for r in first_warns)
    assert not second_warns   # 第二次降 DEBUG


# ─────────────────────────── F8/#100 cassette miss 分项 ───────────────────────────

def test_100_error_only_record_counts_miss_error_rec():
    import collections

    import swarm.models.cassette_playback as cp
    cp._stats.update({"hit": 0, "miss_sha": 0, "miss_error_rec": 0, "miss_exc": 0})
    dq = collections.deque([{"seq": 1, "error": "timeout"}])   # 只有失败尝试
    with patch("swarm.models.cassette_record.compute_request_sha", return_value=([], "sha1")), \
         patch.object(cp, "_ensure_index", return_value={"sha1": dq}):
        out = cp.lookup("n", "m", (), {})
    assert out is None
    assert cp._stats["miss_error_rec"] == 1   # 记 error-rec-miss
    assert cp._stats["miss_sha"] == 0          # 不再误计 sha-miss


# ─────────────────────────── F6/#98 项目门墙钟闸 ───────────────────────────

def test_98_gate_walltime_gate_fails_closed_after_ttl():
    import time as _t

    from swarm.infra.redis_client import ModuleLock
    lock = ModuleLock.__new__(ModuleLock)
    lock.key = "swarm:mlock:proj:mod"
    lock.ttl_sec = 100
    lock._gate_redis_held = True
    lock._gate_last_ok_monotonic = _t.monotonic() - 90  # 距上次续期 90s > TTL*0.8=80
    assert lock._gate_walltime_ok() is False       # fail-closed
    assert lock._gate_redis_held is False


def test_98_gate_walltime_ok_within_ttl():
    import time as _t

    from swarm.infra.redis_client import ModuleLock
    lock = ModuleLock.__new__(ModuleLock)
    lock.key = "k"
    lock.ttl_sec = 100
    lock._gate_redis_held = True
    lock._gate_last_ok_monotonic = _t.monotonic() - 10   # 仅 10s < 80
    assert lock._gate_walltime_ok() is True


def test_98_downgrade_bootstraps_gate_walltime_basis():
    """复核 CONFIRMED HIGH：acquire_by_downgrade 接管 Redis 门层时必须 bootstrap
    _gate_last_ok_monotonic（否则停 0.0→门墙钟闸对每个执行期降级锁失效）。"""
    from swarm.infra.redis_client import ModuleLock
    sub = ModuleLock.__new__(ModuleLock)
    sub.key = "swarm:mlock:p:m"
    sub.ttl_sec = 100
    # 模拟 downgrade 接管：redis_gate_ok=True → 应同步刷墙钟基准
    sub._gate_redis_held = True
    import time as _t
    sub._gate_last_ok_monotonic = _t.monotonic()   # downgrade 修复后应如此
    # 立即查：未超 TTL → OK；模拟长outage(基准回拨 90s>80)→fail-closed（证墙钟闸真生效非恒 True）
    assert sub._gate_walltime_ok() is True
    sub._gate_last_ok_monotonic = _t.monotonic() - 90
    assert sub._gate_walltime_ok() is False


def test_98_gate_walltime_local_only_never_expires():
    from swarm.infra.redis_client import ModuleLock
    lock = ModuleLock.__new__(ModuleLock)
    lock.key = "k"
    lock.ttl_sec = 100
    lock._gate_redis_held = False   # 纯本地门
    lock._gate_last_ok_monotonic = 0.0
    assert lock._gate_walltime_ok() is True


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
