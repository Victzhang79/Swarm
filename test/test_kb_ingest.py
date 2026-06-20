"""knowledge.ingest 采集模块测试。

铁律:
  - 绝不真调 index_chunks 落生产 KB。pipeline 默认 dry_run=True;
    非 dry_run 路径用 mock 的 SemanticIndexer 验证调用,不连真实 Qdrant。
  - 只用已装库造样本(fitz 造 PDF、python-docx 造 docx、html/md 直接字符串)。
  - 远端三源在缺 token 时断言抛清晰 NotImplementedError。

注:格式解析复用生产解析器 brain/ingest.parse_file（md/txt/pdf/docx/html + 图片/扫描件标记），
本模块不再自维护一份解析器。parse_file 各格式的解析正确性由 test/test_ingest.py 覆盖；
这里只补一个 brain/ingest._parse_html 的最小用例（新加的 html 分支），其余只测
sources（遍历/抓取/远端 stub）+ pipeline（dry_run 不落库、复用 brain/ingest）。
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from swarm.brain.ingest import parse_file
from swarm.knowledge.ingest import (
    LocalFileSource,
    FeishuSource,
    TencentDocSource,
    YuqueSource,
    ingest,
    supported_extensions,
)


# ── 样本制造 ──────────────────────────────────────────────────────────


def _make_md(path: Path) -> None:
    path.write_text("# 标题ABC\n\n这是 markdown 正文 hello-world。\n", encoding="utf-8")


def _make_html(path: Path) -> None:
    path.write_text(
        "<html><head><title>页面标题XYZ</title></head>"
        "<body><h1>大标题</h1><p>段落内容 foobar。</p>"
        "<ul><li>项目一</li><li>项目二</li></ul>"
        "<script>var ignored = 'SHOULD_NOT_APPEAR';</script>"
        "</body></html>",
        encoding="utf-8",
    )


def _make_pdf(path: Path) -> None:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "PDF唯一文本 ZZTOP")
    doc.save(str(path))
    doc.close()


def _make_docx(path: Path) -> None:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_heading("DOCX标题Heading", level=1)
    document.add_paragraph("docx 段落正文 unique-docx-text。")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "h1"
    table.cell(0, 1).text = "h2"
    table.cell(1, 0).text = "v1"
    table.cell(1, 1).text = "v2"
    document.save(str(path))


# ── brain/ingest 新增的 _parse_html 分支最小验证 ─────────────────────


def test_brain_ingest_parses_html_strips_script_keeps_structure(tmp_path):
    """验证给 brain/ingest 新加的 html 分支：去 script/style、保留标题与列表换行。"""
    p = tmp_path / "page.html"
    _make_html(p)
    doc = parse_file(p)
    assert doc.error == ""
    assert doc.kind == "html"
    assert "段落内容 foobar。" in doc.text
    assert "项目一" in doc.text
    assert "项目二" in doc.text
    # script 内容整段丢弃
    assert "SHOULD_NOT_APPEAR" not in doc.text
    # 列表项前补了换行（保留结构感）
    assert "- 项目一" in doc.text
    # title 标签文本不应混入正文标记（仅 body 文本进 text）
    assert "大标题" in doc.text


def test_supported_extensions_includes_html(tmp_path):
    exts = supported_extensions()
    for e in (".md", ".pdf", ".docx", ".html", ".htm"):
        assert e in exts


# ── LocalFileSource 测试 ──────────────────────────────────────────────


def test_local_file_source_lists_and_fetches(tmp_path):
    _make_md(tmp_path / "a.md")
    _make_html(tmp_path / "b.html")
    (tmp_path / "ignore.xyz").write_text("nope", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    _make_md(sub / "c.md")

    src = LocalFileSource(tmp_path)
    refs = src.list_documents()
    names = {Path(r.doc_id).name for r in refs}
    assert {"a.md", "b.html", "c.md"} <= names  # 递归 + 扩展过滤
    assert "ignore.xyz" not in names            # 不在白名单 → 不列

    ref = next(r for r in refs if Path(r.doc_id).name == "a.md")
    fetched = src.fetch(ref.doc_id)
    assert b"markdown" in fetched.data
    assert fetched.filename == "a.md"


def test_local_file_source_single_file(tmp_path):
    f = tmp_path / "only.md"
    _make_md(f)
    src = LocalFileSource(f)
    refs = src.list_documents()
    assert len(refs) == 1
    assert Path(refs[0].doc_id).name == "only.md"


def test_local_file_source_missing_path(tmp_path):
    src = LocalFileSource(tmp_path / "nope")
    with pytest.raises(FileNotFoundError):
        src.list_documents()


# ── pipeline dry_run 端到端(不落库) ─────────────────────────────────


async def test_pipeline_dry_run_does_not_index(tmp_path):
    _make_md(tmp_path / "a.md")
    _make_html(tmp_path / "b.html")
    pytest.importorskip("fitz")
    _make_pdf(tmp_path / "c.pdf")
    pytest.importorskip("docx")
    _make_docx(tmp_path / "d.docx")

    # mock 的 indexer：若 dry_run 误调 index_chunks 会被捕获
    indexer = AsyncMock()
    indexer.index_chunks = AsyncMock(return_value=999)

    report = await ingest(
        LocalFileSource(tmp_path),
        project_id="_test_ingest_project",
        indexer=indexer,
        dry_run=True,
    )

    # 铁律：dry_run 绝不调 index_chunks，绝不落库
    indexer.index_chunks.assert_not_called()
    assert report.dry_run is True
    assert report.indexed_chunks == 0
    assert report.parsed_docs == 4
    assert report.total_chunks > 0
    # 每篇 parsed 文档有 chunk 预览
    parsed = [d for d in report.docs if d.status == "parsed"]
    assert all(d.chunk_preview for d in parsed)


async def test_pipeline_skips_unsupported_and_image_formats(tmp_path):
    """不支持/图片格式 → skipped（brain/ingest 把已知原因记在 error，不落库）。"""
    _make_md(tmp_path / "a.md")
    (tmp_path / "pic.png").write_bytes(b"\x89PNGfake")     # 图片 → needs_vision 无文本
    (tmp_path / "weird.xyz").write_text("x", encoding="utf-8")  # 不支持扩展

    report = await ingest(
        # 显式带上 .png/.xyz 让 source 把它们列出来交给 parse_file 判定
        LocalFileSource(tmp_path, extensions=[".md", ".png", ".xyz"]),
        project_id="_test_ingest_project",
        dry_run=True,
    )
    statuses = {Path(d.doc_id).name: d.status for d in report.docs}
    assert statuses["a.md"] == "parsed"
    # .png 图片无确定性文本 → skipped；.xyz 不在白名单 → parse_file 报错 → skipped
    assert statuses["pic.png"] == "skipped"
    assert statuses["weird.xyz"] == "skipped"
    assert report.parsed_docs == 1
    assert report.skipped_docs == 2


async def test_pipeline_non_dry_run_calls_index_with_mock(tmp_path):
    """非 dry_run 路径用 mock indexer 验证 index_chunks 被调用（不碰真实 KB）。"""
    _make_md(tmp_path / "a.md")
    indexer = AsyncMock()
    indexer.index_chunks = AsyncMock(return_value=7)

    report = await ingest(
        LocalFileSource(tmp_path),
        project_id="_test_ingest_project",
        indexer=indexer,
        dry_run=False,
    )
    indexer.index_chunks.assert_awaited()
    # 落库的 project_id 正确传递
    args, _ = indexer.index_chunks.call_args
    assert args[0] == "_test_ingest_project"
    assert report.indexed_chunks == 7


async def test_pipeline_non_dry_run_requires_indexer(tmp_path):
    _make_md(tmp_path / "a.md")
    with pytest.raises(ValueError):
        await ingest(
            LocalFileSource(tmp_path),
            project_id="_test_ingest_project",
            dry_run=False,
            indexer=None,
        )


# ── 远端 stub：缺 token 抛清晰 NotImplementedError ───────────────────


@pytest.mark.parametrize("cls", [FeishuSource, TencentDocSource, YuqueSource])
def test_remote_sources_raise_not_implemented_without_token(cls, monkeypatch):
    # 清掉可能存在的相关 env，确保走 stub 分支
    for key in (
        "SWARM_INGEST_FEISHU_APP_ID",
        "SWARM_INGEST_FEISHU_APP_SECRET",
        "SWARM_INGEST_TENCENT_CLIENT_ID",
        "SWARM_INGEST_TENCENT_CLIENT_SECRET",
        "SWARM_INGEST_TENCENT_ACCESS_TOKEN",
        "YUQUE_TOKEN",
        "YUQUE_NAMESPACE",
    ):
        monkeypatch.delenv(key, raising=False)

    src = cls()
    with pytest.raises(NotImplementedError) as ei:
        src.list_documents()
    # 报错里要写清需要的 env
    assert "环境变量" in str(ei.value) or "env" in str(ei.value).lower()
    with pytest.raises(NotImplementedError):
        src.fetch("any-id")


# ── YuqueSource 真实现：mock urlopen 验证解析逻辑（不真发网络） ──────────


class _FakeResp:
    """最小 urlopen() 返回对象：支持 with 语句 + read()。"""

    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload


def _yuque_src(monkeypatch, *, token="tk", namespace="user/repo"):
    from swarm.knowledge.ingest import YuqueSource

    monkeypatch.setenv("YUQUE_TOKEN", token)
    monkeypatch.setenv("YUQUE_NAMESPACE", namespace)
    monkeypatch.delenv("YUQUE_BASE", raising=False)
    return YuqueSource()


def test_yuque_list_documents_parses_data_array(monkeypatch):
    """mock urlopen 返回假 docs JSON → 解析成 DocRef[]（slug→doc_id, title 保留, ext=.md）。"""
    import swarm.knowledge.ingest.sources as srcmod

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["token"] = req.get_header("X-auth-token")
        payload = {
            "data": [
                {"slug": "intro", "title": "介绍"},
                {"slug": "guide", "title": "指南"},
                {"title": "无 slug 应被跳过"},  # 缺 slug → 跳过
            ]
        }
        return _FakeResp(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(srcmod.urllib.request, "urlopen", fake_urlopen)
    src = _yuque_src(monkeypatch)
    refs = src.list_documents()

    assert captured["url"] == "https://www.yuque.com/api/v2/repos/user/repo/docs"
    assert captured["token"] == "tk"  # header 带上 X-Auth-Token
    ids = [r.doc_id for r in refs]
    assert ids == ["intro", "guide"]  # 缺 slug 的被跳过
    assert refs[0].title == "介绍"
    assert all(r.ext == ".md" for r in refs)
    assert refs[0].metadata["source"] == "yuque"
    assert refs[0].metadata["namespace"] == "user/repo"


def test_yuque_fetch_returns_markdown_body(monkeypatch):
    """mock urlopen 返回单文档 JSON → FetchedDoc.data=body bytes, filename=<slug>.md。"""
    import swarm.knowledge.ingest.sources as srcmod

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        payload = {"data": {"title": "介绍", "body": "# 标题\n\n正文 hello-yuque。"}}
        return _FakeResp(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(srcmod.urllib.request, "urlopen", fake_urlopen)
    src = _yuque_src(monkeypatch)
    fetched = src.fetch("intro")

    assert captured["url"] == "https://www.yuque.com/api/v2/repos/user/repo/docs/intro"
    assert fetched.filename == "intro.md"
    assert fetched.title == "介绍"
    assert "hello-yuque" in fetched.data.decode("utf-8")
    assert fetched.metadata["namespace"] == "user/repo"


def test_yuque_custom_base_and_namespace_override(monkeypatch):
    """YUQUE_BASE 覆盖默认 base；构造参数 namespace 覆盖 env。"""
    import swarm.knowledge.ingest.sources as srcmod
    from swarm.knowledge.ingest import YuqueSource

    monkeypatch.setenv("YUQUE_TOKEN", "tk")
    monkeypatch.setenv("YUQUE_NAMESPACE", "env/ns")
    monkeypatch.setenv("YUQUE_BASE", "https://yuque.corp.local/api/v2/")  # 尾斜杠应被去掉

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _FakeResp(json.dumps({"data": []}).encode("utf-8"))

    monkeypatch.setattr(srcmod.urllib.request, "urlopen", fake_urlopen)
    src = YuqueSource(namespace="arg/ns")  # 显式 namespace 覆盖 env
    src.list_documents()
    assert captured["url"] == "https://yuque.corp.local/api/v2/repos/arg/ns/docs"


def test_yuque_http_error_raises_with_status_code(monkeypatch):
    """HTTP 401/404 → RuntimeError 带状态码（而非 NotImplementedError，也不静默）。"""
    import swarm.knowledge.ingest.sources as srcmod

    def fake_urlopen(req, timeout=None):
        raise srcmod.urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", hdrs=None,
            fp=io.BytesIO(b'{"message":"Token Invalid"}'),
        )

    monkeypatch.setattr(srcmod.urllib.request, "urlopen", fake_urlopen)
    src = _yuque_src(monkeypatch)
    with pytest.raises(RuntimeError) as ei:
        src.list_documents()
    msg = str(ei.value)
    assert "401" in msg
    assert "yuque" in msg.lower()


def test_yuque_missing_env_raises_not_implemented(monkeypatch):
    """缺 token/namespace → NotImplementedError 列出所需 env（可测，不联网）。"""
    from swarm.knowledge.ingest import YuqueSource

    monkeypatch.delenv("YUQUE_TOKEN", raising=False)
    monkeypatch.delenv("YUQUE_NAMESPACE", raising=False)
    src = YuqueSource()
    with pytest.raises(NotImplementedError) as ei:
        src.list_documents()
    assert "YUQUE_TOKEN" in str(ei.value)
    assert "YUQUE_NAMESPACE" in str(ei.value)
    with pytest.raises(NotImplementedError):
        src.fetch("any-id")
