"""A-P0-5: 生产模式安全自检（fail-closed）回归测试。

覆盖 validate_production_security 的四象限：
  - 生产 + 未设 SWARM_SECRET_KEY → raise
  - 生产 + 默认 admin 密码 → raise
  - 生产 + 两者都正确设置 → 不 raise
  - 开发模式（默认）→ 永不 raise（无论配置多弱）

校验逻辑镜像 secret_store._get_fernet 对 SWARM_SECRET_KEY 的判定（os.environ + strip），
故用 monkeypatch.setenv/delenv 控制根密钥；密码通过构造 AppConfig 直接控制。
"""

from __future__ import annotations

import pytest

from swarm.config.settings import AppConfig, validate_production_security


def _cfg(env: str, password: str) -> AppConfig:
    return AppConfig(env=env, bootstrap_admin_password=password)


def test_prod_no_secret_key_raises(monkeypatch):
    monkeypatch.delenv("SWARM_SECRET_KEY", raising=False)
    cfg = _cfg("production", "a-strong-non-default-password")
    with pytest.raises(RuntimeError) as ei:
        validate_production_security(cfg)
    assert "SWARM_SECRET_KEY" in str(ei.value)


def test_prod_default_password_raises(monkeypatch):
    monkeypatch.setenv("SWARM_SECRET_KEY", "x" * 44)
    cfg = _cfg("production", "swarm")  # 公开默认密码
    with pytest.raises(RuntimeError) as ei:
        validate_production_security(cfg)
    assert "BOOTSTRAP_ADMIN_PASSWORD" in str(ei.value)


def test_prod_both_set_properly_ok(monkeypatch):
    monkeypatch.setenv("SWARM_SECRET_KEY", "x" * 44)
    cfg = _cfg("production", "a-strong-non-default-password")
    # 不应抛出
    validate_production_security(cfg)


def test_prod_blank_secret_key_treated_as_unset(monkeypatch):
    """空白 SWARM_SECRET_KEY 等价未设（镜像 secret_store strip 判定）。"""
    monkeypatch.setenv("SWARM_SECRET_KEY", "   ")
    cfg = _cfg("prod", "a-strong-non-default-password")
    with pytest.raises(RuntimeError):
        validate_production_security(cfg)


def test_development_never_raises_even_when_insecure(monkeypatch):
    monkeypatch.delenv("SWARM_SECRET_KEY", raising=False)
    # 开发模式（默认 env），默认弱密码 + 无根密钥：必须放行
    cfg = _cfg("development", "swarm")
    validate_production_security(cfg)  # 不抛出


def test_default_env_is_development(monkeypatch):
    """未设 SWARM_ENV 时默认 development，is_production 为 False，自检放行。"""
    monkeypatch.delenv("SWARM_SECRET_KEY", raising=False)
    cfg = AppConfig(bootstrap_admin_password="swarm")
    assert cfg.is_production() is False
    validate_production_security(cfg)  # 不抛出
