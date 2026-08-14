#!/usr/bin/env python3
"""30 号文批16 GS-2 锁：sandbox IT 纳入 needs_service 族（探测挪 runtest setup）。

原病：`test_sandbox_integration.py:57` 的 `pytestmark = skipif(not _sandbox_reachable())`
在【模块导入期（collection）】发 httpx 探测（违 T-7「runtest setup 才求值」形状，
conftest.py:357 注释族），且无 needs_service 门控 ⇒ 沙箱可达时一次普通全量就跑真沙箱
create/run/kill（共享 infra 被每次全量摸）。

治法：`needs_service("sandbox")` 标记 + conftest `_probe_sandbox`（含
SWARM_RUN_SANDBOX_IT=1 强制档与 hunter H1 短退避重试）。
"""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest


def test_sandbox_registered_in_service_probes(service_probe_internals):
    """接线锁：sandbox 探针必须登记进 _SERVICE_PROBES（漏登记=标记 fail-closed 报
    「未知服务」反而是好事，但本锁让缺失形态直接可读）。"""
    assert "sandbox" in service_probe_internals["probes"]
    assert callable(service_probe_internals["probes"]["sandbox"])


def _load_sandbox_it_module():
    """以独立模块名动态加载 test_sandbox_integration.py（避免与 pytest 已收集的
    模块对象混淆——本测试只验模块顶层形状，不跑其用例）。"""
    path = Path(__file__).resolve().parent / "test_sandbox_integration.py"
    spec = importlib.util.spec_from_file_location("sandbox_it_shape_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pytestmark_is_needs_service_mark():
    """接线事实锁：模块 pytestmark 必须含 needs_service("sandbox")——
    若退回 skipif(_sandbox_reachable()) 形态（collection 期求值）本锁红。
    hunter M5：pytestmark 可能是单标记或列表（将来加 slow 等标记时不许误炸），
    统一归一成列表再找。"""
    mod = _load_sandbox_it_module()
    marks = mod.pytestmark
    if not isinstance(marks, list):
        marks = [marks]
    ns = [m for m in marks if getattr(m, "name", None) == "needs_service"]
    assert ns, f"pytestmark 必须含 needs_service 标记，实际: {marks!r}"
    assert ("sandbox",) in [m.args for m in ns], \
        f"needs_service 必须门控 sandbox，实际: {[m.args for m in ns]!r}"


def test_module_import_fires_no_http_probe(monkeypatch):
    """GS-2 核心行为锁：模块加载（=collection 期）绝不发网络探测。
    旧形状（skipif 实参调 _sandbox_reachable / _code_interpreter_supported）会打 1 次=红。
    hunter M4：只盯 httpx.get 会被 requests/urllib/socket 直连绕过——传输层
    socket.create_connection 是一切 HTTP 库的汇聚点，一并间谍。"""
    import httpx
    import socket
    import urllib.request

    calls = []

    def _spy(*a, **k):
        calls.append((a, k))
        raise AssertionError("collection 期不得发网络探测")

    monkeypatch.setattr(httpx, "get", _spy)
    monkeypatch.setattr(httpx.Client, "get", _spy)
    monkeypatch.setattr(urllib.request, "urlopen", _spy)
    monkeypatch.setattr(socket, "create_connection", _spy)
    monkeypatch.delenv("SWARM_RUN_SANDBOX_IT", raising=False)
    _load_sandbox_it_module()
    assert calls == [], f"模块加载期发出网络探测（GS-2 原病形态）: {calls}"


def test_probe_force_flag_short_circuits(service_probe_internals, monkeypatch):
    """SWARM_RUN_SANDBOX_IT=1 强制档：探测视为通过（CI 集成阶段要让真失败响亮，
    不被 skip 吞掉）。"""
    monkeypatch.setenv("SWARM_RUN_SANDBOX_IT", "1")
    assert service_probe_internals["probes"]["sandbox"]() is None


def test_probe_unreachable_returns_error_string(service_probe_internals, monkeypatch):
    """不可达 → 返回错误字符串（require_service 据此前往 skip/硬失败），绝不抛异常。
    hunter H1：失败结论前必须重试过一次（恰 2 次调用）——否则 30s 冷却会把一次
    瞬时抖动放大成整批冤 skip。"""
    monkeypatch.delenv("SWARM_RUN_SANDBOX_IT", raising=False)
    monkeypatch.setattr(time, "sleep", lambda *a: None)  # 退避不等真墙钟
    import httpx

    calls = []

    def _down(*a, **k):
        calls.append(1)
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", _down)
    err = service_probe_internals["probes"]["sandbox"]()
    assert isinstance(err, str) and "refused" in err
    assert len(calls) == 2, f"失败结论前须重试一次，实际调用 {len(calls)} 次"


def test_probe_retries_once_on_transient_failure(service_probe_internals, monkeypatch):
    """hunter H1 主锁：第一次瞬时超时、第二次可达 ⇒ 探针必须返回 None（恰 2 次调用）。
    删掉重试逻辑（只调 1 次即下失败结论）本锁红。"""
    monkeypatch.delenv("SWARM_RUN_SANDBOX_IT", raising=False)
    monkeypatch.setattr(time, "sleep", lambda *a: None)
    import httpx

    calls = []

    class _Resp:
        status_code = 200

    def _flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("refused")
        return _Resp()

    monkeypatch.setattr(httpx, "get", _flaky)
    assert service_probe_internals["probes"]["sandbox"]() is None
    assert len(calls) == 2


def test_probe_no_config_skips_retry(service_probe_internals, monkeypatch):
    """hunter R2-L 锁：api_url 未配置 = 确定性事实 ⇒ 直接返回且【零 HTTP 调用】
    （重试对确定性结论无意义；且判定必须走控制流而非魔法子串）。"""
    monkeypatch.delenv("SWARM_RUN_SANDBOX_IT", raising=False)
    import httpx

    calls = []

    def _spy(*a, **k):
        calls.append(1)
        raise AssertionError("未配置时不得发 HTTP")

    monkeypatch.setattr(httpx, "get", _spy)

    class _SandboxCfg:
        api_url = ""

    class _Cfg:
        sandbox = _SandboxCfg()

    monkeypatch.setattr(
        "swarm.config.settings.get_config", lambda: _Cfg())
    err = service_probe_internals["probes"]["sandbox"]()
    assert isinstance(err, str) and "未配置" in err
    assert calls == []


def test_require_service_sandbox_skips_when_down(service_probe_internals, monkeypatch):
    """行为锁（hunter M5）：探测失败且未置 SWARM_TEST_REQUIRE_SERVICES ⇒
    require_service("sandbox") 必须 pytest.skip（而非放行或硬失败）。"""
    st = service_probe_internals
    monkeypatch.delenv("SWARM_RUN_SANDBOX_IT", raising=False)
    monkeypatch.delenv("SWARM_TEST_REQUIRE_SERVICES", raising=False)
    monkeypatch.setitem(st["probes"], "sandbox", lambda: "ConnectError: refused")
    st["cache"].pop("sandbox", None)
    st["failed_at"].pop("sandbox", None)
    with pytest.raises(pytest.skip.Exception, match="SERVICE_ABSENT:sandbox"):
        st["require"]("sandbox")
    # 还原会话账：否则会话末汇总会打出本轮并不存在的 SERVICE_ABSENT（假信号）
    st["absent_seen"].discard("sandbox")
    st["cache"].pop("sandbox", None)
    st["failed_at"].pop("sandbox", None)
