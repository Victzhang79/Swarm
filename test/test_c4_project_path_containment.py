"""C4（round22, P1·config 开关）：POST /api/projects 可注册任意可读目录 → 路径 IDOR + 摄取。

根因：仅黑名单 /etc 等系统目录，不强制 containment；多租户共享宿主机下可注册他人目录并触发索引摄取。

治本：加 SWARM_ALLOW_EXTERNAL_PROJECT_PATH（默认 true，不破坏"指向本机已有外部项目"工作流，
如 E2E 的 e2e-projects/RuoYi）；false 时强制 containment 到 workspace。★不用硬 whitelist——那会
破坏用户自己的 E2E（子 agent 误修，已纠偏）。
"""
from __future__ import annotations

import os

import pytest
from fastapi import HTTPException

from swarm.api.routers.project import (
    _enforce_project_path_containment,
    _env_allow_external_project_path,
)


def test_allow_external_default_true(monkeypatch):
    monkeypatch.delenv("SWARM_ALLOW_EXTERNAL_PROJECT_PATH", raising=False)
    assert _env_allow_external_project_path() is True


def test_allow_external_false_when_set(monkeypatch):
    monkeypatch.setenv("SWARM_ALLOW_EXTERNAL_PROJECT_PATH", "false")
    assert _env_allow_external_project_path() is False


def test_external_path_allowed_by_default(tmp_path):
    """默认允许：workspace 外的合法本机项目（如 e2e-projects/RuoYi）不被拒。"""
    external = str(tmp_path / "e2e-projects" / "RuoYi")
    _enforce_project_path_containment(external, "/some/workspace", allow_external=True)  # 不抛即通过


def test_external_path_rejected_when_disabled(tmp_path):
    """开关关闭：workspace 外路径被拒（多租户加固）。"""
    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace, exist_ok=True)
    external = str(tmp_path / "outside" / "secret")
    with pytest.raises(HTTPException) as ei:
        _enforce_project_path_containment(external, workspace, allow_external=False)
    assert ei.value.status_code == 400


def test_inside_workspace_allowed_when_disabled(tmp_path):
    """开关关闭：workspace 内路径放行。"""
    workspace = str(tmp_path / "workspace")
    inside = str(tmp_path / "workspace" / "proj1")
    os.makedirs(inside, exist_ok=True)
    _enforce_project_path_containment(inside, workspace, allow_external=False)  # 不抛即通过


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
