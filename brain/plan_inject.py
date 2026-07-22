"""R65D-T5 plan 注入端 —— 录制 plan 直入 DISPATCH 的 worker 阶段离线调试通道。

背景（round65d 定案）：执行期编排 bug（H1 覆写冤杀 / HANDLE_FAILURE 掉账 / 毒树合入）
只能靠 live E2E 复现，而每次复现都要重烧一遍云端规划期（analyze→tech_design→contract→
plan→elaborate→validate→confirm，~10min + 真金 token）。执行期本身 317 次模型调用全在
本地 worker——贵的只有大脑。本模块把 scripts/cassette_extract.py 抽出的录制 cassette
喂给【新任务】：跳过整个云端规划子图，经确定性收尾器重跑出【治后形态】，再从 CONFIRM
的出口边直接进入 DISPATCH。

三道闸（全 fail-closed）：
1. schema/空 plan 校验——不是 cassette 的东西绝不当 plan 跑；
2. base_commit 一致性——录制基线≠当前项目基线时绝不开跑（worker diff / merge base /
   L2 reset / learn 复位全链相对 base_commit，错基线=全链错乱着跑完才发现）；
3. 图入口路由校验——aupdate_state(as_node="confirm") 后 next 必须恰为 ("dispatch",)，
   不符 fail-loud（防 LangGraph 语义漂移/after_confirm 改动把注入任务静默送错节点）。

治后形态（绝不原样回放）：录制 plan 抽自治疗前的轮次，直接回放=把已治死因再跑一遍。
prepare 先剥掉录制时已注入的脚手架，再重跑 finish_plan_deterministic（内含 #61
reconcile_template_exam 考卷同源 + #57 消费边推导 + 模板 upsert）与
resolve_plan_conflicts（dedupe→fix_dep→normalize→bump 规范序），使注入 plan 反映
当前代码的全部规划期治本。

配套：SWARM_BRAIN_OFFLINE=1（models/router.py 构造点闸）拦截执行期一切条件性云端
brain 调用（HANDLE_FAILURE 故障分析 / L2 LLM 复核 / replan …），调用方走各自既有
降级路径并留机读账——注入调试轮可零云端全程跑完。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from swarm.types import HumanDecision, TaskPlan

logger = logging.getLogger(__name__)

CASSETTE_SCHEMA = "swarm-plan-cassette/v1"


class _FailOpenAlarm(logging.Handler):
    """收集治疗 pass 被 fail-open 吞掉的异常痕迹（带 exc_info 的 WARNING+）。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.hits: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        if record.exc_info:
            self.hits.append(record.getMessage()[:200])


class PlanInjectError(ValueError):
    """注入 fail-closed 拒绝。code=机读原因（落 task.error 供对账/复盘 grep）。

    猎手 HIGH 整改：message 必须自带 code 前缀——本异常可能从【任何】出口冒泡
    （如闸3 在 _stream_brain_events 深处触发时走 runner 的 generic FAILED 归一，
    error=str(exc)[:300]），不带前缀则 plan_inject_* grep 口径在那条路径失效。
    """

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass
class PlanInjectSeed:
    """runner 分支判据：graph_input 为本类型时走 aupdate_state 注入（与
    Command resume 的 isinstance 判据同法，见 brain/runner.py）。"""

    values: dict[str, Any] = field(default_factory=dict)


def strip_injected_scaffolds(plan) -> int:
    """剥掉【已注入】的 st-scaffold-* 子任务 + 一切指向它们的引用（单一事实源）。

    为何必须剥（round62 死链教训，原实现在 scripts/cassette_replay.py）：cassette 常抽自
    DISPATCHING（plan 节点已跑完 inject+decouple），plan 里已带脚手架。inject 幂等
    （sid 已存在即跳过），不剥则重跑 inject 是 no-op——录制时的【旧模板/旧边】被原样冻结，
    正是 #61 跨遍陈旧模板冻结的死型。剥回功能子任务再重注入，模板/考卷/依赖边才是
    当前代码推导出的治后形态。返回剥掉的数量。
    """
    subs = getattr(plan, "subtasks", None) or []
    scaf_ids = {st.id for st in subs if str(st.id).startswith("st-scaffold-")}
    if not scaf_ids:
        return 0
    plan.subtasks = [st for st in subs if st.id not in scaf_ids]
    for st in plan.subtasks:
        st.depends_on = [d for d in (getattr(st, "depends_on", None) or [])
                         if d not in scaf_ids]
    pg = getattr(plan, "parallel_groups", None)
    if pg:
        plan.parallel_groups = [[x for x in g if x not in scaf_ids] for g in pg]
        plan.parallel_groups = [g for g in plan.parallel_groups if g]
    return len(scaf_ids)


def prepare_injected_state(
    cassette: dict,
    *,
    live_base_commit: str | None,
    project_path: str | None,
    task_description: str = "",
) -> dict[str, Any]:
    """校验录制 cassette 并重推导治后形态，返回可直接并入 BrainState 的通道值。

    返回键全部是 brain/state.py 已声明通道（LangGraph 未声明键静默丢弃——批4a实证，
    改这里必须对照 state.py）：plan / shared_contract / tech_design_file_plan /
    human_decision。任何校验失败抛 PlanInjectError（调用方落 FAILED，绝不带病开跑）。
    """
    if not isinstance(cassette, dict) or cassette.get("schema") != CASSETTE_SCHEMA:
        raise PlanInjectError(
            "plan_inject_schema_invalid",
            f"注入载荷不是 {CASSETTE_SCHEMA} cassette（schema="
            f"{cassette.get('schema') if isinstance(cassette, dict) else type(cassette).__name__}）"
            "——请用 scripts/cassette_extract.py 从 live checkpoint 抽取")

    plan_dump = cassette.get("plan") or {}
    if not (isinstance(plan_dump, dict) and plan_dump.get("subtasks")):
        raise PlanInjectError("plan_inject_empty_plan",
                              "cassette.plan 无 subtasks（空壳）——无从注入")

    # ── 闸2：base_commit 一致性（fail-closed，单侧缺失同罪）──
    rec_base = cassette.get("base_commit") or None
    if rec_base != (live_base_commit or None):
        if rec_base is None and live_base_commit is None:
            pass  # 双侧皆无（greenfield/非 git）——无基线可错
        else:
            raise PlanInjectError(
                "plan_inject_base_commit_mismatch",
                f"录制基线与当前项目基线不一致：cassette={rec_base or '(无)'} vs "
                f"live={live_base_commit or '(无)'}。worker diff/merge/L2 全链相对 "
                "base_commit，错基线绝不开跑——请先把项目重置到录制基线"
                f"（git reset --hard {(rec_base or '')[:12]}，E2E 用 e2e_reset_baseline.sh）")
    if rec_base is None and live_base_commit is None:
        logger.warning("[PLAN-INJECT] 双侧均无 base_commit（greenfield/非 git）——"
                       "放行但 diff 基线不受钉扎保护")

    try:
        plan = TaskPlan.model_validate(plan_dump)
    except Exception as exc:  # noqa: BLE001 — pydantic 细节归一为机读拒绝
        raise PlanInjectError(
            "plan_inject_plan_invalid", f"cassette.plan 不是合法 TaskPlan：{exc}") from exc

    # ── 治后形态重推导（剥旧脚手架 → finisher（#61 考卷同源+#57 消费边）→ 规范冲突序）──
    stripped = strip_injected_scaffolds(plan)
    shared_contract = cassette.get("shared_contract") or {}
    file_plan = cassette.get("file_plan") or []
    desc = task_description or str(cassette.get("task_description") or "")
    from swarm.brain.contract_utils import resolve_plan_conflicts
    from swarm.brain.plan_finisher import finish_plan_deterministic

    # 猎手 HIGH 整改（fail-open 警报升闸）：finisher/inject 包装的每个治疗 pass 都
    # try/except fail-open——live 管线里缺口由 VALIDATE 兜底，注入通道没有那层。
    # 被吞的治疗异常唯一的机器可辨痕迹=这两个模块 logger 的【带 exc_info 的 WARNING】
    # （成环跳过/边剪除等正常治疗告警不带 exc_info，天然区分）。捕到即 fail-closed：
    # "部分治疗的 plan 带着成功账开跑"正是 round65d 冻结陈旧模板的死型。
    # 注：捕获按 logger 全局挂载，并发注入会互相误伤为拒绝（安全侧）——调试通道约定
    # 单发注入，不为此加复杂度。
    _alarm = _FailOpenAlarm()
    _watched = [logging.getLogger("swarm.brain.plan_finisher"),
                logging.getLogger("swarm.brain.contract_utils")]
    for _lg in _watched:
        _lg.addHandler(_alarm)
    try:
        finish_out = finish_plan_deterministic(
            plan, file_plan, project_path=project_path,
            task_description=desc, shared_contract=shared_contract,
            base_ref=live_base_commit)
        try:
            # resolve_plan_conflicts 内部【无】fail-open 包裹（与 finisher 不同）——
            # 意外异常在这里归一为机读拒绝，绝不裸冒泡成无码 FAILED（猎手 MEDIUM）。
            resolve_counts = resolve_plan_conflicts(
                plan, project_path=project_path, base_ref=live_base_commit)
        except Exception as exc:  # noqa: BLE001
            raise PlanInjectError(
                "plan_inject_rederive_failed",
                f"resolve_plan_conflicts 异常（治后形态推导中断）：{exc}") from exc
    finally:
        for _lg in _watched:
            _lg.removeHandler(_alarm)
    if _alarm.hits:
        raise PlanInjectError(
            "plan_inject_rederive_degraded",
            f"治后形态重推导有 {len(_alarm.hits)} 个治疗 pass 被 fail-open 吞掉异常"
            f"（部分治疗的 plan 绝不开跑）：{_alarm.hits[:4]}")
    if stripped and not finish_out.get("scaffolds"):
        # 剥了旧脚手架却一个都没重注入且无异常——可能是治疗代际差异（owner 通道接管），
        # 也可能是推导面漂移。不武断拒绝（无异常=非静默失败），但必须可见。
        logger.warning(
            "[PLAN-INJECT] 剥离 %d 个旧脚手架后重注入为 0 且无异常——请人工核对模块地基"
            "是否已由 owner 通道承接", stripped)
    _ra = finish_out.get("upstream_account_reconciled") or {}
    if _ra:
        # R65REPLAY-T4 复核 F6：机读账在注入路径落日志（回放调试正是本通道的用途——
        # 录制 plan 里的幽灵死等账被清了几条要看得见）。
        logger.info(
            "[PLAN-INJECT] 上游账对账剔除幽灵死等条目 %d 子任务/%d 条: %s",
            len(_ra), sum(len(v) for v in _ra.values()),
            {k: v[:3] for k, v in sorted(_ra.items())[:6]})

    # ── 闸4：确定性结构校验（fail-closed）──
    # 注入跳过了 VALIDATE 节点，而 finisher/resolve 全程 fail-open（live 管线里缺口由
    # VALIDATE 权威打回兜底）——注入通道没有这层兜底，重推导若内部静默失败，带病 plan
    # 会直通 DISPATCH。这里用同一把确定性尺子把关：结构非法绝不开跑。
    from swarm.brain.plan_validator import (
        validate_contract_ownership,
        validate_contract_signature_source,
        validate_module_coherence,
        validate_plan_structure,
    )
    _vres = validate_plan_structure(plan)
    if not _vres.valid:
        raise PlanInjectError(
            "plan_inject_validation_failed",
            f"注入 plan 重推导后结构校验未通过（DAG/写冲突/粒度）：{_vres.issues[:8]}")
    # DR-01-F8(#53) 治本：闸4 此前只跑 validate_plan_structure（结构闸），跳过 live VALIDATE 节点
    # 还会跑的 G1 coherence / 契约 owner 对账等【不依赖运行期 state 的确定性维度】。注入通道刻意
    # 无 VALIDATE 兜底 → finisher 只安置不硬判 G1 → 不coherent 的 plan（逻辑模块散落多物理目录/
    # 多模块塌进同一目录）直穿 DISPATCH 死在 reactor（round44/57/59/62 家族）。至少把 G1 这把
    # ★真治本闸★纳入，契约 owner 对账（有 shared_contract 时）一并补齐——与 live VALIDATE 同参。
    _g1 = validate_module_coherence(
        plan, project_path=project_path, file_plan=file_plan, base_ref=live_base_commit)
    if not _g1.valid:
        raise PlanInjectError(
            "plan_inject_coherence_failed",
            f"注入 plan G1 模块 coherence 校验未通过（逻辑模块↔物理构建单元不相交，"
            f"直穿 DISPATCH 必死 reactor）：{_g1.issues[:8]}")
    if shared_contract:
        _cres = validate_contract_ownership(
            plan, shared_contract, project_path=project_path)
        if not _cres.valid:
            raise PlanInjectError(
                "plan_inject_contract_failed",
                f"注入 plan 契约 owner 对账未通过（契约符号无子任务承接=两张皮，L2 才爆缺失）："
                f"{_cres.issues[:8]}")
        _csres = validate_contract_signature_source(plan, shared_contract)
        if not _csres.valid:
            raise PlanInjectError(
                "plan_inject_contract_signature_diverged",
                f"注入 plan 契约签名↔owner 描述方法名分叉（考卷两真值源打架，消费方 L2 必 cannot "
                f"find symbol）：{_csres.issues[:8]}")

    n_edges = sum(len(st.depends_on or []) for st in plan.subtasks)
    logger.info(
        "[PLAN-INJECT] 注入 plan 治后形态重推导完成：subtasks=%d stripped_scaffolds=%d "
        "scaffolds=%s consumer_edges=%s resolve=%s edges_total=%d 机读: plan_inject_prepared",
        len(plan.subtasks), stripped, finish_out.get("scaffolds"),
        finish_out.get("consumer_edges", "n/a"), resolve_counts, n_edges)

    return {
        "plan": plan,
        "shared_contract": shared_contract,
        "tech_design_file_plan": file_plan,
        "human_decision": HumanDecision.ACCEPT,
    }


async def apply_plan_inject_seed(graph, config: dict, values: dict[str, Any]) -> None:
    """以 confirm 的名义写入注入状态，并【校验】图的下一步恰为 dispatch（闸3）。

    aupdate_state(as_node="confirm") 让 LangGraph 视 confirm 已执行完毕，
    后继由 after_confirm 条件边在【注入后状态】上求值——human_decision=ACCEPT →
    "dispatch"。next 不符即 fail-loud：绝不把注入任务静默送进规划/终止节点。
    """
    await graph.aupdate_state(config, values, as_node="confirm")
    snap = await graph.aget_state(config)
    nxt = tuple(getattr(snap, "next", ()) or ())
    if nxt != ("dispatch",):
        raise PlanInjectError(
            "plan_inject_route_mismatch",
            f"注入后图路由异常：next={nxt}（期望 ('dispatch',)）——"
            "after_confirm/图拓扑可能已变更，注入通道需同步修订")
    logger.info("[PLAN-INJECT] 已就位：thread=%s next=dispatch（跳过云端规划子图）",
                (config.get("configurable") or {}).get("thread_id"))
