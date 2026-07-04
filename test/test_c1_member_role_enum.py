"""C1（round22, P1·最高 ROI）：项目成员 role 无枚举校验 → 写 "admin" 项目级通配符提权。

根因：MemberRequest.role 无枚举校验，set_member_api 原样入库；effective_project_role 采信
member_role，can("admin", perm) 因 "*" 恒真 → 全局 developer/owner 可把成员 role 设为 "admin"
获项目级全权。

治本：MemberRequest.role 加项目级角色白名单校验（owner/developer/viewer，排除 admin），
非法值 pydantic ValidationError → FastAPI 自动 422。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from swarm.api.routers.auth import MemberRequest
from swarm.auth.rbac import PROJECT_ASSIGNABLE_ROLES


def test_admin_role_rejected():
    with pytest.raises(ValidationError):
        MemberRequest(user_id="u1", role="admin")


def test_arbitrary_role_rejected():
    with pytest.raises(ValidationError):
        MemberRequest(user_id="u1", role="superuser")


@pytest.mark.parametrize("role", sorted(PROJECT_ASSIGNABLE_ROLES))
def test_valid_project_roles_accepted(role):
    m = MemberRequest(user_id="u1", role=role)
    assert m.role == role


def test_admin_excluded_from_assignable():
    assert "admin" not in PROJECT_ASSIGNABLE_ROLES


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
