#!/usr/bin/env python3
"""B-4a（27 号文 V-C1）：构建面三态 —— 关掉最大的假过口。

## V-C1 是什么

`_detect_build_cmd_generic` 返回 None → `compile_ok=None` → **不 append 任何 issue** →
`passed = len(issues) == 0` → **判 PASS**。而它对下列栈全部返 None：

    PHP · Ruby · C# · Elixir · 仅 requirements.txt 的 Python · 无 build script 的 Node

于是这些栈 **L2 永久 no-op 且判通过**，`can_auto_accept_delivery` 照样放行 → 坏产物直达交付
（27 号文 §1 矩阵里那一整列 "✖ 判 PASS"）。

根因是**一个哨兵承担了两种语义**：`compile_ok=None` 同时表示
  ① "纯 docs 仓，没有构建面，跳过是合理的"
  ② "这个栈的编译闸本仓没实现"
而**全仓无消费者能区分这两种 None**。①放行是对的，②放行是假过。

## 治法

`detect_build_surface()` 返回三态 `(cmd, reason)`；②产 issue（`passed` 自动翻 False）+
机读键 `compile_gate_unsupported_stack` + `record_degrade` + WARNING。
`can_auto_accept_delivery` 给它**专类归因**——沿用 `clarify_blocked_by_facts` 的先例，
绝不让"我们没验"伪装成"你的代码编译失败"，那会把人按错的根因去查。

## 诚实边界

这**不阻断交付**，是拒绝 auto_accept、强制人工确认（27 号文 §6.3 原则 3 的口径）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_s = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_m = importlib.util.module_from_spec(_s)
_s.loader.exec_module(_m)

from swarm.brain.gates import can_auto_accept_delivery  # noqa: E402
from swarm.brain.integration_review import (  # noqa: E402
    NO_BUILD_SURFACE,
    UNSUPPORTED_STACK,
    detect_build_surface,
)
from swarm.stacks import STACK_SPEC  # noqa: E402


# ══════════════════════════════════════════════
# ① 三态本身
# ══════════════════════════════════════════════

def test_pure_docs_repo_is_no_build_surface_not_unsupported(tmp_path):
    """纯 docs 仓 → `NO_BUILD_SURFACE`。**这一格必须继续放行**。

    治本方向错了会变成"所有仓都判未验证"——那是把假过换成误杀，同样不可接受。
    """
    (tmp_path / "README.md").write_text("# docs\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
    cmd, reason, _m = detect_build_surface(str(tmp_path))
    assert cmd is None
    assert reason == NO_BUILD_SURFACE, f"纯 docs 仓不该判未支持栈，实得 {reason}"


def test_maven_repo_still_derives_its_command_bytewise(tmp_path):
    """Maven 路径**字节等价**（G9/L3 铁律：治本绝不改 Maven 既有行为）。"""
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    cmd, reason, _m = detect_build_surface(str(tmp_path))
    assert reason == "ok"
    assert cmd == "mvn -q -DskipTests compile"


@pytest.mark.parametrize("manifest,content,expect_key", [
    ("composer.json", '{"name":"demo/app"}', "php"),
    ("Gemfile", 'source "https://rubygems.org"\n', "ruby"),
    ("mix.exs", "defmodule Demo.MixProject do\nend\n", "elixir"),
    ("pubspec.yaml", "name: demo\n", "dart"),
])
def test_stacks_with_a_build_surface_but_no_gate_are_unsupported_not_skippable(
        tmp_path, manifest, content, expect_key):
    """★V-C1 本尊★ 有构建面却派生不出命令 → `unsupported_stack:<key>`，**绝不**当合理跳过。

    `requirements.txt` 那格是 `STACK_SPEC` 照出来的漂移：它在 python 的 `root_manifests` 里
    （Django/Flask 常态），而 `_detect_build_cmd_generic` 只认 pyproject.toml/setup.py。
    """
    (tmp_path / manifest).write_text(content, encoding="utf-8")
    cmd, reason, _m = detect_build_surface(str(tmp_path))
    assert cmd is None
    assert reason == f"{UNSUPPORTED_STACK}:{expect_key}", (
        f"{manifest} 在场却判 {reason}——这就是'没实现'伪装成'没有构建'")


@pytest.mark.parametrize("files,expect_cmd_contains", [
    # ★这三格是我上一版引入的回归（双复核 HIGH-2 实证）★ 判据"STACK_SPEC 有该清单 +
    # 派生表返 None = 闸未实现"是错的——两张表**宽度不同 ≠ 闸不存在**。这三个栈的通用命令
    # 本来就跑得通，被误判成 unsupported 后（按当时的产 issue 实现）会烧完 replan 后 FAILED。
    ({"go.work": "go 1.22\n\nuse (\n\t./auth\n)\n"}, "go build"),
    ({"settings.gradle": "include ':app'\n"}, "classes"),
    ({"settings.gradle.kts": 'include(":app")\n'}, "classes"),
    ({"requirements.txt": "flask==3.0.0\n"}, "compileall"),
    ({"Pipfile": "[packages]\nflask = \"*\"\n"}, "compileall"),
])
def test_supported_stacks_with_wider_manifests_are_not_misjudged(
        tmp_path, files, expect_cmd_contains):
    """已实现的栈**绝不**因派生表清单太窄而被打成 unsupported（误杀方向）。

    `go.work` 那格尤其刺眼：它打到了本仓自己的 B-0 `go_work` 夹具头上——hunter 跑
    `test/stack_workspaces.py` 的真实夹具实证 `unsupported_stack:go`。
    """
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    cmd, reason, _m = detect_build_surface(str(tmp_path))
    assert reason == "ok", f"{list(files)} 被误判 {reason}——已实现的栈被当成闸缺失"
    assert expect_cmd_contains in (cmd or ""), f"{list(files)} → {cmd!r}"


def test_b0_go_work_fixture_is_not_misjudged(make_workspace):
    """回归锁：直接拿 B-0 的 `go_work` 夹具跑——它曾被判 `unsupported_stack:go`。

    用本仓自己的夹具当判据，比我另造一棵树更硬：夹具形状是共享的单一事实源。
    """
    fx = make_workspace("go_work")
    cmd, reason, _m = detect_build_surface(str(fx.root))
    assert reason == "ok", f"B-0 go_work 夹具被判 {reason}（HIGH-2 回归）"
    assert cmd == "go build ./..."


@pytest.mark.parametrize("extra", ["Makefile", "CMakeLists.txt"])
def test_task_runner_files_do_not_condemn_a_docs_repo(tmp_path, extra):
    """★HIGH-3 误杀锁★ `Makefile`/`CMakeLists.txt` 不得让纯 docs 仓被判 unsupported。

    它们是**构建工具**而非**栈**，且 Makefile 作任务运行器在 docs 仓极常见（Sphinx 的
    `make html`）。实证：入表后纯 docs + Sphinx Makefile → `unsupported_stack:make`
    → 按当时实现烧完 replan 后 FAILED。原 M9 对照臂只造了 `README.md`+`docs/*.md`，
    **没有 Makefile**，所以这一格当时没锁到。
    """
    (tmp_path / "README.md").write_text("# docs\n", encoding="utf-8")
    (tmp_path / extra).write_text("html:\n\tsphinx-build . _build\n", encoding="utf-8")
    _cmd, reason, _m = detect_build_surface(str(tmp_path))
    assert reason == NO_BUILD_SURFACE, f"{extra} 让纯 docs 仓被判 {reason}（误杀）"


def test_matched_manifests_reports_the_full_set_not_just_first(tmp_path):
    """MEDIUM-3：混栈仓报全集。短路首命中会让人按错的栈去查。"""
    (tmp_path / "Gemfile").write_text("source 'x'\n", encoding="utf-8")
    (tmp_path / "composer.json").write_text('{"name":"d/a"}', encoding="utf-8")
    _cmd, reason, matched = detect_build_surface(str(tmp_path))
    assert reason.startswith(UNSUPPORTED_STACK)
    assert {"Gemfile", "composer.json"} <= set(matched), matched


def test_glob_is_escaped_for_bracketed_paths(tmp_path):
    """LOW-1：路径含 `[`/`]` 时 glob 会当字符类 → 漏检 → 退回假过。"""
    d = tmp_path / "proj[1]"
    d.mkdir()
    (d / "Demo.sln").write_text("Solution\n", encoding="utf-8")
    _cmd, reason, _m = detect_build_surface(str(d))
    assert reason == f"{UNSUPPORTED_STACK}:csharp", f"含方括号路径漏检：{reason}"


def test_monorepo_subdir_gap_is_recorded_not_claimed_closed(tmp_path):
    """★诚实边界锁（HIGH-1）★ 后端在子目录的 monorepo **仍是假过**，本批未关。

    §7.9 曾声称"关最大假过口"，而 `detect_build_surface` 全部只查工程根 → §1 矩阵
    "任意栈 + 后端在子目录"那一行一格未动。这条把**未关**这个事实钉成断言：
    B-4b 的 V-H1 修好那天它会红，逼人回来改口径（与 xfail(strict) 同款用法）。
    """
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "pom.xml").write_text("<project/>", encoding="utf-8")
    _cmd, reason, _m = detect_build_surface(str(tmp_path))
    assert reason == NO_BUILD_SURFACE, (
        "子目录探测已实现 → HIGH-1 已治 → 请更新 §7.9 口径并把本测试翻成正向断言")


def test_csharp_project_globs_are_detected(tmp_path):
    """C# 用 glob 判（`*.sln`/`*.csproj` 文件名不固定）——四闸全 skip 的组合之一。"""
    (tmp_path / "Demo.sln").write_text("Microsoft Visual Studio Solution File\n",
                                       encoding="utf-8")
    cmd, reason, _m = detect_build_surface(str(tmp_path))
    assert cmd is None and reason == f"{UNSUPPORTED_STACK}:csharp"


def test_pure_js_without_build_script_is_unsupported_not_skippable(tmp_path):
    """无 build script 且无 tsconfig 的 Node → 有构建面（package.json）却无编译闸。

    旧行为返 None 被当"合理跳过"（代码注释还写着"与强制成功有本质区别"——那句对，但
    它落进了同一个 None，于是 `passed` 照样 True）。
    """
    (tmp_path / "package.json").write_text('{"name":"demo"}', encoding="utf-8")
    cmd, reason, _m = detect_build_surface(str(tmp_path))
    assert cmd is None and reason == f"{UNSUPPORTED_STACK}:npm"


def test_every_registered_stack_is_classifiable(tmp_path):
    """准入闸：`STACK_SPEC` 里每个栈的根清单在场时，**绝不**被判成 NO_BUILD_SURFACE。

    新增一栈却忘了给它编译命令时，这条保证它落进 unsupported（fail-closed）而不是
    悄悄变成"没有构建面"→ 判 PASS。
    """
    for key, spec in STACK_SPEC.items():
        d = tmp_path / key
        d.mkdir()
        (d / spec.root_manifests[0]).write_text("x\n", encoding="utf-8")
        _cmd, reason, _m = detect_build_surface(str(d))
        assert reason != NO_BUILD_SURFACE, (
            f"{key}: 根清单 {spec.root_manifests[0]} 在场却判无构建面 → 会静默判 PASS")


# ══════════════════════════════════════════════
# ② 接线：L2 真的产 issue（不只是三态函数对）
# ══════════════════════════════════════════════

def test_l2_records_the_fact_without_producing_an_issue(tmp_path, monkeypatch):
    """★接线事实 + CRITICAL-3 整改★ L2 记下机读事实，但**不产 issue**。

    为什么不产 issue：产了 → `passed=False` → `l2_passed=False` → `after_verify_l2` 强制
    `handle_failure` → replan ×2 → escalate → FAILED/PARTIAL。而"闸没实现"是磁盘事实，
    replan **零修复力**（重规划后 L2 必逐字复现），纯烧钱；且 escalate 出态必带
    `failure_escalated`，拒因变成"子任务重试耗尽"= 把"我们没验"说成"worker 没干好"。
    §6.3 原则 3 原文：**不是拦交付，是拒绝 auto_accept**。

    判据：把 `elif _surface_reason.startswith(UNSUPPORTED_STACK + ":")` 整块删掉，这条必须红。
    """
    from swarm.brain import integration_review as ir

    (tmp_path / "composer.json").write_text('{"name":"demo/app"}', encoding="utf-8")
    monkeypatch.setattr(ir, "_reset_worktree_to_head", lambda *a, **k: [])
    monkeypatch.setattr(ir, "apply_git_diff", lambda *a, **k: {"ok": True})

    # 注意实参序：`_run_worktree_phase(path, diff, details, issues, ...)`（details 在前）
    passed, issues, details = ir._run_worktree_phase(
        str(tmp_path), "diff --git a/x b/x\n", {}, [],
        timeout=10, compile_runner=None, base_ref=None)

    assert details.get("compile_skip_reason") == f"{UNSUPPORTED_STACK}:php"
    assert details.get("compile_gate_unsupported_stack") == "php"
    assert details.get("compile_ok") is None
    assert "composer.json" in (details.get("compile_surface_manifests") or [])
    assert not any("编译闸未实现" in i for i in issues), (
        f"产了 issue → l2_passed=False → 烧 replan 后 FAILED（CRITICAL-3）：{issues}")
    assert passed is True, (
        "不该在 L2 拦死——拒放行的活交给 can_auto_accept_delivery（§6.3 原则 3）")


def test_l2_degrade_counter_is_recorded(tmp_path, monkeypatch):
    """降级必须有机读账（纪律 3：降级路径至少一个机读键 + 一次 WARNING）。"""
    from swarm.brain import integration_review as ir
    from swarm.infra.degrade import degrade_counts, reset_degrade_counts

    (tmp_path / "Gemfile").write_text("source 'x'\n", encoding="utf-8")
    monkeypatch.setattr(ir, "_reset_worktree_to_head", lambda *a, **k: [])
    monkeypatch.setattr(ir, "apply_git_diff", lambda *a, **k: {"ok": True})
    reset_degrade_counts()
    try:
        ir._run_worktree_phase(str(tmp_path), "d", {}, [],
                               timeout=10, compile_runner=None, base_ref=None)
        counts = degrade_counts()
        assert any(k.startswith("brain.l2.compile_gate_unsupported_stack") for k in counts), (
            f"无 degrade 计数 → 复盘时'多栈支持了多少'永远说不清（§2.5）：{counts}")
    finally:
        reset_degrade_counts()


def test_pure_docs_repo_still_passes_l2(tmp_path, monkeypatch):
    """误杀方向的对照臂：纯 docs 仓必须**照旧通过** L2。"""
    from swarm.brain import integration_review as ir

    (tmp_path / "README.md").write_text("# d\n", encoding="utf-8")
    monkeypatch.setattr(ir, "_reset_worktree_to_head", lambda *a, **k: [])
    monkeypatch.setattr(ir, "apply_git_diff", lambda *a, **k: {"ok": True})
    passed, issues, details = ir._run_worktree_phase(
        str(tmp_path), "d", {}, [], timeout=10, compile_runner=None, base_ref=None)
    assert details.get("compile_skip_reason") == NO_BUILD_SURFACE
    assert not details.get("compile_gate_unsupported_stack")
    assert passed is True, f"纯 docs 仓被误杀：{issues}"


# ══════════════════════════════════════════════
# ③ 归因：auto_accept 拒绝时说的是**真话**
# ══════════════════════════════════════════════

def _real_l2_success_state(stack_key: str) -> dict:
    """构造 **verify_l2 真实成功出态**（l2_passed=True + degraded_reasons）。

    ★为什么不手写最小 dict（双复核 CRITICAL-2）★ 我上一版的 gate 测试 state 只有
    `{l2_passed, l2_details}`，而生产的 `_l2_failure_state` 恒写 `failed_subtask_ids` +
    `verification_failure`、escalate 出口再加 `failure_escalated` —— **缺的正好是遮蔽新分支
    的那两个键**。hunter 的反向突变（只往测试 state 补生产必然键、生产代码零改动）让那两条
    当场红：这是硬检查②点名的"构造出生产代码从不产生的取值"。

    现在 L2 不再失败（CRITICAL-3 换层），真实出态就是"通过 + 带 degraded 留痕"，
    形状取自 `verify.py` 的 `if _l2_unverified_degraded: return {...}` 那条分支。
    """
    return {
        "l2_passed": True,
        "degraded_reasons": [f"l2_unsupported_stack:{stack_key}"],
        "failed_subtask_ids": [],
        "l2_details": {"integration_review": {"compile_gate_unsupported_stack": stack_key}},
    }


def test_auto_accept_refuses_on_unsupported_stack_with_honest_attribution():
    """★如实归因 + 分支真可达★ "我们没验" 绝不能伪装成别的东西。

    上一版把这条接在 `not l2_passed` 分支里，而到达 DELIVER 且 l2_passed=False 的唯一路径
    是 escalate 出口，它必带 `failure_escalated` + 非空 `failed_subtask_ids` —— 两者判序更前
    → 那条分支**在生产路径上永不执行**，人看到的拒因是"子任务重试耗尽已升级人工"，
    比 `l2_failed` 错得更远（L6"治本被静默关闭"复发形态）。
    """
    allow, reason = can_auto_accept_delivery(_real_l2_success_state("php"))
    assert allow is False
    assert reason.startswith("verification_unsupported_stack:php"), reason
    assert "未实现" in reason
    assert "worker" not in reason.lower() or "不是" in reason, "绝不把责任推给 worker"


def test_auto_accept_is_not_shadowed_by_earlier_returns_in_real_state():
    """★反向锁（hunter M-H 的正面版）★ 真实出态里那些"生产必然键"不得遮蔽本判定。

    往 state 里补齐生产上会同时存在的键（`verification_failure`、空 `failed_subtask_ids`、
    `runtime_smoke_passed=True`、`acceptance_passed=True`），本判定仍须命中。
    """
    state = {**_real_l2_success_state("ruby"),
             "verification_failure": "", "runtime_smoke_passed": True,
             "acceptance_passed": True, "l3_passed": None}
    allow, reason = can_auto_accept_delivery(state)
    assert allow is False, "被前置 return 抢先了？（CRITICAL-1 本尊）"
    assert reason.startswith("verification_unsupported_stack:ruby"), reason


def test_auto_accept_still_says_l2_failed_for_a_real_compile_failure():
    """对照臂：真编译失败仍归因 `l2_failed`（别把所有 L2 失败都改口）。

    这里用**真实失败出态形状**：`_l2_failure_state` 恒写 `failed_subtask_ids` +
    `verification_failure`。注意 `failed_subtasks` 判序在 l2 之前，故拒因是它——
    这正是生产实况，不是缺陷。
    """
    state = {"l2_passed": False, "failed_subtask_ids": ["st-1"],
             "verification_failure": "l2",
             "l2_details": {"integration_review": {"compile_ok": False},
                            "issues": ["L2.1 集成编译失败: error: cannot find symbol"]}}
    allow, reason = can_auto_accept_delivery(state)
    assert allow is False
    assert not reason.startswith("verification_unsupported_stack"), (
        "真编译失败被误报成'闸未实现'——误杀方向")


def test_auto_accept_reports_all_matched_stacks_not_just_the_first():
    """MEDIUM-3：混栈仓要报全集，别让人按错的栈去查。"""
    state = {"l2_passed": True, "failed_subtask_ids": [],
             "degraded_reasons": ["l2_unsupported_stack:php",
                                  "l2_unsupported_stack:ruby"]}
    allow, reason = can_auto_accept_delivery(state)
    assert allow is False
    assert "php" in reason and "ruby" in reason, reason


def test_clean_run_still_auto_accepts():
    """★误杀方向总闸★ 没有任何 unsupported 留痕的干净 run 必须照旧放行。"""
    allow, reason = can_auto_accept_delivery(
        {"l2_passed": True, "failed_subtask_ids": [], "degraded_reasons": [],
         "runtime_smoke_passed": True, "acceptance_passed": True})
    assert allow is True, f"干净 run 被误拦：{reason}"
