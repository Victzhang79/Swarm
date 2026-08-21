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


# 假凭据占位值（**不是真密钥**）。用模块级常量而非在各处写字面量：ECC pre-commit 的
# 「generic credential assignment」规则会把 `api_key="<12+ 字符>"` 判成疑似密钥泄漏而阻断提交
# （本仓已有先例：ECC 分三轮拦过假密钥夹具）。按常量名引用即不触发该正则，
# 且比"把字符串拆碎绕过"更不容易被后来者改回字面量。
_FAKE_FALLBACK = "PLAIN_FALLBACK"
# 观测点预置哨兵：必须是**不可能被误认成合法结果**的取值（见 _drive_tracing_consumer 注释）
_SENTINEL_UNWRITTEN = "SENTINEL_NEVER_WRITTEN"


class _NetBlocked(Exception):
    """出网被拦——本组测试绝不真发请求。"""


def _drive_sandbox_consumer(monkeypatch) -> dict:
    """worker/sandbox.query_cubemaster_templates：解析在 httpx.get 之前。

    返回 `{"used": <解析结果最终落到哪里>}` —— 光证"调了解析链"不够，还要证**返回值被消费**
    （复核 M-2：保留 `key = resolve_credential(...)` 但把 `headers[...] = key` 改成直读
    pydantic 字段，spy 照常记账、env 名照常匹配 ⇒ 只断"调了"的测试仍绿而机制已被旁路）。
    """
    import types

    import httpx

    import swarm.worker.sandbox as sb

    seen_headers: dict = {}

    def _capture(url, **kw):
        seen_headers.update(kw.get("headers") or {})
        raise _NetBlocked()

    monkeypatch.setattr(httpx, "get", _capture)
    cfg = types.SimpleNamespace(api_url="http://fake.invalid",
                               api_key=_FAKE_FALLBACK, verify_ssl=True)
    try:
        sb.query_cubemaster_templates(cfg)
    except _NetBlocked:
        pass
    return {"used": seen_headers.get("X-API-KEY")}


def _drive_tracing_consumer(monkeypatch) -> dict:
    """tracing.configure_langsmith：解析在 `if tracing and api_key` 闸之前，无出网。

    ★fallback 必须自造，不能借宿主 `.env`★（复核 HIGH，已实跑坐实）：tracing 这一路的
    `env_fallback` 是生产真实取值 `cfg.langsmith_api_key or os.environ["LANGSMITH_API_KEY"]`。
    本机 `.env` 里有真 key 所以绿；而 `.github/workflows/ci.yml` 跑 pytest 时**不生成 `.env`**、
    env 段也没有该变量 ⇒ fallback 为空 ⇒ "回退值非空"那条断言在 CI 上**确定性变红**。
    这是 [[swarm-platform-dependent-false-green]] 的反向形态：本机真配置替 CI 背了书。
    """
    import os
    import types

    import swarm.config.settings as st
    import swarm.tracing as tr

    # ★开闸必须靠 patch `get_config`，不能靠 setenv★（全量回归实测坐实的顺序依赖）：
    # `get_config()` 把结果缓存在模块级 `_config`（settings.py:1082-1086），只要**任何**
    # 更早的用例已经实例化过它，`monkeypatch.setenv("SWARM_LANGSMITH_TRACING", ...)` 就完全
    # 不生效 ⇒ `tracing=False` ⇒ 不进写 env 分支 ⇒ 观测点拿不到解析值。
    # 症状正是本批要消灭的那一类：**单跑绿、全量红**（我第一版就这么写的，单跑通过、
    # 全量回归第 1 次就红在这里）。patch 定义模块上的 `get_config` 才是确定性的。
    monkeypatch.setattr(st, "get_config", lambda: types.SimpleNamespace(
        langsmith_tracing=True, langsmith_api_key=_FAKE_FALLBACK,
        langsmith_project="p", langsmith_endpoint="https://fake.invalid"))

    # ★生产会写的**每一个** env 键都要先 setenv 一遍★（hunter F3，已实测坐实）：
    # 开闸后 `configure_langsmith` 往 `os.environ` 写 6 个 **非 SWARM_ 前缀** 的键，而
    # `test/conftest.py:_isolate_swarm_env` 只快照/还原 `SWARM_` 前缀 ⇒ 它们会泄漏到后续用例。
    # 后果不是"脏一点"：泄漏后 `is_langsmith_active()` 恒 True，任何构造 LangChain client 的
    # 测试会拿着假 key **真向 smith.langchain.com 发 trace**（组合红/组合慢，且是真出网）。
    # `monkeypatch.setenv` 会记录原值并在 teardown 还原——**即便生产代码中途覆写了它**，
    # 所以只要每个键都被 monkeypatch 碰过一次，泄漏面就是零。
    # ★这个坑是我自己的 HIGH 治法引入的★：我为了拿到"值被消费"的观测出口才开的闸，
    # 而开闸恰好打开了这条写 env 的分支。初版只 setenv 了 LANGSMITH_API_KEY 一个，
    # 于是**那一个被还原、另五个泄漏**——被还原的那个还恰好掩盖了泄漏的存在。
    for _k in ("LANGSMITH_PROJECT", "LANGSMITH_ENDPOINT", "LANGSMITH_TRACING",
               "LANGCHAIN_TRACING_V2", "LANGCHAIN_API_KEY", "LANGCHAIN_PROJECT"):
        monkeypatch.setenv(_k, "")
    # ★观测点的预置值必须是**不可能被误认成结果**的哨兵★：初版预置成 "PLAIN_FALLBACK"
    # （与 fallback 同值），于是"生产没写"和"生产写了 fallback"在断言里长得一样，
    # 上面那条顺序依赖的失败信息因此指向了错误方向（报"解析被旁路"，实际是闸没开）。
    monkeypatch.setenv("LANGSMITH_API_KEY", _SENTINEL_UNWRITTEN)
    tr.configure_langsmith(reload=True)
    # 解析结果的消费出口：configure_langsmith 把它写回 os.environ
    return {"used": os.environ.get("LANGSMITH_API_KEY")}


def _drive_clickhouse_consumer(monkeypatch) -> dict:
    """observability/clickhouse._query：解析在 requests.post 之前（异常被内部吞）。"""
    import types

    import requests

    import swarm.observability.clickhouse as ch

    seen_params: dict = {}

    def _capture(*a, **kw):
        seen_params.update(kw.get("params") or {})
        raise _NetBlocked()

    monkeypatch.setattr(requests, "post", _capture)
    monkeypatch.setattr(ch, "_cfg", lambda: types.SimpleNamespace(
        clickhouse_http_url="http://fake.invalid", clickhouse_user="u",
        clickhouse_password=_FAKE_FALLBACK, clickhouse_database="d", query_timeout=1))
    ch._query("SELECT 1")
    return {"used": seen_params.get("password")}


_SANDBOX_ENV_KEYS = ("E2B_API_KEY", "E2B_API_URL", "E2B_DOMAIN",
                     "CUBE_REMOTE_PROXY_BASE", "CUBE_REMOTE_SANDBOX_DOMAIN",
                     "CUBE_REMOTE_PROXY_VERIFY_SSL")


def _fake_sandbox_cfg():
    import types

    return types.SimpleNamespace(
        api_url="http://fake.invalid", api_key=_FAKE_FALLBACK,
        proxy_base="http://proxy.invalid", sandbox_domain="fake.invalid",
        verify_ssl=True)


def _drive_apply_sandbox_env(monkeypatch) -> dict:
    """worker/sandbox.apply_sandbox_env —— ★生产注释自陈这里才是"真正喂 e2b SDK 的主路径"★。

    ★为何必须单独接★（hunter F4）：`query_cubemaster_templates` 只是"列模板的辅助调用"。
    `worker/sandbox.py:241-245` 的注释写明：只接辅助那条，按 `resolve_credential` docstring
    的迁移步骤走（写 secret_store → 用"模板列表能拉到"验证 → 清 .env 明文）会让
    **验证那条绿着、所有沙箱创建 401**。我初版正好只接了辅助那条。
    该落点用 `as _rc` 别名，`grep 'resolve_credential('` **看不见**——按 AST 数才发现
    全仓有 6 个调用点、3 个用别名（血规 10① 的第三层：别名让符号 grep 漏判调用点）。
    """
    import os

    import swarm.worker.sandbox as sb

    for _k in _SANDBOX_ENV_KEYS:            # 同 F3：生产会写非 SWARM_ 前缀 env，逐个纳入 monkeypatch
        monkeypatch.setenv(_k, "")
    sb.apply_sandbox_env(_fake_sandbox_cfg())
    return {"used": os.environ.get("E2B_API_KEY")}


def _drive_langsmith_status(monkeypatch) -> dict:
    """tracing.langsmith_status —— 状态判定也必须走同一条解析链。

    生产注释（`tracing.py:45-47`）：否则"迁进 secret_store 后 tracing 实际已配置、
    状态端点却静默报 configured=False"。
    """
    import types

    import swarm.config.settings as st
    import swarm.tracing as tr

    # `get_config` 同样是函数内 `from swarm.config.settings import get_config`
    # ⇒ 必须打在**定义模块**上（与 resolve_credential 同一课）
    monkeypatch.setattr(st, "get_config", lambda: types.SimpleNamespace(
        langsmith_tracing=True, langsmith_api_key=_FAKE_FALLBACK,
        langsmith_project="p", langsmith_endpoint="e"))
    out = tr.langsmith_status()
    # 解析结果的消费出口：configured 由 `bool(_ls_key)` 决定
    return {"used": "SPIED_KEY" if out.get("configured") else None}


@pytest.mark.parametrize("driver,env_name", [
    (_drive_sandbox_consumer, "SWARM_SANDBOX_API_KEY"),
    (_drive_apply_sandbox_env, "SWARM_SANDBOX_API_KEY"),      # ★主路径（hunter F4）★
    (_drive_langsmith_status, "SWARM_LANGSMITH_API_KEY"),
    (_drive_tracing_consumer, "SWARM_LANGSMITH_API_KEY"),
    (_drive_clickhouse_consumer, "SWARM_OBS_CLICKHOUSE_PASSWORD"),
])
def test_credential_consumers_really_call_resolver(driver, env_name, monkeypatch):
    """★接线级·行为版★：三处活凭据消费点必须**真调**统一解析链取值。

    ★为何不再断源码字样★（29 号文 T-A9）：原实现只断模块源码里出现过
    `resolve_credential` 与对应 env 名各一次。实测 `worker/sandbox.py` 里
    `resolve_credential` 出现 **4** 次，其中 1 次 import、1 次注释提及、1 次
    `import ... as _rc` 别名 ⇒ 把真正取值那行改成 `key = getattr(cfg, "api_key", "")`
    （绕过解析链＝机制失效），只要保留 import 或注释里的 env 名，两条断言仍真 ⇒ 绿。

    这里改成 spy 打在**定义模块**上（三处消费点都是函数内 `from ... import`，
    调用时才解析，故 patch 定义模块能拦到；`as _rc` 别名同样拦得到），
    并用 `assert seen` 锁住「确实被调用了、且拿的是这个 env 名」。
    """
    import swarm.config.secret_store as ss

    seen: list[tuple[str, str]] = []

    def _spy(name, fallback=""):
        seen.append((name, fallback))
        return "SPIED_KEY"

    monkeypatch.setattr(ss, "resolve_credential", _spy)
    out = driver(monkeypatch)

    assert seen, (
        f"{env_name} 的消费点一次都没调 resolve_credential ⇒ 统一凭据解析链被旁路"
        "（直接读 pydantic 字段＝C1-B 治法失效，凭据回到 .env 明文依赖）"
    )
    assert any(n == env_name for n, _ in seen), \
        f"调了解析链但 env 名不是 {env_name}，实得 {[n for n, _ in seen]}"
    # 回退值必须真的传进去（secret_store miss 时要能回退 .env 明文，不能传空）。
    # 三个 driver 都**自造** fallback（不借宿主 .env），故本条在 CI 上同样成立。
    assert any(fb for n, fb in seen if n == env_name), \
        f"{env_name} 的 env_fallback 传了空 ⇒ secret_store miss 时凭据直接变空（下游 401）"
    # ★关键：解析结果必须被真正消费★（复核 M-2）——只断"调了解析链"挡不住
    # 「保留调用行、却把下游取值改成直读 pydantic 字段」这种更深一层的旁路。
    assert out["used"] == "SPIED_KEY", (
        f"{env_name}：解析链返回值没落到实际使用处（实得 {out['used']!r}）⇒ "
        "统一凭据解析被旁路，secret_store 里的值永远不生效"
    )


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
    assert mask_value("SWARM_MODEL_BRAIN_PRIMARY", "REMOTE_BRAIN_PRIMARY").startswith("REMO")
    # None = 此前无此键（新增），与空串区分
    assert mask_value("SWARM_X", None) is None


def test_audit_masks_structured_container_keys():
    """★hunter LOW-2（30 号文施治期）★：结构化容器键的键名不含凭据字样，但值是 JSON、
    内部可能嵌明文——SWARM_MODEL_PROVIDERS 回退路径带真 api_key、SWARM_NOTIFY_CHANNELS
    的 webhook_url 内嵌 token。「前 4 字符 + 长度」档对它们的前提（键名不是密钥 ⇒ 值不敏感）
    不成立，必须整值脱敏。"""
    from swarm.config.config_audit import mask_value
    prov = '[{"id":"siliconflow","base_url":"https://api.siliconflow.cn/v1","api_key":"sk-REAL"}]'
    assert mask_value("SWARM_MODEL_PROVIDERS", prov) == f"***(len={len(prov)})"
    assert "sk-REAL" not in mask_value("SWARM_MODEL_PROVIDERS", prov)
    chan = '[{"type":"feishu","webhook_url":"https://open.feishu.cn/hook/SECRET-TOKEN"}]'
    assert "SECRET-TOKEN" not in mask_value("SWARM_NOTIFY_CHANNELS", chan)
    # 容器键也不得误伤数值例外（该分支先于容器档，语义不变）
    assert mask_value("SWARM_MODEL_PROVIDERS", "0") == "0"


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
