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


def test_go_toolchain_bakes_goimports():
    """Go 工具链 → 烤入 goimports，且 GOBIN/PATH 在镜像层（非登录 shell 可解析）。"""
    df = generate_dockerfile(
        EnvSpec(project_id="g1", toolchains=[Toolchain(name="go", build_tool="go", dep_source="go.mod")]),
        src_included=True,
    )
    assert "goimports@v0.24.0" in df
    assert "GOBIN=/usr/local/bin" in df
    assert "/root/go/bin" in df  # PATH 含 go bin


def test_node_warmup_npm_install_in_frontend_dir():
    """混合项目：每个 npm 工具链在其 package.json 目录 npm ci（烤 node_modules 进镜像层）。"""
    spec = EnvSpec(project_id="m1", toolchains=[
        Toolchain(name="java", build_tool="maven", dep_source="pom.xml"),
        Toolchain(name="node", build_tool="npm", dep_source="ruoyi-ui/package.json"),
    ])
    df = generate_dockerfile(spec, src_included=True)
    assert "cd /workspace/ruoyi-ui && (npm ci" in df  # 前端子目录
    assert "mvn -B -T 1C" in df                        # 后端 Maven warmup 仍在


def test_node_warmup_root_package_json():
    """根级 package.json → 在 /workspace 直接 npm ci。"""
    df = generate_dockerfile(
        EnvSpec(project_id="m2", toolchains=[Toolchain(name="node", build_tool="npm", dep_source="package.json")]),
        src_included=True,
    )
    assert "cd /workspace && (npm ci" in df


def test_node_warmup_skipped_without_src():
    """无源码(src_included=False)不做 npm warmup（warmup 依赖 COPY 的项目源码）。"""
    df = generate_dockerfile(
        EnvSpec(project_id="m3", toolchains=[Toolchain(name="node", build_tool="npm", dep_source="package.json")]),
        src_included=False,
    )
    assert "npm ci" not in df


# ══════════════════════════════════════════════
# X-C2（27 号文 §3.2 CRITICAL）：java 分支必须按 build_tool 分派
# ══════════════════════════════════════════════


def test_gradle_project_image_installs_gradle():
    """★X-C2★ 原实现 java 分支**只看 tc.name**、一律只装 maven，而 `_selftest_command`
    同一份 spec 上**是**按 build_tool 分派的（gradle → `./gradlew --offline classes ||
    gradle --offline classes`）⇒ Gradle 工程镜像里根本没有 gradle：无 wrapper 时自测 127、
    运行时任何 gradle 命令 127 → 与 X-C1 同型的 BLOCKED 死循环（每轮撞同一个"命令不存在"）。

    判据不对称就是病根：一处按 build_tool 分派、另一处不分派，两处必然分叉。
    """
    df = generate_dockerfile(
        EnvSpec(project_id="g1", toolchains=[
            Toolchain(name="java", version="17", build_tool="gradle",
                      dep_source="build.gradle")]),
        src_included=True)
    assert "gradle" in df, "Gradle 工程的镜像里没装 gradle ⇒ 任何 gradle 命令 127"
    # 装的是 apt 包而不只是出现在自测命令里
    assert "openjdk-17-jdk gradle" in df


def test_maven_project_image_unchanged():
    """★JVM 基线零回归★ maven 工程仍只装 maven（它是唯一跑过 E2E 的栈）。"""
    df = generate_dockerfile(
        EnvSpec(project_id="m9", toolchains=[
            Toolchain(name="java", version="17", build_tool="maven",
                      dep_source="pom.xml")]),
        src_included=True)
    assert "openjdk-17-jdk maven ca-certificates" in df
    assert "openjdk-17-jdk maven gradle" not in df


def test_java_without_build_tool_installs_both():
    """build_tool 缺失（老 spec / 探测不出）→ 保守两个都装。多装一个 apt 包的代价远小于
    127 死循环（fail-safe 方向：宁可镜像大一点，不要工具不在场）。"""
    df = generate_dockerfile(
        EnvSpec(project_id="j0", toolchains=[Toolchain(name="java", version="17")]),
        src_included=True)
    assert "maven gradle" in df


def test_mixed_maven_and_gradle_installs_both():
    """混编工程（同时有 pom 与 build.gradle）→ `infer_env_spec` 产两条 java toolchain，
    各装各的，两个都得在场。"""
    df = generate_dockerfile(
        EnvSpec(project_id="mx", toolchains=[
            Toolchain(name="java", version="17", build_tool="maven", dep_source="pom.xml"),
            Toolchain(name="java", version="17", build_tool="gradle",
                      dep_source="build.gradle")]),
        src_included=True)
    assert "openjdk-17-jdk maven ca-certificates" in df
    assert "openjdk-17-jdk gradle ca-certificates" in df


def test_gradle_warmup_present_and_wrapper_first():
    """★X-C2 配套：gradle warmup★ 缺它的话即便装了 gradle，依赖与插件仍未落盘 ⇒
    `_selftest_command` 的 `--offline classes` 必失败，且沙箱运行时每次都要联网拉依赖
    （离线/弱网沙箱里 L1 构建闸会假失败）。与 maven 填 .m2、npm 填 node_modules 同理。

    wrapper 优先——工程钉的 gradle 版本才是权威。
    """
    df = generate_dockerfile(
        EnvSpec(project_id="g2", toolchains=[
            Toolchain(name="java", build_tool="gradle", dep_source="build.gradle")]),
        src_included=True)
    assert "test -x ./gradlew && ./gradlew --no-daemon classes" in df, "wrapper 未优先"
    # ★两条臂必须**分别**断言★ 只断 `--offline --no-daemon classes -q` 这个子串不行——
    # wrapper 臂与系统 gradle 臂都含它，删掉任一条另一条仍让断言通过（零区分力，突变实测）。
    assert "test -x ./gradlew && ./gradlew --offline --no-daemon classes -q" in df, \
        "离线自检缺 wrapper 臂"
    assert "|| (gradle --offline --no-daemon classes -q)" in df, \
        "离线自检缺系统 gradle 兜底臂（无 wrapper 的 gradle 工程就没自检了）"
    assert "|| (gradle --no-daemon classes 2>&1 | tail -5)" in df, \
        "预热缺系统 gradle 兜底臂"
    assert "--daemon" not in df.replace("--no-daemon", ""), "不该起 daemon（会在镜像层留状态）"


def test_gradle_warmup_skipped_without_src():
    """无源码（src_included=False）不做 gradle warmup —— 与 maven/npm 侧同口径
    （warmup 依赖 COPY 进来的真项目源码）。"""
    df = generate_dockerfile(
        EnvSpec(project_id="g3", toolchains=[
            Toolchain(name="java", build_tool="gradle", dep_source="build.gradle")]),
        src_included=False)
    assert "gradlew" not in df and "classes" not in df


def test_maven_project_gets_no_gradle_warmup():
    """maven 工程不该出现 gradle warmup（别给 JVM 基线加无用构建层）。"""
    df = generate_dockerfile(
        EnvSpec(project_id="m8", toolchains=[
            Toolchain(name="java", build_tool="maven", dep_source="pom.xml")]),
        src_included=True)
    assert "gradlew" not in df


def test_selftest_and_install_dispatch_on_the_same_build_tools():
    """★X-C2 的根因是**判据不对称**，故直接锁"对称"这件事★

    病根不是"少装了 gradle"，而是 `_selftest_command` 按 `build_tool` 分派、
    `_toolchain_install` 不分派 —— 两处对同一份 spec 得出不同结论，必然分叉。
    本条枚举 `_selftest_command` 会分派的每个 (name, build_tool)，断言安装片段里真有那个工具。
    将来任何人给自测加一个新 build_tool 分支而忘了给安装片段加，本条即红。
    """
    from swarm.worker.image_builder import _selftest_command, _toolchain_install

    # (name, build_tool) → 安装片段里必须出现的工具名
    expect_tool = {
        ("java", "maven"): "maven",
        ("java", "gradle"): "gradle",
        ("node", "npm"): "nodejs",
        ("python", "pip"): "python3",
        ("go", "go"): "/usr/local/go",
        ("rust", "cargo"): "rustup",
    }
    for (name, bt), tool in expect_tool.items():
        tc = Toolchain(name=name, version="17" if name == "java" else None,
                       build_tool=bt, dep_source="x")
        st = _selftest_command(EnvSpec(project_id="s", toolchains=[tc]))
        assert st, f"({name},{bt}) 无自测命令——本表该更新了"
        install = _toolchain_install(tc)
        assert tool in install, (
            f"({name},{bt}) 的自测命令是 {st[:60]!r}，但安装片段里没有 {tool!r} ⇒ "
            f"命令必 127 → BLOCKED 死循环（X-C2 同型）")


def test_unknown_toolchain_is_observable():
    """未知工具链只留注释就无声无息（硬检查④：降级必须机读可辨/至少一次 WARNING）。
    这里至少锁住"注释里带得出名字"，好让镜像 Dockerfile 自身可诊断。"""
    from swarm.worker.image_builder import _toolchain_install

    out = _toolchain_install(Toolchain(name="elixir", build_tool="mix"))
    assert "elixir" in out and "未知工具链" in out
