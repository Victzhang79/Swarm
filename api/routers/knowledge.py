"""api/routers/knowledge.py — 知识库域路由 (概览/符号检索/语义检索/规范/热点/一致性/webhook)。

从 api/app.py 抽出, 通过 app.include_router 挂载。
mock 锚点 (store/_validate_project/_get_pg_conn) 用 _app. 属性访问保测试零改动。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import swarm.api.app as _app
from swarm.api._shared import _require_perm

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


class IngestRequest(BaseModel):
    """文档采集请求 — 把外部资料灌进项目知识库（语义层 Layer B）。

    source_type:
      - "local"   用 file_paths（一般来自 POST /api/uploads 返回的 path）建 LocalFileSource。
      - "feishu"/"tencent"/"yuque"  建对应远端 adapter；无 token 时其 list/fetch 抛
        NotImplementedError，端点 catch 后返 400 + 清晰接入提示。
    dry_run:
      默认 False（用户主动点"导入"即落库）；传 True 只解析+切分预览，绝不触达 Qdrant。
    """
    file_paths: list[str] = Field(default_factory=list, description="本地文件绝对路径列表（source_type=local）")
    source_type: str = Field(default="local", description="来源类型：local/feishu/tencent/yuque")
    source_config: dict = Field(default_factory=dict, description="远端来源额外配置（预留，当前 token 走环境变量）")
    dry_run: bool = Field(default=False, description="True 仅预览不落库；False（默认）真落库")



@router.get("/api/projects/{project_id}/knowledge/overview", tags=["知识库"])
async def knowledge_overview(project_id: str, request: Request):
    """项目知识库概览：预处理结果 + 索引统计"""
    _require_perm(request, "project:read", project_id)  # P0-SEC-03：防跨项目读知识库
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
async def search_symbols(project_id: str, q: str, request: Request, limit: int = 30):
    """Layer A — 按符号名模糊搜索"""
    _require_perm(request, "project:read", project_id)  # P0-SEC-03
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
async def search_semantic_chunks(project_id: str, q: str, request: Request, limit: int = 20):
    """Layer B — 语义 chunk 检索（Qdrant）"""
    _require_perm(request, "project:read", project_id)  # P0-SEC-03
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
    request: Request,
):
    """按任务检索知识库+记忆，返回 Brain 编排将注入的 prompt 预览"""
    _require_perm(request, "project:read", project_id)  # P0-SEC-03
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
    request: Request,
    tag: str | None = None,
    active_only: bool = True,
):
    """获取项目规范列表"""
    _require_perm(request, "project:read", project_id)  # P0-SEC-03
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
async def create_norm(project_id: str, request: Request, req: NormCreateRequest):
    """添加项目规范"""
    _require_perm(request, "knowledge:write", project_id)
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
async def update_norm(project_id: str, norm_id: int, request: Request, req: NormUpdateRequest):
    """编辑项目规范 — 只更新提供的字段"""
    _require_perm(request, "knowledge:write", project_id)
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
async def delete_norm(project_id: str, norm_id: int, request: Request):
    """删除项目规范"""
    _require_perm(request, "knowledge:write", project_id)
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


# ─── 文档采集（KB ingest） ────────────────────────────────────────────────


def _build_ingest_source(source_type: str, file_paths: list[str], source_config: dict | None = None):
    """按 source_type 构造 SourceAdapter。

    local 用 file_paths（每个路径建一个 LocalFileSource，下面会合并文档引用）；
    yuque 为真实现（缺 token/namespace 时 list/fetch 抛 NotImplementedError）；
    feishu/tencent 仍是 stub（缺 token 时其 list/fetch 抛 NotImplementedError）。
    yuque 的 namespace 可由 source_config.namespace 覆盖（否则走 YUQUE_NAMESPACE env）。
    """
    from swarm.knowledge.ingest.sources import (
        FeishuSource,
        TencentDocSource,
        YuqueSource,
    )

    cfg = source_config or {}
    st = (source_type or "local").lower()
    if st == "local":
        if not file_paths:
            raise HTTPException(status_code=400, detail="source_type=local 需提供 file_paths（先调 POST /api/uploads 拿路径）")
        return _MultiLocalSource(file_paths)
    if st == "yuque":
        namespace = (cfg.get("namespace") or "").strip() or None
        return YuqueSource(namespace=namespace)
    remote = {
        "feishu": FeishuSource,
        "tencent": TencentDocSource,
        "tencent_doc": TencentDocSource,
    }
    cls = remote.get(st)
    if cls is None:
        raise HTTPException(status_code=400, detail=f"不支持的 source_type: {source_type}（可选 local/feishu/tencent/yuque）")
    return cls()


class _MultiLocalSource:
    """把多个本地文件路径合并成单一 SourceAdapter（每个 path 一个 DocRef）。

    复用 LocalFileSource 的 list/fetch（白名单/读盘），但允许任意一组离散文件路径
    （上传端点返回的是离散 path，不是单个目录）。
    """

    source_name = "local"

    def __init__(self, file_paths: list[str]) -> None:
        from swarm.knowledge.ingest.sources import LocalFileSource

        self._sources = [LocalFileSource(p) for p in file_paths]

    def list_documents(self):
        refs = []
        for s in self._sources:
            refs.extend(s.list_documents())
        return refs

    def fetch(self, doc_id: str):
        from swarm.knowledge.ingest.sources import LocalFileSource

        return LocalFileSource(doc_id).fetch(doc_id)


def _summarize_report(report) -> dict[str, Any]:
    """把 IngestReport 压成前端友好的 JSON 摘要。"""
    return {
        "source_name": report.source_name,
        "project_id": report.project_id,
        "dry_run": report.dry_run,
        "total_docs": report.total_docs,
        "parsed_docs": report.parsed_docs,
        "skipped_docs": report.skipped_docs,
        "failed_docs": report.failed_docs,
        "total_chunks": report.total_chunks,
        "indexed_chunks": report.indexed_chunks,
        "docs": [
            {
                "filename": d.filename,
                "title": d.title,
                "status": d.status,
                "num_chunks": d.num_chunks,
                "error": d.error,
            }
            for d in report.docs
        ],
    }


@router.post("/api/projects/{project_id}/knowledge/ingest", tags=["知识库"])
async def ingest_documents(project_id: str, request: Request, req: IngestRequest):
    """采集外部文档进项目知识库（语义层）。

    dry_run=True：纯预览（解析+切分），绝不落 Qdrant。
    dry_run=False（默认）：用已连接的 SemanticIndexer 真落库（走专用 KB loop，复用单例连接）。
    远端源无 token → adapter 抛 NotImplementedError → 这里 catch 后返 400 + 接入提示。
    """
    _require_perm(request, "knowledge:write", project_id)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)

    from swarm.knowledge.ingest.pipeline import ingest as _ingest_pipeline

    # 构造来源（local 校验在 _build_ingest_source 内，远端缺 token 在 list/fetch 才抛）
    source = _build_ingest_source(req.source_type, req.file_paths, req.source_config)

    def _run_blocking() -> dict[str, Any]:
        """在 executor 线程里跑：dry_run 纯异步预览；非 dry_run 走 KB loop 拿连好的 indexer 落库。"""
        if req.dry_run:
            # 预览不碰真实 KB，独立 loop 跑 async pipeline 即可
            report = asyncio.run(
                _ingest_pipeline(source, project_id=project_id, dry_run=True)
            )
            return _summarize_report(report)

        # 真落库：必须用已连接的 SemanticIndexer（在专用 KB loop 上），否则跨 loop 复用连接会炸
        from swarm.knowledge.service import _run_on_kb_loop, get_retriever

        async def _go():
            retriever = await get_retriever()
            indexer = getattr(retriever, "_semantic", None)
            if indexer is None:
                raise RuntimeError(
                    "语义索引器不可用（Qdrant 未连接或未嵌入）。请先确保 Qdrant 运行并完成预处理。"
                )
            return await _ingest_pipeline(
                source, project_id=project_id, indexer=indexer, dry_run=False
            )

        report = _run_on_kb_loop(_go())
        return _summarize_report(report)

    try:
        return await loop.run_in_executor(None, _run_blocking)
    except HTTPException:
        raise
    except NotImplementedError as e:
        # 远端源未配置 token / 未实现 → 把 adapter 写好的接入说明直接回给前端
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=f"文件不存在: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        # 远端源(如语雀)的 HTTP/网络错误 —— 多是 token/namespace 配错或网络不通，
        # 属客户端可纠正问题，返 400 + 带状态码的清晰提示，而非 500。
        msg = str(e)
        if msg.startswith("[yuque]") or "语雀" in msg:
            raise HTTPException(status_code=400, detail=msg)
        raise HTTPException(status_code=500, detail=f"采集失败: {e}")
    except Exception as e:  # noqa: BLE001 - 兜底，绝不 500 裸抛
        raise HTTPException(status_code=500, detail=f"采集失败: {e}")


@router.get("/api/projects/{project_id}/knowledge/behavior-hotspots", tags=["知识库"])
async def list_behavior_hotspots(project_id: str, request: Request, top_k: int = 20, days: int | None = None):
    """Layer D — 高频修改文件排行（行为热点）"""
    _require_perm(request, "project:read", project_id)  # P0-SEC-03
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
async def knowledge_consistency_check(project_id: str, request: Request, repair: bool = False):
    """ConsistencyChecker — 比对工作区与 Layer A 索引；repair=true 时入队修复。"""
    _require_perm(request, "project:read", project_id)  # P0-SEC-03：读路径也需授权
    if repair:
        _require_perm(request, "knowledge:write", project_id)
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
async def git_knowledge_webhook(project_id: str, request: Request, payload: GitWebhookPayload):
    """Git push webhook → Layer A/B/D 增量更新（P2）。

    S5 修复：webhook 由外部 git 系统调用（无用户登录态），不走 _require_perm，
    改用 per-project HMAC 签名校验——请求头 X-Swarm-Signature: sha256=<hmac(secret, raw_body)>。
    secret 存 secret_store（key=webhook_secret:<project_id>）。未配置 secret 时：
      - RBAC 开启 → 拒绝（403，强制要求配置签名，防 SSRF/KB 投毒）；
      - RBAC 关闭（本地开发）→ 放行并告警（不破坏本地无签名调用）。
    """
    import hashlib
    import hmac as _hmac
    import logging as _logging

    from swarm.config import secret_store
    from swarm.config.settings import get_config

    raw_body = await request.body()
    secret = ""
    try:
        secret = (secret_store.get_secret(f"webhook_secret:{project_id}") or "").strip()
    except Exception:  # noqa: BLE001
        secret = ""

    rbac_on = bool(getattr(get_config(), "rbac_enabled", True))
    if secret:
        sig_header = request.headers.get("X-Swarm-Signature", "").strip()
        expected = "sha256=" + _hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if not sig_header or not _hmac.compare_digest(sig_header, expected):
            raise HTTPException(status_code=403, detail="webhook 签名校验失败")
    elif rbac_on:
        # 生产(RBAC 开)未配置 secret → 拒绝，避免无签名 webhook 被滥用投毒/SSRF
        raise HTTPException(
            status_code=403,
            detail="该项目未配置 webhook secret，拒绝无签名调用（请先设置 webhook_secret）",
        )
    else:
        _logging.getLogger("swarm.api.knowledge").warning(
            "[webhook] 项目 %s 未配置 webhook secret 且 RBAC 关闭，放行无签名调用（仅限本地开发）",
            project_id,
        )

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


# ── 12.16: pending_embeddings 死信队列可观测 + 手动 requeue ──
# 背景：embedding 服务长期不可用时，kb_pending_embeddings 条目 retry_count 累积，
# >=10 被视为永久失败不再自动重试，但此前无 API 暴露、无法手动恢复——运维盲区。

@router.get("/api/projects/{project_id}/knowledge/pending-embeddings", tags=["知识库"])
async def list_pending_embeddings(project_id: str, request: Request):
    """列出该项目待补 embedding 的文件，含 dead(retry_count>=10 永久失败) 标记。"""
    _require_perm(request, "project:read", project_id)  # P0-SEC-03
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)

    def _query() -> dict[str, Any]:
        with _app._get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT file_path, change_type, language, retry_count, last_error, created_at
                    FROM kb_pending_embeddings
                    WHERE project_id = %s
                    ORDER BY retry_count DESC, created_at ASC
                    """,
                    (project_id,),
                )
                rows = cur.fetchall()
        items = [
            {
                "file_path": r[0],
                "change_type": r[1],
                "language": r[2],
                "retry_count": r[3],
                "last_error": r[4],
                "created_at": r[5].isoformat() if r[5] else None,
                "dead": r[3] >= 10,  # 与 updater.retry_pending_embeddings 的 retry_count<10 阈值一致
            }
            for r in rows
        ]
        dead = sum(1 for it in items if it["dead"])
        return {"total": len(items), "dead": dead, "pending": len(items) - dead, "items": items}

    return await loop.run_in_executor(None, _query)


@router.post("/api/projects/{project_id}/knowledge/pending-embeddings/requeue", tags=["知识库"])
async def requeue_pending_embeddings(project_id: str, request: Request):
    """把该项目所有 dead(retry_count>=10) 条目的 retry_count 清零，重新纳入自动重试。

    用于 embedding 服务恢复后手动恢复死信。返回被重置的条目数。
    """
    _require_perm(request, "knowledge:write", project_id)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)

    def _requeue() -> dict[str, Any]:
        with _app._get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE kb_pending_embeddings
                    SET retry_count = 0, last_error = NULL
                    WHERE project_id = %s AND retry_count >= 10
                    """,
                    (project_id,),
                )
                n = cur.rowcount
        return {"requeued": n}

    return await loop.run_in_executor(None, _requeue)
