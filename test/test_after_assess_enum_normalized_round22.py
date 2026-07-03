#!/usr/bin/env python3
"""P0-2 round22：after_assess 枚举归一（复现 graph.py:118 resume 后字符串退化）。

根因：`after_assess` 自造 `comp = state.get("assessed_complexity") or ...`，checkpoint
resume 后该值反序列化成字符串 "ultra" → `"ultra" in (Complexity.COMPLEX, ULTRA)` = False
→ complex/ultra 误走 PLAN 轻量路径（跳过 tech_design/评审）；且 `comp.value` 抛 AttributeError。

治本：复用既有 effective_complexity(state)（brain/state.py:179，归一返回枚举）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.graph import after_assess  # noqa: E402


def test_resume_string_ultra_routes_tech_design():
    # resume 后字符串 "ultra"（非枚举）— 复现点
    route = after_assess({"assessed_complexity": "ultra"})
    assert route == "tech_design", "字符串 ultra 必须走 tech_design（复现 bug：当前误走 plan/抛错）"
    print("  ✅ resume 字符串 ultra → tech_design")


def test_resume_string_complex_routes_tech_design():
    route = after_assess({"assessed_complexity": "complex"})
    assert route == "tech_design"
    print("  ✅ resume 字符串 complex → tech_design")


def test_resume_string_simple_routes_plan():
    route = after_assess({"assessed_complexity": "simple"})
    assert route == "plan"
    print("  ✅ resume 字符串 simple → plan")


def test_enum_ultra_still_works():
    from swarm.types import Complexity
    route = after_assess({"assessed_complexity": Complexity.ULTRA})
    assert route == "tech_design", "枚举路径不回归"
    print("  ✅ 枚举 ULTRA → tech_design（不回归）")


def test_missing_falls_back_medium_plan():
    # 无任何复杂度 → 兜底 MEDIUM → plan（轻量），且不抛
    route = after_assess({})
    assert route == "plan"
    print("  ✅ 缺失 → 兜底 MEDIUM → plan")


def test_subtask_difficulty_coerces_from_string():
    """#2 防回归：resume 后 SubTask.difficulty 若为字符串，pydantic 应 coerce 回枚举。"""
    from swarm.types import SubTask, FileScope, SubTaskDifficulty
    st = SubTask(id="s1", description="d", scope=FileScope(), difficulty="complex")
    assert st.difficulty == SubTaskDifficulty.COMPLEX
    assert hasattr(st.difficulty, "value"), "difficulty 必须是枚举（防 .value AttributeError）"
    print("  ✅ SubTask.difficulty 字符串 coerce 回枚举")


if __name__ == "__main__":
    test_subtask_difficulty_coerces_from_string()
    test_resume_string_ultra_routes_tech_design()
    test_resume_string_complex_routes_tech_design()
    test_resume_string_simple_routes_plan()
    test_enum_ultra_still_works()
    test_missing_falls_back_medium_plan()
    print("\n✅ P0-2 after_assess 枚举归一全部通过")
