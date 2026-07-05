"""api/routers/sandbox.py — 沙箱域路由 (状态/创建/销毁/文件列表/文件内容/日志)。

从 api/app.py 抽出, app.include_router 挂载。
app 级 helper(_get_sandbox_manager/_fetch_sandbox_list_from_server/logger 等)
用 _app. 属性访问(跨域共享, 保持单一定义)。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import swarm.api.app as _app

router = APIRouter()


def _sandbox_owner_info(manager, sandbox_id: str) -> tuple[str | None, str | None]:
    """取沙箱归属 (project_id, task_id)（A2）。优先本进程 meta，回退服务端 metadata 标签。"""
    meta = manager.get_sandbox_meta(sandbox_id) or {}
    pid = meta.get("project_id")
    tid = meta.get("task_id")
    if pid or tid:
        return pid, tid
    # 回退：服务端 metadata 的 swarm_project / swarm_task 标签（批3 create 打的）
    try:
        for sb in _app._fetch_sandbox_list_from_server():
            if sb.get("id") == sandbox_id:
                m = sb.get("metadata") or {}
                return m.get("swarm_project"), m.get("swarm_task")
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _task_creator(task_id: str | None) -> str | None:
    """查任务创建者 user_id（三级权限第三级依据）。失败返回 None。"""
    if not task_id:
        return None
    try:
        from swarm.project import store
        task = store.get_task(task_id)
        return (task or {}).get("created_by_user_id")
    except Exception:  # noqa: BLE001
        return None


def _can_see_sandbox(user, project_id: str | None, task_creator: str | None) -> bool:
    """A2 三级可见性判定（核心）：

    - 系统管理员（global admin）：所有沙箱。
    - 项目管理员（该项目 member_role=owner，或对项目有 project:write）：项目内所有沙箱。
    - 项目成员（developer/viewer）：仅自己创建任务的沙箱（task.created_by_user_id == 自己）。
    - 无项目归属沙箱：仅 admin（在调用处单独处理）。
    """
    from swarm.auth.rbac import Role
    from swarm.auth.store import get_project_member_role

    if user.global_role == Role.ADMIN.value:
        return True
    if not project_id:
        return False  # 无归属仅 admin
    # 项目角色
    try:
        member_role = get_project_member_role(project_id, user.id)
    except Exception:  # noqa: BLE001
        member_role = None
    if member_role is None:
        return False  # 非该项目成员
    # 项目管理员（owner）→ 项目内全部
    if member_role == Role.OWNER.value:
        return True
    # 项目成员 → 仅自建任务的沙箱
    return bool(task_creator) and task_creator == user.id


def _require_sandbox_access(request: Request, sandbox_id: str, permission: str = "task:read"):
    """A2 三级可见性 enforce：校验当前用户能否操作某沙箱。

    admin → 全部；项目 owner → 项目内全部；项目成员 → 仅自建任务沙箱；无归属 → 仅 admin。
    RBAC-off：_require_user 返回 anonymous admin，自然放行（开箱即用）。
    """
    from fastapi import HTTPException

    from swarm.api._shared import _require_user

    user = _require_user(request)
    manager = _app._get_sandbox_manager()
    pid, tid = _sandbox_owner_info(manager, sandbox_id)
    creator = _task_creator(tid)
    if not _can_see_sandbox(user, pid, creator):
        raise HTTPException(status_code=403, detail="无权操作该沙箱（仅管理员/项目管理员/任务创建者可访问）")
    return user


def _require_admin(request: Request):
    """A2 批1：系统级操作仅 admin（RBAC-off 时 anonymous admin 放行）。"""
    from fastapi import HTTPException

    from swarm.api._shared import _require_user
    from swarm.auth.rbac import Role

    user = _require_user(request)
    if user.global_role != Role.ADMIN.value:
        raise HTTPException(status_code=403, detail="系统级操作仅管理员可执行")
    return user


class SandboxCreateRequest(BaseModel):
    """创建沙箱请求"""
    template_id: str | None = Field(
        default=None,
        description="沙箱模板 ID，默认使用配置中的 default_template",
    )
    timeout: int = Field(default=60, description="创建超时（秒）")
    project_id: str | None = Field(default=None, description="关联项目 ID")


@router.get("/api/sandbox/status", tags=["沙箱"])
async def sandbox_status(request: Request, project_id: str | None = None):
    """活跃沙箱列表（A2 三级可见性）：

    - 系统管理员：所有沙箱
    - 项目管理员（owner）：其项目内所有沙箱
    - 项目成员：仅自己创建任务的沙箱
    """
    from swarm.api._shared import _require_user
    from swarm.auth.rbac import Role
    user = _require_user(request)
    is_admin = user.global_role == Role.ADMIN.value

    loop = asyncio.get_running_loop()
    sandboxes = await loop.run_in_executor(None, _app._fetch_sandbox_list_from_server)
    manager = _app._get_sandbox_manager()
    seen = {sb.get("id") for sb in sandboxes if sb.get("id")}

    if project_id:
        allowed = manager.sandboxes_for_project(project_id)
        sandboxes = [sb for sb in sandboxes if sb.get("id") in allowed]
        seen = {sb.get("id") for sb in sandboxes if sb.get("id")}

    for sid in manager.active_ids:
        if project_id and sid not in manager.sandboxes_for_project(project_id):
            continue
        if sid not in seen:
            meta = manager.get_sandbox_meta(sid) or {}
            sandboxes.append({
                "id": sid,
                "status": "running",
                "started_at": "-",
                "template_id": "-",
                "cpu_count": None,
                "memory_mb": None,
                "source": "local",
                "project_id": meta.get("project_id"),
                "task_id": meta.get("task_id"),
            })
            seen.add(sid)

    for sb in sandboxes:
        sid = sb.get("id")
        if sid:
            meta = manager.get_sandbox_meta(sid)
            if meta:
                sb["project_id"] = meta.get("project_id")
                sb["task_id"] = meta.get("task_id")
                sb["source"] = meta.get("source")

    # A2 三级可见性过滤（admin 不过滤）
    if not is_admin:
        def _sb_pid(sb: dict) -> str | None:
            return sb.get("project_id") or (sb.get("metadata") or {}).get("swarm_project")

        def _sb_tid(sb: dict) -> str | None:
            return sb.get("task_id") or (sb.get("metadata") or {}).get("swarm_task")

        # 预取用户在各项目的角色（避免每沙箱重复查）
        from swarm.auth.store import get_project_member_role
        role_cache: dict[str, str | None] = {}

        def _visible(sb: dict) -> bool:
            pid = _sb_pid(sb)
            if not pid:
                return False
            if pid not in role_cache:
                try:
                    role_cache[pid] = get_project_member_role(pid, user.id)
                except Exception:  # noqa: BLE001
                    role_cache[pid] = None
            role = role_cache[pid]
            if role is None:
                return False
            if role == Role.OWNER.value:
                return True  # 项目管理员看项目内全部
            # 项目成员：仅自建任务沙箱
            creator = _task_creator(_sb_tid(sb))
            return bool(creator) and creator == user.id

        sandboxes = [sb for sb in sandboxes if _visible(sb)]

    return {
        "active_count": len(sandboxes),
        "sandboxes": sandboxes,
        "project_id": project_id,
        "viewer_role": "admin" if is_admin else "member",
        "config": {
            "api_url": _app.get_config().sandbox.api_url,
            "proxy_base": _app.get_config().sandbox.proxy_base,
            "default_template": _app.get_config().sandbox.default_template,
            "use_for_worker": _app.get_config().sandbox.use_for_worker,
        },
    }


# ─── 8. POST /api/sandbox/create ───────────────────
@router.post("/api/sandbox/create", tags=["沙箱"])
async def create_sandbox(req: SandboxCreateRequest, request: Request):
    """创建新沙箱（A2 批1：需对目标项目有 task:create 权限；无项目时仅 admin）"""
    from swarm.api._shared import _require_perm
    if req.project_id:
        _require_perm(request, "task:create", req.project_id)
    else:
        _require_admin(request)
    manager = _app._get_sandbox_manager()
    try:
        loop = asyncio.get_running_loop()
        template = req.template_id or manager.config.default_template
        sandbox = await loop.run_in_executor(
            None,
            lambda: manager.create(
                template_id=template,
                timeout=req.timeout,
                project_id=req.project_id,
                source="manual",
            ),
        )
        return {
            "status": "ok",
            "sandbox_id": sandbox.sandbox_id,
        }
    except Exception as e:
        _app.logger.error("Failed to create sandbox: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="创建沙箱失败") from e


# ─── 9b. POST /api/sandbox/cleanup — 批量清理（释放泄漏资源）─
@router.post("/api/sandbox/cleanup", tags=["沙箱"])
async def cleanup_sandboxes(request: Request, task_id: str | None = None, server: bool = False, orphans_only: bool = False):
    """批量销毁沙箱，释放 CubeSandbox/小模型资源。

    A2 批1：批量清理是系统级运维操作（可能影响全局/他人沙箱），仅 admin。
    按单任务释放请用 DELETE /api/sandbox/{sid}（项目级鉴权）。

    - task_id：只销毁该任务关联的沙箱（kill_by_task）。
    - orphans_only=true：只销毁孤儿沙箱（服务端在跑、但无任何项目/任务关联）。
      **全局运维推荐**——不误伤正在被某项目/任务使用的沙箱。
    - server=true：以服务端权威列表为准，销毁所有服务端沙箱（含正在用的）。
    - 默认（无参数）：销毁本进程追踪的全部沙箱（仅本地 _instances）。

    并行 kill，避免逐个串行耗时（实测 95 个串行需 ~280s）。
    """
    _require_admin(request)
    manager = _app._get_sandbox_manager()
    loop = asyncio.get_running_loop()

    if task_id:
        killed = await loop.run_in_executor(None, manager.kill_by_task, task_id)
        return {"status": "ok", "scope": "task", "task_id": task_id, "killed": killed}

    if orphans_only:
        orphans = await loop.run_in_executor(None, _orphan_sandbox_ids, manager)
        sids = orphans
        scope = "orphans"
    elif server:
        server_list = await loop.run_in_executor(None, _app._fetch_sandbox_list_from_server)
        sids = list({sb.get("id") for sb in server_list if sb.get("id")} | set(manager._instances.keys()))
        scope = "server"
    else:
        sids = list(manager._instances.keys())
        scope = "all"

    if not sids:
        return {"status": "ok", "scope": scope, "killed": 0, "message": "无沙箱可清理"}

    results = await asyncio.gather(
        *[loop.run_in_executor(None, manager.kill, sid) for sid in sids],
        return_exceptions=True,
    )
    failed = sum(1 for r in results if isinstance(r, Exception))
    killed = len(sids) - failed
    _app.logger.info("[Sandbox] cleanup(scope=%s): 成功 %d, 失败 %d", scope, killed, failed)
    return {"status": "ok", "scope": scope, "killed": killed, "failed": failed}


# ─── 9c. GET /api/sandbox/orphans — 孤儿沙箱统计（全局，不跟项目）─
@router.get("/api/sandbox/orphans", tags=["沙箱"])
async def list_orphan_sandboxes(request: Request):
    """列出孤儿沙箱：服务端在跑、但无任何项目/任务关联的沙箱。

    全局运维视图（不绑定项目），A2 批1：仅 admin。
    """
    _require_admin(request)
    manager = _app._get_sandbox_manager()
    loop = asyncio.get_running_loop()
    server_list = await loop.run_in_executor(None, _app._fetch_sandbox_list_from_server)
    orphan_ids = _orphan_sandbox_ids(manager, server_list)
    orphan_set = set(orphan_ids)
    orphans = [sb for sb in server_list if sb.get("id") in orphan_set]
    return {
        "status": "ok",
        "total": len(server_list),     # 服务端沙箱总数
        "orphan_count": len(orphan_ids),
        "orphans": orphans,
    }


# ─── 9d-toggle. POST /api/sandbox/pool/toggle — 开关热池（写 .env + 实时启停 reaper）─
@router.post("/api/sandbox/pool/toggle", tags=["沙箱"])
async def toggle_sandbox_pool(request: Request):
    """开关热沙箱池：写 SWARM_SANDBOX_POOL_ENABLED 到 .env + 同步 os.environ + reload，
    并实时启动/停止后台 reaper（无需重启 API 即可生效）。

    Body: {"enabled": true/false}
    """
    _require_admin(request)
    body = await request.json()
    enabled = bool(body.get("enabled"))

    # 1. 写 .env + 同步 os.environ（复用 config 路由的写入约定）
    import os
    from pathlib import Path
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    env_key = "SWARM_SANDBOX_POOL_ENABLED"
    val = "true" if enabled else "false"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    found = False
    out_lines = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s and s.partition("=")[0].strip().upper() == env_key:
            out_lines.append(f"{env_key}={val}")
            found = True
        else:
            out_lines.append(line)
    if not found:
        out_lines.append(f"{env_key}={val}")
    from swarm.config.settings import atomic_write_env
    atomic_write_env(env_path, "\n".join(out_lines) + "\n")  # A-P1-29：原子写
    os.environ[env_key] = val

    # 2. reload config 让 SandboxConfig.pool_enabled 反映新值
    from swarm.config.settings import reload_config as _reload_config
    _reload_config()

    # 3. 实时启停 reaper（运行时尽量生效，避免必须重启）
    from swarm.worker.sandbox_pool import get_sandbox_pool, pool_enabled
    reaper_action = "none"
    try:
        pool = get_sandbox_pool()
        if pool_enabled():
            pool.start_reaper()
            reaper_action = "started"
        else:
            pool.stop_reaper()
            reaper_action = "stopped"
    except Exception as exc:  # noqa: BLE001
        _app.logger.warning("toggle pool reaper 失败: %s", exc)
        reaper_action = f"error: {exc}"

    _app.logger.info("热沙箱池开关 → %s (reaper=%s)", val, reaper_action)
    return {
        "status": "ok",
        "pool_enabled": pool_enabled(),
        "reaper": reaper_action,
        "note": "已写入 .env 并实时启停 reaper。已在运行的 Worker executor 行为在下次任务派发时生效。",
    }


# ─── 9d. GET /api/sandbox/pool — 全局热池可观测面板 ─
@router.get("/api/sandbox/pool", tags=["沙箱"])
async def sandbox_pool_status(request: Request):
    """全局热沙箱池状态卡：池统计 + 孤儿沙箱 + 服务端总数，一处汇总。

    供 webui 全局池面板展示（A2 批1：全局运维视图，仅 admin）。
    """
    _require_admin(request)
    from swarm.worker.sandbox_pool import get_sandbox_pool, pool_enabled

    manager = _app._get_sandbox_manager()
    loop = asyncio.get_running_loop()
    server_list = await loop.run_in_executor(None, _app._fetch_sandbox_list_from_server)
    orphan_ids = _orphan_sandbox_ids(manager, server_list)

    enabled = pool_enabled()
    pool_stats: dict = {}
    if enabled:
        try:
            pool_stats = get_sandbox_pool().stats()
        except Exception as exc:  # noqa: BLE001
            _app.logger.warning("获取池 stats 失败: %s", exc)
            pool_stats = {"error": str(exc)}

    return {
        "status": "ok",
        "pool_enabled": enabled,
        "pool": pool_stats,
        "server_total": len(server_list),
        "orphan_count": len(orphan_ids),
        "config": {
            "templates": {
                "python": _app.get_config().sandbox.template_python,
                "node": _app.get_config().sandbox.template_node,
                "java": _app.get_config().sandbox.template_java,
                "go": _app.get_config().sandbox.template_go,
                "rust": _app.get_config().sandbox.template_rust,
            },
            "max_total": _app.get_config().sandbox.pool_max_total,
            "max_idle_per_template": _app.get_config().sandbox.pool_max_idle_per_template,
            "ttl_seconds": _app.get_config().sandbox.pool_ttl_seconds,
            "idle_seconds": _app.get_config().sandbox.pool_idle_seconds,
        },
    }


# ─── 9e. POST /api/sandbox/pool/reap — 主动回收(孤儿/超时统一入口) ─
@router.post("/api/sandbox/pool/reap", tags=["沙箱"])
async def sandbox_pool_reap(request: Request, include_orphans: bool = True):
    """主动触发回收：池内超 TTL/空闲沙箱 + （可选）服务端孤儿沙箱。

    统一的"清理孤儿"入口（A2 批1：全局运维操作，仅 admin）：
    - 池 reap：回收池内超龄/空闲沙箱。
    - include_orphans=true（默认）：并清理服务端孤儿沙箱（无项目/任务关联）。
    """
    _require_admin(request)
    from swarm.worker.sandbox_pool import get_sandbox_pool, pool_enabled

    manager = _app._get_sandbox_manager()
    loop = asyncio.get_running_loop()
    result: dict = {"status": "ok"}

    if pool_enabled():
        try:
            result["pool_reap"] = await loop.run_in_executor(None, get_sandbox_pool().reap)
        except Exception as exc:  # noqa: BLE001
            result["pool_reap"] = {"error": str(exc)}
    else:
        result["pool_reap"] = {"skipped": "pool disabled"}

    if include_orphans:
        orphans = await loop.run_in_executor(None, _orphan_sandbox_ids, manager)
        if orphans:
            kills = await asyncio.gather(
                *[loop.run_in_executor(None, manager.kill, sid) for sid in orphans],
                return_exceptions=True,
            )
            failed = sum(1 for r in kills if isinstance(r, Exception))
            result["orphan_cleanup"] = {"killed": len(orphans) - failed, "failed": failed}
        else:
            result["orphan_cleanup"] = {"killed": 0}

    return result


def _orphan_sandbox_ids(manager, server_list=None) -> list[str]:
    """计算孤儿沙箱 ID：服务端存在，但本进程 meta 里无 project_id 且无 task_id 关联。

    判定为孤儿的条件（任一）：
    - 该 sandbox_id 在 _sandbox_meta 中无记录（API 重启后丢失追踪）
    - 记录存在但 project_id 与 task_id 均为空（无归属）

    例外：热池 idle 沙箱（source=pool-idle，或当前在池 _pool 桶里）虽 project_id/
    task_id 均空，但它【归热池所有、待复用】，不是孤儿——否则孤儿清理会误杀活池沙箱、
    留下死引用。这类排除掉。
    """
    if server_list is None:
        server_list = _app._fetch_sandbox_list_from_server()
    # 收集热池当前持有的 idle sid（启用时），这些不算孤儿
    pooled_idle: set[str] = set()
    try:
        from swarm.worker.sandbox_pool import get_sandbox_pool, pool_enabled
        if pool_enabled():
            pool = get_sandbox_pool()
            with pool._lock:  # noqa: SLF001
                for bucket in pool._pool.values():  # noqa: SLF001
                    for entry in bucket:
                        pooled_idle.add(entry.sandbox.sandbox_id)
    except Exception:  # noqa: BLE001
        pass
    orphans: list[str] = []
    for sb in server_list:
        sid = sb.get("id")
        if not sid:
            continue
        if sid in pooled_idle:
            continue  # 活池沙箱，归池所有，待复用
        meta = manager.get_sandbox_meta(sid)
        if meta and meta.get("source") == "pool-idle":
            continue  # 池 idle 标记，归池所有（reaper 按 TTL 回收）
        if not meta:
            orphans.append(sid)
            continue
        if not meta.get("project_id") and not meta.get("task_id"):
            orphans.append(sid)
    return orphans


# ─── 9. DELETE /api/sandbox/{sandbox_id} ───────────
@router.delete("/api/sandbox/{sandbox_id}", tags=["沙箱"])
async def destroy_sandbox(sandbox_id: str, request: Request):
    """销毁沙箱（A2 批1：仅本项目成员/admin 可销毁）"""
    _require_sandbox_access(request, sandbox_id, "task:cancel")
    manager = _app._get_sandbox_manager()
    # 先在本地 _instances 中查找；找不到则尝试直接调 kill
    if sandbox_id not in manager._instances:
        # 尝试用 Sandbox.connect + kill 销毁服务端存在的沙箱
        try:
            from e2b_code_interpreter import Sandbox as _Sandbox
            sb = _Sandbox.connect(sandbox_id)
            sb.kill()
            return {"status": "ok", "message": f"Sandbox {sandbox_id} destroyed via server"}
        except Exception as e:
            # D2：泛化客户端文案，原始异常只进服务端日志（多用户下防内部细节跨用户泄漏）。
            _app.logger.warning("Sandbox connect/kill fallback failed %s: %s", sandbox_id, e)
            raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found") from e
    try:
        manager.kill(sandbox_id)
        return {"status": "ok", "message": f"Sandbox {sandbox_id} destroyed"}
    except Exception as e:
        _app.logger.error(f"Failed to destroy sandbox {sandbox_id}: {e}")
        raise HTTPException(status_code=500, detail="销毁沙箱失败") from e


# ─── 10. GET /api/sandbox/{sandbox_id}/files ───────
@router.get("/api/sandbox/{sandbox_id}/files", tags=["沙箱"])
async def sandbox_files(sandbox_id: str, request: Request, path: str = "/"):
    """获取沙箱内目录列表（A2 批1：仅本项目成员/admin 可读）"""
    _require_sandbox_access(request, sandbox_id, "task:read")
    manager = _app._get_sandbox_manager()
    try:
        loop = asyncio.get_running_loop()
        files = await loop.run_in_executor(
            None, lambda: manager.list_files(sandbox_id, path=path or "/"),
        )
        proxy = _app.get_config().sandbox.proxy_base
        return {
            "status": "ok",
            "sandbox_id": sandbox_id,
            "path": path or "/",
            "proxy_base": proxy,
            "note": (
                "沙箱 /workspace 为 Worker 执行期唯一工作目录（sandbox-first）；"
                "启动时 bootstrap 同步本地项目到 /workspace，"
                "Worker 完成后 pull-back 变更到本地项目路径。"
            ),
            "files": files,
        }
    except Exception as e:
        # D2：泛化客户端文案；原始异常 + proxy 排障提示只进服务端日志（proxy 地址属内部
        # 基础设施细节，多用户下不外泄）。
        proxy = _app.get_config().sandbox.proxy_base
        _app.logger.error(
            "Failed to list files in sandbox %s: %s (确认 CubeProxy 可达 "
            "SWARM_SANDBOX_PROXY_BASE=%s，dev_sidecar 未错指 127.0.0.1:11443)",
            sandbox_id, e, proxy,
        )
        raise HTTPException(status_code=502, detail="无法访问沙箱文件系统")


# ─── 11. GET /api/sandbox/{sandbox_id}/files/content ─
@router.get("/api/sandbox/{sandbox_id}/files/content", tags=["沙箱"])
async def sandbox_file_content(sandbox_id: str, path: str, request: Request):
    """读取沙箱内单个文件内容（A2 批1：仅本项目成员/admin 可读）"""
    _require_sandbox_access(request, sandbox_id, "task:read")
    if not path or not path.startswith("/"):
        raise HTTPException(status_code=400, detail="path 必须为沙箱内绝对路径，如 /workspace/foo.py")
    manager = _app._get_sandbox_manager()
    try:
        from e2b_code_interpreter import Sandbox as _Sandbox

        from swarm.worker.sandbox import read_file_from_sandbox

        sandbox = manager._instances.get(sandbox_id)
        if sandbox is None:
            sandbox = _Sandbox.connect(sandbox_id)
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(
            None, lambda: read_file_from_sandbox(sandbox, path, manager=manager),
        )
        if isinstance(data, bytes):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                import base64
                return {
                    "status": "ok",
                    "sandbox_id": sandbox_id,
                    "path": path,
                    "encoding": "base64",
                    "content": base64.b64encode(data).decode("ascii"),
                }
        else:
            text = str(data)
        return {
            "status": "ok",
            "sandbox_id": sandbox_id,
            "path": path,
            "encoding": "utf-8",
            "content": text,
        }
    except Exception as e:
        _app.logger.error("Failed to read file in sandbox %s path=%s: %s", sandbox_id, path, e)
        raise HTTPException(status_code=502, detail="无法读取沙箱文件")  # D2：泛化，异常只进日志


@router.get("/api/sandbox/{sandbox_id}/logs", tags=["沙箱"])
async def sandbox_logs(sandbox_id: str, request: Request, limit: int = 200):
    """沙箱活动日志（A2 批1：仅本项目成员/admin 可读）"""
    _require_sandbox_access(request, sandbox_id, "task:read")
    manager = _app._get_sandbox_manager()
    cap = max(1, min(limit, 500))
    loop = asyncio.get_running_loop()
    logs = await loop.run_in_executor(
        None, lambda: manager.get_activity(sandbox_id, limit=cap),
    )
    meta = manager.get_sandbox_meta(sandbox_id) or {}
    return {
        "sandbox_id": sandbox_id,
        "logs": logs,
        "count": len(logs),
        "project_id": meta.get("project_id"),
        "task_id": meta.get("task_id"),
        "source": meta.get("source"),
    }


# ═══════════════════════════════════════════════════════════
# 沙箱模板配置（exec/verify 镜像，落库，系统级 WebUI 可配）
# ═══════════════════════════════════════════════════════════

class SandboxTemplatesRequest(BaseModel):
    """保存沙箱模板配置：每语言 exec(2c2g) + verify(4c4g)。"""
    templates: dict = Field(
        description="{language: {exec_template, verify_template}}，language ∈ python/node/java/go/rust",
    )


@router.get("/api/sandbox/templates", tags=["沙箱"])
async def get_sandbox_templates(request: Request):
    """当前沙箱模板配置：每语言 exec+verify 的【生效值】（db 优先，回退默认）+ 来源标注。"""
    from swarm.api._shared import _require_user
    from swarm.config import sandbox_store

    _require_user(request)
    cfg = _app.get_config().sandbox
    loop = asyncio.get_running_loop()
    db_rows = await loop.run_in_executor(None, sandbox_store.get_all)

    out = {}
    for lang in sandbox_store.LANGUAGES:
        # 生效值（db 优先回退默认），同时标来源便于 UI 显示
        eff_exec = cfg.template_for_language(lang, purpose="exec")
        eff_verify = cfg.template_for_language(lang, purpose="verify")
        db_row = db_rows.get(lang, {})
        out[lang] = {
            "exec_template": eff_exec,
            "verify_template": eff_verify,
            "exec_from_db": bool(db_row.get("exec_template")),
            "verify_from_db": bool(db_row.get("verify_template")),
        }
    return {"templates": out, "languages": list(sandbox_store.LANGUAGES)}


@router.put("/api/sandbox/templates", tags=["沙箱"])
async def update_sandbox_templates(req: SandboxTemplatesRequest, request: Request):
    """保存沙箱模板配置到 db（落库），并 reload 让生效。

    body: {"templates": {"java": {"exec_template": "tpl-...", "verify_template": "tpl-..."}, ...}}
    只接受已知语言；空串表示该项清空（回退默认值）。
    """
    from swarm.api._shared import _require_perm
    from swarm.config import sandbox_store

    _require_perm(request, "config:write")
    loop = asyncio.get_running_loop()
    saved = []
    for lang, vals in (req.templates or {}).items():
        if lang not in sandbox_store.LANGUAGES or not isinstance(vals, dict):
            continue
        exec_t = str(vals.get("exec_template", "") or "").strip()
        verify_t = str(vals.get("verify_template", "") or "").strip()
        await loop.run_in_executor(
            None, lambda l=lang, e=exec_t, v=verify_t: sandbox_store.set_templates(l, e, v)
        )
        saved.append(lang)

    # reload + 失效缓存（让运行进程立即用新配置）
    from swarm.config.settings import reload_config as _reload_config
    await loop.run_in_executor(None, _reload_config)
    await loop.run_in_executor(None, sandbox_store.invalidate_cache)
    _app.logger.info("沙箱模板配置已更新: %s", saved)
    return {"status": "ok", "saved": saved}


# ═══════════════════════════════════════════════════════════
# 命令安全黑名单（A2 批3，落库 + 管理员 WebUI 可配 + 内置默认）
# ═══════════════════════════════════════════════════════════

class BlacklistRuleRequest(BaseModel):
    """新增黑名单规则：正则 pattern + 描述。"""
    pattern: str = Field(description="正则表达式，对整条命令 search 匹配")
    description: str = Field(default="", description="规则说明")


@router.get("/api/sandbox/command-blacklist", tags=["沙箱"])
async def list_command_blacklist(request: Request):
    """命令黑名单规则列表（A2 批3：系统级安全配置，仅 admin）。"""
    _require_admin(request)
    from swarm.config import command_blacklist_store
    loop = asyncio.get_running_loop()
    rules = await loop.run_in_executor(None, command_blacklist_store.list_rules)
    return {"status": "ok", "rules": rules}


@router.post("/api/sandbox/command-blacklist", tags=["沙箱"])
async def add_command_blacklist(req: BlacklistRuleRequest, request: Request):
    """新增黑名单规则（仅 admin）。保存即生效（失效缓存）。"""
    _require_admin(request)
    from swarm.config import command_blacklist_store
    import re as _re
    try:
        _re.compile(req.pattern)
    except _re.error as exc:
        raise HTTPException(status_code=400, detail=f"无效正则: {exc}")
    loop = asyncio.get_running_loop()
    rid = await loop.run_in_executor(
        None, lambda: command_blacklist_store.add_rule(req.pattern, req.description)
    )
    _app.logger.info("[A2] 新增命令黑名单规则 #%d: %s", rid, req.pattern)
    return {"status": "ok", "id": rid}


@router.post("/api/sandbox/command-blacklist/{rule_id}/toggle", tags=["沙箱"])
async def toggle_command_blacklist(rule_id: int, request: Request):
    """启停某规则（仅 admin）。body: {"enabled": true/false}"""
    _require_admin(request)
    from swarm.config import command_blacklist_store
    body = await request.json()
    enabled = bool(body.get("enabled"))
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: command_blacklist_store.set_rule_enabled(rule_id, enabled))
    return {"status": "ok", "id": rule_id, "enabled": enabled}


@router.delete("/api/sandbox/command-blacklist/{rule_id}", tags=["沙箱"])
async def delete_command_blacklist(rule_id: int, request: Request):
    """删除规则（仅 admin）。内置规则不可删（只能 disable）。"""
    _require_admin(request)
    from swarm.config import command_blacklist_store
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, lambda: command_blacklist_store.delete_rule(rule_id))
    if not ok:
        raise HTTPException(status_code=400, detail="规则不存在或为内置规则（内置规则只能停用不能删除）")
    return {"status": "ok", "deleted": rule_id}
