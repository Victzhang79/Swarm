"""项目沙箱镜像构建器 — 生成器纯逻辑单测（worker/image_builder.py）。

只测纯逻辑：Dockerfile 生成、Maven warmup pom 生成（排内部模块）。
SSH 执行/真实构建涉及外部沙箱机，不在单测覆盖（靠真实 E2E 验证）。
"""
from __future__ import annotations

import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.project.sandbox_spec import EnvSpec, Toolchain, infer_env_spec
from swarm.worker.image_builder import generate_dockerfile, generate_maven_warmup_pom


def test_dockerfile_java_jdk_version():
    """Java 工具链 → Dockerfile 用探测到的 JDK 版本。"""
    spec = EnvSpec(project_id="p1", toolchains=[
        Toolchain(name="java", version="17", build_tool="maven", dep_source="pom.xml")])
    # src_included=True：warmup 离线自测(mvn -o)现在发生在 COPY 真项目源码之后
    # （v3+：对真项目编译预热 .m2，而非旧的精简 warmup pom）。
    df = generate_dockerfile(spec, src_included=True)
    assert "openjdk-17-jdk maven" in df
    assert "java-17-openjdk" in df
    assert "mvn -o" in df  # 离线自测（COPY 源码后对真项目离线编译）
    assert "FROM ghcr.io/tencentcloud/cubesandbox-base" in df
    print("  ✅ Java Dockerfile: JDK17 + maven + mvn -o 自测")


def test_dockerfile_base_only():
    """空项目 → base-only Dockerfile，不装工具链。"""
    spec = EnvSpec(project_id="p2", base_only=True)
    df = generate_dockerfile(spec)
    assert "base-only" in df
    assert "openjdk" not in df and "nodejs" not in df
    print("  ✅ base_only Dockerfile 不装工具链")


def test_dockerfile_mixed_java_node():
    """混编 java+node → 两个工具链都装。"""
    spec = EnvSpec(project_id="p3", toolchains=[
        Toolchain(name="java", version="17", build_tool="maven", dep_source="pom.xml"),
        Toolchain(name="node", version="20", build_tool="npm", dep_source="package.json")])
    df = generate_dockerfile(spec)
    assert "openjdk-17-jdk" in df
    assert "setup_20.x" in df
    print("  ✅ 混编 Dockerfile 装 java+node")


def test_warmup_pom_excludes_internal_modules(tmp_path):
    """warmup pom：保留外部依赖，排除项目内部模块（同 groupId）。"""
    (tmp_path / "pom.xml").write_text("""<?xml version="1.0"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <groupId>com.myorg</groupId>
  <properties><spring-boot.version>3.2.0</spring-boot.version></properties>
  <dependencyManagement><dependencies>
    <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-dependencies</artifactId><version>${spring-boot.version}</version></dependency>
  </dependencies></dependencyManagement>
  <dependencies>
    <dependency><groupId>org.apache.commons</groupId><artifactId>commons-lang3</artifactId><version>3.14.0</version></dependency>
    <dependency><groupId>com.myorg</groupId><artifactId>myorg-common</artifactId><version>1.0</version></dependency>
  </dependencies>
</project>""", encoding="utf-8")
    pom = generate_maven_warmup_pom(tmp_path, "pom.xml")
    # XML 合法
    ET.fromstring(pom)
    # 外部依赖保留
    assert "commons-lang3" in pom
    assert "spring-boot.version" in pom
    # 内部模块排除（com.myorg 的 myorg-common）
    assert "myorg-common" not in pom, "内部模块应被排除"
    print("  ✅ warmup pom 保留外部依赖、排除内部模块")


def test_warmup_pom_real_ruoyi():
    """真实 ruoyi-e2e（若存在）→ warmup pom 含 SB4.0.6 且无 ruoyi 内部模块。"""
    ruoyi = Path("/Users/zhangyanrui/LLM/swarm/e2e-projects/RuoYi")
    if not (ruoyi / "pom.xml").exists():
        print("  ⊘ 跳过(ruoyi-e2e 不在本机)")
        return
    pom = generate_maven_warmup_pom(ruoyi, "pom.xml")
    ET.fromstring(pom)
    assert "4.0.6" in pom  # SB 版本
    assert "shiro-core" in pom
    assert "ruoyi-common" not in pom and "ruoyi-system" not in pom  # 内部模块排除
    print("  ✅ 真实 ruoyi-e2e warmup pom: SB4.0.6 + 排内部模块")


def test_deps_hash_in_dockerfile():
    """Dockerfile 注释含 deps_hash（缓存判断用）。"""
    spec = EnvSpec(project_id="p4", toolchains=[Toolchain(name="java", version="17", build_tool="maven")])
    df = generate_dockerfile(spec)
    assert spec.deps_hash() in df
    print("  ✅ Dockerfile 含 deps_hash 指纹")


if __name__ == "__main__":
    import tempfile
    test_dockerfile_java_jdk_version()
    test_dockerfile_base_only()
    test_dockerfile_mixed_java_node()
    with tempfile.TemporaryDirectory() as d:
        test_warmup_pom_excludes_internal_modules(Path(d))
    test_warmup_pom_real_ruoyi()
    test_deps_hash_in_dockerfile()
    print("\n✅ image_builder 生成器全部测试通过")
