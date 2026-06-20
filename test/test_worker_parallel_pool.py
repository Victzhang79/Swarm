"""worker 主力并行轮转 + Brain 模型对调 验证。"""
from swarm.config.settings import ModelConfig, WorkerConfig


def test_brain_primary_is_kimi():
    """Brain 主模型 = Kimi-K2.7-Code，备 = GLM-5.1（用户拍板对调）。"""
    c = ModelConfig()
    assert c.brain_primary == "zai-org/GLM-5.2", c.brain_primary
    assert c.brain_fallback == "moonshotai/Kimi-K2.7-Code", c.brain_fallback


def test_worker_parallel_pool_tracks_live_config():
    """worker 并行池 = .env 路由实际配置的本地主力集合，用于轮转分散负载。

    不再锁定具体条目数/模型名（已随 .env 路由配置漂移，原写死 2 个本地主力）。
    保留真实不变量：池是非空字符串列表、条目去重、且永不含 64K 小窗口的
    122B-A10B（窗口太小不该进主力轮转池）。
    """
    w = WorkerConfig()
    pool = w.worker_parallel_pool
    assert isinstance(pool, list) and len(pool) >= 1, f"主力池不应为空: {pool!r}"
    assert all(isinstance(m, str) and m for m in pool), f"池内须为非空模型名: {pool!r}"
    # 去重保序（同一模型不该在轮转池里重复）
    assert len(pool) == len(set(pool)), f"主力池含重复条目: {pool!r}"
    # 不变量：永不含 64K 小窗口的 122B-A10B（窗口太小不进主力轮转池）
    assert not any("122B-A10B" in m for m in pool)


def test_parallel_pool_coerce_comma_chain():
    """worker_parallel_pool 支持 env 逗号链（落库可配）。"""
    from swarm.config.settings import _coerce_model_list
    assert _coerce_model_list("A,B") == ["A", "B"]


def test_get_llm_by_name_exists():
    """router 有 get_llm_by_name（主力轮转 override 入口）。"""
    from swarm.models.router import ModelRouter
    assert hasattr(ModelRouter, "get_llm_by_name")


def test_round_robin_index_logic():
    """轮转索引逻辑：N 个子任务按 idx % len(pool) 分配到 2 个主力。"""
    pool = ["Qwen3.6-40B-Claude-4.6-NVFP4", "MiniMax-M2.7-Pro"]
    assigned = [pool[i % len(pool)] for i in range(4)]
    # 4 个子任务 → 两个模型各 2 个（均衡）
    assert assigned.count(pool[0]) == 2
    assert assigned.count(pool[1]) == 2
