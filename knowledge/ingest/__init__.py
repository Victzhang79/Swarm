"""knowledge.ingest — 可插拔的知识库多源采集模块。

两层（解析复用生产解析器，不再自维护一份）:
  sources.py  : 来源适配器(LocalFileSource 真用,飞书/腾讯文档/语雀 stub)。
  pipeline.py : 统一管线 ingest(source, project_id, dry_run=True),默认 dry_run 防误灌；
                解析委托 brain/ingest.parse_file(md/txt/pdf/docx/html + 图片标记)。

安全默认: pipeline.ingest 默认 dry_run=True,绝不偷偷写生产语义库。
"""

from __future__ import annotations

from swarm.knowledge.ingest.pipeline import IngestReport, DocResult, ingest
from swarm.knowledge.ingest.sources import (
    DocRef,
    FetchedDoc,
    FeishuSource,
    LocalFileSource,
    RemoteSourceStub,
    SourceAdapter,
    TencentDocSource,
    YuqueSource,
    supported_extensions,
)

__all__ = [
    # sources
    "SourceAdapter",
    "DocRef",
    "FetchedDoc",
    "LocalFileSource",
    "RemoteSourceStub",
    "FeishuSource",
    "TencentDocSource",
    "YuqueSource",
    "supported_extensions",
    # pipeline
    "ingest",
    "IngestReport",
    "DocResult",
]
