"""Swarm Web 后端 API — FastAPI 应用 + 全部端点

端点列表:
  GET  /api/status                    — 系统组件运行状态
  GET  /api/config                    — 当前配置（API Key 脱敏）
  PUT  /api/config                    — 更新配置（写入 .env 并重载）
  GET  /api/routing                   — 获取当前模型路由表
  PUT  /api/routing                   — 更新模型路由表配置
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
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from swarm.config.settings import (
    get_config,
    reload_config,
)
from swarm.project import store
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
    """配置应用日志（委托统一日志系统 swarm.logging_config）。

    API 进程由 restart-api.sh 将 stdout/stderr 重定向到 swarm.log，
    因此这里关闭控制台 handler，避免「console(stderr→文件) + 文件 handler」
    对同一 swarm.log 双写导致每行日志重复。
    """
    from swarm.logging_config import setup_logging

    setup_logging(console=False)

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

            # 从生效 providers 取【解密后】的 key（单一真相源；key 可能加密存 db）。
            # 不再直接读扁平字段 local_api_key/siliconflow_api_key —— 迁移到 secret_store
            # 后扁平字段为空，会误报"未配置"。
            _eff = {p.id: p for p in cfg.model._effective_providers()}
            _local_p = _eff.get("local")
            _cloud_p = _eff.get("siliconflow") or next(
                (p for p in _eff.values() if p.kind == "cloud"), None
            )
            _local_key = _local_p.api_key if _local_p else ""
            _local_url = _local_p.base_url if _local_p else (cfg.model.local_base_url or "")
            _cloud_key = _cloud_p.api_key if _cloud_p else ""

            # 检测本地模型可用性
            local_ok = False
            try:
                headers = {}
                if _local_key and _local_key not in ("", "***"):
                    headers["Authorization"] = f"Bearer {_local_key}"
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(f"{_local_url}/models", headers=headers)
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

            # 检测云端模型可用性（用解密后的 key）
            cloud_ok = bool(_cloud_key and _cloud_key not in ("", "***"))
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






















# ═══════════════════════════════════════════════════
# ═══════════════════════════════════════════════════
# 全局 SandboxManager（懒加载单例，与 Worker 共享）
# ═══════════════════════════════════════════════════

def _get_sandbox_manager() -> Any:
    from swarm.worker.sandbox import get_sandbox_manager
    return get_sandbox_manager()


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
    """应用启动钩子：LangSmith + dev_sidecar + 建表 + L5 衰减调度 + 通知推送 hook"""
    _configure_app_logging()
    configure_langsmith()
    _init_sidecar()
    # 注册通知推送 hook：store.create_notification 写入后，把记录调度到外部渠道推送。
    # create_notification 常在线程池(run_in_executor)里被调，故用 run_coroutine_threadsafe
    # 把异步推送投回主事件循环；拿不到 loop 时退化为后台线程跑。
    try:
        _main_loop = asyncio.get_running_loop()

        def _log_push_exc(fut) -> None:
            """记录通知推送的异步异常（否则被吞在 future 里不可见）。"""
            try:
                exc = fut.exception()
                if exc:
                    logger.warning("notification dispatch error: %s", exc)
            except Exception:  # noqa: BLE001
                pass

        def _push_notification(record: dict) -> None:
            try:
                from swarm.api.notify import dispatch_notification
                import asyncio as _aio
                try:
                    running = _aio.get_running_loop()
                except RuntimeError:
                    running = None
                if running is _main_loop and running is not None:
                    task = _main_loop.create_task(dispatch_notification(record))
                    task.add_done_callback(_log_push_exc)
                else:
                    fut = _aio.run_coroutine_threadsafe(dispatch_notification(record), _main_loop)
                    fut.add_done_callback(_log_push_exc)
            except Exception as exc:  # noqa: BLE001
                logger.debug("push notification failed: %s", exc)

        store.register_notification_hook(_push_notification)
        logger.info("Notification push hook registered")
    except Exception as e:
        logger.warning(f"Failed to register notification hook: {e}")
    # 确保 project/task 表存在
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, store.ensure_tables)
        logger.info("Project/Task tables ensured")
    except Exception as e:
        logger.warning(f"Failed to ensure project tables: {e}")
    # 确保模型能力库表存在（设计 v3 A 部分）
    try:
        from swarm.models import capability_store

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, capability_store.ensure_tables)
        logger.info("model_capabilities table ensured")
    except Exception as e:
        logger.warning(f"Failed to ensure model_capabilities table: {e}")
    # 确保敏感信息加密存储表存在（API keys 等加密存 db）
    try:
        from swarm.config import secret_store

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, secret_store.ensure_tables)
        logger.info("secret_store table ensured")
    except Exception as e:
        logger.warning(f"Failed to ensure secret_store table: {e}")
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
        # bootstrap admin 始终创建（即使 RBAC 关闭）：RBAC 开关只控制【鉴权是否强制】，
        # 不改变 admin 账号是否存在。否则 RBAC=off 的全新库上 /api/auth/login admin/swarm
        # 会 401（CI 实测复现）。ensure_bootstrap_admin 幂等，已存在则不动。
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
        if cfg.rbac_enabled:
            await loop.run_in_executor(None, backfill_legacy_project_members)
        logger.info("Auth/RBAC tables ensured")
    except Exception as e:
        logger.warning(f"Failed to ensure auth tables: {e}")
    await _start_memory_decay_scheduler()
    await _start_kb_update_scheduler()
    await _start_consistency_scheduler()
    await _start_task_scheduler()
    # 先清扫上一进程残留的孤儿沙箱，再启动池 reaper（顺序重要：清扫在池接管前）
    _sweep_startup_orphans()
    _start_sandbox_pool_reaper()
    _warn_if_multiprocess()
    logger.info("Swarm API started")


def _start_sandbox_pool_reaper() -> None:
    """热沙箱池启用时启动后台 reaper（回收超 TTL/空闲沙箱，防泄漏）。"""
    try:
        from swarm.worker.sandbox_pool import get_sandbox_pool, pool_enabled

        if pool_enabled():
            get_sandbox_pool().start_reaper()
            logger.info("热沙箱池 reaper 已启动")
    except Exception as exc:
        logger.warning("Failed to start sandbox pool reaper: %s", exc)


def _sweep_startup_orphans() -> None:
    """启动时清扫上一进程残留的孤儿沙箱。

    池是进程内内存态：上次进程若被 SIGKILL / 崩溃 / OOM（shutdown 钩子的 drain
    没跑完或没跑），远端 pool/pool-idle 沙箱就成了无主孤儿，本进程 _sandbox_meta
    为空认不得它们 → 永久泄漏烧资源。单进程模型下，启动这一刻远端任何存活沙箱
    都必是上一轮的残留，安全清扫。失败静默（不阻断启动）。
    """
    try:
        server_list = _fetch_sandbox_list_from_server()
        sids = [sb.get("id") for sb in server_list if sb.get("id")]
        if not sids:
            return
        manager = _get_sandbox_manager()
        killed = 0
        for sid in sids:
            try:
                manager.kill(sid)
                killed += 1
            except Exception:  # noqa: BLE001
                logger.debug("启动清扫: kill %s 失败", sid, exc_info=True)
        logger.info("启动清扫残留孤儿沙箱: 发现 %d, 清理 %d", len(sids), killed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动孤儿清扫失败（不阻断）: %s", exc)


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
    """应用关闭钩子：优雅关闭数据库连接池 + 排空热沙箱池。"""
    try:
        from swarm.worker.sandbox_pool import get_sandbox_pool, pool_enabled

        if pool_enabled():
            pool = get_sandbox_pool()
            pool.stop_reaper()
            pool.drain()
            logger.info("热沙箱池已排空")
    except Exception as exc:
        logger.warning("Failed to drain sandbox pool: %s", exc)
    try:
        from swarm.infra.db import close_async_pools, close_sync_pools

        await close_async_pools()
        close_sync_pools()
        logger.info("DB pools closed")
    except Exception as exc:
        logger.warning("Failed to close DB pools: %s", exc)


async def _start_kb_update_scheduler() -> None:
    """PG kb_update_events 队列后台消费（P0）+ 周期全量重预处理（opt-in）。"""
    try:
        from swarm.knowledge.scheduler import (
            start_kb_update_scheduler,
            start_preprocess_refresh_scheduler,
        )

        await start_kb_update_scheduler(interval_seconds=5)
        await start_preprocess_refresh_scheduler()
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


# ═══════════════════════════════════════════════════
# 项目管理端点
# ═══════════════════════════════════════════════════


# ─── 1. GET /api/projects — 项目列表 ──────────────


# ═══════════════════════════════════════════════════
# 任务管理端点
# ═══════════════════════════════════════════════════


# ─── 7. GET /api/projects/{project_id}/tasks — 项目任务列表 ─


# ═══════════════════════════════════════════════════
# 知识库 & 记忆 CRUD API 端点
# ═══════════════════════════════════════════════════


# ─── Pydantic Request Models ─────────────────────





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
async def get_notifications(
    project_id: str | None = None,
    include_archived: bool = False,
    limit: int = 50,
):
    """应用内通知列表（持久化、可归档）。默认只返回未归档。"""
    loop = asyncio.get_running_loop()
    if project_id:
        await loop.run_in_executor(None, _validate_project, project_id)
    notifications = await loop.run_in_executor(
        None,
        lambda: store.list_notifications(
            project_id=project_id,
            include_archived=include_archived,
            limit=min(limit, 200),
        ),
    )
    unread = await loop.run_in_executor(
        None,
        lambda: store.count_unread_notifications(project_id=project_id),
    )
    return {"notifications": notifications, "unread_count": unread}


@app.get("/api/notifications/unread_count", tags=["系统"])
async def get_unread_count(project_id: str | None = None):
    """未归档通知数（铃铛绿点轮询用，轻量）。"""
    loop = asyncio.get_running_loop()
    count = await loop.run_in_executor(
        None,
        lambda: store.count_unread_notifications(project_id=project_id),
    )
    return {"unread_count": count}


@app.post("/api/notifications/{notification_id}/archive", tags=["系统"])
async def archive_notification_endpoint(notification_id: int):
    """归档单条通知。"""
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(
        None,
        lambda: store.archive_notification(notification_id),
    )
    return {"status": "ok", "archived": ok}


@app.post("/api/notifications/archive_all", tags=["系统"])
async def archive_all_notifications_endpoint(project_id: str | None = None):
    """归档全部未读通知（可选按项目过滤）。"""
    loop = asyncio.get_running_loop()
    count = await loop.run_in_executor(
        None,
        lambda: store.archive_all_notifications(project_id=project_id),
    )
    return {"status": "ok", "archived_count": count}


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
def _stamp_static_assets(html: str) -> str:
    """给 index.html 里的本地 /static/ 资源 URL 追加基于文件 mtime 的版本号。

    无构建工具下的缓存失效方案：资源内容变化 → mtime 变化 → URL 变化 →
    浏览器强制重新拉取。避免用户看到旧 CSS/JS（修改 UI 后必现的陷阱）。
    """
    import re as _re

    def _ver(rel_path: str) -> str:
        try:
            fp = _static_dir / rel_path.lstrip("/").removeprefix("static/")
            return str(int(fp.stat().st_mtime))
        except Exception:
            return "0"

    def _repl(m: "_re.Match[str]") -> str:
        attr, url = m.group(1), m.group(2)
        if url.startswith("/static/") and "?" not in url:
            return f'{attr}="{url}?v={_ver(url)}"'
        return m.group(0)

    return _re.sub(r'(href|src)="(/static/[^"]+)"', _repl, html)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index():
    """根路径：提供前端页面（本地静态资源带 mtime 版本号，自动缓存失效）"""
    index_file = _static_dir / "index.html"
    if index_file.exists():
        return _stamp_static_assets(index_file.read_text(encoding="utf-8"))
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
from swarm.api.routers import auth as _auth_router  # noqa: E402
from swarm.api.routers import config as _config_router  # noqa: E402
from swarm.api.routers import knowledge as _knowledge_router  # noqa: E402
from swarm.api.routers import memory as _memory_router  # noqa: E402
from swarm.api.routers import observability as _observability_router  # noqa: E402
from swarm.api.routers import project as _project_router  # noqa: E402
from swarm.api.routers import sandbox as _sandbox_router  # noqa: E402
from swarm.api.routers import task as _task_router  # noqa: E402
from swarm.api.routers import upload as _upload_router  # noqa: E402
from swarm.api.routers import worker as _worker_router  # noqa: E402

app.include_router(_memory_router.router)
app.include_router(_knowledge_router.router)
app.include_router(_worker_router.router)
app.include_router(_auth_router.router)
app.include_router(_sandbox_router.router)
app.include_router(_project_router.router)
app.include_router(_task_router.router)
app.include_router(_upload_router.router)
app.include_router(_config_router.router)
app.include_router(_observability_router.router)
