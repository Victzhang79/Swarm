#!/usr/bin/env python3
"""Jakarta/Javax 命名空间确定性归一 + detect_stack JVM 事实抓取 回归测试。

治本背景：本地模型按训练惯性写 javax.* → `package javax.servlet does not exist` →
复读死循环到迭代上限（实测 RuoYi st-3 等 8 子任务卡死）。两道防线：
  1) detect_stack 据磁盘源码权威定栈命名空间（喂 worker prompt，从源头堵）；
  2) L1 pull-back 后 rewrite_jvm_namespace 确定性改对（短路死循环，不靠换模型）。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from swarm.worker.l1_pipeline import (
    rewrite_jvm_namespace,
    parse_missing_packages,
    parse_missing_versions,
    _attempt_import_repair,
    _attempt_maven_version_repair,
    _attempt_build_repair,
    _build_error_is_upstream,
    _stack_repair_langs,
    _tool_missing,
)
from swarm.brain.stack_detect import detect_stack_deterministic, format_stack_for_prompt


# ── parse_missing_packages：从真实 mvn 编译输出解析 (文件, 包) 对 ──

def test_parse_missing_packages_real_format():
    out = (
        "[ERROR] COMPILATION ERROR :\n"
        "[ERROR] /workspace/ruoyi-alarm/src/main/java/com/ruoyi/alarm/interceptor/"
        "AppAuthInterceptor.java:[3,26] package javax.servlet.http does not exist\n"
        "[ERROR] /workspace/ruoyi-alarm/src/main/java/com/ruoyi/alarm/interceptor/"
        "AppAuthInterceptor.java:[4,26] package javax.servlet.http does not exist\n"
        "[ERROR] /workspace/ruoyi-admin/.../AlarmTemplateController.java:[7,51] "
        "package org.springframework.security.access.prepost does not exist\n"
    )
    pairs = parse_missing_packages(out)
    pkgs = {p for _, p in pairs}
    assert "javax.servlet.http" in pkgs
    assert "org.springframework.security.access.prepost" in pkgs
    # 去重：同文件同包只算一次
    assert sum(1 for _, p in pairs if p == "javax.servlet.http") == 1


def test_parse_missing_packages_empty():
    assert parse_missing_packages("") == []
    assert parse_missing_packages("BUILD SUCCESS") == []


# ── rewrite_jvm_namespace：整包迁移前缀改对，JDK 自带 javax.* 绝不动 ──

def test_servlet_javax_to_jakarta():
    t = "import javax.servlet.http.HttpServletRequest;\nimport javax.servlet.http.HttpServletResponse;"
    out, n = rewrite_jvm_namespace(t, "jakarta")
    assert n == 2
    assert "javax.servlet" not in out
    assert out.count("jakarta.servlet.http") == 2


def test_jdk_javax_packages_must_not_change():
    """javax.sql/crypto/naming/xml.parsers/transform/transaction.xa/annotation.processing 留在 JDK。"""
    keep = (
        "import javax.sql.DataSource; javax.crypto.Cipher; javax.naming.Context; "
        "javax.xml.parsers.DocumentBuilder; javax.xml.transform.Transformer; "
        "javax.transaction.xa.XAResource; javax.annotation.processing.Processor;"
    )
    out, n = rewrite_jvm_namespace(keep, "jakarta")
    assert out == keep and n == 0


def test_moved_annotation_symbols_change_but_processing_stays():
    t = "javax.annotation.Resource a; javax.annotation.processing.Filer f;"
    out, n = rewrite_jvm_namespace(t, "jakarta")
    assert "jakarta.annotation.Resource" in out
    assert "javax.annotation.processing.Filer" in out
    assert n == 1


def test_persistence_validation_inject():
    t = ("import javax.persistence.Entity; import javax.validation.constraints.NotNull; "
         "import javax.inject.Inject;")
    out, n = rewrite_jvm_namespace(t, "jakarta")
    assert "jakarta.persistence.Entity" in out
    assert "jakarta.validation.constraints.NotNull" in out
    assert "jakarta.inject.Inject" in out
    assert n == 3


def test_reverse_direction_jakarta_to_javax():
    """项目是 Spring Boot 2.x（javax）但模型写了 jakarta → 反向改对。"""
    out, n = rewrite_jvm_namespace("import jakarta.servlet.Filter;", "javax")
    assert out == "import javax.servlet.Filter;" and n == 1


def test_already_correct_is_noop():
    out, n = rewrite_jvm_namespace("import jakarta.servlet.Filter;", "jakarta")
    assert n == 0


def test_unknown_target_is_noop():
    t = "import javax.servlet.Filter;"
    assert rewrite_jvm_namespace(t, "")[1] == 0
    assert rewrite_jvm_namespace(t, "spring")[1] == 0


# ── detect_stack：据现存源码 import 实证定命名空间 + Boot/Java 版本 ──

def _write(p: Path, rel: str, body: str) -> None:
    f = p / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")


def test_detect_stack_jvm_namespace_from_source():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write(root, "pom.xml",
               "<project><properties><spring-boot.version>4.0.6</spring-boot.version>"
               "<java.version>17</java.version></properties></project>")
        _write(root, "src/main/java/com/x/A.java",
               "package com.x;\nimport jakarta.servlet.http.HttpServletRequest;\nclass A{}")
        _write(root, "src/main/java/com/x/B.java",
               "package com.x;\nimport jakarta.persistence.Entity;\nclass B{}")
        prof = detect_stack_deterministic(str(root))
        jvm = prof.get("jvm") or {}
        assert jvm.get("servlet_namespace") == "jakarta"
        assert jvm.get("spring_boot_version") == "4.0.6"
        assert jvm.get("java_version") == "17"
        # 硬约束渲染进 prompt
        rendered = format_stack_for_prompt(prof)
        assert "jakarta.servlet" in rendered and "严禁" in rendered


def test_detect_stack_jvm_version_inference_when_no_source_imports():
    """源码无 servlet/jpa import 时，用 Spring Boot 大版本推断命名空间（≥3 → jakarta）。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write(root, "pom.xml",
               "<project><properties><spring-boot.version>3.2.1</spring-boot.version>"
               "</properties></project>")
        _write(root, "src/main/java/com/x/Plain.java", "package com.x;\nclass Plain{}")
        prof = detect_stack_deterministic(str(root))
        jvm = prof.get("jvm") or {}
        assert jvm.get("servlet_namespace") == "jakarta"
        assert "推断" in (jvm.get("namespace_source") or "")


def test_import_repair_derives_canonical_prefix_from_project():
    """治本·通用：写错的 javax 前缀，据【项目自身现存 import】推导出 jakarta 并改对——
    全程无硬编码包名，jakarta 是项目源码自己说了算（local 模式 e2e）。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for i in range(3):
            _write(root, f"src/main/java/com/x/Good{i}.java",
                   f"package com.x;\nimport jakarta.servlet.http.HttpServletRequest;\nclass Good{i}{{}}\n")
        bad = root / "mod/src/main/java/com/x/Bad.java"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("package com.x;\nimport javax.servlet.http.HttpServletRequest;\nclass Bad{}\n")
        build_out = f"[ERROR] {bad}:[2,26] package javax.servlet.http does not exist\n"
        n, paths = _attempt_import_repair(str(root), build_out, timeout=30)
        assert n == 1
        # TD2606-C9：修复路径透传，供 executor 回传本地（即便文件在写权 scope 外）
        assert any(p.endswith("Bad.java") for p in paths), paths
        fixed = bad.read_text()
        assert "jakarta.servlet.http" in fixed and "javax.servlet" not in fixed
        assert not (bad.parent / "Bad.java.bak").exists()  # 不留 .bak


def test_import_repair_leaves_missing_dependency_untouched():
    """项目从未用过该 suffix（真·缺依赖，非前缀写错）→ 绝不误修。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        bad = root / "mod/X.java"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("import org.springframework.security.access.prepost.PreAuthorize;\nclass X{}\n")
        build_out = (f"[ERROR] {bad}:[1,51] package "
                     "org.springframework.security.access.prepost does not exist\n")
        assert _attempt_import_repair(str(root), build_out, timeout=30) == (0, [])
        assert "org.springframework.security.access.prepost" in bad.read_text()


# ── 跨生态 dispatcher：按语言路由 + 工具缺失优雅跳过 + 混合项目 ──

def test_stack_drives_adapter_selection():
    """adapter 选择是 project_stack 的消费者：以 build 工具为准，避开 javascript 含 java 陷阱。"""
    assert _stack_repair_langs({"build": "maven", "backend": "Spring Boot (java)"}) == {"java"}
    assert _stack_repair_langs({"build": "go", "backend": "go"}) == {"go"}
    assert _stack_repair_langs({"build": "cargo", "backend": "rust"}) == {"rust"}
    # Node 项目：backend="javascript/typescript" 含子串 "java" 但绝不能误选 java
    assert _stack_repair_langs(
        {"build": "npm", "backend": "Express (javascript/typescript)"}
    ) == {"ts"}
    # 混合：Java 后端 + 独立 SPA 前端 → java + ts 都入选
    mixed = _stack_repair_langs(
        {"build": "maven", "backend": "Spring Boot (java)",
         "frontend": "Vue(独立)", "frontend_kind": "separated"}
    )
    assert mixed == {"java", "ts"}
    # 未判明 → None（调用方回退扩展名）
    assert _stack_repair_langs({"build": "未判明"}) is None
    assert _stack_repair_langs(None) is None


def test_stack_gating_overrides_stray_extensions(monkeypatch):
    """权威栈说是纯 Go 项目时，即便 modified 混入一个 .ts，也不跑 ts adapter（以栈为准）。"""
    import swarm.worker.l1_pipeline as L
    calls = []
    monkeypatch.setattr(L, "_repair_go", lambda *a, **k: (calls.append("go"), (1, []))[1])
    monkeypatch.setattr(L, "_repair_ts", lambda *a, **k: (calls.append("ts"), (1, []))[1])
    monkeypatch.setattr(L, "_repair_rust", lambda *a, **k: (calls.append("rust"), (1, []))[1])
    monkeypatch.setattr(L, "_attempt_import_repair", lambda *a, **k: (0, []))
    _attempt_build_repair("/x", "", ["svc/main.go", "stray.ts"], 10,
                          project_stack={"build": "go", "backend": "go"})
    assert "go" in calls and "ts" not in calls  # 栈=go → 不碰 stray .ts


def test_tool_missing_detection():
    assert _tool_missing("bash: goimports: command not found")
    assert _tool_missing("npm ERR! could not determine executable to run")
    assert _tool_missing("eslint: not found")
    assert not _tool_missing("[ERROR] real compile error: cannot find symbol")


def test_dispatcher_routes_java_through_to_project_derived_repair(monkeypatch):
    """混合 modified（含 .go/.ts/.rs）下，Java 子树仍走项目自证前缀修复并改对；
    其它生态工具缺失则优雅跳过，不影响 Java，也不抛异常。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for i in range(3):
            _write(root, f"src/main/java/com/x/Good{i}.java",
                   f"package com.x;\nimport jakarta.servlet.http.HttpServletRequest;\nclass Good{i}{{}}\n")
        bad = root / "mod/Bad.java"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("import javax.servlet.http.HttpServletRequest;\nclass Bad{}\n")
        build_out = f"[ERROR] {bad}:[1,26] package javax.servlet.http does not exist\n"
        modified = ["mod/Bad.java", "web/app.ts", "svc/main.go", "core/lib.rs"]
        # 不论 goimports/eslint/cargo 是否安装，都不应抛异常；Java 至少改对 1 个
        n, paths = _attempt_build_repair(str(root), build_out, modified, timeout=30)
        assert n >= 1
        assert any(p.endswith("Bad.java") for p in paths), paths
        assert "jakarta.servlet.http" in bad.read_text()


def test_dispatcher_non_java_tools_absent_is_graceful(monkeypatch):
    """纯非 Java 项目（Go/TS/Rust modified）：无 Java 错误、相关工具大概率未装 →
    返回 0、不抛异常（安全空转，绝不把缺工具当失败）。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write(root, "main.go", "package main\nfunc main(){}\n")
        # 强制各生态工具"缺失"以确定性验证优雅跳过
        import swarm.worker.l1_pipeline as L
        monkeypatch.setattr(L, "_run_l1_command", lambda *a, **k: (127, "command not found"))
        n, paths = _attempt_build_repair(str(root), "go build error: undefined: Foo",
                                         ["main.go", "x.ts", "y.rs"], timeout=10)
        assert n == 0
        assert paths == []


def test_detect_stack_non_jvm_has_empty_jvm():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write(root, "package.json", '{"name":"x","dependencies":{"react":"18"}}')
        prof = detect_stack_deterministic(str(root))
        assert prof.get("jvm") in (None, {}) or not (prof.get("jvm") or {}).get("servlet_namespace")


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}: {e}")
    sys.exit(1 if failed else 0)


# ── P0-A：通用 pom 依赖对账——缺 <version> 元素注入（hutool reactor 毒根治） ──

def test_parse_missing_versions():
    out = (
        "[ERROR] 'dependencies.dependency.version' for cn.hutool:hutool-all:jar is missing. @ line 33\n"
        "The project com.ruoyi:ruoyi-generator:4.8.3 (/workspace/ruoyi-generator/pom.xml) has 1 error\n"
    )
    assert parse_missing_versions(out) == [("cn.hutool", "hutool-all")]
    assert parse_missing_versions("无此类错误") == []


def test_version_repair_injects_missing_version(monkeypatch):
    """模块 pom 的依赖缺 <version>（非版本写错）→ 从仓库 metadata 注入有效版本。"""
    import swarm.worker.l1_pipeline as L
    monkeypatch.setattr(L, "_fetch_maven_versions",
                        lambda g, a, p, t: ["5.8.0", "5.8.35", "5.7.22"])
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        pom = root / "ruoyi-generator" / "pom.xml"
        pom.parent.mkdir(parents=True)
        pom.write_text(
            "<project>\n  <dependencies>\n"
            "    <dependency>\n      <groupId>cn.hutool</groupId>\n"
            "      <artifactId>hutool-all</artifactId>\n    </dependency>\n"
            "  </dependencies>\n</project>\n"
        )
        build_out = ("[ERROR] 'dependencies.dependency.version' for "
                     "cn.hutool:hutool-all:jar is missing.\n")
        n, paths = _attempt_maven_version_repair(str(root), build_out, timeout=30)
        assert n == 1, (n, paths)
        assert "<version>5.8.35</version>" in pom.read_text()
        assert any(p.endswith("pom.xml") for p in paths)


def test_version_repair_skips_managed_pom(monkeypatch):
    """带 dependencyManagement 的父 pom 不注入（受管块本就带版本，注入会造双 version）。"""
    import swarm.worker.l1_pipeline as L
    monkeypatch.setattr(L, "_fetch_maven_versions", lambda g, a, p, t: ["5.8.35"])
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        pom = root / "pom.xml"
        pom.write_text(
            "<project>\n  <dependencyManagement>\n    <dependencies>\n"
            "      <dependency>\n        <groupId>cn.hutool</groupId>\n"
            "        <artifactId>hutool-all</artifactId>\n        <version>5.8.0</version>\n"
            "      </dependency>\n    </dependencies>\n  </dependencyManagement>\n</project>\n"
        )
        build_out = ("[ERROR] 'dependencies.dependency.version' for "
                     "cn.hutool:hutool-all:jar is missing.\n")
        n, _paths = _attempt_maven_version_repair(str(root), build_out, timeout=30)
        assert n == 0, "受管父 pom 不应被注入"


# ── P0-B：-am reactor 连坐——构建错归属判定 ──

def test_upstream_error_not_blamed_on_subtask():
    """-pl ruoyi-alarm 但报错在上游 ruoyi-generator 的坏 pom → 判上游(True)，不连坐本子任务。"""
    cmd = "mvn -pl ruoyi-alarm -am -q compile"
    out = ("[ERROR] 'dependencies.dependency.version' for cn.hutool:hutool-all:jar is missing.\n"
           "The project com.ruoyi:ruoyi-generator:4.8.3 (/workspace/ruoyi-generator/pom.xml) has 1 error\n")
    assert _build_error_is_upstream(out, cmd) is True


def test_own_module_compile_error_is_capability():
    """报错在本子任务自己的模块 → 非上游(False)，是真能力问题，照常判失败。"""
    cmd = "mvn -pl ruoyi-alarm -am -q compile"
    out = ("[ERROR] /workspace/ruoyi-alarm/src/main/java/com/ruoyi/alarm/x/A.java:[3,5] cannot find symbol\n")
    assert _build_error_is_upstream(out, cmd) is False


def test_mixed_own_and_upstream_is_not_excused():
    """本模块也有错 + 上游也有错 → 不放过本子任务(False)。"""
    cmd = "mvn -pl ruoyi-alarm -am -q compile"
    out = ("The project com.ruoyi:ruoyi-generator:4.8.3 (/workspace/ruoyi-generator/pom.xml) has 1 error\n"
           "[ERROR] /workspace/ruoyi-alarm/src/main/java/com/ruoyi/alarm/x/A.java:[3,5] cannot find symbol\n")
    assert _build_error_is_upstream(out, cmd) is False


def test_no_pl_or_no_error_is_false():
    assert _build_error_is_upstream("some error", "mvn compile") is False
    assert _build_error_is_upstream("BUILD SUCCESS", "mvn -pl ruoyi-alarm -am compile") is False


# ── 根因#3：文件级归属——别人的坏文件不连坐本子任务（996db614 7h雪崩根治） ──

def test_error_in_other_subtask_file_is_upstream():
    """本子任务改 AppAuthInterceptor，但 build 炸在别人的 AlarmAppSecretController → 标 BLOCKED。"""
    out = ("[ERROR] /workspace/ruoyi-alarm/src/main/java/com/ruoyi/alarm/controller/"
           "AlarmAppSecretController.java:[134,61] incompatible types: String[] cannot be converted to Long[]\n")
    cmd = "mvn -pl ruoyi-alarm -am -q compile"
    modified = ["ruoyi-alarm/src/main/java/com/ruoyi/alarm/interceptor/AppAuthInterceptor.java"]
    assert _build_error_is_upstream(out, cmd, modified) is True


def test_error_in_own_file_is_capability():
    """build 炸在本子任务自己改的文件 → 不放过（False），由根因#1 全量闸门在源头修。"""
    out = ("[ERROR] /workspace/ruoyi-alarm/src/main/java/com/ruoyi/alarm/interceptor/"
           "AppAuthInterceptor.java:[50,12] cannot find symbol\n")
    cmd = "mvn -pl ruoyi-alarm -am -q compile"
    modified = ["ruoyi-alarm/src/main/java/com/ruoyi/alarm/interceptor/AppAuthInterceptor.java"]
    assert _build_error_is_upstream(out, cmd, modified) is False


def test_mixed_own_and_other_file_not_excused():
    """自己的文件也有错 + 别人的也有错 → 不放过本子任务(False)。"""
    out = ("[ERROR] /workspace/ruoyi-alarm/.../AlarmAppSecretController.java:[134,61] incompatible types\n"
           "[ERROR] /workspace/ruoyi-alarm/src/main/java/com/ruoyi/alarm/interceptor/AppAuthInterceptor.java:[5,1] cannot find symbol\n")
    cmd = "mvn -pl ruoyi-alarm -am -q compile"
    modified = ["ruoyi-alarm/src/main/java/com/ruoyi/alarm/interceptor/AppAuthInterceptor.java"]
    assert _build_error_is_upstream(out, cmd, modified) is False


def test_filelevel_falls_back_to_module_when_no_modified():
    """无 modified → 回退模块级(P0-B 原行为)，向后兼容。"""
    out = "The project com.ruoyi:ruoyi-generator:4.8.3 (/workspace/ruoyi-generator/pom.xml) has 1 error\n"
    assert _build_error_is_upstream(out, "mvn -pl ruoyi-alarm -am compile") is True


# ── 根因#1 通用版：生产者全量构建闸门——任何栈皆然(非 Java 写死) ──
from swarm.worker.l1_pipeline import _derive_full_build_command


def test_derive_build_is_stack_general():
    import os
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # Java/maven
        (root / "pom.xml").write_text("<project/>")
        assert _derive_full_build_command(str(root), ["ruoyi-alarm/X.java"], {"build": "maven"}) == "mvn -q compile"
        # Go
        (root / "go.mod").write_text("module x")
        assert _derive_full_build_command(str(root), ["svc/main.go"], {"build": "go"}) == "go build ./..."
        # Rust
        (root / "Cargo.toml").write_text("[package]")
        assert _derive_full_build_command(str(root), ["src/lib.rs"], {"build": "cargo"}) == "cargo build -q"
        # 前端 TS
        (root / "tsconfig.json").write_text("{}")
        assert _derive_full_build_command(str(root), ["src/app.ts"], {"build": "npm"}) == "tsc --noEmit"


def test_derive_build_gradle_vs_maven():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "build.gradle").write_text("plugins{}")
        # 无 pom + 有 gradle + .java → gradle
        assert "gradle" in _derive_full_build_command(str(root), ["app/A.java"], {"build": "gradle"})


def test_derive_build_noop_without_source_or_manifest():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # 改的是 .md，无源码 → 不派生
        assert _derive_full_build_command(str(root), ["README.md"], {"build": "maven"}) == ""
        # .java 但无 pom/gradle 清单 → 不派生(不臆造)
        assert _derive_full_build_command(str(root), ["X.java"], None) == ""


# ── 根因#2：通用符号 typo 修复——据项目现存符号纠近邻(非硬编码表) ──
from swarm.worker.l1_pipeline import _attempt_symbol_repair, _edit_distance


def test_edit_distance_bounds():
    assert _edit_distance("isEmtpy", "isEmpty") == 2   # 转置
    assert _edit_distance("StringBufffer", "StringBuffer") == 1
    assert _edit_distance("getError", "getMessage") > 2  # 语义错，非 typo，不该纠


def test_symbol_repair_fixes_typo_from_project_usage(monkeypatch):
    """isEmtpy 拼错 → 项目里 isEmpty 高频 → 纠为 isEmpty；全程无硬编码符号表。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # 项目现存源码：isEmpty 高频出现（真理来源）
        for i in range(6):
            _write(root, f"src/U{i}.java", f"class U{i}{{boolean f(String s){{return s.isEmpty();}}}}\n")
        bad = root / "mod/Svc.java"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("class Svc{boolean g(String s){return s.isEmtpy();}}\n")
        build_out = ("[ERROR] mod/Svc.java:[1,40] cannot find symbol\n"
                     "  symbol:   method isEmtpy()\n  location: variable s of type java.lang.String\n")
        n, paths = _attempt_symbol_repair(str(root), build_out, ["mod/Svc.java"], timeout=30)
        assert n == 1, (n, paths)
        assert "isEmpty()" in bad.read_text() and "isEmtpy" not in bad.read_text()


def test_symbol_repair_skips_other_subtask_file(monkeypatch):
    """报错文件不是本子任务改的 → 不动（交 owner，配合文件级归属）。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for i in range(6):
            _write(root, f"src/U{i}.java", f"class U{i}{{boolean f(String s){{return s.isEmpty();}}}}\n")
        other = root / "mod/Other.java"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text("class Other{boolean g(String s){return s.isEmtpy();}}\n")
        build_out = ("[ERROR] mod/Other.java:[1,40] cannot find symbol\n  symbol:   method isEmtpy()\n")
        # 本子任务只改了 X.java，没改 Other.java
        n, _p = _attempt_symbol_repair(str(root), build_out, ["mod/X.java"], timeout=30)
        assert n == 0 and "isEmtpy" in other.read_text()


def test_symbol_repair_noop_on_semantic_error(monkeypatch):
    """getError→getMessage 是语义错(距>2)，无唯一近邻 → 不乱改。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for i in range(6):
            _write(root, f"src/U{i}.java", f"class U{i}{{String f(Exception e){{return e.getMessage();}}}}\n")
        bad = root / "mod/S.java"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("class S{String g(Exception e){return e.getError();}}\n")
        build_out = ("[ERROR] mod/S.java:[1,40] cannot find symbol\n  symbol:   method getError()\n")
        n, _p = _attempt_symbol_repair(str(root), build_out, ["mod/S.java"], timeout=30)
        assert n == 0 and "getError" in bad.read_text()
