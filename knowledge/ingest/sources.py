"""来源适配层 — 把"从哪儿取资料"抽象成统一 Protocol。

SourceAdapter:
  - list_documents() -> list[DocRef]   列出可采集的文档(轻量元信息)。
  - fetch(doc_id)    -> FetchedDoc      取单篇原始内容(bytes + filename)。

实现:
  - LocalFileSource: 本地文件/目录(真能用,有测试)。
  - FeishuSource / TencentDocSource / YuqueSource: 接口 + stub。
    没有 API token 一律抛 NotImplementedError 并写清需要的 env/scope/端点,
    绝不伪造抓取结果。

下游契约: pipeline 拿 FetchedDoc.data + filename 喂给 brain/ingest.parse_file()。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol, runtime_checkable

# brain/ingest 的白名单即默认可采集扩展集合（复用生产解析器，不再自维护一份）
from swarm.brain.ingest import ALLOWED_EXTENSIONS


def supported_extensions() -> list[str]:
    """默认可采集扩展名（复用 brain/ingest 的格式白名单），排序返回。"""
    return sorted(ALLOWED_EXTENSIONS)


@dataclass
class DocRef:
    """list_documents() 返回的轻量文档引用。"""

    doc_id: str                  # fetch() 用得到的唯一标识(本地为绝对路径)
    title: str | None = None
    ext: str | None = None       # 含点的小写扩展名,如 ".pdf"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchedDoc:
    """fetch() 返回的原始内容。"""

    doc_id: str
    data: bytes                  # 原始字节(parsers 据 filename 选解析器)
    filename: str                # 带扩展名,供 parsers 分派
    title: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SourceAdapter(Protocol):
    """来源适配器协议。"""

    source_name: str

    def list_documents(self) -> list[DocRef]:
        ...

    def fetch(self, doc_id: str) -> FetchedDoc:
        ...


# ── LocalFileSource(真实可用) ────────────────────────────────────────


class LocalFileSource:
    """本地文件 / 目录来源。

    root 为文件: 只采集该文件。
    root 为目录: 递归(默认)遍历,按扩展名过滤(默认取 parsers 支持的全部扩展)。

    只读 — 不会写任何东西。
    """

    source_name = "local"

    def __init__(
        self,
        root: str | Path,
        *,
        recursive: bool = True,
        extensions: Iterable[str] | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.recursive = recursive
        # 归一化为小写含点集合
        exts = list(extensions) if extensions is not None else supported_extensions()
        self.extensions = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in exts}

    def list_documents(self) -> list[DocRef]:
        if not self.root.exists():
            raise FileNotFoundError(f"路径不存在: {self.root}")

        if self.root.is_file():
            candidates = [self.root]
        else:
            globber = self.root.rglob("*") if self.recursive else self.root.glob("*")
            candidates = sorted(p for p in globber if p.is_file())

        refs: list[DocRef] = []
        for p in candidates:
            ext = p.suffix.lower()
            if self.extensions and ext not in self.extensions:
                continue
            refs.append(
                DocRef(
                    doc_id=str(p),
                    title=p.stem,
                    ext=ext,
                    metadata={"source": "local", "path": str(p), "size": p.stat().st_size},
                )
            )
        return refs

    def fetch(self, doc_id: str) -> FetchedDoc:
        p = Path(doc_id)
        if not p.is_file():
            raise FileNotFoundError(f"文件不存在: {doc_id}")
        return FetchedDoc(
            doc_id=doc_id,
            data=p.read_bytes(),
            filename=p.name,
            title=p.stem,
            metadata={"source": "local", "path": str(p)},
        )


# ── 远端 stub 基类 ────────────────────────────────────────────────────


class RemoteSourceStub:
    """远端来源的公共 stub 行为。

    子类声明所需的 env 变量集合(REQUIRED_ENV)与人类可读的接入说明(SETUP_DOC),
    任何 list/fetch 调用在缺 token 时统一抛 NotImplementedError 并附带说明。
    """

    source_name = "remote"
    REQUIRED_ENV: tuple[str, ...] = ()
    SETUP_DOC: str = ""

    def _missing_env(self) -> list[str]:
        return [k for k in self.REQUIRED_ENV if not os.environ.get(k)]

    def _raise_not_implemented(self, action: str) -> None:
        missing = self._missing_env()
        raise NotImplementedError(
            f"[{self.source_name}] {action} 未实现/未配置。\n"
            f"需要的环境变量: {', '.join(self.REQUIRED_ENV) or '(无)'}\n"
            f"当前缺失: {', '.join(missing) or '(无)'}\n"
            f"{self.SETUP_DOC}"
        )

    def list_documents(self) -> list[DocRef]:
        self._raise_not_implemented("list_documents")
        return []  # pragma: no cover

    def fetch(self, doc_id: str) -> FetchedDoc:  # noqa: ARG002
        self._raise_not_implemented("fetch")
        raise AssertionError("unreachable")  # pragma: no cover


# ── 飞书文档 ──────────────────────────────────────────────────────────


class FeishuSource(RemoteSourceStub):
    """飞书(Lark)云文档来源 — 接口 + stub。

    接入清单:
      env:
        SWARM_INGEST_FEISHU_APP_ID      自建应用 App ID
        SWARM_INGEST_FEISHU_APP_SECRET  自建应用 App Secret
        SWARM_INGEST_FEISHU_SPACE_ID    (可选)知识库/wiki space,限定采集范围
      流程:
        1. POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
           用 app_id+app_secret 换 tenant_access_token。
        2. 列文档: 知识库用 GET /open-apis/wiki/v2/spaces/{space_id}/nodes;
           云盘用 GET /open-apis/drive/v1/files。
        3. 取内容: docx 用 GET /open-apis/docx/v1/documents/{document_id}/raw_content
           或按 block 拉取 /blocks 再自行拼装。
      scope/权限:
        需在开放平台为应用开通 `docx:document:readonly`、`wiki:wiki:readonly`、
        `drive:drive:readonly` 等只读权限,并将目标文档/知识库授权给该应用。
    """

    source_name = "feishu"
    REQUIRED_ENV = (
        "SWARM_INGEST_FEISHU_APP_ID",
        "SWARM_INGEST_FEISHU_APP_SECRET",
    )
    SETUP_DOC = (
        "飞书接入: 配置 SWARM_INGEST_FEISHU_APP_ID / SWARM_INGEST_FEISHU_APP_SECRET, "
        "为应用开通 docx/wiki/drive 只读 scope 并授权目标文档。"
        "用 tenant_access_token 调 docx raw_content API 取正文。"
    )


# ── 腾讯文档 ──────────────────────────────────────────────────────────


class TencentDocSource(RemoteSourceStub):
    """腾讯文档来源 — 接口 + stub。

    接入清单:
      env:
        SWARM_INGEST_TENCENT_CLIENT_ID      开放平台应用 ClientID
        SWARM_INGEST_TENCENT_CLIENT_SECRET  应用 Secret
        SWARM_INGEST_TENCENT_ACCESS_TOKEN   OAuth2 用户授权后的 access_token
      流程:
        1. 走腾讯文档开放平台 OAuth2 授权码流程拿 access_token(用户需登录授权)。
        2. 列文档: GET https://docs.qq.com/openapi/drive/v2/files(分页 list)。
        3. 取内容: 通过导出接口 POST /openapi/document/v2/export 拿 docx/pdf,
           或文档内容接口拉结构化正文。
      scope/权限:
        OAuth scope 需含文档只读(如 `file.read`),且 access_token 会过期,
        需实现 refresh_token 刷新。腾讯文档开放平台需企业/开发者资质申请。
    """

    source_name = "tencent_doc"
    REQUIRED_ENV = (
        "SWARM_INGEST_TENCENT_CLIENT_ID",
        "SWARM_INGEST_TENCENT_CLIENT_SECRET",
        "SWARM_INGEST_TENCENT_ACCESS_TOKEN",
    )
    SETUP_DOC = (
        "腾讯文档接入: 走 OAuth2 拿 access_token(client_id/secret + 用户授权), "
        "调 drive list 列文件、export 接口导出 docx/pdf 再灌入。token 会过期需刷新。"
    )


# ── 语雀 ──────────────────────────────────────────────────────────────


class YuqueSource(RemoteSourceStub):
    """语雀来源 — 接口 + 较完整骨架(无 token 不联网、不测)。

    语雀 API 较简单(Token 直接走 Header,无 OAuth),这里写出端点与组织方式骨架,
    但没有 token 时一律 stub 抛错,绝不联网。

    接入清单:
      env:
        YUQUE_TOKEN     语雀个人 Token(账户设置 → Token,只读即可)
        YUQUE_NAMESPACE 目标知识库 namespace,形如 "user/repo"
        YUQUE_BASE      (可选)私有部署 base,默认 https://www.yuque.com/api/v2
      API(Header: X-Auth-Token: <token>):
        列文档:   GET {base}/repos/{namespace}/docs        → data[].slug / title
        取单文档: GET {base}/repos/{namespace}/docs/{slug} → data.body(markdown 正文)
      说明:
        语雀文档正文本就是 markdown(data.body),fetch 后 filename 用 "<slug>.md",
        直接走 parsers 的 .md parser,无需额外解析。
    """

    source_name = "yuque"
    REQUIRED_ENV = ("YUQUE_TOKEN", "YUQUE_NAMESPACE")
    SETUP_DOC = (
        "语雀接入: 设置 YUQUE_TOKEN(个人只读 Token)+ YUQUE_NAMESPACE(user/repo)。"
        " 列表 GET /repos/{ns}/docs、正文 GET /repos/{ns}/docs/{slug}.data.body(markdown)。"
    )

    def __init__(self) -> None:
        self.token = os.environ.get("YUQUE_TOKEN")
        self.namespace = os.environ.get("YUQUE_NAMESPACE")
        self.base = os.environ.get("YUQUE_BASE", "https://www.yuque.com/api/v2")

    # 骨架: 真要启用时把 stub 换成下面注释的真实实现(需 httpx + token)。
    def list_documents(self) -> list[DocRef]:
        if self._missing_env():
            self._raise_not_implemented("list_documents")
        # --- 真实实现骨架(需 token 时启用,这里不联网) ---
        # import httpx
        # url = f"{self.base}/repos/{self.namespace}/docs"
        # r = httpx.get(url, headers={"X-Auth-Token": self.token}, timeout=30)
        # r.raise_for_status()
        # return [DocRef(doc_id=d["slug"], title=d["title"], ext=".md",
        #                metadata={"source": "yuque", "namespace": self.namespace})
        #         for d in r.json()["data"]]
        raise NotImplementedError("unreachable")  # pragma: no cover

    def fetch(self, doc_id: str) -> FetchedDoc:
        if self._missing_env():
            self._raise_not_implemented("fetch")
        # --- 真实实现骨架(需 token 时启用,这里不联网) ---
        # import httpx
        # url = f"{self.base}/repos/{self.namespace}/docs/{doc_id}"
        # r = httpx.get(url, headers={"X-Auth-Token": self.token}, timeout=30)
        # r.raise_for_status()
        # body = r.json()["data"]["body"]  # markdown 正文
        # return FetchedDoc(doc_id=doc_id, data=body.encode("utf-8"),
        #                   filename=f"{doc_id}.md", title=r.json()["data"].get("title"),
        #                   metadata={"source": "yuque", "namespace": self.namespace})
        raise NotImplementedError("unreachable")  # pragma: no cover
