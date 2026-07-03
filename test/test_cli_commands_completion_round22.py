#!/usr/bin/env python3
"""round22 CLI 补全：新命令注册 + 可调用 smoke（project/preprocess/task list/user/member/kb retrieve）。

用户反馈"cli 命令很少"+ "CLI 与 RBAC 脱节"。本批补全项目生命周期/预处理/任务列表/用户成员(RBAC)/KB 检索，
全部走 HTTP + 统一注入 token。测试锁定：命令齐备 + --help 可调用 + 请求带 Authorization。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm import cli as _cli  # noqa: E402
from swarm.cli import main  # noqa: E402


def test_new_command_groups_registered():
    expect = {
        "project": {"list", "create", "show", "delete", "stats"},
        "preprocess": {"run", "status"},
        "user": {"list"},
        "member": {"list", "add", "remove"},
    }
    for grp, subs in expect.items():
        assert grp in main.commands, f"缺命令组 {grp}"
        got = set(main.commands[grp].commands.keys())
        assert subs <= got, f"{grp} 缺子命令 {subs - got}"
    # task +list、kb +retrieve
    assert "list" in main.commands["task"].commands
    assert "retrieve" in main.commands["kb"].commands
    print("  ✅ 新命令组/子命令齐备（含 RBAC member 组）")


def test_all_new_commands_help_invokable():
    runner = CliRunner()
    for path in [["project", "list"], ["project", "create"], ["preprocess", "run"],
                 ["task", "list"], ["user", "list"], ["member", "add"], ["kb", "retrieve"]]:
        res = runner.invoke(main, path + ["--help"])
        assert res.exit_code == 0, f"{' '.join(path)} --help 失败: {res.output}"
    print("  ✅ 7 个新命令 --help 均可调用")


def test_project_list_injects_auth(monkeypatch):
    monkeypatch.setenv("SWARM_TOKEN", "tok-cli22")
    captured = {}

    def fake_get(url, **kw):
        captured["headers"] = kw.get("headers")
        resp = MagicMock(); resp.status_code = 200
        resp.json.return_value = {"projects": []}
        return resp

    with patch.object(_cli.httpx, "get", side_effect=fake_get):
        res = CliRunner().invoke(main, ["project", "list"])
    assert res.exit_code == 0
    assert captured["headers"].get("Authorization") == "Bearer tok-cli22", \
        "project list 必须带 token（RBAC 开时不 401）"
    print("  ✅ project list 注入 Authorization")


def test_member_add_uses_put_with_auth(monkeypatch):
    monkeypatch.setenv("SWARM_TOKEN", "tok-cli22")
    captured = {}

    def fake_put(url, **kw):
        captured["url"] = url; captured["json"] = kw.get("json")
        captured["headers"] = kw.get("headers")
        resp = MagicMock(); resp.status_code = 200
        return resp

    with patch.object(_cli.httpx, "put", side_effect=fake_put):
        res = CliRunner().invoke(main, ["member", "add", "-p", "proj1", "-u", "u1", "-r", "developer"])
    assert res.exit_code == 0
    assert "/api/projects/proj1/members" in captured["url"]
    assert captured["json"] == {"user_id": "u1", "role": "developer"}
    assert captured["headers"].get("Authorization") == "Bearer tok-cli22"
    print("  ✅ member add → PUT members + 正确 payload + token")


if __name__ == "__main__":
    test_new_command_groups_registered()
    test_all_new_commands_help_invokable()
    print("\n✅ CLI 补全 smoke 通过（完整断言见 pytest monkeypatch 用例）")
