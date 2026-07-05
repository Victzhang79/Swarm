"""从项目【自身兄弟 manifest】找缺失依赖的权威坐标注入到缺它的 manifest —— 多栈(npm/cargo/go)。

治本 A2 的多栈等价：Maven 侧 brain 的 `_inject_missing_maven_deps` 从兄弟 pom 自证
`<dependency>` 坐标注入失败模块 pom；此前 Go/npm/Cargo 无等价「从兄弟 manifest 找权威坐标注入」
（worker 侧只有 goimports / cargo fix / eslint 这类工具级修复，不解决"整个依赖没声明"）。

本模块在 worker L1 构建修复阶段：构建报「缺某依赖」→ 扫项目自身其它 manifest 找该依赖已声明的
【权威坐标(name+version)】→ 注入到当前构建模块缺它的 manifest。原则与 Maven 版一致：
**只用项目自证坐标、绝不臆造版本、非项目写死、跨模块通用**。触达文件经 `_attempt_build_repair`
的 `(count, paths)` 契约回传（TD2606-C9：修复不能只活在沙箱）。

纯文件操作（读/写项目自身 manifest），确定性、可离线单测，不依赖任何外部工具/网络。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# 扫 manifest 时跳过的目录（依赖安装产物/构建产物/VCS），避免把 node_modules 里第三方
# 的 manifest 当作"兄弟"来源。
_SKIP_DIRS = {".git", "node_modules", "target", "build", "dist", ".venv", "venv",
              "vendor", "__pycache__", ".gradle", ".next", "out"}

# ── 每栈：缺失依赖检测正则（从 build 输出反查依赖名）──────────────────────────
_NPM_MISSING_RE = [
    re.compile(r"""Cannot find module ['"]([^'"]+)['"]"""),
    re.compile(r"""Module not found:.*?Can't resolve ['"]([^'"]+)['"]""", re.I),
    re.compile(r"""Can't resolve ['"]([^'"]+)['"]"""),
]
_CARGO_MISSING_RE = [
    re.compile(r"unresolved import `([A-Za-z0-9_]+)"),
    re.compile(r"use of undeclared crate or module `([A-Za-z0-9_]+)`"),
    re.compile(r"can't find crate for `([A-Za-z0-9_]+)`"),
    re.compile(r"failed to resolve: use of undeclared crate or module `([A-Za-z0-9_]+)`"),
]
_GO_MISSING_RE = [
    re.compile(r'no required module provides package ([^\s;:]+)'),
    re.compile(r'cannot find package ["\']?([^\s"\';]+)'),
    re.compile(r'missing go\.sum entry for module providing package ([^\s;:]+)'),
]


def _norm_npm_pkg(raw: str) -> str | None:
    """npm import 名归一到【包名】：'@scope/pkg/sub' → '@scope/pkg'；'pkg/sub' → 'pkg'；
    相对路径('./x' / '../x' / 绝对) → None（本地文件不是依赖）。"""
    s = (raw or "").strip()
    if not s or s.startswith((".", "/")):
        return None
    parts = s.split("/")
    if s.startswith("@"):
        return "/".join(parts[:2]) if len(parts) >= 2 else None
    return parts[0]


def _missing_deps(build_output: str, stack: str) -> list[str]:
    """从 build 输出提取缺失依赖名（去重保序）。stack ∈ {npm, cargo, go}。"""
    blob = build_output or ""
    regexes = {"npm": _NPM_MISSING_RE, "cargo": _CARGO_MISSING_RE, "go": _GO_MISSING_RE}.get(stack, [])
    out: list[str] = []
    for rx in regexes:
        for m in rx.finditer(blob):
            name = m.group(1)
            if stack == "npm":
                name = _norm_npm_pkg(name)
            if name and name not in out:
                out.append(name)
    return out


# ── 每栈：从一个 manifest 解析【已声明依赖 → 版本坐标】──────────────────────────
def _parse_npm(text: str) -> dict[str, str]:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}
    out: dict[str, str] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            for name, ver in section.items():
                if isinstance(name, str) and isinstance(ver, str) and name not in out:
                    out[name] = ver
    return out


# Cargo：`name = "1.2"` 或 `name = { version = "1.2", ... }`；只取 version 坐标。
_CARGO_LINE_RE = re.compile(
    r'^\s*([A-Za-z0-9_\-]+)\s*=\s*(?:"([^"]+)"|\{[^}]*\bversion\s*=\s*"([^"]+)"[^}]*\})', re.M)


def _parse_cargo(text: str) -> dict[str, str]:
    """解析 Cargo.toml 的 [dependencies]/[dev-dependencies] 区。crate 名归一为下划线（rustc
    诊断用下划线，Cargo.toml 常用连字符）。"""
    out: dict[str, str] = {}
    for block in re.split(r'^\s*\[', text, flags=re.M):
        head = block.split("]", 1)
        if len(head) != 2:
            continue
        section, body = head[0].strip().lower(), head[1]
        if section not in ("dependencies", "dev-dependencies", "build-dependencies"):
            continue
        for m in _CARGO_LINE_RE.finditer(body):
            name = m.group(1)
            ver = m.group(2) or m.group(3)
            if name and ver:
                out.setdefault(name.replace("-", "_"), (name, ver))  # 存原名+版本
    return out


_GO_REQUIRE_RE = re.compile(r'^\s*(?:require\s+)?([^\s()]+/[^\s()]+)\s+(v[0-9][^\s]*)', re.M)


def _parse_go(text: str) -> dict[str, str]:
    """解析 go.mod 的 require（单行 `require m v` 或 block 内 `m v`）→ {module: version}。"""
    out: dict[str, str] = {}
    for m in _GO_REQUIRE_RE.finditer(text):
        mod, ver = m.group(1), m.group(2)
        if mod and mod not in out and not mod.startswith("//"):
            out[mod] = ver
    return out


_MANIFEST = {
    "npm": ("package.json", _parse_npm),
    "cargo": ("Cargo.toml", _parse_cargo),
    "go": ("go.mod", _parse_go),
}


def _iter_manifests(project_path: Path, filename: str, limit: int = 200) -> list[Path]:
    out: list[Path] = []
    for p in project_path.rglob(filename):
        if any(part in _SKIP_DIRS for part in p.relative_to(project_path).parts):
            continue
        out.append(p)
        if len(out) >= limit:
            break
    return out


def _sibling_coord(project_path: Path, filename: str, parser, dep: str, exclude: Path):
    """在项目所有【兄弟 manifest】（排除 exclude 自身）里找 dep 的权威坐标。首个命中即返回。
    返回 parser 存的坐标值（npm: 版本串；cargo: (原名, 版本)；go: 版本串）；无则 None。"""
    for man in _iter_manifests(project_path, filename):
        if man.resolve() == exclude.resolve():
            continue
        try:
            declared = parser(man.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        if dep in declared:
            return declared[dep]
    return None


def _nearest_manifest(project_path: Path, modified: list[str], filename: str) -> Path | None:
    """当前构建模块的 manifest = 距【被改文件】最近的祖先目录 manifest。取不到则 None（fail-closed）。"""
    for rel in modified or []:
        cur = (project_path / str(rel).strip()).resolve().parent
        while True:
            cand = cur / filename
            if cand.is_file():
                return cand
            if cur == project_path.resolve() or project_path.resolve() not in cur.parents:
                break
            cur = cur.parent
    root_man = project_path / filename
    return root_man if root_man.is_file() else None


# ── 每栈：把坐标注入到目标 manifest（目标缺它时）；已声明则跳过。返回是否改动 ──────
def _inject_npm(path: Path, dep: str, ver: str) -> bool:
    text = path.read_text(encoding="utf-8", errors="ignore")
    if dep in _parse_npm(text):
        return False
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return False
    deps = data.setdefault("dependencies", {})
    if not isinstance(deps, dict) or dep in deps:
        return False
    deps[dep] = ver
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def _inject_cargo(path: Path, dep: str, coord, ) -> bool:
    text = path.read_text(encoding="utf-8", errors="ignore")
    if dep in _parse_cargo(text):
        return False
    name, ver = coord if isinstance(coord, tuple) else (dep, coord)
    line = f'{name} = "{ver}"\n'
    m = re.search(r'^\s*\[dependencies\]\s*$', text, re.M)
    if m:
        idx = text.index("\n", m.end()) + 1 if "\n" in text[m.end():] else len(text)
        new = text[:idx] + line + text[idx:]
    else:  # 无 [dependencies] 区 → 追加一个（fail-safe：只在文件非空时）
        new = text.rstrip("\n") + f"\n\n[dependencies]\n{line}"
    path.write_text(new, encoding="utf-8")
    return True


def _inject_go(path: Path, dep: str, ver: str) -> bool:
    text = path.read_text(encoding="utf-8", errors="ignore")
    if dep in _parse_go(text):
        return False
    m = re.search(r'^\s*require\s*\(\s*$', text, re.M)
    if m:  # 插入到 require ( ... ) block 内首行
        idx = text.index("\n", m.end()) + 1
        new = text[:idx] + f"\t{dep} {ver}\n" + text[idx:]
    else:  # 无 block → 追加单行 require
        new = text.rstrip("\n") + f"\nrequire {dep} {ver}\n"
    path.write_text(new, encoding="utf-8")
    return True


_INJECT = {"npm": _inject_npm, "cargo": _inject_cargo, "go": _inject_go}


def repair_from_sibling_manifests(
    project_path: str, build_output: str, modified: list[str], stack: str,
) -> tuple[int, list[str]]:
    """A2 多栈：从项目自身兄弟 manifest 找缺失依赖权威坐标，注入当前构建模块 manifest。

    stack ∈ {npm, cargo, go}。返回 (注入依赖数, 触达 manifest 相对路径列表)——与
    _attempt_build_repair 的其它 adapter 同契约（触达 >0 触发重跑构建 + 路径回传）。
    fail-closed：找不到目标 manifest / 兄弟无该坐标 / 目标已声明 → 跳过不改。
    """
    spec = _MANIFEST.get(stack)
    if not spec or not project_path:
        return 0, []
    filename, parser = spec
    root = Path(project_path)
    if not root.is_dir():
        return 0, []
    deps = _missing_deps(build_output or "", stack)
    if not deps:
        return 0, []
    target = _nearest_manifest(root, modified, filename)
    if target is None:
        return 0, []
    injected = 0
    touched: list[str] = []
    for dep in deps:
        coord = _sibling_coord(root, filename, parser, dep, exclude=target)
        if coord is None:
            continue  # 兄弟里也没这坐标 → 非项目自证，绝不臆造，交回上游（BLOCKED/等生产者）
        try:
            if _INJECT[stack](target, dep, coord):
                injected += 1
                rel = str(target.relative_to(root))
                if rel not in touched:
                    touched.append(rel)
                logger.info(
                    "[L1.2.1·repair] A2 多栈补依赖(%s)：据兄弟 manifest 自证坐标把 %s 注入 %s",
                    stack, dep, rel,
                )
        except OSError as exc:
            logger.debug("[L1.2.1·repair] A2 %s 注入 %s 失败(跳过): %s", stack, dep, exc)
    return injected, touched
