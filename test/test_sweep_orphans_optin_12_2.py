"""12.2 + A1批3 回归测试：启动孤儿沙箱清扫的实例隔离。

演进历史：
- 12.2（A 止血）：opt-in 开关 sweep_orphans_on_startup，共享集群设 False 跳过整个清扫。
- A1 批3（根治）：沙箱打 swarm_instance 标签，清扫按本实例过滤——只 kill 本实例
  残留，绝不误杀别副本。开关语义升级为"无标签沙箱是否清"（有标签的按实例归属判定）。

本测试用 mock 隔离，不连真 CubeSandbox。
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import swarm.api.app  # noqa: F401

app_mod = sys.modules["swarm.api.app"]


def _run_sweep(flag: bool, server_list: list[dict]):
    """在 sweep_orphans_on_startup=flag + 给定 server_list 下执行清扫，
    返回 (fetch 是否被调用, 被 kill 的 sandbox id 列表)。"""
    fake_cfg = MagicMock()
    fake_cfg.sandbox.sweep_orphans_on_startup = flag
    killed: list[str] = []
    mgr = MagicMock()
    mgr.kill.side_effect = lambda sid: killed.append(sid)
    with patch("swarm.config.settings.get_config", return_value=fake_cfg), \
         patch("swarm.worker.sandbox.get_instance_id", return_value="me"), \
         patch.object(app_mod, "_fetch_sandbox_list_from_server", return_value=server_list) as fetch_mock, \
         patch.object(app_mod, "_get_sandbox_manager", return_value=mgr):
        app_mod._sweep_startup_orphans()
    return fetch_mock.called, killed


def _sb(sid, instance=None):
    return {"id": sid, "metadata": ({"swarm_instance": instance} if instance else {})}


def test_kills_only_own_instance_regardless_of_switch():
    """A1批3 核心：清扫只 kill 本实例标签的沙箱，别副本绝不动（开关 on）。"""
    lst = [_sb("mine", "me"), _sb("other", "replica-2")]
    called, killed = _run_sweep(True, lst)
    assert called is True
    assert killed == ["mine"], "只清本实例，不动别副本"


def test_switch_off_keeps_untagged_but_still_cleans_own():
    """开关 off（共享集群保守）：无标签沙箱保留，但本实例标签的仍清。"""
    lst = [_sb("untagged"), _sb("mine", "me"), _sb("other", "replica-2")]
    called, killed = _run_sweep(False, lst)
    assert called is True
    assert killed == ["mine"], "off 时只清本实例标签，无标签和别副本都保留"


def test_switch_on_cleans_untagged_too():
    """开关 on（单机默认）：无标签沙箱也清（向后兼容旧的全清扫语义）。"""
    lst = [_sb("untagged"), _sb("mine", "me")]
    called, killed = _run_sweep(True, lst)
    assert called is True
    assert set(killed) == {"untagged", "mine"}


def test_never_kills_other_replica():
    """关键安全保证：别副本【有标签】沙箱在任何开关下都不被清。"""
    lst = [_sb("o1", "replica-2"), _sb("o2", "replica-3")]
    for flag in (True, False):
        _, killed = _run_sweep(flag, lst)
        assert killed == [], f"开关={flag} 时也绝不动别副本"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}: {e}")
    print(f"\n=== 12.2+A1批3 sweep 实例隔离: {len(fns) - failed}/{len(fns)} passed ===")
    sys.exit(1 if failed else 0)

