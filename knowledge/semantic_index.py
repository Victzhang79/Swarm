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

            # 创建 payload 索引以加速过滤
            await self._client.create_payload_index(
                collection_name=self._collection_name,
                field_name="project_id",
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
            await self._client.create_payload_index(
                collection_name=self._collection_name,
                field_name="file_path",
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
            await self._client.create_payload_index(
                collection_name=self._collection_name,
                field_name="chunk_type",
                field_schema=models.PayloadSchemaType.KEYWORD,
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
        1. 按 class/function/docstring 边界识别语义单元
        2. 过长的单元按 chunk_size 重叠切分
        3. 附加 file_path / module / class_name 元信息
        """
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
        self, project_id: str, chunks: list[Chunk], batch_size: int = 64
    ) -> int:
        """将 chunks 向量化并存入 Qdrant"""
        client = self._client_or_raise()
        total = 0

        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]
            texts = [c.content for c in batch]
            vectors = await self._embed_fn(texts)

            points = []
            for chunk, vector in zip(batch, vectors):
                point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{chunk.file_path}:{chunk.start_line}:{chunk.content[:64]}"))
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
                }
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
    ) -> int:
        """便捷方法: 切分 + 索引单个文件"""
        chunks = self.chunk_source_code(
            source, file_path, module_name,
            chunk_size=self._kb_config.chunk_size,
            chunk_overlap=self._kb_config.chunk_overlap,
        )
        return await self.index_chunks(project_id, chunks)

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
        must_filters = [
            models.FieldCondition(key="project_id", match=models.MatchValue(value=project_id))
        ]
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
                results = await self._query_collection(
                    client, legacy, query_vector, None, top_k,
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
    ) -> list[dict[str, Any]]:
        """语义搜索 + 简单分数重排

        使用 Qdrant 的 prefetch + rerank API（若可用）,
        否则先多取再按 Qdrant 原始分数降序截断。
        query_vector: 预算向量复用（避免重复 embed）。
        """
        retrieval_top_k = retrieval_top_k or self._kb_config.retrieval_top_k
        rerank_top_k = rerank_top_k or self._kb_config.rerank_top_k

        # 先多取
        candidates = await self.search(
            project_id, query, top_k=retrieval_top_k, query_vector=query_vector
        )

        import asyncio

        from swarm.knowledge.reranker import rerank_documents

        return await asyncio.to_thread(
            rerank_documents,
            query,
            candidates,
            top_k=rerank_top_k,
            text_key="content",
        )

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
        line_offset += lines_in_chunk
        if offset >= len(text):
            break
