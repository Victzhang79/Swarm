"""api/routers/knowledge.py — 知识库域路由 (概览/符号检索/语义检索/规范/热点/一致性/webhook)。

从 api/app.py 抽出, 通过 app.include_router 挂载。
mock 锚点 (store/_validate_project/_get_pg_conn) 用 _app. 属性访问保测试零改动。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import swarm.api.app as _app

router = APIRouter()


class NormCreateRequest(BaseModel):
    """添加 Harness 工程规范请求"""
    title: str = Field(description="规范标题")
    content: str = Field(description="规范内容")
    tag: str = Field(default="harness", description="分类标签")
    priority: int = Field(default=5, description="优先级")
    is_active: bool = Field(default=True, description="是否启用")


class NormUpdateRequest(BaseModel):
    """编辑规范请求 — 只更新提供的字段"""
    title: str | None = Field(default=None, description="规范标题")
    content: str | None = Field(default=None, description="规范内容")
    tag: str | None = Field(default=None, description="分类标签")
    priority: int | None = Field(default=None, description="优先级")
    is_active: bool | None = Field(default=None, description="是否启用")



@router.get("/api/projects/{project_id}/knowledge/overview", tags=["知识库"])
async def knowledge_overview(project_id: str):
    """项目知识库概览：预处理结果 + 索引统计"""
    import httpx

    from swarm.config.settings import get_config

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)

    def _query_pg() -> dict[str, Any]:
        with _app._get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT description, file_count, symbol_count, status, language_breakdown, graph_status "
                    "FROM projects WHERE id = %s",
                    (project_id,),
                )
                proj = cur.fetchone()
                cur.execute(
                    "SELECT phase, scan_stats, index_stats, embed_stats, analysis_stats, error "
                    "FROM preprocess_progress WHERE project_id = %s",
                    (project_id,),
                )
                prog = cur.fetchone()
                cur.execute(
                    "SELECT COUNT(*) FROM kb_norms WHERE project_id = %s AND is_active = TRUE",
                    (project_id,),
                )
                norms_count = cur.fetchone()[0]
                cur.execute(
                    "SELECT COUNT(*) FROM kb_symbol_index WHERE project_id = %s",
                    (project_id,),
                )
                symbol_count = cur.fetchone()[0]
        out: dict[str, Any] = {"norms_count": norms_count, "symbol_count": symbol_count}
        if proj:
            from swarm.project.preprocess import _clean_llm_summary
            out.update({
                "description": _clean_llm_summary(proj[0] or ""),
                "file_count": proj[1] or 0,
                "project_symbol_count": proj[2] or 0,
                "status": proj[3],
                "language_breakdown": proj[4] if isinstance(proj[4], dict) else {},
                "graph_status": proj[5] or "NONE",
            })
        if prog:
            out["preprocess"] = {
                "phase": prog[0],
                "scan_stats": prog[1] if isinstance(prog[1], dict) else {},
                "index_stats": prog[2] if isinstance(prog[2], dict) else {},
                "embed_stats": prog[3] if isinstance(prog[3], dict) else {},
                "analysis_stats": prog[4] if isinstance(prog[4], dict) else {},
                "error": prog[5],
            }
        return out

    overview = await loop.run_in_executor(None, _query_pg)

    cfg = get_config()
    qdrant_count = 0
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            coll = cfg.db.qdrant_collection
            resp = await client.post(
                f"{cfg.db.qdrant_url.rstrip('/')}/collections/{coll}/points/count",
                json={"filter": {"must": [{"key": "project_id", "match": {"value": project_id}}]}},
            )
            if resp.status_code == 200:
                qdrant_count = resp.json().get("result", {}).get("count", 0)
            if qdrant_count == 0:
                legacy = f"project_{project_id}"
                resp2 = await client.get(
                    f"{cfg.db.qdrant_url.rstrip('/')}/collections/{legacy}",
                )
                if resp2.status_code == 200:
                    qdrant_count = resp2.json().get("result", {}).get("points_count", 0)
                    overview["qdrant_collection"] = legacy
                else:
                    overview["qdrant_collection"] = coll
            else:
                overview["qdrant_collection"] = coll
    except Exception as exc:
        overview["qdrant_error"] = str(exc)
    overview["qdrant_vectors"] = qdrant_count

    return overview


@router.get("/api/projects/{project_id}/knowledge/symbols", tags=["知识库"])
async def search_symbols(project_id: str, q: str, limit: int = 30):
    """Layer A — 按符号名模糊搜索"""
    from swarm.config.settings import get_config
    from swarm.knowledge.structure_index import StructureIndexer

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)
    if not q.strip():
        raise HTTPException(status_code=400, detail="q 不能为空")

    cap = max(1, min(limit, 100))

    async def _search() -> list[dict[str, Any]]:
        indexer = StructureIndexer(get_config().db)
        await indexer.connect()
        try:
            rows = await indexer.query_symbols_by_name(project_id, q.strip())
            return rows[:cap]
        finally:
            await indexer.close()

    return {"symbols": await _search(), "query": q.strip(), "limit": cap}


@router.get("/api/projects/{project_id}/knowledge/semantic", tags=["知识库"])
async def search_semantic_chunks(project_id: str, q: str, limit: int = 20):
    """Layer B — 语义 chunk 检索（Qdrant）"""
    from swarm.config.settings import get_config
    from swarm.knowledge.semantic_index import SemanticIndexer

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)
    if not q.strip():
        raise HTTPException(status_code=400, detail="q 不能为空")

    cap = max(1, min(limit, 50))
    cfg = get_config()

    async def _search() -> list[dict[str, Any]]:
        indexer = SemanticIndexer(cfg.db, cfg.knowledge)
        await indexer.connect()
        try:
            raw = await indexer.search(project_id, q.strip(), top_k=cap)
            hits: list[dict[str, Any]] = []
            for row in raw:
                content = str(row.get("content") or "")
                hits.append({
                    "id": row.get("id"),
                    "score": row.get("score"),
                    "file_path": row.get("file_path", ""),
                    "start_line": row.get("start_line"),
                    "end_line": row.get("end_line"),
                    "module_name": row.get("module_name"),
                    "chunk_type": row.get("chunk_type"),
                    "content_preview": content[:600],
                })
            return hits
        finally:
            await indexer.close()

    return {"chunks": await _search(), "query": q.strip(), "limit": cap}


class KnowledgeRetrieveRequest(BaseModel):
    """编排检索实验 — 模拟 Brain 按任务检索知识"""
    query: str = Field(description="任务描述 / 检索 query")
    top_k: int | None = Field(default=None, description="单层上限（可选，默认使用 Brain 配置）")


@router.post("/api/projects/{project_id}/knowledge/retrieve", tags=["知识库"])
async def knowledge_retrieve_experiment(
    project_id: str,
    req: KnowledgeRetrieveRequest,
):
    """按任务检索知识库+记忆，返回 Brain 编排将注入的 prompt 预览"""
    from swarm.knowledge.service import DEFAULT_BRAIN_LIMITS, experiment_retrieval

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)

    limits = dict(DEFAULT_BRAIN_LIMITS)
    if req.top_k is not None:
        for k in limits:
            limits[k] = min(req.top_k, limits[k] if req.top_k >= 5 else req.top_k)

    return await experiment_retrieval(req.query.strip(), project_id, limits)


# ─── 知识库 — 规范 (kb_norms) ────────────────────


@router.get("/api/projects/{project_id}/knowledge/norms", tags=["知识库"])
async def list_norms(
    project_id: str,
    tag: str | None = None,
    active_only: bool = True,
):
    """获取项目规范列表"""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)

    def _query():
        with _app._get_pg_conn() as conn:
            with conn.cursor() as cur:
                conditions = ["project_id = %s"]
                params: list = [project_id]
                if tag:
                    conditions.append("tag = %s")
                    params.append(tag)
                if active_only:
                    conditions.append("is_active = TRUE")
                where = " AND ".join(conditions)
                cur.execute(
                    f"SELECT id, project_id, title, content, tag, priority, is_active, created_at, updated_at "
                    f"FROM kb_norms WHERE {where} ORDER BY priority DESC, id ASC",
                    params,
                )
                cols = ["id", "project_id", "title", "content", "tag", "priority", "is_active", "created_at", "updated_at"]
                return [dict(zip(cols, row)) for row in cur.fetchall()]

    norms = await loop.run_in_executor(None, _query)
    return {"norms": norms}


@router.post("/api/projects/{project_id}/knowledge/norms", tags=["知识库"])
async def create_norm(project_id: str, req: NormCreateRequest):
    """添加项目规范"""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)

    def _insert():
        with _app._get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO kb_norms (project_id, title, content, tag, priority, is_active) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, title",
                    (project_id, req.title, req.content, req.tag, req.priority, req.is_active),
                )
                row = cur.fetchone()
                return {"id": row[0], "title": row[1]}

    return await loop.run_in_executor(None, _insert)


@router.put("/api/projects/{project_id}/knowledge/norms/{norm_id}", tags=["知识库"])
async def update_norm(project_id: str, norm_id: int, req: NormUpdateRequest):
    """编辑项目规范 — 只更新提供的字段"""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)

    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    def _do_update():
        with _app._get_pg_conn() as conn:
            with conn.cursor() as cur:
                set_clause = ", ".join(f"{k} = %s" for k in updates)
                values = list(updates.values()) + [project_id, norm_id]
                cur.execute(
                    f"UPDATE kb_norms SET {set_clause}, updated_at = NOW() "
                    f"WHERE project_id = %s AND id = %s",
                    values,
                )
                if cur.rowcount == 0:
                    raise HTTPException(status_code=404, detail=f"Norm {norm_id} not found")
        return {"updated": True}

    return await loop.run_in_executor(None, _do_update)


@router.delete("/api/projects/{project_id}/knowledge/norms/{norm_id}", tags=["知识库"])
async def delete_norm(project_id: str, norm_id: int):
    """删除项目规范"""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)

    def _do_delete():
        with _app._get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM kb_norms WHERE project_id = %s AND id = %s",
                    (project_id, norm_id),
                )
                if cur.rowcount == 0:
                    raise HTTPException(status_code=404, detail=f"Norm {norm_id} not found")
        return {"deleted": True}

    return await loop.run_in_executor(None, _do_delete)


@router.get("/api/projects/{project_id}/knowledge/behavior-hotspots", tags=["知识库"])
async def list_behavior_hotspots(project_id: str, top_k: int = 20, days: int | None = None):
    """Layer D — 高频修改文件排行（行为热点）"""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)

    cap = max(1, min(top_k, 100))

    def _query() -> list[dict[str, Any]]:
        with _app._get_pg_conn() as conn:
            with conn.cursor() as cur:
                if days is not None:
                    cur.execute(
                        """
                        SELECT file_path, COUNT(*) AS mod_count, MAX(modified_at) AS last_modified
                        FROM kb_modification_log
                        WHERE project_id = %s AND modified_at >= now() - make_interval(days => %s)
                        GROUP BY file_path
                        ORDER BY mod_count DESC
                        LIMIT %s
                        """,
                        (project_id, days, cap),
                    )
                else:
                    cur.execute(
                        """
                        SELECT file_path, COUNT(*) AS mod_count, MAX(modified_at) AS last_modified
                        FROM kb_modification_log
                        WHERE project_id = %s
                        GROUP BY file_path
                        ORDER BY mod_count DESC
                        LIMIT %s
                        """,
                        (project_id, cap),
                    )
                rows = cur.fetchall()
        return [
            {
                "file_path": r[0],
                "mod_count": r[1],
                "last_modified": r[2].isoformat() if r[2] else None,
                "type": "hotspot",
            }
            for r in rows
        ]

    hotspots = await loop.run_in_executor(None, _query)
    return {"hotspots": hotspots, "top_k": cap, "days": days}


@router.get("/api/projects/{project_id}/knowledge/consistency", tags=["知识库"])
async def knowledge_consistency_check(project_id: str, repair: bool = False):
    """ConsistencyChecker — 比对工作区与 Layer A 索引；repair=true 时入队修复。"""
    loop = asyncio.get_running_loop()
    project = await loop.run_in_executor(None, _app.store.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    from swarm.knowledge.consistency import (
        check_project_consistency,
        repair_project_consistency,
    )

    if repair:
        return await repair_project_consistency(project_id, project["path"])
    return await loop.run_in_executor(
        None,
        lambda: check_project_consistency(project_id, project["path"]),
    )


class GitWebhookPayload(BaseModel):
    commits: list[dict[str, Any]] = Field(default_factory=list)
    user_name: str | None = None
    ref: str | None = None


@router.post("/api/projects/{project_id}/knowledge/webhook/git", tags=["知识库"])
async def git_knowledge_webhook(project_id: str, payload: GitWebhookPayload):
    """Git push webhook → Layer A/B/D 增量更新（P2）。"""
    loop = asyncio.get_running_loop()
    project = await loop.run_in_executor(None, _app.store.get_project, project_id)
    if not project or not project.get("path"):
        raise HTTPException(status_code=404, detail="Project not found")
    from swarm.knowledge.hooks import handle_git_push_webhook

    return await handle_git_push_webhook(
        project_id,
        project["path"],
        payload.model_dump(),
    )
