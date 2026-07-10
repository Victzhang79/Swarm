"""S2-2 需求条目结构化 — PRD/需求文本 → requirement items（稳定 ID + 防幻觉）。

定案依据 docs/ACCEPTANCE_DESIGN.md（§定案5 / §6 / 给 task#23 的实现指引）：
  - 生成点 = contract_design → plan 边上的轻量节点（所有规划路径的必经汇合点，含
    clarify→assess(simple/medium)→plan 这条绕过 tech_design 的路径——挂 tech_design 会漏它）。
  - 防幻觉 = source_quote 回指原文 substring 确定性校验（空白归一后比对）；"抽取两次对比"
    已被否决（同源幻觉两次都出现 + 小模型输出多样性会高频 flaky）。给不出真 quote 的条目
    被拒——拒单条不拒全量，且绝不静默丢（rejected 计数进 degraded_reasons）。
  - 条目 ID = 内容 hash `req-<sha1(normalize(text))[:8]>`：重抽取顺序漂移不影响 ID；
    replan 稳定性由拓扑天然保证（见 extract_requirements docstring）。
  - fail-closed：LLM 输出 schema 校验不过 → 有界重试 → 耗尽如实降级
    requirement_items=[] + degraded 可观测，绝不塞幻觉条目。下游 task#24 覆盖校验对
    空 items = 跳过 + degraded，不阻塞主链。
  - 通用多栈多领域铁律：本模块 prompt/校验不含任何语言/框架/示例项目/领域词汇。
"""

from __future__ import annotations

import hashlib
import json
import logging
import unicodedata
from typing import Any

from swarm.brain.state import BrainState

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

# ── prompt（通用铁律：无任何领域/技术栈/示例项目词汇）──

REQUIREMENTS_EXTRACT_SYSTEM = """你是需求分析器。把给定的需求文本拆解为独立、可验收的需求条目清单。

规则（务必逐条遵守）：
1. 只抽取需求文本中【明确写出】的需求；绝不臆造，绝不补全你认为"应该有"的需求。
2. 每条条目必须带 source_quote：从需求原文【逐字复制】的一小段（10~80字）作为出处依据。
   给不出原文出处的条目【不要输出】——系统会用原文逐字核对，对不上的条目会被剔除。
3. text 是该条需求的一句话概括（≤200字），忠于原文，不加入原文没有的细节。
4. kind 只能取：functional(功能) / data(数据) / api(接口) / page(页面) / other(其他)。
5. source 标注出处来源：description(任务描述/附件正文) 或 clarify(澄清答复)。
6. 一条只表达一个可独立验收的需求；相同需求不要重复输出。
7. 仅输出 JSON：{"items": [{"text": "...", "kind": "...", "source_quote": "...", "source": "description|clarify"}]}
"""

REQUIREMENTS_EXTRACT_USER = """【需求文本（任务描述+附件正文）】
{description}

【用户澄清答复摘要】
{clarify}

【技术方案给出的验收提示（仅辅助参考——source_quote 仍必须逐字来自上面两段需求文本，不得引用本段）】
{hints}
{retry_feedback}
请输出需求条目 JSON。"""


# ══════════════════════════════════════════════
# 纯函数（离线可测）
# ══════════════════════════════════════════════

def normalize_for_id(text: str) -> str:
    """条目文本归一化（供内容 hash）：去全部空白、去 Unicode 标点、casefold。

    目标：同一条需求在重抽取时因空白/标点/大小写抖动不换 ID（replan/重做稳定）。
    """
    out: list[str] = []
    for ch in str(text):
        if ch.isspace():
            continue
        if unicodedata.category(ch).startswith("P"):
            continue
        out.append(ch.casefold())
    return "".join(out)


def requirement_id(text: str) -> str:
    """稳定条目 ID = req-<sha1(normalize(text))[:8]>。序号 ID 已被否决（顺序漂移即 ID 漂移）。"""
    digest = hashlib.sha1(normalize_for_id(text).encode("utf-8")).hexdigest()
    return f"req-{digest[:8]}"


# S2 复核 S1：quote 回指比对的全半角标点【同义折叠】表——PRD 原文与 LLM quote 之间
# 常见的中英文标点互写（原文"，"被 LLM 复述成 ","等）不该让真 quote 被误拒。
# 只做同义映射不删标点（normalize_for_id 的"全删标点"口径仅用于内容 hash；比对面删光
# 标点会让防幻觉变松——"a，b"与"ab" 不该互相命中）。双侧过同一张表，判定对称。
_PUNCT_FOLD_TABLE = str.maketrans({
    "，": ",", "。": ".", "．": ".", "、": ",",
    "：": ":", "；": ";", "！": "!", "？": "?",
    "（": "(", "）": ")", "【": "[", "】": "]",
    "《": "<", "》": ">", "－": "-", "～": "~",
    "“": '"', "”": '"', "「": '"', "」": '"', "『": '"', "』": '"',
    "‘": "'", "’": "'",
})


def _fold_for_quote_match(text: str) -> str:
    """quote 回指比对归一：去全部空白 + 全半角标点同义折叠（S1）。
    换行/排版空格与标点全半角互写不该让真 quote 被误拒；字符本身保持严格。"""
    return "".join(
        ch for ch in str(text).translate(_PUNCT_FOLD_TABLE) if not ch.isspace())


# R35-B：quote 回指接地阈值（env 可调，配置非法回退默认）。表格竖线打断/跨行拼接的
# 结构性误杀经离线实测坐实：被拒 quote 每片都是源【逐字内容】，只是被 markdown `|` 与
# 跨行切断连续性（非幻觉、非复述）。连续 substring 假设对表格型需求破产 → 加源料平铺 Tier2。
_QUOTE_MIN_TILE_CHARS = 2   # 源子串平铺最小片长（防单字碰巧；2 容纳"邮箱/ID"等短单元格）
_QUOTE_COVER_MIN = 0.85     # quote 内容字符被源片覆盖占比阈值（防"真前缀+编造尾"蒙混过闸）
# R35-B 双复核收紧：Tier2 平铺【前向单调】——源片必须在源中【顺序出现】（上一片之后）。
# 真表格误杀=源单元格【按序】去分隔符拼接（片在源中顺序不变）；而编造 quote 是把散落各处的
# 真词汇【乱序重拼】成源里不存在的新主张（复核双方独立复现："管理员通过邮箱改用户密码"
# 从 4 句不同主题的源里 2-gram 散点凑到 0.85 蒙混过闸）。前向单调即要求"源子序列（含小接缝）"
# 而非"散点词袋"，编造的乱序拼接无法前向平铺 → 覆盖塌 → 仍拒（防幻觉底线复位）。
_QUOTE_MAX_LEN = 600        # Tier2 平铺 quote 长度上限（源 quote 本应 ≤80 字；超长=异常，
#                             退 Tier1 严格判定：既有界化 O(n²·L) 成本，又不给超长编造蒙混空间


def _quote_grounding_params() -> tuple[int, float]:
    """接地阈值 env 覆盖（非法值 WARNING 回退默认，配置错不冒充运行时故障）。
    复核 LOW：越界值（cover_min∉[0,1] / min_tile<1）会把防幻觉闸变near-no-op，钳回默认+WARNING。"""
    import os

    def _one(env: str, default, cast, lo=None, hi=None):
        raw = os.environ.get(env, "") or ""
        try:
            val = cast(raw) if raw.strip() else default
        except (ValueError, TypeError):
            logger.warning("[EXTRACT_REQ] %s 配置非法(%r)——回退默认 %r", env, raw, default)
            return default
        if (lo is not None and val < lo) or (hi is not None and val > hi):
            logger.warning("[EXTRACT_REQ] %s 越界(%r，须∈[%r,%r])——回退默认 %r",
                           env, val, lo, hi, default)
            return default
        return val

    return (_one("SWARM_QUOTE_MIN_TILE_CHARS", _QUOTE_MIN_TILE_CHARS, int, lo=1),
            _one("SWARM_QUOTE_COVER_MIN", _QUOTE_COVER_MIN, float, lo=0.0, hi=1.0))


def _quote_is_grounded(nq: str, nsrc: str) -> bool:
    """quote 回指接地判定（零 LLM、确定性、无模糊匹配、不认语言/框架/格式）。

    Tier1 连续 substring（散文/单元格逐字，行为不变，高置信直通）；不中则 Tier2 源料【前向
    单调】贪心平铺——用源在【游标之后】的最长子串逐段平铺 nq，被 ≥min_tile 长源子串覆盖的
    【内容字符(alnum)】占比 ≥ cover_min 即接地。治本：表格 markdown `|` 打断 / 跨行按序拼接的
    结构性误杀（被拼的每片仍是源逐字内容且【顺序不变】，覆盖≈全）；编造 quote=散落真词汇
    【乱序重拼】成源里不存在的主张——无法前向平铺（片顺序对不上）→ 覆盖低 → 仍拒（防幻觉
    底线，双复核复现坐实）。`|`/空白/连接标点在平铺中当【可跳过的接缝】，不计入覆盖分母。
    超长 quote（>_QUOTE_MAX_LEN，异常形态）退 Tier1 严判：有界成本 + 不给超长编造蒙混。
    """
    if not nq or not nsrc:
        return False
    if nq in nsrc:                      # Tier1：连续 substring，行为不变
        return True
    if len(nq) > _QUOTE_MAX_LEN:        # 异常超长：只认 Tier1（有界 + 防蒙混）
        return False
    min_tile, cover_min = _quote_grounding_params()
    n = len(nq)
    i = 0
    covered = 0
    src_cursor = 0                      # 前向单调游标：下一源片须在上一片之后
    while i < n:                        # Tier2：源最长子串【前向】贪心平铺
        best = 0
        best_pos = -1
        j = i + 1
        while j <= n:
            pos = nsrc.find(nq[i:j], src_cursor)
            if pos < 0:                 # 该长度在游标之后无匹配 → 更长必更无 → 停
                break
            best = j - i
            best_pos = pos
            j += 1
        if best >= min_tile:
            covered += sum(1 for ch in nq[i:i + best] if ch.isalnum())
            src_cursor = best_pos + best   # 前向推进：后续片只能在本片之后
            i += best
        else:
            i += 1                      # 跳过一个接缝/连接符/单字碰巧
    total = sum(1 for ch in nq if ch.isalnum())
    return total > 0 and covered / total >= cover_min


def source_is_truncated(source_text: str) -> bool:
    """需求源文本是否经过 ingest 预算截断（中段省略）——漏抽条目的第一确定性来源。"""
    return TRUNCATION_MARKER in (source_text or "")


def validate_requirement_items(
    raw_items: Any, source_text: str
) -> tuple[list[dict], list[dict]]:
    """LLM 输出 → (合法条目, 被拒条目) 的确定性校验。零 LLM、纯函数。

    每条合法条目：{id, text, kind, source_quote, source}。逐条剔除（拒单条不拒全量）：
      not_object / empty_text / too_long / empty_quote / quote_too_short /
      quote_not_in_source（防幻觉核心：空白归一+全半角标点折叠后 quote 须【接地】——
      连续 substring 或源料贪心平铺覆盖 ≥ 阈值，见 _quote_is_grounded；R35-B 治表格竖线/
      跨行拼接的结构性误杀，防幻觉底线不塌）/
      duplicate（归一化内容 hash 相同，keep-first）/ over_limit（超 MAX_ITEMS=抽取失控）。
    被拒条目不静默丢：返回 [{"reason", "text_head"}] 供调用方入 degraded 可观测。
    """
    items: list[dict] = []
    rejected: list[dict] = []
    seen_ids: set[str] = set()
    normalized_source = _fold_for_quote_match(source_text or "")

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
        if not _quote_is_grounded(normalized_quote, normalized_source):
            # 防幻觉核心：给不出真出处（连续 substring 或源料平铺接地）的条目一律拒收。
            # R35-B：表格竖线/跨行拼接的结构性误杀由 Tier2 源料平铺救回（见 _quote_is_grounded）。
            rejected.append({"reason": "quote_not_in_source", "text_head": text[:80]})
            continue
        item_id = requirement_id(text)
        if item_id in seen_ids:
            rejected.append({"reason": "duplicate", "text_head": text[:80]})
            continue
        kind = _KIND_ALIASES.get(str(raw.get("kind") or "").strip().casefold(), "other")
        source = raw.get("source")
        if source not in _ALLOWED_SOURCES:
            source = "description"
        seen_ids.add(item_id)
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


def _rejected_summary(rejected: list[dict]) -> str:
    counts: dict[str, int] = {}
    for r in rejected:
        counts[r.get("reason", "?")] = counts.get(r.get("reason", "?"), 0) + 1
    return ",".join(f"{k}x{v}" for k, v in sorted(counts.items()))


def _tech_design_hints(state: BrainState) -> str:
    """tech_design 的任务级 acceptance 列表仅作 LLM 辅助提示（ACCEPTANCE_DESIGN §6.1：
    不可作唯一源——simple/medium 澄清路径不经 tech_design）。★不进 quote 回指语料★：
    它本身是 LLM 产物，允许回指等于给幻觉洗白通道。"""
    td = state.get("tech_design") or {}
    acceptance = td.get("acceptance") if isinstance(td, dict) else None
    if not isinstance(acceptance, list) or not acceptance:
        return "（无）"
    lines = [f"- {str(a)[:200]}" for a in acceptance[:20] if str(a).strip()]
    return "\n".join(lines) or "（无）"


# ══════════════════════════════════════════════
# 节点
# ══════════════════════════════════════════════

async def extract_requirements(state: BrainState) -> dict:
    """EXTRACT_REQUIREMENTS 节点 — 需求文本 → 结构化 requirement_items。

    接线：contract_design → extract_requirements → plan（graph.py）。
    幂等/replan 稳定性（ACCEPTANCE_DESIGN §6.4 取证结论）：replan 环
    handle_failure→plan 与 confirm(REVISE)→plan 都直指 plan、不回到本节点——
    items 一次生成后 last-write-wins 天然稳定，不会每次 replan 重烧 LLM；
    review_design reject→tech_design 重做路径发生在本节点之前（items 尚不存在）。
    requirement_items 已存在仍防御性跳过（checkpoint resume/未来新边的安全网），
    由 test_requirements_extract_s2_2.py 拓扑断言锁定。

    输出：requirement_items（防幻觉校验后的条目）；失败/空源如实降级 []+degraded。
    对称面裁决：不进 runner._NODE_STATUS_MAP——与 clarify/assess/tech_design/
    contract_design/elaborate 等规划子图节点同先例（不写任务状态，仍有 brain_node 事件）。
    """
    if state.get("requirement_items"):
        return {}

    description = (state.get("task_description") or "").strip()
    clarify_summary = (state.get("clarify_summary") or "").strip()

    if not description and not clarify_summary:
        logger.warning("[EXTRACT_REQ] 需求源文本为空，降级 items=[]（不调 LLM）")
        return {
            "requirement_items": [],
            "degraded_reasons": ["requirements_extract:empty_source"],
        }

    # quote 回指语料 = 用户权威需求源（增强后描述+澄清摘要）。tech_design 产物只作提示。
    source_text = description + ("\n" + clarify_summary if clarify_summary else "")
    truncated = source_is_truncated(description)

    items: list[dict] = []
    rejected: list[dict] = []
    best_items: list[dict] = []       # 6.9-HF4：历史最优轮（跨轮单调保优）
    best_rejected: list[dict] = []
    retry_feedback = ""
    llm_error: str | None = None
    got_llm_output = False  # R38-D：是否至少一次拿到可解析输出（区分 infra 死 vs 能力差）

    for attempt in range(1 + MAX_EXTRACT_RETRIES):
        try:
            # lazy import：可 patch 的有状态符号从 nodes 命名空间取（planning_nodes 先例，防环）
            from swarm.brain import nodes as _nodes

            llm = _nodes._get_brain_llm()
            resp = await llm.ainvoke([
                {"role": "system", "content": REQUIREMENTS_EXTRACT_SYSTEM},
                {"role": "user", "content": REQUIREMENTS_EXTRACT_USER.format(
                    description=description or "（无）",
                    clarify=clarify_summary or "（无）",
                    hints=_tech_design_hints(state),
                    retry_feedback=retry_feedback,
                )},
            ])
            raw = _nodes._parse_json_from_llm(resp.content)
        except Exception as exc:  # noqa: BLE001
            llm_error = str(exc)[:120]
            logger.warning("[EXTRACT_REQ] LLM 调用/解析失败（第 %d 次）: %s",
                           attempt + 1, llm_error)
            # R38-C sibling：账本拒绝 → 等在飞结算释放预留再重试（33ms 空转重试等不到
            # 103-408s 的 settle）；hopeless/超时 → 立即放弃（下方 fail-loud 兜）。
            from swarm.brain.planning_nodes import (
                _await_token_admission, _is_token_limit_error)
            if _is_token_limit_error(exc):
                if not await _await_token_admission(
                        state.get("task_id"), getattr(exc, "usage", None) or {},
                        max_wait_s=600.0):
                    break
            retry_feedback = "\n【上一轮输出无法解析为规定 JSON，请严格按 schema 仅输出 JSON】\n"
            continue

        got_llm_output = True  # R38-D：模型可达且输出可解析（后续 0 条属能力面非 infra 面）
        raw_items = raw.get("items") if isinstance(raw, dict) else raw
        items, rejected = validate_requirement_items(raw_items, source_text)
        # 6.9-HF4：跨轮保优——F5 重抽轮可能比上一轮更差（更少条目甚至零合法），旧行为
        # 直接覆盖=好轮次被坏轮次 clobber（首轮 8 条真需求可被末轮空清单整体蒸发，
        # 下游覆盖闸对空 items 整体跳过）。收尾采用最优轮，保证结果对轮次单调不减。
        if len(items) > len(best_items):
            best_items, best_rejected = items, rejected
        if items:
            # F5（阶段6，登记册 §七）：轮级质量闸——旧行为首轮非空即收，抽 3 条也过
            # （PRD 万字только 3 条=明显漏抽，下游覆盖闸对着残缺清单空转）。启发式下限=
            # 每 3000 字符至少 1 条（钳 [1,20]），不足且还有重试额度 → 带反馈重抽。
            _min_expect = min(20, max(1, len(source_text) // 3000))
            if len(items) >= _min_expect or attempt >= MAX_EXTRACT_RETRIES:
                if len(items) < _min_expect:
                    logger.warning(
                        "[EXTRACT_REQ] F5 抽取量偏低（%d 条 < 期望下限 %d，源 %d 字符）"
                        "重试额度已尽，如实收下", len(items), _min_expect, len(source_text))
                break
            logger.warning(
                "[EXTRACT_REQ] F5 轮级质量闸：第 %d 轮仅抽 %d 条（期望≥%d，源 %d 字符）"
                "→ 带反馈重抽", attempt + 1, len(items), _min_expect, len(source_text))
            retry_feedback = (
                f"\n【上一轮仅抽取 {len(items)} 条，明显低于文档规模（{len(source_text)}"
                f" 字符）应有的条目数。请逐段通读全文，完整穷举功能/接口/约束/验收需求，"
                "绝不要只摘开头几条】\n"
            )
            continue
        # schema 过了但零合法条目（全幻觉/全空）→ 有界重试，把确定性拒因回灌给 LLM
        logger.warning("[EXTRACT_REQ] 第 %d 次抽取零合法条目（rejected: %s）",
                       attempt + 1, _rejected_summary(rejected) or "无输出")
        retry_feedback = (
            "\n【上一轮输出全部被确定性校验剔除："
            f"{_rejected_summary(rejected) or '空清单'}。"
            "source_quote 必须从需求文本逐字复制，请重新抽取】\n"
        )

    if len(best_items) > len(items):
        logger.warning(
            "[EXTRACT_REQ] 6.9-HF4 末轮（%d 条）劣于历史最优轮（%d 条）→ 采用最优轮结果",
            len(items), len(best_items))
        items, rejected = best_items, best_rejected

    if truncated:
        for it in items:
            it["source_truncated"] = True

    # R38-D fail-loud：全部尝试从未拿到可解析输出（infra/预算面死亡）且源文本非空 →
    # 绝不打"完成：0 条"继续走。round38 实测：3 连拒后静默清零需求分母，
    # PLAN_COVERAGE_GATE 对空 items 整体跳过=覆盖闸失去牙，会带 0 需求"全覆盖"交付。
    # 模型可达但 0 合法条目（全被防幻觉拒）仍走既有 degraded 降级（能力 artifact，
    # 既有闸门兜，先例#1c 不加修复）。
    if not items and not got_llm_output and llm_error:
        raise RuntimeError(
            f"EXTRACT_REQ 全部 {1 + MAX_EXTRACT_RETRIES} 次 LLM 调用失败（{llm_error}）"
            "——需求分母无从建立，拒绝以空需求清单继续（覆盖闸会失去分母静默放行）")

    out: dict = {"requirement_items": items}
    degraded: list[str] = []
    if truncated:
        # 中段被省略的 PRD 必然漏抽（schema 挡不住），只能可观测化供人工闸提示
        degraded.append("requirements_extract:source_truncated")
    if rejected:
        degraded.append(
            f"requirements_extract:rejected={len(rejected)}({_rejected_summary(rejected)})")
    if not items:
        # fail-closed 终态：绝不塞幻觉条目；下游覆盖校验对空 items=跳过+degraded 不阻塞主链
        reason = f"llm_failed:{llm_error}" if llm_error and not rejected else "all_rejected_or_empty"
        degraded.append(f"requirements_extract:empty({reason})")
    if degraded:
        out["degraded_reasons"] = degraded
    logger.info("[EXTRACT_REQ] 完成：%d 条合法条目，%d 条被拒%s",
                len(items), len(rejected), "，源文本经截断" if truncated else "")
    if rejected:
        # R31-4 T4：被拒明细有界落 INFO——quote_not_in_source 的"正确防幻觉击杀 vs
        # 格式差异误杀"必须可事后审计（round31 实证 17 条被拒但 state 只存汇总计数，
        # 误杀率无从判读）。text_head 上游已截 80 字符，再封条数与总量上限。
        # hunter nit：整串尾截断会切碎 JSON 使审计行不可机器解析——按条回退到预算内
        _detail_rows = rejected[:40]
        _detail = json.dumps(_detail_rows, ensure_ascii=False)
        while len(_detail) > 4000 and _detail_rows:
            _detail_rows = _detail_rows[:-1]
            _detail = json.dumps(_detail_rows, ensure_ascii=False)
        logger.info("[EXTRACT_REQ] 被拒明细(误杀审计，%d/%d 条): %s",
                    len(_detail_rows), len(rejected), _detail)
    return out


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
