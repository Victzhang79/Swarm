"""知识检索服务 — Brain / Worker 共享入口

- 进程内 SwarmRetriever 单例（懒连接）
- Worker 上下文（project_id）供 query_knowledge_base 使用
- 同步桥接（Tool 内调用 async 检索）
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from contextvars import ContextVar
from typing import Any

from swarm.knowledge.retriever import SwarmRetriever
from swarm.tracing import PHASE_2, swarm_traceable
from swarm.types import KnowledgeContext

logger = logging.getLogger(__name__)

# Brain 编排注入上限 — 按任务检索后截断，避免大项目撑爆上下文
DEFAULT_BRAIN_LIMITS: dict[str, int] = {
    "struct": 12,
    "semantic": 5,
    "norms": 8,
    "behavior": 5,
    "mistakes": 3,
    "successes": 3,
}
PROJECT_SUMMARY_MAX_CHARS = 800

LAYER_LABELS: dict[str, str] = {
    "struct": "结构索引（符号/文件）",
    "semantic": "语义检索",
    "norms": "Harness 工程规范",
    "behavior": "文件共现/热点",
    "mistakes": "错题记忆",
    "successes": "成功模式",
}

_retriever: SwarmRetriever | None = None
_retriever_lock = asyncio.Lock()

_current_project_id: ContextVar[str | None] = ContextVar("swarm_project_id", default=None)


def set_worker_context(project_id: str | None) -> None:
    """Worker 执行前设置 project_id（供 knowledge tool 检索 scoped 数据）"""
    _current_project_id.set(project_id)


def get_worker_project_id() -> str | None:
    return _current_project_id.get()


async def get_retriever() -> SwarmRetriever:
    global _retriever
    async with _retriever_lock:
        if _retriever is None:
            _retriever = SwarmRetriever()
            await _retriever.connect_all()
            if _retriever._semantic:
                from swarm.project.preprocess import _embed_texts

                async def _shared_embed(texts: list[str]) -> list[list[float]]:
                    return await asyncio.to_thread(_embed_texts, texts)

                _retriever._semantic.set_embed_fn(_shared_embed)
        return _retriever


async def close_retriever() -> None:
    global _retriever
    async with _retriever_lock:
        if _retriever is not None:
            await _retriever.close_all()
            _retriever = None


@swarm_traceable(
    "knowledge/retrieve-brain",
    phase=PHASE_2,
    component="knowledge",
    run_type="retriever",
)
async def retrieve_knowledge(
    task_desc: str,
    project_id: str,
    extra_keywords: list[str] | None = None,
) -> tuple[KnowledgeContext, dict[str, Any]]:
    """Brain analyze / Worker tool 统一检索"""
    if not project_id:
        empty: KnowledgeContext = {
            "struct": [],
            "semantic": [],
            "norms": [],
            "behavior": [],
            "mistakes": [],
            "successes": [],
            "project_summary": "",
            "preprocess_stats": {},
        }
        return empty, {}

    try:
        retriever = await get_retriever()
        result = await retriever.retrieve_for_brain(
            task_desc, project_id, extra_keywords=extra_keywords
        )
        return result.context, result.stats
    except Exception as exc:
        logger.warning("retrieve_knowledge failed for project %s: %s", project_id, exc)
        empty = {
            "struct": [],
            "semantic": [],
            "norms": [],
            "behavior": [],
            "mistakes": [],
            "successes": [],
            "project_summary": "",
            "preprocess_stats": {},
        }
        return empty, {"error": str(exc)}


def retrieve_knowledge_sync(
    task_desc: str,
    project_id: str,
    extra_keywords: list[str] | None = None,
) -> tuple[KnowledgeContext, dict[str, Any]]:
    """从同步上下文（LangChain Tool）调用 async 检索"""
    coro = retrieve_knowledge(task_desc, project_id, extra_keywords)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def empty_knowledge_context() -> KnowledgeContext:
    return {
        "struct": [],
        "semantic": [],
        "norms": [],
        "behavior": [],
        "mistakes": [],
        "successes": [],
    }


def format_layer_items(layer: str, items: list[dict[str, Any]], top_k: int) -> list[dict[str, str]]:
    """将检索结果格式化为 Tool 输出条目"""
    formatted: list[dict[str, str]] = []
    for item in items[:top_k]:
        if layer == "struct":
            title = f"{item.get('symbol_name', '?')} ({item.get('file_path', '')})"
            content = item.get("signature") or item.get("docstring") or item.get("symbol_type", "")
        elif layer == "semantic":
            title = item.get("file_path") or item.get("chunk_id", "semantic")
            content = (item.get("content") or item.get("signature") or item.get("text") or "")[:300]
        elif layer == "norms":
            title = item.get("title", "Harness 规则")
            content = item.get("content", "")
        elif layer == "behavior":
            title = item.get("file_path") or item.get("trigger_file") or "behavior"
            content = f"co_count={item.get('co_count', item.get('mod_count', ''))}"
        elif layer == "mistakes":
            title = item.get("error_type") or item.get("description", "")[:40]
            content = item.get("fix_description") or item.get("description", "")
        elif layer == "successes":
            title = item.get("pattern_name") or item.get("description", "")[:40]
            content = item.get("approach") or item.get("description", "")
        else:
            title = str(item.get("title", item.get("id", "item")))
            content = str(item.get("content", item))[:200]

        relevance = item.get("similarity") or item.get("score") or item.get("priority") or ""
        formatted.append({
            "title": str(title),
            "content": str(content)[:400],
            "relevance": str(relevance),
        })
    return formatted


def slice_context(context: KnowledgeContext, layers: list[str], top_k: int) -> dict[str, list[dict[str, str]]]:
    """按层级截取 KnowledgeContext 并格式化"""
    out: dict[str, list[dict[str, str]]] = {}
    for layer in layers:
        items = context.get(layer, [])  # type: ignore[arg-type]
        if isinstance(items, list):
            out[layer] = format_layer_items(layer, items, top_k)
        else:
            out[layer] = []
    return out


def compact_knowledge_context(
    context: KnowledgeContext,
    limits: dict[str, int] | None = None,
) -> KnowledgeContext:
    """按层上限截断，供 Brain / Worker 注入（非全量 dump）"""
    caps = limits or DEFAULT_BRAIN_LIMITS
    compact: KnowledgeContext = empty_knowledge_context()
    summary = context.get("project_summary")
    if summary:
        compact["project_summary"] = str(summary)[:PROJECT_SUMMARY_MAX_CHARS]
    for layer, cap in caps.items():
        items = context.get(layer)  # type: ignore[arg-type]
        if isinstance(items, list):
            compact[layer] = items[:cap]  # type: ignore[literal-required]
    return compact


def format_brain_knowledge_prompt(
    context: KnowledgeContext,
    query: str,
    limits: dict[str, int] | None = None,
) -> str:
    """将检索结果格式化为 Brain 可读 Markdown（任务相关、有上限）"""
    caps = limits or DEFAULT_BRAIN_LIMITS
    compact = compact_knowledge_context(context, caps)
    parts: list[str] = [
        f"> 以下内容由 SwarmRetriever 按任务「{query[:120]}」检索，"
        f"非全库 dump；各层有数量上限。",
    ]

    from swarm.config.settings import get_config
    from swarm.memory.sliding_window import truncate_text_to_tokens

    cfg = get_config()
    prompt_budget = max(4000, (cfg.context_max_tokens - cfg.context_reserve_tokens) // 2)

    summary = compact.get("project_summary") or context.get("project_summary")
    if summary:
        from swarm.project.preprocess import _clean_llm_summary
        parts.append(f"### 项目摘要\n{_clean_llm_summary(str(summary))[:PROJECT_SUMMARY_MAX_CHARS]}")

    stats = context.get("preprocess_stats")
    if isinstance(stats, dict) and stats:
        scan = stats.get("scan") or {}
        index = stats.get("index") or {}
        embed = stats.get("embed") or {}
        parts.append(
            "### 预处理概况\n"
            f"- 文件 {scan.get('files', '?')} · 符号 {index.get('symbols', '?')} · "
            f"向量 {embed.get('vectors', '?')}"
        )

    for layer, cap in caps.items():
        raw_items = context.get(layer, [])
        if not isinstance(raw_items, list) or not raw_items:
            continue
        formatted = format_layer_items(layer, raw_items, cap)
        if not formatted:
            continue
        label = LAYER_LABELS.get(layer, layer)
        parts.append(
            f"### {label}（展示 {len(formatted)} / 命中 {len(raw_items)}）"
        )
        for i, item in enumerate(formatted, 1):
            line = f"{i}. **{item['title']}**"
            if item.get("content"):
                line += f"\n   {item['content'][:320]}"
            if item.get("relevance"):
                line += f"\n   _相关度: {item['relevance']}_"
            parts.append(line)

    return truncate_text_to_tokens("\n\n".join(parts), prompt_budget)


@swarm_traceable(
    "knowledge/retrieve-experiment",
    phase=PHASE_2,
    component="knowledge",
    run_type="chain",
    extra_tags=["ui-experiment"],
)
async def experiment_retrieval(
    query: str,
    project_id: str,
    limits: dict[str, int] | None = None,
) -> dict[str, Any]:
    """检索实验 — 返回统计、分层切片、Brain 将看到的 prompt 预览"""
    context, stats = await retrieve_knowledge(query, project_id)
    caps = limits or DEFAULT_BRAIN_LIMITS
    slices = {
        layer: format_layer_items(layer, context.get(layer, []), cap)  # type: ignore[arg-type]
        for layer, cap in caps.items()
        if isinstance(context.get(layer), list)
    }
    prompt_preview = format_brain_knowledge_prompt(context, query, caps)
    return {
        "query": query,
        "stats": stats,
        "limits": caps,
        "slices": slices,
        "prompt_preview": prompt_preview,
        "prompt_chars": len(prompt_preview),
        "raw_counts": {
            layer: len(context.get(layer, []))  # type: ignore[arg-type]
            for layer in caps
            if isinstance(context.get(layer), list)
        },
    }
