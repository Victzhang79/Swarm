#!/usr/bin/env python3
"""knowledge/hybrid.py 单测：BM25 融合逻辑。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.knowledge.hybrid import _bm25_scores, _tokenize_doc, hybrid_fuse


def test_tokenize_english_and_chinese():
    toks = _tokenize_doc("selectUserByName 判断字符串")
    assert "selectuserbyname" in toks
    assert "判断" in toks and "字符" in toks  # 中文 2-gram
    print("  ✅ 分词: 英文 token + 中文 2-gram")


def test_bm25_ranks_keyword_match_higher():
    docs = [
        _tokenize_doc("DateUtils 日期格式化工具"),
        _tokenize_doc("StringUtils isBlank 判断字符串为空"),
        _tokenize_doc("文件上传 FileUtils"),
    ]
    scores = _bm25_scores(["isblank", "字符"], docs)
    assert scores[1] == max(scores), "含 isBlank+字符 的文档应得分最高"
    print("  ✅ BM25 关键词命中文档得分最高")


def test_hybrid_pure_vector_when_weight_zero():
    cands = [{"content": "a", "score": 0.9}, {"content": "b", "score": 0.5}]
    out = hybrid_fuse(cands, ["x"], bm25_weight=0.0)
    assert out == cands, "weight=0 应原样返回(纯向量)"
    print("  ✅ weight=0 纯向量原样返回")


def test_hybrid_keyword_boosts_exact_match():
    # 向量分：doc0 高(0.9)，doc1 低(0.4)；但 doc1 精确含关键词
    cands = [
        {"content": "通用日期处理 类", "score": 0.9},
        {"content": "selectUserByLoginName 精确方法", "score": 0.4},
    ]
    # 纯向量 doc0 第一；混合(关键词命中 doc1)后 doc1 应被提上来
    out = hybrid_fuse(cands, ["selectuserbyloginname"], bm25_weight=0.6)
    assert out[0]["content"].startswith("selectUserByLoginName"), "关键词精确命中应被混合提权到第一"
    assert "hybrid_score" in out[0] and "bm25_score" in out[0]
    print("  ✅ 混合检索把精确关键词命中提权")


def test_hybrid_empty_safe():
    assert hybrid_fuse([], ["x"], bm25_weight=0.5) == []
    print("  ✅ 空候选安全")


def main():
    tests = [
        test_tokenize_english_and_chinese,
        test_bm25_ranks_keyword_match_higher,
        test_hybrid_pure_vector_when_weight_zero,
        test_hybrid_keyword_boosts_exact_match,
        test_hybrid_empty_safe,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n📊 结果: {passed} 通过, {failed} 失败\n")
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
