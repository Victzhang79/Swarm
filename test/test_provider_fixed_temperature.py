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
    assert ProviderConfig(id="x").fixed_temperature is None
