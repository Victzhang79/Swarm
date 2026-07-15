"""B5（round22, P1）：多模态路由从全能力库全局选模型，可静默越出 worker 本地策略。

根因：_multimodal_model_from_capabilities 从全库 supports_multimodal=True 记录选 context 最大者，
不过滤 models_in_use()/provider 策略 → 若探到云端 VL 模型进 capability_store，worker 多模态子任务
静默走云端，与"worker 全本地"冲突。

治本：只保留 local provider 的多模态模型（保留 A.5 自动发现）。过滤后空 → None → 回退写死配置。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from swarm.models.router import ModelRouter


def _router(kind_of):
    router = ModelRouter.__new__(ModelRouter)
    cfg = MagicMock()

    def _prov(m):
        p = MagicMock(); p.kind = kind_of(m); return p
    cfg.provider_for_model.side_effect = _prov
    router.config = cfg
    return router


def test_prefers_local_even_when_cloud_has_bigger_context():
    """云端 VL context 更大，但应被过滤——只选本地 VL（阻止静默走云端）。"""
    router = _router(lambda m: "local" if m == "local-vl" else "cloud")
    rows = [
        {"model_id": "cloud/vl", "supports_multimodal": True, "source": "probed", "context_window": 200000},
        {"model_id": "local-vl", "supports_multimodal": True, "source": "probed", "context_window": 8000},
    ]
    with patch("swarm.models.capability_store.list_capabilities", return_value=rows):
        got = router._multimodal_model_from_capabilities()
    assert got == "local-vl", got


def test_none_when_only_cloud_vl_available():
    """只有云端 VL → 返回 None（交调用方回退写死配置，不静默走云端）。"""
    router = _router(lambda m: "cloud")
    rows = [{"model_id": "cloud/vl", "supports_multimodal": True, "source": "probed", "context_window": 200000}]
    with patch("swarm.models.capability_store.list_capabilities", return_value=rows):
        got = router._multimodal_model_from_capabilities()
    assert got is None, got


def test_local_vl_autodiscovered_without_in_use_list():
    """本地探测出的 VL 照常可用（保留 A.5 自动发现，不强制静态 in_use 清单）。"""
    router = _router(lambda m: "local")
    rows = [{"model_id": "local-vl", "supports_multimodal": True, "source": "probed", "context_window": 8000}]
    with patch("swarm.models.capability_store.list_capabilities", return_value=rows):
        got = router._multimodal_model_from_capabilities()
    assert got == "local-vl", got


def _route_router(configured_mm: str):
    """构造 _resolve_route 用的 router：本地 provider + 指定 routing_multimodal。"""
    router = ModelRouter.__new__(ModelRouter)
    cfg = MagicMock()
    cfg.routing_multimodal = configured_mm
    cfg.routing_multimodal_fallback = ["stepfun-ai/Step-3.7-Flash-FP8"]

    def _prov(m):
        p = MagicMock(); p.kind = "local"; p.id = "local"; return p
    cfg.provider_for_model.side_effect = _prov
    router.config = cfg
    return router


def test_configured_multimodal_wins_over_stale_downlined_row():
    """换装安全（hunter#4 治本）：显式配的 routing_multimodal 本身是多模态(启发式 hint)→ 权威，
    绝不被能力库里【已下线模型的陈旧 probed 行】盖过、把图像子任务首派到死端点。"""
    router = _route_router("ThinkingCap-Qwen3.6-27B")  # 名字 hint → 多模态
    stale = [{"model_id": "Qwen3.6-27B-Saka-NVFP4-multimodal", "supports_multimodal": True,
              "source": "probed", "context_window": 128000}]  # 下线模型陈旧行，本会被自动发现选中
    with patch("swarm.models.capability_store.list_capabilities", return_value=stale), \
         patch("swarm.models.capability_store.get_capability", return_value=None):
        primary, fb = router._resolve_route("medium", "multimodal")
    assert primary == "ThinkingCap-Qwen3.6-27B", primary  # 不是下线的 Saka-mm
    assert "Qwen3.6-27B-Saka-NVFP4-multimodal" not in (primary, *fb)


def test_autodiscovery_kicks_in_when_configured_not_multimodal():
    """配的 routing_multimodal 不是多模态(疑似误配) → 回落 A.5 自动发现本地 VL（B5 语义不回归）。"""
    router = _route_router("plain-text-model")  # 无 hint → 非多模态
    rows = [{"model_id": "local-vl", "supports_multimodal": True,
             "source": "probed", "context_window": 8000}]
    with patch("swarm.models.capability_store.list_capabilities", return_value=rows), \
         patch("swarm.models.capability_store.get_capability", return_value=None):
        primary, fb = router._resolve_route("medium", "multimodal")
    assert primary == "local-vl", primary


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
