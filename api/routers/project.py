"""api/routers/project.py — 项目管理域路由 (列表/创建/详情/删除/预处理触发与进度)。

从 api/app.py 抽出, app.include_router 挂载。
mock 锚点(store/_validate_project)及 app 级 preprocess/logger 用 _app. 属性访问。
"""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from swarm.api.rate_limit import rate_limit  # C7
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

import swarm.api.app as _app
from swarm.api._shared import _require_perm, _require_user

router = APIRouter()


class ProjectCreateRequest(BaseModel):
    """创建项目请求"""
    name: str = Field(description="项目名称")
    path: str = Field(default="", description="项目根目录绝对路径；greenfield 留空则自动在 workspace 下创建")
    description: str = Field(default="", description="项目描述")
    greenfield: bool = Field(default=False, description="从零创建（空项目），path 不存在时自动建目录")


@router.get("/api/projects", tags=["项目管理"])
async def list_projects(request: Request):
    """返回当前用户可见的项目列表"""
    from swarm.auth.rbac import Role
    from swarm.auth.store import list_user_project_ids

    user = _require_user(request)
    loop = asyncio.get_running_loop()
    try:
        all_projects = await loop.run_in_executor(None, _app.store.list_projects)
    except Exception as e:
        _app.logger.warning(f"PG unavailable for list_projects: {e}")
        all_projects = []
    if user.global_role != Role.ADMIN.value:
        allowed = list_user_project_ids(user.id)
        all_projects = [p for p in all_projects if p.get("id") in allowed]
    return {"projects": all_projects}


# ─── 2. POST /api/projects — 创建项目 ─────────────
def _env_allow_external_project_path() -> bool:
    """C4：是否允许把项目根指向 workspace 之外的本机已有目录。默认 true。"""
    import os
    return os.environ.get("SWARM_ALLOW_EXTERNAL_PROJECT_PATH", "true").strip().lower() \
        not in ("0", "false", "no", "off")


def _enforce_project_path_containment(resolved_path: str, project_root: str, allow_external: bool) -> None:
    """C4 治本：可选把项目根限制在 workspace 内（防多租户共享宿主机下的路径级 IDOR + 摄取）。

    默认 allow_external=true → 不限制（不破坏"指向本机已有外部项目"的合法工作流，如 E2E 的
    e2e-projects/RuoYi）。SWARM_ALLOW_EXTERNAL_PROJECT_PATH=false 时强制 containment 到
    project_root，越界拒绝。与黑名单 _reject_sensitive 互补（黑名单永远生效，containment 可选加严）。
    """
    if allow_external or not resolved_path:
        return
    import os
    norm = os.path.realpath(os.path.abspath(resolved_path))
    root = os.path.realpath(os.path.abspath(project_root))
    if norm != root and not norm.startswith(root + os.sep):
        raise HTTPException(
            status_code=400,
            detail=(f"项目根必须在 workspace({root}) 内："
                    f"SWARM_ALLOW_EXTERNAL_PROJECT_PATH=false 已禁止外部路径（多租户加固）"),
        )


def _caller_may_reuse_existing_project(user, existing_id: str) -> bool:
    """D16：path 已被既存项目占用时，调用者可否幂等复用（不改写）该项目。

    仅 全局 admin 或 该项目成员 可复用；成员查询失败 fail-closed 拒绝——
    否则任何持 project:create 者提交受害者 path 即可拿到完整项目行（跨用户泄露/劫持）。
    """
    from swarm.auth.rbac import Role

    if getattr(user, "global_role", None) == Role.ADMIN.value:
        return True
    if not existing_id:
        return False
    try:
        import swarm.auth.store as _auth_store
        return _auth_store.get_project_member_role(existing_id, user.id) is not None
    except Exception:  # noqa: BLE001 — DB 抖动等：默认拒绝
        return False


@router.post("/api/projects", tags=["项目管理"])
async def create_project(req: ProjectCreateRequest, request: Request):
    """创建项目并自动启动预处理

    项目状态从 EMPTY → PREPROCESSING → READY
    """
    from swarm.auth.rbac import Role
    from swarm.auth.store import set_project_member

    user = _require_perm(request, "project:create")
    project_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()

    # ── 路径解析 + greenfield（从零创建）支持 ──
    # 既有项目：path 必须指向存在的目录。
    # greenfield：path 不存在则自动创建；留空则在 workspace 下按项目名建目录。
    import os
    import re as _re
    from swarm.config.settings import PROJECT_ROOT

    resolved_path = (req.path or "").strip()

    # M7 修复：拒绝把项目根指向系统敏感目录（后续 apply-diff 会写入该目录）。
    # 不强制 containment 到 workspace（用户合法用例就是指向本机已有项目），
    # 但黑名单系统关键目录，避免误指/恶意指向 /etc /usr /bin 等。
    def _reject_sensitive(p: str) -> None:
        if not p:
            return
        norm = os.path.realpath(os.path.abspath(p))
        sensitive = ("/etc", "/usr", "/bin", "/sbin", "/sys", "/proc", "/dev",
                     "/boot", "/var/run", "/lib", "/lib64", "/root")
        for s in sensitive:
            if norm == s or norm.startswith(s + "/"):
                raise HTTPException(
                    status_code=400,
                    detail=f"拒绝将项目根指向系统敏感目录: {norm}",
                )

    _reject_sensitive(resolved_path)
    _allow_external = _env_allow_external_project_path()
    _enforce_project_path_containment(resolved_path, str(PROJECT_ROOT), _allow_external)
    if req.greenfield:
        if not resolved_path:
            safe = _re.sub(r"[^A-Za-z0-9_.-]+", "-", req.name).strip("-") or project_id[:8]
            resolved_path = str((PROJECT_ROOT / "workdir" / safe).resolve())
        _reject_sensitive(resolved_path)
        _enforce_project_path_containment(resolved_path, str(PROJECT_ROOT), _allow_external)
        try:
            os.makedirs(resolved_path, exist_ok=True)
        except OSError as e:
            raise HTTPException(status_code=400, detail=f"无法创建项目目录 {resolved_path}: {e}") from e
    else:
        if not resolved_path:
            raise HTTPException(status_code=400, detail="既有项目必须提供 path（或设 greenfield=true 从零创建）")
        if not os.path.isdir(resolved_path):
            raise HTTPException(
                status_code=400,
                detail=f"路径不存在: {resolved_path}。如需从零创建空项目，请设 greenfield=true",
            )

    # 创建项目记录
    from swarm.project.store import ProjectPathConflictError

    try:
        project = await loop.run_in_executor(
            None,
            lambda: _app.store.create_project(
                project_id=project_id,
                name=req.name,
                path=resolved_path,
                description=req.description,
            ),
        )
    except ProjectPathConflictError as conflict:
        # D16：path 已被既存项目占用 → 绝不改写。成员（或 admin）按幂等语义返回既有
        # 项目（不触发预处理、不动成员表——重复添加同路径的合法场景）；其他人 409 拒绝，
        # 响应不携带既存项目任何字段（防跨用户信息泄露）。
        existing = conflict.existing or {}
        allowed = await loop.run_in_executor(
            None, lambda: _caller_may_reuse_existing_project(user, existing.get("id") or ""),
        )
        if not allowed or not existing.get("id"):
            raise HTTPException(
                status_code=409,
                detail="该路径已被其他项目占用",
            ) from None
        _app.logger.info(
            "create_project: path=%s 已存在(id=%s)，成员 %s 幂等复用（不改写）",
            resolved_path, existing.get("id"), user.id,
        )
        return {"status": "ok", "project": existing, "existing": True}
    except Exception as e:
        _app.logger.error("Failed to create project: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="创建项目失败，请稍后重试或联系管理员") from e

    if user.global_role != Role.ADMIN.value:
        # D16③：成员行必须挂在【store 返回的真实项目 id】上（勿用本地 uuid——
        # 任何 id 改写都会让成员行成为不存在项目的永久孤儿）。
        try:
            await loop.run_in_executor(
                None,
                lambda: set_project_member(project["id"], user.id, Role.OWNER.value),
            )
        except Exception as e:
            # D22 治本：项目行与成员行非原子——成员写入失败会留下【创建者自己都看不到】的
            # 孤儿项目（不在 list_user_project_ids 白名单）。补偿删除刚建项目，不留孤儿；
            # 补偿失败必须 error 留痕（可观测，运维可按 id 清理），两种情况都对外报错。
            _app.logger.error(
                "create_project: 成员授权写入失败 project=%s user=%s，补偿删除刚建项目: %s",
                project["id"], user.id, e, exc_info=True,
            )
            try:
                removed = await loop.run_in_executor(
                    None, lambda: _app.store.delete_project(project["id"]),
                )
                if not removed:
                    _app.logger.error(
                        "create_project: 补偿删除未生效（孤儿项目残留，需人工清理）project=%s",
                        project["id"],
                    )
            except Exception:  # noqa: BLE001 — 补偿失败不掩盖主错误，但必须留痕
                _app.logger.error(
                    "create_project: 补偿删除失败（孤儿项目残留，需人工清理）project=%s",
                    project["id"], exc_info=True,
                )
            raise HTTPException(
                status_code=500, detail="创建项目失败（成员授权写入失败，已回滚）",
            ) from e

    # 后台启动预处理（不阻塞响应）。D16③：一律用 store 返回的真实 id/path。
    real_project_id = project["id"]
    real_project_path = project.get("path") or resolved_path

    # D20：创建路径同样先认领 in-flight 守卫（与 trigger_preprocess 同一 CAS），
    # 堵住"创建后立刻手动 trigger"窗口内的并发双跑。认领失败=已有执行者，跳过 spawn。
    from swarm.project.preprocess import _preprocess_timeout_sec as _pp_timeout_sec
    try:
        _pp_claimed = await loop.run_in_executor(
            None,
            lambda: _app.store.claim_preprocess_slot(
                real_project_id, stale_after_sec=_pp_timeout_sec() + 600),
        )
    except Exception:  # noqa: BLE001 — 守卫自身故障不阻断创建；preprocess 可手动重触发
        _app.logger.exception("claim_preprocess_slot failed for %s（跳过自动预处理）", real_project_id)
        _pp_claimed = False

    async def _run_preprocess():
        try:
            from swarm.project.preprocess import preprocess_project
            await preprocess_project(real_project_id, real_project_path)
        except Exception as e:
            _app.logger.error(f"Preprocessing failed for project {real_project_id}: {e}")
            # D20：preprocess_project 入口前的意外失败会让项目卡 PREPROCESSING——
            # best-effort 置 ERROR 释放 in-flight 守卫。
            try:
                await loop.run_in_executor(
                    None, lambda: _app.store.update_project(real_project_id, status="ERROR"),
                )
            except Exception:  # noqa: BLE001
                pass

    if _pp_claimed:
        _app._spawn_bg(_run_preprocess())  # D4：走 H9 强引用集，防 fire-and-forget 任务被 GC 静默回收
    else:
        _app.logger.info("项目 %s 预处理已有执行者/守卫未认领，跳过自动 spawn", real_project_id)

    return {"status": "ok", "project": project}


# ─── 3. GET /api/projects/{project_id} — 项目详情 ─
@router.get("/api/projects/{project_id}", tags=["项目管理"])
async def get_project(project_id: str, request: Request):
    """获取项目详情"""
    _require_perm(request, "project:read", project_id)
    loop = asyncio.get_running_loop()
    project = await loop.run_in_executor(None, _app.store.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return {"project": project}


# ─── 4. DELETE /api/projects/{project_id} — 删除项目 ─
@router.delete("/api/projects/{project_id}", tags=["项目管理"])
async def delete_project(project_id: str, request: Request):
    """删除项目及其关联数据。

    删除前先级联取消该项目所有运行中的任务+释放沙箱，否则正在跑的 asyncio 任务
    会因 DB 记录被删而失去取消入口，变成幽灵任务陷入 replan 死循环持续烧 GPU。
    """
    _require_perm(request, "project:delete", project_id)
    loop = asyncio.get_running_loop()
    # 先确认项目存在
    project = await loop.run_in_executor(None, _app.store.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    # 级联终止运行中任务（在删 DB 记录之前，确保 cancel_task 还能查到 task）
    try:
        from swarm.brain.runner import cancel_project_tasks
        cancelled = await cancel_project_tasks(project_id)
        if cancelled:
            _app.logger.info("删除项目 %s 前级联取消了 %d 个运行中任务", project_id, cancelled)
    except Exception:
        _app.logger.exception("删除项目 %s 前级联取消任务失败（继续删除）", project_id)

    deleted = await loop.run_in_executor(None, _app.store.delete_project, project_id)
    if not deleted:
        raise HTTPException(status_code=500, detail="Failed to delete project")

    # 12.5：PG 级联已在 store.delete_project 事务内完成。Qdrant 向量在事务外
    # best-effort 清理——失败仅告警不阻断（残留向量是孤儿，后续可清理/被覆盖，
    # 不应因远程抖动让用户删不掉项目）。
    try:
        from swarm.knowledge.semantic_index import SemanticIndexer
        indexer = SemanticIndexer()
        await indexer.connect()
        try:
            await indexer.delete_by_project(project_id)
        finally:
            await indexer.close()
    except Exception:
        _app.logger.warning(
            "删除项目 %s 的 Qdrant 向量失败（孤儿向量将残留，可后续清理）", project_id,
            exc_info=True,
        )
    return {"status": "ok", "message": f"Project {project_id} deleted"}


# ─── 5. POST /api/projects/{project_id}/preprocess — 手动触发预处理 ─
@router.post("/api/projects/{project_id}/preprocess", tags=["项目管理"],
             dependencies=[Depends(rate_limit("preprocess", capacity=10, rate=0.2))])  # C7
async def trigger_preprocess(project_id: str, request: Request):
    """手动触发/重新触发项目预处理"""
    _require_perm(request, "project:write", project_id)  # P0-SEC-03
    loop = asyncio.get_running_loop()
    try:
        project = await loop.run_in_executor(None, _app.store.get_project, project_id)
    except Exception as e:
        _app.logger.exception("Failed to load project %s for preprocess", project_id)
        raise HTTPException(status_code=503, detail="数据库暂时不可用") from e

    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    project_path = project["path"]

    # D20：in-flight 守卫——DB CAS 原子认领（含同事务进度重置），并发双触发只有一个
    # 拿到执行权，杜绝两个 preprocess 并发交错互删 kb_symbol_index / Qdrant 代际向量。
    # stale 判定：正常运行由 preprocess 总超时（wait_for）兜底必在窗口内落终态并 bump
    # updated_at；PREPROCESSING 且超过【总超时+10min】未动 = 崩溃残留，允许重入（不永拒）。
    from swarm.project.preprocess import _preprocess_timeout_sec
    stale_after = _preprocess_timeout_sec() + 600
    try:
        claimed = await loop.run_in_executor(
            None,
            lambda: _app.store.claim_preprocess_slot(project_id, stale_after_sec=stale_after),
        )
    except Exception as e:
        _app.logger.exception("Failed to claim preprocess slot for %s", project_id)
        raise HTTPException(status_code=500, detail="启动预处理失败，请稍后重试") from e
    if not claimed:
        raise HTTPException(
            status_code=409,
            detail="该项目已在预处理中，请等待完成后再触发",
        )

    # 后台启动预处理
    async def _run_preprocess():
        try:
            from swarm.project.preprocess import preprocess_project
            await preprocess_project(project_id, project_path)
        except Exception:
            _app.logger.exception("Preprocessing failed for project %s", project_id)
            # D20：入口前意外失败会让项目卡 PREPROCESSING——best-effort 置 ERROR 释放守卫。
            try:
                await loop.run_in_executor(
                    None, lambda: _app.store.update_project(project_id, status="ERROR"),
                )
            except Exception:  # noqa: BLE001
                pass

    _app._spawn_bg(_run_preprocess())  # D4：走 H9 强引用集，防 fire-and-forget 任务被 GC 静默回收
    _app.logger.info("Preprocess queued for project %s path=%s", project_id, project_path)

    return {"status": "ok", "message": f"Preprocessing started for project {project_id}"}


# ─── 6b. GET /api/projects/{project_id}/preprocess/status — 预处理状态快照 ─
@router.get("/api/projects/{project_id}/preprocess/status", tags=["项目管理"])
async def get_preprocess_status(project_id: str, request: Request):
    """返回当前预处理进度（非 SSE，供 Tab 打开时加载）"""
    _require_perm(request, "project:read", project_id)  # P0-SEC-03
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)
    progress = await loop.run_in_executor(None, _app.store.get_progress, project_id)
    project = await loop.run_in_executor(None, _app.store.get_project, project_id)
    return {
        "project_status": project.get("status") if project else None,
        "progress": progress,
    }


# ─── 6. GET /api/projects/{project_id}/preprocess/progress — SSE 预处理进度流 ─
@router.get("/api/projects/{project_id}/preprocess/progress", tags=["项目管理"])
async def stream_preprocess_progress(project_id: str, request: Request):
    """SSE 流式推送项目预处理进度

    事件格式: event: progress, data: {phase, phase_progress, message, ...}
    当 phase 为 complete 或 error 时发送后关闭流。
    认证：EventSource 不能带头，中间件从 ?token= 读取；此处补 project:read 授权。
    """
    _require_perm(request, "project:read", project_id)  # P0-SEC-03

    async def event_generator():
        last_phase = None
        last_progress = -1.0
        idle_count = 0
        reauth_tick = 0

        while True:
            # round27（C6 同族补漏）：大项目预处理可持续数分钟，每 ~10s 重校一次授权——
            # token 吊销/成员被移除即断流（复用 task.py 的 _stream_reauthorized 模板）。
            reauth_tick += 1
            if reauth_tick >= 20:
                reauth_tick = 0
                from swarm.api.routers.task import _stream_reauthorized
                if not _stream_reauthorized(request, {"project_id": project_id}, "project:read"):
                    yield {"event": "progress", "data": json.dumps(
                        {"phase": "error", "message": "auth_revoked", "error": "auth_revoked"})}
                    return
            loop = asyncio.get_running_loop()
            progress = await loop.run_in_executor(None, _app.store.get_progress, project_id)

            if progress is None:
                # 尚无进度记录 — 项目可能刚创建
                yield {
                    "event": "progress",
                    "data": json.dumps({
                        "phase": "idle",
                        "phase_progress": 0.0,
                        "message": "Waiting for preprocessing to start...",
                    }),
                }
                idle_count += 1
                if idle_count > 60:  # 等待 60 秒仍无记录则关闭
                    yield {
                        "event": "progress",
                        "data": json.dumps({
                            "phase": "error",
                            "phase_progress": 0.0,
                            "message": "Preprocessing did not start within timeout",
                            "error": "timeout",
                        }),
                    }
                    return
                await asyncio.sleep(1.0)
                continue

            phase = progress.get("phase", "idle")
            phase_progress = progress.get("phase_progress", 0.0)

            # 只在状态变化时推送（减少冗余事件）
            if phase != last_phase or abs(phase_progress - last_progress) > 0.01:
                yield {
                    "event": "progress",
                    "data": json.dumps(progress, default=str),
                }
                last_phase = phase
                last_progress = phase_progress

            # 终止条件
            if phase in ("complete", "error"):
                return

            await asyncio.sleep(0.5)

    return EventSourceResponse(event_generator())
