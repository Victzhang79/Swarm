"""R65TR-T5：基线工程约定漂移——注解处理器（Lombok 形态）基线在位性钉死。

治后回放 C 路实证：RuoYi 基线零 Lombok，交付 12 文件引入 @Data；JDK17 编译绿但
JDK≥23 默认关闭隐式注解处理→112 处找不到符号必挂=环境条件性断裂；且 3 个 @Data
类无模块内调用者=Lombok 失效时静默编译通过的跨模块哑弹。漂移源=模型训练先验
（经验层排查无源头）。

治法=jakarta/javax 命名空间先例同型（_detect_jvm_facts：磁盘 ground truth 钉死
硬前提）：基线构建清单/源码双证探测 Lombok 在位性 → format_stack_for_prompt 渲染
硬约束（不在位=禁 Lombok 注解必须手写访问器；在位=可用）。JVM 专属事实放 per-stack
facts 是既有架构（非 JVM 返回 None 不污染别栈画像）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from swarm.brain.stack_detect import _detect_jvm_facts, format_stack_for_prompt


def _mk_maven(tmp_path: Path, pom_extra: str = "", src: dict | None = None) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion>"
        "<groupId>g</groupId><artifactId>a</artifactId><version>1</version>"
        "<properties><java.version>8</java.version></properties>"
        f"{pom_extra}</project>")
    for rel, text in (src or {}).items():
        p = proj / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return proj


def _jvm(proj: Path):
    java = [str(p) for p in proj.rglob("*.java")]
    return _detect_jvm_facts(str(proj), {"pom.xml": (proj / "pom.xml").read_text().lower()}, java)


def test_detect_lombok_absent_in_baseline(tmp_path):
    proj = _mk_maven(tmp_path, src={
        "src/main/java/com/x/User.java":
            "package com.x;\npublic class User { private String name; "
            "public String getName(){return name;} }\n"})
    facts = _jvm(proj)
    assert facts is not None
    assert facts.get("lombok_available") is False, facts


def test_detect_lombok_present_via_pom(tmp_path):
    proj = _mk_maven(
        tmp_path,
        pom_extra="<dependencies><dependency><groupId>org.projectlombok</groupId>"
                  "<artifactId>lombok</artifactId></dependency></dependencies>")
    facts = _jvm(proj)
    assert facts is not None
    assert facts.get("lombok_available") is True, facts


def test_detect_lombok_present_via_source(tmp_path):
    proj = _mk_maven(tmp_path, src={
        "src/main/java/com/x/Dto.java":
            "package com.x;\nimport lombok.Data;\n@Data\npublic class Dto {}\n"})
    facts = _jvm(proj)
    assert facts is not None
    assert facts.get("lombok_available") is True, facts


def test_prompt_renders_lombok_ban_when_absent():
    p = format_stack_for_prompt({
        "backend": "java", "build": "maven", "frontend": "无", "frontend_kind": "",
        "confidence": 0.9,
        "jvm": {"servlet_namespace": "javax", "namespace_source": "t",
                "spring_boot_version": "", "java_version": "8",
                "lombok_available": False},
    })
    assert "Lombok" in p and ("严禁" in p or "禁止" in p), p
    assert "@Data" in p, "硬约束必须点名典型注解"
    assert "手写" in p, "必须给出正向替代（手写访问器）"


def test_prompt_allows_lombok_when_present():
    p = format_stack_for_prompt({
        "backend": "java", "build": "maven", "frontend": "无", "frontend_kind": "",
        "confidence": 0.9,
        "jvm": {"servlet_namespace": "jakarta", "namespace_source": "t",
                "spring_boot_version": "3.2.0", "java_version": "17",
                "lombok_available": True},
    })
    assert "禁止 @Data" not in p and "严禁 Lombok" not in p, p


def test_prompt_silent_when_fact_unknown():
    """老画像/回放 profile 无该键 → 不渲染任何 Lombok 行（不猜）。"""
    p = format_stack_for_prompt({
        "backend": "java", "build": "maven", "frontend": "无", "frontend_kind": "",
        "confidence": 0.9,
        "jvm": {"servlet_namespace": "javax", "namespace_source": "t",
                "spring_boot_version": "", "java_version": "8"},
    })
    assert "Lombok" not in p, p


# ── 猎手整改锁 ────────────────────────────────────────────────────────


def test_exclusion_block_not_false_positive(tmp_path):
    """猎手 F2：蓄意传递排除块（挡三方 starter 引入 lombok）绝不算"在位"——
    误放行=探测器自己复现要防的哑弹。"""
    proj = _mk_maven(
        tmp_path,
        pom_extra="<dependencies><dependency><groupId>com.some</groupId>"
                  "<artifactId>starter</artifactId><exclusions><exclusion>"
                  "<!-- 避免传递引入 lombok，本项目未启用 -->"
                  "<groupId>org.projectlombok</groupId><artifactId>lombok</artifactId>"
                  "</exclusion></exclusions></dependency></dependencies>")
    facts = _jvm(proj)
    assert facts is not None
    assert facts.get("lombok_available") is False, facts


def test_submodule_pom_declaration_detected(tmp_path):
    """猎手 F1：依赖只声明在非根子模块 pom（常见：common/domain 模块）——
    manifest_texts 按 basename 累积后必须探到，不得被 last-write-wins 吞。"""
    from swarm.brain.stack_detect import detect_stack_deterministic

    proj = _mk_maven(tmp_path)
    for mod, extra in (("mod-a", ""), ("mod-b",
            "<dependencies><dependency><groupId>org.projectlombok</groupId>"
            "<artifactId>lombok</artifactId></dependency></dependencies>"),
            ("mod-c", "")):
        d = proj / mod
        d.mkdir()
        (d / "pom.xml").write_text(
            f"<project><artifactId>{mod}</artifactId>{extra}</project>")
    prof = detect_stack_deterministic(str(proj))
    jvm = prof.get("jvm") or {}
    assert jvm.get("lombok_available") is True, jvm


def test_stack_schema_version_bumped():
    """猎手 F3：画像新增字段必 bump schema 版本（前例 108676a 纪律）——
    否则已缓存画像永缺 lombok 键、硬约束永不渲染。"""
    from swarm.brain.planning_nodes import _STACK_SCHEMA_VERSION
    assert _STACK_SCHEMA_VERSION >= 3


def test_non_jvm_unpolluted(tmp_path):
    proj = tmp_path / "npm"
    proj.mkdir()
    (proj / "package.json").write_text("{}")
    facts = _detect_jvm_facts(str(proj), {"package.json": "{}"}, [])
    assert facts is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
