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
#
# ★批25 GS-5w 换锁★：以下三条为行为锁（原命题是 getsource 锚点+序断言：guard 判定须在
# 降级日志之前、文案锚点、降级路径保留）。现直接驱动 `_phase_prepare` 到「沙箱创建失败」
# 分支断言真实走向。
def _mk_sandbox_prepare_executor(project_id=None):
    from swarm.types import FileScope, SubTask
    from swarm.worker.executor import WorkerExecutor

    st = SubTask(id="st-sbc", description="改 A.java", scope=FileScope(writable=["A.java"]))
    return WorkerExecutor(subtask=st, project_path="/tmp/swarm-sbc-test",
                          project_id=project_id)


def _fake_cfg_sandbox(*, use=True, allow_fallback=False):
    from types import SimpleNamespace

    return SimpleNamespace(sandbox=SimpleNamespace(
        use_for_worker=use, api_url="http://cube.test",
        sandbox_health_check=False,  # 关掉探活重试：create 第一次抛即进失败分支
        allow_local_fallback=allow_fallback,
        template_for_language=lambda _lang, purpose="exec": "tpl-generic"))


def _drive_prepare_create_fail(ex, cfg, *, project_tpl=""):
    """沙箱启用 + manager.create 抛 130404，驱动 _phase_prepare 走完创建失败分支并返回。"""
    import asyncio

    from swarm.worker import executor as ex_mod

    mgr = MagicMock()
    mgr.create.side_effect = RuntimeError("130404 template not found")
    patches = [
        patch.object(ex_mod, "get_config", return_value=cfg),
        patch("swarm.worker.sandbox.get_sandbox_manager", return_value=mgr),
        patch("swarm.worker.sandbox_pool.pool_enabled", return_value=False),
        # 作废指纹是 fail-closed 的副产品（真实现会摸 store），与命题无关，摘成 no-op
        patch("swarm.worker.image_builder.invalidate_project_template_on_stale",
              return_value=False),
        patch.object(ex, "_create_agent", return_value=None),  # 降级路径不真建 LLM agent
    ]
    if project_tpl:
        # 项目配置声明了专属模板 → _sandbox_has_source=True（走 store 查询的那条线）
        patches.append(patch("swarm.project.store.get_project",
                             return_value={"config": {"sandbox_template": project_tpl}}))
    import contextlib
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return asyncio.run(ex._phase_prepare())


def test_sandbox_create_fail_with_project_source_is_fail_closed():
    """依赖项目专属镜像源码（_sandbox_has_source=True）+ 创建失败 → fail-closed 抛错，
    绝不降级本地空跑。★通用降级开关刻意【开着】★：guard 必须优先于通用降级拦下
    （原 getsource 序命题 idx_guard < idx_downgrade 的行为等价）。
    区分力：删掉/调换 `_sandbox_has_source` guard → 落通用降级不抛 → 红。"""
    import pytest

    ex = _mk_sandbox_prepare_executor(project_id="p1")
    with pytest.raises(RuntimeError, match="拒绝降级空跑"):
        _drive_prepare_create_fail(ex, _fake_cfg_sandbox(allow_fallback=True),
                                   project_tpl="tpl-proj")
    # 前提自证：走的确实是「依赖项目源码」那条线（否则上面的命题失去守护对象）
    assert ex._sandbox_has_source is True, "夹具前提失效：专属模板应置 _sandbox_has_source"
    assert any("fail-closed 不降级本地" in e for e in ex.execution_log), ex.execution_log
    assert not any("降级本地执行" in e for e in ex.execution_log), \
        "依赖项目源码时绝不出现降级本地日志"


def test_sandbox_create_fail_generic_task_keeps_explicit_fallback():
    """对照面：不依赖项目源码的通用任务 + 显式 ALLOW_LOCAL_FALLBACK → 降级本地保留
    （纯文字/无源码任务本地执行合法，不可一刀切全 fail-closed；原命题第 3 条）。
    区分力：删掉降级分支（通用失败也一律抛）→ 红。"""
    ex = _mk_sandbox_prepare_executor(project_id=None)
    out = _drive_prepare_create_fail(ex, _fake_cfg_sandbox(allow_fallback=True))
    assert out is None, f"降级后准备阶段应正常走完（agent 已 mock）: {out}"
    assert any("沙箱创建失败，降级本地执行" in e for e in ex.execution_log), ex.execution_log


def test_sandbox_disabled_still_runs_local():
    """对照面 2：沙箱未启用 → 本地执行路径保留（原命题第 3 条的另一半）。
    区分力：把未启用分支也改成抛错 → 红。"""
    import asyncio

    from swarm.worker import executor as ex_mod

    ex = _mk_sandbox_prepare_executor()
    with patch.object(ex_mod, "get_config", return_value=_fake_cfg_sandbox(use=False)), \
         patch.object(ex, "_create_agent", return_value=None):
        out = asyncio.run(ex._phase_prepare())
    assert out is None
    assert any("沙箱未启用，文件与命令将在本地执行" in e for e in ex.execution_log)


# ── 复用悬空引用隐患：预处理复用 project.config[sandbox_template] 前必须探活 CubeMaster，
#    模板被 TTL 过期/清理后 DB 记录仍在 → 复用悬空引用 → worker 创建沙箱报 130404。
#    实证 task 82f12ce4：tpl-2ebae48 及全部基础模板被清，DB 仍留记录。
def test_template_exists_probe_distinguishes_missing_vs_present():
    from swarm.worker import image_builder as ib

    # store 里只有 tpl-present；探活 tpl-missing 应返回 False、tpl-present 返回 True
    # B9 后探活走统一封装 query_cubemaster_templates（sandbox.py，httpx 双认证头），
    # mock 边界同步上移（行为断言不变：存在→True / 不存在→False / 空 id→False）。
    import swarm.worker.sandbox as _sb_mod

    from swarm.config import get_config
    _sb = get_config().sandbox
    _old_url = _sb.api_url
    _sb.api_url = "http://cubemaster.test/api"
    _orig = _sb_mod.query_cubemaster_templates
    try:
        _sb_mod.query_cubemaster_templates = lambda cfg, timeout=10.0: [
            {"id": "tpl-present", "status": "READY", "imageInfo": ""}]
        assert ib.template_exists_in_cubemaster("tpl-present") is True, "存在的模板应判 True"
        assert ib.template_exists_in_cubemaster("tpl-missing") is False, "被清的模板应判 False（触发重建）"
        assert ib.template_exists_in_cubemaster("") is False, "空 id 直接 False"
    finally:
        _sb_mod.query_cubemaster_templates = _orig
        _sb.api_url = _old_url


def test_template_exists_probe_returns_none_on_network_error():
    """探活本身失败（网络/认证）→ None（无法判定，调用方保守复用+告警，不误触发重建）。"""
    from swarm.worker import image_builder as ib
    import swarm.worker.sandbox as _sb_mod

    from swarm.config import get_config
    _sb = get_config().sandbox
    _old_url = _sb.api_url
    _sb.api_url = "http://cubemaster.test/api"  # 须有效 url，否则 CI 空 api_url 会先早返 None（绕过本测的网络错误路径）

    _orig = _sb_mod.query_cubemaster_templates
    try:
        _sb_mod.query_cubemaster_templates = lambda cfg, timeout=10.0: None  # 查询失败
        assert ib.template_exists_in_cubemaster("tpl-x") is None, "探活失败应返回 None（无法判定）"
    finally:
        _sb_mod.query_cubemaster_templates = _orig
        _sb.api_url = _old_url


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
