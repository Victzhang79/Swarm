"""HANDLE_FAILURE 节点实现 —— 从 brain/nodes/__init__.py 抽出（round26 god-file 治理）。

承载 ~660 行的失败处理决策 `_handle_failure_impl`（strategy=retry/replan/escalate 分派、
超时重拆、缺依赖注入、pom 写权授予、scope 扩宽、放弃保 build 等恢复阶梯编排）+ 辅助
`_l1_details_of`。薄包装 `handle_failure`（round24 A4 plan 持久化 seam）仍留 __init__——其 bare
调用 `_handle_failure_impl` 经 __init__ re-export 解析，保 `patch("swarm.brain.nodes._handle_failure_impl")`
的 seam 契约不变。

依赖【单向】：failure → recovery/planning_core/maven_repair/shared（各自不反向 import __init__）。
本模块【禁】eager import brain.nodes.__init__（防 A6 环）——_get_brain_llm/_get_project_path 这两个
仍住 __init__ 的符号在函数内 lazy import（同时保其可 patch 性）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

# 复用 __init__ 的 logger 名 "swarm.brain.nodes"（非 __name__），使 [HANDLE_FAILURE] 策略日志的
# logger 名与外置前逐字节一致（这些是运维关键日志，避免 name 漂移影响既有日志过滤/聚合配置）。
logger = logging.getLogger("swarm.brain.nodes")

from swarm.brain.llm_schemas import FailureStrategyResponse
from swarm.brain.prompts import HANDLE_FAILURE_SYSTEM, HANDLE_FAILURE_USER
from swarm.brain.state import BrainState, effective_complexity
from swarm.config.settings import get_config
from swarm.types import Complexity, WorkerOutput

from swarm.brain.nodes.maven_repair import (
    classify_missing_deps_for_stack,
    inject_missing_deps_for_stack,
    stack_module_manifest,
)
from swarm.brain.nodes.recovery import (
    _INTERNAL_BLOCKED_KINDS,
    _blocked_pkg_unrecoverable,
    _det_of,
    _is_missing_dependency_failure,
    _module_in_git_baseline,
    _module_order_violation_modules,
    _producers_of,
    _root_manifest_registrants,
    _scaffold_subtask_of_module,
    sweep_baseline_anchor_poison,
)
from swarm.brain.nodes.planning_core import (
    _give_up_preserve_build,
    _grant_module_pom_writable,
    _has_stream_stall,
    _is_timeout_oversize_failure,
    _proj_path_from_state,
    _redecompose_timeout_subtasks,
    _serialize_pom_writers,
    _targeted_redecompose,
    _transitive_abandon,
    mass_abandon_cap,
    _widen_scope_for_compile_repair,
)
from swarm.brain.nodes.shared import (
    _parse_json_from_llm,
    attribute_runtime_failure,
    l1_passed,
    runtime_failure_evidence,
)

def _l1_details_of(subtask_results: dict, fid: str) -> dict:
    """取子任务的 L1 详情（§3.2：委托 shared.l1_details_of 单一实现，本地名保 seam）。"""
    from swarm.brain.nodes.shared import l1_details_of
    return l1_details_of(subtask_results.get(fid))


def _is_pipeline_blocked_victim(details: dict) -> bool:
    """#33-闸1/闸3：判失败是【连坐受害者】（自产物无错、仅被上游/-am 兄弟模块拖崩）还是
    【连坐根/病灶】（自模块编译崩/build_fail/拒答/退化）。round65e13 死型本体=单个病灶写坏
    reactor SPOF → -am 连坐一大片自产物全绿的下游，规模闸把 blast-radius 当"连坐规模"误判
    计划覆灭 escalate。受害者信号：pipeline_blocked 键真、det_fail_reason 以 pipeline_blocked
    打头、或自身 compile/build 已通过却仍 transient 失败（被 reactor 里坏兄弟阻塞）。规模闸
    只数病灶、闸1 只给病灶换备选，绝不因受害者众多误判。"""
    if not isinstance(details, dict):
        return False   # 缺证据默认根缺陷（fail-closed，silent-hunter 确认）
    # F1（silent-hunter HIGH 亲裁）：details["pipeline_blocked"] 是【分类字符串】，值域含自身
    # 病灶——worker_deadline_exhausted / malformed_diff_zero_files / build_infra_failure /
    # build_manifest_missing / test_infra_failure / verify_infra_failure。裸真值会把这些自身
    # 病灶误纳受害者→规模闸少算根缺陷→静默 PARTIAL（round65c 死型复活）。只有
    # _INTERNAL_BLOCKED_KINDS（upstream_module_broken/internal_pkg_not_built/
    # module_registered_before_scaffold）才是真·连坐受害者（被坏兄弟模块拖崩，自产物无错）。
    if details.get("pipeline_blocked") in _INTERNAL_BLOCKED_KINDS:
        return True
    _dfr = str(details.get("det_fail_reason") or "")
    if _dfr.startswith("pipeline_blocked") and any(k in _dfr for k in _INTERNAL_BLOCKED_KINDS):
        return True
    # F2（silent-hunter HIGH 亲裁）：删除 `build_ok + transient` 判据——它不要求任何 blocked
    # 证据，会把自己写的挂死测试（编译过+超时→transient）误判受害者。真受害者已被上面白名单
    # 覆盖（st-13=upstream_module_broken）。删更安全，缺证据一律当根缺陷（fail-closed）。
    return False


def _root_defect_ids(failed_ids, subtask_results) -> list:
    """#33-闸1/闸3：连坐根/病灶 id——失败且非纯 pipeline_blocked 受害者。规模闸真计量口径
    （独立根缺陷数，非 blast-radius 闭包）+ 闸1 换备选目标。受害者（自产物无错、仅被 -am
    兄弟拖崩）不计入根缺陷：单个高扇出病灶连坐一大片≠计划覆灭。"""
    return [fid for fid in (failed_ids or [])
            if not _is_pipeline_blocked_victim(_l1_details_of(subtask_results, fid))]


def _is_model_fixable_defect(details: dict) -> bool:
    """#33-闸1 专用判据（与闸3 计量口径 _root_defect_ids 分离）：仅【换模型能修】的缺陷才
    给 retry_alternate。round65e13 复核回归实锤：闸1/闸3 必须用不同判据——

    - 闸3（规模计量）用 _root_defect_ids【宽口径】：infra/自身病灶都算根缺陷（fail-closed，
      该 escalate；silent-hunter 亲裁）。
    - 闸1（换备选）只对【模型可修】：写坏语法（确定性 build_fail/compile 真错）、refusal_hard_fail、
      degeneration_hard_fail。任何 pipeline_blocked（infra/env/上游阻塞：sandbox_env_probe_blocked
      / *_infra_failure / build_manifest_missing / worker_deadline_exhausted / malformed_diff_zero_files
      / _INTERNAL_BLOCKED_KINDS）换模型都没用，一律【不】给闸1（test_b2_third_strike 实锤：
      sandbox_env_probe_blocked 是 infra→该走 partial/abandon，闸1 误换模型=白烧）。

    缺证据（l1_details 空）→ 保守【不】给闸1（infra 未知不赌模型换备），但仍进闸3 根缺陷计量。"""
    if not isinstance(details, dict) or not details:
        return False   # 缺证据保守不换（infra 未知不赌模型），闸3 仍按 fail-closed 计量
    if details.get("pipeline_blocked"):
        return False   # 任何 infra/env/upstream 阻塞非模型可修——换模型没用
    _src = details.get("l1_decision_source")
    if _src in ("refusal_hard_fail", "degeneration_hard_fail"):
        return True    # 拒答/复读退化=模型能力问题，换异构备选有意义
    if str(details.get("det_fail_reason") or "").startswith("build_fail"):
        return True    # worker 自己写坏的代码（确定性编译/语法真错）=换模型可修
    return False


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_DIGIT_RUN_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")


def _normalize_fail_sig(text: str) -> str:
    """R65E8-T2：确定性 L1 失败原因归一成【稳定指纹】——剥 ANSI 转义、数字串→N（版本号/行号/耗时
    抖动）、折叠空白、小写、截断。令 build/verify 假阴性跨轮同签名【可比】：否则原始 det_fail_reason
    含时间戳/行号/版本抖动会让指纹永不复现→检测器变哑（同 runtime_smoke plateau 的 log_tail 教训）。"""
    s = _ANSI_RE.sub("", str(text or ""))
    s = _DIGIT_RUN_RE.sub("N", s)
    s = _WS_RE.sub(" ", s).strip().lower()
    return s[:200]


# ── D3（round65e5 st-53-1 R2 实锤）：授 pom 写权后小模型手改基线 pom 腐化 ──
# R2：A2 授 pom 写权后，worker 把 `<groupId>` 手写成 `<group>`、毁 `<parent>` → 整棵 reactor
# 解析期崩（`Unrecognised tag: 'group'` / `'parent.groupId' is missing`）。恒给"最小增量铁律"，
# 从源头掐断手写腐化。★复核 HIGH 整改★ 铁律核心只讲【怎么改】、不臆断"已补好"——"已确定性补入"
# 是【条件性】事实（unprovisioned-only 那轮 A2 没补任何东西），拆成 `_D3_DEP_ADDED_NOTE` 仅在确有
# 注入/可自证依赖时才讲，否则会与 D2"此包全仓无坐标"自相矛盾、把假前提当硬约束喂给 worker。
_D3_POM_IRON_LAW = (
    "【pom 铁律】若改动 pom.xml，只允许在既有 <dependencies> 内【追加】一个 <dependency>，其余节点"
    "逐字节不动，绝不重写 <groupId>/<parent>/<modelVersion>（写成 <group> 或毁 <parent> 会让整棵 "
    "reactor 解析期崩）。"
)
_D3_DEP_ADDED_NOTE = (
    "【依赖已补】本模块所缺的【可自证坐标】依赖已由系统据项目 pom 确定性补入——通常你无需再动 pom.xml"
    "（如仍需改，严守上述铁律）。"
)
# ── D2（round65e5 st-53-1 R3 实锤）：缺失包在全仓+依赖树无坐标 = 臆造 import 或未引入的外部库 ──
# ★复核 MEDIUM 整改★ 两种可能【等权】给出、由 worker 自判，避免强导向"你臆造了"而压制真需要的
# 新外部库（首次合法使用某库时也会全仓无坐标）。仍明令"勿凭记忆臆造坐标"。
_D2_UNPROVISIONED_TMPL = (
    "【缺失包无坐标】{pkgs} 在项目全仓及依赖树中均无提供它的 Maven 依赖坐标。两种可能，请你自行判定："
    "①你臆造了 import 或 API（引用了不存在的子包/类）→ 核对该库【真实存在的】API 与包路径、修正 import，"
    "不要为不存在的包新增依赖坐标；②它是本项目尚未引入的必需外部库 → 在说明中标注你【确信准确】的坐标"
    "并交人工确认。切勿凭记忆臆造 groupId/version。"
)


def _dep_recovery_retry_guidance(granted: dict, classification: dict,
                                 injected: dict | None = None) -> dict:
    """D2/D3（round65e5 st-53-1）：为授 pom 写权的失败子任务构造重派 guidance（纯函数、可测）。

    返回 {sid: guidance_text}。
    - D3 铁律（`_D3_POM_IRON_LAW`）**恒给**——纯讲"怎么改 pom 不腐化"，任何情形都成立。
    - `_D3_DEP_ADDED_NOTE`（"已补入"）仅当本 sid 【确有注入】(`injected[sid]` 非空) 或有 provisionable
      包时才给——否则那是假前提（复核 HIGH：会与 D2"全仓无坐标"矛盾）。
    - D2（`_D2_UNPROVISIONED_TMPL`）仅当有 unprovisioned 包时给，点名该包、两种可能等权。
    """
    injected = injected or {}
    out: dict = {}
    for sid in (granted or {}):
        cls = classification.get(sid) or {}
        prov = cls.get("provisionable") or []
        unprov = sorted(set(cls.get("unprovisioned") or []))
        adds = [_D3_POM_IRON_LAW]
        if injected.get(sid) or prov:
            adds.append(_D3_DEP_ADDED_NOTE)
        if unprov:
            adds.append(_D2_UNPROVISIONED_TMPL.format(pkgs=unprov))
        out[sid] = "\n".join(adds)
    return out


# D2/D3 guidance 行前缀标记：逐轮 replace 语义（剔旧标记行→并 fresh，防陈旧包列表堆叠）。
_DEP_GUIDANCE_MARKERS = ("【pom 铁律】", "【依赖已补】", "【缺失包无坐标】")


def _merge_dep_guidance_lines(prev: str, fresh: str) -> str:
    """D2/D3 guidance replace 语义（纯函数、可测）：从 prev 剔除本机制旧标记行（保留 A4 诊断等其它
    行），再并入本轮 fresh。★复核 MEDIUM/F4 整改★ 杜绝缺包集跨轮变化时陈旧包列表【堆叠】——旧实现
    靠 `line not in text` 子串判定，只因模板标点巧合才不误吞，改动模板即脆。"""
    kept = [ln for ln in (prev or "").split("\n")
            if ln and not ln.startswith(_DEP_GUIDANCE_MARKERS)]
    return "\n".join(kept + fresh.split("\n"))


def _depends_reaches(plan_obj, src: str, dst: str) -> bool:
    """C9（阶段4）：depends_on 图上 src 是否可达 dst（动态补边前的环安全护栏）。"""
    by_id = {s.id: s for s in (getattr(plan_obj, "subtasks", None) or [])}
    seen: set[str] = set()
    stack = [src]
    while stack:
        cur = stack.pop()
        if cur == dst:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(getattr(by_id.get(cur), "depends_on", None) or [])
    return False


def _alt_map_update(state: dict, wave_ids, use_alternate: bool) -> dict[str, bool]:
    """阶段3.9 H-F7/R-F1：alternate 决策按子任务记账（替代全局 bool）。

    本次决策覆盖 wave_ids：alternate=True → 标记这些 sid；False（普通 retry/transient
    退避）→ 清掉这些 sid 的旧标记（决策被替换）。其余子任务的既有标记原样保留——
    dispatch 对失败撮降优先级会把重试者错开到后续批，标记必须活到它真被派出。"""
    _wave = set(wave_ids or [])
    out = {k: v for k, v in (state.get("subtask_use_alternate") or {}).items()
           if k not in _wave}
    if use_alternate:
        out.update({sid: True for sid in _wave})
    return out


def _planned_producers_exist(plan_obj, files: list) -> bool:
    """B3（round38c）：这些文件在全 plan 是否已有生产者（create_files/writable 命中，
    basename 宽容匹配——LLM 给的路径与计划声明可能差目录前缀）。"""
    if plan_obj is None or not files:
        return False
    declared: set[str] = set()
    basenames: set[str] = set()
    for st in (getattr(plan_obj, "subtasks", None) or []):
        sc = getattr(st, "scope", None)
        for f in (list(getattr(sc, "create_files", None) or [])
                  + list(getattr(sc, "writable", None) or [])):
            fn = str(f).replace("\\", "/")
            fn = fn[2:] if fn.startswith("./") else fn
            declared.add(fn)
            basenames.add(fn.rsplit("/", 1)[-1])
    for f in files:
        fn = str(f).replace("\\", "/")
        fn = fn[2:] if fn.startswith("./") else fn
        if fn in declared or fn.rsplit("/", 1)[-1] in basenames:
            return True
    return False


def _amend_scope_with_missing_files(plan_obj, failed_ids, missing_files, state,
                                    project_path: str | None = None) -> dict | None:
    """B3-2 外科出口（round38c TwoFactorBindVO 类计划级缺陷）：把 LLM 点名且全 plan
    无 owner 的缺失文件补进失败子任务的 create_files——"给现有子任务补一个 create_files"
    这一最小计划修正动作此前全决策面无合法通道（全量 replan 被守卫拦/降级 retry 治不了
    计划缺口，forensics_B3B4_code.md）。
    防毒校验（scope 毒教训，缺陷4）：相对路径/无 ../有扩展名/不与既有生产者重复；
    每子任务限 1 次（subtask_scope_amend_counts）。全不过校验或配额耗尽 → None。"""
    if plan_obj is None or not missing_files or not failed_ids:
        return None
    _amend_counts = dict(state.get("subtask_scope_amend_counts") or {})
    fid = failed_ids[0]
    if _amend_counts.get(fid, 0) >= 1:
        return None
    _by_id = {s.id: s for s in (getattr(plan_obj, "subtasks", None) or [])}
    st = _by_id.get(fid)
    sc = getattr(st, "scope", None) if st is not None else None
    if sc is None:
        return None
    valid: list[str] = []
    for f in missing_files:
        fn = str(f).replace("\\", "/").strip()
        if fn.startswith("./"):
            fn = fn[2:]
        if (not fn or fn.startswith("/") or ".." in fn.split("/")
                or "." not in fn.rsplit("/", 1)[-1]):
            continue
        if _planned_producers_exist(plan_obj, [fn]):
            continue
        # 对抗复核#5：本地树已存在的文件≠"没人创建"——是同步/seed 问题（B1 面），
        # 补进 create_files 会让 worker 从零重写覆掉基线/上游内容。
        if project_path:
            try:
                from pathlib import Path as _P
                if (_P(project_path) / fn).is_file():
                    logger.warning(
                        "[HANDLE_FAILURE] B3-2 缺失文件 %s 实际已在本地树 → 非计划缺口"
                        "（疑似同步/seed 问题），不补 create_files", fn)
                    continue
            except Exception:  # noqa: BLE001 — 存在性判定失败按缺失继续（原语义）
                pass
        valid.append(fn)
    if not valid:
        return None
    sc.create_files = list(dict.fromkeys(list(sc.create_files or []) + valid))
    _amend_counts[fid] = _amend_counts.get(fid, 0) + 1
    return {"applied": valid, "counts": _amend_counts}


def _apply_scope_objection(plan_obj, subtask_results: dict, failed_ids: list, state) -> dict | None:
    """B4-2：消费 worker 结构化 scope 异议（SCOPE_OBJECTION 行协议 → WorkerOutput.
    scope_objection）。file 在该子任务 create_files 且 suggested 过防毒校验（相对路径/
    无 ../有扩展名）→ 替换条目（与 B3-2 共用 subtask_scope_amend_counts，每子任务≤1）。
    治 18:07 "这可能是一个错误"只能落 notes 散文无人读、8 轮穷举框架类名的死通道
    （forensics_B3B4 缺陷4）。无可应用异议 → None。"""
    if plan_obj is None or not failed_ids:
        return None
    _amend_counts = dict(state.get("subtask_scope_amend_counts") or {})
    _by_id = {s.id: s for s in (getattr(plan_obj, "subtasks", None) or [])}
    applied: dict[str, list] = {}
    for fid in failed_ids:
        obj = getattr(subtask_results.get(fid), "scope_objection", None) or {}
        f = str(obj.get("file") or "").replace("\\", "/").strip()
        s = str(obj.get("suggested") or "").replace("\\", "/").strip()
        if not f or not s or _amend_counts.get(fid, 0) >= 1:
            continue
        st = _by_id.get(fid)
        sc = getattr(st, "scope", None) if st is not None else None
        if sc is None:
            continue
        cf = list(getattr(sc, "create_files", None) or [])
        if f not in cf:
            continue
        if s.startswith("./"):
            s = s[2:]
        if s.startswith("/") or ".." in s.split("/") or "." not in s.rsplit("/", 1)[-1]:
            logger.warning("[HANDLE_FAILURE] B4-2 scope 异议 suggested 未过防毒校验，忽略: %s → %s",
                           f, s)
            continue
        # 对抗复核#7：suggested 撞其他子任务的 create_files/writable=破单写者不变量
        # （合并期两写者冲突）——有 owner 即拒绝应用。
        if _planned_producers_exist(plan_obj, [s]):
            logger.warning("[HANDLE_FAILURE] B4-2 scope 异议 suggested %s 已有生产者，忽略（防双写者）", s)
            continue
        sc.create_files = [s if x == f else x for x in cf]
        _amend_counts[fid] = _amend_counts.get(fid, 0) + 1
        applied[fid] = [f, s]
    if not applied:
        return None
    return {"applied": applied, "counts": _amend_counts}


# R65D-T4：JDK/标准库高频类型（本文件自愈面本就 JVM 专属语义，集合过滤不违栈中立）。
# round65d 毒树第一株：worker 缺 `import java.util.Map` → 编译错里 Map 恰好【没有】
# import 证据（缺的就是 import）→ 旧分支③把它当"自造内部类型"下新建指令 → worker
# 抗命写 SCOPE_OBJECTION 拒工书落盘。名单只作用于【无 import 证据/邻近共现】两路，
# 显式 import 证据（`import <blocked>.Map`）仍放行——证据赢过名单。
_JDK_COMMON_TYPES = frozenset({
    # java.lang（隐式导入但 location 报告仍可能出现）/ java.util
    "String", "Integer", "Long", "Double", "Float", "Boolean", "Byte", "Short",
    "Character", "Object", "Class", "Void", "Number", "Math", "Thread", "Runnable",
    "Exception", "RuntimeException", "Error", "Throwable", "Iterable", "Comparable",
    "Map", "HashMap", "LinkedHashMap", "TreeMap", "ConcurrentHashMap",
    "List", "ArrayList", "LinkedList", "CopyOnWriteArrayList",
    "Set", "HashSet", "LinkedHashSet", "TreeSet", "Collection", "Collections",
    "Arrays", "Iterator", "Optional", "Objects", "UUID", "Random", "Scanner",
    "Queue", "Deque", "ArrayDeque", "PriorityQueue", "Stack", "Vector",
    "Date", "Calendar", "TimeZone", "Locale", "Properties", "Base64",
    # java.time / java.math / java.io / java.nio
    "LocalDate", "LocalDateTime", "LocalTime", "Instant", "Duration", "Period",
    "ZonedDateTime", "OffsetDateTime", "DateTimeFormatter", "ChronoUnit",
    "BigDecimal", "BigInteger",
    "File", "InputStream", "OutputStream", "Reader", "Writer", "IOException",
    "BufferedReader", "BufferedWriter", "InputStreamReader", "OutputStreamWriter",
    "Path", "Paths", "Files", "Charset", "StandardCharsets",
    # java.util.function / stream / concurrent
    "Function", "Supplier", "Consumer", "Predicate", "BiFunction", "BiConsumer",
    "Stream", "Collectors", "IntStream", "LongStream",
    "CompletableFuture", "Future", "Executor", "ExecutorService", "Executors",
    "TimeUnit", "CountDownLatch", "AtomicInteger", "AtomicLong", "AtomicBoolean",
    "AtomicReference", "ReentrantLock", "Semaphore",
    # 序列化/常用注解宿主
    "Serializable", "Cloneable", "AutoCloseable", "StringBuilder", "StringBuffer",
})


def _import_evidenced_classes(build_output: str) -> dict[str, set[str]]:
    """import 证据单一事实源（猎手 LOW：双份正则必漂移）：{类名: {import 声明的包}}。"""
    import re
    out: dict[str, set[str]] = {}
    for m in re.finditer(r"import\s+(?:static\s+)?([A-Za-z_][\w.]*)\.([A-Z]\w*)",
                         build_output or ""):
        out.setdefault(m.group(2), set()).add(m.group(1))
    return out


def _stdlib_missing_classes(build_output: str) -> set[str]:
    """R65D-T4：编译错里【无任何 import 证据】且名列 JDK 常见类型的缺失类——诊断=缺
    import 语句，不是缺内部类型。供自愈调用方改道注入补 import 指导。
    （保守 ANY-证据口径：只要全文有该类的 import——无论指向哪个包——都不下"补 JDK
    import"结论；三方 classpath 缺失等形态交常规阶梯，绝不给错方向指导。）"""
    import re
    evidenced = _import_evidenced_classes(build_output)
    return {c for c in re.findall(r"symbol:\s*class\s+([A-Z]\w*)", build_output or "")
            if c in _JDK_COMMON_TYPES and not evidenced.get(c)}


def _derive_missing_type_files(scope_files: list, blocked_pkgs: list, build_output: str) -> list:
    """round36 P0 自愈：从子任务声明文件推源根 + blocked 内部包 + 编译错误里的缺失类名，
    推出【应新建的类型文件路径】。仅服务 internal_pkg_not_built（本就 JVM 包语义）；推不出→空
    （调用方回落连坐放弃，绝不空烧自愈预算）。★把文件加进 create_files（而非 allow_any）：既让
    scope 闸门放行 worker 新建，又使 _writable_files 把它拉回本地（allow_any 在非空声明 scope 下
    拉不回=复核 HIGH#2），且放弃时 #7 purge 也认得它★。"""
    import re
    _MARKERS = ("/src/main/java/", "/src/test/java/",
                "/src/main/kotlin/", "/src/test/kotlin/")
    srcroot = None
    for f in scope_files:
        fn = str(f).replace("\\", "/")
        for mk in _MARKERS:
            i = fn.find(mk)
            if i >= 0:
                srcroot = fn[:i + len(mk)]
                break
        if srcroot:
            break
    if not srcroot or not blocked_pkgs:
        return []
    # B4-1（round38c scope 毒治本，forensics_B3B4 缺陷4）：旧推导把 build_output 里
    # 【所有】`symbol: class X`（含缺 classpath 依赖时的框架类，如 SqlSessionTemplate）
    # 与 blocked_pkgs 做全叉积种进项目包路径 → 框架类名进 create_files 锁死 worker
    # 在"public 类名=文件名"硬约束里穷举 8 轮。改为【证据配对】三层（不写死框架黑名单，
    # 通用铁律）：
    # ①import 证据：`import <pkg>.<Class>` 明示类的真属包——属 blocked 包则配对；属
    #   非 blocked 包（框架/三方，如 org.mybatis.spring）则该类整体出局；
    # ②symbol/location 邻近共现：错误块内提到 blocked 包 → 配对；
    # ③无任何 import 证据的缺失类（未导入的同包引用/worker 自造类型，javac 只给
    #   `location: var …`）→ 保留旧回退配对全部 blocked 包——round36 self-heal 的
    #   真自造引用恰是这种形态（全量回归 2 用例坐实），误配残余由 B4-2 异议通道兜底。
    txt = build_output or ""
    ext = ".kt" if "kotlin" in srcroot else ".java"
    evidenced = _import_evidenced_classes(txt)   # 单一事实源（猎手 LOW）
    blocked = [str(p).strip() for p in blocked_pkgs if str(p).strip()]
    pairs: set[tuple[str, str]] = set()

    def _stdlib_shadow(cls: str, pkg: str) -> bool:
        # R65D-T4：JDK 常见类型且【对这个 blocked 包】无 import 证据=缺 import 而非
        # 缺内部类型——绝不下"新建同名类型"毒指令（Map.java 拒工书死型）。
        # ★复核 HIGH（带复现）整改：证据按【类名×包】配对判定——别的 blocked 包一条
        # 断裂 import（如 `import com.x.dto.Date;` 的 package-does-not-exist 回显）
        # 绝不全局解除同名类在其它包上的遮蔽（文本级 ANY-证据可被巧合同名击穿）。
        return cls in _JDK_COMMON_TYPES and pkg not in evidenced.get(cls, set())

    for p in blocked:
        _pq = re.escape(p)
        for m in re.finditer(
                r"symbol:\s*class\s+([A-Z]\w*)(?:[^\n]*\n){0,3}?[^\n]*" + _pq, txt):
            if not _stdlib_shadow(m.group(1), p):
                pairs.add((p, m.group(1)))
    for cls, pkgs in evidenced.items():
        for p in blocked:
            if p in pkgs:
                pairs.add((p, cls))   # 显式 import 证据赢过名单（真自定义同名类型）
    paired_cls = {c for _, c in pairs}
    for cls in set(re.findall(r"symbol:\s*class\s+([A-Z]\w*)", txt)):
        if cls not in paired_cls and not evidenced.get(cls) \
                and cls not in _JDK_COMMON_TYPES:
            for p in blocked:
                pairs.add((p, cls))
    if not pairs:
        return []
    return sorted({f"{srcroot}{p.replace('.', '/')}/{c}{ext}" for p, c in pairs})


async def _handle_failure_impl(state: BrainState) -> dict:
    """HANDLE_FAILURE 核心逻辑（按 strategy 分支：retry / retry_alternate / replan / escalate）。

    输入: failed_subtask_ids, subtask_results, plan, merge_conflicts
    注意：就地改 plan 的持久化由外层 handle_failure 包装统一回传 plan 保证（brain#3）。
    """
    # round26 god-file 治理：_get_brain_llm/_get_project_path 仍定义在 __init__（被其它节点用 +
    # 被测试 patch("swarm.brain.nodes._get_brain_llm")）。本函数外置到 failure.py 后用函数内 lazy
    # import 从 nodes 命名空间取，保 patch 生效、且不 eager import __init__（防 A6 环）。
    from swarm.brain.nodes import _get_brain_llm, _get_project_path
    failed_ids = list(state.get("failed_subtask_ids", []))
    subtask_results = dict(state.get("subtask_results", {}))
    plan_obj = state.get("plan")
    strategy = "retry"

    logger.info(f"[HANDLE_FAILURE] 处理 {len(failed_ids)} 个失败子任务")

    if state.get("verification_failure") == "runtime_smoke":
        # ── S1-6 运行时失败证据回灌（替换 S1-4 占位）──
        # 专类分支：运行时冒烟失败（含 S1-5 migration_failed 同族——同经 verification_failure=
        # "runtime_smoke" 进入，details 含 SQL/migration 错误输出，证据面同源消费）绝不落下方
        # "l2" 分支：l2 归因链建立在编译输出格式上，对运行时启动日志无效（8bec098 专类教训）。
        # 有界性双闸（绝不给 runtime 单开无界通道）：
        #   ① replan_count 与 L2 共用【同一】熔断计数器——定向/replan 每轮自增，超限 escalate；
        #   ② 定向恢复按子任务配额 targeted_recovery_counts（round29 遗漏项#2 先例，与 A2 缺
        #      依赖/序修复阶梯共用表），配额耗尽 escalate（镜像 "l3" 人工终点，保底不无界）。
        # 环境类（env_missing/timeout/inconclusive）在 verify_runtime 即 skipped+degraded，
        # 不产生 verification_failure，永不进本分支（环境类不进失败通道）。
        _rt_details = dict(state.get("runtime_smoke_details") or {})
        _rt_class = str(_rt_details.get("classification") or "code_error")
        _rt_replan = state.get("replan_count", 0) + 1
        _rt_max = get_config().model.max_retries
        if _rt_replan > _rt_max:
            logger.warning(
                "[HANDLE_FAILURE] 运行时冒烟失败(%s)且 replan 已达上限(%d 次) → 升级人工审核",
                _rt_class, _rt_max,
            )
            return {
                "failure_strategy": "escalate",
                "failure_escalated": True,
                "failed_subtask_ids": failed_ids,
                "verification_failure": None,
                "runtime_smoke_passed": False,
                "replan_count": _rt_replan,
            }
        # 归因：启动日志/迁移错误里的源文件引用 → scope 写权反查写者子任务
        #（复用 attribute_l2_failure 路径匹配机制，栈无关；见 shared.attribute_runtime_failure）
        _rt_attributed = attribute_runtime_failure(plan_obj, _rt_details, subtask_results)
        # ── T4 无进展 plateau 检测（ECC §D "plateau" 半环；max-iteration 半环=上方 replan_count）──
        # 缺口（取证坐实）：既有轮次计数只按【次数】封顶，从不比对【连续两轮是否同一失败形态】。一个
        # 反复以完全相同 classification + 归因子任务集失败的冒烟，会白烧满 replan 预算才停（token 黑洞）。
        # 签名=classification|归因子任务集(排序)；跨轮与上一轮 handle_failure 存的签名比对，一致=无进展。
        # 默认仅【观测留痕】(控制流不变，轮次计数仍兜底，绝不误伤"隐性收敛中"的修复)；strict opt-in
        # (SWARM_RUNTIME_SMOKE_PLATEAU_STRICT=1) 才短路提前 escalate（省无谓重试）。镜像 T3 观测/strict 二态。
        _rt_signature = f"{_rt_class}|{','.join(sorted(_rt_attributed or []))}"
        _rt_prev_sig = str(state.get("runtime_smoke_last_signature") or "")
        if _rt_prev_sig and _rt_signature == _rt_prev_sig:
            # 复核 A（已知粒度权衡·刻意如此）：签名粒度=classification|归因子任务集，【不含】错误
            # 文本指纹。故"同一子任务修好 bug1 又暴露同分类 bug2"会同签名=被判 plateau。之所以不掺
            # log_tail 指纹：启动日志含时间戳/pid/路径抖动，掺入会让签名【永不复现】→ 检测器变哑
            # （比误判更坏的失效模式）。strict 下此权衡的最坏后果=提前【一轮】升级【人工审核】
            #（非静默判过、绝不发坏码），人工可 REVISE 续修，可恢复；且默认 observe 永不误伤——
            # 仅显式 opt-in 的操作者以"省重试费"换"偶发早一轮转人工"。故记为文档化权衡而非缺陷。
            _rt_plateau_strict = os.environ.get(
                "SWARM_RUNTIME_SMOKE_PLATEAU_STRICT", "false"
            ).lower() in ("true", "1", "yes", "on")
            if _rt_plateau_strict:
                logger.warning(
                    "[HANDLE_FAILURE] 运行时冒烟连续两轮同签名(%s)无进展 → strict 短路升级人工审核"
                    "（省无谓重试烧费；默认关，SWARM_RUNTIME_SMOKE_PLATEAU_STRICT=1 开）",
                    _rt_signature,
                )
                return {
                    "failure_strategy": "escalate",
                    "failure_escalated": True,
                    "failed_subtask_ids": _rt_attributed or failed_ids,
                    "verification_failure": None,
                    "runtime_smoke_passed": False,
                    "replan_count": _rt_replan,
                    "runtime_smoke_last_signature": _rt_signature,
                    "degraded_reasons": [f"runtime_smoke_plateau:{_rt_class}"],
                }
            logger.warning(
                "[HANDLE_FAILURE] 运行时冒烟连续两轮同签名(%s)无进展（上一轮定向/replan 未改变失败"
                "形态）→ 观测留痕，控制流不变（轮次计数兜底；strict 模式可短路提前 escalate）",
                _rt_signature,
            )
        if _rt_attributed:
            _rt_trc = dict(state.get("targeted_recovery_counts") or {})
            _rt_eligible = [fid for fid in _rt_attributed if _rt_trc.get(fid, 0) < _rt_max]
            # hunter：配额【部分】耗尽时，被排除的归因子任务绝不静默丢弃——WARN 留痕
            # （列出 fid 与已耗配额），行为不变（仅重派 eligible 者）。全员耗尽走下方 escalate。
            _rt_excluded = [fid for fid in _rt_attributed if fid not in _rt_eligible]
            if _rt_excluded and _rt_eligible:
                logger.warning(
                    "[HANDLE_FAILURE] 运行时冒烟失败归因到 %s，其中 %s 因定向恢复配额耗尽被本轮"
                    "排除(已耗 %s / 上限 %d)，仅重派 %s",
                    _rt_attributed, _rt_excluded,
                    {fid: _rt_trc.get(fid, 0) for fid in _rt_excluded}, _rt_max, _rt_eligible,
                )
            if not _rt_eligible:
                logger.warning(
                    "[HANDLE_FAILURE] 运行时冒烟失败归因到 %s 但各自定向恢复配额均已耗尽(上限 %d)"
                    " → 升级人工审核（绝不无限定向重做）", _rt_attributed, _rt_max,
                )
                return {
                    "failure_strategy": "escalate",
                    "failure_escalated": True,
                    "failed_subtask_ids": _rt_attributed,
                    "verification_failure": None,
                    "runtime_smoke_passed": False,
                    "replan_count": _rt_replan,
                }
            # 证据注入：走既有 retry_guidance 通道（A4 round11，worker/prompts.py 渲染为
            # 硬约束块），重派 worker 直接看到启动日志证据 + 机制说明（运行时失败非编译失败）。
            # S2-6：classification=acceptance_failed 专类定性——应用【已启动、探活已过】，
            # 失败的是对运行中应用的 HTTP 验收断言（证据=逐条断言 verdict，含请求方法/路径/
            # 期待与实得），沿用"启动失败"文案会误导 worker 去查启动面。证据同源
            # runtime_failure_evidence（acceptance 前缀键族，shared.py F3 同款契约）。
            _rt_evidence = runtime_failure_evidence(_rt_details)
            if _rt_class == "acceptance_failed":
                _rt_guidance = (
                    "验收断言失败（应用已启动但接口行为不符预期）：本子任务产出已通过编译、"
                    "L2 集成验证与启动探活，但对运行中应用的验收断言未通过"
                    f"（classification={_rt_class}）。请依据下方逐条断言 verdict 证据"
                    "（含请求方法/路径、期待与实得响应）修复接口行为，勿无谓改动与证据"
                    "无关的编译/启动面。\n断言失败证据：\n" + _rt_evidence
                )[:1600]
            else:
                _rt_guidance = (
                    "运行时启动失败（非编译失败）：本子任务产出已通过编译与 L2 集成验证，但应用在"
                    f"启动/探活冒烟阶段失败（classification={_rt_class}）。请依据下方启动期证据修复"
                    "启动失败根因，勿无谓改动与证据无关的编译面。\n启动失败证据：\n" + _rt_evidence
                )[:1600]
            _rt_by_id = {s.id: s for s in getattr(plan_obj, "subtasks", []) or []}
            for fid in _rt_eligible:
                _rt_st = _rt_by_id.get(fid)
                if _rt_st is not None:
                    _rt_st.retry_guidance = _rt_guidance
            dispatch_remaining = list(state.get("dispatch_remaining", []))
            _rt_rc = dict(state.get("subtask_retry_counts", {}))
            for fid in _rt_eligible:
                subtask_results.pop(fid, None)
                if fid not in dispatch_remaining:
                    dispatch_remaining.append(fid)
                # 运行时失败非其 L1 能力失败（各自 L1 已过）→ 不烧能力配额；
                # 循环边界由 ①replan_count + ②targeted_recovery_counts 双闸保证。
                _rt_rc[fid] = 0
                _rt_trc[fid] = _rt_trc.get(fid, 0) + 1
            logger.info(
                "[HANDLE_FAILURE] 运行时冒烟失败(%s)定向恢复（第 %d/%d 次，按子任务配额 %s）："
                "归因到写者子任务 %s，注入启动日志证据重派，保留 %d 个成功兄弟，不全量 replan",
                _rt_class, _rt_replan, _rt_max, {k: _rt_trc[k] for k in _rt_eligible},
                _rt_eligible, len(subtask_results),
            )
            return {
                "plan": plan_obj,
                "subtask_results": subtask_results,
                "dispatch_remaining": dispatch_remaining,
                "failed_subtask_ids": [],
                "failure_strategy": "retry",
                "failure_escalated": False,
                "verification_failure": None,
                "runtime_smoke_passed": False,
                "replan_count": _rt_replan,
                "subtask_retry_counts": _rt_rc,
                "targeted_recovery_count": state.get("targeted_recovery_count", 0) + 1,  # 遥测保留
                "targeted_recovery_counts": _rt_trc,
                "runtime_smoke_last_signature": _rt_signature,  # T4 跨轮 plateau 比对基准
            }
        # 归因不出 → 退 replan 阶梯（共用 replan_count，上面已判上限，绝不另起无界通道）
        logger.info(
            "[HANDLE_FAILURE] 运行时冒烟失败(%s)证据归因不出写者子任务 — 触发 replan (第 %d/%d 次)",
            _rt_class, _rt_replan, _rt_max,
        )
        return {
            "failure_strategy": "replan",
            "failure_escalated": False,
            "failed_subtask_ids": [],
            "verification_failure": None,
            "runtime_smoke_passed": False,
            "replan_count": _rt_replan,
            "runtime_smoke_last_signature": _rt_signature,  # T4 跨轮 plateau 比对基准
            # A3（2026-07-09 登记册）：与 L2/能力 replan 出口对称——runtime replan 是新规划
            # 目标，给全新 plan 校验重试预算（round36 #9 同理），并清旧覆盖 issue 防污染新规划。
            # 原漏此二键 → 新计划继承已耗尽的 plan_retry_count，首次校验失败即 CONFIRM REJECT。
            "plan_retry_count": 0,
            "plan_validation_prev_structural": {},  # R64-T3 猎手 F1：新周期必须清结构签名（防相邻巧合误熔断）
            "plan_validation_feedback": "",
        }

    if state.get("verification_failure") == "l2":
        # H2 修复：L2 失败 replan 也要走 replan_count 计数/上限，否则绕过熔断可无限重规划
        # （原直接 return replan 不自增计数，仅靠 recursion_limit=50 兜底，违背承诺）。
        _l2_replan = state.get("replan_count", 0) + 1
        _l2_max = get_config().model.max_retries
        if _l2_replan > _l2_max:
            logger.warning(
                "[HANDLE_FAILURE] L2 集成验证失败且 replan 已达上限(%d 次) → 升级人工审核",
                _l2_max,
            )
            return {
                "failure_strategy": "escalate",
                "failed_subtask_ids": failed_ids,
                "failure_escalated": True,
                "verification_failure": None,
                "l2_passed": False,
                "replan_count": _l2_replan,
                # D12（2026-07-09 登记册）：l2_targeted 条件写（verify 归因出才 True），出口须
                # 对称清空——escalate 后人工放行续跑不得带脏定向标记。
                "l2_targeted": False,
            }
        # TD2606-B8：L2 失败已归因到具体子任务（verify_l2 设 l2_targeted）+ 存在成功兄弟
        # → 定向恢复：只重做归因到的子任务、保留成功成果，不全量推倒重来。replan_count 仍
        # 自增（与全量 replan 共用熔断，上面已判上限），杜绝定向重试→L2→定向重试无限循环。
        if state.get("l2_targeted") and failed_ids:
            succeeded_siblings = [
                sid for sid, out in subtask_results.items()
                if sid not in failed_ids and l1_passed(out)
            ]
            if succeeded_siblings:
                # 治本 replan 死循环·E：内部包/上游模块未就绪类失败【不清零重试计数】——它非 L2 偶发，
                # 是结构性不可满足；清零会让 _deepest 永不达 give_up 阈值，与 BLOCKED→replan 合谋成无界循环。
                # 先于 pop 捕获其 pipeline_blocked。
                _blocked_now = {fid for fid in failed_ids
                                if _det_of(subtask_results.get(fid)).get("pipeline_blocked")
                                in _INTERNAL_BLOCKED_KINDS}
                # D3b-①（round38c 主题D 复核 CONFIRMED）：L2 定向恢复此前【全程无证据
                # 注入】——重派 worker 同 prompt 同条件大概率复产同缺陷、盲烧 replan 预算。
                # verify_l2 可在 l2_details.retry_guidance 携带定向指引（如 stub 指纹红线
                # 的"禁止假实现桩"），经既有 retry_guidance 通道（A4 round11，worker/
                # prompts.py 渲染为硬约束块）注入被归因子任务；无指引时行为不变。
                _l2_guidance = (state.get("l2_details") or {}).get("retry_guidance") or ""
                if _l2_guidance:
                    _l2_by_id = {s.id: s for s in getattr(plan_obj, "subtasks", []) or []}
                    for fid in failed_ids:
                        _l2_st = _l2_by_id.get(fid)
                        if _l2_st is not None:
                            _l2_st.retry_guidance = _l2_guidance[:1600]
                dispatch_remaining = list(state.get("dispatch_remaining", []))
                for fid in failed_ids:
                    subtask_results.pop(fid, None)
                    if fid not in dispatch_remaining:
                        dispatch_remaining.append(fid)
                # L2 集成失败非这些子任务的 capability 失败（它们各自 L1 已过）→ 重置其重试计数，
                # 不无谓烧 capability 配额；循环边界由 replan_count 熔断保证。（结构性内部阻断除外，见上）
                _rc = dict(state.get("subtask_retry_counts", {}))
                for fid in failed_ids:
                    if fid not in _blocked_now:
                        _rc[fid] = 0
                logger.info(
                    "[HANDLE_FAILURE] L2 定向恢复（第 %d/%d 次）：集成失败归因到 %s，"
                    "保留 %d 个成功兄弟 %s，仅重做归因子任务，不全量 replan",
                    _l2_replan, _l2_max, failed_ids, len(succeeded_siblings), succeeded_siblings,
                )
                return {
                    "subtask_results": subtask_results,
                    "dispatch_remaining": dispatch_remaining,
                    "failed_subtask_ids": [],
                    "failure_strategy": "retry",
                    "failure_escalated": False,  # 批4c：非 escalate 决策清历史粘滞标记（取证 CONFIRMED，见 DEVLOG）
                    "verification_failure": None,
                    "l2_passed": False,
                    "l2_targeted": False,
                    "replan_count": _l2_replan,
                    "subtask_retry_counts": _rc,
                    }

        logger.info("[HANDLE_FAILURE] L2 集成验证失败 — 触发 replan (第 %d/%d 次)",
                    _l2_replan, _l2_max)
        return {
            "failure_strategy": "replan",
            "failure_escalated": False,  # 批4c：非 escalate 决策清历史粘滞标记（取证 CONFIRMED，见 DEVLOG）
            "failed_subtask_ids": [],
            "verification_failure": None,
            "l2_passed": False,
            "replan_count": _l2_replan,
            # round36 #9 治本：L2 触发的 replan 是【新规划目标】(修 L2 集成缺陷)，须给它一份【全新的
            # plan 校验重试预算】——否则复用早前覆盖重试已耗到 2 的 plan_retry_count → 只剩 1 轮即
            # 3/3 耗尽 CONFIRM reject（round36 实证 replan 从没派发就死在规划）。replan 总次数另由
            # replan_count(独立熔断,默认 2)封顶，故此处清零安全、不会无界。
            "plan_retry_count": 0,
            "plan_validation_prev_structural": {},  # R64-T3 猎手 F1：新周期必须清结构签名（防相邻巧合误熔断）
            "plan_validation_feedback": "",  # 同清跨轮校验粘滞，防旧覆盖 issue 污染新规划
            # D12（2026-07-09 登记册）：全量 replan 出口清 l2_targeted 粘滞——否则下一轮 L2
            # 归因不出（_l2_failure_state 不 emit 该键）时，粘滞 True 把全员连坐误判成"已归因定向"。
            "l2_targeted": False,
        }

    if state.get("verification_failure") == "l3":
        logger.info("[HANDLE_FAILURE] L3 预发/CI 验证失败 — 升级人工审核")
        return {
            "failure_strategy": "escalate",
            "failed_subtask_ids": [],
            "verification_failure": None,
            "l3_passed": False,
        }

    if state.get("verification_failure") == "contract":
        # audit A-P1-03：契约偏离重试必须计数+设上限，否则与能力分支不同——
        # 可无限 retry→contract→retry 至 recursion_limit。复用 subtask_retry_counts
        # 与 max_retries 上限（与 capability/SIMPLE 路径一致），超限升级人工。
        failed = list(state.get("failed_subtask_ids", []))
        if not failed:
            # A1 入口守卫（round38c P0）：旧回填 `or subtask_results.keys()` 是第二个
            # 全员清零源——verify 归因空时把全体完成态列入重跑。空 failed=归因启发式
            # 失败，升级人工携机读账（诚实 PARTIAL），完成态一个不动。
            logger.warning(
                "[HANDLE_FAILURE] 契约失败无归因 owner → 升级人工，绝不全员清零；"
                "unattributed=%s",
                (state.get("l2_details") or {}).get("contract_unattributed"))
            return {
                "failure_escalated": True,
                "failure_strategy": "escalate",
                "failed_subtask_ids": [],
                "verification_failure": None,
            }
        # P1-4：旧 `failed[:3]` 硬截断 → 第 4 个起的失败子任务【静默永不重试】（每轮都取同样前 3 个）。
        # 移除截断：每个子任务由 subtask_retry_counts + max_retries 各自封顶、总量由 recursion_limit
        # 兜底，无需人为截断；截断只会漏修。>3 时记 warning 保留可观测（契约失败连坐面较宽，可见）。
        if len(failed) > 3:
            logger.warning("[HANDLE_FAILURE] 契约失败连坐 %d 个子任务重试（不再静默截断前 3）: %s",
                           len(failed), failed[:8])
        _max_retries = get_config().model.max_retries  # 默认 2
        # D13（阶段6，登记册 §五）：契约重试改独立表 contract_retry_counts——旧实现
        # 复用 subtask_retry_counts（capability 配额），契约反复=交叉挤兑个体能力配额
        # （契约是横切集成面失败，非个体胜任性问题）。
        _retry_counts = dict(state.get("contract_retry_counts", {}))
        _next_counts = {fid: _retry_counts.get(fid, 0) + 1 for fid in failed}
        _deepest = max(_next_counts.values(), default=0)
        if _deepest > _max_retries + 1:
            logger.warning(
                "[HANDLE_FAILURE] 契约偏离重试达上限(%d+alternate)，升级人工: %s",
                _max_retries, failed,
            )
            return {
                "failure_escalated": True,
                "failure_strategy": "escalate",
                "failed_subtask_ids": failed,
                "verification_failure": None,
                "contract_retry_counts": {**_retry_counts, **_next_counts},
            }
        logger.info("[HANDLE_FAILURE] 契约偏离 — 重试相关子任务(第 %d 次)", _deepest)
        # 治本 D24：与其它 retry 分支对称——pop 相关 subtask_results 并加回 dispatch_remaining，
        # 否则该分支只自增计数、既不清结果也不重排队 → 下轮 dispatch 见这些 id 仍在 completed →
        # to_dispatch 空 → 早退 → monitor 读残留 failed 再进 handle_failure，此时 verification_failure
        # 已清 None → 落常规能力阶梯，把 L1 全过的输出误诊断 pop 全部全量重跑。
        _dispatch_remaining = list(state.get("dispatch_remaining", []))
        for fid in failed:
            subtask_results.pop(fid, None)
            if fid not in _dispatch_remaining:
                _dispatch_remaining.append(fid)
        _ret = {
            "subtask_results": subtask_results,
            "dispatch_remaining": _dispatch_remaining,
            "failure_strategy": "retry",
            "failure_escalated": False,  # 批4c：非 escalate 决策清历史粘滞标记（取证 CONFIRMED，见 DEVLOG）
            "contract_retry_counts": {**_retry_counts, **_next_counts},  # D13 独立表
            "failed_subtask_ids": [],
            "verification_failure": None,
        }
        # A1 定向复活（对抗复核#4 补强为传递闭包）：D5 全 plan 归因可命中被弃/打桩
        # owner（round38c 引擎符号真 owner 全在被弃 14 个里）。两点缺一不可：
        # ①派发面过滤 abandoned|give_up 双集（dispatch.py:280-281）——只摘 abandoned
        #  则阶梯三打桩 owner 复活后永不可派；②owner 的传递上游若仍被弃则依赖永不
        #  就绪——复活退化为 #R13-4 熔断前的有界空转（每轮白烧一次全 reactor 编译）。
        # 故复活集=归因 owner + 其传递 depends_on 中处于双集且未完成者，一并摘出双集
        # 并入重派队列（上游复活者不入 contract_retry_counts——非归因对象不受罚）。
        _abandoned = set(state.get("abandoned_subtask_ids") or [])
        _give_up = set(state.get("give_up_isolated_ids") or [])
        _dead = _abandoned | _give_up
        _revive = {fid for fid in failed if fid in _dead}
        if _revive and plan_obj is not None and getattr(plan_obj, "subtasks", None):
            _by_id = {st.id: st for st in plan_obj.subtasks}
            _walk = list(_revive)
            while _walk:
                _st = _by_id.get(_walk.pop())
                for _dep in (getattr(_st, "depends_on", None) or []) if _st else []:
                    if _dep in _dead and _dep not in _revive and _dep not in subtask_results:
                        _revive.add(_dep)
                        _walk.append(_dep)
        if _revive:
            logger.info("[HANDLE_FAILURE] 契约归因命中被弃/打桩 owner → 定向复活传递闭包 %s"
                        "（摘出 abandoned/give_up 双集）", sorted(_revive))
            for fid in sorted(_revive):
                if fid not in _dispatch_remaining:
                    _dispatch_remaining.append(fid)
            if _abandoned & _revive:
                _ret["abandoned_subtask_ids"] = sorted(_abandoned - _revive)
            if _give_up & _revive:
                _ret["give_up_isolated_ids"] = sorted(_give_up - _revive)
        return _ret

    if effective_complexity(state) == Complexity.SIMPLE:  # 修复 12.3：澄清后定级优先
        # 确定性重试上限（与复杂路径一致，防止 SIMPLE 任务无限重试死循环）。
        # 历史 bug：SIMPLE 分支原先无条件 retry，遇到"L1 通过但 diff 收集为空"
        # (如重试时本地文件已被上一轮改过→difflib 基线已含变更→diff=空→被判失败)
        # 会无限循环。这里引入与复杂路径相同的 subtask_retry_counts 硬上限。
        max_retries = get_config().model.max_retries  # 默认 2
        retry_counts = dict(state.get("subtask_retry_counts", {}))
        next_counts = {fid: retry_counts.get(fid, 0) + 1 for fid in failed_ids}
        deepest = max(next_counts.values(), default=0)
        if deepest > max_retries + 1:
            logger.warning(
                "[HANDLE_FAILURE] SIMPLE 子任务重试达上限(%d+alternate)，升级人工: %s",
                max_retries, failed_ids,
            )
            return {
                "failure_escalated": True,
                "failure_strategy": "escalate",
                "l2_passed": False,
                "failed_subtask_ids": failed_ids,
                "subtask_retry_counts": {**retry_counts, **next_counts},
            }
        dispatch_remaining = list(state.get("dispatch_remaining", []))
        for fid in failed_ids:
            subtask_results.pop(fid, None)
            if fid not in dispatch_remaining:
                dispatch_remaining.append(fid)
        forced_alternate = deepest > max_retries
        # R65TR-T4⑪：换备宣称接 router 真相（同 #76 B2 阶梯主宣称点）——SIMPLE 也走同一
        # dispatch 节点的 _has_hetero_alternate 判据，无异构备选时实派仍是同模型+加步数，
        # 谎称"换备选"与实派永久不符。判据异常保守按"无备选"（诚实优先）。
        _alt_txt = ""
        if forced_alternate:
            try:
                from swarm.brain.nodes.dispatch import _has_hetero_alternate
                _by_id_s = {st.id: st for st in (getattr(plan_obj, "subtasks", None) or [])}
                _no_alt_s = [fid for fid in failed_ids if not _has_hetero_alternate(
                    getattr(_by_id_s.get(fid), "difficulty", None))]
            except Exception:  # noqa: BLE001
                _no_alt_s = list(failed_ids)
            _alt_txt = ("，无异构备选→实派同模型+加步数" if _no_alt_s else "，换备选模型")
        logger.info(
            "[HANDLE_FAILURE] SIMPLE 快速路径 — 重试失败子任务(第 %d 次%s)",
            deepest, _alt_txt,
        )
        return {
            "subtask_results": subtask_results,
            "dispatch_remaining": dispatch_remaining,
            "failed_subtask_ids": [],
            "failure_strategy": "retry_alternate" if forced_alternate else "retry",
            "failure_escalated": False,  # 批4c：非 escalate 决策清历史粘滞标记（取证 CONFIRMED，见 DEVLOG）
            "subtask_use_alternate": _alt_map_update(state, failed_ids, forced_alternate),
            "subtask_retry_counts": {**retry_counts, **next_counts},
        }

    # ── 治本·replan 无界循环：上游已被永久放弃 → 下游不可恢复 → 直接连坐放弃（先于超时/LLM/一切重试）──
    # 机制(round12 实证 churn 3h 才被人工取消)：阶梯三给某 upstream 打桩/revert 放弃后，依赖它的下游
    # 会永久 BLOCKED(upstream_module_broken/internal_pkg_not_built，上游永不落地)。若仍走 LLM→replan
    # 或退避重试 → BLOCKED→replan→守卫降级 retry→重派→BLOCKED 无界循环、且阻断任务级 MERGE=阻断交付。
    # 此处在任何重试/replan 前拦截：把"依赖已放弃上游"的下游一并放弃(传递闭包)，run 自终 PARTIAL。
    # 下游归属用【运行时 blocked_on 包/模块→生产者子任务】(跨模块 import 的 depends_on 不可靠) ∪ depends_on。
    # C9（阶段4）：本轮为「合法跨模块等待」补的动态依赖边 {消费者: [pending 生产者]}——
    # 非空时各 return 路径须回写 plan（in-place 补边后 LangGraph 需要显式 emit 才持久化）。
    _c9_edges: dict[str, list[str]] = {}
    if plan_obj is not None:
        _proj_path = _get_project_path(state.get("project_id") or "")
        _by_id = {s.id: s for s in plan_obj.subtasks}
        # #10 治本所需：全局 settled 生产者判据的两个集合。
        _completed_ok = {sid for sid, out in subtask_results.items()
                         if sid not in failed_ids and l1_passed(out)}
        # R65C-T2 修①：【stub 模式】的 give-up ≠ 死上游——阶梯三给被依赖的失败子任务
        # 打了可编译桩（其承诺就是"下游可编译，不连坐"），把它仍计入不可满足集会让
        # 下游一次 0.8s 的 BLOCKED 探针经 _prod_hit 引爆全计划传递闭包连坐
        # （round65c 实锤：st-1-2 探针 → 102/107 被弃 → 假«全部完成» → L2 拦下假交付）。
        # ★复核 CRITICAL 整改★：豁免判据=give_up_mode=="stub"（settled-with-product），
        # 绝不用裸 l1_passed——revert 模式同样写 l1_passed 占位但产物已剥离（round12/13
        # 真死上游必须照旧连坐，两只既有回归锁实证）；abandoned 集不豁免
        # （R51-1：完成者本就不该进 abandoned）。
        from swarm.brain.nodes.shared import l1_details_of as _l1d
        _stubbed_ok = {
            sid for sid in (state.get("give_up_isolated_ids") or [])
            if l1_passed(subtask_results.get(sid))
            and (_l1d(subtask_results.get(sid)) or {}).get("give_up_mode") == "stub"
        }
        _unsat = ((set(state.get("give_up_isolated_ids") or [])
                   | set(state.get("abandoned_subtask_ids") or []))
                  - _stubbed_ok)
        _pending_now = set(state.get("dispatch_remaining") or []) | set(failed_ids)
        _unrecoverable: set[str] = set()
        # round36 P0 治本：区分两类"阻断在无产物的内部包"——(1)【真死上游】有生产者但已放弃/
        # 依赖已放弃上游(dep_hit/prod_hit) → 连坐放弃正确；(2)【worker 自造引用】完全无生产者
        # (_prods 空、非 dep_hit)=消费者自己在编码时引用了一个全场没人生产的类型(round36 实证：
        # st-12-1 引用 TwoFactorSetupVO，全计划无 owner→连坐炸 62/64)。第(2)类不该直接连坐放弃——
        # 那类型本就该由消费者自己在本模块建出，先给一次 scope 自愈(allow_any)+提示重试机会。
        _selfheal: set[str] = set()
        # T3（round63 死锁本体）：阻断在【基线模块】（git HEAD 自带、plan 无任何生产者）的失败集。
        # 逐 fid 判定——绝不做批级判据（round63 缺口3：B2 的 _all_blocked 被混批搭车的超时受害者
        # 永久拆台，三周期 16min×4 白跑至取消）。
        _baseline_broken: dict[str, list[str]] = {}
        for fid in failed_ids:
            _det = _det_of(subtask_results.get(fid))
            if _det.get("pipeline_blocked") not in _INTERNAL_BLOCKED_KINDS:
                continue
            _st = _by_id.get(fid)
            _bpkgs = _det.get("blocked_on_packages") or []
            _bmods = _det.get("blocked_on_modules") or []
            _prods = _producers_of(plan_obj, _bpkgs, _bmods)
            # (B round13) 上游已永久放弃 → 依赖它的下游不可恢复(传递闭包)。
            # R65REPLAY-T1 复核 F2：只算【硬】依赖——软序边（edge_is_soft，readable
            # 驱动消费）的死产者与本次 pipeline_blocked 无因果（真因由 _prods/_futile
            # 单独归因），拿无关死软边判永久放弃=把可恢复的"等生产者"误杀成硬弃。
            from swarm.types import edge_is_soft as _eis
            _dep_hit = bool(_st and any(
                d in _unsat and not _eis(_st, _by_id.get(d))
                for d in (getattr(_st, "depends_on", []) or [])))
            _prod_hit = bool(_prods & _unsat)
            # (#R13-2 → #10 泛化) 阻断在【全部生产者已终结(放弃/完成) 且 包仍不在工作树】的内部包 =
            # 在等一个永不会来的产物，永不可满足。含两情形：(a) 完全无生产者=臆造(#R13-2 原语义)；
            # (b) 有生产者但已完成却产了别的包名(#9 跨 feature 漂移，round19 st-38 慢磨 ~1h 的幽灵生产者)。
            # 只要还有 active(pending/在飞/未跑)生产者 → 继续 transient 等待，绝不误杀合法跨模块等待。
            # 假阳性护栏 _package_in_baseline(扫工作树)：包在树(仅漏 seed)→ 交 #12 重 seed，不据此硬失败。
            _futile = _blocked_pkg_unrecoverable(
                blocked_pkgs=_bpkgs, producers=_prods, unsat=_unsat,
                completed_ok=_completed_ok, pending=_pending_now,
                project_path=_proj_path, self_id=fid,
            )
            if _dep_hit or _prod_hit or _futile:
                # round36 P0：完全无生产者(_prods 空) 且 非依赖已放弃上游(非 dep_hit) 且 有 scope
                # → worker 自造引用，走 scope 自愈；其余(真死上游)照旧连坐放弃。
                if _futile and not _prods and not _dep_hit and _st is not None \
                        and getattr(_st, "scope", None) is not None:
                    _selfheal.add(fid)
                else:
                    _unrecoverable.add(fid)
            elif _prods and _st is not None:
                # C9（阶段4，登记册 §四）：还有 active(pending) 生产者的合法跨模块等待——
                # 旧路径=transient 退避重试，每轮整条 locate/code/verify 白跑才撞同一
                # BLOCKED。治=给消费者补【动态 depends_on 边】(fid→pending 生产者)，
                # dispatch 依赖闸(D23 只认 L1 通过)自然扣住它到生产者真完成再派——
                # 廉价、确定性、零白跑。环护栏：生产者可达 fid 则不补（防依赖环死锁）。
                _pending_prods = sorted(
                    p for p in _prods
                    if p != fid and p in _pending_now
                    and not _depends_reaches(plan_obj, p, fid))
                if _pending_prods:
                    _deps_now = list(getattr(_st, "depends_on", []) or [])
                    _added = [p for p in _pending_prods if p not in _deps_now]
                    if _added:
                        _st.depends_on = _deps_now + _added
                        _c9_edges[fid] = _added
            elif _bmods and not _prods:
                # T3：无生产者的模块阻断——若模块存在于 git HEAD 基线 = 基线构建被破坏。
                # 三既有臂全够不到它（dep_hit/prod_hit 要有已放弃上游；futile 要包不在树；
                # C9 要有 active 生产者），恰落空洞 → 旧行为 transient 无望等待（round63:
                # LLM 三轮自诊"预置模块、不在任何子任务范围内"却仍 retry）。此处结构性接住。
                _bb_mods = [m for m in _bmods if _module_in_git_baseline(
                    _proj_path, str(m))]
                if _bb_mods:
                    _baseline_broken[fid] = _bb_mods
        # round36 P0 自愈：无生产者内部包(worker 自造引用) → 授消费者 allow_any + 提示本模块补建被引
        # 类型 + 重派(按子任务 targeted_recovery_counts 熔断，与 A2 缺依赖恢复同预算)。耗尽预算才回落
        # 连坐放弃(原行为)。这把"一个自造引用炸 62 子任务"降为"消费者补建它自己引用的类型"。
        if _c9_edges:
            logger.info(
                "[HANDLE_FAILURE] C9 合法跨模块等待 → 补动态依赖边（消费者扣在依赖闸，"
                "生产者 L1 过再派，替代 transient 白跑）: %s", _c9_edges)
        # ── T3（round63）：基线模块破坏 → 修复臂优先，修不动才 fail-loud，绝不 transient ──
        if _baseline_broken:
            _br_rounds = int(state.get("baseline_repair_rounds", 0) or 0)
            _br_cap = get_config().model.max_retries  # 与各修复阶梯同上限（默认 2）
            _br_restored: list[dict] = []
            _br_scan_errors = 0
            if _br_rounds < _br_cap:
                _br_restored, _br_scan_errors = sweep_baseline_anchor_poison(
                    _proj_path, plan_obj)
            if _br_restored:
                # 修复臂：基线锚已还原（毒真出树）→ 重派。重试此时有据：输入变了（与 B2
                # "同输入必同结果"判据自洽）。baseline_repair_rounds 封顶防"修了又被投毒"
                # 无界循环（T1 禁产毒 + T2 禁入树后，复发即结构异常，耗尽轮次落死锁终局）。
                # 复核 HIGH#1（混批裁决保全）：同批已判 _unrecoverable（真死上游）者照常
                # 连坐放弃、绝不搭修复臂便车白跑整周期；_selfheal 者随批重派（其 scope 自愈
                # 推迟到下轮该臂处理，受本臂轮次上限约束——此处不复制整套自愈机制）。
                _br_ab = _transitive_abandon(
                    plan_obj.subtasks,
                    set(state.get("abandoned_subtask_ids") or []) | _unrecoverable,
                    completed_ids=_completed_ok,
                ) if _unrecoverable else set()
                # R65D-W2 猎手 CRITICAL：消费边织密图后，混批连坐同样受规模闸约束
                # ——一次新增放弃超阈值=计划覆灭，escalate 人工，绝不静默清盘。
                _br_new = _br_ab - set(state.get("abandoned_subtask_ids") or [])
                if len(_br_new) > mass_abandon_cap(len(plan_obj.subtasks)):
                    logger.error(
                        "[HANDLE_FAILURE] R65D-W2 规模闸（T3 修复臂混批）：连坐 %d 超阈值 %d"
                        "（计划 %d）→ escalate 人工，绝不静默清盘",
                        len(_br_new), mass_abandon_cap(len(plan_obj.subtasks)),
                        len(plan_obj.subtasks))
                    return {
                        **({"plan": plan_obj} if _c9_edges else {}),
                        "failure_strategy": "escalate",
                        "failure_escalated": True,
                        "failed_subtask_ids": failed_ids,
                        "degraded_reasons": [
                            f"mass_abandon_gate:{len(_br_new)}/{len(plan_obj.subtasks)}"],
                    }
                for _a in _br_ab:
                    subtask_results.pop(_a, None)
                _br_remaining = [t for t in (state.get("dispatch_remaining") or [])
                                 if t not in _br_ab]
                _br_rc = dict(state.get("subtask_retry_counts", {}))
                for fid in failed_ids:
                    if fid in _br_ab:
                        continue
                    subtask_results.pop(fid, None)
                    if fid not in _br_remaining:
                        _br_remaining.append(fid)
                    # hunter#5：对着毒树的重试徒劳=只豁免【已证归因毒树】的基线阻断者；
                    # 搭车者（超时/其它失败）按全文件统一惯例 +1 记账，绝不白拿免费重试。
                    _br_rc[fid] = 0 if fid in _baseline_broken \
                        else _br_rc.get(fid, 0) + 1
                logger.warning(
                    "[HANDLE_FAILURE] T3 基线模块破坏死锁（%s 阻断在基线模块 %s，plan 无生产者"
                    "=无人会修）→ 修复臂已按 git 基线还原共享清单版本锚 %s（第 %d/%d 轮%s），"
                    "毒已出树，重派 %s（基线阻断者不计配额；同批真死上游连坐放弃 %d）",
                    sorted(_baseline_broken),
                    sorted({m for ms in _baseline_broken.values() for m in ms}),
                    _br_restored, _br_rounds + 1, _br_cap,
                    f"，扫描另有 {_br_scan_errors} 处盲区" if _br_scan_errors else "",
                    [f for f in failed_ids if f not in _br_ab], len(_br_ab),
                )
                _br_ret = {
                    # C9 补边/重派路径统一回写 plan（外层 handle_failure 兜底，此处显式）
                    "plan": plan_obj,
                    "subtask_results": subtask_results,
                    "dispatch_remaining": _br_remaining,
                    "failed_subtask_ids": [],
                    "failure_strategy": "retry",
                    "failure_escalated": False,
                    "subtask_retry_counts": _br_rc,
                    "baseline_repair_rounds": _br_rounds + 1,
                }
                if _br_ab:
                    _br_ret["abandoned_subtask_ids"] = sorted(_br_ab)
                return _br_ret
            if _br_rounds < _br_cap and _br_scan_errors:
                # hunter#1：扫瞎（scanner 坏）≠ 扫净（树真干净）。据盲扫判死锁放弃是方向性
                # 错误——回落既有行为（transient 阶梯自有 B2/A2 封顶），不消耗修复轮次，
                # WARNING 留痕供运维定位 scanner 故障。
                logger.warning(
                    "[HANDLE_FAILURE] T3 基线模块破坏疑似死锁（%s 阻断在基线模块 %s）但修复臂"
                    "扫描盲（%d 处扫描失败、0 还原）→ 不判死锁不放弃，回落既有阶梯；"
                    "请排查 git/文件系统故障",
                    sorted(_baseline_broken),
                    sorted({m for ms in _baseline_broken.values() for m in ms}),
                    _br_scan_errors,
                )
            else:
                # 扫净无可还原（破坏非锚投毒/HEAD 本身坏）或修复轮次耗尽 → 判死锁：并入
                # _unrecoverable 连坐放弃通道（诚实 PARTIAL），fail-loud，绝不 transient
                # 无望等待（register T3 处方：upstream_module_broken on 基线模块 ≠ transient）。
                logger.warning(
                    "[HANDLE_FAILURE] T3 基线模块破坏死锁（%s 阻断在基线模块 %s，plan 无生产者）"
                    "且修复臂%s → 判死锁，连坐放弃（诚实 PARTIAL），不再 transient 重试"
                    "（round63：三周期无望等待 16min×4/轮的治本）",
                    sorted(_baseline_broken),
                    sorted({m for ms in _baseline_broken.values() for m in ms}),
                    f"已达轮次上限({_br_cap})" if _br_rounds >= _br_cap
                    else "无可还原差异（破坏非版本锚投毒，需人工介入）",
                )
                _unrecoverable |= set(_baseline_broken)
        if _selfheal:
            _sh_max = get_config().model.max_retries
            _sh_trc = dict(state.get("targeted_recovery_counts") or {})
            _healed: list[str] = []
            _diverted: set[str] = set()   # R65D-T4：补 import 改道者（区别于 scope 自愈）
            for fid in sorted(_selfheal):
                if _sh_trc.get(fid, 0) >= _sh_max:
                    # 猎手 MED：stdlib 改道耗尽配额时给区分性告警——若真身是与 JDK
                    # 同名的自定义类型（Optional/Path 等），运维要能看见真因，
                    # 而非误读下方"臆造/无生产者"的通用放弃文案。
                    _ex_det = _det_of(subtask_results.get(fid))
                    _ex_stdlib = _stdlib_missing_classes(
                        _ex_det.get("build_output") or "")
                    if _ex_stdlib:
                        logger.warning(
                            "[HANDLE_FAILURE] R65D-T4 补 import 指导已耗尽配额(%d) 仍缺 %s"
                            "——若这是与 JDK 同名的项目自定义类型，需人工把它加进该子任务 "
                            "create_files（名单式改道对真同名自定义类型无能为力）: %s",
                            _sh_max, sorted(_ex_stdlib)[:4], fid)
                    _unrecoverable.add(fid)  # 自愈预算耗尽 → 回落连坐放弃
                    continue
                _st = _by_id.get(fid)
                _det2 = _det_of(subtask_results.get(fid))
                _bpkgs = _det2.get("blocked_on_packages") or []
                _sc = _st.scope
                _decl = (list(getattr(_sc, "writable", []) or [])
                         + list(getattr(_sc, "create_files", []) or []))
                _new_files = _derive_missing_type_files(
                    _decl, _bpkgs, _det2.get("build_output") or "")
                if not _new_files:
                    # R65D-T4 改道：缺失类全是 JDK 标准库类型=worker 缺 import 语句——
                    # 治得了的病绝不放弃、更绝不下"新建同名类型"毒指令（round65d
                    # Map.java 拒工书死型）。注入补 import 指导，按同一自愈配额重派。
                    _stdlib = _stdlib_missing_classes(
                        _det2.get("build_output") or "")
                    if _stdlib:
                        # 猎手 HIGH：清除旧轮（本闸上线前/checkpoint 恢复）被误诊逻辑
                        # 塞进 create_files 的 JDK 同名毒文件声明——否则 worker 同时
                        # 收到"补 import"与"可新建 Map.java"两道矛盾指令（round65d
                        # SCOPE_OBJECTION 拒工书死型变体）。
                        _bad_names = {c.lower() for c in _stdlib}
                        _cf_old = list(getattr(_sc, "create_files", []) or [])
                        _cf_new = [p for p in _cf_old
                                   if str(p).rsplit("/", 1)[-1].rsplit(".", 1)[0]
                                   .lower() not in _bad_names]
                        if len(_cf_new) != len(_cf_old):
                            _sc.create_files = _cf_new
                            logger.warning(
                                "[HANDLE_FAILURE] R65D-T4 清除 %s 残留的 JDK 同名毒 "
                                "create_files 声明 %d 条（旧轮误诊/checkpoint 恢复残留）",
                                fid, len(_cf_old) - len(_cf_new))
                        _st.retry_guidance = (
                            f"编译缺失的 {sorted(_stdlib)[:6]} 是 JDK/标准库类型——"
                            "你缺的是 import 语句（如 import java.util.Map），"
                            "请在报错文件顶部补全对应 import 后重交。"
                            "绝不在项目内新建这些同名类型（会遮蔽标准库并毒化模块）。"
                        )[:800]
                        _sh_trc[fid] = _sh_trc.get(fid, 0) + 1
                        _healed.append(fid)
                        _diverted.add(fid)
                        continue
                    # 推不出该建哪个文件 → 自愈无从下手 → 回落连坐放弃(不空烧预算、不假装能修)
                    _unrecoverable.add(fid)
                    continue
                _cf = list(getattr(_sc, "create_files", []) or [])
                for _nf in _new_files:
                    if _nf not in _cf:
                        _cf.append(_nf)
                _sc.create_files = _cf  # ★纳入 create_files：scope 放行新建 + _writable_files 拉回本地★
                _st.retry_guidance = (
                    f"你的代码引用了项目内不存在、且无任何子任务负责生产的内部类型 {_bpkgs}"
                    f"（编译报 package does not exist / cannot find symbol）。这是本功能自身需要的类型，"
                    f"已把待建文件 {[p.rsplit('/', 1)[-1] for p in _new_files][:6]} 纳入你的可写范围——"
                    f"请在本模块内【新建】它们（VO/DTO/枚举/请求响应对象等）使编译通过，而非假设已存在。"
                )[:800]
                _sh_trc[fid] = _sh_trc.get(fid, 0) + 1
                _healed.append(fid)
            if _healed:
                # 复核 HIGH#1（混批）：真死上游 _unrecoverable 在同一 return 里【照常连坐放弃】，
                # 绝不因存在自愈项就拖着不放弃；只重派【已愈】项，放弃/未愈项不重派。
                _ab = _transitive_abandon(  # R51-1：completed 绝不入闭包
                    plan_obj.subtasks,
                    set(state.get("abandoned_subtask_ids") or []) | _unrecoverable,
                    completed_ids=_completed_ok,
                ) if _unrecoverable else set()
                # R65D-W2 猎手 CRITICAL：自愈混批的连坐同受规模闸约束（四点同源）
                _sh_new_ab = _ab - set(state.get("abandoned_subtask_ids") or [])
                if len(_sh_new_ab) > mass_abandon_cap(len(plan_obj.subtasks)):
                    logger.error(
                        "[HANDLE_FAILURE] R65D-W2 规模闸（自愈混批）：连坐 %d 超阈值 %d"
                        "（计划 %d）→ escalate 人工，绝不静默清盘",
                        len(_sh_new_ab), mass_abandon_cap(len(plan_obj.subtasks)),
                        len(plan_obj.subtasks))
                    return {
                        **({"plan": plan_obj} if _c9_edges else {}),
                        "failure_strategy": "escalate",
                        "failure_escalated": True,
                        "failed_subtask_ids": failed_ids,
                        "degraded_reasons": [
                            f"mass_abandon_gate:{len(_sh_new_ab)}/{len(plan_obj.subtasks)}"],
                    }
                for _a in _ab:
                    subtask_results.pop(_a, None)
                _sh_rc = dict(state.get("subtask_retry_counts", {}))
                for fid in _healed:
                    # 猎手 MED：清零只给【scope 真不可满足】的自愈者（旧理由成立）；
                    # 补 import 改道者的既往失败是普通可修错误，保留计数——否则每次
                    # 改道都白拿一份全新常规配额（预算静默翻倍+违反单调记账不变量）。
                    if fid not in _diverted:
                        _sh_rc[fid] = 0  # 因 scope 不可满足而徒劳的重试不计入常规配额
                _sh_remaining = [t for t in (state.get("dispatch_remaining") or []) if t not in _ab]
                for fid in _healed:
                    if fid in _ab:  # 已愈但落在放弃闭包(依赖真死上游)→随闭包放弃，不重派
                        continue
                    subtask_results.pop(fid, None)
                    if fid not in _sh_remaining:
                        _sh_remaining.append(fid)
                # ★R65D-T1 根修（round65d 死因本体）★：与 _unrecoverable 分支对称——
                # 同批【未愈未放弃】的其余失败（C9 补边消费者/verify 失败等）放回重派，
                # 各自重试计数原样保留，下轮再进常规阶梯。旧行为把它们 failed_subtask_ids
                # 清零+不回队+失败 result 滞留 → st-26 僵尸化被数成"完成态" → 90/94 经
                # C9 汇流饿死（10:45:30 处理 4 只处置 3 的实锤本体）。
                _sh_leftover = [f for f in failed_ids
                                if f not in _healed and f not in _ab]
                for fid in _sh_leftover:
                    subtask_results.pop(fid, None)
                    if fid not in _sh_remaining:
                        _sh_remaining.append(fid)
                logger.warning(
                    "[HANDLE_FAILURE] round36 P0 自愈：无生产者内部类型(worker 自造引用) → 把待建类型文件"
                    "纳入 create_files 让消费者本模块补建 + 重派(按子任务熔断 %s/%d)；同批真死上游 %s "
                    "照常连坐放弃 %d；其余失败 %s 放回重派（R65D-T1：绝不静默掉账）",
                    {k: _sh_trc[k] for k in _healed}, _sh_max, sorted(_unrecoverable),
                    len(_ab), _sh_leftover)
                _ret = {
                    "plan": plan_obj,
                    "subtask_results": subtask_results,
                    "dispatch_remaining": _sh_remaining,
                    "failed_subtask_ids": [],
                    "failure_strategy": "retry_alternate",
                    "failure_escalated": False,
                    "targeted_recovery_counts": _sh_trc,
                    "subtask_retry_counts": _sh_rc,
                }
                if _ab:
                    _ret["abandoned_subtask_ids"] = sorted(_ab)
                return _ret
        if _unrecoverable:
            abandoned = _transitive_abandon(
                plan_obj.subtasks,
                set(state.get("abandoned_subtask_ids") or []) | _unrecoverable,
                completed_ids=_completed_ok,  # R51-1
            )
            # R65C-T2 修③：连坐规模闸——一次放弃超过计划 25%（或 10 个取大）绝不静默
            # 执行：那不是"剪除死枝"而是"计划覆灭"，必须 escalate 人工决策。
            # round65c 实锤：单个 0.8s BLOCKED 探针触发 102/107 连坐 → 假«全部完成»。
            # 显式剪除决策之外，pending 工作集绝不允许一笔大额缩水（结构性不变量）。
            _new_abandon = set(abandoned) - set(state.get("abandoned_subtask_ids") or [])
            _abandon_cap = mass_abandon_cap(len(plan_obj.subtasks))  # R65D-W2 四点单源
            if len(_new_abandon) > _abandon_cap:
                logger.error(
                    "[HANDLE_FAILURE] R65C-T2 连坐规模闸：本次连坐 %d 个（阈值 %d，计划共 %d）"
                    "——触发源 %s。这不是剪枝而是计划覆灭，escalate 人工决策，绝不静默放弃",
                    len(_new_abandon), _abandon_cap, len(plan_obj.subtasks),
                    sorted(_unrecoverable)[:6],
                )
                return {
                    **({"plan": plan_obj} if _c9_edges else {}),
                    "failure_strategy": "escalate",
                    "failure_escalated": True,
                    "failed_subtask_ids": failed_ids,
                    "degraded_reasons": [
                        f"mass_abandon_gate:{len(_new_abandon)}/{len(plan_obj.subtasks)}"],
                }
            for _a in abandoned:
                subtask_results.pop(_a, None)
            _remaining = [t for t in (state.get("dispatch_remaining") or []) if t not in abandoned]
            # 非不可恢复的其余失败放回重派（各自重试计数原样保留，下轮再进常规阶梯）
            for fid in [f for f in failed_ids if f not in abandoned]:
                subtask_results.pop(fid, None)
                if fid not in _remaining:
                    _remaining.append(fid)
            logger.warning(
                "[HANDLE_FAILURE] 不可恢复子任务 %s(+依赖闭包共 %d) → 连坐放弃、不再 retry/replan："
                "上游已永久放弃 或 阻断在臆造/基线不存在且无生产者的包；终态 PARTIAL（诚实列明需人工补完）",
                sorted(_unrecoverable), len(abandoned),
            )
            return {
                # C9（4.9 复核 R-F6/H-F6）：补边必须在【所有】可达 return 回写 plan——
                # in-place 变异靠 checkpoint 捎带是被禁模式（重启即丢边，白跑复发）。
                **({"plan": plan_obj} if _c9_edges else {}),
                "failure_strategy": "abandon",
                "failure_escalated": False,  # 批4c：非 escalate 决策清历史粘滞标记（取证 CONFIRMED，见 DEVLOG）
                "abandoned_subtask_ids": sorted(abandoned),
                "failed_subtask_ids": [],
                "dispatch_remaining": _remaining,
                "subtask_results": subtask_results,
            }

    # ── 主干B 不变量·超时→强制拆小作【第一恢复动作】（先于 LLM 策略 + 一切换模型重试）──
    # 子任务 coding/locating 超时 = 工作单元对执行预算太大的确定性信号。先换模型重试同样的大块
    # 只会再超时（round10 实证磨到取消）；确定性拆小才治本。可拆的立即拆小重派、保留成功兄弟，
    # 不可拆的（≤2 文件/已拆过）落回常规阶梯。无任何可拆超时 → None → 继续常规分析。
    _timeout_ids = [fid for fid in failed_ids
                    if _is_timeout_oversize_failure(subtask_results.get(fid))]
    if _timeout_ids:
        _redecomp_timeout = await _redecompose_timeout_subtasks(state, _timeout_ids)
        if _redecomp_timeout is not None:
            return _redecomp_timeout

    # ── LLM 故障分析 ──
    # audit #17：strategy 必须在 try 前有确定默认值——否则 _get_brain_llm() 抛异常时
    # except 分支用到 strategy 会 NameError。默认 "retry" 表示确定性回退（非 LLM 建议）。
    strategy = "retry"
    _diagnosis = ""   # A4 治本(round11)：brain 失败诊断，注入重试 worker 提示防重蹈
    _llm_missing_files: list[str] = []  # B3-3：LLM 点名"计划无人创建"的文件（结构化载荷）
    try:
        llm = _get_brain_llm()
        failure_details_dict: dict[str, dict] = {}
        for fid in failed_ids:
            out = subtask_results.get(fid)
            if isinstance(out, WorkerOutput):
                failure_details_dict[fid] = out.l1_details
            elif isinstance(out, dict):
                failure_details_dict[fid] = out.get("l1_details", {})
            else:
                failure_details_dict[fid] = {}
        failure_details = json.dumps(failure_details_dict, ensure_ascii=False)
        # D50：瘦身 plan（剥每子任务 contract/context_snippets 副本）——旧全量 dump 把
        # ~1MB plan_json 注入失败分析 prompt（validate_plan 已改 slim，此处漏改）。
        from swarm.brain.plan_validator import slim_plan_json_or_empty
        plan_json = slim_plan_json_or_empty(plan_obj)
        prompt_user = HANDLE_FAILURE_USER.format(
            failed_subtask_ids=failed_ids,
            failure_details=failure_details,
            plan_json=plan_json,
        )
        response = await llm.ainvoke([
            {"role": "system", "content": HANDLE_FAILURE_SYSTEM},
            {"role": "user", "content": prompt_user},
        ])
        result = _parse_json_from_llm(response.content)
        # Wave 1/TD2606-B1：策略走类型边界。未知策略 → ValidationError → 下方 except 确定性回退 retry
        # （不让 LLM 吐的未知字符串静默穿过策略阶梯）。result 保留供下游读取 adjusted_subtasks 等。
        _fs = FailureStrategyResponse.model_validate(result)
        strategy = _fs.strategy
        _diagnosis = (_fs.reasoning or "").strip()
        # B3-3：结构化载荷真消费（旧 extra:"ignore" 把 LLM 点名的缺失文件当散文丢弃）
        _llm_missing_files = [str(x).strip() for x in (_fs.missing_files or [])
                              if str(x).strip()][:10]
        logger.info(f"[HANDLE_FAILURE] LLM 策略: {strategy} — {_fs.reasoning}")
    except json.JSONDecodeError as e:
        logger.warning(f"[HANDLE_FAILURE] LLM 输出解析失败 → 确定性回退 retry（非 LLM 建议）: {e}")
        from swarm.infra.degrade import record_degrade
        record_degrade("brain.handle_failure.llm_fallback")  # E1
        strategy = "retry"
    except Exception as e:
        logger.warning(f"[HANDLE_FAILURE] LLM 分析异常 → 确定性回退 retry（非 LLM 建议）: {e}")
        from swarm.infra.degrade import record_degrade
        record_degrade("brain.handle_failure.llm_fallback")  # E1
        strategy = "retry"

    # ── A4 治本(round11)：把 brain 诊断作为硬约束注入【重试 worker 提示】──
    # round11: brain 明写"该 RuoYi 版本用 ShiroUtils 而非 SecurityUtils"却只 retry_alternate
    # 换模型、不传 worker → 重试 worker 仍 import 不存在的 SecurityUtils。把诊断挂到失败子任务
    # 的 retry_guidance(worker prompt 渲染为硬约束块)，所有 retry 分支(A2/常规阶梯)统一携带。
    # R65TR-T2 B1：确定性装填——LLM 诊断有无都恒并入该子任务的机读判死依据
    # （evaluate_l1 stamp 的 l1_details["det_fail_reason"]）。回放实锤：brain 离线时
    # LLM 分析异常→_diagnosis 空→st-2 五连跑零信息重试，判死原文从未抵达模型；
    # 在线时确定性依据也从不注入。栈中立（依据由 worker 闸门按失败面取证）。
    if failed_ids:
        _by_id = {st.id: st for st in (getattr(plan_obj, "subtasks", None) or [])}
        for _fid in failed_ids:
            _st = _by_id.get(_fid)
            if _st is None:
                continue
            _parts: list[str] = []
            if _diagnosis:
                _parts.append(_diagnosis[:800])
            else:
                # 猎手 R65TR-T2：LLM 本轮闪断（自建模型临时掉线是常态）时绝不倒退——
                # 保留上一轮语义诊断（剥掉旧确定性依据行防跨轮堆叠，该行下方按本轮重写）。
                _prev = (getattr(_st, "retry_guidance", "") or "").strip()
                if _prev:
                    _prev_keep = "\n".join(
                        ln for ln in _prev.splitlines()
                        if not ln.startswith("上次尝试的确定性判死依据")).strip()
                    if _prev_keep:
                        _parts.append(_prev_keep[:800])
            _det_r = str((_l1_details_of(subtask_results, _fid) or {})
                         .get("det_fail_reason") or "").strip()
            if _det_r:
                _parts.append(
                    "上次尝试的确定性判死依据（机读，必须针对性修复后再交付）："
                    f"{_det_r[:600]}")
            if _parts:
                # SubTask 是可变 pydantic BaseModel、retry_guidance 是声明的 str 字段 →
                # 直接赋值不会抛（原 except:pass 是无谓的静默吞错，brain#3 一并去掉）。
                # 就地改的持久化由外层 handle_failure 回传 plan 保证。
                _st.retry_guidance = "\n".join(_parts)

    # ── P0-B/P1-D：缺符号/缺依赖编译失败 → 定向恢复（先于一切 strategy 分支拦截）──
    # 这类失败是【scope 不可满足】（pom 不在子任务写权内，原地重试 100 次也修不了）。无论 LLM
    # 选 retry 还是 replan，都先走定向恢复：补模块 pom 写权 + 重置徒劳的重试计数 + 只重派失败
    # 子任务（保留成功兄弟、不进 PLAN、不清完成态全表）。targeted_recovery_counts【按子任务】熔断防死循环（遗漏项#2）。
    # B3-1（round38c 缺陷3）：缺依赖拦截让位"计划缺口"信号——LLM 判 replan 且点名的
    # 缺失文件在全 plan 无 owner 时，"缺符号"的根因是【计划缺 create_files】而非
    # scope 不可满足：补 pom/换模型对此零效（17:06 实证 LLM 判对被本拦截覆盖，
    # TwoFactorBindVO 拖 3-5h）。放行到 replan 分支走 B3-2 外科修正。
    _plan_gap = bool(strategy == "replan" and _llm_missing_files
                     and plan_obj is not None
                     and not _planned_producers_exist(plan_obj, _llm_missing_files))
    if _plan_gap:
        logger.info(
            "[HANDLE_FAILURE] B3-1 计划缺口信号：LLM 点名缺失文件 %s 全 plan 无 owner → "
            "跳过缺依赖定向恢复（补 pom 治不了计划缺口），交 replan 分支外科修正",
            _llm_missing_files[:5])
    if _is_missing_dependency_failure(subtask_results, failed_ids) and failed_ids and not _plan_gap:
        _tr_max = get_config().model.max_retries  # 复用 max_retries（默认 2）
        # round29 遗漏项#2 治本：熔断改【按子任务】计（targeted_recovery_counts）——旧任务级
        # 全局计数会被先失败的子任务用光、饿死后续同类受害者（d37a52a3 st-25 实证：配额被
        # st-4-1 波耗尽，st-25 从未拿到 pom 写权即"已达上限"落兜底 → 迭代上限/900s 空烧 →
        # +24 abandon 波主推手）。同子任务 grant→fail→grant 环安全语义不变（每 fid ≤ _tr_max）。
        _trc = dict(state.get("targeted_recovery_counts") or {})
        _eligible = [fid for fid in failed_ids if _trc.get(fid, 0) < _tr_max]
        if not _eligible:
            # 熔断：全部失败子任务各自达上限仍缺依赖 → 不再 mutate plan，落常规 strategy 兜底
            #（HIGH-3：先判上限再改 plan）。
            logger.warning(
                "[HANDLE_FAILURE] 失败子任务 %s 各自定向恢复均已达上限(%d 次)仍缺依赖 → 落常规 %s 兜底",
                failed_ids, _tr_max, strategy,
            )
        else:
            # 仅在配额内才 mutate plan（补构建清单写权 + 串 owner 依赖），杜绝兜底路径留下孤儿 scope 改动。
            # ★#38 治本★ 按项目栈授【正确清单】（Go→go.mod…），绝不在非 Maven 工程授幻影 pom.xml。
            _recovery_manifest = stack_module_manifest(state.get("project_stack"))
            granted = _grant_module_pom_writable(plan_obj, _eligible, _recovery_manifest)
            if granted:
                # 治本 A2：授权后【确定性】据项目自身 pom 把缺失依赖补进失败模块 pom，
                # 不再指望小模型自己加（实测它加不上 → 耗尽配额 → 全量 replan 砸成功子任务）。
                # F9：经 per-stack driver 分发（Maven=既有 driver；未覆盖栈 loud no-op）
                _dep_injected = inject_missing_deps_for_stack(
                    state.get("project_stack"),
                    _proj_path_from_state(state), granted, subtask_results)
                if _dep_injected:
                    logger.info(
                        "[HANDLE_FAILURE] 确定性补依赖（治本 A2，据项目自身 pom 自证坐标，"
                        "重派 worker 直接编过、不再耗配额）：%s", _dep_injected,
                    )
                # ── round65e5 st-53-1 治本 D2/D3：给授 pom 写权的重派 worker 精准 guidance ──
                # D3：恒注入"最小增量铁律"防手写 pom 腐化（R2 实锤 <group>/毁 <parent>）。
                # D2：缺失包在全仓+依赖树无坐标（幻觉/未 provision，如 R3 `.generator`）→ 追加
                #     "改代码别加依赖"提示 + 记降级信号；补依赖救不了幻觉、授 pom 写权反诱发腐化。
                try:
                    _dep_cls = classify_missing_deps_for_stack(
                        state.get("project_stack"),
                        _proj_path_from_state(state), granted, subtask_results)
                except Exception:  # noqa: BLE001 — 分类失败绝不阻断恢复（fail-open）
                    # ★复核 F1 整改★ except 必配 record_degrade：否则"分类崩了"与"确实没
                    # unprovisioned"在 /api/metrics 上无从区分（degrade 约定，见 infra/degrade）。
                    from swarm.infra.degrade import record_degrade
                    record_degrade("brain.handle_failure.dep_classify_error")
                    logger.warning("[HANDLE_FAILURE] D2 缺失包分类异常（fail-open，退回既有恢复）",
                                   exc_info=True)
                    _dep_cls = {}
                _rc_guidance = _dep_recovery_retry_guidance(granted, _dep_cls, _dep_injected)
                for _gid, _gtext in _rc_guidance.items():
                    _gst = _by_id.get(_gid)
                    if _gst is None:
                        # ★复核 F3 整改★ 现结构下不可达（granted ⊆ failed_ids ⊆ _by_id），但一旦未来
                        # 授权/快照解耦，静默丢弃=R2 抗腐化铁律恰在最需要时消失且无痕——留响亮日志。
                        logger.warning("[HANDLE_FAILURE] D2/D3 guidance 目标 sid %s 不在 plan → 丢弃"
                                       "（不应发生，授权与 plan 快照解耦？）", _gid)
                        continue
                    if not _gtext:
                        continue
                    _gprev = getattr(_gst, "retry_guidance", "") or ""
                    _gnew = _merge_dep_guidance_lines(_gprev, _gtext)  # replace 语义（可测）
                    if _gnew != _gprev:
                        _gst.retry_guidance = _gnew
                if any((v.get("unprovisioned") for v in _dep_cls.values())):
                    from swarm.infra.degrade import record_degrade
                    record_degrade("brain.handle_failure.dep_unprovisioned")  # D2 信号
                    logger.warning(
                        "[HANDLE_FAILURE] D2：缺失包在全仓+依赖树无自证坐标（幻觉/未 provision）→ "
                        "已注入'改代码别加依赖'guidance，不再靠补依赖空烧：%s",
                        {k: v.get("unprovisioned") for k, v in _dep_cls.items() if v.get("unprovisioned")},
                    )
                # D2 复核 CONFIRMED：无产出放弃者（revert 路，已 pop 出 subtask_results）
                # 不得入链——_is_ready 对其永不就绪，入链=新授权任务被自己刚加的边扣死
                _no_output_abandoned = {
                    sid for sid in (set(state.get("abandoned_subtask_ids") or [])
                                    | set(state.get("give_up_isolated_ids") or []))
                    if sid not in subtask_results
                }
                _serialize_pom_writers(plan_obj, granted,
                                       exclude_ids=_no_output_abandoned)
                dispatch_remaining = list(state.get("dispatch_remaining", []))
                for fid in failed_ids:
                    subtask_results.pop(fid, None)
                    if fid not in dispatch_remaining:
                        dispatch_remaining.append(fid)
                # 之前的重试因 scope 不可满足而徒劳，不计入配额——重置【获授权】子任务重试计数
                #（未获授权的搭车重派者保留计数，防其经反复重置绕过常规熔断）。
                _rc = dict(state.get("subtask_retry_counts", {}))
                for fid in granted:
                    _rc[fid] = 0
                # 配额按子任务消费：只给真正获 pom 授权者记账（遗漏项#2）。
                for fid in granted:
                    _trc[fid] = _trc.get(fid, 0) + 1
                _kept = [sid for sid in subtask_results if sid not in failed_ids]
                _riders = [f for f in failed_ids if f not in granted]
                logger.info(
                    "[HANDLE_FAILURE] 定向恢复（按子任务配额 %s / 上限 %d）：缺符号/缺依赖编译失败 → "
                    "给失败子任务 补模块 pom 写权 %s + 重置重试计数，仅重派失败子任务 %s"
                    "（保留 %d 个完成态；搭车重派/未计配额未重置计数=%s），换备选模型，不进 PLAN、不清完成态全表",
                    {k: _trc[k] for k in granted}, _tr_max, granted, failed_ids, len(_kept), _riders,
                )
                return {
                    "plan": plan_obj,
                    "subtask_results": subtask_results,
                    "dispatch_remaining": dispatch_remaining,
                    "failed_subtask_ids": [],
                    "failure_strategy": "retry_alternate",
                    "failure_escalated": False,  # 批4c：非 escalate 决策清历史粘滞标记（取证 CONFIRMED，见 DEVLOG）
                    "subtask_use_alternate": _alt_map_update(state, failed_ids, True),
                    "subtask_retry_counts": _rc,
                    "targeted_recovery_count": state.get("targeted_recovery_count", 0) + 1,  # 遥测保留
                    "targeted_recovery_counts": _trc,
                    }
            # granted 为空（推不出模块 pom）→ 不 mutate、不自增计数，落常规 strategy（其自带
            # replan_count 熔断会兜底升级），不会在此空转（MEDIUM-2）。
            logger.info(
                "[HANDLE_FAILURE] 缺依赖失败但推不出可补的模块 pom（失败子任务无模块路径）→ 落常规 %s",
                strategy,
            )

    # ── round29 A(b)：模块「注册先于脚手架」依赖序定点重排（先于 replan 守卫拦截）──
    # worker 分类器坐实症状（清单注册的模块在树里不存在=plan 期边连反的确定性后果），重试/换模型/
    # replan 都治不了。定点插「registrant-after-scaffold」规范边（planning_core 删反向直边+传递防环）
    # + 只重派失败子任务、保成功兄弟、重置徒劳重试计数；targeted_recovery_counts【按子任务】断路器（与 A2
    # 缺依赖阶梯共用配额）防死循环。掐断 d37a52a3 的「守卫堵死→阶梯三 revert→级联 abandon」死路。
    _order_mods = _module_order_violation_modules(subtask_results, failed_ids)
    if _order_mods and plan_obj is not None and failed_ids:
        _tro_max = get_config().model.max_retries  # 复用 max_retries（默认 2）
        # 遗漏项#2：与 A2 同表按【子任务】计配额（targeted_recovery_counts），不再共用任务级
        # 全局计数互相挤兑（round29-A 复核 #7 注记的正解）。
        _trc_o = dict(state.get("targeted_recovery_counts") or {})
        _eligible_o = [fid for fid in failed_ids if _trc_o.get(fid, 0) < _tro_max]
        if not _eligible_o:
            logger.warning(
                "[HANDLE_FAILURE] 失败子任务 %s 各自序修复配额均已达上限(%d 次)仍撞"
                "「注册先于脚手架」→ 落常规 %s 兜底", failed_ids, _tro_max, strategy,
            )
        else:
            from swarm.brain.nodes.planning_core import _add_dep_safe, _insert_module_order_edge
            _edges: list[tuple[str, str]] = []
            _unlocated: list[str] = []
            _regs = _root_manifest_registrants(plan_obj)
            _by_id_all = {s.id: s for s in getattr(plan_obj, "subtasks", []) or []}
            for _mod in sorted(_order_mods):
                _scaf = _scaffold_subtask_of_module(plan_obj, _mod)
                if _scaf is None:
                    _unlocated.append(_mod)  # 定位不到脚手架（模块臆造/plan 外/归一化差异）
                    continue
                for _reg in _regs:
                    if _reg.id != _scaf.id and _insert_module_order_edge(plan_obj, _reg.id, _scaf.id):
                        _edges.append((_reg.id, _scaf.id))
                # 复核整改（reviewer#6）：失败子任务本身也补「等脚手架」边——否则 scaffold 尚未跑完时
                # 重派会立刻再撞同一 reactor 错。_add_dep_safe 传递防环；scaffold 已完成则边天然满足。
                for fid in failed_ids:
                    if fid != _scaf.id and _add_dep_safe(_by_id_all, fid, _scaf.id):
                        _edges.append((fid, _scaf.id))
            if _unlocated:
                # 猎人#2：定位失败=本阶梯对这些模块失效（将退回常规阶梯直至 abandon）。必须 WARNING
                # 级留痕，运维/复盘才能第一时间看出「序修复未激活」而非普通空转。
                logger.warning(
                    "[HANDLE_FAILURE] 撞「注册先于脚手架」但定位不到模块 %s 的脚手架子任务"
                    "（模块臆造/plan 外/路径归一化差异）→ 这些模块的序修复未激活，退回常规阶梯",
                    _unlocated,
                )
            if _edges:
                dispatch_remaining = list(state.get("dispatch_remaining", []))
                for fid in failed_ids:
                    subtask_results.pop(fid, None)
                    if fid not in dispatch_remaining:
                        dispatch_remaining.append(fid)
                # 结构性序问题上的既往重试是徒劳，不计能力配额（仅限【本次消费配额】的 eligible
                # 子任务，搭车者保留计数防绕过常规熔断）；循环边界由按子任务配额保证。
                _rco = dict(state.get("subtask_retry_counts", {}))
                for fid in _eligible_o:
                    _rco[fid] = 0
                    _trc_o[fid] = _trc_o.get(fid, 0) + 1
                logger.info(
                    "[HANDLE_FAILURE] 序修复（按子任务配额 %s / 上限 %d）：清单注册的模块 %s 在树里"
                    "不存在 → 插「注册后于脚手架」规范边 %s + 仅重派失败子任务 %s（保留 %d 个完成态；"
                    "搭车重派/未计配额=%s），不进 PLAN",
                    {k: _trc_o[k] for k in _eligible_o}, _tro_max, sorted(_order_mods), _edges,
                    failed_ids, len([s for s in subtask_results if s not in failed_ids]),
                    [f for f in failed_ids if f not in _eligible_o],
                )
                return {
                    "plan": plan_obj,
                    "subtask_results": subtask_results,
                    "dispatch_remaining": dispatch_remaining,
                    "failed_subtask_ids": [],
                    "failure_strategy": "retry",
                    "failure_escalated": False,
                    "subtask_retry_counts": _rco,
                    "targeted_recovery_count": state.get("targeted_recovery_count", 0) + 1,  # 遥测保留
                    "targeted_recovery_counts": _trc_o,
                    }
            logger.warning(
                "[HANDLE_FAILURE] 撞「注册先于脚手架」但无一条序边可安全成立（脚手架全定位不到/"
                "插边成环）→ 序修复未激活，落常规 %s", strategy,
            )

    # ── B4-2（round38c 缺陷4）：worker 结构化 scope 异议消费——scope 文件名/路径本身
    # 错误（撞框架类名等）时替换条目重派，不再原样锁死让 worker 在"类名=文件名"里穷举 ──
    _obj = _apply_scope_objection(plan_obj, subtask_results, failed_ids, state)
    if _obj is not None:
        _obj_remaining = list(state.get("dispatch_remaining", []))
        for fid in failed_ids:
            subtask_results.pop(fid, None)
            if fid not in _obj_remaining:
                _obj_remaining.append(fid)
        logger.info("[HANDLE_FAILURE] B4-2 scope 异议应用：%s → 替换 create_files 条目后重派",
                    _obj["applied"])
        return {
            "plan": plan_obj,
            "subtask_results": subtask_results,
            "dispatch_remaining": _obj_remaining,
            "failed_subtask_ids": [],
            "failure_strategy": "retry",
            "failure_escalated": False,
            "subtask_scope_amend_counts": _obj["counts"],
        }

    if strategy == "replan":
        # ── 修复 B：replan 守卫 —— 保护已成功的兄弟子任务，避免一个子任务失败就全量推倒重来 ──
        # 背景(task dab669bb)：medium 任务拆成 st-1(实现)+st-2(测试)，st-1 成功 DONE、
        # st-2 因写错 JUnit L1 失败 → LLM 选 replan → 清空【含成功的 st-1】全部重新规划 ~10min →
        # 循环。replan 只该用于【计划本身有结构性问题】(拆分错/依赖悬空)，单个子任务的
        # L1 质量失败应只【重做失败子任务】，保留成功成果。
        # 守卫条件：本批失败是子任务级 L1 失败 + 存在已成功(L1 通过)的兄弟子任务 +
        #          失败子任务未达重试上限 → 降级为 retry（只重派失败的，不动成功的）。
        succeeded_siblings = [
            sid for sid, out in subtask_results.items()
            if sid not in failed_ids and l1_passed(out)
        ]
        _retry_counts = dict(state.get("subtask_retry_counts", {}))
        _next_counts = {fid: _retry_counts.get(fid, 0) + 1 for fid in failed_ids}
        _deepest = max(_next_counts.values(), default=0)
        _max_retries = get_config().model.max_retries  # 默认 2
        # R1a（治本，996db614 实测主失控）：**只要存在已成功兄弟子任务，就绝不全量 replan-clobber**。
        # 旧守卫仅在【未烧光重试配额】时拦截，一旦失败子任务耗尽重试(_deepest>max+1)就落到下方全量
        # replan→PLAN 清空完成态→把 34 个已完成全丢弃从头重跑（实测 剩余0/完成34→剩余47/完成1，再撞
        # 同一幻觉 escalate→FAILED）。但 replan(重生成 plan) 治不了 worker 臆造方法/能力失败——只会重生成
        # 同样的 plan 再失败。故：有成功兄弟时——还有重试预算→只重做失败的(retry/retry_alternate)；
        # 已耗尽→escalate【失败子任务】并【完整保留成功成果】，绝不清空。
        if succeeded_siblings and failed_ids:
            # B3-2 外科出口：LLM 点名的缺失文件全 plan 无 owner=计划级缺陷——守卫降级
            # retry 前先补 create_files（每子任务限 1 次+防毒校验），使 replan 判决落地为
            # 最小计划修正而非被降级白跑（round38c LLM 判对 10+ 次全被覆盖的治本面）。
            if _llm_missing_files and not _planned_producers_exist(plan_obj, _llm_missing_files):
                _amend = _amend_scope_with_missing_files(
                    plan_obj, failed_ids, _llm_missing_files, state,
                    project_path=_proj_path_from_state(state))
                if _amend:
                    dispatch_remaining = list(state.get("dispatch_remaining", []))
                    for fid in failed_ids:
                        subtask_results.pop(fid, None)
                        if fid not in dispatch_remaining:
                            dispatch_remaining.append(fid)
                    logger.info(
                        "[HANDLE_FAILURE] B3-2 外科计划修正：缺失文件 %s 补进 %s 的 "
                        "create_files（全 plan 无 owner，replan 判决转外科），仅重派失败子任务",
                        _amend["applied"], failed_ids[0])
                    return {
                        "plan": plan_obj,
                        "subtask_results": subtask_results,
                        "dispatch_remaining": dispatch_remaining,
                        "failed_subtask_ids": [],
                        "failure_strategy": "retry",
                        "failure_escalated": False,
                        "subtask_scope_amend_counts": _amend["counts"],
                    }
            if _deepest <= _max_retries + 1:
                dispatch_remaining = list(state.get("dispatch_remaining", []))
                for fid in failed_ids:
                    subtask_results.pop(fid, None)
                    if fid not in dispatch_remaining:
                        dispatch_remaining.append(fid)
                forced_alternate = _deepest > _max_retries
                logger.info(
                    "[HANDLE_FAILURE] replan 守卫生效 — 保留 %d 个成功子任务 %s，"
                    "仅重做失败子任务 %s（第 %d 次%s），不全量重规划",
                    len(succeeded_siblings), succeeded_siblings, failed_ids, _deepest,
                    "，换备选模型" if forced_alternate else "",
                )
                return {
                    # C9（4.9 复核 R-F6/H-F6）：补边必须在【所有】可达 return 回写 plan——
                    # in-place 变异靠 checkpoint 捎带是被禁模式（重启即丢边，白跑复发）。
                    **({"plan": plan_obj} if _c9_edges else {}),
                    "subtask_results": subtask_results,
                    "dispatch_remaining": dispatch_remaining,
                    "failed_subtask_ids": [],
                    "failure_strategy": "retry_alternate" if forced_alternate else "retry",
                    "failure_escalated": False,  # 批4c：非 escalate 决策清历史粘滞标记（取证 CONFIRMED，见 DEVLOG）
                    "subtask_use_alternate": _alt_map_update(state, failed_ids, forced_alternate),
                    "subtask_retry_counts": {**_retry_counts, **_next_counts},
                }
            # 卡死子任务恢复阶梯·阶梯二：escalate 前先试【定点拆小】（仅单个失败子任务时）。
            # 多文件卡死多因子任务太大，拆小后小块各自更易成功，保留成功兄弟、只重派小块。
            if len(failed_ids) == 1:
                _redecomp = await _targeted_redecompose(state, failed_ids[0])
                if _redecomp is not None:
                    return _redecomp
            # 卡死子任务恢复阶梯·阶梯三：escalate(全盘 FAILED) 前先试【保 build 放弃】——
            # 清本地树足迹防 -am reactor 中毒，被依赖→打可编译桩(救下游)、不被依赖→revert(只丢 X)，
            # 给 X 终态计入 completed，run 继续 merge→L2，终态 PARTIAL 诚实交付而非整任务 FAILED。
            _giveup = await _give_up_preserve_build(state, failed_ids)
            if _giveup is not None:
                return _giveup
            # 阶梯三也无法保 build 放弃（无 plan/无足迹）→ 兜底 escalate 失败子任务、【保留全部成功
            # 成果】，绝不全量 replan clobber（replan 治不了能力失败，只会推倒 N 个已完成重跑再失败）。
            logger.warning(
                "[HANDLE_FAILURE] 失败子任务 %s 耗尽重试但有 %d 个成功兄弟 → escalate 失败子任务、"
                "完整保留成果，绝不全量 replan 清空（治本：局部能力失败不推倒全盘）",
                failed_ids, len(succeeded_siblings),
            )
            return {
                # C9（4.9 复核 R-F6/H-F6）：补边必须在【所有】可达 return 回写 plan——
                # in-place 变异靠 checkpoint 捎带是被禁模式（重启即丢边，白跑复发）。
                **({"plan": plan_obj} if _c9_edges else {}),
                "subtask_results": subtask_results,
                "failed_subtask_ids": failed_ids,
                "failure_escalated": True,
                "failure_strategy": "escalate",
                "l2_passed": False,
                "replan_count": state.get("replan_count", 0),
            }

        for fid in failed_ids:
            subtask_results.pop(fid, None)
        # P0-2 熔断：replan 不能无限重入。每次 replan 计数，超过上限直接升级人工，
        # 而非继续 PLAN→ELABORATE→（可能同样的坏计划）→再失败，最终撞穿 recursion_limit
        # （见 task 0f93f1fc：replan 后又拆出同样的悬空依赖）。
        replan_count = state.get("replan_count", 0) + 1
        max_replan = get_config().model.max_retries  # 复用 max_retries（默认 2）
        if replan_count > max_replan:
            logger.warning(
                "[HANDLE_FAILURE] replan 已达上限(%d 次)仍失败 → 升级人工审核（避免无限重规划）",
                max_replan,
            )
            return {
                # C9（4.9 复核 R-F6/H-F6）：补边必须在【所有】可达 return 回写 plan——
                # in-place 变异靠 checkpoint 捎带是被禁模式（重启即丢边，白跑复发）。
                **({"plan": plan_obj} if _c9_edges else {}),
                "subtask_results": subtask_results,
                "failed_subtask_ids": failed_ids,
                "failure_escalated": True,
                "failure_strategy": "escalate",
                "l2_passed": False,
                "replan_count": replan_count,
            }
        # P0-2 携带失败原因：把本轮失败详情注入 state，供 PLAN 重新规划时参考，
        # 避免 LLM 看不到失败原因而原样重生成同一个坏计划。
        replan_feedback = (result.get("reasoning") or "").strip()
        if _llm_missing_files:
            # B3-3 对抗复核 minor：真 replan 时把结构化缺失文件清单显式带给 PLAN
            # （不靠 LLM reasoning 散文复述）——新计划必须给这些文件安排 owner。
            replan_feedback = (replan_feedback + "\n[结构化缺口] 以下文件在旧计划中无人创建，"
                               "新计划必须为其安排 create_files owner: "
                               + ", ".join(_llm_missing_files)).strip()
        logger.info(
            "[HANDLE_FAILURE] 策略=replan（第 %d/%d 次）— 清除失败结果，触发重新规划%s",
            replan_count, max_replan,
            "（已携带失败原因供 PLAN 参考）" if replan_feedback else "",
        )
        return {
            # C9（4.9 复核 R-F6/H-F6）：补边必须在【所有】可达 return 回写 plan——
            # in-place 变异靠 checkpoint 捎带是被禁模式（重启即丢边，白跑复发）。
            **({"plan": plan_obj} if _c9_edges else {}),
            "subtask_results": subtask_results,
            "failed_subtask_ids": [],
            "plan_valid": False,
            "failure_strategy": "replan",
            "failure_escalated": False,  # 批4c：非 escalate 决策清历史粘滞标记（取证 CONFIRMED，见 DEVLOG）
            "replan_count": replan_count,
            "replan_feedback": replan_feedback,
            # round36 #9 治本：执行失败 replan 同理给全新 plan 校验重试预算（清零 plan_retry_count），
            # 别继承覆盖重试已耗尽的额度。replan_count(独立熔断)封顶总次数。
            "plan_retry_count": 0,
            "plan_validation_prev_structural": {},  # R64-T3 猎手 F1：新周期必须清结构签名（防相邻巧合误熔断）
            # A3 sibling（2026-07-09 登记册）：与 L2 出口对称清旧覆盖 issue，防污染新规划。
            "plan_validation_feedback": "",
        }

    if strategy == "escalate":
        logger.info("[HANDLE_FAILURE] 策略=escalate — 上报人工审核")
        return {
            # C9（4.9 复核 R-F6/H-F6）：补边必须在【所有】可达 return 回写 plan——
            # in-place 变异靠 checkpoint 捎带是被禁模式（重启即丢边，白跑复发）。
            **({"plan": plan_obj} if _c9_edges else {}),
            "failure_escalated": True,
            "failure_strategy": "escalate",
            "l2_passed": False,
            "failed_subtask_ids": failed_ids,
        }

    # ── P2：瞬时(transient)失败优先走退避重试，与 capability 配额隔离 ──
    # 背景(task 37460a5b)：Connection error/Internal Server Error 等基础设施抖动，过去与
    # 拒答/空 diff 等能力问题混在同一条 retry 阶梯，0.8s 内连撞两次烧光配额直接 escalate。
    # 现在：本批若【全部】是 transient 失败 → 走带指数退避的轻量重试(独立计数器，上限 3)，
    # 不消耗 capability 的 subtask_retry_counts。一旦混入 capability 失败，则交给下方阶梯
    # (capability 才是该换模型/升级的真问题，不能被 transient 掩盖)。
    from swarm.models.errors import TRANSIENT, classify_failure, backoff_seconds

    def _failure_class_of(fid: str) -> str | None:
        out = subtask_results.get(fid)
        details: dict = {}
        summary = ""
        if isinstance(out, WorkerOutput):
            details = out.l1_details or {}
            summary = out.summary or ""
        elif isinstance(out, dict):
            details = out.get("l1_details", {}) or {}
            summary = out.get("summary", "") or ""
        fc = details.get("failure_class")
        if fc:
            return fc
        # 兜底：文本再分类（worker 未显式标注时）。B8（2026-07-09 登记册）：error 是原始
        # 异常文本（最可靠）优先判，判不出再退叙述性 summary——原 `summary or error` 让
        # 非空叙述遮蔽 error 里的真 transient 特征，误入 capability 阶梯。
        return classify_failure(details.get("error")) or classify_failure(summary)

    failure_classes = {fid: _failure_class_of(fid) for fid in failed_ids}
    transient_ids = [fid for fid, fc in failure_classes.items() if fc == TRANSIENT]
    MAX_TRANSIENT_RETRY = 3

    # B2（round38c st-3-1 七轮同输入白跑治本）：BLOCKED 失败指纹。pipeline_blocked 类
    # transient 是确定性构建闸的产物——同签名（同阻断类型+同缺失文件/包集合）重派=
    # 同输入必然同结果。B1 派发注入使"上游 owner 后续完成"时签名自然变化（产物进
    # seed）；全批 blocked 且两连不变=阻断源未解 → 跳过 transient 退避直落 capability
    # 阶梯；三连不变 → 视同重试耗尽进阶梯终局分支（部分交付/abandon 优先于 escalate，
    # 对抗复核 4b：直接 escalate 会抢占 PARTIAL 出口把终态硬化）。
    # 精化（对抗复核 4a）：短路条件=【全部】transient 都是 blocked 且各自连击达标——
    # 混入网络抖动/流式 stall 的批绝不连坐（同输入重试正是其正确语义）。
    _blk_sigs = dict(state.get("subtask_block_signatures", {}))
    _blk_counts: dict[str, int] = {}
    for fid in transient_ids:
        _bd = _det_of(subtask_results.get(fid))
        _pb = _bd.get("pipeline_blocked")
        if _pb:
            # 原 B2：blocked 指纹（格式逐字节不动，守既有测试/跨轮签名兼容）
            _sig = str(_pb) + "|" + ",".join(sorted(
                [str(x) for x in (_bd.get("blocked_on_files") or [])]
                + [str(x) for x in (_bd.get("blocked_on_packages") or [])]
                + [str(x) for x in (_bd.get("blocked_on_modules") or [])]))
        else:
            # R65E8-T2（round65e8 死因·纵深防御）：非 blocked 的 transient 中，仅【compile 阶段 build_fail】
            # 才签名追踪。★复核 HIGH 收窄★：
            # - verify_failed 的 det_fail_reason 键在【命令串】非失败内容→跨轮恒定，无法辨"签名变=有进展"
            #   （sibling 落地后同命令换个原因失败仍同签名）→会误短路"等 sibling"的合法重试；且 verify/test
            #   阶段【无】sibling-wait→pipeline_blocked 检测（仅 compile 阶段有）。test_fail 前 160 字常是
            #   runner banner 样板→不同真失败塌成同签名。故【排除 verify_failed/test_fail/scope 等】。
            # - build_fail 是【内容型】(编译器错误文本，归一后版本/行号抖动无害)：真进展→错误变→签名变→连击归 1；
            #   且 compile 阶段的 sibling-wait 已被 internal_pkg_not_built/upstream_module_broken 路由成
            #   pipeline_blocked（走上支）→到这里的 build_fail 是【非等待、同输入同输出】的真确定性失败，安全。
            # - st-3/4/5 的 verify_failed storm 已由 R65E8-T1(验收命令 reactor 归一)从源头治，本 T2 不必兜它。
            # 纯 infra/网络(无 det_fail_reason)恒不追踪——同输入重试正是其正确语义。env 关：SWARM_TRANSIENT_DET_PLATEAU=0。
            _dfr = str(_bd.get("det_fail_reason") or "").strip()
            if (not _dfr or not _dfr.startswith("build_fail")
                    or os.environ.get("SWARM_TRANSIENT_DET_PLATEAU", "1").strip().lower()
                    in ("0", "false", "no", "off")):
                continue
            _sig = "det|" + _normalize_fail_sig(_dfr)
            logger.info("[HANDLE_FAILURE] R65E8-T2 %s compile build_fail 确定性指纹追踪（第 %d 连）：%s",
                        fid, int((_blk_sigs.get(fid) or {}).get("count", 0)) + 1
                        if (_blk_sigs.get(fid) or {}).get("sig") == _sig else 1, _sig[:80])
        _prev = _blk_sigs.get(fid) or {}
        _cnt = int(_prev.get("count", 0)) + 1 if _prev.get("sig") == _sig else 1
        _blk_sigs[fid] = {"sig": _sig, "count": _cnt}
        _blk_counts[fid] = _cnt
    # _all_blocked：本批 transient 是否【全部】有稳定重复指纹（blocked 或确定性闸失败）——纯 infra
    # transient 不入 _blk_counts，故混入 infra 即 False→照常退避重试（不误短路网络抖动，R65E8-T2）。
    _all_blocked = bool(transient_ids) and set(_blk_counts) == set(transient_ids)
    _sig_skip = _all_blocked and min(_blk_counts.values()) >= 2
    _sig_exhausted = _all_blocked and min(_blk_counts.values()) >= 3 \
        and len(transient_ids) == len(failed_ids)

    # 仅当本批失败【全部】为 transient 时才走退避快路（混入 capability 则不抢占阶梯）。
    if transient_ids and len(transient_ids) == len(failed_ids) and not _sig_exhausted:
        transient_counts = dict(state.get("subtask_transient_counts", {}))
        next_tcounts = {fid: transient_counts.get(fid, 0) + 1 for fid in transient_ids}
        deepest_t = max(next_tcounts.values(), default=0)
        if _sig_skip:
            # B2：全批 blocked 同签名二连——transient 退避对同输入无意义，直落 capability 阶梯
            logger.warning(
                "[HANDLE_FAILURE] B2/T2 失败指纹二连不变（全批 blocked 或确定性闸假阴性）→ 跳过 transient 退避，"
                "直落 capability 阶梯: %s", transient_ids)
        elif deepest_t <= MAX_TRANSIENT_RETRY:
            # 治本 C：流式 stall（模型服务并发拥塞）立即重试会撞同一拥塞 → 给【更长退避】让拥塞散去
            # （8/16/32s）；普通 transient（连接抖动/5xx）恢复快，沿用短退避（2/4/8s）。两者都【不换模型】
            # （use_alternate_model=False）——是基建瞬时不是模型弱。
            _stall = _has_stream_stall(subtask_results, transient_ids)
            delay = backoff_seconds(deepest_t, base=8.0, cap=60.0) if _stall else backoff_seconds(deepest_t)
            logger.info(
                "[HANDLE_FAILURE] 策略=retry(transient%s 退避，第 %d/%d 次，sleep %.1fs，不换模型/不计 capability 配额): %s",
                "·流式stall" if _stall else "", deepest_t, MAX_TRANSIENT_RETRY, delay, transient_ids,
            )
            await asyncio.sleep(delay)
            dispatch_remaining = list(state.get("dispatch_remaining", []))
            for fid in transient_ids:
                subtask_results.pop(fid, None)
                if fid not in dispatch_remaining:
                    dispatch_remaining.append(fid)
            return {
                "dispatch_remaining": dispatch_remaining,
                "failed_subtask_ids": [],
                "subtask_results": subtask_results,
                "failure_strategy": "retry",
                "failure_escalated": False,  # 批4c：非 escalate 决策清历史粘滞标记（取证 CONFIRMED，见 DEVLOG）
                "subtask_use_alternate": _alt_map_update(state, transient_ids, False),
                "subtask_transient_counts": {**transient_counts, **next_tcounts},
                "subtask_block_signatures": _blk_sigs,  # B2：指纹持久化（同签名连击跨轮计数）
                # C9：补了动态依赖边必须回写 plan（dispatch 依赖闸消费）
                **({"plan": plan_obj} if _c9_edges else {}),
            }
        # transient 退避也用尽 → 落入下方 capability 阶梯（基础设施持续不可用，升级人工）
        logger.warning(
            "[HANDLE_FAILURE] transient 退避重试已达上限(%d 次)仍失败，转入 capability 阶梯: %s",
            MAX_TRANSIENT_RETRY, transient_ids,
        )

    # retry / retry_alternate — 确定性递进升级（覆盖 LLM 决策，防止无限重试）
    #
    # 设计文档要求"重试最多 2 次 → 换模型 → 上报人工"，但原实现完全依赖 LLM 单次
    # 决策，LLM 可能持续输出 retry 导致死循环。这里引入每子任务的确定性重试计数器，
    # 强制执行升级阶梯：
    #   retry_count < max_retries        → retry        (普通重试)
    #   retry_count == max_retries       → retry_alternate (换备选模型)
    #   retry_count > max_retries        → escalate     (上报人工)
    # LLM 仍可主动选择 replan/escalate（上面已处理），但 retry 类不会突破硬上限。
    max_retries = get_config().model.max_retries  # 默认 2
    retry_counts = dict(state.get("subtask_retry_counts", {}))

    # 计算本批失败子任务里"最深"的重试次数，决定整批升级档位
    next_counts = {fid: retry_counts.get(fid, 0) + 1 for fid in failed_ids}
    deepest = max(next_counts.values(), default=0)
    # A2（round48c 实锤）：终身派发硬熔断——retry_counts 会被签名剪枝重置（scope
    # 加宽/replan 改签名），st-14-1 实跑 11 次烧掉 2.8h 槽位。terminal 兜底：任一
    # 失败者终身派发数 ≥ 上限 → 视同重试耗尽进终局分支（部分交付/abandon 优先，
    # 与 B2 三连同构），任何签名重置都救不活它。
    try:
        _lifetime_cap = int(os.environ.get("SWARM_SUBTASK_MAX_DISPATCH_TOTAL", "6"))
    except (TypeError, ValueError):
        _lifetime_cap = 6
    _totals = state.get("subtask_dispatch_totals") or {}
    _over_cap = [fid for fid in failed_ids
                 if int(_totals.get(fid, 0)) >= _lifetime_cap > 0]
    if _over_cap:
        logger.warning(
            "[HANDLE_FAILURE] A2 终身派发熔断：%s 已派 ≥%d 次（签名重置免疫账本）→ "
            "视同重试耗尽进终局分支", _over_cap, _lifetime_cap)
        deepest = max(deepest, max_retries + 2)
    if _sig_exhausted:
        # B2 三连不变：确定性阻断对同输入已白跑 transient+阶梯——视同重试耗尽进终局
        # 分支（下方 deepest>max+1：有完成产物 → abandon+PARTIAL 部分交付；无 → escalate）。
        # 不直接 escalate：那会抢占部分交付出口把本可 PARTIAL 的终态硬化（对抗复核 4b）。
        logger.warning(
            "[HANDLE_FAILURE] B2 失败指纹三连不变（transient+阶梯均对同输入白跑）→ "
            "视同重试耗尽进终局分支: %s", failed_ids)
        deepest = max(deepest, max_retries + 2)

    # FINDING-12：拒答/步数耗尽(refusal_hard_fail)的子任务，重试强制走【最强模型】(40B 256k)，
    # 而非更弱 fallback——步数耗尽是小模型 agent 循环不收敛，换更弱只会更糟。
    # R63-T7：复读退化(degeneration_hard_fail)同通路——round63 实锤 st-2-1-1-2 同模型
    # 跨 4 次重启反复复读（同上下文大概率复现），链内 fallback 已试过更弱备选仍冒泡到
    # 这里，只有升最强模型才有意义。
    force_strong = dict(state.get("subtask_force_strong", {}))
    # ── #33-闸2：模型退役——复读退化在最强模型上【重复】发生 → 换异构备选（问题③治本）──
    # R63-T7 首次复读退化升最强模型（偶发退化换弱备更糟），此处保留。但 round65e13/round63
    # 死型是【最强模型本身退化】：force_strong 恒钉 routing_complex 且 dispatch line 641 的
    # `not _fs` 把 use_alternate 吞掉 → 退化模型永不退役、同上下文反复复读。故：degeneration
    # 且【上一轮已 force_strong】（=已升过最强仍退化）者，判定最强模型不胜任 → 换异构备选、
    # 清 force_strong（让 dispatch 换 provider），不再复用退化模型。首次退化仍走 R63-T7。
    _prev_fs = state.get("subtask_force_strong") or {}
    _degen_retire: list = []
    _self_forced_fids: set = set()   # #33-F3：本轮由 refusal/degeneration 源置 force_strong 的 fid
    for _fid in failed_ids:
        _res = subtask_results.get(_fid)
        _src = (getattr(_res, "l1_details", {}) or {}).get("l1_decision_source") if _res else None
        if _src in ("refusal_hard_fail", "degeneration_hard_fail"):
            force_strong[_fid] = True
            _self_forced_fids.add(_fid)   # F3：只有这些 fid 的 force_strong 是本文件自己置的，可清
        if _src == "degeneration_hard_fail" and _prev_fs.get(_fid):
            _degen_retire.append(_fid)

    if deepest > max_retries + 1:
        # ── #33-闸1：病灶优先换备选（round65e13 head-of-line 连坐死型·问题②治本）──
        # round65e13：连坐根（写坏 reactor SPOF 的脚手架子任务）多次 brain 级重试全同模型，
        # 从没换过异构备选——因 retry_counts 被签名剪枝反复重置（scope 加宽/replan 改签名），
        # 病灶永远够不到 retry_count==max_retries→retry_alternate 那一档（1920-1922 档位注释），
        # 而 B2 指纹三连(_sig_exhausted)把 deepest 直跳 max_retries+2 进终局，越过换备选格。
        # 治本：进终局(escalate/abandon)前，对【经指纹三连进终局、且从未换过备选】的连坐根
        # （非 pipeline_blocked 受害者），给【一次】retry_alternate 再回 DISPATCH。
        #
        # F5（silent-hunter MED）：触发条件【仅 _sig_exhausted】（组织性耗尽=同输入确定性
        # 白跑，安全换模型），【不含 _over_cap】——_over_cap（终身派发≥6）是硬资源天花板铁律
        # （A2 round48c 2.8h 空烧），必须绝对，绝不因闸1 再加派。
        # CRITICAL（code-reviewer）：判"从未换过"用【持久账本 subtask_alternate_ever_used】
        # （只增、dispatch 绝不消费、仅 replan 签名剪枝），不用 subtask_use_alternate（dispatch
        # :904 派出即清→无法辨"从未换过"vs"换过被消费"→每轮无界重触发架空 A2）。
        # F3（code-reviewer MED）：清 force_strong 只清【本轮 refusal/degeneration 源自置】者
        # （_self_forced_fids），绝不碰 E5(超大不可拆块)等其它来源置的 force_strong（否则超大块
        # 被降级弱模型）。无法判来源的病灶保留 force_strong（不清=换备选可能被 `not _fs` 吞掉，
        # 但资源正确性优先）。
        if _sig_exhausted:
            _ever_alt = state.get("subtask_alternate_ever_used") or {}
            # 闸1 目标=连坐根 ∩【模型可修】∩ 从未换过备选（回归实锤：infra/env 阻塞如
            # sandbox_env_probe_blocked 是根缺陷但换模型没用→不给闸1，走 partial/abandon）。
            # 闸3 的 _pd_roots 维持宽口径 _root_defect_ids（fail-closed 计量不变）。
            _never_alt_roots = [
                fid for fid in _root_defect_ids(failed_ids, subtask_results)
                if _is_model_fixable_defect(_l1_details_of(subtask_results, fid))
                and not _ever_alt.get(fid)]
            if _never_alt_roots and plan_obj is not None:
                _clearable = set(_never_alt_roots) & _self_forced_fids
                _fs_g1 = {k: v for k, v in force_strong.items() if k not in _clearable}
                _g1_remaining = list(state.get("dispatch_remaining") or [])
                _g1_sr = dict(subtask_results)
                for fid in failed_ids:
                    _g1_sr.pop(fid, None)
                    if fid not in _g1_remaining:
                        _g1_remaining.append(fid)
                logger.warning(
                    "[HANDLE_FAILURE] #33-闸1 病灶优先换备选：连坐根 %s 经指纹三连抄近道进终局、"
                    "从未换过异构备选（据持久账本）→ 给一次 retry_alternate（清本文件自置的 "
                    "force_strong=%s 让备选真生效）再回 DISPATCH，不直接 escalate/abandon"
                    "（round65e13 病灶永锁同模型死型治本；持久账本保证至多一轮、绝不无界重触发）",
                    _never_alt_roots, sorted(_clearable))
                return {
                    **({"plan": plan_obj} if _c9_edges else {}),
                    "dispatch_remaining": _g1_remaining,
                    "failed_subtask_ids": [],
                    "subtask_results": _g1_sr,
                    "failure_strategy": "retry_alternate",
                    "failure_escalated": False,
                    "subtask_use_alternate": _alt_map_update(state, _never_alt_roots, True),
                    # CRITICAL：同步写持久账本（wrapper chokepoint 亦会合并，此处显式冗余保险）
                    "subtask_alternate_ever_used": {
                        **{k: True for k in (state.get("subtask_alternate_ever_used") or {})},
                        **{fid: True for fid in _never_alt_roots}},
                    "subtask_force_strong": _fs_g1,
                    "subtask_retry_counts": {**retry_counts, **next_counts},
                    "subtask_block_signatures": _blk_sigs,
                }
        # 重试耗尽。【部分交付】：已有完成子任务 + 开启 partial → 放弃 failed(+传递依赖者)，
        # 继续交付其余，终态 PARTIAL(非 DONE，诚实未完成)。否则(0 完成 / 关闭 partial) →
        # 维持 escalate(整任务失败)，避免无产出却假成功。
        _abandoned_so_far = set(state.get("abandoned_subtask_ids") or [])
        _done = [tid for tid in subtask_results
                 if tid not in failed_ids and tid not in _abandoned_so_far]
        _allow_partial = getattr(get_config().worker, "allow_partial_delivery", True)
        if _allow_partial and _done and plan_obj is not None:
            # 传递放弃：依赖被放弃者的子任务也放弃(缺依赖跑不了)，避免它们永留 remaining 死循环
            _done_ok = {tid for tid, o in subtask_results.items()
                        if tid not in failed_ids and l1_passed(o)}
            abandoned = _transitive_abandon(
                plan_obj.subtasks, _abandoned_so_far | set(failed_ids),
                completed_ids=_done_ok)  # R51-1：completed 绝不入闭包
            # R65D-W2 猎手 CRITICAL（复现实锤）：消费边织密图后，单个高扇出生产者重试
            # 耗尽会沿新边一笔连坐全场闭包——本分支此前是四个 _transitive_abandon 消费点
            # 中唯一最常走且完全未设防的旁门（round65c 102/107 静默清盘死型复活路径）。
            _pd_new = abandoned - _abandoned_so_far
            # ── #33-闸3：规模闸计量口径 = 独立【根缺陷】数，非 blast-radius 闭包（问题①治本）──
            # round65e13：单个高扇出病灶（写坏 reactor SPOF）经 -am 连坐下游 50，_pd_new=50 撞
            # 阈值 escalate——但独立根缺陷只 1 个（其余全是自产物无错、仅被坏兄弟拖崩的
            # pipeline_blocked 受害者）。单病灶高扇出≠计划覆灭，应先走闸1 换备选/走部分交付，
            # 绝不因受害者众多误判 escalate。只有【独立根缺陷数】超阈值才算真·计划覆灭。
            # fail-closed 语义不变（仍 escalate 非静默 PARTIAL），只把计量从闭包收窄到根缺陷。
            # blast-radius(_pd_new)仍进日志，供复盘辨"根缺陷多"vs"单根高扇出"。
            _pd_roots = [f for f in _root_defect_ids(failed_ids, subtask_results)
                         if f in _pd_new or f in set(failed_ids)]
            if len(_pd_roots) > mass_abandon_cap(len(plan_obj.subtasks)):
                logger.error(
                    "[HANDLE_FAILURE] R65D-W2 规模闸（重试耗尽部分交付）：独立根缺陷 %d 超阈值 %d"
                    "（计划 %d，连坐闭包 blast-radius=%d，根缺陷=%s）→ escalate 人工，绝不静默"
                    "清盘成 PARTIAL；连坐名单=%s",  # R65TR-T4④：名单从不打印=复盘只能靠闭包倒推
                    len(_pd_roots), mass_abandon_cap(len(plan_obj.subtasks)),
                    len(plan_obj.subtasks), len(_pd_new), sorted(_pd_roots)[:40],
                    sorted(_pd_new)[:40])
                return {
                    **({"plan": plan_obj} if _c9_edges else {}),
                    "failure_strategy": "escalate",
                    "failure_escalated": True,
                    "failed_subtask_ids": failed_ids,
                    "subtask_retry_counts": {**retry_counts, **next_counts},
                    "degraded_reasons": [
                        f"mass_abandon_gate:{len(_pd_roots)}/{len(plan_obj.subtasks)}"],
                }
            _remaining = [t for t in (state.get("dispatch_remaining") or []) if t not in abandoned]
            logger.warning(
                "[HANDLE_FAILURE] 部分交付：放弃 %s(+依赖者，共 %d)，继续交付其余 %d 个，终态将 PARTIAL",
                failed_ids, len(abandoned), len(_remaining),
            )
            return {
                # C9（4.9 复核 R-F6/H-F6）：补边必须在【所有】可达 return 回写 plan——
                # in-place 变异靠 checkpoint 捎带是被禁模式（重启即丢边，白跑复发）。
                **({"plan": plan_obj} if _c9_edges else {}),
                "failure_strategy": "abandon",
                "failure_escalated": False,  # 批4c：非 escalate 决策清历史粘滞标记（取证 CONFIRMED，见 DEVLOG）
                "abandoned_subtask_ids": sorted(abandoned),
                "failed_subtask_ids": [],
                "dispatch_remaining": _remaining,
                "subtask_force_strong": force_strong,
                "subtask_retry_counts": {**retry_counts, **next_counts},
            }
        # 已用尽 retry + alternate 且无可交付/关闭 partial → 升级人工(整任务失败)
        logger.warning(
            "[HANDLE_FAILURE] 子任务重试已达上限（retry %d + alternate 1），升级人工审核: %s",
            max_retries, failed_ids,
        )
        return {
            # C9（4.9 复核 R-F6/H-F6）：补边必须在【所有】可达 return 回写 plan——
            # in-place 变异靠 checkpoint 捎带是被禁模式（重启即丢边，白跑复发）。
            **({"plan": plan_obj} if _c9_edges else {}),
            "failure_escalated": True,
            "failure_strategy": "escalate",
            "l2_passed": False,
            "failed_subtask_ids": failed_ids,
            "subtask_retry_counts": {**retry_counts, **next_counts},
        }

    # 将失败子任务重新加入 dispatch_remaining
    # round27：pop 前先留存 L1 详情——下方 _widen_scope_for_compile_repair 需要 build_output/
    # 编译标志判"根因在 scope 外"。旧序是先 pop 再取 → 恒空 dict → 加宽自引入以来从未生效
    # （RUN16 st-20 类死循环治本实际未通电）。
    saved_l1_details = {fid: _l1_details_of(subtask_results, fid) for fid in failed_ids}
    dispatch_remaining = list(state.get("dispatch_remaining", []))
    for fid in failed_ids:
        subtask_results.pop(fid, None)
        if fid not in dispatch_remaining:
            dispatch_remaining.append(fid)

    # 确定性档位：超过 max_retries 次普通重试后切换备选模型
    forced_alternate = deepest > max_retries
    effective_strategy = "retry_alternate" if forced_alternate else "retry"
    # 若 LLM 主动要求 retry_alternate 且尚未到 alternate 档，也尊重它（提前换模型）
    if strategy == "retry_alternate":
        effective_strategy = "retry_alternate"

    # ── 治本：编译失败根因在 scope 外(缺 pom 依赖/上游文件)→ 加宽 scope 让重试能真正修 ──
    _scope_widened = False
    if plan_obj is not None:
        for fid in failed_ids:
            new_files = _widen_scope_for_compile_repair(
                plan_obj, fid, saved_l1_details.get(fid, {}), subtask_results=subtask_results)
            if new_files:
                _scope_widened = True
                logger.info(
                    "[HANDLE_FAILURE] 编译修复加宽 scope：子任务 %s 纳入 %s（治根因在 scope 外的编译失败，使重试可改 pom/上游）",
                    fid, new_files,
                )

    out: dict = {
        "dispatch_remaining": dispatch_remaining,
        "failed_subtask_ids": [],
        "subtask_results": subtask_results,
        "failure_strategy": effective_strategy,
        "subtask_retry_counts": {**retry_counts, **next_counts},
        "subtask_force_strong": force_strong,  # FINDING-12：拒答子任务重试走最强模型
        "subtask_block_signatures": _blk_sigs,  # B2：指纹持久化（阶梯路径也计连击，三连升级）
        # 批4c：本返回 strategy 恒为 retry/retry_alternate（escalate 在上方早返回），
        # 非 escalate 决策清历史粘滞标记（取证 CONFIRMED，见 DEVLOG）
        "failure_escalated": False,
    }
    if _scope_widened or _c9_edges:
        out["plan"] = plan_obj  # 回写加宽后的 scope / C9 动态依赖边，dispatch 重试用
    if effective_strategy == "retry_alternate":
        out["subtask_use_alternate"] = _alt_map_update(state, failed_ids, True)
        # R65TR-T2 B2：换备宣称接 router 真相——dispatch E1 判据判无异构备选的难度
        # 实派仍是同模型+加步数，此处日志谎称"换备选"与实派永久不符（回放 st-2
        # L6454/6458 实锤，两轮误导复盘）。判据异常按"有备选"处理（只影响措辞）。
        try:
            from swarm.brain.nodes.dispatch import _has_hetero_alternate
            _by_id_alt = {st.id: st for st in (getattr(plan_obj, "subtasks", None) or [])}
            _no_alt = [fid for fid in failed_ids if not _has_hetero_alternate(
                getattr(_by_id_alt.get(fid), "difficulty", None))]
        except Exception:  # noqa: BLE001
            # 猎手 R65TR-T2：回退方向必须与 _has_hetero_alternate 自身惯例一致
            # （异常按【无备选】处理）——回退成 []=谎称全员有备选，恰是本处要治的谎。
            _no_alt = list(failed_ids)
        if _no_alt:
            logger.info(
                "[HANDLE_FAILURE] 策略=retry_alternate（第 %d 次）: %s"
                "（其中无异构备选→实派同模型+加步数: %s）",
                deepest, failed_ids, _no_alt,
            )
        else:
            logger.info(
                "[HANDLE_FAILURE] 策略=retry_alternate（第 %d 次，换备选模型）: %s",
                deepest, failed_ids,
            )
    else:
        out["subtask_use_alternate"] = _alt_map_update(state, failed_ids, False)
        logger.info(
            "[HANDLE_FAILURE] 策略=retry（第 %d/%d 次）: %s",
            deepest, max_retries, failed_ids,
        )
    # ── #33-闸2 应用：最强模型重复复读退化者换异构备选、清 force_strong（问题③治本）──
    # 置于最后：retry 分支的 _alt_map_update(..., False) 会清 failed_ids 的 alternate 标记，
    # 故必须在其后为 _degen_retire 重新置 True，并从 force_strong 摘除（否则 dispatch `not _fs`
    # 吞掉换备选）。首次退化不在 _degen_retire，force_strong 原样保留 → R63-T7 语义不回归。
    if _degen_retire:
        _fs2 = dict(out.get("subtask_force_strong") or force_strong)
        _alt2 = dict(out.get("subtask_use_alternate") or {})
        for _fid in _degen_retire:
            # F3（code-reviewer MED）：只清【本轮 degeneration 源自置】的 force_strong
            # （_degen_retire ⊆ _self_forced_fids 恒成立，显式 intersect 防未来改动破 provenance），
            # 绝不碰 E5 超大块等其它来源置的 force_strong。
            if _fid in _self_forced_fids:
                _fs2.pop(_fid, None)
            _alt2[_fid] = True
        out["subtask_force_strong"] = _fs2
        out["subtask_use_alternate"] = _alt2
        # CRITICAL：换备选写持久账本（防闸1 后续误判"从未换过"；wrapper chokepoint 亦合并）
        out["subtask_alternate_ever_used"] = {
            **{k: True for k in (state.get("subtask_alternate_ever_used") or {})},
            **{fid: True for fid in _degen_retire}}
        logger.warning(
            "[HANDLE_FAILURE] #33-闸2 模型退役：%s 在最强模型上重复复读退化 → 换异构备选"
            "（清 force_strong 让 dispatch 换 provider），不再复用退化模型", _degen_retire)
    return out


def audit_failure_disposition(state, result) -> None:
    """R65D-T1 处置完备性铁律（round65d 死因本体的结构性防复发闸）。

    唯一咽喉=handle_failure 包装：无论 _handle_failure_impl 走哪条分支（含未来新分支），
    出口必须满足两条不变量，违反即 fail-loud + 强制兜底，绝不静默：

    ①【入口失败数≡出口处置数】entry failed_subtask_ids 中的每个 fid，要么仍在出口
      failed_subtask_ids（保留失败态走 retry/escalate 面），要么进 dispatch_remaining
      （重派），要么进 abandoned（放弃），要么进 give_up_isolated_ids（阶梯三
      settled-with-product 终局，复核 CRITICAL：全仓其余消费者都把 abandoned∪give_up
      当"已终局勿动"集，铁律不认桩=毁桩+复活重派无界循环）。四无=掉账（st-26 本体：
      10:45 处理 4 只处置 3，僵尸 result 被 10:59 数成"完成态"，90/94 经 C9 汇流饿死）
      → ERROR + 强制回队 + 失败 result 出账 + degraded_reasons 机读留痕
      （failure_disposition_leak:<fid>）。两类整体豁免：通道未动（result 无
      failed_subtask_ids 键=全员仍在失败集）；strategy=replan/escalate（交棒 PLAN 全量
      重规划/人工，旧 fid 语义已尽，复核 HIGH：误报会给健康 replan 永久打上死因签名）。

    ②【处方↔派发闭环核销】本轮回队重派的 fid，其传递依赖必须终结在
      完成(l1_passed)/在队/仍失败(会再处置)/桩豁免(give_up_mode=stub)——命中已放弃
      （非桩）上游或【历史僵尸】（有失败 result 却不在队/不在失败集/未放弃——st-26
      形态本体，猎手 F3）=处方注定落空（10:59 "宣称重派 4 实派 0"形态），ERROR
      fail-loud + degraded_reasons（recovery_prescription_unsatisfiable:<fid>），绝不等
      25min 后 R13-4 终态才发声。只告警不改队列（放弃决策归下一轮失败处置/连坐面）。

    就地改 result（dict，checkpoint 前）；自身异常由调用方 fail-loud 记账兜底。
    """
    entry = [f for f in (state.get("failed_subtask_ids") or []) if f]
    if not entry or not isinstance(result, dict):
        return
    # 复核 HIGH：replan=交棒 PLAN 节点全量重规划（旧子任务 id 语义已尽，PLAN 重入自会
    # 重置 dispatch_remaining）；escalate=交棒人工。两者都是合法整体处置，绝不当掉账。
    _strategy = str(result.get("failure_strategy")
                    or state.get("failure_strategy") or "")
    # R65D-W3③：恒 INFO 处置总账（round65d 掉账要等终态复盘才发现——每轮一行机读账，
    # 陪跑镜像当场可见）。escalate/replan 交棒也记（覆盖全部出口）。
    _acc_q = set(result.get("dispatch_remaining")
                 if "dispatch_remaining" in result
                 else (state.get("dispatch_remaining") or []))
    _acc_ab = set(result.get("abandoned_subtask_ids") or [])
    _acc_sf = set(result.get("failed_subtask_ids")
                  if "failed_subtask_ids" in result else entry)
    logger.info(
        "[HANDLE_FAILURE] R65D-W3 处置总账：入口 %d → 重派 %d / 放弃 %d / 保留失败 %d"
        "（strategy=%s）: %s",
        len(entry), len([f for f in entry if f in _acc_q]),
        len([f for f in entry if f in _acc_ab]),
        len([f for f in entry if f in _acc_sf]),
        _strategy or "retry", entry[:8])
    if _strategy in ("replan", "escalate"):
        return
    plan_obj = result.get("plan") or state.get("plan")
    _sr_src = (result.get("subtask_results")
               if "subtask_results" in result else state.get("subtask_results")) or {}
    abandoned = (set(result.get("abandoned_subtask_ids") or [])
                 | set(state.get("abandoned_subtask_ids") or []))
    # 复核 CRITICAL：阶梯三 give-up（stub/revert 皆 settled 终局）=有效处置，与全仓其余
    # 消费者（dispatch._is_ready/graph/verify/runner）的 abandoned∪give_up 口径对齐。
    give_ups = (set(result.get("give_up_isolated_ids") or [])
                | set(state.get("give_up_isolated_ids") or []))
    queued = list(result.get("dispatch_remaining")
                  if "dispatch_remaining" in result
                  else (state.get("dispatch_remaining") or []))
    # ① 完备性
    if "failed_subtask_ids" in result:
        still_failed = set(result.get("failed_subtask_ids") or [])
        leaked = [f for f in entry
                  if f not in still_failed and f not in queued
                  and f not in abandoned and f not in give_ups]
        if leaked:
            logger.error(
                "[HANDLE_FAILURE] R65D-T1 处置完备性铁律：入口 %d 个失败、%d 个无处置 %s"
                "（不重派/不放弃/不保留失败态三无=掉账）→ 强制回队重派+失败 result 出账"
                "（round65d st-26 静默掉账饿死 90/94 的死因本体，绝不静默复发）",
                len(entry), len(leaked), leaked)
            _sr = dict(_sr_src)
            for f in leaked:
                if f not in queued:
                    queued.append(f)
                _sr.pop(f, None)
            result["dispatch_remaining"] = queued
            result["subtask_results"] = _sr
            result["degraded_reasons"] = (
                list(result.get("degraded_reasons") or [])
                + [f"failure_disposition_leak:{f}" for f in leaked])
            _sr_src = _sr
    # ② 处方核销：本轮回队者的传递依赖不得命中已放弃（非桩）上游
    requeued = [f for f in entry if f in queued]
    if not requeued or plan_obj is None:
        return
    from swarm.brain.nodes.shared import l1_details_of as _l1d
    from swarm.brain.nodes.shared import l1_passed as _l1p
    _stubbed = {
        sid for sid in give_ups
        if _l1p(_sr_src.get(sid))
        and (_l1d(_sr_src.get(sid)) or {}).get("give_up_mode") == "stub"
    }
    _dead = abandoned - _stubbed
    _failed_now = set(result.get("failed_subtask_ids")
                      if "failed_subtask_ids" in result
                      else (state.get("failed_subtask_ids") or []))

    def _is_dead_dep(d: str) -> bool:
        if d in _dead:
            return True
        # 猎手 F3：历史僵尸——有失败 result 却不在队/不在失败集/未终局（st-26 形态本体，
        # 可能来自本闸上线前的轮次或外部通道）＝这条依赖永远不会被满足。
        return (d in _sr_src and not _l1p(_sr_src.get(d))
                and d not in queued and d not in _failed_now
                and d not in give_ups and d not in abandoned)

    _by_id = {s.id: s for s in (getattr(plan_obj, "subtasks", None) or [])}
    _bad: dict[str, list[str]] = {}
    for f in requeued:
        seen: set[str] = set()
        stack = list(getattr(_by_id.get(f), "depends_on", []) or [])
        hits: list[str] = []
        while stack:
            d = stack.pop()
            if d in seen:
                continue
            seen.add(d)
            if _is_dead_dep(d):
                hits.append(d)
                continue
            stack.extend(list(getattr(_by_id.get(d), "depends_on", []) or []))
        if hits:
            _bad[f] = sorted(hits)
    if _bad:
        logger.error(
            "[HANDLE_FAILURE] R65D-T1 处方核销：重派进队的 %s 传递依赖已放弃（非桩）上游 %s"
            "——处方注定落空、依赖闸永不放行（round65d '宣称重派 4 实派 0' 形态）。"
            "fail-loud 留痕交下一轮失败处置/连坐面裁决，绝不静默等饿死",
            sorted(_bad), sorted({d for ds in _bad.values() for d in ds}))
        result["degraded_reasons"] = (
            list(result.get("degraded_reasons") or [])
            + [f"recovery_prescription_unsatisfiable:{f}" for f in sorted(_bad)])
