#!/usr/bin/env python3
"""Tests for scripts/benchmark_accept_rate.py"""

from __future__ import annotations

import importlib.util
import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import benchmark_accept_rate as bench


def test_compute_accept_rate():
    outcomes = [
        bench.TaskOutcome("a", "d", True, "complete", 0),
        bench.TaskOutcome("b", "d", False, "failed", 0),
        bench.TaskOutcome("c", "d", True, "complete", 0),
    ]
    assert bench.compute_accept_rate(outcomes) == 2 / 3
    assert bench.compute_accept_rate([]) == 0.0
    print("  ✅ compute_accept_rate")


def test_is_worker_success():
    assert bench.is_worker_success({"step": "error"}) is False
    assert bench.is_worker_success({"step": "complete", "result": {"l1_passed": True}}) is True
    assert bench.is_worker_success({"step": "complete", "result": {"diff": "--- a\n+++ b\n"}}) is True
    assert bench.is_worker_success({"step": "complete", "status": "done"}) is True
    assert bench.is_worker_success({"step": "complete", "result": {}}) is False
    print("  ✅ is_worker_success")


def test_is_task_success_and_meets_threshold():
    assert bench.is_task_success("DELIVERING") is True
    assert bench.is_task_success("DONE") is True
    assert bench.is_task_success("FAILED") is False
    assert bench.meets_threshold(0.6, 0.6) is True
    assert bench.meets_threshold(0.59, 0.6) is False
    print("  ✅ is_task_success / meets_threshold")


def test_build_report_and_summary():
    outcomes = [
        bench.TaskOutcome("x", "desc", True, "complete", 0, run_id="r1"),
        bench.TaskOutcome("y", "desc", False, "failed", 0, error="timeout"),
    ]
    report = bench.build_report(
        project_id="proj-1",
        phase=0,
        outcomes=outcomes,
        threshold=0.6,
        dry_run=False,
        api_url="http://localhost:8420",
    )
    assert report["accept_rate"] == 0.5
    assert report["passed"] is False
    summary = bench.format_summary(report)
    assert "Accept rate: 1/2" in summary
    assert "FAIL" in summary
    print("  ✅ build_report / format_summary")


def test_dry_run_mode():
    buf = StringIO()
    with patch("sys.stdout", buf):
        code = bench.main(["--project-id", "test-proj", "--dry-run", "--phase", "1"])
    out = buf.getvalue()
    assert code == 0
    report = json.loads(out.split("\n\n")[0])
    assert report["dry_run"] is True
    assert report["total"] == len(bench.BENCHMARK_FIXTURES)
    assert report["accept_rate"] is None
    assert all(t["terminal_status"] == "dry_run" for t in report["tasks"])
    assert "No API calls made" in out
    print("  ✅ dry-run mode")


def test_consume_worker_stream_complete():
    lines = [
        'event: progress',
        'data: {"step":"start","status":"running"}',
        "",
        'event: result',
        'data: {"step":"result","result":{"l1_passed":true,"diff":"+x"}}',
        "",
        'event: progress',
        'data: {"step":"complete","status":"done"}',
        "",
    ]
    resp = MagicMock()
    resp.iter_lines.return_value = iter(lines)
    status, event, err = bench.consume_worker_stream(resp)
    assert status == "complete"
    assert err is None
    assert event is not None and event["result"]["l1_passed"] is True
    print("  ✅ consume_worker_stream")


def test_run_phase0_task_mock_httpx():
    fixture = bench.BENCHMARK_FIXTURES[0]
    post_resp = MagicMock()
    post_resp.status_code = 200
    post_resp.json.return_value = {"run_id": "run-abc"}

    stream_resp = MagicMock()
    stream_resp.status_code = 200
    stream_resp.iter_lines.return_value = iter([
        'data: {"step":"result","result":{"l1_passed":true,"diff":"+line"}}',
        "",
        'data: {"step":"complete","status":"done"}',
        "",
    ])
    stream_resp.__enter__ = MagicMock(return_value=stream_resp)
    stream_resp.__exit__ = MagicMock(return_value=False)

    client = MagicMock()
    client.post.return_value = post_resp
    client.stream.return_value = stream_resp

    outcome = bench.run_phase0_task(
        client, "http://127.0.0.1:8420", "proj-1", fixture, timeout_s=30
    )
    assert outcome.accepted is True
    assert outcome.terminal_status == "complete"
    assert outcome.run_id == "run-abc"
    client.post.assert_called_once()
    client.stream.assert_called_once()
    print("  ✅ run_phase0_task mock httpx")


def main() -> int:
    tests = [
        test_compute_accept_rate,
        test_is_worker_success,
        test_is_task_success_and_meets_threshold,
        test_build_report_and_summary,
        test_dry_run_mode,
        test_consume_worker_stream_complete,
        test_run_phase0_task_mock_httpx,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
            import traceback

            traceback.print_exc()
    if failed:
        print(f"\n{failed}/{len(tests)} failed")
        return 1
    print(f"\n✅ 全部 {len(tests)} 项 benchmark 测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
