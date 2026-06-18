"""共享契约 — Brain 统一定义、注入 Worker、L2 校验。"""

from __future__ import annotations

import json
import re
from typing import Any

from swarm.types import TaskPlan

# Maven `-pl <module>` 提取（reactor 模块选择）。
_MVN_PL_RE = re.compile(r"-pl\s+([^\s,]+)")


def _ensure_maven_module_build_scope(subtasks: list) -> bool:
    """规则3：Maven 新模块构建闸门【可满足性】补全（现场 task 69d34b1b）。

    现场：子任务新建 `ruoyi-alarm-app/src/...` 下 7 个文件，验收 `mvn -pl ruoyi-alarm-app -am compile`，
    但模块自己的 `pom.xml` 与父 `pom.xml` 的 `<module>` 注册都不在任何 scope →
    `Could not find the selected project in the reactor` 必败、worker 够不着、空转到超时升级。

    规则：凡子任务 build/test/verify/acceptance 命令含 `-pl <module>` 且该 `<module>/` 目录下
    在本计划里有 create_files（=正在新建该模块）：
    - `<module>/pom.xml` → 归该子任务 create_files（各模块自己的 POM，不同文件，无争用）；
    - 根 `pom.xml`（父 `<modules>` 注册）→【单一归属】只归第一个这样的子任务，并从其余同类
      子任务的写权移除。否则 N 个模块子任务都写同一根 pom → 验证器判 N 路写冲突（task 927d95c6
      实证：45 子任务里 40+ 个都写 pom.xml，VALIDATE_PLAN 结构校验失败、刷屏、重试耗尽）。
    pom 已存在时 executor 的 scope 归一会把 create 自动降级为 modify，故对"模块已存在"也安全。
    """
    changed = False
    root_pom = "pom.xml"
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

    # 找出所有"新建 Maven 模块"的子任务（有 -pl <module> 且该模块目录下有 create_files）。
    builders: list = []
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
        if modules:
            builders.append((st, scope, modules))

    if not builders:
        return False

    # ① 各模块自己的 <module>/pom.xml → 归该 builder（不同文件，无争用）。
    for _st, scope, modules in builders:
        creates = list(getattr(scope, "create_files", []) or [])
        for mod in modules:
            mod_pom = f"{mod}/pom.xml"
            if mod_pom not in all_write_targets:
                creates.append(mod_pom)
                all_write_targets.add(mod_pom)
                changed = True
        scope.create_files = creates

    # ② 根 pom.xml 单一归属：只归第一个 builder；从其余 builder 写权移除，杜绝 N 路争写。
    owner_scope = builders[0][1]
    owner_writ = list(getattr(owner_scope, "writable", []) or [])
    owner_cre = list(getattr(owner_scope, "create_files", []) or [])
    if root_pom not in owner_writ and root_pom not in owner_cre:
        owner_writ.append(root_pom)
        owner_scope.writable = owner_writ
        changed = True
    for _st, scope, _modules in builders[1:]:
        w = list(getattr(scope, "writable", []) or [])
        c = list(getattr(scope, "create_files", []) or [])
        if root_pom in w or root_pom in c:
            scope.writable = [f for f in w if f != root_pom]
            scope.create_files = [f for f in c if f != root_pom]
            changed = True

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

    # ── 规则 3（先于规则1跑）：Maven 新模块构建闸门可满足性补全（治本 task 69d34b1b）。
    # 放规则1前，使补进来的 pom 也受"同文件写权唯一"去重/串行化（多模块子任务不并发抢写根 pom）。
    changed = _ensure_maven_module_build_scope(subtasks) or changed

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
