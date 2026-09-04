"""需求抽取节点编排；确定性分母与引文校验见 requirements_extract。"""

from __future__ import annotations

import json

from swarm.brain.state import BrainState

from .requirements_extract import (
    MAX_EXTRACT_RETRIES,
    _minimum_expected_items,
    deterministic_evidence_coverage,
    logger,
    requirement_denominator_snapshot_complete,
    source_is_truncated,
    validate_requirement_items,
)

REQUIREMENTS_EXTRACT_SYSTEM = """你是需求分析器。把给定的需求文本拆解为独立、可验收的需求条目清单。

规则（务必逐条遵守）：
1. 只抽取需求文本中【明确写出】的需求；绝不臆造，绝不补全你认为"应该有"的需求。
2. 每条条目必须带 source_quote：从需求原文【逐字复制】的一小段（10~80字）作为出处依据。
   给不出原文出处的条目【不要输出】——系统会用原文逐字核对，对不上的条目会被剔除。
3. text 是该条需求的一句话概括（≤200字），忠于原文，不加入原文没有的细节。
4. kind 只能取：functional(功能) / data(数据) / api(接口) / page(页面) / other(其他)。
5. source 标注出处来源：description(任务描述/附件正文) 或 clarify(澄清答复)。
6. 一条只表达一个可独立验收的需求；相同需求不要重复输出。
7. 表格/列举型内容若每行各自描述一个独立实体、规格、对接方式或策略，则每行各成一条，
   各自带该行的 source_quote；纯属性说明表归并为一条，不逐字段拆条。
8. 仅输出 JSON：{"items": [{"text": "...", "kind": "...", "source_quote": "...", "source": "description|clarify"}]}
"""

REQUIREMENTS_EXTRACT_USER = """【需求文本（任务描述+附件正文）】
{description}

【用户澄清答复摘要】
{clarify}

【技术方案给出的验收提示（仅辅助参考——source_quote 仍必须逐字来自上面两段需求文本，不得引用本段）】
{hints}
{retry_feedback}
请输出需求条目 JSON。"""


def _rejected_summary(rejected: list[dict]) -> str:
    counts: dict[str, int] = {}
    for row in rejected:
        reason = row.get("reason", "?")
        counts[reason] = counts.get(reason, 0) + 1
    return ",".join(f"{key}x{value}" for key, value in sorted(counts.items()))


def _tech_design_hints(state: BrainState) -> str:
    """技术方案只作抽取提示，绝不进入 quote 回指语料。"""
    tech_design = state.get("tech_design") or {}
    acceptance = tech_design.get("acceptance") if isinstance(tech_design, dict) else None
    if not isinstance(acceptance, list) or not acceptance:
        return "（无）"
    lines = [f"- {str(item)[:200]}" for item in acceptance[:20] if str(item).strip()]
    return "\n".join(lines) or "（无）"


async def extract_requirements(state: BrainState) -> dict:
    """EXTRACT_REQUIREMENTS：从权威需求源建立可审计的结构化分母。"""
    description = (state.get("task_description") or "").strip()
    clarify_summary = (state.get("clarify_summary") or "").strip()
    if not description and not clarify_summary:
        logger.warning("[EXTRACT_REQ] 需求源文本为空，降级 items=[]（不调 LLM）")
        return {
            "requirement_items": [],
            "requirement_denominator_complete": False,
            "requirement_denominator_reason": "empty_source",
            "degraded_reasons": ["requirements_extract:empty_source"],
        }

    source_text = description + ("\n" + clarify_summary if clarify_summary else "")
    existing_items = state.get("requirement_items")
    if existing_items:
        if requirement_denominator_snapshot_complete(
            complete=state.get("requirement_denominator_complete"),
            reason=state.get("requirement_denominator_reason"),
            source_text=source_text,
            items=existing_items,
        ):
            return {
                "requirement_denominator_complete": True,
                "requirement_denominator_reason": "",
            }
        logger.warning(
            "[EXTRACT_REQ] checkpoint 需求分母未通过当前规则重验，丢弃旧条目并重新抽取"
        )
    truncated = source_is_truncated(description)
    min_expected = _minimum_expected_items(source_text)
    items: list[dict] = []
    rejected: list[dict] = []
    best_items: list[dict] = []
    best_rejected: list[dict] = []
    best_coverage = -1
    retry_feedback = ""
    llm_error: str | None = None
    got_llm_output = False

    for attempt in range(1 + MAX_EXTRACT_RETRIES):
        try:
            from swarm.brain import nodes as _nodes

            llm = _nodes._get_brain_llm()
            response = await llm.ainvoke([
                {"role": "system", "content": REQUIREMENTS_EXTRACT_SYSTEM},
                {"role": "user", "content": REQUIREMENTS_EXTRACT_USER.format(
                    description=description or "（无）",
                    clarify=clarify_summary or "（无）",
                    hints=_tech_design_hints(state),
                    retry_feedback=retry_feedback,
                )},
            ])
            raw = _nodes._parse_json_from_llm(response.content)
        except Exception as exc:  # noqa: BLE001
            llm_error = str(exc)[:120]
            logger.warning(
                "[EXTRACT_REQ] LLM 调用/解析失败（第 %d 次）: %s",
                attempt + 1,
                llm_error,
            )
            from swarm.brain.planning_nodes import _await_token_admission, _is_token_limit_error

            if _is_token_limit_error(exc) and not await _await_token_admission(
                state.get("task_id"),
                getattr(exc, "usage", None) or {},
                max_wait_s=600.0,
            ):
                break
            retry_feedback = "\n【上一轮输出无法解析为规定 JSON，请严格按 schema 仅输出 JSON】\n"
            continue

        got_llm_output = True
        raw_items = raw.get("items") if isinstance(raw, dict) else raw
        items, rejected = validate_requirement_items(raw_items, source_text)
        covered_evidence, expected_evidence = deterministic_evidence_coverage(
            items, source_text
        )
        if (covered_evidence, len(items)) > (best_coverage, len(best_items)):
            best_items, best_rejected = items, rejected
            best_coverage = covered_evidence

        if items:
            evidence_complete = (
                expected_evidence == 0 or covered_evidence == expected_evidence
            )
            if (
                len(items) >= min_expected and evidence_complete
            ) or attempt >= MAX_EXTRACT_RETRIES:
                if len(items) < min_expected or not evidence_complete:
                    logger.warning(
                        "[EXTRACT_REQ] F5 分母不完整（条目 %d/%d，证据槽 %d/%d，源 %d 字符）"
                        "重试额度已尽，如实收下",
                        len(items), min_expected, covered_evidence, expected_evidence,
                        len(source_text),
                    )
                break
            logger.warning(
                "[EXTRACT_REQ] F5 轮级质量闸：第 %d 轮条目 %d/%d、证据槽 %d/%d"
                "（源 %d 字符）→ 带反馈重抽",
                attempt + 1, len(items), min_expected, covered_evidence,
                expected_evidence, len(source_text),
            )
            retry_feedback = (
                f"\n【上一轮抽取 {len(items)} 条（期望至少 {min_expected}），且只覆盖 "
                f"{covered_evidence}/{expected_evidence} 个确定性需求证据槽。请逐段通读全文，"
                "完整穷举功能/接口/约束/验收需求，绝不要只摘开头几条】\n"
            )
            continue

        logger.warning(
            "[EXTRACT_REQ] 第 %d 次抽取零合法条目（rejected: %s）",
            attempt + 1,
            _rejected_summary(rejected) or "无输出",
        )
        retry_feedback = (
            "\n【上一轮输出全部被确定性校验剔除："
            f"{_rejected_summary(rejected) or '空清单'}。"
            "source_quote 必须从需求文本逐字复制，请重新抽取】\n"
        )

    current_coverage, _ = deterministic_evidence_coverage(items, source_text)
    if (best_coverage, len(best_items)) > (current_coverage, len(items)):
        logger.warning(
            "[EXTRACT_REQ] 末轮（覆盖 %d、条目 %d）劣于历史最优轮"
            "（覆盖 %d、条目 %d）→ 采用最优轮结果",
            current_coverage, len(items), best_coverage, len(best_items),
        )
        items, rejected = best_items, best_rejected

    if truncated:
        items = [{**item, "source_truncated": True} for item in items]
    if not items and not got_llm_output and llm_error:
        raise RuntimeError(
            f"EXTRACT_REQ 全部 {1 + MAX_EXTRACT_RETRIES} 次 LLM 调用失败（{llm_error}）"
            "——需求分母无从建立，拒绝以空需求清单继续"
        )

    grounded_items_truncated = any(
        str(row.get("reason") or "") == "over_limit"
        for row in rejected if isinstance(row, dict)
    )
    covered_evidence, expected_evidence = deterministic_evidence_coverage(
        items, source_text
    )
    evidence_incomplete = bool(
        expected_evidence and covered_evidence < expected_evidence
    )
    below_expected = bool(items) and len(items) < min_expected
    if truncated:
        denominator_reason = "source_truncated"
    elif not items:
        denominator_reason = "empty"
    elif grounded_items_truncated:
        denominator_reason = "grounded_items_truncated"
    elif evidence_incomplete:
        denominator_reason = "evidence_units_uncovered"
    elif below_expected:
        denominator_reason = "below_expected_after_retries"
    else:
        denominator_reason = ""

    output: dict = {
        "requirement_items": items,
        "requirement_denominator_complete": not denominator_reason,
        "requirement_denominator_reason": denominator_reason,
    }
    degraded: list[str] = []
    if truncated:
        degraded.append("requirements_extract:source_truncated")
    if rejected:
        degraded.append(
            f"requirements_extract:rejected={len(rejected)}({_rejected_summary(rejected)})"
        )
    if grounded_items_truncated:
        degraded.append("requirements_extract:grounded_items_truncated")
    if evidence_incomplete:
        degraded.append(
            f"requirements_extract:evidence_coverage={covered_evidence}<{expected_evidence}"
        )
    if below_expected:
        degraded.append(
            f"requirements_extract:insufficient_count={len(items)}<{min_expected}"
        )
    if not items:
        reason = (
            f"llm_failed:{llm_error}"
            if llm_error and not rejected
            else "all_rejected_or_empty"
        )
        degraded.append(f"requirements_extract:empty({reason})")
    if degraded:
        output["degraded_reasons"] = degraded

    logger.info(
        "[EXTRACT_REQ] 完成：%d 条合法条目，%d 条被拒%s",
        len(items), len(rejected), "，源文本经截断" if truncated else "",
    )
    if rejected:
        detail_rows = rejected[:40]
        detail = json.dumps(detail_rows, ensure_ascii=False)
        while len(detail) > 4000 and detail_rows:
            detail_rows = detail_rows[:-1]
            detail = json.dumps(detail_rows, ensure_ascii=False)
        logger.info(
            "[EXTRACT_REQ] 被拒明细(误杀审计，%d/%d 条): %s",
            len(detail_rows), len(rejected), detail,
        )
    return output


__all__ = ["extract_requirements"]
