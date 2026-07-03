#!/usr/bin/env python3
"""#12 跨沙箱产物同步 → bootstrap fail-closed seed 闸门（round20 治本 Candidate B）回归测试。

治本背景（round19 实测）：生产者产物 pull-back 落了本地，但被放弃时 _local_tree_revert_subtask
硬删其足迹 + 传递级联误删下游有效产物 → 饿死下游 seed → 消费者 bootstrap 上传报"本地文件不存在"
→ 沙箱缺包 → `package does not exist` 空烧整条 locate/code/verify 预算才在 L1 判 BLOCKED。

B（安全、不碰红线 revert 层）：plan 在 readable 传播上游/兄弟 create_files 时【标 provenance】
(FileScope.upstream_artifacts)；worker bootstrap 后若这些产物缺失于本地树 → 上游未就绪/被 revert →
先判 BLOCKED（transient·等生产者）短路早返，不空烧。provenance 只标上游产物，基线只读上下文不入，
杜绝误 BLOCKED（fail-closed 但不误伤能跑的子任务）。

本套验证：① 纯函数 missing_seed_artifacts；② 纯函数 packages_from_missing_artifacts；
③ FileScope 字段默认+round-trip；④ plan 传播确标 provenance；⑤ 端到端方法：缺产物→BLOCKED、
齐→放行、无 provenance→放行、本地模式→放行。
"""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.types import FileScope, NotRunKind, SubTask  # noqa: E402
from swarm.worker.executor import (  # noqa: E402
    WorkerExecutor,
    WorkerPhase,
    missing_seed_artifacts,
    packages_from_missing_artifacts,
)

_JAVA = "modA/src/main/java/com/ruoyi/alarm/domain/RobotSender.java"


# ── ① 纯函数：缺失检测 ──

def test_missing_seed_artifacts_pure():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        (d / "a.txt").write_text("x")
        # a.txt 在、b.txt 缺、空串跳过、去重
        assert missing_seed_artifacts(["a.txt", "b.txt", "", "b.txt"], d) == ["b.txt"]
        assert missing_seed_artifacts([], d) == []
        assert missing_seed_artifacts(["a.txt"], d) == []
    print("  ✅ ① missing_seed_artifacts：缺失/在/空/去重")


# ── ② 纯函数：缺失源文件 → 内部包 ──

def test_packages_from_missing_artifacts_pure():
    pkgs = packages_from_missing_artifacts([_JAVA, "modA/pom.xml", "README.md"])
    assert pkgs == ["com.ruoyi.alarm.domain"], pkgs  # 仅源文件反推包，非源忽略
    assert packages_from_missing_artifacts([]) == []
    print("  ✅ ② packages_from_missing_artifacts：源文件→包，非源忽略")


# ── ③ FileScope 新字段：默认空 + 可设 + round-trip ──

def test_filescope_upstream_artifacts_field():
    assert FileScope().upstream_artifacts == []
    s = FileScope(readable=["x.java"], upstream_artifacts=["x.java"])
    s2 = FileScope.model_validate(s.model_dump())
    assert s2.upstream_artifacts == ["x.java"]
    print("  ✅ ③ FileScope.upstream_artifacts：默认空/可设/round-trip")


# ── ④ plan 传播：ELABORATE 按文件分批时，下游批 readable 里的上游产物入 upstream_artifacts ──

def test_plan_marks_provenance_on_propagation():
    from swarm.brain import planning_nodes as pn
    # 两实体全栈：domain/mapper 批 → service/impl 批（串行下游读上游产物）
    creates = [
        f"modA/src/main/java/com/x/{layer}/{ent}{suf}.java"
        for ent in ("Alarm", "Robot")
        for layer, suf in (("domain", ""), ("mapper", "Mapper"),
                           ("service", "Service"), ("service/impl", "ServiceImpl"))
    ]
    scope = FileScope(create_files=creates)
    st = SubTask(id="st-1", description="父任务：实体+服务", scope=scope)
    fn = getattr(pn, "_split_oversized_by_files", None)
    assert fn is not None
    children = fn(st)
    assert children and len(children) >= 2, "应按文件分层拆成多批"
    # 至少一个下游批的 upstream_artifacts 非空，且 ⊆ 其 readable、⊆ 上游批 create_files
    all_creates = set(creates)
    downstream_with_prov = [
        c for c in children if getattr(c.scope, "upstream_artifacts", [])
    ]
    assert downstream_with_prov, "下游批应带 provenance 标记"
    for c in downstream_with_prov:
        ua = set(c.scope.upstream_artifacts)
        assert ua <= set(c.scope.readable), "provenance ⊆ readable"
        assert ua <= all_creates, "provenance 只含上游 create_files 产物"
        # 不含本批自己的 create_files（那是自产非上游）
        assert not (ua & set(c.scope.create_files)), "不含本批自产文件"
    print("  ✅ ④ plan 传播标 provenance：⊆readable、⊆上游产物、不含自产")


# ── ⑤ 端到端方法（__new__ 轻构造）：缺→BLOCKED、齐→放行、无 provenance→放行、本地→放行 ──

def _mk_executor(sandbox, project_path, upstream_artifacts):
    ex = WorkerExecutor.__new__(WorkerExecutor)
    ex._sandbox = sandbox
    ex._sandbox_manager = None
    ex.project_path = str(project_path)
    ex.execution_log = []
    ex.start_time = 0
    ex.phase = WorkerPhase.PREPARING
    ex.subtask = SubTask(
        id="st-c", description="消费者",
        scope=FileScope(readable=list(upstream_artifacts),
                        upstream_artifacts=list(upstream_artifacts)),
    )
    return ex


def test_precheck_blocks_on_missing_upstream():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        ex = _mk_executor(sandbox=object(), project_path=d, upstream_artifacts=[_JAVA])
        out = ex._precheck_upstream_seed()
        assert out is not None, "缺上游产物应短路 BLOCKED"
        det = out.l1_details
        assert det["pipeline_blocked"] == "internal_pkg_not_built"
        assert det["not_run_kind"] == NotRunKind.BLOCKED.value
        assert det["failure_class"] == "transient"
        assert _JAVA in det["blocked_on_files"]
        assert "com.ruoyi.alarm.domain" in det["blocked_on_packages"]
        assert out.l1_passed is False
    print("  ✅ ⑤a 缺上游产物 → BLOCKED(transient·等生产者)，带 blocked_on 包/文件")


def test_precheck_passes_when_present():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        f = d / _JAVA
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("package com.ruoyi.alarm.domain; public class RobotSender {}")
        ex = _mk_executor(sandbox=object(), project_path=d, upstream_artifacts=[_JAVA])
        assert ex._precheck_upstream_seed() is None, "产物齐 → 放行"
    print("  ✅ ⑤b 上游产物齐 → 放行")


def test_precheck_passes_without_provenance():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        # 无 provenance（基线只读上下文缺失不算数）→ 放行，杜绝误 BLOCKED
        ex = _mk_executor(sandbox=object(), project_path=d, upstream_artifacts=[])
        ex.subtask.scope.readable = ["some/baseline/Ctx.java"]  # 缺但非 provenance
        assert ex._precheck_upstream_seed() is None
    print("  ✅ ⑤c 无 provenance（基线 readable 缺）→ 放行，不误 BLOCKED")


def test_precheck_passes_in_local_mode():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        ex = _mk_executor(sandbox=None, project_path=d, upstream_artifacts=[_JAVA])
        assert ex._precheck_upstream_seed() is None, "本地模式无 seed 环节 → 不适用"
    print("  ✅ ⑤d 本地模式（无沙箱）→ 放行")


if __name__ == "__main__":
    test_missing_seed_artifacts_pure()
    test_packages_from_missing_artifacts_pure()
    test_filescope_upstream_artifacts_field()
    test_plan_marks_provenance_on_propagation()
    test_precheck_blocks_on_missing_upstream()
    test_precheck_passes_when_present()
    test_precheck_passes_without_provenance()
    test_precheck_passes_in_local_mode()
    print("\n✅ 全部通过：#12 bootstrap fail-closed seed 闸门（Candidate B）")
