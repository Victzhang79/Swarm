#!/usr/bin/env python3
"""Swarm Phase 0–1 acceptance benchmark — measures task accept rate against fixtures."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

# Built-in fixture set (no LLM needed for script logic)
BENCHMARK_FIXTURES: tuple[dict[str, str], ...] = (
    {
        "id": "readme-comment",
        "description": (
            "Add a one-line HTML comment at the top of README.md: "
            "<!-- Swarm benchmark fixture -->"
        ),
        "difficulty": "trivial",
    },
    {
        "id": "docstring-typo",
        "description": (
            "Fix a typo in any module docstring: change 'teh' to 'the' if present, "
            "otherwise add a one-line module docstring to a small Python file."
        ),
        "difficulty": "trivial",
    },
    {
        "id": "license-year",
        "description": (
            "If LICENSE exists, ensure the copyright line mentions 2026; "
            "if missing, add a one-line MIT license header comment to README.md."
        ),
        "difficulty": "trivial",
    },
    {
        "id": "gitignore-entry",
        "description": (
            "Add '.swarm-benchmark' to .gitignore if not already present "
            "(create .gitignore with that single line if the file is missing)."
        ),
        "difficulty": "trivial",
    },
    {
        "id": "contributing-note",
        "description": (
            "Add a short '## Development' section to README.md with one bullet: "
            "'Run tests before committing.' Skip if the section already exists."
        ),
        "difficulty": "trivial",
    },
)

TERMINAL_TASK_STATUSES = frozenset({"DELIVERING", "DONE", "FAILED"})
SUCCESS_TASK_STATUSES = frozenset({"DELIVERING", "DONE"})
DEFAULT_TIMEOUT_S = 120
DEFAULT_THRESHOLD = 0.6
POLL_INTERVAL_S = 2.0


@dataclass
class TaskOutcome:
    fixture_id: str
    description: str
    accepted: bool
    terminal_status: str
    phase: int
    run_id: str | None = None
    task_id: str | None = None
    error: str | None = None
    elapsed_s: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


def compute_accept_rate(outcomes: list[TaskOutcome]) -> float:
    """Return approved_or_success / total (0.0 when total is 0)."""
    if not outcomes:
        return 0.0
    accepted = sum(1 for o in outcomes if o.accepted)
    return accepted / len(outcomes)


def is_worker_success(event: dict[str, Any]) -> bool:
    """Phase 0: worker run succeeded when complete without error."""
    step = event.get("step", "")
    if step == "error":
        return False
    if step != "complete":
        return False
    result = event.get("result") or {}
    if isinstance(result, dict):
        if result.get("l1_passed") is True:
            return True
        diff = result.get("diff") or ""
        if diff.strip():
            return True
    return event.get("status") == "done"


def is_task_success(status: str) -> bool:
    """Phase 1: DELIVERING or DONE counts as accepted."""
    return status in SUCCESS_TASK_STATUSES


def meets_threshold(accept_rate: float, threshold: float) -> bool:
    return accept_rate >= threshold


def build_report(
    *,
    project_id: str,
    phase: int,
    outcomes: list[TaskOutcome],
    threshold: float,
    dry_run: bool,
    api_url: str,
) -> dict[str, Any]:
    total = len(outcomes)
    accepted = sum(1 for o in outcomes if o.accepted)
    rate = compute_accept_rate(outcomes) if not dry_run else None
    passed = dry_run or (rate is not None and meets_threshold(rate, threshold))
    return {
        "dry_run": dry_run,
        "api_url": api_url,
        "project_id": project_id,
        "phase": phase,
        "threshold": threshold,
        "total": total,
        "accepted": accepted if not dry_run else None,
        "accept_rate": round(rate, 4) if rate is not None else None,
        "passed": passed,
        "tasks": [asdict(o) for o in outcomes],
    }


def format_summary(report: dict[str, Any]) -> str:
    lines = [
        "=== Swarm Acceptance Benchmark ===",
        f"Project: {report['project_id']}  Phase: {report['phase']}  "
        f"Dry-run: {report['dry_run']}",
    ]
    if report["dry_run"]:
        lines.append(f"Fixtures ({report['total']}):")
        for task in report["tasks"]:
            desc = task["description"]
            if len(desc) > 72:
                desc = desc[:72] + "…"
            lines.append(f"  - [{task['fixture_id']}] {desc}")
        lines.append("No API calls made (--dry-run).")
        return "\n".join(lines)

    rate_pct = (report["accept_rate"] or 0) * 100
    lines.append(
        f"Accept rate: {report['accepted']}/{report['total']} "
        f"({rate_pct:.1f}%)  threshold={report['threshold'] * 100:.0f}%"
    )
    for task in report["tasks"]:
        mark = "✅" if task["accepted"] else "❌"
        status = task.get("terminal_status") or "?"
        err = task.get("error")
        suffix = f" — {err}" if err else ""
        lines.append(f"  {mark} [{task['fixture_id']}] {status}{suffix}")
    verdict = "PASS" if report["passed"] else "FAIL"
    lines.append(f"Result: {verdict}")
    return "\n".join(lines)


def consume_worker_stream(response: httpx.Response) -> tuple[str, dict[str, Any] | None, str | None]:
    """Read SSE stream until terminal worker event."""
    event_type = "progress"
    last_result: dict[str, Any] | None = None
    for raw_line in response.iter_lines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("event:"):
            event_type = line.split(":", 1)[1].strip()
            continue
        if not line.startswith("data:"):
            continue
        data_str = line.split(":", 1)[1].strip()
        if not data_str:
            continue
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        step = data.get("step", "")
        if step == "result" or event_type == "result":
            candidate = data.get("result")
            if isinstance(candidate, dict):
                last_result = candidate
        if step == "complete":
            merged = {**data, "step": "complete", "result": last_result or data.get("result")}
            if is_worker_success(merged):
                return "complete", merged, None
            return "failed", merged, "worker did not pass acceptance"
        if step == "error":
            return "error", data, data.get("message") or "worker error"
    if last_result is not None:
        merged = {"step": "complete", "status": "done", "result": last_result}
        if is_worker_success(merged):
            return "complete", merged, None
        return "failed", merged, "worker did not pass acceptance"
    return "timeout", None, "no terminal event from worker stream"


def poll_worker_run(
    client: httpx.Client,
    api_url: str,
    run_id: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[str, dict[str, Any] | None, str | None]:
    """Stream worker SSE until complete/error or timeout."""
    url = f"{api_url}/api/worker/{run_id}/stream"
    try:
        with client.stream(
            "GET",
            url,
            timeout=httpx.Timeout(timeout_s + 10, connect=10.0),
        ) as resp:
            if resp.status_code >= 400:
                body = resp.read().decode(errors="replace")[:500]
                return "error", None, f"HTTP {resp.status_code}: {body}"
            return consume_worker_stream(resp)
    except httpx.TimeoutException:
        return "timeout", None, f"worker stream timed out after {timeout_s}s"
    except httpx.HTTPError as exc:
        return "error", None, str(exc)


def poll_task_status(
    client: httpx.Client,
    api_url: str,
    task_id: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[str, dict[str, Any] | None, str | None]:
    """Poll GET /api/tasks/{id} until DELIVERING/DONE/FAILED or timeout."""
    url = f"{api_url}/api/tasks/{task_id}"
    deadline = time.monotonic() + timeout_s
    last_status = ""
    last_task: dict[str, Any] | None = None

    while time.monotonic() < deadline:
        try:
            resp = client.get(url, timeout=15.0)
            if resp.status_code >= 400:
                return "error", None, f"HTTP {resp.status_code}: {resp.text[:500]}"
            body = resp.json()
            task = body.get("task") or body
            last_task = task
            status = task.get("status", "")
            if status != last_status:
                last_status = status
            if status in TERMINAL_TASK_STATUSES:
                return status, task, None
        except httpx.HTTPError as exc:
            return "error", None, str(exc)
        time.sleep(POLL_INTERVAL_S)

    return "timeout", last_task, f"task poll timed out after {timeout_s}s (last={last_status})"


def run_phase0_task(
    client: httpx.Client,
    api_url: str,
    project_id: str,
    fixture: dict[str, str],
    timeout_s: float,
) -> TaskOutcome:
    start = time.monotonic()
    fixture_id = fixture["id"]
    description = fixture["description"]
    try:
        resp = client.post(
            f"{api_url}/api/projects/{project_id}/worker/run",
            json={"description": description, "difficulty": fixture.get("difficulty", "trivial")},
            timeout=30.0,
        )
        if resp.status_code >= 400:
            return TaskOutcome(
                fixture_id=fixture_id,
                description=description,
                accepted=False,
                terminal_status="error",
                phase=0,
                error=f"HTTP {resp.status_code}: {resp.text[:500]}",
                elapsed_s=time.monotonic() - start,
            )
        run_id = resp.json().get("run_id")
        if not run_id:
            return TaskOutcome(
                fixture_id=fixture_id,
                description=description,
                accepted=False,
                terminal_status="error",
                phase=0,
                error="API response missing run_id",
                elapsed_s=time.monotonic() - start,
            )
        terminal, event, err = poll_worker_run(client, api_url, run_id, timeout_s)
        accepted = terminal == "complete" and event is not None and is_worker_success(event)
        details: dict[str, Any] = {}
        if event and event.get("result"):
            result = event["result"]
            if isinstance(result, dict):
                details["l1_passed"] = result.get("l1_passed")
                details["diff_lines"] = len((result.get("diff") or "").splitlines())
        return TaskOutcome(
            fixture_id=fixture_id,
            description=description,
            accepted=accepted,
            terminal_status=terminal,
            phase=0,
            run_id=run_id,
            error=err,
            elapsed_s=time.monotonic() - start,
            details=details,
        )
    except httpx.HTTPError as exc:
        return TaskOutcome(
            fixture_id=fixture_id,
            description=description,
            accepted=False,
            terminal_status="error",
            phase=0,
            error=str(exc),
            elapsed_s=time.monotonic() - start,
        )


def run_phase1_task(
    client: httpx.Client,
    api_url: str,
    project_id: str,
    fixture: dict[str, str],
    timeout_s: float,
) -> TaskOutcome:
    start = time.monotonic()
    fixture_id = fixture["id"]
    description = fixture["description"]
    try:
        resp = client.post(
            f"{api_url}/api/projects/{project_id}/tasks",
            json={"description": description, "auto_accept": True},
            timeout=30.0,
        )
        if resp.status_code >= 400:
            return TaskOutcome(
                fixture_id=fixture_id,
                description=description,
                accepted=False,
                terminal_status="error",
                phase=1,
                error=f"HTTP {resp.status_code}: {resp.text[:500]}",
                elapsed_s=time.monotonic() - start,
            )
        body = resp.json()
        task = body.get("task") or body
        task_id = task.get("id")
        if not task_id:
            return TaskOutcome(
                fixture_id=fixture_id,
                description=description,
                accepted=False,
                terminal_status="error",
                phase=1,
                error="API response missing task id",
                elapsed_s=time.monotonic() - start,
            )
        terminal, task_data, err = poll_task_status(client, api_url, task_id, timeout_s)
        accepted = is_task_success(terminal)
        details: dict[str, Any] = {}
        if task_data:
            details["merged_diff_len"] = len(task_data.get("merged_diff") or "")
        return TaskOutcome(
            fixture_id=fixture_id,
            description=description,
            accepted=accepted,
            terminal_status=terminal,
            phase=1,
            task_id=task_id,
            error=err,
            elapsed_s=time.monotonic() - start,
            details=details,
        )
    except httpx.HTTPError as exc:
        return TaskOutcome(
            fixture_id=fixture_id,
            description=description,
            accepted=False,
            terminal_status="error",
            phase=1,
            error=str(exc),
            elapsed_s=time.monotonic() - start,
        )


def run_benchmark(
    api_url: str,
    project_id: str,
    phase: int,
    threshold: float,
    timeout_s: float,
    dry_run: bool,
    post_report: bool = True,
) -> tuple[dict[str, Any], int]:
    api_url = api_url.rstrip("/")
    if dry_run:
        outcomes = [
            TaskOutcome(
                fixture_id=f["id"],
                description=f["description"],
                accepted=False,
                terminal_status="dry_run",
                phase=phase,
            )
            for f in BENCHMARK_FIXTURES
        ]
        report = build_report(
            project_id=project_id,
            phase=phase,
            outcomes=outcomes,
            threshold=threshold,
            dry_run=True,
            api_url=api_url,
        )
        return report, 0

    outcomes: list[TaskOutcome] = []
    runner = run_phase0_task if phase == 0 else run_phase1_task
    with httpx.Client() as client:
        try:
            health = client.get(f"{api_url}/api/health", timeout=10.0)
            if health.status_code >= 400:
                report = build_report(
                    project_id=project_id,
                    phase=phase,
                    outcomes=[],
                    threshold=threshold,
                    dry_run=False,
                    api_url=api_url,
                )
                report["error"] = f"API health check failed: HTTP {health.status_code}"
                return report, 1
        except httpx.HTTPError as exc:
            report = build_report(
                project_id=project_id,
                phase=phase,
                outcomes=[],
                threshold=threshold,
                dry_run=False,
                api_url=api_url,
            )
            report["error"] = f"Cannot reach API: {exc}"
            return report, 1

        for fixture in BENCHMARK_FIXTURES:
            outcomes.append(runner(client, api_url, project_id, fixture, timeout_s))

    report = build_report(
        project_id=project_id,
        phase=phase,
        outcomes=outcomes,
        threshold=threshold,
        dry_run=False,
        api_url=api_url,
    )
    exit_code = 0 if report["passed"] else 1
    if post_report:
        _maybe_post_milestone(api_url, project_id, phase, report, exit_code)
    return report, exit_code


def _maybe_post_milestone(
    api_url: str,
    project_id: str,
    phase: int,
    report: dict[str, Any],
    exit_code: int,
) -> None:
    """POST /api/milestones 持久化报告（失败不阻断 exit code）。"""
    try:
        with httpx.Client(timeout=15.0) as client:
            client.post(
                f"{api_url.rstrip('/')}/api/milestones",
                json={
                    "project_id": project_id,
                    "phase": str(phase),
                    "accept_rate": report.get("accept_rate"),
                    "threshold": report.get("threshold"),
                    "passed": exit_code == 0,
                    "report": report,
                },
            )
    except httpx.HTTPError:
        pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Swarm Phase 0/1 acceptance benchmark — measure task accept rate",
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:8420", help="Swarm API base URL")
    parser.add_argument("--project-id", required=True, help="Target project ID")
    parser.add_argument(
        "--phase",
        type=int,
        choices=(0, 1),
        default=0,
        help="0=worker/run only, 1=brain tasks via POST /tasks",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help="Minimum accept_rate to exit 0 (default 0.6)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help="Per-task timeout in seconds (default 120)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List fixture tasks without calling the API",
    )
    parser.add_argument(
        "--no-post-report",
        action="store_true",
        help="Do not POST report to /api/milestones",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report, exit_code = run_benchmark(
        api_url=args.api_url,
        project_id=args.project_id,
        phase=args.phase,
        threshold=args.threshold,
        timeout_s=args.timeout,
        dry_run=args.dry_run,
        post_report=not args.no_post_report,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print()
    print(format_summary(report))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
