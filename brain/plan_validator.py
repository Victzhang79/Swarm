"""PlanValidator — 任务计划确定性校验（P0）。"""

from __future__ import annotations

import json
import logging
import re
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

# #39-A：各栈【根聚合清单】——单写者硬失败 backstop 覆盖面（路径无前缀=仓库根级）。聚合结构
# 重写非加性（Maven <modules>、Gradle include、Go go.work use、Cargo [workspace] members），
# 双写者=rebase 循环根因，栈中立统一硬失败。子目录同名清单（如 member Cargo.toml）不在此列。
_ROOT_AGGREGATOR_MANIFESTS = frozenset({
    "pom.xml", "settings.gradle", "settings.gradle.kts", "go.work", "Cargo.toml",
})


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

    # DR-01-F2(#46) 治本：写冲突/并行冲突/根聚合清单三处闸此前用 scope 原始串作键（仅
    # replace("\\","/")，未剥 './'/前导 '/'）→ 同一文件的路径形态变体（'./pom.xml' vs
    # 'pom.xml'、'src//A.java'）逃过所有冲突判定，且 './pom.xml' 不在 _ROOT_AGGREGATOR_MANIFESTS
    # 裸成员集 → 根聚合双写者 backstop 被一个 './' 绕过。统一走 _norm_scope_path 归一（与
    # normalize_plan_scopes 同一把尺子）。lazy import 沿用本模块既有反循环导入约定。
    from swarm.brain.contract_utils import _norm_scope_path

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
                nfp = _norm_scope_path(fp)  # DR-01-F2(#46)：归一键，防 './' 前缀绕过
                if nfp in seen and seen[nfp] != tid:
                    result.add(
                        f"parallel_groups[{gi}] 中 {seen[nfp]} 与 {tid} 同时写 {fp}（并行冲突）"
                    )
                else:
                    seen[nfp] = tid

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
            writable_map.setdefault(_norm_scope_path(fp), []).append(t.id)  # DR-01-F2(#46) 归一键
    for fp, ids in writable_map.items():
        ids = list(dict.fromkeys(ids))  # 跨子任务再去重（保序）
        if len(ids) < 2:
            continue
        joined = ", ".join(ids)
        # D1 治本 backstop：根聚合清单永远【单写者】。两份对聚合结构（Maven <modules>/
        # <dependencyManagement>、Gradle settings.gradle include(...)、Go go.work use、Cargo
        # [workspace] members）的重写都无法安全 3-way/union 合并（round18 P0-A 畸形 / rebase
        # 循环→escalate→FAILED），即便依赖序也不行（各自整段重写、非加性）。
        # ★#39-A 治本★ 此前只硬失败 pom.xml → 非 Maven 根聚合（settings.gradle/go.work/根
        # Cargo.toml）的依赖序双写者只落下方 warn，逃过 backstop → Gradle/Go 重现 pom 早年那条
        # 非加性 rebase 循环。栈中立铺开：根级聚合清单集统一硬失败（路径无前缀=在仓库根）。
        # normalize_plan_scopes 已收敛唯一 owner；此处硬失败仅在收敛失效时兜底（fail-closed）。
        if _norm_scope_path(fp) in _ROOT_AGGREGATOR_MANIFESTS:  # DR-01-F2(#46)：fp 已归一，剥 './'
            result.add(
                f"根聚合清单 {fp} 有 {len(ids)} 个写者: [{joined}]"
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


def build_coverage_matrix(plan, requirement_items, baseline_covered=None,
                          baseline_vocab=None, baseline_ineligible=None) -> dict:
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
    # R65E7-L1（P1 盲区兜底）：传入 baseline_vocab 时，把【无基线证据】的假 baseline 申报（T1 口径
    # 同源 baseline_claims_missing_evidence）踢出覆盖——否则 P1 外科闸的 build_coverage_matrix 见假
    # baseline 在 baseline_ids→算已覆盖→uncovered=0→「覆盖已满足」return None→回退全量重拆丢覆盖回归
    # （round65e7 FAILED@PLAN 振荡真因）。不传 vocab→逐字节向后兼容（既有调用点行为不变）。
    # fail-open：vocab 空→baseline_claims_missing_evidence 返 []→不踢（缺索引不误伤合法存量申报）。
    _evidenceless: set[str] = set()
    if baseline_vocab:
        from swarm.brain.baseline_candidates import baseline_claims_missing_evidence
        _evidenceless = set(baseline_claims_missing_evidence(
            baseline_norm, valid_items, baseline_vocab))
    # R65E9-T1：曾被证据闸判假的 baseline id 单调累积集——【无条件】踢出合法 baseline（不看 vocab/
    # reason/evidence），逼其落 uncovered→建 covers 子任务。治 round65e9 死钉：被拒 baseline req 陷
    # limbo（非 covered 非 unplanned）→planner 每 retry 重 declare 同一 req→3-retry 耗尽 FAILED@PLAN。
    # 缺省 None→逐字节向后兼容（既有调用点行为不变）。
    _ineligible: set[str] = {str(x).strip() for x in (baseline_ineligible or []) if str(x).strip()}
    baseline_valid: list[dict] = []
    dangling_baseline: list[str] = []
    baseline_ids: set[str] = set()
    for entry in baseline_norm:
        if entry["id"] not in covered_by:
            dangling_baseline.append(entry["id"])
        elif (entry["reason"] and entry["id"] not in _evidenceless
              and entry["id"] not in _ineligible):
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


def basename_owns_symbol(stem: str, sym: str,
                         decorated_prefix: bool = True) -> bool:
    """R42：文件名主干是否按【确定性命名惯例等价】承接契约符号（零 LLM 单一口径）。

    round42 死因实锤：契约符号 AlarmTaskService ↔ 计划文件 IAlarmTaskService.java +
    AlarmTaskServiceImpl.java（RuoYi I 前缀接口/Impl 实现惯例）、NotifyUserService ↔
    IAlarmNotifyUserService.java（再加项目前缀装饰）——plan 按栈惯例命名没错，字面
    basename 对账把 21/26 判无主，三轮教育 LLM 也"修不对"（错的是口径不是 plan）。
    等价规则：
      ① 精确：stem == Symbol
      ② 接口 I 前缀：stem == "I"+Symbol
      ③ 实现 Impl 后缀：剥尾部 Impl 后回到 ①/②
      ④ 装饰前缀（项目/模块名，如 Alarm+NotifyUserService）：Symbol ≥8 字符时
         【大小写敏感】CamelCase 词边界后缀匹配（半词 Taskservice≠TaskService）。
    ④ 是"宁误勿漏"通道：同一 stem 可同时命中长短两个契约符号（AlarmTaskServiceImpl
    命中 AlarmTaskService 也命中 TaskService）——真缺的短符号会被吞掉，且 L2 契约
    核验是子串匹配（taskservice ⊂ alarmtaskservice）**兜不住**这类遮蔽（复核 F2
    CONFIRMED）。调用方消费文件通道时必须做【最长符号优先消歧】（见
    unowned_contract_symbols）；无全量符号清单的场景（棕地基线豁免）传
    decorated_prefix=False 关掉 ④（复核 F3：5k 文件树上 ISysUserService 会豁免
    一切 *UserService 新符号，豁免半径失控）。"""
    return basename_symbol_match(stem, sym, decorated_prefix=decorated_prefix) >= 0


def basename_symbol_match(stem: str, sym: str,
                          decorated_prefix: bool = True) -> int:
    """R43 复核 F1：带【匹配强度】的等价判定——消歧必须先比强度再比长度。

    tier 0=精确同名（Impl 剥离后 stem==Symbol）
    tier 1=惯例等价精确（文件带 I / 符号带 I，剥后同名）
    tier 2=装饰前缀后缀匹配（宁误勿漏通道，decorated_prefix=False 时关闭）
    -1=不匹配。
    复核 F1（CONFIRMED 回归）：契约同时含 IChannelAdapter+ChannelAdapter 双胞胎
    （P6 边界重叠自并的典型产物）且两文件都在计划时，纯 max(len) 消歧让弱通道的
    长符号抢走精确同名文件 → 短胞胎被判无主且 LLM 无法修（文件明明在）——精确
    匹配必须永远赢过等价通道。
    已知有界误配（复核 F3）：^I[A-Z] 无法区分接口前缀与 I 开头缩写
    （IOException 的 base=OException 可后缀误配 DAOException）；≥8 字符+大写
    边界+强度分层兜住半径，且契约极少收录 JDK 异常类，留观不加特判。"""
    s = str(stem or "")
    y = str(sym or "")
    if not s or not y:
        return -1
    if s.lower().endswith("impl") and len(s) > 4:
        s = s[:-4]
    sl, yl = s.lower(), y.lower()
    if sl == yl:
        return 0
    if sl == "i" + yl:
        return 1
    y_base = y[1:] if (len(y) >= 3 and y[0] == "I" and y[1].isupper()) else None
    if y_base is not None and sl == y_base.lower():
        return 1
    if decorated_prefix:
        if len(y) >= 8 and len(s) > len(y) and s.endswith(y) \
                and s[len(s) - len(y)].isupper():
            return 2
        if y_base is not None and len(y_base) >= 8 and len(s) > len(y_base) \
                and s.endswith(y_base) and s[len(s) - len(y_base)].isupper():
            return 2
    return -1


def unowned_contract_symbols(plan, symbols: list[str]) -> list[str]:
    """C1 owner 匹配判定（R39-2 抽出为单一口径：C1 闸与 symbol_surgery 共用）。

    owner 判据（零 LLM）：某子任务的 description/acceptance_criteria/contract
    词边界命中（与 verify._d5_attribute_owners 同口径），或 create_files/writable
    文件名按命名惯例等价命中（basename_owns_symbol，R42：字面 <Symbol>.<ext>
    对 RuoYi I 前缀/Impl 惯例结构性落空是 round42 FAILED@PLAN 直接死因）。
    返回无 owner 符号列表（保输入序）。"""
    import json as _json
    import re as _re

    subtasks = list(getattr(plan, "subtasks", None) or [])
    if not symbols or not subtasks:
        return []
    corpus: dict[str, str] = {}
    stems_by_st: dict[str, list[str]] = {}
    for st in subtasks:
        sc = getattr(st, "scope", None)
        corpus[st.id] = (
            (getattr(st, "description", "") or "") + " "
            + " ".join(getattr(st, "acceptance_criteria", None) or [])
            + " " + _json.dumps(getattr(st, "contract", None) or {}, ensure_ascii=False)
        ).lower()
        # 保留原大小写主干：惯例等价的 CamelCase 边界判定需要大小写信息
        stems_by_st[st.id] = [
            str(f).replace("\\", "/").rsplit("/", 1)[-1].split(".", 1)[0]
            for f in (list(getattr(sc, "create_files", None) or [])
                      + list(getattr(sc, "writable", None) or []))]
    # 文件通道（R42 复核 F2：最长符号优先消歧）——同一 stem 命中多个契约符号时只归
    # 最长者：AlarmTaskServiceImpl 同时命中 AlarmTaskService（真 owner）与 TaskService
    # （装饰前缀误配），若都算 owned 则真缺的 TaskService 被吞掉且 L2 子串核验兜不住。
    _syms = [str(x) for x in symbols]
    file_owned: set[str] = set()
    for bl in stems_by_st.values():
        for b in bl:
            # 复核 F1：先比匹配强度（精确>等价>装饰），同强度才取最长符号——
            # 精确同名文件绝不被弱通道的长符号抢走
            best, best_key = None, None
            for y in _syms:
                t = basename_symbol_match(b, y)
                if t < 0:
                    continue
                key = (t, -len(y))
                if best_key is None or key < best_key:
                    best, best_key = y, key
            if best is not None:
                file_owned.add(best)
    unowned: list[str] = []
    for sym in symbols:
        s = str(sym).lower()
        pat = _re.compile(r"(?<![0-9a-z_])" + _re.escape(s) + r"(?![0-9a-z_])")
        hit = str(sym) in file_owned or any(
            pat.search(txt) for txt in corpus.values())
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
                if "." in s and s.split(".", 1)[0] in _sym_set:
                    return True
                # R43：成员形态降软——小写开头标识符（getByAppId/validateApp…）是
                # 方法/字段命名惯例，从不对应独立文件，file-owner 硬对账对其结构性
                # 无解（round43 实测：GLM 把 8 个方法名塞进 apis/interfaces 硬键，
                # 24/37=65% 打回里方法占一半，教育 LLM 不收敛）。惯例判据非语言写死；
                # PascalCase 方法栈（C#）不受益也不受害（维持原 kind 分级）。
                return bool(s) and s[0].islower()

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


# ── DR-PM66-C2(#112) 考卷同源·接口方法名（round66 st-20-2/st-48-1 死循环真根）─────────────
def _camel_words(name: str) -> list[str]:
    """标识符按 camelCase/PascalCase 切成词序列（selectAlarmList → ['select','Alarm','List']）。"""
    return re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", name)


def _is_method_name_variant(a: str, b: str) -> bool:
    """a、b 是否为「插入/删除一个内部大写词」的近变体（selectAlarmScheduleStrategyList ↔
    selectScheduleStrategyList），且首词/尾词一致。仅用于识别契约签名 vs 描述的方法名分叉——
    首尾词锚定 + 恰差一个内部词，误配空间极小（不同于 difflib 宽松相似度，round31 F4 教训）。"""
    wa, wb = _camel_words(a), _camel_words(b)
    if not wa or not wb or wa[0].lower() != wb[0].lower() or wa[-1] != wb[-1]:
        return False
    long_, short_ = (wa, wb) if len(wa) >= len(wb) else (wb, wa)
    if len(long_) - len(short_) != 1:
        return False
    for k in range(1, len(long_) - 1):        # 删一个【内部】词（首尾已锚定一致）
        if long_[:k] + long_[k + 1:] == short_:
            # 复核整改（code-reviewer CONFIRMED MED）：要求差异词【之后】仍共享 ≥2 个词——区分
            # "实体前缀中缀差异"=真分叉（selectScheduleStrategyList ↔ selectAlarmScheduleStrategyList，
            # 差异词后共享 ScheduleStrategyList=3 词）与"两个恰差一词的【不同】CRUD 方法"=合法（RuoYi
            # 常见 selectAlarmList vs selectAlarmScheduleList，差异词后仅共享 List=1 词 → 不判分叉，
            # 防 fail-closed 误杀好 plan 空烧 replan 预算）。
            if len(long_) - k - 1 >= 2:
                return True
    return False


def validate_contract_signature_source(plan, shared_contract) -> PlanValidationResult:
    """DR-PM66-C2(#112) 考卷同源·接口方法名：契约 interface.signature 的方法名与【创建该接口文件的
    owner 子任务 description】里出现的方法名不得分叉。

    round66 死因：契约用短名 selectScheduleStrategyList，而 owner 子任务 st-20-2 描述用长名
    selectAlarmScheduleStrategyList（差一个 Alarm 中缀，6 个方法 5 个不一致）→ worker 按描述落长名
    接口、消费方（Controller/实现类）按契约调短名 → cannot find symbol，st-20-3/st-48-1 连续多轮
    空转（6.5h 最大黑洞之一）。规划期两个真值源就已分叉，worker 每轮被其中一个误导。

    保守判据（零误伤）：仅当某契约方法【未在 owner 描述里逐字出现】、【且】描述里存在它的【近变体】
    （_is_method_name_variant，插入/删除一个内部大写词）才判分叉——描述沉默（不列方法名）或完全
    一致均不触发。栈中立：camelCase 方法名惯例（小写开头），PascalCase 方法栈不受益也不受害。
    """
    from swarm.brain.contract_utils import _norm_scope_path
    result = PlanValidationResult(valid=True)
    ifaces = (shared_contract or {}).get("interfaces") or []
    if not isinstance(ifaces, list) or not ifaces:
        return result
    subtasks = list(getattr(plan, "subtasks", None) or [])

    def _owner_desc(defined_in: str) -> str | None:
        di = _norm_scope_path(defined_in)
        for st in subtasks:
            sc = getattr(st, "scope", None)
            files = (list(getattr(sc, "create_files", None) or [])
                     + list(getattr(sc, "writable", None) or []))
            if any(_norm_scope_path(f) == di for f in files):
                return getattr(st, "description", "") or ""
        return None

    for e in ifaces:
        if not isinstance(e, dict):
            continue
        di = str(e.get("defined_in") or "").strip()
        sig = str(e.get("signature") or "")
        if not di or not sig:
            continue
        desc = _owner_desc(di)
        if desc is None:
            continue        # 无 owner 子任务：另有 file_plan/ownership 闸覆盖，此处不重复报
        c_methods = re.findall(r"(\w+)\s*\(", sig)
        desc_tokens = set(re.findall(r"\b([a-z][a-zA-Z0-9]*[A-Z][a-zA-Z0-9]*)\b", desc))
        diverged: list[tuple[str, list[str]]] = []
        for c in c_methods:
            if not c or not c[0].islower():
                continue    # 只查方法名（小写开头惯例），跳过构造/类型名
            # 复核整改（猎手 PLAUSIBLE）：逐字出现用【词边界】匹配，非裸子串——否则 'get' 命中
            # 'target'/'budget' 等无关英文单词内部 → 误判"一致"放过真分叉（#112 假阴性）。
            if c in desc_tokens or re.search(rf"\b{re.escape(c)}\b", desc):
                continue    # 契约方法在描述里逐字出现 = 一致
            variants = [t for t in desc_tokens
                        if t not in c_methods and _is_method_name_variant(t, c)]
            if variants:
                diverged.append((c, sorted(variants)))
        if diverged:
            _pairs = "; ".join(f"契约 {c!r} vs 描述 {v}" for c, v in diverged[:6])
            result.add(
                f"接口 {e.get('name') or di} 的契约方法签名与其 owner 子任务描述【方法名分叉】"
                f"（考卷两个真值源打架，消费方按契约调用必 cannot find symbol 多轮空转）：{_pairs}。"
                f"统一为唯一真值：把 owner 子任务描述里的方法名改成与 shared_contract.signature "
                f"完全逐字一致（或反向修正契约），二者方法名必须相同。")
    return result


def validate_file_plan_ownership(
    plan, file_plan, exclude_test_paths: bool = False,
) -> PlanValidationResult:
    """R40-1(a)：file_plan 归属确定性闸——tech_design 规划的文件必须有 owner 子任务。

    round40 PARTIAL 直接死因：file_plan 43 文件里 3 个（含两个 ServiceImpl=BLOCKED
    "无生产者的包"核心实现类 + DDL）无任何子任务认领，批拆丢件规划期零校验，执行期
    才以 BLOCKED→连坐放弃形态爆发。规则（零 LLM）：
    - 先过与批拆同一个 P5 dedupe_file_plan（口径同源：同名去重由权威函数裁决，
      被 P5 丢弃的孪生件本就不该被要求 owner——复核 HIGH：自造 basename 豁免会
      静默放行真缺件，_PER_MODULE_FILENAMES 内每模块一份的文件按全路径各自硬性）；
    - 去重后仍无 owner 的每个文件【逐条】issue 打回（一条一 bullet 让 D09 A9
      分页轮转生效，防"固定截断头部永远修不到看不见的条目"；上限 60 防爆炸）；
    - 单子任务计划（SIMPLE 面自证）/空 file_plan → 跳过。
    """
    result = PlanValidationResult(valid=True)
    subtasks = list(getattr(plan, "subtasks", None) or [])
    files = normalized_file_plan_paths(file_plan, exclude_test_paths=exclude_test_paths)
    if len(subtasks) <= 1 or not files:
        return result
    owned_paths: set[str] = set()
    for st in subtasks:
        sc = getattr(st, "scope", None)
        for f in (list(getattr(sc, "create_files", None) or [])
                  + list(getattr(sc, "writable", None) or [])):
            owned_paths.add(str(f).replace("\\", "/").lstrip("/"))
    missing = [f for f in files if f not in owned_paths]
    for f in missing[:60]:
        result.add(
            f"file_plan 文件无 owner 子任务: {f} ——把它加入同模块子任务的"
            " create_files，或为其新建子任务（缺实现类=下游 BLOCKED 无生产者必死）")
    if len(missing) > 60:
        result.add(f"file_plan 缺件共 {len(missing)} 个（仅列前 60，逐轮分页轮转其余）")
    return result


def _cross_module_class_redefinitions(plan) -> dict[str, dict[str, list[str]]]:
    """全 plan 的 create_files 建 FQN→{物理模块: [子任务 id]} 倒排；同一 FQN 落 ≥2 物理模块=违规。
    只看 create_files（重复【创建】同类），modify 既有文件不算。disk-independent、栈中立。
    FQN 口径与 #101 去冲突归一共用 contract_utils.classpath_fqn_key（单一事实源）。"""
    from swarm.brain.contract_utils import classpath_fqn_key
    index: dict[str, dict[str, list[str]]] = {}
    for st in getattr(plan, "subtasks", None) or []:
        sc = getattr(st, "scope", None)
        for f in (list(getattr(sc, "create_files", None) or [])):
            key = classpath_fqn_key(f)
            if not key:
                continue
            mod, fqn = key
            index.setdefault(fqn, {}).setdefault(mod, []).append(getattr(st, "id", "?"))
    return {fqn: mods for fqn, mods in index.items() if len(mods) >= 2}


def validate_module_coherence(
    plan, *, project_path: str | None = None, file_plan: list | None = None,
    base_ref: str | None = None,
) -> PlanValidationResult:
    """G1 ★真治本闸★（Task#9 审计①②）：每个模块解析到【恰好一个物理构建单元】。

    60 轮死因家族（round44/57/59/62）真根 = 规划期【从不校验 plan 本身可执行性】：契约声明的
    【逻辑模块】与【物理构建目录】不相交/一对多/多对一时，脚手架归一器只能【尽力】造 pom，造错
    层级/撞车 → reactor 找不到项目 → 整批连坐。此前只有【归一器】(scaffold 注入)+【告警】，**没有
    验证闸**：不coherent 的 plan 照过 structure/C1/R40-1/coverage 四闸、死在 worker/reactor。

    本闸把【计划+契约自证的、与磁盘状态无关的】不coherent 硬失败——**只**打回 disk-independent
    的铁证，绝不做状态依赖判定（round59 血泪：依赖"目录存不存在"的判据第一轮过、replan 才炸）：

      ① 一个模块 → 计划里【多个】不同物理目录（module≠单一 build 单元；round62 alarm-api 双落点）。
      ② 多个模块 → 【同一个】物理目录（一个构建清单不可能是多个模块的构建文件；R59-2）。

    zero-dir（契约声明了模块、但计划里无任何代码落点）与自反聚合器 → **仅 warn**：前者离线无法
    区分"幻影模块"与"棕地既有基线目录"（硬判=状态依赖假阳）；后者由脚手架 LOUD 告警 + Task#4
    归一器处理。多栈中立：模块边界一律经 `_code_module_root`/`_SRC_LAYOUT_SEGMENTS` 求，绝不写死栈。
    """
    from swarm.brain.contract_utils import (
        _file_plan_module_paths,
        _resolve_module_dirs,
    )

    result = PlanValidationResult(valid=True)

    # ③ DR-PM66-C1(#110) 治本：同一 FQN（包路径+类名）被多子任务在【不同物理模块】各自 create =
    # 类路径副本遮蔽/split-package（round66 #101 真根：com.ruoyi.alarm.appkey.domain.AlarmAppSecret
    # 同时落 ruoyi-alarm 与 ruoyi-admin → 全局 reactor 编译时消费方解析到缺方法的错误副本 → cannot
    # find symbol）。L1 隔离编译（mvn -pl <mod> -am）看不见，L2 必炸——正是"81 done 是假象"。这是
    # disk-independent 铁证（纯 create_files 路径推导），fail-closed 打回，与 #101 的 normalize 去冲突
    # 互补（防御纵深）。仅 JVM 类路径共享命名空间源码适用（栈中立，见 _classpath_fqn_key）。此检查
    # 不依赖模块声明，故置于 `if not want` 早退之前（跨模块同 FQN 与模块宇宙无关）。
    _redef = _cross_module_class_redefinitions(plan)
    for fqn, mods in sorted(_redef.items()):
        _where = "; ".join(f"{m}（{','.join(ids[:3])}）" for m, ids in sorted(mods.items()))
        result.add(
            f"同一全限定类 {fqn!r} 被多个物理模块各自 create：{_where}。同 FQN 跨模块=类路径副本"
            f"遮蔽/split-package，全局编译时消费方会解析到缺方法的错误副本（cannot find symbol）。"
            f"该类只能属【一个】模块：其余模块请 import 该模块的 FQN 而非重建，并从其 create_files 移除。")

    # 模块宇宙 = 契约依赖声明的模块 ∪ file_plan 声明的模块（两者都是【这是一个模块】的权威声明）。
    deps = ((getattr(plan, "shared_contract", None) or {}).get("dependencies")) or []
    want = {(e.get("module") or "").strip().rstrip("/")
            for e in deps if isinstance(e, dict)} - {""}
    want |= set(_file_plan_module_paths(file_plan).keys())
    if not want:
        return result   # 无多模块声明 → 本不变量无适用面（单模块/greenfield）

    # ★消费单一权威 resolver 的结构化诊断（Task#9 双复核 CRITICAL 整改）★——绝不再 fork 一套
    # module→dir 扫描（forked-resolver 正是审计①病根；旧实现把尾部包名当第二个物理目录、
    # 确定性打回好 plan）。歧义/撞车的口径与脚手架**完全同源**：resolver 消歧（file_plan/基线
    # 覆盖名字匹配、扫到源码根即停）后仍未解的才是真违规，故绝不误伤惯例命名的单模块 plan。
    resolved, ambiguous, collision, cross_res = _resolve_module_dirs(
        plan, project_path, file_plan, base_ref=base_ref, with_cross_res=True)

    # ① 一对多：一个模块散落到多个物理目录 = module≠单一 build 单元（file_plan/基线未能消歧）
    for mod, dirs in sorted(ambiguous.items()):
        result.add(
            f"模块 {mod!r} 在计划里对应【多个物理目录】{dirs}——一个模块必须【恰好】是一个物理"
            f"构建单元（含单一构建清单的目录）。请把 {mod} 的全部文件归到同一个模块目录，并在 "
            f"file_plan 里明确它的归属；若它们本属不同物理模块，请起【不同的模块名】各自独立。")

    # ② 多对一：多个模块塌进同一物理目录 = 一个构建清单不可能是多个模块的构建文件（R59-2）
    for d, mods in sorted(collision.items()):
        result.add(
            f"模块 {mods} 全部落在【同一物理目录】{d!r}——同一个构建清单不可能是多个模块的构建"
            f"文件。请把它们合并为一个模块，或给每个模块独立的目录。")

    # zero-dir：声明了模块但既无落点、又非棕地基线目录（project_path 已在 resolver 里核过基线）
    # → 仅 warn（离线无法证伪幻影 vs 遗漏生产者，硬判=状态依赖假阳，round59 血泪）。
    _accounted = set(resolved) | set(ambiguous) | {m for ms in collision.values() for m in ms}
    _zero = sorted(m for m in want if m not in _accounted)
    if _zero:
        result.warn(
            f"{len(_zero)} 个声明的模块在计划里无任何代码落点、且非棕地既有基线目录（可能是幻影"
            f"依赖，也可能缺生产者子任务）：{_zero[:8]}")

    # ★R65E-T2 复核② CONFIRMED HIGH（silent-hunter）整改★ 资源/辅助文件（视图模板/静态 .js/
    # mapper XML/DDL…）落在模块【构建根之外】的物理目录：按设计【不主张物理根、不硬打回】——否则
    # round65e4 死因重现（RuoYi 带 UI 的 feature 视图必落 admin webapp、每个都被误杀）。但【运行时
    # 归属】（资源是否被打进正确模块的 jar/war、是否误路由进无关模块）G1 物理层无法判：升为软 warn
    # 结构化可观测（"降级可观测"铁律），移交 #67 语义/L1-L2 资源批核验，绝不只剩 logger.info 湮没。
    for mod, dirs in sorted(cross_res.items()):
        result.warn(
            f"模块 {mod!r} 的资源/辅助文件落在其构建根之外的物理目录 {dirs}（视图/静态资源/DDL 等，"
            f"按设计放行不阻断规划）——但其【运行时模块归属】G1 物理层无法核验，移交 #67 资源绑定/"
            f"L1-L2 批核验：请确认这些资源确属该模块且被打进正确构建产物、非误路由进无关既有模块。")
    return result


def validate_plan_granularity(
    plan, *, complex_ratio_warn: float = 0.6, min_subtasks: int = 4,
) -> PlanValidationResult:
    """G7+G8（Task#9 审计④ 颗粒度/难度路由）：规划期【颗粒度 smell】确定性检测——**仅告警**。

    这两条是启发式质量信号、**非执行致命**（COMPLEX-heavy / 混模块的 plan 照样能跑，只是难度
    路由把多数子任务压到最强模型、且更易失败/绕圈），故【绝不硬打回、绝不盲目强制再拆】：
      · 盲目 force-resplit 会把"COMPLEX 但单文件"的合法任务拆成多子任务同写一文件 → diff 行号
        冲突拼坏 patch（正是 planning_nodes._needs_resplit 单文件守卫要防的回归）；
      · 真·超预算/超文件数的 COMPLEX 子任务，_needs_resplit 已按预算+文件数确定性再拆。
    本闸只把【全局欠分解】这一此前**不可见**的信号 surface 出来（喂 validation warnings + 遥测 +
    replan feedback），令"67% 子任务是 COMPLEX"这类规划期质量问题可观测、可反馈重分解。多栈中立。

    G7 COMPLEX 占比：难度=COMPLEX 的子任务占比超阈值（且 plan 够大）→ warn（欠分解信号）。
    G8 混模块颗粒度：单个子任务的写目标（create ∪ writable）横跨 ≥2 个不同【物理模块根】
       （经 _code_module_root 求，栈中立）→ warn（一个子任务应内聚于一个模块，混关注点难验收）。
    """
    from swarm.brain.contract_utils import _code_module_root

    result = PlanValidationResult(valid=True)
    subs = list(getattr(plan, "subtasks", None) or [])
    if not subs:
        return result

    # ── G7：COMPLEX 占比（仅在 plan 够大时评估，小 plan 天然高占比不是 smell）──
    if len(subs) >= min_subtasks:
        # silent-hunter #2：难度取值 enum→.value / 纯串→自身（绝不因 difficulty 偶为字符串
        # 而静默漏数 COMPLEX）。注：str(str-Enum) 在部分 Python 版本返回 "类名.成员" 故不能用。
        def _diff_val(st):
            d = getattr(st, "difficulty", None)
            return str(getattr(d, "value", d) or "").lower()
        _n_complex = sum(1 for st in subs if _diff_val(st) == "complex")
        _ratio = _n_complex / len(subs)
        if _ratio > complex_ratio_warn:
            result.warn(
                f"G7 颗粒度：{_n_complex}/{len(subs)} 个子任务为 COMPLEX（占比 {_ratio:.0%}，超阈值 "
                f"{complex_ratio_warn:.0%}）——COMPLEX=架构/跨模块/复杂算法，理应稀少；占比过高多为规划期"
                f"【欠分解】（难度路由会把多数子任务压到最强模型、更易失败）。建议更细粒度拆分。")

    # ── G8：单子任务混模块（写目标横跨多个物理模块根）──
    for st in subs:
        sc = getattr(st, "scope", None)
        if sc is None:
            continue
        _targets = (list(getattr(sc, "create_files", None) or [])
                    + list(getattr(sc, "writable", None) or []))
        _roots = {r for f in _targets if (r := _code_module_root(f))}
        if len(_roots) >= 2:
            result.warn(
                f"G8 颗粒度：子任务 {getattr(st, 'id', '?')!r} 的写目标横跨 {len(_roots)} 个物理模块根 "
                f"{sorted(_roots)[:4]}——一个子任务应内聚于【单个模块】，跨模块混关注点难独立验收、"
                f"易并发撞写。建议按模块拆分为多个子任务。")
    return result


def normalized_file_plan_paths(file_plan, exclude_test_paths: bool = False) -> list[str]:
    """R40-1 口径适配：原始 file_plan（str 或 {path} dict 混合）→ P5 去重后的归一路径列表。

    与 plan 批拆消费同一 dedupe_file_plan（单一事实源）——被 P5 按 basename 丢弃的
    同名件不进校验分母，validate/repair 两侧共用本函数防口径分叉。
    exclude_test_paths（R41 复核 F2）：任务未要求测试时 _strip_unrequested_tests 会把
    测试文件从 scope 剥掉，但归属分母若仍计入=确定性弹跳（挂靠→剥离→打回→再挂靠，
    修复通道每轮"成功"却永不过闸）——分母必须与剥离对称（谓词同源 _is_test_file_path）。"""
    from swarm.brain.plan_batch import dedupe_file_plan
    entries = []
    for f in (file_plan or []):
        if isinstance(f, dict):
            if str(f.get("path") or "").strip():
                entries.append(f)
        elif str(f or "").strip():
            entries.append({"path": str(f)})
    deduped = dedupe_file_plan(entries)
    paths = [str(e["path"]).replace("\\", "/").strip("/")
             for e in deduped if isinstance(e, dict) and e.get("path")]
    if exclude_test_paths:
        from swarm.brain.nodes.shared import _is_test_file_path
        paths = [p for p in paths if not _is_test_file_path(p)]
    return paths


def validate_requirement_coverage(
    plan, requirement_items, baseline_covered=None, baseline_vocab=None,
    baseline_ineligible=None,
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
    # R65E9-T1：baseline_ineligible（曾被证据闸判假的 baseline id 单调集）传入矩阵 → pinned id 无条件
    # 落 uncovered → 走"未覆盖·分配子任务"出口（而非重复陷 baseline limbo）。缺省 None=行为不变。
    matrix = build_coverage_matrix(plan, requirement_items, baseline_covered,
                                   baseline_ineligible=baseline_ineligible)
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
        # DR-01-F4(#49) 治本：只对【真的缺 reason】的空申报报"缺 reason"。条目【带 reason】却未
        # 进 baseline_valid，是被证据闸/单调集(baseline_ineligible)踢出——它已由 uncovered issue
        # 或 baseline_claims_missing_evidence 专项 issue 覆盖。此处再报"缺 reason"=与那些 issue
        # 互斥的矛盾指令（补 reason 无效、仍被单调集再踢），恰复活 R65E9 baseline limbo 振荡。
        if (entry.get("reason") or "").strip():
            continue
        result.add(
            f"baseline_covered 申报 {entry['id']} 缺少 reason 理由"
            f"（申报存量已满足必须给出依据：现有代码何处/如何满足该需求）"
        )
    # R65E6-T1（round65e6 实锤）：假 baseline_covered 证据闸——申报"存量已满足"但该需求的判别术语
    # 在基线符号/文件索引里【零命中】= 极可能把【新特性】谎称存量（Google 2FA 嫁接无 2FA 方法的
    # SysUserController，静默丢出交付）。有 token 且全不在基线 → 打回，逼建子任务或改用有据 reason。
    # 纯中文/无索引豁免（round37 过严教训）。baseline_vocab=None（老调用点/测试）→ 不启用，行为不变。
    if baseline_vocab:
        from swarm.brain.baseline_candidates import baseline_claims_missing_evidence
        _text_by_id = {str(r.get("id")): str(r.get("text") or "")
                       for r in (requirement_items or []) if isinstance(r, dict) and r.get("id")}
        for rid in baseline_claims_missing_evidence(
                baseline_covered, requirement_items, baseline_vocab):
            result.add(
                f"baseline_covered 申报 {rid} 缺乏基线证据 — {_text_by_id.get(rid, '')[:100]}"
                f"（该需求的判别术语在现有代码符号/文件索引中【零命中】，基线极可能并无此能力："
                f"若确为新功能→分配子任务实现并 covers 此 ID，切勿谎称存量；"
                f"若确系存量→reason 必须引用现有代码中【真实出现该能力术语】的位置）"
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
