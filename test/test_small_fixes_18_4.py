"""独立小修批回归测试（audit #18 JSON 解析边界 / #4 mistakes 格式化健壮性）。

#18 _parse_json_from_llm：```json 无换行 → 旧 text.index('\\n') 抛 ValueError；
    rfind('```') 找不到收尾 → -1 截取出错。修复后安全提取。
#4 _format_mistakes_for_worker：metadata 非 dict 时不应崩、snippet 取空。

纯函数测试。
"""

from __future__ import annotations

import json

from swarm.brain.nodes import _parse_json_from_llm
from swarm.worker.prompts import _format_mistakes_for_worker


# ── #18 JSON 解析边界 ──────────────────────────────

def test_18_plain_json():
    assert _parse_json_from_llm('{"a": 1}') == {"a": 1}


def test_18_fenced_json_normal():
    text = '```json\n{"a": 1, "b": 2}\n```'
    assert _parse_json_from_llm(text) == {"a": 1, "b": 2}


def test_18_fenced_no_newline_does_not_raise():
    """旧 bug：```json{...}``` 无换行 → text.index('\\n') ValueError。"""
    # 形态：```{"a":1}```（无语言标识、无换行）
    text = '```{"a": 1}```'
    # 不应抛 ValueError；能解析出 dict
    result = _parse_json_from_llm(text)
    assert result == {"a": 1}


def test_18_fenced_lang_no_closing_fence():
    """```json\\n{...} 无收尾 ``` → 旧 rfind 返回 -1 截取出错。"""
    text = '```json\n{"a": 1}'
    result = _parse_json_from_llm(text)
    assert result == {"a": 1}


# ── #4 mistakes 格式化健壮性 ──────────────────────────

def test_4_metadata_non_dict_no_crash():
    """metadata 为 None / 非 dict 时不崩，snippet 取空。"""
    items = [
        {"description": "错误A", "metadata": None},
        {"description": "错误B", "metadata": "not-a-dict"},
        {"description": "错误C", "metadata": {"code_snippet": "x = 1"}},
    ]
    out = _format_mistakes_for_worker(items)
    assert "错误A" in out and "错误B" in out and "错误C" in out
    assert "x = 1" in out  # 正常 dict 的 snippet 仍输出


def test_4_empty_items():
    assert _format_mistakes_for_worker([]) == "（无）"


if __name__ == "__main__":
    import sys
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  💥 {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n=== #18/#4 small fixes: {len(fns) - failed}/{len(fns)} passed ===")
    sys.exit(1 if failed else 0)


# ── RUN10 治本：LLM JSON 瑕疵(缺逗号/尾逗号/截断) → json_repair 修复，杜绝单字符瑕疵
#    让 TECH_DESIGN 整个设计塌成空方案 → 欠规划 PRD 假成功 ──
def test_json_repair_missing_comma():
    """缺逗号(RUN10 实证 'Expecting , delimiter')→ 修复成功，不丢内容。"""
    r = _parse_json_from_llm('{"modules": [{"name": "a" "x": 1}], "file_plan": [{"p":"f.java"}]}')
    assert isinstance(r, dict) and len(r.get("file_plan", [])) == 1, r
    assert r["modules"][0]["name"] == "a"


def test_json_repair_trailing_comma_and_truncation():
    assert _parse_json_from_llm('{"a": [1, 2,], "b": 3,}')["a"] == [1, 2]
    # 截断(超长 JSON 被截) → 尽力恢复已完整部分
    r = _parse_json_from_llm('{"file_plan": [{"path":"a.java"},{"path":"b.j')
    assert isinstance(r, dict) and "file_plan" in r


def test_json_repair_valid_unchanged():
    """合法 JSON 零行为差(走严格解析快路径)。"""
    assert _parse_json_from_llm('{"x": [1, 2, 3], "y": {"z": true}}') == {"x": [1, 2, 3], "y": {"z": True}}


def test_json_garbage_still_raises():
    """纯垃圾仍抛异常(不静默吞成空 → 交调用方重试/降级，不假成功)。"""
    import pytest
    with pytest.raises(Exception):
        _parse_json_from_llm("this is not json at all !!!")
