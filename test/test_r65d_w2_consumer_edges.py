"""R65D-W2：plan 初始 DAG 缺消费者→生产者边 + 调度不按扇出优先高位生产者。

round65d 实锤（task b583df8f，live 事发态）：13 个根任务大半是 admin 隐性消费者
（readable 引用 alarm 新文件却零 depends_on 边），首两批 7/8 派发全 BLOCKED（头排
堵塞 10min 零完成）；fixture 终版 checkpoint 态上本步实测 +176 边/70 消费者；54/94 任务书 upstream_artifacts 空，worker 盲猜上游符号致命名分叉
（AlarmBotService vs 契约 IAlarmBotService）。三面同根=规划期不建消费关系，
C9 动态补边/L1 BLOCKED 退避只能执行期代偿（每次代偿=整条 locate/code 白跑）。

治本（规划期确定性，零 LLM）：
① plan_finisher 新步 derive_consumer_depends_edges：readable 引用【本 plan 其它
  子任务 create_files 将建的文件】= 结构性消费关系 → 确定性下推 depends_on 边
  （环护栏：创建者可达消费者则不加；创建者歧义/基线文件不加；幂等）。
  边就位后：消费者被 dispatch 依赖闸自然扣住（头排不再 BLOCKED 白跑）、
  生产者 dep_counts>0 自然升 tier-1、规则2/B1 的 readable/upstream 注入面被激活。
② get_dispatch_batch 生产者层按【下游扇出】降序（高位生产者先跑——round65d
  st-26 扇出 90 却与叶子同权重排队）。
"""
from __future__ import annotations

import pytest

from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan


@pytest.fixture
def derive_consumer_depends_edges():
    from swarm.brain.plan_finisher import derive_consumer_depends_edges as fn
    return fn

_API = "mod-api/src/main/java/com/x/IChannel.java"
_IMPL = "mod-impl/src/main/java/com/x/ChannelImpl.java"
_BASE = "ruoyi-common/src/main/java/com/ruoyi/common/BaseEntity.java"


def _st(sid, *, create=None, readable=None, depends=None):
    return SubTask(id=sid, description=f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(create_files=create or [],
                                   readable=readable or []),
                   depends_on=depends or [])


def _plan(subs):
    return TaskPlan(subtasks=subs, parallel_groups=[[s.id for s in subs]])


# ───────────────────── ① 消费边下推 ─────────────────────

def test_readable_of_plan_created_file_pushes_edge(derive_consumer_depends_edges):
    """★头排堵塞本体★：消费者 readable 引用生产者将建文件 → 确定性下推 depends_on。"""
    prod = _st("st-p", create=[_API])
    cons = _st("st-c", create=[_IMPL], readable=[_API])
    plan = _plan([prod, cons])
    added = derive_consumer_depends_edges(plan)
    assert "st-p" in cons.depends_on, \
        f"消费关系必须成边（否则消费者头排派发必 BLOCKED 白跑）: {cons.depends_on}"
    assert added, "机读返回必须报告新增边"


def test_baseline_readable_no_edge(derive_consumer_depends_edges):
    """基线文件（无 plan 创建者）readable 不成边——绝不无中生有串行。"""
    cons = _st("st-c", create=[_IMPL], readable=[_BASE])
    plan = _plan([cons])
    derive_consumer_depends_edges(plan)
    assert cons.depends_on == []


def test_cycle_guard_no_reverse_edge(derive_consumer_depends_edges):
    """环护栏：创建者（传递）依赖消费者时绝不加边（加了=死锁环）。"""
    prod = _st("st-p", create=[_API], depends=["st-c"])
    cons = _st("st-c", create=[_IMPL], readable=[_API])
    plan = _plan([prod, cons])
    derive_consumer_depends_edges(plan)
    assert "st-p" not in cons.depends_on, "会成环的边必须跳过"


def test_ambiguous_creator_no_edge(derive_consumer_depends_edges):
    """创建者歧义（两子任务都声明 create 同一文件——上游计划错）→ 不加边不猜。"""
    p1 = _st("st-p1", create=[_API])
    p2 = _st("st-p2", create=[_API])
    cons = _st("st-c", create=[_IMPL], readable=[_API])
    plan = _plan([p1, p2, cons])
    derive_consumer_depends_edges(plan)
    assert "st-p1" not in cons.depends_on and "st-p2" not in cons.depends_on


def test_idempotent_and_no_self_edge(derive_consumer_depends_edges):
    prod = _st("st-p", create=[_API])
    cons = _st("st-c", create=[_IMPL], readable=[_API, _IMPL])
    plan = _plan([prod, cons])
    derive_consumer_depends_edges(plan)
    derive_consumer_depends_edges(plan)
    assert cons.depends_on.count("st-p") == 1, "幂等：重跑不重复加边"
    assert "st-c" not in cons.depends_on, "自引用绝不成边"


def test_finisher_wires_consumer_edges():
    """接线面：finish_plan_deterministic 必须包含本步（readable 归一之后）。"""
    from swarm.brain.plan_finisher import finish_plan_deterministic
    prod = _st("st-p", create=[_API])
    cons = _st("st-c", create=[_IMPL], readable=[_API])
    plan = _plan([prod, cons])
    out = finish_plan_deterministic(plan, [], None)
    assert "st-p" in cons.depends_on, "收尾器必须接线消费边下推"
    assert out.get("consumer_edges"), f"机读摘要必须含新增边: {out}"


# ───────────────────── ② 调度扇出优先 ─────────────────────

def test_dispatch_prioritizes_high_fanout_producer():
    """★st-26 教训★：同为就绪生产者，下游扇出 3 的必须排在扇出 1 的前面
    （高位生产者晚跑一轮=全场多等一轮）。"""
    big = _st("st-big", create=["m/src/main/java/A.java"])
    small = _st("st-small", create=["m/src/main/java/B.java"])
    plan = TaskPlan(subtasks=[
        small,                                  # 列表序在前（旧行为会先派它）
        big,
        _st("st-d1", create=["m/src/main/java/C.java"], depends=["st-big"]),
        _st("st-d2", create=["m/src/main/java/D.java"], depends=["st-big"]),
        _st("st-d3", create=["m/src/main/java/E.java"], depends=["st-big"]),
        _st("st-d4", create=["m/src/main/java/F.java"], depends=["st-small"]),
    ], parallel_groups=[["st-small", "st-big", "st-d1", "st-d2", "st-d3", "st-d4"]])
    batch = plan.get_dispatch_batch(
        completed_ids=set(),
        dispatch_remaining=[s.id for s in plan.subtasks],
        max_concurrent=2)
    ids = [t.id for t in batch]
    assert ids.index("st-big") < ids.index("st-small"), \
        f"高扇出生产者必须优先: {ids}"


def test_dispatch_manifest_still_first():
    """对照面：构建清单子任务仍最优先（B6 语义不回归）。"""
    pom = _st("st-pom", create=["mod/pom.xml"])
    big = _st("st-big", create=["m/src/main/java/A.java"])
    plan = TaskPlan(subtasks=[
        big, pom,
        _st("st-d1", create=["m/src/main/java/C.java"], depends=["st-big"]),
        _st("st-d2", create=["m/src/main/java/D.java"], depends=["st-big"]),
    ], parallel_groups=[["st-big", "st-pom", "st-d1", "st-d2"]])
    batch = plan.get_dispatch_batch(
        completed_ids=set(),
        dispatch_remaining=[s.id for s in plan.subtasks],
        max_concurrent=2)
    assert batch[0].id == "st-pom", f"清单子任务恒第一: {[t.id for t in batch]}"


def test_ladder_exhausted_mass_abandon_gated(derive_consumer_depends_edges):
    """★猎手 CRITICAL 锁★：消费边织密图后，高扇出生产者重试耗尽走【部分交付】旁门
    时，连坐闭包超规模闸必须 escalate 人工——绝不静默清盘成 PARTIAL
    （round65c 102/107 死型经未设防调用点复活的路径）。"""
    import asyncio
    from swarm.brain.nodes import handle_failure
    from swarm.types import WorkerOutput
    prod = _st("st-p", create=[_API])
    consumers = [
        _st(f"st-c{i}", create=[f"admin/src/main/java/C{i}.java"],
            readable=[_API])
        for i in range(14)
    ]
    done = _st("st-done", create=["m/src/main/java/D.java"])
    plan = _plan([prod, *consumers, done])
    derive_consumer_depends_edges(plan)
    state = {
        "plan": plan,
        "failed_subtask_ids": ["st-p"],
        "subtask_results": {
            "st-p": WorkerOutput(subtask_id="st-p", diff="+x", summary="",
                                 l1_passed=False,
                                 l1_details={"verify_failed": "grep"}),
            "st-done": WorkerOutput(subtask_id="st-done", diff="+ok", summary="",
                                    l1_passed=True),
        },
        "subtask_retry_counts": {"st-p": 99},   # 重试耗尽 → 部分交付旁门
        "dispatch_remaining": [c.id for c in consumers],
    }
    r = asyncio.run(handle_failure(state))
    assert r.get("failure_strategy") == "escalate", \
        f"连坐 15/16 超阈值必须 escalate 而非静默 abandon: {r.get('failure_strategy')}"
    assert any(str(d).startswith("mass_abandon_gate")
               for d in (r.get("degraded_reasons") or [])), r.get("degraded_reasons")


def test_ladder_exhausted_small_abandon_still_partial(derive_consumer_depends_edges):
    """对照面：阈值内连坐照旧走部分交付（规模闸绝不误伤正常剪枝）。"""
    import asyncio
    from swarm.brain.nodes import handle_failure
    from swarm.types import WorkerOutput
    subs = [_st("st-p", create=[_API]),
            _st("st-c0", create=["admin/src/main/java/C0.java"], readable=[_API])]
    subs += [_st(f"st-x{i}", create=[f"m/src/main/java/X{i}.java"])
             for i in range(40)]   # 大计划：闭包 2/42 远低于阈值
    done = _st("st-done", create=["m/src/main/java/D.java"])
    plan = _plan([*subs, done])
    derive_consumer_depends_edges(plan)
    state = {
        "plan": plan,
        "failed_subtask_ids": ["st-p"],
        "subtask_results": {
            "st-p": WorkerOutput(subtask_id="st-p", diff="+x", summary="",
                                 l1_passed=False,
                                 l1_details={"verify_failed": "grep"}),
            "st-done": WorkerOutput(subtask_id="st-done", diff="+ok", summary="",
                                    l1_passed=True),
        },
        "subtask_retry_counts": {"st-p": 99},
        "dispatch_remaining": [s.id for s in subs if s.id != "st-p"],
    }
    r = asyncio.run(handle_failure(state))
    assert r.get("failure_strategy") == "abandon", r.get("failure_strategy")
    assert "st-c0" in set(r.get("abandoned_subtask_ids") or []), \
        "消费者随生产者进闭包（阈值内正常剪枝）"


def test_consumer_edges_prevent_headline_blocked_batch(derive_consumer_depends_edges):
    """端到端缩影：边就位后，首批派发只含生产者——消费者被依赖闸扣住，
    绝不再有 7/8 BLOCKED 的头排白跑。"""
    prod = _st("st-p", create=[_API])
    consumers = [
        _st(f"st-c{i}", create=[f"admin/src/main/java/C{i}.java"],
            readable=[_API])
        for i in range(4)
    ]
    plan = _plan([prod, *consumers])
    derive_consumer_depends_edges(plan)
    batch = plan.get_dispatch_batch(
        completed_ids=set(),
        dispatch_remaining=[s.id for s in plan.subtasks],
        max_concurrent=8)
    ids = {t.id for t in batch}
    assert ids == {"st-p"}, \
        f"消费者必须被依赖闸扣住等生产者，绝不头排 BLOCKED 白跑: {ids}"
