"""CTO 复盘批次回归测试 — 验证一批已核实的安全/正确性修复。

覆盖：
- A-P0-1 向量污染：无嵌入服务时拒绝写随机向量（_embed_texts 返回 None；_phase_embed 跳过 upsert）。
- A-P0-2 安全扫描 SKIP=PASS：阻断模式下无任何真实扫描器执行 → fail-closed 阻断。
- A-P0-6 语雀 SSRF/路径穿越：namespace/doc_id 转义 + 拒绝 .. / @ + base scheme 校验 + 跨 host 重定向拒绝。
- A-P0-7 / A-P1-27 未授权读端点：补 _require_user/_require_perm（源码静态断言）。
- A-P1-01 rebase 死循环：clean merge 显式回写 rebase_subtask_ids=[]（源码静态断言）。
- A-P1-03 契约重试无计数：契约失败重试计数+上限+超限升级（源码静态断言）。
- A-P1-02 clarify 无限重问：答复后清空 tech_design_fact_issues + 轮数上限前置（源码静态断言）。
- A-P1-29 .env 非原子写：atomic_write_env 用 os.replace 原子改名 + 锁（行为 + 静态断言）。
"""
from __future__ import annotations

import inspect

# ─────────────────────────────────────────────────────────
# FIX 1 — A-P0-1 向量污染
# ─────────────────────────────────────────────────────────
def test_embed_texts_returns_none_without_service(monkeypatch):
    """无任何嵌入服务 + 未设 SWARM_ALLOW_RANDOM_EMBED → 返回 None（绝不返回随机向量）。"""
    import swarm.project.preprocess as pp

    monkeypatch.delenv("SWARM_ALLOW_RANDOM_EMBED", raising=False)

    # 让所有嵌入后端都失败
    def _boom(*a, **k):
        raise RuntimeError("no embed service")

    monkeypatch.setattr("swarm.knowledge.embed_client.embed_texts_sync", _boom, raising=False)
    # sentence_transformers 不存在 / requests / openai 也都失败：用环境隔离已足够；
    # 直接断言无服务时返回 None。
    result = pp._embed_texts(["hello world"])
    assert result is None, "无嵌入服务必须返回 None，不得返回随机占位向量"


def test_embed_texts_random_only_behind_flag(monkeypatch):
    """显式 SWARM_ALLOW_RANDOM_EMBED=1 时（仅本地测试）才允许随机向量回退。"""
    import swarm.project.preprocess as pp

    monkeypatch.setenv("SWARM_ALLOW_RANDOM_EMBED", "1")

    def _boom(*a, **k):
        raise RuntimeError("no embed service")

    monkeypatch.setattr("swarm.knowledge.embed_client.embed_texts_sync", _boom, raising=False)
    result = pp._embed_texts(["hello"])
    assert result is not None and len(result) == 1


def test_phase_embed_skips_upsert_on_none():
    """_phase_embed 源码：vectors 为 None / 数量不匹配 → 跳过 upsert 并标记 skipped。"""
    import swarm.project.preprocess as pp

    src = inspect.getsource(pp._phase_embed)
    assert "vectors is None" in src
    assert '"skipped": True' in src
    # 必须在跳过分支 return，且不调用 _store_vectors_qdrant（不写垃圾）
    assert "_store_vectors_qdrant" in src  # 正常路径仍调用
    skip_idx = src.index("vectors is None")
    store_idx = src.index("_store_vectors_qdrant")
    # 跳过判断在 store 调用之前（早返回保护）
    assert skip_idx < store_idx


# ─────────────────────────────────────────────────────────
# FIX 2 — A-P0-2 安全扫描 SKIP=PASS
# ─────────────────────────────────────────────────────────
def test_security_scan_fail_closed_no_scanner(monkeypatch, tmp_path):
    """阻断模式 + 无任何真实扫描器执行 → should_block=True（fail-closed）。"""
    import swarm.worker.security_scan as ss

    # 模拟所有外部工具都不存在
    monkeypatch.setattr(ss.shutil, "which", lambda name: None)
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")

    findings, should_block = ss.run_security_scan(str(tmp_path), "python", block_severity="critical")
    assert should_block is True, "无扫描器+阻断模式必须 fail-closed"
    assert any(f.rule_id == "fail-closed-no-scanner" for f in findings)


def test_security_scan_report_mode_never_blocks(monkeypatch, tmp_path):
    """report-only(none) + 无扫描器 → should_block=False（运维明示永不阻断）。"""
    import swarm.worker.security_scan as ss

    monkeypatch.setattr(ss.shutil, "which", lambda name: None)
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")

    findings, should_block = ss.run_security_scan(str(tmp_path), "python", block_severity="none")
    assert should_block is False
    assert not any(f.rule_id == "fail-closed-no-scanner" for f in findings)
    # A-P0-2 report-mode 可见性：必须有 INFO 级 coverage-zero 信号（"没扫"≠"干净"），但不阻断。
    cov = [f for f in findings if f.rule_id == "scan-coverage-zero"]
    assert len(cov) == 1, "report-only 模式下无扫描器应注入 scan-coverage-zero 可观测信号"
    assert cov[0].severity == ss.Severity.INFO
    assert ss._severity_gte(cov[0].severity, "low") is False  # INFO 永不触发任何阈值


def test_security_scan_report_mode_with_scanner_no_coverage_signal(monkeypatch, tmp_path):
    """report-only + 有扫描器真跑过且干净 → 不应注入 coverage-zero（避免噪声）。"""
    import swarm.worker.security_scan as ss

    # 模拟 bandit 存在且返回零发现：which 命中 bandit，_run_tool 返回干净 JSON。
    monkeypatch.setattr(ss.shutil, "which", lambda name: "/usr/bin/" + name if name == "bandit" else None)
    monkeypatch.setattr(ss, "_run_tool", lambda *a, **k: (0, '{"results": []}', ""))
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")

    findings, should_block = ss.run_security_scan(str(tmp_path), "python", block_severity="none")
    assert should_block is False
    assert not any(f.rule_id == "scan-coverage-zero" for f in findings)


def test_security_scan_default_block_severity_is_critical():
    """默认阻断阈值为 critical（确认本修复影响默认流，需谨慎收紧）。"""
    from swarm.config.settings import WorkerConfig

    assert WorkerConfig().security_block_severity == "critical"


# ─────────────────────────────────────────────────────────
# FIX 3 — A-P0-6 语雀 SSRF / 路径穿越
# ─────────────────────────────────────────────────────────
def test_yuque_rejects_path_traversal():
    from swarm.knowledge.ingest.sources import YuqueSource

    for bad in ("../../etc", "ns/..", "a/../b", "ns@evil.com", "a\\b", "//evil.com"):
        try:
            YuqueSource._safe_path_component(bad, allow_slash=True, label="namespace")
            assert False, f"应拒绝非法 namespace: {bad!r}"
        except RuntimeError:
            pass


def test_yuque_doc_id_rejects_slash():
    from swarm.knowledge.ingest.sources import YuqueSource

    # doc_id（slug）不允许 '/'
    try:
        YuqueSource._safe_path_component("a/b", allow_slash=False, label="doc_id")
        assert False, "doc_id 含 / 应被拒绝"
    except RuntimeError:
        pass
    # 合法 slug 被 URL 转义
    out = YuqueSource._safe_path_component("hello world", allow_slash=False, label="doc_id")
    assert out == "hello%20world"


def test_yuque_namespace_quoted():
    from swarm.knowledge.ingest.sources import YuqueSource

    out = YuqueSource._safe_path_component("user/re po", allow_slash=True, label="namespace")
    assert out == "user/re%20po"  # 保留内部 /，空格转义


def test_yuque_base_scheme_validated():
    from swarm.knowledge.ingest.sources import YuqueSource

    src = YuqueSource(namespace="u/r")
    src.base = "file:///etc/passwd"
    try:
        src._base_host()
        assert False, "非 http/https scheme 应被拒绝"
    except RuntimeError:
        pass
    src.base = "https://www.yuque.com/api/v2"
    assert src._base_host() == "www.yuque.com"


def test_yuque_get_json_rejects_cross_host_redirect():
    """_get_json 源码使用拒绝跨 host 重定向的自定义 opener。"""
    from swarm.knowledge.ingest import sources

    src = inspect.getsource(sources.YuqueSource._get_json)
    assert "build_opener" in src
    assert "跨 host" in src or "cross" in src.lower()


# ─────────────────────────────────────────────────────────
# FIX 4 — A-P0-7 / A-P1-27 未授权读端点
# ─────────────────────────────────────────────────────────
def _src(fn) -> str:
    return inspect.getsource(fn)


def test_observability_endpoints_gated():
    from swarm.api.routers import observability as obs

    for fn in (obs.obs_summary, obs.obs_latency, obs.obs_timeseries, obs.obs_slow):
        assert "_require_user(request)" in _src(fn), f"{fn.__name__} 未鉴权"


def test_config_read_endpoints_gated():
    from swarm.api.routers import config as cfg

    assert "_require_user(request)" in _src(cfg.get_config_endpoint)
    assert "_require_user(request)" in _src(cfg.list_models)
    assert "_require_user(request)" in _src(cfg.get_routing)
    assert "_require_user(request)" in _src(cfg.get_kb_embed_rerank)
    assert "_require_user(request)" in _src(cfg.get_notify_channels)
    # POST /api/config/test → 写权限
    assert '_require_perm(request, "config:write")' in _src(cfg.test_config)


def test_app_notification_milestone_endpoints_gated():
    """app.py 中通知/里程碑端点：源码文本断言闸门存在。"""
    import importlib

    mod = importlib.import_module("swarm.api.app")
    text = inspect.getsource(mod)
    # GET 端点用 _require_user
    for marker in (
        "async def get_notifications(",
        "async def get_unread_count(",
        "async def archive_notification_endpoint(",
        "async def archive_all_notifications_endpoint(",
        "async def list_milestones(",
    ):
        assert marker in text
    # 这些函数体都应含 _require_user(request)
    for fn_name in (
        "get_notifications",
        "get_unread_count",
        "archive_notification_endpoint",
        "archive_all_notifications_endpoint",
        "list_milestones",
    ):
        fn = getattr(mod, fn_name)
        assert "_require_user(request)" in inspect.getsource(fn), f"{fn_name} 未鉴权"
    # POST /api/milestones → 写权限
    assert '_require_perm(request, "config:write")' in inspect.getsource(mod.post_milestone_report)


# ─────────────────────────────────────────────────────────
# FIX 5 — A-P1-01 rebase 死循环
# ─────────────────────────────────────────────────────────
def test_merge_clean_path_resets_rebase_ids():
    """merge 节点 clean 路径显式回写 rebase_subtask_ids=[]（防 last-write-wins 残留）。"""
    from swarm.brain import nodes

    src = inspect.getsource(nodes)
    # 在 merge 节点里，out 初始化后应显式置空 rebase_subtask_ids
    assert 'out["rebase_subtask_ids"] = []' in src


# ─────────────────────────────────────────────────────────
# FIX 6 — A-P1-03 契约重试无计数
# ─────────────────────────────────────────────────────────
def test_contract_retry_has_counter_and_ceiling():
    """契约失败分支应有 subtask_retry_counts 计数 + 上限 + 超限升级。"""
    from swarm.brain import nodes

    src = inspect.getsource(nodes)
    # 定位契约分支
    idx = src.index('verification_failure") == "contract"')
    window = src[idx: idx + 1400]
    assert "subtask_retry_counts" in window
    assert "max_retries" in window
    assert "failure_escalated" in window
    assert "escalate" in window


# ─────────────────────────────────────────────────────────
# FIX 7 — A-P1-02 clarify 无限重问
# ─────────────────────────────────────────────────────────
def test_clarify_consumes_fact_issues_and_caps_rounds():
    from swarm.brain import planning_nodes

    src = inspect.getsource(planning_nodes)
    idx = src.index("false_premises = [")
    window = src[idx: idx + 2200]
    # 答复后清空 tech_design_fact_issues
    assert '"tech_design_fact_issues": []' in window
    # 轮数上限在虚假前提分支前置检查
    assert "_fact_max" in window or "clarify_rounds" in window


# ─────────────────────────────────────────────────────────
# FIX 8 — A-P1-29 .env 非原子写
# ─────────────────────────────────────────────────────────
def test_atomic_write_env_uses_replace_and_lock():
    from swarm.config import settings

    src = inspect.getsource(settings.atomic_write_env)
    assert "os.replace" in src or "_os.replace" in src, "必须用 os.replace 原子改名"
    # 锁存在
    assert hasattr(settings, "_ENV_WRITE_LOCK")


def test_atomic_write_env_writes_correct_content(tmp_path):
    from swarm.config.settings import atomic_write_env

    env = tmp_path / ".env"
    atomic_write_env(env, "A=1\nB=2\n")
    assert env.read_text(encoding="utf-8") == "A=1\nB=2\n"
    # 覆盖写不留临时文件
    atomic_write_env(env, "A=3\n")
    assert env.read_text(encoding="utf-8") == "A=3\n"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != ".env"]
    assert leftovers == [], f"不应残留临时文件: {leftovers}"


# ─────────────────────────────────────────────────────────
# FIX W1.3 — KB payload 索引必建 + 旧集合回退也带 project_id 过滤
# ─────────────────────────────────────────────────────────
def test_ensure_collection_builds_payload_index_even_when_exists():
    """集合已存在(预处理路径建出、无 payload 索引)时，ensure_collection 仍必须
    为 project_id/file_path/chunk_type 建 payload 索引（幂等），否则过滤查询全量扫描。"""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from swarm.knowledge.semantic_index import SemanticIndexer

    idx = SemanticIndexer()
    client = AsyncMock()
    # 模拟集合【已存在】
    coll = MagicMock()
    coll.name = idx._collection_name
    collections_resp = MagicMock()
    collections_resp.collections = [coll]
    client.get_collections.return_value = collections_resp
    idx._client = client

    asyncio.run(idx.ensure_collection())

    # 集合已存在 → 不应再 create_collection
    client.create_collection.assert_not_called()
    # 但 payload 索引必须每次都建（幂等），三个字段各一次
    indexed_fields = {
        call.kwargs.get("field_name")
        for call in client.create_payload_index.call_args_list
    }
    assert {"project_id", "file_path", "chunk_type"} <= indexed_fields, indexed_fields


def test_ensure_collection_tolerates_index_already_exists():
    """create_payload_index 抛"已存在"异常时 ensure_collection 不应崩（吞掉续跑）。"""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from swarm.knowledge.semantic_index import SemanticIndexer

    idx = SemanticIndexer()
    client = AsyncMock()
    coll = MagicMock()
    coll.name = idx._collection_name
    collections_resp = MagicMock()
    collections_resp.collections = [coll]
    client.get_collections.return_value = collections_resp
    client.create_payload_index.side_effect = RuntimeError("index already exists")
    idx._client = client

    # 不应抛异常
    asyncio.run(idx.ensure_collection())


def test_legacy_fallback_search_applies_project_filter():
    """旧集合(project_<id>)回退搜索路径必须带 project_id 过滤，禁止 must_filters=None
    导致跨项目返回他人数据（数据越权泄漏）。"""
    import asyncio
    from unittest.mock import AsyncMock

    from qdrant_client import models

    from swarm.knowledge.semantic_index import SemanticIndexer

    idx = SemanticIndexer()
    idx._client = AsyncMock()
    idx.set_embed_fn(AsyncMock(return_value=[[0.1] * 4]))

    captured = {"calls": []}

    async def fake_query(client, collection_name, query_vector, must_filters, top_k):
        captured["calls"].append((collection_name, must_filters))
        return []  # 主集合返回空 → 触发旧集合回退

    async def fake_exists(name):
        return True  # 旧集合存在

    idx._query_collection = fake_query
    idx._collection_exists = fake_exists

    asyncio.run(idx.search("proj-A", "some query", top_k=5))

    # 至少两次调用：主集合 + 旧集合回退
    assert len(captured["calls"]) >= 2
    legacy_call = [c for c in captured["calls"] if c[0] == "project_proj-A"]
    assert legacy_call, "应走旧集合回退路径"
    legacy_filters = legacy_call[0][1]
    assert legacy_filters is not None, "旧集合回退路径 must_filters 不得为 None（会跨项目泄漏）"
    # 过滤条件里必须含 project_id=proj-A
    keys = {f.key for f in legacy_filters if isinstance(f, models.FieldCondition)}
    assert "project_id" in keys, f"旧集合回退必须带 project_id 过滤: {keys}"


# ─────────────────────────────────────────────────────────
# FIX W1.1 — ultra tech_design 失败模块阻断静默 auto_accept
# ─────────────────────────────────────────────────────────
def test_failed_modules_block_auto_accept_plan():
    """tech_design_failed_modules 非空 → can_auto_accept_plan 拒绝放行（fail-fast）。"""
    from swarm.brain.gates import can_auto_accept_plan

    # 无失败模块 + 计划合法 → 放行
    allow, _ = can_auto_accept_plan({"plan_valid": True})
    assert allow is True

    # 有失败模块 → 拒绝放行
    allow, reason = can_auto_accept_plan({
        "plan_valid": True,
        "tech_design_failed_modules": [{"name": "payment", "idx": 3, "reason": "timeout"}],
    })
    assert allow is False
    assert "tech_design_incomplete" in reason
    assert "payment" in reason


def test_confirm_plan_escalates_on_failed_modules_under_auto_accept():
    """auto_accept + tech_design 有失败模块 → confirm 不得 ACCEPT，须 REJECT + 升级人工。"""
    from swarm.brain.nodes import confirm_plan
    from swarm.brain.state import Complexity, HumanDecision

    state = {
        "auto_accept": True,
        "plan_valid": True,
        "complexity": Complexity.ULTRA,
        "tech_design_failed_modules": [{"name": "auth", "idx": 1, "reason": "json parse"}],
    }
    out = confirm_plan(state)
    assert out["human_decision"] == HumanDecision.REJECT, "有失败模块绝不能 auto-ACCEPT"
    assert out.get("failure_escalated") is True, "须升级人工"
    assert out.get("failure_strategy") == "escalate"
    assert out.get("verification_failure") == "tech_design_incomplete"


def test_tech_design_surfaces_failed_modules_to_degraded_reasons():
    """tech_design 节点把 stage2_failed_modules 透传进 degraded_reasons + 专用 state 字段。"""
    import inspect

    from swarm.brain import planning_nodes

    src = inspect.getsource(planning_nodes.tech_design)
    # 返回 patch 里带 tech_design_failed_modules 字段
    assert '"tech_design_failed_modules"' in src
    # 失败模块时追加 degraded_reasons
    assert '"degraded_reasons"' in src
    assert "stage2_failed_modules" in src
