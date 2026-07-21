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
        # 项目级沙箱构建阶段（_phase_build_sandbox 设的 message 含"构建项目专属沙箱"）
        # 透传更明确的提示，让用户知道是在构建沙箱而非笼统"预处理中"。
        running_msg = pp.get("message") or ""
        if "沙箱" in running_msg:
            return {"level": "running", "message": running_msg}
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

    # R65B-T2 猎手(c)：源码语义嵌入中止也算 partial——否则"签名层成功+源码层死了"
    # 仍报 ready，Brain 在退化语义层上检索却以为正常（正是本机制要治的召回塌陷复发面）。
    # DR-08-F3(#81)：codegraph 失败时 _phase_index 写 index_stats={"ok": False, ...}（无 skipped 键）
    # 并置 graph_status=DEGRADED，但 preprocess 仍标 READY。旧 partial 只看 skipped → 把"结构索引降级
    # /0 符号"当 ready，Brain 在 Layer A 恒空的项目上被告知"知识就绪"。补 `ok is False` 检测。
    partial = bool(index.get("skipped") or (index.get("ok") is False)
                   or embed.get("skipped") or embed.get("source_aborted"))
    if partial:
        parts: list[str] = []
        if index.get("skipped"):
            parts.append("结构索引(Layer A)已跳过")
        if index.get("ok") is False:
            parts.append("结构索引降级(codegraph 失败，符号检索受限)")
        if embed.get("skipped"):
            parts.append("向量嵌入(Layer B)已跳过")
        if embed.get("source_aborted"):
            parts.append(
                f"源码语义嵌入中止（{str(embed.get('source_aborted'))[:60]}——检索退化为签名层）")
        return {
            "level": "partial",
            "message": "预处理已完成 · " + "，".join(parts) + "（Brain 仍可使用扫描/分析结果）",
        }

    # A-P1-25：阶段=complete 但既无结构符号也无向量 → "完成"是空壳，绝非 ready。
    # 原先只看 status==READY，会把"预处理跑完但 0 索引/0 嵌入"误报为已就绪，
    # Brain 在空知识库上检索却以为正常。纯分类器：只看入参里已有的计数，不探活 DB。
    # （index 计数取 symbols，embed 计数取 vectors，与 preprocess 写入字段对齐。）
    index_count = int(index.get("symbols") or 0)
    embed_count = int(embed.get("vectors") or 0)
    if index_count == 0 and embed_count == 0:
        return {
            "level": "degraded",
            "message": "预处理已完成但结构索引与向量嵌入均为空 — Brain 检索将无可用知识，请重跑预处理",
            "show_preprocess_cta": True,
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
