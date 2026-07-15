#!/usr/bin/env python3
"""消费方改造单测（设计 v3 A批3）。

验证：
  1. 上下文预算 _context_budget — 有真值用真值×0.75、全default回退、env覆盖、无库回退。
  2. 多模态选型 _resolve_route — 能力库有多模态模型则用之，无则回退写死配置。
全部 mock 能力库（不接真 DB）。重点保证**回退安全**：不破坏现有规划链路。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import os

from swarm.brain import planning_nodes as P
from swarm.models import capability_store as cap


# ── 上下文预算 ─────────────────────────────────────────────

def test_budget_env_override_wins(monkeypatch):
    monkeypatch.setenv("SWARM_SUBTASK_CONTEXT_BUDGET", "99999")
    assert P._context_budget() == 99999
    print("  ✅ 预算: env 显式覆盖优先 → 99999")


def test_budget_no_capability_falls_back(monkeypatch):
    monkeypatch.delenv("SWARM_SUBTASK_CONTEXT_BUDGET", raising=False)
    # 能力库无真值 → 回退写死兜底
    with patch.object(P, "_min_worker_context_window", return_value=None):
        assert P._context_budget() == P.DEFAULT_CONTEXT_BUDGET
    print(f"  ✅ 预算: 无能力库真值 → 回退兜底 {P.DEFAULT_CONTEXT_BUDGET}")


def test_budget_uses_real_window(monkeypatch):
    monkeypatch.delenv("SWARM_SUBTASK_CONTEXT_BUDGET", raising=False)
    # 真实窗口 40000 → 40000*0.75=30000 < 兜底150000 → 取 30000
    with patch.object(P, "_min_worker_context_window", return_value=40000):
        assert P._context_budget() == 30000
    print("  ✅ 预算: 真实窗口40k×0.75=30k < 兜底 → 取30k")


def test_budget_caps_at_fallback(monkeypatch):
    monkeypatch.delenv("SWARM_SUBTASK_CONTEXT_BUDGET", raising=False)
    # 真实窗口 1000000 → ×0.75=750000 > 兜底150000 → 取兜底（保守）
    with patch.object(P, "_min_worker_context_window", return_value=1_000_000):
        assert P._context_budget() == P.DEFAULT_CONTEXT_BUDGET
    print("  ✅ 预算: 巨大窗口×0.75 > 兜底 → 取兜底(保守上限)")


def test_min_worker_window_skips_default_source():
    """只采纳 source != default 的真值；全 default 则返回 None。"""
    def fake_get_cap(provider_id, model_id, conn_str=None):
        # 全部返回 default 源 → 应被跳过
        return {"context_window": 32000, "source": cap.SOURCE_DEFAULT}

    with patch("swarm.models.capability_store.get_capability", side_effect=fake_get_cap):
        assert P._min_worker_context_window() is None
    print("  ✅ 预算: 全 default 源被跳过 → None (不假装精确)")


def test_min_worker_window_takes_min_of_real(monkeypatch):
    """多个真值取最小（保守，预算须让各档子任务都装得下）。

    不锁定 .env 里 worker_parallel_pool 的具体条目数（已随路由配置漂移到 1 个），
    而是显式注入 2 个候选模型来验证【多真值取最小】这一不变量本身。每个模型按
    其名字返回固定真值（避免依赖调用次序/池长度）。
    """
    from swarm.config.settings import get_config

    # 注入两个候选模型 → 强制走"多窗口取最小"分支，与 live config 解耦
    monkeypatch.setattr(
        get_config().worker, "worker_parallel_pool", ["probed_a", "probed_b"]
    )
    real_windows = {"probed_a": 128000, "probed_b": 32768}

    def fake_get_cap(provider_id, model_id, conn_str=None):
        # 候选集 = 池 ∪ 路由三档+fallback（2026-07-15 治本：涵盖所有可达 worker 目标）。
        # 对注入的两个 pool 模型给真值；其余(live 路由模型)给大窗口，不影响"取最小"断言。
        return {"context_window": real_windows.get(model_id, 999999), "source": cap.SOURCE_PROBED}

    with patch("swarm.models.capability_store.get_capability", side_effect=fake_get_cap):
        result = P._min_worker_context_window()
    assert result == 32768, f"应取最小真值，得 {result}"
    print("  ✅ 预算: 多真值取最小 → 32768")


# ── 多模态选型 ─────────────────────────────────────────────

def test_multimodal_route_autodiscovers_when_configured_not_mm(monkeypatch):
    """precedence 演进（2026-07-15 hunter#4 治本）：仅当配的 routing_multimodal【本身非多模态】
    (疑似误配)时，才从能力库自动发现——探测确认的 vision-pro 优先 default 源的 vision-small。"""
    from swarm.models.router import ModelRouter

    router = ModelRouter()
    monkeypatch.setattr(router.config, "routing_multimodal", "plain-text-model")  # 非多模态
    rows = [
        {"model_id": "text-only", "supports_multimodal": False, "source": "probed", "context_window": 128000},
        {"model_id": "vision-pro", "supports_multimodal": True, "source": "probed", "context_window": 200000},
        {"model_id": "vision-small", "supports_multimodal": True, "source": "default", "context_window": 32000},
    ]
    with patch("swarm.models.capability_store.list_capabilities", return_value=rows), \
         patch("swarm.models.capability_store.get_capability", return_value=None):
        primary, fallback = router._resolve_route("medium", "multimodal")
    assert primary == "vision-pro", primary
    print("  ✅ 多模态: 配非多模态→能力库自动发现探测确认的 vision-pro")


def test_configured_multimodal_wins_over_capability(monkeypatch):
    """换装安全（hunter#4 治本）：配的 routing_multimodal 本身多模态 → 单一权威，压过能力库
    自动发现——绝不被陈旧/下线模型的 probed 行把子任务派到死端点。"""
    from swarm.models.router import ModelRouter

    router = ModelRouter()
    monkeypatch.setattr(router.config, "routing_multimodal", "ThinkingCap-Qwen3.6-27B")  # 名字 hint→多模态
    rows = [{"model_id": "vision-pro", "supports_multimodal": True, "source": "probed", "context_window": 200000}]
    with patch("swarm.models.capability_store.list_capabilities", return_value=rows), \
         patch("swarm.models.capability_store.get_capability", return_value=None):
        primary, fallback = router._resolve_route("medium", "multimodal")
    assert primary == "ThinkingCap-Qwen3.6-27B", primary  # 配的权威，不被 vision-pro 顶掉
    print("  ✅ 多模态: 配的多模态模型权威，压过能力库自动发现")


def test_multimodal_route_fallback_when_no_capability():
    from swarm.models.router import ModelRouter

    router = ModelRouter()
    # 能力库无多模态模型 → 回退写死 routing_multimodal
    with patch("swarm.models.capability_store.list_capabilities", return_value=[]):
        primary, fallback = router._resolve_route("medium", "multimodal")
    assert primary == router.config.routing_multimodal
    print(f"  ✅ 多模态: 能力库空 → 回退写死配置 {primary}")


def test_text_route_unaffected():
    """文本路由不受能力库影响（回退安全：不破坏现有链路）。"""
    from swarm.models.router import ModelRouter

    router = ModelRouter()
    primary, fallback = router._resolve_route("complex", "text")
    assert primary == router.config.routing_complex
    print("  ✅ 多模态: 文本路由不受影响（回退安全）")


# ── 在用模型集合（探测范围收窄）─────────────────────────────

def test_models_in_use_dedup():
    """在用模型集合去重保序，覆盖 brain+worker+routing 全部档位。"""
    from swarm.config.settings import ModelConfig

    cfg = ModelConfig(
        brain_primary="A", brain_fallback="B",
        worker_primary="A",  # 与 brain_primary 重复 → 去重
        worker_local="C", worker_fallback="D",
        routing_trivial="E", routing_trivial_fallback="F",
        routing_medium="A",  # 重复
        routing_medium_fallback="G",
        routing_complex="H", routing_complex_fallback="I",
        routing_multimodal="J", routing_multimodal_fallback="A",  # 重复
    )
    models = cfg.models_in_use()
    # 去重后应是 A,B,C,D,E,F,G,H,I,J（保序，A 只出现一次且在最前）
    assert models[0] == "A"
    assert models.count("A") == 1
    assert set(models) == set("ABCDEFGHIJ")
    print(f"  ✅ 在用模型: 去重保序 → {len(models)} 个唯一模型")


def test_models_in_use_for_provider():
    """按 provider 过滤在用模型 —— 探测某接入点的精确目标集合。"""
    from swarm.config.settings import ModelConfig, ProviderConfig

    cfg = ModelConfig(
        providers=[
            ProviderConfig(id="cloud1", kind="cloud", base_url="https://x/v1"),
            ProviderConfig(id="local1", kind="local", base_url="http://y/v1"),
        ],
        model_providers={
            "cloud-model": "cloud1",
            "local-model": "local1",
        },
        brain_primary="cloud-model", brain_fallback="local-model",
        worker_primary="cloud-model", worker_local="local-model", worker_fallback="local-model",
        routing_trivial="local-model", routing_trivial_fallback="local-model",
        routing_medium="cloud-model", routing_medium_fallback="cloud-model",
        routing_complex="cloud-model", routing_complex_fallback="local-model",
        routing_multimodal="local-model", routing_multimodal_fallback="cloud-model",
    )
    cloud_models = cfg.models_in_use_for_provider("cloud1")
    local_models = cfg.models_in_use_for_provider("local1")
    assert cloud_models == ["cloud-model"]
    assert local_models == ["local-model"]
    print("  ✅ 在用模型: 按 provider 过滤 (cloud1→[cloud-model], local1→[local-model])")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))
