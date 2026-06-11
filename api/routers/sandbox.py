"""api/routers/sandbox.py — 沙箱域路由 (状态/创建/销毁/文件列表/文件内容/日志)。

从 api/app.py 抽出, app.include_router 挂载。
app 级 helper(_get_sandbox_manager/_fetch_sandbox_list_from_server/logger 等)
用 _app. 属性访问(跨域共享, 保持单一定义)。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import swarm.api.app as _app

router = APIRouter()


class SandboxCreateRequest(BaseModel):
    """创建沙箱请求"""
    template_id: str | None = Field(
        default=None,
        description="沙箱模板 ID，默认使用配置中的 default_template",
    )
    timeout: int = Field(default=60, description="创建超时（秒）")
    project_id: str | None = Field(default=None, description="关联项目 ID")


@router.get("/api/sandbox/status", tags=["沙箱"])
async def sandbox_status(project_id: str | None = None):
    """活跃沙箱列表（可按 project_id 过滤，仅显示本项目注册/创建的沙箱）"""
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

    return {
        "active_count": len(sandboxes),
        "sandboxes": sandboxes,
        "project_id": project_id,
        "config": {
            "api_url": _app.get_config().sandbox.api_url,
            "proxy_base": _app.get_config().sandbox.proxy_base,
            "default_template": _app.get_config().sandbox.default_template,
            "use_for_worker": _app.get_config().sandbox.use_for_worker,
        },
    }


# ─── 8. POST /api/sandbox/create ───────────────────
@router.post("/api/sandbox/create", tags=["沙箱"])
async def create_sandbox(req: SandboxCreateRequest):
    """创建新沙箱"""
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
        _app.logger.error(f"Failed to create sandbox: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create sandbox: {str(e)}")


# ─── 9b. POST /api/sandbox/cleanup — 批量清理（释放泄漏资源）─
@router.post("/api/sandbox/cleanup", tags=["沙箱"])
async def cleanup_sandboxes(task_id: str | None = None, server: bool = False, orphans_only: bool = False):
    """批量销毁沙箱，释放 CubeSandbox/小模型资源。

    - task_id：只销毁该任务关联的沙箱（kill_by_task）。
    - orphans_only=true：只销毁孤儿沙箱（服务端在跑、但无任何项目/任务关联）。
      **全局运维推荐**——不误伤正在被某项目/任务使用的沙箱。
    - server=true：以服务端权威列表为准，销毁所有服务端沙箱（含正在用的）。
    - 默认（无参数）：销毁本进程追踪的全部沙箱（仅本地 _instances）。

    并行 kill，避免逐个串行耗时（实测 95 个串行需 ~280s）。
    """
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
async def list_orphan_sandboxes():
    """列出孤儿沙箱：服务端在跑、但无任何项目/任务关联的沙箱。

    全局运维视图（不绑定项目）。用于系统设置里展示「孤儿数 / 服务端总数」。
    """
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


# ─── 9d. GET /api/sandbox/pool — 全局热池可观测面板 ─
@router.get("/api/sandbox/pool", tags=["沙箱"])
async def sandbox_pool_status():
    """全局热沙箱池状态卡：池统计 + 孤儿沙箱 + 服务端总数，一处汇总。

    供 webui 全局池面板展示：是否启用、各语言桶深度、借出/空闲、孤儿数、复用率。
    """
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
async def sandbox_pool_reap(include_orphans: bool = True):
    """主动触发回收：池内超 TTL/空闲沙箱 + （可选）服务端孤儿沙箱。

    统一的"清理孤儿"入口（取代分散的手动清理）：
    - 池 reap：回收池内超龄/空闲沙箱。
    - include_orphans=true（默认）：并清理服务端孤儿沙箱（无项目/任务关联）。
    """
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
    """
    if server_list is None:
        server_list = _app._fetch_sandbox_list_from_server()
    orphans: list[str] = []
    for sb in server_list:
        sid = sb.get("id")
        if not sid:
            continue
        meta = manager.get_sandbox_meta(sid)
        if not meta:
            orphans.append(sid)
            continue
        if not meta.get("project_id") and not meta.get("task_id"):
            orphans.append(sid)
    return orphans


# ─── 9. DELETE /api/sandbox/{sandbox_id} ───────────
@router.delete("/api/sandbox/{sandbox_id}", tags=["沙箱"])
async def destroy_sandbox(sandbox_id: str):
    """销毁沙箱"""
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
            raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found: {e}")
    try:
        manager.kill(sandbox_id)
        return {"status": "ok", "message": f"Sandbox {sandbox_id} destroyed"}
    except Exception as e:
        _app.logger.error(f"Failed to destroy sandbox {sandbox_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to destroy sandbox: {str(e)}")


# ─── 10. GET /api/sandbox/{sandbox_id}/files ───────
@router.get("/api/sandbox/{sandbox_id}/files", tags=["沙箱"])
async def sandbox_files(sandbox_id: str, path: str = "/"):
    """获取沙箱内目录列表（CubeProxy 经 dev_sidecar 转发）"""
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
        _app.logger.error("Failed to list files in sandbox %s: %s", sandbox_id, e)
        proxy = _app.get_config().sandbox.proxy_base
        raise HTTPException(
            status_code=502,
            detail=(
                f"无法访问沙箱文件系统: {e}. "
                f"请确认 CubeProxy 可达 (SWARM_SANDBOX_PROXY_BASE={proxy})，"
                "且 dev_sidecar 未被错误指向 127.0.0.1:11443。"
            ),
        )


# ─── 11. GET /api/sandbox/{sandbox_id}/files/content ─
@router.get("/api/sandbox/{sandbox_id}/files/content", tags=["沙箱"])
async def sandbox_file_content(sandbox_id: str, path: str):
    """读取沙箱内单个文件内容（CubeProxy 经 dev_sidecar 转发）"""
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
        raise HTTPException(status_code=502, detail=f"无法读取沙箱文件: {e}")


@router.get("/api/sandbox/{sandbox_id}/logs", tags=["沙箱"])
async def sandbox_logs(sandbox_id: str, limit: int = 200):
    """沙箱活动日志 — Worker 阶段日志 + run_code stdout/stderr"""
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
