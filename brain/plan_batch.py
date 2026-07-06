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


def group_into_module_batches(file_plan: list[dict],
                              module_deps: dict[str, list[str]] | None = None,
                              ) -> list[tuple[str, list[dict]]]:
    """按【功能模块】分批（治本 P1/P2/P5）：每个模块 = 一批，批间按模块依赖排序。

    替代旧的 10% 机械文件切片——模块即垂直切片边界，保证一个功能模块的
    Entity+Mapper+Service+Controller 在同一批拆解，避免水平切片 + 跨批依赖丢失。

    module_deps: {模块名: [前置模块名]}（来自 tech_design 阶段1 modules.depends_on）。
                 用于批间排序；缺失则回退文件级 depends_on/分层序。
    返回 [(module_name, [file_plan_item, ...]), ...]，已按依赖序排列。
    """
    groups = group_file_plan(file_plan)
    if not groups:
        return []
    names = list(groups.keys())
    # 优先用 tech_design 的模块依赖排序
    ordered: list[str] | None = None
    if module_deps:
        edges = {n: set(d for d in (module_deps.get(n) or []) if d in groups) for n in names}
        ordered = _toposort(names, edges)
    if ordered is None:
        ordered = _order_groups(groups)  # 回退：文件级 depends_on/分层序
    return [(g, groups[g]) for g in ordered]


def _order_groups(groups: dict[str, list[dict]]) -> list[str]:
    """对组排序：先尝试 depends_on 跨组拓扑序，无有效依赖则用分层序兜底。"""
    names = list(groups.keys())
    # 构建文件路径 → 组 的映射。basename 兜底映射只登记【全计划无歧义】的名字：
    # P1-6 后同名清单文件（moduleA/pom.xml + moduleB/pom.xml）多份共存，last-writer-wins
    # 会把裸 basename 依赖("pom.xml")错连到最后登记的组 → 伪边污染组间拓扑序（hunter 抓）。
    # 歧义 basename 不参与解析（裸名依赖本就无法确定指向，宁缺勿错连）。
    path_to_group: dict[str, str] = {}
    base_group: dict[str, str] = {}
    base_ambiguous: set[str] = set()
    for g, items in groups.items():
        for fp in items:
            p = (fp.get("path") or "").replace("\\", "/").strip("/")
            if not p:
                continue
            path_to_group[p] = g
            b = os.path.basename(p)
            if b in base_group and base_group[b] != g:
                base_ambiguous.add(b)
            else:
                base_group.setdefault(b, g)
    for b, g in base_group.items():
        if b not in base_ambiguous:
            path_to_group.setdefault(b, g)

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


# CODEWALK P1-6：这些文件名是"每模块一份"的生态惯例（构建清单/配置/桶文件）——
# basename 去重会把 moduleB/pom.xml 静默丢掉（与 contract_utils 规则3"每模块 pom
# 各自独立"矛盾 → 多模块脚手架残缺）。白名单内只按完全路径去重；
# 源码文件保持 basename 去重（P5：防 LLM 在两模块重复建同名类）。
_PER_MODULE_FILENAMES = frozenset({
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
    "package.json", "tsconfig.json", "vite.config.ts", "vite.config.js",
    "go.mod", "go.sum", "cargo.toml",
    "application.yml", "application.yaml", "application.properties", "bootstrap.yml",
    "index.ts", "index.js", "__init__.py", "readme.md", ".gitignore", "dockerfile", "makefile",
})


def dedupe_file_plan(file_plan: list[dict]) -> list[dict]:
    """P5：同名文件去重（全局符号表）。

    分批/分模块拆解时，不同模块可能各建一个同名文件（如 INotifyService.java
    被 channel 和 engine 各建一次，路径不同）→ 语义冲突 + 编译重复定义。
    按 basename 去重：保留首个，丢弃后续同名（保留先出现的，通常是更基础的模块）。
    例外（P1-6）：_PER_MODULE_FILENAMES 内的模块惯例文件只按完全路径去重。
    路径完全相同的也去重。返回去重后的 file_plan + 记录被去重项数。
    """
    seen_base: dict[str, str] = {}  # basename(lower) -> 已保留的 path
    seen_path: set[str] = set()
    out: list[dict] = []
    for fp in file_plan:
        if not isinstance(fp, dict) or not fp.get("path"):
            out.append(fp)
            continue
        path = fp["path"].replace("\\", "/").strip("/")
        base = os.path.basename(path).lower()
        if path in seen_path:
            continue  # 完全同路径，跳过
        if base in seen_base and seen_base[base] != path and base not in _PER_MODULE_FILENAMES:
            # 同名不同路径 → 疑似重复创建，跳过后者（保留先出现的）
            continue
        seen_path.add(path)
        seen_base[base] = path
        out.append(fp)
    return out


def _norm_paths(st: dict, *keys: str) -> set[str]:
    """取子任务 scope 中若干键的归一化路径集合。"""
    sc = st.get("scope") or {}
    out: set[str] = set()
    for key in keys:
        for f in (sc.get(key) or []):
            if isinstance(f, str) and f.strip():
                out.add(f.replace("\\", "/").strip("/"))
    return out


def _fresh_deliverable_signature(st: dict, global_creates: frozenset[str]) -> frozenset[str]:
    """子任务"新建交付物"签名 = (create∪writable) ∩ 全计划 create_files 并集。

    判据完全内生于计划、零生态特判：一个文件只要被【任一】子任务 create，它就是"需新建、
    有明确 owner 的交付物"；而共享的【既存】文件（根 pom / settings.gradle / go.mod /
    Cargo.toml / pyproject.toml / *.csproj…）永远只被 modify、绝不在 create_files →
    天然不入 global_creates，自动排除，无需文件名清单也无需查 git。两子任务触碰同一新建
    交付物 = 同一桩活的重复（含 create vs writable 的口径分歧），正是 RUN6 的 st-1/st-7。
    """
    return frozenset((_norm_paths(st, "create_files", "writable")) & global_creates)


def dedupe_subtasks(subtasks: list[dict]) -> list[dict]:
    """跨批重复子任务去重（治本 RUN6：分批分解把地基活每批各拆一遍）。

    实证 RUN6 task f3f85f3d：st-1 与 st-7 都是"创建 ruoyi-alarm 模块脚手架"，后者还依赖
    倒置依赖了填充该模块的 st-6 → 模型对着已完工的活反复拒答 → Brain 循环撞 recursion_limit
    崩。判据：新建交付物签名（见 _fresh_deliverable_signature，零生态特判）非空且相等 → 同一
    桩活。保留依赖更少者（更地基，避免保留依赖倒置的副本）；位次相同保留先出现者。被丢弃者
    id 重映射到保留者，所有 depends_on 改指保留者。与 contract_utils"同文件写权唯一"同源。
    """
    global_creates = frozenset().union(
        *[_norm_paths(st, "create_files") for st in subtasks]
    ) if subtasks else frozenset()
    keep_by_sig: dict[frozenset[str], dict] = {}
    drop_remap: dict[str, str] = {}  # 被丢弃 id -> 保留 id
    order: list[dict] = []
    for st in subtasks:
        sig = _fresh_deliverable_signature(st, global_creates)
        if not sig:
            order.append(st)
            continue
        prev = keep_by_sig.get(sig)
        if prev is None:
            keep_by_sig[sig] = st
            order.append(st)
            continue
        # 同签名重复：保留依赖更少者（更地基）。当前更地基则顶替 prev。
        if len(st.get("depends_on") or []) < len(prev.get("depends_on") or []):
            drop_remap[prev["id"]] = st["id"]
            order[order.index(prev)] = st
            keep_by_sig[sig] = st
        else:
            drop_remap[st["id"]] = prev["id"]
    if not drop_remap:
        return subtasks
    out: list[dict] = []
    for st in order:
        if st["id"] in drop_remap:
            continue
        deps: list[str] = []
        for d in (st.get("depends_on") or []):
            nd = drop_remap.get(d, d)
            if nd != st["id"] and nd not in deps:
                deps.append(nd)
        out.append({**st, "depends_on": deps})
    return out


def break_dependency_cycles(subtasks: list[dict]) -> list[dict]:
    """剔除悬空依赖 + 打断 depends_on 环（DFS 回边）。

    分批串行门控（批首挂前批末尾）叠加 LLM 误标依赖，可能造成环/倒置 → 依赖驱动调度
    死锁或永不就绪。只剔除：①指向不存在子任务的悬空依赖 ②自指 ③构成真实环的回边
    （DFS 灰点回边）。不动合法的前向边，避免误删正确依赖。
    """
    ids = {st["id"] for st in subtasks}
    graph: dict[str, list[str]] = {
        st["id"]: [d for d in (st.get("depends_on") or []) if d in ids and d != st["id"]]
        for st in subtasks
    }
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {i: WHITE for i in graph}
    removed: set[tuple[str, str]] = set()

    def dfs(u: str) -> None:
        color[u] = GRAY
        for v in graph[u]:
            if (u, v) in removed:
                continue
            if color[v] == GRAY:
                removed.add((u, v))  # 回边 → 环，剔除
            elif color[v] == WHITE:
                dfs(v)
        color[u] = BLACK

    for i in graph:
        if color[i] == WHITE:
            dfs(i)
    return [
        {**st, "depends_on": [d for d in graph[st["id"]] if (st["id"], d) not in removed]}
        for st in subtasks
    ]


def merge_subtask_batches(batch_results: list[list[dict]]) -> list[dict]:
    """合并各批拆出的子任务，重编全局唯一 id（Q：组前缀+序号），保留批内 depends_on。

    batch_results: [[subtask_dict, ...], ...]（每批 LLM 拆出的子任务列表）
    返回扁平 subtasks 列表，id 重写为 st-<全局序号>，并建立批间串行依赖（后批依赖前批末尾）。
    合并后做跨批去重（dedupe_subtasks）+ 环打断（break_dependency_cycles）以治本分批重复/倒置。
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
    # 跨批去重（地基活每批各拆一遍）+ 环/悬空依赖打断，治本 RUN6 崩溃。
    merged = dedupe_subtasks(merged)
    merged = break_dependency_cycles(merged)
    return merged


def batch_progress_line(batch_idx: int, total_batches: int, file_count: int,
                        llm_seconds: float | None = None) -> str:
    """进度日志行（Q2：批次/总数/百分比/云端耗时）。"""
    pct = int(round((batch_idx / total_batches) * 100)) if total_batches else 0
    base = f"[PLAN-BATCH] 批 {batch_idx}/{total_batches} ({pct}%) 文件数={file_count}"
    if llm_seconds is not None:
        base += f" LLM耗时={llm_seconds:.1f}s"
    return base
