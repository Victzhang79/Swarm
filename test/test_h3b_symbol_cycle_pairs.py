"""R67J-H3b：符号成环消费对——落账 + 消费者防臆造提示 + dispatch 软序兜底。

法证A 横切病灶（规模递增 49→66→96 对）+ round67 task 64cb44ed 实锤：R67-T4b 消费边
成环（生产者传递依赖消费者）→ 跳过补边 → 消费者先于生产者执行，引用一个尚不存在的类
→ worker 只能臆造签名/重复实现（编译可过=假过，round67 st-50-1 2FA 双实现真根）或
L1 cannot find symbol（H-3a 后=BLOCKED 退避，但生产者在等消费者 → 互等结徒劳烧预算）。

设计修正（对照 PLAN_round67j_hardening H-3b 原 sketch）：cycle 对在 DAG 上生产者传递
依赖消费者 → 二者【不可能】同批就绪，"同批不并发"原前提仅在依赖被放弃/软边旁路松弛后
才可达。故治法四件套：
  ① cycle 对持久化 TaskPlan.symbol_cycle_pairs（随 plan 过 checkpoint，机读账）；
  ② 消费者 context_snippets 注入确定性提示：不得编译期引用未来类、绝不自行臆造同名类
     （直接掐死 round67 假过真根——臆造）；
  ③ get_dispatch_batch 软序兜底：对 (c,p) 两者同批就绪（放弃松弛场景）时 defer c 一批
     让 p 先行；只影响批序绝不改依赖图、绝不丢派（deferred 仍在 remaining）；
     防饿死/防空批：p 不就绪不 defer（防 DAG 环死锁）、互对/链式对至少保留一个；
  ④ failure C9 环滤空洞 WARNING（互等结可观测，自动解结另案）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.plan_finisher import wire_symbol_consumption_edges  # noqa: E402
from swarm.types import FileScope, SubTask, TaskHarness, TaskPlan  # noqa: E402


def _st(sid, *, create=None, writable=None, readable=None, depends=None, desc="d"):
    return SubTask(
        id=sid, description=desc,
        scope=FileScope(writable=writable or [], create_files=create or [],
                        readable=readable or []),
        harness=TaskHarness(language="java"), depends_on=depends or [],
    )


_SVC = "m/src/main/java/com/a/BazService.java"


def _cycle_plan():
    """生产者 st-p 创建 BazService 且依赖消费者 st-c；st-c 描述消费 BazService → 成环跳边。"""
    producer = _st("st-p", create=[_SVC], depends=["st-c"])
    consumer = _st("st-c", create=["m2/src/main/java/com/b/C.java"], desc="消费 BazService")
    return TaskPlan(subtasks=[producer, consumer], parallel_groups=[["st-c"], ["st-p"]])


# ── ①② 落账 + 消费者提示 ──────────────────────────────────────────────────────

def test_cycle_pair_persisted_on_plan():
    plan = _cycle_plan()
    wire_symbol_consumption_edges(plan)
    assert ["st-c", "st-p"] in [list(p) for p in (plan.symbol_cycle_pairs or [])], \
        "成环放弃的 (消费者,生产者) 对必须落 plan 持久账（dispatch/观测消费）"
    # 既有语义不回归：边仍绝不添加（防环）
    consumer = next(s for s in plan.subtasks if s.id == "st-c")
    assert "st-p" not in (consumer.depends_on or [])


def test_cycle_consumer_gets_anti_fabrication_note():
    """消费者必须拿到确定性提示：类由他人稍后创建，不得引用、绝不臆造同名类。"""
    plan = _cycle_plan()
    wire_symbol_consumption_edges(plan)
    consumer = next(s for s in plan.subtasks if s.id == "st-c")
    note = consumer.context_snippets or ""
    assert "BazService" in note and "st-p" in note, \
        "提示必须点名类与生产者（round67 假过真根=worker 臆造未来类）"


def test_cycle_note_idempotent():
    """plan-finish 可重入（replan/revision 复跑）→ 提示绝不重复堆叠。"""
    plan = _cycle_plan()
    wire_symbol_consumption_edges(plan)
    consumer = next(s for s in plan.subtasks if s.id == "st-c")
    once = consumer.context_snippets
    wire_symbol_consumption_edges(plan)
    assert consumer.context_snippets == once


def test_pairs_cleared_when_cycle_resolved_on_rerun():
    """★always-emit 防粘滞★ 同 plan 对象 finish 重入且本轮环已消 → 旧对必须清空覆写
    （残留旧账会让 dispatch 软序 defer 错人）。"""
    plan = _cycle_plan()
    wire_symbol_consumption_edges(plan)
    assert plan.symbol_cycle_pairs
    producer = next(s for s in plan.subtasks if s.id == "st-p")
    producer.depends_on = []            # 环因（p 依赖 c）已被修掉
    wire_symbol_consumption_edges(plan)
    assert plan.symbol_cycle_pairs == [], \
        f"无环轮必须清空旧账: {plan.symbol_cycle_pairs}"


def test_no_cycle_no_pair_zero_regression():
    """正常成边路径：账为空、行为与既有完全一致。"""
    producer = _st("st-p", create=[_SVC])
    consumer = _st("st-c", desc="消费 BazService")
    plan = TaskPlan(subtasks=[producer, consumer])
    wire_symbol_consumption_edges(plan)
    assert not (plan.symbol_cycle_pairs or [])
    assert "st-p" in (consumer.depends_on or [])


def test_old_checkpoint_without_field_defaults_empty():
    """老 checkpoint 反序列化无此字段 → 默认空列表，零迁移。"""
    plan = TaskPlan.model_validate({"subtasks": [
        {"id": "a", "description": "d",
         "scope": {"writable": [], "readable": [], "create_files": []}}]})
    assert plan.symbol_cycle_pairs == []


# ── ③ dispatch 软序兜底 ───────────────────────────────────────────────────────

def _batch(plan, completed, remaining, mx=8):
    return [t.id for t in plan.get_dispatch_batch(
        completed_ids=set(completed), dispatch_remaining=list(remaining),
        max_concurrent=mx)]


def test_dispatch_defers_consumer_when_producer_coready():
    """(c,p) 同批就绪（放弃松弛场景的模拟：二者无 DAG 边）→ c 让一批、p 先行。"""
    plan = TaskPlan(subtasks=[_st("c"), _st("p", create=[_SVC])])
    plan.symbol_cycle_pairs = [["c", "p"]]
    ids = _batch(plan, completed=[], remaining=["c", "p"])
    assert ids == ["p"], f"消费者必须 defer 让生产者先行: {ids}"


def test_dispatch_releases_consumer_after_producer_done():
    plan = TaskPlan(subtasks=[_st("c"), _st("p", create=[_SVC])])
    plan.symbol_cycle_pairs = [["c", "p"]]
    ids = _batch(plan, completed=["p"], remaining=["c"])
    assert ids == ["c"], "生产者完成后消费者必须立即释放（defer 绝不是丢派）"


def test_dispatch_mutual_pairs_no_stall():
    """互对 (a,b)+(b,a) → 至少保留一个，绝不空批停摆。"""
    plan = TaskPlan(subtasks=[_st("a"), _st("b")])
    plan.symbol_cycle_pairs = [["a", "b"], ["b", "a"]]
    ids = _batch(plan, completed=[], remaining=["a", "b"])
    assert len(ids) == 1, f"互对必须恰好 defer 一侧: {ids}"


def test_dispatch_chain_pairs_keep_at_least_one():
    """链式对 a→b→c→a：任意组合下至少一个被保留（结构性无空批证明的行为锁）。"""
    plan = TaskPlan(subtasks=[_st("a"), _st("b"), _st("c")])
    plan.symbol_cycle_pairs = [["a", "b"], ["b", "c"], ["c", "a"]]
    ids = _batch(plan, completed=[], remaining=["a", "b", "c"])
    assert ids, "链式软序对绝不许把整批 defer 成空批"


def test_dispatch_no_defer_when_producer_not_ready():
    """p 自身依赖未满足（不就绪）→ c 绝不 defer——否则 DAG 环场景（p 传递依赖 c）
    下 c 等 p、p 等 c = 调度自死锁。"""
    plan = TaskPlan(subtasks=[
        _st("c"), _st("x"), _st("p", create=[_SVC], depends=["x"])])
    plan.symbol_cycle_pairs = [["c", "p"]]
    ids = _batch(plan, completed=[], remaining=["c", "x", "p"])
    assert "c" in ids, f"生产者不就绪时消费者必须照常派发（防调度死锁）: {ids}"


def test_dispatch_defers_consumer_while_producer_in_flight():
    """★复核 M2 锁★ 滚动补位场景：p 已派出未完成（不在 remaining → 不在就绪集）时
    c 必须继续 defer——否则软序被补位轮绕过，c 与执行中的 p 并跑。"""
    plan = TaskPlan(subtasks=[_st("c"), _st("p", create=[_SVC])])
    plan.symbol_cycle_pairs = [["c", "p"]]
    ids = [t.id for t in plan.get_dispatch_batch(
        completed_ids=set(), dispatch_remaining=["c"], max_concurrent=8,
        in_flight={"p"})]
    assert ids == [], f"生产者在飞时消费者必须继续 defer: {ids}"
    # p 完成后释放（in_flight 清空 + completed 含 p）
    ids2 = [t.id for t in plan.get_dispatch_batch(
        completed_ids={"p"}, dispatch_remaining=["c"], max_concurrent=8,
        in_flight=set())]
    assert ids2 == ["c"]


def test_dispatch_empty_pairs_zero_regression():
    plan = TaskPlan(subtasks=[_st("c"), _st("p", create=[_SVC])])
    ids = _batch(plan, completed=[], remaining=["c", "p"])
    assert sorted(ids) == ["c", "p"]
