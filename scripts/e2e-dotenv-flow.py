#!/usr/bin/env python3
"""python-dotenv 主链路 E2E：预处理 → 检索 → 建任务 → Brain → Worker → diff → 学习"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

os.environ.setdefault("PYTHONUNBUFFERED", "1")

BASE = "http://127.0.0.1:8420"
PROJECT_ID = "e6ca3f4a-bca0-4bab-86f1-4195fceb8cb3"
# 足够简单：启发式 SIMPLE + 单子任务 trivial + 小模型快速路径
TASK_DESC = (
    "在 src/dotenv/main.py 中为 load_dotenv 函数添加一行英文 docstring："
    "'Load environment variables from a .env file.' 只改这一个函数。"
)


def log(msg: str) -> None:
    print(msg, flush=True)


def req(method: str, path: str, body: dict | None = None, timeout: int = 120) -> dict:
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        raise RuntimeError(f"{method} {path} → HTTP {e.code}: {err[:500]}") from e


def poll_preprocess(force: bool = False, max_wait: int = 600) -> dict:
    status = req("GET", f"/api/projects/{PROJECT_ID}/preprocess/status")
    p = status.get("progress") or {}
    if not force and p.get("phase") == "complete":
        log(f"▶ 预处理已完成，跳过 (files={((p.get('scan_stats') or {}).get('files'))})")
        return p

    log("▶ 触发预处理…")
    req("POST", f"/api/projects/{PROJECT_ID}/preprocess")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        data = req("GET", f"/api/projects/{PROJECT_ID}/preprocess/status")
        p = data.get("progress") or {}
        phase = p.get("phase", "?")
        pct = int(float(p.get("phase_progress", 0)) * 100)
        log(f"  预处理 {phase} {pct}% — {p.get('message', '')[:80]}")
        if phase == "complete":
            return p
        if phase == "error":
            raise RuntimeError(p.get("error") or p.get("message") or "preprocess error")
        time.sleep(2)
    raise TimeoutError("preprocess timeout")


def experiment_retrieve() -> dict:
    log("▶ 知识库 + Harness 检索实验…")
    out = req("POST", f"/api/projects/{PROJECT_ID}/knowledge/retrieve", {"query": TASK_DESC})
    stats = out.get("stats") or {}
    log(
        f"  命中 struct={stats.get('struct_count')} semantic={stats.get('semantic_count')} "
        f"harness={stats.get('norms_count')} mistakes={stats.get('mistakes_count')} "
        f"prompt={out.get('prompt_chars')} chars"
    )
    return out


def check_sandboxes() -> None:
    sb = req("GET", "/api/sandbox/status")
    log(f"▶ 沙箱: active={sb.get('active_count')} use_for_worker={((sb.get('config') or {}).get('use_for_worker'))}")


def run_task(task_id: str | None = None, max_wait: int = 900) -> dict:
    if task_id:
        log(f"▶ 继续监控已有任务 {task_id}…")
    else:
        log("▶ 创建任务（auto_accept=true）…")
        created = req(
            "POST",
            f"/api/projects/{PROJECT_ID}/tasks",
            {"description": TASK_DESC, "auto_accept": True},
        )
        task = created.get("task") or {}
        task_id = task.get("id")
        if not task_id:
            raise RuntimeError(f"no task id: {created}")
        log(f"  task_id={task_id}")

    deadline = time.time() + max_wait
    last_status = ""
    last_log = 0.0

    while time.time() < deadline:
        t = req("GET", f"/api/tasks/{task_id}")
        task = t.get("task") or t
        status = task.get("status", "")
        if status != last_status:
            log(f"  任务状态 → {status}")
            if status == "DISPATCHING":
                check_sandboxes()
            last_status = status

        now = time.time()
        if now - last_log > 20:
            plan = task.get("plan") or {}
            n_st = len((plan.get("subtasks") or [])) if isinstance(plan, dict) else 0
            log(f"  … status={status} subtasks={n_st} diff={len(task.get('merged_diff') or '')}")
            last_log = now

        if status in ("DELIVERING", "CONFIRMING"):
            log("▶ 到达审核节点，自动 approve…")
            req("POST", f"/api/tasks/{task_id}/approve")
            time.sleep(2)
            continue

        if status == "DONE":
            diff = task.get("merged_diff") or ""
            log(f"✅ 任务完成 merged_diff={len(diff)} chars complexity={task.get('complexity')}")
            if diff:
                log("--- diff head ---")
                log(diff[:1200])
            verify_apply_diff(task_id, diff)
            return task

        if status == "FAILED":
            raise RuntimeError(f"task failed: {json.dumps(task, ensure_ascii=False)[:800]}")

        time.sleep(4)

    raise TimeoutError(f"task timeout last_status={last_status} task_id={task_id}")


def verify_apply_diff(task_id: str, diff: str) -> None:
    """冒烟 apply-diff API；sandbox pull-back 已写回时 check_only 可能 422，记为预期。"""
    if not diff.strip():
        log("▶ apply-diff 跳过（无 merged_diff）")
        return
    log("▶ apply-diff API 校验 (check_only)…")
    try:
        out = req("POST", f"/api/tasks/{task_id}/apply-diff", {"check_only": True})
        log(f"  ✅ {out.get('message', out)}")
    except RuntimeError as exc:
        msg = str(exc)
        if "422" in msg or "HTTP 422" in msg:
            log("  ⚠ check_only 未通过（变更可能已由 sandbox pull-back 写回工作区，属预期）")
        else:
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description="python-dotenv E2E flow")
    parser.add_argument("--force-preprocess", action="store_true")
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--skip-preprocess", action="store_true")
    parser.add_argument("--skip-retrieve", action="store_true")
    args = parser.parse_args()

    log("=== Swarm E2E 主流程 ===")
    health = req("GET", "/api/health")
    log(f"API ok: {health.get('status')}")

    if not args.skip_preprocess and not args.task_id:
        poll_preprocess(force=args.force_preprocess)
    if not args.skip_retrieve and not args.task_id:
        experiment_retrieve()

    task = run_task(task_id=args.task_id)
    diff = task.get("merged_diff") or ""
    if not _diff_has_plus_lines(diff):
        log("⚠ 任务 DONE 但 merged_diff 无有效代码变更")
        return 1
    log("=== E2E 主流程通过 ===")
    return 0


def _diff_has_plus_lines(diff: str) -> bool:
    return any(
        line.startswith("+") and not line.startswith("+++")
        for line in diff.splitlines()
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        log(f"❌ E2E 失败: {exc}")
        sys.exit(1)
