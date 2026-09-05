"""Batch 4B：模型探活与多模态选型必须使用同一条权威路由。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from swarm.api.routers import config as config_api
from swarm.brain import vision_ingest
from swarm.models.router import ModelRouter


def _successful_llm(content: str = "OK") -> MagicMock:
    llm = MagicMock()
    llm.invoke.side_effect = AssertionError("模型探活不得绕过异步流总墙钟")
    llm.ainvoke = AsyncMock(return_value=SimpleNamespace(content=content))
    return llm


def test_config_probe_fails_when_exact_brain_primary_is_down(monkeypatch):
    """备用成功不能把已下线的主模型伪装成探活成功。"""
    model = SimpleNamespace(
        brain_primary="brain-primary",
        brain_temperature=0.1,
        brain_stream_wallclock_s=1500.0,
        routing_medium="worker-medium",
        routing_complex="worker-complex",
        worker_temperature=0.2,
        worker_stream_wallclock_s=420.0,
        timeout_seconds=120,
    )
    monkeypatch.setattr(config_api._app, "get_config", lambda: SimpleNamespace(model=model))
    monkeypatch.setattr(config_api, "_require_perm", lambda *args, **kwargs: None)

    router = MagicMock()
    # 旧实现走完整 fallback 链时三个探针都会成功，精确探活则必须暴露主模型失败。
    router.get_brain_llm.return_value = _successful_llm()
    router.get_llm_for_subtask.return_value = _successful_llm()

    def exact_model(name: str, temperature: float = 0.2, **kwargs):
        if name == "brain-primary":
            raise RuntimeError("brain primary unavailable")
        return _successful_llm()

    router.get_model_by_name.side_effect = exact_model
    monkeypatch.setattr("swarm.models.router.ModelRouter", lambda: router)

    result = asyncio.run(config_api.test_config(MagicMock()))

    assert result["brain_primary"]["model"] == "brain-primary"
    assert result["brain_primary"]["ok"] is False
    assert result["worker_local_medium"]["ok"] is True
    assert result["worker_cloud_complex"]["ok"] is True
    assert result["all_ok"] is False
    assert router.get_llm_for_subtask.return_value.invoke.call_count == 0
    assert [call.args[0] for call in router.get_model_by_name.call_args_list] == [
        "brain-primary",
        "worker-medium",
        "worker-complex",
    ]
    assert router.get_model_by_name.call_args_list[0].kwargs == {
        "temperature": 0.1,
        "role": "brain/probe",
        "max_tokens": 8,
        "wallclock_budget": 120.0,
    }
    assert router.get_model_by_name.call_args_list[1].kwargs == {
        "temperature": 0.2,
        "role": "worker/medium/probe",
        "max_tokens": 8,
        "wallclock_budget": 120.0,
    }
    assert router.get_model_by_name.call_args_list[2].kwargs == {
        "temperature": 0.2,
        "role": "worker/complex/probe",
        "max_tokens": 8,
        "wallclock_budget": 120.0,
    }


def test_vision_selection_uses_public_route_resolver(monkeypatch):
    """附件视觉不能绕过显式 routing_multimodal 去捞能力库旧行。"""
    router = MagicMock()
    router.get_primary_model_name_for_subtask.return_value = "configured-vision"
    # 若生产代码仍直接消费能力库私有选择器，会得到错误的退役模型。
    router._multimodal_model_from_capabilities.return_value = "retired-vision"
    router.config.routing_multimodal = "configured-vision"
    monkeypatch.setattr("swarm.models.router.ModelRouter", lambda: router)

    assert vision_ingest.select_vision_model() == "configured-vision"
    router.get_primary_model_name_for_subtask.assert_called_once_with(
        "medium", "multimodal"
    )


def test_direct_model_factory_carries_explicit_role_and_wallclock():
    """精确按名构造不能丢掉调用角色和总墙钟预算。"""
    router = ModelRouter.__new__(ModelRouter)
    router.config = SimpleNamespace(
        worker_max_tokens=8192,
        worker_stream_wallclock_s=420.0,
    )
    provider = MagicMock()
    provider.provider.kind = "local"
    provider.provider.id = "local"
    expected = MagicMock()
    provider.get_chat_model.return_value = expected
    router._get_provider_for_model = MagicMock(return_value=provider)

    actual = router.get_model_by_name(
        "brain-primary",
        temperature=0.1,
        role="brain/probe",
        max_tokens=8,
        wallclock_budget=12.0,
    )

    assert actual is expected
    kwargs = provider.get_chat_model.call_args.kwargs
    assert kwargs["max_tokens"] == 8
    assert kwargs["wallclock_budget"] == 12.0
    assert kwargs["callbacks"][0].role == "brain/probe"


def test_config_probe_has_endpoint_deadline_before_first_chunk(monkeypatch):
    """模型连首包都不返回时，探活自身也必须在预算内失败而不是永久等待。"""
    model = SimpleNamespace(
        brain_primary="hanging-brain",
        brain_temperature=0.1,
        brain_stream_wallclock_s=0.0,
        routing_medium="worker-medium",
        routing_complex="worker-complex",
        worker_temperature=0.2,
        worker_stream_wallclock_s=0.0,
        timeout_seconds=0,
    )
    monkeypatch.setattr(config_api, "_MODEL_PROBE_DEFAULT_DEADLINE_S", 0.01)
    monkeypatch.setattr(config_api._app, "get_config", lambda: SimpleNamespace(model=model))
    monkeypatch.setattr(config_api, "_require_perm", lambda *args, **kwargs: None)

    hanging = MagicMock()

    async def wait_forever(*args, **kwargs):
        await asyncio.Event().wait()

    hanging.ainvoke = AsyncMock(side_effect=wait_forever)
    router = MagicMock()
    router.get_model_by_name.side_effect = lambda name, **kwargs: (
        hanging if name == "hanging-brain" else _successful_llm()
    )
    monkeypatch.setattr("swarm.models.router.ModelRouter", lambda: router)

    result = asyncio.run(
        asyncio.wait_for(config_api.test_config(MagicMock()), timeout=0.5)
    )

    assert result["brain_primary"]["ok"] is False
    assert result["brain_primary"]["error"] == "TimeoutError"
    assert result["worker_local_medium"]["ok"] is True
    assert result["worker_cloud_complex"]["ok"] is True
