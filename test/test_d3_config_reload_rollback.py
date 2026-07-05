"""D3：配置热更 reload 被生产安全门禁拒绝 → 原子回滚 .env + os.environ。

行为测试：mock reload_config 抛 RuntimeError(门禁拒绝)，断言 _persist_env_updates 回滚
.env 内容与 os.environ，并抛 400；成功路径则提交。
"""
from __future__ import annotations

import os

import pytest
from fastapi import HTTPException

from swarm.api.routers import config as cfgmod


def test_rollback_on_reload_gate_failure(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("EXISTING=1\nSWARM_TESTKEY=old\n", encoding="utf-8")
    monkeypatch.setattr(cfgmod._app, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("SWARM_TESTKEY", "old")

    def _boom():
        raise RuntimeError("生产安全门禁拒绝")

    monkeypatch.setattr("swarm.config.settings.reload_config", _boom)

    with pytest.raises(HTTPException) as ei:
        cfgmod._persist_env_updates({"SWARM_TESTKEY": "new"})
    assert ei.value.status_code == 400

    # 回滚：.env 恢复旧内容、os.environ 恢复旧值（不留脏配置）
    content = env.read_text(encoding="utf-8")
    assert "SWARM_TESTKEY=old" in content
    assert "SWARM_TESTKEY=new" not in content
    assert os.environ["SWARM_TESTKEY"] == "old"


def test_rollback_removes_newly_added_key(tmp_path, monkeypatch):
    # 新增键（原 os.environ 无）reload 失败 → 回滚须【删除】该键，不残留
    env = tmp_path / ".env"
    env.write_text("EXISTING=1\n", encoding="utf-8")
    monkeypatch.setattr(cfgmod._app, "_PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("SWARM_BRANDNEW", raising=False)
    monkeypatch.setattr("swarm.config.settings.reload_config",
                        lambda: (_ for _ in ()).throw(RuntimeError("gate")))

    with pytest.raises(HTTPException):
        cfgmod._persist_env_updates({"SWARM_BRANDNEW": "x"})
    assert "SWARM_BRANDNEW" not in os.environ
    assert "SWARM_BRANDNEW" not in env.read_text(encoding="utf-8")


def test_rollback_on_non_runtimeerror(tmp_path, monkeypatch):
    # R1 复核：非 RuntimeError(如 env 畸形致 pydantic ValidationError)也须回滚+500，不留脏 .env
    env = tmp_path / ".env"
    env.write_text("EXISTING=1\nSWARM_TESTKEY=old\n", encoding="utf-8")
    monkeypatch.setattr(cfgmod._app, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("SWARM_TESTKEY", "old")

    def _boom_value():
        raise ValueError("malformed SWARM_NOTIFY_CHANNELS json")

    monkeypatch.setattr("swarm.config.settings.reload_config", _boom_value)

    with pytest.raises(HTTPException) as ei:
        cfgmod._persist_env_updates({"SWARM_TESTKEY": "new"})
    assert ei.value.status_code == 500, "非门禁失败应 500（门禁 RuntimeError 才 400）"
    # 仍回滚
    assert "SWARM_TESTKEY=old" in env.read_text(encoding="utf-8")
    assert os.environ["SWARM_TESTKEY"] == "old"


def test_commit_on_success(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("EXISTING=1\n", encoding="utf-8")
    monkeypatch.setattr(cfgmod._app, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("SWARM_OKKEY", "seed")  # 让 monkeypatch teardown 负责清理
    monkeypatch.setattr("swarm.config.settings.reload_config", lambda: None)

    cfgmod._persist_env_updates({"SWARM_OKKEY": "v1"})
    assert "SWARM_OKKEY=v1" in env.read_text(encoding="utf-8")
    assert os.environ["SWARM_OKKEY"] == "v1"
