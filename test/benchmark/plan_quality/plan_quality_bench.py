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


def _check_invariants(plan: TaskPlan, invariants: list[str],
                      entry: dict | None = None) -> list[str]:
    """按 manifest 声明的不变量逐条核查，返回违反项(空=全过)。

    ★栈中立（B-0）★：不变量本身是【计划质量断言】，与技术栈无关——"根聚合清单的脚手架
    不能是 trivial"对 pom.xml / package.json / go.work 同样成立。故根清单名与源码后缀
    从夹具 entry 取（`root_manifest` / `source_exts`），缺省退回 Maven 口径（既有夹具零改动）。
    """
    entry = entry or {}
    root_manifest = entry.get("root_manifest") or "pom.xml"
    source_exts = tuple(entry.get("source_exts") or (".java",))
    violations: list[str] = []
    for inv in invariants:
        if inv == "no_trivial_scaffold":
            # R62-Task6 收窄：只有【写根 pom.xml】的脚手架（RUN19 多步本质=读庞大根 pom +
            # 定位 <modules> + 多模块登记）必须非 trivial；模块 pom 脚手架（<dir>/pom.xml，
            # 描述内嵌权威模板=单文件落盘）维持 trivial 轻量路径（与 bump_scaffold_difficulty
            # 同口径）。原不变量对所有脚手架一刀切=把纯机械模块 pom 写误送重多步路径。
            from swarm.brain.contract_utils import _norm_scope_path
            bad = []
            for s in plan.subtasks:
                if s.difficulty != SubTaskDifficulty.TRIVIAL:
                    continue
                sc = getattr(s, "scope", None)
                _w = (set(getattr(sc, "create_files", []) or [])
                      | set(getattr(sc, "writable", []) or []))
                if root_manifest in {_norm_scope_path(x) for x in _w}:
                    bad.append(s.id)
            if bad:
                violations.append(
                    f"no_trivial_scaffold: 仍有 trivial 根 {root_manifest} 脚手架 {bad}")
        elif inv == "no_trivial_multi_source":
            # R67-T9 的不变量表述（**不复刻实现判据**，纪律 6）：一次 create ≥3 个源码文件
            # 的子任务不能是 trivial——worker trivial 单发路径合并定位+编码、封顶 30 步，
            # 塞不下多文件 → 低估路由弱档 = 白烧后重派。此判据与语言无关，只与"要写几个
            # 源文件"有关；源码后缀由夹具声明（Maven .java / npm .ts / go .go）。
            bad = []
            for s in plan.subtasks:
                if s.difficulty != SubTaskDifficulty.TRIVIAL:
                    continue
                n = sum(1 for f in (getattr(getattr(s, "scope", None), "create_files", []) or [])
                        if str(f).endswith(source_exts))
                if n >= 3:
                    bad.append(f"{s.id}({n} 个源文件)")
            if bad:
                violations.append(f"no_trivial_multi_source: {bad}")
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
    # ★已知缺口（B-0 红灯先行）★：夹具写的是【正确期望】，当前实现还达不到 → 这里记原因。
    # CI 侧对它 xfail(strict=True)：既不假绿（缺口一直可见），修好后又会 XPASS 逼人来摘标记，
    # 不会变成"修完了没人知道"的僵尸标记。绝不允许把期望改成迁就现状——那才是真假绿。
    known_gap: str = ""


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
        cu._exists_in_repo = lambda pp, rel, cache, base_ref=None, _a=agg: rel in _a
    try:
        counts = cu.resolve_plan_conflicts(plan, project_path="/fixture/repo" if agg else None)
    finally:
        cu._exists_in_repo = orig

    after = validate_plan_structure(plan).valid
    violations = _check_invariants(plan, entry.get("invariants") or [], entry)

    res = FixtureResult(run=entry.get("run", "?"), file=entry["file"],
                        before_valid=before, after_valid=after,
                        resolve_counts=counts, violations=violations,
                        known_gap=entry.get("known_gap", "") or "")

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
        mark = "✅" if ok else ("🟡" if r.known_gap else "❌")
        lines.append(f"{mark} {r.run:6} {r.file}")
        if r.known_gap:
            lines.append(f"      🟡 已知缺口（红灯先行，非回归）: {r.known_gap}")
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
    # 已知缺口不计失败（它们有 xfail 守着，且缺口本身在跑分卡上以 🟡 常驻可见）
    failed = [r for r in results
              if not (r.expectations_met and not r.violations) and not r.known_gap]
    raise SystemExit(1 if failed else 0)
