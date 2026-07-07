"""PlanValidator — 任务计划确定性校验（P0）。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from swarm.types import SubTask, TaskPlan

# 喂给 VALIDATE_PLAN 软建议 LLM 的 plan_json 字符上限。超过则跳过 LLM 软建议（结构确定性闸门
# 已放行），绝不把超大 prompt 喂推理模型。~120K 字符 ≈ 30K token，足够表达结构/依赖/scope。
MAX_LLM_VALIDATION_PLAN_CHARS = 120_000

# 软校验瘦身时剥离的重体积/冗余子任务字段（不参与 DAG/scope/依赖/完整性判断）。
# contract：每子任务约 42K 字符且各子任务重复携带（round16 实测 24× → plan_json ~1MB）；
# context_snippets：worker 免探索的注入代码，纯执行辅助，与计划结构无关。
_SLIM_STRIP_SUBTASK_FIELDS = ("contract", "context_snippets")


def slim_plan_json_for_llm_validation(plan: TaskPlan) -> str:
    """构造喂给 VALIDATE_PLAN 软建议 LLM 的【瘦身 plan_json】。

    背景（round16 实测）：`plan_obj.model_dump_json()` 把每个子任务约 42K 字符的 `contract`
    副本（24 子任务重复 24×）+ 注入代码全序列化 → plan_json 达 ~1MB（~260K token），喂给
    推理模型 GLM-5.2 触发 84K+ chunk / 25min reasoning runaway（撞 1500s wall-clock 上限才
    放行，且结果是软建议、被丢弃）→ 卡在到 DISPATCH 之前。

    结构校验（validate_plan_structure）已确定性硬保证 DAG/scope/依赖可执行性；LLM 软校验只做
    主观质量信号，无需每个子任务内联的 contract 副本——契约完整性由 plan 级 shared_contract
    一次性体现。这里剥离每子任务的 contract/context_snippets（体积大且冗余），其余字段
    （id/description/scope/depends_on/难度/验收标准/shared_contract）原样保留。
    """
    data = plan.model_dump()
    for st in data.get("subtasks", []) or []:
        if isinstance(st, dict):
            for f in _SLIM_STRIP_SUBTASK_FIELDS:
                st.pop(f, None)
    return json.dumps(data, ensure_ascii=False, indent=2)


def slim_plan_json_or_empty(plan_obj) -> str:
    """None 安全的瘦身 plan 序列化（D50：handle_failure / learn_success / learn_failure 提示词）。

    这三处原用 `plan_obj.model_dump_json(indent=2)` 全量注入 LLM prompt——含每子任务 ~42K
    的 contract 内联副本（validate_plan 早已用 slim 瘦身，此三处漏改，handle_failure 还是
    失败循环高频节点）。统一走 slim；瘦身路径本身异常时 fail-closed 回退旧全量序列化
    （宁可 prompt 大也不丢失败分析输入），再不行才 "{}" 。
    """
    if plan_obj is None or not hasattr(plan_obj, "model_dump"):
        return "{}"
    try:
        return slim_plan_json_for_llm_validation(plan_obj)
    except Exception:
        try:
            return plan_obj.model_dump_json(indent=2)
        except Exception:
            return "{}"

# 单子任务可写文件数：软上限 = 一个垂直功能合理跨越的分层文件数（domain/controller/service/
# impl/mapper+xml 等，RuoYi 这类分层框架一个功能天然 4-6 个文件）。软上限内不告警；
# 软~硬之间仅 warning（不阻断，尊重垂直切片：一个完整功能即使跨多文件也归一个子任务）；
# 超硬上限才判 fail（真正失控的 scope 过度圈定，如把整个模块塞进 writable）。
# task 34fab09e：旧 MAX=3 硬上限把"导出 Excel"功能（4 个分层文件）强制 replan 砍碎丢文件。
SOFT_WRITABLE_FILES_PER_SUBTASK = 6
MAX_WRITABLE_FILES_PER_SUBTASK = 12


@dataclass
class PlanValidationResult:
    valid: bool
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, message: str) -> None:
        self.issues.append(message)
        self.valid = False

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def validate_plan_structure(
    plan: TaskPlan,
    *,
    affected_files: list[str] | None = None,
) -> PlanValidationResult:
    """确定性计划校验：DAG、并行写冲突、文件粒度、组完整性。"""
    result = PlanValidationResult(valid=True)
    if not plan.subtasks:
        result.add("计划无子任务")
        return result

    # H1 治本(round21 假绿门)：整 plan 必须至少有一个子任务能【产出改动】(writable ∪ create_files
    # 非空)。tech_design/plan 返回【空但合法】JSON(file_plan=[])时，所有子任务写 scope 皆空 → 只能
    # 产空 diff → 在"DONE 零放弃"判据下沿 tech_design→plan→validate→confirm→dispatch 直穿判成功
    # 交付(空交付假 DONE)。此处确定性 fail-closed 掐断该跨节点假绿链，根本不放空计划下去。
    _writers = [
        t for t in plan.subtasks
        if (getattr(getattr(t, "scope", None), "writable", None)
            or getattr(getattr(t, "scope", None), "create_files", None))
    ]
    if not _writers:
        result.add(
            "计划无任何可产出改动的子任务(所有子任务 writable+create_files 皆空)——"
            "空计划只能产空 diff、会被误判成功交付(空 diff 假 DONE)，拒绝放行"
        )
        return result

    task_ids = {t.id for t in plan.subtasks}
    subtask_by_id = {t.id: t for t in plan.subtasks}

    # 依赖 ID 存在
    for t in plan.subtasks:
        for dep in t.depends_on:
            if dep not in task_ids:
                result.add(f"子任务 {t.id} 依赖未知任务 {dep}")

    # DAG 无环
    if _has_cycle(plan):
        result.add("执行计划存在循环依赖")

    # parallel_groups 完整性
    grouped: set[str] = set()
    for gi, group in enumerate(plan.parallel_groups):
        for tid in group:
            if tid not in task_ids:
                result.add(f"parallel_groups[{gi}] 含未知子任务 {tid}")
            if tid in grouped:
                result.add(f"子任务 {tid} 出现在多个 parallel_groups")
            grouped.add(tid)

    missing_in_groups = task_ids - grouped
    if plan.parallel_groups and missing_in_groups:
        result.add(f"以下子任务未出现在 parallel_groups: {sorted(missing_in_groups)}")

    # 单任务文件数：软上限内放行；软~硬之间仅告警（尊重垂直切片，一个完整功能可跨多文件）；
    # 超硬上限才判失败（scope 失控，如整个模块塞进 writable）。
    for t in plan.subtasks:
        n = len(t.scope.writable or [])
        if n > MAX_WRITABLE_FILES_PER_SUBTASK:
            result.add(
                f"子任务 {t.id} 涉及 {n} 个可写文件，超过硬上限 {MAX_WRITABLE_FILES_PER_SUBTASK}"
                f"（scope 可能失控，需拆分或收窄）"
            )
        elif n > SOFT_WRITABLE_FILES_PER_SUBTASK:
            result.warn(
                f"子任务 {t.id} 涉及 {n} 个可写文件（超软上限 {SOFT_WRITABLE_FILES_PER_SUBTASK}）。"
                f"若为单一垂直功能跨分层文件属正常；若含多个独立功能建议拆分"
            )

    # 并行组内 writable 不得冲突（同组即并行）
    for gi, group in enumerate(plan.parallel_groups):
        seen: dict[str, str] = {}
        for tid in group:
            t = subtask_by_id.get(tid)
            if not t:
                continue
            for fp in t.scope.writable or []:
                if fp in seen and seen[fp] != tid:
                    result.add(
                        f"parallel_groups[{gi}] 中 {seen[fp]} 与 {tid} 同时写 {fp}（并行冲突）"
                    )
                else:
                    seen[fp] = tid

    # 跨子任务写冲突。writable + create_files 都算"写"（B3：每个文件只应属一个子任务）。
    # - 无依赖关系还写同文件 → 硬失败（并行必冲突）。
    # - 有依赖关系写同文件 → 告警（B3 依赖序拆分要求文件不重叠；即使串行，两子任务各自
    #   在独立沙箱改同文件，MERGE 时仍会冲突。降级 warn 不阻断，尊重少数合理场景如
    #   前序 create + 后序 modify，但提示风险）。
    writable_map: dict[str, list[str]] = {}
    for t in plan.subtasks:
        # 同一子任务内 writable ∪ create_files 去重：否则文件【既在 writable 又在 create_files】
        # 时同一子任务 id 被记两次 → 下方两两比较会出现 "st-N 与 st-N 同时写"（自己跟自己冲突）。
        for fp in (set(t.scope.writable or []) | set(getattr(t.scope, "create_files", []) or [])):
            writable_map.setdefault(fp, []).append(t.id)
    for fp, ids in writable_map.items():
        ids = list(dict.fromkeys(ids))  # 跨子任务再去重（保序）
        if len(ids) < 2:
            continue
        joined = ", ".join(ids)
        # D1 治本 backstop：Maven 根聚合 pom.xml 永远【单写者】。两份对 <modules>/
        # <dependencyManagement> 的结构重写无法安全 3-way/union 合并（round18 P0-A 畸形 /
        # rebase 循环→escalate→FAILED），即便依赖序也不行（各自整段重写、非加性）。
        # normalize_plan_scopes 已收敛唯一 owner；此处硬失败仅在收敛失效时兜底（fail-closed）。
        if fp.replace("\\", "/") == "pom.xml":
            result.add(
                f"根聚合 pom.xml 有 {len(ids)} 个写者: [{joined}]"
                f"（必须收敛唯一 aggregator-owner；双写者=P0-A 畸形/rebase 循环根因）"
            )
            continue
        # 按文件【聚合】成一条，而非两两组合 O(n²) 刷屏（N 个子任务写同一文件原会打 N²/2 条）。
        # 只要存在一对【无依赖】争写者即硬失败（并行必冲突）；否则全为依赖序 → 告警。
        has_independent = any(
            not _depends(a, b, subtask_by_id[a], subtask_by_id[b], subtask_by_id)
            for i, a in enumerate(ids)
            for b in ids[i + 1 :]
        )
        if has_independent:
            result.add(
                f"{len(ids)} 个无依赖子任务同时写 {fp}: [{joined}]"
                f"（并行必冲突，每个文件应只归一个子任务）"
            )
        else:
            result.warn(
                f"{len(ids)} 个依赖序子任务都写 {fp}: [{joined}]"
                f"（已串行化；聚合/注册类共享文件由 bootstrap 传播 + MERGE 3-way/rebase 收口，"
                f"非聚合文件建议仍只归一个子任务）"
            )

    # 检索定位文件覆盖（可选）
    if affected_files:
        scoped: set[str] = set()
        for t in plan.subtasks:
            scoped.update(t.scope.writable or [])
            scoped.update(t.scope.readable or [])
        missing = [f for f in affected_files if f and f not in scoped]
        if missing and len(missing) <= 10:
            result.warn(
                f"检索定位的文件可能未被任何子任务 scope 覆盖: {missing[:5]}"
                + (" ..." if len(missing) > 5 else "")
            )

    # 共享契约（跨子任务）
    if len(plan.subtasks) > 1 and not plan.shared_contract:
        result.warn("多子任务计划缺少 plan.shared_contract，跨文件协调风险较高")

    if plan.shared_contract:
        sc_keys = set(plan.shared_contract.keys())
        for t in plan.subtasks:
            if t.contract:
                overlap = sc_keys & set(t.contract.keys())
                if not overlap and len(plan.subtasks) > 1:
                    result.warn(f"子任务 {t.id} contract 未引用 shared_contract 字段")

    return result


def _depends(
    id_a: str,
    id_b: str,
    ta: SubTask,
    tb: SubTask,
    subtask_by_id: dict[str, SubTask],
) -> bool:
    deps_a = _all_deps(ta, subtask_by_id)
    deps_b = _all_deps(tb, subtask_by_id)
    return id_b in deps_a or id_a in deps_b


def _all_deps(task: SubTask, subtask_by_id: dict[str, SubTask]) -> set[str]:
    deps: set[str] = set()
    stack = list(task.depends_on)
    while stack:
        dep_id = stack.pop()
        if dep_id in deps:
            continue
        deps.add(dep_id)
        dep_task = subtask_by_id.get(dep_id)
        if dep_task:
            stack.extend(dep_task.depends_on)
    return deps


def _has_cycle(plan: TaskPlan) -> bool:
    subtask_by_id = {t.id: t for t in plan.subtasks}
    visited: set[str] = set()
    stack: set[str] = set()

    def dfs(tid: str) -> bool:
        if tid in stack:
            return True
        if tid in visited:
            return False
        visited.add(tid)
        stack.add(tid)
        t = subtask_by_id.get(tid)
        if t:
            for dep in t.depends_on:
                if dfs(dep):
                    return True
        stack.remove(tid)
        return False

    return any(dfs(t.id) for t in plan.subtasks)
