"""#29-8 M-1：项目数软限制（SWARM_MAX_ACTIVE_PROJECTS）接线验收。

机制（env 登记/机读键/WARNING）此前零生产调用=死账——运维以为有保护，
实际第 N+1 个项目照进，PG/Qdrant/沙箱预算被悄悄超订。
接线点=项目创建端点（新项目才增加活跃项目数；既有项目上新任务不增，不拦）。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from swarm.api.routers import project as proj_router
from swarm.infra import redis_client


def _call(req):
    return asyncio.run(proj_router.create_project(req, request=None))


@pytest.fixture
def _bypass_auth(monkeypatch):
    monkeypatch.setattr(proj_router, "_require_perm", lambda request, perm: object())


def test_over_limit_rejects_409(_bypass_auth, monkeypatch):
    monkeypatch.setattr(
        redis_client, "check_project_limit",
        lambda: {"active": 10, "limit": 10, "warn": True,
                 "message": "活跃项目数 (10) 已达软限制 (10)，建议清理不活跃项目"})
    with pytest.raises(HTTPException) as exc:
        _call(proj_router.ProjectCreateRequest(name="x", path="/tmp/whatever"))
    assert exc.value.status_code == 409
    assert "软限制" in str(exc.value.detail)


def test_under_limit_proceeds_past_gate(_bypass_auth, monkeypatch):
    """未超限 → 过闸继续走路径校验（拿 400 路径不存在证明越过了软限制闸）。"""
    monkeypatch.setattr(
        redis_client, "check_project_limit",
        lambda: {"active": 3, "limit": 10, "warn": False, "message": "正常"})
    with pytest.raises(HTTPException) as exc:
        _call(proj_router.ProjectCreateRequest(name="x", path="/tmp/_nonexistent_m1_path"))
    assert exc.value.status_code == 400
    assert "路径不存在" in str(exc.value.detail)


def test_pg_unavailable_fail_open_with_warning(_bypass_auth, monkeypatch, caplog):
    """PG 不可用（active=-1）→ 软闸不阻断（可用性优先），但 WARNING 留痕。"""
    monkeypatch.setattr(
        redis_client, "check_project_limit",
        lambda: {"active": -1, "limit": 10, "warn": False, "message": "无法查询项目列表: boom"})
    with caplog.at_level("WARNING"):
        with pytest.raises(HTTPException) as exc:
            _call(proj_router.ProjectCreateRequest(name="x", path="/tmp/_nonexistent_m1_path"))
    assert exc.value.status_code == 400, "软闸 fail-open 后继续走原流程"
    assert any("软限制检查不可用" in r.message for r in caplog.records), \
        "fail-open 必须留 WARNING（硬检查④降级可观测）"
