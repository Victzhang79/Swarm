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
- W-4：注入目标由构建失败输出里的【出错文件路径】确定性定位（L1 构建恒以 project_path
  为 cwd：go 错误路径相对 go 命令 cwd；cargo `-->` 经实证为工作区根相对；tsc 相对
  tsconfig 所在=构建 cwd）——不再只凭 modified 首文件猜（嵌套 go module 注根 go.mod
  白烧）。有证据但映射不回项目内 manifest（逃逸/失效路径）→ fail-closed 不注。

纯文件操作（读/写项目自身 manifest），确定性、可离线单测，不依赖任何外部工具/网络。
"""

from __future__ import annotations

import json
import logging
import os
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


def missing_deps_for(build_output: str, stack: str) -> list[str]:
    """★W-22★ `_missing_deps` 的公开入口——l1_pipeline A2「推送未达」机读记录要记下
    本轮试图注入的缺依赖坐标，l1_verdict 据「同一坐标仍在报错」判失败仅由推送未达引起
    （transient）而非 capability。与 `_missing_deps` 同一实现，绝不另造口径。"""
    return _missing_deps(build_output, stack)


# ── 每栈：从一个 manifest 解析【已声明依赖 → 版本坐标】──────────────────────────
def _parse_npm(text: str) -> dict[str, tuple[str, str]]:
    """name → (版本, 来源 section)。D14：记录 section——devDependencies 的坐标注入
    目标时必须落在 devDependencies（注入运行时 dependencies 会把构建期工具变成
    生产依赖）。同名多处时 dependencies 优先（循环顺序保证）。"""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, tuple[str, str]] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            for name, ver in section.items():
                if isinstance(name, str) and isinstance(ver, str) and name not in out:
                    out[name] = (ver, key)
    return out


# Cargo：D13 起依赖区解析只走 _CARGO_RAW_LINE_RE（整行原始声明，平表/内联表）——
# 注入时整体移植，保 features/default-features 等。
_CARGO_RAW_LINE_RE = re.compile(
    r'^\s*([A-Za-z0-9_\-]+)\s*=\s*("(?:[^"]+)"|\{[^}]*\})\s*(?:#.*)?$', re.M)
# 依赖区里任意 `name =` 条目（含 workspace = true / path = ... 等无 version 形态）。
_CARGO_NAME_RE = re.compile(r'^\s*([A-Za-z0-9_\-]+)\s*=', re.M)
# `[dependencies.NAME]` 点表段名。
_CARGO_DOT_SECTION_RE = re.compile(
    r'(?:dependencies|dev-dependencies|build-dependencies)\.["\']?([A-Za-z0-9_\-]+)["\']?$')
_CARGO_DEP_SECTIONS = ("dependencies", "dev-dependencies", "build-dependencies")


def _parse_cargo(text: str) -> dict[str, tuple[str, str | None, str | None, str]]:
    """解析 Cargo.toml 依赖区（平表/`{ version = .. }` 内联表/`[dependencies.NAME]` 点表/
    `workspace = true` 继承）。crate 名归一为下划线键（rustc 诊断用下划线，Cargo.toml 常用
    连字符），值 = (原名, 版本或 None, 原始单行声明或 None, 来源 section)。版本 None =
    已声明但无可移植版本（workspace 继承/path 依赖/点表无 version）——声明检查算已声明，
    坐标源侧不可用。原始声明（D13）只在单行形态（平表/内联表）捕获，注入时整体移植保
    features；点表是多行段，移植复杂，维持 version-only（诚实边界）。
    来源 section（D14）：注入必须落回同类 section（dev/build 依赖绝不进运行时
    [dependencies]）。"""
    out: dict[str, tuple[str, str | None, str | None, str]] = {}
    for block in re.split(r'^\s*\[', text, flags=re.M):
        head = block.split("]", 1)
        if len(head) != 2:
            continue
        section, body = head[0].strip().lower(), head[1]
        m_dot = _CARGO_DOT_SECTION_RE.match(section)
        if m_dot:
            name = m_dot.group(1)
            vm = re.search(r'^\s*version\s*=\s*"([^"]+)"', body, re.M)
            out.setdefault(name.replace("-", "_"),
                           (name, vm.group(1) if vm else None, None, section.split(".")[0]))
            continue
        if section not in _CARGO_DEP_SECTIONS:
            continue
        for m in _CARGO_RAW_LINE_RE.finditer(body):
            name, rhs = m.group(1), m.group(2)
            raw_line = f"{name} = {rhs}"
            if rhs.startswith('"'):
                out.setdefault(name.replace("-", "_"), (name, rhs[1:-1], raw_line, section))
            else:
                vm = re.search(r'\bversion\s*=\s*"([^"]+)"', rhs)
                if vm:
                    out.setdefault(name.replace("-", "_"), (name, vm.group(1), raw_line, section))
        for m in _CARGO_NAME_RE.finditer(body):
            name = m.group(1)
            if name:
                out.setdefault(name.replace("-", "_"), (name, None, None, section))
    return out


_GO_DEP_LINE_RE = re.compile(r'([^\s()]+/[^\s()]+)\s+(v[0-9][^\s]*)')
_GO_REPLACE_LHS_RE = re.compile(r'([^\s()=]+/[^\s()=]+)')

# ── W-4：构建失败输出里的【出错文件路径】提取（注入目标的确定性证据）────────────
# L1 构建恒以 project_path 为 cwd（go build ./... / cargo build -q / tsc --noEmit），故：
# - go  `dir/f.go:3:2: no required module ...` 相对 go 命令 cwd = 项目根；
# - cargo `--> crates/foo/src/lib.rs:1:5` 经实证（cargo 1.84 workspace member）= 工作区根相对；
# - tsc `src/a.ts(3,23): error TS2307` 相对 tsconfig 所在 = 构建 cwd；webpack `ERROR in ./x`。
_FAIL_FILE_RE = {
    "go": [re.compile(r"^([^\s():]+\.go):\d+:\d+:", re.M)],
    "cargo": [re.compile(r"^\s*-->\s+([^\s:]+\.rs):\d+:\d+", re.M)],
    "npm": [
        re.compile(r"^([^\s()]+\.[cm]?[jt]sx?)\(\d+,\d+\):\s*error", re.M),   # tsc
        re.compile(r"^ERROR in\s+\.?/?([^\s]+\.[cm]?[jt]sx?)\b", re.M),       # webpack
    ],
}
_FAIL_FILE_CAP = 20  # 病态输出截断：前 20 个证据足够定位失败模块


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
        if not (isinstance(coord, tuple) and coord[1] is not None):
            return False
        # D13：raw 内联表含 path =（本地相对路径）→ 跨目录移植必错，不可作坐标源
        _raw = coord[2] if len(coord) > 2 else None
        return not (_raw and re.search(r"\bpath\s*=", _raw))
    if stack == "npm":
        # (ver, section) 二元组；版本目录相对协议不可移植
        return (isinstance(coord, tuple) and isinstance(coord[0], str)
                and not coord[0].startswith(_NPM_NONPORTABLE_VER))
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
    # D14：排序确定化——rglob 目录序依赖文件系统，多兄弟声明同名依赖不同版本时
    # "首个命中" 在旧码下非确定（同输入不同坐标源）。
    return sorted(out)


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


def _failure_manifest(root: Path, build_output: str, filename: str,
                      stack: str) -> tuple[Path | None, bool]:
    """W-4：从构建失败输出提取出错文件 → resolve 到项目根（= L1 构建 cwd）→ 最近祖先 manifest。

    与 _nearest_manifest 的差别：证据来自【失败输出】而非 modified 首文件——后者在跨模块/
    嵌套 module 场景指向的是 worker 碰巧改的第一个文件，与真正编译失败的模块无关（如嵌套
    go module 注根 go.mod 白烧）。

    证据分级（批次7+8 闸门 hunter CONFIRMED + reviewer MEDIUM 整改）：
    - 相对路径 resolve 进项目且 is_file → 可用证据；
    - 绝对路径：resolve 后落在项目内（本地绝对输出）→ 可用；否则逐级剥前缀取后缀
      resolve（沙箱/容器 `/workspace/...` 形态， cwd 不是本地根）→ 命中项目内真文件即
      可用——绝不因"绝对"二字整条丢弃（丢了=回退 modified 猜=W-4 原病复发）；
    - 有可用证据但祖先链无 manifest → (None, True)：调用方 fail-closed 不注
      （知道哪文件失败却定不出目标模块，猜=注错比不注更糟）；
    - 提取到的证据全部映射不回项目（逃逸/失效/外来输出）→ (None, False)：证据不可用
      而非证据反对——回退 modified 首文件旧行为（绝不掐死正常修复流，hunter CONFIRMED：
      沙箱绝对路径输出形态下旧实现把 repair 整体关停）。"""
    resolved = root.resolve()
    cands: list[str] = []
    _overflow = 0
    for rx in _FAIL_FILE_RE.get(stack, []):
        for m in rx.finditer(build_output or ""):
            rel = m.group(1)
            if rel not in cands:
                if len(cands) >= _FAIL_FILE_CAP:
                    _overflow += 1   # 闸门 R2 hunter LOW-3：截断必须可观测（C13 同型纪律）
                    continue
                cands.append(rel)
    if _overflow:
        logger.warning("[L1.2.1·repair] W-4 失败输出出错文件超 cap=%d，丢弃 %d 条证据"
                       "（病态输出场景被丢弃者可能含项目内报错）", _FAIL_FILE_CAP, _overflow)

    def _walk(cur: Path) -> Path | None:
        while True:
            cand = cur / filename
            if cand.is_file():
                return cand
            if cur == resolved or resolved not in cur.parents:
                return None
            cur = cur.parent

    def _resolve_evidence(rel: str) -> Path | None:
        """单条证据 → 项目内真文件（不可用 → None）。"""
        if rel.startswith(("/", "\\")) or (len(rel) >= 2 and rel[1] == ":"):
            ap = Path(rel).resolve()
            if ap.is_file() and (ap == resolved or resolved in ap.parents):
                return ap                       # 本地绝对输出：项目内直接命中
            parts = [p for p in Path(rel).parts if p not in ("/", "\\")]
            for i in range(1, len(parts)):      # 沙箱绝对路径：逐级剥前缀取后缀
                cand = (resolved / "/".join(parts[i:])).resolve()
                if (cand == resolved or resolved in cand.parents) and cand.is_file():
                    return cand
            return None
        cand = (resolved / rel).resolve()
        if cand != resolved and resolved not in cand.parents:
            return None                         # ../ 逃逸 → 非本项目证据
        return cand if cand.is_file() else None  # 失效/外来路径（防陈旧输出误导）

    saw_usable = False
    for rel in cands:
        f = _resolve_evidence(rel)
        if f is None:
            continue
        # 闸门 R2 reviewer MEDIUM①：证据落进产物/第三方目录（node_modules/vendor/target…）
        # 时 _walk 会把【第三方 manifest】当注入目标（依赖注给 node_modules 里的包=注错
        # 还自以为修复）。与 _SKIP_DIRS 同源过滤——这类证据不算"项目内可用"，继续考察
        # 后续证据，全部如此则回退 modified-nearest（绝不注第三方）。
        if any(part in _SKIP_DIRS for part in f.relative_to(resolved).parts):
            logger.info("[L1.2.1·repair] W-4 证据落产物/第三方目录，不作注入目标: %s", rel)
            continue
        saw_usable = True
        man = _walk(f.parent)
        if man is not None:
            return man, True
    return None, saw_usable


# ── 每栈：把坐标注入到目标 manifest（目标缺它时）；已声明则跳过。返回是否改动 ──────
def _inject_npm(path: Path, dep: str, coord) -> bool:
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
    # D14：注入落到【来源 section】——devDependencies 坐标绝不进运行时 dependencies
    ver, section = coord if isinstance(coord, tuple) else (coord, "dependencies")
    deps = data.setdefault(section, {})
    if not isinstance(deps, dict) or dep in deps:
        return False
    deps[dep] = ver
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def toml_section_anchor(text: str, section: str) -> "re.Match | None":
    """在 TOML 文本里定位 `[section]` 头行 —— **注入侧与并集侧的唯一事实源**。

    容忍两种合法写法：① 头内空白（`[ dependencies ]`，批次6 R1 已治）；② **行尾注释**
    （`[dependencies]  # keep sorted`）。

    ★#29-2 复核 MEDIUM（reviewer 提，已独立复现）★ 原正则以 `\\s*$` 收尾，行尾注释直接
    失配 ⇒ 走"追加一个新 `[section]`"的 fallback ⇒ 重复 section ⇒ 非法 TOML ⇒ 被后置校验
    拦下 ⇒ **A2 注入/并集在这类完全合法的 manifest 上静默变 no-op**（有 WARNING，但"本该
    修好却没修"）。`# keep sorted` / `# 按字母序` 这类行尾注释在真实 Cargo.toml 里很常见。
    诚实边界：`#` 出现在 section 名里不合法，故这里无需担心把注释符当名字的一部分。

    ★同时纠正复核对因果的描述★：reviewer 称本次改动"把原本产毒但返回 True 的场景改成了
    诚实拒绝 False"。实测 `5c9e0a2^` 旧代码在同一输入下**同样**返回 False（重复 section 是
    **语法**错，旧的 M-2 `tomllib.loads` 也拦得住）⇒ 本次改动在这条路径上**行为未变**，
    缺口是纯既有缺口。记在这里，免得后来人以为这条路径被本次动过。
    """
    return re.search(rf'^[ \t]*\[[ \t]*{re.escape(section)}[ \t]*\][ \t]*(?:#[^\n]*)?$',
                     text, re.M)


def _norm_crate_key(name: str) -> str:
    """crate 名归一：`-` 与 `_` 在 Cargo 生态里可互换（rustc 诊断用 `_`、manifest 常用 `-`）。

    ★#29-2 复核 MEDIUM-2 的处置：采纳改法，但**如实记它在生产上不可达**★
    reviewer 指出原先的三态集合枚举 `{name, -→_, _→-}` 对**混用两种分隔符**的键
    （`my-crate_name` 配 rustc 名 `my_crate_name`）覆盖不到 ⇒ 合法插入被误判"没落地" ⇒
    fail-closed 冤拒。**枚举缺口本身成立**（已用矩阵实测：单纯全 `-`↔全 `_` 互换三态覆盖
    得到，混用键覆盖不到）。
    但**沿生产调用路径不可达**（血规 10①：加机制先数调用点，且要数到"谁消费返回值"这一层）：
    `_inject_cargo` 两条臂传给 `_toml_insert_ok` 的 `name` 都**恒等于真正插入的那个键** ——
    tuple 臂 `name = coord[0]` 而 `raw` 由 `_parse_cargo` 以同一个 name 拼成
    （`raw_line = f"{name} = {rhs}"`，见 :147）；非 tuple 臂 `name = dep` 而写入行是
    `f'{name} = "{ver}"'`。故三态集合里恒含 `name` 自身 ⇒ 恒命中 ⇒ 那个缺口今天咬不到。
    仍改的理由：少一份形态枚举（枚举缺口是本仓反复栽过的一类），且哪天有新调用方传入与
    落地键不同的 name 时不会冤拒。**不宣称它修了一个活缺陷，也没有突变能压住它**
    （见 `scripts/w1_mutation_check.py` 里 W-1-p 撤销的记录）。
    """
    return name.replace("-", "_")


def _toml_insert_ok(original: str, candidate: str, section: str, name: str) -> bool:
    """#29-2 W-1：TOML 单条依赖插入的【事实】后置校验。

    三条断言全过才算插对：① candidate 是合法 TOML；② `[section].name` **真的**存在于
    结果里；③ 把该键摘掉后与 original 逐键相等（其它值一个都没变）。

    ★为什么不能只验语法★：插入锚点是正则找 `[section]` 行，而该行可能出现在**多行字符串
    值**里（`description = \"\"\"…\\n[dependencies]\\n…\"\"\"` 是常见写法）。此时依赖被插进
    字符串 ⇒ 用户 manifest 的值被污染、真依赖没进去、而结果**仍是合法 TOML** ⇒ 只验
    `tomllib.loads` 的闸恒放行（也正因如此那道闸原本不可独立证伪——冗余防御互相兜底）。
    ③ 是其中最强的一条：它把"文本级插入落错位置"整类问题一次性关掉，不依赖穷举形态
    （血规：声称穷举必须指出权威来源——这里改成不穷举，直接对账端状态）。

    名字归一：Cargo.toml 惯用连字符、rustc 诊断用下划线，两种写法指同一 crate；断言 ②
    两种都认（否则合法注入会被自己的校验冤杀）。见 `_norm_crate_key`。
    """
    import tomllib
    try:
        new_obj = tomllib.loads(candidate)
        old_obj = tomllib.loads(original)
    except Exception:  # noqa: BLE001 — 含 TOMLDecodeError；原文畸形也走 fail-closed
        return False
    sec_new = new_obj.get(section)
    if not isinstance(sec_new, dict):
        return False
    _want = _norm_crate_key(name)
    hit = next((k for k in sec_new if _norm_crate_key(k) == _want), None)
    if hit is None:
        return False
    stripped = {k: (dict(v) if isinstance(v, dict) else v) for k, v in new_obj.items()}
    stripped[section].pop(hit, None)
    if not stripped[section] and not isinstance(old_obj.get(section), dict):
        stripped.pop(section, None)
    return stripped == old_obj


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
    # D13：内联表/平表单行声明整体移植（保 features/default-features）；无 raw（点表）退 version-only
    # D14：注入落回【来源 section】——dev/build 依赖绝不进运行时 [dependencies]
    if isinstance(coord, tuple):
        name, ver = coord[0], coord[1]
        raw = coord[2] if len(coord) > 2 else None
        section = coord[3] if len(coord) > 3 and coord[3] else "dependencies"
    else:
        name, ver, raw, section = dep, coord, None, "dependencies"
    if section not in _CARGO_DEP_SECTIONS:
        section = "dependencies"

    def _insert(dep_line: str) -> str:
        # 批次6 R1：section 头容忍内空白（`[ dependencies ]` 是合法 TOML）——旧正则
        # 不匹配会追加第二个同名 section → duplicate-section 非法 TOML 毒化目标。
        # #29-2 复核：锚点定位收敛到 `toml_section_anchor` 单一事实源（并集侧同用），
        # 并补上"行尾注释"这一合法形态。
        m = toml_section_anchor(text, section)
        if m:
            idx = text.index("\n", m.end()) + 1 if "\n" in text[m.end():] else len(text)
            return text[:idx] + dep_line + text[idx:]
        # 无对应 section 区 → 追加一个（上面已确证是真 crate manifest）
        return text.rstrip("\n") + f"\n\n[{section}]\n{dep_line}"

    # M-2（批次6 R1 hunter+reviewer 双点名）：raw 移植后必须过 tomllib 全量校验——
    # 内联表正则被字符串内 `}` 截断/兄弟 raw 依赖来源文件上下文时，盲写会产出
    # 非法 TOML 直接毒化目标 manifest。校验不过 → 回退 version-only 再校；
    # 再不过 → fail-closed 不注（诚实边界：丢保 features 也不产毒）。
    # ★#29-2 W-1 升级：语法合法 ≠ 插对地方★ 原 M-2 只验 `tomllib.loads` 不抛。实测缺陷：
    # `description = """…\n[dependencies]\n…"""`（多行字符串里写用法说明）时，`_insert` 的
    # 正则锚点命中【字符串内】那一行 ⇒ 依赖被插进 description 值 ⇒ ① 用户 manifest 的
    # description 被污染并直接进交付 diff ② 真依赖【没注进去】 ③ 产出**仍是合法 TOML**
    # ⇒ 旧校验完全看不见，且函数返回 True ⇒ 调用方记 injected+=1、触发重跑、构建照旧缺
    # 同一依赖 ⇒ repair 收敛循环空烧轮次。故校验升级为 `_toml_insert_ok`（判事实：条目真
    # 在目标 section 里，且其它值一个都没变）。
    if raw:
        candidate = _insert(f"{raw}\n")
        if not _toml_insert_ok(text, candidate, section, name):
            logger.warning(
                "[L1.2.1·repair] cargo raw 移植后 TOML 后置校验不过（内联表截断/上下文依赖/"
                "锚点落在多行字符串内），回退 version-only 注入: %s -> %s", dep, path)
            raw = None
    line = f"{raw}\n" if raw else f'{name} = "{ver}"\n'
    new = _insert(line) if not raw else candidate
    if not _toml_insert_ok(text, new, section, name):
        logger.warning(
            "[L1.2.1·repair] cargo 注入后 TOML 后置校验不过，fail-closed 不注"
            "（宁可让构建如实再报缺依赖，绝不产毒/伪成功）: %s -> %s", dep, path)
        return False
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


def _go_module_lookup(pkg: str, sibling_decls: list[dict]) -> tuple[str | None, str | None]:
    """D12：go build 报错给的是【包路径】（github.com/x/y/z），go.mod require 的是
    【模块路径】（github.com/x/y）——子包场景（Go 常态）键恒不相等，旧精确匹配恒跳过。

    按**最长前缀**匹配兄弟声明的模块路径（`pkg == mod` 或 `pkg` 以 `mod/` 为前缀）；
    多个命中时优先坐标可用者（长前缀带 replace 伴随=None 不可移植时，退到较短但可用的）。
    返回 (模块路径, 坐标)；无覆盖 → (None, None)。
    """
    matches: list[tuple[str, str | None]] = []
    for decl in sibling_decls:
        for mod, coord in decl.items():
            if pkg == mod or pkg.startswith(mod + "/"):
                matches.append((mod, coord))
    if not matches:
        return None, None
    matches.sort(key=lambda mc: len(mc[0]), reverse=True)  # 最长前缀优先
    for mod, coord in matches:
        if _coord_usable("go", coord):
            return mod, coord
    return matches[0]  # 全不可用 → 返回最长者让上层如实跳过（绝不退而臆造）


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
    # ★#29-5 W-3：入口统一 resolve★——target 的两个来源（_failure_manifest/
    # _nearest_manifest）内部都 resolve 后返回；root 不 resolve 时
    # `target.relative_to(root)` 在 symlink/平台前缀形态（macOS /var→/private/var）
    # 恒抛 ValueError（实跑复现）。root resolve 后与 target 同源，相对化恒成功。
    # 下游消费的是【相对路径】，与根形态无关 ⇒ 行为不变只更稳。
    root = Path(project_path).resolve()
    if not root.is_dir():
        return 0, []
    deps = _missing_deps(build_output or "", stack)
    if not deps:
        return 0, []
    target, evidence = _failure_manifest(root, build_output, filename, stack)
    if target is None:
        if evidence:
            # W-4 fail-closed：有出错文件证据但映射不回项目内 manifest（逃逸/失效路径）
            # → 绝不退而凭 modified 首文件猜目标（注错 manifest 比不注更糟）。
            logger.warning(
                "[L1.2.1·repair] A2 %s 出错文件证据映射不回项目内 manifest → fail-closed 不注入",
                stack)
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
        if stack == "go":
            # D12：包路径 → 模块路径最长前缀解析（go.mod require 的是模块不是包）
            decl_key, coord = _go_module_lookup(dep, sibling_decls)
            if decl_key is None:
                logger.info(
                    "[L1.2.1·repair] A2 go 包路径 %s 无兄弟模块覆盖（精确/前缀皆无）→ 跳过",
                    dep,
                )
                continue
        else:
            decl_key = dep
            coord = next((decl[dep] for decl in sibling_decls if dep in decl), None)
        if not _coord_usable(stack, coord):
            continue  # 兄弟无坐标/坐标不可移植 → 非项目自证，绝不臆造，交回上游（BLOCKED/等生产者）
        try:
            if _INJECT[stack](target, decl_key, coord):
                # ★#29-5 W-3：原子记账★——写盘成功 ⇒ injected 与 touched 必须同块
                # 同时记账，绝不允许分叉。旧形态把 relative_to 与 injected+=1 放同一
                # try：relative_to 抛 ValueError ⇒ 落下面 except 打「注入失败」WARNING
                # 而文件真被改了（fail-honest 违），且返回 (1, []) ⇒ count 触发重跑
                # 构建而 paths 空 ⇒ 不进 repaired_file_paths ⇒ 修复从交付产物蒸发
                # （macOS /var→/private/var 天然复现，pytest tmp_path 已 resolved
                # 从不触发=平台相关假绿）。
                rel: str | None
                try:
                    rel = str(target.relative_to(root))
                except ValueError:
                    # root 已 resolve 后本不应抛；relpath 兜底——路径再怪也绝不
                    # 上抛进 except（上抛=脱账新形态：文件已改而 injected 不记）。
                    try:
                        rel = os.path.relpath(target, root)
                    except ValueError:
                        rel = None  # Windows 跨盘符（reviewer F-2：POSIX 才恒成功）
                injected += 1
                if rel is None or rel.split(os.sep)[0] == ".." or rel.split("/")[0] == "..":
                    # ★W-3 R1 双票同根 F1★：兜底触发=出了设计外的事，【绝不静默】；
                    # 且越界产物（"../x"）下游三处全不防（push 会读项目外文件、
                    # _norm_rel 放行 ⇒ git diff rc=128 连坐、pull-back 写项目外）——
                    # fail-closed：injected 照记（文件真改了，触发重跑=诚实）但越界
                    # 路径绝不进交付账（宁缺不毒），WARNING 留全量现场。
                    logger.warning(
                        "[L1.2.1·repair] A2 %s 注入 %s 已写盘但路径相对化产物越界/失败"
                        "（rel=%r, target=%s, root=%s）⇒ 计数照记但路径不进交付账"
                        "（fail-closed：越界路径毒化 push/diff/pull-back 比缺失更糟）",
                        stack, decl_key, rel, target, root)
                    continue
                if rel not in touched:
                    touched.append(rel)
                logger.info(
                    "[L1.2.1·repair] A2 多栈补依赖(%s)：据兄弟 manifest 自证坐标把 %s 注入 %s",
                    stack, decl_key, rel,
                )
        except (OSError, ValueError) as exc:
            # 写用户 manifest 半途失败必须可见（w 模式先截断，disk-full 半途 = 文件损坏）。
            logger.warning("[L1.2.1·repair] A2 %s 注入 %s 失败(跳过): %s", stack, dep, exc)
    return injected, touched
