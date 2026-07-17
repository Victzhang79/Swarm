"""R65REPLAY-T1（task #66）反噬面：消费边=软序边，连坐不穿透、死产者可越过。

round65d 回放轮 C 路反事实实测：#57 消费边（readable→创建者）把 st-11 死点的连坐
闭包从 15 放大到 72（仅去掉 st-2 上 4 条新增边，failed 闭包 68→11）——97 任务 74%
陪葬。病根=一条"我想读你将建的文件"的排序边，被 _transitive_abandon/_is_ready 当成
"没有你我构建必失败"的硬依赖：生产者死 → 消费者及其全部下游整链判死。

治本（栈中立，零簿记）：边的软硬【结构性判定】而非存储标记（存储方案在重拆
remap/plan rebuild/注入重推导处必漏——st-2→st-11-1 的软边被 remap 成 →分片就是实锤）：
  边 B→A 为【软序边】 ⇔ A 产出(create∪writable) ∩ B.upstream_artifacts = ∅
                        且 A.create_files ∩ B.readable ≠ ∅
                        且 A.writable    ∩ B.readable = ∅   （复核 F1 收紧）
  （ua 有交=seed 构建输入=硬；readable∩writable 有交=读存量文件改造后版本、死后盘上
  留旧版=硬；零交集=LLM/脚手架结构边，理由未知，保守判硬。）
软边语义：①_transitive_abandon 连坐闭包不穿透软边；②生产者已放弃且无产出时，
软边视为已满足（消费者可越过尝试——readable 幻影由 R49-2 运行期剔除，L1 裁决）；
③生产者活着（未完成未放弃）时软边照常排序等待——时序价值保留，只废连坐代价。
"""
from __future__ import annotations

from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

_F = "mod/src/main/resources/m/AMapper.xml"
_G = "mod/src/main/java/com/x/Svc.java"


def _st(sid, *, create=None, writable=None, readable=None, ua=None, depends=None):
    return SubTask(
        id=sid, description=f"task {sid}", difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(create_files=create or [], writable=writable or [],
                        readable=readable or [], upstream_artifacts=ua or []),
        depends_on=depends or [])


def _plan(subs):
    return TaskPlan(subtasks=subs, parallel_groups=[[s.id for s in subs]])


# ───────── ① 连坐闭包不穿透软边 ─────────

def test_transitive_abandon_stops_at_soft_edge():
    """★反噬本体★：readable 驱动的消费边不得把生产者之死传染给消费者及其下游。"""
    from swarm.brain.nodes.planning_core import _transitive_abandon
    a = _st("st-a", create=[_F])
    b = _st("st-b", create=[_G], readable=[_F], depends=["st-a"])   # 软：只想读
    c = _st("st-c", create=["mod/C.java"], readable=[_G], ua=[_G], depends=["st-b"])  # 硬
    closed = _transitive_abandon([a, b, c], {"st-a"})
    assert closed == {"st-a"}, \
        f"软序边穿透连坐=回放轮 15→72 爆炸半径复发: {sorted(closed)}"


def test_transitive_abandon_hard_edge_still_propagates():
    """ua 声明的构建输入（seed 闸消费）仍是硬依赖——生产者死则消费者死，语义不变。"""
    from swarm.brain.nodes.planning_core import _transitive_abandon
    a = _st("st-a", create=[_F])
    b = _st("st-b", create=[_G], readable=[_F], ua=[_F], depends=["st-a"])
    c = _st("st-c", create=["mod/C.java"], depends=["st-b"])        # 零交集=结构边=硬
    closed = _transitive_abandon([a, b, c], {"st-a"})
    assert closed == {"st-a", "st-b", "st-c"}, f"硬依赖连坐语义被误废: {sorted(closed)}"


def test_transitive_abandon_dangling_producer_stays_hard():
    """悬空生产者（重拆后旧 id 仍在放弃集但已不在 plan）无法判软 → 保守硬（旧行为）。"""
    from swarm.brain.nodes.planning_core import _transitive_abandon
    b = _st("st-b", create=[_G], readable=[_F], depends=["st-ghost"])
    closed = _transitive_abandon([b], {"st-ghost"})
    assert "st-b" in closed


def test_transitive_abandon_completed_still_exempt():
    """R51-1 语义保留：已完成者绝不入闭包（软硬判定不得破坏该先例）。"""
    from swarm.brain.nodes.planning_core import _transitive_abandon
    a = _st("st-a", create=[_F])
    b = _st("st-b", create=[_G], ua=[_F], depends=["st-a"])
    closed = _transitive_abandon([a, b], {"st-a"}, completed_ids={"st-b"})
    assert closed == {"st-a"}


# ───────── ② 死产者软边可越过（就绪面）─────────

def test_dispatch_batch_soft_dep_on_dead_producer_is_ready():
    """生产者已放弃且无产出：软边消费者必须可派发（否则死等语义借就绪面复活）。"""
    a = _st("st-a", create=[_F])
    b = _st("st-b", create=[_G], readable=[_F], depends=["st-a"])
    plan = _plan([a, b])
    batch = plan.get_dispatch_batch(set(), ["st-b"], 4, abandoned={"st-a"})
    assert [t.id for t in batch] == ["st-b"], \
        "软边消费者被死产者永久扣死=连坐治了就绪面没治"


def test_dispatch_batch_hard_dep_on_dead_producer_not_ready():
    a = _st("st-a", create=[_F])
    b = _st("st-b", create=[_G], readable=[_F], ua=[_F], depends=["st-a"])
    plan = _plan([a, b])
    batch = plan.get_dispatch_batch(set(), ["st-b"], 4, abandoned={"st-a"})
    assert batch == [], "硬依赖死产者仍派发=派 worker 去 seed 闸必死"


def test_dispatch_batch_alive_producer_still_orders_soft_edge():
    """生产者活着（未完成未放弃）：软边照常等待——时序价值必须保留（round65d
    头排 BLOCKED 白跑就是没这条序）。"""
    a = _st("st-a", create=[_F])
    b = _st("st-b", create=[_G], readable=[_F], depends=["st-a"])
    plan = _plan([a, b])
    batch = plan.get_dispatch_batch(set(), ["st-a", "st-b"], 4)
    assert [t.id for t in batch] == ["st-a"], \
        f"软边不该在生产者活着时被越过: {[t.id for t in batch]}"


def test_get_ready_tasks_soft_dead_producer_ready():
    """get_ready_tasks 与 get_dispatch_batch 同语义（两处就绪判定不许分叉）。"""
    a = _st("st-a", create=[_F])
    b = _st("st-b", create=[_G], readable=[_F], depends=["st-a"])
    plan = _plan([a, b])
    ready = plan.get_ready_tasks(set(), abandoned={"st-a"})
    assert [t.id for t in ready] == ["st-b"]


def test_get_ready_tasks_completed_producer_unchanged():
    """完成态生产者：软硬边一律照常就绪（回归锁）。"""
    a = _st("st-a", create=[_F])
    b = _st("st-b", create=[_G], readable=[_F], ua=[_F], depends=["st-a"])
    plan = _plan([a, b])
    ready = plan.get_ready_tasks({"st-a"})
    assert [t.id for t in ready] == ["st-b"]


# ───────── ③ 回放形态缩微复现 ─────────

def test_replay_blast_radius_shrinks():
    """缩微 st-11 死型：4 死点 + readable 型消费者 + 其下游家族——连坐必须止于
    真硬依赖（回放实锤：去软边闭包 68→11）。"""
    from swarm.brain.nodes.planning_core import _transitive_abandon
    shards = [_st(f"st-p-{i}", create=[f"mod/res/m{i}.xml"],
                  depends=[f"st-p-{i-1}"] if i > 1 else []) for i in range(1, 5)]
    consumer = _st("st-2", create=["mod/A.java"],
                   readable=[f"mod/res/m{i}.xml" for i in range(1, 5)],
                   depends=["st-p-4"])                       # 软（remap 后形态）
    family = [_st(f"st-2-{j}", create=[f"mod/B{j}.java"],
                  ua=["mod/A.java"], depends=["st-2"]) for j in range(1, 4)]
    closed = _transitive_abandon(shards + [consumer] + family,
                                 {s.id for s in shards})
    assert closed == {s.id for s in shards}, \
        f"死点应止于分片族本体（4），实际连坐 {len(closed)}: {sorted(closed)}"


# ───────── ④ 双复核整改锁（hunter F1/F2/F4/F6）─────────

def test_writable_producer_death_stays_hard():
    """复核 F1（hunter HIGH）：消费者要读的是生产者【writable 改造】的存量文件——
    生产者死后盘上留旧版（R49-2 只查存在性兜不住），越过=静默拿旧接口写代码 → 必须硬。"""
    from swarm.brain.nodes.planning_core import _transitive_abandon
    w = _st("st-w", writable=[_G])                       # 改造存量文件
    b = _st("st-b", create=["mod/B.java"], readable=[_G], depends=["st-w"])
    closed = _transitive_abandon([w, b], {"st-w"})
    assert closed == {"st-w", "st-b"}, \
        f"writable 生产者之死必须硬连坐（陈旧读危险面）: {sorted(closed)}"
    plan = _plan([w, b])
    assert plan.get_dispatch_batch(set(), ["st-b"], 4, abandoned={"st-w"}) == [], \
        "writable 生产者死后消费者被派发=静默陈旧读"


def test_dep_hit_ignores_irrelevant_dead_soft_edge():
    """复核 F2（hunter HIGH）：failure.py 内部阻断归因的 _dep_hit 口径——无关死软边
    不得把"等活生产者"的可恢复 BLOCKED 判成永久放弃。此处锁 edge_is_soft 对该形态
    的判定（软），行为面由 _dep_hit 硬边过滤消费。"""
    from swarm.types import edge_is_soft
    dead_soft = _st("st-res", create=["mod/res/tpl.html"])
    consumer = _st("st-c", create=["mod/C.java"],
                   readable=["mod/res/tpl.html"], depends=["st-res", "st-alive"])
    assert edge_is_soft(consumer, dead_soft) is True
    alive_hard = _st("st-alive", create=[_G])
    assert edge_is_soft(consumer, alive_hard) is False  # 零交集=结构边=硬


def test_det_fail_reason_empty_build_output_honest():
    """复核 F4：build_output 空时绝不让构建命令冒充诊断——如实报"无输出捕获"。"""
    from swarm.worker.executor import _det_fail_reason
    r = _det_fail_reason({"l1_2_1_build_ok": False,
                          "build_output": "",
                          "build_failed": "mvn -B clean install -pl mod -am"})
    assert "无输出捕获" in r, f"空输出时命令冒充 reason: {r}"
    assert not r.startswith("build_fail: mvn"), r


def test_norm_path_parity_with_contract_utils():
    """复核 F6：types._edge_norm_path 与 contract_utils._norm_scope_path 双实现
    必须逐字节同口径（分层约束下的刻意内联，此锁防漂移）。"""
    from swarm.brain.contract_utils import _norm_scope_path
    from swarm.types import _edge_norm_path
    for p in ["./mod/A.java", "mod\\sub\\B.xml", "/abs/C.java",
              "././x/D.java", "plain/E.java", ".hidden/F.java"]:
        assert _edge_norm_path(p) == _norm_scope_path(p), p
