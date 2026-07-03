"""api/routers/upload.py — 文件上传端点（设计 v3 B批3，B.3 安全）。

任务创建前先上传文件 → 存到 workspace/uploads/<batch_id>/（路径隔离）→ 返回路径列表，
前端创建任务时带上这些路径（task_records.uploaded_files）。

安全限制（B.3）：
  - 格式白名单（仅 png/jpg/webp/pdf/docx/md/txt）—— 复用 ingest.ALLOWED_EXTENSIONS。
  - 单文件大小上限 + 总大小上限 + 文件数上限。
  - 路径隔离：每批一个隔离目录，文件名 sanitize（禁止 ../ 穿越）。
  - MIME 校验（不只信扩展名）：用文件头魔数粗校验。
  - 上传需 task:create 权限（与建任务同权限）。
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

import swarm.api.app as _app
from swarm.api._shared import _require_perm
from swarm.brain import ingest as doc_ingest

logger = logging.getLogger(__name__)

router = APIRouter()

# 总量限制（B.3）
MAX_FILES_PER_BATCH = 10
MAX_TOTAL_BYTES = 60 * 1024 * 1024   # 单批总大小 60MB

# MIME 魔数（文件头）粗校验：扩展名 → 合法文件头前缀（任一匹配即通过）。
# 不只信扩展名（B.3）。文本类不校验魔数（无固定头）。
_MAGIC_BYTES: dict[str, list[bytes]] = {
    ".pdf": [b"%PDF"],
    ".png": [b"\x89PNG"],
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".webp": [b"RIFF"],   # RIFF....WEBP
    ".docx": [b"PK\x03\x04"],  # docx 是 zip
}

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\u4e00-\u9fff-]")


def _uploads_root() -> Path:
    from swarm.config.settings import get_config

    root = Path(get_config().workspace_root) / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


# #5(b) LFI 防护 helper 的单一事实源在 brain 层（避免 brain→api 反向依赖），此处 re-export
# 供上传/建任务端点与测试沿用同一判据。
from swarm.brain.ingest import path_is_within_uploads  # noqa: E402,F401


def _sanitize_filename(name: str) -> str:
    """清洗文件名：去路径成分、替换危险字符，防 ../ 穿越。"""
    # 只取最后的文件名部分（去掉任何路径）
    base = Path(name).name
    # 替换危险字符
    cleaned = _SAFE_NAME.sub("_", base)
    # 防空名 / 纯点
    if not cleaned or cleaned.strip(".") == "":
        cleaned = "upload"
    return cleaned[:120]  # 限长


def _check_magic(ext: str, head: bytes) -> bool:
    """文件头魔数校验。文本类（无魔数定义）直接通过。"""
    expected = _MAGIC_BYTES.get(ext)
    if not expected:
        return True  # 文本类等无魔数要求
    return any(head.startswith(sig) for sig in expected)


@router.post("/api/uploads", tags=["任务管理"])
async def upload_files(request: Request):
    """上传一批文件，返回隔离存储后的路径列表（供创建任务时引用）。

    multipart/form-data，字段名 files（可多个）。
    返回 {batch_id, dir, files:[{filename, path, kind, size, error}]}。
    """
    _require_perm(request, "task:create")

    form = await request.form()
    upload_files_list = form.getlist("files")
    if not upload_files_list:
        raise HTTPException(status_code=400, detail="未提供文件（字段名 files）")
    if len(upload_files_list) > MAX_FILES_PER_BATCH:
        raise HTTPException(
            status_code=400,
            detail=f"文件数超限：{len(upload_files_list)} > {MAX_FILES_PER_BATCH}",
        )

    batch_id = uuid.uuid4().hex[:16]
    batch_dir = _uploads_root() / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    total_bytes = 0

    for item in upload_files_list:
        # 鸭子类型判断：上传文件对象有 filename + read（FastAPI/Starlette UploadFile 不同类）
        if not (hasattr(item, "filename") and hasattr(item, "read")):
            continue
        raw_name = item.filename or "upload"
        safe_name = _sanitize_filename(raw_name)
        ext = Path(safe_name).suffix.lower()

        # 1) 扩展名白名单
        if ext not in doc_ingest.ALLOWED_EXTENSIONS:
            results.append({"filename": raw_name, "error": f"不支持的格式: {ext}"})
            continue

        # P2：先读后判 size → 巨型文件被全量读进内存后才拒绝(内存放大/可被滥用打爆)。
        # Starlette UploadFile 暴露 .size（来自 multipart 部分长度），先据此预检，超限直接拒绝
        # 不读内容；预检通过/size 未知时再 read（此时单文件上限已基本可控）。
        declared = getattr(item, "size", None)
        if isinstance(declared, int):
            if declared > doc_ingest.DEFAULT_MAX_FILE_BYTES:
                results.append({"filename": raw_name, "error": f"文件过大: {declared / 1024 / 1024:.1f}MB"})
                continue
            if total_bytes + declared > MAX_TOTAL_BYTES:
                results.append({"filename": raw_name, "error": "批次总大小超限"})
                continue

        # 读内容（一次性；上方已据 declared size 预拦超限文件）
        content = await item.read()
        size = len(content)
        total_bytes += size

        # 2) 单文件大小（declared 缺失或谎报时的兜底实测校验）
        if size > doc_ingest.DEFAULT_MAX_FILE_BYTES:
            results.append({"filename": raw_name, "error": f"文件过大: {size / 1024 / 1024:.1f}MB"})
            continue
        if size == 0:
            results.append({"filename": raw_name, "error": "空文件"})
            continue
        # 3) 总大小
        if total_bytes > MAX_TOTAL_BYTES:
            results.append({"filename": raw_name, "error": "批次总大小超限"})
            continue

        # 4) MIME 魔数校验（不只信扩展名）
        if not _check_magic(ext, content[:16]):
            results.append({"filename": raw_name, "error": f"文件内容与扩展名 {ext} 不符（MIME 校验失败）"})
            continue

        # 5) 落盘到隔离目录
        dest = batch_dir / safe_name
        # 重名加序号避免覆盖
        n = 1
        while dest.exists():
            dest = batch_dir / f"{dest.stem}_{n}{dest.suffix}"
            n += 1
        dest.write_bytes(content)

        kind = (
            "image" if ext in doc_ingest.IMAGE_EXTENSIONS
            else "text" if ext in doc_ingest.TEXT_EXTENSIONS
            else ext.lstrip(".")
        )
        results.append({
            "filename": safe_name,
            "path": str(dest.resolve()),
            "kind": kind,
            "size": size,
        })

    ok_files = [r for r in results if "path" in r]
    logger.info("[UPLOAD] 批次 %s：%d 成功 / %d 总数", batch_id, len(ok_files), len(results))
    return {
        "batch_id": batch_id,
        "dir": str(batch_dir.resolve()),
        "files": results,
        "ok_count": len(ok_files),
    }
