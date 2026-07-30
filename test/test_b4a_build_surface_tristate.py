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
    cmd, reason = detect_build_surface(str(tmp_path))
    assert cmd is None
    assert reason == NO_BUILD_SURFACE, f"纯 docs 仓不该判未支持栈，实得 {reason}"


def test_maven_repo_still_derives_its_command_bytewise(tmp_path):
    """Maven 路径**字节等价**（G9/L3 铁律：治本绝不改 Maven 既有行为）。"""
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    cmd, reason = detect_build_surface(str(tmp_path))
    assert reason == "ok"
    assert cmd == "mvn -q -DskipTests compile"


@pytest.mark.parametrize("manifest,content,expect_key", [
    ("composer.json", '{"name":"demo/app"}', "php"),
    ("Gemfile", 'source "https://rubygems.org"\n', "ruby"),
    ("mix.exs", "defmodule Demo.MixProject do\nend\n", "elixir"),
    ("pubspec.yaml", "name: demo\n", "dart"),
    ("requirements.txt", "flask==3.0.0\n", "python"),
])
def test_stacks_with_a_build_surface_but_no_gate_are_unsupported_not_skippable(
        tmp_path, manifest, content, expect_key):
    """★V-C1 本尊★ 有构建面却派生不出命令 → `unsupported_stack:<key>`，**绝不**当合理跳过。

    `requirements.txt` 那格是 `STACK_SPEC` 照出来的漂移：它在 python 的 `root_manifests` 里
    （Django/Flask 常态），而 `_detect_build_cmd_generic` 只认 pyproject.toml/setup.py。
    """
    (tmp_path / manifest).write_text(content, encoding="utf-8")
    cmd, reason = detect_build_surface(str(tmp_path))
    assert cmd is None
    assert reason == f"{UNSUPPORTED_STACK}:{expect_key}", (
        f"{manifest} 在场却判 {reason}——这就是'没实现'伪装成'没有构建'")


def test_csharp_project_globs_are_detected(tmp_path):
    """C# 用 glob 判（`*.sln`/`*.csproj` 文件名不固定）——四闸全 skip 的组合之一。"""
    (tmp_path / "Demo.sln").write_text("Microsoft Visual Studio Solution File\n",
                                       encoding="utf-8")
    cmd, reason = detect_build_surface(str(tmp_path))
    assert cmd is None and reason == f"{UNSUPPORTED_STACK}:csharp"


def test_pure_js_without_build_script_is_unsupported_not_skippable(tmp_path):
    """无 build script 且无 tsconfig 的 Node → 有构建面（package.json）却无编译闸。

    旧行为返 None 被当"合理跳过"（代码注释还写着"与强制成功有本质区别"——那句对，但
    它落进了同一个 None，于是 `passed` 照样 True）。
    """
    (tmp_path / "package.json").write_text('{"name":"demo"}', encoding="utf-8")
    cmd, reason = detect_build_surface(str(tmp_path))
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
        _cmd, reason = detect_build_surface(str(d))
        assert reason != NO_BUILD_SURFACE, (
            f"{key}: 根清单 {spec.root_manifests[0]} 在场却判无构建面 → 会静默判 PASS")


# ══════════════════════════════════════════════
# ② 接线：L2 真的产 issue（不只是三态函数对）
# ══════════════════════════════════════════════

def test_l2_appends_an_issue_and_a_machine_readable_key(tmp_path, monkeypatch):
    """★接线事实★ 三态函数判对 ≠ L2 真的拒绝放行。这条直接跑 L2 工作树段。

    判据（本仓四条硬检查②）：把 `elif _surface_reason.startswith(UNSUPPORTED_STACK)`
    整块删掉，这条必须红。
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
    assert any("编译闸未实现" in i for i in issues), f"未产 issue → passed 不会翻 False：{issues}"
    assert passed is False, "有构建面却零编译验证，绝不能判 PASS（V-C1 本尊）"


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

def test_auto_accept_gives_honest_attribution_not_l2_failed():
    """★如实归因★ "我们没验" 绝不能伪装成 "你的代码编译失败"。

    沿用 `clarify_blocked_by_facts` 的先例（那条注释写明：归因错误会污染 L5 错题、
    让人按不存在的 L2 失败去查）。本条锁死拒因里带栈名与"未实现"字样。
    """
    state = {
        "l2_passed": False,
        "l2_details": {"integration_review": {"compile_gate_unsupported_stack": "php"},
                       "issues": ["L2 集成编译闸未实现（栈=php）"]},
    }
    allow, reason = can_auto_accept_delivery(state)
    assert allow is False
    assert reason.startswith("verification_unsupported_stack:php"), reason
    assert "未实现" in reason
    assert not reason.startswith("l2_failed"), "归因错误：把'闸缺失'说成'集成验证未通过'"


def test_auto_accept_still_says_l2_failed_for_a_real_compile_failure():
    """对照臂：真编译失败仍归因 `l2_failed`（别把所有 L2 失败都改口）。"""
    state = {"l2_passed": False,
             "l2_details": {"integration_review": {"compile_ok": False},
                            "issues": ["L2.1 集成编译失败: error: cannot find symbol"]}}
    allow, reason = can_auto_accept_delivery(state)
    assert allow is False
    assert reason.startswith("l2_failed"), reason


def test_auto_accept_reads_both_l2_details_shapes():
    """`l2_details` 有两种形状（含/不含 `integration_review` 包层）——两层都得查。

    只查一层就是"接线覆盖 ≠ 机制存在"：早退分支写的是扁平形状，那条路径会静默漏掉。
    """
    flat = {"l2_passed": False,
            "l2_details": {"compile_gate_unsupported_stack": "ruby"}}
    allow, reason = can_auto_accept_delivery(flat)
    assert allow is False and reason.startswith("verification_unsupported_stack:ruby"), reason
