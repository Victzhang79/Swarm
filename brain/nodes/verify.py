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


def _runtime_smoke_enabled() -> bool:
    """S1-4 运行时冒烟杀开关（对照 D52 SWARM_SANDBOX_TAR_SYNC 先例）：默认开。

    为什么需要：冒烟是新上线闸门，线上出问题时必须能一键回退到旧行为（L2→L3 直连），
    且关闭必须【可观测】——verify_runtime 对关闭态走 skipped+degraded 留痕，绝不静默。
    """
    return os.environ.get("SWARM_RUNTIME_SMOKE_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _kill_sandbox_quiet(sandbox_id: str) -> None:
    """按 sid 尽力销毁沙箱（转交沙箱的处置口径：失败只 debug 留痕——远端 900s 自动到期
    + 启动清扫是既有泄漏兜底，见设计 §2.3）。"""
    if not sandbox_id:
        return
    try:
        from swarm.worker.sandbox import get_sandbox_manager
        get_sandbox_manager().kill(sandbox_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[VERIFY_RUNTIME] 销毁沙箱 %s 失败(远端到期兜底): %s", sandbox_id, exc)


async def verify_l2(state: BrainState) -> dict:
    """VERIFY_L2 节点 — L2 集成测试验证（薄包装）。

    S1-4：统一处置编译沙箱的冒烟延活转交——_verify_l2_impl 内沙箱编译成功时
    _run_reactor_build_in_sandbox 可能延活不杀并回传 sid（设计 §2）。本包装在【L2 最终
    结论】处收口：L2 通过 → sid 写入 state（runtime_smoke_sandbox_id，对象经进程内
    manager._instances registry 转交 verify_runtime）；L2 未通过/异常 → verify_runtime
    不会执行，本节点是最后责任人，立即销毁，不留 900s 泄漏窗。
    """
    handoff: list[str] = []
    try:
        result = await _verify_l2_impl(state, handoff)
    except BaseException:
        for sid in handoff:
            await asyncio.to_thread(_kill_sandbox_quiet, sid)
        raise
    if handoff:
        sid = handoff[-1]
        if result.get("l2_passed"):
            result = {**result, "runtime_smoke_sandbox_id": sid}
        else:
            await asyncio.to_thread(_kill_sandbox_quiet, sid)
    return result


async def _verify_l2_impl(state: BrainState, _smoke_handoff: list[str]) -> dict:
    """VERIFY_L2 核心逻辑（原 verify_l2 本体，S1-4 抽薄包装时原样保留）。

    输入: merged_diff, plan, task_description
    输出: l2_passed；_smoke_handoff（out 参数）收集编译沙箱延活转交的 sid。
    """
    # A6：惰性导入破 nodes↔verify eager 循环依赖（_get_project_path/_try_l2_*/_verify_l2_via_llm
    # 是留在 __init__ 的可 patch 有状态符号；调用时 nodes 已初始化，patch 仍命中）。
    from swarm.brain import nodes
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
            # S1-4：第 4 元素是编译沙箱冒烟延活转交 sid（编译成功+冒烟开启+续期成功才非 None）。
            # compile_runner 契约（integration_review）仍是三元组——sid 在此收进 out 参数，
            # 由 verify_l2 薄包装按 L2 最终结论统一处置（通过→入 state / 未通过→杀）。
            ran, ok, out, smoke_sid = nodes._run_reactor_build_in_sandbox(
                project_path, project_id, build_cmd, timeout=600
            )
            if smoke_sid:
                # 同一次 L2 可能多次调用编译器：新转交到来时先处置旧的，绝不叠泄漏。
                while _smoke_handoff:
                    _kill_sandbox_quiet(_smoke_handoff.pop())
                _smoke_handoff.append(smoke_sid)
            return ran, ok, out

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
        # R23-1 续（round25 #10）：_try_l2_sandbox_verify/_try_l2_local_verify 内含 subprocess.run
        # (timeout 180s)，是同步阻塞；verify_l2 是 async 节点——直接调用会卡死事件循环(SSE/心跳/并发)。
        # 与主路径 run_integration_review 同样卸到线程池(asyncio.to_thread 拷贝 contextvars，沙箱上下文照常)。
        sandbox_result = await asyncio.to_thread(
            nodes._try_l2_sandbox_verify, project_id, merged_diff, test_cmd, timeout=180
        )
        if sandbox_result is not None:
            logger.info("[VERIFY_L2] 沙箱结果: %s", "通过" if sandbox_result else "未通过")
            if not sandbox_result:
                return _l2_failure_state(subtask_results)
            return {"l2_passed": sandbox_result}

        local_result = await asyncio.to_thread(
            nodes._try_l2_local_verify,
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
    from swarm.brain import nodes  # A6：惰性导入破循环依赖（见 verify_l2）
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
                if not project_path:
                    # D34 fail-closed：push 开启但项目路径不可得 → 不能退回在默认 ref 上跑
                    # pipeline（merged_diff 根本不在那上面，绿=什么都没验证的假绿）。
                    logger.error(
                        "[VERIFY_L3] L3 push 已开启但项目路径不可得(project_id=%s) → "
                        "fail-closed 跳过 L3，不在默认 ref 上假绿", project_id,
                    )
                    return {
                        "l3_passed": None,
                        "l3_skipped": True,
                        "l3_message": "L3 push enabled but project path unavailable "
                                      "(fail-closed skip, not verified)",
                    }
                # R23-1 续（round25 #10）：push_merged_diff_branch 内含 git fetch/push(timeout 可达
                # 300s)，同步阻塞；verify_l3 是 async 节点 → 卸线程池，与下方 trigger_and_poll 同样处理。
                # base_commit＝任务钉扎基线：L3 apply 与 merged_diff 生成基线同源（round29 口径）。
                branch, push_err = await asyncio.to_thread(
                    push_merged_diff_branch,
                    project_path, merged_diff, task_id or "unknown",
                    base_ref=ref, base_commit=state.get("base_commit") or None,
                )
                if not branch:
                    # D34 fail-closed：push 失败绝不回退默认 ref——pipeline(默认 ref) 本来就绿，
                    # 等于未测任何变更的假绿。push 失败是 infra/基线问题而非代码验证失败，按
                    # "未执行"上报（l3_passed=None+skipped，gates/graph 均视为跳过），不伪装成
                    # False 误触发 HANDLE_FAILURE 把 infra 归因成验证失败。
                    logger.error(
                        "[VERIFY_L3] L3 push 失败 → fail-closed 跳过 L3"
                        "(不回退默认 ref 假绿): %s", push_err,
                    )
                    return {
                        "l3_passed": None,
                        "l3_skipped": True,
                        "l3_message": "L3 push failed, fail-closed skip (infra, not "
                                      f"verified): {push_err or 'unknown push failure'}",
                    }
                ref = branch
                logger.info("[VERIFY_L3] 已推送 L3 分支: %s", branch)

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


async def verify_runtime(state: BrainState) -> dict:
    """VERIFY_RUNTIME 节点 — 运行时冒烟闸门（S1-4 接线；推导层 task#16 + 探针层 task#17）。

    输入: project_stack, project_id, runtime_smoke_sandbox_id(L2 编译沙箱延活转交，可缺)
    输出三态（对齐 verify_l3 的 P1-12 语义，路由见 graph.after_verify_runtime）:
      passed  → runtime_smoke_passed=True，继续 VERIFY_L3；
      failed  → runtime_smoke_passed=False + verification_failure="runtime_smoke"
                （details 含 classification/log_tail，供 task#20 归因回灌）→ HANDLE_FAILURE；
      skipped → runtime_smoke_passed=None + runtime_smoke_skipped=True + degraded_reasons
                留痕（开关关/推导不全/沙箱不可得/环境缺失/不确定——skipped 永远可观测）。
    沙箱处置：无论转交还是自建，finally 必杀（本节点是第一责任人，设计 §2.3）。
    """
    from swarm.brain import nodes  # A6：惰性导入破循环依赖（见 verify_l2）
    from swarm.brain.nodes.runtime_smoke import (
        RUN_TIMEOUT_BUFFER_SEC,
        build_project_symbols,
        build_smoke_script,
        normalize_language_key,
        resolve_prepare_timeout_sec,
        resolve_smoke_timeout_sec,
        run_runtime_smoke,
    )
    from swarm.brain.smoke_derive import derive_runtime_smoke

    handoff_sid = str(state.get("runtime_smoke_sandbox_id") or "").strip()
    project_id = state.get("project_id", "")
    project_stack = state.get("project_stack") or {}

    async def _release_handoff() -> None:
        # 早退路径（开关关/推导不全…）也必须处置转交沙箱——verify_runtime 是唯一消费者。
        if handoff_sid:
            await asyncio.to_thread(_kill_sandbox_quiet, handoff_sid)

    # a. 杀开关（默认开；关闭走 skipped+degraded，绝不静默）
    if not _runtime_smoke_enabled():
        logger.info("[VERIFY_RUNTIME] SWARM_RUNTIME_SMOKE_ENABLED 关闭 → skipped（degraded 留痕）")
        await _release_handoff()
        return _runtime_skipped_state(
            "runtime_smoke_disabled",
            "运行时冒烟已被 SWARM_RUNTIME_SMOKE_ENABLED 显式关闭，未执行",
            {},
        )

    # b. 推导（纯函数层）：工作树路径与 verify_l2 同源取法（_get_project_path）——
    #    L2 主路径已在该工作树上本地 apply 过 merged_diff，推导读到的是【将交付的形态】。
    project_path = nodes._get_project_path(project_id)
    if not project_path:
        logger.warning("[VERIFY_RUNTIME] 项目工作树路径不可得(project_id=%s) → skipped", project_id)
        await _release_handoff()
        return _runtime_skipped_state(
            "project_path_unavailable", "项目工作树路径不可得，冒烟未执行", {})

    try:
        derivation = await asyncio.to_thread(derive_runtime_smoke, project_stack, project_path)
    except Exception as exc:  # noqa: BLE001 — derive 承诺不抛，纯防御：推导异常≠代码失败
        logger.warning("[VERIFY_RUNTIME] 冒烟推导异常 → skipped: %s", exc)
        await _release_handoff()
        return _runtime_skipped_state(
            "derivation_error", f"冒烟推导异常，未执行: {str(exc)[:200]}", {})

    if not derivation.start_cmd or derivation.port is None:
        # fail-closed：推不出就不猜（smoke_derive 铁律），如实报缺哪个 + 已有 evidence。
        missing = [name for name, val in
                   (("start_cmd", derivation.start_cmd), ("port", derivation.port))
                   if val is None]
        logger.info("[VERIFY_RUNTIME] 推导不全(缺 %s) → skipped；evidence=%s",
                    missing, derivation.evidence)
        await _release_handoff()
        return _apply_migration_patch(
            _runtime_skipped_state(
                "derivation_incomplete",
                f"启动方式推导不全（缺 {'/'.join(missing)}，不猜）；evidence={dict(derivation.evidence)}",
                {"missing": missing, "evidence": dict(derivation.evidence)},
            ),
            _migration_not_run_patch(derivation),
        )

    # 冒烟预算 = 探活窗口 + run_command 收尾缓冲 + 节点内建箱/重建余量（与 L2 侧转交续期同口径）
    # F1：prepare_cmd 存在时加 prepare 预算（构建产物命令，JVM package 可到数分钟）
    smoke_window = resolve_smoke_timeout_sec()
    prepare_budget = resolve_prepare_timeout_sec() if derivation.prepare_cmd else 0
    budget = smoke_window + RUN_TIMEOUT_BUFFER_SEC + 120 + prepare_budget

    from swarm.worker.sandbox import get_sandbox_manager
    manager = get_sandbox_manager()
    sandbox = None
    acquire_details: dict = {}
    migration_patch: dict = {}
    try:
        # c. 沙箱获取：优先 L2 延活转交，不成立回退自建+重建（同步阻塞卸线程池，R23-1 口径）
        sandbox, skip_reason, acquire_details = await asyncio.to_thread(
            _acquire_smoke_sandbox, manager, handoff_sid, project_id, project_path, budget,
        )
        if sandbox is None:
            return _apply_migration_patch(
                _runtime_skipped_state(
                    skip_reason or "sandbox_unavailable",
                    f"冒烟沙箱不可得({skip_reason})，未执行（环境问题非代码失败）",
                    acquire_details,
                ),
                _migration_not_run_patch(derivation),
            )
        # d. 探针执行（run_runtime_smoke 内部自带 to_thread + infra≠失败三分类）
        # F2：项目内符号索引（import 缺失归属判定）——建不出=None，分类器保守 dependency_missing
        try:
            project_symbols = await asyncio.to_thread(build_project_symbols, project_path)
        except Exception:  # noqa: BLE001 — 索引是证据面增强，缺失不阻断冒烟
            project_symbols = None
        script = build_smoke_script(
            derivation.start_cmd,
            derivation.port,
            derivation.health_path or "/",
            prepare_cmd=derivation.prepare_cmd,
            timeout_sec=smoke_window,
            workdir=get_config().sandbox.sandbox_remote_workdir,
        )
        res = await run_runtime_smoke(
            manager, sandbox, script,
            timeout_sec=smoke_window,
            language_key=normalize_language_key(project_stack.get("backend")),
            prepare_timeout_sec=prepare_budget or None,
            project_symbols=project_symbols,
            probe_port=derivation.port,
        )
        # d2. S1-5：migration phase——必须在 finally 杀箱【之前】（直接执行通道复用同一
        #     沙箱）。_run_migration_phase 承诺不抛，绝不把冒烟结论污染成 node_exception。
        migration_patch = await _run_migration_phase(
            manager, sandbox, derivation, project_stack, project_path, res,
        )
    except Exception as exc:  # noqa: BLE001 — infra 异常≠冒烟失败（D31 口径），如实 skipped
        logger.warning("[VERIFY_RUNTIME] 冒烟执行异常(infra) → skipped: %s", exc)
        return _apply_migration_patch(
            _runtime_skipped_state(
                "node_exception",
                f"冒烟节点异常(infra)，未执行: {str(exc)[:200]}",
                {**acquire_details, "error": str(exc)[:500]},
            ),
            _migration_not_run_patch(derivation),
        )
    finally:
        # e. finally 必杀：转交/自建一视同仁；转交不成立时旧 sid 也一并处置（幂等）。
        used_sid = str(getattr(sandbox, "sandbox_id", "") or "")
        for sid in {used_sid, handoff_sid} - {""}:
            await asyncio.to_thread(_kill_sandbox_quiet, sid)

    # f. 写 state（键均已在 BrainState 声明；runtime_smoke_sandbox_id 消费后清空防跨轮粘滞）
    details = {
        **res.details,
        "classification": res.classification,
        "log_tail": res.log_tail,
        "derivation_evidence": dict(derivation.evidence),
        "sandbox": acquire_details,
    }
    # S1-5：migration phase 结论并入（`_` 前缀是节点内部信号，绝不写进 state）
    mig_keys = {k: v for k, v in migration_patch.items() if not k.startswith("_")}
    mig_degraded = [migration_patch["_degraded"]] if migration_patch.get("_degraded") else []

    if migration_patch.get("_failed"):
        # migration 确定性 SQL 失败 → 并入 runtime 失败通道（task#20 的归因回灌统一消费）。
        # classification=migration_failed 专类留痕；冒烟自身结论保留在 smoke_* 供审计。
        # F3：migration 证据以 `migration` 前缀键并入 runtime_smoke_details——
        # shared.runtime_failure_evidence 按 startswith("migration") 契约消费，
        # 否则写侧(migration_verify_details)/读侧(runtime_smoke_details)形状断裂，
        # SQL 错误证据永远到不了归因回灌。
        logger.warning("[VERIFY_RUNTIME] migration 验证失败(确定性 SQL 证据) → runtime 失败通道: %s",
                       migration_patch.get("_message", ""))
        mig_details = migration_patch.get("migration_verify_details") or {}
        mig_ev = mig_details.get("evidence") or {}
        mig_channel = mig_details.get("channel") or {}
        mig_evidence_keys: dict = {}
        mig_output = str(mig_ev.get("output_tail") or "").strip() or "\n".join(
            str(ln) for ln in (mig_ev.get("log_lines") or []) if ln).strip()
        if mig_output:
            mig_evidence_keys["migration_output"] = mig_output
        if mig_ev.get("hits"):
            mig_evidence_keys["migration_hits"] = list(mig_ev["hits"])
        mig_cmd = mig_ev.get("command") or mig_channel.get("command")
        if mig_cmd:
            mig_evidence_keys["migration_command"] = str(mig_cmd)
        return {
            **_runtime_failure_state(),
            "runtime_smoke_message": f"migration 验证失败: {migration_patch.get('_message') or ''}".strip(),
            "runtime_smoke_details": {
                **details,
                **mig_evidence_keys,
                "classification": "migration_failed",
                "smoke_status": res.status,
                "smoke_classification": res.classification,
            },
            "runtime_smoke_sandbox_id": "",
            **mig_keys,
        }
    if res.status == "passed":
        logger.info("[VERIFY_RUNTIME] 冒烟通过: %s", res.message)
        out = {
            "runtime_smoke_passed": True,
            "runtime_smoke_skipped": False,
            "runtime_smoke_message": res.message,
            "runtime_smoke_details": details,
            "runtime_smoke_sandbox_id": "",
            **mig_keys,
        }
        if mig_degraded:
            out["degraded_reasons"] = mig_degraded
        return out
    if res.status == "failed":
        logger.warning("[VERIFY_RUNTIME] 冒烟失败(%s): %s", res.classification, res.message)
        out = {
            **_runtime_failure_state(),
            "runtime_smoke_message": res.message,
            "runtime_smoke_details": details,
            "runtime_smoke_sandbox_id": "",
            **mig_keys,
        }
        if mig_degraded:
            out["degraded_reasons"] = mig_degraded
        return out
    logger.info("[VERIFY_RUNTIME] 冒烟跳过(%s): %s", res.classification, res.message)
    return _apply_migration_patch(
        _runtime_skipped_state(res.classification, res.message, details), migration_patch)


def _acquire_smoke_sandbox(
    manager,
    handoff_sid: str,
    project_id: str,
    project_path: str,
    budget_sec: int,
) -> tuple[object | None, str | None, dict]:
    """冒烟沙箱获取（同步，供 to_thread）→ (sandbox|None, skip_reason|None, details)。

    ① 转交快路径（设计 §2.3）：state 只有 sid 字符串，活对象经进程内 manager._instances
       registry 取；须 try_extend_lifetime 续期成功、或 remaining_lifetime 足额才算成立。
    ② 回退自建：manager.create + tar sync + _detect_build_cmd_generic 重建构建产物——
       L2 已证编译通过，这里失败是环境问题 → 调用方按 skipped（rebuild_failed）处理，非 failed。
    自建失败路径内部即时销毁自建沙箱；成功返回的沙箱由 verify_runtime finally 统一处置。
    """
    details: dict = {}
    # ① 转交
    if handoff_sid:
        sandbox = getattr(manager, "_instances", {}).get(handoff_sid)
        if sandbox is not None:
            extended = False
            try:
                extended = bool(manager.try_extend_lifetime(sandbox, int(budget_sec)))
            except Exception:  # noqa: BLE001 — 续期异常按不成立处理，走寿命校验/回退
                extended = False
            if extended:
                details.update({"source": "handoff", "sandbox_id": handoff_sid, "extended": True})
                return sandbox, None, details
            remaining = None
            try:
                remaining = manager.remaining_lifetime(handoff_sid)
            except Exception:  # noqa: BLE001
                remaining = None
            if remaining is not None and remaining >= budget_sec:
                details.update({"source": "handoff", "sandbox_id": handoff_sid,
                                "extended": False, "remaining_lifetime": remaining})
                return sandbox, None, details
        logger.info("[VERIFY_RUNTIME] L2 转交沙箱 %s 不成立(已死/续期失败且寿命不足) → 回退自建",
                    handoff_sid)
        details["handoff_rejected"] = handoff_sid

    # ② 回退自建
    from swarm.brain import nodes as _nodes  # lazy：_sandbox_available 住 __init__（可 patch）
    if not _nodes._sandbox_available():
        return None, "sandbox_unavailable", details
    from pathlib import Path

    from swarm.brain.integration_review import _detect_build_cmd_generic

    workdir = get_config().sandbox.sandbox_remote_workdir
    try:
        sandbox = manager.create(project_id=project_id or None, source="verify_runtime")
    except Exception as exc:  # noqa: BLE001
        return None, "sandbox_create_failed", {**details, "error": str(exc)[:300]}
    sid = str(getattr(sandbox, "sandbox_id", "") or "")
    details.update({"source": "self_built", "sandbox_id": sid})
    try:
        manager.sync_project_to_sandbox(sandbox, Path(project_path), workdir)
        # 尽力把自建沙箱寿命对齐冒烟预算（D28；默认 900s 通常已够，失败不阻断）
        try:
            manager.try_extend_lifetime(sandbox, int(budget_sec))
        except Exception:  # noqa: BLE001
            pass
        build_cmd = _detect_build_cmd_generic(project_path)
        if build_cmd:
            result = manager.run_command(
                sandbox, f"cd {workdir} && ({build_cmd}); echo __RC__$?", timeout=600,
            )
            out = ((getattr(result, "stdout", "") or "")
                   + (getattr(result, "stderr", "") or ""))
            if "__RC__0" not in out:
                # L2 已证编译通过 → 此处失败/未执行(标记缺失)是环境问题 → skipped 非 failed
                details["rebuild_output"] = out[-1500:]
                details["rebuild_ran"] = "__RC__" in out
                _kill_sandbox_quiet(sid)
                return None, "rebuild_failed", details
            details["rebuilt"] = True
        else:
            details["rebuilt"] = False  # 无已知构建面（纯 docs 等）→ 无产物可建，直接冒烟
    except Exception as exc:  # noqa: BLE001 — sync/rebuild infra 异常 → 环境问题，杀箱后 skipped
        details["rebuild_error"] = str(exc)[:300]
        _kill_sandbox_quiet(sid)
        return None, "rebuild_failed", details
    return sandbox, None, details


def _runtime_skipped_state(reason: str, message: str, details: dict) -> dict:
    """冒烟 skipped 三态（对齐 verify_l3 的 fail-closed skip 形态 + l2_no_test_executed
    degraded 先例）：None=跳过≠失败，degraded_reasons 留痕保证 skipped 永远可观测。"""
    return {
        "runtime_smoke_passed": None,
        "runtime_smoke_skipped": True,
        "runtime_smoke_message": message,
        "runtime_smoke_details": {**(details or {}), "skip_reason": reason},
        # 转交沙箱已消费/处置 → 清空，防 replan 重入时读到死 sid（last-write-wins 粘滞）
        "runtime_smoke_sandbox_id": "",
        # S1-5：migration 结论默认=未验证（冒烟没跑到的早退路径也不留上一轮粘滞值；
        # 有真实 migration phase 结果时由 _apply_migration_patch 覆盖）
        "migration_verify_passed": None,
        "migration_verify_details": {"reason": "smoke_not_executed"},
        "degraded_reasons": [f"runtime_smoke_skipped:{reason}"],
    }


def _migration_not_run_patch(derivation) -> dict:
    """S1-5：冒烟没跑到执行阶段时的 migration 跟随结论（沙箱不可得/推导不全/节点异常）。

    kind 未检出 → 常态 no_migration_detected（不进 degraded）；kind 已检出但没机会验证
    → skipped 跟随 + degraded 留痕（skipped 永远可观测铁律）。"""
    kind = getattr(derivation, "migration_kind", None) if derivation is not None else None
    if not kind:
        return {"migration_verify_passed": None,
                "migration_verify_details": {"reason": "no_migration_detected"}}
    return {"migration_verify_passed": None,
            "migration_verify_details": {"reason": "smoke_not_executed", "kind": kind},
            "_degraded": "migration_verify_skipped:smoke_not_executed"}


def _apply_migration_patch(base: dict, mig: dict) -> dict:
    """migration phase 结论并入节点 patch。`_` 前缀键是节点内部信号（_degraded/_failed/
    _message），绝不写进 state（BrainState 只认声明键）；degraded_reasons 追加不覆盖。"""
    out = dict(base)
    out.update({k: v for k, v in mig.items() if not k.startswith("_")})
    if mig.get("_degraded"):
        out["degraded_reasons"] = list(out.get("degraded_reasons") or []) + [mig["_degraded"]]
    return out


async def _run_migration_phase(manager, sandbox, derivation, project_stack,
                               project_path: str, smoke_res) -> dict:
    """S1-5：migration 验证 phase（冒烟执行后、finally 杀箱【之前】调用——直接执行
    通道复用冒烟同一沙箱）。承诺不抛：migration 侧任何异常绝不污染冒烟结论。

    返回 patch 片段：migration_verify_passed/details（state 键）+ 内部信号
    `_failed`（确定性 SQL 失败 → 调用方并入 runtime 失败通道）/`_degraded`/`_message`。
    """
    kind = getattr(derivation, "migration_kind", None)
    if not kind:
        # 没有 migration 是常态不是降级 → None + reason，不进 degraded
        return {"migration_verify_passed": None,
                "migration_verify_details": {"reason": "no_migration_detected"}}
    try:
        from swarm.brain import migration_verify as mv
        channel = await asyncio.to_thread(
            mv.detect_migration_channel, kind, project_stack, project_path)
        if channel.runs_on_startup:
            # 通道①：寄生冒烟启动——从启动日志收割结论（冒烟没过则自然跟随 skipped）
            result = mv.harvest_startup_migration(kind, smoke_res.status, smoke_res.log_tail)
        elif channel.executable and channel.command:
            if smoke_res.status == "failed":
                # 冒烟本身 failed → migration 不执行，跟随 skipped（别在已判失败的现场加戏）
                result = mv.MigrationVerifyResult(
                    "skipped", "smoke_failed", "冒烟失败，migration 不执行（跟随 skipped）")
            else:
                # 审C：直接执行通道前给沙箱寿命续上 migration 执行预算——冒烟预算公式
                # 未给本阶段预留，探活窗口耗尽后剩余寿命可能不足。失败不阻断（900s 默认
                # 寿命通常够），但如实留痕 details 供排障。
                lifetime_extended: bool | None = None
                try:
                    lifetime_extended = bool(await asyncio.to_thread(
                        manager.try_extend_lifetime, sandbox,
                        mv.MIGRATION_EXEC_TIMEOUT_SEC + 60))
                except Exception as ext_exc:  # noqa: BLE001 — 续期尽力而为
                    lifetime_extended = False
                    logger.debug("[VERIFY_RUNTIME] migration 执行前续期异常(不阻断): %s", ext_exc)
                # 通道②：嵌入式 DB 直接执行（冒烟同一沙箱，__RC__ 口径 + infra≠失败）
                result = await mv.execute_migration(
                    manager, sandbox, channel.command,
                    workdir=get_config().sandbox.sandbox_remote_workdir)
                result.evidence.setdefault("lifetime_extended", lifetime_extended)
        else:
            # 通道③：无可执行通道（真实外部 DB/证据不全/无引擎）→ 诚实 skipped+reason
            result = mv.MigrationVerifyResult(
                "skipped", channel.reason or "not_executable",
                f"migration({kind}) 无可执行通道: {channel.reason}")
        details = {
            "kind": kind,
            "channel": {"executable": channel.executable,
                        "runs_on_startup": channel.runs_on_startup,
                        "reason": channel.reason, "command": channel.command,
                        "evidence": dict(channel.evidence)},
            "reason": result.reason,
            "message": result.message,
            "evidence": dict(result.evidence),
        }
        if result.status == "passed":
            logger.info("[VERIFY_RUNTIME] migration 验证通过(%s): %s", result.reason, result.message)
            return {"migration_verify_passed": True, "migration_verify_details": details}
        if result.status == "failed":
            return {"migration_verify_passed": False, "migration_verify_details": details,
                    "_failed": True, "_message": result.message}
        logger.info("[VERIFY_RUNTIME] migration 验证跳过(%s): %s", result.reason, result.message)
        return {"migration_verify_passed": None, "migration_verify_details": details,
                "_degraded": f"migration_verify_skipped:{result.reason}"}
    except Exception as exc:  # noqa: BLE001 — migration phase 异常绝不冤枉冒烟结论
        logger.warning("[VERIFY_RUNTIME] migration phase 异常 → skipped: %s", exc)
        return {"migration_verify_passed": None,
                "migration_verify_details": {"reason": "migration_phase_error",
                                             "kind": kind, "error": str(exc)[:300]},
                "_degraded": "migration_verify_skipped:migration_phase_error"}


def _runtime_failure_state() -> dict:
    """冒烟 failed 态（对齐 _l3_failure_state 形态）：专类 verification_failure 供
    handle_failure 归因——绝不落 "l2" 分支误触发编译失败的定向恢复链。"""
    return {
        "runtime_smoke_passed": False,
        "runtime_smoke_skipped": False,
        "verification_failure": "runtime_smoke",
        # S1-4 占位与 failure.py 占位分支对齐（有界 escalate）；task#20 完整归因替换。
        "failure_strategy": "escalate",
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
