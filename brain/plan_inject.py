"""R65D-T5 plan 注入端 —— 录制 plan 直入权威 VALIDATE 的离线调试通道。

背景（round65d 定案）：执行期编排 bug（H1 覆写冤杀 / HANDLE_FAILURE 掉账 / 毒树合入）
只能靠 live E2E 复现，而每次复现都要重烧一遍云端规划期（analyze→tech_design→contract→
plan→elaborate→validate→confirm，~10min + 真金 token）。执行期本身 317 次模型调用全在
本地 worker——贵的只有大脑。本模块把 scripts/cassette_extract.py 抽出的录制 cassette
喂给【新任务】：跳过整个云端规划生成子图，经确定性收尾器重跑出【治后形态】，再从
ELABORATE 的出口边进入 VALIDATE_PLAN，通过全套权威硬闸后再进入 DISPATCH。

入口闸全部 fail-closed：
1. schema/空 plan 校验——不是 cassette 的东西绝不当 plan 跑；
2. base_commit 一致性——录制基线≠当前项目基线时绝不开跑（worker diff / merge base /
   L2 reset / learn 复位全链相对 base_commit，错基线=全链错乱着跑完才发现）；
3. 需求分母校验——条目必须逐条回指录制源文本，且 checkpoint 明确标为完整；
4. 图入口路由校验——aupdate_state(as_node="elaborate") 后 next 必须恰为
   ("validate_plan",)，
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

_INJECT_RETRY_STATE_KEYS = frozenset({
    "plan", "subtask_results", "coverage_watermark", "dispatch_remaining",
    "failed_subtask_ids", "abandoned_subtask_ids", "give_up_isolated_ids",
    "deprioritized_subtask_ids", "merge_rebase_dropped", "partial_salvage_ids",
    "l2_passed", "runtime_smoke_passed", "l3_passed", "acceptance_passed",
    "delivery_reviewed", "delivery_finalization_failed", "learned",
})


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


def build_injected_initial_state(
    initial_state: dict[str, Any],
    prepared: dict[str, Any],
) -> dict[str, Any]:
    """用 cassette 新周期替换 retry 播种态，绝不继承旧 worker 产物/验证水位。"""
    clean = {
        key: value
        for key, value in initial_state.items()
        if key not in _INJECT_RETRY_STATE_KEYS
    }
    return {**clean, **prepared}


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
    task_description: str | None = None,
) -> dict[str, Any]:
    """校验录制 cassette 并重推导治后形态，返回可直接并入 BrainState 的通道值。

    返回键全部是 brain/state.py 已声明通道（LangGraph 未声明键静默丢弃——批4a实证，
    改这里必须对照 state.py）：plan / shared_contract / tech_design_file_plan /
    requirement_* / baseline_* / human_decision。任何校验失败抛 PlanInjectError
    （调用方落 FAILED，绝不带病开跑）。
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

    recorded_description = str(cassette.get("task_description") or "")
    recorded_description_len = cassette.get("task_description_len")
    if (
        isinstance(recorded_description_len, bool)
        or not isinstance(recorded_description_len, int)
        or recorded_description_len != len(recorded_description)
    ):
        raise PlanInjectError(
            "plan_inject_description_provenance_invalid",
            "cassette.task_description_len 缺失或与录制原文长度不一致；"
            "快照来源已不可证明，请重新抽取",
        )
    if task_description is not None and task_description != recorded_description:
        raise PlanInjectError(
            "plan_inject_description_mismatch",
            "新任务 description 与 cassette 录制原文不一致；录制计划不得服务另一项需求",
        )

    try:
        plan = TaskPlan.model_validate(plan_dump)
    except Exception as exc:  # noqa: BLE001 — pydantic 细节归一为机读拒绝
        raise PlanInjectError(
            "plan_inject_plan_invalid", f"cassette.plan 不是合法 TaskPlan：{exc}") from exc

    # ── 治后形态重推导（剥旧脚手架 → finisher（#61 考卷同源+#57 消费边）→ 规范冲突序）──
    stripped = strip_injected_scaffolds(plan)
    shared_contract = cassette.get("shared_contract") or {}
    file_plan = cassette.get("file_plan") or []
    # 已在入口做逐字来源绑定；后续所有推导只消费 cassette 的权威原文。
    desc = recorded_description
    recorded_requirements = cassette.get("requirement_items")
    requirement_source = recorded_description
    clarify_source = str(cassette.get("clarify_summary") or "")
    if clarify_source:
        requirement_source += "\n" + clarify_source
    from swarm.brain.requirements_extract import (
        _minimum_expected_items,
        deterministic_evidence_coverage,
        requirement_denominator_provenance_complete,
        validate_requirement_items,
    )

    if not requirement_denominator_provenance_complete(
        complete=cassette.get("requirement_denominator_complete"),
        reason=cassette.get("requirement_denominator_reason"),
        source_text=requirement_source,
        items=recorded_requirements,
    ):
        raise PlanInjectError(
            "plan_inject_requirement_denominator_unknown",
            "cassette 未携带完整、非空的结构化需求分母；注入路径会跳过需求抽取节点，"
            "不能把旧快照或残缺分母冒充完整交付。请从已完成需求抽取的新 checkpoint 重新抽取",
        )
    validated_requirements, rejected_requirements = validate_requirement_items(
        recorded_requirements, requirement_source
    )
    if rejected_requirements or len(validated_requirements) != len(recorded_requirements):
        raise PlanInjectError(
            "plan_inject_requirement_items_invalid",
            "cassette 的结构化需求无法逐条回指录制源文本："
            f"accepted={len(validated_requirements)} rejected={rejected_requirements[:6]}",
        )
    expected_items = _minimum_expected_items(requirement_source)
    covered_evidence, expected_evidence = deterministic_evidence_coverage(
        validated_requirements, requirement_source
    )
    if (
        len(validated_requirements) < expected_items
        or (expected_evidence and covered_evidence < expected_evidence)
    ):
        raise PlanInjectError(
            "plan_inject_requirement_denominator_incomplete",
            "cassette 虽声明分母完整，但按当前确定性规则重算仍低产："
            f"accepted={len(validated_requirements)} expected>={expected_items}，"
            f"evidence={covered_evidence}/{expected_evidence}。"
            "请重新运行需求抽取并生成 cassette",
        )
    from swarm.brain.plan_validator import normalize_baseline_covered

    baseline_covered = normalize_baseline_covered(cassette.get("baseline_covered"))
    baseline_ineligible = sorted({
        str(item).strip()
        for item in (cassette.get("baseline_ineligible_reqs") or [])
        if str(item).strip()
    })
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
    # H-6（批次8 hunter R1 CONFIRMED）：注入=新规划周期 → 裁决账从空重推导，finisher/resolve
    # 的新裁决（剥离/归位）必须入账并随注入 state 落键——否则注入 plan 的裁决不进账，下游
    # reconcile/attach 前置核对这批违例失明（账与 file_plan 漂移，回退 PLAN 时复活面重开）。
    _adjs: list = []
    try:
        finish_out = finish_plan_deterministic(
            plan, file_plan, project_path=project_path,
            task_description=desc, shared_contract=shared_contract,
            base_ref=live_base_commit, adjudications=_adjs)
        try:
            # resolve_plan_conflicts 内部【无】fail-open 包裹（与 finisher 不同）——
            # 意外异常在这里归一为机读拒绝，绝不裸冒泡成无码 FAILED（猎手 MEDIUM）。
            resolve_counts = resolve_plan_conflicts(
                plan, project_path=project_path, base_ref=live_base_commit,
                file_plan=file_plan,   # create-vs-base modify-shadow 归位需 file_plan 的 modify 信号
                adjudications=_adjs)
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

    # ── 闸4：注入前确定性预检（fail-closed）──
    # 这里尽早拒绝明显坏 cassette；它不是 plan_valid 的权威来源。图注入后仍会完整经过
    # VALIDATE_PLAN 的 coverage/R40-1/G1/契约等全闸，避免形成缩水的第二套真值源。
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
    # R67C-T6（hunter 二/三轮 CONFIRMED）：G1 warn（③e 汇聚点 / ③g R67C-T6 悬空占位孤岛等）在
    # live VALIDATE 节点会逐条 logger.warning + 折进 state["plan_validation_warnings"]（机读面，
    # 进 deliver payload、API/盯跑/复盘据它读）。注入通道刻意无 VALIDATE 兜底 → 此前把 .warnings
    # 整个丢弃。★必须【日志+机读 state 键】两半都镜像★：只 log 不 seed=G3-2 log-and-forget 病复现
    # （API/机读面仍看不见）。收集结构闸(_vres)+G1(_g1) 全部 warn，随注入 state 以 confirm 名义落键。
    _inject_warnings = [str(_w) for _w in (getattr(_vres, "warnings", None) or [])]
    _inject_warnings += [str(_w) for _w in (getattr(_g1, "warnings", None) or [])]
    for _w in _inject_warnings:
        logger.warning("[PLAN-INJECT] plan 校验告警（非阻断，回放同 surface）：%s", _w)
    if shared_contract:
        # ★31 号文 A1-M1★ `layout_punted` 必传——漏传即 R67M2-T2 C1（复核 HIGH-2）那道硬打回
        # 在本通道**结构性失效**：`finish_plan_deterministic` 照常算出该账
        # （`plan_finisher.py:1676-1680`），但缺省 None ⇒ `_punted_set` 空 ⇒ 那条"不占无主
        # 宽容直接打回"的 `result.add` 永不触发 ⇒ punt 符号退回只参与 `ratio > 0.4` 比率判定。
        # 后果：胖契约 plan 里少数符号落点是幽灵布局（无 src 段）→ 布局闸 punt、不建安置 →
        # 仍无主但占比 < 0.4 → C1 valid=True → 闸4 放行 → 直穿 DISPATCH，爆点后移到 L2/交付
        # ——正是 HIGH-2 立项要防的形态。本通道**刻意无 VALIDATE 节点**（见上方 :208-210 自述），
        # 闸4 是唯一确定性把关，漏传等于该布局零防护。
        # live 通道对照（写法权威）：`brain/nodes/__init__.py:3701`。
        _cres = validate_contract_ownership(
            plan, shared_contract, project_path=project_path,
            layout_punted=finish_out.get("contract_symbols_layout_punted") or [])
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
        "requirement_items": validated_requirements,
        "requirement_denominator_complete": True,
        "requirement_denominator_reason": "",
        "baseline_covered": baseline_covered,
        "baseline_ineligible_reqs": baseline_ineligible,
        "human_decision": HumanDecision.ACCEPT,
        # 预检 warning 先 seed；后续权威 VALIDATE 会按本轮结果重新 always-emit 同一键。
        "plan_validation_warnings": _inject_warnings,
        # H-6：注入周期重推导出的裁决账落键（attach 前置核/回退 PLAN 的 reconcile 消费）。
        "file_plan_adjudications": _adjs,
    }


async def apply_plan_inject_seed(graph, config: dict, values: dict[str, Any]) -> None:
    """以 elaborate 的名义写入注入状态，并校验下一步恰为 validate_plan（闸3）。

    注入只跳过生成阶段，不自行签发 plan_valid。权威 validate_plan 仍完整执行 coverage、
    文件归属、模块 coherence、契约等全部硬闸；否则 prepare 的局部校验会成为第二套缩水
    真值源。next 不符即 fail-loud，防图拓扑漂移静默绕过权威验证。
    """
    # prepare 已重跑与 ELABORATE 同源的确定性 finisher/resolve；以 elaborate 名义落点
    # 可跳过该生成/收尾阶段，同时保留其唯一后继 validate_plan。
    await graph.aupdate_state(config, values, as_node="elaborate")
    snap = await graph.aget_state(config)
    nxt = tuple(getattr(snap, "next", ()) or ())
    if nxt != ("validate_plan",):
        raise PlanInjectError(
            "plan_inject_route_mismatch",
            f"注入后图路由异常：next={nxt}（期望 ('validate_plan',)）——"
            "plan/validate 图拓扑可能已变更，注入通道需同步修订")
    logger.info("[PLAN-INJECT] 已就位：thread=%s next=validate_plan（跳过云端规划生成子图）",
                (config.get("configurable") or {}).get("thread_id"))
