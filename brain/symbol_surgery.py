"""R39-2（round39 治本批）：C1 缺 owner 契约符号的确定性外科挂靠。

round39 死因（TASK_REGISTER R39-1 取证）：C1 契约符号对账打回后无符号类修复通道，
LLM 全量重拆三轮缺口 71→71→68/103 不动（D09 裸文本 issues 对符号缺口无效）——
批拆 prompt 按 F8 亲和子集注入，LLM 收不到"缺 owner 符号分给哪个批"的结构化分配。

治本（零 LLM，纯函数可单测）：
  1. 存量优先——基线树已有 `<Symbol>.<ext>` 同名文件的符号=存量承接（棕地误伤面），
     不挂子任务，报告 baseline_owned（C1 侧同步豁免，见 validate_contract_ownership）。
  2. 确定性挂靠——契约条目自带 module 归属（_merge_module_contracts D10 合并键），
     子任务模块归属由 scope 文件路径首段推导；缺 owner 符号点名进【同模块】子任务的
     contract["symbols"]（C1 语料词边界即命中，与闸同口径）。
  3. 防毒映射（#28 教训）——无模块归属/无同模块候选绝不猜挂，留 remainder 如实上报；
     单子任务挂靠量有上限，防 68 符号全倒进一个 st。

口径纪律：unowned 判定复用 plan_validator.unowned_contract_symbols，符号提取复用
contract_utils.contract_symbols_with_module——与 C1 闸单一事实源，挂完闸必过。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 单子任务挂靠上限默认值：够承接一个模块的接口簇，又不至于把整批缺口倒进一个 st
_DEFAULT_MAX_PER_SUBTASK = 8


# 模块根构建清单（不计入挂靠候选权重）：只会产出构建文件的子任务（如 R39-4 注入的
# 脚手架）结构上永远实现不了接口——把符号挂给它=幻影 ownership 骗过 C1，两张皮复活
# （对抗复核 CONFIRMED，带复现）。栈公平的小集合，与规则5 的 Maven 决策同族。
_BUILD_MANIFESTS = {"pom.xml", "build.gradle", "build.gradle.kts",
                    "settings.gradle", "package.json"}


def _subtask_modules(st) -> dict[str, int]:
    """子任务模块归属推导：scope 写目标路径首段 → {模块: 可实现文件数}。

    与契约 module 字段同粒度（Maven 多模块/monorepo 顶层目录名）。无 '/' 的
    根文件（如根 pom.xml）不计；模块根构建清单（<module>/pom.xml 等）不计权重
    ——确保"只写构建文件"的脚手架子任务不会成为符号挂靠候选。"""
    counts: dict[str, int] = {}
    sc = getattr(st, "scope", None)
    for f in (list(getattr(sc, "create_files", None) or [])
              + list(getattr(sc, "writable", None) or [])):
        p = str(f).replace("\\", "/").lstrip("/")
        if "/" not in p:
            continue
        mod, rest = p.split("/", 1)
        mod = mod.strip()
        if not mod:
            continue
        if rest.strip() in _BUILD_MANIFESTS:
            continue  # 模块根构建清单：不构成"能实现符号"的证据
        counts[mod] = counts.get(mod, 0) + 1
    return counts


def surgical_symbol_attach(
    plan,
    shared_contract: dict[str, Any] | None,
    project_path: str | None = None,
    max_per_subtask: int = _DEFAULT_MAX_PER_SUBTASK,
) -> dict[str, Any]:
    """把 C1 无 owner 契约符号确定性挂靠到同模块子任务（就地改 plan）。

    返回机读报告 {"attached": {symbol: st_id}, "baseline_owned": [...],
    "remainder": [...]}——remainder 非空且仍超 C1 阈值时由调用方决定后续
    （回退全量重拆），本函数绝不猜挂。幂等：已 owned 符号（含上次挂靠的）跳过。
    """
    from swarm.brain.contract_utils import (
        baseline_symbol_files,
        contract_symbols_with_module,
    )
    from swarm.brain.plan_validator import unowned_contract_symbols

    report: dict[str, Any] = {"attached": {}, "baseline_owned": [], "remainder": []}
    entries = contract_symbols_with_module(shared_contract or {})
    subtasks = list(getattr(plan, "subtasks", None) or [])
    if not entries or not subtasks:
        return report

    symbols = [e["symbol"] for e in entries]
    module_of = {e["symbol"]: e["module"] for e in entries}
    unowned = unowned_contract_symbols(plan, symbols)
    if not unowned:
        return report

    # 1) 存量豁免：基线树同名文件承接（棕地），与 C1 的豁免同一判据函数
    base_hits = baseline_symbol_files(unowned, project_path)
    if base_hits:
        report["baseline_owned"] = [s for s in unowned if s in base_hits]
        unowned = [s for s in unowned if s not in base_hits]

    # 2) 模块 → 候选子任务索引（按该模块文件数降序、st id 升序，确定性排序）
    st_mods: dict[str, dict[str, int]] = {st.id: _subtask_modules(st) for st in subtasks}
    st_by_id = {st.id: st for st in subtasks}
    attach_load: dict[str, int] = {st.id: 0 for st in subtasks}

    def _candidates(mod: str) -> list[str]:
        cands = [sid for sid, mods in st_mods.items() if mod in mods]
        return sorted(cands, key=lambda sid: (-st_mods[sid][mod], sid))

    # 3) 逐符号确定性挂靠；无模块/无候选/候选全满 → remainder（绝不猜挂）
    for sym in unowned:
        mod = (module_of.get(sym) or "").strip()
        target = None
        if mod:
            for sid in _candidates(mod):
                if attach_load[sid] < max(int(max_per_subtask), 1):
                    target = sid
                    break
        if target is None:
            report["remainder"].append(sym)
            continue
        st = st_by_id[target]
        if st.contract and not isinstance(st.contract, dict):
            # hunter④：非 dict contract 被重建=原内容静默丢失，留痕（当前无写入方
            # 会造成此态，纵深防御）
            logger.warning("[SYMBOL-SURGERY] 子任务 %s contract 非 dict(%s)，重建为 dict",
                           st.id, type(st.contract).__name__)
        contract = st.contract if isinstance(st.contract, dict) else {}
        syms = contract.setdefault("symbols", [])
        if sym not in syms:
            syms.append(sym)
        st.contract = contract
        attach_load[target] += 1
        report["attached"][sym] = target

    if report["attached"] or report["baseline_owned"] or report["remainder"]:
        logger.info(
            "[SYMBOL-SURGERY] 挂靠 %d / 存量承接 %d / 无法确定性归属 %d（详见报告）",
            len(report["attached"]), len(report["baseline_owned"]),
            len(report["remainder"]))
    return report


# C1/规则5 打回文案的符号类指纹（与 plan_validator 消息同源；改文案两处同改）
_SYMBOL_ISSUE_MARKERS = ("契约符号无 owner", "规则5")

# R40-1 缺件类指纹（与 validate_file_plan_ownership 消息同源）
_FILEPLAN_ISSUE_MARKER = "file_plan 文件无 owner"


def attach_orphan_file_plan_entries(plan, file_plan_paths) -> tuple[int, list[str]]:
    """R41-1 共享内核：file_plan 孤儿文件按【同顶层模块 + 共享路径前缀最深】确定性
    挂靠到子任务 create_files（零 LLM、幂等）。

    round41 死因：孤儿挂靠算法只活在 maybe_file_plan_repair 的互斥通道里
    （task_plan is None 才走），P1 覆盖外科抢跑后缺件带病重验，最后一轮重试
    原地复死——一个 sql 文件杀掉 90 子任务计划。抽成共享内核后：
    - 外科通道保持 strict 语义（有挂不上的 → 调用方整体回退全量重拆）；
    - PLAN 确定性收尾器 fail-open 语义（挂不上的留给 VALIDATE 如实打回）。
    返回 (挂上数, 挂不上清单)。构建清单-only 脚手架不作候选（同 R39 CRITICAL 教训，
    _subtask_modules 已滤权重）。
    """
    owned: set[str] = set()
    for st in plan.subtasks:
        sc = getattr(st, "scope", None)
        for f in (list(getattr(sc, "create_files", None) or [])
                  + list(getattr(sc, "writable", None) or [])):
            owned.add(str(f).replace("\\", "/").lstrip("/"))
    missing = [f for f in (file_plan_paths or [])
               if f.replace("\\", "/").lstrip("/") not in owned]

    def _prefix_depth(a: str, b: str) -> int:
        pa, pb = a.split("/"), b.split("/")
        n = 0
        for x, y in zip(pa, pb):
            if x != y:
                break
            n += 1
        return n

    attached, left = 0, []
    for f in missing:
        # 口径同源（复核 HIGH 修）：file_plan 已过 P5 权威去重，走到这的全是真缺件
        # ——绝不再按 basename 豁免（自造豁免会静默放行"不同模块同名各建"的合法缺件）
        p = f.replace("\\", "/").lstrip("/")
        mod = p.split("/", 1)[0] if "/" in p else ""
        best, best_key = None, (-1, -1)
        for st in plan.subtasks:
            mods = _subtask_modules(st)
            if not mod or mod not in mods:
                continue
            sc = st.scope
            depth = max((_prefix_depth(p, str(w).replace("\\", "/").lstrip("/"))
                         for w in (list(sc.create_files) + list(sc.writable))),
                        default=0)
            key = (depth, mods[mod])
            if key > best_key:
                best, best_key = st, key
        if best is None:
            left.append(f)
            continue
        if p not in best.scope.create_files:
            best.scope.create_files.append(p)
            # 复核 F3：只挂 scope 不挂意图=worker prompt 永不提及该文件，L1 只拦
            # "零 diff"不拦"缺单个文件"→ 静默丢交付物直通 DONE。挂靠必须同步注入
            # 描述+验收标准，让 worker 拿到"要产出这个文件"的明示意图。
            best.description = (best.description or "") + (
                f"\n【收尾器挂靠】file_plan 规划了 {p} 但无子任务认领，"
                f"按模块归属由你产出：必须创建该文件并实现其应有内容。")
            if p not in " ".join(best.acceptance_criteria or []):
                best.acceptance_criteria = list(best.acceptance_criteria or []) + [
                    f"文件 {p} 已创建且内容完整"]
        # 复核 F1：记录挂靠（#6 覆盖单调化 scope 身份配对两侧对称剔除，防键漂移丢 covers）
        rec = plan.finisher_attached.setdefault(best.id, [])
        if p not in rec:
            rec.append(p)
        owned.add(p)
        attached += 1
    return attached, left


def maybe_file_plan_repair(state, project_path: str | None = None):
    """R40-1(b)：file_plan 缺件类校验失败 → 确定性挂靠修复，不全量重拆。

    round40 死因：批拆丢 3 件（两个 ServiceImpl+DDL）规划期无校验。修复规则
    （零 LLM）：deepcopy 上一版 plan，每个缺件挂到【同顶层模块 + 共享路径前缀
    最深】的子任务 create_files（构建清单-only 脚手架不作候选，同 R39 CRITICAL
    教训）；全部挂上且闸复核通过才放行；有缺件挂不上如实 None 回退全量重拆。
    守卫与 maybe_symbol_repair 同族（F-3/缺模块让位；泄压阀共用
    SWARM_PLAN_SYMBOL_SURGERY——同为"规划期确定性外科"面）。
    """
    import os

    if not (state.get("plan_validation_feedback") or "").strip():
        return None
    _blob = " ".join(str(i) for i in (state.get("plan_validation_issues") or [])) \
        + " " + (state.get("plan_validation_feedback") or "")
    if _FILEPLAN_ISSUE_MARKER not in _blob:
        return None
    if os.environ.get("SWARM_PLAN_SYMBOL_SURGERY", "1").strip().lower() in (
            "0", "false", "no", "off"):
        logger.info("[FILEPLAN-SURGERY] 泄压阀 off → 旧行为全量重拆")
        return None
    if (state.get("replan_feedback") or "").strip():
        logger.info("[FILEPLAN-SURGERY] 存在执行失败 replan_feedback（F-3）→ 让位")
        return None
    if state.get("plan_batch_failed_modules"):
        logger.info("[FILEPLAN-SURGERY] 整模块分解失败 → 让位全量重拆")
        return None
    prior = state.get("plan")
    from swarm.brain.nodes.shared import _task_requests_tests
    from swarm.brain.plan_validator import normalized_file_plan_paths
    # R41 复核 F2：分母口径与 VALIDATE/_strip_unrequested_tests 三方对称
    _excl_tests = not _task_requests_tests(state.get("task_description") or "")
    file_plan = normalized_file_plan_paths(
        state.get("tech_design_file_plan"), exclude_test_paths=_excl_tests)
    if prior is None or not getattr(prior, "subtasks", None) or not file_plan:
        logger.warning("[FILEPLAN-SURGERY] 缺件类失败但无上一版 plan/file_plan → 全量重拆")
        return None
    from swarm.brain.plan_validator import (
        validate_file_plan_ownership,
        validate_plan_structure,
    )
    if not validate_plan_structure(prior).valid:
        logger.warning("[FILEPLAN-SURGERY] 上一版结构不合法 → 结构失败归全量重拆")
        return None
    verdict0 = validate_file_plan_ownership(prior, file_plan,
                                            exclude_test_paths=_excl_tests)
    if verdict0.valid:
        return None  # 缺件已不存在（别的维度失败），不越权
    candidate = prior.model_copy(deep=True)
    attached, left = attach_orphan_file_plan_entries(candidate, file_plan)
    if left:
        # strict 语义：有挂不上的缺件 → 整体回退全量重拆（半修不放行）
        logger.warning("[FILEPLAN-SURGERY] 缺件 %s 无同模块候选 → 回退全量重拆", left[0])
        return None
    verdict = validate_file_plan_ownership(candidate, file_plan,
                                           exclude_test_paths=_excl_tests)
    if not verdict.valid:
        logger.warning("[FILEPLAN-SURGERY] 修后闸仍未过 → 回退全量重拆")
        return None
    logger.info("[PLAN] R40-1 命中缺件外科路径：%d 个 file_plan 缺件确定性挂靠，"
                "复用上一版 %d 子任务不重拆", attached, len(candidate.subtasks))
    return candidate


def maybe_symbol_repair(state, project_path: str | None = None):
    """R39-5 分流闸门：符号类校验失败重试 → 确定性外科修复，不全量重拆。

    round39 实证：覆盖满足后 P1 让路（nodes:1802），符号类失败只剩全量重拆一条路，
    LLM 三轮缺口不动白烧。本闸门仅在【符号类/规则5 失败】命中：deepcopy 上一版
    plan → R39-4 脚手架注入 + R39-2 符号挂靠 → C1 同口径复核通过才放行修复版；
    修不好如实返回 None 回退全量重拆（结构类失败的正当出口）——绝不半改原 plan。
    守卫对齐 P1：F-3 执行失败 replan 必须真跑；整模块分解失败绝不外科。
    泄压阀 SWARM_PLAN_SYMBOL_SURGERY（默认开，对照 SWARM_PLAN_COVERAGE_TOPUP 先例）。
    """
    import os

    # 前两条是"职责范围外"的常规静默早退（非重试轮/非符号类失败，逐轮打日志=噪音）；
    # 一旦确认是符号类失败（本通道的职责范围），任何放弃都必须留痕（hunter①：
    # round39 的观测缺口正是"为什么外科没接手"无从考古）。
    if not (state.get("plan_validation_feedback") or "").strip():
        return None
    _blob = " ".join(str(i) for i in (state.get("plan_validation_issues") or [])) \
        + " " + (state.get("plan_validation_feedback") or "")
    if not any(m in _blob for m in _SYMBOL_ISSUE_MARKERS):
        return None  # 覆盖类归 P1 / 结构类归全量重拆，符号外科不越权
    if os.environ.get("SWARM_PLAN_SYMBOL_SURGERY", "1").strip().lower() in (
            "0", "false", "no", "off"):
        logger.info("[SYMBOL-SURGERY] 泄压阀 SWARM_PLAN_SYMBOL_SURGERY=off，"
                    "符号类失败按旧行为走全量重拆")
        return None
    if (state.get("replan_feedback") or "").strip():
        logger.info("[SYMBOL-SURGERY] 存在执行失败 replan_feedback（F-3 必须真跑）→ 让位")
        return None
    if state.get("plan_batch_failed_modules"):
        logger.info("[SYMBOL-SURGERY] 上轮有整模块分解失败 %s → 外科救不了缺模块，让位全量重拆",
                    state.get("plan_batch_failed_modules"))
        return None
    prior = state.get("plan")
    if prior is None or not getattr(prior, "subtasks", None):
        logger.warning("[SYMBOL-SURGERY] 符号类失败但无上一版 plan 可修 → 走全量重拆")
        return None
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    from swarm.brain.plan_validator import (
        validate_contract_ownership,
        validate_plan_structure,
    )
    if not validate_plan_structure(prior).valid:
        logger.warning("[SYMBOL-SURGERY] 上一版 plan 结构校验不过 → 结构失败归全量重拆")
        return None
    candidate = prior.model_copy(deep=True)
    sc = state.get("shared_contract") or (
        getattr(candidate, "shared_contract", None) or {})
    injected = inject_build_scaffold_subtasks(candidate, project_path)
    report = surgical_symbol_attach(candidate, sc, project_path=project_path)
    verdict = validate_contract_ownership(candidate, sc, project_path=project_path)
    if not verdict.valid:
        logger.warning(
            "[SYMBOL-SURGERY] 外科后 C1 仍未过（挂靠 %d/存量 %d/剩 %d）→ 如实回退全量重拆",
            len(report["attached"]), len(report["baseline_owned"]),
            len(report["remainder"]))
        return None
    logger.info(
        "[PLAN] R39-5 命中符号外科路径：挂靠 %d + 存量承接 %d + 脚手架注入 %d，"
        "C1 复核通过，复用上一版 %d 子任务不重拆",
        len(report["attached"]), len(report["baseline_owned"]),
        len(injected), len(candidate.subtasks))
    return candidate
