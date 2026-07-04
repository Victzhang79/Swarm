"""brain/nodes/verify.py — verify_l2/verify_l3 节点 + 失败态/巡检 helper（B1 批3 抽出）。

被测试 patch 的 _get_brain_llm/_get_project_path/_try_l2_*/_verify_l2_via_llm 留在 __init__.py；
本模块内对它们的调用用 `nodes.X(...)` 模块限定，使 patch("swarm.brain.nodes.X") 命中。
_diff_has_changes 已移到 shared.py。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from swarm.brain import nodes
from swarm.brain.prompts import (
    VERIFY_L2_SYSTEM,
    VERIFY_L2_USER,
    VERIFY_L3_SYSTEM,
    VERIFY_L3_USER,
)
from swarm.brain.nodes.shared import (
    _diff_has_changes,
    _l2_test_command_from_criteria,
    _parse_json_from_llm,
    attribute_l2_failure,
)
from swarm.brain.state import BrainState, effective_complexity
from swarm.config.settings import get_config
from swarm.types import Complexity, WorkerOutput

logger = logging.getLogger(__name__)


async def verify_l2(state: BrainState) -> dict:
    """VERIFY_L2 节点 — L2 集成测试验证

    输入: merged_diff, plan, task_description
    输出: l2_passed
    """
    merged_diff = state.get("merged_diff", "")
    plan_obj = state.get("plan")
    task_description = state.get("task_description", "")
    project_id = state.get("project_id", "")
    subtask_results = state.get("subtask_results", {})

    logger.info("[VERIFY_L2] 执行集成验证")

    complexity = effective_complexity(state)  # 修复 12.3：澄清后定级优先
    if complexity == Complexity.SIMPLE:
        merged = (merged_diff or "").strip()
        l2_passed = _diff_has_changes(merged)
        if subtask_results:
            l2_passed = l2_passed and all(
                (isinstance(o, WorkerOutput) and o.l1_passed)
                # #5：dict 结果缺 l1_passed 时保守判 False（默认 True 会把"未验证"当通过）。
                or (isinstance(o, dict) and o.get("l1_passed", False))
                for o in subtask_results.values()
            )
        logger.info("[VERIFY_L2] SIMPLE 快速路径 — diff+L1 检查: %s", "通过" if l2_passed else "未通过")
        if not l2_passed:
            return _l2_failure_state(subtask_results)
        return {"l2_passed": l2_passed}

    acceptance_criteria: list[str] = []
    if plan_obj:
        for t in plan_obj.subtasks:
            acceptance_criteria.extend(t.acceptance_criteria)

    test_cmd = _l2_test_command_from_criteria(acceptance_criteria)
    shared_contract = state.get("shared_contract") or (
        plan_obj.shared_contract if plan_obj else {}
    )

    # L2 确定性集成审查（编译 + 契约）
    project_path = nodes._get_project_path(project_id)
    if project_path and (merged_diff or "").strip():
        from swarm.brain.integration_review import run_integration_review

        # 治本 round21：L2 全 reactor 编译优先在【项目沙箱】(按检测栈版本烤的工具链)跑——brain host
        # 无需装 Java/Go/Rust/Node，多栈/多版本自动正确。沙箱不可用则 run_integration_review 退回本机
        # (仅当本机装了该栈工具)；两者都不行 → fail-loud 拒绝假绿。
        def _sandbox_compile_runner(build_cmd: str):
            return nodes._run_reactor_build_in_sandbox(
                project_path, project_id, build_cmd, timeout=600
            )

        # R23-1 治本：run_integration_review 是同步阻塞(内含 subprocess.run，timeout 可达 600s)，
        # verify_l2 是 async 节点——直接调用会卡死整个 API 事件循环(SSE/心跳/并发任务)。放线程池
        # 执行(asyncio.to_thread 会拷贝 contextvars，沙箱上下文照常可用)。
        ir_ok, ir_issues, ir_details = await asyncio.to_thread(
            run_integration_review,
            project_path,
            merged_diff,
            shared_contract or None,
            timeout=600,
            compile_runner=_sandbox_compile_runner,
            base_ref=state.get("base_commit"),  # 3rd#2：L2 reset/apply-check 相对钉扎 base
        )
        logger.info("[VERIFY_L2] integration_review: %s issues=%s", ir_ok, ir_issues[:3])
        if not ir_ok:
            if any("契约" in i for i in ir_issues):
                return {
                    "l2_passed": False,
                    "verification_failure": "contract",
                    "failure_strategy": "retry",
                    "failed_subtask_ids": list(subtask_results.keys()),
                    "l2_details": {"integration_review": ir_details, "issues": ir_issues},
                }
            # TD2606-B8：把集成编译失败归因到具体子任务（编译输出已含出错文件路径），
            # 能定位则只重做相关子任务、保留成功兄弟；定位不了回退现状（全量 replan）。
            _l2_details = {"integration_review": ir_details, "issues": ir_issues}
            attributed = attribute_l2_failure(plan_obj, _l2_details, subtask_results)
            return _l2_failure_state(
                subtask_results, attributed_ids=attributed, l2_details=_l2_details
            )

    if (merged_diff or "").strip() and test_cmd.strip():
        sandbox_result = nodes._try_l2_sandbox_verify(
            project_id, merged_diff, test_cmd, timeout=180
        )
        if sandbox_result is not None:
            logger.info("[VERIFY_L2] 沙箱结果: %s", "通过" if sandbox_result else "未通过")
            if not sandbox_result:
                return _l2_failure_state(subtask_results)
            return {"l2_passed": sandbox_result}

        local_result = nodes._try_l2_local_verify(
            project_id, merged_diff, test_cmd, timeout=180,
            base_ref=state.get("base_commit"),
        )
        if local_result is not None:
            logger.info("[VERIFY_L2] 本地结果: %s", "通过" if local_result else "未通过")
            if not local_result:
                return _l2_failure_state(subtask_results)
            return {"l2_passed": local_result}
    elif (merged_diff or "").strip() and not test_cmd.strip():
        # 任务未要求测试（criteria 无显式测试命令）→ 跳过功能测试验证。
        # integration_review（编译+契约+git apply 同源）已作为确定性证据通过，故【放行】
        # L2，不因无谓/写死框架的测试而误判（task dc1ec890），更不会硬卡 docs/config 这类
        # 本就无测试的任务。
        # A-P1-06（诚实/可见性，非阻断）：编译通过 ≠ 功能正确，且本路径未跑任何功能测试，
        # 因此打一条 degraded 标记 l2_no_test_executed，让交付/确认环节看得见"L2 未经测试
        # 验证"，避免静默当成"已测通过"。仍 l2_passed=True 放行。
        logger.info(
            "[VERIFY_L2] 无显式测试命令，integration_review 已通过 → L2 放行"
            "（未跑功能测试，标记 degraded: l2_no_test_executed）"
        )
        return {"l2_passed": True, "degraded_reasons": ["l2_no_test_executed"]}

    l2_passed = await nodes._verify_l2_via_llm(
        task_description,
        merged_diff,
        acceptance_criteria,
        subtask_results,
    )

    logger.info(f"[VERIFY_L2] 结果: {'通过' if l2_passed else '未通过'}")
    if not l2_passed:
        return _l2_failure_state(subtask_results)
    return {"l2_passed": l2_passed}


async def verify_l3(state: BrainState) -> dict:
    """VERIFY_L3 节点 — L3 预发/扩展验证（COMPLEX/ULTRA）

    输入: merged_diff, complexity, task_description
    输出: l3_passed, l3_skipped, l3_message
    """
    complexity = effective_complexity(state)  # 修复 12.3：澄清后定级优先，避免漏跑 L3
    merged_diff = (state.get("merged_diff") or "").strip()
    task_description = state.get("task_description", "")

    if complexity in (Complexity.SIMPLE, Complexity.MEDIUM):
        logger.info("[VERIFY_L3] SIMPLE/MEDIUM — 跳过 L3")
        return {
            "l3_passed": None,
            "l3_skipped": True,
            "l3_message": "L3 skipped for simple/medium complexity",
        }

    if not merged_diff:
        logger.info("[VERIFY_L3] 无 merged_diff — 跳过 L3")
        return {
            "l3_passed": None,
            "l3_skipped": True,
            "l3_message": "No merged diff for L3",
        }

    task_id = state.get("task_id", "")
    project_id = state.get("project_id", "")
    from swarm.brain.l3_gitlab import (
        gitlab_configured,
        l3_push_enabled,
        push_merged_diff_branch,
        trigger_and_poll_pipeline,
    )

    if gitlab_configured():
        try:
            ref = os.environ.get("SWARM_GITLAB_REF", "main")
            if l3_push_enabled():
                project_path = nodes._get_project_path(project_id)
                if project_path:
                    branch, push_err = push_merged_diff_branch(
                        project_path, merged_diff, task_id or "unknown", base_ref=ref
                    )
                    if branch:
                        ref = branch
                        logger.info("[VERIFY_L3] 已推送 L3 分支: %s", branch)
                    elif push_err:
                        logger.warning("[VERIFY_L3] L3 push 失败，回退默认 ref: %s", push_err)

            # R23-1 治本：trigger_and_poll_pipeline 内含 time.sleep 轮询(同步阻塞)，放线程池执行，
            # 不卡 async 事件循环。
            l3_passed, l3_message = await asyncio.to_thread(
                trigger_and_poll_pipeline, task_id=task_id or "unknown", ref=ref
            )
            logger.info("[VERIFY_L3] GitLab: %s — %s", "通过" if l3_passed else "未通过", l3_message)
            if not l3_passed:
                return {**_l3_failure_state(), "l3_message": l3_message, "l3_branch": ref}
            return {
                "l3_passed": l3_passed,
                "l3_skipped": False,
                "l3_message": l3_message,
                # N-04 修复：把实际推送的 L3 分支(ref)写进 state，否则 learn_success 读
                # state['l3_branch'] 为空 → MR 回退到从未推送的 swarm/task-xxx 分支。
                "l3_branch": ref,
            }
        except Exception as exc:
            logger.warning("[VERIFY_L3] GitLab pipeline 失败，回退 staging/LLM: %s", exc)

    staging_url = os.environ.get("SWARM_STAGING_URL", "").strip()
    if not staging_url:
        logger.info("[VERIFY_L3] 未配置 SWARM_STAGING_URL — 跳过 L3")
        return {
            "l3_passed": None,
            "l3_skipped": True,
            "l3_message": "No staging URL configured",
        }

    logger.info("[VERIFY_L3] 执行扩展验证: %s", staging_url)
    try:
        llm = nodes._get_brain_llm()
        prompt_user = VERIFY_L3_USER.format(
            task_description=task_description,
            merged_diff=merged_diff[:4000],
            staging_url=staging_url,
        )
        response = await llm.ainvoke([
            {"role": "system", "content": VERIFY_L3_SYSTEM},
            {"role": "user", "content": prompt_user},
        ])
        result = _parse_json_from_llm(response.content)
        l3_passed = bool(result.get("l3_passed", False))
        l3_message = str(result.get("message", "L3 LLM validation"))
    except json.JSONDecodeError as e:
        logger.warning("[VERIFY_L3] LLM JSON 解析失败，回退 HTTP 探测: %s", e)
        l3_passed, l3_message = _l3_staging_http_check(staging_url)
    except Exception as e:
        logger.warning("[VERIFY_L3] LLM 验证异常，回退 HTTP 探测: %s", e)
        l3_passed, l3_message = _l3_staging_http_check(staging_url)

    logger.info("[VERIFY_L3] 结果: %s — %s", "通过" if l3_passed else "未通过", l3_message)
    if not l3_passed:
        return {**_l3_failure_state(), "l3_message": l3_message}
    return {
        "l3_passed": l3_passed,
        "l3_skipped": False,
        "l3_message": l3_message,
    }


def _l2_failure_state(
    subtask_results: dict,
    attributed_ids: list[str] | None = None,
    l2_details: dict | None = None,
) -> dict:
    """L2 失败态。TD2606-B8：attributed_ids 非空 → 只把这些子任务标失败并打 l2_targeted，
    供 handle_failure 走定向恢复（保留成功兄弟）；否则连坐全部、走原全量 replan。"""
    state: dict = {
        "l2_passed": False,
        "verification_failure": "l2",
        "failure_strategy": "replan",
    }
    if l2_details:
        state["l2_details"] = l2_details
    if attributed_ids:
        state["failed_subtask_ids"] = list(attributed_ids)
        state["l2_targeted"] = True
    else:
        state["failed_subtask_ids"] = list(subtask_results.keys()) if subtask_results else []
    return state


def _l3_failure_state() -> dict:
    return {
        "l3_passed": False,
        "l3_skipped": False,
        "verification_failure": "l3",
        "failure_strategy": "escalate",
    }


def _l3_staging_http_check(staging_url: str) -> tuple[bool, str]:
    """对预发 URL 做轻量 HTTP 可达性检查。"""
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(staging_url, method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as resp:
            passed = resp.status < 400
            return passed, f"Staging HEAD {staging_url} status={resp.status}"
    except urllib.error.HTTPError as exc:
        return exc.code < 400, f"Staging HEAD {staging_url} status={exc.code}"
    except Exception as exc:
        return False, f"Staging check failed: {exc}"
