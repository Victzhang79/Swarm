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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from swarm.config.settings import (
    get_config,
    reload_config,
)
from swarm.infra.redis_client import get_redis, redis_enabled
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


def _accessible_project_ids_or_none(user, project_id: str | None, request) -> "set[str] | None":
    """#19 通知/项目级资源的 IDOR 防护辅助。

    - 指定了 project_id：要求调用方对该项目有 project:read（无权 → 403）；返回 None
      （单项目已由 SQL 的 project_id= 约束，无需再传白名单）。
    - 未指定 project_id：global admin 返回 None（全量可见）；其余用户返回其可访问项目 id 集合，
      调用方据此把查询限定在白名单内（防跨项目读取/计数/归档）。
    """
    from swarm.auth.rbac import Role
    if project_id:
        from swarm.api._shared import _require_perm
        _require_perm(request, "project:read", project_id)
        return None
    if getattr(user, "global_role", None) == Role.ADMIN.value:
        return None
    from swarm.auth.store import list_user_project_ids
    return set(list_user_project_ids(user.id))

# LangSmith 在 on_startup 中初始化（需在 _configure_app_logging 之后，才能写入 swarm.log）

# 项目根目录（本仓库根 = swarm 包目录）
_PROJECT_ROOT = Path(__file__).parent.parent

logger = logging.getLogger(__name__)

# H9 修复：startup 长生命周期后台任务强引用集合，防被 GC 静默丢失。
_APP_BG_TASKS: set = set()


def _spawn_bg(coro):
    """持引用地 create_task（H9）：避免 asyncio 弱引用下任务被回收。"""
    import asyncio as _a
    _t = _a.create_task(coro)
    _APP_BG_TASKS.add(_t)
    _t.add_done_callback(_APP_BG_TASKS.discard)
    return _t
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

async def _check_component(name: str, is_admin: bool = False) -> dict[str, Any]:
    # B8-F1（对抗复核 MEDIUM）：安全布尔 fail-closed 缺省 False（铁律#3）——默认掩码内部坐标，
    # 唯有显式 is_admin=True 才返回完整 detail。杜绝未来新调用点忘传参而静默全量泄露。
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
            details: list[str] = []

            # 1) 检查 Qdrant 是否在线——R2-4：直接复用 /api/health/ready 的
            # _probe_qdrant_ready（fail-closed：远端挂+陈旧本地目录不算健康、本地文件模式
            # 需 _is_local_qdrant+meta.json；探测目标取同一 config）。
            # 上一版是"逻辑对齐"的第二份实现，改为复用消除再漂移。
            qdrant_ok, _q_detail = await _probe_qdrant_ready()
            details.append(f"qdrant {_q_detail}" if qdrant_ok else f"qdrant unreachable: {_q_detail}")

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
                # 通过【统一入口】get_embed_endpoint 探测远程 embedding，确保探测目标
                # 与实际嵌入所用端点一致（修复 12.7：此前直读 siliconflow_* 与实际使用
                # 的 embed_rerank_config 入口不一致，可能误报 ready/degraded）。
                try:
                    from swarm.knowledge.embed_rerank_config import get_embed_endpoint
                    ep = get_embed_endpoint()
                    if ep is not None:
                        async with httpx.AsyncClient(timeout=5) as client:
                            headers = {"Authorization": f"Bearer {ep.api_key}"} if ep.api_key else {}
                            resp = await client.post(
                                f"{ep.base_url}/embeddings",
                                json={"model": ep.model, "input": "test"},
                                headers=headers,
                            )
                            if resp.status_code == 200:
                                dim = len(resp.json().get("data", [{}])[0].get("embedding", []))
                                details.append(f"embedding: {ep.model} (remote, dim={dim})")
                                embed_ok = True
                            else:
                                details.append(
                                    f"embedding: endpoint {ep.base_url} 返回 status={resp.status_code}"
                                )
                except Exception as exc:
                    logger.debug("embedding 远程探测失败: %s", exc)
            if not embed_ok:
                # KB 检索仍可用（降级为 BM25 关键词检索），但语义召回质量下降
                details.append("embedding: no local model, no remote endpoint (KB 检索降级为 BM25)")

            # 用真实探测结果——子串启发式 any("qdrant" in d) 在失败文案
            # "qdrant unreachable: X" 上同样命中 → Qdrant 全挂仍报健康（假绿）
            qdrant_ok_flag = qdrant_ok
            embed_ok_flag = embed_ok  # 用显式探测结果，不靠脆弱的字符串匹配（降级文案也含 "embedding:"）
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
                from swarm.infra.db import pg_connect_timeout_kwargs

                result: dict[str, Any] = {}
                # D15：直连补 connect_timeout——PG 黑洞时健康检查有界快失败，不泄漏挂起线程。
                with psycopg.connect(uri, autocommit=True, **pg_connect_timeout_kwargs()) as conn:
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
                from swarm.infra.db import pg_connect_timeout_kwargs

                result: dict[str, Any] = {}
                # D15：直连补 connect_timeout——PG 黑洞时健康检查有界快失败，不泄漏挂起线程。
                with psycopg.connect(uri, autocommit=True, **pg_connect_timeout_kwargs()) as conn:
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
            # 发版级 hunter CONFIRMED：本分支曾是探活假绿的最后一个 sibling——自有内联实现
            # 缺 _is_local_qdrant 守卫（远端挂+陈旧 ~/.swarm/qdrant 报 ready）。与"知识库"
            # 分量同法：直接复用 /ready 的 _probe_qdrant_ready，全系统 Qdrant 探活单一实现。
            q_ok, q_detail = await _probe_qdrant_ready()
            status["status"] = "running" if q_ok else "error"
            status["detail"] = q_detail if q_ok else f"unreachable: {q_detail}"

        else:
            status["status"] = "unknown"

    except Exception as e:
        status["status"] = "error"
        status["detail"] = str(e)[:200]

    # B8-F1：/api/status 无角色闸，任何已认证用户（含 viewer）都能读到 detail。detail 内嵌
    # 内部基建坐标（worker 主模型名 / sandbox_api_url / 远程沙箱 api_url / PG version / 模型端点）
    # = 横向移动侦察面。与 sandbox.py D47b（同一 api_url 只给 admin）对齐：非 admin 只保留
    # 健康红绿灯（status），清空 detail（fail-closed：未来给某分量新增坐标也默认不外泄，
    # 不靠逐分量维护敏感字段清单）。RBAC-off → anonymous admin → is_admin=True → 全量 detail。
    if not is_admin:
        status["detail"] = ""

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

from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """F3：FastAPI lifespan（替代弃用的 @app.on_event startup/shutdown）。

    启动/关闭逻辑仍在下方 on_startup()/on_shutdown()（保留函数名，既有 getsource/集成测试
    不回归）；此处只编排：yield 前跑 startup、yield 后(finally)跑 shutdown。
    """
    await on_startup()
    try:
        yield
    finally:
        await on_shutdown()


app = FastAPI(
    title="Swarm API",
    version="0.9.64",
    description="Swarm Web 后端 API",
    lifespan=_lifespan,
)

from swarm.api.auth import SwarmAPIKeyMiddleware  # noqa: E402

app.add_middleware(SwarmAPIKeyMiddleware)


async def on_startup():
    """应用启动钩子：LangSmith + dev_sidecar + 建表 + L5 衰减调度 + 通知推送 hook"""
    _configure_app_logging()
    # 破坏性误配 fail-fast（放最前，任何资源初始化前）：多 worker 与单进程架构不兼容 →
    # 硬拦拒绝启动，而非带病运行到运行期 SSE/调度错乱才暴雷。
    _warn_if_multiprocess()
    # 生产模式安全自检（fail-closed）：默认凭据/未设根密钥时拒绝启动，
    # 让误配的生产部署在启动期就快速失败，而非带病运行到运行期才暴雷。
    from swarm.config.settings import validate_production_security
    validate_production_security()
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
    # P0-C：先跑版本化迁移（run_migrations 幂等：全新库跑 baseline DDL、既有库仅盖章
    # schema_version），再逐个 ensure_tables。此前迁移只在 scripts/init_db.py 调，容器/直起
    # 路径从不 stamp schema_version → 版本化迁移形同虚设、将来 ALTER 不自动应用 → schema 漂移。
    # fail-fast（不 try/except 吞）：迁移是 schema 单一事实源，失败即抛让启动崩溃，杜绝带病运行；
    # 区别于下方 ensure_tables 的 fail-open 容错。
    from swarm.infra.migrations.runner import run_migrations

    loop = asyncio.get_running_loop()
    # conn_str=None → run_migrations 内部走 DatabaseConfig().postgres_uri（与 .env 同源），
    # 且避免在 on_startup 里引用被下方局部 import 遮蔽的 get_config。
    await loop.run_in_executor(None, run_migrations, None)
    logger.info("DB migrations applied (schema_version stamped)")
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
    # 确保沙箱模板配置表存在（exec/verify 镜像，系统级 WebUI 可配）
    try:
        from swarm.config import sandbox_store

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, sandbox_store.ensure_tables)
        logger.info("sandbox_templates table ensured")
    except Exception as e:
        logger.warning(f"Failed to ensure sandbox_templates table: {e}")
    try:
        from swarm.config import command_blacklist_store

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, command_blacklist_store.ensure_tables)
        logger.info("command_blacklist table ensured")
    except Exception as e:
        logger.warning(f"Failed to ensure command_blacklist table: {e}")
    try:
        from swarm.config import skill_store

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, skill_store.ensure_tables)
        logger.info("experience_skills table ensured")
    except Exception as e:
        logger.warning(f"Failed to ensure experience_skills table: {e}")
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
        # R23-7 治本：RBAC 开启时 auth 表初始化失败必须 fail-fast——否则鉴权/登录会在运行期(首个
        # 请求)才暴雷，可能带病放行或全线 500。仅在 RBAC 关闭(无鉴权需求)时降级为 warning。
        if get_config().rbac_enabled:
            logger.error("RBAC 开启但 auth 表初始化失败，fail-fast 拒绝带病启动: %s", e)
            raise
        logger.warning(f"Failed to ensure auth tables (RBAC off, 忽略): {e}")
    # A1 批1：初始化 PG checkpointer（多副本共享 + 跨副本 interrupt/resume）。
    # 必须在 runner/调度器使用 graph 之前。
    # P0-D fail-fast：init_postgres_checkpointer 仅在【要求强制 PG】（生产默认 / 显式
    # SWARM_REQUIRE_PG_CHECKPOINTER）且初始化失败时 raise——此时【不得吞异常】，让启动崩溃，
    # 杜绝生产带 MemorySaver 带病运行（重启即丢中断 checkpoint）。dev/非强制路径返回 False
    # 静默降级、不抛，故此处不再包 try/except（与上方 run_migrations fail-fast 同规格）。
    from swarm.brain.graph import init_postgres_checkpointer

    await init_postgres_checkpointer()
    # A1 批2：调度器选主——leader 副本跑全部后台调度器，非 leader 待命可接管。
    # 单进程/PG 不可用时降级为"本进程即 leader"（单机行为不变）。
    try:
        from swarm.infra.scheduler_leadership import init_coordination_backend

        await init_coordination_backend()
    except Exception as e:
        logger.warning(f"协调后端初始化跳过: {e}")
    _spawn_bg(_run_schedulers_with_leadership())
    # 先清扫上一进程残留的孤儿沙箱，再启动池 reaper（顺序重要：清扫在池接管前）
    _sweep_startup_orphans()
    _start_sandbox_pool_reaper()
    # P0-A：启动对账——把上一进程残留的"进行中"任务按态分治恢复/失败（沙箱已由上方 sweep 清；
    # schedulers 已 spawn，SUBMITTED 重入队项会被消费）。best-effort：对账失败不阻断启动。
    try:
        from swarm.brain.runner import reconcile_orphan_tasks

        await reconcile_orphan_tasks()
    except Exception as e:
        logger.warning(f"启动对账跳过: {e}")

    # round29 运维项：checkpoint 三表 GC（终态 TTL + 孤儿线程 + worker 子图 ns 残留，实测
    # 无清理机制累积 14.4GB）。放对账之后（对账可能把孤儿任务标 FAILED→本轮即可清其过期项）；
    # 后台执行不阻断启动（大库首清可能分钟级），自身 fail-safe。
    async def _checkpoint_gc_bg() -> None:
        # 纵深防御（hunter#2）：_spawn_bg 的 done-callback 不取 exception → 任何裸穿异常只会
        # 延迟落 asyncio 的通用 "never retrieved" 告警（无上下文、时机不定）。此处自兜自记。
        try:
            import asyncio as _aio

            from swarm.infra.checkpoint_gc import sweep_stale_checkpoints

            await _aio.get_running_loop().run_in_executor(None, sweep_stale_checkpoints)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[checkpoint-gc] 后台任务异常（不影响服务）: %s", exc)

    _spawn_bg(_checkpoint_gc_bg())
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


def _partition_sweep_targets(
    server_list: list[dict], my_instance: str | None, sweep_untagged: bool
) -> tuple[list[str], int, int]:
    """A1 批3：把服务端沙箱列表按归属分类（纯函数，可单测）。

    返回 (to_kill, kept_other, kept_untagged)：
    - 有 swarm_instance 标签 == 本实例 → to_kill（本进程残留）
    - 有标签但 != 本实例 → kept_other（别副本，绝不动）
    - 无标签 → sweep_untagged 决定 kill 还是 kept_untagged
    """
    to_kill: list[str] = []
    kept_other = 0
    kept_untagged = 0
    for sb in server_list:
        sid = sb.get("id")
        if not sid:
            continue
        owner = (sb.get("metadata") or {}).get("swarm_instance")
        if owner:
            if owner == my_instance:
                to_kill.append(sid)
            else:
                kept_other += 1
        else:
            if sweep_untagged:
                to_kill.append(sid)
            else:
                kept_untagged += 1
    return to_kill, kept_other, kept_untagged


def _sweep_startup_orphans() -> None:
    """启动时清扫【本实例】上一进程残留的孤儿沙箱（A1 批3：实例隔离）。

    池是进程内内存态：上次进程若被 SIGKILL/崩溃/OOM，远端 pool 沙箱成无主孤儿，
    本进程 _sandbox_meta 为空认不得 → 永久泄漏。

    A1 批3 用实例标签精确清扫（替代 12.2 的 opt-in 全清扫开关止血）：
    - 有 swarm_instance 标签且 == 本实例 → 必是本进程上一轮残留，清扫（多副本安全，不误杀别副本）。
    - 无标签沙箱（降级创建/旧版残留）→ 退回 sweep_orphans_on_startup 开关控制：
      开关 on（单机默认）则清，off（共享集群保守）则留。
    失败静默（不阻断启动）。
    """
    try:
        from swarm.config.settings import get_config
        from swarm.worker.sandbox import get_instance_id

        sweep_untagged = get_config().sandbox.sweep_orphans_on_startup
        my_instance = get_instance_id()
    except Exception:  # noqa: BLE001 — 配置/实例读取失败按保守默认
        sweep_untagged = True
        my_instance = None

    try:
        server_list = _fetch_sandbox_list_from_server()
        to_kill, kept_other, kept_untagged = _partition_sweep_targets(
            server_list, my_instance, sweep_untagged
        )
        if not to_kill:
            logger.info(
                "启动清扫: 无本实例残留可清（别副本=%d, 无标签保留=%d）", kept_other, kept_untagged
            )
            return
        manager = _get_sandbox_manager()
        killed = 0
        for sid in to_kill:
            try:
                manager.kill(sid)
                killed += 1
            except Exception:  # noqa: BLE001
                logger.debug("启动清扫: kill %s 失败", sid, exc_info=True)
        logger.info(
            "启动清扫本实例孤儿沙箱: 清理 %d/%d（别副本保留=%d, 无标签保留=%d, 实例=%s）",
            killed, len(to_kill), kept_other, kept_untagged, my_instance,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动孤儿清扫失败（不阻断）: %s", exc)


def _warn_if_multiprocess():
    """检测多 worker 误配置并【硬拦】（fail-fast）。

    当前架构为单进程模型：任务事件队列（SSE/WS 推送）、调度器 leader、leader 内存队列 meta、
    KB updater 均为进程内单例。若用 uvicorn --workers N>1 或 gunicorn 多 worker 启动，会导致：
    任务在 A 进程跑、客户端 SSE 连到 B 进程收不到推送；调度/队列 meta 各进程各一份互不可见。

    改为硬拦（原仅告警）：多 worker 是【会静默错乱】的破坏性误配，按 fail-closed 在启动期拒绝，
    而非带病运行。逃生阀 SWARM_ALLOW_MULTIPROCESS=1：明确知道风险（如已外置队列/SSE）时降级为告警。
    """
    web_concurrency = os.environ.get("WEB_CONCURRENCY")
    try:
        n = int(web_concurrency) if web_concurrency else 1
    except ValueError:
        n = 1
    if n <= 1:
        return
    allow = os.environ.get("SWARM_ALLOW_MULTIPROCESS", "").strip().lower() in ("1", "true", "yes")
    msg = (
        f"检测到 WEB_CONCURRENCY={web_concurrency}（多 worker）。当前架构为单进程模型，"
        "多 worker 会导致 SSE/WS 推送错乱、调度器/队列 meta 各进程割裂、resume 不可靠。"
        "请以单 worker 启动；确需多副本须先外置队列/SSE（见 README Roadmap）。"
    )
    if allow:
        logger.warning("⚠️  %s （SWARM_ALLOW_MULTIPROCESS 已设 → 仅告警，风险自负）", msg)
        return
    raise RuntimeError(msg + " 如确需绕过请设 SWARM_ALLOW_MULTIPROCESS=1。")


async def _start_task_scheduler() -> None:
    """任务准入调度器（优先级队列 + 有界并发）。"""
    try:
        from swarm.brain.scheduler import start_task_scheduler

        await start_task_scheduler()
    except Exception as exc:
        logger.warning("Failed to start task scheduler: %s", exc)


async def on_shutdown():
    """应用关闭钩子：优雅关闭数据库连接池 + 排空热沙箱池。

    N-08/N-10：必须【先】取消所有后台循环 task（leadership/decay/consistency/KB 调度），
    再关池/释放 advisory lock——否则循环会在已关闭的池上继续跑抛错，并重抢刚释放的
    advisory lock 导致脑裂（两副本同时调度）。
    """
    # 1) 取消应用级后台循环（_spawn_bg 持引用的 leadership/decay/consistency 等）
    import asyncio as _aio

    _bg = list(_APP_BG_TASKS)
    for _t in _bg:
        if not _t.done():
            _t.cancel()
    if _bg:
        await _aio.gather(*_bg, return_exceptions=True)
        logger.info("已取消 %d 个应用后台任务", len(_bg))
    # 2) 取消 KB 调度循环并关闭共享 updater（此前 shutdown_kb_scheduler 从不被调用）
    try:
        from swarm.knowledge.scheduler import shutdown_kb_scheduler

        await shutdown_kb_scheduler()
        logger.info("KB 调度器已关闭")
    except Exception as exc:
        logger.warning("Failed to shutdown KB scheduler: %s", exc)
    # 3) 停止任务准入调度器消费循环
    try:
        from swarm.brain.scheduler import stop_task_scheduler

        await stop_task_scheduler()
        logger.info("任务准入调度器已停止")
    except Exception as exc:
        logger.warning("Failed to stop task scheduler: %s", exc)
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
    # A1 批1：关闭 PG checkpointer 连接（与 app 生命周期对齐）
    try:
        from swarm.brain.graph import close_postgres_checkpointer

        await close_postgres_checkpointer()
    except Exception as exc:
        logger.warning("Failed to close PG checkpointer: %s", exc)
    # A1 批2：关闭协调后端（释放 advisory lock，让其它副本可接管调度）
    try:
        from swarm.infra.scheduler_leadership import close_coordination_backend

        await close_coordination_backend()
    except Exception as exc:
        logger.warning("Failed to close coordination backend: %s", exc)
    # 最后：立刻停掉 LangSmith 后台上报，绝不为可观测性 flush 阻塞进程退出/重启。
    try:
        from swarm.tracing import shutdown_tracing

        shutdown_tracing(timeout=0.0)
    except Exception as exc:
        logger.warning("Failed to shutdown LangSmith tracing: %s", exc)


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
        _spawn_bg(_loop())
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
        from swarm.infra.db import pg_connect_timeout_kwargs

        # D15：直连补 connect_timeout——PG 黑洞时有界快失败，不无限挂。
        conn = await psycopg.AsyncConnection.connect(
            cfg.db.postgres_uri, autocommit=True, **pg_connect_timeout_kwargs()
        )
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


def _leader_heartbeat_seconds() -> float:
    """D38：leader 心跳间隔（秒）。env SWARM_LEADER_HEARTBEAT_SEC，非法值回退默认 30，
    钳制 [0.05, 3600]（下限放宽到 0.05 供测试注入短心跳）。"""
    raw = os.environ.get("SWARM_LEADER_HEARTBEAT_SEC", "")
    try:
        val = float(raw) if raw else 30.0
    except ValueError:
        val = 30.0
    return min(3600.0, max(0.05, val))


async def _stop_leader_schedulers(sched_tasks: list) -> None:
    """D38：失主时停止本副本全部后台调度器（对齐 on_shutdown 的调度器段）。

    task/KB 调度器有干净停止面（stop_task_scheduler/shutdown_kb_scheduler，均幂等且
    停止后可重启）；decay/consistency/kb-prune 等 _spawn_bg 循环无独立停止面，用启动
    前后 _APP_BG_TASKS 快照差集精确 cancel（不误伤其它后台任务）。"""
    try:
        from swarm.brain.scheduler import stop_task_scheduler

        await stop_task_scheduler()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[D38] 失主停任务准入调度器失败: %s", exc)
    try:
        from swarm.knowledge.scheduler import shutdown_kb_scheduler

        await shutdown_kb_scheduler()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[D38] 失主停 KB 调度器失败: %s", exc)
    pending = [t for t in sched_tasks if not t.done()]
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    logger.info("[D38] 失主清理完成：已停止调度器 + 取消 %d 个调度后台循环", len(pending))


async def _run_schedulers_with_leadership() -> None:
    """A1 批2：仅 leader 副本启动全部后台调度器，非 leader 待命并周期重试抢主。

    4 个调度器内部各自是常驻 loop（每日/每5s），故 leader 只需启动一次。
    非 leader 每 30s 重试；原 leader 挂掉（连接断→advisory lock 释放）后接管。
    单进程/PG 不可用时 try_become_leader 恒为 True（降级单机不变）。

    ★D38 治本★：启动调度器后【不再 return】——原码抢主即返回，此后永不验主：PG 重启/
    闪断使 advisory lock 服务端释放，副本 B 接管后 A 仍在跑（永久双 leader 双消费）。
    现改为 leader 心跳看门狗：周期 verify_leadership（校验 PG 会话真实存活），失主 →
    logger.critical + 停调度器（任务/KB 有干净停止面，其余按 bg-task 差集 cancel）→
    回候选循环重新竞选。单机降级（无协调后端）保持原行为：恒 leader、无看门狗。
    """
    from swarm.infra.scheduler_leadership import get_coordination_backend, make_leadership

    lead = make_leadership("scheduler:all")
    while True:
        if not await lead.try_become_leader():
            await asyncio.sleep(30)
            continue
        logger.info("[A1] 本副本成为调度器 leader，启动后台调度器")
        _before = set(_APP_BG_TASKS)
        await _start_memory_decay_scheduler()
        await _start_kb_update_scheduler()
        await _start_kb_prune_scheduler()  # P2-C：每日 KB 日志/共现清理
        await _start_consistency_scheduler()
        await _start_task_scheduler()
        await _start_periodic_reconcile()  # E7+E13（阶段5）：leader 专属周期对账+挂起 TTL
        if get_coordination_backend() is None:
            # 单进程降级：无协调后端 → 本进程恒为 leader，无失主可言（原行为不变）
            return
        _sched_tasks = [t for t in _APP_BG_TASKS if t not in _before]
        hb = _leader_heartbeat_seconds()
        while True:
            await asyncio.sleep(hb)
            try:
                still = await lead.still_leader()
            except Exception as exc:  # noqa: BLE001
                # fail-closed：心跳自身异常按失主处理（宁可停下重新竞选，绝不带病双跑）
                logger.warning("[D38] leader 心跳校验异常，按失主处理: %s", exc)
                still = False
            if not still:
                break
        logger.critical(
            "[D38] 调度器 leadership 丢失（PG 会话断/锁被其它副本接管）——"
            "停止本副本后台调度器防双跑，回候选循环重新竞选"
        )
        await _stop_leader_schedulers(_sched_tasks)
        try:
            await lead.release()  # 幂等：清本地态（锁本身已随会话释放）
        except Exception:  # noqa: BLE001
            pass


async def _start_periodic_reconcile() -> None:
    """E7+E13（阶段5，登记册 §六）：周期孤儿对账 + 挂起态 TTL 升级通知。

    旧行为对账只在 lifespan startup 跑一次——运行期出现的孤儿（FAILED 落库失败即
    永久"进行中"、进程内执行态与 DB 漂移）无人再看。leader 专属（随 leadership 停启，
    进 _sched_tasks 差集在失主时被 cancel）；periodic=True 跳过 SUBMITTED 重入队
    （防队列膨胀），活跃孤儿判定沿用 is_task_claimed（本进程在跑的绝不误杀）。
    SWARM_RECONCILE_INTERVAL_S<=0 关闭（默认 600s）。"""
    import os as _os
    try:
        interval = float(_os.environ.get("SWARM_RECONCILE_INTERVAL_S", "600") or "600")
    except ValueError:
        interval = 600.0
    if interval <= 0:
        logger.info("[E7] 周期对账关闭（SWARM_RECONCILE_INTERVAL_S<=0）")
        return

    async def _loop() -> None:
        from swarm.brain.runner import check_suspended_ttl, reconcile_orphan_tasks
        while True:
            await asyncio.sleep(interval)
            try:
                await reconcile_orphan_tasks(periodic=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[E7] 周期对账异常（下轮再试）: %s", exc)
            try:
                await check_suspended_ttl()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[E13] 挂起态 TTL 检查异常（下轮再试）: %s", exc)

    _spawn_bg(_loop())
    logger.info("[E7] 周期对账已启动（每 %.0fs，含 E13 挂起态 TTL 提醒）", interval)


async def _start_memory_decay_scheduler() -> None:
    """启动 L5 错题集每日衰减调度；PG 不可用时仅记录警告"""
    try:
        from swarm.memory.decay import MemoryDecay
        from swarm.memory.store import MemoryStore

        mem_store = MemoryStore()
        await mem_store.connect()
        decay = MemoryDecay(mem_store)
        _spawn_bg(decay.start_daily_decay())
        logger.info("L5 memory decay scheduler started (daily at 03:00)")
    except Exception as exc:
        logger.warning("Failed to start L5 memory decay scheduler: %s", exc)


async def _start_kb_prune_scheduler() -> None:
    """P2-C：启动每日 KB 修改日志/共现清理调度（防 kb_modification_log/kb_co_occurrence 无界）。

    此前 behavior_store.prune_old_logs 零调用方 → 表只增不删。leader 独占（经 leader 启动链调用），
    随 _spawn_bg 在 app 关闭时被取消。PG 不可用仅告警。"""
    try:
        _spawn_bg(_kb_prune_daily_loop())
        logger.info("KB prune scheduler started (daily at 04:00)")
    except Exception as exc:
        logger.warning("Failed to start KB prune scheduler: %s", exc)


async def _run_kb_prune_once(retention: int) -> None:
    """跑一轮全库 KB 清理（供每日循环 wait_for 包裹，防单轮挂死拖垮整个调度）。"""
    from swarm.knowledge.behavior_store import BehaviorStore

    loop = asyncio.get_running_loop()
    projects = await loop.run_in_executor(None, store.list_projects)
    bstore = BehaviorStore()
    try:
        await bstore.connect()
        total = 0
        for p in projects:
            pid = p.get("id")
            if not pid:
                continue
            try:
                total += await bstore.prune_old_logs(pid, retention_days=retention)
            except Exception as exc:  # noqa: BLE001
                logger.debug("KB prune 项目 %s 失败: %s", pid, exc)
        logger.info("每日 KB 清理完成：删 %d 条日志，跨 %d 个项目（保留 %d 天）",
                    total, len(projects), retention)
    finally:
        await bstore.close()

    # P2-A：同轮裁剪 append-only 的 task_audit_log（纯增无删路径 → 长跑膨胀）。
    # 独立 retention（审计合规窗口通常更长），复用同一每日调度 + wait_for 超时保护。
    try:
        audit_retention = int(os.environ.get("SWARM_AUDIT_RETENTION_DAYS", "365"))
    except ValueError:
        audit_retention = 365
    try:
        purged = await loop.run_in_executor(
            None, lambda: store.purge_old_task_audit(audit_retention))
        if purged:
            logger.info("每日审计裁剪：删 task_audit_log %d 行（保留 %d 天）", purged, audit_retention)
    except Exception as exc:  # noqa: BLE001
        logger.warning("每日审计裁剪失败(非致命): %s", exc)

    # D21：uploads 孤儿批次 GC——workspace/uploads/<batch>/ 此前全仓无删除路径（上传后
    # 不建任务/旧版删除不清文件 → 无限累积）。只删【无任何 task_records 引用且超龄】的
    # 批次目录（SWARM_UPLOADS_GC_DAYS，默认 7 天；路径归属复校防穿越），同轮跑、异常不阻断。
    try:
        removed_uploads = await loop.run_in_executor(None, store.gc_orphan_upload_batches)
        if removed_uploads:
            logger.info("每日 uploads 孤儿批次 GC：清理 %d 个目录", removed_uploads)
    except Exception as exc:  # noqa: BLE001
        logger.warning("每日 uploads 孤儿批次 GC 失败(非致命): %s", exc)

    # P2-B：Qdrant 孤儿向量对账——删已不存在项目的残留 points（delete_project 时 best-effort
    # Qdrant 清理失败的残留）。存活集 = DB 现存 project_id。同轮跑，异常不阻断。
    try:
        # 复核 F1（TOCTOU）：live 集必须【此刻新取】——上面 KB prune 循环可能已跑数十秒，
        # 期间新建并已索引的项目若用旧快照会被当孤儿误删。就地重取把窗口压到近 0。
        fresh_projects = await loop.run_in_executor(None, store.list_projects)
        live_ids = {p.get("id") for p in (fresh_projects or []) if p.get("id")}
        from swarm.knowledge.semantic_index import SemanticIndexer
        idx = SemanticIndexer()
        try:
            await idx.connect()
            cleaned = await idx.reconcile_orphan_points(live_ids)
            if cleaned:
                logger.info("每日 Qdrant 孤儿对账：清理 %d 个已删项目的残留向量", cleaned)
        finally:
            await idx.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("每日 Qdrant 孤儿对账失败(非致命): %s", exc)


async def _kb_prune_daily_loop(hour: int = 4) -> None:
    """每日 hour 点清理全库项目的过旧 KB 修改日志 + 陈旧共现。SWARM_KB_LOG_RETENTION_DAYS 可调。

    对抗复核 P2-C：BehaviorStore 用裸连接（不走 F2 池 connect_timeout），且 DELETE 无语句超时——
    PG 不可达/锁冲突会把本协程挂在 await 上、后续每日轮全部静默跳过。故整轮用 asyncio.wait_for
    硬性封顶（默认 300s），超时记警告并等下一天，绝不永久卡死调度。"""
    from datetime import datetime, timedelta

    try:
        retention = int(os.environ.get("SWARM_KB_LOG_RETENTION_DAYS", "180"))
    except ValueError:
        retention = 180
    try:
        run_timeout = float(os.environ.get("SWARM_KB_PRUNE_TIMEOUT", "300"))
    except ValueError:
        run_timeout = 300.0
    while True:
        now = datetime.now()
        nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            await asyncio.wait_for(_run_kb_prune_once(retention), timeout=run_timeout)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning("每日 KB 清理超时（>%.0fs），本轮跳过，等下一天", run_timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning("每日 KB 清理失败: %s", exc)

_static_dir = Path(__file__).parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


# ═══════════════════════════════════════════════════
# 端点实现
# ═══════════════════════════════════════════════════


# ─── 1. GET /api/health ────────────────────────────
@app.get("/api/health", tags=["系统"])
async def health_check():
    """健康检查（含版本号，供前端 version-badge 动态显示）"""
    try:
        from swarm import __version__ as _ver
    except Exception:  # noqa: BLE001
        _ver = ""
    return {"status": "ok", "timestamp": time.time(), "version": _ver}


@app.get("/api/metrics", tags=["系统"])
async def metrics(request: Request):
    """P2-D：Prometheus 文本导出（任务态计数 + 调度器在飞/待跑 + Redis 开关）。

    需鉴权（避免任务态计数信息泄漏）；无外部依赖（手写 exposition，不引 prometheus_client）。
    DB 不可用时相应指标缺省为空段，端点仍 200（探针友好）。"""
    from swarm.api._shared import _require_user_async
    await _require_user_async(request)  # D48：鉴权 PG 查询卸线程（Prometheus 抓取面）

    loop = asyncio.get_running_loop()
    by_status = await loop.run_in_executor(None, store.count_tasks_by_status)
    from swarm.brain.scheduler import queue_stats
    qs = queue_stats()

    lines: list[str] = []
    lines.append("# HELP swarm_tasks_total Task count by status")
    lines.append("# TYPE swarm_tasks_total gauge")
    for st, n in sorted(by_status.items()):
        # 复核 F5：Prometheus label value 正确转义（\ → \\，换行 → \n，" → \"），非仅剥引号。
        safe = str(st).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
        lines.append(f'swarm_tasks_total{{status="{safe}"}} {int(n)}')
    lines.append("# HELP swarm_scheduler_inflight In-flight (running) tasks")
    lines.append("# TYPE swarm_scheduler_inflight gauge")
    lines.append(f"swarm_scheduler_inflight {int(qs['inflight'])}")
    lines.append("# HELP swarm_scheduler_pending_meta Pending exec-meta in memory")
    lines.append("# TYPE swarm_scheduler_pending_meta gauge")
    lines.append(f"swarm_scheduler_pending_meta {int(qs['pending_meta'])}")
    lines.append("# HELP swarm_scheduler_max_concurrent Concurrency cap")
    lines.append("# TYPE swarm_scheduler_max_concurrent gauge")
    lines.append(f"swarm_scheduler_max_concurrent {int(qs['max_concurrent'])}")
    lines.append("# HELP swarm_redis_enabled Redis backend enabled (1/0)")
    lines.append("# TYPE swarm_redis_enabled gauge")
    lines.append(f"swarm_redis_enabled {1 if redis_enabled() else 0}")
    # E1：降级路径分类计数——运维据此分清"预期降级"vs"某路径被高频触发的真 bug"。
    from swarm.infra.degrade import degrade_counts
    _dg = degrade_counts()
    lines.append("# HELP swarm_degrade_total Fail-soft degrade events by category")
    lines.append("# TYPE swarm_degrade_total counter")
    for _cat, _n in sorted(_dg.items()):
        _safe = str(_cat).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
        lines.append(f'swarm_degrade_total{{category="{_safe}"}} {int(_n)}')
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


# ─── 1b. 就绪探针依赖探测（供 /api/health/ready 复用；测试可 patch 这三只） ───
# 设计：只回布尔 + 良性短 detail；失败仅回异常类名，绝不回显连接串/版本/密码
# （/api/health/ready 经 _PUBLIC_PREFIXES 前缀公开可达，须防 #21 类信息泄漏）。


async def _probe_pg_ready() -> tuple[bool, str]:
    """PG 就绪：SELECT 1（2s connect 超时，跑线程避免阻塞事件循环）。"""
    import psycopg

    uri = get_config().db.postgres_uri

    def _run() -> None:
        with psycopg.connect(uri, autocommit=True, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()

    try:
        await asyncio.to_thread(_run)
        return True, "SELECT 1 ok"
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__


async def _probe_redis_ready() -> tuple[bool, str]:
    """Redis 就绪：get_redis() 内部已 ping，不可用返 None。仅在 redis_enabled() 时被调用。"""
    try:
        r = await asyncio.to_thread(get_redis)
        return (True, "ping ok") if r is not None else (False, "unreachable")
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__


def _is_local_qdrant(url: str) -> bool:
    """qdrant_url 是否指向本地/环回地址（→ 才有资格用本地文件兜底判健康）。"""
    from urllib.parse import urlparse

    # 空 host（如误配 http:///... 或缺 host）不是环回地址——不得当本地，否则远端误配 +
    # 宿主机陈旧 ~/.swarm/qdrant/meta.json 会假绿（对抗复核 P0-B 残留）。
    host = (urlparse(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1")


async def _probe_qdrant_ready() -> tuple[bool, str]:
    """Qdrant 就绪：GET /collections（2s）。

    ★fail-closed 关键（P0-B 对抗复核 Finding-1）：本地文件模式兜底【只在 qdrant_url 指向本地】
    时才算健康——server 模式（远端 URL）下服务器不可达=KB 真不可用，若用宿主机上一次本地跑
    残留的 ~/.swarm/qdrant/meta.json 误判「local file mode」健康，就把「假绿」又给 Qdrant 放回来了。
    """
    import httpx

    qdrant_url = get_config().db.qdrant_url
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            resp = await client.get(f"{qdrant_url}/collections")
        if resp.status_code == 200:
            n = len(resp.json().get("result", {}).get("collections", []))
            return True, f"server online, {n} collections"
        return False, f"HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        if _is_local_qdrant(qdrant_url):
            storage_path = os.path.expanduser("~/.swarm/qdrant")
            if os.path.exists(storage_path) and os.path.exists(os.path.join(storage_path, "meta.json")):
                return True, "local file mode"
        logger.debug("[qdrant probe] unreachable %s: %s", type(exc).__name__, exc)
        return False, "unreachable"


# ─── 1c. GET /api/health/ready ─────────────────────
@app.get("/api/health/ready", tags=["系统"])
async def health_ready():
    """就绪探针 — 真实探测启用中的依赖，供容器 HEALTHCHECK / 编排就绪门使用。

    PG 必查；Redis 仅在 SWARM_REDIS_ENABLED 时纳入判定（默认关→不因它失败）；
    Qdrant 含本地文件兜底。任一【启用中】依赖不可达 → 503 + 每依赖 ok/detail 明细。
    fail-closed：只对启用依赖判失败，且探测本身异常一律视为该依赖不可达。
    """
    checks: dict[str, dict] = {}
    ok_all = True

    pg_ok, pg_detail = await _probe_pg_ready()
    checks["postgres"] = {"ok": pg_ok, "detail": pg_detail}
    ok_all = ok_all and pg_ok

    if redis_enabled():
        redis_ok, redis_detail = await _probe_redis_ready()
        checks["redis"] = {"ok": redis_ok, "detail": redis_detail}
        ok_all = ok_all and redis_ok
    else:
        checks["redis"] = {"ok": True, "detail": "disabled"}

    q_ok, q_detail = await _probe_qdrant_ready()
    checks["qdrant"] = {"ok": q_ok, "detail": q_detail}
    ok_all = ok_all and q_ok

    # F4：/ready 经 _PUBLIC_PREFIXES【匿名可达】(容器 HEALTHCHECK/编排就绪门无 token)。生产(RBAC
    # 开)下若把 per-component up/down+detail 直接返给匿名调用方 = 基建拓扑信息泄漏(#21 同类)。探针
    # 只需 200/503 状态位；拓扑明细收敛到【需鉴权】的 /api/status。非生产(RBAC 关，dev/CI)保留 checks
    # 便于本地排障。读配置失败 → fail-closed 不暴露明细。
    _expose_detail = False
    try:
        _expose_detail = not get_config().rbac_enabled
    except Exception:  # noqa: BLE001
        _expose_detail = False
    body = ({"status": "ok" if ok_all else "unavailable", "checks": checks}
            if _expose_detail else {"status": "ok" if ok_all else "unavailable"})
    if not ok_all:
        return JSONResponse(status_code=503, content=body)
    return body


# ─── Auth / RBAC ───────────────────────────────────









# ─── 2. GET /api/status ────────────────────────────
@app.get("/api/status", tags=["系统"])
async def get_status(request: Request):
    """系统组件运行状态（8 个组件）。

    B8-F1：仅认证不足以放行内部坐标——非 admin 只得健康态（status），detail 掩空。
    RBAC-off 时 _require_user 返回 anonymous admin，is_admin=True，行为不变（开箱即用）。
    """
    from swarm.api._shared import _require_user
    from swarm.auth.rbac import Role
    _user = _require_user(request)
    _is_admin = _user.global_role == Role.ADMIN.value
    components = ["Brain 状态机", "Worker 执行器", "知识库", "记忆系统", "远程沙箱", "模型路由", "PostgreSQL", "Qdrant"]
    results = await asyncio.gather(*[_check_component(c, _is_admin) for c in components])
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
                # A1 批3：回读实例归属标签，供启动清扫按本实例过滤
                "metadata": getattr(sb, "metadata", None) or {},
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
async def get_stats(request: Request, project_id: str | None = None):
    """任务统计：总量、完成/失败/取消、Accept 率、平均耗时、token 估算、学习趋势、最近 10 条"""
    # P0-SEC-03：项目级统计须 project:read（防跨项目）；全局统计须认证用户。
    from swarm.api._shared import _require_perm, _require_user
    if project_id:
        _require_perm(request, "project:read", project_id)
        _scope_ids = None
    else:
        user = _require_user(request)
        # C2 治本：无 project_id → 限定到成员项目白名单（admin 返回 None=全量），杜绝跨项目
        # 任务元数据(description/token_usage/最近 10 条)泄露。对齐 /api/milestones 的 #5(a) 处理。
        _scope_ids = _accessible_project_ids_or_none(user, None, request)
    loop = asyncio.get_running_loop()
    if project_id:
        await loop.run_in_executor(None, _validate_project, project_id)

    stats = await loop.run_in_executor(
        None, lambda: store.get_task_stats(project_id, project_ids=_scope_ids))
    if project_id:
        stats["project_id"] = project_id
        # 项目级定制沙箱：附带当前项目专属模板 ID（系统配置好的，供项目统计页展示）。
        # 见 docs/Project_Scoped_Sandbox_Design.md。
        try:
            from swarm.project.store import get_project
            _proj = await loop.run_in_executor(None, lambda: get_project(project_id))
            _cfg = (_proj or {}).get("config") or {}
            stats["sandbox_template"] = _cfg.get("sandbox_template") or ""
            stats["sandbox_deps_hash"] = _cfg.get("sandbox_deps_hash") or ""
        except Exception:  # noqa: BLE001 — 读项目失败不影响统计返回
            stats["sandbox_template"] = ""
            stats["sandbox_deps_hash"] = ""
    return stats


@app.get("/api/projects/{project_id}/stats", tags=["系统"])
async def get_project_stats(project_id: str, request: Request):
    """项目 scoped 任务统计"""
    from swarm.api._shared import _require_perm
    _require_perm(request, "project:read", project_id)  # P0-SEC-03
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _validate_project, project_id)
    stats = await loop.run_in_executor(None, lambda: store.get_task_stats(project_id))
    stats["project_id"] = project_id
    return stats


@app.get("/api/stats/token-usage", tags=["系统"])
async def get_token_usage(request: Request):
    """LLM token 用量统计：云端 vs 本地分项 + 总计 + 每项目（数据落 PG，实时 flush 后读）。

    让运维看清实际烧了多少 token、云端/本地各多少、每个项目多少。全局视图 → 认证用户可见。
    """
    from swarm.api._shared import _require_user
    user = _require_user(request)
    # C3 治本：非 admin 限定到成员项目白名单，杜绝跨项目 token 用量泄露。
    _scope_ids = _accessible_project_ids_or_none(user, None, request)
    loop = asyncio.get_running_loop()
    from swarm.models import usage_tracker
    return await loop.run_in_executor(
        None, lambda: usage_tracker.get_token_usage_stats(project_ids=_scope_ids))


@app.get("/api/notifications", tags=["系统"])
async def get_notifications(
    request: Request,
    project_id: str | None = None,
    include_archived: bool = False,
    limit: int = 50,
):
    """应用内通知列表（持久化、可归档）。默认只返回未归档。"""
    from swarm.api._shared import _require_user_async
    user = await _require_user_async(request)  # A-P1-27：通知含任务/项目信息，需鉴权（D48 卸线程）
    # #19：防跨项目 IDOR。指定 project_id → 必须有该项目 project:read；未指定 → 限定到
    # 用户可访问项目集（admin 不受限，project_ids=None 表示全量）。D48：内部两条 PG 查询卸线程。
    _scope_ids = await asyncio.to_thread(_accessible_project_ids_or_none, user, project_id, request)
    loop = asyncio.get_running_loop()
    if project_id:
        await loop.run_in_executor(None, _validate_project, project_id)
    notifications = await loop.run_in_executor(
        None,
        lambda: store.list_notifications(
            project_id=project_id,
            project_ids=_scope_ids,
            include_archived=include_archived,
            limit=min(limit, 200),
        ),
    )
    unread = await loop.run_in_executor(
        None,
        lambda: store.count_unread_notifications(project_id=project_id, project_ids=_scope_ids),
    )
    return {"notifications": notifications, "unread_count": unread}


@app.get("/api/notifications/unread_count", tags=["系统"])
async def get_unread_count(request: Request, project_id: str | None = None):
    """未归档通知数（铃铛绿点轮询用，轻量）。"""
    from swarm.api._shared import _require_user_async
    user = await _require_user_async(request)  # A-P1-27（D48：15s 轮询面，卸线程）
    _scope_ids = await asyncio.to_thread(_accessible_project_ids_or_none, user, project_id, request)  # #19
    loop = asyncio.get_running_loop()
    count = await loop.run_in_executor(
        None,
        lambda: store.count_unread_notifications(project_id=project_id, project_ids=_scope_ids),
    )
    return {"unread_count": count}


@app.post("/api/notifications/{notification_id}/archive", tags=["系统"])
async def archive_notification_endpoint(notification_id: int, request: Request):
    """归档单条通知。"""
    from swarm.api._shared import _require_perm, _require_user
    from swarm.auth.rbac import Role
    user = _require_user(request)  # A-P1-27
    loop = asyncio.get_running_loop()
    # #19：先查该通知归属项目并鉴权（project:read），杜绝凭 id 越权归档他人/他项目通知。
    exists, notif_pid = await loop.run_in_executor(
        None, lambda: store.get_notification_project_id(notification_id)
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Notification not found")
    if notif_pid:
        _require_perm(request, "project:read", notif_pid)
    elif getattr(user, "global_role", None) != Role.ADMIN.value:
        # 无项目归属的全局通知：仅 admin 可归档（非 admin 无从主张归属）。
        raise HTTPException(status_code=403, detail="Permission denied")
    ok = await loop.run_in_executor(
        None,
        lambda: store.archive_notification(notification_id),
    )
    return {"status": "ok", "archived": ok}


@app.post("/api/notifications/archive_all", tags=["系统"])
async def archive_all_notifications_endpoint(request: Request, project_id: str | None = None):
    """归档全部未读通知（可选按项目过滤）。"""
    from swarm.api._shared import _require_user_async
    user = await _require_user_async(request)  # A-P1-27（D48 卸线程）
    _scope_ids = await asyncio.to_thread(_accessible_project_ids_or_none, user, project_id, request)  # #19
    loop = asyncio.get_running_loop()
    count = await loop.run_in_executor(
        None,
        lambda: store.archive_all_notifications(project_id=project_id, project_ids=_scope_ids),
    )
    return {"status": "ok", "archived_count": count}


@app.get("/api/milestones", tags=["系统"])
async def list_milestones(request: Request, project_id: str | None = None, limit: int = 10):
    """Accept 率基准历史报告（P0）。"""
    from swarm.api._shared import _require_perm, _require_user
    loop = asyncio.get_running_loop()
    # #5(a)：无 project_id 时过去任意登录用户读全库里程碑（跨项目 accept-rate 越权）。治本：
    #  - 指定 project_id → 校验该项目 task:read。
    #  - 无 project_id → admin 全量、非 admin 限成员项目（get_latest_milestone_reports 支持 project_ids）。
    if project_id:
        _require_perm(request, "task:read", project_id)
        reports = await loop.run_in_executor(
            None,
            lambda: store.get_latest_milestone_reports(project_id=project_id, limit=min(limit, 50)),
        )
    else:
        user = _require_user(request)
        from swarm.auth.rbac import Role
        from swarm.auth.store import list_user_project_ids
        _pids = None if getattr(user, "global_role", "") == Role.ADMIN.value \
            else list(list_user_project_ids(user.id))
        reports = await loop.run_in_executor(
            None,
            lambda: store.get_latest_milestone_reports(project_ids=_pids, limit=min(limit, 50)),
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
async def post_milestone_report(body: MilestoneReportBody, request: Request):
    """保存 benchmark 脚本产出的里程碑报告。"""
    from swarm.api._shared import _require_perm
    _require_perm(request, "config:write")  # A-P1-27：写里程碑需写权限
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
from swarm.api.routers import skills as _skills_router  # noqa: E402
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
app.include_router(_skills_router.router)
app.include_router(_observability_router.router)


def run() -> None:
    """console_scripts 入口（swarm-web）：以 uvicorn 启动 API。

    R23-8 治本：pyproject 的 `swarm-web` 原指向 ASGI 对象 `swarm.api.app:app`（非可调用入口），
    安装后执行 `swarm-web` 会报错。console_scripts 必须指向【无参可调用】。此处提供该入口，
    与 scripts/restart-api.sh 一致（uvicorn swarm.api.app:app，host/port 可 env 覆盖）。
    """
    import os
    import uvicorn
    uvicorn.run(
        "swarm.api.app:app",
        host=os.environ.get("SWARM_API_HOST", "0.0.0.0"),
        port=int(os.environ.get("SWARM_PORT", os.environ.get("SWARM_API_PORT", "8420"))),
        log_level=os.environ.get("SWARM_API_LOG_LEVEL", "info"),
    )
