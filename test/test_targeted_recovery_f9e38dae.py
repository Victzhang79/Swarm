"""P0-B/P1-D 回归（治本 task f9e38dae）：scope 不可满足的编译失败 → 定向恢复。

现场：st-24 用 RedisTemplate 但 ruoyi-alarm/pom.xml 没声明依赖、pom 又不在 st-24 scope →
原地重试 8 次必败 → 耗尽配额 → 落全量 replan 清空 23 个完成态、子任务 30→34。

治本：识别"缺符号/缺依赖"编译失败 → 给失败子任务补【模块 pom】写权 + 重置徒劳重试计数 +
只重派失败子任务（保留成功兄弟、不进 PLAN、不清完成态全表）。targeted_recovery_count 熔断。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import swarm.brain.nodes as nodes
from swarm.brain.nodes import (
    _grant_module_pom_writable,
    _is_missing_dependency_failure,
    _serialize_pom_writers,
)
from swarm.types import (
    Complexity,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskPlan,
    WorkerOutput,
)


def _plan():
    return TaskPlan(
        subtasks=[
            SubTask(id="st-1", description="脚手架+AlarmApp",
                    difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
                    scope=FileScope(create_files=["ruoyi-alarm/pom.xml",
                                                  "ruoyi-alarm/src/main/java/App.java"])),
            SubTask(id="st-24", description="VoipNotifyServiceImpl 用 RedisTemplate",
                    difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
                    scope=FileScope(create_files=["ruoyi-alarm/src/main/java/impl/Voip.java"]),
                    depends_on=["st-1"]),
        ],
        parallel_groups=[["st-1"]],
    )


def _missing_dep_output(sid):
    return WorkerOutput(
        subtask_id=sid, diff="", summary="编译失败", l1_passed=False,
        l1_details={"build_failed": "mvn -pl ruoyi-alarm -am -q compile",
                    "build_output": "[ERROR] Voip.java:[12,8] cannot find symbol\n  symbol: class RedisTemplate"},
    )


def _state(**over):
    s = {
        "complexity": Complexity.ULTRA,
        "plan": _plan(),
        "failed_subtask_ids": ["st-24"],
        "subtask_results": {
            "st-1": WorkerOutput(subtask_id="st-1", diff="d", summary="ok", l1_passed=True),
            "st-24": _missing_dep_output("st-24"),
        },
        # 配额已烧光（模拟现场重试 8 次）——定向恢复仍应介入。
        "subtask_retry_counts": {"st-24": 8},
        "dispatch_remaining": [],
        "degraded_reasons": [],
    }
    s.update(over)
    return s


class _FakeResp:
    def __init__(self, content): self.content = content


def _fake_llm(strategy="replan"):
    class _L:
        async def ainvoke(self, _msgs):
            return _FakeResp('{"strategy":"%s","reasoning":"缺依赖"}' % strategy)
    return lambda: _L()


# ── 检测纯函数 ──────────────────────────────────────────
def test_detects_missing_symbol():
    sr = {"st-24": _missing_dep_output("st-24")}
    assert _is_missing_dependency_failure(sr, ["st-24"]) is True


def test_detects_chinese_missing_package():
    out = WorkerOutput(subtask_id="x", diff="", summary="", l1_passed=False,
                       l1_details={"build_output": "错误: 程序包 lombok.extern.slf4j 不存在"})
    assert _is_missing_dependency_failure({"x": out}, ["x"]) is True


def test_no_false_positive_on_generic_failure():
    out = WorkerOutput(subtask_id="x", diff="", summary="", l1_passed=False,
                       l1_details={"build_output": "test FooTest.bar 断言失败 expected 1 got 2"})
    assert _is_missing_dependency_failure({"x": out}, ["x"]) is False


def test_no_false_positive_on_does_not_exist_phrases():
    """HIGH-1：'User does not exist'/'table does not exist'/Java 模块可见性不应误判为缺依赖。"""
    for blob in ("Authentication failed: User does not exist",
                 "SQLException: Table 'alarm_app' does not exist",
                 "error: cannot access module java.base does not open"):
        out = WorkerOutput(subtask_id="x", diff="", summary="", l1_passed=False,
                           l1_details={"build_output": blob})
        assert _is_missing_dependency_failure({"x": out}, ["x"]) is False, blob


# ── 补 pom 写权 ─────────────────────────────────────────
def test_grant_module_pom_writable():
    plan = _plan()
    granted = _grant_module_pom_writable(plan, ["st-24"])
    assert granted == {"st-24": "ruoyi-alarm/pom.xml"}
    st24 = next(s for s in plan.subtasks if s.id == "st-24")
    assert "ruoyi-alarm/pom.xml" in st24.scope.writable
    # HIGH-2：失败 coder 应 depends_on pom 既有 owner(st-1)，保证 MERGE 拓扑序(create 先于 modify)
    assert "st-1" in (st24.depends_on or []), "应串到 pom owner 后面防 merge 冲突"


def test_grant_skips_non_module_top_dir():
    """MEDIUM-1：首段是 src/test 等非模块目录时不误推 'src/pom.xml'。"""
    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="d",
                scope=FileScope(create_files=["src/main/java/Root.java"])),
    ])
    granted = _grant_module_pom_writable(plan, ["st-1"])
    assert granted == {}, "src/ 不是模块目录，不应授 pom 写权"
    assert "src/pom.xml" not in (plan.subtasks[0].scope.writable or [])


def test_add_dep_safe_refuses_cycle():
    """HIGH-4：加边会成环时拒绝（传递可达检查，非仅直接边）。"""
    from swarm.brain.nodes import _add_dep_safe
    # 链 a→b→c（a 依赖 b 依赖 c）；试图让 c 依赖 a 会成环 a→b→c→a。
    a = SubTask(id="a", description="d", scope=FileScope(), depends_on=["b"])
    b = SubTask(id="b", description="d", scope=FileScope(), depends_on=["c"])
    c = SubTask(id="c", description="d", scope=FileScope())
    by_id = {"a": a, "b": b, "c": c}
    assert _add_dep_safe(by_id, "c", "a") is False, "应拒绝成环边"
    assert "a" not in (c.depends_on or [])


def test_serialize_pom_writers_chains_same_module():
    plan = TaskPlan(subtasks=[
        SubTask(id="st-19", description="d", scope=FileScope(create_files=["m/a.java"])),
        SubTask(id="st-24", description="d", scope=FileScope(create_files=["m/b.java"])),
    ])
    _serialize_pom_writers(plan, {"st-19": "m/pom.xml", "st-24": "m/pom.xml"})
    st24 = next(s for s in plan.subtasks if s.id == "st-24")
    assert "st-19" in (st24.depends_on or []), "同模块 pom 写者应串行化防争抢"


# ── handle_failure 端到端定向恢复 ───────────────────────
def test_targeted_recovery_fires_even_with_exhausted_budget():
    """配额烧光 + 缺依赖 + 有成功兄弟 → 定向恢复（非全量 replan、非 escalate）。"""
    with patch.object(nodes, "_get_brain_llm", _fake_llm("replan")):
        out = asyncio.run(nodes.handle_failure(_state()))
    assert out.get("failure_strategy") == "retry_alternate", out.get("failure_strategy")
    assert out.get("targeted_recovery") is True
    assert out.get("targeted_recovery_count") == 1
    # 保留成功兄弟 st-1
    assert "st-1" in out.get("subtask_results", {})
    # 只重派失败的 st-24
    assert out.get("dispatch_remaining") == ["st-24"]
    # 重置徒劳的重试计数
    assert out.get("subtask_retry_counts", {}).get("st-24") == 0
    # pom 写权补进 plan
    plan = out.get("plan")
    st24 = next(s for s in plan.subtasks if s.id == "st-24")
    assert "ruoyi-alarm/pom.xml" in st24.scope.writable


def test_targeted_recovery_circuit_breaks_then_falls_through():
    """定向恢复达上限仍缺依赖 → 不再定向，落常规 replan 兜底。"""
    from swarm.config.settings import get_config
    cap = get_config().model.max_retries
    with patch.object(nodes, "_get_brain_llm", _fake_llm("replan")):
        out = asyncio.run(nodes.handle_failure(_state(targeted_recovery_count=cap)))
    # 第 cap+1 次定向恢复被熔断 → 落常规 replan（携带 replan_count）
    assert out.get("failure_strategy") == "replan", out.get("failure_strategy")
    assert out.get("targeted_recovery") is not True


def test_generic_failure_skips_targeted_recovery():
    """非缺依赖失败（如测试断言失败）不走定向恢复，按原 replan 守卫处理。"""
    st = _state(subtask_results={
        "st-1": WorkerOutput(subtask_id="st-1", diff="d", summary="ok", l1_passed=True),
        "st-24": WorkerOutput(subtask_id="st-24", diff="", summary="断言失败", l1_passed=False,
                              l1_details={"build_output": "FooTest 断言 expected 1 got 2"}),
    }, subtask_retry_counts={})
    with patch.object(nodes, "_get_brain_llm", _fake_llm("replan")):
        out = asyncio.run(nodes.handle_failure(st))
    assert out.get("targeted_recovery") is not True
    # 配额未耗尽 + 有成功兄弟 → 原 replan 守卫降级为 retry（保留 st-1）
    assert out.get("failure_strategy") in ("retry", "retry_alternate")
    assert "st-1" in out.get("subtask_results", {})


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
