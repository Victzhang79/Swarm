"""FastAPI 鉴权依赖。"""

from __future__ import annotations

from fastapi import HTTPException, Request

from swarm.auth.rbac import Role
from swarm.auth.store import SwarmUser, user_can_on_project
from swarm.config.settings import get_config


def get_current_user(request: Request) -> SwarmUser:
    user = getattr(request.state, "user", None)
    if user is None:
        cfg = get_config()
        if not cfg.rbac_enabled:
            return SwarmUser(
                id="anonymous",
                username="dev",
                display_name="Dev",
                global_role=Role.ADMIN.value,
                must_change_password=False,
            )
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_permission(permission: str, project_id: str | None = None):
    """返回 FastAPI 依赖：校验当前用户对 project 的权限。"""

    def _dep(request: Request) -> SwarmUser:
        user = get_current_user(request)
        pid = project_id
        if pid is None:
            pid = request.path_params.get("project_id")
        if not user_can_on_project(user, permission, pid):
            raise HTTPException(status_code=403, detail=f"Permission denied: {permission}")
        return user

    return _dep
