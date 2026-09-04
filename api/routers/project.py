"""api/routers/project.py — 项目管理域路由 (列表/创建/详情/删除/预处理触发与进度)。

从 api/app.py 抽出, app.include_router 挂载。
mock 锚点(store/_validate_project)及 app 级 preprocess/logger 用 _app. 属性访问。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from swarm.api.rate_limit import rate_limit  # C7
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

import swarm.api.app as _app
from swarm.api._shared import _require_perm, _require_user
from swarm.infra.cancellation import (
    run_blocking_owned,
    run_db_blocking_owned,
)
# ★独立双复核 LOW 整改★ 模块级导入（infra/degrade 是叶子，只依赖 threading/collections，
# 无循环依赖）——原实现在 except 臂里做延迟 import 且不受 try 保护，import 若抛会让
# fail-closed 的鉴权函数变成 500。
from swarm.infra.degrade import record_degrade_safe as _record_degrade_safe

router = APIRouter()


async def _await_claim_spawn_owned(task: asyncio.Task, *, project_id: str) -> object:
    """取消时先等 claim→spawn 临界单元结束，再恢复调用方取消语义。"""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        try:
            task.result()
        except Exception:  # noqa: BLE001 — 取消语义优先，但收尾异常必须留痕
            _app.logger.exception("项目 %s 的 claim→spawn 拥有单元异常", project_id)
        raise


async def _claim_and_spawn_preprocess(
    project_id: str,
    project_path: str,
    *,
    stale_after_sec: int,
    operation_prefix: str,
):
    """先取得项目写锁再 claim，并把锁所有权原子转交给后台预处理。"""
    from swarm.infra.redis_client import ModuleLock
    from swarm.project.preprocess import (
        PreprocessLockBusyError,
        PreprocessOwnershipLostError,
        PreprocessStartOutcome,
        preprocess_project,
    )
    from swarm.project.store import ProjectDeletionInProgressError

    lock = ModuleLock(project_id, "default")
    acquired = False
    transferred = False
    try:
        acquired = bool(await run_blocking_owned(
            lock.acquire,
            operation=f"{operation_prefix}获取项目写锁 project={project_id}",
            cancel_result_cleanup=lambda result: lock.release() if result else None,
        ))
        if not acquired:
            return PreprocessStartOutcome.LOCK_BUSY

        claimed = await run_db_blocking_owned(
            lambda: _app.store.claim_preprocess_slot(
                project_id, stale_after_sec=stale_after_sec,
            ),
            operation=f"{operation_prefix}认领",
        )
        if not claimed:
            return PreprocessStartOutcome.ALREADY_CLAIMED

        entered = asyncio.get_running_loop().create_future()

        async def _run_owned_preprocess() -> None:
            # 必须是 coroutine 首次 poll 的第一条语句；一旦置位，下面的 await 会同步
            # 驱动 preprocess_project 建立其 lock-release finally 后才首次挂起。
            entered.set_result(None)
            try:
                await preprocess_project(project_id, project_path, owned_lock=lock)
            except asyncio.CancelledError:
                # preprocess_project 在仍持 ModuleLock 时已完成双账结算并排空所有 writer。
                raise
            except (
                PreprocessLockBusyError,
                PreprocessOwnershipLostError,
                ProjectDeletionInProgressError,
            ) as exc:
                # 合法争用/失主不能由陈旧执行者覆写 ERROR；新 owner 或删除围栏负责收口。
                _app.logger.warning(
                    "%s未取得/失去项目写盘所有权 project=%s: %s",
                    operation_prefix,
                    project_id,
                    exc,
                )
            except Exception as exc:  # noqa: BLE001 — 入口失败需释放 PREPROCESSING 守卫
                _app.logger.exception("Preprocessing failed for project %s", project_id)

        child_coro = _run_owned_preprocess()
        try:
            child = _app._spawn_bg(child_coro)
        except BaseException:
            child_coro.close()
            raise

        # spawn 返回不等于所有权转移：Task 可能在第一次 poll 前被 shutdown/cancel。
        # 启动者等 entered 或 child 先终止；只有 entered 才能把 lock 交给 child。
        await asyncio.wait({entered, child}, return_when=asyncio.FIRST_COMPLETED)
        if not entered.done():
            try:
                child.result()
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — 下方统一机读补偿并抛启动错误
                _app.logger.exception(
                    "%s child 在 ownership handshake 前失败 project=%s",
                    operation_prefix,
                    project_id,
                )
            await run_db_blocking_owned(
                _app.store.settle_cancelled_preprocess,
                project_id,
                operation=f"{operation_prefix}未交接 claim 补偿",
            )
            raise RuntimeError(
                f"preprocess ownership handshake failed for project {project_id}"
            )
        transferred = True
        return PreprocessStartOutcome.STARTED
    finally:
        if acquired and not transferred:
            await run_blocking_owned(
                lock.release,
                operation=f"{operation_prefix}未转交时释放项目写锁 project={project_id}",
            )


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
    """是否由管理员显式开放 workspace 外的宿主路径。默认关闭。"""
    import os
    return os.environ.get("SWARM_ALLOW_EXTERNAL_PROJECT_PATH", "").strip().lower() \
        in ("1", "true", "yes", "on")


def _enforce_project_path_containment(
    resolved_path: str,
    workspace_root: str,
    allow_external: bool,
    *,
    user,
) -> None:
    """授权项目根：workspace 内按 project:create，外部路径仅全局 admin 可显式开放。

    外部项目路径最终会进入索引器和 Worker，等价于授予宿主目录读写能力。配置开关只表达
    部署者是否开放此能力，不向普通角色授予能力；否则 developer 可自行成为任意宿主目录
    的 owner。调用方传入的路径必须已经 realpath 归一，函数仍再次归一以免未来调用点漏做。
    """
    if not resolved_path:
        return
    from swarm.auth.rbac import Role

    norm = os.path.realpath(os.path.abspath(resolved_path))
    root = os.path.realpath(os.path.abspath(workspace_root))
    try:
        inside_workspace = os.path.commonpath((norm, root)) == root
    except ValueError:
        # Windows 异盘等无法比较的路径必然不在同一 workspace。
        inside_workspace = False
    if inside_workspace:
        return
    if getattr(user, "global_role", None) == Role.ADMIN.value and allow_external:
        return
    raise HTTPException(
        status_code=403,
        detail=("无权注册 workspace 外的宿主路径；"
                "仅全局管理员可在显式开启 SWARM_ALLOW_EXTERNAL_PROJECT_PATH 后执行此操作"),
    )


def _canonicalize_project_path(raw: str | None) -> str:
    """I-SEC-1（round38c 主题I·外部深审 CRITICAL）：项目路径 alias 归一。

    项目唯一性靠 PG ON CONFLICT (path) 的【字符串】比较——尾斜杠/./​段/symlink 等
    alias 形态可把同一物理目录注册成多个项目，绕过 D16 冲突检测与成员授权模型
    （他人项目目录经 alias 注册为"自己的"项目=多租户越权读写）。入口统一 realpath
    归一，落库即规范物理路径。历史非规范存量行不迁移（诚实边界，登记册记录）。"""
    from swarm.project.store import normalize_project_path

    return normalize_project_path(raw)


# H-5（round38c 主题I·外部深审 HIGH）：系统敏感目录黑名单——【必须含 realpath 后的形态】。
# macOS 上 /etc→/private/etc、/var→/private/var、/tmp→/private/tmp（firmlink/symlink），
# 而 _reject_sensitive 先 realpath 再比对；旧黑名单只有字面 "/etc" → norm("/private/etc") 既
# 不 ==「/etc」也不 startswith「/etc/」→ 绕过（可把项目根指向 /private/etc 等宿主敏感目录）。
# 治：黑名单条目本身也过 realpath 并入集（Linux 上 realpath(/etc)==/etc 无变化，平台通用）。
_SENSITIVE_DIRS_RAW = ("/etc", "/usr", "/bin", "/sbin", "/sys", "/proc", "/dev",
                       "/boot", "/var/run", "/lib", "/lib64", "/root")


def _sensitive_dir_set() -> tuple[str, ...]:
    """字面 + realpath 归一形态并集（去重、稳定序）。"""
    out: list[str] = []
    for s in _SENSITIVE_DIRS_RAW:
        for form in (s, os.path.realpath(s)):
            if form and form not in out:
                out.append(form)
    return tuple(out)


def _path_is_sensitive(p: str) -> bool:
    """归一后的路径 p 是否落在（或等于）任一系统敏感目录。p 应已 realpath。"""
    if not p:
        return False
    norm = os.path.realpath(os.path.abspath(p))
    for s in _sensitive_dir_set():
        if norm == s or norm.startswith(s + "/"):
            return True
    return False


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
    except Exception as exc:  # noqa: BLE001 — DB 抖动等：默认拒绝
        # ★32 号文 A6-L1 同族第四处★（findings 点名三处在 routers/sandbox.py，本处是它
        # 自己列的"同族"）。极性正确＝fail-closed 拒绝复用，缺的是可观测性：DB 抖动时
        # 合法项目成员的"复用既有项目"请求被拒，而服务端零线索。
        # 与"合法的非成员"（走 try 内 `is not None` 返 False，不经这里）刻意分开。
        # ★独立双复核 LOW 整改★：原先此处 `from swarm.api.routers.sandbox import _degrade`
        # ——跨模块导入兄弟路由的私有符号，且那次延迟 import **不在任何 try 内** ⇒ 它若抛，
        # 异常会逃出本鉴权函数变成 500，而本函数的契约是 fail-closed 返 False。
        # 改为模块级导入 infra 叶子模块的 `record_degrade_safe`（见文件头 import 段）。
        _record_degrade_safe("api.project.reuse_member_role_lookup_failed")
        _app.logger.warning(
            "[Project] 复用鉴权的成员角色查询失败 project=%s user=%s: %s ——按拒绝处理"
            "（fail-closed，非权限配置问题）", existing_id, getattr(user, "id", "?"), exc,
        )
        return False


@router.post("/api/projects", tags=["项目管理"])
async def create_project(req: ProjectCreateRequest, request: Request):
    """创建项目并自动启动预处理

    项目状态从 EMPTY → PREPROCESSING → READY
    """
    from swarm.auth.rbac import Role

    user = _require_perm(request, "project:create")
    project_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    if req.name.strip() in (".", ".."):
        raise HTTPException(status_code=400, detail="项目名称不能是 . 或 ..")

    # ── 路径解析 + 授权 ──
    # 项目根会被预处理读取并被 Worker 写入，属于宿主资源授权；必须先于目录创建、项目落库、
    # 成员写入与预处理。PROJECT_ROOT 是源码/部署根，不是租户 workspace；授权边界必须使用
    # 可配置的 AppConfig.workspace_root。
    import re as _re
    from swarm.config.settings import get_config

    workspace_root = os.path.realpath(os.path.abspath(str(get_config().workspace_root)))
    resolved_path = _canonicalize_project_path(req.path)
    if not req.greenfield and not resolved_path:
        raise HTTPException(status_code=400, detail="既有项目必须提供 path（或设 greenfield=true 从零创建）")
    if req.greenfield and not resolved_path:
        safe = _re.sub(r"[^A-Za-z0-9_.-]+", "-", req.name).strip("-") or project_id[:8]
        # 拼接后仍要再次规范化，避免未来名称清洗规则扩展时把路径段原样写入存储。
        resolved_path = _canonicalize_project_path(os.path.join(workspace_root, "workdir", safe))

    _enforce_project_path_containment(
        resolved_path,
        workspace_root,
        _env_allow_external_project_path(),
        user=user,
    )
    # M7：系统敏感目录黑名单是纵深防御；主授权是 role + workspace containment，不能靠
    # 扩大黑名单枚举宿主机上的所有敏感位置。先做外部路径授权，避免向未授权调用者泄露
    # 目标目录属于哪一类宿主资源。
    if _path_is_sensitive(resolved_path):
        raise HTTPException(
            status_code=400,
            detail=f"拒绝将项目根指向系统敏感目录: {os.path.realpath(os.path.abspath(resolved_path))}",
        )

    from swarm.project.store import (
        ProjectPathNamespaceError,
        normalize_project_path,
        validate_project_path_namespace,
    )
    try:
        validate_project_path_namespace(resolved_path, workspace_root)
    except ProjectPathNamespaceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    # 路由早拒：已有重叠必须在 greenfield mkdir / 项目写入 / 成员授权 / 预处理前裁决。
    # create_project 内会在 projects 表写锁事务下重做同一检查，负责封住并发 TOCTOU。
    try:
        overlap = await loop.run_in_executor(
            None, lambda: _app.store.find_project_path_overlap(resolved_path),
        )
    except Exception as exc:  # noqa: BLE001 — 无法确认隔离边界时 fail-closed
        _app.logger.error("create_project: 项目路径重叠检查失败", exc_info=True)
        raise HTTPException(status_code=500, detail="无法确认项目路径隔离边界") from exc
    if overlap:
        exact_match = normalize_project_path(overlap.get("path")) == resolved_path
        if exact_match and await loop.run_in_executor(
            None, lambda: _caller_may_reuse_existing_project(user, overlap.get("id") or ""),
        ):
            return {"status": "ok", "project": overlap, "existing": True}
        raise HTTPException(status_code=409, detail="该路径与已有项目目录重叠")

    # ★#29-8 M-1★ 项目数软限制接线（此前全机制——env 登记/机读键/WARNING——零生产
    # 调用=死账，运维设 SWARM_MAX_ACTIVE_PROJECTS 以为有保护实际第 N+1 个项目照进，
    # PG/Qdrant/沙箱预算被悄悄超订）。超限拒收新项目；PG 不可用（active=-1）时
    # 不阻断（软限制语义，可用性优先）但 WARNING 留痕。
    from swarm.infra.redis_client import check_project_limit
    _pl = await loop.run_in_executor(None, check_project_limit)
    if _pl.get("warn"):
        raise HTTPException(
            status_code=409,
            detail=str(_pl.get("message") or "活跃项目数已达软限制"),
        )
    if int(_pl.get("active", -1)) < 0:
        _app.logger.warning(
            "[create_project] 项目数软限制检查不可用 → 放行（软闸 fail-open，留痕）: %s",
            _pl.get("message"))

    # ── 路径存在性 + greenfield（从零创建）支持 ──
    # 既有项目：path 必须指向存在的目录。
    # greenfield 的必要 mkdir 必须由 store 在表锁事务内做，不能在排他路径预留前留下
    # TOCTOU 目录；事务失败时 store 只清理由本请求新建且仍为空的目录。
    if not req.greenfield:
        if not os.path.isdir(resolved_path):
            raise HTTPException(
                status_code=400,
                detail=f"路径不存在: {resolved_path}。如需从零创建空项目，请设 greenfield=true",
            )

    # 创建项目记录
    from swarm.project.store import ProjectPathConflictError, ProjectPathLockTimeoutError

    try:
        project = await run_db_blocking_owned(
            lambda: _app.store.create_project(
                project_id=project_id,
                name=req.name,
                path=resolved_path,
                description=req.description,
                owner_user_id=(None if user.global_role == Role.ADMIN.value else user.id),
                owner_role=(None if user.global_role == Role.ADMIN.value else Role.OWNER.value),
                create_directory=req.greenfield,
            ),
            operation="项目目录、记录与创建者授权原子创建",
        )
    except ProjectPathLockTimeoutError as exc:
        raise HTTPException(
            status_code=503,
            detail="项目路径正在被其他写事务占用，请稍后重试",
            headers={"Retry-After": str(max(1, (exc.timeout_ms + 999) // 1000))},
        ) from None
    except ProjectPathConflictError as conflict:
        # D16：path 已被既存项目占用 → 绝不改写。成员（或 admin）按幂等语义返回既有
        # 项目（不触发预处理、不动成员表——重复添加同路径的合法场景）；其他人 409 拒绝，
        # 响应不携带既存项目任何字段（防跨用户信息泄露）。
        existing = conflict.existing or {}
        allowed = await loop.run_in_executor(
            None, lambda: _caller_may_reuse_existing_project(user, existing.get("id") or ""),
        )
        if not conflict.exact_match or not allowed or not existing.get("id"):
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

    # 后台启动预处理（不阻塞响应）。D16③：一律用 store 返回的真实 id/path。
    real_project_id = project["id"]
    real_project_path = project.get("path") or resolved_path

    # D20：claim 与 spawn 是一个不可拆的拥有单元。仅把同步 CAS 改成 owned 仍不够：
    # 请求在 CAS 提交后收到取消，会在赋值前抛出并留下无人执行的 PREPROCESSING。
    # 因此 shield 整个 claim→spawn，取消时等它完成后再传播。
    async def _claim_and_spawn():
        from swarm.project.preprocess import (
            PreprocessStartOutcome,
            _preprocess_timeout_sec as _pp_timeout_sec,
        )

        try:
            outcome = await _claim_and_spawn_preprocess(
                real_project_id,
                real_project_path,
                stale_after_sec=_pp_timeout_sec() + 600,
                operation_prefix="自动项目预处理",
            )
        except Exception:  # noqa: BLE001 — 守卫故障不阻断创建；可手动重触发
            _app.logger.exception(
                "claim_preprocess_slot failed for %s（跳过自动预处理）", real_project_id,
            )
            return
        if outcome is not PreprocessStartOutcome.STARTED:
            _app.logger.info(
                "项目 %s 预处理未启动（%s），跳过自动 spawn",
                real_project_id,
                outcome.value,
            )

    claim_spawn_task = asyncio.create_task(_claim_and_spawn())
    await _await_claim_spawn_owned(claim_spawn_task, project_id=real_project_id)

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

    # 级联终止运行中任务（在删 DB 记录之前，确保 cancel_task 还能查到 task）。
    # 活跃执行归本地 leader 所有；follower 无跨副本 cancel 信号，绝不能删掉远端 runner 的 DB 行。
    tasks_before = await loop.run_in_executor(None, _app.store.list_tasks, project_id)
    from swarm.brain.runner import _ACTIVE_DB_STATUSES

    # 项目 hard delete 本身也是执行所有权操作：即使初始列表为空，也必须在落
    # DELETING 前确认本副本是 leader，避免 follower 擅自冻结可用项目。
    await _app.require_local_execution_leader()

    # leader 门在任何持久写之前：follower 观察到远端 active 时不能把项目擅自冻成
    # DELETING。随后落 admission fence，再复读一次覆盖 list→claim 之间的新 active。
    deleting = await run_db_blocking_owned(
        _app.store.claim_project_deletion,
        project_id,
        operation=f"认领项目删除围栏 project={project_id}",
    )
    if not deleting:
        raise HTTPException(status_code=409, detail="项目删除认领失败，请稍后重试")
    tasks_after_fence = await loop.run_in_executor(None, _app.store.list_tasks, project_id)
    if any(t.get("status") in _ACTIVE_DB_STATUSES for t in tasks_after_fence):
        await _app.require_local_execution_leader()
    try:
        from swarm.brain.runner import cancel_project_tasks
        cancelled = await cancel_project_tasks(project_id)
        if cancelled:
            _app.logger.info("删除项目 %s 前级联取消了 %d 个运行中任务", project_id, cancelled)
    except Exception as exc:
        _app.logger.exception("删除项目 %s 前级联取消任务失败，拒绝继续删除", project_id)
        raise HTTPException(status_code=409, detail="项目活跃任务取消失败，请稍后重试") from exc

    tasks_after = await loop.run_in_executor(None, _app.store.list_tasks, project_id)
    if any(t.get("status") in _ACTIVE_DB_STATUSES for t in tasks_after):
        raise HTTPException(status_code=409, detail="项目仍有未安全结算的活跃任务，请稍后重试")

    # 与 runner、manual apply、preprocess 共用项目宽 ModuleLock。拿不到说明仍有真实
    # writer，保留 DELETING 围栏并 409；拿到后在锁内复读 fence+active，再 hard delete。
    from swarm.infra.cancellation import run_blocking_owned
    from swarm.infra.redis_client import ModuleLock

    delete_lock = ModuleLock(project_id, "default")
    acquired = await run_blocking_owned(
        delete_lock.acquire,
        operation=f"项目删除获取写盘静默屏障 project={project_id}",
        cancel_result_cleanup=lambda result: delete_lock.release() if result else None,
    )
    if not acquired:
        raise HTTPException(status_code=409, detail="项目仍有预处理或写盘任务，请稍后重试")
    try:
        fenced_project = await loop.run_in_executor(None, _app.store.get_project, project_id)
        locked_tasks = await loop.run_in_executor(None, _app.store.list_tasks, project_id)
        if not fenced_project or fenced_project.get("status") != "DELETING":
            raise HTTPException(status_code=409, detail="项目删除围栏已变化，拒绝陈旧删除")
        if any(t.get("status") in _ACTIVE_DB_STATUSES for t in locked_tasks):
            raise HTTPException(status_code=409, detail="锁内仍有活跃任务，拒绝删除")
        deleted = await run_db_blocking_owned(
            lambda: _app.store.delete_project(project_id, require_deleting=True),
            operation=f"删除已围栏项目 project={project_id}",
        )
    finally:
        await run_blocking_owned(
            delete_lock.release,
            operation=f"项目删除释放写盘静默屏障 project={project_id}",
        )
    if not deleted:
        raise HTTPException(status_code=409, detail="项目删除围栏已变化，拒绝陈旧删除")

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
    async def _claim_and_spawn():
        return await _claim_and_spawn_preprocess(
            project_id,
            project_path,
            stale_after_sec=stale_after,
            operation_prefix="手动项目预处理",
        )

    try:
        claim_spawn_task = asyncio.create_task(_claim_and_spawn())
        outcome = await _await_claim_spawn_owned(claim_spawn_task, project_id=project_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _app.logger.exception("Failed to claim preprocess slot for %s", project_id)
        raise HTTPException(status_code=500, detail="启动预处理失败，请稍后重试") from e
    from swarm.project.preprocess import PreprocessStartOutcome
    if outcome is PreprocessStartOutcome.LOCK_BUSY:
        raise HTTPException(
            status_code=409,
            detail="项目正在被其他写盘任务占用，请稍后重试",
        )
    if outcome is not PreprocessStartOutcome.STARTED:
        raise HTTPException(
            status_code=409,
            detail="该项目已在预处理中，请等待完成后再触发",
        )

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
                if not await asyncio.to_thread(
                    _stream_reauthorized,
                    request,
                    {"project_id": project_id},
                    "project:read",
                ):
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
