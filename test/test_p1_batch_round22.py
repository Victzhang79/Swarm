#!/usr/bin/env python3
"""round22 P1 批治本回归：P1-2/8/11/20 行为锁定。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ── P1-2：verify_merged_patch_applies 异常 fail-closed ──
def test_p1_2_apply_check_exception_failclosed(tmp_path):
    import subprocess
    from unittest.mock import patch
    from swarm.brain.merge_engine import verify_merged_patch_applies
    (tmp_path / ".git").mkdir()
    diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
    with patch("subprocess.run", side_effect=subprocess.SubprocessError("boom")):
        ok, detail = verify_merged_patch_applies(str(tmp_path), diff)
    assert ok is False, "校验器异常必须 fail-closed（复现 fail-open bug）"
    assert "fail-closed" in detail
    print("  ✅ P1-2 apply-check 异常 → ok=False")


def test_p1_2_empty_and_nogit_still_true(tmp_path):
    from swarm.brain.merge_engine import verify_merged_patch_applies
    assert verify_merged_patch_applies(str(tmp_path), "")[0] is True       # 空 diff
    assert verify_merged_patch_applies(str(tmp_path), "diff x")[0] is True  # 无 .git
    print("  ✅ P1-2 空 diff / 无 git → 仍 True（不回归）")


# ── P1-8：rerank 优先 rerank_score ──
def test_p1_8_rerank_prefers_rerank_score():
    from swarm.knowledge.retriever import SwarmRetriever
    ctx = {"semantic": [
        {"id": "a", "score": 0.9, "rerank_score": 0.1},
        {"id": "b", "score": 0.2, "rerank_score": 0.95},
    ]}
    r = SwarmRetriever.__new__(SwarmRetriever)
    r._rerank(ctx, "q")
    assert ctx["semantic"][0]["id"] == "b", "应按 rerank_score 排序（b 在前），非原始 score"
    print("  ✅ P1-8 semantic 按 rerank_score 排序")


def test_p1_8_rerank_fallback_score():
    from swarm.knowledge.retriever import SwarmRetriever
    ctx = {"semantic": [{"id": "a", "score": 0.3}, {"id": "b", "score": 0.8}]}
    r = SwarmRetriever.__new__(SwarmRetriever)
    r._rerank(ctx, "q")
    assert ctx["semantic"][0]["id"] == "b", "无 rerank_score 时回退 score（不回归）"
    print("  ✅ P1-8 无 rerank_score 回退 score")


# ── P1-11：sliding window 先逐出价值最低(数字最大) ──
def test_p1_11_evicts_least_valuable_first():
    from swarm.memory import sliding_window as sw
    # 超预算：USER(1) + WORKER(2) + PROCESS(3)，应先逐 PROCESS(3) 保 WORKER(2) 与 USER(1)
    log = [
        {"role": "user", "content": "U" * 40, "priority": sw.PRIORITY_USER},
        {"role": "worker", "content": "W" * 4000, "priority": sw.PRIORITY_WORKER},
        {"role": "process", "content": "P" * 4000, "priority": sw.PRIORITY_PROCESS},
    ]
    new_log, summary, _tk = sw.compress_context_log(log, max_tokens=1100, reserve_tokens=0)
    kept_pri = {e.get("priority") for e in new_log}
    assert sw.PRIORITY_USER in kept_pri, "USER 永不逐出"
    assert not (sw.PRIORITY_PROCESS in kept_pri and sw.PRIORITY_WORKER not in kept_pri), \
        "方向错：保了 PROCESS(3) 却逐了 WORKER(2)"
    print("  ✅ P1-11 先逐价值最低(PROCESS) 保 WORKER")


# ── P1-20：TLS 校验仅 localhost/私网跳过 ──
def test_p1_20_tls_host_gate():
    from swarm.api.routers.config import _is_local_or_private_host as f
    assert f("http://localhost:11434/v1") is True
    assert f("http://127.0.0.1:8000") is True
    assert f("http://192.168.1.10:1234") is True
    assert f("https://api.openai.com/v1") is False   # 公网 → 强校验
    assert f("https://api.siliconflow.cn/v1") is False
    print("  ✅ P1-20 仅本地/私网跳过 TLS，公网强校验")


if __name__ == "__main__":
    import tempfile
    test_p1_2_apply_check_exception_failclosed(Path(tempfile.mkdtemp()))
    test_p1_2_empty_and_nogit_still_true(Path(tempfile.mkdtemp()))
    test_p1_8_rerank_prefers_rerank_score()
    test_p1_8_rerank_fallback_score()
    test_p1_11_evicts_least_valuable_first()
    test_p1_20_tls_host_gate()  # noqa
    print("\n✅ P1 批治本回归全部通过")
