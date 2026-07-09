"""api/routers/task.py — 任务管理域路由 (列表/创建/SSE流/详情/删除/取消/重试/审批/diff/修订/拒绝)。

从 api/app.py 抽出, app.include_router 挂载。
mock 锚点(store)及 app 级 get_config/logger 用 _app. 属性访问。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect

from swarm.api.rate_limit import rate_limit  # C7
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

import swarm.api.app as _app
from swarm.api._shared import ApplyDiffRequest, _require_perm, _require_user

router = APIRouter()


def _require_task_access(request, task, task_id: str, perm: str):
    """C5 治本：任务鉴权统一口径——【不存在】与【无权】返回同一 generic 404，防 task_id 存在性
    枚举（旧代码 not-found→404、有但无权→403 可区分）。对齐 WS 端点已有的通用拒绝。
    认证失败(无 token)仍由 _require_user 抛 401（与任务是否存在无关，不泄露存在性）。
    """
    from swarm.api._shared import _require_user as _ru
    from swarm.auth.store import user_can_on_project
    from fastapi import HTTPException as _HTTPException
    user = _ru(request)
    if not task or not user_can_on_project(user, perm, (task or {}).get("project_id")):
        raise _HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return task


async def _require_task_access_async(request, task, task_id: str, perm: str):
    """D48：_require_task_access 的卸线程版（鉴权两条 PG 查询不再冻结事件循环）。
    语义与同步版一致（同一函数经 to_thread），HTTPException 原样穿透。"""
    return await asyncio.to_thread(_require_task_access, request, task, task_id, perm)


def _sse_reauth_interval_s() -> float:
    """D48：SSE 连接后周期重认证间隔（秒），env 可配降频。

    默认 30s（stream_task 心跳既有节奏）；stream_task_logs 原每 5s 重校一次（每客户端每次
    都是同步 get_user_by_token+成员查询打在事件循环上），统一降频到本间隔。下限 5s 防误配。
    """
    import os
    try:
        v = float(os.environ.get("SWARM_SSE_REAUTH_INTERVAL_S", "30"))
    except ValueError:
        v = 30.0
    return max(5.0, v)


def _stream_reauthorized(request, task, perm: str) -> bool:
    """C6 治本：流连接建立后周期性重校——token 吊销/过期或成员被移除即返回 False（断流）。
    旧代码仅在连接建立时鉴权一次，之后放任事件流至自然结束（失权后仍能观察敏感进度）。"""
    try:
        from swarm.api._shared import _require_user as _ru
        from swarm.auth.store import user_can_on_project
        user = _ru(request)  # 重读 token：get_user_by_token 过滤 token_revoked/expired
        return bool(task) and user_can_on_project(user, perm, (task or {}).get("project_id"))
    except Exception as exc:  # noqa: BLE001
        # 复核 SF-2：DB 瞬时抖动也进这里——记 warning 便于区分【真实吊销】vs【基础设施噪声】，
        # 仍 fail-closed 断流(SSE 客户端会自动重连，重连时在连接口重新鉴权，安全)。
        try:
            _app.logger.warning("[stream] 连接后重鉴权异常(疑 DB 瞬时)，按失权断流: %s", exc)
        except Exception:  # noqa: BLE001
            pass
        return False


class TaskCreateRequest(BaseModel):
    """创建任务请求"""
    description: str = Field(description="任务描述")
    auto_accept: bool = Field(default=False, description="自动通过审核（E2E/演示）")
    priority: str = Field(default="normal", description="队列优先级: urgent / normal / background")
    force: bool = Field(default=False, description="跳过重复检测，强制新建（即使有同描述的进行中任务）")
    # B 部分：多模态摄取
    uploaded_files: list[str] = Field(default_factory=list, description="上传文件路径（来自 /api/uploads）")
    auto_confirm_vision: bool = Field(default=False, description="模型自行确认图片理解（跳过人工确认）")
    pooled: bool = Field(default=False, description="仅入需求池（不立即执行），稍后手动触发")


class TaskReviseRequest(BaseModel):
    """审核修订请求"""
    feedback: str = Field(description="修订反馈意见")


class TaskRetryRequest(BaseModel):
    """重跑任务请求"""
    auto_accept: bool | None = Field(default=None, description="自动通过审核（默认沿用环境变量）")


class ApproveTaskRequest(BaseModel):
    """审核通过选项"""
    apply_diff: bool = Field(
        default=False,
        description="显式 git apply；sandbox_first 模式下通常已由 pull-back 写回本地",
    )


def _task_list_default_limit() -> int:
    """D49：任务列表默认分页上限（env 可配）。

    取证（api/static/js/tabs/tasks.js / core/project.js / cli task_list）：全部消费者只读
    id/status/description/complexity 且无分页参数、期待"项目内全部任务"。默认取大（500）
    保住现有 UI/CLI 的全量列表预期——超过 500 条历史任务的长寿项目从此每次轮询只搬最近
    500 条（并记 WARN），而不是无上限搬全库。下限钳 1。
    """
    import os
    try:
        v = int(os.environ.get("SWARM_TASK_LIST_DEFAULT_LIMIT", "500"))
    except ValueError:
        v = 500
    return max(1, v)


@router.get("/api/projects/{project_id}/tasks", tags=["任务管理"])
async def list_tasks(project_id: str, request: Request,
                     limit: int | None = None, offset: int = 0):
    """获取项目下的任务列表（轻量列，D49）。

    - 分页：limit/offset 可选；缺省 limit=SWARM_TASK_LIST_DEFAULT_LIMIT（默认 500）。
    - 列表视图不再携带 merged_diff/plan/l3_result/token_usage 等重字段（MB 级 diff 每次
      轮询全量搬运是热点病灶）；详情仍走 GET /api/tasks/{id} 全量。
    """
    from swarm.api._shared import _require_perm_async
    await _require_perm_async(request, "task:read", project_id)  # P0-SEC-03 + D48 卸线程
    loop = asyncio.get_running_loop()
    # 确认项目存在
    project = await loop.run_in_executor(None, _app.store.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    eff_limit = limit if (limit is not None and limit > 0) else _task_list_default_limit()
    eff_offset = max(0, offset)
    tasks = await loop.run_in_executor(
        None, lambda: _app.store.list_tasks_light(project_id, limit=eff_limit, offset=eff_offset)
    )
    if limit is None and len(tasks) >= eff_limit:
        _app.logger.warning(
            "[list_tasks] project=%s 任务数达到默认分页上限 %d，列表被截断（消费者可传 limit/offset 翻页）",
            project_id, eff_limit,
        )
    return {"tasks": tasks, "limit": eff_limit, "offset": eff_offset}


# ─── 8. POST /api/projects/{project_id}/tasks — 创建任务 ─
@router.post("/api/projects/{project_id}/tasks", tags=["任务管理"],
             dependencies=[Depends(rate_limit("task_create", capacity=20, rate=0.5))])  # C7
async def create_task(project_id: str, req: TaskCreateRequest, request: Request):
    """创建任务并后台启动 Brain 编排"""
    user = _require_perm(request, "task:create", project_id)
    # 任务描述严格必填（入口硬门槛）：附件只作补充，不能替代描述。
    # 后端兜底校验——前端校验可被绕过/直接调 API，这里是单一可信防线。
    if not (req.description or "").strip():
        raise HTTPException(status_code=400, detail="任务描述不能为空（附件只作补充，不能替代描述）")
    # #5(b) LFI 防护：uploaded_files 客户端可控，写入端清洗可被直接建任务绕过 → 入口复核每个
    # 路径落在 uploads 根内，否则任意 task:create 用户可读服务器任意文件（内容会并入描述回显）。
    if req.uploaded_files:
        from swarm.api.routers.upload import path_is_within_uploads
        _bad = [p for p in req.uploaded_files if not path_is_within_uploads(p)]
        if _bad:
            _app.logger.warning("拒绝越界 uploaded_files（疑似 LFI）: %s", _bad[:5])
            raise HTTPException(
                status_code=400,
                detail="附件路径非法：仅接受经 /api/uploads 上传的文件（拒绝越界路径）",
            )
    loop = asyncio.get_running_loop()
    project = await loop.run_in_executor(None, _app.store.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    from swarm.knowledge.readiness import brain_task_ready

    progress = await loop.run_in_executor(None, _app.store.get_progress, project_id)
    ready, reason = brain_task_ready(project, progress)
    if not ready:
        # 可诊断性：拒绝创建任务=对运维可见的决策点（区分 degraded/missing/error），
        # 否则只有客户端能看到 409，服务端无审计痕迹。
        _app.logger.warning(
            "拒绝创建 Brain 任务: project_id=%s 知识库未就绪 — %s",
            project_id, reason,
        )
        raise HTTPException(
            status_code=409,
            detail=reason or "项目知识库未就绪，请先完成预处理",
        )

    # 去重：同项目内已有「相同描述 + 进行中（非终态）」任务时，默认不重复创建，
    # 直接复用并返回已有任务（避免误触/重复提交建出多个一样的任务）。
    # force=true 可显式绕过。终态任务不算重复，允许重新发起。
    if not req.force:
        dup = await loop.run_in_executor(
            None,
            lambda: _app.store.find_active_duplicate_task(project_id, req.description),
        )
        if dup:
            return {
                "status": "duplicate",
                "task": dup,
                "message": (
                    f"已存在进行中的同描述任务（#{str(dup.get('id'))[:8]}，"
                    f"状态 {dup.get('status')}）。如需强制新建请用 force=true。"
                ),
            }

    task_id = str(uuid.uuid4())
    # 需求池模式（B.5）：仅入池，状态 POOLED，不进调度。
    initial_status = "POOLED" if req.pooled else "SUBMITTED"
    # P0-A：队列执行 meta（auto_accept + priority）随初始状态一并落库，
    # 供 leader 重启后从 DB 重建 _pending_meta（否则出队缺 meta → 静默丢）。
    priority = getattr(req, "priority", "normal") or "normal"
    try:
        # D22 治本：初始状态 + 执行 meta 随 create_task【同一条 INSERT】原子落库。
        # 旧链路 create_task + update_task 两条 autocommit：第二条失败 → SUBMITTED 行
        # 残留且缺 meta、未入调度队列 → 长稳进程该任务永久卡死至重启对账。
        task = await loop.run_in_executor(
            None,
            lambda: _app.store.create_task(
                task_id=task_id,
                project_id=project_id,
                description=req.description,
                created_by_user_id=user.id,
                uploaded_files=req.uploaded_files or [],
                auto_confirm_vision=req.auto_confirm_vision,
                pooled=req.pooled,
                status=initial_status,
                thread_id=task_id,
                auto_accept=bool(req.auto_accept),
                queue_priority=priority,
            ),
        )
    except Exception as e:
        _app.logger.error("Failed to create task: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="创建任务失败") from e

    # 需求池模式：不立即执行，等用户手动「执行」触发（POST .../execute）。
    if req.pooled:
        await loop.run_in_executor(
            None,
            lambda: _app.store.create_notification(
                "task_created",
                task_id=task_id,
                project_id=project_id,
                title="任务已入池",
                message=f"#{task_id[:8]} {(req.description or '')[:80]}（待执行）",
            ),
        )
        task = await loop.run_in_executor(None, _app.store.get_task, task_id)
        return {"status": "pooled", "task": task}

    from swarm.brain.scheduler import submit_task

    # 入优先级队列，由准入调度器按并发上限执行（urgent>normal>background）。
    # priority 已在上方落库时算好（同一事实源，避免二次计算漂移）。
    submit_task(
        task_id, project_id, req.description,
        auto_accept=req.auto_accept, priority=priority,
    )

    # 应用内通知：任务已建立（带 task_id）
    short = (req.description or "")[:80]
    await loop.run_in_executor(
        None,
        lambda: _app.store.create_notification(
            "task_created",
            task_id=task_id,
            project_id=project_id,
            title="任务已建立",
            message=f"#{task_id[:8]} {short}",
        ),
    )

    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    return {"status": "ok", "task": task}




# ─── 9. GET /api/tasks/{task_id}/stream — SSE 任务进度 ─
@router.get("/api/tasks/{task_id}/stream", tags=["任务管理"])
async def stream_task(task_id: str, request: Request):
    """SSE 流式推送任务 Brain 执行进度"""
    from swarm.brain.runner import subscribe_task

    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    # P0-SEC-03/C5：进度流需 task:read 授权；不存在与无权统一 404（防跨项目订阅 + 存在性枚举）。
    # D48：鉴权 PG 查询卸线程。
    await _require_task_access_async(request, task, task_id, "task:read")

    # N-CW1/N-CW2：每个连接订阅【自己的】队列（多端互不抢事件），断开时注销回收。
    topic, queue = subscribe_task(task_id)

    async def event_generator():
        try:
            while True:
                try:
                    event_data = await asyncio.wait_for(queue.get(), timeout=_sse_reauth_interval_s())
                except asyncio.TimeoutError:
                    # C6：每心跳(默认~30s，env 可配)重校 token/成员——吊销/踢出即断流。
                    # D48：重认证的两条同步 PG 查询卸线程（每连接每心跳都打在事件循环上是系统性冻结源）。
                    if not await asyncio.to_thread(_stream_reauthorized, request, task, "task:read"):
                        yield {"event": "error",
                               "data": json.dumps({"step": "error", "message": "认证已失效，连接关闭"})}
                        break
                    yield {"event": "heartbeat", "data": ""}
                    continue

                step = event_data.get("step", "")
                # D18：step:"result" 死协议已废——终态载荷并入 complete 事件（runner._handle_post_run），
                # 此处不再有 result 事件类型映射。
                event_type = "progress"
                if step == "error":
                    event_type = "error"
                elif step == "awaiting_review":
                    event_type = "awaiting_review"

                yield {
                    "event": event_type,
                    "data": json.dumps(event_data, ensure_ascii=False, default=str),
                }

                # D18：cancelled 也是终止事件——旧 break 集合漏它 → cancel 后流永久挂起（每 30s 心跳）。
                if step in ("complete", "error", "awaiting_review", "cancelled"):
                    break
        except asyncio.CancelledError:
            pass
        finally:
            topic.unsubscribe(queue)  # N-CW2：断开即注销，避免内存增长

    return EventSourceResponse(event_generator())


# ─── 9b. WS /ws/tasks/{task_id} — WebSocket 任务进度（与 SSE 并存）──
@router.websocket("/ws/tasks/{task_id}")
async def ws_task_progress(websocket: WebSocket, task_id: str):
    """WebSocket 推送任务 Brain 执行进度

    与 SSE 共享同一 fanout 主题，但【各自独立订阅队列】（N-CW1：不再争抢同一 queue）。
    消息格式: JSON {"event": "progress"|"result"|"error"|"heartbeat", "data": {...}}
    连接断开时优雅处理并注销订阅。
    """
    from swarm.brain.runner import subscribe_task

    # #9/#20：鉴权【先于 accept】——token 无效直接在握手阶段拒绝，不建立连接（更严，
    # 未鉴权连接不会被短暂建立）。鉴权也必须先于任务存在性查询，否则可凭 not-found
    # 区分 task_id 是否存在（枚举预言机）。authenticate_ws 从 scope 读 token，无需 accept。
    from swarm.api.auth import authenticate_ws
    from swarm.auth.store import user_can_on_project

    user = authenticate_ws(websocket)
    if user is None:
        await websocket.close(code=1008)  # 握手阶段拒绝（不 accept）
        return

    await websocket.accept()

    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    # 不存在 OR 无 task:read 权限 → 统一通用拒绝（不区分，防任务存在性枚举）。
    if not task or not user_can_on_project(user, "task:read", task.get("project_id")):
        await websocket.send_json({"event": "error", "data": {"detail": "Not found or access denied"}})
        await websocket.close(code=1008)
        return

    # N-CW1/N-CW2：独立订阅 + 断开注销
    topic, queue = subscribe_task(task_id)

    try:
        while True:
            try:
                event_data = await asyncio.wait_for(queue.get(), timeout=30)
            except asyncio.TimeoutError:
                # C6：每心跳(~30s)重校 token/成员——吊销/踢出即断连（重跑 authenticate_ws 读新 token）。
                _u = authenticate_ws(websocket)
                if _u is None or not user_can_on_project(_u, "task:read", task.get("project_id")):
                    await websocket.send_json({"event": "error", "data": {"detail": "认证已失效"}})
                    await websocket.close(code=1008)
                    break
                # 心跳：防止连接空闲超时
                await websocket.send_json({"event": "heartbeat", "data": ""})
                continue

            step = event_data.get("step", "")
            # D18：step:"result" 死协议已废——终态载荷并入 complete 事件（与 SSE 对称）。
            event_type = "progress"
            if step == "error":
                event_type = "error"
            elif step == "awaiting_review":
                event_type = "awaiting_review"

            await websocket.send_json({
                "event": event_type,
                "data": event_data,
            })

            # 终止事件（D18：cancelled 也终止，与 SSE 对称——否则 cancel 后 WS 永久挂起）
            if step in ("complete", "error", "awaiting_review", "cancelled"):
                break
    except WebSocketDisconnect:
        # 客户端断开连接 — 优雅退出
        pass
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        _app.logger.warning("WebSocket /ws/tasks/%s 异常: %s", task_id, exc)
    finally:
        topic.unsubscribe(queue)  # N-CW2：断开即注销订阅
        try:
            await websocket.close()
        except Exception:
            pass


# ─── 9. GET /api/tasks/{task_id} — 任务详情 ──────
# 注：/api/tasks/audit 必须在此【之前】注册，否则 'audit' 会被 {task_id} 捕获。
@router.get("/api/tasks/audit", tags=["任务管理"])
async def task_audit_endpoint(request: Request, task_id: str = "", project_id: str = "", limit: int = 100):
    """查询任务审计日志（append-only，含已删除任务的生命周期留痕）。

    解决可追溯性：即使任务/项目被硬删，仍能在此查到它的创建/删除记录与描述。
    """
    # P0-SEC-03 / #5(a)：跨项目审计读越权治本。
    #  - limit 封顶（防 ?limit=999999 拖全库）。
    #  - project_id 查询 → 校验该项目 task:read。
    #  - task_id 查询 → 反查任务归属校验；已删除任务(get_task None)无法反查 → 非 admin 限成员项目。
    #  - 无过滤 → admin 全量、非 admin 限成员项目（绝不再让任意登录用户读全库审计）。
    limit = max(1, min(limit, 500))
    loop = asyncio.get_running_loop()

    def _member_scope(user) -> "list[str] | None":
        from swarm.auth.rbac import Role
        from swarm.auth.store import list_user_project_ids
        if getattr(user, "global_role", "") == Role.ADMIN.value:
            return None  # admin：不限 scope
        return list(list_user_project_ids(user.id))

    if project_id:
        _require_perm(request, "task:read", project_id)
        rows = await loop.run_in_executor(
            None,
            lambda: _app.store.list_task_audit(
                task_id=task_id or None, project_id=project_id, limit=limit),
        )
    elif task_id:
        user = _require_user(request)
        task = await loop.run_in_executor(None, _app.store.get_task, task_id)
        if task and task.get("project_id"):
            _require_perm(request, "task:read", task.get("project_id"))
            rows = await loop.run_in_executor(
                None,
                lambda: _app.store.list_task_audit(
                    task_id=task_id, project_id=task.get("project_id"), limit=limit),
            )
        else:  # 已删除任务：无法反查归属 → 非 admin 限成员项目
            _pids = _member_scope(user)
            rows = await loop.run_in_executor(
                None,
                lambda: _app.store.list_task_audit(
                    task_id=task_id, limit=limit, project_ids=_pids),
            )
    else:
        user = _require_user(request)
        _pids = _member_scope(user)
        rows = await loop.run_in_executor(
            None,
            lambda: _app.store.list_task_audit(limit=limit, project_ids=_pids),
        )
    return {"status": "ok", "audit": rows}


@router.get("/api/tasks/{task_id}", tags=["任务管理"])
async def get_task(task_id: str, request: Request):
    """获取任务详情"""
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    await _require_task_access_async(request, task, task_id, "task:read")  # D48：卸线程
    return {"task": jsonable_encoder(task)}


@router.delete("/api/tasks/{task_id}", tags=["任务管理"])
async def delete_task_endpoint(task_id: str, request: Request, force: bool = False):
    """删除任务；force=true 时先取消运行中任务；orphaned 活跃任务可直接删除"""
    from swarm.brain.runner import (
        _ACTIVE_DB_STATUSES,
        cancel_task,
        is_task_orphaned,
        is_task_running,
    )

    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    _require_task_access(request, task, task_id, "task:cancel")  # 删除=终止性操作，owner/developer 可

    status = task.get("status", "")
    if is_task_running(task_id):
        if not force:
            raise HTTPException(status_code=409, detail="任务正在执行中，请使用 force=true 强制删除")
        await cancel_task(task_id)
    elif status in _ACTIVE_DB_STATUSES and not is_task_orphaned(task_id):
        if not force:
            raise HTTPException(status_code=409, detail="任务处于活跃状态，请使用 force=true 强制删除")

    deleted = await loop.run_in_executor(None, _app.store.delete_task, task_id)
    if not deleted:
        raise HTTPException(status_code=500, detail="删除失败")
    return {"status": "ok", "message": f"任务 {task_id} 已删除"}


@router.post("/api/tasks/{task_id}/cancel", tags=["任务管理"])
async def cancel_task_endpoint(task_id: str, request: Request):
    """取消运行中任务，或将 orphaned 活跃任务标记为已取消"""
    from swarm.brain.runner import cancel_task, is_task_orphaned, is_task_running

    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    _require_task_access(request, task, task_id, "task:cancel")

    if not is_task_running(task_id) and not is_task_orphaned(task_id):
        from swarm.task_states import TERMINAL_STATES

        status = task.get("status", "")
        if status in TERMINAL_STATES:  # #3：PARTIAL 也是终态（含 DONE/FAILED/CANCELLED）
            return {"status": "ok", "task": task, "message": "任务已结束，无需取消"}
        raise HTTPException(status_code=409, detail=f"任务状态 {status} 不可取消")

    await cancel_task(task_id)
    updated = await loop.run_in_executor(None, _app.store.get_task, task_id)
    return {"status": "ok", "task": jsonable_encoder(updated), "message": "任务已取消"}


@router.post("/api/tasks/{task_id}/retry", tags=["任务管理"])
async def retry_task_endpoint(task_id: str, request: Request, req: TaskRetryRequest | None = None):
    """重跑失败/已取消/orphaned 任务"""
    from swarm.brain.runner import can_retry_task, register_task_queue, retry_task_background

    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    _require_task_access(request, task, task_id, "task:create")  # 重跑=重新发起执行

    allowed, reason = can_retry_task(task_id)
    if not allowed:
        raise HTTPException(status_code=409, detail=reason or "当前状态不可重跑")

    auto_accept = req.auto_accept if req else None
    register_task_queue(task_id)
    retry_task_background(task_id, auto_accept=auto_accept)
    return {"status": "ok", "task": jsonable_encoder(task), "message": "已提交重跑，Brain 重新执行"}


# ─── POST /api/tasks/{task_id}/execute — 执行需求池任务（B.5）─
@router.post("/api/tasks/{task_id}/execute", tags=["任务管理"])
async def execute_pooled_task(task_id: str, req: TaskRetryRequest | None = None, request: Request = None):  # type: ignore[assignment]
    """把需求池里的 POOLED 任务转入执行（B.5 需求池模式）。

    仅 POOLED 状态可执行；转 SUBMITTED 并入调度队列，走正常 Brain 流程
    （含 ingest 摄取已上传的文件）。
    """
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    _require_task_access(request, task, task_id, "task:create")  # 起跑需求池=发起执行
    if task.get("status") != "POOLED":
        raise HTTPException(
            status_code=409,
            detail=f"任务状态为 {task.get('status')}，仅 POOLED（需求池）任务可执行",
        )

    # 转 SUBMITTED + 清 pooled 标记；P0-A：队列执行 meta 一并落库（重启可重建）。
    auto_accept = req.auto_accept if req else False
    await loop.run_in_executor(
        None,
        lambda: _app.store.update_task(
            task_id, status="SUBMITTED",
            auto_accept=bool(auto_accept), queue_priority="normal",
        ),
    )

    from swarm.brain.scheduler import submit_task

    submit_task(
        task_id, task["project_id"], task["description"],
        auto_accept=bool(auto_accept), priority="normal",
    )
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    return {"status": "ok", "task": jsonable_encoder(task), "message": "已从需求池触发执行"}


# ─── GET /api/tasks/{task_id}/logs — 该任务执行日志 ─
@router.get("/api/tasks/{task_id}/logs", tags=["任务管理"])
async def get_task_logs(task_id: str, request: Request, limit: int = 500):
    """读取某任务的执行日志（从 swarm.log 按 [task=前8位] 过滤）。

    依赖统一日志系统的 task 上下文前缀（swarm.logging_config.bind/set_task_context）。
    """
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    await _require_task_access_async(request, task, task_id, "task:read")  # D48：卸线程

    from swarm.logging_config import read_task_logs, resolve_log_path

    # 上限 2000→20000：ultra 任务单轮日志轻松上万行，2000 封顶导致 WebUI 只见尾段、早期阶段被截。
    limit = max(1, min(int(limit or 500), 20000))
    lines = await loop.run_in_executor(None, lambda: read_task_logs(task_id, limit=limit))
    log_path = resolve_log_path()
    return {
        "task_id": task_id,
        "status": task.get("status"),
        "count": len(lines),
        "lines": lines,
        "log_file": str(log_path) if log_path else None,
        "hint": "" if lines else "暂无该任务日志（可能任务在日志轮转前执行，或日志文件未配置）",
    }


# ─── GET /api/tasks/{task_id}/logs/stream — 实时日志 SSE ─
@router.get("/api/tasks/{task_id}/logs/stream", tags=["任务管理"])
async def stream_task_logs(task_id: str, request: Request):
    """SSE 实时推送某任务的执行日志（tail swarm.log 按 [task=前8位] 过滤）。

    纯文件读，不触发任何任务执行。任务进入终态后自动结束流。
    认证：中间件从 ?token= 读取（EventSource 不能带 Authorization 头）。
    """
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    await _require_task_access_async(request, task, task_id, "task:read")  # D48：卸线程

    from swarm.logging_config import TaskLogPoller

    # #3 round22：PARTIAL 也是终态，漏它 → PARTIAL 任务 SSE 永不 event:end、流悬挂。
    from swarm.task_states import TERMINAL_STATES as _TERMINAL

    async def event_generator():
        poller = TaskLogPoller(task_id)
        terminal_idle = 0
        db_tick = 0
        # D48：重认证降频——旧节奏每 5s 一次（每客户端两条同步 PG 查询），降到与 stream_task
        # 同源的 env 可配间隔（默认 30s）。终态检查仍每 5 拍（收尾延迟语义不变）。
        reauth_every_ticks = max(1, int(_sse_reauth_interval_s() / 5.0))
        reauth_tick = 0
        try:
            while True:
                batch = await loop.run_in_executor(None, poller.poll)
                if batch:
                    for line in batch:
                        yield {"event": "log", "data": line}
                    terminal_idle = 0
                    continue

                # 无新行：心跳（每秒），DB 检查（终态+重鉴权）降频到每 5 拍——
                # round27 perf：旧逻辑每秒 get_task，7h 长任务单客户端 2.5 万次无谓 DB 查询。
                # 终态收尾/失权断流延迟 1s→最长 5s，可接受。
                yield {"event": "heartbeat", "data": ""}
                db_tick += 1
                if db_tick >= 5:
                    db_tick = 0
                    cur = await loop.run_in_executor(None, _app.store.get_task, task_id)
                    # round27（C6 同族补漏）：日志流含代码/构建输出等敏感内容，与 stream_task 一致
                    # 周期性重校——token 吊销/成员被移除即断流，不再"连上后失权仍可看到任务终态"。
                    # D48：卸线程 + 按 SWARM_SSE_REAUTH_INTERVAL_S 降频（失权断流延迟上限=该间隔）。
                    reauth_tick += 1
                    if reauth_tick >= reauth_every_ticks:
                        reauth_tick = 0
                        if not await asyncio.to_thread(
                            _stream_reauthorized, request, cur or task, "task:read"
                        ):
                            yield {"event": "end", "data": "auth_revoked"}
                            break
                    if cur and cur.get("status") in _TERMINAL:
                        terminal_idle += 1
                        # 终态后再多轮询一次确保尾部日志吐完，然后收尾
                        if terminal_idle >= 2:
                            yield {"event": "end", "data": cur.get("status")}
                            break
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass

    return EventSourceResponse(event_generator())


# ─── 10. POST /api/tasks/{task_id}/approve — 审核通过 ─
@router.post("/api/tasks/{task_id}/approve", tags=["任务管理"])
async def approve_task(task_id: str, request: Request, req: ApproveTaskRequest | None = None):
    """审核通过 — 可选 apply diff + 增量知识更新，然后 resume Brain"""
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    _require_task_access(request, task, task_id, "task:approve")

    # P1-A 原子认领：仅当任务处于【计划确认/结果审核】态才推进（一次成功）。双击/重复提交时
    # 第二次的同一条件 UPDATE 匹配 0 行 → None → 走幂等无副作用分支（不重复 apply diff / 触发 resume）。
    # 认领即写 human_decision=ACCEPT + 状态推 ANALYZING（与 resume_task accept 路径一致）。
    from swarm.task_states import PLAN_RESULT_REVIEW_STATES

    orig_status = task.get("status")
    claimed = await loop.run_in_executor(
        None,
        lambda: _app.store.claim_human_gate(
            task_id, PLAN_RESULT_REVIEW_STATES, "ANALYZING", human_decision="ACCEPT"
        ),
    )
    if claimed is None:
        current = await loop.run_in_executor(None, _app.store.get_task, task_id)
        return {
            "status": "ok", "task": current,
            "message": "任务当前无待处理的通过决策（可能已提交或已推进），未重复执行",
        }
    task = claimed

    project = await loop.run_in_executor(None, _app.store.get_project, task["project_id"])
    merged_diff = task.get("merged_diff") or ""
    apply_diff_flag = req.apply_diff if req else False
    cfg = _app.get_config()
    should_apply = apply_diff_flag or (
        not cfg.sandbox.sandbox_first and bool(merged_diff.strip())
    )
    apply_result: dict[str, Any] | None = None

    if should_apply and merged_diff.strip() and project and project.get("path"):
        from swarm.infra.redis_client import ModuleLock
        from swarm.project.diff_apply import apply_git_diff

        # E9（阶段5，登记册 §六）：apply 直写项目工作树此前【不持模块锁】——同项目另一
        # 任务的 runner 正持锁写树（merge/L2 reset），两写并发=树污染。与 runner 同一
        # 把锁（project:default）；拿不到=有任务在写，回滚认领并 409（稍后重试，幂等）。
        _apply_lock = ModuleLock(task["project_id"], "default")
        _got_lock = await loop.run_in_executor(None, _apply_lock.acquire)
        if not _got_lock:
            try:
                await loop.run_in_executor(
                    None, lambda: _app.store.update_task(task_id, status=orig_status),
                )
            except Exception:  # noqa: BLE001
                _app.logger.warning("approve 锁竞争回滚认领状态失败 task=%s", task_id, exc_info=True)
            raise HTTPException(
                status_code=409,
                detail="同项目有任务正在写工作树（模块锁被占用），请稍后重试审批",
            )
        try:
            apply_result = await loop.run_in_executor(
                None,
                lambda: apply_git_diff(project["path"], merged_diff, check_only=False),
            )
        finally:
            await loop.run_in_executor(None, _apply_lock.release)
        if apply_result and not apply_result.get("ok"):
            # D17 治本：apply 失败【无论显式/隐式】一律阻断 accept 推进。旧逻辑只在
            # apply_diff_flag 为真时 422，隐式 apply（非 sandbox_first）失败被吞——
            # resume("accept") 照常推进 DONE 而工作区无变更（假交付，唯一线索是响应体
            # apply_diff.ok=false）。统一沿用显式路径既有的 422/回滚语义：
            # 回滚认领状态（恢复审核态），任务留在可重试/待人工状态，避免卡 ANALYZING 却无 resume。
            # status 认领 → 只需还原 status（human_decision 无关，留原值无害）；best-effort 不掩盖 422。
            try:
                await loop.run_in_executor(
                    None, lambda: _app.store.update_task(task_id, status=orig_status),
                )
            except Exception:  # noqa: BLE001
                _app.logger.warning("approve apply 失败后回滚认领状态失败 task=%s", task_id, exc_info=True)
            raise HTTPException(
                status_code=422,
                detail=apply_result.get("stderr") or apply_result.get("stdout") or "git apply 失败",
            )

    # ★对抗复核 3rd#1 治本★：KB 增量索引已移到 learn_success 的 commit 之后触发（读到已 apply
    # 的最终产出、且覆盖 auto_accept 路径）。此处【不再】在 apply 之前读磁盘触发——否则会用 L2
    # 回滚后的 HEAD 旧内容覆盖知识库（知识库随使用系统性变旧）。resume→learn_success 收口。

    from swarm.brain.runner import register_task_queue, resume_task_background

    register_task_queue(task_id)
    resume_task_background(task_id, "accept", revert_status=orig_status)
    updated = task  # 认领后的行（human_decision=ACCEPT 已落库）
    out: dict[str, Any] = {"status": "ok", "task": updated, "message": "已提交接受，Brain 继续执行"}
    if apply_result:
        out["apply_diff"] = apply_result

    # 审批事件通知（task_approved），与任务"完成事件"正交：
    # 完成事件（task_completed/task_failed）由 brain/runner.py 的 _emit_task_notification 在
    # 任务生命周期 DONE/FAILED 时发出。两类事件语义不同，分别发送。
    # 统一走 store.create_notification（→ 应用内铃铛 + hook 自动推外部渠道），
    # 并保留旧单 webhook notify() 向后兼容（SWARM_NOTIFY_WEBHOOK_URL）。
    msg = f"任务 {task_id} 已审核通过，Brain 继续执行"
    await loop.run_in_executor(None, lambda: _app.store.create_notification(
        "task_approved", task_id=task_id, project_id=updated.get("project_id") if isinstance(updated, dict) else None,
        title="任务已通过", message=msg))
    from swarm.api.notify import notify
    await notify("task_approved", task_id, msg)

    return out


@router.post("/api/tasks/{task_id}/apply-diff", tags=["任务管理"])
async def apply_task_diff(task_id: str, request: Request, req: ApplyDiffRequest | None = None):
    """Phase 1 — 将 merged_diff 应用到项目 git 工作区（git apply）"""
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    # P0-SEC-02/C5：写盘端点须授权 + 成员资格；不存在与无权统一 404（防存在性枚举）。task:approve。
    _require_task_access(request, task, task_id, "task:approve")

    project = await loop.run_in_executor(None, _app.store.get_project, task["project_id"])
    if not project or not project.get("path"):
        raise HTTPException(status_code=400, detail="项目路径不可用")

    diff = (req.diff if req and req.diff else None) or task.get("merged_diff") or ""
    if not diff.strip():
        raise HTTPException(status_code=400, detail="任务无 merged_diff 可应用")

    conflicts = task.get("merge_conflicts") or []
    if conflicts and not (req and req.check_only):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "存在 merge 冲突，请先解决冲突后再 apply",
                "merge_conflicts": conflicts,
            },
        )
    if conflicts and req and req.check_only:
        return {
            "status": "conflict",
            "ok": False,
            "message": "merge 冲突 — git apply 已阻断",
            "merge_conflicts": conflicts,
        }

    check_only = req.check_only if req else False
    from swarm.project.diff_apply import apply_git_diff

    result = await loop.run_in_executor(
        None,
        lambda: apply_git_diff(project["path"], diff, check_only=check_only),
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=422,
            detail=result.get("stderr") or result.get("stdout") or "git apply 失败",
        )
    return {"status": "ok", **result}


# ─── 11. POST /api/tasks/{task_id}/revise — 审核修订 ─
@router.post("/api/tasks/{task_id}/revise", tags=["任务管理"])
async def revise_task(task_id: str, request: Request, req: TaskReviseRequest):
    """审核修订 — resume Brain (revise + feedback)"""
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    _require_task_access(request, task, task_id, "task:approve")  # 审核修订=审批决策

    # P1-A 原子认领：仅审核态放行，双击第二次 → None → 幂等不重复 resume。
    from swarm.task_states import PLAN_RESULT_REVIEW_STATES

    orig_status = task.get("status")
    claimed = await loop.run_in_executor(
        None,
        lambda: _app.store.claim_human_gate(
            task_id, PLAN_RESULT_REVIEW_STATES, "IN_REVISION", human_decision="REVISE"
        ),
    )
    if claimed is None:
        current = await loop.run_in_executor(None, _app.store.get_task, task_id)
        return {
            "status": "ok", "task": current,
            "message": "任务当前无待处理的修订决策（可能已提交或已推进），未重复执行",
        }

    from swarm.brain.runner import register_task_queue, resume_task_background

    register_task_queue(task_id)
    resume_task_background(task_id, "revise", req.feedback, revert_status=orig_status)
    updated = claimed
    # 审批事件通知（task_revised），与完成事件正交（完成事件见 runner._emit_task_notification）。
    # 统一走 create_notification（铃铛 + 多渠道），保留旧 notify() 兼容。
    msg = f"任务 {task_id} 已提交修订，Brain 重新调度"
    await loop.run_in_executor(None, lambda: _app.store.create_notification(
        "task_revised", task_id=task_id, project_id=updated.get("project_id") if isinstance(updated, dict) else None,
        title="任务已修订", message=msg))
    from swarm.api.notify import notify
    await notify("task_revised", task_id, msg)
    return {"status": "ok", "task": updated, "message": "已提交修订，Brain 重新调度"}


# ─── 12. POST /api/tasks/{task_id}/reject — 审核拒绝 ─
@router.post("/api/tasks/{task_id}/reject", tags=["任务管理"])
async def reject_task(task_id: str, request: Request):
    """审核拒绝 — resume Brain (reject)"""
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    _require_task_access(request, task, task_id, "task:approve")  # 审核拒绝=审批决策

    # P1-A 原子认领：仅审核态放行，双击第二次 → None → 幂等不重复 resume。
    from swarm.task_states import PLAN_RESULT_REVIEW_STATES

    orig_status = task.get("status")
    claimed = await loop.run_in_executor(
        None,
        lambda: _app.store.claim_human_gate(
            task_id, PLAN_RESULT_REVIEW_STATES, "ANALYZING", human_decision="REJECT"
        ),
    )
    if claimed is None:
        current = await loop.run_in_executor(None, _app.store.get_task, task_id)
        return {
            "status": "ok", "task": current,
            "message": "任务当前无待处理的拒绝决策（可能已提交或已推进），未重复执行",
        }

    from swarm.brain.runner import register_task_queue, resume_task_background

    register_task_queue(task_id)
    resume_task_background(task_id, "reject", revert_status=orig_status)
    updated = claimed
    # 审批事件通知（task_rejected），与完成事件正交（完成事件见 runner._emit_task_notification）。
    # 统一走 create_notification（铃铛 + 多渠道），保留旧 notify() 兼容。
    msg = f"任务 {task_id} 已拒绝，Brain 进入学习失败流程"
    await loop.run_in_executor(None, lambda: _app.store.create_notification(
        "task_rejected", task_id=task_id, project_id=updated.get("project_id") if isinstance(updated, dict) else None,
        title="任务已拒绝", message=msg))
    from swarm.api.notify import notify
    await notify("task_rejected", task_id, msg)
    return {"status": "ok", "task": updated, "message": "已拒绝，Brain 进入学习失败流程"}


@router.get("/api/tasks/{task_id}/planning", tags=["任务管理"])
async def get_task_planning(task_id: str, request: Request):
    """读取任务的规划过程产物（Q4 可追溯）：澄清问答 / 技术方案 / 评审决策 / 澄清后定级。

    任务详情页"规划过程"区用。无规划产物（微任务/轻量路径）时返回空。
    """
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    _require_task_access(request, task, task_id, "task:read")
    artifacts = await loop.run_in_executor(
        None, lambda: _app.store.get_planning_artifacts(task_id)
    )
    return {"task_id": task_id, "planning": artifacts or {}}


# ─── GET /api/tasks/{task_id}/pending — 当前挂起的人机交互(刷新后恢复用) ─
@router.get("/api/tasks/{task_id}/pending", tags=["任务管理"])
async def get_task_pending(task_id: str, request: Request):
    """返回任务当前【挂起的 interrupt】(澄清/虚假前提/技术方案评审)，供前端刷新后恢复交互卡片。

    治本：澄清卡片此前仅由瞬时 SSE 渲染，刷新页面即丢失、无法答复。前端选中任务时拉此端点
    重渲染待答问题。无挂起返回 pending=null。
    """
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    _require_task_access(request, task, task_id, "task:read")
    from swarm.brain.runner import get_pending_interrupt

    pending = await get_pending_interrupt(task_id)
    return {"task_id": task_id, "pending": pending}


@router.post("/api/tasks/{task_id}/clarify", tags=["任务管理"])
async def submit_clarify(task_id: str, request: Request):
    """提交需求澄清答复（恢复被 clarify interrupt 暂停的任务）。

    Body: {"answers": {"0": "...", "1": "..."}} 逐条回答，或 {"action": "skip"} 整体跳过。
    """
    # P0-SEC-03：澄清答复会推进任务规划，须 task:approve（审批/规划决策）+ 成员资格。
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    _require_task_access(request, task, task_id, "task:approve")
    body = await request.json()
    if body.get("action") == "skip":
        payload: dict = {"action": "skip"}
    else:
        answers = body.get("answers")
        if not isinstance(answers, dict):
            raise HTTPException(status_code=400, detail="需要 answers 字典或 action=skip")
        payload = answers
    # P1-A 原子认领：仅 CLARIFYING 态放行，重复提交 → None → 幂等不重复 resume。
    orig_status = task.get("status")
    claimed = await loop.run_in_executor(
        None,
        lambda: _app.store.claim_human_gate(task_id, {"CLARIFYING"}, "ANALYZING"),
    )
    if claimed is None:
        return {"status": "ok", "message": "任务当前无待答复的澄清（可能已提交或已推进），未重复执行"}
    from swarm.brain.runner import resume_planning_background
    resume_planning_background(task_id, payload, revert_status=orig_status)
    return {"status": "ok", "message": "澄清已提交，规划继续"}


@router.post("/api/tasks/{task_id}/review-design", tags=["任务管理"])
async def submit_design_review(task_id: str, request: Request):
    """提交技术方案评审决策（恢复被 review_design interrupt 暂停的任务）。

    Body: {"decision": "approve"} 通过，或 {"decision": "reject", "feedback": "..."} 打回重做。
    """
    # P0-SEC-03：评审决策推进规划，须 task:approve（审批/规划决策）+ 成员资格。
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    _require_task_access(request, task, task_id, "task:approve")
    body = await request.json()
    decision = body.get("decision")
    if decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="decision 须为 approve 或 reject")
    payload = {"decision": decision, "feedback": body.get("feedback", "")}
    # P1-A 原子认领：仅 DESIGN_REVIEW 态放行，重复提交 → None → 幂等不重复 resume。
    orig_status = task.get("status")
    claimed = await loop.run_in_executor(
        None,
        lambda: _app.store.claim_human_gate(task_id, {"DESIGN_REVIEW"}, "ANALYZING"),
    )
    if claimed is None:
        return {"status": "ok", "message": "任务当前无待评审的方案（可能已提交或已推进），未重复执行"}
    from swarm.brain.runner import resume_planning_background
    resume_planning_background(task_id, payload, revert_status=orig_status)
    return {"status": "ok", "message": "方案评审已提交，规划继续"}
