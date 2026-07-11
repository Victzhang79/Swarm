"""PlanValidator — 任务计划确定性校验（P0）。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from swarm.types import SubTask, TaskPlan

logger = logging.getLogger(__name__)

# 喂给 VALIDATE_PLAN 软建议 LLM 的 plan_json 字符上限。超过则跳过 LLM 软建议（结构确定性闸门
# 已放行），绝不把超大 prompt 喂推理模型。~120K 字符 ≈ 30K token，足够表达结构/依赖/scope。
MAX_LLM_VALIDATION_PLAN_CHARS = 120_000

# 软校验瘦身时剥离的重体积/冗余子任务字段（不参与 DAG/scope/依赖/完整性判断）。
# contract：每子任务约 42K 字符且各子任务重复携带（round16 实测 24× → plan_json ~1MB）；
# context_snippets：worker 免探索的注入代码，纯执行辅助，与计划结构无关。
_SLIM_STRIP_SUBTASK_FIELDS = ("contract", "context_snippets")


def slim_plan_json_for_llm_validation(plan: TaskPlan) -> str:
    """构造喂给 VALIDATE_PLAN 软建议 LLM 的【瘦身 plan_json】。

    背景（round16 实测）：`plan_obj.model_dump_json()` 把每个子任务约 42K 字符的 `contract`
    副本（24 子任务重复 24×）+ 注入代码全序列化 → plan_json 达 ~1MB（~260K token），喂给
    推理模型 GLM-5.2 触发 84K+ chunk / 25min reasoning runaway（撞 1500s wall-clock 上限才
    放行，且结果是软建议、被丢弃）→ 卡在到 DISPATCH 之前。

    结构校验（validate_plan_structure）已确定性硬保证 DAG/scope/依赖可执行性；LLM 软校验只做
    主观质量信号，无需每个子任务内联的 contract 副本——契约完整性由 plan 级 shared_contract
    一次性体现。这里剥离每子任务的 contract/context_snippets（体积大且冗余），其余字段
    （id/description/scope/depends_on/难度/验收标准/shared_contract）原样保留。
    """
    data = plan.model_dump()
    for st in data.get("subtasks", []) or []:
        if isinstance(st, dict):
            for f in _SLIM_STRIP_SUBTASK_FIELDS:
                st.pop(f, None)
    return json.dumps(data, ensure_ascii=False, indent=2)


def slim_plan_json_or_empty(plan_obj) -> str:
    """None 安全的瘦身 plan 序列化（D50：handle_failure / learn_success / learn_failure 提示词）。

    这三处原用 `plan_obj.model_dump_json(indent=2)` 全量注入 LLM prompt——含每子任务 ~42K
    的 contract 内联副本（validate_plan 早已用 slim 瘦身，此三处漏改，handle_failure 还是
    失败循环高频节点）。统一走 slim；瘦身路径本身异常时 fail-closed 回退旧全量序列化
    （宁可 prompt 大也不丢失败分析输入），再不行才 "{}" 。
    """
    if plan_obj is None or not hasattr(plan_obj, "model_dump"):
        return "{}"
    try:
        return slim_plan_json_for_llm_validation(plan_obj)
    except Exception:
        try:
            return plan_obj.model_dump_json(indent=2)
        except Exception:
            return "{}"

# 单子任务可写文件数：软上限 = 一个垂直功能合理跨越的分层文件数（domain/controller/service/
# impl/mapper+xml 等，RuoYi 这类分层框架一个功能天然 4-6 个文件）。软上限内不告警；
# 软~硬之间仅 warning（不阻断，尊重垂直切片：一个完整功能即使跨多文件也归一个子任务）；
# 超硬上限才判 fail（真正失控的 scope 过度圈定，如把整个模块塞进 writable）。
# task 34fab09e：旧 MAX=3 硬上限把"导出 Excel"功能（4 个分层文件）强制 replan 砍碎丢文件。
SOFT_WRITABLE_FILES_PER_SUBTASK = 6
MAX_WRITABLE_FILES_PER_SUBTASK = 12


@dataclass
class PlanValidationResult:
    valid: bool
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, message: str) -> None:
        self.issues.append(message)
        self.valid = False

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def validate_plan_structure(
    plan: TaskPlan,
    *,
    affected_files: list[str] | None = None,
) -> PlanValidationResult:
    """确定性计划校验：DAG、并行写冲突、文件粒度、组完整性。"""
    result = PlanValidationResult(valid=True)
    if not plan.subtasks:
        result.add("计划无子任务")
        return result

    # H1 治本(round21 假绿门)：整 plan 必须至少有一个子任务能【产出改动】(writable ∪ create_files
    # 非空)。tech_design/plan 返回【空但合法】JSON(file_plan=[])时，所有子任务写 scope 皆空 → 只能
    # 产空 diff → 在"DONE 零放弃"判据下沿 tech_design→plan→validate→confirm→dispatch 直穿判成功
    # 交付(空交付假 DONE)。此处确定性 fail-closed 掐断该跨节点假绿链，根本不放空计划下去。
    # A5（2026-07-09 登记册）：allow_any=True 的子任务【能】写任意路径（_build_simple_plan
    # 开放式需求形态），纯删除子任务（仅 delete_files）产出真实删除 diff——两者都有产出能力，
    # 纳入 writers 判定。原只看 writable/create_files → SIMPLE allow_any 计划被确定性拒绝，
    # 确定性构造重试重建同一计划=三连败任务死。
    _writers = [
        t for t in plan.subtasks
        if (getattr(getattr(t, "scope", None), "writable", None)
            or getattr(getattr(t, "scope", None), "create_files", None)
            or getattr(getattr(t, "scope", None), "delete_files", None)
            or getattr(getattr(t, "scope", None), "allow_any", False))
    ]
    if not _writers:
        result.add(
            "计划无任何可产出改动的子任务(所有子任务 writable+create_files+delete_files 皆空"
            "且无 allow_any)——空计划只能产空 diff、会被误判成功交付(空 diff 假 DONE)，拒绝放行"
        )
        return result

    task_ids = {t.id for t in plan.subtasks}
    subtask_by_id = {t.id: t for t in plan.subtasks}

    # 依赖 ID 存在
    for t in plan.subtasks:
        for dep in t.depends_on:
            if dep not in task_ids:
                result.add(f"子任务 {t.id} 依赖未知任务 {dep}")

    # DAG 无环
    if _has_cycle(plan):
        result.add("执行计划存在循环依赖")

    # parallel_groups 完整性
    grouped: set[str] = set()
    for gi, group in enumerate(plan.parallel_groups):
        for tid in group:
            if tid not in task_ids:
                result.add(f"parallel_groups[{gi}] 含未知子任务 {tid}")
            if tid in grouped:
                result.add(f"子任务 {tid} 出现在多个 parallel_groups")
            grouped.add(tid)

    missing_in_groups = task_ids - grouped
    if plan.parallel_groups and missing_in_groups:
        result.add(f"以下子任务未出现在 parallel_groups: {sorted(missing_in_groups)}")

    # 单任务文件数：软上限内放行；软~硬之间仅告警（尊重垂直切片，一个完整功能可跨多文件）；
    # 超硬上限才判失败（scope 失控，如整个模块塞进 writable）。
    for t in plan.subtasks:
        n = len(t.scope.writable or [])
        if n > MAX_WRITABLE_FILES_PER_SUBTASK:
            result.add(
                f"子任务 {t.id} 涉及 {n} 个可写文件，超过硬上限 {MAX_WRITABLE_FILES_PER_SUBTASK}"
                f"（scope 可能失控，需拆分或收窄）"
            )
        elif n > SOFT_WRITABLE_FILES_PER_SUBTASK:
            result.warn(
                f"子任务 {t.id} 涉及 {n} 个可写文件（超软上限 {SOFT_WRITABLE_FILES_PER_SUBTASK}）。"
                f"若为单一垂直功能跨分层文件属正常；若含多个独立功能建议拆分"
            )

    # 并行组内 writable 不得冲突（同组即并行）
    for gi, group in enumerate(plan.parallel_groups):
        seen: dict[str, str] = {}
        for tid in group:
            t = subtask_by_id.get(tid)
            if not t:
                continue
            for fp in t.scope.writable or []:
                if fp in seen and seen[fp] != tid:
                    result.add(
                        f"parallel_groups[{gi}] 中 {seen[fp]} 与 {tid} 同时写 {fp}（并行冲突）"
                    )
                else:
                    seen[fp] = tid

    # 跨子任务写冲突。writable + create_files 都算"写"（B3：每个文件只应属一个子任务）。
    # - 无依赖关系还写同文件 → 硬失败（并行必冲突）。
    # - 有依赖关系写同文件 → 告警（B3 依赖序拆分要求文件不重叠；即使串行，两子任务各自
    #   在独立沙箱改同文件，MERGE 时仍会冲突。降级 warn 不阻断，尊重少数合理场景如
    #   前序 create + 后序 modify，但提示风险）。
    writable_map: dict[str, list[str]] = {}
    for t in plan.subtasks:
        # 同一子任务内 writable ∪ create_files 去重：否则文件【既在 writable 又在 create_files】
        # 时同一子任务 id 被记两次 → 下方两两比较会出现 "st-N 与 st-N 同时写"（自己跟自己冲突）。
        for fp in (set(t.scope.writable or []) | set(getattr(t.scope, "create_files", []) or [])):
            writable_map.setdefault(fp, []).append(t.id)
    for fp, ids in writable_map.items():
        ids = list(dict.fromkeys(ids))  # 跨子任务再去重（保序）
        if len(ids) < 2:
            continue
        joined = ", ".join(ids)
        # D1 治本 backstop：Maven 根聚合 pom.xml 永远【单写者】。两份对 <modules>/
        # <dependencyManagement> 的结构重写无法安全 3-way/union 合并（round18 P0-A 畸形 /
        # rebase 循环→escalate→FAILED），即便依赖序也不行（各自整段重写、非加性）。
        # normalize_plan_scopes 已收敛唯一 owner；此处硬失败仅在收敛失效时兜底（fail-closed）。
        if fp.replace("\\", "/") == "pom.xml":
            result.add(
                f"根聚合 pom.xml 有 {len(ids)} 个写者: [{joined}]"
                f"（必须收敛唯一 aggregator-owner；双写者=P0-A 畸形/rebase 循环根因）"
            )
            continue
        # 按文件【聚合】成一条，而非两两组合 O(n²) 刷屏（N 个子任务写同一文件原会打 N²/2 条）。
        # 只要存在一对【无依赖】争写者即硬失败（并行必冲突）；否则全为依赖序 → 告警。
        has_independent = any(
            not _depends(a, b, subtask_by_id[a], subtask_by_id[b], subtask_by_id)
            for i, a in enumerate(ids)
            for b in ids[i + 1 :]
        )
        if has_independent:
            result.add(
                f"{len(ids)} 个无依赖子任务同时写 {fp}: [{joined}]"
                f"（并行必冲突，每个文件应只归一个子任务）"
            )
        else:
            result.warn(
                f"{len(ids)} 个依赖序子任务都写 {fp}: [{joined}]"
                f"（已串行化；聚合/注册类共享文件由 bootstrap 传播 + MERGE 3-way/rebase 收口，"
                f"非聚合文件建议仍只归一个子任务）"
            )

    # 检索定位文件覆盖（可选）
    if affected_files:
        scoped: set[str] = set()
        for t in plan.subtasks:
            scoped.update(t.scope.writable or [])
            scoped.update(t.scope.readable or [])
        missing = [f for f in affected_files if f and f not in scoped]
        if missing and len(missing) <= 10:
            result.warn(
                f"检索定位的文件可能未被任何子任务 scope 覆盖: {missing[:5]}"
                + (" ..." if len(missing) > 5 else "")
            )

    # 共享契约（跨子任务）
    if len(plan.subtasks) > 1 and not plan.shared_contract:
        result.warn("多子任务计划缺少 plan.shared_contract，跨文件协调风险较高")

    if plan.shared_contract:
        sc_keys = set(plan.shared_contract.keys())
        for t in plan.subtasks:
            if t.contract:
                overlap = sc_keys & set(t.contract.keys())
                if not overlap and len(plan.subtasks) > 1:
                    result.warn(f"子任务 {t.id} contract 未引用 shared_contract 字段")

    return result


# ── S2-3 PRD 覆盖矩阵（task#24，ACCEPTANCE_DESIGN 定案3/§2.5）──────────────
# 通用多栈多领域：只对账 req_id 与 covers 的映射结构，不含任何语言/框架/领域词汇。

def normalize_baseline_covered(raw) -> list[dict]:
    """R31-1 T1：baseline_covered 申报的确定性清洗。纯函数零 LLM。

    输入是 PLAN LLM 顶层输出（或 state 键回读），形态不可信：
    dict{id,reason} / 裸字符串（补空 reason，交由覆盖校验拒"缺理由"）/ 垃圾类型（丢弃）。
    按 id 去重【保优】（带 reason 的胜过空 reason——复核 L-3：保首会让先到的空申报
    多烧一轮"缺理由"重试）；reason 有界 300 字符、总条数有界=抽取硬顶（A8 同源，超帽
    WARNING 留痕——LLM 失控吐数百条假 ID 时 state 键/feedback/prompt 三处联动膨胀仍有界）。
    非 list 整体 → []。
    """
    if not isinstance(raw, list):
        return []
    # A8（2026-07-09 登记册）：申报帽与抽取条目硬顶【同源】（requirements_extract 单一事实源）。
    # 原硬帽 100 vs 抽取硬顶 500：棕地底座 >100 条时诚实申报第 101 条起被静默砍→回到
    # uncovered→覆盖闸死（申报越诚实越完整越死）。同源后申报数结构上不可能超帽（申报⊆抽取
    # 条目）；仍超帽=LLM 失控吐假 ID，有界截断 + WARNING 留痕不再静默。
    from swarm.brain.requirements_extract import _HARD_MAX_ITEMS
    out: list[dict] = []
    index: dict[str, int] = {}
    _dropped = 0
    for entry in raw:
        if isinstance(entry, dict):
            rid = str(entry.get("id") or "").strip()
            reason = str(entry.get("reason") or "").strip()[:300]
        elif isinstance(entry, str):
            rid, reason = entry.strip(), ""
        else:
            continue
        if not rid:
            continue
        if rid in index:
            if reason and not out[index[rid]]["reason"]:
                out[index[rid]]["reason"] = reason
            continue
        if len(out) >= _HARD_MAX_ITEMS:
            _dropped += 1
            continue
        index[rid] = len(out)
        out.append({"id": rid, "reason": reason})
    if _dropped:
        logger.warning(
            "[PLAN-VALIDATE] baseline_covered 申报超同源硬顶 %d，丢弃 %d 条"
            "（申报⊆抽取条目结构上不可能超帽——此形态=LLM 失控吐假 ID，留痕不静默）",
            _HARD_MAX_ITEMS, _dropped,
        )
    return out


# R31-2 T2 近邻判据：与真实 ID 共享的最短前缀长度。ID 形态 req-<sha1[:8]>，
# 共享"req-"+6 位 hash 前缀的随机碰撞率 ≈16^-6/对（可忽略）——比相似度阈值可靠：
# hunter F4 仿真坐实 difflib cutoff 0.75 在 60 条随机 ID 下有 ~9.5% 概率把纯臆造 ID
# 指向无关真实条目（SequenceMatcher 非连续块匹配远比直觉宽松），而 round31 实证对
# （req-72fd9811 vs req-72fd98fb）ratio 仅 0.833，提高 cutoff 到 0.85 又会漏掉动机
# 案例。前缀规则两者兼得；代价=前段 typo 无提示（fail-closed：宁缺勿误导）。
_NEAR_MISS_PREFIX = 10  # len("req-") + 6


def _near_miss_hint(bad_id: str, known_ids) -> str:
    """R31-2 T2：臆造 ID 的确定性近邻提示（绝不自动改写，只在 issue 文案点名候选）。

    round31 实证：LLM 写 req-72fd9811（真实为 req-72fd98fb），D09 纯文字"不得编造"
    四轮不自愈。判据=唯一共享 ≥10 字符前缀的真实 ID；候选不唯一/不存在 → 不提示
    （误导比不提示更糟，hunter F4）。
    """
    if len(bad_id) < _NEAR_MISS_PREFIX:
        return ""
    prefix = bad_id[:_NEAR_MISS_PREFIX]
    matches = [k for k in known_ids if k[:_NEAR_MISS_PREFIX] == prefix and k != bad_id]
    return f"（可能想引用 {matches[0]}？）" if len(matches) == 1 else ""


def build_coverage_matrix(plan, requirement_items, baseline_covered=None) -> dict:
    """覆盖矩阵 = 从 plan.subtasks[].covers 现算的【派生数据】。

    定案（ACCEPTANCE_DESIGN 新键清单）：矩阵绝不进 state——它完全由 plan 与
    requirement_items 派生，落两份事实必漂移。纯函数零 LLM，供 validate_requirement_coverage
    与交付报告展示（task#26/#27）复用同一口径。

    返回：
      {
        "total_items": int,                     # 合法需求条目数
        "covered_items": int,                   # 至少被一个子任务 covers 的条目数
        "items": [{"id","text","kind","covered_by":[subtask_id,...]}],
        "uncovered": [{"id","text"}],           # 未被任何子任务覆盖的条目
        "dangling_covers": {subtask_id: [req_id,...]},  # 悬空引用（指向不存在的条目 ID）
        "baseline_covered": [{"id","reason"}],  # R31-1 T1：合法的"存量已满足"申报（reason 非空）
        "dangling_baseline": [req_id,...],      # 申报了清单外 ID（臆造，校验拒绝）
      }

    R31-1 T1 覆盖口径：条目"被覆盖" = 被子任务 covers 引用 ∪ 被合法 baseline 申报
    （id 在清单内且 reason 非空——fail-closed，无依据申报不算覆盖）。
    baseline_covered=None/缺省 → 全部既有调用点行为逐字节不变（新键恒空）。

    容错口径：requirement_items 缺失/空 → 空矩阵（total_items=0，调用方按
    "跳过校验+degraded"处理）；非 dict/无 id 条目跳过（上游 requirements_extract
    已确定性清洗，此处纯防御）。
    """
    valid_items = [
        it for it in (requirement_items or [])
        if isinstance(it, dict) and str(it.get("id") or "").strip()
    ]
    covered_by: dict[str, list[str]] = {str(it["id"]).strip(): [] for it in valid_items}
    dangling: dict[str, list[str]] = {}
    for st in (getattr(plan, "subtasks", None) or []):
        st_id = str(getattr(st, "id", "") or "?")
        for rid in (getattr(st, "covers", None) or []):
            rid = str(rid).strip()
            if not rid:
                continue
            if rid in covered_by:
                if st_id not in covered_by[rid]:
                    covered_by[rid].append(st_id)
            else:
                bucket = dangling.setdefault(st_id, [])
                if rid not in bucket:
                    bucket.append(rid)
    # R31-1 T1：baseline 申报分拣——清单内且带理由=合法覆盖；清单外=dangling（臆造）。
    # 空 reason 的申报既不算覆盖也不进 baseline 桶（validate 层对它出专项 issue）。
    baseline_norm = normalize_baseline_covered(baseline_covered)
    baseline_valid: list[dict] = []
    dangling_baseline: list[str] = []
    baseline_ids: set[str] = set()
    for entry in baseline_norm:
        if entry["id"] not in covered_by:
            dangling_baseline.append(entry["id"])
        elif entry["reason"]:
            baseline_valid.append(entry)
            baseline_ids.add(entry["id"])
    items_out: list[dict] = []
    uncovered: list[dict] = []
    for it in valid_items:
        rid = str(it["id"]).strip()
        text = str(it.get("text") or "")
        items_out.append({
            "id": rid,
            "text": text,
            "kind": str(it.get("kind") or "other"),
            "covered_by": covered_by[rid],
        })
        if not covered_by[rid] and rid not in baseline_ids:
            uncovered.append({"id": rid, "text": text})
    return {
        "total_items": len(items_out),
        "covered_items": len(items_out) - len(uncovered),
        "items": items_out,
        "uncovered": uncovered,
        "dangling_covers": dangling,
        "baseline_covered": baseline_valid,
        "dangling_baseline": dangling_baseline,
    }


def covered_req_ids(matrix: dict) -> list[str]:
    """阶段3.1 单调合同：从覆盖矩阵取【本轮已覆盖】req id 集（covers∪合法 baseline），
    排序去重——coverage_watermark 的唯一口径（与 build_coverage_matrix 同源，防两份事实）。"""
    ids = {it["id"] for it in (matrix.get("items") or []) if it.get("covered_by")}
    ids.update(e["id"] for e in (matrix.get("baseline_covered") or []))
    return sorted(ids)


def unowned_contract_symbols(plan, symbols: list[str]) -> list[str]:
    """C1 owner 匹配判定（R39-2 抽出为单一口径：C1 闸与 symbol_surgery 共用）。

    owner 判据（零 LLM）：某子任务的 description/acceptance_criteria/contract
    词边界命中（与 verify._d5_attribute_owners 同口径），或 create_files/writable
    文件名命中（<Symbol>.<ext>）。返回无 owner 符号列表（保输入序）。"""
    import json as _json
    import re as _re

    subtasks = list(getattr(plan, "subtasks", None) or [])
    if not symbols or not subtasks:
        return []
    corpus: dict[str, str] = {}
    files_by_st: dict[str, list[str]] = {}
    for st in subtasks:
        sc = getattr(st, "scope", None)
        corpus[st.id] = (
            (getattr(st, "description", "") or "") + " "
            + " ".join(getattr(st, "acceptance_criteria", None) or [])
            + " " + _json.dumps(getattr(st, "contract", None) or {}, ensure_ascii=False)
        ).lower()
        files_by_st[st.id] = [
            str(f).replace("\\", "/").rsplit("/", 1)[-1].lower()
            for f in (list(getattr(sc, "create_files", None) or [])
                      + list(getattr(sc, "writable", None) or []))]
    unowned: list[str] = []
    for sym in symbols:
        s = str(sym).lower()
        pat = _re.compile(r"(?<![0-9a-z_])" + _re.escape(s) + r"(?![0-9a-z_])")
        hit = any(pat.search(txt) for txt in corpus.values()) or any(
            b.startswith(s + ".") for bl in files_by_st.values() for b in bl)
        if not hit:
            unowned.append(str(sym))
    return unowned


def validate_contract_ownership(
    plan, shared_contract, project_path: str | None = None,
) -> PlanValidationResult:
    """C1（round38c 主题C）：契约符号→owner 确定性对账——D5 从 VERIFY_L2 前移到 PLAN 期。

    round38c 死因链头：24 个契约接口从未进任何子任务语料，规划期两张皮，8 小时后
    L2 才第一次对账爆缺失 16/24 → 全员清零（A1 已封死清零，本函数掐断"两张皮"本身）。
    规则（零 LLM）：owner 判据见 unowned_contract_symbols（单一口径）。
    无主符号占比 > SWARM_CONTRACT_UNOWNED_RATIO（默认 0.4，与 L2 缺失阈值同源）→
    invalid 走 D09 回灌打回（feedback 教 PLAN 怎么修）；否则逐条 warn（可观测不烧重试）。
    规则5 落空依赖（unclaimed_contract_deps）并入 warnings（旧纯 log 无人消费）。
    R39-2 存量豁免：project_path 非空时，基线树已有 `<Symbol>.<ext>` 同名文件的
    符号视为存量承接不算 unowned（round39：棕地存量符号被判无主是误伤面）。"""
    import os as _os

    from swarm.brain.contract_utils import (
        baseline_symbol_files,
        contract_symbols_with_module,
        unclaimed_contract_deps,
    )
    result = PlanValidationResult(valid=True)
    entries = contract_symbols_with_module(shared_contract or {})
    symbols = [e["symbol"] for e in entries]
    subtasks = list(getattr(plan, "subtasks", None) or [])
    if symbols and subtasks:
        unowned = unowned_contract_symbols(plan, symbols)
        if unowned and project_path:
            _base_hits = baseline_symbol_files(unowned, project_path)
            if _base_hits:
                logger.info(
                    "[C1] 存量豁免 %d 符号（基线树同名文件承接）: %s",
                    len(_base_hits), sorted(_base_hits)[:8])
                # hunter⑤：豁免决策进 state 级 warnings（唯一审计痕不能只活在
                # 日志里——运维把日志级别调到 WARNING 就蒸发）
                for _sym in sorted(_base_hits)[:20]:
                    result.warn(f"契约符号 {_sym} 由基线存量文件承接（存量豁免，"
                                "不要求新 owner）")
                unowned = [s for s in unowned if s not in _base_hits]
        if unowned:
            # R39-3 硬/软分级：C1 原始意图是"接口两张皮"（round38c 24 接口从未进
            # 计划语料）；DTO/字段/方法名随其宿主文件落地，不必逐名出现在子任务语料
            # ——round39 胖契约 67 DTO 全算硬性把 40% 阈值顶爆=FAILED@PLAN 直接死因。
            # 成员符号（X.Y 且 X 已在符号集，如 AlarmSimpleUtil.Builder）是边界重叠
            # 自并膨胀产物，同样降软。软性 unowned 保留 warn 可观测，L2 全量消费不变。
            _HARD_KINDS = {"interfaces", "types", "apis", "symbols"}
            _kind_of = {e["symbol"]: e.get("kind", "") for e in entries}
            _sym_set = set(symbols)

            def _is_soft(s: str) -> bool:
                if _kind_of.get(s) not in _HARD_KINDS:
                    return True
                return "." in s and s.split(".", 1)[0] in _sym_set

            hard_unowned = [s for s in unowned if not _is_soft(s)]
            soft_unowned = [s for s in unowned if _is_soft(s)]
            hard_total = sum(1 for s in symbols if not _is_soft(s))
            try:
                ratio_cap = float(_os.environ.get("SWARM_CONTRACT_UNOWNED_RATIO", "0.4"))
            except (TypeError, ValueError):
                ratio_cap = 0.4
            ratio = len(hard_unowned) / max(hard_total, 1)
            if hard_unowned and ratio > ratio_cap:
                result.add(
                    f"契约符号无 owner 子任务承接 {len(hard_unowned)}/{hard_total}"
                    f"（占比 {ratio:.0%} 超阈值 {ratio_cap:.0%}）: {', '.join(hard_unowned[:12])}"
                    "——每个契约符号必须由某个子任务负责产出：在该子任务的 description/"
                    "contract 中点名符号，或在其 create_files 安排 <符号名>.<扩展名> 文件")
            else:
                for sym in hard_unowned[:20]:
                    result.warn(f"契约符号 {sym} 无 owner 子任务（L2 将按 D5 归因定向重派，"
                                "建议规划期即安排 owner）")
            for sym in soft_unowned[:20]:
                result.warn(f"契约软性符号 {sym} 无 owner（dtos/fields/methods/成员符号"
                            "随宿主文件落地，不计入打回比率；L2 D5 仍全量核验）")
    for entry in unclaimed_contract_deps(plan):
        result.warn(
            f"规则5：模块 {entry['module']} 的 {len(entry['artifacts'])} 个依赖契约"
            "无 pom owner 承接（编译期可能缺依赖）")
    return result


def validate_requirement_coverage(
    plan, requirement_items, baseline_covered=None,
) -> PlanValidationResult:
    """S2-3 确定性覆盖校验维度（validate_plan 内、结构校验+SIMPLE 早退之后调用）。

    规则（全确定性，零 LLM）：
      ① 每个 requirement item 至少被一个子任务的 covers 引用（未覆盖 → issue）；
      ② covers 无悬空引用（指向不存在的 req_id → issue）。
    requirement_items 缺失/空的"跳过+degraded"是 state 侧决策，由调用方（validate_plan）
    负责——本函数对空 items 如实返回 valid（无可对账项）。

    诚实边界（定案3）：确定性面只能校验"映射结构合法"，挡不住 LLM 谎称 cover——
    语义真覆盖由 ACCEPT 运行时断言兜底（task#25+），两层合成闭环。
    失败 issues 逐条带条目 id+text：D09 回灌反馈的具体性决定 PLAN LLM 能否修对。
    """
    result = PlanValidationResult(valid=True)
    matrix = build_coverage_matrix(plan, requirement_items, baseline_covered)
    known_ids = [row["id"] for row in matrix["items"]]
    for item in matrix["uncovered"]:
        # R31-1：文案必须教申报通道——round31 实证 PLAN 拒绝为存量已有能力造子任务
        # （工程判断合理），不给出口=确定性死锁烧光重试。
        result.add(
            f"需求条目未被任何子任务覆盖: {item['id']} — {item['text'][:120]}"
            f"（若需新实现：分配给某个子任务并在其 covers 声明此 ID；"
            f"若现有代码已完整满足该需求、本任务无需改动：在计划 JSON 顶层的 "
            f"baseline_covered 列表申报此 ID 并给出 reason 依据）"
        )
    for st_id in sorted(matrix["dangling_covers"]):
        bad = matrix["dangling_covers"][st_id]
        # 复核 L-2：提示逐 ID 归属（多坏 ID 时串尾拼接会让 LLM 猜配对）
        listed = ", ".join(f"{b}{_near_miss_hint(b, known_ids)}" for b in bad)
        result.add(
            f"子任务 {st_id} 的 covers 引用不存在的需求条目 ID: {listed}"
            f"（covers 只能引用需求条目清单中给出的 ID，不得编造）"
        )
    for bad in matrix["dangling_baseline"]:
        result.add(
            f"baseline_covered 申报了不存在的需求条目 ID: {bad}{_near_miss_hint(bad, known_ids)}"
            f"（只能申报需求条目清单中给出的 ID，不得编造）"
        )
    # 缺理由申报：normalize 保留了它但矩阵不算覆盖——这里出专项 issue（D09 可修性：
    # 点名到条目，LLM 补 reason 即过，绝不让"未覆盖"泛化文案掩盖真实动作项。
    _declared_valid = {e["id"] for e in matrix["baseline_covered"]}
    for entry in normalize_baseline_covered(baseline_covered):
        if entry["id"] in _declared_valid or entry["id"] in matrix["dangling_baseline"]:
            continue
        result.add(
            f"baseline_covered 申报 {entry['id']} 缺少 reason 理由"
            f"（申报存量已满足必须给出依据：现有代码何处/如何满足该需求）"
        )
    return result


def _depends(
    id_a: str,
    id_b: str,
    ta: SubTask,
    tb: SubTask,
    subtask_by_id: dict[str, SubTask],
) -> bool:
    deps_a = _all_deps(ta, subtask_by_id)
    deps_b = _all_deps(tb, subtask_by_id)
    return id_b in deps_a or id_a in deps_b


def _all_deps(task: SubTask, subtask_by_id: dict[str, SubTask]) -> set[str]:
    deps: set[str] = set()
    stack = list(task.depends_on)
    while stack:
        dep_id = stack.pop()
        if dep_id in deps:
            continue
        deps.add(dep_id)
        dep_task = subtask_by_id.get(dep_id)
        if dep_task:
            stack.extend(dep_task.depends_on)
    return deps


def _has_cycle(plan: TaskPlan) -> bool:
    subtask_by_id = {t.id: t for t in plan.subtasks}
    visited: set[str] = set()
    stack: set[str] = set()

    def dfs(tid: str) -> bool:
        if tid in stack:
            return True
        if tid in visited:
            return False
        visited.add(tid)
        stack.add(tid)
        t = subtask_by_id.get(tid)
        if t:
            for dep in t.depends_on:
                if dfs(dep):
                    return True
        stack.remove(tid)
        return False

    return any(dfs(t.id) for t in plan.subtasks)
