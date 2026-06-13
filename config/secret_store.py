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
_fernet = None
_fernet_lock = threading.Lock()


# ──────────────────────────────────────────────
# 加密引擎（Fernet）
# ──────────────────────────────────────────────

def _derive_key_from_db() -> str:
    """无 SWARM_SECRET_KEY 时，从 db 连接串派生一个稳定根密钥（弱保护兜底）。"""
    seed = DatabaseConfig().postgres_uri or "swarm-default-seed"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _get_fernet():
    """惰性构造 Fernet 实例。根密钥优先 env SWARM_SECRET_KEY，否则 db 派生。"""
    global _fernet
    if _fernet is not None:
        return _fernet
    with _fernet_lock:
        if _fernet is not None:
            return _fernet
        from cryptography.fernet import Fernet

        raw = os.environ.get("SWARM_SECRET_KEY", "").strip()
        if not raw:
            raw = _derive_key_from_db()
            logger.warning(
                "未设置 SWARM_SECRET_KEY，使用 db 连接串派生的弱根密钥加密敏感信息。"
                "生产环境请显式设置 SWARM_SECRET_KEY（32 字节 base64）以增强保护。"
            )
        # Fernet 需要 32 字节 urlsafe base64 key —— 用 sha256 归一化任意输入
        digest = hashlib.sha256(raw.encode("utf-8")).digest()
        fkey = base64.urlsafe_b64encode(digest)
        _fernet = Fernet(fkey)
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
    return DatabaseConfig().postgres_uri


def ensure_tables(conn_str: str | None = None) -> None:
    """建 secret_store 表（幂等）。由 init_db / app on_startup 调用。"""
    conn_str = conn_str or _conn_str()
    with psycopg.connect(conn_str, autocommit=True) as conn:
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
            return None
        plaintext = decrypt(row[0])
    except Exception as exc:  # noqa: BLE001
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
