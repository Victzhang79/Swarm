"""共享契约 — Brain 统一定义、注入 Worker、L2 校验。"""

from __future__ import annotations

import functools as _functools
import json
import re
from typing import Any

from swarm.types import TaskPlan

# Maven `-pl <module>` 提取（reactor 模块选择）。
_MVN_PL_RE = re.compile(r"-pl\s+([^\s,]+)")


def _exists_in_repo(project_path: str | None, rel: str, cache: dict[str, bool]) -> bool:
    """文件是否已存在于项目 repo 基线（用于区分"聚合修改"vs"新建撞车"）。

    争抢分流的事实依据：已存在文件被多个独立子任务写 = 聚合/注册类共享文件
    （父 pom/settings.gradle/路由 index/DI 注册表…），必须保留各自写权（串行）不可
    静默降级丢贡献；不存在 = 真·新建撞车，独占首写者即可。

    git repo → 以 HEAD 为权威基线（`git cat-file -e HEAD:<rel>`，整洁排除未跟踪残留）；
    非 git → 退化 os.path.isfile。project_path 为空 → 一律 False（退化为今日 demote 行为，
    向后兼容）。结果按 rel 缓存，避免对同一文件重复 fork git。
    """
    if not project_path or not rel:
        return False
    if rel in cache:
        return cache[rel]
    import os
    import subprocess

    result = False
    try:
        if os.path.isdir(os.path.join(project_path, ".git")):
            r = subprocess.run(
                ["git", "-C", project_path, "cat-file", "-e", f"HEAD:{rel}"],
                capture_output=True,
                timeout=10,
            )
            result = r.returncode == 0
        else:
            result = os.path.isfile(os.path.join(project_path, rel))
    except (OSError, subprocess.SubprocessError):
        result = False
    cache[rel] = result
    return result


def _ensure_maven_module_build_scope(subtasks: list) -> bool:
    """规则3：Maven 新模块构建闸门【可满足性】补全（现场 task 69d34b1b）。

    现场：子任务新建 `ruoyi-alarm-app/src/...` 下 7 个文件，验收 `mvn -pl ruoyi-alarm-app -am compile`，
    但模块自己的 `pom.xml` 与父 `pom.xml` 的 `<module>` 注册都不在任何 scope →
    `Could not find the selected project in the reactor` 必败、worker 够不着、空转到超时升级。

    规则（仅保留无害安全网，2026-06-18 回滚）：凡子任务 build/test/verify/acceptance 命令含
    `-pl <module>` 且该 `<module>/` 目录下在本计划里有 create_files（=正在新建该模块），就把
    `<module>/pom.xml` 并入该子任务 create_files（各模块自己的 POM，不同文件，无争用）。

    **不再碰根 `pom.xml`**：父 `<modules>` 注册是【N 个新模块往同一文件追加各自一行】的天然
    共享写——单归属会漏注册其余模块（其 `mvn -pl X` 仍 reactor not found）、喷洒又造成 N 路争写。
    这俩都错。父 pom 注册交给 LLM 计划的脚手架子任务 + bootstrap 传播根因修复处理，本规则不插手。
    """
    changed = False
    all_creates: list[str] = []
    all_write_targets: set[str] = set()
    for st in subtasks:
        scope = getattr(st, "scope", None)
        if scope is None:
            continue
        all_creates += list(getattr(scope, "create_files", []) or [])
        all_write_targets |= set(getattr(scope, "create_files", []) or []) | set(
            getattr(scope, "writable", []) or []
        )

    for st in subtasks:
        scope = getattr(st, "scope", None)
        harness = getattr(st, "harness", None)
        if scope is None:
            continue
        cmds: list[str] = []
        if harness is not None:
            for attr in ("build_command", "test_command"):
                v = getattr(harness, attr, "") or ""
                if v:
                    cmds.append(v)
            cmds += [c for c in (getattr(harness, "verify_commands", []) or []) if c]
        cmds += [c for c in (getattr(st, "acceptance_criteria", []) or []) if c]

        modules: set[str] = set()
        for c in cmds:
            for m in _MVN_PL_RE.findall(c):
                m = m.lstrip(":").strip()
                # 只处理目录式模块名（`:artifactId` 无法可靠映射目录，跳过）+ 该模块确在新建。
                if m and "/" not in m and any(
                    cf.startswith(m.rstrip("/") + "/") for cf in all_creates
                ):
                    modules.add(m)

        if not modules:
            continue
        creates = list(getattr(scope, "create_files", []) or [])
        for mod in modules:
            mod_pom = f"{mod}/pom.xml"
            if mod_pom not in all_write_targets:
                creates.append(mod_pom)
                all_write_targets.add(mod_pom)
                changed = True
        scope.create_files = creates

    return changed


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


def normalize_plan_scopes(plan: TaskPlan, project_path: str | None = None) -> bool:
    """P1-1：scope 归一，消除"同一文件创建/写权限分散到多个子任务"导致的 scope_violation。

    task 0f93f1fc 现场：st-1-1 把 NumberUtilsTest.java 放进 create_files，st-1-2 想改它
    但该文件既不在 st-1-2 的 writable 也不在 create_files → scope_guard 拦截 → empty_diff。

    归一规则（原地修改 plan.subtasks）：
    1. 同文件写权处理：同一文件被多个子任务列为写目标(create_files ∪ writable)时，按子任务
       顺序（近似拓扑序：上游在前）取首写者。其余写者分流（治本"文件被争抢"这一类，2026-06-18）：
       - 串行链协作（其一传递依赖另一）：create→writable 改首写者产物，保留写权。
       - 独立并发 + 文件【已存在于 repo】（聚合/注册类共享文件，如父 pom/settings.gradle/
         路由 index/DI 注册表）：【保留写权】并按写者序【串行化】（依赖前序写者，防环守卫）。
         绝不降级 readable——降级会静默丢失各写者的登记。MERGE 3-way+rebase + bootstrap
         传播负责收口。需 project_path 判存在；缺省退化为下一条 demote（向后兼容）。
       - 独立并发 + 文件【不存在】（真·新建撞车）：首写者建，其余降级 readable + 依赖首写者。
    2. 被依赖产物自动入域：子任务 depends_on 的上游写产物，自动并入本任务 readable。
    （规则3=Maven 模块自身 pom 补全；规则4=Maven 父 pom 单 owner 注册 backstop，见下。）

    project_path：项目仓库路径（用于判断文件是否已存在 → 区分聚合修改 vs 新建撞车）。
    返回是否发生了任何 scope 改动（供调用方决定是否回写 plan）。
    """
    subtasks = list(getattr(plan, "subtasks", []) or [])
    if not subtasks:
        return False
    changed = False

    # ── 规则 3（先于规则1跑）：Maven 新模块构建闸门可满足性补全（治本 task 69d34b1b）。
    # 放规则1前，使补进来的 pom 也受"同文件写权唯一"去重/串行化（多模块子任务不并发抢写根 pom）。
    changed = _ensure_maven_module_build_scope(subtasks) or changed

    # ── 规则 1：同文件写权处理（区分串行协作 vs 独立并发 vs 聚合修改）──
    # 每个文件的【有序写者列表】（按 subtasks 顺序，近似拓扑序：上游在前）。
    writers_by_file: dict[str, list[str]] = {}
    for st in subtasks:
        scope = getattr(st, "scope", None)
        if scope is None:
            continue
        _wt = list(getattr(scope, "create_files", []) or [])
        _wt += list(getattr(scope, "writable", []) or [])
        for f in _wt:
            ids = writers_by_file.setdefault(f, [])
            if st.id not in ids:
                ids.append(st.id)
    first_writer: dict[str, str] = {f: ids[0] for f, ids in writers_by_file.items()}

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

    # 争抢分流分类（仅对 ≥2 写者的文件）：文件【已存在于 repo】= 聚合/注册类共享文件
    # （父 pom/settings.gradle/路由 index/DI 注册表…），独立写者保留写权 + 串行化（防丢贡献）；
    # 不存在 = 真·新建撞车，独占首写者，其余降级。project_path 缺省 → 无聚合文件（退化今日行为）。
    _exist_cache: dict[str, bool] = {}
    aggregate_files: set[str] = {
        f for f, ids in writers_by_file.items()
        if len(ids) >= 2 and _exists_in_repo(project_path, f, _exist_cache)
    }

    def _prev_safe_writer(f: str, me: str) -> str | None:
        """聚合文件串行化：返回写者序里 me 之前、不会与 me 成环的最近前序写者；无则 None。"""
        ids = writers_by_file.get(f, [])
        if me not in ids:
            return None
        for j in range(ids.index(me) - 1, -1, -1):
            cand = ids[j]
            # cand 不能（传递）依赖 me，否则加 me→cand 依赖会成环。
            if not _depends_transitively(cand, me):
                return cand
        return None

    serialized_ids: set[str] = set()  # 因聚合文件被串行化（保留写权）的子任务

    for st in subtasks:
        scope = getattr(st, "scope", None)
        if scope is None:
            continue
        creates = list(getattr(scope, "create_files", []) or [])
        writables = list(getattr(scope, "writable", []) or [])
        readables = list(getattr(scope, "readable", []) or [])
        new_creates: list[str] = []
        new_writables: list[str] = []
        demoted: list[str] = []  # 真正降级为只读的文件（独立并发新建撞车）
        serialize_after: dict[str, str] = {}  # 聚合文件 → 需串行依赖的前序写者

        # 合并写目标按 (文件, 是否新建) 处理：create 优先，writable 去重（同文件双列只算一次）。
        targets: list[tuple[str, bool]] = [(f, True) for f in creates]
        _seen_t = set(creates)
        for f in writables:
            if f not in _seen_t:
                targets.append((f, False))
                _seen_t.add(f)

        for f, from_create in targets:
            writer = first_writer.get(f)
            if writer == st.id:
                # 首写者：聚合文件且已存在 → 实为 modify，落 writable；否则保留原操作类型。
                if f in aggregate_files:
                    if f not in new_writables:
                        new_writables.append(f)
                elif from_create:
                    new_creates.append(f)
                else:
                    new_writables.append(f)
            elif writer is None or _on_same_serial_chain(st.id, writer):
                # 串行链协作（或无主）：保留写权（create→writable 改首写者产物）。
                if f not in new_writables:
                    new_writables.append(f)
            elif f in aggregate_files:
                # 独立并发 + 聚合文件：保留写权（转 writable 修改）+ 串行到前序写者，绝不降级。
                prev = _prev_safe_writer(f, st.id)
                if prev:
                    if f not in new_writables:
                        new_writables.append(f)
                    serialize_after[f] = prev
                    serialized_ids.add(st.id)
                else:
                    demoted.append(f)  # 无安全前序（防环兜底）→ 退化降级
            else:
                # 独立并发 + 新建撞车：降级 readable，杜绝并发抢建同一文件。
                demoted.append(f)

        # serialize_after 也要进：聚合文件保留写权时 scope 内容不变，但仍需补串行依赖。
        if (new_creates != creates or new_writables != writables or demoted or serialize_after):
            for f in demoted:
                if f not in readables and f not in new_writables:
                    readables.append(f)
            scope.create_files = new_creates
            scope.writable = new_writables
            scope.readable = readables
            changed = True
            deps = list(getattr(st, "depends_on", []) or [])
            # 降级者（新建撞车）依赖首写者强制串行，杜绝并发物理冲突。
            for f in demoted:
                writer = first_writer.get(f)
                if writer and writer != st.id and writer not in deps:
                    deps.append(writer)
            # 聚合文件保留写权者：依赖前序写者，串行追加（bootstrap 传播 + MERGE 3-way/rebase 收口）。
            for prev in serialize_after.values():
                if prev and prev != st.id and prev not in deps:
                    deps.append(prev)
            if deps != list(getattr(st, "depends_on", []) or []):
                st.depends_on = deps

    # 聚合文件被串行化保留写权后，相关子任务不能再与前序写者同处一个 parallel_group
    # （否则 validator 的 parallel-group 同写检查会硬 fail）。parallel_groups 已 vestigial
    # （dispatch 走 depends_on，见 planning_nodes._rebuild_plan "依赖驱动调度不需要它"），
    # 直接清空交由依赖驱动调度，与既有约定一致。
    if serialized_ids and getattr(plan, "parallel_groups", None):
        plan.parallel_groups = []
        changed = True

    # ── 规则 4：Maven 父 pom 单 owner 注册 backstop（治本"文件被争抢"之 Maven 专项）──
    # 规则3 只补各模块【自己的】pom；父 `<modules>` 注册是 N 个新模块往同一文件追加。planner
    # 通常已给一个脚手架子任务 own 根 pom（line76 不可重叠硬约束）；多 owner 情形交规则1 串行网。
    # 唯一缺口：有新模块但【无人】own 根 pom → 模块永不被注册、`mvn -pl X` reactor not found。
    # 本规则仅补这个缺口：指派单一 owner 登记全部新模块（additive，不动既有 owner）。
    new_modules: set[str] = set()
    pom_owned = False
    for st in subtasks:
        scope = getattr(st, "scope", None)
        if scope is None:
            continue
        creates = list(getattr(scope, "create_files", []) or [])
        writables = list(getattr(scope, "writable", []) or [])
        if "pom.xml" in creates or "pom.xml" in writables:
            pom_owned = True
        for cf in creates:
            if cf.endswith("/pom.xml") and cf.count("/") == 1:
                new_modules.add(cf.split("/", 1)[0])
    # 仅当：有新模块 + 根 pom 已存在于 repo（真·注册进父 pom 场景）+ 当前无人 own 根 pom。
    if new_modules and not pom_owned and _exists_in_repo(project_path, "pom.xml", _exist_cache):
        owner = next(
            (
                st for st in subtasks
                if any(
                    cf.endswith("/pom.xml") and cf.count("/") == 1
                    for cf in (getattr(getattr(st, "scope", None), "create_files", []) or [])
                )
            ),
            None,
        )
        if owner is not None and getattr(owner, "scope", None) is not None:
            w = list(getattr(owner.scope, "writable", []) or [])
            if "pom.xml" not in w:
                w.append("pom.xml")
                owner.scope.writable = w
                changed = True
            ac = list(getattr(owner, "acceptance_criteria", []) or [])
            note = f"在根 pom.xml 的 <modules> 中登记全部新模块: {sorted(new_modules)}"
            if note not in ac:
                ac.append(note)
                owner.acceptance_criteria = ac
                changed = True
            # 其余构建新模块的子任务依赖 owner（确保注册已就位再 mvn -pl），带防环守卫。
            for st in subtasks:
                if st.id == owner.id:
                    continue
                scope = getattr(st, "scope", None)
                if scope is None:
                    continue
                builds_new_module = any(
                    "/" in cf and cf.split("/", 1)[0] in new_modules
                    for cf in (
                        list(getattr(scope, "create_files", []) or [])
                        + list(getattr(scope, "writable", []) or [])
                    )
                )
                if builds_new_module and not _depends_transitively(owner.id, st.id):
                    deps = list(getattr(st, "depends_on", []) or [])
                    if owner.id not in deps:
                        deps.append(owner.id)
                        st.depends_on = deps
                        changed = True

    # ── 规则 1.5：共享文件写者【串行流水化】(治本 RUN9 类——同类反复出现的根 class) ──
    # 前述规则1只保证每个写者与【首写者】同链，漏了"多个写者各自挂首写者链、彼此却并行"：
    # 实证 RUN9(task 225b1c7e)：5 个子任务都写根 pom.xml，各自传递依赖到 scaffold 故被判"同链"
    # 保留写权，但彼此无依赖序 → plan_validator 判"N 个无依赖子任务同时写"硬失败 → auto_accept
    # fail-fast。注册/聚合类共享文件(根 pom/settings.gradle/DI 注册表…)多写者本是合法模式，
    # 正解是把全部写者按拓扑序串成【单一总序链】(writer[i] 依赖 writer[i-1])，确保任意两写者
    # 必有依赖序、零并行 → 各写者顺序追加注册、MERGE 3-way/bootstrap 传播收口。带防环守卫。
    # 无需 project_path，故 VALIDATE 路径(line 719 无 project_path)也生效。
    _writers_final: dict[str, list[str]] = {}
    _pos = {st.id: i for i, st in enumerate(subtasks)}
    for st in subtasks:
        sc = getattr(st, "scope", None)
        if sc is None:
            continue
        for f in (set(getattr(sc, "create_files", []) or []) | set(getattr(sc, "writable", []) or [])):
            _writers_final.setdefault(f, []).append(st.id)
    for f, wids in _writers_final.items():
        wids = list(dict.fromkeys(wids))
        if len(wids) < 2:
            continue
        ordered = sorted(wids, key=lambda _i: _pos.get(_i, 1 << 30))  # 列表位次≈拓扑序，上游在前
        for k in range(1, len(ordered)):
            cur_id, prev_id = ordered[k], ordered[k - 1]
            cur = by_id_all.get(cur_id)
            if cur is None:
                continue
            # 已(传递)有序则跳过；防环：若 prev 已传递依赖 cur，加 cur→prev 会成环 → 跳过
            if _depends_transitively(cur_id, prev_id) or _depends_transitively(prev_id, cur_id):
                continue
            deps = list(getattr(cur, "depends_on", []) or [])
            if prev_id not in deps:
                deps.append(prev_id)
                cur.depends_on = deps
                changed = True

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
    """从共享契约提取需出现在变更中的【核心标识符】（非整句描述）。

    task 2c019bc5：契约 apis 常是 "GET /system/device/list — 分页查询设备列表，参数：..."
    这种带中文描述的整句。旧实现把整句当符号去 diff 精确匹配 → 必然找不到 → 误判契约偏离。
    修复：抽核心标识——API 取 URL 路径段（/system/device/list → device/list 或末段），
    类/方法/字段取其标识符 token。这样匹配的是代码里真会出现的东西，而非自然语言描述。
    """
    if not shared_contract:
        return []
    import re

    def _core(item: str) -> str:
        """从一条契约描述抽核心标识：优先 URL 路径末段，否则首个标识符 token。"""
        s = item.strip()
        # 截断描述部分（破折号/冒号/中文逗号后多为说明）
        s = re.split(r"\s*[—–:：，,]\s*", s, maxsplit=1)[0].strip()
        # API 形如 "GET /system/device/list" 或 "/system/device/edit/{id}"
        # → 取路径最后一个【非占位符】段（list / edit / device）
        url = re.search(r"/([\w/{}.\-]+)", s)
        if url:
            segs = [seg for seg in url.group(1).split("/")
                    if seg and "{" not in seg and seg.replace("-", "").replace(".", "").isalnum()]
            if segs:
                return segs[-1]
        # 否则取首个像标识符的 token（类名/方法名/字段名）
        tok = re.search(r"[A-Za-z_]\w{2,}", s)
        return tok.group(0) if tok else ""

    symbols: list[str] = []
    for key in ("interfaces", "types", "apis", "fields", "methods"):
        val = shared_contract.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    symbols.append(_core(item))
                elif isinstance(item, dict):
                    symbols.append(str(item.get("name") or item.get("id") or ""))
        elif isinstance(val, dict):
            symbols.extend(str(k) for k in val.keys())
    for item in shared_contract.get("symbols", []) or []:
        if isinstance(item, str):
            symbols.append(_core(item))
    # 去重 + 过滤太短/HTTP 动词噪音
    _noise = {"get", "post", "put", "delete", "patch", "the", "and", "for"}
    return [s for s in dict.fromkeys(symbols) if s and len(s) >= 3 and s.lower() not in _noise]


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


# ── 方案A(task 34fab09e)：上下文预注入 ───────────────────────────────────
# worker 在执行阶段把 50 步迭代预算【全耗在 cat/ls 探索代码】上（实测 84 命令多为 cat），
# 没到写代码就步数耗尽 → 空 diff。根因：scope 只给了文件路径，没给"理解功能所需的上下文"。
# 这里在 ELABORATE 阶段【直接读 scope 文件真实内容】抽取关键片段注入子任务 context_snippets，
# worker prompt 带上后即可直接写，无需自己 cat 探索。

_MAX_SNIPPET_CHARS_PER_FILE = 6000   # 单文件片段上限（防 prompt 爆炸）
_MAX_TOTAL_SNIPPET_CHARS = 24000     # 单子任务所有片段总上限
_READABLE_FULL_LINE_LIMIT = 280      # readable 参照文件 ≤此行数则全给，否则抽签名


def _extract_signatures(text: str, lang_ext: str) -> str:
    """轻量抽取类/方法/函数签名骨架（不依赖外部工具，正则即可，跨语言）。"""
    import re
    lines = text.split("\n")
    sig_lines: list[str] = []
    # 跨语言签名特征：类/接口/方法/函数声明行（含可见性修饰或 def/func/class 等）
    pat = re.compile(
        r"^\s*(?:"
        r"(?:public|private|protected|static|final|abstract|async|export|default)\s+)*"
        r"(?:class|interface|enum|struct|trait|def|func|function|fn|public|private|protected|void|"
        r"[A-Z][A-Za-z0-9_<>\[\]]*\s+[a-zA-Z_]\w*\s*\()"
    )
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        # 类/接口/枚举声明，或方法/函数签名（带括号）
        if pat.match(ln) or re.match(r"^\s*(class|interface|enum|struct|def |func |function |fn )", ln):
            sig_lines.append(f"{i+1}: {s[:160]}")
    return "\n".join(sig_lines[:120])


def _infer_create_layer(rel: str) -> tuple[str, str] | None:
    """从待新建文件路径推断其【分层类型】→ 返回 (层名, glob 范式) 用于找同类既有文件作模板。

    治本 RUN11：纯 CREATE 子任务 writable/readable 皆空 → context_snippets 空 → worker
    探索全项目找 RuoYi 写法烧光 600s 预算。给它预读一个【同类既有文件】(建 entity 就给个既有
    entity、建 mapper 就给个既有 mapper)，照着写即可，无需探索。跨语言可扩展，当前覆盖 Java 分层。
    """
    low = rel.replace("\\", "/").lower()
    if low.endswith(".xml") and "mapper" in low:
        return ("mapperxml", "**/resources/mapper/**/*.xml")
    if not low.endswith(".java"):
        return None
    if "/controller/" in low:
        return ("controller", "**/controller/*.java")
    if "/service/impl/" in low:
        return ("serviceimpl", "**/service/impl/*.java")
    if "/service/" in low:
        return ("service", "**/service/I*.java")
    if "/mapper/" in low:
        return ("mapper", "**/mapper/*.java")
    if "/vo/" in low:
        return ("vo", "**/vo/*.java")
    if "/dto/" in low:
        return ("dto", "**/dto/*.java")
    if "/domain/" in low or "/entity/" in low:
        return ("domain", "**/domain/*.java")
    return None


@_functools.lru_cache(maxsize=512)
def _find_layer_reference(project_path: str, pattern: str, exclude_top: str) -> str | None:
    """项目内匹配 pattern 的既有文件里挑【最小的一个】作模板(省 token)，排除新建模块目录。"""
    import glob as _glob
    import os as _os
    matches = _glob.glob(_os.path.join(project_path, pattern), recursive=True)
    cands = [
        m for m in matches
        if _os.path.isfile(m)
        and not _os.path.relpath(m, project_path).replace("\\", "/").startswith(exclude_top + "/")
    ]
    if not cands:
        return None
    cands.sort(key=lambda p: _os.path.getsize(p))
    return _os.path.relpath(cands[0], project_path).replace("\\", "/")


def enrich_context_snippets(plan: TaskPlan, project_path: str | None) -> bool:
    """把 scope 文件的关键代码片段抽进每个子任务的 context_snippets。

    - readable 参照文件（worker 要"照着写"的，如工具类/基类）：小文件给全文，大文件给签名。
    - writable 已存在文件（worker 要在其上改的）：给类声明 + 方法签名骨架（知道现有结构/往哪插）。
    返回是否发生注入。无 project_path → no-op。
    """
    if not project_path:
        return False
    import os

    changed = False
    for st in getattr(plan, "subtasks", []) or []:
        scope = getattr(st, "scope", None)
        if scope is None:
            continue
        if getattr(st, "context_snippets", ""):
            continue  # 已有则不覆盖（replan 幂等）

        writable = list(getattr(scope, "writable", []) or [])
        readable = list(getattr(scope, "readable", []) or [])
        parts: list[str] = []
        total = 0

        def _read(rel: str) -> str | None:
            abs = os.path.join(project_path, rel)
            if not os.path.isfile(abs):
                return None
            try:
                with open(abs, encoding="utf-8", errors="replace") as f:
                    return f.read()
            except OSError:
                return None

        # 1) writable 已存在文件 → 类/方法签名骨架（worker 需知现有结构，避免破坏/重复）
        for rel in writable:
            if total >= _MAX_TOTAL_SNIPPET_CHARS:
                break
            txt = _read(rel)
            if txt is None:
                continue  # 新建文件不存在，跳过
            ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
            sigs = _extract_signatures(txt, ext)
            if not sigs:
                continue
            block = f"### 待修改文件（现有结构，在此基础上改）: {rel}\n```\n{sigs[:_MAX_SNIPPET_CHARS_PER_FILE]}\n```"
            parts.append(block)
            total += len(block)

        # 2) readable 参照文件 → 小文件给全文（最有价值：worker 照着写），大文件给签名
        for rel in readable:
            if total >= _MAX_TOTAL_SNIPPET_CHARS:
                break
            txt = _read(rel)
            if txt is None:
                continue
            nlines = txt.count("\n") + 1
            ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
            if nlines <= _READABLE_FULL_LINE_LIMIT and len(txt) <= _MAX_SNIPPET_CHARS_PER_FILE:
                body = txt
                label = "参照文件（完整，照此写法/调用）"
            else:
                body = _extract_signatures(txt, ext)
                label = "参照文件（签名，可调用的接口）"
            if not body.strip():
                continue
            block = f"### {label}: {rel}\n```\n{body[:_MAX_SNIPPET_CHARS_PER_FILE]}\n```"
            parts.append(block)
            total += len(block)

        # 3) CREATE 文件无既有可读 → 找【同类既有文件】作模板注入(治本 LOCATING 空转)。
        # 每个分层类型只取一个范例(去重)，让 worker 照 RuoYi 写法实现，无需探索全项目。
        creates = list(getattr(scope, "create_files", []) or [])
        _exclude_top = ""
        for cf in creates:  # 新建模块顶层目录(如 ruoyi-alarm)——范例要排除它(它还不存在/正在建)
            top = cf.replace("\\", "/").split("/", 1)[0]
            if top:
                _exclude_top = top
                break
        seen_layers: set[str] = set()
        for rel in creates:
            if total >= _MAX_TOTAL_SNIPPET_CHARS:
                break
            layer = _infer_create_layer(rel)
            if not layer or layer[0] in seen_layers:
                continue
            ref = _find_layer_reference(project_path, layer[1], _exclude_top)
            if not ref:
                continue
            txt = _read(ref)
            if not txt:
                continue
            seen_layers.add(layer[0])
            ext = ref.rsplit(".", 1)[-1].lower() if "." in ref else ""
            body = txt if len(txt) <= _MAX_SNIPPET_CHARS_PER_FILE else _extract_signatures(txt, ext)
            if not body.strip():
                continue
            block = (f"### 同类既有范例（照此 RuoYi 写法实现 {rel} 这一层，无需再探索项目）: {ref}\n"
                     f"```\n{body[:_MAX_SNIPPET_CHARS_PER_FILE]}\n```")
            parts.append(block)
            total += len(block)

        if parts:
            st.context_snippets = (
                "以下是本子任务相关文件的真实代码（已为你预读，直接据此编写，"
                "无需再逐个 cat 探索）：\n\n" + "\n\n".join(parts)
            )
            changed = True
    return changed


def correct_misclassified_intent(plan: TaskPlan) -> bool:
    """用确定性信号（scope 有无写文件）校正 LLM 误判的子任务意图。

    task dbfc265f：产品功能需求"操作日志导出 Excel"被 LLM 误判 intent=AUDIT（因含
    "操作日志/权限校验"语义联想），→ 走 security_audit 不产 diff → findings=0 判失败 →
    retry 死循环。但 AUDIT 是【只读安全分析】，子任务若有 writable/create 文件，本质是
    【写代码】(MODIFY/CREATE)，意图必然判错。这里以"有无写文件"硬信号纠正 LLM 自由判断：
      - intent=AUDIT 但有 create_files（无对应 writable）→ CREATE
      - intent=AUDIT 但有 writable → MODIFY
    返回是否发生校正。
    """
    from swarm.types import TaskIntent

    changed = False
    for st in getattr(plan, "subtasks", []) or []:
        scope = getattr(st, "scope", None)
        if scope is None:
            continue
        writable = list(getattr(scope, "writable", []) or [])
        create = list(getattr(scope, "create_files", []) or [])
        if st.intent == TaskIntent.AUDIT and (writable or create):
            st.intent = TaskIntent.CREATE if (create and not writable) else TaskIntent.MODIFY
            changed = True
    return changed
