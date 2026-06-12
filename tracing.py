"""LangSmith / LangChain 追踪 — 初始化 + Phase 0–2 运行上下文

Phase 0: Worker standalone / Brain dispatch → worker ReAct agent
Phase 1: Brain 任务 / resume → LangGraph 根 run
Phase 2: 知识检索、预处理架构 LLM

LangChain ChatOpenAI / LangGraph 在 LANGCHAIN_TRACING_V2=true 时自动上报；
本模块补充 run_name、tags、metadata，便于在 LangSmith 按场景筛选。
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# ─── 场景标签（与 LangSmith Filters 对齐）────────────────────
PHASE_0 = "phase-0"  # Worker 直跑
PHASE_1 = "phase-1"  # Brain 任务链路
PHASE_2 = "phase-2"  # 知识库 / 预处理


def is_langsmith_active() -> bool:
    """当前进程是否已向 LangChain 注入 tracing 环境变量。"""
    return os.environ.get("LANGCHAIN_TRACING_V2", "").lower() == "true" and bool(
        os.environ.get("LANGCHAIN_API_KEY") or os.environ.get("LANGSMITH_API_KEY")
    )


def langsmith_status() -> dict[str, str | bool]:
    """供 API / 调试使用的 tracing 状态快照。"""
    from swarm.config.settings import get_config

    cfg = get_config()
    return {
        "configured": cfg.langsmith_tracing and bool(cfg.langsmith_api_key),
        "active": is_langsmith_active(),
        "project": os.environ.get("LANGCHAIN_PROJECT")
        or os.environ.get("LANGSMITH_PROJECT")
        or cfg.langsmith_project
        or "swarm-dev",
        "endpoint": os.environ.get("LANGSMITH_ENDPOINT") or cfg.langsmith_endpoint,
    }


def configure_langsmith(*, reload: bool = False) -> bool:
    """根据 AppConfig 启用 LangSmith 追踪。reload=True 时强制重新读取配置。"""
    from swarm.config.settings import get_config

    cfg = get_config()

    tracing = cfg.langsmith_tracing or os.environ.get("LANGSMITH_TRACING", "").lower() in (
        "true",
        "1",
        "yes",
    )
    api_key = cfg.langsmith_api_key or os.environ.get("LANGSMITH_API_KEY", "")
    project = cfg.langsmith_project or os.environ.get("LANGSMITH_PROJECT", "swarm-dev")
    endpoint = cfg.langsmith_endpoint or os.environ.get(
        "LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"
    )

    if tracing and api_key:
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGSMITH_API_KEY"] = api_key
        os.environ["LANGSMITH_PROJECT"] = project
        os.environ["LANGSMITH_ENDPOINT"] = endpoint
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGCHAIN_API_KEY"] = api_key
        os.environ["LANGCHAIN_PROJECT"] = project
        if reload or not getattr(configure_langsmith, "_logged", False):
            logger.info("LangSmith tracing enabled (project=%s)", project)
            configure_langsmith._logged = True  # type: ignore[attr-defined]
        return True

    for key in (
        "LANGSMITH_TRACING",
        "LANGCHAIN_TRACING_V2",
    ):
        os.environ.pop(key, None)
    if reload:
        logger.info("LangSmith tracing disabled (tracing=%s, has_key=%s)", tracing, bool(api_key))
    return False


def push_l1_feedback(
    l1_details: dict[str, Any],
    *,
    l1_passed: bool,
    run_id: str | None = None,
) -> None:
    """把 L1 确定性验证结果作为【结构化 feedback】上报 LangSmith。

    解决"Smith 上断言太水/不规范"：原来 LangSmith 只能看到 LLM 自报通过这种
    弱信号。这里把确定性闸门的真实证据(scope/compile/lint/test/verify 各分项)
    作为规范化的 feedback key-score 推回当前 run，让每条断言可量化、可筛选、
    可追溯到底是确定性验证还是 LLM 自报。

    tracing 关闭时 no-op；任何异常都吞掉(可观测性不应影响主流程)。
    """
    if not is_langsmith_active():
        return
    try:
        from langsmith import Client
        from langsmith.run_helpers import get_current_run_tree

        rid = run_id
        if not rid:
            rt = get_current_run_tree()
            rid = str(rt.id) if rt else None
        if not rid:
            return

        client = Client()
        source = l1_details.get("l1_decision_source", "unknown")

        def _fb(key: str, score: float | bool, comment: str = "") -> None:
            try:
                client.create_feedback(
                    run_id=rid, key=key,
                    score=float(bool(score)) if isinstance(score, bool) else score,
                    comment=comment or None,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("create_feedback %s skipped: %s", key, exc)

        # 顶层结论 + 决策来源（规范化断言：明确是确定性还是自报）
        _fb("l1_passed", l1_passed, f"decision_source={source}")
        _fb("l1_decision_is_deterministic", source == "deterministic",
            "确定性闸门=可信；llm_self_report=弱信号")

        # 各分项确定性证据
        for key, fb_key in (
            ("l1_1_scope_ok", "scope_ok"),
            ("l1_2_compile_ok", "compile_ok"),
            ("l1_3_test_ok", "test_ok"),
        ):
            if key in l1_details:
                _fb(fb_key, bool(l1_details[key]))
        lint = l1_details.get("lint") or {}
        if isinstance(lint, dict) and lint.get("status") in ("ok", "error"):
            _fb("lint_ok", lint.get("status") == "ok", lint.get("message", "")[:200])
        # harness verify 命令逐条
        vcs = l1_details.get("verify_commands") or []
        if vcs:
            passed = sum(1 for v in vcs if v.get("ok"))
            _fb("verify_pass_rate", passed / len(vcs),
                f"{passed}/{len(vcs)} harness 验收命令通过")
    except Exception as exc:  # noqa: BLE001
        logger.debug("push_l1_feedback skipped: %s", exc)


def push_planning_feedback(planning: dict[str, Any], *, run_id: str | None = None) -> None:
    """把 Q4 规划子图的关键决策作为结构化 feedback 上报 LangSmith。

    指标：澄清轮数、是否跳过、澄清后定级、技术方案评审(通过/打回)、拆分密度、
    超预算子任务数、INVEST 自检失败数。tracing 关闭 no-op；异常全吞。
    """
    if not is_langsmith_active():
        return
    try:
        from langsmith import Client
        from langsmith.run_helpers import get_current_run_tree

        rid = run_id
        if not rid:
            rt = get_current_run_tree()
            rid = str(rt.id) if rt else None
        if not rid:
            return

        client = Client()

        def _fb(key: str, score: float | bool, comment: str = "") -> None:
            try:
                client.create_feedback(
                    run_id=rid, key=key,
                    score=float(bool(score)) if isinstance(score, bool) else score,
                    comment=comment or None,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("create_feedback %s skipped: %s", key, exc)

        if "clarify_rounds" in planning:
            _fb("clarify_rounds", float(planning.get("clarify_rounds", 0)),
                f"自适应澄清轮数（上限 {planning.get('clarify_max', 5)}）")
        if "clarify_skipped" in planning:
            _fb("clarify_skipped", bool(planning.get("clarify_skipped")), "用户是否整体跳过澄清")
        if planning.get("assessed_complexity"):
            _fb("assessed_complexity_is_complex",
                planning["assessed_complexity"] in ("complex", "ultra"),
                f"澄清后定级={planning['assessed_complexity']}")
        if "design_review_decision" in planning:
            _fb("design_approved", planning.get("design_review_decision") == "approve",
                f"打回 {planning.get('design_reject_count', 0)} 次")
        ms = planning.get("milestone_count")
        sub = planning.get("subtask_count")
        if ms and sub:
            _fb("plan_elaboration_ratio", sub / ms, f"{sub} 子任务 / {ms} 里程碑")
        if "oversized_count" in planning:
            _fb("oversized_subtasks", float(planning.get("oversized_count", 0)),
                "预估超上下文预算、拆不下的子任务数（>0 表示需重新切分）")
        if "invest_fail_count" in planning:
            _fb("invest_fail_count", float(planning.get("invest_fail_count", 0)),
                "INVEST 自检未过（如缺验收标准）的子任务数")
    except Exception as exc:  # noqa: BLE001
        logger.debug("push_planning_feedback skipped: %s", exc)


def _base_tags(*, phase: str, component: str, extra: list[str] | None = None) -> list[str]:
    tags = ["swarm", f"swarm-{phase}", f"swarm-{component}"]
    if extra:
        tags.extend(extra)
    return tags


def merge_invoke_config(base: dict[str, Any], tracing: dict[str, Any]) -> dict[str, Any]:
    """合并 LangGraph/LangChain invoke config（保留 recursion_limit 等）。"""
    out = dict(base)
    for key in ("run_name", "tags", "metadata", "recursion_limit"):
        if key in tracing:
            out[key] = tracing[key]
    if "configurable" in base or "configurable" in tracing:
        out["configurable"] = {
            **(base.get("configurable") or {}),
            **(tracing.get("configurable") or {}),
        }
    return out


def brain_graph_config(
    *,
    task_id: str,
    project_id: str,
    thread_id: str,
    resume: bool = False,
    description: str = "",
) -> dict[str, Any]:
    """Phase 1 — Brain LangGraph 根 run（create_task / approve resume）。"""
    base: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    if not is_langsmith_active():
        return base

    kind = "resume" if resume else "task"
    meta: dict[str, Any] = {
        "swarm_task_id": task_id,
        "swarm_project_id": project_id,
        "swarm_thread_id": thread_id,
        "swarm_flow": f"brain-{kind}",
    }
    if description:
        meta["task_description_preview"] = description[:200]

    return merge_invoke_config(
        base,
        {
            "run_name": f"brain/{kind}/{task_id[:8]}",
            "tags": _base_tags(phase=PHASE_1, component="brain", extra=[f"brain-{kind}"]),
            "metadata": meta,
        },
    )


def worker_agent_config(
    *,
    run_id: str,
    project_id: str | None,
    task_id: str | None = None,
    subtask_id: str | None = None,
    difficulty: str = "medium",
    worker_phase: str = "agent",
    step: str = "react",
    source: str = "standalone",
) -> dict[str, Any]:
    """Phase 0 / dispatch — Worker ReAct Agent 单次 ainvoke。"""
    base: dict[str, Any] = {}
    if not is_langsmith_active():
        return base

    flow = "brain-dispatch" if source == "dispatch" else "standalone"
    meta: dict[str, Any] = {
        "swarm_run_id": run_id,
        "swarm_project_id": project_id or "",
        "swarm_difficulty": difficulty,
        "swarm_worker_phase": worker_phase,
        "swarm_agent_step": step,
        "swarm_flow": flow,
    }
    if task_id:
        meta["swarm_task_id"] = task_id
    if subtask_id:
        meta["swarm_subtask_id"] = subtask_id

    name_part = subtask_id or run_id
    return {
        "run_name": f"worker/{flow}/{name_part[:8]}/{worker_phase.lower()}/{step}",
        "tags": _base_tags(
            phase=PHASE_0 if source == "standalone" else PHASE_1,
            component="worker",
            extra=[flow, f"difficulty-{difficulty}"],
        ),
        "metadata": meta,
    }


def swarm_traceable(
    name: str,
    *,
    phase: str,
    component: str,
    run_type: str = "chain",
    extra_tags: list[str] | None = None,
) -> Callable[[F], F]:
    """装饰器：tracing 关闭时为 no-op；开启时写入 LangSmith run。"""

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            if not is_langsmith_active():
                return await fn(*args, **kwargs)
            try:
                from langsmith.run_helpers import traceable

                traced = traceable(
                    name=name,
                    run_type=run_type,
                    tags=_base_tags(phase=phase, component=component, extra=extra_tags),
                )(fn)
                return await traced(*args, **kwargs)
            except Exception as exc:
                logger.debug("LangSmith traceable skipped for %s: %s", name, exc)
                return await fn(*args, **kwargs)

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            if not is_langsmith_active():
                return fn(*args, **kwargs)
            try:
                from langsmith.run_helpers import traceable

                traced = traceable(
                    name=name,
                    run_type=run_type,
                    tags=_base_tags(phase=phase, component=component, extra=extra_tags),
                )(fn)
                return traced(*args, **kwargs)
            except Exception as exc:
                logger.debug("LangSmith traceable skipped for %s: %s", name, exc)
                return fn(*args, **kwargs)

        if inspect.iscoroutinefunction(fn):
            return async_wrapper  # type: ignore[return-value]
        return sync_wrapper  # type: ignore[return-value]

    return decorator
