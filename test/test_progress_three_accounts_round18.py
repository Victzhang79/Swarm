"""round18 P2：web 进度三本账（完成/放弃/剩余）。进度只显 completed/count 会误导
"卡在 12/38",实则放弃单元不计入。_row_to_task 暴露 abandoned_subtasks + 派生 remaining。
"""
import datetime

from swarm.project.store import _TASK_SELECT, _row_to_task


def _row(subtask_count, completed, abandoned):
    """按 _TASK_SELECT 顺序构造 25 列行（0..24）。"""
    now = datetime.datetime(2026, 7, 2)
    return (
        "t-1", "p-1", "desc", "RUNNING", "complex",  # 0-4
        None, subtask_count, completed,               # 5 plan,6 count,7 completed
        None, None, None,                             # 8 human_decision,9 merged_diff,10 thread
        {}, 1.0, [], {}, None,                        # 11-15
        now, now,                                      # 16-17 created/updated
        [], False, False, "",                          # 18-21
        abandoned,                                     # 22 abandoned_subtasks
        False, "normal",                               # 23 auto_accept,24 queue_priority
    )


def test_select_has_26_columns():
    cols = [c.strip() for c in _TASK_SELECT.replace("\n", " ").split(",") if c.strip()]
    # P0-A：追加队列执行 meta 两列（auto_accept, queue_priority)；3rd#2：末尾追加 base_commit。
    # E1（阶段5）语义演进：末尾追加 retry_prev_thread_id（重试续跑锚点）→ 27 列。
    # R38-E：末尾追加 error（FAILED 终态机读账）→ 28 列。
    # R65D-T5：末尾追加 injected_plan（plan 注入 cassette）→ 29 列（_row_to_task row[28]）。
    assert cols[-7] == "abandoned_subtasks", cols
    assert cols[-6:] == ["auto_accept", "queue_priority", "base_commit",
                         "retry_prev_thread_id", "error", "injected_plan"], cols
    assert len(cols) == 29, cols


def test_three_accounts_typical():
    """38 计划:完成18 放弃19 剩余1(round18 现场口径,web 曾只显 completed=12/38 误导)。"""
    t = _row_to_task(_row(38, 18, 19))
    assert t["subtask_count"] == 38
    assert t["completed_subtasks"] == 18
    assert t["abandoned_subtasks"] == 19
    assert t["remaining_subtasks"] == 1


def test_remaining_never_negative():
    """夹紧:completed+abandoned 超过 count 时 remaining 不为负。"""
    t = _row_to_task(_row(10, 8, 5))
    assert t["remaining_subtasks"] == 0


def test_backward_compat_short_row():
    """老库/短行(无第22列)→ abandoned 缺省 0,remaining 退化为 count-completed。"""
    short = _row(10, 4, 0)[:22]  # 砍掉 abandoned 列
    t = _row_to_task(short)
    assert t["abandoned_subtasks"] == 0
    assert t["remaining_subtasks"] == 6


def test_all_done_zero_remaining():
    t = _row_to_task(_row(5, 5, 0))
    assert t["remaining_subtasks"] == 0
    assert t["completed_subtasks"] == 5
