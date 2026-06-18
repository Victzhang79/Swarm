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


# ── P0-SEC-08 延伸：沙箱「应启用且依赖项目专属镜像源码」但【创建失败】→ fail-closed，
#    不降级本地空跑。原 except Exception 只对 SandboxUnhealthyError(运行中熔断)fail-closed，
#    漏了创建失败：实证 task 82f12ce4「推箱子」——项目镜像 tpl 在 CubeMaster 丢失
#    (130404 template not found) → 降级本地 → 本地无源码 → diff=5 空产出 → 3 次重试全败
#    → escalate，把"镜像不可用"伪装成"任务失败"。修复：_sandbox_has_source=True 时创建失败抛错。
def test_sandbox_create_fail_with_project_source_is_fail_closed():
    import inspect
    from swarm.worker import executor as ex

    src = inspect.getsource(ex)
    # 1) fail-closed 分支存在且以 _sandbox_has_source 为判据
    assert "_sandbox_has_source" in src, "应以 _sandbox_has_source 区分是否依赖项目源码"
    assert "fail-closed 不降级本地" in src or "拒绝降级空跑" in src, \
        "创建失败且依赖项目源码时应 fail-closed（抛错）而非降级本地"
    # 2) 结构守护：fail-closed 的 raise 必须在 _sandbox_has_source 判定之内、降级日志之前
    idx_guard = src.find("if getattr(self, \"_sandbox_has_source\", False):")
    idx_downgrade = src.find("沙箱创建失败，降级本地执行")
    assert idx_guard != -1 and idx_downgrade != -1, "两条路径都应存在（依赖源码=fail-closed / 通用=降级）"
    assert idx_guard < idx_downgrade, "fail-closed 判定应在通用降级之前（依赖源码优先拦截）"
    # 3) 通用降级路径仍保留（纯文字/无源码任务本地执行合法，不可一刀切全 fail-closed）
    assert "沙箱未启用，文件与命令将在本地执行" in src, "沙箱未启用时本地执行应保留"


# ── 复用悬空引用隐患：预处理复用 project.config[sandbox_template] 前必须探活 CubeMaster，
#    模板被 TTL 过期/清理后 DB 记录仍在 → 复用悬空引用 → worker 创建沙箱报 130404。
#    实证 task 82f12ce4：tpl-2ebae48 及全部基础模板被清，DB 仍留记录。
def test_template_exists_probe_distinguishes_missing_vs_present():
    from swarm.worker import image_builder as ib

    class _FakeResp:
        def __init__(self, body):
            self._b = body
        def read(self):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    import json as _json

    # store 里只有 tpl-present；探活 tpl-missing 应返回 False、tpl-present 返回 True
    body = _json.dumps([{"templateID": "tpl-present", "status": "READY"}]).encode()

    # monkeypatch 函数内 import 的 urllib.request.urlopen
    import urllib.request as _ur
    _orig = _ur.urlopen
    try:
        _ur.urlopen = lambda *a, **k: _FakeResp(body)
        assert ib.template_exists_in_cubemaster("tpl-present") is True, "存在的模板应判 True"
        assert ib.template_exists_in_cubemaster("tpl-missing") is False, "被清的模板应判 False（触发重建）"
        assert ib.template_exists_in_cubemaster("") is False, "空 id 直接 False"
    finally:
        _ur.urlopen = _orig


def test_template_exists_probe_returns_none_on_network_error():
    """探活本身失败（网络/认证）→ None（无法判定，调用方保守复用+告警，不误触发重建）。"""
    from swarm.worker import image_builder as ib
    import urllib.request as _ur

    _orig = _ur.urlopen
    try:
        def _boom(*a, **k):
            raise OSError("connection refused")
        _ur.urlopen = _boom
        assert ib.template_exists_in_cubemaster("tpl-x") is None, "探活失败应返回 None（无法判定）"
    finally:
        _ur.urlopen = _orig


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
