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
import os
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

import swarm.api.app as _app
from swarm.api._shared import _require_perm
from swarm.api.rate_limit import rate_limit  # C8
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


def _max_body_bytes() -> int:
    """D46：全局请求 body 上限（字节）。env SWARM_UPLOAD_MAX_BODY_BYTES，非法值回退默认
    = 批次总大小上限 + 8MB multipart 封包余量；钳制下限 1KB。"""
    default = MAX_TOTAL_BYTES + 8 * 1024 * 1024
    raw = os.environ.get("SWARM_UPLOAD_MAX_BODY_BYTES", "")
    try:
        val = int(raw) if raw else default
    except ValueError:
        val = default
    return max(1024, val)


def _enforce_body_limit(request) -> None:
    """D46 治本：在 request.form()（解析完整 multipart 进内存/磁盘）之前预检 body 大小。

    - Content-Length 超上限 → 413（解析前即拒，DoS 面收敛到一个 header 比较）；
    - 无 Content-Length（chunked）→ 411 fail-closed 拒绝。取舍：浏览器/httpx/curl 的
      multipart 上传总是带 Content-Length；支持无长度流式上传需换流式 parser 带增量配额，
      当前上传面（PRD/附件，≤60MB/批）不值得该复杂度，宁可显式拒绝也不承担无界解析。
    Content-Length 由 HTTP 服务器（uvicorn）强制执行，客户端谎报小值只会被截断，不构成绕过。
    """
    cl = request.headers.get("content-length")
    if cl is None:
        raise HTTPException(status_code=411, detail="缺少 Content-Length：不支持 chunked 上传")
    try:
        n = int(cl)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="非法 Content-Length") from None
    limit = _max_body_bytes()
    if n > limit:
        raise HTTPException(status_code=413, detail=f"请求体过大：{n} > {limit} 字节")


async def _read_upload_limited(item, per_file_cap: int, remaining_batch: int):
    """D46：分块读上传文件并【增量】校验大小，超限立即停读。

    返回 (content, overflow)：overflow=True 表示超过单文件上限或批次剩余额度（content=None，
    不再继续消费流）。旧码在 size 缺失时先 read() 整个文件进内存后才判 60MB——谎报/缺失
    size 的巨型文件构成内存放大面。read 不支持带参（非 UploadFile 鸭子对象）时退化为
    整读后判限（仍有上限兜底，只失去增量性）。
    """
    cap = min(per_file_cap, remaining_batch)
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = await item.read(1024 * 1024)
        except TypeError:
            data = await item.read()
            if len(data) > cap:
                return None, True
            return data, False
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            return None, True
        chunks.append(chunk)
    return b"".join(chunks), False


def _check_magic(ext: str, head: bytes) -> bool:
    """文件头魔数校验。文本类（无魔数定义）直接通过。"""
    expected = _MAGIC_BYTES.get(ext)
    if not expected:
        return True  # 文本类等无魔数要求
    # F13：WebP 是 RIFF 容器族——仅校验 `RIFF` 前缀会放行任意 wav/avi/其它 RIFF 文件。真正的 WebP
    # 头是 `RIFF`(0..4) + 4字节长度 + `WEBP`(8..12)。须同时校验 form-type，防伪装绕过。
    if ext == ".webp":
        return head.startswith(b"RIFF") and head[8:12] == b"WEBP"
    return any(head.startswith(sig) for sig in expected)


@router.post("/api/uploads", tags=["任务管理"],
             dependencies=[Depends(rate_limit("uploads", capacity=10, rate=0.2))])  # C8
async def upload_files(request: Request):
    """上传一批文件，返回隔离存储后的路径列表（供创建任务时引用）。

    multipart/form-data，字段名 files（可多个）。
    返回 {batch_id, dir, files:[{filename, path, kind, size, error}]}。
    """
    _require_perm(request, "task:create")

    # D46：解析 multipart 之前先做全局 body 上限预检（413/411），见 _enforce_body_limit。
    _enforce_body_limit(request)

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

        # 读内容——D46：分块读 + 增量校验（declared 缺失/谎报防线），超限即断不再消费流。
        content, _overflow = await _read_upload_limited(
            item, doc_ingest.DEFAULT_MAX_FILE_BYTES, MAX_TOTAL_BYTES - total_bytes,
        )
        if _overflow:
            results.append({"filename": raw_name, "error": "文件过大或批次总大小超限（增量校验截断）"})
            continue
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
