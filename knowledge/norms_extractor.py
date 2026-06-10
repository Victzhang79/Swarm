"""Layer C — 项目规范自动提取: 从配置文件扫描并生成 Norm 记录

支持:
- .editorconfig: 缩进风格/缩进大小/行尾/字符集/最大行长
- pyproject.toml [tool.ruff]/[tool.black]: line-length, target-version, lint rules
- .ruff.toml / ruff.toml: 同上
- setup.cfg [flake8]: max-line-length 等
- .eslintrc(.json)/package.json eslintConfig: ESLint 使用 + 关键规则
- .prettierrc(.json): 格式化规范
- pom.xml: checkstyle/spotless 插件检测
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from swarm.knowledge.norms_store import Norm

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 主入口: 纯提取，不写库
# ──────────────────────────────────────────────

def extract_norms_from_project(project_path: str | Path) -> list[Norm]:
    """扫描项目根目录的配置文件，提取规范返回 Norm 列表

    Args:
        project_path: 项目根目录路径
    Returns:
        提取到的 Norm 列表（tag='auto'）
    """
    root = Path(project_path)
    if not root.is_dir():
        logger.warning("项目路径不存在或不是目录: %s", project_path)
        return []

    norms: list[Norm] = []

    # 依次扫描各种配置文件
    _extract_editorconfig(root, norms)
    _extract_pyproject_toml(root, norms)
    _extract_ruff_toml(root, norms)
    _extract_setup_cfg(root, norms)
    _extract_eslintrc(root, norms)
    _extract_prettierrc(root, norms)
    _extract_pom_xml(root, norms)

    return norms


# ──────────────────────────────────────────────
# .editorconfig
# ──────────────────────────────────────────────

def _extract_editorconfig(root: Path, norms: list[Norm]) -> None:
    path = root / ".editorconfig"
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return

    # 简易解析（不依赖第三方库）
    items: list[str] = []

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().lower()
        val = val.strip()
        if key in ("indent_style", "indent_size", "end_of_line", "charset", "max_line_length", "trim_trailing_whitespace", "insert_final_newline"):
            # 映射中文可读名
            label_map = {
                "indent_style": "缩进风格",
                "indent_size": "缩进大小",
                "end_of_line": "行尾符",
                "charset": "字符集",
                "max_line_length": "最大行长",
                "trim_trailing_whitespace": "去除尾部空白",
                "insert_final_newline": "末尾插入换行",
            }
            items.append(f"{label_map.get(key, key)}: {val}")

    if items:
        norms.append(Norm(
            title="代码风格: EditorConfig",
            content="项目使用 .editorconfig 约定编辑器配置:\n" + "\n".join(f"- {i}" for i in items),
            tag="auto",
            priority=5,
            metadata={"source": ".editorconfig"},
        ))


# ──────────────────────────────────────────────
# pyproject.toml [tool.ruff] / [tool.black]
# ──────────────────────────────────────────────

def _extract_pyproject_toml(root: Path, norms: list[Norm]) -> None:
    path = root / "pyproject.toml"
    if not path.is_file():
        return
    try:
        data = _parse_toml(path)
    except Exception:
        return
    if not data:
        return

    tools = data.get("tool", {})

    # [tool.ruff]
    ruff = tools.get("ruff", {})
    if ruff:
        items: list[str] = []
        if "line-length" in ruff:
            items.append(f"行长度上限: {ruff['line-length']}")
        if "target-version" in ruff:
            tv = ruff["target-version"]
            if isinstance(tv, list):
                items.append(f"目标 Python 版本: {', '.join(tv)}")
            else:
                items.append(f"目标 Python 版本: {tv}")
        # [tool.ruff.lint]
        lint = ruff.get("lint", {})
        select = lint.get("select", [])
        if select:
            items.append(f"启用的 lint 规则组: {', '.join(select) if isinstance(select, list) else select}")
        ignore = lint.get("ignore", [])
        if ignore:
            items.append(f"忽略的 lint 规则: {', '.join(ignore) if isinstance(ignore, list) else ignore}")
        # 兼容旧格式 [tool.ruff] select/ignore
        if not select and "select" in ruff:
            s = ruff["select"]
            items.append(f"启用的 lint 规则组: {', '.join(s) if isinstance(s, list) else s}")
        if not ignore and "ignore" in ruff:
            ig = ruff["ignore"]
            items.append(f"忽略的 lint 规则: {', '.join(ig) if isinstance(ig, list) else ig}")

        if items:
            norms.append(Norm(
                title="代码风格: Ruff (pyproject.toml)",
                content="项目使用 Ruff 做代码检查:\n" + "\n".join(f"- {i}" for i in items),
                tag="auto",
                priority=5,
                metadata={"source": "pyproject.toml [tool.ruff]"},
            ))

    # [tool.black]
    black = tools.get("black", {})
    if black:
        items = []
        if "line-length" in black:
            items.append(f"行长度上限: {black['line-length']}")
        if "target-version" in black:
            tv = black["target-version"]
            if isinstance(tv, list):
                items.append(f"目标 Python 版本: {', '.join(tv)}")
            else:
                items.append(f"目标 Python 版本: {tv}")
        if items:
            norms.append(Norm(
                title="代码风格: Black (pyproject.toml)",
                content="项目使用 Black 做代码格式化:\n" + "\n".join(f"- {i}" for i in items),
                tag="auto",
                priority=5,
                metadata={"source": "pyproject.toml [tool.black]"},
            ))


# ──────────────────────────────────────────────
# .ruff.toml / ruff.toml
# ──────────────────────────────────────────────

def _extract_ruff_toml(root: Path, norms: list[Norm]) -> None:
    for name in (".ruff.toml", "ruff.toml"):
        path = root / name
        if not path.is_file():
            continue
        try:
            data = _parse_toml(path)
        except Exception:
            continue
        if not data:
            continue

        items: list[str] = []
        if "line-length" in data:
            items.append(f"行长度上限: {data['line-length']}")
        if "target-version" in data:
            tv = data["target-version"]
            if isinstance(tv, list):
                items.append(f"目标 Python 版本: {', '.join(tv)}")
            else:
                items.append(f"目标 Python 版本: {tv}")
        lint = data.get("lint", {})
        select = lint.get("select", [])
        if select:
            items.append(f"启用的 lint 规则组: {', '.join(select) if isinstance(select, list) else select}")
        ignore = lint.get("ignore", [])
        if ignore:
            items.append(f"忽略的 lint 规则: {', '.join(ignore) if isinstance(ignore, list) else ignore}")

        if items:
            norms.append(Norm(
                title=f"代码风格: Ruff ({name})",
                content="项目使用 Ruff 做代码检查:\n" + "\n".join(f"- {i}" for i in items),
                tag="auto",
                priority=5,
                metadata={"source": name},
            ))


# ──────────────────────────────────────────────
# setup.cfg [flake8]
# ──────────────────────────────────────────────

def _extract_setup_cfg(root: Path, norms: list[Norm]) -> None:
    path = root / "setup.cfg"
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return

    # 简易解析 [flake8] 段
    in_flake8 = False
    items: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_flake8 = stripped.lower() == "[flake8]"
            continue
        if not in_flake8:
            continue
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        if "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        key = key.strip().lower()
        val = val.strip()
        label_map = {
            "max-line-length": "最大行长",
            "max-line-complexity": "最大复杂度",
            "indent-size": "缩进大小",
            "ignore": "忽略规则",
            "select": "启用规则",
            "exclude": "排除目录",
            "per-file-ignores": "文件级忽略",
        }
        if key in label_map:
            items.append(f"{label_map[key]}: {val}")

    if items:
        norms.append(Norm(
            title="代码风格: Flake8 (setup.cfg)",
            content="项目使用 Flake8 做代码检查:\n" + "\n".join(f"- {i}" for i in items),
            tag="auto",
            priority=5,
            metadata={"source": "setup.cfg [flake8]"},
        ))


# ──────────────────────────────────────────────
# .eslintrc(.json/.js/.yml) / package.json eslintConfig
# ──────────────────────────────────────────────

def _extract_eslintrc(root: Path, norms: list[Norm]) -> None:
    # 尝试读取各种 eslint 配置
    eslint_data: dict[str, Any] | None = None
    source = ""

    # .eslintrc.json
    for name in (".eslintrc.json", ".eslintrc"):
        path = root / name
        if path.is_file():
            try:
                eslint_data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
                source = name
                break
            except (json.JSONDecodeError, OSError):
                continue

    # .eslintrc.js — 无法安全解析 JS，只标记存在
    if eslint_data is None:
        for name in (".eslintrc.js", ".eslintrc.cjs"):
            if (root / name).is_file():
                norms.append(Norm(
                    title="代码风格: ESLint",
                    content=f"项目使用 ESLint（配置文件: {name}，具体规则需手动查看）",
                    tag="auto",
                    priority=5,
                    metadata={"source": name},
                ))
                return

    # .eslintrc.yml — 简易解析
    if eslint_data is None:
        path = root / ".eslintrc.yml"
        if path.is_file():
            try:
                import yaml  # type: ignore
                eslint_data = yaml.safe_load(path.read_text(encoding="utf-8", errors="ignore"))
                source = ".eslintrc.yml"
            except Exception:
                pass

    # package.json eslintConfig
    if eslint_data is None:
        pkg_path = root / "package.json"
        if pkg_path.is_file():
            try:
                pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="ignore"))
                if "eslintConfig" in pkg:
                    eslint_data = pkg["eslintConfig"]
                    source = "package.json eslintConfig"
            except (json.JSONDecodeError, OSError):
                pass

    if not eslint_data or not isinstance(eslint_data, dict):
        return

    items: list[str] = ["项目使用 ESLint 做代码检查"]
    if "extends" in eslint_data:
        ext = eslint_data["extends"]
        if isinstance(ext, list):
            items.append(f"继承配置: {', '.join(ext)}")
        else:
            items.append(f"继承配置: {ext}")
    if "rules" in eslint_data and isinstance(eslint_data["rules"], dict):
        rule_items = [f"{k}: {v}" for k, v in list(eslint_data["rules"].items())[:10]]
        items.append("关键规则:\n" + "\n".join(f"  - {r}" for r in rule_items))

    norms.append(Norm(
        title="代码风格: ESLint",
        content="\n".join(f"- {i}" if not i.startswith("关键规则") else i for i in items),
        tag="auto",
        priority=5,
        metadata={"source": source},
    ))


# ──────────────────────────────────────────────
# .prettierrc
# ──────────────────────────────────────────────

def _extract_prettierrc(root: Path, norms: list[Norm]) -> None:
    prettier_data: dict[str, Any] | None = None
    source = ""

    for name in (".prettierrc", ".prettierrc.json"):
        path = root / name
        if path.is_file():
            try:
                prettier_data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
                source = name
                break
            except (json.JSONDecodeError, OSError):
                continue

    # .prettierrc.js — 只标记存在
    if prettier_data is None:
        for name in (".prettierrc.js", ".prettierrc.cjs", "prettier.config.js"):
            if (root / name).is_file():
                norms.append(Norm(
                    title="代码风格: Prettier",
                    content=f"项目使用 Prettier 做代码格式化（配置文件: {name}，具体规则需手动查看）",
                    tag="auto",
                    priority=5,
                    metadata={"source": name},
                ))
                return

    if not prettier_data or not isinstance(prettier_data, dict):
        return

    items: list[str] = []
    label_map = {
        "printWidth": "行宽上限",
        "tabWidth": "缩进大小",
        "useTabs": "使用 Tab 缩进",
        "semi": "行尾分号",
        "singleQuote": "单引号",
        "trailingComma": "尾逗号",
        "bracketSpacing": "对象花括号空格",
        "arrowParens": "箭头函数括号",
        "endOfLine": "行尾符",
    }
    for key, label in label_map.items():
        if key in prettier_data:
            items.append(f"{label}: {prettier_data[key]}")

    # 其他字段也列出
    for key, val in prettier_data.items():
        if key not in label_map:
            items.append(f"{key}: {val}")

    if items:
        norms.append(Norm(
            title="代码风格: Prettier",
            content="项目使用 Prettier 做代码格式化:\n" + "\n".join(f"- {i}" for i in items),
            tag="auto",
            priority=5,
            metadata={"source": source},
        ))


# ──────────────────────────────────────────────
# pom.xml — 检测 checkstyle/spotless 插件
# ──────────────────────────────────────────────

def _extract_pom_xml(root: Path, norms: list[Norm]) -> None:
    path = root / "pom.xml"
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return

    items: list[str] = []

    # checkstyle 插件
    if re.search(r"<groupId>\s*org\.apache\.maven\.plugins\s*</groupId>\s*<artifactId>\s*maven-checkstyle-plugin\s*</artifactId>", text, re.DOTALL):
        items.append("使用 maven-checkstyle-plugin (Checkstyle)")
    if re.search(r"<groupId>\s*com\.github\.spotbugs\s*</groupId>\s*<artifactId>\s*spotbugs-maven-plugin\s*</artifactId>", text, re.DOTALL):
        items.append("使用 spotbugs-maven-plugin (SpotBugs)")

    # spotless 插件
    if re.search(r"<groupId>\s*com\.diffplug\.spotless\s*</groupId>\s*<artifactId>\s*spotless-maven-plugin\s*</artifactId>", text, re.DOTALL):
        items.append("使用 spotless-maven-plugin (Spotless)")

    if items:
        norms.append(Norm(
            title="代码风格: Java 构建工具 (pom.xml)",
            content="项目 pom.xml 中配置了代码规范工具:\n" + "\n".join(f"- {i}" for i in items),
            tag="auto",
            priority=5,
            metadata={"source": "pom.xml"},
        ))


# ──────────────────────────────────────────────
# TOML 解析 — 优先 stdlib tomllib (3.11+)
# ──────────────────────────────────────────────

def _parse_toml(path: Path) -> dict[str, Any]:
    """解析 TOML 文件，优先 stdlib tomllib"""
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            logger.warning("无法解析 TOML 文件: 需要 Python 3.11+ 或安装 tomli")
            return {}

    with open(path, "rb") as f:
        return tomllib.load(f)
