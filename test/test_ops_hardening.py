#!/usr/bin/env python3
"""P2-C（KB 清理接线）+ P2-G（备份 runbook）+ F1（沙箱 create 请求超时）装配/存在守卫。

均为源码级/文件级守卫，无需 DB/沙箱，CI 安全。
"""

from __future__ import annotations

import importlib
import inspect
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


# ── P2-C：prune_old_logs 已接入每日 leader 调度 ──────────────


def test_kb_prune_scheduler_wired_into_leader_startup():
    app_mod = importlib.import_module("swarm.api.app")
    # leader 启动链调用 _start_kb_prune_scheduler
    leader_src = inspect.getsource(app_mod._run_schedulers_with_leadership)
    assert "_start_kb_prune_scheduler" in leader_src, "KB 清理调度未接入 leader 启动链（P2-C 回归）"
    # 每日循环经 wait_for 调 _run_kb_prune_once（防单轮挂死），后者才真正清理
    loop_src = inspect.getsource(app_mod._kb_prune_daily_loop)
    assert "_run_kb_prune_once" in loop_src, "每日循环未调用 _run_kb_prune_once"
    once_src = inspect.getsource(app_mod._run_kb_prune_once)
    assert "prune_old_logs" in once_src, "单轮清理未调用 prune_old_logs"
    assert "list_projects" in once_src, "单轮清理未枚举全库项目"


def test_prune_old_logs_still_exists():
    from swarm.knowledge.behavior_store import BehaviorStore

    assert hasattr(BehaviorStore, "prune_old_logs")


# ── F1：沙箱 create 注入 request_timeout（绑住 HTTP 挂起）──────


def test_sandbox_create_passes_request_timeout():
    from swarm.worker import sandbox as sb

    src = inspect.getsource(sb)
    assert "request_timeout" in src, "沙箱 create 未注入 request_timeout（F1 回归）"
    assert "SWARM_SANDBOX_REQUEST_TIMEOUT" in src, "request_timeout 应可经 env 调整"


# ── P2-G：备份脚本 + runbook 存在且脚本语法合法 ──────────────


def test_backup_script_and_runbook_exist():
    assert (_ROOT / "scripts" / "backup.sh").is_file()
    assert (_ROOT / "scripts" / "BACKUP_RESTORE.md").is_file()


def test_backup_script_is_valid_bash():
    r = subprocess.run(
        ["bash", "-n", str(_ROOT / "scripts" / "backup.sh")],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"backup.sh 语法错误: {r.stderr}"


def test_backup_script_covers_the_three_assets():
    txt = (_ROOT / "scripts" / "backup.sh").read_text(encoding="utf-8")
    assert "pg_dump" in txt          # PostgreSQL
    assert "snapshots" in txt        # Qdrant snapshot
    assert "SWARM_SECRET_KEY" in txt  # 根密钥托管提示


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
