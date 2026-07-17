"""L1 裁决纯函数簇 —— 从 worker/executor.py 抽出（round26 god-file 治理）。

本模块只含【无 self 状态】的纯函数/常量/数据类，是 executor god-class 的叶簇：
    - trivial 自报 / 拒答-截断 检测（_is_refusal_or_truncated / _trivial_llm_self_report_passed）
    - 单一 L1 裁决仲裁器（L1Verdict / _det_fail_source / evaluate_l1）
    - LOCATING 阶段步数预算（_locate_step_cap）
    - seed 产物缺失/反推包（missing_seed_artifacts / packages_from_missing_artifacts）

executor.py 顶部 re-export 本模块符号回 `swarm.worker.executor` 命名空间，保持内部调用
与既有测试经 executor 命名空间导入（evaluate_l1 / L1Verdict / _locate_step_cap ...）的
可寻址性不变。行为逐字节等价，纯 move。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path  # noqa: F401  # 供 missing_seed_artifacts 的前向引用注解

from swarm.types import NotRunKind

# audit #22：trivial 快路径的 LLM 自报判定。原 `"fail" not in combined.lower()` 是裸
# 子串，会把 "check for failures" / "failed to X but recovered" 等正常叙述误判为失败。
# 改用词边界正则只命中独立失败词。注意：这只是【弱信号】，仅在确定性 L1 闸门无法判定
# （无工程文件可编译/测试）时回退使用；闸门可判时其结果优先覆盖本判定。
_FAIL_WORD_RE = re.compile(r"\b(fail|failed|failure|failures|error|errored|errors)\b")


# Bug-4（task 0f93f1fc 实证）：模型拒答/截断标记。worker agent 主回复命中这些 =
# 它根本没真正完成工作（停滞/截断/算力耗尽），产出不可信。此前这类回复仅让 LLM 自报
# LOCATING 阶段步数硬上限（recursion_limit 计节点访问，agent+tool 各 1，故 ~20 ≈ 10 think-act
# 循环）。定位只是"理解结构/确认落点"，有预读范例+契约时足够；逼模型少探索、把预算留给 CODING。
_LOCATE_STEP_CAP = 20
# #14 治本：定位预算硬顶（多文件弹性放宽的上界）。CODING 按 scope 文件数弹性(base+15/file)，
# 而 LOCATING 原 flat-20 → 多文件子任务勘察不全全部落点→CODING 欠信息空烧。此处让 LOCATING 也
# 弹性，但单文件/trivial 恒 20（不回归 RUN12 墙钟保护），多文件 +4/file、双重封顶不失控。
_LOCATE_STEP_CAP_MAX = 40


def _locate_step_cap(n_scope_files: int, max_iterations: int) -> int:
    """LOCATING 阶段 recursion_limit：base 20 + 每多一个目标文件 +4，硬顶 40，且 ≤ max_iterations。

    单文件/trivial(n≤1)→ 恒 20（与 RUN12 原行为逐字节一致，不回归墙钟保护）；多文件按需放宽以
    勘察全部落点，避免 CODING 欠信息空烧。纯文件计数、跨栈、非项目写死；上界双重封顶不失控。"""
    try:
        n = max(0, int(n_scope_files or 0))
    except (TypeError, ValueError):
        n = 0
    cap = _LOCATE_STEP_CAP + max(0, n - 1) * 4
    cap = min(cap, _LOCATE_STEP_CAP_MAX)
    if max_iterations and max_iterations > 0:
        cap = min(cap, max_iterations)
    return cap

# 判 False，但 deterministic gate（diff 非空 + compile 恰好过）会翻盘判通过 → 幻觉 PASS。
# 这类标记必须【硬否决整个 L1】，覆盖 deterministic gate——产出来源不可信时编译过也不算数。
# W1.2 commit②：补齐中文拒答/截断标记。本地中文模型（27B/40B）拒答多用中文措辞
# （"抱歉/我无法/无法完成/需要更多步骤"），原英文清单全部漏过 → 中文拒答被当有效产出
# 送进确定性闸门，compile 恰好过即幻觉 PASS。
_REFUSAL_MARKERS = (
    # ── 强标记：措辞特异，子串命中任意位置即拒答（不会出现在正常 fix 描述里）──
    "sorry, need more steps",
    "need more steps to process",
    "i'm unable to",
    "i am unable to",
    "cannot complete this request",
    "unable to complete",
    "i cannot complete",
    "需要更多步骤",
    "需要更多步数",
    "超出我的能力",
)

# TD2606-C1：弱中文"无能为力"标记。裸子串匹配会把【描述性回复】误判拒答，例如
# "原代码无法完成空值校验，现已修复" / "抱歉之前漏了，已补上测试" → 命中"无法完成"/"抱歉"
# 却是【成功产出】，旧逻辑判 refusal_hard_fail（sticky 不可翻盘）更毒。改为：弱标记只在
# 【无任何成功/完成信号】时才算拒答（真拒答如"抱歉，我无法完成此任务"不含成功信号）。
_REFUSAL_WEAK_CN = ("抱歉", "我无法", "无法完成", "无法继续", "我不能完成")
_SUCCESS_SIGNALS = (
    "已修复", "修复了", "已完成", "已实现", "已补", "已添加", "已新增",
    "测试通过", "通过测试", "编译通过", "l1_result:pass", "l1_result: pass", "✅",
)

# W1.2 commit②：verify 回复"可用性"下限。空/纯空格、或极短且无 L1_RESULT 标记的回复，
# 都不是有效的验证结论（模型截断/算力耗尽/空转），按不可用处理 → 非 PASS。
# 阈值设为 8：低于此长度且无显式 L1_RESULT 标记的回复，不可能承载有意义的验证结论。
_MIN_VERIFY_REPLY_CHARS = 8


def _is_refusal_or_truncated(text: str) -> bool:
    """判断 agent 回复是否为模型拒答/截断/不可用（非有效产出信号）。

    W1.2 commit② 硬化：
      1. 命中拒答/截断标记（中英）→ True。
      2. 空/纯空格回复 → True（截断/空转，无有效结论）。
      3. 极短回复（strip 后 < _MIN_VERIFY_REPLY_CHARS）且不含 L1_RESULT 标记 → True
         （无法承载有意义的验证结论；含 L1_RESULT 的短回复如 "L1_RESULT:PASS" 例外放行）。
    """
    stripped = (text or "").strip()
    if not stripped:
        # 空/纯空格 = 模型没给出任何有效回复（截断/空转），不可用 → 非 PASS。
        return True
    low = stripped.lower()
    if any(mk in low for mk in _REFUSAL_MARKERS):
        return True
    # C1：弱中文标记只在【无成功/完成信号】时才判拒答，避免误伤"无法…已修复"类描述性成功回复。
    if any(mk in stripped for mk in _REFUSAL_WEAK_CN) and not any(s in low for s in _SUCCESS_SIGNALS):
        return True
    # 极短且无显式验证标记 → 不可用。含 L1_RESULT 的短回复仍是有效结论，放行。
    if len(stripped) < _MIN_VERIFY_REPLY_CHARS and "l1_result" not in low:
        return True
    return False


def _trivial_llm_self_report_passed(combined: str) -> bool:
    """从 trivial agent 自由文本自报中弱判断是否通过（词边界匹配失败词）。"""
    if not combined:
        return True
    if "❌" in combined:
        return False
    return not bool(_FAIL_WORD_RE.search(combined.lower()))


# ════════════════════════════════════════════════════════════════════
# W1.2：单一 L1 裁决仲裁器（L1Verdict + evaluate_l1）
#
# 此前三处裁决点（Phase-3 循环 / trivial / Phase-4 翻盘）各自内联裁决逻辑，
# 真值表互有差异，且 Phase-4 的翻盘是【无条件】翻盘——只要 det True + llm True
# 就把循环内任何 fail（含编译失败、scope 违规等已确定的真错误）翻成 PASS，这是
# 幻觉 PASS 的最后一道漏洞。本仲裁器把三处统一到一张真值表，翻盘仅限【可翻盘来源】。
# ════════════════════════════════════════════════════════════════════

# 可翻盘来源白名单：仅这两类 fail 才允许 Phase-4 在确定性+LLM 双证据下翻盘为通过。
#   - empty_diff_transient：循环内空 diff（沙箱尚未 pull-back），pull-back 后可能有真产出。
#   - llm_self_report：纯 LLM 弱信号 fail（无确定性证据），收到确定性证据后可被覆盖。
# 编译/lint/scope/test/verify/refusal 失败都是【确定的真错误】，sticky=True，永不翻盘。
_FLIPPABLE_SOURCES = frozenset({
    "empty_diff_transient", "llm_self_report", "refusal_in_self_review",
    # 循环内「验证没跑成」(BLOCKED)是无确定性证据的非 sticky fail——Phase-4 pull-back 后
    # 若确定性闸门真跑通，应允许翻盘为通过（与 llm_self_report 同类）。
    "verification_not_run",
    # C5（阶段4）：verify 步拒答/截断是验证通道 artifact（provider 截断/沙箱限制），非对
    # 编码产出的否证——Phase-4 确定性+LLM 双证据到位时可翻盘（旧 sticky=True 让一次
    # provider 截断永久判死好产出）。
    "refusal_hard_fail",
})


@dataclass
class L1Verdict:
    """单一 L1 裁决结论。

    passed:  True=通过 / False=未通过 / None=无确定结论（仅 det_ok=None 且无 prior 时）。
    source:  裁决来源（refusal_hard_fail / 各确定性失败原因 / llm_self_report /
             deterministic / empty_diff_transient ...）。
    reason:  人类可读说明。
    sticky:  True=该 fail 是确定的真错误，永不翻盘；False=可在后续阶段被确定性证据翻盘。
    details: 透传的 l1_details 证据字典。
    """
    passed: bool | None
    source: str
    reason: str = ""
    sticky: bool = False
    details: dict = field(default_factory=dict)


def _det_fail_source(det_details: dict) -> tuple[str, str]:
    """把确定性闸门的失败 det_details 映射为 (source, reason)。

    覆盖契约要求的 5 类确定性失败：empty_diff_expected_changes / scope / compile /
    lint / test（含 build/verify 归入 test/compile 语义）。映射依据 _deterministic_l1_gate
    与 run_l1_pipeline 写入 details 的键。
    """
    reason_key = det_details.get("reason") or ""
    # 空 diff 但期望有变更：这是循环内最常见的"中途无 diff"，标记为 transient（可翻盘）。
    if reason_key == "empty_diff_but_changes_expected":
        return "empty_diff_transient", "空 diff 但期望有变更（沙箱可能尚未 pull-back）"
    # scope 违规
    if det_details.get("scope_violations"):
        return "scope", f"scope 违规: {det_details.get('scope_violations')}"
    if det_details.get("l1_1_scope_ok") is False:
        return "scope", "scope 违规"
    # 编译 / build 失败
    if det_details.get("l1_2_compile_ok") is False:
        return "compile", f"编译失败: {det_details.get('compile_message', '')[:120]}"
    if det_details.get("l1_2_1_build_ok") is False or det_details.get("build_failed"):
        return "compile", f"构建失败: {det_details.get('build_failed', '')}"
    # lint 语法级 error 硬阻断
    _lint = det_details.get("lint") or {}
    if isinstance(_lint, dict) and _lint.get("has_error") and _lint.get("gated"):
        return "lint", "lint 语法级 error 硬阻断"
    # 测试 / 验收命令失败
    if det_details.get("l1_3_test_ok") is False:
        return "test", "scoped 测试失败"
    if det_details.get("verify_failed"):
        return "test", f"验收命令失败: {det_details.get('verify_failed')}"
    # 兜底：确定性闸门判 fail 但未识别具体阶段
    return "deterministic", det_details.get("deterministic_gate") or "确定性闸门判失败"


def _det_fail_reason(details: dict) -> str:
    """R65D-W3①：确定性闸判死的机读 reason 提取——round65d st-26 冤案 79ms 判 False
    全程零解释。单一提取点，判死日志必带。
    猎手 HIGH：观测代码绝不反噬主流程——畸形 details 兑底成标记串而非冒泡崩 Phase-4。
    R65TR-T2：从 executor.py 移入本模块（executor re-export 保可寻址）——evaluate_l1
    失败时 stamp 进 details，供 brain 侧确定性装填 retry_guidance，免跨层 import。"""
    try:
        d = details if isinstance(details, dict) else {}
        if d.get("verify_failed"):
            return f"verify_failed: {str(d['verify_failed'])[:160]}"
        if d.get("reason"):
            _note = f" | {str(d['note'])[:120]}" if d.get("note") else ""
            return f"reason: {str(d['reason'])[:160]}{_note}"
        _sv = d.get("scope_violations")
        if _sv:
            _svl = list(_sv) if isinstance(_sv, (list, tuple, set)) else [_sv]
            return f"scope_violations×{len(_svl)}: " \
                   f"{[str(v)[:60] for v in _svl[:3]]}"
        if d.get("pipeline_blocked"):
            return f"pipeline_blocked: {str(d['pipeline_blocked'])[:120]}"
        # 复核 HIGH：单文件编译闸失败时真错误在 compile_message（此形态 build_output
        # 结构性缺席——early-return 于 harness 构建段之前），漏读=最常见判死形态
        # 照旧空 reason。
        # R65REPLAY 回放实锤修正：build 段失败时 compile_message 常是早段通过的
        # "compile ok"——旧序无条件先引它 = 判死依据自相矛盾（"compile_fail: compile
        # ok"）。按失败面取证：build 失败引 build_output 首个错误行。
        if d.get("l1_2_1_build_ok") is False or d.get("build_failed"):
            _bo = str(d.get("build_output") or "")
            if not _bo.strip():
                # 复核 F4：build_failed 存的是【构建命令】不是错误文本——空输出时绝不
                # 让命令冒充诊断（貌似有内容的假 reason 比"无输出"更害排障）。
                return ("build_fail: (构建无输出捕获) "
                        f"cmd={str(d.get('build_failed') or '?')[:100]}")
            # 复核 F5：错误行识别复用 output_compress 多栈信号正则（ERROR/error: 双
            # 字面量漏 npm ERR!/Gradle FAILURE/中文 错误: 等口径）。
            try:
                from swarm.worker.output_compress import _SIGNAL_RE
                _err = next((ln.strip() for ln in _bo.splitlines()
                             if _SIGNAL_RE.search(ln)), _bo[:160])
            except Exception:  # noqa: BLE001 — 正则源不可用退回双字面量
                _err = next((ln.strip() for ln in _bo.splitlines()
                             if "ERROR" in ln or "error:" in ln), _bo[:160])
            return f"build_fail: {_err[:160]}"
        if d.get("compile_message"):
            return f"compile_fail: {str(d['compile_message'])[:160]}"
        if d.get("l1_2_compile_ok") is False or d.get("build_output"):
            return f"compile_fail: {str(d.get('build_output') or '')[:160]}"
        if d.get("test_output"):
            return f"test_fail: {str(d['test_output'])[:160]}"
        return f"deterministic_gate={d.get('deterministic_gate', '?')}（无细分 reason 键）"
    except Exception as _exc:  # noqa: BLE001 — 提取失败自报，绝不把判死日志崩成通用异常
        return f"reason_extraction_failed:{type(_exc).__name__}"


def evaluate_l1(
    *,
    det_ok: bool | None,
    det_details: dict,
    verify_result: str | None,
    llm_ok: bool | None,
    prior: L1Verdict | None,
    phase: str,
) -> L1Verdict:
    """单一仲裁器出口包装（R65TR-T2 W2）：判死 verdict 必带机读
    details["det_fail_reason"]（回放实锤：st-2 五连跑判死原文从未抵达重试模型——
    brain 侧确定性装填 retry_guidance 的单一事实源在此 stamp）；通过时清除，
    保持「存在⟺判死」不变量（trivial 修复轮跨次合并 details 不残留陈旧依据）。"""
    verdict = _evaluate_l1_core(
        det_ok=det_ok, det_details=det_details, verify_result=verify_result,
        llm_ok=llm_ok, prior=prior, phase=phase,
    )
    try:
        if verdict.passed:
            verdict.details.pop("det_fail_reason", None)
        else:
            verdict.details["det_fail_reason"] = _det_fail_reason(verdict.details)
    except Exception:  # noqa: BLE001 — stamp 是观测增强，绝不反噬裁决
        pass
    return verdict


def _evaluate_l1_core(
    *,
    det_ok: bool | None,
    det_details: dict,
    verify_result: str | None,
    llm_ok: bool | None,
    prior: L1Verdict | None,
    phase: str,
) -> L1Verdict:
    """单一 L1 裁决仲裁器——三处裁决点共用。决策顺序（首个命中即返回）：

    1. refusal/截断（_is_refusal_or_truncated(verify_result)）：
       - det_ok is True（文件已创建、编译通过）→ 拒答只在"自读验证"阶段（沙箱限制），
         非执行拒绝。source=refusal_in_self_review，sticky=False（可翻盘）。
       - det_ok 非 True（无确定性证据 / 确定性失败）→ source=refusal_hard_fail，
         sticky=True，覆盖一切，永不翻盘。
    2. det_ok is False → False，sticky=True，source 携带确定性失败原因。永不翻盘。
       （例外：empty_diff_transient sticky=False，是设计上唯一可翻盘的 det fail。）
    3. det_ok is None（无 diff 可检 + 无 harness）→ passed=llm_self_report，
       source=llm_self_report，sticky=False。不主动翻盘 prior 的 fail（维持 prior 结论）。
    4. det_ok is True → 考虑 llm_ok：
       - llm_ok is False → False（确定性证据冲突，非 sticky 但当前结论 fail）。
       - llm_ok is True：
         · prior is None → True。
         · prior.passed is True → True（维持）。
         · prior.passed is False → 仅当 prior.sticky is False 且
           prior.source ∈ _FLIPPABLE_SOURCES 才翻盘为 True；否则维持 False。
    """
    details = dict(det_details or {})

    # ① refusal / 截断 / 不可用 → 视确定性证据分两级处理。
    #    verify_result is None 表示【本阶段不提供 verify 文本】（如 Phase-4：refusal 已在
    #    循环内裁过并落进 prior），跳过 refusal 检测——绝不能把"没传文本"误判为"空回复拒答"。
    if verify_result is not None and _is_refusal_or_truncated(verify_result):
        if verify_result:
            details["raw_refusal"] = verify_result[:200]
        details["raw_result"] = "(模型拒答/截断，非有效验证自报)"
        if det_ok is True:
            # 确定性闸门已通过（文件真实创建、编译通过）：拒答只发生在"自读验证"步骤
            # （沙箱限制致模型说"无法直接读取刚创建的文件"），非执行阶段拒绝。
            # 降级为可翻盘的非 sticky fail，Phase4 确定性+LLM 双证据可接管。
            details["l1_decision_source"] = "refusal_in_self_review"
            return L1Verdict(
                passed=False, source="refusal_in_self_review",
                reason="verify 拒答/截断，但确定性闸门已通过——拒答只在自读验证阶段，降级为可翻盘 fail",
                sticky=False, details=details,
            )
        details["l1_decision_source"] = "refusal_hard_fail"
        return L1Verdict(
            passed=False, source="refusal_hard_fail",
            reason="verify 步拒答/截断且无确定性通过证据——当前判失败；"
                   "拒答发生在验证通道（编码产出经 det 闸门另行裁决），后续确定性证据可翻盘",
            # C5（阶段4，登记册 §四）：sticky True→False。verify_result 只来自 verify agent
            # 步——拒答/截断是【验证通道】artifact（provider 截断/沙箱限制），不是对编码产出
            # 的否证（编码没产出会被 det 闸门 empty_diff 抓）。旧 sticky=True 让 provider
            # 一次截断永久判死好产出（Phase-4 pull-back 后 det 证据也无法接管）。
            # source 名保留：brain FINDING-12 据它强制强模型重试（处方合理，不动路由）。
            sticky=False, details=details,
        )

    # ② det_ok is False → 确定性失败
    if det_ok is False:
        source, reason = _det_fail_source(details)
        sticky = source != "empty_diff_transient"  # 仅 transient 空 diff 可翻盘
        details["l1_decision_source"] = (
            "empty_diff_transient" if source == "empty_diff_transient" else "deterministic"
        )
        return L1Verdict(passed=False, source=source, reason=reason,
                         sticky=sticky, details=details)

    # ③ det_ok is None → 无确定性结论。【fail-closed 核心】必须区分「为何没结论」。
    #    not_run_kind 由 _deterministic_l1_gate / run_l1_pipeline 写入 details：
    #      BENIGN  = 真 no-op（空 diff + 无 harness + scope 不期望改动）→ 可回退 LLM 弱信号。
    #      BLOCKED = 本应验证却跑不起来（pipeline 异常 / 工具或工程清单缺失 / 构建命中 infra
    #                故障 / 非空 diff 却解析到 0 文件）→ 绝不当 PASS。
    #      缺失/未知 → 按 BLOCKED 处理（fail-closed 默认，这是与旧行为相反的关键反转：
    #                旧行为 `passed = bool(llm_ok)` 把「没验证」判给模型自报，是静默成功总根）。
    if det_ok is None:
        kind = details.get("not_run_kind")
        if prior is not None and prior.passed is False:
            # 缺乏确定性证据，不足以翻盘循环内/此前的 fail → 维持 prior 结论。
            details["l1_decision_source"] = "verification_not_run_keep_prior"
            return L1Verdict(passed=False, source=prior.source,
                             reason="无确定性证据(det=None)，维持 prior 的未通过结论",
                             sticky=prior.sticky, details=details)
        if prior is not None and prior.passed is True:
            # D12 治本：det_ok=None 表示【本轮没重新做确定性判定】（预算耗尽 / diff 异常 /
            # pipeline 异常 / pull-back skip 等），并非【否定】。若上一轮（循环内）已用确定性证据
            # 坐实 PASS，则不应把它翻成失败——否则典型链路「验证循环确定性通过 → Phase-4 produce
            # 超预算(det=None)」会落到 verification_not_run(passed=False)+timeout marker → brain
            # 的 _TIMEOUT_OVERSIZE_MARKERS 当 oversize 把整份已完成工作拆小重做（diff 已回传却作废）。
            # 与 prior.passed is False 分支对称：det=None 一律【维持】prior 已坐实的结论，不主动翻盘。
            details["l1_decision_source"] = "verification_not_run_keep_prior_pass"
            return L1Verdict(passed=True, source=prior.source,
                             reason="无确定性证据(det=None)，保留 prior 已坐实的通过结论（不翻转）",
                             sticky=prior.sticky, details=details)
        if kind == NotRunKind.BENIGN.value:
            details["l1_decision_source"] = "no_op_benign"
            return L1Verdict(passed=bool(llm_ok), source="no_op_benign",
                             reason="真 no-op（无可验证产出），回退 LLM 弱信号",
                             sticky=False, details=details)
        # BLOCKED 或未知 → fail-closed：不采信 LLM 自报，标 transient 交 brain 退避重试，
        # 耗尽 transient 配额后落 capability 阶梯 → 最终硬 FAIL（绝不静默通过）。
        details["l1_decision_source"] = "verification_not_run"
        details["failure_class"] = "transient"
        return L1Verdict(
            passed=False, source="verification_not_run",
            reason=f"确定性验证未能执行(not_run_kind={kind or 'unknown'})，"
                   "fail-closed 不采信 LLM 自报，转 transient 退避重试",
            sticky=False, details=details,
        )

    # ④ det_ok is True → 考虑 LLM 自检
    details["l1_decision_source"] = "deterministic"
    if llm_ok is False:
        return L1Verdict(passed=False, source="deterministic_llm_conflict",
                         reason="确定性闸门通过但 LLM 自检判失败（证据冲突，当前结论 fail）",
                         sticky=False, details=details)
    # llm_ok is True（或 None 视作不反对，由调用点决定是否传 True）
    if prior is None or prior.passed is True:
        return L1Verdict(passed=True, source="deterministic",
                         reason="确定性闸门 + LLM 自检通过", sticky=False, details=details)
    # prior.passed is False：仅可翻盘来源 + 非 sticky 才翻盘
    if prior.sticky is False and prior.source in _FLIPPABLE_SOURCES:
        return L1Verdict(passed=True, source="deterministic",
                         reason="确定性+LLM 双证据通过，翻盘可翻盘来源的 prior fail",
                         sticky=False, details=details)
    return L1Verdict(
        passed=False, source=prior.source,
        reason=f"prior fail 不可翻盘(source={prior.source}, sticky={prior.sticky})，维持未通过",
        sticky=prior.sticky, details=details,
    )


def exception_l1_details(exc: BaseException, failure_class: str | None) -> dict:
    """R63-T7：worker execute() 异常路径的 l1_details 组装（单一权威）。

    StreamDegenerationError（流式复读退化，链尾无模型可切时冒泡至此）打
    l1_decision_source=degeneration_hard_fail——brain FINDING-12 据此 force_strong
    升最强模型重派（与 refusal_hard_fail 同通路：都是"同模型重试只会更糟"的能力信号）。
    普通异常绝不冒充该标记（否则一切 infra 异常都被升档，烧穿最强模型配额）。
    """
    details: dict = {"error": str(exc), "failure_class": failure_class}
    from swarm.models.errors import StreamDegenerationError
    if isinstance(exc, StreamDegenerationError):
        details["l1_decision_source"] = "degeneration_hard_fail"
        if exc.evidence:
            details["degeneration_evidence"] = dict(exc.evidence)
    return details


def missing_seed_artifacts(artifacts: list[str], local_root: "Path") -> list[str]:
    """#12 治本(B fail-closed seed)·纯函数：返回【上游产物里缺失于本地树】的相对路径（去重保序）。

    upstream_artifacts 是 plan 标注的 provenance——本子任务 readable 里由上游/兄弟 create_files
    传播来的产物。基线只读上下文不入此集，故【缺失即上游未就绪/被 revert】，无误判基线之虞。"""
    out: list[str] = []
    for rel in dict.fromkeys(artifacts or []):
        r = str(rel).strip()
        if not r:
            continue
        try:
            if not (local_root / r).is_file():
                out.append(r)
        except OSError:
            out.append(r)  # 无从判定 → 保守当缺失（fail-closed）
    return out


def packages_from_missing_artifacts(missing: list[str]) -> list[str]:
    """从缺失的源文件路径反推【被阻断的内部包】（供 brain 反查生产者子任务）。去重保序。

    仅对可识别包路径的源文件（file_path_to_fqn 命中 src 根）产出包；非源文件忽略。通用跨栈
    的部分交 blocked_on_files 承载。"""
    from swarm.worker.symbol_resolver import file_path_to_fqn
    pkgs: list[str] = []
    seen: set[str] = set()
    for rel in missing or []:
        fqn = file_path_to_fqn(str(rel))
        if fqn and "." in fqn:
            pkg = fqn.rsplit(".", 1)[0]
            if pkg and pkg not in seen:
                seen.add(pkg)
                pkgs.append(pkg)
    return pkgs
