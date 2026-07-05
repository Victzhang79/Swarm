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


# 进程级技术栈画像缓存（按 project_path/project_id）：避免每个子任务重复扫盘探测栈。
_PROJECT_STACK_CACHE: dict[str, dict | None] = {}


class WorkerPhase(str, Enum):
    """Worker 执行阶段"""
    PREPARING = "PREPARING"
    LOCATING = "LOCATING"       # Phase 1
    CODING = "CODING"           # Phase 2
    VERIFYING = "VERIFYING"     # Phase 3
    PRODUCING = "PRODUCING"     # Phase 4
    DONE = "DONE"
    FAILED = "FAILED"


class WorkerExecutor(_SandboxSyncMixin, _PromptBuildingMixin):
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
        # TD2606-C9：L1 确定性闸门在沙箱里修复（version-repair / import-repair / goimports …）
        # 的文件相对路径——【含子任务写权 scope 之外的，如父 pom】。累积于此，使每次 pull-back
        # 都回传它们、且计入 _get_git_diff，杜绝"修复只活在沙箱、merged_diff 缺失→集成重炸"。
        self._repaired_extra_paths: set[str] = set()
        # P1-D：fix 循环 no-progress 早停——记上一轮确定性失败签名 + 连同次数。
        self._last_fail_sig: str = ""
        self._same_fail_streak: int = 0

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
        _wants_test = any(kw in desc for kw in (
            "测试", "单测", "test", "Test", "覆盖", "coverage", "用例",
        ))
        if not _wants_test:
            def _is_test_path(p: str) -> bool:
                pl = str(p).replace("\\", "/").lower()
                return ("/test/" in pl or "/tests/" in pl
                        or pl.endswith("test.java") or pl.endswith("tests.java")
                        or "test_" in pl.rsplit("/", 1)[-1]
                        or pl.endswith("_test.py") or pl.endswith(".test.js")
                        or pl.endswith(".spec.ts") or pl.endswith(".test.ts"))
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
            from swarm.tools.build_tools import clear_sandbox_context
            clear_sandbox_context()
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
                    self._reset_scope_to_head()
                    await self._sync_to_sandbox("bootstrap")
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
                        raise RuntimeError(
                            f"项目专属沙箱镜像不可用（{exc}）——镜像可能已过期/被清理。"
                            f"本地无项目源码，拒绝降级空跑。请重建该项目沙箱模板后重试。"
                        ) from exc
                    # 通用镜像/无源码依赖（纯文字等）→ 降级本地合法
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
                    symbol_hint = await self._symbol_grounding_hint(verify_result, l1_details)
                    fix_result = await self._run_agent(
                        self._build_fix_prompt(verify_result, l1_details, symbol_hint),
                        step=f"fix-{fix_round}",
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

    async def _phase_produce(
        self, l1_passed: bool, l1_details: dict, prior: L1Verdict | None = None,
    ) -> WorkerOutput:
        """Phase 4：产出 + 最终复核 + DEBUG 闸门 + 置信度校正，返回最终 WorkerOutput。"""
        if True:
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

                # W1.2 commit②：Phase-4 增值——det_ok=True 时跑带 LLM 自检的 pipeline 拿 llm_ok。
                # 仅当 diff 真有可解析变更时跑（纯占位/空 diff 已被闸门拦在 False/None）。
                llm_ok: bool | None = True
                if det_ok is True and output.diff:
                    from swarm.worker.l1_pipeline import run_l1_pipeline

                    l1_llm = None
                    try:
                        from swarm.models.router import ModelRouter
                        l1_llm = ModelRouter().get_worker_llm(strategy="cost_optimized")
                    except Exception as exc:  # noqa: BLE001
                        self._log(f"L1 自检 LLM 获取失败，跳过自检: {exc}")
                    llm_ok, llm_details = run_l1_pipeline(
                        self.project_path, self.subtask, output.diff, llm=l1_llm,
                        project_stack=self._resolve_project_stack(),
                        # round18 P0-B：与确定性闸门同口径，排除修复触达的 scope 外文件。
                        extra_writable_paths=set(self._repaired_extra_paths),
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

            return output

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
            project_stack=self._resolve_project_stack(),
        )

    def _resolve_project_stack(self) -> dict | None:
        """解析本项目技术栈画像，喂给 worker prompt（jakarta/javax 命名空间等硬前提）。

        单一权威事实复用：优先取 detect_stack 已缓存到 projects.config 的画像；该画像若是
        本次改动【新增 jvm 字段】前的旧缓存（无 servlet_namespace），或无 project record
        （ad-hoc 运行），则当场对磁盘做一次确定性探测兜底——保证命名空间事实始终在场。
        结果按 project_path 进程级缓存，避免每个子任务重复扫盘。
        """
        key = self.project_path or self.project_id or ""
        if key in _PROJECT_STACK_CACHE:
            return _PROJECT_STACK_CACHE[key]
        profile: dict | None = None
        # ① projects.config 缓存（detect_stack 产出的权威画像）
        if self.project_id:
            try:
                from swarm.project import store as _pstore
                rec = _pstore.get_project(self.project_id)
                cached = (rec or {}).get("config", {}).get("project_stack")
                if isinstance(cached, dict):
                    profile = cached
            except Exception:  # noqa: BLE001
                profile = None
        # ② 重探触发：旧缓存缺 jvm 命名空间 / 无 record / 【指纹漂移=栈已变更】。
        # TD2606-B20：原仅在 servlet_namespace 缺失时兜底，盲信缓存的前后端裁决——栈迁移
        # （javax→jakarta、加 JS 前端等）但 detect_stack 未重跑时，旧画像会当硬前提喂错 worker。
        # 这里用廉价 compute_repo_fingerprint 比对缓存指纹，漂移则【整画像重探】（每进程每 key 仅一次）。
        cur_fp = ""
        if self.project_path:
            try:
                from swarm.brain.stack_detect import compute_repo_fingerprint
                cur_fp = compute_repo_fingerprint(self.project_path)
            except Exception:  # noqa: BLE001
                cur_fp = ""
        fp_drifted = bool(
            profile and cur_fp and profile.get("fingerprint") and cur_fp != profile.get("fingerprint")
        )
        if fp_drifted:
            logger.info("[STACK] 缓存技术栈指纹漂移(%s→%s)，整画像重探（B20）",
                        profile.get("fingerprint"), cur_fp)
        need_disk = fp_drifted or not profile or not (
            (profile.get("jvm") or {}).get("servlet_namespace")
        )
        if need_disk and self.project_path:
            try:
                from swarm.brain.stack_detect import detect_stack_deterministic
                fresh = detect_stack_deterministic(self.project_path)
                if fp_drifted:
                    profile = fresh  # 指纹漂移 → 整画像重取，不保留旧前后端裁决
                    if cur_fp:
                        profile["fingerprint"] = cur_fp
                elif profile and (fresh.get("jvm") or {}).get("servlet_namespace"):
                    # 保留权威画像其它字段，仅补 jvm（前后端裁决以缓存为准）
                    profile = {**profile, "jvm": fresh["jvm"]}
                else:
                    profile = profile or fresh
            except Exception:  # noqa: BLE001
                pass
        _PROJECT_STACK_CACHE[key] = profile
        return profile


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
                    # 归还：L1 通过/无异常的沙箱可复用；失败的不回池(可能脏)。
                    # TD2606-C4：项目专属镜像(_sandbox_has_source)把源码烤进 /workspace 且【不含 .git】。
                    # 池复用前 clean_workspace 的 `rm -rf /workspace` 会抹掉烤进的源码 → 下个任务缺源编译失败；
                    # 而仅"保留 /workspace"又会带入上个任务的改动破坏跨任务隔离。两难。故烤源沙箱【不回池】，
                    # 每任务从【缓存镜像】创建新容器（镜像层仍含源码+.m2/warmup，启动快），杜绝抹源与污染两种 bug。
                    # getattr 默认与 __init__ 一致取 False（fail-closed：缺标记=不复用脏沙箱）
                    reusable = bool(getattr(self, "_l1_passed_flag", False)) and not getattr(
                        self, "_sandbox_has_source", False)
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

    async def _run_agent(self, human_message: str, *, step: str = "react",
                         max_steps: int | None = None) -> str:
        """调用 Agent 执行一步并返回结果（受总执行时间预算约束）。

        max_steps：本步专属 recursion_limit 上限（默认用整体 max_iterations）。LOCATING 等
        "理解/定位"阶段用更紧的 cap，逼模型少探索直接产出（RUN12 实证：预读注入了但模型仍
        探索 167-286s 烧光预算）。撞 cap 非硬失败——下方 GraphRecursionError 优雅返回，交 CODING。
        """
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
        _limit = max_steps if (max_steps and max_steps > 0) else self.max_iterations
        invoke_config = merge_invoke_config(
            {"recursion_limit": _limit},
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
                self._log(f"Agent 撞迭代上限({_limit})，以沙箱实际产出为准交确定性闸门裁决")
                return f"⚠️ Agent 达到迭代上限（{_limit}），已做改动交由确定性 L1 验证"
            raise

        # 提取最后一条 AI 消息
        messages = result.get("messages", [])
        if messages:
            last = messages[-1]
            return getattr(last, "content", str(last))
        return "(Agent 无输出)"


    async def _run_trivial_fast(self) -> WorkerOutput:
        """trivial 子任务快速路径：合并定位+编码，最小 L1，快速产出"""
        self.phase = WorkerPhase.CODING
        self._log("trivial 快速路径：合并定位与编码")
        combined = await self._run_agent(
            "这是 trivial 简单子任务，请一次完成：\n"
            f"任务：{self.subtask.description}\n\n"
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
            # 该难度 fallback 链首=更弱模型(如 27B-Saka)→ 雪上加霜，仍拒答。修正方向：
            # 换【最强本地模型】(routing_complex=40B 256k)worker 内部重试一次；已在最强模型上
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
        output = self._parse_produce_result(produce_result, l1_passed, l1_details)
        self.phase = WorkerPhase.DONE
        # 记录 L1 结果供 kill_sandbox 决定 reusable（脏沙箱不回池）。
        # trivial 快速路径直接 return，不经过 run() 末尾的 _l1_passed_flag 赋值，
        # 必须在此显式设置，否则 L1 失败的脏沙箱会以默认 reusable=True 回池污染。
        self._l1_passed_flag = bool(getattr(output, "l1_passed", False))
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
        """B2：在沙箱里 git add 指定文件，锁定阶段进度（best-effort，失败不致命）。"""
        if not self._sandbox or not files:
            return
        try:
            import shlex
            quoted = " ".join(shlex.quote(f) for f in files)  # R23-4：安全引用，防文件名注入
            cmd = f"cd /workspace && git add {quoted} 2>/dev/null || true"
            run = getattr(self._sandbox, "commands", None)
            if run and hasattr(run, "run"):
                await asyncio.to_thread(run.run, cmd)
            self._log(f"B2 checkpoint：已 git add {len(files)} 个文件锁定进度")
        except Exception as exc:  # noqa: BLE001
            self._log(f"B2 checkpoint 跳过（非致命）: {exc}")


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

    @staticmethod
    def _failure_signature(l1_details: dict) -> str:
        """P1-D：把确定性闸门的失败归一化成稳定签名，用于跨轮 no-progress 比对。

        取 build/test/compile 输出里的错误行，剥掉行列号/绝对路径/ANSI（这些每轮可能抖动但
        不代表进展），对【去重排序后的错误行集合】求 hash。整组错误一字不变 → 同签名 → 无进展。
        """
        if not isinstance(l1_details, dict):
            return ""
        import hashlib
        blob = "\n".join(
            str(l1_details.get(k) or "")
            for k in ("build_output", "test_output", "compile_message", "reason", "build_failed")
        )
        if not blob.strip():
            return ""
        t = re.sub(r"\x1b\[[0-9;]*m", "", blob)                       # 去 ANSI
        t = re.sub(r":\[\d+,\d+\]", ":[L,C]", t)                      # 去行列号
        t = re.sub(r"(/[^\s:]+)+/", "<path>/", t)                     # 去绝对路径
        # 去掉每轮必抖动但与进展无关的噪声（时长/时间戳/maven 下载进度）
        t = re.sub(r"(?m)^.*(Total time|Finished at|Progress \(\d+\)|"
                   r"Download(ing|ed) from).*$", "", t)
        t = re.sub(r"\s+", " ", t).strip()
        if not t:
            return ""
        return hashlib.md5(t.encode("utf-8")).hexdigest()[:12]

    def _record_repaired_paths(self, details: dict) -> None:
        """TD2606-C9：登记 L1 闸门在沙箱里确定性修复的文件相对路径。

        - 归一化（去 ./ 前缀），累积到 self._repaired_extra_paths；后续每次 _sync_from_sandbox
          都会把它们一并 pull-back，_get_git_diff 也会把它们纳入 diff——即便文件在子任务写权
          scope 之外（典型：父 pom 的版本号被 version-repair 改对）。
        - 同时为【无 .git 回退 difflib】路径补 pre 基线：此刻本地文件尚未被沙箱修复触及
          （修复发生在沙箱），其本地内容即 HEAD 基线，捕获后 difflib 才能算出正确增量。
        """
        paths = details.get("repaired_file_paths") if isinstance(details, dict) else None
        if not paths:
            return
        local_root = Path(self.project_path) if self.project_path else None
        for raw in paths:
            rel = str(raw or "").strip()
            if rel.startswith("./"):
                rel = rel[2:]
            if not rel:
                continue
            self._repaired_extra_paths.add(rel)
            # difflib 基线（仅在尚未捕获时，且仅用于无 .git 回退路径）：优先 git HEAD 提交版
            # （两种执行模式都正确）；无 git 时回退本地工作副本（沙箱模式下此刻本地仍是 HEAD）。
            if local_root is not None and rel not in self._pre_sync_contents:
                git_text = self._git_baseline_text(local_root, rel)
                if git_text is not None:
                    self._pre_sync_contents[rel] = git_text
                else:
                    try:
                        lp = local_root / rel
                        self._pre_sync_contents[rel] = (
                            lp.read_text("utf-8") if lp.is_file() else ""
                        )
                    except (OSError, UnicodeDecodeError):
                        self._pre_sync_contents[rel] = ""

    def _deterministic_l1_gate(self) -> tuple[bool | None, dict]:
        """循环内确定性 L1 闸门：用真实 compile/lint/scope 结果驱动修复轮次。

        借鉴 ECC 的"确定性断言驱动控制循环"经验 —— 不依赖 LLM 自报 PASS，
        而是对当前 git diff 跑确定性 pipeline。返回:
            (None, {...}) 表示无 diff 可检（跳过，交给 LLM 信号）
            (bool, details) 表示确定性结论。
        """
        # A5 治本：worker 总预算闸。确定性 L1 同步路径(含 run_l1_pipeline 的 build-repair
        # 循环，自带 900s 墙钟、与 worker 总预算解耦)过去无任何 _check_timeout——verify 撞
        # max_execution_time 后 Phase4 仍能再起一整轮 repair runaway。已超时 → 不进 pipeline，
        # 降 BLOCKED(交裁决器走退避，run() 随即因超时收尾)。
        if self._check_timeout():
            return None, {"deterministic_gate": "skipped: worker budget exhausted",
                          "not_run_kind": NotRunKind.BLOCKED.value,
                          "error": "timeout_in_verifying"}
        if not self.project_path:
            return None, {"deterministic_gate": "skipped: no project_path",
                          "not_run_kind": NotRunKind.BLOCKED.value}
        try:
            diff = self._get_git_diff()
        except Exception as exc:  # noqa: BLE001
            return None, {"deterministic_gate": f"skipped: diff error {exc}",
                          "not_run_kind": NotRunKind.BLOCKED.value}
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
        # A1 治本：delete_files 也是"期望产生变更"——删除必须体现为 diff（本地文件被
        # unlink → git diff 显示删除）。漏算 delete_files 时，纯删除 scope 恒空 diff →
        # 走下方 BENIGN → 回退 LLM 弱信号假绿（删除从未发生/未传播）。纳入后：删除已
        # 传播成功 → diff 非空正常裁决；未传播/未执行 → 空 diff + expects → 判 False。
        expects_changes = bool(
            (getattr(scope, "writable", []) or [])
            or (getattr(scope, "create_files", []) or [])
            or (getattr(scope, "delete_files", []) or [])
        )
        if empty_diff and expects_changes:
            return False, {
                "deterministic_gate": "fail",
                "reason": "empty_diff_but_changes_expected",
                "note": "worker 未产生任何改动（期望修改/新建文件），判定未完成",
            }
        if empty_diff and not has_harness_checks:
            # 既无 diff 又无 harness 可执行检查——且上方已排除 expects_changes（那是 BLOCKED fail）。
            # 此即真 no-op：合法地没东西可验证 → BENIGN，可回退 LLM 弱信号。
            return None, {"deterministic_gate": "skipped: empty diff",
                          "not_run_kind": NotRunKind.BENIGN.value}
        try:
            from swarm.worker.l1_pipeline import run_l1_pipeline

            # 空 diff 但有 harness（如 greenfield 新建文件 diff 未被捕获）：
            # 仍用 harness 命令做确定性验证，杜绝 LLM 口头自报合格。
            ok, details = run_l1_pipeline(
                self.project_path, self.subtask, diff or "", llm=None,
                project_stack=self._resolve_project_stack(),
                # round18 P0-B：确定性修复触达的 scope 外文件(如 module-reg 自愈的父 pom)
                # 不计入 scope 违规——见 _get_git_diff 把 _repaired_extra_paths 纳入 diff。
                extra_writable_paths=set(self._repaired_extra_paths),
            )
            # TD2606-C9：登记本轮在沙箱里被确定性修复的文件，使其回传本地 + 计入 diff。
            self._record_repaired_paths(details)
            # audit #5/#29：标记此为 Phase 3 循环内确定性闸门(llm=None，无 LLM 开销)。
            details["l1_phase"] = "phase3_loop_deterministic"
            # fail-closed：pipeline 可能「跑通了能跑的、但有该验证的环节被阻塞」（构建工具/工程
            # 清单缺失、构建命中 infra 瞬时故障、非空 diff 却解析到 0 文件）。这种 passed-but-blocked
            # 绝不能当真 PASS → 降为 None(BLOCKED)，交裁决器走 transient 退避重试。
            if ok and details.get("pipeline_blocked"):
                details["deterministic_gate"] = "skipped: pipeline blocked"
                details["not_run_kind"] = NotRunKind.BLOCKED.value
                return None, details
            # A3 治本(fail-closed)：沙箱内 pipeline 判 True，但本轮 pull-back 若有 skip(>1MiB)
            # 或 err(读失败) → 本地工作区/diff 不完整，"沙箱绿"不代表"本地交付完整"。此时禁止
            # 判 True → 降 None(BLOCKED) 走 transient 退避重试拉全，杜绝沙箱绿本地缺的静默假绿。
            if ok and (self._sync_skipped_count > 0 or self._sync_error_rels):
                logger.warning(
                    "[L1] pull-back 不完整(skipped=%d, errors=%d)但沙箱 pipeline 判过 → "
                    "拒绝判 PASS(降 BLOCKED 重试)，防沙箱绿本地 diff 缺改",
                    self._sync_skipped_count, len(self._sync_error_rels),
                )
                details["deterministic_gate"] = "skipped: pull-back incomplete"
                details["not_run_kind"] = NotRunKind.BLOCKED.value
                details["pullback_skipped"] = self._sync_skipped_count
                details["pullback_errors"] = len(self._sync_error_rels)
                return None, details
            details["deterministic_gate"] = "pass" if ok else "fail"
            if empty_diff:
                details["note"] = "empty diff，仅靠 harness 命令验证"
            return ok, details
        except Exception as exc:  # noqa: BLE001
            return None, {"deterministic_gate": f"skipped: pipeline error {exc}",
                          "not_run_kind": NotRunKind.BLOCKED.value}

    def _run_failing_test_gate(self, failing_cmd: str) -> tuple[bool, str]:
        """DEBUG 意图专属 L1 闸门：确定性执行 failing_test_command，验证修复后该命令通过。

        复用 l1_pipeline 的 _normalize_python_cmd + subprocess 机制，
        与现有 L1 确定性验证共享同样的执行模型（local / sandbox 均可）。
        返回 (bool, detail_str)：True=命令通过(bug 已修复)，False=命令仍失败。

        优雅降级：异常时返回 False（保守失败，M1 修复）——执行环境失败
        不能误判为 bug 已修复，宁可判未通过让其重试/人工复核。
        """
        # TD2606-C2：走 sandbox-first 的 _run_l1_command（与 L1 确定性闸门同执行模型）。
        # 原实现裸 local subprocess，在非 Python 栈(本地无 mvn/go/cargo 工具链)必 except →
        # DEBUG 意图任务的闸门【永远】保守失败、无法验证修复。沙箱可用即在沙箱跑、否则回退本地。
        from swarm.worker.l1_pipeline import _run_l1_command
        from swarm.worker.output_compress import compress_tool_output

        try:
            ec, out = _run_l1_command(failing_cmd, self.project_path, timeout=120)
            ok = ec == 0
            detail = f"exit_code={ec}, output={compress_tool_output(out or '', max_chars=800)}"
            return ok, detail
        except Exception as exc:  # noqa: BLE001
            # M1：执行环境异常 → 保守判失败（不能把"验证不了"当"已修复"放过未修坏代码）。
            self._log(f"DEBUG L1: failing_test_command 执行异常，保守判未通过: {exc}")
            return False, f"execution error (conservative fail): {exc}"

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
