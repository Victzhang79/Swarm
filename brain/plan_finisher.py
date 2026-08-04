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

from swarm.stacks import STACK_SPEC

logger = logging.getLogger(__name__)

# P-M3（27 号文）：JVM 类文件扩展名集——唯一事实源=STACK_SPEC 的
# shares_classpath_namespace=True 栈的 source_exts 并集（= {"java","kt","scala","groovy"}，
# 派生非手写）。消费点=_domicile_contract_symbols 的「小写首字母符号不安置」排除：
# 该排除本是 Maven/Java 时代的「方法名/字段名不是类」护栏，对 Python(get_user_report)/
# JS(fetchReport)/Go(非导出符号) 是【把契约正主符号挡在确定性安置门外】的误杀。
# 只在 JVM classpath 命名空间栈（文件名=公开类名，小写名绝不可能是类文件）才保留排除。
_JVM_CLASS_FILE_EXTS: frozenset[str] = frozenset(
    e.lstrip(".") for s in STACK_SPEC.values()
    if s.shares_classpath_namespace for e in s.source_exts)


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
    # P-M3（27 号文）：主源扩展名用于门控小写符号排除（见 _JVM_CLASS_FILE_EXTS）。
    # 空 exts 时 _dominant_ext="" → 不在 JVM 集 → 不排除小写；该形态随后在下方
    # `if not exts` 原样早返 {}，行为与治前逐字节一致（门控形同虚设，不造新分支）。
    # P-M3 R2（hunter F2）：most_common 平票按插入序（=LLM 输出序）→ 同一逻辑 plan
    # 的门控结论随文件顺序抖动。平票确定性=（-计数, 扩展名字典序）双键排序，与下方
    # `_mode` 同形状；方向登记：java/py 平票时 java 先=排除保留（保守方向）。
    _dominant_ext = (sorted(exts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
                     if exts else "")
    hard = [e for e in entries
            if (e.get("kind") in _HARD or e["symbol"] in _referenced_dtos)
            and e["symbol"] and _ident.fullmatch(e["symbol"])
            # P-M3：小写排除仅 JVM 类文件栈保留（文件名=公开类名，小写名=方法/字段
            # 必不是类）；非 JVM 栈（py/go/ts/rs…）小写符号是契约正主，确定性安置。
            # 边界登记：混合栈 plan（java 主导+零星 py 模块）按主导扩展名判，
            # py 模块的小写符号仍被排除（=治前行为，保守方向，不放宽）。
            and (_dominant_ext not in _JVM_CLASS_FILE_EXTS
                 or not e["symbol"][0].islower())
            and not ("." in e["symbol"] and e["symbol"].split(".", 1)[0] in sym_set)]
    if not hard:
        return {}
    unowned = set(unowned_contract_symbols(plan, [e["symbol"] for e in hard]))
    todo = [e for e in hard if e["symbol"] in unowned
            and e.get("module") and _ident.fullmatch(
                e["module"].replace("-", "_").replace("/", ""))]
    if not todo:
        return {}
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
    _fp_paths: dict[str, list[str]] = {}   # B5（R67M-T2）base 查表的模块候选根证据源
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
            # 复核 MEDIUM-4（"豁免即失明"补偿观测）：G4 落点本身可能是幽灵布局
            # （tpl_dir 退化默认 src 时 mod/src/seg 不过可编译布局）——豁免照建
            # （防死循环）但必须如实标注幽灵面，与证据面闸住的 punt 可区分。
            _g4_probe = f"{mod}/{mod_dirs[''].most_common(1)[0][0] if ('' in mod_dirs and mod_dirs['']) else tpl_dir}/{seg}/X.{_ext_for(mod)}"
            _g4_ghost = (_ext_for(mod).lower() in _JVM_CLASS_FILE_EXTS
                         and not _jvm_compilable_layout(_g4_probe))
            logger.warning(
                "[PLAN-FINISH] G4 契约模块 %r 零物理证据（file_plan/scaffold/基线全无）→ "
                "按新模块名兜底安置到 %s/…（存疑：若非真·新模块，计划欠指定其物理落点）；"
                "交 G1 coherence 闸/coverage 面暴露，绝不静默丢符号%s", mod, mod,
                "。★落点本身非可编译源码布局（mvn 不编译=幻影文件面，C1 basename owner "
                "判据对此失明）——布局闸刻意豁免 G4 防死循环，此 WARNING 是唯一观测面★"
                if _g4_ghost else "")
        if "" in mod_dirs and mod_dirs[""]:
            # 单模块布局：模板已是完整目录（含 src 段），前缀模块 + 尾段包名
            return f"{mod}/{mod_dirs[''].most_common(1)[0][0]}/{seg}"
        return f"{mod}/{tpl_dir}/{seg}"

    # ── R67M-T2 B5（23号文·round67m CVB 死因治本）：安置前 base 树查表 ──
    # round67m 实证：契约符号 ISysJobService 是 base 既有实体，LLM 把 defined_in 染成
    # 新包幻影路径（quartz/task/…）→ C1 判无主 → 本函数造 st-contract-quartz-task 影子
    # create → G1 ③f _created_class_shadows_base 硬打回（轮1/轮4 同形复发，烧穿重试）。
    # 治=安置前对 base 树做确定性查表，认出"该符号实为存量引用"并跳过影子安置：
    #   Case A：契约 defined_in【已指向盘上实存文件】且过证据卫生判据、stem==符号
    #     （LLM 显式声明=存量引用，round67g 治法A 同形态）→ 信任声明，直接跳过；
    #   Case B：defined_in 空/幻影 + purpose/description 显式复用语义 + base 树【唯一】
    #     stem 命中 + 命中落点在该模块候选物理根（file_plan 首段 ∪ phys 根）内
    #     → 把 defined_in 归位到 base 真身路径，跳过安置。
    # ★绝不用 base 结构相似度挑边（round67c 血泪）：唯一命中 + LLM 显式声明/显式复用
    # 语义，缺一即不动★。两案皆不成立 → 原样安置（影子照造、③f fail-closed 硬打回兜底）。
    # 下游安全：C1 R39-2 存量豁免（baseline_symbol_files 同 stem 命中即豁免）保证转换后
    # 不再判无主；defined_in=base 真身是 round67g 治法A 已接线的合法下游形态。
    # R67M2-T2 C1（24号文）：Case C/布局闸的 JVM 判定面（B5 块与下方安置布局闸共用
    # 单一事实源）——栈中立走 classpath_fqn_key；扩展名判据保证只触 JVM 代码文件，
    # 异栈（_ext_for 不产这些扩展名）零行为变化。
    from swarm.brain.contract_utils import classpath_fqn_key as _classpath_fqn_key
    from swarm.brain.contract_utils import jvm_compilable_layout as _jvm_compilable_layout
    # P-M3 R2（reviewer F1）：本函数三处 JVM 代码扩展名判定（G4 幽灵探测 / B5 Case C /
    # 安置布局闸）统一消费模块级 `_JVM_CLASS_FILE_EXTS`（STACK_SPEC 派生）——
    # 原本地手写 {"java","kt","scala","groovy"} 与派生集今日逐字节相等，
    # 但「同一概念两处手抄」正是 P-M3 要治的漂移族，新增 JVM 系栈即分叉。
    _base_refs: list[str] = []
    if todo and project_path and Path(project_path).is_dir():
        import os as _os
        # 复核 LOW-2：bin/obj/vendor 同族产物/依赖目录一并剪（与依赖/构建产物同义）。
        _PRUNE_DIRS = {".git", "node_modules", "target", "build", "dist", "out",
                       ".gradle", ".idea", ".vscode", "__pycache__", ".codegraph", ".venv",
                       "bin", "obj", "vendor"}
        # 复核 F6（reviewer LOW-1/hunter MEDIUM-3②）：否定语境先行排除——"不复用既有 X"
        # 含 复用/既有 但语义相反。★语言边界如实声明★：意图语料按中文散文设计，英文
        # "reuse existing" 不命中=覆盖缺口（方向 fail-closed 安全，扩表需独立证据批）。
        # ★R2 LOW-R2-1★：否定表为示例非穷尽（"不再复用旧接口"会被"再"字滑过）——
        # 词表扩表=打地鼠，硬底=复合前提（无主+唯一命中+候选根内）+③f fail-closed。
        _REUSE_INTENT_RE = _re.compile(r"既有|已有|已建|复用|现存|已存在|不新增|无需新增")
        _REUSE_NEGATE_RE = _re.compile(r"不复用|不再复用|不再使用|不用既有|并非不新增|不是复用")

        def _is_code_evidence(rel: str) -> bool:
            """base 证据文件卫生判据（Case A/B 单一事实源，复核 F1 reviewer HIGH-1）：
            代码扩展名（排 _NON_CODE_EXT/pom.*）+ 非 test/tests 段 + 非产物/依赖目录段
            + 不逃逸项目根。无此判据 Case A 会把 .xml/测试树/构建产物同名文件当存量真身
            → 跳过安置+C1 豁免放行=迟发编译失败（③f 秒级打回被换成执行期烧钱）。"""
            segs = rel.split("/")
            if any(s in ("", ".", "..") for s in segs):
                return False
            if any(s in _PRUNE_DIRS for s in segs[:-1]):
                return False
            if any(s in ("test", "tests") for s in segs[:-1]):
                return False
            base = segs[-1]
            if "." not in base or base.startswith("pom."):
                return False
            return base.rsplit(".", 1)[-1].lower() not in _NON_CODE_EXT

        def _contract_item_of(e: dict) -> dict | None:
            for it in (shared_contract.get(e["kind"]) or []):
                if (isinstance(it, dict)
                        and str(it.get("name") or it.get("id") or "") == e["symbol"]):
                    return it
            return None

        # 复核 F3（hunter MEDIUM-1/reviewer MEDIUM-2）：单次 walk 建 stem→paths 索引——
        # 逐符号全树 walk 是 O(N×树)（plan 节点内联同步执行，事件循环阻塞 R42 F4 同族）。
        _stem_idx: dict[str, list[str]] = {e["symbol"]: [] for e in todo}
        for root, dirs, files in _os.walk(project_path):
            dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS]
            for fn in files:
                stem, dot, _fx = fn.rpartition(".")
                if dot and stem in _stem_idx:
                    rel = _os.path.relpath(
                        _os.path.join(root, fn), project_path).replace(_os.sep, "/")
                    if _is_code_evidence(rel):
                        _stem_idx[stem].append(rel)

        _todo_keep: list[dict] = []
        _punts: dict[str, list[str]] = {}   # 复核 F5：punt 方向聚合观测（原因→符号）
        for e in todo:
            item = _contract_item_of(e)
            if item is None:
                _todo_keep.append(e)
                continue
            _di = str(item.get("defined_in") or "").strip().replace("\\", "/").lstrip("/")
            if (_di and _is_code_evidence(_di)
                    and _di.rsplit("/", 1)[-1].rsplit(".", 1)[0] == e["symbol"]
                    and (Path(project_path) / _di).is_file()):
                # Case A：defined_in 已显式声明在实存 base 代码文件——再安置=造影子。
                _base_refs.append(f"{e['symbol']}→{_di}(defined_in 实存)")
                continue
            hits = _stem_idx.get(e["symbol"]) or []
            if len(hits) != 1:
                _todo_keep.append(e)   # 零命中=真新符号；多命中=歧义，留 ③f 裁
                if hits:
                    _punts.setdefault("base 多命中歧义", []).append(e["symbol"])
                continue
            _hit = hits[0]
            _cand_roots = (
                {p.split("/", 1)[0] for p in _fp_paths.get(e["module"], []) if "/" in p}
                | ({phys[e["module"]].split("/", 1)[0]} if phys.get(e["module"]) else set())
                # R67M2-T2 C1：模块名本身=base 树【盘上实存】顶层目录时即候选根（磁盘实证
                # 非名字臆造——round67m2 SysJob：模块 ruoyi-quartz 在 file_plan 零条目、
                # phys 未映射，但 base 树确有 ruoyi-quartz/ 目录=该模块物理根最强证据）。
                # 复核 LOW-3：段净化——模块名含 / 或 . 段时 is_dir 可逃逸项目根，与
                # _is_code_evidence 同卫生（判据同质，绝不拿脏名做磁盘判定）。
                | ({e["module"]} if ("/" not in e["module"]
                                     and e["module"] not in ("", ".", "..")
                                     and (Path(project_path) / e["module"]).is_dir())
                   else set()))
            if (not _cand_roots) or _hit.split("/", 1)[0] not in _cand_roots:
                _todo_keep.append(e)   # 命中不在该模块候选物理根=跨模块同名，不算归属证据
                _punts.setdefault("命中不在模块候选根", []).append(e["symbol"])
                continue
            _prose = " ".join(str(item.get(k) or "")
                              for k in ("purpose", "description"))
            # R67M2-T2 C1/C3（24号文）Case C：幽灵布局 defined_in 对账——契约 defined_in
            # 是 JVM 代码扩展名却不在可编译源码布局内（jvm_compilable_layout=False=自证
            # 幻觉，如 round67m2 SysJob 的 ruoyi-quartz/SysJob.java 无 src 布局段），且
            # base 唯一 stem 命中落本模块候选根 → 不需要复用散文（幻觉声明自损信用），
            # 直接归位 base 真身（IGenTableColumnService controller/→service/ 错包族同治）。
            # round67c 护栏保持：唯一命中+候选根内，跨模块命中绝不凭名对账（上面 punt）。
            # 复核 HIGH-1：判据从 classpath_fqn_key=None 收紧为 jvm_compilable_layout——
            # 根级 src 单模块工程（模块根为空串）FQN key 恒 None 但完全可编译，绝不当幽灵。
            # 复核 LOW-1：显式否定复用语境（"不复用既有 X"）→ 尊重 LLM 声明不越权归位，
            # punt 留安置/布局闸/③f 权威链（fail-closed）。
            _di_ext = _di.rsplit(".", 1)[-1].lower() if "." in _di.rsplit("/", 1)[-1] else ""
            if (_di and _di_ext in _JVM_CLASS_FILE_EXTS
                    and not _jvm_compilable_layout(_di)
                    and not (Path(project_path) / _di).is_file()):
                if _REUSE_NEGATE_RE.search(_prose):
                    _todo_keep.append(e)
                    _punts.setdefault("显式否定复用语义", []).append(e["symbol"])
                    continue
                item["defined_in"] = _hit
                _base_refs.append(f"{e['symbol']}→{_hit}(幽灵布局 defined_in 对账归位)")
                continue
            if not _REUSE_INTENT_RE.search(_prose) or _REUSE_NEGATE_RE.search(_prose):
                _todo_keep.append(e)   # 无显式复用语义（或否定语境）→ 不动（fail-closed 留 ③f）
                _punts.setdefault("无显式复用语义", []).append(e["symbol"])
                continue
            # Case B：幻影/空 defined_in 归位到 base 真身（治法A 形态），跳过影子安置。
            item["defined_in"] = _hit
            _base_refs.append(f"{e['symbol']}→{_hit}(defined_in 归位)")
        if _punts:
            # 复核 F5（hunter MEDIUM-3①）：punt 方向原本零直接观测，复盘只能反推——
            # 聚合一条 INFO（转换失败方向=按原路径安置，③f 仍是权威兜底，非告警级）。
            logger.info(
                "[PLAN-FINISH] R67M-T2-B5 base 查表 punt（证据不足不转换，留安置/③f 权威）: %s",
                {k: v[:6] for k, v in sorted(_punts.items())})
        if _base_refs:
            todo = _todo_keep
            logger.warning(
                "[PLAN-FINISH] R67M-T2-B5 安置前 base 查表：%d 个契约符号实为存量引用，"
                "转 base 引用并跳过影子安置（防 G1 ③f _created_class_shadows_base 硬打回）: %s",
                len(_base_refs), _base_refs)

    groups: dict[str, list[str]] = {}
    for e in todo:
        groups.setdefault(e["module"], []).append(e["symbol"])
    # R67M2-T2 C1（24号文，round67m2 SysJob 幽灵治本）安置落点布局闸：JVM 代码扩展名
    # 的安置落点必须在可编译源码布局内（jvm_compilable_layout）。round67m2 实证幽灵面：
    # 落点 ruoyi-quartz/SysJob.java 无 src 布局段 → mvn 不编译=验收假过，且
    # classpath_fqn_key=None 使 ③b/③f/samename 结构性失明、安置出 owner 使 C1 也盲。
    # 闸住=不建安置（符号留契约无主 + punt 账带出交 C1 owner 闸【硬打回】——复核
    # HIGH-2：punt 符号不占 0.4 无主宽容，防胖契约下静默蒸发），聚合 WARNING 可观测。
    # 栈中立：仅 JVM 代码扩展名参与（异栈零行为变化）。
    # ★复核 HIGH-1★：判据是 jvm_compilable_layout（布局段判定）而非 classpath_fqn_key
    # ——根级 src 单模块工程（src/main/java/... 直接在仓库根，模块根为空串）FQN key 恒
    # None 但完全可编译，拿 FQN key 当判据会整类误杀=新确定性死循环。
    # ★边界（r48b 既有行为保持）★：只闸【证据解析】落点（_resolved_dir/mod_dirs 命
    # 中——证据本身是幽灵布局，SysJob 族）；G4 零证据名兜底（mod/src/seg 最后手段，
    # 自有 G4 WARNING+G1 coherence 面、无更优确定性落点）不接——闸它=owner 闸确定
    # 性死循环（重试永远同样零证据），比半幽灵更糟。
    _evidence_dirs = set(_resolved_dir) | set(mod_dirs)
    _layout_punted: list[str] = []
    for mod in list(groups):
        if mod not in _evidence_dirs:
            continue
        _d0, _e0 = _dir_for(mod), _ext_for(mod)
        if _e0.lower() not in _JVM_CLASS_FILE_EXTS:
            continue
        _kept0: list[str] = []
        for s in groups[mod]:
            if not _jvm_compilable_layout(f"{_d0}/{s}.{_e0}"):
                _layout_punted.append(f"{s}→{_d0}/{s}.{_e0}")
            else:
                _kept0.append(s)
        if _kept0:
            groups[mod] = _kept0
        else:
            del groups[mod]
    if _layout_punted:
        logger.warning(
            "[PLAN-FINISH] R67M2-T2-C1 安置落点布局闸：%d 个契约符号的安置落点不在可编译"
            "源码布局内（幽灵路径=mvn 不编译验收假过+③b/③f 结构性失明）→ 不建安置，"
            "符号留契约无主+punt 账交 C1 owner 闸硬打回（不占 0.4 宽容，A1 全量可见反馈"
            "驱动重规划给真落点）: %s",
            len(_layout_punted), _layout_punted[:8])
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
        # 复核 LOW-8：分子分母都只算实际安置（len(todo) 含布局闸 punt 的、created 含
        # "_" 元键，都会虚报）。
        _placed_n = sum(len(v) for k, v in created.items() if not k.startswith("_"))
        _n_hosts = sum(1 for k in created if not k.startswith("_"))
        logger.info(
            "[PLAN-FINISH] R48b-1 契约符号安置：无主硬符号 %d 个（安置 %d/布局闸 punt %d）"
            " → 新建/收养 %d 个承接子任务: %s", len(todo), _placed_n,
            len(_layout_punted), _n_hosts,
            {k: v[:4] for k, v in created.items() if not k.startswith("_")})
    if _base_refs:
        # R67M-T2 B5：base 引用转换账以 "_" 前缀元键带出（调用方弹出独立入账；
        # 键空间与 sid 不相交，harness bootstrap 按 st.id 遍历不受影响）。
        created["_base_referenced"] = _base_refs
    if _layout_punted:
        # R67M2-T2 C1（复核 HIGH-2/MEDIUM-3）：布局闸 punt 账以 "_" 前缀元键带出——
        # punt 符号是确定性知道"按现有证据永不可安置"的，必须进 state 账交 C1 owner
        # 闸硬打回（不占 0.4 无主宽容），绝不只活在日志里（日志级别调高即蒸发）。
        created["_layout_punted"] = _layout_punted
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


# B-5（21 号文）：扩英文形态——规划 LLM 用英文描述时 "depends on st-X"/"after st-X"/
# "requires st-X" 同样是零歧义结构信号，漏配则 readable 唯一信号源对它们结构性失明
# （fail-safe 方向：C9 运行时补边兜底，但首批并派即 BLOCKED 白跑一轮）。
# 批次2 闸门 reviewer LOW：英文备选加 \b 左边界（"thereafter st-1" 不误配）；中文侧
# 绝不能裸加 \b——CJK 表意字在 re 里是 word char，"于依赖st-1" 无边界会回退匹配面。
_DESC_ST_DEP_RE = _re.compile(
    r"(?:依赖|\b(?:depends?\s+on|requires?|after))\s*(st-[A-Za-z0-9_-]+)", _re.IGNORECASE)


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
    # 批次2 闸门 hunter R2 LOW-2：正则 IGNORECASE 捕到的 "ST-1" 必须落到真身 id——
    # by_id 大小写敏感会把它们当幻影 id 静默跳过（IGNORECASE 一半收益落空）。
    _canon = {k.lower(): k for k in by_id}
    added: dict[str, list[str]] = {}
    for st in subs:
        sid = str(getattr(st, "id", ""))
        for dep in set(_DESC_ST_DEP_RE.findall(str(getattr(st, "description", "") or ""))):
            dep = _canon.get(dep.lower(), dep)
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
    # R67J-H3b①：成环对落 plan 持久账（dispatch 软序兜底/观测消费）。★always-emit 防
    # 粘滞★：无环轮也必须清空覆写——同 plan 对象 finish 重入/revision 场景下旧对残留
    # 会让 dispatch 软序 defer 错人（复核盲区四类之 always-emit，见记忆）。
    try:
        plan.symbol_cycle_pairs = [
            list(pr) for pr in sorted({(c, p) for c, p, _ in cycle_skipped})]
    except Exception:  # noqa: BLE001 — 落账是增益，绝不拖垮 plan-finish 主链
        logger.warning("[PLAN-FINISH] R67J-H3b 成环对落账失败（fail-open，仅失兜底）",
                       exc_info=True)
        # 猎手复核整改：异常路显式归零=自证不变量。当前调用图每轮重建全新 TaskPlan
        # （默认已空），此行防的是未来"就地 patch 现有 plan 对象"路径下残留上一轮陈旧对。
        try:
            plan.symbol_cycle_pairs = []
        except Exception:  # noqa: BLE001,S110 — 归零再失败只能放行（对象本身异常）
            pass
    if cycle_skipped:
        logger.warning(
            "[PLAN-FINISH] R67-T4b %d 条符号消费边会成环 → 跳过（多为拆分簇兄弟互引且"
            "反向边已保序；边方向属更深计划错留 VALIDATE/C9 面）: %s",
            len(cycle_skipped), cycle_skipped[:6])
        # R67J-H3b②：消费者注入确定性提示。round67 64cb44ed 真根：消费者先于生产者执行
        # 时 worker 面对"引用一个尚不存在的类"只有两条死路——臆造同名类（编译可过=假过，
        # L1 全盲）或 cannot find symbol（H-3a 后=BLOCKED，但生产者在等消费者=互等结徒劳
        # 退避）。提示把 worker 钉在第三条活路上：不引用、不臆造、确需则如实报缺符号。
        try:
            _note_by_sid: dict[str, set[tuple[str, str]]] = {}
            for c, p, tok in cycle_skipped:
                _note_by_sid.setdefault(c, set()).add((p, tok))
            for c, items in _note_by_sid.items():
                st = by_id.get(c)
                if st is None:
                    continue
                cur = str(getattr(st, "context_snippets", "") or "")
                if "R67J-H3b 成环消费" in cur:
                    continue        # plan-finish 可重入（replan/revision）→ 幂等不堆叠
                _lines = "；".join(
                    f"{tok}（由 {p} 在你之后创建）" for p, tok in sorted(items))
                st.context_snippets = cur + (
                    "\n\n⚠️ 结构提示（R67J-H3b 成环消费）：以下类由其他子任务在你【之后】"
                    f"创建，当前尚不存在：{_lines}。你的代码【不得】import/编译期引用它们，"
                    "也【绝不要】自行创建/臆造同名类（会与后续真身冲突导致启动崩溃）；"
                    "若你的验收确实需要编译期引用它，如实报告缺符号失败即可（系统会按"
                    " BLOCKED 序化处理），绝不要编造它的实现。")
        except Exception:  # noqa: BLE001 — 提示是增益，绝不拖垮 plan-finish 主链
            logger.warning("[PLAN-FINISH] R67J-H3b 消费者防臆造提示注入失败"
                           "（fail-open，仅失提示）", exc_info=True)
        # R67L-B3④（22号文批次3，round67l st-2 卷子必死实锤）：H3b 提示与考卷同源对账。
        # 成环跳过的符号，消费者 verify_commands 里的【正断言】（grep 必考 import/引用该
        # 符号）与本 pass 刚注入的"【不得】import/编译期引用"提示直接打架——worker 服从
        # 提示则验收必挂、服从验收则违提示臆造，卷子注定死（st-2 verify#2/#4 vs H3b）。
        # 剔除成环符号的正断言（fail-honest：该断言此刻不可满足，真判定交 producer 落地后
        # 的 BLOCKED 软序通道）；【负断言】（禁建同名类，`! grep` / test -z）与提示同向，
        # 保留不动。词边界匹配防 substring 误伤（Config≠AppConfig）。
        try:
            import re as _re
            _exam_dropped: dict[str, list[str]] = {}
            _toks_by_sid: dict[str, set[str]] = {}
            for c, _p, tok in cycle_skipped:
                _toks_by_sid.setdefault(c, set()).add(tok)
            for c, toks in _toks_by_sid.items():
                st = by_id.get(c)
                h = getattr(st, "harness", None) if st is not None else None
                vcs = list(getattr(h, "verify_commands", None) or []) if h else []
                if not vcs:
                    continue
                kept: list[str] = []
                for vc in vcs:
                    _s = str(vc).strip()
                    _neg = _s.startswith("!") or bool(_re.match(r"^test\s+-z", _s))
                    if (not _neg) and any(
                            _re.search(rf"\b{_re.escape(t)}\b", _s) for t in sorted(toks)):
                        _exam_dropped.setdefault(c, []).append(_s)
                        continue
                    kept.append(vc)
                if len(kept) != len(vcs):
                    h.verify_commands = kept
            if _exam_dropped:
                logger.warning(
                    "[PLAN-FINISH] R67L-B3④ H3b↔考卷同源对账：剔除 %d 个子任务对成环符号的"
                    "验收正断言（与'不得 import'提示打架=卷子必死，round67l st-2 死型）: %s",
                    len(_exam_dropped),
                    {k: [v[:60] for v in vs[:2]] for k, vs in
                     sorted(_exam_dropped.items())})
        except Exception:  # noqa: BLE001 — 对账是增益，绝不拖垮 plan-finish 主链
            logger.warning("[PLAN-FINISH] R67L-B3④ H3b↔考卷对账失败"
                           "（fail-open，矛盾卷留执行期闸兜底）", exc_info=True)
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


def reconcile_scope_actions_with_file_plan(plan, file_plan) -> dict[str, list[str]]:
    """R67L-B3①（22号文批次3，round67l 11 处实锤）：scope writable/create_files 与
    file_plan action 对账——file_plan 是经多道确定性 pass 策展的【权威归属】（R58-1），
    scope 的写权语义必须与之同源。

    st-3-2 死型：file_plan action=create（基线不存在=新建）却落 scope.writable（修改
    既有）→ worker 拿不到 create 语义（pull-back/上传账/验收三面全按 modify 走），
    C7 上传账族误杀的规划期同谋。反向（action=modify 落 create_files）同治。
    返回 {sid: [归一的文件…]} 机读账；幂等。
    """
    actions: dict[str, str] = {}
    for e in (file_plan or []):
        if isinstance(e, dict):
            _p = str(e.get("path") or "").replace("\\", "/").lstrip("/")
            _a = str(e.get("action") or "").strip().lower()
            if _p and _a in ("create", "modify"):
                actions.setdefault(_p, _a)   # 首现为准（H-6 已裁决去重，残留 dup 不猜）
    moved: dict[str, list[str]] = {}
    if not actions:
        return moved
    for st in (getattr(plan, "subtasks", None) or []):
        sc = getattr(st, "scope", None)
        if sc is None:
            continue
        w = list(getattr(sc, "writable", None) or [])
        cf = list(getattr(sc, "create_files", None) or [])
        if not w and not cf:
            continue
        new_w, new_cf = list(w), list(cf)
        for f in w:
            _n = str(f).replace("\\", "/").lstrip("/")
            if actions.get(_n) == "create" and f not in new_cf:
                new_w.remove(f)
                new_cf.append(f)
                moved.setdefault(str(getattr(st, "id", "")), []).append(f)
        for f in cf:
            _n = str(f).replace("\\", "/").lstrip("/")
            if actions.get(_n) == "modify" and f not in new_w:
                new_cf.remove(f)
                new_w.append(f)
                moved.setdefault(str(getattr(st, "id", "")), []).append(f)
        if new_w != w or new_cf != cf:
            sc.writable = new_w
            sc.create_files = new_cf
    if moved:
        logger.warning(
            "[PLAN-FINISH] R67L-B3① scope 写权语义与 file_plan action 对账归一 %d 处"
            "（create 错声明 writable=modify 语义三面错配，round67l st-3-2 型）: %s",
            sum(len(v) for v in moved.values()), moved)
    return moved


def wire_file_plan_depends_edges(plan, file_plan) -> dict[str, list[str]]:
    """R67L-B3②（22号文批次3，round67l 68 条断链实锤）：file_plan 声明的【文件级依赖边】
    下推到子任务 depends_on——file_plan 是规划期权威依赖账（html→controller、domain→sql
    断链实锤），不下推=dispatch 依赖闸看不见，消费者先于生产者执行（幽灵解封同族）。

    判据（与 W2/T4b 同律，宁缺毋滥）：owner(消费文件)≠owner(依赖文件) 才成边；已传递
    可达幂等跳过；会成环（生产者传递依赖消费者）跳过+WARNING（留 VALIDATE/C9 面）；
    依赖文件无 owner（孤儿）不猜——孤儿挂靠的是 attach_orphan 的职责。
    返回 {消费者 id: [新增上游 id…]} 机读账。
    """
    subs = list(getattr(plan, "subtasks", None) or [])
    if not subs or not file_plan:
        return {}
    by_id = {str(getattr(st, "id", "")): st for st in subs}
    owner: dict[str, str] = {}
    for st in subs:
        sid = str(getattr(st, "id", ""))
        sc = getattr(st, "scope", None)
        for f in (list(getattr(sc, "create_files", None) or [])
                  + list(getattr(sc, "writable", None) or [])):
            owner.setdefault(str(f).replace("\\", "/").lstrip("/"), sid)
    added: dict[str, list[str]] = {}
    skipped_cycle: list[tuple[str, str]] = []
    for e in file_plan:
        if not isinstance(e, dict):
            continue
        consumer = owner.get(str(e.get("path") or "").replace("\\", "/").lstrip("/"))
        if not consumer:
            continue
        for d in (e.get("depends_on") or []):
            producer = owner.get(str(d).replace("\\", "/").lstrip("/"))
            if not producer or producer == consumer:
                continue
            st = by_id.get(consumer)
            if st is None:
                continue
            cur = list(getattr(st, "depends_on", None) or [])
            if producer in cur or _plan_reaches(by_id, consumer, producer):
                continue                        # 已有/传递可达 → 幂等跳过
            if _plan_reaches(by_id, producer, consumer):
                skipped_cycle.append((consumer, producer))
                continue                        # 成环不猜（同 T4b，留结构闸面）
            st.depends_on = cur + [producer]
            added.setdefault(consumer, []).append(producer)
    if skipped_cycle:
        logger.warning(
            "[PLAN-FINISH] R67L-B3② %d 条 file_plan 依赖边会成环 → 跳过（留 VALIDATE/C9 面）: %s",
            len(skipped_cycle), skipped_cycle[:6])
    if added:
        logger.info(
            "[PLAN-FINISH] R67L-B3② file_plan 依赖边下推 depends_on %d 个消费者共 %d 条"
            "（html→controller/domain→sql 断链族，round67l 68 条实锤）: %s",
            len(added), sum(len(v) for v in added.values()), dict(sorted(added.items())))
    return added


def wire_module_pom_dep_edges(plan, dirs, project_path: str | None = None) -> dict[str, list[str]]:
    """R67L-B4②（22号文批次4，round67l st-14 未授权序执行实锤）：模块 pom【生产者→消费者】
    depends_on 边确定性补齐——file_plan depends_on 是 LLM 声明（round67l ruoyi-alarm/pom.xml
    声明空 → st-14 depends_on=[] 首批即派，生产者 ruoyi-alarm-interface 未建就越权写根 pom
    毒终态树）。模块 pom 的先后序不该靠 LLM 声明：reactor 解析期就要求被依赖模块 pom 先
    落地，且证据与 T5 模板注入完全同源（contract_utils.derive_module_pom_producer_edges：
    跨模块 readable code 文件=编译依赖）。

    成环守卫与 B3②/T4b 同律：已传递可达幂等跳过；生产者传递依赖消费者=会成环 → 跳过
    +WARNING（留 VALIDATE/C9 面，绝不猜方向）。project_path 缺省/异常 fail-open（跳过本
    pass，LLM 声明面与 C9 动态边兜底）。返回 {消费者 sid: [新增上游 sid…]} 机读账。
    """
    subs = list(getattr(plan, "subtasks", None) or [])
    if not subs or not dirs:
        return {}
    try:
        from swarm.brain.contract_utils import derive_module_pom_producer_edges
        spec = derive_module_pom_producer_edges(plan, dirs)
    except Exception:  # noqa: BLE001 — fail-open：C9 动态边/seed 闸执行期兜底
        logger.warning("[PLAN-FINISH] R67L-B4② 模块 pom 依赖边推导失败（fail-open）",
                       exc_info=True)
        return {}
    if not spec:
        return {}
    by_id = {str(getattr(st, "id", "")): st for st in subs}
    added: dict[str, list[str]] = {}
    skipped_cycle: list[tuple[str, str]] = []
    for consumer, producers in spec.items():
        st = by_id.get(consumer)
        if st is None:
            continue
        for producer in producers:
            cur = list(getattr(st, "depends_on", None) or [])
            if producer in cur or _plan_reaches(by_id, consumer, producer):
                continue                        # 已有/传递可达 → 幂等跳过
            if _plan_reaches(by_id, producer, consumer):
                skipped_cycle.append((consumer, producer))
                continue                        # 成环不猜（同 T4b，留结构闸面）
            st.depends_on = cur + [producer]
            added.setdefault(consumer, []).append(producer)
    if skipped_cycle:
        logger.warning(
            "[PLAN-FINISH] R67L-B4② %d 条模块 pom 依赖边会成环 → 跳过（留 VALIDATE/C9 面）: %s",
            len(skipped_cycle), skipped_cycle[:6])
    if added:
        logger.info(
            "[PLAN-FINISH] R67L-B4② 模块 pom 生产者→消费者依赖边补齐 %d 个消费者共 %d 条"
            "（LLM 声明缺口确定性补齐，round67l st-14 未授权序死型）: %s",
            len(added), sum(len(v) for v in added.values()), dict(sorted(added.items())))
    return added


def _is_pom_manifest_path(path: str) -> bool:
    """P-C1 复核 R2-1：pom 判定一律 **basename 相等**（与 #16/F1 `_evidence_class` 口径
    同源）——`endswith("pom.xml")` 会把 `xpom.xml` 这类同后缀文件误判为 pom create，
    注入 `test -f`/`! grep '<version>${'` 等 Maven 专属断言给非 pom 产物虚假背书。
    """
    return path.replace("\\", "/").rsplit("/", 1)[-1].strip().lower() == "pom.xml"


def ensure_pom_create_min_acceptance(plan, project_path: str | None) -> dict[str, list[str]]:
    """R67L-B3⑤（22号文批次3·裸奔闸，round67l st-3-1 实锤）：create pom.xml 的子任务
    零 verify_commands=零确定性闸过闸（st-3-1 零验收过掉 parent 3.8.7 幻觉 pom，
    基线真身 4.8.3）——坏产物直送 merge 毒化 reactor。

    最低验收强度（全确定性证据，绝不猜）：
      ① `test -f <pom>` —— 产物存在；
      ② parent 版本字面量断言 `! grep -q '<version>${' <pom>` —— 属性引用=reactor 读不出；
      ③ 根 pom 版本可读（_root_gav 确定性证据）且所创 pom【非根 pom 自身】时：`grep -A5
         '<parent>' <pom> | grep -q '<version>{根版}</version>'` —— parent 版本必须=基线根版
         （3.8.7 幻觉的死法）；根 pom 无 <parent> 块，注入即冤杀（终闸复核 M-1）。
    只补【零 verify】的子任务（已有考卷的不动，绝不削既有闸）；非 pom create 不动。

    ★P-C1 复核 F2（升级版：不是"话说宽了"，是真 fail-open）★ 本函数曾有
    `_bstk` 早返（基线为已知非 Maven 栈 ⇒ 整 pass 返 {}）——它撤掉的 ①② 是**栈无关**
    断言（产物存在性 + 对所创 pom 自身的字面量检查，对任何 pom 都成立），③ 早已
    自门控（`_root_gav` 在无根 pom.xml 时返 None ⇒ 非 Maven 基线自动省略 + WARNING）。
    早返对 ③ 冗余、对 ①② 有害：非 Maven 基线 + 幻觉 create-pom + 零 verify ⇒
    零确定性验收直送 worker＝st-3-1 原病复活，且注释自称"留 VALIDATE 打回"的那条闸
    **不存在**（plan_validator 全部 REJECT 无一管"非 Maven 基线里 create pom"）。
    ★为什么不是补一条 VALIDATE REJECT★ "非 Maven 基线里 create pom"不总是幻觉——
    多语言仓（python 根 + 新增 service-java/pom.xml）是合法形态，REJECT 会误杀它；
    ①② 在合法形态下无害且有益。没有确定性判据区分"幻觉 create-pom"与"合法多语言
    新模块"之前，绝不把幻想的闸写进注释（承诺比落地宽）。
    根版读不到 → ③ 省略有 WARNING（fail-open，①②仍在）。返回 {sid: [注入命令…]}。
    """
    from swarm.brain.contract_utils import _root_gav
    injected: dict[str, list[str]] = {}
    _root_ver = None
    if project_path:
        try:
            _rg = _root_gav(project_path)
            _root_ver = _rg[2] if _rg else None
        except Exception:  # noqa: BLE001 — 根版读不到仅少一条断言，绝不阻断
            logger.warning("[PLAN-FINISH] R67L-B3⑤ 根 pom GAV 读取失败，"
                           "parent 版本对账断言省略（fail-open，①②仍注入）", exc_info=True)
    for st in (getattr(plan, "subtasks", None) or []):
        sc = getattr(st, "scope", None)
        h = getattr(st, "harness", None)
        if sc is None or h is None:
            continue
        poms = [str(f).replace("\\", "/").lstrip("/")
                for f in (getattr(sc, "create_files", None) or [])
                if _is_pom_manifest_path(str(f))]
        if not poms or list(getattr(h, "verify_commands", None) or []):
            continue                        # 非 pom create / 已有考卷 → 不动
        cmds: list[str] = []
        for pom in poms:
            cmds.append(f"test -f {pom} && echo OK")
            cmds.append(f"! grep -q '<version>${{' {pom}")
            if _root_ver and pom not in ("pom.xml", "./pom.xml"):
                # 根 pom 自身无 <parent> 块 → ③ 跳过（复核 M-1：注入即恒败冤杀）。
                # 终扫 hunter M-3：显式枚举判定——lstrip("./") 会把 ../pom.xml/.pom.xml
                # 也削成 "pom.xml" 误判根 pom 逃逸校验。
                cmds.append(
                    f"grep -A5 '<parent>' {pom} | grep -q '<version>{_root_ver}</version>'")
        h.verify_commands = cmds
        injected[str(getattr(st, "id", ""))] = cmds
    if injected:
        logger.warning(
            "[PLAN-FINISH] R67L-B3⑤ 裸奔闸：%d 个 create-pom 子任务零验收 → 注入最低验收"
            "强度（产物存在+parent 字面量+parent 版本对账根版 %s，st-3-1 型零闸过坏产物死型）: %s",
            len(injected), _root_ver, sorted(injected))
    return injected


def sanitize_negated_grep_exam(plan) -> dict[str, list[str]]:
    """R67L-B3⑥（22号文批次3，round67l st-3 run-1 误杀实锤）：禁令型【未锚定】子串 grep
    负断言的生成侧语义闸——`! grep -qi 'lombok' pom.xml` 会被 pom 注释散文
    （"零重依赖：不引入…lombok…"）撞死：禁令散文撞禁令，产物全对被冤杀。

    确定性重写（只改可证更严且语义等价/更准的形态，其余原样保留+WARNING 观测）：
      - 目标全为 pom.xml 且 pattern 是裸词（无空格/锚点/正则元字符）→
        `<artifactId>词</artifactId>`（注释散文不含完整标签，依赖禁令语义不变）；
      - 目标全为 JVM 源码/源码目录且 pattern 是小写包形词（含点，如 javax\\.）→
        锚定 import 行 `^[[:space:]]*import[[:space:]].*词`（注释行以 ///* 开头天然豁免）。
    复合命令（&&/|/;）、含空格散文 pattern（'class X'）、已锚定 → 不动。
    返回 {sid: [改写后命令…]} 机读账。
    """
    import re as _re
    rewritten: dict[str, list[str]] = {}
    _NEG = _re.compile(r"^!\s*grep\s+(?P<flags>(?:-[a-zA-Z]+\s+)*)"
                       r"(?P<q>['\"])(?P<pat>[^'\"]+)(?P=q)(?P<rest>.*)$")
    _WORD = _re.compile(r"^[A-Za-z0-9_-]+$")
    # 包形词：段间可带转义点、允许尾点（javax\. / org.springframework.security / lombok）。
    # 起头限小写（复核 M-2）：大写类名（Lombok）不是包形词——锚定 import 会把"不得使用"
    # 弱化成"不得 import"（new Lombok() 内联用法漏网），宁保守不动。
    _PKG = _re.compile(r"^[a-z_][A-Za-z0-9_]*(\\?\.[A-Za-z0-9_]*)*\\?\.?$")
    for st in (getattr(plan, "subtasks", None) or []):
        h = getattr(st, "harness", None)
        vcs = list(getattr(h, "verify_commands", None) or []) if h else []
        if not vcs:
            continue
        new_vcs: list[str] = []
        changed: list[str] = []
        for vc in vcs:
            s = str(vc).strip()
            # 良性后缀 `&& echo WORD`（st-14 型：`! grep … && echo NO_LOMBOK`）可剥离再
            # 拼回——退出码由左侧 grep 决定（grep 命中→!失败→echo 不跑→非零；未命中→echo
            # 跑→零），语义与裸 `! grep` 完全一致。其余复合形态（||/;/管道）不猜语义。
            suffix = ""
            # 复核 L-4：引号形态（echo "NO_LOMBOK"）与 printf 同义后缀也属良性可剥离
            _m_sfx = _re.search(r"\s*&&\s*(?:echo|printf)\s+(?:['\"][^'\"]*['\"]|\S+)\s*$", s)
            if _m_sfx:
                suffix = s[_m_sfx.start():]
                s = s[:_m_sfx.start()].strip()
            if any(op in s for op in ("&&", "||", ";", "|")):
                new_vcs.append(vc)
                continue                            # 复合命令不猜语义
            m = _NEG.match(s)
            if not m:
                new_vcs.append(vc)
                continue
            pat, rest = m.group("pat"), m.group("rest").strip()
            targets = [t for t in rest.split() if not t.startswith("-")]
            if not targets:
                new_vcs.append(vc)
                continue
            new_pat = None
            if all(_is_pom_manifest_path(t) for t in targets) and _WORD.fullmatch(pat):
                new_pat = f"<artifactId>{pat}</artifactId>"
            elif (all(t.endswith((".java", ".kt", ".scala")) or "/src/" in t
                      for t in targets) and _PKG.fullmatch(pat)):
                new_pat = f"^[[:space:]]*import[[:space:]].*{pat}"
            if new_pat is None:
                new_vcs.append(vc)
                continue
            nv = f"! grep {m.group('flags') or ''}'{new_pat}' {rest}{suffix}"
            new_vcs.append(nv)
            changed.append(nv)
        if changed:
            h.verify_commands = new_vcs
            rewritten[str(getattr(st, "id", ""))] = changed
    if rewritten:
        logger.warning(
            "[PLAN-FINISH] R67L-B3⑥ 禁令型未锚定子串 grep 语义闸：重写 %d 个子任务的负断言"
            "为注释豁免形态（st-3 run-1 禁令散文撞禁令误杀死型）: %s",
            len(rewritten), sorted(rewritten))
    return rewritten


def reconcile_dep_ban_prose(plan) -> dict[str, dict]:
    """R67M-T1（round67m FAILED@PLAN 死因治本）：依赖禁令散文 vs 注入坐标的【真矛盾】
    确定性自愈——把全称硬禁令改写为相对禁令，消弭 ③d 打回面，不再交"修一拨冒一拨"的
    LLM 重产循环。

    round67m 三轮燃烧实证（轮1 st-1 / 轮2 st-11-1,11-2『零第三方依赖』vs 坐标注入，每轮
    ~35min k3 全量重产，MAX_PLAN_RETRY 3/3 耗尽终态 FAILED）：③d 原设计"交打回反馈让 LLM
    显式裁决"对本族不可靠——LLM 每轮修一拨冒一拨。

    自愈边界（保守不挑边，与 ③d 同判据同源代码）：
      - 只动 ③d 会判【真矛盾】的禁令句：全称禁令命中 ∧ 无软化词 ∧ scope=third_party ∧
        注入坐标经 ③d 同源过滤（_is_internal_dep_coord：内部 reactor 坐标=模块物理根+
        全坐标 group 证据，复核 A3）后仍有真第三方冲突（spring-boot-starter-* 型）。
      - 改写方向=相对化（"除已声明必需依赖外不引入新的第三方依赖"）：既不删禁令（保守卫
        价值）也不剥坐标（不挑边——与 R65E10-T2 lombok 有磁盘实证可自动剥不同，此处无实证）。
        相对句天然命中 ③d 相对表述豁免（模板列既有依赖不构成矛盾），规划期确定性消弭。
      - ★复核 A1（CRITICAL）★原位改写必须保否定：match 前已有禁止动词（不引入/禁止/不得…）
        才用名词形；否则（『零第三方依赖』型否定在 match 内）用子句形（自带"不引入"）——
        名词形无条件替换会把『实现X：零第三方依赖。』改成肯定式病句（否定随匹配段被吞），
        ③d 静默=假过。
      - "仅用/只用 JDK"（scope=all）不动——该语义下任何坐标注入皆矛盾，改写会架空设计
        声明；仍交 ③d REJECT（fail-closed）。
      - 具名禁令面不动（具名禁令与无关依赖并存合法，本就零误杀面）。

    返回 {sid: {"old": 原禁令句, "coords": [冲突坐标…]}} 机读账。
    """
    from swarm.brain.plan_validator import (
        _BAN_SOFTENER_RE, _MAVEN_COORD_RE, _TEMPLATE_DEP_RE, _UNIVERSAL_DEP_BAN_RE,
        _ban_sentence_span, _dep_ban_scope, _internal_dep_groups,
        _internal_module_artifacts, _is_internal_dep_coord,
    )
    # 相对禁令改写件三形：整句形（裸禁令句整句替换）/子句形（原位替换且自带"不引入"保否定）
    # /名词形（仅 match 前已有禁止动词时用，否定由残留动词承载，复核 A1）。三形均刻意避开
    # _UNIVERSAL_DEP_BAN_RE/_SPECIFIC_DEP_BAN_RE 的命中面（无"任何第三方"连续字、无"零第三方
    # 依赖"、无"仅用/只用"、"不引入"后不接 ASCII 名）——幂等。
    _REL_SENT = "除本任务已声明的必需依赖外，不引入新的第三方依赖"
    _REL_CLAUSE = "除本任务已声明的必需依赖外不引入新的第三方依赖"
    _REL_NOUN = "本任务已声明必需依赖之外的新第三方依赖"
    _PROHIB_VERB_RE = _re.compile(
        r"(?:不引入|不使用|不依赖|禁止引入|禁止使用|不得引入|不得使用|严禁引入|严禁使用"
        r"|禁止|严禁|不得|勿用)$")
    reconciled: dict[str, dict] = {}
    internal_arts = _internal_module_artifacts(plan)
    internal_groups = _internal_dep_groups(plan, internal_arts)
    for st in getattr(plan, "subtasks", None) or []:
        desc = str(getattr(st, "description", "") or "")
        ac_text = "\n".join(str(a) for a in (getattr(st, "acceptance_criteria", None) or []))
        injected = [a.strip() for a in _TEMPLATE_DEP_RE.findall(desc)]
        injected += _MAVEN_COORD_RE.findall(ac_text)
        if not injected:
            continue
        # ③d 同源 scope 过滤：内部 reactor 坐标豁免后仍有真第三方冲突才有自愈面
        hits = [a for a in injected
                if not _is_internal_dep_coord(a, internal_arts, internal_groups)]
        if not hits:
            continue
        old_sent = None
        while True:  # 替换改变串长→每轮重扫；替换件不再命中禁令正则，保证收敛
            m_uni = None
            sent = None
            s_lo = s_hi = 0
            for m in _UNIVERSAL_DEP_BAN_RE.finditer(desc):
                _s, _e = _ban_sentence_span(desc, m.start(), m.end())  # 复核 A5：共享句界
                sent = desc[_s:_e]
                if _BAN_SOFTENER_RE.search(sent):
                    continue
                if _dep_ban_scope(sent) == "all":
                    continue  # 仅用/只用 JDK：改写架空设计声明，留 ③d REJECT（fail-closed）
                m_uni, s_lo, s_hi = m, _s, _e
                break
            if m_uni is None:
                break
            if old_sent is None:
                old_sent = sent
            if sent.strip() == m_uni.group(0).strip():
                desc = desc[:s_lo] + _REL_SENT + desc[s_hi:]      # 裸禁令句→整句相对化
            elif _PROHIB_VERB_RE.search(desc[s_lo:m_uni.start()].rstrip()):
                desc = desc[:m_uni.start()] + _REL_NOUN + desc[m_uni.end():]  # 前有禁止动词→名词形
            else:
                desc = desc[:m_uni.start()] + _REL_CLAUSE + desc[m_uni.end():]  # 否定在 match 内→子句形保否定（A1）
        if old_sent is not None:
            st.description = desc
            reconciled[str(getattr(st, "id", "?"))] = {"old": old_sent.strip(),
                                                       "coords": hits[:6]}
    if reconciled:
        logger.warning(
            "[PLAN-FINISH] R67M-T1 依赖禁令散文自愈：%d 个子任务的全称硬禁令已相对化"
            "（除已声明必需依赖外不引入新第三方依赖）——round67m 三轮燃烧死因治本，"
            "真矛盾不再交 LLM 重产循环: %s",
            len(reconciled), sorted(reconciled))
    return reconciled


def finish_plan_deterministic(plan, file_plan, project_path: str | None = None,
                              task_description: str = "",
                              shared_contract: dict | None = None,
                              base_ref: str | None = None,
                              adjudications: list | None = None) -> dict:
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
        # R67B-T1（对抗双复核 HIGH 整改）：跨模块 create 归属重规范化必须覆盖【全部】plan
        # 产出路径（单发/ULTRA 分批/P1/R39-5/R40-1 外科都汇入本收尾器）——只挂 ULTRA 分批
        # 一处时，round67b 原死因（create 落既有基线模块目录但标签错位→G1 违①→打回 PLAN
        # 空转）在 file_plan≤30 或外科通道上原样复现。ULTRA 路径分批前已跑过一次（批次按
        # module 分组需先归位），此处幂等重跑对其为 no-op；放脚手架注入之前——标签先归位，
        # 注入器才不会对错位模块按歧义拒绝脚手架。
        from swarm.brain.plan_batch import renormalize_cross_module_creates
        _renorm = renormalize_cross_module_creates(
            file_plan, project_path, base_ref=base_ref)
        if _renorm:
            out["cross_module_creates_renormalized"] = _renorm
    except Exception:  # noqa: BLE001 — fail-open，G1 权威兜底
        out["cross_module_renormalize_failed"] = True  # 终扫：崩溃≠零命中，扫尾进 degraded
        logger.warning("[PLAN-FINISH] R67B-T1 归属重规范化失败（fail-open，G1 兜底）",
                       exc_info=True)
    try:
        # R67E-P2（round67e 类治，类名 file-path 分叉）：必跑在【renormalize 之后、孤儿挂靠/脚手架之前】
        # ——rename create_files 必须与 file_plan 同串归一（否则 R40-1 判孤儿 + attach 把旧名复活成重复）；
        # 早于孤儿挂靠读 file_plan。owner create_files+file_plan+desc/AC/verify 三面+contract defined_in 对齐
        # 到契约名（greenfield 磁盘判方向，fail-closed 重），names 转 tier0 后 elaborate pin/wire 接管消费方。
        if shared_contract:
            from swarm.brain.contract_utils import reconcile_contract_symbol_paths
            _csp = reconcile_contract_symbol_paths(
                plan, file_plan, project_path=project_path, base_ref=base_ref)
            if _csp:
                out["contract_symbol_paths_reconciled"] = _csp
    except Exception:  # noqa: BLE001 — fail-open，pin tier2_only 现状兜底；残留分叉由下方 always-emit 重跑 detect 观测
        out["contract_symbol_paths_reconcile_failed"] = True
        logger.warning("[PLAN-FINISH] R67E-P2 契约类名 file-path 对齐失败（fail-open，pin tier2_only 兜底）",
                       exc_info=True)
    if shared_contract:
        # ★hunter CRITICAL 整改：未愈可见★——独立 try（reconcile 崩了也跑，故 crash-残留也观测到）重跑
        # detect（幂等：已愈转 tier0 消失，残留=punt/畸形/歧义未愈的分叉，将死 L2）。★Finding B：写
        # last-write-wins 观测键 always-emit（愈合=[] 清空不粘滞），plan 节点整体替换回 state，绝不进
        # append-only degraded_reasons（那里无人能清→愈后陈旧粘滞永久误拦 should_write_success+误导 deliver）★。
        # 刻意不硬 REJECT（会复刻 round67e 名分叉 LLM 重产不收敛熔断；file-path 分叉 LLM 改不动归属），
        # 诚实观测由 L2 真失败兜底门（北极星：honest PARTIAL > false DONE / 熔断）。
        try:
            from swarm.brain.symbol_provenance import (
                detect_contract_classname_divergences,
            )
            out["contract_symbol_paths_unhealed"] = sorted(
                {d["symbol"] for d in detect_contract_classname_divergences(plan)})
        except Exception:  # noqa: BLE001
            logger.warning("[PLAN-FINISH] R67E-P2 未愈分叉 detect 复扫失败（观测缺失，非致命）",
                           exc_info=True)
    try:
        # R67L-B3①（round67l 11 处实锤）：scope 写权语义与 file_plan action 对账——放
        # 【renormalize/契约符号路径对齐之后、脚手架注入之前】：落点与标签都已定格，
        # create 错声明 writable（st-3-2 型）先归一，后续 pass 看到的 scope 语义才真。
        _rsa = reconcile_scope_actions_with_file_plan(plan, file_plan)
        if _rsa:
            out["scope_actions_reconciled"] = _rsa
    except Exception:  # noqa: BLE001 — fail-open，G1/VALIDATE 兜底
        # 复核 M-2：崩溃≠零命中，机读标记让审计可区分（对称 upstream_account_reconcile_failed）
        out["scope_actions_reconcile_failed"] = True
        logger.warning("[PLAN-FINISH] R67L-B3① scope↔file_plan action 对账失败（fail-open）",
                       exc_info=True)
    try:
        # R67C-T3b：pom-写倒挂拆分——必跑在脚手架注入【之前】，R58-3 才会把权威模板嵌进新早叶 owner。
        from swarm.brain.contract_utils import split_manifest_owner_leaf
        _split = split_manifest_owner_leaf(plan)
        if _split:
            out["manifest_leaves_split"] = _split
            from swarm.brain.nodes.shared import bootstrap_subtask_harness
            _lids = {e["leaf"] for e in _split}
            for st in plan.subtasks:
                if st.id in _lids:
                    bootstrap_subtask_harness(st, task_description or st.description)
                    if not getattr(st, "est_context_tokens", 0):
                        st.est_context_tokens = 8000 + 6000   # TRIVIAL 基线+1 文件
    except Exception:  # noqa: BLE001 — fail-open，VALIDATE 兜底
        out["manifest_leaf_split_failed"] = True  # 终扫：崩溃≠零命中
        logger.warning("[PLAN-FINISH] R67C-T3b pom-写倒挂拆分失败（fail-open）", exc_info=True)
    try:
        from swarm.brain.contract_utils import inject_build_scaffold_subtasks
        # R58-1：file_plan 是【模块 → 文件】的权威归属（逻辑模块名 ≠ 物理目录时唯一的证据源）
        # ★P-C2 复核 F-2★ `_unverified` 收集 P-C2 闸【没能证实】的坐标。三种结局原先全塌成
        # `source="explicit"`：确证存在 / registry·proxy 不可达 fail-open / 刻意不判。
        # 塌成一个值的后果不是标签不好看——国内环境 proxy.golang.org 常不可达时，F1 收紧后
        # `proxy_version_exists` 永不返 False ⇒ 闸**整轮静默失效**，而交付物与闸正常时逐字
        # 相同，唯一信号是每依赖一条 WARNING（纪律 #106 禁止解析 swarm.log ⇒ 等于没有信号）。
        _unverified: dict = {}
        injected = inject_build_scaffold_subtasks(plan, project_path, file_plan,
                                                 unverified_out=_unverified)
        out["scaffolds"] = [e["module"] for e in injected]
        # always-emit（空也发）：同 dep_ban_reconciled/contract_symbol_paths_unhealed 口径，
        # last-write-wins 不粘滞。刻意**不做成闸**：不可达是环境常态，拿它拦 auto_accept 会
        # 让每个 plan 都 degraded ⇒ 使用者必然绕开（"过宽的闸使用者会绕开"）。纯诚实观测。
        out["dep_versions_unverified"] = {m: sorted(set(v)) for m, v in _unverified.items()}
        if _unverified:
            logger.warning(
                "[PLAN-FINISH] P-C2 F-2：%d 个模块存在未经证实/不判的依赖坐标（闸 fail-open "
                "保留了 LLM 主张；无下游兜底，止于 WARNING——npm/go 的 L1 dep-legality "
                "是空转，见 27 号文 P-C2 R2）→ 已记 dep_versions_unverified: %s",
                len(_unverified), {m: v[:3] for m, v in list(_unverified.items())[:4]})
        if injected:
            from swarm.brain.nodes.shared import bootstrap_subtask_harness
            _ids = {e["subtask_id"] for e in injected}
            for st in plan.subtasks:
                if st.id in _ids:
                    bootstrap_subtask_harness(st, task_description or st.description)
                    if not getattr(st, "est_context_tokens", 0):
                        st.est_context_tokens = 8000 + 6000  # TRIVIAL 基线+1 文件
    except Exception:  # noqa: BLE001 — fail-open，VALIDATE 兜底
        out["scaffold_inject_failed"] = True  # 终扫：崩溃≠零命中
        logger.warning("[PLAN-FINISH] 脚手架注入失败（fail-open）", exc_info=True)
    try:
        # R67L-B3⑤（round67l st-3-1 裸奔实锤）：create-pom 子任务零验收 → 注入最低验收
        # 强度。放【脚手架注入之后】：脚手架注入的 owner 自带考卷的不动，只补真零验收者。
        _mpc = ensure_pom_create_min_acceptance(plan, project_path)
        if _mpc:
            out["pom_create_min_acceptance"] = sorted(_mpc)
    except Exception:  # noqa: BLE001 — fail-open，L1 闸兜底
        out["pom_create_min_acceptance_failed"] = True  # 复核 M-2：崩溃≠零命中
        logger.warning("[PLAN-FINISH] R67L-B3⑤ 裸奔闸最低验收注入失败（fail-open）",
                       exc_info=True)
    try:
        # R62-Task3：R57-6 收权后确定性剪除空写 scope 死子任务（无人依赖者），
        # 否则一路漏到 dispatch → worker 空转 churn。
        from swarm.brain.contract_utils import prune_empty_scope_subtasks
        _pruned = prune_empty_scope_subtasks(plan)
        if _pruned:
            out["pruned_empty_scope"] = _pruned
    except Exception:  # noqa: BLE001 — fail-open
        out["prune_empty_scope_failed"] = True  # 终扫：崩溃≠零命中
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
            # H-6 前置核：裁决账（strip/relocate/dedupe）命中的路径【不挂靠不新建】——该路径
            # 是被确定性 pass 裁决剥离的副本，孤儿状态是【正确】的，挂靠=复活（round67h 环根）。
            attached, left = attach_orphan_file_plan_entries(
                plan, paths, adjudications=adjudications)
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
        out["orphan_attach_failed"] = True  # 终扫：崩溃≠零命中
        logger.warning("[PLAN-FINISH] 孤儿文件挂靠失败（fail-open）", exc_info=True)
    try:
        # ④ R48b-1：契约符号安置（P1 命中会短路 R39-5 符号外科——收尾器全路径必经）
        if shared_contract and len(plan.subtasks) > 1:
            dom = _domicile_contract_symbols(
                plan, shared_contract, project_path, task_description, file_plan)
            _bref = dom.pop("_base_referenced", None)
            if _bref:
                # R67M-T2 B5：安置前 base 查表转换账（影子安置被拦下的存量引用符号）
                out["contract_symbols_base_referenced"] = _bref
            _punted = dom.pop("_layout_punted", None)
            if _punted:
                # R67M2-T2 C1（复核 HIGH-2）：布局闸 punt 账——确定性永不可安置的符号，
                # 进 state 交 C1 owner 闸硬打回（不占 0.4 无主宽容，防胖契约静默蒸发）
                out["contract_symbols_layout_punted"] = _punted
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
        out["symbol_domicile_failed"] = True  # 终扫：崩溃≠零命中
        logger.warning("[PLAN-FINISH] 契约符号安置失败（fail-open）", exc_info=True)
    try:
        # R62-Task5：readable 幻影包路径归一到 producer 真实落点（放【末端】——所有
        # producer 含 domicile 新建者都已就位，落点已定）→ provenance 一致，consumer 编得过。
        from swarm.brain.contract_utils import align_readable_to_producer
        _al = align_readable_to_producer(plan, project_path)
        if _al.get("aligned"):
            out["readable_aligned"] = _al["aligned"]
    except Exception:  # noqa: BLE001 — fail-open
        out["readable_align_failed"] = True  # 终扫：崩溃≠零命中
        logger.warning("[PLAN-FINISH] readable 落点归一失败（fail-open）", exc_info=True)
    try:
        # R65D-W2①：消费边下推（放 readable 归一之后——落点已定，边才准）
        _ce = derive_consumer_depends_edges(plan)
        if _ce:
            out["consumer_edges"] = _ce
    except Exception:  # noqa: BLE001 — fail-open
        out["consumer_edges_failed"] = True  # 终扫：崩溃≠零命中
        logger.warning("[PLAN-FINISH] 消费边下推失败（fail-open）", exc_info=True)
    try:
        # R67L-B3②（round67l 68 条断链实锤）：file_plan 声明依赖边下推 depends_on——
        # 放【W2 消费边之后、T4a/T4b 之前】：readable 推得出的 W2 已建，file_plan 权威
        # 依赖账（html→controller/domain→sql）补 W2 盲区；成环守卫与 T4b 同律。
        _fpde = wire_file_plan_depends_edges(plan, file_plan)
        if _fpde:
            out["file_plan_dep_edges"] = _fpde
    except Exception:  # noqa: BLE001 — fail-open
        out["file_plan_dep_edges_failed"] = True  # 复核 M-2：崩溃≠零命中
        logger.warning("[PLAN-FINISH] R67L-B3② file_plan 依赖边下推失败（fail-open）",
                       exc_info=True)
    try:
        # R67L-B4②（round67l st-14 未授权序实锤）：模块 pom 生产者→消费者依赖边确定性
        # 补齐——file_plan depends_on 是 LLM 声明（st-14 声明空→首批即派毒终态树），
        # 模块 pom 序走 T5 同源证据（跨模块 readable code=编译依赖），不靠 LLM 声明。
        # 放 B3② 之后：LLM 声明面先下推，本 pass 只补声明缺口的 pom 序（幂等守卫同律）。
        from swarm.brain.contract_utils import _module_physical_dirs
        _dirs_b4 = _module_physical_dirs(plan, project_path, file_plan) if project_path else {}
        _mpde = wire_module_pom_dep_edges(plan, _dirs_b4, project_path)
        if _mpde:
            out["module_pom_dep_edges"] = _mpde
    except Exception:  # noqa: BLE001 — fail-open
        out["module_pom_dep_edges_failed"] = True  # 复核 M-2：崩溃≠零命中
        logger.warning("[PLAN-FINISH] R67L-B4② 模块 pom 依赖边补齐失败（fail-open）",
                       exc_info=True)
    # R67-T4a/T4b：自然语言消费关系补边（放 W2 之后——readable 推得出的边 W2 已建，
    # 此处只接 W2 结构盲区）。★hunter F3 整改★两 pass 各自 try（与本函数逐 pass 纪律一致）：
    # T4b 崩不吞 T4a 成果可见性，日志带已落边账区分"零边"vs"半应用"。
    try:
        _t4a = wire_described_dependency_tokens(plan)
        if _t4a:
            out["described_dep_edges"] = _t4a
    except Exception:  # noqa: BLE001 — fail-open
        out["described_dep_edges_failed"] = True  # 终扫：崩溃≠零命中
        logger.warning("[PLAN-FINISH] R67-T4a 词元补边失败（fail-open；已落边账=%s）",
                       out.get("described_dep_edges"), exc_info=True)
    try:
        _t4b = wire_symbol_consumption_edges(plan)
        if _t4b:
            out["symbol_consumption_edges"] = _t4b
    except Exception:  # noqa: BLE001 — fail-open
        out["symbol_consumption_edges_failed"] = True  # 终扫：崩溃≠零命中
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
    try:
        # R67E-T1（round67e 死因治本 task 88584950）：C2 契约方法名分叉【确定性自愈】——放
        # 【末端】（owner description 已被前序 pass 定型）。契约=权威真值源，对齐 owner
        # description/acceptance_criteria/verify_commands 三面里的方法名变体，消除 C2 分叉，
        # 免 VALIDATE C2 闸打回 LLM 重产（round67e 5 轮不收敛熔断真根）。fail-open：自愈挂了
        # C2 闸兜底打回，不更糟。
        if shared_contract:
            from swarm.brain.contract_utils import reconcile_contract_method_names
            _cmn = reconcile_contract_method_names(plan, shared_contract)
            if _cmn:
                out["contract_method_names_reconciled"] = _cmn
    except Exception:  # noqa: BLE001 — fail-open，VALIDATE C2 兜底
        # hunter F1(HIGH)：整体失效=退回 round67e 死因链（C2 分叉原样→打回→LLM 重产不收敛
        # 熔断），必须进 degraded_reasons 可查（对称 upstream_account_reconcile_failed）——
        # 否则唯一信号是无人 grep 的 WARNING，违反"进度查 API 绝不解析 swarm.log"纪律。
        out["contract_method_names_reconcile_failed"] = True
        logger.warning("[PLAN-FINISH] R67E-T1 契约方法名自愈失败（fail-open，C2 闸兜底）",
                       exc_info=True)
    try:
        # R67L-B3⑥（round67l st-3 run-1 误杀实锤）：禁令型未锚定子串 grep 负断言语义闸——
        # 放【末端】（reconcile_template_exam/H3b 对账都已定格考卷，本 pass 只做注释豁免
        # 形态重写，不增删断言面）。
        _snge = sanitize_negated_grep_exam(plan)
        if _snge:
            out["negated_grep_sanitized"] = sorted(_snge)
    except Exception:  # noqa: BLE001 — fail-open，原样考卷留执行期
        out["negated_grep_sanitize_failed"] = True  # 复核 M-2：崩溃≠零命中
        logger.warning("[PLAN-FINISH] R67L-B3⑥ 禁令 grep 语义闸失败（fail-open）",
                       exc_info=True)
    try:
        # R67M-T1（round67m 三轮燃烧死因治本）：依赖禁令散文真矛盾确定性自愈——放【末端】
        # （reconcile_template_exam/H3b 对账已定格考卷注入坐标，本 pass 只把 ③d 会判真矛盾
        # 的全称禁令句相对化）。fail-open：自愈挂了 ③d 兜底打回，不更糟。
        _rdb = reconcile_dep_ban_prose(plan)
        if _rdb:
            out["dep_ban_reconciled"] = _rdb
    except Exception:  # noqa: BLE001 — fail-open，③d 兜底
        out["dep_ban_reconcile_failed"] = True  # 崩溃≠零命中，通用扫尾自动进 degraded
        logger.warning("[PLAN-FINISH] R67M-T1 依赖禁令散文自愈失败（fail-open，③d 兜底）",
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
