"""R63-T8 治本锁：上游越界破坏 worker fail-fast。

round63 实锤（logs_archive/round63_postmortem/swarm.noheartbeat.log）：
  · auto-repair 毒化 ruoyi-framework/pom.xml + 共享 spring-boot.version → ruoyi-common
    编译崩 → 所有 -am reactor 兄弟 `upstream_module_broken`（L5036 首现 10:18:21）；
  · st-8 明知阻断在 scope 外仍三阶段各撞 95 迭代、3 轮共烧 ≈41min（L5285/5462/7372）；
  · brain 三次 retry 全投向未清洗环境（L5755/6652/7603）。

四个 worker 侧缺口（调查定案）：
  ① `_build_error_is_upstream` 用「报错文件 vs 本轮 diff」近似 scope 归属——空 diff/
     无 -pl 时双双失灵 → 掉进 build_failed 硬 FAIL 烧修复轮。治本=传入真 FileScope，
     写权归属做确定性判据。
  ② 依赖解析级错误（`Could not find artifact <内部模块坐标>`）不含文件路径 →
     errs_files 恒空 → 盲区（_build_blocked_on_unbuilt_internal 只管 package 级）。
     治本=坐标→注册模块 pom 路径映射（复用 _maven_modules）。
  ③ BLOCKED 后 `_phase_produce` 仍无条件跑 produce LLM 步 + Phase-4 复核
     （st-8 PRODUCING 622.8s/95 迭代发生在 blocked 判定之后）。治本=短路。
  ④ BLOCKED 时 det_ok=None → verify agent 步照跑（VERIFYING 95 迭代来源），
     其输出对 BLOCKED 恒被仲裁器丢弃=纯浪费。治本=跳过。
brain 侧消费（T3 死锁探测/C9 补边）契约键（pipeline_blocked/blocked_on_modules/
blocked_on_files/not_run_kind）保持不变——T8 只提升命中率与省预算，零改契约。
"""
from __future__ import annotations

import pytest
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskHarness
from swarm.worker.l1_pipeline import _build_error_is_upstream

# round63 第二代死因原文形态（L5463）：编译错在 scope 外的 ruoyi-common（保持日志原文，不折行）
_R63_UPSTREAM_COMPILE_ERR = """\
[ERROR] COMPILATION ERROR :
[ERROR] /workspace/ruoyi-common/src/main/java/com/ruoyi/common/utils/StringUtils.java:[9,32] cannot find symbol
  symbol:   class Strings
  location: package org.apache.commons.lang3
[ERROR] /workspace/ruoyi-common/src/main/java/com/ruoyi/common/config/thread/ThreadPoolConfig.java:[52,31] cannot find symbol
  symbol:   method builder()
[ERROR] After correcting the problems, you can resume the build with the command
[ERROR]   mvn <args> -rf :ruoyi-common
"""  # noqa: E501

# ② 形态：依赖解析级错误——不含任何源文件路径，只有坐标
_R63_ARTIFACT_RESOLVE_ERR = """\
[ERROR] Failed to execute goal on project ruoyi-alarm: Could not resolve dependencies \
for project com.ruoyi:ruoyi-alarm:jar:3.8.7
[ERROR] Could not find artifact com.ruoyi:ruoyi-common:jar:3.8.7
[ERROR] -> [Help 1]
"""

_ALARM_SCOPE = FileScope(writable=[
    "ruoyi-alarm/src/main/java/com/ruoyi/alarm/config/AlarmConfig.java",
    "ruoyi-alarm/src/main/java/com/ruoyi/alarm/util/AlarmUtil.java",
])


def _mk_maven_tree(tmp_path, modules=("ruoyi-common", "ruoyi-alarm")):
    mods_xml = "".join(f"<module>{m}</module>" for m in modules)
    (tmp_path / "pom.xml").write_text(
        f"<project><modules>{mods_xml}</modules></project>", encoding="utf-8")
    for m in modules:
        d = tmp_path / m
        d.mkdir(exist_ok=True)
        (d / "pom.xml").write_text("<project/>", encoding="utf-8")
    return str(tmp_path)


# ═══════════ ① scope 确定性通道 ═══════════

def test_scope_channel_detects_upstream_without_pl_or_diff():
    """★头号锁★ 无 -pl、空 diff（旧启发式双双失灵的形态）——报错文件全在写权集外
    → 必须确定性判 upstream，绝不掉进 build_failed 硬 FAIL 烧修复轮。"""
    assert _build_error_is_upstream(
        _R63_UPSTREAM_COMPILE_ERR, "mvn -B -T 1C compile",
        modified=[], scope=_ALARM_SCOPE,
    ) is True


def test_scope_channel_never_masks_own_error():
    """报错文件在写权集内（哪怕本轮 diff 还没碰它）→ 绝不判 upstream（自己的错源头修）。"""
    err = _R63_UPSTREAM_COMPILE_ERR.replace(
        "ruoyi-common/src/main/java/com/ruoyi/common/utils/StringUtils.java",
        "ruoyi-alarm/src/main/java/com/ruoyi/alarm/config/AlarmConfig.java",
    )
    assert _build_error_is_upstream(
        err, "mvn -B compile", modified=[], scope=_ALARM_SCOPE,
    ) is False


def test_scope_channel_mixed_errors_not_upstream():
    """自己的错 + 上游的错混合 → 不判 upstream（先修自己的，别把自身缺陷推给上游）。"""
    mixed = (_R63_UPSTREAM_COMPILE_ERR
             + "[ERROR] /workspace/ruoyi-alarm/src/main/java/com/ruoyi/alarm/util/"
               "AlarmUtil.java:[3,1] cannot find symbol\n")
    assert _build_error_is_upstream(
        mixed, "mvn -B compile", modified=[], scope=_ALARM_SCOPE,
    ) is False


def test_allow_any_scope_skips_channel():
    """allow_any scope（全项目可写）→ scope 通道让位旧启发式（没有'写权外'概念）。"""
    assert _build_error_is_upstream(
        _R63_UPSTREAM_COMPILE_ERR, "mvn -B compile",
        modified=[], scope=FileScope(allow_any=True),
    ) is False  # 旧回退：无 -pl 无 modified → False


def test_backward_compat_without_scope():
    """scope=None → 旧行为原样保留（文件级 disjoint 命中 round63 带 -pl 形态）。"""
    assert _build_error_is_upstream(
        _R63_UPSTREAM_COMPILE_ERR, "mvn -B -pl ruoyi-alarm -am compile",
        modified=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/config/AlarmConfig.java"],
    ) is True
    assert _build_error_is_upstream(
        _R63_UPSTREAM_COMPILE_ERR, "mvn -B compile", modified=[],
    ) is False  # 旧盲区如实保留（无 scope 时无从判定）


# ═══════════ ② 内部 artifact 坐标→pom 路径映射 ═══════════

def test_unresolved_internal_artifact_maps_to_module_pom(tmp_path):
    from swarm.worker.l1_pipeline import _unresolved_internal_module_poms
    root = _mk_maven_tree(tmp_path)
    assert _unresolved_internal_module_poms(_R63_ARTIFACT_RESOLVE_ERR, root) == {
        "ruoyi-common/pom.xml"}
    # 第三方坐标（无对应注册模块）→ 空集（交 dep-repair 防线④，不冒充 upstream）
    third = "[ERROR] Could not find artifact org.apache.kafka:kafka-clients:jar:9.9.9\n"
    assert _unresolved_internal_module_poms(third, root) == set()


def test_upstream_true_on_internal_artifact_resolution_failure(tmp_path):
    """★②主锁★ 依赖解析级错误（零文件路径）：旧代码 errs_files 空+模块正则不匹配
    → False 硬 FAIL；新代码坐标映射到注册模块 pom → scope 外 → True。"""
    root = _mk_maven_tree(tmp_path)
    assert _build_error_is_upstream(
        _R63_ARTIFACT_RESOLVE_ERR, "mvn -B compile",
        modified=[], scope=_ALARM_SCOPE, project_path=root,
    ) is True


# ── 对抗双复核整改锁 ────────────────────────────────────────────

def test_version_mismatch_coordinate_not_mapped(tmp_path):
    """★复核 R-MED 锁★ 缺失坐标版本与模块真身版本不符=引用方幻觉版本（round47 类），
    绝不把健康兄弟模块诬告成 upstream——不映射，留 fix 循环源头修。"""
    from swarm.worker.l1_pipeline import _unresolved_internal_module_poms
    root = _mk_maven_tree(tmp_path)
    (tmp_path / "ruoyi-common" / "pom.xml").write_text(
        "<project><parent><version>9.0.0</version></parent>"
        "<artifactId>ruoyi-common</artifactId><version>3.8.7</version></project>",
        encoding="utf-8")
    ghost = "[ERROR] Could not find artifact com.ruoyi:ruoyi-common:jar:9.9.9\n"
    assert _unresolved_internal_module_poms(ghost, root) == set(), \
        "版本不符=幻觉引用，不得映射 upstream"
    # 版本相符（真·产物未就绪）→ 照常映射
    real = "[ERROR] Could not find artifact com.ruoyi:ruoyi-common:jar:3.8.7\n"
    assert _unresolved_internal_module_poms(real, root) == {"ruoyi-common/pom.xml"}
    # 版本不可判定（模块继承父且根 pom 也是属性形态）→ 保守仍映射
    (tmp_path / "ruoyi-common" / "pom.xml").write_text("<project/>", encoding="utf-8")
    (tmp_path / "pom.xml").write_text(
        "<project><modules><module>ruoyi-common</module><module>ruoyi-alarm</module>"
        "</modules><version>${revision}</version></project>", encoding="utf-8")
    assert _unresolved_internal_module_poms(ghost, root) == {"ruoyi-common/pom.xml"}


def test_scope_exception_falls_back_with_warning(caplog):
    """★猎手 F1 锁★ scope 判定异常 → fail-open 落旧启发式，但必须 WARNING 可观测
    （新判据静默失效=退回治本前旧行为且无人察觉）。"""
    import logging

    class _BadScope:
        allow_any = False

        def is_writable(self, _f):
            raise TypeError("bad scope object")

    with caplog.at_level(logging.WARNING, logger="swarm.worker.l1_pipeline"):
        got = _build_error_is_upstream(
            _R63_UPSTREAM_COMPILE_ERR, "mvn -B compile",
            modified=[], scope=_BadScope())
    assert got is False, "异常降级后走旧启发式（无 -pl 无 diff → False）"
    assert any("scope 写权判定异常" in r.message for r in caplog.records), \
        "fail-open 必须 WARNING，不许静默"


def test_pom_map_tree_failure_warns(tmp_path, caplog, monkeypatch):
    """★猎手 F2 锁★ 坐标→模块映射读树失败 → fail-open 空集，但必须 WARNING 可观测。"""
    import logging

    import swarm.worker.l1_pipeline as lp

    def _boom(_p):
        raise OSError("tree unreadable")

    monkeypatch.setattr(lp, "_maven_modules", _boom)
    with caplog.at_level(logging.WARNING, logger="swarm.worker.l1_pipeline"):
        got = lp._unresolved_internal_module_poms(_R63_ARTIFACT_RESOLVE_ERR, str(tmp_path))
    assert got == set()
    assert any("坐标→模块映射读树失败" in r.message for r in caplog.records)


def test_upstream_judge_channel_recorded():
    """★猎手 F4 锁★ 判据通道写入 evidence_out——复盘可直接看走的哪条通道。"""
    ev: dict = {}
    _build_error_is_upstream(_R63_UPSTREAM_COMPILE_ERR, "mvn -B compile",
                             modified=[], scope=_ALARM_SCOPE, evidence_out=ev)
    assert ev.get("channel") == "scope"
    ev2: dict = {}
    _build_error_is_upstream(
        _R63_UPSTREAM_COMPILE_ERR, "mvn -B -pl ruoyi-alarm -am compile",
        modified=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/config/AlarmConfig.java"],
        evidence_out=ev2)
    assert ev2.get("channel") == "file_disjoint"


# ═══════════ 端到端接线锁（run_l1_pipeline 真跑到 BLOCKED 契约） ═══════════

def test_pipeline_end_to_end_upstream_blocked_on_artifact_error(tmp_path, monkeypatch):
    """★接线锁★ 从 run_l1_pipeline 入口全链路：harness 构建失败输出 ② 形态错误 →
    pipeline_blocked=upstream_module_broken + blocked_on_modules 含 ruoyi-common
    （T3/C9 消费的契约键原样产出）。旧代码此处落 build_failed 硬 FAIL。"""
    from swarm.worker.l1_pipeline import run_l1_pipeline

    monkeypatch.setenv("SWARM_WORKER_IMPORT_REPAIR", "false")
    root = _mk_maven_tree(tmp_path)
    err_file = tmp_path / "build_err.txt"
    err_file.write_text(_R63_ARTIFACT_RESOLVE_ERR, encoding="utf-8")
    src = tmp_path / "ruoyi-alarm/src/main/java/com/ruoyi/alarm/config"
    src.mkdir(parents=True)
    subtask = SubTask(
        id="st-8", description="x", difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(
            writable=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/config/AlarmConfig.java"]),
        harness=TaskHarness(build_command="sh -c 'cat build_err.txt; exit 1'"),
    )
    diff = (
        "--- /dev/null\n"
        "+++ b/ruoyi-alarm/src/main/java/com/ruoyi/alarm/config/AlarmConfig.java\n"
        "@@ -0,0 +1,2 @@\n"
        "+package com.ruoyi.alarm.config;\n"
        "+public class AlarmConfig {}\n"
    )
    (src / "AlarmConfig.java").write_text(
        "package com.ruoyi.alarm.config;\npublic class AlarmConfig {}\n", encoding="utf-8")
    ok, details = run_l1_pipeline(root, subtask, diff)
    assert ok is True, f"BLOCKED 契约应 ok=True：{details}"
    assert details.get("pipeline_blocked") == "upstream_module_broken", details
    assert details.get("not_run_kind") == "blocked"
    assert "ruoyi-common" in (details.get("blocked_on_modules") or []), \
        f"必须结构化吐阻断模块供 brain 反查生产者: {details.get('blocked_on_modules')}"
    assert details.get("upstream_judge_channel") == "scope", \
        "判据通道必须留痕（猎手 F4）"


# ═══════════ ④ verify agent 步 BLOCKED 短路 ═══════════

def test_should_run_verify_agent_matrix():
    """BLOCKED 时 verify agent 输出恒被仲裁器丢弃（not_run_kind=BLOCKED →
    verification_not_run）——跑它=纯烧预算（round63 VERIFYING 95 迭代来源）。"""
    from swarm.worker.executor import _should_run_verify_agent

    blocked = {"pipeline_blocked": "upstream_module_broken"}
    assert _should_run_verify_agent("auto", None, blocked) is False
    assert _should_run_verify_agent("auto", None, {}) is True
    assert _should_run_verify_agent("auto", True, {}) is False   # det 有结论=旧行为
    assert _should_run_verify_agent("auto", False, {}) is False
    assert _should_run_verify_agent("always", None, blocked) is True  # 显式旧行为开关不动
    assert _should_run_verify_agent("never", None, {}) is False


# ═══════════ ③ produce 步 BLOCKED 短路 ═══════════

def test_blocked_failfast_kind():
    from swarm.worker.executor import _blocked_failfast_kind
    from swarm.worker.l1_verdict import L1Verdict

    blocked_prior = L1Verdict(passed=False, source="verification_not_run",
                              reason="", sticky=False, details={})
    det_prior = L1Verdict(passed=False, source="deterministic",
                          reason="", sticky=True, details={})
    d = {"pipeline_blocked": "upstream_module_broken"}
    assert _blocked_failfast_kind(blocked_prior, d) == "upstream_module_broken"
    assert _blocked_failfast_kind(det_prior, d) is None, "真编译错不得冒充 BLOCKED 短路"
    assert _blocked_failfast_kind(None, d) is None
    assert _blocked_failfast_kind(blocked_prior, {}) is None


@pytest.mark.asyncio
async def test_phase_produce_skips_llm_and_reverify_on_blocked():
    """★③主锁★ BLOCKED 时 _phase_produce 不跑 produce LLM 步（st-8 实测 622.8s/95
    迭代白烧点），diff 照常收集（已做改动不丢），上报文本带结构化阻断信息。"""
    from types import SimpleNamespace

    from swarm.types import Confidence, WorkerOutput
    from swarm.worker.executor import WorkerExecutor, WorkerPhase  # noqa: F401
    from swarm.worker.l1_verdict import L1Verdict

    calls = {"agent": 0, "produce_texts": []}

    def _mk(prior):
        ex = object.__new__(WorkerExecutor)
        ex.project_path = None          # Phase-4 复核本就按无 project 跳过，聚焦 agent 步差分
        ex.subtask = SimpleNamespace(intent="implement", harness=None)
        ex._log = lambda *_a, **_k: None

        async def _sync(_tag):
            return None
        ex._sync_from_sandbox = _sync

        async def _agent(_prompt, step="produce"):
            calls["agent"] += 1
            return "LLM 产出摘要"
        ex._run_agent = _agent

        def _parse(text, l1_passed, l1_details):
            calls["produce_texts"].append(text)
            return WorkerOutput(subtask_id="st-8", diff="--- a\n+++ b\n@@ -1 +1 @@\n-a\n+b\n",
                                summary=text[:80], l1_passed=l1_passed,
                                l1_details=l1_details, confidence=Confidence.LOW)
        ex._parse_produce_result = _parse
        ex._rollback_failed_manifest_footprint = lambda *_a, **_k: None
        return ex, prior

    blocked_prior = L1Verdict(passed=False, source="verification_not_run",
                              reason="", sticky=False, details={})
    l1d = {"pipeline_blocked": "upstream_module_broken",
           "blocked_on_modules": ["ruoyi-common"]}
    ex, prior = _mk(blocked_prior)
    out = await ex._phase_produce(False, dict(l1d), prior=prior)
    assert calls["agent"] == 0, "BLOCKED 必须跳过 produce LLM 步"
    assert out.diff.strip(), "已做改动必须随 diff 回传（fail-fast 不丢工作）"
    assert "upstream_module_broken" in calls["produce_texts"][0]
    assert "ruoyi-common" in calls["produce_texts"][0], "上报文本须带阻断模块，brain 可读"

    # 对照：非 BLOCKED（普通确定性失败）→ produce LLM 步照跑（老行为不破）
    det_prior = L1Verdict(passed=False, source="deterministic",
                          reason="", sticky=True, details={})
    ex2, prior2 = _mk(det_prior)
    await ex2._phase_produce(False, {"error": "compile"}, prior=prior2)
    assert calls["agent"] == 1, "非 BLOCKED 不得误短路"


@pytest.mark.asyncio
async def test_phase_produce_blocked_skips_debug_failing_test_gate():
    """★猎手 F3/复核 LOW 锁★ DEBUG 意图 + BLOCKED：failing_test_command 专属闸门也
    必须短路——对着被上游阻断的工作区跑它只会再失败一次（120s 白烧）且日志误导。"""
    from types import SimpleNamespace

    from swarm.types import Confidence, WorkerOutput
    from swarm.worker.executor import WorkerExecutor
    from swarm.worker.l1_verdict import L1Verdict

    gate_calls = {"n": 0}
    ex = object.__new__(WorkerExecutor)
    ex.project_path = "/tmp/fake-project"  # 非空：DEBUG 闸旧条件会进
    ex.subtask = SimpleNamespace(
        intent="debug",
        harness=SimpleNamespace(failing_test_command="mvn test -Dtest=X"))
    ex._log = lambda *_a, **_k: None

    async def _sync(_tag):
        return None
    ex._sync_from_sandbox = _sync

    async def _agent(_p, step="produce"):
        return "x"
    ex._run_agent = _agent

    def _parse(text, l1_passed, l1_details):
        return WorkerOutput(subtask_id="st-d", diff="", summary=text[:50],
                            l1_passed=l1_passed, l1_details=l1_details,
                            confidence=Confidence.LOW)
    ex._parse_produce_result = _parse

    def _gate(_cmd):
        gate_calls["n"] += 1
        return False, "boom"
    ex._run_failing_test_gate = _gate
    ex._rollback_failed_manifest_footprint = lambda *_a, **_k: None
    # project_path 非空会触发 Phase-4 det 闸重跑——BLOCKED 分支应整体跳过它
    ex._deterministic_l1_gate = lambda: (_ for _ in ()).throw(
        AssertionError("BLOCKED 短路后不得重跑确定性闸门"))

    prior = L1Verdict(passed=False, source="verification_not_run",
                      reason="", sticky=False, details={})
    l1d = {"pipeline_blocked": "upstream_module_broken",
           "blocked_on_modules": ["ruoyi-common"]}
    out = await ex._phase_produce(False, l1d, prior=prior)
    assert gate_calls["n"] == 0, "BLOCKED 必须跳过 DEBUG failing_test 闸门"
    assert out.l1_details.get("pipeline_blocked") == "upstream_module_broken", \
        "结构化阻断键必须原样保留（brain T3/C9 消费）"


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
