"""C5（round22, P2·多用户）：HTTP task 端点 404 vs 403 分裂 → task_id 存在性枚举。

根因：get_task→None→404、存在但无权→_require_perm 抛 403，可区分 → 枚举有效 task_id。
WS 端点已统一为 generic 拒绝，HTTP 未对齐。

治本：_require_task_access ——不存在与无权返回【同一】404（认证失败仍 401，与存在性无关）。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from swarm.api.routers import task as taskmod


def test_missing_task_returns_404():
    with patch("swarm.api._shared._require_user", return_value=MagicMock()):
        with pytest.raises(HTTPException) as ei:
            taskmod._require_task_access(MagicMock(), None, "tid", "task:read")
    assert ei.value.status_code == 404


def test_no_permission_also_returns_404_not_403():
    """存在但无权 → 也返回 404（与不存在不可区分，杜绝枚举）。"""
    with patch("swarm.api._shared._require_user", return_value=MagicMock()), \
         patch("swarm.auth.store.user_can_on_project", return_value=False):
        with pytest.raises(HTTPException) as ei:
            taskmod._require_task_access(MagicMock(), {"project_id": "p1"}, "tid", "task:read")
    assert ei.value.status_code == 404, "无权必须与不存在返回同一 404"


def test_authorized_returns_task():
    t = {"project_id": "p1"}
    with patch("swarm.api._shared._require_user", return_value=MagicMock()), \
         patch("swarm.auth.store.user_can_on_project", return_value=True):
        got = taskmod._require_task_access(MagicMock(), t, "tid", "task:read")
    assert got is t


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
