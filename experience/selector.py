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
# G6（阶段E，E9-2 更正）：DB 面主信号 = detect_stack 的 db 字段（依赖坐标 ground
# truth，见 stack_detect._DB_DEP_MARKERS）；下方子串词表只作 frontend/backend/build
# 文本的兜底（LLM 裁决画像可能把 "MySQL" 写进 backend 串）。探不出都不挂（宁缺勿错）。
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
    for db, subs in _DB_SUBSTRINGS.items():  # G6：DB 互斥挂载信号（文本兜底）
        if any(sub in text for sub in subs):
            langs.add(db)
    # E9-2：detect_stack 确定性 db 面（依赖坐标 ground truth）——主通道
    for eng in (project_stack.get("db") or []):
        e = str(eng).strip().lower()
        if e:
            langs.add(e)
    return langs


def profile_terms_from_project_stack(project_stack: dict | None) -> set[str]:
    """E9-3（复核 RF2/RF3）：从画像文本抽【框架级】词元（fastapi/django/vue/react/
    spring…），供技能相关性提权——语言级栈轴分不出"FastAPI 项目别推 Django 安全"。
    纯词元切分，不含框架写死清单（技能 id/tags 命中即提权，通用多栈）。"""
    if not isinstance(project_stack, dict):
        return set()
    text = " ".join(str(project_stack.get(k) or "")
                    for k in ("frontend", "backend", "build")).lower()
    return {t for t in re.split(r"[^a-z0-9]+", text) if len(t) >= 3}


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


def _fw_hit(s: SkillDoc, terms: set[str]) -> int:
    """技能与画像的框架级相关性（id 词元/tags 与画像词元双向前缀命中 → 1）。

    前缀而非全等：真实画像是 "Vue3"/"SpringBoot 2.x" 形态，词元切出 "vue3"/
    "springboot"，须命中技能词元 "vue"/"springboot"。词元 <3 字符不参与（防噪声）。"""
    if not terms:
        return 0
    toks = {t for t in (set(s.id.lower().split("-"))
                        | {str(t).lower() for t in (s.tags or ())}) if len(t) >= 3}
    for tok in toks:
        for term in terms:
            if tok.startswith(term) or term.startswith(tok):
                return 1
    return 0


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
    profile_terms: set[str] | None = None,
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
    # G3（阶段E）+E9-3：排序 = 栈特化 > 框架相关性 > priority > id。
    # 旧键 (-priority, id) 在截断点按字母序丢栈特化技能；语言级栈轴又分不出框架
    # （复核实证：FastAPI 项目 push django-security、Vue 项目挂 react-patterns 丢
    # vue-patterns）——框架词元命中提权，确定性无 LLM。
    _terms = profile_terms or set()
    cands.sort(key=lambda s: (0 if "*" in s.applies_to_stacks else -1,
                              -_fw_hit(s, _terms), -s.priority, s.id))

    picked = _budget_pick(cands, budget_chars=budget_chars, max_k=max_k)
    # E9-4（复核 RF14）：通配层保底——G2（3 个位）×G3（特化绝对优先）乘积效应会让
    # 主流栈项目的通配技能（error-handling/api-design/imported）一条都进不了。
    # 特化占满且存在通配候选时，末位让给最优通配（保横切经验可达）。
    if (max_k >= 2 and len(picked) == max_k
            and all("*" not in x.applies_to_stacks for x in picked)):
        _wild = next((c for c in cands if "*" in c.applies_to_stacks), None)
        if _wild is not None:
            picked = picked[:-1] + [_wild]
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
