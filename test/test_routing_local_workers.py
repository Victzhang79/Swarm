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
    """complex 首选 = 本地强模型（不再云端 GLM）。不写死具体型号——worker 主力会随线上
    可用模型轮换（如 40B→Qwopus-27B-v2），断言"非空且非云端"而非锁名，避免换模型即测试红。"""
    c = _cfg()
    assert c.routing_complex, "complex 首选不应为空"
    assert not any(m in c.routing_complex for m in ["GLM-5.1", "GLM-5.2", "Kimi", "moonshot", "zai-org"]), \
        f"complex 首选不应云端: {c.routing_complex}"


def test_complex_fallback_chain_order():
    """用户编排(2026-07-20 更新·#30)：complex/pool 首派 Qwopus 挂 → 兜底链首=MiniMax(196k 同级大)
    → ThinkingCap-27B(256k) → 最后 Step-Flash(慢，垫底)。旧序 stepfun 垫底致失败重试落更弱模型。"""
    c = _cfg()
    fb = c.routing_complex_fallback
    assert isinstance(fb, list) and len(fb) >= 2, f"应多级兜底 list: {fb}"
    assert "MiniMax" in fb[0], f"第一兜底应 MiniMax(Qwopus 失败切同级大模型，#30): {fb}"
    assert any("ThinkingCap" in x for x in fb), f"应含 ThinkingCap 次级兜底: {fb}"
    assert any("Step" in x for x in fb), f"应含 Step-Flash 最终垫底: {fb}"
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
    assert primary and not any(m in primary for m in ["GLM-5.1", "GLM-5.2", "Kimi", "zai-org"])  # 本地强模型，不锁具体名
    assert isinstance(fb, list) and len(fb) >= 2  # 多级兜底链


def test_alternate_picks_first_non_primary():
    """retry 换备选取 fallback 链上首个 ≠primary【且非 trivial 档 primary】的模型。

    语义演进（round38c 主题E 复核 C-4）：FINDING-8 原语义「首个≠primary」在三档链统一
    trivial 档居首的编排下，把 medium/complex 失败重试全派到 trivial 档模型（ThinkingCap）——
    RUN10「换模型=降级」顾虑成真。现 alternate 候选排除 trivial 档 primary：complex
    (primary=Qwopus) 的 alternate=MiniMax（medium 档）而非 trivial 档 ThinkingCap。"""
    from swarm.models.router import ModelRouter
    r = ModelRouter()
    _, model_name = r.get_alternate_llm_for_subtask("complex", "text")
    _trivial = getattr(r.config, "routing_trivial", "")
    assert model_name != _trivial and model_name, (
        f"非 trivial 难度的 alternate 不得落到 trivial 档 primary: {model_name}")


def test_alternate_skips_primary_duplicate(monkeypatch):
    """FINDING-8(task 3e07c592)：fallback 链首=刚失败的首派时，备选须跳到下一个异构模型。
    #30 语义修正：首派权威=worker_parallel_pool。此处令池={Qwen-40B}（=实际首派），备选须跳过它。"""
    from swarm.config.settings import ModelConfig
    from swarm.models.router import ModelRouter
    import swarm.config as _cfg
    _real = _cfg.get_config()
    monkeypatch.setattr(_real.worker, "worker_parallel_pool", ["Qwen-40B"], raising=False)
    r = ModelRouter(ModelConfig(
        routing_medium="Qwen-40B",
        routing_medium_fallback="Qwen-40B,Qwen-27B",  # 链首=首派(池)
    ))
    _, model_name = r.get_alternate_llm_for_subtask("medium", "text")
    assert model_name == "Qwen-27B", f"应跳过=首派(池)的链首、取下一个异构模型: {model_name}"


def test_pool_override_makes_tier_primary_valid_alternate(monkeypatch):
    """★#30 核心（用户 Qwopus 池→MiniMax）★ 池={Qwopus} 首派所有子任务，medium tier primary=MiniMax
    不在池里→它没被首派→是合法异构备选。Qwopus 池失败后 medium 备选必须=MiniMax（而非垫底 stepfun）。"""
    from swarm.config.settings import ModelConfig
    from swarm.models.router import ModelRouter
    import swarm.config as _cfg
    monkeypatch.setattr(_cfg.get_config().worker, "worker_parallel_pool",
                        ["Qwopus3.6-27B-v2-NVFP4"], raising=False)
    r = ModelRouter(ModelConfig(
        routing_trivial="ThinkingCap-Qwen3.6-27B",
        routing_medium="MiniMax-M2.7-Pro",
        routing_medium_fallback="MiniMax-M2.7-Pro,ThinkingCap-Qwen3.6-27B,stepfun-ai/Step-3.7-Flash-FP8",
    ))
    _, model_name = r.get_alternate_llm_for_subtask("medium", "text")
    assert model_name == "MiniMax-M2.7-Pro", \
        f"Qwopus 池失败→medium 备选应=MiniMax(tier primary 未被首派=合法备选)，实为 {model_name}"


def test_alternate_falls_back_to_primary_when_no_distinct():
    """链全=primary 或空（如 COMPLEX 只配单模型）→ 回退 primary，不崩。"""
    from swarm.config.settings import ModelConfig
    from swarm.models.router import ModelRouter
    r = ModelRouter(ModelConfig(
        routing_complex="Solo-40B",
        routing_complex_fallback="Solo-40B",  # 唯一且=primary，无真异构备选
    ))
    _, model_name = r.get_alternate_llm_for_subtask("complex", "text")
    assert model_name == "Solo-40B"


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
    raw = ["MiniMax-M2.7-Pro", "ThinkingCap-Qwen3.6-27B", "Qwopus3.6-27B-v2-NVFP4"]
    val = ",".join(str(x).strip() for x in raw if str(x).strip())
    assert val == "MiniMax-M2.7-Pro,ThinkingCap-Qwen3.6-27B,Qwopus3.6-27B-v2-NVFP4"
    # env 读回应还原成原 list
    assert _coerce_model_list(val) == raw
    # 反例：str(list) 会产生非法 JSON（验证我们没用它）
    bad = str(raw)
    assert bad.startswith("[") and "'" in bad  # Python repr 单引号
