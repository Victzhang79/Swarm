"""A7-M1 回归：预算账本持久化失败必须可观测，且三个"返 None"的原因分档。

治前缺陷＝**同一函数 `_load_row` 的两条失败出口后果相同、可观测性差一个数量级**：
  · 查询失败(`:239`)给 WARNING + 成文权衡理由；
  · 建表/连接失败(经 `_ensure_table` 早退)给**零日志**（上游仅 DEBUG）。
而沉默那条恰是 **PG 启动期不可达**时走的 ⇒ "账本数小时没落库 + 每次重启已花额度归零"
在运维面上与正常运行【逐字一致】。

诚实边界（写清以免被当成"预算闸失效"）：闸本体在内存 `_entries`，PG 只做持久化
⇒ 单进程生命周期内闸照常生效。本条治的是可观测性，不是闸的正确性。

★本文件的区分力核心＝`test_absent_row_is_not_a_degrade`★：`_load_row` 返 None 的**第三个**
原因是"全新任务本来就没有行"，那是正常情形而非降级。缺这条，"每次返 None 都记
table_unavailable" 也能让其余用例全绿，而那会把每个新任务都误报成账本故障。

失败注入走 `_pool` 这个模块自有 IO 缝（patch 它 = 模拟 PG 不可达）；"无该行"一例用**真 PG**
（真连接、真查询、真的没有那行），不用假 pool——假 pool 的 fetchone 返 None 是我自己编的，
证不了真库上的同一路径。
"""

from __future__ import annotations

import logging
import uuid

import pytest

from swarm.infra.degrade import degrade_counts, reset_degrade_counts
from swarm.models import ledger

pytestmark = pytest.mark.needs_service("pg")

_TABLE_KEY = "models.ledger.table_unavailable"
_QUERY_KEY = "models.ledger.load_query_failed"
_FLUSH_KEY = "models.ledger.flush_failed"


@pytest.fixture(autouse=True)
def _clean():
    """每例清零内存态 + degrade 计数（计数是进程全局的，不清则跨用例累加）。"""
    ledger._reset_for_tests()
    reset_degrade_counts()
    yield
    ledger._reset_for_tests()
    reset_degrade_counts()


def _break_pool(monkeypatch):
    """把 PG 打成不可达。"""
    def _raise():
        raise RuntimeError("PG 不可达（夹具注入）")
    monkeypatch.setattr(ledger, "_pool", _raise)


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


def _row(task_id: str) -> dict:
    return {
        "cloud_tokens_in": 1, "cloud_tokens_out": 2, "local_tokens": 3,
        "llm_calls": 4, "wall_ms": 5, "replan_rounds": 0,
        "stage_spent": {}, "budget_total": 100, "seq": 1,
    }


# ── ① 建表/连接失败：WARNING（非 DEBUG）+ 机读键 ──

def test_table_unavailable_warns_and_counts(monkeypatch, caplog):
    """治前是零日志（上游仅 DEBUG）——本条锁"必须有 WARNING 且有机读键"。"""
    _break_pool(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="swarm.models.ledger"):
        assert ledger._ensure_table() is False

    warns = _warnings(caplog)
    assert warns, f"建表失败必须留 WARNING，实得等级 {[r.levelname for r in caplog.records]}"
    assert degrade_counts().get(_TABLE_KEY) == 1, degrade_counts()


def test_load_row_table_unavailable_is_observable(monkeypatch, caplog):
    """★接线证明★：观测点放在 `_ensure_table` 内，`_load_row` 的早退路径必须继承它。

    治前 `_load_row:212` 那个 `return None` 处零日志——这条走真实调用方入口，
    而不是直接调 `_ensure_table`（只测后者会漏掉"调用点没被覆盖"这一整类失效）。
    """
    _break_pool(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="swarm.models.ledger"):
        assert ledger._load_row("t-a7m1") is None

    assert _warnings(caplog), "经 _load_row 触发的建表失败也必须留 WARNING"
    assert degrade_counts().get(_TABLE_KEY) == 1, degrade_counts()


def test_flush_row_table_unavailable_is_observable(monkeypatch, caplog):
    """同上，另一个早退调用方 `_flush_row:250`——两个调用点都得被覆盖。"""
    _break_pool(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="swarm.models.ledger"):
        assert ledger._flush_row("t-a7m1", _row("t-a7m1")) is False

    assert _warnings(caplog), "经 _flush_row 触发的建表失败也必须留 WARNING"
    assert degrade_counts().get(_TABLE_KEY) == 1, degrade_counts()


# ── warn-once：日志节流但【计数不节流】──

def test_warning_throttled_but_counter_is_not(monkeypatch, caplog):
    """WARNING 只一条（不刷屏），计数每次都涨——否则"挂了多久"在指标面不可量化。

    ★节流的作用域＝启动期（首次建表成功之前）★，由下方 `_table_ready is False` 断言钉住：
    `_table_ready` 是单向闩，成功后 `_ensure_table` 的 except 再也到不了，所以本条覆盖的是
    "启动期 PG 不可达"这个窗口。成功【之后】的故障不节流，见
    `test_after_table_ready_every_failure_warns_unthrottled`——两条合起来才说清边界在哪。
    """
    _break_pool(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="swarm.models.ledger"):
        for _ in range(3):
            ledger._ensure_table()
        assert ledger._table_ready is False, "夹具前提：本窗口内建表始终没成功"

    assert len(_warnings(caplog)) == 1, (
        f"warn-once 失效，得 {len(_warnings(caplog))} 条 WARNING"
    )
    assert degrade_counts().get(_TABLE_KEY) == 3, (
        f"计数被一起节流了（那样就看不出持续时长），得 {degrade_counts()}"
    )


def test_after_table_ready_every_failure_warns_unthrottled(monkeypatch, caplog):
    """★建表成功之后的 PG 故障必须【每次都】WARNING（不节流）★

    这是"第二次故障仍可见"的**真实**承载路径，也是本条治法里唯一生产可达的那条：
    `_table_ready` 是单向闩（生产侧无任何路径置回 False）⇒ 首次建表成功后
    `_ensure_table` 的 except 分支再也到不了 ⇒ warn-once 只覆盖启动期那个窗口。
    此后的故障全部走 `_load_row`/`_flush_row` 的 except，那两条 WARNING 不节流。

    ★本条是双复核整改后的替代锁★：原先这里锁的是"恢复后闩被清零、下次故障再告警一次"，
    而那个性质**生产不可达**（我在成功分支加的解闩，其设的值永远不会再被读）——
    锁一个生产到不了的状态＝给自己发一张空头背书。现在锁的是生产真会走的那条。
    """
    ledger._table_ready = True          # 模拟"本进程已成功建表过"（生产上此后即单向闩）
    _break_pool(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="swarm.models.ledger"):
        for _ in range(3):
            ledger._load_row("t-a7m1")

    assert len(_warnings(caplog)) == 3, (
        "建表成功后的每次读账失败都必须留 WARNING（不节流）——这是第二次故障的唯一可见性来源，"
        f"得 {len(_warnings(caplog))} 条"
    )
    assert degrade_counts().get(_QUERY_KEY) == 3, degrade_counts()




# ── ② 查询失败：与①不同因、不同键 ──

def test_load_query_failure_has_its_own_key(monkeypatch, caplog):
    """表在而 SELECT 抛 ⇒ 记 load_query_failed，**不**记 table_unavailable。"""
    ledger._table_ready = True          # 表已就绪 ⇒ 跳过 _ensure_table，隔离出查询失败路径
    _break_pool(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="swarm.models.ledger"):
        assert ledger._load_row("t-a7m1") is None

    counts = degrade_counts()
    assert counts.get(_QUERY_KEY) == 1, f"查询失败应有独立机读键，得 {counts}"
    assert _TABLE_KEY not in counts, f"分档失效：查询失败被记成通道不可用，{counts}"
    assert _warnings(caplog), "查询失败的 WARNING（原有）不应被改掉"


# ── ③ 无该行 = 正常，不是降级（★区分力核心★）──

def test_absent_row_is_not_a_degrade(caplog):
    """全新任务本来就没有账本行：返 None 但【不记任何键、不打 WARNING】。

    缺这条锁，"凡返 None 就记 table_unavailable" 也能让上面全部用例通过，
    而那会把每一个新任务都误报成账本故障（指标彻底失去意义）。
    真 PG + 随机 task_id：真连接、真查询、真的没有那行。
    """
    with caplog.at_level(logging.DEBUG, logger="swarm.models.ledger"):
        assert ledger._load_row(f"_test_a7m1_absent_{uuid.uuid4().hex}") is None

    counts = degrade_counts()
    assert _TABLE_KEY not in counts, f"新任务无行被误记成通道不可用：{counts}"
    assert _QUERY_KEY not in counts, f"新任务无行被误记成查询失败：{counts}"
    assert not _warnings(caplog), (
        f"新任务无行是正常情形，不该有 WARNING：{[r.message for r in _warnings(caplog)]}"
    )


# ── ③' 落库失败的机读键 ──

def test_flush_failure_has_its_own_key(monkeypatch, caplog):
    """表在而 upsert 抛 ⇒ flush_failed。重试循环是 `except: pass`，无此键则
    "已连续失败 N 小时" 只能靠人翻日志。"""
    ledger._table_ready = True
    _break_pool(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="swarm.models.ledger"):
        assert ledger._flush_row("t-a7m1", _row("t-a7m1")) is False

    counts = degrade_counts()
    assert counts.get(_FLUSH_KEY) == 1, f"落库失败应有独立机读键，得 {counts}"
    assert _TABLE_KEY not in counts, f"分档失效：落库失败被记成通道不可用，{counts}"


# ── 键必须真被 /api/metrics 那条现成通道消费（非新造账）──

def test_keys_surface_on_metrics_channel(monkeypatch):
    """三个键必须出现在 `degrade_counts()` 快照里——那正是 api/app.py:1421 导出
    `swarm_degrade_total{category}` 时读的函数。锁"接了现成通道"，不是造了新账。"""
    _break_pool(monkeypatch)
    ledger._load_row("t-a7m1")              # ① table_unavailable
    ledger._table_ready = True
    ledger._load_row("t-a7m1")              # ② load_query_failed
    ledger._flush_row("t-a7m1", _row("t"))  # ③ flush_failed

    counts = degrade_counts()
    for key in (_TABLE_KEY, _QUERY_KEY, _FLUSH_KEY):
        assert key in counts, f"{key} 未出现在 metrics 消费的快照里：{counts}"
