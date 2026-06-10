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
