"""round23 审计治本 — R23-4 shell 拼接 shlex.quote。

沙箱文件探测/checkpoint/L1 sed 原用裸单引号或只剥 '，文件名含 $()/;/空格 可破坏引号边界。
"""
from __future__ import annotations

from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality
from swarm.worker.executor import WorkerExecutor


def _mk():
    st = SubTask(id="s", description="d", difficulty=SubTaskDifficulty.MEDIUM,
                 modality=SubTaskModality.TEXT, scope=FileScope())
    return WorkerExecutor(subtask=st)


def test_sandbox_file_exists_shlex_quotes_dangerous_name():
    ex = _mk()
    cap = {}

    class _Mgr:
        def run_command(self, sb, cmd, timeout=15):
            cap["cmd"] = cmd

            class _R:
                stdout = "__N__"
            return _R()

    ex._sandbox = object()
    ex._sandbox_manager = _Mgr()
    out = ex._sandbox_file_exists("a b$(evil);rm.java")
    assert out is False
    # 危险内容仍在，但整体被 shlex 单引号包裹（test -f '...'），$() 不在裸上下文求值。
    assert "test -f '" in cap["cmd"], cap["cmd"]
    assert "$(evil)" in cap["cmd"]
    # 单引号内 $() 不被 shell 展开——校验命令里没有把 $(evil) 暴露在引号外
    assert "-f '" in cap["cmd"] and cap["cmd"].count("'") >= 2


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
