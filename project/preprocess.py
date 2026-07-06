"""预处理编排器 — 4 阶段 pipeline: scan → index → embed → analyze

每个阶段更新 PreprocessProgress 到 PG，sleep 0.1 让 SSE 能推。
预处理在后台线程运行（不阻塞 API），同步操作通过 asyncio.to_thread 包装。

用法:
    await preprocess_project(project_id, project_path)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from swarm.config.settings import DatabaseConfig, ModelConfig

logger = logging.getLogger(__name__)


def _preprocess_timeout_sec() -> int:
    """预处理总超时秒数（P2 防永卡 PREPROCESSING，默认 3600，可配）。"""
    try:
        return max(60, int(os.environ.get("SWARM_PREPROCESS_TIMEOUT_SEC", "3600")))
    except ValueError:
        return 3600


# ──────────────────────────────────────────────
# 排除目录和文件
# ──────────────────────────────────────────────

EXCLUDED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".hg", ".svn", "dist", "build", "egg-info", ".eggs",
    ".next", ".nuxt", "target", "bin", "obj",
}

EXCLUDED_EXTENSIONS = {
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe",
    ".o", ".a", ".lib", ".woff", ".woff2", ".ttf", ".eot",
    ".ico", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg",
    ".mp3", ".mp4", ".wav", ".avi", ".mov",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".db", ".sqlite", ".sqlite3",
}

# 语言识别映射
LANGUAGE_MAP: dict[str, str] = {
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".java": "Java",
    ".kt": "Kotlin",
    ".scala": "Scala",
    ".go": "Go",
    ".rs": "Rust",
    ".c": "C",
    ".cpp": "C++",
    ".h": "C/C++ Header",
    ".hpp": "C++ Header",
    ".cs": "C#",
    ".rb": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".m": "Objective-C",
    ".r": "R",
    ".R": "R",
    ".sql": "SQL",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".lua": "Lua",
    ".pl": "Perl",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".erl": "Erlang",
    ".hs": "Haskell",
    ".ml": "OCaml",
    ".clj": "Clojure",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".html": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".less": "Less",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".xml": "XML",
    ".md": "Markdown",
    ".rst": "reStructuredText",
}


# ──────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────

async def preprocess_project(project_id: str, project_path: str) -> None:
    """异步预处理入口 — 在后台线程运行 4 阶段 pipeline

    Args:
        project_id: 项目 ID
        project_path: 项目根目录绝对路径
    """
    # 延迟导入避免循环引用
    from swarm.project.store import (
        update_project,
        upsert_progress,
    )

    logger.info("Starting preprocessing for project %s at %s", project_id, project_path)

    # 验证目录存在
    if not os.path.isdir(project_path):
        await asyncio.to_thread(
            upsert_progress,
            project_id,
            phase="error",
            phase_progress=0.0,
            message=f"Project path does not exist: {project_path}",
            error=f"Path not found: {project_path}",
        )
        await asyncio.to_thread(
            update_project,
            project_id,
            status="ERROR",
        )
        return

    # 初始化进度
    await asyncio.to_thread(
        upsert_progress,
        project_id,
        phase="scanning",
        phase_progress=0.0,
        message="Starting preprocessing...",
        started_at=datetime.now(),
        completed_at=None,
        error=None,
        scan_stats={},
        index_stats={},
        embed_stats={},
        analysis_stats={},
    )
    await asyncio.to_thread(
        update_project,
        project_id,
        status="PREPROCESSING",
    )

    async def _run_phases() -> tuple[dict, dict, dict]:
        # ── Phase 1: SCAN ──
        scan_result = await _phase_scan(project_id, project_path)
        # ── Phase 1.5: NORMS EXTRACT — 从配置文件自动提取项目规范 ──
        await _phase_extract_norms(project_id, project_path)
        # ── Phase 2: INDEX ──
        index_result = await _phase_index(project_id, project_path)
        # ── Phase 3: EMBED ──
        embed_result = await _phase_embed(project_id, project_path, index_result)
        # ── Phase 4: ANALYZE ──
        # _phase_analyze 内部已持久化摘要(_save_analysis_summary)与进度，返回的
        # 统计信息当前无需在此使用，故不接收返回值（避免 F841 死变量）。
        await _phase_analyze(project_id, project_path, scan_result)
        # ── Phase 5: BUILD SANDBOX（项目级定制沙箱）──
        # 按真实环境构建项目专属沙箱镜像 → 写 project.config["sandbox_template"]。
        # 构建失败不阻断预处理（回退通用池）。见 docs/Project_Scoped_Sandbox_Design.md。
        await _phase_build_sandbox(project_id, project_path)
        return scan_result, index_result, embed_result

    try:
        # P2：整段预处理设总超时（默认 3600s，可配 SWARM_PREPROCESS_TIMEOUT_SEC）。
        # 任一阶段挂死(如 embedding/沙箱构建端点 hang)→ TimeoutError → 下方 except 置 ERROR，
        # 避免项目【永卡 PREPROCESSING】无人能用(准入闸门只放行 READY)。
        scan_result, index_result, embed_result = await asyncio.wait_for(
            _run_phases(), timeout=_preprocess_timeout_sec()
        )

        # ── 完成 ──
        await asyncio.to_thread(
            upsert_progress,
            project_id,
            phase="complete",
            phase_progress=1.0,
            message="Preprocessing complete",
            completed_at=datetime.now(),
        )
        await asyncio.to_thread(
            update_project,
            project_id,
            status="READY",
            file_count=scan_result["file_count"],
            symbol_count=index_result.get("symbol_count", embed_result.get("vector_count", 0)),
            language_breakdown=scan_result["language_breakdown"],
        )
        logger.info("Preprocessing complete for project %s", project_id)

    except (TimeoutError, asyncio.TimeoutError):
        msg = f"预处理超时(>{_preprocess_timeout_sec()}s)，置 ERROR 避免永卡 PREPROCESSING"
        logger.error("Preprocessing TIMEOUT for project %s: %s", project_id, msg)
        await asyncio.to_thread(
            upsert_progress, project_id, phase="error", phase_progress=0.0,
            message=msg, error="preprocess_timeout",
        )
        await asyncio.to_thread(update_project, project_id, status="ERROR")
        return
    except Exception as exc:
        logger.exception("Preprocessing failed for project %s", project_id)
        await asyncio.to_thread(
            upsert_progress,
            project_id,
            phase="error",
            phase_progress=0.0,
            message=f"Preprocessing failed: {exc}",
            error=str(exc),
        )
        await asyncio.to_thread(
            update_project,
            project_id,
            status="ERROR",
        )


# ──────────────────────────────────────────────
# Phase 1: SCAN — 遍历目录，统计语言分布/行数/文件数
# ──────────────────────────────────────────────

def _scan_sync(project_path: str) -> dict[str, Any]:
    """同步扫描项目目录"""
    root = Path(project_path)
    file_count = 0
    dir_count = 0
    language_breakdown: dict[str, int] = {}
    line_counts: dict[str, int] = {}
    file_list: list[dict[str, Any]] = []

    for dirpath, dirnames, filenames in os.walk(root):
        # 排除特定目录（原地修改 dirnames 控制 os.walk 递归）
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS and not d.startswith(".")]

        abs_dir = Path(dirpath)
        rel_dir = abs_dir.relative_to(root)
        dir_count += 1

        for filename in filenames:
            ext = Path(filename).suffix.lower()
            if ext in EXCLUDED_EXTENSIONS:
                continue

            abs_file = abs_dir / filename
            rel_file = str(rel_dir / filename) if str(rel_dir) != "." else filename

            # 语言识别
            language = LANGUAGE_MAP.get(ext, "Other")

            # 统计
            file_count += 1
            language_breakdown[language] = language_breakdown.get(language, 0) + 1

            # 行数统计（对文本文件）
            lines = 0
            try:
                if ext not in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".ico",
                               ".woff", ".woff2", ".ttf", ".eot", ".mp3", ".mp4",
                               ".zip", ".tar", ".gz", ".db", ".sqlite"}:
                    with open(abs_file, "r", encoding="utf-8", errors="ignore") as f:
                        lines = sum(1 for _ in f)
            except (OSError, PermissionError):
                pass
            line_counts[language] = line_counts.get(language, 0) + lines

            # 文件 hash
            file_hash = ""
            try:
                if abs_file.stat().st_size < 10 * 1024 * 1024:  # < 10MB
                    file_hash = _md5_file(abs_file)
            except (OSError, PermissionError):
                pass

            file_list.append({
                "rel_path": rel_file,
                "abs_path": str(abs_file),
                "language": language,
                "lines": lines,
                "hash": file_hash,
            })

    return {
        "file_count": file_count,
        "dir_count": dir_count,
        "language_breakdown": language_breakdown,
        "line_counts": line_counts,
        "files": file_list,
    }


async def _phase_scan(project_id: str, project_path: str) -> dict[str, Any]:
    """Phase 1: 扫描文件结构"""
    from swarm.project.store import update_project, upsert_progress

    await asyncio.to_thread(
        upsert_progress,
        project_id,
        phase="scanning",
        phase_progress=0.0,
        message="Scanning project files...",
    )
    await asyncio.sleep(0.1)

    # 在线程中执行同步扫描
    scan_result = await asyncio.to_thread(_scan_sync, project_path)

    total = scan_result["file_count"]
    # §3.4 假动作治理：扫描此刻已真实完成——原"模拟逐步进度"循环（分批写进度+sleep）
    # 是纯表演动画（白占 ~1s + N 次 DB 写）。诚实上报一次完成态。
    await asyncio.to_thread(
        upsert_progress,
        project_id,
        phase="scanning",
        phase_progress=1.0,
        message=f"Scanned {total}/{total} files",
    )

    # 保存扫描结果到 kb_file_index
    await asyncio.to_thread(
        _save_file_index, project_id, scan_result["files"]
    )

    # 最终更新
    scan_stats = {
        "files": scan_result["file_count"],
        "dirs": scan_result["dir_count"],
        "languages": list(scan_result["language_breakdown"].keys()),
        "line_counts": scan_result["line_counts"],
    }
    await asyncio.to_thread(
        upsert_progress,
        project_id,
        phase="scanning",
        phase_progress=1.0,
        message=f"Scan complete: {scan_result['file_count']} files, {scan_result['dir_count']} dirs",
        scan_stats=scan_stats,
    )
    await asyncio.to_thread(
        update_project,
        project_id,
        graph_status="NONE",
        file_count=scan_result["file_count"],
    )
    await asyncio.sleep(0.1)

    return scan_result


# ──────────────────────────────────────────────
# Phase 2: INDEX — CodeGraph 索引
# ──────────────────────────────────────────────

async def _phase_index(project_id: str, project_path: str) -> dict[str, Any]:
    """Phase 2: CodeGraph 索引"""
    from swarm.project.store import update_project, upsert_progress

    await asyncio.to_thread(
        upsert_progress,
        project_id,
        phase="indexing",
        phase_progress=0.0,
        message="Checking codegraph CLI...",
    )
    await asyncio.sleep(0.1)

    # 检查 codegraph 是否安装
    is_installed = await asyncio.to_thread(_check_codegraph)

    if not is_installed:
        await asyncio.to_thread(
            upsert_progress,
            project_id,
            phase="indexing",
            phase_progress=1.0,
            message="codegraph CLI not installed, skipping",
            index_stats={"skipped": True, "reason": "CLI not installed"},
        )
        await asyncio.to_thread(
            update_project,
            project_id,
            graph_status="NONE",
        )
        await asyncio.sleep(0.1)
        return {"symbol_count": 0, "edge_count": 0, "skipped": True}

    await asyncio.to_thread(
        upsert_progress,
        project_id,
        phase="indexing",
        phase_progress=0.1,
        message="Running codegraph init...",
    )
    await asyncio.sleep(0.1)

    # 运行 codegraph (在后台线程)
    await asyncio.to_thread(
        update_project,
        project_id,
        graph_status="INDEXING",
    )

    cg_result = await asyncio.to_thread(_run_codegraph, project_path)

    await asyncio.to_thread(
        upsert_progress,
        project_id,
        phase="indexing",
        phase_progress=0.7,
        message=f"Indexed {cg_result.symbol_count} symbols, {cg_result.edge_count} edges",
    )
    await asyncio.sleep(0.1)

    # 将符号写入 kb_symbol_index
    if cg_result.symbols:
        await asyncio.to_thread(
            _save_symbol_index, project_id, cg_result.symbols
        )

    # 将依赖写入 kb_dependency_graph
    if cg_result.edges:
        await asyncio.to_thread(
            _save_dependency_graph, project_id, cg_result.edges
        )

    # P1-25 对账：全量重索引后清除磁盘已不存在文件的残留符号(整文件删除的幽灵符号)。
    await asyncio.to_thread(_prune_absent_files, project_id, project_path)

    # P1-21：据 cg_result.ok 判终态。成功(含真空项目 0 符号)→ INDEXED；索引失败/部分
    # (init/index 失败、db 缺失、解析异常)→ DEGRADED，据实反映，不把失败当完成。
    cg_ok = getattr(cg_result, "ok", True)
    index_stats = {
        "symbols": cg_result.symbol_count,
        "edges": cg_result.edge_count,
        "time_ms": cg_result.time_ms,
        "ok": cg_ok,
    }
    if not cg_ok:
        index_stats["error"] = getattr(cg_result, "error", None)
    _status = "INDEXED" if cg_ok else "DEGRADED"
    _msg = (
        f"Index complete: {cg_result.symbol_count} symbols"
        if cg_ok
        else f"Index degraded: {getattr(cg_result, 'error', 'codegraph failed')}"
    )
    await asyncio.to_thread(
        upsert_progress,
        project_id,
        phase="indexing",
        phase_progress=1.0,
        message=_msg,
        index_stats=index_stats,
    )
    await asyncio.to_thread(
        update_project,
        project_id,
        graph_status=_status,
        graph_progress=1.0,
        symbol_count=cg_result.symbol_count,
    )
    await asyncio.sleep(0.1)

    return {
        "symbol_count": cg_result.symbol_count,
        "edge_count": cg_result.edge_count,
        "symbols": cg_result.symbols,
        "skipped": False,
    }


# ──────────────────────────────────────────────
# Phase 3: EMBED — 向量嵌入到 Qdrant
# ──────────────────────────────────────────────

async def _phase_embed(
    project_id: str,
    project_path: str,
    index_result: dict[str, Any],
) -> dict[str, Any]:
    """Phase 3: 读取 kb_symbol_index, bge-m3 嵌入, 存 Qdrant"""
    from swarm.project.store import upsert_progress

    await asyncio.to_thread(
        upsert_progress,
        project_id,
        phase="embedding",
        phase_progress=0.0,
        message="Loading symbols for embedding...",
    )
    await asyncio.sleep(0.1)

    # Qdrant 不可用时跳过（不阻断 scan/index/analyze）
    qdrant_ok = await asyncio.to_thread(_check_qdrant)
    if not qdrant_ok:
        logger.warning("[EMBED] Qdrant unavailable — skipping vector embedding for project %s", project_id)
        await asyncio.to_thread(
            upsert_progress,
            project_id,
            phase="embedding",
            phase_progress=1.0,
            message="Qdrant unavailable (connection refused), skipping embedding",
            embed_stats={"vectors": 0, "dim": 0, "skipped": True, "reason": "qdrant_unavailable"},
        )
        await asyncio.sleep(0.1)
        return {"vector_count": 0, "dim": 0, "skipped": True}

    # 从 PG 读取符号
    symbols = await asyncio.to_thread(_read_symbols_for_embed, project_id)

    if not symbols:
        await asyncio.to_thread(
            upsert_progress,
            project_id,
            phase="embedding",
            phase_progress=1.0,
            message="No symbols to embed",
            embed_stats={"vectors": 0, "dim": 0},
        )
        await asyncio.sleep(0.1)
        return {"vector_count": 0, "dim": 0}

    await asyncio.to_thread(
        upsert_progress,
        project_id,
        phase="embedding",
        phase_progress=0.1,
        message=f"Embedding {len(symbols)} symbols...",
    )
    await asyncio.sleep(0.1)

    # 生成嵌入向量
    texts = _build_embed_texts(symbols)
    vectors = await asyncio.to_thread(_embed_texts, texts)

    # audit A-P0-1：嵌入服务不可用时 _embed_texts 返回 None（拒绝写随机向量）。
    # 此处必须跳过 upsert，并把阶段标记为 degraded/skipped，绝不报成功污染 KB。
    if vectors is None or len(vectors) != len(symbols):
        reason = (
            "embedding service unavailable"
            if vectors is None
            else f"vector/symbol count mismatch ({len(vectors)} != {len(symbols)})"
        )
        logger.error(
            "[EMBED] skipping Qdrant upsert for project %s — %s", project_id, reason
        )
        await asyncio.to_thread(
            upsert_progress,
            project_id,
            phase="embedding",
            phase_progress=1.0,
            message=f"Embedding skipped: {reason}",
            embed_stats={"vectors": 0, "dim": 0, "skipped": True, "reason": reason},
        )
        await asyncio.sleep(0.1)
        return {"vector_count": 0, "dim": 0, "skipped": True, "reason": reason}

    dim = len(vectors[0]) if vectors else 0

    # P1-9：实际向量维度须与配置维度一致（也是 SemanticIndexer 建集合所用维度）。不符→
    # fail-closed，标 degraded 跳过 upsert，绝不把错维向量写进共享集合（否则 Qdrant 拒/语义错乱）。
    from swarm.config.settings import KnowledgeConfig
    expected_dim = int(getattr(KnowledgeConfig(), "embed_dimension", 1024))
    if dim and dim != expected_dim:
        reason = f"embedding dim {dim} != configured {expected_dim}"
        logger.error(
            "[EMBED] skipping Qdrant upsert for project %s — %s（换 embedding 模型？"
            "核对 SWARM_KB_EMBED_DIMENSION）", project_id, reason,
        )
        await asyncio.to_thread(
            upsert_progress,
            project_id,
            phase="embedding",
            phase_progress=1.0,
            message=f"Embedding skipped: {reason}",
            embed_stats={"vectors": 0, "dim": dim, "skipped": True, "reason": reason},
        )
        await asyncio.sleep(0.1)
        return {"vector_count": 0, "dim": dim, "skipped": True, "reason": reason}

    def _embed_progress_cb(progress: float, message: str) -> None:
        upsert_progress(
            project_id,
            phase="embedding",
            phase_progress=round(progress, 3),
            message=message,
        )

    # 存入 Qdrant
    await asyncio.to_thread(
        _store_vectors_qdrant,
        project_id,
        symbols,
        vectors,
        dim,
        _embed_progress_cb,
    )

    embed_stats = {
        "vectors": len(vectors),
        "dim": dim,
    }
    await asyncio.to_thread(
        upsert_progress,
        project_id,
        phase="embedding",
        phase_progress=1.0,
        message=f"Embedding complete: {len(vectors)} vectors",
        embed_stats=embed_stats,
    )
    await asyncio.sleep(0.1)

    return {"vector_count": len(vectors), "dim": dim}


# ──────────────────────────────────────────────
# Phase 1.5: NORMS EXTRACT — 从配置文件自动提取项目规范
# ──────────────────────────────────────────────

async def _phase_extract_norms(project_id: str, project_path: str) -> None:
    """Phase 1.5: 扫描项目配置文件，提取编码规范写入 NormsStore"""
    from swarm.project.store import upsert_progress

    await asyncio.to_thread(
        upsert_progress,
        project_id,
        phase="scanning",
        phase_progress=1.0,
        message="Extracting project norms from config files...",
    )

    try:
        from swarm.knowledge.norms_extractor import extract_norms_from_project
        norms = await asyncio.to_thread(extract_norms_from_project, project_path)

        # Phase 1.6: 从【实际代码】推断工程惯例（资深工程师读代码），补 config 提取的不足。
        # 老项目无 .editorconfig/.ruff.toml 时 config 提取=0，inferred 是主要来源。
        inferred: list = []
        try:
            from swarm.knowledge.norms_inference import infer_norms_from_code
            proj_name = ""
            try:
                from swarm.project import store as _pstore
                p = _pstore.get_project(project_id)
                proj_name = (p or {}).get("name", "") if p else ""
            except Exception:  # noqa: BLE001
                pass
            inferred = await asyncio.to_thread(infer_norms_from_code, project_path, proj_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("norms 代码推断失败(不阻断) %s: %s", project_id, exc)

        if not norms and not inferred:
            logger.info("No norms (config or inferred) for project %s", project_id)
            return

        # 写入 NormsStore — config 提取的标 'auto'，代码推断的标 'inferred'，各自幂等替换
        from swarm.config.settings import DatabaseConfig
        from swarm.knowledge.norms_store import NormsStore

        store = NormsStore(DatabaseConfig())
        await store.connect()
        try:
            if norms:
                deleted = await store.delete_norms_by_tag(project_id, "auto")
                if deleted:
                    logger.info("Cleared %d old auto norms for project %s", deleted, project_id)
                ids = await store.add_norms_batch(project_id, norms)
                logger.info("Inserted %d auto(config) norms for project %s", len(ids), project_id)
            if inferred:
                deleted_inf = await store.delete_norms_by_tag(project_id, "inferred")
                if deleted_inf:
                    logger.info("Cleared %d old inferred norms for project %s", deleted_inf, project_id)
                # add_norms_batch 用各 Norm 自身的 tag（这里是 'inferred'），不被覆盖
                inf_ids = await store.add_norms_batch(project_id, inferred)
                logger.info("Inserted %d inferred(code) norms for project %s", len(inf_ids), project_id)
        finally:
            await store.close()

    except Exception as exc:
        # 规范提取失败不阻断预处理
        logger.warning("Norms extraction failed for project %s: %s", project_id, exc)


# ──────────────────────────────────────────────
# Phase 5: BUILD SANDBOX — 项目级定制沙箱
# ──────────────────────────────────────────────
async def _phase_build_sandbox(project_id: str, project_path: str) -> None:
    """按项目真实环境构建专属沙箱镜像（方案 B：自带完整源码），写 project.config。

    见 docs/DESIGN_project_sandbox_prebake_source.md。
    通用主流程：所有有构建文件的项目预处理时都精准构建专属沙箱（不分语言、装齐工具链、
    源码进 /workspace）。失败不阻断预处理（回退通用池）。
    开关 config.sandbox.project_scoped_enabled 默认 True（设 False 可全局关闭回退旧池）。
    """
    from swarm.config.settings import get_config
    cfg = get_config()
    if not getattr(cfg.sandbox, "project_scoped_enabled", True):
        logger.info("项目 %s: project_scoped 已显式关闭，跳过专属沙箱（用通用池）", project_id)
        return

    try:
        from swarm.project.sandbox_spec import infer_env_spec
        from swarm.project.store import get_project, update_project, upsert_progress
        from swarm.worker.image_builder import (
            SSHConfig,
            build_project_image,
            compute_project_fingerprint,
            template_exists_in_cubemaster,
        )

        spec = infer_env_spec(project_path, project_id=project_id)
        if spec.base_only:
            logger.info("项目 %s 无构建文件(全新项目)，跳过专属沙箱，等首个任务需求分析", project_id)
            return

        # 双指纹（deps + 源码树）：依赖或源码变了才重建（方案 B）。
        fingerprint = await asyncio.to_thread(compute_project_fingerprint, spec, project_path)
        proj = get_project(project_id) or {}
        existing = (proj.get("config") or {})
        if existing.get("sandbox_template") and existing.get("sandbox_deps_hash") == fingerprint:
            # 复用前探活：CubeMaster 模板会因 TTL 过期/存储清理而消失，DB 记录却仍在
            # （实测 task 82f12ce4：tpl-2ebae48 被清，复用悬空引用→worker 创建沙箱必报
            # 130404）。只有模板【确认存在】(True) 才复用；【确认不存在】(False) 继续往下重建；
            # 探活失败(None) 保守复用（避免网络抖动触发昂贵重建），但告警。
            _exists = await asyncio.to_thread(
                template_exists_in_cubemaster, existing["sandbox_template"]
            )
            if _exists is True:
                logger.info("项目 %s 依赖+源码未变且模板存在，复用专属模板 %s",
                            project_id, existing["sandbox_template"])
                return
            if _exists is None:
                logger.warning("项目 %s 模板 %s 探活失败（无法判定存在性），保守复用；"
                               "若后续创建沙箱报 template_not_found 请手动触发重新预处理",
                               project_id, existing["sandbox_template"])
                return
            logger.warning("项目 %s 的专属模板 %s 在 CubeMaster 已不存在（悬空引用，疑似过期/被清），"
                           "重建专属沙箱", project_id, existing["sandbox_template"])

        if SSHConfig.from_secret_store() is None:
            logger.warning("项目 %s: 沙箱机 SSH 凭据未配置，跳过专属沙箱构建（回退通用池）", project_id)
            return

        # building_sandbox 阶段通知（构建耗时不定，前端可见；任务此时只能入池等待，见调度器闸门）
        await asyncio.to_thread(
            upsert_progress, project_id,
            phase="building_sandbox", phase_progress=0.0,
            message=f"构建项目专属沙箱（工具链 {[t.name for t in spec.toolchains]}），耗时数分钟，期间任务仅入池等待…",
        )
        logger.info("项目 %s 开始构建专属沙箱(自带源码): %s", project_id, [t.name for t in spec.toolchains])
        result = await asyncio.to_thread(build_project_image, spec, project_path)
        if result.ok and result.template_id:
            new_config = {**existing,
                          "sandbox_template": result.template_id,
                          "sandbox_deps_hash": fingerprint}
            await asyncio.to_thread(update_project, project_id, config=new_config)
            await asyncio.to_thread(
                upsert_progress, project_id,
                phase="building_sandbox", phase_progress=1.0,
                message=f"项目专属沙箱就绪：{result.template_id}",
            )
            logger.info("项目 %s 专属沙箱就绪: %s", project_id, result.template_id)
        else:
            await asyncio.to_thread(
                upsert_progress, project_id,
                phase="building_sandbox", phase_progress=1.0,
                message=f"专属沙箱构建失败，回退通用池：{result.message[:120]}",
            )
            logger.warning("项目 %s 专属沙箱构建失败(回退通用池): %s", project_id, result.message)
    except Exception as exc:  # noqa: BLE001 — 构建失败不阻断预处理
        logger.warning("项目 %s 专属沙箱构建异常(回退通用池): %s", project_id, exc)


# ──────────────────────────────────────────────
# Phase 4: ANALYZE — LLM 生成项目摘要
# ──────────────────────────────────────────────

async def _phase_analyze(
    project_id: str,
    project_path: str,
    scan_result: dict[str, Any],
) -> dict[str, Any]:
    """Phase 4: 调本地 MiniMax-M2.7-Pro 生成项目摘要"""
    from swarm.project.store import upsert_progress

    await asyncio.to_thread(
        upsert_progress,
        project_id,
        phase="analyzing",
        phase_progress=0.0,
        message="Analyzing project architecture...",
    )
    await asyncio.sleep(0.1)

    # 构建分析输入
    analysis_input = _build_analysis_input(project_path, scan_result)

    await asyncio.to_thread(
        upsert_progress,
        project_id,
        phase="analyzing",
        phase_progress=0.2,
        message="Calling LLM for project summary...",
    )
    await asyncio.sleep(0.1)

    # 调用本地 MiniMax 模型
    summary = await asyncio.to_thread(_call_local_llm, analysis_input)

    await asyncio.to_thread(
        upsert_progress,
        project_id,
        phase="analyzing",
        phase_progress=0.8,
        message="Storing analysis results...",
    )
    await asyncio.sleep(0.1)

    # 将摘要存入 PG 项目描述（或mem_user_profile）
    analysis_stats = {
        "summary_tokens": len(summary.split()) if summary else 0,
        "entities": len(analysis_input.get("key_files", [])),
    }
    await asyncio.to_thread(
        _save_analysis_summary, project_id, summary
    )

    await asyncio.to_thread(
        upsert_progress,
        project_id,
        phase="analyzing",
        phase_progress=1.0,
        message="Analysis complete",
        analysis_stats=analysis_stats,
    )
    await asyncio.sleep(0.1)

    return {"summary": summary, "tokens": analysis_stats["summary_tokens"]}


# ════════════════════════════════════════════════
# 同步辅助函数
# ════════════════════════════════════════════════

def _md5_file(path: Path) -> str:
    """计算文件 MD5"""
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
    except (OSError, PermissionError):
        return ""
    return h.hexdigest()


def _check_codegraph() -> bool:
    """检查 codegraph 是否安装"""
    from swarm.project.codegraph import is_codegraph_installed
    return is_codegraph_installed()


def _check_qdrant() -> bool:
    """检查 Qdrant 是否在线"""
    try:
        import httpx
        cfg = DatabaseConfig()
        url = cfg.qdrant_url.rstrip("/")
        resp = httpx.get(f"{url}/collections", timeout=3.0)
        return resp.status_code == 200
    except Exception as exc:
        logger.warning("Qdrant health check failed: %s", exc)
        return False


def _run_codegraph(project_path: str):
    """运行 codegraph 全流程"""
    from swarm.project.codegraph import run_codegraph_full
    return run_codegraph_full(project_path)


def _save_file_index(project_id: str, files: list[dict[str, Any]]) -> None:
    """将扫描结果写入 kb_file_index

    A-P1-22：用连接池 + executemany。原先每次 psycopg.connect(autocommit) 绕过池、
    异常时只在成功路径 conn.close() → 抛错即泄漏连接；逐行 execute 多次往返。
    """
    if not files:
        return
    try:
        import psycopg

        from swarm.infra.db import sync_pool
        rows = [
            (
                project_id,
                f["rel_path"],
                f["language"],
                f["hash"],
                psycopg.types.json.Jsonb({"lines": f["lines"], "abs_path": f["abs_path"]}),
            )
            for f in files
        ]
        with sync_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO kb_file_index (project_id, file_path, language, file_hash, metadata_json)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (project_id, file_path) DO UPDATE SET
                        language = EXCLUDED.language,
                        file_hash = EXCLUDED.file_hash,
                        metadata_json = EXCLUDED.metadata_json,
                        last_modified = NOW()
                    """,
                    rows,
                )
    except Exception as exc:
        logger.warning("Failed to save file index: %s", exc)


def _save_symbol_index(project_id: str, symbols: list) -> None:
    """将 codegraph 符号写入 kb_symbol_index（A-P1-22：池 + executemany）"""
    if not symbols:
        return
    try:
        import psycopg

        from swarm.infra.db import sync_pool
        rows = [
            (
                project_id,
                sym.file_path,
                sym.name,
                sym.symbol_type,
                sym.start_line,
                sym.end_line,
                sym.signature,
                sym.docstring,
                sym.class_name,
                psycopg.types.json.Jsonb({}),
            )
            for sym in symbols
        ]
        fresh_files = sorted({sym.file_path for sym in symbols})
        with sync_pool().connection() as conn:
            # 复核 storage#2 治本：sync_pool 连接是 autocommit，注释声称"同事务原子"实则 DELETE 与
            # INSERT 各自提交——INSERT 失败会把该批文件符号清空。显式 conn.transaction() 让 DELETE+
            # INSERT 真原子(全成或全回滚)，与 project/store.delete_project 一致。
            with conn.transaction():
                with conn.cursor() as cur:
                    # P1-25：本批重新索引的文件【先整体删旧符号，再插 fresh 集】(同事务原子)。
                    # 纯 upsert 只增不删——文件内被删除的符号会残留成幽灵符号；delete-then-insert
                    # 让每个重新索引的文件的符号集权威覆盖，清掉已移除的符号。
                    cur.execute(
                        "DELETE FROM kb_symbol_index WHERE project_id = %s AND file_path = ANY(%s)",
                        (project_id, fresh_files),
                    )
                    cur.executemany(
                    """
                    INSERT INTO kb_symbol_index
                        (project_id, file_path, symbol_name, symbol_type,
                         start_line, end_line, signature, docstring, class_name, metadata_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (project_id, file_path, symbol_name, symbol_type) DO UPDATE SET
                        start_line = EXCLUDED.start_line,
                        end_line = EXCLUDED.end_line,
                        signature = EXCLUDED.signature,
                        docstring = EXCLUDED.docstring,
                        class_name = EXCLUDED.class_name,
                        metadata_json = EXCLUDED.metadata_json
                    """,
                    rows,
                )
    except Exception as exc:
        logger.warning("Failed to save symbol index: %s", exc)


def _prune_absent_files(project_id: str, project_path: str) -> int:
    """P1-25 对账：删除 kb_symbol_index / kb_dependency_graph 中工作区磁盘已不存在的文件行。

    全量 preprocess 后调用，清理【整文件删除】残留的幽灵符号（delete-then-insert 只覆盖
    仍产生符号的文件，删掉的文件不在 fresh 集里、需靠磁盘对账清）。
    fail-closed：只删磁盘【确已不存在】的文件，绝不误伤仍存在的文件行；异常吞掉不阻断预处理。
    file_path 相对/绝对均可：Path(base)/绝对路径 == 绝对路径，相对则落在项目下。
    """
    try:
        from pathlib import Path

        from swarm.infra.db import sync_pool
        base = Path(project_path)
        # fail-closed 破坏范围最小化：project_path 为空/不是现存目录（沙箱未挂载、目录被移走、
        # 调用方传空/相对路径）时【绝不】对账——否则每个 file_path 都判"不存在"→整表被清空。
        # 宁可漏清一次(下次正常预处理再对账)，也不能误删权威索引。
        if not project_path or not base.is_dir():
            logger.warning(
                "[preprocess] P1-25 对账跳过：project_path 不是现存目录(%r)，避免误删整表索引",
                project_path,
            )
            return 0
        with sync_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT file_path FROM kb_symbol_index WHERE project_id = %s",
                    (project_id,),
                )
                indexed = [r[0] for r in cur.fetchall()]
                absent = [fp for fp in indexed if fp and not (base / fp).exists()]
                if not absent:
                    return 0
                cur.execute(
                    "DELETE FROM kb_symbol_index WHERE project_id = %s AND file_path = ANY(%s)",
                    (project_id, absent),
                )
                cur.execute(
                    "DELETE FROM kb_dependency_graph WHERE project_id = %s "
                    "AND (source_file = ANY(%s) OR target_file = ANY(%s))",
                    (project_id, absent, absent),
                )
        logger.info(
            "[preprocess] P1-25 对账：从符号索引清除 %d 个磁盘已不存在的文件 (project=%s)",
            len(absent), project_id,
        )
        return len(absent)
    except Exception as exc:
        logger.warning("Failed to prune absent files from symbol index: %s", exc)
        return 0


_DEP_INSERT_SQL = """
INSERT INTO kb_dependency_graph (project_id, source_file, target_file, import_type, metadata_json)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (project_id, source_file, target_file) DO UPDATE SET
    import_type = EXCLUDED.import_type,
    metadata_json = EXCLUDED.metadata_json
"""


def _dep_edge_rows(project_id: str, edges: list) -> list:
    """把 codegraph edges 展平为 kb_dependency_graph 行（单一事实源，供 save/replace 共用）。"""
    import psycopg
    return [
        (project_id, edge.source_file, edge.target_file, edge.import_type,
         psycopg.types.json.Jsonb({}))
        for edge in edges
    ]


def _save_dependency_graph(project_id: str, edges: list) -> None:
    """将 codegraph 依赖写入 kb_dependency_graph（A-P1-22：池 + executemany；纯 upsert，不删旧边）。"""
    if not edges:
        return
    try:
        from swarm.infra.db import sync_pool
        with sync_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(_DEP_INSERT_SQL, _dep_edge_rows(project_id, edges))
    except Exception as exc:
        logger.warning("Failed to save dependency graph: %s", exc)


def _replace_dependency_graph(project_id: str, edges: list) -> None:
    """C1 治本：在【同一 sync 事务】内 DELETE 旧边 + INSERT 新边，原子替换某项目依赖图。

    原 updater 重建把 DELETE 走 async 连接、INSERT 走本 sync 池 → 跨连接非原子，中途崩溃
    只删不写 → 依赖图空、检索劣化。合到单事务：任一步失败整体回滚，绝不留空/半图。
    edges 空也执行 DELETE（显式清空该项目依赖图，语义正确；调用方按需在空时早返回）。
    """
    try:
        from swarm.infra.db import sync_pool
        rows = _dep_edge_rows(project_id, edges)
        with sync_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM kb_dependency_graph WHERE project_id = %s",
                        (project_id,),
                    )
                    if rows:
                        cur.executemany(_DEP_INSERT_SQL, rows)
    except Exception as exc:
        logger.warning("Failed to replace dependency graph: %s", exc)


def _read_symbols_for_embed(project_id: str) -> list[dict[str, Any]]:
    """从 kb_symbol_index 读取符号（用于嵌入）（A-P1-22：池，with 不泄漏）"""
    try:
        from swarm.infra.db import sync_pool
        with sync_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT file_path, symbol_name, symbol_type, start_line, end_line,
                           signature, docstring, class_name
                    FROM kb_symbol_index
                    WHERE project_id = %s
                    ORDER BY file_path, start_line
                    """,
                    (project_id,),
                )
                rows = cur.fetchall()
        return [
            {
                "file_path": r[0],
                "name": r[1],
                "symbol_type": r[2],
                "start_line": r[3],
                "end_line": r[4],
                "signature": r[5],
                "docstring": r[6],
                "class_name": r[7],
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("Failed to read symbols for embed: %s", exc)
        return []


def _build_embed_texts(symbols: list[dict[str, Any]]) -> list[str]:
    """为符号构建嵌入文本: 签名 + 文档 + 前5行上下文"""
    texts: list[str] = []
    for sym in symbols:
        parts: list[str] = []

        # 签名
        if sym.get("signature"):
            parts.append(sym["signature"])
        else:
            type_str = sym.get("symbol_type", "function")
            name = sym.get("name", "")
            cls = sym.get("class_name")
            if cls and type_str == "method":
                parts.append(f"{cls}.{name}")
            else:
                parts.append(f"{type_str} {name}")

        # 文档字符串
        if sym.get("docstring"):
            parts.append(sym["docstring"])

        # 文件位置
        file_path = sym.get("file_path", "")
        start_line = sym.get("start_line")
        if file_path:
            loc = f"in {file_path}"
            if start_line:
                loc += f":{start_line}"
            parts.append(loc)

        text = " | ".join(parts)
        texts.append(text)
    return texts


def _embed_texts(texts: list[str]) -> list[list[float]] | None:
    """使用 bge-m3 嵌入文本列表

    优先级：专用 embed 服务(SWARM_KB_EMBED_BASE_URL) → sentence-transformers →
    本地 LLM 网关 → SiliconFlow。专用服务最稳(真 bge-m3,归一化向量)，放第一位。
    """
    from swarm.config.settings import KnowledgeConfig
    dim = int(getattr(KnowledgeConfig(), "embed_dimension", 1024))  # 单一来源(bge-m3=1024)

    # 尝试 0: 专用 embedding 服务（统一客户端，OpenAI 兼容 /embeddings，ai.bit:8082）
    try:
        from swarm.knowledge.embed_client import embed_texts_sync
        vecs = embed_texts_sync(texts)
        if vecs is not None:
            return vecs
    except Exception as exc:
        logger.warning("专用 embed 服务调用失败(回退): %s", exc)

    # 尝试 1: sentence-transformers
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("BAAI/bge-m3")
        embeddings = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
        return embeddings.tolist()
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("sentence-transformers embedding failed: %s", exc)

    # 尝试 2: HTTP API (本地 embedding 服务) —— 用配置的 local_base_url(不再硬编码 localhost)
    try:
        import requests
        from swarm.config.settings import KnowledgeConfig, ModelConfig
        mcfg = ModelConfig()
        emb_model = KnowledgeConfig().embedding_model
        base = mcfg.local_base_url.rstrip("/")
        headers = {}
        if mcfg.local_api_key:
            headers["Authorization"] = f"Bearer {mcfg.local_api_key}"
        resp = requests.post(
            f"{base}/embeddings",
            json={"model": emb_model, "input": texts},
            headers=headers,
            timeout=120,
        )
        if resp.status_code == 200:
            data = resp.json()
            return [d["embedding"] for d in data.get("data", [])]
    except Exception as exc:
        logger.warning("HTTP embedding API failed: %s", exc)

    # 尝试 3: OpenAI-compatible API —— 同样用配置端点
    try:
        from openai import OpenAI
        from swarm.config.settings import KnowledgeConfig, ModelConfig
        mcfg = ModelConfig()
        emb_model = KnowledgeConfig().embedding_model
        client = OpenAI(base_url=mcfg.local_base_url, api_key=mcfg.local_api_key or "dummy")
        response = client.embeddings.create(model=emb_model, input=texts)
        return [d.embedding for d in response.data]
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("OpenAI-compatible embedding API failed: %s", exc)

    # audit A-P0-1：无嵌入服务时【绝不】返回随机占位向量。随机向量被写入 Qdrant
    # 会永久污染 KB（检索结果是噪声但阶段报成功），属严重数据完整性事故。
    # 改为返回 None 表示“嵌入失败”，由调用方跳过 upsert 并标记 degraded。
    # 仅当显式设置 SWARM_ALLOW_RANDOM_EMBED=1（本地纯测试）时才保留随机回退。
    import os
    if os.getenv("SWARM_ALLOW_RANDOM_EMBED") == "1":
        logger.warning(
            "No embedding service available — SWARM_ALLOW_RANDOM_EMBED=1, "
            "using random vectors (DEV ONLY, will pollute KB if used in prod)."
        )
        import random
        random.seed(42)
        return [[random.gauss(0, 1) for _ in range(dim)] for _ in texts]
    logger.error(
        "No embedding service available — refusing to write random placeholder "
        "vectors (would poison KB). Skipping embedding. Configure "
        "sentence-transformers or an embedding API for this to succeed."
    )
    return None


def _store_vectors_qdrant(
    project_id: str,
    symbols: list[dict[str, Any]],
    vectors: list[list[float]],
    dim: int,
    progress_callback=None,
) -> None:
    """将向量存入 Qdrant 统一集合 swarm_kb（与 SemanticIndexer 检索对齐）"""
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    from swarm.knowledge.semantic_index import (
        INDEX_SOURCE_CODEGRAPH,
        INDEX_VERSION,
    )

    cfg = DatabaseConfig()
    collection_name = cfg.qdrant_collection
    client = QdrantClient(url=cfg.qdrant_url, check_compatibility=False)

    existing = [c.name for c in client.get_collections().collections]
    if collection_name not in existing:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )

    # #2：write-then-prune（不再"先删后写"）。先 upsert 全部新向量（稳定 ID 覆盖同符号），
    # 再按代际删除本项目残留的旧向量。避免"先删后写"在 upsert 中途崩溃时留下空/残检索窗口。
    # 崩溃后重跑：稳定 ID 幂等覆盖 + 末尾 prune 兜底，最终一致可恢复。
    import time as _time
    from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue
    _index_generation = str(_time.time_ns())

    batch_size = 100
    total = len(symbols)
    for i in range(0, total, batch_size):
        batch_symbols = symbols[i : i + batch_size]
        batch_vectors = vectors[i : i + batch_size]

        points = []
        for j, (sym, vec) in enumerate(zip(batch_symbols, batch_vectors)):
            name = sym.get("name", "")
            signature = sym.get("signature") or ""
            docstring = sym.get("docstring") or ""
            content = " | ".join(p for p in [signature, docstring, name] if p)
            payload = {
                "project_id": project_id,
                "content": content,
                "chunk_type": "symbol",
                "name": name,
                "symbol_type": sym.get("symbol_type", ""),
                "file_path": sym.get("file_path", ""),
                "start_line": sym.get("start_line"),
                "end_line": sym.get("end_line"),
                "signature": signature,
                "docstring": docstring,
                "class_name": sym.get("class_name") or "",
                # 索引溯源（12.4）：标记本路径为预处理全量 CodeGraph 符号嵌入
                "index_version": INDEX_VERSION,
                "index_source": INDEX_SOURCE_CODEGRAPH,
                # #2：本次重建代际，末尾据此 prune 掉旧代际残留
                "index_generation": _index_generation,
            }
            # P1-DEBT-04：与增量(semantic)路径共用同一 ID 方案（make_point_id），
            # 同一 (file,line,content) 产同一 point ID → 两路径可互相 upsert 去重，
            # 不再因 int/uuid 双方案不相交导致同集合内召回漂移。
            from swarm.knowledge.semantic_index import make_point_id
            stable_id = make_point_id(
                sym.get("file_path", ""), sym.get("start_line", 0), content
            )
            points.append(
                PointStruct(id=stable_id, vector=vec, payload=payload)
            )

        client.upsert(collection_name=collection_name, points=points)

        if progress_callback:
            progress = min((i + batch_size) / max(total, 1), 1.0)
            progress_callback(
                progress,
                f"Storing vectors {min(i + batch_size, total)}/{total}...",
            )

    # #2：prune——全部新向量落盘后，删本项目【非本代际】的残留旧向量。
    # 仅在新数据就位后执行，故检索全程有数据（无空窗）；崩在 upsert 中途则不 prune，
    # 留待下次全量重建幂等收敛。symbols 为空(total=0)时本删除即清空本项目【CodeGraph 代际】向量。
    # ★F2 治本★：prune 必须限定 index_source=CODEGRAPH——否则"project_id 且 非本代际"会连带删掉
    # 【增量语义路径(semantic)刚写入的 chunk】(它们代际不同、index_source 不同)，造成增量索引被
    # 全量重建静默清空（并发/交错时尤甚）。限定来源后，全量重建只回收自己这一路的旧代际残留，
    # 与增量路径在同集合内井水不犯河水（两路 make_point_id 一致仍可互相幂等去重）。
    client.delete(
        collection_name=collection_name,
        points_selector=FilterSelector(
            filter=Filter(
                must=[
                    FieldCondition(key="project_id", match=MatchValue(value=project_id)),
                    FieldCondition(key="index_source", match=MatchValue(value=INDEX_SOURCE_CODEGRAPH)),
                ],
                must_not=[FieldCondition(
                    key="index_generation", match=MatchValue(value=_index_generation)
                )],
            )
        ),
    )


def _build_analysis_input(project_path: str, scan_result: dict[str, Any]) -> dict[str, Any]:
    """构建 LLM 分析输入"""
    root = Path(project_path)

    # 目录树（限制深度）
    tree = _build_directory_tree(root, max_depth=3)

    # 读取 README
    readme_content = ""
    for readme_name in ["README.md", "README.rst", "README.txt", "README"]:
        readme_path = root / readme_name
        if readme_path.exists():
            try:
                readme_content = readme_path.read_text(encoding="utf-8", errors="ignore")[:5000]
            except (OSError, PermissionError):
                pass
            break

    # 核心文件（入口文件、配置文件等）
    key_files: list[str] = []
    for pattern in [
        "main.py", "app.py", "manage.py", "setup.py", "pyproject.toml",
        "package.json", "Cargo.toml", "go.mod", "Makefile", "Dockerfile",
        "docker-compose.yml", "docker-compose.yaml",
    ]:
        p = root / pattern
        if p.exists():
            key_files.append(pattern)

    return {
        "tree": tree,
        "readme": readme_content,
        "key_files": key_files,
        "language_breakdown": scan_result.get("language_breakdown", {}),
        "file_count": scan_result.get("file_count", 0),
        "line_counts": scan_result.get("line_counts", {}),
    }


def _build_directory_tree(root: Path, max_depth: int = 3, prefix: str = "") -> str:
    """构建目录树字符串表示"""
    lines: list[str] = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return prefix + "<permission denied>"

    # 排除隐藏和忽略目录
    entries = [
        e for e in entries
        if e.name not in EXCLUDED_DIRS and not e.name.startswith(".")
    ]

    dirs = [e for e in entries if e.is_dir()]
    files = [e for e in entries if e.is_file()][:20]  # 限制文件数量展示

    for d in dirs:
        lines.append(f"{prefix}{d}/")
        if max_depth > 1:
            lines.append(_build_directory_tree(d, max_depth - 1, prefix + "  "))

    for f in files:
        lines.append(f"{prefix}{f.name}")

    if len(entries) > len(dirs) + len(files):
        lines.append(f"{prefix}... ({len(entries) - len(dirs) - len(files)} more)")

    return "\n".join(lines)


def _call_local_llm(analysis_input: dict[str, Any]) -> str:
    """调本地 MiniMax-M2.7-Pro 生成项目摘要"""
    from swarm.tracing import PHASE_2, is_langsmith_active

    if is_langsmith_active():
        try:
            from langsmith.run_helpers import traceable

            return traceable(
                name="preprocess/architecture-llm",
                run_type="llm",
                tags=["swarm", f"swarm-{PHASE_2}", "swarm-preprocess"],
            )(_call_local_llm_impl)(analysis_input)
        except Exception as exc:
            logger.debug("LangSmith trace skipped for preprocess LLM: %s", exc)
    return _call_local_llm_impl(analysis_input)


def _call_local_llm_impl(analysis_input: dict[str, Any]) -> str:
    """preprocess Phase 4 analyze — 实际 LLM 调用"""
    model_config = ModelConfig()
    prompt = f"""Please analyze this project and generate a comprehensive summary.

## Directory Structure
```
{analysis_input['tree']}
```

## README
{analysis_input['readme'] or 'No README found'}

## Key Files
{', '.join(analysis_input['key_files']) or 'None detected'}

## Language Breakdown
{analysis_input['language_breakdown']}

## Statistics
- Total files: {analysis_input['file_count']}
- Line counts: {analysis_input['line_counts']}

Please provide:
1. Project Architecture Summary
2. Core Module Dependencies
3. Entry Functions / Critical Paths
4. Coding Conventions / Testing Conventions
"""

    # 尝试 OpenAI-compatible API 调用本地模型
    try:
        from openai import OpenAI
        client = OpenAI(
            base_url=model_config.local_base_url,
            api_key=model_config.local_api_key or "dummy",
        )
        response = client.chat.completions.create(
            model="MiniMax-M2.7-Pro",
            messages=[
                {"role": "system", "content": "You are a software architecture analyst. Provide concise, structured project analysis."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=2000,
        )
        return response.choices[0].message.content or ""
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("Local LLM API call failed: %s", exc)

    # 回退: 尝试 SiliconFlow API
    try:
        from openai import OpenAI
        client = OpenAI(
            base_url=model_config.siliconflow_base_url,
            api_key=model_config.siliconflow_api_key,
        )
        response = client.chat.completions.create(
            model="Pro/zai-org/GLM-5.1",
            messages=[
                {"role": "system", "content": "You are a software architecture analyst. Provide concise, structured project analysis."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=2000,
        )
        return response.choices[0].message.content or ""
    except Exception as exc:
        logger.warning("SiliconFlow API call failed: %s", exc)

    # 最终回退: 基于统计信息生成基本摘要
    logger.warning("All LLM APIs unavailable — generating basic summary from statistics")
    langs = analysis_input.get("language_breakdown", {})
    lang_str = ", ".join(f"{k} ({v} files)" for k, v in sorted(langs.items(), key=lambda x: -x[1]))
    return (
        f"Project with {analysis_input.get('file_count', 0)} files. "
        f"Languages: {lang_str}. "
        f"Key files: {', '.join(analysis_input.get('key_files', []))}. "
        f"Auto-generated summary (LLM unavailable)."
    )


def _clean_llm_summary(text: str) -> str:
    """去掉模型 thinking 标签，保留可读摘要"""
    import re
    cleaned = re.sub(
        r"<think>[\s\S]*?</think>",
        "",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"<thinking>.*?</thinking>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


def _save_analysis_summary(project_id: str, summary: str) -> None:
    """保存分析摘要到项目描述"""
    try:
        from swarm.project.store import update_project
        cleaned = _clean_llm_summary(summary)
        update_project(project_id, description=cleaned[:4000])
    except Exception as exc:
        logger.warning("Failed to save analysis summary: %s", exc)
