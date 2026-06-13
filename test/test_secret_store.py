#!/usr/bin/env python3
"""敏感信息加密存储单测。

加解密纯逻辑（无 DB，任何环境跑）+ db round-trip（接真 PG，连不上 skip）。
覆盖：Fernet 加解密、db 存读删、缓存、provider key 从 db 优先回退 .env。
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# 固定根密钥，保证加解密可复现
os.environ["SWARM_SECRET_KEY"] = "test-root-key-fixed-for-unit-tests"

from swarm.config import secret_store


# ── 加解密（纯逻辑）────────────────────────────────────────

def test_encrypt_decrypt_roundtrip():
    plain = "sk-secret-api-key-12345"
    enc = secret_store.encrypt(plain)
    assert enc != plain  # 确实加密了
    assert secret_store.decrypt(enc) == plain
    print("  ✅ 加密: encrypt/decrypt round-trip")


def test_encrypt_different_each_time():
    # Fernet 含随机 IV/时间戳，同明文每次密文不同（但都能解回）
    a = secret_store.encrypt("same")
    b = secret_store.encrypt("same")
    assert a != b
    assert secret_store.decrypt(a) == secret_store.decrypt(b) == "same"
    print("  ✅ 加密: 同明文密文不同(随机IV)，均可解回")


def test_encrypt_empty():
    assert secret_store.decrypt(secret_store.encrypt("")) == ""
    print("  ✅ 加密: 空串可加解密")


# ── db round-trip（接真 PG；连不上 skip）──────────────────

def _pg_available() -> bool:
    import psycopg
    try:
        with psycopg.connect(secret_store._conn_str(), connect_timeout=3):
            return True
    except Exception:
        return False


_pg = pytest.mark.skipif(not _pg_available(), reason="PG 不可达")

_TEST_KEY = "_test_secret_unit"


@pytest.fixture()
def _clean():
    secret_store.ensure_tables()
    secret_store.delete_secret(_TEST_KEY)
    secret_store.invalidate_cache()
    yield
    secret_store.delete_secret(_TEST_KEY)
    secret_store.invalidate_cache()


@_pg
def test_set_get_secret(_clean):
    secret_store.set_secret(_TEST_KEY, "my-plaintext-value")
    assert secret_store.get_secret(_TEST_KEY) == "my-plaintext-value"
    print("  ✅ db: set/get round-trip(解密正确)")


@_pg
def test_get_missing_returns_none(_clean):
    assert secret_store.get_secret("_never_stored_xyz") is None
    print("  ✅ db: 缺失返回 None")


@_pg
def test_stored_value_is_encrypted_in_db(_clean):
    """db 里存的是密文，不是明文。"""
    import psycopg
    secret_store.set_secret(_TEST_KEY, "plaintext-must-not-appear")
    with psycopg.connect(secret_store._conn_str()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT encrypted_value FROM secret_store WHERE key_name=%s", (_TEST_KEY,))
            stored = cur.fetchone()[0]
    assert "plaintext-must-not-appear" not in stored  # db 里看不到明文
    assert secret_store.decrypt(stored) == "plaintext-must-not-appear"
    print("  ✅ db: 存储的是密文(db泄露看不到明文)")


@_pg
def test_delete_and_cache_invalidate(_clean):
    secret_store.set_secret(_TEST_KEY, "v1")
    assert secret_store.get_secret(_TEST_KEY) == "v1"
    assert secret_store.delete_secret(_TEST_KEY) is True
    secret_store.invalidate_cache(_TEST_KEY)
    assert secret_store.get_secret(_TEST_KEY) is None
    print("  ✅ db: delete + 缓存失效")


@_pg
def test_provider_key_from_db_overrides_env(_clean):
    """_effective_providers 的 api_key 从 db 优先（覆盖 .env 明文）。"""
    from swarm.config.settings import ModelConfig

    secret_store.set_secret("provider_api_key:siliconflow", "db-encrypted-key")
    secret_store.invalidate_cache()
    cfg = ModelConfig(
        siliconflow_base_url="https://api.siliconflow.cn/v1",
        siliconflow_api_key="env-plaintext-key",  # .env 明文
    )
    providers = cfg._effective_providers()
    sf = next(p for p in providers if p.id == "siliconflow")
    assert sf.api_key == "db-encrypted-key", "应优先用 db 的 key"
    secret_store.delete_secret("provider_api_key:siliconflow")
    secret_store.invalidate_cache()
    print("  ✅ 集成: provider key 从 db 优先(覆盖.env明文)")


@_pg
def test_provider_key_falls_back_to_env(_clean):
    """db 没有该 provider key 时回退 .env 明文（向后兼容）。"""
    from swarm.config.settings import ModelConfig

    secret_store.delete_secret("provider_api_key:local")
    secret_store.invalidate_cache()
    cfg = ModelConfig(
        local_base_url="http://ai.bit:3000/api",
        local_api_key="env-local-key",
    )
    providers = cfg._effective_providers()
    local = next(p for p in providers if p.id == "local")
    assert local.api_key == "env-local-key", "db 无则回退 .env"
    print("  ✅ 集成: db 无 key → 回退 .env(向后兼容)")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))
