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
from swarm.worker.image_builder import (_GRADLE_DEFAULT, generate_dockerfile,
                                        generate_maven_warmup_pom)


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
    # ★复核 H-3★ 不用 apt 的 gradle（Debian 稳定版是 4.x，跑不了 Java 17＝装了但不可用），
    # 照 go 分支成例钉发行版下载 + 软链进 PATH。
    assert "gradle-${GRADLE_VERSION}-bin.zip" in df, "没钉 gradle 发行版下载"
    # ★变量必须真被定义★ 只断 URL 里的 `${GRADLE_VERSION}` 字面量是零区分力的：删掉
    # `ENV GRADLE_VERSION=` 那行，URL 文本照旧在，而构建期展开成
    # `gradle--bin.zip` → 404 → 没装上 gradle → 127 死循环（突变实测该断言不红）。
    assert f"ENV GRADLE_VERSION={_GRADLE_DEFAULT}" in df, \
        "GRADLE_VERSION 未定义 ⇒ 下载 URL 展开成 gradle--bin.zip ⇒ 404 ⇒ 没装上"
    assert "ln -sf /opt/gradle-${GRADLE_VERSION}/bin/gradle /usr/local/bin/gradle" in df, \
        "gradle 没进 PATH ⇒ 命令仍 127"
    # ★复核 H-2★ gradle 是唯一没有依赖镜像的栈，而构建机网络受限（go.dev 实测被墙）
    assert "COPY warmup/init.gradle /root/.gradle/init.gradle" in df, "缺 gradle 镜像源"


def test_maven_project_image_unchanged():
    """★JVM 基线零回归★ maven 工程仍只装 maven（它是唯一跑过 E2E 的栈）。"""
    df = generate_dockerfile(
        EnvSpec(project_id="m9", toolchains=[
            Toolchain(name="java", version="17", build_tool="maven",
                      dep_source="pom.xml")]),
        src_included=True)
    assert "openjdk-17-jdk maven curl ca-certificates" in df
    assert "gradle" not in df, "maven 工程不该被搭上 gradle（JVM 基线零回归）"


def test_java_without_build_tool_installs_both():
    """build_tool 缺失（老 spec / 探测不出）→ 保守两个都装。多装一个 apt 包的代价远小于
    127 死循环（fail-safe 方向：宁可镜像大一点，不要工具不在场）。"""
    df = generate_dockerfile(
        EnvSpec(project_id="j0", toolchains=[Toolchain(name="java", version="17")]),
        src_included=True)
    # maven 走 apt、gradle 走钉版下载 ⇒ 两者都在场
    assert "openjdk-17-jdk maven curl" in df
    assert "gradle-${GRADLE_VERSION}-bin.zip" in df


def test_mixed_maven_and_gradle_installs_both():
    """混编工程（同时有 pom 与 build.gradle）→ `infer_env_spec` 产两条 java toolchain，
    各装各的，两个都得在场。"""
    df = generate_dockerfile(
        EnvSpec(project_id="mx", toolchains=[
            Toolchain(name="java", version="17", build_tool="maven", dep_source="pom.xml"),
            Toolchain(name="java", version="17", build_tool="gradle",
                      dep_source="build.gradle")]),
        src_included=True)
    assert "openjdk-17-jdk maven curl ca-certificates" in df
    assert "gradle-${GRADLE_VERSION}-bin.zip" in df


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
    # ★复核 H-1★ 判成败那一臂后面绝不能接管道——`cmd | tail` 的退出码是 tail 的（恒 0）⇒
    # `|| 系统 gradle` 兜底臂成死代码，而 wrapper 臂恰是"恒失败"的（C-2）⇒ warmup 每次
    # 看起来成功、~/.gradle 全空、且 `|| true` 让它完全静默。
    assert "|| (gradle --no-daemon classes > /tmp/gradle-warmup.log 2>&1)" in df, \
        "预热缺系统 gradle 兜底臂"
    assert "classes 2>&1 | tail" not in df, \
        "判成败的臂后面接了管道 ⇒ 退出码被 tail 吞掉 ⇒ 兜底臂是死代码"
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


def test_selftest_and_install_read_the_same_registry():
    """★X-C2 的根因是**同一件事有两张表**，故锁"只有一张表"★

    病根不是"少装了 gradle"：`_selftest_command` 按 build_tool 分派、`_toolchain_install`
    不分派，两处对同一份 spec 得出不同结论 ⇒ 自测发 gradle 命令而镜像只装 maven ⇒ 127。

    ★复核 H-4 的整改★ 本条原先枚举一张**手写** `expect_tool` 表——实测给
    `_selftest_command` 加一个 `("java","sbt")` 分支而不动安装片段，测试**照旧全绿**：
    它只锁反向、不锁正向，而那张手写表本身就是第二套真相源（正是它要防的那个形态）。
    现在两个函数同读 `_STACK_REGISTRY`，本条改为**从 registry 派生**遍历。
    """
    from swarm.worker.image_builder import (_STACK_REGISTRY, _selftest_command,
                                            _toolchain_install)

    assert _STACK_REGISTRY, "registry 空了？"
    for (name, bt), entry in _STACK_REGISTRY.items():
        tc = Toolchain(name=name, version="17" if name == "java" else None,
                       build_tool=bt, dep_source="x")
        st = _selftest_command(EnvSpec(project_id="s", toolchains=[tc]))
        assert st == entry.selftest, (
            f"({name},{bt}) 的自测命令不是从 registry 取的 ⇒ 又出现第二套表")
        install = _toolchain_install(tc)
        # ★复核 CRITICAL-2★ 不能只断"工具名出现在片段里"——`_toolchain_install` 对未收录栈
        # 返回的是 `# (未知工具链 elixir，跳过)`，**注释里就带着那个名字** ⇒ 断言被注释满足＝
        # 零区分力（实测：往 registry 塞 ("elixir","mix") 后该断言照旧通过，而镜像里啥也没装）。
        # 改为结构性判据：片段必须真的有 RUN/ENV 指令，且工具名出现在**非注释行**上。
        assert "未知工具链" not in install, (
            f"({name},{bt}) 在 registry 里却走了『未知工具链』分支 ⇒ 镜像啥也不装 ⇒ 127")
        effective = "\n".join(ln for ln in install.splitlines()
                              if ln.strip() and not ln.lstrip().startswith("#"))
        assert any(ln.startswith(("RUN ", "ENV ", "COPY ")) for ln in effective.splitlines()), (
            f"({name},{bt}) 安装片段没有任何 RUN/ENV/COPY 指令 ⇒ 什么都没装")
        # ★27 号文 #8★ 这里原是 `for tool in entry.apt_packages: assert tool in effective`，
        # 实测 6 个组合里 2 条**必然假过**、1 条侥幸（详见 image_builder.py `_StackEntry` 上方）：
        # go 声明 `/usr/local/go` 而唯一命中是 `ENV PATH=…/usr/local/go/bin…`（删掉整条
        # `curl|tar` 安装照旧绿）；gradle 声明 `gradle` 命中 6 行、5 行是 `/root/.gradle`
        # 之类顺带命中。病根：声明是数据，而"验证"只是让文本里出现一个子串，子串到处都是。
        # 改为断言 registry 的 `verify_cmd` 作为**构建期硬闸**真被接上。
        # ★这条测试证的是"接线"，不是"工具真装上了"★——后者由镜像构建时 docker 跑这条
        # `RUN` 来证（装不上即构建失败）。区分力：把任一 `_verify_block(...)` 调用拧掉，
        # 该栈这条立刻红（原断言拧掉真安装都不红）。
        assert entry.verify_cmd, (
            f"({name},{bt}) registry 没写 verify_cmd ⇒ 该栈没有构建期在场硬闸 ⇒ "
            f"装不上要等到运行时 127 → BLOCKED 死循环（X-C2 同型）")
        assert f"RUN {entry.verify_cmd}" in install, (
            f"({name},{bt}) registry 声明硬闸 {entry.verify_cmd!r}，但安装片段里没接上 ⇒ "
            f"该栈的『装没装上』又退回运行时才发现")
        for ln in install.splitlines():
            if ln.startswith(f"RUN {entry.verify_cmd}"):
                assert "||" not in ln.split(entry.verify_cmd, 1)[1], (
                    f"({name},{bt}) 在场硬闸被软化成软诊断（挂了 `||`）: {ln}")


def test_new_build_tool_in_registry_is_installed_and_selftested():
    """★正向区分力（H-4 的真判据）★ 往 registry 加一个新 (name, build_tool)，两个函数都必须
    立刻认它——这才证明"改一处两处都动"。原实现下这条不可能通过（两张表各写各的）。"""
    from swarm.worker import image_builder as ib

    tc = Toolchain(name="java", version="17", build_tool="sbt", dep_source="build.sbt")
    # 加之前：registry 没有它 ⇒ 无自测（fail-honest，不臆造命令）
    assert _sel(ib, tc) is None
    ib._STACK_REGISTRY[("java", "sbt")] = ib._StackEntry(
        "sbt -version", "cd /workspace && sbt -batch compile")
    try:
        assert _sel(ib, tc) == "cd /workspace && sbt -batch compile", \
            "registry 里加了自测命令，_selftest_command 却没认 ⇒ 两张表又分叉了"
    finally:
        ib._STACK_REGISTRY.pop(("java", "sbt"), None)


def _sel(ib, tc):
    from swarm.project.sandbox_spec import EnvSpec as _E
    return ib._selftest_command(_E(project_id="s", toolchains=[tc]))


def test_unknown_toolchain_is_observable():
    """未知工具链只留注释就无声无息（硬检查④：降级必须机读可辨/至少一次 WARNING）。
    这里至少锁住"注释里带得出名字"，好让镜像 Dockerfile 自身可诊断。"""
    from swarm.worker.image_builder import _toolchain_install

    out = _toolchain_install(Toolchain(name="elixir", build_tool="mix"))
    assert "elixir" in out and "未知工具链" in out


def test_builder_version_bumped_so_old_images_are_invalidated():
    """★复核 C-1★ `_BUILDER_VERSION` 是"构建逻辑变了"的**唯一**信号：
    `compute_project_fingerprint = f"v{_BUILDER_VERSION}-{deps_hash}-{dep_hash}"`。
    gradle 工程的 `deps_hash`（来自 sandbox_spec）与 `build.gradle` 内容都没变 ⇒ 不递增它，
    `_phase_build_sandbox` 就走"依赖+源码未变且模板存在 → 复用专属模板"⇒ **老的没装 gradle 的
    镜像继续被复用**，X-C2 的修复一行都到不了生产。

    ★27 号文 #8 的守卫形状整改★ 本条原来断的是 `int(_BUILDER_VERSION) >= 8`——**下界断言是
    永绿的**：递增到 9/10 它照旧过，而它要防的恰恰是"改了生成逻辑却**不**递增"，那种情况下
    版本号停在 8、`>= 8` 依然成立 ⇒ 这条守卫对它要防的事零区分力。同型缺陷在 P-C3 的
    `_STACK_SCHEMA_VERSION >= 3` 上已实证过一次，属同一个"缓存键不含代码版本"族。
    正确形状＝把**被缓存内容的摘要**与版本常量钉成一对：生成物变了摘要必变，两者必须同时改。
    """
    import hashlib

    from swarm.worker.image_builder import (_BUILDER_VERSION, _STACK_REGISTRY,
                                            _toolchain_install)

    parts = []
    for (nm, bt), e in sorted(_STACK_REGISTRY.items()):
        tc = Toolchain(name=nm, version="17" if nm == "java" else None,
                       build_tool=bt, dep_source="x")
        parts.append(f"{nm}|{bt}|{e.verify_cmd}|{e.selftest}|{_toolchain_install(tc)}")
    # build_tool 缺失（java 两个都装）也是一种生成形态，必须进摘要
    parts.append("java|<none>|" + _toolchain_install(
        Toolchain(name="java", version="17", build_tool=None, dep_source="x")))
    digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]

    assert (_BUILDER_VERSION, digest) == ("9", "68483ed7a0bc940a"), (
        f"镜像生成物或 registry 变了（当前摘要 {digest}，版本 {_BUILDER_VERSION}）。\n"
        "这不是让你改数字对付过去：**必须递增 `_BUILDER_VERSION` 并同步更新本条的摘要**。\n"
        "只改摘要不递增版本 ⇒ `compute_project_fingerprint` 不变 ⇒ 已有专属镜像模板继续被复用 ⇒ "
        "改动一行都到不了生产（X-C2 复核 C-1 的原始事故就是这个）。")


def test_wrapper_jars_survive_source_tarball(tmp_path):
    """★复核 C-2★ `.jar` 的排除会连**构建工具自己的 wrapper jar** 一起剥掉：
    `gradle/wrapper/gradle-wrapper.jar` 是 `./gradlew` 的全部实现。剥掉后镜像里
    脚本在、jar 不在 ⇒ `./gradlew` 报 `找不到或无法载入主要类别
    org.gradle.wrapper.GradleWrapperMain` ⇒ 而 L1 的 `_derive_full_build_command` 见到
    `gradlew` 就发 `./gradlew -q classes`、**没有 `|| gradle` 兜底** ⇒ 每轮硬失败 → BLOCKED
    → 重试 → 同样失败。127 换成 ClassNotFound，死循环不变。
    `.mvn/wrapper/maven-wrapper.jar` 同型（X-C1 同族，此前潜伏）。
    """
    import tarfile

    from swarm.worker.image_builder import _make_source_tarball

    (tmp_path / "gradle" / "wrapper").mkdir(parents=True)
    (tmp_path / ".mvn" / "wrapper").mkdir(parents=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "gradle" / "wrapper" / "gradle-wrapper.jar").write_bytes(b"PK\x03\x04g")
    (tmp_path / "gradle" / "wrapper" / "gradle-wrapper.properties").write_text(
        "distributionUrl=https\\://x/gradle-8.7-bin.zip\n")
    (tmp_path / ".mvn" / "wrapper" / "maven-wrapper.jar").write_bytes(b"PK\x03\x04m")
    (tmp_path / ".mvn" / "wrapper" / "maven-wrapper.properties").write_text(
        "distributionUrl=https\\://x/apache-maven-3.9.6-bin.zip\n")
    (tmp_path / ".mvn" / "maven.config").write_text("-T 1C\n")
    (tmp_path / ".mvn" / "jvm.config").write_text("-Xmx2g\n")
    (tmp_path / "libs").mkdir()
    (tmp_path / "libs" / "vendored.jar").write_bytes(b"PK\x03\x04v")   # 真构建产物，仍该剥
    (tmp_path / "build.gradle").write_text("plugins { id 'java' }\n")
    (tmp_path / "src" / "A.java").write_text("class A {}\n")

    # ★归一只剥前导 `./`★ 用 `lstrip("./")` 会把 `.mvn/...` 剥成 `mvn/...`，于是断言看起来
    # 失败而其实 tarball 是对的（本会话第三次撞上同一个惯用法陷阱：生产两处 + 这条测试）。
    def _norm(n: str) -> str:
        while n.startswith("./"):
            n = n[2:]
        return n.lstrip("/")

    with tarfile.open(fileobj=__import__("io").BytesIO(_make_source_tarball(tmp_path))) as t:
        names = {_norm(n) for n in t.getnames()}
    assert "gradle/wrapper/gradle-wrapper.jar" in names, "gradlew 的 jar 被剥了 ⇒ ./gradlew 必崩"
    assert ".mvn/wrapper/maven-wrapper.jar" in names, "mvnw 的 jar 被剥了 ⇒ ./mvnw 必崩"
    # ★复核 HIGH-1★ 只留 jar **不够**：`./mvnw` 启动时要读 properties 取 `distributionUrl`，
    # 而 `.mvn` 整目录被 `_SRC_EXCLUDE_DIRS` 排除 ⇒ jar 留下了、properties 还是没了 ⇒ 照旧起不来。
    # `maven.config`/`jvm.config` 是**工程钉的构建参数**（`-T 1C` / `-Xmx`），丢了会让镜像里的
    # 构建行为与工程作者意图不一致（内存不够直接 OOM）。
    for need in (".mvn/wrapper/maven-wrapper.properties", ".mvn/maven.config",
                 ".mvn/jvm.config", "gradle/wrapper/gradle-wrapper.properties"):
        assert need in names, f"{need} 被剥了 ⇒ wrapper/构建参数不完整"
    assert "libs/vendored.jar" not in names, "普通 jar 仍该按构建产物剥掉（别把排除整类放宽）"


def test_gradle_warmup_cleans_build_dir():
    """★复核 H-5★ `chmod -R 0777 /workspace` 在 warmup **之前**跑，warmup 留下的 `build/`
    与 `.gradle/` 是 root 所有 + 默认权限；而 worker 可能以非 root 跑 gradle ⇒
    "could not create parent directories / Permission denied 编译失败"（本文件自己的注释
    就是在说这个）。maven 侧两条 RUN 都清了 `target/`，gradle 侧必须对称。"""
    df = generate_dockerfile(
        EnvSpec(project_id="g4", toolchains=[
            Toolchain(name="java", build_tool="gradle", dep_source="build.gradle")]),
        src_included=True)
    assert "-name build -prune -exec rm -rf {} +" in df, "gradle warmup 没清 build/"
    assert "rm -rf /workspace/.gradle" in df, "gradle warmup 没清 .gradle/（root 所有）"


def test_gradle_init_script_uploaded_when_gradle_present():
    """Dockerfile 里有 `COPY warmup/init.gradle`，**不传这个文件就是构建失败**。
    故上传判据与生成判据必须同源（都走 `_has_build_tool`）。"""
    from swarm.worker.image_builder import _GRADLE_INIT, _has_build_tool

    spec = EnvSpec(project_id="g5", toolchains=[
        Toolchain(name="java", build_tool="gradle", dep_source="build.gradle")])
    df = generate_dockerfile(spec, src_included=True)
    assert "COPY warmup/init.gradle" in df
    assert _has_build_tool(spec, "gradle") is True, "上传闸判不出 gradle ⇒ COPY 缺文件 ⇒ 构建失败"
    assert "aliyun" in _GRADLE_INIT and "pluginManagement" in _GRADLE_INIT


def test_has_build_tool_single_normalization():
    """★复核 L-1★ 同一字段两种归一（`== "maven"` vs `(x or "").lower()`）正是本批在治的
    "判据不对称"。统一走 `_has_build_tool`，且 build_tool 未定的 java 对两者都算"有"——
    因为安装片段给它装了 maven+gradle，warmup/settings 不能两头落空（L-2）。"""
    from swarm.worker.image_builder import _has_build_tool

    undetermined = EnvSpec(project_id="u", toolchains=[Toolchain(name="java", version="17")])
    assert _has_build_tool(undetermined, "maven") is True
    assert _has_build_tool(undetermined, "gradle") is True
    assert _has_build_tool(undetermined, "npm") is False
    assert _has_build_tool(
        EnvSpec(project_id="n", toolchains=[Toolchain(name="node", build_tool="NPM")]),
        "npm") is True, "大小写归一失效"


def test_undetermined_java_still_gets_warmup_and_settings():
    """★复核 L-2★ build_tool 未定的 java：装了两个工具却"两个缓存都没填、也没自测"＝
    半接线。至少 maven 侧的 settings/warmup 与自测要在（有 pom 就真编译，无 pom 软失败）。"""
    spec = EnvSpec(project_id="u2", toolchains=[Toolchain(name="java", version="17")])
    df = generate_dockerfile(spec, src_included=True)
    assert "COPY warmup/settings.xml" in df, "未定 build_tool 的 java 连 settings 都没有"
    assert "mvn -B -T 1C" in df, "未定 build_tool 的 java 没有任何 warmup"
    from swarm.worker.image_builder import _selftest_command
    assert _selftest_command(spec), "未定 build_tool 的 java 连自测都没有（零构建期验证）"


# ══════════════════════════════════════════════
# X-C2 CRITICAL-1（治法 D）：harness 命令不适用 → L1 按真实清单改派
# ══════════════════════════════════════════════


def test_harness_maven_command_overridden_on_gradle_project(tmp_path, monkeypatch):
    """★复核 CRITICAL-1（治法 D，用户拍板）★ `_infer_harness` 对整个 JVM 族恒发
    `mvn -q compile`（那一层只有 task_description + scope，**拿不到工程事实**）⇒ gradle 工程
    收到 mvn 命令 → `_build_cmd_applicable` 为 False → build 闸**整块跳过** ⇒ 零构建闸。
    而 `_derive_full_build_command` 的判据是真实清单，实测能给出 `./gradlew -q classes`。
    """
    import swarm.worker.l1_pipeline as lp
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskHarness

    (tmp_path / "build.gradle").write_text("plugins { id 'java' }\n")
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    (tmp_path / "gradlew").chmod(0o755)
    src = tmp_path / "src" / "main" / "java"
    src.mkdir(parents=True)
    (src / "A.java").write_text("class A {}\n")

    # 先证前提：harness 的 mvn 命令对本工程确实不适用（否则本条测的是别的命题）
    assert lp._build_cmd_applicable("mvn -q compile", str(tmp_path)) is False
    assert lp._derive_full_build_command(
        str(tmp_path), ["src/main/java/A.java"], None) == "./gradlew -q classes"

    seen: list[str] = []
    monkeypatch.setattr(lp, "_run_l1_command",
                        lambda cmd, pp, timeout=120: (seen.append(cmd), (0, ""))[1])
    monkeypatch.setattr(lp, "_compile_files", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(lp, "_run_check_split", lambda *a, **k: (1, "", ""))
    monkeypatch.setenv("SWARM_WORKER_L1_LINT", "false")
    monkeypatch.setenv("SWARM_WORKER_L1_FORMAT", "false")
    import swarm.worker.workspace_manifest as wm
    monkeypatch.setattr(wm, "reconcile_workspace_manifests",
                        lambda *a, **k: {"modified_manifests": [], "added": []})

    diff = ("--- a/src/main/java/A.java\n+++ b/src/main/java/A.java\n"
            "@@ -1 +1 @@\n-old\n+new\n")
    st = SubTask(id="st-d", description="gradle", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["src/main/java/A.java"]),
                 harness=TaskHarness(language="java", build_command="mvn -q compile"))
    _ok, details = lp.run_l1_pipeline(str(tmp_path), st, diff, timeout=30)

    assert details.get("build_command") == "./gradlew -q classes", \
        f"gradle 工程仍在跑 harness 的 mvn 命令（或闸被跳过）: {details.get('build_command')}"
    assert details.get("build_command_overridden"), "改派没留痕（闸跑的是哪条命令无从追溯）"
    assert any("gradlew" in c for c in seen), f"实际执行的命令里没有 gradlew: {seen}"


def test_harness_maven_command_untouched_on_maven_project(tmp_path, monkeypatch):
    """★maven 零回归（治法 D 的立论）★ `mvn` 对有 pom 的工程 applicable 恒 True ⇒ 第二个
    条件恒假 ⇒ derive 不被调用 ⇒ 路径逐字节不变，`build_command_derived`/`_overridden`
    都不该置位。这是唯一跑过 E2E 的栈，必须钉死。"""
    import swarm.worker.l1_pipeline as lp
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskHarness

    (tmp_path / "pom.xml").write_text(
        "<project><groupId>g</groupId><artifactId>a</artifactId>"
        "<version>1</version></project>\n")
    src = tmp_path / "src" / "main" / "java"
    src.mkdir(parents=True)
    (src / "A.java").write_text("class A {}\n")

    assert lp._build_cmd_applicable("mvn -q compile", str(tmp_path)) is True

    monkeypatch.setattr(lp, "_run_l1_command", lambda cmd, pp, timeout=120: (0, ""))
    monkeypatch.setattr(lp, "_compile_files", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(lp, "_enforce_parent_version_literals", lambda *a, **k: (0, []))
    monkeypatch.setattr(lp, "_enforce_dep_legality", lambda *a, **k: (0, []))
    monkeypatch.setenv("SWARM_WORKER_L1_LINT", "false")
    monkeypatch.setenv("SWARM_WORKER_L1_FORMAT", "false")
    import swarm.worker.workspace_manifest as wm
    monkeypatch.setattr(wm, "reconcile_workspace_manifests",
                        lambda *a, **k: {"modified_manifests": [], "added": []})

    diff = ("--- a/src/main/java/A.java\n+++ b/src/main/java/A.java\n"
            "@@ -1 +1 @@\n-old\n+new\n")
    st = SubTask(id="st-m", description="maven", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["src/main/java/A.java"]),
                 harness=TaskHarness(language="java", build_command="mvn -q compile"))
    _ok, details = lp.run_l1_pipeline(str(tmp_path), st, diff, timeout=30)

    assert "build_command_overridden" not in details, "maven 工程被改派了 ⇒ JVM 基线回归"
    assert "build_command_derived" not in details, "maven 工程走了 derive 路径 ⇒ 语义面变了"
    assert (details.get("build_command") or "").startswith("mvn"), \
        f"maven 工程的构建命令被换掉了: {details.get('build_command')}"


def test_gradle_build_time_verification():
    """★复核 MED-3★ 钉版下载那串是 `curl A || curl B`；两个源都失败时后面的 unzip 会拿着一个
    空/半截文件继续，而**镜像照样发布成功** ⇒ 运行时才 127，回到本批要治的死循环。
    `RUN gradle -v`（无 `|| true`）把"装没装上"从运行时提前到构建期。"""
    df = generate_dockerfile(
        EnvSpec(project_id="gv", toolchains=[
            Toolchain(name="java", build_tool="gradle", dep_source="build.gradle")]),
        src_included=True)
    assert "RUN gradle -v" in df, "缺构建期硬闸 ⇒ 下载失败也会发布出一个没有 gradle 的镜像"
    # 必须是硬闸：后面不能挂 `|| true` / `|| echo`
    for ln in df.splitlines():
        if ln.startswith("RUN gradle -v"):
            assert "||" not in ln, f"构建期验证被软化成软诊断: {ln}"


def test_gradle_warmup_observability():
    """★复核 MED-4★ warmup 是否填上了 `~/.gradle` 只能靠这条日志判断：5 行装不下 gradle 的
    失败摘要（`* What went wrong` / `* Try:` 好几段），且没有机读键时无法机器判读。"""
    df = generate_dockerfile(
        EnvSpec(project_id="gw", toolchains=[
            Toolchain(name="java", build_tool="gradle", dep_source="build.gradle")]),
        src_included=True)
    assert "tail -40" in df, "日志尾太短，装不下 gradle 的失败摘要"
    assert "[swarm:gradle-warmup=failed]" in df, "warmup 失败无机读标记"
    assert ("[swarm:gradle-offline=ok]" in df
            and "[swarm:gradle-offline=degraded]" in df), \
        "离线自检结果无机读标记（两个方向都要有，否则只能看出一半）"
    # 标记不许用 `SWARM_*=` 形态：那是 config/env_registry 的配置开关命名空间
    assert "SWARM_GRADLE" not in df, "日志标记占用了配置开关命名空间（会被登记闸判成未登记）"
