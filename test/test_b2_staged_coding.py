"""task 82104bf1 B2 回归：多文件子任务分阶段编码逻辑。

≤3 文件 → 单次 loop（原行为）；>3 → 按文件分批（每批≤2），各批独立步数预算 + checkpoint。
这里测纯逻辑（分批切分、prompt 聚焦），不起真实沙箱/agent。
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.types import FileScope, SubTask, SubTaskDifficulty
from swarm.worker.executor import WorkerExecutor


def _mk_worker(writable):
    st = SubTask(id="st-1", description="多文件功能", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=writable))
    return WorkerExecutor(st, task_id="t1")


@pytest.mark.asyncio
async def test_le3_files_single_loop():
    """≤3 文件走单次 loop（不分批）。"""
    w = _mk_worker(["a.java", "b.java", "c.java"])
    calls = []
    async def fake_run_agent(msg, step="react"):
        calls.append(step)
        return "done"
    w._run_agent = fake_run_agent
    await w._run_coding_phase("locate")
    assert calls == ["code"], f"≤3 文件应单次 code，got {calls}"


@pytest.mark.asyncio
async def test_gt3_files_batched():
    """>3 文件分批：5 文件 → 3 批（2+2+1），每批独立 loop + checkpoint。"""
    w = _mk_worker(["a.java", "b.java", "c.java", "d.java", "e.java"])
    calls = []
    async def fake_run_agent(msg, step="react"):
        calls.append(step)
        return "batch done"
    w._run_agent = fake_run_agent
    w._sandbox_checkpoint = AsyncMock()
    w._check_timeout = MagicMock(return_value=False)
    await w._run_coding_phase("locate")
    # 5 文件 / batch2 = 3 批
    assert calls == ["code-batch-1", "code-batch-2", "code-batch-3"], calls
    assert w._sandbox_checkpoint.await_count == 3


@pytest.mark.asyncio
async def test_batch_prompt_focuses_only_batch_files():
    """分批 prompt 只列本批文件 + 标注已完成的。"""
    w = _mk_worker(["a.java", "b.java", "c.java", "d.java"])
    p = w._build_batch_code_prompt("loc", ["c.java", "d.java"], ["a.java", "b.java"], 2, 2)
    assert "c.java" in p and "d.java" in p
    assert "已完成" in p and "a.java" in p  # 已完成提示
    assert "只" in p  # 聚焦约束


@pytest.mark.asyncio
async def test_timeout_stops_remaining_batches():
    """时间预算用尽时停止后续批次（已写保留）。"""
    w = _mk_worker(["a.java", "b.java", "c.java", "d.java", "e.java", "f.java"])
    calls = []
    async def fake_run_agent(msg, step="react"):
        calls.append(step)
        return "done"
    w._run_agent = fake_run_agent
    w._sandbox_checkpoint = AsyncMock()
    # 第一批后就超时
    w._check_timeout = MagicMock(side_effect=[True])
    await w._run_coding_phase("locate")
    assert len(calls) == 1, f"超时应在第一批后停止，got {calls}"
