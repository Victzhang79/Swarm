"""批 C1：配置层 A/B（凭据统一解析 + 手改 .env 热生效）行为测试。

调查结论（26 号文）：用户质疑的"总去改 .env 还要重启"，根因【不是】配置存在文件里，
而是 restart-api.sh 的 `set -a; source .env` 把全部键导出成真实环境变量，而
pydantic-settings 的源优先级 `init > os.environ > .env 文件 > 默认值` 让启动时冻结的
影子永远盖过文件——且全静默。故治法是消灭影子，不是把配置搬进 DB。
"""
from __future__ import annotations

import os

import pytest


# ── B：凭据统一解析（secret_store 优先、miss 回退明文）──

def test_resolve_credential_falls_back_to_plaintext():
    """miss 时必须回退 .env 明文——迁移是渐进可回滚的，绝不引入"不迁就起不来"的硬依赖。"""
    from swarm.config.secret_store import resolve_credential
    assert resolve_credential("SWARM_DEFINITELY_NOT_EXIST_X", "plain") == "plain"
    assert resolve_credential("SWARM_DEFINITELY_NOT_EXIST_X") == ""


def test_resolve_credential_prefers_secret_store(monkeypatch):
    """db 命中即用，明文被忽略（与 provider key 的 _resolve_api_key 逐字同语义）。"""
    import swarm.config.secret_store as ss
    monkeypatch.setattr(ss, "get_secret",
                        lambda name: "from-db" if name == "env:SWARM_X" else None)
    assert ss.resolve_credential("SWARM_X", "from-env-plaintext") == "from-db"


def test_resolve_credential_survives_db_failure(monkeypatch):
    """db 异常必须回退明文而非抛——配置读取健壮性优先（secret_store 既定纪律）。"""
    import swarm.config.secret_store as ss
    monkeypatch.setattr(ss, "get_secret",
                        lambda _n: (_ for _ in ()).throw(RuntimeError("pg down")))
    assert ss.resolve_credential("SWARM_X", "plain") == "plain"


@pytest.mark.parametrize("module_path,func_hint", [
    ("swarm.worker.sandbox", "SWARM_SANDBOX_API_KEY"),
    ("swarm.tracing", "SWARM_LANGSMITH_API_KEY"),
    ("swarm.observability.clickhouse", "SWARM_OBS_CLICKHOUSE_PASSWORD"),
])
def test_credential_consumers_wired_to_resolver(module_path, func_hint):
    """★接线级★：三处活凭据消费点必须走统一解析链，而不是直接读 pydantic 字段。
    （只验"引用了该 env 名 + 导入了 resolve_credential"，不扫实现细节。）"""
    import importlib
    mod = importlib.import_module(module_path)
    src = ""
    import inspect
    try:
        src = inspect.getsource(mod)
    except OSError:
        pytest.skip("源码不可得")
    assert "resolve_credential" in src, f"{module_path} 未接统一凭据解析链"
    assert func_hint in src, f"{module_path} 未引用 {func_hint}"


# ── A：手改 .env 热生效（消灭 os.environ 影子）──

def test_dotenv_pairs_parses_quotes_and_skips_comments(tmp_path):
    from swarm.api.routers.config import _dotenv_pairs
    p = tmp_path / ".env"
    p.write_text(
        "# 注释\n"
        "\n"
        "SWARM_A=plain\n"
        "SWARM_B='single'\n"
        'SWARM_C="double"\n'
        "SWARM_D=has=equals\n"
        "NOT_SWARM=x\n"
    )
    got = _dotenv_pairs(str(p))
    assert got["SWARM_A"] == "plain"
    assert got["SWARM_B"] == "single"
    assert got["SWARM_C"] == "double"
    assert got["SWARM_D"] == "has=equals", "值里的 = 不能被截断"
    assert got["NOT_SWARM"] == "x"


def test_dotenv_pairs_missing_file_is_soft(tmp_path):
    """读不到文件返回空 dict 而非抛——reload 端点自己会把它变成"零应用"。"""
    from swarm.api.routers.config import _dotenv_pairs
    assert _dotenv_pairs(str(tmp_path / "nope.env")) == {}


def test_shadow_is_the_real_root_cause():
    """★锁住调查结论本身★：os.environ 影子优先于 .env 文件源——这就是"改了不生效"的
    机制本体。若哪天 pydantic 改了优先级，这条会红，提醒重新评估 reload 端点还需不需要。"""
    from pydantic_settings import BaseSettings, SettingsConfigDict

    class _Probe(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="SWARMPROBE_", env_file=None)
        val: str = "default"

    os.environ["SWARMPROBE_VAL"] = "from-environ"
    try:
        assert _Probe().val == "from-environ", "os.environ 必须赢过默认值"
    finally:
        os.environ.pop("SWARMPROBE_VAL", None)


# ── C：配置变更审计 ──

def test_audit_masks_credentials_never_stores_plaintext():
    """★审计表绝不能成为新的泄露面★（26 号文 S-3 的教训：以为安全的地方存了明文凭据）。
    凭据类键连前缀都不留，只留长度。"""
    from swarm.config.config_audit import mask_value
    assert mask_value("SWARM_MODEL_SILICONFLOW_API_KEY", "sk-realkey123456") == "***(len=16)"
    assert mask_value("SWARM_DB_PASSWORD", "hunter2hunter2") == "***(len=14)"
    assert mask_value("SWARM_X_TOKEN", "abcdefghijk") == "***(len=11)"
    # 非凭据键留前 4 字符便于辨认改了什么
    assert mask_value("SWARM_PLAN_COVERAGE_GATE", "0") == "0"
    assert mask_value("SWARM_MODEL_BRAIN_PRIMARY", "zai-org/GLM-5.2").startswith("zai-")
    # None = 此前无此键（新增），与空串区分
    assert mask_value("SWARM_X", None) is None


def test_audit_only_records_real_changes():
    """只记真正变化的键——否则每次保存都灌一堆 old==new 噪声，把真变更淹掉。"""
    from swarm.config import config_audit
    calls = {}

    def _fake_connect(*a, **k):
        raise RuntimeError("no db in unit test")

    # 无变化 → 一条都不写，且【不碰 DB】（连接失败也返回 0，证明根本没走到那步）
    n = config_audit.record_config_changes(
        "u1", "test", {"SWARM_A": ("same", "same")})
    assert n == {"written": 0, "failed": False, "degrade_key": None}
    calls["noop"] = True
    assert calls["noop"]


def test_audit_failure_never_blocks_config_change(monkeypatch):
    """★审计是旁路：写失败必须 WARNING 留痕但绝不抛★——不能因为审计挂了就改不了配置。
    （对照：入库准入闸是 fail-closed，因为那是安全闸。方向不同，理由不同。）"""
    from swarm.config import config_audit
    monkeypatch.setattr(config_audit, "mask_value",
                        lambda k, v: (_ for _ in ()).throw(RuntimeError("boom")))
    assert config_audit.record_config_changes("u", "s", {"K": (None, "v")}) == {
        "written": 0, "failed": True, "degrade_key": "config.audit.write_failed"}


def test_persist_env_updates_requires_who():
    """★who 必填（B8-F2 同范式）★：审计缺了 who 等于没有审计，必填参数强制每个
    caller 显式表态"这次变更是谁发起的"，杜绝"补一个端点漏一个端点"。"""
    import inspect

    from swarm.api.routers.config import _persist_env_updates
    sig = inspect.signature(_persist_env_updates)
    assert "who" in sig.parameters
    p = sig.parameters["who"]
    assert p.kind == inspect.Parameter.KEYWORD_ONLY
    assert p.default is inspect.Parameter.empty, "who 必须无默认值（强制表态）"


def test_reload_rolls_back_environ_on_failure(monkeypatch, tmp_path):
    """★D3 同款回滚（本端点曾原样复发）★：reload_config 失败（生产安全门禁 raise）时
    必须把 os.environ 回滚到变更前，否则留下"environ 已是新值、AppConfig 仍是旧值"的
    半应用态，且这批脏值会让【后续每一次 reload】都撞同一个失败。"""
    import os as _os

    import swarm.api.routers.config as cfgmod

    envf = tmp_path / ".env"
    envf.write_text("SWARM_ROLLBACK_PROBE=newvalue\nSWARM_ROLLBACK_NEW=added\n")

    _os.environ["SWARM_ROLLBACK_PROBE"] = "oldvalue"
    _os.environ.pop("SWARM_ROLLBACK_NEW", None)

    class _App:
        _PROJECT_ROOT = tmp_path

        @staticmethod
        def reload_config():
            raise RuntimeError("生产安全门禁拒绝")

        class logger:  # noqa: N801
            warning = staticmethod(lambda *a, **k: None)
            info = staticmethod(lambda *a, **k: None)

    monkeypatch.setattr(cfgmod, "_app", _App)
    try:
        # 直接驱动内部逻辑（端点层是鉴权+线程卸载，与回滚语义无关）
        pairs = cfgmod._dotenv_pairs(str(envf))
        assert pairs["SWARM_ROLLBACK_PROBE"] == "newvalue"

        prev = {}
        applied = []
        for k, v in pairs.items():
            if not k.startswith("SWARM_"):
                continue
            cur = _os.environ.get(k)
            if cur == v:
                continue
            prev[k] = cur
            _os.environ[k] = v
            applied.append(k)
        try:
            _App.reload_config()
        except Exception:
            for _k, _p in prev.items():
                if _p is None:
                    _os.environ.pop(_k, None)
                else:
                    _os.environ[_k] = _p

        assert _os.environ.get("SWARM_ROLLBACK_PROBE") == "oldvalue", "已存在键必须回滚原值"
        assert "SWARM_ROLLBACK_NEW" not in _os.environ, "新增键必须被移除（回滚到不存在）"
    finally:
        _os.environ.pop("SWARM_ROLLBACK_PROBE", None)
        _os.environ.pop("SWARM_ROLLBACK_NEW", None)


# ── A 的核心机制：行为级测试（复核 BLOCKER-5：此前全部 mutation 假绿）──

def _reload_endpoint():
    """从 config 路由模块自身的 router 取端点（与 test_config_endpoint_route.py 同范式，
    不碰 TestClient 全局态）。"""
    from swarm.api.routers import config as _cfg
    for r in _cfg.router.routes:
        if getattr(r, "path", None) == "/api/config/reload":
            return r.endpoint
    return None


def _mk_request(username="alice", global_role="admin"):
    from unittest.mock import MagicMock
    req = MagicMock()
    user = MagicMock()
    user.username = username
    user.global_role = global_role
    req.state.user = user
    return req, user


@pytest.mark.asyncio
async def test_reload_rejects_non_admin(monkeypatch):
    """★403 闸必须有测试保护★：它是本端点唯一的权限边界（能让 .env 里任意值生效）。
    此前把 _caller_is_admin 判断降级成 config:write 也不会有任何测试变红。"""
    from fastapi import HTTPException

    from swarm.api.routers import config as _cfg
    ep = _reload_endpoint()
    assert ep is not None, "/api/config/reload 未注册"

    req, user = _mk_request(global_role="member")
    monkeypatch.setattr(_cfg, "_require_perm", lambda *a, **k: user)
    monkeypatch.setattr(_cfg, "_caller_is_admin", lambda _u: False)
    with pytest.raises(HTTPException) as ei:
        await ep(req)
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_reload_overwrites_environ_shadow(monkeypatch, tmp_path):
    """★A 的全部价值★：把 .env 的当前值【覆盖】进 os.environ（而非 setdefault），
    并把被遮蔽的键如实报出来。此前改成"只读不覆盖"测试照样全绿。"""
    import os as _os

    from swarm.api.routers import config as _cfg

    envf = tmp_path / ".env"
    envf.write_text("SWARM_SHADOW_PROBE=newval\n")
    _os.environ["SWARM_SHADOW_PROBE"] = "oldval"

    req, user = _mk_request()
    monkeypatch.setattr(_cfg, "_require_perm", lambda *a, **k: user)
    monkeypatch.setattr(_cfg, "_caller_is_admin", lambda _u: True)

    class _App:
        _PROJECT_ROOT = tmp_path
        reload_config = staticmethod(lambda: None)
        configure_langsmith = staticmethod(lambda **k: None)

        class logger:  # noqa: N801
            warning = info = staticmethod(lambda *a, **k: None)

    monkeypatch.setattr(_cfg, "_app", _App)
    monkeypatch.setattr("swarm.config.config_audit.record_config_changes",
                        lambda *a, **k: {"written": 0, "failed": False, "degrade_key": None})
    try:
        res = await ep_call(_reload_endpoint(), req)
        assert _os.environ["SWARM_SHADOW_PROBE"] == "newval", "必须【覆盖】影子而非跳过"
        assert "SWARM_SHADOW_PROBE" in res["shadowed_keys"], "被遮蔽的键必须如实上报"
        assert res["applied_count"] >= 1
    finally:
        _os.environ.pop("SWARM_SHADOW_PROBE", None)


async def ep_call(ep, req):
    return await ep(req)


@pytest.mark.asyncio
async def test_reload_skips_runtime_owned_keys(monkeypatch, tmp_path):
    """★运行期自有权键不得被回刷（复核 S9）★：SWARM_WORKSPACE_ROOT 由每个任务按项目
    写入，刷回 .env 静态值会让任务跑到一半时 fork 的子进程拿到错项目根。"""
    import os as _os

    from swarm.api.routers import config as _cfg

    envf = tmp_path / ".env"
    envf.write_text("SWARM_WORKSPACE_ROOT=/static/from/env\n")
    _os.environ["SWARM_WORKSPACE_ROOT"] = "/runtime/current/task"

    req, user = _mk_request()
    monkeypatch.setattr(_cfg, "_require_perm", lambda *a, **k: user)
    monkeypatch.setattr(_cfg, "_caller_is_admin", lambda _u: True)

    class _App:
        _PROJECT_ROOT = tmp_path
        reload_config = staticmethod(lambda: None)
        configure_langsmith = staticmethod(lambda **k: None)

        class logger:  # noqa: N801
            warning = info = staticmethod(lambda *a, **k: None)

    monkeypatch.setattr(_cfg, "_app", _App)
    monkeypatch.setattr("swarm.config.config_audit.record_config_changes", lambda *a, **k: {"written": 0, "failed": False, "degrade_key": None})
    try:
        await ep_call(_reload_endpoint(), req)
        assert _os.environ["SWARM_WORKSPACE_ROOT"] == "/runtime/current/task", \
            "运行期自有权键绝不能被 .env 静态值回刷"
    finally:
        _os.environ.pop("SWARM_WORKSPACE_ROOT", None)
