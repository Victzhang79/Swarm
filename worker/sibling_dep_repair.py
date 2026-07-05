"""从项目【自身兄弟 manifest】找缺失依赖的权威坐标注入到缺它的 manifest —— 多栈(npm/cargo/go)。

治本 A2 的多栈等价：Maven 侧 brain 的 `_inject_missing_maven_deps` 从兄弟 pom 自证
`<dependency>` 坐标注入失败模块 pom；此前 Go/npm/Cargo 无等价「从兄弟 manifest 找权威坐标注入」
（worker 侧只有 goimports / cargo fix / eslint 这类工具级修复，不解决"整个依赖没声明"）。

本模块在 worker L1 构建修复阶段：构建报「缺某依赖」→ 扫项目自身其它 manifest 找该依赖已声明的
【权威坐标(name+version)】→ 注入到当前构建模块缺它的 manifest。原则与 Maven 版一致：
**只用项目自证坐标、绝不臆造版本、非项目写死、跨模块通用**。触达文件经 `_attempt_build_repair`
的 `(count, paths)` 契约回传（TD2606-C9：修复不能只活在沙箱）。

注错比不注更糟，因此坐标源与注入目标两侧都 fail-closed：
- 声明检查（防重复注入/覆盖）比坐标源检查【宽】：`workspace = true`、`[dependencies.NAME]`
  点表、`file:` 版本都算"已声明"，但都不可作注入源（无可移植版本/目录相对坐标）。
- go 的 replace/exclude 不算 require；兄弟 require 若带伴随 replace（本地模块），注 require
  不带 replace 拉取必败 → 该坐标不可移植。
- cargo 目标无 `[package]`（workspace 虚拟根）注 `[dependencies]` 会被 cargo 整树拒绝 → 不碰。
- cargo/go 是全文读改写：目标含非 UTF-8 字节时严格读失败即跳过，绝不 errors="ignore" 后写回
  （那会静默丢字节）。npm 经 json 解析重建，不受此影响。

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
    if not isinstance(data, dict):
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
# 依赖区里任意 `name =` 条目（含 workspace = true / path = ... 等无 version 形态）。
_CARGO_NAME_RE = re.compile(r'^\s*([A-Za-z0-9_\-]+)\s*=', re.M)
# `[dependencies.NAME]` 点表段名。
_CARGO_DOT_SECTION_RE = re.compile(
    r'(?:dependencies|dev-dependencies|build-dependencies)\.["\']?([A-Za-z0-9_\-]+)["\']?$')
_CARGO_DEP_SECTIONS = ("dependencies", "dev-dependencies", "build-dependencies")


def _parse_cargo(text: str) -> dict[str, tuple[str, str | None]]:
    """解析 Cargo.toml 依赖区（平表/`{ version = .. }` 内联表/`[dependencies.NAME]` 点表/
    `workspace = true` 继承）。crate 名归一为下划线键（rustc 诊断用下划线，Cargo.toml 常用
    连字符），值 = (原名, 版本或 None)。版本 None = 已声明但无可移植版本（workspace 继承/
    path 依赖/点表无 version）——声明检查算已声明，坐标源侧不可用。"""
    out: dict[str, tuple[str, str | None]] = {}
    for block in re.split(r'^\s*\[', text, flags=re.M):
        head = block.split("]", 1)
        if len(head) != 2:
            continue
        section, body = head[0].strip().lower(), head[1]
        m_dot = _CARGO_DOT_SECTION_RE.match(section)
        if m_dot:
            name = m_dot.group(1)
            vm = re.search(r'^\s*version\s*=\s*"([^"]+)"', body, re.M)
            out.setdefault(name.replace("-", "_"), (name, vm.group(1) if vm else None))
            continue
        if section not in _CARGO_DEP_SECTIONS:
            continue
        for m in _CARGO_LINE_RE.finditer(body):
            name = m.group(1)
            ver = m.group(2) or m.group(3)
            if name and ver:
                out.setdefault(name.replace("-", "_"), (name, ver))
        for m in _CARGO_NAME_RE.finditer(body):
            name = m.group(1)
            if name:
                out.setdefault(name.replace("-", "_"), (name, None))
    return out


_GO_DEP_LINE_RE = re.compile(r'([^\s()]+/[^\s()]+)\s+(v[0-9][^\s]*)')
_GO_REPLACE_LHS_RE = re.compile(r'([^\s()=]+/[^\s()=]+)')


def _parse_go(text: str) -> dict[str, str | None]:
    """解析 go.mod：只认 require（单行/block），replace/exclude 块不算声明来源。
    出现在 replace 左侧的 require 模块版本置 None——注 require 不带伴随 replace（本地模块
    场景）拉取必败，该坐标不可移植；声明检查仍算已声明。"""
    out: dict[str, str | None] = {}
    replaced: set[str] = set()
    block: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if block:
            if line.startswith(")"):
                block = None
            elif not line.startswith("//"):
                if block == "require":
                    m = _GO_DEP_LINE_RE.match(line)
                    if m:
                        out.setdefault(m.group(1), m.group(2))
                elif block == "replace":
                    m = _GO_REPLACE_LHS_RE.match(line)
                    if m:
                        replaced.add(m.group(1))
            continue
        m = re.match(r'(require|replace|exclude)\s*\(\s*$', line)
        if m:
            block = m.group(1)
            continue
        m = re.match(r'require\s+(.+)$', line)
        if m:
            dm = _GO_DEP_LINE_RE.match(m.group(1))
            if dm:
                out.setdefault(dm.group(1), dm.group(2))
            continue
        m = re.match(r'replace\s+(.+)$', line)
        if m:
            rm = _GO_REPLACE_LHS_RE.match(m.group(1))
            if rm:
                replaced.add(rm.group(1))
    for mod in replaced:
        if mod in out:
            out[mod] = None  # require+replace 伴随 → 不可作注入坐标源
    return out


_MANIFEST = {
    "npm": ("package.json", _parse_npm),
    "cargo": ("Cargo.toml", _parse_cargo),
    "go": ("go.mod", _parse_go),
}

# npm 目录相对版本协议：跨目录移植必错，不可作坐标源。`workspace:` 按包名解析（pnpm/yarn
# workspace 同仓语义）可移植，故不在列。
_NPM_NONPORTABLE_VER = ("file:", "link:", "portal:")


def _coord_usable(stack: str, coord) -> bool:
    """兄弟坐标可否作注入源。fail-closed：None/无版本/目录相对 → 不可用。"""
    if coord is None:
        return False
    if stack == "cargo":
        return isinstance(coord, tuple) and coord[1] is not None
    if stack == "npm":
        return isinstance(coord, str) and not coord.startswith(_NPM_NONPORTABLE_VER)
    return isinstance(coord, str)  # go：replace 伴随的 None 已在上面挡


def _iter_manifests(project_path: Path, filename: str, limit: int = 200) -> list[Path]:
    out: list[Path] = []
    for p in project_path.rglob(filename):
        if any(part in _SKIP_DIRS for part in p.relative_to(project_path).parts):
            continue
        out.append(p)
        if len(out) >= limit:
            logger.warning(
                "[L1.2.1·repair] A2 manifest 扫描达上限 %d（%s），已截断——超大 monorepo 可能漏坐标",
                limit, filename)
            break
    return out


def _nearest_manifest(project_path: Path, modified: list[str], filename: str) -> Path | None:
    """当前构建模块的 manifest = 距【被改文件】最近的祖先目录 manifest。取不到则回退项目根，
    再取不到 None（fail-closed）。modified 里的绝对路径/../ 穿越不可信，默认拒绝
    （对齐 diff_apply._rel_within_root 的 P0-3 守卫），绝不选中项目外文件。"""
    root = project_path.resolve()
    for rel in modified or []:
        r = str(rel).strip()
        if not r or r.startswith(("/", "\\")) or (len(r) >= 2 and r[1] == ":"):
            continue
        cur = (root / r).resolve().parent
        if cur != root and root not in cur.parents:
            continue
        while True:
            cand = cur / filename
            if cand.is_file():
                return cand
            if cur == root or root not in cur.parents:
                break
            cur = cur.parent
    root_man = project_path / filename
    return root_man if root_man.is_file() else None


# ── 每栈：把坐标注入到目标 manifest（目标缺它时）；已声明则跳过。返回是否改动 ──────
def _inject_npm(path: Path, dep: str, ver: str) -> bool:
    # npm 产物经 json.loads → json.dumps 重建，errors="ignore" 不会把丢字节写回原文。
    text = path.read_text(encoding="utf-8", errors="ignore")
    if dep in _parse_npm(text):
        return False
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        logger.warning("[L1.2.1·repair] A2 npm 目标 manifest JSON 解析失败(跳过注入): %s", path)
        return False
    if not isinstance(data, dict):
        logger.warning("[L1.2.1·repair] A2 npm 目标 manifest 根不是对象(跳过注入): %s", path)
        return False
    deps = data.setdefault("dependencies", {})
    if not isinstance(deps, dict) or dep in deps:
        return False
    deps[dep] = ver
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def _read_strict_utf8(path: Path, stack: str) -> str | None:
    """cargo/go 注入是全文读改写：非 UTF-8 字节严格失败返回 None（errors=\"ignore\" 会静默
    丢字节再写回 = 损坏用户文件）。"""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        logger.warning(
            "[L1.2.1·repair] A2 %s 目标 manifest 含非 UTF-8 字节，跳过注入防损坏: %s", stack, path)
        return None


def _inject_cargo(path: Path, dep: str, coord) -> bool:
    text = _read_strict_utf8(path, "cargo")
    if text is None or not text.strip():
        return False
    if dep in _parse_cargo(text):
        return False
    if not re.search(r'^\s*\[package\]\s*(?:#.*)?$', text, re.M):
        # 无 [package] = workspace 虚拟根/非 crate manifest：注 [dependencies] 会被 cargo
        # 整树拒绝（"virtual manifest specifies a [dependencies] section"）→ fail-closed。
        logger.warning(
            "[L1.2.1·repair] A2 cargo 目标非 crate manifest(无 [package])，跳过注入: %s", path)
        return False
    name, ver = coord if isinstance(coord, tuple) else (dep, coord)
    line = f'{name} = "{ver}"\n'
    m = re.search(r'^\s*\[dependencies\]\s*$', text, re.M)
    if m:
        idx = text.index("\n", m.end()) + 1 if "\n" in text[m.end():] else len(text)
        new = text[:idx] + line + text[idx:]
    else:  # 无 [dependencies] 区 → 追加一个（上面已确证是真 crate manifest）
        new = text.rstrip("\n") + f"\n\n[dependencies]\n{line}"
    path.write_text(new, encoding="utf-8")
    return True


def _inject_go(path: Path, dep: str, ver: str) -> bool:
    text = _read_strict_utf8(path, "go")
    if text is None:
        return False
    if dep in _parse_go(text):
        return False
    m = re.search(r'^\s*require\s*\(\s*$', text, re.M)
    if m:  # 插入到 require ( ... ) block 内首行
        nl = text.find("\n", m.end())
        if nl == -1:
            return False  # `require (` 悬在 EOF 未闭合 = 畸形 manifest → fail-closed 不碰
        idx = nl + 1
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
    fail-closed：找不到目标 manifest / 兄弟无该坐标 / 坐标不可移植 / 目标已声明 → 跳过不改。
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
    # 兄弟 manifest 只扫描/解析一遍（每 dep 重扫全树是 O(deps×tree) 浪费）。
    target_resolved = target.resolve()
    sibling_decls: list[dict] = []
    for man in _iter_manifests(root, filename):
        if man.resolve() == target_resolved:
            continue
        try:
            sibling_decls.append(parser(man.read_text(encoding="utf-8", errors="ignore")))
        except OSError as exc:
            logger.debug("[L1.2.1·repair] A2 兄弟 manifest 读取失败(跳过): %s — %s", man, exc)
    injected = 0
    touched: list[str] = []
    for dep in deps:
        coord = next((decl[dep] for decl in sibling_decls if dep in decl), None)
        if not _coord_usable(stack, coord):
            continue  # 兄弟无坐标/坐标不可移植 → 非项目自证，绝不臆造，交回上游（BLOCKED/等生产者）
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
        except (OSError, ValueError) as exc:
            # 写用户 manifest 半途失败必须可见（w 模式先截断，disk-full 半途 = 文件损坏）。
            logger.warning("[L1.2.1·repair] A2 %s 注入 %s 失败(跳过): %s", stack, dep, exc)
    return injected, touched
