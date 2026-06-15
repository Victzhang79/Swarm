"""共享契约 — Brain 统一定义、注入 Worker、L2 校验。"""

from __future__ import annotations

import json
from typing import Any

from swarm.types import TaskPlan


def enrich_plan_with_shared_contract(plan: TaskPlan) -> TaskPlan:
    """将 plan.shared_contract 合并进各子任务 contract（子任务字段优先）。"""
    shared = plan.shared_contract or {}
    if not shared:
        return plan
    for st in plan.subtasks:
        merged: dict[str, Any] = dict(shared)
        if st.contract:
            merged.update(st.contract)
        st.contract = merged
    return plan


def normalize_plan_scopes(plan: TaskPlan) -> bool:
    """P1-1：scope 归一，消除"同一文件创建/写权限分散到多个子任务"导致的 scope_violation。

    task 0f93f1fc 现场：st-1-1 把 NumberUtilsTest.java 放进 create_files，st-1-2 想改它
    但该文件既不在 st-1-2 的 writable 也不在 create_files → scope_guard 拦截 → empty_diff。

    两条归一规则（原地修改 plan.subtasks）：
    1. 同文件写权唯一：同一文件被多个子任务列为写目标(create_files ∪ writable)时，
       按子任务在列表中的顺序（近似拓扑序：上游在前）保留首个为"写者"，
       后续子任务对该文件的写权降级——从 create_files/writable 移除，并入 readable
       （它们仍可读到上游产物，但不重复创建/抢写，避免 scope 冲突）。
    2. 被依赖产物自动入域：子任务 depends_on 的上游写产物(create_files ∪ writable)，
       若不在本任务任何写权内，自动并入本任务 readable（保证能读到依赖的契约/实现）。

    返回是否发生了任何 scope 改动（供调用方决定是否回写 plan）。
    """
    subtasks = list(getattr(plan, "subtasks", []) or [])
    if not subtasks:
        return False
    changed = False

    # ── 规则 1：同文件写权处理（区分串行协作 vs 独立并发）──
    # 记录每个文件的首个写者（按 subtasks 顺序，近似拓扑序）
    first_writer: dict[str, str] = {}
    for st in subtasks:
        scope = getattr(st, "scope", None)
        if scope is None:
            continue
        write_targets = list(getattr(scope, "create_files", []) or []) + list(getattr(scope, "writable", []) or [])
        for f in write_targets:
            if f not in first_writer:
                first_writer[f] = st.id

    # 依赖可达性：判断 a 是否（直接/间接）依赖 b，用于区分"串行子链协作"与"独立并发"。
    by_id_all = {getattr(s, "id", ""): s for s in subtasks}

    def _depends_transitively(a_id: str, b_id: str) -> bool:
        """a_id 是否经 depends_on 链（传递）依赖 b_id。"""
        seen = set()
        stack = list(getattr(by_id_all.get(a_id), "depends_on", []) or [])
        while stack:
            cur = stack.pop()
            if cur == b_id:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(getattr(by_id_all.get(cur), "depends_on", []) or [])
        return False

    def _on_same_serial_chain(a_id: str, b_id: str) -> bool:
        """两个写者是否在同一串行链上（其一传递依赖另一）→ 串行写同一文件安全。"""
        return _depends_transitively(a_id, b_id) or _depends_transitively(b_id, a_id)

    for st in subtasks:
        scope = getattr(st, "scope", None)
        if scope is None:
            continue
        creates = list(getattr(scope, "create_files", []) or [])
        writables = list(getattr(scope, "writable", []) or [])
        readables = list(getattr(scope, "readable", []) or [])
        new_creates: list[str] = []
        new_writables = list(writables)
        demoted: list[str] = []  # 真正降级为只读的文件（独立并发竞争者）
        chain_modify: list[str] = []  # 串行链协作：create→writable（修改首写者产物）

        for f in creates:
            writer = first_writer.get(f)
            if writer == st.id:
                new_creates.append(f)  # 首写者：保留 create
            elif writer and _on_same_serial_chain(st.id, writer):
                # 串行链上的后续写者：不能重复 create（首写者已新建），转为 writable 修改。
                if f not in new_writables:
                    chain_modify.append(f)
            else:
                # 独立并发的非首写者：降级 readable，杜绝并发抢建同一文件。
                demoted.append(f)

        # writable 同理：非首写者且不在串行链 → 降级；串行链上保留可写。
        kept_writables: list[str] = []
        for f in new_writables:
            writer = first_writer.get(f)
            if writer is None or writer == st.id or _on_same_serial_chain(st.id, writer):
                kept_writables.append(f)
            else:
                demoted.append(f)
        new_writables = kept_writables + chain_modify

        if demoted or chain_modify or new_creates != creates or new_writables != writables:
            for f in demoted:
                if f not in readables:
                    readables.append(f)
            scope.create_files = new_creates
            scope.writable = new_writables
            scope.readable = readables
            changed = True
            # Bug-3 根治：写权被降级（独立并发竞争者）→ 依赖首写者强制串行，杜绝并发
            # 物理冲突。串行链上的协作写者已有依赖关系，无需重复加。
            deps = list(getattr(st, "depends_on", []) or [])
            for f in demoted:
                writer = first_writer.get(f)
                if writer and writer != st.id and writer not in deps:
                    deps.append(writer)
            if deps != list(getattr(st, "depends_on", []) or []):
                st.depends_on = deps

    # ── 规则 2：被依赖产物自动入 readable ──
    by_id = {st.id: st for st in subtasks}
    for st in subtasks:
        scope = getattr(st, "scope", None)
        if scope is None:
            continue
        own_writes = set(getattr(scope, "create_files", []) or []) | set(getattr(scope, "writable", []) or [])
        readables = list(getattr(scope, "readable", []) or [])
        for dep_id in (getattr(st, "depends_on", []) or []):
            dep = by_id.get(dep_id)
            if dep is None:
                continue
            dep_scope = getattr(dep, "scope", None)
            if dep_scope is None:
                continue
            dep_products = list(getattr(dep_scope, "create_files", []) or []) + list(getattr(dep_scope, "writable", []) or [])
            for f in dep_products:
                if f not in own_writes and f not in readables:
                    readables.append(f)
                    changed = True
        scope.readable = readables

    return changed


def format_shared_contract_for_prompt(plan: TaskPlan | None) -> str:
    if not plan or not plan.shared_contract:
        return "（无 Brain 级共享契约）"
    try:
        return json.dumps(plan.shared_contract, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(plan.shared_contract)


def contract_symbols(shared_contract: dict[str, Any] | None) -> list[str]:
    """从共享契约提取需出现在变更中的符号/接口名。"""
    if not shared_contract:
        return []
    symbols: list[str] = []
    for key in ("interfaces", "types", "apis", "fields", "methods"):
        val = shared_contract.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    symbols.append(item)
                elif isinstance(item, dict):
                    symbols.append(str(item.get("name") or item.get("id") or ""))
        elif isinstance(val, dict):
            symbols.extend(str(k) for k in val.keys())
    for item in shared_contract.get("symbols", []) or []:
        if isinstance(item, str):
            symbols.append(item)
    return [s for s in symbols if s]


def enrich_java_package_readable(plan: TaskPlan, project_path: str | None) -> bool:
    """P2-1：把每个 Java 写目标所在 package 目录下的其它 .java 文件纳入同子任务 readable。

    task 0f93f1fc 现场：StringUtils.java 引用同包/相邻类 Constants/StrFormatter/
    CharsetKit，但这些类不在子任务可读 scope → mvn compile 报 "cannot find symbol" →
    同模块编译注定失败，worker 白忙一场。

    一期保守启发式（Q4=A）：仅纳入"同 package 目录"的 .java 文件（不做精确 import
    图解析，避免重 + 解析 bug）。覆盖本案（同目录依赖）。精确 import 解析留二期。

    返回是否发生改动。无 project_path 或非 Java 项目 → no-op 返回 False。
    """
    if not project_path:
        return False
    import os

    changed = False
    for st in getattr(plan, "subtasks", []) or []:
        scope = getattr(st, "scope", None)
        if scope is None:
            continue
        write_targets = (
            list(getattr(scope, "create_files", []) or [])
            + list(getattr(scope, "writable", []) or [])
        )
        java_targets = [f for f in write_targets if f.endswith(".java")]
        if not java_targets:
            continue
        readables = list(getattr(scope, "readable", []) or [])
        own = set(write_targets)
        st_changed = False
        # 收集每个 Java 写目标所在目录的同包 .java 文件
        pkg_dirs = {os.path.dirname(f) for f in java_targets}
        for rel_dir in pkg_dirs:
            abs_dir = os.path.join(project_path, rel_dir)
            if not os.path.isdir(abs_dir):
                continue
            try:
                siblings = os.listdir(abs_dir)
            except OSError:
                continue
            for name in siblings:
                if not name.endswith(".java"):
                    continue
                rel = os.path.join(rel_dir, name) if rel_dir else name
                if rel in own or rel in readables:
                    continue
                readables.append(rel)
                st_changed = True
        if st_changed:
            scope.readable = readables
            changed = True
    return changed
