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


def test_dotfile_owner_claim_survives_normalization(monkeypatch):
    """★#29-8 M-3★ claims 归一化只剥字面 './' 前缀——`.gitignore` 绝不被字符集
    `lstrip("./")` 剥成 `gitignore`（diff 解析侧保留点前缀 ⇒ 两侧键永不相等 ⇒
    R57-6 owner 保护对 dotfile 全族静默失效，退回拓扑选+rebase）。"""
    from swarm.brain import nodes
    from swarm.types import FileScope, SubTask, TaskPlan, WorkerOutput

    def _dot(content):
        return ("diff --git a/.gitignore b/.gitignore\n"
                "new file mode 100644\n--- /dev/null\n"
                "+++ b/.gitignore\n@@ -0,0 +1,1 @@\n+" + content + "\n")

    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="d",
                scope=FileScope(writable=[".gitignore"]), depends_on=[]),
        SubTask(id="st-2", description="d",
                scope=FileScope(writable=["x.java"]), depends_on=[]),
    ])
    state = {
        "plan": plan,
        "subtask_results": {
            "st-1": WorkerOutput(subtask_id="st-1", diff=_dot("target/"), summary="",
                                 l1_passed=True, l1_details={}, confidence="high"),
            "st-2": WorkerOutput(subtask_id="st-2", diff=_dot("*.class"), summary="",
                                 l1_passed=True, l1_details={}, confidence="high"),
        },
    }
    monkeypatch.setattr(nodes, "_make_base_reader", lambda s: (lambda f: None))
    monkeypatch.setattr("swarm.brain.merge_engine.verify_merged_patch_applies",
                        lambda *a, **k: (True, ""))
    out = nodes.merge(state)
    drops = out.get("merge_owner_drops") or []
    assert any(d["file"] == ".gitignore" and d["owner"] == "st-1"
               and d["dropped"] == ["st-2"] for d in drops), \
        f"dotfile 的 owner 裁决必须生效（而非退回拓扑选+rebase）: {drops}"
    assert not out.get("rebase_subtask_ids"), "owner 证据在案绝不退 rebase"
    assert "target/" in out["merged_diff"] and "*.class" not in out["merged_diff"]


# ══════════════════════════════════════════════
# C-4 的【真数据丢失面】：新建聚合清单并集不可达
# ══════════════════════════════════════════════

def _pom(dep, artifact="alarm-task"):
    body = ["<project>", f"  <artifactId>{artifact}</artifactId>", "  <dependencies>",
            f"    <dependency>{dep}</dependency>", "  </dependencies>", "</project>"]
    return (f"diff --git a/{artifact}/pom.xml b/{artifact}/pom.xml\n"
            "new file mode 100644\n--- /dev/null\n"
            f"+++ b/{artifact}/pom.xml\n@@ -0,0 +1,{len(body)} @@\n"
            + "".join("+" + ln + "\n" for ln in body))


def _merge_pom(*diffs, owner="st-1"):
    return merge_diffs([(f"st-{i + 1}", d) for i, d in enumerate(diffs)],
                       base_reader=lambda f: None, file_owner=lambda f: owner)


def test_new_manifest_multiwriter_unions_instead_of_dropping():
    """★这才是 C-4 的真数据丢失面（26 号文原文）★
    实跑：两写者各向【新建】 alarm-task/pom.xml 加不同 `<dependency>` → owner 独占、
    另一份整份蒸发，写者账面仍 DONE。而同一 pom 若 base 已存在，走的是
    `merge_insert_only_changes(allow_anchor_union=True)` 正确并集——
    **新文件专路在任何 3-way/union 之前 continue，并集机制对新建模块 pom 结构性不可达**。
    真实日志该分支命中 17 次，落点 100% 是 module pom，co-writer 多达 8 个。"""
    r = _merge_pom(_pom("alarm-core"), _pom("alarm-notify"))
    assert "alarm-core" in r.merged_diff
    assert "alarm-notify" in r.merged_diff, "非 owner 写者加的依赖绝不能凭空蒸发"


def test_union_success_records_unions_not_drops():
    """★#29-8 H-1★ 并集成功=一行没丢，账必须记【事实】：
    owner_drops 必须为空（否则 degraded_reasons 冤杀 L6 should_write_success、
    人工闸看到假丢件、M-6 auto_accept 闸被冤触发），并集事件记进 owner_unions。"""
    r = _merge_pom(_pom("alarm-core"), _pom("alarm-notify"))
    assert r.owner_drops == [], "并集成功一行没丢，绝不允许记丢件假账"
    assert len(r.owner_unions) == 1
    rec = r.owner_unions[0]
    assert rec["file"] == "alarm-task/pom.xml"
    assert rec["owner"] == "st-1"
    assert rec["unioned"] == ["st-2"]


def test_unioned_manifest_stays_structurally_valid():
    """并集绝不能产出畸形清单（两个 `<project>` 根会让 git apply 整包连坐）。"""
    r = _merge_pom(_pom("a"), _pom("b"), _pom("c"))
    body = [ln[1:] for ln in r.merged_diff.splitlines() if ln.startswith("+")
            and not ln.startswith("+++")]
    assert body.count("<project>") == 1 and body.count("</project>") == 1
    assert sum(1 for ln in body if "<dependency>" in ln) == 3, "三个写者的条目都要在"


def test_union_falls_back_when_other_adds_nothing_new():
    """★救不回来就如实认，绝不产畸形★ 并集函数对【另一版不带来任何新内容】
    （merged==owner 版）返回 None → 回退 owner 独占，丢件账仍记（不静默）。

    ★#29-8 H-1 订正★：本用例原夹具（`<完全不同的骨架/>` 版）声称触发重复单例守卫，
    实测从未触发——并集函数对它是【成功并集】（骨架行被并进结果），而旧代码在并集
    成功后照样记 drops，断言 `r.owner_drops` 恰被这个 bug 喂绿（夹具形状没编码承诺）。
    重复单例守卫本身的覆盖在 test_merge_aggregate_malformed_round18.py（两条），
    此处改用【真回退】夹具（子集版）锁「回退时 drops 有账、unions 无账」。"""
    full = _pom("alarm-core").replace("+</project>", "+    <dependency>alarm-notify</dependency>\n+</project>")
    # owner=全量版（st-1），st-2 是它的真子集（只含 alarm-core，不带来新条目）。
    subset = _pom("alarm-core")
    r = merge_diffs([("st-1", full), ("st-2", subset)],
                    base_reader=lambda f: None, file_owner=lambda f: "st-1")
    body = [ln[1:] for ln in r.merged_diff.splitlines() if ln.startswith("+")
            and not ln.startswith("+++")]
    assert body.count("<project>") == 1, "宁可丢件也绝不产出重复根标签"
    assert r.owner_drops, "回退 owner 独占时丢件账必须仍在"
    assert r.owner_unions == [], "并集失败（回退独占）不得记并集账（#29-8 H-1 分账）"


def test_non_manifest_new_file_keeps_owner_only():
    """并集只对【聚合清单】开（条目并存是它的语义）；普通源码同名新文件仍 owner 独占
    ——两份 Java 类拼在一起是畸形，绝不能并。"""
    java = ("diff --git a/A.java b/A.java\nnew file mode 100644\n--- /dev/null\n"
            "+++ b/A.java\n@@ -0,0 +1,1 @@\n+class A { void x() {} }\n")
    java2 = java.replace("void x()", "void y()")
    r = _merge_pom(java, java2)
    assert "void x()" in r.merged_diff and "void y()" not in r.merged_diff
    assert r.owner_drops, "丢件必须记账"
    assert r.owner_unions == [], "非清单文件不并集，不得记并集账（#29-8 H-1 分账）"
