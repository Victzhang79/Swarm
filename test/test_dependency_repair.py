#!/usr/bin/env python3
"""缺第三方依赖声明的确定性兜底（防线④ dep-repair）回归测试。

治本背景（996db614 实测 package-does-not-exist ~137/213 头号）：worker import 了第三方库
（jjwt/redis/fastjson2/hutool…）但 module pom 没声明 → `package P does not exist` → 整文件编不过
→ 下游 cannot-find-symbol 级联 → 死循环。import-repair 明确不碰它（缺依赖≠前缀错）。

本套用【真实临时 Maven 树 + 本地 grep/perl】（无沙箱时 _run_*命令回退本地 subprocess），只 mock
网络部分（Central 反查坐标 + maven-metadata 版本），端到端验证注入正确、且三类该跳的都跳：
  ① 项目自有 group 前缀（内部包未就绪 ②）不动；② 别人子任务的文件不动；③ 臆造类(查无坐标)不动。
"""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import swarm.worker.l1_pipeline as l1  # noqa: E402


_PARENT_POM = """<?xml version="1.0"?>
<project>
  <groupId>com.example</groupId>
  <artifactId>demo-parent</artifactId>
  <version>1.0.0</version>
  <packaging>pom</packaging>
  <modules><module>modA</module></modules>
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>com.alibaba</groupId>
        <artifactId>fastjson</artifactId>
        <version>1.2.83</version>
      </dependency>
    </dependencies>
  </dependencyManagement>
</project>
"""

_MODULE_POM = """<?xml version="1.0"?>
<project>
  <parent>
    <groupId>com.example</groupId>
    <artifactId>demo-parent</artifactId>
    <version>1.0.0</version>
  </parent>
  <artifactId>modA</artifactId>
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter</artifactId>
    </dependency>
  </dependencies>
</project>
"""


def _make_project(d: Path, svc_imports: str) -> str:
    (d / "pom.xml").write_text(_PARENT_POM)
    mod = d / "modA"
    (mod).mkdir()
    (mod / "pom.xml").write_text(_MODULE_POM)
    src = mod / "src/main/java/com/example/a"
    src.mkdir(parents=True)
    (src / "Svc.java").write_text(
        f"package com.example.a;\n{svc_imports}\npublic class Svc {{}}\n"
    )
    return "modA/src/main/java/com/example/a/Svc.java"


def _patch_network(monkey_g_a, monkey_versions):
    """mock 仅网络部分：Central 反查 + 版本。返回还原器。"""
    orig_resolve = l1._resolve_artifact_via_central
    orig_ver = l1._fetch_maven_versions
    l1._resolve_artifact_via_central = lambda fqcn, pkg, pp, to: monkey_g_a
    l1._fetch_maven_versions = lambda g, a, pp, to: monkey_versions

    def restore():
        l1._resolve_artifact_via_central = orig_resolve
        l1._fetch_maven_versions = orig_ver
    return restore


# ── 端到端：缺 jjwt → 反查 io.jsonwebtoken:jjwt-api → 注入 module pom（含版本）──

def test_dep_repair_injects_missing_thirdparty():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        rel = _make_project(d, "import io.jsonwebtoken.Jwts;")
        build_out = (
            f"[ERROR] {rel}:[2,20] package io.jsonwebtoken does not exist\n"
        )
        restore = _patch_network(("io.jsonwebtoken", "jjwt-api"), ["0.11.5", "0.12.6"])
        try:
            n, poms = l1._attempt_dependency_repair(str(d), build_out, [rel], timeout=30)
            assert n == 1, (n, poms)
            assert poms == ["modA/pom.xml"]
            pom_txt = (d / "modA/pom.xml").read_text()
            assert "<artifactId>jjwt-api</artifactId>" in pom_txt
            assert "<groupId>io.jsonwebtoken</groupId>" in pom_txt
            assert "<version>0.12.6</version>" in pom_txt  # 取最新可用版
            # 注入在常规 <dependencies> 块内（spring-boot-starter 仍在）
            assert "spring-boot-starter" in pom_txt
        finally:
            restore()
    print("  ✅ 缺第三方依赖 → Central 反查坐标 → 注入 module pom（含最新版）")


# ── ① artifact 家族补全：jjwt-api 编译够，但运行时需 jjwt-impl/jjwt-jackson（runtime scope）──

def test_dep_repair_injects_runtime_companions():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        rel = _make_project(d, "import io.jsonwebtoken.Jwts;")
        build_out = f"[ERROR] {rel}:[2,20] package io.jsonwebtoken does not exist\n"
        restore = _patch_network(("io.jsonwebtoken", "jjwt-api"), ["0.12.6"])
        orig_fam = l1._resolve_artifact_family
        l1._resolve_artifact_family = lambda g, a, pp, to: ["jjwt-impl", "jjwt-jackson"]
        try:
            n, poms = l1._attempt_dependency_repair(str(d), build_out, [rel], timeout=30)
            assert n == 1, (n, poms)
            pom = (d / "modA/pom.xml").read_text()
            # 主件 compile（无 scope）
            assert "<artifactId>jjwt-api</artifactId>" in pom
            # 运行时伴生件：runtime scope
            assert "<artifactId>jjwt-impl</artifactId>" in pom
            assert "<artifactId>jjwt-jackson</artifactId>" in pom
            assert pom.count("<scope>runtime</scope>") == 2, "两个伴生件都应 runtime scope"
            assert pom.count("<version>0.12.6</version>") == 3, "家族同版本"
        finally:
            l1._resolve_artifact_family = orig_fam
            restore()
    print("  ✅ ① artifact 家族：jjwt-api + jjwt-impl/jackson(runtime,同版本)")


def test_resolve_family_base_and_suffix_logic():
    # 纯逻辑：mock Central g: 查询返回同 group 全部 artifact
    import json as _json
    docs = {"response": {"docs": [
        {"a": "jjwt-api"}, {"a": "jjwt-impl"}, {"a": "jjwt-jackson"},
        {"a": "jjwt-gson"}, {"a": "jjwt-root"},  # gson 不取(避免双 JSON 绑定)、root 是 BOM
    ]}}
    orig = l1._run_l1_command
    l1._run_l1_command = lambda cmd, pp, timeout=120: (0, _json.dumps(docs))
    try:
        fam = l1._resolve_artifact_family("io.jsonwebtoken", "jjwt-api", "/x", 30)
        assert fam == ["jjwt-impl", "jjwt-jackson"], fam  # 顺序按后缀约定，无 gson/root
        # 主件非 -api/-core → 不猜伴生件
        assert l1._resolve_artifact_family("com.x", "plainlib", "/x", 30) == []
    finally:
        l1._run_l1_command = orig
    print("  ✅ 家族解析：去 -api 基名 + 运行时后缀过滤（排 gson/root）")


# ── 受管依赖（父 dependencyManagement 有）→ 注入【无 version】继承 ──

def test_dep_repair_managed_no_version():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        rel = _make_project(d, "import com.alibaba.fastjson.JSON;")
        build_out = f"[ERROR] {rel}:[2,20] package com.alibaba.fastjson does not exist\n"
        # Central 反查到 com.alibaba:fastjson（父受管），版本不该被用到
        restore = _patch_network(("com.alibaba", "fastjson"), ["1.2.99"])
        try:
            n, poms = l1._attempt_dependency_repair(str(d), build_out, [rel], timeout=30)
            assert n == 1, (n, poms)
            pom_txt = (d / "modA/pom.xml").read_text()
            assert "<artifactId>fastjson</artifactId>" in pom_txt
            # 受管 → 无 version（继承父 dependencyManagement），不得注入 1.2.99
            assert "1.2.99" not in pom_txt
        finally:
            restore()
    print("  ✅ 受管依赖 → 注入无 version 继承（不双 version）")


# ── ② 项目自有 group 前缀（内部包未就绪）→ 不当缺依赖，绝不注入 ──

def test_dep_repair_skips_own_group_internal_pkg():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        rel = _make_project(d, "import com.example.b.dto.FooDto;")
        build_out = f"[ERROR] {rel}:[2,20] package com.example.b.dto does not exist\n"
        # 即便 mock 了坐标，也必须因 own-group 在反查前就跳过
        restore = _patch_network(("com.example", "modB"), ["1.0.0"])
        try:
            n, poms = l1._attempt_dependency_repair(str(d), build_out, [rel], timeout=30)
            assert n == 0 and poms == [], (n, poms)
            assert "modB" not in (d / "modA/pom.xml").read_text()
        finally:
            restore()
    print("  ✅ 项目自有 group（内部包未就绪②）→ 不注入（交依赖拓扑）")


# ── 别人子任务的文件 → 不动（配合文件级归属）──

def test_dep_repair_skips_other_subtask_file():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        rel = _make_project(d, "import io.jsonwebtoken.Jwts;")
        build_out = f"[ERROR] {rel}:[2,20] package io.jsonwebtoken does not exist\n"
        restore = _patch_network(("io.jsonwebtoken", "jjwt-api"), ["0.12.6"])
        try:
            # 本子任务改的是别的文件，没改 Svc.java
            n, poms = l1._attempt_dependency_repair(
                str(d), build_out, ["modA/src/main/java/com/example/a/Other.java"], timeout=30
            )
            assert n == 0 and poms == [], (n, poms)
            assert "jjwt" not in (d / "modA/pom.xml").read_text()
        finally:
            restore()
    print("  ✅ 别人子任务的文件 → 不动（文件级归属）")


# ── 臆造的类（Central 查无 prefix 匹配坐标）→ 不乱注入 ──

def test_dep_repair_skips_hallucinated_pkg():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        rel = _make_project(d, "import org.fictional.lib.MagicThing;")
        build_out = f"[ERROR] {rel}:[2,20] package org.fictional.lib does not exist\n"
        restore = _patch_network(None, [])  # 反查返回 None（无 artifact 提供该类）
        try:
            n, poms = l1._attempt_dependency_repair(str(d), build_out, [rel], timeout=30)
            assert n == 0 and poms == [], (n, poms)
            assert "fictional" not in (d / "modA/pom.xml").read_text()
        finally:
            restore()
    print("  ✅ 臆造包(坐标查无) → 不乱注入")


# ── JDK / servlet 命名空间 → 不当缺依赖 ──

def test_dep_repair_skips_jdk_namespace():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        rel = _make_project(d, "import javax.servlet.http.HttpServletRequest;")
        build_out = f"[ERROR] {rel}:[2,20] package javax.servlet.http does not exist\n"
        called = {"resolve": False}

        def _spy(fqcn, pkg, pp, to):
            called["resolve"] = True
            return ("x", "y")
        orig = l1._resolve_artifact_via_central
        l1._resolve_artifact_via_central = _spy
        try:
            n, poms = l1._attempt_dependency_repair(str(d), build_out, [rel], timeout=30)
            assert n == 0 and not called["resolve"], "javax.* 应在反查前就跳过"
        finally:
            l1._resolve_artifact_via_central = orig
    print("  ✅ javax/servlet 命名空间 → 不当缺依赖（交命名空间防线）")


# ── 回归：第三方 group 出现在 pom（依赖声明）但不在源码 → 绝不当"项目自有"──
# Bug：旧 _project_own_groups 据 pom <groupId> ≥2 pom 判自有 → com.alibaba/org.springframework
# 等第三方依赖 group 在多 pom 现身被误判自有 → 缺 fastjson2 被当内部包误 BLOCKED 还不补依赖。
# 治本：据源码 package 声明判自有。本测钉死：pom 里声明了 com.alibaba 依赖、但源码只声明 com.example
# → own={com.example}，com.alibaba.fastjson2 缺包【不】被当自有，dep-repair 照常补。

def test_thirdparty_group_in_pom_not_treated_as_own():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        rel = _make_project(d, "import com.alibaba.fastjson2.JSON;")
        # 往两个 pom 都塞 com.alibaba 依赖声明（模拟父 dManagement + 模块 deps 都现身）
        for pf in (d / "pom.xml", d / "modA/pom.xml"):
            t = pf.read_text().replace(
                "</project>",
                "  <dependencies><dependency><groupId>com.alibaba</groupId>"
                "<artifactId>fastjson</artifactId></dependency></dependencies>\n</project>")
            pf.write_text(t)
        own = l1._project_own_packages(str(d), 30)
        assert "com.example" in own, f"源码声明的 com.example 应是自有: {own}"
        assert "com.alibaba" not in own, f"第三方 com.alibaba(仅 pom 依赖,无源码)不应判自有: {own}"
        # 端到端：fastjson2 缺包 → 不被当内部包 BLOCKED，dep-repair 照常补
        build_out = f"[ERROR] {rel}:[2,20] package com.alibaba.fastjson2 does not exist\n"
        assert l1._build_blocked_on_unbuilt_internal(str(d), build_out, 30) == set(), \
            "第三方 fastjson2 缺包不应被误判为②内部包未就绪"
        restore = _patch_network(("com.alibaba", "fastjson2"), ["2.0.51"])
        try:
            n, poms = l1._attempt_dependency_repair(str(d), build_out, [rel], timeout=30)
            assert n == 1 and "fastjson2" in (d / "modA/pom.xml").read_text(), (n, poms)
        finally:
            restore()
    print("  ✅ 第三方 group 在 pom 不在源码 → 不判自有，dep-repair 照常补（回归 bug 已治）")


# ── _resolve_artifact_via_central 的 groupId 前缀过滤（纯逻辑，mock JSON）──

def test_resolve_central_groupid_prefix_filter():
    import json as _json
    docs = {
        "response": {"docs": [
            {"g": "com.unrelated", "a": "wrong-lib"},        # groupId 非 pkg 前缀 → 拒
            {"g": "io.jsonwebtoken", "a": "jjwt-bom"},        # 前缀匹配但 bom → 降权
            {"g": "io.jsonwebtoken", "a": "jjwt-api"},        # 前缀匹配实体 → 选它
        ]}
    }
    orig = l1._run_l1_command
    l1._run_l1_command = lambda cmd, pp, timeout=120: (0, _json.dumps(docs))
    try:
        ga = l1._resolve_artifact_via_central(
            "io.jsonwebtoken.Jwts", "io.jsonwebtoken", "/x", 30
        )
        assert ga == ("io.jsonwebtoken", "jjwt-api"), ga
    finally:
        l1._run_l1_command = orig
    print("  ✅ Central 反查：groupId 前缀过滤 + 实体 artifact 优先")


# ── ② 跨模块/跨子任务内部包未就绪 → BLOCKED 退避（_build_blocked_on_unbuilt_internal）──

def test_blocked_on_unbuilt_internal_pkg():
    """缺【尚未建出的项目内部包】(own group + 树里无声明) → 判 BLOCKED（待生产者落地重试）。"""
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        _make_project(d, "import com.example.b.sender.dto.FooDto;")
        build_out = (
            "[ERROR] modA/src/main/java/com/example/a/Svc.java:[2,30] "
            "package com.example.b.sender.dto does not exist\n"
        )
        # 新：返回【被阻断的内部缺包集合】(非空即②)，供 brain 反查生产者子任务
        assert l1._build_blocked_on_unbuilt_internal(str(d), build_out, 30) == {
            "com.example.b.sender.dto"
        }
    print("  ✅ 内部包尚未建出 → BLOCKED（②退避待生产者）+ 吐出缺包集")


def test_not_blocked_when_internal_pkg_exists_in_tree():
    """内部包【已在树里】却报 does not exist → 真编译错(如包名拼错)，不标 BLOCKED，照常 FAIL。"""
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        _make_project(d, "import com.example.a.Helper;")
        # com.example.a 已被 Svc.java 声明（在树里）
        build_out = (
            "[ERROR] modA/src/main/java/com/example/a/Svc.java:[2,30] "
            "package com.example.a does not exist\n"
        )
        assert l1._build_blocked_on_unbuilt_internal(str(d), build_out, 30) == set()
    print("  ✅ 内部包已在树里 → 真错，不误标 BLOCKED")


def test_not_blocked_when_thirdparty_pkg_missing():
    """混入第三方缺包（应交 dep-repair）→ 不算纯②，不标 BLOCKED。"""
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        _make_project(d, "import io.jsonwebtoken.Jwts;")
        build_out = (
            "[ERROR] modA/src/main/java/com/example/a/Svc.java:[2,30] "
            "package com.example.b.dto does not exist\n"
            "[ERROR] modA/src/main/java/com/example/a/Svc.java:[3,30] "
            "package io.jsonwebtoken does not exist\n"
        )
        assert l1._build_blocked_on_unbuilt_internal(str(d), build_out, 30) == set()
    print("  ✅ 含第三方缺包 → 交 dep-repair，不标 BLOCKED")


def test_run_l1_pipeline_blocks_on_unbuilt_internal():
    """端到端：run_l1_pipeline 构建缺内部包(repair 无能为力) → pipeline_blocked=internal_pkg_not_built。"""
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskHarness, NotRunKind

    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        rel = _make_project(d, "import com.example.b.sender.dto.FooDto;")
        st = SubTask(
            id="s-1", description="② test", difficulty=SubTaskDifficulty.MEDIUM,
            scope=FileScope(writable=[rel], readable=[rel]),
            harness=TaskHarness(language="java", build_command="mvn -q -pl modA -am compile"),
        )
        diff = f"--- /dev/null\n+++ b/{rel}\n@@ -0,0 +1,2 @@\n+package com.example.a;\n+class Svc{{}}\n"
        # 构建恒返回内部包缺失；repair 无可修(无第三方/无 typo)→ 落到 ② BLOCKED 分类
        FAIL = (1, f"[ERROR] {rel}:[2,30] package com.example.b.sender.dto does not exist")
        orig_run, orig_app = l1._run_l1_command, l1._build_cmd_applicable
        l1._run_l1_command = lambda cmd, pp, timeout=120: FAIL
        l1._build_cmd_applicable = lambda cmd, pp: True
        try:
            ok, details = l1.run_l1_pipeline(str(d), st, diff, timeout=30)
            assert details.get("pipeline_blocked") == "internal_pkg_not_built", details
            assert details.get("not_run_kind") == NotRunKind.BLOCKED.value
            assert not details.get("build_failed"), "②未就绪不应记 capability FAIL"
            # 结构化输出：吐出缺的内部包，供 brain 反查生产者子任务（治本 replan 死循环）
            assert details.get("blocked_on_packages") == ["com.example.b.sender.dto"], details
        finally:
            l1._run_l1_command, l1._build_cmd_applicable = orig_run, orig_app
    print("  ✅ run_l1_pipeline：缺内部包 → BLOCKED（非 capability FAIL）")


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"  ❌ {fn.__name__}: {e}")
            traceback.print_exc()
            fails += 1
    sys.exit(1 if fails else 0)
