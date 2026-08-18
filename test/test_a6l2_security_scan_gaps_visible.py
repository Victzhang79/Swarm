"""32 号文 A6-L2 治本锁：安全扫描覆盖缺口必须进人工闸 payload。

**病根**：`security_scan_skipped_tools` / `security_scan_categories_ran`
（`brain/nodes/audit_node.py:132-133`）**全仓零读点**，而其来源 docstring
（`worker/security_scan.py:81-82`）声明这两个键"供 progress/metrics/审计端消费"
——三个消费者一个都不存在＝血规 10④"新账没有消费者＝没造"。

**为什么治法是接线而非删键**（与同批 A6-L3 的"删"相反，别套同一治法）：
A6-L3 的 `compile_passed`/`tests_passed` 是**正则派生的伪权威**（确定性闸才是权威，
删它是纯收窄）；这两个键是**真实覆盖面事实**，删掉会让下面这个 fail-open 彻底隐形——

  阻断模式下 secret 类的 per-category 哨兵（`security_scan.py:170-204`）**刻意近于
  不触发**：内置正则恒跑（`:1238`，30 号文 C-2 治本）即置 `secret_ran` ⇒ gitleaks 与
  trufflehog 双缺失时，"只跑了单行正则"与"跑了 entropy + git 历史"在账面上**都叫
  secret 类有覆盖**。`:168-169` 的注释把"哨兵不触发"的正当性明确寄托在**工具级缺口
  可见**上，而那份可见性此前不存在——本批就是补它。

★接手提示词写的 fail-open 形状（"bandit 没装⇒by_severity 空⇒should_block False"）
与代码不符，已实读推翻★：阻断模式下任一类零覆盖会注合成 finding 并强制阻断
（`:170-204`），report-only 模式注 INFO finding（`:148`）。**类**粒度已可辨；真缺口在
**工具**粒度（类级 OR 粘滞：`_mark_ran` 首个成功即置位、永不复位）。

语义是**如实呈现、不阻断**（同 needs_review / partial_test_coverage / planning）：
`block_severity` 是运维明示旋钮，且工具级缺口在多数环境恒非空（gitleaks/trufflehog
少有预装）⇒ 据此阻断等于把一条 LOW 可观测项变成"必须装齐所有扫描器"的硬门槛。
"""
from __future__ import annotations

import asyncio
import os
import tempfile


def _audit_subtask(sid: str = "audit-1"):
    from swarm.types import FileScope, SubTask, TaskHarness, TaskIntent

    return SubTask(
        id=sid, description="安全审计", intent=TaskIntent.AUDIT,
        scope=FileScope(readable=["app.py"]), harness=TaskHarness(language="python"),
    )


def _proj() -> str:
    d = tempfile.mkdtemp(prefix="swarm_a6l2_")
    with open(os.path.join(d, "app.py"), "w") as f:
        f.write("x = 1\n")
    return d


def test_gap_flows_from_real_producer_to_human_gate(monkeypatch):
    """★核心锁·端到端★ 缺口必须从**真实生产者**一路到人工闸 payload。

    ★为什么不手工造 l1_details★ 本仓点名过的假绿形态＝"构造出生产代码从不产生的取值"。
    这条锁走真实链路：`run_security_scan` 返回 scan_details → `_run_security_audit`
    落键 → 放进 `subtask_results`（`dispatch.py:988` 的真实落点形状）→
    `_deliver_review_payload` 聚合。中间任何一跳断了这条锁就红。
    """
    from swarm.brain.nodes import _deliver_review_payload, _run_security_audit

    # gitleaks/trufflehog 双缺失 + secret 类仍"有覆盖"（内置正则恒跑置位）
    # ＝本批要治的那个真实 fail-open 形状，逐字复刻。
    def _fake_scan(project_path, language, *, files=None, block_severity="critical"):
        return ([], False, {
            "skipped_tools": ["gitleaks", "trufflehog"],
            "categories_ran": {"sast": True, "dep": True, "secret": True},
        })

    monkeypatch.setattr("swarm.worker.security_scan.run_security_scan", _fake_scan)
    out = asyncio.run(_run_security_audit(_audit_subtask(), _proj(), task_id="t"))

    # 前提断言：真实生产者确实落了这两个键（否则下面测的不是这条通道）
    assert out.l1_details.get("security_scan_skipped_tools") == ["gitleaks", "trufflehog"], (
        f"前提不成立——生产者未落键，本锁测的不是目标通道。实得 {out.l1_details}"
    )
    assert out.l1_passed is True, (
        f"前提：本夹具无高危发现应放行（否则测的是阻断路径）。实得 {out.l1_details}"
    )

    rows = _deliver_review_payload({"subtask_results": {"audit-1": out}})["security_scan_gaps"]
    assert len(rows) == 1, f"人工闸必须看见这一条缺口，实得 {rows}"
    assert rows[0]["gap"] == "partial_coverage", rows
    assert rows[0]["subtask_id"] == "audit-1", rows
    assert set(rows[0]["skipped_tools"]) == {"gitleaks", "trufflehog"}, (
        f"★必须列出具体工具名★ 缺口从类级下探到工具级正是这两个键存在的理由。实得 {rows}"
    )


def test_clean_scan_reports_no_gap(monkeypatch):
    """★区分力锁★ 工具齐全 + 各类都跑成 → 零行。

    ★为什么单独锁★ 没有这条，"恒追加一行"的实现也能让上面那条核心锁全绿
    （断言只看"有没有这一条"，不看"该没有时是不是真没有"）。
    """
    from swarm.brain.nodes import _deliver_review_payload, _run_security_audit

    def _fake_scan(project_path, language, *, files=None, block_severity="critical"):
        return ([], False, {
            "skipped_tools": [],
            "categories_ran": {"sast": True, "dep": True, "secret": True},
        })

    monkeypatch.setattr("swarm.worker.security_scan.run_security_scan", _fake_scan)
    out = asyncio.run(_run_security_audit(_audit_subtask(), _proj(), task_id="t"))
    rows = _deliver_review_payload({"subtask_results": {"audit-1": out}})["security_scan_gaps"]
    assert rows == [], f"无缺口时必须零行（否则人工闸天天见狼来了）。实得 {rows}"


def test_missing_category_is_reported_with_names(monkeypatch):
    """某类零覆盖 → 列出类名（report-only 模式下这是唯一可见处）。

    report-only（`block_severity=none`）时 `should_block` 恒 False、`l1_passed=True`，
    类级缺口只落一条 INFO finding；人工闸此前完全看不见"哪一类没扫"。
    """
    from swarm.brain.nodes import _deliver_review_payload, _run_security_audit

    def _fake_scan(project_path, language, *, files=None, block_severity="critical"):
        return ([], False, {
            "skipped_tools": ["pip-audit"],
            "categories_ran": {"sast": True, "dep": False, "secret": True},
        })

    monkeypatch.setattr("swarm.worker.security_scan.run_security_scan", _fake_scan)
    out = asyncio.run(_run_security_audit(_audit_subtask(), _proj(), task_id="t"))
    rows = _deliver_review_payload({"subtask_results": {"audit-1": out}})["security_scan_gaps"]
    assert len(rows) == 1 and rows[0]["categories_missing"] == ["dep"], (
        f"★必须列出零覆盖的类名★ 只报「有缺口」人工无法判断严重度。实得 {rows}"
    )


def test_scanner_crash_reported_though_it_has_no_categories_key(monkeypatch):
    """★1c 教训锁★ 崩溃路径**没有** `categories_ran` 键，仍必须上报。

    ★这条锁的是判据的前件，不是判据的结论★ 本批（32 号文批2）已两次踩到"判据前件
    排除整类形态"：若聚合器写成"先要求 `security_scan_categories_ran` 在场"，则
    `audit_node.py:90-95` 的崩溃路径（只落 mode/error/fail_closed 三键）与
    `:62` 的无路径路径**整类被静默排除**——而它们恰是"没扫"的**最强**形态。
    数调用点永远发现不了这种缺口，只能靠造前件不成立的夹具形状。

    可达性：report-only 模式下崩溃 `l1_passed = not _audit_fail_closed` ＝ True ⇒
    交付照常放行，而 error 串在此前零消费者 ⇒ "扫描器崩了"对人工完全隐形。
    """
    from swarm.brain.nodes import _deliver_review_payload, _run_security_audit

    def _boom(project_path, language, *, files=None, block_severity="critical"):
        raise RuntimeError("bandit segfault")

    monkeypatch.setattr("swarm.worker.security_scan.run_security_scan", _boom)
    out = asyncio.run(_run_security_audit(_audit_subtask(), _proj(), task_id="t"))

    # 前提断言：崩溃路径确实不落 categories_ran（本锁的前件就是"这个键缺席"）
    assert "security_scan_categories_ran" not in out.l1_details, (
        f"前提不成立——崩溃路径若已落该键，这条锁就测不到「前件排除整类」。实得 {out.l1_details}"
    )
    assert out.l1_details.get("error"), f"前提：崩溃应落 error 串。实得 {out.l1_details}"

    rows = _deliver_review_payload({"subtask_results": {"audit-1": out}})["security_scan_gaps"]
    assert len(rows) == 1 and rows[0]["gap"] == "scan_error", (
        f"★扫描器崩溃必须可见★ 这是「没扫」的最强形态，绝不能因缺 categories_ran 被漏掉。"
        f"实得 {rows}"
    )
    assert "bandit segfault" in rows[0]["detail"], (
        f"必须带根因（人工要能判断是工具缺失还是真崩）。实得 {rows}"
    )


def test_no_project_path_reported():
    """无可扫对象也必须可见：既有契约按"安全跳过"放行，但"一个字节没扫"人工有权知道。

    这条同样是前件不成立的形状（`l1_details` 只有 mode/skipped 两键）。
    """
    from swarm.brain.nodes import _deliver_review_payload, _run_security_audit

    out = asyncio.run(_run_security_audit(_audit_subtask(), None, task_id="t"))
    assert out.l1_passed is True and out.l1_details.get("skipped") == "no_project_path", (
        f"前提：无路径应安全跳过（既有契约）。实得 {out.l1_details}"
    )
    rows = _deliver_review_payload({"subtask_results": {"audit-1": out}})["security_scan_gaps"]
    assert len(rows) == 1 and rows[0]["gap"] == "no_project_path", (
        f"★放行但零扫描必须可见★ 实得 {rows}"
    )


def test_ordinary_failed_subtask_is_not_reported_as_security_gap():
    """★mode 闸承重锁★ 普通子任务执行失败**不得**被误报成安全缺口。

    ★为什么这条是承重的★ `dispatch.py:973-980` 对任何抛异常的子任务写
    `l1_details={"error": str(outcome)}`——与 AUDIT 崩溃路径**共用 `error` 键名**。
    若聚合器不先按 `mode == "audit"` 收口，则**每个失败的普通子任务**都会在人工闸上
    冒充一条"安全扫描崩溃"（`gap=scan_error`）⇒ 该块立刻变噪声、被人工学会忽略，
    等于把刚接上的可见性又废掉。删掉 mode 闸这条锁必红。
    """
    from swarm.brain.nodes import _deliver_review_payload
    from swarm.types import Confidence, WorkerOutput

    # 逐字复刻 dispatch.py:973-980 的失败落点形状
    failed = WorkerOutput(
        subtask_id="st-1", diff="", summary="执行失败: boom",
        confidence=Confidence.LOW, l1_passed=False,
        l1_details={"error": "boom"},
    )
    rows = _deliver_review_payload({"subtask_results": {"st-1": failed}})["security_scan_gaps"]
    assert rows == [], (
        f"普通子任务失败不是安全缺口——mode 闸必须收口，否则该块沦为噪声。实得 {rows}"
    )


def test_payload_tolerates_old_checkpoint_and_dirty_types():
    """旧 checkpoint / 脏类型 → 空缺省，绝不抛。

    deliver 是 interrupt 锚点，payload 组装失败＝**人工闸打不开**（比看不见账更坏）。
    脏类型来自真实风险：`skipped_tools` 若被写成 `str`，`list()` 会把它拆成逐字符脏账
    （本仓点名过的 R65TR 形态）。
    """
    from swarm.brain.nodes import _deliver_review_payload
    from swarm.types import Confidence, WorkerOutput

    assert _deliver_review_payload({})["security_scan_gaps"] == []
    assert _deliver_review_payload({"subtask_results": None})["security_scan_gaps"] == []

    dirty = WorkerOutput(
        subtask_id="a", diff="", summary="", confidence=Confidence.LOW, l1_passed=True,
        l1_details={"mode": "audit",
                    "security_scan_skipped_tools": "gitleaks",   # str 而非 list
                    "security_scan_categories_ran": "nope"},     # str 而非 dict
    )
    rows = _deliver_review_payload({"subtask_results": {"a": dirty}})["security_scan_gaps"]
    assert rows == [], (
        f"脏类型必须按「无法判定」处理且不抛，绝不能拆成逐字符脏账。实得 {rows}"
    )


def test_rows_are_capped():
    """限量：payload 经 SSE 透传，缺口清单不得无界膨胀（形状同邻居）。"""
    from swarm.brain.nodes import _DELIVER_ASSERT_ROWS_MAX, _deliver_review_payload
    from swarm.types import Confidence, WorkerOutput

    _n = _DELIVER_ASSERT_ROWS_MAX + 5
    results = {
        f"a-{i}": WorkerOutput(
            subtask_id=f"a-{i}", diff="", summary="", confidence=Confidence.LOW,
            l1_passed=True,
            l1_details={"mode": "audit", "security_scan_skipped_tools": ["gitleaks"],
                        "security_scan_categories_ran": {"sast": True}},
        )
        for i in range(_n)
    }
    rows = _deliver_review_payload({"subtask_results": results})["security_scan_gaps"]
    assert len(rows) == _DELIVER_ASSERT_ROWS_MAX, f"必须限量，实得 {len(rows)}"
