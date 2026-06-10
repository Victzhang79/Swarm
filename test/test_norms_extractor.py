#!/usr/bin/env python3
"""测试 norms_extractor: 从配置文件自动提取项目规范"""

from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path

# 加载 bootstrap，使 `from swarm.xxx` 可用
_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.knowledge.norms_extractor import extract_norms_from_project
from swarm.knowledge.norms_store import Norm


def _make_project(tmp: Path, files: dict[str, str]) -> Path:
    """在 tmp 下创建项目目录，files: {相对路径: 内容}"""
    proj = tmp / "test_proj"
    proj.mkdir(exist_ok=True)
    for rel, content in files.items():
        p = proj / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return proj


# ──────────────────────────────────────────────
# 测试用例
# ──────────────────────────────────────────────

def test_editorconfig(tmp_path: Path) -> None:
    proj = _make_project(tmp_path, {
        ".editorconfig": textwrap.dedent("""\
            root = true
            [*]
            indent_style = space
            indent_size = 4
            end_of_line = lf
            charset = utf-8
            max_line_length = 120
            trim_trailing_whitespace = true
            insert_final_newline = true
        """),
    })
    norms = extract_norms_from_project(proj)
    assert len(norms) == 1
    n = norms[0]
    assert n.tag == "auto"
    assert n.metadata["source"] == ".editorconfig"
    assert "缩进风格: space" in n.content
    assert "最大行长: 120" in n.content
    assert "行尾符: lf" in n.content
    print("  ✅ .editorconfig 提取")


def test_pyproject_toml_ruff(tmp_path: Path) -> None:
    proj = _make_project(tmp_path, {
        "pyproject.toml": textwrap.dedent("""\
            [tool.ruff]
            line-length = 88
            target-version = "py311"

            [tool.ruff.lint]
            select = ["E", "F", "I"]
            ignore = ["E501"]
        """),
    })
    norms = extract_norms_from_project(proj)
    # 可能有 ruff + 其他，至少找到 ruff
    ruff_norms = [n for n in norms if "Ruff" in n.title]
    assert len(ruff_norms) == 1
    n = ruff_norms[0]
    assert n.tag == "auto"
    assert "88" in n.content
    assert "py311" in n.content
    assert "E, F, I" in n.content
    print("  ✅ pyproject.toml [tool.ruff] 提取")


def test_pyproject_toml_black(tmp_path: Path) -> None:
    proj = _make_project(tmp_path, {
        "pyproject.toml": textwrap.dedent("""\
            [tool.black]
            line-length = 100
            target-version = ["py310", "py311"]
        """),
    })
    norms = extract_norms_from_project(proj)
    black_norms = [n for n in norms if "Black" in n.title]
    assert len(black_norms) == 1
    n = black_norms[0]
    assert "100" in n.content
    assert "py310" in n.content
    print("  ✅ pyproject.toml [tool.black] 提取")


def test_ruff_toml(tmp_path: Path) -> None:
    proj = _make_project(tmp_path, {
        ".ruff.toml": textwrap.dedent("""\
            line-length = 99
            target-version = "py312"

            [lint]
            select = ["E", "F"]
            ignore = ["E501", "E402"]
        """),
    })
    norms = extract_norms_from_project(proj)
    ruff_norms = [n for n in norms if "Ruff" in n.title]
    assert len(ruff_norms) == 1
    n = ruff_norms[0]
    assert "99" in n.content
    assert n.metadata["source"] == ".ruff.toml"
    print("  ✅ .ruff.toml 提取")


def test_setup_cfg_flake8(tmp_path: Path) -> None:
    proj = _make_project(tmp_path, {
        "setup.cfg": textwrap.dedent("""\
            [flake8]
            max-line-length = 120
            ignore = E501,W503
            exclude = .git,__pycache__,build
        """),
    })
    norms = extract_norms_from_project(proj)
    flake_norms = [n for n in norms if "Flake8" in n.title]
    assert len(flake_norms) == 1
    n = flake_norms[0]
    assert "120" in n.content
    assert "E501,W503" in n.content
    print("  ✅ setup.cfg [flake8] 提取")


def test_eslintrc_json(tmp_path: Path) -> None:
    proj = _make_project(tmp_path, {
        ".eslintrc.json": json.dumps({
            "extends": ["eslint:recommended", "plugin:@typescript-eslint/recommended"],
            "rules": {
                "no-unused-vars": "warn",
                "semi": ["error", "always"],
            },
        }),
    })
    norms = extract_norms_from_project(proj)
    eslint_norms = [n for n in norms if "ESLint" in n.title]
    assert len(eslint_norms) == 1
    n = eslint_norms[0]
    assert "eslint:recommended" in n.content
    assert "semi" in n.content
    print("  ✅ .eslintrc.json 提取")


def test_eslintrc_js(tmp_path: Path) -> None:
    """JS 配置文件无法解析内容，但应标记存在"""
    proj = _make_project(tmp_path, {
        ".eslintrc.js": 'module.exports = { rules: {} };',
    })
    norms = extract_norms_from_project(proj)
    eslint_norms = [n for n in norms if "ESLint" in n.title]
    assert len(eslint_norms) == 1
    assert ".eslintrc.js" in eslint_norms[0].content
    print("  ✅ .eslintrc.js 存在检测")


def test_prettierrc(tmp_path: Path) -> None:
    proj = _make_project(tmp_path, {
        ".prettierrc.json": json.dumps({
            "printWidth": 100,
            "tabWidth": 2,
            "semi": True,
            "singleQuote": True,
            "trailingComma": "es5",
        }),
    })
    norms = extract_norms_from_project(proj)
    prettier_norms = [n for n in norms if "Prettier" in n.title]
    assert len(prettier_norms) == 1
    n = prettier_norms[0]
    assert "行宽上限: 100" in n.content
    assert "单引号: True" in n.content
    print("  ✅ .prettierrc.json 提取")


def test_pom_xml(tmp_path: Path) -> None:
    proj = _make_project(tmp_path, {
        "pom.xml": textwrap.dedent("""\
            <project>
              <build>
                <plugins>
                  <plugin>
                    <groupId>org.apache.maven.plugins</groupId>
                    <artifactId>maven-checkstyle-plugin</artifactId>
                  </plugin>
                  <plugin>
                    <groupId>com.diffplug.spotless</groupId>
                    <artifactId>spotless-maven-plugin</artifactId>
                  </plugin>
                </plugins>
              </build>
            </project>
        """),
    })
    norms = extract_norms_from_project(proj)
    pom_norms = [n for n in norms if "pom.xml" in n.title]
    assert len(pom_norms) == 1
    n = pom_norms[0]
    assert "Checkstyle" in n.content
    assert "Spotless" in n.content
    print("  ✅ pom.xml 插件检测")


def test_no_config_files(tmp_path: Path) -> None:
    """项目没有任何配置文件时应返回空列表"""
    proj = _make_project(tmp_path, {
        "main.py": "print('hello')",
    })
    norms = extract_norms_from_project(proj)
    assert norms == []
    print("  ✅ 无配置文件返回空列表")


def test_nonexistent_path(tmp_path: Path) -> None:
    """路径不存在应返回空列表不报错"""
    norms = extract_norms_from_project(tmp_path / "nonexistent")
    assert norms == []
    print("  ✅ 不存在路径返回空列表")


def test_malformed_config_graceful(tmp_path: Path) -> None:
    """解析失败时应优雅跳过"""
    proj = _make_project(tmp_path, {
        "pyproject.toml": "this is not valid toml [[[",
        ".eslintrc.json": "{invalid json",
        ".prettierrc.json": "not json",
    })
    # 不应该抛异常
    norms = extract_norms_from_project(proj)
    # 可能部分解析成功，也可能全失败，关键是不断裂
    assert isinstance(norms, list)
    print("  ✅ 损坏配置文件优雅跳过")


def test_all_tag_auto(tmp_path: Path) -> None:
    """所有提取的 Norm 都应是 tag='auto'"""
    proj = _make_project(tmp_path, {
        ".editorconfig": "[*]\nindent_style = tab\n",
        "pyproject.toml": "[tool.ruff]\nline-length = 88\n",
    })
    norms = extract_norms_from_project(proj)
    assert all(n.tag == "auto" for n in norms), [n.tag for n in norms]
    print("  ✅ 全部 tag='auto'")


def test_package_json_eslint_config(tmp_path: Path) -> None:
    """package.json 中的 eslintConfig 字段"""
    proj = _make_project(tmp_path, {
        "package.json": json.dumps({
            "name": "test",
            "eslintConfig": {
                "extends": "next/core-web-vitals",
                "rules": {"no-console": "warn"},
            },
        }),
    })
    norms = extract_norms_from_project(proj)
    eslint_norms = [n for n in norms if "ESLint" in n.title]
    assert len(eslint_norms) == 1
    assert "next/core-web-vitals" in eslint_norms[0].content
    print("  ✅ package.json eslintConfig 提取")


def test_norm_structure(tmp_path: Path) -> None:
    """验证 Norm 数据结构完整性"""
    proj = _make_project(tmp_path, {
        ".editorconfig": "[*]\nindent_size = 2\n",
    })
    norms = extract_norms_from_project(proj)
    assert len(norms) == 1
    n = norms[0]
    assert isinstance(n, Norm)
    assert isinstance(n.title, str) and n.title
    assert isinstance(n.content, str) and n.content
    assert isinstance(n.tag, str)
    assert isinstance(n.priority, int)
    assert isinstance(n.metadata, dict)
    assert "source" in n.metadata
    print("  ✅ Norm 数据结构完整")


# ──────────────────────────────────────────────
# 运行入口
# ──────────────────────────────────────────────

def main() -> int:
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="norms_test_"))

    tests = [
        lambda: test_editorconfig(tmp / "t1"),
        lambda: test_pyproject_toml_ruff(tmp / "t2"),
        lambda: test_pyproject_toml_black(tmp / "t3"),
        lambda: test_ruff_toml(tmp / "t4"),
        lambda: test_setup_cfg_flake8(tmp / "t5"),
        lambda: test_eslintrc_json(tmp / "t6"),
        lambda: test_eslintrc_js(tmp / "t7"),
        lambda: test_prettierrc(tmp / "t8"),
        lambda: test_pom_xml(tmp / "t9"),
        lambda: test_no_config_files(tmp / "t10"),
        lambda: test_nonexistent_path(tmp / "t11"),
        lambda: test_malformed_config_graceful(tmp / "t12"),
        lambda: test_all_tag_auto(tmp / "t13"),
        lambda: test_package_json_eslint_config(tmp / "t14"),
        lambda: test_norm_structure(tmp / "t15"),
    ]

    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
            import traceback
            traceback.print_exc()

    if failed:
        print(f"\n{failed}/{len(tests)} failed")
        return 1
    print(f"\n✅ 全部 {len(tests)} 项 norms_extractor 测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
