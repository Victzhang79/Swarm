"""用户与项目成员存储。"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from swarm.auth.passwords import generate_api_token, hash_password, verify_password
from swarm.auth.rbac import Role, can, effective_project_role
from swarm.config.settings import DatabaseConfig

logger = logging.getLogger(__name__)

AUTH_DDL = """
CREATE TABLE IF NOT EXISTS swarm_users (
    id              TEXT PRIMARY KEY,
    username        TEXT UNIQUE NOT NULL,
    display_name    TEXT,
    password_hash   TEXT,
    api_token       TEXT UNIQUE,
    global_role     TEXT NOT NULL DEFAULT 'developer',
    must_change_password BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS swarm_project_members (
    project_id      TEXT NOT NULL,
    user_id         TEXT NOT NULL REFERENCES swarm_users(id) ON DELETE CASCADE,
    role            TEXT NOT NULL DEFAULT 'developer',
    created_at      TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (project_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_swarm_members_user ON swarm_project_members(user_id);
"""

_PROFILE_MIGRATION = """
ALTER TABLE mem_user_profile ADD COLUMN IF NOT EXISTS project_id TEXT NOT NULL DEFAULT '';
ALTER TABLE swarm_users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT false;
"""

_BOOTSTRAP_USERNAME = "admin"
_DEFAULT_BOOTSTRAP_PASSWORD = "swarm"  # 默认弱密码；用此值创建 admin 时强制改密(12.19)


@dataclass
class SwarmUser:
    id: str
    username: str
    display_name: str | None
    global_role: str
    api_token: str | None = None

    def has_permission(self, permission: str, *, project_role: str | None = None) -> bool:
        role = effective_project_role(self.global_role, project_role)
        if role == Role.ADMIN.value:
            return True
        return can(role, permission)


def _conn_str(db_config: DatabaseConfig | None = None) -> str:
    cfg = db_config or DatabaseConfig()
    return cfg.postgres_uri


def _pooled_conn(conn_str: str | None = None):
    """池化连接上下文管理器（autocommit）。退出时归还池而非关闭。"""
    from swarm.infra.db import sync_pool

    return sync_pool(conn_str).connection()


def ensure_auth_tables(conn_str: str | None = None) -> None:
    conn_str = conn_str or _conn_str()
    with psycopg.connect(conn_str, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(AUTH_DDL)
            # _PROFILE_MIGRATION 是对 memory 层的 mem_user_profile 表做 ALTER。
            # 全新空库若 auth 先于 memory 建表（如 CI 的 init_db 顺序），该表尚不存在，
            # 直接 ALTER 会报 relation 不存在。仅在表已存在时执行，否则交由 memory
            # 建表后再补（DEFAULT 已在 mem_user_profile DDL 里，缺这列也无碍）。
            cur.execute("SELECT to_regclass('mem_user_profile')")
            _row = cur.fetchone()
            if _row is not None and _row[0] is not None:
                cur.execute(_PROFILE_MIGRATION)
    logger.info("Auth tables ensured")


def count_users(conn_str: str | None = None) -> int:
    with _pooled_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM swarm_users")
            row = cur.fetchone()
            return int(row[0]) if row else 0


def ensure_bootstrap_admin(
    *,
    password: str = "swarm",
    reset_password: bool = False,
    conn_str: str | None = None,
) -> SwarmUser:
    """确保 admin 用户存在；可选重置密码（开发环境恢复）。

    安全（12.19）：当 admin 仍在使用默认弱密码 'swarm' 时，置 must_change_password=true，
    RBAC 开启时前端将强制首次登录改密。自定义了 SWARM_BOOTSTRAP_ADMIN_PASSWORD 的
    部署不触发（视为已主动设密）。RBAC 关闭（开发/CI）时该标志返回但不阻断登录。
    """
    must_change = (password == _DEFAULT_BOOTSTRAP_PASSWORD)
    record = get_user_by_username(_BOOTSTRAP_USERNAME, conn_str)
    if record is None:
        token = generate_api_token()
        user = create_user(
            username=_BOOTSTRAP_USERNAME,
            password=password,
            display_name="Administrator",
            global_role=Role.ADMIN.value,
            api_token=token,
            must_change_password=must_change,
            conn_str=conn_str,
        )
        logger.warning(
            "Default admin created: username=%s password=<bootstrap> token=%s%s",
            _BOOTSTRAP_USERNAME,
            token,
            " ⚠️ 使用默认弱密码 'swarm'，请尽快修改！" if must_change else "",
        )
        return user

    if reset_password or not record.get("password_hash"):
        update_user_password(record["id"], password, conn_str=conn_str)
        logger.warning("Default admin password synced from SWARM_BOOTSTRAP_ADMIN_PASSWORD")

    user = get_user_by_id(record["id"], conn_str)
    assert user is not None
    return user


def update_user_password(user_id: str, password: str, conn_str: str | None = None) -> None:
    pwd_hash = hash_password(password)
    with _pooled_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE swarm_users SET password_hash = %s, updated_at = now() WHERE id = %s",
                (pwd_hash, user_id),
            )


def get_must_change_password(user_id: str, conn_str: str | None = None) -> bool:
    """查询用户是否需强制改密（12.19）。列缺失时安全返回 False（兼容旧库未迁移）。"""
    with _pooled_conn(conn_str) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT must_change_password FROM swarm_users WHERE id = %s", (user_id,)
                )
                row = cur.fetchone()
            except Exception:  # noqa: BLE001 — 列不存在(旧库)等
                return False
    return bool(row[0]) if row else False


def clear_must_change_password(user_id: str, conn_str: str | None = None) -> None:
    """用户成功改密后清除强制改密标志（12.19）。"""
    with _pooled_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE swarm_users SET must_change_password = false, updated_at = now() WHERE id = %s",
                (user_id,),
            )


def ensure_admin_default_profile(user_id: str, conn_str: str | None = None) -> bool:
    """为 admin 写入全局默认画像（仅当不存在时）。"""
    from swarm.auth.default_profile import DEFAULT_ADMIN_PROFILE, GLOBAL_PROFILE_SUFFIX

    storage_key = profile_key(user_id, GLOBAL_PROFILE_SUFFIX)
    with _pooled_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM mem_user_profile WHERE user_id = %s",
                (storage_key,),
            )
            if cur.fetchone():
                return False
            cur.execute(
                """
                INSERT INTO mem_user_profile (user_id, profile_json, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (user_id) DO NOTHING
                """,
                (storage_key, Jsonb(DEFAULT_ADMIN_PROFILE)),
            )
    logger.info("Default admin profile seeded (key=%s)", storage_key)
    return True


def _row_to_user(row: tuple) -> SwarmUser:
    return SwarmUser(
        id=row[0],
        username=row[1],
        display_name=row[2],
        global_role=row[3],
        api_token=row[4] if len(row) > 4 else None,
    )


def create_user(
    *,
    username: str,
    password: str | None = None,
    display_name: str | None = None,
    global_role: str = Role.DEVELOPER.value,
    api_token: str | None = None,
    must_change_password: bool = False,
    conn_str: str | None = None,
) -> SwarmUser:
    user_id = str(uuid.uuid4())
    token = api_token or generate_api_token()
    pwd_hash = hash_password(password) if password else None
    with _pooled_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO swarm_users (id, username, display_name, password_hash, api_token, global_role, must_change_password)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, username, display_name, global_role, api_token
                """,
                (user_id, username, display_name, pwd_hash, token, global_role, must_change_password),
            )
            row = cur.fetchone()
    return _row_to_user(row)


def get_user_by_token(token: str, conn_str: str | None = None) -> SwarmUser | None:
    if not token:
        return None
    with _pooled_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, display_name, global_role, api_token
                FROM swarm_users WHERE api_token = %s
                """,
                (token,),
            )
            row = cur.fetchone()
    return _row_to_user(row) if row else None


def get_user_by_username(username: str, conn_str: str | None = None) -> dict[str, Any] | None:
    with _pooled_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, display_name, password_hash, global_role, api_token
                FROM swarm_users WHERE username = %s
                """,
                (username,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "username": row[1],
        "display_name": row[2],
        "password_hash": row[3],
        "global_role": row[4],
        "api_token": row[5],
    }


def get_user_by_id(user_id: str, conn_str: str | None = None) -> SwarmUser | None:
    with _pooled_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, display_name, global_role, api_token
                FROM swarm_users WHERE id = %s
                """,
                (user_id,),
            )
            row = cur.fetchone()
    return _row_to_user(row) if row else None


def authenticate(username: str, password: str, conn_str: str | None = None) -> SwarmUser | None:
    record = get_user_by_username(username, conn_str)
    if not record or not record.get("password_hash"):
        return None
    if not verify_password(password, record["password_hash"]):
        return None
    return SwarmUser(
        id=record["id"],
        username=record["username"],
        display_name=record["display_name"],
        global_role=record["global_role"],
        api_token=record["api_token"],
    )


def list_users(conn_str: str | None = None) -> list[dict[str, Any]]:
    with _pooled_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, display_name, global_role, created_at
                FROM swarm_users ORDER BY username
                """
            )
            rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "username": r[1],
            "display_name": r[2],
            "global_role": r[3],
            "created_at": r[4].isoformat() if r[4] else None,
        }
        for r in rows
    ]


def get_project_member_role(
    project_id: str,
    user_id: str,
    conn_str: str | None = None,
) -> str | None:
    with _pooled_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT role FROM swarm_project_members
                WHERE project_id = %s AND user_id = %s
                """,
                (project_id, user_id),
            )
            row = cur.fetchone()
    return row[0] if row else None


def set_project_member(
    project_id: str,
    user_id: str,
    role: str,
    conn_str: str | None = None,
) -> None:
    with _pooled_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO swarm_project_members (project_id, user_id, role)
                VALUES (%s, %s, %s)
                ON CONFLICT (project_id, user_id) DO UPDATE SET role = EXCLUDED.role
                """,
                (project_id, user_id, role),
            )


def list_project_members(project_id: str, conn_str: str | None = None) -> list[dict[str, Any]]:
    with _pooled_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.username, u.display_name, m.role
                FROM swarm_project_members m
                JOIN swarm_users u ON u.id = m.user_id
                WHERE m.project_id = %s
                ORDER BY u.username
                """,
                (project_id,),
            )
            rows = cur.fetchall()
    return [
        {"user_id": r[0], "username": r[1], "display_name": r[2], "role": r[3]}
        for r in rows
    ]


def count_project_members(project_id: str, conn_str: str | None = None) -> int:
    with _pooled_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM swarm_project_members WHERE project_id = %s",
                (project_id,),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0


def backfill_legacy_project_members(conn_str: str | None = None) -> int:
    """为无成员的旧项目添加全部已有用户（admin→owner，其余→developer）。

    每个项目的成员写入用显式事务包裹：要么全部授权成功，要么整个项目回滚，
    避免半授权状态（部分用户授权后崩溃 → 项目 COUNT>0 被永久跳过）。
    """
    from swarm.infra.db import sync_pool

    added = 0
    try:
        with sync_pool(conn_str).connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM projects")
                project_ids = [r[0] for r in cur.fetchall()]
                cur.execute("SELECT id, global_role FROM swarm_users ORDER BY created_at")
                users = cur.fetchall()
            if not users:
                return 0
            for pid in project_ids:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM swarm_project_members WHERE project_id = %s",
                        (pid,),
                    )
                    if int(cur.fetchone()[0]) > 0:
                        continue
                # 单项目成员写入：事务化（all-or-nothing）
                with conn.transaction():
                    with conn.cursor() as cur:
                        for uid, role in users:
                            member_role = (
                                Role.OWNER.value
                                if role == Role.ADMIN.value
                                else Role.DEVELOPER.value
                            )
                            cur.execute(
                                """
                                INSERT INTO swarm_project_members (project_id, user_id, role)
                                VALUES (%s, %s, %s)
                                ON CONFLICT DO NOTHING
                                """,
                                (pid, uid, member_role),
                            )
                added += 1
    except Exception as exc:
        logger.warning("backfill_legacy_project_members failed: %s", exc)
    if added:
        logger.info("Backfilled membership for %d legacy project(s)", added)
    return added


def list_user_project_ids(user_id: str, conn_str: str | None = None) -> set[str]:
    with _pooled_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT project_id FROM swarm_project_members WHERE user_id = %s",
                (user_id,),
            )
            rows = cur.fetchall()
    return {r[0] for r in rows}


def user_can_on_project(user: SwarmUser, permission: str, project_id: str | None) -> bool:
    if user.global_role == Role.ADMIN.value:
        return True
    if permission == "project:create" and project_id is None:
        return can(user.global_role, permission)
    member_role = None
    if project_id:
        try:
            member_count = count_project_members(project_id)
        except Exception as exc:
            logger.debug("member count unavailable for %s: %s", project_id, exc)
            member_count = 0
        if member_count == 0:
            # RBAC 之前创建的项目：无成员记录时保持登录用户可访问
            return can(user.global_role, permission)
        member_role = get_project_member_role(project_id, user.id)
        if member_role is None:
            return False
    role = effective_project_role(user.global_role, member_role)
    return can(role, permission)


def profile_key(user_id: str, project_id: str) -> str:
    return f"{user_id}:{project_id}"
