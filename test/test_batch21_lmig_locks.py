"""30 号文批21 L-MIG 锁：四处 inline ADD COLUMN 迁版本化 v9。

- inline 常量不复活（P0-C：改列绝不藏进 ensure 惰性建表）；
- v9 登记在册、to_regclass 逐表门控（表不存在 no-op）、列清单全覆盖；
- 五处 CREATE TABLE 必须含列——断言从 runner._V9_INLINE_COLUMNS【派生】（单一事实源），
  绝不手抄清单（手抄漏项病 GS-1/P-C2 同族：测试与生产从同一份不全心智模型各抄一份）。
"""
from __future__ import annotations

import re

import swarm.auth.store as auth_store
import swarm.knowledge.updater as kb_updater
import swarm.memory.store as mem_store
import swarm.project.store as proj_store
from swarm.infra.migrations import runner as mig

# 表 → 权威 CREATE DDL 常量（新库直建的单一事实源）
_TABLE_DDL = {
    "mem_successes": mem_store.SUCCESSES_DDL,
    "mem_user_profile": mem_store.USER_PROFILE_DDL,
    "swarm_users": auth_store.AUTH_DDL,
    "task_records": proj_store.TASK_RECORDS_DDL,
    "projects": proj_store.PROJECTS_DDL,
    "kb_update_events": kb_updater.EVENT_QUEUE_DDL,
}


def test_inline_add_column_removed_from_all_four_sites():
    """①inline 不复活：三个常量删除 + updater DDL 不再内嵌 ALTER。"""
    assert not hasattr(mem_store, "SUCCESSES_MIGRATION_DDL"), \
        "memory inline 迁移回潮——改列必须走 runner 登记册（P0-C）"
    assert not hasattr(auth_store, "_PROFILE_MIGRATION"), \
        "auth inline 迁移回潮（P0-C）"
    assert not hasattr(proj_store, "_TASK_RECORDS_MIGRATIONS"), \
        "project inline 迁移回潮（P0-C）"
    assert "ADD COLUMN" not in kb_updater.EVENT_QUEUE_DDL, \
        "kb EVENT_QUEUE_DDL 又内嵌 inline ALTER（P0-C）"


def test_v9_registered():
    """②v9 在册【且真接线】——只断名字串的话 callable 换 None 仍绿（MU1 首跑假绿实证），
    必须断注册项的 callable 就是函数本体。"""
    by_name = {n: f for _, n, f in mig._MIGRATIONS}
    assert "inline_add_column_consolidation" in by_name, \
        f"v9 未登记: {list(by_name)}"
    assert by_name["inline_add_column_consolidation"] is \
        mig._migration_v9_inline_add_column_consolidation, \
        "v9 注册了名字但没接函数本体（callable 被换/悬空）"
    versions = [v for v, _, _ in mig._MIGRATIONS]
    assert versions == sorted(versions) and len(set(versions)) == len(versions), \
        f"迁移版本必须升序且唯一: {versions}"


class _Cur:
    """fake cursor：to_regclass 按 inexistent_tables 集合回答，其余 SQL 记为 DDL。"""

    def __init__(self, absent: set[str]):
        self.absent = absent
        self.ddl: list[str] = []
        self._last_table = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, *p):
        if "to_regclass" in sql:
            # 参数化形式：execute(sql, (name,)) ⇒ p[0]=(name,)，表名取 p[0][0]
            self._last_table = (p[0][0].split(".")[-1] if p else None)
        else:
            self.ddl.append(sql)

    def fetchone(self):
        if self._last_table is None:
            return None
        return None if self._last_table in self.absent else (f"public.{self._last_table}",)


class _Conn:
    def __init__(self, absent: set[str]):
        self.cur = _Cur(absent)

    def cursor(self):
        return self.cur


def test_v9_gated_per_table_and_covers_all_columns():
    """③门控+覆盖：全部表不存在 ⇒ 零 DDL；部分存在 ⇒ 只补存在的表；全存在 ⇒
    23 列一列不少且定义逐字等于 _V9_INLINE_COLUMNS。"""
    all_tables = {t for t, _, _ in mig._V9_INLINE_COLUMNS}
    # 全缺席：零 DDL
    c0 = _Conn(set(all_tables))
    mig._migration_v9_inline_add_column_consolidation(c0)
    assert c0.cur.ddl == [], f"表不存在必须 no-op: {c0.cur.ddl}"
    # 部分缺席：只有存在的表拿到 ALTER
    c1 = _Conn({"task_records", "swarm_users"})
    mig._migration_v9_inline_add_column_consolidation(c1)
    hit_tables = {re.search(r"ALTER TABLE (\w+)", s).group(1) for s in c1.cur.ddl}
    assert hit_tables == all_tables - {"task_records", "swarm_users"}, hit_tables
    # 全存在：每列恰一条 ALTER，定义逐字同源
    c2 = _Conn(set())
    mig._migration_v9_inline_add_column_consolidation(c2)
    expected = {
        f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {c} {d}"
        for t, c, d in mig._V9_INLINE_COLUMNS
    }
    assert set(c2.cur.ddl) == expected, \
        f"v9 DDL 与权威清单不一致: 多 {set(c2.cur.ddl) - expected} 少 {expected - set(c2.cur.ddl)}"


def test_create_tables_contain_all_v9_columns():
    """④新库直建：每列必须出现在对应 CREATE DDL 里（词边界匹配）。
    清单从 _V9_INLINE_COLUMNS 派生——加列只改 runner 一处，漏补 CREATE 本锁即红。"""
    for table, col, decl in mig._V9_INLINE_COLUMNS:
        ddl = _TABLE_DDL[table]
        # 列定义行：含列名的【非注释】行（DDL 里注释也可能提到列名，如 kb_update_events）
        rows = [ln for ln in ddl.splitlines()
                if re.search(rf"\b{re.escape(col)}\b", ln)
                and not ln.strip().startswith("--")]
        assert rows, f"{table} 的 CREATE TABLE 缺列 {col}（新库会失列）"
        row = rows[0]
        for tok in decl.split():
            assert tok.rstrip(",") in row, f"{table}.{col} 定义漂移: 期望含 {tok}，实 {row.strip()}"


def test_v9_column_count_matches_four_sites():
    """⑤覆盖守恒钉：23 = memory 1 + auth 4（profile 1 + users 3）+ task_records 15 +
    projects 1 + kb 2。删任何一行清单本锁红（防「迁一半」半落地）。"""
    assert len(mig._V9_INLINE_COLUMNS) == 23, \
        f"列账总数变了（{len(mig._V9_INLINE_COLUMNS)}≠23）——若是有意增删，同步改本钉与登记册"
    per_table = {}
    for t, _, _ in mig._V9_INLINE_COLUMNS:
        per_table[t] = per_table.get(t, 0) + 1
    assert per_table == {
        "mem_successes": 1, "mem_user_profile": 1, "swarm_users": 3,
        "task_records": 15, "projects": 1, "kb_update_events": 2,
    }, per_table


def test_baseline_ddl_order_memory_before_auth(monkeypatch):
    """★批21 hunter M★：baseline 顺序 memory→auth 是硬约束（bootstrap admin 的
    ensure_admin_default_profile 写 mem_user_profile；auth 先跑空库必炸 relation
    not exist）——此前纯约定无锁。调换 _apply_baseline_ddl 里的调用顺序本锁红。"""
    import swarm.scripts.init_db as init_db

    calls: list[str] = []

    def _rec(name):
        def _sync(*a, **k):
            calls.append(name)
        async def _async(*a, **k):
            calls.append(name)
        return _async if name == "_ensure_async_tables" else _sync

    monkeypatch.setattr(init_db, "_ensure_pgvector", _rec("_ensure_pgvector"))
    monkeypatch.setattr(init_db, "_ensure_sync_tables", _rec("_ensure_sync_tables"))
    monkeypatch.setattr(init_db, "_ensure_async_tables", _rec("_ensure_async_tables"))
    monkeypatch.setattr(init_db, "_ensure_auth_tables", _rec("_ensure_auth_tables"))
    mig._apply_baseline_ddl()
    assert calls == [
        "_ensure_pgvector", "_ensure_sync_tables", "_ensure_async_tables",
        "_ensure_auth_tables",
    ], f"baseline 顺序漂移（memory 必须先于 auth）: {calls}"
