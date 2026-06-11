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
            self.max_iterations = min(self.max_iterations, 12)
            self.max_fix_rounds = 0

        # 运行时状态
        self.phase = WorkerPhase.PREPARING
        self.start_time: float = 0.0
        self.execution_log: list[str] = []
        self.fix_rounds: int = 0
        self._agent: dict | None = None
        self._sandbox: Any | None = None
        self._sandbox_manager: Any | None = None
        # diff 基线/产出快照（difflib 生成 diff 用）。沙箱模式由 _sync_to/from_sandbox 填充，
        # 本地模式由 _snapshot_scope_local 填充。__init__ 初始化避免本地模式下属性缺失。
        self._pre_sync_contents: dict[str, str | None] = {}
        self._post_sync_contents: dict[str, str | None] = {}

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

        from swarm.tools.build_tools import clear_sandbox_context, set_sandbox_context

        try:
            # ── Phase 0: 准备 ──
            self.phase = WorkerPhase.PREPARING
            self._log("准备阶段：设置 Scope，创建 Agent")

            cfg = get_config()
            if cfg.sandbox.use_for_worker and cfg.sandbox.api_url:
                try:
                    from swarm.worker.sandbox import get_sandbox_manager

                    self._sandbox_manager = get_sandbox_manager()
                    self._sandbox = self._sandbox_manager.create(
                        project_id=self.project_id,
                        task_id=self.task_id or self.subtask.id,
                        source=f"worker:{self.subtask.id}",
                    )
                    set_sandbox_context(self._sandbox, self._sandbox_manager)
                    self._log(f"远程沙箱已创建: {self._sandbox.sandbox_id}")
                    await self._sync_to_sandbox("bootstrap")
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

                self._log(
                    f"L1 验证结果: {'通过 ✅' if l1_passed else '未通过 ❌'} "
                    f"| 来源: {l1_details.get('l1_decision_source')} | 详情: {l1_details}"
                )

                if l1_passed:
                    break

                if fix_round < self.max_fix_rounds:
                    self._log(f"修复尝试 {fix_round + 1}/{self.max_fix_rounds}")
                    fix_result = await self._run_agent(
                        self._build_fix_prompt(verify_result),
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

            # L1 确定性流水线（scope / compile / lint / scoped test / LLM 自检）
            if output.diff and self.project_path:
                from swarm.worker.l1_pipeline import run_l1_pipeline

                # 获取 LLM 句柄用于 L1.4 自检（可选，不传则自检跳过）
                l1_llm = None
                try:
                    from swarm.models.router import ModelRouter
                    l1_llm = ModelRouter().get_worker_llm(strategy="cost_optimized")
                except Exception as exc:
                    self._log(f"L1 自检 LLM 获取失败，跳过自检: {exc}")

                det_ok, det_details = run_l1_pipeline(
                    self.project_path,
                    self.subtask,
                    output.diff,
                    llm=l1_llm,
                )
                l1_details = {**l1_details, **det_details, "deterministic_l1": det_ok}
                if not det_ok:
                    l1_passed = False
                    output = output.model_copy(
                        update={"l1_passed": False, "l1_details": l1_details}
                    )
                elif det_ok and not l1_passed:
                    l1_passed = True
                    output = output.model_copy(
                        update={"l1_passed": True, "l1_details": l1_details}
                    )

            self.phase = WorkerPhase.DONE
            self._log(f"执行完成，置信度: {output.confidence.value}")

            return output

        except Exception as e:
            self.phase = WorkerPhase.FAILED
            self._log(f"执行异常: {e}")
            return self._make_output(
                diff="",
                summary=f"执行异常: {e}",
                confidence=Confidence.LOW,
                l1_passed=False,
                l1_details={"error": str(e)},
            )
        finally:
            clear_sandbox_context()
            clear_scope()
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
        """上传到沙箱的文件清单（readable ∪ writable(modify)，去重保序）。

        注意：排除 create_files——它们是【待新建】文件，本地不存在，强行 read/upload
        会 FileNotFoundError（曾导致 worker 把"新建 readme"当成读取不存在文件而卡住）。
        delete_files 也不上传（要删的没必要传）。
        """
        scope = self.effective_scope
        files: list[str] = []
        create = set(getattr(scope, "create_files", []) or [])
        delete = set(getattr(scope, "delete_files", []) or [])
        for f in list(getattr(scope, "readable", []) or []) + list(getattr(scope, "writable", []) or []):
            rel = str(f).strip()
            if rel and rel not in files and rel not in create and rel not in delete:
                files.append(rel)
        return files

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

    def _snapshot_scope_local(
        self, local_root: Path, files: list[str] | None = None
    ) -> dict[str, str | None]:
        """读取本地文件内容快照，作为 difflib diff 的基线/产出。

        files 为 None 时用 writable scope（diff 只关心可写文件的前后变化）。
        值为文件文本；不存在的文件记为空串；二进制/不可读记为 None。
        """
        rel_files = [
            self._norm_rel(local_root, f)
            for f in (files if files is not None else self._writable_files())
        ]
        snapshot: dict[str, str | None] = {}
        for rel in rel_files:
            lp = local_root / rel
            try:
                snapshot[rel] = lp.read_text("utf-8") if lp.is_file() else ""
            except (UnicodeDecodeError, OSError):
                snapshot[rel] = None
        return snapshot

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

        rel_files = [self._norm_rel(local_root, f) for f in self._scope_files()]
        if not rel_files:
            self._log(f"{reason} scope 为空，跳过文件上传（无目标文件）")
            return

        # 记录上传前内容快照（用于 diff 基线）
        for rel in rel_files:
            lp = (local_root / rel)
            try:
                self._pre_sync_contents[rel] = lp.read_text("utf-8") if lp.is_file() else ""
            except (UnicodeDecodeError, OSError):
                self._pre_sync_contents[rel] = None  # 二进制/不可读

        try:
            sync_stats = await asyncio.to_thread(
                self._sandbox_manager.sync_files_to_sandbox,
                self._sandbox,
                local_root,
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
            if old_text == new_text:
                continue
            old_lines = old_text.splitlines(keepends=True)
            new_lines = new_text.splitlines(keepends=True)
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
        """销毁远程沙箱"""
        if hasattr(self, "_sandbox") and self._sandbox is not None:
            try:
                self._sandbox_manager.kill(self._sandbox.sandbox_id)
                self._log(f"远程沙箱已销毁: {self._sandbox.sandbox_id}")
            except Exception as e:
                self._log(f"沙箱销毁失败: {e}")
            self._sandbox = None
            self._sandbox_manager = None

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

        # 提取最后一条 AI 消息
        messages = result.get("messages", [])
        if messages:
            last = messages[-1]
            return getattr(last, "content", str(last))
        return "(Agent 无输出)"

    def _scope_ops_hint(self) -> str:
        """生成给 LLM 的文件操作清单：明确哪些是修改/新建/删除。"""
        s = self.effective_scope
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

        l1_passed = "fail" not in combined.lower() and "❌" not in combined
        l1_details = {"mode": "trivial_fast", "agent_summary": combined[:500]}

        self.phase = WorkerPhase.PRODUCING
        self._log("产出阶段：从沙箱 pull-back 并收集 diff")
        await self._sync_from_sandbox("产出")
        produce_result = await self._run_agent(self._build_produce_prompt(), step="produce")
        output = self._parse_produce_result(produce_result, l1_passed, l1_details)
        self.phase = WorkerPhase.DONE
        self._log(f"trivial 快速路径完成，置信度: {output.confidence.value}")
        return output

    def _build_locate_prompt(self) -> str:
        return (
            "请开始 Phase 1（定位）：\n"
            "1. 阅读你权限范围内的相关文件\n"
            "2. 定位需要修改或实现的代码位置\n"
            "3. 确认接口契约和依赖关系\n"
            "请简要汇报你的定位结果。"
        )

    def _build_code_prompt(self, locate_result: str) -> str:
        return (
            "请开始 Phase 2（编码）：\n"
            f"定位结果: {locate_result}\n\n"
            "文件操作清单（务必按操作类型处理）：\n"
            f"{self._scope_ops_hint()}\n\n"
            "根据定位结果和子任务要求进行实现：\n"
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

    def _build_fix_prompt(self, verify_result: str) -> str:
        return (
            f"L1 验证未通过，结果：{verify_result}\n\n"
            "请分析失败原因并修复代码：\n"
            "1. 仔细阅读错误信息\n"
            "2. 定位问题根因\n"
            "3. 使用 patch_file 修复\n"
            "完成后请再次运行验证。"
        )

    def _build_produce_prompt(self) -> str:
        return (
            "请开始 Phase 4（产出）：\n"
            "1. 使用 git_diff 查看你的所有变更\n"
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

        details: dict = {
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
        if not diff or diff in ("(无变更)", "(无法获取 git diff)"):
            return None, {"deterministic_gate": "skipped: empty diff"}
        try:
            from swarm.worker.l1_pipeline import run_l1_pipeline

            ok, details = run_l1_pipeline(
                self.project_path, self.subtask, diff, llm=None
            )
            details["deterministic_gate"] = "pass" if ok else "fail"
            return ok, details
        except Exception as exc:  # noqa: BLE001
            return None, {"deterministic_gate": f"skipped: pipeline error {exc}"}


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
