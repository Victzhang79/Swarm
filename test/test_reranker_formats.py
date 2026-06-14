"""Rerank 格式适配器单测 — knowledge/reranker.py 三格式（批2）。

固化 simple / openai_rerank / cohere_rerank 三种响应格式的解析正确性。
mock httpx.Client.post 返回各家格式，验证 index→doc 映射与 rerank_score 提取。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.knowledge import reranker
from swarm.knowledge.embed_rerank_config import RerankEndpoint


class _FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, payload, status=200):
        self._p = payload
        self._s = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *a, **k):
        return _FakeResp(self._p, self._s)


_DOCS = [{"content": "doc A"}, {"content": "doc B"}, {"content": "doc C"}]


def test_simple_format(monkeypatch):
    """自建 simple：[{index,score}] → 按 score 排序映射回 doc。"""
    ep = RerankEndpoint(url="http://ai.bit:8081/rerank", api_key="", model="m", fmt="simple")
    payload = [{"index": 2, "score": 0.9}, {"index": 0, "score": 0.3}]
    monkeypatch.setattr(reranker.httpx, "Client", lambda *a, **k: _FakeClient(payload))
    out = reranker._rerank_simple(ep, "q", ["a", "b", "c"], _DOCS)
    assert out[0]["content"] == "doc C" and out[0]["rerank_score"] == 0.9
    assert {d["content"] for d in out} == {"doc C", "doc A"}
    print("  ✅ simple 格式解析")


def test_openai_rerank_format(monkeypatch):
    """SiliconFlow/OpenAI：{results:[{index,relevance_score}]}。"""
    ep = RerankEndpoint(url="https://api.siliconflow.cn/v1", api_key="sk-x", model="bge", fmt="openai_rerank")
    payload = {"results": [{"index": 1, "relevance_score": 0.88}, {"index": 0, "relevance_score": 0.5}]}
    monkeypatch.setattr(reranker.httpx, "Client", lambda *a, **k: _FakeClient(payload))
    out = reranker._rerank_openai(ep, "q", ["a", "b", "c"], _DOCS, top_k=5)
    assert out[0]["content"] == "doc B" and out[0]["rerank_score"] == 0.88
    print("  ✅ openai_rerank 格式解析")


def test_cohere_format(monkeypatch):
    """Cohere：{results:[{index,relevance_score}]}（只认 relevance_score）。"""
    ep = RerankEndpoint(url="https://api.cohere.ai/v1", api_key="co-x", model="rerank-v3", fmt="cohere_rerank")
    payload = {"results": [{"index": 2, "relevance_score": 0.95}]}
    monkeypatch.setattr(reranker.httpx, "Client", lambda *a, **k: _FakeClient(payload))
    out = reranker._rerank_cohere(ep, "q", ["a", "b", "c"], _DOCS, top_k=5)
    assert out[0]["content"] == "doc C" and out[0]["rerank_score"] == 0.95
    print("  ✅ cohere_rerank 格式解析")


def test_out_of_range_index_skipped(monkeypatch):
    """越界 index 被跳过，不崩。"""
    ep = RerankEndpoint(url="http://x/rerank", api_key="", model="m", fmt="simple")
    payload = [{"index": 99, "score": 0.9}, {"index": 1, "score": 0.4}]
    monkeypatch.setattr(reranker.httpx, "Client", lambda *a, **k: _FakeClient(payload))
    out = reranker._rerank_simple(ep, "q", ["a", "b", "c"], _DOCS)
    assert len(out) == 1 and out[0]["content"] == "doc B"
    print("  ✅ 越界 index 跳过")


def test_dispatch_by_format(monkeypatch):
    """rerank_documents 按 ep.fmt 分发到对应适配器 + 阈值过滤 + top_k 截断。"""
    ep = RerankEndpoint(url="http://ai.bit:8081/rerank", api_key="", model="m", fmt="simple")
    monkeypatch.setattr(reranker, "get_rerank_endpoint", lambda: ep, raising=False)
    # patch embed_rerank_config 的 import 入口
    import swarm.knowledge.embed_rerank_config as erc
    monkeypatch.setattr(erc, "get_rerank_endpoint", lambda: ep, raising=False)
    payload = [{"index": 0, "score": 0.9}, {"index": 1, "score": 0.1}, {"index": 2, "score": 0.7}]
    monkeypatch.setattr(reranker.httpx, "Client", lambda *a, **k: _FakeClient(payload))
    out = reranker.rerank_documents("q", _DOCS, top_k=2)
    assert len(out) == 2
    assert out[0]["content"] == "doc A" and out[1]["content"] == "doc C"  # 按 score 降序取 top2
    print("  ✅ rerank_documents 按格式分发+top_k")


def test_fallback_when_no_endpoint(monkeypatch):
    """无接入点 → 本地排序兜底（按已有 score）。"""
    import swarm.knowledge.embed_rerank_config as erc
    monkeypatch.setattr(erc, "get_rerank_endpoint", lambda: None, raising=False)
    docs = [{"content": "x", "score": 0.2}, {"content": "y", "score": 0.8}]
    out = reranker.rerank_documents("q", docs, top_k=5)
    assert out[0]["content"] == "y"
    print("  ✅ 无接入点→本地排序兜底")
