"""31 号文 A4-C1 / A4-H1 / A4-H2：配置写入面的【认证/授权/隔离】类键 admin 闸 + 明文清除诚实性。

三条 finding 的锁（均为**接线证明**，不是实现正确性）：

- **A4-C1（CRITICAL，提权）**：`PUT /api/config` 原先只有三道闸（结构化键 400 / 键名正则 /
  出站端点闸），而端点闸的两层判据（`*_URL` 后缀 + 值内 `://`）对认证类键**设计上不覆盖**：
  `SWARM_API_KEY` 既无端点后缀、值里也没有 `://` ⇒ 非 admin owner（OWNER 持 config:write）
  一次请求写入自选 legacy key，此后 `resolve_user` 返 `_LEGACY_USER`（global_role=ADMIN，
  不可吊销不过期）＝提权。dev 模式（SWARM_ENV 缺省 development）连 opt-in 键都不用带。
- **A4-H2**：「关 TLS 校验＝仅 admin」在本仓有两个标签——provider `tls_insecure`（L-2c 已立闸）
  与 env 键 `SWARM_SANDBOX_VERIFY_SSL`（原零闸）。同一事实第二个标签漏判。
- **A4-H1**：`_clear_plaintext_keys_locked` 用 `partition("=")` 取值 ⇒ 拿到带 `_env_quote`
  单引号的原文 ⇒ `json.loads` 必抛 ⇒ `except: pass` 静默吞 ⇒ 在**本系统自己写出的 .env 上
  恒为空操作**，而端点照报"已清除"。

判绿判据（突变实验，必须逐个跑、每次 git status）：
1. 把 `_reject_endpoint_keys` 里 `_is_admin_only_security_key` 那个分支整块删掉
   ⇒ 本文件 A4-C1/A4-H2 全部断言必须红。
2. 把 `_is_admin_only_security_key` 的 `.upper()` 摘掉 ⇒ 大小写变体那条必须红。
3. 把 `_ADMIN_ONLY_SECURITY_PATTERNS` 清空 ⇒ 族兜底那条 + 登记册派生那条必须红。
4. 把 `_clear_plaintext_keys_locked` 的 `_dotenv_pairs` 换回 `partition("=")` 取值
   ⇒ A4-H1 往返锁必须红。
5. 把写回的 `_env_quote(...)` 摘掉 ⇒ 引号形态锁必须红。

★OPS-1 教训★：`atomic_write_env` 与一切落盘 sink **无条件 mock**——删闸突变会让测试载荷
走完真实写盘路径，真实 .env 已被毒化过一次。A4-H1 那组用 tmp_path 自带隔离。
"""
from __future__ import annotations

import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from swarm.api.routers import config as _cfg


# ═════════════════ A4-C1：谓词层（表 + 族兜底 + 大小写归一） ═════════════════

# 必须判 admin-only 的键，每条都标"改了它等价于什么"（消费点已在生产注释里逐条给出）
_MUST_BE_ADMIN_ONLY = [
    ("SWARM_API_KEY", "legacy 万能钥匙 → _LEGACY_USER 全局 admin"),
    ("SWARM_ALLOW_LEGACY_API_KEY", "生产门禁逃生门（自我豁免）"),
    ("SWARM_RBAC_ENABLED", "鉴权总闸 → 匿名 admin"),
    ("SWARM_BOOTSTRAP_ADMIN_PASSWORD", "下次启动改写 admin 密码（持久后门）"),
    ("SWARM_BOOTSTRAP_RESET_ADMIN_PASSWORD", "同上，触发重置"),
    ("SWARM_ENV", "改 development 令生产门禁整体早返"),
    ("SWARM_SECRET_KEY", "密钥存储根密钥"),
    ("SWARM_REQUIRE_SECRET_KEY", "根密钥强制开关"),
    ("SWARM_SANDBOX_VERIFY_SSL", "A4-H2：关 TLS 校验＝真凭据走可 MITM 连接"),
    ("SWARM_SSH_STRICT_HOST_KEY", "关主机公钥校验"),
    ("SWARM_SANDBOX_ALLOW_LOCAL_FALLBACK", "命令逃出沙箱隔离跑在 brain 宿主机"),
    ("SWARM_ALLOW_EXTERNAL_PROJECT_PATH", "放开项目路径边界"),
    ("SWARM_WORKER_COMMAND_WHITELIST", "放开可执行命令面"),
    ("SWARM_RATELIMIT_DISABLED", "关限流（登录爆破）"),
    ("SWARM_DOCS_PUBLIC", "免鉴权暴露 docs"),
    ("SWARM_TRUSTED_PROXY_HOPS", "伪造 XFF 绕过 per-IP 限流"),
    ("SWARM_TOKEN_TTL_HOURS", "设 0＝令牌永不过期"),
]

# 必须放行的键（冤杀锁）：provider/外部服务凭据与预算阈值——改它们不提权。
# ★这一组和上一组同等重要★：过严的闸使用者会绕开（改 .env 重启），反而把变更移出审计面。
_MUST_STAY_OWNER_WRITABLE = [
    "SWARM_KB_EMBED_API_KEY", "SWARM_KB_RERANK_API_KEY",      # kb 端点，非 admin owner 合法改
    "SWARM_MODEL_SILICONFLOW_API_KEY", "SWARM_MODEL_LOCAL_API_KEY",
    "SWARM_LANGSMITH_API_KEY", "SWARM_SANDBOX_API_KEY",
    "SWARM_INGEST_FEISHU_APP_SECRET", "SWARM_OBS_CLICKHOUSE_PASSWORD",
    "SWARM_TOKEN",                    # CLI 客户端自己的 token，服务端鉴权不读
    "SWARM_SMOKE_TOKEN", "SWARM_GITLAB_TOKEN",
    "SWARM_MODEL_BRAIN_PRIMARY", "SWARM_KB_CHUNK_SIZE",
    "SWARM_CONTEXT_MAX_TOKENS", "SWARM_MAX_TASK_TOKENS_PER_MODULE",
    "SWARM_RATELIMIT_MAX_BUCKETS",    # 容量，非"关限流"
]


@pytest.mark.parametrize("key,why", _MUST_BE_ADMIN_ONLY, ids=[k for k, _ in _MUST_BE_ADMIN_ONLY])
def test_a4c1_security_keys_are_admin_only(key, why):
    assert _cfg._is_admin_only_security_key(key) is True, (
        f"{key} 必须 admin-only（{why}）——非 admin owner 改它即提权/关闸/破隔离")


@pytest.mark.parametrize("key", _MUST_STAY_OWNER_WRITABLE)
def test_a4c1_credential_and_budget_keys_not_falsely_gated(key):
    assert _cfg._is_admin_only_security_key(key) is False, (
        f"{key} 是 provider/外部凭据或预算阈值，改它不提权；判成 admin-only 即冤杀"
        "（过严的闸使用者会绕开改 .env，反把变更移出审计面）")


@pytest.mark.parametrize("variant", [
    "swarm_rbac_enabled", "Swarm_Rbac_Enabled", "SWARM_rbac_ENABLED",
    "  SWARM_RBAC_ENABLED  ",
])
def test_a4c1_predicate_normalizes_case_and_space(variant):
    """pydantic case_sensitive 未设（默认不敏感）⇒ 小写键绑定同一字段。不归一即可绕过
    （与 D-1 结构化键闸被 R1 复核抓到的同一个坑）。"""
    assert _cfg._is_admin_only_security_key(variant) is True, (
        f"{variant!r} 必须与大写同判——否则改小写键名即绕过整道闸")


# ═══════════ A4-C1：覆盖面由【登记册派生】兜底（新增同族键漏收即红） ═══════════

def test_a4c1_registry_derived_family_coverage():
    """★为漏项造的兜底网不能与主判据同源★（本仓已立档）：
    `_ADMIN_ONLY_SECURITY_KEYS` 是我按"消费点"编的表，可能漏；族正则按"危险语义词根"编，
    来源不同。本测试从 **REGISTERED_ENVS**（config/env_registry.py，冻结登记册＝单一事实源）
    派生断言：登记册里任何命中危险族的键都必须被本闸判 admin-only。

    这条是"新增同族键"的机器检查——将来有人加 `SWARM_FOO_VERIFY_SSL` / `SWARM_BAR_RBAC_X`
    而忘了收，这里立刻红。**诚实边界**：语义危险但既不在表内也不命中任何族的全新键仍会漏，
    本测试不声称穷举（声称穷举必须指出权威来源，而"哪些键算安全键"没有权威来源）。
    """
    from swarm.config.env_registry import REGISTERED_ENVS

    missed = []
    for key in REGISTERED_ENVS:
        ku = key.upper()
        if any(p in ku for p in _cfg._ADMIN_ONLY_SECURITY_PATTERNS):
            if not _cfg._is_admin_only_security_key(key):
                missed.append(key)
    assert not missed, (
        f"登记册里命中危险族却未被判 admin-only 的键: {missed}"
        "（族兜底与枚举表任一处漏收都会在此暴露）")


@pytest.mark.parametrize("future_key,family", [
    ("SWARM_GATEWAY_VERIFY_SSL", "VERIFY_SSL"),
    ("SWARM_ADMIN_UI_RBAC_MODE", "RBAC"),
    ("SWARM_NEW_ALLOW_LEGACY_TOKEN", "ALLOW_LEGACY"),
    ("SWARM_BOOTSTRAP_SERVICE_SECRET", "BOOTSTRAP"),
    ("SWARM_BUILDER_SSH_STRICT_HOST_KEY", "STRICT_HOST_KEY"),
    ("SWARM_POOL_ALLOW_LOCAL_FALLBACK", "ALLOW_LOCAL_FALLBACK"),
    ("SWARM_SCRIPT_COMMAND_WHITELIST", "COMMAND_WHITELIST"),
    ("SWARM_LOGIN_RATELIMIT_DISABLED", "RATELIMIT_DISABLED"),
    ("SWARM_API_DOCS_PUBLIC_MODE", "DOCS_PUBLIC"),
    ("SWARM_EDGE_TRUSTED_PROXY_HOPS", "TRUSTED_PROXY"),
    ("SWARM_SESSION_TOKEN_TTL_DAYS", "TOKEN_TTL"),
    ("SWARM_ALLOW_EXTERNAL_ARTIFACT_PATH", "ALLOW_EXTERNAL"),
])
def test_a4c1_family_fallback_catches_unenumerated_future_keys(future_key, family):
    """★族兜底的唯一可证伪面★

    这些键**今天不存在**（刻意如此）：族兜底对当前登记册的增量是 0，因为每个已知危险键都已
    在枚举表里。若只用真实键做断言，清空 `_ADMIN_ONLY_SECURITY_PATTERNS` 的突变会**仍绿**
    ——两处判据互相兜底 ⇒ 任一处单独突变都不可证伪（本仓已立档的「冗余防御」形态）。实测确认
    过一次：m3 突变初版就是仍绿，才补出本条。

    故这里用「未来同族新键」构造只有族兜底能接住的取值域，把两处判据的职责分开钉：
    - 上面的枚举表锁 → 证已知键被收
    - 本条 → 证「同一危害换个键名再来一次」会被接住（这正是 A4-H2 的复发形态：
      `tls_insecure` 立了闸，`SWARM_SANDBOX_VERIFY_SSL` 漏了）
    """
    from swarm.config.env_registry import REGISTERED_ENVS
    assert future_key not in REGISTERED_ENVS, (
        f"{future_key} 已成为真实键——请把它移进 _MUST_BE_ADMIN_ONLY 并换一个假名，"
        "否则本条又退化成与枚举表互相兜底")
    assert future_key.upper() not in _cfg._ADMIN_ONLY_SECURITY_KEYS, (
        f"{future_key} 已被枚举表收录 ⇒ 本条不再单独检验族兜底")
    assert _cfg._is_admin_only_security_key(future_key) is True, (
        f"族兜底未接住 {future_key}（族 {family}）——新增同族键会零闸落盘")


def test_a4c1_enumerated_table_is_not_empty_and_pinned():
    """防"整表被清空/被误删"——突变实验里清空表必须有测试红，否则表本身不可证伪。"""
    assert len(_cfg._ADMIN_ONLY_SECURITY_KEYS) >= 17, (
        f"枚举表只剩 {len(_cfg._ADMIN_ONLY_SECURITY_KEYS)} 项——删键必须是刻意行为，"
        "请同步本断言并说明为什么该键不再需要 admin")
    assert len(_cfg._ADMIN_ONLY_SECURITY_PATTERNS) >= 12, (
        "族兜底被削——它是「同一危害换个键名再来一次」的唯一机器防线")


# ═══════════ A4-C1：chokepoint 接线（四个 caller + persist backstop 共用） ═══════════

def test_a4c1_chokepoint_rejects_security_keys_for_non_admin():
    """闸必须在 `_reject_endpoint_keys` 这个 **chokepoint** 上，而不是只在 update_config 内联。
    理由（B8-F2 的原始教训）：四个 caller + `_persist_env_updates` backstop 都过它，
    接在 chokepoint 上＝新闸自动继承全部接线，否则就要再数五个调用点＝"补一个漏一个"。"""
    m = {
        "SWARM_MODEL_BRAIN_PRIMARY": "vendor/model-x",     # 放行
        "SWARM_KB_CHUNK_SIZE": "512",                      # 放行
        "SWARM_API_KEY": "attacker-chosen",                # 拒
        "SWARM_RBAC_ENABLED": "false",                     # 拒
        "SWARM_SANDBOX_VERIFY_SSL": "false",               # 拒（A4-H2）
    }
    rejected: list[str] = []
    kept = _cfg._reject_endpoint_keys(dict(m), False, "owner", rejected_out=rejected)

    assert set(kept) == {"SWARM_MODEL_BRAIN_PRIMARY", "SWARM_KB_CHUNK_SIZE"}, (
        f"非 admin 应只留下非特权键，实得 {sorted(kept)}")
    assert set(rejected) == {"SWARM_API_KEY", "SWARM_RBAC_ENABLED", "SWARM_SANDBOX_VERIFY_SSL"}, (
        f"被拒键必须经 rejected_out 显式回传（拒绝不能伪装成「无变更」），实得 {sorted(rejected)}")


def test_a4c1_chokepoint_admin_still_passes_everything():
    """方向性：闸是"提权到 admin"，不是"谁都不能改"。admin 必须照旧全放行，
    否则运维改不动安全开关会去手改 .env——把变更移出审计面（比不设闸更坏）。"""
    m = {"SWARM_API_KEY": "k", "SWARM_RBAC_ENABLED": "false",
         "SWARM_SANDBOX_VERIFY_SSL": "false", "SWARM_KB_CHUNK_SIZE": "512"}
    kept = _cfg._reject_endpoint_keys(dict(m), True, "admin")
    assert set(kept) == set(m), f"admin 必须全放行，实得 {sorted(kept)}"


def test_a4c1_persist_backstop_really_invokes_the_gate(monkeypatch, tmp_path):
    """`_persist_env_updates` 的 backstop 必须真的过同一 chokepoint。

    ★行为锁而非 getsource★（纪律6：禁结构焊死测试）：把闸换成间谍再调 persist，
    断言"闸被调到了 + 安全键真被剔除"。这比断源码含某字符串强——重命名/换实现不误红，
    而真绕过（某 caller 直接写盘）必红。落盘 sink 全 mock（OPS-1：突变态会走真实写盘路径）。
    """
    seen: list[tuple] = []
    real_gate = _cfg._reject_endpoint_keys

    def _spy(update_map, is_admin, who, rejected_out=None):
        seen.append((dict(update_map), is_admin, who))
        return real_gate(update_map, is_admin, who, rejected_out=rejected_out)

    monkeypatch.setattr(_cfg, "_reject_endpoint_keys", _spy)
    written: list = []
    monkeypatch.setattr(_cfg, "atomic_write_env",
                        lambda path, content: written.append((path, content)))
    monkeypatch.setattr(_cfg._app, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(_cfg, "env_file_lock", lambda _p: __import__("contextlib").nullcontext())
    monkeypatch.setattr(_cfg, "_reload_with_rollback", lambda *a, **k: None)
    monkeypatch.setenv("SWARM_RBAC_ENABLED", "true")

    _cfg._persist_env_updates(
        {"SWARM_RBAC_ENABLED": "false", "SWARM_KB_CHUNK_SIZE": "512"},
        is_admin=False, who="owner")

    assert seen, "persist backstop 未调用闸——某 caller 忘了端点层过滤即可绕过整道闸"
    body = "".join(c for _p, c in written)
    assert "SWARM_RBAC_ENABLED=false" not in body, (
        "安全键经 persist 路径落盘了——backstop 没兜住")
    assert written, "非安全键应照常写盘（闸不能把合法变更一起吃掉）"


# ═══════════ A4-C1：端到端（PUT /api/config，非 admin owner 的真实提权载荷） ═══════════

def _endpoint(path: str, method: str = "PUT"):
    for r in _cfg.router.routes:
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set()):
            return r.endpoint
    return None


@pytest.fixture
def _owner_harness(monkeypatch, tmp_path):
    """非 admin owner 调 PUT /api/config 的 mock 面。
    ★落盘 sink 无条件 mock（OPS-1）★：删闸突变会让提权载荷走完真实写盘路径。"""
    user = MagicMock()
    user.username = "owner-alice"
    user.global_role = "owner"          # 持 config:write，但不是 admin
    monkeypatch.setattr(_cfg, "_require_perm", lambda *a, **k: user)
    written: list = []
    monkeypatch.setattr(_cfg, "atomic_write_env",
                        lambda path, content: written.append((path, content)))
    monkeypatch.setattr(_cfg._app, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(_cfg, "env_file_lock", lambda _p: __import__("contextlib").nullcontext())
    monkeypatch.setattr(_cfg, "_reload_with_rollback", lambda *a, **k: None)
    monkeypatch.setattr(_cfg._app, "reload_config", lambda *a, **k: None)
    monkeypatch.setattr(_cfg._app, "configure_langsmith", lambda **k: None)
    cfg_obj = MagicMock()
    cfg_obj.model_dump.return_value = {}
    monkeypatch.setattr(_cfg._app, "get_config", lambda: cfg_obj)
    monkeypatch.setattr(_cfg, "_mask_config_dict", lambda d: {})
    return {"written": written, "user": user}


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,label", [
    ({"SWARM_API_KEY": "attacker-chosen", "SWARM_ALLOW_LEGACY_API_KEY": "true"}, "A 路 legacy 万能钥匙"),
    ({"SWARM_RBAC_ENABLED": "false"}, "B 路 关 RBAC"),
    ({"SWARM_BOOTSTRAP_ADMIN_PASSWORD": "atk-strong-pw",
      "SWARM_BOOTSTRAP_RESET_ADMIN_PASSWORD": "true"}, "C 路 重置 admin 密码"),
    ({"SWARM_SANDBOX_VERIFY_SSL": "false"}, "A4-H2 关沙箱 TLS 校验"),
])
async def test_a4c1_e2e_non_admin_privilege_payload_never_lands(_owner_harness, payload, label):
    """三条提权路径 + A4-H2：非 admin owner 提交后【一个字节都不许落盘】，且响应显式列出被拒键。

    ★这是本批最重的一条锁★——它证的是"提权载荷到不了磁盘"，而不是"某函数返回了什么"。
    """
    ep = _endpoint("/api/config")
    assert ep is not None, "PUT /api/config 端点未找到（路由变更须同步本测试）"
    req = MagicMock()
    req.json = AsyncMock(return_value={"config": payload})

    resp = await ep(req)

    body = "".join(c for _p, c in _owner_harness["written"])
    for k in payload:
        assert f"{k}=" not in body, f"{label}：{k} 落盘了 ⇒ 提权成功（闸失效）"
    assert resp.get("status") == "rejected", (
        f"{label}：全部键被拒时 status 必须是 rejected，不能伪装成 no_changes/ok，实得 {resp.get('status')}")
    assert set(resp.get("rejected_keys") or []) == set(payload), (
        f"{label}：被拒键必须逐个列出（不可机读的缺席＝调用方以为生效了），"
        f"实得 {resp.get('rejected_keys')}")


@pytest.mark.asyncio
async def test_a4c1_e2e_owner_can_still_write_benign_keys(_owner_harness):
    """冤杀反向锁：同一个非 admin owner 改模型/阈值必须照常成功落盘。
    没有这条，把闸拧成"全拒"也能让上面那组全绿（区分力）。"""
    ep = _endpoint("/api/config")
    req = MagicMock()
    req.json = AsyncMock(return_value={
        "config": {"SWARM_MODEL_BRAIN_PRIMARY": "vendor/model-x", "SWARM_KB_CHUNK_SIZE": "512"}})

    resp = await ep(req)

    assert resp.get("status") == "ok", f"合法变更被拒＝冤杀，实得 {resp}"
    body = "".join(c for _p, c in _owner_harness["written"])
    assert "SWARM_MODEL_BRAIN_PRIMARY=vendor/model-x" in body
    assert not (resp.get("rejected_keys") or []), f"不该有被拒键，实得 {resp.get('rejected_keys')}"


@pytest.mark.asyncio
async def test_a4c1_e2e_mixed_payload_partial_rejection_is_machine_readable(_owner_harness):
    """混合载荷：合法键生效 + 特权键被拒，且被拒事实必须在响应里可辨。
    原实现的 rejected_keys 只覆盖端点键，认证键会静默消失＝"少写一个键"与"全部生效"不可分。"""
    ep = _endpoint("/api/config")
    req = MagicMock()
    req.json = AsyncMock(return_value={"config": {
        "SWARM_KB_CHUNK_SIZE": "512",          # 合法
        "SWARM_RBAC_ENABLED": "false",         # 特权
    }})

    resp = await ep(req)

    body = "".join(c for _p, c in _owner_harness["written"])
    assert "SWARM_KB_CHUNK_SIZE=512" in body, "合法键应生效"
    assert "SWARM_RBAC_ENABLED" not in body, "特权键不许落盘"
    assert resp.get("rejected_keys") == ["SWARM_RBAC_ENABLED"], (
        f"部分拒绝必须机读可辨，实得 {resp.get('rejected_keys')}")


# ═══════════════ A4-H1：明文清除的往返正确性 + 失败可机读 ═══════════════

_PROVIDERS_FIXTURE = [
    {"id": "siliconflow", "kind": "cloud", "base_url": "https://api.example.com/v1",
     "api_key": "sk-REAL-SECRET-VALUE"},
    {"id": "local", "kind": "local", "base_url": "http://127.0.0.1:8000/v1", "api_key": ""},
]


@pytest.fixture(autouse=True)
def _isolate_flat_key_env(monkeypatch):
    """★进程态隔离★：`_clear_plaintext_keys_locked` 清掉扁平键后会同步
    `os.environ[k] = ""`（生产行为正确——.env 与进程态必须一致），但在 pytest 进程里
    这是**跨用例污染**：本文件跑完会把该键在整个 session 里抹成空串，后续读它的用例
    可能静默改变行为。autouse + monkeypatch.setenv ⇒ teardown 自动还原。
    做成 autouse 而非逐个 setenv：将来新增用例不会忘（忘了就是又一次静默污染）。
    """
    monkeypatch.setenv("SWARM_MODEL_SILICONFLOW_API_KEY", "sk-sentinel-restored-by-monkeypatch")
    monkeypatch.setenv("SWARM_MODEL_LOCAL_API_KEY", "sk-sentinel-restored-by-monkeypatch")


def _write_env_production_shape(env_path) -> None:
    """★夹具形状决定命题唯一性★（本仓已立档）：必须用 `_env_quote` 写出的形态，
    也就是**本系统自己产出的 .env**。手写裸 JSON 会让测试走另一条恰好能过的路径——
    那正是这条 finding 逃逸至今的原因（治前代码在裸 JSON 上能工作，在真实 .env 上恒失败）。"""
    quoted = _cfg._env_quote(json.dumps(_PROVIDERS_FIXTURE, ensure_ascii=False))
    env_path.write_text(
        "# 注释行须原样保留\n"
        f"SWARM_MODEL_PROVIDERS={quoted}\n"
        "SWARM_MODEL_SILICONFLOW_API_KEY=sk-flat-secret\n"
        "SWARM_KB_CHUNK_SIZE=512\n",
        encoding="utf-8")


def test_a4h1_clears_plaintext_from_env_quoted_file(tmp_path, monkeypatch):
    """治前：partition("=") 取到带单引号的值 → json.loads 抛 → except:pass 静默吞
    → 该键永不进 cleared，明文留在磁盘上，而端点回报"已清除"。"""
    env = tmp_path / ".env"
    _write_env_production_shape(env)
    monkeypatch.setattr(_cfg._app, "_PROJECT_ROOT", tmp_path)

    cleared, failed = _cfg._clear_plaintext_keys_locked(env)

    assert "SWARM_MODEL_PROVIDERS" in cleared, (
        "★A4-H1 核心★ 在 _env_quote 形态（＝生产真实形态）的 .env 上必须真的清除；"
        f"实得 cleared={cleared} failed={failed}")
    after = env.read_text(encoding="utf-8")
    assert "sk-REAL-SECRET-VALUE" not in after, "JSON 内 provider 明文仍在磁盘上"
    assert "sk-flat-secret" not in after, "扁平字段明文仍在磁盘上"
    assert not failed, f"不该有失败键，实得 {failed}"


def test_a4h1_rewrites_through_env_quote(tmp_path, monkeypatch):
    """#28 复发锁：写回必须过 `_env_quote`，否则裸 JSON 落盘 ⇒ 下次 `source .env` 报 127，
    restart-api 起不来（CLAUDE.md 已记的坑，代码里复发过一次）。"""
    env = tmp_path / ".env"
    _write_env_production_shape(env)
    monkeypatch.setattr(_cfg._app, "_PROJECT_ROOT", tmp_path)

    _cfg._clear_plaintext_keys_locked(env)

    line = [ln for ln in env.read_text(encoding="utf-8").splitlines()
            if ln.startswith("SWARM_MODEL_PROVIDERS=")][0]
    val = line.partition("=")[2]
    assert val[:1] in ("'", '"'), f"写回未加引号 ⇒ source .env 会 127：{val[:40]}"
    # 且引号内仍是合法 JSON（清除不能把值写坏）
    assert all(not e.get("api_key") for e in json.loads(val[1:-1]))


def test_a4h1_preserves_unrelated_lines(tmp_path, monkeypatch):
    """清除不得动无关键与注释（写盘范围最小化）。"""
    env = tmp_path / ".env"
    _write_env_production_shape(env)
    monkeypatch.setattr(_cfg._app, "_PROJECT_ROOT", tmp_path)

    _cfg._clear_plaintext_keys_locked(env)

    after = env.read_text(encoding="utf-8")
    assert "SWARM_KB_CHUNK_SIZE=512" in after
    assert "# 注释行须原样保留" in after


def test_a4h1_malformed_json_is_reported_not_swallowed(tmp_path, monkeypatch):
    """★空返回/缺席必须机读可辨★：清不掉时必须进 `failed`，绝不静默当成"本来就没明文"。
    治前 `except Exception: pass` 让这条路死了很久没人知道。"""
    env = tmp_path / ".env"
    # 引号内是坏 JSON：解析必失败 ⇒ 必须成账
    env.write_text("SWARM_MODEL_PROVIDERS='[{\"id\": \"x\", broken'\n", encoding="utf-8")
    monkeypatch.setattr(_cfg._app, "_PROJECT_ROOT", tmp_path)

    cleared, failed = _cfg._clear_plaintext_keys_locked(env)

    assert "SWARM_MODEL_PROVIDERS" in failed, (
        f"清除失败必须机读可辨（cleared={cleared} failed={failed}）——"
        "静默吞掉会让端点谎报「已从 .env 清除」，运维据此相信磁盘上无明文")
    assert "SWARM_MODEL_PROVIDERS" not in cleared, "失败的键绝不能同时报成已清除"


def test_a4h1_unparseable_env_is_reported_not_silently_skipped(tmp_path, monkeypatch):
    """★治法自己的同型坑★：`_dotenv_pairs` **自吞异常返回 {}**（它的降级设计）。
    若治法只写 try/except 去接它，那是永不执行的死代码，而 `{}` 会让每个值都被看成"缺席"
    ⇒ 一个键都不清、failed 也为空 ⇒ 又变回静默空操作（＝本 finding 本尊）。
    故判据是"有 SWARM_ 赋值行但解析为空"。"""
    env = tmp_path / ".env"
    monkeypatch.setattr(_cfg._app, "_PROJECT_ROOT", tmp_path)
    # 让 _dotenv_pairs 返回 {}（模拟它内部解析失败的降级），文件里确有 SWARM_ 赋值行
    monkeypatch.setattr(_cfg, "_dotenv_pairs", lambda _p: {})
    env.write_text("SWARM_MODEL_SILICONFLOW_API_KEY=sk-flat-secret\n", encoding="utf-8")

    cleared, failed = _cfg._clear_plaintext_keys_locked(env)

    assert not cleared, f"解析为空时绝不能报已清除，实得 {cleared}"
    assert failed, (
        "解析为空（dotenv 降级）必须成账——否则端点谎报「已清除」而明文仍在磁盘上，"
        "正是本 finding 的形态在治法里复发")
    assert "sk-flat-secret" in env.read_text(encoding="utf-8"), (
        "既然报了失败，就不该动文件（不许半清）")


def test_a4h1_empty_env_is_not_reported_as_failure(tmp_path, monkeypatch):
    """区分力：空 .env / 无 SWARM_ 行时解析为空是**正常**，不得报失败
    （否则上一条靠"解析空就报失败"会把正常情形也判成失败＝冤杀）。"""
    env = tmp_path / ".env"
    monkeypatch.setattr(_cfg._app, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(_cfg, "_dotenv_pairs", lambda _p: {})
    env.write_text("# 只有注释\n\n", encoding="utf-8")

    cleared, failed = _cfg._clear_plaintext_keys_locked(env)

    assert not cleared and not failed, (
        f"无 SWARM_ 赋值行时解析空属正常，不该成账，实得 cleared={cleared} failed={failed}")


def test_a4h1_no_plaintext_means_no_failure(tmp_path, monkeypatch):
    """区分力：本来就没明文时 failed 必须为空（否则上一条锁靠"永远报失败"也能绿）。"""
    env = tmp_path / ".env"
    clean = _cfg._env_quote(json.dumps([{"id": "x", "api_key": ""}], ensure_ascii=False))
    env.write_text(f"SWARM_MODEL_PROVIDERS={clean}\nSWARM_KB_CHUNK_SIZE=512\n", encoding="utf-8")
    monkeypatch.setattr(_cfg._app, "_PROJECT_ROOT", tmp_path)

    cleared, failed = _cfg._clear_plaintext_keys_locked(env)

    assert not failed, f"无明文时不该报失败，实得 {failed}"
    assert not cleared, f"无明文时不该报已清除，实得 {cleared}"

