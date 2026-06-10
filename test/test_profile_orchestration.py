#!/usr/bin/env python3
"""L1 用户画像 — 编排注入测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.auth.default_profile import DEFAULT_ADMIN_PROFILE
from swarm.brain.prompts import ANALYZE_USER
from swarm.memory.profile import (
    format_user_profile_for_brain,
    format_user_profile_for_worker,
    load_profile_prompts,
    resolve_user_profile,
)
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality
from swarm.worker.prompts import build_worker_prompt


def test_default_profile_has_llm_instructions():
    assert "instructions_for_brain" in DEFAULT_ADMIN_PROFILE
    assert "instructions_for_worker" in DEFAULT_ADMIN_PROFILE
    assert len(DEFAULT_ADMIN_PROFILE["instructions_for_brain"]) >= 3
    print("  ✅ default profile has brain/worker instructions")


def test_format_injects_into_brain_prompt():
    _, brain_prompt, _ = load_profile_prompts(None, "proj-test")
    assert "编排指令" in brain_prompt or "L1" in brain_prompt
    user_msg = ANALYZE_USER.format(
        task_description="add api",
        user_profile=brain_prompt,
        knowledge_context="ctx",
        recent_tasks="（无近期任务）",
        session_metadata="{}",
        sliding_context="",
    )
    assert "add api" in user_msg
    assert "用户画像" in user_msg
    assert "最小" in user_msg or "编排" in user_msg
    print("  ✅ brain prompt includes user profile section")


def test_format_injects_into_worker_prompt():
    profile = resolve_user_profile(None, "p1")
    worker_section = format_user_profile_for_worker(profile)
    st = SubTask(
        id="st-1",
        description="fix bug",
        difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(writable=["a.py"], readable=["a.py"]),
    )
    prompt = build_worker_prompt(st, user_profile_prompt=worker_section)
    assert "用户画像" in prompt
    assert "fix bug" in prompt
    assert "实现指令" in prompt or "编码偏好" in prompt
    print("  ✅ worker system prompt includes user profile")


def test_enrich_legacy_profile():
    legacy = {"preferences": {"language": "zh-CN"}, "workflow": {"review_before_apply": True}}
    brain = format_user_profile_for_brain(
        __import__("swarm.memory.profile", fromlist=["_enrich_profile"])._enrich_profile(legacy)
    )
    assert "编排指令" in brain
    print("  ✅ legacy profile enriched with instructions")


def main() -> int:
    print("=== test_profile_orchestration ===")
    tests = [
        test_default_profile_has_llm_instructions,
        test_format_injects_into_brain_prompt,
        test_format_injects_into_worker_prompt,
        test_enrich_legacy_profile,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
            import traceback
            traceback.print_exc()
    if failed:
        return 1
    print("\nAll passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
