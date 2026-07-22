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
import re as _re

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

    def _emit_fileplan_subtask(sid: str, mod: str, chunk: list[str]) -> None:
        """确定性新建一个 file_plan 承接子任务（调用方保证单片 ≤_MAX_FILES_PER_GROUP，防超 validate 上限）。"""
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

    for mod, paths in sorted(groups.items()):
        base_sid = f"st-fileplan-{mod}"
        # 复核 F1：既有承接子任务 → 收养（追加 scope+描述+验收），绝不丢弃
        if base_sid in by_id and by_id[base_sid].id.startswith("st-fileplan-"):
            host = by_id[base_sid]
            adopt = [p for p in paths
                     if p not in host.scope.create_files
                     and p not in host.scope.writable]
            if adopt:
                # DR-01-F7(#52) 治本：收养也受容量约束。新建分支已按 _MAX_FILES_PER_GROUP 预分片防超
                # validate 硬上限，收养分支旧代码却无条件 append 全部 adopt → host writable 可超硬上限
                # → validate 硬失败 → 每轮确定性收养到同一超限 host → D09 盲重试死循环（新建分支的
                # 预分片守卫这里漏了）。只收养 host 剩余容量内的，溢出的走新建分片路径（base_sid-N）。
                from swarm.brain.plan_validator import MAX_WRITABLE_FILES_PER_SUBTASK
                _used = len(host.scope.writable or []) + len(host.scope.create_files or [])
                _room = max(0, MAX_WRITABLE_FILES_PER_SUBTASK - _used)
                _take, _overflow = adopt[:_room], adopt[_room:]
                for p in _take:
                    exists = bool(project_path) and (Path(project_path) / p).is_file()
                    (host.scope.writable if exists
                     else host.scope.create_files).append(p)
                    host.acceptance_criteria.append(
                        f"{p} 按 file_plan 用途实现并编译通过")
                if _take:
                    host.description += "\n【file_plan 承接·追加】\n" + _fmt(_take)
                    created[base_sid] = _take
                if _overflow:
                    _chunks = [_overflow[i:i + _MAX_FILES_PER_GROUP]
                               for i in range(0, len(_overflow), _MAX_FILES_PER_GROUP)]
                    _suffix = 2
                    for _chunk in _chunks:
                        while f"{base_sid}-{_suffix}" in by_id:
                            _suffix += 1
                        _emit_fileplan_subtask(f"{base_sid}-{_suffix}", mod, _chunk)
                        _suffix += 1
            continue
        # 复核 F2：预分片防超限
        chunks = [paths[i:i + _MAX_FILES_PER_GROUP]
                  for i in range(0, len(paths), _MAX_FILES_PER_GROUP)]
        for ci, chunk in enumerate(chunks):
            sid = base_sid if ci == 0 else f"{base_sid}-{ci + 1}"
            if sid in by_id:
                continue
            _emit_fileplan_subtask(sid, mod, chunk)
    if created:
        logger.info(
            "[PLAN-FINISH] R48-1 孤儿无候选 → 确定性新建/收养承接子任务 %d 个: %s",
            len(created), {k: v[:3] for k, v in created.items()})
    return created


def _domicile_contract_symbols(plan, shared_contract, project_path: str | None,
                               task_description: str,
                               file_plan: list | None = None) -> dict[str, list[str]]:
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
    import json as _json
    import re as _re
    _HARD = {"interfaces", "types", "apis", "symbols"}
    # T6②（round63 幻影 DTO）：dtos 是软符号（C1 只警不闸），但**被接口签名/apis 引用**的
    # 无主 dto=契约自引用的幻影类型（AlarmTaskDTO：契约声明+签名引用+plan 零文件零语料 →
    # worker 实现接口时只能臆造包名，8× "package …core.domain.dto does not exist"）。
    # 与硬符号同等安置成真产出文件（T4 pin 随后钉 defined_in，消费者拿精确 import）；
    # 孤立无引用的 dto 不安置（宁缺勿滥，交 C1 warn）。
    _ref_blob = " ".join(
        str(i.get("signature") or "")
        for i in (shared_contract.get("interfaces") or []) if isinstance(i, dict)
    ) + " " + _json.dumps(shared_contract.get("apis") or [], ensure_ascii=False)
    _referenced_dtos = {
        e["symbol"] for e in entries
        if e.get("kind") == "dtos" and e["symbol"] and _re.search(
            r"(?<![0-9A-Za-z_])" + _re.escape(e["symbol"]) + r"(?![0-9A-Za-z_])", _ref_blob)}
    sym_set = {e["symbol"] for e in entries}
    # 复核 F1：符号名标识符白名单——dict 条目 name 是未净化 LLM 字符串，脏名
    # （"GET /x/Export"、"IFoo<T>"、"../X"）直通会拼出垃圾/穿越路径；不合格如实留 VALIDATE
    _ident = _re.compile(r"^[A-Za-z_]\w*$")
    hard = [e for e in entries
            if (e.get("kind") in _HARD or e["symbol"] in _referenced_dtos)
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
    # 非源码/清单/纯标记样式扩展名：绝不作 code 符号的扩展名/源目录证据（markup/style ≠
    # code，栈中立）。★Task2 病根★：旧实现 mod_dirs 对**每个**文件无条件计数，MyBatis
    # `.xml`（src/main/resources/mapper）把 tpl_dir 拽进 resources/mapper → ext=java 造出
    # `.../resources/mapper/…/NotifyFacade.java`（classpath 不可见、不编译）。治=扩展名/
    # 源目录证据都只认 code 文件（同一集合，Task1/Task2 同源）。
    _NON_CODE_EXT = {"xml", "yml", "yaml", "properties", "sql", "md",
                     "html", "htm", "css", "scss", "sass", "less"}
    from collections import Counter
    exts: Counter = Counter()
    mod_dirs: dict[str, Counter] = {}
    for st in plan.subtasks:
        sc = getattr(st, "scope", None)
        for f in (list(getattr(sc, "create_files", None) or [])
                  + list(getattr(sc, "writable", None) or [])):
            p = str(f).replace("\\", "/").lstrip("/")
            base = p.rsplit("/", 1)[-1]
            # 只认 code 文件作扩展名/源目录证据（resource/markup/style 都不是源码落点）
            if "." not in base or base.startswith("pom."):
                continue
            if base.rsplit(".", 1)[-1].lower() in _NON_CODE_EXT:
                continue
            exts[base.rsplit(".", 1)[-1].lower()] += 1
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

    # ★Task1（round62 治本）★ 落点解析必须走【权威 file_plan】，不拿逻辑模块名拼猜。
    # file_plan 是设计产出的【模块→文件】权威归属，也是唯一与 CubeSandbox 挂载一致的
    # **项目相对**坐标源（host 磁盘探测会与 sandbox 分叉，故这里只认 file_plan / 计划
    # scope，二者皆项目相对，绝不产出 host 绝对路径）。逻辑模块名 ≠ 物理目录（契约
    # `alarm-sdk` 实住 `ruoyi-alarm/alarm-interface/`）：旧 `_dir_for` 拿名字拼出幻影
    # `alarm-sdk/…`、且把 .java 落进 resources/mapper。
    # 落点+扩展名 = 该模块【自身】源文件众数决定（★per-module，非 plan 全局★）：先取该模块
    # 自己的主源扩展名（众数，排配置/清单/纯标记样式），再取该扩展名【非测试】目录的众数
    # ——一个磁盘/设计里真实存在、含真源码、任意技术栈都可编译的目录。众数绝不像"公共前缀"
    # 塌成 `src/` 浅目录，也无需"像不像源目录"白名单；★per-module 扩展名让 Java 主计划里的
    # TS 模块也落到 .ts 真目录而非幻影★（对抗复核 HIGH：plan 全局 ext 会饿死异栈模块）。
    # 测试目录不放主代码符号（栈中立按 test/tests 段剔除，全测试则 fail-open 不剔）。
    # ★不丢符号★：无 file_plan/physical 证据的模块退回旧启发式（老流程零回归），绝不
    # "留 VALIDATE"——实测 C1 无主符号占比<0.4 仅告警不拦（silent-hunter F2），丢弃=符号
    # 既不落地又不被拦。跨物理模块的功能分组（module≠单一 build 单元）落主模块并告警，
    # 结构性归一/硬打回由 G1 validate_module_coherence 负责。（_NON_CODE_EXT 同 Task2 源）

    def _mode(items: list[str]) -> str:
        """众数；平票按字典序取最小 → 确定性（items 非空）。"""
        return sorted(Counter(items).items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

    def _resolve_place(paths: list[str]) -> tuple[str | None, str | None]:
        """一组文件路径 → (落点目录, 符号扩展名)：定该模块【自身】主源扩展名（众数，排
        非源码/清单/标记），再取该扩展名【非测试】目录众数。无源码证据→(None, None)。"""
        src: list[tuple[str, str]] = []
        for p in paths:
            p = str(p).replace("\\", "/").lstrip("/")
            b = p.rsplit("/", 1)[-1]
            if "/" not in p or "." not in b or b.startswith("pom."):
                continue
            e = b.rsplit(".", 1)[-1].lower()
            if e in _NON_CODE_EXT:
                continue
            src.append((p.rsplit("/", 1)[0], e))
        if not src:
            return None, None
        mode_ext = _mode([e for _, e in src])
        dirs = [d for d, e in src if e == mode_ext
                and not any(seg in ("test", "tests") for seg in d.split("/"))]
        dirs = dirs or [d for d, e in src if e == mode_ext]
        return _mode(dirs), mode_ext

    # ★权威落点预解析（file_plan 可用时）★ gate 与 _dir_for/_ext_for 共用同一张
    # `_resolved_dir`——绝不让"判定可安置"与"实际落点"分叉（round62 d1 回归教训同源）。
    _resolved_dir: dict[str, str] = {}
    _resolved_ext: dict[str, str] = {}
    _fp_src: dict[str, list[str]] = {}
    phys: dict[str, str] = {}
    if file_plan:
        from swarm.brain.contract_utils import (
            _file_plan_module_paths,
            _module_physical_dirs,
        )
        _fp_paths = _file_plan_module_paths(file_plan)
        _fp_src = {m: ps for m, ps in _fp_paths.items() if ps}   # F5 门：该模块有 file_plan 落点
        phys = _module_physical_dirs(plan, project_path, file_plan)
        for _m in {e["module"] for e in todo}:
            # ① 权威：file_plan 该模块自身源文件众数（真目录 + per-module 扩展名）
            d, e2 = _resolve_place(_fp_paths.get(_m, []))
            if d:
                _resolved_dir[_m], _resolved_ext[_m] = d, e2
                # 观测：源文件跨【多个物理模块根】= 功能分组（module≠单一 build 单元）。
                # 落主模块目录，结构性归一/硬打回由 validate_module_coherence（G1，cc7be64，
                # 已接线 validate_plan）判定——本函数只负责安置不丢符号。T5 核实：旧措辞
                # "Task4 待接管"是闸落地前的前瞻语，round63 复盘曾被它误导成"闸未实现"。
                _roots = {"/".join(x.split("/")[:2]) for x in _fp_paths[_m] if "/" in x}
                if len(_roots) > 1:
                    logger.warning(
                        "[PLAN-FINISH] Task1 契约模块 %s 的 file_plan 源文件跨多个物理模块 "
                        "%s → 落到主模块目录 %s（module≠单一 build 单元，一对多/多对一硬判"
                        "由 G1 validate_module_coherence 负责）", _m, sorted(_roots), d)
                continue
            # ② 次权威：_module_physical_dirs 物理根（含 flat 裸根，真 plan 证据）下计划
            #    源文件众数；仍无源证据 → 用物理根本身（真实证据目录胜过名字臆造幻影）。
            root = phys.get(_m)
            if root:
                _under = []
                for st in plan.subtasks:
                    sc = getattr(st, "scope", None)
                    for f in (list(getattr(sc, "create_files", None) or [])
                              + list(getattr(sc, "writable", None) or [])):
                        pp = str(f).replace("\\", "/").lstrip("/")
                        if pp == root or pp.startswith(root + "/"):
                            _under.append(pp)
                d2, e2 = _resolve_place(_under)
                _resolved_dir[_m] = d2 or root
                if e2:
                    _resolved_ext[_m] = e2
            # ③ 无 file_plan/physical 证据 → 不预解析，_dir_for 走旧启发式（老流程零回归）

    def _ext_for(mod: str) -> str:
        return _resolved_ext.get(mod, ext)

    _guessed_mods: set[str] = set()   # G4：零证据兜底告警去重（_dir_for 每模块可被调多次）

    def _dir_for(mod: str) -> str:
        # ★file_plan 可用且有权威证据 → 走众数预解析（真目录、栈中立）★
        if mod in _resolved_dir:
            return _resolved_dir[mod]
        # 回退第一档：模块名【本身就是】计划里真出现过的顶层目录（mod_dirs 命中=真证据，
        # 非名字臆造）→ 用之。注：单一权威 _resolve_module_dirs（经 phys/_resolved_dir）
        # 已先吃过 plan scope + 基线树证据；走到这里说明那层要么歧义（G1 闸会硬打回、此
        # 落点无所谓）、要么该模块压根没被它覆盖。
        if mod in mod_dirs:
            return f"{mod}/{mod_dirs[mod].most_common(1)[0][0]}"
        # ★G4（Task#9 审计 TIER3）★ 走到这里=file_plan/scaffold/基线【全部证据穷尽】、
        # mod 也不是任何真实顶层目录 → 该契约模块【零物理证据】。审计原判"杀掉 fallback"
        # 经复核为误：此处并非 R44/R57 病根（那病根=模块【已存在于他处】却被名字臆造成幻影
        # 重复，已由上面的权威解析吃掉），而是【真·新模块 or 计划欠指定】的末端兜底。
        # 保留 `{mod}/` 形状（脚手架注入可将其注册进 reactor 成真新模块）——剥掉前缀会把
        # 符号落到工程根 src/（多模块 reactor 里根本不是模块，编不到），是【回归】不是治本。
        # 真正缺的是【可观测】：把静默臆造升级为一次去重 LOUD 告警，令 G1 coherence 闸/
        # coverage 面能看见"这个模块零证据、按新模块名安置"这一存疑事实（交闸=surface，非删）。
        seg = mod.replace("_", "-").split("-")[-1]
        if mod not in _guessed_mods:
            _guessed_mods.add(mod)
            logger.warning(
                "[PLAN-FINISH] G4 契约模块 %r 零物理证据（file_plan/scaffold/基线全无）→ "
                "按新模块名兜底安置到 %s/…（存疑：若非真·新模块，计划欠指定其物理落点）；"
                "交 G1 coherence 闸/coverage 面暴露，绝不静默丢符号", mod, mod)
        if "" in mod_dirs and mod_dirs[""]:
            # 单模块布局：模板已是完整目录（含 src 段），前缀模块 + 尾段包名
            return f"{mod}/{mod_dirs[''].most_common(1)[0][0]}/{seg}"
        return f"{mod}/{tpl_dir}/{seg}"

    groups: dict[str, list[str]] = {}
    for e in todo:
        groups.setdefault(e["module"], []).append(e["symbol"])
    by_id = {st.id: st for st in plan.subtasks}
    created: dict[str, list[str]] = {}
    _MAX = 6
    # ★G9 收口（对抗双复核 HIGH：两处 pom 伪造入口必须同源）★ 本函数下方给【零证据新模块】补
    # pom 脚手架是第二条 pom 伪造路径，必须与 inject_build_scaffold_subtasks 走【同一】栈闸，
    # 否则异栈工程仍会经此路径被塞 pom。已知非 Maven 栈 → 不补 pom（其余安置逻辑照常，绝不丢符号）。
    from swarm.brain.contract_utils import _should_fabricate_maven_scaffold
    _maven_scaffold_ok, _ = _should_fabricate_maven_scaffold(plan, project_path, file_plan)
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
                d, _e = _dir_for(mod), _ext_for(mod)
                for s in adopt:
                    host.scope.create_files.append(f"{d}/{s}.{_e}")
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
            d, _e = _dir_for(mod), _ext_for(mod)
            files = [f"{d}/{s}.{_e}" for s in chunk]
            st = SubTask(
                id=sid,
                description=(
                    f"【契约符号安置】契约模块 {mod} 的以下符号无子任务承接"
                    "（收尾器确定性新建本子任务）。按共享契约定义完整实现每个符号"
                    "（接口/类型按契约签名，落在对应文件）：\n"
                    + "\n".join(f"- {s} → {d}/{s}.{_e}" for s in chunk)),
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
            if (scaffold_sid not in by_id and project_path and _maven_scaffold_ok
                    and mod not in phys and mod not in _fp_src
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


def derive_consumer_depends_edges(plan) -> dict[str, list[str]]:
    """R65D-W2①（round65d 头排堵塞）：readable→创建者的结构性消费关系确定性下推为
    depends_on 边（零 LLM、幂等）。

    round65d 实锤（live 事发态）：13 根任务大半是 admin 隐性消费者（readable 引用
    alarm 新文件却零 depends_on 边）→ 首两批 7/8 派发全 BLOCKED 白跑整条 locate/code；
    C9 动态补边只能执行期代偿。规划期就把边建好：消费者被 dispatch 依赖闸自然扣住、
    生产者 dep_counts>0 自然升 tier-1、B1/规则2 的上游产物注入面被激活。
    （fixture plan_b583.json 为终版 checkpoint 态：既有边已较多，本步实测仍 +176 条/
    70 消费者——G2 在 elaborate 期看不到的增量；根任务数在该 fixture 上前后均为 2。）

    算法单一事实源=contract_utils.wire_readable_provenance（G2，elaborate 期同 pass）
    ——复核 MED 收敛：两处独立实现必然漂移，本步只是把 G2 在收尾器【末端】（scaffold/
    孤儿/domicile/readable 归一全就位后）再跑一遍，接住 G2 在 elaborate 期看不到的
    readable 增量（round65d fixture 实测 +176 条）。护栏随 G2：唯一创建者才成边、
    歧义/基线不猜、成环记 unresolved 绝不制造环、传递可达即幂等跳过。
    返回 {消费者 id: [新增上游 id…]} 机读账（unresolved 环候选 WARNING 留痕）。
    """
    subs = getattr(plan, "subtasks", None) or []
    if len(subs) < 2:
        return {}
    from swarm.brain.contract_utils import wire_readable_provenance
    added_edges, unresolved = wire_readable_provenance(plan)
    added: dict[str, list[str]] = {}
    for consumer, producer in added_edges:
        added.setdefault(consumer, []).append(producer)
    if unresolved:
        logger.warning(
            "[PLAN-FINISH] R65D-W2 %d 条消费边会成环（创建者传递依赖消费者）→ 跳过；"
            "边方向属更深计划错，留 VALIDATE/C9 面: %s",
            len(unresolved), unresolved[:6])
    if added:
        logger.info(
            "[PLAN-FINISH] R65D-W2 消费边下推 %d 个消费者共 %d 条（readable→创建者，"
            "头排 BLOCKED 白跑在规划期消解；算法同源 G2）: %s",
            len(added), sum(len(v) for v in added.values()),
            {k: v for k, v in sorted(added.items())[:6]})
    return added


def _plan_reaches(by_id: dict, start: str, target: str) -> bool:
    """start 是否经 depends_on 传递到达 target（R67-T4 加边前防环/幂等共用）。"""
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


_DESC_ST_DEP_RE = _re.compile(r"依赖\s*(st-[A-Za-z0-9_-]+)")


def wire_described_dependency_tokens(plan) -> dict[str, list[str]]:
    """R67-T4a（round67 R67-4）：描述里"依赖 st-X"词元 ↔ depends_on 对账，缺边确定性补上。

    round67 实锤：st-48 描述明写"控制器注入…（依赖 st-1 的 pom 装配）"，但 depends_on=[]、
    readable 全基线——消费意图只活在自然语言里，W2/G2 以 readable 为唯一信号源对它结构性
    失明 → 首批并行派发即 BLOCKED 白跑一轮。规划 LLM 既然点名了 st-X，这是零歧义的结构
    信号：存在性校验 + 防环 + 传递可达幂等跳过后直接成边（零 LLM）。
    返回 {消费者 id: [新增上游 id…]} 机读账。
    """
    subs = list(getattr(plan, "subtasks", None) or [])
    if len(subs) < 2:
        return {}
    by_id = {str(getattr(st, "id", "")): st for st in subs}
    added: dict[str, list[str]] = {}
    for st in subs:
        sid = str(getattr(st, "id", ""))
        for dep in set(_DESC_ST_DEP_RE.findall(str(getattr(st, "description", "") or ""))):
            if dep == sid or dep not in by_id:
                continue                    # 自引用/幻影 id 不成边
            if _plan_reaches(by_id, sid, dep):
                continue                    # 已传递可达 = 幂等跳过
            if _plan_reaches(by_id, dep, sid):
                logger.warning(
                    "[PLAN-FINISH] R67-T4a 描述点名依赖 %s→%s 会成环 → 跳过（边方向属更深"
                    "计划错，留 VALIDATE/C9 面）", sid, dep)
                continue
            st.depends_on = list(getattr(st, "depends_on", None) or []) + [dep]
            added.setdefault(sid, []).append(dep)
    if added:
        logger.info(
            "[PLAN-FINISH] R67-T4a 描述词元补边 %d 个消费者共 %d 条（\"依赖 st-X\"只活在"
            "自然语言=W2 结构盲区，round67 st-48 首批 BLOCKED 真根）: %s",
            len(added), sum(len(v) for v in added.values()), dict(sorted(added.items())))
    return added


# 符号词元门槛：≥2 个大写字母的驼峰标识（IAlarmRecordService/AlarmRecord），排除普通
# 英文单词/单驼峰词（Controller/Thymeleaf 仅 1 个大写）误配。
_SYMBOL_TOKEN_RE = _re.compile(r"\b([A-Z][a-z0-9]*(?:[A-Z][a-z0-9]*)+)\b")


def wire_symbol_consumption_edges(plan) -> dict[str, list[str]]:
    """R67-T4b（round67 R67-5）：desc/AC 引用的【本计划他人 create 的类符号】→ 缺边扫描补齐。

    round67 实锤：st-50-1 要注入 ISysGoogleAuthService（st-8-1 同批并行创建），零边、
    readable 全基线、context 零相关符号 → worker 只能臆造签名或重复实现（编译可过=假过）。
    判据（护栏随 G2，宁缺毋滥）：符号 token（≥2 大写驼峰）逐字命中【唯一】创建者的类路径
    源码 basename 词干才成边；多创建者歧义不猜、自引用不成边、防环、传递可达幂等跳过。
    成边同时把产物路径补进消费者 readable（激活 B1/规则2 上游产物注入面，与 W2 同理）。
    返回 {消费者 id: [新增上游 id…]} 机读账。
    """
    from swarm.brain.contract_utils import classpath_fqn_key
    subs = list(getattr(plan, "subtasks", None) or [])
    if len(subs) < 2:
        return {}
    by_id = {str(getattr(st, "id", "")): st for st in subs}
    # 符号词干 → (唯一创建者 sid, 产物路径)；多创建者 → None（歧义哨兵，绝不猜）
    stem_owner: dict[str, tuple[str, str] | None] = {}
    for st in subs:
        sid = str(getattr(st, "id", ""))
        sc = getattr(st, "scope", None)
        for f in list(getattr(sc, "create_files", None) or []):
            if not classpath_fqn_key(f):
                continue                    # 仅类路径源码构成符号（资源/非 JVM 不判）
            stem = str(f).replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if not _SYMBOL_TOKEN_RE.fullmatch(stem):
                continue                    # 单驼峰/非符号形态词干不参与（防误配）
            prev = stem_owner.get(stem)
            if prev is None and stem in stem_owner:
                continue                    # 已判歧义
            if prev is not None and prev[0] != sid:
                stem_owner[stem] = None     # 多创建者 → 歧义哨兵
            else:
                stem_owner[stem] = (sid, str(f))
    added: dict[str, list[str]] = {}
    cycle_skipped: list[tuple[str, str, str]] = []
    for st in subs:
        sid = str(getattr(st, "id", ""))
        text = (str(getattr(st, "description", "") or "") + "\n"
                + "\n".join(str(a) for a in (getattr(st, "acceptance_criteria", None) or [])))
        for tok in set(_SYMBOL_TOKEN_RE.findall(text)):
            owner = stem_owner.get(tok)
            if not owner or owner[0] == sid:
                continue                    # 无创建者/歧义/自引用
            producer, path = owner
            sc = getattr(st, "scope", None)
            if sc is not None and path in (list(getattr(sc, "create_files", None) or [])
                                           + list(getattr(sc, "writable", None) or [])):
                continue                    # 自己也写该文件（共写面由 T3 串行化管）
            if _plan_reaches(by_id, sid, producer):
                pass                        # 已可达仍可补 readable（幂等，不重复加边）
            elif _plan_reaches(by_id, producer, sid):
                cycle_skipped.append((sid, producer, tok))
                continue                    # 成环跳过（多为拆分簇兄弟互引且反向边已保序）
            else:
                st.depends_on = list(getattr(st, "depends_on", None) or []) + [producer]
                added.setdefault(sid, []).append(producer)
            if sc is not None:
                rd = list(getattr(sc, "readable", None) or [])
                if path not in rd:
                    sc.readable = rd + [path]
    if cycle_skipped:
        logger.warning(
            "[PLAN-FINISH] R67-T4b %d 条符号消费边会成环 → 跳过（多为拆分簇兄弟互引且"
            "反向边已保序；边方向属更深计划错留 VALIDATE/C9 面）: %s",
            len(cycle_skipped), cycle_skipped[:6])
    if added:
        logger.info(
            "[PLAN-FINISH] R67-T4b 符号消费补边 %d 个消费者共 %d 条（desc/AC 引用他人 create"
            " 类符号而 readable 未列=G2 盲区，round67 st-50-1 2FA 双实现断裂真根）: %s",
            len(added), sum(len(v) for v in added.values()), dict(sorted(added.items())))
    return added


def reconcile_upstream_account(plan) -> dict[str, list[str]]:
    """R65REPLAY-T4（round65d 回放轮死因本体）：上游账↔依赖序对账。

    实锤链：R63-T4 符号布线按语料文本引用写 readable+upstream_artifacts 不查方向
    → st-11-1(XML) 的账里被布进 4 个 Mapper 接口，其计划内创建者 st-11-2..5 反过来
    （传递）依赖 st-11-1 → seed 闸 fail-closed 只看账、不知生产者在自己下游=永久
    BLOCKED"等生产者"→ 预算闸拆分 deep-copy 继承账 1 变 4 → 连坐 72 → PARTIAL。
    W2/T5 检测到环只跳边从不清账——DAG 无环但【账与序矛盾】原样入执行期。

    对每个子任务的 upstream_artifacts 条目：若其计划内生产者【全部】是本任务自身或
    （传递）依赖本任务=生产者在自己下游，结构矛盾 → 从 ua（及 readable 同路径）确定性
    剔除 + WARNING + 机读账 + 把剔除路径以文本提示注入 context_snippets（复核 F3：
    信息通道保留——worker 仍知道权威路径可推导符号名，只掐死 seed 闸死等通道）。
    账让位于序：语义引用不是构建输入，真需要时 L1 编译闸/C9 动态边兜底。
    生产者口径=create_files∪writable（复核 F1：剔除判定必须比 G2 加边的 create-only
    更宽——writable 声明的上游修改者也是合法生产者，看不见它=冤剔真上游账）；
    路径经 _norm_scope_path 归一比对（复核 F2：R41 实证 './'/反斜杠口径漂移是真实病）。
    生产者歧义（上下游混合）不猜；无计划内生产者（基线/存量）不动——存在性交
    seed 闸/R49-2 运行期判。★写者无关：每次调用都从当前 create_files/writable/
    depends_on 全量结构重算，不维护写者清单——任何时点新增的 ua/scope 写者只要发生在
    下一次调用前即自动被覆盖（复核 F5，维护者勿退化成按写者点名的补丁）★。
    纯 DAG/路径逻辑，栈中立；幂等。返回 {子任务 id: [剔除路径…]} 机读账（空=零矛盾）。
    """
    subs = getattr(plan, "subtasks", None) or []
    if not subs:
        return {}
    from swarm.brain.contract_utils import _norm_scope_path  # 路径口径单一事实源(R41)
    by_id = {st.id: st for st in subs}
    owners: dict[str, set[str]] = {}
    for st in subs:
        sc = getattr(st, "scope", None)
        if sc is None:
            continue
        for p in (list(getattr(sc, "create_files", None) or [])
                  + list(getattr(sc, "writable", None) or [])):
            owners.setdefault(_norm_scope_path(p), set()).add(st.id)
    if not owners:
        return {}
    _closure_cache: dict[str, set[str]] = {}

    def _closure(sid: str) -> set[str]:
        """sid 的传递 depends_on 闭包（迭代 DFS，环安全）。"""
        cached = _closure_cache.get(sid)
        if cached is not None:
            return cached
        seen: set[str] = set()
        stack = [sid]
        while stack:
            cur = by_id.get(stack.pop())
            for dep in (getattr(cur, "depends_on", None) or []) if cur else []:
                if dep not in seen:
                    seen.add(dep)
                    stack.append(dep)
        _closure_cache[sid] = seen
        return seen

    removed: dict[str, list[str]] = {}
    for st in subs:
        sc = getattr(st, "scope", None)
        if sc is None:
            continue
        ua = list(getattr(sc, "upstream_artifacts", None) or [])
        if not ua:
            continue
        bad = [p for p in ua
               if (own := owners.get(_norm_scope_path(p)))
               and all(o == st.id or st.id in _closure(o) for o in own)]
        if not bad:
            continue
        _bad_norm = {_norm_scope_path(p) for p in bad}
        sc.upstream_artifacts = [p for p in ua if _norm_scope_path(p) not in _bad_norm]
        rd = list(getattr(sc, "readable", None) or [])
        if any(_norm_scope_path(p) in _bad_norm for p in rd):
            sc.readable = [p for p in rd if _norm_scope_path(p) not in _bad_norm]
        # 复核 F3：信息通道保留——剔的是 seed 闸死等语义，不是路径知识。worker（尤其
        # 引用后续任务符号的资源/模板类产出）仍需权威路径推导符号名，否则从"死等"退化成
        # "盲猜命名"（更糟：无 BLOCKED 信号无人知道）。文本提示零 seed 语义。
        _hint = ("\n\n[计划序提示] 以下文件由计划内后续任务创建，本批次不可读取；"
                 "如需引用其符号/命名，以下列路径为权威推导，勿自行发明：\n"
                 + "\n".join(f"- {p}" for p in bad))
        st.context_snippets = (getattr(st, "context_snippets", "") or "") + _hint
        removed[st.id] = bad
    if removed:
        logger.warning(
            "[PLAN-FINISH] R65REPLAY-T4 上游账对账：%d 个子任务共 %d 条 ua 条目的计划内"
            "创建者在自己（传递）下游=账与序矛盾（seed 闸会永久死等）→ 从账剔除"
            "（账让位于序；L1/C9 兜底）: %s",
            len(removed), sum(len(v) for v in removed.values()),
            {k: v[:3] for k, v in sorted(removed.items())[:6]})
    return removed


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
        # R62-Task3：R57-6 收权后确定性剪除空写 scope 死子任务（无人依赖者），
        # 否则一路漏到 dispatch → worker 空转 churn。
        from swarm.brain.contract_utils import prune_empty_scope_subtasks
        _pruned = prune_empty_scope_subtasks(plan)
        if _pruned:
            out["pruned_empty_scope"] = _pruned
    except Exception:  # noqa: BLE001 — fail-open
        logger.warning("[PLAN-FINISH] 空 scope 死子任务剪除失败（fail-open）", exc_info=True)
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
                plan, shared_contract, project_path, task_description, file_plan)
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
    try:
        # R62-Task5：readable 幻影包路径归一到 producer 真实落点（放【末端】——所有
        # producer 含 domicile 新建者都已就位，落点已定）→ provenance 一致，consumer 编得过。
        from swarm.brain.contract_utils import align_readable_to_producer
        _al = align_readable_to_producer(plan, project_path)
        if _al.get("aligned"):
            out["readable_aligned"] = _al["aligned"]
    except Exception:  # noqa: BLE001 — fail-open
        logger.warning("[PLAN-FINISH] readable 落点归一失败（fail-open）", exc_info=True)
    try:
        # R65D-W2①：消费边下推（放 readable 归一之后——落点已定，边才准）
        _ce = derive_consumer_depends_edges(plan)
        if _ce:
            out["consumer_edges"] = _ce
    except Exception:  # noqa: BLE001 — fail-open
        logger.warning("[PLAN-FINISH] 消费边下推失败（fail-open）", exc_info=True)
    # R67-T4a/T4b：自然语言消费关系补边（放 W2 之后——readable 推得出的边 W2 已建，
    # 此处只接 W2 结构盲区）。★hunter F3 整改★两 pass 各自 try（与本函数逐 pass 纪律一致）：
    # T4b 崩不吞 T4a 成果可见性，日志带已落边账区分"零边"vs"半应用"。
    try:
        _t4a = wire_described_dependency_tokens(plan)
        if _t4a:
            out["described_dep_edges"] = _t4a
    except Exception:  # noqa: BLE001 — fail-open
        logger.warning("[PLAN-FINISH] R67-T4a 词元补边失败（fail-open；已落边账=%s）",
                       out.get("described_dep_edges"), exc_info=True)
    try:
        _t4b = wire_symbol_consumption_edges(plan)
        if _t4b:
            out["symbol_consumption_edges"] = _t4b
    except Exception:  # noqa: BLE001 — fail-open
        logger.warning("[PLAN-FINISH] R67-T4b 符号补边失败（fail-open；已落边账=%s）",
                       out.get("symbol_consumption_edges"), exc_info=True)
    try:
        # R65REPLAY-T4：上游账对账放【消费边之后】——W2 能成的边先成（生产者转正为
        # 真上游，账合法保留）；只有成环跳过的矛盾账才被剔除。
        _ra = reconcile_upstream_account(plan)
        if _ra:
            out["upstream_account_reconciled"] = _ra
    except Exception:  # noqa: BLE001 — fail-open
        # 复核 F6：对账挂了=幽灵死等账可能残留（比本机制出现前更糟的是无人知道）——
        # 机读标记供调用方进 degraded_reasons；注入路径由 _FailOpenAlarm（watch 本
        # logger 的 exc_info WARNING）自动升闸。
        out["upstream_account_reconcile_failed"] = True
        logger.warning("[PLAN-FINISH] 上游账对账失败（fail-open，幽灵死等账可能残留）",
                       exc_info=True)
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
