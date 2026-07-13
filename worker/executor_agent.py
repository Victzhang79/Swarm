"""Worker Agent 循环 / 栈画像混入 —— 从 worker/executor.py 抽出（round26 god-file 治理 Step4）。

AGENT 连通分量（4 方法）：worker agent 构建（_create_agent）、技术栈画像解析+进程级缓存
（_resolve_project_stack）、剩余墙钟预算（_remaining_seconds）、单次 agent 运行（_run_agent，
含 cancel/超时透传）。跨簇仅调 self._log（核心类，MRO 解析）；禁 eager import worker.executor
（防 A6 环）——create_worker_agent/set_worker_context/stack_detect/tracing 等保持方法内 lazy。
进程级栈画像缓存 _PROJECT_STACK_CACHE 只被 _resolve_project_stack 使用，随本簇迁入。
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


def _budget_banner(limit: int, remaining_s: float) -> str:
    """E4：本次调用的步数/时间预算横幅（拼在 human_message 头部）。

    静态劝诫（executor_prompts「省步数预算」）对模型不可见具体数字——预算数字化
    可见才能让模型自己收敛探索（register #34：定位期 22 次必撞 cap）。纯文本零状态。"""
    return (f"【预算】本次调用迭代上限 {int(limit)} 步、剩余时间 {remaining_s:.0f}s。"
            "预算内完不成宁可先交部分产出；勿重复浏览已读文件。\n\n")

# 进程级技术栈画像缓存（按 project_path/project_id）：避免每个子任务重复扫盘探测栈。
_PROJECT_STACK_CACHE: dict[str, dict | None] = {}


class _AgentLoopMixin:
    """WorkerExecutor 的 agent 构建/运行 + 栈画像方法簇（见模块 docstring）。不持有自身状态。"""

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
        # E4（round38c 主题E，register #34）：预算动态注入——此前只有静态「省步数」劝诫，
        # 模型对具体预算不可见（定位期 22 次必撞 cap）。数字可见让模型自己收敛探索。
        human_message = _budget_banner(_limit, remaining) + human_message
        # C7/R41-4（round48c 实锤 37 次≈2.05M 虚增）：硬杀的在飞 LLM 调用不触发
        # on_llm_error → 预留挂 30min TTL 才高估结算。作用域令牌让本步被 cancel 的
        # 预留【即时】按中止语义结算；正常路径残留为空（settle 时已注销）。
        from swarm.models import ledger as _ledger
        _scope = _ledger.begin_inflight_scope()
        try:
            result = await asyncio.wait_for(
                agent.ainvoke(
                    {"messages": [("human", human_message)]},
                    config=invoke_config,
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            _ledger.end_inflight_scope(_scope, settle_leaked=True)
            self._log(f"Agent 调用超时（剩余预算 {remaining:.0f}s）")
            return f"❌ Agent 调用超时（预算 {self.max_execution_time}s）"
        except asyncio.CancelledError:
            _ledger.end_inflight_scope(_scope, settle_leaked=True)
            raise
        except BaseException:  # noqa: BLE001 — KeyboardInterrupt 级：清作用域防内存泄漏
            _ledger.end_inflight_scope(_scope, settle_leaked=True)
            raise
        except Exception as exc:
            # GraphRecursionError 等：agent 撞迭代上限。它在沙箱里已做的改动仍有效，
            # 不当作硬失败——后续 pull-back + 确定性 L1 闸门会按真实文件状态裁决
            # (与"子代理撞步数上限但已产出部分工作"同理，让确定性验证说话)。
            cls = type(exc).__name__
            if "Recursion" in cls or "recursion" in str(exc).lower():
                _ledger.end_inflight_scope(_scope, settle_leaked=False)
                self._log(f"Agent 撞迭代上限({_limit})，以沙箱实际产出为准交确定性闸门裁决")
                return f"⚠️ Agent 达到迭代上限（{_limit}），已做改动交由确定性 L1 验证"
            _ledger.end_inflight_scope(_scope, settle_leaked=True)
            raise

        _ledger.end_inflight_scope(_scope, settle_leaked=False)
        # 提取最后一条 AI 消息
        messages = result.get("messages", [])
        self._record_tool_telemetry(messages, step)
        if messages:
            last = messages[-1]
            return getattr(last, "content", str(last))
        return "(Agent 无输出)"

    def _record_tool_telemetry(self, messages, step: str) -> None:
        """G2-2（主题G·工具观测面，#9-C）：从 agent 返回 messages 确定性统计工具调用。

        沙箱 jsonl 只见 exec/shell，write_file/experience__<id> 等 LangGraph 工具零留痕
        → 分不清某工具「没挂载」还是「挂了模型没用」。此处按 AIMessage.tool_calls 归因
        调用数、ToolMessage.status=='error' 归因错误数，累计进 self._tool_telemetry（_make_output
        注入 l1_details 供机读）+发一行结构化 [tool-telemetry]（含 experience__ 前缀=技能遥测
        join 的落库端，取代 tools.py 只发不 join 的 skills-telemetry grep）。观测面 fail-open。
        """
        try:
            tel = getattr(self, "_tool_telemetry", None)
            if tel is None:
                tel = {"calls": {}, "errors": {}}
                self._tool_telemetry = tel
            calls_this: dict[str, int] = {}
            for m in messages or []:
                tcs = getattr(m, "tool_calls", None)
                if tcs:
                    for tc in tcs:
                        name = (tc.get("name") if isinstance(tc, dict)
                                else getattr(tc, "name", None)) or "?"
                        tel["calls"][name] = tel["calls"].get(name, 0) + 1
                        calls_this[name] = calls_this.get(name, 0) + 1
                # ToolMessage（type=='tool'）执行失败：langchain 置 status=='error'
                if getattr(m, "type", None) == "tool" and getattr(m, "status", None) == "error":
                    nm = getattr(m, "name", None) or "?"
                    tel["errors"][nm] = tel["errors"].get(nm, 0) + 1
            if calls_this:
                logger.info("[tool-telemetry] subtask=%s step=%s tools=%s",
                            self.subtask.id, step, dict(sorted(calls_this.items())))
        except Exception:  # noqa: BLE001 — 观测面绝不拖垮执行
            pass
