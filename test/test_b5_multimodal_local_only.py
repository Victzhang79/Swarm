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


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
