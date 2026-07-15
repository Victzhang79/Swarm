#!/usr/bin/env python3
"""OFFLINE cassette 抽取器 —— 从 live LangGraph checkpoint 抽出 TaskPlan 快照。

痛点（记忆 round53~61）：swarm 近 60+ 轮 live E2E 全烧在同一条【确定性】plan→scaffold
流水线上（brain/contract_utils.py 的 inject_build_scaffold_subtasks / _inject_aggregator_
scaffold，与 resolve_plan_conflicts / normalize_plan_scopes）——触发器只是 brain LLM 产出的
一份 TaskPlan。只要把那份 plan 从 checkpoint 抽出来，整条确定性流水线就能【离线、零成本】重放。

本脚本【纯读】某 task 的 checkpoint state（aget_state，不推进图），把复现所需的最小切片
落盘成 cassette JSON：
  - plan          : 最终 TaskPlan.model_dump()（子任务/依赖/scope/契约全在内）
  - shared_contract
  - file_plan     : state["tech_design_file_plan"]（模块→文件权威归属，喂 inject）
  - base_commit   : 钉扎 base ref（normalize/resolve 的 aggregate-vs-新建撞车判定用）
  - project_path  : 项目基线磁盘路径（若 state/project store 里有；离线机上可能不存在）
  - module_dirs   : 若 state 里已解析出物理模块目录则一并存

★绝不发起任何 LLM 调用★——只连 PG checkpointer 读快照。checkpoint 尚未到 PLAN（无 plan）
时【大声失败】并给出清晰原因，不静默产出半截 cassette。

用法（在 swarm/ 包目录、激活 .venv 后）：
    python scripts/cassette_extract.py <task_id> [--out cassettes/<task_id>.json] [--thread <thread_id>]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent            # /Users/.../LLM/swarm/swarm  (the `swarm` package)
_REPO_PARENT = _PKG_ROOT.parent     # 使 `import swarm` 可用（editable 未装时兜底）
if str(_REPO_PARENT) not in sys.path:
    sys.path.insert(0, str(_REPO_PARENT))


class CassetteExtractError(RuntimeError):
    """抽取失败（无快照 / 未到 PLAN / 无 plan）——大声失败，绝不静默产半截 cassette。"""


def _plan_to_jsonable(plan: Any) -> dict:
    """把 checkpoint 里反序列化出的 TaskPlan（或已是 dict 的老快照）转成可 JSON 化 dict。"""
    if plan is None:
        return {}
    if hasattr(plan, "model_dump"):
        return plan.model_dump(mode="json")
    if isinstance(plan, dict):
        return plan
    raise CassetteExtractError(
        f"state['plan'] 类型异常（既非 TaskPlan 也非 dict）: {type(plan)!r}")


async def _extract(task_id: str, thread_id: str | None) -> dict:
    # 依赖 API 进程外的独立 PG checkpointer 初始化——纯读，不碰运行中的任务。
    from swarm.brain.graph import init_postgres_checkpointer
    from swarm.brain.runner import _load_state_snapshot  # 复用 runner 的只读快照加载
    from swarm.project import store as project_store

    ok = await init_postgres_checkpointer()
    if not ok:
        # REQUIRE_PG_CHECKPOINTER=1 时 checkpoint 只在 PG——降级到 MemorySaver 读不到任何东西。
        raise CassetteExtractError(
            "PG checkpointer 初始化失败——无法读取 live checkpoint（检查 "
            "SWARM_DB_POSTGRES_URI / PG 是否可达）。")

    state = await _load_state_snapshot(task_id, thread_id=thread_id)
    if not state:
        raise CassetteExtractError(
            f"task {task_id} 无 checkpoint 快照（thread_id={thread_id or 'auto'}）——"
            "任务不存在、或 checkpoint 尚未落任何 state。")

    plan = state.get("plan")
    if plan is None:
        raise CassetteExtractError(
            f"task {task_id} 的 checkpoint 里还没有 plan——任务尚未跑到 PLAN 节点"
            f"（当前 state 键: {sorted(state.keys())[:20]}）。无 plan 无从重放，不产 cassette。")

    plan_dump = _plan_to_jsonable(plan)
    if not plan_dump.get("subtasks"):
        raise CassetteExtractError(
            f"task {task_id} 的 plan 无 subtasks（空壳）——不产 cassette。")

    # project_path：优先 state，其次 project store（离线机上路径可能不存在，仅存不校验）。
    project_id = str(state.get("project_id") or "")
    project_path = state.get("project_path")
    if not project_path and project_id:
        try:
            proj = project_store.get_project(project_id) or {}
            project_path = proj.get("path")
        except Exception as exc:  # noqa: BLE001 — 取不到路径不阻断抽取（重放可传 None 降级）
            print(f"[extract] 取 project_path 失败（重放将传 None 降级）: {exc}", file=sys.stderr)

    cassette: dict = {
        "schema": "swarm-plan-cassette/v1",
        "task_id": task_id,
        "thread_id": thread_id or "",
        "project_id": project_id,
        "project_path": project_path,
        "base_commit": state.get("base_commit"),
        "plan": plan_dump,
        "shared_contract": state.get("shared_contract")
        or plan_dump.get("shared_contract") or {},
        "file_plan": state.get("tech_design_file_plan") or [],
        "module_dirs": state.get("module_dirs") or state.get("module_physical_dirs") or {},
        "task_description": (state.get("task_description") or state.get("description") or "")[:2000],
    }
    return cassette


def main() -> int:
    ap = argparse.ArgumentParser(description="从 live checkpoint 抽 TaskPlan cassette（纯读，零 LLM）")
    ap.add_argument("task_id", help="要抽取的任务 ID")
    ap.add_argument("--out", default=None, help="输出路径（默认 cassettes/<task_id>.json）")
    ap.add_argument("--thread", default=None, help="指定历史 thread_id（默认取 task 记录里的）")
    args = ap.parse_args()

    out_path = Path(args.out) if args.out else (_PKG_ROOT / "cassettes" / f"{args.task_id}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        cassette = asyncio.run(_extract(args.task_id, args.thread))
    except CassetteExtractError as exc:
        print(f"\n✗ 抽取失败: {exc}\n", file=sys.stderr)
        return 2

    out_path.write_text(json.dumps(cassette, ensure_ascii=False, indent=2), encoding="utf-8")
    n_sub = len(cassette["plan"].get("subtasks") or [])
    print(f"✓ cassette 已落盘: {out_path}")
    print(f"  task={cassette['task_id']}  subtasks={n_sub}  "
          f"file_plan={len(cassette['file_plan'])}  base={cassette.get('base_commit')}")
    print(f"  重放: python scripts/cassette_replay.py {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
