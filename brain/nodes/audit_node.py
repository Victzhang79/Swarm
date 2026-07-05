"""brain/nodes/audit.py — AUDIT 意图执行分支（round24 god-file 拆解·簇D）。

安全审计节点：跑安全扫描、产结构化报告（不产 diff）。叶函数——仅依赖 swarm.types /
swarm.audit / worker.security_scan / config，无 nodes/__init__ helper 调用、无测试 patch，
经 nodes/__init__ re-export 保 swarm.brain.nodes._run_security_audit 可寻址。
"""

from __future__ import annotations

import logging

from swarm.audit import audit
from swarm.types import Confidence, SubTask, WorkerOutput

logger = logging.getLogger(__name__)


async def _run_security_audit(
    subtask: SubTask,
    project_path: str | None,
    *,
    project_id: str = "",
    task_id: str = "",
) -> WorkerOutput:
    """AUDIT 意图执行分支：跑安全扫描，产结构化报告(不产 diff)。

    阻断/仅报告双模式由 WorkerConfig.security_block_severity 控制：
    - critical/high：发现该级别漏洞 → should_block → l1_passed=False(阻断交付)
    - none：仅报告，永不阻断(l1_passed=True)
    """
    import asyncio as _asyncio

    from swarm.config.settings import get_config

    lang = getattr(getattr(subtask, "harness", None), "language", "") or ""
    block_severity = get_config().worker.security_block_severity

    audit(
        "security_audit_start",
        orchestrator="Brain",
        executor="Worker",
        task_id=task_id,
        subtask_id=subtask.id,
        language=lang,
        block_severity=block_severity,
    )

    # N-01 fail-closed 判据：仅用于【扫描器崩溃】路径——我们【有】东西可扫但扫挂了，
    # 在阻断模式(block_severity != "none")下"扫不了"绝不能与"真·零漏洞"混同放行。
    # report-only(none)模式是运维明示"永不阻断"，此时保持不阻断(可观测性不误杀)。
    # 注意：无 project_path 是【编排未提供可扫对象】(非攻击面/非扫描失败)，按既有契约安全跳过。
    _audit_fail_closed = block_severity != "none"

    if not project_path:
        logger.warning("[AUDIT] 子任务 %s 无项目路径，安全审计跳过", subtask.id)
        return WorkerOutput(
            subtask_id=subtask.id,
            diff="",
            summary="安全审计跳过：无项目路径",
            confidence=Confidence.LOW,
            l1_passed=True,  # 无路径=无可扫对象，安全跳过不误杀（既有契约）
            l1_details={"mode": "audit", "skipped": "no_project_path"},
            audit_findings=[],
        )

    def _scan() -> tuple[list, bool]:
        from swarm.worker.security_scan import run_security_scan

        scope_files = list(
            getattr(subtask.scope, "writable", []) or []
        ) + list(getattr(subtask.scope, "readable", []) or [])
        return run_security_scan(
            project_path,
            lang,
            files=scope_files or None,
            block_severity=block_severity,
        )

    try:
        findings, should_block = await _asyncio.get_running_loop().run_in_executor(None, _scan)
    except Exception as exc:  # noqa: BLE001
        logger.error("[AUDIT] 安全扫描失败: %s (fail_closed=%s)", exc, _audit_fail_closed)
        return WorkerOutput(
            subtask_id=subtask.id,
            diff="",
            summary=f"安全审计执行失败: {exc}",
            confidence=Confidence.LOW,
            # N-01：阻断模式下扫描器崩溃→fail-closed(不可与"真零漏洞"混同)；none 模式不阻断
            l1_passed=not _audit_fail_closed,
            l1_details={
                "mode": "audit",
                "error": str(exc),
                "fail_closed": _audit_fail_closed,
                "block_severity": block_severity,
            },
            audit_findings=[],
        )

    by_sev: dict[str, int] = {}
    for f in findings:
        sev = f.severity.value if hasattr(f.severity, "value") else str(f.severity)
        by_sev[sev] = by_sev.get(sev, 0) + 1
    summary = (
        f"安全审计完成：{len(findings)} 项发现 "
        f"({', '.join(f'{k}={v}' for k, v in sorted(by_sev.items())) or '无'})"
        f"；block_severity={block_severity} → {'阻断交付' if should_block else '通过'}"
    )
    audit(
        "security_audit_done",
        orchestrator="Brain",
        executor="Worker",
        task_id=task_id,
        subtask_id=subtask.id,
        findings=len(findings),
        should_block=should_block,
        by_severity=by_sev,
    )
    logger.info("[AUDIT] %s | %s", subtask.id, summary)
    return WorkerOutput(
        subtask_id=subtask.id,
        diff="",  # 审计不产 diff
        summary=summary,
        confidence=Confidence.HIGH,
        l1_passed=not should_block,  # 阻断模式下有高危发现即 L1 不通过
        l1_details={
            "mode": "audit",
            "l1_decision_source": "deterministic",
            "findings_total": len(findings),
            "by_severity": by_sev,
            "block_severity": block_severity,
            "should_block": should_block,
        },
        audit_findings=findings,
    )
