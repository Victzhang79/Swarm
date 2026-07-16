#!/usr/bin/env python3
"""T3（round63 死锁治本）—— 基线模块破坏死锁：结构性判定 + 确定性修复臂 + fail-loud 终局。

取证（ROUND63_POSTMORTEM_TREATMENT_REGISTER.md §T3 调查结论）：
  3 个子任务 12 次上报 `upstream_module_broken|blocked_on_modules=['ruoyi-common']`，
  MONITOR 完成数 13 冻结跨 3 周期；LLM 三轮诊断"预置模块、不在任何子任务范围内"（=无人能修）
  却仍 retry。现有三层全空过：worker 一切 BLOCKED 统标 transient；brain 早段拦截三臂
  （dep_hit/futile/C9）对"基线模块破坏"全不命中；B2 批级判据被混批搭车者（超时受害者）拆台。

治本：
  ①判据用结构不用状态：blocked 模块存在于 git HEAD 基线且 plan 无生产者 → 基线破坏（逐 fid 判，
    免疫混批拆台）；②修复臂：sweep_baseline_anchor_poison 对照 HEAD 还原共享清单既有版本锚篡改
    （复用 T2 纯函数，加法放行），还原>0 重派（输入真变了）、轮次封顶；③修不动/轮次耗尽 →
    并入 _unrecoverable 连坐放弃（诚实 PARTIAL），绝不 transient 无望等待。
"""
from __future__ import annotations

import asyncio
import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import swarm.brain.nodes as nodes  # noqa: E402
from swarm.brain.nodes.recovery import (  # noqa: E402
    _module_in_git_baseline,
    sweep_baseline_anchor_poison,
)
from swarm.types import (  # noqa: E402
    Confidence,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
    WorkerOutput,
)

# ── fixtures：RuoYi 形根 pom（基线 4.0.6）与投毒态（3.5.16）──

_BASE_ROOT_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project>
    <groupId>com.ruoyi</groupId>
    <artifactId>ruoyi</artifactId>
    <version>4.0.6</version>
    <packaging>pom</packaging>
    <properties>
        <ruoyi.version>4.0.6</ruoyi.version>
        <spring-boot.version>4.0.6</spring-boot.version>
    </properties>
    <modules>
        <module>ruoyi-common</module>
        <module>ruoyi-framework</module>
    </modules>
</project>
"""

_BASE_CHILD_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project>
    <parent>
        <groupId>com.ruoyi</groupId>
        <artifactId>ruoyi</artifactId>
        <version>4.0.6</version>
    </parent>
    <artifactId>{art}</artifactId>
</project>
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, timeout=30)


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    (repo / "ruoyi-common").mkdir(parents=True)
    (repo / "ruoyi-framework").mkdir(parents=True)
    (repo / "pom.xml").write_text(_BASE_ROOT_POM, encoding="utf-8")
    (repo / "ruoyi-common" / "pom.xml").write_text(
        _BASE_CHILD_POM.format(art="ruoyi-common"), encoding="utf-8")
    (repo / "ruoyi-framework" / "pom.xml").write_text(
        _BASE_CHILD_POM.format(art="ruoyi-framework"), encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    return repo


def _poison(repo: Path) -> None:
    """round63 实锤毒型：共享版本锚 4.0.6→3.5.16（根 pom 属性 + framework parent 版本）。"""
    root = repo / "pom.xml"
    root.write_text(
        root.read_text(encoding="utf-8").replace(
            "<spring-boot.version>4.0.6</spring-boot.version>",
            "<spring-boot.version>3.5.16</spring-boot.version>"),
        encoding="utf-8")
    fw = repo / "ruoyi-framework" / "pom.xml"
    fw.write_text(
        fw.read_text(encoding="utf-8").replace(
            "<version>4.0.6</version>", "<version>3.5.16</version>"),
        encoding="utf-8")


def _st(sid, writable=None, create=None):
    return SubTask(id=sid, description=f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=writable or [], create_files=create or []))


def _ok(sid):
    return WorkerOutput(subtask_id=sid, diff="d", summary="",
                        confidence=Confidence.HIGH, l1_passed=True)


def _blocked(sid, modules):
    return WorkerOutput(
        subtask_id=sid, diff="", summary="上游模块编译破坏判 BLOCKED",
        confidence=Confidence.LOW, l1_passed=False,
        l1_details={
            "pipeline_blocked": "upstream_module_broken",
            "blocked_on_modules": list(modules),
            "failure_class": "transient",
        },
    )


class _FakeResp:
    def __init__(self, content):
        self.content = content


def _fake_llm_retry():
    class _L:
        async def ainvoke(self, _msgs):
            return _FakeResp('{"strategy":"retry","reasoning":"r"}')
    return lambda: _L()


# ══ ① 结构性判据：模块是否存在于 git HEAD 基线 ══

def test_module_in_git_baseline_true_for_committed_module(tmp_path):
    repo = _mk_repo(tmp_path)
    assert _module_in_git_baseline(str(repo), "ruoyi-common") is True


def test_module_in_git_baseline_false_for_plan_created_module(tmp_path):
    repo = _mk_repo(tmp_path)
    # 计划新建的模块：目录在工作树但不在 HEAD → 非基线模块（有 plan 内 owner，别拦截）
    (repo / "ruoyi-alarm").mkdir()
    (repo / "ruoyi-alarm" / "pom.xml").write_text("<project/>", encoding="utf-8")
    assert _module_in_git_baseline(str(repo), "ruoyi-alarm") is False


def test_module_in_git_baseline_failopen_no_git(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _module_in_git_baseline(str(plain), "ruoyi-common") is False
    assert _module_in_git_baseline(None, "ruoyi-common") is False
    assert _module_in_git_baseline(str(plain), "") is False


# ══ ② 修复臂：sweep_baseline_anchor_poison ══

def test_sweep_restores_poisoned_anchors(tmp_path):
    repo = _mk_repo(tmp_path)
    _poison(repo)
    plan = TaskPlan(subtasks=[_st("st-1", writable=["ruoyi-alarm/src/A.java"])],
                    parallel_groups=[["st-1"]])
    restored, scan_errors = sweep_baseline_anchor_poison(str(repo), plan)
    assert scan_errors == 0
    files = {r["file"] for r in restored}
    assert "pom.xml" in files and "ruoyi-framework/pom.xml" in files, (
        f"两处锚投毒都必须还原，实得 {restored}")
    assert "<spring-boot.version>4.0.6</spring-boot.version>" in (
        repo / "pom.xml").read_text(encoding="utf-8")
    assert "<version>4.0.6</version>" in (
        repo / "ruoyi-framework" / "pom.xml").read_text(encoding="utf-8")


def test_sweep_skips_plan_owned_manifest(tmp_path):
    repo = _mk_repo(tmp_path)
    _poison(repo)
    # 根 pom 在某子任务 writable 内=计划授权编辑面 → 豁免；framework pom 无主 → 仍还原
    plan = TaskPlan(subtasks=[_st("st-1", writable=["pom.xml"])],
                    parallel_groups=[["st-1"]])
    restored, _ = sweep_baseline_anchor_poison(str(repo), plan)
    files = {r["file"] for r in restored}
    assert "pom.xml" not in files, "plan 授权面绝不还原（T2 HIGH#1 同款豁免）"
    assert "ruoyi-framework/pom.xml" in files
    assert "<spring-boot.version>3.5.16</spring-boot.version>" in (
        repo / "pom.xml").read_text(encoding="utf-8"), "授权面的改动必须保留"


def test_sweep_clean_tree_returns_empty(tmp_path):
    repo = _mk_repo(tmp_path)
    plan = TaskPlan(subtasks=[_st("st-1")], parallel_groups=[["st-1"]])
    assert sweep_baseline_anchor_poison(str(repo), plan) == ([], 0)


def test_sweep_pure_additions_untouched(tmp_path):
    repo = _mk_repo(tmp_path)
    root = repo / "pom.xml"
    txt = root.read_text(encoding="utf-8")
    txt = txt.replace("</properties>",
                      "    <alarm.version>1.0.0</alarm.version>\n    </properties>")
    txt = txt.replace("</modules>",
                      "    <module>ruoyi-alarm</module>\n    </modules>")
    root.write_text(txt, encoding="utf-8")
    plan = TaskPlan(subtasks=[_st("st-1")], parallel_groups=[["st-1"]])
    assert sweep_baseline_anchor_poison(str(repo), plan) == ([], 0), (
        "纯加法（新属性/新模块注册）绝不还原——并行兄弟的合法注册不能被冲掉")
    assert "<module>ruoyi-alarm</module>" in root.read_text(encoding="utf-8")


def test_sweep_failopen_no_git(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "pom.xml").write_text(_BASE_ROOT_POM, encoding="utf-8")
    plan = TaskPlan(subtasks=[_st("st-1")], parallel_groups=[["st-1"]])
    restored, _ = sweep_baseline_anchor_poison(str(plain), plan)
    assert restored == []


# ══ ③ handle_failure 集成：基线破坏死锁判决 ══

def _deadlock_state(plan, results, failed, **extra):
    state = {
        "plan": plan,
        "project_id": "p1",
        "failed_subtask_ids": list(failed),
        "subtask_results": dict(results),
        "subtask_retry_counts": {},
        "dispatch_remaining": [],
        "degraded_reasons": [],
    }
    state.update(extra)
    return state


def _run_hf(state, repo):
    with patch.object(nodes, "_get_brain_llm", _fake_llm_retry()), \
         patch.object(nodes, "_get_project_path", lambda _pid: str(repo)):
        return asyncio.run(nodes.handle_failure(state))


def test_baseline_blocked_triggers_repair_and_redispatch(tmp_path):
    """round63 主死锁形态：blocked on 基线模块 + 树被锚投毒 → 修复臂还原 + 重派（非 transient 白跑）。"""
    repo = _mk_repo(tmp_path)
    _poison(repo)
    plan = TaskPlan(
        subtasks=[_st("st-ok", writable=["ruoyi-alarm/src/B.java"]),
                  _st("st-1", writable=["ruoyi-alarm/src/A.java"])],
        parallel_groups=[["st-ok", "st-1"]])
    out = _run_hf(_deadlock_state(
        plan, {"st-ok": _ok("st-ok"), "st-1": _blocked("st-1", ["ruoyi-common"])},
        ["st-1"]), repo)
    assert out.get("failure_strategy") == "retry", f"修复臂后应重派，实得 {out.get('failure_strategy')}"
    assert out.get("baseline_repair_rounds") == 1, "修复轮次必须计数（防修了又被投毒的无界循环）"
    assert "st-1" in out.get("dispatch_remaining", [])
    assert out.get("subtask_retry_counts", {}).get("st-1") == 0, (
        "对着毒树的既往重试是徒劳，不计 capability 配额")
    assert "<spring-boot.version>4.0.6</spring-boot.version>" in (
        repo / "pom.xml").read_text(encoding="utf-8"), "毒必须真出树"
    assert not out.get("abandoned_subtask_ids"), "修得动就不放弃"


def test_baseline_blocked_clean_tree_abandons_failloud(tmp_path):
    """扫描无可还原（破坏非锚投毒）→ 判死锁 fail-loud 连坐放弃，绝不 transient 无望等待。"""
    repo = _mk_repo(tmp_path)  # 树干净：锚与 HEAD 全等
    plan = TaskPlan(
        subtasks=[_st("st-ok", writable=["ruoyi-alarm/src/B.java"]),
                  _st("st-1", writable=["ruoyi-alarm/src/A.java"])],
        parallel_groups=[["st-ok", "st-1"]])
    out = _run_hf(_deadlock_state(
        plan, {"st-ok": _ok("st-ok"), "st-1": _blocked("st-1", ["ruoyi-common"])},
        ["st-1"]), repo)
    assert out.get("failure_strategy") == "abandon", (
        f"基线破坏且修复臂无从下手 → 诚实 PARTIAL，实得 {out.get('failure_strategy')}")
    assert "st-1" in out.get("abandoned_subtask_ids", [])
    assert "subtask_transient_counts" not in out, "绝不落 transient 退避（无望等待）"


def test_baseline_blocked_rounds_exhausted_abandons(tmp_path):
    """修复轮次耗尽仍 blocked → 不再扫描/写盘，直接死锁终局。"""
    repo = _mk_repo(tmp_path)
    _poison(repo)
    plan = TaskPlan(
        subtasks=[_st("st-ok", writable=["ruoyi-alarm/src/B.java"]),
                  _st("st-1", writable=["ruoyi-alarm/src/A.java"])],
        parallel_groups=[["st-ok", "st-1"]])
    out = _run_hf(_deadlock_state(
        plan, {"st-ok": _ok("st-ok"), "st-1": _blocked("st-1", ["ruoyi-common"])},
        ["st-1"], baseline_repair_rounds=2), repo)
    assert out.get("failure_strategy") == "abandon"
    assert "st-1" in out.get("abandoned_subtask_ids", [])
    assert "<spring-boot.version>3.5.16</spring-boot.version>" in (
        repo / "pom.xml").read_text(encoding="utf-8"), "轮次耗尽绝不再写盘"


def test_module_with_active_producer_not_intercepted(tmp_path):
    """blocked 模块有 active 生产者（plan 内 owner 会来修）→ 不判基线死锁，保住合法跨模块等待。"""
    repo = _mk_repo(tmp_path)
    _poison(repo)
    plan = TaskPlan(
        subtasks=[_st("st-p", writable=["ruoyi-common/src/C.java"]),
                  _st("st-1", writable=["ruoyi-alarm/src/A.java"])],
        parallel_groups=[["st-p", "st-1"]])
    out = _run_hf(_deadlock_state(
        plan, {"st-1": _blocked("st-1", ["ruoyi-common"])},
        ["st-1"], dispatch_remaining=["st-p"]), repo)
    assert "baseline_repair_rounds" not in out, "有生产者绝不触发基线死锁臂（交 C9/transient 既有语义）"
    assert "<spring-boot.version>3.5.16</spring-boot.version>" in (
        repo / "pom.xml").read_text(encoding="utf-8"), "未判死锁不扫树"
    assert not out.get("abandoned_subtask_ids")


def test_mixed_batch_still_intercepts(tmp_path):
    """round63 缺口3 击杀：混批（blocked+超时搭车者）绝不拆台——逐 fid 判定，修复臂照常触发。"""
    repo = _mk_repo(tmp_path)
    _poison(repo)
    plan = TaskPlan(
        subtasks=[_st("st-ok", writable=["ruoyi-alarm/src/B.java"]),
                  _st("st-1", writable=["ruoyi-alarm/src/A.java"]),
                  _st("st-2", writable=["ruoyi-alarm/src/D.java"])],
        parallel_groups=[["st-ok", "st-1", "st-2"]])
    timeout_victim = WorkerOutput(
        subtask_id="st-2", diff="", summary="timeout_in_coding",
        confidence=Confidence.LOW, l1_passed=False,
        l1_details={"error": "timeout_in_coding", "failure_class": "transient"})
    out = _run_hf(_deadlock_state(
        plan,
        {"st-ok": _ok("st-ok"), "st-1": _blocked("st-1", ["ruoyi-common"]),
         "st-2": timeout_victim},
        ["st-1", "st-2"]), repo)
    assert out.get("baseline_repair_rounds") == 1, (
        "混入非 blocked 搭车者绝不拆台（round63 三周期 16min×4 白跑的结构性缺口）")
    assert "<spring-boot.version>4.0.6</spring-boot.version>" in (
        repo / "pom.xml").read_text(encoding="utf-8")
    assert "st-1" in out.get("dispatch_remaining", []) and "st-2" in out.get(
        "dispatch_remaining", []), "搭车受害者随批重派（毒出树后其超时根因同消）"


def test_nonbaseline_module_keeps_old_behavior(tmp_path):
    """blocked 模块不在 HEAD（无生产者、非基线）→ 不触发新臂，落既有 transient 语义。"""
    repo = _mk_repo(tmp_path)
    plan = TaskPlan(
        subtasks=[_st("st-1", writable=["ruoyi-alarm/src/A.java"])],
        parallel_groups=[["st-1"]])
    out = _run_hf(_deadlock_state(
        plan, {"st-1": _blocked("st-1", ["ghost-mod"])}, ["st-1"]), repo)
    assert "baseline_repair_rounds" not in out
    assert out.get("failure_strategy") == "retry", "既有 transient 退避语义保持不变"


# ══ ④ 对抗复核整改回归锁 ══

def _flaky_git_show(real_run):
    """git show 抛异常、其余 git 子命令照常——模拟 scanner 半盲（hunter#1）。"""
    def _run(cmd, **kw):
        if isinstance(cmd, (list, tuple)) and "show" in cmd:
            raise subprocess.SubprocessError("simulated git show failure")
        return real_run(cmd, **kw)
    return _run


def test_sweep_scan_blind_reports_errors(tmp_path):
    """hunter#1（HIGH）：扫瞎必须可区分于扫净——scan_errors>0，绝不伪装成"树干净"。"""
    import swarm.brain.nodes.recovery as recovery
    repo = _mk_repo(tmp_path)
    _poison(repo)
    plan = TaskPlan(subtasks=[_st("st-1", writable=["ruoyi-alarm/src/A.java"])],
                    parallel_groups=[["st-1"]])
    with patch.object(recovery.subprocess, "run",
                      side_effect=_flaky_git_show(subprocess.run)):
        restored, scan_errors = sweep_baseline_anchor_poison(str(repo), plan)
    assert restored == [] and scan_errors > 0, (
        f"扫描全盲时必须报 scan_errors，实得 restored={restored} errors={scan_errors}")


def test_scan_blind_not_misjudged_as_deadlock(tmp_path):
    """hunter#1（HIGH）：scanner 坏 ≠ 树干净——盲扫绝不判死锁放弃，回落既有阶梯且不耗修复轮次。"""
    import swarm.brain.nodes.recovery as recovery
    repo = _mk_repo(tmp_path)
    _poison(repo)
    plan = TaskPlan(
        subtasks=[_st("st-ok", writable=["ruoyi-alarm/src/B.java"]),
                  _st("st-1", writable=["ruoyi-alarm/src/A.java"])],
        parallel_groups=[["st-ok", "st-1"]])
    state = _deadlock_state(
        plan, {"st-ok": _ok("st-ok"), "st-1": _blocked("st-1", ["ruoyi-common"])},
        ["st-1"])
    with patch.object(recovery.subprocess, "run",
                      side_effect=_flaky_git_show(subprocess.run)):
        out = _run_hf(state, repo)
    assert not out.get("abandoned_subtask_ids"), (
        "盲扫（0 还原 + N 扫描失败）判死锁放弃=方向性错误（scanner 坏 ≠ 树干净）")
    assert "baseline_repair_rounds" not in out, "盲扫不消耗修复轮次"
    assert out.get("failure_strategy") == "retry", "回落既有 transient 阶梯（自有 B2/A2 封顶）"


def test_mixed_unrecoverable_verdict_preserved(tmp_path):
    """reviewer HIGH#1：同批已判真死上游者绝不搭修复臂便车白跑——同一 return 里照常连坐放弃。"""
    repo = _mk_repo(tmp_path)
    _poison(repo)
    st_a = SubTask(id="st-A", description="depends on abandoned upstream",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   depends_on=["st-dead"],
                   scope=FileScope(writable=["ruoyi-alarm/src/AA.java"]))
    plan = TaskPlan(
        subtasks=[_st("st-dead"), st_a,
                  _st("st-B", writable=["ruoyi-alarm/src/BB.java"]),
                  _st("st-ok", writable=["ruoyi-alarm/src/C.java"])],
        parallel_groups=[["st-dead", "st-A", "st-B", "st-ok"]])
    out = _run_hf(_deadlock_state(
        plan,
        {"st-ok": _ok("st-ok"),
         "st-A": _blocked("st-A", ["ghost-mod"]),
         "st-B": _blocked("st-B", ["ruoyi-common"])},
        ["st-A", "st-B"], abandoned_subtask_ids=["st-dead"]), repo)
    assert out.get("failure_strategy") == "retry", "修复臂对 st-B 照常生效"
    assert out.get("baseline_repair_rounds") == 1
    assert "st-A" in out.get("abandoned_subtask_ids", []), (
        "依赖已放弃上游的 st-A 必须在同一 return 连坐放弃，绝不重派白跑整周期")
    assert "st-A" not in out.get("dispatch_remaining", [])
    assert "st-B" in out.get("dispatch_remaining", [])


def test_straggler_retry_counter_increments(tmp_path):
    """hunter#5：混批搭车者按全文件惯例 +1 记账，绝不白拿免费重试；基线阻断者豁免归零。"""
    repo = _mk_repo(tmp_path)
    _poison(repo)
    plan = TaskPlan(
        subtasks=[_st("st-ok", writable=["ruoyi-alarm/src/B.java"]),
                  _st("st-1", writable=["ruoyi-alarm/src/A.java"]),
                  _st("st-2", writable=["ruoyi-alarm/src/D.java"])],
        parallel_groups=[["st-ok", "st-1", "st-2"]])
    timeout_victim = WorkerOutput(
        subtask_id="st-2", diff="", summary="timeout_in_coding",
        confidence=Confidence.LOW, l1_passed=False,
        l1_details={"error": "timeout_in_coding", "failure_class": "transient"})
    out = _run_hf(_deadlock_state(
        plan,
        {"st-ok": _ok("st-ok"), "st-1": _blocked("st-1", ["ruoyi-common"]),
         "st-2": timeout_victim},
        ["st-1", "st-2"], subtask_retry_counts={"st-2": 1}), repo)
    assert out.get("baseline_repair_rounds") == 1
    rc = out.get("subtask_retry_counts", {})
    assert rc.get("st-1") == 0, "基线阻断者：对毒树的既往重试徒劳，豁免归零"
    assert rc.get("st-2") == 2, "搭车者必须 +1 记账（否则无限白拿免费重试绕过阶梯）"


def test_module_in_git_baseline_git_error_failopen(tmp_path, caplog):
    """hunter#2：git 异常 fail-open False 但必须留 WARNING 痕（T3 臂静默解除武装可观测）。"""
    import logging as _logging
    import swarm.brain.nodes.recovery as recovery
    repo = _mk_repo(tmp_path)

    def _timeout_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 15)
    with caplog.at_level(_logging.WARNING, logger="swarm.brain.nodes.recovery"), \
         patch.object(recovery.subprocess, "run", side_effect=_timeout_run):
        assert _module_in_git_baseline(str(repo), "ruoyi-common") is False
    assert any("T3" in r.message and "fail-open" in r.message
               for r in caplog.records), "git 异常路径必须 WARNING 留痕"
