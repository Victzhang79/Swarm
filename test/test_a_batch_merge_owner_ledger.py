"""26 号文 A 路：C-4 merge owner 裁决整份丢弃非 owner 版本，全仓零机读账。

深扫把它标为"下一轮最可能炸的点"——不是因为裁决错（裁决本身有正当理由），
而是因为**丢弃这件事没有账**：出事时无从查证"这个文件当初用了谁的版本、丢了谁的"。
"""
from __future__ import annotations

import inspect

from swarm.brain.merge_engine import MergeResult, merge_diffs

_NEW_A = """diff --git a/src/Svc.java b/src/Svc.java
new file mode 100644
--- /dev/null
+++ b/src/Svc.java
@@ -0,0 +1,3 @@
+class Svc {
+  void a() {}
+}
"""

_NEW_B = """diff --git a/src/Svc.java b/src/Svc.java
new file mode 100644
--- /dev/null
+++ b/src/Svc.java
@@ -0,0 +1,4 @@
+class Svc {
+  void a() {}
+  void b() {}
+}
"""


def _merge(owner):
    # base_reader 返回 None = merge base 里没有该文件 → 走【新文件专路】（owner 裁决在这条路上）。
    # 不传 base_reader 时新旧由保守判定落到 modify 通用路，构造不出本用例的场景。
    return merge_diffs([("st-1", _NEW_A), ("st-2", _NEW_B)],
                       base_reader=lambda f: None,
                       file_owner=lambda f: owner)


def test_owner_drop_is_recorded_machine_readably():
    """★丢弃必须留【机读】账（26 号文 C-4）★
    owner 通道整份丢弃非 owner 写者的版本且**刻意不进 rebase**——理由正当（它们只是
    确定性修复"碰过"该文件，重做多少次还会被碰到，那正是 rebase 不收敛的根源）。
    但此前全仓只有一行 WARNING：复盘要靠 grep 日志，而日志会轮转（V3 取证污染同一课）。
    更要紧的是 owner 判据来自 **plan 声明的写权**——plan 声明错时被丢的可能正是真产出。"""
    r = _merge("st-1")
    assert r.owner_drops, "owner 裁决丢件必须落账"
    d = r.owner_drops[0]
    assert d["file"] == "src/Svc.java"
    assert d["owner"] == "st-1"
    assert d["dropped"] == ["st-2"]
    assert d["dropped_lines"] > 0, "丢了多少内容必须可量化"


def test_no_owner_evidence_still_goes_to_rebase():
    """既有 D2 铁律不削弱：无 owner 证据 → 退回拓扑选 + 落选者进 rebase 重生成
    （"非选中写者不再静默丢弃"），此时不该产生 owner_drops 账。"""
    r = _merge(None)
    assert r.owner_drops == []
    assert "st-2" in r.rebase_subtask_ids or "st-1" in r.rebase_subtask_ids


def test_single_writer_produces_no_ledger_noise():
    """单写者是绝大多数情形——绝不能凭空产生丢件账（否则 degraded_reasons 天天有噪声，
    真丢件反而被淹）。"""
    r = merge_diffs([("st-1", _NEW_A)], base_reader=lambda f: None,
                    file_owner=lambda f: "st-1")
    assert r.owner_drops == []


def test_merge_result_defaults_are_safe():
    """旧 checkpoint / 其它构造点不带该字段 → 默认空列表，消费侧 getattr 不炸。"""
    assert MergeResult(merged_diff="").owner_drops == []


def test_merge_node_writes_ledger_unconditionally():
    """★条件 emit = 陈旧持久化（ACCOUNTING_KEY_LIFECYCLE 血泪）★
    LangGraph 对缺席键保留旧值：clean 路径若不写 []，上一轮的丢件账会粘滞成
    "这轮也丢了"，人工闸看到的是一份过期的丢件清单。"""
    from swarm.brain import nodes
    src = inspect.getsource(nodes.merge)
    assert 'out["merge_owner_drops"] = _owner_drops' in src
    assert '**({"merge_owner_drops"' not in src, "绝不能改成条件 emit"


def test_ledger_reaches_degraded_reasons_and_delivery_payload():
    """账要有人消费才叫账（复核盲区之一：新账必须有人消费）。
    两个消费面：degraded_reasons（机读/学习面）+ 交付 payload（人工闸）。"""
    from swarm.brain import nodes
    assert "merge_owner_drop:" in inspect.getsource(nodes.merge)
    assert "merge_owner_drops" in inspect.getsource(nodes._deliver_review_payload)


def test_state_key_declared_with_lifecycle():
    """★LangGraph 未在 schema 声明的键会被静默丢弃★（CLAUDE.md 明列的头号血泪）"""
    from swarm.brain.state import ACCOUNTING_KEY_LIFECYCLE, BrainState
    assert "merge_owner_drops" in BrainState.__annotations__
    assert ACCOUNTING_KEY_LIFECYCLE["merge_owner_drops"] == "round"
