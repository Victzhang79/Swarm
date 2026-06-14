"""12.19 修复回归测试：默认弱密码 admin 强制改密标志（must_change_password）。

修复点：
- ensure_bootstrap_admin 用默认密码 'swarm' 创建 admin 时置 must_change_password=true；
  自定义密码不置位。
- change-password 成功后清除标志。
- 登录响应携带 must_change_password（RBAC 关闭时仍返回，但前端不阻断 → 不破坏 CI）。

本测试触真实 PG，严格测试铁律：仅用 _test_ 前缀隔离 username，try/finally 清理，
绝不碰真 admin 账号。需要本地 PG。
"""

from __future__ import annotations

import uuid

from swarm.auth.store import (
    clear_must_change_password,
    create_user,
    ensure_auth_tables,
    get_must_change_password,
)

_TEST_USER_FORCED = f"_test_12_19_forced_{uuid.uuid4().hex[:8]}"
_TEST_USER_CUSTOM = f"_test_12_19_custom_{uuid.uuid4().hex[:8]}"


def _cleanup(usernames):
    import psycopg

    from swarm.config.settings import DatabaseConfig
    with psycopg.connect(DatabaseConfig().postgres_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            for u in usernames:
                cur.execute("DELETE FROM swarm_users WHERE username = %s", (u,))


def test_must_change_password_flag_lifecycle():
    ensure_auth_tables()
    try:
        # 模拟"默认弱密码"创建：显式置 must_change_password=True
        forced = create_user(
            username=_TEST_USER_FORCED,
            password="swarm",
            display_name="forced",
            must_change_password=True,
        )
        # 模拟自定义密码：不置位
        custom = create_user(
            username=_TEST_USER_CUSTOM,
            password="a-strong-custom-pw",
            display_name="custom",
            must_change_password=False,
        )

        assert get_must_change_password(forced.id) is True, "默认弱密码用户应需强制改密"
        assert get_must_change_password(custom.id) is False, "自定义密码用户不应被强制"

        # 改密后清标志
        clear_must_change_password(forced.id)
        assert get_must_change_password(forced.id) is False, "改密后强制标志应清除"
    finally:
        _cleanup([_TEST_USER_FORCED, _TEST_USER_CUSTOM])


if __name__ == "__main__":
    try:
        test_must_change_password_flag_lifecycle()
        print("  ✅ test_must_change_password_flag_lifecycle")
        print("\n=== 12.19 force password change: 1/1 passed ===")
    except AssertionError as e:
        print(f"  ❌ {e}")
        raise
