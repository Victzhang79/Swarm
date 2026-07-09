"""brain/nodes/adversarial.py — T1（round37b，ECC §B santa-method 移植）对抗验证节点。

swarm 此前只有 HANDLE_FAILURE（管失败），对自报 done（L1 通过）的子任务【无独立复核】——
round36"自造 TwoFactorSetupVO 却自认成功"即漏网。本节点把 ECC santa-method 内核移植为
swarm 原生能力（不依赖 ECC/CLI）：

  · 两个【独立】reviewer（GLM 主 × Kimi 备 = 真模型多样性），同 rubric、无共享上下文；
  · 任一 reviewer 判 FAIL（且带 concrete failure_scenario）→ 该子任务 NAUGHTY 打回
    （santa 判据："单人抓到即真，另一人漏 = 要消灭的盲区"）；
  · flag-back 复用既有 failed_subtask_ids→HANDLE_FAILURE 重试预算：NAUGHTY 子任务
    l1_passed 置 False + 评语进 l1_details → 既有换模型重试/放弃阶梯自然收敛（双界：
    subtask_retry_counts abandon + 本节点 MAX_ROUNDS 早熔断），绝不新造无界循环；
  · Pre-Report Gate（移植 code-reviewer）：FAIL 必带 failure_scenario，否则降级不计（防
    小模型无凭据乱 flag）；
  · 降级分型（对齐本仓 runtime_smoke 三态先例，绝不静默放行——hunter F1 整改）：
      - 不收敛达上限（有负面证据未解决）→ degraded `adversarial_verify_unconverged:*`，
        can_auto_accept_delivery 据此【硬拦】auto_accept（交人工），非静默 ACCEPT；
      - reviewer 基建全挂 / reviewer 漏审覆盖不全（缺复核，非负面证据）→ degraded
        `adversarial_verify_{skipped:reviewer_unavailable,incomplete_coverage}`，经
        blocking_degraded_reasons 自动【挡 L6 假学习】+ deliver payload 人工可见，但【不硬拦
        交付】——与 runtime_smoke skip 同哲学（provider 挂时硬拦会 strand 全部交付=新黑洞）。

调 _get_brain_llm/_get_brain_fallback_llm/_invoke_llm_abortable/_parse_json_from_llm 均经
`from swarm.brain import nodes` 模块限定（与 verify.py 同法），使 patch("swarm.brain.nodes.X") 命中。

栈无关（北极星）：rubric 只谈 subtask/diff/契约/引用，绝不写死语言/框架/示例项目词汇。
"""

from __future__ import annotations

import logging
import os

from swarm.brain.state import BrainState, effective_complexity
from swarm.types import Complexity, SubTask, TaskIntent, WorkerOutput

logger = logging.getLogger(__name__)


# ───────────────────────── 配置（泄压阀 + 有界预算） ─────────────────────────

def _enabled() -> bool:
    """泄压阀（对照 S1-4 SWARM_RUNTIME_SMOKE_ENABLED 先例）：默认开，关闭走 skipped 可观测。"""
    return os.environ.get("SWARM_ADVERSARIAL_VERIFY", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _max_rounds() -> int:
    """不收敛熔断上限（santa MAX_ITER）：默认 2 轮——每轮打回=重跑子任务(贵)，从紧。"""
    try:
        return max(1, int(os.environ.get("SWARM_ADVERSARIAL_MAX_ROUNDS", "2")))
    except ValueError:
        logger.error("[ADVERSARIAL] SWARM_ADVERSARIAL_MAX_ROUNDS 非法(%r)——回退 2",
                     os.environ.get("SWARM_ADVERSARIAL_MAX_ROUNDS"))
        return 2


def _review_timeout() -> float:
    """单 reviewer 墙钟预算：默认 180s（复用 _invoke_llm_abortable 流式看门狗，慢即断）。"""
    try:
        return float(os.environ.get("SWARM_ADVERSARIAL_REVIEW_TIMEOUT", "180") or "180")
    except ValueError:
        return 180.0


def _per_diff_chars() -> int:
    """每子任务 diff 进 prompt 的字符上限（有界成本，语义 smell test 不需逐行）：默认 2000。"""
    try:
        return max(200, int(os.environ.get("SWARM_ADVERSARIAL_DIFF_CHARS", "2000")))
    except ValueError:
        return 2000


def _diff_sig(diff: str) -> str:
    """diff 内容签名（对抗复核 python-reviewer CONFIRMED 修：verified 须绑内容非仅 id）。

    根因：MERGE 的 rebase/硬冲突路径会【重新生成】已复核子任务的 diff（新 worker 产出）→
    若只按 id 记 verified，新 diff 会因 id 仍在 verified 里被跳过复核——正是 T1 要抓的
    "交付码偏离已复核码"。故把 verified token 绑到 diff 内容签名：内容一变即重新入候选复核。"""
    import hashlib
    return hashlib.sha1((diff or "").encode("utf-8", "replace")).hexdigest()[:12]


def _verified_token(sid: str, wo: WorkerOutput) -> str:
    """已复核标记 = 子任务 id + 其 diff 内容签名。内容变（rebase/重生成）→ token 变 → 重审。"""
    return f"{sid}@{_diff_sig(wo.diff)}"


# ───────────────────────── rubric（栈无关，移植 santa + code-reviewer 精髓） ─────────────────────────

_REVIEW_SYSTEM = """你是一名【独立】质量复核员，正在复核某个子任务【自报完成】的产出。你没有看过任何
其他复核意见，你的职责是【找出真问题】，不是批准。

复核 rubric（逐条判定，全部满足才 PASS，任一不满足即该子任务 FAIL）：
1. 实现契合：diff 确实实现了子任务描述/覆盖项声明的目标，不是占位/打桩/空壳冒充完成。
2. 无捏造引用：diff 引用的类型/接口/符号/方法，要么在本 diff 内定义，要么在【声明的共享契约】
   或【上游产物】中确有其物——绝不是凭空编造（自造一个不存在的生产者类型却自认成功=典型病灶）。
3. 契约一致：与声明的共享契约签名/字段一致，无擅自偏离。
4. 完整：子任务覆盖的需求项在 diff 中确有对应改动，无遗漏。

【Pre-Report 铁律】判 FAIL 前必须能给出 concrete failure_scenario（具体输入/状态 → 错误
输出/崩溃路径）。给不出具体失败场景 = 你在模式匹配而非复核 → 该项必须判 PASS。宁可漏报也不
凑数误报；干净产出就该 PASS（零 FAIL 是完全正常且被期望的结果）。

只输出 JSON，不要任何解释文字：
{"reviews": [{"subtask_id": "<id>", "verdict": "PASS"|"FAIL",
  "issue": "<FAIL 时一句话问题，PASS 留空>",
  "failure_scenario": "<FAIL 时具体失败场景，PASS 留空>"}]}
对载荷中【每一个】子任务都给一条 review。"""


def _format_contracts(shared_contract: dict) -> str:
    if not shared_contract:
        return "（无声明的共享契约）"
    import json
    try:
        return json.dumps(shared_contract, ensure_ascii=False)[:3000]
    except (TypeError, ValueError):
        return str(shared_contract)[:3000]


def _build_review_user(candidates: list[tuple[SubTask, WorkerOutput]],
                       shared_contract: dict) -> str:
    cap = _per_diff_chars()
    blocks: list[str] = ["【声明的共享契约（跨子任务稳定接口）】", _format_contracts(shared_contract), ""]
    for st, wo in candidates:
        diff = wo.diff or ""
        truncated = len(diff) > cap
        if truncated:
            logger.info("[ADVERSARIAL] 子任务 %s diff %d 字符 > %d，复核载荷截断（语义 smell test）",
                        st.id, len(diff), cap)
        blocks.append(f"───── 子任务 {st.id} ─────")
        blocks.append(f"描述: {st.description}")
        if st.covers:
            blocks.append(f"覆盖需求项: {', '.join(st.covers)}")
        if st.acceptance_criteria:
            blocks.append(f"验收标准: {'; '.join(st.acceptance_criteria)}")
        if st.contract:
            blocks.append(f"本子任务契约: {_format_contracts(st.contract)}")
        blocks.append("diff:")
        blocks.append(diff[:cap] + ("\n…（已截断）" if truncated else ""))
        blocks.append("")
    return "\n".join(blocks)


def _parse_reviews(content: str) -> dict[str, tuple[str, str]]:
    """解析单个 reviewer 的结构化 verdict → {subtask_id: (VERDICT, failure_scenario)}。

    解析失败/结构不符 → 返回 {}（该 reviewer 视为无有效 verdict，由调用方按"不可用"处置——
    绝不把解析失败当作全 PASS 而放行）。"""
    from swarm.brain import nodes
    try:
        data = nodes._parse_json_from_llm(content)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ADVERSARIAL] reviewer 输出解析失败(%s)——本 reviewer 记无效", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    reviews = data.get("reviews")
    if not isinstance(reviews, list):
        return {}
    out: dict[str, tuple[str, str]] = {}
    for r in reviews:
        if not isinstance(r, dict):
            continue
        sid = r.get("subtask_id")
        if not isinstance(sid, str) or not sid.strip():
            continue
        sid = sid.strip()  # F3：归一 echo（防大小写/空白错配令 vt.get(st.id) 静默丢真 verdict）
        verdict = str(r.get("verdict", "")).strip().upper()
        fs = str(r.get("failure_scenario", "") or "").strip()
        # F5：只收合法三态 verdict——畸形形态（list/dict/未知串→str 化后 !="PASS"/"FAIL"）绝不
        # 当 PASS 静默放行。丢弃后该候选将因"零合法 verdict"落 unreviewed 通道（F2），非误 PASS。
        if verdict not in ("PASS", "FAIL"):
            logger.warning("[ADVERSARIAL] 子任务 %s 非法 verdict=%r——丢弃（记未复核，绝不当 PASS）",
                           sid, verdict)
            continue
        # 归一：存 (VERDICT, failure_scenario)——failure_scenario 供 Pre-Report gate 判是否计入 FAIL
        out[sid] = (verdict, fs)
    return out


async def _run_one_reviewer(llm, messages, tag: str) -> dict[str, tuple[str, str]] | None:
    """跑一个 reviewer，返回其 verdict 表；基建异常（超时/挂）→ None（不可用，不误判）。"""
    from swarm.brain import nodes
    try:
        resp = await nodes._invoke_llm_abortable(llm, messages, _review_timeout())
    except Exception as exc:  # noqa: BLE001 — 任何基建异常都归"该 reviewer 不可用"
        logger.warning("[ADVERSARIAL] reviewer %s 调用失败(%s)——本 reviewer 不可用", tag, exc)
        return None
    verdicts = _parse_reviews(getattr(resp, "content", "") or "")
    if not verdicts:
        logger.warning("[ADVERSARIAL] reviewer %s 无有效 verdict（解析空）——不可用", tag)
        return None
    return verdicts


# ───────────────────────── 节点 ─────────────────────────

def _skip(round_no: int, verified: list[str], message: str,
          degraded: str | None = None) -> dict:
    """跳过/降级放行的统一返回（always-emit 路由三态键 + 可观测）。"""
    out: dict = {
        "adversarial_verify_passed": None,
        "adversarial_verify_round": round_no,
        "adversarial_verified_ids": verified,
        "adversarial_verify_message": message,
    }
    if degraded:
        out["degraded_reasons"] = [degraded]
    return out


async def adversarial_verify(state: BrainState) -> dict:
    """ADVERSARIAL_VERIFY 节点 — 对自报成功的子任务做独立双复核（MONITOR 全完成→此→MERGE）。

    输入: subtask_results, plan, dispatch_remaining, adversarial_verified_ids,
          adversarial_verify_round
    输出: adversarial_verify_passed（三态路由键：False→handle_failure；True/None→merge），
          adversarial_verify_round / adversarial_verified_ids（always-emit），
          NAUGHTY 时追加 failed_subtask_ids + 改写 subtask_results（l1_passed=False+评语）。
    """
    cur_round = int(state.get("adversarial_verify_round", 0) or 0)
    verified: list[str] = list(state.get("adversarial_verified_ids") or [])

    # ── 门槛 1：泄压阀关 ──
    if not _enabled():
        return _skip(cur_round, verified, "对抗验证泄压阀关闭（SWARM_ADVERSARIAL_VERIFY=0）")

    # ── 门槛 2：低复杂度跳过（跨模块幻觉在这层不发生，省成本；非降级=by-design）──
    complexity = effective_complexity(state)
    if complexity not in (Complexity.COMPLEX, Complexity.ULTRA):
        return _skip(cur_round, verified, f"复杂度 {complexity} 跳过对抗验证（仅 COMPLEX/ULTRA 启用）")

    # ── 门槛 3：PARTIAL 熔断路径（after_monitor #R13-4：剩余不可派发→merge）不主动复核 ──
    if state.get("dispatch_remaining"):
        return _skip(cur_round, verified,
                     "PARTIAL 熔断路径（尚有未派发子任务）跳过对抗验证，不扰动部分交付")

    # ── 门槛 4：不收敛熔断（santa MAX_ITER）——短路 escalate，绝不无界烧 token ──
    max_rounds = _max_rounds()
    if cur_round >= max_rounds:
        logger.warning("[ADVERSARIAL] 已达 %d 轮仍未收敛 → 升人工（degraded 放行，绝不静默/绝不再打回）",
                       max_rounds)
        return _skip(cur_round, verified,
                     f"对抗验证 {max_rounds} 轮未收敛，升人工复核（degraded 放行）",
                     degraded=f"adversarial_verify_unconverged:round_cap_{max_rounds}")

    plan = state.get("plan")
    subtask_results: dict = state.get("subtask_results", {})
    if plan is None or not subtask_results:
        return _skip(cur_round, verified, "无 plan/无子任务产出，跳过对抗验证")

    # ── 候选集：L1 通过 + 有 diff + 非 AUDIT + 未放弃 + 【当前内容】未复核过 ──
    # verified 现为【内容绑定 token】(sid@diff_sig)：id 相同但 diff 被 rebase/重生成 → token 变
    # → 重新入候选复核（python-reviewer CONFIRMED 修，见 _diff_sig）。放弃/give_up 仍按纯 id 排除。
    by_id = {s.id: s for s in plan.subtasks}
    verified_tokens = set(verified)
    excluded_sids = (set(state.get("abandoned_subtask_ids") or [])
                     | set(state.get("give_up_isolated_ids") or []))
    candidates: list[tuple[SubTask, WorkerOutput]] = []
    for sid, wo in subtask_results.items():
        if sid in excluded_sids:
            continue
        st = by_id.get(sid)
        if st is None or st.intent == TaskIntent.AUDIT:
            continue
        if not isinstance(wo, WorkerOutput):
            continue  # dict 兜底态不复核（无 l1_passed 语义保证）
        if not wo.l1_passed or not (wo.diff or "").strip():
            continue
        if _verified_token(sid, wo) in verified_tokens:
            continue  # 当前 diff 内容已复核过——不重审（省成本），内容一变即失配重审
        candidates.append((st, wo))

    if not candidates:
        return {
            "adversarial_verify_passed": True,
            "adversarial_verify_round": cur_round,
            "adversarial_verified_ids": verified,
            "adversarial_verify_message": "无需对抗验证的候选子任务（全部为审计/空 diff/已复核）",
        }

    # ── 独立双复核：GLM 主 × Kimi 备（真模型多样性）──
    from swarm.brain import nodes
    shared_contract = state.get("shared_contract") or (
        plan.shared_contract if plan is not None else {})
    user_msg = _build_review_user(candidates, shared_contract)
    messages = [{"role": "system", "content": _REVIEW_SYSTEM},
                {"role": "user", "content": user_msg}]

    primary = nodes._get_brain_llm()
    fallback = nodes._get_brain_fallback_llm()
    reviewer_llms: list[tuple[str, object]] = [("A", primary)]
    single_reviewer_degraded: str | None = None
    if fallback is not None:
        reviewer_llms.append(("B", fallback))
    else:
        # 无备用（备==主）→ 退化单 reviewer（不重跑同模型省成本），独立性降低须可观测
        single_reviewer_degraded = "adversarial_verify_single_reviewer:no_model_diversity"
        logger.warning("[ADVERSARIAL] 无备用模型 → 退化【单】reviewer（模型多样性缺失，独立性降低）")

    verdict_tables: list[dict[str, tuple[str, str]]] = []
    for tag, llm in reviewer_llms:
        vt = await _run_one_reviewer(llm, messages, tag)
        if vt is not None:
            verdict_tables.append(vt)

    # ── reviewer 基建全挂 → 降级放行（绝不因坏 reviewer 黑洞交付，也不误 flag）──
    if not verdict_tables:
        return _skip(cur_round, verified,
                     "对抗验证 reviewer 全不可用（超时/挂），降级放行待人工",
                     degraded="adversarial_verify_skipped:reviewer_unavailable")

    # ── F3 可观测：reviewer 回了但不属候选集的 id（echo 幻觉/错配）──
    _cand_ids = {st.id for st, _ in candidates}
    for vt in verdict_tables:
        for rid in vt:
            if rid not in _cand_ids:
                logger.warning("[ADVERSARIAL] reviewer 返回未知子任务 id=%r（不在候选集）——忽略", rid)

    # 拿到【至少一个合法 verdict】(PASS/FAIL)的候选 id——用于 F2 未复核判定
    reviewed_ids: set[str] = set()
    for vt in verdict_tables:
        reviewed_ids |= set(vt.keys())

    # ── verdict 门：任一 reviewer 带 failure_scenario 判 FAIL → NAUGHTY；零合法 verdict → unreviewed ──
    naughty: dict[str, list[str]] = {}
    unreviewed: list[str] = []
    for st, _wo in candidates:
        critiques: list[str] = []
        for vt in verdict_tables:
            v = vt.get(st.id)
            if v is None:
                continue
            verdict, fs = v
            if verdict == "FAIL":
                if fs:  # Pre-Report gate：FAIL 必带 concrete failure_scenario 才计入
                    critiques.append(fs)
                else:   # F4：FAIL 无凭据 → 降级不计，但记 info 可审计（防系统性漏检无迹可查）
                    logger.info("[ADVERSARIAL] 子任务 %s 一 reviewer 判 FAIL 但无 failure_scenario"
                                "——Pre-Report gate 降级不计", st.id)
        if critiques:
            naughty[st.id] = critiques
        elif st.id not in reviewed_ids:
            # F2：无任何 reviewer 给出合法 verdict → 该候选【未复核】，绝不当 PASS/verified 静默放行
            unreviewed.append(st.id)

    # 只有【真拿到 PASS、既非 NAUGHTY 亦非 unreviewed】的候选才算通过、才绑内容 token 记 verified
    passed = [(st, wo) for st, wo in candidates
              if st.id not in naughty and st.id not in unreviewed]
    new_verified = list(dict.fromkeys(
        list(verified) + [_verified_token(st.id, wo) for st, wo in passed]))

    # 降级聚合：单 reviewer（独立性降低）+ 覆盖不全（有候选零 verdict，缺复核）
    degraded: list[str] = []
    if single_reviewer_degraded:
        degraded.append(single_reviewer_degraded)
    if unreviewed:
        logger.warning("[ADVERSARIAL] %d 个候选无任何合法 verdict（reviewer 漏审）→ 不计 PASS、"
                       "不入 verified，记 degraded（挡 L6 假学习）: %s", len(unreviewed), unreviewed)
        degraded.append("adversarial_verify_incomplete_coverage:" + ",".join(unreviewed[:10]))

    # ── 全 NICE：放行（都过才发）——unreviewed 不算 NAUGHTY（无负面证据），但已记 degraded 挡 L6 ──
    if not naughty:
        out: dict = {
            "adversarial_verify_passed": True,
            "adversarial_verify_round": cur_round,
            "adversarial_verified_ids": new_verified,
            "adversarial_verify_message":
                f"对抗验证通过：{len(passed)} 个子任务经 {len(verdict_tables)} 个独立 reviewer 复核均 PASS"
                + (f"；{len(unreviewed)} 个 reviewer 漏审待下轮复核" if unreviewed else ""),
        }
        if degraded:
            out["degraded_reasons"] = degraded
        return out

    # ── 有 NAUGHTY：flag-back（复用 HANDLE_FAILURE 重试预算），l1_passed 置 False + 评语入 l1_details ──
    logger.warning("[ADVERSARIAL] %d 个子任务未过对抗复核 → 打回重做: %s",
                   len(naughty), list(naughty.keys()))
    new_results = dict(subtask_results)
    for sid, critiques in naughty.items():
        wo = new_results[sid]
        critique_text = "对抗复核未通过：" + "；".join(critiques)
        new_results[sid] = wo.model_copy(update={
            "l1_passed": False,  # 依赖者据 completed_l1_ids 须等待；handle_failure 据此重做
            "l1_details": {**(wo.l1_details or {}),
                           "adversarial_critique": critique_text,
                           # 复用既有失败通道（dispatch 置 l1_details["error"]），令 handle_failure
                           # 的 retry_guidance 构造能看见评语，worker 重试不重蹈同类幻觉。
                           "error": critique_text},
        })
    failed = list(state.get("failed_subtask_ids") or [])
    for sid in naughty:
        if sid not in failed:
            failed.append(sid)

    out = {
        "adversarial_verify_passed": False,
        "adversarial_verify_round": cur_round + 1,
        "adversarial_verified_ids": new_verified,
        "subtask_results": new_results,
        "failed_subtask_ids": failed,
        "adversarial_verify_message":
            f"对抗验证打回 {len(naughty)} 个子任务（第 {cur_round + 1} 轮），复用重试预算重做",
        "adversarial_verify_details": {
            sid: {"critiques": cs} for sid, cs in naughty.items()},
    }
    if degraded:
        out["degraded_reasons"] = degraded
    return out
