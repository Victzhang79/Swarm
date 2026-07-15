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
    *, stack_langs: set[str], intent: str, phase: str, target: str, budget_chars: int,
    exclude_tags: set[str] | None = None,
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
            exclude_tags=exclude_tags,
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
    pushes, pulls = select_worker_push_pull(subtask, project_stack)
    return list(pushes) + list(pulls)


# 路径/构建面的**泛词**：几乎每个子任务都有，零区分度，且会制造跨栈误推
# （Gradle 工程的 build.gradle 命中 maven-build-lifecycle 的 "build" 词元 → Maven 经验
# 漏进 Gradle 工程）。相关性只认有信息量的词（mapper/controller/security/migration…）。
_NOISE_TERMS = frozenset({
    "src", "main", "app", "com", "org", "net", "impl", "resources", "target",
    "build", "project", "module", "modules", "file", "files", "code", "new",
})


def _subtask_terms(subtask) -> set[str]:
    """R53-7：从子任务本身提取词元——描述 + 它要写的文件路径（目录/文件名/扩展名）。

    经验层此前对子任务内容完全盲（选择器只吃 stack/intent/phase/画像词元），导致三轮
    104 次 push 全是同一对最泛技能。文件路径是最硬的信号：`*/pom.xml`→构建，`*Mapper.java`
    /`*Entity.java`/`*.sql`→持久化，`*Controller.java`→API，`*Test.java`→测试。
    栈无关：只做词元切分，不写死任何语言/框架名。
    """
    import re as _re
    terms: set[str] = set()
    for w in _re.split(r"\W+", str(getattr(subtask, "description", "") or "").lower()):
        if len(w) >= 3:
            terms.add(w)
    sc = getattr(subtask, "scope", None)
    files = (list(getattr(sc, "create_files", None) or [])
             + list(getattr(sc, "writable", None) or []))
    for f in files:
        p = str(f).replace("\\", "/").lower()
        for w in _re.split(r"[^a-z0-9]+", p):
            if len(w) >= 3:
                terms.add(w)
        # 驼峰再切一层：AlarmTaskMapper.java → alarm/task/mapper
        raw_base = str(f).replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        # 驼峰按原大小写切（先小写再切 → AlarmTaskMapper 变成一坨，切不出 task/mapper）
        for w in _re.findall(r"[A-Z]?[a-z]{2,}", raw_base):
            terms.add(w.lower())
        if "." in p.rsplit("/", 1)[-1]:
            terms.add(p.rsplit(".", 1)[-1])   # 扩展名（sql/xml/java/ts…）
    return terms - _NOISE_TERMS


def select_worker_push_pull(subtask, project_stack: dict | None = None):
    """R40-3（round40 定案重塑 G8）：push top-K 栈特化全文 + pull 默认关。

    round39/40 两轮 tool-telemetry 实证：experience__ pull 工具调用恒 0（round40
    36 条遥测零命中，query_knowledge_base 正常用）——离散 pull 把选择负担压给最弱
    环节（小模型）被证伪为死重量，还占 worker 工具槽。改：
    - push 扩到 top-K（cfg.worker_push_k，默认 2），E9-3 门槛逐条保留（栈特化且
      框架词元命中/语言前缀命中；通配泛化建议不 push）；
    - pull 默认关（cfg.worker_pull_enabled=False），开=回退旧混合行为；
    - E9-5 承诺不变：worker_max_tools<=0 = worker 侧经验全关。
    返回 (push_skills, pull_skills) 两列表，互不重叠。fail-open → ([], [])。
    """
    try:
        from swarm.config.settings import get_config

        cfg = get_config().skills
        if not cfg.enabled:
            return [], []
        skills = _merged_skills(cfg.dir_list())
        if not skills:
            return [], []
        if cfg.worker_max_tools <= 0:
            # E9-5（复核 RF5）：0 = worker 侧经验全关（push 也关）——否则"0=不挂经验
            # 工具"的配置承诺静默漂移成"只关 pull"。
            return [], []
        push_k = max(int(getattr(cfg, "worker_push_k", 2)), 0)
        pull_budget = cfg.worker_max_tools if getattr(
            cfg, "worker_pull_enabled", False) else 0
        if push_k <= 0 and pull_budget <= 0:
            return [], []
        intent = str(
            getattr(getattr(subtask, "intent", ""), "value", getattr(subtask, "intent", "")) or ""
        ).lower()
        stack_langs = stack_langs_from_project_stack(project_stack)
        terms = profile_terms_from_project_stack(project_stack)
        picked = select_skills(
            skills, stack_langs=stack_langs, intent=intent, phase="code",
            target="worker", budget_chars=10**9,
            # R53-7：多取候选。push 门槛（_pushable）是在**截断之后**才施加的——若只取
            # push_k+1 条，排名靠前但不可 push 的候选（如 Gradle 工程里的 maven-* 技能）
            # 会把坑占满，随后被门槛筛掉 → 实测 Gradle/Java 脚手架一条经验都推不出来（空集）。
            # 多留余量，让门槛筛完仍有货。
            max_k=push_k + max(pull_budget, 0) + 6,
            rerank_fn=None, profile_terms=terms,
            task_terms=_subtask_terms(subtask),   # R53-7：按"这个子任务在写什么"选经验
        )
        if not picked:
            return [], []
        # E9-3（复核 RF2）：push 门槛逐条适用——栈特化且【与画像框架级相关】
        # （框架词元命中，或 id 语言前缀 ∈ 探出语言集，如 java-coding-standards）。
        # 否则"任意栈特化即 push"会把 django-security 全文塞给 FastAPI 项目。
        from swarm.experience.selector import _task_hit, stack_affinity

        _task_terms = _subtask_terms(subtask)

        def _pushable(doc) -> bool:
            if "*" in doc.applies_to_stacks:
                return False
            # 与排序同尺（stack_affinity）：框架词元命中 或 id 语言前缀 ∈ 栈语言集。
            if stack_affinity(doc, terms, stack_langs):
                return True
            # R53-7：栈已匹配（applies_to_stacks 轴）+ 与本子任务**强相关** → 同样放行。
            # 否则 jpa-patterns 这类"栈特化但 id 不以语言开头、也不含框架词元"的技能永远
            # 进不了 push 面：写 Mapper/Repository 的子任务够不到 JPA 经验（实测）。
            # 泛词已在 _subtask_terms 里剔除（_NOISE_TERMS），剩下的都是有信息量的词，
            # 命中 1 个即足以证明相关（写 Mapper 的子任务只会命中 jpa 的 "mapper" 一词）。
            return _task_hit(doc, _task_terms, stack_langs) >= 1

        pushes = [d for d in picked if _pushable(d)][:push_k]
        _pushed_ids = {d.id for d in pushes}
        pulls = ([d for d in picked if d.id not in _pushed_ids][:pull_budget]
                 if pull_budget > 0 else [])
        return pushes, pulls
    except Exception as e:  # noqa: BLE001 — 经验层绝不拖垮主流程
        logger.warning("[skills] worker push/pull 选择失败，降级为空：%s", e)
        return [], []


def worker_skills_block(subtask, project_stack: dict | None = None) -> str:
    """为 Worker 系统提示生成经验块：push 技能【全文】+ pull 工具目录（G8 混合）。

    与 build_worker_experience_tools 用同一 select_worker_push_pull，保证"目录里的
    工具"与"实际挂上的工具"一一对应。空/禁用/异常 → ""。
    """
    try:
        pushes, pulls = select_worker_push_pull(subtask, project_stack)
        parts = []
        if pushes:
            # 可观测性（round48c 实证缺口）：push 是 R40-3 后 worker 侧唯一经验通道，
            # 却零日志——live 轮无法区分"通道工作中"与"静默失活"（pull 恒 0 的教训
            # 就是靠遥测才定案的）。一行 INFO 让 grep skills-telemetry 两通道齐观测。
            logger.info(
                "[skills-telemetry] worker_push subtask=%s skills=%s",
                str(getattr(subtask, "id", "") or "?"), [d.id for d in pushes])
            parts.append(render_skills_block(list(pushes)))
        catalog = render_experience_tool_catalog(pulls)
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

        _, skills = select_worker_push_pull(subtask, project_stack)  # R40-3：pull 默认关=[]
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
                _push_k = max(int(getattr(cfg, "worker_push_k", 2)), 0)
                _pull_on = bool(getattr(cfg, "worker_pull_enabled", False))
                picked = select_skills(
                    pool, stack_langs={st_tag}, intent=it, phase="code",
                    target="worker", budget_chars=10**9,
                    max_k=_push_k + (max(cfg.worker_max_tools, 0) if _pull_on else 0))
                ids = [x.id for x in picked]
                rank = ids.index(doc.id) if doc.id in ids else -1
                mode = ""
                # R40-3 近似预览：前 push_k 内且栈特化 → push；其余仅在 pull 开时挂
                if 0 <= rank < _push_k and "*" not in doc.applies_to_stacks:
                    mode = "push"
                elif rank >= 0 and _pull_on:
                    mode = "pull"
                surfaces.append({"stack": st_tag, "intent": it, "target": "worker",
                                 "mounted": bool(mode), "rank": rank, "mode": mode})
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


# G10（审计⑤）：绝不进【大脑 planner】注入面的技能标签——架构分层/端口适配/微服务/领域
# 拆分类经验都在教"按层/按域切成多个单元"，与 Task#7「module=单一物理 build 单元」硬不变量
# 正面冲突。planner 只该拿【栈无关、不碰模块划分】的横切经验（api 约定/迁移/错误处理）。
# 结构性 deny（非内容扫描、非靠预算巧合）——任何新增带这些 tag 的技能都自动挡在大脑之外。
_PLANNER_DENY_TAGS = frozenset({
    "architecture", "ddd", "ports-adapters", "hexagonal", "microservices",
    "microservice", "modularization", "layering", "clean-architecture",
    "domain-driven-design", "bounded-context",
})


def planner_skills_block(project_stack: dict | None = None) -> str:
    """为 Planner（plan 节点）生成技能注入块。空/禁用/异常 → ""。

    栈来自 project_stack；阶段固定 'plan'；意图在规划期尚未拆到子任务，用 '*' 表示
    "不按意图轴过滤"（栈×plan 预筛即可，见 handoff §6）。
    _PLANNER_DENY_TAGS：结构性挡掉架构分解类技能（G10），防其诱导大脑拆多物理模块。
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
            exclude_tags=_PLANNER_DENY_TAGS,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[skills] planner 注入失败，降级为空：%s", e)
        return ""
