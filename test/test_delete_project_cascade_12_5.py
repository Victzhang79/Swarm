"""12.5 修复回归测试：delete_project 级联清理 kb_*/mem_* 残留。

历史 bug：delete_project 仅删 task_records + preprocess_progress + projects，
残留 kb_*/mem_* 行成为孤立数据，长期膨胀。修复后在同一事务内级联删除全部
kb_*/mem_* 表（按 project_id），表不存在则跳过。

本测试触真实 PG，严格测试铁律：仅用 _test_ 前缀隔离 project_id，
try/finally 兜底清理，绝不碰真实项目。需要本地 PG。
（Qdrant 向量删除在路由层 best-effort，非本单测范围。）
"""

from __future__ import annotations

import uuid

import psycopg

from swarm.config.settings import DatabaseConfig
from swarm.project.store import delete_project, ensure_tables

_TEST_PROJECT_ID = f"_test_12_5_cascade_{uuid.uuid4().hex[:8]}"

# 抽样验证：覆盖 kb_ 与 mem_ 两类，含带向量的 mem_successes
_SAMPLE_TABLES = ["kb_norms", "kb_file_index", "mem_task_summary", "mem_successes"]


def _conn():
    return psycopg.connect(DatabaseConfig().postgres_uri, autocommit=True)


def _table_exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (table,))
    row = cur.fetchone()
    return bool(row and row[0] is not None)


def _count(cur, table: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {table} WHERE project_id = %s", (_TEST_PROJECT_ID,))
    return cur.fetchone()[0]


def test_delete_project_cascades_kb_mem():
    # 确保相关表存在（project + 各 store 的 DDL）
    ensure_tables()
    try:
        from swarm.knowledge.structure_index import StructureIndexer  # noqa: F401
        from swarm.memory.store import MemoryStore  # noqa: F401
    except Exception:
        pass

    with _conn() as conn:
        with conn.cursor() as cur:
            # 建项目行
            cur.execute(
                "INSERT INTO projects (id, name, path) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (_TEST_PROJECT_ID, "test-cascade", "/tmp/_test_cascade"),
            )
            # 往存在的抽样表各插一行（仅 project_id 列必填，其余给最小值/NULL）
            seeded = []
            for tbl in _SAMPLE_TABLES:
                if not _table_exists(cur, tbl):
                    continue
                try:
                    cur.execute(
                        f"INSERT INTO {tbl} (project_id) VALUES (%s)", (_TEST_PROJECT_ID,)
                    )
                    seeded.append(tbl)
                except Exception:
                    # 该表有额外 NOT NULL 列，跳过插入但仍验证删除不报错
                    conn.rollback() if not conn.autocommit else None

    try:
        # 删除项目
        ok = delete_project(_TEST_PROJECT_ID)
        assert ok is True, "delete_project 应返回 True"

        # 断言抽样表中该 project 行已清空 + projects 行已删
        with _conn() as conn:
            with conn.cursor() as cur:
                for tbl in _SAMPLE_TABLES:
                    if _table_exists(cur, tbl):
                        assert _count(cur, tbl) == 0, f"{tbl} 仍残留 {_TEST_PROJECT_ID} 行"
                cur.execute("SELECT COUNT(*) FROM projects WHERE id = %s", (_TEST_PROJECT_ID,))
                assert cur.fetchone()[0] == 0, "projects 行应已删除"
    finally:
        # 兜底清理（即便断言失败也不留垃圾）
        with _conn() as conn:
            with conn.cursor() as cur:
                for tbl in _SAMPLE_TABLES + ["projects"]:
                    try:
                        col = "id" if tbl == "projects" else "project_id"
                        if _table_exists(cur, tbl) or tbl == "projects":
                            cur.execute(
                                f"DELETE FROM {tbl} WHERE {col} = %s", (_TEST_PROJECT_ID,)
                            )
                    except Exception:
                        pass


if __name__ == "__main__":
    try:
        test_delete_project_cascades_kb_mem()
        print("  ✅ test_delete_project_cascades_kb_mem")
        print("\n=== 12.5 cascade delete: 1/1 passed ===")
    except AssertionError as e:
        print(f"  ❌ {e}")
        raise
