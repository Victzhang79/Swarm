"""知识库检索 Tool — query_knowledge_base

通过 SwarmRetriever 统一检索 API 获取项目结构、语义、规范、行为、错题、成功模式。
"""

from __future__ import annotations

from typing import Optional

from langchain_core.tools import tool

from swarm.knowledge.service import (
    get_worker_project_id,
    retrieve_knowledge_sync,
    slice_context,
)


@tool
def query_knowledge_base(
    query: str,
    layers: Optional[list[str]] = None,
    top_k: int = 5,
) -> str:
    """从项目知识库检索相关信息。

    可检索的知识层级：
    - struct: 项目结构索引（符号、文件）
    - semantic: 语义检索（向量相似度）
    - norms: 项目编程规范与约定
    - behavior: 文件共现与热点
    - mistakes: 过去的错误及修复方案（L5）
    - successes: 成功的实现模式（L6）

    Args:
        query: 检索查询文本
        layers: 要检索的知识层级列表，如 ['struct', 'mistakes']。默认全部。
        top_k: 每个层级返回的最大结果数，默认 5

    Returns:
        检索结果文本
    """
    all_layers = ["struct", "semantic", "norms", "behavior", "mistakes", "successes"]
    target_layers = layers if layers else all_layers

    invalid = set(target_layers) - set(all_layers)
    if invalid:
        return f"❌ 无效的知识层级：{invalid}，有效层级：{all_layers}"

    project_id = get_worker_project_id()
    if not project_id:
        return "❌ 未设置 project_id，无法检索知识库（Worker 上下文缺失）"

    context, stats = retrieve_knowledge_sync(query, project_id)
    if stats.get("error"):
        return f"⚠️ 知识检索部分失败: {stats['error']}"

    formatted = slice_context(context, target_layers, top_k)

    output_parts: list[str] = []
    for layer in target_layers:
        items = formatted.get(layer, [])
        if not items:
            output_parts.append(f"## {layer}\n(无相关结果)")
            continue
        output_parts.append(f"## {layer}")
        for i, item in enumerate(items, 1):
            title = item.get("title", f"结果 {i}")
            content = item.get("content", "")
            relevance = item.get("relevance", "")
            line = f"  {i}. {title}"
            if relevance:
                line += f" (相关度: {relevance})"
            if content:
                line += f"\n     {content[:200]}"
            output_parts.append(line)

    return "\n".join(output_parts)
