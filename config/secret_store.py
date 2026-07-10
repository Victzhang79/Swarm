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
    if _fernet is not None:
        return _fernet
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
            return None
    except Exception as exc:  # noqa: BLE001
        # DB 连接/查询失败（非解密问题）→ debug 即可
        logger.debug("读取 secret %s 失败（回退 .env）: %s", key_name, exc)
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


def invalidate_cache(key_name: str | None = None) -> None:
    """失效缓存（key_name=None 清全部）。配置 reload 后调用。"""
    with _cache_lock:
        if key_name is None:
            _cache.clear()
        else:
            _cache.pop(key_name, None)
