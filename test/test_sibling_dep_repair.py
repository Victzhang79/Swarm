"""A2 多栈「从兄弟 manifest 找权威坐标注入」行为测试（npm/cargo/go）。

对齐 Maven 侧 _inject_missing_maven_deps 的原则：只用项目自证坐标、绝不臆造版本、fail-closed。
纯文件操作，不触网络/沙箱/工具。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.worker.sibling_dep_repair import (  # noqa: E402
    _missing_deps,
    _norm_npm_pkg,
    repair_from_sibling_manifests,
)


# ── 缺失依赖检测 ──────────────────────────────────────────────
def test_missing_deps_npm():
    out = _missing_deps("Error: Cannot find module 'lodash'\nCan't resolve '@scope/ui/button'", "npm")
    assert "lodash" in out and "@scope/ui" in out


def test_missing_deps_relative_import_ignored():
    assert _missing_deps("Cannot find module './local/util'", "npm") == []
    assert _norm_npm_pkg("../x") is None


def test_missing_deps_cargo_and_go():
    assert "serde" in _missing_deps("error[E0432]: unresolved import `serde`", "cargo")
    assert "github.com/foo/bar" in _missing_deps(
        "main.go:3: no required module provides package github.com/foo/bar", "go")


# ── npm 注入 ──────────────────────────────────────────────
def test_npm_injects_from_sibling(tmp_path):
    # 兄弟包声明 lodash 权威版本
    sib = tmp_path / "pkg-a"
    sib.mkdir()
    (sib / "package.json").write_text(json.dumps(
        {"name": "a", "dependencies": {"lodash": "^4.17.21"}}), encoding="utf-8")
    # 目标包（被改文件所在）缺 lodash
    tgt = tmp_path / "pkg-b"
    (tgt / "src").mkdir(parents=True)
    (tgt / "package.json").write_text(json.dumps({"name": "b", "dependencies": {}}), encoding="utf-8")
    (tgt / "src" / "index.js").write_text("import _ from 'lodash';", encoding="utf-8")

    n, paths = repair_from_sibling_manifests(
        str(tmp_path), "Cannot find module 'lodash'", ["pkg-b/src/index.js"], "npm")
    assert n == 1 and "pkg-b/package.json" in paths
    got = json.loads((tgt / "package.json").read_text())
    assert got["dependencies"]["lodash"] == "^4.17.21"  # 权威坐标，非臆造


def test_npm_failclosed_when_no_sibling_coord(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "b", "dependencies": {}}), encoding="utf-8")
    (tmp_path / "i.js").write_text("import x from 'nowhere';", encoding="utf-8")
    n, paths = repair_from_sibling_manifests(
        str(tmp_path), "Cannot find module 'nowhere'", ["i.js"], "npm")
    assert n == 0 and paths == []  # 兄弟里也没 → 绝不臆造


def test_npm_skip_if_already_declared(tmp_path):
    sib = tmp_path / "a"
    sib.mkdir()
    (sib / "package.json").write_text(json.dumps({"dependencies": {"lodash": "^4.0.0"}}), encoding="utf-8")
    tgt = tmp_path / "b"
    tgt.mkdir()
    (tgt / "package.json").write_text(json.dumps({"dependencies": {"lodash": "^3.0.0"}}), encoding="utf-8")
    n, _ = repair_from_sibling_manifests(
        str(tmp_path), "Cannot find module 'lodash'", ["b/x.js"], "npm")
    assert n == 0  # 目标已声明（哪怕版本不同）→ 不动，不覆盖


# ── cargo 注入 ──────────────────────────────────────────────
def test_cargo_injects_from_sibling(tmp_path):
    sib = tmp_path / "crate-a"
    sib.mkdir()
    (sib / "Cargo.toml").write_text('[package]\nname="a"\n\n[dependencies]\nserde = "1.0.197"\n', encoding="utf-8")
    tgt = tmp_path / "crate-b"
    (tgt / "src").mkdir(parents=True)
    (tgt / "Cargo.toml").write_text('[package]\nname="b"\n\n[dependencies]\n', encoding="utf-8")
    (tgt / "src" / "lib.rs").write_text("use serde::Serialize;", encoding="utf-8")

    n, paths = repair_from_sibling_manifests(
        str(tmp_path), "error[E0432]: unresolved import `serde`", ["crate-b/src/lib.rs"], "cargo")
    assert n == 1 and "crate-b/Cargo.toml" in paths
    assert 'serde = "1.0.197"' in (tgt / "Cargo.toml").read_text()


# ── go 注入 ──────────────────────────────────────────────
def test_go_injects_from_sibling(tmp_path):
    sib = tmp_path / "svc-a"
    sib.mkdir()
    (sib / "go.mod").write_text(
        "module a\n\ngo 1.21\n\nrequire (\n\tgithub.com/foo/bar v1.2.3\n)\n", encoding="utf-8")
    tgt = tmp_path / "svc-b"
    tgt.mkdir()
    (tgt / "go.mod").write_text("module b\n\ngo 1.21\n\nrequire (\n)\n", encoding="utf-8")
    (tgt / "main.go").write_text('import "github.com/foo/bar"', encoding="utf-8")

    n, paths = repair_from_sibling_manifests(
        str(tmp_path),
        "main.go:1: no required module provides package github.com/foo/bar",
        ["svc-b/main.go"], "go")
    assert n == 1 and "svc-b/go.mod" in paths
    assert "github.com/foo/bar v1.2.3" in (tgt / "go.mod").read_text()


def test_unknown_stack_noop(tmp_path):
    assert repair_from_sibling_manifests(str(tmp_path), "x", [], "python") == (0, [])


# ── round27 双复核回归：注错/损坏 manifest 级缺陷 ──────────────────────────
def _cargo_sib(tmp_path, body='[package]\nname="a"\n\n[dependencies]\nserde = "1.0.197"\n'):
    sib = tmp_path / "crate-a"
    sib.mkdir()
    (sib / "Cargo.toml").write_text(body, encoding="utf-8")


def test_cargo_dot_table_counts_declared(tmp_path):
    """[dependencies.NAME] 点表已声明 → 不得注入重复键（TOML 重复键 cargo 拒绝解析）。"""
    _cargo_sib(tmp_path)
    tgt = tmp_path / "crate-b"
    (tgt / "src").mkdir(parents=True)
    before = '[package]\nname="b"\n\n[dependencies.serde]\nversion = "1.0.200"\nfeatures = ["derive"]\n'
    (tgt / "Cargo.toml").write_text(before, encoding="utf-8")
    n, _ = repair_from_sibling_manifests(
        str(tmp_path), "error[E0432]: unresolved import `serde`", ["crate-b/src/lib.rs"], "cargo")
    assert n == 0 and (tgt / "Cargo.toml").read_text() == before


def test_cargo_workspace_inherit_counts_declared_and_not_source(tmp_path):
    """目标 `serde = { workspace = true }` 已声明不注入；兄弟 workspace=true 无版本不可作坐标源。"""
    _cargo_sib(tmp_path, '[package]\nname="a"\n\n[dependencies]\nserde = { workspace = true }\n')
    tgt = tmp_path / "crate-b"
    (tgt / "src").mkdir(parents=True)
    before = '[package]\nname="b"\n\n[dependencies]\nserde = { workspace = true }\n'
    (tgt / "Cargo.toml").write_text(before, encoding="utf-8")
    n, _ = repair_from_sibling_manifests(
        str(tmp_path), "error[E0432]: unresolved import `serde`", ["crate-b/src/lib.rs"], "cargo")
    assert n == 0 and (tgt / "Cargo.toml").read_text() == before
    # 反面：目标真缺、唯一兄弟只有 workspace=true → 无可移植版本，fail-closed 不臆造
    tgt2 = tmp_path / "crate-c"
    (tgt2 / "src").mkdir(parents=True)
    (tgt2 / "Cargo.toml").write_text('[package]\nname="c"\n', encoding="utf-8")
    n2, _ = repair_from_sibling_manifests(
        str(tmp_path), "error[E0432]: unresolved import `serde`", ["crate-c/src/lib.rs"], "cargo")
    assert n2 == 0


def test_cargo_workspace_root_failclosed(tmp_path):
    """无 [package] 的 workspace 虚拟根注 [dependencies] cargo 直接拒绝 → 必须 fail-closed 不碰。"""
    _cargo_sib(tmp_path)
    before = '[workspace]\nmembers = ["crate-a", "crate-b"]\n'
    (tmp_path / "Cargo.toml").write_text(before, encoding="utf-8")
    # crate-b 只有源码没有自己的 Cargo.toml → _nearest_manifest 走到虚拟根
    (tmp_path / "crate-b" / "src").mkdir(parents=True)
    n, paths = repair_from_sibling_manifests(
        str(tmp_path), "error[E0432]: unresolved import `serde`", ["crate-b/src/lib.rs"], "cargo")
    assert n == 0 and paths == []
    assert (tmp_path / "Cargo.toml").read_text() == before


def test_npm_file_version_not_transplanted(tmp_path):
    """兄弟的 `file:../x` 是目录相对坐标，跨目录移植必错 → 不可作坐标源。"""
    sib = tmp_path / "pkg-a"
    sib.mkdir()
    (sib / "package.json").write_text(json.dumps(
        {"dependencies": {"common": "file:../common"}}), encoding="utf-8")
    tgt = tmp_path / "apps" / "web"
    tgt.mkdir(parents=True)
    before = json.dumps({"name": "web", "dependencies": {}})
    (tgt / "package.json").write_text(before, encoding="utf-8")
    n, _ = repair_from_sibling_manifests(
        str(tmp_path), "Cannot find module 'common'", ["apps/web/src/i.js"], "npm")
    assert n == 0 and (tgt / "package.json").read_text() == before


def test_go_replace_companion_not_coord_source(tmp_path):
    """兄弟 require+replace 本地模块：注 require 不带 replace → 拉取必败 → 不可作坐标源；
    replace/exclude block 里的条目也不得当 require 声明。"""
    sib = tmp_path / "svc-a"
    sib.mkdir()
    (sib / "go.mod").write_text(
        "module a\n\ngo 1.21\n\nrequire (\n\tgithub.com/org/lib v0.0.0-00010101000000-000000000000\n)\n"
        "\nreplace github.com/org/lib => ../lib\n", encoding="utf-8")
    tgt = tmp_path / "svc-b"
    tgt.mkdir()
    before = "module b\n\ngo 1.21\n"
    (tgt / "go.mod").write_text(before, encoding="utf-8")
    n, _ = repair_from_sibling_manifests(
        str(tmp_path), "no required module provides package github.com/org/lib", ["svc-b/main.go"], "go")
    assert n == 0 and (tgt / "go.mod").read_text() == before
    # exclude/replace block 条目不算 require 来源
    sib2 = tmp_path / "svc-c"
    sib2.mkdir()
    (sib2 / "go.mod").write_text(
        "module c\n\nexclude (\n\tgithub.com/bad/pkg v0.1.0\n)\n", encoding="utf-8")
    n2, _ = repair_from_sibling_manifests(
        str(tmp_path), "no required module provides package github.com/bad/pkg", ["svc-b/main.go"], "go")
    assert n2 == 0


def test_go_unclosed_require_block_failclosed(tmp_path):
    """目标 go.mod 以未闭合 `require (` 结尾（畸形）→ 不 crash、不注入、不改文件。"""
    sib = tmp_path / "svc-a"
    sib.mkdir()
    (sib / "go.mod").write_text(
        "module a\n\nrequire (\n\tgithub.com/foo/bar v1.2.3\n)\n", encoding="utf-8")
    tgt = tmp_path / "svc-b"
    tgt.mkdir()
    before = "module b\n\nrequire ("
    (tgt / "go.mod").write_text(before, encoding="utf-8")
    n, _ = repair_from_sibling_manifests(
        str(tmp_path), "no required module provides package github.com/foo/bar", ["svc-b/main.go"], "go")
    assert n == 0 and (tgt / "go.mod").read_text() == before


def test_npm_nondict_or_broken_target_failclosed(tmp_path):
    """目标 package.json 根非对象/坏 JSON → 不 crash、不注入，且不影响后续 dep 处理。"""
    sib = tmp_path / "pkg-a"
    sib.mkdir()
    (sib / "package.json").write_text(json.dumps(
        {"dependencies": {"lodash": "^4.0.0"}}), encoding="utf-8")
    tgt = tmp_path / "pkg-b"
    tgt.mkdir()
    (tgt / "package.json").write_text("[1, 2, 3]", encoding="utf-8")
    n, _ = repair_from_sibling_manifests(
        str(tmp_path), "Cannot find module 'lodash'", ["pkg-b/i.js"], "npm")
    assert n == 0
    (tgt / "package.json").write_text("{not json", encoding="utf-8")
    n2, _ = repair_from_sibling_manifests(
        str(tmp_path), "Cannot find module 'lodash'", ["pkg-b/i.js"], "npm")
    assert n2 == 0


def test_modified_path_escape_rejected(tmp_path):
    """modified 里的绝对路径/../ 穿越不可信 → 绝不选中项目外 manifest（默认拒绝，
    对齐 diff_apply._rel_within_root）。回退项目根 manifest。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "package.json").write_text(json.dumps({"dependencies": {}}), encoding="utf-8")
    proj = tmp_path / "proj"
    sib = proj / "pkg-a"
    sib.mkdir(parents=True)
    (sib / "package.json").write_text(json.dumps(
        {"dependencies": {"lodash": "^4.0.0"}}), encoding="utf-8")
    (proj / "package.json").write_text(json.dumps({"name": "root", "dependencies": {}}), encoding="utf-8")
    n, paths = repair_from_sibling_manifests(
        str(proj), "Cannot find module 'lodash'", ["../outside/src/i.js", str(outside / "i.js")], "npm")
    # 项目外 manifest 绝不能被写
    assert json.loads((outside / "package.json").read_text()) == {"dependencies": {}}
    # 回退到项目根 manifest 注入
    assert n == 1 and paths == ["package.json"]


def test_cargo_non_utf8_target_failclosed(tmp_path):
    """cargo/go 是全文读改写：目标含非 UTF-8 字节时严格读失败 → 跳过不写（防静默丢字节）。"""
    _cargo_sib(tmp_path)
    tgt = tmp_path / "crate-b"
    (tgt / "src").mkdir(parents=True)
    raw = b'[package]\nname="b" # caf\xe9\n'
    (tgt / "Cargo.toml").write_bytes(raw)
    n, _ = repair_from_sibling_manifests(
        str(tmp_path), "error[E0432]: unresolved import `serde`", ["crate-b/src/lib.rs"], "cargo")
    assert n == 0 and (tgt / "Cargo.toml").read_bytes() == raw


# ── D12：go 包路径 → 模块路径最长前缀匹配（子包=Go 常态，旧精确匹配恒跳过）──

def test_d12_go_subpackage_matches_module_longest_prefix(tmp_path):
    """D12 核心：build 报子包 github.com/foo/bar/sub，兄弟 require 模块 github.com/foo/bar
    → 按最长前缀命中并注入【模块路径】（go.mod require 的是模块不是包）。"""
    sib = tmp_path / "svc-a"
    sib.mkdir()
    (sib / "go.mod").write_text(
        "module a\n\ngo 1.21\n\nrequire (\n\tgithub.com/foo/bar v1.2.3\n)\n", encoding="utf-8")
    tgt = tmp_path / "svc-b"
    tgt.mkdir()
    (tgt / "go.mod").write_text("module b\n\ngo 1.21\n", encoding="utf-8")

    n, paths = repair_from_sibling_manifests(
        str(tmp_path),
        "main.go:1: no required module provides package github.com/foo/bar/sub",
        ["svc-b/main.go"], "go")
    assert n == 1 and "svc-b/go.mod" in paths
    out = (tgt / "go.mod").read_text()
    assert "github.com/foo/bar v1.2.3" in out, "注入键必须是模块路径"
    assert "github.com/foo/bar/sub" not in out, "绝不能把包路径写进 require"


def test_d12_go_longest_prefix_wins(tmp_path):
    """两兄弟分别声明 github.com/foo 与 github.com/foo/bar → 最长前缀（bar）胜出。"""
    s1 = tmp_path / "svc-a"
    s1.mkdir()
    (s1 / "go.mod").write_text("module a\n\nrequire github.com/foo v0.9.0\n", encoding="utf-8")
    s2 = tmp_path / "svc-c"
    s2.mkdir()
    (s2 / "go.mod").write_text("module c\n\nrequire github.com/foo/bar v1.2.3\n", encoding="utf-8")
    tgt = tmp_path / "svc-b"
    tgt.mkdir()
    (tgt / "go.mod").write_text("module b\n\ngo 1.21\n", encoding="utf-8")

    n, _ = repair_from_sibling_manifests(
        str(tmp_path), "no required module provides package github.com/foo/bar/sub",
        ["svc-b/main.go"], "go")
    assert n == 1
    out = (tgt / "go.mod").read_text()
    assert "github.com/foo/bar v1.2.3" in out
    assert "github.com/foo v0.9.0" not in out


def test_d12_go_prefix_boundary_not_substring(tmp_path):
    """前缀必须落在路径段边界：github.com/foo/barbaz 不得匹配模块 github.com/foo/bar。"""
    sib = tmp_path / "svc-a"
    sib.mkdir()
    (sib / "go.mod").write_text("module a\n\nrequire github.com/foo/bar v1.2.3\n", encoding="utf-8")
    tgt = tmp_path / "svc-b"
    tgt.mkdir()
    before = "module b\n\ngo 1.21\n"
    (tgt / "go.mod").write_text(before, encoding="utf-8")

    n, _ = repair_from_sibling_manifests(
        str(tmp_path), "no required module provides package github.com/foo/barbaz",
        ["svc-b/main.go"], "go")
    assert n == 0 and (tgt / "go.mod").read_text() == before


def test_d12_go_unusable_long_prefix_falls_back_to_shorter(tmp_path):
    """最长前缀带 replace 伴随（坐标不可移植）→ 退到较短但可用的模块坐标，绝不臆造。"""
    s1 = tmp_path / "svc-a"
    s1.mkdir()
    (s1 / "go.mod").write_text(
        "module a\n\nrequire github.com/foo/bar v0.0.0-00010101000000-000000000000\n"
        "\nreplace github.com/foo/bar => ../bar\n", encoding="utf-8")
    s2 = tmp_path / "svc-c"
    s2.mkdir()
    (s2 / "go.mod").write_text("module c\n\nrequire github.com/foo v0.9.0\n", encoding="utf-8")
    tgt = tmp_path / "svc-b"
    tgt.mkdir()
    (tgt / "go.mod").write_text("module b\n\ngo 1.21\n", encoding="utf-8")

    n, _ = repair_from_sibling_manifests(
        str(tmp_path), "no required module provides package github.com/foo/bar/sub",
        ["svc-b/main.go"], "go")
    assert n == 1
    assert "github.com/foo v0.9.0" in (tgt / "go.mod").read_text()


# ── D13：cargo 注入保留 features（内联表整体移植）──

def test_d13_cargo_inline_table_transplanted_whole(tmp_path):
    """D13 核心：兄弟 tokio = { version = "1", features = ["full"] } → 整体移植，
    不再降成 tokio = "1"（feature-gated API 注入后即可用，不白烧一轮编译）。"""
    sib = tmp_path / "svc-a"
    sib.mkdir()
    (sib / "Cargo.toml").write_text(
        '[package]\nname = "a"\nversion = "0.1.0"\n\n'
        '[dependencies]\ntokio = { version = "1", features = ["full"] }\n', encoding="utf-8")
    tgt = tmp_path / "svc-b"
    tgt.mkdir()
    (tgt / "Cargo.toml").write_text(
        '[package]\nname = "b"\nversion = "0.1.0"\n\n[dependencies]\n', encoding="utf-8")

    n, _ = repair_from_sibling_manifests(
        str(tmp_path), "error[E0432]: unresolved import `tokio`", ["svc-b/src/main.rs"], "cargo")
    assert n == 1
    out = (tgt / "Cargo.toml").read_text()
    assert 'tokio = { version = "1", features = ["full"] }' in out, out


def test_d13_cargo_flat_form_still_injected(tmp_path):
    """平表 serde = "1.0.0" → 注入形态不变（回归）。"""
    sib = tmp_path / "svc-a"
    sib.mkdir()
    (sib / "Cargo.toml").write_text(
        '[package]\nname = "a"\n\n[dependencies]\nserde = "1.0.0"\n', encoding="utf-8")
    tgt = tmp_path / "svc-b"
    tgt.mkdir()
    (tgt / "Cargo.toml").write_text(
        '[package]\nname = "b"\n\n[dependencies]\n', encoding="utf-8")

    n, _ = repair_from_sibling_manifests(
        str(tmp_path), "error[E0432]: unresolved import `serde`", ["svc-b/src/lib.rs"], "cargo")
    assert n == 1
    assert 'serde = "1.0.0"' in (tgt / "Cargo.toml").read_text()


def test_d13_cargo_path_dep_inline_not_transplanted(tmp_path):
    """内联表含 path =（本地相对路径）→ 跨目录移植必错 → 不可作坐标源（与 npm file: 同款）。"""
    sib = tmp_path / "svc-a"
    sib.mkdir()
    (sib / "Cargo.toml").write_text(
        '[package]\nname = "a"\n\n[dependencies]\nfoo = { version = "0.2", path = "../foo" }\n',
        encoding="utf-8")
    tgt = tmp_path / "svc-b"
    tgt.mkdir()
    before = '[package]\nname = "b"\n\n[dependencies]\n'
    (tgt / "Cargo.toml").write_text(before, encoding="utf-8")

    n, _ = repair_from_sibling_manifests(
        str(tmp_path), "error[E0432]: unresolved import `foo`", ["svc-b/src/lib.rs"], "cargo")
    assert n == 0 and (tgt / "Cargo.toml").read_text() == before


def test_m2_cargo_spaced_section_header_no_duplicate_section(tmp_path):
    """M-2（批次6 R1 hunter+reviewer）：目标含内空白 section 头（`[ dependencies ]`，
    合法 TOML）时旧正则不匹配 → 追加第二个同名 section=非法 TOML 毒化目标。
    section 头容忍内空白 + 移植后 tomllib 校验双保险。"""
    import tomllib
    sib = tmp_path / "svc-a"
    sib.mkdir()
    (sib / "Cargo.toml").write_text(
        '[package]\nname = "a"\n\n[dependencies]\ntokio = { version = "1", features = ["full"] }\n',
        encoding="utf-8")
    tgt = tmp_path / "svc-b"
    tgt.mkdir()
    (tgt / "Cargo.toml").write_text(
        '[package]\nname = "b"\n\n[ dependencies ]\n', encoding="utf-8")

    n, _ = repair_from_sibling_manifests(
        str(tmp_path), "error[E0432]: unresolved import `tokio`", ["svc-b/src/main.rs"], "cargo")
    assert n == 1
    out = (tgt / "Cargo.toml").read_text()
    parsed = tomllib.loads(out)  # 非法 TOML=毒化交付物，必须抛错红灯
    assert out.count("[dependencies]") + out.count("[ dependencies ]") == 1, \
        f"绝不得追加第二个同名 section：{out}"
    assert parsed["dependencies"]["tokio"]["features"] == ["full"]


def test_m2_cargo_broken_target_fail_closed_no_poison(tmp_path):
    """M-2 fail-closed 臂：目标 manifest 注入前已非法 → 校验不过绝不落笔（不雪上加霜）。"""
    sib = tmp_path / "svc-a"
    sib.mkdir()
    (sib / "Cargo.toml").write_text(
        '[package]\nname = "a"\n\n[dependencies]\nserde = "1.0.0"\n', encoding="utf-8")
    tgt = tmp_path / "svc-b"
    tgt.mkdir()
    broken = '[package]\nname = "b"\n\n[dependencies]\nserde = \n'  # 非法 TOML（值缺失）
    (tgt / "Cargo.toml").write_text(broken, encoding="utf-8")

    n, _ = repair_from_sibling_manifests(
        str(tmp_path), "error[E0432]: unresolved import `serde`", ["svc-b/src/lib.rs"], "cargo")
    assert n == 0, "目标已非法时注入必须 fail-closed（校验不过不落笔）"
    assert (tgt / "Cargo.toml").read_text() == broken, "校验不过绝不改写目标文件"


# ── D14：注入落回【来源 section】（dev/build 依赖绝不进运行时区）──

def test_d14_npm_devdep_injected_into_devdependencies(tmp_path):
    """兄弟 devDependencies 的坐标 → 目标 devDependencies（不进运行时 dependencies）。"""
    sib = tmp_path / "web-a"
    sib.mkdir()
    (sib / "package.json").write_text(
        '{"name": "a", "devDependencies": {"vitest": "^1.0.0"}}', encoding="utf-8")
    tgt = tmp_path / "web-b"
    tgt.mkdir()
    (tgt / "package.json").write_text('{"name": "b", "dependencies": {}}', encoding="utf-8")

    n, _ = repair_from_sibling_manifests(
        str(tmp_path), "Error: Cannot find module 'vitest'", ["web-b/src/x.test.ts"], "npm")
    assert n == 1
    data = json.loads((tgt / "package.json").read_text())
    assert data["devDependencies"]["vitest"] == "^1.0.0"
    assert "vitest" not in data["dependencies"]


def test_d14_cargo_devdep_injected_into_dev_section(tmp_path):
    """兄弟 [dev-dependencies] 的坐标 → 目标 [dev-dependencies]（不进 [dependencies]）。"""
    sib = tmp_path / "svc-a"
    sib.mkdir()
    (sib / "Cargo.toml").write_text(
        '[package]\nname = "a"\n\n[dev-dependencies]\ntempfile = "3"\n', encoding="utf-8")
    tgt = tmp_path / "svc-b"
    tgt.mkdir()
    (tgt / "Cargo.toml").write_text(
        '[package]\nname = "b"\n\n[dependencies]\n', encoding="utf-8")

    n, _ = repair_from_sibling_manifests(
        str(tmp_path), "error[E0432]: unresolved import `tempfile`", ["svc-b/tests/t.rs"], "cargo")
    assert n == 1
    out = (tgt / "Cargo.toml").read_text()
    dep_idx = out.index("[dependencies]")
    dev_idx = out.index("[dev-dependencies]")
    tf_idx = out.index('tempfile = "3"')
    assert dev_idx < tf_idx and (dep_idx > tf_idx or dep_idx < dev_idx), out


def test_d14_sibling_coord_pick_deterministic(tmp_path):
    """D14：多兄弟同名不同版本 → 目录排序后首个（确定性，不随文件系统枚举序漂移）。"""
    for name, ver in (("svc-z", "1.0.0"), ("svc-a", "2.0.0")):
        d = tmp_path / name
        d.mkdir()
        (d / "package.json").write_text(
            json.dumps({"name": name, "dependencies": {"lodash": ver}}), encoding="utf-8")
    tgt = tmp_path / "web-b"
    tgt.mkdir()
    (tgt / "package.json").write_text('{"name": "b", "dependencies": {}}', encoding="utf-8")

    n, _ = repair_from_sibling_manifests(
        str(tmp_path), "Cannot find module 'lodash'", ["web-b/src/x.ts"], "npm")
    assert n == 1
    data = json.loads((tgt / "package.json").read_text())
    assert data["dependencies"]["lodash"] == "2.0.0", "排序后 svc-a 恒为首个坐标源"


# ── W-4：注入目标由构建失败输出的出错文件确定性定位（不再凭 modified 首文件猜）─────
def test_w4_cargo_workspace_member_targeted_by_failure_evidence(tmp_path):
    """workspace 成员失败：cargo `-->` 路径=工作区根相对（实证）→ 注成员 manifest；
    modified 首文件落在别的 crate 也不被带偏。"""
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["crates/foo", "crates/bar"]\n', encoding="utf-8")
    bar = tmp_path / "crates" / "bar"
    (bar / "src").mkdir(parents=True)
    (bar / "Cargo.toml").write_text(
        '[package]\nname = "bar"\n\n[dependencies]\nserde = "1.0.197"\n', encoding="utf-8")
    (bar / "src" / "lib.rs").write_text("pub fn b() {}", encoding="utf-8")
    foo = tmp_path / "crates" / "foo"
    (foo / "src").mkdir(parents=True)
    (foo / "Cargo.toml").write_text(
        '[package]\nname = "foo"\n\n[dependencies]\n', encoding="utf-8")
    (foo / "src" / "lib.rs").write_text("use serde::Serialize;", encoding="utf-8")
    out = ("error[E0432]: unresolved import `serde`\n"
           " --> crates/foo/src/lib.rs:1:5\n"
           "  |\n1 | use serde::Serialize;\n")
    n, paths = repair_from_sibling_manifests(
        str(tmp_path), out, ["crates/bar/src/lib.rs"], "cargo")  # modified 指向 bar=诱饵
    assert n == 1 and "crates/foo/Cargo.toml" in paths
    assert 'serde = "1.0.197"' in (foo / "Cargo.toml").read_text()
    assert "serde" not in (tmp_path / "Cargo.toml").read_text(), "绝不污染虚拟根"


def test_w4_go_nested_module_targeted_not_root(tmp_path):
    """嵌套 go module：失败证据指向 svc/a → 注 svc/a/go.mod，不注根 go.mod（根注=白烧）。"""
    (tmp_path / "go.mod").write_text("module example.com/root\n\ngo 1.22\n", encoding="utf-8")
    (tmp_path / "main.go").write_text("package main\n", encoding="utf-8")
    a = tmp_path / "svc" / "a"
    a.mkdir(parents=True)
    (a / "go.mod").write_text("module example.com/a\n\ngo 1.22\n", encoding="utf-8")
    (a / "main.go").write_text("package main\n", encoding="utf-8")
    b = tmp_path / "svc" / "b"
    b.mkdir(parents=True)
    (b / "go.mod").write_text(
        "module example.com/b\n\ngo 1.22\n\nrequire github.com/x/y v1.2.3\n", encoding="utf-8")
    out = ("svc/a/main.go:4:2: no required module provides package github.com/x/y; "
           "to add it:\n\tgo get github.com/x/y\n")
    n, paths = repair_from_sibling_manifests(
        str(tmp_path), out, ["main.go"], "go")  # modified 指向根=诱饵
    assert n == 1 and str(Path("svc/a/go.mod")) in paths
    assert "github.com/x/y v1.2.3" in (a / "go.mod").read_text()
    assert "github.com/x/y" not in (tmp_path / "go.mod").read_text(), "根 go.mod 不得被注"


def test_w4_evidence_unmappable_falls_back(tmp_path):
    """证据映射不回项目（../ 逃逸=外来/陈旧输出）→ 证据【不可用】而非证据反对：
    回退 modified 最近 manifest 旧行为（闸门整改：绝不掐死正常修复流）。"""
    a = tmp_path / "crate-a"
    a.mkdir()
    (a / "Cargo.toml").write_text(
        '[package]\nname = "a"\n\n[dependencies]\nserde = "1"\n', encoding="utf-8")
    tgt = tmp_path / "crate-b"
    (tgt / "src").mkdir(parents=True)
    (tgt / "Cargo.toml").write_text('[package]\nname = "b"\n\n[dependencies]\n', encoding="utf-8")
    (tgt / "src" / "lib.rs").write_text("use serde::Serialize;", encoding="utf-8")
    out = ("error[E0432]: unresolved import `serde`\n"
           " --> ../outside/src/lib.rs:9:1\n")
    n, paths = repair_from_sibling_manifests(
        str(tmp_path), out, ["crate-b/src/lib.rs"], "cargo")
    assert n == 1 and str(Path("crate-b/Cargo.toml")) in paths


def test_w4_failclosed_when_evidence_maps_but_no_manifest(tmp_path):
    """证据可用（映射进项目真文件）但祖先链无 manifest → fail-closed 不注：
    知道哪文件失败却定不出目标模块，猜=注错比不注更糟。"""
    a = tmp_path / "crate-a"
    a.mkdir()
    (a / "Cargo.toml").write_text(
        '[package]\nname = "a"\n\n[dependencies]\nserde = "1"\n', encoding="utf-8")
    orphan = tmp_path / "loose" / "src"
    orphan.mkdir(parents=True)
    (orphan / "lib.rs").write_text("use serde::Serialize;", encoding="utf-8")
    # 注意：项目根无 Cargo.toml，loose/ 下也无——证据可用但目标定不出
    out = ("error[E0432]: unresolved import `serde`\n"
           " --> loose/src/lib.rs:1:5\n")
    n, paths = repair_from_sibling_manifests(
        str(tmp_path), out, ["crate-a/src/lib.rs"], "cargo")
    assert n == 0 and paths == []


def test_w4_abs_path_sandbox_suffix_resolves(tmp_path):
    """沙箱/容器绝对路径输出（/workspace/... 前缀）：逐级剥前缀取后缀命中项目内真文件
    → 证据可用（reviewer MEDIUM：绝不因'绝对'整条丢弃回退 modified 猜=W-4 原病复发）。"""
    bar = tmp_path / "crates" / "bar"
    (bar / "src").mkdir(parents=True)
    (bar / "Cargo.toml").write_text(
        '[package]\nname = "bar"\n\n[dependencies]\nserde = "1"\n', encoding="utf-8")
    foo = tmp_path / "crates" / "foo"
    (foo / "src").mkdir(parents=True)
    (foo / "Cargo.toml").write_text('[package]\nname = "foo"\n\n[dependencies]\n', encoding="utf-8")
    (foo / "src" / "lib.rs").write_text("use serde::Serialize;", encoding="utf-8")
    out = ("error[E0432]: unresolved import `serde`\n"
           " --> /workspace/sandbox-17/crates/foo/src/lib.rs:1:5\n")
    n, paths = repair_from_sibling_manifests(
        str(tmp_path), out, ["crates/bar/src/lib.rs"], "cargo")  # modified 指向 bar=诱饵
    assert n == 1 and str(Path("crates/foo/Cargo.toml")) in paths


def test_w4_abs_path_local_in_project(tmp_path):
    """本地绝对输出：路径本身就在项目内 → 直接作证据。"""
    sib = tmp_path / "api"
    sib.mkdir()
    (sib / "package.json").write_text(json.dumps(
        {"name": "api", "dependencies": {"axios": "^1.6.0"}}), encoding="utf-8")
    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    (web / "package.json").write_text(
        json.dumps({"name": "web", "dependencies": {}}), encoding="utf-8")
    (web / "src" / "index.ts").write_text("import axios from 'axios';", encoding="utf-8")
    out = f"{tmp_path}/web/src/index.ts(3,23): error TS2307: Cannot find module 'axios'.\n"
    n, paths = repair_from_sibling_manifests(
        str(tmp_path), out, ["api/src/main.ts"], "npm")
    assert n == 1 and str(Path("web/package.json")) in paths


def test_w4_no_evidence_falls_back_to_modified(tmp_path):
    """输出无出错文件证据（纯模块名报错）→ 维持既有 modified 最近 manifest 回退。"""
    sib = tmp_path / "api"
    sib.mkdir()
    (sib / "package.json").write_text(json.dumps(
        {"name": "api", "dependencies": {"axios": "^1.6.0"}}), encoding="utf-8")
    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    (web / "package.json").write_text(
        json.dumps({"name": "web", "dependencies": {}}), encoding="utf-8")
    (web / "src" / "index.ts").write_text("import axios from 'axios';", encoding="utf-8")
    n, paths = repair_from_sibling_manifests(
        str(tmp_path), "Cannot find module 'axios'", ["web/src/index.ts"], "npm")
    assert n == 1 and str(Path("web/package.json")) in paths


def test_w4_npm_tsc_evidence_wins_over_modified(tmp_path):
    """tsc `path(l,c): error TS2307` 证据定位 web 包；modified 首文件在 api=诱饵不生效。"""
    api = tmp_path / "api"
    (api / "src").mkdir(parents=True)
    (api / "package.json").write_text(json.dumps(
        {"name": "api", "dependencies": {"axios": "^1.6.0"}}), encoding="utf-8")
    (api / "src" / "main.ts").write_text("export {};", encoding="utf-8")
    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    (web / "package.json").write_text(
        json.dumps({"name": "web", "dependencies": {}}), encoding="utf-8")
    (web / "src" / "index.ts").write_text("import axios from 'axios';", encoding="utf-8")
    out = "web/src/index.ts(3,23): error TS2307: Cannot find module 'axios'.\n"
    n, paths = repair_from_sibling_manifests(
        str(tmp_path), out, ["api/src/main.ts"], "npm")
    assert n == 1 and str(Path("web/package.json")) in paths
    got = json.loads((web / "package.json").read_text())
    assert got["dependencies"]["axios"] == "^1.6.0"


def test_w4_evidence_in_skip_dirs_never_injection_target(tmp_path):
    """闸门 R2 reviewer MEDIUM①：证据落产物/第三方目录（node_modules）→ 不作注入目标
    （否则依赖注给第三方包的 package.json=注错还自以为修复）；证据不算项目内可用，
    回退 modified 最近 manifest。"""
    third = tmp_path / "node_modules" / "somepkg" / "dist"
    third.mkdir(parents=True)
    (third / "index.js").write_text("require('axios');", encoding="utf-8")
    (tmp_path / "node_modules" / "somepkg" / "package.json").write_text(
        json.dumps({"name": "somepkg", "dependencies": {}}), encoding="utf-8")
    api = tmp_path / "api"
    api.mkdir()
    (api / "package.json").write_text(json.dumps(
        {"name": "api", "dependencies": {"axios": "^1.6.0"}}), encoding="utf-8")
    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    (web / "package.json").write_text(
        json.dumps({"name": "web", "dependencies": {}}), encoding="utf-8")
    (web / "src" / "index.ts").write_text("import axios from 'axios';", encoding="utf-8")
    out = ("ERROR in ./node_modules/somepkg/dist/index.js\n"
           "Module not found: Can't resolve 'axios'\n")
    n, paths = repair_from_sibling_manifests(
        str(tmp_path), out, ["web/src/index.ts"], "npm")
    assert str(Path("node_modules/somepkg/package.json")) not in paths, \
        "第三方 manifest 绝不作注入目标"
    assert n == 1 and str(Path("web/package.json")) in paths, \
        "node_modules 证据不算项目内可用 → 回退 modified 最近 manifest"


def test_w4_evidence_cap_truncation_logged(tmp_path, caplog):
    """闸门 R2 hunter LOW-3：出错文件证据超 cap 截断必须可观测（C13 同型纪律）——
    病态输出场景被丢弃者可能含项目内报错。"""
    import logging
    a = tmp_path / "crate-a"
    a.mkdir()
    (a / "Cargo.toml").write_text(
        '[package]\nname = "a"\n\n[dependencies]\nserde = "1"\n', encoding="utf-8")
    tgt = tmp_path / "crate-b"
    (tgt / "src").mkdir(parents=True)
    (tgt / "Cargo.toml").write_text('[package]\nname = "b"\n\n[dependencies]\n', encoding="utf-8")
    (tgt / "src" / "lib.rs").write_text("use serde::Serialize;", encoding="utf-8")
    out = "".join(f"error[E0432]: unresolved import `serde`\n --> ghost{i}/src/lib.rs:1:5\n"
                  for i in range(25))
    with caplog.at_level(logging.WARNING):
        repair_from_sibling_manifests(str(tmp_path), out, ["crate-b/src/lib.rs"], "cargo")
    assert any("超 cap" in r.message for r in caplog.records), \
        "证据截断必须 WARNING 可观测"
