"""P1-23 回归：create_project 路径冲突时不得静默丢弃调用方新 config。

历史 bug：ON CONFLICT (path) DO UPDATE 只更新 name/description，未更新 config →
调用方带新 config 复用既存项目行时，新 config 被静默丢弃。
修复：DO UPDATE 合并 config（projects.config || EXCLUDED.config，两个方向都不丢）；
      传入 id 与既存 id 不一致时告警（可观测，不静默）。

触真实 PG，_test_ 前缀隔离 + try/finally 清理。需本地 PG。
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import psycopg
import pytest

from swarm.config.settings import DatabaseConfig
from swarm.project.store import create_project, ensure_tables


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


def test_create_project_path_conflict_merges_config_and_warns():
    ensure_tables()
    try:
        first = create_project(_ID_A, "proj-a", _PATH, description="d1", config={"x": 1})
        assert first["id"] == _ID_A
        assert first["config"] == {"x": 1}

        # 相同 path、不同 id、新 config → 复用既存行(id=A)，但新 config 不得丢。
        # 直接 patch 模块 logger 断言告警（不依赖 caplog，避免全量跑时全局 logging 状态干扰）。
        with patch("swarm.project.store.logger") as mock_log:
            second = create_project(_ID_B, "proj-b", _PATH, description="d2", config={"y": 2})

        # id 因 path 自然键无法重指，返回既存 id
        assert second["id"] == _ID_A, "path 冲突应复用既存 id"
        # 两个方向都不丢：新键 y 生效、既存键 x 保留
        assert second["config"].get("y") == 2, "调用方新 config 被静默丢弃(回归)"
        assert second["config"].get("x") == 1, "既存 config 键被覆盖丢失"
        # name/description 仍更新（原有行为不回归）
        assert second["name"] == "proj-b"
        assert second["description"] == "d2"
        # id 不一致 → 有告警(可观测)
        assert mock_log.warning.called, "传入 id 与既存 id 不一致时应告警"
        assert "id" in (str(mock_log.warning.call_args).lower())
    finally:
        _cleanup()
