"""Batch 2-C：崩溃重启后的沙箱 owner 必须可安全接续。"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from swarm.api.app import _partition_sweep_targets


def _instance_id_from_fresh_process(state_dir) -> str:
    env = os.environ.copy()
    env.pop("SWARM_INSTANCE_ID", None)
    env["SWARM_INSTANCE_STATE_DIR"] = str(state_dir)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from swarm.worker.sandbox import get_instance_id; print(get_instance_id())",
        ],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    return result.stdout.strip().splitlines()[-1]


def _instance_process(*, state_dir, explicit_owner: str | None = None):
    env = os.environ.copy()
    if explicit_owner is None:
        env.pop("SWARM_INSTANCE_ID", None)
    else:
        env["SWARM_INSTANCE_ID"] = explicit_owner
    env["SWARM_INSTANCE_STATE_DIR"] = str(state_dir)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from swarm.worker.sandbox import get_instance_id; print(get_instance_id())",
        ],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )


def _holding_instance(state_dir):
    env = os.environ.copy()
    env.pop("SWARM_INSTANCE_ID", None)
    env["SWARM_INSTANCE_STATE_DIR"] = str(state_dir)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from swarm.worker.sandbox import get_instance_id; "
                "print(get_instance_id(), flush=True); sys.stdin.readline()"
            ),
        ],
        cwd=os.getcwd(),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    return process, process.stdout.readline().strip()


def test_crashed_process_owner_is_reclaimed_and_its_sandbox_becomes_sweepable(tmp_path):
    """前一进程退出后，新进程应接续其 owner，使崩溃遗留进入启动清扫集。"""
    state_dir = tmp_path / "owner-leases"
    crashed, previous_owner = _holding_instance(state_dir)
    try:
        crashed.kill()
        crashed.wait(timeout=10)
        restarted_owner = _instance_id_from_fresh_process(state_dir)
    finally:
        if crashed.poll() is None:
            crashed.terminate()
            crashed.wait(timeout=10)

    to_kill, kept_other, kept_untagged = _partition_sweep_targets(
        [{"id": "crash-leftover", "metadata": {"swarm_instance": previous_owner}}],
        restarted_owner,
        sweep_untagged=False,
    )

    assert restarted_owner == previous_owner
    assert to_kill == ["crash-leftover"]
    assert kept_other == 0
    assert kept_untagged == 0


def test_reclaiming_dead_owner_never_claims_live_replica_owner(tmp_path):
    """同机多副本各持不同槽；只回收已退出 owner，仍持 lease 的副本必须保留。"""
    state_dir = tmp_path / "owner-leases"
    first, first_owner = _holding_instance(state_dir)
    second, second_owner = _holding_instance(state_dir)
    try:
        assert first_owner != second_owner
        assert first.stdin is not None
        first.stdin.write("\n")
        first.stdin.flush()
        first.wait(timeout=10)

        restarted_owner = _instance_id_from_fresh_process(state_dir)
        to_kill, kept_other, _ = _partition_sweep_targets(
            [
                {"id": "dead-owner-box", "metadata": {"swarm_instance": first_owner}},
                {"id": "live-owner-box", "metadata": {"swarm_instance": second_owner}},
            ],
            restarted_owner,
            sweep_untagged=False,
        )

        assert restarted_owner == first_owner
        assert to_kill == ["dead-owner-box"]
        assert kept_other == 1
    finally:
        for process in (first, second):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


def test_single_restart_temporarily_claims_all_dead_owner_slots(tmp_path):
    """多副本全灭后缩容为单副本，也要安全接管所有无锁 owner 供本轮清扫。"""
    state_dir = tmp_path / "owner-leases"
    state_dir.mkdir()
    owners = ["swarm-000000000001", "swarm-000000000002"]
    for index, owner in enumerate(owners):
        (state_dir / f"owner-{index}.lease").write_text(owner)

    env = os.environ.copy()
    env.pop("SWARM_INSTANCE_ID", None)
    env["SWARM_INSTANCE_STATE_DIR"] = str(state_dir)
    script = """
from swarm.worker.sandbox import get_instance_id, reclaimable_instance_ids

current = get_instance_id()
with reclaimable_instance_ids(current) as claimed:
    print(current)
    print(",".join(sorted(claimed)))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    current, claimed = result.stdout.strip().splitlines()
    assert current == owners[0]
    assert claimed.split(",") == owners


def test_bad_slot_after_dead_slot_cannot_release_previously_claimed_lease(tmp_path):
    """扫描后续坏槽不得误关上一轮 fd；reclaim scope 内另一进程不能抢走 dead owner。"""
    state_dir = tmp_path / "owner-leases"
    state_dir.mkdir()
    owners = ["swarm-000000000011", "swarm-000000000012"]
    for index, owner in enumerate(owners):
        (state_dir / f"owner-{index}.lease").write_text(owner)
    victim = tmp_path / "victim"
    victim.write_text("safe")
    (state_dir / "owner-2.lease").symlink_to(victim)

    env = os.environ.copy()
    env.pop("SWARM_INSTANCE_ID", None)
    env["SWARM_INSTANCE_STATE_DIR"] = str(state_dir)
    script = """
import os
import subprocess
import sys
from swarm.worker.sandbox import get_instance_id, reclaimable_instance_ids

current = get_instance_id()
with reclaimable_instance_ids(current) as claimed:
    child = subprocess.run(
        [sys.executable, "-c", "from swarm.worker.sandbox import get_instance_id; print(get_instance_id())"],
        env=os.environ.copy(), capture_output=True, text=True, check=True, timeout=20,
    )
    print(current)
    print(",".join(sorted(claimed)))
    print(child.stdout.strip())
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    current, claimed, contender = result.stdout.strip().splitlines()
    assert current == owners[0]
    assert claimed.split(",") == owners
    assert contender not in owners
    assert victim.read_text() == "safe"


def test_explicit_instance_id_remains_backward_compatible_even_without_state_dir(tmp_path):
    """编排器显式注入的旧契约优先，且不依赖本地 lease 目录可用。"""
    unusable_state_dir = tmp_path / "not-a-directory"
    unusable_state_dir.write_text("occupied")

    result = _instance_process(
        state_dir=unusable_state_dir,
        explicit_owner="orchestrator-owned-replica",
    )

    assert result.stdout.strip() == "orchestrator-owned-replica"
    assert "lease 不可用" not in result.stderr


def test_existing_short_owner_lease_remains_compatible(tmp_path):
    """已落盘的早期 12 位 owner 不因扩大新 ID 熵而失去接续能力。"""
    state_dir = tmp_path / "owner-leases"
    state_dir.mkdir()
    (state_dir / "owner-0.lease").write_text("swarm-0123456789ab")

    result = _instance_process(state_dir=state_dir)

    assert result.stdout.strip() == "swarm-0123456789ab"


def test_unavailable_lease_state_fails_safe_without_blocking_startup(tmp_path):
    """无法证明 owner 接续时应降级新 ID，不能猜测并误清其他实例。"""
    unusable_state_dir = tmp_path / "not-a-directory"
    unusable_state_dir.write_text("occupied")

    env = os.environ.copy()
    env.pop("SWARM_INSTANCE_ID", None)
    env["SWARM_INSTANCE_STATE_DIR"] = str(unusable_state_dir)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from swarm.worker.sandbox import get_instance_id; "
                "from swarm.infra.degrade import degrade_counts; "
                "print(get_instance_id()); "
                "print(degrade_counts().get('worker.sandbox.owner_lease_unavailable', 0))"
            ),
        ],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )

    owner, degrade_count = result.stdout.strip().splitlines()
    assert re.fullmatch(r"swarm-[0-9a-f]{32}", owner)
    assert degrade_count == "1"
    assert "sandbox owner lease 不可用" in result.stderr


def test_bad_lease_slot_is_skipped_and_next_slot_stays_reclaimable(tmp_path):
    """单槽损坏只跳过该槽；下一槽仍须稳定接管，不能整批降级随机 owner。"""
    state_dir = tmp_path / "owner-leases"
    state_dir.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("do-not-touch")
    (state_dir / "owner-0.lease").symlink_to(victim)

    first = _instance_process(state_dir=state_dir)
    second = _instance_process(state_dir=state_dir)

    assert first.stdout.strip() == second.stdout.strip()
    assert re.fullmatch(r"swarm-[0-9a-f]{32}", first.stdout.strip())
    assert (state_dir / "owner-1.lease").read_text() == first.stdout.strip()
    assert "降级随机实例 ID" not in first.stderr + second.stderr
    assert victim.read_text() == "do-not-touch"


def test_non_regular_lease_slot_is_skipped(tmp_path):
    """目录等非普通 slot 只影响自身，不能阻断后续有效槽。"""
    state_dir = tmp_path / "owner-leases"
    (state_dir / "owner-0.lease").mkdir(parents=True)

    first = _instance_process(state_dir=state_dir)
    second = _instance_process(state_dir=state_dir)

    assert first.stdout.strip() == second.stdout.strip()
    assert (state_dir / "owner-1.lease").read_text() == first.stdout.strip()
    assert "降级随机实例 ID" not in first.stderr + second.stderr


def test_permission_error_on_one_lease_slot_is_skipped(tmp_path):
    """单槽权限错误与 symlink/非普通文件同档：跳过后仍使用稳定下一槽。"""
    state_dir = tmp_path / "owner-leases"
    env = os.environ.copy()
    env.pop("SWARM_INSTANCE_ID", None)
    env["SWARM_INSTANCE_STATE_DIR"] = str(state_dir)
    script = """
import os
import swarm.worker.sandbox as sandbox

real_open = os.open
def guarded_open(path, *args, **kwargs):
    if str(path).endswith("owner-0.lease"):
        raise PermissionError(13, "permission denied", str(path))
    return real_open(path, *args, **kwargs)
sandbox.os.open = guarded_open
print(sandbox.get_instance_id())
"""

    results = [
        subprocess.run(
            [sys.executable, "-c", script],
            cwd=os.getcwd(),
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        for _ in range(2)
    ]

    assert results[0].stdout.strip() == results[1].stdout.strip()
    assert (state_dir / "owner-1.lease").read_text() == results[0].stdout.strip()
    assert all("降级随机实例 ID" not in result.stderr for result in results)


def test_concurrent_first_calls_share_one_process_owner(tmp_path):
    """首次创建并发发生时，本进程所有调用方仍必须观察到同一个 owner。"""
    env = os.environ.copy()
    env.pop("SWARM_INSTANCE_ID", None)
    env["SWARM_INSTANCE_STATE_DIR"] = str(tmp_path / "owner-leases")
    script = """
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from swarm.worker.sandbox import get_instance_id

barrier = Barrier(32)
def read_owner(_):
    barrier.wait()
    return get_instance_id()

with ThreadPoolExecutor(max_workers=32) as pool:
    print(len(set(pool.map(read_owner, range(32)))))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )

    assert result.stdout.strip() == "1"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="需要 POSIX fork")
def test_fork_child_drops_inherited_owner_and_claims_distinct_live_slot(tmp_path):
    """父进程先初始化再 fork 时，子进程不得继承同 owner 或同一 flock 所有权。"""
    env = os.environ.copy()
    env.pop("SWARM_INSTANCE_ID", None)
    env["SWARM_INSTANCE_STATE_DIR"] = str(tmp_path / "owner-leases")
    script = """
import os
from swarm.worker.sandbox import get_instance_id

parent_owner = get_instance_id()
read_fd, write_fd = os.pipe()
pid = os.fork()
if pid == 0:
    os.close(read_fd)
    os.write(write_fd, get_instance_id().encode("ascii"))
    os.close(write_fd)
    os._exit(0)
os.close(write_fd)
child_owner = os.read(read_fd, 256).decode("ascii")
os.close(read_fd)
os.waitpid(pid, 0)
print(parent_owner)
print(child_owner)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )

    parent_owner, child_owner = result.stdout.strip().splitlines()
    assert parent_owner != child_owner


def test_compose_persists_default_owner_lease_state_for_container_restarts():
    """仓内 compose 的默认服务必须把 owner lease 放到跨容器重建保留的命名卷。"""
    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    service = compose["services"]["swarm"]
    lease_path = "/home/swarm/.swarm/instance_leases"

    assert service["environment"]["SWARM_INSTANCE_STATE_DIR"] == lease_path
    assert f"swarm_instance_leases:{lease_path}" in service["volumes"]
    assert "swarm_instance_leases" in compose["volumes"]

    dockerfile = (root / "Dockerfile").read_text()
    provision = f"mkdir -p {lease_path}"
    assert provision in dockerfile
    assert dockerfile.index(provision) < dockerfile.index("USER swarm")
