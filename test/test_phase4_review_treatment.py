"""阶段4.9 对抗双复核治理批：11 条 CONFIRMED 行为锁。

reviewer 10 + hunter 10 条合并去重（互相印证 4 条）：
  T1 C10 intent 闸击穿：str(TaskIntent.DEBUG)=='TaskIntent.DEBUG' 恒 miss——DEBUG/AUDIT
     被静默剥夺考古工具（测试传裸字符串遮蔽了真实枚举路径）。
  T2 C3 过宽：e2b TimeoutException 四语义（unavailable/canceled/deadline_exceeded/unknown）
     全被合成 124+清零熔断计数——半死沙箱永不熔断（熔断缴械）。治=沙箱级死亡标记走
     infra 计数；真·进程超时合成 124 且【中性】（不清零也不计失败）。
  T3 C2 缓存命中绕过 D30/A3 pull-back 完整性闸（读 live 计数器的 fail-closed 防线被
     短路=A3 假绿复活窗）。治=命中条件追加同步计数干净。
  T4 C9 补边只在 2/12 条 return 回写 plan——重启后动态边丢失（CODEWALK 纪律③被禁的
     in-place 变异靠捎带模式）。治=全部可达 return 补条件 emit。
  T5 阶梯三打桩生产者（give_up+桩产出 l1_passed=True）把带依赖边的下游永久扣死——
     _is_ready 先查放弃集后查 completed。治=completed（含桩）优先满足依赖。
  T6 C1 查点留白：compile/lint 段无 _deadline_blocked；test 命令超时未钳。
  T7 deadline BLOCKED 不带 timeout_in_verifying marker——oversize 拆小信号靠 Phase-4
     A5 偶然对齐续命。治=_deadline_blocked 就地 setdefault。
  T8 F-F 过滤在整段 traceback 上子串匹配："502"命中行号/"connect"命中 connectionpool.py
     帧路径→capability 错误误喂 breaker。治=只取首行+数字词边界。
  T9 C5 verify 跳过后 test/verify 失败的 fix 证据为空串（digest 不含 test_output/
     verify_failed）→ 修复 agent 盲修。治=digest 补两分支。
  T10 C11 GEN 死键：中途 invalidate 后 prune 保留旧 GEN 正项=永不命中泄漏。治=prune
      顺带丢非当前 GEN。
"""

from __future__ import annotations

import itertools
from unittest.mock import MagicMock, patch

from swarm.types import (
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskIntent,
    TaskPlan,
)

# ─────────────── T1：intent 枚举自愈 ───────────────


def test_t1_real_enum_intent_keeps_archaeology_tools():
    from swarm.worker.agent import _get_worker_tools
    tools = _get_worker_tools(FileScope(writable=["a.py"], readable=[]),
                              TaskIntent.DEBUG)
    names = {t.name for t in tools}
    assert "git_blame" in names and "git_log" in names, (
        "str(TaskIntent.DEBUG)=='TaskIntent.DEBUG' 恒 miss——生产调用点传的是枚举，"
        "闸门必须对枚举/字符串双输入自愈（.value 优先）")


def test_t1_enum_modify_still_trims():
    from swarm.worker.agent import _get_worker_tools
    names = {t.name for t in _get_worker_tools(
        FileScope(writable=["a.py"], readable=[]), TaskIntent.MODIFY)}
    assert "git_blame" not in names


# ─────────────── T2：C3 超时分型 ───────────────

class _ProcTimeout(Exception):
    """进程级超时（deadline_exceeded 语义——命令真跑了只是慢）。"""


class _SandboxDeadTimeout(Exception):
    """沙箱级超时（unavailable 语义——沙箱本体过期/不可达）。"""


def _run_with_exc(exc):
    from swarm.worker.sandbox import SandboxManager
    pool = SandboxManager.__new__(SandboxManager)
    pool._record_sandbox_failure = MagicMock()
    pool._record_sandbox_success = MagicMock()
    pool.append_activity = MagicMock()
    sandbox = MagicMock()
    sandbox.sandbox_id = "sb-1"
    sandbox.commands.run.side_effect = exc
    return pool, pool.run_command(sandbox, "mvn -q compile", timeout=5)


def test_t2_process_timeout_neutral_not_success():
    pool, cr = _run_with_exc(_ProcTimeout("process timed out after 5s"))
    assert cr.error == "exit_code=124"
    pool._record_sandbox_failure.assert_not_called()
    pool._record_sandbox_success.assert_not_called(), (
        "真·进程超时=中性证据——清零计数会让「infra 失败/超时交替」的真抖动沙箱永远攒不满 5 次")


def test_t2_sandbox_dead_timeout_counts_infra():
    pool, cr = _run_with_exc(_SandboxDeadTimeout(
        "sandbox was not found or timeout: sandbox unavailable"))
    assert cr.error != "exit_code=124", (
        "沙箱本体过期/不可达（unavailable/canceled 语义）不是「命令太慢」——"
        "合成 124+清零=半死沙箱永不熔断，每条命令烧满超时窗慢磨到 worker deadline")
    pool._record_sandbox_failure.assert_called_once()


# ─────────────── T3：C2 缓存命中不得绕过同步完整性闸 ───────────────

_DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"


def _mk_ex():
    from swarm.worker.executor import WorkerExecutor
    st = SubTask(id="st-t3", description="改 a.py",
                 difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["a.py"], readable=[]), intent="modify")
    return WorkerExecutor(subtask=st, project_path="/tmp/swarm-t3")


def test_t3_cache_hit_requires_clean_sync_counters():
    ex = _mk_ex()
    with patch.object(ex, "_check_timeout", return_value=False), \
         patch.object(ex, "_get_git_diff", return_value=_DIFF), \
         patch("swarm.worker.l1_pipeline.run_l1_pipeline",
               return_value=(True, {})) as mock_pipe:
        ex._deterministic_l1_gate()          # PASS→缓存
        ex._sync_error_rels = ["big.bin"]    # Phase-4 同步出现读错误
        det_ok, d2 = ex._deterministic_l1_gate()
    assert mock_pipe.call_count == 2, (
        "同步计数非干净时缓存命中=绕过 A3/D30 fail-closed 闸——"
        "「沙箱绿本地 diff 缺产物」的静默假绿复活")


# ─────────────── T4：C9 补边后 replan 守卫 return 也须回写 plan ───────────────

def _wo_blocked_cap(sid, pkgs):
    from swarm.types import Confidence, WorkerOutput
    return WorkerOutput(
        subtask_id=sid, diff="", summary="", l1_passed=False,
        confidence=Confidence.LOW,
        l1_details={"pipeline_blocked": "internal_pkg_not_built",
                    "blocked_on_packages": pkgs,
                    "failure_class": "capability"})


def test_t4_replan_guard_return_emits_plan_when_edges_added(monkeypatch):
    import asyncio

    import swarm.brain.nodes as nodes
    from swarm.types import Confidence, WorkerOutput
    plan = TaskPlan(subtasks=[
        SubTask(id="st-p", description="生产 dto", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=["backend/src/com/acme/dto/UserDto.java"],
                                readable=[]), depends_on=[]),
        SubTask(id="st-c", description="消费 dto", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=["web/src/com/acme/web/UserCtl.java"],
                                readable=[]), depends_on=[]),
        SubTask(id="st-ok", description="旁支", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=["misc/util.py"], readable=[]), depends_on=[]),
    ], parallel_groups=[["st-p", "st-c", "st-ok"]])

    class _ReplanLLM:
        async def ainvoke(self, msgs):
            class R:
                content = '{"strategy": "replan", "reason": "整体重排"}'
            return R()

    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _ReplanLLM())
    state = {
        "complexity": "complex",
        "plan": plan,
        "failed_subtask_ids": ["st-c"],
        "subtask_results": {
            "st-c": _wo_blocked_cap("st-c", ["com.acme.dto"]),
            "st-ok": WorkerOutput(subtask_id="st-ok", diff="+x\n", summary="",
                                  l1_passed=True, confidence=Confidence.HIGH),
        },
        "dispatch_remaining": ["st-p"],
        "subtask_retry_counts": {},
    }
    out = asyncio.run(nodes.handle_failure(state))
    st_c = next(s for s in plan.subtasks if s.id == "st-c")
    if "st-p" in (st_c.depends_on or []):  # C9 补了边
        assert out.get("plan") is plan, (
            "replan 守卫 return 不回写 plan=动态边只活在内存（重启即丢，"
            "CODEWALK 纪律③禁的 in-place 变异靠捎带模式）")


# ─────────────── T5：打桩生产者满足依赖 ───────────────

def test_t5_stubbed_producer_satisfies_dependency():
    plan = TaskPlan(subtasks=[
        SubTask(id="st-p", description="p", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=["a.py"], readable=[])),
        SubTask(id="st-c", description="c", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=["b.py"], readable=[]), depends_on=["st-p"]),
    ], parallel_groups=[["st-p"], ["st-c"]])
    ready = plan.get_dispatch_batch(
        completed_ids={"st-p"},          # 阶梯三打桩：桩产出 l1_passed=True 已入 completed
        dispatch_remaining=["st-c"], max_concurrent=4,
        abandoned={"st-p"},              # dispatch 把 give_up 并入 abandoned
    )
    assert [t.id for t in ready] == ["st-c"], (
        "打桩路的设计意图=让下游对可编译桩照常推进；先查放弃集后查 completed 把"
        "带依赖边的下游永久扣死→被 #R13-4 静默划进 PARTIAL（交付面缩水）")


def test_t5_reverted_producer_still_blocks():
    plan = TaskPlan(subtasks=[
        SubTask(id="st-p", description="p", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=["a.py"], readable=[])),
        SubTask(id="st-c", description="c", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=["b.py"], readable=[]), depends_on=["st-p"]),
    ], parallel_groups=[["st-p"], ["st-c"]])
    ready = plan.get_dispatch_batch(
        completed_ids=set(),             # revert 路：无产出
        dispatch_remaining=["st-c"], max_concurrent=4, abandoned={"st-p"},
    )
    assert ready == [], "revert 放弃（无产出）的依赖仍永不就绪（原语义不回归）"


# ─────────────── T6/T7：compile/lint 查点 + deadline marker ───────────────

def test_t6_compile_stage_has_deadline_checkpoint(monkeypatch, tmp_path):
    from swarm.worker import l1_pipeline as lp
    (tmp_path / "a.py").write_text("x = 1\n")
    clock = itertools.chain([0.0], itertools.repeat(10.0))  # entry 过闸后预算耗尽
    monkeypatch.setattr(lp._time, "monotonic", lambda: next(clock))
    st = SubTask(id="st-t6", description="改 a.py", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["a.py"], readable=[]), intent="modify")
    ok, details = lp.run_l1_pipeline(str(tmp_path), st, _DIFF, llm=None, deadline=5.0)
    assert ok is True and details.get("pipeline_blocked") == "worker_deadline_exhausted"
    assert details.get("deadline_stage") == "compile", (
        f"compile/lint 段无查点=deadline 在 entry 后过期仍可越线跑 5-10 分钟: {details}")


def test_t7_deadline_blocked_carries_timeout_marker(tmp_path):
    import time as _t

    from swarm.worker.l1_pipeline import run_l1_pipeline
    (tmp_path / "a.py").write_text("x = 1\n")
    st = SubTask(id="st-t7", description="改 a.py", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["a.py"], readable=[]), intent="modify")
    ok, details = run_l1_pipeline(str(tmp_path), st, _DIFF, llm=None,
                                  deadline=_t.monotonic() - 1)
    assert details.get("error") == "timeout_in_verifying", (
        "oversize 拆小信号（_is_timeout_oversize_failure 消费）必须在产生处落 marker，"
        "不能靠 Phase-4 A5 布尔快照的偶然对齐续命")


# ─────────────── T8：F-F 过滤只看首行 + 数字词边界 ───────────────

def test_t8_filter_ignores_traceback_line_numbers():
    from swarm.models.router import _breaker_error_transient
    cap_err = ('BadRequestError: model_not_found\n'
               '  File "/x/connectionpool.py", line 5021, in urlopen\n'
               '    raise ...')
    assert _breaker_error_transient(cap_err) is False, (
        "在整段 traceback 上子串匹配：\"502\"命中行号 5021、\"connect\"命中"
        " connectionpool.py 帧路径→capability 错误误喂 breaker 熔断健康模型")
    assert _breaker_error_transient("ReadTimeout: request timed out\n  File ...") is True
    assert _breaker_error_transient("APIStatusError: 502 Bad Gateway") is True


# ─────────────── T9：digest 补 test/verify 失败证据 ───────────────

def test_t9_digest_covers_test_and_verify_failures():
    ex = _mk_ex()
    d1 = ex._l1_failure_digest({"l1_3_test_ok": False,
                                "test_output": "AssertionError: expected 1 got 2"})
    assert "AssertionError" in d1, (
        "verify agent 步跳过后（C5），test 失败的 fix prompt 证据为空串=修复 agent 盲修")
    d2 = ex._l1_failure_digest({"verify_failed": "curl -sf localhost:8080/health",
                                "verify_commands": [{"cmd": "curl", "ok": False,
                                                     "output": "exit 7"}]})
    assert "curl" in d2, "验收命令失败同理"


# ─────────────── T10：C11 GEN 死键清理 ───────────────

def test_t10_prune_drops_stale_generation_keys():
    from swarm.worker import l1_pipeline as lp
    lp._MANIFEST_PRESENT_CACHE.clear()
    _gen = lp._MANIFEST_CACHE_GEN
    lp._MANIFEST_PRESENT_CACHE[(_gen - 1, "sb-1", ("pom.xml",))] = True  # 旧代死键
    lp._MANIFEST_PRESENT_CACHE[(_gen, "sb-1", ("pom.xml",))] = True
    lp._prune_manifest_cache_negatives()
    assert (_gen - 1, "sb-1", ("pom.xml",)) not in lp._MANIFEST_PRESENT_CACHE, (
        "旧 GEN 正项永不命中=纯泄漏（key 含 GEN，invalidate 后不可达）")
    assert (_gen, "sb-1", ("pom.xml",)) in lp._MANIFEST_PRESENT_CACHE
    lp._MANIFEST_PRESENT_CACHE.clear()
