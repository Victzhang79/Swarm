#!/usr/bin/env python3
"""OFFLINE cassette 重放器 —— 把抽出的 TaskPlan 喂回【确定性】plan→scaffold 流水线复现崩溃。

这是复现工具：它必须让 live 流水线【怎么崩的就怎么崩】。因此它直接调用底层确定性函数
（不经 plan_finisher.finish_plan_deterministic 那层 fail-open 包裹——那层会吞掉异常、
掩盖崩溃），忠实复刻 brain 图里 `plan`→`elaborate` 两个节点跑这些 pass 的真实顺序：

  真实调用点（file:line）：
    1) normalize_plan_scopes        —— plan 节点 T3（brain/nodes/__init__.py:2201）
    2) inject_build_scaffold_subtasks—— plan 节点末端 finish_plan_deterministic
                                        （brain/nodes/__init__.py:2290 → plan_finisher.py:329）
    3) resolve_plan_conflicts       —— elaborate 节点（brain/planning_nodes.py:2346）
                                        内部固定顺序 dedupe→fix_dep→normalize→bump
  图边：plan → elaborate → validate_plan（brain/graph.py:515-516）

打印：注入的脚手架清单、结果依赖 DAG（subtask id → depends_on）、任何异常的完整 traceback。

★零 LLM、零沙箱、零云端★。用法：
    python scripts/cassette_replay.py cassettes/<task_id>.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent
_REPO_PARENT = _PKG_ROOT.parent
if str(_REPO_PARENT) not in sys.path:
    sys.path.insert(0, str(_REPO_PARENT))


@dataclass
class ReplayResult:
    plan: Any = None                      # 重放后的 TaskPlan（成功时）
    scaffolds: list[dict] = field(default_factory=list)   # inject 返回的机读清单
    resolve_counts: dict = field(default_factory=dict)    # resolve_plan_conflicts 计数
    dag: dict[str, list[str]] = field(default_factory=dict)  # subtask id → depends_on
    stripped_scaffolds: int = 0           # 重放前剥掉的【已注入】脚手架数（还原 pre-scaffold 态）
    decoupled: int = 0                    # decouple pass 剥离的假依赖数
    failed_stage: str | None = None       # 崩在哪一 pass（None=全过）
    error: BaseException | None = None
    traceback_str: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None


def _strip_injected_scaffolds(plan) -> int:
    """还原 pre-scaffold 态：剥掉【已注入】的 st-scaffold-* 子任务 + 一切指向它们的 depends_on /
    parallel_groups 引用。

    为何必须剥：cassette 常抽自 DISPATCHING（plan 节点已跑完 inject+decouple），plan 里已带
    脚手架。inject_build_scaffold_subtasks 幂等（sid 已存在即跳过），若不剥，replay 再跑 inject
    是 no-op、decouple 也无边可剥 → 复现不出「inject 造边 → decouple 删边」这条 round62 死链。
    剥回功能子任务后重跑 inject，才忠实复刻脚手架从无到有再经 decouple 的真实序。返回剥掉的数量。
    """
    subs = getattr(plan, "subtasks", None) or []
    scaf_ids = {st.id for st in subs if str(st.id).startswith("st-scaffold-")}
    if not scaf_ids:
        return 0
    plan.subtasks = [st for st in subs if st.id not in scaf_ids]
    for st in plan.subtasks:
        deps = [d for d in (getattr(st, "depends_on", None) or []) if d not in scaf_ids]
        st.depends_on = deps
    pg = getattr(plan, "parallel_groups", None)
    if pg:
        plan.parallel_groups = [[x for x in g if x not in scaf_ids] for g in pg]
        plan.parallel_groups = [g for g in plan.parallel_groups if g]
    return len(scaf_ids)


def _build_plan(cassette: dict):
    """从 cassette 还原 TaskPlan —— plan 是完整 model_dump，直接 model_validate。"""
    from swarm.types import TaskPlan

    plan_raw = cassette.get("plan") or {}
    if not plan_raw.get("subtasks"):
        raise ValueError("cassette.plan 无 subtasks——无从重放（先跑 cassette_extract）")
    return TaskPlan.model_validate(plan_raw)


def replay_cassette(cassette: dict, *, verbose: bool = False) -> ReplayResult:
    """把 cassette 喂回确定性流水线，忠实复刻 plan→elaborate 的真实调用顺序。

    绝不吞异常：捕获仅为记录崩在哪一 pass + 完整 traceback，随后原样保存在 result.error。
    """
    from swarm.brain.contract_utils import (
        inject_build_scaffold_subtasks,
        normalize_plan_scopes,
        resolve_plan_conflicts,
    )
    from swarm.brain.planning_nodes import _decouple_independent_subtasks

    res = ReplayResult()
    project_path = cassette.get("project_path")
    base_ref = cassette.get("base_commit")
    file_plan = cassette.get("file_plan") or None

    if project_path and not os.path.isdir(project_path):
        print(f"[replay] 注意：project_path 不在本机磁盘（{project_path}）——"
              "aggregate-vs-新建/pom 模板等磁盘取证会退化为 greenfield 行为，"
              "依赖读磁盘的崩溃可能复现不出。", file=sys.stderr)

    def _emit(stage: str) -> None:
        if verbose:
            print(f"[replay] → {stage}")

    try:
        # 0) 还原计划：从 model_dump 重建 TaskPlan。放在 try 内——恶意/漂移 cassette（schema
        # 变更、pydantic ValidationError、缺 subtasks）也要落成结构化 ReplayResult 而非裸抛，
        # 守住"任何崩溃入 result.error"契约（对抗复核 #4）。
        _emit("_build_plan (还原 TaskPlan)")
        res.failed_stage = "_build_plan"
        plan = _build_plan(cassette)
        res.plan = plan

        # cassette 抽自 DISPATCHING → plan 已带注入的脚手架。剥回 pre-scaffold 态，让下面重跑
        # inject 忠实复刻「造边」、decouple 复刻「删边」（否则 inject 幂等跳过 → 复现不出死链）。
        res.failed_stage = "_strip_injected_scaffolds"
        res.stripped_scaffolds = _strip_injected_scaffolds(plan)
        if res.stripped_scaffolds and verbose:
            print(f"[replay] 剥掉 {res.stripped_scaffolds} 个已注入脚手架，还原 pre-scaffold 态")

        # 1) plan 节点 T3：scope 归一（同文件写权唯一 + 降级者依赖首写者）
        _emit("normalize_plan_scopes (plan 节点 T3)")
        res.failed_stage = "normalize_plan_scopes"
        normalize_plan_scopes(plan, project_path=project_path, base_ref=base_ref)

        # 2) plan 节点末端：脚手架注入（直接调，绕开 finish_plan_deterministic 的 fail-open 包裹）
        _emit("inject_build_scaffold_subtasks (plan 节点末端)")
        res.failed_stage = "inject_build_scaffold_subtasks"
        res.scaffolds = inject_build_scaffold_subtasks(plan, project_path, file_plan)

        # 3) elaborate 节点【首个 pass】：剥离假 depends_on（提升并行度）。★round62 死因就在这★——
        # 它曾把 inject 造的 module 脚手架→聚合父边当"假依赖"剥了。必须在 replay 里如实跑，否则
        # 谁删的边这条链就断在工具外、永远抓不到。真实序：decouple 在 resolve 之前（planning_nodes.py:2318）。
        _emit("_decouple_independent_subtasks (elaborate 首 pass)")
        res.failed_stage = "_decouple_independent_subtasks"
        res.decoupled = _decouple_independent_subtasks(plan)

        # 4) elaborate 节点：冲突解决唯一事实源（dedupe→fix_dep→normalize→bump）
        _emit("resolve_plan_conflicts (elaborate 节点)")
        res.failed_stage = "resolve_plan_conflicts"
        res.resolve_counts = resolve_plan_conflicts(
            plan, project_path=project_path, base_ref=base_ref)

        res.failed_stage = None
    except BaseException as exc:  # noqa: BLE001 — 复现工具：任何崩溃都要如实呈现，不吞
        res.error = exc
        res.traceback_str = traceback.format_exc()

    # DAG 快照（无论成功/崩溃，能取多少取多少）
    try:
        res.dag = {st.id: list(getattr(st, "depends_on", []) or [])
                   for st in getattr(res.plan, "subtasks", []) or []}
    except Exception as exc:  # noqa: BLE001
        # 复现工具"绝不吞异常"铁律：快照失败本身有诊断价值（某 pass 崩后子任务半残），
        # 落 stderr 留痕再退化空 dict，不静默（对抗复核 #3）。
        print(f"[replay] DAG 快照构建失败（子任务可能被崩溃的 pass 留成半残态）: {exc}",
              file=sys.stderr)
    return res


def _print_report(cassette: dict, res: ReplayResult) -> None:
    line = "=" * 78
    print(line)
    print(f"cassette 重放报告  task={cassette.get('task_id')}  "
          f"subtasks(入)={len((cassette.get('plan') or {}).get('subtasks') or [])}")
    print(line)

    print(f"\n注入脚手架 ({len(res.scaffolds)}):")
    for e in res.scaffolds:
        print(f"  · module={e.get('module')}  subtask={e.get('subtask_id')}  "
              f"pom_exists={e.get('pom_exists')}  artifacts={e.get('artifacts')}")
    if not res.scaffolds:
        print("  (无)")

    if res.stripped_scaffolds:
        print(f"\n还原 pre-scaffold：剥掉 {res.stripped_scaffolds} 个已注入脚手架后重跑 inject")
    print(f"\ndecouple 剥离假依赖: {res.decoupled} 条"
          + ("  ⚠️ 若含 st-scaffold-* 目标边=round62 死因复活" if res.decoupled else ""))
    if res.resolve_counts:
        print(f"resolve_plan_conflicts 计数: {res.resolve_counts}")

    print(f"\n依赖 DAG (subtask → depends_on)  共 {len(res.dag)} 个子任务:")
    for sid, deps in res.dag.items():
        print(f"  {sid} → {deps}")

    if res.error is not None:
        print(f"\n✗ 崩溃于 pass: {res.failed_stage}")
        print("-" * 78)
        print(res.traceback_str.rstrip())
        print("-" * 78)
    else:
        print("\n✓ 确定性流水线全程无异常（normalize → inject → resolve 全过）")


def main() -> int:
    ap = argparse.ArgumentParser(description="重放 plan cassette，复现确定性流水线崩溃（零 LLM）")
    ap.add_argument("cassette", help="cassette JSON 路径")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每一 pass 进度")
    args = ap.parse_args()

    path = Path(args.cassette)
    if not path.is_file():
        print(f"✗ cassette 不存在: {path}", file=sys.stderr)
        return 2
    cassette = json.loads(path.read_text(encoding="utf-8"))

    res = replay_cassette(cassette, verbose=args.verbose)
    _print_report(cassette, res)
    return 1 if res.error is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
