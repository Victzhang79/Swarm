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

# 复用 __init__ 的 logger 名 "swarm.brain.nodes"（非 __name__），使 [HANDLE_FAILURE] 策略日志的
# logger 名与外置前逐字节一致（这些是运维关键日志，避免 name 漂移影响既有日志过滤/聚合配置）。
logger = logging.getLogger("swarm.brain.nodes")

from swarm.brain.llm_schemas import FailureStrategyResponse
from swarm.brain.prompts import HANDLE_FAILURE_SYSTEM, HANDLE_FAILURE_USER
from swarm.brain.state import BrainState, effective_complexity
from swarm.config.settings import get_config
from swarm.types import Complexity, WorkerOutput

from swarm.brain.nodes.maven_repair import inject_missing_deps_for_stack
from swarm.brain.nodes.recovery import (
    _INTERNAL_BLOCKED_KINDS,
    _blocked_pkg_unrecoverable,
    _det_of,
    _is_missing_dependency_failure,
    _module_order_violation_modules,
    _producers_of,
    _root_manifest_registrants,
    _scaffold_subtask_of_module,
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
    # 缺失类名：javac "cannot find symbol ... symbol: class X"（首字母大写=类型名，滤掉方法/变量）
    classes = {c for c in re.findall(r"symbol:\s*class\s+([A-Za-z_]\w*)", build_output or "")
               if c[:1].isupper()}
    if not classes:
        return []
    ext = ".kt" if "kotlin" in srcroot else ".java"
    out: list[str] = []
    for pkg in blocked_pkgs:
        pkgpath = str(pkg).strip().replace(".", "/")
        for cls in sorted(classes):
            out.append(f"{srcroot}{pkgpath}/{cls}{ext}")
    return sorted(set(out))


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
        failed = list(state.get("failed_subtask_ids", [])) or list(
            (state.get("subtask_results") or {}).keys()
        )
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
        return {
            "subtask_results": subtask_results,
            "dispatch_remaining": _dispatch_remaining,
            "failure_strategy": "retry",
            "failure_escalated": False,  # 批4c：非 escalate 决策清历史粘滞标记（取证 CONFIRMED，见 DEVLOG）
            "contract_retry_counts": {**_retry_counts, **_next_counts},  # D13 独立表
            "failed_subtask_ids": [],
            "verification_failure": None,
        }

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
        logger.info(
            "[HANDLE_FAILURE] SIMPLE 快速路径 — 重试失败子任务(第 %d 次%s)",
            deepest, "，换备选模型" if forced_alternate else "",
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
        _unsat = (set(state.get("give_up_isolated_ids") or [])
                  | set(state.get("abandoned_subtask_ids") or []))
        _proj_path = _get_project_path(state.get("project_id") or "")
        _by_id = {s.id: s for s in plan_obj.subtasks}
        # #10 治本所需：全局 settled 生产者判据的两个集合。
        _completed_ok = {sid for sid, out in subtask_results.items()
                         if sid not in failed_ids and l1_passed(out)}
        _pending_now = set(state.get("dispatch_remaining") or []) | set(failed_ids)
        _unrecoverable: set[str] = set()
        # round36 P0 治本：区分两类"阻断在无产物的内部包"——(1)【真死上游】有生产者但已放弃/
        # 依赖已放弃上游(dep_hit/prod_hit) → 连坐放弃正确；(2)【worker 自造引用】完全无生产者
        # (_prods 空、非 dep_hit)=消费者自己在编码时引用了一个全场没人生产的类型(round36 实证：
        # st-12-1 引用 TwoFactorSetupVO，全计划无 owner→连坐炸 62/64)。第(2)类不该直接连坐放弃——
        # 那类型本就该由消费者自己在本模块建出，先给一次 scope 自愈(allow_any)+提示重试机会。
        _selfheal: set[str] = set()
        for fid in failed_ids:
            _det = _det_of(subtask_results.get(fid))
            if _det.get("pipeline_blocked") not in _INTERNAL_BLOCKED_KINDS:
                continue
            _st = _by_id.get(fid)
            _bpkgs = _det.get("blocked_on_packages") or []
            _bmods = _det.get("blocked_on_modules") or []
            _prods = _producers_of(plan_obj, _bpkgs, _bmods)
            # (B round13) 上游已永久放弃 → 依赖它的下游不可恢复(传递闭包)。
            _dep_hit = bool(set(getattr(_st, "depends_on", []) or []) & _unsat) if _st else False
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
        # round36 P0 自愈：无生产者内部包(worker 自造引用) → 授消费者 allow_any + 提示本模块补建被引
        # 类型 + 重派(按子任务 targeted_recovery_counts 熔断，与 A2 缺依赖恢复同预算)。耗尽预算才回落
        # 连坐放弃(原行为)。这把"一个自造引用炸 62 子任务"降为"消费者补建它自己引用的类型"。
        if _c9_edges:
            logger.info(
                "[HANDLE_FAILURE] C9 合法跨模块等待 → 补动态依赖边（消费者扣在依赖闸，"
                "生产者 L1 过再派，替代 transient 白跑）: %s", _c9_edges)
        if _selfheal:
            _sh_max = get_config().model.max_retries
            _sh_trc = dict(state.get("targeted_recovery_counts") or {})
            _healed: list[str] = []
            for fid in sorted(_selfheal):
                if _sh_trc.get(fid, 0) >= _sh_max:
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
                _ab = _transitive_abandon(
                    plan_obj.subtasks,
                    set(state.get("abandoned_subtask_ids") or []) | _unrecoverable,
                ) if _unrecoverable else set()
                for _a in _ab:
                    subtask_results.pop(_a, None)
                _sh_rc = dict(state.get("subtask_retry_counts", {}))
                for fid in _healed:
                    _sh_rc[fid] = 0  # 因 scope 不可满足而徒劳的重试不计入常规配额
                _sh_remaining = [t for t in (state.get("dispatch_remaining") or []) if t not in _ab]
                for fid in _healed:
                    if fid in _ab:  # 已愈但落在放弃闭包(依赖真死上游)→随闭包放弃，不重派
                        continue
                    subtask_results.pop(fid, None)
                    if fid not in _sh_remaining:
                        _sh_remaining.append(fid)
                logger.warning(
                    "[HANDLE_FAILURE] round36 P0 自愈：无生产者内部类型(worker 自造引用) → 把待建类型文件"
                    "纳入 create_files 让消费者本模块补建 + 重派(按子任务熔断 %s/%d)；同批真死上游 %s "
                    "照常连坐放弃 %d",
                    {k: _sh_trc[k] for k in _healed}, _sh_max, sorted(_unrecoverable), len(_ab))
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
            )
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
    if _diagnosis and failed_ids:
        _by_id = {st.id: st for st in (getattr(plan_obj, "subtasks", None) or [])}
        for _fid in failed_ids:
            _st = _by_id.get(_fid)
            if _st is not None:
                # SubTask 是可变 pydantic BaseModel、retry_guidance 是声明的 str 字段 →
                # 直接赋值不会抛（原 except:pass 是无谓的静默吞错，brain#3 一并去掉）。
                # 就地改的持久化由外层 handle_failure 回传 plan 保证。
                _st.retry_guidance = _diagnosis[:800]

    # ── P0-B/P1-D：缺符号/缺依赖编译失败 → 定向恢复（先于一切 strategy 分支拦截）──
    # 这类失败是【scope 不可满足】（pom 不在子任务写权内，原地重试 100 次也修不了）。无论 LLM
    # 选 retry 还是 replan，都先走定向恢复：补模块 pom 写权 + 重置徒劳的重试计数 + 只重派失败
    # 子任务（保留成功兄弟、不进 PLAN、不清完成态全表）。targeted_recovery_counts【按子任务】熔断防死循环（遗漏项#2）。
    if _is_missing_dependency_failure(subtask_results, failed_ids) and failed_ids:
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
            # 仅在配额内才 mutate plan（补 pom 写权 + 串 owner 依赖），杜绝兜底路径留下孤儿 scope 改动。
            granted = _grant_module_pom_writable(plan_obj, _eligible)
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
                _serialize_pom_writers(plan_obj, granted)
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

    # 仅当本批失败【全部】为 transient 时才走退避快路（混入 capability 则不抢占阶梯）。
    if transient_ids and len(transient_ids) == len(failed_ids):
        transient_counts = dict(state.get("subtask_transient_counts", {}))
        next_tcounts = {fid: transient_counts.get(fid, 0) + 1 for fid in transient_ids}
        deepest_t = max(next_tcounts.values(), default=0)
        if deepest_t <= MAX_TRANSIENT_RETRY:
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

    # FINDING-12：拒答/步数耗尽(refusal_hard_fail)的子任务，重试强制走【最强模型】(40B 256k)，
    # 而非更弱 fallback——步数耗尽是小模型 agent 循环不收敛，换更弱只会更糟。
    force_strong = dict(state.get("subtask_force_strong", {}))
    for _fid in failed_ids:
        _res = subtask_results.get(_fid)
        _src = (getattr(_res, "l1_details", {}) or {}).get("l1_decision_source") if _res else None
        if _src == "refusal_hard_fail":
            force_strong[_fid] = True

    if deepest > max_retries + 1:
        # 重试耗尽。【部分交付】：已有完成子任务 + 开启 partial → 放弃 failed(+传递依赖者)，
        # 继续交付其余，终态 PARTIAL(非 DONE，诚实未完成)。否则(0 完成 / 关闭 partial) →
        # 维持 escalate(整任务失败)，避免无产出却假成功。
        _abandoned_so_far = set(state.get("abandoned_subtask_ids") or [])
        _done = [tid for tid in subtask_results
                 if tid not in failed_ids and tid not in _abandoned_so_far]
        _allow_partial = getattr(get_config().worker, "allow_partial_delivery", True)
        if _allow_partial and _done and plan_obj is not None:
            # 传递放弃：依赖被放弃者的子任务也放弃(缺依赖跑不了)，避免它们永留 remaining 死循环
            abandoned = _transitive_abandon(plan_obj.subtasks, _abandoned_so_far | set(failed_ids))
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
            new_files = _widen_scope_for_compile_repair(plan_obj, fid, saved_l1_details.get(fid, {}))
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
        # 批4c：本返回 strategy 恒为 retry/retry_alternate（escalate 在上方早返回），
        # 非 escalate 决策清历史粘滞标记（取证 CONFIRMED，见 DEVLOG）
        "failure_escalated": False,
    }
    if _scope_widened or _c9_edges:
        out["plan"] = plan_obj  # 回写加宽后的 scope / C9 动态依赖边，dispatch 重试用
    if effective_strategy == "retry_alternate":
        out["subtask_use_alternate"] = _alt_map_update(state, failed_ids, True)
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
    return out
