"""逐任务日志文件：logs/<task_id>.log + <task_id>.sandbox.log。

用户诉求：每个任务的日志、以及该任务的沙箱日志分别建文件，统一放 logs/（不提交），便于回看。
"""
from __future__ import annotations

import logging

from swarm.logging_config import _ContextFilter, _PerTaskFileHandler, bind_task


def _make_logger(tmp_path):
    h = _PerTaskFileHandler(tmp_path)
    h.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    h.addFilter(_ContextFilter())
    lg = logging.getLogger("swarm.test.pertask")
    lg.handlers = [h]
    lg.setLevel(logging.INFO)
    lg.propagate = False
    return lg, h


def test_task_log_file_written(tmp_path):
    lg, h = _make_logger(tmp_path)
    with bind_task("task-abc"):
        lg.info("hello from task")
    h.close()
    f = tmp_path / "task-abc.log"
    assert f.exists(), "应为该任务建独立日志文件"
    assert "hello from task" in f.read_text(encoding="utf-8")


def test_sandbox_logs_split_into_sandbox_file(tmp_path):
    h = _PerTaskFileHandler(tmp_path)
    h.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    h.addFilter(_ContextFilter())
    sandbox_lg = logging.getLogger("swarm.worker.sandbox")
    sandbox_lg.handlers = [h]
    sandbox_lg.setLevel(logging.INFO)
    sandbox_lg.propagate = False
    with bind_task("task-xyz"):
        sandbox_lg.info("Targeted sync from sandbox abc")
    h.close()
    # 沙箱日志：既进主任务文件（全量），也进 .sandbox.log（隔离视图）
    assert (tmp_path / "task-xyz.log").exists()
    sbx = tmp_path / "task-xyz.sandbox.log"
    assert sbx.exists(), "沙箱日志应另建 .sandbox.log"
    assert "Targeted sync from sandbox" in sbx.read_text(encoding="utf-8")


def test_no_task_context_writes_nothing(tmp_path):
    lg, h = _make_logger(tmp_path)
    lg.info("no task bound")  # 无 bind_task
    h.close()
    assert list(tmp_path.iterdir()) == [], "无 task 上下文不应建任何逐任务文件"


def test_lru_caps_open_handles(tmp_path):
    h = _PerTaskFileHandler(tmp_path, max_open=4)
    h.setFormatter(logging.Formatter("%(message)s"))
    h.addFilter(_ContextFilter())
    lg = logging.getLogger("swarm.test.lru")
    lg.handlers = [h]
    lg.setLevel(logging.INFO)
    lg.propagate = False
    for i in range(10):
        with bind_task(f"task-{i}"):
            lg.info(f"line {i}")
    assert len(h._files) <= 4, "同时打开的句柄数应被 LRU 限制"
    h.close()
    # 所有任务文件都应已落盘（即便句柄被 LRU 关闭，内容已 flush）
    assert sum(1 for p in tmp_path.iterdir() if p.suffix == ".log") == 10


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
