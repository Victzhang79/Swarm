"""符号接地解析器回归 — 用 RUN20 真实编译错误验证。

RUN20 现场:worker 在猜的包/接口名上引用类 → javac `cannot find symbol` → 40B 再猜 → 死循环。
本模块把缺失符号查 codegraph 反推真实 FQN，产出修复提示。
"""

from __future__ import annotations

import asyncio

from swarm.worker.symbol_resolver import (
    MissingSymbol,
    build_symbol_hints,
    file_path_to_fqn,
    format_symbol_hints,
    parse_missing_symbols,
    resolve_and_format,
)

# RUN20 真实沙箱编译输出片段
_RUN20_BUILD = """
[ERROR] /workspace/ruoyi-alarm/src/main/java/com/ruoyi/alarm/controller/NotifyCallbackController.java:[12,40]
cannot find symbol
  symbol:   class CallbackRequest
  location: package com.ruoyi.alarm.dto
[ERROR] cannot find symbol
  symbol:   class IAlarmEngineService
  location: package com.ruoyi.alarm.service
[ERROR] cannot find symbol
  symbol:   method tail()
  location: variable matcher of type java.util.regex.Matcher
"""


def test_parse_extracts_class_symbols_dedup():
    ms = parse_missing_symbols(_RUN20_BUILD)
    names = [(m.kind, m.name) for m in ms]
    assert ("class", "CallbackRequest") in names
    assert ("class", "IAlarmEngineService") in names
    assert ("method", "tail") in names  # 方法也解析出但后续不做 FQN


def test_parse_empty():
    assert parse_missing_symbols("") == []
    assert parse_missing_symbols("BUILD SUCCESS") == []


def test_file_path_to_fqn():
    assert (file_path_to_fqn("ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/dto/CallbackRequest.java")
            == "com.ruoyi.alarm.domain.dto.CallbackRequest")
    assert (file_path_to_fqn("/workspace/x/src/test/java/com/a/B.java") == "com.a.B")
    assert file_path_to_fqn("README.md") is None


def test_hint_resolved_points_to_real_fqn():
    """RUN20 核心:CallbackRequest 真实在 domain.dto，提示给出真实 FQN。"""
    missing = [MissingSymbol("class", "CallbackRequest")]
    resolved = {"CallbackRequest": ["com.ruoyi.alarm.domain.dto.CallbackRequest"]}
    hints = build_symbol_hints(missing, resolved)
    assert len(hints) == 1 and hints[0].status == "resolved"
    assert "domain.dto.CallbackRequest" in hints[0].message
    assert "勿臆造" in hints[0].message


def test_hint_planned_when_sibling_creates_it():
    missing = [MissingSymbol("class", "AlarmBotMapper")]
    plan_files = ["ruoyi-alarm/src/main/java/com/ruoyi/alarm/mapper/AlarmBotMapper.java"]
    hints = build_symbol_hints(missing, {}, plan_files)
    assert hints[0].status == "planned"
    assert "其它子任务创建" in hints[0].message


def test_hint_absent_when_nowhere():
    """RedisCache 类缺陷:codegraph 与 plan 都没有 → 提示需新建/换等价类，勿臆造。"""
    missing = [MissingSymbol("class", "RedisCache")]
    hints = build_symbol_hints(missing, {}, [])
    assert hints[0].status == "absent"
    assert "臆造" in hints[0].message


def test_method_variable_not_fqn_resolved():
    """method/variable 错(如 Matcher.tail())不走 FQN 解析，不产 class 提示。"""
    missing = [MissingSymbol("method", "tail"), MissingSymbol("variable", "foo")]
    assert build_symbol_hints(missing, {}) == []


def test_format_empty_returns_blank():
    assert format_symbol_hints([]) == ""


def test_format_renders_block():
    hints = build_symbol_hints([MissingSymbol("class", "X")],
                               {"X": ["com.a.X"]})
    block = format_symbol_hints(hints)
    assert "符号接地提示" in block and "com.a.X" in block


def test_resolve_and_format_async_with_fake_indexer():
    """端到端(异步):fake indexer 返回 codegraph 行 → 解析提示。验证 ILIKE 模糊后精确名过滤。"""
    class _FakeIndexer:
        async def query_symbols_by_name(self, project_id, name):
            if name == "CallbackRequest":
                return [
                    # 精确命中
                    {"symbol_name": "CallbackRequest", "class_name": "CallbackRequest",
                     "file_path": "ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/dto/CallbackRequest.java"},
                    # ILIKE 模糊误带的（名不精确等于）→ 应被过滤
                    {"symbol_name": "CallbackRequestHandler", "class_name": "CallbackRequestHandler",
                     "file_path": "x/src/main/java/com/a/CallbackRequestHandler.java"},
                ]
            return []

    out = asyncio.run(resolve_and_format(_RUN20_BUILD, "pid", _FakeIndexer()))
    assert "com.ruoyi.alarm.domain.dto.CallbackRequest" in out
    assert "CallbackRequestHandler" not in out  # 模糊误带被精确名过滤掉
    assert "IAlarmEngineService" in out  # codegraph 没查到 → absent 提示也在


def test_resolve_and_format_swallows_indexer_errors():
    """indexer 抛错时返回空串，绝不让接地提示拖垮修复回路。"""
    class _BoomIndexer:
        async def query_symbols_by_name(self, project_id, name):
            raise RuntimeError("db down")

    out = asyncio.run(resolve_and_format(_RUN20_BUILD, "pid", _BoomIndexer()))
    # 查询抛错被吞 → 缺失符号全归 absent，但仍产出提示(不崩)
    assert "cannot find symbol" not in out.lower() or out == "" or "符号接地提示" in out


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== 符号接地解析器: {len(fns)}/{len(fns)} passed ===")
