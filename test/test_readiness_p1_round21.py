#!/usr/bin/env python3
"""P1 (round21 起跑前对抗审计·Agent C)：两处会重新制造故障的回归修复。

#7 消费者分类优先权：`_detect_parallel_impls` 对家族目录外的 extra core，先挑 downstream 消费者、
剩下才算 upstream shared——没给 `_is_upstream_shared` 优先权。名字以 Gateway/Resource/Facade/
Aggregator 结尾【且同时是共享抽象】的类(IPaymentGateway/AbstractGateway/BaseFacade)会被误判
downstream→末批后建→leaf fan-out 编译时 import 它→cannot find symbol(正是红线要防的症状)。
治本：消费者判定须 `_is_downstream_consumer(b) and not _is_upstream_shared(b)`。

#11(c) proj_path 空值护栏：`filter_orphan_module_patches` 的 base_module_exists 在 base 路径不可用
(project_id 缺失/store 抛错)时对所有 dir 返 False → 既有模块被误判孤儿→补丁全剔→误杀交付。
治本：base 不可用时传 base_module_exists=None → filter 跳过过滤(fail-safe 不误剔)。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.merge_engine import filter_orphan_module_patches  # noqa: E402
from swarm.brain.planning_nodes import _detect_parallel_impls  # noqa: E402

_LEAVES = [
    "modA/src/main/java/com/x/pay/channel/AlipayChannel.java",
    "modA/src/main/java/com/x/pay/channel/WechatChannel.java",
    "modA/src/main/java/com/x/pay/channel/UnionpayChannel.java",
]


# ── #7：共享抽象即便名字以消费者后缀结尾也须归 upstream ──

def test_shared_abstraction_with_consumer_suffix_stays_upstream():
    # IPaymentGateway：I 前缀接口(=共享抽象) 但以 Gateway 结尾(=消费者后缀)——应 upstream，非 downstream
    iface = "modA/src/main/java/com/x/pay/api/IPaymentGateway.java"
    leaves, upstream, downstream = _detect_parallel_impls([*_LEAVES, iface])
    assert iface in upstream, "I 前缀接口即便以 Gateway 结尾也应 upstream(leaf 依赖它)"
    assert iface not in downstream, "绝不能把共享接口降到 downstream→leaf cannot find symbol"
    print("  ✅ #7a IPaymentGateway(接口+Gateway后缀) → upstream")


def test_abstract_base_with_consumer_suffix_stays_upstream():
    for shared in ("AbstractPaymentGateway", "BasePaymentFacade", "PaymentSupport"):
        f = f"modA/src/main/java/com/x/pay/api/{shared}.java"
        leaves, upstream, downstream = _detect_parallel_impls([*_LEAVES, f])
        assert f in upstream and f not in downstream, shared
    print("  ✅ #7b Abstract*/Base*/*Support 即便碰消费者后缀 → upstream")


def test_genuine_consumer_still_downstream():
    # 真消费者(非共享抽象) 仍归 downstream
    ctrl = "modA/src/main/java/com/x/pay/web/PaymentController.java"
    fac = "modA/src/main/java/com/x/pay/config/PaymentChannelFactory.java"
    leaves, upstream, downstream = _detect_parallel_impls([*_LEAVES, ctrl, fac])
    assert ctrl in downstream and fac in downstream
    assert ctrl not in upstream and fac not in upstream
    print("  ✅ #7c 真消费者(Controller/Factory) → 仍 downstream(治本未过枉)")


# ── #11(c)：base 路径不可用 → 跳过孤儿过滤(fail-safe) ──

_ORPHAN_DIFF = (
    "diff --git a/neworphan/src/main/java/com/x/A.java b/neworphan/src/main/java/com/x/A.java\n"
    "--- a/neworphan/src/main/java/com/x/A.java\n"
    "+++ b/neworphan/src/main/java/com/x/A.java\n"
    "@@ -0,0 +1,1 @@\n+class A {}\n"
)


def test_orphan_culled_when_base_says_absent():
    diffs = [("st-1", _ORPHAN_DIFF)]
    out, dropped = filter_orphan_module_patches(diffs, base_module_exists=lambda d: False)
    assert dropped and out == [], "base 明确说模块不存在 → 剔孤儿补丁(既有行为)"
    print("  ✅ #11c-base_absent 孤儿补丁被剔(既有行为不回归)")


def test_orphan_kept_when_base_unavailable_none():
    diffs = [("st-1", _ORPHAN_DIFF)]
    # base 路径不可用 → 传 None → 跳过过滤，绝不误剔
    out, dropped = filter_orphan_module_patches(diffs, base_module_exists=None)
    assert out == diffs and not dropped, "base 不可用(None) → fail-safe 不过滤、不误杀"
    print("  ✅ #11c-base_none base 不可用 → 跳过过滤(fail-safe 不误杀既有模块)")


def test_orphan_kept_when_base_says_exists():
    diffs = [("st-1", _ORPHAN_DIFF)]
    out, dropped = filter_orphan_module_patches(diffs, base_module_exists=lambda d: True)
    assert out == diffs and not dropped
    print("  ✅ #11c-base_exists 既有模块 → 保留")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
