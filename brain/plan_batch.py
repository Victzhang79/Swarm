"""PLAN 超大需求分批拆解 —— 分组 / 分批 / 组间排序 / 合并工具（DESIGN_plan_batch_decompose.md）。

背景：ultra 需求 tech_design 产出 file_plan 上百文件，PLAN 单次 LLM 调用拆全量 DAG 会卡死
（stream chunk 不超时 + 超长 JSON 生成极慢）。本模块把 file_plan 分组分批，让 PLAN 逐批拆解，
每批规模可控，避免单次超长输出。

全部纯函数 + 确定性，便于单测；LLM 调用在 plan 节点里按本模块的分批结果逐批进行。
"""
from __future__ import annotations

import logging
import math
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

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
    # 通用层/脚手架目录段（不作为业务分组标识）。CODEWALK 根因C：原硬编 ruoyi/ruoyi-system
    # 等项目专名——换成两条【通用规律】，RuoYi 分组行为经由规律不变，其它项目同等受益：
    # ① `<前缀>-<通用后缀>` 形态的聚合模块目录名（xxx-system/xxx-common/... 任意项目通用）
    # ② Java 包根(com/org/net/io/cn)之后的一段是 groupId（公司/项目名，非业务语义）
    _generic = {
        "src", "main", "java", "resources",
        "controller", "service", "mapper", "dao", "domain", "entity", "model",
        "impl", "vo", "dto", "po", "bo", "config", "util", "common", "web",
        "api", "rest", "test", "webapp", "static", "assets", "views", "components",
    }
    _pkg_roots = {"com", "org", "net", "io", "cn"}
    # 仅聚合模块的典型后缀——刻意【不含】service/api/app/biz/web：那些是微服务【业务名】
    # 常用后缀（payment-service），吞进来会把整个服务名当脚手架段（hunter 抓）。
    _module_suffix = re.compile(
        r"^[\w.]+-(system|admin|framework|common|core|ui|starter|parent|bom)$"
    )

    def _is_generic(i: int, p: str) -> bool:
        low = p.lower()
        if low in _generic or low in _pkg_roots:
            return True
        # 聚合模块目录名只出现在路径根级（Maven 多模块布局）；深层同形名多为业务目录
        if i == 0 and _module_suffix.match(low):
            return True
        # 包根后一段=groupId（…/java/com/ruoyi/…）。包根若在路径首段（如业务目录恰叫
        # io/cn），不按 Java 源树处理，避免把其子目录误判成 groupId。
        return i > 1 and parts[i - 1].lower() in _pkg_roots

    # 找业务语义段（连续 1-2 段非通用目录），优先靠后的（更接近功能名）
    biz = [p for i, p in enumerate(parts[:-1]) if not _is_generic(i, p)]
    if biz:
        return "/".join(biz[-2:]) if len(biz) >= 2 else biz[-1]
    # 全是通用目录 → 用顶层模块目录
    return parts[0]


def split_oversized_batches(
    batches: list[tuple[str, list[dict]]], max_files: int,
) -> list[tuple[str, list[dict]]]:
    """R32-2 U1：超大模块批二次切分——单批文件数上限，超限模块按原序切成 mod#i/k 子批。

    取证（round32）：大模块批 LLM 分解确定性超时 >300s（4 轮 16 次，小批几乎全成），
    FINDING-10 降级只兜底不治"批太大"。子批保持文件原序（组内已按依赖/分层排好），
    批间串行门控沿用 merge_subtask_batches 既有机制（后批首子任务依赖前批末尾），
    子批天然串行不破坏依赖。max_files<=0 视为不限制（配置容错，不切）。
    """
    if max_files <= 0:
        return batches
    out: list[tuple[str, list[dict]]] = []
    for name, files in batches:
        if len(files) <= max_files:
            out.append((name, files))
            continue
        total = math.ceil(len(files) / max_files)
        for i in range(total):
            out.append((f"{name}#{i + 1}/{total}",
                        files[i * max_files:(i + 1) * max_files]))
    return out


def batch_signature(name: str, files: list[dict]) -> str:
    """R32-1 U2：模块批内容签名（缓存键）——模块名 + 批内文件 path/action/responsibility。

    签名只吃影响分解产物的输入；feedback/sliding_ctx 刻意不参与（补齐型重试正要跨
    feedback 复用成功批）。file_plan 变更（replan/新 tech_design）→ 签名天然不同不复用。
    """
    import hashlib
    payload = name + "\x00" + "\x00".join(
        f"{f.get('path')}\x01{f.get('action')}\x01{f.get('responsibility')}"
        for f in files
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


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
    # 外部复核补遗：其它生态"每模块一份"的清单/配置/入口
    "pyproject.toml", "setup.py", "requirements.txt", "conftest.py",
    "cmakelists.txt", "main.go",
    "jest.config.js", "jest.config.ts", "webpack.config.js", "rollup.config.js",
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
    if not isinstance(sc, dict):
        # R32 复核附带：LLM 吐 scope 为字符串等畸形时，此处裸 .get 会炸掉整个 merge
        # （连坐全部批，比"复核 B"构造期隔离更早）——按空 scope 处理，让畸形子任务
        # 活到 SubTask 构造期被逐个剔除记账（invalid_subtasks），不连坐。
        return out
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


def prune_parallel_groups(groups, valid_ids) -> list:
    """D10（治本）：从 parallel_groups 剔除不在 valid_ids 的子任务 id，删除成员变空的组。

    任何【重建 subtasks 的确定性路径】（单发 dedupe_subtasks / dedupe_module_scaffolds）删掉子任务后，
    parallel_groups 会残留悬空引用 → plan_validator "parallel_groups[i] 含未知子任务 <id>" 硬失败 →
    叠加 D09 盲重试成死循环。本函数同步收敛 groups，成员全被删的组整组删除（不留空组）。
    不 mutate 入参，返回新列表。groups 为空/None → 返回 []。"""
    if not groups:
        return []
    valid = set(valid_ids or [])
    pruned: list = []
    for g in groups:
        kept = [tid for tid in (g or []) if tid in valid]
        if kept:
            pruned.append(kept)
    return pruned


def _merge_dropped_into_survivor(keep: dict, drop: dict) -> dict:
    """keep-first 去重的守恒面（S2 复核 F1）：被丢弃者的 covers（需求覆盖声明）与
    acceptance_criteria（验收标准，含 contract_utils 机器写入的依赖/登记声明）以
    【并集去重、survivor 在前】并入 survivor。丢了 covers 会让 validate_plan 的覆盖
    矩阵把已实现条目误判"未覆盖"白烧 plan 重试预算；丢了 criteria 会让机器写入的
    构建约定（依赖声明/根 pom 登记）随重复副本蒸发。比照 shared._merge_horizontal_subtasks
    的并集口径（此处是 LLM 原始 dict 形态，非 SubTask 对象）。不 mutate 入参。"""
    merged = dict(keep)
    for key in ("covers", "acceptance_criteria"):
        seen: list = [v for v in (keep.get(key) or []) if v]
        for v in (drop.get(key) or []):
            if v and v not in seen:
                seen.append(v)
        if seen:
            merged[key] = seen
    return merged


def dedupe_subtasks(subtasks: list[dict]) -> list[dict]:
    """跨批重复子任务去重（治本 RUN6：分批分解把地基活每批各拆一遍）。

    实证 RUN6 task f3f85f3d：st-1 与 st-7 都是"创建 ruoyi-alarm 模块脚手架"，后者还依赖
    倒置依赖了填充该模块的 st-6 → 模型对着已完工的活反复拒答 → Brain 循环撞 recursion_limit
    崩。判据：新建交付物签名（见 _fresh_deliverable_signature，零生态特判）非空且相等 → 同一
    桩活。保留依赖更少者（更地基，避免保留依赖倒置的副本）；位次相同保留先出现者。被丢弃者
    id 重映射到保留者，所有 depends_on 改指保留者。与 contract_utils"同文件写权唯一"同源。
    S2 复核 F1：被丢弃者的 covers/acceptance_criteria 并入 survivor（见
    _merge_dropped_into_survivor）——去重只消灭重复的【活】，不消灭覆盖声明/验收约定。
    """
    global_creates = frozenset().union(
        *[_norm_paths(st, "create_files") for st in subtasks]
    ) if subtasks else frozenset()
    keep_by_sig: dict[frozenset[str], dict] = {}
    drop_remap: dict[str, str] = {}  # 被丢弃 id -> 保留 id

    def _replace_in_order(order_list: list[dict], old: dict, new: dict) -> None:
        # 按对象身份替换（dict 相等性可能撞同内容子任务，identity 才唯一）
        idx = next(i for i, o in enumerate(order_list) if o is old)
        order_list[idx] = new

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
        # 两个方向都把被丢弃者的 covers/criteria 并入 survivor（F1 守恒面）。
        if len(st.get("depends_on") or []) < len(prev.get("depends_on") or []):
            drop_remap[prev["id"]] = st["id"]
            survivor = _merge_dropped_into_survivor(st, prev)
            _replace_in_order(order, prev, survivor)
            keep_by_sig[sig] = survivor
        else:
            drop_remap[st["id"]] = prev["id"]
            survivor = _merge_dropped_into_survivor(prev, st)
            _replace_in_order(order, prev, survivor)
            keep_by_sig[sig] = survivor
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


def _base_module(name: str) -> str:
    """批名归一到 base 模块名：容量子批 mod#i/k 与 bisect 半批 mod~a 都归 mod。"""
    return str(name or "").split("~")[0].split("#")[0]


def _item_module_affinity(text: str, base_name: str, files: list) -> int:
    """F8（阶段3.6）：需求条目文本 ↔ 模块批的确定性亲和度。
    模块名整串子串命中=3（中文模块名天然可路由）；模块名分段（-_/ 切）命中各+1；
    批内文件名 stem（≥4 字符）命中各+2。0=与本批无关。"""
    low = str(text or "").lower()
    if not low:
        return 0
    score = 0
    b = str(base_name or "").lower()
    if len(b) >= 2 and b in low:
        score += 3
    import re as _re
    for part in _re.split(r"[-_/]", b):
        if len(part) >= 3 and part in low:
            score += 1
    for f in (files or []):
        p = f.get("path") if isinstance(f, dict) else str(f)
        stem = str(p or "").replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
        if len(stem) >= 4 and stem in low:
            score += 2
    return score


def bucket_requirement_items(
    items: list[dict], module_batches: list[tuple[str, list]],
) -> tuple[dict[str, list[dict]], list[dict]]:
    """F8+A10②（阶段3.6，2026-07-09 登记册）：需求条目按模块批确定性预分桶。

    返回 (by_module, cross)：by_module={base 模块: [亲和条目]}（条目可属多模块）；
    cross=对【所有】批 0 亲和的横切/不可路由条目——注入所有批并明示可任务级认领
    （NFR 无文件归属→无批 covers 的 A10 死角出口）。不变量：任一条目至少出现在
    一个批（cross 进全部批）；module_batches 空=全部按 cross 处理（安全回退=等效
    原全量注入行为）。治 prompt 双线性膨胀：每批只注入本批亲和子集+横切。"""
    valid = [it for it in (items or [])
             if isinstance(it, dict) and str(it.get("id") or "").strip()]
    if not valid:
        return {}, []
    if not module_batches:
        return {}, list(valid)
    bases = [( _base_module(n), f) for n, f in module_batches]
    by_module: dict[str, list[dict]] = {}
    cross: list[dict] = []
    for it in valid:
        text = str(it.get("text") or "")
        # R-F7（阶段3.9 复核 CONFIRMED）：容量/bisect 子批（mod#i/k、mod~a）归一同 base 后，
        # 条目对多个子批亲和会被重复 append → 需求清单块重复行。按 base 去重。
        routed_bases: set[str] = set()
        for base, files in bases:
            if base in routed_bases:
                continue
            if _item_module_affinity(text, base, files) > 0:
                by_module.setdefault(base, []).append(it)
                routed_bases.add(base)
        if not routed_bases:
            cross.append(it)
    return by_module, cross


def merge_subtask_batches(batch_results: list[list[dict]],
                          batch_modules: list[str] | None = None,
                          module_deps: dict[str, list[str]] | None = None) -> list[dict]:
    """合并各批拆出的子任务，重编全局唯一 id（Q：组前缀+序号），保留批内 depends_on。

    batch_results: [[subtask_dict, ...], ...]（每批 LLM 拆出的子任务列表）
    返回扁平 subtasks 列表，id 重写为 st-<全局序号>。
    合并后做跨批去重（dedupe_subtasks）+ 环打断（break_dependency_cycles）以治本分批重复/倒置。

    批间连边（A12，阶段3.3，2026-07-09 登记册）：
    - 传 batch_modules（与 batch_results 对齐的模块名，可含 #i/k、~a 子批后缀）时，
      跨批边【只按真实 module_deps 连】：批首任务 → 各前置模块最后一批的末任务。
      同 base 模块的多个子批保持模块内串行（容量切分/bisect 的既有安全语义）。
      此前无条件"后批首挂前批末"的人造串行链使并行度塌缩≈1，且早批放弃沿链连坐
      全部后续模块、elaborate 假依赖剥离对这条边行为不可预测。
    - 不传（legacy 调用方/测试）→ 原串行门控逐字节不变（零回归）。
    """
    _true_deps = (batch_modules is not None
                  and len(batch_modules) == len(batch_results))
    _deps_by_base: dict[str, list[str]] = {}
    if _true_deps:
        for m, ds in (module_deps or {}).items():
            _deps_by_base[_base_module(m)] = [_base_module(d) for d in (ds or [])]
        # R-F4（阶段3.9 复核 CONFIRMED）：module_deps 键=tech_design modules[].name，批名=
        # file_plan module 字段/路径推断——两套口径从未被校验对齐。依赖表【非空但对批名
        # 零命中】=对齐失败，真依赖模式会产出零跨批边=全并行，而旧串行链在依赖情报劣化时
        # 恰恰最保守 → 回退 legacy 串行门控并留痕。注意：deps 表为空（tech_design 未声明
        # 依赖）不回退——"无真实依赖=并行"是 A12 已拍板语义（3.3 测试锁定），validate
        # 覆盖闸/L1 兜底；本护栏只治"有情报但名字对不上"的静默丢边。
        _batch_bases = {_base_module(m) for m in (batch_modules or [])}
        if (_deps_by_base and len(_batch_bases) > 1
                and not (_batch_bases & set(_deps_by_base.keys()))):
            logger.warning(
                "[PLAN-BATCH] A12 依赖表对批名零命中（deps键=%s vs 批base=%s）——"
                "口径不对齐，回退 legacy 串行门控（最保守），不赌全并行",
                sorted(_deps_by_base.keys())[:8], sorted(_batch_bases)[:8])
            _true_deps = False
    merged: list[dict] = []
    seq = 0
    prev_batch_last_id: str | None = None
    _last_by_module: dict[str, str] = {}  # base 模块 → 该模块最新一批的末任务 id
    for bi, batch in enumerate(batch_results):
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
        # 本批首任务的跨批边
        _gate_ids: list[str] = []
        if _true_deps:
            _mod = _base_module(batch_modules[bi])
            if _mod in _last_by_module:
                _gate_ids.append(_last_by_module[_mod])  # 同模块子批串行
            for _dep in _deps_by_base.get(_mod, []):
                if _dep != _mod and _dep in _last_by_module:
                    _gate_ids.append(_last_by_module[_dep])  # 真实模块依赖
        elif prev_batch_last_id:
            _gate_ids.append(prev_batch_last_id)  # legacy 串行门控
        # 再修正 depends_on（批内旧 id → 新 id），批首追加跨批边
        for idx, st in enumerate(local_ids):
            deps = [id_remap.get(d, d) for d in (st.get("depends_on") or [])]
            if idx == 0:
                for g in _gate_ids:
                    if g not in deps:
                        deps.append(g)
            st["depends_on"] = deps
            merged.append(st)
        if local_ids:
            prev_batch_last_id = local_ids[-1]["id"]
            if _true_deps:
                _last_by_module[_base_module(batch_modules[bi])] = local_ids[-1]["id"]
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
