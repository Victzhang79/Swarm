"""共享契约 — Brain 统一定义、注入 Worker、L2 校验。"""

from __future__ import annotations

import functools as _functools
import json
import logging
import re
from typing import Any

from swarm.types import SubTaskDifficulty, TaskPlan

logger = logging.getLogger(__name__)

# Maven `-pl <module>` 提取（reactor 模块选择）。
_MVN_PL_RE = re.compile(r"-pl\s+([^\s,]+)")


def _is_root_pom(rel: str) -> bool:
    """是否为 Maven 根聚合 pom（repo 根的 pom.xml，无目录前缀）。

    D1 治本要害：根 pom 同时承载【加性 <modules> 注册】与【结构性 <dependencyManagement>
    版本块】。两个子任务各自【整段结构重写】它时，3-way/union 合并无法收口（round18 P0-A：
    畸形重复闭标签/斩头 dependency，或 rebase 循环→escalate→FAILED）。故根 pom 必须【单写者】。
    模块 pom（<module>/pom.xml，有目录前缀）各自独立、无争用，不在此列。
    """
    return str(rel).replace("\\", "/") == "pom.xml"


def _is_pom_file(rel: str) -> bool:
    """是否为 Maven pom（根或模块 pom，basename == pom.xml）。

    #11(a) 治本：任何 pom 都是【结构性全文件】——两个写者各自整段重写 <modules>/
    <dependencyManagement>/<dependencies>，union/3-way 合并无法收口（round18 P0-A 根 pom
    畸形闭标签 / round19 模块 pom 双 <project> 根拼接 → apply 后不可解析、交付死于门口）。
    故【任何 pom】都须单写者，非首写者一律 demote+依赖 owner（不止根 pom）。不同模块的 pom
    是不同文件（各有 first_writer），互不干扰——本判据只把"同一个 pom 的多写者"收敛。
    """
    return str(rel).replace("\\", "/").rsplit("/", 1)[-1] == "pom.xml"


def _exists_in_repo(project_path: str | None, rel: str, cache: dict[str, bool],
                    base_ref: str | None = None) -> bool:
    """文件是否已存在于项目 repo 基线（用于区分"聚合修改"vs"新建撞车"）。

    争抢分流的事实依据：已存在文件被多个独立子任务写 = 聚合/注册类共享文件
    （父 pom/settings.gradle/路由 index/DI 注册表…），必须保留各自写权（串行）不可
    静默降级丢贡献；不存在 = 真·新建撞车，独占首写者即可。

    ★B6 复核 #2★：git repo 以【任务钉扎 base】为权威基线（`git cat-file -e <base>:<rel>`）——
    ELABORATE 会在 replan/resplit 时重跑，此刻 HEAD 可能已被用户/兄弟任务推进；若这里读实时 HEAD
    而 merge/worker/L2 全链读 base，会把"base 时新建、HEAD 时已存在"的文件误判为 aggregate，
    错留多写者/串行化策略。base_ref=None → "HEAD"（零回归，与全链一致）。
    非 git → 退化 os.path.isfile。project_path 为空 → 一律 False（向后兼容）。结果按 rel 缓存。
    """
    if not project_path or not rel:
        return False
    if rel in cache:
        return cache[rel]
    import os
    import subprocess

    from swarm.git_base import resolve_base_ref
    _base = resolve_base_ref(base_ref)
    result = False
    try:
        if os.path.isdir(os.path.join(project_path, ".git")):
            r = subprocess.run(
                ["git", "-C", project_path, "cat-file", "-e", f"{_base}:{rel}"],
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


def _module_pom_owners(subtasks: list) -> dict[str, object]:
    """{物理模块名: 拥有该模块 `<模块>/pom.xml` 写权的子任务}（不含根 pom）。

    用于规则5 A5 归并：判断 plan 是否单物理模块（唯一 owner）。通用，不写死模块名。
    """
    owners: dict[str, object] = {}
    for st in subtasks:
        sc = getattr(st, "scope", None)
        if sc is None:
            continue
        files = list(getattr(sc, "create_files", []) or []) + list(getattr(sc, "writable", []) or [])
        for f in files:
            ff = str(f).replace("\\", "/")
            if ff.endswith("/pom.xml"):  # 模块 pom（有目录前缀），排除根 pom.xml
                modname = ff[: -len("/pom.xml")].rsplit("/", 1)[-1]
                if modname:
                    owners.setdefault(modname, st)
    return owners


def normalize_plan_scopes(plan: TaskPlan, project_path: str | None = None,
                          base_ref: str | None = None) -> bool:
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
        if len(ids) >= 2 and _exists_in_repo(project_path, f, _exist_cache, base_ref)
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
            elif _is_pom_file(f):
                # D1 治本(#11a 扩展到模块 pom)：任何 pom(根/模块)永远【单写者】(收敛唯一
                # owner)。非首写者【一律 demote】为 readable + 依赖 owner——不论是否同链/聚合。
                # 两份【整段结构重写】(<modules>/<dependencyManagement>/<dependencies>)无法安全
                # 合并(round18 P0-A 根 pom 畸形闭标签 / round19 模块 pom 双 <project> 拼接)。
                # demote 不丢登记：根 <modules> 由 reconcile_workspace_manifests 据磁盘
                # ground-truth 补齐(L1/L2/交付三处)，dependencyManagement 版本由 D2 reconcile
                # 兜底；模块 pom 自身由 owner 一次建全(脚手架职责)。owner 侧由规则4 确保登记全部新模块。
                demoted.append(f)
                serialized_ids.add(st.id)  # 获依赖边 → 需清 parallel_groups(不与 owner 同组)
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
            # 降级者（新建撞车 / 根 pom 非 owner）依赖首写者强制串行，杜绝并发物理冲突。
            # 防环：owner 若已(传递)依赖本子任务，加反向边会成环 → 跳过(不加边，reconcile 兜底登记)。
            for f in demoted:
                writer = first_writer.get(f)
                if (writer and writer != st.id and writer not in deps
                        and not _depends_transitively(writer, st.id)):
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

    # ── 规则 4：Maven 根 pom 单 owner 登记全部新模块（D1 配套：owner 恒登记，非仅 unowned 时）──
    # 规则3 只补各模块【自己的】pom；根 `<modules>` 注册是 N 个新模块往同一文件追加。规则1 已把
    # 根 pom 收敛为【唯一 owner】(非首写者 demote)。本规则确保【那个 owner】(或无人 own 时指派一个)
    # 登记全部新模块——包括被 demote 写者的模块，杜绝注册落空。additive、去重、带防环。
    # 注：<modules> 最终仍由 reconcile_workspace_manifests 据磁盘 ground-truth 兜底补齐；此处
    # 令 owner 显式登记是【计划意图】层的收口(worker 一次建全、验收可查)，与 reconcile 双保险。
    new_modules: set[str] = set()
    root_pom_owner = None

    def _module_dir_of_pom(rel: str) -> str | None:
        """rel 若是模块 pom（任意嵌套深度的 <dir>/pom.xml，根 pom 不算）→ 返回模块目录。
        round29 复核整改（猎人#5）：旧判定 count("/")==1 使嵌套模块（backend/svc-a/pom.xml）
        对规则 4 完全不可见 → 零序约束，d37a52a3 类 reactor 中毒在 monorepo 布局原样复现。"""
        fn = str(rel).replace("\\", "/").lstrip("./")
        if "/" not in fn:
            return None
        d, base = fn.rsplit("/", 1)
        return d if base == "pom.xml" and d else None

    for st in subtasks:
        scope = getattr(st, "scope", None)
        if scope is None:
            continue
        creates = list(getattr(scope, "create_files", []) or [])
        writables = list(getattr(scope, "writable", []) or [])
        if root_pom_owner is None and ("pom.xml" in creates or "pom.xml" in writables):
            root_pom_owner = st  # 规则1 收敛后唯一 owner（列表序首个）
        for cf in creates:
            d = _module_dir_of_pom(cf)
            if d:
                new_modules.add(d)
        # 复核整改（reviewer#3）：LLM 可能把新模块 pom 误标进 writable（目录已有部分文件）——
        # 以 repo 基线真值兜底判新（基线无此 pom = 真新建），口径与 builds_new_module 一致。
        for wf in writables:
            d = _module_dir_of_pom(wf)
            if d and not _exists_in_repo(
                    project_path, str(wf).replace("\\", "/").lstrip("./"), _exist_cache, base_ref):
                new_modules.add(d)
    # 有新模块 + 根 pom 已存在于 repo（真·注册进父 pom 场景）。
    if new_modules and _exists_in_repo(project_path, "pom.xml", _exist_cache, base_ref):
        # owner = 已收敛的根 pom owner；无人 own 时 backstop 指派首个建模块 pom 的子任务。
        owner = root_pom_owner or next(
            (
                st for st in subtasks
                if any(
                    _module_dir_of_pom(cf)
                    for cf in (getattr(getattr(st, "scope", None), "create_files", []) or [])
                )
            ),
            None,
        )
        if owner is not None and getattr(owner, "scope", None) is not None:
            w = list(getattr(owner.scope, "writable", []) or [])
            _owner_creates = list(getattr(owner.scope, "create_files", []) or [])
            if "pom.xml" not in w and "pom.xml" not in _owner_creates:
                w.append("pom.xml")
                owner.scope.writable = w
                changed = True
            ac = list(getattr(owner, "acceptance_criteria", []) or [])
            note = f"在根 pom.xml 的 <modules> 中登记全部新模块: {sorted(new_modules)}"
            if note not in ac:
                ac.append(note)
                owner.acceptance_criteria = ac
                changed = True
            # round29 A(c) 治本：依赖序方向反正——单一规范不变量「注册后于脚手架」。
            # 旧边（scaffold depends_on owner=注册先行）使注册先落地而模块目录不存在 →
            # Maven `Child module … does not exist` 毒化全 reactor → 级联 abandon
            # （task d37a52a3 真根因）。新序：
            #   · owner(registrant) depends_on 每个【脚手架】（建 <module>/pom.xml 者），
            #     并删除既有反向直边（不叠边，防 2-cycle 被环卫随机断）；
            #   · 模块【内容】子任务（不建新模块 pom）仍依赖 owner（内容 -pl 构建需注册在位，
            #     链式 content→owner→scaffold 传递保序）。
            # 脚手架自身的 -pl 构建不需注册先行：清单 reconcile 在沙箱内自愈注册
            # （l1_pipeline._push_manifests_to_sandbox），两向均带 _depends_transitively 防环。
            _owner_scope = getattr(owner, "scope", None)
            _owner_other_files = {
                str(f).replace("\\", "/").lstrip("./")
                for f in (list(getattr(_owner_scope, "writable", []) or [])
                          + list(getattr(_owner_scope, "create_files", []) or []))
            } - {"pom.xml"}
            for st in subtasks:
                if st.id == owner.id:
                    continue
                scope = getattr(st, "scope", None)
                if scope is None:
                    continue
                creates = list(getattr(scope, "create_files", []) or [])
                writables = list(getattr(scope, "writable", []) or [])
                _st_norm = {str(f).replace("\\", "/").lstrip("./") for f in creates + writables}
                # 脚手架=建任意新模块的 pom（嵌套深度不限；writable 里的新模块 pom 已并入 new_modules）
                is_scaffold = any(
                    (_module_dir_of_pom(cf) or "") in new_modules
                    for cf in creates + writables if _module_dir_of_pom(cf)
                )
                builds_new_module = any(
                    fn.startswith(m + "/") for fn in _st_norm for m in new_modules
                )
                if is_scaffold:
                    # 复核护栏（reviewer#2）：st 与 owner 还共享【其它非根 pom 文件】的写序时，
                    # 既有 demote/串行边可能承载那份文件的物理写序——保守跳过规范化（不删不加），
                    # 该模块的注册序交 reconcile/运行期序修复阶梯兜底。
                    if _owner_other_files & (_st_norm - {"pom.xml"}):
                        logger.info(
                            "[contract] 规则4 跳过 %s↔%s 序规范化：两者共享其它文件写序（%s），"
                            "保守保留既有边，注册序交 reconcile/运行期阶梯兜底",
                            owner.id, st.id,
                            sorted(_owner_other_files & (_st_norm - {"pom.xml"}))[:3],
                        )
                        continue
                    deps_st = list(getattr(st, "depends_on", []) or [])
                    if owner.id in deps_st:
                        deps_st.remove(owner.id)   # 删反向直边：只留单一规范方向
                        st.depends_on = deps_st
                        changed = True
                    if not _depends_transitively(st.id, owner.id):
                        odeps = list(getattr(owner, "depends_on", []) or [])
                        if st.id not in odeps:
                            odeps.append(st.id)
                            owner.depends_on = odeps
                            changed = True
                elif builds_new_module and not _depends_transitively(owner.id, st.id):
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

    # ── 规则 5：模块依赖契约落地（治本：编译期缺依赖 → 必败 → 全量 replan，task f9e38dae）──
    # 现场：st-1 顺手建 ruoyi-alarm/pom.xml 只声明自己要的依赖；后续 30 个引擎/渠道子任务用
    # RedisTemplate/@Slf4j 但 pom 没声明、它们 scope 又碰不到 pom → mvn compile 必败。根因=
    # 规划器从不把"模块依赖并集"当契约。本规则：把 shared_contract.dependencies 里每个模块需要的
    # artifacts，确定性地追加进【该模块 pom owner 子任务】的 acceptance_criteria（additive、去重），
    # 即使 LLM 漏写 prompt 要求，也强制 owner 把依赖声明全、可被 mvn compile 验收。零 LLM、纯函数可测。
    shared = getattr(plan, "shared_contract", None) or {}
    deps_spec = shared.get("dependencies") if isinstance(shared, dict) else None
    if isinstance(deps_spec, list) and deps_spec:
        # A5 治本(round11)：契约常把【逻辑模块】(alarm-robot/template…)当物理 Maven 模块声明依赖，
        # 但 plan 实际把它们的代码都落进【单个】物理模块(如 ruoyi-alarm)。此时 `alarm-robot/pom.xml`
        # 无 owner → 原逻辑只告警、依赖落空 → 编译期缺依赖。修法：仅当全 plan 存在【唯一】物理模块
        # pom owner(单模块项目，无歧义)时，把无独立 owner 的契约依赖确定性归并到它，杜绝落空 + 消除
        # false-alarm。多 owner(真多模块)歧义 → 保守只告警(行为不变)。通用，不写死模块名。
        _mod_owners = _module_pom_owners(subtasks)
        _distinct = list({id(o): o for o in _mod_owners.values()}.values())
        _sole_owner = _distinct[0] if len(_distinct) == 1 else None
        for entry in deps_spec:
            if not isinstance(entry, dict):
                continue
            mod = (entry.get("module") or "").strip().rstrip("/")
            arts = [a for a in (entry.get("artifacts") or []) if a]
            if not mod or not arts:
                continue
            mod_pom = f"{mod}/pom.xml"
            owner = next(
                (
                    st for st in subtasks
                    if mod_pom in (
                        list(getattr(getattr(st, "scope", None), "create_files", []) or [])
                        + list(getattr(getattr(st, "scope", None), "writable", []) or [])
                    )
                ),
                None,
            )
            reconciled = False
            if owner is None:
                if _sole_owner is not None:
                    owner = _sole_owner
                    reconciled = True
                    logger.info(
                        "[normalize] 规则5：契约模块 %s 无独立 pom owner → 逻辑模块落进单物理模块，"
                        "依赖确定性归并到唯一物理模块 pom owner %s（杜绝依赖落空+消除 false-alarm）",
                        mod, getattr(_sole_owner, "id", "?"),
                    )
                else:
                    logger.warning(
                        "[normalize] 规则5：模块 %s 的依赖契约无 pom owner 承接（%d 个 artifacts 落空）"
                        "——编译期可能缺依赖，请确认有脚手架子任务建 %s",
                        mod, len(arts), mod_pom,
                    )
                    continue
            ac = list(getattr(owner, "acceptance_criteria", []) or [])
            if reconciled:
                note = (f"本模块 pom.xml 必须声明 {mod} 所需依赖: {sorted(arts)}"
                        f"（{mod} 的代码落在本物理模块，缺一即 mvn compile 失败）")
            else:
                note = f"{mod}/pom.xml 必须声明依赖: {sorted(arts)}（缺一即整模块 mvn compile 失败）"
            if note not in ac:
                ac.append(note)
                owner.acceptance_criteria = ac
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
    # ── 非 Java 生态常见分层（CODEWALK 根因C：原仅 Java/MyBatis，其余栈拿不到模板
    # 只能全项目探索烧预算；识别不了的类型仍 fail-safe 返回 None 走探索）──
    if low.endswith(".vue"):
        if "/views/" in low:
            return ("vue_view", "**/views/**/*.vue")
        if "/components/" in low:
            return ("vue_component", "**/components/**/*.vue")
        return ("vue", "**/*.vue")
    if low.endswith((".ts", ".js")) and "/api/" in low:
        return ("api_client", "**/api/**/*.[tj]s")
    if low.endswith(".go"):
        if "/handler/" in low or "/handlers/" in low:
            return ("go_handler", "**/handler*/*.go")
        if "/service/" in low:
            return ("go_service", "**/service/*.go")
        return None
    if low.endswith(".py"):
        if "/routers/" in low or "/router/" in low:
            return ("py_router", "**/router*/*.py")
        return None
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
            block = (f"### 同类既有范例（照此项目既有写法实现 {rel} 这一层，无需再探索项目）: {ref}\n"
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


# ── D4(b) 外部库 API 知识注入 ─────────────────────────────────────────────
# 治本 round18 st-16：本地小模型对第三方库类名/方法名产生幻觉+退化死循环(把 okhttp3.OkHttpClient
# 写成 OkHttp、方法名退化 executeecute)烧光 900s。通用治法(非硬编 okhttp=B 类 hack)：小型可扩展
# 知识表(key=依赖 artifact 片段 / import 前缀，value=正确类名+关键方法签名)，按 plan 声明的依赖命中,
# 把正确签名片段确定性注入【写源码且所在模块声明了该库】的子任务 context_snippets。表按需扩条即可,
# 不绑定具体项目/模块名，跨栈可加(Go/TS 等)。
_API_KNOWLEDGE: list[dict[str, Any]] = [
    {
        # OkHttp 3/4：小模型高频把客户端类 OkHttpClient 写成 OkHttp、方法名退化。
        "artifacts": ["com.squareup.okhttp3:okhttp", "com.squareup.okhttp", "okhttp3"],
        "title": "OkHttp (okhttp3) 正确 API",
        "snippet": (
            "import okhttp3.OkHttpClient;   // 客户端类名是 OkHttpClient（不是 OkHttp）\n"
            "import okhttp3.Request;\n"
            "import okhttp3.RequestBody;\n"
            "import okhttp3.MediaType;\n"
            "import okhttp3.Response;\n"
            "\n"
            "OkHttpClient client = new OkHttpClient();\n"
            "MediaType JSON = MediaType.parse(\"application/json; charset=utf-8\");\n"
            "RequestBody body = RequestBody.create(jsonString, JSON);   // okhttp 4.x\n"
            "// okhttp 3.x 参数顺序相反: RequestBody.create(JSON, jsonString)\n"
            "Request request = new Request.Builder().url(url).post(body).build();\n"
            "try (Response response = client.newCall(request).execute()) {\n"
            "    int code = response.code();\n"
            "    String respBody = response.body() != null ? response.body().string() : \"\";\n"
            "}\n"
            "\n"
            "// 若对第三方 HTTP 客户端 API 不确定，可改用 JDK 自带 java.net.http.HttpClient（无需额外依赖）:\n"
            "//   HttpClient c = HttpClient.newHttpClient();\n"
            "//   HttpRequest r = HttpRequest.newBuilder(URI.create(url))\n"
            "//       .header(\"Content-Type\", \"application/json\")\n"
            "//       .POST(HttpRequest.BodyPublishers.ofString(jsonString)).build();\n"
            "//   HttpResponse<String> resp = c.send(r, HttpResponse.BodyHandlers.ofString());\n"
        ),
    },
]

_SOURCE_EXTS = frozenset({
    "java", "kt", "kts", "scala", "groovy", "go", "py", "ts", "tsx", "js", "jsx",
    "vue", "rs", "cs", "rb", "php", "swift", "cpp", "cc", "c", "h", "hpp",
})


def _is_source_file(rel: str) -> bool:
    ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
    return ext in _SOURCE_EXTS


def _module_of(rel: str) -> str:
    """文件所属【物理模块顶层目录】(RuoYi: ruoyi-alarm/…/X.java → ruoyi-alarm)。"""
    return rel.replace("\\", "/").split("/", 1)[0]


def _artifact_hits(patterns: list[str], declared: set[str]) -> bool:
    """知识表 entry 的任一 artifact 片段是否命中任一声明依赖(大小写不敏感子串)。"""
    low = [d.lower() for d in declared]
    return any(any(p.lower() in d for d in low) for p in patterns)


def inject_api_knowledge(plan: TaskPlan) -> bool:
    """按 plan 声明的依赖命中知识表，把正确外部库 API 签名注入相关子任务 context_snippets。

    命中规则(确定性/幂等/零 LLM)：
      - 子任务须【写源码文件】(纯 pom/注册子任务跳过——它们不调库 API)。
      - 子任务所在物理模块声明了该库(shared_contract.dependencies)；契约常以【逻辑模块名】声明,
        故当全 plan 仅一个物理模块时用其依赖并集 fallback(A5 同风格,杜绝逻辑↔物理错配落空)。
    additive 叠加在已有 context_snippets 之后；重复注入按标题幂等(replan 安全)。返回是否注入。
    """
    shared = getattr(plan, "shared_contract", None) or {}
    deps_spec = shared.get("dependencies") if isinstance(shared, dict) else None
    if not isinstance(deps_spec, list) or not deps_spec:
        return False

    mod_arts: dict[str, set[str]] = {}
    for entry in deps_spec:
        if not isinstance(entry, dict):
            continue
        mod = (entry.get("module") or "").strip().rstrip("/")
        for a in (entry.get("artifacts") or []):
            if a:
                mod_arts.setdefault(mod, set()).add(str(a))
    if not mod_arts:
        return False
    all_arts: set[str] = set().union(*mod_arts.values())

    subtasks = getattr(plan, "subtasks", []) or []
    phys_modules = {
        _module_of(f)
        for st in subtasks
        for f in (list(getattr(getattr(st, "scope", None), "create_files", []) or [])
                  + list(getattr(getattr(st, "scope", None), "writable", []) or []))
        if f
    }
    sole_phys = len(phys_modules) == 1

    changed = False
    for st in subtasks:
        scope = getattr(st, "scope", None)
        if scope is None:
            continue
        srcs = [f for f in (list(getattr(scope, "create_files", []) or [])
                            + list(getattr(scope, "writable", []) or []))
                if _is_source_file(f)]
        if not srcs:
            continue  # 纯 pom/注册子任务 → 不注入库 API 片段
        st_mod = _module_of(srcs[0])
        arts = set(mod_arts.get(st_mod, set()))
        if sole_phys:
            arts |= all_arts   # 单物理模块：逻辑模块声明的依赖都落在它 → 用并集
        if not arts:
            continue

        existing = getattr(st, "context_snippets", "") or ""
        new_blocks: list[str] = []
        for entry in _API_KNOWLEDGE:
            if not _artifact_hits(entry["artifacts"], arts):
                continue
            header = f"### 外部库正确 API（照此签名调用，勿凭记忆臆造类名/方法）— {entry['title']}"
            if header in existing:
                continue  # 幂等：已注入过
            new_blocks.append(f"{header}\n```\n{entry['snippet']}\n```")
        if not new_blocks:
            continue
        st.context_snippets = (
            existing + ("\n\n" if existing else "")
            + "以下外部依赖库的 API 已为你校准（本地小模型对第三方库类名/方法名易产生幻觉，"
              "请严格照此，不确定时优先用 JDK 自带等价物）：\n\n"
            + "\n\n".join(new_blocks)
        )
        changed = True
    return changed


def _st_create_files(st) -> list[str]:
    sc = getattr(st, "scope", None)
    return list(getattr(sc, "create_files", []) or []) if sc else []


def _is_scaffold_subtask(st) -> bool:
    """脚手架子任务 = 创建模块 pom.xml(且不建实体)，是模块的地基,应最先就位。"""
    cf = _st_create_files(st)
    has_pom = any(f.replace("\\", "/").rsplit("/", 1)[-1] == "pom.xml" for f in cf)
    builds_entity = any(f.endswith(".java") and ("/domain/" in f or "/entity/" in f) for f in cf)
    return has_pom and not builds_entity


def _is_sql_subtask(st) -> bool:
    """纯 SQL 子任务 = create 全是 .sql(建表 DDL / seed)。"""
    cf = _st_create_files(st)
    return bool(cf) and all(f.endswith(".sql") for f in cf)


def bump_scaffold_difficulty(plan: TaskPlan) -> int:
    """治本(RUN19 根脚手架卡死)：脚手架 / 写根 pom 的子任务，难度下限提到 MEDIUM。

    RUN19 现场：st-1 是"建模块 pom.xml + 编辑庞大根 pom 的 <modules> 注册 + 建目录"的根脚手架，
    被 LLM 误判 difficulty=trivial → 走 worker 的【trivial 单发快速路径】(合并定位+编码于一次 agent
    运行，封顶 30 步)。但读懂大根 pom + 定位 <modules> + 追加注册 + 另建模块 pom 本质是【多步】任务，
    单发塞不下 → 40B 吐 "Sorry, need more steps" 拒答(撞内部上限) → 根脚手架硬失败。因所有功能子任务
    都依赖它，全依赖链卡死 → 看守判死循环取消(3/13)。即便 force_strong 换最强模型也救不了：问题不在
    模型强弱，在【路径】——这种脚手架必须走结构化 locate→code→verify 多步路径(MEDIUM 起，按文件数
    动态加步数预算)，而非 trivial 单发。

    规则：difficulty==TRIVIAL 且 (是脚手架子任务 或 写根 pom.xml) → 提到 MEDIUM。原地改，返回提升个数。
    """
    bumped = 0
    for st in getattr(plan, "subtasks", []) or []:
        if getattr(st, "difficulty", None) != SubTaskDifficulty.TRIVIAL:
            continue
        sc = getattr(st, "scope", None)
        writes = set(_st_create_files(st)) | set(getattr(sc, "writable", []) or [])
        writes_root_pom = "pom.xml" in writes  # 根 pom：大文件 + 多模块登记，读改皆重
        if _is_scaffold_subtask(st) or writes_root_pom:
            st.difficulty = SubTaskDifficulty.MEDIUM
            bumped += 1
    return bumped


def resolve_plan_conflicts(plan: TaskPlan, project_path: str | None = None,
                           base_ref: str | None = None) -> dict[str, int]:
    """计划冲突解决【唯一事实源】——确定性后处理 pass 的【规范顺序】，_elaborate 与离线评测共用。

    顺序是治本要害(RUN18 实证：两 pass 互撤 → 0 交付)，做成单一函数杜绝调用点各写一份导致漂移：

      1) dedupe_module_scaffolds  —— 先合并重复模块脚手架(N 个建同一 module pom → 1 个)，
         避免后续按文件归一时把重复地基当多写者乱串。
      2) fix_dependency_ordering  —— 依赖序重构(脚手架置根 + SQL 依赖实体跑最后)。【必须在 normalize 前】：
         它的"脚手架置根"会清空脚手架 depends_on。
      3) normalize_plan_scopes    —— scope 单一写者不变量【最后定锤】(给共享聚合文件 root pom 写者补
         串行化依赖)。放在 fix_dep【之后】，其补的串行化依赖不再被任何后续 pass 撤销。
         ★ 反例(RUN18)：normalize→fix_dep 顺序下，fix_dep 把脚手架(恰是 root pom 写者)依赖清空 →
           退回"N 个无依赖子任务同时写 pom" → plan_validator 硬失败 → auto_accept fail-fast → 0 交付。
      4) bump_scaffold_difficulty —— 脚手架/根 pom 写者难度提 MEDIUM，避开 worker trivial 单发拒答(RUN19)。

    plan_validator 校验的"每个文件单一写者 + 无悬空依赖"不变量，由本函数确定性满足。返回各 pass 改动计数。
    """
    return {
        "scaffolds_merged": dedupe_module_scaffolds(plan),
        "dep_reordered": int(fix_dependency_ordering(plan)),
        "scope_normalized": int(normalize_plan_scopes(plan, project_path=project_path, base_ref=base_ref)),
        "difficulty_bumped": bump_scaffold_difficulty(plan),
    }


def _union_keep_order(*lists) -> list:
    seen: set = set()
    out: list = []
    for lst in lists:
        for x in (lst or []):
            if x not in seen:
                seen.add(x)
                out.append(x)
    return out


def dedupe_module_scaffolds(plan: TaskPlan) -> int:
    """治本(RUN17 严重冲突,VALIDATE 只软警告未修)：多个子任务重复创建【同一模块脚手架】
    (都建同一个 <module>/pom.xml)→ 合并为一个 canonical。

    重复地基即便各自编译过,也是冗余/互相覆盖的非生产级产物(4 个子任务各建一遍 ruoyi-alarm
    模块 pom/目录/根 pom 注册)。确定性合并:保留首个,其余 create/writable/readable/depends_on
    并入它,下游依赖重映射到它,删除其余。返回合并掉的子任务数。
    """
    import collections
    subs = list(getattr(plan, "subtasks", None) or [])
    if len(subs) < 2:
        return 0
    # 按【模块 pom 路径】给脚手架子任务分组(只认带目录前缀的模块 pom,排除根 pom.xml)
    groups: "collections.OrderedDict[str, list]" = collections.OrderedDict()
    for st in subs:
        if not _is_scaffold_subtask(st):
            continue
        for f in _st_create_files(st):
            norm = f.replace("\\", "/")
            if norm.rsplit("/", 1)[-1] == "pom.xml" and "/" in norm:
                groups.setdefault(norm, []).append(st)
                break
    drop_to_canon: dict[str, str] = {}
    merged = 0
    for _pom, group in groups.items():
        if len(group) < 2:
            continue
        canon = group[0]
        for dup in group[1:]:
            cs, ds = getattr(canon, "scope", None), getattr(dup, "scope", None)
            if cs and ds:
                cs.create_files = _union_keep_order(cs.create_files, ds.create_files)
                cs.writable = _union_keep_order(cs.writable, ds.writable)
                cs.readable = _union_keep_order(cs.readable, ds.readable)
            canon.depends_on = _union_keep_order(getattr(canon, "depends_on", []),
                                                 getattr(dup, "depends_on", []))
            drop_to_canon[dup.id] = canon.id
            merged += 1
    if not merged:
        return 0
    plan.subtasks = [s for s in subs if s.id not in drop_to_canon]
    # 重映射所有下游依赖到 canonical，去自依赖
    for s in plan.subtasks:
        s.depends_on = sorted({drop_to_canon.get(d, d) for d in (getattr(s, "depends_on", []) or [])
                               if drop_to_canon.get(d, d) != s.id})
    logger.info("[ELABORATE] 重复模块脚手架合并：%d 个重复脚手架并入 canonical(杜绝冗余地基,治严重文件冲突)",
                merged)
    return merged


def fix_dependency_ordering(plan: TaskPlan) -> bool:
    """治本(RUN17 依赖倒置死锁)：确定性修正子任务【依赖序】，杜绝"建全部表 SQL"巨任务
    成为全局根瓶颈 → 无实体上下文空转超时 → 整个项目卡死。

    三条规则(纯结构,不调 LLM,可复现)：
      1. 没人应依赖 SQL 子任务 —— 把其它子任务 depends_on 里的 sql id 剥掉(SQL 不该挡路)。
      2. 脚手架子任务【置根】(depends_on=[]) —— 模块 pom 最先建,别吊在 SQL/seed 后面。
      3. SQL 子任务改为【依赖所有实体(java)子任务】、跑在最后 —— 实体建完才有字段可建表;
         并把实体 domain 文件纳入其 readable，让 worker 照字段生成 DDL(防无上下文空转)。
    返回是否改动了 plan。
    """
    subs = list(getattr(plan, "subtasks", None) or [])
    if not subs:
        return False
    scaffold_ids = {st.id for st in subs if _is_scaffold_subtask(st)}
    sql_ids = {st.id for st in subs if _is_sql_subtask(st)}
    if not sql_ids and not scaffold_ids:
        return False
    java_ids = sorted({st.id for st in subs
                       if any(f.endswith(".java") for f in _st_create_files(st))
                       and st.id not in scaffold_ids and st.id not in sql_ids})
    entity_files = sorted({f for st in subs for f in _st_create_files(st)
                           if f.endswith(".java") and ("/domain/" in f or "/entity/" in f)})
    changed = False

    # 规则 1：剥离别人对 SQL 的依赖
    for st in subs:
        if st.id in sql_ids:
            continue
        deps = list(getattr(st, "depends_on", []) or [])
        nd = [d for d in deps if d not in sql_ids]
        if nd != deps:
            st.depends_on = nd
            changed = True

    # 规则 2：脚手架置根
    for st in subs:
        if st.id in scaffold_ids and (getattr(st, "depends_on", None) or []):
            st.depends_on = []
            changed = True

    # 规则 3：SQL 依赖所有实体(无 java 则兜底依赖脚手架),并纳入实体 readable
    target = java_ids or sorted(scaffold_ids)
    for st in subs:
        if st.id not in sql_ids:
            continue
        nd = [t for t in target if t != st.id]
        if set(getattr(st, "depends_on", []) or []) != set(nd):
            st.depends_on = nd
            changed = True
        sc = getattr(st, "scope", None)
        if sc and entity_files:
            r = list(getattr(sc, "readable", []) or [])
            add = [f for f in entity_files if f not in r]
            if add:
                sc.readable = r + add
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
