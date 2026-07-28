"""R67L-B2（22号文批次2）行为测试：prune↔验收矛盾 / H-3a scope 内生产者 / 死端 BLOCKED。

round67l 三路复盘定案的三条执行期闸矛盾：

1. **prune↔验收自相矛盾**（st-14 烧 4 轮实锤）：phantom-dep prune 把【plan 声明、生产者
   尚未 merge 进树】的内部模块依赖判"永不可解析"剪掉，而本子任务验收命令 grep 必考该
   artifactId → 剪→验收挂→worker 加回→再剪，注定永败。治=prune 留账，verify 失败时对账，
   第一轮即判 BLOCKED upstream_module_broken（等生产者/连坐放弃交 brain 既有通道）。
2. **H-3a 误分类**（st-2 实锤）：缺符号的生产者全在本子任务自己 scope 内=自己没建出
   （capability），判 BLOCKED 把可修编译错送进不可修通道、fix 循环短路。治=全自有→FAIL。
3. **死端 BLOCKED**：判归上游却吐不出任何可指名模块/文件（blocked_on 全空）=没有可等的
   生产者，BLOCKED 退避只白烧重试/熔断配额。治=落 FAIL 修复梯（fail-honest）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import swarm.worker.l1_pipeline as lp  # noqa: E402
from swarm.types import (FileScope, SubTask, SubTaskDifficulty,  # noqa: E402
                         TaskHarness)


def _mk(scope: FileScope, harness: TaskHarness) -> SubTask:
    return SubTask(
        id="st-r67l-b2", description="R67L-B2", difficulty=SubTaskDifficulty.MEDIUM,
        scope=scope, harness=harness,
    )


_DIFF = (
    "--- a/ruoyi-alarm/src/main/java/com/ruoyi/alarm/sender/Sender.java\n"
    "+++ b/ruoyi-alarm/src/main/java/com/ruoyi/alarm/sender/Sender.java\n"
    "@@ -1 +1 @@\n-old\n+new\n"
)
_SRC = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/sender/Sender.java"
_DTO = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/sender/dto/AlarmDto.java"


@pytest.fixture()
def project(tmp_path):
    """最小 Maven 工程：根 pom（com.ruoyi，reactor=ruoyi-alarm）+ 模块 pom。"""
    (tmp_path / "pom.xml").write_text(
        "<project><groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
        "<version>4.8.3</version><packaging>pom</packaging>"
        "<modules><module>ruoyi-alarm</module></modules></project>")
    mod = tmp_path / "ruoyi-alarm"
    mod.mkdir()
    (mod / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion>"
        "<parent><groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
        "<version>4.8.3</version></parent>"
        "<artifactId>ruoyi-alarm</artifactId></project>")
    return tmp_path


@pytest.fixture()
def quiet_gates(monkeypatch):
    """关掉与本组断言无关的实跑闸门（网络探测/格式化/lint/清单对账）。"""
    monkeypatch.setenv("SWARM_WORKER_L1_FORMAT", "false")
    monkeypatch.setenv("SWARM_WORKER_L1_LINT", "false")
    monkeypatch.setattr(lp, "_enforce_parent_version_literals", lambda *a, **k: (0, []))
    monkeypatch.setattr(lp, "_enforce_dep_legality", lambda *a, **k: (0, []))
    monkeypatch.setattr(lp, "_build_error_is_reactor_missing_module", lambda *a, **k: None)
    monkeypatch.setattr(lp, "_scan_fullwidth_punct", lambda *a, **k: [])
    import swarm.worker.workspace_manifest as wm
    monkeypatch.setattr(wm, "reconcile_workspace_manifests",
                        lambda *a, **k: {"modified_manifests": [], "added": []})


# ─── H-3a：scope 内生产者判据（纯函数）───


def test_scope_producer_detects_pkg_and_class():
    scope = FileScope(writable=[_SRC], create_files=[_DTO])
    own_p, own_c = lp._missing_internal_produced_in_scope(
        scope, {"com.ruoyi.alarm.sender.dto"},
        ["com.ruoyi.alarm.sender.dto.AlarmDto"])
    assert own_p == {"com.ruoyi.alarm.sender.dto"}
    assert own_c == {"com.ruoyi.alarm.sender.dto.AlarmDto"}


def test_scope_producer_ignores_unrelated_and_non_jvm():
    """别的包/非 JVM 布局（Thymeleaf 模板）一律不判——栈中立 fail-open。"""
    scope = FileScope(
        writable=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/sender/Sender.java",
                  "ruoyi-admin/src/main/resources/templates/alarm/edit.html"])
    own_p, own_c = lp._missing_internal_produced_in_scope(
        scope, {"com.ruoyi.alarm.sender.dto"},
        ["com.ruoyi.alarm.sender.dto.AlarmDto"])
    assert own_p == set() and own_c == set()


def test_scope_producer_none_scope():
    assert lp._missing_internal_produced_in_scope(
        None, {"com.ruoyi.x"}, []) == (set(), set())


# ─── H-3a：管线级分流 ───


def _drive_build_fail(project, monkeypatch, quiet_gates, scope, build_output):
    """公共驱动：构建必败（repair 零触达），返回 run_l1_pipeline 结果。"""
    monkeypatch.setattr(lp, "_run_l1_command", lambda cmd, pp, timeout=120: (1, build_output))
    monkeypatch.setattr(lp, "_attempt_build_repair",
                        lambda *a, **k: (0, []))
    st = _mk(scope, TaskHarness(language="java", build_command="mvn -q compile"))
    return lp.run_l1_pipeline(str(project), st, _DIFF, timeout=60)


def test_h3a_all_producers_in_scope_fails_not_blocked(project, monkeypatch, quiet_gates):
    """缺包的生产者在本子任务 create_files 里=自己没建出 → FAIL 进修复梯，不判 BLOCKED。"""
    monkeypatch.setattr(lp, "_build_error_is_upstream", lambda *a, **k: False)
    monkeypatch.setattr(lp, "_build_blocked_on_unbuilt_internal",
                        lambda *a, **k: {"com.ruoyi.alarm.sender.dto"})
    monkeypatch.setattr(lp, "_build_blocked_on_unbuilt_internal_classes",
                        lambda *a, **k: set())
    scope = FileScope(writable=[_SRC], create_files=[_DTO])
    ok, details = _drive_build_fail(
        project, monkeypatch, quiet_gates, scope,
        "[ERROR] package com.ruoyi.alarm.sender.dto does not exist")
    assert ok is False, f"应判 FAIL 进修复梯: {details.get('pipeline_blocked')}"
    assert details.get("pipeline_blocked") is None
    assert details.get("in_scope_producer_fail") == ["com.ruoyi.alarm.sender.dto"]
    assert details.get("build_failed")


def test_h3a_external_pkg_still_blocked(project, monkeypatch, quiet_gates):
    """缺包的生产者不在本 scope（真等上游）→ 维持 BLOCKED（回归：不误放宽）。"""
    monkeypatch.setattr(lp, "_build_error_is_upstream", lambda *a, **k: False)
    monkeypatch.setattr(lp, "_build_blocked_on_unbuilt_internal",
                        lambda *a, **k: {"com.ruoyi.alarm.sender.dto"})
    monkeypatch.setattr(lp, "_build_blocked_on_unbuilt_internal_classes",
                        lambda *a, **k: set())
    scope = FileScope(writable=[_SRC])  # 无 dto 生产者
    ok, details = _drive_build_fail(
        project, monkeypatch, quiet_gates, scope,
        "[ERROR] package com.ruoyi.alarm.sender.dto does not exist")
    assert ok is True, "BLOCKED 契约=ok=True + pipeline_blocked 置位"
    assert details.get("pipeline_blocked") == "internal_pkg_not_built"
    assert details.get("blocked_on_packages") == ["com.ruoyi.alarm.sender.dto"]
    assert "in_scope_producer_fail" not in details


def test_upstream_deadend_empty_evidence_falls_to_fail(project, monkeypatch, quiet_gates):
    """判归上游但 blocked_on 全空=死端判词（无可等的生产者）→ 落 FAIL，不烧退避配额。"""
    monkeypatch.setattr(lp, "_build_error_is_upstream", lambda *a, **k: True)
    monkeypatch.setattr(lp, "_build_error_modules", lambda *a, **k: set())
    monkeypatch.setattr(lp, "_build_error_files", lambda *a, **k: set())
    monkeypatch.setattr(lp, "_unresolved_internal_module_poms", lambda *a, **k: set())
    monkeypatch.setattr(lp, "_build_blocked_on_unbuilt_internal", lambda *a, **k: set())
    monkeypatch.setattr(lp, "_build_blocked_on_unbuilt_internal_classes", lambda *a, **k: set())
    scope = FileScope(writable=[_SRC])
    ok, details = _drive_build_fail(
        project, monkeypatch, quiet_gates, scope,
        "[ERROR] /x/Other.java:[1,1] some upstream-flavored error")
    assert ok is False, f"死端不得 BLOCKED 空等: {details.get('pipeline_blocked')}"
    assert details.get("pipeline_blocked") is None
    assert details.get("upstream_deadend_no_evidence") is True
    assert details.get("build_failed")


def test_upstream_with_named_evidence_still_blocked(project, monkeypatch, quiet_gates):
    """判归上游且有具名模块 → 维持 BLOCKED（回归：死端守卫不误伤真上游等待）。"""
    monkeypatch.setattr(lp, "_build_error_is_upstream", lambda *a, **k: True)
    monkeypatch.setattr(lp, "_build_error_modules", lambda *a, **k: {"ruoyi-gen"})
    monkeypatch.setattr(lp, "_build_error_files", lambda *a, **k: set())
    monkeypatch.setattr(lp, "_unresolved_internal_module_poms", lambda *a, **k: set())
    scope = FileScope(writable=[_SRC])
    ok, details = _drive_build_fail(
        project, monkeypatch, quiet_gates, scope,
        "[ERROR] /x/ruoyi-gen/A.java:[1,1] broken upstream")
    assert ok is True
    assert details.get("pipeline_blocked") == "upstream_module_broken"
    assert details.get("blocked_on_modules") == ["ruoyi-gen"]


# ─── prune↔验收矛盾：verify 阶段对账早判 BLOCKED ───


def test_prune_acceptance_conflict_early_blocked(project, monkeypatch, quiet_gates):
    """st-14 原型：prune 剪掉 ruoyi-alarm-interface，考卷 grep 必考它 → 第一轮即 BLOCKED。"""
    state = {"build_calls": 0, "repair_calls": 0}

    def _fake_run(cmd, pp, timeout=120):
        if cmd.strip().startswith("mvn"):
            state["build_calls"] += 1
            if state["build_calls"] == 1:
                return 1, ("[ERROR] Could not find artifact "
                           "com.ruoyi:ruoyi-alarm-interface:jar:4.8.3")
            return 0, "BUILD SUCCESS"
        if "grep" in cmd:
            return 1, ""      # 验收 grep 必挂（依赖已被剪）
        return 0, ""

    def _fake_repair(project_path, build_output, modified, timeout,
                     project_stack=None, evidence_out=None):
        state["repair_calls"] += 1
        if state["repair_calls"] == 1:
            # 真身语义：phantom-dep prune 判永不可解析剪除 + 留账
            if evidence_out is not None:
                evidence_out.setdefault(
                    "pruned_phantom_internal", set()).add("ruoyi-alarm-interface")
            return 1, ["ruoyi-alarm/pom.xml"]
        return 0, []

    monkeypatch.setattr(lp, "_run_l1_command", _fake_run)
    monkeypatch.setattr(lp, "_attempt_build_repair", _fake_repair)
    monkeypatch.setattr(lp, "_build_error_is_upstream", lambda *a, **k: False)

    scope = FileScope(writable=[_SRC, "ruoyi-alarm/pom.xml"])
    harness = TaskHarness(
        language="java", build_command="mvn -q compile",
        verify_commands=[
            "grep -q '<artifactId>ruoyi-alarm-interface</artifactId>' ruoyi-alarm/pom.xml"],
    )
    ok, details = lp.run_l1_pipeline(str(project), _mk(scope, harness), _DIFF, timeout=60)
    assert ok is True, "BLOCKED 契约=ok=True + pipeline_blocked 置位"
    assert details.get("pipeline_blocked") == "upstream_module_broken", details
    assert details.get("blocked_on_modules") == ["ruoyi-alarm-interface"]
    assert details.get("prune_acceptance_conflict") == ["ruoyi-alarm-interface"]
    assert "verify_failed" not in details, "矛盾情形不得落 verify_failed 烧修复轮"


def test_prune_without_acceptance_assertion_unchanged(project, monkeypatch, quiet_gates):
    """prune 留账但考卷不考该模块 → 对账不命中，verify 失败维持原 FAIL 语义（不误拦）。"""
    state = {"build_calls": 0, "repair_calls": 0}

    def _fake_run(cmd, pp, timeout=120):
        if cmd.strip().startswith("mvn"):
            state["build_calls"] += 1
            if state["build_calls"] == 1:
                return 1, ("[ERROR] Could not find artifact "
                           "com.ruoyi:ruoyi-alarm-interface:jar:4.8.3")
            return 0, "BUILD SUCCESS"
        if "grep" in cmd:
            return 1, ""
        return 0, ""

    def _fake_repair(project_path, build_output, modified, timeout,
                     project_stack=None, evidence_out=None):
        state["repair_calls"] += 1
        if state["repair_calls"] == 1:
            if evidence_out is not None:
                evidence_out.setdefault(
                    "pruned_phantom_internal", set()).add("ruoyi-alarm-interface")
            return 1, ["ruoyi-alarm/pom.xml"]
        return 0, []

    monkeypatch.setattr(lp, "_run_l1_command", _fake_run)
    monkeypatch.setattr(lp, "_attempt_build_repair", _fake_repair)
    monkeypatch.setattr(lp, "_build_error_is_upstream", lambda *a, **k: False)

    scope = FileScope(writable=[_SRC, "ruoyi-alarm/pom.xml"])
    harness = TaskHarness(
        language="java", build_command="mvn -q compile",
        verify_commands=["grep -q 'class Sender' ruoyi-alarm/src/main/java/X.java"],
    )
    ok, details = lp.run_l1_pipeline(str(project), _mk(scope, harness), _DIFF, timeout=60)
    assert ok is False, "考卷不考被剪模块 → 维持 verify_failed FAIL 语义"
    assert details.get("pipeline_blocked") is None
    assert details.get("verify_failed")


# ─── prune 留账：_attempt_maven_version_repair 直测（真 pom、本地命令）───


def test_prune_records_phantom_internal_evidence(project, monkeypatch):
    """phantom-internal 剪除必须留账 artifactId（verify 对账的唯一数据源）。"""
    mod_pom = project / "ruoyi-alarm" / "pom.xml"
    mod_pom.write_text(
        "<project><modelVersion>4.0.0</modelVersion>"
        "<parent><groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
        "<version>4.8.3</version></parent>"
        "<artifactId>ruoyi-alarm</artifactId>"
        "<dependencies><dependency>"
        "<groupId>com.ruoyi</groupId>"
        "<artifactId>ruoyi-alarm-interface</artifactId>"
        "<version>4.8.3</version>"
        "</dependency></dependencies></project>")
    evidence: dict = {}
    n, files = lp._attempt_maven_version_repair(
        str(project),
        "[ERROR] Could not find artifact com.ruoyi:ruoyi-alarm-interface:jar:4.8.3",
        60, evidence_out=evidence)
    assert evidence.get("pruned_phantom_internal") == {"ruoyi-alarm-interface"}, evidence
    assert n >= 1 and [f.lstrip("./") for f in files] == ["ruoyi-alarm/pom.xml"], (n, files)
    assert "ruoyi-alarm-interface" not in mod_pom.read_text(), "幻影依赖必须真被剪掉"


def test_prune_evidence_not_recorded_for_external_phantom(project, monkeypatch):
    """第三方幻影（非工程 groupId）不记内部账——对账只管 plan 声明内部模块。"""
    # 第三方坐标走 _fetch_maven_versions_probe：不可达=fail-open 不剪，本测试钉死
    # 【不记内部账】这一半语义（可达性分支由 R56-6 既有测试锁定）。
    monkeypatch.setattr(lp, "_fetch_maven_versions_probe",
                        lambda *a, **k: ([], True))  # 确证查无 → 会剪，但非内部账
    evidence: dict = {}
    lp._attempt_maven_version_repair(
        str(project),
        "[ERROR] Could not find artifact com.github.aerogear:aerogear-otp-java:jar:1.1.0",
        60, evidence_out=evidence)
    assert "pruned_phantom_internal" not in evidence, evidence
