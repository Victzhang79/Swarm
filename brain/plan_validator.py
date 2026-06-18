"""PlanValidator — 任务计划确定性校验（P0）。"""

from __future__ import annotations

from dataclasses import dataclass, field

from swarm.types import SubTask, TaskPlan

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
        # 按文件【聚合】成一条，而非两两组合 O(n²) 刷屏（N 个子任务写同一文件原会打 N²/2 条）。
        # 只要存在一对【无依赖】争写者即硬失败（并行必冲突）；否则全为依赖序 → 告警。
        has_independent = any(
            not _depends(a, b, subtask_by_id[a], subtask_by_id[b], subtask_by_id)
            for i, a in enumerate(ids)
            for b in ids[i + 1 :]
        )
        joined = ", ".join(ids)
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
