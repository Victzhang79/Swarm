"""R65REPLAY-T4（task #69）：上游账↔依赖序对账——幽灵上游账死锁治本。

round65d 回放轮实锤（task b1eaeef0，三路定案）：R63-T4 符号布线按语料文本引用把
落点写进 readable+upstream_artifacts 但不查方向——st-11-1(写 XML) 的账里被布进
4 个 Mapper 接口 .java，而这些接口的计划内创建者 st-11-2..5 反过来（传递）依赖
st-11-1。seed 闸 fail-closed 只看账不知生产者在自己下游 → 永久 BLOCKED"等生产者"；
执行期预算闸拆分 deep-copy 继承账把 1 个死等复制成 4 个；A2/B2/规模闸 64min 后
会师 → 连坐 72 → PARTIAL 24/97。W2/T5 规划期检测到环只跳边、从不清账。

治本（栈中立，纯 DAG/路径逻辑，绝不派 worker 去死等）：
① plan_finisher.reconcile_upstream_account：对每个子任务的 upstream_artifacts
  条目，若其计划内全部创建者都（传递）依赖本任务（生产者在自己下游=结构矛盾），
  确定性从 ua（及 readable 同路径）剔除 + WARNING + 机读账。账让位于序：语义引用
  不是构建输入，L1/C9 兜底。创建者歧义（上下游混合）/无计划内创建者（基线）不动。
② finish_plan_deterministic 末端接线（live 规划与 plan 注入回放共用同一收尾器）。
③ dispatch 侧同一 helper 兜执行期账写者（拆分继承/#54 梯三桩），见
  test_r65replay_t4_dispatch_reconcile。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "plan_b583.json"

_XML = "mod/src/main/resources/mapper/AMapper.xml"
_JAVA = "mod/src/main/java/com/x/AMapper.java"
_JAVA2 = "mod/src/main/java/com/x/BMapper.java"


@pytest.fixture
def reconcile():
    from swarm.brain.plan_finisher import reconcile_upstream_account as fn
    return fn


def _st(sid, *, create=None, writable=None, readable=None, ua=None, depends=None):
    return SubTask(
        id=sid, description=f"task {sid}", difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(create_files=create or [], writable=writable or [],
                        readable=readable or [], upstream_artifacts=ua or []),
        depends_on=depends or [])


def _plan(subs):
    return TaskPlan(subtasks=subs, parallel_groups=[[s.id for s in subs]])


# ───────────── ① helper 语义 ─────────────

def test_downstream_producer_entry_removed(reconcile):
    """★死锁本体★：ua 条目的创建者依赖本任务（生产者在下游）→ 必须剔账。"""
    a = _st("st-a", create=[_XML], readable=[_JAVA], ua=[_JAVA])
    b = _st("st-b", create=[_JAVA], depends=["st-a"])
    removed = reconcile(_plan([a, b]))
    assert _JAVA not in (a.scope.upstream_artifacts or []), \
        "生产者在自己下游的 ua 条目不剔 = seed 闸永久死等（round65d 回放死因本体）"
    assert _JAVA not in (a.scope.readable or []), "readable 同路径条目须一并剔除"
    assert removed == {"st-a": [_JAVA]}, f"机读账必须报告剔除明细: {removed}"


def test_true_upstream_entry_kept(reconcile):
    """真上游（消费者依赖生产者，方向正确）的账绝不动。"""
    b = _st("st-b", create=[_JAVA])
    a = _st("st-a", create=[_XML], readable=[_JAVA], ua=[_JAVA], depends=["st-b"])
    removed = reconcile(_plan([a, b]))
    assert _JAVA in a.scope.upstream_artifacts, "方向正确的上游账被误剔=冤杀"
    assert removed == {}


def test_transitive_downstream_removed(reconcile):
    """生产者【传递】依赖本任务（b→m→a 链）同样是死锁 → 剔。"""
    a = _st("st-a", create=[_XML], ua=[_JAVA])
    m = _st("st-m", create=["mod/M.java"], depends=["st-a"])
    b = _st("st-b", create=[_JAVA], depends=["st-m"])
    removed = reconcile(_plan([a, m, b]))
    assert _JAVA not in (a.scope.upstream_artifacts or [])
    assert removed == {"st-a": [_JAVA]}


def test_no_plan_owner_kept(reconcile):
    """无计划内创建者（基线/存量文件）不动——存在性由 seed 闸/R49-2 运行期判。"""
    a = _st("st-a", create=[_XML], ua=["ruoyi-common/src/main/java/Base.java"])
    removed = reconcile(_plan([a]))
    assert a.scope.upstream_artifacts == ["ruoyi-common/src/main/java/Base.java"]
    assert removed == {}


def test_mixed_owners_conservative_kept(reconcile):
    """创建者歧义（一个在上游一个在下游）→ 保守不动（上游者可能真产出）。"""
    up = _st("st-up", create=[_JAVA])
    a = _st("st-a", create=[_XML], ua=[_JAVA], depends=["st-up"])
    down = _st("st-down", create=[_JAVA], depends=["st-a"])
    removed = reconcile(_plan([up, a, down]))
    assert _JAVA in a.scope.upstream_artifacts, "歧义创建者绝不猜（宁缺勿滥先例）"
    assert removed == {}


def test_self_owned_entry_removed(reconcile):
    """自己 create 的文件混进自己的 ua（自等死锁）→ 剔。"""
    a = _st("st-a", create=[_JAVA], ua=[_JAVA])
    removed = reconcile(_plan([a]))
    assert a.scope.upstream_artifacts == []
    assert removed == {"st-a": [_JAVA]}


def test_shard_inheritance_shape_cleaned(reconcile):
    """回放实锤形态：父拆 4 分片串行链继承幽灵账，生产者 remap 到链尾 → 全剔。"""
    files = [_JAVA, _JAVA2]
    shards = []
    for i in range(1, 5):
        shards.append(_st(
            f"st-p-{i}", create=[f"mod/src/main/resources/m{i}.xml"],
            readable=list(files), ua=list(files),
            depends=[f"st-p-{i-1}"] if i > 1 else []))
    prods = [
        _st("st-q1", create=[_JAVA], depends=["st-p-4"]),
        _st("st-q2", create=[_JAVA2], depends=["st-p-4"]),
    ]
    removed = reconcile(_plan(shards + prods))
    for sh in shards:
        assert not (sh.scope.upstream_artifacts or []), \
            f"分片 {sh.id} 继承的幽灵账未清（1 个死等复制成 4 个的回放死型）"
        assert not any(f in (sh.scope.readable or []) for f in files)
    assert set(removed) == {s.id for s in shards}


def test_writable_upstream_producer_kept(reconcile):
    """复核 F1：真生产者是上游 writable 修改者（create 口径看不见），下游另有重复
    create 声明 → 账必须保留（剔了=worker 拿陈旧内容盲写且无 BLOCKED 信号）。"""
    w = _st("st-w", writable=[_JAVA])
    a = _st("st-a", create=[_XML], ua=[_JAVA], depends=["st-w"])
    dup = _st("st-dup", create=[_JAVA], depends=["st-a"])
    removed = reconcile(_plan([w, a, dup]))
    assert _JAVA in a.scope.upstream_artifacts, \
        "writable 声明的上游修改者是合法生产者——owner 口径必须 create∪writable"
    assert removed == {}


def test_path_drift_normalized_still_removed(reconcile):
    """复核 F2（R41 先例）：'./' 前缀/反斜杠口径漂移不得让幽灵账漏网。"""
    a = _st("st-a", create=[_XML],
            ua=["./" + _JAVA, _JAVA2.replace("/", "\\")],
            readable=["./" + _JAVA])
    b = _st("st-b", create=[_JAVA, _JAVA2], depends=["st-a"])
    removed = reconcile(_plan([a, b]))
    assert a.scope.upstream_artifacts == [], \
        f"路径口径漂移让幽灵账漏网=死锁静默复发: {a.scope.upstream_artifacts}"
    assert a.scope.readable == []
    assert set(removed) == {"st-a"} and len(removed["st-a"]) == 2


def test_removed_paths_hint_injected(reconcile):
    """复核 F3：剔账保留信息通道——权威路径以文本提示注入 context_snippets，
    worker 仍能按路径推导符号名（掐死的是 seed 死等语义，不是路径知识）。"""
    a = _st("st-a", create=[_XML], ua=[_JAVA])
    b = _st("st-b", create=[_JAVA], depends=["st-a"])
    reconcile(_plan([a, b]))
    snip = getattr(a, "context_snippets", "") or ""
    assert _JAVA in snip and "后续任务创建" in snip, \
        f"剔账后 worker 失去权威路径=从死等退化成盲猜命名: {snip[-200:]}"


def test_reconcile_failure_marks_machine_readable(monkeypatch):
    """复核 F6：对账 pass 自身挂掉必须落机读标记（调用方进 degraded_reasons）——
    否则唯一信号是无人 grep 的 WARNING，静默回归比治前更糟。"""
    import swarm.brain.plan_finisher as pf
    a = _st("st-a", create=[_XML], ua=[_JAVA])
    plan = _plan([a])
    monkeypatch.setattr(pf, "reconcile_upstream_account",
                        lambda _p: (_ for _ in ()).throw(RuntimeError("boom")))
    out = pf.finish_plan_deterministic(plan, None)
    assert out.get("upstream_account_reconcile_failed") is True


def test_idempotent(reconcile):
    """幂等：清过的 plan 再跑一遍零变更。"""
    a = _st("st-a", create=[_XML], ua=[_JAVA])
    b = _st("st-b", create=[_JAVA], depends=["st-a"])
    plan = _plan([a, b])
    reconcile(plan)
    assert reconcile(plan) == {}


# ───────────── ② 收尾器接线（live 规划 + 注入回放共用）─────────────

def test_finish_plan_deterministic_reconciles(caplog):
    from swarm.brain.plan_finisher import finish_plan_deterministic
    a = _st("st-a", create=[_XML], readable=[_JAVA], ua=[_JAVA])
    b = _st("st-b", create=[_JAVA], depends=["st-a"])
    plan = _plan([a, b])
    with caplog.at_level(logging.WARNING):
        out = finish_plan_deterministic(plan, None)
    assert _JAVA not in (a.scope.upstream_artifacts or []), \
        "收尾器末端必须跑上游账对账（W2 跳成环边之后，账不许留矛盾）"
    assert out.get("upstream_account_reconciled") == {"st-a": [_JAVA]}, \
        f"收尾器机读摘要须含对账明细: {out.get('upstream_account_reconciled')}"
    assert any("上游账" in r.message or "upstream" in r.message.lower()
               for r in caplog.records), "剔账必须 WARNING 留痕（可观测降级铁律）"


def test_fixture_st_11_1_ghost_account_cleaned():
    """★回放死因 RED→GREEN 判据★：cassette 注入重推导后，st-11-1 的账里不得再有
    下游 st-11-2..5 才会创建的 4 个 Mapper 接口；st-17-1 同构（Impl 归 st-17-2..4）。"""
    from swarm.brain.plan_inject import prepare_injected_state
    c = json.loads(_FIXTURE.read_text())
    values = prepare_injected_state(
        c, live_base_commit=c["base_commit"], project_path=None,
        task_description=c.get("task_description", ""))
    plan = values["plan"]
    st = next(s for s in plan.subtasks if s.id == "st-11-1")
    ghosts = [f for f in (st.scope.upstream_artifacts or [])
              if f.endswith("Mapper.java")]
    assert not ghosts, f"st-11-1 幽灵上游账未清（回放轮连坐 72 的死因本体）: {ghosts}"
    ghosts_r = [f for f in (st.scope.readable or []) if f.endswith("Mapper.java")]
    assert not ghosts_r, f"st-11-1 readable 幽灵引用未清: {ghosts_r}"
    st17 = next((s for s in plan.subtasks if s.id == "st-17-1"), None)
    if st17 is not None:
        g17 = [f for f in (st17.scope.upstream_artifacts or [])
               if f.endswith("ServiceImpl.java")]
        assert not g17, f"st-17-1 同构幽灵账未清（未连坐仅因未进失败态）: {g17}"


# ───────────── ③ dispatch 侧兜底（执行期账写者：拆分继承/梯三桩）─────────────

def test_dispatch_reconcile_covers_execution_time_writers():
    """dispatch 前对本批子任务跑同一 helper：执行期新写的幽灵账（如重拆继承）
    也必须被兜住（规划收尾期只护得住规划期写者）。"""
    from swarm.brain.nodes.dispatch import _reconcile_dispatch_accounts
    a = _st("st-a", create=[_XML], ua=[_JAVA])
    b = _st("st-b", create=[_JAVA], depends=["st-a"])
    plan = _plan([a, b])
    changed = _reconcile_dispatch_accounts(plan, [a])
    assert changed is True, "剔账发生须返回 changed=True（调用方据此显式 emit plan）"
    assert _JAVA not in (a.scope.upstream_artifacts or [])
    assert _reconcile_dispatch_accounts(plan, [a]) is False, "幂等：二次调用零变更"
