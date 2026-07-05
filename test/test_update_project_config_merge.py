"""C4 回归：update_project(config=) 须 jsonb 合并（与 create 一致），不整列覆盖。

历史不一致：create 用 `config || EXCLUDED.config` 合并，update 却 `config = %s` 整列覆盖 →
更新一个 config 键会清掉其它既存键（DETECT_STACK 缓存等场景需读-改-写规避，有竞态）。

触真实 PG，_test_ 前缀隔离 + try/finally 清理。需本地 PG。
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from swarm.config.settings import DatabaseConfig
from swarm.project.store import create_project, ensure_tables, get_project, update_project


def _pg_available() -> bool:
    try:
        with psycopg.connect(DatabaseConfig().postgres_uri, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="PG 不可达")

_PATH = f"/tmp/_test_c4_{uuid.uuid4().hex[:8]}"
_ID = f"_test_c4_{uuid.uuid4().hex[:8]}"


def _cleanup():
    with psycopg.connect(DatabaseConfig().postgres_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM projects WHERE path = %s", (_PATH,))


def test_update_project_merges_config_not_clobber():
    ensure_tables()
    try:
        create_project(_ID, "c4", _PATH, description="d", config={"a": 1, "keep": "x"})

        # 更新只传 b → 既存 a/keep 必须保留（合并，非整列覆盖）
        out = update_project(_ID, config={"b": 2})
        assert out["config"].get("b") == 2, "新键应生效"
        assert out["config"].get("a") == 1, "既存键 a 被整列覆盖丢失(回归)"
        assert out["config"].get("keep") == "x", "既存键 keep 被丢失(回归)"

        # 同名键覆盖：a→9，其余保留
        out2 = update_project(_ID, config={"a": 9})
        assert out2["config"].get("a") == 9, "同名键应被新值覆盖"
        assert out2["config"].get("b") == 2 and out2["config"].get("keep") == "x"

        # 落库一致（get_project 复读）
        reread = get_project(_ID)
        assert reread["config"] == {"a": 9, "b": 2, "keep": "x"}
    finally:
        _cleanup()
