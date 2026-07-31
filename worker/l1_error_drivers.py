"""L1 构建错误归因的**栈驱动层**（X-C3；27 号文 §3.2 CRITICAL）。

## 治的是什么

"等生产者"BLOCKED 通道原本 **JVM 独占**：`l1_parse._MISSING_PKG_RE` 正则锚死 `.java` +
Maven 的 `[行,列]`，`_MISSING_SYMBOL_CLASS_RE` 锚死 javac 的三行组。于是
  Go `no required module provides package` · TS `TS2307` · Rust `E0432` · Python
  `ModuleNotFoundError`
**全部识别不出** → 拿不到 `internal_pkg_not_built` → 落 `build_failed` capability 硬 FAIL
→ 烧修复轮 → abandon → 连坐。**这正是 round38/round67 在 Java 上花十几轮治的头号死法，
换栈完全复发。**

## 为什么是"驱动"而不是"再加四条正则"

JVM 那条链有 **5 步**，只有前 3 步是栈相关的：

| 步 | JVM 现状 | 栈相关？ |
|---|---|---|
| 1 解析缺失标识 | `_MISSING_PKG_RE` / `_MISSING_SYMBOL_CLASS_RE` | **是** |
| 2 判内部 vs 第三方 | `_project_own_packages`（源码声明的包根）+ `_DEP_REPAIR_SKIP_PREFIXES` | **是** |
| 3 树里是否已声明 | `grep '^package P;' --include='*.java'` | **是** |
| 4 生产者是否在本子任务 scope 内 → capability FAIL 而非 BLOCKED | `_missing_internal_produced_in_scope`（R67L-B2） | **否** |
| 5 出裁决 + `blocked_on_packages`/`blocked_on_classes` | 裁决点 | **否** |

★**4/5 步刻意留在共用层，绝不下沉到 driver**★ ——第 4 步是"worker 无权等自己"的
capability 边界（下沉即每栈重写一遍、必然腐化）；第 5 步的类级 FQN 是 brain 侧
**防臆造类 futile 判据**的唯一输入。两者都是"重复实现即失守"的横切不变量。

## 内部 vs 第三方的判据（各栈同源，来自 JVM 的血泪）

`_project_own_packages` 的注释写明：**绝不用 pom `<groupId>`** ——它含一堆第三方 group
（com.alibaba/org.springframework…），据它判会把 `fastjson2` 当"自有" → 缺第三方包被误
BLOCKED 且不补依赖。可靠分界是：**项目自己 build 的东西必由自己的源码"声明"，第三方只被
"引用"从不被声明。** 本模块各栈驱动一律沿用该判据的同构形式：

- **JVM**：源码 `package X.Y` 声明的 2 段包根
- **Go**：`go.mod` 的 `module <path>` —— import path 以它为前缀＝同模块内（自己 build）
- **Node/TS**：相对路径（`./` `../`）恒内部；`tsconfig` `paths` 别名映射到工程内目录者内部
- **Rust**：`crate::`/`self::`/`super::` 恒内部；`Cargo.toml` 的 workspace member 名
- **Python**：工程内的顶层包目录（含 `__init__.py`）与顶层模块文件名

## 落点

**本批只给非 JVM 栈接线，JVM 路径一字不动**（它是唯一跑过 E2E 的栈；把它改成走 driver 是
纯重构、零收益、却把 4900 行文件的重试通道置于回归风险下）。JVM 以**薄适配器**登记进
registry，让"哪些栈有 error driver"有单一事实源、且可被测试枚举。
"""
from __future__ import annotations

import re
from typing import Callable, NamedTuple, Protocol

# 运行只读探测命令的注入点（与 l1_pipeline 的 `_run_check_split` 同形）：
# (cmd, project_path, timeout) -> (exit_code, stdout, stderr)
RunProbe = Callable[[str, str, int], tuple[int, str, str]]


class MissingRef(NamedTuple):
    """一条"引用了尚未建出的项目内部标识"的证据。

    `ref` = 容器级标识（JVM 包名 / Go import path / TS 模块路径 / Rust 模块路径 /
    Python 模块名）。`symbol` 非空 = **容器已在树里、其中某个符号未建出**（JVM 的
    「cannot find symbol: class C / location: package P」同型）；None = 整个容器缺失。
    `src` = 报错文件（可缺，仅作证据留痕）。
    """

    ref: str
    symbol: str | None = None
    src: str | None = None


class ErrorDriver(Protocol):
    """栈驱动契约：只管前 3 步（解析 / 判内部 / 树内是否已有）+ 一条路径映射原语。

    ★三个方法都必须"宁可不标也不误标"★ ——误标 BLOCKED 会让 worker 去等一个永不到来的
    生产者（#10 幽灵生产者，烧满退避阶梯）；漏标只是退回现状（FAIL 修复梯）。故一律
    fail-closed：任何不确定 → 返回"不是内部未建出"。
    """

    key: str

    def parse_missing(self, build_output: str) -> list[MissingRef]:
        """从构建输出解析缺失标识。纯函数、可单测、去重保序。"""
        ...

    def is_internal(self, ref: str, project_path: str, timeout: int,
                    run: RunProbe) -> bool:
        """该标识是否属于**本工程自己 build 的东西**（而非第三方/标准库）。"""
        ...

    def ref_tree_paths(self, ref: str, project_path: str, timeout: int,
                       run: RunProbe) -> list[str]:
        """ref → 该容器在树里**应当落**的相对路径**词干**集（不含扩展名）。

        ★这条原语是"树内是否已有"（步骤 3）与"生产者是否在本子任务 scope 内"（步骤 4）
        的**共同**路径口径★。步骤 4 留在共用层（`_missing_internal_produced_in_scope`），
        但它原本经 `classpath_fqn_key` 实现＝**JVM-only**，对非 JVM 布局恒返 None ——
        非 JVM 栈的"worker 无权等自己"闸因此恒空过。X-C3 之前非 JVM 到不了那个裁决点，
        缺口潜伏无害；X-C3 把它**激活**了（fail-open：自己漏建 → 去等永不到来的生产者）。
        故把"ref ↔ 路径"这一**唯一栈相关部分**下沉成原语，判据本身仍在共用层。

        词干语义（各栈同构）：`a/b` 既可物化为 `a/b.<ext>`，也可物化为目录 `a/b/`。
        匹配一律走 `_stem_matches`（词干相等 或 落在 `<词干>/` 下），绝不逐扩展名穷举。
        无法定位（`self::`/`super::` 无上下文、读不出 go.mod）→ 返 `[]`（fail-closed）。
        """
        ...

    def present_in_tree(self, ref: str, symbol: str | None, project_path: str,
                        timeout: int, run: RunProbe) -> bool:
        """标识是否**已在当前工程树里**。True ⇒ 真编译错（非未就绪）⇒ 照常 FAIL。"""
        ...


def _stem_matches(rel_path: str, stem: str) -> bool:
    """相对路径是否**落在**该词干上：词干相等（去扩展名）或位于 `<词干>/` 子树。

    `routes/users.ts` vs 词干 `routes/users` → True（去扩展名相等）
    `routes/users/index.ts` vs 词干 `routes/users` → True（子树）
    `routes/users_admin.ts` vs 词干 `routes/users` → **False**（★不许裸 startswith★，
    否则 `svc` 会吃掉 `svc_test`/`svcutil`＝把别人的文件算成自己的产出、误抑 BLOCKED）
    """
    p = str(rel_path or "").replace("\\", "/").lstrip("./").lstrip("/")
    s = str(stem or "").replace("\\", "/").lstrip("./").lstrip("/")
    if not p or not s:
        return False
    if p.startswith(s + "/"):
        return True
    return p.rsplit(".", 1)[0] == s if "." in p.rsplit("/", 1)[-1] else p == s


def _dedupe(refs: list[MissingRef]) -> list[MissingRef]:
    """按 (ref, symbol) 去重保序——同一缺失在多文件报错时只留一条证据。"""
    seen: set[tuple[str, str | None]] = set()
    out: list[MissingRef] = []
    for r in refs:
        k = (r.ref, r.symbol)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _strip_ansi(text: str) -> str:
    """剥 ANSI —— 并行/带色构建输出会把转义插进多行组里让正则静默失配（A5 同源）。"""
    return re.sub(r"\x1b\[[0-9;]*m", "", text or "")


# ══════════════════════════════════════════════════════════════════
# Go
# ══════════════════════════════════════════════════════════════════

# `go build ./...` 的两种缺包形态（都带 file:line:col 前缀，但**不强制**——
# vet/list 路径下可能裸出）：
#   main.go:5:2: no required module provides package github.com/x/y/internal/svc
#   main.go:5:2: package github.com/x/y/internal/svc is not in std (/usr/.../svc)
_GO_MISSING_PKG_RE = re.compile(
    r"(?:^|\n)(?:([^\s:]+\.go):\d+:\d+:\s*)?"
    r"(?:no required module provides package|package)\s+"
    r"([A-Za-z0-9_./\-]+)"
    r"(?=[\s;]|\s+is not in|$)"
)
# 同包内符号未建出（容器在、符号缺）——Go 的类级同型：
#   ./handler.go:12:9: undefined: ListUsers
#   ./handler.go:12:9: undefined: svc.ListUsers
_GO_UNDEFINED_RE = re.compile(
    r"(?:^|\n)(?:\./)?([^\s:]+\.go):\d+:\d+:\s*undefined:\s*"
    r"(?:([A-Za-z0-9_.]+)\.)?([A-Za-z_][A-Za-z0-9_]*)"
)


class GoErrorDriver:
    """Go：内部性判据＝`go.mod` 的 module path 前缀（自己 build 的必在同模块内）。"""

    key = "go"
    symbol_sep = "."

    def parse_missing(self, build_output: str) -> list[MissingRef]:
        text = _strip_ansi(build_output)
        out: list[MissingRef] = []
        for m in _GO_MISSING_PKG_RE.finditer(text):
            ref = m.group(2)
            # 过滤明显非 import path 的噪声（无点无斜杠＝标准库短名，交由 is_internal 拒掉）
            out.append(MissingRef(ref=ref, symbol=None, src=m.group(1)))
        for m in _GO_UNDEFINED_RE.finditer(text):
            qualifier, sym = m.group(2), m.group(3)
            # `undefined: svc.ListUsers` → 容器是 svc（同模块内的包别名，无法从这行还原
            # 完整 import path）；`undefined: ListUsers` → 容器是报错文件自己所在包。
            # 两者都只有【符号级】信息，容器留 qualifier 或空串由 is_internal 兜。
            out.append(MissingRef(ref=qualifier or "", symbol=sym, src=m.group(1)))
        return _dedupe(out)

    def _module_path(self, project_path: str, timeout: int, run: RunProbe) -> str:
        """读 `go.mod` 的 `module <path>`。★不用 go list★ 它要求工具链在场且会联网。"""
        cmd = ("awk '/^[[:space:]]*module[[:space:]]/{print $2; exit}' go.mod "
               "2>/dev/null || true")
        _ec, out, _e = run(cmd, project_path, min(timeout, 20))
        return (out or "").strip()

    def resolve_ref(self, r: MissingRef, project_path: str, timeout: int,
                    run: RunProbe) -> MissingRef:
        """把裸 `undefined: X` 的容器补成**报错文件自己所在包**的 import path。

        ★为什么值得做★ 这正是 H-3a 那个形态（容器在树里、其中某符号未建出）——它在 Java 上
        害得 round67 st-50-1 被判 hard fail 弃修、烧了十几轮才定案。Go 里 `undefined: X`
        是它的同型，且**比 Java 更常见**（同包多文件是 Go 的常态组织方式，跨子任务分工必然
        产生"我引用同包里别人还没写的函数"）。

        不做的话该形态恒 fail-closed 落 FAIL 修复梯 = worker 反复去修一个"别人还没建"的
        符号，正是 X-C3 要治的病在 Go 上原样保留。
        """
        if r.ref or not r.src:
            return r
        mod = self._module_path(project_path, timeout, run)
        if not mod:
            return r
        # 报错文件所在目录 → 相对 module root 的 import path
        _slash = r.src.rfind("/")
        rel_dir = r.src[:_slash] if _slash > 0 else ""
        return r._replace(ref=(mod + "/" + rel_dir) if rel_dir else mod)

    def is_internal(self, ref: str, project_path: str, timeout: int,
                    run: RunProbe) -> bool:
        if not ref:
            return False          # 容器仍不可知（无 src / 读不出 module）→ fail-closed
        mod = self._module_path(project_path, timeout, run)
        if not mod:
            return False          # 读不出 module path → fail-closed
        return ref == mod or ref.startswith(mod + "/")

    def ref_tree_paths(self, ref: str, project_path: str, timeout: int,
                       run: RunProbe) -> list[str]:
        """Go import path → 相对 module root 的目录（Go 的包**恒是目录**）。"""
        mod = self._module_path(project_path, timeout, run)
        if not mod or not (ref == mod or ref.startswith(mod + "/")):
            return []
        rel = ref[len(mod) + 1:] if ref != mod else ""
        return [rel] if rel else []      # ref==mod（根包）→ 无词干可锚，fail-closed

    def present_in_tree(self, ref: str, symbol: str | None, project_path: str,
                        timeout: int, run: RunProbe) -> bool:
        mod = self._module_path(project_path, timeout, run)
        if not mod or not (ref == mod or ref.startswith(mod + "/")):
            return False
        rel = "." if ref == mod else ref[len(mod) + 1:]
        if symbol is None:
            # 整包缺失：该目录下有任何 .go 文件即"已在树里"（→ 真编译错）
            cmd = (f"ls {_sh_quote(rel)}/*.go 2>/dev/null | head -1")
            _ec, out, _e = run(cmd, project_path, min(timeout, 20))
            return bool((out or "").strip())
        # 符号级：该目录下是否已有源码声明该标识（func/type/var/const）
        cmd = (f"grep -rlE '^[[:space:]]*(func|type|var|const)[[:space:]]+"
               f"{re.escape(symbol)}\\b' --include='*.go' {_sh_quote(rel)} "
               f"2>/dev/null | head -1")
        _ec, out, _e = run(cmd, project_path, min(timeout, 20))
        return bool((out or "").strip())


def _sh_quote(p: str) -> str:
    """最小 shell 转义——路径来自构建输出（外部输入），绝不裸拼进命令。"""
    return "'" + str(p).replace("'", "'\\''") + "'"


# ══════════════════════════════════════════════════════════════════
# Node / TypeScript
# ══════════════════════════════════════════════════════════════════

# tsc：src/app.ts(3,24): error TS2307: Cannot find module './routes/users' or its ...
_TS_MISSING_MODULE_RE = re.compile(
    r"(?:^|\n)([^\s(]+)\(\d+,\d+\):\s*error TS2307:\s*Cannot find module\s*"
    r"['\"]([^'\"]+)['\"]"
)
# 容器在、导出缺（H-3a 同型）：
#   src/app.ts(5,10): error TS2305: Module '"./svc"' has no exported member 'listUsers'.
_TS_MISSING_EXPORT_RE = re.compile(
    r"(?:^|\n)([^\s(]+)\(\d+,\d+\):\s*error TS2305:\s*Module\s*"
    r"['\"]+([^'\"]+)['\"]+\s*has no exported member\s*['\"]([^'\"]+)['\"]"
)
# node 运行期（`npm start` 冒烟/测试路径也会走 L1）：
#   Error: Cannot find module './routes/users'
_NODE_REQUIRE_RE = re.compile(
    r"Cannot find module\s*['\"](\.[^'\"]+)['\"]"
)


class NodeErrorDriver:
    """Node/TS：相对路径恒内部（`./` `../`）——这是**字面即证据**的最强判据。

    裸包名（`express`）一律判第三方，交 dep-repair；`tsconfig` 的 `paths` 别名不在本批
    覆盖面（要解析 tsconfig 的 JSONC + baseUrl 组合，属独立机制）→ fail-closed 不标。
    诚实边界写在此处，别让下一个人以为别名已支持。
    """

    key = "node"
    symbol_sep = "#"      # ESM 惯例：module#export

    def parse_missing(self, build_output: str) -> list[MissingRef]:
        text = _strip_ansi(build_output)
        out: list[MissingRef] = []
        for m in _TS_MISSING_MODULE_RE.finditer(text):
            out.append(MissingRef(ref=m.group(2), symbol=None, src=m.group(1)))
        for m in _NODE_REQUIRE_RE.finditer(text):
            out.append(MissingRef(ref=m.group(1), symbol=None, src=None))
        for m in _TS_MISSING_EXPORT_RE.finditer(text):
            out.append(MissingRef(ref=m.group(2), symbol=m.group(3), src=m.group(1)))
        return _dedupe(out)

    def is_internal(self, ref: str, project_path: str, timeout: int,
                    run: RunProbe) -> bool:
        # 只认相对路径。`express`/`@scope/pkg` → 第三方；`~/x`、`@/x` 等别名 → 不标（诚实边界）
        return ref.startswith("./") or ref.startswith("../")

    def ref_tree_paths(self, ref: str, project_path: str, timeout: int,
                       run: RunProbe) -> list[str]:
        """`./routes/users` → 词干 `routes/users`（既可是 `.ts` 文件也可是 `/index.ts` 目录）。

        ★`../` 不返词干★ 它相对**报错文件**而非工程根，无 src 上下文无法归一 → fail-closed。
        """
        if not ref.startswith("./"):
            return []
        base = ref[2:].strip("/")
        return [base] if base else []

    def present_in_tree(self, ref: str, symbol: str | None, project_path: str,
                        timeout: int, run: RunProbe) -> bool:
        if not (ref.startswith("./") or ref.startswith("../")):
            return False
        base = ref[2:] if ref.startswith("./") else ref
        # 模块解析：<base>.{ts,tsx,js,jsx,mjs,cjs} 或 <base>/index.*
        cmd = (f"ls {_sh_quote(base)}.ts {_sh_quote(base)}.tsx {_sh_quote(base)}.js "
               f"{_sh_quote(base)}.jsx {_sh_quote(base)}.mjs {_sh_quote(base)}.cjs "
               f"{_sh_quote(base)}/index.* 2>/dev/null | head -1")
        _ec, out, _e = run(cmd, project_path, min(timeout, 20))
        found = bool((out or "").strip())
        if symbol is None:
            return found
        if not found:
            return False   # 容器都不在 → 容器级缺失，交由 symbol=None 那条证据处理
        # 容器在：是否已导出该符号（`export … symbol` / `export { symbol }`）
        cmd = (f"grep -rlE 'export[^\\n]*\\b{re.escape(symbol)}\\b' "
               f"{_sh_quote(base)}.ts {_sh_quote(base)}.js {_sh_quote(base)}/index.* "
               f"2>/dev/null | head -1")
        _ec, out, _e = run(cmd, project_path, min(timeout, 20))
        return bool((out or "").strip())


# ══════════════════════════════════════════════════════════════════
# Rust
# ══════════════════════════════════════════════════════════════════

# error[E0432]: unresolved import `crate::svc::list_users`
_RUST_UNRESOLVED_IMPORT_RE = re.compile(
    r"error\[E0432\]:\s*unresolved import\s*`([^`]+)`"
)
# error[E0433]: failed to resolve: use of undeclared crate or module `svc`
_RUST_UNDECLARED_RE = re.compile(
    r"error\[E0433\]:\s*failed to resolve:[^\n`]*`([^`]+)`"
)
# error[E0425]: cannot find function `list_users` in module `crate::svc`
_RUST_CANNOT_FIND_IN_MOD_RE = re.compile(
    r"error\[E042[15]\]:\s*cannot find \w+\s*`([^`]+)`\s*in (?:module|crate)\s*`([^`]+)`"
)


class RustErrorDriver:
    """Rust：`crate::`/`self::`/`super::` 前缀恒内部（模块路径**字面即证据**）。

    外部 crate 名（`serde::…`）→ 第三方；workspace member 名的解析不在本批覆盖面
    （要读 `Cargo.toml` 的 `[workspace] members` + 各成员 package name）→ fail-closed。
    """

    key = "rust"
    symbol_sep = "::"

    _INTERNAL_PREFIXES = ("crate::", "self::", "super::")

    def parse_missing(self, build_output: str) -> list[MissingRef]:
        text = _strip_ansi(build_output)
        out: list[MissingRef] = []
        for m in _RUST_UNRESOLVED_IMPORT_RE.finditer(text):
            path = m.group(1)
            # `crate::svc::list_users` → 容器 crate::svc、符号 list_users
            if "::" in path:
                container, _, leaf = path.rpartition("::")
                out.append(MissingRef(ref=container, symbol=leaf, src=None))
            else:
                out.append(MissingRef(ref=path, symbol=None, src=None))
        for m in _RUST_UNDECLARED_RE.finditer(text):
            out.append(MissingRef(ref=m.group(1), symbol=None, src=None))
        for m in _RUST_CANNOT_FIND_IN_MOD_RE.finditer(text):
            out.append(MissingRef(ref=m.group(2), symbol=m.group(1), src=None))
        return _dedupe(out)

    def is_internal(self, ref: str, project_path: str, timeout: int,
                    run: RunProbe) -> bool:
        return ref.startswith(self._INTERNAL_PREFIXES)

    def _mod_rel(self, ref: str) -> str:
        """`crate::a::b` → `a/b`（`self::`/`super::` 无法在无上下文时定位 → 返空串）。"""
        if not ref.startswith("crate::"):
            return ""
        return ref[len("crate::"):].replace("::", "/")

    def ref_tree_paths(self, ref: str, project_path: str, timeout: int,
                       run: RunProbe) -> list[str]:
        """`crate::a::b` → 词干 `src/a/b`（`src/a/b.rs` 或 `src/a/b/mod.rs` 都落在它上）。"""
        rel = self._mod_rel(ref)
        return [f"src/{rel}"] if rel else []

    def present_in_tree(self, ref: str, symbol: str | None, project_path: str,
                        timeout: int, run: RunProbe) -> bool:
        rel = self._mod_rel(ref)
        if not rel:
            return False
        # 模块解析：src/<rel>.rs 或 src/<rel>/mod.rs
        cmd = (f"ls src/{_sh_quote(rel)}.rs src/{_sh_quote(rel)}/mod.rs "
               f"2>/dev/null | head -1")
        _ec, out, _e = run(cmd, project_path, min(timeout, 20))
        found = bool((out or "").strip())
        if symbol is None:
            return found
        if not found:
            return False
        cmd = (f"grep -rlE '\\b(fn|struct|enum|trait|type|const|static|mod)"
               f"[[:space:]]+{re.escape(symbol)}\\b' "
               f"src/{_sh_quote(rel)}.rs src/{_sh_quote(rel)}/mod.rs 2>/dev/null | head -1")
        _ec, out, _e = run(cmd, project_path, min(timeout, 20))
        return bool((out or "").strip())


# ══════════════════════════════════════════════════════════════════
# Python
# ══════════════════════════════════════════════════════════════════

# ModuleNotFoundError: No module named 'app.services.user'
_PY_MODULE_NOT_FOUND_RE = re.compile(
    r"ModuleNotFoundError:\s*No module named\s*['\"]([\w.]+)['\"]"
)
# ImportError: cannot import name 'list_users' from 'app.services.user'
_PY_CANNOT_IMPORT_NAME_RE = re.compile(
    r"ImportError:\s*cannot import name\s*['\"](\w+)['\"]\s*from\s*['\"]([\w.]+)['\"]"
)


class PythonErrorDriver:
    """Python：内部性＝**工程内存在同名顶层包/模块**（同 JVM「源码声明才算自有」判据）。

    顶层包＝含 `__init__.py` 的目录；顶层模块＝根下的 `<name>.py`。第三方（site-packages）
    在工程树里**不存在**同名顶层项 → 自然被判外部，交 dep-repair。
    """

    key = "python"
    symbol_sep = "."

    def parse_missing(self, build_output: str) -> list[MissingRef]:
        text = _strip_ansi(build_output)
        out: list[MissingRef] = []
        for m in _PY_MODULE_NOT_FOUND_RE.finditer(text):
            out.append(MissingRef(ref=m.group(1), symbol=None, src=None))
        for m in _PY_CANNOT_IMPORT_NAME_RE.finditer(text):
            out.append(MissingRef(ref=m.group(2), symbol=m.group(1), src=None))
        return _dedupe(out)

    def is_internal(self, ref: str, project_path: str, timeout: int,
                    run: RunProbe) -> bool:
        top = ref.split(".")[0]
        if not top:
            return False
        cmd = (f"ls -d {_sh_quote(top)}/__init__.py {_sh_quote(top)}.py "
               f"src/{_sh_quote(top)}/__init__.py 2>/dev/null | head -1")
        _ec, out, _e = run(cmd, project_path, min(timeout, 20))
        return bool((out or "").strip())

    def ref_tree_paths(self, ref: str, project_path: str, timeout: int,
                       run: RunProbe) -> list[str]:
        """`app.services.user` → 词干 `app/services/user` + src-layout 变体 `src/…`。

        两个词干都返（`is_internal` 已认这两种布局）——它是**候选**集，任一命中即算owner。
        """
        rel = ref.replace(".", "/").strip("/")
        return [rel, f"src/{rel}"] if rel else []

    def present_in_tree(self, ref: str, symbol: str | None, project_path: str,
                        timeout: int, run: RunProbe) -> bool:
        rel = ref.replace(".", "/")
        cmd = (f"ls -d {_sh_quote(rel)}.py {_sh_quote(rel)}/__init__.py "
               f"src/{_sh_quote(rel)}.py src/{_sh_quote(rel)}/__init__.py "
               f"2>/dev/null | head -1")
        _ec, out, _e = run(cmd, project_path, min(timeout, 20))
        found = bool((out or "").strip())
        if symbol is None:
            return found
        if not found:
            return False
        cmd = (f"grep -rlE '^[[:space:]]*(def|class|{re.escape(symbol)}[[:space:]]*=)"
               f"[[:space:]]*{re.escape(symbol)}?\\b' "
               f"{_sh_quote(rel)}.py {_sh_quote(rel)}/__init__.py "
               f"src/{_sh_quote(rel)}.py src/{_sh_quote(rel)}/__init__.py "
               f"2>/dev/null | head -1")
        _ec, out, _e = run(cmd, project_path, min(timeout, 20))
        return bool((out or "").strip())


# ══════════════════════════════════════════════════════════════════
# JVM 适配器 + registry
# ══════════════════════════════════════════════════════════════════

class JvmErrorDriver:
    """JVM 薄适配器：**只做登记与解析**，判内部/树内仍由 `l1_pipeline` 既有函数承担。

    ★刻意不把 JVM 的判据搬进来★ 它是唯一跑过 E2E 的栈，`_project_own_packages`（含
    "绝不用 pom groupId"的血泪）与两个 `_build_blocked_on_unbuilt_internal*` 都在
    `l1_pipeline` 里被多处消费。搬过来＝纯重构、零收益、却把 4900 行文件的重试通道置于
    回归风险下。本类存在的意义是让"哪些栈有 error driver"有**单一事实源**（registry），
    可被测试枚举、可被覆盖率闸消费。
    """

    key = "java"
    symbol_sep = "."

    def parse_missing(self, build_output: str) -> list[MissingRef]:
        from swarm.worker.l1_parse import (
            parse_missing_packages,
            parse_missing_symbol_classes,
        )
        out: list[MissingRef] = []
        for src, pkg in parse_missing_packages(build_output):
            out.append(MissingRef(ref=pkg, symbol=None, src=src))
        for cls, pkg in parse_missing_symbol_classes(build_output):
            out.append(MissingRef(ref=pkg, symbol=cls, src=None))
        return _dedupe(out)

    def is_internal(self, ref: str, project_path: str, timeout: int,
                    run: RunProbe) -> bool:
        raise NotImplementedError(
            "JVM 路径由 l1_pipeline._build_blocked_on_unbuilt_internal* 承担（刻意不搬）")

    def ref_tree_paths(self, ref: str, project_path: str, timeout: int,
                       run: RunProbe) -> list[str]:
        raise NotImplementedError(
            "JVM 步骤 4 走 classpath_fqn_key（JVM 类路径命名空间口径，刻意不搬）")

    def present_in_tree(self, ref: str, symbol: str | None, project_path: str,
                        timeout: int, run: RunProbe) -> bool:
        raise NotImplementedError(
            "JVM 路径由 l1_pipeline._build_blocked_on_unbuilt_internal* 承担（刻意不搬）")


# 单一事实源：栈键 → error driver。`stack_detect`/`normalize_language_key` 的语言键口径。
ERROR_DRIVERS: dict[str, ErrorDriver] = {
    "java": JvmErrorDriver(),
    "go": GoErrorDriver(),
    "node": NodeErrorDriver(),
    "rust": RustErrorDriver(),
    "python": PythonErrorDriver(),
}

# JVM 走既有专用链，不经本模块的通用求解器（见 JvmErrorDriver docstring）。
_SELF_HANDLED_KEYS = frozenset({"java"})


def driver_for(language_key: str | None) -> ErrorDriver | None:
    """取该语言的 error driver。**未收录栈返 None**（fail-closed：不标 BLOCKED）。"""
    if not language_key:
        return None
    return ERROR_DRIVERS.get(language_key)


def blocked_on_unbuilt_internal(
    language_key: str | None, build_output: str, project_path: str,
    timeout: int, run: RunProbe,
) -> tuple[set[str], list[str]]:
    """通用求解器（步骤 1-3）→ `(被阻断的容器集合, 符号级 FQN 列表)`。

    与 JVM 版**同律**（`_build_blocked_on_unbuilt_internal` 的注释是权威）：
      · 只要有**一条**缺失是第三方 → 返空（交 dep-repair，照常 FAIL）
      · 只要有**一条**已在树里 → 返空（真编译错，照常 FAIL）
    ★这个"全或无"是刻意的★ 混合形态下标 BLOCKED 会让 worker 去等一个不存在的生产者，
    而漏标只是退回 FAIL 修复梯——两种错的代价不对称。

    返回的两个值分别喂 `blocked_on_packages` / `blocked_on_classes`（与 JVM 同键同消费链，
    brain 侧反查生产者与类级 futile 判据零改动）。
    """
    drv = driver_for(language_key)
    if drv is None or drv.key in _SELF_HANDLED_KEYS:
        return set(), []
    refs = drv.parse_missing(build_output)
    if not refs:
        return set(), []
    resolve = getattr(drv, "resolve_ref", None)
    if resolve is not None:
        refs = [resolve(r, project_path, timeout, run) for r in refs]
    containers: set[str] = set()
    symbols: list[str] = []
    for r in refs:
        if not drv.is_internal(r.ref, project_path, timeout, run):
            return set(), []      # 有第三方 → 全盘不标
        if drv.present_in_tree(r.ref, r.symbol, project_path, timeout, run):
            return set(), []      # 有已在树里的 → 真编译错，全盘不标
        containers.add(r.ref)
        if r.symbol:
            # 分隔符按栈取（Rust 是 `::`、ESM 用 `#`）——混用会让 brain 侧类级 futile
            # 判据的前缀匹配失配（它按容器名前缀反查生产者子任务）。
            _sep = getattr(drv, "symbol_sep", ".")
            symbols.append(f"{r.ref}{_sep}{r.symbol}" if r.ref else r.symbol)
    return containers, sorted(set(symbols))


def produced_in_scope(
    language_key: str | None, refs: set[str], scope_files: list[str],
    project_path: str, timeout: int, run: RunProbe,
) -> set[str]:
    """步骤 4 的**非 JVM 半边**：`refs` 里哪些容器的生产者就在本子任务 scope 内。

    ★为什么必须有这个函数★ 步骤 4 的判据（"worker 无权等自己"）刻意留在共用层，但共用层
    的实现 `_missing_internal_produced_in_scope` 经 `classpath_fqn_key` → **JVM-only**，
    对 go/rust/node/python 布局恒返 None ⇒ own 集恒空 ⇒ 那道闸对非 JVM **静默失效**：
    子任务自己该建 `internal/svc/user.go` 却漏建，会被判 BLOCKED 去等一个永不到来的生产者
    （#10 幽灵生产者，烧满退避阶梯）。X-C3 之前非 JVM 到不了裁决点，缺口潜伏无害；X-C3
    把它激活了。这是"复用单一事实源 ≠ 复用其消费契约"的又一实例——判据共享，实现必须分栈。

    返回命中的容器 ref 集（**原样 ref**，非路径——调用方要拿它做集合差）。
    未收录栈 / self-handled / 取不到词干 → 空集（fail-closed 维持 BLOCKED 语义，与
    共用层 `classpath_fqn_key` 不可用时的 fail-open 取向一致：宁可多等一轮，不误判 capability）。
    """
    drv = driver_for(language_key)
    if drv is None or drv.key in _SELF_HANDLED_KEYS or not refs or not scope_files:
        return set()
    get_paths = getattr(drv, "ref_tree_paths", None)
    if get_paths is None:
        return set()
    rels = [str(f) for f in scope_files if str(f).strip()]
    own: set[str] = set()
    for ref in refs:
        try:
            stems = get_paths(ref, project_path, timeout, run) or []
        except Exception:  # noqa: BLE001 — 路径映射异常不得阻断裁决，退回"非自产"
            continue
        if any(_stem_matches(f, s) for s in stems for f in rels):
            own.add(ref)
    return own
