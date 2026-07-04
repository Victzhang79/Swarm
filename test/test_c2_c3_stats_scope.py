"""C2/C3（round22, P1·多用户）：/api/stats 与 /api/stats/token-usage 跨项目泄露。

根因：无 project_id 时仅 _require_user，然后聚合全库任务统计/全项目 token 用量（含最近 10 条
跨项目 description/token_usage）。任意登录用户可越权读他项目活动。

治本：无 project_id 时用 _accessible_project_ids_or_none 限成员项目（admin→None 全量），
store.get_task_stats / usage_tracker.get_token_usage_stats 加 project_ids 过滤。

行为测试：验证端点把成员项目 scope 正确透传给底层（admin→None，非 admin→成员集）。
"""
from __future__ import annotations

import asyncio
import importlib

from unittest.mock import MagicMock, patch

# 注意：swarm.api.__init__ 导出 app(FastAPI 实例)，会遮蔽子模块名 → 用 importlib 取真模块。
appmod = importlib.import_module("swarm.api.app")


def _run_get_stats(scope):
    captured = {}

    def _fake_stats(project_id=None, *, project_ids=None):
        captured["project_id"] = project_id
        captured["project_ids"] = project_ids
        return {}

    with patch.object(appmod.store, "get_task_stats", side_effect=_fake_stats), \
         patch.object(appmod, "_accessible_project_ids_or_none", return_value=scope), \
         patch("swarm.api._shared._require_user", return_value=MagicMock()):
        asyncio.run(appmod.get_stats(MagicMock(), project_id=None))
    return captured


def test_stats_non_admin_scoped_to_member_projects():
    cap = _run_get_stats({"p1", "p2"})
    assert cap["project_ids"] == {"p1", "p2"}, cap


def test_stats_admin_gets_none_scope():
    cap = _run_get_stats(None)
    assert cap["project_ids"] is None, cap


def _run_token_usage(scope):
    captured = {}

    def _fake_usage(*, project_ids=None):
        captured["project_ids"] = project_ids
        return {}

    with patch("swarm.models.usage_tracker.get_token_usage_stats", side_effect=_fake_usage), \
         patch.object(appmod, "_accessible_project_ids_or_none", return_value=scope), \
         patch("swarm.api._shared._require_user", return_value=MagicMock()):
        asyncio.run(appmod.get_token_usage(MagicMock()))
    return captured


def test_token_usage_non_admin_scoped():
    cap = _run_token_usage({"p1"})
    assert cap["project_ids"] == {"p1"}, cap


def test_token_usage_admin_none_scope():
    cap = _run_token_usage(None)
    assert cap["project_ids"] is None, cap


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
