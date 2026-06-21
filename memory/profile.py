"""L1 用户画像 — 加载与 LLM Prompt 格式化（Brain / Worker 编排注入）。"""

from __future__ import annotations

import logging
from typing import Any

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

    if keys:
        try:
            from swarm.infra.db import sync_pool

            # P2：原实现按 keys 逐个查（N+1 往返）。改为单次 ANY(%s) 批量取，再在内存里
            # 按 keys 的优先级顺序选首个非空画像（项目专属 > 用户全局 > 旧版 project_id）。
            found: dict[str, dict] = {}
            with sync_pool(conn_str).connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT user_id, profile_json FROM mem_user_profile WHERE user_id = ANY(%s)",
                        (keys,),
                    )
                    for uid, pj in cur.fetchall():
                        if isinstance(pj, dict) and pj:
                            found[str(uid)] = pj
            for key in keys:  # 保持优先级顺序
                if key in found:
                    return _enrich_profile(found[key])
        except Exception as exc:
            # 不静默吞：DB 故障升 error 级（区别于"用户未配画像"的正常缺省），
            # 仍回退默认画像保证编排不中断，但失败在日志可见。
            logger.error("resolve_user_profile DB 查询失败，回退默认画像: %s", exc, exc_info=True)

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
    _dn = identity.get("display_name")
    _role = identity.get("role")
    if _dn or _role:
        if _dn and _role:
            parts.append(f"**负责人**: {_dn}（{_role}）")
        elif _role:
            parts.append(f"**视角**: {_role}")
        else:
            parts.append(f"**负责人**: {_dn}")
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

    # 不可协商红线（no_secrets / must_compile / 风格一致 等）——Worker 也须遵守
    qbar = profile.get("quality_bar")
    if isinstance(qbar, dict) and qbar:
        parts.extend(_flatten_dict_section("不可协商红线", qbar))
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
