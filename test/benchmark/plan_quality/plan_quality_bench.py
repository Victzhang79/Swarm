"""Plan-quality 离线评测基准（借鉴 multi-rag-agent 的"廉价评测兜底"文化）。

痛点:swarm 验证 planning 改动只能靠 $30/次 的 live E2E 跑，一轮 ~40min 才暴露一个确定性 plan
bug(RUN17 依赖倒置 / RUN18 pass 互撤 / RUN19 脚手架难度)。这些本该秒级离线测出。

本基准把【真实 E2E 失败/通过的 plan 快照】固化成夹具，重放 brain 的确定性冲突解决流水线
(resolve_plan_conflicts，与 _elaborate 共用同一函数)，再用 plan_validator 断言不变量满足。
每改 planning pass 先跑本基准，零 LLM、零沙箱、秒级，替代靠 live E2E 撞 bug。

用法:
    python test/benchmark/plan_quality/plan_quality_bench.py          # 跑全部夹具，打分卡
    python -m pytest test/test_plan_quality_bench.py                  # CI 回归(全夹具须过)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from swarm.brain.plan_validator import validate_plan_structure
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan

_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES_DIR = os.path.join(_HERE, "fixtures")
MANIFEST = os.path.join(_HERE, "manifest.json")


def _load_plan(path: str) -> TaskPlan:
    """从瘦身夹具 JSON 还原 TaskPlan(只取 resolver/validator 需要的字段)。"""
    raw = json.load(open(path, encoding="utf-8"))
    sts = []
    for s in raw.get("subtasks", []):
        sc = s.get("scope", {}) or {}
        try:
            diff = SubTaskDifficulty(s.get("difficulty", "medium"))
        except ValueError:
            diff = SubTaskDifficulty.MEDIUM
        sts.append(SubTask(
            id=s["id"], description=s.get("description", "x") or "x",
            difficulty=diff, modality=SubTaskModality.TEXT,
            scope=FileScope(
                create_files=sc.get("create_files") or [],
                writable=sc.get("writable") or [],
                readable=sc.get("readable") or [],
            ),
            depends_on=s.get("depends_on") or [],
            acceptance_criteria=s.get("acceptance_criteria") or ["ok"],
        ))
    return TaskPlan(subtasks=sts, parallel_groups=raw.get("parallel_groups") or [],
                    shared_contract=raw.get("shared_contract") or {})


def _check_invariants(plan: TaskPlan, invariants: list[str]) -> list[str]:
    """按 manifest 声明的不变量逐条核查，返回违反项(空=全过)。"""
    violations: list[str] = []
    for inv in invariants:
        if inv == "no_trivial_scaffold":
            # 解决后不应再有 trivial 脚手架(会走 worker 单发拒答陷阱)
            from swarm.brain.contract_utils import _is_scaffold_subtask
            bad = [s.id for s in plan.subtasks
                   if s.difficulty == SubTaskDifficulty.TRIVIAL and _is_scaffold_subtask(s)]
            if bad:
                violations.append(f"no_trivial_scaffold: 仍有 trivial 脚手架 {bad}")
        elif inv == "no_parallel_file_writers":
            # 任一文件的多个写者必须有依赖序(不能并发写)；用 validator 的同款判定兜底
            r = validate_plan_structure(plan)
            pom_like = [i for i in r.issues if "同时写" in i]
            if pom_like:
                violations.append(f"no_parallel_file_writers: {pom_like}")
    return violations


@dataclass
class FixtureResult:
    run: str
    file: str
    before_valid: bool
    after_valid: bool
    resolve_counts: dict
    violations: list[str] = field(default_factory=list)
    expectations_met: bool = True
    notes: list[str] = field(default_factory=list)


def run_fixture(entry: dict) -> FixtureResult:
    """重放一个夹具:加载 → 解决前校验 → resolve_plan_conflicts → 解决后校验 + 不变量。"""
    # 延迟 import,确保测的是当前代码
    from swarm.brain import contract_utils as cu

    path = os.path.join(FIXTURES_DIR, entry["file"])
    plan = _load_plan(path)

    before = validate_plan_structure(plan).valid

    # 聚合文件存在性 monkeypatch:夹具自带,不依赖真实 repo(可移植、CI 友好)。
    agg = set(entry.get("aggregate_files") or [])
    orig = cu._exists_in_repo
    if agg:
        cu._exists_in_repo = lambda pp, rel, cache, _a=agg: rel in _a
    try:
        counts = cu.resolve_plan_conflicts(plan, project_path="/fixture/repo" if agg else None)
    finally:
        cu._exists_in_repo = orig

    after = validate_plan_structure(plan).valid
    violations = _check_invariants(plan, entry.get("invariants") or [])

    res = FixtureResult(run=entry.get("run", "?"), file=entry["file"],
                        before_valid=before, after_valid=after,
                        resolve_counts=counts, violations=violations)

    # 期望核对
    if before != entry.get("expect_before_valid", before):
        res.expectations_met = False
        res.notes.append(f"before_valid 期望 {entry['expect_before_valid']} 实得 {before}")
    if after != entry.get("expect_after_valid", True):
        res.expectations_met = False
        res.notes.append(f"after_valid 期望 {entry['expect_after_valid']} 实得 {after}")
    if violations:
        res.expectations_met = False
    return res


def run_all() -> list[FixtureResult]:
    manifest = json.load(open(MANIFEST, encoding="utf-8"))
    return [run_fixture(e) for e in manifest["fixtures"]]


def _scorecard(results: list[FixtureResult]) -> str:
    lines = ["", "=" * 78, "Plan-Quality 离线评测基准", "=" * 78]
    passed = 0
    for r in results:
        ok = r.expectations_met and not r.violations
        passed += ok
        mark = "✅" if ok else "❌"
        lines.append(f"{mark} {r.run:6} {r.file}")
        lines.append(f"      valid: 解决前={r.before_valid} → 解决后={r.after_valid}  "
                     f"| resolve={r.resolve_counts}")
        for n in r.notes:
            lines.append(f"      ⚠ {n}")
        for v in r.violations:
            lines.append(f"      ✗ {v}")
    lines.append("-" * 78)
    lines.append(f"通过 {passed}/{len(results)} 夹具")
    lines.append("=" * 78)
    return "\n".join(lines)


if __name__ == "__main__":
    results = run_all()
    print(_scorecard(results))
    failed = [r for r in results if not (r.expectations_met and not r.violations)]
    raise SystemExit(1 if failed else 0)
