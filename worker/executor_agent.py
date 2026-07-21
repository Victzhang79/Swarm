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
import os
import time

logger = logging.getLogger(__name__)


def _is_continuity_step(step: str) -> bool:
    """R63-T9②：该步的对话是否可作下一 fix 轮的延续源。

    只有【产码步】（code / code-batch-N / fix-N）参与——模型改代码的推理与工具轨迹
    是修复轮最需要的上下文；verify/locate/produce 步框架不同（验证叙事/定位探索），
    延续进修复轮只添噪，且 verify 步夹在 code 与 fix 之间，纳入会冲掉真正的产码对话。
    """
    return step == "code" or step.startswith("code-batch-") or step.startswith("fix-")


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
        ) or (
            # R65TR-T5 猎手 F3：老缓存画像缺新 jvm 事实键 → 补探（否则 lombok 基线
            # 约束对已建档项目永不生效；与 _STACK_SCHEMA_VERSION=3 双保险）。
            profile.get("jvm") is not None
            and "lombok_available" not in (profile.get("jvm") or {})
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
                         max_steps: int | None = None,
                         continue_messages: list | None = None) -> str:
        """调用 Agent 执行一步并返回结果（受总执行时间预算约束）。

        max_steps：本步专属 recursion_limit 上限（默认用整体 max_iterations）。LOCATING 等
        "理解/定位"阶段用更紧的 cap，逼模型少探索直接产出（RUN12 实证：预读注入了但模型仍
        探索 167-286s 烧光预算）。撞 cap 非硬失败——下方 GraphRecursionError 优雅返回，交 CODING。

        continue_messages（R63-T9②）：历史对话前缀（已裁剪），前置进本次 ainvoke——
        fix 轮据此把确定性 build 错回喂进【同一对话】，模型看得到自己上一轮的改动与
        推理，不再每轮全新单消息从零重探（st-8 撞 95 迭代的直接成因之一）。
        """
        if self._agent is None:
            return "❌ Agent 未创建"
        # R63-T11 双保险：brain 图节点层统一打 LLM 录制标签（graph._maybe_labeled），
        # dispatch/monitor 已在 denylist 不打——但 contextvar 会经 ensure_future 拷贝，
        # 未来任何标签泄漏进 worker 任务上下文都会让 worker 流量被误录（cassette 铁律：
        # worker 流量不录）。此处无条件清空，worker agent LLM 调用绝不带 brain 节点标签。
        from swarm.models.router import set_llm_node
        set_llm_node("")
        # 产码步先清 carry 源：本轮失败/异常时不留 stale 历史（比没有历史更危险），
        # 成功后在下方以本轮全量对话覆盖。非产码步（verify 等）不触碰。
        _continuity = _is_continuity_step(step)
        if _continuity:
            self._continuity_messages = None

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
        # R63-T9②：历史前缀 + 新 human 消息（无前缀=旧行为单消息，零回归）。
        _prior_n = len(continue_messages) if continue_messages else 0
        _input_messages = (list(continue_messages) if continue_messages else []) \
            + [("human", human_message)]
        try:
            result = await asyncio.wait_for(
                agent.ainvoke(
                    {"messages": _input_messages},
                    config=invoke_config,
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            _ledger.end_inflight_scope(_scope, settle_leaked=True)
            self._log(f"Agent 调用超时（剩余预算 {remaining:.0f}s）")
            if _continuity:
                # T9 猎手 F1：优雅返回路径必须留观测——carry 源已在开头清空，
                # 下一 fix 轮会回退全新单消息，操作员要能从日志区分这与"从未有 carry"。
                self._log("R63-T9 turn 连续性：本轮超时无完整对话可延续，"
                          "carry 源已清空，下一 fix 轮回退全新单消息")
            return f"❌ Agent 调用超时（预算 {self.max_execution_time}s）"
        except asyncio.CancelledError:
            _ledger.end_inflight_scope(_scope, settle_leaked=True)
            raise
        except Exception as exc:
            # R63-T7 猎手 F1（CRITICAL）：StreamDegenerationError 的 message 内嵌任意复读
            # token（needle）——若恰含 "recursion"（模型卡壳时念叨 RecursionError/infinite
            # recursion 很常见），会被下方无词边界的子串启发式吞成"撞迭代上限"优雅返回，
            # 升档信号（degeneration_hard_fail→force_strong）整条丢失。已分类异常必须
            # 先 isinstance 短路原样上抛，绝不进启发式。
            from swarm.models.errors import StreamDegenerationError
            if isinstance(exc, StreamDegenerationError):
                _ledger.end_inflight_scope(_scope, settle_leaked=True)
                raise
            # GraphRecursionError 等：agent 撞迭代上限。它在沙箱里已做的改动仍有效，
            # 不当作硬失败——后续 pull-back + 确定性 L1 闸门会按真实文件状态裁决
            # (与"子代理撞步数上限但已产出部分工作"同理，让确定性验证说话)。
            # DR-04-F2 治本：判 GraphRecursionError 只认【专有 token "GraphRecursionError"】，绝不用
            # 无词边界子串 `"Recursion" in cls or "recursion" in msg`——后者会命中内置 RecursionError
            # （真实栈溢出/无限递归 bug，类名即 "RecursionError"）→ 真崩溃被伪装成"撞迭代上限"优雅返回、
            # 根因从日志蒸发，且 settle_leaked=False 与超时/取消路径不一致致账虚高；偶含 "recursion" 的
            # provider 异常同理。既认真实例(isinstance)，也认【被包裹重抛】携原类名进 message 的形态
            # （R63-T7/T9 实测 RuntimeError("GraphRecursionError: …") 是 langgraph 撞上限的真实冒泡形态），
            # 但 token 必须是专有的 "GraphRecursionError"，故内置 RecursionError 落下方 raise。
            try:
                from langgraph.errors import GraphRecursionError as _GRE
                _is_graph_recursion = isinstance(exc, _GRE)
            except Exception:  # noqa: BLE001 — 导入失败退回下方 token 判定
                _is_graph_recursion = False
            if not _is_graph_recursion:
                _is_graph_recursion = (
                    type(exc).__name__ == "GraphRecursionError"
                    or "GraphRecursionError" in str(exc)
                )
            if _is_graph_recursion:
                _ledger.end_inflight_scope(_scope, settle_leaked=False)
                self._log(f"Agent 撞迭代上限({_limit})，以沙箱实际产出为准交确定性闸门裁决")
                if _continuity:
                    # T9 猎手 F1：同上——撞上限正是 T9 要治的病理场景，carry 断链必须可见。
                    self._log("R63-T9 turn 连续性：本轮撞迭代上限无完整对话可延续，"
                              "carry 源已清空，下一 fix 轮回退全新单消息")
                return f"⚠️ Agent 达到迭代上限（{_limit}），已做改动交由确定性 L1 验证"
            _ledger.end_inflight_scope(_scope, settle_leaked=True)
            raise
        except BaseException:  # KeyboardInterrupt 级：清作用域防内存泄漏（必须列在
            # Exception 之后——r50 实测列前面会截走 GraphRecursionError 杀死优雅路径）
            _ledger.end_inflight_scope(_scope, settle_leaked=True)
            raise

        _ledger.end_inflight_scope(_scope, settle_leaked=False)
        # 提取最后一条 AI 消息
        messages = result.get("messages", [])
        # R63-T9②：telemetry 只吃本次新增切片（携带的历史前缀含前轮工具调用，
        # 全量喂会重复计数）；成功的产码轮以全量对话更新 carry 源（用时再裁剪）。
        self._record_tool_telemetry(messages[_prior_n:], step)
        if _continuity:
            self._continuity_messages = messages or None
        if messages:
            last = messages[-1]
            return getattr(last, "content", str(last))
        return "(Agent 无输出)"

    def _fix_carry_messages(self) -> list | None:
        """R63-T9②：取上一产码轮对话（裁剪后）作为本 fix 轮的延续前缀。

        env SWARM_WORKER_FIX_TURN_CONTINUITY=false 一键回退旧行为（全新单消息轮）；
        SWARM_WORKER_FIX_CARRY_BUDGET_CHARS 控制携带预算（默认 24000 字符，本地小窗
        worker 可调小）。裁剪自身异常 → fail-open 回退 + WARNING（铁律：不许静默）。
        """
        if os.environ.get("SWARM_WORKER_FIX_TURN_CONTINUITY",
                          "true").strip().lower() in ("false", "0", "no"):
            return None
        msgs = getattr(self, "_continuity_messages", None)
        if not msgs:
            # T9 猎手 F1：无 carry 源（首轮，或上一产码轮超时/撞上限被清）必须可见，
            # 操作员才能验证 T9 在病理 fix 循环上是否真的接管了。
            logger.info(
                "R63-T9 turn 连续性：本 fix 轮无可延续 carry 源"
                "（首轮或上一产码轮未成功完成），回退全新单消息轮")
            return None
        _raw_budget = os.environ.get("SWARM_WORKER_FIX_CARRY_BUDGET_CHARS", "24000")
        try:
            budget = int(_raw_budget)
        except ValueError:
            # T9 猎手 F2：配置坏值静默回退违反 fail-open 可观测铁律。
            logger.warning(
                "R63-T9：SWARM_WORKER_FIX_CARRY_BUDGET_CHARS=%r 不是整数，"
                "回退默认 24000", _raw_budget)
            budget = 24000
        try:
            from swarm.worker import turn_continuity as _tc
            return _tc.trim_carry_messages(msgs, budget_chars=budget)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "R63-T9 turn 连续性裁剪失败，fail-open 回退全新单消息轮: %s", exc)
            return None

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
