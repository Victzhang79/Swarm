#!/usr/bin/env python3
"""#7 D4 平行实现家族·跨目录【消费者】批序错位（round20 治本）回归测试。

背景（round19 st-41 潜在，未触发）：`_detect_parallel_impls` 把家族目录【外】的所有 core 一律归
upstream 首批（`upstream = [f for f in core if f not in fam_dir] + upstream`）。但家族目录外的 core
有两类：① 共享类型（接口/DTO/消息类，leaf 依赖它们→确应上游首批先建）；② 消费者（Controller/
Facade/工厂等，依赖 leaf→应下游末批后建，readable 含全部 leaf）。原码把②也塞进上游→消费者先于
leaf 编译 → `cannot find symbol`。

治本：新 `_is_downstream_consumer` 识别跨目录消费者（协调者 + 表现层 Controller/Endpoint/Resource/
Facade/Aggregator/Gateway）→ 归 downstream 末批；其余共享类型仍归 upstream。fail-closed：消费者名
即便非真消费者，归下游(后建)也安全(下游可读全部)；反之归上游必炸→本治本消除该风险。

本套验证 leaf/upstream/downstream 三分类的落位。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.planning_nodes import _detect_parallel_impls, _is_downstream_consumer  # noqa: E402

# 3 个平行渠道 impl（sender 目录）+ 共享接口（同目录）+ 跨目录消费者 Controller + 跨目录共享 DTO
_LEAVES = [
    "modA/src/main/java/com/x/notify/sender/SlackNotifySender.java",
    "modA/src/main/java/com/x/notify/sender/DingTalkNotifySender.java",
    "modA/src/main/java/com/x/notify/sender/EmailNotifySender.java",
]
_IFACE = "modA/src/main/java/com/x/notify/sender/INotifySender.java"         # 共享抽象(同家族目录,I 前缀接口)
_CTRL = "modA/src/main/java/com/x/notify/web/NotifyController.java"          # 跨目录消费者
_DTO = "modA/src/main/java/com/x/notify/dto/NotifyMessage.java"             # 跨目录共享类型
_FACTORY = "modA/src/main/java/com/x/notify/config/NotifySenderFactory.java"  # 跨目录协调者


def test_reproduction_controller_not_in_upstream():
    core = [*_LEAVES, _IFACE, _CTRL]
    res = _detect_parallel_impls(core)
    assert res is not None, "3 个 *NotifySender 同目录应判为平行家族"
    leaves, upstream, downstream = res
    assert set(leaves) == set(_LEAVES)
    # ★核心★ 消费者 Controller 必须在 downstream(后建)，绝不在 upstream(先建→cannot find symbol)
    assert _CTRL in downstream, "跨目录消费者 Controller 应归 downstream 末批"
    assert _CTRL not in upstream, "Controller 绝不应在 upstream 首批（会先于 leaf 编译）"
    # 共享接口仍在 upstream 首批
    assert _IFACE in upstream
    print("  ✅ ① 复现+治本：Controller 归 downstream、接口归 upstream")


def test_cross_dir_shared_dto_stays_upstream():
    core = [*_LEAVES, _IFACE, _DTO]
    leaves, upstream, downstream = _detect_parallel_impls(core)
    # DTO 是 leaf 依赖的共享类型 → 仍 upstream 首批
    assert _DTO in upstream and _DTO not in downstream
    print("  ✅ ② 跨目录共享 DTO → 仍 upstream 首批（leaf 依赖它）")


def test_cross_dir_factory_goes_downstream():
    core = [*_LEAVES, _IFACE, _FACTORY]
    leaves, upstream, downstream = _detect_parallel_impls(core)
    assert _FACTORY in downstream and _FACTORY not in upstream
    print("  ✅ ③ 跨目录工厂/协调者 → downstream 末批")


def test_mixed_all_three_kinds():
    core = [*_LEAVES, _IFACE, _DTO, _CTRL, _FACTORY]
    leaves, upstream, downstream = _detect_parallel_impls(core)
    assert set(leaves) == set(_LEAVES)
    assert _IFACE in upstream and _DTO in upstream
    assert _CTRL in downstream and _FACTORY in downstream
    # upstream/downstream 无交叉、无遗漏
    assert not (set(upstream) & set(downstream))
    assert set(core) == set(leaves) | set(upstream) | set(downstream)
    print("  ✅ ④ 混合：接口/DTO 上游、Controller/Factory 下游，无交叉无遗漏")


def test_is_downstream_consumer_pure():
    for b in ("NotifyController", "UserEndpoint", "OrderResource", "PayFacade",
              "StatAggregator", "ApiGateway", "SenderFactory", "HandlerRegistry"):
        assert _is_downstream_consumer(b), b
    for b in ("NotifySender", "NotifyMessage", "AbstractSender", "BaseHandler", "ISender"):
        assert not _is_downstream_consumer(b), b
    print("  ✅ ⑤ _is_downstream_consumer：表现/协调层是、实现/共享类型否")


if __name__ == "__main__":
    test_reproduction_controller_not_in_upstream()
    test_cross_dir_shared_dto_stays_upstream()
    test_cross_dir_factory_goes_downstream()
    test_mixed_all_three_kinds()
    test_is_downstream_consumer_pure()
    print("\n✅ 全部通过：#7 平行家族跨目录消费者批序（round20 治本）")
