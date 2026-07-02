"""round18 P0-B 复现 + 治本回归：确定性修复机制(module-registration 自愈 / version-repair)
合法触达的 scope 外文件（典型：父 pom）不应被 Phase4 scope 复核误判越权。

真机制（沙箱铁证 1b8ea89de4a04693a665991893c6f14b.jsonl）：
- worker 无任何对 pom.xml 的写命令（scope 只含 Java 文件）。
- VERIFYING 时 module-registration 自愈把 `ruoyi-alarm-sdk` 注册进 root pom，
  记入 details['repaired_file_paths']=['pom.xml']（executor._record_repaired_paths →
  _repaired_extra_paths）。此刻 scope 复核【先于】注册运行，diff 3 文件、scope_ok=True。
- PRODUCING(Phase4) 时 _get_git_diff 把 _repaired_extra_paths 里的 pom.xml 纳入 diff
  （executor.py:1927）→ diff 4 文件 → scope 复核见 pom.xml 越 scope → 整份判死
  （'来源=scope | scope 违规: [pom.xml]'）→ 14 个有效 Java 产出被误杀。

治本：scope 复核排除确定性修复触达的路径（只按 worker 实际写命令判 scope）。
"""
from swarm.types import FileScope, SubTask
from swarm.worker.l1_pipeline import _scope_violations, run_l1_pipeline

# 同形输入：3 个 scope 内 Java 文件 + 1 个 scope 外的 root pom（由 module-reg 自愈触达）。
_JAVA = (
    "--- a/ruoyi-alarm/src/main/java/com/ruoyi/alarm/engine/service/AlarmEngineService.java\n"
    "+++ b/ruoyi-alarm/src/main/java/com/ruoyi/alarm/engine/service/AlarmEngineService.java\n"
    "@@ -1 +1 @@\n+class AlarmEngineService {}\n"
)
_POM = (
    "--- a/pom.xml\n+++ b/pom.xml\n@@ -1 +1 @@\n"
    "+    <module>ruoyi-alarm-sdk</module>\n"
)
_SCOPE = FileScope(
    writable=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/engine/service/AlarmEngineService.java"],
    create_files=[],
)


def test_repaired_pom_not_violation_when_excluded():
    """治本：pom.xml 在 extra_allowed（=_repaired_extra_paths）中 → 不判越权。"""
    v = _scope_violations(_JAVA + _POM, _SCOPE, extra_allowed={"pom.xml"})
    assert v == [], f"确定性修复触达的 pom 不应判越权: {v}"


def test_repaired_pom_is_violation_without_exclusion():
    """复现（控制组）：不排除时 pom.xml 被误判越权——即 round18 P0-B 的现场。"""
    v = _scope_violations(_JAVA + _POM, _SCOPE)
    assert "pom.xml" in v


def test_real_worker_overreach_still_caught_with_exclusion():
    """不放水：worker 真越权改的 scope 外文件（不在 repaired 集合）仍判越权。"""
    other = (
        "--- a/ruoyi-common/src/main/java/com/ruoyi/common/Hack.java\n"
        "+++ b/ruoyi-common/src/main/java/com/ruoyi/common/Hack.java\n"
        "@@ -1 +1 @@\n+hacked\n"
    )
    v = _scope_violations(_JAVA + _POM + other, _SCOPE, extra_allowed={"pom.xml"})
    assert "ruoyi-common/src/main/java/com/ruoyi/common/Hack.java" in v
    assert "pom.xml" not in v


def test_pipeline_passes_scope_with_extra_writable_paths(tmp_path):
    """端到端：run_l1_pipeline 传入 extra_writable_paths → scope 阶段不再整份判死。"""
    st = SubTask(
        id="st-x",
        description="alarm engine service",
        scope=_SCOPE,
    )
    ok, details = run_l1_pipeline(
        str(tmp_path), st, _JAVA + _POM,
        extra_writable_paths={"pom.xml"},
    )
    assert details["l1_1_scope_ok"] is True, details.get("scope_violations")
    assert details["scope_violations"] == []


def test_pipeline_scope_fail_without_extra_paths(tmp_path):
    """控制组：不传 extra_writable_paths → 复现 scope 整份判死（返回 False）。"""
    st = SubTask(
        id="st-x",
        description="alarm engine service",
        scope=_SCOPE,
    )
    ok, details = run_l1_pipeline(str(tmp_path), st, _JAVA + _POM)
    assert ok is False
    assert "pom.xml" in details["scope_violations"]
