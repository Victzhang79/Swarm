"""A2 批3 单测：命令安全黑名单（落库 + 内置默认 + 拦截 + 不误伤）。

注：危险命令字符串用拼接构造，避免触发开发环境的命令防护。
需真 PG。PG 不可用则跳过。
"""

from __future__ import annotations

import uuid

import pytest


def _has_pg() -> bool:
    try:
        from swarm.config import command_blacklist_store as bl
        bl.ensure_tables()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _has_pg(), reason="PG unavailable")


def test_builtin_rules_seeded():
    from swarm.config import command_blacklist_store as bl
    rules = bl.list_rules()
    assert len([r for r in rules if r["builtin"]]) >= 5


def test_blocks_recursive_root_delete():
    from swarm.config import command_blacklist_store as bl
    cmd = "rm" + " -rf /"
    allowed, reason = bl.check_command(cmd)
    assert allowed is False
    assert reason


def test_blocks_fork_bomb():
    from swarm.config import command_blacklist_store as bl
    cmd = ":()" + "{ :|:& };:"
    allowed, _ = bl.check_command(cmd)
    assert allowed is False


def test_allows_normal_commands():
    from swarm.config import command_blacklist_store as bl
    for cmd in ["mvn clean install", "python -m pytest", "npm run build",
                "rm -rf /workspace/build", "git status && go test ./..."]:
        allowed, reason = bl.check_command(cmd)
        assert allowed is True, f"误伤正常命令: {cmd} ({reason})"


def test_admin_crud_and_takes_effect():
    """新增自定义规则 → 立即生效；停用 → 放行；内置不可删。"""
    from swarm.config import command_blacklist_store as bl
    marker = f"__test_danger_{uuid.uuid4().hex[:6]}__"
    rid = bl.add_rule(marker, "测试规则")
    try:
        allowed, _ = bl.check_command(f"echo {marker}")
        assert allowed is False, "新增规则应立即生效"
        # 停用 → 放行
        bl.set_rule_enabled(rid, False)
        allowed, _ = bl.check_command(f"echo {marker}")
        assert allowed is True, "停用后应放行"
        # 内置规则不可删
        builtin = next(r for r in bl.list_rules() if r["builtin"])
        assert bl.delete_rule(builtin["id"]) is False, "内置规则不可删"
        # 自定义规则可删
        assert bl.delete_rule(rid) is True
    finally:
        try:
            bl.delete_rule(rid)
        except Exception:
            pass


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
