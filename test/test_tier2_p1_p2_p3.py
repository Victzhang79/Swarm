#!/usr/bin/env python3
"""P1/P3/P2（Tier-2，996db614 第五轮）：路由可达性误报 / 契约依赖 prompt / brain JSON mode。"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch


# ── P1：云端 brain 模型不应被本地探测库误报"不可达" ──

def test_p1_cloud_brain_not_flagged_unreachable():
    import swarm.models.capability_store as cap
    from swarm.models.router import ModelRouter

    router = ModelRouter()
    orig = cap.list_capabilities
    # 能力库已探测但只含某无关【本地】模型（不含 brain 云端模型）
    cap.list_capabilities = lambda *a, **k: [{"model_id": "some-local-only-model"}]
    try:
        issues = router.validate_routing_reachability()
        brain_issues = [i for i in issues if i.get("tier") == "brain"]
        assert not brain_issues, f"云端 brain 档(GLM-5.2/Kimi)经 provider 解析为 cloud 应可达，不应误报: {brain_issues}"
    finally:
        cap.list_capabilities = orig


# ── P3：契约依赖 prompt 改为【职责驱动】+ 功能→依赖映射 ──

def test_p3_contract_prompt_responsibility_driven():
    from swarm.brain.planning_nodes import CONTRACT_MODULE_SYSTEM

    # 契约阶段代码未写 → 据职责推断（非 import）
    assert "职责" in CONTRACT_MODULE_SYSTEM
    assert "代码尚未写" in CONTRACT_MODULE_SYSTEM or "据本模块" in CONTRACT_MODULE_SYSTEM
    # 给出常见功能→依赖映射，引导枚举全
    assert "jjwt" in CONTRACT_MODULE_SYSTEM and "redis" in CONTRACT_MODULE_SYSTEM
    assert "宁多勿漏" in CONTRACT_MODULE_SYSTEM


# ── P2：brain JSON mode 默认关（安全）、开启时绑 response_format、绑定失败优雅回退 ──

def test_p2_json_mode_off_by_default():
    import swarm.brain.nodes as nodes

    fake_router = MagicMock()
    fake_router.get_brain_llm.return_value = "BASE_LLM"
    old = os.environ.pop("SWARM_BRAIN_JSON_MODE", None)
    try:
        with patch.object(nodes, "ModelRouter", return_value=fake_router):
            assert nodes._get_brain_llm() == "BASE_LLM"  # 默认不绑
    finally:
        if old is not None:
            os.environ["SWARM_BRAIN_JSON_MODE"] = old


def test_p2_json_mode_on_binds_response_format():
    import swarm.brain.nodes as nodes

    base = MagicMock()
    base.bind.return_value = "BOUND_LLM"
    fake_router = MagicMock()
    fake_router.get_brain_llm.return_value = base
    os.environ["SWARM_BRAIN_JSON_MODE"] = "true"
    try:
        with patch.object(nodes, "ModelRouter", return_value=fake_router):
            assert nodes._get_brain_llm() == "BOUND_LLM"
            base.bind.assert_called_once()
            assert base.bind.call_args.kwargs.get("response_format") == {"type": "json_object"}
    finally:
        os.environ.pop("SWARM_BRAIN_JSON_MODE", None)


def test_p2_json_mode_bind_failure_degrades():
    import swarm.brain.nodes as nodes

    base = MagicMock()
    base.bind.side_effect = RuntimeError("不支持 response_format")
    fake_router = MagicMock()
    fake_router.get_brain_llm.return_value = base
    os.environ["SWARM_BRAIN_JSON_MODE"] = "true"
    try:
        with patch.object(nodes, "ModelRouter", return_value=fake_router):
            assert nodes._get_brain_llm() is base  # 绑定失败 → 回退原 llm，不崩
    finally:
        os.environ.pop("SWARM_BRAIN_JSON_MODE", None)


if __name__ == "__main__":
    import sys
    fails = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            try:
                v()
            except Exception as e:  # noqa: BLE001
                import traceback
                print(f"  ❌ {k}: {e}")
                traceback.print_exc()
                fails += 1
    print("OK" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
