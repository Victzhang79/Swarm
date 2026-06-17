"""SWARM_CTO_GUIDE Batch C 回归测试 — P0 安全 fail-closed/传输/鉴权。

覆盖：P0-SEC-08 沙箱故障不落宿主机、P0-SEC-05 verify_ssl 默认/shlex、P0-SEC-07 路径越界、
P0-SEC-09 token 不入日志/零成员 fail-closed、P0-SEC-NEW WS 鉴权。
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch


# ── P0-SEC-08：沙箱激活下基础设施失败 → fail-closed，绝不落 _run_local（宿主机）──
def test_sandbox_infra_fail_does_not_run_local():
    from swarm.tools import build_tools

    fake_sandbox, fake_mgr = object(), MagicMock()
    cr = MagicMock()
    cr.success = False
    cr.error = "502 Bad Gateway"
    fake_mgr.run_command.return_value = cr

    with patch.object(build_tools, "get_sandbox_context", return_value=(fake_sandbox, fake_mgr)), \
         patch.object(build_tools, "_run_local", side_effect=AssertionError("绝不能落宿主机执行!")) as spy_local:
        out = build_tools._run_in_sandbox("echo hi")

    spy_local.assert_not_called()
    assert "fail-closed" in out and "未执行" in out


# ── P0-SEC-05(a)：verify_ssl 默认 True（secure-by-default）──
def test_verify_ssl_secure_by_default():
    from swarm.config.settings import SandboxConfig

    os.environ.pop("SWARM_SANDBOX_VERIFY_SSL", None)
    assert SandboxConfig(_env_file=None).verify_ssl is True


# ── P0-SEC-07：变更操作路径越出 workspace → 拒绝 ──
def test_resolve_write_rejects_escape(tmp_path):
    from swarm.tools import file_tools

    ws = tmp_path / "ws"
    ws.mkdir()
    with patch("swarm.tools.paths.workspace_root", return_value=str(ws)):
        # workspace 内合法
        assert file_tools._resolve_write("a.py").is_relative_to(ws.resolve())
        # 绝对越界路径被拒
        try:
            file_tools._resolve_write("/etc/passwd")
            assert False, "越界路径必须抛 WorkspaceEscapeError"
        except file_tools.WorkspaceEscapeError:
            pass


# ── P0-SEC-09：bootstrap admin 日志绝不含 token 明文 ──
def test_bootstrap_admin_token_not_logged(caplog):
    import logging

    from swarm.auth import store

    sentinel_token = "swarm_tok_SENTINEL_SECRET_abc123"
    with patch.object(store, "get_user_by_username", return_value=None), \
         patch.object(store, "generate_api_token", return_value=sentinel_token), \
         patch.object(store, "create_user", return_value=MagicMock()):
        with caplog.at_level(logging.WARNING):
            store.ensure_bootstrap_admin(password="swarm")
    assert sentinel_token not in caplog.text, "token 明文不得进入日志"


# ── P0-SEC-09：成员数查询失败 → fail-closed（非 admin 拒绝），不再 DB 抖动即授权 ──
def test_user_can_on_project_fail_closed_on_db_error():
    from swarm.auth.rbac import Role
    from swarm.auth.store import SwarmUser, user_can_on_project

    non_admin = SwarmUser(
        id="u1", username="dev", display_name="Dev",
        global_role=Role.DEVELOPER.value, must_change_password=False,
    )
    with patch("swarm.auth.store.count_project_members", side_effect=RuntimeError("db down")):
        assert user_can_on_project(non_admin, "task:write", "proj-1") is False


# ── P0-SEC-NEW：rbac 开启时 WS 无 token → authenticate_ws 返回 None（端点据此关闭）──
def test_authenticate_ws_rejects_missing_token():
    from swarm.api.auth import authenticate_ws

    ws = MagicMock()
    ws.headers = {}
    ws.query_params = {}
    fake_cfg = MagicMock()
    fake_cfg.rbac_enabled = True
    fake_cfg.api_key = ""
    with patch("swarm.api.auth.get_config", return_value=fake_cfg), \
         patch("swarm.api.auth.get_user_by_token", return_value=None):
        assert authenticate_ws(ws) is None


def test_authenticate_ws_allows_when_rbac_disabled():
    from swarm.api.auth import authenticate_ws

    ws = MagicMock()
    ws.headers = {}
    ws.query_params = {}
    fake_cfg = MagicMock()
    fake_cfg.rbac_enabled = False
    with patch("swarm.api.auth.get_config", return_value=fake_cfg):
        user = authenticate_ws(ws)
    assert user is not None and user.global_role


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
