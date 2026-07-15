"""worker 主力并行轮转 + Brain 模型对调 验证。"""
from swarm.config.settings import ModelConfig, WorkerConfig


def test_brain_primary_fallback_configured_and_distinct():
    """脑主/备模型是部署配置（.env 单一事实源，运维可随时对调主备）。

    只断机制不变量：两者非空且互不相同（主备相同=切备失去意义）。不焊死具体
    模型名——2026-07-11 主备对调再次实证焊死断言必被运维动作打红（disciplines:
    禁结构焊死测试）。"""
    c = ModelConfig()
    assert c.brain_primary and isinstance(c.brain_primary, str)
    assert c.brain_fallback and isinstance(c.brain_fallback, str)
    assert c.brain_primary != c.brain_fallback


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
    pool = ["Qwopus3.6-27B-v2-NVFP4", "MiniMax-M2.7-Pro"]
    assigned = [pool[i % len(pool)] for i in range(4)]
    # 4 个子任务 → 两个模型各 2 个（均衡）
    assert assigned.count(pool[0]) == 2
    assert assigned.count(pool[1]) == 2


def test_override_model_carries_fallback_chain(monkeypatch):
    """治本：主力轮转 override(model_name 已设)必须走 get_llm_by_name(带难度 fallback 链)，
    而非 get_model_by_name(裸模型无 fallback)。否则 override 模型不可用(400 Model not found)
    时无链可切→worker 直接死→看守判死循环取消整任务(E2E 996 第三轮实测)。"""
    from unittest.mock import MagicMock
    import swarm.worker.agent as wa
    from swarm.types import FileScope, SubTask, SubTaskDifficulty

    calls = {"get_llm_by_name": None, "get_model_by_name": None}

    class _FakeRouter:
        def get_llm_by_name(self, model_name, difficulty="medium"):
            calls["get_llm_by_name"] = (model_name, difficulty)
            return MagicMock(name="llm_with_fallbacks")

        def get_model_by_name(self, model_name, temperature=0.2):
            calls["get_model_by_name"] = (model_name, temperature)
            return MagicMock(name="bare_llm")

        def get_worker_llm(self, strategy="cost_optimized"):
            return MagicMock(name="default_llm")

    monkeypatch.setattr(wa, "ModelRouter", _FakeRouter)
    monkeypatch.setattr(wa, "create_react_agent", lambda **kw: MagicMock(name="agent"))
    # C10（阶段4）语义演进：_get_worker_tools 按 (scope, intent) 裁剪——桩随签名
    monkeypatch.setattr(wa, "_get_worker_tools", lambda *a, **k: [])
    monkeypatch.setattr(wa, "build_worker_prompt", lambda **kw: "SYS")

    st = SubTask(id="st-1", description="d", difficulty=SubTaskDifficulty.COMPLEX,
                 scope=FileScope(writable=["a.java"], readable=["a.java"]))
    wa.create_worker_agent(subtask=st, scope=st.scope, model_name="Qwopus3.6-27B-v2-NVFP4")

    # 必须走带 fallback 的 get_llm_by_name，且难度透传正确
    assert calls["get_llm_by_name"] == ("Qwopus3.6-27B-v2-NVFP4", "complex"), calls
    # 绝不走裸模型 get_model_by_name（那是 bug 源）
    assert calls["get_model_by_name"] is None, calls
