"""12.2 修复回归测试：启动孤儿沙箱清扫的 opt-in 开关。

问题：_sweep_startup_orphans 无差别 kill 服务器上所有沙箱，基于"单进程独占集群"
假设；共享 CubeSandbox 集群下会误杀其他实例/用户的沙箱。

修复（A 止血）：新增 SandboxConfig.sweep_orphans_on_startup（默认 True 保持单机
行为），共享集群部署设 False 则跳过清扫——不触碰任何远程沙箱。

本测试用 mock 隔离，不连真 CubeSandbox。
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import swarm.api.app  # noqa: F401

# 注意：swarm.api.app 模块内有 `app = FastAPI()`，`import ... as app_mod` 在某些
# 解析下会拿到 app 实例而非模块。用 sys.modules 明确取模块对象。
app_mod = sys.modules["swarm.api.app"]


def _run_sweep_with_flag(flag: bool):
    """在 sweep_orphans_on_startup=flag 下执行 _sweep_startup_orphans，
    返回 _fetch_sandbox_list_from_server 是否被调用。"""
    fake_cfg = MagicMock()
    fake_cfg.sandbox.sweep_orphans_on_startup = flag
    with patch("swarm.config.settings.get_config", return_value=fake_cfg), \
         patch.object(app_mod, "_fetch_sandbox_list_from_server", return_value=[]) as fetch_mock, \
         patch.object(app_mod, "_get_sandbox_manager", return_value=MagicMock()):
        app_mod._sweep_startup_orphans()
    return fetch_mock.called


def test_sweep_disabled_skips_all_remote_calls():
    """开关关闭 → 不调用任何远程沙箱列表/kill（共享集群安全模式）。"""
    called = _run_sweep_with_flag(False)
    assert called is False, "关闭开关后不应触碰远程沙箱（误杀风险）"


def test_sweep_enabled_proceeds():
    """开关开启（默认）→ 正常执行清扫流程（拉取服务器列表）。"""
    called = _run_sweep_with_flag(True)
    assert called is True, "开启开关应正常拉取并清扫"


if __name__ == "__main__":
    import sys
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}: {e}")
    print(f"\n=== 12.2 sweep opt-in: {len(fns) - failed}/{len(fns)} passed ===")
    sys.exit(1 if failed else 0)
