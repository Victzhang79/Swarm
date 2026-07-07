"""P2-14 D53（本文件先落 D53；D52 tar 批量同步测试同文件追加）。

D53：L1 确定性闸门 / git diff 产出解析卸线程——阻塞型闸门执行期间事件循环保持存活，
且闸门运行在非事件循环线程。ls-files 探测补 timeout。
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import AsyncMock, patch

from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality
from swarm.worker.executor import WorkerExecutor


def _mk_executor(tmp_path):
    st = SubTask(
        id="st-d53",
        description="改 A.java",
        difficulty=SubTaskDifficulty.TRIVIAL,
        modality=SubTaskModality.TEXT,
        scope=FileScope(writable=["A.java"]),
        intent="modify",
    )
    return WorkerExecutor(subtask=st, project_path=str(tmp_path))


def test_d53_trivial_gate_runs_off_loop_and_loop_stays_alive(tmp_path):
    """trivial 路径的确定性闸门（可长跑 build）执行于线程池且不冻结事件循环。

    改前 `det_ok, det_details = self._deterministic_l1_gate()` 直跑在 loop 上：
    闸门 sleep 0.3s 期间心跳协程完全停摆（ticks 个位数），且执行线程==loop 线程。
    """
    ex = _mk_executor(tmp_path)
    gate_info: dict = {}

    def blocking_gate():
        gate_info["thread"] = threading.get_ident()
        time.sleep(0.3)  # 模拟同步 build/git（真实场景可达 900s）
        return True, {"l1_decision_source": "deterministic_gate"}

    ticks = {"n": 0}

    async def heartbeat(stop):
        while not stop.is_set():
            ticks["n"] += 1
            await asyncio.sleep(0.01)

    async def main():
        loop_thread = threading.get_ident()
        stop = asyncio.Event()
        hb = asyncio.create_task(heartbeat(stop))
        with patch.object(ex, "_run_agent", new=AsyncMock(return_value="SUMMARY: done")), \
             patch.object(ex, "_sync_from_sandbox", new=AsyncMock(return_value=None)), \
             patch.object(ex, "_deterministic_l1_gate", side_effect=blocking_gate):
            out = await ex._run_trivial_fast()
        stop.set()
        await hb
        return out, loop_thread

    out, loop_thread = asyncio.run(main())
    assert out.l1_passed is True                        # 行为不变：闸门结论照常生效
    assert gate_info["thread"] != loop_thread           # 闸门跑在线程池，不在事件循环线程
    assert ticks["n"] >= 15, ticks["n"]                 # 0.3s 阻塞期间心跳持续（loop 未冻结）


def test_d53_ls_files_probe_has_timeout(tmp_path, monkeypatch):
    """_try_local_git_diff 的 ls-files 探测带 timeout（原无超时，git 挂死占死线程）。"""
    import subprocess as _sp

    # 造一个真实 git 仓库 + 一个 scope 文件，让路径走到 ls-files 探测
    _sp.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "A.java").write_text("class A {}\n")

    ex = _mk_executor(tmp_path)
    seen: dict = {}
    real_run = _sp.run

    def spy_run(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and "ls-files" in cmd and "--error-unmatch" in cmd:
            seen["timeout"] = kw.get("timeout")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr("subprocess.run", spy_run)
    ex._try_local_git_diff()
    assert seen.get("timeout"), "ls-files 探测必须带 timeout"


# ─── D52：tar 批量上传（O(N) 往返 → O(1)）────────────────


class _FakeFiles:
    def __init__(self):
        self.written: dict[str, bytes] = {}

    def write(self, path, data):
        self.written[path] = data if isinstance(data, bytes) else data.encode()

    def make_dir(self, path):
        pass


class _FakeSandbox:
    sandbox_id = "sb-d52"

    def __init__(self):
        self.files = _FakeFiles()


def _mk_manager():
    from swarm.config.settings import SandboxConfig
    from swarm.worker.sandbox import SandboxManager

    return SandboxManager(SandboxConfig(api_url="http://x", default_template="tpl"))


def _local_tree(tmp_path, names=("a.txt", "sub/b.txt", "c.txt", "sub/deep/d.txt")):
    for n in names:
        p = tmp_path / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"content-of-{n}\n")
    return list(names)


def test_d52_targeted_sync_uses_single_tar_upload(tmp_path, monkeypatch):
    """≥阈值文件数时：一次 tar 上传 + 一次解包校验命令，不再逐文件 files.write。"""
    import io
    import tarfile

    from swarm.worker.sandbox import CodeResult

    monkeypatch.delenv("SWARM_SANDBOX_TAR_SYNC", raising=False)
    mgr = _mk_manager()
    sb = _FakeSandbox()
    rels = _local_tree(tmp_path)
    cmds: list[str] = []

    def fake_run_command(sandbox, command, timeout=120, **kw):
        cmds.append(command)
        return CodeResult(stdout="", success=True)

    monkeypatch.setattr(mgr, "run_command", fake_run_command)
    stats = mgr.sync_files_to_sandbox(sb, tmp_path, rels, remote_root="/workspace")

    assert stats["uploaded"] == 4 and not stats["errors"]
    assert sorted(stats["files"]) == sorted(rels)
    # 唯一一次 files.write = tar 包本身（不再每文件一次）
    tar_paths = [p for p in sb.files.written if p.endswith(".tar.gz")]
    assert len(sb.files.written) == 1 and len(tar_paths) == 1
    # tar 内容完整且路径正确
    with tarfile.open(fileobj=io.BytesIO(sb.files.written[tar_paths[0]]), mode="r:gz") as tf:
        members = {m.name for m in tf.getmembers()}
        assert members == set(rels)
        f = tf.extractfile("sub/b.txt")
        assert f is not None and b"content-of-sub/b.txt" in f.read()
    # 解包 + 清单校验命令下发
    assert any("tar -xzf" in c and "MISSING:" in c for c in cmds)


def test_d52_verification_missing_falls_back_to_per_file(tmp_path, monkeypatch):
    """解包校验发现缺文件 → fail-closed 回退逐文件路径，最终仍全部上传。"""
    from swarm.worker.sandbox import CodeResult

    monkeypatch.delenv("SWARM_SANDBOX_TAR_SYNC", raising=False)
    mgr = _mk_manager()
    sb = _FakeSandbox()
    rels = _local_tree(tmp_path)

    def fake_run_command(sandbox, command, timeout=120, **kw):
        if "tar -xzf" in command:
            return CodeResult(stdout="MISSING:a.txt", success=True)
        return CodeResult(stdout="", success=True)

    monkeypatch.setattr(mgr, "run_command", fake_run_command)
    stats = mgr.sync_files_to_sandbox(sb, tmp_path, rels, remote_root="/workspace")

    assert stats["uploaded"] == 4 and not stats["errors"]
    # 逐文件路径：4 个业务文件各一次 files.write（外加 1 次失败的 tar 包上传）
    per_file = [p for p in sb.files.written if not p.endswith(".tar.gz")]
    assert len(per_file) == 4


def test_d52_kill_switch_and_small_batches_use_per_file(tmp_path, monkeypatch):
    from swarm.worker.sandbox import CodeResult

    mgr = _mk_manager()
    called = {"n": 0}

    def fake_run_command(sandbox, command, timeout=120, **kw):
        called["n"] += 1
        return CodeResult(stdout="", success=True)

    monkeypatch.setattr(mgr, "run_command", fake_run_command)

    # 杀开关关闭 → 不走 tar
    monkeypatch.setenv("SWARM_SANDBOX_TAR_SYNC", "false")
    sb = _FakeSandbox()
    rels = _local_tree(tmp_path)
    stats = mgr.sync_files_to_sandbox(sb, tmp_path, rels, remote_root="/workspace")
    assert stats["uploaded"] == 4
    assert not any(p.endswith(".tar.gz") for p in sb.files.written)

    # 低于阈值（2 个文件）→ 逐文件（tar 固定开销不划算）
    monkeypatch.delenv("SWARM_SANDBOX_TAR_SYNC", raising=False)
    sb2 = _FakeSandbox()
    stats2 = mgr.sync_files_to_sandbox(sb2, tmp_path, rels[:2], remote_root="/workspace")
    assert stats2["uploaded"] == 2
    assert not any(p.endswith(".tar.gz") for p in sb2.files.written)


def test_d52_validation_errors_preserved_with_tar_path(tmp_path, monkeypatch):
    """越界/缺失文件的记账口径与旧逐文件路径一致（tar 只吃合法条目）。"""
    from swarm.worker.sandbox import CodeResult

    monkeypatch.delenv("SWARM_SANDBOX_TAR_SYNC", raising=False)
    mgr = _mk_manager()
    sb = _FakeSandbox()
    rels = _local_tree(tmp_path)
    monkeypatch.setattr(mgr, "run_command",
                        lambda sandbox, command, timeout=120, **kw: CodeResult(stdout=""))
    stats = mgr.sync_files_to_sandbox(
        sb, tmp_path, rels + ["missing.txt", "../escape.txt"], remote_root="/workspace")
    assert stats["uploaded"] == 4
    assert any("missing.txt" in e for e in stats["errors"])
    assert any("越界" in e for e in stats["errors"])


def test_d52_shared_ssl_context_reused():
    from swarm.worker.sandbox import _shared_ssl_context

    assert _shared_ssl_context(True) is _shared_ssl_context(True)
    assert _shared_ssl_context(False) is _shared_ssl_context(False)
    assert _shared_ssl_context(True) is not _shared_ssl_context(False)
    assert _shared_ssl_context(False).check_hostname is False
