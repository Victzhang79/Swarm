"""WorkerExecutor — 管理 Worker Agent 生命周期

执行环境：远程 CubeSandbox（E2B），sandbox-first 模式。
本地 project_path 仅作 bootstrap 输入与产出 pull-back 持久化；
Phases 1–3 的读写、编译、测试均在沙箱 /workspace 内进行。

执行阶段:
    Phase 0: 准备 — 创建远程沙箱、bootstrap 同步本地 → /workspace
    Phase 1: 定位（<5s） → 在沙箱内阅读代码、理解结构
    Phase 2: 编码（10-60s）→ 在沙箱内实现变更
    Phase 3: L1 验证（10-120s）→ 沙箱内编译+测试，失败则修复
    Phase 4: 产出 → pull-back 到本地、收集 diff、生成 WorkerOutput

使用方式:
    executor = WorkerExecutor(subtask=subtask)
    output = await executor.run()
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from enum import Enum
from pathlib import Path
from typing import Any

from swarm.config.settings import get_config
from swarm.tools.scope_guard import clear_scope
from swarm.types import (
    Confidence,
    FileScope,
    KnowledgeContext,
    SubTask,
    SubTaskDifficulty,
    WorkerOutput,
)

logger = logging.getLogger(__name__)


# audit #22：trivial 快路径的 LLM 自报判定。原 `"fail" not in combined.lower()` 是裸
# 子串，会把 "check for failures" / "failed to X but recovered" 等正常叙述误判为失败。
# 改用词边界正则只命中独立失败词。注意：这只是【弱信号】，仅在确定性 L1 闸门无法判定
# （无工程文件可编译/测试）时回退使用；闸门可判时其结果优先覆盖本判定。
_FAIL_WORD_RE = re.compile(r"\b(fail|failed|failure|failures|error|errored|errors)\b")


# Bug-4（task 0f93f1fc 实证）：模型拒答/截断标记。worker agent 主回复命中这些 =
# 它根本没真正完成工作（停滞/截断/算力耗尽），产出不可信。此前这类回复仅让 LLM 自报
# 判 False，但 deterministic gate（diff 非空 + compile 恰好过）会翻盘判通过 → 幻觉 PASS。
# 这类标记必须【硬否决整个 L1】，覆盖 deterministic gate——产出来源不可信时编译过也不算数。
_REFUSAL_MARKERS = (
    "sorry, need more steps",
    "need more steps to process",
    "i'm unable to",
    "i am unable to",
    "cannot complete this request",
)


def _is_refusal_or_truncated(text: str) -> bool:
    """判断 agent 回复是否为模型拒答/截断标记（非有效产出信号）。"""
    if not text or not text.strip():
        return False
    low = text.lower()
    return any(mk in low for mk in _REFUSAL_MARKERS)


def _trivial_llm_self_report_passed(combined: str) -> bool:
    """从 trivial agent 自由文本自报中弱判断是否通过（词边界匹配失败词）。"""
    if not combined:
        return True
    if "❌" in combined:
        return False
    return not bool(_FAIL_WORD_RE.search(combined.lower()))


class WorkerPhase(str, Enum):
    """Worker 执行阶段"""
    PREPARING = "PREPARING"
    LOCATING = "LOCATING"       # Phase 1
    CODING = "CODING"           # Phase 2
    VERIFYING = "VERIFYING"     # Phase 3
    PRODUCING = "PRODUCING"     # Phase 4
    DONE = "DONE"
    FAILED = "FAILED"


class WorkerExecutor:
    """Worker 生命周期管理器

    管理一个 Worker Agent 从准备到产出的完整生命周期。
    """

    def __init__(
        self,
        subtask: SubTask,
        scope: FileScope | None = None,
        model_name: str | None = None,
        model_strategy: str = "cost_optimized",
        knowledge: KnowledgeContext | None = None,
        project_path: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        user_profile_prompt: str = "",
        shared_contract: dict | None = None,
    ):
        self.subtask = subtask
        self.effective_scope = scope or subtask.scope
        self.shared_contract = shared_contract or {}
        self.model_name = model_name
        self.model_strategy = model_strategy
        self.knowledge = knowledge
        self.project_id = project_id
        self.task_id = task_id
        self.user_profile_prompt = user_profile_prompt
        self.project_path = project_path or str(get_config().workspace_root)

        config = get_config()
        self.max_execution_time = config.worker.max_execution_time
        self.max_iterations = config.worker.max_iterations
        self.max_fix_rounds = config.worker.max_fix_rounds
        if subtask.difficulty == SubTaskDifficulty.TRIVIAL:
            # trivial 快速路径：合并定位+编码于一次 agent 运行，recursion_limit 要给够。
            # LangGraph recursion_limit 计每个节点访问(agent节点+tool节点各 1)，
            # 故 N 步 ≈ N/2 个 think-act 循环。原来 12(≈6 循环)对真实企业级文件编辑
            # (read→think→patch→compile→read→fix)不够，小模型频繁撞 "need more steps"。
            # 提到 30(≈15 循环)，与"子代理迭代到完成"理念一致；仍受 max_execution_time 兜底。
            self.max_iterations = min(self.max_iterations, 30)
            self.max_fix_rounds = 0

        # 运行时状态
        self.phase = WorkerPhase.PREPARING
        self.start_time: float = 0.0
        self.execution_log: list[str] = []
        self.fix_rounds: int = 0
        self._agent: dict | None = None
        self._sandbox: Any | None = None
        self._sandbox_manager: Any | None = None
        self._sandbox_pool: Any | None = None
        self._from_pool: bool = False
        # 沙箱归还热池时据此决定 reusable（L1 通过/无异常才复用，脏沙箱不回池）
        self._l1_passed_flag: bool = True
        # diff 基线/产出快照（difflib 生成 diff 用）。沙箱模式由 _sync_to/from_sandbox 填充，
        # 本地模式由 _snapshot_scope_local 填充。__init__ 初始化避免本地模式下属性缺失。
        self._pre_sync_contents: dict[str, str | None] = {}
        self._post_sync_contents: dict[str, str | None] = {}

        # 归一化 scope：Brain 有时把【已存在】文件误判进 create_files（如"给现有
        # ruoyi.js 加函数"），create_files 走"新建"路径(不上传/不读取原内容)会丢内容。
        # 放在 execution_log 等运行时状态初始化之后，因其内部会 _log。
        self._normalize_scope_create_files()

    def _normalize_scope_create_files(self) -> None:
        """把 scope.create_files 中【本地已存在】的文件降级到 writable（修改语义）。

        Brain 规划偶把现有文件误判为新建（实测：给已存在的 ruoyi.js 加函数被分到
        create_files）。create_files 走"新建"路径(不上传/不读取原内容)，对追加改动
        会丢失原文件内容。这里在 worker 启动即纠正：存在即视为修改。幂等、无副作用。
        """
        scope = self.effective_scope
        create = list(getattr(scope, "create_files", []) or [])
        if not create or not self.project_path:
            return
        from pathlib import Path as _P
        root = _P(self.project_path)
        writable = list(getattr(scope, "writable", []) or [])
        still_create: list[str] = []
        moved: list[str] = []
        for f in create:
            rel = str(f).strip()
            if rel and (root / rel).is_file():
                if rel not in writable:
                    writable.append(rel)
                moved.append(rel)
            else:
                still_create.append(rel)
        if moved:
            try:
                scope.create_files = still_create
                scope.writable = writable
            except Exception:  # noqa: BLE001
                return  # scope 不可变则放弃（不阻断）
            self._log(f"scope 归一化：{len(moved)} 个已存在文件从 create_files 降级为 writable: {moved[:5]}")

    def _log(self, message: str) -> None:
        """记录执行日志"""
        elapsed = time.monotonic() - self.start_time if self.start_time else 0
        entry = f"[{elapsed:.1f}s][{self.phase.value}] {message}"
        self.execution_log.append(entry)
        logger.info(f"Worker({self.subtask.id}): {entry}")
        if self._sandbox and self._sandbox_manager:
            sid = getattr(self._sandbox, "sandbox_id", None)
            if sid:
                self._sandbox_manager.append_activity(sid, "worker", entry)

    def _check_timeout(self) -> bool:
        """检查是否超时"""
        if not self.start_time:
            return False
        elapsed = time.monotonic() - self.start_time
        return elapsed >= self.max_execution_time

    async def run(self) -> WorkerOutput:
        """执行完整的 Worker 生命周期

        Returns:
            WorkerOutput 产出物
        """
        self.start_time = time.monotonic()
        self._log(f"开始执行子任务: {self.subtask.id}")

        from swarm.tools.build_tools import (
            clear_extra_whitelist,
            clear_sandbox_context,
            set_extra_whitelist,
            set_sandbox_context,
        )

        # 按子任务 harness 放行其构建/测试/验收命令（否则 worker 跑不了验证命令）
        _harness = getattr(self.subtask, "harness", None)
        set_extra_whitelist(getattr(_harness, "extra_whitelist", None) if _harness else None)

        try:
            # ── Phase 0: 准备 ──
            self.phase = WorkerPhase.PREPARING
            self._log("准备阶段：设置 Scope，创建 Agent")

            cfg = get_config()
            if cfg.sandbox.use_for_worker and cfg.sandbox.api_url:
                try:
                    from swarm.worker.sandbox import (
                        SandboxUnhealthyError,
                        get_sandbox_manager,
                    )

                    self._sandbox_manager = get_sandbox_manager()
                    # 热池启用时从池借（省冷启动），否则直接创建
                    from swarm.worker.sandbox_pool import get_sandbox_pool, pool_enabled

                    # 模板选择优先级：harness 显式指定 > 按子任务语言+性质映射 > 默认模板。
                    # 性质判定（方案 B）：harness 有 build/test 命令 → 需真编译/测试 →
                    # 用 verify(4c4g 带缓存完整环境)；纯写代码类 → 用 exec(2c2g 轻量)。
                    # 一个沙箱跑到底（不分阶段切换），按子任务整体性质选镜像。
                    _harness = getattr(self.subtask, "harness", None)
                    _tpl = getattr(_harness, "sandbox_template", "") or ""
                    # 项目专属模板（方案 B）自带完整项目源码进 /workspace；用它时 worker
                    # 只需上传被改的 writable 文件（readable 镜像已有，不必传，见 _sync_to_sandbox）。
                    self._sandbox_has_source = False
                    # 【项目级定制沙箱】优先用项目专属模板（预处理时按真实环境构建，
                    # 见 docs/DESIGN_project_sandbox_prebake_source.md）。无则回退按语言选通用模板。
                    if not _tpl and self.project_id:
                        try:
                            from swarm.project.store import get_project
                            _proj = get_project(self.project_id)
                            _proj_tpl = ((_proj or {}).get("config") or {}).get("sandbox_template", "")
                            if _proj_tpl:
                                _tpl = _proj_tpl
                                self._sandbox_has_source = True  # 专属模板自带源码
                                self._log(f"沙箱镜像选择: 项目专属模板={_tpl}（自带项目源码）")
                        except Exception as exc:  # noqa: BLE001 — 读项目失败不阻断，回退通用
                            self._log(f"读项目专属模板失败，回退通用: {exc}")
                    if not _tpl:
                        _lang = getattr(_harness, "language", "") or ""
                        _has_build = bool(getattr(_harness, "build_command", "") or "")
                        _has_test = bool(getattr(_harness, "test_command", "") or "")
                        _purpose = "verify" if (_has_build or _has_test) else "exec"
                        _tpl = cfg.sandbox.template_for_language(_lang, purpose=_purpose)
                        self._log(f"沙箱镜像选择: 语言={_lang or '?'} 用途={_purpose} 模板={_tpl or '默认'}")
                    _tpl = _tpl or None
                    # 借/建沙箱 + envd 健康探活：不健康则弃用换新（最多 health_retries 次）。
                    # 修复：坏镜像/envd 故障的沙箱会让 agent 空转到超时（实测 node 坏镜像
                    # 死循环 186 次/10 分钟），探活提前拦截。
                    _health_on = getattr(cfg.sandbox, "sandbox_health_check", True)
                    _max_tries = (getattr(cfg.sandbox, "sandbox_health_retries", 2) + 1) if _health_on else 1
                    self._from_pool = pool_enabled()
                    if self._from_pool:
                        self._sandbox_pool = get_sandbox_pool()
                    for _attempt in range(_max_tries):
                        if self._from_pool:
                            self._sandbox = self._sandbox_pool.acquire(
                                _tpl,
                                project_id=self.project_id,
                                task_id=self.task_id or self.subtask.id,
                            )
                        else:
                            self._sandbox = self._sandbox_manager.create(
                                template_id=_tpl,
                                project_id=self.project_id,
                                task_id=self.task_id or self.subtask.id,
                                source=f"worker:{self.subtask.id}",
                            )
                        # envd 健康探活
                        if _health_on and not self._sandbox_manager.health_check(self._sandbox):
                            _bad = self._sandbox.sandbox_id
                            self._log(
                                f"沙箱 {_bad} envd 健康探活失败"
                                f"（疑似镜像/envd 故障），弃用换新 [{_attempt + 1}/{_max_tries}]"
                            )
                            # 坏沙箱不归还热池，直接销毁。
                            # 关键修复：从池借出的沙箱(_from_pool)必须走 pool.release(reusable=False)
                            # 而非裸 manager.kill —— kill 只销毁服务端实例，不扣减池的 borrowed 计数，
                            # 导致探活失败的沙箱永久泄漏 borrowed（实测 borrowed=3/server=0 幽灵记账，
                            # 占满 max_total 配额后续无法借出）。release(reusable=False) 既 kill 又扣计数。
                            try:
                                if self._from_pool and self._sandbox_pool is not None:
                                    self._sandbox_pool.release(self._sandbox, reusable=False)
                                else:
                                    self._sandbox_manager.kill(_bad)
                            except Exception:  # noqa: BLE001
                                pass
                            self._sandbox = None
                            continue
                        # 健康（或未开启探活）→ 用它
                        if self._from_pool:
                            self._log(f"远程沙箱(热池)已就绪: {self._sandbox.sandbox_id}")
                        else:
                            self._log(f"远程沙箱已创建: {self._sandbox.sandbox_id}")
                        break
                    if self._sandbox is None:
                        raise RuntimeError(
                            f"连续 {_max_tries} 个沙箱 envd 健康探活均失败"
                            f"（镜像 {_tpl} 疑似故障），放弃远程沙箱"
                        )
                    set_sandbox_context(self._sandbox, self._sandbox_manager)
                    # 批次2-B：bootstrap 上传前先把 scope 内 tracked 文件 reset 到 HEAD，
                    # 杜绝上一轮 pull-back 写回本地的改动跨子任务/重试累积叠加。
                    self._reset_scope_to_head()
                    await self._sync_to_sandbox("bootstrap")
                except SandboxUnhealthyError as exc:
                    # 熔断：沙箱运行中连续失败达阈值 → 明确失败，不降级空转
                    self._log(f"沙箱熔断: {exc}")
                    raise
                except Exception as exc:
                    self._log(f"沙箱创建失败，降级本地执行: {exc}")
            else:
                self._log("沙箱未启用，文件与命令将在本地执行")

            self._agent = self._create_agent()
            self._log("Agent 创建完成")

            if self._check_timeout():
                return self._make_output(
                    diff="",
                    summary="超时：准备阶段即超时",
                    confidence=Confidence.LOW,
                    l1_passed=False,
                    l1_details={"error": "timeout_in_preparing"},
                )

            if self.subtask.difficulty == SubTaskDifficulty.TRIVIAL:
                return await self._run_trivial_fast()

            # ── Phase 1: 定位 ──
            self.phase = WorkerPhase.LOCATING
            self._log("定位阶段：阅读代码，理解结构")
            locate_result = await self._run_agent(
                self._build_locate_prompt(),
                step="locate",
            )
            self._log(f"定位完成: {locate_result[:200]}")

            if self._check_timeout():
                return self._make_output(
                    diff="",
                    summary="超时：定位阶段超时",
                    confidence=Confidence.LOW,
                    l1_passed=False,
                    l1_details={"error": "timeout_in_locating"},
                )

            # ── Phase 2: 编码 ──
            self.phase = WorkerPhase.CODING
            self._log("编码阶段：实现变更")
            code_result = await self._run_agent(
                self._build_code_prompt(locate_result),
                step="code",
            )
            self._log(f"编码完成: {code_result[:200]}")

            if self._check_timeout():
                return self._make_output(
                    diff="",
                    summary="超时：编码阶段超时",
                    confidence=Confidence.LOW,
                    l1_passed=False,
                    l1_details={"error": "timeout_in_coding"},
                )

            # ── Phase 3: L1 验证（含重试循环） ──
            self.phase = WorkerPhase.VERIFYING
            l1_passed = False
            l1_details: dict = {}

            for fix_round in range(self.max_fix_rounds + 1):
                self.fix_rounds = fix_round
                self._log(f"L1 验证轮次 {fix_round + 1}/{self.max_fix_rounds + 1}")

                verify_result = await self._run_agent(
                    self._build_verify_prompt(),
                    step=f"verify-{fix_round}",
                )
                llm_passed, l1_details = self._parse_l1_result(verify_result)

                # 确定性闸门优先：用真实 compile/lint/scope 结果覆盖 LLM 自报。
                # 借鉴 ECC —— 确定性断言驱动修复循环，杜绝 LLM 幻觉 PASS 提前 break。
                # ── 关键修复：循环内确定性闸门查的是本地 difflib diff（_post_sync_contents），
                #    但 worker 在【沙箱】里改文件，循环内若不先 pull-back，本地 diff 恒空 →
                #    `empty_diff_but_changes_expected` 每轮必 fail（medium/complex 子任务白跑满
                #    修复轮，靠 Phase4 pull-back 后才翻盘，又慢又易误判）。
                #    故沙箱模式下：闸门前先把沙箱改动 pull-back 刷新本地，使 diff 反映真实改动。
                if self._sandbox and self._sandbox_manager:
                    await self._sync_from_sandbox(f"verify-{fix_round} 闸门前同步")
                det_ok, det_details = self._deterministic_l1_gate()
                l1_details = {**l1_details, **det_details}
                if det_ok is None:
                    # 无 diff 可检，回退到 LLM 自报信号
                    l1_passed = llm_passed
                    l1_details["l1_decision_source"] = "llm_self_report"
                else:
                    l1_passed = det_ok
                    l1_details["l1_decision_source"] = "deterministic"
                    if det_ok and not llm_passed:
                        self._log("确定性闸门通过但 LLM 自报失败，以确定性结果为准")
                    elif not det_ok and llm_passed:
                        self._log("LLM 自报通过但确定性闸门失败，以确定性结果为准（拦截幻觉 PASS）")

                # Bug-4 根治：verify 回复是拒答/截断标记 → worker 没真正完成，产出不可信，
                # 硬否决覆盖 deterministic gate（即便 compile 恰好过也不算数）。
                if _is_refusal_or_truncated(verify_result):
                    l1_passed = False
                    l1_details["l1_decision_source"] = "refusal_hard_fail"
                    l1_details["raw_refusal"] = (verify_result or "")[:200]
                    self._log("verify 回复为拒答/截断标记，硬否决 L1（产出不可信，覆盖确定性闸门）")

                self._log(
                    f"L1 验证结果: {'通过 ✅' if l1_passed else '未通过 ❌'} "
                    f"| 来源: {l1_details.get('l1_decision_source')} | 详情: {l1_details}"
                )

                # 把 L1 确定性证据作为规范化 feedback 推回 LangSmith（可量化断言）
                try:
                    from swarm.tracing import push_l1_feedback
                    push_l1_feedback(l1_details, l1_passed=l1_passed)
                except Exception:  # noqa: BLE001
                    pass

                if l1_passed:
                    break

                if fix_round < self.max_fix_rounds:
                    self._log(f"修复尝试 {fix_round + 1}/{self.max_fix_rounds}")
                    fix_result = await self._run_agent(
                        self._build_fix_prompt(verify_result, l1_details),
                        step=f"fix-{fix_round}",
                    )
                    self._log(f"修复完成: {fix_result[:200]}")

                if self._check_timeout():
                    self._log("验证阶段超时")
                    break

            # ── Phase 4: 产出 ──
            self.phase = WorkerPhase.PRODUCING
            self._log("产出阶段：从沙箱 pull-back 并收集 diff")
            await self._sync_from_sandbox("产出")
            produce_result = await self._run_agent(
                self._build_produce_prompt(),
                step="produce",
            )

            output = self._parse_produce_result(produce_result, l1_passed, l1_details)

            # ── Phase 4 最终复核：与 Phase3 循环(L374)、trivial 通道(L1121)同源 ──
            # 关键修复(task 37460a5b)：此处过去裸调 run_l1_pipeline()，绕过了
            # _deterministic_l1_gate 的 "empty_diff + expects_changes → False" 拦截，
            # 导致占位/空 diff 被 run_l1_pipeline 当 "no diff changes" 返回 True → 翻盘为通过。
            # 现统一走确定性闸门拿三态，再以 LLM 自检作为 Phase4 增值，杜绝 "skip 当 pass"。
            if self.project_path:
                det_ok, det_details = self._deterministic_l1_gate()
                l1_details = {**l1_details, **det_details, "deterministic_l1": det_ok,
                              "l1_phase": "phase4_final"}

                if det_ok is False:
                    # 确定性闸门判失败（空 diff 但期望变更 / 编译失败 / scope 违规）→ 禁止翻盘。
                    l1_passed = False
                    self._log(
                        "L1 最终复核（Phase4 产出后）: 未通过 ❌ — 确定性闸门判失败，"
                        f"不予翻盘 | {det_details.get('reason') or det_details.get('deterministic_gate')}"
                    )
                elif det_ok is True:
                    # 确定性闸门通过。Phase4 增值：再跑一次带 LLM 自检的 pipeline 做最终复核
                    # （仅当 diff 真有可解析变更时；纯占位 diff 不会到这分支，已被闸门拦在 False/None）。
                    llm_ok = True
                    if output.diff:
                        from swarm.worker.l1_pipeline import run_l1_pipeline

                        l1_llm = None
                        try:
                            from swarm.models.router import ModelRouter
                            l1_llm = ModelRouter().get_worker_llm(strategy="cost_optimized")
                        except Exception as exc:  # noqa: BLE001
                            self._log(f"L1 自检 LLM 获取失败，跳过自检: {exc}")
                        llm_ok, llm_details = run_l1_pipeline(
                            self.project_path, self.subtask, output.diff, llm=l1_llm,
                        )
                        l1_details = {**l1_details, **llm_details, "l1_phase": "phase4_final_with_llm"}
                    if not llm_ok:
                        l1_passed = False
                        self._log("L1 最终复核（Phase4 产出后）: 未通过 ❌ — 带 LLM 自检的 pipeline 判失败")
                    elif not l1_passed:
                        # Phase3 循环内 fail（如中途无 diff/拒答），但 pull-back 后收集到【有效】
                        # 产出且确定性闸门 + pipeline 双双通过 → 翻盘为通过。空 diff 已被上面
                        # det_ok is False/None 两条分支拦住，到不了这里。
                        l1_passed = True
                        self._log(
                            "L1 最终复核（Phase4 产出后）: 翻盘为通过 ✅ — "
                            "循环内未通过多因中途无 diff，pull-back 后收集到有效产出且确定性+LLM 校验通过"
                        )
                    else:
                        self._log("L1 最终复核（Phase4 产出后）: 维持通过 ✅")
                else:
                    # det_ok is None：无 diff 可检且无 harness 可执行检查 → 回退 LLM 信号。
                    # 此时【不主动翻盘】：循环内若已 fail，缺乏确定性证据不足以翻盘为通过。
                    l1_details["l1_decision_source"] = "llm_self_report"
                    self._log(
                        f"L1 最终复核（Phase4 产出后）: 无确定性证据(det=None)，维持循环内结论 "
                        f"{'通过 ✅' if l1_passed else '未通过 ❌'}（不主动翻盘）"
                    )
                output = output.model_copy(update={"l1_passed": l1_passed, "l1_details": l1_details})

            # ── DEBUG 意图专属 L1 校验：验证 failing_test_command 修复后通过 ──
            if self.subtask.intent == "debug" and self.project_path:
                harness = getattr(self.subtask, "harness", None)
                failing_cmd = getattr(harness, "failing_test_command", "") if harness else ""
                if failing_cmd:
                    self._log(f"DEBUG L1: 执行 failing_test_command 验证修复: {failing_cmd}")
                    debug_l1_ok, debug_l1_detail = self._run_failing_test_gate(failing_cmd)
                    l1_details["debug_failing_test_command"] = failing_cmd
                    l1_details["debug_failing_test_passed"] = debug_l1_ok
                    l1_details["debug_failing_test_detail"] = debug_l1_detail
                    if not debug_l1_ok:
                        l1_passed = False
                        output = output.model_copy(
                            update={"l1_passed": False, "l1_details": l1_details}
                        )
                        self._log(
                            f"DEBUG L1: failing_test_command 仍失败，判定为未修复 ❌ | {debug_l1_detail}"
                        )
                    else:
                        self._log("DEBUG L1: failing_test_command 通过，bug 已修复 ✅")
                else:
                    self._log("DEBUG L1: harness.failing_test_command 为空，跳过专属校验")
            # 非 DEBUG 意图完全不受影响

            self.phase = WorkerPhase.DONE
            self._log(f"执行完成，置信度: {output.confidence.value}")

            # 记录 L1 结果供 kill_sandbox 决定是否归还热池复用（脏沙箱不回池）
            self._l1_passed_flag = bool(getattr(output, "l1_passed", False))

            return output

        except Exception as e:
            self.phase = WorkerPhase.FAILED
            self._log(f"执行异常: {e}")
            self._l1_passed_flag = False  # 异常路径：沙箱可能脏，不归还复用
            # P2：分类失败类型，供 handle_failure 决定退避重试(transient) 还是换模型(capability)。
            from swarm.models.errors import classify_failure
            failure_class = classify_failure(e)
            return self._make_output(
                diff="",
                summary=f"执行异常: {e}",
                confidence=Confidence.LOW,
                l1_passed=False,
                l1_details={"error": str(e), "failure_class": failure_class},
            )
        finally:
            clear_sandbox_context()
            clear_scope()
            clear_extra_whitelist()
            self.kill_sandbox()
            elapsed = time.monotonic() - self.start_time
            self._log(f"总执行时间: {elapsed:.1f}s")

    # ──────────────────────────────────────────
    # 内部方法
    # ──────────────────────────────────────────

    def _create_agent(self) -> dict:
        """创建 Worker Agent（延迟导入避免循环依赖）"""
        from swarm.knowledge.service import set_worker_context
        from swarm.worker.agent import create_worker_agent

        set_worker_context(self.project_id)
        return create_worker_agent(
            subtask=self.subtask,
            scope=self.effective_scope,
            model_name=self.model_name,
            model_strategy=self.model_strategy,
            knowledge=self.knowledge,
            project_id=self.project_id,
            user_profile_prompt=self.user_profile_prompt,
            shared_contract=self.shared_contract,
        )

    def _scope_files(self) -> list[str]:
        """上传到沙箱的文件清单（readable ∪ writable(modify) ∪ 构建清单，去重保序）。

        注意：排除 create_files——它们是【待新建】文件，本地不存在，强行 read/upload
        会 FileNotFoundError（曾导致 worker 把"新建 readme"当成读取不存在文件而卡住）。
        delete_files 也不上传（要删的没必要传）。

        关键：必须额外带上【构建清单文件】(pom.xml/build.gradle/go.mod/Cargo.toml/
        package.json 等)，否则 mvn/gradle/go build/cargo 在沙箱里因找不到工程描述
        文件而失败（实测 RuoYi: "no POM in /workspace"）。
        """
        scope = self.effective_scope
        files: list[str] = []
        create = set(getattr(scope, "create_files", []) or [])
        delete = set(getattr(scope, "delete_files", []) or [])
        for f in list(getattr(scope, "readable", []) or []) + list(getattr(scope, "writable", []) or []):
            rel = str(f).strip()
            if rel and rel not in files and rel not in create and rel not in delete:
                files.append(rel)
        # 追加构建清单（沙箱编译/测试的前提）
        for rel in self._build_manifest_files():
            if rel not in files and rel not in create and rel not in delete:
                files.append(rel)
        # 追加【改动所在模块的完整源码树】——仅当 harness 需真实编译时。
        # 精准 scope 同步只传选中文件，但 mvn/gradle 编译整模块会因缺同级类
        # (DateUtils 依赖 Constants/StringUtils 等)报 cannot find symbol 秒挂。
        # 编译型语言必须带齐改动模块的全部源码。
        for rel in self._module_source_files():
            if rel not in files and rel not in create and rel not in delete:
                files.append(rel)
        return files

    def _module_source_files(self) -> list[str]:
        """改动文件所在【构建模块】的完整源码树(仅编译型语言需要)。

        判据：harness.build_command 存在(说明要真实编译)。从改动文件向上找最近的
        构建清单(pom.xml/build.gradle)确定模块根，再收该模块 src/ 下全部源文件。
        防超大：单模块上限 800 文件。非编译型(无 build_command)返回空，保持精准同步。
        """
        harness = getattr(self.subtask, "harness", None)
        build_cmd = getattr(harness, "build_command", "") if harness else ""
        if not build_cmd or not self.project_path:
            return []
        # 仅对 JVM 系(mvn/gradle)启用整模块同步；其他语言模块边界不同，暂不扩展
        if not any(t in build_cmd for t in ("mvn", "gradle")):
            return []
        root = Path(self.project_path).resolve()
        scope = self.effective_scope
        changed = (list(getattr(scope, "writable", []) or [])
                   + list(getattr(scope, "create_files", []) or [])
                   + list(getattr(scope, "readable", []) or []))
        module_roots: set[Path] = set()
        for f in changed:
            cur = (root / str(f).strip()).resolve().parent
            # 向上找最近含 pom.xml/build.gradle 的目录 = 模块根
            while True:
                if (cur / "pom.xml").is_file() or (cur / "build.gradle").is_file() or (cur / "build.gradle.kts").is_file():
                    module_roots.add(cur)
                    break
                if cur == root or root not in cur.parents:
                    break
                cur = cur.parent
        out: list[str] = []
        _SRC_EXT = (".java", ".kt", ".scala", ".groovy")
        for mroot in module_roots:
            src_dir = mroot / "src"
            base = src_dir if src_dir.is_dir() else mroot
            count = 0
            for p in base.rglob("*"):
                if count >= 800:
                    break
                if not p.is_file() or p.suffix not in _SRC_EXT:
                    continue
                if "target" in p.relative_to(root).parts or "build" in p.relative_to(root).parts:
                    continue
                try:
                    out.append(str(p.relative_to(root)))
                    count += 1
                except ValueError:
                    continue
        return out

    def _build_manifest_files(self) -> list[str]:
        """发现项目里的构建清单文件，确保沙箱编译有工程描述。

        覆盖 5 语言主流构建系统。从【已 scope 的文件路径】向上回溯各级目录找清单
        (多模块项目根 + 模块各有 pom.xml)，再补项目根的清单。只返回真实存在的相对路径。
        """
        if not self.project_path:
            return []
        root = Path(self.project_path).resolve()
        manifest_names = (
            "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
            "settings.gradle.kts", "go.mod", "go.sum", "Cargo.toml", "Cargo.lock",
            "package.json", "tsconfig.json", "pyproject.toml", "setup.py",
            "requirements.txt", "build.xml",
        )
        found: list[str] = []

        def _add_dir_manifests(d: Path) -> None:
            for name in manifest_names:
                p = d / name
                if p.is_file():
                    try:
                        rel = str(p.relative_to(root))
                    except ValueError:
                        continue
                    if rel not in found:
                        found.append(rel)

        # 1) 项目根清单（多模块工程的父 pom / 聚合构建）
        _add_dir_manifests(root)
        # 2) 沿已 scope 文件向上回溯到根，收集每级目录的清单（覆盖各子模块）
        scope = self.effective_scope
        scoped = (list(getattr(scope, "readable", []) or [])
                  + list(getattr(scope, "writable", []) or [])
                  + list(getattr(scope, "create_files", []) or []))
        seen_dirs: set[Path] = set()
        for f in scoped:
            try:
                cur = (root / str(f).strip()).resolve().parent
            except (OSError, ValueError):
                continue
            # 向上回溯到 root（含中间各级模块目录）
            while True:
                if cur in seen_dirs:
                    break
                seen_dirs.add(cur)
                if root not in cur.parents and cur != root:
                    break
                _add_dir_manifests(cur)
                if cur == root:
                    break
                cur = cur.parent
        # 3) 多模块工程：聚合父 pom 会引用【所有】子模块，mvn -pl/聚合构建需要全部
        #    模块的构建清单在场。项目级 glob 收集所有清单(限 60 个，防超大 monorepo)。
        #    只收清单文件本身(小)，不碰源码，开销可忽略。
        _SKIP = {".git", "node_modules", "target", "build", ".venv", "venv",
                 "dist", ".gradle", "__pycache__", ".codegraph"}
        manifest_set = set(manifest_names)
        count = 0
        for p in root.rglob("*"):
            if count >= 60:
                break
            if p.name not in manifest_set or not p.is_file():
                continue
            if any(part in _SKIP for part in p.relative_to(root).parts):
                continue
            try:
                rel = str(p.relative_to(root))
            except ValueError:
                continue
            if rel not in found:
                found.append(rel)
                count += 1
        return found


    def _writable_files(self) -> list[str]:
        """pull-back 范围：可修改文件 + 新建文件（都要从沙箱拉回）；不含删除文件。"""
        out: list[str] = []
        scope = self.effective_scope
        for f in list(getattr(scope, "writable", []) or []) + list(getattr(scope, "create_files", []) or []):
            rel = str(f).strip()
            if rel and rel not in out:
                out.append(rel)
        return out

    @staticmethod
    def _norm_rel(local_root: Path, f: str) -> str:
        """把 scope 里的文件路径归一化为相对 local_root 的 posix 路径。"""
        p = Path(f)
        if p.is_absolute():
            try:
                return p.resolve().relative_to(local_root).as_posix()
            except ValueError:
                return p.name  # 越界则退化为文件名
        return p.as_posix().lstrip("/")

    def _git_baseline_text(self, local_root: Path, rel: str) -> str | None:
        """从 git HEAD 读取文件的提交版作为 diff 基线（防本地工作副本被前序运行污染）。

        sandbox-first 模式下，前一个相同任务的 pull-back 会把改动写回本地工作副本，
        导致下次运行的 _pre_sync_contents 基线already含该改动 → diff 空 → 误判"无变更"
        → 重试死循环(实测 c592c562 连跑 3 次 diff 均为"(无变更)")。
        用 git HEAD 的提交版做基线，diff 永远相对干净的已提交状态，杜绝污染。
        返回 None 表示无法用 git(非 git 仓库/文件未跟踪)，调用方回退本地工作副本。
        """
        try:
            import subprocess
            proc = subprocess.run(
                ["git", "show", f"HEAD:{rel}"],
                cwd=str(local_root), capture_output=True, text=True, timeout=15,
            )
            if proc.returncode == 0:
                return proc.stdout
            # 文件在 HEAD 不存在(新建文件)→ 基线为空串
            if "exists on disk, but not in" in proc.stderr or "does not exist" in proc.stderr or "fatal: path" in proc.stderr:
                return ""
            return None
        except Exception:
            return None

    def _snapshot_scope_local(
        self, local_root: Path, files: list[str] | None = None
    ) -> dict[str, str | None]:
        """读取本地文件内容快照，作为 difflib diff 的基线/产出。

        files 为 None 时用 writable scope（diff 只关心可写文件的前后变化）。
        值为文件文本；不存在的文件记为空串；二进制/不可读记为 None。

        基线优先用 git HEAD 提交版(防前序运行 pull-back 污染本地工作副本)；
        git 不可用时回退本地工作副本。仅在 baseline 模式(files is None)下用 git。
        """
        use_git_baseline = files is None  # 只有基线快照需要防污染；产出快照读真实本地
        rel_files = [
            self._norm_rel(local_root, f)
            for f in (files if files is not None else self._writable_files())
        ]
        snapshot: dict[str, str | None] = {}
        for rel in rel_files:
            lp = local_root / rel
            if use_git_baseline:
                git_text = self._git_baseline_text(local_root, rel)
                if git_text is not None:
                    snapshot[rel] = git_text
                    continue
            try:
                snapshot[rel] = lp.read_text("utf-8") if lp.is_file() else ""
            except (UnicodeDecodeError, OSError):
                snapshot[rel] = None
        return snapshot

    def _reset_scope_to_head(self) -> int:
        """批次2-B（Bug：跨任务/重试 workspace 累积脏）：子任务起点把本 scope 内的
        git【跟踪】文件 reset 到 HEAD，杜绝上一轮 pull-back 写回的改动累积叠加。

        - 只 reset writable ∪ scope 内【被 git 跟踪】的文件（git ls-files 白名单）；
          untracked / 新建产物（create_files 尚未提交）一律不碰，零误删风险。
        - per-project 文件锁（fcntl.flock）串行化：并发子任务共享同一 project_path 时，
          同一时刻只有一个 executor 在 reset（dispatch 用 asyncio.gather 真并发，
          scope 可能重叠，见 task 0f93f1fc）。reset 是毫秒级 git 操作，串行无实质开销。
        - SWARM_WORKER_RESET_SCOPE=false 可回退旧行为（默认 true）。
        - 非 git 仓库 / 无 project_path 优雅跳过，返回 0。

        返回被 reset 的文件数。
        """
        import subprocess

        if os.environ.get("SWARM_WORKER_RESET_SCOPE", "true").lower() in ("false", "0", "no"):
            return 0
        if not self.project_path:
            return 0
        local_root = Path(self.project_path).resolve()
        if not (local_root / ".git").exists():
            return 0

        # 候选：writable ∪ scope 文件（modify 意图的文件；create_files 新建产物在
        # _writable_files 里，但只有【已被 git 跟踪】的才 reset）
        candidates = set()
        for f in self._writable_files():
            candidates.add(self._norm_rel(local_root, f))
        for f in self._scope_files():
            candidates.add(self._norm_rel(local_root, f))
        if not candidates:
            return 0

        # 只保留 git 跟踪的文件（ls-files --error-unmatch 逐个判定）
        tracked = []
        for rel in candidates:
            try:
                r = subprocess.run(
                    ["git", "ls-files", "--error-unmatch", rel],
                    cwd=str(local_root), capture_output=True, text=True, timeout=10,
                )
                if r.returncode == 0:
                    tracked.append(rel)
            except Exception:  # noqa: BLE001
                continue
        if not tracked:
            return 0

        # 锁文件放系统临时目录（按 project_path 派生稳定名），不污染目标 workdir
        # （避免 .swarm_reset.lock 出现在用户项目的 git status）。
        import hashlib
        import tempfile as _tf
        _proj_hash = hashlib.sha1(str(local_root).encode()).hexdigest()[:16]
        lock_path = Path(_tf.gettempdir()) / f"swarm_reset_{_proj_hash}.lock"
        try:
            import fcntl
            lock_f = open(lock_path, "w")
        except Exception:  # noqa: BLE001
            lock_f = None
        try:
            if lock_f is not None:
                try:
                    fcntl.flock(lock_f, fcntl.LOCK_EX)
                except Exception:  # noqa: BLE001
                    pass
            r = subprocess.run(
                ["git", "checkout", "HEAD", "--", *tracked],
                cwd=str(local_root), capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                self._log(f"bootstrap 前 workspace reset：{len(tracked)} 个 tracked 文件恢复到 HEAD（防跨轮脏叠加）")
                return len(tracked)
            self._log(f"workspace reset 警告（git checkout 非零）: {r.stderr.strip()[:200]}")
            return 0
        except Exception as exc:  # noqa: BLE001
            self._log(f"workspace reset 跳过（异常）: {exc}")
            return 0
        finally:
            if lock_f is not None:
                try:
                    fcntl.flock(lock_f, fcntl.LOCK_UN)
                    lock_f.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _sync_to_sandbox(self, reason: str) -> None:
        """精准上传：只把子任务 scope 内的文件推送到沙箱 /workspace。

        同时保存上传前内容快照 self._pre_sync_contents，供 difflib 生成 diff。
        本地执行模式（无沙箱）下仅记录本地快照作为 diff 基线。
        """
        local_root = Path(self.project_path).resolve()
        self._pre_sync_contents = self._snapshot_scope_local(local_root)
        if not self._sandbox or not self._sandbox_manager:
            return
        cfg = get_config()

        # 上传范围：
        # - 项目专属沙箱（镜像自带完整源码，方案 B）→ 只传被改的 writable/create_files；
        #   readable 镜像已有，传了反而可能用本地覆盖镜像基线（且浪费 I/O）。
        # - 通用池沙箱（/workspace 空）→ 传完整 scope_files（readable ∪ writable ∪ 构建清单），
        #   否则编译找不到依赖源文件/pom。
        if getattr(self, "_sandbox_has_source", False):
            rel_files = [self._norm_rel(local_root, f) for f in self._writable_files()]
            self._log(f"{reason} 专属沙箱自带源码 → 仅上传 {len(rel_files)} 个改动文件（writable/create）")
        else:
            rel_files = [self._norm_rel(local_root, f) for f in self._scope_files()]
        if not rel_files:
            self._log(f"{reason} scope 为空，跳过文件上传（无目标文件）")
            return

        # 记录上传前内容快照（用于 diff 基线）。writable 文件的基线已由
        # _snapshot_scope_local 用 git HEAD 填好(防污染)，这里【不覆盖】已有键，
        # 只为额外的 scope 文件(readable/构建清单)补基线，且同样优先 git。
        for rel in rel_files:
            if rel in self._pre_sync_contents:
                continue  # 已有 git 基线，绝不用本地工作副本覆盖(否则前序污染复现)
            git_text = self._git_baseline_text(local_root, rel)
            if git_text is not None:
                self._pre_sync_contents[rel] = git_text
                continue
            lp = (local_root / rel)
            try:
                self._pre_sync_contents[rel] = lp.read_text("utf-8") if lp.is_file() else ""
            except (UnicodeDecodeError, OSError):
                self._pre_sync_contents[rel] = None  # 二进制/不可读

        # ── 批次2-A（Bug：跨重试改动叠加）：上传 git HEAD 内容而非脏磁盘 ──
        # 根因：上一个 executor 的 pull-back 把改动写回本地 project_path，重新派发时
        # bootstrap 上传脏文件 → LLM 在脏版本上叠加修改（docstring 重复等）。
        # _git_baseline_text 此前只兜住了 diff 基线，没兜上传内容（半个修复）。
        # 这里把 writable(modify) 中【git 跟踪】的文件改用 HEAD 版写入临时 staging
        # 目录上传；untracked/新建/readable 仍从真实磁盘上传（HEAD 取不到）。
        # SWARM_WORKER_CLEAN_UPLOAD=false 可回退旧行为（默认 true）。
        import shutil
        import tempfile

        clean_upload = os.environ.get(
            "SWARM_WORKER_CLEAN_UPLOAD", "true"
        ).lower() not in ("false", "0", "no")
        writable_set = {self._norm_rel(local_root, f) for f in self._writable_files()}
        upload_root = local_root
        staging_dir: str | None = None
        if clean_upload:
            import subprocess as _sp
            try:
                staging_dir = tempfile.mkdtemp(prefix="swarm_clean_upload_")
                staging_root = Path(staging_dir)
                cleaned = 0
                for rel in rel_files:
                    dst = staging_root / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    # 仅当 rel 是 writable 且【确实被 git 跟踪】时用 HEAD 版。
                    # 用 ls-files 显式判定，区分 "tracked"（含空文件）与 "untracked/新建"
                    # （_git_baseline_text 对两者都返回 ""，无法区分 → 会把新建文件写空）。
                    is_tracked = False
                    if rel in writable_set:
                        try:
                            _r = _sp.run(
                                ["git", "ls-files", "--error-unmatch", rel],
                                cwd=str(local_root), capture_output=True, text=True, timeout=10,
                            )
                            is_tracked = _r.returncode == 0
                        except Exception:  # noqa: BLE001
                            is_tracked = False
                    git_text = self._git_baseline_text(local_root, rel) if is_tracked else None
                    if git_text is not None:
                        # writable 且 git 跟踪 → 用 HEAD 干净版（杜绝脏磁盘叠加）
                        dst.write_text(git_text, encoding="utf-8")
                        cleaned += 1
                    else:
                        # readable / untracked / 新建 → copy 真实磁盘（HEAD 无此版）
                        src = local_root / rel
                        if src.is_file():
                            shutil.copy2(src, dst)
                        # 源不存在（待新建）→ staging 也不建，上传层跳过
                upload_root = staging_root
                if cleaned:
                    self._log(f"{reason} 干净上传：{cleaned} 个 writable 文件用 git HEAD 版上传（防脏叠加）")
            except Exception as stage_exc:  # noqa: BLE001
                self._log(f"{reason} staging 构造失败，回退脏磁盘上传: {stage_exc}")
                upload_root = local_root
                if staging_dir:
                    shutil.rmtree(staging_dir, ignore_errors=True)
                    staging_dir = None

        try:
            sync_stats = await asyncio.to_thread(
                self._sandbox_manager.sync_files_to_sandbox,
                self._sandbox,
                upload_root,
                rel_files,
                cfg.sandbox.sandbox_remote_workdir,
            )
            err_count = len(sync_stats.get("errors") or [])
            self._log(
                f"{reason} 本地→沙箱精准上传: "
                f"uploaded={sync_stats.get('uploaded', 0)}, "
                f"errors={err_count}, files={sync_stats.get('files')}"
            )
            for err in (sync_stats.get("errors") or [])[:5]:
                self._log(f"上传警告: {err}")
        except Exception as sync_exc:
            self._log(f"{reason} 本地→沙箱精准上传失败: {sync_exc}")
        finally:
            if staging_dir:
                shutil.rmtree(staging_dir, ignore_errors=True)

    async def _sync_from_sandbox(self, reason: str) -> None:
        """精准拉回：只把子任务可写文件从沙箱拉回本地 project_path。

        拉回内容存入 self._post_sync_contents，供 difflib 生成 diff。
        本地执行模式（无沙箱）下读取本地 writable 文件当前内容作为产出快照
        （agent 已直接改本地文件），从而本地模式也能正确产出 diff。
        """
        local_root = Path(self.project_path).resolve()
        if not self._sandbox or not self._sandbox_manager:
            # 本地模式：直接快照本地 writable 文件（agent 已就地修改）
            self._post_sync_contents = self._snapshot_scope_local(
                local_root, files=self._writable_files()
            )
            return
        self._post_sync_contents = {}
        cfg = get_config()
        rel_files = [self._norm_rel(local_root, f) for f in self._writable_files()]
        # greenfield/allow_any 模式：scope 没有预设文件，worker 自由创建。
        # 列出沙箱 workspace 实际文件作为 pull-back 清单，否则新建文件拉不回来。
        if not rel_files and getattr(self.effective_scope, "allow_any", False):
            try:
                rel_files = await asyncio.to_thread(self._list_sandbox_workspace_files)
                self._log(f"{reason} allow_any 模式：枚举沙箱产物 {len(rel_files)} 个文件")
            except Exception as exc:
                self._log(f"{reason} allow_any 枚举沙箱文件失败: {exc}")
        if not rel_files:
            self._log(f"{reason} 无可写文件，跳过 pull-back")
            return
        try:
            sync_stats = await asyncio.to_thread(
                self._sandbox_manager.sync_files_from_sandbox,
                self._sandbox,
                local_root,
                rel_files,
                cfg.sandbox.sandbox_remote_workdir,
            )
            self._post_sync_contents = sync_stats.get("contents") or {}
            err_count = len(sync_stats.get("errors") or [])
            self._log(
                f"{reason} 沙箱→本地精准 pull-back: "
                f"downloaded={sync_stats.get('downloaded', 0)}, "
                f"errors={err_count}"
            )
            for err in (sync_stats.get("errors") or [])[:5]:
                self._log(f"pull-back 警告: {err}")
        except Exception as sync_exc:
            self._log(f"{reason} 沙箱→本地 pull-back 失败: {sync_exc}")

    def _list_sandbox_workspace_files(self) -> list[str]:
        """递归列出沙箱 /workspace 下的相对文件路径（allow_any/greenfield pull-back 用）。

        走 shell 端点(run_command + find)——不依赖 Jupyter kernel(自建语言镜像无
        kernel 会 502)。过滤常见噪声目录，返回相对 remote_workdir 的路径(上限 200)。
        """
        if not self._sandbox or not self._sandbox_manager:
            return []
        cfg = get_config()
        remote = cfg.sandbox.sandbox_remote_workdir
        # find 排除噪声目录 + 限 2MB；-printf 输出相对路径
        prune = r"\( -name .git -o -name __pycache__ -o -name node_modules -o -name .venv -o -name venv -o -name .codegraph -o -name dist -o -name build -o -name .pytest_cache \)"
        cmd = (
            f"cd {remote} 2>/dev/null && "
            f"find . {prune} -prune -o -type f -size -2000k -print 2>/dev/null "
            f"| sed 's|^\\./||' | head -200"
        )
        rc = getattr(self._sandbox_manager, "run_command", None)
        if rc is None:
            return []
        result = rc(self._sandbox, cmd, timeout=30)
        if getattr(result, "error", None) and not getattr(result, "stdout", ""):
            return []
        out = (result.stdout or "").strip()
        if not out:
            return []
        return [line.strip() for line in out.splitlines() if line.strip()]

    def _get_git_diff(self) -> str:
        """用 difflib 对比上传前快照与拉回后内容生成 unified diff（不依赖 git）。

        基线 = _sync_to_sandbox 保存的 _pre_sync_contents；
        新值 = _sync_from_sandbox 拉回的 _post_sync_contents。
        二进制文件（值为 None）仅标注变更，不产 diff 文本。
        """
        import difflib

        pre = getattr(self, "_pre_sync_contents", None) or {}
        post = getattr(self, "_post_sync_contents", None) or {}

        if not post:
            return "(无变更)"

        diff_parts: list[str] = []
        for rel in sorted(post.keys()):
            new_text = post.get(rel)
            old_text = pre.get(rel, "")
            # 二进制文件
            if new_text is None or old_text is None:
                if new_text != old_text:
                    diff_parts.append(f"二进制文件变更: {rel}")
                continue
            # 行尾归一化：基线(git HEAD/本地)可能是 LF，pull-back 回来可能是 CRLF
            # (RuoYi 原始文件即 Windows CRLF)。不归一会让 difflib 把每行都判为变更，
            # 产出整文件 churn 的垃圾 diff(实测 StringUtils 862 行全变 44KB)，淹没真实改动。
            old_norm = old_text.replace("\r\n", "\n").replace("\r", "\n")
            new_norm = new_text.replace("\r\n", "\n").replace("\r", "\n")
            if old_norm == new_norm:
                continue
            old_lines = old_norm.splitlines(keepends=True)
            new_lines = new_norm.splitlines(keepends=True)
            ud = difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                lineterm="",
            )
            block = "\n".join(ud)
            if block.strip():
                diff_parts.append(block)

        if not diff_parts:
            return "(无变更)"
        return "\n".join(diff_parts)

    def create_sandbox(self) -> Any:
        """创建远程 CubeSandbox 实例（用于沙箱内代码执行和编译验证）"""
        from swarm.worker.sandbox import get_sandbox_manager

        manager = get_sandbox_manager()
        # 传 task_id/project_id，使 cancel_task 能按任务 kill_by_task 释放资源
        sandbox = manager.create(
            project_id=self.project_id,
            task_id=self.task_id,
            source="worker",
        )
        self._sandbox = sandbox
        self._sandbox_manager = manager
        self._log(f"远程沙箱已创建: {sandbox.sandbox_id}")
        return sandbox

    def run_in_sandbox(self, code: str, timeout: int = 30) -> str:
        """在远程沙箱中执行 Python 代码并返回输出"""
        if not hasattr(self, "_sandbox") or self._sandbox is None:
            self.create_sandbox()
        result = self._sandbox_manager.run_code(self._sandbox, code, timeout)
        output_parts = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.text:
            output_parts.append(f"→ {result.text}")
        if result.stderr:
            output_parts.append(f"STDERR: {result.stderr}")
        if result.error:
            output_parts.append(f"ERROR: {result.error}")
        return "\n".join(output_parts)

    def kill_sandbox(self) -> None:
        """释放远程沙箱：热池借来的归还(健康则回池)，否则销毁。"""
        if hasattr(self, "_sandbox") and self._sandbox is not None:
            from_pool = getattr(self, "_from_pool", False)
            pool = getattr(self, "_sandbox_pool", None)
            sid = self._sandbox.sandbox_id
            try:
                if from_pool and pool is not None:
                    # 归还：L1 通过/无异常的沙箱可复用；失败的不回池(可能脏)
                    reusable = bool(getattr(self, "_l1_passed_flag", True))
                    pool.release(self._sandbox, reusable=reusable)
                    self._log(f"远程沙箱已归还热池(reusable={reusable}): {sid}")
                else:
                    self._sandbox_manager.kill(sid)
                    self._log(f"远程沙箱已销毁: {sid}")
            except Exception as e:
                self._log(f"沙箱释放失败: {e}")
            self._sandbox = None
            self._sandbox_manager = None
            self._sandbox_pool = None

    def _remaining_seconds(self) -> float:
        if not self.start_time:
            return float(self.max_execution_time)
        return max(0.0, self.max_execution_time - (time.monotonic() - self.start_time))

    async def _run_agent(self, human_message: str, *, step: str = "react") -> str:
        """调用 Agent 执行一步并返回结果（受总执行时间预算约束）"""
        if self._agent is None:
            return "❌ Agent 未创建"

        remaining = self._remaining_seconds()
        if remaining <= 1:
            return f"❌ 执行超时（预算 {self.max_execution_time}s 已用尽）"

        from swarm.tracing import merge_invoke_config, worker_agent_config

        agent = self._agent["agent"]
        source = "dispatch" if self.task_id else "standalone"
        trace_cfg = worker_agent_config(
            run_id=self.subtask.id,
            project_id=self.project_id,
            task_id=self.task_id,
            subtask_id=self.subtask.id,
            difficulty=self.subtask.difficulty.value
            if hasattr(self.subtask.difficulty, "value")
            else str(self.subtask.difficulty),
            worker_phase=self.phase.value,
            step=step,
            source=source,
        )
        invoke_config = merge_invoke_config(
            {"recursion_limit": self.max_iterations},
            trace_cfg,
        )
        try:
            result = await asyncio.wait_for(
                agent.ainvoke(
                    {"messages": [("human", human_message)]},
                    config=invoke_config,
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            self._log(f"Agent 调用超时（剩余预算 {remaining:.0f}s）")
            return f"❌ Agent 调用超时（预算 {self.max_execution_time}s）"
        except Exception as exc:
            # GraphRecursionError 等：agent 撞迭代上限。它在沙箱里已做的改动仍有效，
            # 不当作硬失败——后续 pull-back + 确定性 L1 闸门会按真实文件状态裁决
            # (与"子代理撞步数上限但已产出部分工作"同理，让确定性验证说话)。
            cls = type(exc).__name__
            if "Recursion" in cls or "recursion" in str(exc).lower():
                self._log(f"Agent 撞迭代上限({self.max_iterations})，以沙箱实际产出为准交确定性闸门裁决")
                return f"⚠️ Agent 达到迭代上限（{self.max_iterations}），已做改动交由确定性 L1 验证"
            raise

        # 提取最后一条 AI 消息
        messages = result.get("messages", [])
        if messages:
            last = messages[-1]
            return getattr(last, "content", str(last))
        return "(Agent 无输出)"

    def _scope_ops_hint(self) -> str:
        """生成给 LLM 的文件操作清单：明确哪些是修改/新建/删除。"""
        s = self.effective_scope
        if getattr(s, "allow_any", False) and not (
            getattr(s, "writable", []) or getattr(s, "create_files", []) or getattr(s, "delete_files", [])
        ):
            return (
                "【自由创建模式】这是一个从零开始/开放式任务，没有预设文件清单。"
                "你可以根据需求自由用 write_file 创建任意需要的文件（如源码、README、配置等），"
                "用 run_command 建目录/跑命令。请规划合理的项目结构并实现完整功能。"
            )
        modify = list(getattr(s, "writable", []) or [])
        create = list(getattr(s, "create_files", []) or [])
        delete = list(getattr(s, "delete_files", []) or [])
        readable = [f for f in (getattr(s, "readable", []) or []) if f not in modify + create + delete]
        lines = []
        if modify:
            lines.append(f"【修改现有文件】{', '.join(modify)} — 先 read_file 读取，再 patch_file/write_file 改动")
        if create:
            lines.append(f"【新建文件】{', '.join(create)} — 不要 read_file（文件还不存在），直接 write_file 写入完整内容")
        if delete:
            lines.append(f"【删除文件】{', '.join(delete)} — 用 run_command 执行 rm 删除")
        if readable:
            lines.append(f"【只读参考】{', '.join(readable)} — 仅供理解上下文，不要修改")
        return "\n".join(lines) if lines else "见 scope（无显式文件清单，请先用工具探查项目结构）"

    async def _run_trivial_fast(self) -> WorkerOutput:
        """trivial 子任务快速路径：合并定位+编码，最小 L1，快速产出"""
        self.phase = WorkerPhase.CODING
        self._log("trivial 快速路径：合并定位与编码")
        combined = await self._run_agent(
            "这是 trivial 简单子任务，请一次完成：\n"
            f"任务：{self.subtask.description}\n\n"
            "文件操作清单（务必按操作类型处理）：\n"
            f"{self._scope_ops_hint()}\n\n"
            "执行步骤：\n"
            "1. 对【修改】文件：read_file 读取后 patch_file 做最小必要改动\n"
            "2. 对【新建】文件：直接 write_file 写入完整内容（切勿先 read_file）\n"
            "3. 对【删除】文件：run_command 执行 rm\n"
            "4. 若涉及 Python 文件，run_command 执行 python -m py_compile 验证语法\n"
            "完成后简要说明你做了哪些改动。",
            step="trivial-combined",
        )
        self._log(f"合并执行完成: {combined[:200]}")

        # LLM 自报仅作弱信号（仅在确定性闸门无法判定时回退使用）。audit #22 见
        # _trivial_llm_self_report_passed。
        llm_passed = _trivial_llm_self_report_passed(combined)
        l1_details = {"mode": "trivial_fast", "agent_summary": combined[:500]}

        # Bug-4 根治：agent 主回复是拒答/截断标记 → worker 没真正完成，产出不可信，
        # 硬否决整个 L1（覆盖 deterministic gate）。否则沙箱里残留/部分编辑导致 diff
        # 非空 + compile 恰好过会翻盘判通过（task 0f93f1fc：st-1-1 "Sorry need more
        # steps" 却 L1=通过）。
        if _is_refusal_or_truncated(combined):
            l1_details["l1_decision_source"] = "refusal_hard_fail"
            l1_details["raw_refusal"] = combined[:200]
            self._log("trivial: agent 回复为拒答/截断标记，硬否决 L1（产出不可信，覆盖确定性闸门）")
            self.phase = WorkerPhase.PRODUCING
            await self._sync_from_sandbox("产出")
            produce_result = await self._run_agent(self._build_produce_prompt(), step="produce")
            output = self._parse_produce_result(produce_result, False, l1_details)
            self.phase = WorkerPhase.DONE
            self._l1_passed_flag = False
            self._log(f"trivial 快速路径完成（拒答否决），置信度: {output.confidence.value}")
            return output


        self.phase = WorkerPhase.PRODUCING
        self._log("产出阶段：从沙箱 pull-back 并收集 diff")
        await self._sync_from_sandbox("产出")

        # 确定性 L1 闸门：trivial 路径过去仅靠 LLM 文本里有无 "fail" 判定（纯自报，
        # 曾让 "Sorry, need more steps" 也被判通过）。pull-back 后文件已在本地，
        # 用 harness 真跑 compile/test/verify 覆盖自报，杜绝幻觉 PASS。
        det_ok, det_details = self._deterministic_l1_gate()
        l1_details = {**l1_details, **det_details}
        if det_ok is None:
            l1_passed = llm_passed
            l1_details["l1_decision_source"] = "llm_self_report"
        else:
            l1_passed = det_ok
            l1_details["l1_decision_source"] = "deterministic"
            if not det_ok and llm_passed:
                self._log("trivial: LLM 自报通过但确定性闸门失败，以确定性为准（拦截幻觉 PASS）")
        self._log(
            f"trivial L1: {'通过 ✅' if l1_passed else '未通过 ❌'} "
            f"| 来源: {l1_details.get('l1_decision_source')}"
        )
        try:
            from swarm.tracing import push_l1_feedback
            push_l1_feedback(l1_details, l1_passed=l1_passed)
        except Exception:  # noqa: BLE001
            pass

        produce_result = await self._run_agent(self._build_produce_prompt(), step="produce")
        output = self._parse_produce_result(produce_result, l1_passed, l1_details)
        self.phase = WorkerPhase.DONE
        # 记录 L1 结果供 kill_sandbox 决定 reusable（脏沙箱不回池）。
        # trivial 快速路径直接 return，不经过 run() 末尾的 _l1_passed_flag 赋值，
        # 必须在此显式设置，否则 L1 失败的脏沙箱会以默认 reusable=True 回池污染。
        self._l1_passed_flag = bool(getattr(output, "l1_passed", False))
        self._log(f"trivial 快速路径完成，置信度: {output.confidence.value}")
        return output

    def _build_locate_prompt(self) -> str:
        return (
            "请开始 Phase 1（定位）：\n"
            "1. 阅读你权限范围内的相关文件\n"
            "2. 定位需要修改或实现的代码位置\n"
            "3. 确认接口契约和依赖关系\n"
            "⚠️ 上下文有限：大文件务必用 read_file(path, start_line=N, end_line=M) 只读需要的"
            "行范围，或先 search_files 定位行号再局部读。禁止对大文件无参数读全文（会撑爆上下文）。\n"
            "请简要汇报你的定位结果。"
        )

    def _build_code_prompt(self, locate_result: str) -> str:
        return (
            "请开始 Phase 2（编码）：\n"
            f"定位结果: {locate_result}\n\n"
            "文件操作清单（务必按操作类型处理）：\n"
            f"{self._scope_ops_hint()}\n\n"
            "根据定位结果和子任务要求进行实现：\n"
            "⚠️ 上下文有限：改文件前用 read_file(path, start_line=N, end_line=M) 只读目标行范围，"
            "不要无参数读全文；用 patch_file 做最小必要改动，不要全文重写输出（大文件全文重写会撑爆上下文）。\n"
            "1. 【修改】文件：用 patch_file 在可写范围内改动\n"
            "2. 【新建】文件：用 write_file 直接写入完整内容，不要先 read_file\n"
            "3. 【删除】文件：用 run_command 执行 rm\n"
            "4. 确保修改符合接口契约，保持代码风格一致\n"
            "完成后请确认你做了哪些修改。"
        )

    def _build_verify_prompt(self) -> str:
        return (
            "请开始 Phase 3（L1 验证）：\n"
            "1. 运行编译命令（run_compile），确认无语法错误\n"
            "2. 运行测试（run_tests），确认功能正确\n"
            "请报告验证结果：编译是否通过？测试是否通过？\n"
            "格式：L1_RESULT: PASS 或 L1_RESULT: FAIL，然后说明详情。"
        )

    def _l1_failure_digest(self, l1_details: dict) -> str:
        """从确定性 L1 结果提取【真实失败证据】摘要（已是压缩值，不膨胀 context）。

        I4（Anthropic code-execution/context-engineering 启发）：fix prompt 过去只带 LLM
        自己上轮的 verify_result（自报，可能没说清真正的 compile 错误）。这里改为优先注入
        确定性 pipeline 抓到的真实失败信号（compile_message / lint / build_output，均已被
        compress_tool_output 压到 ≤1500 字符），让修复有的放矢，且因用压缩摘要不灌全量输出。
        """
        if not l1_details:
            return ""
        parts: list[str] = []
        # scope 越权（最高优先，确定性硬失败）
        sv = l1_details.get("scope_violations")
        if sv:
            parts.append(f"[scope 越权] 改了 scope 外的文件: {sv}")
        cm = (l1_details.get("compile_message") or "").strip()
        if cm and not l1_details.get("l1_2_compile_ok", True):
            parts.append(f"[编译失败]\n{cm}")
        lint = l1_details.get("lint") or {}
        if isinstance(lint, dict) and lint.get("message") and lint.get("status") == "error":
            parts.append(f"[lint 失败]\n{str(lint.get('message')).strip()}")
        bo = (l1_details.get("build_output") or "").strip()
        if bo and l1_details.get("l1_2_1_build_ok") is False:
            parts.append(f"[构建失败]\n{bo}")
        reason = l1_details.get("reason")
        if reason and not parts:
            parts.append(f"[确定性闸门] {reason}: {l1_details.get('note', '')}")
        return "\n\n".join(parts).strip()

    def _build_fix_prompt(self, verify_result: str, l1_details: dict | None = None) -> str:
        # I4：优先用确定性失败证据（真实 compile/lint/scope，已压缩），回退 LLM 自报
        digest = self._l1_failure_digest(l1_details or {})
        evidence = digest if digest else verify_result
        return (
            f"L1 验证未通过，确定性失败证据：\n{evidence}\n\n"
            "请分析失败原因并修复代码：\n"
            "1. 仔细阅读上面的错误信息（这是真实的编译/lint/scope 检查结果）\n"
            "2. 定位问题根因\n"
            "3. 使用 patch_file 修复\n"
            "完成后请再次运行验证。"
        )

    def _build_produce_prompt(self) -> str:
        return (
            "请开始 Phase 4（产出）：\n"
            "1. 回顾你刚才用 write_file/patch_file 做的所有改动（系统会自动采集文件 diff，"
            "无需依赖 git；若你想复核可用 read_file 查看最终内容）\n"
            "2. 撰写变更摘要\n"
            "3. 评估你的置信度\n\n"
            "请按以下格式输出：\n"
            "```\n"
            "SUMMARY: (变更摘要)\n"
            "CONFIDENCE: (high/medium/low)\n"
            "NOTES: (需要人工审查的部分，如无则写 无)\n"
            "```"
        )

    def _parse_l1_result(self, verify_result: str) -> tuple[bool, dict]:
        """解析 LLM 自报的 L1 验证结果（弱信号，仅作辅助）。

        注意：LLM 自报易误判（幻觉 PASS / 中文措辞歧义），真正的权威是
        Phase 3 循环内的确定性 pipeline（见 _deterministic_l1_gate）。
        此处仅用更鲁棒的方式提取 LLM 的自报信号。
        """
        import re

        text = verify_result or ""

        # P1-2：识别模型拒答/截断响应（复用模块级 _is_refusal_or_truncated，
        # 与 trivial / Phase3 硬否决同一事实源）。这类不是真正的验证结论，
        # 标记为 unavailable，明确区分"模型没给出有效自报"与"模型报告失败"。
        llm_unavailable = _is_refusal_or_truncated(text)
        if llm_unavailable:
            details: dict = {
                "raw_result": "(模型拒答/截断，非有效验证自报)",
                "raw_refusal": text[:200],
                "llm_self_report": "unavailable",
                "compile_passed": False,
                "tests_passed": False,
            }
            # 自报不可用 → 保守判 fail（但最终以 deterministic gate 为准）
            return False, details

        # 显式标记优先：L1_RESULT: PASS / FAIL（容忍大小写与空格）
        m = re.search(r"L1_RESULT\s*:?\s*(PASS|FAIL)", text, re.IGNORECASE)
        if m:
            passed = m.group(1).upper() == "PASS"
        else:
            # 无显式标记时保守判定：出现明确失败信号即视为未通过
            low = text.lower()
            has_fail = any(
                kw in low
                for kw in ("fail", "失败", "未通过", "error", "错误", "❌")
            )
            has_pass = any(
                kw in low for kw in ("pass", "通过", "成功", "✅")
            )
            passed = has_pass and not has_fail

        details = {
            "raw_result": text[:500],
            "llm_self_report": "pass" if passed else "fail",
            "compile_passed": bool(re.search(r"编译.*通过|compile.*ok|compiled", text, re.IGNORECASE)),
            "tests_passed": bool(re.search(r"测试.*通过|tests?.*pass", text, re.IGNORECASE)),
        }
        return passed, details

    def _deterministic_l1_gate(self) -> tuple[bool | None, dict]:
        """循环内确定性 L1 闸门：用真实 compile/lint/scope 结果驱动修复轮次。

        借鉴 ECC 的"确定性断言驱动控制循环"经验 —— 不依赖 LLM 自报 PASS，
        而是对当前 git diff 跑确定性 pipeline。返回:
            (None, {...}) 表示无 diff 可检（跳过，交给 LLM 信号）
            (bool, details) 表示确定性结论。
        """
        if not self.project_path:
            return None, {"deterministic_gate": "skipped: no project_path"}
        try:
            diff = self._get_git_diff()
        except Exception as exc:  # noqa: BLE001
            return None, {"deterministic_gate": f"skipped: diff error {exc}"}
        # empty_diff 判定：strip 后判空，杜绝 whitespace-only / 占位变体绕过。
        # 过去仅匹配固定字面串("(无变更)"等)，导致纯空格 diff(如 "   ")被当"有变更"
        # 送进 pipeline → 解析出 0 文件 → "no diff changes → True" → 空 diff 漏判通过。
        _diff_stripped = (diff or "").strip()
        empty_diff = (
            not _diff_stripped
            or _diff_stripped in ("(无变更)", "(无法获取 git diff)")
        )
        harness = getattr(self.subtask, "harness", None)
        has_harness_checks = bool(
            harness and (harness.build_command or harness.test_command or harness.verify_commands)
        )
        # 空 diff = worker 没产生任何改动。若任务【本应改/建文件】(scope 有 writable/
        # create_files)，这是"没干活"，绝不能因 mvn 编译未改动代码恰好通过就误判 PASS。
        # 实测：模型 "need more steps"/stall 后没改 StringUtils，diff 空但 L1 却通过 →
        # 任务假 DONE。空 diff + 期望有产出 → 确定性判失败，触发重试/换模型。
        scope = self.effective_scope
        expects_changes = bool(
            (getattr(scope, "writable", []) or []) or (getattr(scope, "create_files", []) or [])
        )
        if empty_diff and expects_changes:
            return False, {
                "deterministic_gate": "fail",
                "reason": "empty_diff_but_changes_expected",
                "note": "worker 未产生任何改动（期望修改/新建文件），判定未完成",
            }
        if empty_diff and not has_harness_checks:
            # 既无 diff 又无 harness 可执行检查，才回退 LLM 自报
            return None, {"deterministic_gate": "skipped: empty diff"}
        try:
            from swarm.worker.l1_pipeline import run_l1_pipeline

            # 空 diff 但有 harness（如 greenfield 新建文件 diff 未被捕获）：
            # 仍用 harness 命令做确定性验证，杜绝 LLM 口头自报合格。
            ok, details = run_l1_pipeline(
                self.project_path, self.subtask, diff or "", llm=None
            )
            details["deterministic_gate"] = "pass" if ok else "fail"
            # audit #5/#29：标记此为 Phase 3 循环内确定性闸门(llm=None，无 LLM 开销)。
            details["l1_phase"] = "phase3_loop_deterministic"
            if empty_diff:
                details["note"] = "empty diff，仅靠 harness 命令验证"
            return ok, details
        except Exception as exc:  # noqa: BLE001
            return None, {"deterministic_gate": f"skipped: pipeline error {exc}"}

    def _run_failing_test_gate(self, failing_cmd: str) -> tuple[bool, str]:
        """DEBUG 意图专属 L1 闸门：确定性执行 failing_test_command，验证修复后该命令通过。

        复用 l1_pipeline 的 _normalize_python_cmd + subprocess 机制，
        与现有 L1 确定性验证共享同样的执行模型（local / sandbox 均可）。
        返回 (bool, detail_str)：True=命令通过(bug 已修复)，False=命令仍失败。

        优雅降级：异常时返回 True（不因执行环境失败误判为 bug 未修复），
        因为沙箱环境可能已销毁。
        """
        import subprocess

        from swarm.worker.l1_pipeline import _normalize_python_cmd

        try:
            proc = subprocess.run(
                _normalize_python_cmd(failing_cmd),
                cwd=self.project_path,
                shell=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            ok = proc.returncode == 0
            from swarm.worker.output_compress import compress_tool_output

            output_summary = compress_tool_output(
                proc.stdout or proc.stderr or "", max_chars=800
            )
            detail = f"exit_code={proc.returncode}, output={output_summary}"
            return ok, detail
        except subprocess.TimeoutExpired:
            return False, "failing_test_command timeout (120s)"
        except Exception as exc:  # noqa: BLE001
            # 优雅降级：执行环境异常不误判
            self._log(f"DEBUG L1: failing_test_command 执行异常，跳过: {exc}")
            return True, f"execution skipped: {exc}"

    def _parse_produce_result(
        self,
        produce_result: str,
        l1_passed: bool,
        l1_details: dict,
    ) -> WorkerOutput:
        """解析产出结果，构造 WorkerOutput"""
        summary = ""
        confidence = Confidence.MEDIUM
        notes = ""

        # 尝试从输出中提取结构化字段
        lines = produce_result.split("\n")
        for line in lines:
            if line.startswith("SUMMARY:"):
                summary = line[len("SUMMARY:"):].strip()
            elif line.startswith("CONFIDENCE:"):
                conf_str = line[len("CONFIDENCE:"):].strip().lower()
                confidence = Confidence(conf_str) if conf_str in ("high", "medium", "low") else Confidence.MEDIUM
            elif line.startswith("NOTES:"):
                notes = line[len("NOTES:"):].strip()

        if not summary:
            summary = produce_result[:500]

        diff = self._get_git_diff()

        return WorkerOutput(
            subtask_id=self.subtask.id,
            diff=diff,
            summary=summary,
            confidence=confidence,
            l1_passed=l1_passed,
            l1_details=l1_details,
            execution_log="\n".join(self.execution_log),
            notes=notes,
        )

    def _make_output(
        self,
        diff: str,
        summary: str,
        confidence: Confidence,
        l1_passed: bool,
        l1_details: dict,
    ) -> WorkerOutput:
        """快速构造 WorkerOutput"""
        return WorkerOutput(
            subtask_id=self.subtask.id,
            diff=diff,
            summary=summary,
            confidence=confidence,
            l1_passed=l1_passed,
            l1_details=l1_details,
            execution_log="\n".join(self.execution_log),
        )
