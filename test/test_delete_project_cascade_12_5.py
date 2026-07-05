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


def test_delete_project_cascades_user_profile_by_composite_key():
    """mem_user_profile 按【复合键尾段】级联删（治「名存实亡」）。

    治本前：delete_project 按恒空的 project_id 列删 → 匹配 0 行（该项目用户画像永不清理）。
    治本后：mem_user_profile.user_id = f"{user}:{project_id}"（全局用 ":__global__"），按尾段删。
    断言：本项目的每用户 L1 画像行被清；同用户的全局画像 + 其它项目的画像行保留。
    仅用 _test_ 前缀 user/project 隔离，try/finally 兜底，绝不碰真实数据。需本地 PG。
    """
    ensure_tables()
    try:
        from swarm.memory.store import MemoryStore  # noqa: F401
    except Exception:
        pass

    with _conn() as conn:
        with conn.cursor() as cur:
            if not _table_exists(cur, "mem_user_profile"):
                import pytest
                pytest.skip("mem_user_profile 表不存在（memory 迁移未跑）")

    other_pid = f"{_TEST_PROJECT_ID}_other"
    user = "_test_prof_user"
    key_proj = f"{user}:{_TEST_PROJECT_ID}"       # 本项目画像 → 应删
    key_global = f"{user}:__global__"              # 全局画像 → 应留
    key_other = f"{user}:{other_pid}"              # 其它项目画像 → 应留
    seeded_keys = [key_proj, key_global, key_other]

    def _profile_exists(cur, k):
        cur.execute("SELECT 1 FROM mem_user_profile WHERE user_id = %s", (k,))
        return cur.fetchone() is not None

    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO projects (id, name, path) VALUES (%s,%s,%s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (_TEST_PROJECT_ID, "test-profile-cascade", "/tmp/_test_profile"),
                )
                for k in seeded_keys:
                    cur.execute(
                        "INSERT INTO mem_user_profile (user_id, profile_json) VALUES (%s, '{}'::jsonb) "
                        "ON CONFLICT (user_id) DO NOTHING",
                        (k,),
                    )

        ok = delete_project(_TEST_PROJECT_ID)
        assert ok is True, "delete_project 应返回 True（含 profile 复合键删也不炸事务）"

        with _conn() as conn:
            with conn.cursor() as cur:
                assert not _profile_exists(cur, key_proj), "本项目用户画像应被清（复合键尾段删）"
                assert _profile_exists(cur, key_global), "全局画像 :__global__ 不应被删"
                assert _profile_exists(cur, key_other), "其它项目的画像不应被误删"
    finally:
        with _conn() as conn:
            with conn.cursor() as cur:
                for k in seeded_keys:
                    try:
                        cur.execute("DELETE FROM mem_user_profile WHERE user_id = %s", (k,))
                    except Exception:
                        pass
                for pid in (_TEST_PROJECT_ID, other_pid):
                    try:
                        cur.execute("DELETE FROM projects WHERE id = %s", (pid,))
                    except Exception:
                        pass


if __name__ == "__main__":
    try:
        test_delete_project_cascades_kb_mem()
        print("  ✅ test_delete_project_cascades_kb_mem")
        test_delete_project_cascades_user_profile_by_composite_key()
        print("  ✅ test_delete_project_cascades_user_profile_by_composite_key")
        print("\n=== 12.5 cascade delete: 2/2 passed ===")
    except AssertionError as e:
        print(f"  ❌ {e}")
        raise
