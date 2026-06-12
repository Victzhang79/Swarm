#!/usr/bin/env python3
"""knowledge/norms_inference.py 单测：JSON 解析 + 文件取样（不调真 LLM）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.knowledge.norms_inference import (
    _parse_norms_json,
    _pick_sample_files,
    infer_norms_from_code,
)


def test_parse_plain_json_array():
    raw = '[{"title":"工具类静态化","tag":"utility","content":"StringUtils 全静态方法","priority":4}]'
    norms = _parse_norms_json(raw)
    assert len(norms) == 1
    assert norms[0].title == "工具类静态化"
    assert norms[0].tag == "utility"
    assert norms[0].priority == 4
    assert norms[0].metadata.get("source") == "inferred"
    print("  ✅ 纯 JSON 数组解析")


def test_parse_with_code_fence():
    raw = '说明文字\n```json\n[{"title":"命名","tag":"naming","content":"驼峰命名"}]\n```\n结尾'
    norms = _parse_norms_json(raw)
    assert len(norms) == 1
    assert norms[0].tag == "naming"
    assert norms[0].priority == 2  # 默认
    print("  ✅ 带 code fence + 周围文字解析")


def test_parse_invalid_tag_falls_back_general():
    raw = '[{"title":"x","tag":"乱写的","content":"y"}]'
    norms = _parse_norms_json(raw)
    assert norms[0].tag == "general"
    print("  ✅ 非法 tag 回退 general")


def test_parse_garbage_returns_empty():
    assert _parse_norms_json("完全不是 JSON") == []
    assert _parse_norms_json("") == []
    assert _parse_norms_json('{"not":"array"}') == []
    print("  ✅ 垃圾输入返回空")


def test_parse_skips_incomplete_items():
    raw = '[{"title":"有","content":"内容"},{"title":"","content":"无标题"},{"content":"无标题2"}]'
    norms = _parse_norms_json(raw)
    assert len(norms) == 1, "应只保留 title+content 都全的"
    print("  ✅ 跳过缺 title/content 的条目")


def test_pick_sample_files_filters(tmp_path):
    # 造几个文件：测试文件、过小文件、正常文件
    (tmp_path / "Service.java").write_text("x" * 1000, encoding="utf-8")
    (tmp_path / "ServiceTest.java").write_text("x" * 1000, encoding="utf-8")  # test 排除
    (tmp_path / "tiny.java").write_text("x", encoding="utf-8")  # 过小排除
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("x" * 1000, encoding="utf-8")  # 排除目录
    picks = _pick_sample_files(str(tmp_path))
    names = {p.name for p in picks}
    assert "Service.java" in names
    assert "ServiceTest.java" not in names
    assert "tiny.java" not in names
    assert "lib.js" not in names
    print("  ✅ 取样过滤测试/过小/排除目录")


def test_infer_empty_project_returns_empty(tmp_path):
    # 空项目无源文件 → 直接返回空，不调 LLM
    assert infer_norms_from_code(str(tmp_path)) == []
    print("  ✅ 空项目返回空（不调 LLM）")


def main():
    tests = [
        test_parse_plain_json_array,
        test_parse_with_code_fence,
        test_parse_invalid_tag_falls_back_general,
        test_parse_garbage_returns_empty,
        test_parse_skips_incomplete_items,
        test_pick_sample_files_filters,
        test_infer_empty_project_returns_empty,
    ]
    import tempfile
    passed = failed = 0
    for t in tests:
        try:
            import inspect
            if "tmp_path" in inspect.signature(t).parameters:
                with tempfile.TemporaryDirectory() as d:
                    t(Path(d))
            else:
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
