"""统一检索入口 — 4层知识库配合 + Rerank

检索流水线:
  Layer A 精确定位 → Layer B 语义扩展 → Layer C 全量注入 → Layer D 共现分析 → L5/L6 记忆检索

返回 SwarmRetrieverResult，供 Brain 直接消费。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from swarm.config.settings import DatabaseConfig, KnowledgeConfig
from swarm.knowledge.behavior_store import BehaviorStore
from swarm.knowledge.norms_store import NormsStore
from swarm.knowledge.semantic_index import SemanticIndexer
from swarm.knowledge.structure_index import StructureIndexer
from swarm.memory.store import MemoryStore
from swarm.types import KnowledgeContext

logger = logging.getLogger(__name__)


def _is_zero_vec(vec: list[float] | None, sample_size: int = 8) -> bool:
    """检测查询向量是否为零向量（embedding 服务不可用时的占位回退信号）。

    采样前若干维即可判定（bge-m3 零向量回退是全 0）。用于 Layer B 优雅降级（12.7）。
    """
    if not vec:
        return True
    return all(abs(x) < 1e-12 for x in vec[:sample_size])


@dataclass
class SwarmRetrieverResult:
    """检索结果封装"""
    context: KnowledgeContext
    stats: dict[str, Any] = field(default_factory=dict)


class SwarmRetriever:
    """统一检索入口 — 4层知识库 + L5/L6 记忆

    职责:
    - 从任务描述中提取关键词/模块/类名
    - 按 Layer A → B → C → D 顺序逐层检索
    - 合并 L5错题集 / L6成功模式集
    - 对全部结果做 Rerank
    - 返回 KnowledgeContext
    """

    def __init__(
        self,
        db_config: DatabaseConfig | None = None,
        kb_config: KnowledgeConfig | None = None,
    ) -> None:
        self._db_config = db_config or DatabaseConfig()
        self._kb_config = kb_config or KnowledgeConfig()

        # 各层组件(外部注入或懒连)
        self._struct: StructureIndexer | None = None
        self._semantic: SemanticIndexer | None = None
        self._norms: NormsStore | None = None
        self._behavior: BehaviorStore | None = None
        self._memory: MemoryStore | None = None

    # ── 组件注入 ──────────────────────────────

    def set_structure_indexer(self, indexer: StructureIndexer) -> None:
        self._struct = indexer

    def set_semantic_indexer(self, indexer: SemanticIndexer) -> None:
        self._semantic = indexer

    def set_norms_store(self, store: NormsStore) -> None:
        self._norms = store

    def set_behavior_store(self, store: BehaviorStore) -> None:
        self._behavior = store

    def set_memory_store(self, store: MemoryStore) -> None:
        self._memory = store

    async def connect_all(self) -> None:
        """便捷方法: 创建并连接所有组件"""
        if not self._struct:
            self._struct = StructureIndexer(self._db_config)
            await self._struct.connect()
        if not self._semantic:
            self._semantic = SemanticIndexer(self._db_config, self._kb_config)
            await self._semantic.connect()
        if not self._norms:
            self._norms = NormsStore(self._db_config)
            await self._norms.connect()
        if not self._behavior:
            self._behavior = BehaviorStore(self._db_config)
            await self._behavior.connect()
        if not self._memory:
            self._memory = MemoryStore(self._db_config)
            await self._memory.connect()

    async def close_all(self) -> None:
        if self._struct:
            await self._struct.close()
        if self._semantic:
            await self._semantic.close()
        if self._norms:
            await self._norms.close()
        if self._behavior:
            await self._behavior.close()
        if self._memory:
            await self._memory.close()

    # ── 主检索方法 ──────────────────────────────

    async def retrieve_for_brain(
        self,
        task_desc: str,
        project_id: str,
        extra_keywords: list[str] | None = None,
    ) -> SwarmRetrieverResult:
        """Brain 用的统一检索

        流水线:
        1. Layer A: 从任务描述提取关键词，精确定位符号和文件
        2. Layer B: 任务描述语义检索，扩展相关代码块
        3. Layer C: 全量注入项目规范
        4. Layer D: 基于已定位文件做共现分析
        5. L5/L6: 检索错题和成功模式
        6. Rerank: 简单分数重排
        """
        context: KnowledgeContext = {
            "struct": [],
            "semantic": [],
            "norms": [],
            "behavior": [],
            "mistakes": [],
            "successes": [],
        }
        stats: dict[str, Any] = {}

        # ── 项目摘要 & 预处理统计（供 Brain 理解项目全貌）──
        project_meta = await self._load_project_meta(project_id)
        if project_meta.get("summary"):
            context["project_summary"] = project_meta["summary"]
        if project_meta.get("preprocess_stats"):
            context["preprocess_stats"] = project_meta["preprocess_stats"]
        stats["has_project_summary"] = bool(project_meta.get("summary"))

        # ── 提取检索关键词 ────────────────────
        keywords = _extract_keywords(task_desc)
        if extra_keywords:
            keywords.extend(extra_keywords)

        # ── Layer A: 结构索引精确定位 ──────────
        try:
            struct_results = await self._retrieve_layer_a(project_id, keywords)
            context["struct"] = struct_results
            stats["struct_count"] = len(struct_results)
        except Exception as exc:
            logger.warning("Layer A retrieval failed: %s", exc)
            stats["struct_error"] = str(exc)

        # 收集 Layer A 定位到的文件路径
        located_files = _collect_file_paths(context.get("struct", []))

        # ── A→依赖图扩展（P0）──────────────────
        dependency_files: list[str] = []
        try:
            dependency_files = await self._expand_dependency_files(
                project_id, located_files, max_depth=2
            )
            if dependency_files:
                context["dependency_files"] = dependency_files
                context["struct"] = list(context.get("struct", [])) + [
                    {"file_path": fp, "symbol_name": "", "source": "dependency_graph"}
                    for fp in dependency_files
                ]
                located_files = list(dict.fromkeys(located_files + dependency_files))
                stats["deps_expanded_count"] = len(dependency_files)
        except Exception as exc:
            logger.warning("Dependency expansion failed: %s", exc)
            stats["deps_error"] = str(exc)

        context["affected_files"] = located_files
        stats["affected_files_count"] = len(located_files)

        # ── Layer B: 语义扩展 ──────────────────
        try:
            semantic_results = await self._retrieve_layer_b(
                project_id, task_desc, located_files, keywords=keywords
            )
            context["semantic"] = semantic_results
            stats["semantic_count"] = len(semantic_results)
        except Exception as exc:
            logger.warning("Layer B retrieval failed: %s", exc)
            stats["semantic_error"] = str(exc)

        # 补充: 语义检索命中的文件也纳入共现分析
        all_files = located_files + [
            r.get("file_path", "") for r in context.get("semantic", [])
        ]
        all_files = list(dict.fromkeys(f for f in all_files if f))  # 去重去空

        # ── Layer C: Harness 规范（按任务相关度筛选，非全量）──
        try:
            norms_results = await self._retrieve_layer_c(
                project_id, task_desc, keywords
            )
            context["norms"] = norms_results
            stats["norms_count"] = len(norms_results)
        except Exception as exc:
            logger.warning("Layer C retrieval failed: %s", exc)
            stats["norms_error"] = str(exc)

        # ── Layer D: 共现分析 ──────────────────
        try:
            behavior_results = await self._retrieve_layer_d(project_id, all_files)
            context["behavior"] = behavior_results
            stats["behavior_count"] = len(behavior_results)
        except Exception as exc:
            logger.warning("Layer D retrieval failed: %s", exc)
            stats["behavior_error"] = str(exc)

        # ── L5/L6: 记忆检索 ───────────────────
        # 注意：检索是只读操作，不在此处自增 occurrence/reuse 计数。
        # 原实现每次检索都对 top-5 自增权重，导致"被检索"等同于"被复用"，
        # 反复检索会人为推高权重、扭曲衰减。真正的复用计数应在模式实际被采纳时
        # （learn 阶段命中成功模式 / 错题重现）单独触发。
        if self._memory:
            try:
                # 宽召回(retrieval_top_k) → cross-encoder 精排 + 近因融合 → 截 rerank_top_k。
                # 复用已有 rerank 通路(ai.bit:8081 TEI bge-reranker-v2-m3，reranker.simple 格式)，
                # 服务不可用时优雅回退原(余弦+近因)序，不阻塞。
                wide = self._kb_config.retrieval_top_k or 20
                mistakes = await self._memory.query_mistakes(
                    project_id, task_desc, top_k=wide
                )
                successes = await self._memory.query_successes(
                    project_id, task_desc, top_k=wide
                )
                mistakes = await self._rerank_memory(task_desc, mistakes)
                successes = await self._rerank_memory(task_desc, successes)
                context["mistakes"] = mistakes
                context["successes"] = successes
                stats["mistakes_count"] = len(mistakes)
                stats["successes_count"] = len(successes)
            except Exception as exc:
                logger.warning("L5/L6 retrieval failed: %s", exc)
                stats["memory_error"] = str(exc)

        # ── Rerank: 简单分数合并 ──────────────
        context = self._rerank(context, task_desc)
        context = await self._apply_hybrid_fusion(context, project_id)

        logger.info(
            "retrieve_for_brain complete: struct=%d semantic=%d norms=%d "
            "behavior=%d mistakes=%d successes=%d",
            stats.get("struct_count", 0),
            stats.get("semantic_count", 0),
            stats.get("norms_count", 0),
            stats.get("behavior_count", 0),
            stats.get("mistakes_count", 0),
            stats.get("successes_count", 0),
        )

        return SwarmRetrieverResult(context=context, stats=stats)

    async def _load_project_meta(self, project_id: str) -> dict[str, Any]:
        """加载项目摘要与预处理统计"""
        import asyncio

        def _query() -> dict[str, Any]:
            import psycopg

            from swarm.config.settings import DatabaseConfig
            cfg = DatabaseConfig()
            conn = psycopg.connect(cfg.postgres_uri, autocommit=True)
            meta: dict[str, Any] = {}
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT description FROM projects WHERE id = %s",
                        (project_id,),
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        meta["summary"] = str(row[0])[:4000]
                    cur.execute(
                        """
                        SELECT scan_stats, index_stats, embed_stats, analysis_stats
                        FROM preprocess_progress WHERE project_id = %s
                        """,
                        (project_id,),
                    )
                    prow = cur.fetchone()
                    if prow:
                        meta["preprocess_stats"] = {
                            "scan": prow[0] if isinstance(prow[0], dict) else {},
                            "index": prow[1] if isinstance(prow[1], dict) else {},
                            "embed": prow[2] if isinstance(prow[2], dict) else {},
                            "analysis": prow[3] if isinstance(prow[3], dict) else {},
                        }
            finally:
                conn.close()
            return meta

        return await asyncio.to_thread(_query)

    # ── 各层检索 ──────────────────────────────

    async def _retrieve_layer_a(
        self, project_id: str, keywords: list[str]
    ) -> list[dict[str, Any]]:
        """Layer A: 结构索引精确查找"""
        if not self._struct:
            return []

        results: list[dict[str, Any]] = []
        for kw in keywords[:10]:  # 限制关键词数量
            # 按名称查符号
            symbols = await self._struct.query_symbols_by_name(project_id, kw)
            results.extend(symbols)

            # 按类名查
            class_symbols = await self._struct.query_symbols_by_class(project_id, kw)
            results.extend(class_symbols)

            # 按文件路径模糊查（补盲区：关键词是模块/文件名而非符号名时，
            # 如 'parser'→src/dotenv/parser.py，前两种查法全空但这个能命中）
            if len(kw) >= 3 and not _is_cjk(kw):  # 仅对英文 token 做文件名匹配，避免中文 2-gram 噪声
                file_symbols = await self._struct.query_symbols_by_file_keyword(
                    project_id, kw, limit=15
                )
                results.extend(file_symbols)

        # 去重(按 file_path + symbol_name)
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for r in results:
            key = f"{r.get('file_path', '')}:{r.get('symbol_name', '')}"
            if key not in seen:
                seen.add(key)
                deduped.append(r)

        return deduped[:25]  # 限制数量（Brain 侧还会再截断）

    async def _expand_dependency_files(
        self,
        project_id: str,
        seed_files: list[str],
        *,
        max_depth: int = 2,
    ) -> list[str]:
        """Layer A 定位文件 → 依赖图传递扩展（BFS）。"""
        if not self._struct or not seed_files:
            return []

        expanded: list[str] = []
        seen = set(seed_files)
        for fp in seed_files[:10]:
            deps = await self._struct.query_transitive_deps(
                project_id, fp, max_depth=max_depth
            )
            for dep in deps:
                if dep not in seen:
                    seen.add(dep)
                    expanded.append(dep)
        return expanded

    async def _retrieve_layer_b(
        self, project_id: str, query: str,
        priority_files: list[str] | None = None,
        keywords: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Layer B: 语义扩展检索（向量 + BM25 混合）"""
        if not self._semantic:
            return []

        # 同一 query 向量化【一次】，复用到所有 search（priority 各文件 + 全局）。
        # 此前每个 priority_file + 全局各 embed 一次(最多 6 次相同 query)，纯浪费 LAN 往返。
        query_vector = None
        try:
            vecs = await self._semantic._embed_fn([query])  # noqa: SLF001
            if vecs:
                query_vector = vecs[0]
        except Exception as exc:  # noqa: BLE001
            logger.debug("query 预向量化失败(降级为各自 embed): %s", exc)

        # ── 优雅降级（修复 12.7）：embedding 不可用(零向量/None)时，不用零向量污染
        #    向量检索，改走 BM25-only 关键词检索保住基本召回能力。──
        if query_vector is None or _is_zero_vec(query_vector):
            # TD2606-B11：本次检索走 BM25-only 关键词降级（无语义召回）。置标记供 service 透传给
            # Brain，使其知道"拿到的是关键词召回、非完整语义召回"，不静默当完整上下文规划。
            self._embed_degraded_active = True
            if not getattr(self, "_embed_degraded_warned", False):
                logger.warning(
                    "[Layer B] embedding 服务不可用(零向量) — KB 语义检索降级为 BM25 关键词检索。"
                    "请检查 SWARM_KB_EMBED_* 配置或 embed 服务可用性。"
                )
                self._embed_degraded_warned = True
            try:
                bm25_results = await self._semantic.bm25_only_search(
                    project_id, query_terms=keywords,
                )
                return bm25_results
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Layer B] BM25 降级检索失败: %s", exc)
                return []

        # 在指定文件中优先检索(若 Layer A 有结果)
        results: list[dict[str, Any]] = []

        if priority_files:
            max_pf = getattr(self._kb_config, "max_priority_files", 5)
            pf_top_k = getattr(self._kb_config, "priority_file_top_k", 3)
            for fp in priority_files[:max_pf]:
                file_results = await self._semantic.search(
                    project_id, query,
                    top_k=pf_top_k,
                    filter_dict={"file_path": fp},
                    query_vector=query_vector,
                )
                results.extend(file_results)

        # 全局补充
        global_results = await self._semantic.search_with_rerank(
            project_id, query,
            retrieval_top_k=self._kb_config.retrieval_top_k,
            rerank_top_k=self._kb_config.rerank_top_k,
            query_vector=query_vector,
            query_terms=keywords,
        )
        results.extend(global_results)

        # 去重
        seen_ids: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for r in results:
            rid = r.get("id", id(r))
            if rid not in seen_ids:
                seen_ids.add(rid)
                deduped.append(r)

        # P1-10：语义相似度阈值过滤（兑现 semantic_score_threshold 配置语义："向量相似度
        # 低于此值丢弃，0=不过滤"）。按原始向量相似度 score 判（rerank 前的召回信号）。
        threshold = float(getattr(self._kb_config, "semantic_score_threshold", 0.0) or 0.0)
        if threshold > 0.0:
            deduped = [r for r in deduped if float(r.get("score", 0.0) or 0.0) >= threshold]

        return deduped

    async def _retrieve_layer_c(
        self,
        project_id: str,
        task_desc: str = "",
        keywords: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Layer C: Harness 工程规范 — 按任务关键词相关度排序，取 top-N"""
        if not self._norms:
            return []
        all_norms = await self._norms.get_all_norms(project_id, active_only=True)
        if not all_norms:
            return []

        kws = keywords or (_extract_keywords(task_desc) if task_desc else [])
        if not kws:
            return all_norms[:15]

        scored: list[tuple[float, dict[str, Any]]] = []
        for norm in all_norms:
            text = f"{norm.get('title', '')} {norm.get('content', '')}".lower()
            score = float(norm.get("priority", 5))
            for kw in kws:
                if kw.lower() in text:
                    score += 3.0
            scored.append((score, norm))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [n for _, n in scored[:15]]

    async def _retrieve_layer_d(
        self, project_id: str, files: list[str]
    ) -> list[dict[str, Any]]:
        """Layer D: 共现分析 + 热点"""
        if not self._behavior:
            return []

        results: list[dict[str, Any]] = []

        # 每个文件的共现
        for fp in files[:10]:
            co_files = await self._behavior.get_co_occurring_files(
                project_id, fp, top_k=5
            )
            for cf in co_files:
                cf["trigger_file"] = fp
                results.append(cf)

        # 补充全局热点
        hotspots = await self._behavior.get_hotspot_files(project_id, top_k=10)
        for hs in hotspots:
            hs["type"] = "hotspot"
            results.append(hs)

        # MR 历史（Layer D 扩展）
        try:
            mr_items = await self._retrieve_mr_history(project_id, files)
            results.extend(mr_items)
        except Exception as exc:
            logger.debug("MR history retrieval skipped: %s", exc)

        return results

    async def _retrieve_mr_history(
        self, project_id: str, files: list[str]
    ) -> list[dict[str, Any]]:
        if not self._struct:
            return []
        conn = self._struct._conn_or_raise()
        from swarm.knowledge.mr_history import MR_HISTORY_DDL, query_mr_history_for_files

        async with conn.cursor() as cur:
            await cur.execute(MR_HISTORY_DDL)
            return await query_mr_history_for_files(cur, project_id, files, top_k=5)

    # ── Rerank ──────────────────────────────────

    def _rerank(self, context: KnowledgeContext, query: str) -> KnowledgeContext:
        """简单 Rerank: 按各层结果的相关度排序

        策略:
        - struct 结果不变(已精确)
        - semantic 按 score 降序
        - norms 按 priority 降序
        - behavior 按 co_count 降序
        - mistakes / successes 按 similarity 降序
        """
        if context.get("semantic"):
            # P1-8：cross-encoder 精排写入 rerank_score，须优先按它排序，否则被原始 score 覆盖、
            # rerank 形同无效。缺 rerank_score（未精排）时回退 score，不回归。
            context["semantic"].sort(
                key=lambda x: x.get("rerank_score", x.get("score", 0.0)), reverse=True
            )

        if context.get("norms"):
            context["norms"].sort(
                key=lambda x: x.get("priority", 0), reverse=True
            )

        if context.get("behavior"):
            context["behavior"].sort(
                key=lambda x: x.get("co_count", x.get("mod_count", 0)), reverse=True
            )

        # 已 cross-encoder 精排+近因融合的，优先用 memory_rank_score 保序；否则回退 similarity。
        if context.get("mistakes"):
            context["mistakes"].sort(
                key=lambda x: x.get("memory_rank_score", x.get("similarity", 0.0)), reverse=True
            )

        if context.get("successes"):
            context["successes"].sort(
                key=lambda x: x.get("memory_rank_score", x.get("similarity", 0.0)), reverse=True
            )

        return context

    # 近因融合地板：与 store.RECENCY_RANK_FLOOR 同义——精排语义分至多被近因打 5 折。
    _MEM_RECENCY_FLOOR = 0.5

    async def _rerank_memory(
        self, query: str, items: list[dict[str, Any]], text_key: str = "description"
    ) -> list[dict[str, Any]]:
        """L5/L6 cross-encoder 精排 + 近因融合，截 rerank_top_k。

        宽召回候选 → TEI bge-reranker(simple) 现成通路打语义分 → 乘近因因子(effective_weight)
        → 截断。服务不可用/异常时优雅回退原(余弦+近因)序，绝不阻塞主检索。
        """
        top_k = self._kb_config.rerank_top_k or 5
        if not items or len(items) <= 1:
            return items[:top_k]
        try:
            import asyncio

            from swarm.knowledge.reranker import rerank_documents
            reranked = await asyncio.to_thread(
                rerank_documents, query, items, top_k=len(items), text_key=text_key
            )
            floor = self._MEM_RECENCY_FLOOR
            for d in reranked:
                base = d.get("rerank_score")
                if base is None:
                    base = d.get("similarity", 0.0)
                eff = float(d.get("effective_weight", 1.0) or 0.0)
                eff = min(max(eff, 0.0), 1.0)
                d["memory_rank_score"] = base * (floor + (1.0 - floor) * eff)
            reranked.sort(key=lambda x: x.get("memory_rank_score", 0.0), reverse=True)
            return reranked[:top_k]
        except Exception as exc:  # noqa: BLE001
            logger.warning("L5/L6 rerank 失败(回退原序): %s", exc)
            return items[:top_k]

    async def _apply_hybrid_fusion(
        self, context: KnowledgeContext, project_id: str = ""
    ) -> KnowledgeContext:
        """Layer A + B 融合打分 — 精确定位与语义扩展联合排序。

        增强逻辑(优雅降级):
        - 时间权重: 最近修改的文件得分更高
        - 共现交叉过滤: 与高分文件共现频繁的文件提升权重
        """
        scores: dict[str, float] = {}
        for r in context.get("struct", []):
            fp = r.get("file_path", "")
            if fp:
                scores[fp] = scores.get(fp, 0.0) + 2.0
        for r in context.get("semantic", []):
            fp = r.get("file_path", "")
            if fp:
                scores[fp] = scores.get(fp, 0.0) + float(
                    r.get("score", r.get("rerank_score", 0.5))
                )

        # ── 时间权重: 文件越新得分越高 ──
        file_times = await self._load_file_mod_times(list(scores.keys()), project_id)
        if file_times:
            scores = _apply_time_decay(scores, file_times)

        # ── 共现交叉过滤: 与高分文件共现频繁 → 提升权重 ──
        scores = await self._apply_co_occurrence_boost(scores, project_id)

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        context["hybrid_ranked_files"] = [fp for fp, _ in ranked[:20]]
        context["hybrid_scores"] = dict(ranked[:20])
        return context

    async def _load_file_mod_times(
        self, file_paths: list[str], project_id: str = ""
    ) -> dict[str, float]:
        """从 kb_file_index 查询文件最后修改时间(UNIX 时间戳)。

        kb_file_index 以 project_id 为键，必须按 project_id 过滤，否则同名文件
        跨项目碰撞会取到错误项目的修改时间（时间权重污染）。

        优雅降级: 查不到返回空字典，调用方不加权。
        """
        if not file_paths or not self._struct:
            return {}
        try:
            conn = self._struct._conn_or_raise()
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT file_path, EXTRACT(EPOCH FROM last_modified) AS ts
                    FROM kb_file_index
                    WHERE project_id = %s AND file_path = ANY(%s)
                    """,
                    (project_id, file_paths),
                )
                rows = await cur.fetchall()
            return {r[0]: float(r[1]) for r in rows if r[1] is not None}
        except Exception as exc:
            logger.debug("文件修改时间查询失败，跳过时间权重: %s", exc)
            return {}

    async def _apply_co_occurrence_boost(
        self, scores: dict[str, float], project_id: str = ""
    ) -> dict[str, float]:
        """共现交叉过滤: 高分文件的共现文件提升权重。

        优雅降级: behavior store 不可用或查不到共现数据时不加权。
        """
        if not self._behavior or not scores:
            return scores
        try:
            # 取 top-5 高分文件作为锚点
            top_files = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:5]
            # 查询每个锚点的共现文件
            co_scores: dict[str, float] = {}
            for fp, _score in top_files:
                try:
                    co_files = await self._behavior.get_co_occurring_files(
                        project_id, fp, top_k=5
                    )
                except Exception:
                    continue
                for cf in co_files:
                    co_fp = cf.get("file_path", "")
                    co_cnt = cf.get("co_count", 0)
                    if co_fp and co_fp in scores and co_cnt > 0:
                        co_scores[co_fp] = co_scores.get(co_fp, 0.0) + co_cnt

            if not co_scores:
                return scores

            # 归一化: 最大共现计数作为分母，加权系数 0.2
            max_co = max(co_scores.values())
            for fp, co_val in co_scores.items():
                boost = 1.0 + 0.2 * (co_val / max_co)
                scores[fp] = scores[fp] * boost
        except Exception as exc:
            logger.debug("共现交叉过滤失败，跳过: %s", exc)
        return scores


# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────

def _is_cjk(token: str) -> bool:
    """token 是否含中日韩字符（用于跳过中文 2-gram 的文件名匹配）。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in token)


def _extract_keywords(task_desc: str) -> list[str]:
    """从任务描述中提取检索关键词(简单启发式)

    策略:
    - 提取驼峰式命名拆分为单词
    - 提取下划线命名拆分为单词
    - 过滤停用词
    - 保留 2 字符以上的 token
    - 中文: 连续中文字符 2-gram 滑窗切分，过滤中文停用词
    """
    import re

    stop_words = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will",
        "would", "could", "should", "may", "might", "shall", "can",
        "need", "dare", "ought", "used", "to", "of", "in", "for",
        "on", "with", "at", "by", "from", "as", "into", "through",
        "during", "before", "after", "above", "below", "between",
        "out", "off", "over", "under", "again", "further", "then",
        "once", "here", "there", "when", "where", "why", "how",
        "all", "each", "few", "more", "most", "other", "some",
        "such", "no", "not", "only", "same", "so", "than", "too",
        "very", "just", "because", "but", "and", "or", "if", "that",
        "this", "it", "its", "add", "update", "modify", "change",
        "create", "delete", "remove", "fix", "implement", "make",
    }

    # 中文停用词
    cn_stop_words = {
        "的", "了", "和", "给", "加", "修改", "实现", "创建",
        "删除", "移除", "修复", "更新", "在", "是", "有", "不",
        "也", "都", "把", "被", "让", "用", "为", "从", "到",
        "这", "那", "个", "一", "就", "要", "会", "能", "可以",
        "与", "及", "等", "对", "将", "其", "或", "但", "而",
        "又", "很", "吗", "呢", "吧", "啊", "地", "得", "着",
        "过", "来", "去", "上", "下", "中", "里", "外", "前",
        "后", "时", "所", "之", "以", "于", "则", "已", "还",
    }

    # ── 中文关键词提取: 2-gram 滑窗 ──
    cn_keywords: list[str] = []
    for cn_chunk in re.findall(r"[\u4e00-\u9fff]+", task_desc):
        # 连续中文串: 取所有相邻字符对
        if len(cn_chunk) >= 2:
            for i in range(len(cn_chunk) - 1):
                bigram = cn_chunk[i : i + 2]
                # 整词作为停用词则跳过(如 "修改"、"实现")
                if bigram in cn_stop_words:
                    continue
                cn_keywords.append(bigram)
        elif cn_chunk not in cn_stop_words:
            # 单字(极少情况)也保留
            cn_keywords.append(cn_chunk)

    # 驼峰式拆分
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", task_desc)
    # 下划线/连字符拆分
    text = text.replace("_", " ").replace("-", " ")
    # 非字母非中文替换为空格(保留中文)
    text = re.sub(r"[^a-zA-Z\u4e00-\u9fff\s]", " ", text)

    tokens = text.lower().split()
    keywords = [
        t for t in tokens
        if len(t) >= 2 and t not in stop_words
        # 纯中文 token 跳过(已由 2-gram 处理)
        and not re.match(r"^[\u4e00-\u9fff]+$", t)
    ]

    # 去重保持顺序
    seen: set[str] = set()
    unique: list[str] = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            unique.append(kw)

    # 也保留原始驼峰式 token(如 className → className + Class + Name)
    camel_tokens = re.findall(r"[A-Z][a-z]+[A-Z][a-z]+[A-Za-z]*", task_desc)
    for ct in camel_tokens:
        if ct not in seen:
            seen.add(ct)
            unique.append(ct)

    # 中文关键词去重(放在英文去重之后，共享 seen 集合)
    for kw in cn_keywords:
        if kw not in seen:
            seen.add(kw)
            unique.append(kw)

    return unique


def _collect_file_paths(struct_results: list[dict[str, Any]]) -> list[str]:
    """从结构索引结果中收集文件路径"""
    paths: list[str] = []
    seen: set[str] = set()
    for r in struct_results:
        fp = r.get("file_path", "")
        if fp and fp not in seen:
            seen.add(fp)
            paths.append(fp)
    return paths


def _apply_time_decay(
    scores: dict[str, float], file_times: dict[str, float]
) -> dict[str, float]:
    """时间衰减加权: 文件越新得分越高。

    公式: score *= (1 + alpha * recency_factor)
    - alpha = 0.2 (时间权重占比 20%)
    - recency_factor ∈ [0, 1]: 最近修改 → 1, 最早修改 → 0
    - 查不到时间的文件不加分也不扣分(recency_factor=0)
    """
    if not file_times:
        return scores

    alpha = 0.2
    min_ts = min(file_times.values())
    max_ts = max(file_times.values())
    span = max_ts - min_ts if max_ts > min_ts else 1.0

    for fp, base_score in scores.items():
        ts = file_times.get(fp)
        if ts is not None:
            recency = (ts - min_ts) / span
            scores[fp] = base_score * (1.0 + alpha * recency)
    return scores
