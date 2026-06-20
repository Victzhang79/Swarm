"""统一采集管线 — source.list → fetch → parse → 切分 →(dry_run 预览 / 否则灌库)。

安全默认: dry_run=True。除非显式传 dry_run=False,管线绝不调 index_chunks 落真实 KB。
这是防"测试/误操作偷偷写生产语义库"的最后一道闸门。

解析复用生产解析器 brain/ingest.parse_file(md/txt/pdf/docx/html + 图片/扫描件标记)，
不再自造一份格式解析。它自带安全加固：格式白名单、20MB 上限、解析超时、按类型分流。
远端来源给的是 bytes(无路径)，spool 到带正确后缀的临时文件再交给 parse_file，
让白名单/大小/解析逻辑对远端内容同样生效；临时文件用后即删。

切分复用 SemanticIndexer.chunk_source_code(chunk_size/chunk_overlap 来自 KnowledgeConfig),
不自造切分逻辑。产出的 Chunk 结构与 index_chunks 的入参完全一致。
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from swarm.brain.ingest import ParsedDocument, parse_file
from swarm.knowledge.ingest.sources import DocRef, SourceAdapter
from swarm.knowledge.semantic_index import Chunk, SemanticIndexer

logger = logging.getLogger(__name__)


@dataclass
class DocResult:
    """单篇文档的采集结果。"""

    doc_id: str
    title: str | None
    filename: str
    status: str                       # "parsed" | "skipped" | "error"
    num_chunks: int = 0
    error: str | None = None
    # dry_run 时携带 chunk 预览(内容截断),非 dry_run 时为空以省内存
    chunk_preview: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class IngestReport:
    """整次采集的汇总。"""

    source_name: str
    project_id: str
    dry_run: bool
    total_docs: int = 0
    parsed_docs: int = 0
    skipped_docs: int = 0
    failed_docs: int = 0
    total_chunks: int = 0
    indexed_chunks: int = 0          # 实际落库的 chunk 数(dry_run 恒为 0)
    docs: list[DocResult] = field(default_factory=list)


def _virtual_file_path(source_name: str, ref_or_doc_id: str) -> str:
    """为非本地来源构造稳定的虚拟 file_path(进入 chunk.file_path / point id)。

    本地来源 doc_id 即真实路径,直接用;远端用 `ingest://<source>/<doc_id>` 命名空间,
    避免与代码库 file_path 撞 point id。
    """
    if source_name == "local":
        return ref_or_doc_id
    return f"ingest://{source_name}/{ref_or_doc_id}"


def _parse_bytes(data: bytes, filename: str) -> ParsedDocument:
    """把 bytes spool 到带正确后缀的临时文件，交给 brain/ingest.parse_file。

    复用 parse_file 的白名单/大小/超时/分流逻辑。临时文件用后必删。
    """
    suffix = Path(filename).suffix
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(data)
        tmp.flush()
        tmp.close()
        return parse_file(tmp.name)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:  # pragma: no cover - 删除失败不影响解析结果
            pass


def _doc_to_chunks(
    parsed: ParsedDocument,
    *,
    file_path: str,
    source_name: str,
    title: str | None,
    extra_meta: dict[str, Any],
    chunk_size: int,
    chunk_overlap: int,
) -> list[Chunk]:
    """把解析文本切成 Chunk(复用 SemanticIndexer 切分),并打上采集元数据。"""
    chunks = SemanticIndexer.chunk_source_code(
        parsed.text,
        file_path=file_path,
        module_name=None,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    ingest_meta = {
        "ingest_source": source_name,
        "doc_title": title,
        "source_type": parsed.kind,
        "ext": parsed.ext,
        "num_pages": parsed.page_count,
        **extra_meta,
    }
    for c in chunks:
        # 采集来的资料保留切分判定的 chunk_type，附加来源元数据
        c.metadata = {**ingest_meta, **(c.metadata or {})}
    return chunks


def _preview(chunks: list[Chunk], limit: int, content_chars: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in chunks[:limit]:
        out.append(
            {
                "chunk_type": c.chunk_type,
                "file_path": c.file_path,
                "start_line": c.start_line,
                "content": (c.content or "")[:content_chars],
                "metadata": c.metadata,
            }
        )
    return out


async def ingest(
    source: SourceAdapter,
    *,
    project_id: str,
    indexer: SemanticIndexer | None = None,
    dry_run: bool = True,
    chunk_size: int = 512,
    chunk_overlap: int = 50,
    preview_chunks_per_doc: int = 3,
    preview_content_chars: int = 200,
) -> IngestReport:
    """采集一个来源的全部文档进 KB。

    source:     任意满足 SourceAdapter 协议的适配器(LocalFileSource / 远端 stub)。
    project_id: 目标项目(落库时写入 payload.project_id)。
    indexer:    非 dry_run 时必须传(已 connect 的 SemanticIndexer)。dry_run 时忽略。
    dry_run:    True(默认)只解析+切分+返回预览,绝不调 index_chunks;
                False 才真正向量化落库 —— 调用方需自负风险并显式开启。

    解析委托给 brain/ingest.parse_file：
      - 有 text          → parsed（切分 + 预览/落库）。
      - 有 error         → skipped（不支持格式 / 超大 / 解析失败等已知原因）。
      - needs_vision 无文本 → skipped（图片/扫描件，待多模态层处理，本管线不灌）。

    返回 IngestReport(逐文档状态 + 汇总)。
    """
    report = IngestReport(
        source_name=getattr(source, "source_name", "unknown"),
        project_id=project_id,
        dry_run=dry_run,
    )

    if not dry_run and indexer is None:
        raise ValueError("dry_run=False 时必须提供已连接的 SemanticIndexer(indexer=...)")

    refs: list[DocRef] = source.list_documents()
    report.total_docs = len(refs)

    for ref in refs:
        try:
            fetched = source.fetch(ref.doc_id)
        except Exception as e:  # noqa: BLE001 - 单篇失败不应中断整批
            report.failed_docs += 1
            report.docs.append(
                DocResult(
                    doc_id=ref.doc_id,
                    title=ref.title,
                    filename=ref.doc_id,
                    status="error",
                    error=f"fetch 失败: {e}",
                )
            )
            continue

        try:
            parsed = _parse_bytes(fetched.data, fetched.filename)
        except Exception as e:  # noqa: BLE001 - parse_file 本身已吞异常，这是兜底
            report.failed_docs += 1
            report.docs.append(
                DocResult(
                    doc_id=ref.doc_id,
                    title=fetched.title or ref.title,
                    filename=fetched.filename,
                    status="error",
                    error=f"解析异常: {e}",
                )
            )
            continue

        # brain/ingest 把已知失败（不支持格式/超大/解析失败）记在 error，不抛 → 视作 skipped
        if parsed.error:
            report.skipped_docs += 1
            report.docs.append(
                DocResult(
                    doc_id=ref.doc_id,
                    title=fetched.title or ref.title,
                    filename=fetched.filename,
                    status="skipped",
                    error=parsed.error,
                )
            )
            continue

        # 图片/扫描件：无确定性文本，本管线不灌（留给多模态层），记为 skipped
        if not parsed.text:
            report.skipped_docs += 1
            report.docs.append(
                DocResult(
                    doc_id=ref.doc_id,
                    title=fetched.title or ref.title,
                    filename=fetched.filename,
                    status="skipped",
                    error="无可灌文本（图片/扫描件需多模态理解，本管线跳过）"
                    if parsed.needs_vision
                    else "解析文本为空",
                )
            )
            continue

        file_path = _virtual_file_path(report.source_name, ref.doc_id)
        title = fetched.title or ref.title or Path(fetched.filename).stem
        chunks = _doc_to_chunks(
            parsed,
            file_path=file_path,
            source_name=report.source_name,
            title=title,
            extra_meta={k: v for k, v in (ref.metadata or {}).items()},
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

        report.parsed_docs += 1
        report.total_chunks += len(chunks)

        result = DocResult(
            doc_id=ref.doc_id,
            title=title,
            filename=fetched.filename,
            status="parsed",
            num_chunks=len(chunks),
        )

        if dry_run:
            result.chunk_preview = _preview(
                chunks, preview_chunks_per_doc, preview_content_chars
            )
        else:
            # 仅在显式 dry_run=False 时才触达真实 KB
            assert indexer is not None
            n = await indexer.index_chunks(project_id, chunks)
            report.indexed_chunks += n

        report.docs.append(result)

    logger.info(
        "ingest 完成 source=%s project=%s dry_run=%s docs=%d parsed=%d skipped=%d failed=%d "
        "chunks=%d indexed=%d",
        report.source_name, project_id, dry_run, report.total_docs, report.parsed_docs,
        report.skipped_docs, report.failed_docs, report.total_chunks, report.indexed_chunks,
    )
    return report
