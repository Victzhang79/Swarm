"""32 号文 A9-M1 治本锁：`types.TaskStatus.is_terminal_status` 必须与
`task_states.TERMINAL_STATES` **同源**，不得各抄一份字面量。

★为什么原有 11 条断言钉不住这件事（本锁的立项理由）★
`test_p2_14_d58_d59_infra.py:181-183` 与 `test_partial_delivery_ssot_round22.py:83-93`
一共 11 处断言，**全部**是断 `is_terminal_status` / `is_successful_status` 对某个具体取值
的返回值（"PARTIAL 是终态"、"MONITORING 不是"…）。这类断言证明的是**该方法当前实现自洽**，
而缺陷是**两份定义会漂移**：往 `TERMINAL_STATES` 加第五个终态时，那 11 条照绿，
`types` 那份静默变错。

**判据必须断"两个集合相等"，而不是"某几个取值答对了"**——这正是 CLAUDE.md 硬检查②
"测试要证被接上了而非实现正确"的形状：把派生关系整块拆掉（改回硬抄），本文件必红。

注：`task_states.py` 是**无依赖叶子**（docstring 明写不得 import 任何 swarm 内部模块，
实测只 import `__future__`），故绑定方向只能是 `types` → `task_states`，无循环依赖风险。
"""
from __future__ import annotations

from swarm.task_states import TERMINAL_STATES
from swarm.types import TaskStatus


def test_types_terminal_predicate_is_derived_from_ssot_not_handcopied():
    """★核心锁★ 谓词判为终态的 enum 取值集合，必须**逐元素等于** SSOT 的终态集。

    这条在两份定义漂移时必红——不管是 `TERMINAL_STATES` 加了新态而谓词没跟上，
    还是谓词多认了一个 SSOT 里没有的态。
    """
    _derived = {s.value for s in TaskStatus if TaskStatus.is_terminal_status(s)}
    assert _derived == set(TERMINAL_STATES), (
        "★两份终态定义已漂移★ `types.TaskStatus.is_terminal_status` 与 "
        "`task_states.TERMINAL_STATES` 必须同源（前者从后者派生）。\n"
        f"谓词认定的终态={sorted(_derived)}\n"
        f"SSOT 的终态    ={sorted(TERMINAL_STATES)}\n"
        f"只在谓词侧={sorted(_derived - set(TERMINAL_STATES))}\n"
        f"只在 SSOT 侧={sorted(set(TERMINAL_STATES) - _derived)}"
    )


def test_ssot_terminal_states_are_all_declared_enum_members():
    """SSOT 里每个终态字符串都必须是 `TaskStatus` 的成员。

    ★为什么需要这条（上一条单独不够）★ 上一条是拿 enum 成员**遍历**出来的集合去比，
    若 SSOT 新增了一个**不在 enum 里**的终态，`_derived` 那侧根本产不出它 ⇒ 上一条会红，
    但红的原因看起来像"谓词漏认"，而真因是"enum 少一个成员"。这条把成因分开，
    让诊断落在正确的一侧（缺席必须机读可辨）。
    """
    _members = {s.value for s in TaskStatus}
    _missing = set(TERMINAL_STATES) - _members
    assert not _missing, (
        f"★SSOT 声明的终态在 TaskStatus 里没有对应成员★ 缺={sorted(_missing)}。"
        f"加终态时必须同时补 enum 成员，否则一切按 enum 遍历的消费者都看不见它。"
        f"\n现有成员={sorted(_members)}"
    )


def test_successful_status_is_strict_subset_of_terminal_and_not_derived():
    """成功集必须是终态集的**真子集**，且**刻意不同源**。

    ★这条锁的是一个设计决定，不是实现细节★ `is_successful_status` 保持 `== DONE`，
    没有改成从 `TERMINAL_STATES` 派生——因为终态＝"不再推进"、成功＝"达成目标"，
    两者语义不同。若有人图省事把它写成"终态里除掉失败那些"，新增一个终态就会
    **自动改变什么算成功**，那比手抄更坏（同族：复用单一事实源 ≠ 复用其消费契约）。
    """
    _succ = {s.value for s in TaskStatus if TaskStatus.is_successful_status(s)}
    assert _succ == {TaskStatus.DONE.value}, (
        f"成功集必须恰为 {{DONE}}，实得 {sorted(_succ)}——"
        f"PARTIAL/FAILED/CANCELLED 都是终态但都不是成功（诚实未完成优于假 DONE）"
    )
    assert _succ < set(TERMINAL_STATES), (
        f"成功集必须是终态集的真子集，实得成功={sorted(_succ)} 终态={sorted(TERMINAL_STATES)}"
    )


def test_predicate_accepts_both_enum_and_raw_string():
    """两种入参形态（enum 成员 / 裸字符串）结论必须一致。

    生产上两种都在用（DB 读回来是字符串、代码内部传 enum）。派生化改动动了取值来源，
    这条确保没把某一种入参形态弄坏——`status.value if isinstance(...)` 那一跳仍在。
    """
    for _s in TERMINAL_STATES:
        assert TaskStatus.is_terminal_status(_s) is True, f"裸字符串 {_s} 应判终态"
        assert TaskStatus.is_terminal_status(TaskStatus(_s)) is True, f"enum {_s} 应判终态"
    # 反向：一个确定非终态的取值，两种形态都必须判 False
    assert TaskStatus.is_terminal_status("MONITORING") is False
    assert TaskStatus.is_terminal_status(TaskStatus.MONITORING) is False


def test_store_terminal_tuple_stays_aligned_with_ssot():
    """`project/store.py` 的 `_TERMINAL_STATUSES` 元组仍与 SSOT 同源。

    ★为什么把它也钉在这里★ A9-M1 的病根是"注释引了个非权威"：store 的注释原本写
    "与 `types.TaskStatus.is_terminal_status` 同口径"，而它实际从 `task_states` 派生。
    注释已更正，但**注释不是可执行的**——这条锁让 store 侧与 SSOT 的派生关系有牙。
    （`test_task_states.py:60` 已有一条同向锁；本条与它同族，重复是刻意的：
    A9-M1 要求的是"三处都同源"，任一处脱钩都该有锁红。）
    """
    from swarm.project.store import _TERMINAL_STATUSES

    assert set(_TERMINAL_STATUSES) == set(TERMINAL_STATES), (
        f"store 的终态元组与 SSOT 漂移：store={sorted(_TERMINAL_STATUSES)} "
        f"SSOT={sorted(TERMINAL_STATES)}"
    )
