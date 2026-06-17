"""PLAN 超大需求分批拆解 —— 分组 / 分批 / 组间排序 / 合并工具（DESIGN_plan_batch_decompose.md）。

背景：ultra 需求 tech_design 产出 file_plan 上百文件，PLAN 单次 LLM 调用拆全量 DAG 会卡死
（stream chunk 不超时 + 超长 JSON 生成极慢）。本模块把 file_plan 分组分批，让 PLAN 逐批拆解，
每批规模可控，避免单次超长输出。

全部纯函数 + 确定性，便于单测；LLM 调用在 plan 节点里按本模块的分批结果逐批进行。
"""
from __future__ import annotations

import math
import os
from typing import Any

# Java/前端典型分层顺序（depends_on 缺失时的组间排序兜底，Q3）。
# 数字越小越先执行（被依赖者先做）。
_LAYER_ORDER = [
    ("entity", 0), ("domain", 0), ("model", 0), ("po", 0), ("bo", 0), ("vo", 5),
    ("mapper", 10), ("dao", 10), ("repository", 10),
    ("xml", 15),  # MyBatis mapper xml
    ("service", 20), ("manager", 22),
    ("controller", 30), ("rest", 30), ("api", 30), ("web", 30),
    ("config", 25), ("util", 8), ("constant", 2), ("enums", 2),
    ("dto", 5), ("req", 5), ("resp", 5),
    ("ui", 40), ("vue", 40), ("view", 40), ("component", 40), ("js", 40),
    ("sql", -5),  # 建表脚本最先
]


def group_file_plan(file_plan: list[dict]) -> dict[str, list[dict]]:
    """把 file_plan 分组（Q1：module 字段优先，缺失回退路径前缀）。

    返回 {group_name: [file_plan_item, ...]}，保证【无遗漏、无重复】（所有文件都落到某组）。
    """
    groups: dict[str, list[dict]] = {}
    for fp in file_plan:
        if not isinstance(fp, dict):
            continue
        # 优先 module 字段（tech_design 标注，最准）
        mod = (fp.get("module") or "").strip()
        if not mod:
            mod = _infer_group_from_path(fp.get("path") or "")
        groups.setdefault(mod, []).append(fp)
    return groups


def _infer_group_from_path(path: str) -> str:
    """从文件路径推断分组名（路径前缀回退策略）。

    取业务语义目录段：跳过通用层目录(controller/service/...)，找最像"业务模块"的那段。
    例 'ruoyi-system/src/main/java/com/ruoyi/alarm/task/AlarmTask.java' → 'alarm/task'。
    取不到则用顶层目录或 'misc'。
    """
    if not path:
        return "misc"
    norm = path.replace("\\", "/").strip("/")
    parts = [p for p in norm.split("/") if p and p not in (".", "..")]
    if not parts:
        return "misc"
    # 通用层/脚手架目录段（不作为业务分组标识）
    _generic = {
        "src", "main", "java", "resources", "com", "org", "net", "ruoyi",
        "controller", "service", "mapper", "dao", "domain", "entity", "model",
        "impl", "vo", "dto", "po", "bo", "config", "util", "common", "web",
        "api", "rest", "test", "webapp", "static", "assets", "views", "components",
        "ruoyi-system", "ruoyi-admin", "ruoyi-framework", "ruoyi-common", "ruoyi-ui",
    }
    # 找业务语义段（连续 1-2 段非通用目录），优先靠后的（更接近功能名）
    biz = [p for p in parts[:-1] if p.lower() not in _generic]
    if biz:
        return "/".join(biz[-2:]) if len(biz) >= 2 else biz[-1]
    # 全是通用目录 → 用顶层模块目录
    return parts[0]


def compute_batches(file_plan: list[dict], ratio: float = 0.1, min_batch: int = 1) -> list[list[dict]]:
    """按比例分批（Q2：每批 ceil(N*ratio) 个，约 10 批 + 余数）。

    保持分组内聚：先 group_file_plan，再把组按【组间排序】展平，最后按 batch_size 切片。
    这样同模块文件尽量落同一批，组间顺序遵循依赖。
    """
    if not file_plan:
        return []
    ordered = order_groups_flatten(file_plan)
    n = len(ordered)
    batch_size = max(min_batch, math.ceil(n * ratio))
    return [ordered[i:i + batch_size] for i in range(0, n, batch_size)]


def order_groups_flatten(file_plan: list[dict]) -> list[dict]:
    """组间排序后展平（Q3：depends_on 拓扑序优先，回退分层序）。

    返回扁平的 file_plan 列表，顺序 = 组按依赖/分层排序 → 组内保持原序。
    """
    groups = group_file_plan(file_plan)
    if not groups:
        return list(file_plan)
    ordered_group_names = _order_groups(groups)
    out: list[dict] = []
    for g in ordered_group_names:
        out.extend(groups[g])
    return out


def _order_groups(groups: dict[str, list[dict]]) -> list[str]:
    """对组排序：先尝试 depends_on 跨组拓扑序，无有效依赖则用分层序兜底。"""
    names = list(groups.keys())
    # 构建文件路径 → 组 的映射
    path_to_group: dict[str, str] = {}
    for g, items in groups.items():
        for fp in items:
            p = (fp.get("path") or "").replace("\\", "/").strip("/")
            if p:
                path_to_group[p] = g
                path_to_group[os.path.basename(p)] = g

    # 跨组依赖边：组 X 依赖组 Y（X 的某文件 depends_on Y 的某文件）
    edges: dict[str, set[str]] = {g: set() for g in names}
    has_dep = False
    for g, items in groups.items():
        for fp in items:
            for dep in (fp.get("depends_on") or []):
                dn = (dep or "").replace("\\", "/").strip("/")
                dep_group = path_to_group.get(dn) or path_to_group.get(os.path.basename(dn))
                if dep_group and dep_group != g:
                    edges[g].add(dep_group)  # g 依赖 dep_group → dep_group 先
                    has_dep = True

    if has_dep:
        topo = _toposort(names, edges)
        if topo is not None:
            return topo
    # 回退：按分层序（组名里的层关键词）
    return sorted(names, key=_layer_rank)


def _layer_rank(group_name: str) -> tuple[int, str]:
    gl = group_name.lower()
    for kw, rank in _LAYER_ORDER:
        if kw in gl:
            return (rank, group_name)
    return (18, group_name)  # 未知层放中间（service 附近）


def _toposort(names: list[str], edges: dict[str, set[str]]) -> list[str] | None:
    """Kahn 拓扑排序。edges[x] = x 依赖的集合（依赖者后于被依赖者）。有环返回 None。"""
    # 入度：被依赖次数。先做被依赖者。
    indeg: dict[str, int] = {n: 0 for n in names}
    radj: dict[str, set[str]] = {n: set() for n in names}
    for x, deps in edges.items():
        for y in deps:
            if y in indeg:
                radj[y].add(x)
                indeg[x] += 1
    # 稳定起点：入度0按分层序
    queue = sorted([n for n in names if indeg[n] == 0], key=_layer_rank)
    out: list[str] = []
    while queue:
        cur = queue.pop(0)
        out.append(cur)
        nxt = []
        for m in radj[cur]:
            indeg[m] -= 1
            if indeg[m] == 0:
                nxt.append(m)
        queue.extend(sorted(nxt, key=_layer_rank))
    if len(out) != len(names):
        return None  # 有环
    return out


def merge_subtask_batches(batch_results: list[list[dict]]) -> list[dict]:
    """合并各批拆出的子任务，重编全局唯一 id（Q：组前缀+序号），保留批内 depends_on。

    batch_results: [[subtask_dict, ...], ...]（每批 LLM 拆出的子任务列表）
    返回扁平 subtasks 列表，id 重写为 st-<全局序号>，并建立批间串行依赖（后批依赖前批末尾）。
    """
    merged: list[dict] = []
    seq = 0
    prev_batch_last_id: str | None = None
    for bi, batch in enumerate(batch_results):
        first_in_batch: str | None = None
        id_remap: dict[str, str] = {}
        # 先分配新 id
        local_ids = []
        for st in batch:
            if not isinstance(st, dict):
                continue
            seq += 1
            new_id = f"st-{seq}"
            old_id = st.get("id")
            if old_id:
                id_remap[old_id] = new_id
            st = {**st, "id": new_id}
            local_ids.append(st)
        # 再修正 depends_on（批内旧 id → 新 id），并把批首挂到上一批末尾（串行门控）
        for idx, st in enumerate(local_ids):
            deps = [id_remap.get(d, d) for d in (st.get("depends_on") or [])]
            if idx == 0 and prev_batch_last_id:
                if prev_batch_last_id not in deps:
                    deps.append(prev_batch_last_id)
            st["depends_on"] = deps
            if first_in_batch is None:
                first_in_batch = st["id"]
            merged.append(st)
        if local_ids:
            prev_batch_last_id = local_ids[-1]["id"]
    return merged


def batch_progress_line(batch_idx: int, total_batches: int, file_count: int,
                        llm_seconds: float | None = None) -> str:
    """进度日志行（Q2：批次/总数/百分比/云端耗时）。"""
    pct = int(round((batch_idx / total_batches) * 100)) if total_batches else 0
    base = f"[PLAN-BATCH] 批 {batch_idx}/{total_batches} ({pct}%) 文件数={file_count}"
    if llm_seconds is not None:
        base += f" LLM耗时={llm_seconds:.1f}s"
    return base
