"""round18 P2 治本：纯 pom/资源子任务（无可编译源码）不应"无 Java 即判负/空转 BLOCKED"，
而应走 `mvn validate` 确定性校验 pom 结构 + reactor 可解析性（st-30 变体 5065fe04/st-29-2 现场）。
"""
import os

from swarm.worker.l1_pipeline import _derive_full_build_command


def _touch(d, name):
    open(os.path.join(d, name), "w").close()


def test_pure_pom_derives_mvn_validate(tmp_path):
    _touch(tmp_path, "pom.xml")
    cmd = _derive_full_build_command(str(tmp_path), ["ruoyi-alarm/pom.xml"], {"build": "maven"})
    assert cmd == "mvn -q validate", cmd


def test_pure_root_pom_derives_validate(tmp_path):
    _touch(tmp_path, "pom.xml")
    cmd = _derive_full_build_command(str(tmp_path), ["pom.xml"], None)
    assert cmd == "mvn -q validate", cmd


def test_java_still_derives_compile_not_validate(tmp_path):
    """有 .java → 仍走全量 compile（不被 validate 分支抢走）。"""
    _touch(tmp_path, "pom.xml")
    cmd = _derive_full_build_command(str(tmp_path), ["ruoyi-alarm/src/main/java/A.java"], {"build": "maven"})
    assert cmd == "mvn -q compile", cmd


def test_pom_plus_java_prefers_compile(tmp_path):
    """pom + java 混合 → compile（更强，覆盖 validate）。"""
    _touch(tmp_path, "pom.xml")
    cmd = _derive_full_build_command(str(tmp_path), ["pom.xml", "src/main/java/A.java"], {"build": "maven"})
    assert cmd == "mvn -q compile", cmd


def test_non_maven_pom_absent_no_validate(tmp_path):
    """非 Maven 工程（无 pom.xml、无 maven 栈）→ 不臆造 validate。"""
    cmd = _derive_full_build_command(str(tmp_path), ["some/config.xml"], None)
    assert cmd == "", cmd


def test_pure_resource_non_pom_no_validate(tmp_path):
    """纯非 pom 资源（.properties/.yml）不触发 mvn validate（只对 pom 生效）。"""
    _touch(tmp_path, "pom.xml")
    cmd = _derive_full_build_command(str(tmp_path), ["src/main/resources/app.yml"], {"build": "maven"})
    assert cmd == "", cmd
