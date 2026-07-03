#!/usr/bin/env python3
"""#5(a) round22：审计/里程碑跨项目读越权治本（store 层 project_ids scope 机制）。

根因：审计端点无 project_id 时只 _require_user 不做 scope 过滤；list_task_audit project_id=None
→ 无 WHERE 返回全部项目；里程碑 GET 同理。治本：store 支持 project_ids 成员 scope，空列表 fail-closed。

端点授权逻辑（admin 全量/非 admin 限成员/task_id 反查归属/limit 封顶）在 task.py+app.py，
本测试锁定 store 层 project_ids 过滤的 fail-closed 契约（越权防护的机制底座）。
"""
from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.project import store  # noqa: E402


def test_list_task_audit_empty_project_ids_failclosed():
    # 非 admin 无任何成员项目 → project_ids=[] → 绝不返回全库（fail-closed 空）
    rows = store.list_task_audit(project_ids=[])
    assert rows == [], "空成员项目集必须 fail-closed 返回空（不泄露全库审计）"
    print("  ✅ list_task_audit project_ids=[] → fail-closed 空")


def test_milestone_empty_project_ids_failclosed():
    rows = store.get_latest_milestone_reports(project_ids=[])
    assert rows == [], "空成员项目集必须 fail-closed 返回空（不泄露全库里程碑）"
    print("  ✅ get_latest_milestone_reports project_ids=[] → fail-closed 空")


def test_signatures_have_project_ids():
    # 契约：两个 store 查询都新增了 project_ids 成员 scope 参数
    assert "project_ids" in inspect.signature(store.list_task_audit).parameters
    assert "project_ids" in inspect.signature(store.get_latest_milestone_reports).parameters
    print("  ✅ store 查询暴露 project_ids scope 参数")


if __name__ == "__main__":
    test_list_task_audit_empty_project_ids_failclosed()
    test_milestone_empty_project_ids_failclosed()
    test_signatures_have_project_ids()
    print("\n✅ #5(a) 审计/里程碑 scope 机制全部通过")
