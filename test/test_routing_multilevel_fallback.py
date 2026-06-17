"""T2 多级兜底链 —— 补充 test_routing_local_workers.py 未覆盖的实现细节：
env 逗号链解析、get_llm 串多级 with_fallbacks、models_in_use 展平、单串向后兼容。
（worker全本地/无Kimi/_resolve_route返list/alternate取同级 等验收点见 test_routing_local_workers.py）
"""
from swarm.config.settings import ModelConfig
from swarm.models.router import ModelRouter


def test_fallback_fields_are_lists_with_backcompat():
    c = ModelConfig()
    assert isinstance(c.routing_complex_fallback, list)
    assert len(c.routing_complex_fallback) >= 2  # 多级链
    # 旧式纯字符串构造 → 自动归一为单元素 list（向后兼容旧 .env）
    c2 = ModelConfig(routing_complex_fallback="只有一个模型")
    assert c2.routing_complex_fallback == ["只有一个模型"]


def test_env_comma_chain_parses(monkeypatch):
    monkeypatch.setenv("SWARM_MODEL_ROUTING_COMPLEX_FALLBACK", "M1, M2 ,M3")
    c = ModelConfig()
    assert c.routing_complex_fallback == ["M1", "M2", "M3"]


def test_get_llm_builds_multilevel_fallbacks():
    r = ModelRouter(ModelConfig(
        routing_medium="local-a",
        routing_medium_fallback="local-b,local-c",
    ))
    llm = r.get_llm_for_subtask("medium")
    # 多级 → RunnableWithFallbacks，fallbacks 数量 = 链长
    assert hasattr(llm, "fallbacks")
    assert len(llm.fallbacks) == 2


def test_models_in_use_flattens_chain():
    c = ModelConfig(routing_complex="X", routing_complex_fallback="Y1,Y2")
    mu = c.models_in_use()
    assert "X" in mu and "Y1" in mu and "Y2" in mu
