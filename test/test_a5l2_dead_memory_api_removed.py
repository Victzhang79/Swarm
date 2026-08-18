"""32 号文 A5-L2 治本锁：memory 层三处死代码已删，且**不许回潮**。

**病根**：`MemoryStore` 上三个生产零调用点的成员，其中两个是"接上就破坏不变量"的雷：

| 已删 | 为什么必须删而非留着 |
|---|---|
| `get_user_profile(user_id)` | 只按裸 `user_id` 单查 ⇒ **拿不到项目维度画像**。
  真实读路径＝`memory/profile.py:resolve_user_profile`（复合键 `user:project_id`
  + 三级回退）。照名字去用会静默取到全局画像或空 dict |
| `delete_expired_mistakes` | 判据是 base `decay_weight`（`decay.py:3-7` 宣布废弃的
  口径，会与读时衰减叠加成双重衰减）**且** SQL 是裸 `DELETE`、无 A5-M2 留存谓词
  ⇒ 谁接上，A5-M2 当场半落地 |
| `delete_expired_successes` | 同上，同批删——只删一个＝半落地 |

**为什么不是 `raise NotImplementedError`**（`decay_l5/l6_batch_sql` 兄弟的形状）：
那两个历史上**有**调用者，需要 fail-loud 拦住存量调用；这三个从来没有。留一个
"看起来能用但会破坏不变量"的公开方法，等于给下一个维护者埋雷。

**与 A5-M2 锁的分工（刻意不重叠，否则两边互相兜底、都不可证伪）**：
`test_a5m2_purge_retains_status_marked.py` 是真库行为级，锁的是 `purge_expired`
**本身**保留标记行。它在有人重造 `delete_expired_mistakes` 时**会全绿**——那正是缺口，
本文件补的就是它：**不许出现第二条绕开留存谓词的物理删通道**。

**本轮实读发现、findings 未提的一件事（已核为不需治）**：`decay.py:129/175` 仍有两条
裸 `DELETE ... decay_weight < %s`，但它们坐在 `raise NotImplementedError`（`:86`/`:148`）
**之后**＝不可达，且不可达性已被既有锁 `test_b7_knowledge_fixes.py:57
test_84_decay_batch_methods_raise` 钉住 ⇒ 本文件刻意**不**重复锁它。
"""
from __future__ import annotations

import re

_DEAD_METHODS = ("get_user_profile", "delete_expired_mistakes", "delete_expired_successes")


def _live_source(mod) -> str:
    """模块源码去掉整行注释（防锁被自己的解释性注释满足——本仓踩过的假绿形态）。"""
    import inspect

    return "\n".join(
        ln for ln in inspect.getsource(mod).splitlines()
        if not ln.strip().startswith("#")
    )


def test_dead_methods_do_not_come_back_by_name():
    """★锁①·命名回潮★ 三个方法名不许重新出现在 MemoryStore 上。

    形状取自本仓既有先例（`test_batch21_lmig_locks.py:31` 的 inline 迁移不复活）。
    断言消息带"该走哪条路"——回潮的人第一眼就看到替代入口，而不是只看到一句"不许"。
    """
    from swarm.memory.store import MemoryStore

    _correct_path = {
        "get_user_profile": "memory/profile.py:resolve_user_profile（复合键+三级回退）",
        "delete_expired_mistakes": "memory/decay.py:purge_expired（带 A5-M2 留存谓词）",
        "delete_expired_successes": "memory/decay.py:purge_expired（带 A5-M2 留存谓词）",
    }
    for name in _DEAD_METHODS:
        assert not hasattr(MemoryStore, name), (
            f"MemoryStore.{name} 回潮了（32 号文 A5-L2 已删）。"
            f"正确入口＝{_correct_path[name]}。"
            f"若确要恢复：delete_* 两个必须带 _purge_eligible_status_sql()，"
            f"否则 A5-M2（dismissed/merged 行永不物理删）当场半落地。"
        )


def test_no_second_physical_delete_channel_in_store():
    """★锁②·通道回潮（改名也拦）★ `memory/store.py` 不许有针对这两张表的物理删。

    ★为什么与锁① 不重叠★ 锁① 只认那三个名字：有人以 `purge_old_mistakes` 之类新名
    重造裸 DELETE，锁① 全绿。反向也成立：以 `delete_expired_mistakes` 为名但内部转调
    `purge_expired`（无裸 DELETE），锁② 全绿而锁① 红。两条各自可证伪，非互相兜底。

    物理删这两张表的**唯一**合法通道＝`decay.purge_expired`（按 effective_weight
    + `_purge_eligible_status_sql()` 留存谓词）。store 层只做读/写/权重更新。
    """
    import swarm.memory.store as mem_store

    src = _live_source(mem_store)
    hits = re.findall(r"DELETE\s+FROM\s+(mem_mistakes|mem_successes)", src)
    assert hits == [], (
        f"memory/store.py 出现了针对 {set(hits)} 的物理删——绕开了 A5-M2 留存谓词。"
        f"物理清理唯一入口＝decay.purge_expired；store 层只读/写/更新权重。"
    )


def test_replacement_read_path_is_in_place():
    """★锁③·先写后删★ 删掉 `get_user_profile` 的前提是替代读路径在场且能用。

    ★为什么需要这条★ "删死代码"最坏的形态是把**唯一**读路径当死代码删掉。
    这条断替代品真实存在且拿得到项目维度画像（不只是 `hasattr` 存在性）。
    """
    from swarm.auth.store import profile_key
    from swarm.memory.profile import resolve_user_profile

    assert callable(resolve_user_profile)
    # 项目维度靠复合键落在 user_id 里——这是被删方法（裸 user_id 单查）拿不到的那一维
    k = profile_key("alice", "proj-1")
    assert "proj-1" in k and k != "alice", (
        f"复合键形状变了（{k}）——被删的 get_user_profile 按裸 user_id 查，"
        f"若复合键不再含 project_id，删除的理由（拿不到项目维度）就不再成立，需重新评估"
    )
