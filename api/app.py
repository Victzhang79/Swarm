"""Swarm Web 后端 API — FastAPI 应用 + 全部端点

端点列表:
  GET  /api/status                    — 系统组件运行状态
  GET  /api/config                    — 当前配置（API Key 脱敏）
  PUT  /api/config                    — 更新配置（写入 .env 并重载）
  GET  /api/routing                   — 获取当前模型路由表
  PUT  /api/routing                   — 更新模型路由表配置
  POST /api/demo                      — 触发 swarm demo 任务（后台执行，返回 run_id）
  GET  /api/demo/stream?run_id=xxx    — SSE 流式订阅 demo 执行进度（需 run_id，不自动触发任务）
  GET  /api/sandbox/status            — 活跃沙箱列表（含详情）
  POST /api/sandbox/create            — 创建新沙箱
  DELETE /api/sandbox/{sid}           — 销毁沙箱
  GET  /api/sandbox/{sid}/files       — 查看沙箱文件列表
  GET  /api/sandbox/{sid}/files/content — 读取沙箱内文件内容
  GET  /api/sandbox/{sid}/logs          — 沙箱活动日志（Worker + run_code）
  GET  /api/health                    — 健康检查
  GET  /api/projects                  — 项目列表
  POST /api/projects                  — 创建项目（自动启动预处理）
  GET  /api/projects/{project_id}     — 项目详情
  DELETE /api/projects/{project_id}   — 删除项目
  POST /api/projects/{project_id}/preprocess         — 手动触发/重新预处理
  GET  /api/projects/{project_id}/preprocess/progress — SSE 预处理进度流
  GET  /api/projects/{project_id}/tasks — 项目任务列表
  POST /api/projects/{project_id}/tasks — 创建任务
  POST /api/projects/{project_id}/worker/run — Phase 0 单 Worker 直跑
  POST /api/projects/{project_id}/apply-diff — 将 diff 应用到项目工作区
  GET  /api/worker/{run_id}/stream    — SSE Worker 进度流
  GET  /api/tasks/{task_id}           — 任务详情
  GET  /api/tasks/{task_id}/stream    — SSE 任务执行进度流
  WS   /ws/tasks/{task_id}            — WebSocket 任务执行进度流（与 SSE 并存）
  POST /api/tasks/{task_id}/apply-diff — git apply merged_diff
  POST /api/tasks/{task_id}/approve   — 审核通过（resume Brain）
  POST /api/tasks/{task_id}/revise    — 审核修订（resume Brain）
  POST /api/tasks/{task_id}/reject    — 审核拒绝（resume Brain）
  POST /api/tasks/{task_id}/cancel    — 取消任务
  POST /api/tasks/{task_id}/retry     — 重跑任务
  DELETE /api/tasks/{task_id}         — 删除任务
  GET  /api/projects/{project_id}/knowledge/overview — 知识库概览
  GET  /api/projects/{project_id}/knowledge/symbols — Layer A 符号搜索
  GET  /api/projects/{project_id}/knowledge/semantic — Layer B 语义检索
  POST /api/projects/{project_id}/knowledge/retrieve — 编排检索实验
  GET  /api/projects/{project_id}/knowledge/norms           — 项目规范列表
  POST /api/projects/{project_id}/knowledge/norms           — 添加规范
  PUT  /api/projects/{project_id}/knowledge/norms/{norm_id} — 编辑规范
  DELETE /api/projects/{project_id}/knowledge/norms/{norm_id} — 删除规范
  GET  /api/projects/{project_id}/memories/mistakes         — 错题列表
  POST /api/projects/{project_id}/memories/mistakes         — 添加错题
  DELETE /api/projects/{project_id}/memories/mistakes/{mid} — 删除错题
  GET  /api/projects/{project_id}/memories/successes        — 成功模式列表
  POST /api/projects/{project_id}/memories/successes        — 添加成功模式
  DELETE /api/projects/{project_id}/memories/successes/{sid} — 删除成功模式
  GET  /api/projects/{project_id}/memories/summaries        — 任务摘要列表
  POST /api/projects/{project_id}/memories/summaries        — 添加任务摘要
  PUT  /api/projects/{project_id}/memories/summaries/{sid}  — 编辑任务摘要
  DELETE /api/projects/{project_id}/memories/summaries/{sid} — 删除任务摘要
  GET  /api/projects/{project_id}/memories/profile          — L1 用户画像
  PUT  /api/projects/{project_id}/memories/profile          — 更新 L1 用户画像
  GET  /api/projects/{project_id}/knowledge/behavior-hotspots — Layer D 行为热点
  GET  /api/stats                     — 任务统计（可选 project_id）
  GET  /api/projects/{project_id}/stats — 项目任务统计
  GET  /api/notifications             — 任务完成/失败/待审通知
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from swarm.api._shared import (
    _EMBEDDING_ZERO,
    _flatten_model_config,
    _mask_config_dict,
    _parse_since_param,
    _profile_storage_key,
    _require_perm,
    _require_user,
    _resolve_key,
)
from swarm.config.settings import (
    get_config,
    reload_config,
)
from swarm.project import preprocess, store
from swarm.tracing import configure_langsmith


def _get_pg_conn():
    """获取池化 psycopg 同步连接（autocommit）。

    返回的是连接池的 connection() 上下文管理器，用法 `with _get_pg_conn() as conn:`，
    退出时归还池而非关闭。
    """
    from swarm.infra.db import sync_pool

    return sync_pool().connection()


def _validate_project(project_id: str) -> None:
    """校验项目是否存在，不存在则抛 404"""
    project = store.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

# LangSmith 在 on_startup 中初始化（需在 _configure_app_logging 之后，才能写入 swarm.log）

# 项目根目录（本仓库根 = swarm 包目录）
_PROJECT_ROOT = Path(__file__).parent.parent

logger = logging.getLogger(__name__)
_LOG_FILE = _PROJECT_ROOT / "swarm.log"


def _configure_app_logging() -> None:
    """将 swarm.* 应用日志写入 swarm.log（与 uvicorn 访问日志同文件）"""
    if getattr(_configure_app_logging, "_done", False):
        return
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        fh = logging.FileHandler(_LOG_FILE, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(logging.INFO)
        for name in ("swarm", "uvicorn.error"):
            lg = logging.getLogger(name)
            lg.setLevel(logging.INFO)
            if not any(isinstance(h, logging.FileHandler) for h in lg.handlers):
                lg.addHandler(fh)
    except OSError as exc:
        logger.warning("Cannot write app logs to %s: %s", _LOG_FILE, exc)
    _configure_app_logging._done = True  # type: ignore[attr-defined]

# ═══════════════════════════════════════════════════
# 启动时初始化 dev_sidecar
# ═══════════════════════════════════════════════════

_sidecar_initialized = False


def _init_sidecar() -> None:
    """应用启动时初始化 dev_sidecar 代理"""
    global _sidecar_initialized
    if _sidecar_initialized:
        return
    try:
        from swarm.worker.sandbox import apply_sandbox_env

        apply_sandbox_env()
        sidecar_path = _PROJECT_ROOT / "test" / "sandbox" / "dev_sidecar.py"
        if sidecar_path.exists():
            sys.path.insert(0, str(sidecar_path.parent))
            from dev_sidecar import setup_dev_sidecar

            setup_dev_sidecar()
            _sidecar_initialized = True
            logger.info("dev_sidecar initialized on app startup (proxy=%s)", os.environ.get("CUBE_REMOTE_PROXY_BASE"))
        else:
            logger.warning(f"dev_sidecar not found at {sidecar_path}")
    except Exception as e:
        logger.error(f"Failed to init dev_sidecar: {e}")


# ═══════════════════════════════════════════════════
# API Key 脱敏
# ═══════════════════════════════════════════════════







# ═══════════════════════════════════════════════════
# 组件状态检查
# ═══════════════════════════════════════════════════

async def _check_component(name: str) -> dict[str, Any]:
    """检查单个组件的运行状态 — 真实连通性检测"""
    import httpx

    status: dict[str, Any] = {"name": name, "status": "unknown", "detail": ""}

    try:
        # ─── Brain 状态机 ─────────────────────────────
        if name == "Brain 状态机":
            from swarm.brain.graph import get_compiled_brain_graph
            get_compiled_brain_graph()
            status["status"] = "running"
            status["detail"] = "Graph compiled OK"

        # ─── Worker 执行器 ─────────────────────────────
        elif name == "Worker 执行器":
            cfg = get_config()
            issues: list[str] = []
            # 检查 worker 模型配置
            if not cfg.model.worker_primary:
                issues.append("worker_primary model not set")
            # 检查 sandbox 配置完整性
            try:
                sc = get_config().sandbox
                if not sc.api_url:
                    issues.append("sandbox api_url not set")
            except Exception as e:
                issues.append(f"sandbox config error: {e}")
            if issues:
                status["status"] = "degraded"
                status["detail"] = "; ".join(issues)
            else:
                status["status"] = "ready"
                status["detail"] = (
                    f"worker_primary={cfg.model.worker_primary}, "
                    f"sandbox_api={cfg.sandbox.api_url}"
                )

        # ─── 知识库 ────────────────────────────────────
        elif name == "知识库":
            cfg = get_config()
            qdrant_url = cfg.db.qdrant_url
            details: list[str] = []

            # 1) 检查 Qdrant 是否在线
            qdrant_ok = False
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(f"{qdrant_url}/collections")
                    if resp.status_code == 200:
                        data = resp.json()
                        collections = data.get("result", {}).get("collections", [])
                        details.append(f"qdrant online, {len(collections)} collections")
                        qdrant_ok = True
                    else:
                        details.append(f"qdrant HTTP {resp.status_code}")
            except Exception as e:
                # fallback 本地文件模式
                try:
                    from qdrant_client import QdrantClient
                    storage_path = os.path.expanduser("~/.swarm/qdrant")
                    if os.path.exists(storage_path):
                        def _check_local_qdrant_kb():
                            c = QdrantClient(path=storage_path)
                            cols = c.get_collections()
                            return [col.name for col in cols.collections]
                        coll_names = await asyncio.to_thread(_check_local_qdrant_kb)
                        details.append(f"qdrant local file, {len(coll_names)} collections")
                        qdrant_ok = True  # noqa: F841
                    else:
                        details.append(f"qdrant unreachable: {type(e).__name__}")
                except Exception:
                    details.append(f"qdrant unreachable: {type(e).__name__}")

            # 2) 检查 embedding 模型可用性
            embed_ok = False
            try:
                from fastembed import TextEmbedding  # noqa: F401
                details.append(f"embedding: {cfg.knowledge.embedding_model} (fastembed)")
                embed_ok = True
            except ImportError:
                pass
            if not embed_ok:
                try:
                    from sentence_transformers import SentenceTransformer  # noqa: F401
                    details.append(f"embedding: {cfg.knowledge.embedding_model} (sentence-transformers)")
                    embed_ok = True
                except ImportError:
                    pass
            if not embed_ok:
                # 尝试通过 HTTP 检测远程 embedding endpoint
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        if cfg.model.siliconflow_api_key:
                            resp = await client.post(
                                f"{cfg.model.siliconflow_base_url}/embeddings",
                                json={"model": cfg.knowledge.embedding_model, "input": "test"},
                                headers={"Authorization": f"Bearer {cfg.model.siliconflow_api_key}"},
                            )
                            if resp.status_code == 200:
                                dim = len(resp.json().get("data", [{}])[0].get("embedding", []))
                                details.append(f"embedding: {cfg.knowledge.embedding_model} (remote, dim={dim})")
                                embed_ok = True
                except Exception as exc:
                    logger.debug("embedding 远程探测失败: %s", exc)
            if not embed_ok:
                details.append("embedding: no local model, no remote endpoint")

            qdrant_ok_flag = any("qdrant" in d for d in details)
            embed_ok_flag = any("embedding:" in d for d in details)
            if qdrant_ok_flag and embed_ok_flag:
                status["status"] = "running"
            elif qdrant_ok_flag or embed_ok_flag:
                status["status"] = "degraded"
            else:
                status["status"] = "error"
            status["detail"] = "; ".join(details)

        # ─── 记忆系统 ──────────────────────────────────
        elif name == "记忆系统":
            import psycopg

            cfg = get_config()
            uri = cfg.db.postgres_uri

            def _check_memory() -> dict[str, Any]:
                """同步检测记忆系统（在线程中执行）"""
                result: dict[str, Any] = {}
                with psycopg.connect(uri, autocommit=True) as conn:
                    # 检查连接
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        cur.fetchone()
                    result["pg_ok"] = True

                    # 检查 mem_* 表是否存在
                    mem_tables = ["mem_user_profile", "mem_task_summary", "mem_mistakes", "mem_successes"]
                    existing: list[str] = []
                    for tbl in mem_tables:
                        try:
                            with conn.cursor() as cur:
                                cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                                cnt = cur.fetchone()[0]
                                existing.append(f"{tbl}({cnt})")
                        except Exception:
                            pass  # 表不存在
                    result["tables"] = existing
                return result

            mem_result = await asyncio.to_thread(_check_memory)

            if not mem_result.get("pg_ok"):
                status["status"] = "error"
                status["detail"] = "PostgreSQL connection failed"
            elif not mem_result.get("tables"):
                status["status"] = "degraded"
                status["detail"] = "PG connected, tables not created yet"
            else:
                status["status"] = "running"
                status["detail"] = f"tables: {', '.join(mem_result['tables'])}"

        # ─── 远程沙箱 ──────────────────────────────────
        elif name == "远程沙箱":
            def _check_sandbox() -> dict[str, Any]:
                """同步检测远程沙箱（在线程中执行）"""
                result: dict[str, Any] = {}
                try:
                    from e2b_code_interpreter import Sandbox  # noqa: F401

                    # 设置必要环境变量
                    from swarm.config.settings import get_config as _get_cfg
                    sc = _get_cfg().sandbox
                    os.environ["E2B_API_URL"] = sc.api_url
                    os.environ["E2B_API_KEY"] = sc.api_key
                    os.environ["CUBE_REMOTE_PROXY_BASE"] = sc.proxy_base
                    os.environ["CUBE_REMOTE_PROXY_VERIFY_SSL"] = str(sc.verify_ssl).lower()
                    os.environ.pop("E2B_DOMAIN", None)
                    # 尝试通过 e2b SDK 列出沙箱
                    try:
                        from e2b.sandbox_sync.sandbox_api import SandboxApi
                        paginator = SandboxApi.list(limit=10)
                        # SandboxPaginator 不支持 list()，用 next_items()
                        sandbox_list = []
                        if paginator:
                            try:
                                items = paginator.next_items() if hasattr(paginator, 'next_items') else list(paginator)
                                if items:
                                    sandbox_list = items
                            except StopIteration:
                                pass
                        result["sandbox_count"] = len(sandbox_list)
                    except Exception as list_err:
                        # SandboxApi.list 可能不可用，降级为仅检查连通性
                        result["sandbox_count"] = None
                        result["list_error"] = str(list_err)[:100]
                    result["api_url"] = sc.api_url
                    result["import_ok"] = True
                except ImportError as ie:
                    result["import_error"] = f"e2b import failed: {ie}"
                except Exception as e:
                    result["error"] = str(e)[:200]
                return result

            sb_result = await asyncio.to_thread(_check_sandbox)

            if "import_error" in sb_result:
                status["status"] = "error"
                status["detail"] = sb_result["import_error"]
            elif "error" in sb_result:
                status["status"] = "degraded"
                status["detail"] = f"api_url={sb_result.get('api_url', '?')}, error: {sb_result['error']}"
            elif sb_result.get("sandbox_count") is not None:
                status["status"] = "running"
                status["detail"] = f"api_url={sb_result['api_url']}, active={sb_result['sandbox_count']}"
            else:
                # list 调用失败但 import 成功 — 连通性不确定
                status["status"] = "degraded"
                list_err = sb_result.get("list_error", "unknown")
                status["detail"] = f"api_url={sb_result.get('api_url', '?')}, list failed: {list_err}"

        # ─── 模型路由 ──────────────────────────────────
        elif name == "模型路由":
            cfg = get_config()
            details = []

            # 检测本地模型可用性
            local_ok = False
            try:
                headers = {}
                if cfg.model.local_api_key and cfg.model.local_api_key not in ("", "***"):
                    headers["Authorization"] = f"Bearer {cfg.model.local_api_key}"
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(f"{cfg.model.local_base_url}/models", headers=headers)
                    if resp.status_code == 200:
                        models_data = resp.json().get("data", [])
                        local_count = len(models_data)
                        if local_count > 0:
                            local_ok = True
                            details.append(f"本地 {local_count} 个模型可用")
                        else:
                            details.append("本地模型列表为空(可能需要API Key)")
                    else:
                        details.append(f"本地模型HTTP {resp.status_code}")
            except Exception:
                details.append("本地模型不可达")

            # 检测云端模型可用性
            cloud_ok = bool(cfg.model.siliconflow_api_key and cfg.model.siliconflow_api_key not in ("", "***"))
            if cloud_ok:
                details.append("云端(SiliconFlow)已配置")
            else:
                details.append("云端未配置API Key")

            if local_ok and cloud_ok:
                status["status"] = "running"
            elif local_ok or cloud_ok:
                status["status"] = "degraded"
            else:
                status["status"] = "error"
            status["detail"] = "; ".join(details)

        # ─── PostgreSQL ────────────────────────────────
        elif name == "PostgreSQL":
            import psycopg

            cfg = get_config()
            uri = cfg.db.postgres_uri

            def _check_pg() -> dict[str, Any]:
                """同步检测 PostgreSQL（在线程中执行）"""
                result: dict[str, Any] = {}
                with psycopg.connect(uri, autocommit=True) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT version()")
                        ver = cur.fetchone()[0]
                    result["version"] = ver
                return result

            pg_result = await asyncio.to_thread(_check_pg)
            status["status"] = "running"
            # 只取版本号主行（第一段）
            ver_short = pg_result["version"].split(",")[0] if pg_result.get("version") else "unknown"
            status["detail"] = ver_short

        # ─── Qdrant ────────────────────────────────────
        elif name == "Qdrant":
            cfg = get_config()
            qdrant_url = cfg.db.qdrant_url
            # 优先检测远程 server
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(f"{qdrant_url}/collections")
                    if resp.status_code == 200:
                        data = resp.json()
                        collections = data.get("result", {}).get("collections", [])
                        coll_names = [c.get("name", "?") for c in collections]
                        status["status"] = "running"
                        status["detail"] = f"server online, collections: {coll_names or 'none'}"
                    else:
                        status["status"] = "error"
                        status["detail"] = f"HTTP {resp.status_code}"
            except Exception:
                # 远程不可达，检查本地文件是否存在（不打开，避免和知识库检测锁冲突）
                storage_path = os.path.expanduser("~/.swarm/qdrant")
                if os.path.exists(storage_path) and os.path.exists(os.path.join(storage_path, "meta.json")):
                    status["status"] = "ready"
                    status["detail"] = "local file mode (verified by 知识库 check)"
                else:
                    status["status"] = "error"
                    status["detail"] = f"server unreachable, no local storage at {storage_path}"

        else:
            status["status"] = "unknown"

    except Exception as e:
        status["status"] = "error"
        status["detail"] = str(e)[:200]

    return status


# ═══════════════════════════════════════════════════
# Pydantic 请求/响应模型
# ═══════════════════════════════════════════════════


class ConfigUpdateRequest(BaseModel):
    """配置更新请求
    
    支持两种键名格式：
    1. 环境变量名：SWARM_MODEL_SILICONFLOW_API_KEY
    2. 短名：siliconflow_api_key（自动添加 SWARM_MODEL_ 前缀）
    """
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="配置键值对，支持环境变量名或短名",
    )


# 短名 → 环境变量名映射




class SandboxCreateRequest(BaseModel):
    """创建沙箱请求"""
    template_id: str | None = Field(
        default=None,
        description="沙箱模板 ID，默认使用配置中的 default_template",
    )
    timeout: int = Field(default=60, description="创建超时（秒）")
    project_id: str | None = Field(default=None, description="关联项目 ID")


class DemoRequest(BaseModel):
    """Demo 任务请求"""
    task: str = Field(
        default="计算斐波那契数列前 10 项并输出结果",
        description="要在沙箱中执行的任务描述",
    )
    code: str | None = Field(
        default=None,
        description="自定义 Python 代码，不提供则自动生成",
    )
    project_id: str | None = Field(
        default=None,
        description="关联的项目ID，用于设置 Worker 的工作目录",
    )


class ProjectCreateRequest(BaseModel):
    """创建项目请求"""
    name: str = Field(description="项目名称")
    path: str = Field(description="项目根目录绝对路径")
    description: str = Field(default="", description="项目描述")


class TaskCreateRequest(BaseModel):
    """创建任务请求"""
    description: str = Field(description="任务描述")
    auto_accept: bool = Field(default=False, description="自动通过审核（E2E/演示）")
    priority: str = Field(default="normal", description="队列优先级: urgent / normal / background")


class TaskReviseRequest(BaseModel):
    """审核修订请求"""
    feedback: str = Field(description="修订反馈意见")


class TaskRetryRequest(BaseModel):
    """重跑任务请求"""
    auto_accept: bool | None = Field(default=None, description="自动通过审核（默认沿用环境变量）")


class WorkerRunRequest(BaseModel):
    """Phase 0 — 单 Worker 直跑（不经 Brain）"""
    description: str = Field(description="子任务描述")
    difficulty: str = Field(default="medium", description="trivial | medium | complex")
    writable: list[str] | None = Field(default=None, description="可写路径，默认全项目")
    readable: list[str] | None = Field(default=None, description="可读路径，默认全项目")


class ApplyDiffRequest(BaseModel):
    """将 merged_diff 应用到项目工作区"""
    diff: str | None = Field(default=None, description="可选覆盖 task.merged_diff")
    check_only: bool = Field(default=False, description="仅 git apply --check")


class ApproveTaskRequest(BaseModel):
    """审核通过选项"""
    apply_diff: bool = Field(
        default=False,
        description="显式 git apply；sandbox_first 模式下通常已由 pull-back 写回本地",
    )


# ═══════════════════════════════════════════════════
# ═══════════════════════════════════════════════════
# 全局 SandboxManager（懒加载单例，与 Worker 共享）
# ═══════════════════════════════════════════════════

def _get_sandbox_manager() -> Any:
    from swarm.worker.sandbox import get_sandbox_manager
    return get_sandbox_manager()


# ═══════════════════════════════════════════════════
# Demo 运行注册表（SSE 订阅用）
# ═══════════════════════════════════════════════════
# POST /api/demo 触发任务后生成 run_id 并注册，
# GET /api/demo/stream?run_id=xxx 只订阅已有任务的进度，
# 不再自动触发新任务（修复 GPU 无限占用 bug）

_demo_runs: dict[str, asyncio.Queue] = {}  # run_id → asyncio.Queue[dict]


def _new_demo_run() -> tuple[str, asyncio.Queue]:
    """创建新的 demo 运行注册，返回 (run_id, queue)"""
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    queue: asyncio.Queue[dict] = asyncio.Queue()
    _demo_runs[run_id] = queue
    # 限制注册表大小：清理超过 30 分钟的旧 run
    _cleanup_old_runs()
    return run_id, queue


def _cleanup_old_runs() -> None:
    """清理注册表中超过 100 条的旧记录"""
    if len(_demo_runs) > 100:
        # 简单策略：删除最早的条目
        to_del = list(_demo_runs.keys())[:len(_demo_runs) - 50]
        for k in to_del:
            _demo_runs.pop(k, None)


# ═══════════════════════════════════════════════════
# FastAPI 应用
# ═══════════════════════════════════════════════════

app = FastAPI(
    title="Swarm API",
    version="0.1.0",
    description="Swarm Web 后端 API",
)

from swarm.api.auth import SwarmAPIKeyMiddleware  # noqa: E402

app.add_middleware(SwarmAPIKeyMiddleware)


@app.on_event("startup")
async def on_startup():
    """应用启动钩子：LangSmith + dev_sidecar + 建表 + L5 衰减调度"""
    _configure_app_logging()
    configure_langsmith()
    _init_sidecar()
    # 确保 project/task 表存在
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, store.ensure_tables)
        logger.info("Project/Task tables ensured")
    except Exception as e:
        logger.warning(f"Failed to ensure project tables: {e}")
    try:
        from swarm.auth.store import (
            backfill_legacy_project_members,
            ensure_admin_default_profile,
            ensure_auth_tables,
            ensure_bootstrap_admin,
        )
        from swarm.config.settings import get_config

        cfg = get_config()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, ensure_auth_tables)
        if cfg.rbac_enabled:
            admin_user = await loop.run_in_executor(
                None,
                lambda: ensure_bootstrap_admin(
                    password=cfg.bootstrap_admin_password,
                    reset_password=cfg.bootstrap_reset_admin_password,
                ),
            )
            await loop.run_in_executor(
                None,
                lambda: ensure_admin_default_profile(admin_user.id),
            )
            await loop.run_in_executor(None, backfill_legacy_project_members)
        logger.info("Auth/RBAC tables ensured")
    except Exception as e:
        logger.warning(f"Failed to ensure auth tables: {e}")
    await _start_memory_decay_scheduler()
    await _start_kb_update_scheduler()
    await _start_consistency_scheduler()
    await _start_task_scheduler()
    _warn_if_multiprocess()
    logger.info("Swarm API started")


def _warn_if_multiprocess():
    """检测多 worker 误配置。

    当前架构为单进程模型：任务事件队列（SSE/WS 推送）、Brain 内存 checkpointer、
    KB updater 均为进程内单例。若用 uvicorn --workers N>1 或 gunicorn 多 worker 启动，
    会导致：任务在 A 进程跑、客户端 SSE 连到 B 进程收不到推送；resume 找不到 checkpointer。
    这里检测并告警（不阻断，便于开发期单机调试）。
    """
    try:
        # uvicorn --workers 会设置 WEB_CONCURRENCY
        web_concurrency = os.environ.get("WEB_CONCURRENCY")
        if web_concurrency and int(web_concurrency) > 1:
            logger.warning(
                "⚠️  检测到 WEB_CONCURRENCY=%s（多 worker）。当前架构为单进程模型，"
                "多 worker 会导致 SSE/WS 推送错乱与 resume 失败。"
                "多副本部署需先将任务队列/checkpointer 外置到 Redis/PG（见 README Roadmap）。",
                web_concurrency,
            )
    except Exception:
        pass


async def _start_task_scheduler() -> None:
    """任务准入调度器（优先级队列 + 有界并发）。"""
    try:
        from swarm.brain.scheduler import start_task_scheduler

        await start_task_scheduler()
    except Exception as exc:
        logger.warning("Failed to start task scheduler: %s", exc)


@app.on_event("shutdown")
async def on_shutdown():
    """应用关闭钩子：优雅关闭数据库连接池。"""
    try:
        from swarm.infra.db import close_async_pools, close_sync_pools

        await close_async_pools()
        close_sync_pools()
        logger.info("DB pools closed")
    except Exception as exc:
        logger.warning("Failed to close DB pools: %s", exc)


async def _start_kb_update_scheduler() -> None:
    """PG kb_update_events 队列后台消费（P0）。"""
    try:
        from swarm.knowledge.scheduler import start_kb_update_scheduler

        await start_kb_update_scheduler(interval_seconds=5)
    except Exception as exc:
        logger.warning("Failed to start KB update scheduler: %s", exc)


async def _start_consistency_scheduler() -> None:
    """每日 ConsistencyChecker（设计文档 P1）。"""
    import datetime as dt

    from swarm.knowledge.consistency import run_daily_consistency_all_projects

    async def _loop() -> None:
        while True:
            now = dt.datetime.now()
            target = now.replace(hour=4, minute=0, second=0, microsecond=0)
            if target <= now:
                target += dt.timedelta(days=1)
            await asyncio.sleep(max(60.0, (target - now).total_seconds()))
            try:
                await run_daily_consistency_all_projects(repair=True)
                await _sync_mr_history_all_projects()
            except Exception as exc:
                logger.warning("ConsistencyChecker daily run failed: %s", exc)

    try:
        asyncio.create_task(_loop())
        logger.info("Knowledge ConsistencyChecker scheduled (daily ~04:00)")
    except Exception as exc:
        logger.warning("Failed to start ConsistencyChecker: %s", exc)


async def _sync_mr_history_all_projects() -> None:
    """每日同步 GitLab MR 历史到 Layer D。"""
    import psycopg

    from swarm.config.settings import get_config
    from swarm.knowledge.mr_history import MR_HISTORY_DDL, sync_mr_history_from_gitlab
    from swarm.project import store

    cfg = get_config()

    async def _get_conn():
        conn = await psycopg.AsyncConnection.connect(cfg.db.postgres_uri, autocommit=True)
        async with conn.cursor() as cur:
            await cur.execute(MR_HISTORY_DDL)
        return conn

    try:
        projects = store.list_projects()
    except Exception as exc:
        logger.warning("[MR history] list projects failed: %s", exc)
        return

    for p in projects:
        pid = p.get("id")
        if not pid:
            continue
        try:
            count = await sync_mr_history_from_gitlab(_get_conn, pid, limit=50)
            if count:
                logger.info("[MR history] project=%s synced=%d", pid, count)
        except Exception as exc:
            logger.warning("[MR history] project=%s failed: %s", pid, exc)


async def _start_memory_decay_scheduler() -> None:
    """启动 L5 错题集每日衰减调度；PG 不可用时仅记录警告"""
    try:
        from swarm.memory.decay import MemoryDecay
        from swarm.memory.store import MemoryStore

        mem_store = MemoryStore()
        await mem_store.connect()
        decay = MemoryDecay(mem_store)
        asyncio.create_task(decay.start_daily_decay())
        logger.info("L5 memory decay scheduler started (daily at 03:00)")
    except Exception as exc:
        logger.warning("Failed to start L5 memory decay scheduler: %s", exc)


# ─── 静态文件 ──────────────────────────────────────

_static_dir = Path(__file__).parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


# ═══════════════════════════════════════════════════
# 端点实现
# ═══════════════════════════════════════════════════


# ─── 1. GET /api/health ────────────────────────────
@app.get("/api/health", tags=["系统"])
async def health_check():
    """健康检查"""
    return {"status": "ok", "timestamp": time.time()}


# ─── Auth / RBAC ───────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""
    global_role: str = "developer"


class MemberRequest(BaseModel):
    user_id: str
    role: str = "developer"






@app.post("/api/auth/login", tags=["认证"])
async def auth_login(req: LoginRequest):
    """用户名密码登录，返回 api_token（Bearer / X-Swarm-Token）。"""
    from swarm.auth.store import authenticate

    loop = asyncio.get_running_loop()
    user = await loop.run_in_executor(None, lambda: authenticate(req.username, req.password))
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return {
        "token": user.api_token,
        "user": {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "global_role": user.global_role,
        },
    }


@app.get("/api/auth/me", tags=["认证"])
async def auth_me(request: Request):
    user = _require_user(request)
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "global_role": user.global_role,
    }


@app.get("/api/users", tags=["认证"])
async def list_users_api(request: Request):
    _require_perm(request, "config:write")
    from swarm.auth.store import list_users

    loop = asyncio.get_running_loop()
    return {"users": await loop.run_in_executor(None, list_users)}


@app.post("/api/users", tags=["认证"])
async def create_user_api(request: Request, req: CreateUserRequest):
    _require_perm(request, "config:write")
    from swarm.auth.store import create_user

    loop = asyncio.get_running_loop()

    def _create():
        try:
            return create_user(
                username=req.username,
                password=req.password,
                display_name=req.display_name or None,
                global_role=req.global_role,
            )
        except Exception as exc:
            if "unique" in str(exc).lower():
                raise HTTPException(status_code=409, detail="Username already exists") from exc
            raise

    user = await loop.run_in_executor(None, _create)
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "global_role": user.global_role,
        "token": user.api_token,
    }


@app.get("/api/projects/{project_id}/members", tags=["认证"])
async def list_members_api(project_id: str, request: Request):
    _require_perm(request, "project:read", project_id)
    from swarm.auth.store import list_project_members

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _validate_project, project_id)
    members = await loop.run_in_executor(None, lambda: list_project_members(project_id))
    return {"members": members}


@app.put("/api/projects/{project_id}/members", tags=["认证"])
async def set_member_api(project_id: str, req: MemberRequest, request: Request):
    _require_perm(request, "member:manage", project_id)
    from swarm.auth.store import set_project_member

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _validate_project, project_id)
    await loop.run_in_executor(
        None,
        lambda: set_project_member(project_id, req.user_id, req.role),
    )
    return {"project_id": project_id, "user_id": req.user_id, "role": req.role}


# ─── 2. GET /api/status ────────────────────────────
@app.get("/api/status", tags=["系统"])
async def get_status():
    """系统组件运行状态（8 个组件）"""
    components = ["Brain 状态机", "Worker 执行器", "知识库", "记忆系统", "远程沙箱", "模型路由", "PostgreSQL", "Qdrant"]
    results = await asyncio.gather(*[_check_component(c) for c in components])
    overall = "running"
    for r in results:
        if r["status"] == "error":
            overall = "error"
            break
        if r["status"] in ("degraded", "unknown"):
            overall = "degraded"
    return {"overall": overall, "components": results}


# ─── 3. GET /api/config ────────────────────────────


@app.get("/api/config", tags=["配置"])
async def get_config_endpoint():
    """返回当前配置（脱敏 API Key）"""
    cfg = get_config()
    raw = cfg.model_dump()
    masked = _mask_config_dict(raw)
    flat = _mask_config_dict(_flatten_model_config(cfg))
    from swarm.tracing import langsmith_status

    return {"config": masked, "flat": flat, "langsmith": langsmith_status()}


# ─── 3.5 GET /api/models ─────────────────────────
@app.get("/api/models", tags=["配置"])
async def list_models():
    """从 SiliconFlow 和本地 API 拉取可用模型列表"""
    import httpx

    result = {"siliconflow": [], "local": []}

    # SiliconFlow 模型列表
    cfg = get_config()
    sf_key = cfg.model.siliconflow_api_key
    if sf_key:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{cfg.model.siliconflow_base_url}/models",
                    headers={"Authorization": f"Bearer {sf_key}"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    models = data.get("data", data.get("models", []))
                    result["siliconflow"] = sorted(
                        [m.get("id", m.get("name", "")) for m in models if m.get("id") or m.get("name")]
                    )
        except Exception as e:
            logger.warning(f"Failed to fetch SiliconFlow models: {e}")
            result["siliconflow_error"] = str(e)

    # 本地模型列表 (支持 OpenAI 兼容 / Open WebUI / Ollama)
    local_url = cfg.model.local_base_url
    local_key = cfg.model.local_api_key
    if local_url:
        try:
            headers = {}
            if local_key:
                headers["Authorization"] = f"Bearer {local_key}"
            base = local_url.rstrip("/").removesuffix("/v1").removesuffix("/api")
            async with httpx.AsyncClient(timeout=10, verify=False) as client:
                # 尝试多种端点：OpenAI /v1/models → Open WebUI /api/models → Ollama /api/tags
                models = []
                for endpoint in [f"{base}/v1/models", f"{base}/api/models", f"{base}/api/tags"]:
                    try:
                        resp = await client.get(endpoint, headers=headers)
                        if resp.status_code == 200:
                            data = resp.json()
                            # OpenAI 格式: {"data": [{"id": "..."}]}
                            # Ollama 格式: {"models": [{"name": "..."}]}
                            raw = data.get("data", data.get("models", []))
                            models = [m.get("id", m.get("name", "")) for m in raw if m.get("id") or m.get("name")]
                            if models:
                                break
                        elif resp.status_code == 401:
                            result["local_error"] = "认证失败：请配置本地 API Key"
                            break
                    except Exception:
                        continue
                if models:
                    result["local"] = sorted(models)
        except Exception as e:
            logger.warning(f"Failed to fetch local models: {e}")
            result["local_error"] = str(e)

    return result


@app.post("/api/config/test", tags=["配置"])
async def test_config():
    """测试 Brain / 本地 Worker / 云端 Worker 模型是否可调用"""
    import asyncio

    cfg = get_config()
    from swarm.models.router import ModelRouter

    router = ModelRouter()
    results: dict[str, Any] = {}

    def _probe(label: str, llm_factory) -> dict[str, Any]:
        try:
            llm = llm_factory()
            resp = llm.invoke([{"role": "user", "content": "Reply with exactly: OK"}])
            content = resp.content
            if isinstance(content, list):
                content = " ".join(
                    p if isinstance(p, str) else p.get("text", "")
                    for p in content
                )
            preview = str(content).strip()[:120]
            return {"ok": True, "preview": preview}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    results["brain_primary"] = {
        "model": cfg.model.brain_primary,
        **await asyncio.to_thread(
            _probe, "brain", router.get_brain_llm,
        ),
    }
    results["worker_local_medium"] = {
        "model": cfg.model.routing_medium,
        **await asyncio.to_thread(
            lambda: _probe("medium", lambda: router.get_llm_for_subtask("medium", "text")),
        ),
    }
    results["worker_cloud_complex"] = {
        "model": cfg.model.routing_complex,
        **await asyncio.to_thread(
            lambda: _probe("complex", lambda: router.get_llm_for_subtask("complex", "text")),
        ),
    }
    results["all_ok"] = all(
        results[k].get("ok") for k in (
            "brain_primary", "worker_local_medium", "worker_cloud_complex"
        )
    )
    return results


# ─── 4. PUT /api/config ────────────────────────────
@app.put("/api/config", tags=["配置"])
async def update_config(request: Request):
    """更新配置 — 写入 .env 文件并重新加载

    接收格式:
    - {"config": {"siliconflow_api_key": "...", ...}}
    - {"siliconflow_api_key": "...", ...}
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    raw_items = body.get("config", body) if isinstance(body.get("config"), dict) else body

    env_path = _PROJECT_ROOT / ".env"

    # 读取现有 .env 内容
    existing_lines: list[str] = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    # 构建更新映射（支持短名自动转换）
    update_map: dict[str, str] = {}
    for key, value in raw_items.items():
        if not isinstance(key, str) or not isinstance(value, (str, int, float, bool)):
            continue
        str_val = str(value).strip()
        # 跳过空值 — 前端不输入 Key 时不应该覆盖已有 Key 为空
        if not str_val:
            continue
        # 跳过脱敏值（包含 *** 或 ... 的值是前端回传的脱敏显示值）
        if "***" in str_val or (str_val.count("...") == 1 and len(str_val) < 30):
            continue
        env_key = _resolve_key(key.strip())
        update_map[env_key] = str_val

    if not update_map:
        # 所有值都是脱敏值或未修改 — 不算错误
        cfg = get_config()
        raw = cfg.model_dump()
        masked = _mask_config_dict(raw)
        return {"status": "no_changes", "message": "未检测到有效变更（脱敏值已忽略）", "config": masked}

    # 更新或追加行
    updated_keys: set[str] = set()
    new_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, _ = stripped.partition("=")
            k = k.strip().upper()
            if k in update_map:
                new_lines.append(f"{k}={update_map[k]}")
                updated_keys.add(k)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    # 追加新键
    for k, v in update_map.items():
        if k not in updated_keys:
            new_lines.append(f"{k}={v}")

    # 写回 .env
    content = "\n".join(new_lines) + "\n"
    env_path.write_text(content, encoding="utf-8")

    # 同步更新 os.environ（让 reload_config 新建的 BaseSettings 能读到新值）
    for k, v in update_map.items():
        os.environ[k] = v
    logger.info(f"Updated .env + os.environ with keys: {list(update_map.keys())}")

    # 重新加载配置
    reload_config()
    configure_langsmith(reload=True)
    try:
        from swarm.worker.sandbox import reset_sandbox_manager
        reset_sandbox_manager()
    except Exception as exc:
        logger.warning("Failed to reset sandbox manager after config reload: %s", exc)
    logger.info("Config reloaded")

    cfg = get_config()
    raw = cfg.model_dump()
    masked = _mask_config_dict(raw)

    return {
        "status": "ok",
        "updated_keys": list(update_map.keys()),
        "config": masked,
    }


# ─── 4.5 GET /api/routing ────────────────────────────
@app.get("/api/routing", tags=["配置"])
async def get_routing():
    """获取当前模型路由表"""
    from swarm.models.router import ModelRouter
    router = ModelRouter()
    return router.get_routing_table()


# ─── 4.6 PUT /api/routing ────────────────────────────
@app.put("/api/routing", tags=["配置"])
async def update_routing(request: Request):
    """更新模型路由表配置 — 写入 .env 并重载"""
    body = await request.json()

    # 兼容前端旧格式 { routes: { tiers: "..." } }
    if isinstance(body.get("routes"), dict):
        body = {**body, **body["routes"]}
    tiers = body.get("tiers")
    if isinstance(tiers, str):
        import json as _json
        try:
            tiers = _json.loads(tiers)
        except _json.JSONDecodeError:
            tiers = None
    if isinstance(tiers, dict):
        for tier_name, tier_cfg in tiers.items():
            if not isinstance(tier_cfg, dict):
                continue
            body[f"routing_{tier_name}"] = tier_cfg.get("primary", "")
            body[f"routing_{tier_name}_fallback"] = tier_cfg.get("fallback", "")

    # 短名 → 环境变量名映射
    mapping = {
        'brain_primary': 'SWARM_MODEL_BRAIN_PRIMARY',
        'brain_fallback': 'SWARM_MODEL_BRAIN_FALLBACK',
        'routing_trivial': 'SWARM_MODEL_ROUTING_TRIVIAL',
        'routing_trivial_fallback': 'SWARM_MODEL_ROUTING_TRIVIAL_FALLBACK',
        'routing_medium': 'SWARM_MODEL_ROUTING_MEDIUM',
        'routing_medium_fallback': 'SWARM_MODEL_ROUTING_MEDIUM_FALLBACK',
        'routing_complex': 'SWARM_MODEL_ROUTING_COMPLEX',
        'routing_complex_fallback': 'SWARM_MODEL_ROUTING_COMPLEX_FALLBACK',
        'routing_multimodal': 'SWARM_MODEL_ROUTING_MULTIMODAL',
        'routing_multimodal_fallback': 'SWARM_MODEL_ROUTING_MULTIMODAL_FALLBACK',
    }

    # 构建 update_map（同 PUT /api/config 逻辑）
    update_map: dict[str, str] = {}
    for key, env_key in mapping.items():
        if key in body:
            val = str(body[key]).strip()
            if val:
                update_map[env_key] = val

    if not update_map:
        from swarm.models.router import ModelRouter
        router = ModelRouter()
        return {"status": "no_changes", "message": "未检测到有效变更", **router.get_routing_table()}

    # 更新 os.environ
    for k, v in update_map.items():
        os.environ[k] = v

    # 写入 .env 文件
    env_path = _PROJECT_ROOT / ".env"
    existing_lines: list[str] = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    updated_keys: set[str] = set()
    new_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, _ = stripped.partition("=")
            k = k.strip().upper()
            if k in update_map:
                new_lines.append(f"{k}={update_map[k]}")
                updated_keys.add(k)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    for k, v in update_map.items():
        if k not in updated_keys:
            new_lines.append(f"{k}={v}")

    content = "\n".join(new_lines) + "\n"
    env_path.write_text(content, encoding="utf-8")

    logger.info(f"Updated routing .env + os.environ with keys: {list(update_map.keys())}")

    # 重新加载配置
    reload_config()
    logger.info("Config reloaded after routing update")

    from swarm.models.router import ModelRouter
    router = ModelRouter()
    return {
        "status": "ok",
        "updated_keys": list(update_map.keys()),
        **router.get_routing_table(),
    }


# ─── 5. POST /api/demo ─────────────────────────────
@app.post("/api/demo", tags=["Demo"])
async def run_demo(req: DemoRequest):
    """触发 swarm demo 任务（后台执行，返回 run_id 用于 SSE 订阅）

    优先走 Brain 编排链路（analyze → plan → dispatch → worker → sandbox），
    如果 Brain 不可用则降级为沙箱直接执行。

    返回 {"run_id": "run-xxxx", "status": "started"}，
    前端通过 GET /api/demo/stream?run_id=run-xxxx 订阅进度。
    """
    task = req.task or "计算斐波那契数列前10项"
    project_id = getattr(req, "project_id", None) or None

    run_id, queue = _new_demo_run()

    async def _run_in_background() -> None:
        """后台执行 Brain 编排，将进度推入 queue"""
        sandbox: Any | None = None
        loop = asyncio.get_running_loop()

        # 如果指定了 project_id，设置 workspace root 到项目路径
        if project_id:
            try:
                project = store.get_project(project_id)
                if project and project.get("path"):
                    os.environ["SWARM_WORKSPACE_ROOT"] = project["path"]
                    logger.info(f"[Demo] workspace root → {project['path']}")
            except Exception as e:
                logger.warning(f"[Demo] 设置 workspace root 失败: {e}")

        # ── 尝试 Brain 编排流 ──
        try:
            from swarm.brain.graph import compile_brain_graph

            await queue.put({"step": "brain_init", "status": "running",
                             "message": "🧠 Brain 编排模式启动…", "mode": "brain", "progress": 5})

            compiled = compile_brain_graph()
            thread_id = f"demo-{run_id}"

            from swarm.tracing import brain_graph_config

            graph_config = brain_graph_config(
                task_id=run_id,
                project_id=project_id or "",
                thread_id=thread_id,
                resume=False,
                description=task[:200],
            )

            await queue.put({"step": "brain_compile", "status": "done",
                             "message": "Brain graph 已编译", "mode": "brain", "progress": 10})

            await queue.put({"step": "brain_invoke", "status": "running",
                             "message": f"Brain 正在编排任务 (thread={thread_id})…",
                             "mode": "brain", "progress": 20})

            invoke_input = {"task": task, "task_description": task, "auto_accept": True}
            if project_id:
                invoke_input["project_id"] = project_id

            final_state = None
            progress_val = 20
            async for event in compiled.astream_events(
                invoke_input,
                config=graph_config,
                version="v2",
            ):
                kind = event.get("event", "")
                if kind == "on_chain_start":
                    name = event.get("name", "")
                    if name and name not in ("LangGraph", "ChannelWrite"):
                        progress_val = min(progress_val + 5, 85)
                        await queue.put({"step": "brain_node", "status": "running",
                                         "message": f"🧠 Brain 节点: {name}",
                                         "mode": "brain", "node": name, "progress": progress_val})
                elif kind == "on_chain_end":
                    name = event.get("name", "")
                    output = event.get("data", {}).get("output")
                    if name == "LangGraph" and output is not None:
                        final_state = output

            await queue.put({"step": "brain_done", "status": "done",
                             "message": "🧠 Brain 编排完成", "mode": "brain", "progress": 95})

            # 提取输出
            output_parts = {}
            if isinstance(final_state, dict):
                for k, v in final_state.items():
                    if k in ("merged_diff", "l2_passed", "learn_summary",
                             "complexity", "plan", "subtask_results") and v:
                        if hasattr(v, "model_dump"):
                            output_parts[k] = v.model_dump(mode="json")
                        elif isinstance(v, dict):
                            output_parts[k] = v
                        else:
                            output_parts[k] = str(v)
            if not output_parts and final_state is not None:
                output_parts = {"raw": str(final_state)[:2000]}

            await queue.put({"step": "complete", "status": "done",
                             "message": "Demo 执行完成", "mode": "brain", "progress": 100})
            await queue.put({"step": "result", "mode": "brain", "result": output_parts})
            return  # Brain 成功

        except Exception as brain_err:
            logger.warning(f"Brain failed, falling back to sandbox: {brain_err}")
            await queue.put({"step": "brain_fallback", "status": "warning",
                             "message": "⚠️ Brain 不可用，降级为沙箱直接执行",
                             "mode": "sandbox_fallback", "progress": 15})

        # ── 降级：沙箱直接执行 ──
        manager = _get_sandbox_manager()
        try:
            await queue.put({"step": "create_sandbox", "status": "running",
                             "message": "正在创建远程 CubeSandbox…",
                             "mode": "sandbox_fallback", "progress": 20})

            sandbox = await loop.run_in_executor(
                None, lambda: manager.create(
                    template_id="tpl-8fa882f5d775429cad1530c9", timeout=120,
                )
            )
            sandbox_id = sandbox.sandbox_id

            await queue.put({"step": "create_sandbox", "status": "done",
                             "message": f"沙箱已创建: {sandbox_id}",
                             "mode": "sandbox_fallback", "progress": 40,
                             "sandbox_id": sandbox_id})

            code = req.code or '''
import json
def fibonacci(n):
    a, b = 0, 1
    result = []
    for _ in range(n):
        result.append(a)
        a, b = b, a + b
    return result
result = fibonacci(10)
print(f"Fibonacci sequence: {result}")
print(f"Sum: {sum(result)}")
'''

            await queue.put({"step": "execute_code", "status": "running",
                             "message": "正在沙箱中执行代码…",
                             "mode": "sandbox_fallback", "progress": 60})

            result = await loop.run_in_executor(
                None, lambda: manager.run_code(sandbox, code, timeout=60)
            )

            await queue.put({"step": "execute_code", "status": "done",
                             "message": "代码执行完成", "mode": "sandbox_fallback",
                             "progress": 80, "output": {
                                 "stdout": result.stdout, "stderr": result.stderr,
                                 "text": result.text, "error": result.error,
                                 "success": result.success,
                             }})

            # 销毁沙箱
            await queue.put({"step": "destroy_sandbox", "status": "running",
                             "message": f"正在销毁沙箱 {sandbox_id}…",
                             "mode": "sandbox_fallback", "progress": 90})

            await loop.run_in_executor(None, lambda: manager.kill(sandbox_id))
            sandbox = None

            await queue.put({"step": "complete", "status": "done",
                             "message": "Demo 执行完成",
                             "mode": "sandbox_fallback", "progress": 100})
            await queue.put({"step": "result", "mode": "sandbox_fallback",
                             "success": result.success, "stdout": result.stdout,
                             "stderr": result.stderr, "text": result.text,
                             "error": result.error})

        except Exception as e:
            logger.error(f"Demo failed: {e}\n{traceback.format_exc()}")
            await queue.put({"step": "error", "status": "error",
                             "message": f"执行失败: {str(e)}", "progress": -1})
        finally:
            if sandbox is not None:
                try:
                    manager.kill(sandbox.sandbox_id)
                except Exception:
                    pass

    # 在后台启动执行，立即返回 run_id
    asyncio.create_task(_run_in_background())

    return {"run_id": run_id, "status": "started", "task": task}


# ─── 6. GET /api/demo/stream ───────────────────────
@app.get("/api/demo/stream", tags=["Demo"])
async def stream_demo(run_id: str = ""):
    """SSE 流式推送 demo 执行进度（纯订阅，不触发任务）

    必须提供 run_id 参数（由 POST /api/demo 返回），
    否则返回 400 错误。这修复了之前 SSE 连接自动触发 Brain 任务
    导致 GPU 无限占用的 bug。
    """
    if not run_id or run_id not in _demo_runs:
        raise HTTPException(
            status_code=400,
            detail="无效或缺失 run_id。请先 POST /api/demo 获取 run_id，"
                   "再通过 ?run_id=xxx 订阅进度。",
        )

    queue = _demo_runs[run_id]

    async def event_generator():
        """从 queue 中读取进度事件并推送"""
        try:
            while True:
                try:
                    # 等待新事件，超时 30s 发心跳
                    event_data = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    # 心跳：防止连接超时断开
                    yield {"event": "heartbeat", "data": ""}
                    continue

                step = event_data.get("step", "")
                event_type = "progress"

                if step == "result":
                    event_type = "result"
                elif step == "error":
                    event_type = "error"

                yield {
                    "event": event_type,
                    "data": json.dumps(event_data, ensure_ascii=False),
                }

                # 终止事件：complete 或 error
                if step in ("complete", "error"):
                    # 完成后清理注册
                    _demo_runs.pop(run_id, None)
                    break
        except asyncio.CancelledError:
            # 客户端断开连接
            pass

    return EventSourceResponse(event_generator())


# ─── 辅助: 从 CubeSandbox 服务端拉取沙箱列表 ────────
def _fetch_sandbox_list_from_server() -> list[dict]:
    """调用 Sandbox.list() 获取服务端权威沙箱列表
    
    返回包含 id/state/started_at/template_id/cpu/memory 的字典列表。
    started_at 从 UTC 转换为本地时区显示。
    """
    try:
        from e2b_code_interpreter import Sandbox as _Sandbox
        paginator = _Sandbox.list()
        items = paginator.next_items()
        seen_ids = set()
        result = []
        for sb in items:
            sid = sb.sandbox_id
            if sid in seen_ids:
                continue  # 去重（CubeSandbox API 可能返回重复条目）
            seen_ids.add(sid)
            # UTC → 本地时间
            started_local = "-"
            if sb.started_at:
                try:
                    utc_dt = sb.started_at
                    if hasattr(utc_dt, "astimezone"):
                        local_dt = utc_dt.astimezone()
                        started_local = local_dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    started_local = str(sb.started_at)[:19]
            result.append({
                "id": sid,
                "status": sb.state or "unknown",
                "started_at": started_local,
                "template_id": sb.template_id or "-",
                "cpu_count": sb.cpu_count,
                "memory_mb": sb.memory_mb,
            })
        return result
    except Exception as e:
        logger.warning(f"Failed to fetch sandbox list from server: {e}")
        return []


# ─── 7. GET /api/sandbox/status ────────────────────
@app.get("/api/sandbox/status", tags=["沙箱"])
async def sandbox_status(project_id: str | None = None):
    """活跃沙箱列表（可按 project_id 过滤，仅显示本项目注册/创建的沙箱）"""
    loop = asyncio.get_running_loop()
    sandboxes = await loop.run_in_executor(None, _fetch_sandbox_list_from_server)
    manager = _get_sandbox_manager()
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
            "api_url": get_config().sandbox.api_url,
            "proxy_base": get_config().sandbox.proxy_base,
            "default_template": get_config().sandbox.default_template,
            "use_for_worker": get_config().sandbox.use_for_worker,
        },
    }


# ─── 8. POST /api/sandbox/create ───────────────────
@app.post("/api/sandbox/create", tags=["沙箱"])
async def create_sandbox(req: SandboxCreateRequest):
    """创建新沙箱"""
    manager = _get_sandbox_manager()
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
        logger.error(f"Failed to create sandbox: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create sandbox: {str(e)}")


# ─── 9. DELETE /api/sandbox/{sandbox_id} ───────────
@app.delete("/api/sandbox/{sandbox_id}", tags=["沙箱"])
async def destroy_sandbox(sandbox_id: str):
    """销毁沙箱"""
    manager = _get_sandbox_manager()
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
        logger.error(f"Failed to destroy sandbox {sandbox_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to destroy sandbox: {str(e)}")


# ─── 10. GET /api/sandbox/{sandbox_id}/files ───────
@app.get("/api/sandbox/{sandbox_id}/files", tags=["沙箱"])
async def sandbox_files(sandbox_id: str, path: str = "/"):
    """获取沙箱内目录列表（CubeProxy 经 dev_sidecar 转发）"""
    manager = _get_sandbox_manager()
    try:
        loop = asyncio.get_running_loop()
        files = await loop.run_in_executor(
            None, lambda: manager.list_files(sandbox_id, path=path or "/"),
        )
        proxy = get_config().sandbox.proxy_base
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
        logger.error("Failed to list files in sandbox %s: %s", sandbox_id, e)
        proxy = get_config().sandbox.proxy_base
        raise HTTPException(
            status_code=502,
            detail=(
                f"无法访问沙箱文件系统: {e}. "
                f"请确认 CubeProxy 可达 (SWARM_SANDBOX_PROXY_BASE={proxy})，"
                "且 dev_sidecar 未被错误指向 127.0.0.1:11443。"
            ),
        )


# ─── 11. GET /api/sandbox/{sandbox_id}/files/content ─
@app.get("/api/sandbox/{sandbox_id}/files/content", tags=["沙箱"])
async def sandbox_file_content(sandbox_id: str, path: str):
    """读取沙箱内单个文件内容（CubeProxy 经 dev_sidecar 转发）"""
    if not path or not path.startswith("/"):
        raise HTTPException(status_code=400, detail="path 必须为沙箱内绝对路径，如 /workspace/foo.py")
    manager = _get_sandbox_manager()
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
        logger.error("Failed to read file in sandbox %s path=%s: %s", sandbox_id, path, e)
        raise HTTPException(status_code=502, detail=f"无法读取沙箱文件: {e}")


@app.get("/api/sandbox/{sandbox_id}/logs", tags=["沙箱"])
async def sandbox_logs(sandbox_id: str, limit: int = 200):
    """沙箱活动日志 — Worker 阶段日志 + run_code stdout/stderr"""
    manager = _get_sandbox_manager()
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


# ═══════════════════════════════════════════════════
# 项目管理端点
# ═══════════════════════════════════════════════════


# ─── 1. GET /api/projects — 项目列表 ──────────────
@app.get("/api/projects", tags=["项目管理"])
async def list_projects(request: Request):
    """返回当前用户可见的项目列表"""
    from swarm.auth.rbac import Role
    from swarm.auth.store import list_user_project_ids

    user = _require_user(request)
    loop = asyncio.get_running_loop()
    try:
        all_projects = await loop.run_in_executor(None, store.list_projects)
    except Exception as e:
        logger.warning(f"PG unavailable for list_projects: {e}")
        all_projects = []
    if user.global_role != Role.ADMIN.value:
        allowed = list_user_project_ids(user.id)
        all_projects = [p for p in all_projects if p.get("id") in allowed]
    return {"projects": all_projects}


# ─── 2. POST /api/projects — 创建项目 ─────────────
@app.post("/api/projects", tags=["项目管理"])
async def create_project(req: ProjectCreateRequest, request: Request):
    """创建项目并自动启动预处理

    项目状态从 EMPTY → PREPROCESSING → READY
    """
    from swarm.auth.rbac import Role
    from swarm.auth.store import set_project_member

    user = _require_perm(request, "project:create")
    project_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()

    # 创建项目记录
    try:
        project = await loop.run_in_executor(
            None,
            lambda: store.create_project(
                project_id=project_id,
                name=req.name,
                path=req.path,
                description=req.description,
            ),
        )
        if user.global_role != Role.ADMIN.value:
            await loop.run_in_executor(
                None,
                lambda: set_project_member(project_id, user.id, Role.OWNER.value),
            )
    except Exception as e:
        logger.error(f"Failed to create project: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create project: {str(e)}")

    # 后台启动预处理（不阻塞响应）
    async def _run_preprocess():
        try:
            await preprocess.preprocess_project(project_id, req.path)
        except Exception as e:
            logger.error(f"Preprocessing failed for project {project_id}: {e}")

    asyncio.create_task(_run_preprocess())

    return {"status": "ok", "project": project}


# ─── 3. GET /api/projects/{project_id} — 项目详情 ─
@app.get("/api/projects/{project_id}", tags=["项目管理"])
async def get_project(project_id: str, request: Request):
    """获取项目详情"""
    _require_perm(request, "project:read", project_id)
    loop = asyncio.get_running_loop()
    project = await loop.run_in_executor(None, store.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return {"project": project}


# ─── 4. DELETE /api/projects/{project_id} — 删除项目 ─
@app.delete("/api/projects/{project_id}", tags=["项目管理"])
async def delete_project(project_id: str, request: Request):
    """删除项目及其关联数据"""
    _require_perm(request, "project:delete", project_id)
    loop = asyncio.get_running_loop()
    # 先确认项目存在
    project = await loop.run_in_executor(None, store.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    deleted = await loop.run_in_executor(None, store.delete_project, project_id)
    if not deleted:
        raise HTTPException(status_code=500, detail="Failed to delete project")
    return {"status": "ok", "message": f"Project {project_id} deleted"}


# ─── 5. POST /api/projects/{project_id}/preprocess — 手动触发预处理 ─
@app.post("/api/projects/{project_id}/preprocess", tags=["项目管理"])
async def trigger_preprocess(project_id: str):
    """手动触发/重新触发项目预处理"""
    loop = asyncio.get_running_loop()
    try:
        project = await loop.run_in_executor(None, store.get_project, project_id)
    except Exception as e:
        logger.exception("Failed to load project %s for preprocess", project_id)
        raise HTTPException(status_code=503, detail=f"Database unavailable: {e}") from e

    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    project_path = project["path"]

    try:
        await loop.run_in_executor(None, store.reset_preprocess_progress, project_id)
        await loop.run_in_executor(
            None,
            lambda: store.update_project(project_id, status="PREPROCESSING"),
        )
    except Exception as e:
        logger.exception("Failed to reset preprocess state for %s", project_id)
        raise HTTPException(status_code=500, detail=f"Failed to start preprocess: {e}") from e

    # 后台启动预处理
    async def _run_preprocess():
        try:
            await preprocess.preprocess_project(project_id, project_path)
        except Exception:
            logger.exception("Preprocessing failed for project %s", project_id)

    asyncio.create_task(_run_preprocess())
    logger.info("Preprocess queued for project %s path=%s", project_id, project_path)

    return {"status": "ok", "message": f"Preprocessing started for project {project_id}"}


# ─── 6b. GET /api/projects/{project_id}/preprocess/status — 预处理状态快照 ─
@app.get("/api/projects/{project_id}/preprocess/status", tags=["项目管理"])
async def get_preprocess_status(project_id: str):
    """返回当前预处理进度（非 SSE，供 Tab 打开时加载）"""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _validate_project, project_id)
    progress = await loop.run_in_executor(None, store.get_progress, project_id)
    project = await loop.run_in_executor(None, store.get_project, project_id)
    return {
        "project_status": project.get("status") if project else None,
        "progress": progress,
    }


# ─── 6. GET /api/projects/{project_id}/preprocess/progress — SSE 预处理进度流 ─
@app.get("/api/projects/{project_id}/preprocess/progress", tags=["项目管理"])
async def stream_preprocess_progress(project_id: str):
    """SSE 流式推送项目预处理进度

    事件格式: event: progress, data: {phase, phase_progress, message, ...}
    当 phase 为 complete 或 error 时发送后关闭流。
    """

    async def event_generator():
        last_phase = None
        last_progress = -1.0
        idle_count = 0

        while True:
            loop = asyncio.get_running_loop()
            progress = await loop.run_in_executor(None, store.get_progress, project_id)

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


# ═══════════════════════════════════════════════════
# 任务管理端点
# ═══════════════════════════════════════════════════


# ─── 7. GET /api/projects/{project_id}/tasks — 项目任务列表 ─
@app.get("/api/projects/{project_id}/tasks", tags=["任务管理"])
async def list_tasks(project_id: str):
    """获取项目下的所有任务"""
    loop = asyncio.get_running_loop()
    # 确认项目存在
    project = await loop.run_in_executor(None, store.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    tasks = await loop.run_in_executor(None, store.list_tasks, project_id)
    return {"tasks": tasks}


# ─── 8. POST /api/projects/{project_id}/tasks — 创建任务 ─
@app.post("/api/projects/{project_id}/tasks", tags=["任务管理"])
async def create_task(project_id: str, req: TaskCreateRequest, request: Request):
    """创建任务并后台启动 Brain 编排"""
    user = _require_perm(request, "task:create", project_id)
    loop = asyncio.get_running_loop()
    project = await loop.run_in_executor(None, store.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    from swarm.knowledge.readiness import brain_task_ready

    progress = await loop.run_in_executor(None, store.get_progress, project_id)
    ready, reason = brain_task_ready(project, progress)
    if not ready:
        raise HTTPException(
            status_code=409,
            detail=reason or "项目知识库未就绪，请先完成预处理",
        )

    task_id = str(uuid.uuid4())
    try:
        task = await loop.run_in_executor(
            None,
            lambda: store.create_task(
                task_id=task_id,
                project_id=project_id,
                description=req.description,
                created_by_user_id=user.id,
            ),
        )
        await loop.run_in_executor(
            None,
            lambda: store.update_task(task_id, status="SUBMITTED", thread_id=task_id),
        )
    except Exception as e:
        logger.error(f"Failed to create task: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create task: {str(e)}")

    from swarm.brain.scheduler import submit_task

    # 入优先级队列，由准入调度器按并发上限执行（urgent>normal>background）
    priority = getattr(req, "priority", "normal") or "normal"
    submit_task(
        task_id, project_id, req.description,
        auto_accept=req.auto_accept, priority=priority,
    )
    task = await loop.run_in_executor(None, store.get_task, task_id)
    return {"status": "ok", "task": task}


# ─── Phase 0: POST /api/projects/{project_id}/worker/run ───
@app.post("/api/projects/{project_id}/worker/run", tags=["Worker"])
async def start_worker_run(project_id: str, req: WorkerRunRequest):
    """单 Worker 直跑（不经 Brain），用于 Phase 0 验证 scope + L1 + diff"""
    loop = asyncio.get_running_loop()
    project = await loop.run_in_executor(None, store.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    run_id = str(uuid.uuid4())
    from swarm.worker.runner import start_standalone_worker_background

    start_standalone_worker_background(
        run_id,
        project_id,
        req.description,
        difficulty=req.difficulty,
        writable=req.writable,
        readable=req.readable,
    )
    return {"status": "ok", "run_id": run_id, "project_id": project_id}


# ─── Phase 0: GET /api/worker/{run_id}/stream ───
@app.get("/api/worker/{run_id}/stream", tags=["Worker"])
async def stream_worker_run(run_id: str):
    """SSE 订阅 Standalone Worker 进度"""
    from swarm.worker.runner import get_worker_queue, register_worker_queue

    queue = get_worker_queue(run_id) or register_worker_queue(run_id)

    async def event_generator():
        try:
            while True:
                try:
                    event_data = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat", "data": ""}
                    continue

                step = event_data.get("step", "")
                event_type = "progress"
                if step == "result":
                    event_type = "result"
                elif step == "error":
                    event_type = "error"

                yield {
                    "event": event_type,
                    "data": json.dumps(event_data, ensure_ascii=False, default=str),
                }
                if step in ("complete", "error"):
                    break
        except asyncio.CancelledError:
            pass

    return EventSourceResponse(event_generator())


@app.post("/api/projects/{project_id}/apply-diff", tags=["Worker"])
async def apply_project_diff(project_id: str, req: ApplyDiffRequest):
    """Phase 0/1 — 将 diff 应用到项目 git 工作区（Worker 直跑或手动 patch）"""
    if not req or not (req.diff or "").strip():
        raise HTTPException(status_code=400, detail="请求体须包含 diff 字段")
    loop = asyncio.get_running_loop()
    project = await loop.run_in_executor(None, store.get_project, project_id)
    if not project or not project.get("path"):
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    from swarm.project.diff_apply import apply_git_diff

    result = await loop.run_in_executor(
        None,
        lambda: apply_git_diff(project["path"], req.diff or "", check_only=req.check_only),
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=422,
            detail=result.get("stderr") or result.get("stdout") or "git apply 失败",
        )
    return {"status": "ok", **result}


# ─── 9. GET /api/tasks/{task_id}/stream — SSE 任务进度 ─
@app.get("/api/tasks/{task_id}/stream", tags=["任务管理"])
async def stream_task(task_id: str):
    """SSE 流式推送任务 Brain 执行进度"""
    from swarm.brain.runner import get_task_queue, register_task_queue

    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    queue = get_task_queue(task_id) or register_task_queue(task_id)

    async def event_generator():
        try:
            while True:
                try:
                    event_data = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat", "data": ""}
                    continue

                step = event_data.get("step", "")
                event_type = "progress"
                if step == "result":
                    event_type = "result"
                elif step == "error":
                    event_type = "error"
                elif step == "awaiting_review":
                    event_type = "awaiting_review"

                yield {
                    "event": event_type,
                    "data": json.dumps(event_data, ensure_ascii=False, default=str),
                }

                if step in ("complete", "error", "awaiting_review"):
                    break
        except asyncio.CancelledError:
            pass

    return EventSourceResponse(event_generator())


# ─── 9b. WS /ws/tasks/{task_id} — WebSocket 任务进度（与 SSE 并存）──
@app.websocket("/ws/tasks/{task_id}")
async def ws_task_progress(websocket: WebSocket, task_id: str):
    """WebSocket 推送任务 Brain 执行进度

    复用 SSE 的同一个 asyncio.Queue 事件源，通过 WebSocket 传输。
    消息格式: JSON {"event": "progress"|"result"|"error"|"heartbeat", "data": {...}}
    连接断开时优雅处理。
    """
    from swarm.brain.runner import get_task_queue, register_task_queue

    await websocket.accept()

    # 校验任务是否存在
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, store.get_task, task_id)
    if not task:
        await websocket.send_json({"event": "error", "data": {"detail": f"Task {task_id} not found"}})
        await websocket.close()
        return

    queue = get_task_queue(task_id) or register_task_queue(task_id)

    try:
        while True:
            try:
                event_data = await asyncio.wait_for(queue.get(), timeout=30)
            except asyncio.TimeoutError:
                # 心跳：防止连接空闲超时
                await websocket.send_json({"event": "heartbeat", "data": ""})
                continue

            step = event_data.get("step", "")
            event_type = "progress"
            if step == "result":
                event_type = "result"
            elif step == "error":
                event_type = "error"
            elif step == "awaiting_review":
                event_type = "awaiting_review"

            await websocket.send_json({
                "event": event_type,
                "data": event_data,
            })

            # 终止事件
            if step in ("complete", "error", "awaiting_review"):
                break
    except WebSocketDisconnect:
        # 客户端断开连接 — 优雅退出
        pass
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.warning("WebSocket /ws/tasks/%s 异常: %s", task_id, exc)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ─── 9. GET /api/tasks/{task_id} — 任务详情 ──────
@app.get("/api/tasks/{task_id}", tags=["任务管理"])
async def get_task(task_id: str):
    """获取任务详情"""
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return {"task": jsonable_encoder(task)}


@app.delete("/api/tasks/{task_id}", tags=["任务管理"])
async def delete_task_endpoint(task_id: str, force: bool = False):
    """删除任务；force=true 时先取消运行中任务；orphaned 活跃任务可直接删除"""
    from swarm.brain.runner import (
        _ACTIVE_DB_STATUSES,
        cancel_task,
        is_task_orphaned,
        is_task_running,
    )

    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    status = task.get("status", "")
    if is_task_running(task_id):
        if not force:
            raise HTTPException(status_code=409, detail="任务正在执行中，请使用 force=true 强制删除")
        await cancel_task(task_id)
    elif status in _ACTIVE_DB_STATUSES and not is_task_orphaned(task_id):
        if not force:
            raise HTTPException(status_code=409, detail="任务处于活跃状态，请使用 force=true 强制删除")

    deleted = await loop.run_in_executor(None, store.delete_task, task_id)
    if not deleted:
        raise HTTPException(status_code=500, detail="删除失败")
    return {"status": "ok", "message": f"任务 {task_id} 已删除"}


@app.post("/api/tasks/{task_id}/cancel", tags=["任务管理"])
async def cancel_task_endpoint(task_id: str):
    """取消运行中任务，或将 orphaned 活跃任务标记为已取消"""
    from swarm.brain.runner import cancel_task, is_task_orphaned, is_task_running

    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    if not is_task_running(task_id) and not is_task_orphaned(task_id):
        status = task.get("status", "")
        if status in ("CANCELLED", "FAILED", "DONE"):
            return {"status": "ok", "task": task, "message": "任务已结束，无需取消"}
        raise HTTPException(status_code=409, detail=f"任务状态 {status} 不可取消")

    await cancel_task(task_id)
    updated = await loop.run_in_executor(None, store.get_task, task_id)
    return {"status": "ok", "task": jsonable_encoder(updated), "message": "任务已取消"}


@app.post("/api/tasks/{task_id}/retry", tags=["任务管理"])
async def retry_task_endpoint(task_id: str, req: TaskRetryRequest | None = None):
    """重跑失败/已取消/orphaned 任务"""
    from swarm.brain.runner import can_retry_task, register_task_queue, retry_task_background

    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    allowed, reason = can_retry_task(task_id)
    if not allowed:
        raise HTTPException(status_code=409, detail=reason or "当前状态不可重跑")

    auto_accept = req.auto_accept if req else None
    register_task_queue(task_id)
    retry_task_background(task_id, auto_accept=auto_accept)
    return {"status": "ok", "task": jsonable_encoder(task), "message": "已提交重跑，Brain 重新执行"}


# ─── 10. POST /api/tasks/{task_id}/approve — 审核通过 ─
@app.post("/api/tasks/{task_id}/approve", tags=["任务管理"])
async def approve_task(task_id: str, req: ApproveTaskRequest | None = None):
    """审核通过 — 可选 apply diff + 增量知识更新，然后 resume Brain"""
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    project = await loop.run_in_executor(None, store.get_project, task["project_id"])
    merged_diff = task.get("merged_diff") or ""
    apply_diff_flag = req.apply_diff if req else False
    cfg = get_config()
    should_apply = apply_diff_flag or (
        not cfg.sandbox.sandbox_first and bool(merged_diff.strip())
    )
    apply_result: dict[str, Any] | None = None

    if should_apply and merged_diff.strip() and project and project.get("path"):
        from swarm.project.diff_apply import apply_git_diff

        apply_result = await loop.run_in_executor(
            None,
            lambda: apply_git_diff(project["path"], merged_diff, check_only=False),
        )
        if apply_diff_flag and apply_result and not apply_result.get("ok"):
            raise HTTPException(
                status_code=422,
                detail=apply_result.get("stderr") or apply_result.get("stdout") or "git apply 失败",
            )

    if merged_diff.strip() and project and project.get("path"):
        from swarm.knowledge.hooks import schedule_incremental_update

        schedule_incremental_update(
            task["project_id"],
            project["path"],
            merged_diff,
            task_id=task_id,
        )

    from swarm.brain.runner import register_task_queue, resume_task_background

    register_task_queue(task_id)
    resume_task_background(task_id, "accept")
    updated = await loop.run_in_executor(
        None,
        lambda: store.update_task(task_id, human_decision="ACCEPT"),
    )
    out: dict[str, Any] = {"status": "ok", "task": updated, "message": "已提交接受，Brain 继续执行"}
    if apply_result:
        out["apply_diff"] = apply_result

    # TODO: 在 Brain runner 任务真正完成时调用 notify（此处仅审批节点）
    from swarm.api.notify import notify
    await notify("task_approved", task_id, f"任务 {task_id} 已审核通过，Brain 继续执行")

    return out


@app.post("/api/tasks/{task_id}/apply-diff", tags=["任务管理"])
async def apply_task_diff(task_id: str, req: ApplyDiffRequest | None = None):
    """Phase 1 — 将 merged_diff 应用到项目 git 工作区（git apply）"""
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    project = await loop.run_in_executor(None, store.get_project, task["project_id"])
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
@app.post("/api/tasks/{task_id}/revise", tags=["任务管理"])
async def revise_task(task_id: str, req: TaskReviseRequest):
    """审核修订 — resume Brain (revise + feedback)"""
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    from swarm.brain.runner import register_task_queue, resume_task_background

    register_task_queue(task_id)
    resume_task_background(task_id, "revise", req.feedback)
    updated = await loop.run_in_executor(
        None,
        lambda: store.update_task(task_id, human_decision="REVISE"),
    )
    # TODO: 在 Brain runner 任务真正完成时调用 notify（此处仅审批节点）
    from swarm.api.notify import notify
    await notify("task_revised", task_id, f"任务 {task_id} 已提交修订，Brain 重新调度")
    return {"status": "ok", "task": updated, "message": "已提交修订，Brain 重新调度"}


# ─── 12. POST /api/tasks/{task_id}/reject — 审核拒绝 ─
@app.post("/api/tasks/{task_id}/reject", tags=["任务管理"])
async def reject_task(task_id: str):
    """审核拒绝 — resume Brain (reject)"""
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    from swarm.brain.runner import register_task_queue, resume_task_background

    register_task_queue(task_id)
    resume_task_background(task_id, "reject")
    updated = await loop.run_in_executor(
        None,
        lambda: store.update_task(task_id, human_decision="REJECT"),
    )
    # TODO: 在 Brain runner 任务真正完成时调用 notify（此处仅审批节点）
    from swarm.api.notify import notify
    await notify("task_rejected", task_id, f"任务 {task_id} 已拒绝，Brain 进入学习失败流程")
    return {"status": "ok", "task": updated, "message": "已拒绝，Brain 进入学习失败流程"}


# ═══════════════════════════════════════════════════
# 知识库 & 记忆 CRUD API 端点
# ═══════════════════════════════════════════════════

import psycopg as _psycopg  # noqa: E402

# ─── Pydantic Request Models ─────────────────────


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



@app.get("/api/projects/{project_id}/knowledge/overview", tags=["知识库"])
async def knowledge_overview(project_id: str):
    """项目知识库概览：预处理结果 + 索引统计"""
    import httpx

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _validate_project, project_id)

    def _query_pg() -> dict[str, Any]:
        with _get_pg_conn() as conn:
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


@app.get("/api/projects/{project_id}/knowledge/symbols", tags=["知识库"])
async def search_symbols(project_id: str, q: str, limit: int = 30):
    """Layer A — 按符号名模糊搜索"""
    from swarm.config.settings import get_config
    from swarm.knowledge.structure_index import StructureIndexer

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _validate_project, project_id)
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


@app.get("/api/projects/{project_id}/knowledge/semantic", tags=["知识库"])
async def search_semantic_chunks(project_id: str, q: str, limit: int = 20):
    """Layer B — 语义 chunk 检索（Qdrant）"""
    from swarm.config.settings import get_config
    from swarm.knowledge.semantic_index import SemanticIndexer

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _validate_project, project_id)
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


@app.post("/api/projects/{project_id}/knowledge/retrieve", tags=["知识库"])
async def knowledge_retrieve_experiment(
    project_id: str,
    req: KnowledgeRetrieveRequest,
):
    """按任务检索知识库+记忆，返回 Brain 编排将注入的 prompt 预览"""
    from swarm.knowledge.service import DEFAULT_BRAIN_LIMITS, experiment_retrieval

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _validate_project, project_id)

    limits = dict(DEFAULT_BRAIN_LIMITS)
    if req.top_k is not None:
        for k in limits:
            limits[k] = min(req.top_k, limits[k] if req.top_k >= 5 else req.top_k)

    return await experiment_retrieval(req.query.strip(), project_id, limits)


# ─── 知识库 — 规范 (kb_norms) ────────────────────


@app.get("/api/projects/{project_id}/knowledge/norms", tags=["知识库"])
async def list_norms(
    project_id: str,
    tag: str | None = None,
    active_only: bool = True,
):
    """获取项目规范列表"""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _validate_project, project_id)

    def _query():
        with _get_pg_conn() as conn:
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


@app.post("/api/projects/{project_id}/knowledge/norms", tags=["知识库"])
async def create_norm(project_id: str, req: NormCreateRequest):
    """添加项目规范"""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _validate_project, project_id)

    def _insert():
        with _get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO kb_norms (project_id, title, content, tag, priority, is_active) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, title",
                    (project_id, req.title, req.content, req.tag, req.priority, req.is_active),
                )
                row = cur.fetchone()
                return {"id": row[0], "title": row[1]}

    return await loop.run_in_executor(None, _insert)


@app.put("/api/projects/{project_id}/knowledge/norms/{norm_id}", tags=["知识库"])
async def update_norm(project_id: str, norm_id: int, req: NormUpdateRequest):
    """编辑项目规范 — 只更新提供的字段"""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _validate_project, project_id)

    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    def _do_update():
        with _get_pg_conn() as conn:
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


@app.delete("/api/projects/{project_id}/knowledge/norms/{norm_id}", tags=["知识库"])
async def delete_norm(project_id: str, norm_id: int):
    """删除项目规范"""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _validate_project, project_id)

    def _do_delete():
        with _get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM kb_norms WHERE project_id = %s AND id = %s",
                    (project_id, norm_id),
                )
                if cur.rowcount == 0:
                    raise HTTPException(status_code=404, detail=f"Norm {norm_id} not found")
        return {"deleted": True}

    return await loop.run_in_executor(None, _do_delete)


@app.get("/api/projects/{project_id}/knowledge/behavior-hotspots", tags=["知识库"])
async def list_behavior_hotspots(project_id: str, top_k: int = 20, days: int | None = None):
    """Layer D — 高频修改文件排行（行为热点）"""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _validate_project, project_id)

    cap = max(1, min(top_k, 100))

    def _query() -> list[dict[str, Any]]:
        with _get_pg_conn() as conn:
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


@app.get("/api/projects/{project_id}/knowledge/consistency", tags=["知识库"])
async def knowledge_consistency_check(project_id: str, repair: bool = False):
    """ConsistencyChecker — 比对工作区与 Layer A 索引；repair=true 时入队修复。"""
    loop = asyncio.get_running_loop()
    project = await loop.run_in_executor(None, store.get_project, project_id)
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


@app.post("/api/projects/{project_id}/knowledge/webhook/git", tags=["知识库"])
async def git_knowledge_webhook(project_id: str, payload: GitWebhookPayload):
    """Git push webhook → Layer A/B/D 增量更新（P2）。"""
    loop = asyncio.get_running_loop()
    project = await loop.run_in_executor(None, store.get_project, project_id)
    if not project or not project.get("path"):
        raise HTTPException(status_code=404, detail="Project not found")
    from swarm.knowledge.hooks import handle_git_push_webhook

    return await handle_git_push_webhook(
        project_id,
        project["path"],
        payload.model_dump(),
    )



# ─── Phase 5: Stats & Notifications ─────────────────




@app.get("/api/stats", tags=["系统"])
async def get_stats(project_id: str | None = None):
    """任务统计：总量、完成/失败/取消、Accept 率、平均耗时、token 估算、学习趋势、最近 10 条"""
    loop = asyncio.get_running_loop()
    if project_id:
        await loop.run_in_executor(None, _validate_project, project_id)

    stats = await loop.run_in_executor(None, lambda: store.get_task_stats(project_id))
    if project_id:
        stats["project_id"] = project_id
    return stats


@app.get("/api/projects/{project_id}/stats", tags=["系统"])
async def get_project_stats(project_id: str):
    """项目 scoped 任务统计"""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _validate_project, project_id)
    stats = await loop.run_in_executor(None, lambda: store.get_task_stats(project_id))
    stats["project_id"] = project_id
    return stats


@app.get("/api/notifications", tags=["系统"])
async def get_notifications(project_id: str | None = None, since: str | None = None):
    """最近任务事件：完成 / 失败 / 待审核（基于 task_records.updated_at）"""
    loop = asyncio.get_running_loop()
    since_dt = _parse_since_param(since)
    if project_id:
        await loop.run_in_executor(None, _validate_project, project_id)

    notifications = await loop.run_in_executor(
        None,
        lambda: store.get_task_notifications(project_id=project_id, since=since_dt),
    )
    return {"notifications": notifications}


@app.get("/api/milestones", tags=["系统"])
async def list_milestones(project_id: str | None = None, limit: int = 10):
    """Accept 率基准历史报告（P0）。"""
    loop = asyncio.get_running_loop()
    reports = await loop.run_in_executor(
        None,
        lambda: store.get_latest_milestone_reports(project_id=project_id, limit=min(limit, 50)),
    )
    return {"reports": reports}


class MilestoneReportBody(BaseModel):
    project_id: str | None = None
    phase: str
    accept_rate: float
    threshold: float
    passed: bool
    report: dict[str, Any] = Field(default_factory=dict)


@app.post("/api/milestones", tags=["系统"])
async def post_milestone_report(body: MilestoneReportBody):
    """保存 benchmark 脚本产出的里程碑报告。"""
    loop = asyncio.get_running_loop()
    saved = await loop.run_in_executor(
        None,
        lambda: store.save_milestone_report(
            project_id=body.project_id,
            phase=body.phase,
            accept_rate=body.accept_rate,
            threshold=body.threshold,
            passed=body.passed,
            report=body.report,
        ),
    )
    return saved


# ─── 前端入口 ──────────────────────────────────────
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index():
    """根路径：提供前端页面"""
    index_file = _static_dir / "index.html"
    if index_file.exists():
        return index_file.read_text(encoding="utf-8")
    return HTMLResponse(
        content="""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Swarm Web</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               max-width: 800px; margin: 40px auto; padding: 20px; background: #1a1a2e; color: #e0e0e0; }
        h1 { color: #e94560; }
        h2 { color: #0f3460; background: #16213e; padding: 8px 16px; border-radius: 4px; }
        a { color: #e94560; text-decoration: none; }
        a:hover { text-decoration: underline; }
        code { background: #16213e; padding: 2px 6px; border-radius: 3px; }
        .endpoint { margin: 8px 0; padding: 8px 12px; background: #16213e; border-radius: 6px; }
        .method { font-weight: bold; margin-right: 8px; }
        .get { color: #4CAF50; }
        .post { color: #FF9800; }
        .put { color: #2196F3; }
        .delete { color: #f44336; }
    </style>
</head>
<body>
    <h1>🐝 Swarm Web API</h1>
    <p>Swarm 群智协作系统 Web 后端</p>

    <h2>可用端点</h2>
    <div class="endpoint"><span class="method get">GET</span><code>/api/health</code> — 健康检查</div>
    <div class="endpoint"><span class="method get">GET</span><code>/api/status</code> — 系统组件状态</div>
    <div class="endpoint"><span class="method get">GET</span><code>/api/config</code> — 获取配置（脱敏）</div>
    <div class="endpoint"><span class="method put">PUT</span><code>/api/config</code> — 更新配置</div>
    <div class="endpoint"><span class="method post">POST</span><code>/api/demo</code> — 运行 Demo</div>
    <div class="endpoint"><span class="method get">GET</span><code>/api/demo/stream</code> — SSE Demo 进度流</div>
    <div class="endpoint"><span class="method get">GET</span><code>/api/sandbox/status</code> — 活跃沙箱列表</div>
    <div class="endpoint"><span class="method post">POST</span><code>/api/sandbox/create</code> — 创建沙箱</div>
    <div class="endpoint"><span class="method delete">DELETE</span><code>/api/sandbox/{id}</code> — 销毁沙箱</div>
    <div class="endpoint"><span class="method get">GET</span><code>/api/sandbox/{id}/files</code> — 查看沙箱文件列表</div>
    <div class="endpoint"><span class="method get">GET</span><code>/api/sandbox/{id}/files/content?path=</code> — 读取沙箱文件内容</div>

    <h2>文档</h2>
    <p><a href="/docs">Swagger UI</a> | <a href="/redoc">ReDoc</a></p>
</body>
</html>""",
        status_code=200,
    )


# ─── 路由模块注册 (Phase2 域拆分) ─────────────────
# 注意: 放在文件末尾, 确保 app 实例与共享 helper 均已定义,
# router 模块通过 `import swarm.api.app as _app` 反向引用时 sys.modules 已就绪。
from swarm.api.routers import memory as _memory_router  # noqa: E402

app.include_router(_memory_router.router)
