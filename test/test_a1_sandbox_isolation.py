"""A1 批3 单测：沙箱启动清扫的实例隔离分类逻辑。

核心保证：多副本下，启动清扫只 kill 本实例标签的沙箱，绝不误杀别副本的沙箱
（替代 12.2 的 opt-in 全清扫开关止血）。
"""

from __future__ import annotations

from swarm.api.app import _partition_sweep_targets
from swarm.worker.sandbox import get_instance_id


def _sb(sid, instance=None):
    meta = {"swarm_instance": instance} if instance else {}
    return {"id": sid, "metadata": meta}


def test_kills_only_own_instance():
    """本实例标签的清，别副本的保留。"""
    lst = [
        _sb("a", "me"),
        _sb("b", "other-replica"),
        _sb("c", "me"),
    ]
    to_kill, kept_other, kept_untagged = _partition_sweep_targets(lst, "me", sweep_untagged=True)
    assert set(to_kill) == {"a", "c"}
    assert kept_other == 1  # b 是别副本，绝不动
    assert kept_untagged == 0


def test_untagged_respects_switch_on():
    """无标签沙箱：开关 on → 清。"""
    lst = [_sb("x"), _sb("y", "me")]
    to_kill, _, kept_untagged = _partition_sweep_targets(lst, "me", sweep_untagged=True)
    assert set(to_kill) == {"x", "y"}
    assert kept_untagged == 0


def test_untagged_respects_switch_off():
    """无标签沙箱：开关 off（共享集群保守）→ 留。"""
    lst = [_sb("x"), _sb("y", "me")]
    to_kill, _, kept_untagged = _partition_sweep_targets(lst, "me", sweep_untagged=False)
    assert to_kill == ["y"]  # 只清本实例标签的
    assert kept_untagged == 1  # 无标签的 x 保留


def test_never_kills_other_replica_even_with_switch_on():
    """关键安全保证：即使 sweep_untagged=True，别副本【有标签】的沙箱也绝不被清。"""
    lst = [_sb("other1", "replica-2"), _sb("other2", "replica-3")]
    to_kill, kept_other, _ = _partition_sweep_targets(lst, "me", sweep_untagged=True)
    assert to_kill == []
    assert kept_other == 2


def test_instance_id_stable_within_process():
    assert get_instance_id() == get_instance_id()


if __name__ == "__main__":
    import sys
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}: {e}")
    print(f"\n=== A1 批3 实例隔离清扫: {len(fns) - failed}/{len(fns)} passed ===")
    sys.exit(1 if failed else 0)
