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
