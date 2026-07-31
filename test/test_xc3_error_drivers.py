#!/usr/bin/env python3
"""X-C3（27 号文 §3.2 CRITICAL）：L1 构建错误归因的**栈驱动层** + 两个调用点的接线证明。

治的病：`等生产者` BLOCKED 通道原本 JVM 独占（正则锚死 `.java` + Maven `[行,列]`），
Go/TS/Rust/Python 的"引用了别的子任务还没建出的内部标识"识别不出 → 落 capability 硬 FAIL
→ 烧修复轮 → abandon → 连坐。这是 round38/round67 在 Java 上花十几轮治的头号死法。

## 本文件的断言分三类（都在证"被接上了"，不只是"实现正确"）

1. **解析器**：真实编译器输出语料（不是手抄的理想串）。
2. **通用求解器**：全或无（有一条第三方 / 有一条已在树里 → 全盘不标）+ 未收录栈 fail-closed。
3. **★接线★**：走 `run_l1_pipeline` 本体，只假造最底层探针 `_run_check_split`
   （＝真实 `_compile_files`、真实 `raw_out` 管线、真实 driver 全部执行）。
   假造 `_compile_files` 会正好绕过要证的那段接线 —— B-4a CRITICAL-1 的教训。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import swarm.worker.l1_error_drivers as ed  # noqa: E402
import swarm.worker.l1_pipeline as lp  # noqa: E402
from swarm.types import (FileScope, SubTask, SubTaskDifficulty,  # noqa: E402
                         TaskHarness)

# ═══════════════════════════════════════════════════════════════
# 真实编译器输出语料（各栈实测形态，勿"整理"成理想串）
# ═══════════════════════════════════════════════════════════════

GO_MISSING_PKG = (
    "internal/handler/user.go:7:2: no required module provides package "
    "github.com/acme/shop/internal/svc; to add it:\n\tgo get github.com/acme/shop/internal/svc\n"
)
GO_UNDEFINED_BARE = "internal/handler/user.go:19:9: undefined: ListUsers\n"
GO_QUALIFIED_UNDEFINED = (
    "# github.com/acme/shop/internal/handler\n"
    "internal/handler/user.go:20:9: undefined: svc.GetUser\n"
)
GO_THIRD_PARTY = (
    "internal/handler/user.go:8:2: no required module provides package "
    "github.com/gin-gonic/gin; to add it:\n"
)
TS_MISSING_MODULE = (
    "src/app.ts(3,24): error TS2307: Cannot find module './routes/users' or its "
    "corresponding type declarations.\n"
)
TS_MISSING_EXPORT = (
    "src/app.ts(5,10): error TS2305: Module '\"./svc\"' has no exported member 'listUsers'.\n"
)
# ★主形态★（`mod svc;` 未声明 —— 即"生产者未就绪"的真实形态）
RUST_UNRESOLVED = (
    "error[E0432]: unresolved import `crate::svc`\n"
    " --> src/handler.rs:2:5\n  |\n2 | use crate::svc::list_users;\n"
    "  |     ^^^^^^^^^^ use of undeclared crate or module `svc`\n"
)
# 模块在、其中的项缺失（rustc 带尾注 `no \`X\` in \`Y\``）
RUST_UNRESOLVED_WITH_NOTE = (
    "error[E0432]: unresolved import `crate::svc::list_users`\n"
    " --> src/handler.rs:2:5\n  |\n2 | use crate::svc::list_users;\n"
    "  |     ^^^^^^^^^^^^^^^^^^^^^^ no `list_users` in `svc`\n"
)
PY_MODULE_NOT_FOUND = (
    "Traceback (most recent call last):\n"
    '  File "app/main.py", line 3, in <module>\n'
    "    from app.services.user import list_users\n"
    "ModuleNotFoundError: No module named 'app.services.user'\n"
)


def _fake_probe(*, module: str = "github.com/acme/shop", present: set[str] = frozenset()):
    """假造最底层只读探针。`present` = 树里**已存在**的路径词干（其余一律不存在）。

    只认 driver 真正会发的三类命令（读 go.mod / `ls` 存在性 / `grep` 符号声明）；
    其它命令返 (1, "", "") —— 若 driver 改用别的命令形态，这些测试会红而不是静默放过。
    """
    def _run(cmd: str, project_path: str, timeout: int = 60):
        if "go.mod" in cmd and "awk" in cmd:
            return 0, module + "\n", ""
        if cmd.startswith("ls ") or "grep" in cmd:
            # ★路径必须**精确**命中，不能子串匹配（复核 C-1 就是被子串假探针放过的）★
            # 子串匹配下 `ls 'routes/users'.ts…`（错口径）与 `ls 'src/routes/users'.ts…`
            # （对口径）都会返命中 ⇒ 夹具替 present_in_tree 的路径口径背书，
            # `present_in_tree` 用错路径也照旧绿。
            for p in present:
                if any(_tok_matches(p, t) for t in _shell_path_tokens(cmd)):
                    return 0, p + "\n", ""
            return 1, "", ""
        return 1, "", ""
    return _run


def _shell_path_tokens(cmd: str) -> list[str]:
    """抽出命令里被单引号包起来的路径实参（driver 一律经 `_sh_quote` 拼）。"""
    import re as _re
    toks = _re.findall(r"'([^']+)'", cmd)
    # glob 实参不经 _sh_quote（`internal/svc/*.go`），单独捞
    toks += _re.findall(r"(?<![\w'/])([\w./-]*\*[\w.*/-]*)", cmd)
    return [t for t in toks if t and t not in ("-d", "-lE", "-l")]


def _tok_matches(present_stem: str, token: str) -> bool:
    """token（如 `src/routes/users.ts` / `internal/svc/*.go`）是否落在已存在的词干上。"""
    t = token.rstrip("/")
    for suffix in ("/*.go", "/index.ts", "/index.js", "/mod.rs", "/__init__.py"):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
            break
    else:
        if "." in t.rsplit("/", 1)[-1]:
            t = t.rsplit(".", 1)[0]
    return t == present_stem


# ═══════════════════════════════════════════════════════════════
# 1. 解析器（真实语料）
# ═══════════════════════════════════════════════════════════════


def test_go_parses_missing_pkg_and_bare_undefined():
    refs = ed.GoErrorDriver().parse_missing(GO_MISSING_PKG + GO_UNDEFINED_BARE)
    assert ("github.com/acme/shop/internal/svc", None) in [(r.ref, r.symbol) for r in refs]
    # 裸 `undefined: X` 容器未知（留空由 resolve_ref 从报错文件目录反解）
    assert ("", "ListUsers") in [(r.ref, r.symbol) for r in refs]


def test_go_resolve_ref_backfills_container_from_error_file_dir():
    """★裸 undefined 的容器反解★ 报错文件 `internal/handler/user.go` → 容器
    `<module>/internal/handler`。不做的话该形态恒 fail-closed，Go 最常见的
    "引用同包里别人还没写的函数"整类退回 FAIL 修复梯。"""
    drv = ed.GoErrorDriver()
    bare = [r for r in drv.parse_missing(GO_UNDEFINED_BARE) if not r.ref][0]
    out = drv.resolve_ref(bare, "/p", 60, _fake_probe())
    assert out.ref == "github.com/acme/shop/internal/handler"
    assert out.symbol == "ListUsers"


def test_solver_wires_go_bare_undefined_through_resolve_ref():
    """★接线臂（突变逮到的）★ 上一条只调 `resolve_ref` 本体＝证实现正确；把求解器里那句
    `refs = [resolve(r, …)]` 删掉它照旧绿。本条走**求解器**，证反解真的被接在链上：
    裸 `undefined: ListUsers` 必须经反解拿到容器，最终产出符号级 FQN。
    """
    pkgs, syms = ed.blocked_on_unbuilt_internal(
        "go", GO_UNDEFINED_BARE, "/p", 60, _fake_probe())
    assert pkgs == {"github.com/acme/shop/internal/handler"}
    assert syms == ["github.com/acme/shop/internal/handler.ListUsers"]


def test_ts_parses_module_and_export():
    refs = ed.NodeErrorDriver().parse_missing(TS_MISSING_MODULE + TS_MISSING_EXPORT)
    pairs = [(r.ref, r.symbol) for r in refs]
    assert ("./routes/users", None) in pairs
    assert ("./svc", "listUsers") in pairs


def test_rust_primary_form_is_whole_container_not_split():
    """★复核 HIGH-1★ `mod svc;` 未声明时 rustc 报的是 `unresolved import \\`crate::svc\\``
    ——这是**主形态**。按段数 rpartition 会切成容器 `crate` + 符号 `svc`，`is_internal("crate")`
    为假 ⇒ 全或无当场清盘 ⇒ Rust 臂在目标场景下实质零覆盖（原语料用全路径形态，那只在
    svc 已存在时才出现＝已经不是"生产者未就绪"）。判据只认 rustc 自己的尾注。"""
    refs = ed.RustErrorDriver().parse_missing(RUST_UNRESOLVED)
    assert [(r.ref, r.symbol) for r in refs] == [("crate::svc", None)]
    pkgs, _ = ed.blocked_on_unbuilt_internal(
        "rust", RUST_UNRESOLVED, "/p", 60, _fake_probe())
    assert pkgs == {"crate::svc"}, "主形态必须能标出来，否则整个 Rust 臂是装饰"


def test_rust_splits_container_and_leaf_only_with_rustc_note():
    """有尾注 `no \\`X\\` in \\`Y\\`` 才拆容器+符号（模块在、项缺失）。"""
    refs = ed.RustErrorDriver().parse_missing(RUST_UNRESOLVED_WITH_NOTE)
    assert [(r.ref, r.symbol) for r in refs] == [("crate::svc", "list_users")]


def test_dedupe_prefers_evidence_with_source_file():
    """★整改过程中实测撞到的★ tsc 的 TS2307 整行**同时**匹配 TS 式与 require 式正则，
    后者拿不到报错文件。若去重留下 src=None 那条，步骤 4 就解不出相对导入的归属 → 整批落
    UNKNOWN → 刚治好的 CRITICAL-2 通道又被关掉（且表现为"归属解不出"这种无辜文案）。
    """
    # ★夹具必须把 src=None 那条排在**前面**★ 正则遍历顺序恰好让带 src 的先入时，收敛分支
    # 根本不执行——那样的夹具对本机制零区分力（突变实测：仍全绿）。
    refs = [
        ed.MissingRef(ref="./routes/users", symbol=None, src=None),
        ed.MissingRef(ref="./routes/users", symbol=None, src="src/app.ts"),
    ]
    got = [(r.ref, r.src) for r in ed._dedupe(refs)]
    assert got == [("./routes/users", "src/app.ts")], \
        f"同一 ref 必须收敛到带报错文件的那条证据: {got}"
    # 管线级：同一 TS2307 行被 TS 式与 require 式两条正则同时命中，最终仍须带 src
    parsed = ed.NodeErrorDriver().parse_missing(TS_MISSING_MODULE)
    assert [(r.ref, r.src) for r in parsed if r.ref == "./routes/users"] == \
        [("./routes/users", "src/app.ts")]


def test_go_qualified_undefined_resolves_alias():
    """★复核 HIGH-2★ `undefined: svc.GetUser` 里 `svc` 是**包别名**不是 import path。
    原实现直接当容器 → `is_internal("svc")` 恒假 → 全或无把**同批的裸 undefined 一起清盘**
    ⇒ Go 符号通道在跨包调用（Go 的常态写法）下实质零覆盖。治法＝从报错文件的 import 块
    确定性反解，解不出则保持 fail-closed（`svc` 也可能根本不是包）。"""
    imports = '\tsvc "github.com/acme/shop/internal/svc"\n'

    def run(cmd, pp, timeout=60):
        if "go.mod" in cmd and "awk" in cmd:
            return 0, "github.com/acme/shop\n", ""
        if "grep -oE" in cmd:
            return 0, imports, ""
        return 1, "", ""

    pkgs, syms = ed.blocked_on_unbuilt_internal("go", GO_QUALIFIED_UNDEFINED, "/p", 60, run)
    assert "github.com/acme/shop/internal/svc" in pkgs
    assert "github.com/acme/shop/internal/svc.GetUser" in syms

    def run_no_imports(cmd, pp, timeout=60):
        if "go.mod" in cmd and "awk" in cmd:
            return 0, "github.com/acme/shop\n", ""
        return 1, "", ""

    assert ed.blocked_on_unbuilt_internal(
        "go", GO_QUALIFIED_UNDEFINED, "/p", 60, run_no_imports) == (set(), []), \
        "别名解不出 → 全盘不标（fail-closed），绝不臆造容器"


def test_go_bare_package_noise_does_not_disarm_gate():
    """★复核 MED-2★ `main.go:3:8: package main` 这类普通诊断行若被当成缺失包解析出来，
    `is_internal` 会拒它 → 全或无**把该轮 X-C3 整个关掉，且零日志**（上一批三条 CRITICAL
    的同型：过宽 marker 静默解除下游武装）。"""
    noise = ("main.go:3:8: package main\n"
             "internal/handler/user.go:7:2: no required module provides package "
             "github.com/acme/shop/internal/svc\n")
    refs = ed.GoErrorDriver().parse_missing(noise)
    assert [r.ref for r in refs] == ["github.com/acme/shop/internal/svc"], \
        "裸 package 诊断行不得进解析结果"
    pkgs, _ = ed.blocked_on_unbuilt_internal("go", noise, "/p", 60, _fake_probe())
    assert pkgs == {"github.com/acme/shop/internal/svc"}


def test_solver_sees_gopath_third_party():
    """★复核 MED-1★ 全或无只对**解析器认出的** ref 生效——GOPATH 形第三方缺失行原先
    完全看不见 ⇒ 第三方那一票不存在 ⇒ 照标 BLOCKED（武装被静默解除）。"""
    mixed = ('main.go:8:2: cannot find package "github.com/gin-gonic/gin" in any of:\n'
             "internal/handler/user.go:7:2: no required module provides package "
             "github.com/acme/shop/internal/svc\n")
    assert ed.blocked_on_unbuilt_internal(
        "go", mixed, "/p", 60, _fake_probe()) == (set(), [])


def test_solver_sees_bundler_third_party():
    """★复核 MED-1（Node 侧）★ vite/rollup/webpack 形态原先解析器看不见。"""
    mixed = (TS_MISSING_MODULE +
             '[vite]: Rollup failed to resolve import "express" from "src/app.ts".\n')
    assert ed.blocked_on_unbuilt_internal(
        "node", mixed, "/p", 60, _fake_probe()) == (set(), [])


@pytest.mark.parametrize("txt", [
    # 真 cargo 的两种 E0433 文案（都吐**裸**模块名，无 crate:: 前缀）
    "error[E0433]: failed to resolve: could not find `svc` in the crate root\n"
    " --> src/main.rs:4:12\n",
    "error[E0433]: failed to resolve: use of undeclared crate or module `svc`\n"
    " --> src/main.rs:4:5\n",
])
def test_rust_e0433_call_forms_normalize_to_crate_path(txt):
    """★复核 H-2★ 调用形（`svc::f()` / `crate::svc::f()`）报的是 E0433 + **裸** `svc`。
    原实现直接当容器 → `is_internal("svc")` 恒假（只认 `crate::` 前缀）→ 全或无当场清盘，
    连同批的合法 E0432 证据一起清掉（A+B 混用是工程常态）⇒ Rust 臂近零覆盖。"""
    pkgs, _ = ed.blocked_on_unbuilt_internal("rust", txt, "/p", 60, _fake_probe())
    assert pkgs == {"crate::svc"}


def test_rust_mixed_use_and_call_forms_not_cleared():
    """A（use 形 E0432）+ B（调用形 E0433）混用时，B 不得把 A 的证据清盘。"""
    mixed = RUST_UNRESOLVED + (
        "error[E0433]: failed to resolve: could not find `svc` in the crate root\n")
    pkgs, _ = ed.blocked_on_unbuilt_internal("rust", mixed, "/p", 60, _fake_probe())
    assert pkgs == {"crate::svc"}


def test_python_parses_module_not_found():
    refs = ed.PythonErrorDriver().parse_missing(PY_MODULE_NOT_FOUND)
    assert [(r.ref, r.symbol) for r in refs] == [("app.services.user", None)]


def test_rust_symbol_fqn_uses_stack_separator():
    """★分隔符按栈取★ 混用会让 brain 侧类级 futile 判据的前缀匹配失配
    （它按容器名前缀反查生产者子任务）。Rust 必须是 `::` 而不是 `.`。"""
    pkgs, syms = ed.blocked_on_unbuilt_internal(
        "rust", RUST_UNRESOLVED_WITH_NOTE, "/p", 60, _fake_probe())
    assert pkgs == {"crate::svc"}
    assert syms == ["crate::svc::list_users"]


# ═══════════════════════════════════════════════════════════════
# 2. 通用求解器：全或无 + fail-closed
# ═══════════════════════════════════════════════════════════════


def test_solver_marks_internal_not_yet_built():
    pkgs, _syms = ed.blocked_on_unbuilt_internal(
        "go", GO_MISSING_PKG, "/p", 60, _fake_probe())
    assert pkgs == {"github.com/acme/shop/internal/svc"}


def test_solver_all_or_nothing_third_party_present():
    """★有一条第三方缺失 → 全盘不标★ 混合形态标 BLOCKED 会让 worker 去等一个不存在的
    生产者；漏标只是退回 FAIL 修复梯。两种错代价不对称，故刻意全或无。"""
    pkgs, syms = ed.blocked_on_unbuilt_internal(
        "go", GO_MISSING_PKG + GO_THIRD_PARTY, "/p", 60, _fake_probe())
    assert (pkgs, syms) == (set(), []), "混入 gin（第三方）必须整盘不标，交 dep-repair"


def test_solver_all_or_nothing_already_in_tree():
    """已在树里 ⇒ 真编译错 ⇒ 照常 FAIL（不是"生产者未就绪"）。"""
    pkgs, _ = ed.blocked_on_unbuilt_internal(
        "go", GO_MISSING_PKG, "/p", 60,
        _fake_probe(present={"internal/svc"}))
    assert pkgs == set()


def test_solver_unregistered_stack_fail_closed():
    """未收录栈（elixir/dart/php…）与 None → 不标（fail-closed），绝不臆造 BLOCKED。"""
    for key in ("elixir", "dart", "php", "csharp", None, ""):
        assert ed.blocked_on_unbuilt_internal(
            key, GO_MISSING_PKG, "/p", 60, _fake_probe()) == (set(), [])


def test_solver_java_is_self_handled():
    """★JVM 恒返空★ 它走 l1_pipeline 既有专用链（唯一跑过 E2E 的栈，路径逐字节不变）。"""
    javac = ("[ERROR] /p/src/main/java/com/acme/A.java:[7,25] package "
             "com.acme.svc does not exist")
    assert ed.blocked_on_unbuilt_internal(
        "java", javac, "/p", 60, _fake_probe()) == (set(), [])
    assert "java" in ed.ERROR_DRIVERS, "但仍须在 registry 里（单一事实源可枚举）"


# ═══════════════════════════════════════════════════════════════
# 3. F2：步骤 4（"worker 无权等自己"）的非 JVM 半边
# ═══════════════════════════════════════════════════════════════


def test_stem_matches_rejects_sibling_prefix():
    """★不许裸 startswith★ 否则词干 `svc` 会吃掉 `svc_test.go`/`svcutil/` ——
    把别人的文件算成自己的产出 → 误抑 BLOCKED（该等上游却去烧修复轮）。"""
    assert ed._stem_matches("internal/svc/user.go", "internal/svc")
    assert ed._stem_matches("routes/users.ts", "routes/users")
    assert ed._stem_matches("routes/users/index.ts", "routes/users")
    assert not ed._stem_matches("internal/svcutil/x.go", "internal/svc")
    assert not ed._stem_matches("routes/users_admin.ts", "routes/users")


@pytest.mark.parametrize("lang,ref,src,own_file,other_file", [
    ("go", "github.com/acme/shop/internal/svc", None,
     "internal/svc/user.go", "internal/other/x.go"),
    # ★复核 CRITICAL-2★ `./routes/users` 从 `src/app.ts` 引用 → 真实解析到
    # `src/routes/users.ts`（相对**报错文件**，不是工程根）。原夹具写 `routes/users.ts`
    # ＝夹具把 bug 当成布局前提，替 `ref_tree_paths` 背书。
    ("node", "./routes/users", "src/app.ts",
     "src/routes/users.ts", "src/routes/admin.ts"),
    # `../` 形态：从 `src/api/app.ts` 看 `../svc/user` → `src/svc/user`
    ("node", "../svc/user", "src/api/app.ts",
     "src/svc/user.ts", "src/api/svc/user.ts"),
    ("rust", "crate::svc", None, "src/svc/mod.rs", "src/other.rs"),
    ("python", "app.services.user", None, "app/services/user.py", "app/other.py"),
])
def test_produced_in_scope_detects_own_container(lang, ref, src, own_file, other_file):
    """scope 里有该容器的源码 ⇒ 生产者是自己 ⇒ 不该判 BLOCKED（否则等自己=幽灵生产者）。"""
    mr = ed.MissingRef(ref=ref, symbol=None, src=src)
    own, unres = ed.produced_in_scope(lang, [mr], [own_file], "/p", 60, _fake_probe())
    assert own == {ref} and not unres
    own2, unres2 = ed.produced_in_scope(lang, [mr], [other_file], "/p", 60, _fake_probe())
    assert own2 == set() and not unres2, "别的文件不得算自产，且必须是【确定】不自产"


def test_present_in_tree_uses_same_path_convention_as_ref_tree_paths(tmp_path):
    """★复核 C-1★ 步骤 3（`present_in_tree`）与步骤 4（`ref_tree_paths`）必须同口径。

    原先各算一遍：CRITICAL-2 只修了步骤 4，步骤 3 仍按**工程根**解 `./x` ⇒ TS 的 `src/`
    布局（业界常态）下「已在树里 → 全盘不标」这道保险丝**恒不触发** ⇒ 真编译错
    （tsconfig paths 配错/缺 .d.ts/循环依赖）被判成"生产者未就绪"BLOCKED = 最贵的那一侧。

    ★用真 shell 探针 + 真工程树★ —— 假探针的子串匹配正是 C-1 的藏身处。
    """
    import subprocess

    def real_run(cmd, project_path, timeout=60):
        p = subprocess.run(cmd, cwd=project_path, shell=True,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr

    (tmp_path / "src" / "routes").mkdir(parents=True)
    (tmp_path / "src" / "app.ts").write_text("import {x} from './routes/users';\n")
    (tmp_path / "src" / "routes" / "users.ts").write_text("export const x = 1;\n")
    drv = ed.NodeErrorDriver()
    assert drv.ref_tree_paths("./routes/users", "src/app.ts", str(tmp_path), 20,
                              real_run) == ["src/routes/users"]
    assert drv.present_in_tree("./routes/users", None, "src/app.ts", str(tmp_path),
                               20, real_run) is True, \
        "模块就在 src/routes/users.ts —— 判不出=保险丝失效"
    # 端到端：已在树里 ⇒ 求解器必须全盘不标（真编译错，交修复梯）
    pkgs, _ = ed.blocked_on_unbuilt_internal(
        "node", TS_MISSING_MODULE, str(tmp_path), 20, real_run)
    assert pkgs == set(), "已在树里的模块被标 BLOCKED = 去等一个永不到来的生产者"


def test_disarm_reason_is_machine_readable(tmp_path):
    """★复核 H-1★ "全或无"解除武装必须留机读原因——五种返空成因对调用方同形时，
    "机制一次都没触发"与"本轮真没有内部缺失"不可分（解析器漏形态的唯一症状就是返空）。"""
    cases = [
        ("elixir", GO_MISSING_PKG, "unregistered_stack"),
        ("java", "[ERROR] package com.acme.x does not exist", "self_handled"),
        ("go", "some unrelated build noise\n", "no_refs_parsed"),
        ("go", GO_MISSING_PKG + GO_THIRD_PARTY, "third_party"),
    ]
    for lang, out, expect in cases:
        d: dict = {}
        ed.blocked_on_unbuilt_internal(lang, out, "/p", 20, _fake_probe(), disarm_out=d)
        assert d.get("reason") == expect, f"{lang}/{expect}: 实得 {d}"


def test_produced_in_scope_fail_closed_for_unknown_and_java():
    for key in ("java", "elixir", None):
        assert ed.produced_in_scope(
            key, {"x"}, ["a/b.go"], "/p", 60, _fake_probe()) == (set(), set())


def test_produced_in_scope_treats_empty_stems_as_unresolved():
    """★复核 H-4★ `[]` 与 `None` 都必须记为"归属未知"。Protocol 曾教人用 `[]` 表"解不出"
    并自称 fail-closed，而 `[]` 静默 fall-through 成"确定不自产"→ 推向 BLOCKED（去等自己）。
    当前 4 个 driver 都不返 `[]`，但留着就是 CRITICAL-2 的复发种子，故按契约锁死。"""
    class _EmptyStemDriver:
        key = "go"
        symbol_sep = "."

        def parse_missing(self, out):
            return []

        def is_internal(self, ref, pp, t, run):
            return True

        def ref_tree_paths(self, ref, src, pp, t, run):
            return []          # ← 契约上的"确定无词干"，绝不能被读成"确定不自产"

        def present_in_tree(self, ref, sym, src, pp, t, run):
            return False

    import unittest.mock as _mock
    with _mock.patch.dict(ed.ERROR_DRIVERS, {"go": _EmptyStemDriver()}):
        own, unres = ed.produced_in_scope(
            "go", [ed.MissingRef(ref="x/y", symbol=None, src="x/y/z.go")],
            ["x/y/z.go"], "/p", 60, _fake_probe())
    assert own == set() and unres == {"x/y"}, \
        "空词干必须落 unresolved（裁决层会据此落 FAIL），不能塌成『确定不自产』"


def test_symbol_probe_does_not_cross_package_boundary(tmp_path):
    """★复核 M-3★ 符号级探测原用 `grep -r … .`（根包＝递归整树），而 Go 的子目录是**不同的
    包**，短名（New/Run/Handler/buildRouter）跨包重名是常态 ⇒ 别包的同名符号会被当成"已建出"
    → 漏标（且无痕）。现在只搜容器自己的文件。"""
    import subprocess

    def real_run(cmd, project_path, timeout=60):
        p = subprocess.run(cmd, cwd=project_path, shell=True,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr

    (tmp_path / "go.mod").write_text("module github.com/acme/shop\n\ngo 1.22\n")
    (tmp_path / "main.go").write_text("package main\n\nfunc main() {}\n")
    (tmp_path / "internal" / "unrelated").mkdir(parents=True)
    (tmp_path / "internal" / "unrelated" / "r.go").write_text(
        "package unrelated\n\nfunc buildRouter() {}\n")   # ★别的包里有同名 func★
    drv = ed.GoErrorDriver()
    assert drv.present_in_tree(
        "github.com/acme/shop", "buildRouter", "main.go", str(tmp_path), 20,
        real_run) is False, "别包的同名符号不得算作『根包里已建出』"


def test_step4_driver_exception_falls_to_fail_not_blocked():
    """★复核 H-3★ 步骤 4 的 driver 半边抛异常时原先 fail-**open**（own 集空 → 判 BLOCKED），
    日志还写"维持 BLOCKED 语义"——与 CRITICAL-2 的结论（归属解不出 → 不敢断言外部生产者）
    方向相反。异常吞在 except 里 ⇒ unresolved_out 永远收不到 ⇒ 外层判不到 ⇒ 静默。"""
    import swarm.worker.l1_error_drivers as _ed_mod

    def _boom(*a, **k):
        raise RuntimeError("driver 半边炸了")

    _orig = _ed_mod.produced_in_scope
    _ed_mod.produced_in_scope = _boom
    try:
        details: dict = {}
        blocked = lp.decide_unbuilt_internal_verdict(
            details, FileScope(writable=["internal/svc/user.go"]),
            {"github.com/acme/shop/internal/svc"}, [],
            cmd="go build ./...", stage="build", output="",
            language_key="go", project_path="/p", timeout=60, run=_fake_probe())
    finally:
        _ed_mod.produced_in_scope = _orig
    assert blocked is False, "归属探测炸了却判 BLOCKED = 可能让 worker 去等自己"
    assert details.get("pipeline_blocked") is None
    assert details.get("blocked_owner_unresolved"), "降级必须留机读账"


def test_produced_in_scope_reports_unresolved_separately():
    """★复核 CRITICAL-2 的三态★ "解不出"必须与"确定不自产"分账——塌成同一个空集时，
    上层会把它当"生产者在外部"→ 判 BLOCKED → worker 去等自己。

    node 的相对导入缺 `src` 上下文（bundler/require 形态没有报错文件）即解不出。
    """
    mr = ed.MissingRef(ref="./routes/users", symbol=None, src=None)
    own, unres = ed.produced_in_scope(
        "node", [mr], ["src/routes/users.ts"], "/p", 60, _fake_probe())
    assert own == set() and unres == {"./routes/users"}


def test_go_root_package_stem_is_root_only():
    """★复核 CRITICAL-2 变体★ Go 根包（`main.go` 里的裸 undefined）原返 [] 自称
    fail-closed，实际被读成"非自产"→ 去等自己。现在返根词干，且**只**认工程根直下文件。"""
    mr = ed.MissingRef(ref="github.com/acme/shop", symbol="buildRouter", src="main.go")
    own, unres = ed.produced_in_scope("go", [mr], ["main.go"], "/p", 60, _fake_probe())
    assert own == {"github.com/acme/shop"} and not unres
    # 子目录文件绝不算根包自产（否则整棵树都算）
    own2, _ = ed.produced_in_scope(
        "go", [mr], ["internal/svc/user.go"], "/p", 60, _fake_probe())
    assert own2 == set()


def test_norm_rel_preserves_dotfile_dirs():
    """★复核 LOW-3★ `lstrip("./")` 会把 `.github/x` 剥成 `github/x`、`.mvn/wrapper` 剥成
    `mvn/wrapper`——本仓已被这个惯用法坑过（`.mvn/wrapper`/`.yarn/releases` 被剔没）。"""
    assert ed._norm_rel("./a/b.ts") == "a/b.ts"
    assert ed._norm_rel(".github/workflows/ci.yml") == ".github/workflows/ci.yml"
    assert ed._norm_rel(".mvn/wrapper/maven-wrapper.properties") == \
        ".mvn/wrapper/maven-wrapper.properties"


def test_step4_shared_layer_consults_driver_half():
    """★F2 的核心断言★ 共用层 `_missing_internal_produced_in_scope` 经 `classpath_fqn_key`
    实现＝**JVM-only**，对 Go 布局恒返 None ⇒ own 集恒空 ⇒ "worker 无权等自己"这道闸对
    非 JVM 栈静默失效（fail-open：自己漏建 → 去等永不到来的生产者，烧满退避阶梯）。
    X-C3 之前非 JVM 到不了裁决点、缺口潜伏无害；X-C3 把它激活了，故必须补 driver 半边。

    突变判据：把 `_missing_internal_produced_in_scope` 里的 driver 半边整块删掉 → 本条红。
    """
    scope = FileScope(writable=["internal/svc/user.go"])
    ref = "github.com/acme/shop/internal/svc"
    # 不传 driver 参数（老调用方形态）→ 只有 JVM 通道 → 对 Go 布局解不出（这正是缺口）
    assert lp._missing_internal_produced_in_scope(scope, {ref}, []) == (set(), set())
    # 传 driver 参数 → 认出自产
    own_p, _own_c = lp._missing_internal_produced_in_scope(
        scope, {ref}, [], language_key="go", project_path="/p",
        timeout=60, run=_fake_probe())
    assert own_p == {ref}, "非 JVM 栈的 scope 自产必须被认出，否则 worker 去等自己"


def test_step4_symbol_inherits_container_ownership():
    """容器自产 ⇒ 其下符号也归本子任务（容器是它建的，符号缺就是它漏建）。"""
    scope = FileScope(writable=["internal/svc/user.go"])
    ref = "github.com/acme/shop/internal/svc"
    _own_p, own_c = lp._missing_internal_produced_in_scope(
        scope, {ref}, [f"{ref}.ListUsers"], language_key="go",
        project_path="/p", timeout=60, run=_fake_probe())
    assert own_c == {f"{ref}.ListUsers"}


# ═══════════════════════════════════════════════════════════════
# 4. ★接线证明★ —— 走 run_l1_pipeline 本体
# ═══════════════════════════════════════════════════════════════

_TS_DIFF = ("--- a/src/app.ts\n+++ b/src/app.ts\n@@ -1 +1 @@\n-old\n+new\n")


@pytest.fixture()
def ts_project(tmp_path):
    """真 Node/TS 工程（`_compile_files` 的 tsc 分支要求 package.json 在场）。"""
    (tmp_path / "package.json").write_text('{"name": "shop", "version": "1.0.0"}')
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions": {"strict": true}}')
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.ts").write_text("import { listUsers } from './routes/users';\n")
    return tmp_path


@pytest.fixture()
def quiet_gates(monkeypatch):
    monkeypatch.setenv("SWARM_WORKER_L1_FORMAT", "false")
    monkeypatch.setenv("SWARM_WORKER_L1_LINT", "false")


def _run_ts(project, monkeypatch, scope, tsc_out, present=frozenset()):
    """只假造最底层探针：真实 `_compile_files`（含 tsc 分支 + raw_out）与真实 driver 全跑。"""
    probe = _fake_probe(present=set(present))

    def _split(cmd, project_path, timeout=60):
        if "tsc" in cmd:
            return 2, tsc_out, ""
        return probe(cmd, project_path, timeout)

    monkeypatch.setattr(lp, "_run_check_split", _split)
    st = SubTask(id="st-xc3-ts", description="X-C3 ts", scope=scope,
                 difficulty=SubTaskDifficulty.MEDIUM,
                 harness=TaskHarness(language="typescript"))
    return lp.run_l1_pipeline(str(project), st, _TS_DIFF, timeout=60,
                              project_stack={"backend": "Express (node)"})


def test_wiring_ts_compile_gate_reaches_blocked(ts_project, monkeypatch, quiet_gates):
    """★F1 的核心断言★ node/ts 的 driver 若只接在 L1.2.1 build 闸上就是**死代码**：
    `_compile_files` 对 .ts 跑 `tsc --noEmit`，TS2307 → rc≠0 且非 infra → L1.2 当场
    hard-fail 早返 ⇒ build 闸永不执行。故必须在 L1.2 也接一个调用点。

    突变判据：把 L1.2 那段 X-C3 归因整块删掉 → 本条红（ok 变 False、无 pipeline_blocked）。
    """
    scope = FileScope(writable=["src/app.ts"])   # routes/users 不在 scope = 真等上游
    ok, details = _run_ts(ts_project, monkeypatch, scope, TS_MISSING_MODULE)
    assert ok is True, f"BLOCKED 契约=ok=True + pipeline_blocked: {details}"
    assert details.get("pipeline_blocked") == "internal_pkg_not_built"
    assert details.get("blocked_on_packages") == ["./routes/users"]
    assert details.get("blocked_via_error_driver") == "node"
    # ★ok 键必须是 None 而不是 False★ l1_verdict._det_fail_source 把 False 读成
    # capability「编译失败」→ BLOCKED 会被归成能力失败去换模型（正是本机制要防的）
    assert details.get("l1_2_compile_ok") is None


def test_wiring_ts_blocked_not_read_as_capability_failure(
        ts_project, monkeypatch, quiet_gates):
    """接上真实消费者 `l1_verdict`：BLOCKED 不得被归成 compile capability 失败。"""
    from swarm.worker.l1_verdict import _det_fail_source
    scope = FileScope(writable=["src/app.ts"])
    _ok, details = _run_ts(ts_project, monkeypatch, scope, TS_MISSING_MODULE)
    source, _reason = _det_fail_source(details)
    assert source != "compile", (
        "BLOCKED 被读成 compile capability 失败 → 会去换模型而非等生产者")


def test_wiring_ts_own_scope_producer_falls_to_fail(
        ts_project, monkeypatch, quiet_gates):
    """★F2 在管线上的验证★ 缺的模块本就该由自己建（在 create_files 里）→ 落 FAIL 修复梯。
    没有 driver 半边时这里会误判 BLOCKED = 去等自己（#10 幽灵生产者）。"""
    # ★路径必须是真实解析结果★ `./routes/users` 从 `src/app.ts` 引用 → `src/routes/users.ts`。
    # 写 `routes/users.ts`（工程根相对）就是复核 CRITICAL-2 逮到的夹具自我背书。
    scope = FileScope(writable=["src/app.ts"], create_files=["src/routes/users.ts"])
    ok, details = _run_ts(ts_project, monkeypatch, scope, TS_MISSING_MODULE)
    assert ok is False, f"自己该建的 → FAIL 进修复梯，不判 BLOCKED: {details}"
    assert details.get("pipeline_blocked") is None
    assert details.get("in_scope_producer_fail") == ["./routes/users"]
    # M-4：裁决翻转成 FAIL ⇒ BLOCKED 侧的键不得粘滞（本仓在别处维持"存在⟺判死"不变量）
    assert "blocked_via_error_driver" not in details, \
        "判 FAIL 却留着 blocked_via_error_driver = 陈键，下游读它会误判"


def test_wiring_ts_unresolved_owner_falls_to_fail(
        ts_project, monkeypatch, quiet_gates):
    """★复核 CRITICAL-2 的管线级验证★ 归属解不出时不得判 BLOCKED。

    用 `require` 形态（`Cannot find module './routes/users'`，**无报错文件**）→ 相对导入
    无锚点 → UNKNOWN。此时判 BLOCKED 就可能让 worker 去等自己（#10 幽灵生产者）；
    落 FAIL 修复梯只是退回现状。两侧代价不对称。
    """
    scope = FileScope(writable=["src/app.ts"])
    node_require = "Error: Cannot find module './routes/users'\n"
    ok, details = _run_ts(ts_project, monkeypatch, scope, node_require)
    assert ok is False, f"归属未知 → FAIL，不判 BLOCKED: {details}"
    assert details.get("pipeline_blocked") is None
    assert details.get("blocked_owner_unresolved") == ["./routes/users"], \
        "降级必须留机读账（否则这一层可以死很久没人知道）"


def test_step4_symbol_inheritance_rejects_sibling_container():
    """★复核 LOW-2★ 符号继承容器归属时用裸 startswith 会让容器 `…/svc` 吞掉
    `…/svcutil.Foo` —— 正是 `_stem_matches` 存在的理由所禁止的形态。"""
    scope = FileScope(writable=["internal/svc/user.go"])
    ref = "github.com/acme/shop/internal/svc"
    sibling = "github.com/acme/shop/internal/svcutil.Foo"
    _own_p, own_c = lp._missing_internal_produced_in_scope(
        scope, {ref}, [f"{ref}.ListUsers", sibling], language_key="go",
        project_path="/p", timeout=60, run=_fake_probe())
    assert f"{ref}.ListUsers" in own_c
    assert sibling not in own_c, "sibling 容器的符号不得被算成自产"


def test_wiring_ts_third_party_stays_plain_compile_fail(
        ts_project, monkeypatch, quiet_gates):
    """回归臂：第三方缺失（裸包名）不得被 X-C3 吞成 BLOCKED，照常 compile FAIL。"""
    scope = FileScope(writable=["src/app.ts"])
    third = ("src/app.ts(1,20): error TS2307: Cannot find module 'express' or its "
             "corresponding type declarations.\n")
    ok, details = _run_ts(ts_project, monkeypatch, scope, third)
    assert ok is False
    assert details.get("pipeline_blocked") is None
    assert details.get("l1_2_compile_ok") is False


def test_wiring_ts_already_in_tree_stays_compile_fail(
        ts_project, monkeypatch, quiet_gates):
    """回归臂：模块已在树里 ⇒ 真编译错 ⇒ 照常 FAIL（不是"生产者未就绪"）。"""
    scope = FileScope(writable=["src/app.ts"])
    # ★路径口径（复核 C-1）★ `./routes/users` 从 `src/app.ts` 引用 → 树里的真实位置是
    # `src/routes/users`。原来写 `routes/users` 也能绿，只因假探针是**子串**匹配 —— 那让
    # `present_in_tree` 用错口径（工程根相对）照旧过，正是 C-1 藏身之处。
    ok, details = _run_ts(ts_project, monkeypatch, scope, TS_MISSING_MODULE,
                          present={"src/routes/users"})
    assert ok is False
    assert details.get("pipeline_blocked") is None
    assert (details.get("xc3_disarm") or {}).get("reason") == "already_in_tree", \
        "武装被解除必须留机读账（H-1：返空不留痕 ⇒ 解析器漏形态时无信号）"


@pytest.mark.parametrize("stack,manifest,mf_body,src_rel,src_body,build_out,expect_pkg", [
    ("Gin (go)", "go.mod", "module github.com/acme/shop\n\ngo 1.22\n",
     "internal/handler/user.go", "package handler\n",
     "internal/handler/user.go:7:2: no required module provides package "
     "github.com/acme/shop/internal/svc\n",
     "github.com/acme/shop/internal/svc"),
    ("Axum (rust)", "Cargo.toml", "[package]\nname = \"shop\"\nversion = \"0.1.0\"\n",
     "src/handler.rs", "// handler\n",
     "error[E0432]: unresolved import `crate::svc`\n --> src/handler.rs:2:5\n"
     "  |     ^^^^^^^^^^ use of undeclared crate or module `svc`\n",
     "crate::svc"),
])
def test_wiring_go_rust_build_gate_reaches_blocked(
        tmp_path, monkeypatch, quiet_gates, stack, manifest, mf_body,
        src_rel, src_body, build_out, expect_pkg):
    """★复核 LOW-1★ go/rust 走的是 **L1.2.1 build 闸**（另一个调用点），此前"可达"只有
    散文声称、零测试钉住——而本批**正是因为可达性误判**（node/ts 死代码）才返工的。

    走真实 `run_l1_pipeline`：真实 `_derive_full_build_command`（据清单+改动文件派生
    `go build ./...` / `cargo build -q`）、真实 `_build_cmd_applicable`、真实 driver。
    只假造 `_run_l1_command`（构建执行）与 `_run_check_split`（只读探针）。
    """
    (tmp_path / manifest).write_text(mf_body)
    p = tmp_path / src_rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src_body)
    monkeypatch.setattr(lp, "_run_l1_command", lambda cmd, pp, timeout=120: (1, build_out))
    monkeypatch.setattr(lp, "_attempt_build_repair", lambda *a, **k: (0, []))
    monkeypatch.setattr(lp, "_build_error_is_upstream", lambda *a, **k: False)
    monkeypatch.setattr(lp, "_scan_fullwidth_punct", lambda *a, **k: [])
    import swarm.worker.workspace_manifest as wm
    monkeypatch.setattr(wm, "reconcile_workspace_manifests",
                        lambda *a, **k: {"modified_manifests": [], "added": []})

    def _split(cmd, project_path, timeout=60):
        if "go.mod" in cmd and "awk" in cmd:
            return 0, "github.com/acme/shop\n", ""
        return 1, "", ""

    monkeypatch.setattr(lp, "_run_check_split", _split)
    diff = f"--- a/{src_rel}\n+++ b/{src_rel}\n@@ -1 +1 @@\n-old\n+new\n"
    st = SubTask(id="st-xc3-br", description="X-C3 build gate",
                 difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=[src_rel]),
                 harness=TaskHarness(language=stack.split("(")[-1].rstrip(")")))
    ok, details = lp.run_l1_pipeline(str(tmp_path), st, diff, timeout=60,
                                     project_stack={"backend": stack})
    assert details.get("build_command_derived"), \
        f"派生不出构建命令 ⇒ build 闸整块跳过 ⇒ 该栈根本到不了 X-C3: {details}"
    assert ok is True, f"BLOCKED 契约=ok=True + pipeline_blocked: {details}"
    assert details.get("pipeline_blocked") == "internal_pkg_not_built"
    assert details.get("blocked_on_packages") == [expect_pkg]
    assert details.get("l1_2_1_build_ok") is None


def test_wiring_python_test_gate_reaches_blocked(tmp_path, monkeypatch, quiet_gates):
    """★复核 C-2★ python 的 `ModuleNotFoundError` **只**在真 import 时出现：`py_compile`
    与 `compileall`（brain 给 python 的默认 build_command）都只做语法检查、缺 import 恒 rc=0
    （实测）。所以 PythonErrorDriver 在 compile/build 两个调用点都不可达＝**死代码**，而
    registry 把 python 报成"已覆盖" —— 与本批已赔过一整批的 node/ts 死代码完全同型。
    治法＝在 L1.3 test 闸接第三个调用点（那是该形态唯一的产出地）。

    突变判据：把 L1.3 那段 X-C3 归因整块删掉 → 本条红。
    """
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "main.py").write_text(
        "from app.services.user import list_users\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='shop'\n")
    monkeypatch.setattr(lp, "_compile_files", lambda *a, **k: (True, "compile ok"))
    monkeypatch.setattr(lp, "_run_l1_command",
                        lambda cmd, pp, timeout=120: (1, PY_MODULE_NOT_FOUND))
    # `app` 是工程内顶层包（`is_internal` 据此判自有）；`app/services/user` **未**建出
    monkeypatch.setattr(lp, "_run_check_split", _fake_probe(present={"app"}))
    monkeypatch.setattr(lp, "_build_cmd_applicable", lambda *a, **k: True)
    diff = ("--- a/app/main.py\n+++ b/app/main.py\n@@ -1 +1 @@\n-old\n+new\n")
    st = SubTask(id="st-xc3-py", description="X-C3 python test gate",
                 difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["app/main.py"]),
                 harness=TaskHarness(language="python", test_command="pytest -q"))
    ok, details = lp.run_l1_pipeline(str(tmp_path), st, diff, timeout=60,
                                     project_stack={"backend": "FastAPI (python)"})
    assert ok is True, f"BLOCKED 契约=ok=True + pipeline_blocked: {details}"
    assert details.get("pipeline_blocked") == "internal_pkg_not_built"
    assert details.get("blocked_on_packages") == ["app.services.user"]
    assert details.get("blocked_via_error_driver") == "python"
    assert details.get("l1_3_test_ok") is None, \
        "BLOCKED 必须写 None——False 会被 l1_verdict 读成 capability 失败去换模型"


def test_python_build_gates_cannot_see_import_errors(tmp_path):
    """C-2 的前提事实（别让下一个人以为 compile/build 闸能接住 python）：
    `py_compile`/`compileall` 对缺 import 恒 rc=0，输出里没有 ModuleNotFoundError。"""
    import subprocess
    import sys as _sys
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "main.py").write_text("from app.nope.missing import x\n")
    for cmd in ([_sys.executable, "-m", "compileall", "-q", "."],
                [_sys.executable, "-m", "py_compile", "app/main.py"]):
        p = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True)
        combined = (p.stdout or "") + (p.stderr or "")
        assert p.returncode == 0, f"{cmd}: 竟然非零退出（前提变了，C-2 结论要重估）"
        assert "ModuleNotFoundError" not in combined


def test_wiring_compile_classifier_eats_untruncated_output(
        ts_project, monkeypatch, quiet_gates):
    """★raw_out 分账的理由★ 人读 `compile_message` 截到 1000 字符；若分类器也吃截断文本，
    截断正好切掉那条**第三方**缺失行时"全或无"就 fail-open 误标 BLOCKED。
    造 1000+ 字符噪声把第三方行挤到截断线之后。

    突变判据：把 `raw_out` 那行删掉（分类器改吃 compile_msg）→ 本条红。
    """
    noise = "".join(
        f"src/n{i}.ts(1,1): error TS2564: Property 'p{i}' has no initializer.\n"
        for i in range(40))
    tsc_out = TS_MISSING_MODULE + noise + (
        "src/app.ts(2,20): error TS2307: Cannot find module 'express' or its "
        "corresponding type declarations.\n")
    assert len(tsc_out) > 1000, "夹具必须真超过截断线，否则本条测不到东西"
    scope = FileScope(writable=["src/app.ts"])
    ok, details = _run_ts(ts_project, monkeypatch, scope, tsc_out)
    assert ok is False, "全文里有第三方缺失（express）→ 全盘不标 BLOCKED"
    assert details.get("pipeline_blocked") is None
