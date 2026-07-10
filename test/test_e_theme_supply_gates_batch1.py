#!/usr/bin/env python3
"""主题E 批1（round38c）—— E1 retry_alternate 真兑现 + E6③ 密钥闸阈值接线 + E7① 路径归一。

取证（forensics 主题E + 亲核）：
- E1：dispatch 旧判据 len(_pool)==1 把「池长」当「无备选」——.env 池 1 模型但 difficulty
  fallback 链有异构备选（Saka/MiniMax/Step），retry_alternate 被静默改写同模型+boost，
  failure 侧「换备选」日志 9 次全空转（register #26）。
- E6③：MERGE 密钥闸丢弃 scan_diff_for_secrets 返回的 should_block、硬编码 CRITICAL；
  settings.security_block_severity 开关存在但只被 AUDIT 消费——配 high 无效。
- E7①：修复族路径三形态（裸相对/./ 前缀/沙箱绝对），登记只剥 ./，git diff targets 完全
  不归一——一条 /workspace/... 混入 `git diff -- <targets>` 即 rc=128 连坐整个 diff 回退
  difflib，repaired/兄弟改动从交付蒸发（D36 兄弟回传失败真身之一）。
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.types import (  # noqa: E402
    Confidence,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
    WorkerOutput,
)


# ══════════════ E1 ══════════════

def test_e1_router_has_alternate_judged_by_route_table(monkeypatch):
    from swarm.models.router import ModelRouter
    monkeypatch.setattr(ModelRouter, "_resolve_route",
                        lambda self, d, m: ("primary-m", ["primary-m", "alt-b"]))
    assert ModelRouter().has_alternate_for_subtask("medium") is True
    monkeypatch.setattr(ModelRouter, "_resolve_route",
                        lambda self, d, m: ("primary-m", ["primary-m"]))
    assert ModelRouter().has_alternate_for_subtask("medium") is False, (
        "fallback 链全=primary 即单模型模式（RUN10 诉求由配置表达）")


async def _drive_dispatch(monkeypatch, has_alt: bool):
    """池=1 模型 + subtask_use_alternate 标记，驱动 dispatch 并捕获 _ua 与 boost。"""
    import importlib
    dmod = importlib.import_module("swarm.brain.nodes.dispatch")
    monkeypatch.setattr(dmod, "_has_hetero_alternate", lambda d: has_alt)
    from swarm.config.settings import get_config
    _cfg = get_config()
    monkeypatch.setattr(_cfg.worker, "worker_parallel_pool", ["only-model"], raising=False)

    plan = TaskPlan(subtasks=[
        SubTask(id="st-fail", description="失败重试", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=["b.py"], readable=[]), depends_on=[]),
    ], parallel_groups=[["st-fail"]])
    seen: dict = {}

    async def fake_worker(subtask, knowledge_context, project_id="", task_id="", **kw):
        seen["use_alternate"] = kw.get("use_alternate")
        seen["recursion_boost"] = kw.get("recursion_boost")
        return WorkerOutput(subtask_id=subtask.id, diff="+x\n", summary="",
                            l1_passed=True, confidence=Confidence.HIGH)

    state = {
        "task_id": "t1", "project_id": "p1", "plan": plan,
        "subtask_results": {}, "dispatch_remaining": ["st-fail"],
        "failed_subtask_ids": [], "knowledge_context": {},
        "subtask_use_alternate": {"st-fail": True},
        "subtask_retry_counts": {"st-fail": 1},
    }
    with patch("swarm.brain.nodes._dispatch_to_worker", side_effect=fake_worker):
        await dmod.dispatch(state)
    return seen


async def test_e1_single_pool_with_hetero_fallback_honors_alternate(monkeypatch):
    seen = await _drive_dispatch(monkeypatch, has_alt=True)
    assert seen.get("use_alternate") is True, (
        "池 1 模型但 fallback 链有异构备选时 retry_alternate 必须真兑现——旧判据"
        "len(_pool)==1 静默改写同模型（register #26：换备日志与实派模型永久不符）")


async def test_e1_no_hetero_fallback_stays_with_boost(monkeypatch):
    seen = await _drive_dispatch(monkeypatch, has_alt=False)
    assert seen.get("use_alternate") in (False, None), "无异构备选回退单模型路径"
    assert seen.get("recursion_boost") == 30, (
        "无备选时保留 RUN10 语义：同模型 + recursion_boost 助收敛")


# ══════════════ E2 ══════════════

def test_e2_worker_llm_carries_stream_wallclock(monkeypatch):
    """E2（register #32）：worker 单流总墙钟必须传进 LLM 构造——超时抛 Transient
    让 with_fallbacks 本步切备，治「单调用挂满 900s 总预算、整 agent 被 cancel」。"""
    from unittest.mock import MagicMock
    from swarm.models.router import EndpointProvider, ModelRouter
    captured: list[dict] = []
    _orig = EndpointProvider.get_chat_model

    def spy(self, model_name, temperature=0.2, **kw):
        captured.append({"model": model_name, **kw})
        return MagicMock()

    monkeypatch.setattr(EndpointProvider, "get_chat_model", spy)
    monkeypatch.setattr(ModelRouter, "_resolve_route",
                        lambda self, d, m="text": ("m-a", ["m-a", "m-b"]))
    monkeypatch.setattr(ModelRouter, "_assemble_worker_chain",
                        lambda self, named: MagicMock())
    r = ModelRouter()
    monkeypatch.setattr(r.config, "worker_stream_wallclock_s", 420.0, raising=False)

    r.get_llm_for_subtask(difficulty="medium")
    r.get_llm_by_name("m-x", difficulty="medium")
    r.get_alternate_llm_for_subtask("medium")
    assert captured, "spy 未捕获任何构造"
    assert all(c.get("wallclock_budget") == 420.0 for c in captured), (
        f"worker 全部 LLM 构造点必须带 wallclock_budget（管道 router 早已有、"
        f"此前 worker 恒 0=关闭）: {captured}")


def test_e2_wallclock_zero_disables(monkeypatch):
    from unittest.mock import MagicMock
    from swarm.models.router import EndpointProvider, ModelRouter
    captured: list[dict] = []

    def spy(self, model_name, temperature=0.2, **kw):
        captured.append(kw)
        return MagicMock()

    monkeypatch.setattr(EndpointProvider, "get_chat_model", spy)
    monkeypatch.setattr(ModelRouter, "_resolve_route",
                        lambda self, d, m="text": ("m-a", ["m-a"]))
    monkeypatch.setattr(ModelRouter, "_assemble_worker_chain",
                        lambda self, named: MagicMock())
    r = ModelRouter()
    monkeypatch.setattr(r.config, "worker_stream_wallclock_s", 0.0, raising=False)
    r.get_llm_for_subtask(difficulty="medium")
    assert all(not c.get("wallclock_budget") for c in captured), "0=关闭（回退旧行为）"


# ══════════════ E6③ ══════════════

_SLACK_TOK = "xoxb-1234567890-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"


def _high_secret_diff() -> str:
    return (
        "diff --git a/conf/app.py b/conf/app.py\n"
        "--- a/conf/app.py\n+++ b/conf/app.py\n@@ -1,1 +1,2 @@\n"
        " x = 1\n"
        f'+SLACK = "{_SLACK_TOK}"\n'
    )


def test_e6_high_secret_blocks_when_threshold_high(monkeypatch):
    from swarm.brain.nodes import _scan_merged_diff_for_secrets
    from swarm.config.settings import get_config
    monkeypatch.setattr(get_config().worker, "security_block_severity", "high",
                        raising=False)
    out: dict = {}
    _scan_merged_diff_for_secrets(out, _high_secret_diff())
    assert out.get("failure_escalated") is True, (
        "security_block_severity=high 时 HIGH 密钥必须 escalate——旧实现丢弃 scan 的"
        "should_block、硬编码 CRITICAL，开关存在但没接到 MERGE 闸")
    assert out.get("verification_failure") == "merge_secret_detected"


def test_e6_high_secret_default_critical_reports_only(monkeypatch):
    from swarm.brain.nodes import _scan_merged_diff_for_secrets
    from swarm.config.settings import get_config
    monkeypatch.setattr(get_config().worker, "security_block_severity", "critical",
                        raising=False)
    out: dict = {}
    _scan_merged_diff_for_secrets(out, _high_secret_diff())
    assert not out.get("failure_escalated"), "默认 critical 阈值行为不变（HIGH 只留痕）"
    assert any(str(r).startswith("merge_secret_reported")
               for r in out.get("degraded_reasons") or []), "HIGH 必须留痕可审计"


# ══════════════ E7① ══════════════

def _executor(tmp_path, writable=None, create=None):
    from swarm.worker.executor import WorkerExecutor
    st = SubTask(id="sub-1", description="t",
                 scope=FileScope(writable=writable or [], create_files=create or []))
    return WorkerExecutor(st, project_path=str(tmp_path), project_id="p1", task_id="t1")


def test_e7_norm_rel_strips_sandbox_workdir_prefix(tmp_path):
    ex = _executor(tmp_path)
    assert ex._norm_rel(tmp_path, "/workspace/mod/pom.xml") == "mod/pom.xml", (
        "沙箱内绝对路径（修复族 sed/grep 产出形态）必须剥 remote_workdir 前缀归一，"
        "退化 basename 是错上加错（撞名错配）")
    assert ex._norm_rel(tmp_path, "mod/pom.xml") == "mod/pom.xml", "相对路径幂等"


def test_e7_record_repaired_paths_normalizes_absolute(tmp_path):
    ex = _executor(tmp_path, writable=["mod/Foo.java"])
    ex._record_repaired_paths({"repaired_file_paths": ["/workspace/pom.xml", "./b.xml"]})
    assert "pom.xml" in ex._repaired_extra_paths, "沙箱绝对路径必须归一后登记"
    assert "b.xml" in ex._repaired_extra_paths
    assert "/workspace/pom.xml" not in ex._repaired_extra_paths


def _git(tmp_path, *args):
    subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                   capture_output=True, text=True)


def test_e7_git_diff_survives_absolute_repaired_path(tmp_path):
    """一条绝对路径混入 targets 时 `git diff -- /workspace/...` rc=128 会连坐整个
    diff 回退 difflib——修后 targets 统一归一，真实 writable 改动绝不蒸发。"""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.py").write_text("old\n", encoding="utf-8")
    (tmp_path / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / "a.py").write_text("new\n", encoding="utf-8")
    (tmp_path / "pom.xml").write_text("<project><x/></project>\n", encoding="utf-8")

    ex = _executor(tmp_path, writable=["a.py"])
    # 模拟修复族登记了沙箱绝对路径（绕过登记点归一直接塞坏形态，验证消费点兜底）
    ex._repaired_extra_paths.add("/workspace/pom.xml")
    diff = ex._try_local_git_diff()
    assert diff is not None and "a.py" in diff, (
        "targets 含绝对路径时整个 git diff 曾 rc=128 → None → 回退 difflib，"
        "scope 内真实改动一并蒸发")
    assert "pom.xml" in diff, "归一后 repaired 文件改动也应纳入 diff"


# ══════════════ E3 ══════════════

def test_e3_data_file_syntax_gate(tmp_path):
    """非编译数据文件（json/yaml/xml）此前 compile_ok 恒 True fall-through=假绿通道。"""
    from swarm.worker.l1_pipeline import _compile_files
    (tmp_path / "bad.json").write_text('{"a": 1,,}', encoding="utf-8")
    ok, msg = _compile_files(str(tmp_path), ["bad.json"])
    assert ok is False and "bad.json" in msg, "坏 json 必须被确定性抓获"
    (tmp_path / "bad.xml").write_text("<project><x></project>", encoding="utf-8")
    ok2, msg2 = _compile_files(str(tmp_path), ["bad.xml"])
    assert ok2 is False and "bad.xml" in msg2, "坏 xml（未闭合标签）必须被抓获"
    (tmp_path / "good.json").write_text('{"a": 1}', encoding="utf-8")
    (tmp_path / "good.xml").write_text("<project/>", encoding="utf-8")
    ok3, _ = _compile_files(str(tmp_path), ["good.json", "good.xml", "note.md"])
    assert ok3 is True, "良构数据文件与无校验器类型（.md）不误杀"


def test_e3_yaml_multidoc_and_jsonc_not_killed(tmp_path):
    """复核 C-1（CONFIRMED）：Spring Boot application.yml 的 `---` 多文档 profile 是
    标准写法，safe_load 单文档必炸=确定性误杀且教模型删合法 `---`；tsconfig 类 JSONC
    合法含注释。两者都不得误杀。"""
    from swarm.worker.l1_pipeline import _compile_files
    (tmp_path / "application.yml").write_text(
        "spring:\n  profiles:\n    active: dev\n---\nspring:\n  config: {}\n",
        encoding="utf-8")
    ok, msg = _compile_files(str(tmp_path), ["application.yml"])
    assert ok is True, f"多文档 yaml 被误杀: {msg}"
    (tmp_path / "tsconfig.json").write_text(
        '{\n  // comment\n  "compilerOptions": {},\n}\n', encoding="utf-8")
    ok2, msg2 = _compile_files(str(tmp_path), ["tsconfig.json"])
    assert ok2 is True, f"JSONC 家族被误杀: {msg2}"
    (tmp_path / "bad.yml").write_text("a: [unclosed\n", encoding="utf-8")
    ok3, _ = _compile_files(str(tmp_path), ["bad.yml"])
    assert ok3 is False, "真坏 yaml 仍要抓"


def test_e6_block_severity_none_reports_only(monkeypatch):
    """复核 C-2（CONFIRMED）：'none'=文档化仅报告模式——scan 的 _severity_gte(any,'none')
    恒真，原样透传会把仅报告反转成逢密钥必 escalate。"""
    from swarm.brain.nodes import _scan_merged_diff_for_secrets
    from swarm.config.settings import get_config
    monkeypatch.setattr(get_config().worker, "security_block_severity", "none",
                        raising=False)
    out: dict = {}
    _scan_merged_diff_for_secrets(out, _high_secret_diff())
    assert not out.get("failure_escalated"), "'none' 必须保持仅报告语义（不阻断）"
    assert out.get("degraded_reasons"), "仅报告模式仍必须留痕"


def test_e6_blocking_never_empty_on_escalate(monkeypatch):
    """复核 C-2 叠加缺陷：escalate 时 degraded_reasons 绝不为空（空留痕砸 observability）。"""
    from swarm.brain.nodes import _scan_merged_diff_for_secrets
    from swarm.config.settings import get_config
    monkeypatch.setattr(get_config().worker, "security_block_severity", "High ",
                        raising=False)  # 带尾空格+大写的非常规配置值
    out: dict = {}
    _scan_merged_diff_for_secrets(out, _high_secret_diff())
    if out.get("failure_escalated"):
        assert out.get("degraded_reasons"), "escalate 却零留痕=可观测性被砸"


def test_e1_alternate_skips_trivial_tier(monkeypatch):
    """复核 C-4（CONFIRMED）：三档 fallback 链统一 trivial 档居首时，「第一个 ≠primary」
    把 medium 重试派到最弱模型（RUN10 顾虑成真）——候选必须排除 trivial 档 primary。"""
    from swarm.models.router import ModelRouter
    monkeypatch.setattr(ModelRouter, "_resolve_route",
                        lambda self, d, m="text": ("m-medium", ["m-trivial", "m-medium", "m-strong"]))
    r = ModelRouter()
    monkeypatch.setattr(r.config, "routing_trivial", "m-trivial", raising=False)
    cands = r._alternate_candidates("medium")
    assert cands == ["m-strong"], (
        f"medium 的 alternate 不得落到 trivial 档（换模型≠降级）: {cands}")
    # trivial 难度本身可用同档其它模型
    cands_t = r._alternate_candidates("trivial")
    assert "m-trivial" in cands_t or cands_t, "trivial 难度不排除自档"


async def test_e1_complex_never_uses_alternate(monkeypatch):
    """复核 C-4：complex 已派最强模型，alternate 必降级且会丢弃 override——禁用并走 boost。"""
    import importlib
    dmod = importlib.import_module("swarm.brain.nodes.dispatch")
    monkeypatch.setattr(dmod, "_has_hetero_alternate", lambda d: True)
    from swarm.config.settings import get_config
    monkeypatch.setattr(get_config().worker, "worker_parallel_pool", ["only-model"],
                        raising=False)
    plan = TaskPlan(subtasks=[
        SubTask(id="st-hard", description="复杂重试", difficulty=SubTaskDifficulty.COMPLEX,
                scope=FileScope(writable=["b.py"], readable=[]), depends_on=[]),
    ], parallel_groups=[["st-hard"]])
    seen: dict = {}

    async def fake_worker(subtask, knowledge_context, project_id="", task_id="", **kw):
        seen["use_alternate"] = kw.get("use_alternate")
        seen["recursion_boost"] = kw.get("recursion_boost")
        return WorkerOutput(subtask_id=subtask.id, diff="+x\n", summary="",
                            l1_passed=True, confidence=Confidence.HIGH)

    state = {
        "task_id": "t1", "project_id": "p1", "plan": plan,
        "subtask_results": {}, "dispatch_remaining": ["st-hard"],
        "failed_subtask_ids": [], "knowledge_context": {},
        "subtask_use_alternate": {"st-hard": True},
        "subtask_retry_counts": {"st-hard": 1},
    }
    with patch("swarm.brain.nodes._dispatch_to_worker", side_effect=fake_worker):
        await dmod.dispatch(state)
    assert seen.get("use_alternate") in (False, None), "complex 禁用 alternate（换必降级）"
    assert seen.get("recursion_boost") == 30, "complex 重试留最强模型+boost 助收敛"


def test_e3_data_file_missing_locally_skipped(tmp_path):
    from swarm.worker.l1_pipeline import _compile_files
    ok, _ = _compile_files(str(tmp_path), ["not_pulled_back.json"])
    assert ok is True, "文件不在本地（沙箱未 pull-back 等）按 infra 口径跳过闸门"


# ══════════════ E6① ══════════════

def _new_java_diff(path: str, pkg: str) -> str:
    return (f"diff --git a/{path} b/{path}\n"
            f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,3 @@\n"
            f"+package {pkg};\n"
            "+\n"
            "+public class Foo {}\n")


def test_e6_package_decl_mismatch_caught():
    from swarm.worker.l1_pipeline import _package_decl_mismatches
    diff = _new_java_diff("mod-a/src/main/java/com/x/alarm/service/Foo.java",
                          "com.x.other.wrong")
    mis = _package_decl_mismatches(diff)
    assert len(mis) == 1 and mis[0]["declared"] == "com.x.other.wrong" \
        and mis[0]["expected"] == "com.x.alarm.service", (
        "新建 java 包声明与路径不符=class 落错包、毒发在下游 import——必须确定性抓获"
        "（producer-gate 不对称：既有机制全在 import 消费侧）")


def test_e6_package_decl_match_and_nonstandard_layout_pass():
    from swarm.worker.l1_pipeline import _package_decl_mismatches
    ok_diff = _new_java_diff("mod-a/src/main/java/com/x/alarm/Foo.java", "com.x.alarm")
    assert _package_decl_mismatches(ok_diff) == []
    # 非常规布局（无 src/main/java 根标记）保守跳过不误杀
    odd = _new_java_diff("scripts/Foo.java", "whatever.pkg")
    assert _package_decl_mismatches(odd) == []
    # 既有文件修改（非新建）不查
    mod_diff = ("diff --git a/m/src/main/java/com/x/A.java b/m/src/main/java/com/x/A.java\n"
                "--- a/m/src/main/java/com/x/A.java\n+++ b/m/src/main/java/com/x/A.java\n"
                "@@ -1,1 +1,2 @@\n package com.y.wrong;\n+// edit\n")
    assert _package_decl_mismatches(mod_diff) == []


# ══════════════ D3c ══════════════

def test_d3c_validate_downgrade_marks_unverified_sources():
    """脚手架 validate 降级（R34-6 故意治法）此前零机读痕迹——「validate PASS」被读作
    「编译 PASS」，scaffold 同批新建 .java 零编译假绿。标记判据必须命中降级形态。"""
    from swarm.worker.l1_pipeline import _validate_downgrade_unverified_sources
    downgraded = "mvn -f ruoyi-alarm/pom.xml validate -q"
    srcs = _validate_downgrade_unverified_sources(
        downgraded, ["ruoyi-alarm/pom.xml", "ruoyi-alarm/src/main/java/com/x/A.java"])
    assert srcs == ["ruoyi-alarm/src/main/java/com/x/A.java"], (
        "降级窗口内的源码文件必须被登记为「本轮未经编译」")
    assert _validate_downgrade_unverified_sources(
        "mvn -pl ruoyi-alarm -am compile -q", ["a/A.java"]) == [], "常规 reactor 不误标"
    assert _validate_downgrade_unverified_sources(downgraded, ["ruoyi-alarm/pom.xml"]) == [], (
        "纯 pom 脚手架无源码=validate 契约内，不标")


# ══════════════ E4 ══════════════

def test_e4_budget_banner_injected():
    from swarm.worker.executor_agent import _budget_banner
    b = _budget_banner(40, 612.3)
    assert "40" in b and "612" in b, "预算数字必须对模型可见（静态劝诫无具体数字）"


# ══════════════ E5 ══════════════

def test_e5_unsplittable_oversized_marked_force_strong(monkeypatch):
    """拆不动的大块此前要白烧 1×900s 超时+数轮重试才被 FINDING-12 补最强模型——
    闸门既然确定拆不动，首轮就标 force_strong。"""
    import importlib
    dmod = importlib.import_module("swarm.brain.nodes.dispatch")
    st = SubTask(id="st-big", description="单文件巨核", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["huge/Core.java"], readable=[]))
    plan = TaskPlan(subtasks=[st], parallel_groups=[["st-big"]])
    import swarm.brain.planning_nodes as pn
    monkeypatch.setattr(pn, "_oversized_by_files", lambda s: s.id == "st-big")
    monkeypatch.setattr(pn, "_split_oversized_by_files", lambda s: [s])  # 拆不动
    fs: dict = {}
    dmod._enforce_dispatch_budget_gate(
        plan, set(), ["st-big"], 4, [st], force_strong_out=fs)
    assert fs.get("st-big") is True, "拆不动的大块必须首轮 force_strong（省 1×900s 白烧）"


if __name__ == "__main__":
    print("run via pytest")
