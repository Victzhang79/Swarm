"""复核整改（code-reviewer MEDIUM）：secret_store 弱回退根密钥的种子稳定性。

D15 给默认 DSN 字面量加了 `?connect_timeout=10`，而 `_derive_key_from_db` 恰以
`DatabaseConfig().postgres_uri` 全串为种子——DSN 的化妆性改动（query 参数）会静默
轮换 Fernet 根密钥，升级后旧密文全部 InvalidToken → get_secret 回退 .env → 已存
密钥"消失"。治本：种子取 URI 去 query 的归一形态；含 query 的完整 URI 作为兼容
解密回退（MultiFernet 轮换语义），两代密文都解得开，未来 query 改动不再轮换。

行为断言（禁 getsource）：直接用 encrypt/decrypt 往返验证跨"DSN query 变化"的解密能力。
"""

from __future__ import annotations

import base64
import hashlib

import pytest


@pytest.fixture()
def _clean_fernet(monkeypatch):
    """每例隔离：清 Fernet 单例 + 摘 SWARM_SECRET_KEY（钉的是弱回退派生路径）。"""
    import swarm.config.secret_store as ss

    monkeypatch.delenv("SWARM_SECRET_KEY", raising=False)
    monkeypatch.delenv("SWARM_REQUIRE_SECRET_KEY", raising=False)
    monkeypatch.setattr(ss, "_fernet", None)
    yield ss
    monkeypatch.setattr(ss, "_fernet", None)


def _set_uri(monkeypatch, ss, uri: str) -> None:
    class _FakeDBConfig:
        postgres_uri = uri

    monkeypatch.setattr(ss, "DatabaseConfig", _FakeDBConfig)
    monkeypatch.setattr(ss, "_fernet", None)  # 换种子必须重建单例


_BASE_URI = "postgresql://swarm:swarm@localhost:5432/swarm"


def test_query_param_change_does_not_rotate_key(_clean_fernet, monkeypatch):
    """升级前（无 query 默认 DSN）加密的密文，升级后（DSN 带 connect_timeout）必须仍可解。"""
    ss = _clean_fernet
    _set_uri(monkeypatch, ss, _BASE_URI)
    token = ss.encrypt("s3cret-api-key")

    _set_uri(monkeypatch, ss, _BASE_URI + "?connect_timeout=10")
    assert ss.decrypt(token) == "s3cret-api-key"


def test_legacy_ciphertext_under_full_uri_seed_still_decrypts(_clean_fernet, monkeypatch):
    """历史部署若曾以【含 query 的完整 URI】为种子加密（旧派生逻辑），该密文仍必须可解
    （兼容回退），同时新加密走归一化种子。"""
    ss = _clean_fernet
    from cryptography.fernet import Fernet

    full_uri = _BASE_URI + "?sslmode=disable"
    # 手工构造旧逻辑密文：sha256(full_uri).hexdigest() 为 raw，再 sha256(raw) 作 Fernet key
    legacy_raw = hashlib.sha256(full_uri.encode("utf-8")).hexdigest()
    legacy_key = base64.urlsafe_b64encode(hashlib.sha256(legacy_raw.encode("utf-8")).digest())
    legacy_token = Fernet(legacy_key).encrypt(b"legacy-secret").decode("ascii")

    _set_uri(monkeypatch, ss, full_uri)
    assert ss.decrypt(legacy_token) == "legacy-secret"


def test_explicit_secret_key_path_unchanged(_clean_fernet, monkeypatch):
    """显式 SWARM_SECRET_KEY 路径不受影响：加密解密往返一致。"""
    ss = _clean_fernet
    monkeypatch.setenv("SWARM_SECRET_KEY", "my-explicit-root-key")
    monkeypatch.setattr(ss, "_fernet", None)
    token = ss.encrypt("hello")
    assert ss.decrypt(token) == "hello"


def test_roundtrip_same_seed(_clean_fernet, monkeypatch):
    """同种子往返基线。"""
    ss = _clean_fernet
    _set_uri(monkeypatch, ss, _BASE_URI + "?connect_timeout=10")
    assert ss.decrypt(ss.encrypt("x")) == "x"
