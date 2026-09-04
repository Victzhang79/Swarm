"""需求条目的确定性身份、引文回指、结构分母与完整性校验。"""

from __future__ import annotations

import logging
import re
from typing import Any

from swarm.brain.requirements_identity import (
    fold_for_quote_match as _fold_for_quote_match,
    normalize_for_id,
    requirement_id,
)
from swarm.brain.requirements_grounding import (
    quote_grounded_spans as _quote_grounded_spans,
    quote_is_grounded as _quote_is_grounded,
)

logger = logging.getLogger(__name__)

# ── 常量 ──

# ingest.summarize_to_budget（brain/ingest.py）超预算截断时插入的中段省略标记。
# 截断可观测的唯一确定性证据面：IngestResult.documents 的 truncated 标志不进 state，
# 但标记随增强后的 task_description 持久化。与 ingest 行为的一致性由
# test_requirements_extract_s2_2.py::test_truncation_detector_matches_real_ingest_output
# 行为锁定（改 ingest 标记不同步此处会即刻红）。
TRUNCATION_MARKER = "…（文档过长，中间内容已省略）…"

MAX_EXTRACT_RETRIES = 2       # LLM 抽取有界重试：首发 + 2 次重试后如实降级
MAX_ITEMS = 100               # 抽取失控熔断阀（ACCEPTANCE_DESIGN §6.2）。R32-4 用户拍板：
                              # 旧值 60 基于"正常 PRD<60 条"设计假设，三轮 E2E 实测合格
                              # 74/96/88 条证伪——切的是真需求。env SWARM_EXTRACT_MAX_ITEMS
                              # 可调；超限截断按 kind 优先级非到达序（_KIND_PRIORITY）。
MAX_ITEM_TEXT_CHARS = 500     # 单条超长=段落而非条目
MIN_QUOTE_CHARS = 4           # 归一化后过短的 quote 无回指力（如单个词），拒收

# kind 枚举（功能/数据/接口/页面/其他）。存储用英文标识（下游 acceptance_assertions 的
# kind="http_probe" 同风格），LLM 输出中英文同义词都归一。
REQUIREMENT_KINDS = ("functional", "data", "api", "page", "other")

# R32-4：超限截断的 kind 优先级（值小优先收留）。round37b 用户拍板废止其在截断中的使用
# ——NFR（可用/安全/幂等/可插拔）落 kind=other 被最先砍，违背"如实还原需求第一"。保留常量
# 仅供其他诊断/排序用途，over_limit 截断改到达序 keep-first（见 _effective_items_limit）。
_KIND_PRIORITY = {"functional": 0, "api": 1, "data": 2, "page": 3, "other": 4}

# P4（round37b）：抽取上限【随规模自适应】——治"固定绝对阈值把 LLM 失控与 PRD 真大混为一谈"
# （用户拍板，见 memory/swarm-req-extract-over-limit-fixed-threshold）。真失控信号=低接地/
# 高重复，已由 quote_not_in_source + duplicate 单独抓；接地且非重复的条目数多=PRD 真大，非
# 失控。故阈值 = max(配置下限, min(硬 backstop, 源料规模//每条最小源字符))：大 PRD 的真需求
# 不被固定阈值砍，仅真病态爆炸（远超任何真实 PRD）撞硬 backstop 才截断。
_CHARS_PER_REQ = 40          # 一条可接地需求至少约需的源字符数（保守，防 tiny 源料 runaway）
_HARD_MAX_ITEMS = 500        # 绝对 backstop：真病态爆炸（远超任何真实 PRD）才截断


def _max_items_limit() -> int:
    """R32-4：抽取上限 env 可调（非法值 WARNING 回退默认，配置错不冒充运行时故障）。"""
    import os
    raw = os.environ.get("SWARM_EXTRACT_MAX_ITEMS", "") or ""
    try:
        val = int(raw) if raw.strip() else MAX_ITEMS
        if val <= 0:
            logger.warning("[EXTRACT_REQ] SWARM_EXTRACT_MAX_ITEMS 非正数(%r)——回退默认 %d",
                           raw, MAX_ITEMS)
            return MAX_ITEMS
        return val
    except ValueError:
        logger.warning("[EXTRACT_REQ] SWARM_EXTRACT_MAX_ITEMS 配置非法(%r)——回退默认 %d",
                       raw, MAX_ITEMS)
        return MAX_ITEMS


def _effective_items_limit(source_text: str) -> int:
    """P4：随源料规模自适应的抽取上限。

    = max(配置下限, min(硬 backstop, len(源料)//每条最小源字符))。配置下限（_max_items_limit，
    含 env 覆盖）永远被尊重（用户显式覆盖优先）；自适应分量只在其上【上抬】以容纳大 PRD 的真
    需求，并被 _HARD_MAX_ITEMS 封顶防病态爆炸。源料越大→可容纳的真需求越多（规模即尺度）。
    """
    floor = _max_items_limit()
    adaptive = min(_HARD_MAX_ITEMS, len(source_text or "") // _CHARS_PER_REQ)
    return max(floor, adaptive)
_KIND_ALIASES = {
    "functional": "functional", "function": "functional", "feature": "functional",
    "功能": "functional",
    "data": "data", "数据": "data",
    "api": "api", "interface": "api", "接口": "api",
    "page": "page", "ui": "page", "view": "page", "页面": "page",
    "other": "other", "misc": "other", "其他": "other",
}
_ALLOWED_SOURCES = ("description", "attachment", "clarify")

# ══════════════════════════════════════════════
# 纯函数（离线可测）
# ══════════════════════════════════════════════

def source_is_truncated(source_text: str) -> bool:
    """需求源文本是否经过 ingest 预算截断（中段省略）——漏抽条目的第一确定性来源。"""
    return TRUNCATION_MARKER in (source_text or "")


def requirement_denominator_provenance_complete(
    *,
    complete: object,
    reason: object,
    source_text: str,
    items: object,
) -> bool:
    """完整分母正向不变量；注入/恢复边界不得把矛盾账洗成 complete。"""
    return bool(
        complete is True
        and not str(reason or "").strip()
        and not source_is_truncated(source_text)
        and isinstance(items, list)
        and items
        and all(
            isinstance(item, dict) and item.get("source_truncated") is not True
            for item in items
        )
    )


def requirement_denominator_snapshot_complete(
    *, complete: object, reason: object, source_text: str, items: object,
) -> bool:
    """按当前规则重验完整分母快照；旧布尔账不能单独授权幂等跳过。"""
    if not requirement_denominator_provenance_complete(
        complete=complete, reason=reason, source_text=source_text, items=items,
    ):
        return False
    validated, rejected = validate_requirement_items(items, source_text)
    if rejected or len(validated) != len(items):
        return False
    covered, evidence_total = deterministic_evidence_coverage(validated, source_text)
    return bool(
        len(validated) >= _minimum_expected_items(source_text)
        and (evidence_total == 0 or covered == evidence_total)
    )


def _structural_heading_keys(source_text: str) -> set[str]:
    """返回紧邻列表/表格的冒号标题键；标题 marker 不应另算一条需求。"""
    keys: set[str] = set()
    source_lines = source_text.splitlines()
    for index, line in enumerate(source_lines):
        stripped = line.strip()
        if not stripped.endswith((":", "：")):
            continue
        following = next(
            (candidate for candidate in source_lines[index + 1:] if candidate.strip()),
            "",
        )
        following_cells, following_delimiters = _split_markdown_table_row(following)
        if (
            _STRUCTURED_REQUIREMENT_LINE_RE.match(following)
            or (following_delimiters >= 1 and len(following_cells) >= 2)
        ):
            keys.add(normalize_for_id(stripped))
    return keys


def _source_requirement_units(
    source_text: str,
    normalized_source: str,
) -> list[tuple[int, int, frozenset[str]]]:
    """把源文本切成 clause/list/table-row 内可独立消费的义务槽。"""
    units: list[tuple[int, int, frozenset[str]]] = []
    structured_keys = _structured_requirement_lines(source_text)
    table_line_indexes = set(_markdown_table_data_row_entries(source_text))
    heading_keys = _structural_heading_keys(source_text)
    cursor = 0
    for line_index, raw_line in enumerate(source_text.splitlines()):
        normalized_line = _fold_for_quote_match(raw_line)
        if not normalized_line:
            continue
        line_begin = normalized_source.find(normalized_line, cursor)
        if line_begin < 0:
            line_begin = normalized_source.find(normalized_line)
        if line_begin < 0:
            continue
        line_key_source = raw_line.strip()
        if line_key_source.startswith("|") and line_key_source.endswith("|"):
            line_key_source = line_key_source[1:-1]
        line_key = normalize_for_id(line_key_source)
        is_list = line_key in structured_keys
        is_table = line_index in table_line_indexes
        raw_units = [raw_line] if is_list or is_table else re.split(r"[。；;]+", raw_line)
        unit_cursor = line_begin
        for raw_unit in raw_units:
            normalized_unit = _fold_for_quote_match(raw_unit)
            if not normalized_unit:
                continue
            begin = normalized_source.find(normalized_unit, unit_cursor)
            if begin < 0:
                continue
            end = begin + len(normalized_unit)
            unit_key = normalize_for_id(raw_unit.strip())
            markers = [] if unit_key in heading_keys else list(
                _EXPLICIT_REQUIREMENT_MARKER_RE.finditer(raw_unit)
            )
            evidence_kinds = set()
            if markers:
                evidence_kinds.add("marker")
            if is_list:
                evidence_kinds.add("list")
            if is_table:
                evidence_kinds.add("table")
            if len(markers) <= 1:
                units.append((begin, end, frozenset(evidence_kinds)))
            else:
                boundaries = [0] + [
                    len(_fold_for_quote_match(raw_unit[:marker.start()]))
                    for marker in markers[1:]
                ] + [len(normalized_unit)]
                units.extend(
                    (begin + left, begin + right, frozenset(evidence_kinds))
                    for left, right in zip(boundaries, boundaries[1:])
                    if right > left
                )
            unit_cursor = end
        cursor = line_begin + len(normalized_line)
    return units


def _quote_candidate_slots(
    normalized_quote: str,
    normalized_source: str,
    source_units: list[tuple[int, int, frozenset[str]]],
) -> list[tuple[int, ...]]:
    """返回 quote 每种合法回指在源义务槽上的映射。"""
    direct_spans: list[tuple[int, int]] = []
    start = normalized_source.find(normalized_quote)
    while start >= 0:
        direct_spans.append((start, start + len(normalized_quote)))
        start = normalized_source.find(normalized_quote, start + 1)
    grounded_spans = _quote_grounded_spans(normalized_quote, normalized_source)
    provenance_candidates: list[tuple[tuple[int, int], ...]] = (
        [((begin, end),) for begin, end in direct_spans]
        if direct_spans
        else [tuple(grounded_spans)] if grounded_spans else []
    )
    return [
        tuple(
            unit_id
            for unit_id, (begin, end, _evidence_kinds) in enumerate(source_units)
            if any(
                span_begin < end and begin < span_end
                for span_begin, span_end in candidate
            )
        )
        for candidate in provenance_candidates
    ]


def _evidence_unit_groups(
    source_units: list[tuple[int, int, frozenset[str]]],
    normalized_source: str,
) -> tuple[dict[int, int], int]:
    """把 prose/list/table 中描述同一义务的重复证据折叠为一个分母槽。"""
    group_cores: list[str] = []
    group_kinds: list[frozenset[str]] = []
    unit_groups: dict[int, int] = {}
    for unit_id, (begin, end, evidence_kinds) in enumerate(source_units):
        if not evidence_kinds:
            continue
        core = _requirement_evidence_core(
            normalize_for_id(normalized_source[begin:end])
        )
        group_id = next(
            (
                index
                for index, existing in enumerate(group_cores)
                if core == existing
                or _evidence_cores_overlap(
                    core, existing, evidence_kinds, group_kinds[index]
                )
            ),
            None,
        )
        if group_id is None:
            group_id = len(group_cores)
            group_cores.append(core)
            group_kinds.append(evidence_kinds)
        else:
            group_kinds[group_id] = group_kinds[group_id] | evidence_kinds
        unit_groups[unit_id] = group_id
    return unit_groups, len(group_cores)


def _evidence_surfaces_can_fold(
    left: frozenset[str], right: frozenset[str],
) -> bool:
    """仅跨 prose/list/table 表达面折叠；同一列表内包含关系仍是不同需求。"""
    left_surfaces = left - {"marker"}
    right_surfaces = right - {"marker"}
    return bool(left_surfaces or right_surfaces) and left_surfaces.isdisjoint(
        right_surfaces
    )


def _evidence_cores_overlap(
    left_core: str,
    right_core: str,
    left_kinds: frozenset[str],
    right_kinds: frozenset[str],
) -> bool:
    """跨表达面同义证据折叠；双方显式义务允许较短的中文核心。"""
    minimum = 4 if "marker" in left_kinds and "marker" in right_kinds else 6
    return bool(
        _evidence_surfaces_can_fold(left_kinds, right_kinds)
        and min(len(left_core), len(right_core)) >= minimum
        and (left_core in right_core or right_core in left_core)
    )


def _select_quote_units(
    normalized_quote: str,
    normalized_source: str,
    source_units: list[tuple[int, int, frozenset[str]]],
    unit_groups: dict[int, int],
) -> tuple[tuple[int, ...] | None, bool]:
    """选择唯一回指；第二返回值表示 quote 跨/混淆多个确定性义务。"""
    candidates = list(dict.fromkeys(
        _quote_candidate_slots(normalized_quote, normalized_source, source_units)
    ))
    if not candidates or any(not candidate for candidate in candidates):
        return None, False
    if len(candidates) > 1:
        quote_core = _requirement_evidence_core(normalize_for_id(normalized_quote))
        exact = [
            candidate
            for candidate in candidates
            if len(candidate) == 1
            and _requirement_evidence_core(normalize_for_id(
                normalized_source[source_units[candidate[0]][0]:source_units[candidate[0]][1]]
            )) == quote_core
        ]
        if len(exact) == 1:
            candidates = exact
        else:
            evidence_mappings = {
                tuple(sorted({unit_groups[unit_id] for unit_id in candidate
                              if unit_id in unit_groups}))
                for candidate in candidates
            }
            if any(evidence_mappings) and len(evidence_mappings) > 1:
                return None, True
            candidates = [candidates[0]]
    selected = candidates[0]
    evidence_groups = {
        unit_groups[unit_id] for unit_id in selected if unit_id in unit_groups
    }
    if len(evidence_groups) > 1:
        return None, True
    return selected, False


def deterministic_evidence_coverage(
    items: list[dict], source_text: str
) -> tuple[int, int]:
    """返回已覆盖/应覆盖的确定性需求证据槽数。"""
    normalized_source = _fold_for_quote_match(source_text or "")
    source_units = _source_requirement_units(source_text or "", normalized_source)
    unit_groups, expected = _evidence_unit_groups(source_units, normalized_source)
    covered: set[int] = set()
    for item in items:
        quote = _fold_for_quote_match(str(item.get("source_quote") or ""))
        selected_units, ambiguous = _select_quote_units(
            quote, normalized_source, source_units, unit_groups
        )
        if ambiguous or not selected_units:
            continue
        group_id = next(
            (unit_groups[unit_id] for unit_id in selected_units if unit_id in unit_groups),
            None,
        )
        if group_id is not None:
            covered.add(group_id)
    return len(covered), expected


def validate_requirement_items(
    raw_items: Any, source_text: str
) -> tuple[list[dict], list[dict]]:
    """LLM 输出 → (合法条目, 被拒条目) 的确定性校验。零 LLM、纯函数。

    每条合法条目：{id, text, kind, source_quote, source}。逐条剔除（拒单条不拒全量）：
      not_object / empty_text / too_long / empty_quote / quote_too_short /
      quote_not_in_source（防幻觉核心：空白归一+全半角标点折叠后 quote 须【接地】——
      连续 substring 或源料贪心平铺覆盖 ≥ 阈值，见 _quote_is_grounded；R35-B 治表格竖线/
      跨行拼接的结构性误杀，防幻觉底线不塌）/
      duplicate（归一化内容 hash 相同，keep-first）/ quote_ambiguous（引文横跨多个义务槽，
      或同文在不同槽重复出现）/ duplicate_quote（同一原文出处只允许支撑一个条目，防止用
      不同复述灌满分母）/ over_limit（超 MAX_ITEMS=抽取失控）。
    被拒条目不静默丢：返回 [{"reason", "text_head"}] 供调用方入 degraded 可观测。
    """
    items: list[dict] = []
    rejected: list[dict] = []
    seen_ids: set[str] = set()
    occupied_source_units: set[int] = set()
    normalized_source = _fold_for_quote_match(source_text or "")
    source_units = _source_requirement_units(source_text or "", normalized_source)
    unit_groups, _expected_evidence = _evidence_unit_groups(
        source_units, normalized_source
    )

    if not isinstance(raw_items, list):
        raw_items = []

    for raw in raw_items:
        if not isinstance(raw, dict):
            rejected.append({"reason": "not_object", "text_head": str(raw)[:80]})
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            rejected.append({"reason": "empty_text", "text_head": ""})
            continue
        if len(text) > MAX_ITEM_TEXT_CHARS:
            rejected.append({"reason": "too_long", "text_head": text[:80]})
            continue
        quote = str(raw.get("source_quote") or "").strip()
        if not quote:
            rejected.append({"reason": "empty_quote", "text_head": text[:80]})
            continue
        normalized_quote = _fold_for_quote_match(quote)
        if len(normalized_quote) < MIN_QUOTE_CHARS:
            rejected.append({"reason": "quote_too_short", "text_head": text[:80]})
            continue
        if not _quote_grounded_spans(normalized_quote, normalized_source):
            # 防幻觉核心：给不出真出处（连续 substring 或源料平铺接地）的条目一律拒收。
            # R35-B：表格竖线/跨行拼接的结构性误杀由 Tier2 源料平铺救回（见 _quote_is_grounded）。
            rejected.append({"reason": "quote_not_in_source", "text_head": text[:80]})
            continue
        item_id = requirement_id(text)
        if item_id in seen_ids:
            rejected.append({"reason": "duplicate", "text_head": text[:80]})
            continue
        selected_source_units, ambiguous = _select_quote_units(
            normalized_quote, normalized_source, source_units, unit_groups
        )
        if ambiguous:
            rejected.append({"reason": "quote_ambiguous", "text_head": text[:80]})
            continue
        if not selected_source_units:
            rejected.append({"reason": "quote_not_in_source", "text_head": text[:80]})
            continue
        if occupied_source_units.intersection(selected_source_units):
            rejected.append({"reason": "duplicate_quote", "text_head": text[:80]})
            continue
        kind = _KIND_ALIASES.get(str(raw.get("kind") or "").strip().casefold(), "other")
        source = raw.get("source")
        if source not in _ALLOWED_SOURCES:
            source = "description"
        seen_ids.add(item_id)
        occupied_source_units.update(selected_source_units)
        items.append({
            "id": item_id,
            "text": text,
            "kind": kind,
            "source_quote": quote,
            "source": source,
        })
    # P4（round37b）：超限截断【自适应阈值 + 到达序 keep-first】。阈值随源料规模上抬，让大
    # PRD 的真需求不被固定 100 砍（真失控=低接地/高重复已被上面单独抓）。撞（自适应）阈值时
    # 按【到达序】保前段——kind 中性，绝不再按 kind 优先级系统性砍掉整类 NFR/other（round37b
    # 实测漏 6 条真 NFR，用户判定违背"如实还原需求第一"）。未超限=零行为变化。
    limit = _effective_items_limit(source_text or "")
    if len(items) > limit:
        logger.warning(
            "[EXTRACT_REQ] 抽取条目 %d 超自适应上限 %d（源料 %d 字符）——按到达序截留前 %d 条，"
            "余 %d 条记 over_limit", len(items), limit, len(source_text or ""),
            limit, len(items) - limit)
        rejected.extend({"reason": "over_limit", "text_head": items[i]["text"][:80]}
                        for i in range(limit, len(items)))
        items = items[:limit]
    return items, rejected


_EXPLICIT_REQUIREMENT_MARKER_RE = re.compile(
    r"(?:必须|应当|不得|禁止|务必|需要|应该|须|(?<![按无供刚])需(?!求|量|要)|"
    r"\bmust\b|\bshall\b|\bshould\b|\brequired\s+to\b)",
    re.IGNORECASE,
)
_STRUCTURED_REQUIREMENT_LINE_RE = re.compile(
    r"^\s*(?:(?:[-*+•·–—]\s+)|"
    r"(?:\d{1,3}[.)、．]\s*)|"
    r"(?:[（(]\d{1,3}[）)]\s*)|"
    r"(?:[A-Za-z][.)]\s+)|"
    r"(?:[(][A-Za-z][)]\s+)|"
    r"(?:(?:requirements?|req)\s*[-#]?\s*\d{1,3}\s*[:.)-]\s*)|"
    r"(?:[一二三四五六七八九十百]+[、.)．]\s*)|"
    r"(?:[①-⑳]\s+))\S+"
, re.IGNORECASE)
_MARKDOWN_TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")
_REQUIREMENT_TABLE_HEADER_RE = re.compile(
    # “接口/字段/endpoints/fields”常是现状盘点或同一实体的数据字典，逐行计数会
    # 与抽取器的需求粒度分叉；只有明确需求语义的表头才作为保守分母证据。
    r"(?:功能|需求|规则|约束|验收|能力|行为|"
    r"\brequirements?\b|\bfeatures?\b|"
    r"\bconstraints?\b|\bacceptance\b)",
    re.IGNORECASE,
)
_NON_REQUIREMENT_TABLE_HEADER_RE = re.compile(
    r"(?:现有|当前|已上线|盘点|清单|\bexisting\b|\bcurrent\b|\binventory\b)",
    re.IGNORECASE,
)
_STRONG_REQUIREMENT_TABLE_HEADER_RE = re.compile(
    r"(?:需求|要求|规则|约束|验收|\brequirements?\b|"
    r"\bconstraints?\b|\bacceptance\b)",
    re.IGNORECASE,
)
_REQUIREMENT_LIST_CONTEXT_RE = re.compile(
    r"(?:需求|要求|功能|规则|约束|验收|能力|行为|"
    r"\brequirements?\b|\bfeatures?\b|\bconstraints?\b|\bacceptance\b)",
    re.IGNORECASE,
)
_NON_REQUIREMENT_LIST_CONTEXT_RE = re.compile(
    r"(?:技术栈|技术选型|依赖|环境|团队|成员|人员|角色|目标用户|版本|目录|参考|背景|现状|"
    r"现有|当前|盘点|清单|"
    r"\btech(?:nology)?\s*stack\b|\bdependencies\b|\bteam\b|\bmembers?\b|"
    r"\broles?\b|\bversions?\b|\bexisting\b|\bcurrent\b|\binventory\b)",
    re.IGNORECASE,
)
_CURRENT_REQUIREMENT_CONTEXT_RE = re.compile(
    r"(?:本次|此次|本轮|新增|待实现|\bthis\s+(?:change|release|task)\b)",
    re.IGNORECASE,
)


def _is_strong_requirement_context(text: str) -> bool:
    return bool(
        _STRONG_REQUIREMENT_TABLE_HEADER_RE.search(text)
        or _EXPLICIT_REQUIREMENT_MARKER_RE.search(text)
        or _CURRENT_REQUIREMENT_CONTEXT_RE.search(text)
    )


def _split_markdown_table_row(line: str) -> tuple[list[str], int]:
    """按未转义、非行内代码的竖线分列，并保留无外框表的尾空单元格。"""
    text = line.strip()
    cells: list[str] = []
    current: list[str] = []
    delimiters = 0
    code_ticks = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "`":
            end = index
            while end < len(text) and text[end] == "`":
                end += 1
            run = end - index
            if code_ticks == 0:
                code_ticks = run
            elif code_ticks == run:
                code_ticks = 0
            current.extend(text[index:end])
            index = end
            continue
        if char == "|" and code_ticks == 0:
            backslashes = 0
            pos = len(current) - 1
            while pos >= 0 and current[pos] == "\\":
                backslashes += 1
                pos -= 1
            if backslashes % 2:
                current.pop()
                current.append("|")
            else:
                cells.append("".join(current).strip())
                current = []
                delimiters += 1
            index += 1
            continue
        current.append(char)
        index += 1
    cells.append("".join(current).strip())
    if text.startswith("|") and cells and cells[0] == "":
        cells.pop(0)
    # 只把“首尾成对”的竖线视为外框。无首竖线时末尾 `|` 可能是最后一列为空，
    # 不能 strip 掉这个唯一分隔符。
    if text.startswith("|") and text.endswith("|") and cells and cells[-1] == "":
        cells.pop()
    return cells, delimiters


def _markdown_table_data_row_entries(source_text: str) -> dict[int, str]:
    """返回标准 Markdown 表数据行的真实行号与规范键。"""
    rows: dict[int, str] = {}
    in_table_body = False
    table_columns = 0
    previous_cells: list[str] = []
    preceding_context = ""
    header_context = ""
    for line_index, line in enumerate(source_text.splitlines()):
        cells, delimiters = _split_markdown_table_row(line)
        # CommonMark 表格允许省略首尾竖线；一根列分隔符即可证明有两列。
        is_table_row = delimiters >= 1 and len(cells) >= 2
        is_separator = is_table_row and all(
            _MARKDOWN_TABLE_SEPARATOR_CELL_RE.fullmatch(cell) for cell in cells
        )
        if is_separator:
            header = "|".join(previous_cells)
            table_evidence = "\n".join(
                part for part in (header_context, header) if part
            )
            in_table_body = bool(
                _REQUIREMENT_TABLE_HEADER_RE.search(header)
                and (
                    _is_strong_requirement_context(table_evidence)
                    or not _NON_REQUIREMENT_TABLE_HEADER_RE.search(table_evidence)
                )
            )
            table_columns = len(cells)
            previous_cells = []
            continue
        if in_table_body and delimiters >= 1 and cells:
            # GFM 表体少列时补空、多列时忽略尾列；严格等长会把合法表第一行误判成表结束，
            # 使三行需求静默退成 size_floor=1。仍要求真实分隔符，普通散文不放宽。
            body_cells = (cells[:table_columns] + [""] * table_columns)[:table_columns]
            if not any(body_cells):
                continue
            rows[line_index] = normalize_for_id("|".join(body_cells))
            continue
        in_table_body = False
        table_columns = 0
        if is_table_row:
            previous_cells = cells
            header_context = preceding_context
        else:
            previous_cells = []
            if line.strip():
                preceding_context = line.strip()
    return rows


def _markdown_table_data_rows(source_text: str) -> set[str]:
    """提取标准 Markdown 表数据行的规范键，供分母跨表达面去重。"""
    return set(_markdown_table_data_row_entries(source_text).values())


def _structured_requirement_lines(source_text: str) -> set[str]:
    """提取需求枚举，同时排除有明确元数据标题的清单。

    无标题编号列表仍按需求计数，保留对短 PRD 的 fail-closed 保护；只有相邻标题能
    确定其为技术栈、团队等元数据时才排除，避免用内容关键词猜测条目语义。
    """
    rows: set[str] = set()
    preceding_context = ""
    in_list = False
    include_block = True
    for line in source_text.splitlines():
        if _STRUCTURED_REQUIREMENT_LINE_RE.match(line):
            if not in_list:
                strong_requirement_context = _is_strong_requirement_context(
                    preceding_context
                )
                include_block = bool(
                    strong_requirement_context
                    or not _NON_REQUIREMENT_LIST_CONTEXT_RE.search(preceding_context)
                )
            if include_block:
                rows.add(normalize_for_id(line))
            in_list = True
            continue
        in_list = False
        if line.strip():
            preceding_context = line.strip()
    return rows


def _requirement_evidence_core(normalized: str) -> str:
    """剥离常见主语/义务模态，供跨 prose、列表、表格的保守重复证据折叠。"""
    core = normalized
    for prefix in (
        "thesystem", "system", "系统", "平台", "服务", "应用", "模块", "程序",
        "客户端", "服务端", "用户",
    ):
        if core.startswith(prefix):
            core = core[len(prefix):]
            break
    for modal in (
        "requiredto", "must", "shall", "should", "必须", "应当", "需要", "应该",
        "务必", "不得", "禁止", "须", "需",
    ):
        if core.startswith(modal):
            core = core[len(modal):]
            break
    return core or normalized


def _minimum_expected_items(source_text: str) -> int:
    """返回可由源文本本身证明的保守条目下限。

    字符规模只能发现长文低产，短而密集的规范仍会漏检。显式义务词则是源内的
    确定性结构证据：每个 ``必须/shall`` 等标记至少代表一个待抽取义务。这里不把
    普通项目符号或模糊的“可以/支持”算入，避免把说明性列表误判为需求。
    """
    # 字符规模只是启发式，保留旧 20 上限；显式 marker/列表/需求表都是确定性结构，
    # 不得被同一上限截断，否则 30 条真需求抽 20 条也会被签成 complete。
    size_floor = min(20, max(1, len(source_text) // 3000))
    # “现有功能需要改进：”后紧跟列表/表格时，marker 是该结构块的标题语气，不能再额外
    # 算一条需求；否则三行清单会被抬成四条。只在冒号标题且下一非空行具有明确结构时折叠。
    structural_heading_keys: set[str] = set()
    source_lines = source_text.splitlines()
    for index, line in enumerate(source_lines):
        stripped = line.strip()
        if not stripped.endswith((":", "：")):
            continue
        following = next(
            (candidate for candidate in source_lines[index + 1:] if candidate.strip()),
            "",
        )
        following_cells, following_delimiters = _split_markdown_table_row(following)
        if (
            _STRUCTURED_REQUIREMENT_LINE_RE.match(following)
            or (following_delimiters >= 1 and len(following_cells) >= 2)
        ):
            structural_heading_keys.add(normalize_for_id(stripped))
    # 同一 clause 可含多个独立义务，按 marker 数计；重复出现的相同 clause 只取
    # 最大次数，避免复制粘贴/长文重复把保守下限虚增。
    clause_marker_counts: dict[str, int] = {}
    for clause in re.split(r"[\n。；;]+", source_text):
        clause_key = clause.strip()
        if clause_key.startswith("|") and clause_key.endswith("|"):
            clause_key = clause_key[1:-1]
        normalized = normalize_for_id(clause_key)
        if not normalized:
            continue
        if normalized in structural_heading_keys:
            continue
        marker_count = len(_EXPLICIT_REQUIREMENT_MARKER_RE.findall(clause))
        if marker_count:
            clause_marker_counts[normalized] = max(
                clause_marker_counts.get(normalized, 0), marker_count
            )
    structured_lines = _structured_requirement_lines(source_text)
    table_rows = _markdown_table_data_rows(source_text)
    # 三类证据可能相交，也可能分属不同段落。取 max 会把“2 条列表 + 2 条需求表”
    # 系统性压成 2；直接相加又会把列表行里的 must/必须重复计数。用归一化结构单元作并集，
    # 同一单元取最强证据计数，不同单元累加。
    evidence_units: dict[str, tuple[int, frozenset[str]]] = {
        unit: (count, frozenset({"marker"}))
        for unit, count in clause_marker_counts.items()
    }
    for unit in structured_lines | table_rows:
        kind = "list" if unit in structured_lines else "table"
        old_count, old_kinds = evidence_units.get(unit, (0, frozenset()))
        evidence_units[unit] = (max(old_count, 1), old_kinds | {kind})
    deduplicated_units: list[tuple[str, int, frozenset[str]]] = []
    for unit, (count, kinds) in evidence_units.items():
        core = _requirement_evidence_core(unit)
        overlaps_existing = any(
            count == existing_count == 1
            and _evidence_cores_overlap(core, existing_core, kinds, existing_kinds)
            for existing_core, existing_count, existing_kinds in deduplicated_units
        )
        if not overlaps_existing:
            deduplicated_units.append((core, count, kinds))
    explicit_floor = sum(count for _, count, _kinds in deduplicated_units)
    return max(size_floor, explicit_floor)


from swarm.brain.requirements_node import extract_requirements

__all__ = [
    "MAX_EXTRACT_RETRIES",
    "MAX_ITEMS",
    "MAX_ITEM_TEXT_CHARS",
    "REQUIREMENT_KINDS",
    "TRUNCATION_MARKER",
    "extract_requirements",
    "normalize_for_id",
    "requirement_id",
    "source_is_truncated",
    "validate_requirement_items",
]
