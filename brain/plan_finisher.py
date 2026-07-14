"""R41 确定性收尾器（round41 治本批）：PLAN 产出后、VALIDATE 前的零 LLM 修复。

round41 死因（task 3740e421 取证）：确定性修复能力齐备但接线互斥——
1. R40-1 孤儿文件挂靠只活在 maybe_file_plan_repair（task_plan is None 才走），
   P1 覆盖外科抢跑产出 plan 后缺件带病重验，最后一轮重试 9 秒原地复死：
   一个 `sql/alarm_notice_read.sql` 无 owner 杀掉 2h22min 的 90 子任务计划。
2. R39-4 脚手架注入只接线在 maybe_symbol_repair 内部：符号外科修不了硬符号
   如实回退时，注入随被丢弃的候选一起蒸发，全量重拆的新 plan 无人再注
   ——规则5 预警 11 模块贯穿三轮原样复现。

治本：无论哪条路径产出 plan（P1 外科 / R39-5 符号外科 / R40-1 缺件外科 /
LLM 全量重拆 / ULTRA 分批），进 VALIDATE 前统一跑本收尾器：
  ① inject_build_scaffold_subtasks —— 规则5 落空模块注入 pom 脚手架
     （unclaimed_contract_deps 只报"无人拥有该模块 pom"，结构上不与既有写者相撞）；
  ② attach_orphan_file_plan_entries —— file_plan 孤儿文件按同模块最深前缀挂靠，
     挂不上的 fail-open 留给 VALIDATE 如实打回（不越权猜挂）。
两步均幂等、确定性；VALIDATE 仍是权威判定，收尾器只消解机械可修缺口。
外科通道保留：它们额外提供"跳过 LLM 全量重拆"的成本优化，与收尾器不冲突。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _synthesize_orphan_subtasks(plan, orphans: list[str], file_plan,
                                project_path: str | None,
                                _task_description: str = "") -> dict[str, list[str]]:
    """R48-1：为挂靠无候选的 file_plan 孤儿按顶层模块新建子任务 → {sid: [paths]}。

    确定性、幂等；描述带上 file_plan 条目的 purpose（worker 拿到明示意图）；文件
    已存在于基线 → writable（改），否则 create_files（建）；同模块有脚手架子任务
    → depends_on（先有 pom 再写码）；parallel_groups 完整性守约（与 SCAFFOLD-INJECT
    同款接线；dispatch 纯 depends_on 驱动，组序无拓扑约束）。
    复核 F1：sid 撞既有 st-fileplan-* 时【收养进既有子任务】而非丢弃整组——
    continue 会让后到孤儿每轮原样打回=round48 死法换壳；复核 F2：组内按
    _MAX_FILES_PER_GROUP 预分片，绝不确定性造出超 validate 文件上限的子任务。
    """
    from pathlib import Path

    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskIntent
    _MAX_FILES_PER_GROUP = 6  # < validate 硬闸 12，且给 ELABORATE 按实体拆留余量
    # purpose 索引：归一路径 → file_plan 条目描述文本
    purpose: dict[str, str] = {}
    for e in (file_plan or []):
        if isinstance(e, dict) and e.get("path"):
            p = str(e["path"]).replace("\\", "/").strip("/")
            txt = str(e.get("purpose") or e.get("description") or "").strip()
            if txt:
                purpose[p] = txt
    groups: dict[str, list[str]] = {}
    for f in orphans:
        p = str(f).replace("\\", "/").lstrip("/")
        groups.setdefault(p.split("/", 1)[0] if "/" in p else "root", []).append(p)
    by_id = {st.id: st for st in plan.subtasks}
    created: dict[str, list[str]] = {}

    def _fmt(paths: list[str]) -> str:
        return "\n".join(
            f"- {p}" + (f"：{purpose[p]}" if p in purpose else "") for p in paths)

    for mod, paths in sorted(groups.items()):
        base_sid = f"st-fileplan-{mod}"
        # 复核 F1：既有承接子任务 → 收养（追加 scope+描述+验收），绝不丢弃
        if base_sid in by_id and by_id[base_sid].id.startswith("st-fileplan-"):
            host = by_id[base_sid]
            adopt = [p for p in paths
                     if p not in host.scope.create_files
                     and p not in host.scope.writable]
            if adopt:
                for p in adopt:
                    exists = bool(project_path) and (Path(project_path) / p).is_file()
                    (host.scope.writable if exists
                     else host.scope.create_files).append(p)
                    host.acceptance_criteria.append(
                        f"{p} 按 file_plan 用途实现并编译通过")
                host.description += "\n【file_plan 承接·追加】\n" + _fmt(adopt)
                created[base_sid] = adopt
            continue
        # 复核 F2：预分片防超限
        chunks = [paths[i:i + _MAX_FILES_PER_GROUP]
                  for i in range(0, len(paths), _MAX_FILES_PER_GROUP)]
        for ci, chunk in enumerate(chunks):
            sid = base_sid if ci == 0 else f"{base_sid}-{ci + 1}"
            if sid in by_id:
                continue
            writable, create = [], []
            for p in chunk:
                exists = bool(project_path) and (Path(project_path) / p).is_file()
                (writable if exists else create).append(p)
            st = SubTask(
                id=sid,
                description=(
                    f"【file_plan 承接】技术方案 file_plan 规划了以下 {mod} 模块文件，"
                    "但无子任务承接（收尾器确定性新建本子任务）。按各文件用途完整实现：\n"
                    + _fmt(chunk)),
                intent=TaskIntent.MODIFY if writable and not create else TaskIntent.CREATE,
                difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=writable, create_files=create),
                acceptance_criteria=[
                    f"{p} 按 file_plan 用途实现并编译通过" for p in chunk],
            )
            scaffold_sid = f"st-scaffold-{mod}"
            if scaffold_sid in by_id:
                st.depends_on.append(scaffold_sid)
            plan.subtasks.append(st)
            by_id[sid] = st
            if plan.parallel_groups:
                plan.parallel_groups.append([sid])
            created[sid] = chunk
    if created:
        logger.info(
            "[PLAN-FINISH] R48-1 孤儿无候选 → 确定性新建/收养承接子任务 %d 个: %s",
            len(created), {k: v[:3] for k, v in created.items()})
    return created


def _domicile_contract_symbols(plan, shared_contract, project_path: str | None,
                               task_description: str) -> dict[str, list[str]]:
    """R48b-1（收尾器第④步）：C1 无主硬符号按契约模块确定性安置 → {sid: [symbols]}。

    round48b 死因：P1 覆盖外科命中即短路 R39-5 符号外科（first-match 互斥残留），
    19 个无主硬符号最后一轮无人处理三连耗尽 REJECTED；且外科"挂靠"只能挂到既有
    文件——契约细粒度模块（14 个）在 plan 中无代码文件时 61 符号无处可挂。治=
    VALIDATE 提示语的治法机械化："在其 create_files 安排 <符号名>.<扩展名> 文件"：
    为每个有模块归属的无主硬符号新建/收养 st-contract-<mod> 实现子任务。
    路径推导（多栈通用，不写死语言）：扩展名=plan 既有 create_files 众数扩展名；
    源前缀=同模块既有文件目录 > 全 plan 众数源根模式（模块名替换）> {mod}/src/。
    C1 owner 判据只看 basename（basename_owns_symbol），路径形状不影响过闸；
    包声明↔路径对齐交 worker + L1.1b 既有闸。module 归属缺失的符号如实留给
    VALIDATE（不越权猜模块）。幂等：符号已被拥有/子任务已含该文件即跳过。
    """
    from pathlib import Path

    from swarm.brain.contract_utils import contract_symbols_with_module
    from swarm.brain.plan_validator import unowned_contract_symbols
    from swarm.types import FileScope, SubTask, SubTaskDifficulty

    entries = contract_symbols_with_module(shared_contract)
    if not entries:
        return {}
    import re as _re
    _HARD = {"interfaces", "types", "apis", "symbols"}
    sym_set = {e["symbol"] for e in entries}
    # 复核 F1：符号名标识符白名单——dict 条目 name 是未净化 LLM 字符串，脏名
    # （"GET /x/Export"、"IFoo<T>"、"../X"）直通会拼出垃圾/穿越路径；不合格如实留 VALIDATE
    _ident = _re.compile(r"^[A-Za-z_]\w*$")
    hard = [e for e in entries
            if e.get("kind") in _HARD
            and e["symbol"] and _ident.fullmatch(e["symbol"])
            and not e["symbol"][0].islower()
            and not ("." in e["symbol"] and e["symbol"].split(".", 1)[0] in sym_set)]
    if not hard:
        return {}
    unowned = set(unowned_contract_symbols(plan, [e["symbol"] for e in hard]))
    todo = [e for e in hard if e["symbol"] in unowned
            and e.get("module") and _ident.fullmatch(
                e["module"].replace("-", "_").replace("/", ""))]
    if not todo:
        return {}
    # 路径推导素材：plan 既有 create_files 的扩展名众数 + 各模块目录样本。
    # 复核 F2：已知源根顶段（src/app/lib 等）不是模块名——单模块工程 `src/main/...`
    # 的 top="src" 被当模块吃掉会让模板丢 src 段（文件落 {mod}/main/java/... =
    # L1.1b fqn 解析不到 + reactor 编不到的永久死文件）。源根形态记入 "" 键，
    # 模板取【完整目录】。
    _SRC_ROOTS = {"src", "app", "lib", "source", "sources"}
    from collections import Counter
    exts: Counter = Counter()
    mod_dirs: dict[str, Counter] = {}
    for st in plan.subtasks:
        sc = getattr(st, "scope", None)
        for f in (list(getattr(sc, "create_files", None) or [])
                  + list(getattr(sc, "writable", None) or [])):
            p = str(f).replace("\\", "/").lstrip("/")
            base = p.rsplit("/", 1)[-1]
            if "." in base and not base.startswith("pom."):
                ext = base.rsplit(".", 1)[-1].lower()
                if ext not in ("xml", "yml", "yaml", "properties", "sql", "md"):
                    exts[ext] += 1
            if "/" not in p:
                continue
            top, rest = p.split("/", 1)
            if top in _SRC_ROOTS:
                mod_dirs.setdefault("", Counter())[p.rsplit("/", 1)[0]] += 1
            elif "/" in rest:
                mod_dirs.setdefault(top, Counter())[rest.rsplit("/", 1)[0]] += 1
    if not exts:
        # 复核 F3：无源码扩展名证据 → 不猜语言（多栈铁律），本步 fail-open 留 VALIDATE
        logger.info("[PLAN-FINISH] R48b-1 无源码扩展名证据（纯配置/SQL plan）→ "
                    "符号安置跳过，留 VALIDATE 权威打回")
        return {}
    ext = exts.most_common(1)[0][0]
    # 全 plan 众数源根模式（模块前缀已剥；单模块 "" 键为完整目录）
    all_dir = Counter()
    for c in mod_dirs.values():
        all_dir.update(c)
    tpl_dir = all_dir.most_common(1)[0][0] if all_dir else "src"

    def _dir_for(mod: str) -> str:
        if mod in mod_dirs:
            return f"{mod}/{mod_dirs[mod].most_common(1)[0][0]}"
        if "" in mod_dirs and mod_dirs[""]:
            # 单模块布局：模板已是完整目录（含 src 段），前缀模块 + 尾段包名
            seg = mod.replace("_", "-").split("-")[-1]
            return f"{mod}/{mod_dirs[''].most_common(1)[0][0]}/{seg}"
        seg = mod.replace("_", "-").split("-")[-1]
        return f"{mod}/{tpl_dir}/{seg}"

    groups: dict[str, list[str]] = {}
    for e in todo:
        groups.setdefault(e["module"], []).append(e["symbol"])
    by_id = {st.id: st for st in plan.subtasks}
    created: dict[str, list[str]] = {}
    _MAX = 6
    for mod, syms in sorted(groups.items()):
        base_sid = f"st-contract-{mod}"
        host = by_id.get(base_sid)
        if host is not None:
            # 收养：追加缺的符号文件（R48-1 F1 同款，绝不丢弃后到符号）。
            # 复核 F4：收养也受 _MAX 约束——host 满员后溢出走下方分片新建；
            # 收养后按增量抬 est（只在 falsy 时设置会让旧小预算带大 scope）。
            have = {str(f).rsplit("/", 1)[-1].split(".", 1)[0]
                    for f in host.scope.create_files}
            adopt_all = [s for s in syms if s not in have]
            room = max(0, _MAX - len(host.scope.create_files)
                       - len(host.scope.writable))
            adopt, syms = adopt_all[:room], adopt_all[room:]
            if adopt:
                d = _dir_for(mod)
                for s in adopt:
                    host.scope.create_files.append(f"{d}/{s}.{ext}")
                    host.acceptance_criteria.append(f"契约符号 {s} 已定义并编译通过")
                host.description += "\n【契约符号安置·追加】\n" + "\n".join(
                    f"- {s}" for s in adopt)
                host.est_context_tokens = (
                    getattr(host, "est_context_tokens", 0) or 0) + 6000 * len(adopt)
                created[base_sid] = adopt
            if not syms:
                continue
            # 溢出符号落到 -2/-3… 分片（下方通用路径，sid 已存在的片自动跳过）
        chunks = [syms[i:i + _MAX] for i in range(0, len(syms), _MAX)]
        # sid 分配：跳过已占用后缀但【绝不丢符号】（host 溢出时 chunk0 落 -2 起）
        _suffixes = iter([base_sid] + [f"{base_sid}-{n}" for n in range(2, 99)])
        for chunk in chunks:
            sid = next(s for s in _suffixes if s not in by_id)
            d = _dir_for(mod)
            files = [f"{d}/{s}.{ext}" for s in chunk]
            st = SubTask(
                id=sid,
                description=(
                    f"【契约符号安置】契约模块 {mod} 的以下符号无子任务承接"
                    "（收尾器确定性新建本子任务）。按共享契约定义完整实现每个符号"
                    "（接口/类型按契约签名，落在对应文件）：\n"
                    + "\n".join(f"- {s} → {d}/{s}.{ext}" for s in chunk)),
                difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=[], create_files=files),
                contract={"symbols": list(chunk), "module": mod},
                acceptance_criteria=[
                    f"契约符号 {s} 已定义并编译通过" for s in chunk],
            )
            scaffold_sid = f"st-scaffold-{mod}"
            # 复核 F5：新顶层模块无 pom 无注册 = r46 reactor missing-child 同款毒。
            # 模块物理不存在且 plan 无其文件且无脚手架 → 确定性补注（R45-2 权威模板
            # 同源），代码子任务依赖之；root pom 注册交 workspace reconcile add 侧。
            if (scaffold_sid not in by_id and project_path
                    and mod not in mod_dirs
                    and not (Path(project_path) / mod).is_dir()):
                try:
                    from swarm.brain.contract_utils import (
                        _deterministic_pom_template,
                    )
                    _tpl = _deterministic_pom_template(mod, [], project_path)
                    if _tpl:
                        sc_st = SubTask(
                            id=scaffold_sid,
                            description=(
                                f"【构建脚手架】为模块 {mod} 创建构建文件 "
                                f"{mod}/pom.xml\n【权威 pom 模板（确定性生成，原样"
                                "写入；仅当项目另有明确约定才允许在此基础上增改，"
                                f"绝不重构结构）】\n```xml\n{_tpl}\n```"),
                            difficulty=SubTaskDifficulty.TRIVIAL,
                            scope=FileScope(writable=[],
                                            create_files=[f"{mod}/pom.xml"]),
                            acceptance_criteria=[
                                f"{mod}/pom.xml 存在且可被 reactor 解析"],
                        )
                        plan.subtasks.append(sc_st)
                        by_id[scaffold_sid] = sc_st
                        if plan.parallel_groups:
                            plan.parallel_groups.append([scaffold_sid])
                except Exception:  # noqa: BLE001 — 补注失败不阻断安置本体
                    logger.warning(
                        "[PLAN-FINISH] R48b-1 模块 %s 脚手架补注失败（fail-open）",
                        mod, exc_info=True)
            if scaffold_sid in by_id:
                st.depends_on.append(scaffold_sid)
            plan.subtasks.append(st)
            by_id[sid] = st
            if plan.parallel_groups:
                plan.parallel_groups.append([sid])
            created[sid] = chunk
    if created:
        logger.info(
            "[PLAN-FINISH] R48b-1 契约符号安置：无主硬符号 %d 个 → 新建/收养 %d 个"
            "承接子任务: %s", len(todo), len(created),
            {k: v[:4] for k, v in created.items()})
    return created


def finish_plan_deterministic(plan, file_plan, project_path: str | None = None,
                              task_description: str = "",
                              shared_contract: dict | None = None) -> dict:
    """对 plan 原地跑确定性收尾（脚手架注入 + 孤儿挂靠 + 契约符号安置）。

    返回机读摘要 {scaffolds, orphans_attached, orphans_left, ...}；任何一步异常
    fail-open（收尾器绝不拖垮 PLAN 节点，缺口留给 VALIDATE 权威打回）。
    接线位置（复核 F1 定案）：PLAN 后处理区【末端】（#6 覆盖单调化之后）——收尾器
    改 scope 会让 #6 的 scope 身份键漂移，放末端保证 #6 两侧比较的都是 LLM 原始
    scope；挂靠记录进 plan.finisher_attached 供 #6 跨轮对称剔除。注入的脚手架
    因此错过主 harness 循环 → 本函数自行 bootstrap（含 est_context_tokens 兜底）。
    """
    out: dict = {"scaffolds": [], "orphans_attached": 0, "orphans_left": []}
    if plan is None or not getattr(plan, "subtasks", None):
        return out
    try:
        from swarm.brain.contract_utils import inject_build_scaffold_subtasks
        # R58-1：file_plan 是【模块 → 文件】的权威归属（逻辑模块名 ≠ 物理目录时唯一的证据源）
        injected = inject_build_scaffold_subtasks(plan, project_path, file_plan)
        out["scaffolds"] = [e["module"] for e in injected]
        if injected:
            from swarm.brain.nodes.shared import bootstrap_subtask_harness
            _ids = {e["subtask_id"] for e in injected}
            for st in plan.subtasks:
                if st.id in _ids:
                    bootstrap_subtask_harness(st, task_description or st.description)
                    if not getattr(st, "est_context_tokens", 0):
                        st.est_context_tokens = 8000 + 6000  # TRIVIAL 基线+1 文件
    except Exception:  # noqa: BLE001 — fail-open，VALIDATE 兜底
        logger.warning("[PLAN-FINISH] 脚手架注入失败（fail-open）", exc_info=True)
    try:
        from swarm.brain.nodes.shared import _task_requests_tests
        from swarm.brain.plan_validator import normalized_file_plan_paths
        from swarm.brain.symbol_surgery import attach_orphan_file_plan_entries
        # 单子任务计划：validate_file_plan_ownership 同口径跳过（SIMPLE 面自证），
        # 收尾器不越权挂靠防 scope 膨胀。复核 F2：测试路径分母对称剔除——收尾器在
        # _strip_unrequested_tests 之后运行，挂测试文件=复活刚被剥掉的路径。
        paths = (normalized_file_plan_paths(
                     file_plan,
                     exclude_test_paths=not _task_requests_tests(task_description))
                 if len(plan.subtasks) > 1 else [])
        if paths:
            attached, left = attach_orphan_file_plan_entries(plan, paths)
            out["orphans_attached"] = attached
            out["orphans_left"] = left
            # ③ R48-1（round48 死因）：挂靠"无候选"（没有任何子任务碰该模块）时，
            # 旧行为留给 VALIDATE 打回——但 LLM 三轮都不按 issues 修（round48 实测
            # 单个 ruoyi-common 孤儿文件三连原样打回 → CONFIRM 拒绝杀整个计划）。
            # VALIDATE 提示语自己就写着治法"或为其新建子任务"——这一步是机械可做的，
            # 收尾器确定性闭环：按模块分组新建子任务承接（幂等、零 LLM）。
            if left:
                created = _synthesize_orphan_subtasks(
                    plan, left, file_plan, project_path, task_description)
                if created:
                    out["orphan_subtasks"] = created
                    _cset = {p for ids in created.values() for p in ids}
                    out["orphans_left"] = [p for p in left if p not in _cset]
                    from swarm.brain.nodes.shared import bootstrap_subtask_harness
                    for st in plan.subtasks:
                        if st.id in created:
                            bootstrap_subtask_harness(
                                st, task_description or st.description)
                            if not getattr(st, "est_context_tokens", 0):
                                # 复核 F3：MEDIUM 基线 50000 与主启发式同源（8000 是 TRIVIAL 档）
                                st.est_context_tokens = (
                                    50000 + 6000 * max(1, len(created[st.id])))
    except Exception:  # noqa: BLE001
        logger.warning("[PLAN-FINISH] 孤儿文件挂靠失败（fail-open）", exc_info=True)
    try:
        # ④ R48b-1：契约符号安置（P1 命中会短路 R39-5 符号外科——收尾器全路径必经）
        if shared_contract and len(plan.subtasks) > 1:
            dom = _domicile_contract_symbols(
                plan, shared_contract, project_path, task_description)
            if dom:
                out["symbols_domiciled"] = dom
                from swarm.brain.nodes.shared import bootstrap_subtask_harness
                for st in plan.subtasks:
                    if st.id in dom:
                        bootstrap_subtask_harness(
                            st, task_description or st.description)
                        if not getattr(st, "est_context_tokens", 0):
                            st.est_context_tokens = (
                                50000 + 6000 * max(1, len(dom[st.id])))
    except Exception:  # noqa: BLE001
        logger.warning("[PLAN-FINISH] 契约符号安置失败（fail-open）", exc_info=True)
    if (out["scaffolds"] or out["orphans_attached"] or out["orphans_left"]
            or out.get("orphan_subtasks") or out.get("symbols_domiciled")):
        logger.info(
            "[PLAN-FINISH] 确定性收尾：脚手架注入 %d 个模块%s；file_plan 孤儿挂靠 %d 个%s%s",
            len(out["scaffolds"]),
            f" {out['scaffolds']}" if out["scaffolds"] else "",
            out["orphans_attached"],
            f"；无候选新建承接子任务 {len(out['orphan_subtasks'])} 个"
            if out.get("orphan_subtasks") else "",
            f"（仍无候选 {len(out['orphans_left'])} 个: {out['orphans_left'][:5]}，"
            "留 VALIDATE 权威打回）" if out["orphans_left"] else "")
    return out
