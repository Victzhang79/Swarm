"""A2 批3 单测：命令安全黑名单（落库 + 内置默认 + 拦截 + 不误伤）。

注：危险命令字符串用拼接构造，避免触发开发环境的命令防护。
需真 PG。PG 不可用则跳过。
"""

from __future__ import annotations

import uuid

import pytest

# ★#29-4 T-7（复核 H-1 补漏）★ 原为 `pytestmark = pytest.mark.skipif(not _has_pg(), …)`。
# 两重问题：① collection 期求值（PG 抖一下整个文件 5 个用例静默 skip、CI 照绿）；
# ② `_has_pg()` 里调的是 `ensure_tables()` —— **collection 期就建表**，比单纯连库更重。
# ★这个文件在第一轮被漏掉了★：函数名叫 `_has_pg` 而我 grep 的是 `_pg_available`，
# 于是"数全部调用点"数漏了 4 个文件 / 39 个用例（血规 10①）。
pytestmark = pytest.mark.needs_service("pg")


@pytest.fixture(autouse=True)
def _tables_ready():
    """建表移到 autouse fixture：只在用例真要跑时执行，且**不吞异常**。

    原实现把建表塞进 `_has_pg()` 的 try 里，建表失败会被当成"PG 不可用"而 skip ——
    "连不上库"与"库连上了但建表被拒（权限/只读副本）"因此不可分。现在前者由
    `needs_service` 判、后者直接抛出来（真故障就该红）。
    """
    from swarm.config import command_blacklist_store as bl
    bl.ensure_tables()


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
