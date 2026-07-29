"""敏感信息加密存储（API keys 等）— db + Fernet 对称加密。

设计目标（用户需求）：
  - API keys 不再明文躺在 .env —— 加密后存 db（db 不上传 git，泄露也多一层保护）。
  - 改 key 无需重启：db 是单一真相源，写时刷新缓存，多进程靠短 TTL 缓存最终一致。
  - 向后兼容：db 没有该 key 时回退 .env 明文值（渐进迁移，不破坏现有部署）。
  - 范围仅敏感信息（api_key/password/secret/token）；其余配置仍走 .env。

根密钥：来自环境变量 SWARM_SECRET_KEY（唯一必须留在环境的种子）。
  - 未设置时：自动用 db 连接串派生一个稳定密钥（弱保护，仅防明文裸奔；
    生产应显式设置 SWARM_SECRET_KEY）。日志会告警提示。
  - 这是对称加密的固有约束：必须有个根密钥，否则"加密"无意义。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading
import time

import psycopg

from swarm.config.settings import DatabaseConfig

logger = logging.getLogger(__name__)

SECRET_STORE_DDL = """
CREATE TABLE IF NOT EXISTS secret_store (
    key_name TEXT PRIMARY KEY,
    encrypted_value TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
"""

# 内存缓存（TTL 最终一致；写时立即失效）。多进程各自缓存，TTL 内可能短暂不一致，
# 对配置类数据可接受（30s）。
_CACHE_TTL = 30.0
_cache: dict[str, tuple[str, float]] = {}   # key_name -> (plaintext, cached_at)
_cache_lock = threading.Lock()
# G1-1b（round38c 主题G）：解密失败 warn-once——同 key 同因每次读取都重打（round38c
# 621 条=52% 全部 WARNING）。首次 WARNING（真运维信号保留），之后同 key 降 DEBUG。
_decrypt_warned: set[str] = set()
_db_fail_warned: set[str] = set()   # DR-07-F5(#97)：DB 读失败 warn-once 节流
_fernet = None
_fernet_lock = threading.Lock()


# ──────────────────────────────────────────────
# 加密引擎（Fernet）
# ──────────────────────────────────────────────

def _derive_key_seeds_from_db() -> list[str]:
    """无 SWARM_SECRET_KEY 时，从 db 连接串派生根密钥种子（弱保护兜底）。

    复核整改（reviewer MEDIUM）：主种子取【URI 去 query 的归一形态】——DSN 的化妆性
    改动（如 D15 默认值补 ?connect_timeout=10）不得轮换根密钥，否则升级即静默解不开
    全部已存密文（get_secret 回退 .env，已配置密钥"消失"）。历史部署可能以【含 query
    的完整 URI】为种子加密过（旧派生逻辑）→ 该形态作第二种子保留（仅解密回退，
    新加密一律走归一主种子）。
    """
    uri = DatabaseConfig().postgres_uri or "swarm-default-seed"
    base = uri.split("?", 1)[0]
    seeds = [hashlib.sha256(base.encode("utf-8")).hexdigest()]
    if uri != base:
        seeds.append(hashlib.sha256(uri.encode("utf-8")).hexdigest())
    return seeds


def _get_fernet():
    """惰性构造 Fernet 实例。根密钥优先 env SWARM_SECRET_KEY，否则 db 派生。

    db 派生路径返回 MultiFernet（密钥轮换语义）：首密钥（归一种子）用于加密，
    旧完整 URI 种子仅参与解密——两代密文都解得开。
    """
    global _fernet
    # 复核 Finding 6：捕获局部再判——reset_fernet() 使 _fernet 可 value→None 循环（轮换热更），
    # 无局部时 `if _fernet is not None: return _fernet` 两条 LOAD_GLOBAL 间被 reset 置 None 会返回
    # None → 调用方 AttributeError。局部快照消除该 TOCTOU（GIL 调度无关），零成本。
    _f = _fernet
    if _f is not None:
        return _f
    with _fernet_lock:
        if _fernet is not None:
            return _fernet
        from cryptography.fernet import Fernet, MultiFernet

        def _to_fernet(raw: str) -> Fernet:
            # Fernet 需要 32 字节 urlsafe base64 key —— 用 sha256 归一化任意输入
            digest = hashlib.sha256(raw.encode("utf-8")).digest()
            return Fernet(base64.urlsafe_b64encode(digest))

        raw = os.environ.get("SWARM_SECRET_KEY", "").strip()
        if raw:
            _fernet = _to_fernet(raw)
            return _fernet
        # H5 修复：DB 派生根密钥是弱保护（DB dump + 本仓库即可解密所有存储 key）。
        # 生产环境应显式设 SWARM_SECRET_KEY；置 SWARM_REQUIRE_SECRET_KEY=1 时强制拒绝派生回退。
        if os.environ.get("SWARM_REQUIRE_SECRET_KEY", "").strip().lower() in ("1", "true", "yes"):
            raise RuntimeError(
                "SWARM_REQUIRE_SECRET_KEY 已启用但未设置 SWARM_SECRET_KEY。"
                "生产环境必须显式提供高熵根密钥（32 字节 base64），拒绝用 DB 连接串派生的弱回退。"
            )
        logger.warning(
            "【安全风险】未设置 SWARM_SECRET_KEY，回退到 DB 连接串派生的弱根密钥加密敏感信息——"
            "拿到 DB dump + 本仓库即可解密所有存储的 API key。生产环境请显式设置 "
            "SWARM_SECRET_KEY（32 字节 base64），并置 SWARM_REQUIRE_SECRET_KEY=1 强制校验。"
        )
        fernets = [_to_fernet(s) for s in _derive_key_seeds_from_db()]
        _fernet = fernets[0] if len(fernets) == 1 else MultiFernet(fernets)
        return _fernet


def encrypt(plaintext: str) -> str:
    """加密明文 → base64 密文字符串。"""
    if plaintext is None:
        plaintext = ""
    token = _get_fernet().encrypt(plaintext.encode("utf-8"))
    return token.decode("ascii")


def decrypt(ciphertext: str) -> str:
    """解密密文字符串 → 明文。失败抛异常（由调用方决定回退）。"""
    return _get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")


# ──────────────────────────────────────────────
# db 连接 + 建表
# ──────────────────────────────────────────────

def _conn_str() -> str:
    from swarm.infra.db import pg_conn_str  # §3.2：单一来源，本地名保 seam
    return pg_conn_str()


def ensure_tables(conn_str: str | None = None) -> None:
    """建 secret_store 表（幂等）。由 init_db / app on_startup 调用。"""
    conn_str = conn_str or _conn_str()
    from swarm.infra.db import pg_connect_timeout_kwargs

    # D15：直连补 connect_timeout——PG 黑洞时启动建表有界快失败，不无限挂。
    with psycopg.connect(conn_str, autocommit=True, **pg_connect_timeout_kwargs()) as conn:
        with conn.cursor() as cur:
            cur.execute(SECRET_STORE_DDL)
    logger.info("secret_store table ensured")


def _get_conn(conn_str: str | None = None):
    from swarm.infra.db import sync_pool

    return sync_pool(conn_str).connection()


# ──────────────────────────────────────────────
# 读写（带缓存）
# ──────────────────────────────────────────────

def set_secret(key_name: str, plaintext: str, conn_str: str | None = None) -> None:
    """加密存储一条敏感信息（upsert），并立即失效缓存。"""
    enc = encrypt(plaintext)
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO secret_store (key_name, encrypted_value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key_name) DO UPDATE SET
                    encrypted_value = EXCLUDED.encrypted_value,
                    updated_at = NOW()
                """,
                (key_name, enc),
            )
    with _cache_lock:
        _cache[key_name] = (plaintext, time.monotonic())


def get_secret(key_name: str, conn_str: str | None = None) -> str | None:
    """读取并解密一条敏感信息。不存在返回 None。带 TTL 缓存。

    任何 db/解密错误都返回 None（调用方回退 .env），不抛——保证配置读取健壮。
    """
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key_name)
        if hit and (now - hit[1]) < _CACHE_TTL:
            return hit[0]

    try:
        with _get_conn(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT encrypted_value FROM secret_store WHERE key_name = %s",
                    (key_name,),
                )
                row = cur.fetchone()
        if not row:
            # 真正的 miss（无此 secret）→ 静默返回 None，回退 .env 是预期行为
            return None
        try:
            plaintext = decrypt(row[0])
        except Exception as dec_exc:  # noqa: BLE001
            # M5 修复：decrypt 失败（key 轮换/密文损坏）与 miss 是两回事——
            # 此时 DB 里【有】密文却解不开，静默回退 .env 旧值会让 key 轮换问题极难排查。
            # 升级为 warning 显式告警，便于运维定位。
            if key_name in _decrypt_warned:
                logger.debug("secret %s 解密失败（已告警过，回退 .env）: %s", key_name, dec_exc)
            else:
                _decrypt_warned.add(key_name)
                logger.warning(
                    "secret %s 解密失败（可能 SWARM_SECRET_KEY 轮换或密文损坏），回退 .env"
                    "（同 key 后续降 DEBUG）: %s",
                    key_name, dec_exc,
                )
            # ★负结果也必须进缓存★：只有成功路径写缓存 → 解不开的密文（key 轮换/损坏）
            # 会让【每一次】get_secret 都重新连 PG + 重试解密 + 失败。实测：get_config()
            # 内 _resolve_api_key 按 provider 数调用，一个测试里 71 次 get_config
            # = 213 次 PG 往返，dispatch 节点耗时从 ~0.08s 涨到 0.6s（生产同受影响）。
            # 缓存 None 与"真 miss"同语义（调用方都回退 .env），TTL 到期自然重试，
            # 故修好密文后最长 30s 生效——可接受。
            with _cache_lock:
                _cache[key_name] = (None, now)
            return None
    except Exception as exc:  # noqa: BLE001
        # DR-07-F5(#97)：DB 连接/查询失败 → warn-once（比照 decrypt 分支）。纯 DB 部署（.env 无明文）
        # 下 PG 一抖，每个 get_secret 静默返 None → API key 变空 → 下游 401 一片，但旧 DEBUG 无运维
        # 可见信号，排查者从模型层错误反查半天才定位 PG。首次 WARNING 明示"密钥因 DB 不可用回退"。
        if key_name in _db_fail_warned:
            logger.debug("读取 secret %s 失败（已告警过，回退 .env）: %s", key_name, exc)
        else:
            _db_fail_warned.add(key_name)
            logger.warning(
                "secret_store DB 不可用，secret %s 回退 .env/None（可能导致下游 401/连接错误；"
                "同 key 后续降 DEBUG）: %s", key_name, exc)
        # 同上：DB 不可用时也缓存负结果，避免每次调用都撞一次连接超时
        # （PG 黑洞场景下这会把 get_config 拖成秒级）。
        with _cache_lock:
            _cache[key_name] = (None, now)
        return None

    with _cache_lock:
        _cache[key_name] = (plaintext, now)
    return plaintext


def delete_secret(key_name: str, conn_str: str | None = None) -> bool:
    """删除一条敏感信息（并失效缓存）。"""
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM secret_store WHERE key_name = %s", (key_name,))
            deleted = cur.rowcount > 0
    with _cache_lock:
        _cache.pop(key_name, None)
    return deleted


def list_secret_names(conn_str: str | None = None) -> list[str]:
    """列出已存储的敏感信息 key 名（不返回值，仅供管理/审计）。"""
    try:
        with _get_conn(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT key_name FROM secret_store ORDER BY key_name")
                return [r[0] for r in cur.fetchall()]
    except Exception:  # noqa: BLE001
        return []


def reset_fernet() -> None:
    """DR-07-F4(#96)：清 Fernet 引擎缓存，使 SWARM_SECRET_KEY 根密钥轮换热生效。

    `_fernet` 首次构造后全局缓存且无失效钩——轮换根密钥（改 env）后未重启进程继续用旧钥
    encrypt/decrypt（新进程用新钥，跨进程密文互不可解，且无告警）。reload/显式重置时调用本函数
    强制下次 _get_fernet 按当前 env 重建。"""
    global _fernet
    with _fernet_lock:
        _fernet = None


def invalidate_cache(key_name: str | None = None) -> None:
    """失效缓存（key_name=None 清全部）。配置 reload 后调用。"""
    with _cache_lock:
        if key_name is None:
            _cache.clear()
            # DR-07-F4(#96)：全量失效=配置 reload → 一并重建 Fernet，使根密钥轮换热生效。
            reset_fernet()
        else:
            _cache.pop(key_name, None)


# ══════════════════════════════════════════════
# 通用凭据解析（批 C1-B）
# ══════════════════════════════════════════════

def resolve_credential(env_name: str, env_fallback: str = "") -> str:
    """凭据统一解析：**secret_store 优先，miss 回退 .env 明文**。

    背景（26 号文调查）：`ModelConfig._resolve_api_key` 早已用这个范式把 provider key
    迁进了加密存储（`.env` 里那两项现已为空），但**非 provider 类凭据没有对等通道**——
    `SWARM_SANDBOX_API_KEY` / `SWARM_LANGSMITH_API_KEY` / `SWARM_OBS_CLICKHOUSE_PASSWORD`
    仍以明文躺在 `.env` 里。而 `.env` 明文的现实风险已被实证：它曾被整份切块向量化进
    知识库（26 号文 S-3）。本函数是 provider 版的通用化，供任意凭据字段复用，
    命名约定 `env:<ENV_NAME>`。

    与 `_resolve_api_key` 逐字同语义（db 命中即用、miss/异常均回退），因此迁移是
    **渐进且可回滚**的：先写入 secret_store，验证生效后再清 `.env` 明文；任何一步出问题
    回退到明文即可。绝不引入"必须先迁移否则起不来"的硬依赖。

    ★"合理留文件"清单（迁移前必须先确认消费方，别把活配置当死配置搬走）★
      - `SWARM_SECRET_KEY`：解密 secret_store 的根密钥（鸡生蛋）
      - `SWARM_BOOTSTRAP_ADMIN_PASSWORD`：DB 里还没有 admin 时就要用（鸡生蛋）
      - `SWARM_E2E_PASSWORD`：**消费方是 shell**——`scripts/e2e_login.sh` 直接
        `grep '^SWARM_E2E_PASSWORD=' .env` 读文件（见 docs/E2E_RUNBOOK.md）。迁进
        secret_store 等于要让 shell 去连 PG 解密，是倒退。
      - `SWARM_DB_*` / `SWARM_ENV` / `SWARM_API_HOST|PORT`：进程启动前就要存在。

    ★方法论疤痕（本次迁移调查连错三次）★：判断"某凭据有没有真实消费点"时，grep 字段名
    极易漏判——① 嵌套访问 `cfg.sandbox.api_key` 与扁平名 `cfg.sandbox_api_key` 不同；
    ② 子配置实例上直接就是 `cfg.api_key`（cfg 已经是 SandboxConfig）；③ 消费方可能
    **根本不是 Python**（shell 脚本 grep .env）。结论必须交叉验证后再动手删改。
    """
    try:
        val = get_secret(f"env:{env_name}")
        if val:
            return val
    except Exception:  # noqa: BLE001 — 与 _resolve_api_key 同：任何异常都回退明文
        pass
    return env_fallback
