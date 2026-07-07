"""create_project path 冲突语义回归（P1-23 → D16 演进）。

历史：P1-23 时代 path 冲突走 ON CONFLICT DO UPDATE 合并 config——但这正是 D16 坐实的
跨用户项目劫持破口（任何持 project:create 者提交已存在 path 即静默改写受害项目
name/description/config 并拿到完整项目行）。

D16 治本后（默认拒绝）：store 层 path 冲突【不改写既存行】，抛 ProjectPathConflictError
（携带既存项目行），成员幂等/403 的授权决策上移到路由层。本测试钉住新语义。

触真实 PG，_test_ 前缀隔离 + try/finally 清理。需本地 PG。
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from swarm.config.settings import DatabaseConfig
from swarm.project.store import (
    ProjectPathConflictError,
    create_project,
    ensure_tables,
    get_project,
)


def _pg_available() -> bool:
    try:
        with psycopg.connect(DatabaseConfig().postgres_uri, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="PG 不可达")

_PATH = f"/tmp/_test_p1_23_{uuid.uuid4().hex[:8]}"
_ID_A = f"_test_p1_23_a_{uuid.uuid4().hex[:8]}"
_ID_B = f"_test_p1_23_b_{uuid.uuid4().hex[:8]}"


def _cleanup():
    with psycopg.connect(DatabaseConfig().postgres_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM projects WHERE path = %s", (_PATH,))


def test_create_project_path_conflict_raises_without_mutation():
    ensure_tables()
    try:
        first = create_project(_ID_A, "proj-a", _PATH, description="d1", config={"x": 1})
        assert first["id"] == _ID_A
        assert first["config"] == {"x": 1}

        # 相同 path、不同 id、新 config → D16：拒绝且既存行【一字不改】，
        # 冲突信号携带既存项目行（供路由做成员幂等/403 决策）。
        with pytest.raises(ProjectPathConflictError) as exc_info:
            create_project(_ID_B, "proj-b", _PATH, description="d2", config={"y": 2})
        assert exc_info.value.existing["id"] == _ID_A

        after = get_project(_ID_A)
        assert after["name"] == "proj-a", "冲突不得改写既存项目 name（D16 劫持破口）"
        assert after["description"] == "d1", "冲突不得改写 description"
        assert after["config"] == {"x": 1}, "冲突不得合并/改写 config"
    finally:
        _cleanup()
