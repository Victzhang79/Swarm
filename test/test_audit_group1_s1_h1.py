"""走查报告第1组：S1 沙箱上下文 ContextVar 并发隔离 + H1 _run_l2_local 工作树回滚。"""
import asyncio
import os
import subprocess
import tempfile


def test_s1_sandbox_context_isolated_across_tasks():
    """S1：两个并发 asyncio task 各设各的沙箱上下文，互不串味。"""
    from swarm.tools.build_tools import get_sandbox_context, set_sandbox_context

    seen = {}

    async def worker(name):
        set_sandbox_context(f"sbx-{name}", f"mgr-{name}")
        await asyncio.sleep(0.02)  # 让出，模拟并发交错
        sbx, mgr = get_sandbox_context()
        seen[name] = (sbx, mgr)

    async def run():
        await asyncio.gather(worker("A"), worker("B"))

    asyncio.run(run())
    # A 读到自己的 sbx-A，不被 B 的 set 污染
    assert seen["A"] == ("sbx-A", "mgr-A"), seen
    assert seen["B"] == ("sbx-B", "mgr-B"), seen


def test_s1_extra_whitelist_isolated():
    from swarm.tools.build_tools import get_extra_whitelist, set_extra_whitelist

    seen = {}

    async def worker(name, wl):
        set_extra_whitelist(wl)
        await asyncio.sleep(0.02)
        seen[name] = get_extra_whitelist()

    async def run():
        await asyncio.gather(worker("A", ["mvn test"]), worker("B", ["npm run build"]))

    asyncio.run(run())
    assert seen["A"] == ["mvn test"], seen
    assert seen["B"] == ["npm run build"], seen


def test_h1_l2_local_rolls_back_worktree():
    """H1：_run_l2_local apply+test 后还原工作树，不残留脏改动。"""
    from swarm.brain.nodes import _run_l2_local

    d = tempfile.mkdtemp()
    subprocess.run(["git", "-C", d, "init", "-q"], check=True)
    subprocess.run(["git", "-C", d, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", d, "config", "user.name", "t"], check=True)
    with open(f"{d}/foo.txt", "w") as f:
        f.write("original\n")
    subprocess.run(["git", "-C", d, "add", "."], check=True)
    subprocess.run(["git", "-C", d, "commit", "-qm", "init"], check=True)

    # 一个会修改 foo.txt 的 diff
    diff = ("--- a/foo.txt\n+++ b/foo.txt\n@@ -1 +1 @@\n-original\n+modified\n")
    # test_cmd 用 true(必过)，重点验证回滚
    _run_l2_local(d, diff, "true", timeout=30)

    # 验证：工作树已还原，foo.txt 仍是 original，git status 干净
    with open(f"{d}/foo.txt") as f:
        assert f.read() == "original\n", "L2 验证后工作树应还原"
    status = subprocess.run(["git", "-C", d, "status", "--short"],
                            capture_output=True, text=True).stdout
    assert status.strip() == "", f"工作树应干净，实际: {status}"
