"""复杂度误判 SIMPLE 修复的回归测试。

E2E 三轮验证暴露真 bug：`_heuristic_complexity` 关键词短路抢在 LLM 前拦截——命中"注释/typo"
等词就直接判 SIMPLE、连云端大模型都不调，导致跨文件/新建类任务被误降级成单子任务、
单容器、不并行、不走 L6。

正确修复（非堆关键词）：**废弃启发式短路，复杂度一律由带知识库检索结果的云端 Brain 大模型判定**。
本测试固化：①该短路已移除（analyze 不再有 _heuristic_complexity 前置 return）；
②read_task_logs 有回退扫描修 done 任务查不到日志。
"""
from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_heuristic_shortcut_removed():
    """启发式关键词短路应已彻底移除——shared.py 不再有 _heuristic_complexity，
    analyze 不再在 LLM 前用关键词判死复杂度。"""
    import swarm.brain.nodes.shared as shared
    assert not hasattr(shared, "_heuristic_complexity"), "启发式短路未移除"
    assert not hasattr(shared, "_COMPLEXITY_AMPLIFIERS"), "关键词护栏表未移除"
    print("  ✅ 启发式关键词短路/护栏表已移除")


def test_analyze_routes_complexity_to_llm():
    """analyze 节点的复杂度判定应走 LLM（带知识库 prompt），无关键词前置 return。"""
    from swarm.brain.nodes import analyze
    src = inspect.getsource(analyze)
    # 必须把知识库检索结果喂进判级 prompt
    assert "format_brain_knowledge_prompt" in src, "未把知识库喂进判级"
    assert "ANALYZE_SYSTEM" in src and "ANALYZE_USER" in src, "未走 LLM 判级 prompt"
    # 不应再调用启发式短路（注释里提及历史不算；检查没有实际调用/前置 return）
    assert "_heuristic_complexity(" not in src, "analyze 仍残留启发式短路调用"
    assert "heuristic is not None" not in src, "analyze 仍有启发式前置 return 分支"
    print("  ✅ analyze 复杂度走带知识库的 LLM 判定")


def test_analyze_prompt_has_classification_rules():
    """LLM 判级 prompt 应含明确判级铁律（新建文件/跨文件 → 至少 medium），指导大模型判准。"""
    from swarm.brain.prompts import ANALYZE_SYSTEM
    assert "新建" in ANALYZE_SYSTEM and "medium" in ANALYZE_SYSTEM
    assert "就高不就低" in ANALYZE_SYSTEM or "至少 medium" in ANALYZE_SYSTEM
    print("  ✅ 判级 prompt 含明确铁律指导大模型")


def test_read_task_logs_has_fallback_scan():
    """read_task_logs 应有回退全量+backup 扫描（修 done 任务查不到日志）。"""
    from swarm.logging_config import read_task_logs
    src = inspect.getsource(read_task_logs)
    assert "backup" in src.lower() or "glob" in src.lower(), "缺回退扫描 backup 逻辑"
    assert "tail=None" in src, "缺全量扫描回退"
    print("  ✅ read_task_logs 有回退扫描")


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-v", "-s"]))
