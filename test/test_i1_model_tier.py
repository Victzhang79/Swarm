"""I1 单测：模型能力分级（model_tier）+ planning_nodes 约束随 tier 调整。

核心安全保证：默认（SWARM_MODEL_TIER_ENABLED 未开）= standard = 现有硬编码上限，零行为变化。
显式启用后，强模型收紧约束、弱模型放宽。用 monkeypatch.setenv，纯 env，无存储依赖。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.model_tier import (
    ModelCapabilityTier,
    infer_tier_from_model,
    resolve_tier,
    tier_constraints,
)

import pytest


@pytest.fixture(autouse=True)
def _isolate_config_from_env(monkeypatch):
    """这些测试验证 model_tier 的 env 路径逻辑。但 model_tier 现在优先读
    get_config().model.tier/tier_enabled（真相源），其从 .env 文件加载会干扰 env 用例。
    强制让 config 读取抛异常 → model_tier 回退到纯 env 路径（代码已有 try/except），
    使测试聚焦于 env 优先级/推断逻辑本身。config 优先于 env 的真实闭环已由浏览器
    dogfood + 全新进程探针验证（见会话记录），此处不重复。
    """
    import swarm.config.settings as _settings

    def _boom():
        raise RuntimeError("config disabled in unit test (force env path)")
    monkeypatch.setattr(_settings, "get_config", _boom, raising=True)


def test_infer_strong_models():
    assert infer_tier_from_model("Pro/zai-org/GLM-5.1") == ModelCapabilityTier.STRONG
    assert infer_tier_from_model("claude-opus-4") == ModelCapabilityTier.STRONG
    assert infer_tier_from_model("gpt-5") == ModelCapabilityTier.STRONG
    assert infer_tier_from_model("deepseek-v3") == ModelCapabilityTier.STRONG
    print("  ✅ 前沿模型推断 strong")


def test_infer_weak_models():
    assert infer_tier_from_model("qwen2.5-7b-instruct") == ModelCapabilityTier.WEAK
    assert infer_tier_from_model("gemma-2b") == ModelCapabilityTier.WEAK
    print("  ✅ 小模型推断 weak")


def test_infer_unknown_defaults_standard():
    assert infer_tier_from_model("some-random-model") == ModelCapabilityTier.STANDARD
    assert infer_tier_from_model(None) == ModelCapabilityTier.STANDARD
    assert infer_tier_from_model("") == ModelCapabilityTier.STANDARD
    print("  ✅ 未知模型默认 standard")


def test_default_disabled_is_standard(monkeypatch):
    """默认（开关未开）→ 永远 standard 约束（=现有硬编码值），零行为变化。"""
    monkeypatch.delenv("SWARM_MODEL_TIER_ENABLED", raising=False)
    monkeypatch.delenv("SWARM_MODEL_TIER", raising=False)
    # 即便传强模型名，开关没开也是 standard
    c = tier_constraints("Pro/zai-org/GLM-5.1")
    assert c == {"clarify_rounds": 5, "design_rejects": 3, "elaborate_resplit": 3}
    print("  ✅ 默认关 = standard 约束（现状不变）")


def test_enabled_strong_tightens(monkeypatch):
    """启用 + 强模型 → 约束收紧。"""
    monkeypatch.setenv("SWARM_MODEL_TIER_ENABLED", "true")
    monkeypatch.delenv("SWARM_MODEL_TIER", raising=False)
    c = tier_constraints("claude-opus-4")
    assert c["clarify_rounds"] == 3 and c["design_rejects"] == 2 and c["elaborate_resplit"] == 2
    print("  ✅ 启用+强模型 = 约束收紧")


def test_enabled_weak_loosens(monkeypatch):
    """启用 + 弱模型 → 约束放宽。"""
    monkeypatch.setenv("SWARM_MODEL_TIER_ENABLED", "true")
    monkeypatch.delenv("SWARM_MODEL_TIER", raising=False)
    c = tier_constraints("qwen2.5-7b")
    assert c["elaborate_resplit"] == 4 and c["clarify_rounds"] == 6
    print("  ✅ 启用+弱模型 = 约束放宽")


def test_manual_override_wins(monkeypatch):
    """SWARM_MODEL_TIER 手动覆盖 > 模型名推断。"""
    monkeypatch.setenv("SWARM_MODEL_TIER_ENABLED", "true")
    monkeypatch.setenv("SWARM_MODEL_TIER", "weak")
    # 模型名是强模型，但手动覆盖为 weak
    assert resolve_tier("claude-opus-4") == ModelCapabilityTier.WEAK
    c = tier_constraints("claude-opus-4")
    assert c["elaborate_resplit"] == 4
    print("  ✅ 手动覆盖优先于推断")


def test_invalid_override_ignored(monkeypatch):
    """无效 SWARM_MODEL_TIER → 忽略，回退推断。"""
    monkeypatch.setenv("SWARM_MODEL_TIER_ENABLED", "true")
    monkeypatch.setenv("SWARM_MODEL_TIER", "garbage")
    assert resolve_tier("claude-opus-4") == ModelCapabilityTier.STRONG
    print("  ✅ 无效覆盖忽略回退推断")


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v", "-s"]))
