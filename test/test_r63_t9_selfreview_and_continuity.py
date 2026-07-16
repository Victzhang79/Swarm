#!/usr/bin/env python3
"""R63-T9：LLM 自检默认关闭（advisory 空烧）+ fix 轮 turn 连续性。

round63 实锤（register T9，RC7/T-D1/D2）：
  ①（自检空烧）21/34 幻觉 PASS 全被确定性闸拦下——自评结论【从不影响 verdict】
    （run_l1_pipeline L1.4 无论自检结论恒 return True；evaluate_l1 的 llm_ok 来自
    pipeline 确定性返回值而非 self_review），但每个通过的子任务仍烧 1 次 worker LLM
    调用产一份假 ✅ 清单。治本＝默认关闭，env 显式 opt-in。
  ②（turn 连续性）fix 循环每轮 _run_agent 都是全新单条 human 消息，模型看不到自己
    上一轮的改动与推理，确定性 build 错只能跨 turn 经新 human 消息回喂 → 同一
    cannot find symbol 反复重探（st-8 撞 95 迭代）。治本＝上一轮代码 agent 对话
    （结构保持裁剪后）延续进本轮 ainvoke，把 build 错回喂进【同一对话】。

调查定案（本文件不测、register 记录）：「compile 已失败就短路自评」已由既有控制流
覆盖——L1.2/L1.2.1 失败在 L1.4 之前 return False；verify agent 步由 C5（det≠None）
+ T8（BLOCKED）短路。T9 不再动那些路径。
"""
from __future__ import annotations

import importlib.util
import logging
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ════════════════════════════════════════════════════════════════
# ① 自检默认关闭（advisory 空烧）
# ════════════════════════════════════════════════════════════════

def test_l1_self_review_enabled_default_off(monkeypatch):
    """默认（无 env）自检关闭；显式 true/1/yes 开启；false 关闭。"""
    from swarm.worker.l1_pipeline import l1_self_review_enabled

    monkeypatch.delenv("SWARM_WORKER_L1_SELF_REVIEW", raising=False)
    assert l1_self_review_enabled() is False, \
        "R63-T9①：自检结论从不影响 verdict，默认必须关闭（纯烧 token）"
    for v in ("true", "1", "yes", "True"):
        monkeypatch.setenv("SWARM_WORKER_L1_SELF_REVIEW", v)
        assert l1_self_review_enabled() is True, f"opt-in {v!r} 应开启"
    for v in ("false", "0", "no", ""):
        monkeypatch.setenv("SWARM_WORKER_L1_SELF_REVIEW", v)
        assert l1_self_review_enabled() is False, f"{v!r} 应关闭"


def _make_subtask():
    from swarm.types import FileScope, SubTask, SubTaskDifficulty

    return SubTask(
        id="sub-t9",
        description="t9 subtask",
        difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=["hello.py"], readable=["hello.py"]),
    )


_SIMPLE_DIFF = "--- a/hello.py\n+++ b/hello.py\n@@ -1 +1 @@\n-old\n+new\n"


def test_l1_4_self_review_not_run_by_default(monkeypatch):
    """★T9① 主锁★ 默认关闭：即使调用方传了 llm，L1.4 也不烧自检调用，
    details 显式标 disabled（fail-open 可观测，不是静默消失）。"""
    import swarm.worker.l1_pipeline as l1p

    monkeypatch.delenv("SWARM_WORKER_L1_SELF_REVIEW", raising=False)
    calls: list = []
    monkeypatch.setattr(l1p, "_run_self_review",
                        lambda *a, **k: calls.append(1) or {"passed": True})
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "hello.py").write_text("x = 1\n", encoding="utf-8")
        ok, details = l1p.run_l1_pipeline(tmp, _make_subtask(), _SIMPLE_DIFF,
                                          llm=object())
    assert ok is True, f"确定性面全过应 PASS: {details}"
    assert calls == [], "默认关闭时绝不许烧自检 LLM 调用"
    assert details["self_review"]["status"] == "disabled", \
        f"关闭必须可观测: {details.get('self_review')}"


def test_l1_4_self_review_opt_in_still_works(monkeypatch):
    """opt-in（env=true）保留旧行为：自检执行且结论进 details（advisory）。"""
    import swarm.worker.l1_pipeline as l1p

    monkeypatch.setenv("SWARM_WORKER_L1_SELF_REVIEW", "true")
    calls: list = []

    def _fake_review(llm, subtask, diff, timeout=60):
        calls.append(1)
        return {"passed": False, "issues": ["potential bug"]}

    monkeypatch.setattr(l1p, "_run_self_review", _fake_review)
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "hello.py").write_text("x = 1\n", encoding="utf-8")
        ok, details = l1p.run_l1_pipeline(tmp, _make_subtask(), _SIMPLE_DIFF,
                                          llm=object())
    assert ok is True, "自检恒 advisory：passed=False 也不阻断（旧语义锁）"
    assert calls == [1], "opt-in 时自检应执行"
    assert details["self_review"]["passed"] is False
    assert "note" in details["self_review"]


def test_phase4_self_review_llm_default_none(monkeypatch):
    """★T9① 接线锁★ 默认关闭时 Phase-4 连 ModelRouter 都不碰（不取 LLM 句柄）。"""
    import swarm.models.router as router_mod
    from swarm.worker.executor import WorkerExecutor

    monkeypatch.delenv("SWARM_WORKER_L1_SELF_REVIEW", raising=False)

    class _MustNotInstantiate:
        def __init__(self):
            raise AssertionError("默认关闭时不许实例化 ModelRouter 取自检 LLM")

    monkeypatch.setattr(router_mod, "ModelRouter", _MustNotInstantiate)
    ex = object.__new__(WorkerExecutor)
    ex._log = lambda m: None
    assert ex._self_review_llm() is None


def test_phase4_self_review_llm_opt_in_fetch_and_failopen(monkeypatch):
    """opt-in 时取 LLM；取失败 → None + 日志可观测（fail-open 不炸 Phase-4）。"""
    import swarm.models.router as router_mod
    from swarm.worker.executor import WorkerExecutor

    monkeypatch.setenv("SWARM_WORKER_L1_SELF_REVIEW", "true")
    marker = object()

    class _Router:
        def get_worker_llm(self, strategy="cost_optimized"):
            return marker

    monkeypatch.setattr(router_mod, "ModelRouter", _Router)
    ex = object.__new__(WorkerExecutor)
    logs: list[str] = []
    ex._log = logs.append
    assert ex._self_review_llm() is marker

    class _BoomRouter:
        def __init__(self):
            raise RuntimeError("router down")

    monkeypatch.setattr(router_mod, "ModelRouter", _BoomRouter)
    assert ex._self_review_llm() is None
    assert any("自检 LLM 获取失败" in m for m in logs), "fail-open 必须可观测"


# ════════════════════════════════════════════════════════════════
# ② turn 连续性：trim_carry_messages（纯函数）
# ════════════════════════════════════════════════════════════════

def _tc(name: str, args: dict, cid: str) -> dict:
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


def test_trim_noop_under_budget_and_no_mutation():
    """预算内：序列结构原样保留；长 tool 内容在【副本】上裁剪，原件绝不改。"""
    from swarm.worker.turn_continuity import trim_carry_messages

    long_tool = "文件内容" * 500  # 2000 字符 > tool_keep 默认 800
    original = [
        HumanMessage(content="修复编译错"),
        AIMessage(content="我来读文件", tool_calls=[_tc("read_file", {"p": "A.java"}, "c1")]),
        ToolMessage(content=long_tool, tool_call_id="c1"),
        AIMessage(content="改完了"),
    ]
    out = trim_carry_messages(original)
    assert out is not None and len(out) == 4
    assert [type(m) for m in out] == [HumanMessage, AIMessage, ToolMessage, AIMessage]
    assert len(out[2].content) < len(long_tool) and "裁剪" in out[2].content
    assert original[2].content == long_tool, "绝不原地修改传入消息（上游还握着引用）"
    assert out[1].tool_calls and out[1].tool_calls[0]["id"] == "c1", \
        "tool_calls 配对结构必须完整保留"


def test_trim_drop_oldest_group_first_message_human():
    """超预算：从最旧组整组丢弃（AI+其 tool 结果成组），最新组保留；
    裁掉开头后首消息必须还是 human（占位 stub 顶位，严格服务器拒 AI 开头）。"""
    from swarm.worker.turn_continuity import trim_carry_messages

    msgs = [
        HumanMessage(content="旧任务提示" + "x" * 3000),
        AIMessage(content="", tool_calls=[_tc("read_file", {"p": "A"}, "c1")]),
        ToolMessage(content="旧" * 400, tool_call_id="c1"),
        AIMessage(content="", tool_calls=[_tc("write_file", {"p": "B"}, "c2")]),
        ToolMessage(content="新" * 400, tool_call_id="c2"),
        AIMessage(content="最终修改说明"),
    ]
    out = trim_carry_messages(msgs, budget_chars=2200, human_keep_chars=2400)
    assert out is not None
    assert isinstance(out[0], HumanMessage), "首消息必须是 human"
    texts = [getattr(m, "content", "") for m in out]
    assert any("最终修改说明" in t for t in texts), "最新组必须保留"
    # 被整组丢弃的旧 tool 组不得残留孤儿 tool 消息
    for m in out:
        if isinstance(m, ToolMessage):
            ids = [tc["id"] for mm in out if isinstance(mm, AIMessage)
                   for tc in (mm.tool_calls or [])]
            assert m.tool_call_id in ids, f"孤儿 tool 消息 {m.tool_call_id}（配对被拆散）"


def test_trim_orphan_tool_and_system_dropped():
    """开头孤儿 tool（配不上 AI）与 System 消息一律剔除（agent 自带 system prompt）。"""
    from swarm.worker.turn_continuity import trim_carry_messages

    msgs = [
        SystemMessage(content="system prompt"),
        ToolMessage(content="孤儿", tool_call_id="ghost"),
        HumanMessage(content="任务"),
        AIMessage(content="回答"),
    ]
    out = trim_carry_messages(msgs)
    assert out is not None
    assert not any(isinstance(m, (SystemMessage, ToolMessage)) for m in out)


def test_trim_giant_single_group_gives_up():
    """仅剩一组仍超预算（如 write_file tool_call args 巨大——args 不可截断，截了
    JSON 就废）→ 放弃 carry 返回 None，回退全新单消息轮（宁缺勿滥）。"""
    from swarm.worker.turn_continuity import trim_carry_messages

    msgs = [
        AIMessage(content="", tool_calls=[
            _tc("write_file", {"content": "y" * 60000}, "c1")]),
        ToolMessage(content="ok", tool_call_id="c1"),
    ]
    assert trim_carry_messages(msgs, budget_chars=24000) is None


def test_trim_empty_returns_none():
    from swarm.worker.turn_continuity import trim_carry_messages

    assert trim_carry_messages([]) is None
    assert trim_carry_messages(None) is None


# ════════════════════════════════════════════════════════════════
# ② turn 连续性：_run_agent 接线
# ════════════════════════════════════════════════════════════════

def test_is_continuity_step():
    """只有产码步（code / code-batch-N / fix-N）参与对话延续；verify/locate/produce
    步的对话不做 carry 源（框架不同：验证叙事延续进修复轮只添噪）。"""
    from swarm.worker.executor_agent import _is_continuity_step

    for s in ("code", "code-batch-1", "code-batch-12", "fix-0", "fix-2"):
        assert _is_continuity_step(s) is True, s
    for s in ("verify-0", "locate", "produce", "react", "trivial", "fixup"):
        assert _is_continuity_step(s) is False, s


class _CaptureAgent:
    """记录 ainvoke 输入并返回可控 messages 的假 agent。"""

    def __init__(self, extra_msgs=None, exc=None):
        self.calls: list[dict] = []
        self._extra = extra_msgs or [AIMessage(content="done")]
        self._exc = exc

    async def ainvoke(self, payload, config=None):
        self.calls.append(payload)
        if self._exc is not None:
            raise self._exc
        return {"messages": list(payload["messages"]) + list(self._extra)}


def _mk_exec(agent):
    from swarm.worker.executor_agent import _AgentLoopMixin

    class _FakeExec(_AgentLoopMixin):
        def __init__(self):
            self._agent = {"agent": agent}
            self.start_time = time.monotonic()
            self.max_execution_time = 60
            self.max_iterations = 10
            self.subtask = SimpleNamespace(
                id="st-t9", difficulty=SimpleNamespace(value="medium"))
            self.project_id = "p"
            self.task_id = "t"
            self.phase = SimpleNamespace(value="verifying")
            self.telemetry_batches: list = []

        def _log(self, _m):
            pass

        def _record_tool_telemetry(self, messages, step):
            self.telemetry_batches.append((step, list(messages)))

    return _FakeExec()


@pytest.mark.asyncio
async def test_run_agent_carries_prior_messages():
    """★T9② 主锁★ continue_messages 前置进 ainvoke 输入，新 human 缀尾；
    telemetry 只记本次新增（不重复计前轮工具调用）；成功后 carry 源更新。"""
    agent = _CaptureAgent()
    ex = _mk_exec(agent)
    carried = [HumanMessage(content="前轮提示"), AIMessage(content="前轮修改")]
    out = await ex._run_agent("修复 cannot find symbol", step="fix-1",
                              continue_messages=carried)
    assert "done" in out
    sent = agent.calls[0]["messages"]
    assert sent[0] is carried[0] and sent[1] is carried[1], "carry 必须前置"
    assert sent[2][0] == "human" and "修复 cannot find symbol" in sent[2][1], \
        "新修复提示必须作为最后一条 human 缀尾（同一对话回喂 build 错）"
    # telemetry 只吃新增切片：新 human + 新 AI，绝不含 carried 两条
    step, batch = ex.telemetry_batches[0]
    assert step == "fix-1" and len(batch) == 2, \
        f"telemetry 应只记新增 2 条（吃到 {len(batch)} 条=重复计数前轮工具）"
    # 成功轮更新 carry 源为全量对话（供下一轮裁剪使用）
    assert ex._continuity_messages is not None
    assert len(ex._continuity_messages) == 4


@pytest.mark.asyncio
async def test_run_agent_no_carry_source_for_verify_step():
    """非产码步（verify）不写 carry 源，也不清掉已有的产码 carry。"""
    agent = _CaptureAgent()
    ex = _mk_exec(agent)
    sentinel = [HumanMessage(content="code 轮对话")]
    ex._continuity_messages = sentinel
    await ex._run_agent("验证", step="verify-0")
    assert ex._continuity_messages is sentinel, \
        "verify 步不得覆盖/清空产码轮的 carry 源"


@pytest.mark.asyncio
async def test_run_agent_failure_clears_continuity():
    """产码步异常（如撞迭代上限）→ carry 源清空：失败轮没有可信对话可延续，
    下一轮回退全新单消息（stale 历史比没有历史更危险）。"""
    agent = _CaptureAgent(exc=RuntimeError("GraphRecursionError: limit"))
    ex = _mk_exec(agent)
    ex._continuity_messages = [HumanMessage(content="stale")]
    out = await ex._run_agent("hi", step="fix-0")
    assert "迭代上限" in out, "撞上限仍优雅返回（老行为不破）"
    assert ex._continuity_messages is None


@pytest.mark.asyncio
async def test_run_agent_without_carry_unchanged():
    """对照：不传 continue_messages 时输入仍是单条 human（旧行为零回归）。"""
    agent = _CaptureAgent()
    ex = _mk_exec(agent)
    await ex._run_agent("hello", step="code")
    sent = agent.calls[0]["messages"]
    assert len(sent) == 1 and sent[0][0] == "human"


# ════════════════════════════════════════════════════════════════
# ② turn 连续性：_fix_carry_messages（env 闸 + fail-open）
# ════════════════════════════════════════════════════════════════

def test_fix_carry_env_kill_switch(monkeypatch):
    agent = _CaptureAgent()
    ex = _mk_exec(agent)
    ex._continuity_messages = [HumanMessage(content="h"), AIMessage(content="a")]
    monkeypatch.delenv("SWARM_WORKER_FIX_TURN_CONTINUITY", raising=False)
    assert ex._fix_carry_messages(), "默认开启（这是 T9 治本本体）"
    monkeypatch.setenv("SWARM_WORKER_FIX_TURN_CONTINUITY", "false")
    assert ex._fix_carry_messages() is None, "kill-switch 必须生效"


def test_fix_carry_no_source_returns_none(monkeypatch):
    agent = _CaptureAgent()
    ex = _mk_exec(agent)
    monkeypatch.delenv("SWARM_WORKER_FIX_TURN_CONTINUITY", raising=False)
    ex._continuity_messages = None
    assert ex._fix_carry_messages() is None


def test_fix_carry_trim_failure_fails_open(monkeypatch, caplog):
    """裁剪自身异常 → fail-open 回退全新单消息轮 + WARNING 可观测（铁律）。"""
    import swarm.worker.turn_continuity as tc

    agent = _CaptureAgent()
    ex = _mk_exec(agent)
    ex._continuity_messages = [HumanMessage(content="h")]
    monkeypatch.delenv("SWARM_WORKER_FIX_TURN_CONTINUITY", raising=False)

    def _boom(*a, **k):
        raise ValueError("trim internal bug")

    monkeypatch.setattr(tc, "trim_carry_messages", _boom)
    with caplog.at_level(logging.WARNING, logger="swarm.worker.executor_agent"):
        assert ex._fix_carry_messages() is None
    assert any("turn 连续性" in r.message and "fail-open" in r.message
               for r in caplog.records), "fail-open 必须 WARNING 可观测，不许静默"


def test_fix_carry_budget_env(monkeypatch):
    """SWARM_WORKER_FIX_CARRY_BUDGET_CHARS 生效：小预算裁到只剩最新组。"""
    agent = _CaptureAgent()
    ex = _mk_exec(agent)
    ex._continuity_messages = [
        HumanMessage(content="旧" * 900),
        AIMessage(content="旧答" * 450),
        HumanMessage(content="新提示"),
        AIMessage(content="最新说明"),
    ]
    monkeypatch.delenv("SWARM_WORKER_FIX_TURN_CONTINUITY", raising=False)
    monkeypatch.setenv("SWARM_WORKER_FIX_CARRY_BUDGET_CHARS", "300")
    out = ex._fix_carry_messages()
    assert out is not None
    texts = "".join(getattr(m, "content", "") for m in out)
    assert "最新说明" in texts
    assert sum(len(getattr(m, "content", "")) for m in out) <= 300 + 200, \
        "预算旋钮必须生效（允许 stub 顶位的小额超出）"


# ════════════════════════════════════════════════════════════════
# ② turn 连续性：verify 循环接线（fix 步真的带 carry）
# ════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_verify_loop_fix_round_receives_carry(monkeypatch):
    """★T9② 接线锁★ _phase_verify_loop 的 fix 步必须带 continue_messages
    （来自 _fix_carry_messages），把确定性 build 错回喂进同一对话。"""
    from swarm.worker.executor import WorkerExecutor, WorkerPhase

    monkeypatch.delenv("SWARM_WORKER_FIX_TURN_CONTINUITY", raising=False)
    monkeypatch.setenv("SWARM_WORKER_VERIFY_AGENT_STEP", "auto")

    ex = object.__new__(WorkerExecutor)
    ex.phase = WorkerPhase.VERIFYING
    ex.max_fix_rounds = 1
    ex.fix_rounds = 0
    ex._sandbox = None
    ex._sandbox_manager = None
    ex.start_time = None  # 跳过 P7 时间 bail
    ex._last_fail_sig = None
    ex._same_fail_streak = 0
    ex._continuity_messages = [HumanMessage(content="code 轮"),
                               AIMessage(content="我写了 AlarmTask.java")]
    ex._log = lambda m: None
    ex._check_timeout = lambda: False
    _sigs = iter(["sig-a", "sig-b", "sig-c"])
    ex._failure_signature = lambda d: next(_sigs)

    async def _det_gate():
        return (False, {"compile_errors": "cannot find symbol: AlarmTaskDTO"})
    ex._deterministic_l1_gate = lambda: (
        False, {"compile_errors": "cannot find symbol: AlarmTaskDTO"})

    async def _hint(vr, det):
        return ""
    ex._symbol_grounding_hint = _hint
    ex._build_fix_prompt = lambda vr, det, hint: "修复它"

    captured: list[dict] = []

    async def _fake_run_agent(prompt, *, step="react", max_steps=None,
                              continue_messages=None):
        captured.append({"step": step, "carry": continue_messages})
        return "已修复"
    ex._run_agent = _fake_run_agent

    await ex._phase_verify_loop()
    fix_calls = [c for c in captured if c["step"].startswith("fix-")]
    assert fix_calls, "应至少跑一轮 fix"
    carry = fix_calls[0]["carry"]
    assert carry, "fix 步必须带上一产码轮对话（同一对话回喂 build 错）"
    assert any("AlarmTask.java" in getattr(m, "content", "") for m in carry)


# ════════════════════════════════════════════════════════════════
# 对抗复核整改锁（猎手 F1/F2/F3 + 复核 R-MED）
# ════════════════════════════════════════════════════════════════

def test_fix_carry_no_source_logs_observably(monkeypatch, caplog):
    """★猎手 F1 锁★ 无 carry 源（首轮或上一产码轮超时/撞上限被清）必须留日志——
    操作员要能验证 T9 在病理 fix 循环上是否真的接管了。"""
    agent = _CaptureAgent()
    ex = _mk_exec(agent)
    ex._continuity_messages = None
    monkeypatch.delenv("SWARM_WORKER_FIX_TURN_CONTINUITY", raising=False)
    with caplog.at_level(logging.INFO, logger="swarm.worker.executor_agent"):
        assert ex._fix_carry_messages() is None
    assert any("无可延续 carry 源" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_recursion_cap_logs_carry_break(monkeypatch):
    """★猎手 F1 锁★ 产码步撞迭代上限（T9 要治的病理场景本体）时，carry 断链
    必须在执行日志可见，与"从未有 carry"可区分。"""
    agent = _CaptureAgent(exc=RuntimeError("GraphRecursionError: limit"))
    ex = _mk_exec(agent)
    logs: list[str] = []
    ex._log = logs.append
    await ex._run_agent("hi", step="fix-1")
    assert any("carry 源已清空" in m for m in logs), \
        f"撞上限的 carry 断链必须可观测: {logs}"


def test_fix_carry_bad_budget_env_warns(monkeypatch, caplog):
    """★猎手 F2 锁★ 预算 env 坏值回退默认必须 WARNING（fail-open 可观测铁律）。"""
    agent = _CaptureAgent()
    ex = _mk_exec(agent)
    ex._continuity_messages = [HumanMessage(content="h"), AIMessage(content="a")]
    monkeypatch.delenv("SWARM_WORKER_FIX_TURN_CONTINUITY", raising=False)
    monkeypatch.setenv("SWARM_WORKER_FIX_CARRY_BUDGET_CHARS", "24k")
    with caplog.at_level(logging.WARNING, logger="swarm.worker.executor_agent"):
        out = ex._fix_carry_messages()
    assert out, "坏值回退默认预算后仍应正常 carry"
    assert any("不是整数" in r.message for r in caplog.records)


def test_deadline_exhausted_reason_not_clobbered(monkeypatch):
    """★猎手 F3 锁（既有 clobber）★ opt-in 自检 + deadline 在 test 阶段之后、L1.4
    之前耗尽（真实场景：test 命令吃光预算）→ self_review 必须保留
    worker_deadline_exhausted 原因，不许被通用 disabled 文案覆盖误导去查 env。

    确定性驱动：假时钟 + monkeypatch _run_l1_command 在 test 命令执行点拨快时钟
    （entry/compile/test 三道 BLOCKED 前置检查都在拨快之前，全部放行）。"""
    import time as _real_time

    import swarm.worker.l1_pipeline as l1p
    from swarm.types import TaskHarness

    monkeypatch.setenv("SWARM_WORKER_L1_SELF_REVIEW", "true")

    class _FakeTime:
        offset = 0.0

        def __getattr__(self, name):
            return getattr(_real_time, name)

        def monotonic(self):
            return _real_time.monotonic() + self.offset

    fake = _FakeTime()
    monkeypatch.setattr(l1p, "_time", fake)

    def _fake_run_cmd(cmd, cwd, timeout=300):
        fake.offset = 10**7  # test 命令"吃光预算"——此后 L1.4 检查看到已过期
        return 0, "1 passing"

    monkeypatch.setattr(l1p, "_run_l1_command", _fake_run_cmd)

    subtask = _make_subtask()
    subtask.harness = TaskHarness(test_command="echo ok")
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "hello.py").write_text("x = 1\n", encoding="utf-8")
        ok, details = l1p.run_l1_pipeline(
            tmp, subtask, _SIMPLE_DIFF, llm=object(),
            deadline=_real_time.monotonic() + 3600)
    assert ok is True, f"确定性面全过应 PASS: {details}"
    assert details["self_review"].get("reason") == "worker_deadline_exhausted", \
        f"deadline 原因被覆盖会误导操作员去查 env: {details['self_review']}"


def test_trim_strips_reasoning_bulk_from_copy():
    """★复核 R-MED 锁★ 携带副本剥离 additional_kwargs 里的 reasoning 大块
    （已核实 wire 不回发；剥副本让预算账严格成立），原件绝不动。"""
    from swarm.worker.turn_continuity import trim_carry_messages

    reasoning = "思考" * 20000
    original = [
        HumanMessage(content="任务"),
        AIMessage(content="结论",
                  additional_kwargs={"reasoning_content": reasoning,
                                     "other_key": "keep"}),
    ]
    out = trim_carry_messages(original, budget_chars=24000)
    assert out is not None and len(out) == 2
    assert "reasoning_content" not in out[1].additional_kwargs
    assert out[1].additional_kwargs.get("other_key") == "keep", "无关键不许误伤"
    assert original[1].additional_kwargs["reasoning_content"] == reasoning, \
        "原件绝不原地修改"


# ════════════════════════════════════════════════════════════════
# 冻结面
# ════════════════════════════════════════════════════════════════

def test_env_registry_has_t9_envs():
    from swarm.config.env_registry import REGISTERED_ENVS

    assert "SWARM_WORKER_FIX_TURN_CONTINUITY" in REGISTERED_ENVS
    assert "SWARM_WORKER_FIX_CARRY_BUDGET_CHARS" in REGISTERED_ENVS
