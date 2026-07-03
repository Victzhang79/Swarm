"""Layer B — 语义检索: Qdrant vectorstore + bge-m3 embedding

负责:
- 文档按语义单元切分(方法/类签名/文档块)
- 向量化并存入 Qdrant
- metadata 附加(file_path, module, class_name)
- 语义搜索 + Qdrant prefetch + rerank
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from qdrant_client import AsyncQdrantClient, models
from qdrant_client.models import Distance, PointStruct, VectorParams

from swarm.config.settings import DatabaseConfig, KnowledgeConfig

logger = logging.getLogger(__name__)

# bge-m3 向量维度
BGE_M3_DIMENSION = 1024


class EmbeddingUnavailableError(RuntimeError):
    """embedding 服务不可用（返回零向量占位）→ 拒绝写入，避免污染检索/误删旧 chunk。"""

# Qdrant payload 索引版本（12.4）：标记 payload 形态版本，便于排查"首次增量更新后
# 向量库内容形态变化"。预处理全量(CodeGraph 符号嵌入)与增量(SemanticIndexer 语义分块)
# 两条路径写入形态不同，靠 index_source 区分、index_version 标版本。
INDEX_VERSION = "v1"
INDEX_SOURCE_SEMANTIC = "semantic"   # SemanticIndexer 语义分块（增量路径）
INDEX_SOURCE_CODEGRAPH = "codegraph"  # 预处理 CodeGraph 符号嵌入（全量路径）


def make_point_id(file_path: str, start_line: object, content: str = "") -> str:
    """A-P1-19：Qdrant point ID 的【单一来源】，只按 (file_path, start_line) 作键。

    背景：预处理全量(codegraph) 与增量(semantic) 写同一集合，先前各用不相交方案
    (blake2b int vs uuid5 str)，"P1-DEBT-04 统一"把两边都改成 uuid5 但 key 里含 content[:64]
    —— 而两条路径对同一 (file,line) 喂的 content 不同（codegraph=签名|文档|名，semantic=分块原文）
    → 仍产不同 uuid5 → 依旧不相交，未真正统一。

    本次彻底去掉 content：ID = uuid5(file_path:start_line)。同一逻辑 chunk（同文件同起始行）
    无论哪条路径写、内容是否更新，都产同一 point ID → 互相 upsert 去重，召回不再随
    "最后谁写"漂移。content 参数保留以兼容旧调用签名，但不再参与 ID 计算。

    迁移影响：旧的 content-based ID 与新 ID 不匹配。下次索引会按新 ID 写入新点，旧点成为
    孤儿，直到全量重索引 / 该 (file,line) 被重新写入覆盖。KB 可重建、最终一致，无需专门
    清理脚本——但若需立即清除孤儿，按 project_id 全量重索引即可。
    """
    key = f"{file_path}:{start_line if start_line is not None else 0}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


@dataclass
class Chunk:
    """语义切分后的一个 chunk"""
    content: str
    chunk_type: str              # method / class_signature / doc_block / free_text
    file_path: str
    module_name: str | None = None
    class_name: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class SemanticIndexer:
    """Layer B — 语义索引管理器

    使用 Qdrant 存储 chunk 向量，支持语义检索与 rerank。
    """

    # 模块级标志: 零向量占位只警告一次，避免刷屏
    _placeholder_warned: bool = False

    def __init__(
        self,
        db_config: DatabaseConfig | None = None,
        kb_config: KnowledgeConfig | None = None,
    ) -> None:
        self._db_config = db_config or DatabaseConfig()
        self._kb_config = kb_config or KnowledgeConfig()
        self._client: AsyncQdrantClient | None = None
        self._collection_name = self._db_config.qdrant_collection
        # 占位 embedding 函数 — 实际部署时替换为真实模型调用
        self._embed_fn = self._default_embed

    # ── 连接管理 ──────────────────────────────

    async def connect(self) -> None:
        """建立 Qdrant 连接并确保集合存在"""
        if self._client is not None:
            return  # TD2606-B16：幂等守卫——重复 connect 不再丢弃旧 Qdrant 客户端
        self._client = AsyncQdrantClient(
            url=self._db_config.qdrant_url,
            check_compatibility=False,
        )
        await self.ensure_collection()

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    async def ensure_collection(self) -> None:
        """创建 Qdrant 集合(如不存在)"""
        assert self._client
        collections = await self._client.get_collections()
        names = [c.name for c in collections.collections]
        if self._collection_name not in names:
            await self._client.create_collection(
                collection_name=self._collection_name,
                vectors_config=VectorParams(
                    size=BGE_M3_DIMENSION,
                    distance=Distance.COSINE,
                ),
            )
            logger.info("Created Qdrant collection: %s", self._collection_name)

        # 创建 payload 索引以加速过滤 —— 必须每次 ensure_collection 都执行（幂等）。
        # 预处理路径(project/preprocess.py)创建集合时不建这些索引；若仅在
        # "集合不存在"分支建索引，则集合已由预处理建出来时这里会跳过，导致
        # 所有带过滤的查询走未建索引的全量扫描。Qdrant create_payload_index
        # 可重复调用，对"已存在"容错（try/except 吞掉重复创建报错）。
        for field_name in ("project_id", "file_path", "chunk_type"):
            try:
                await self._client.create_payload_index(
                    collection_name=self._collection_name,
                    field_name=field_name,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                )
            except Exception as exc:  # noqa: BLE001 — 索引已存在等可容错
                logger.debug(
                    "create_payload_index(%s) on %s skipped: %s",
                    field_name, self._collection_name, exc,
                )

    def _client_or_raise(self) -> AsyncQdrantClient:
        if self._client is None:
            raise RuntimeError("SemanticIndexer not connected — call connect() first")
        return self._client

    # ── Embedding 占位 ──────────────────────────

    @staticmethod
    async def _default_embed(texts: list[str]) -> list[list[float]]:
        """默认 embedding：优先专用 embed 服务，不可用回退零向量(告警)。

        这是检索期 query 向量化入口，必须接真服务，否则向量检索全零向量=关闭。
        """
        from swarm.knowledge.embed_client import embed_texts_async
        vecs = await embed_texts_async(texts)
        if vecs is not None:
            return vecs
        if not SemanticIndexer._placeholder_warned:
            SemanticIndexer._placeholder_warned = True
            logger.warning(
                "⚠️  Using PLACEHOLDER zero-vector embedding in SemanticIndexer — "
                "vector search is DISABLED! 配置 SWARM_KB_EMBED_BASE_URL 指向真 bge-m3 服务。",
                stacklevel=2,
            )
        return [[0.0] * BGE_M3_DIMENSION for _ in texts]

    def set_embed_fn(self, fn) -> None:
        """替换 embedding 函数"""
        self._embed_fn = fn

    # ── 语义切分 ────────────────────────────────

    @staticmethod
    def chunk_source_code(
        source: str,
        file_path: str,
        module_name: str | None = None,
        class_name: str | None = None,
        chunk_size: int = 512,
        chunk_overlap: int = 50,
    ) -> list[Chunk]:
        """将源码按语义单元切分

        策略:
        1. W2.3：Java/TS/JS/Go 用 tree-sitter 按 类/方法/函数 节点切（语法树精准）；
           Python 走原有缩进/关键字识别；未知语言/解析失败 → 原有 free_text + 字符切兜底。
        2. 过长的单元按 chunk_size 重叠切分
        3. 附加 file_path / module / class_name 元信息
        """
        # W2.3：按扩展名分派多语言 tree-sitter 切分（Java 重点：RuoYi 是 Java 单体）。
        ts_lang = _ts_lang_for_path(file_path)
        if ts_lang is not None:
            ts_chunks = _chunk_with_treesitter(
                source, file_path, ts_lang, module_name, class_name,
                chunk_size, chunk_overlap,
            )
            if ts_chunks is not None:
                return ts_chunks
            # tree-sitter 不可用/解析失败 → 落到下方原有兜底（free_text + 字符切）

        lines = source.splitlines()
        chunks: list[Chunk] = []

        # 简单基于缩进的语义单元识别
        current_block: list[str] = []
        current_type = "free_text"
        block_start_line = 1
        current_class: str | None = class_name

        for i, line in enumerate(lines, start=1):
            stripped = line.strip()

            # 检测类定义
            if stripped.startswith("class ") and stripped.endswith(":"):
                if current_block:
                    _flush_block(
                        current_block, current_type, block_start_line,
                        i - 1, file_path, module_name, current_class,
                        chunk_size, chunk_overlap, chunks,
                    )
                current_class = stripped.split("(")[0].split(":")[0].replace("class ", "").strip()
                current_block = [line]
                current_type = "class_signature"
                block_start_line = i
                continue

            # 检测方法/函数定义
            if stripped.startswith("def ") or stripped.startswith("async def "):
                if current_block:
                    _flush_block(
                        current_block, current_type, block_start_line,
                        i - 1, file_path, module_name, current_class,
                        chunk_size, chunk_overlap, chunks,
                    )
                current_block = [line]
                current_type = "method"
                block_start_line = i
                continue

            # 检测文档块(多行注释)
            if stripped.startswith('"""') or stripped.startswith("'''"):
                if current_block and current_type != "doc_block":
                    _flush_block(
                        current_block, current_type, block_start_line,
                        i - 1, file_path, module_name, current_class,
                        chunk_size, chunk_overlap, chunks,
                    )
                    current_block = [line]
                    current_type = "doc_block"
                    block_start_line = i
                else:
                    current_block.append(line)
                continue

            current_block.append(line)

        # 最后一个块
        if current_block:
            _flush_block(
                current_block, current_type, block_start_line,
                len(lines), file_path, module_name, current_class,
                chunk_size, chunk_overlap, chunks,
            )

        return chunks

    # ── 写入 ────────────────────────────────────

    async def index_chunks(
        self, project_id: str, chunks: list[Chunk], batch_size: int = 64,
        *, index_generation: str | None = None,
    ) -> int:
        """将 chunks 向量化并存入 Qdrant。

        index_generation：本次写入代际标记。配合 prune_file_stale 实现 write-then-prune
        （先 upsert 新 chunk 打代际，再删本文件旧代际残留），消除"先删后索引"的空窗。
        """
        client = self._client_or_raise()
        total = 0

        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]
            texts = [c.content for c in batch]
            vectors = await self._embed_fn(texts)

            # 零向量占位 = embedding 服务不可用（真 bge-m3 不会返回全零）。绝不能 upsert：
            # ① 写零向量污染检索；② 更糟——reindex_file_atomic 写完会 prune 旧代际，等于
            # 删掉旧的有效 chunk 只留零向量。故检测到即抛出（在任何 upsert 之前），让
            # 原子重建中止 prune、旧 chunk 原样保留；调用方降级到重试队列。
            if any(not any(v) for v in vectors):
                raise EmbeddingUnavailableError(
                    "embedding 服务返回零向量占位，拒绝写入 Qdrant（避免污染检索/误删旧 chunk）"
                )

            points = []
            for chunk, vector in zip(batch, vectors):
                point_id = make_point_id(chunk.file_path, chunk.start_line, chunk.content)
                payload = {
                    "project_id": project_id,
                    "content": chunk.content,
                    "chunk_type": chunk.chunk_type,
                    "file_path": chunk.file_path,
                    "module_name": chunk.module_name,
                    "class_name": chunk.class_name,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    **chunk.metadata,
                    # 索引溯源（12.4）：放在 metadata 展开之后，确保系统字段权威、不被覆盖
                    "index_version": INDEX_VERSION,
                    "index_source": INDEX_SOURCE_SEMANTIC,
                }
                if index_generation is not None:
                    payload["index_generation"] = index_generation
                points.append(
                    PointStruct(id=point_id, vector=vector, payload=payload)
                )

            await client.upsert(
                collection_name=self._collection_name,
                points=points,
            )
            total += len(points)

        logger.info("Indexed %d chunks for project %s", total, project_id)
        return total

    async def index_source_file(
        self, project_id: str, source: str, file_path: str,
        module_name: str | None = None,
        *, index_generation: str | None = None,
    ) -> int:
        """便捷方法: 切分 + 索引单个文件。index_generation 透传给 index_chunks（write-then-prune）。"""
        chunks = self.chunk_source_code(
            source, file_path, module_name,
            chunk_size=self._kb_config.chunk_size,
            chunk_overlap=self._kb_config.chunk_overlap,
        )
        return await self.index_chunks(project_id, chunks, index_generation=index_generation)

    async def reindex_file_atomic(
        self, project_id: str, source: str, file_path: str,
        module_name: str | None = None,
    ) -> int:
        """单文件 write-then-prune 重建：先 upsert 新 chunk（打代际），成功后删本文件旧代际残留。

        替代"delete_by_file 然后 index_source_file"的先删后索引——后者在 index 失败时留下
        向量空窗。本法 index 失败则抛出且不 prune，旧 chunk 原样保留（无空窗），由调用方重试兜底。
        """
        import time as _time
        gen = str(_time.time_ns())
        n = await self.index_source_file(
            project_id, source, file_path, module_name, index_generation=gen,
        )
        await self.prune_file_stale(project_id, file_path, gen)
        return n

    async def prune_file_stale(
        self, project_id: str, file_path: str, keep_generation: str
    ) -> None:
        """删某文件【非 keep_generation 代际】的 chunks（write-then-prune 的 prune 步）。"""
        client = self._client_or_raise()
        await client.delete(
            collection_name=self._collection_name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(key="project_id", match=models.MatchValue(value=project_id)),
                        models.FieldCondition(key="file_path", match=models.MatchValue(value=file_path)),
                    ],
                    must_not=[
                        models.FieldCondition(
                            key="index_generation", match=models.MatchValue(value=keep_generation)
                        ),
                    ],
                )
            ),
        )

    # ── 检索 ────────────────────────────────────

    async def search(
        self,
        project_id: str,
        query: str,
        top_k: int | None = None,
        filter_dict: dict[str, Any] | None = None,
        query_vector: list[float] | None = None,
    ) -> list[dict[str, Any]]:
        """语义搜索: query → 向量 → Qdrant search

        query_vector: 预先算好的查询向量。传入则跳过 embedding（避免同一 query
        在多次 search 中重复向量化，省 LAN 往返）。

        返回格式: [{id, score, payload}, ...]
        """
        client = self._client_or_raise()
        top_k = top_k or self._kb_config.retrieval_top_k

        # 向量化 query（已传入向量则复用，不重复调远端 embed）
        if query_vector is None:
            query_vectors = await self._embed_fn([query])
            query_vector = query_vectors[0]

        # 构造过滤条件
        project_filter = models.FieldCondition(
            key="project_id", match=models.MatchValue(value=project_id)
        )
        must_filters = [project_filter]
        if filter_dict:
            for k, v in filter_dict.items():
                must_filters.append(
                    models.FieldCondition(key=k, match=models.MatchValue(value=v))
                )

        results = await self._query_collection(
            client,
            self._collection_name,
            query_vector,
            must_filters,
            top_k,
        )
        if not results:
            legacy = f"project_{project_id}"
            if await self._collection_exists(legacy):
                # 旧集合回退路径同样必须带 project_id 过滤，否则跨项目返回
                # 其他项目的点 = 数据越权泄漏。
                results = await self._query_collection(
                    client, legacy, query_vector, [project_filter], top_k,
                )
        return results

    async def _collection_exists(self, name: str) -> bool:
        client = self._client_or_raise()
        collections = await client.get_collections()
        return name in [c.name for c in collections.collections]

    async def _query_collection(
        self,
        client: AsyncQdrantClient,
        collection_name: str,
        query_vector: list[float],
        must_filters: list[models.FieldCondition] | None,
        top_k: int,
    ) -> list[dict[str, Any]]:
        query_filter = models.Filter(must=must_filters) if must_filters else None
        response = await client.query_points(
            collection_name=collection_name,
            query=query_vector,
            query_filter=query_filter,
            limit=top_k,
            with_payload=True,
        )
        return [
            {
                "id": str(p.id),
                "score": p.score,
                **(p.payload or {}),
            }
            for p in response.points
        ]

    async def search_with_rerank(
        self,
        project_id: str,
        query: str,
        retrieval_top_k: int | None = None,
        rerank_top_k: int | None = None,
        query_vector: list[float] | None = None,
        query_terms: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """语义搜索 + (可选)BM25 混合融合 + rerank 精排

        流程：向量召回 retrieval_top_k 候选 → 若 query_terms 提供且 bm25_weight>0
        则 hybrid 融合重排 → reranker 取 rerank_top_k。
        query_vector: 预算向量复用；query_terms: 关键词(英文+中文2gram)用于 BM25。
        """
        retrieval_top_k = retrieval_top_k or self._kb_config.retrieval_top_k
        rerank_top_k = rerank_top_k or self._kb_config.rerank_top_k

        # 先多取（向量召回）
        candidates = await self.search(
            project_id, query, top_k=retrieval_top_k, query_vector=query_vector
        )

        # BM25 混合融合（召回后重打分，零额外远端调用）
        bm25_w = getattr(self._kb_config, "hybrid_bm25_weight", 0.0) or 0.0
        if query_terms and bm25_w > 0 and candidates:
            try:
                from swarm.knowledge.hybrid import hybrid_fuse
                candidates = hybrid_fuse(
                    candidates, query_terms, bm25_weight=bm25_w, text_key="content"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("hybrid 融合失败(降级纯向量): %s", exc)

        import asyncio

        from swarm.knowledge.reranker import rerank_documents

        return await asyncio.to_thread(
            rerank_documents,
            query,
            candidates,
            top_k=rerank_top_k,
            text_key="content",
        )

    async def bm25_only_search(
        self,
        project_id: str,
        query_terms: list[str] | None = None,
        top_k: int | None = None,
        scroll_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """无向量降级检索（修复 12.7）：embedding 服务不可用时的优雅降级路径。

        不做向量相似度，改用 Qdrant scroll 按 project_id 过滤拉取候选 chunk，
        再用与 hybrid 相同口径的 BM25 对 content 关键词打分排序，取 top_k。
        这样 embed 挂了也能保住"关键词检索"能力，而非返回空或零向量噪声。

        query_terms: 已提取的查询关键词（英文 token + 中文 2-gram）。为空则只能
        靠 scroll 顺序返回（无排序信号），仍优于零向量噪声。
        scroll_limit: 候选池上限，默认 retrieval_top_k*10（保守，避免拉全库）。
        """
        from swarm.knowledge.hybrid import _bm25_scores, _tokenize_doc

        client = self._client_or_raise()
        top_k = top_k or self._kb_config.retrieval_top_k
        scroll_limit = scroll_limit or (self._kb_config.retrieval_top_k * 10)

        flt = models.Filter(
            must=[models.FieldCondition(key="project_id", match=models.MatchValue(value=project_id))]
        )
        points, _ = await client.scroll(
            collection_name=self._collection_name,
            scroll_filter=flt,
            limit=scroll_limit,
            with_payload=True,
            with_vectors=False,
        )
        candidates: list[dict[str, Any]] = [
            {"id": str(p.id), "score": 0.0, **(p.payload or {})} for p in points
        ]
        if not candidates:
            return []

        # BM25 关键词排序（无 query_terms 则保持 scroll 顺序）
        if query_terms:
            docs_terms = [_tokenize_doc(str(c.get("content") or "")) for c in candidates]
            scores = _bm25_scores([t.lower() for t in query_terms], docs_terms)
            for c, s in zip(candidates, scores):
                c["bm25_score"] = round(s, 4)
                c["score"] = round(s, 4)  # 让下游按 score 排序/筛选时有信号
            candidates.sort(key=lambda c: c.get("bm25_score", 0.0), reverse=True)

        return candidates[:top_k]

    # ── 删除 ────────────────────────────────────

    async def delete_by_file(self, project_id: str, file_path: str) -> None:
        """删除某文件的所有 chunks"""
        client = self._client_or_raise()
        await client.delete(
            collection_name=self._collection_name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(key="project_id", match=models.MatchValue(value=project_id)),
                        models.FieldCondition(key="file_path", match=models.MatchValue(value=file_path)),
                    ]
                )
            ),
        )

    async def delete_by_project(self, project_id: str) -> None:
        """删除某项目的所有 chunks"""
        client = self._client_or_raise()
        await client.delete(
            collection_name=self._collection_name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(key="project_id", match=models.MatchValue(value=project_id)),
                    ]
                )
            ),
        )


# ──────────────────────────────────────────────
# 内部工具函数
# ──────────────────────────────────────────────

def _flush_block(
    block: list[str],
    block_type: str,
    start: int,
    end: int,
    file_path: str,
    module_name: str | None,
    class_name: str | None,
    chunk_size: int,
    chunk_overlap: int,
    result: list[Chunk],
) -> None:
    """将当前代码块切成 Chunk 写入 result"""
    text = "\n".join(block)
    # 若不超长, 直接作为一个 chunk
    if len(text) <= chunk_size:
        result.append(Chunk(
            content=text,
            chunk_type=block_type,
            file_path=file_path,
            module_name=module_name,
            class_name=class_name,
            start_line=start,
            end_line=end,
        ))
        return

    # 超长 → 按字符重叠切分
    offset = 0
    line_offset = 0
    while offset < len(text):
        end_pos = min(offset + chunk_size, len(text))
        chunk_text = text[offset:end_pos]
        # 估算行号
        lines_in_chunk = chunk_text.count("\n")
        result.append(Chunk(
            content=chunk_text,
            chunk_type=block_type,
            file_path=file_path,
            module_name=module_name,
            class_name=class_name,
            start_line=start + line_offset,
            end_line=start + line_offset + lines_in_chunk,
        ))
        offset += chunk_size - chunk_overlap
        # P1-7：单行超长块 lines_in_chunk=0 → line_offset 不递增 → 相邻 chunk 同 (file,start_line)
        # → uuid5 point ID 相同互相覆盖静默丢块。至少递增 1 保证 start_line 唯一。
        line_offset += max(lines_in_chunk, 1)
        if offset >= len(text):
            break


# ════════════════════════════════════════════════
# W2.3 — tree-sitter 多语言切分
# ════════════════════════════════════════════════

# 扩展名 → tree-sitter 语言键。Python 不在此（走原 AST/缩进路径）。
_TS_EXT_TO_LANG = {
    ".java": "java",
    ".go": "go",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
}

# 各语言的「语义单元」节点类型（类/接口/方法/函数/构造器）。
_TS_CHUNK_NODE_TYPES = {
    "java": {
        "class_declaration", "interface_declaration", "enum_declaration",
        "record_declaration", "method_declaration", "constructor_declaration",
    },
    "go": {
        "function_declaration", "method_declaration", "type_declaration",
    },
    "javascript": {
        "function_declaration", "method_definition", "class_declaration",
        "generator_function_declaration",
    },
    "typescript": {
        "function_declaration", "method_definition", "class_declaration",
        "interface_declaration", "enum_declaration", "abstract_class_declaration",
    },
    "tsx": {
        "function_declaration", "method_definition", "class_declaration",
        "interface_declaration", "enum_declaration", "abstract_class_declaration",
    },
}

# 类型容器节点：遇到它则下钻其方法，而非把整类当一个巨块。
_TS_CONTAINER_NODE_TYPES = {
    "java": {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"},
    "go": set(),
    "javascript": {"class_declaration"},
    "typescript": {"class_declaration", "abstract_class_declaration", "interface_declaration"},
    "tsx": {"class_declaration", "abstract_class_declaration", "interface_declaration"},
}

_TS_PARSERS: dict[str, Any] = {}
_TS_LOAD_FAILED: set[str] = set()
_TS_WARNED = False


def _ts_lang_for_path(file_path: str) -> str | None:
    """按扩展名返回 tree-sitter 语言键；非目标语言返回 None。"""
    import os
    ext = os.path.splitext(file_path or "")[1].lower()
    return _TS_EXT_TO_LANG.get(ext)


def _get_ts_parser(lang_name: str):
    """惰性加载并缓存某语言的 tree-sitter Parser；不可用则返回 None（优雅降级）。"""
    global _TS_WARNED
    if lang_name in _TS_PARSERS:
        return _TS_PARSERS[lang_name]
    if lang_name in _TS_LOAD_FAILED:
        return None
    try:
        from tree_sitter import Language, Parser

        if lang_name == "java":
            import tree_sitter_java as ts_mod
            language = Language(ts_mod.language())
        elif lang_name == "go":
            import tree_sitter_go as ts_mod
            language = Language(ts_mod.language())
        elif lang_name == "javascript":
            import tree_sitter_javascript as ts_mod
            language = Language(ts_mod.language())
        elif lang_name == "typescript":
            import tree_sitter_typescript as ts_mod
            language = Language(ts_mod.language_typescript())
        elif lang_name == "tsx":
            import tree_sitter_typescript as ts_mod
            language = Language(ts_mod.language_tsx())
        else:
            _TS_LOAD_FAILED.add(lang_name)
            return None
        parser = Parser(language)
        _TS_PARSERS[lang_name] = parser
        return parser
    except Exception as exc:  # noqa: BLE001 — grammar 缺失/版本不兼容 → 降级
        _TS_LOAD_FAILED.add(lang_name)
        if not _TS_WARNED:
            _TS_WARNED = True
            logger.warning(
                "tree-sitter grammar 不可用(%s: %s)，多语言切分回退字符切兜底。"
                "如需精准切分请安装 tree-sitter + 对应 grammar。",
                lang_name, exc,
            )
        return None


def _chunk_with_treesitter(
    source: str,
    file_path: str,
    lang_name: str,
    module_name: str | None,
    class_name: str | None,
    chunk_size: int,
    chunk_overlap: int,
) -> list[Chunk] | None:
    """用 tree-sitter 按 类/方法/函数 节点切分。

    返回 None 表示 grammar 不可用或解析异常 → 调用方走原有兜底。
    返回 [] 表示成功解析但无可识别语义单元（空文件/纯声明）→ 调用方亦可视情况兜底，
    本实现此时返回 None 让兜底处理，避免丢内容。
    """
    parser = _get_ts_parser(lang_name)
    if parser is None:
        return None
    try:
        src_bytes = source.encode("utf-8", errors="ignore")
        tree = parser.parse(src_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.debug("tree-sitter parse 失败(%s, %s)：%s", lang_name, file_path, exc)
        return None

    node_types = _TS_CHUNK_NODE_TYPES.get(lang_name, set())
    container_types = _TS_CONTAINER_NODE_TYPES.get(lang_name, set())
    chunks: list[Chunk] = []

    def _node_text(node) -> str:
        return src_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")

    def _node_name(node) -> str | None:
        nm = node.child_by_field_name("name")
        if nm is not None:
            return src_bytes[nm.start_byte:nm.end_byte].decode("utf-8", errors="ignore")
        return None

    def _emit(node, chunk_type: str, enclosing_class: str | None) -> None:
        text = _node_text(node)
        start = node.start_point[0] + 1
        end = node.end_point[0] + 1
        if len(text) <= chunk_size:
            chunks.append(Chunk(
                content=text,
                chunk_type=chunk_type,
                file_path=file_path,
                module_name=module_name,
                class_name=enclosing_class or class_name,
                start_line=start,
                end_line=end,
                metadata={"chunker": "tree-sitter", "lang": lang_name},
            ))
        else:
            # 超长方法仍按字符重叠切，保留语义起点行号
            block = text.split("\n")
            _flush_block(
                block, chunk_type, start, end, file_path, module_name,
                enclosing_class or class_name, chunk_size, chunk_overlap, chunks,
            )

    def _walk(node, enclosing_class: str | None) -> None:
        for child in node.children:
            ctype = child.type
            if ctype in container_types:
                # 容器（类/接口）：发一个"类签名"chunk（仅头部，不含全部方法体），
                # 再下钻其方法，避免把整类塞成一个巨块。
                cname = _node_name(child) or enclosing_class
                body = child.child_by_field_name("body")
                header_end_byte = body.start_byte if body is not None else child.end_byte
                header_text = src_bytes[child.start_byte:header_end_byte].decode("utf-8", errors="ignore").strip()
                if header_text:
                    chunks.append(Chunk(
                        content=header_text,
                        chunk_type="class_signature",
                        file_path=file_path,
                        module_name=module_name,
                        class_name=cname,
                        start_line=child.start_point[0] + 1,
                        end_line=(body.start_point[0] + 1) if body is not None else child.end_point[0] + 1,
                        metadata={"chunker": "tree-sitter", "lang": lang_name},
                    ))
                _walk(child, cname)
            elif ctype in node_types:
                _emit(child, "method" if "method" in ctype or "constructor" in ctype else ctype, enclosing_class)
                # 方法内一般不再下钻（嵌套函数少见，足够用）
            else:
                _walk(child, enclosing_class)

    _walk(tree.root_node, class_name)

    if not chunks:
        # 解析成功但没抓到任何语义单元（如纯 import 文件）→ 让兜底处理，不丢内容。
        return None
    return chunks
