"""pytest 全局 — 加载 swarm_bootstrap + 测试数据清理兜底。"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import time
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
# P-H4：python 脚手架 driver 的 registry 解析同纪律（绝不让测试依赖网络/假绿）。
os.environ.setdefault("SWARM_PYPI_LOOKUP", "0")
# P-H4b：cargo 脚手架 driver 的 crates.io 解析同纪律（消费者=cargo_registry._lookup_enabled）。
os.environ.setdefault("SWARM_CARGO_LOOKUP", "0")

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
# B-0：共享**非 Maven workspace 夹具**（27 号文 §7 B-0 硬前提）
#
# 27 号文 §4.3 实测：非 Maven 侧零回归网，工程树全是各文件现场造的 1~5 文件微型树，
# 而那些微型树恰好绕开了 `_reconcile_*` / `_safe_subdirs` 的前提句 → 测试走不进被测分支。
# 四型真实拓扑（+single-root +Maven 对照臂）一次造对，供 sandbox_spec / workspace_manifest /
# l1_pipeline / plan_validator 复用。造树逻辑在 test/stack_workspaces.py。
#
# `--import-mode=importlib`（pyproject addopts）下 test/ 不进 sys.path，故按路径加载并
# 注册进 sys.modules —— 与本仓 swarm_bootstrap / test_cassette_replay 同款既有范式，
# 让测试模块能 `import stack_workspaces` 拿 builders 做 collection 期 parametrize。
# ──────────────────────────────────────────────────────────────────────

_sw_path = Path(__file__).parent / "stack_workspaces.py"
_sw_spec = importlib.util.spec_from_file_location("stack_workspaces", _sw_path)
assert _sw_spec and _sw_spec.loader
stack_workspaces = importlib.util.module_from_spec(_sw_spec)
sys.modules["stack_workspaces"] = stack_workspaces
_sw_spec.loader.exec_module(stack_workspaces)


@pytest.fixture
def make_workspace(tmp_path):
    """工厂夹具：`make_workspace("go_work")` → 造好树的 `WorkspaceFixture`。

    同一测试内可造多棵（各自独立子目录），供混栈场景用。
    """
    def _make(name: str, subdir: str | None = None):
        root = tmp_path / (subdir or name)
        return stack_workspaces.build_workspace(name, root)
    return _make


# 注：**刻意不提供** `params=NON_MAVEN_WORKSPACES` 的参数化夹具。消费者要遍历时用
# `@pytest.mark.parametrize(..., indirect=True)` 自带的本地夹具（见
# test_b0_workspace_fixture_matrix.py 的 `fx`）——那样 parametrize id 与覆盖范围都在
# 用它的文件里一目了然。本仓纪律：新账没有消费者＝没造，别先摆一个"将来会有人用"的夹具。


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


class FakeSecretCursor:
    """最小 psycopg cursor 替身：只回一行预置密文（`secret_store` 分支驱动用）。"""

    def __init__(self, row):
        self._row = row

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSecretConn:
    """配合 `FakeSecretCursor` 的连接替身。

    ★放在 conftest 而非某个测试文件里★（hunter F11）：`test/` 没有 `__init__.py`，
    `from test.test_g_theme_observability_p0 import _FakeConn` 能跑通只因 pytest 把 rootdir
    插进 `sys.path[0]` 后 `test` 成了指向本仓的 namespace package —— 而 venv 裸 `python` 下
    `import test` 拿到的是**标准库的 `test` 包**（同名遮蔽）。跨文件 import 另一个**测试文件**
    还多一层风险：被测夹具模块可能在两个名字下各执行一次。共享夹具一律走 conftest。
    """

    def __init__(self, row):
        self._row = row

    def cursor(self):
        return FakeSecretCursor(self._row)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def fake_secret_conn():
    """回 `FakeSecretConn` 类本身，供测试构造 `_get_conn` 替身。

    用 fixture 而不是让测试 `from test.xxx import` / `import conftest`：前者会撞
    stdlib `test` 遮蔽（见 `FakeSecretConn` docstring），后者依赖 rootdir 恰在 sys.path。
    """
    return FakeSecretConn


@pytest.fixture
def secret_store_state():
    """#29-3：快照+还原 `secret_store` 的三份模块级可变状态（**非 autouse，按名取用**）。

    背景：驱动 `get_secret` 的 warn-once / 负缓存分支必须先清 `_cache`（否则第二次调用
    命中负缓存、根本不进被测分支）。但只在**测试开头**清、结束不还原，会把
    `_decrypt_warned={'K_BROKEN'}` / `_db_fail_warned={'MY_KEY'}` / `_cache={'MY_KEY': None}`
    留给后续用例 —— 后果与 H2 那批顺序依赖 flake 同型：**单跑绿、组合红**（别的测试若期待
    同名 key 的首次 WARNING，会因节流集里已有该 key 而只拿到 DEBUG；`_cache` 里的负条目
    还会让 `get_secret` 直接返 None 不碰库）。

    做成非 autouse 是刻意的：全量 7000+ 用例里只有个别几条碰这三份状态，没必要人人付开销。
    """
    import swarm.config.secret_store as ss
    snap = (set(ss._decrypt_warned), set(ss._db_fail_warned), dict(ss._cache))
    ss._decrypt_warned.clear()
    ss._db_fail_warned.clear()
    ss._cache.clear()
    try:
        yield ss
    finally:
        ss._decrypt_warned.clear()
        ss._decrypt_warned.update(snap[0])
        ss._db_fail_warned.clear()
        ss._db_fail_warned.update(snap[1])
        ss._cache.clear()
        ss._cache.update(snap[2])


# ══════════════════════════════════════════════════════════════════════════
# #29-4 T-7：外部服务可用性 —— **惰性求值** + 声明起了服务就 fail-closed
# ══════════════════════════════════════════════════════════════════════════
#
# 治的是两个叠在一起的病：
#   ① **collection 期求值**：原范式 `@pytest.mark.skipif(not _pg_available(), …)` 把
#      连库动作放进**装饰器实参**，import 时求值一次。PG 抖一下 → 整批集成测试降级为
#      skip，而 CI 照样 EXIT 0。同款范式散布 10+ 文件。
#   ② **静默降级**：CI 里 PG/Redis 都是 `services:` 里**明确声明起了**的，连不上是
#      真故障，不是"本机没装"。原实现无法区分这两种情形，一律 skip。
#
# 治法：可用性判定改**惰性**（测试体内首次调用才连），并由
# `SWARM_TEST_REQUIRE_SERVICES` 决定缺服务时的后果：
#   置 1（CI 设，见 .github/workflows/ci.yml）→ `pytest.fail` 硬失败，附带连接错误原文；
#   未置（开发机）→ `pytest.skip`，reason 带机读前缀 `SERVICE_ABSENT:` 便于统计。
#
# ★`SWARM_TEST_REQUIRE_SERVICES` 刻意**不**进 `config/env_registry.py`★（#29-4 T-7 裁决）：
# 那本册的边界是**生产**开关，且它的第二条测试是双向的（登记了而生产代码里扫不到 ⇒ 判死
# 条目 ⇒ 红），所以登记一个只被 test/ 读的开关结构上必然红。实测过替代路径（把 test/ 纳入
# 扫描面）：会牵出 32 个未登记名，绝大多数是"未登记开关必须被拒"那类测试故意造的假名
# （SWARM_BAD / SWARM_DEFINITELY_NOT_EXIST_X …），登记它们会让册子退化成字符串集合。
# 故测试期开关就近在此说明。详见 env_registry 模块 docstring 里的同一段裁决。
#
# ★为什么后果要可配而不是一律硬失败★：开发机上不装 Redis 是合法工作方式，一律硬失败
# 会逼开发者去改测试（比 skip 更坏）。但"缺席"必须**机读可辨**（血规 10④），故 skip
# reason 带固定前缀 + 会话末汇总一次 WARNING，绝不静默。

_SERVICE_PROBE_CACHE: dict[str, str | None] = {}   # name -> None=可用 / str=错误原文
_SERVICE_PROBE_FAILED_AT: dict[str, float] = {}    # name -> 上次失败的 monotonic 时刻
# 失败结论的冷却期（秒）。与生产侧 `_REDIS_REPROBE_COOLDOWN_SEC` 同值同理由：
# 足够吸收瞬时抖动，又不至于长时间停留在"整批降级"态。
_PROBE_FAIL_COOLDOWN_SEC = 30.0
# ★复核 M-3 整改：两档必须分账★
# 原来 fail 档与 skip 档共用一个集合，而汇总文案只描述 skip 档 ⇒ 在
# `SWARM_TEST_REQUIRE_SERVICES=1`（CI，也是这机制唯一为之设计的环境）下，用例明明是
# ERROR，汇总却说"已降级为 skip"并建议"应置 SWARM_TEST_REQUIRE_SERVICES=1"——那开关
# 已经置了。**同一个事实（服务不可用）在两档下后果不同，就必须分档记**
# （血规 10③：复用单一事实源 ≠ 复用其消费契约）。
_SERVICE_ABSENT_SEEN: set[str] = set()      # 降级为 skip 的
_SERVICE_FAILED_HARD: set[str] = set()      # 硬失败的


def _require_services_hard() -> bool:
    return os.environ.get("SWARM_TEST_REQUIRE_SERVICES", "").strip().lower() in ("1", "true", "yes")


def _probe_pg() -> str | None:
    try:
        import psycopg
        from swarm.config.settings import DatabaseConfig
        with psycopg.connect(DatabaseConfig().postgres_uri, connect_timeout=5):
            return None
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def _probe_redis() -> str | None:
    try:
        import redis
        from swarm.config.settings import get_config
        client = redis.from_url(get_config().db.redis_uri, decode_responses=True,
                                socket_connect_timeout=5, socket_timeout=5)
        client.ping()
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def _probe_sandbox() -> str | None:
    """30 号文批16 GS-2：CubeSandbox 探测纳入 needs_service 族。

    原 `test_sandbox_integration.py:57` 在【模块导入期】（collection）发 httpx 探测
    （违 T-7「runtest setup 才求值」形状），且无 needs_service 门控 ⇒ 沙箱可达时一次
    普通全量就对共享沙箱 create/run/kill。SWARM_RUN_SANDBOX_IT=1 强制档视为通过
    （CI 集成阶段要让真失败响亮）。
    """
    import os
    if os.environ.get("SWARM_RUN_SANDBOX_IT") == "1":
        return None
    try:
        from swarm.config.settings import get_config
        api_url = getattr(get_config().sandbox, "api_url", "") or ""
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    if not api_url:
        # 「未配置」是确定性事实非瞬时抖动——控制流直接返回、不参与下方重试
        # （hunter R2-L：绝不靠 "未配置" 魔法子串判重试，文案微调会静默改行为）。
        return "sandbox.api_url 未配置"

    def _once() -> str | None:
        try:
            import httpx
            resp = httpx.get(api_url.rstrip("/"), timeout=3.0)
            if resp.status_code < 500:
                return None
            return f"sandbox HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"

    # ★30 号文批16 双复核 hunter H1 整改★：sandbox 是远程 HTTP 服务（比同族 PG/Redis
    # 更抖），一次 3s 超时被 30s 失败冷却放大成整批 7 条用例冤 skip。探针内部先短退避
    # 重试一次再下失败结论——共享冷却契约不动（改共享口径须先问新老消费者后果是否相同）。
    err = _once()
    if err is not None:
        time.sleep(0.4)
        err = _once()
    return err


_SERVICE_PROBES = {"pg": _probe_pg, "redis": _probe_redis, "sandbox": _probe_sandbox}


def require_service(name: str) -> None:
    """测试体内调用：服务不可用时按 `SWARM_TEST_REQUIRE_SERVICES` fail 或 skip。

    ★必须在**测试体内**调用，绝不放进 `@skipif(...)` 的实参或 `pytestmark`★——那会回到
    collection 期求值，本函数存在的全部意义就是躲开它。
    """
    if name not in _SERVICE_PROBES:
        raise ValueError(f"未知服务 {name!r}，可选：{sorted(_SERVICE_PROBES)}")
    # ★复核 M-5 整改：失败要带冷却重探，不许永久锁存★
    # 原实现一次失败缓存整个 session。生产侧 `infra/redis_client.py:17-24` 里写着同一件事
    # 的复盘：「旧实现用布尔 `_redis_checked` 永久锁存失败 → 启动期一次瞬时抖动会让整个
    # 进程永久退化」，并为此加了 30s 冷却重探。我在测试侧把那个已修的病重犯了一遍：
    # 长 session 里 PG 抖 1 秒，之后**所有** PG 用例全 skip（CI 档则全部硬失败）。
    # 成功结论永久缓存（服务不会"变得不可用"到需要每次重连的程度，且要控开销）；
    # 失败结论只缓存 `_PROBE_FAIL_COOLDOWN_SEC` 秒。
    cached = _SERVICE_PROBE_CACHE.get(name)
    if cached is None and name in _SERVICE_PROBE_CACHE:
        return                                  # 已知可用
    now = time.monotonic()
    last_at = _SERVICE_PROBE_FAILED_AT.get(name)
    if cached is not None and last_at is not None and (now - last_at) < _PROBE_FAIL_COOLDOWN_SEC:
        err = cached                            # 冷却窗内：沿用上次失败结论，不重连
    else:
        err = _SERVICE_PROBES[name]()
        _SERVICE_PROBE_CACHE[name] = err
        if err is None:
            _SERVICE_PROBE_FAILED_AT.pop(name, None)
            return
        _SERVICE_PROBE_FAILED_AT[name] = now
    if _require_services_hard():
        _SERVICE_FAILED_HARD.add(name)
        pytest.fail(
            f"SERVICE_ABSENT:{name} —— {err}。"
            f"（SWARM_TEST_REQUIRE_SERVICES=1：{name} 是 ci.yml `services:` 明确声明"
            f"起了的，连不上=真故障，故本条硬失败而非静默 skip）")
    _SERVICE_ABSENT_SEEN.add(name)
    pytest.skip(
        f"SERVICE_ABSENT:{name} —— {err}。"
        f"（本机未置 SWARM_TEST_REQUIRE_SERVICES ⇒ 降级为 skip。CI 上该开关为 1，"
        f"同样情形会硬失败）")


def pytest_configure(config):
    """注册 `needs_service` 标记（否则 `-W error::PytestUnknownMarkWarning` 下会报未知标记）。"""
    config.addinivalue_line(
        "markers",
        "needs_service(name): 该用例需要外部服务（pg/redis/sandbox）。判定在 **runtest setup** "
        "阶段做（非 collection 期），缺席时按 SWARM_TEST_REQUIRE_SERVICES 硬失败或可见 skip。",
    )


def pytest_runtest_setup(item):
    """#29-4 T-7：`needs_service` 标记的求值点 —— **runtest setup**，不是 collection。

    原范式 `pytestmark = pytest.mark.skipif(not _pg_available(), …)` 把连库动作放在
    模块顶层（装饰器实参），import 时求值一次：
      · PG 抖一下 → **整批**集成测试降级 skip，CI 照样 EXIT 0；
      · 且判定发生在**任何**测试跑之前，`-p no:warnings -q` 下不留任何痕迹。
    危害具体化：`test_startup_runs_migrations.py` 那两条被 skip 后，"迁移失败必须
    fail-fast" 的守护就只剩静态断言。

    改成标记 + 本钩子后：判定推迟到该用例真要跑时、按服务名缓存一次、缺席走
    `require_service` 的统一后果（CI 硬失败 / 本地可见 skip + 会话末汇总 WARNING）。
    """
    for mark in item.iter_markers(name="needs_service"):
        # ★复核 M-4 整改★ 漏写服务名必须 fail-closed。
        # 原实现 `for name in mark.args` 在 `@pytest.mark.needs_service`（忘了参数）时
        # args 为空 → 循环体不执行 → **用例照跑**，闸静默不设。方向恰好反了：
        # 名字写**错**是 fail-closed（require_service 抛 ValueError），名字**缺失**却
        # fail-open —— 而漏参数比拼错名字常见得多。
        # 用 `pytest.fail`；`pytest.UsageError` 实测等效（同样让用例 ERROR、rc≠0）。
        # 选 fail 是因为它语义上是"这条用例不合格"而非"命令行用法错"。
        if not mark.args:
            pytest.fail(
                f"{item.nodeid}: `needs_service` 标记没写服务名 —— "
                f"闸不会检查任何服务（fail-open）。请写成 "
                f"`needs_service(<服务名>)`，可选：{', '.join(sorted(_SERVICE_PROBES))}。")
        for name in mark.args:
            require_service(name)


@pytest.fixture
def service_probe_internals():
    """暴露服务探测的内部状态给测试（M-5 冷却重探锁的消费者）。

    走 fixture 而非 `import conftest`（`--import-mode=importlib` 下后者 ModuleNotFoundError）。
    """
    return {
        "cache": _SERVICE_PROBE_CACHE,
        "failed_at": _SERVICE_PROBE_FAILED_AT,
        "cooldown": _PROBE_FAIL_COOLDOWN_SEC,
        "probes": _SERVICE_PROBES,
        "require": require_service,
        # 两本会话级账也要给出去：用假探针造失败的测试必须能还原它们，
        # 否则会话末汇总会打出本轮并不存在的 SERVICE_ABSENT（假信号）。
        "absent_seen": _SERVICE_ABSENT_SEEN,
        "failed_hard": _SERVICE_FAILED_HARD,
    }


@pytest.fixture
def require_svc():
    """回 `require_service` 本身，供测试体内调用。

    ★用 fixture 而不是让测试 `import conftest`★：本仓 `addopts` 带
    `--import-mode=importlib`，conftest 不进 `sys.modules` 顶层名字空间，
    `from conftest import …` 直接 ModuleNotFoundError（与 `FakeSecretConn`
    那条同源理由：跨文件 import 测试基建一律走 fixture）。
    """
    return require_service


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """会话末汇总：本轮有哪些服务缺席导致降级 —— **写进 terminal reporter**。

    血规 10④：「空返回/缺席必须机读可辨」。逐条 skip 已带 `SERVICE_ABSENT:` 前缀，
    但 `-q` 下没人会翻 skip 列表，"整批降级"与"全部真跑"在终端输出上不可分。

    ★这里为什么不是 `logger.warning`（我的第一版就是，实测无效）★
    第一版用 session 级 autouse fixture 打 `logging.warning`。**实测在本仓惯用命令
    `pytest -p no:warnings -q` 下那条 WARNING 根本不显示**（pytest 只在失败时才吐
    captured log）——于是"缺席可观测"成了纸面承诺，而这批治的恰恰就是"降级不可观测"。
    我造了机制，然后没验证就宣称它生效了。
    `pytest_terminal_summary` 是唯一在 `-q` + `-p no:warnings` 下仍必然输出的通道
    （它写的是 terminal reporter 本身，不经 logging、不经 warnings 插件）。
    """
    # ── ① 降级为 skip 的服务（本机档）──
    if _SERVICE_ABSENT_SEEN:
        names = ",".join(sorted(_SERVICE_ABSENT_SEEN))
        terminalreporter.write_sep(
            "=", f"SERVICE_ABSENT(skip): {names}", yellow=True, bold=True)
        terminalreporter.write_line(
            f"本轮以下外部服务缺席，相关用例【已降级为 skip，非真跑】：{names}。"
            f"CI 上应置 SWARM_TEST_REQUIRE_SERVICES=1 使其硬失败而非静默跳过。")
        logging.getLogger("swarm.test").warning(
            "[SERVICE_ABSENT] 本轮外部服务缺席，相关用例已降级（非真跑）：%s", names)

    # ── ② 硬失败的服务（CI 档）── ★复核 M-3★ 文案绝不能说"已降级为 skip"
    if _SERVICE_FAILED_HARD:
        names = ",".join(sorted(_SERVICE_FAILED_HARD))
        terminalreporter.write_sep(
            "=", f"SERVICE_ABSENT(hard-fail): {names}", red=True, bold=True)
        terminalreporter.write_line(
            f"本轮以下外部服务不可用，相关用例【已硬失败（ERROR），不是 skip】：{names}。"
            f"SWARM_TEST_REQUIRE_SERVICES 已置位，这些服务在 ci.yml `services:` 里"
            f"明确声明起了 —— 连不上是真故障，请查 service 是否起来/健康检查是否通过。")
        logging.getLogger("swarm.test").error(
            "[SERVICE_ABSENT] 本轮外部服务不可用且已硬失败：%s", names)

    # ── ③ 测试用户清理的结果（T-6 那本账的**真正消费者**）──
    # ★自查发现（#29-4，非复核意见）★：`_PURGE_LEDGER` 原先只被 test_conftest_purge_ledger.py
    # 读（那是**驱动出来的**清理），而**会话末真跑那一次**的账目没有任何人看，它唯一的
    # 出口是 `logger.warning` —— 而我已实测那条在 `-p no:warnings -q` 下不显示。
    # 于是 T-6 治的病（清理静默失败 ⇒ 垃圾用户在真库里累积而无人知晓）在 T-6 的修复里
    # 原样存活：账造好了、没有消费者，等于没造（血规 10④）。
    # 这是**同一个缺陷在本批里的第二次**（第一次是 SERVICE_ABSENT 汇总用 logging）。
    if _PURGE_LEDGER.get("phase") == "failed":
        terminalreporter.write_sep(
            "=", "PURGE_FAILED: 测试用户清理失败", red=True, bold=True)
        terminalreporter.write_line(
            f"会话末测试用户清理失败：{_PURGE_LEDGER.get('error')}。"
            f"垃圾测试用户会在 .env 指向的真库里持续累积（下次会话末会再扫一遍）。")


# #29-4 T-6：清理结果的机读账。测试可读它断"清理真跑了/真失败了"。
#   phase: "skipped"（无 PG 依赖，压根没连）/ "done" / "failed"
#   deleted: 实际删除的用户数；error: 失败原因原文
_PURGE_LEDGER: dict[str, object] = {"phase": None, "deleted": None, "error": None}


def _purge_test_users() -> None:
    """会话末清理测试用户。

    ★#29-4 T-6★ 原实现两层**裸吞异常**（`except: return` / `except: pass`），失败
    零日志零账目 —— 于是"清理成功"、"没连上库"、"DELETE 被权限拒绝"三种情形在输出上
    完全不可分（血规 10④：空返回/缺席必须机读可辨）。清理静默失败的后果是垃圾用户
    在真库里持续累积。
    ★同时这也是纪律「绝不在 live E2E 时跑全量回归」的机制来源★：本函数连的是 `.env`
    里的**真库**，按 `test_%`/`other_%` 前缀 DELETE。注释写在这里，因为读到这段代码的人
    正是需要知道这件事的人。
    """
    logger = logging.getLogger("swarm.test")
    _PURGE_LEDGER.update({"phase": None, "deleted": None, "error": None})
    try:
        import psycopg
        from swarm.config.settings import DatabaseConfig
        conn_str = DatabaseConfig().postgres_uri
    except Exception as exc:  # noqa: BLE001
        # 无 psycopg / 无配置：合法跳过（不是失败），但必须留痕+记账
        _PURGE_LEDGER.update({"phase": "skipped", "error": f"{type(exc).__name__}: {exc}"})
        logger.info("[PURGE] 跳过测试用户清理（无 PG 依赖或无配置）: %s", exc)
        return

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
    except Exception as exc:  # noqa: BLE001
        # 清理失败不让测试套件报错（会话已结束，报错只会掩盖真实结果），但**绝不静默**：
        # WARNING + 机读账，否则"垃圾用户持续累积"这件事永远没人知道。
        _PURGE_LEDGER.update({"phase": "failed", "deleted": 0,
                              "error": f"{type(exc).__name__}: {exc}"})
        logger.warning(
            "[PURGE] 测试用户清理失败（垃圾用户将累积到下次会话末重扫）: %s: %s",
            type(exc).__name__, exc)
        return
    _PURGE_LEDGER.update({"phase": "done", "deleted": len(ids)})
    if ids:
        logger.info("[PURGE] 已清理 %d 个测试用户", len(ids))


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_users_after_session():
    yield
    _purge_test_users()


@pytest.fixture
def purge_probe():
    """回 `(_purge_test_users, _PURGE_LEDGER)`，供测试驱动清理并读机读账。

    ★这个 fixture 就是 T-6 那本账的消费者★——账目没有消费者等于没造（血规 10④）。
    """
    return _purge_test_users, _PURGE_LEDGER
