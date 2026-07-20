#!/usr/bin/env python3
"""#37 治本：L2/L1 gradle 编译命令用 `classes`(全 JVM 语言)而非 `compileJava`。

真根(审计 ab041ca)：build.gradle(.kts) 的编译命令硬编 `compileJava` → Kotlin/Scala 工程
编译零源→exit 0【假过自动 ACCEPT 坏构建】或任务不存在【冤杀好构建】。`classes` 任务由任一
JVM 语言插件创建，编译主源集全部语言(compileJava+compileKotlin+compileScala+compileGroovy)。
"""
from __future__ import annotations

import os
from unittest.mock import patch


def _write(d, name, body="x"):
    with open(os.path.join(d, name), "w", encoding="utf-8") as f:
        f.write(body)


def test_37_l2_gradle_kts_uses_classes(tmp_path):
    """L2 _detect_build_cmd_generic：build.gradle.kts(Kotlin)→ classes，绝不 compileJava。"""
    from swarm.brain.integration_review import _detect_build_cmd_generic
    _write(str(tmp_path), "build.gradle.kts", "plugins { kotlin(\"jvm\") }")
    cmd = _detect_build_cmd_generic(str(tmp_path))
    assert cmd and "classes" in cmd and "compileJava" not in cmd, cmd


def test_37_l2_gradle_groovy_uses_classes(tmp_path):
    """L2：build.gradle 同样用 classes。"""
    from swarm.brain.integration_review import _detect_build_cmd_generic
    _write(str(tmp_path), "build.gradle", "plugins { id 'java' }")
    cmd = _detect_build_cmd_generic(str(tmp_path))
    assert cmd and "classes" in cmd and "compileJava" not in cmd, cmd


def test_37_l2_maven_unchanged(tmp_path):
    """回归：Maven 仍 mvn compile，不受影响。"""
    from swarm.brain.integration_review import _detect_build_cmd_generic
    _write(str(tmp_path), "pom.xml", "<project/>")
    cmd = _detect_build_cmd_generic(str(tmp_path))
    assert cmd == "mvn -q -DskipTests compile", cmd


def test_37_l1_derive_gradle_kotlin_uses_classes():
    """L1 _derive_full_build_command：.kt 源 + build=gradle → classes（非 compileJava）。"""
    from swarm.worker import l1_pipeline
    with patch.object(l1_pipeline, "_manifest_present", return_value=True):
        cmd = l1_pipeline._derive_full_build_command(
            "/tmp/x", ["app/src/main/kotlin/A.kt"], {"build": "gradle"})
    assert "classes" in cmd and "compileJava" not in cmd, cmd


def test_37_l1_derive_maven_java_unchanged():
    """回归：.java + maven → mvn compile 不变。"""
    from swarm.worker import l1_pipeline
    with patch.object(l1_pipeline, "_manifest_present", return_value=True):
        cmd = l1_pipeline._derive_full_build_command(
            "/tmp/x", ["mod/src/main/java/A.java"], {"build": "maven"})
    assert cmd == "mvn -q compile", cmd
