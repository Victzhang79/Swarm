"""27 号文 P-H1：非 JVM 栈等价接地事实（npm ESM/CJS、go module 前缀/版本、python 包根）。

锁的命题：
  · 三探测器各自自门控（无对应清单 → None，不污染异栈画像——jvm 同律）；
  · 事实保真（go module 路径大小写不丢、npm "type" 显式 vs 缺省机读可辨）；
  · 解析/读取/枚举失败各自一声 WARNING（硬检查④：失败 ≠ 真没有）；
  · 接线：detect_stack_deterministic → 画像键 → format_stack_for_prompt 硬约束行，
    全链路走真实调用点（把 attach 一行改没，wiring 测试必须红）；
  · 老画像（回放/缓存 profile 无新键）不猜不渲染（lombok 同律）。
"""
from __future__ import annotations

import logging
import os

import pytest

from swarm.brain import stack_detect as sd
from swarm.brain.stack_detect import (
    _detect_go_facts,
    _detect_npm_facts,
    _detect_python_facts,
    detect_stack_deterministic,
    format_stack_for_prompt,
)

_SD_LOGGER = "swarm.brain.stack_detect"


# ── npm ──────────────────────────────────────────────────────────────

def test_npm_esm_facts_and_render(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"name": "web", "type": "module", "engines": {"node": ">=18"}}')
    facts = _detect_npm_facts(str(tmp_path))
    assert facts == {"module_system": "esm", "module_system_source": "explicit",
                     "node_engines": ">=18"}
    out = format_stack_for_prompt({"npm_facts": facts})
    assert "模块系统·硬约束" in out and "require is not defined" in out
    assert "`.js` 扩展名" in out
    assert "engines.node = `>=18`" in out


def test_npm_cjs_default_vs_explicit(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    facts = _detect_npm_facts(str(tmp_path))
    assert facts == {"module_system": "cjs", "module_system_source": "default"}
    out = format_stack_for_prompt({"npm_facts": facts})
    assert "CommonJS" in out and "缺省即 CommonJS" in out
    assert "ERR_REQUIRE_ESM" in out

    (tmp_path / "package.json").write_text('{"type": "commonjs"}')
    facts2 = _detect_npm_facts(str(tmp_path))
    assert facts2["module_system_source"] == "explicit"
    out2 = format_stack_for_prompt({"npm_facts": facts2})
    assert '"type": "commonjs"' in out2


def test_npm_malformed_json_warns_not_silent(tmp_path, caplog):
    (tmp_path / "package.json").write_text("{ not json !!!")
    with caplog.at_level(logging.WARNING, logger=_SD_LOGGER):
        facts = _detect_npm_facts(str(tmp_path))
    assert facts is None
    hits = [r for r in caplog.records
            if r.levelno == logging.WARNING and "P-H1" in r.getMessage()]
    assert hits, "解析失败必须有一声 WARNING——自吞=「失败」与「真没有」不可辨（硬检查④）"


def test_npm_absent_returns_none(tmp_path):
    assert _detect_npm_facts(str(tmp_path)) is None


# ── go ───────────────────────────────────────────────────────────────

def test_go_facts_case_preserved_and_render(tmp_path):
    # module 路径可含大小写（BurntSushi 是真实主流模块）——_read_text 小写化会腐蚀硬约束
    (tmp_path / "go.mod").write_text(
        "module github.com/BurntSushi/toml\n\ngo 1.21 // 最低版本\n")
    facts = _detect_go_facts(str(tmp_path))
    assert facts == {"module_path": "github.com/BurntSushi/toml", "go_version": "1.21"}
    out = format_stack_for_prompt({"go_facts": facts})
    assert "module 路径·硬约束" in out
    assert "`github.com/BurntSushi/toml`（go 1.21）" in out
    assert "replace" in out


def test_go_version_absent_still_renders_prefix(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/api\n")
    facts = _detect_go_facts(str(tmp_path))
    assert facts == {"module_path": "example.com/api"}
    out = format_stack_for_prompt({"go_facts": facts})
    assert "`example.com/api`" in out and "（go " not in out


def test_go_mod_unreadable_warns_not_silent(tmp_path, caplog):
    mod = tmp_path / "go.mod"
    mod.write_text("module example.com/api\n")
    os.chmod(mod, 0)
    try:
        with caplog.at_level(logging.WARNING, logger=_SD_LOGGER):
            facts = _detect_go_facts(str(tmp_path))
    finally:
        os.chmod(mod, 0o644)
    assert facts is None
    assert any(r.levelno == logging.WARNING and "P-H1" in r.getMessage()
               for r in caplog.records)


def test_go_absent_returns_none(tmp_path):
    assert _detect_go_facts(str(tmp_path)) is None


# ── python ───────────────────────────────────────────────────────────

def _py_project(tmp_path, *, src_layout: bool) -> str:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nrequires-python = ">=3.10"\n')
    pkg_parent = tmp_path / "src" if src_layout else tmp_path
    pkg_parent.mkdir(exist_ok=True)
    (pkg_parent / "mypkg").mkdir()
    (pkg_parent / "mypkg" / "__init__.py").write_text("")
    return str(tmp_path)


def test_python_src_layout_facts_and_render(tmp_path):
    proj = _py_project(tmp_path, src_layout=True)
    facts = _detect_python_facts(proj, {"pyproject.toml": "x"})
    assert facts == {"requires_python": ">=3.10", "import_roots": ["mypkg"],
                     "src_layout": True}
    out = format_stack_for_prompt({"python_facts": facts})
    assert "包结构·硬约束" in out and "`mypkg`" in out
    assert "`src.` 前缀" in out  # src 布局的核心硬约束：import 路径不含 src.
    assert "requires-python = `>=3.10`" in out


def test_python_flat_layout_filters_non_packages(tmp_path):
    proj = _py_project(tmp_path, src_layout=False)
    # 非包目录（无 __init__.py）/ skip 名单里的 tests / 点开头隐藏目录——都不算 import 根
    (tmp_path / "utils").mkdir()                      # 无 __init__.py
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "__init__.py").write_text("")
    facts = _detect_python_facts(proj, {"pyproject.toml": "x"})
    assert facts["import_roots"] == ["mypkg"]
    assert "src_layout" not in facts
    out = format_stack_for_prompt({"python_facts": facts})
    assert "`src.` 前缀" not in out  # 非 src 布局不渲染该子句（不猜）


def test_python_requires_from_setup_py(tmp_path):
    (tmp_path / "setup.py").write_text("from setuptools import setup\n")
    facts = _detect_python_facts(
        str(tmp_path), {"setup.py": 'setup(name="demo", python_requires=">=3.8")'})
    assert facts == {"requires_python": ">=3.8"}


def test_python_no_packages_no_render(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask==3.0.0\n")
    facts = _detect_python_facts(str(tmp_path), {"requirements.txt": "x"})
    assert facts is None
    out = format_stack_for_prompt({"python_facts": {}})
    assert "包结构·硬约束" not in out


def test_python_absent_manifest_returns_none(tmp_path):
    assert _detect_python_facts(str(tmp_path), {}) is None


def test_python_pyproject_unreadable_warns(tmp_path, caplog):
    pj = tmp_path / "pyproject.toml"
    pj.write_text('[project]\nrequires-python = ">=3.10"\n')
    (tmp_path / "mypkg").mkdir()
    (tmp_path / "mypkg" / "__init__.py").write_text("")
    os.chmod(pj, 0)
    try:
        with caplog.at_level(logging.WARNING, logger=_SD_LOGGER):
            facts = _detect_python_facts(str(tmp_path), {"pyproject.toml": "x"})
    finally:
        os.chmod(pj, 0o644)
    # 读取失败=该路事实缺席，但其余路（包根枚举）不受影响——失败必须可辨
    assert "requires_python" not in facts
    assert facts["import_roots"] == ["mypkg"]
    assert any(r.levelno == logging.WARNING and "P-H1" in r.getMessage()
               for r in caplog.records)


def test_python_src_enum_failure_warns(tmp_path, caplog):
    proj = _py_project(tmp_path, src_layout=True)
    os.chmod(os.path.join(proj, "src"), 0)
    try:
        with caplog.at_level(logging.WARNING, logger=_SD_LOGGER):
            facts = _detect_python_facts(proj, {"pyproject.toml": "x"})
    finally:
        os.chmod(os.path.join(proj, "src"), 0o755)
    assert "import_roots" not in (facts or {})
    assert any(r.levelno == logging.WARNING and "枚举失败" in r.getMessage()
               for r in caplog.records)


# ── 跨栈不污染 + 全链路接线 + 老画像兼容 ─────────────────────────────

def test_jvm_project_unpolluted(tmp_path):
    (tmp_path / "pom.xml").write_text("<project></project>")
    prof = detect_stack_deterministic(str(tmp_path))
    assert prof["npm_facts"] == {} and prof["go_facts"] == {} and prof["python_facts"] == {}


def test_wiring_npm_profile_reaches_render_via_real_caller(tmp_path):
    """接线锁：真实 detect → 画像键 → format_stack_for_prompt，全链路走生产调用点。

    把装配点 `\"npm_facts\": npm_facts or {}` 改成 `{}`（机制造对了却没接上），本条必须红。
    """
    (tmp_path / "package.json").write_text('{"type": "module"}')
    prof = detect_stack_deterministic(str(tmp_path))
    assert prof["npm_facts"]["module_system"] == "esm"
    out = format_stack_for_prompt(prof)
    assert "模块系统·硬约束" in out and "require is not defined" in out
    assert any("npm 模块系统" in e for e in prof["evidence"])


def test_wiring_go_profile_reaches_render_via_real_caller(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/api\n\ngo 1.21\n")
    (tmp_path / "main.go").write_text("package main\nfunc main(){}\n")
    prof = detect_stack_deterministic(str(tmp_path))
    assert prof["go_facts"]["module_path"] == "example.com/api"
    out = format_stack_for_prompt(prof)
    assert "module 路径·硬约束" in out and "`example.com/api`（go 1.21）" in out


def test_wiring_python_profile_reaches_render_via_real_caller(tmp_path):
    """REV-2：python attach 也曾是静默 no-op 窗口——本锁补上（与 npm/go 两臂同形）。"""
    proj = _py_project(tmp_path, src_layout=True)
    prof = detect_stack_deterministic(proj)
    assert prof["python_facts"]["import_roots"] == ["mypkg"]
    assert prof["python_facts"]["requires_python"] == ">=3.10"
    out = format_stack_for_prompt(prof)
    assert "包结构·硬约束" in out and "`mypkg`" in out and "`src.` 前缀" in out


def test_render_old_cached_profile_without_new_keys():
    """老画像/回放 profile 无新键：不猜不渲染不崩（lombok 键缺席同律）。"""
    out = format_stack_for_prompt({"frontend": "无", "backend": "Flask (python)",
                                   "build": "pip", "frontend_kind": "none"})
    assert "模块系统·硬约束" not in out
    assert "module 路径·硬约束" not in out
    assert "包结构·硬约束" not in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
