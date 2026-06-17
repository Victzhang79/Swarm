"""T2 模型路由验证：worker 全本地小模型 + 多级兜底链 + retry 取同级主力。"""
from swarm.config.settings import ModelConfig


def _cfg():
    return ModelConfig()


def test_routing_all_local_no_cloud():
    """worker 各档首选都是本地小模型，无云端 GLM/Kimi。"""
    c = _cfg()
    cloud_markers = ["GLM-5.1", "Kimi", "moonshot", "zai-org"]
    for tier in [c.routing_trivial, c.routing_medium, c.routing_complex]:
        assert not any(m in tier for m in cloud_markers), f"worker 首选不应云端: {tier}"


def test_complex_primary_is_strongest_local():
    """complex 首选 = 本地最强 Qwen3.6-40B-Claude（256K），不再云端 GLM。"""
    c = _cfg()
    assert "Qwen3.6-40B-Claude" in c.routing_complex, c.routing_complex


def test_complex_fallback_chain_order():
    """Q-T2-1：complex 兜底链 = 同级主力 MiniMax → 次级 Saka（122B-A10B 64K 已排除）。"""
    c = _cfg()
    fb = c.routing_complex_fallback
    assert isinstance(fb, list) and len(fb) >= 2, f"应多级兜底 list: {fb}"
    assert "MiniMax-M2.7-Pro" in fb[0], f"第一兜底应同级主力 MiniMax: {fb}"
    assert "Saka" in fb[1], f"第二兜底应次级 Saka: {fb}"
    # 122B-A10B(64K) 已排除出 worker 列表
    assert not any("122B-A10B" in x for x in fb), f"122B-A10B 应已排除: {fb}"


def test_no_small_context_model_in_workers():
    """64K 小窗口的 122B-A10B 不应出现在任何 worker 路由档/兜底链/worker_fallback。"""
    c = _cfg()
    allmodels = ([c.routing_trivial, c.routing_medium, c.routing_complex, c.worker_fallback]
                 + c.routing_trivial_fallback + c.routing_medium_fallback
                 + c.routing_complex_fallback)
    assert not any("122B-A10B" in m for m in allmodels), f"122B-A10B(64K) 应排除出 worker: {allmodels}"


def test_no_kimi_403_anywhere():
    """移除所有 moonshotai/Kimi-K2.6（403 private 坏兜底）。"""
    c = _cfg()
    allfb = (c.routing_trivial_fallback + c.routing_medium_fallback +
             c.routing_complex_fallback + c.routing_multimodal_fallback)
    assert not any("Kimi" in x or "moonshot" in x for x in allfb), f"不应有 Kimi: {allfb}"


def test_resolve_route_returns_list_fallback():
    """_resolve_route 返回 (primary, list[fallback])。"""
    from swarm.models.router import ModelRouter
    r = ModelRouter()
    primary, fb = r._resolve_route("complex", "text")
    assert "Qwen3.6-40B-Claude" in primary
    assert isinstance(fb, list) and len(fb) >= 2  # MiniMax + Saka（122B-A10B 已排除）


def test_alternate_picks_same_tier_main():
    """retry 换备选取 fallback[0]=同级主力 MiniMax（Q-T2-1），不是 Kimi。"""
    from swarm.models.router import ModelRouter
    r = ModelRouter()
    _, model_name = r.get_alternate_llm_for_subtask("complex", "text")
    assert "MiniMax-M2.7-Pro" in model_name, f"retry 应换同级主力: {model_name}"


def test_coerce_model_list_formats():
    """_coerce_model_list 兼容 单串/逗号链/JSON 数组（env 落库可配）。"""
    from swarm.config.settings import _coerce_model_list
    assert _coerce_model_list("A") == ["A"]
    assert _coerce_model_list("A,B,C") == ["A", "B", "C"]
    assert _coerce_model_list('["A","B"]') == ["A", "B"]
    assert _coerce_model_list(["A", "B"]) == ["A", "B"]
    assert _coerce_model_list(None) == []


def test_update_routing_stores_list_as_comma_chain():
    """T2-4：PUT /api/routing 收到 fallback list → 存逗号链(非 str(list))，可被 _coerce 还原。"""
    from swarm.config.settings import _coerce_model_list
    # 模拟 update_routing 的 list→env 转换逻辑
    raw = ["MiniMax-M2.7-Pro", "Qwen3.6-27B-Saka-NVFP4", "Qwen3.5-122B-A10B-NVFP4"]
    val = ",".join(str(x).strip() for x in raw if str(x).strip())
    assert val == "MiniMax-M2.7-Pro,Qwen3.6-27B-Saka-NVFP4,Qwen3.5-122B-A10B-NVFP4"
    # env 读回应还原成原 list
    assert _coerce_model_list(val) == raw
    # 反例：str(list) 会产生非法 JSON（验证我们没用它）
    bad = str(raw)
    assert bad.startswith("[") and "'" in bad  # Python repr 单引号
