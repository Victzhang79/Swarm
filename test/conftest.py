"""pytest 全局 — 加载 swarm_bootstrap + 测试数据清理兜底。"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

# 单元测试默认关闭 RBAC（匿名 admin 放行），避免大量 401。
# 认证相关测试（test_auth_login / test_rbac）直接调用 auth 模块或公开端点，不受影响。
os.environ.setdefault("SWARM_RBAC_ENABLED", "false")

_path = Path(__file__).parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _path)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ──────────────────────────────────────────────────────────────────────
# 测试数据清理兜底（测试铁律：触真实存储的测试须 _test_ 隔离名 + 清理）
#
# 历史教训：test_rbac / test_a2_sandbox_rbac 等直接对真实 PG 调 create_user /
# set_project_member 且不清理，导致跑一次全量测试就往生产库灌几百个垃圾用户
# （_test_* / test_* / other_* / _uitest_*），污染「用户与权限管理」UI。
#
# 此 session 级 autouse fixture 在所有测试结束后扫除这些前缀的残留用户及其
# 项目成员记录——绝不触碰真实用户（admin 及不带测试前缀的）。
# 单个测试仍应自行用 try/finally 清理；这是最后一道兜底防线。
# ──────────────────────────────────────────────────────────────────────

# 仅清理这些前缀的用户名（测试专用命名）。ESCAPE '\\' 转义下划线，避免误匹配。
_TEST_USER_PATTERNS = (
    r"\_test\_%",   # _test_*
    r"test\_%",     # test_*
    r"other\_%",    # other_*
    r"\_uitest\_%",  # _uitest_*
)


def _purge_test_users() -> None:
    try:
        import psycopg

        from swarm.config.settings import DatabaseConfig
        conn_str = DatabaseConfig().postgres_uri
    except Exception:
        return  # 无 PG（CI 无库等）直接跳过

    where = " OR ".join("username LIKE %s ESCAPE '\\'" for _ in _TEST_USER_PATTERNS)
    try:
        with psycopg.connect(conn_str, autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT id FROM swarm_users WHERE ({where}) "
                    f"AND global_role <> 'admin' AND username <> 'admin'",
                    _TEST_USER_PATTERNS,
                )
                ids = [r[0] for r in cur.fetchall()]
                if ids:
                    cur.execute("DELETE FROM swarm_project_members WHERE user_id = ANY(%s)", (ids,))
                    cur.execute("DELETE FROM swarm_users WHERE id = ANY(%s)", (ids,))
            conn.commit()
    except Exception:
        # 清理失败不应让测试套件报错；下次 session 末会再扫
        pass


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_users_after_session():
    yield
    _purge_test_users()
