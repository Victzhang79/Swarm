"""B3（round22, P1）：embed/rerank 完全不记账 → token 统计/预算失真。

根因：_UsageRecorder 只挂在 get_chat_model 的 Chat LLM；embed_client / reranker 直连 HTTP，
零 usage_tracker.record → WebUI/DB token 统计漏掉知识检索的 embed/rerank 消耗（与 B2 同根：
per-task 真实累计也漏这块）。

治本：embed/rerank 成功后 best-effort usage_tracker.record（优先响应里的真实 usage.prompt_tokens，
否则 len//4 估算）。经 B2 的 ContextVar 自动归属到当前 task。

行为测试：patch usage_tracker.record + HTTP，断言 embed 成功后 record 被调用且 prompt>0。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from swarm.knowledge import embed_client


class _FakeResp:
    status_code = 200

    def json(self):
        return {"data": [{"embedding": [0.1, 0.2]}], "usage": {"prompt_tokens": 42}}

    def raise_for_status(self):
        pass


def test_embed_sync_records_usage():
    ep = ("http://localhost:9000/v1", "", "bge-m3", 32)
    with patch.object(embed_client, "_endpoint", return_value=ep), \
         patch("requests.post", return_value=_FakeResp()), \
         patch("swarm.models.usage_tracker.record") as rec:
        out = embed_client.embed_texts_sync(["hello world"])
    assert out == [[0.1, 0.2]]
    assert rec.called, "embed 成功后必须记账"
    # prompt_tokens 应取响应真实值 42
    _, kwargs = rec.call_args
    pt = kwargs.get("prompt_tokens")
    assert pt == 42, f"应优先取响应真实 usage: {rec.call_args}"


def test_embed_sync_records_estimate_when_no_usage():
    class _NoUsageResp(_FakeResp):
        def json(self):
            return {"data": [{"embedding": [0.1, 0.2]}]}  # 无 usage

    ep = ("http://localhost:9000/v1", "", "bge-m3", 32)
    with patch.object(embed_client, "_endpoint", return_value=ep), \
         patch("requests.post", return_value=_NoUsageResp()), \
         patch("swarm.models.usage_tracker.record") as rec:
        embed_client.embed_texts_sync(["hello world"])
    assert rec.called
    _, kwargs = rec.call_args
    assert kwargs.get("prompt_tokens", 0) > 0, "无响应 usage 时应回退估算 (>0)"


def test_rerank_records_usage():
    from swarm.knowledge import reranker
    # 直接测底层 _rerank_simple 记账（避免全 rerank_documents 的配置分支）
    ep = MagicMock(); ep.url = "http://localhost:9100/rerank"; ep.api_key = ""; ep.model = "bge-rerank"

    class _RerankResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"scores": [0.9, 0.1]}

    q = "what is the meaning of this document"
    texts = ["document one full text here", "document two full text here"]
    docs = [{"text": t} for t in texts]
    with patch("httpx.Client") as Client, \
         patch("swarm.models.usage_tracker.record") as rec:
        Client.return_value.__enter__.return_value.post.return_value = _RerankResp()
        reranker._rerank_simple(ep, q, texts, docs)
    assert rec.called, "rerank 成功后必须记账"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
