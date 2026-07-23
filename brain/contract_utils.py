"""共享契约 — Brain 统一定义、注入 Worker、L2 校验。"""

from __future__ import annotations

import functools as _functools
import json
import logging
import re
from pathlib import Path
from typing import Any

from swarm.types import SubTaskDifficulty, TaskPlan

logger = logging.getLogger(__name__)


def _record_degrade_safe(category: str) -> None:
    """record_degrade 薄封装——degrade 基建缺失/异常绝不炸主链（本模块降级留痕用）。"""
    try:
        from swarm.infra.degrade import record_degrade
        record_degrade(category)
    except Exception:  # noqa: BLE001
        pass

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
    """将 plan.shared_contract 合并进各子任务 contract（子任务字段优先）。

    D51：plan 节点已【不再调用】本函数——每子任务内联一份 ~42K shared 副本是 plan/
    checkpoint 体积病灶（slim_plan_json_for_llm_validation 就是为对冲它而生的补丁）。
    完整契约改由派发面 worker/prompts.build_worker_prompt 以同一 merge 语义现场合成。
    函数保留：merge 语义的单一参照实现 + 既有测试消费者 + 兼容外部调用。"""
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


def _base_tree_listing(project_path: str | None, base_ref: str | None) -> list[str] | None:
    """规则0：base 树全量文件清单（单次 git ls-tree，失败/非 git → None=跳过规则0）。"""
    if not project_path:
        return None
    import os
    import subprocess

    from swarm.git_base import resolve_base_ref
    if not os.path.isdir(os.path.join(project_path, ".git")):
        return None
    try:
        r = subprocess.run(
            ["git", "-C", project_path, "ls-tree", "-r", "--name-only", "-z",
             resolve_base_ref(base_ref)],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        return [p for p in r.stdout.split("\0") if p]
    except (OSError, subprocess.SubprocessError):
        return None


def _norm_scope_path(f) -> str:
    """scope 路径归一（R41 复核 F5）：反斜杠→/、剥 './' 前缀与前导 '/'。"""
    p = str(f).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def unclaimed_contract_deps(plan) -> list[dict]:
    """C1/规则5 机读面（round38c：98 条 artifacts 落空纯 log 无消费）：返回无 pom owner
    承接且无法归并（多物理模块歧义）的契约依赖 entries [{module, artifacts}]，供
    VALIDATE_PLAN 升 warn 可观测。单物理模块场景规则5 已确定性归并 → 恒空。"""
    shared = getattr(plan, "shared_contract", None) or {}
    deps_spec = shared.get("dependencies") if isinstance(shared, dict) else None
    if not (isinstance(deps_spec, list) and deps_spec):
        return []
    subtasks = list(getattr(plan, "subtasks", None) or [])
    _mod_owners = _module_pom_owners(subtasks)
    _distinct = list({id(o): o for o in _mod_owners.values()}.values())
    if len(_distinct) == 1:
        return []
    out: list[dict] = []
    for entry in deps_spec:
        if not isinstance(entry, dict):
            continue
        mod = (entry.get("module") or "").strip().rstrip("/")
        arts = [a for a in (entry.get("artifacts") or []) if a]
        if not mod or not arts:
            continue
        mod_pom = f"{mod}/pom.xml"
        # R41 复核 F5：归一后再比（./mod/pom.xml、反斜杠等写法的 owner 此前会被漏判
        # → 重复注入 pom 写者 → T3 单写者归一把脚手架降成空 scope 壳子任务）
        owner = next((st for st in subtasks if mod_pom in (
            _norm_scope_path(f)
            for f in (list(getattr(getattr(st, "scope", None), "create_files", []) or [])
                      + list(getattr(getattr(st, "scope", None), "writable", []) or []))
        )), None)
        if owner is None:
            out.append({"module": mod, "artifacts": arts})
    return out


def _dep_group_from_baseline(project_path: str, artifact_id: str) -> str | None:
    """R47-2：从基线 poms ground truth 解析依赖 artifactId 的真实 groupId。

    round47 实锤：模板对裸 artifact（spring-boot-starter-web/lombok/…）回退用
    【工程 groupId】= 凭空制造 `com.ruoyi:spring-boot-starter-web` 无版本幽灵坐标，
    盖着"权威模板"章让听话 worker 原样写入 → 毒化整个 reactor（R45-2 要防的病被
    模板自己复制）。治本 = groupId 只认基线证据：root pom（dependencyManagement
    含）优先，其余基线 poms 兜底；解析不到返回 None（调用方省略该依赖并响亮日志
    ——缺依赖是可归因可修的编译错，伪造坐标是 reactor 毒药）。纯文本确定性解析。
    """
    import re as _re
    root = Path(project_path)
    poms = [root / "pom.xml"]
    try:
        poms += sorted(root.glob("*/pom.xml"))  # 单层扫描假设：多模块惯例为扁平布局
    except OSError:
        pass
    # 复核 F1（真树复现级）：往届轮次交付/残留的 LLM 毒 pom（com.ruoyi:starter-web 类
    # 伪造块）也躺在项目树里——"项目树=干净基线"跨任务即失效，首个匹配会把 round47 的
    # 毒原样发回还盖权威章。治法：①收集全部候选 group + 各 pom 自身 artifactId（工程
    # 内部模块集合）；②非工程 groupId 的候选唯一 → 采信；多个互斥 → 存疑弃用；
    # ③工程 groupId 只有当 artifact 真是 reactor 内部模块时才合法（裸第三方 artifact
    # + 工程 groupId = 伪造，本函数的公理，无论证据来自哪都拒绝）。
    project_group: str | None = None
    module_own: set[str] = set()
    candidates: list[str] = []
    for i, pom in enumerate(poms):
        try:
            txt = pom.read_text("utf-8", errors="replace")
        except OSError:
            continue
        txt = _re.sub(r"<!--.*?-->", "", txt, flags=_re.S)
        # pom 自身坐标区（剥 parent/依赖/构建块后首个 artifactId/groupId）
        body = _re.sub(r"<parent>.*?</parent>", "", txt, flags=_re.S)
        body = _re.sub(
            r"<dependencyManagement>.*?</dependencyManagement>", "", body, flags=_re.S)
        body = _re.sub(r"<dependencies>.*?</dependencies>", "", body, flags=_re.S)
        body = _re.sub(r"<build>.*?</build>", "", body, flags=_re.S)
        own_a = _re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", body)
        if own_a:
            module_own.add(own_a.group(1))
        if i == 0:
            og = _re.search(r"<groupId>\s*([^<\s]+)\s*</groupId>", body)
            project_group = og.group(1) if og else None
            for m in _re.findall(r"<module>\s*([^<\s]+)\s*</module>", txt):
                module_own.add(m.rstrip("/").rsplit("/", 1)[-1])
        for blk in _re.finditer(r"<dependency>(.*?)</dependency>", txt, _re.S):
            # 复核 F2：剥 <exclusions>——exclusion 里的 artifactId 撞名会错配外层 group
            b = _re.sub(r"<exclusions>.*?</exclusions>", "", blk.group(1), flags=_re.S)
            a = _re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", b)
            if not a or a.group(1) != artifact_id:
                continue
            g = _re.search(r"<groupId>\s*([^<\s]+)\s*</groupId>", b)
            if g:
                candidates.append(g.group(1))
    third_party = sorted({c for c in candidates if c != project_group})
    if len(third_party) == 1:
        return third_party[0]
    if len(third_party) > 1:
        return None  # 互斥证据 → 存疑弃用（省略依赖，绝不猜）
    if project_group and artifact_id in module_own:
        return project_group  # 真 reactor 内部模块，工程 groupId 合法（无需依赖块证据）
    return None


def _plan_module_artifacts(plan) -> set[str]:
    """R67C-T2：plan 自己在建/声明的模块 artifactId 集——新模块 base pom 尚无 <module> 登记，
    坐标解析须认它们为 reactor 兄弟，否则裸名走 Central 反查→R53-1 误剔（round67c st-37 死型：
    ruoyi-alarm 被误剔→ruoyi-admin 不依赖 alarm→主 app 不 scan→运行期全 404）。来源：契约
    dependencies 各 entry 的 module ∪ 全子任务 create/writable 的物理模块根。栈中立（模块=物理
    路径，artifactId=dir 名，与 maven_registry index 的 <module> 口径一致）。"""
    mods: set[str] = set()
    sc = getattr(plan, "shared_contract", None)
    if isinstance(sc, dict):
        for e in (sc.get("dependencies") or []):
            if isinstance(e, dict):
                m = str(e.get("module") or "").strip().rstrip("/")
                if m:
                    mods.add(m.rsplit("/", 1)[-1])
    for st in (getattr(plan, "subtasks", None) or []):
        _sc = getattr(st, "scope", None)
        for f in (list(getattr(_sc, "create_files", None) or [])
                  + list(getattr(_sc, "writable", None) or [])):
            r = _code_module_root(str(f))
            if r:
                mods.add(r.rstrip("/").rsplit("/", 1)[-1])
    mods.discard("")
    return mods


def resolve_scaffold_artifacts(project_path: str | None, artifacts: list[str],
                               extra_module_artifacts: set[str] | None = None):
    """R53-1：契约 artifacts → 可写入 pom 的确定性坐标 (kept, dropped)。

    模板与验收标准必须**同源**：能解析的才进模板、才进契约/验收；解析不到的一并剔除。
    旧实现把依赖从模板里省略、验收却仍要求"声明契约全部 artifacts" → 自相矛盾 →
    worker 只能手写臆造坐标（round53 实锤：幻影 alarm-interface 无 version，Maven 连
    reactor 都读不出，全体 worker 构建闸 BLOCKED）。project_path 未知/解析器异常 →
    退回旧行为（全部省略，不阻断规划）。extra_module_artifacts=plan 新增模块（R67C-T2）。"""
    if not project_path:
        return [], list(artifacts)
    try:
        from swarm.brain.maven_registry import resolve_artifacts
        return resolve_artifacts(project_path, list(artifacts),
                                 extra_module_artifacts=extra_module_artifacts)
    except Exception as exc:  # 解析器/网络异常绝不阻断规划期
        logger.warning("[SCAFFOLD-TPL] R53-1 坐标解析不可用（%s）→ 退回省略旧行为", exc)
        return [], list(artifacts)


def _render_dep_block(dep) -> str:
    ver = (f"\n            <version>{dep.version}</version>" if dep.version else "")
    return (f"        <dependency>\n            <groupId>{dep.group}</groupId>\n"
            f"            <artifactId>{dep.artifact}</artifactId>{ver}\n"
            "        </dependency>")


def _root_gav(project_path: str | None) -> tuple[str, str, str] | None:
    """根 pom 自身 GAV（剥注释/parent/依赖后取坐标区）。继承 GAV 的根 → None（不猜）。"""
    if not project_path:
        return None
    import re as _re
    f = Path(project_path) / "pom.xml"
    if not f.is_file():
        return None
    txt = _re.sub(r"<!--.*?-->", "", f.read_text("utf-8", errors="replace"), flags=_re.S)
    head = _re.sub(r"<parent>.*?</parent>", "", txt, flags=_re.S)
    head = _re.sub(r"<dependencyManagement>.*?</dependencyManagement>", "", head, flags=_re.S)
    head = _re.sub(r"<dependencies>.*?</dependencies>", "", head, flags=_re.S)
    head = _re.sub(r"<build>.*?</build>", "", head, flags=_re.S)
    g = _re.search(r"<groupId>([^<]+)</groupId>", head)
    a = _re.search(r"<artifactId>([^<]+)</artifactId>", head)
    v = _re.search(r"<version>([^<]+)</version>", head)
    if not (g and a and v):
        return None
    return g.group(1).strip(), a.group(1).strip(), v.group(1).strip()


def _aggregator_pom_template(agg_dir: str, submodules: list[str],
                             project_path: str | None) -> str:
    """R57-4b：聚合父模块 pom（packaging=pom）的确定性模板。

    ★R57-7（推演揪出）★ 它的 GAV 必须是**可预测的**——因为子模块的 <parent> 要指向它：
    groupId = 根 groupId；artifactId = **聚合目录名**；version = 根 version。
    子模块 pom 的 relativePath 默认 `../pom.xml` 正好指到这里，GAV 一致 → Maven 解析得通。
    """
    gav = _root_gav(project_path)
    if not gav:
        return ""
    rg, ra, rv = gav
    # ★Task#4 复核治本★ 聚合器的 <parent> 必须是它的**直接上级**（relativePath ../pom.xml），
    # 不是无条件的根工程——嵌套聚合器（agg_dir 含 '/'）的 parent 是其**上级聚合目录**的
    # artifactId，只有顶层聚合器（无 '/'）的 parent 才是根。旧实现一律写根 GAV → 嵌套时
    # `../pom.xml` 指到的上级 artifactId 与之对不上 → round57 'wrong local POM' FATAL。
    # 与叶子/孤儿 pom 的 _pgav 计算同源（groupId/version 全工程统一 = 根的）。
    parent_art = agg_dir.rsplit("/", 1)[0].rsplit("/", 1)[-1] if "/" in agg_dir else ra
    mods = "\n".join(f"        <module>{m}</module>" for m in submodules)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<project xmlns="http://maven.apache.org/POM/4.0.0"\n'
        '         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        '         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 '
        'http://maven.apache.org/xsd/maven-4.0.0.xsd">\n'
        "    <modelVersion>4.0.0</modelVersion>\n"
        "    <parent>\n"
        f"        <groupId>{rg}</groupId>\n"
        f"        <artifactId>{parent_art}</artifactId>\n"
        f"        <version>{rv}</version>\n"
        "    </parent>\n"
        f"    <artifactId>{agg_dir.rsplit('/', 1)[-1]}</artifactId>\n"
        "    <packaging>pom</packaging>\n"
        "    <modules>\n"
        f"{mods}\n"
        "    </modules>\n"
        "</project>")


def _deterministic_pom_template(mod: str, artifacts: list[str],
                                project_path: str | None,
                                resolved: list | None = None,
                                parent_gav: tuple[str, str, str] | None = None,
                                extra_module_artifacts: set[str] | None = None) -> str:
    """R45-2：从根 pom parent GAV + 契约 artifacts 确定性生成模块 pom 模板。

    根 pom 不可解析/无 project_path → 返回空串（scaffold 退回旧行为，不假装精确）。
    R53-1：依赖坐标经 maven_registry 解析——父级（含 import BOM 传递闭包）受管 → 不写
    版本（写死会覆盖工程统一版本）；不受管 → **必须写显式版本**（无版本又无人管 = Maven
    连 reactor 都读不出，比缺依赖严重一个数量级）；解析不到 → 省略（调用方须同步剔除验收）。"""
    if not project_path:
        return ""
    try:
        import re as _re
        root_pom = Path(project_path) / "pom.xml"
        if not root_pom.is_file():
            return ""
        txt = root_pom.read_text("utf-8", errors="replace")
        # 复核 F2：先剥注释（注释里的历史坐标会赢过真坐标）；再剥 <parent> 防误取父级
        stripped = _re.sub(r"<!--.*?-->", "", txt, flags=_re.S)
        stripped = _re.sub(r"<parent>.*?</parent>", "", stripped, flags=_re.S)
        # 复核 F1：GAV 搜索限定在首个大区块之前（properties/dependencies/…里的
        # 坐标是依赖不是本工程）；根 pom 继承 GAV（缺 groupId/version）→ 如实 ""
        # fail-open——否则首个匹配会拼出幽灵 parent 坐标还盖"权威"章=确定性制造
        # round45 要防的 reactor 中毒
        m_blk = _re.search(
            r"<(properties|dependencies|dependencyManagement|build|modules|profiles)>",
            stripped)
        head = stripped[:m_blk.start()] if m_blk else stripped
        g = _re.search(r"<groupId>([^<]+)</groupId>", head)
        a = _re.search(r"<artifactId>([^<]+)</artifactId>", head)
        v = _re.search(r"<version>([^<]+)</version>", head)
        if not (g and a and v):
            return ""
        # R53-1：坐标解析统一走 maven_registry（基线证据 → reactor 模块 → Central 反查），
        # R47-2 铁律不变（绝不伪造工程 groupId），但不再"查不到就一律省略"——省略会让权威
        # 模板变空壳 pom，而验收标准仍要求声明全部依赖 → 逼 worker 手写臆造坐标。
        if resolved is None:
            # R67C-T2 防御纵深：回退解析也须认 plan 新模块（当前调用方均传 resolved= 故此路
            # 未达，但防未来重构者省略 resolved 时静默退回 pre-T2 行为=新模块误剔复现 st-37 死型）。
            resolved, _dropped = resolve_scaffold_artifacts(
                project_path, artifacts, extra_module_artifacts=extra_module_artifacts)
            if _dropped:
                logger.warning(
                    "[SCAFFOLD-TPL] 模块 %s 的 %d 个契约依赖无法解析坐标/版本 → 从模板省略"
                    "（调用方须同步从验收标准剔除）: %s", mod, len(_dropped), _dropped)
        deps_block = "\n".join(_render_dep_block(d) for d in resolved)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<project xmlns="http://maven.apache.org/POM/4.0.0"\n'
            '         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
            '         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 '
            'http://maven.apache.org/xsd/maven-4.0.0.xsd">\n'
            "    <modelVersion>4.0.0</modelVersion>\n"
            "    <parent>\n"
            # R57-7：子模块的 <parent> 必须是它**真实的上级 pom**（relativePath 默认 ../pom.xml）。
            # 住在聚合目录下却把 parent 写成根工程 → GAV 对不上 → Maven FATAL
            # 'parent.relativePath points at wrong local POM'（round57 实锤原文）。
            f"        <groupId>{(parent_gav or (g.group(1).strip(), a.group(1).strip(), v.group(1).strip()))[0]}</groupId>\n"
            f"        <artifactId>{(parent_gav or (g.group(1).strip(), a.group(1).strip(), v.group(1).strip()))[1]}</artifactId>\n"
            f"        <version>{(parent_gav or (g.group(1).strip(), a.group(1).strip(), v.group(1).strip()))[2]}</version>\n"
            "    </parent>\n"
            f"    <artifactId>{mod}</artifactId>\n"
            "    <packaging>jar</packaging>\n"
            "    <dependencies>\n"
            f"{deps_block}\n"
            "    </dependencies>\n"
            "</project>")
    except Exception:  # noqa: BLE001 — 模板生成 fail-open，scaffold 退回旧行为
        logger.warning("[SCAFFOLD-INJECT] pom 模板确定性生成失败（fail-open）", exc_info=True)
        return ""


# 标准源码布局段：它们是**布局**不是模块（Maven/Gradle: src/main/java；Cargo: src；Go: cmd/internal…）
_SRC_LAYOUT_SEGMENTS = frozenset({"src", "main", "java", "kotlin", "scala", "resources",
                                  "test", "tests", "webapp", "cmd", "internal", "pkg"})
_BUILD_MANIFESTS = ("pom.xml", "build.gradle", "build.gradle.kts", "Cargo.toml",
                    "go.mod", "package.json", "pyproject.toml")


# ★R64★ 辅助交付物扩展名（多栈通用）：DDL/文档/图片/脚本/纯配置——不参与构建 reactor、
# 不需要构建清单的文件类型。它们是逻辑归属模块的辅助交付物，绝不定义/扩张模块物理根。
# .xml/.yml/.properties 在 src 树内（mapper/应用配置）也归此类——无损：模块根由同树源码
# 文件给出；构建清单（pom.xml 等）在分类器里【先于】本表判定，不受影响。
_AUX_EXTENSIONS = frozenset({
    ".sql", ".ddl", ".md", ".markdown", ".rst", ".adoc", ".txt", ".csv", ".tsv",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".log",
    ".html", ".css", ".xml", ".yml", ".yaml", ".toml", ".properties", ".conf",
    ".ini", ".cfg", ".env", ".sh", ".bat", ".ps1",
})

# 证据分类（★R64 证据强度分层★，round64 死因 + 对抗双复核 4 条 CONFIRMED 一体整改）：
#   manifest  构建清单=「声明的构建根」本身：主张【所在目录】为根（逮"清单声明错目录"，
#             猎手 F3），不参与源码定根/前缀。
#   aux       辅助扩展名——【先于】布局段判定（'sql/main/x.sql' 的 'main' 撞布局段词表也
#             不得升格，猎手 F2）。有任何代码/清单证据时整类退位；纯辅助模块才回退顶层目录。
#   strong    含标准布局段的源码（<mod>/src/main/java/…）：根=_code_module_root；根级布局
#             （src/ 直居仓库根）根记 "."=仓库根本身，绝不静默丢证据（复核 CR-H1：丢了它，
#             "根级根+子目录根"的真双根违①会静默消失、G1 降级成 warn）。
#   weak_code 无布局段的其余文件（flat 布局真源码：web/App.js、svc/app.py）：根=顶层目录。
#             flat 源码是真代码不是辅助物，与 strong 同等参与歧义判定（猎手 F1：混合布局
#             真双根不得因"另一根恰好带布局段"被静默放行）。
_EV_MANIFEST, _EV_AUX, _EV_STRONG, _EV_WEAK_CODE = "manifest", "aux", "strong", "weak_code"


def _evidence_class(path: str) -> str:
    """单文件路径 → 证据类别（见上表）。round64 死因本体：顶层 sql/*.sql 经顶层目录回退
    被当第二物理根 → G1 三验三拒好 plan（issues 反馈"归到同一模块目录"对棕地顶层 sql
    惯例结构性不可满足 → LLM 永不收敛 → FAILED@PLAN）。多栈中立，不写死任何单一栈。"""
    p = _norm_scope_path(path)
    if p.endswith(_BUILD_MANIFESTS):
        return _EV_MANIFEST
    name = p.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    if dot > 0 and name[dot:].lower() in _AUX_EXTENSIONS:
        return _EV_AUX
    # ★R65E-T2（round65e4 死因物理层收口，#67）★ Maven/Gradle 源集资源根 `src/<sourceSet>/resources/**`
    # =打包不编译、永不定义/扩张构建单元的**资源根**——其下任意扩展名（含 `.js`/`.png`/未来类型）
    # 皆不主张模块物理根，整类退位（与 `.html`/`.css`/mapper `.xml` 同性质，只是它们靠扩展名表已
    # 命中、`.js` 等靠本规则统一收口，不再逐扩展名打地鼠）。死因：RuoYi feature 视图静态资源按
    # 框架必落 `ruoyi-admin` webapp 的 `src/main/resources/static/js` → 旧分类器撞布局段升 _EV_STRONG →
    # 误主张第二物理根 → G1 硬打回本可 build 的 plan（每个带 UI 的 feature 必撞）。
    # ★复核① CONFIRMED HIGH 整改★ 必须锚定 `resources` 为**源集根**（紧跟 `src/<sourceSet>/`，即
    # 相对某个 `src` 段偏移 +2），绝非"`src` 之后任意位置出现 `resources`"——否则名为 `resources` 的
    # 【包】里的真编译源码 `…/src/main/java/com/x/resources/Foo.java` 会被误降级为 aux、静默放行真跨模块
    # 违①（G1 存在的全部意义）。真 JS 工程源码 `web/src/App.js`（无 resources 段）本就不匹配、仍主张根。
    _dirs = p.split("/")[:-1]   # 末段是文件名，不参与
    _in_resource_root = any(
        seg == "src" and i + 2 < len(_dirs) and _dirs[i + 2] == "resources"
        for i, seg in enumerate(_dirs)
    )
    if _in_resource_root:
        return _EV_AUX
    if any(seg in _SRC_LAYOUT_SEGMENTS for seg in _dirs):
        return _EV_STRONG
    return _EV_WEAK_CODE


def _evidence_root(path: str, cls: str) -> str | None:
    """单文件路径 + 证据类别 → 它主张的模块物理根（None=不主张）。"""
    p = _norm_scope_path(path)
    if cls == _EV_MANIFEST:
        return p.rsplit("/", 1)[0] if "/" in p else None   # 根级清单=聚合器本体，不主张
    if cls == _EV_STRONG:
        r = _code_module_root(p)
        return r if r is not None else "."   # 根级布局：模块根=仓库根（CR-H1）
    if cls == _EV_WEAK_CODE:
        return p.split("/", 1)[0] if "/" in p else None
    return None   # aux：不主张（调用方对纯辅助模块另行回退顶层目录）


def _is_existing_baseline_module(
    project_path: str | None, rel: str, cache: dict[str, bool], base_ref: str | None,
) -> bool:
    """R65E-T1：`rel` 在【任务钉扎 base 树】里是否为一个既有物理构建单元（目录带构建清单）。

    ★猎手 B CONFIRMED 整改★ 必须读【钉扎 base】（`git cat-file -e <base_commit>:<rel>/<manifest>`）
    而非实时工作树——project_path 是每轮 merge 累积落盘的持久 git 树，若读实时磁盘，前一轮/前一
    任务已 merge 的新模块会被误当"既有基线"，让无关模块的 pom 编辑被静默剔除（round59 血泪的
    跨轮版：判据随磁盘态闪烁）。复用 _exists_in_repo 的 git-pin 口径，与 merge/worker/L2 全链一致。
    非 git → 退化 os.path.isfile（_exists_in_repo 内建）。base_ref=None → HEAD（零回归兜底）。"""
    if not rel:
        return False
    return any(
        _exists_in_repo(project_path, f"{rel}/{name}", cache, base_ref)
        for name in _BUILD_MANIFESTS
    )


def _module_physical_dirs(plan, project_path: str | None,
                          file_plan: list | None = None) -> dict[str, str]:
    """R57-1 + R57-4 合治：契约模块名 → 它在磁盘上的**真实物理目录**（多栈通用，不写死任何栈）。

    ★round57 头号杀手★ 旧实现把契约里的**模块名字面**当**物理路径**（`alarm-core` →
    根级 `alarm-core/pom.xml`），而计划里的代码其实全落在 `ruoyi-alarm/alarm-core/` 下 →
    两套口径分叉：脚手架在根级建 pom，验收命令 `mvn -pl ruoyi-alarm/alarm-interface` 却在
    reactor 里找不到项目 → 3 个子任务全灭 → 阶梯三保 build → **连坐放弃下游 69 个**。
    这正是 round44 的病根本体（契约【逻辑模块名】≠【物理目录】），当时只治了符号通道。

    **铁律：模块 = 物理路径，由计划的真实 scope 自证；契约里的模块名只是一个标签。**

    取证（证据必须独立于契约自身，否则是循环论证）：
      ① 计划里有子任务往 `…/<mod>/` 下写/建**非构建清单**文件（即真代码）→ 该目录就是物理落点；
      ② 基线里 `…/<mod>/` 是真实存在的目录（棕地既有模块）。
    命中**多个**不同物理目录（歧义）或**零个**（如 LLM 把 schema 占位符 `module`/`artifacts`
    抄成了模块名）→ **不返回**（fail-closed：绝不凭一个字符串在磁盘上造模块）。
    """
    # ★单一权威 resolver（Task#9 G1/G5）★ 解析 + 结构化诊断都走 `_resolve_module_dirs`；
    # 本函数只负责【告警 + 返回 resolved】（保持既有脚手架契约不变），G1 模块 coherence 闸
    # 消费同一份诊断打回——绝不再各自实现一套 module→dir 解析（那正是审计①的 forked-resolver 病根）。
    out, ambiguous, collision = _resolve_module_dirs(plan, project_path, file_plan)
    for mod, dirs in ambiguous.items():
        logger.warning(
            "[SCAFFOLD-INJECT] R57-4 模块名 %r 在计划里对应**多个**物理目录 %s → 歧义，"
            "拒绝脚手架（绝不猜落点：建错层级=reactor 找不到项目=整批子任务白跑）", mod, dirs)
    for d, mods in collision.items():
        logger.warning(
            "[SCAFFOLD-INJECT] R59-2 %d 个契约模块解析到**同一物理目录** %r：%s → 矛盾，"
            "全部拒绝脚手架（同一个 pom 不可能是两个模块的构建文件；带病继续会让写权击鼓传花、"
            "最终没人拥有它 → 全员 BLOCKED）", len(mods), d, mods)
    return out


def _resolve_module_dirs(
    plan, project_path: str | None, file_plan: list | None = None,
    *, base_ref: str | None = None, with_cross_res: bool = False,
):
    """★单一权威 module→物理目录 resolver + 结构化诊断（Task#9 G1/G5 单一事实源）★。

    返回 (resolved, ambiguous, collision)：
      - resolved: {契约模块名 → 唯一物理构建目录}（fail-closed，歧义/撞车者不在内）。
      - ambiguous: {模块 → [多个物理目录]}——名字匹配落到 2+ 目录且 file_plan/基线未消歧（违①）。
      - collision: {物理目录 → [2+ 模块]}——多个模块塌进同一目录（R59-2，违②）。

    证据独立于契约自身（否则循环论证）：计划里往 `…/<mod>/` 写**非构建清单**文件 + file_plan
    权威归属 + 基线既有目录。★关键修正（Task#9 双复核 CRITICAL）★ 模块名段只在【源码根之前】
    出现才算模块边界——`ruoyi-alarm/api/src/main/java/com/x/api/Foo.java` 里尾部包名 `api` 是
    **包**不是模块，扫到第一个源码布局段即停，绝不把包名当第二个物理目录（否则单模块惯例命名被
    误判成多落点、确定性打回好 plan=比 round59 更毒的死锁）。file_plan/基线是权威、覆盖名字匹配
    （R58-1）；歧义仅在权威未消解时才成立。
    """
    _want = {(e.get("module") or "").strip().rstrip("/")
             for e in ((getattr(plan, "shared_contract", None) or {}).get("dependencies") or [])
             if isinstance(e, dict)} - {""}
    # ★R64 证据强度分层（两通道同规则）★：代码证据（strong/weak_code）与辅助证据（aux）
    # 分桶收集；有代码证据的模块只看代码证据——aux 绝不给名字匹配通道造第二物理根
    # （`sql/<模块名>/…` 与 fp 通道的顶层 sql 是同族暗门，猎手 F2 一并封死）。flat 布局
    # 真源码（weak_code）与 strong 同等参与（猎手 F1）；纯辅助模块回退 aux 桶（不砍
    # flat/纯资源项目的唯一证据来源，silent-hunter #1 兜底不放水）。
    cands_code: dict[str, set[str]] = {}
    cands_aux: dict[str, set[str]] = {}
    for st in getattr(plan, "subtasks", []) or []:
        sc = getattr(st, "scope", None)
        files = (list(getattr(sc, "create_files", []) or [])
                 + list(getattr(sc, "writable", []) or []))
        for f in files:
            p = _norm_scope_path(f)
            if "/" not in p or p.endswith(_BUILD_MANIFESTS):
                continue   # 构建清单不算名字匹配证据（它正是我们要造的东西）
            bucket = (cands_aux if _evidence_class(p) == _EV_AUX else cands_code)
            parts = p.split("/")
            for i, seg in enumerate(parts[:-1]):    # 末段是文件名
                if seg in _SRC_LAYOUT_SEGMENTS:
                    break   # 模块名只可能在源码根之前；其后是包/目录段，不是模块边界
                if seg in _want:
                    bucket.setdefault(seg, set()).add("/".join(parts[:i + 1]))
    cands: dict[str, set[str]] = {}
    for m in set(cands_code) | set(cands_aux):
        code_dirs, aux_dirs = cands_code.get(m), cands_aux.get(m)
        cands[m] = code_dirs or aux_dirs or set()
        if code_dirs and aux_dirs and not (aux_dirs <= code_dirs):
            # 猎手 F5：证据退位必须可观测——否则"闸正确解析"与"闸静默扔了一桶矛盾证据"
            # 在日志上不可分，round64 族回归将再次只能靠第一性原理考古。
            logger.info("[R64-EVIDENCE] 模块 %r 名字匹配的辅助文件根 %s 不计入物理根判定"
                        "（代码证据根=%s）", m, sorted(aux_dirs - code_dirs), sorted(code_dirs))
    out: dict[str, str] = {}
    for mod, dirs in cands.items():
        if len(dirs) == 1:
            out[mod] = next(iter(dirs))
    if project_path:
        base = Path(project_path)
        for mod in _want:
            if mod and mod not in out and (base / mod).is_dir():
                out[mod] = mod   # 基线里真实存在的目录 = 真模块（棕地）
    # ★R58-1★ file_plan 是权威归属，覆盖名字匹配（契约 `alarm-admin` 实住 `ruoyi-admin/`）。
    # 同时按【每文件物理模块根】判 file_plan 自身是否把一个模块摊到多个物理根（违①）：源码布局
    # 走 `_code_module_root`，非标准布局（flat/纯脚本）退回顶层目录（栈中立，补 silent-hunter #1
    # 的 `_code_module_root`→None 静默漏判）。out 的解析口径保持 `_common_module_prefix` 不变
    # （脚手架行为零回归）——多根仅额外进 fp_ambiguous 供 G1 闸打回，不改 out。
    fp_ambiguous: dict[str, list[str]] = {}
    # ★R65E-T2 复核② CONFIRMED HIGH（silent-hunter）★ 资源/辅助文件退位【不主张物理根=按设计
    # 放行】，但退位一旦【消解掉一个本会成立的多根违①】必须【结构化可观测】（"降级可观测"铁律）：
    # 记录 {模块 → 落在其构建根之外的资源顶层目录}，由 G1 升为 result.warn（软、不阻断），供 #67
    # 语义/L1-L2 资源批核验消费——绝不让 .js 等资源的跨模块落点只剩一条 logger.info 湮没。
    cross_res: dict[str, list[str]] = {}
    _exist_cache: dict[str, bool] = {}   # R65E-T1：既有基线模块 git-pin 存在性缓存
    for mod, paths in _file_plan_module_paths(file_plan).items():
        # ★R64 证据强度分层★：按 _evidence_class 逐文件分类。物理根由代码证据
        # （strong/weak_code）+ 清单证据（manifest，主张清单所在目录——逮"清单声明错目录"）
        # 共同主张；aux（顶层 sql/docs/scripts 等辅助交付物）在存在任何其它证据时整类退位、
        # 绝不扩张物理根（round64 死因本体）。纯辅助模块（如 db-scripts 只含 sql）→ 回退
        # aux 顶层目录，保持原行为。
        by_cls: dict[str, list[str]] = {}
        for p in paths:
            by_cls.setdefault(_evidence_class(p), []).append(p)
        aux = by_cls.get(_EV_AUX, [])
        code_paths = by_cls.get(_EV_STRONG, []) + by_cls.get(_EV_WEAK_CODE, [])
        # 定根前缀优先代码证据；无代码证据（纯辅助/纯清单模块，如聚合父只带自己的 pom）
        # 回退全 paths——保持旧行为（_common_module_prefix 的末段=文件名不参与，清单路径
        # 能给出正确的模块目录前缀）。
        prefix = _common_module_prefix(code_paths or paths, project_path)
        if prefix:
            out[mod] = prefix
        _roots_cls: dict[str, set[str]] = {}
        _root_files: dict[str, list[tuple[str, str]]] = {}   # R65E14-T1：根→[(路径,证据类)]
        for p, cls in ((q, c) for c, ps in by_cls.items() for q in ps if c != _EV_AUX):
            r = _evidence_root(p, cls)
            if r:
                _roots_cls.setdefault(r, set()).add(cls)
                _root_files.setdefault(r, []).append((p, cls))
        _roots: set[str] = set(_roots_cls)
        if not _roots:   # 纯辅助模块：回退顶层目录（多根照打回，flat 兜底不放水）
            _roots = {q.split("/", 1)[0] for q in aux if "/" in q}
        elif aux:
            _aux_tops = {q.split("/", 1)[0] for q in aux if "/" in q} - _roots
            if _aux_tops:
                # 猎手 F5：退位必须可观测（fail-open 可观测铁律）。★R65E-T2 复核②★ _aux_tops 非空
                # + 存在代码根 = 退位消解掉了一个本会成立的多根违① → 记入 cross_res 供 G1 结构化 warn。
                logger.info("[R64-EVIDENCE] 模块 %r file_plan 的辅助文件根 %s 不计入物理根"
                            "判定（代码/清单证据根=%s）", mod, sorted(_aux_tops), sorted(_roots))
                cross_res[mod] = sorted(_aux_tops)
        # ★R65E-T1★ 改【既有外部基线模块】的构建清单 = 合法跨模块接线（单体 feature 把依赖
        # 注册进 app 壳的 pom，round65e 死因本体），绝不构成本模块跨物理 build 单元。只剔除
        # 【仅由 manifest 证据主张、且 ≠本模块、且是既有基线模块】的根——本模块把真源码
        # （strong/weak_code）落进外部模块仍保留为根（那是真跨模块 smell，不放行）；无
        # project_path 无法证实既有基线 → 保守不剔（fail-closed）；两个都是新落点仍歧义
        # （round62 alarm-api 保护不破）。
        if project_path and len(_roots) > 1:
            # ★R65E14-T1（#40）★ 豁免判据从"仅 manifest 证据"扩展为其超集："该根的【全部】
            # 证据文件均为 manifest 或【基线既有文件】（git-pin base，_exists_in_repo 与
            # R65E-T1 基线模块判定同源同缓存）"。MODIFY 既有外部源码文件（往既有 Shiro 链/
            # 路由表/DI 注册点写接线，round65e14 死因本体=admin 特性改 framework 的既有
            # ShiroConfig.java）与改既有外部 pom 是同一类合法 fan-in 接线——文件的家早已确定
            # （属外部模块），本模块只是去改它，不产生"本模块的家在哪"的歧义。CREATE 新文件
            # 进外部模块（不在 base）→ all() 不成立 → 根保留仍打回（新文件的家无法判=真
            # 跨模块 smell，round62 保护不破）。
            _foreign = {
                r for r in _roots
                if _is_existing_baseline_module(project_path, r, _exist_cache, base_ref)
                and all(
                    cls == _EV_MANIFEST
                    or _exists_in_repo(project_path, p, _exist_cache, base_ref)
                    for p, cls in (_root_files.get(r) or [("", "")])
                )
            }
            # ★复核①CONFIRMED★ 只在剔除后本模块仍保有【自有锚根】(_own 非空)时才剔——否则
            # =纯接线模块只改两个既有外部模块的 pom、无任何自有代码归属，本身就是真违①
            # （哪个目录是它的家无法判），必须保持歧义硬判，绝不静默降级成 zero-dir 软 warn。
            # （绝不用 `r != mod` 拿物理路径比契约标签——R58-1 二者常不等。）
            # ★猎手 A 已知边界（移交 #67）★ aux 资源（_EV_AUX：模板/mapper XML/yml）不主张根，
            # 故本模块把【自己的】资源误路由进外部既有模块（如 alarm 的 Mapper XML 落 ruoyi-admin）
            # 与【合法】的单体视图模板落 admin 壳，在物理目录层不可分——round65e 合法案必须放行
            # 模板→admin，无法在此拦误路由 mapper。资源↔模块【运行时绑定】coherence 是语义问题，
            # 属 #67（MyBatis mapper XML/资源批 L1-L2 盲区）本职，非 G1 物理 build-dir coherence 范畴。
            _own = _roots - _foreign
            if _foreign and _own:
                logger.info("[R65E-COHERENCE] 模块 %r 改既有外部模块 %s 的构建清单=合法接线，"
                            "不计入本模块物理根判定（本模块自有根=%s）",
                            mod, sorted(_foreign), sorted(_own))
                _roots = _own
                # ★R65E14-T1 猎手 Finding1（CONFIRMED HIGH）整改★ out[mod] 的 prefix 是在
                # 剔除 foreign 之前用全量 code_paths 算的——标签≠目录字面名时（R58-1：契约
                # `alarm-admin` 实住 `ruoyi-admin/`）跨 top 段 → prefix=None → out 无此模块；
                # 豁免又清了 ambiguous → 模块从 resolved/ambiguous 双双消失 → G1 zero-dir
                # 误诊"幻影依赖"软 warn → 脚手架/依赖推导拿不到根 → 执行期 reactor 死型
                # 静默复活。剔除后按 _own 内文件重算（口径同构：代码证据优先、无则回退全部；
                # file_plan 是权威，非空即覆盖名字匹配的旧值——R58-1 既有裁决）。
                _own_files = [(p, c) for r2 in _own for p, c in (_root_files.get(r2) or [])]
                _own_code = [p for p, c in _own_files if c != _EV_MANIFEST]
                _prefix2 = _common_module_prefix(
                    _own_code or [p for p, _ in _own_files], project_path)
                if _prefix2:
                    out[mod] = _prefix2
        if len(_roots) > 1:
            fp_ambiguous[mod] = sorted(_roots)
    # 违①：名字匹配落到 2+ 目录、且 file_plan/基线未把它消解到唯一目录 → 真歧义；file_plan 自身
    # 跨多物理根也是违①（即便 _common_module_prefix 给了浅公共前缀）。
    ambiguous = {m: sorted(d) for m, d in cands.items() if len(d) > 1 and m not in out}
    for m, roots in fp_ambiguous.items():
        ambiguous.setdefault(m, roots)
    # ★R59-2★ 违②：多个模块塌进同一物理目录 → fail-closed 全丢（同一 pom 不能属两模块）。
    _by_dir: dict[str, list[str]] = {}
    for m, d in out.items():
        _by_dir.setdefault(d, []).append(m)
    collision = {d: sorted(mods) for d, mods in _by_dir.items() if len(mods) > 1}
    for d in collision:
        for m in collision[d]:
            out.pop(m, None)
    # 默认 3-tuple（既有全部调用点/测试零改动）；with_cross_res=True 时附第 4 元
    # cross_res（{模块 → 构建根外资源顶层目录}）供 G1 结构化 warn——单一权威 resolver 出口，
    # 绝不让 validator fork 一套扫描（审计① forked-resolver 病根）。
    if with_cross_res:
        return out, ambiguous, collision, cross_res
    return out, ambiguous, collision


def _file_plan_module_paths(file_plan: list | None) -> dict[str, list[str]]:
    """file_plan → {模块名: [文件路径…]}（容忍 dict / 对象两种形态）。"""
    out: dict[str, list[str]] = {}
    for it in (file_plan or []):
        mod = (it.get("module") if isinstance(it, dict) else getattr(it, "module", None)) or ""
        path = (it.get("path") if isinstance(it, dict) else getattr(it, "path", None)) or ""
        mod, path = str(mod).strip().rstrip("/"), _norm_scope_path(path)
        if mod and path:
            out.setdefault(mod, []).append(path)
    return out


def _common_module_prefix(paths: list[str], project_path: str | None) -> str | None:
    """一组文件的**模块根目录** = 最长公共目录前缀，**切在标准源码布局之前**。

    ★R59-1（round59 死因，我自己的补丁造成的）★ 旧实现取"从根往下第一个**存在的**目录"——
    第一轮 worker 在磁盘上建出 `ruoyi-alarm/` 之后，replan 时它对**每个**子模块都返回聚合父
    `ruoyi-alarm` → 所有模块共用一个 pom 路径 → R57-6 收回写权变成**击鼓传花**
    → 聚合父脚手架失去自己的 pom 写权 → 根 pom 注册了 ruoyi-alarm 但该 pom 从未被建
    → `清单注册的模块在树里不存在` → **全员 BLOCKED**。
    **这是状态依赖 bug：第一轮跑不出来，replan 才炸。** 判据绝不能依赖"目录存不存在"。

    `ruoyi-alarm/alarm-common/src/main/java/…` + `ruoyi-alarm/alarm-common/src/main/resources/…`
      → 公共前缀 `ruoyi-alarm/alarm-common/src/main` → 切在 `src` 前 → **`ruoyi-alarm/alarm-common`**
    """
    if not paths:
        return None
    segs = [p.split("/") for p in paths]
    common: list[str] = []
    for i in range(min(len(x) for x in segs) - 1):     # 末段是文件名，不参与
        col = {x[i] for x in segs}
        if len(col) != 1:
            break
        common.append(next(iter(col)))
    # 切在标准源码布局之前——它们是**布局**不是模块（多栈通用：Maven/Gradle/Cargo/Go 皆然）
    for i, seg in enumerate(common):
        if seg in _SRC_LAYOUT_SEGMENTS:
            common = common[:i]
            break
    return "/".join(common) if common else None


def _code_module_root(path: str) -> str | None:
    """一个文件路径 → 它所属的**物理模块根目录**（切在标准源码布局之前），与
    `_common_module_prefix` 同口径但作用于**单个文件**：

      `ruoyi-alarm/alarm-core/src/main/java/X.java` → `ruoyi-alarm/alarm-core`

    构建清单（pom.xml 等）不算证据（那正是脚手架要造的东西）；根级源码（`src/...`，
    模块根为空串）→ None（根模块不是聚合子模块）；找不到源码布局段（无法判定模块
    边界，多栈通用）→ None（fail-closed：绝不凭一个字符串在磁盘上切出模块）。
    """
    p = _norm_scope_path(path)
    if not p or "/" not in p or p.endswith(_BUILD_MANIFESTS):
        return None
    parts = p.split("/")
    for i, seg in enumerate(parts):
        if seg in _SRC_LAYOUT_SEGMENTS:
            root = "/".join(parts[:i])
            return root or None
    return None


# DR-PM66-C1(#110)/DR-09-F1(#101)：JVM 家族【类路径共享命名空间】布局目录。只有 src/.../{java,
# kotlin,scala,groovy}/ 之下的包路径才构成"全类路径唯一"的 FQN——同 FQN 跨模块 = split-package/
# 副本遮蔽（类路径上互相遮蔽、消费方解析到错误副本），全局 reactor 编译必炸。Go/Rust/Python/Node
# 的 import 由模块/crate/包限定，同相对路径落不同模块合法。故靠【布局目录集数据驱动】判定，绝不写死
# 语言逻辑（CLAUDE.md 铁律①：栈相关行为走 registry/driver 分发）。
_CLASSPATH_NS_LAYOUT_DIRS = frozenset({"java", "kotlin", "scala", "groovy"})
# 每模块/每包各自一份、跨模块同相对路径【合法】的 JVM 描述符——绝不当作重复类误杀。
_PER_MODULE_JVM_DESCRIPTORS = frozenset({"module-info.java", "package-info.java"})


def classpath_fqn_key(path: str) -> tuple[str, str] | None:
    """create_file 路径 → (物理模块根, 包限定 FQN 相对路径)，**仅**对 JVM 类路径共享命名空间源码。
    非 JVM 布局 / 无法定模块根 / 无包路径（默认包·根级描述符）/ per-module 描述符 → None（不判）。
    #110（validate REJECT 闸）与 #101（确定性去冲突归一）共用此单一口径，栈中立。"""
    mod = _code_module_root(path)
    if not mod:
        return None                        # 无物理模块根（根级/无布局段）→ 无跨模块可言
    parts = _norm_scope_path(path).split("/")
    try:
        i = next(idx for idx, seg in enumerate(parts) if seg in _SRC_LAYOUT_SEGMENTS)
    except StopIteration:
        return None
    j, saw_ns_lang = i, False              # 跳过 module_root 之后的布局段游程，要求含 JVM 语言目录
    while j < len(parts) and parts[j] in _SRC_LAYOUT_SEGMENTS:
        if parts[j] in _CLASSPATH_NS_LAYOUT_DIRS:
            saw_ns_lang = True
        j += 1
    if not saw_ns_lang:
        return None                        # 非 JVM 类路径命名空间（Go/Rust/Py flat 或资源）→ 不判
    fqn_parts = parts[j:]                   # 包路径 + 文件名
    if len(fqn_parts) < 2 or fqn_parts[-1] in _PER_MODULE_JVM_DESCRIPTORS:
        return None                        # 无包（默认包）或 per-module 描述符 → 不判（避免误伤）
    return mod, "/".join(fqn_parts)


def _physical_code_module_dirs(plan, file_plan: list | None = None) -> set[str]:
    """★Task#4 治本★ 计划里**实际收码**的全部物理模块根目录——聚合器 <modules> 完整性的权威。

    与 `_module_physical_dirs` 的分工必须分清：后者按【契约模块名】求落点、对歧义/撞车
    fail-closed（宁缺毋滥——它决定"给谁建带契约依赖的 pom / 收谁的写权"）；本函数只回答
    "哪些目录里真的落了代码"，**不做名字匹配、不 fail-closed**。

    为什么聚合器 <modules> 必须用它而不是 `_module_physical_dirs`（round62 真断）：Maven 只会
    下钻**登记在父 <modules> 里的**子模块。一个收了码、拿了 pom、但**契约模块名解析被 fail-closed
    拒掉**（歧义/撞车/占位符）的物理模块，若不进父 <modules> → mvn 根本不构建它 → **静默丢模块**
    （无任何报错的 round62 级真断）。登记一个真实收码目录永远安全（它本就要 build）——这正是
    "少登记=灾难、多登记=无害"的非对称，故此处用**完整物理证据**，不用 fail-closed 的名字映射。
    """
    out: set[str] = set()
    _unrooted: set[str] = set()

    def _consider(f: str) -> None:
        p = _norm_scope_path(f)
        if not p:
            return
        d = _code_module_root(f)
        if d:
            out.add(d)
        elif "/" in p and not p.endswith(_BUILD_MANIFESTS):
            _unrooted.add(p.rsplit("/", 1)[0])   # 无源码布局段 → 记其所在目录，待覆盖判定

    for st in getattr(plan, "subtasks", []) or []:
        sc = getattr(st, "scope", None)
        for f in (list(getattr(sc, "create_files", None) or [])
                  + list(getattr(sc, "writable", None) or [])):
            _consider(f)
    for it in (file_plan or []):
        path = (it.get("path") if isinstance(it, dict) else getattr(it, "path", None)) or ""
        _consider(str(path))
    # ★Task#4 复核（silent-failure-hunter）★ 收码路径**无标准源码布局段**（非 src/main/... 布局，
    # 如 flat 布局或纯 sql/config 目录）→ `_code_module_root` 无法确定物理模块根、未纳入 phys。
    # 若该目录也不在任何已解析模块之下 = 潜在**漏登记的独立模块** → LOUD 提示（绝不静默隐形）。
    # named 模块（契约/file_plan）另由 `_module_physical_dirs` 覆盖、不依赖本通道，故此仅提示非致命。
    _uncovered = sorted(d for d in _unrooted
                        if not any(d == o or d.startswith(o + "/") for o in out))
    if _uncovered:
        logger.warning(
            "[SCAFFOLD-INJECT] Task#4 %d 个收码目录无标准源码布局段、无法确定物理模块根 → 未纳入聚合器 "
            "<modules> 完整性判定；若本应是独立 maven 模块则可能漏登记（named 模块由契约/file_plan 通道"
            "覆盖、不受影响）：%s", len(_uncovered), _uncovered[:8])
    return out


def prune_contract_dependencies(plan, project_path: str | None) -> dict[str, list[str]]:
    """T6①（round63 治本）：契约依赖剪除**同源传播**。

    round63 死因（cassette 实锤）：R53-1 的"模板/契约/验收三处同源剔除"只实现在**脚手架
    子任务**自己的三处；shared_contract.dependencies 本身从未被剪，normalize 规则5 的验收
    note 又用未解析原始 artifacts → st-5 验收标准要求含被剪 spring-boot-starter-aop 的
    20 项而权威模板只有 19 项（"缺一即整模块 mvn compile 失败"）＝结构性逼 worker 复入。

    治：PLAN 期（脚手架注入前）统一 resolve 一次：entry.artifacts 回写为 kept（保留原
    spec 字符串，幂等），dropped 落 shared_contract["pruned_artifacts"]（{module: [spec…]}
    持久账本——随 D51 契约下发，worker 可见"这些坐标已证不可解析"的负面知识）。此后
    模板（resolve kept）/规则5 验收（读已剪 entry）/worker 契约三面天然同源。

    ★不禁 worker 复入★：防线④按**真实 import + Central FQCN 反查**注入是比规划期解析
    更强的证据（受管不写版本/不受管取稳定版），是误剪的救生索——"禁复入"字面执行会把
    解析器误剪变成永久缺依赖死锁。fail-open：project_path 缺/解析器异常 → 不动契约
    （绝不把"解析器坏了"当"全部不可解析"剪空契约）+ WARNING。返回本次剪除。
    """
    sc = getattr(plan, "shared_contract", None)
    if not project_path or not isinstance(sc, dict):
        return {}
    deps = sc.get("dependencies")
    if not isinstance(deps, list) or not deps:
        return {}
    # hunter#F1（HIGH，实证）：先**全部解析进暂存区**、零变异；任何 entry 抛异常 → 整批
    # 放弃（契约真·保持原样）。旧写法边解析边就地剪，第 3 个 entry 抛异常时前 2 个已永久
    # 变异，except 却谎报"保持原样"——T4 hunter#1 同型半应用反模式。
    staged: list[tuple[dict, list, str, list[str]]] = []
    total_arts = 0
    total_dropped = 0
    _pma = _plan_module_artifacts(plan)   # R67C-T2：plan 新模块认作 reactor 兄弟，不误剔
    try:
        from swarm.brain.maven_registry import resolve_artifacts
        for entry in deps:
            if not isinstance(entry, dict):
                continue
            mod = str(entry.get("module") or "").strip().rstrip("/")
            arts_now = [a for a in (entry.get("artifacts") or []) if a]
            # 复核 MED（跨轮破坏性别名）：plan.shared_contract 与 state["shared_contract_draft"]
            # 同对象，replan 不再生契约——若直接从已剪 artifacts 重解析，单次瞬时误剪将永久
            # 不可复议。每轮都从 artifacts_pre_prune（首轮快照的原始清单）重解析：瞬时误剪
            # 在解析器恢复后的下一轮自动复原。
            orig = [a for a in (entry.get("artifacts_pre_prune") or []) if a] or arts_now
            if not mod or not orig:
                continue
            total_arts += len(orig)
            _kept, dropped = resolve_artifacts(project_path, list(orig),
                                               extra_module_artifacts=_pma)
            _dropped_set = {str(d).strip() for d in dropped}
            total_dropped += len(_dropped_set)
            if _dropped_set:
                staged.append((entry,
                               [a for a in orig if str(a).strip() not in _dropped_set],
                               mod, sorted(_dropped_set)))
            elif arts_now != orig:
                # 历史轮剪过、本轮全部可解析 → 从原始清单整体复原（瞬时误剪自愈）
                staged.append((entry, list(orig), mod, []))
    except Exception:  # noqa: BLE001 — 解析器异常绝不误剪契约（暂存未提交=真·原样）
        logger.warning("[T6] 契约依赖同源剪除失败（fail-open：暂存未提交，契约真·保持原样；"
                       "模板侧照旧按 R53-1 剪模板——两面本轮暂不同源，验收或仍含不可解析项）",
                       exc_info=True)
        return {}
    if not staged:
        return {}
    # hunter#F2：断网/解析器退化时 registry 查无静默返回 None，与"真不可解析"不可区分。
    # dropped 占比>50% 且绝对量≥3 → 判解析器退化，整批拒剪+WARNING（绝不把网络故障当
    # "不可解析"永久烧进权威契约；占比思路同 SWARM_CONTRACT_MISSING_RATIO）。
    if total_dropped >= 3 and total_arts and total_dropped / total_arts > 0.5:
        logger.warning(
            "[T6] 待剪 %d/%d（>50%%）个契约依赖，疑解析器退化/断网 → 本轮拒绝同源剪除"
            "（绝不把网络故障当'不可解析'永久剪进权威契约），模板侧仍按 R53-1 剪模板",
            total_dropped, total_arts)
        return {}
    pruned_now: dict[str, list[str]] = {}
    for entry, _new_arts, mod, _dlist in staged:
        if "artifacts_pre_prune" not in entry:
            entry["artifacts_pre_prune"] = [a for a in (entry.get("artifacts") or []) if a]
        entry["artifacts"] = _new_arts
        ledger = sc.setdefault("pruned_artifacts", {})
        if _dlist:
            ledger[mod] = _dlist          # 按轮重建（复议语义），不跨轮累积陈旧项
            pruned_now[mod] = _dlist
        else:
            ledger.pop(mod, None)          # 本轮全部可解析 → 撤账（瞬时误剪自愈）
            logger.info("[T6] 模块 %s 历史轮被剪依赖本轮全部可解析 → 契约从原始清单复原", mod)
    if not sc.get("pruned_artifacts"):
        sc.pop("pruned_artifacts", None)
        sc.pop("pruned_artifacts_note", None)
    if not pruned_now:
        return {}
    # hunter#F3：账本随 D51 契约 JSON 进 worker prompt——裸 {module:[spec]} 对 LLM 语义
    # 歧义（可能被读成"要声明的清单"）。就地自释义，钉死负面知识框定。
    sc.setdefault(
        "pruned_artifacts_note",
        "pruned_artifacts 中的坐标已证无法确定性解析，已从模板与验收标准剔除；请勿在构建"
        "清单手写声明它们——若源码确实需要，构建修复会按真实 import 反查合法坐标补入")
    logger.warning(
        "[T6] R53-1 剪除同源传播：%d 个模块的不可解析契约依赖已从 shared_contract 剪除"
        "并记入 pruned_artifacts 账本（模板/验收/worker 契约同源，消除'验收逼 worker "
        "复入'——round63 st-5 死型）: %s", len(pruned_now), pruned_now)
    # ── R67C-T5（round67c 开箱）：剔除同源传播到【消费方子任务 desc】──────────────
    # round67c st-27-4 死型：com.github.submail:submail 被剔（pom/验收已同源剔），但 st-27-*
    # 父 desc 仍字面"VoiceNotifyService（Submail 语音 API 拨打）"→worker 被 desc 逼着用已剔
    # 依赖→import 不存在的类编译失败/臆造坐标。pruned_artifacts_note 只随契约进 prompt、不点名
    # 子任务；此处把负面知识下推到【提及被剔坐标的具体子任务 desc】，与 note 同措辞。
    # ★hunter 二轮整改（入口对称自愈）★：不用"已有标记就跳过"的单向幂等——那样某 artifact 复原后
    # 旧通告会永久粘滞、falsely 劝 worker 别用一个已合法的坐标（违 ledger.pop 撤账的自愈对称）。改
    # 【先剥旧标记→按本轮 pruned_tokens 重算】：本轮仍剔的重新贴、本轮已复原的自动撤，幂等且自愈。
    # 注：本块仅在 pruned_now 非空（有剔除）时到达（上方 `if not pruned_now: return {}` 早退）；
    # 全部复原致 pruned_now 空的整撤由 ledger.pop + pruned_artifacts_note 清除覆盖（今 latent：
    # replan 每轮重生子任务对象、desc 不跨轮；未来 retry 复用子任务对象时本自愈即 live 生效）。
    _pruned_tokens = set()
    for _specs in pruned_now.values():
        for _sp in _specs:
            _s = str(_sp).strip()
            if _s:
                _pruned_tokens.add(_s.split(":")[1] if ":" in _s else _s)  # artifactId
    _pruned_tokens.discard("")
    _T5_MARK = "【R67C-T5 依赖剔除通告】"

    def _strip_t5_mark(_d: str) -> str:
        _p = _d.find(_T5_MARK)
        if _p == -1:
            return _d
        _ls = _d.rfind("\n", 0, _p)
        _ls = _ls if _ls != -1 else _p
        _le = _d.find("\n", _p)
        _le = _le if _le != -1 else len(_d)
        return (_d[:_ls] + _d[_le:]).rstrip()

    _annotated = []
    for st in (getattr(plan, "subtasks", None) or []):
        _d0 = getattr(st, "description", None) or ""
        _d = _strip_t5_mark(_d0)                           # 先剥旧通告（自愈撤销起点）
        _hit = sorted(t for t in _pruned_tokens if t.lower() in _d.lower())
        if _hit:
            st.description = _d + (
                f"\n{_T5_MARK}以下契约依赖无法确定性解析坐标、已从构建与验收剔除：{_hit}。"
                "本子任务若提及它们，请勿 import/声明，改用已在册的可用栈（如 okhttp 直连 "
                "REST）实现或留占位说明，绝不臆造坐标。")
            _annotated.append(getattr(st, "id", "?"))
        elif _d != _d0:
            st.description = _d                            # 剥了旧通告本轮无命中→落剥后的（撤销）
    if _annotated:
        logger.info(
            "[T5-DESC] R67C-T5 剔除通告下推 %d 个子任务 desc（消费方散文仍提及被剔坐标 %s，"
            "防 worker 被 desc 逼用已剔依赖）: %s",
            len(_annotated), sorted(_pruned_tokens), _annotated)
    return pruned_now


def prune_baseline_absent_dependencies(plan, project_path: str | None) -> dict[str, list[str]]:
    """R65E10-T2（round65e10 死因②·源头正确方向）：基线【明确无 Lombok】时，从契约依赖剥除
    lombok 坐标——防 H1 权威 pom 模板据契约 artifacts 把 lombok 注入 pom，撞 T5 grounding 派生的
    验收 `! grep -rq 'lombok' <module-dir>/`（禁令是【正确侧】：基线 0 lombok）→ 每轮确定性不可赢
    →st-1 head-of-line 连坐全 92（本轮实证）。

    正确方向：剥【错侧】(lombok 进 pom)、保【对侧】(禁令验收)——交付与基线约定一致（手写 getter），
    禁令继续守"代码不得用 lombok"。必须跑在模板生成前（同 prune_contract_dependencies 咽喉）。

    栈中立：仅当 baseline_lombok_present 明确返回 False 才剥（Java 特有信号，其他栈恒 None/无 lombok
    坐标=no-op）。fail-open：无法判定基线（None）/无契约/异常 → 不剥（绝不误删真在用 lombok 致编译
    断裂）。与 prune_contract_dependencies 同律留 artifacts_pre_prune 快照（瞬时可复原）。
    返回 {module: [dropped_spec…]}。"""
    sc = getattr(plan, "shared_contract", None)
    if not project_path or not isinstance(sc, dict):
        return {}
    deps = sc.get("dependencies")
    if not isinstance(deps, list) or not deps:
        return {}
    try:
        from swarm.brain.stack_detect import baseline_lombok_present
        _present = baseline_lombok_present(project_path)
    except Exception:  # noqa: BLE001 — 探测异常绝不误剪
        _record_degrade_safe("brain.contract.lombok_baseline_undeterminable")
        logger.warning("[R65E10-T2] 基线 lombok 探测异常（fail-open，不剥）", exc_info=True)
        return {}
    if _present is None:
        # ★复核 MED★ 无法判定（无构建清单/walk 截断/探测异常）≠ 确定无 lombok——record_degrade
        # 令"探测失败静默不剥→死因可能复发"在 /api/metrics 可分（sibling record_degrade 约定）。
        _record_degrade_safe("brain.contract.lombok_baseline_undeterminable")
        logger.info("[R65E10-T2] 基线 lombok 在位性无法判定（fail-open 保守不剥，绝不误删真在用）")
        return {}
    if _present is True:
        return {}  # 基线真在用 lombok → 保留（合法 no-op，静默）
    # lombok 判据（复核）：与验收 `! grep -rq 'lombok'` 同语义——大小写不敏感【子串】匹配，
    # 涵盖 org.projectlombok:lombok / 裸 lombok / 任何含 'lombok' 的坐标（如 com.foo:lombok-utils，
    # 其字面进 pom 同样触发禁令）。基线 0 lombok 时这些都不该在 pom，与禁令口径一致。
    def _is_lombok(spec: str) -> bool:
        return "lombok" in str(spec or "").lower()

    # ★复核 HIGH★ 本函数【必须跑在 prune_contract_dependencies 之前】，且把 lombok 从
    # artifacts_pre_prune（若前轮已建）也剥掉——否则 prune_contract_dependencies 的"历史轮被剪
    # 本轮全可解析→从 artifacts_pre_prune 复原"分支会把 lombok（可解析坐标）复活，静默抵消本剥除
    # （hunter 实证再入轮复活）。故不自建含 lombok 的快照，只【清理】既有快照，令复原源永久无 lombok。
    dropped_all: dict[str, list[str]] = {}
    for entry in deps:
        if not isinstance(entry, dict):
            continue
        mod = str(entry.get("module") or "").strip().rstrip("/")
        arts = [a for a in (entry.get("artifacts") or []) if a]
        _drop = [a for a in arts if _is_lombok(a)]
        _pre = entry.get("artifacts_pre_prune")
        _pre_has = isinstance(_pre, list) and any(_is_lombok(a) for a in _pre)
        if not _drop and not _pre_has:
            continue
        entry["artifacts"] = [a for a in arts if not _is_lombok(a)]
        if isinstance(_pre, list):   # 前轮快照同步清 lombok，杜绝下游复原源复活
            entry["artifacts_pre_prune"] = [a for a in _pre if not _is_lombok(a)]
        if _drop:
            dropped_all[mod or "?"] = _drop
    if dropped_all:
        logger.warning(
            "[R65E10-T2] 基线无 Lombok（磁盘实证）→ 从契约剥除 lombok 坐标 %s"
            "（防 pom 模板注 lombok 撞 `! grep -rq lombok` 禁令=round65e10 st-1 考卷矛盾死因②；"
            "交付手写 getter 与基线一致）", dropped_all)
    return dropped_all


def _baseline_module_artifact(root: Path, mod_dir: str) -> str | None:
    """T5：磁盘上既有模块是否**可被依赖**（Maven 注入层过滤）。可依赖 → 返回其 artifactId。

    不可依赖三类：无 pom（非 Maven 模块，注入层不认）；packaging=pom/war（聚合父/война包
    不能上 classpath）；含 spring-boot-maven-plugin（repackage 后的可执行 fat-jar 不是库，
    依赖它必炸——round63 佐证：ruoyi-alarm 子任务 readable 里有 ruoyi-admin 样例文件，
    盲注会把可执行件拖进依赖）。纯文本确定性解析，解析失败按不可依赖（宁缺勿滥）。"""
    import re as _re
    pom = root / mod_dir / "pom.xml"
    try:
        if not pom.is_file():
            return None
        txt = _re.sub(r"<!--.*?-->", "", pom.read_text("utf-8", errors="replace"), flags=_re.S)
        m_pkg = _re.search(r"<packaging>([^<]+)</packaging>", txt)
        if m_pkg and m_pkg.group(1).strip().lower() in ("pom", "war", "ear"):
            logger.debug("[T5] 模块 %s packaging=%s → 不可被依赖（聚合父/打包件，设计内过滤）",
                         mod_dir, m_pkg.group(1).strip())
            return None
        if "spring-boot-maven-plugin" in txt:
            logger.debug("[T5] 模块 %s 含 spring-boot-maven-plugin（可执行 fat-jar）→ 不可被依赖",
                         mod_dir)
            return None
        head = _re.sub(r"<parent>.*?</parent>", "", txt, flags=_re.S)
        m_a = _re.search(r"<artifactId>([^<]+)</artifactId>", head)
        return m_a.group(1).strip() if m_a else None
    except OSError as e:
        # hunter#F3："读不到"≠"确认不可依赖"——瞬时 IO 失败会让真实基线库从模板里静默消失
        # （round63 症状复现且无痕）。WARNING 区分于上面的设计内 debug 过滤。
        logger.warning("[T5] 模块 %s 的 pom 读取失败（%s）→ 本轮视为不可依赖，模板可能缺真实"
                       "基线依赖（交 worker L1 防线④兜底）", mod_dir, e)
        return None


def _reverse_internal_edge_producer(plan, root: Path, consumer_writer_ids: set[str],
                                    target_dir: str) -> str | None:
    """R65D-T2② 判据（round65d st-26 反向边死型）：目标模块 pom 不在磁盘、且它的
    plan 生产者【传递依赖】消费方的写者 → 消费方构建时目标 pom 必然还不存在
    （成环形态），注入该依赖=确定性制造不可解析坐标。命中返回生产者 id，否则 None。
    正向证据才剪：无 pom 生产者/基线已有 pom 一律放行，绝不误杀合法单向依赖。"""
    d = (target_dir or "").strip("/")
    if not d or (root / d / "pom.xml").is_file():
        return None
    pom = f"{d}/pom.xml"
    prod = None
    for st in (getattr(plan, "subtasks", None) or []):
        sc = getattr(st, "scope", None)
        owns = [_norm_scope_path(str(f)) for f in
                (list(getattr(sc, "create_files", None) or [])
                 + list(getattr(sc, "writable", None) or []))]
        if pom in owns:
            prod = st.id
            break
    if prod is None or not consumer_writer_ids:
        return None
    deps_of = {st.id: list(getattr(st, "depends_on", []) or [])
               for st in (getattr(plan, "subtasks", None) or [])}
    seen: set[str] = set()
    stack = [prod]
    while stack:
        for dep in deps_of.get(stack.pop(), []):
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return prod if (seen & consumer_writer_ids) else None


def _prune_reverse_contract_internal_deps(plan, dirs: dict[str, str],
                                          project_path: str | None) -> None:
    """R65D-T2② 配套（猎手 HIGH）：反向边剪除同样适用于【契约自声明】的兄弟模块依赖。

    只剪推导通道不够——round63 观察是"契约从不声明内部依赖"，但一旦 LLM 在
    shared_contract 里直接声明反向兄弟依赖，_merge_internal_deps 以契约优先只去重
    不剪环，st-26 死型换个通道原样复活。判据与推导通道同源
    （_reverse_internal_edge_producer），剪除响亮留痕。fail-open：无证据不动。"""
    if not project_path or not dirs:
        return
    root = Path(project_path)
    shared = getattr(plan, "shared_contract", None)
    entries = shared.get("dependencies") if isinstance(shared, dict) else None
    if not isinstance(entries, list):
        return
    sib_by_key: dict[str, str] = {}
    for n, d in dirs.items():
        sib_by_key[(d or "").rstrip("/").rsplit("/", 1)[-1]] = n
        sib_by_key[n] = n
    writers_of: dict[str, set[str]] = {}
    for st in (getattr(plan, "subtasks", None) or []):
        sc = getattr(st, "scope", None)
        for f in (list(getattr(sc, "create_files", None) or [])
                  + list(getattr(sc, "writable", None) or [])):
            p = _norm_scope_path(str(f))
            for n, d in dirs.items():
                dd = (d or "").strip("/")
                if dd and (p == dd or p.startswith(dd + "/")):
                    writers_of.setdefault(n, set()).add(st.id)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        m = (entry.get("module") or "").strip().rstrip("/")
        if m not in dirs:
            continue
        arts = [a for a in (entry.get("artifacts") or []) if a]
        kept: list[str] = []
        for a in arts:
            parts = str(a).split(":")
            art_id = (parts[1] if len(parts) > 1 else parts[0]).strip()
            target = sib_by_key.get(art_id)
            if target and target != m:
                prod = _reverse_internal_edge_producer(
                    plan, root, writers_of.get(m, set()), dirs.get(target) or "")
                if prod is not None:
                    logger.warning(
                        "[R65D-T2] 契约声明的反向内部依赖剪除：%s → %s（artifact=%s，"
                        "目标 pom 生产者 %s 在消费方传递下游=成环死型；只剪推导通道"
                        "会让 st-26 换契约通道复活）", m, target, a, prod)
                    continue
            kept.append(a)
        if len(kept) != len(arts):
            entry["artifacts"] = kept


def derive_internal_module_deps(plan, dirs: dict[str, str],
                                project_path: str | None) -> dict[str, list[str]]:
    """T5（round63 治本）：从子任务跨模块 readable 证据确定性推导**内部模块依赖**。

    round63 死因：模板 <dependencies> 唯一来源=契约 LLM 自声明的第三方 artifacts，契约从不
    产内部模块依赖 → st-5 权威模板缺 ruoyi-common → 首波 30+ 次"程序包 com.ruoyi.common.
    core.domain 不存在"。而推导证据 plan 里现成（ruoyi-alarm 子任务 readable→ruoyi-common
    code 文件 108 次）从无人消费。

    推导（证据面栈中立）：模块 M 的子任务（写文件落在 dirs[M] 下）的 readable 里出现
    **其他模块的 code 文件** = M 编译需要该模块。落点两类：
    - plan 模块（dirs 有映射）：磁盘已有 pom → 按基线模块过滤；尚无 pom（新兄弟）→ 显式
      `group:模块名:${project.version}`（group 取根 GAV；模板 artifactId=契约模块名同源）。
      互指（A↔B 都有对方证据）=注入必成环 → 双向跳过+WARNING（更深计划错，交结构闸）。
    - 基线目录（顶段含构建清单）：经 _baseline_module_artifact 过滤（无 pom/聚合父/war/
      spring-boot 可执行件不注入）。基线绝不依赖新模块（它出现时新模块还不存在）→ 无环。
    返回 {契约模块: [artifact spec…]}；注入点合并进契约 artifacts 后走既有
    resolve_scaffold_artifacts（坐标解析单一权威，R53-1）。fail-open：无 project_path → {}。
    """
    if not project_path or not dirs:
        return {}
    root = Path(project_path)
    from swarm.brain.symbol_provenance import _is_code_path  # T4 单一 code 判定源
    plan_dir_of = {m: (d or "").strip("/") for m, d in dirs.items() if d}
    if not plan_dir_of:
        return {}

    # 复核 MED：**最长目录优先**——嵌套布局（"mods" 与 "mods/alarm" 同为 plan 模块）下
    # 首配会把内层文件误归外层模块，证据错挂/互指误剪。按目录长度降序保证最具体者赢。
    _ordered_dirs = sorted(plan_dir_of.items(), key=lambda kv: -len(kv[1]))

    def _owner_mod(p: str) -> str | None:
        for m, d in _ordered_dirs:
            if p == d or p.startswith(d + "/"):
                return m
        return None

    # 证据收集：{module: {("plan"|"baseline", 名)}}；writers_of 供 R65D-T2② 方向校验
    evid: dict[str, set[tuple[str, str]]] = {m: set() for m in plan_dir_of}
    writers_of: dict[str, set[str]] = {}
    for st in (getattr(plan, "subtasks", None) or []):
        sc = getattr(st, "scope", None)
        writes = [_norm_scope_path(str(f)) for f in
                  (list(getattr(sc, "create_files", None) or [])
                   + list(getattr(sc, "writable", None) or []))]
        write_mods = {_owner_mod(p) for p in writes} - {None}
        if not write_mods:
            continue
        for _wm in write_mods:
            writers_of.setdefault(_wm, set()).add(st.id)
        for r in (getattr(sc, "readable", None) or []):
            rp = _norm_scope_path(str(r))
            if "/" not in rp or not _is_code_path(rp):
                continue
            # ★R65E14-T5（#44）★ AUX 资源（static/resources 布局段下的 .js/.html/模板等）
            # 不构成模块编译依赖证据——round65e14 实测 alarm→admin 的 9 条"证据"全是
            # admin/static/*.js（垂直切片塞的前端上下文），与真依赖 admin→alarm 凑成互指
            # → 双向剪除把真依赖也连坐剪掉。复用 R64 证据分层（_evidence_class 判据=路径
            # 布局，栈中立：npm 的 src/*.js 是 WEAK_CODE 不受影响）。
            if _evidence_class(rp) == _EV_AUX:
                continue
            om = _owner_mod(rp)
            for m in write_mods:
                if om is not None:
                    if om != m:
                        evid[m].add(("plan", om))
                    continue
                top = rp.split("/", 1)[0]
                if top in _SRC_LAYOUT_SEGMENTS:
                    continue
                evid[m].add(("baseline", top))

    # plan 兄弟互指 = 注入必成环 → 双向剪除（更深计划错，surfaced 不静默）
    mutual = {(a, b) for a, deps in evid.items()
              for kind, b in deps if kind == "plan" and ("plan", a) in evid.get(b, set())}
    if mutual:
        logger.warning(
            "[T5] %d 对 plan 模块互相消费对方产物（注入依赖必成环）→ 双向都不注入内部依赖"
            "（更深计划错，交结构闸/依赖序机制），请查: %s",
            len(mutual), sorted(mutual)[:4])

    rg = _root_gav(project_path)
    out: dict[str, list[str]] = {}
    for m, deps in evid.items():
        specs: list[str] = []
        for kind, name in sorted(deps):
            if kind == "plan":
                if (m, name) in mutual:
                    continue
                d = plan_dir_of.get(name) or name
                # R65D-T2②（round65d st-26 反向边死型）：目标模块 pom 生产者在消费方
                # 写者的传递下游 → 剪除 fail-loud（判据/铁律见 helper docstring）
                _prod = _reverse_internal_edge_producer(
                    plan, root, writers_of.get(m, set()), d)
                if _prod is not None:
                    logger.warning(
                        "[T5] R65D-T2 反向内部依赖剪除：%s → %s——目标模块 pom 生产者 "
                        "%s 传递依赖 %s 的写者（消费方构建时目标 pom 必不存在=成环死型，"
                        "round65d st-26 本体），绝不注入；边方向属更深计划错，交 #57 DAG 面",
                        m, name, _prod, m)
                    continue
                art = _baseline_module_artifact(root, d)
                if art:
                    specs.append(art)          # 落在既有模块目录（R58-1 形态）→ 依真 artifactId
                elif not (root / d / "pom.xml").is_file():
                    # 新兄弟：pom 尚不存在（同批 scaffold 造）→ 显式坐标与模板 artifactId 同源。
                    # hunter#F2 CRITICAL：根 GAV 解析不到（继承 GAV 根）绝不退化裸名——裸名会
                    # 流进 maven_registry 的 Central 反查，把**本工程自己的新模块名**解析成
                    # 不相干的真实第三方构件（实测 alarm-interface → org.ow2.jasmine 包）＝
                    # 伪造坐标盖权威章（R47-2 禁令变体）。解析不到就不注入+响亮留痕。
                    if rg:
                        specs.append(f"{rg[0]}:{name}:${{project.version}}")
                    else:
                        logger.warning(
                            "[T5] 根 pom GAV 不可解析（继承 GAV？）→ 新兄弟模块 %s 的内部"
                            "依赖不注入（绝不退化裸名走 Central 反查伪造坐标，R47-2）——"
                            "该依赖交 worker L1 防线④按真实 import 事后补", name)
            else:
                art = _baseline_module_artifact(root, name)
                if art:
                    specs.append(art)
        if specs:
            out[m] = sorted(set(specs))
    if out:
        logger.info(
            "[T5] 从跨模块 readable 证据推导出内部模块依赖（消费方=owner/scaffold 两个模板"
            "注入点；round63：契约从不声明内部依赖 → 模板缺 ruoyi-common 类基线库）: %s",
            {k: v for k, v in sorted(out.items())})
    return out


def _merge_internal_deps(arts: list[str], derived: list[str]) -> list[str]:
    """T5：把推导出的内部依赖并入契约 artifacts（按 artifactId 去重，契约已列的绝不重复）。"""
    have: set[str] = set()
    for a in arts:
        parts = str(a).split(":")
        have.add((parts[1] if len(parts) > 1 else parts[0]).strip())
    merged = list(arts)
    for d in derived:
        parts = str(d).split(":")
        art = (parts[1] if len(parts) > 1 else parts[0]).strip()
        if art and art not in have:
            merged.append(d)
            have.add(art)
    return merged


def _strip_machine_pom_blocks(desc: str, pom: str) -> str:
    """R65D-T2①：剥除 description 中针对 pom 的既有机器块（权威模板/铁律/缺失片段）。

    round65d 死因链第①层：R58-3 旧幂等守卫「描述里已有模板→整体跳过」把第一遍
    注入的陈旧模板冻结（10:25 T5 单向证据推出 interface→alarm 反向依赖烤进 st-26，
    10:39 终版推导该边已消失却无从刷新）；MODIFY 形态守卫更是只认「权威 pom 模板」
    字样 → 铁律/片段块被重复追加（st-42 实锤 ×2）。upsert=先剥后贴，终版为真值。
    """
    import re as _re
    p = _re.escape(pom)
    # 带 ```xml fence 的块（权威模板 / 缺失依赖片段），两代措辞通吃
    desc = _re.sub(
        rf"\n?【(?:权威 pom 模板|缺失依赖片段)（[^】]*{p}[^】]*】\n```xml\n.*?\n?```",
        "", desc, flags=_re.S)
    # 铁律块（无 fence）：从标题到下一个机器块标题或串尾
    desc = _re.sub(
        rf"\n?【既有 pom 修改铁律（{p} 已存在）】.*?(?=\n【|\Z)",
        "", desc, flags=_re.S)
    return desc


def _upsert_owner_pom_block(owner, pom: str, new_block: str) -> bool:
    """把针对 pom 的机器块 upsert 进 owner.description（幂等；刷新时 fail-loud）。"""
    old = owner.description or ""
    new_desc = _strip_machine_pom_blocks(old, pom) + new_block
    if new_desc == old:
        return False   # 幂等：本遍确定性产物与已有块一致
    # 猎手 MED 整改（多 pom owner 顺序抖动）：块内容一致只是位置不同（strip+append 会把
    # 先处理的 pom 块挪到后处理的之后，逐遍交替"变化"）→ 位置无关幂等，杜绝 WARNING
    # 刷屏把真漂移信号淹成噪声。判据=旧描述已含完全一致的本遍块，且除它外无本 pom 其他机器块。
    if new_block and new_block in old:
        _without = old.replace(new_block, "", 1)
        if _strip_machine_pom_blocks(_without, pom) == _without:
            return False
    had_block = old != _strip_machine_pom_blocks(old, pom)
    if had_block:
        logger.warning(
            "[SCAFFOLD-INJECT] R65D-T2 %s 的 %s 机器块与本遍确定性产物不一致 → 刷新"
            "（陈旧模板绝不冻结上车——round65d st-26 反向依赖即第一遍毒模板被旧守卫冻结）",
            getattr(owner, "id", "?"), pom)
    owner.description = new_desc
    return True


def _inject_templates_into_pom_owners(plan, project_path: str | None,
                                      file_plan: list | None = None,
                                      internal_deps: dict[str, list[str]] | None = None,
                                      ) -> list[str]:
    """R58-3：给**已被认领**的模块 pom 的 owner 子任务，也嵌入确定性权威模板。

    脚手架只覆盖"无人认领"的 pom；一旦计划里某个写代码的子任务顺手认领了 `<mod>/pom.xml`，
    它就绕过了确定性模板、由小模型自由发挥 —— round58 实测写出属性引用的 parent 版本 → FATAL。
    模板是**纯机械产物**，谁写都该照抄同一份。
    T5：internal_deps 由调用方（inject_build_scaffold_subtasks）单次推导传入（hunter#F4：
    两注入点必须共用同一份，各算各的会在输入分叉时产出不一致模板）；直接调用时自算兜底。
    """
    if not project_path:
        return []
    dirs = _module_physical_dirs(plan, project_path, file_plan)
    if internal_deps is None:
        try:
            internal_deps = derive_internal_module_deps(plan, dirs, project_path)
        except Exception:  # noqa: BLE001
            logger.warning("[T5] 内部模块依赖推导失败（fail-open，模板退回纯契约 artifacts）",
                           exc_info=True)
            internal_deps = {}
    _internal_deps = internal_deps
    touched: list[str] = []
    # T5（hunter#F1 对称面）：迭代面=契约 dependencies 条目 ∪ 只有内部依赖证据的模块——
    # 契约无条目/条目被剪的模块，其认领者同样必须拿到含内部依赖的模板。
    _iter_entries: list[tuple[str, list]] = []
    _seen_mods: set[str] = set()
    for entry in ((plan.shared_contract or {}).get("dependencies") or []):
        if not isinstance(entry, dict):
            continue
        _em = (entry.get("module") or "").strip().rstrip("/")
        if _em:
            _iter_entries.append((_em, [a for a in (entry.get("artifacts") or []) if a]))
            _seen_mods.add(_em)
    for _m in sorted(_internal_deps):
        if _m not in _seen_mods:
            _iter_entries.append((_m, []))
    for mod, arts in _iter_entries:
        mdir = dirs.get(mod)
        if not mod or not mdir:
            continue
        pom = f"{mdir}/pom.xml"
        owner = None
        for st in plan.subtasks:
            sc = getattr(st, "scope", None)
            owns = [_norm_scope_path(f) for f in
                    (list(getattr(sc, "create_files", None) or [])
                     + list(getattr(sc, "writable", None) or []))]
            if pom in owns:
                owner = st
                break
        if owner is None:
            continue
        # R65D-T2①：已有机器块不再是跳过理由——终版确定性产物 upsert（幂等/刷新），
        # 陈旧模板（第一遍 T5 推导烤进的反向依赖）绝不冻结上车。
        arts = _merge_internal_deps(arts, _internal_deps.get(mod) or [])   # T5
        _kept, _ = resolve_scaffold_artifacts(
            project_path, arts, extra_module_artifacts=_plan_module_artifacts(plan))  # R67C-T2
        # R65C-T1 毒株(a)：完整模板只给 CREATE（与主入口 1595-1615 同律）——
        # 「原样写入」对**既有** pom = 最小化重写清空基线依赖（round65c 实锤：
        # ruoyi-common 丢 poi / ruoyi-framework 丢 web、aop starter，worker 服从性
        # 写入 → 模块自伤 346 行编译错，换模型重试必同果）。既有 pom 只给
        # 缺失依赖片段 + 并入措辞。
        _pom_exists = bool(project_path) and (Path(project_path) / pom).is_file()
        if _pom_exists:
            # 猎手 R65C (a)：零可解析缺失依赖也必须给护栏——静默跳过=owner 在无任何
            # 反 clobber/反属性引用指引下自由改既有 pom（R58-3 保护面整段丢弃且无日志）。
            # 片段可以没有，护栏必须有，touched 必须记。
            _dep_snips = "\n".join(_render_dep_block(d) for d in _kept)
            _snip_block = (f"\n【缺失依赖片段（并入 {pom} 既有 <dependencies>）】\n```xml\n"
                           f"{_dep_snips}\n```") if _dep_snips else ""
            _iron_block = (
                f"\n【既有 pom 修改铁律（{pom} 已存在）】只做最小增量修改：绝不整体替换/"
                "重写该文件，绝不删除既有依赖/插件/属性，绝不改动既有 parent 声明"
                "（parent 版本若需写必须是**字面量**，绝不可写成 ${{...}} 属性引用）。"
                + _snip_block)
            if _upsert_owner_pom_block(owner, pom, _iron_block):   # R65D-T2①
                touched.append(owner.id)
            continue
        _pgav = None
        _rg = _root_gav(project_path)
        if _rg and "/" in mdir:      # R57-7：住在聚合目录下 → parent 是聚合父，不是根
            _pgav = (_rg[0], mdir.rsplit("/", 1)[0].rsplit("/", 1)[-1], _rg[2])
        tpl = _deterministic_pom_template(mod, [], project_path, resolved=_kept,
                                          parent_gav=_pgav)
        if not tpl:
            continue
        _auth_block = (
            f"\n【权威 pom 模板（确定性生成，原样写入 {pom}；parent 版本必须是**字面量**，"
            f"绝不可写成 ${{...}} 属性引用——Maven 解析 parent 时尚未加载父 pom，属性永远解析不了，"
            f"整棵 reactor 会读不出）】\n```xml\n{tpl}\n```")
        if _upsert_owner_pom_block(owner, pom, _auth_block):   # R65D-T2①
            touched.append(owner.id)
    if touched:
        logger.warning(
            "[SCAFFOLD-INJECT] R58-3 %d 个子任务自行认领了模块 pom（不走脚手架）→ 已把**确定性权威模板**"
            "嵌进它们的 description：%s —— 有 owner ≠ 有模板；小模型手写 pom 会写出属性引用的 parent "
            "版本，pom 解析期就崩（round58 实锤死因）", len(touched), touched[:8])
    return touched


def _inject_aggregator_scaffold(plan, dirs: dict[str, str],
                                project_path: str | None, existing_ids: set,
                                injected: list,
                                phys: set[str] | None = None) -> dict[str, str]:
    """R57-4b：子模块同处一个**非根**聚合目录时，确定性注入该聚合父 POM 的脚手架（拓扑最先）。

    round57 实锤：子模块都在 `ruoyi-alarm/` 下，而父 POM `ruoyi-alarm/pom.xml` 的创建权被
    分给了 st-1，st-1 又依赖 st-13/21/39 → **依赖顺序死结** → 那三个子任务编译时父 POM
    不存在（`Could not find the selected project in the reactor`）→ 全灭 → 阶梯三保 build
    → **连坐放弃下游 69 个**。父聚合模块**不依赖任何子模块**，必须先于它们落地。

    ★R60-1（round60 死因）★ 聚合父的存在性**与子模块 pom 有没有 owner 无关**——必须基于
    **全部契约模块的物理目录**判定，绝不能只看 `unclaimed` 的那些。round60 实锤：R58-3 太成功，
    8 个子模块 pom 全被认领 → entries 空 → 本函数（曾用 entries 过滤）看到空聚合层 → 不注入
    → `ruoyi-alarm/pom.xml` 没人建 → 所有子模块 parent `com.ruoyi:ruoyi-alarm:pom` 找不到 → 全员 FATAL。

    只在**唯一**聚合目录且**无人认领其 pom** 时注入；歧义/已有 owner → 不动（绝不猜）。
    注入后，让**所有认领了该聚合下子模块 pom 的 owner**（含脚手架与写代码的子任务）依赖聚合父先落地。
    """
    # ★R61-1★ 每个**非根**聚合目录都需要一个聚合父 POM。round61 前旧实现"全局唯一聚合目录
    # 才注入、否则一个不建"，多聚合场景会漏掉全部父 POM。逐个处理。
    # ★R61-2（对抗复核实锤）★ 返回【聚合目录→脚手架 sid】映射，而非单个 last_sid：下游给每个
    # 子模块脚手架挂"依赖父 POM 先落地"的边时，必须挂**它自己所在聚合目录**的父，不能一律挂最后
    # 一个（多聚合场景会把 ruoyi-alarm 下的模块错挂到 ruoyi-biz 的父上、且漏掉真父 → parent
    # 找不到 → round57 死因原样复活）。
    # ★Task#4 治本★ 聚合目录集合必须覆盖**所有收码物理模块**的父目录，而不仅是干净契约模块
    # 解析出的那些——否则一个契约名被 fail-closed 拒掉、但真收了码的子模块，其聚合父根本不会被
    # 注入 → 该聚合层缺父 pom / 子模块不进 <modules> → round62 静默丢模块。
    _all_mod_dirs = set(dirs.values()) | (phys or set())
    # ★Task#4 复核治本（多级聚合）★ parents = 每个物理模块到根之间的**全部祖先目录**，而非仅
    # 直接父。否则 `a/b/c` 只注入 `a/b`、漏掉中间层 `a`，且 `a/b` 的 parent GAV 会指向不存在/
    # 错误的上级 → round57 FATAL。all_nodes = 物理模块 ∪ 全部聚合祖先 = reactor 里每个 maven
    # 节点（jar 或 pom），<modules> 完整性据它算（直接子节点，含中间层聚合器）。
    parents_set: set[str] = set()
    for d in _all_mod_dirs:
        _parts = d.split("/")
        for i in range(1, len(_parts)):
            parents_set.add("/".join(_parts[:i]))
    all_nodes = _all_mod_dirs | parents_set
    agg_ids: dict[str, str] = {}
    for agg in sorted(parents_set):   # 浅→深：父聚合器先注入，子聚合器才挂得到它
        _sid = _inject_one_aggregator_pom(
            plan, agg, dirs, project_path, existing_ids, injected, phys, all_nodes)
        if not _sid:
            continue
        agg_ids[agg] = _sid
        # 嵌套聚合器依赖它**自己的上级聚合器**先落地（顶层聚合器 parent=根，无此边）——
        # 与叶子/孤儿依赖直接聚合父同理，保证 parent pom 链在编译前齐备。
        _pagg = agg.rsplit("/", 1)[0] if "/" in agg else None
        _pagg_sid = agg_ids.get(_pagg) if _pagg else None
        if _pagg_sid:
            _scaf = next((s for s in plan.subtasks if s.id == _sid), None)
            if _scaf is not None and _pagg_sid not in _scaf.depends_on:
                _scaf.depends_on.append(_pagg_sid)
    return agg_ids


def _inject_one_aggregator_pom(plan, agg: str, dirs: dict[str, str],
                               project_path: str | None, existing_ids: set,
                               injected: list,
                               phys: set[str] | None = None,
                               all_nodes: set[str] | None = None) -> str | None:
    """为单个聚合目录 agg 注入确定性聚合父 POM 脚手架（拓扑最先、**独占**其 pom 写权）。"""
    from swarm.types import FileScope, SubTask, TaskIntent

    agg_pom = f"{agg}/pom.xml"
    sid = f"st-scaffold-{agg.replace('/', '-')}"
    if sid in existing_ids:
        return sid
    # ★R61-1（round61 死因）★ 即使有**写代码的子任务**认领了聚合父 pom，也**绝不让位**——
    # 它不保证拓扑最先、也不保证内容正确（手写 pom），子模块编译时父 POM 可能还没建/内容不对
    # → `Non-resolvable parent POM` → 全员 FATAL（round57 原始死因复活）。改为：确定性脚手架
    # 独占其写权（下方 R57-6 式收回），拓扑最先。
    exists = bool(project_path) and (Path(project_path) / agg_pom).is_file()
    # ★Task#4 复核治本（多级）★ 结构冲突自检：一个目录若**既是收码物理模块又是聚合父**
    # （自身有直接代码 + 名下还有子模块），Maven 无法两全（packaging=pom 不编译自身代码、
    # packaging=jar 不下钻 <modules>）——这是计划质量缺陷（公共代码应下沉到独立子模块）。
    # 绝不静默产出会丢代码的 pom：LOUD 告警交 plan-quality 复核（Task#9），登记仍按聚合父走。
    if phys and agg in phys:
        logger.warning(
            "[SCAFFOLD-INJECT] Task#4 结构冲突：聚合目录 %r **同时有直接代码落点**——Maven 目录不能既是 "
            "packaging=pom 聚合父又是 jar 代码模块，其自身直接代码将不被编译。这是计划质量缺陷"
            "（应把公共代码下沉到独立子模块）→ 已按聚合父登记但请复核。", agg)
    # ★Task#4 治本（round62 真断）★ <modules> 必须登记**该聚合下全部直接子节点**（收码物理子模块
    # + 中间层子聚合器）——而非仅 `_module_physical_dirs` 解析出的干净契约模块。契约名被 fail-closed
    # 拒掉（歧义/撞车/占位符）但真收了码、拿了 pom 的子模块，若不进 <modules> → Maven 不下钻 → 静默
    # 丢模块。据 all_nodes（物理模块 ∪ 全部聚合祖先）算，缺登记=灾难、多登记=无害。
    _nodes = all_nodes if all_nodes is not None else (set(dirs.values()) | (phys or set()))
    sub_names = sorted({d.rsplit("/", 1)[-1] for d in _nodes
                        if "/" in d and d.rsplit("/", 1)[0] == agg})   # 只算**直接**子节点
    _agg_tpl = _aggregator_pom_template(agg, sub_names, project_path)
    scaffold = SubTask(
        id=sid,
        description=(
            f"【构建脚手架·聚合父模块】{'补齐' if exists else '创建'} {agg_pom}："
            f"packaging=pom 的聚合模块，<modules> 里登记全部子模块 {sub_names}，"
            f"并把 {agg} 注册进根 pom 的 <modules>。"
            "\n⚠️ 它是所有子模块的父级：父 POM 不存在 → 子模块一个都编译不了"
            "（`Could not find the selected project in the reactor`）→ 必须最先落地。"
            "\n只写构建文件，不写任何业务代码。"
            + ((f"\n【权威 pom 模板（确定性生成，"
                + (f"参照此模板补齐 {agg_pom} 的 <modules> 登记——并入既有内容，"
                   "绝不删除既有 <modules> 条目/依赖/其他既有段"
                   if exists else f"原样写入 {agg_pom}")
                + f"）】\n```xml\n{_agg_tpl}\n```")
               if _agg_tpl else "")),
        intent=TaskIntent.MODIFY if exists else TaskIntent.CREATE,
        difficulty=SubTaskDifficulty.TRIVIAL,
        scope=FileScope(writable=[agg_pom, "pom.xml"] if exists else ["pom.xml"],
                        create_files=[] if exists else [agg_pom]),
        acceptance_criteria=[f"{agg_pom} 存在且 packaging 为 pom",
                             f"{agg_pom} 的 <modules> 登记了 {sub_names}",
                             f"根 pom 的 <modules> 里有 {agg}"],
    )
    plan.subtasks.append(scaffold)
    existing_ids.add(sid)
    if plan.parallel_groups:
        plan.parallel_groups.insert(0, [sid])   # 拓扑最先
    injected.append({"module": agg, "subtask_id": sid, "artifacts": [],
                     "pom_exists": exists, "aggregator": True})
    # R60-1：让**所有认领了该聚合下任一 pom 的子任务**（含写代码的认领者）依赖聚合父先落地。
    # 否则子模块编译时 `ruoyi-alarm/pom.xml` 可能还没建 → parent 找不到（round60 死因）。
    _agg_prefix = f"{agg}/"
    for st in plan.subtasks:
        if st.id == sid:
            continue
        sc = getattr(st, "scope", None)
        # R61-1：从**写代码的子任务**手里收回聚合父 pom 写权（脚手架不碰）→ 脚手架独占、拓扑最先。
        if not str(st.id).startswith("st-scaffold-"):
            for _attr in ("create_files", "writable"):
                _lst = getattr(sc, _attr, None)
                if _lst:
                    _keep = [f for f in _lst if _norm_scope_path(f) != agg_pom]
                    if len(_keep) != len(_lst):
                        logger.warning(
                            "[SCAFFOLD-INJECT] R61-1 从 %s 收回聚合父 pom 写权 %s → 脚手架 %s 独占"
                            "（认领者不保证拓扑最先/内容正确 → parent POM 找不到 → 全员 FATAL）",
                            st.id, agg_pom, sid)
                        setattr(sc, _attr, _keep)
        owns = [_norm_scope_path(f) for f in
                (list(getattr(sc, "create_files", None) or [])
                 + list(getattr(sc, "writable", None) or []))]
        # 往聚合目录下写**任何**文件（代码或 pom）的子任务，编译都需要父 POM 先在 → 依赖它。
        if any(o.startswith(_agg_prefix) for o in owns) and sid not in st.depends_on:
            st.depends_on.append(sid)
    logger.warning(
        "[SCAFFOLD-INJECT] R57-4b/R60-1 子模块同处聚合目录 %r → 确定性注入父 POM 脚手架 %s（拓扑最先，"
        "不依赖任何子模块；所有子模块 pom 的 owner 依赖它先落地）。父 POM 没人先建 → 子模块全部 "
        "'not in the reactor' / parent not found → 连坐全灭。", agg, sid)
    return sid


def _inject_orphan_module_scaffolds(plan, phys: set[str], dirs: dict[str, str],
                                    agg_ids: dict[str, str], project_path: str | None,
                                    existing_ids: set, injected: list) -> None:
    """★Task#4 治本★ 收码但【非干净契约模块】的物理子模块 → 补确定性最小 pom 脚手架。

    `_module_physical_dirs` 对歧义/撞车契约模块名 fail-closed（不给它们建带契约依赖的 pom）；
    但它们**真的收了码**、且已被 `_inject_one_aggregator_pom` 用 phys 登记进聚合父 <modules>
    → Maven 会下钻找它们的 pom。若没人确定性地建这个 pom（worker 手写又可能把 parent GAV 写成
    根工程 → round57 FATAL），就是"派 worker 去失败"。这里给每个这样的孤儿模块补一个
    parent=聚合父、packaging=jar、无契约依赖的最小 pom（L1 build-repair 再补依赖），并**独占**
    其写权（同 R57-6/R61-1：构建文件是纯机械产物，绝不让小模型编 parent 坐标）。

    只处理**聚合父之下**的孤儿（agg_ids 里有其父）——根级模块由 manifest_synth 路径兜底、
    且根级无 parent-GAV 歧义。id 用**完整物理路径**（st-scaffold-<path-dashed>）→ 天然防止
    同名叶子在不同聚合下 slug 撞车（Task#4 预判）。基线已有该 pom → 尊重既有、不 clobber。
    """
    from swarm.types import FileScope, SubTask, TaskIntent

    _clean_dirs = set(dirs.values())
    for d in sorted(phys):
        if "/" not in d:
            continue                       # 根级模块（无聚合父）：不在本函数职责内
        agg = d.rsplit("/", 1)[0]
        agg_sid = agg_ids.get(agg)
        if not agg_sid:
            continue                       # 不在任何已注入的聚合父之下 → 不动（绝不猜落点）
        if d in _clean_dirs:
            continue                       # 干净契约模块：走 entries 脚手架（带契约依赖），不重复
        pom = f"{d}/pom.xml"
        sid = f"st-scaffold-{d.replace('/', '-')}"
        if sid in existing_ids:
            continue                       # 幂等（含 d 本身又是聚合父的自反情形：sid 已被聚合父占用）
        if project_path and (Path(project_path) / pom).is_file():
            continue                       # 基线已有 pom：尊重既有（登记已由 <modules> 完成）
        name = d.rsplit("/", 1)[-1]
        _pgav = None
        _rg = _root_gav(project_path)
        if _rg:
            _pgav = (_rg[0], agg.rsplit("/", 1)[-1], _rg[2])   # parent = 聚合父 GAV（同聚合模板）
        _tpl = _deterministic_pom_template(name, [], project_path, resolved=[],
                                           parent_gav=_pgav)
        _tpl_block = (f"\n【权威 pom 模板（确定性生成，原样写入 {pom}）】\n```xml\n{_tpl}\n```"
                      if _tpl else "")
        if not _tpl:
            # ★Task#4 复核（silent-failure-hunter F3）★ 根 pom 不可解析/缺 GAV → 无确定性模板，
            # 脚手架仅剩文字指引、无字面 parent GAV，worker 须手写 parent（R57-7 手写属性引用/错
            # 坐标风险）。绝不静默降级：LOUD 标注为待复核降级项（同 R45-2 精神——pom 是机械产物）。
            logger.warning(
                "[SCAFFOLD-INJECT] Task#4 孤儿模块 %r 无法确定性生成 pom 模板（根 pom 不可解析/缺 GAV）→ "
                "脚手架降级为纯文字指引、无字面 GAV，worker 须手写 parent（R57-7 风险）——已标注待复核。", d)
        scaffold = SubTask(
            id=sid,
            description=(
                f"【构建脚手架·孤儿模块】为收码物理模块 {d} 创建 {pom}："
                f"packaging=jar、parent=聚合父 {agg.rsplit('/', 1)[-1]}（relativePath ../pom.xml 正好指到它）。"
                "\n它已登记进聚合父 <modules>，Maven 会下钻构建它——pom 不存在 → "
                "`child module ... does not exist`。只写构建文件，不写任何业务代码。"
                + _tpl_block),
            intent=TaskIntent.CREATE,
            difficulty=SubTaskDifficulty.TRIVIAL,
            scope=FileScope(create_files=[pom]),
            acceptance_criteria=[f"{pom} 存在且 packaging 为 jar，parent 指向聚合父 {name!r} 的上级"],
        )
        plan.subtasks.append(scaffold)
        existing_ids.add(sid)
        if agg_sid != sid and agg_sid not in scaffold.depends_on:
            scaffold.depends_on.append(agg_sid)   # 孤儿依赖聚合父先落地（拓扑：父 pom 先在）
        # R57-6 式收权：从写代码子任务手里收回该孤儿 pom 写权（多写者 rebase 不收敛 + 手写
        # parent 坐标风险），并让往该目录写码者依赖脚手架先落地。绝不碰别的脚手架的写权。
        _prefix = d.rstrip("/") + "/"
        for st in plan.subtasks:
            if st.id == sid or str(st.id).startswith("st-scaffold-"):
                continue
            sc = getattr(st, "scope", None)
            for _attr in ("create_files", "writable"):
                _lst = getattr(sc, _attr, None)
                if _lst:
                    _keep = [f for f in _lst if _norm_scope_path(f) != pom]
                    if len(_keep) != len(_lst):
                        setattr(sc, _attr, _keep)
            _writes = [_norm_scope_path(f) for f in
                       (list(getattr(sc, "create_files", None) or [])
                        + list(getattr(sc, "writable", None) or []))]
            if any(w.startswith(_prefix) for w in _writes) and sid not in st.depends_on:
                st.depends_on.append(sid)
        if plan.parallel_groups:
            plan.parallel_groups.insert(0, [sid])
        injected.append({"module": name, "subtask_id": sid, "artifacts": [],
                         "pom_exists": False, "orphan": True})
        logger.warning(
            "[SCAFFOLD-INJECT] Task#4 收码物理模块 %r 非干净契约模块、无 pom owner → 补确定性最小 pom "
            "脚手架 %s（parent=聚合父 %s，已进父 <modules>；否则 Maven 下钻找不到其 pom = 派 worker 去失败）",
            d, sid, agg.rsplit("/", 1)[-1])


# ★G9（Task#9 审计⑤ stack-neutrality 铁律）★ 脚手架注入 per-stack driver 注册表。
# round44-62 病根之一：plan 期 build 闭合机制【只认 Maven】——脚手架无条件造 pom.xml/<modules>/
# <parent> reactor，从不看技术栈；对 Go/npm/Rust/Python 工程 = 凭空塞 Maven 产物污染 reactor。
# 治：注入【入口按栈分派】——Maven 走既有确定性 pom 脚手架（行为字节级不变，是本注册表首个 driver）；
# 已知非 Maven 栈目前无 aggregator 脚手架实现 → 明确【不伪造】(no-op + LOUD 告警)，绝不再拿 pom
# 污染异栈；未知栈 → 保守回退 Maven（back-compat，与今日行为一致，下游 R57-1 pom 取证仍二次把关）。
# 各栈的 aggregator 脚手架（Gradle settings.gradle include / Cargo workspace / go.work…）是本注册表
# 的后续插入点：新增一栈只需给它一个 aggregator driver 并登记进 _AGGREGATOR_SCAFFOLD_STACKS。
# ★键用 manifest 的【规范大小写】★（Cargo.toml 首字母大写）——基线 os.path.exists 在
# 大小写敏感文件系统（Linux）上必须用真实文件名，否则漏检 Cargo 工程。计划路径匹配走下方
# 小写映射（LLM 写的路径大小写不可信）。
_MANIFEST_TO_STACK = {
    "pom.xml": "maven",
    "build.gradle": "gradle", "build.gradle.kts": "gradle",
    "settings.gradle": "gradle", "settings.gradle.kts": "gradle",
    "package.json": "npm", "go.mod": "go", "go.work": "go",
    "Cargo.toml": "cargo", "pyproject.toml": "python",
}
_MANIFEST_TO_STACK_LC = {k.lower(): v for k, v in _MANIFEST_TO_STACK.items()}
# 目前具备【确定性 aggregator 脚手架实现】的栈（其余已知栈明确不伪造，交后续 driver）。
_AGGREGATOR_SCAFFOLD_STACKS = frozenset({"maven"})


def _detect_build_stack(plan, project_path: str | None, file_plan: list | None = None) -> str:
    """确定性推断工程构建栈（栈中立、离线）：基线根 manifest（真工程）+ 计划/file_plan 里出现的
    manifest basename。有 pom 证据 → 'maven'（混栈工程优先保 Maven 脚手架，与今日行为一致）；
    无任何 manifest 证据 → 'unknown'（保守回退 Maven，back-compat）。"""
    import os
    seen: set[str] = set()
    if project_path:
        try:
            for name, stk in _MANIFEST_TO_STACK.items():
                if os.path.exists(os.path.join(project_path, name)):
                    seen.add(stk)
        except (OSError, TypeError, ValueError) as exc:  # os.path.join 对非法输入可抛
            logger.debug("[SCAFFOLD-INJECT] G9 基线 manifest 探测异常（跳过基线证据源）: %s", exc)
    paths: list[str] = []
    for st in getattr(plan, "subtasks", None) or []:
        sc = getattr(st, "scope", None)
        paths += list(getattr(sc, "create_files", None) or [])
        paths += list(getattr(sc, "writable", None) or [])
    try:
        for ps in _file_plan_module_paths(file_plan).values():
            paths += list(ps or [])
    except Exception as exc:  # noqa: BLE001 — file_plan 解析失败不影响栈判定
        logger.debug("[SCAFFOLD-INJECT] G9 file_plan 栈证据解析失败（跳过该证据源）: %s", exc)
    _has_jvm_src = False
    for p in paths:
        pn = _norm_scope_path(p)
        base = pn.rsplit("/", 1)[-1].lower()
        if base in _MANIFEST_TO_STACK_LC:
            seen.add(_MANIFEST_TO_STACK_LC[base])
        if base.rsplit(".", 1)[-1] in ("java", "kt", "kts", "scala"):
            _has_jvm_src = True
    if not seen:
        return "unknown"          # 无任何 manifest 证据 → 保守回退 Maven（调用方 log）
    if "maven" in seen:
        return "maven"            # pom 证据 → Maven 脚手架适用（混栈优先保 Maven）
    if "gradle" in seen:
        return "gradle"           # 明确 Gradle → 跳过 pom 伪造（Gradle 不用 pom）
    # 无 pom/gradle manifest、但有 go/npm/cargo/python manifest。★安全护栏（对抗复核预判）★：
    # 若计划里同时有 JVM 源码（.java/.kt/.scala），这是【歧义混栈】——可能是 greenfield Maven
    # 后端 pom 尚未建 + 前端 package.json；此时【绝不】按 npm 跳过 Maven 脚手架（否则后端 Java
    # 模块静默丢 pom = round62 家族级回归），回退 unknown→Maven（与今日行为一致，下游 R57-1 把关）。
    if _has_jvm_src:
        return "unknown"
    return sorted(seen)[0]


def _should_fabricate_maven_scaffold(
    plan, project_path: str | None, file_plan: list | None = None,
) -> tuple[bool, str]:
    """★G9 单一权威闸（对抗双复核 HIGH：两处 pom 伪造入口必须同源、杜绝漂移）★
    是否应走 Maven pom 脚手架 → (should, detected_stack)。已知非 Maven 栈（gradle/go/npm/cargo/
    python）→ False；Maven/unknown → True（unknown=无证据保守回退 Maven，back-compat）。
    ★局限（诚实记录）★：本判据是【整计划级】——零证据【新模块】落在 Maven 根工程里时无法逐模块
    辨栈，随根工程按 Maven 处理（无证据即随根约定，是合理默认；root pom 存在性由下游模板再把关）。"""
    stk = _detect_build_stack(plan, project_path, file_plan)
    return (stk == "unknown" or stk in _AGGREGATOR_SCAFFOLD_STACKS), stk


def _extract_auth_templates(desc: str) -> list[tuple[str, str]]:
    """description → [(pom 路径, 模板 XML)]（仅「原样写入」CREATE 形态；MODIFY 片段
    是增量语义、文件终态不确定，考卷同源不适用）。"""
    import re as _re
    out: list[tuple[str, str]] = []
    for m in _re.finditer(
            # 复核 CONFIRMED：路径捕获必须排除全角）——聚合父/孤儿脚手架措辞
            # 「原样写入 {pom}）】」无限定语，漏排会把 ）粘进路径 → 断言永远考错文件
            r"【权威 pom 模板（[^】]*?原样写入 ([^\s;；)）】]+)[^】]*】\n```xml\n(.*?)\n?```",
            desc or "", flags=_re.S):
        out.append((m.group(1).strip(), m.group(2)))
    return out


def _strip_pom_exclusions(text: str) -> str:
    """R65E2-T1（猎手 F1）：剔除 `<exclusions>…</exclusions>` 子区——Maven exclusion 是
    【禁入某传递依赖】的声明，其 <artifactId> 绝不是本模块的直接依赖，也绝不是"禁入守卫
    负断言与模板正面矛盾"的证据（禁 log4j 的 exclusion 被误读成"要求 log4j"→剔除合法禁入
    守卫+注入假正断言）。"""
    import re as _re
    return _re.sub(r"<exclusions>.*?</exclusions>", "", text or "", flags=_re.S)


def _template_dep_artifacts(tpl: str) -> list[str]:
    """模板 <dependencies> 区的 artifactId 清单（去重保序；parent/自身声明天然不在区内）。"""
    import re as _re
    seg = (tpl or "").split("<dependencies>", 1)
    if len(seg) < 2:
        return []
    body = _strip_pom_exclusions(seg[1].split("</dependencies>", 1)[0])   # 猎手 F1：排除 <exclusions>
    return list(dict.fromkeys(
        a.strip() for a in _re.findall(r"<artifactId>([^<]+)</artifactId>", body)
        if a.strip()))


def _exam_grep_pattern(cmd: str) -> str | None:
    import re as _re
    m = _re.search(r"grep\s+(?:-{1,2}[\w=-]+\s+)*(?:'([^']*)'|\"([^\"]*)\")", cmd or "")
    if not m:
        return None
    return m.group(1) if m.group(1) is not None else m.group(2)


def _exam_pattern_hits(pat: str, text: str) -> bool:
    import re as _re
    try:
        return _re.search(pat, text) is not None
    except _re.error:
        return pat in text


def _is_pom_content_assert(cmd: str, pom: str) -> bool:
    import re as _re
    c = (cmd or "").strip()
    # 复核 CONFIRMED：路径按完整 token 边界匹配——裸子串会把兄弟模块
    # `a/mod-a/pom.xml`（嵌套复用叶名）的合法断言误吞进本 pom 的重生成面
    if not pom or not _re.search(
            r"(^|[\s'\"=(])" + _re.escape(pom) + r"($|[\s'\")])", c):
        return False
    return bool(c.startswith("grep ")
                or _re.match(r'^!\s*grep\s', c)   # R65E2-T1：`! grep …` 排除断言（round65e2 死因变体）
                or _re.match(r'^test\s+-[zn]\s+["\']?\$\(\s*grep', c))


def reconcile_template_exam(plan) -> dict[str, dict]:
    """R65D-T2③（round65d 主治）：考卷与权威模板同源——模板即真值，考卷必须从模板生成。

    st-26 四面矛盾死局：description 前半（LLM：jackson+httpclient5）↔ 权威模板
    （契约+T5：okhttp 系）↔ harness.verify（LLM 按自己的前半写 grep jackson）↔
    acceptance（#3 LLM jackson 系 vs #4 规则5 契约 okhttp 系互斥）。worker 徒手写出
    并集 pom（矛盾卷唯一最优解）被 H1 覆写销毁，再被旧考卷杀死=规划期注定的冤案。

    确定性对账（零 LLM，纯文本，幂等）——对每个带「原样写入」权威模板的子任务：
    - verify_commands 中针对该 pom 的【正断言】剔除、由模板 <dependencies> 逐条重新
      生成 grep '<artifactId>…</artifactId>'（陈旧正断言=考错卷，st-26 死型）；
    - 【负断言】（test -z "$(grep…)"）与模板正面冲突 → 剔除+WARNING（规划期自曝矛盾，
      绝不留给 worker 送死）；与模板不冲突 → ★保留★（猎手 CRITICAL：负断言是
      "禁入依赖不得出现"的守卫，模板被后续机制改写时它是最后一道牙齿）；
    - acceptance 的规则5 机器行（"必须声明依赖: […]"）改写为模板依赖清单；
      追加「模板即真值」权威验收行（下游 L2/CONFIRM 判官消歧用）。
    构建/工具类命令与针对其他文件的断言绝不误动。
    猎手 MED 整改：每子任务先在暂存区算全量结果、末尾一次性提交+独立 try/except——
    某子任务模板畸形绝不让已处理/未处理的兄弟处于半变异态（prune_contract_dependencies
    同律）。
    接线唯一咽喉=inject_build_scaffold_subtasks 末端（两遍注入+外科重试全覆盖）。
    返回 {subtask_id: {dropped_verify, added_verify, acceptance_rewritten}} 机读摘要
    （直接调用方消费；咽喉包装内以日志留痕）。
    """
    import re as _re
    summary: dict[str, dict] = {}
    for st in (getattr(plan, "subtasks", None) or []):
        try:
            tpls = _extract_auth_templates(getattr(st, "description", "") or "")
            if not tpls:
                continue
            rec = {"dropped_verify": [], "added_verify": 0, "acceptance_rewritten": 0}
            h = getattr(st, "harness", None)
            # 暂存区：全部 pom 处理完才一次性提交（异常=本子任务整体放弃，零半变异）
            staged_vcs = list(getattr(h, "verify_commands", []) or []) if h else []
            staged_acc = list(getattr(st, "acceptance_criteria", []) or [])
            for pom, tpl in tpls:
                deps = _template_dep_artifacts(tpl)
                _tpl_scan = _strip_pom_exclusions(tpl)   # 猎手 F1：冲突判定不看 <exclusions> 内的禁入项
                tpl_asserts = [f"grep -q '<artifactId>{a}</artifactId>' {pom}"
                               for a in deps]
                if h is not None:
                    kept: list[str] = []
                    for vc in staged_vcs:
                        if vc in tpl_asserts or not _is_pom_content_assert(vc, pom):
                            kept.append(vc)
                            continue
                        _s = vc.strip()
                        # R65E2-T1（round65e2 死因）：`! grep X` 与 `test -z/-n "$(grep X)"` 同为
                        # 负断言。旧识别只认 test -z/-n → st-1 的 `! grep -qE 'ruoyi-(...)'` 被漏识、
                        # 无条件 KEPT，与模板正断言 grep ruoyi-common 共存=verify 互斥死局。
                        _is_neg_grep = bool(_re.match(r"^!\s*grep", _s))
                        neg = bool(_re.match(r"^test\s+-[zn]", _s)) or _is_neg_grep
                        pat = _exam_grep_pattern(vc)
                        if neg:
                            # 排除断言（`test -z "$(grep X)"` 或 `! grep X` 皆断言"X 不得出现"）与
                            # 权威模板正面矛盾（pattern 命中模板=模板要求 X 存在）→ 剔除+留痕。
                            # `test -n`（断言 X 存在）不是排除断言，不在此列（原语义保持）。
                            _is_exclusion = _is_neg_grep or bool(_re.match(r"^test\s+-z", _s))
                            if pat and _is_exclusion and _exam_pattern_hits(pat, _tpl_scan):
                                logger.warning(
                                    "[R65D-T2] %s 验收负断言与权威模板正面矛盾"
                                    "（pattern=%r 在模板中存在）→ 剔除+留痕"
                                    "（st-26 四面矛盾死型在规划期现形；round65e2 `! grep` 变体）: %s",
                                    st.id, pat, vc)
                                rec["dropped_verify"].append(vc)
                            else:
                                kept.append(vc)   # 不冲突的负断言=禁入守卫，保留
                            continue
                        rec["dropped_verify"].append(vc)   # 陈旧正断言 → 模板重生成
                    staged_vcs = list(dict.fromkeys(kept + tpl_asserts))
                rule5_line = (f"{pom} 必须声明依赖: {sorted(deps)}"
                              "（缺一即整模块 mvn compile 失败）") if deps else ""
                _single_tpl = len(tpls) == 1
                new_acc: list[str] = []
                for a in staged_acc:
                    # 复核 LOW：契约模块名≠物理目录（R58-1）时规则5 行的路径对不上
                    # pom——唯一模板子任务兜底匹配任何规则5 机器行，杜绝新旧两行并存
                    if (("必须声明依赖" in a or "所需依赖" in a)
                            and _re.search(r"依赖: \[", a)
                            and (pom in a or a.startswith("本模块 pom.xml")
                                 or _single_tpl)):
                        if rule5_line and rule5_line not in new_acc:
                            new_acc.append(rule5_line)
                            if a != rule5_line:
                                rec["acceptance_rewritten"] += 1
                        continue
                    # R65E2-T1（猎手 F4）：acceptance 自然语言排除条目（"不包含/不得包含/零 X 依赖"）
                    # 命名了模板【要求】的依赖 → 与模板正面矛盾（round65e2 st-1 "不包含 ruoyi-common"
                    # ↔ 模板含 ruoyi-common）→ 剔除+留痕，令 acceptance 与 verify/模板同源自洽（否则
                    # 下游 CONFIRM/judge 或 worker 读 NL 条目仍被导回原矛盾）。
                    if (deps and _re.search(r"不包含|不得包含|不应包含|零\s*(?:RuoYi|ruoyi)?\s*依赖", a)
                            and any(d in a for d in deps)):
                        logger.info(
                            "[R65D-T2] %s 验收 NL 排除条目命名了模板要求的依赖 %s → 剔除"
                            "（acceptance 与模板同源，round65e2 `不包含 X` 变体）: %s",
                            st.id, [d for d in deps if d in a], a[:80])
                        rec["acceptance_rewritten"] += 1
                        continue
                    new_acc.append(a)
                auth_line = (f"依赖清单以 description 中【权威 pom 模板】（{pom}）字面为准"
                             "——模板即真值，其他验收条目与模板冲突时以模板为准")
                if auth_line not in new_acc:
                    new_acc.append(auth_line)
                    rec["acceptance_rewritten"] += 1
                staged_acc = new_acc
            # 一次性提交暂存区
            if h is not None and staged_vcs != list(getattr(h, "verify_commands", []) or []):
                rec["added_verify"] += len(
                    [v for v in staged_vcs
                     if v not in (getattr(h, "verify_commands", []) or [])])
                h.verify_commands = staged_vcs
            if staged_acc != list(getattr(st, "acceptance_criteria", []) or []):
                st.acceptance_criteria = staged_acc
            if rec["dropped_verify"] or rec["added_verify"] or rec["acceptance_rewritten"]:
                summary[st.id] = rec
                if rec["dropped_verify"]:
                    logger.info(
                        "[R65D-T2] 考卷同源重生成 %s：剔除旧内容断言 %d 条 %s → 模板依赖"
                        "断言 %d 条（模板即真值，考卷必须从模板生成）",
                        st.id, len(rec["dropped_verify"]), rec["dropped_verify"][:4],
                        rec["added_verify"])
        except Exception:  # noqa: BLE001 — 单子任务畸形绝不拖垮全计划的对账
            logger.warning(
                "[R65D-T2] %s 考卷同源对账失败（该子任务保持原样，兄弟不受影响）",
                getattr(st, "id", "?"), exc_info=True)
    return summary


def reconcile_contract_method_names(plan, shared_contract) -> dict[str, list]:
    """R67E-T1（round67e 死因治本 task 88584950）：C2 契约方法名分叉【确定性自愈】。

    契约 signature 方法名=唯一权威真值源（tech_design/contract_design 产出，比 plan 阶段
    LLM 复述的 description 更权威）。确定性把 owner 子任务 description + acceptance_criteria
    + harness.verify_commands 三面里的方法名【变体】逐字对齐到契约方法名——消除 C2 分叉，
    无需打回 LLM 重产（round67e 5 轮不收敛熔断真根：LLM 会回退重犯已修接口）。

    三面广播对齐：C2 检测只扫 description，若命中的分叉变体也出现在 AC/verify 而只改 desc →
    下游 CONFIRM/judge/worker 经 AC/verify 导回原分叉=半修复（照 R65D-T2 reconcile_template_exam
    范式）；故把 description 里判定的分叉变体【广播】到 desc + acceptance_criteria +
    harness.verify_commands 三面词边界替换。

    ★hunter F3 已知盲区★：变体检测源=owner description（与 C2 闸 detect 同判据源，validate/
    reconcile 对称）。仅在 AC/verify 里【独有】、description 从未出现的分叉变体不在本 pass 覆盖
    ——C2 闸与本自愈对此【对称失明】（不存在"本可自愈却只有 C2 发现"的错位），是 C2 体系既有
    盲区，登记 future 治理（扩展检测面会改 C2 validate 误伤边界，超本次 blast radius）。

    零误伤：只替换 C2 已判分叉的确切 (契约 c, 描述变体 t) 对（复用
    detect_contract_signature_divergences 单一判据），词边界替换防子串误伤（长变体先替，防
    短变体是长变体子串时抢替）。暂存区+单子任务 try/except：某子任务畸形绝不让兄弟半变异
    （R65D-T2 同律）。幂等（对齐后 detect 不再判分叉）。fail-open：自愈挂了 C2 闸兜底打回，
    整体失效经 out["contract_method_names_reconcile_failed"] 进 degraded_reasons 可查。
    返回 {subtask_id: [{"from": t, "to": c}, ...]} 机读摘要（调用方消费 + 日志留痕）。
    """
    import re as _re

    from swarm.brain.plan_validator import detect_contract_signature_divergences
    summary: dict[str, list] = {}
    for owner, iface_name, diverged in detect_contract_signature_divergences(
            plan, shared_contract):
        try:
            # (变体 t → 契约 c) 全量映射；长变体先替，防短变体是长变体子串时抢替（词边界之外
            # 的额外保险）。
            repl = sorted(
                ((t, c) for c, variants in diverged for t in variants),
                key=lambda p: len(p[0]), reverse=True)
            if not repl:
                continue

            def _sub(text: str, _repl=repl) -> str:
                for t, c in _repl:
                    text = _re.sub(rf"\b{_re.escape(t)}\b", c, text)
                return text

            # 暂存区：先算三面全量结果，任一面异常=本 owner 整体放弃（零半变异）。
            old_desc = getattr(owner, "description", "") or ""
            new_desc = _sub(old_desc)
            h = getattr(owner, "harness", None)
            old_vcs = list(getattr(h, "verify_commands", []) or []) if h is not None else None
            new_vcs = [_sub(v) for v in old_vcs] if old_vcs is not None else None
            old_acc = list(getattr(owner, "acceptance_criteria", []) or [])
            new_acc = [_sub(a) for a in old_acc]
            # 一次性提交（三面）。
            if new_desc != old_desc:
                owner.description = new_desc
            if h is not None and new_vcs is not None and new_vcs != old_vcs:
                h.verify_commands = new_vcs
            if new_acc != old_acc:
                owner.acceptance_criteria = new_acc
            # hunter F2(MED)：同一 owner 拥多个契约接口时按 owner.id extend 累积,不覆盖丢账。
            summary.setdefault(getattr(owner, "id", "?"), []).extend(
                {"from": t, "to": c} for t, c in repl)
            logger.info(
                "[R67E-T1] 契约方法名自愈 %s(接口 %s)：description 分叉变体广播对齐 desc/验收/"
                "verify 三面 %d 对 %s（考卷同源,消除 C2 分叉无需打回 LLM）",
                getattr(owner, "id", "?"), iface_name, len(repl),
                [f"{t}→{c}" for t, c in repl[:4]])
        except Exception:  # noqa: BLE001 — 单子任务畸形绝不拖垮全计划的自愈（R65D-T2 同律）
            # hunter F4(LOW/latent)：三面提交是顺序赋值,当前 SubTask/TaskHarness 无
            # validate_assignment 故计算阶段(全部 _sub 先于任何提交)失败=零半变异、提交阶段不抛;
            # 措辞用"自愈中止"而非"保持原样"——未来若模型加固 validate_assignment,提交中途异常
            # 可能留部分字段已变更,此时该子任务仍被 C2 闸复扫兜底(残留分叉如实打回)。
            logger.warning(
                "[R67E-T1] %s 契约方法名自愈中止（兄弟不受影响,残留分叉由 C2 闸复扫兜底）",
                getattr(owner, "id", "?"), exc_info=True)
    return summary


def reconcile_contract_symbol_paths(
    plan, file_plan, project_path: str | None = None, base_ref: str | None = None,
) -> dict[str, list]:
    """round67e Phase 2（类治）：契约类名 file-path 分叉【确定性对齐】（v1 greenfield-only，fail-closed 重）。

    死型（tier2_only 类名分叉）：契约条目 name=X（ScheduleStrategyService），owner 子任务 create_files +
    file_plan 同漂到装饰变体 V（AlarmScheduleStrategyService.java，basename tier2）→ 契约落单 → 消费方按
    契约 import X、只建了 V → L2 cannot find symbol X。pin 现状：tier2 故意不钉 → 甩执行期符号接地兜不住。

    治：finish_plan_deterministic 早位（renormalize 之后、孤儿挂靠之前）跑本 pass，把 owner
    create_files + file_plan + description/AC/verify 三面 + 契约 defined_in 对齐到契约名 X。names 转 tier0
    后 elaborate 的 pin/wire 原样接管连消费方（下游零改）。检测=detect_contract_classname_divergences（本地
    import 避免循环）。

    ★方向/撞名 六闸（全 fail-closed，v1 只做纯 greenfield）★（deep_read_findings/18 §三 + 对抗双复核整改）：
    1. project_path 存在且是目录；1b. **必须是 git repo**（.git 存在）——非 git 时 _exists_in_repo 退化活磁盘
       isfile（读未构建工作树，非 base 权威），对"改文件"这种高 blast 动作不可信，fail-closed（hunter LOW 整改）。
    2. V 或目标 T 在 git-pin base（_exists_in_repo，盲区 D）→ 棕地（改契约+消费方高 blast / owner 应 MODIFY）→ punt；
    3. 目标 stem X 已被别的子任务 create（别包同名）→ G1 _cross_package_same_basename_creates 会 REJECT → punt；
    4. 目标全路径 T 已被任何子任务 write（create∪writable）→ 写冲突 → punt；
    5. 检测阶段已保证 tier2 唯一命中 + 唯一 create owner（多命中/多 owner 歧义不返回）；
    6. **多 div 同调用内目标撞车**：create_stems/all_write 每次提交后【就地更新】——两 div 目标同路径时
       第二个被闸 3/4 逮住，不再吃陈旧快照（对抗双复核 MEDIUM 整改）。
    棕地两方向 + 歧义 + 无 project_path/非 git 全 punt（退回 pin tier2_only 现状=零回归；round67c 盲区 I：无实证绝不挑边）。

    ★未愈可见（hunter CRITICAL 整改）★：punt/畸形导致未愈的分叉【绝不静默】——finish 收尾器在本 pass 后
    重跑 detect（幂等：已愈转 tier0 消失，残留=未愈）写 last-write-wins 观测键 contract_symbol_paths_unhealed
    （always-emit，愈合=[] 清空不粘滞；★绝不进 append-only degraded_reasons：那里无人能清→陈旧粘滞，Finding B★）。
    **刻意不加硬 REJECT 回灌闸**（对称 C2 的 validate_contract_signature_source）：round67e 铁证=LLM 重产名分叉
    5 轮不收敛熔断，硬打回只会复刻该死因；file-path 分叉 LLM 更改不动（改 create_files 归属）。故"能确定性愈的愈，
    愈不了的诚实上报 degraded"（北极星：honest PARTIAL > false DONE / 熔断），非甩回 LLM。

    ★词边界铁律（reviewer HIGH 整改）★：文本三面替换用 ASCII lookaround `(?<![0-9A-Za-z_])..(?![0-9A-Za-z_])`
    而非 `\b`——CJK 表意字是 `\w`(category Lo)，`\b` 在【汉字紧贴标识符】(实现XxxService接口，中文无分隔惯例)时
    【零匹配】→ 结构名改了、三面文本没改=半修复(worker 建 X.java 却按描述命名类 V → public class/文件名不符 L1 崩)。
    与 plan_validator:565 / symbol_provenance:227 既有 ASCII lookaround 口径同源（C2 reconcile 的 `\b` 同型隐患，
    登记 future——C2 detect/reconcile 同用 `\b` 对称失明=漏检 punt 非半修复，低一档且属已发版代码，另案）。

    纪律：★原子暂存（hunter LOW/latent 整改）★——三面文本 + 全 scope 路径重写 + file_plan 改动全【先算进 staging】，
    末尾一次性提交；提交阶段纯赋值不抛（即便未来 SubTask 加 validate_assignment，跨全子任务的 scope 循环也不半变异）。
    per-div try/except（一条畸形不拖垮兄弟）；幂等；fail-open（整体失效 finish 记 out["...reconcile_failed"] 诊断 +
    独立 try 重跑 detect 仍把 crash-残留分叉纳入 unhealed 观测键，绝不静默）。
    返回 {owner_id: [{"from":V_path,"to":T_path,"symbol":X}, ...]} 机读摘要（未愈另由 finish 重跑 detect 上报）。
    """
    import os as _os
    import re as _re

    from swarm.brain.symbol_provenance import detect_contract_classname_divergences
    divs = detect_contract_classname_divergences(plan)
    if not divs:
        return {}
    subs = getattr(plan, "subtasks", None) or []
    fp_list = file_plan if isinstance(file_plan, list) else []
    # 撞名/写冲突全集：所有 create 落点的 stem（G1 别包同名闸口径）+ 所有 write 路径（写冲突口径）。多 div
    # 每提交一次就地更新（闸 6），故用可变 dict/set 贯穿循环。
    create_stems: dict[str, set[str]] = {}
    all_write: set[str] = set()
    for st in subs:
        sc = getattr(st, "scope", None)
        for f in (getattr(sc, "create_files", None) or []):
            p = _norm_scope_path(f)
            create_stems.setdefault(p.rsplit("/", 1)[-1].split(".", 1)[0], set()).add(p)
        all_write |= {_norm_scope_path(f) for f in (
            list(getattr(sc, "create_files", None) or [])
            + list(getattr(sc, "writable", None) or []))}
    cache: dict[str, bool] = {}
    _is_git = bool(project_path) and _os.path.isdir(_os.path.join(project_path or "", ".git"))
    summary: dict[str, list] = {}
    for d in divs:
        try:
            X, owner = d["symbol"], d["owner"]
            v_path, v_stem = _norm_scope_path(d["v_path"]), d["v_stem"]
            _base = v_path.rsplit("/", 1)[-1]
            _ext = _base.rsplit(".", 1)[-1] if "." in _base else "java"
            _dir = v_path.rsplit("/", 1)[0] if "/" in v_path else ""
            t_path = f"{_dir}/{X}.{_ext}" if _dir else f"{X}.{_ext}"
            # ── 六闸（全 fail-closed）──
            if t_path == v_path:
                continue
            if not project_path or not _os.path.isdir(project_path) or not _is_git:
                continue                           # 闸 1/1b：非目录/非 git → 无 base 权威 → 绝不改文件
            if (_exists_in_repo(project_path, v_path, cache, base_ref)
                    or _exists_in_repo(project_path, t_path, cache, base_ref)):
                continue                           # 闸 2：棕地任一方向 → punt
            if (create_stems.get(X, set()) - {v_path, t_path}):
                continue                           # 闸 3/6：目标 stem 撞别的子任务 create（含前序 div 已提交）
            if t_path in (all_write - {v_path}):
                continue                           # 闸 4/6：目标全路径撞别人 write（含前序 div 已提交）
            # ── 原子暂存：三面文本 + 全 scope 重写 + file_plan 改动全先算，末尾一次性提交 ──
            def _sub(text, _v=v_stem, _x=X):       # ASCII lookaround，防 CJK 紧贴半修复（reviewer HIGH）
                return _re.sub(rf"(?<![0-9A-Za-z_]){_re.escape(_v)}(?![0-9A-Za-z_])", _x, text)
            old_desc = getattr(owner, "description", "") or ""
            new_desc = _sub(old_desc)
            h = getattr(owner, "harness", None)
            old_vcs = list(getattr(h, "verify_commands", []) or []) if h is not None else None
            new_vcs = [_sub(v) for v in old_vcs] if old_vcs is not None else None
            old_acc = list(getattr(owner, "acceptance_criteria", []) or [])
            new_acc = [_sub(a) for a in old_acc]
            # 全 scope 路径重写 v_path→t_path（owner create 权不变仅改名；兄弟悬空 readable/upstream/delete
            # 同步归一，无悬空）——含 delete_files（reviewer LOW：FileScope.is_writable 也查 delete_files）。
            scope_changes: list = []               # (scope_obj, attr, new_list)
            for st in subs:
                sc = getattr(st, "scope", None)
                if sc is None:
                    continue
                for attr in ("create_files", "writable", "readable",
                             "upstream_artifacts", "delete_files"):
                    lst = getattr(sc, attr, None)
                    if not lst:
                        continue
                    new, seen, changed = [], set(), False
                    for f in lst:
                        nf = t_path if _norm_scope_path(f) == v_path else f
                        if nf != f:
                            changed = True
                        k = _norm_scope_path(nf)
                        if k not in seen:
                            seen.add(k)
                            new.append(nf)
                    if changed:
                        scope_changes.append((sc, attr, new))
            # file_plan 归一（dict {"path":...} 与 bare-str 混合形态都要处理——normalized_file_plan_paths
            # 两种都读；只改 dict 会漏 str → R40-1 判孤儿把旧名复活成重复，hunter HIGH）。
            fp_changes: list = []                  # (index, new_value)
            for _i, e in enumerate(fp_list):
                if isinstance(e, dict):
                    if _norm_scope_path(str(e.get("path") or "")) == v_path:
                        fp_changes.append((_i, {**e, "path": t_path}))
                elif isinstance(e, str) and _norm_scope_path(e) == v_path:
                    fp_changes.append((_i, t_path))
            # ── 一次性提交（纯赋值，不抛；跨全子任务无半变异）──
            for _sc, _attr, _new in scope_changes:
                setattr(_sc, _attr, _new)
            for _i, _val in fp_changes:
                fp_list[_i] = _val
            if new_desc != old_desc:
                owner.description = new_desc
            if h is not None and new_vcs is not None and new_vcs != old_vcs:
                h.verify_commands = new_vcs
            if new_acc != old_acc:
                owner.acceptance_criteria = new_acc
            d["item"]["defined_in"] = t_path       # 显式钉，不依赖 elaborate pin 再推导
            # 闸 6：就地更新撞名/写冲突全集，后续 div 见一致视图
            create_stems.setdefault(X, set()).add(t_path)
            _vs = create_stems.get(v_stem)
            if _vs is not None:
                _vs.discard(v_path)
            all_write.discard(v_path)
            all_write.add(t_path)
            summary.setdefault(getattr(owner, "id", "?"), []).append(
                {"from": v_path, "to": t_path, "symbol": X})
            logger.info(
                "[R67E-P2] 契约类名 file-path 对齐 %s：%s→%s（契约名权威，greenfield 磁盘判方向）"
                "——create_files+file_plan+desc/AC/verify 三面+defined_in 同步，names 转 tier0 交 pin/wire 接管消费方",
                getattr(owner, "id", "?"), v_stem, X)
        except Exception:  # noqa: BLE001 — 一条畸形不拖垮兄弟（R65D-T2/C2 同律），残留由 finish 重跑 detect 上报 degraded
            logger.warning(
                "[R67E-P2] %s 类名 file-path 对齐中止（兄弟不受影响，残留分叉退回 pin tier2_only 现状）",
                d.get("symbol"), exc_info=True)
    return summary


def _narrow_grep_scan_paths(cmd: str, declared: set[str], owned_areas: set[str]) -> str | None:
    """DR-PM66-C4(#111) 单命令收敛：含 grep 的内容断言命令，剔除【外模块 scope 泄漏】的路径参数。

    ★对抗双复核 CONFIRMED HIGH×2 整改★——正确不变量不是"scope ⊆ writable 文件"（过严，会把
    合法的【整模块目录级基线断言】如 `! grep -rq lombok <module>/`〔挂脚手架子任务、只 owns pom.xml、
    却故意扫全模块〕静默收窄成对自身空转；也会把针对 readable 契约文件的验证断言误删）。改为保留：
      · 本子任务【声明的文件】（writable ∪ create_files ∪ readable，含只读契约文件）；或
      · 本子任务【自身物理模块区域】内的目录/文件（含整模块基线断言，如 lombok 禁令，合法跨兄弟）。
    只剔除【外模块】scope 泄漏参数（子任务无立场的另一顶层模块）；全部越界→None（整条剔除）。
    绝不改写目录扫描的语义（保留原样，不再展开为文件列表）；绝不动构建命令（无 grep）。"""
    import re as _re
    if "grep" not in cmd:
        return cmd
    # 抽 grep 调用：flags* + 引号 pattern + 其后 path 段（到 ) | ; & > 之前）
    m = _re.search(
        r"grep\b(?P<flags>(?:\s+-{1,2}[\w=-]+)*)\s+"
        r"(?P<q>['\"])(?P<pat>.*?)(?P=q)(?P<paths>[^)|;&>]*)", cmd)
    if not m:
        return cmd            # 非常见形态 → 不动（保守 fail-open，绝不误改成永假）
    toks = [t for t in m.group("paths").split() if t and not t.startswith("-")]
    if not toks:
        return cmd            # grep 无显式路径参数（管道/stdin）→ 不动
    kept: list[str] = []
    changed = False
    for p in toks:
        np = _norm_scope_path(p)
        top = np.split("/", 1)[0] if "/" in np else np
        if np in declared or top in owned_areas:
            kept.append(p)                   # 声明文件 或 本模块区域（含整模块目录级断言）→ 原样保留
        else:
            changed = True                   # 外模块 scope 泄漏 → 剔除该参数
    if not kept:
        return None          # 全部越出声明 scope + 本模块区域 → 整条剔除（不能被外模块内容判死）
    if not changed:
        return cmd
    seen: set[str] = set()
    kept = [x for x in kept if not (x in seen or seen.add(x))]
    return cmd[:m.start("paths")] + " " + " ".join(kept) + cmd[m.end("paths"):]


# DR-10-F1(#102)：源码 forbidden-import 负断言的【裸包前缀】分支识别正则——【首段小写】(Java/Kotlin/
# Scala 包名铁律=小写；类名 CamelCase 大写开头) + 纯标识符+转义点、以 `\.` 结尾（如 javax\. /
# jakarta\. / com\.foo\.），且不含 import 关键字。
# ★复核 CONFIRMED HIGH 整改★：首段必须小写——排除 `Runtime\.` / `System\.` / `Math\.` 这类
# 【java.lang.* 类名 API 禁令】（这些类【自动导入·从不写 import`Runtime.exec()`】，若锚定到 import
# 上下文会令模式【永不匹配】→ 该 API 禁令永久失效=假 DONE）。类名 API 负断言不属"import 禁令"语义，
# 保持原样（本就该匹配代码任意位置的调用）。方法名(结尾 `(`)/字符串/已锚定(含 import)分支亦不匹配。
_PKG_PREFIX_BRANCH_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\\\.[A-Za-z0-9_]+)*\\\.$")


# DR-10-F1(#102) import 锚定前缀：`^[[:space:]]*import[[:space:]].*`
# = 行首(可缩进) + import 关键字 + ≥1 空白 + 任意（覆盖 `import static <pkg>`）。
# ★可移植性铁律★：用 POSIX 字符类 `[[:space:]]` 而非 GNU 扩展 `\s`——负断言在【沙箱】跑(grep 可能
# 是 busybox/BSD，不认 `\s`)，`\s` 会令模式【永不匹配】→ 真 import javax 也放行=放松禁令假 DONE。
# `[[:space:]]`/`.*`/`^`/`*` 在 BRE/ERE/GNU/busybox/BSD 全通用（刻意不用 `(?:...)?` 分组——ERE 用
# `(...)`、BRE 用 `\(...\)`、`(?:)` 是 PCRE，均不可移植；`import[[:space:]].*` 用 `.*` 覆盖 `static`）。
# 行首锚定(`^`)杜绝"// import javax 是禁止的"这类注释里含 import 关键字的边缘假阴性。
# ★复核 CONFIRMED（已知取舍·非改动引入死面）★：锚定把语义从"禁 javax 出现在任意位置"收敛为"禁
# import javax 语句"——【FQN 内联用法】(`javax.servlet.X x=...`、`@lombok.Data`，无 import 行)会逃逸。
# 但：①0-baseline(javax 不在 classpath)下 FQN 用法【编译必失败】→ 编译闸兜底；②"禁 import 某包"正是
# 负断言原意；③可靠区分"代码 FQN 用法"vs"注释里的 FQN"超出 grep 能力。故 FQN 逃逸列为已知限制。
_IMPORT_ANCHOR_PREFIX = r"^[[:space:]]*import[[:space:]].*"


def _anchor_forbidden_import_pattern(pattern: str, allow_bare_word: bool = False) -> str:
    """把 grep 负断言 pattern 里的【裸包前缀】分支锚定到 import 上下文，把【已含 import 关键字但未
    行首锚定】的分支(如 `import lombok`)补行首锚。非包前缀(方法名/字符串/结尾非 `\\.`)/已行首锚定
    分支原样。

    #102：`javax\\.` 裸包前缀是【行内子串】匹配→命中注释/散文里的 `javax.`（worker 写"本类无
    javax.*"表功注释被判死=假阴性杀好产出）。#102-复核C（修一类捞 sibling）：同一 pattern 的
    `import lombok` 分支同病（命中"无需 import lombok"注释），也补行首锚。栈中立。

    R67-T7b 扩 sibling（round67 R67-9）：①分支切分兼容 BRE `\\|` 交替（旧 split("|") 会把
    `vue\\|Vue` 切成尾带反斜杠的碎片，所有分支既锚不上也不放行——幂等失效面）；②allow_bare_word
    =True（调用方已证目标是 JVM 源码）时，【裸小写单词】禁令分支（lombok/vue）也锚 import——
    `! grep -rl 'lombok' <java目录>` 命中"不使用 lombok"注释即假阳杀，而 Java 侧真实使用
    必有 import 行（FQN 内联逃逸=已知限制，0-baseline 编译闸兜底，同 #102 取舍）。"""
    import re as _re
    # R67-T7b①：保分隔符切分（`\|`=BRE 交替 / `|`=ERE 交替），分支与分隔符各归其位
    parts = _re.split(r"(\\\||\|)", pattern)
    out: list[str] = []
    changed = False
    for idx, b in enumerate(parts):
        if idx % 2 == 1:                                    # 分隔符原样
            out.append(b)
            continue
        if b.startswith("^"):
            out.append(b)                                   # 已行首锚定 → 幂等跳过
        elif _PKG_PREFIX_BRANCH_RE.match(b):
            out.append(_IMPORT_ANCHOR_PREFIX + b)           # 裸包前缀(javax\.)→ 全 import 上下文锚
            changed = True
        elif _re.match(r"^import\s", b):
            # 复核 C：已含 import 关键字但未行首锚定（`import lombok`）→ 仅补行首锚，杜绝命中
            # "// 无需 import lombok" 散文；不改其余（它已限定 import 语义，只差行首约束）。
            out.append(r"^[[:space:]]*" + b)
            changed = True
        elif allow_bare_word and _re.fullmatch(r"[a-z][a-z0-9_]{2,}|[A-Z][a-z0-9_]{2,}", b):
            # R67-T7b②：裸词禁令锚 import。判据（全量回归 test_b4b 逮到的真界线）：
            # 依赖/技术名=全小写（lombok/vue）或单峰首大写（Lombok，注释专属词，复核 MED）
            # → 锚 import；【内部驼峰】（getGroups/createApp）=方法/API 名负断言（"不得有
            # 该方法"须匹配代码任意位置），锚 import=永假=放松禁令假过，绝不动。
            out.append(_IMPORT_ANCHOR_PREFIX + b)
            changed = True
        else:
            out.append(b)
    return "".join(out) if changed else pattern


def _anchor_forbidden_import_in_cmd(cmd: str) -> str:
    """对【负断言】源码命令（`! grep …` / `test -z|-n "$(grep …)"`）的 grep pattern 做 import 锚定。
    非负断言/非 grep/解析不出 → 原样（fail-open，绝不误改成永假）。仅重写裸包前缀分支。"""
    import re as _re
    # 复核 D 整改：只认【真·负断言】——`! grep`（取反）与 `test -z "$(grep …)"`（断言输出为空=
    # 模式不得出现）。★绝不含 `test -n`★——`test -n` 断言输出【非空】=模式【必须出现】=正面存在
    # 断言（如"必须 import 某拦截器"），语义与 forbidden-import 相反，锚定它会把"子串出现"要求
    # 收紧成"必须是 import 语句行"→ 合法 FQN/异形满足被冤杀。
    if not (_re.match(r"^\s*!\s*grep\b", cmd) or _re.search(r"\btest\s+-z\b.*grep", cmd)):
        return cmd            # 只锚【负断言】——正面 grep/`test -n` 存在断言语义不同，不动
    # R67-T7b②：裸词禁令锚 import 仅当扫描目标可证为 JVM 类路径源码（路径含 JVM 布局段或
    # .java/.kt 后缀）——html/js 等资源目标 import 语义不适用，fail-open 原样（st-52 vue 类
    # 禁令列为已知限制：资源侧注释假阳交 deliver/manual 面）。
    _java_target = bool(_re.search(
        r"/(?:java|kotlin|scala|groovy)/|\.java\b|\.kt\b|\.scala\b|\.groovy\b", cmd))
    m = _re.search(r"grep\b(?:\s+-{1,2}[\w=-]+)*\s+(?P<q>['\"])(?P<pat>.*?)(?P=q)", cmd)
    if m:
        new_pat = _anchor_forbidden_import_pattern(m.group("pat"), allow_bare_word=_java_target)
        if new_pat == m.group("pat"):
            return cmd
        return cmd[:m.start("pat")] + new_pat + cmd[m.end("pat"):]
    # R67-T7b：未引号单词 pattern（`! grep -rq lombok dir/`）——锚定后含 [[:space:]] 必须补引号
    m = _re.search(r"grep\b(?:\s+-{1,2}[\w=-]+)*\s+(?P<pat>[^\s'\"]+)", cmd)
    if not m:
        return cmd
    new_pat = _anchor_forbidden_import_pattern(m.group("pat"), allow_bare_word=_java_target)
    if new_pat == m.group("pat"):
        return cmd
    return cmd[:m.start("pat")] + "'" + new_pat + "'" + cmd[m.end("pat"):]


def anchor_forbidden_import_asserts(plan) -> dict[str, dict]:
    """DR-10-F1(#102) 治本：源码 forbidden-import 负断言的裸包前缀分支锚定到 import 上下文，
    杜绝命中注释/散文的假阴性判死好产出（round66 st-29/st-32 实证：worker 写"无 javax.*"注释被杀）。

    确定性、幂等、零 LLM、栈中立、fail-open（解析异常/未知形态原样）。与 sanitize_verify_scope(#111)
    同咽喉：#111 收敛【扫描范围】(跨模块泄漏)，本闸锚定【匹配模式】(import 上下文)，两者正交互补。
    返回 {subtask_id: {anchored: [(旧,新)…]}} 机读摘要。"""
    summary: dict[str, dict] = {}
    for st in (getattr(plan, "subtasks", None) or []):
        h = getattr(st, "harness", None)
        vcs = list(getattr(h, "verify_commands", []) or []) if h else []
        if not vcs:
            continue
        new_vcs: list[str] = []
        anchored: list[tuple[str, str]] = []
        for vc in vcs:
            try:
                out = _anchor_forbidden_import_in_cmd(vc)
            except Exception:  # noqa: BLE001 — 单命令解析异常绝不拖垮全 plan，留痕后保留原命令
                logger.warning(
                    "[IMPORT-ANCHOR] #102 %s 负断言 import 锚定异常，保留原命令: %s",
                    getattr(st, "id", "?"), vc, exc_info=True)
                new_vcs.append(vc)
                continue
            if out != vc:
                anchored.append((vc, out))
            new_vcs.append(out)
        if anchored and h is not None:
            h.verify_commands = new_vcs
            summary[getattr(st, "id", "?")] = {"anchored": anchored}
    return summary


def sanitize_verify_scope(plan) -> dict[str, dict]:
    """DR-PM66-C4(#111) 治本：源码内容断言（grep/test 类）的扫描路径不得越出【本子任务声明 scope +
    本子任务自身物理模块区域】。否则一个子任务的成败被【外模块产物】决定=保证性假阴性死局。

    ★对抗双复核整改★：判据从"⊆ writable 文件"放宽为"声明文件（writable∪create_files∪readable）∪
    本模块区域"——保住合法的整模块基线断言（lombok 禁令）与 readable 契约文件验证，只剔除外模块泄漏。
    round66 st-32 的真根（负断言未锚定 import 命中注释）由 #102 治，本闸只堵【跨模块】责任错配。
    确定性、幂等、零 LLM；绝不动构建命令（mvn/gradle/go build，无 grep，模块级合法，见黄灯）。栈中立。
    返回 {subtask_id: {narrowed, dropped}} 机读摘要。"""
    summary: dict[str, dict] = {}
    for st in (getattr(plan, "subtasks", None) or []):
        h = getattr(st, "harness", None)
        vcs = list(getattr(h, "verify_commands", []) or []) if h else []
        if not vcs:
            continue
        sc = getattr(st, "scope", None)
        _wr = [f for f in (list(getattr(sc, "writable", None) or [])
                           + list(getattr(sc, "create_files", None) or [])) if str(f).strip()]
        # 声明文件=可写∪新建∪只读（含 readable 契约文件，猎手 CONFIRMED：只读契约验证断言不得误删）
        declared = {_norm_scope_path(f) for f in
                    (_wr + [f for f in (getattr(sc, "readable", None) or []) if str(f).strip()])}
        # 本模块区域=可写/新建文件的顶层模块段（整模块目录级基线断言合法，复核 CONFIRMED：lombok 禁令）
        owned_areas = {_norm_scope_path(f).split("/", 1)[0]
                       for f in _wr if "/" in _norm_scope_path(f)}
        if not declared and not owned_areas:
            continue          # 子任务无任何声明 scope（allow_any/纯删除等）→ 无从判定 → 不动
        new_vcs: list[str] = []
        rec = {"narrowed": [], "dropped": []}
        for vc in vcs:
            try:
                out = _narrow_grep_scan_paths(vc, declared, owned_areas)
            except Exception:  # noqa: BLE001 — 单命令解析异常绝不拖垮全 plan，但必留痕（猎手 CONFIRMED）
                logger.warning(
                    "[VERIFY-SCOPE] DR-PM66-C4(#111) %s 单命令解析异常，保留原命令不动: %s",
                    getattr(st, "id", "?"), vc, exc_info=True)
                new_vcs.append(vc)
                continue
            if out is None:
                rec["dropped"].append(vc)
                logger.warning(
                    "[VERIFY-SCOPE] DR-PM66-C4(#111) %s 内容断言扫描路径全部越出【本子任务声明 scope"
                    "＋本模块区域】=外模块泄漏（会被外模块产物判死）→ 剔除整条: %s",
                    getattr(st, "id", "?"), vc)
            elif out != vc:
                rec["narrowed"].append((vc, out))
                new_vcs.append(out)
            else:
                new_vcs.append(vc)
        if (rec["narrowed"] or rec["dropped"]) and h is not None:
            h.verify_commands = new_vcs
            summary[getattr(st, "id", "?")] = rec
    return summary


def inject_build_scaffold_subtasks(
    plan, project_path: str | None = None, file_plan: list | None = None,
) -> list[dict]:
    """R65D-T2 咽喉包装：注入（两遍/外科重试全走这里）后必跑考卷同源 reconcile + 验收作用域收敛。"""
    injected = _inject_build_scaffold_subtasks_impl(plan, project_path, file_plan)
    try:
        _exam = reconcile_template_exam(plan)
        if _exam:
            logger.info("[R65D-T2] 考卷同源对账完成：%d 个子任务被重写 %s",
                        len(_exam), sorted(_exam)[:8])
    except Exception:  # noqa: BLE001 — fail-open，考卷维持原样交 worker 侧 H1 兜底
        logger.warning("[R65D-T2] 考卷同源 reconcile 失败（fail-open）", exc_info=True)
    try:
        _vs = sanitize_verify_scope(plan)   # DR-PM66-C4(#111)
        if _vs:
            logger.info("[VERIFY-SCOPE] #111 验收作用域收敛：%d 个子任务被收敛/剔除 %s",
                        len(_vs), sorted(_vs)[:8])
    except Exception:  # noqa: BLE001 — fail-open，越界断言维持原样（不引入新致死面）
        logger.warning("[VERIFY-SCOPE] #111 验收作用域收敛失败（fail-open）", exc_info=True)
    try:
        _ia = anchor_forbidden_import_asserts(plan)   # DR-10-F1(#102)
        if _ia:
            logger.info("[IMPORT-ANCHOR] #102 forbidden-import 负断言锚定：%d 个子任务被锚定 %s",
                        len(_ia), sorted(_ia)[:8])
    except Exception:  # noqa: BLE001 — fail-open，负断言维持原样（不引入新致死面）
        logger.warning("[IMPORT-ANCHOR] #102 forbidden-import 锚定失败（fail-open）", exc_info=True)
    return injected


# ═══════════════════════════════════════════════════════════════════════════════
# #31-Phase2b/2c：npm / go 脚手架 driver（栈中立铺开，Maven 路径零改动）
#
# 病根（G9 铺开）：round39 起脚手架注入【只认 Maven】——只会造 pom.xml。已知非 Maven 栈此前
# 明确 no-op（绝不拿 pom 污染异栈），但那留下一个洞：npm/go 工程的规则5 落空模块**没有任何
# 确定性构建清单出口**，回到"派 worker 去手写 package.json/go.mod + 臆造依赖版本"的 R47/R53 病。
# 本 driver 给 npm/go 补上等价的确定性 per-module 清单脚手架：版本经 npm/go registry 解析
# （绝不臆造，见 npm_registry/go_registry），内部包/module 走 workspace:*/replace（零网络）。
#
# ★与 Maven 的诚实差异（非偷懒，是栈语义差异）★：Maven 的聚合父 pom / <modules> reactor /
# 孤儿模块补 pom 全是 **Maven reactor 专属机制**（npm 用根 package.json 的 workspaces glob、
# go 用 go.work use，二者本就无"父 POM 下钻找子 pom"的失败模式）→ 本 driver 只做**每模块清单
# 注入**，不复制 reactor 机械。Maven 路径（下方 _inject_build_scaffold_subtasks_impl）保持字节
# 不变，本 driver 是独立分派分支。
# ═══════════════════════════════════════════════════════════════════════════════

def _manifest_owner_subtask(subtasks, manifest_rel: str):
    """某 manifest 相对路径是否已被某子任务认领（create/writable）。栈中立、路径归一后比。"""
    for st in subtasks:
        sc = getattr(st, "scope", None)
        files = (list(getattr(sc, "create_files", None) or [])
                 + list(getattr(sc, "writable", None) or []))
        if any(_norm_scope_path(f) == manifest_rel for f in files):
            return st
    return None


def _wire_scaffold_ownership(plan, sid: str, mdir: str, manifest_rel: str) -> None:
    """R57-6 栈中立复用：脚手架独占本模块清单写权 + 同目录写代码子任务 depends_on 脚手架。
    （多写者→MERGE 反复 rebase 不收敛；确定性模板会被 LLM 手写版顶掉——收回写权从源头消除。）"""
    prefix = mdir.rstrip("/") + "/"
    for st in plan.subtasks:
        if st.id == sid or str(st.id).startswith("st-scaffold-"):
            continue   # 绝不从别的脚手架手里抢（R59-2：击鼓传花到没人拥有）
        sc = getattr(st, "scope", None)
        if sc is None:
            continue
        for _attr in ("create_files", "writable"):
            _lst = getattr(sc, _attr, None)
            if not _lst:
                continue
            _keep = [f for f in _lst if _norm_scope_path(f) != manifest_rel]
            if len(_keep) != len(_lst):
                logger.warning(
                    "[SCAFFOLD-INJECT] #31-P2 从 %s 的 %s 收回构建清单写权 %s → 脚手架 %s 独占",
                    st.id, _attr, manifest_rel, sid)
                setattr(sc, _attr, _keep)
    for st in plan.subtasks:
        if st.id == sid:
            continue
        sc = getattr(st, "scope", None)
        writes = (list(getattr(sc, "create_files", None) or [])
                  + list(getattr(sc, "writable", None) or []))
        if any(str(f).replace("\\", "/").lstrip("/").startswith(prefix)
               for f in writes) and sid not in st.depends_on:
            st.depends_on.append(sid)


def split_manifest_owner_leaf(plan) -> list[dict]:
    """R67C-T3b（round67c 开箱）：把【混合了代码、且倒挂在同模块编译者之后】的 manifest 写者
    拆成独立早叶 pom-owner，消除 pom-依赖-写倒挂 + 成环死型。

    实锤：st-7-2-1 唯一写 ruoyi-framework/pom.xml（加 googleauth 外部依赖）却也写
    SysLoginService.java、且 depends_on st-7-1-1（GoogleAuthUtils 创建者，import googleauth）；
    st-7-1-1 不依赖 st-7-2-1 → GoogleAuthUtils 先于 googleauth 入 pom 编译 → L1 崩。反加边
    st-7-1-1→st-7-2-1 会成环（st-7-2-1 已依赖 st-7-1-1）。唯一解=把 pom-写抽成【无代码依赖】早叶
    W'，同模块全部编译者与原任务皆依赖 W'（复用 _wire_scaffold_ownership），W' 由后续 R58-3 嵌
    权威模板（本 pass 必跑在 inject_build_scaffold_subtasks 之前）。栈中立：manifest=_BUILD_MANIFESTS，
    代码=classpath_fqn_key。幂等（W' 已存在则跳）。条件三者全真才拆（避免无谓拆分）：① W 写模块 M
    的 manifest；② W 另有 classpath 源码产出（混合）；③ M 的某编译者 C 使 W 传递依赖 C（反向 wire 成环）。
    """
    from swarm.types import FileScope, SubTask, TaskIntent
    subs = list(getattr(plan, "subtasks", None) or [])
    if len(subs) < 2:
        return []
    by_id = {s.id: s for s in subs}

    def _reaches(a: str, b: str) -> bool:
        stack = list(getattr(by_id.get(a), "depends_on", None) or [])
        seen: set[str] = set()
        while stack:
            d = stack.pop()
            if d == b:
                return True
            if d in seen or d not in by_id:
                continue
            seen.add(d)
            stack.extend(getattr(by_id.get(d), "depends_on", None) or [])
        return False

    existing_ids = {s.id for s in subs}
    out: list[dict] = []
    for W in list(subs):
        sc = getattr(W, "scope", None)
        if sc is None:
            continue
        _all = (list(getattr(sc, "create_files", None) or [])
                + list(getattr(sc, "writable", None) or []))
        _manifests = [p for p in _all
                      if _evidence_class(p) == _EV_MANIFEST]
        _CODE_CLS = (_EV_STRONG, _EV_WEAK_CODE)
        # ★"是否代码"一律用 _evidence_class，不用 classpath_fqn_key（后者对默认包 .java 返 None
        # =误当资源，T3a 全量回归实锤）。混合=有 manifest 也有编译源码。
        if not _manifests or not any(_evidence_class(p) in _CODE_CLS for p in _all):
            continue                     # 无 manifest 或非混合（纯 manifest owner 无成环之虞）→ 不拆
        for man in _manifests:
            man_rel = _norm_scope_path(man)
            mdir = man_rel.rsplit("/", 1)[0] if "/" in man_rel else ""
            mod = mdir.rstrip("/").rsplit("/", 1)[-1] if mdir else ""
            if not mod:
                continue
            _prefix = mdir.rstrip("/") + "/"
            compilers = [s.id for s in subs if s.id != W.id and any(
                _evidence_class(f) in _CODE_CLS
                and str(f).replace("\\", "/").lstrip("/").startswith(_prefix)
                for f in (list(getattr(getattr(s, "scope", None), "create_files", None) or [])
                          + list(getattr(getattr(s, "scope", None), "writable", None) or [])))]
            # ★hunter 二轮整改★：只在【同模块全部编译者都已（传递）依赖 W】时才安全不拆——那时 pom
            # 必先于所有编译写好。否则（反向边 W→C 成环风险 / 或 W 与 C 无序=无边 race，两者 C 都可能
            # 先于 pom 编译）都要拆：把 pom 抽早叶，全部编译者经 _wire_scaffold_ownership 依赖它。旧版
            # 只判反向边 any(_reaches(W,c)) 漏了无边 race（W 与同模块兄弟无 depends 边→并行→C 先编 L1 崩）。
            if compilers and all(_reaches(c, W.id) for c in compilers):
                continue                 # 全部编译者已在 W 下游→pom 必先写→安全，不拆
            if not compilers:
                continue                 # 本模块无其他编译者→无 race
            sid = f"{W.id}-pom-{mod}"     # ★含 mod★：一个 W 写多模块 manifest 时每模块各拆一叶
            if sid in existing_ids:       # （旧 f"{W.id}-pom" 会让第 2 个模块撞名被跳过=静默漏拆）
                continue
            _in_create = any(_norm_scope_path(x) == man_rel
                             for x in (getattr(sc, "create_files", None) or []))
            leaf = SubTask(
                id=sid,
                description=(f"【构建脚手架·T3b 拆分】独立写模块 {mod} 的构建清单 {man_rel}：声明该"
                            "模块契约依赖全部坐标（含新增外部依赖）。本子任务只写清单、不写代码、无上游"
                            "依赖，必先于同模块任何编译（权威模板由脚手架注入）。"),
                intent=TaskIntent.CREATE if _in_create else TaskIntent.MODIFY,
                difficulty=SubTaskDifficulty.TRIVIAL,
                scope=FileScope(writable=[] if _in_create else [man_rel],
                                create_files=[man_rel] if _in_create else []),
                acceptance_criteria=[f"{man_rel} 声明该模块契约依赖坐标，模块可编译"],
                depends_on=[],
            )
            plan.subtasks.append(leaf)
            existing_ids.add(sid)
            subs.append(leaf)
            by_id[sid] = leaf
            sc.create_files = [x for x in (getattr(sc, "create_files", None) or [])
                               if _norm_scope_path(x) != man_rel]
            sc.writable = [x for x in (getattr(sc, "writable", None) or [])
                           if _norm_scope_path(x) != man_rel]
            _wire_scaffold_ownership(plan, sid, mdir, man_rel)   # 编译者+W 皆依赖 W'（W' 无 code dep→不成环）
            if plan.parallel_groups:
                plan.parallel_groups.insert(0, [sid])
            out.append({"orig": W.id, "leaf": sid, "module": mod})
    if out:
        logger.warning(
            "[SCAFFOLD-INJECT] R67C-T3b pom-写倒挂拆分：%d 个混合 manifest 写者的构建清单抽成早叶"
            "（同模块编译者先拿 pom 再编译，破 googleauth 型倒挂+成环）: %s", len(out), out)
    return out


def _npm_module_name(project_path: str | None, mdir: str, module_label: str) -> str:
    """内部 workspace 包名：磁盘已有 package.json 的 name 字段（事实来源）优先；greenfield →
    模块标签（我们为**新建包**自定的确定性命名约定，非臆造外部包名——红线是"绝不猜别人发布的
    包名"，给自己造的新包命名不在此列，且全 driver 统一用同一约定 → 内部互引可自洽匹配）。"""
    if project_path:
        pj = Path(project_path) / mdir / "package.json"
        try:
            if pj.is_file():
                name = json.loads(pj.read_text("utf-8", errors="replace")).get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
        except (OSError, ValueError):
            pass
    return module_label


def _render_package_json(name: str, deps) -> str:
    body: dict = {"name": name, "version": "0.0.0", "private": True}
    if deps:
        body["dependencies"] = {d.name: d.spec for d in deps}
    return json.dumps(body, indent=2, ensure_ascii=False) + "\n"


def _contract_dep_entries(plan, dirs) -> list[dict]:
    """全部【有物理落点】的契约依赖模块 [{module,dir,artifacts}]（含 manifest 已被认领者）。

    ★对抗双复核 HIGH（cr#1/#2、hunter#1/#2）★：owner-backfill 与内部包/module 标识全集都必须看
    【全物理模块集】，绝不只看 unclaimed——只看 unclaimed 会让【已认领/跨 replan 轮】的内部模块被
    当第三方送去 registry/proxy 误解析（npm：拉到同名的无关公网包；go：解析失败误报"版本问题"）。"""
    shared = getattr(plan, "shared_contract", None) or {}
    deps_spec = shared.get("dependencies") if isinstance(shared, dict) else None
    if not (isinstance(deps_spec, list) and deps_spec):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for entry in deps_spec:
        if not isinstance(entry, dict):
            continue
        mod = (entry.get("module") or "").strip().rstrip("/")
        if not mod or mod in seen or mod not in dirs:
            continue
        seen.add(mod)
        out.append({"module": mod, "dir": dirs[mod],
                    "artifacts": [a for a in (entry.get("artifacts") or []) if a]})
    return out


def _prune_scaffold_contract_entry(plan, mod: str, final_names: list[str], dropped: list) -> None:
    """同源剪 plan.shared_contract（★hunter HIGH #3★：npm/go 分支在 prune_contract_dependencies
    之前 return，dropped 仍留在【注入每个 worker 的只读契约】里=「模板没有、验收却要求」的 round63
    考卷矛盾，逼 worker 复入臆造版本）。把该模块契约 artifacts 覆写成【最终落地清单】、dropped 记
    pruned_artifacts 账。

    ★hunter NEW HIGH（schema 收敛）★ pruned_artifacts 必须与 Maven prune_contract_dependencies
    (:952-961) **同键同形**——dict `{module: [dropped]}`、按轮重建（复议语义）、全解析则撤账、空则
    整键删。绝不用 list 撞它的 dict：unknown→Maven 回退轮会先 seed 成 dict，本分支后跑若写 list →
    ①Maven 侧 `ledger[mod]=` 对 list 崩（TypeError 掀掉整轮 Maven 脚手架），②或本侧 isinstance
    检查静默丢账。既有非 dict（历史脏值）→ 响亮重置（绝不静默吞 schema 漂移）。"""
    shared = getattr(plan, "shared_contract", None)
    if not isinstance(shared, dict):
        return
    for entry in (shared.get("dependencies") or []):
        if isinstance(entry, dict) and (entry.get("module") or "").strip().rstrip("/") == mod:
            if "artifacts_pre_prune" not in entry:
                entry["artifacts_pre_prune"] = [a for a in (entry.get("artifacts") or []) if a]
            entry["artifacts"] = list(final_names)
    led = shared.get("pruned_artifacts")
    if led is not None and not isinstance(led, dict):
        logger.warning("[#31-P2] pruned_artifacts 既有非 dict（%s）→ 重置为 dict（与 Maven 账本 "
                       "schema 收敛，防跨机制类型冲突）", type(led).__name__)
        shared.pop("pruned_artifacts", None)
    led = shared.setdefault("pruned_artifacts", {})
    if dropped:
        led[mod] = [str(d) for d in dropped]     # 按轮重建，不跨轮累积（与 Maven 同律）
        shared.setdefault(
            "pruned_artifacts_note",
            "pruned_artifacts 中的坐标已证无法确定性解析，已从模板与验收标准剔除；请勿在构建"
            "清单手写声明它们——若源码确实需要，构建修复会按真实 import 反查合法坐标补入")
    else:
        led.pop(mod, None)                        # 本轮全可解析 → 撤账（瞬时误剪自愈）
    if not led:
        shared.pop("pruned_artifacts", None)
        shared.pop("pruned_artifacts_note", None)


def _p2_wrap(manifest_rel: str, block: str) -> str:
    """sentinel 包裹确定性清单机器块，供 owner-backfill 跨 replan 轮可靠 strip（幂等/刷新）。"""
    return f"\n<!--#31P2 {manifest_rel}-->{block}\n<!--#31P2-end {manifest_rel}-->"


def _refresh_scaffold_owner_contract(owner, mod: str, mdir: str, final_names: list[str]) -> None:
    """hunter LOW：owner 是【上一轮注入的脚手架子任务】时，backfill 只刷 description、其 contract
    字段（2a 依赖完整性闸读的就是它）会陈旧。此处把该模块 contract 依赖同步为本轮最终清单——仅
    当 owner 已带该模块 contract 条目时刷（不给普通写代码 owner 凭空塞 contract）。"""
    c = getattr(owner, "contract", None)
    if not isinstance(c, dict):
        return
    deps = c.get("dependencies")
    if not isinstance(deps, list):
        return
    for d in deps:
        if isinstance(d, dict) and (d.get("module") or "").strip().rstrip("/") == mod:
            d["dir"] = mdir
            d["artifacts"] = list(final_names)


def _upsert_owner_manifest_block(owner, manifest_rel: str, block: str) -> bool:
    """把确定性清单机器块 upsert 进【已认领 manifest 的 owner 子任务】description。

    ★cr#1 CONFIRMED HIGH（R58-3 npm/go 对应物）★：脚手架只覆盖无人认领的 manifest；一旦某写代码
    子任务顺手认领了 package.json/go.mod，它就绕过确定性模板由小模型自由发挥→臆造版本/丢内部
    replace（正是 round58 Maven 死因的 npm/go 变体）。sentinel 包裹→幂等（同块不重复）/刷新（陈旧
    块不冻结）。

    ★hunter NEW MEDIUM（非桥接 strip）★ `.*?` 用 tempered-dot `(?:(?!<start>).)*?` 挡住【跨越另一个
    起始 sentinel】——否则外部截断留下的孤儿起始 sentinel（本库有 ELABORATE 期描述截断前科）会让下
    一轮 strip 从孤儿起始一路吞到下一个良构块的结束 sentinel，误删中间合法内容。tempered 后孤儿起
    始因其后无配对结束而不匹配（原样留存=无害陈旧文本），良构块照常 strip/重贴，零内容丢失。"""
    import re as _re
    esc = _re.escape(manifest_rel)
    old = owner.description or ""
    stripped = _re.sub(
        rf"\n?<!--#31P2 {esc}-->(?:(?!<!--#31P2 {esc}-->).)*?<!--#31P2-end {esc}-->",
        "", old, flags=_re.S)
    new = stripped + _p2_wrap(manifest_rel, block)
    if new == old:
        return False
    owner.description = new
    return True


def _npm_dep_block(manifest_rel: str, kept, pkg_name: str, exists: bool) -> str:
    """npm 清单机器块：CREATE→权威 package.json 模板；MODIFY→修改铁律（+缺失依赖片段，若有）。
    ★cr#1★ MODIFY 即便零缺失依赖也给铁律护栏（静默无块=owner 无指引自由改 → 丢既有依赖）。"""
    if not exists:
        return (f"\n【权威 package.json 模板（确定性生成，原样写入 {manifest_rel}；仅当项目另有"
                f"明确约定才允许在此基础上增改）】\n```json\n{_render_package_json(pkg_name, kept)}\n```")
    snip = ""
    if kept:
        deps = ",\n".join(f'    "{k.name}": "{k.spec}"' for k in kept)
        snip = (f"\n【缺失依赖片段（并入 {manifest_rel} 既有 \"dependencies\"，★仅追加下列键、"
                f"逐字保留其余内容★）】\n```json\n{{\n{deps}\n}}\n```")
    return (f"\n【既有 package.json 修改铁律（{manifest_rel} 已存在）】只做最小增量：绝不整体替换/"
            "重写，绝不删除既有 dependencies/字段，仅在 \"dependencies\" 内追加缺失键。" + snip)


def _inject_npm_scaffolds(plan, project_path, file_plan, dirs) -> list[dict]:
    """npm per-package.json driver（对抗双复核整改版）：内部标识取【全物理模块集】(dirs)、同源剪
    shared_contract、已认领 manifest 走 owner-backfill、unclaimed 注入脚手架。第三方版本经 npm
    registry 解析（^ver），内部 workspace 包 → workspace:*（零网络）。解析不到如实丢弃+同源剔除。"""
    from swarm.brain.npm_registry import resolve_npm_deps
    from swarm.types import FileScope, SubTask, TaskIntent

    mods_all = _contract_dep_entries(plan, dirs)
    if not mods_all:
        return []
    # ★内部包名全集从【全 dirs】取★（磁盘 name 或模块标签约定）——含已认领/跨轮模块，供 workspace:*
    # 正确分流，绝不让内部包被当第三方去公网 registry 误解析（cr#2/hunter#1）。
    internal_names = {_npm_module_name(project_path, d, m) for m, d in dirs.items()}
    injected: list[dict] = []
    backfilled: list[str] = []
    existing_ids = {st.id for st in plan.subtasks}
    for entry in mods_all:
        mod, mdir, arts = entry["module"], entry["dir"], entry["artifacts"]
        manifest_rel = f"{mdir}/package.json"
        exists = bool(project_path) and (Path(project_path) / manifest_rel).is_file()
        kept, dropped = resolve_npm_deps(project_path, arts, internal_names=internal_names)
        if dropped:
            logger.warning(
                "[SCAFFOLD-INJECT] #31-P2b 模块 %s 的 %d 个 npm 依赖无法确定性解析版本 → "
                "模板/契约/验收三处一并剔除（绝不逼 worker 编版本）: %s", mod, len(dropped), dropped)
        kept_specs = [k.name for k in kept]
        _prune_scaffold_contract_entry(plan, mod, kept_specs, dropped)   # hunter#3 同源剪契约
        pkg_name = _npm_module_name(project_path, mdir, mod)
        block = _npm_dep_block(manifest_rel, kept, pkg_name, exists)
        owner = _manifest_owner_subtask(plan.subtasks, manifest_rel)
        if owner is not None:   # cr#1：已认领 → backfill 进 owner，绝不静默留它手写
            _refresh_scaffold_owner_contract(owner, mod, mdir, kept_specs)   # 2a 闸同步
            if _upsert_owner_manifest_block(owner, manifest_rel, block):
                backfilled.append(owner.id)
            continue
        sid = f"st-scaffold-{mod}"
        if sid in existing_ids:
            continue
        scaffold = SubTask(
            id=sid,
            description=(f"【构建脚手架】为模块 {mod} " + ("补齐" if exists else "创建")
                        + f" npm 清单 {manifest_rel}：声明契约依赖全部包"
                        "（写代码的子任务碰不到构建清单，缺一个=整包装不上）"
                        + _p2_wrap(manifest_rel, block)),
            intent=TaskIntent.MODIFY if exists else TaskIntent.CREATE,
            difficulty=SubTaskDifficulty.TRIVIAL,
            scope=FileScope(writable=[manifest_rel] if exists else [],
                            create_files=[] if exists else [manifest_rel]),
            contract={"dependencies": [{"module": mod, "dir": mdir, "artifacts": kept_specs}]},
            acceptance_criteria=[f"{manifest_rel} 声明契约依赖全部包，`npm install` 通过"],
        )
        plan.subtasks.append(scaffold)
        existing_ids.add(sid)
        _wire_scaffold_ownership(plan, sid, mdir, manifest_rel)
        if plan.parallel_groups:
            plan.parallel_groups.insert(0, [sid])
        injected.append({"module": mod, "subtask_id": sid, "artifacts": kept_specs,
                         "manifest_exists": exists, "stack": "npm"})
    if injected:
        logger.info("[SCAFFOLD-INJECT] #31-P2b npm 脚手架注入 %d 个: %s",
                    len(injected), [e["module"] for e in injected])
    if backfilled:
        logger.warning("[SCAFFOLD-INJECT] #31-P2b R58-3 npm：%d 个 owner 自认领 package.json → 已把"
                       "确定性清单块嵌进其 description（有 owner≠有模板）: %s", len(backfilled), backfilled[:8])
    return injected


def _go_root_module_path(project_path: str | None) -> str | None:
    if not project_path:
        return None
    f = Path(project_path) / "go.mod"
    try:
        if f.is_file():
            import re as _re
            m = _re.search(r"^module\s+(\S+)", f.read_text("utf-8", errors="replace"), _re.M)
            return m.group(1) if m else None
    except OSError:
        pass
    return None


def _go_root_directive(project_path: str | None) -> str:
    """根 go.mod 的 `go X.Y` 指令（读真值，不猜）；无根 go.mod → 保守 '1.21'（语言指令非依赖版本）。"""
    if project_path:
        f = Path(project_path) / "go.mod"
        try:
            if f.is_file():
                import re as _re
                m = _re.search(r"^go\s+(\d+\.\d+(?:\.\d+)?)",
                               f.read_text("utf-8", errors="replace"), _re.M)
                if m:
                    return m.group(1)
        except OSError:
            pass
    return "1.21"


def _go_module_path(project_path: str | None, mdir: str, root_module: str | None) -> str | None:
    """内部 module import 路径：磁盘 go.mod module 行（事实来源）→ 根 module + reldir（go 惯例，
    可推导非猜）→ None（无根 go.mod 无从确定 import 路径，绝不臆造一个假路径）。"""
    if project_path:
        f = Path(project_path) / mdir / "go.mod"
        try:
            if f.is_file():
                import re as _re
                m = _re.search(r"^module\s+(\S+)", f.read_text("utf-8", errors="replace"), _re.M)
                if m:
                    return m.group(1)
        except OSError:
            pass
    if root_module:
        return f"{root_module}/{mdir.strip('/')}"
    return None


def _render_go_mod(module_path: str, go_directive: str, kept,
                   internal: list[tuple[str, str]]) -> str:
    lines = [f"module {module_path}", "", f"go {go_directive}"]
    if kept:
        lines.append("")
        lines.append("require (")
        lines += [f"\t{d.module} {d.version}" for d in kept]
        lines.append(")")
    for mp, target in internal:
        lines.append("")
        lines.append(f"require {mp} v0.0.0")
        lines.append(f"replace {mp} => {target}")
    return "\n".join(lines) + "\n"


def _go_dep_block(manifest_rel: str, self_path: str, go_directive: str,
                  kept, replaces: list[tuple[str, str]], exists: bool) -> str:
    """go 清单机器块：CREATE→权威 go.mod 模板（含 require+replace）；MODIFY→修改铁律 + 缺失
    require 片段 **+ 内部 replace 指令**。★cr#3 CONFIRMED HIGH★：replace 此前只在 CREATE 落，
    MODIFY（既有 go.mod）内部依赖的 replace 被整段丢 → 只依赖内部 module 的模块零指引。"""
    if not exists:
        return (f"\n【权威 go.mod 模板（确定性生成，原样写入 {manifest_rel}）】"
                f"\n```\n{_render_go_mod(self_path, go_directive, kept, replaces)}\n```")
    parts: list[str] = []
    if kept:
        reqs = "\n".join(f"\t{k.module} {k.version}" for k in kept)
        parts.append(f"require (\n{reqs}\n)")
    for mp, target in replaces:   # cr#3：MODIFY 路径也必须落 replace（内部 module 相对路径）
        parts.append(f"require {mp} v0.0.0\nreplace {mp} => {target}")
    snip = ("\n```\n" + "\n\n".join(parts) + "\n```") if parts else ""
    return (f"\n【既有 go.mod 修改铁律（{manifest_rel} 已存在）】只做最小增量：绝不整体替换/重写，"
            "绝不删除既有 require/replace，仅追加下列缺失项（内部 module 必须带 replace 指向本地"
            "相对路径，绝不去 proxy 拉）：" + snip)


def _inject_go_scaffolds(plan, project_path, file_plan, dirs) -> list[dict]:
    """go per-go.mod driver（对抗双复核整改版）：内部 module 标识取【全物理模块集】(dirs)、同源剪
    shared_contract、已认领 go.mod 走 owner-backfill、unclaimed 注入脚手架。第三方 require 版本经
    go proxy 解析（vX.Y.Z），内部 module → replace 指向本地相对路径（零网络）。解析不到如实丢弃；
    无根 go.mod 时 import 路径不可推导 → 跳过（绝不臆造假路径）。"""
    from swarm.brain.go_registry import resolve_go_deps
    from swarm.types import FileScope, SubTask, TaskIntent

    mods_all = _contract_dep_entries(plan, dirs)
    if not mods_all:
        return []
    root_module = _go_root_module_path(project_path)
    go_directive = _go_root_directive(project_path)
    # ★内部 import 路径全集从【全 dirs】取★（磁盘 go.mod module 或 根 module+reldir 推导）；含已
    # 认领/跨轮模块，绝不让内部 module 被当第三方去 proxy 误解析（cr#2/hunter#2）。不可推导者不入集。
    internal_paths: dict[str, str] = {}   # module_label → canonical import path
    path_to_dir: dict[str, str] = {}      # canonical import path → physical dir
    for m, d in dirs.items():
        p = _go_module_path(project_path, d, root_module)
        if p:
            internal_paths[m] = p
            path_to_dir[p] = d
    internal_ids = set(path_to_dir)       # 只用【规范 import 路径】做内部判定，避免裸标签泄进 replace
    injected: list[dict] = []
    backfilled: list[str] = []
    existing_ids = {st.id for st in plan.subtasks}
    for entry in mods_all:
        mod, mdir, arts = entry["module"], entry["dir"], entry["artifacts"]
        manifest_rel = f"{mdir}/go.mod"
        exists = bool(project_path) and (Path(project_path) / manifest_rel).is_file()
        # 契约可能用【模块标签】或【import 路径】引用内部 module → 统一归一成规范 import 路径，
        # 令 resolve_go_deps 的内部判定与下方 replace 生成同用一套规范键（杜绝裸标签泄进 go.mod）。
        _norm_arts = [internal_paths.get(a, a) for a in arts]
        kept, internal_mods, dropped = resolve_go_deps(_norm_arts, internal_modules=internal_ids)
        if dropped:
            logger.warning(
                "[SCAFFOLD-INJECT] #31-P2c 模块 %s 的 %d 个 go 依赖无法确定性解析版本 → 三处剔除: %s",
                mod, len(dropped), dropped)
        # 内部依赖 → replace <import路径> => <相对路径>（本模块目录视角看目标模块目录）
        replaces = [(im, _go_relpath(mdir, path_to_dir[im]))
                    for im in internal_mods if im in path_to_dir]
        final_names = [k.module for k in kept] + list(internal_mods)   # 契约=第三方 + 内部路径
        self_path = _go_module_path(project_path, mdir, root_module)
        if not self_path:
            # hunter LOW：无 self_path=整个 go.mod 脚手架跳过 → 契约**不剪**（本轮该模块无任何清单
            # 工作，剪了会让契约与"没做的事"错位；下一轮 self_path 可推导时再同源剪）。
            logger.warning(
                "[SCAFFOLD-INJECT] #31-P2c 模块 %s 无根 go.mod 可推导 import 路径 → 跳过 go.mod 脚手架"
                "（绝不臆造一个假 module 路径污染构建）", mod)
            continue
        _prune_scaffold_contract_entry(plan, mod, final_names, dropped)   # hunter#3 同源剪契约
        block = _go_dep_block(manifest_rel, self_path, go_directive, kept, replaces, exists)
        owner = _manifest_owner_subtask(plan.subtasks, manifest_rel)
        if owner is not None:   # cr#1：已认领 → backfill 进 owner（含 MODIFY 的 replace，cr#3）
            _refresh_scaffold_owner_contract(owner, mod, mdir, final_names)   # 2a 闸同步
            if _upsert_owner_manifest_block(owner, manifest_rel, block):
                backfilled.append(owner.id)
            continue
        sid = f"st-scaffold-{mod}"
        if sid in existing_ids:
            continue
        scaffold = SubTask(
            id=sid,
            description=(f"【构建脚手架】为模块 {mod} " + ("补齐" if exists else "创建")
                        + f" go 清单 {manifest_rel}：声明契约依赖全部 module"
                        "（写代码的子任务碰不到构建清单，缺一个=整模块编译失败）"
                        + _p2_wrap(manifest_rel, block)),
            intent=TaskIntent.MODIFY if exists else TaskIntent.CREATE,
            difficulty=SubTaskDifficulty.TRIVIAL,
            scope=FileScope(writable=[manifest_rel] if exists else [],
                            create_files=[] if exists else [manifest_rel]),
            contract={"dependencies": [{"module": mod, "dir": mdir, "artifacts": final_names}]},
            acceptance_criteria=[f"{manifest_rel} 声明契约依赖全部 module，`go build ./...` 通过"],
        )
        plan.subtasks.append(scaffold)
        existing_ids.add(sid)
        _wire_scaffold_ownership(plan, sid, mdir, manifest_rel)
        if plan.parallel_groups:
            plan.parallel_groups.insert(0, [sid])
        injected.append({"module": mod, "subtask_id": sid, "artifacts": final_names,
                         "manifest_exists": exists, "stack": "go"})
    if injected:
        logger.info("[SCAFFOLD-INJECT] #31-P2c go 脚手架注入 %d 个: %s",
                    len(injected), [e["module"] for e in injected])
    if backfilled:
        logger.warning("[SCAFFOLD-INJECT] #31-P2c R58-3 go：%d 个 owner 自认领 go.mod → 已把确定性"
                       "清单块嵌进其 description（有 owner≠有模板）: %s", len(backfilled), backfilled[:8])
    return injected


def _go_relpath(from_dir: str, to_dir: str) -> str:
    """go replace 目标相对路径（from_dir 的 go.mod 视角看 to_dir）；必带 ./ 或 ../ 前缀
    （go 要求本地 replace 是文件系统相对/绝对路径，裸名会被当 module 路径）。"""
    import posixpath
    rel = posixpath.relpath(to_dir.strip("/"), from_dir.strip("/"))
    return rel if rel.startswith(".") else f"./{rel}"


def _inject_build_scaffold_subtasks_impl(
    plan, project_path: str | None = None, file_plan: list | None = None,
) -> list[dict]:
    """R39-4：规则5 落空模块 → 确定性注入构建文件脚手架子任务（零 LLM）。

    round39 三轮 VALIDATE 各 6 模块规则5 WARNING 无人消费（#30② 同病）；脚手架
    此前只靠 prompts 叮嘱 LLM。本函数把落空模块的构建文件承接变成确定性动作：
    - 注入子任务 owner `<module>/pom.xml`（沿用规则5 自身口径；Maven 专属为既有
      产品决策，round24 A2 先例），契约 dependencies 全集随 contract 落地；
    - 基线已有 pom → writable 修改，否则 create_files 新建（project_path 判存在）；
    - 同模块写代码子任务 depends_on 脚手架（先有构建文件再编译）；脚手架自身无
      上游依赖 → 结构上不可能成环；其它模块不受影响（不过度串行）；
    - parallel_groups 完整性守约（validate_plan_structure 要求全员入组）。
    返回机读清单 [{module, subtask_id, artifacts, pom_exists}]；无落空=[]（幂等）。
    """
    # ★G9（Task#9 审计⑤）★ 入口按栈分派：本函数以下全部逻辑都是【Maven 专属】（pom.xml/<modules>/
    # <parent> reactor）。仅当工程栈是 Maven（或未知，back-compat 回退）才跑；已知非 Maven 栈
    # （Go/npm/Rust/Python/Gradle…）→ 明确不伪造 Maven 产物（no-op + 告警），杜绝异栈 reactor 污染。
    _ok, _stack = _should_fabricate_maven_scaffold(plan, project_path, file_plan)
    if not _ok:
        # #31-P2b/2c：非 Maven 栈不再无条件 no-op——npm/go 走各自的 per-module 清单 driver
        # （版本经 registry 确定性解析，绝不拿 pom 污染异栈）。其余已知栈（gradle/cargo/python）
        # 暂无 driver → 保持明确 no-op（绝不伪造清单）。
        if _stack in ("npm", "go"):
            try:
                _p2_dirs = _module_physical_dirs(plan, project_path, file_plan)
            except Exception:  # noqa: BLE001 — 物理落点解析失败绝不阻断规划
                logger.warning("[SCAFFOLD-INJECT] #31-P2 物理落点解析失败（fail-open，跳过脚手架）",
                               exc_info=True)
                return []
            if not _p2_dirs:
                # hunter#4：空 dirs 也留痕（降级可观测；_module_physical_dirs 内部对歧义/撞车已
                # WARNING，但"全部契约模块名都没匹配上物理落点"这一路径此前无信号）。
                logger.info("[SCAFFOLD-INJECT] #31-P2 栈=%s 但无契约模块解析到物理落点 → 无脚手架"
                            "（模块名或未匹配任何 scope 证据）", _stack)
                return []
            try:
                return (_inject_npm_scaffolds if _stack == "npm" else _inject_go_scaffolds)(
                    plan, project_path, file_plan, _p2_dirs)
            except Exception:  # noqa: BLE001 — driver 异常 fail-open，绝不炸规划主链
                logger.warning("[SCAFFOLD-INJECT] #31-P2 %s driver 异常（fail-open）", _stack,
                               exc_info=True)
                return []
        logger.warning(
            "[SCAFFOLD-INJECT] G9 工程构建栈=%s（非 Maven/npm/go）→ 跳过 pom 脚手架注入（绝不拿 "
            "pom.xml/<modules>/<parent> 污染异栈工程）。该栈的 aggregator 脚手架 driver 待接入。",
            _stack)
        return []
    if _stack == "unknown":   # #5：无证据保守回退 Maven 也留痕（异栈污染事故可回溯）
        logger.debug("[SCAFFOLD-INJECT] G9 未检出构建栈证据 → 保守回退 Maven（back-compat）")
    # R65E10-T2：基线无 Lombok 时剥 lombok 契约坐标——防 pom 注 lombok 撞 `! grep -rq lombok`
    # 禁令=round65e10 st-1 考卷矛盾死因②。★必须先于 prune_contract_dependencies★（复核 HIGH）：
    # 后者会从 artifacts_pre_prune 复原可解析坐标，lombok 可解析→若在其后跑会被复活。本函数把
    # lombok 从 artifacts+既有快照都剥掉，令下游 snapshot/复原源永久无 lombok。fail-open 见函数。
    try:
        prune_baseline_absent_dependencies(plan, project_path)
    except Exception:  # noqa: BLE001 — 绝不炸脚手架注入主链
        logger.warning("[R65E10-T2] 基线约定剥除失败（fail-open，契约保持原样）", exc_info=True)
    # T6①：契约依赖同源剪除**先于一切消费面**（owner 模板/scaffold 模板/后续规则5 验收
    # 读的都是剪后 entry）——round63 死型=验收要求被剪依赖逼 worker 复入。
    prune_contract_dependencies(plan, project_path)
    # T5（hunter#F4）：内部模块依赖推导**单次计算**、两个注入点（owner/scaffold）共用同一份
    # ——各算各的会让 fail-open-per-callsite 在输入分叉时给同一模块产出两份不一致模板。
    _dirs = _module_physical_dirs(plan, project_path, file_plan)
    try:
        # R65D-T2②配套：契约自声明的反向兄弟依赖与推导通道同判据剪除（先于模板消费面）
        _prune_reverse_contract_internal_deps(plan, _dirs, project_path)
    except Exception:  # noqa: BLE001 — fail-open
        logger.warning("[R65D-T2] 契约反向内部依赖剪除失败（fail-open）", exc_info=True)
    try:
        _internal_deps = derive_internal_module_deps(plan, _dirs, project_path)
    except Exception:  # noqa: BLE001
        logger.warning("[T5] 内部模块依赖推导失败（fail-open，模板退回纯契约 artifacts）",
                       exc_info=True)
        _internal_deps = {}
    # R58-3（round58 结构性死因）：**有 owner ≠ 有模板**。
    # 计划里的 pom 一旦被某个写代码的子任务"认领"，旧规则就不建脚手架 → 那个 pom **完全没经过
    # 确定性模板**、由小模型手写 → 写出 `<parent><version>${ruoyi.version}</version>`（属性引用，
    # Maven 解析 parent 时还没加载父 pom → 永远解析不了）→ **pom 解析期崩塌、整棵 reactor 读不出**。
    # R45-2 的全部意义（"pom 是纯机械产物，别让小模型编"）在这条路径上完全落空。
    # 治：**认领者也必须拿到确定性权威模板**（嵌进 description，让它抄而不是编）。
    _inject_templates_into_pom_owners(plan, project_path, file_plan,
                                      internal_deps=_internal_deps)

    from swarm.types import FileScope, SubTask, TaskIntent
    injected: list[dict] = []
    existing_ids = {st.id for st in plan.subtasks}
    # ★Task#4 治本★ 全部**实际收码**物理模块目录（不做名字匹配、不 fail-closed）——聚合器
    # <modules> 完整性与孤儿模块补脚手架的权威证据源，与 fail-closed 的 _dirs 分工。
    _phys = _physical_code_module_dirs(plan, file_plan)
    # ★先算 entries★：聚合父脚手架会写根 `pom.xml`，这会触发规则5 的 A5 归并（误判"单 pom owner
    # → 单模块项目"）把子模块的 unclaimed 全吃掉 → 子模块脚手架不再注入。故必须在注入聚合父**之前**固定。
    entries = unclaimed_contract_deps(plan)
    # T5（hunter#F1 HIGH，round63 死型本体）：契约条目 artifacts 为空（模块只需内部基线库，
    # 如只依赖 ruoyi-common）会被 unclaimed_contract_deps 的 `not arts` 剪掉 → 推导出的内部
    # 依赖**无处注入**、模块 pom 无人建，而旧 INFO 还谎报"已并入模板"。治：有内部依赖证据且
    # 无 pom owner 的模块，补一条零 artifacts 脚手架条目（R57-1 物理落点取证照常适用）。
    _entry_mods = {e["module"] for e in entries}
    # T6①补面：artifacts 全为空的契约模块（原生空 / 被同源剪除剪空）同样需要 pom 出口——
    # 声明了模块就得有构建清单，与"有没有第三方依赖"无关。
    _empty_contract_mods = {
        (e.get("module") or "").strip().rstrip("/")
        for e in ((plan.shared_contract or {}).get("dependencies") or [])
        if isinstance(e, dict) and (e.get("module") or "").strip()
        and not [a for a in (e.get("artifacts") or []) if a]}
    for _m in sorted(set(_internal_deps) | _empty_contract_mods):
        if _m in _entry_mods or _m not in _dirs:
            continue
        _pom_m = f"{_dirs[_m]}/pom.xml"
        _owned = any(_pom_m in (
            _norm_scope_path(f)
            for f in (list(getattr(getattr(st, "scope", None), "create_files", []) or [])
                      + list(getattr(getattr(st, "scope", None), "writable", []) or [])))
            for st in plan.subtasks)
        if _owned:
            continue   # 认领者路径（R58-3 owner 注入点）已拿到含内部依赖的模板
        entries.append({"module": _m, "artifacts": []})
        logger.info(
            "[T5/T6] 模块 %s 契约零第三方 artifacts（原生空/被同源剪空）→ 补脚手架条目"
            "（模块声明了就得有 pom；有内部依赖证据时随模板一并注入）", _m)
    # ★R60-1（round60 死因）★ 聚合父注入必须**先于** early-return，且**独立于 entries**——
    # 子模块 pom 全被认领时 entries 空，但聚合父 pom（纯 packaging=pom、无代码）没人认领，
    # 若被 early-return 跳过 → `ruoyi-alarm/pom.xml` 无人建 → 所有子模块 parent 找不到 → 全员 FATAL。
    _agg_ids = _inject_aggregator_scaffold(plan, _dirs, project_path, existing_ids, injected, _phys)
    # ★Task#4 治本★ 补孤儿：收码但**非干净契约模块**的物理子模块（已进聚合父 <modules>、Maven
    # 会下钻找其 pom）必须有确定性 pom owner，否则 `child module ... does not exist` = 派 worker
    # 去失败。同样**先于** early-return（entries 可能空，但孤儿仍需补 pom）。
    _inject_orphan_module_scaffolds(
        plan, _phys, _dirs, _agg_ids, project_path, existing_ids, injected)
    if not entries:
        return injected   # 可能已注入聚合父/孤儿（R60-1/Task#4）——绝不能再返回硬编码 []
    # R57-1 治本（round57 实锤）：**光凭契约里一个字符串，不足以在磁盘上造一个模块。**
    # LLM 把契约 schema 的占位符原样抄成了模块名（真实出现过 `module` / `artifacts`），
    # 旧实现对模块名零取证 → 无条件建 `module/pom.xml`、`artifacts/pom.xml` → 磁盘上凭空
    # 长出垃圾模块、污染 reactor（还得靠依赖合法性闸去替它擦屁股）。
    # 取证要求（二者其一，独立于契约自身——否则是循环论证）：
    #   ① 计划里有子任务往 `<mod>/` 下写**代码**（pom 本身不算：那正是我们要造的东西）；
    #   ② 它已在基线根 manifest 的模块清单里（棕地既有模块，本轮无人动它也仍是真模块）。
    _dirs = _module_physical_dirs(plan, project_path, file_plan)
    _rejected = [e["module"] for e in entries if e["module"] not in _dirs]
    if _rejected:
        logger.warning(
            "[SCAFFOLD-INJECT] R57-1 拒绝为 %d 个**无物理落点**的契约模块名建脚手架（它们不是真模块，"
            "多半是 LLM 把契约 schema 的占位符抄成了模块名）：%s —— 判据=计划里无人往该目录写代码、"
            "且基线里也没有该目录。凭空造模块会污染 reactor。",
            len(_rejected), _rejected)
        entries = [e for e in entries if e["module"] in _dirs]
        if not entries:
            return injected   # 聚合父可能已注入（R60-1）
    # 注：聚合父脚手架（R57-4b/R60-1）已在 early-return 之前注入，此处不再重复。
    # T5：内部模块依赖已在函数头单次推导（hunter#F4），此处直接并入。
    for entry in entries:
        mod = entry["module"]
        arts = _merge_internal_deps(list(entry["artifacts"]), _internal_deps.get(mod) or [])
        sid = f"st-scaffold-{mod}"
        if sid in existing_ids:
            continue  # 幂等兜底（正常情况下注入后 unclaimed 已清零走不到这）
        # R57-4：pom 建在**代码真实所在的物理目录**，而不是契约模块名的字面处。
        _mdir = _dirs[mod]
        pom = f"{_mdir}/pom.xml"
        # R41 复核 F5：project_path 未知（store 瞬时失败等）时保守按"已存在"走 MODIFY
        # ——CREATE 会让 worker 现造最小 pom 盖掉基线真 pom（clobber 比漏改更致命）
        pom_exists = (not project_path) or (Path(project_path) / pom).is_file()
        # R45-2（round45 死因）：pom 内容是纯机械产物（parent GAV+契约依赖展开），
        # 交给最弱环节（小模型）自由发挥产出坏 POM=reactor 中毒 → 阶梯三 revert
        # 连坐下游 95/107。确定性生成权威模板嵌进 description：小模型抄而不是编。
        # 复核 F3：完整模板只给 CREATE（新建无可失）；MODIFY 只给依赖片段+并入措辞
        # ——"原样写入"对既有 pom=clobber 复活（R41-F5 铁律：clobber 比漏改更致命）。
        # R53-1：坐标解析【一次】，模板 / 契约 / 验收标准三者同源。解析不到的依赖必须
        # 从三处一并剔除——旧实现只从模板剔除、验收仍要求"声明全部 artifacts"，这条矛盾
        # 直接逼 worker 手写臆造坐标（round53：幻影 alarm-interface 毒死整个 reactor）。
        _kept, _dropped = resolve_scaffold_artifacts(
            project_path, arts, extra_module_artifacts=_plan_module_artifacts(plan))  # R67C-T2
        if _dropped:
            logger.warning(
                "[SCAFFOLD-INJECT] R53-1 模块 %s 的 %d 个契约依赖无法确定性解析 → 模板/契约/"
                "验收三处一并剔除（如实缺失，绝不逼 worker 编坐标）: %s",
                mod, len(_dropped), _dropped)
        arts = [f"{d.group}:{d.artifact}" + (f":{d.version}" if d.version else "")
                for d in _kept]
        _tpl_block = ""
        if not pom_exists:
            # R57-7：住在聚合目录下的子模块，其 <parent> 必须是**聚合父**（relativePath ../pom.xml
            # 正好指到它），GAV 与聚合模板同源（根 groupId + 聚合目录名 + 根 version）。
            _pgav = None
            _rg = _root_gav(project_path)
            if _rg and "/" in _mdir:
                _agg_dir = _mdir.rsplit("/", 1)[0]
                _pgav = (_rg[0], _agg_dir.rsplit("/", 1)[-1], _rg[2])
            _tpl = _deterministic_pom_template(mod, arts, project_path, resolved=_kept,
                                               parent_gav=_pgav)
            if _tpl:
                _tpl_block = (
                    f"\n【权威 pom 模板（确定性生成，原样写入 {pom}；仅当项目另有明确"
                    f"约定才允许在此基础上增改，绝不重构结构）】\n```xml\n{_tpl}\n```")
        else:
            _dep_snips = "\n".join(_render_dep_block(d) for d in _kept)
            if _dep_snips:
                # #29-A（round65e12 治源头）：既有 pom 已合法，worker 只需在 <dependencies> 内【追加】
                # 缺失依赖。硬化措辞禁止碰结构（round65e12 死因=worker 把 framework pom 写成 `<group>`
                # +丢 parent.groupId 整体重写毒 reactor）。#29-B L1.1c pom 结构闸做确定性兜底。
                _tpl_block = (
                    f"\n【缺失依赖片段（并入 {pom} 既有 <dependencies>，"
                    "★仅在其内部【追加】下列 <dependency>、逐字保留其余全部内容★：绝不整体替换/删除/"
                    "重排，绝不改动 <parent>/<groupId>/<artifactId>/<packaging>/<modelVersion>"
                    "——它们已合法，改了必毒 reactor 连坐下游）】\n```xml\n"
                    f"{_dep_snips}\n```")
        scaffold = SubTask(
            id=sid,
            description=(
                f"【构建脚手架】为模块 {mod} " + ("补齐" if pom_exists else "创建")
                + f"构建文件 {pom}：一次性声明契约 dependencies 的全部 artifacts"
                "（写代码的子任务碰不到构建文件，缺一个依赖=整模块编译失败）"
                + ("\n★既有 pom：只在 <dependencies> 内追加缺失依赖，结构部分逐字不动★"
                   if pom_exists else "")
                + _tpl_block),
            intent=TaskIntent.MODIFY if pom_exists else TaskIntent.CREATE,
            difficulty=SubTaskDifficulty.TRIVIAL,
            scope=FileScope(
                writable=[pom] if pom_exists else [],
                create_files=[] if pom_exists else [pom]),
            # #31-P2 复核 HIGH：契约 module 是 LLM 逻辑标签（R58-1 可与物理目录不同名，
            # 如 alarm-admin 实住 ruoyi-admin/）。额外记【物理目录 _mdir】作 ground truth，
            # 让 L1 依赖完整性闸按物理真相归属 manifest，而非猜标签（否则改名模块闸静默失效）。
            contract={"dependencies": [{"module": mod, "dir": _mdir, "artifacts": arts}]},
            acceptance_criteria=[
                f"{pom} 声明契约 dependencies 全部 artifacts，模块构建命令通过"],
        )
        plan.subtasks.append(scaffold)
        existing_ids.add(sid)
        prefix = _mdir.rstrip("/") + "/"   # R57-4：按**物理目录**判同模块，不按契约模块名
        # R57-6（round57 MERGE 死循环）：**脚手架独占本模块构建文件写权**。
        # 实锤：写代码的 st-16/st-29 也在建同一批 `alarm-*/pom.xml` → MERGE 判"多写者内容不一致"
        # → 确定性取了 LLM 手写版、把脚手架的确定性权威模板丢进 rebase 重生成 → 重做 → 再 MERGE
        # → **同一批多写者** → 再 rebase……两轮 rebase=10 冲突集完全相同，**不收敛**。
        # 脚手架存在的全部理由就是"写代码的子任务碰不到构建文件"（R39-4）——把写权从它们手里收回来，
        # 多写者从源头消失，rebase 循环自然不存在。
        for st in plan.subtasks:
            if st.id == sid:
                continue
            # R59-2：绝不从**别的脚手架**手里抢写权——它们同样是确定性 owner。
            # round59 实锤：脚手架之间互相"收回"同一个 pom，击鼓传花到最后没人拥有聚合父的 pom。
            if str(st.id).startswith("st-scaffold-"):
                continue
            sc = getattr(st, "scope", None)
            for _attr in ("create_files", "writable"):
                _lst = getattr(sc, _attr, None)
                if not _lst:
                    continue
                _keep = [f for f in _lst if _norm_scope_path(f) != pom]
                if len(_keep) != len(_lst):
                    logger.warning(
                        "[SCAFFOLD-INJECT] R57-6 从 %s 的 %s 收回构建文件写权 %s → 脚手架 %s 独占"
                        "（多写者会让 MERGE 反复 rebase 不收敛，且确定性模板会被 LLM 手写版顶掉）",
                        st.id, _attr, pom, sid)
                    setattr(sc, _attr, _keep)
        for st in plan.subtasks:
            if st.id == sid:
                continue
            sc = getattr(st, "scope", None)
            writes = (list(getattr(sc, "create_files", None) or [])
                      + list(getattr(sc, "writable", None) or []))
            if any(str(f).replace("\\", "/").lstrip("/").startswith(prefix)
                   for f in writes) and sid not in st.depends_on:
                st.depends_on.append(sid)
        # R57-4b：本模块脚手架必须依赖**它自己所在聚合目录**的父 POM 先落地（R61-2：按 _mdir
        # 的直接父目录查，绝不用"最后一个聚合"——那会在多聚合场景错挂/漏挂真父）。
        _mod_agg = _mdir.rsplit("/", 1)[0] if "/" in _mdir else None
        _agg_sid = _agg_ids.get(_mod_agg) if _mod_agg else None
        if _agg_sid and _agg_sid != sid and _agg_sid not in scaffold.depends_on:
            scaffold.depends_on.append(_agg_sid)
        if plan.parallel_groups:
            plan.parallel_groups.insert(0, [sid])
        injected.append({"module": mod, "subtask_id": sid,
                         "artifacts": arts, "pom_exists": pom_exists})
    if injected:
        logger.info(
            "[SCAFFOLD-INJECT] 规则5 落空模块确定性注入脚手架 %d 个: %s",
            len(injected), [e["module"] for e in injected])
    return injected


def prune_empty_scope_subtasks(plan) -> list[str]:
    """R62-Task3（round62 治本）：R57-6 收权后确定性剪除【空写 scope 死子任务】。

    病根：R57-6 从 LLM 自建脚手架子任务手里收回 pom 写权（脚手架独占），留下 writable/
    create_files/delete_files **全空**且非 allow_any 的子任务（round62 实测 st-3/25/31/34）。
    这类子任务**不可派发**——scope_guard 放行不了任何写、验收"构建成功"永不满足 → worker
    空转 churn。dispatch 无空 scope 闸、plan_batch 只剪 group 不剪子任务 → 它们一路漏到执行期。

    治：无任何写目标且非 allow_any = 死任务，确定性剪除。★仅剪【无人依赖】者★——被别的
    子任务 depends_on 的死任务是更深的计划错，保留并告警，**绝不静默重映射把工作丢了**。
    剪除时一并清 depends_on 引用 + parallel_groups（守 validate_plan_structure 全员入组约束）。
    栈中立（纯结构判定，不涉任何语言）。返回被剪 id 列表（供收尾器机读观测）。

    对抗复核加固：
    - ★AUDIT 意图豁免★：intent=AUDIT 不产 diff、走 _run_security_audit 专路（nodes:3051），
      空写 scope 是它的**预期**形态（contract_utils:2407 反向印证：AUDIT 带写权才是误标）→
      绝不当死任务剪，否则静默删真审计工作。
    - ★不动点迭代★：剪掉链尾死任务后其上游死任务可能变得无人依赖 → 再剪，直到不动
      （单趟会漏链尾之上的死任务，仍空转）。
    - ★绝不剪成空计划★：LLM 双超时/解析失败的降级兜底计划就是单个空 scope 占位 st-1，
      携 plan_generation_failed 交下游 fail-fast，不可剪没；计划恒 ≥1 子任务。
    - ★过度剪除升警★：一次剪掉计划相当比例=多半上游回归（本区历史"补丁磁铁"），升 warning。
    """
    from swarm.types import TaskIntent

    def _dead(s) -> bool:
        if getattr(s, "intent", None) == TaskIntent.AUDIT:
            return False   # AUDIT 空写 scope 是预期形态（走审计专路），绝不剪
        sc = getattr(s, "scope", None)
        if sc is None or getattr(sc, "allow_any", False):
            return False
        return not (list(getattr(sc, "writable", None) or [])
                    + list(getattr(sc, "create_files", None) or [])
                    + list(getattr(sc, "delete_files", None) or []))

    pruned_all: list[str] = []
    subs = getattr(plan, "subtasks", None) or []
    for _ in range(len(subs) + 1):   # 不动点；上界=子任务数，绝不无限
        subs = plan.subtasks
        dead_ids = {s.id for s in subs if _dead(s)}
        if not dead_ids:
            break
        depended = {d for s in subs for d in (getattr(s, "depends_on", None) or [])
                    if d in dead_ids}
        prunable = dead_ids - depended
        # ★绝不剪成空计划★：全死（含降级兜底单 st-1）→ 保留交下游 fail-fast
        if prunable and [s for s in subs if s.id not in prunable]:
            plan.subtasks = [s for s in subs if s.id not in prunable]
            for s in plan.subtasks:
                if getattr(s, "depends_on", None):
                    s.depends_on = [d for d in s.depends_on if d not in prunable]
            pg = getattr(plan, "parallel_groups", None)
            if pg:
                plan.parallel_groups = [[x for x in g if x not in prunable] for g in pg]
                plan.parallel_groups = [g for g in plan.parallel_groups if g]
            pruned_all.extend(sorted(prunable))
        else:
            break   # 无可剪（全被依赖 / 会剪空）→ 停
    # 收尾：仍在的死任务（被依赖或"剪空"守卫保下的降级态）→ 告警可观测，不静默
    _left_dead = sorted(s.id for s in plan.subtasks if _dead(s))
    if _left_dead:
        logger.warning(
            "[SCAFFOLD-INJECT] R62-Task3 %d 个空写 scope 死子任务保留（被依赖 或 全死降级态"
            "不可剪空）→ 交下游计划复核/fail-fast，绝不静默重映射丢工作: %s",
            len(_left_dead), _left_dead)
    if pruned_all:
        _total = len(pruned_all) + len(plan.subtasks)
        _lvl = (logger.warning if len(pruned_all) > max(2, _total // 4)
                else logger.info)
        _lvl("[SCAFFOLD-INJECT] R62-Task3 确定性剪除 %d/%d 个空写 scope 死子任务（收权后无写"
             "目标、无人依赖，派发=worker 空转 churn）%s: %s", len(pruned_all), _total,
             "（占比偏高，疑上游回归，请核）" if len(pruned_all) > max(2, _total // 4) else "",
             pruned_all)
    return pruned_all


def wire_readable_provenance(plan) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """G2（Task#9 审计③ GAP1）：readable 声明的消费 → 补上 depends_on 供给边（provenance 自愈）。

    审计③ GAP1：C1 只保证每个契约符号有 owner，但【消费 A 产物的 B】从未被校验必须 depends_on A。
    LLM 漏画依赖边时 B 与 A 同波派发 → B 的沙箱里根本没有 A 的产物 → worker 伪造未见符号。
    既有 normalize 规则2 是【依赖边→readable】的单向传播；本 pass 补对称的另一向【readable→依赖边】：
    B.readable 里出现某个 A 的 create_files（★精确路径匹配，非模糊符号提及★）= B 确定性消费 A 的
    产物 → B 必须 depends_on A（同沙箱里 A 的文件才存在；跨沙箱 B1 才把已完成产物注入）。

    与 [[align_readable_to_producer]] 互补：那个修 readable 的【路径形状】对齐 producer 落点，本个
    修 readable 消费关系对应的【依赖序】。加边前查环：若加 B→A 会成环（A 已传递依赖 B）则**不加**、
    记为 unresolved（更深的计划环，交结构闸/告警，绝不制造环）。栈中立（纯路径匹配 + 图判定）。
    返回 (added_edges, unresolved_cycles)。
    """
    subs = list(getattr(plan, "subtasks", None) or [])
    if len(subs) < 2:
        return [], []
    by_id = {s.id: s for s in subs}
    # 产者映射按 _norm_scope_path 归一键（栈中立）。★复核 HIGH★：normalize 的撞车检测用的是
    # 原始路径串键，与本函数键空间不一致——同一文件不同拼写（`./x/A` vs `x/A`）它检测不到、不串行；
    # 故此处**不假设**撞车已归一：同一归一键有 2+ 不同产者 = normalize 漏归一的双建，记入 _ambig，
    # 绝不静默 setdefault 挑一个产者去接线（会把消费者挂到任意一个而非它真需要的那个）。normalize
    # 键空间统一交 G5（[[swarm-task9-brain-plan-audit]] 次级批）根治。
    produced_by: dict[str, str] = {}
    _ambig: set[str] = set()
    for s in subs:
        sc = getattr(s, "scope", None)
        for f in list(getattr(sc, "create_files", None) or []):
            k = _norm_scope_path(f)
            if k in produced_by and produced_by[k] != s.id:
                _ambig.add(k)
            else:
                produced_by.setdefault(k, s.id)
    if _ambig:
        logger.warning(
            "[contract] G2 %d 个文件被多个子任务 create（normalize 因键空间差异漏归一双建）→ "
            "跳过其 provenance 接线（不挂任意产者），交 normalize 键空间统一(G5)根治: %s",
            len(_ambig), sorted(_ambig)[:5])

    def _reaches(a: str, b: str) -> bool:
        """a 是否【传递】depends_on b（含直接）。迭代式（复核 MEDIUM：递归在长链上会
        RecursionError 崩掉整个 ELABORATE 节点）——照 `_depends_transitively` 的栈式写法。"""
        stack = list(getattr(by_id.get(a), "depends_on", None) or [])
        seen: set[str] = set()
        while stack:
            d = stack.pop()
            if d == b:
                return True
            if d in seen or d not in by_id:
                continue
            seen.add(d)
            stack.extend(getattr(by_id.get(d), "depends_on", None) or [])
        return False

    added: list[tuple[str, str]] = []
    unresolved: list[tuple[str, str]] = []
    _defanned: list[str] = []
    for b in subs:
        sc = getattr(b, "scope", None)
        # ── R67C-T3a（round67c 开箱）：纯资源/DDL 产物读【代码文件】= provenance，非 build 依赖 ──
        # 实锤：st-13-2/st-4-2 create 只有 sql/*.sql（不编译），却 readable 111~113 个实体 .java
        # →本 pass 把 provenance 全转成 61~62 条 build 依赖边 → 纯 DDL 变全图汇聚点 sink：上游任一
        # 放弃即连坐、爆炸半径=整个 2FA 落库（round67 st-14 DDL 77-dep 同型）。.sql/资源不参与编译、
        # 对 .java 无 build 序依赖（schema 已在 desc 自足）；只有【产物本身是 classpath 源码】的消费者
        # 读代码才是真 build 依赖。判据：本子任务【全部产出】(create ∪ writable) 皆非 classpath 源码
        # =纯资源产物 → 跳过它读【代码文件】的补边（保留读【资源】的边：如 .sql 依赖另一 .sql）。
        # 栈中立：经 _evidence_class（_EV_AUX=不编译资源/_EV_STRONG|_EV_WEAK_CODE=编译源码）。
        # ★不可用 classpath_fqn_key 判"是否代码"★：它对【默认包】.java（无 com/x/ 路径）也返 None，
        # 会把真源码误当资源→错误跳过真 build 边（全量回归实锤 test_consumer_edges_prevent_...）。
        _outs = (list(getattr(sc, "create_files", None) or [])
                 + list(getattr(sc, "writable", None) or []))
        _b_pure_resource = bool(_outs) and all(_evidence_class(x) == _EV_AUX for x in _outs)
        needs = set()
        for f in list(getattr(sc, "readable", None) or []):
            k = _norm_scope_path(f)
            if k in _ambig:
                continue
            a = produced_by.get(k)
            if not a:
                continue
            if _b_pure_resource and _evidence_class(f) in (_EV_STRONG, _EV_WEAK_CODE):
                _defanned.append(b.id)          # 纯资源读代码：provenance 保留、build 边不加
                continue
            needs.add(a)
        needs.discard(None)
        needs.discard(b.id)
        for a in sorted(x for x in needs if x):
            if _reaches(b.id, a):
                continue   # provenance 依赖序已在（直接或传递）
            if _reaches(a, b.id):
                unresolved.append((b.id, a))   # 加边成环 → 更深计划错，绝不制造环
                continue
            b.depends_on = list(getattr(b, "depends_on", None) or []) + [a]
            added.append((b.id, a))
    if _defanned:
        from collections import Counter
        _cnt = Counter(_defanned)
        logger.info(
            "[contract] R67C-T3a 纯资源/DDL 产物读代码=provenance 非 build 依赖，跳过补边 "
            "%d 条（防纯 DDL 全图汇聚点连坐；schema 已在 desc 自足，readable 仍留作上下文）: %s",
            sum(_cnt.values()), dict(_cnt))
    return added, unresolved


def align_readable_to_producer(plan, project_path: str | None = None) -> dict:
    """R62-Task5（round62 治本）：readable 幻影包路径确定性归一到 producer 真实落点。

    病根（不变量③ provenance 一致性）：consumer 的 readable 引 `.../sdk/model/
    AlarmRequest.java`（幻影子路径），但唯一 producer 其实建在 `.../sdk/AlarmRequest.java`
    → consumer `import sdk.model.X` 编不过（round62 实测 43 条幻影 readable）。依赖边与
    上下文不一致：worker 拿到看不见的文件路径。

    治：readable 的 basename 若被【恰好一个】producer 的 create_files 产出（=唯一符号、
    且是 code 文件）且**路径不一致** → 对齐到真实 create 落点。★只归一唯一 producer★：
    歧义 basename（多 producer，如每模块都有 pom.xml/常见名）绝不动（会误改）；无 producer
    的 readable（baseline 只读文件）不动（不是幻影）。upstream_artifacts 同步归一。栈中立。

    ★防误改真文件（对抗复核 #1）★：只归一【真幻影】——readable 路径本身既不是任何
    producer/writable 的真实计划落点、又不是 baseline 磁盘上真实存在的文件（后者是合法
    只读上下文，同 basename 纯属巧合，绝不把 consumer 重定向到别的同名文件）。返回 {aligned}。
    """
    _NON_CODE = {"xml", "yml", "yaml", "properties", "sql", "md",
                 "html", "htm", "css", "scss", "sass", "less"}
    subs = getattr(plan, "subtasks", None) or []
    # basename → 所有 create 落点（只收 code 文件，排 manifest/配置/标记）；并集所有真实计划路径
    producers: dict[str, set] = {}
    real_paths: set = set()
    for st in subs:
        sc = getattr(st, "scope", None)
        for f in (list(getattr(sc, "create_files", None) or [])
                  + list(getattr(sc, "writable", None) or [])):
            p = _norm_scope_path(f)
            real_paths.add(p)
        for f in (list(getattr(sc, "create_files", None) or [])):
            p = _norm_scope_path(f)
            b = p.rsplit("/", 1)[-1]
            if ("." not in b or b.startswith("pom.")
                    or b.rsplit(".", 1)[-1].lower() in _NON_CODE):
                continue
            producers.setdefault(b, set()).add(p)
    # 唯一 producer 的 basename → 其真实落点
    unique = {b: next(iter(ps)) for b, ps in producers.items() if len(ps) == 1}
    if not unique:
        return {"aligned": 0}
    aligned = 0
    _base = Path(project_path) if project_path else None

    def _is_real(xp: str) -> bool:
        # 真实计划落点（有人建/改）或 baseline 磁盘既有 → 合法引用，非幻影，绝不动
        return xp in real_paths or (_base is not None and (_base / xp).is_file())

    def _fix(paths):
        nonlocal aligned
        out = []
        local = []   # (old, new)
        for x in paths:
            xp = _norm_scope_path(x)
            b = xp.rsplit("/", 1)[-1]
            tgt = unique.get(b)
            if tgt and tgt != xp and not _is_real(xp):
                out.append(tgt)
                aligned += 1
                local.append((xp, tgt))
            else:
                out.append(x)
        return out, local

    changes: list = []   # (subtask_id, attr, old, new) —— 机读审计（对抗复核 #3）
    for st in subs:
        sc = getattr(st, "scope", None)
        if sc is None:
            continue
        for _attr in ("readable", "upstream_artifacts"):
            _lst = getattr(sc, _attr, None)
            if _lst:
                _new, _local = _fix(_lst)
                if _local:
                    # 去重保序
                    _seen = set()
                    _dedup = [x for x in _new if not (x in _seen or _seen.add(x))]
                    setattr(sc, _attr, _dedup)
                    for _old, _tgt in _local:
                        changes.append((st.id, _attr, _old, _tgt))
    if aligned:
        logger.info(
            "[PLAN-FINISH] R62-Task5 幻影 readable 包路径归一到 producer 真实落点 %d 条"
            "（唯一符号 basename 匹配；歧义名/无 producer/真实文件不动）: %s",
            aligned, [f"{c[0]}:{c[2]}→{c[3]}" for c in changes[:8]])
    return {"aligned": aligned, "changes": changes}


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

    # ── 规则 0（round38c F1 裁决分流，先于一切规则跑）：writable 存在性核对 ──
    # F1 取证实锤：SysUser.java 被声明在 ruoyi-system/.../domain/（基线真身在
    # ruoyi-common/.../entity/），worker 对着幻觉路径建重复实体或不改。writable 语义=
    # 修改既有文件，必须 ∈ base 树 ∪ 全 plan create_files：
    #   · basename 在 base 树唯一命中 → 确定性重定位（指向真身）；
    #   · 无命中 → 真新文件，挪入本子任务 create_files；
    #   · 多义命中 → 保守告警不动（B4-2 异议通道兜底）。
    # 对抗复核 CONFIRMED 修正：①本规则必须跑在规则1/1.5/3/4 之前——重定位可能造出
    # 跨子任务同文件多写者，交给下游写权归一/串行化收敛（原插在规则5 前=收敛全部
    # 跑完，双写者直通 plan_validator 硬失败）；②构建清单 basename（pom.xml 等）
    # 一律不重定位——新模块 pom 被误标 writable 是 LLM 常见形态（规则4 注释自证），
    # 按 basename 撞根 pom=击穿 D1 单写者+脚手架蒸发，一律走"挪 create_files"；
    # ③目录上下文：writable 所在目录有本 plan 的 create_files 兄弟=新目录新文件
    # （合法同名分层复制），不重定位。非 git/清单失败 → 整条跳过（greenfield 不误伤）。
    _RULE0_MANIFESTS = {"pom.xml", "build.gradle", "build.gradle.kts",
                        "settings.gradle", "settings.gradle.kts",
                        "package.json", "go.mod", "cargo.toml"}
    _tree = _base_tree_listing(project_path, base_ref)
    if _tree:
        _tree_set = set(_tree)
        _by_base: dict[str, list[str]] = {}
        for _p in _tree:
            _by_base.setdefault(_p.rsplit("/", 1)[-1], []).append(_p)
        _all_creates = {str(f).replace("\\", "/") for st in subtasks
                        for f in (getattr(getattr(st, "scope", None), "create_files", None) or [])}
        _create_dirs = {c.rsplit("/", 1)[0] for c in _all_creates if "/" in c}
        for st in subtasks:
            _sc0 = getattr(st, "scope", None)
            if _sc0 is None:
                continue
            _w = list(getattr(_sc0, "writable", None) or [])
            _new_w: list = []
            _moved: list = []
            for f in _w:
                fn = str(f).replace("\\", "/")
                fn = fn[2:] if fn.startswith("./") else fn
                if fn in _tree_set or fn in _all_creates:
                    _new_w.append(f)
                    continue
                _base_name = fn.rsplit("/", 1)[-1]
                _dir = fn.rsplit("/", 1)[0] if "/" in fn else ""
                _hits = _by_base.get(_base_name) or []
                _is_manifest = _base_name.lower() in _RULE0_MANIFESTS
                _dir_is_new = bool(_dir) and _dir in _create_dirs
                if _hits and len(_hits) == 1 and not _is_manifest and not _dir_is_new:
                    logger.warning(
                        "[normalize] 规则0：%s 的 writable %s 不在 base 树，basename 唯一命中 "
                        "%s → 确定性重定位（幻觉路径治本 F1）", st.id, fn, _hits[0])
                    if _hits[0] not in _new_w:
                        _new_w.append(_hits[0])
                    changed = True
                elif not _hits or _is_manifest or _dir_is_new:
                    logger.warning(
                        "[normalize] 规则0：%s 的 writable %s 不在 base 树（%s）→ "
                        "视为新建挪入 create_files", st.id, fn,
                        "构建清单不重定位" if _is_manifest and _hits else (
                            "新目录上下文" if _dir_is_new and _hits else "无同名文件"))
                    _moved.append(fn)
                    changed = True
                else:
                    logger.warning(
                        "[normalize] 规则0：%s 的 writable %s 不在 base 树，basename 多义命中 "
                        "%d 处 → 保守保留（worker 异议通道兜底）", st.id, fn, len(_hits))
                    _new_w.append(f)
            if _moved:
                _sc0.create_files = list(dict.fromkeys(
                    list(getattr(_sc0, "create_files", None) or []) + _moved))
                _all_creates.update(_moved)
                _create_dirs.update(c.rsplit("/", 1)[0] for c in _moved if "/" in c)
            if _new_w != _w:
                _sc0.writable = _new_w
            # ── R67-T8（round67 R67-10，规则0 逆向 sibling）：create_files 撞基线既有文件 →
            # 降级 writable(modify)。实锤：契约符号安置把基线已有 GenTable/GenTableColumn.java
            # 当 create（worker 按"新建"写=覆写基线代码）；既有模块 pom 入 create（污染"新建"
            # 统计与 H1 模板路径）。base 树是唯一权威（同规则0 正向口径），命中即降级。
            _c0 = list(getattr(_sc0, "create_files", None) or [])
            _demoted = []
            _kept_c = []
            for f in _c0:
                fn = str(f).replace("\\", "/")
                fn = fn[2:] if fn.startswith("./") else fn
                if fn in _tree_set:
                    _demoted.append(fn)
                else:
                    _kept_c.append(f)
            if _demoted:
                logger.warning(
                    "[normalize] R67-T8 规则0逆向：%s 的 create_files %s 在 base 树已存在 → "
                    "降级 writable(modify)（按新建写=覆写基线，round67 GenTable 死型）",
                    st.id, _demoted)
                _sc0.create_files = _kept_c
                _sc0.writable = list(dict.fromkeys(
                    list(getattr(_sc0, "writable", None) or []) + _demoted))
                changed = True

    # ── 规则 3（先于规则1跑）：Maven 新模块构建闸门可满足性补全（治本 task 69d34b1b）。
    # 放规则1前，使补进来的 pom 也受"同文件写权唯一"去重/串行化（多模块子任务不并发抢写根 pom）。
    changed = _ensure_maven_module_build_scope(subtasks) or changed

    # ── 规则 1：同文件写权处理（区分串行协作 vs 独立并发 vs 聚合修改）──
    # 每个文件的【有序写者列表】（按 subtasks 顺序，近似拓扑序：上游在前）。
    # ★G5（Task#9 审计 TIER3 / G2 复核 HIGH 收尾）★ 写者身份索引必须按【归一路径】建键：
    # `./mod-a/Foo.java` 与 `mod-a/Foo.java`（同一物理文件、拼写不同）若各自建键 → 各是
    # "唯一首写者" → 双建撞车逃过单写者归一 → 直通 plan_validator（它信任本函数已收敛唯一
    # owner，见 plan_validator.py:194 注释）→ dispatch 期两子任务并发建同一物理文件。产者索引
    # （wire_readable_provenance）早已按 _norm_scope_path 建键——此处统一到同一键空间。
    # 注：scope 里保留原始拼写（本 pass 只纠正写者【身份判定】，不改写路径字符串）。
    writers_by_file: dict[str, list[str]] = {}
    for st in subtasks:
        scope = getattr(st, "scope", None)
        if scope is None:
            continue
        _wt = list(getattr(scope, "create_files", []) or [])
        _wt += list(getattr(scope, "writable", []) or [])
        for f in _wt:
            ids = writers_by_file.setdefault(_norm_scope_path(f), [])
            if st.id not in ids:
                ids.append(st.id)
    first_writer: dict[str, str] = {k: ids[0] for k, ids in writers_by_file.items()}

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
            nf = _norm_scope_path(f)   # G5：写者身份/聚合判定走归一键；scope 仍存原始拼写 f
            writer = first_writer.get(nf)
            if writer == st.id:
                # 首写者：聚合文件且已存在 → 实为 modify，落 writable；否则保留原操作类型。
                if nf in aggregate_files:
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
            elif nf in aggregate_files:
                # 独立并发 + 聚合文件：保留写权（转 writable 修改）+ 串行到前序写者，绝不降级。
                prev = _prev_safe_writer(nf, st.id)
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
                writer = first_writer.get(_norm_scope_path(f))   # G5：归一键（demoted 存原始拼写）
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
                    # R62 收编：若 owner 本身是脚手架（如嵌套聚合父 pom），则 st→owner 是
                    # **结构性继承边**（子模块 pom 的 <parent> 指向聚合父，R57-4b/R61 注入器造），
                    # 绝不能删——删了就是 round62 死因经 normalize 通道复活（合成多聚合几何实锤：
                    # owner=st-scaffold-ruoyi-alarm 时旧码把 child→父边 REMOVE 掉再反转）。
                    # 只对【非脚手架 registrant】（做递归 reactor 构建、真需注册后于脚手架者，
                    # 如 d37a52a3 建代码的根 registrant）删反向直边。owner 是脚手架时保留继承边；
                    # 其后 ADD 有 _depends_transitively 守卫，继承边在 → 反向 ADD 自动跳过、绝不成环。
                    if owner.id in deps_st:
                        if _is_scaffold_inheritance_parent(st, owner):
                            # owner 是 st 的【继承父】（st 的 module pom 严格嵌套在 owner 的
                            # module pom 下）→ st→owner 是结构性继承边，保留、不反转（后续 ADD
                            # 由 _depends_transitively 自动跳过、不成环）。★用边关系判而非目标分类：
                            # registrant 若只是"注册 st 的模块进根 pom"（无目录嵌套）则照常反正，
                            # 不误伤 d37a52a3/d1 的注册序。★
                            logger.info(
                                "[contract] 规则4 保留结构性继承边 %s→%s（owner 是 st 的继承父，"
                                "非 registrant；反转会复活 round62 module_registered_before_scaffold）",
                                st.id, owner.id)
                        else:
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


def contract_symbols_with_module(
    shared_contract: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """contract_symbols 的带模块归属版（R39-2 单一事实源）。

    返回 [{"symbol": <核心标识符>, "module": <契约条目 module 字段，无则空串>}]，
    符号序列与 contract_symbols 逐项同序同值——contract_symbols 委托本函数，
    防"两份提取逻辑"漂移。module 归属来自 _merge_module_contracts D10 合并键，
    是符号外科挂靠（symbol_surgery）的确定性依据。
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

    entries: list[dict[str, str]] = []
    # C1 复核补漏：ULTRA 合并契约的 DTO 落在 "dtos" 键（CONTRACT_MODULE schema），
    # 旧列表只读 "types" → DTO 名对 C1 规划期对账 / L2 契约核验双盲。
    # R39-3：kind=来源键，C1 硬/软分级消费（interfaces/types/apis/symbols 硬，
    # dtos/fields/methods 软）；L2 全量消费不区分。
    for key in ("interfaces", "types", "dtos", "apis", "fields", "methods"):
        val = shared_contract.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    entries.append({"symbol": _core(item), "module": "", "kind": key})
                elif isinstance(item, dict):
                    entries.append({
                        "symbol": str(item.get("name") or item.get("id") or ""),
                        "module": str(item.get("module") or "").strip().rstrip("/"),
                        "kind": key,
                    })
        elif isinstance(val, dict):
            entries.extend(
                {"symbol": str(k), "module": "", "kind": key} for k in val.keys())
    for item in shared_contract.get("symbols", []) or []:
        if isinstance(item, str):
            entries.append({"symbol": _core(item), "module": "", "kind": "symbols"})
    # 去重（保首见及其 module 归属）+ 过滤太短/HTTP 动词噪音
    _noise = {"get", "post", "put", "delete", "patch", "the", "and", "for"}
    seen: dict[str, dict[str, str]] = {}
    for e in entries:
        s = e["symbol"]
        if s and len(s) >= 3 and s.lower() not in _noise and s not in seen:
            seen[s] = e
    return list(seen.values())


def symbol_diff_variants(sym: str) -> list[str]:
    """R43 复核 F4：L2 子串核验的符号变体（lower）。契约符号带 I 前缀而代码只写
    基名（IChannelAdapter ↔ class ChannelAdapter）时，字面子串会把 C1 已按惯例
    等价放行的符号在 L2 判缺——C1↔L2 口径必须对称，否则"两张皮"只是位移到 8h 后。
    保守只加 I 基名变体（不加装饰前缀：子串方向天然覆盖装饰）。"""
    s = str(sym or "")
    out = [s.lower()]
    if len(s) >= 3 and s[0] == "I" and s[1].isupper():
        out.append(s[1:].lower())
    return out


def contract_symbols(shared_contract: dict[str, Any] | None) -> list[str]:
    """从共享契约提取需出现在变更中的【核心标识符】（非整句描述）。

    task 2c019bc5：契约 apis 常是 "GET /system/device/list — 分页查询设备列表，参数：..."
    这种带中文描述的整句。旧实现把整句当符号去 diff 精确匹配 → 必然找不到 → 误判契约偏离。
    修复：抽核心标识——API 取 URL 路径段（/system/device/list → device/list 或末段），
    类/方法/字段取其标识符 token。这样匹配的是代码里真会出现的东西，而非自然语言描述。
    实现委托 contract_symbols_with_module（R39-2）——单一提取逻辑，防两份事实。
    """
    return [e["symbol"] for e in contract_symbols_with_module(shared_contract)]


def baseline_symbol_files(
    symbols: list[str], project_path: str | None,
) -> set[str]:
    """R39-2 存量豁免依据：项目基线树里已有 `<Symbol>.<ext>` 同名文件的符号集。

    棕地场景契约常引用存量类型（round39：C1 完全不查存量 → 已存在的符号也被判
    unowned）。判据=文件名 stem 精确等于符号（确定性、栈无关：Java/TS/C# 等类文件
    同名约定；不做内容 grep 防误命中注释）。跳过依赖/构建产物目录。
    """
    if not symbols or not project_path:
        return set()
    import os as _os
    root = Path(project_path)
    if not root.is_dir():
        # hunter②：给了 project_path 却不是可用目录=存量豁免整体失效，绝不能与
        # "真无存量"混同静默——否则棕地符号全落 unowned 硬性打回（round39 死因族）。
        logger.warning(
            "[baseline-scan] project_path 非有效目录，存量豁免失效（按无存量处理）: %s",
            project_path)
        return set()
    want = {s for s in symbols if s}
    hits: set[str] = set()
    _skip = {".git", "node_modules", "target", "build", "dist", "out",
             ".gradle", ".idea", ".vscode", "__pycache__", ".codegraph"}
    # R42：命名惯例等价（棕地存量 ISysRoleService.java 承接符号 SysRoleService 同病
    # 同治）。复核 F3：只开 ①②③ 通道（decorated_prefix=False）——④ 装饰前缀在
    # 5k 文件棕地树上豁免半径失控（ISysUserService 会豁免一切 *UserService 新符号，
    # 缺实现静默漂到 L2 且子串核验兜不住）。
    from swarm.brain.plan_validator import basename_owns_symbol
    for dirpath, dirnames, filenames in _os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _skip]
        for fn in filenames:
            stem = fn.rsplit(".", 1)[0]
            if stem in want:
                hits.add(stem)
                continue
            for s in want - hits:
                if basename_owns_symbol(stem, s, decorated_prefix=False):
                    hits.add(s)
        if hits >= want:
            break
    return hits


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


def _module_pom_dirs(st) -> set[str]:
    """该子任务创建的所有【目录限定 module pom】的模块目录集（排除裸根 `pom.xml`）。"""
    out: set[str] = set()
    for f in _st_create_files(st):
        fn = str(f).replace("\\", "/").lstrip("./")
        if "/" in fn and fn.rsplit("/", 1)[-1] == "pom.xml":
            out.add(fn.rsplit("/", 1)[0])
    return out


def _creates_module_pom(st) -> bool:
    """创建【目录限定的模块 pom】（`<dir>/pom.xml`，**排除裸根 `pom.xml`**）。
    模块 pom 才有 `<parent>`、才参与继承排序；裸根 pom 是继承树顶（registrant 角色），不算。"""
    return bool(_module_pom_dirs(st))


def _is_scaffold_inheritance_parent(child_st, parent_st) -> bool:
    """parent_st 是否是 child_st 的【Maven 继承父】：child 建的某 module pom 目录**严格嵌套**在
    parent 建的某 module pom 目录之下（`child_dir startswith parent_dir + "/"`）。

    这才是"子 pom 的 `<parent>` 要求父 pom 先落地"的**继承结构边**（R57-4b/R61 注入器造），
    与"**注册边**"（模块登记进根/父 pom 的 `<modules>`，无目录嵌套关系）**本质不同**：
    registrant 即便自己也建某 module pom（如 st-1 建 ruoyi-alarm/pom.xml + 写根 pom 注册
    ruoyi-alarm-sdk），只要 st 的模块目录不在它下面（ruoyi-alarm-sdk ⊄ ruoyi-alarm/），
    就**不是**继承父 → 规则4 照常反正（注册后于脚手架），d37a52a3/d1 保护不动。
    ★用【边关系】判，而非【目标分类】——同一 owner 可兼任 registrant 与 module 脚手架两角，
    只有目录嵌套能区分该边到底是"继承"还是"注册"（对抗双复核 + d1 全量回归共同实锤）。★"""
    child_dirs = _module_pom_dirs(child_st)
    parent_dirs = _module_pom_dirs(parent_st)
    return any(cd.startswith(pd + "/") for cd in child_dirs for pd in parent_dirs)


def is_structural_scaffold_dep(dep_st) -> bool:
    """★脚手架排序边【单一权威判据】（R62 收编）★

    一条 `depends_on` 边【指向模块脚手架】即为**确定性构建顺序约束**（Maven 继承地基：
    子 pom 的 `<parent>` 要求父 pom 先落地；写代码子任务要求本模块 pom 先落地），**绝非**
    "LLM 误加的假依赖"。任何启发式 pass 都不得【剥】它（decouple 剥离假依赖）或【反转】它
    （normalize 规则4 registrant-inversion 的 REMOVE 步）。脚手架用 `mvn -f <pom> validate`
    非递归构建（l1_pipeline:3033），彼此靠注入器造的继承边自排序，不需 registrant 倒挂。

    判据 = 结构性脚手架(`_is_scaffold_subtask`：建 pom + 不建实体) **且** 建【目录限定 module
    pom】(`_creates_module_pom`)。覆盖两条 provenance：①注入器脚手架(id `st-scaffold-*`，
    contract_utils:688/814)；②R58-3 LLM 认领某 module pom 者(结构上是脚手架、无 st-scaffold- id)。

    ★为何必须排除裸根 pom（对抗双复核一致 HIGH，两 reviewer 独立实锤）★：`_is_scaffold_subtask`
    对**创建裸根 `pom.xml` 的 registrant**也判 True。若不排除，normalize 规则4 的 REMOVE 守卫会
    把根 pom registrant 误当"结构性脚手架"→跳过 registrant-inversion→静默重引 d37a52a3
    「Child module … does not exist」reactor 中毒（registrant 建 create_files 含裸 pom.xml 时）。
    仓库既有 `bump_scaffold_difficulty` 用 `_is_scaffold_subtask(st) or writes_root_pom` 早已区分
    "建根 pom"≠"是模块脚手架"；此处同口径：只有【目录限定 module pom】才是继承地基。

    dep_st=None（悬空依赖，目标不存在）→ False（不臆断，交既有悬空处理）。"""
    return dep_st is not None and _is_scaffold_subtask(dep_st) and _creates_module_pom(dep_st)


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

    ★R62-Task6 收窄（对抗复核路由病）★：只提【写根 pom.xml】者。RUN19 的多步本质是
    "读庞大根 pom + 定位 <modules> + 追加注册"——那是**根** pom 专属。而**模块** pom 脚手架
    （`<mod>/pom.xml`，描述内嵌【权威模板·原样写入】）本质是**单文件模板落盘**（无需 locate、
    无需读大文件）=真 trivial；旧判据 `_is_scaffold_subtask(st) or …` 把这 8-9 个纯机械 pom 写
    全提到 MEDIUM → 送 worker 重多步路径、白占小本地模型算力（不变量④难度路由异味）。收窄后
    模块 pom 脚手架维持 TRIVIAL 轻量路径，仅根 pom 写者（真多步）保 MEDIUM。

    规则：difficulty==TRIVIAL 且 写根 pom.xml（scope 含**裸** `pom.xml`）→ 提到 MEDIUM。
    R67-T9 sibling（round67 R67-13 实锤 st-17）：TRIVIAL 且 create ≥3 个类路径源码文件
    （实体簇）→ 提到 MEDIUM——trivial 单发路径（合并定位+编码封顶 30 步）塞不下多源码
    文件，低估路由弱档=白烧后重派。模块 pom/资源单文件模板落盘不受影响（真 trivial 保留）。
    原地改，返回提升个数。
    """
    bumped = 0
    for st in getattr(plan, "subtasks", []) or []:
        if getattr(st, "difficulty", None) != SubTaskDifficulty.TRIVIAL:
            continue
        sc = getattr(st, "scope", None)
        creates = list(_st_create_files(st))
        writes = set(creates) | set(getattr(sc, "writable", []) or [])
        # 裸 `pom.xml`（根 pom：大文件 + 多模块登记，读改皆重多步）——模块 pom 是 `<dir>/pom.xml`
        writes_root_pom = "pom.xml" in {_norm_scope_path(w) for w in writes}
        # R67-T9：≥3 个类路径源码 create（classpath_fqn_key 非 None=编译参与源码，
        # 资源/清单不计——单文件模板落盘仍走 trivial 轻路径）
        many_sources = sum(1 for f in creates if classpath_fqn_key(f)) >= 3
        if writes_root_pom or many_sources:
            st.difficulty = SubTaskDifficulty.MEDIUM
            bumped += 1
    return bumped


def deconflict_cross_module_creates(plan: TaskPlan) -> int:
    """DR-09-F1(#101) part(1)：同一 FQN 被多子任务在【不同物理模块】各自 create 时的确定性归一。

    round66/65e14 死因：st-6/16/18 在 ruoyi-alarm 正确 create AlarmTemplate/AlarmNotifyUser/
    AlarmAppSecret 等；st-45-1-1/46-1-1/47-1 又把同 FQN 重复安排到 ruoyi-admin 下 create → 同
    FQN 跨模块副本遮蔽 + 语法损坏 + 连坐（normalize_plan_scopes 只按【路径】去冲突，跨模块同 FQN
    不同路径逃过；symbol_provenance T4 检测到多落点只消极不钉）。

    本 pass 用【契约 defined_in】作唯一权威判 owner 模块（绝不用裸 basename/启发式，防 com.a.Foo
    与 com.b.Foo 误并——FQN=包路径+类名，见 classpath_fqn_key）：
      · 契约明确钉某 FQN 的 owner 模块 → 保留 owner 子任务的 create，其余子任务把该文件从
        create_files 剥除、改 readable（指向 owner 落点）+ 依赖 owner 子任务（带防环）。
      · 无契约权威可判的歧义 → 【不动】，留给 #110 validate REJECT 硬打回（绝不静默挑一个）。
    与 #110 REJECT 互补（防御纵深）：能确定性消解的省一轮 replan，消解不了的 fail-closed 打回。
    返回被归一（剥除）的文件数。栈中立（仅 JVM 类路径共享命名空间适用，见 classpath_fqn_key）。
    """
    subtasks = list(getattr(plan, "subtasks", None) or [])
    if len(subtasks) < 2:
        return 0
    # FQN → {module -> [subtask]}（仅 create_files）；(id(st),fqn) → 实际 create 路径
    fqn_index: dict[str, dict[str, list]] = {}
    file_of: dict[tuple[int, str], str] = {}
    for st in subtasks:
        sc = getattr(st, "scope", None)
        for f in list(getattr(sc, "create_files", None) or []):
            key = classpath_fqn_key(f)
            if not key:
                continue
            mod, fqn = key
            fqn_index.setdefault(fqn, {}).setdefault(mod, []).append(st)
            file_of[(id(st), fqn)] = f
    # 契约 defined_in 权威：fqn → owner 模块
    owner_mod: dict[str, str] = {}
    for e in ((getattr(plan, "shared_contract", None) or {}).get("interfaces") or []):
        if isinstance(e, dict):
            key = classpath_fqn_key(str(e.get("defined_in") or ""))
            if key:
                owner_mod[key[1]] = key[0]
    by_id = {getattr(st, "id", None): st for st in subtasks}

    def _reaches_dep(start, target) -> bool:
        """start 是否经 depends_on 链到达 target（加边 st→owner 前防环）。"""
        seen, stack = set(), [start]
        while stack:
            cur = stack.pop()
            if cur == target:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            st = by_id.get(cur)
            if st is not None:
                stack.extend(getattr(st, "depends_on", None) or [])
        return False

    changed = 0
    for fqn, mods in fqn_index.items():
        if len(mods) < 2:
            continue
        auth = owner_mod.get(fqn)
        if not auth or auth not in mods:
            continue        # 无契约权威 → 留给 #110 REJECT，绝不静默挑
        owner_st = mods[auth][0]
        owner_id = getattr(owner_st, "id", None)
        owner_file = file_of.get((id(owner_st), fqn))
        for mod, sts in mods.items():
            if mod == auth:
                continue
            for st in sts:
                f = file_of.get((id(st), fqn))
                sc = getattr(st, "scope", None)
                if not f or sc is None:
                    continue
                nf = _norm_scope_path(f)
                sc.create_files = [x for x in (getattr(sc, "create_files", None) or [])
                                   if _norm_scope_path(x) != nf]
                # 复核整改（猎手 PLAUSIBLE）：剥除 create_files 的同时清掉【专门针对该文件】的验收/
                # 验证条目——否则子任务仍被 "X.java 按用途实现并编译通过" 误导去重建已剥离文件，抵消归一。
                _ac = getattr(st, "acceptance_criteria", None)
                if _ac:
                    st.acceptance_criteria = [a for a in _ac if f not in str(a) and nf not in str(a)]
                _hh = getattr(st, "harness", None)
                _vc = getattr(_hh, "verify_commands", None) if _hh is not None else None
                if _vc:
                    _hh.verify_commands = [v for v in _vc if f not in str(v) and nf not in str(v)]
                if owner_file:      # 消费方改 readable 指向 owner 真实落点
                    rd = list(getattr(sc, "readable", None) or [])
                    if _norm_scope_path(owner_file) not in {_norm_scope_path(x) for x in rd}:
                        rd.append(owner_file)
                        sc.readable = rd
                sid = getattr(st, "id", None)
                if owner_id and sid and owner_id != sid and not _reaches_dep(owner_id, sid):
                    deps = list(getattr(st, "depends_on", None) or [])
                    if owner_id not in deps:
                        st.depends_on = deps + [owner_id]
                changed += 1
                logger.info(
                    "[DECONFLICT-XMOD] DR-09-F1(#101) 同 FQN %s 跨模块重复 create：契约 owner 模块=%s"
                    "（子任务 %s）；从子任务 %s 剥除 %s（改 readable+依赖 owner）",
                    fqn, auth, owner_id, sid, f)
    return changed


def contract_owner_ledger_block(contract: dict | None) -> str:
    """R67F-T3（层②·fan-out 前硬预算禁写清单）：从 shared_contract 提取已认领符号的【唯一 owner
    落点】，拼成分批 prompt 的硬约束禁写块——令每批 LLM 在【拆之前】就知道哪些类已有指定归属，
    从源头杜绝在别包重复 create 同名类（round67f 死因的预防层，与层③消解/层②熔断纵深互补）。

    round67f 死因：分批规划各批独立 LLM 调用、只见本批文件清单，看不到别的模块已认领的符号 →
    A 模块的 st 与 B 模块的 st 各自 create 同名(simple-name)异包类（AesUtils / AlarmAsyncConfig）→
    G1 ③b 打回 → 全量重拆 renumber 重犯。本块把契约的 defined_in 权威【前置广播】给每一批：
    "这些类已有唯一 owner，你若要用就 readable 引用其 FQN，【严禁】在别的包/模块重新 create 同名类"。

    栈中立：仅收 classpath_fqn_key 非 None（JVM 类路径命名空间）的 defined_in——同名异包冲突是
    JVM simple-name bean 命名空间特有问题（Spring/MyBatis），Go/Py/TS 同名跨包合法故天然不入清单。
    契约无 JVM 认领符号 → 返回空串（一字不加，不污染非 JVM 栈 prompt）。条目按 basename 去重排序、
    上限 60 条防 prompt 膨胀（超出静默截断=保守，宁少列不误导）。
    """
    interfaces = ((contract or {}).get("interfaces") or [])
    seen: dict[str, str] = {}       # basename -> owner 展示路径（首见为准，契约内首个权威）
    for e in interfaces:
        if not isinstance(e, dict):
            continue
        defined_in = str(e.get("defined_in") or "").strip()
        if not defined_in:
            continue
        key = classpath_fqn_key(defined_in)
        if not key:
            continue                # 非 JVM 类路径 → 不入禁写清单（栈中立）
        _mod, fqn = key
        base = fqn.rsplit("/", 1)[-1]        # 保原样大小写用于展示
        if base.lower() not in {b.lower() for b in seen}:
            seen[base] = _norm_scope_path(defined_in)
    if not seen:
        return ""
    rows = "\n".join(f"  - {b} → 唯一 owner：{p}"
                     for b, p in sorted(seen.items())[:60])
    return (
        "\n\n【硬约束-P8 已认领类唯一 owner（禁止同名异包重复创建）】以下类已由契约指定【唯一 owner "
        "落点】。本批若需使用它们，请在 scope.readable 引用其 owner 路径（import 该 FQN），"
        "【严禁】在其他包/模块的路径下 create 同名（simple name 相同）的类——JVM 类路径下同名类会导致"
        f"Spring bean 名冲突/启动失败，且会被确定性闸打回重拆：\n{rows}")


def _contract_owner_authority(
        shared_contract: dict | None) -> tuple[dict[str, str], set[str]]:
    """契约 defined_in 唯一权威：simple-name(lower) → owner FQN；同名两 owner=歧义入 set。

    ★R67G 修：扫【所有带 defined_in 的 section】（interfaces + dtos + …），非仅 interfaces★——
    round67g 铁证：枚举 AlarmLevelEnum/AlarmTypeEnum 的 defined_in 落在契约 `dtos` section
    （fields 是枚举常量），只读 interfaces 会漏掉 → 同名异包 create 消解无权威→fail-closed→
    LLM 无限重犯。权威已确定性存在于契约，只是没读。栈中立（classpath_fqn_key 门控）。
    """
    owner_fqn_by_base: dict[str, str] = {}
    ambiguous_base: set[str] = set()
    for sec in (shared_contract or {}).values():
        if not isinstance(sec, list):
            continue
        for e in sec:
            if not isinstance(e, dict):
                continue
            key = classpath_fqn_key(str(e.get("defined_in") or ""))
            if not key:
                continue
            _m, fqn = key
            base = fqn.rsplit("/", 1)[-1].lower()
            prev = owner_fqn_by_base.get(base)
            if prev is not None and prev != fqn:
                ambiguous_base.add(base)   # 契约自身给同 simple-name 两个不同 owner → 无唯一权威
            owner_fqn_by_base[base] = fqn
    return owner_fqn_by_base, ambiguous_base


def deconflict_same_name_cross_package_creates(plan: TaskPlan) -> int:
    """R67F-T1（层③）：同名(simple name)JVM 类被多子任务在【不同包】(异 FQN)各自 create 时，
    契约 defined_in 有唯一权威 owner → 确定性归一（保 owner 落点、其余异包副本剥除+改 readable+依赖 owner）。

    round67f 死因（task=ad7b1916，k3 连烧 2 轮同类重犯）：st-27-1 create com/ruoyi/alarm/util/
    AesUtils.java 与 st-6 create com/ruoyi/common/utils/encrypt/AesUtils.java = 同名异包重复设计
    （Spring bean 名默认取 simple name，两份并存启动即 ConflictingBeanDefinitionException；消费方
    也会解析到语义漂移的副本）。G1 ③b(R67-T1b，plan_validator._cross_package_same_basename_creates)
    正确 REJECT，但【纯打回】→LLM 全量重拆→renumber 后同接口原样重犯（轮1 st-27-1 / 轮2 st-11-1）
    →无限烧。本 pass 用【契约 defined_in】唯一权威（与 ③/#101 deconflict_cross_module_creates 同源
    判据，★绝不裸 basename 挑边——round67c 血泪：全局 basename 佐证会误合并合法通用名新类静默腐化★）
    确定性消解【有权威】的违例；无权威者（纯常量类等不在契约 interfaces）仍留 G1 ③b REJECT
    （fail-closed，绝不静默挑边），配合层② 去 st-id 规范化签名熔断止血。

    与 ③(#101) 互补且互斥：③ 判【同 FQN 跨物理模块】(相同包不同根)，本 pass 判【异 FQN 同
    simple-name 跨包】——判据（FQN 相等 vs 仅 basename 相等）不重叠。★必须【在 ③ 之后】跑★：③
    先塌缩同 FQN 跨模块副本，本 pass 面对的 owner FQN 恰有唯一创建者。栈中立（classpath_fqn_key
    仅 JVM 类路径命名空间非 None，资源/Go/Py/TS 天然豁免）；test 布局豁免（每模块一份
    ApplicationTests 是生态惯例）。返回被归一（剥除）的文件数。
    """
    subtasks = list(getattr(plan, "subtasks", None) or [])
    if len(subtasks) < 2:
        return 0
    # basename → {fqn -> [(subtask, create_path)]}（仅 JVM 源码 create，test 布局豁免）
    base_index: dict[str, dict[str, list]] = {}
    for st in subtasks:
        sc = getattr(st, "scope", None)
        for f in list(getattr(sc, "create_files", None) or []):
            norm = str(f).replace("\\", "/")
            parts = [p for p in norm.split("/") if p]
            if "test" in parts or "tests" in parts:
                continue        # test 布局豁免（保守：路径任一段为 test/tests 即豁免）
            key = classpath_fqn_key(f)
            if not key:
                continue        # 非 JVM 类路径源码天然豁免（栈中立）
            _mod, fqn = key
            base = fqn.rsplit("/", 1)[-1].lower()
            base_index.setdefault(base, {}).setdefault(fqn, []).append((st, f))
    # 契约 defined_in 权威（★R67G：扫所有带 defined_in 的 section，非仅 interfaces——枚举/DTO 在
    # dtos section，只读 interfaces 会漏权威→同名异包无从消解→LLM 无限重犯★）
    owner_fqn_by_base, ambiguous_base = _contract_owner_authority(
        getattr(plan, "shared_contract", None))
    by_id = {getattr(st, "id", None): st for st in subtasks}

    def _reaches_dep(start, target) -> bool:
        """start 是否经 depends_on 链到达 target（加边 st→owner 前防环）。"""
        seen, stack = set(), [start]
        while stack:
            cur = stack.pop()
            if cur == target:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            st = by_id.get(cur)
            if st is not None:
                stack.extend(getattr(st, "depends_on", None) or [])
        return False

    changed = 0
    for base, fqns in base_index.items():
        if len(fqns) < 2:
            continue          # 单一 FQN（同 FQN 跨模块由 ③ deconflict_cross_module_creates 处理）
        if base in ambiguous_base:
            continue          # 契约自身歧义 → fail-closed 留 ③b REJECT
        owner_fqn = owner_fqn_by_base.get(base)
        if not owner_fqn or owner_fqn not in fqns:
            continue          # 无契约权威 / 权威 owner 无人创建 → fail-closed 留 ③b REJECT（绝不静默挑边）
        owner_st, owner_file = fqns[owner_fqn][0]
        owner_id = getattr(owner_st, "id", None)
        for fqn, entries in fqns.items():
            if fqn == owner_fqn:
                continue       # owner 侧不动
            for st, f in entries:
                sc = getattr(st, "scope", None)
                if sc is None:
                    continue
                nf = _norm_scope_path(f)
                sc.create_files = [x for x in (getattr(sc, "create_files", None) or [])
                                   if _norm_scope_path(x) != nf]
                # 三面同步（同 ③ 猎手整改）：剥 create 的同时清掉【专门针对该文件】的验收/验证条目，
                # 否则子任务仍被 "X.java 按用途实现并编译通过" 误导去重建已剥离文件，抵消归一。
                # ★复核 Hunter#3(MEDIUM) 整改★：同名异包场景 AC/verify 常按【basename 文件名】
                # （"AesUtils.java 实现并编译"）而非全路径引用被剥文件——除全/规范路径外也按 basename
                # 剥（basename 含扩展名，误伤 MyAesUtils.java 概率极低且仅作用于本 dup 子任务）。
                _bn = f.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                _strip_refs = {r for r in (f, nf, _bn) if r}   # 全路径/规范路径/basename 任一命中即剥
                _ac = getattr(st, "acceptance_criteria", None)
                if _ac:
                    st.acceptance_criteria = [a for a in _ac
                                              if not any(r in str(a) for r in _strip_refs)]
                _hh = getattr(st, "harness", None)
                _vc = getattr(_hh, "verify_commands", None) if _hh is not None else None
                if _vc:
                    _hh.verify_commands = [v for v in _vc
                                           if not any(r in str(v) for r in _strip_refs)]
                if owner_file:      # 消费方改 readable 指向 owner 真实（异包）落点
                    rd = list(getattr(sc, "readable", None) or [])
                    if _norm_scope_path(owner_file) not in {_norm_scope_path(x) for x in rd}:
                        rd.append(owner_file)
                        sc.readable = rd
                sid = getattr(st, "id", None)
                if owner_id and sid and owner_id != sid and not _reaches_dep(owner_id, sid):
                    deps = list(getattr(st, "depends_on", None) or [])
                    if owner_id not in deps:
                        st.depends_on = deps + [owner_id]
                changed += 1
                logger.info(
                    "[DECONFLICT-SAMENAME] R67F-T1 同名 %s 跨包异 FQN 重复 create：契约 owner FQN=%s"
                    "（子任务 %s）；从子任务 %s 剥除 %s（异包副本改 readable+依赖 owner）",
                    base, owner_fqn, owner_id, sid, f)
    return changed


def deconflict_file_plan_same_name_creates(
    file_plan: list[dict],
    *,
    shared_contract: dict | None = None,
    project_path: str | None = None,
    base_ref: str | None = None,
) -> dict[str, int]:
    """R67G-T1/T2（file_plan 层·分批前【唯一】确定性杠杆）：同 simple-name JVM class create 消解。

    死因（task=b3659ca9 FAILED@PLAN，2026-07-23）：VALIDATE 期 G1 ③b/③f REJECT 4 违例
    （AlarmLevelEnum/AlarmTypeEnum 异 FQN 同 simple-name 跨包 create + SysMenu/SysUser
    create-vs-base shadow）。但重试从【恒定 tech_design_file_plan】重拆、只重新分组，batch LLM
    无权从 file_plan 删条目 → renumber 后原样重犯 → 层② 违例签名熔断 FAILED@PLAN（诚实止血但
    产不出合法 plan）。层③(deconflict_same_name_cross_package_creates)在【子任务级】且晚于分批，
    G1 ③b/③f 只 REJECT 不消解 → 本 pass 填【分批前 file_plan 唯一能改的确定性关口】这个空缺
    （brain/nodes/__init__.py _plan_ultra_batched：dedupe_file_plan 后、group_into_module_batches 前）。

    T1（本轮唯一确定性消解）异 FQN 同 simple-name 跨包 create 重复 → 【契约 defined_in】唯一权威裁
    owner，删非 owner 副本条目（同层③口径 classpath_fqn_key + owner_fqn_by_base；剥除后 resync 其它
    条目 depends_on 改指 owner 落点）。无契约权威/契约歧义 → fail-closed 留 ③b + 层②熔断兜底。

    ★create-vs-base shadow（SysMenu/SysUser 型：create/modify 撞 base 既有同名异路径类）本轮【不做
    确定性归位】★：以「base 同名唯一即归位」为权威=round67c 已被 ecc 复核判 HIGH 删除的裸 basename
    挑边（合法通用名新类 Config/Constants 撞无关 base→静默腐化），且 round67g 契约把 base 实体位置
    幻觉错（无安全权威）→ 交 G1 ③f 显式 REJECT（诚实 FAILED@PLAN 优于静默腐化），作独立前沿另治。

    ★fail-closed 铁律（round67c 血泪：裸 basename 全局佐证误合并合法通用名新类静默腐化，ecc 复核
    删过该自愈）★：任何"哪个是 owner/真身"判不出唯一确定性权威 → 绝不挑边，留 ③b/③f REJECT +
    层② 熔断兜底。栈中立：classpath_fqn_key 仅 JVM 类路径命名空间非 None（Go/Py/TS/资源天然豁免）；
    test 布局豁免（每模块一份 ApplicationTests 生态惯例，同层③）。返回计数 dict（可观测）。
    """
    counts = {"samename_creates_deduped": 0}
    if not file_plan:
        return counts

    def _is_test_path(path: str) -> bool:
        parts = [p for p in str(path).replace("\\", "/").split("/") if p]
        return "test" in parts or "tests" in parts

    # ── T1：异 FQN 同 simple-name 跨包 create 重复（契约 defined_in 唯一权威消解，同层③口径）──
    create_index: dict[str, dict[str, list]] = {}
    for e in file_plan:
        if not isinstance(e, dict):
            continue
        if str(e.get("action") or "create") != "create":
            continue
        key = classpath_fqn_key(str(e.get("path") or ""))
        if not key or _is_test_path(str(e.get("path") or "")):
            continue
        _mod, fqn = key
        base = fqn.rsplit("/", 1)[-1].lower()
        create_index.setdefault(base, {}).setdefault(fqn, []).append(e)

    # 契约 defined_in 权威（★扫所有带 defined_in 的 section——枚举/DTO 在 dtos，非 interfaces★）
    owner_fqn_by_base, ambiguous_base = _contract_owner_authority(shared_contract)

    _removed: set[int] = set()
    _removed_path_to_owner: dict[str, str] = {}   # 被剥副本路径 → owner 落点（供 depends_on resync）
    for base, fqns in create_index.items():
        if len(fqns) < 2:
            continue          # 单一 FQN（同 FQN 跨模块由 ③ deconflict_cross_module_creates 处理）
        if base in ambiguous_base:
            continue          # 契约歧义 → fail-closed 留 ③b
        owner_fqn = owner_fqn_by_base.get(base)
        if not owner_fqn or owner_fqn not in fqns:
            continue          # 无契约权威 / owner 无人建 → fail-closed（绝不裸挑边）
        _owner_path = _norm_scope_path(str((fqns[owner_fqn][0]).get("path") or ""))
        for fqn, entries in fqns.items():
            if fqn == owner_fqn:
                continue      # owner 侧不动
            for ent in entries:
                _removed.add(id(ent))
                _rp = _norm_scope_path(str(ent.get("path") or ""))
                if _rp and _owner_path:
                    _removed_path_to_owner[_rp] = _owner_path
                counts["samename_creates_deduped"] += 1
                logger.info(
                    "[DECONFLICT-FILEPLAN] R67G-T1 同名 %s 跨包异 FQN 重复 create：契约 owner=%s"
                    " → 剥离副本条目 %s（file_plan 层，分批前唯一杠杆）",
                    base, owner_fqn, ent.get("path"))
    if _removed:
        file_plan[:] = [e for e in file_plan if id(e) not in _removed]
        # depends_on resync（复核 Hunter#2）：其它条目若 depends_on 引被剥路径 → 改指 owner 落点，
        # 否则陈旧边被 group_into_module_batches 的 file 级排序回退【静默丢弃】（无信号）。
        for e in file_plan:
            if not isinstance(e, dict) or not e.get("depends_on"):
                continue
            _new: list = []
            _changed = False
            for d in (e.get("depends_on") or []):
                _own = _removed_path_to_owner.get(_norm_scope_path(str(d)))
                if _own:
                    _changed = True
                    if _own not in _new:
                        _new.append(_own)     # 改指 owner（去重）
                elif d not in _new:
                    _new.append(d)
            if _changed:
                e["depends_on"] = _new

    # ── create-vs-base shadow（SysMenu/SysUser 型）：本 file_plan 层【不做确定性归位】 ─────────
    # 以「base 同名唯一即归位」为权威=round67c 已被 ecc 复核判 HIGH【删除】的裸 basename 挑边（合法
    # 通用名新类 Config/Constants 撞无关 base 同名→被 modify 覆盖【静默腐化】）。★create-vs-base 的
    # modify-only 安全子集（LLM 显式 action=modify 却把落点写成 base 实体的幻觉异路径，SysUser 型）
    # 由子任务级 deconflict_create_vs_base_modify_shadow 归位★——它以【file_plan action=modify】为
    # 唯一安全信号（LLM 自认是"改既有类"，非 create 一个新类），故不复活裸 basename 挑边；create 侧
    # （SysMenu 型：LLM 认作新类）无此信号 → 仍交 G1 ③f 显式 REJECT（fail-closed，诚实 FAILED@PLAN
    # 优于静默腐化）+ 上游 contract/tech_design 前沿。project_path/base_ref 供 file_plan 层未来治本接入。
    _ = (project_path, base_ref)

    return counts


def deconflict_create_vs_base_modify_shadow(
    plan,
    file_plan: list | None,
    project_path: str | None = None,
    base_ref: str | None = None,
) -> int:
    """create-vs-base【modify-only 安全子集】确定性归位（子任务 scope 层，clear G1 ③f）。

    死型（task=b3659ca9 FAILED@PLAN，2026-07-23）：LLM 想【改】base 既有实体（SysUser），却把落点
    写成【幻觉异路径】——tech_design file_plan 里 `action=modify ruoyi-system/domain/SysUser.java`
    （真身在 base `ruoyi-common/.../entity/SysUser.java`），而 PLAN batch 把它落进某子任务的
    `create_files`（st-16-1）→ G1 ③f `_created_class_shadows_base`（读子任务 create_files）判
    create-vs-base shadow REJECT（MyBatis typeAlias 递归撞别名/两份并存启动崩）→ 重试从恒定 file_plan
    重拆原样重犯 → 层② 熔断，产不出合法 plan。

    ★唯一安全信号 = file_plan 里该 simple-name 的 action=modify（且【无】create 条目）★——即 LLM
    自认是"编辑既有类"，非"新建一个类"。凭此才敢把子任务 create_files 里的幻觉异路径归位到 base 真身
    （改 create→writable/modify）。★绝不复活裸 basename 挑边（round67c 血泪：合法通用名新类 Config/
    Constants 撞无关 base 同名 → 误合并静默腐化，ecc 复核判 HIGH 删过该自愈）★：SysMenu 型（file_plan
    action=create，LLM 认作新类）无 modify 信号 → 本 pass 不碰，仍交 G1 ③f 显式 REJECT（诚实
    FAILED@PLAN + 上游 tech_design 前沿），绝不静默归并。

    fail-closed 铁律（任一判不出唯一确定性证据即不动，留 ③f REJECT 兜底）：
      · 无 base 树（greenfield/非 git，_base_tree_listing 返 None）→ 整体跳过（不误伤纯新建）；
      · 无 file_plan → 无 modify 信号源 → 跳过；
      · base 同名【非唯一】命中（0 或 ≥2 处同 simple-name）→ 命名空间容忍/歧义，绝不挑边；
      · file_plan 该 simple-name 【无 modify】或【兼有 create】（意图歧义）→ 跳过；
      · create_files 落点【已精确 ∈ base 树】→ 归 R67-T8 规则0逆向降级 modify，本 pass 不重复处理。

    栈中立：classpath_fqn_key 仅 JVM 类路径命名空间非 None（Go/Py/TS/资源天然豁免）；test 布局
    路径豁免（同 ③f/层③）。归位为【同子任务内】create_files→writable(modify)、路径改指 base 真身，
    绝不清空 scope（永有 writable 承接）。depends_on 是子任务 ID 粒度、归位不动它，故无陈旧依赖边。

    ★readable 陈旧边的诚实边界（对抗复核 hunter PLAUSIBLE-2）★：归位只把 base 真身塞进【生产者
    (owner)】的 writable，不改【消费者】readable。**只有 elaborate 路径**（planning_nodes:2830 后
    紧跟 prune/provenance(pin/wire)/dangling 兜底）会把消费者 readable 重新布线到 base 真身；
    revision(nodes:5247)/plan_inject(:172) 调 resolve_plan_conflicts 时其后【不跑】那些兜底 → 消费者
    可能残留指向已剥幻觉路径的 readable。危害有界=退化的 curated 上下文（base 真身是磁盘既有文件、
    worker 仍可 cat；非硬失败、非需求丢失），故不在此另造 readable 重布线（避免与 provenance 双实现）。
    live 主路径(PLAN→ELABORATE→VALIDATE)经 elaborate 兜底，无此残留。返回归位条数（可观测）。
    """
    subtasks = list(getattr(plan, "subtasks", None) or [])
    if not subtasks:
        return 0
    tree = _base_tree_listing(project_path, base_ref)
    if not tree or not file_plan:
        return 0                              # 无 base 权威 / 无 modify 信号源 → fail-closed 跳过

    def _is_test_path(path: str) -> bool:
        parts = [p for p in str(path).replace("\\", "/").split("/") if p]
        return "test" in parts or "tests" in parts

    # base 真身索引：simple-name(lower, 含扩展，同 ③f 口径) → [base 路径…]（仅 JVM 类路径）
    base_by_simple: dict[str, list[str]] = {}
    for p in tree:
        k = classpath_fqn_key(p)
        if not k:
            continue
        _m, fqn = k
        base_by_simple.setdefault(fqn.rsplit("/", 1)[-1].lower(), []).append(_norm_scope_path(p))

    # file_plan 意图信号【★路径粒度★，对抗双复核 HIGH/PLAUSIBLE-1 整改】：只按 simple-name 匹配
    # "存在某个同名 modify 条目"会误授权——file_plan 对【另一个不同类】的 modify 会把一个本该新建的
    # 同名类（如脚手架/契约符号安置注入、其路径不在 file_plan）误归并进无关 base（复活 round67c 腐化）。
    # 收紧：被归位的 create_files 落点【本身】须在 file_plan 声明为 action=modify（真死型满足：PLAN
    # batch 从 file_plan 派生 create_files，故 create 路径 == file_plan modify 路径=那个幻觉异路径）。
    fp_modify_paths: set[str] = set()
    fp_create_paths: set[str] = set()
    for e in (file_plan or []):
        if not isinstance(e, dict):
            continue
        _p = str(e.get("path") or "")
        if not classpath_fqn_key(_p):
            continue                          # 仅 JVM 类路径（栈中立）
        if str(e.get("action") or "create") == "modify":
            fp_modify_paths.add(_norm_scope_path(_p))
        else:
            fp_create_paths.add(_norm_scope_path(_p))

    tree_set = {_norm_scope_path(p) for p in tree}
    relocated = 0
    for st in subtasks:
        sc = getattr(st, "scope", None)
        if sc is None:
            continue
        creates = list(getattr(sc, "create_files", None) or [])
        if not creates:
            continue
        _new_creates: list = []
        _to_writable: list = []
        for f in creates:
            norm = _norm_scope_path(f)
            if _is_test_path(norm):
                _new_creates.append(f)
                continue
            k = classpath_fqn_key(f)
            if not k:
                _new_creates.append(f)
                continue
            _m, fqn = k
            simple = fqn.rsplit("/", 1)[-1].lower()
            if norm in tree_set:
                _new_creates.append(f)        # 精确 ∈ base 树 → R67-T8 逆向处理，本 pass 不碰
                continue
            hits = base_by_simple.get(simple) or []
            if len(hits) != 1 or hits[0] == norm:
                _new_creates.append(f)        # base 同名非唯一 / 就是自己 → fail-closed 不动
                continue
            # ★路径粒度安全信号★：该 create_files 落点【本身】须被 file_plan 声明为 modify（LLM 显式
            # "改此路径"），且未被同路径 create 声明（同路径兼 create/modify=意图歧义 fail-closed）。
            if norm not in fp_modify_paths or norm in fp_create_paths:
                _new_creates.append(f)        # 落点非 file_plan modify / 同路径歧义 → 留 ③f REJECT
                continue
            # 安全信号齐备：LLM 显式改既有类却把 create_files 落点写成幻觉异路径 → 归位到 base 真身
            _base_path = hits[0]
            _to_writable.append(_base_path)
            relocated += 1
            logger.warning(
                "[DECONFLICT-CVB] create-vs-base modify-shadow 归位：%s 的 create_files %s "
                "（file_plan action=modify）→ base 真身 %s 改 writable(modify)（G1 ③f 治本）",
                getattr(st, "id", "?"), norm, _base_path)
        if _to_writable:
            sc.create_files = _new_creates
            _w = list(getattr(sc, "writable", None) or [])
            for b in _to_writable:
                if b not in _w:
                    _w.append(b)
            sc.writable = _w
    return relocated


def resolve_plan_conflicts(plan: TaskPlan, project_path: str | None = None,
                           base_ref: str | None = None,
                           file_plan: list | None = None) -> dict[str, int]:
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
        # #101 先跑：剥掉契约有权威 owner 的跨模块重复 create（同 FQN），后续 pass 只看干净 scope。
        "xmod_creates_deconflicted": deconflict_cross_module_creates(plan),
        # R67F-T1（层③）紧随 ③ 之后：同名异包（异 FQN 同 simple-name）有契约权威者确定性消解。
        # ★必须在 ③ 之后★：③ 先塌缩同 FQN 跨模块副本 → 本 pass 面对的 owner FQN 恰有唯一创建者。
        "samename_creates_deconflicted": deconflict_same_name_cross_package_creates(plan),
        # create-vs-base【modify-only 安全子集】：LLM 显式 action=modify 却把子任务 create_files 落点
        # 写成 base 实体幻觉异路径（SysUser 型）→ 归位到 base 真身（改 modify）。★必须在 normalize
        # 之前★：归位可能造 st-x/st-y 同文件双写者，交下方 normalize_plan_scopes 串行化收敛。无 file_plan
        # modify 信号（SysMenu 型 create）不碰，留 G1 ③f REJECT（fail-closed，绝不裸 basename 挑边）。
        "cvb_modify_shadow_relocated": deconflict_create_vs_base_modify_shadow(
            plan, file_plan, project_path=project_path, base_ref=base_ref),
        "scaffolds_merged": dedupe_module_scaffolds(plan),
        "dep_reordered": int(fix_dependency_ordering(plan)),
        "scope_normalized": int(normalize_plan_scopes(plan, project_path=project_path, base_ref=base_ref)),
        "difficulty_bumped": bump_scaffold_difficulty(plan),
    }


# 6.9-HF9：dedupe_module_scaffolds 机器追加段的固定定界符（签名剥离锚点，勿改措辞）
MERGED_DUP_DELIM = "\n[MERGED-DUP]；（并入重复脚手架语义）"


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
                # D14（阶段6，登记册 §五）：dup 其余 scope 成员不再丢弃——delete_files/
                # create_dirs 也并集（此前只并 3 字段，dup 的删除/建目录意图静默蒸发）。
                for _fld in ("delete_files", "create_dirs"):
                    if hasattr(cs, _fld) or hasattr(ds, _fld):
                        setattr(cs, _fld, _union_keep_order(
                            list(getattr(cs, _fld, None) or []),
                            list(getattr(ds, _fld, None) or [])))
            canon.depends_on = _union_keep_order(getattr(canon, "depends_on", []),
                                                 getattr(dup, "depends_on", []))
            # D14：验收标准/描述并集——dup 独有的 acceptance_criteria 丢弃=验收面缩水；
            # description 追加（去重）保住 dup 语义供 worker prompt。
            _ac = _union_keep_order(
                list(getattr(canon, "acceptance_criteria", None) or []),
                list(getattr(dup, "acceptance_criteria", None) or []))
            if _ac:
                canon.acceptance_criteria = _ac
            _dd = (getattr(dup, "description", "") or "").strip()
            # R65C-T1 毒株(b) 源头面：dup 描述【尾部】的权威模板围栏（含其【…】标头）
            # 绝不随注记并入——canon 自有权威模板，双模板+注记文本会被 trivial 快路径
            # 原样写进文件（round65c 实锤：[MERGED-DUP] 注记进 ruoyi-alarm/pom.xml 致
            # XML 非法）。猎手(b)：只剥**尾部**围栏块（循环剥多块），不动中段引用——
            # R58-3 认领型 dup 的自由文本里合法围栏不误伤；worker 出口另有剥离兜底。
            import re as _re_dd
            while True:
                _dd2 = _re_dd.sub(
                    r"(?:\n?【[^\n]*】)?\s*```[a-zA-Z]*\n.*?```\s*$", "", _dd,
                    count=1, flags=_re_dd.S).strip()
                if _dd2 == _dd:
                    break
                _dd = _dd2
            if _dd and _dd not in (getattr(canon, "description", "") or ""):
                # 6.9-HF9：机器追加段用固定定界符——_subtask_signature 含 description 全文，
                # 两轮 replan 的 dup 集不同（常态）会使 canon 描述串漂移 → 签名不等 →
                # 外科 reset 把已完成态/配额表误剪（白重跑）。签名侧按定界符剥机器段。
                # DR-01-F5(#50) 治本：绝不对【canon 原有内容】做尾截断。canon 自身 description 可
                # 已 >2000（内嵌完整权威 pom `\`\`\`xml…</project>\`\`\``），旧 `(canon+DELIM+dup)[:2000]`
                # 会从 2000 处切掉 canon 模板尾 → worker trivial 快路径写入半截 XML → pom 非法/reactor
                # 中毒（round65c 同族事故）。改为只截【追加的 dup 片段】；canon 已占满预算则不追加
                # （dup 语义由 acceptance/covers 并集守恒）。
                _canon_desc = getattr(canon, "description", "") or ""
                _room = 2000 - len(_canon_desc) - len(MERGED_DUP_DELIM)
                if _room > 0:
                    canon.description = _canon_desc + MERGED_DUP_DELIM + _dd[:_room]
            drop_to_canon[dup.id] = canon.id
            merged += 1
    if not merged:
        return 0
    plan.subtasks = [s for s in subs if s.id not in drop_to_canon]
    # 重映射所有下游依赖到 canonical，去自依赖
    for s in plan.subtasks:
        s.depends_on = sorted({drop_to_canon.get(d, d) for d in (getattr(s, "depends_on", []) or [])
                               if drop_to_canon.get(d, d) != s.id})
    # D10：删掉重复脚手架子任务后同步 parallel_groups——剔除悬空引用+清空空组，
    # 否则 plan_validator "parallel_groups 含未知子任务" 硬失败，叠加 D09 盲重试死循环。
    if getattr(plan, "parallel_groups", None):
        from swarm.brain.plan_batch import prune_parallel_groups
        plan.parallel_groups = prune_parallel_groups(
            plan.parallel_groups, {s.id for s in plan.subtasks})
    logger.info("[ELABORATE] 重复模块脚手架合并：%d 个重复脚手架并入 canonical(杜绝冗余地基,治严重文件冲突)",
                merged)
    return merged


def _graph_has_cycle(graph: dict) -> bool:
    """迭代三色 DFS 判环（只走 graph 内节点；确定性，无递归深度风险）。"""
    white, gray, black = 0, 1, 2
    color = dict.fromkeys(graph, white)
    for root in graph:
        if color[root] != white:
            continue
        stack = [(root, iter(graph[root]))]
        color[root] = gray
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if nxt not in graph:
                    continue
                if color[nxt] == gray:
                    return True
                if color[nxt] == white:
                    color[nxt] = gray
                    stack.append((nxt, iter(graph[nxt])))
                    advanced = True
                    break
            if not advanced:
                color[node] = black
                stack.pop()
    return False


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

    # 规则 2：脚手架置根——D15（阶段6，登记册 §五）：不再无条件清空。脚手架间的
    # 真实依赖（父 pom 先于子模块清单、根 workspace 先于成员）是合法上游序，抹平
    # 置根会让 greenfield 并行建清单撞 reactor 时序错误且无回补。只剥指向【非脚手架】
    # 的依赖（那才是规则2 要治的"脚手架被业务代码倒挂"）。
    for st in subs:
        _deps = list(getattr(st, "depends_on", None) or [])
        if st.id in scaffold_ids and _deps:
            _kept_deps = [d for d in _deps if d in scaffold_ids]
            if _kept_deps != _deps:
                st.depends_on = _kept_deps
                changed = True

    # 6.9-HF8：D15 保留 scaffold→scaffold 边 + dedupe_module_scaffolds 的 depends_on 并集
    # 可能【新造环】；旧规则2 的无条件清空恰是天然破环器，D15 拆掉后环会存活到
    # plan_validator 硬失败 → replan（LLM 大概率复现同环）→ 熔断烧钱。此处确定性破环：
    # 仅在脚手架子图真成环时，按子任务原序剥【后向边】（与 plan_batch 的
    # break_dependency_cycles 同法）；无环时一条不动（D15 语义零回归）。
    if scaffold_ids:
        _pos = {st.id: i for i, st in enumerate(subs)}
        _sg = {st.id: [d for d in (getattr(st, "depends_on", None) or []) if d in scaffold_ids]
               for st in subs if st.id in scaffold_ids}
        if _graph_has_cycle(_sg):
            for st in subs:
                if st.id not in scaffold_ids:
                    continue
                _deps = list(getattr(st, "depends_on", None) or [])
                _nd = [d for d in _deps
                       if not (d in scaffold_ids and _pos.get(d, -1) > _pos[st.id])]
                if _nd != _deps:
                    logger.warning(
                        "[PLAN-NORM] 6.9-HF8 脚手架依赖成环，确定性剥后向边：%s 剥 %s",
                        st.id, sorted(set(_deps) - set(_nd)))
                    st.depends_on = _nd
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
