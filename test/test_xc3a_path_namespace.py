#!/usr/bin/env python3
"""X-C3-A（27 号文 §7.13，治法 B）：`blocked_on_packages` 的 brain 侧消费者口径分栈。

## 治的是什么

X-C3 让非 JVM 栈也能判 `internal_pkg_not_built` BLOCKED，但 `blocked_on_packages` 的三个
brain 侧消费者**内部一律是「先把 Java 点分 FQN 转成路径再比」**：

  · `recovery._producers_of`         `"/".join(p.split("."))`
  · `recovery._package_in_baseline`  `pkg.replace(".", "/")`
  · `failure._derive_missing_type_files`  只认 `/src/main/java|kotlin/` 标记

非 JVM 的 ref 按那个口径转出来**无一命中**（`github.com/a/s/internal/svc` →
`github/com/…`、`./routes/users` 原样、`crate::svc` 原样、`app.services.user` 只当目录段比
而 `user` 其实是**文件**）⇒ `_prods=∅` → `_futile=True` → 推不出该建啥 → `_unrecoverable`
⇒ **首轮连坐放弃**。即"烧修复轮→abandon"变成"**零修复轮→abandon**"，**比 X-C3 之前更坏**。

## 治法 B（用户拍板）

worker 侧一并吐**路径口径**（`blocked_on_paths` / `blocked_on_paths_by_ref`，来源＝driver 的
`ref_tree_paths`，不引入第三套路径规则）；三个消费者**优先读它、缺席回落原路**。
`ref_path_stems` 对 java 恒返空 ⇒ 键缺席 ⇒ **JVM 侧逐字节走老路**（唯一跑过 E2E 的栈零风险）。

## 本文件的断言

每个消费者都断**两条**：非 JVM 用新口径能解开（治前不能）· JVM 在键缺席时行为不变（零回归）。
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import swarm.worker.l1_error_drivers as ed  # noqa: E402
import swarm.worker.l1_pipeline as lp  # noqa: E402
from swarm.brain.nodes.failure import _derive_missing_type_files  # noqa: E402
from swarm.brain.nodes.recovery import (_blocked_pkg_unrecoverable,  # noqa: E402
                                        _package_in_baseline, _producers_of)
from swarm.types import (FileScope, SubTask, SubTaskDifficulty)  # noqa: E402

# (栈键, ref, 报错文件, 生产者文件, 期望词干, 自愈该建的文件)
CASES = [
    ("go", "github.com/acme/shop/internal/svc", "internal/handler/u.go",
     "internal/svc/user.go", "internal/svc", "internal/svc/svc.go"),
    ("node", "./routes/users", "src/app.ts",
     "src/routes/users.ts", "src/routes/users", "src/routes/users.ts"),
    ("rust", "crate::svc", None,
     "src/svc/mod.rs", "src/svc", "src/svc.rs"),
    ("python", "app.services.user", None,
     "app/services/user.py", "app/services/user", "app/services/user.py"),
]
_IDS = [c[0] for c in CASES]

JVM_REF = "com.acme.svc"
JVM_PROD = "mod/src/main/java/com/acme/svc/Svc.java"


def _probe(module: str = "github.com/acme/shop"):
    def _run(cmd, pp, timeout=60):
        if "go.mod" in cmd and "awk" in cmd:
            return 0, module + "\n", ""
        return 1, "", ""
    return _run


def _stems(lang: str, ref: str, src: str | None) -> list[str]:
    mr = ed.MissingRef(ref=ref, symbol=None, src=src)
    return ed.ref_path_stems(lang, [mr], "/p", 20, _probe()).get(ref, [])


def _plan(files: list[str]):
    st = SubTask(id="st-producer", description="p",
                 difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=files))

    class _P:
        subtasks = [st]
    return _P()


@pytest.fixture()
def tree(tmp_path):
    """真工程树：四栈的生产者文件 + JVM 对照臂都物化在磁盘上。"""
    for _lang, _ref, _src, prod, _stem, _new in CASES:
        (tmp_path / prod).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / prod).write_text("x\n")
    (tmp_path / JVM_PROD).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / JVM_PROD).write_text("x\n")
    return tmp_path


# ═══════════════════════════════════════════════════════════════
# worker 侧：路径口径的产出（java 必须缺席）
# ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("lang,ref,src,prod,stem,new", CASES, ids=_IDS)
def test_worker_emits_path_stems(lang, ref, src, prod, stem, new):
    assert stem in _stems(lang, ref, src)


def test_worker_emits_nothing_for_jvm():
    """★JVM 零风险的根据★ 键缺席 ⇒ 三个消费者全部回落老路，行为逐字节不变。"""
    assert ed.ref_path_stems("java", [ed.MissingRef(ref=JVM_REF)], "/p", 20,
                             _probe()) == {}


# ═══════════════════════════════════════════════════════════════
# 消费者 1：_producers_of（反查生产者子任务）
# ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("lang,ref,src,prod,stem,new", CASES, ids=_IDS)
def test_producers_of_resolves_with_paths(lang, ref, src, prod, stem, new):
    """治前：老口径对四栈全空 ⇒ `_prods=∅` ⇒ `_futile=True` ⇒ 首轮连坐放弃。"""
    plan = _plan([prod])
    assert _producers_of(plan, [ref], []) == set(), \
        "夹具前提变了：老口径竟然反查到了，本条命题需重估"
    assert _producers_of(plan, [ref], [], paths=_stems(lang, ref, src)) == {"st-producer"}


def test_producers_of_jvm_unchanged_without_paths():
    assert _producers_of(_plan([JVM_PROD]), [JVM_REF], []) == {"st-producer"}


# ═══════════════════════════════════════════════════════════════
# 消费者 2：_package_in_baseline（假阳性护栏）
# ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("lang,ref,src,prod,stem,new", CASES, ids=_IDS)
def test_package_in_baseline_sees_existing_tree(lang, ref, src, prod, stem, new, tree):
    """护栏语义＝"包在树里（只是漏 seed）→ 继续等，别硬失败"。老口径对非 JVM 恒 False
    ⇒ 护栏失效 ⇒ 判臆造 ⇒ 连坐放弃。"""
    assert _package_in_baseline(str(tree), ref) is False, \
        "夹具前提变了：老口径竟然认出来了"
    assert _package_in_baseline(str(tree), ref,
                               path_stems=_stems(lang, ref, src)) is True


def test_package_in_baseline_still_negative_when_truly_absent(tmp_path):
    """★别把闸拧成恒不触发★ 真不在树里时必须仍返 False（否则 #10 幽灵生产者无人拦）。"""
    assert _package_in_baseline(
        str(tmp_path), "github.com/acme/shop/internal/nope",
        path_stems=["internal/nope"]) is False


def test_package_in_baseline_jvm_unchanged(tree):
    assert _package_in_baseline(str(tree), JVM_REF) is True


# ═══════════════════════════════════════════════════════════════
# 消费者 2b：_blocked_pkg_unrecoverable（futile 判据）
# ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("lang,ref,src,prod,stem,new", CASES, ids=_IDS)
def test_futile_no_longer_true_for_existing_tree(lang, ref, src, prod, stem, new, tree):
    kw = dict(blocked_pkgs=[ref], producers=set(), unsat=set(), completed_ok=set(),
              pending=set(), project_path=str(tree), self_id="st-x")
    assert _blocked_pkg_unrecoverable(**kw) is True, "夹具前提变了"
    assert _blocked_pkg_unrecoverable(
        **kw, paths_by_ref={ref: _stems(lang, ref, src)}) is False


def test_symbol_level_arm_uses_path_namespace(tmp_path):
    """★复核 CRITICAL-1★ 类级臂原先**抢先 return**，把整个符号级通道挡在新口径之外：
    `_class_in_baseline` 是 JVM-only（`endswith(".java")` + `\\b(class|interface|enum|
    record)\\s+`），非 JVM 符号恒返 False ⇒ futile=True ⇒ 而 `_prods` 已被本批修成非空 ⇒
    走 `_unrecoverable` 而非 `_selfheal` ⇒ **首轮连坐放弃**。

    覆盖面不是边角：四个非 JVM driver **全部**产符号级 ref（Go `undefined:`、TS TS2305、
    Rust `no X in Y`/E0425、Python `cannot import name`），Go 那条 driver 的注释还写着该形态
    "比 Java 更常见（同包多文件是 Go 的常态组织方式）"。
    """
    (tmp_path / "internal" / "svc").mkdir(parents=True)
    (tmp_path / "internal" / "svc" / "user.go").write_text("package svc\n")
    ref = "github.com/acme/shop/internal/svc"
    cls = f"{ref}.NewUserSvc"
    kw = dict(blocked_pkgs=[ref], producers=set(), unsat=set(), completed_ok=set(),
              pending=set(), project_path=str(tmp_path), self_id="st-x",
              blocked_classes=[cls])
    # 老路（无路径口径）：JVM-only 类级判据 → 恒 futile
    assert _blocked_pkg_unrecoverable(**kw) is True, "夹具前提变了"
    # 新路：符号的容器用路径口径判在树里 → 继续等
    assert _blocked_pkg_unrecoverable(
        **kw, paths_by_ref={ref: ["internal/svc"], cls: ["internal/svc"]}) is False


def test_worker_emits_path_stems_for_symbol_fqns(tmp_path):
    """CRITICAL-1 的配套接线：worker 必须给**符号 FQN**也发路径口径，否则 brain 类级臂
    `_pbr.get(cls)` 恒空 → 仍走 JVM-only 判据 → 修复形同没做。"""
    import swarm.worker.l1_pipeline as lp
    (tmp_path / "internal" / "svc").mkdir(parents=True)
    ref = "github.com/acme/shop/internal/svc"
    cls = f"{ref}.NewUserSvc"
    details: dict = {}
    lp.decide_unbuilt_internal_verdict(
        details, FileScope(writable=["internal/handler/u.go"]), {ref}, [cls],
        cmd="go build ./...", stage="build", output="", language_key="go",
        project_path=str(tmp_path), timeout=20, run=_probe(),
        driver_refs=[ed.MissingRef(ref=ref, symbol="NewUserSvc",
                                   src="internal/handler/u.go")])
    assert details["blocked_on_paths_by_ref"].get(cls) == ["internal/svc"], \
        "符号 FQN 无路径口径 ⇒ brain 类级臂拿不到 ⇒ CRITICAL-1 未真修"


def test_go_root_stem_survives_to_all_consumers(tmp_path):
    """★复核 HIGH-2★ Go 根包词干是 `""`（`ref_tree_paths` 刻意返 `[""]`，由 `_stem_matches`
    特判成"只认工程根直下"）。它是**合法词干**，不是"解不出"。原实现 `[s for s in stems if s]`
    把它过滤成键缺席 ⇒ 三个消费者回落 Java 点分（`github.com/acme/shop` → `github/com/…`
    无一命中）⇒ 首轮连坐放弃，**且零留痕**。触发条件毫不刁钻：`main.go` 里任何 `undefined: X`。
    """
    (tmp_path / "main.go").write_text("package main\n")
    mr = ed.MissingRef(ref="github.com/acme/shop", symbol="buildRouter", src="main.go")
    assert ed.ref_path_stems("go", [mr], "/p", 20, _probe()) == \
        {"github.com/acme/shop": [""]}, "根词干被过滤掉了"
    # 三个消费者都认它
    assert _producers_of(_plan(["main.go"]), ["github.com/acme/shop"], [],
                         paths=[""]) == {"st-producer"}
    assert _package_in_baseline(str(tmp_path), "github.com/acme/shop",
                                path_stems=[""]) is True
    assert _blocked_pkg_unrecoverable(
        blocked_pkgs=["github.com/acme/shop"], producers=set(), unsat=set(),
        completed_ok=set(), pending=set(), project_path=str(tmp_path), self_id="st-x",
        paths_by_ref={"github.com/acme/shop": [""]}) is False


def test_root_stem_does_not_swallow_subdirs(tmp_path):
    """根词干只认工程根**直下**文件——否则整棵树都算自产/在树里。"""
    (tmp_path / "internal" / "svc").mkdir(parents=True)
    (tmp_path / "internal" / "svc" / "user.go").write_text("x\n")
    assert _producers_of(_plan(["internal/svc/user.go"]), ["github.com/acme/shop"], [],
                         paths=[""]) == set()


def test_unregistered_stack_never_reaches_blocked_tail(tmp_path):
    """（原 `test_blocked_without_path_namespace_leaves_account`，随决定 3 改写）

    原测试断言"未收录栈判 BLOCKED 时要落 `blocked_on_paths_absent`"——但它是**直调裁决本体
    硬塞** `language_key="elixir"` + 非空 `blocked_pkgs` 才走到的，生产上到不了：未收录栈在
    `blocked_on_unbuilt_internal` 就会以 `disarm=unregistered_stack` 返空 ⇒ `blocked_pkgs`
    恒空 ⇒ 裁决层立刻短路。这正是"构造了生产代码从不产生的取值"那类假绿。
    改为断言**生产事实本身**：未收录栈根本进不了 BLOCKED 尾。
    """
    disarm: dict = {}
    pkgs, syms = ed.blocked_on_unbuilt_internal(
        "elixir", "** (CompileError) lib/a.ex:3: undefined function foo/0",
        str(tmp_path), 20, _probe(), disarm_out=disarm)
    assert (pkgs, syms) == (set(), []), "未收录栈竟产出了 blocked ref"
    assert disarm.get("reason") == "unregistered_stack"
    # 因此裁决层收到空集 → 立刻返 False，`blocked_on_paths*` 一个都不写
    details: dict = {}
    assert lp.decide_unbuilt_internal_verdict(
        details, FileScope(writable=["a/b.ex"]), pkgs, syms,
        cmd="mix compile", stage="build", output="", language_key="elixir",
        project_path=str(tmp_path), timeout=20, run=_probe()) is False
    assert "blocked_on_paths" not in details
    assert "blocked_on_paths_absent" not in details

def test_symbol_fqn_inherits_longest_matching_container(tmp_path):
    """★hunter 复核 HIGH-2★ 两个 ref 互为点分前缀时（`app.services` 与 `app.services.user`
    同批缺失），符号 FQN 必须继承**最长**匹配容器的词干。原实现"首命中即 break"取决于 dict
    插入序 ⇒ 可能继承 `app/services` ⇒ `_package_in_baseline` 拿**错容器**查 → 它存在 →
    futile=False「继续等」而真容器其实不在 ⇒ 烧满退避阶梯（最贵那侧），且结果非确定性。"""
    import swarm.worker.l1_pipeline as lp
    (tmp_path / "app" / "services").mkdir(parents=True)
    details: dict = {}
    lp.decide_unbuilt_internal_verdict(
        details, FileScope(writable=["app/main.py"]),
        {"app.services", "app.services.user"}, ["app.services.user.list_users"],
        cmd="pytest -q", stage="test", output="", language_key="python",
        project_path=str(tmp_path), timeout=20, run=_probe(),
        driver_refs=[ed.MissingRef(ref="app.services", symbol=None, src=None),
                     ed.MissingRef(ref="app.services.user", symbol="list_users",
                                   src=None)])
    got = details["blocked_on_paths_by_ref"]["app.services.user.list_users"]
    assert got == ["app/services/user", "src/app/services/user"], \
        f"符号 FQN 继承了错的容器词干（该取最长前缀）: {got}"


def test_selfheal_guidance_lists_only_granted_refs(tmp_path):
    """★hunter 复核 HIGH-1★ 词干被丢弃（定不了档/越界/已存在）时，只有部分 ref 的落点进了
    create_files —— 而指导文案原先列**全量**，等于承诺了没给的可写范围 ⇒ worker 建不了那些
    → 再失败/SCOPE_OBJECTION → 自愈配额（默认 2）耗尽 → abandon。这是 reviewer HIGH-3
    的失败形状经"丢弃"通道复发。丢弃必须留机读键（下游只看到"给了几个文件"，分不出成因）。
    """
    (tmp_path / "src" / "app" / "services").mkdir(parents=True)   # user 定得了档
    got = _derive_missing_type_files(
        [], ["app.services.user", "app.repo.order"], "",
        path_stems={"app.services.user": ["app/services/user",
                                          "src/app/services/user"],
                    "app.repo.order": ["app/repo/order", "src/app/repo/order"]},
        stack="python", project_path=str(tmp_path))
    assert got == ["src/app/services/user.py"], "夹具前提变了"
    # ★调生产本体★（不照抄一遍判据——那样改坏生产也不会红）
    from swarm.brain.nodes.failure import _granted_refs
    _pbr = {"app.services.user": ["app/services/user", "src/app/services/user"],
            "app.repo.order": ["app/repo/order", "src/app/repo/order"]}
    kept, dropped = _granted_refs(["app.services.user", "app.repo.order"], got, _pbr)
    assert kept == ["app.services.user"], \
        "对账没把被丢弃的 ref 剔出文案 ⇒ 会承诺没给的可写范围"
    assert dropped == ["app.repo.order"], "被丢弃的 ref 必须能被指名（机读账要用它）"


def test_class_arm_falls_back_per_class_not_by_dropping(tmp_path):
    """★hunter 复核 MED-4★ 类级臂原先把**没有词干**的类整条剔除（`for c in _cls if
    _pbr.get(c)`），而被剔的恰是可能投 False 票（"在树里→继续等"）的那些 ⇒ futile 从 False
    翻成 True ⇒ 该等的变成判永不可满足 → 连坐放弃（方向翻转）。逐个类各选口径才对。"""
    (tmp_path / "mod" / "src" / "main" / "java" / "com" / "acme").mkdir(parents=True)
    (tmp_path / "mod" / "src" / "main" / "java" / "com" / "acme" / "B.java").write_text(
        "package com.acme;\npublic class B {}\n")
    ref = "github.com/acme/shop/internal/svc"
    c_with = f"{ref}.A"                  # 有词干，容器**不在**树里 → 该投 True
    c_without = "com.acme.B"             # 无词干，走 JVM 类级判据 → **在**树里 → 投 False
    got = _blocked_pkg_unrecoverable(
        blocked_pkgs=[ref], producers=set(), unsat=set(), completed_ok=set(),
        pending=set(), project_path=str(tmp_path), self_id="st-x",
        blocked_classes=[c_with, c_without],
        paths_by_ref={c_with: ["internal/svc"]})
    assert got is False, \
        "无词干的类被整条剔除 ⇒ 它的 False 票丢了 ⇒ 该等的被判永不可满足→连坐"


def test_ref_path_stems_shares_memo_with_solver():
    """★hunter 复核 MED-2★ 两个理由：① 探针放大（实测 10 个 ref 读 go.mod 10 次，而求解器
    内部 memo 只读 1 次，且这里是失败路径 + 远程沙箱）；② **视图分叉**——步骤 3/4 吃 memo
    快照、本函数重新实探时，探针瞬时失败只发生在这一侧 ⇒ 该 ref 静默缺席 ⇒ brain 回落
    Java 点分 ⇒ 首轮连坐放弃。同一轮内对同一棵树必须给同一答案。"""
    calls = {"n": 0}

    def counting(cmd, pp, t=60):
        if "go.mod" in cmd and "awk" in cmd:
            calls["n"] += 1
            return 0, "github.com/acme/shop\n", ""
        return 1, "", ""

    refs = [ed.MissingRef(ref=f"github.com/acme/shop/internal/p{i}", symbol=None,
                          src=None) for i in range(10)]
    ed.ref_path_stems("go", refs, "/p", 20, counting)
    assert calls["n"] == 1, f"10 个 ref 读了 {calls['n']} 次 go.mod（没复用 memo）"


def test_sanitize_drops_are_observable(tmp_path, caplog):
    """★hunter 复核 MED-5★ 越界词干是**外部输入的攻击信号**（实测形态
    `github.com/acme/shop/../../../tmp/pwn`），丢弃方向对，但原先零日志零键＝零取证痕迹。"""
    import logging
    with caplog.at_level(logging.WARNING):
        assert _derive_missing_type_files(
            [], ["x"], "", path_stems={"x": ["../../../tmp/pwn"]}, stack="go",
            project_path=str(tmp_path)) == []
    assert any("越界" in r.message or "traversal" in r.message.lower()
               or "sanitize" in r.message.lower() for r in caplog.records), \
        "越界词干被静默丢弃 ⇒ 攻击信号零取证痕迹"


def test_futile_still_true_when_truly_hallucinated(tmp_path):
    """回归臂：ref 真不在树里 + 无生产者 ⇒ 仍判永不可满足（快失败，不烧退避阶梯）。"""
    assert _blocked_pkg_unrecoverable(
        blocked_pkgs=["github.com/acme/shop/internal/nope"], producers=set(),
        unsat=set(), completed_ok=set(), pending=set(), project_path=str(tmp_path),
        self_id="st-x", paths_by_ref={"github.com/acme/shop/internal/nope":
                                      ["internal/nope"]}) is True


# ═══════════════════════════════════════════════════════════════
# 消费者 3：_derive_missing_type_files（自愈：该建哪个文件）
# ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("lang,ref,src,prod,stem,new", CASES, ids=_IDS)
def test_derive_missing_files_for_non_jvm(lang, ref, src, prod, stem, new, tmp_path):
    """治前：JVM 推导锚死 `/src/main/java|kotlin/` + 类名＝文件名 ⇒ 非 JVM 一个 marker
    都不命中 ⇒ 返 [] ⇒ `_unrecoverable` ⇒ 首轮连坐放弃。

    ★按 ref 分组传 + 给 project_path★（HIGH-3/HIGH-4）：多候选据真实布局定档，
    故夹具必须**真的物化那个布局**，否则测的是"猜"而不是"据证据"。
    """
    (tmp_path / stem).parent.mkdir(parents=True, exist_ok=True)
    assert _derive_missing_type_files([prod], [ref], "cannot find symbol: class Foo") \
        == [], "夹具前提变了：老推导竟然推出了东西"
    assert _derive_missing_type_files(
        [prod], [ref], "", path_stems={ref: _stems(lang, ref, src)}, stack=lang,
        project_path=str(tmp_path)) == [new]


def test_derive_go_package_is_a_directory():
    """★Go 的包是目录★ 词干 `internal/svc` 该建 `internal/svc/svc.go`，
    不是 `internal/svc.go`（后者建出与包同名的**文件**，编译仍缺包）。"""
    assert _derive_missing_type_files(
        [], [], "", path_stems=["internal/svc"], stack="go") == ["internal/svc/svc.go"]


@pytest.mark.parametrize("layout,expect", [
    ("src/app/services", "src/app/services/user.py"),   # 真 src-layout（PyPA 推荐）
    ("app/services", "app/services/user.py"),           # 真顶层布局
])
def test_derive_picks_layout_by_evidence_not_by_guess(tmp_path, layout, expect):
    """★复核 HIGH-4★ 双候选（`app/x` vs `src/app/x`）**不许猜**。原实现"取最短＝顶层优先"
    在真 src-layout 工程（根下无 `app/`）上给出 `app/services/user.py` ⇒ 落点错、还在根下
    种出**第二棵顶层包**污染工程树。改为按证据定档：父目录真实存在的那个候选胜出。"""
    (tmp_path / layout).mkdir(parents=True)
    got = _derive_missing_type_files(
        [], ["app.services.user"], "",
        path_stems={"app.services.user": ["app/services/user",
                                          "src/app/services/user"]},
        stack="python", project_path=str(tmp_path))
    assert got == [expect]


def test_derive_fails_honest_when_layout_undecidable():
    """两个候选都定不了档（无 project_path、scope 也不指向任一前缀）→ **丢弃该 ref**
    （纪律 2：解析不出→如实丢弃，绝不逼 worker 臆造落点）。"""
    assert _derive_missing_type_files(
        [], ["app.services.user"], "",
        path_stems={"app.services.user": ["app/services/user",
                                          "src/app/services/user"]},
        stack="python") == []


def test_derive_keeps_every_independent_ref(tmp_path):
    """★复核 HIGH-3★ "多候选二选一"是**单个 ref 内**的事。原实现在调用点把所有 ref 的候选
    摊平进一个池、再 `min()` 取全局一个 ⇒ 缺 3 个包只建 1 个 ⇒ `retry_guidance` 说"已纳入
    可写范围"而实际没纳入 ⇒ worker 再失败/SCOPE_OBJECTION ⇒ 自愈配额（默认 2）耗尽 → abandon。"""
    got = _derive_missing_type_files(
        [], ["a", "b"], "",
        path_stems={"a": ["internal/svc"], "b": ["internal/repo"]},
        stack="go", project_path=str(tmp_path))
    assert got == ["internal/svc/svc.go", "internal/repo/repo.go"]


def test_derive_rejects_path_traversal_stems(tmp_path):
    """★复核 HIGH-5★ 词干源头是**构建输出＝外部输入**。实测
    `no required module provides package github.com/acme/shop/../../../tmp/pwn` 能让
    `is_internal` 放行、`ref_tree_paths` 吐 `../../../tmp/pwn`，自愈据此往 create_files 塞
    `../../../tmp/pwn/pwn.go` ⇒ **写到工程树外**。校验照抄 sibling
    `_amend_scope_with_missing_files`（纪律 5：修一类先全仓捞 sibling）。"""
    for bad in ("../../../tmp/pwn", "/etc/passwd", "a/../../b"):
        assert _derive_missing_type_files(
            [], ["x"], "", path_stems={"x": [bad]}, stack="go",
            project_path=str(tmp_path)) == [], f"{bad} 不该进 create_files"


def test_derive_skips_already_existing_file(tmp_path):
    """★复核 HIGH-5 第二道★ 落点**已存在**时不再指为"该新建"——sibling 的注释写明来源
    （对抗复核#5）："补进 create_files 会让 worker 从零重写覆掉基线/上游内容"。
    而 `missing_created_files` 闸只查"不存在"，存在的一律放行 ⇒ 覆写无人拦。

    ★词干必须用生产真会产出的形状★（hunter 复核 MED-1）：`GoErrorDriver.ref_tree_paths`
    **恒返包目录**、永不带文件名。原夹具传 `internal/svc/svc`（带文件名）⇒ 测的是生产不产生
    的取值 ⇒ 那道闸对 Go 其实恒不生效（落点是 `<stem>/<base>.go`，而过滤按 `stem+ext` 判，
    两者不是同一路径），突变却假锁通过。
    """
    (tmp_path / "internal" / "svc").mkdir(parents=True)
    (tmp_path / "internal" / "svc" / "svc.go").write_text("// 300 行既有实现\n")
    assert _derive_missing_type_files(
        [], ["x"], "", path_stems={"x": ["internal/svc"]}, stack="go",
        project_path=str(tmp_path)) == [], "Go 的落点已存在却仍指为『该新建』⇒ 会覆写既有实现"
    # 非 Go 栈同场景（落点＝stem+ext）也必须拦住
    (tmp_path / "src" / "routes").mkdir(parents=True)
    (tmp_path / "src" / "routes" / "users.ts").write_text("export const x = 1;\n")
    assert _derive_missing_type_files(
        [], ["y"], "", path_stems={"y": ["src/routes/users"]}, stack="node",
        project_path=str(tmp_path)) == []


def test_derive_returns_empty_for_unregistered_stack():
    """未收录栈 → 不猜扩展名（fail-honest，交连坐放弃），绝不建出个 `x.unknown`。"""
    assert _derive_missing_type_files([], [], "", path_stems=["a/b"],
                                     stack="elixir") == []


def test_derive_jvm_unchanged_without_stems():
    got = _derive_missing_type_files([JVM_PROD], [JVM_REF],
                                     "symbol: class Foo")
    assert got == ["mod/src/main/java/com/acme/svc/Foo.java"]


# ═══════════════════════════════════════════════════════════════
# 端到端：worker 裁决写出的 details 能被 brain 直接消费
# ═══════════════════════════════════════════════════════════════


def test_verdict_details_carry_path_keys_consumable_by_brain(tmp_path):
    """★接线证明★ 走 `decide_unbuilt_internal_verdict` 本体拿 details，再把 details 里的键
    原样喂给三个 brain 消费者 —— 证"worker 写的键 brain 真解得开"，不是各测一半。"""
    import swarm.worker.l1_pipeline as lp
    (tmp_path / "internal" / "svc").mkdir(parents=True)
    (tmp_path / "internal" / "svc" / "user.go").write_text("package svc\n")
    ref = "github.com/acme/shop/internal/svc"
    details: dict = {}
    blocked = lp.decide_unbuilt_internal_verdict(
        details, FileScope(writable=["internal/handler/u.go"]), {ref}, [],
        cmd="go build ./...", stage="build", output="",
        language_key="go", project_path=str(tmp_path), timeout=20,
        run=_probe(), driver_refs=[ed.MissingRef(ref=ref, symbol=None,
                                                 src="internal/handler/u.go")])
    assert blocked is True
    assert details["blocked_on_paths"] == ["internal/svc"]
    assert details["blocked_on_paths_by_ref"] == {ref: ["internal/svc"]}
    # brain 侧三个消费者直接吃 details 里的键
    assert _producers_of(_plan(["internal/svc/user.go"]),
                         details["blocked_on_packages"], [],
                         paths=details["blocked_on_paths"]) == {"st-producer"}
    assert _package_in_baseline(
        str(tmp_path), ref,
        path_stems=details["blocked_on_paths_by_ref"][ref]) is True
    # ★必须用**生产的**传法★（复核指出原写法按 ref 索引单条，而生产是整个 dict——
    # "测试构造了生产代码从不产生的取值"是本仓记过的第 2 类假绿，HIGH-3 正是从那个缺口逃逸的）
    assert _derive_missing_type_files(
        [], details["blocked_on_packages"], "",
        path_stems=details["blocked_on_paths_by_ref"],
        stack=details["blocked_via_error_driver"],
        project_path=str(tmp_path)) == ["internal/svc/svc.go"]


def test_verdict_marks_absence_when_no_path_stems(tmp_path):
    """缺席必须可辨（否则"没路径口径"与"忘了算"不可分；brain 会回落老路 → 大概率连坐）。"""
    import swarm.worker.l1_pipeline as lp
    ref = "./routes/users"           # 无 src ⇒ 相对导入解不出 ⇒ 无词干
    details: dict = {}
    lp.decide_unbuilt_internal_verdict(
        details, FileScope(writable=["src/app.ts"]), {ref}, [],
        cmd="(L1.2 compile)", stage="compile", output="",
        language_key="node", project_path=str(tmp_path), timeout=20,
        run=_probe(), driver_refs=[ed.MissingRef(ref=ref, symbol=None, src=None)])
    # ★词干解不出 ⇒ 必然先在 CRITICAL-2 的 UNKNOWN 闸落 FAIL★ 两处用同一个
    # `ref_tree_paths` 且门控逐字相同，所以"到了 BLOCKED 尾却没词干"不可能发生
    # （原先我在那儿写了个 `blocked_on_paths_absent` 降级账，突变证明锁不住＝不可达，已删）。
    assert details.get("blocked_owner_unresolved") == [ref]
    assert details.get("pipeline_blocked") is None
    assert "blocked_on_paths" not in details


def test_jvm_verdict_writes_no_path_keys(tmp_path):
    """★JVM 零回归的接线证明★ java 走专用链时 `language_key` 恒 None ⇒ 路径键整块不写。"""
    import swarm.worker.l1_pipeline as lp
    details: dict = {}
    blocked = lp.decide_unbuilt_internal_verdict(
        details, FileScope(writable=["mod/src/main/java/com/acme/a/A.java"]),
        {JVM_REF}, [], cmd="mvn -q compile", stage="build", output="")
    assert blocked is True
    assert "blocked_on_paths" not in details
    assert "blocked_on_paths_by_ref" not in details
    assert details["blocked_on_packages"] == [JVM_REF]
    assert os.sep is not None      # 占位：确保 import 被用到（ruff F401 防呆）



# ══════════════════════════════════════════════
# 决定 3（用户拍板"按真实可达性重判"）
# ══════════════════════════════════════════════

_ABSENT_SHAPES = [
    ("go", "github.com/acme/shop", None, "根包 ref==module"),
    ("go", "github.com/other/x", None, "非本模块"),
    ("node", "./routes/users", None, "相对导入无 src"),
    ("node", "../svc/user", None, "../ 无 src"),
    ("node", "express", None, "裸包名"),
    ("rust", "self::helper", None, "self:: 无上下文"),
    ("rust", "super::helper", None, "super:: 无上下文"),
    ("python", "app.services.user", None, "正常（有词干）"),
]


@pytest.mark.parametrize("lang,ref,src,label", _ABSENT_SHAPES,
                         ids=[c[3] for c in _ABSENT_SHAPES])
def test_absent_branch_is_unreachable_by_construction(lang, ref, src, label, tmp_path):
    """★决定 3：`blocked_on_paths_absent` 按**实测**定案为不可达，本条锁住那个不变量★

    这条账加过、删过、又加过、最后删掉。因果值得留：
      · 最初删：推理是"词干解不出的 ref 必先在 UNKNOWN 闸早返"。
      · reviewer 证伪：漏了**第三态**——词干*解得出但被过滤成空*（当时
        `ref_path_stems` 的 `[s for s in stems if s]` 把 Go 根包的合法词干 `[""]` 滤没了）。
      · hunter 又指它"生产不可达 + 零消费者"＝空账。
      ★两人都对，只是针对的代码状态不同★：HIGH-2 那轮把过滤修了，第三态消失，分支重新不可达。

    本条穷举八种形态走**裁决本体**，断言：要么有词干（走 BLOCKED 尾），要么落
    `blocked_owner_unresolved`（早返 FAIL）—— **绝不出现"BLOCKED 了却没有路径口径"**。
    维持它的责任在 `ref_path_stems`：不许再把合法词干过滤掉。谁破了这条不变量，本条即红。
    """
    mr = ed.MissingRef(ref=ref, symbol=None, src=src)
    details: dict = {}
    blocked = lp.decide_unbuilt_internal_verdict(
        details, FileScope(writable=["a/b.txt"]), {ref}, [],
        cmd="x", stage="build", output="", language_key=lang,
        project_path=str(tmp_path), timeout=20, run=_probe(), driver_refs=[mr])
    assert "blocked_on_paths_absent" not in details, \
        "第三态又出现了（词干被过滤成空却没落 unresolved）⇒ 这条降级账得恢复"
    if blocked:
        assert details.get("blocked_on_paths"), \
            "判了 BLOCKED 却没有路径口径 ⇒ brain 会回落 Java 点分 → 首轮连坐放弃"
    else:
        assert details.get("blocked_owner_unresolved"), \
            "落 FAIL 却没说明原因（归属未知）⇒ 降级无痕"
