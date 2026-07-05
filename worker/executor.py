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
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from swarm.config.settings import get_config
from swarm.git_base import resolve_base_ref
from swarm.models.errors import TransientInfraError
from swarm.paths import is_within_root
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
from swarm.worker.l1_verdict import (  # noqa: E402
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
# re-export 供内部（_reset_scope_to_head / _try_local_git_diff）与既有测试可寻址。
from swarm.worker.git_flock import (  # noqa: E402
    _ProjectGitFlock,
    _warn_git_flock_fail_open_once,
)


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

    def _apply_local_deletions(self, local_root: Path, exists_in_sandbox) -> list[str]:
        """A1 治本：把 worker 在沙箱里执行的删除【传播到本地工作树】。

        delete_files 不在 _writable_files（不上传/不拉回），历史上无任何机制把删除落到本地 →
        git diff 永远看不到删除 → 交付漏删 + 纯删除子任务恒空 diff 假绿。判据：scope 声明要删的
        文件，若【沙箱里已不存在】(worker 真删了)且【本地仍存在】→ 本地 unlink，使 git diff 如实
        显示删除；沙箱里仍在 = worker 没删 → 保留本地(diff 空)→ 上游 expects_changes 判未完成。

        exists_in_sandbox(rel)->bool 是【逐文件精确探测】(见 _sandbox_file_exists 的 test -f)。
        ★复核 CR-2 修正：绝不用 head-200 截断的全量列举比对——否则沙箱 >200 文件时位次 201+ 的
          文件虽仍在却被判"已删"→ 误 unlink 数据丢失(RuoYi 数百文件必触发)。
        ★复核 CR-4 修正：unlink 前强制 containment 到 local_root，`..` 越界路径拒删(unlink 不可逆)。
        探测失败保守视为"仍在"(不删)——删除是不可逆方向，宁可漏删触发重试，绝不误删。
        """
        deleted: list[str] = []
        scope = self.effective_scope
        for f in (getattr(scope, "delete_files", []) or []):
            rel = self._norm_rel(local_root, f)
            if not rel:
                continue
            lp = local_root / rel
            # CR-4：containment——解析后必须在 local_root 内，杜绝 `../x` 越界 unlink（A5 归一原语）。
            if not is_within_root(local_root, rel, join=True):
                self._log(f"删除路径越界（不在项目根内），拒删: {rel}")
                continue
            if exists_in_sandbox(rel):
                continue  # 沙箱里还在 → worker 没删 → 保留本地
            try:
                if lp.is_file():
                    lp.unlink()
                    self._deleted_local_paths.add(rel)
                    deleted.append(rel)
            except OSError as exc:
                logger.warning(
                    "删除传播失败 %s（保留本地，需核查权限/占用）: %s", rel, exc, exc_info=True)
        return deleted

    def _sandbox_file_exists(self, rel: str) -> bool:
        """A1(复核 CR-2)：逐文件精确探测沙箱是否仍有该文件(test -f)，替代 head-200 截断全量列举。
        无沙箱/探测失败 → 保守返回 True(视为仍在→不删)，绝不因抖动/截断误删本地文件。"""
        if not self._sandbox or not self._sandbox_manager:
            return True
        rc = getattr(self._sandbox_manager, "run_command", None)
        if rc is None:
            return True
        import shlex
        remote = get_config().sandbox.sandbox_remote_workdir
        # 复核 R23-4：shlex.quote 全路径（不再只剥 '/换行）——文件名含 $()/;/空格等不破坏引号边界。
        _qp = shlex.quote(f"{remote}/{rel}")
        try:
            result = rc(self._sandbox,
                        f"test -f {_qp} && echo __Y__ || echo __N__", timeout=15)
            return "__Y__" in (getattr(result, "stdout", "") or "")
        except Exception:  # noqa: BLE001
            return True  # 探测失败 → 保守不删

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
                ["git", "show", f"{resolve_base_ref(getattr(self, 'base_ref', None))}:{rel}"],
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

        # 候选：仅本子任务【会写】的文件（writable ∪ create_files；只 reset 已被 git 跟踪者）。
        # 根因修复(69d34b1b)：【不再 reset readable / 构建清单文件】——它们本子任务不写，却可能
        # 含【上游子任务的产物】(脚手架建的模块 pom、注册了新模块的父 pom)。把这些 reset 到 HEAD
        # 会抹掉上游改动 → 本子任务沙箱缺依赖 → `mvn -pl <module>` 报 reactor not found（实测）。
        candidates = set()
        for f in self._writable_files():
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

        # TD2606-B5/C5：reset 与 diff/add-N 共用同一 per-project 锁（_ProjectGitFlock），
        # 串行化所有 git 临界操作，杜绝并发 worker 在共享工作树/索引上互踩。
        try:
            with _ProjectGitFlock(local_root):
                r = subprocess.run(
                    ["git", "checkout", resolve_base_ref(getattr(self, 'base_ref', None)), "--", *tracked],
                    cwd=str(local_root), capture_output=True, text=True, timeout=30,
                )
            if r.returncode == 0:
                self._log(f"bootstrap 前 workspace reset：{len(tracked)} 个 tracked 文件恢复到钉扎 base（防跨轮脏叠加）")
                return len(tracked)
            self._log(f"workspace reset 警告（git checkout 非零）: {r.stderr.strip()[:200]}")
            return 0
        except Exception as exc:  # noqa: BLE001
            self._log(f"workspace reset 跳过（异常）: {exc}")
            return 0

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
            # 根因修复(69d34b1b)：自带源码模式默认不传 readable（baked 镜像=git HEAD 已有）。
            # 但【上游子任务改过/新建的文件】(脚手架建的模块 pom、注册了模块的父 pom)在本依赖
            # 子任务里常列为 readable，其本地内容 ≠ git HEAD（镜像里是旧版/没有）→ 不补传则本
            # 子任务沙箱看不到上游产物 → `mvn -pl <module>` 报 reactor not found。
            # 判据：readable 文件【本地存在】且【本地内容 ≠ git HEAD 版】= 被上游改动 → 补传。
            _seen = set(rel_files)
            _extra: list[str] = []
            if (local_root / ".git").exists():
                import subprocess as _sp
                for f in (getattr(self.effective_scope, "readable", []) or []):
                    rel = self._norm_rel(local_root, f)
                    if rel in _seen:
                        continue
                    abs_p = local_root / rel
                    if not abs_p.is_file():
                        continue
                    try:
                        in_head = _sp.run(
                            ["git", "cat-file", "-e", f"{resolve_base_ref(getattr(self, 'base_ref', None))}:{rel}"],
                            cwd=str(local_root), capture_output=True, timeout=10,
                        ).returncode == 0
                    except Exception:  # noqa: BLE001
                        continue
                    if not in_head:
                        # 上游新建（base 无、本地有，如脚手架建的模块 pom）→ 补传
                        rel_files.append(rel)
                        _seen.add(rel)
                        _extra.append(rel)
                        continue
                    # 在 HEAD：比对内容，本地 ≠ HEAD = 上游改动（如父 pom 注册了模块）→ 补传
                    head_text = self._git_baseline_text(local_root, rel)
                    if head_text is None:
                        continue
                    try:
                        local_text = abs_p.read_text(encoding="utf-8")
                    except (UnicodeDecodeError, OSError):
                        continue
                    if local_text != head_text:
                        rel_files.append(rel)
                        _seen.add(rel)
                        _extra.append(rel)
                # FINDING-11(task 0847c303)：build-critical 清单(root/模块 pom、settings/build.gradle)
                # 任何 `mvn -pl`/reactor 构建都隐式依赖父 pom 的 <modules> 注册，但这些文件常【不在本
                # 子任务 scope】(上面 readable 循环漏掉)→ 上游脚手架注册的父 pom 不传到本沙箱 → reactor
                # not found(实测 st-3 跨 replan/retry 全败)。故【始终】补传变更的 build 清单(local≠HEAD)，
                # 不限 scope——是 69d34b1b 修复的泛化(从"传 scope 内变更"扩到"额外始终传 build-critical")。
                _BUILD_MANIFESTS = (
                    "pom.xml", "settings.gradle", "build.gradle",
                    "settings.gradle.kts", "build.gradle.kts",
                )
                try:
                    _ch = _sp.run(
                        ["git", "diff", "--name-only", resolve_base_ref(getattr(self, 'base_ref', None))],
                        cwd=str(local_root), capture_output=True, text=True, timeout=15,
                    ).stdout.splitlines()
                    _ut = _sp.run(
                        ["git", "ls-files", "--others", "--exclude-standard"],
                        cwd=str(local_root), capture_output=True, text=True, timeout=15,
                    ).stdout.splitlines()
                except Exception:  # noqa: BLE001
                    _ch, _ut = [], []
                for rel in (_ch + _ut):
                    rel = (rel or "").strip()
                    if not rel or rel in _seen:
                        continue
                    if rel.rsplit("/", 1)[-1] not in _BUILD_MANIFESTS:
                        continue
                    if not (local_root / rel).is_file():
                        continue
                    rel_files.append(rel)
                    _seen.add(rel)
                    _extra.append(rel)
            if _extra:
                self._log(
                    f"{reason} 自带源码：补传 {len(_extra)} 个上游产物(本地≠HEAD，如模块/父 pom): {_extra[:5]}"
                )
            self._log(
                f"{reason} 专属沙箱自带源码 → 上传 {len(rel_files)} 个文件（改动 + 上游产物）"
            )
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
            # N-06：bootstrap 上传失败若吞掉，agent 会对【缺文件的沙箱】空跑→被误判能力失败
            # （空 diff）→错误触发换模型。这是基础设施瞬时失败，显式抛 TransientInfraError →
            # run() 归类 transient → 退避重试同模型（自愈）。
            self._log(f"{reason} 本地→沙箱精准上传失败（infra 瞬时，将退避重试）: {sync_exc}")
            raise TransientInfraError(
                f"sandbox upload failed ({reason}): {sync_exc}"
            ) from sync_exc
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
        # TD2606-C9：闸门在沙箱里确定性修复的文件（含 scope 外，如父 pom）也要回传。
        extra_repaired = sorted(self._repaired_extra_paths)
        if not self._sandbox or not self._sandbox_manager:
            # 本地模式：直接快照本地 writable 文件（agent 已就地修改）+ 被修复文件
            self._post_sync_contents = self._snapshot_scope_local(
                local_root, files=self._writable_files() + extra_repaired
            )
            await self._normalize_jvm_namespace(local_root, reason)
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
        # 并入被确定性修复的文件（去重保序），使其无论是否在写权 scope 内都被拉回本地。
        if extra_repaired:
            rel_files = list(dict.fromkeys(
                rel_files + [self._norm_rel(local_root, p) for p in extra_repaired]
            ))
        # ★H-exec1 治本(round21 假绿门)★：worker 常自建【未声明】的同包 helper/config/枚举/内部类——
        # 在沙箱编过→L1 绿，但只回传【声明 scope】会漏掉它们→本地树缺→MERGE/集成期 cannot find symbol
        # (L1 假绿+产物不落盘)。故在【声明文件的父目录】下按源扩展名枚举沙箱里【本地尚无】的新文件，
        # 纳入回传。有界(仅子任务自己的包目录 + 只补本地缺失文件，不拉全仓/构建产物/不碰既有文件)。
        if rel_files and not getattr(self.effective_scope, "allow_any", False):
            try:
                _decl_dirs = {
                    str(Path(f).parent).replace("\\", "/")
                    for f in rel_files if "/" in f.replace("\\", "/")
                }
                if _decl_dirs:
                    _all_sb = await asyncio.to_thread(self._list_sandbox_workspace_files)
                    _SRC_EXT = (".java", ".kt", ".kts", ".go", ".rs", ".ts", ".tsx",
                                ".js", ".jsx", ".vue", ".py", ".xml", ".sql", ".proto")
                    _rel_set = set(rel_files)
                    _extra_new = [
                        f for f in _all_sb
                        if f not in _rel_set
                        and f.lower().endswith(_SRC_EXT)
                        and any(f.replace("\\", "/").startswith(d + "/") for d in _decl_dirs)
                        and not (local_root / f).exists()  # 只补本地【尚无】的新文件，不碰既有
                    ]
                    if _extra_new:
                        rel_files = list(dict.fromkeys(rel_files + _extra_new))
                        self._log(
                            f"{reason} H-exec1：纳入 {len(_extra_new)} 个未声明沙箱新建源文件"
                            f"(同包，防 L1 绿但产物不落盘): {_extra_new[:5]}"
                        )
            except Exception as _hexc:  # noqa: BLE001
                self._log(f"{reason} H-exec1 枚举沙箱新增文件失败(非致命): {_hexc}")
        # A1：删除传播——必须在 rel_files 空的 early-return 之前，纯删除 scope 才不被跳过。
        # 复核 CR-2 修正：逐文件 test -f 精确探测(不再 head-200 截断全量列举比对，杜绝误删)。
        if getattr(self.effective_scope, "delete_files", []):
            try:
                _deleted = await asyncio.to_thread(
                    self._apply_local_deletions, local_root, self._sandbox_file_exists)
                if _deleted:
                    self._log(f"{reason} 删除传播：worker 已在沙箱删除 → 本地同步删除 {_deleted}")
            except Exception as _dexc:  # noqa: BLE001
                self._log(f"{reason} 删除传播失败（非致命）: {_dexc}")
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
            # A3：记录本轮 pull-back 完整性信号（skip/err），供 L1 闸门 fail-closed。
            self._sync_skipped_count = int(sync_stats.get("skipped") or 0)
            self._sync_error_rels = list(sync_stats.get("errors") or [])
            err_count = len(sync_stats.get("errors") or [])
            self._log(
                f"{reason} 沙箱→本地精准 pull-back: "
                f"downloaded={sync_stats.get('downloaded', 0)}, "
                f"errors={err_count}"
            )
            for err in (sync_stats.get("errors") or [])[:5]:
                self._log(f"pull-back 警告: {err}")
            await self._normalize_jvm_namespace(local_root, reason)
        except Exception as sync_exc:
            # N-07：pull-back 失败若吞掉，成功执行的产出拉不回来→diff 空→报"无变更"→
            # 错误触发换模型降级。这是基础设施瞬时失败，显式抛 → run() 归类 transient → 退避重试。
            self._log(f"{reason} 沙箱→本地 pull-back 失败（infra 瞬时，将退避重试）: {sync_exc}")
            raise TransientInfraError(
                f"sandbox pull-back failed ({reason}): {sync_exc}"
            ) from sync_exc

    async def _normalize_jvm_namespace(self, local_root: Path, reason: str) -> None:
        """确定性 Jakarta/Javax 命名空间归一（治本：短路模型复读死循环）。

        worker 写代码后 pull-back 到本地，这里据 project_stack 的权威命名空间把改动文件里
        【写错的】Jakarta EE 包前缀（如本项目用 jakarta 却写成 javax.servlet）确定性改对，
        并把改过的文件【回写本地 + 重新上传沙箱】，使随后的 L1 build 闸门在沙箱里直接编过，
        不再让本地小模型对着 `package javax.servlet does not exist` 空转到迭代上限。
        - 仅当 project_stack.jvm.servlet_namespace ∈ {jakarta,javax} 时生效；非 JVM/未判明→no-op。
        - 只动 .java 文件、只改整包迁移的 Jakarta EE 前缀（见 rewrite_jvm_namespace），JDK 自带
          的 javax.*（sql/crypto/naming…）一律不碰。SWARM_WORKER_JVM_NS_FIX=false 可关。
        """
        if os.environ.get("SWARM_WORKER_JVM_NS_FIX", "true").lower() in ("false", "0", "no"):
            return
        contents = getattr(self, "_post_sync_contents", None)
        if not contents:
            return
        profile = self._resolve_project_stack() or {}
        target_ns = (profile.get("jvm") or {}).get("servlet_namespace")
        if target_ns not in ("jakarta", "javax"):
            return
        from swarm.worker.l1_pipeline import rewrite_jvm_namespace

        fixed: dict[str, int] = {}
        for rel, text in list(contents.items()):
            if not rel.endswith(".java") or not isinstance(text, str):
                continue
            new_text, n = rewrite_jvm_namespace(text, target_ns)
            if n <= 0:
                continue
            other = "javax" if target_ns == "jakarta" else "jakarta"
            # 回写本地（diff 源）+ 更新快照
            try:
                lp = (local_root / rel)
                lp.parent.mkdir(parents=True, exist_ok=True)
                data = new_text.encode("utf-8")
                data = self._sandbox_manager._preserve_line_endings(lp, data) \
                    if self._sandbox_manager else data
                lp.write_bytes(data)
                contents[rel] = new_text
                fixed[rel] = n
            except OSError as exc:
                self._log(f"{reason} 命名空间归一回写本地失败 {rel}: {exc}")
        if not fixed:
            return
        self._log(
            f"{reason} 命名空间确定性归一（→{target_ns}.*，治本短路死循环）："
            + ", ".join(f"{r}×{c}" for r, c in list(fixed.items())[:5])
            + (f" 等 {len(fixed)} 文件" if len(fixed) > 5 else "")
        )
        # 沙箱模式：把改对的文件重新上传，使 L1 build 在沙箱里见到 jakarta 版
        if self._sandbox and self._sandbox_manager:
            try:
                cfg = get_config()
                await asyncio.to_thread(
                    self._sandbox_manager.sync_files_to_sandbox,
                    self._sandbox,
                    local_root,
                    list(fixed.keys()),
                    cfg.sandbox.sandbox_remote_workdir,
                )
            except Exception as exc:  # noqa: BLE001
                self._log(f"{reason} 命名空间归一回传沙箱失败（不致命，build 闸门会暴露）: {exc}")

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
        """生成子任务改动的 unified diff。

        ── 优先用【本地 git diff】(task 1a49aa66 治本)──
        diff 在【本地】生成(worker 在沙箱改文件→pull-back 写回本地→在此比对)。
        若本地 project_path 是 git 仓库(本机开发的真实项目几乎都是)，直接用 `git diff`
        生成——它与 git apply 同源，产出的补丁【必被 git apply 接受】，从根上消除 difflib
        手工拼 unified diff 的格式错乱(hunk 行数/前导符错位→"补丁损坏")。
        仅当无 git 仓库(greenfield/无 .git)时回退到 difflib(已修正 keepends/lineterm 用法)。

        基线 = HEAD（项目模板/本地工作区的干净基线）；新值 = pull-back 后的工作区当前内容。
        """
        # ── 路径1：本地 git 仓库 → git diff（治本，必被 git apply 接受）──
        git_diff = self._try_local_git_diff()
        if git_diff is not None:
            return git_diff if git_diff.strip() else "(无变更)"

        # ── 路径2：difflib fallback（无 git 仓库时）──
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
            # ── 关键修复(task 1a49aa66)：difflib unified_diff 的正确用法 ──
            # 实测唯一能被 git apply 接受的组合：splitlines(keepends=True)[内容行自带\n] +
            # lineterm=""[difflib 不给 hunk头/文件头加换行] + 逐元素规范化补换行 + "".join。
            # 旧代码 keepends=True + lineterm="" + "\n".join 会给本已含\n的内容行再加\n（行尾翻倍）；
            # 而 keepends=False + lineterm="\n" 会让内容行【没有】换行符（全挤一行）。两者都让
            # git apply 报"补丁损坏"。下面的 normalize 方案兼顾：内容行用自带\n，头部行补\n。
            old_lines = old_norm.splitlines(keepends=True)
            new_lines = new_norm.splitlines(keepends=True)
            ud = difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                lineterm="",
            )
            # 逐元素规范化：hunk头/文件头(lineterm="" 故无换行)补\n；内容行(keepends 已含\n)不动。
            block = "".join(x if x.endswith("\n") else x + "\n" for x in ud)
            block = block.rstrip("\n")
            if block.strip():
                diff_parts.append(block)

        if not diff_parts:
            return "(无变更)"
        return "\n".join(diff_parts)

    def _try_local_git_diff(self) -> str | None:
        """本地 git 仓库 → 用 git diff 生成子任务 scope 文件的 unified diff。

        返回 None 表示不可用（无 project_path / 非 git 仓库 / git 调用失败）→ 上层回退 difflib。
        返回 "" 或 diff 文本表示成功（""=无变更）。

        关键：worker 已把沙箱改动 pull-back 写回本地工作区，所以工作区当前内容就是改动后状态，
        git diff 基线为 HEAD。只 diff 子任务 scope 文件（writable∪create∪delete），避免把
        .codegraph/ 等无关变更带进来。新建文件用 `git diff --no-index /dev/null <file>` 或
        `git add -N` 让其出现在 diff 中。
        """
        import subprocess as _sp
        from pathlib import Path as _P

        root = getattr(self, "project_path", None)
        if not root:
            return None
        root = str(_P(root).resolve())
        if not (_P(root) / ".git").exists():
            return None

        scope = self.effective_scope
        # scope 内所有受影响文件（相对路径）
        modify = [f for f in (getattr(scope, "writable", []) or []) if f]
        create = [f for f in (getattr(scope, "create_files", []) or []) if f]
        delete = [f for f in (getattr(scope, "delete_files", []) or []) if f]
        # TD2606-C9：把闸门在沙箱里修复的文件（含 scope 外，如父 pom）纳入 diff，
        # 否则修复进了本地工作区却因不在 scope 而被 `-- <files>` 过滤掉 → merged_diff 缺失。
        repaired = [f for f in sorted(self._repaired_extra_paths) if f]
        targets = list(dict.fromkeys(modify + create + delete + repaired))
        if not targets:
            return None

        try:
            # 让新建/未跟踪文件也能进 git diff：对 create_files 做 intent-to-add（-N，不暂存内容，
            # 仅登记路径，使 git diff 能显示其全部新增行）。幂等、无副作用（不真正 commit）。
            untracked = []
            for f in targets:
                p = _P(root) / f
                if p.is_file():
                    # 是否已跟踪
                    r = _sp.run(["git", "-C", root, "ls-files", "--error-unmatch", f],
                                capture_output=True, text=True)
                    if r.returncode != 0:
                        untracked.append(f)
            # TD2606-B5/C5/M5：add -N（改共享 index）+ diff 必须在同一 per-project 锁内原子完成，
            # 否则并发 worker 的 intent-to-add 互相泄漏进对方 diff、与他人 reset/diff 互踩。
            # 锁内只放这两条短命 git 命令；ls-files 探测（只读）与 diff 结果处理在锁外。
            with _ProjectGitFlock(root):
                # ── 主干A 治本（并行子任务共享聚合态）──
                # 根因：pull-back 把产物写回【共享】project_path 工作区（一任务一份，N 个并行
                # worker 共用），而本路径取"工作区当前内容"作 diff 新值。多写者对同一聚合文件
                # （根 pom / settings.gradle / Cargo.toml…）last-write-wins 互相覆盖 → 谁后写、
                # 谁的内容进了别人的 diff，被覆盖者的 +<module>/+<dependency> 直接从 diff 丢失，
                # 下游 MERGE 并集（Lever A）也救不回【从未被任何 diff 捕获】的成员。
                # 不变量：worker 的 diff 必须是 (HEAD, 本 worker 自己 pull-back 的产出) 的纯函数，
                # 与其他 worker 无关。锁内先用本 worker 的 _post_sync_contents 把自己的 scope 文件
                # 重置回自己的产出，再 diff——把 diff 对【长生命周期共享工作区】的依赖切断，concurrent
                # 写者无法在"重置→diff"这段持锁原子区内插进来。仅重置本 subtask owns 的 targets，
                # 不碰他人文件；二进制(None)/删除(缺键)/未产出则保留工作区现状（退化为原行为）。
                _own = getattr(self, "_post_sync_contents", None) or {}
                for _f in targets:
                    _txt = _own.get(_f)
                    if not isinstance(_txt, str):
                        continue
                    try:
                        _lp = _P(root) / _f
                        _lp.parent.mkdir(parents=True, exist_ok=True)
                        # _txt 来自 _post_sync_contents：其字节在 pull-back 时已过 _preserve_line_endings
                        # （与本地/HEAD 同行尾），decode 成字符串后行尾已正确，直接 encode 写回即同源。
                        # 【不再】二次对磁盘采样判 CRLF——持锁前磁盘可能是别的 worker 的覆盖版，采它会
                        # 误判行尾、给本 worker 的 diff 引入伪 CRLF 噪声（评审 MEDIUM，治本：不依赖共享磁盘）。
                        _lp.write_bytes(_txt.encode("utf-8"))
                    except OSError as _wexc:
                        self._log(f"主干A 自产出重置落盘失败 {_f}（退化读工作区现状）: {_wexc}")
                if untracked:
                    _sp.run(["git", "-C", root, "add", "-N", *untracked],
                            capture_output=True, text=True, timeout=30)
                r = _sp.run(
                    ["git", "-C", root, "diff", "--no-color",
                     resolve_base_ref(getattr(self, 'base_ref', None)), "--", *targets],
                    capture_output=True, timeout=60,  # 注意：不传 text=True，拿原始 bytes
                )

            # 生成 diff：钉扎 base 基线 vs 工作区当前（含 pull-back 的改动 + -N 的新文件）。
            # 3rd#2：显式相对 base（None→HEAD），与 merge base_reader 同源对齐，消除运行期 HEAD 漂移。
            # --no-color 防 ANSI；-- <files> 限定 scope，不带入无关变更。
            # 行尾一致性由 pull-back 的 _preserve_line_endings 保证（CRLF 项目写回仍 CRLF），
            # 工作区与 git HEAD 同行尾 → git diff 不会全文 churn、产出的 context 行带正确行尾，
            # git apply 同源必成功。故【不再用 --ignore-cr-at-eol】(那会产 LF context 反而和
            # CRLF 的 HEAD 对不上，task f20ea68d 实测 git apply --ignore-whitespace 都救不了)。
            # 【关键(task f20ea68d 根因)】用 bytes 模式读 git diff，不能用 text=True！
            # text=True 触发 Python universal-newlines，会把 git diff 输出里 CRLF 文件的
            # context 行尾 \r\n 静默转成 \n → diff 丢失 \r → git apply 回 CRLF 的 HEAD 时
            # context 字节不匹配（实测 --ignore-whitespace/--3way 都救不了）。bytes 模式
            # 保留 \r，产出与 CRLF 源文件完全同源的 diff，git apply 直接成功（无需任何忽略参数）。
            # （diff 已在上方 _ProjectGitFlock 锁内执行，结果即 r。）
            if r.returncode != 0:
                _err = (r.stderr or b"").decode("utf-8", "replace")
                self._log(f"git diff 失败(rc={r.returncode})，回退 difflib: {_err[:120]}")
                return None
            # 解码保留行尾：用 decode 不做 newline 转换（bytes→str 不触发 universal newlines）
            diff = (r.stdout or b"").decode("utf-8", "replace")
            # 删除文件：git diff 已能体现（工作区文件被删 → diff 显示删除）。
            self._log(f"diff 来源: 本地 git diff（{len(targets)} 个 scope 文件，行尾同源，git apply 直通）")
            # 仅去掉【整个 diff 末尾】的多余空行，不碰行内 \r（rstrip 只删尾部 \n，\r 在行内不受影响）
            return diff.rstrip("\n")
        except Exception as e:  # noqa: BLE001
            self._log(f"git diff 异常({str(e)[:80]})，回退 difflib")
            return None

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

    def _context_snippets_block(self) -> str:
        """方案A(task 34fab09e)：ELABORATE 预注入的 scope 文件代码片段。
        worker 直接据此编写，无需在沙箱里 cat 探索耗尽迭代步数。无则返回空串。"""
        snip = getattr(self.subtask, "context_snippets", "") or ""
        if not snip.strip():
            return ""
        return f"\n\n📎 预读代码上下文（已为你读好，直接据此实现，无需再 cat 探索）：\n{snip}\n"

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

    def _build_locate_prompt(self) -> str:
        return (
            "请开始 Phase 1（定位）：\n"
            "1. 阅读你权限范围内的相关文件\n"
            "2. 定位需要修改或实现的代码位置\n"
            "3. 确认接口契约和依赖关系\n"
            "⚠️ 上下文有限：大文件务必用 read_file(path, start_line=N, end_line=M) 只读需要的"
            "行范围，或先 search_files 定位行号再局部读。禁止对大文件无参数读全文（会撑爆上下文）。\n"
            "✅ 若下方已提供【预读代码上下文】，优先据此定位，能不 cat 就不 cat（省步数预算）。\n"
            "请简要汇报你的定位结果。"
            + self._context_snippets_block()
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
            "4. 确保修改符合接口契约，保持代码风格一致\n\n"
            "⚠️ 本阶段【只管把目标文件改对】，禁止运行 mvn/gradle/npm 等重型构建或测试命令"
            "（编译和测试由后续 Phase 3 / 系统确定性 L1 闸门统一负责）。反复跑构建会耗光步数"
            "预算导致任务失败。改完目标文件即【立即停止】并确认改动，不要反复读取/编译/自我怀疑。\n"
        )

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

    def _build_batch_code_prompt(
        self, locate_result: str, batch: list[str], done: list[str], idx: int, total: int
    ) -> str:
        """B2 分批编码 prompt：只聚焦本批文件，已完成的不重做。"""
        done_hint = f"\n已完成文件（勿重做）：{done}\n" if done else "\n"
        return (
            f"请开始 Phase 2 编码（分批 {idx}/{total}）：\n"
            f"定位结果: {locate_result[:400]}\n"
            f"{done_hint}"
            f"\n🎯 本批【只】负责这些文件，其它文件本批不要碰：\n{batch}\n\n"
            "处理规则：\n"
            "1. 【修改】用 read_file(局部行范围) 后 patch_file 最小改动\n"
            "2. 【新建】直接 write_file 写完整内容（不要先 read_file）\n"
            "3. 确保符合接口契约、与已完成文件协调一致、保持代码风格\n"
            "⚠️ 只写本批文件即【立即停止】，禁止跑 mvn/gradle/npm 构建测试（L1 闸门统一负责），"
            "不要反复读取/自我怀疑（省步数预算）。\n"
            + self._context_snippets_block()
        )

    def _build_verify_prompt(self) -> str:
        # ── 根因修复(task 51c8e1f8)：medium/complex 路径的 worker 自验证绕圈 ──
        # 旧 prompt 让 worker 自己 run_compile + run_tests，但系统的确定性 L1 闸门
        # worker 再自己反复跑 mvn compile/test 是【纯多余的绕圈】：在复杂项目(RuoYi junit
        # 环境)测试跑不起来时，worker 会反复 mvn test + 查 junit 依赖，耗尽迭代上限(50)，
        # 即使实现代码本身 mvn compile=exit0(对的)也被拖死。
        # 修复：worker 只【自查改动是否完整】(读回改的文件确认)，编译/测试由系统确定性闸门负责。
        # 与 trivial 路径"禁止自跑 mvn"一致。worker 是开发，不是测试工程师。
        return (
            "请开始 Phase 3（自查）：\n"
            "1. 简要 review 你本轮的改动是否【完整覆盖】子任务要求（可 read_file 看几眼改过的文件）。\n"
            "2. 确认没有明显语法错误（凭阅读判断，不要运行构建）。\n\n"
            "⚠️【禁止运行重型构建/测试命令】：不要跑 mvn compile / mvn test / gradle / npm 等。\n"
            "编译和测试由系统的确定性 L1 闸门统一负责（系统会真跑一次编译+harness 测试），\n"
            "你自己反复跑会耗光步数预算导致任务失败。改动完整即【立即停止】。\n"
            "报告格式：L1_RESULT: PASS（你认为改动完整）或 L1_RESULT: FAIL（发现改漏/改错），然后简述。"
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

    async def _symbol_grounding_hint(self, verify_result: str, l1_details: dict | None) -> str:
        """路径1 治本：编译报 cannot find symbol → codegraph 解析真实 FQN 提示。

        只在出现 cannot find symbol 时触发；offload 到线程不阻塞执行 loop；全程 try 包裹 +
        service 层自身吞异常——接地是【增益】，绝不能因它让修复回路崩或卡。
        """
        try:
            digest = self._l1_failure_digest(l1_details or {})
            evidence = digest or verify_result or ""
            if "cannot find symbol" not in evidence:
                return ""
            import asyncio as _asyncio

            from swarm.knowledge.service import resolve_symbols_sync
            sc = getattr(self.subtask, "scope", None)
            create_files = list(getattr(sc, "create_files", []) or []) if sc else []
            class_hint = await _asyncio.to_thread(
                resolve_symbols_sync, evidence, self.project_id or "", create_files
            )
            # P5：臆造【方法】接地——class-FQN 解析接不住"真实类上调不存在的方法"
            # （Base64.encodeToByte），沙箱 javap 取真实方法集喂模型。
            method_hint = await _asyncio.to_thread(self._javap_method_grounding, evidence)
            return "\n\n".join(p for p in (class_hint, method_hint) if p)
        except Exception:  # noqa: BLE001
            return ""

    def _javap_method_grounding(self, evidence: str) -> str:
        """P5（治本，996db614 实测 18×900s 主因之一）：编译报 `cannot find symbol: method X /
        location: class C`（在真实存在的类上调臆造方法）→ 沙箱内 `javap C` 取 C 真实方法集，
        生成"C 真实方法有 [...]，X 不存在，从中选"提示，杜绝模型反复臆造方法烧满 900s。

        JDK 类（java.*/javax.*）javap 无需 classpath 直接解析；非 JDK/javap 失败优雅跳过（增益层，
        绝不阻断）。symbol-repair 的近邻纠错接不住此类（无项目近邻），codegraph 也跳过 method。"""
        try:
            if not self._sandbox or not self._sandbox_manager:
                return ""
            from swarm.worker.symbol_resolver import (
                build_method_grounding,
                parse_javap_methods,
                parse_missing_methods,
                to_javap_class_name,
            )
            pairs = parse_missing_methods(evidence)
            if not pairs:
                return ""
            rc = getattr(self._sandbox_manager, "run_command", None)
            if rc is None:
                return ""
            remote = get_config().sandbox.sandbox_remote_workdir
            # R2（治本，996db614 实证 CipherUtils 类幻觉）：javap 无 -cp 解析不了【项目类】(CipherUtils
            # 在 ruoyi-common)/【第三方库类】(RedisTemplate/Jwts/StrUtil 在依赖 jar)→ 空输出→无接地→
            # 模型打地鼠猜方法名。组【完整 classpath】让任意 classpath 上的类都可 javap：
            #   ① 项目类 = 各模块 target/classes（-am compile 后已存在）；
            #   ② 第三方类 = mvn dependency:build-classpath 导出依赖 jar 全集（deps 已在 ~/.m2，本地解析快）。
            # 合并去重写入沙箱临时文件一次，各 javap 复用。mvn 不可用/失败→优雅降级到仅 target/classes。
            cp_build = (
                f"cd {remote} 2>/dev/null && rm -f /tmp/swarm_dep_cp.txt 2>/dev/null; "
                f"mvn -q dependency:build-classpath -Dmdep.outputFile=/tmp/swarm_dep_cp.txt "
                f"-Dmdep.appendOutput=true >/dev/null 2>&1; "
                f"{{ find . -path '*/target/classes' -type d 2>/dev/null; "
                f"tr ':' '\\n' < /tmp/swarm_dep_cp.txt 2>/dev/null; }} "
                f"| sort -u | tr '\\n' ':' > /tmp/swarm_javap_cp.txt"
            )
            rc(self._sandbox, cp_build, timeout=150)
            probed: list[tuple[str, str, list[str]]] = []
            seen_classes: set[str] = set()
            for method, klass in pairs[:5]:
                if klass in seen_classes:
                    continue
                seen_classes.add(klass)
                bin_name = to_javap_class_name(klass)
                import shlex
                cmd = (
                    f"cd {shlex.quote(remote)} 2>/dev/null && "
                    f"javap -cp \"$(cat /tmp/swarm_javap_cp.txt 2>/dev/null).\" "
                    f"-public {shlex.quote(bin_name)} 2>/dev/null | head -80"
                )
                result = rc(self._sandbox, cmd, timeout=30)
                methods = parse_javap_methods(getattr(result, "stdout", "") or "")
                if methods:
                    probed.append((method, klass, methods))
            return build_method_grounding(probed)
        except Exception:  # noqa: BLE001
            return ""

    def _build_fix_prompt(
        self, verify_result: str, l1_details: dict | None = None, symbol_hint: str = ""
    ) -> str:
        # I4：优先用确定性失败证据（真实 compile/lint/scope，已压缩），回退 LLM 自报
        digest = self._l1_failure_digest(l1_details or {})
        evidence = digest if digest else verify_result
        # 路径1 治本：编译报 cannot find symbol 时，附 codegraph 解析的真实 FQN，
        # 让 worker 照真实位置改 import，而非再猜包名（RUN20 主导缺陷类）。
        grounding = f"\n\n{symbol_hint}" if symbol_hint else ""
        return (
            f"L1 验证未通过，确定性失败证据：\n{evidence}{grounding}\n\n"
            "请分析失败原因并修复代码：\n"
            "1. 仔细阅读上面的错误信息（这是真实的编译/lint/scope 检查结果）\n"
            "2. 定位问题根因（若有【符号接地提示】，照其给出的真实 FQN 修正引用，勿臆造包名）\n"
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
