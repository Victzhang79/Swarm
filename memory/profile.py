"""L1 用户画像 — 加载与 LLM Prompt 格式化（Brain / Worker 编排注入）。"""

from __future__ import annotations

import json
import logging
from typing import Any

import psycopg

from swarm.auth.default_profile import DEFAULT_ADMIN_PROFILE, GLOBAL_PROFILE_SUFFIX
from swarm.auth.store import profile_key
from swarm.config.settings import DatabaseConfig

logger = logging.getLogger(__name__)

_EMPTY_BRAIN = "（未配置用户画像；Brain 使用系统默认编排策略）"
_EMPTY_WORKER = "（未配置用户画像；Worker 使用系统默认实现策略）"


def _conn_str(db_config: DatabaseConfig | None = None) -> str:
    return (db_config or DatabaseConfig()).postgres_uri


def resolve_user_profile(
    user_id: str | None,
    project_id: str,
    *,
    conn_str: str | None = None,
) -> dict[str, Any]:
    """按 项目专属 → 用户全局 → 旧版 project_id → 代码默认 回退。"""
    conn_str = conn_str or _conn_str()
    if not user_id:
        user_id = _default_admin_user_id(conn_str)

    keys: list[str] = []
    if user_id and project_id:
        keys.append(profile_key(user_id, project_id))
    if user_id:
        keys.append(profile_key(user_id, GLOBAL_PROFILE_SUFFIX))
    if project_id:
        keys.append(project_id)

    try:
        from swarm.infra.db import sync_pool

        with sync_pool(conn_str).connection() as conn:
            with conn.cursor() as cur:
                for key in keys:
                    cur.execute(
                        "SELECT profile_json FROM mem_user_profile WHERE user_id = %s",
                        (key,),
                    )
                    row = cur.fetchone()
                    if row and isinstance(row[0], dict) and row[0]:
                        return _enrich_profile(row[0])
    except Exception as exc:
        logger.warning("resolve_user_profile failed: %s", exc)

    return _enrich_profile(dict(DEFAULT_ADMIN_PROFILE))


def _enrich_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """旧版画像补全 instructions 字段，保证可注入 LLM。"""
    out = dict(profile)
    if out.get("version") == 1 and out.get("instructions_for_brain"):
        return out
    for key in ("identity", "instructions_for_brain", "instructions_for_worker"):
        if not out.get(key) and DEFAULT_ADMIN_PROFILE.get(key):
            out[key] = DEFAULT_ADMIN_PROFILE[key]
    return out


def _default_admin_user_id(conn_str: str) -> str | None:
    try:
        from swarm.infra.db import sync_pool

        with sync_pool(conn_str).connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM swarm_users WHERE username = %s LIMIT 1",
                    ("admin",),
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception:
        return None


def _bullet_lines(items: list[Any]) -> list[str]:
    lines: list[str] = []
    for i, item in enumerate(items, 1):
        if isinstance(item, str) and item.strip():
            lines.append(f"{i}. {item.strip()}")
    return lines


def _flatten_dict_section(title: str, data: dict[str, Any]) -> list[str]:
    if not data:
        return []
    lines = [f"### {title}"]
    for key, val in data.items():
        if isinstance(val, list):
            lines.append(f"- **{key}**: {', '.join(str(v) for v in val)}")
        elif isinstance(val, bool):
            lines.append(f"- **{key}**: {'是' if val else '否'}")
        elif val is not None and val != "":
            lines.append(f"- **{key}**: {val}")
    return lines


def format_user_profile_for_brain(profile: dict[str, Any]) -> str:
    """格式化为 Brain analyze/plan/validate 节点可读 Markdown。"""
    if not profile:
        return _EMPTY_BRAIN

    parts = [
        "## 用户画像（L1 — Brain 编排约束）",
        "> 以下为用户/项目负责人偏好。**任务拆解、计划验证、复杂度判断**须与此一致。",
        "",
    ]

    identity = profile.get("identity") or {}
    if identity.get("display_name") or identity.get("role"):
        parts.append(
            f"**负责人**: {identity.get('display_name', '—')} "
            f"（{identity.get('role', 'developer')}）"
        )
        parts.append("")

    brain_ins = profile.get("instructions_for_brain")
    if isinstance(brain_ins, list) and brain_ins:
        parts.append("### 编排指令")
        parts.extend(_bullet_lines(brain_ins))
        parts.append("")

    for section_key, title in (
        ("workflow", "工作流偏好"),
        ("quality_bar", "质量门槛"),
        ("preferences", "通用偏好"),
    ):
        block = profile.get(section_key)
        if isinstance(block, dict) and block:
            parts.extend(_flatten_dict_section(title, block))
            parts.append("")

    notes = profile.get("notes")
    if isinstance(notes, str) and notes.strip():
        parts.append(f"### 备注\n{notes.strip()}")

    text = "\n".join(parts).strip()
    return text or _EMPTY_BRAIN


def format_user_profile_for_worker(profile: dict[str, Any]) -> str:
    """格式化为 Worker 系统提示词段落。"""
    if not profile:
        return _EMPTY_WORKER

    parts = [
        "## 👤 用户画像（L1 — 实现约束）",
        "> 编码、验证、产出时必须遵循以下用户偏好。",
        "",
    ]

    worker_ins = profile.get("instructions_for_worker")
    if isinstance(worker_ins, list) and worker_ins:
        parts.append("### 实现指令")
        parts.extend(_bullet_lines(worker_ins))
        parts.append("")

    prefs = profile.get("preferences")
    if isinstance(prefs, dict) and prefs:
        parts.extend(_flatten_dict_section("编码偏好", prefs))
        parts.append("")

    stack = profile.get("tech_stack")
    if isinstance(stack, dict) and stack:
        parts.extend(_flatten_dict_section("技术栈", stack))
        parts.append("")

    text = "\n".join(parts).strip()
    return text or _EMPTY_WORKER


def load_profile_prompts(
    user_id: str | None,
    project_id: str,
    *,
    conn_str: str | None = None,
) -> tuple[dict[str, Any], str, str]:
    """返回 (profile_dict, brain_prompt, worker_prompt)。"""
    profile = resolve_user_profile(user_id, project_id, conn_str=conn_str)
    return (
        profile,
        format_user_profile_for_brain(profile),
        format_user_profile_for_worker(profile),
    )
