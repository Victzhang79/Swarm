"""32 号文 A5-M1 治本锁：规划期两个账（`oversized_subtask_ids` / `invest_fail_count`）
必须进人工闸 payload。

**病根**：`oversized_subtask_ids` 的声明（`brain/state.py:310`）自己写着"需人工提示"，
而 `_deliver_review_payload` 里**根本没有这一块** ⇒ 声明是过期承诺。
同族 `invest_fail_count` 有 LangSmith 上报（`tracing.py:279`）但同样零 state 消费者。
两者都是血规 10④"新账没有消费者＝没造"的形态。

**为什么治法是接线而非删键**（三条依据，逐条可核）：
1. 声明的原意是"需人工提示"——删键等于把声明的承诺也一起删掉；
2. payload 里已有**四个**同形态先例（`merge_owner_drops` / `needs_review` /
   `partial_test_coverage` / A5-H1 的 `ingest`），全是"写一处读零处 ⇒ 接进人工闸"；
3. 两键的生命周期是 `round`＝last-write-wins + **无条件 emit**（`planning_nodes.py:2995`
   空态也写）⇒ deliver 时拿到的是**最后一轮 elaborate** 的值，即真正被派发的那份 plan，
   不是陈旧快照。（若是条件 emit，上一轮的账会粘滞成"本轮也超预算"，那才不该直接接。）

语义是**如实呈现、不阻断**（同 needs_review）：超预算子任务多半后续 L1 自己失败暴露，
但"拆不下去还是派了"这件事人工有权在放行前看到。
"""
from __future__ import annotations


def test_deliver_payload_exposes_planning_ledger():
    """★核心锁★ 人工闸必须看得见规划期两个账（此前 payload 无此块 ⇒ 人工是盲的）。"""
    from swarm.brain.nodes import _deliver_review_payload

    payload = _deliver_review_payload({
        "oversized_subtask_ids": ["st-3", "st-7"],
        "invest_fail_count": 2,
    })
    blk = payload.get("planning")
    assert isinstance(blk, dict), (
        f"payload 必须含 planning 块，实得键={sorted(payload)}"
    )
    assert blk["oversized_count"] == 2, blk
    assert set(blk["oversized_subtask_ids"]) == {"st-3", "st-7"}, (
        f"必须列出具体子任务 id（人工要能定位是哪个拆不下去），实得 {blk}"
    )
    assert blk["invest_fail_count"] == 2, (
        f"INVEST 打回次数必须可见（同族账，同样零消费者），实得 {blk}"
    )


def test_planning_block_tolerates_missing_keys():
    """旧 checkpoint 无这些键 → 空缺省，绝不抛。

    deliver 是 interrupt 锚点，payload 组装失败＝**人工闸打不开**（比看不见账更坏）。
    """
    from swarm.brain.nodes import _deliver_review_payload

    blk = _deliver_review_payload({}).get("planning")
    assert blk == {"oversized_subtask_ids": [], "oversized_count": 0,
                   "invest_fail_count": 0}, blk


def test_planning_block_survives_dirty_types():
    """脏类型（None / 非序列 / 非数字）不得让 payload 组装抛异常。

    ★为什么单独锁★ 这两个键来自 LLM 驱动的规划期节点，历轮实测 state 里出现过
    `None` 与字符串态（`str` 被 `list()` 拆成脏账是本仓点名过的坑）。
    payload 组装抛异常＝人工闸打不开，故必须容错。
    """
    from swarm.brain.nodes import _deliver_review_payload

    blk = _deliver_review_payload({
        "oversized_subtask_ids": None,
        "invest_fail_count": None,
    }).get("planning")
    assert blk == {"oversized_subtask_ids": [], "oversized_count": 0,
                   "invest_fail_count": 0}, blk

    # 非数字的 invest_fail_count 不得炸（int(None) 已由 `or 0` 兜，这里钉字符串数字）
    blk2 = _deliver_review_payload({"invest_fail_count": "3"}).get("planning")
    assert blk2["invest_fail_count"] == 3, blk2


def test_oversized_ids_are_capped_not_unbounded():
    """限量：payload 会经 SSE 透传，超预算清单不得无界膨胀。

    形状与邻居一致（`_DELIVER_ASSERT_ROWS_MAX`）。同时断**总数仍如实**——
    截断的是展示清单，`oversized_count` 必须是真实总数，否则人工会以为只有这么几个
    （"截断把总数也截了"是本仓出现过的形态）。
    """
    from swarm.brain.nodes import _DELIVER_ASSERT_ROWS_MAX, _deliver_review_payload

    _n = _DELIVER_ASSERT_ROWS_MAX + 5
    blk = _deliver_review_payload({
        "oversized_subtask_ids": [f"st-{i}" for i in range(_n)],
    }).get("planning")
    assert len(blk["oversized_subtask_ids"]) == _DELIVER_ASSERT_ROWS_MAX, blk
    assert blk["oversized_count"] == _n, (
        f"★总数必须如实★ 展示清单可截断，计数不能——实得 count={blk['oversized_count']} 应为 {_n}"
    )


def test_lifecycle_is_round_so_payload_value_is_not_stale():
    """两键必须登记为 `round` 生命周期——这是"payload 值不陈旧"的前提。

    ★这条锁的是治法的**前提**，不是治法本身★ 若哪天有人把它们改成条件 emit 或换成
    `monotonic`，deliver 上看到的就可能是**上一轮**的超预算清单（粘滞账），
    那时"如实呈现"就变成了误导人工。改生命周期的人必须在这里被拦一次。
    """
    from swarm.brain.state import ACCOUNTING_KEY_LIFECYCLE

    for _k in ("oversized_subtask_ids", "invest_fail_count"):
        assert ACCOUNTING_KEY_LIFECYCLE.get(_k) == "round", (
            f"{_k} 的生命周期必须是 round（last-write-wins + 无条件 emit），"
            f"否则 deliver payload 会展示陈旧账。实得 {ACCOUNTING_KEY_LIFECYCLE.get(_k)}"
        )
