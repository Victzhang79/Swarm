"""provider 固定 temperature（2026-07-20 接入 Kimi Code 订阅 k3 时治本）：
某些 provider 的模型只接受固定 temperature（k3/kimi-for-coding 恒 =1，reasoning 约束，
传 0.1/0.2 直接 400 invalid temperature）。ProviderConfig.fixed_temperature 设了则强制覆盖。
栈中立：任意 provider 可声明；留空=老行为（用调用方 temperature）。
"""
from __future__ import annotations

from swarm.config.settings import ModelConfig, ProviderConfig
from swarm.models.router import EndpointProvider


def _ep(**prov_kw):
    prov = ProviderConfig(id=prov_kw.pop("id", "p"), kind=prov_kw.pop("kind", "cloud"),
                          base_url="https://x/v1", api_key="k", **prov_kw)
    return EndpointProvider(prov, ModelConfig())


def test_fixed_temperature_overrides_caller():
    """★核心★ provider.fixed_temperature=1 → get_chat_model 忽略调用方 0.1，用 1。"""
    m = _ep(id="kimi-code", fixed_temperature=1.0).get_chat_model("k3", temperature=0.1)
    assert float(m.temperature) == 1.0, f"应被强制为 1，实为 {m.temperature}"


def test_no_fixed_temperature_uses_caller():
    """未设 fixed_temperature → 老行为，用调用方 temperature（不回归）。"""
    m = _ep(id="siliconflow").get_chat_model("glm", temperature=0.1)
    assert abs(float(m.temperature) - 0.1) < 1e-9


def test_fixed_temperature_default_none():
    """ProviderConfig.fixed_temperature 缺省 None（老 provider 零变化）。"""
    assert ProviderConfig(id="x1").fixed_temperature is None


def test_local_provider_does_not_assume_thinking_extension():
    """local 只表示部署位置，不能暗含网关支持 chat_template_kwargs。"""
    m = _ep(id="local", kind="local").get_chat_model("Qwen3.8-27B-TP2")
    assert m.extra_body is None


def test_provider_can_explicitly_disable_thinking():
    """只有 provider 显式声明支持时，才发 enable_thinking=false 扩展字段。"""
    m = _ep(id="vllm", kind="local", disable_thinking=True).get_chat_model("qwen")
    assert m.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}


def test_disable_thinking_defaults_false():
    assert ProviderConfig(id="x1").disable_thinking is False


def test_local_brain_primary_keeps_reasoning_failover_budget():
    """本地 Brain 主模型仍须先触发 reasoning 超时，给本地备模型让路。"""
    cfg = ModelConfig(brain_reasoning_phase_budget_s=321.0)
    ep = EndpointProvider(
        ProviderConfig(id="local-brain", kind="local", base_url="https://x/v1", api_key="k"),
        cfg,
    )

    primary = ep.get_chat_model(
        "glm-5.3-flash-local", wallclock_budget=1500.0, no_fallback=False,
    )
    chain_tail = ep.get_chat_model(
        "glm-5.3", wallclock_budget=1500.0, no_fallback=True,
    )

    assert primary.swarm_reasoning_phase_budget == 321.0
    assert chain_tail.swarm_reasoning_phase_budget == 0.0


def test_brain_router_builds_flash_primary_and_glm_fallback(monkeypatch):
    """生产 Router 必须按 Flash→GLM 构造链，并给两端各自完整墙钟预算。"""
    from swarm.models.router import ModelRouter

    monkeypatch.setattr(ModelRouter, "_reachability_validated", True)
    monkeypatch.setattr(
        ModelConfig, "_resolve_api_key",
        lambda self, provider_id, env_fallback: (env_fallback, 0),
    )
    cfg = ModelConfig(
        _env_file=None,
        providers=[ProviderConfig(
            id="local", kind="local", base_url="https://x/v1", api_key="test-key")],
        model_providers={
            "glm-5.3-flash-local": "local",
            "glm-5.3": "local",
        },
        brain_primary="glm-5.3-flash-local",
        brain_fallback="glm-5.3",
        brain_reasoning_phase_budget_s=600.0,
        brain_stream_wallclock_s=1500.0,
    )
    chain = ModelRouter(cfg).get_brain_llm()

    assert chain.runnable.model_name == "glm-5.3-flash-local"
    assert chain.fallbacks[0].model_name == "glm-5.3"
    assert chain.runnable.swarm_reasoning_phase_budget == 600.0
    assert chain.runnable.swarm_wallclock_budget == 1500.0
    assert chain.fallbacks[0].swarm_reasoning_phase_budget == 0.0
    assert chain.fallbacks[0].swarm_wallclock_budget == 1500.0
