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
import time
from enum import Enum
from pathlib import Path
from typing import Any

from swarm.config.settings import get_config
from swarm.models.errors import TransientInfraError
from swarm.tools.scope_guard import clear_scope
from swarm.types import (
    Confidence,
    FileScope,
    KnowledgeContext,
    NotRunKind,
    SubTask,
    SubTaskDifficulty,
    WorkerOutput,
)

logger = logging.getLogger(__name__)

# round26 god-file 治理：L1 裁决纯函数簇已外置 worker/l1_verdict.py。
# 这里 re-export 回本命名空间，保持内部调用与既有测试
# （from swarm.worker.executor import evaluate_l1 / L1Verdict / ...）可寻址不变。
# F401 抑制：部分符号仅供【外部/测试】经本命名空间导入（如 _LOCATE_STEP_CAP、_det_fail_source），
# 本文件内部不用但必须保留可寻址——勿被 ruff --fix 当未用导入删除（会断测试导入）。
from swarm.worker.l1_verdict import (  # noqa: E402,F401
    _FLIPPABLE_SOURCES,
    _LOCATE_STEP_CAP,
    _LOCATE_STEP_CAP_MAX,
    _det_fail_source,
    _is_refusal_or_truncated,
    _locate_step_cap,
    _trivial_llm_self_report_passed,
    L1Verdict,
    evaluate_l1,
    missing_seed_artifacts,
    packages_from_missing_artifacts,
)

# round26 god-file 治理：per-project git 文件锁已外置 worker/git_flock.py。
# re-export 供【外部/测试】经本命名空间导入（sandbox.py / nodes / test_wave3_gitlock）——
# SYNC 簇外置后本文件内部已不用，仅作向后兼容 shim，勿被 ruff --fix 删除。
from swarm.worker.git_flock import (  # noqa: E402,F401
    _ProjectGitFlock,
    _warn_git_flock_fail_open_once,
)

# round26 god-file 治理 Step1：prompt/grounding 构建方法簇已外置为混入类。
from swarm.worker.executor_prompts import _PromptBuildingMixin  # noqa: E402
from swarm.worker.executor_sync import _SandboxSyncMixin  # noqa: E402
from swarm.worker.executor_l1gate import _L1GateMixin  # noqa: E402
from swarm.worker.executor_agent import _AgentLoopMixin  # noqa: E402
from swarm.worker.executor_lifecycle import _SandboxLifecycleMixin  # noqa: E402


class WorkerPhase(str, Enum):
    """Worker 执行阶段"""
    PREPARING = "PREPARING"
    LOCATING = "LOCATING"       # Phase 1
    CODING = "CODING"           # Phase 2
    VERIFYING = "VERIFYING"     # Phase 3
    PRODUCING = "PRODUCING"     # Phase 4
    DONE = "DONE"
    FAILED = "FAILED"


def _should_run_verify_agent(mode: str, det_ok, det_details: dict) -> bool:
    """R63-T8④：verify agent 步（每 fix_round 一整轮带工具 agent，高成本）是否该跑。

    C5 既有规则：det 有结论时不跑（llm_ok 恒被仲裁器强制 True，近零价值）。
    T8 补：det_ok=None 但 pipeline BLOCKED（如 upstream_module_broken）时也不跑——
    仲裁器对 BLOCKED 恒判 verification_not_run，verify agent 的输出无论说什么都被丢弃，
    跑它=纯烧预算（round63 st-8 VERIFYING 撞 95 迭代的直接来源）。
    mode=always 保留旧行为逃生口；never 恒不跑。
    """
    if mode == "never":
        return False
    if mode == "always":
        return True
    if det_ok is not None:
        return False
    if (det_details or {}).get("pipeline_blocked"):
        return False
    return True


def _blocked_failfast_kind(prior, l1_details: dict) -> str | None:
    """R63-T8③：Phase-4 是否应 BLOCKED fail-fast（跳过 produce LLM 步与复核）。

    仅当验证循环的结论是 verification_not_run（验证根本没跑成）且确凿带
    pipeline_blocked（如 upstream_module_broken：构建阻断根因在本子任务 scope 外）——
    此时 produce agent 与 Phase-4 重跑闸门都无增值（round63 st-8 在 blocked 判定后
    PRODUCING 仍烧 622.8s/95 迭代）。真编译错（deterministic）绝不短路：produce 步
    要收集/汇报模型已做的修复。返回阻断类别或 None。
    """
    if prior is None or getattr(prior, "source", "") != "verification_not_run":
        return None
    return (l1_details or {}).get("pipeline_blocked") or None


class WorkerExecutor(
    _L1GateMixin, _SandboxSyncMixin, _PromptBuildingMixin,
    _AgentLoopMixin, _SandboxLifecycleMixin,
):
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
        recursion_boost: int = 0,
        base_ref: str | None = None,
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
        # 3rd#2：任务钉扎的 base commit（brain 经 dispatcher 透传）。worker 在本地仓算 diff 的
        # 基线统一相对它，消除运行期 HEAD 漂移。None → resolve_base_ref 退回 "HEAD"（零回归）。
        self.base_ref = base_ref

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
        else:
            # ── B1(task 34fab09e/b63792fa)：非 trivial 按 scope 文件数动态加预算 ──
            # 实证：跨多文件功能(导出 Excel 涉 4 文件)的 worker 撞固定上限 50 共 8 次，
            # 步数不够写完所有文件。每个 writable/create 文件约需一轮 read→改→自检，
            # 故 base + 每文件 +15 步，封顶 100（受 max_execution_time 兜底，不会无限跑）。
            try:
                sc = getattr(subtask, "scope", None)
                _nfiles = len(list(getattr(sc, "writable", []) or [])
                               + list(getattr(sc, "create_files", []) or [])) if sc else 0
                if _nfiles > 1:
                    self.max_iterations = min(100, self.max_iterations + _nfiles * 15)
            except Exception:  # noqa: BLE001
                pass

        # FINDING-12：拒答/步数耗尽子任务重试时，除换最强模型外还要给更多步数。
        # RUN5 死在 trivial 档 recursion_limit(~30)——`Sorry, need more steps`。只换 40B
        # 不抬上限，多步任务照样撞同一堵墙。boost 抬顶(trivial 30→60，非 trivial 封顶 150)，
        # 仍受 max_execution_time 兜底，不会无限跑。boost=0 时此分支不动（默认零行为差）。
        if recursion_boost > 0:
            self.max_iterations = min(150, self.max_iterations + recursion_boost)

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
        # 沙箱归还热池时据此决定 reusable（L1 通过/无异常才复用，脏沙箱不回池）。
        # fail-closed 初始化为 False：仅在正常完成(1069)/trivial 快速路径(2228)显式置真才复用；
        # 任何 early-return（超时/异常路径未走到赋值点）保持 False → 脏沙箱不回池，避免污染下一子任务。
        self._l1_passed_flag: bool = False
        # diff 基线/产出快照（difflib 生成 diff 用）。沙箱模式由 _sync_to/from_sandbox 填充，
        # 本地模式由 _snapshot_scope_local 填充。__init__ 初始化避免本地模式下属性缺失。
        self._pre_sync_contents: dict[str, str | None] = {}
        self._post_sync_contents: dict[str, str | None] = {}
        # A1：本轮真正在本地 unlink 掉的 delete_files（worker 已在沙箱删除并传播到本地），
        # 供可观测/诊断；diff 由 _get_git_diff 的 delete targets 如实体现。
        self._deleted_local_paths: set[str] = set()
        # A3：最近一次 pull-back 的【完整性信号】。sync_files_from_sandbox 对 >1MiB 文件
        # 静默 skip、对读失败文件记 errors 不中断——这些都是交付相关文件，缺一块则本地 diff
        # 不完整。_deterministic_l1_gate 据此禁止在 pull-back 不完整时判 True（防沙箱绿本地缺）。
        self._sync_skipped_count: int = 0
        self._sync_error_rels: list[str] = []
        # D30：最近一次 pull-back 因超过 MAX_SYNC_FILE_SIZE 被【确定性】skip 的文件。与上面
        # transient 信号分账——重试不可恢复，L1 闸门对它判确定性失败（走失败阶梯），
        # 绝不当 BLOCKED transient 无限重试（旧行为：package-lock.json >1MiB 永久活锁）。
        self._sync_oversize_rels: list[str] = []
        # TD2606-C9：L1 确定性闸门在沙箱里修复（version-repair / import-repair / goimports …）
        # 的文件相对路径——【含子任务写权 scope 之外的，如父 pom】。累积于此，使每次 pull-back
        # 都回传它们、且计入 _get_git_diff，杜绝"修复只活在沙箱、merged_diff 缺失→集成重炸"。
        self._repaired_extra_paths: set[str] = set()
        # T2（round63 死锁触发器结构性兜底）：pull-back 三方基线闸还原过的【基线共享版本锚篡改】
        # 登记 [{file, anchor, from, to}]。fail-loud 可查证据（worker/repair 无权改基线共享锚）。
        self._baseline_integrity_restored: list[dict] = []
        # D36：bootstrap 上传完成后在沙箱 touch 的标记文件相对路径。pull-back 时据其 mtime
        # 用 `find -newer` 圈出 worker 在沙箱改过的【上下文兄弟文件】（readable/整模块源码里被
        # sed 改的），并入回传+diff——否则 sandbox 编过绿、改动不落盘→集成期 cannot find symbol。
        self._bootstrap_marker: str = ""
        # P1-D：fix 循环 no-progress 早停——记上一轮确定性失败签名 + 连同次数。
        self._last_fail_sig: str = ""
        self._same_fail_streak: int = 0
        # R63-T9②：turn 连续性 carry 源——最近一次成功产码轮（code/code-batch/fix）
        # 的全量对话，fix 轮取用时经 trim_carry_messages 裁剪。失败轮清空。
        self._continuity_messages: list | None = None

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

        # ── 确定性兜底：任务未要求测试时，剔除 scope 里的测试文件（task 5c17c464）──
        # Brain 偶把"加方法"任务擅自配测试子任务/塞 src/test 文件进 scope（PLAN prompt 已加软
        # 约束，此处为硬兜底）。测试文件常本地不存在(bootstrap errors=1)、徒增失败面。
        # 仅当子任务描述【明确要求】测试时才保留；否则从 writable/create_files 剔除测试路径。
        desc = (getattr(self.subtask, "description", "") or "")
        # §3.4：英文词按词界匹配——裸子串 "test" 被 "latest"/"attestation" 误命中，
        # 会把无关任务当"要求写测试"放行测试文件。中文关键词无词界问题保持子串。
        import re as _re
        _wants_test = (
            any(kw in desc for kw in ("测试", "单测", "覆盖", "用例"))
            or bool(_re.search(r"\b(unit[- ]?tests?|tests?|testing|coverage)\b", desc, _re.IGNORECASE))
        )
        if not _wants_test:
            # D43 治本：与 brain 侧统一单一事实源判据（shared._is_test_file_path，basename
            # startswith("test_") 路径段精确匹配）。旧本地副本用裸子串 `"test_" in 文件名`，
            # 误伤 latest_/contest_/greatest_ 等普通文件 → 被剔出 writable/create 无权写 →
            # 交付静默不完整；且与 brain 口径分叉。worker 已有多处 lazy import brain 先例
            # （stack_detect/planning_nodes），同进程运行时 brain 必已加载，无环无额外开销。
            from swarm.brain.nodes.shared import _is_test_file_path as _is_test_path
            try:
                w2 = [f for f in (getattr(scope, "writable", []) or []) if not _is_test_path(f)]
                c2 = [f for f in (getattr(scope, "create_files", []) or []) if not _is_test_path(f)]
                removed = (len(getattr(scope, "writable", []) or []) - len(w2)) + \
                          (len(getattr(scope, "create_files", []) or []) - len(c2))
                # 仅当剔除后 scope 仍有可写目标时才生效（避免把唯一目标误删成空 scope）
                if removed > 0 and (w2 or c2):
                    scope.writable = w2
                    scope.create_files = c2
                    self._log(f"scope 兜底：任务未要求测试，剔除 {removed} 个测试文件（防 Brain 擅自塞测试）")
            except Exception:  # noqa: BLE001
                pass

    def _log(self, message: str, level: str = "info") -> None:
        """记录执行日志。

        G1-4（round38c 主题G）：level 参数——旧实现恒 logger.info，真警告语义的行只能
        在【消息文本里内嵌 `[WARN]`】表达，导致 `grep WARNING`/按 level 过滤全部漏抓
        （观测硬伤）。真警告用 level="warning" 走真 logger.warning。
        """
        elapsed = time.monotonic() - self.start_time if self.start_time else 0
        entry = f"[{elapsed:.1f}s][{self.phase.value}] {message}"
        self.execution_log.append(entry)
        (logger.warning if level == "warning" else logger.info)(
            f"Worker({self.subtask.id}): {entry}")
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

    def _verify_bail_seconds(self) -> float:
        """P7：fix 循环【提前 bail 的已用预算阈值】秒。超此且确定性闸门仍红即停，不烧满到超时。
        SWARM_WORKER_VERIFY_BAIL_FRACTION（默认 0.6，钳 [0.3,0.95]）。"""
        try:
            frac = float(os.environ.get("SWARM_WORKER_VERIFY_BAIL_FRACTION", "0.6"))
        except ValueError:
            frac = 0.6
        frac = min(max(frac, 0.3), 0.95)
        return frac * self.max_execution_time

    async def run(self) -> WorkerOutput:
        """执行完整的 Worker 生命周期（编排器：依次调用各 phase 方法）

        W1.2 重构：原单体 run() 拆为 _phase_prepare / _phase_locate /
        _phase_code / _phase_verify_loop / _phase_produce 五个 phase 方法。
        编排逻辑（超时早返、trivial 分流、异常归类、finally 清理）保留在此。
        各 phase 早返时返回 WorkerOutput；否则返回 None 继续下一阶段。

        Returns:
            WorkerOutput 产出物
        """
        self.start_time = time.monotonic()
        self._log(f"开始执行子任务: {self.subtask.id}")
        # C8（阶段4）：worker 总预算 deadline 进 contextvar——工具执行入口据此做
        # 收尾哨兵（预算尽=命令不发）+ 超时钳（不冲破 deadline），治 agent 超时后
        # 孤儿同步线程对已销毁沙箱烧请求到自身超时。
        from swarm.tools.build_tools import set_worker_deadline
        set_worker_deadline(self.start_time + self.max_execution_time)

        from swarm.tools.build_tools import (
            clear_extra_whitelist,
            set_extra_whitelist,
        )

        # 按子任务 harness 放行其构建/测试/验收命令（否则 worker 跑不了验证命令）
        _harness = getattr(self.subtask, "harness", None)
        set_extra_whitelist(getattr(_harness, "extra_whitelist", None) if _harness else None)

        try:
            # ── Phase 0: 准备 ──
            early = await self._phase_prepare()
            if early is not None:
                return early
            if self.subtask.difficulty == SubTaskDifficulty.TRIVIAL:
                return await self._run_trivial_fast()

            # ── Phase 1: 定位 ──
            locate_result, early = await self._phase_locate()
            if early is not None:
                return early

            # ── Phase 2: 编码 ──
            early = await self._phase_code(locate_result)
            if early is not None:
                return early

            # ── Phase 3: L1 验证（含重试循环） ──
            l1_passed, l1_details, prior_verdict = await self._phase_verify_loop()

            # ── Phase 4: 产出 + 最终复核 + DEBUG 闸门 + 置信度校正 ──
            return await self._phase_produce(l1_passed, l1_details, prior=prior_verdict)

        except Exception as e:
            self.phase = WorkerPhase.FAILED
            self._log(f"执行异常: {e}")
            self._l1_passed_flag = False  # 异常路径：沙箱可能脏，不归还复用
            # P2：分类失败类型，供 handle_failure 决定退避重试(transient) 还是换模型(capability)。
            # R63-T7：l1_details 组装收拢到 exception_l1_details——复读退化（链尾冒泡）
            # 在此打 degeneration_hard_fail，brain 据此 force_strong 升档。
            from swarm.models.errors import classify_failure
            from swarm.worker.l1_verdict import exception_l1_details
            failure_class = classify_failure(e)
            return self._make_output(
                diff="",
                summary=f"执行异常: {e}",
                confidence=Confidence.LOW,
                l1_passed=False,
                l1_details=exception_l1_details(e, failure_class),
            )
        finally:
            from swarm.tools.build_tools import (
                clear_sandbox_context,
                clear_worker_deadline,
            )
            clear_sandbox_context()
            clear_worker_deadline()  # C8：对称清除（contextvar 按 task 隔离，防串扰）
            clear_scope()
            clear_extra_whitelist()
            self.kill_sandbox()
            elapsed = time.monotonic() - self.start_time
            self._log(f"总执行时间: {elapsed:.1f}s")

    async def _phase_prepare(self) -> WorkerOutput | None:
        """Phase 0：创建/借用远程沙箱、bootstrap 同步、创建 Agent。

        早返：超时 → 返回 WorkerOutput；否则返回 None 继续。
        """
        from swarm.tools.build_tools import set_sandbox_context

        self.phase = WorkerPhase.PREPARING
        self._log("准备阶段：设置 Scope，创建 Agent")

        if True:
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
                    # round27 perf：git 进程 + flock 属阻塞 IO，卸线程池防并发 bootstrap 卡事件环。
                    await asyncio.to_thread(self._reset_scope_to_head)
                    await self._sync_to_sandbox("bootstrap")
                    # D36：bootstrap 上传【完成后】在沙箱内打时间标记——之后 worker 对沙箱里
                    # 任何文件的改动 mtime 都晚于它，pull-back 据此圈出被改的兄弟/readable 文件。
                    self._touch_bootstrap_marker()
                    # #12 治本(B fail-closed seed)：bootstrap 后若【上游产物(provenance 标注)】缺失于
                    # 本地树（上游未就绪 或 被放弃 revert 抹掉），沙箱 seed 必不含该包 → 本子任务注定
                    # `package does not exist`。不把破工作区交 worker 空烧整条 locate/code/verify 预算，
                    # 先判 BLOCKED（transient·等生产者）短路早返，与既有 internal_pkg_not_built 同分类，
                    # 交 handle_failure 反查生产者（已就绪则重试自愈，已放弃则连坐放弃，杜绝白烧）。
                    _early_blocked = self._precheck_upstream_seed()
                    if _early_blocked is not None:
                        return _early_blocked
                except SandboxUnhealthyError as exc:
                    # 熔断：沙箱运行中连续失败达阈值 → 明确失败，不降级空转
                    self._log(f"沙箱熔断: {exc}")
                    raise
                except TransientInfraError:
                    # D11 治本（N-06 补全）：bootstrap 上传（/ pull-back / seed 预检）抛的
                    # TransientInfraError 是【基础设施瞬时失败】信号，必须向上传播——run() 的
                    # except 归类 classify_failure=transient → handle_failure 退避重试【同模型】自愈。
                    # 绝不能落到下面的宽 except 被吞成"降级本地"：此时 set_sandbox_context 已在上面
                    # 生效、self._sandbox 非 None，一旦降级，agent/L1/sync 会全部打到【bootstrap
                    # 不完整】的缺文件沙箱空跑 → 空 diff → 被误判 capability 换模型（正是要防的反模式）。
                    # 与 pull-back 侧 A3 pull-back-incomplete fail-closed 闸门对称。
                    self._log("bootstrap/同步瞬时基础设施失败，向上传播为 transient（退避重试，不降级本地空跑）")
                    raise
                except Exception as exc:
                    # fail-closed（治本，对齐 P0-SEC-08 精神）：若任务依赖【项目专属镜像自带源码】
                    # (_sandbox_has_source)，沙箱创建失败时绝不降级本地——本地没有项目源码，
                    # agent 在空环境里找不到目标文件，必然产出空 diff（dispatch 判空产出=失败）、
                    # 白烧 3 次重试后 escalate，且把"镜像丢失/不可用"伪装成"任务失败"误导用户。
                    # 实证 task 82f12ce4「推箱子」：tpl 在 CubeMaster 丢失(130404 template not found)
                    # → 降级本地 → 无 README → diff=5 空产出 → 3 次重试全败 → escalate。
                    # 明确抛错（提示重建项目镜像），而非降级到注定失败的本地空跑。
                    if getattr(self, "_sandbox_has_source", False):
                        self._log(f"沙箱创建失败且任务依赖项目专属镜像源码，fail-closed 不降级本地: {exc}")
                        # 治本（2026-07-06）：若报错坐实【专属模板悬空/过期】(130404/130409/stale/
                        # needs redo)，反向作废项目沙箱指纹→下次 preprocess 自愈重建（template_exists
                        # 探活对 130409 stale 盲，列表 status 仍 READY，只清指纹不清指针，不改本次在飞行为）。
                        try:
                            from swarm.worker.image_builder import invalidate_project_template_on_stale
                            if invalidate_project_template_on_stale(self.project_id, str(exc)):
                                self._log("已作废项目沙箱复用指纹，下次预处理将重建专属模板")
                        except Exception:  # noqa: BLE001 — 作废失败不阻断 fail-closed 主路径
                            pass
                        raise RuntimeError(
                            f"项目专属沙箱镜像不可用（{exc}）——镜像可能已过期/被清理。"
                            f"本地无项目源码，拒绝降级空跑。请重建该项目沙箱模板后重试。"
                        ) from exc
                    # I-SEC-2（round38c 主题I·外部深审 CRITICAL）：默认 fail-closed——
                    # 沙箱启用但创建失败时静默降级=LLM 任意命令逃出隔离直接跑在
                    # brain 宿主机。显式 SWARM_SANDBOX_ALLOW_LOCAL_FALLBACK=true 才
                    # 保留旧降级（单机开发本地模式请用 use_for_worker=false）。
                    if getattr(cfg.sandbox, "allow_local_fallback", False):
                        self._log(f"沙箱创建失败，降级本地执行（ALLOW_LOCAL_FALLBACK 显式开启）: {exc}")
                    else:
                        self._log(f"沙箱创建失败，fail-closed 拒绝降级宿主机执行: {exc}")
                        raise RuntimeError(
                            f"沙箱创建失败（{exc}）——fail-closed 拒绝把 worker 命令降级到"
                            "宿主机执行（安全边界）。修复沙箱服务，或单机开发场景显式设 "
                            "SWARM_SANDBOX_USE_FOR_WORKER=false / "
                            "SWARM_SANDBOX_ALLOW_LOCAL_FALLBACK=true。"
                        ) from exc
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
        return None

    def _precheck_upstream_seed(self) -> WorkerOutput | None:
        """#12 治本(B fail-closed seed)：bootstrap 后检查【上游产物 provenance】是否缺失于本地树。

        缺失=上游未就绪 或 被放弃 revert 抹掉 → 沙箱 seed 必不含该包 → 本子任务注定
        `package does not exist`。返回 BLOCKED WorkerOutput 短路早返（不空烧）；无缺失返回 None。
        仅沙箱模式生效（本地模式无 seed 环节）。provenance 由 plan 标注，基线只读上下文不入，无误判。"""
        if not self._sandbox:
            return None
        ua = list(getattr(getattr(self.subtask, "scope", None), "upstream_artifacts", []) or [])
        if not ua:
            return None
        local_root = Path(self.project_path).resolve()
        rels = [self._norm_rel(local_root, f) for f in ua]
        missing = missing_seed_artifacts(rels, local_root)
        if not missing:
            return None
        blocked_pkgs = packages_from_missing_artifacts(missing)
        self._log(
            f"[#12·seed闸门] 上游产物缺失于本地树 {missing[:5]}"
            f"{' 等' + str(len(missing)) + ' 个' if len(missing) > 5 else ''} "
            f"→ 沙箱 seed 缺包，判 BLOCKED 等生产者（不空烧 locate/code/verify）"
        )
        return self._make_output(
            diff="",
            summary=(
                f"[#12·seed闸门] 依赖的上游产物未落本地树（{len(missing)} 个：{missing[:3]}…）"
                "——上游未就绪或被放弃，工作区不完整，判 BLOCKED 退避等生产者，不空烧本子任务预算"
            ),
            confidence=Confidence.LOW,
            l1_passed=False,
            l1_details={
                "pipeline_blocked": "internal_pkg_not_built",
                "not_run_kind": NotRunKind.BLOCKED.value,
                "blocked_on_files": sorted(missing),
                "blocked_on_packages": sorted(blocked_pkgs),
                "failure_class": "transient",
            },
        )

    async def _phase_locate(self) -> tuple[str, WorkerOutput | None]:
        """Phase 1：定位。返回 (locate_result, early_output)；early 非 None 即超时早返。"""
        # 硬砍 LOCATING 预算（治本 RUN12：预读注入了但模型仍探索 167-286s 烧光整体 600s 预算，
        # 导致 CODING/VERIFY 没预算 → 超时 → 集成级联）。定位只是"理解结构/确认落点"，不该烧 50 步；
        # 有预读范例+共享契约时 ~20 步(≈10 think-act 循环)足够。撞 cap 非硬失败(返回提示交 CODING)，
        # 省下的预算留给真正干活的 CODING+VERIFY。把整体超时根因从"探索吃光预算"摁住。
        self.phase = WorkerPhase.LOCATING
        self._log("定位阶段：阅读代码，理解结构")
        # #14 治本：定位预算按 scope 文件数弹性（与 CODING 对称）；单文件/trivial 恒 20 不回归。
        _sc = getattr(self, "effective_scope", None) or getattr(self.subtask, "scope", None)
        _nf = (len(list(getattr(_sc, "writable", []) or [])
                   + list(getattr(_sc, "create_files", []) or [])) if _sc else 0)
        locate_result = await self._run_agent(
            self._build_locate_prompt(),
            step="locate",
            max_steps=_locate_step_cap(_nf, self.max_iterations),
        )
        self._log(f"定位完成: {locate_result[:200]}")

        if self._check_timeout():
            return locate_result, self._make_output(
                diff="",
                summary="超时：定位阶段超时",
                confidence=Confidence.LOW,
                l1_passed=False,
                l1_details={"error": "timeout_in_locating"},
            )
        return locate_result, None

    async def _phase_code(self, locate_result: str) -> WorkerOutput | None:
        """Phase 2：编码（B2 多文件分阶段执行）。超时早返 WorkerOutput，否则 None。"""
        self.phase = WorkerPhase.CODING
        self._log("编码阶段：实现变更")
        # T3·TDD 红绿闸（ECC §C）：编码前在 HEAD 基线取 RED（DEBUG 意图），供 Phase4 红绿裁决。
        # 内部已按 intent/env/failing_cmd 自守卫（非 DEBUG 即廉价 no-op）；卸线程池防 git/subprocess
        # 阻塞 IO 卡事件环（与 bootstrap reset 同法）。
        await asyncio.to_thread(self._maybe_capture_tdd_red_baseline)
        code_result = await self._run_coding_phase(locate_result)
        self._log(f"编码完成: {code_result[:200]}")

        if self._check_timeout():
            return self._make_output(
                diff="",
                summary="超时：编码阶段超时",
                confidence=Confidence.LOW,
                l1_passed=False,
                l1_details={"error": "timeout_in_coding"},
            )
        return None

    async def _phase_verify_loop(self) -> tuple[bool, dict, L1Verdict]:
        """Phase 3：L1 验证（含修复重试循环）。

        W1.2 commit②：三处裁决统一走 evaluate_l1 仲裁器。返回 (l1_passed,
        l1_details, verdict)；verdict 作为 prior 传入 Phase-4，决定是否允许翻盘
        （仅 empty_diff_transient / llm_self_report 这类非 sticky fail 可翻盘）。
        """
        self.phase = WorkerPhase.VERIFYING
        l1_passed = False
        l1_details: dict = {}
        verdict = L1Verdict(passed=None, source="init", reason="未执行验证")

        if True:
            for fix_round in range(self.max_fix_rounds + 1):
                self.fix_rounds = fix_round
                self._log(f"L1 验证轮次 {fix_round + 1}/{self.max_fix_rounds + 1}")

                # ── 关键修复：循环内确定性闸门查的是本地 difflib diff（_post_sync_contents），
                #    但 worker 在【沙箱】里改文件，循环内若不先 pull-back，本地 diff 恒空 →
                #    `empty_diff_but_changes_expected` 每轮必 fail（medium/complex 子任务白跑满
                #    修复轮，靠 Phase4 pull-back 后才翻盘，又慢又易误判）。
                #    故沙箱模式下：闸门前先把沙箱改动 pull-back 刷新本地，使 diff 反映真实改动。
                if self._sandbox and self._sandbox_manager:
                    await self._sync_from_sandbox(f"verify-{fix_round} 闸门前同步")
                # D53：确定性闸门（run_l1_pipeline 同步 HTTP + git 子进程 + flock，build
                # timeout 可达 900s）卸线程——旧直调会把全部并发 worker/brain/SSE/看守心跳
                # 一起冻结在事件循环上。flock 获取/释放在同一线程内完成，互斥语义不变。
                det_ok, det_details = await asyncio.to_thread(self._deterministic_l1_gate)
                # C5（阶段4，登记册 §四）：确定性闸门【先行】，verify agent 步（每 fix_round
                # 一整轮带工具 agent，高成本）只在 det_ok=None（无确定性证据、需要 LLM 弱
                # 自报兜底）时才跑——det 已有结论时它的 llm_ok 恒被仲裁器强制 True（近零价值），
                # 而其拒答/截断反而会误杀好产出（refusal 通道 artifact）。
                # SWARM_WORKER_VERIFY_AGENT_STEP: auto(默认)=仅 det None｜always=旧行为｜never=禁用。
                _verify_mode = os.environ.get(
                    "SWARM_WORKER_VERIFY_AGENT_STEP", "auto").strip().lower()
                verify_result: str | None = None
                llm_passed, l1_details = True, {}
                # R63-T8④：BLOCKED（det_ok=None + pipeline_blocked）时 verify agent 输出
                # 恒被仲裁器丢弃 → 跳过，不烧 95 迭代（决策收拢 _should_run_verify_agent）。
                if _should_run_verify_agent(_verify_mode, det_ok, det_details):
                    verify_result = await self._run_agent(
                        self._build_verify_prompt(),
                        step=f"verify-{fix_round}",
                    )
                    llm_passed, l1_details = self._parse_l1_result(verify_result)
                # W1.2：单一仲裁器裁决（refusal → det False → det None → det True）。
                # 循环内不传 prior（每轮独立裁决），翻盘逻辑只在 Phase-4 生效。
                # llm_ok 语义对齐契约：det_ok=None 时用 LLM 弱自报作为唯一信号；
                # det_ok 非 None 时循环内【无独立 LLM pipeline 自检】，故 llm_ok=True
                # 表示"无 LLM 反对"，让确定性闸门权威（保持旧行为：det True+自报 fail→以 det 为准）。
                _llm_ok_for_arbiter = llm_passed if det_ok is None else True
                verdict = evaluate_l1(
                    det_ok=det_ok,
                    det_details={**l1_details, **det_details},
                    verify_result=verify_result,
                    llm_ok=_llm_ok_for_arbiter,
                    prior=None,
                    phase="phase3_loop",
                )
                l1_passed = bool(verdict.passed)
                l1_details = {**l1_details, **verdict.details}
                if verdict.source == "refusal_hard_fail":
                    self._log("verify 回复为拒答/截断标记，硬否决 L1（产出不可信，覆盖确定性闸门）")
                elif det_ok is True and not llm_passed:
                    self._log("确定性闸门通过但 LLM 自报失败，以确定性结果为准")
                elif det_ok is False and llm_passed:
                    self._log("LLM 自报通过但确定性闸门失败，以确定性结果为准（拦截幻觉 PASS）")

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

                # fail-closed：本轮是「验证没跑成」(BLOCKED：构建 infra 故障 / 工具或工程清单缺失 /
                # pipeline 异常)——re-prompt 模型无意义（不是代码能力问题）。提前 bail，把恢复交给
                # brain 重新派发（verdict 已标 failure_class=transient → 退避重试，优先换新沙箱）。
                # 同时这是 TD2606-B6「fix 循环无 no-progress 检测」开的第一刀。
                if verdict.source == "verification_not_run":
                    self._log("L1 验证未能执行(BLOCKED)，提前结束 fix 循环，转交 brain 退避重试")
                    break

                # P1-D（TD2606-B6 第二刀）：no-progress 早停——确定性闸门连轮吐【完全相同的
                # 失败签名】(同一组编译错/同一坏 pom)说明上一轮"修复"毫无进展（996db614 实测
                # 模型把 4 轮 + 900s 全烧在同一个 cannot find symbol 上）。提前结束交 brain，
                # 不空烧到超时。签名按【整组归一化错误行】算：模型只要修掉任一错→签名变→重置。
                _sig = self._failure_signature(l1_details)
                if _sig and _sig == self._last_fail_sig:
                    self._same_fail_streak += 1
                else:
                    self._same_fail_streak = 0
                self._last_fail_sig = _sig
                if _sig and self._same_fail_streak >= 1:
                    self._log(
                        "fix 循环连续 2 轮同一失败签名、零进展 → 提前结束交 brain 退避"
                        "（修不动，不空烧到超时）"
                    )
                    break

                # P7（治本，996db614 实测 18×900s grind 直接成因）：no-progress 早停按【单轮失败
                # 签名】判，模型每轮把错改一点点→签名变→streak 重置→不早停→烧满 900s × 多次
                # orchestration 重试。加【时间维度】兜底：已用预算超阈值(默认 60%)、确定性闸门仍红、
                # 且至少跑过 1 轮 LLM 修复仍没过 → 提前 bail，别把剩余预算烧在大概率修不动的错上
                # （交 brain 退避/换模型，比同模型再烧 360s 更值）。env SWARM_WORKER_VERIFY_BAIL_FRACTION。
                if (
                    not l1_passed and fix_round >= 1 and self.start_time
                    and (time.monotonic() - self.start_time) >= self._verify_bail_seconds()
                ):
                    l1_details["verify_time_bail"] = True
                    self._log(
                        "fix 循环已用预算超阈值且确定性闸门仍未过 → 提前 bail（修不动，不烧满到"
                        "超时，交 brain 退避/换模型）"
                    )
                    break

                if fix_round < self.max_fix_rounds:
                    self._log(f"修复尝试 {fix_round + 1}/{self.max_fix_rounds}")
                    # C5：verify 步可能被跳过（det 已有结论）——fix 证据以确定性 digest 为主
                    symbol_hint = await self._symbol_grounding_hint(
                        verify_result or "", l1_details)
                    # R63-T9②：延续上一产码轮对话（裁剪后），把确定性 build 错回喂
                    # 进【同一对话】——模型看得到自己上一轮的改动与推理，不再全新
                    # 单消息从零重探（st-8 撞 95 迭代成因之一；C7 记忆块仍兜底）。
                    fix_result = await self._run_agent(
                        self._build_fix_prompt(verify_result or "", l1_details, symbol_hint),
                        step=f"fix-{fix_round}",
                        continue_messages=self._fix_carry_messages(),
                    )
                    self._log(f"修复完成: {fix_result[:200]}")

                if self._check_timeout():
                    self._log("验证阶段超时")
                    # A7 治本：写尺寸超时 marker，与 coding/locating 同源——verify 修不动到超时
                    # 同样是"工作单元太大"信号，上游 _is_timeout_oversize_failure 据此拆小而非
                    # 换模型重试同样的大块（主干B 不变量的派发面对偶）。
                    l1_details["error"] = "timeout_in_verifying"
                    break

        return l1_passed, l1_details, verdict

    def _self_review_llm(self):
        """R63-T9①：Phase-4 advisory 自检的 LLM 句柄——默认 None（不烧）。

        round63 实锤：L1.4 自检结论从不影响 verdict（pipeline 恒 return True；
        evaluate_l1 的 llm_ok 是 pipeline 确定性返回值），却在每个通过的子任务上烧
        1 次 worker LLM 调用产假 ✅ 清单。默认关闭时连 ModelRouter 都不碰；
        SWARM_WORKER_L1_SELF_REVIEW=true 显式恢复（与 pipeline 内 L1.4 闸同源）。
        """
        from swarm.worker.l1_pipeline import l1_self_review_enabled
        if not l1_self_review_enabled():
            return None
        try:
            from swarm.models.router import ModelRouter
            return ModelRouter().get_worker_llm(strategy="cost_optimized")
        except Exception as exc:  # noqa: BLE001
            self._log(f"L1 自检 LLM 获取失败，跳过自检: {exc}")
            return None

    async def _phase_produce(
        self, l1_passed: bool, l1_details: dict, prior: L1Verdict | None = None,
    ) -> WorkerOutput:
        """Phase 4：产出 + 最终复核 + DEBUG 闸门 + 置信度校正，返回最终 WorkerOutput。"""
        if True:
            # ── Phase 4: 产出 ──
            self.phase = WorkerPhase.PRODUCING
            self._log("产出阶段：从沙箱 pull-back 并收集 diff")
            await self._sync_from_sandbox("产出")
            # R63-T8③：验证循环已确定性判 BLOCKED（如 upstream_module_broken：构建阻断
            # 根因在本子任务 scope 外）→ produce LLM 步与 Phase-4 复核都无增值，立即
            # 结构化 fail-fast 交 brain（round63 st-8 在 blocked 判定后 PRODUCING 仍烧
            # 622.8s/95 迭代）。已做改动仍随 pull-back diff 回传，不丢工作。
            _ff_kind = _blocked_failfast_kind(prior, l1_details)
            if _ff_kind:
                _bmods = (l1_details or {}).get("blocked_on_modules") or []
                self._log(
                    f"R63-T8 fail-fast：L1 BLOCKED（{_ff_kind}，blocked_on={_bmods}）→ "
                    "跳过产出 LLM 步与 Phase-4 复核，立即交 brain（省预算，不烧无效迭代）"
                )
                produce_result = (
                    f"❌ blocked by upstream: {_ff_kind} blocked_on_modules={_bmods}"
                    "（R63-T8 fail-fast：构建阻断根因在本子任务 scope 之外，产出/复核步"
                    "已跳过，已做改动仍随 diff 回传）"
                )
            else:
                produce_result = await self._run_agent(
                    self._build_produce_prompt(),
                    step="produce",
                )

            # D53：_parse_produce_result 内含 _get_git_diff（git 子进程 + per-project flock），卸线程
            output = await asyncio.to_thread(
                self._parse_produce_result, produce_result, l1_passed, l1_details)

            # ── Phase 4 最终复核：与 Phase3 循环(L374)、trivial 通道(L1121)同源 ──
            # 关键修复(task 37460a5b)：此处过去裸调 run_l1_pipeline()，绕过了
            # _deterministic_l1_gate 的 "empty_diff + expects_changes → False" 拦截，
            # 导致占位/空 diff 被 run_l1_pipeline 当 "no diff changes" 返回 True → 翻盘为通过。
            # 现统一走确定性闸门拿三态，再以 LLM 自检作为 Phase4 增值，杜绝 "skip 当 pass"。
            if self.project_path and not _ff_kind:
                # D53：卸线程（同 Phase-3 循环内闸门，见上）
                det_ok, det_details = await asyncio.to_thread(self._deterministic_l1_gate)
                l1_details = {**l1_details, **det_details, "deterministic_l1": det_ok,
                              "l1_phase": "phase4_final"}

                # W1.2 commit②：Phase-4 增值——det_ok=True 时跑带 LLM 自检的 pipeline 拿 llm_ok。
                # 仅当 diff 真有可解析变更时跑（纯占位/空 diff 已被闸门拦在 False/None）。
                llm_ok: bool | None = True
                if det_ok is True and output.diff:
                    from swarm.worker.l1_pipeline import run_l1_pipeline

                    # R63-T9①：自检 LLM 默认不取（advisory 空烧），env opt-in 恢复。
                    l1_llm = self._self_review_llm()
                    # D53：带 LLM 自检的 pipeline（同步 HTTP，可长跑）同样卸线程
                    llm_ok, llm_details = await asyncio.to_thread(
                        run_l1_pipeline,
                        self.project_path, self.subtask, output.diff, llm=l1_llm,
                        project_stack=self._resolve_project_stack(),
                        # round18 P0-B：与确定性闸门同口径，排除修复触达的 scope 外文件。
                        extra_writable_paths=set(self._repaired_extra_paths),
                        # C1（阶段4）：Phase-4 自检 pipeline 同样受 worker 总预算贯穿约束
                        deadline=(self.start_time + self.max_execution_time
                                  if self.start_time else None),
                    )
                    l1_details = {**l1_details, **llm_details, "l1_phase": "phase4_final_with_llm"}
                    # #1(c) 可观测：Phase-4 的 LLM 自检 pipeline 若 blocked，det_ok=True 仍据【本阶段
                    # 刚跑的确定性闸门】(=run_l1_pipeline(llm=None)，同口径 compile/lint/test/scope)
                    # 判 PASS——这是设计正确（确定性证据独立成立、evaluate_l1:384 视 None 为不反对），
                    # 但过去 blocked 被静默吞掉。此处显式记录，杜绝"看似双证据、实为单证据"的隐形降级。
                    if llm_details.get("pipeline_blocked"):
                        self._log(
                            "L1 自检 pipeline blocked（det_ok=%s，据确定性闸门裁决，LLM 自检未增值）"
                            % det_ok
                        )

                # 单一仲裁器裁决。prior=循环内结论；翻盘仅限 prior 为可翻盘来源
                # （empty_diff_transient / llm_self_report）且非 sticky。编译/lint/scope/
                # test/refusal 失败 sticky=True，到此【永不翻盘】（W1.2 关闭的幻觉 PASS 漏洞）。
                prior_for_phase4 = prior if (prior and prior.source != "init") else None
                final_verdict = evaluate_l1(
                    det_ok=det_ok,
                    det_details=l1_details,
                    verify_result=None,  # Phase-4 不再看 verify 文本（refusal 已在循环内裁过并落进 prior）
                    llm_ok=llm_ok,
                    prior=prior_for_phase4,
                    phase="phase4_final",
                )
                l1_passed = bool(final_verdict.passed)
                l1_details = {**l1_details, **final_verdict.details}
                # W1.2 可诊断性：显式标注【翻盘】与【sticky 不翻盘】两类争议裁决，
                # 便于事后定位某个 L1 结论为何翻/为何不翻（disputed L1 outcome 的根因）。
                if prior_for_phase4 is not None and prior_for_phase4.passed is False:
                    if l1_passed:
                        self._log(
                            f"L1 翻盘：prior fail(source={prior_for_phase4.source}, "
                            f"sticky={prior_for_phase4.sticky}) 被 Phase4 确定性+LLM 双证据翻为通过"
                        )
                    elif prior_for_phase4.sticky:
                        self._log(
                            f"L1 不翻盘(sticky)：prior fail(source={prior_for_phase4.source}) "
                            f"为确定性真错误，维持未通过（关闭幻觉 PASS）"
                        )
                self._log(
                    f"L1 最终复核（Phase4 产出后）: {'通过 ✅' if l1_passed else '未通过 ❌'} "
                    f"| 来源={final_verdict.source} | {final_verdict.reason}"
                )
                output = output.model_copy(update={"l1_passed": l1_passed, "l1_details": l1_details})

            # ── DEBUG 意图专属 L1 校验：验证 failing_test_command 修复后通过 ──
            # 猎手 F3/复核 LOW：BLOCKED fail-fast 时同样跳过——对着已知被上游阻断的
            # 工作区跑 failing_test 只会再失败一次（最多 120s 白烧），且"仍失败=未修复"
            # 的日志会掩盖真实原因是上游阻断。
            if self.subtask.intent == "debug" and self.project_path and not _ff_kind:
                harness = getattr(self.subtask, "harness", None)
                failing_cmd = getattr(harness, "failing_test_command", "") if harness else ""
                if failing_cmd:
                    self._log(f"DEBUG L1: 执行 failing_test_command 验证修复: {failing_cmd}")
                    debug_l1_ok, debug_l1_detail = self._run_failing_test_gate(failing_cmd)
                    l1_details["debug_failing_test_command"] = failing_cmd
                    l1_details["debug_failing_test_passed"] = debug_l1_ok
                    l1_details["debug_failing_test_detail"] = debug_l1_detail
                    # T3·红绿闸（ECC §C）：并入编码前基线 RED 三态证据，综合裁决"无红不算绿"。
                    _red_ec = getattr(self, "_tdd_red_exit_code", None)
                    l1_details["tdd_red_exit_code"] = _red_ec
                    l1_details["tdd_red_detail"] = getattr(self, "_tdd_red_detail", "")
                    l1_details["tdd_red_proven"] = None if _red_ec is None else _red_ec != 0
                    # strict 泄压阀（默认关=观测优先，见 _tdd_red_green_verdict 对抗复核 F1/F2 论证）——
                    # 命名/解析与本仓 SWARM_WORKER_* 惯例一致。
                    _tdd_strict = os.environ.get(
                        "SWARM_WORKER_TDD_RED_STRICT", "false").lower() in ("true", "1", "yes", "on")
                    debug_l1_ok, _tdd_reason = self._tdd_red_green_verdict(
                        debug_l1_ok, _red_ec, strict=_tdd_strict)
                    l1_details["tdd_gate"] = _tdd_reason
                    if not debug_l1_ok:
                        l1_passed = False
                        if _tdd_reason == "red_not_proven_failclosed":
                            self._log(
                                "TDD 红绿闸(strict): failing_test 基线(未修)就通过=不复现 bug，修复信号"
                                f"不可信 → fail-closed ❌ | {debug_l1_detail}"
                            )
                        else:
                            self._log(
                                "DEBUG L1: failing_test_command 仍失败，判定为未修复 ❌ | "
                                f"{debug_l1_detail}"
                            )
                    elif _tdd_reason == "red_not_proven_observed":
                        self._log(
                            "TDD 红绿闸: ⚠️ failing_test 基线就通过=红证不成立（仅观测不阻断，非 strict）；"
                            f"判 bug 已修但存疑，证据入 provenance | {debug_l1_detail}"
                        )
                    else:
                        self._log(
                            f"DEBUG L1: failing_test_command 通过且红证={_tdd_reason}，bug 已修复 ✅"
                        )
                    # P3(对抗复核)：成功/失败两路都显式回写 output，杜绝依赖 dict 别名隐式传播 tdd_red_*
                    # 证据（否则未来有人在本块前 rebind l1_details，证据会静默从最终 WorkerOutput 蒸发）。
                    output = output.model_copy(
                        update={"l1_passed": l1_passed, "l1_details": l1_details}
                    )
                else:
                    self._log("DEBUG L1: harness.failing_test_command 为空，跳过专属校验")
            # 非 DEBUG 意图完全不受影响

            # ── C2 修复(task 34fab09e)：消除"空 diff/未过 L1 却报 high 置信度"的假性成功 ──
            # worker 撞迭代上限(50)后可能产出空 diff，但 confidence 仍是 LLM 自报的 high，
            # 误导 handle_failure 与人工审核。这里以【确定性结果】校正自报置信度：
            #   ① L1 未通过 → 置信度封顶 LOW（不让自报 high 掩盖失败）；
            #   ② diff 为空但本应有改动 → 置信度强制 LOW（撞上限空转的典型特征）。
            try:
                _diff_empty = not (getattr(output, "diff", "") or "").strip()
                _l1_ok = bool(getattr(output, "l1_passed", False))
                if (not _l1_ok or _diff_empty) and output.confidence != Confidence.LOW:
                    _old = output.confidence.value
                    output = output.model_copy(update={"confidence": Confidence.LOW})
                    self._log(
                        f"置信度校正：{_old} → low（"
                        + ("L1未通过" if not _l1_ok else "")
                        + ("+空diff" if _diff_empty else "")
                        + "，确定性结果覆盖自报置信度）"
                    )
            except Exception as _ce:  # noqa: BLE001
                self._log(f"置信度校正跳过（非致命）: {_ce}")

            self.phase = WorkerPhase.DONE
            self._log(f"执行完成，置信度: {output.confidence.value}")

            # 记录 L1 结果供 kill_sandbox 决定是否归还热池复用（脏沙箱不回池）
            self._l1_passed_flag = bool(getattr(output, "l1_passed", False))

            # H2（round48c 深读实锤·本轮最大杀伤链）：L1 最终未通过的子任务，其触碰的
            # 【共享构建清单】足迹必须立即从本地共享树回滚——毒 pom 落树后被 bootstrap
            # "补传上游产物"复制进后续全部沙箱（17 会话 86 次命中），三层旧防线全部
            # 失效（clean_upload tracked 判定空集 55/88、钉扎 reset 不管 untracked、
            # L1 闸不拦 pull-back）。diff 已进 WorkerOutput（重试上下文不丢工作）；
            # 源码文件不回滚（模块内污染有 BLOCKED 豁免，且保留供重试增量）。
            if not self._l1_passed_flag:
                try:
                    await asyncio.to_thread(
                        self._rollback_failed_manifest_footprint,
                        getattr(output, "l1_details", None) or {})
                except Exception as _rb_exc:  # noqa: BLE001 — 回滚失败不改变终局
                    self._log(f"H2 清单足迹回滚失败（不致命）: {_rb_exc}")

            return output

    # ──────────────────────────────────────────
    # 内部方法
    # ──────────────────────────────────────────


    async def _run_trivial_fast(self) -> WorkerOutput:
        """trivial 子任务快速路径：合并定位+编码，最小 L1，快速产出"""
        self.phase = WorkerPhase.CODING
        self._log("trivial 快速路径：合并定位与编码")
        from swarm.worker.prompts import strip_machine_annotations as _strip_ann
        combined = await self._run_agent(
            "这是 trivial 简单子任务，请一次完成：\n"
            f"任务：{_strip_ann(self.subtask.description)}\n\n"
            "文件操作清单（务必按操作类型处理）：\n"
            f"{self._scope_ops_hint()}\n"
            f"{self._context_snippets_block()}\n"
            "执行步骤：\n"
            "1. 对【修改】文件：read_file 读取后 patch_file 做最小必要改动\n"
            "2. 对【新建】文件：直接 write_file 写入完整内容（切勿先 read_file）\n"
            "3. 对【删除】文件：run_command 执行 rm\n\n"
            "⚠️ 重要约束（避免绕圈耗尽步数）：\n"
            "- 【禁止】自己运行重型构建/测试命令：不要跑 mvn compile / mvn test / "
            "gradle build / npm build / npm test 等。编译和测试由系统的确定性 L1 闸门统一负责，"
            "你只管把文件改对。反复跑构建会耗光你的步数预算导致任务失败。\n"
            "- 改完目标文件即【立即停止】并简要说明改动，不要反复读取/验证/自我怀疑。\n"
            "- Java/前端等非 Python 文件：改完直接结束，不要尝试编译。\n",
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
            # ── 拒答(空转输出 "Sorry, need more steps")是【模型能力】问题
            # (models/errors.py 归 CAPABILITY，非瞬时)。RUN5/RUN6 实证：trivial 档拒答时换
            # 该难度 fallback 链首=更弱模型(如 ThinkingCap-27B)→ 雪上加霜，仍拒答。修正方向：
            # 换【最强本地模型】(routing_complex=Qwopus 256k)worker 内部重试一次；已在最强模型上
            # 则无可再升，直接硬否决，抛给上层 HANDLE_FAILURE 走 force_strong/abandon。
            _strongest = get_config().model.routing_complex
            if (not getattr(self, "_trivial_alt_retried", False)
                    and _strongest and _strongest != self.model_name):
                self._trivial_alt_retried = True
                self._log(f"trivial: 主模型({self.model_name})拒答 → 升级最强模型 {_strongest} 内部重试一次")
                try:
                    self.model_name = _strongest
                    self._agent = self._create_agent()  # 用最强模型重建 agent
                    return await self._run_trivial_fast()
                except Exception as e:  # noqa: BLE001
                    self._log(f"trivial: 最强模型重试初始化失败({str(e)[:60]})，硬否决 L1")
            l1_details["l1_decision_source"] = "refusal_hard_fail"
            l1_details["raw_refusal"] = combined[:200]
            self._log("trivial: agent 回复为拒答/截断标记，硬否决 L1（产出不可信，覆盖确定性闸门）")
            self.phase = WorkerPhase.PRODUCING
            await self._sync_from_sandbox("产出")
            produce_result = await self._run_agent(self._build_produce_prompt(), step="produce")
            # D53：git diff 子进程 + flock 卸线程
            output = await asyncio.to_thread(self._parse_produce_result, produce_result, False, l1_details)
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
        # D53：卸线程（同步 build/git/flock 不冻结事件循环）
        det_ok, det_details = await asyncio.to_thread(self._deterministic_l1_gate)
        # W1.2 commit②：trivial 也走单一仲裁器。此处 combined 已确认非 refusal（上方
        # refusal 分支已早返），故 verify_result=None 避免重复 refusal 检测；llm_ok 语义
        # 同 Phase-3：det_ok=None 时用弱自报，det_ok 非 None 时 llm_ok=True 让确定性权威。
        _llm_ok_for_arbiter = llm_passed if det_ok is None else True
        verdict = evaluate_l1(
            det_ok=det_ok,
            det_details={**l1_details, **det_details},
            verify_result=None,
            llm_ok=_llm_ok_for_arbiter,
            prior=None,
            phase="trivial",
        )
        l1_passed = bool(verdict.passed)
        l1_details = {**l1_details, **verdict.details}
        if det_ok is False and llm_passed:
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
        # D53：git diff 子进程 + flock 卸线程
        output = await asyncio.to_thread(self._parse_produce_result, produce_result, l1_passed, l1_details)
        self.phase = WorkerPhase.DONE
        # 记录 L1 结果供 kill_sandbox 决定 reusable（脏沙箱不回池）。
        # trivial 快速路径直接 return，不经过 run() 末尾的 _l1_passed_flag 赋值，
        # 必须在此显式设置，否则 L1 失败的脏沙箱会以默认 reusable=True 回池污染。
        self._l1_passed_flag = bool(getattr(output, "l1_passed", False))
        # H2：trivial 路径同享清单足迹回滚（入口对称——脚手架 pom 子任务多走此路）
        if not self._l1_passed_flag:
            try:
                await asyncio.to_thread(
                    self._rollback_failed_manifest_footprint,
                    getattr(output, "l1_details", None) or {})
            except Exception as _rb_exc:  # noqa: BLE001
                self._log(f"H2 清单足迹回滚失败（不致命）: {_rb_exc}")
        self._log(f"trivial 快速路径完成，置信度: {output.confidence.value}")
        return output


    async def _run_coding_phase(self, locate_result: str) -> str:
        """B2(task 82104bf1)：多文件子任务分阶段编码，避免单次 agent loop 步数爆。

        scope 写文件 ≤3 → 单次 loop（保持原行为）。
        >3 → 按文件分批（每批 1-2 个），每批独立 agent loop（独立步数预算），
        写完即在沙箱 git add 锁定进度。阶段间不丢已写文件，每批步数充裕。
        """
        scope = self.effective_scope
        write_files = (list(getattr(scope, "writable", []) or [])
                       + list(getattr(scope, "create_files", []) or []))
        # ≤3 文件：单次（原行为，不引入分批开销）
        if len(write_files) <= 3:
            return await self._run_agent(self._build_code_prompt(locate_result), step="code")

        # >3 文件：按文件分批，每批 2 个
        batch_size = 2
        batches = [write_files[i:i + batch_size] for i in range(0, len(write_files), batch_size)]
        self._log(f"B2 分阶段编码：{len(write_files)} 个文件分 {len(batches)} 批（每批≤{batch_size}），各批独立步数预算")
        results: list[str] = []
        done_files: list[str] = []
        for bi, batch in enumerate(batches):
            self._log(f"B2 批次 {bi + 1}/{len(batches)}：聚焦 {batch}")
            r = await self._run_agent(
                self._build_batch_code_prompt(locate_result, batch, done_files, bi + 1, len(batches)),
                step=f"code-batch-{bi + 1}",
            )
            results.append(f"[批次{bi + 1}] {r[:150]}")
            done_files.extend(batch)
            # 在沙箱里 git add 已写文件，锁定进度（即使后续批次撞上限，已写的不丢）
            await self._sandbox_checkpoint(batch)
            if self._check_timeout():
                self._log("B2 分阶段编码：时间预算用尽，停止后续批次（已写文件保留）")
                break
        return " | ".join(results)

    async def _sandbox_checkpoint(self, files: list[str]) -> None:
        """B2 阶段进度记录（C12 阶段4 治虚假 git add，登记册 §四）。

        旧实现在沙箱里 `git add … || true`——沙箱【无 .git】是 by design（round20#13），
        命令静默 no-op 却无条件日志"已锁定进度"=纯剧场：真正的进度保护本就来自
        ①文件持久在沙箱文件系统②pull-back 按内容同步（与 git index 无关）。
        改为只做诚实的进度日志（保留 seam：分批编码的进度可观测点），不再发假命令。
        """
        if not self._sandbox or not files:
            return
        self._log(
            f"B2 checkpoint：批内 {len(files)} 个文件已写入沙箱文件系统"
            "（pull-back 按内容同步，后续批次撞上限也不丢）")


    def _make_output(
        self,
        diff: str,
        summary: str,
        confidence: Confidence,
        l1_passed: bool,
        l1_details: dict,
    ) -> WorkerOutput:
        """快速构造 WorkerOutput"""
        # G2-2（主题G·工具观测面）：把累计的工具调用遥测挂进 l1_details（机读通道，进
        # subtask_results；experience__ 工具含在内=技能→子任务成败可事后 join）。无调用则不塞。
        _tel = getattr(self, "_tool_telemetry", None)
        if _tel and _tel.get("calls") and isinstance(l1_details, dict) \
                and "tool_telemetry" not in l1_details:
            # hunter F3：快照拷贝而非挂 live 引用——防未来非终态多次 _make_output 时已返回的
            # WorkerOutput.l1_details 被后续 agent 步静默改写（当前调用图不触发，廉价保险）。
            l1_details = {**l1_details, "tool_telemetry": {
                "calls": dict(_tel.get("calls") or {}),
                "errors": dict(_tel.get("errors") or {}),
            }}
        return WorkerOutput(
            subtask_id=self.subtask.id,
            diff=diff,
            summary=summary,
            confidence=confidence,
            l1_passed=l1_passed,
            l1_details=l1_details,
            execution_log="\n".join(self.execution_log),
        )
