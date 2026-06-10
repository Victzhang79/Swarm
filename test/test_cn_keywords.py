#!/usr/bin/env python3
"""knowledge/retriever.py 中文关键词提取 + 时间衰减单测。

（原 test_cn_keywords.py 是项目根的 print 脚本，归类为正式 pytest 测试。）
覆盖：中文 2-gram 抽取、英文分词、中英混合、时间权重加成、空时间字典优雅降级。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.knowledge.retriever import _apply_time_decay, _extract_keywords


# ── 关键词提取 ───────────────────────────────────


def test_extract_keywords_chinese_2gram():
    """纯中文走 2-gram 切分，能抽到核心词。"""
    kws = _extract_keywords("修改用户登录模块的密码验证逻辑")
    assert isinstance(kws, list)
    assert len(kws) > 0
    # 关键 2-gram 应出现
    assert "用户" in kws
    assert "登录" in kws
    assert "密码" in kws
    print("  ✅ _extract_keywords 纯中文 2-gram")


def test_extract_keywords_english_tokenize():
    """英文按词切分并小写。"""
    kws = _extract_keywords("fix Main class bug")
    assert "main" in kws
    assert "class" in kws
    assert "bug" in kws
    print("  ✅ _extract_keywords 英文分词小写")


def test_extract_keywords_mixed():
    """中英混合：英文 token + 驼峰拆分 + 中文 2-gram 同时存在。"""
    kws = _extract_keywords("修改 UserService 的密码验证逻辑")
    # 英文原词 + 驼峰拆分
    assert "UserService" in kws
    assert "user" in kws and "service" in kws
    # 中文 2-gram
    assert "密码" in kws
    print("  ✅ _extract_keywords 中英混合 + 驼峰拆分")


def test_extract_keywords_returns_list():
    """空白/停用词输入不崩溃，返回 list。"""
    assert isinstance(_extract_keywords("的实现和修改"), list)
    assert isinstance(_extract_keywords(""), list)
    assert isinstance(_extract_keywords("   "), list)
    print("  ✅ _extract_keywords 边界输入返回 list")


# ── 时间衰减 ─────────────────────────────────────


def test_apply_time_decay_recent_boosted():
    """最近修改的文件获得更高时间加成。"""
    scores = {"a.py": 2.0, "b.py": 1.5, "c.py": 1.0}
    times = {"a.py": 1000.0, "b.py": 2000.0, "c.py": 3000.0}  # c 最新
    result = _apply_time_decay(scores, times)
    # 所有分数被加权（>= 原值，因为最新文件加成）
    assert result["c.py"] > 1.0  # 最新文件得到加成
    # 加权后仍是 3 个键
    assert set(result.keys()) == {"a.py", "b.py", "c.py"}
    print("  ✅ _apply_time_decay 最近修改获时间加成")


def test_apply_time_decay_empty_times_graceful():
    """空时间字典 → 优雅降级，原分数不变。"""
    result = _apply_time_decay({"a.py": 2.0}, {})
    assert result == {"a.py": 2.0}
    print("  ✅ _apply_time_decay 空时间字典优雅降级")


def test_apply_time_decay_empty_scores():
    assert _apply_time_decay({}, {"a.py": 1000.0}) == {}
    print("  ✅ _apply_time_decay 空分数返回空")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\ncn_keywords (retriever) 单测通过。")
