"""经验拔插层的编排入口：config → loader → selector → injector。

wiring（worker/prompts.py、brain plan 节点）只调这里的两个函数：
  - worker_skills_block(subtask, project_stack)
  - planner_skills_block(project_stack)

**全程 fail-open**：本层是 advisory 知识注入，任何异常（配置坏/目录缺/技能坏/选择器
抛错）都返回空串 "" 让主流程照跑，绝不因经验层拖垮交付。总开关 SWARM_SKILLS_ENABLED=0
= 整层旁路（不加载不注入）。不依赖任何 CLI / 外部服务（rerank 关时纯本地文件+计算）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from swarm.experience.injector import (
    render_experience_tool_catalog,
    render_skills_block,
)
from swarm.experience.library import load_skills_from
from swarm.experience.models import SkillDoc
from swarm.experience.selector import select_skills, stack_langs_from_project_stack

logger = logging.getLogger(__name__)

# 技能库缓存：key=解析后的目录元组，value=已加载 SkillDoc 列表。
# 技能库是启动即定的小型静态资产（无需每次拆 prompt 重读盘）；config reload 经
# invalidate_cache() 清缓存（在 settings.reload_config 的 store 刷新循环里登记）。
_CACHE: dict[tuple[str, ...], list[SkillDoc]] = {}


def invalidate_cache() -> None:
    """清空技能库缓存（.env/config 热更新后由 reload_config 调用）。"""
    _CACHE.clear()


def _resolve_dirs(dir_list: list[str]) -> tuple[str, ...]:
    """把配置的（可能相对的）目录解析成绝对路径元组。相对路径以包根解析。"""
    from swarm.config.settings import PROJECT_ROOT

    resolved: list[str] = []
    for d in dir_list:
        p = Path(d)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        resolved.append(str(p))
    return tuple(resolved)


def _load_cached(dir_list: list[str]) -> list[SkillDoc]:
    key = _resolve_dirs(dir_list)
    cached = _CACHE.get(key)
    if cached is None:
        cached = load_skills_from(key)
        _CACHE[key] = cached
        logger.debug("[skills] 加载技能库 %s → %d 条", list(key), len(cached))
    return cached


def _merged_skills(dir_list: list[str]) -> list[SkillDoc]:
    """合并【内置种子（文件系统）∪ DB 系统级技能】。DB 同 id 覆盖内置（用户定制优先）。

    DB 读取自带 fail-open（get_enabled_docs 出错返回 []）→ 退化为纯内置种子。
    """
    fs = _load_cached(dir_list)
    try:
        from swarm.config import skill_store
        db_docs = skill_store.get_enabled_docs()
    except Exception as e:  # noqa: BLE001 — DB 不可用不拖垮经验层
        logger.warning("[skills] DB 技能读取失败,仅用内置种子: %s", e)
        db_docs = []
    if not db_docs:
        return fs
    by_id: dict[str, SkillDoc] = {d.id: d for d in fs}
    for d in db_docs:  # DB 覆盖同 id 内置
        by_id[d.id] = d
    return sorted(by_id.values(), key=lambda d: d.id)


def _render_block(
    *, stack_langs: set[str], intent: str, phase: str, target: str, budget_chars: int
) -> str:
    """选择 + 渲染。任一步异常 → ""（fail-open）。"""
    from swarm.config.settings import get_config

    try:
        cfg = get_config().skills
        if not cfg.enabled:
            return ""
        skills = _merged_skills(cfg.dir_list())
        if not skills:
            return ""
        picked = select_skills(
            skills,
            stack_langs=stack_langs,
            intent=intent,
            phase=phase,
            target=target,
            budget_chars=budget_chars,
            max_k=cfg.max_k,
            rerank_fn=None,  # P6：rerank 落地后按 cfg.rerank 挂 _llm_rerank；默认确定性
        )
        return render_skills_block(picked)
    except Exception as e:  # noqa: BLE001 — advisory，绝不阻断主流程
        logger.warning("[skills] 注入失败，降级为空（不影响交付）：%s", e)
        return ""


def select_worker_skills(subtask, project_stack: dict | None = None) -> list[SkillDoc]:
    """选出与当前 worker 上下文（栈×意图×阶段=code）匹配的候选技能（供挂成离散工具）。

    确定性 + 缓存 + fail-open（异常/禁用/无库 → []）。**必须**与 worker_skills_block 用同一
    选择逻辑，保证"提示里的工具目录"与"实际挂上的工具"一一对应。字符预算给足（工具候选由
    worker_max_tools 封顶，不靠字符裁剪），正文按需 pull 才计费。
    """
    try:
        from swarm.config.settings import get_config

        cfg = get_config().skills
        if not cfg.enabled:
            return []
        skills = _merged_skills(cfg.dir_list())
        if not skills:
            return []
        intent = str(
            getattr(getattr(subtask, "intent", ""), "value", getattr(subtask, "intent", "")) or ""
        ).lower()
        stack_langs = stack_langs_from_project_stack(project_stack)
        return select_skills(
            skills,
            stack_langs=stack_langs,
            intent=intent,
            phase="code",
            target="worker",
            budget_chars=10**9,  # 工具候选不靠字符预算裁剪，只受 worker_max_tools 封顶
            max_k=cfg.worker_max_tools,
            rerank_fn=None,
        )
    except Exception as e:  # noqa: BLE001 — 经验层绝不拖垮主流程
        logger.warning("[skills] worker 技能选择失败，降级为空：%s", e)
        return []


def worker_skills_block(subtask, project_stack: dict | None = None) -> str:
    """为 Worker 系统提示生成【经验工具目录】（轻量：工具名+标题+摘要，不含正文）。

    正文由离散 experience__<id> 工具按需 pull；本目录只提示这些工具存在、各管什么。
    空/禁用/异常 → ""。
    """
    try:
        return render_experience_tool_catalog(select_worker_skills(subtask, project_stack))
    except Exception as e:  # noqa: BLE001
        logger.warning("[skills] worker 目录渲染失败，降级为空：%s", e)
        return ""


def build_worker_experience_tools(subtask, project_stack: dict | None = None):
    """把 worker 上下文匹配的候选技能构建成离散工具列表。异常/禁用/无命中 → []（fail-open）。"""
    try:
        from swarm.config.settings import get_config
        from swarm.experience.tools import build_experience_tools

        skills = select_worker_skills(subtask, project_stack)
        if not skills:
            return []
        return build_experience_tools(
            skills, max_chars=get_config().skills.tool_body_max_chars,
            subtask_id=str(getattr(subtask, "id", "") or ""),  # G4：遥测 join 键
        )
    except Exception as e:  # noqa: BLE001 — 绝不拖垮 worker agent 创建
        logger.warning("[skills] 构建 worker 经验工具失败，降级为空：%s", e)
        return []


def planner_skills_block(project_stack: dict | None = None) -> str:
    """为 Planner（plan 节点）生成技能注入块。空/禁用/异常 → ""。

    栈来自 project_stack；阶段固定 'plan'；意图在规划期尚未拆到子任务，用 '*' 表示
    "不按意图轴过滤"（栈×plan 预筛即可，见 handoff §6）。
    """
    try:
        from swarm.config.settings import get_config

        budget = get_config().skills.planner_budget_chars
        stack_langs = stack_langs_from_project_stack(project_stack)
        return _render_block(
            stack_langs=stack_langs,
            intent="*",
            phase="plan",
            target="planner",
            budget_chars=budget,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[skills] planner 注入失败，降级为空：%s", e)
        return ""
