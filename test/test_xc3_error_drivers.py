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
RUST_UNRESOLVED = (
    "error[E0432]: unresolved import `crate::svc::list_users`\n"
    " --> src/handler.rs:2:5\n  |\n2 | use crate::svc::list_users;\n"
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
        if cmd.startswith("ls ") or cmd.startswith("ls -d "):
            for p in present:
                if p in cmd:
                    return 0, p + "\n", ""
            return 1, "", ""
        if "grep" in cmd:
            for p in present:
                if p in cmd:
                    return 0, p + "\n", ""
            return 1, "", ""
        return 1, "", ""
    return _run


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


def test_rust_splits_container_and_leaf():
    refs = ed.RustErrorDriver().parse_missing(RUST_UNRESOLVED)
    assert [(r.ref, r.symbol) for r in refs] == [("crate::svc", "list_users")]


def test_python_parses_module_not_found():
    refs = ed.PythonErrorDriver().parse_missing(PY_MODULE_NOT_FOUND)
    assert [(r.ref, r.symbol) for r in refs] == [("app.services.user", None)]


def test_rust_symbol_fqn_uses_stack_separator():
    """★分隔符按栈取★ 混用会让 brain 侧类级 futile 判据的前缀匹配失配
    （它按容器名前缀反查生产者子任务）。Rust 必须是 `::` 而不是 `.`。"""
    pkgs, syms = ed.blocked_on_unbuilt_internal(
        "rust", RUST_UNRESOLVED, "/p", 60, _fake_probe())
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


@pytest.mark.parametrize("lang,ref,own_file,other_file", [
    ("go", "github.com/acme/shop/internal/svc",
     "internal/svc/user.go", "internal/other/x.go"),
    ("node", "./routes/users", "routes/users.ts", "routes/admin.ts"),
    ("rust", "crate::svc", "src/svc/mod.rs", "src/other.rs"),
    ("python", "app.services.user", "app/services/user.py", "app/other.py"),
])
def test_produced_in_scope_detects_own_container(lang, ref, own_file, other_file):
    """scope 里有该容器的源码 ⇒ 生产者是自己 ⇒ 不该判 BLOCKED（否则等自己=幽灵生产者）。"""
    assert ed.produced_in_scope(lang, {ref}, [own_file], "/p", 60, _fake_probe()) == {ref}
    assert ed.produced_in_scope(lang, {ref}, [other_file], "/p", 60, _fake_probe()) == set()


def test_produced_in_scope_fail_closed_for_unknown_and_java():
    for key in ("java", "elixir", None):
        assert ed.produced_in_scope(
            key, {"x"}, ["a/b.go"], "/p", 60, _fake_probe()) == set()


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
    scope = FileScope(writable=["src/app.ts"], create_files=["routes/users.ts"])
    ok, details = _run_ts(ts_project, monkeypatch, scope, TS_MISSING_MODULE)
    assert ok is False, f"自己该建的 → FAIL 进修复梯，不判 BLOCKED: {details}"
    assert details.get("pipeline_blocked") is None
    assert details.get("in_scope_producer_fail") == ["./routes/users"]


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
    ok, details = _run_ts(ts_project, monkeypatch, scope, TS_MISSING_MODULE,
                          present={"routes/users"})
    assert ok is False
    assert details.get("pipeline_blocked") is None


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
