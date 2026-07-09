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
from swarm.experience.selector import (
    profile_terms_from_project_stack,
    select_skills,
    stack_langs_from_project_stack,
)

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
    # E9-7（复核 HF8/RF15）：生产零调用的旧入口收编为 push/pull 的兼容壳——避免
    # "两套选择逻辑并存"的未来误用面（docstring 曾承诺与目录同源，已不再成立）。
    push, pull = select_worker_push_pull(subtask, project_stack)
    return ([push] if push is not None else []) + list(pull)


def select_worker_push_pull(subtask, project_stack: dict | None = None):
    """G8（阶段E 拍板）：push top-1 栈特化 + pull ≤worker_max_tools 的混合分离。

    离散 pull 把选择负担压给最弱环节（小模型）；最相关的一条【栈特化】技能改为全文
    push 进 prompt（零迭代成本），其余候选仍走按需 pull 工具。top 候选是通配技能时
    不 push（泛化建议不值得无条件占 prefill）。返回 (push_skill|None, pull_skills)。
    push 与 pull 不重叠（已 push 全文的技能不再占工具位）。fail-open → (None, [])。
    """
    try:
        from swarm.config.settings import get_config

        cfg = get_config().skills
        if not cfg.enabled:
            return None, []
        skills = _merged_skills(cfg.dir_list())
        if not skills:
            return None, []
        if cfg.worker_max_tools <= 0:
            # E9-5（复核 RF5）：0 = worker 侧经验全关（push 也关）——否则"0=不挂经验
            # 工具"的配置承诺静默漂移成"只关 pull"。
            return None, []
        intent = str(
            getattr(getattr(subtask, "intent", ""), "value", getattr(subtask, "intent", "")) or ""
        ).lower()
        stack_langs = stack_langs_from_project_stack(project_stack)
        terms = profile_terms_from_project_stack(project_stack)
        picked = select_skills(
            skills, stack_langs=stack_langs, intent=intent, phase="code",
            target="worker", budget_chars=10**9,
            max_k=cfg.worker_max_tools + 1,  # +1：top-1 归 push，其余归 pull
            rerank_fn=None, profile_terms=terms,
        )
        if not picked:
            return None, []
        _top = picked[0]
        # E9-3（复核 RF2）：push 门槛收紧——仅当 top 栈特化且【与画像框架级相关】
        # （框架词元命中，或 id 语言前缀 ∈ 探出语言集，如 java-coding-standards）。
        # 否则"任意栈特化即 push"会把 django-security 全文塞给 FastAPI 项目。
        from swarm.experience.selector import _fw_hit
        _lang_prefix = _top.id.split("-", 1)[0].lower() in stack_langs
        if "*" not in _top.applies_to_stacks and (_fw_hit(_top, terms) or _lang_prefix):
            return _top, picked[1:]
        return None, picked[:cfg.worker_max_tools]
    except Exception as e:  # noqa: BLE001 — 经验层绝不拖垮主流程
        logger.warning("[skills] worker push/pull 选择失败，降级为空：%s", e)
        return None, []


def worker_skills_block(subtask, project_stack: dict | None = None) -> str:
    """为 Worker 系统提示生成经验块：push 技能【全文】+ pull 工具目录（G8 混合）。

    与 build_worker_experience_tools 用同一 select_worker_push_pull，保证"目录里的
    工具"与"实际挂上的工具"一一对应。空/禁用/异常 → ""。
    """
    try:
        push, pull = select_worker_push_pull(subtask, project_stack)
        parts = []
        if push is not None:
            parts.append(render_skills_block([push]))
        catalog = render_experience_tool_catalog(pull)
        if catalog:
            parts.append(catalog)
        return "\n".join(p for p in parts if p)
    except Exception as e:  # noqa: BLE001
        logger.warning("[skills] worker 目录渲染失败，降级为空：%s", e)
        return ""


def build_worker_experience_tools(subtask, project_stack: dict | None = None):
    """把 worker 上下文匹配的候选技能构建成离散工具列表。异常/禁用/无命中 → []（fail-open）。"""
    try:
        from swarm.config.settings import get_config
        from swarm.experience.tools import build_experience_tools

        _, skills = select_worker_push_pull(subtask, project_stack)  # G8：只挂 pull 侧
        if not skills:
            return []
        return build_experience_tools(
            skills, max_chars=get_config().skills.tool_body_max_chars,
            subtask_id=str(getattr(subtask, "id", "") or ""),  # G4：遥测 join 键
        )
    except Exception as e:  # noqa: BLE001 — 绝不拖垮 worker agent 创建
        logger.warning("[skills] 构建 worker 经验工具失败，降级为空：%s", e)
        return []


def preview_mount_surfaces(doc: SkillDoc) -> dict:
    """G9（阶段E）：挂载预览——该技能会出现在哪些【栈×意图】的注入面/工具面及排位。

    保存前展示影响面（质量闸从"只挡恶意"补到"可见平庸的代价"）：worker 侧模拟
    push/pull 分离排位；planner 侧模拟全文注入选择。纯确定性干跑，不落库不调 LLM。
    """
    from swarm.config.settings import get_config

    cfg = get_config().skills
    others = [d for d in _merged_skills(cfg.dir_list()) if d.id != doc.id]
    pool = others + [doc]
    # E9-6（复核 HF6/RF7）：输入钳制（防 authenticated CPU DoS：面数=栈×意图全库选择）
    rep_stacks = (list(doc.applies_to_stacks)[:8] if "*" not in doc.applies_to_stacks
                  else ["java", "python", "node", "go"])
    rep_intents = (list(doc.applies_to_intents)[:5] if "*" not in doc.applies_to_intents
                   else ["create", "modify"])
    surfaces: list[dict] = []
    for st_tag in rep_stacks:
        for it in rep_intents:
            if "worker" in doc.target:
                picked = select_skills(
                    pool, stack_langs={st_tag}, intent=it, phase="code",
                    target="worker", budget_chars=10**9,
                    max_k=max(cfg.worker_max_tools, 0) + 1)
                ids = [x.id for x in picked]
                rank = ids.index(doc.id) if doc.id in ids else -1
                mode = ""
                if rank == 0 and picked and "*" not in picked[0].applies_to_stacks:
                    mode = "push"
                elif rank >= 0:
                    mode = "pull"
                surfaces.append({"stack": st_tag, "intent": it, "target": "worker",
                                 "mounted": rank >= 0, "rank": rank, "mode": mode})
            if "planner" in doc.target:
                picked_p = select_skills(
                    pool, stack_langs={st_tag}, intent="*", phase="plan",
                    target="planner", budget_chars=cfg.planner_budget_chars,
                    max_k=cfg.max_k)
                ids_p = [x.id for x in picked_p]
                rank_p = ids_p.index(doc.id) if doc.id in ids_p else -1
                surfaces.append({"stack": st_tag, "intent": "*", "target": "planner",
                                 "mounted": rank_p >= 0, "rank": rank_p,
                                 "mode": "planner_push" if rank_p >= 0 else ""})
    # E9-6：预览诚实化——层开关关/技能本身 disabled/库空 与"真不匹配"必须可区分；
    # 单栈模拟排位在多栈真项目会更靠后，明示防乐观误导（复核 RF4）。
    return {"surfaces": surfaces,
            "layer_enabled": bool(cfg.enabled),
            "doc_enabled": bool(getattr(doc, "enabled", True)),
            "pool_size": len(pool),
            "note": "单栈理想面模拟；多栈项目候选更多、实际排位可能更靠后"}


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
