"""Embed/Rerank 配置解析单测 — knowledge/embed_rerank_config.py。

固化方案 A 关键逻辑（docs/Embed_Rerank_Config_Design.md §四.4）：
- 复用 provider key 三道防线：同源放行 / 异源拒绝 / provider 缺失或无 key 降级。
- key 解析优先级：自己明文 > secret_store > 复用 provider。
- KnowledgeConfig 新增字段默认值。
无外部依赖（monkeypatch 掉 ModelConfig / secret_store）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.knowledge import embed_rerank_config as erc


class _FakeProvider:
    def __init__(self, pid, base_url, api_key):
        self.id = pid
        self.base_url = base_url
        self.api_key = api_key


def _patch_providers(monkeypatch, providers):
    """monkeypatch ModelConfig._effective_providers 返回指定 providers。"""
    import swarm.config.settings as settings

    def _fake_eff(self):
        return providers
    monkeypatch.setattr(settings.ModelConfig, "_effective_providers", _fake_eff, raising=False)


def test_same_origin():
    assert erc._same_origin("https://api.siliconflow.cn/v1", "https://api.siliconflow.cn/v1/")
    assert erc._same_origin("https://api.siliconflow.cn/v1", "https://api.siliconflow.cn/rerank")
    assert not erc._same_origin("https://api.siliconflow.cn/v1", "https://api.openai.com/v1")
    assert not erc._same_origin("http://ai.bit:8082/v1", "http://ai.bit:8081/rerank")  # 端口不同
    print("  ✅ 同源判定（host+port，路径无关）")


def test_reuse_key_same_origin_allowed(monkeypatch):
    """同源 → 放行复用 provider key。"""
    _patch_providers(monkeypatch, [_FakeProvider("siliconflow", "https://api.siliconflow.cn/v1", "sk-REUSE")])
    key = erc._provider_key_if_same_origin("siliconflow", "https://api.siliconflow.cn/v1")
    assert key == "sk-REUSE"
    print("  ✅ 同源放行复用 key")


def test_reuse_key_cross_origin_rejected(monkeypatch):
    """异源 → 拒绝（防把 A 家 key 发给 B 家端点）。"""
    _patch_providers(monkeypatch, [_FakeProvider("siliconflow", "https://api.siliconflow.cn/v1", "sk-REUSE")])
    key = erc._provider_key_if_same_origin("siliconflow", "https://api.openai.com/v1")
    assert key == "", "异源必须拒绝"
    print("  ✅ 异源拒绝复用 key（防错配）")


def test_reuse_key_provider_missing(monkeypatch):
    """provider 不存在 → 降级返回空，不崩。"""
    _patch_providers(monkeypatch, [])
    assert erc._provider_key_if_same_origin("nonexistent", "https://x.com/v1") == ""
    print("  ✅ provider 缺失降级")


def test_reuse_key_provider_no_key(monkeypatch):
    """provider 存在但无 key → 降级返回空。"""
    _patch_providers(monkeypatch, [_FakeProvider("siliconflow", "https://api.siliconflow.cn/v1", "")])
    assert erc._provider_key_if_same_origin("siliconflow", "https://api.siliconflow.cn/v1") == ""
    print("  ✅ provider 无 key 降级")


def test_resolve_key_priority_own_first(monkeypatch):
    """自己有明文 key → 优先用自己的（不查 secret_store / 不复用）。"""
    _patch_providers(monkeypatch, [_FakeProvider("siliconflow", "https://api.siliconflow.cn/v1", "sk-PROVIDER")])
    key = erc._resolve_key("sk-OWN", erc.SECRET_EMBED_KEY, "siliconflow", "https://api.siliconflow.cn/v1")
    assert key == "sk-OWN"
    print("  ✅ key 优先级：自己明文优先")


def test_resolve_key_fallback_to_reuse(monkeypatch):
    """自己无 key + secret_store 无 → 回退复用 provider（同源）。"""
    _patch_providers(monkeypatch, [_FakeProvider("siliconflow", "https://api.siliconflow.cn/v1", "sk-PROVIDER")])
    monkeypatch.setattr(erc, "_resolve_key", erc._resolve_key)  # 确保用真实实现
    # secret_store 返回 None
    import swarm.config.secret_store as ss
    monkeypatch.setattr(ss, "get_secret", lambda name: None, raising=False)
    key = erc._resolve_key("", erc.SECRET_EMBED_KEY, "siliconflow", "https://api.siliconflow.cn/v1")
    assert key == "sk-PROVIDER"
    print("  ✅ key 优先级：回退复用 provider")


def test_config_fields_defaults():
    """KnowledgeConfig 新增字段默认值正确。"""
    from swarm.config.settings import KnowledgeConfig
    k = KnowledgeConfig()
    assert k.embed_format == "openai"
    assert k.rerank_format == "simple"
    assert hasattr(k, "rerank_api_key")
    assert hasattr(k, "embed_reuse_provider")
    assert hasattr(k, "rerank_reuse_provider")
    print("  ✅ KnowledgeConfig 新字段默认值")


def test_catalog_shape():
    """catalog 预置结构完整。"""
    for c in erc.EMBED_CATALOG:
        assert {"id", "label", "base_url", "model", "format"} <= set(c)
    for c in erc.RERANK_CATALOG:
        assert {"id", "label", "base_url", "model", "format"} <= set(c)
        assert c["format"] in ("simple", "openai_rerank", "cohere_rerank")
    print("  ✅ catalog 结构完整")
