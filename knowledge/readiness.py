"""Brain 任务创建前的知识库就绪评估（与 Web UI assessKnowledgeReadiness 对齐）"""

from __future__ import annotations

from typing import Any


def assess_knowledge_readiness(
    project: dict[str, Any] | None,
    preprocess: dict[str, Any] | None,
) -> dict[str, Any]:
    """返回 level: ready | partial | running | missing | error 及 message。"""
    project_status = (project or {}).get("status") or "UNKNOWN"
    pp = preprocess or {}
    phase = str(pp.get("phase") or "").lower()
    index = pp.get("index_stats") or {}
    embed = pp.get("embed_stats") or {}

    preprocess_done = phase == "complete" or project_status == "READY"
    preprocess_running = project_status == "PREPROCESSING" or phase in (
        "scanning",
        "indexing",
        "embedding",
        "analyzing",
    )
    preprocess_failed = phase == "error" or project_status == "ERROR"

    if preprocess_failed:
        return {
            "level": "error",
            "message": pp.get("error") or pp.get("message") or "预处理失败，请查看预处理 Tab",
        }
    if preprocess_running:
        return {
            "level": "running",
            "message": f"预处理进行中（{phase or '…'}）— 完成后 Brain 检索将可用",
        }
    if not preprocess_done:
        return {
            "level": "missing",
            "message": "尚未运行预处理 — Brain 检索质量将受限",
            "show_preprocess_cta": True,
        }

    partial = bool(index.get("skipped") or embed.get("skipped"))
    if partial:
        parts: list[str] = []
        if index.get("skipped"):
            parts.append("结构索引(Layer A)已跳过")
        if embed.get("skipped"):
            parts.append("向量嵌入(Layer B)已跳过")
        return {
            "level": "partial",
            "message": "预处理已完成 · " + "，".join(parts) + "（Brain 仍可使用扫描/分析结果）",
        }
    return {"level": "ready", "message": "知识库已就绪 — Brain 可正常检索本项目"}


def brain_task_ready(
    project: dict[str, Any] | None,
    preprocess: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Brain 任务是否允许创建（ready / partial 为 True）。"""
    readiness = assess_knowledge_readiness(project, preprocess)
    if readiness["level"] in ("ready", "partial"):
        return True, ""
    return False, readiness.get("message") or "知识库尚未就绪，无法创建 Brain 任务"
