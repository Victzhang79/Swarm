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

    def ref_tree_paths(self, ref: str, src: str | None, project_path: str,
                       timeout: int, run: RunProbe) -> list[str] | None:
        """ref → 该容器在树里**应当落**的相对路径**词干**集（不含扩展名）。

        ★三态（复核 CRITICAL-2）★ `None` = **解不出**（UNKNOWN），`[]` = 确定无词干，
        非空 = 词干集。原实现只有两态，`[]` 被两个消费者**同侧**解读（步骤 3：不在树里→
        推向 BLOCKED；步骤 4：非自产→推向 BLOCKED）⇒ 这条原语**没有任何一侧是 fail-closed**，
        docstring 里写的"fail-closed"是假的。三态让步骤 4 能把 UNKNOWN 与"确定不自产"分开：
        解不出时**不敢断言外部生产者**，裁决落回 FAIL 修复梯（误 BLOCKED 才是贵的那一侧）。

        `src` = 报错文件（相对工程根）。TS/JS 的相对导入是**相对报错文件**而非工程根
        （`src/app.ts` 里的 `./routes/users` → `src/routes/users`），无它必错。

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
    p = _norm_rel(rel_path)
    s = _norm_rel(stem)
    if not p:
        return False
    if stem == "":
        # 根词干（Go 根包）：只认**工程根直下**的文件，绝不吃子目录（否则整棵树都算自产）
        return "/" not in p
    if not s:
        return False
    if p.startswith(s + "/"):
        return True
    return p.rsplit(".", 1)[0] == s if "." in p.rsplit("/", 1)[-1] else p == s


def _norm_rel(path: str | None) -> str:
    """归一相对路径：只剥**前导 `./`** 与前导 `/`。

    ★复核 LOW-3★ 原写法 `lstrip("./")` 会剥掉任意 `.`/`/` 组合 → `.github/x` 变
    `github/x`、`.mvn/wrapper` 变 `mvn/wrapper`。本仓已被这个惯用法坑过一次
    （`.mvn/wrapper`、`.yarn/releases` 被当噪声剔没）。
    """
    p = str(path or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def _memoized(run: RunProbe) -> RunProbe:
    """按 (cmd, project_path) 记忆只读探针结果——同一次求解内工程树不变，结果必相同。

    只读命令（`awk go.mod` / `ls` / `grep`）无副作用，memo 语义安全。作用域＝一次调用，
    绝不跨任务（那会缓存过期的工程树状态）。
    """
    cache: dict[tuple[str, str], tuple[int, str, str]] = {}

    def _run(cmd: str, project_path: str, timeout: int = 60):
        key = (cmd, project_path)
        if key not in cache:
            cache[key] = run(cmd, project_path, timeout)
        return cache[key]
    return _run


def _dedupe(refs: list[MissingRef]) -> list[MissingRef]:
    """按 (ref, symbol, src) 去重保序——同一缺失在多文件报错时每个报错文件各留一条。

    ★复核 LOW-4★ 键原为 (ref, symbol)，会把两个**不同文件**里的同名裸 `undefined: X`
    塌成一条 → 只按第一个文件的目录反解容器（Go 的 `resolve_ref` 依赖 `src`），另一个
    文件所在包的缺失证据静默丢失。src 进键即可，代价只是多一条同名证据。

    ★同一 ref 既有带 src 的证据又有不带的 → 收敛到带 src 的那条★ 各栈的多条正则会
    命中**同一行**（如 tsc 的 `… error TS2307: Cannot find module './x'` 整行同时匹配 TS 式
    与 require 式，后者拿不到报错文件）。留下 src=None 的那条 ⇒ 步骤 4 解不出归属 ⇒ 整批
    落 UNKNOWN，把治好的 CRITICAL-2 通道又关掉。故先按 (ref, symbol) 分组取**证据最全**者。
    """
    best: dict[tuple[str, str | None], MissingRef] = {}
    order: list[tuple[str, str | None]] = []
    for r in refs:
        k = (r.ref, r.symbol)
        if k not in best:
            best[k] = r
            order.append(k)
        elif best[k].src is None and r.src:
            best[k] = r          # 同一缺失：带报错文件的证据胜出（src 是反解/归属的输入）
    out: list[MissingRef] = [best[k] for k in order]
    # 同一 (ref, symbol) 出现在**不同**报错文件时各留一条（LOW-4：Go 反解按 src 定容器）
    seen: set[tuple[str, str | None, str | None]] = {
        (r.ref, r.symbol, r.src) for r in out}
    for r in refs:
        k3 = (r.ref, r.symbol, r.src)
        if r.src and k3 not in seen:
            seen.add(k3)
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
# ★MED-2（复核实测）★ 原式的裸 `|package` 分支过宽：`main.go:3:8: package main` 这类
# 普通诊断行会被解析成 ref=`main` → `is_internal` 拒 → **全或无把该轮 X-C3 整个关掉，且零
# 日志**（上一批三条 CRITICAL 的同型：过宽 marker 静默解除下游武装）。故裸 `package` 分支
# 必须锚死 go 自己的两个句式尾部（`is not in std` / `is not in GOROOT`），不吃任何裸 package 行。
_GO_MISSING_PKG_RE = re.compile(
    r"(?:^|\n)(?:([^\s:]+\.go):\d+:\d+:\s*)?"
    r"(?:no required module provides package\s+([A-Za-z0-9_./\-]+)"
    r"|package\s+([A-Za-z0-9_./\-]+)(?=\s+is not in\b))"
)
# GOPATH 时代/vendor 形态的第三方缺失（MED-1：解析器漏了它 ⇒ 第三方行不存在 ⇒ 全或无
# 被静默解除）。只用来**参与全或无判据**，与上式同吃。
_GO_CANNOT_FIND_PKG_RE = re.compile(
    r"cannot find package\s+\"([^\"]+)\""
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
            ref = m.group(2) or m.group(3)
            if not ref:
                continue
            # 过滤明显非 import path 的噪声（无点无斜杠＝标准库短名，交由 is_internal 拒掉）
            out.append(MissingRef(ref=ref, symbol=None, src=m.group(1)))
        for m in _GO_CANNOT_FIND_PKG_RE.finditer(text):
            out.append(MissingRef(ref=m.group(1), symbol=None, src=None))
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

    def _resolve_qualifier(self, qualifier: str, src: str, project_path: str,
                           timeout: int, run: RunProbe) -> str | None:
        """★HIGH-2（复核实测）★ `undefined: svc.GetUser` 里的 `svc` 是**包别名**，不是
        import path。原实现把它直接当容器 → `is_internal("svc")` 恒假 → 全或无把**同批的
        裸 undefined 一起清盘** ⇒ Go 符号通道在跨包调用（Go 的常态写法）下实质零覆盖。

        治法＝从报错文件自己的 import 块**确定性反解**（别名或路径末段匹配 qualifier），
        而不是猜。解不出 → 返 None，调用方保持原样（qualifier 当容器 → is_internal 拒 →
        全盘不标）：那是 fail-closed 方向，因为 `svc` 也可能根本不是包（局部变量/类型）。
        """
        if not src or "/" in qualifier or "." in qualifier:
            return None
        # 取 import 块里形如 `alias "path"` 或 `"path"` 的行；末段==qualifier 或 别名==qualifier
        cmd = (f"grep -oE '^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*[[:space:]]+)?\"[^\"]+\"' "
               f"{_sh_quote(src)} 2>/dev/null | head -40")
        _ec, out, _e = run(cmd, project_path, min(timeout, 20))
        for line in (out or "").splitlines():
            line = line.strip()
            if '"' not in line:
                continue
            alias = line.split('"', 1)[0].strip()
            path = line.split('"')[1]
            if not path:
                continue
            if alias == qualifier or path.rsplit("/", 1)[-1] == qualifier:
                return path
        return None

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
        if r.ref and r.src and "/" not in r.ref and "." not in r.ref:
            # HIGH-2：ref 是**包别名**形态（`undefined: svc.GetUser` 的 `svc`）→ 反解成
            # 真 import path；解不出就原样留着（→ is_internal 拒 → 全盘不标，fail-closed）
            _p = self._resolve_qualifier(r.ref, r.src, project_path, timeout, run)
            return r._replace(ref=_p) if _p else r
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

    def ref_tree_paths(self, ref: str, src: str | None, project_path: str,
                       timeout: int, run: RunProbe) -> list[str] | None:
        """Go import path → 相对 module root 的目录（Go 的包**恒是目录**）。"""
        mod = self._module_path(project_path, timeout, run)
        if not mod or not (ref == mod or ref.startswith(mod + "/")):
            return None                  # 读不出 module / 非本模块 → UNKNOWN
        if ref == mod:
            # ★复核 CRITICAL-2 变体★ 根包：词干是"工程根下的 .go 文件"（不含子目录）。
            # 原实现返 [] 并自称 fail-closed，实际推向 BLOCKED ⇒ `main.go` 里的裸
            # `undefined: buildRouter` 会去等自己。用 "" 表根，由 _stem_matches 特判。
            return [""]
        return [ref[len(mod) + 1:]]

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
# ★MED-1（复核实测）★ bundler 形态的缺失（vite/rollup/webpack）原先解析器完全看不见 →
# 混合批里的**第三方**缺失行不存在 ⇒ "全或无"被静默解除武装 ⇒ 照标 BLOCKED。
# 相对路径与裸包名都收（裸包名会被 is_internal 判第三方，正是全或无要的那一票）。
_NODE_BUNDLER_RESOLVE_RE = re.compile(
    r"(?:failed to resolve import|Could not resolve|Module not found:[^\n]*?)\s*"
    r"['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
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
        for m in _NODE_BUNDLER_RESOLVE_RE.finditer(text):
            out.append(MissingRef(ref=m.group(1), symbol=None, src=None))
        for m in _TS_MISSING_EXPORT_RE.finditer(text):
            out.append(MissingRef(ref=m.group(2), symbol=m.group(3), src=m.group(1)))
        return _dedupe(out)

    def is_internal(self, ref: str, project_path: str, timeout: int,
                    run: RunProbe) -> bool:
        # 只认相对路径。`express`/`@scope/pkg` → 第三方；`~/x`、`@/x` 等别名 → 不标（诚实边界）
        return ref.startswith("./") or ref.startswith("../")

    def ref_tree_paths(self, ref: str, src: str | None, project_path: str,
                       timeout: int, run: RunProbe) -> list[str] | None:
        """`./routes/users` **相对报错文件所在目录**归一 → `src/routes/users`。

        ★复核 CRITICAL-2★ 原实现把 `./x` 当**工程根**相对（返 `['routes/users']`），而
        TS/JS 的模块说明符是相对**导入文件**的：`src/app.ts` 里的 `./routes/users` 解析到
        `src/routes/users.ts`。scope 是工程根相对 ⇒ 词干永不匹配 ⇒ 步骤 4 恒判"非自产" ⇒
        worker 去等自己（#10 幽灵生产者）。`../` 同理，原实现直接返 []＝同一 fail-open。
        无 `src` 上下文（bundler/require 形态可能没有报错文件）→ UNKNOWN，不硬猜。
        """
        if not (ref.startswith("./") or ref.startswith("../")):
            return None                       # 裸包名/别名 → 非相对导入，UNKNOWN
        if not src:
            return None                       # 无报错文件 → 解不出，UNKNOWN（不当根相对）
        base_dir = str(src).replace("\\", "/").rsplit("/", 1)[0] if "/" in str(src) else ""
        parts = [p for p in (base_dir.split("/") if base_dir else []) if p and p != "."]
        for seg in ref.split("/"):
            if seg == "..":
                if not parts:
                    return None               # 爬出工程根 → 解不出
                parts.pop()
            elif seg not in (".", ""):
                parts.append(seg)
        return ["/".join(parts)] if parts else None

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
# ★HIGH-1（复核实测）★ rustc 对 E0432 有**两种**形态，且靠段数分不开：
#   模块整个不存在（`mod svc;` 未声明）→ unresolved import `crate::svc`      ← **主形态**
#   模块在、其中的项缺失          → unresolved import `crate::svc::list_users`
#                                    + 尾注 `no `list_users` in `svc``
# 原实现一律 `rpartition("::")` ⇒ 主形态被切成容器 `crate` + 符号 `svc`，而
# `is_internal("crate")` 为假 ⇒ **全或无当场清盘** ⇒ Rust 臂在目标场景下实质零覆盖
# （原语料用的是全路径形态，那只在 svc 已存在时才出现＝已经不是"生产者未就绪"）。
# 判据改成**只认 rustc 自己的尾注**：有 `no X in Y` ⇒ X 是叶符号、拆；无 ⇒ 整条是容器。
# 绝不靠段数猜（`crate::a::b` 既可能是缺模块 a::b，也可能是缺 a 里的 b）。
_RUST_NO_ITEM_IN_MOD_RE = re.compile(
    r"no\s+`([A-Za-z_]\w*)`\s+in\s+`([^`]+)`"
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
        # HIGH-1：先收 rustc 尾注声明的"缺项"叶名集——它是拆/不拆的**唯一权威**依据
        _leaves = {m.group(1) for m in _RUST_NO_ITEM_IN_MOD_RE.finditer(text)}
        for m in _RUST_UNRESOLVED_IMPORT_RE.finditer(text):
            path = m.group(1)
            container, _, leaf = path.rpartition("::")
            # 只有 rustc 明说"`leaf` 不在 `mod` 里"时才拆成 容器+符号；否则整条是缺失容器
            # （主形态 `crate::svc`：模块本身不存在，拆了会得到 `crate` 这个非法容器）。
            if leaf in _leaves and container:
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

    def ref_tree_paths(self, ref: str, src: str | None, project_path: str,
                       timeout: int, run: RunProbe) -> list[str] | None:
        """`crate::a::b` → 词干 `src/a/b`（`src/a/b.rs` 或 `src/a/b/mod.rs` 都落在它上）。

        `self::`/`super::` 无上下文无法定位 → **UNKNOWN**（原实现返 []＝被读成"非自产"
        ⇒ 推向 BLOCKED，方向错了；复核 CRITICAL-2 同族）。
        """
        rel = self._mod_rel(ref)
        return [f"src/{rel}"] if rel else None

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

    def ref_tree_paths(self, ref: str, src: str | None, project_path: str,
                       timeout: int, run: RunProbe) -> list[str] | None:
        """`app.services.user` → 词干 `app/services/user` + src-layout 变体 `src/…`。

        两个词干都返（`is_internal` 已认这两种布局）——它是**候选**集，任一命中即算owner。
        """
        rel = ref.replace(".", "/").strip("/")
        return [rel, f"src/{rel}"] if rel else None

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

    def ref_tree_paths(self, ref: str, src: str | None, project_path: str,
                       timeout: int, run: RunProbe) -> list[str] | None:
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
    timeout: int, run: RunProbe, refs_out: list | None = None,
) -> tuple[set[str], list[str]]:
    """通用求解器（步骤 1-3）→ `(被阻断的容器集合, 符号级 FQN 列表)`。

    与 JVM 版**同律**（`_build_blocked_on_unbuilt_internal` 的注释是权威）：
      · 只要有**一条**缺失是第三方 → 返空（交 dep-repair，照常 FAIL）
      · 只要有**一条**已在树里 → 返空（真编译错，照常 FAIL）
    ★这个"全或无"是刻意的★ 混合形态下标 BLOCKED 会让 worker 去等一个不存在的生产者，
    而漏标只是退回 FAIL 修复梯——两种错的代价不对称。

    返回的两个值分别喂 `blocked_on_packages` / `blocked_on_classes`。

    ★注意（复核 CRITICAL-1，尚未闭合）★ 这两个键的 brain 侧消费者
    （`recovery._producers_of` / `recovery._package_in_baseline` /
    `failure._derive_missing_type_files`）**写死 Java 点分 FQN → 路径**口径，非 JVM 的 ref
    进去恒解不开（实测四栈全灭）⇒ 无生产者 + 不在基线 + 推不出该建啥 = 首轮连坐放弃。
    在那三个消费者按栈解之前，本模块产出的 ref 只对 worker 侧退避有意义。

    `refs_out`：可选 out 参数，回填**解析并反解后**的 `MissingRef` 列表——步骤 4 需要其中的
    `src` 来解相对导入（TS/JS 的 `./x` 相对报错文件），拿裸 ref 字符串解不出。
    """
    drv = driver_for(language_key)
    if drv is None or drv.key in _SELF_HANDLED_KEYS:
        return set(), []
    refs = drv.parse_missing(build_output)
    if not refs:
        return set(), []
    # ★复核 MED-6★ 探针放大：10 个 ref 实测 30 次调用，其中 20 次是重复读同一个 go.mod
    # （is_internal 与 present_in_tree 各读一次）。E2E 下每次是一趟远程沙箱 run_command，
    # 且本函数处在**失败路径**上。同一命令在一次求解内结果不变 → 按 (cmd, path) memo。
    # 只活在本次调用的闭包里，绝不跨任务缓存（工程树会变）。
    run = _memoized(run)
    resolve = getattr(drv, "resolve_ref", None)
    if resolve is not None:
        refs = [resolve(r, project_path, timeout, run) for r in refs]
    if refs_out is not None:
        refs_out.extend(refs)          # 步骤 4 要 src 解相对导入（裸 ref 解不出）
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
    language_key: str | None, refs, scope_files: list[str],
    project_path: str, timeout: int, run: RunProbe,
) -> tuple[set[str], set[str]]:
    """步骤 4 的**非 JVM 半边**：`refs` 里哪些容器的生产者就在本子任务 scope 内。

    ★为什么必须有这个函数★ 步骤 4 的判据（"worker 无权等自己"）刻意留在共用层，但共用层
    的实现 `_missing_internal_produced_in_scope` 经 `classpath_fqn_key` → **JVM-only**，
    对 go/rust/node/python 布局恒返 None ⇒ own 集恒空 ⇒ 那道闸对非 JVM **静默失效**：
    子任务自己该建 `internal/svc/user.go` 却漏建，会被判 BLOCKED 去等一个永不到来的生产者
    （#10 幽灵生产者，烧满退避阶梯）。X-C3 之前非 JVM 到不了裁决点，缺口潜伏无害；X-C3
    把它激活了。这是"复用单一事实源 ≠ 复用其消费契约"的又一实例——判据共享，实现必须分栈。

    `refs` 接受 `MissingRef` 序列（需要 `src` 解相对导入）或裸 ref 字符串序列（后者对
    TS/JS 会拿不到报错文件 ⇒ 落 UNKNOWN，不静默当根相对）。

    返回 `(自产的 ref 集, 归属解不出的 ref 集)`。
    ★三态的理由（复核 CRITICAL-2）★ 原实现只返自产集，"解不出"与"确定不自产"塌成同一个
    空集 → 上层把它当"外部生产者"→ 判 BLOCKED = worker 去等自己。现在解不出单独回传，
    由裁决层决定（见 `decide_unbuilt_internal_verdict`：有 UNKNOWN 就不敢断言外部生产者）。
    """
    drv = driver_for(language_key)
    if drv is None or drv.key in _SELF_HANDLED_KEYS or not refs:
        return set(), set()
    get_paths = getattr(drv, "ref_tree_paths", None)
    if get_paths is None:
        return set(), set()
    rels = [str(f) for f in scope_files if str(f).strip()]
    own: set[str] = set()
    unresolved: set[str] = set()
    for r in refs:
        ref = r.ref if isinstance(r, MissingRef) else str(r)
        src = r.src if isinstance(r, MissingRef) else None
        if not ref:
            continue
        try:
            stems = get_paths(ref, src, project_path, timeout, run)
        except Exception:  # noqa: BLE001 — 路径映射异常 → 归属未知（绝不当"非自产"）
            unresolved.add(ref)
            continue
        if stems is None:
            unresolved.add(ref)
            continue
        if any(_stem_matches(f, s) for s in stems for f in rels):
            own.add(ref)
    return own, unresolved
