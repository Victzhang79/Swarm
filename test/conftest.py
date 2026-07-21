"""pytest 全局 — 加载 swarm_bootstrap + 测试数据清理兜底。"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

# 单元测试默认关闭 RBAC（匿名 admin 放行），避免大量 401。
# 认证相关测试（test_auth_login / test_rbac）直接调用 auth 模块或公开端点，不受影响。
os.environ.setdefault("SWARM_RBAC_ENABLED", "false")

# R53-1：单测默认关闭 Maven 仓库联网解析。测试要么 monkeypatch 解析器（确定性），要么
# 走"解析不到 → 如实省略"的离线降级路径；绝不允许真去 Central 查坐标——那会让结果随
# 网络与上游版本发布漂移（"网络好就绿"是最坏的一种假绿），且拖慢每一次跑测。
os.environ.setdefault("SWARM_MAVEN_LOOKUP", "0")
# #31-P2b/2c：npm/go 版本解析同律默认关闭（栈中立铺开，同上防"网络好就绿"假绿）。
os.environ.setdefault("SWARM_NPM_LOOKUP", "0")
os.environ.setdefault("SWARM_GO_LOOKUP", "0")

def install_noop_transaction(mock_store) -> None:
    """A-P1-26：给 AsyncMock 的 MemoryStore 装一个 no-op 的 transaction() 异步上下文。

    learn_store 现把 L5/L6 + L2 两写包进 `async with store.transaction():`。真实 store
    返回 psycopg 事务对象；AsyncMock 默认让 store.transaction() 返回 coroutine（非 async CM）
    会炸。此 helper 让 transaction() 同步返回一个 enter/exit 都 no-op 的异步上下文。
    """
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, MagicMock

    @asynccontextmanager
    async def _txn():
        yield None

    mock_store.transaction = MagicMock(side_effect=_txn)
    # WS4：learn 落库前会查幂等键防重放双计数。AsyncMock 默认让它返回 truthy Mock（误判为重复→跳过
    # 落库）。默认置 False（非重复，放行），需要测重放的用例自行覆盖为 True。
    mock_store.summary_has_idempotency_key = AsyncMock(return_value=False)


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


@pytest.fixture(autouse=True)
def _swarm_logger_propagates():
    """测试基建：保证 "swarm" logger 向 root 传播（caplog 依赖 root handler）。

    生产 setup_logging 故意置 propagate=False（自管文件 handler，防双写）；任一测试
    触发它（如 import api.app）后，后续所有 caplog 断言 swarm.* 日志的测试都会静默
    落空（2026-07-10 全量回归实证：顺序依赖 flake，单跑绿组合红）。逐测恢复传播。"""
    import logging as _logging
    lg = _logging.getLogger("swarm")
    prev = lg.propagate
    lg.propagate = True
    try:
        yield
    finally:
        lg.propagate = prev


@pytest.fixture(autouse=True)
def _isolate_swarm_env():
    """H2（主题H·测试隔离）：每测试快照+还原 SWARM_* 环境变量。

    根治顺序依赖 flake：21 个测试文件直接 `os.environ["SWARM_X"] = ...`（非 monkeypatch）
    不还原→污染后续用例（单跑绿、组合红，如 test_15_graph_interrupt 曾被遗留 SWARM_AUTO_ACCEPT
    误导走 auto 早退）。monkeypatch.setenv 本已自还原，此 fixture 是所有【裸赋值/del】的兜底。
    只管 SWARM_ 前缀=污染域，不碰 PATH/PYTEST 等基建 env。"""
    _snap = {k: v for k, v in os.environ.items() if k.startswith("SWARM_")}
    try:
        yield
    finally:
        for k in [k for k in os.environ if k.startswith("SWARM_")]:
            if k not in _snap:
                del os.environ[k]
        for k, v in _snap.items():
            if os.environ.get(k) != v:
                os.environ[k] = v


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
