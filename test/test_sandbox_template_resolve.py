#!/usr/bin/env python3
"""治本：沙箱模板随 CubeMaster 真实可用集解析 + 配置漂移自愈兜底（_resolve_template）。

实证根因(2026-06-29)：服务器只剩 3 个 READY 模板，.env/DB/默认全配早被回收的 ID →
每次 create 都 130404 template not found → worker 静默降级本地 / WebUI 创建失败。
治本=不死配，随服务器真实清单解析（配置在→用；漂移→优先项目匹配镜像→任一 READY；
拿不到清单→沿用配置不擅改）。
"""
from __future__ import annotations

from unittest.mock import patch

from swarm.config.settings import SandboxConfig
from swarm.worker.sandbox import SandboxManager

_SERVER = [
    {"id": "tpl-projA", "status": "READY", "imageInfo": "sandbox-proj-5d0e9db8-d00:abc"},
    {"id": "tpl-projB", "status": "READY", "imageInfo": "sandbox-proj-85ae38dc-36f:def"},
    {"id": "tpl-building", "status": "BUILDING", "imageInfo": "sandbox-proj-5d0e9db8-d00:zzz"},
]


def _mgr():
    return SandboxManager(SandboxConfig(api_url="http://x", default_template="tpl-default-stale"))


def _patch(items):
    return patch.object(SandboxManager, "_fetch_server_templates", return_value=items)


def test_configured_template_present_is_respected():
    """配置值在服务器 READY 集 → 直接用（尊重显式配置）。"""
    with _patch(_SERVER):
        assert _mgr()._resolve_template("tpl-projB", project_id=None) == "tpl-projB"


def test_drifted_template_picks_project_matched_image():
    """配置值不在服务器(漂移) + 有 project_id → 选 imageInfo 项目匹配的 READY 镜像。"""
    with _patch(_SERVER):
        got = _mgr()._resolve_template("tpl-default-stale", project_id="5d0e9db8-d000-40f6")
    assert got == "tpl-projA", "应选本项目(5d0e9db8)烤的 READY 镜像"


def test_drifted_no_project_match_falls_back_to_any_ready():
    """配置漂移 + 无项目匹配 → 退任一 READY（绝不返回不存在的配置 ID）。"""
    with _patch(_SERVER):
        got = _mgr()._resolve_template("tpl-default-stale", project_id="ffffffff-0000")
    assert got in {"tpl-projA", "tpl-projB"}, got


def test_building_template_not_chosen():
    """非 READY(BUILDING) 模板不应被选中。"""
    only_building = [{"id": "tpl-building", "status": "BUILDING", "imageInfo": ""}]
    with _patch(only_building):
        # 无 READY → 沿用配置（如实暴露，create 会失败但不擅自用未就绪模板）
        assert _mgr()._resolve_template("tpl-stale", project_id="p") == "tpl-stale"


def test_empty_server_list_keeps_configured():
    """拿不到服务器清单(网络等)→ 沿用配置，不擅改（按原行为如实暴露）。"""
    with _patch([]):
        assert _mgr()._resolve_template("tpl-stale", project_id="p") == "tpl-stale"


def test_default_template_used_when_none_passed():
    """template 传 None → 取 config.default_template 参与解析。"""
    with _patch(_SERVER):
        # default-stale 不在集内、无项目匹配 → 退任一 READY
        assert _mgr()._resolve_template(None, project_id=None) in {"tpl-projA", "tpl-projB"}


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
