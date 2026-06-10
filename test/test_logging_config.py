#!/usr/bin/env python3
"""swarm/logging_config.py 单测：task 上下文绑定 + setup 幂等 + JSON 格式。"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.logging_config import (
    _ContextFilter,
    _JsonFormatter,
    bind_task,
    clear_task_context,
    current_task_id,
    set_task_context,
    setup_logging,
)


def test_bind_task_scoped():
    clear_task_context()
    assert current_task_id() == ""
    with bind_task("task-123", "st-1"):
        assert current_task_id() == "task-123"
    # 退出作用域自动还原
    assert current_task_id() == ""
    print("  ✅ bind_task 作用域内绑定、退出还原")


def test_bind_task_nested():
    with bind_task("outer"):
        assert current_task_id() == "outer"
        with bind_task("inner"):
            assert current_task_id() == "inner"
        # 内层退出恢复外层
        assert current_task_id() == "outer"
    assert current_task_id() == ""
    print("  ✅ bind_task 嵌套正确还原")


def test_set_clear_task_context():
    set_task_context("manual-task")
    assert current_task_id() == "manual-task"
    clear_task_context()
    assert current_task_id() == ""
    print("  ✅ set/clear_task_context")


def test_context_filter_injects_suffix():
    clear_task_context()
    f = _ContextFilter()
    rec = logging.LogRecord("swarm.x", logging.INFO, "f.py", 1, "msg", None, None)
    # 无 task
    f.filter(rec)
    assert rec.task_suffix == ""
    # 有 task + subtask
    with bind_task("abcdef123456", "st-9"):
        rec2 = logging.LogRecord("swarm.x", logging.INFO, "f.py", 1, "msg", None, None)
        f.filter(rec2)
        assert "abcdef12" in rec2.task_suffix
        assert "st-9" in rec2.task_suffix
    print("  ✅ _ContextFilter 注入 task 后缀")


def test_json_formatter_includes_context():
    import json

    f = _ContextFilter()
    fmt = _JsonFormatter()
    with bind_task("task-json", "st-json"):
        rec = logging.LogRecord("swarm.x", logging.INFO, "f.py", 1, "hello", None, None)
        f.filter(rec)
        line = fmt.format(rec)
    data = json.loads(line)
    assert data["msg"] == "hello"
    assert data["level"] == "INFO"
    assert data["task_id"] == "task-json"
    assert data["subtask_id"] == "st-json"
    clear_task_context()
    print("  ✅ _JsonFormatter 输出含 task 上下文的合法 JSON")


def test_setup_logging_idempotent():
    setup_logging()
    sw = logging.getLogger("swarm")
    n1 = len(sw.handlers)
    setup_logging()  # 再次调用不应叠加 handler
    n2 = len(sw.handlers)
    assert n1 == n2
    print("  ✅ setup_logging 幂等（不重复挂 handler）")


def test_setup_logging_has_rotating_handler():
    import logging.handlers

    setup_logging(force=True)
    sw = logging.getLogger("swarm")
    has_rot = any(
        isinstance(h, logging.handlers.RotatingFileHandler) for h in sw.handlers
    )
    assert has_rot, "应配置 RotatingFileHandler（修复无限增长）"
    print("  ✅ setup_logging 含轮转文件 handler")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nlogging_config 单测通过。")
