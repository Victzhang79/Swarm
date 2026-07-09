"""P1 · 混合选择器：栈×意图×阶段 标签预筛 → 优先级+预算截断 → 可选 LLM rerank。

纯函数，无副作用、不连网（rerank 经注入的 callable 才可能触网，默认 None）。确定性
预筛保相关+有界成本，符合 swarm 确定性哲学；rerank 是可选增强，默认关。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence

from swarm.experience.models import SkillDoc

logger = logging.getLogger(__name__)

# 栈画像文本 → 归一语言集。多栈【对称】映射表（不偏向任何单一栈/项目），语义对齐
# stack_detect._MANIFEST_BACKEND 的 backend_lang 词表。key=会在 project_stack 的
# frontend/backend/build 文本里出现的信号子串；value=技能 applies_to_stacks 用的归一语言。
# 说明：这是"语言"层面的通用词表，非项目/框架写死——扩栈只需在此加一行对称条目。
_LANG_SUBSTRINGS: dict[str, tuple[str, ...]] = {
    "python": ("python", "django", "flask", "fastapi", "poetry"),
    "node": (
        "javascript", "typescript", "node", "npm", "pnpm", "yarn",
        "react", "vue", "angular", "svelte", "next.js", "express", "nest",
    ),
    "java": ("spring", "maven", "gradle", "jvm", "mybatis"),
    "kotlin": ("kotlin",),
    "go": ("golang",),
    "rust": ("rust", "cargo"),
    "cpp": ("c++", "cpp", "cmake"),
    "csharp": ("csharp", "c#", ".net", "dotnet", ".csproj"),
    "php": ("php", "laravel", "composer", "symfony"),
    "ruby": ("ruby", "rails", "gemfile"),
}
# G6（阶段E）：DB 面词表——画像文本（frontend/backend/build/evidence 拼串）提及才挂
# 对应 DB 技能（互斥）；探不出都不挂（宁缺勿错，错库建议是负资产）。词表对称扩展。
_DB_SUBSTRINGS: dict[str, tuple[str, ...]] = {
    "mysql": ("mysql", "mariadb"),
    "postgres": ("postgres", "postgresql", "pgsql"),
}
# 需按【词边界】判定的语言（子串会误伤：'java' ⊂ 'javascript'，'go' ⊂ 'mongo'）。
_LANG_WORDS: dict[str, tuple[str, ...]] = {
    "python": ("py",),
    "java": ("java",),
    "go": ("go",),
}


def stack_langs_from_project_stack(project_stack: dict | None) -> set[str]:
    """从 project_stack（frontend/backend/build）归一出语言集，供 selector 栈轴匹配。

    容错：project_stack 为 None/空/异常 → 空集（selector 会退化为只命中 `*` 技能）。
    """
    if not isinstance(project_stack, dict):
        return set()
    parts = [
        str(project_stack.get("frontend") or ""),
        str(project_stack.get("backend") or ""),
        str(project_stack.get("build") or ""),
    ]
    text = " ".join(parts).lower()
    if not text.strip():
        return set()
    langs: set[str] = set()
    for lang, subs in _LANG_SUBSTRINGS.items():
        if any(sub in text for sub in subs):
            langs.add(lang)
    for lang, words in _LANG_WORDS.items():
        if any(re.search(rf"\b{re.escape(w)}\b", text) for w in words):
            langs.add(lang)
    for db, subs in _DB_SUBSTRINGS.items():  # G6：DB 互斥挂载信号
        if any(sub in text for sub in subs):
            langs.add(db)
    return langs


def _match_stacks(skill_stacks: Sequence[str], stack_langs: set[str]) -> bool:
    """技能栈标签命中：'*' 或与当前栈语言集有交集。"""
    if "*" in skill_stacks:
        return True
    return bool(set(skill_stacks) & stack_langs)


def _match_one(skill_vals: Sequence[str], value: str) -> bool:
    """意图/阶段单值命中（'*' 两侧通配）：

    - 技能侧含 '*' → 匹配任意查询（该技能对全意图/全阶段适用）；
    - 查询值为 '*' → 匹配任意技能（调用方明确"不按此轴过滤"，如 planner 期意图未定）；
    - 否则精确包含。value 为空 → 只命中技能侧 '*'。
    """
    if "*" in skill_vals or value == "*":
        return True
    return bool(value) and value in skill_vals


# rerank callable 契约：(candidates, max_k, budget_chars) -> 选中的 SkillDoc 列表 | None。
# None/异常 → selector 回退确定性结果（fail-open）。
RerankFn = Callable[[Sequence[SkillDoc], int, int], Sequence[SkillDoc] | None]


def _budget_pick(
    cands: list[SkillDoc], *, budget_chars: int, max_k: int
) -> list[SkillDoc]:
    """优先级降序 → 预算截断（continue 而非 break，允许小技能填缝）→ max_k 硬上限。"""
    picked: list[SkillDoc] = []
    used = 0
    for s in cands:
        if len(picked) >= max_k:  # 硬上限先判：max_k<=0 → 一条不选（不再多带一条）
            break
        cost = min(len(s.body), s.max_chars) if s.max_chars > 0 else len(s.body)
        if used + cost > budget_chars:
            continue
        picked.append(s)
        used += cost
    return picked


def select_skills(
    skills: Iterable[SkillDoc],
    *,
    stack_langs: set[str],
    intent: str,
    phase: str,
    target: str,
    budget_chars: int,
    max_k: int,
    rerank_fn: RerankFn | None = None,
) -> list[SkillDoc]:
    """选出注入用技能（handoff §5 混合算法）。

    ① 确定性标签预筛：target 命中 + 栈×意图×阶段 命中。
    ② 优先级降序 + 预算截断（填缝）+ max_k。
    ③ 可选 LLM rerank：仅 rerank_fn 提供且候选 > max_k 时；失败回退 ②。

    栈/意图/阶段任一为空时退化为宽匹配（空 stack_langs → 只命中 '*' 栈技能；空
    intent/phase → 只命中 '*' 意图/阶段技能），不会误命中。
    """
    intent = (intent or "").strip().lower()
    phase = (phase or "").strip().lower()
    cands = [
        s
        for s in skills
        if getattr(s, "enabled", True)  # G5/G6：disabled 绝不进任何注入面/工具面
        and target in s.target
        and _match_stacks(s.applies_to_stacks, stack_langs)
        and _match_one(s.applies_to_intents, intent)
        and _match_one(s.applies_to_phases, phase)
    ]
    # G3（阶段E）：栈特化（非'*'）先于 priority——旧键 (-priority, id) 在截断点按字母序丢
    # 栈特化技能（实测 Vue 项目丢 vue-patterns(48) 留 mysql(48)+postgres(50) 双通配）。
    cands.sort(key=lambda s: (0 if "*" in s.applies_to_stacks else -1, -s.priority, s.id))

    picked = _budget_pick(cands, budget_chars=budget_chars, max_k=max_k)
    if len(picked) < len(cands):
        # G3：截断必须可观测——静默 drop 让"配了但从未生效"与"没配"在日志上不可分。
        _picked_ids = {s.id for s in picked}
        logger.debug(
            "[skills] 候选 %d 条截断至 %d（target=%s intent=%s）；dropped=%s",
            len(cands), len(picked), target, intent,
            [s.id for s in cands if s.id not in _picked_ids],
        )

    if rerank_fn is not None and len(cands) > max_k:
        try:
            reranked = rerank_fn(cands, max_k, budget_chars)
        except Exception as e:  # noqa: BLE001 — rerank 永不阻断，失败回退确定性结果
            # 必须留痕：否则 P6 接入真 LLM rerank 后，"每次都失败静默回退"与"rerank 未启用"
            # 在日志里无法区分（正是本层要防的"启用 vs 坏了不可分"）。
            logger.warning("[skills] rerank 失败，回退确定性排序：%s", e)
            reranked = None
        if reranked:
            # rerank 只重排/精选，仍受预算+max_k 约束（防其越界）
            picked = _budget_pick(list(reranked), budget_chars=budget_chars, max_k=max_k)
    return picked
