"""A2 前置实测：摸排 CubeSandbox 真实隔离边界（网络/逃逸/资源）。

用项目 SDK 创建一个临时探测沙箱，在沙箱内跑探测命令，收集事实供 A2 设计定范围。
探测完立即 kill。只读探测，不做任何破坏性操作。
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from swarm.worker.sandbox import get_sandbox_manager


PROBES = {
    # 身份/权限
    "whoami": "whoami; id",
    "is_root": "[ \"$(id -u)\" = \"0\" ] && echo ROOT || echo NONROOT",
    # 容器迹象
    "container_hint": "cat /proc/1/cgroup 2>/dev/null | head -3; ls -la /.dockerenv 2>/dev/null || echo no-dockerenv",
    # 资源限额
    "cpu_count": "nproc",
    "mem_limit": "cat /sys/fs/cgroup/memory.max 2>/dev/null || cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null || echo unknown",
    "pids_limit": "cat /sys/fs/cgroup/pids.max 2>/dev/null || echo unknown",
    "ulimit_procs": "ulimit -u",
    # 文件系统可见性（能否看到宿主敏感文件）
    "host_etc": "ls /etc/hostname; cat /etc/hostname",
    "proc_host": "ls /proc/1/root 2>&1 | head -2 || echo blocked",
    # 网络：能否访问内网控制面 / PG / 公网
    "net_tools": "which curl wget nc python3 2>/dev/null | tr '\\n' ' '; echo",
    "dns": "getent hosts github.com 2>/dev/null | head -1 || echo no-dns",
    # 内网控制面（CubeSandbox 服务器自身 192.168.60.106）
    "net_control_plane": "timeout 5 bash -c 'curl -s -o /dev/null -w \"%{http_code}\" http://192.168.60.106:3000 2>/dev/null' || echo unreachable-or-no-curl",
    # 内网 PG（若沙箱能连到 swarm 的 PG 就是大问题）
    "net_internal_pg": "timeout 5 bash -c '</dev/tcp/192.168.60.106/5432 && echo PG-REACHABLE' 2>/dev/null || echo pg-blocked",
    # 公网
    "net_public": "timeout 6 bash -c 'curl -s -o /dev/null -w \"%{http_code}\" https://api.github.com 2>/dev/null' || echo public-blocked",
    # 工作目录
    "workdir": "pwd; ls -la /workspace 2>/dev/null | head -3; echo '---tmp---'; ls -la /tmp 2>/dev/null | head -5; echo '---home---'; echo HOME=$HOME; ls -la $HOME 2>/dev/null | head -5",
}


def main():
    mgr = get_sandbox_manager()
    print("=== 创建探测沙箱 ===")
    sb = mgr.create(source="a2_probe", task_id="_test_a2_probe")
    sid = sb.sandbox_id
    print(f"sandbox_id={sid}\n")
    try:
        for name, cmd in PROBES.items():
            try:
                r = mgr.run_command(sb, cmd, timeout=20)
                out = (r.stdout or "").strip()
                err = (r.stderr or "").strip()
                print(f"### {name}")
                if out:
                    print(out[:600])
                if err and not out:
                    print(f"[stderr] {err[:300]}")
                print()
            except Exception as e:
                print(f"### {name}\n[probe error] {type(e).__name__}: {e}\n")
    finally:
        print("=== 清理探测沙箱 ===")
        try:
            mgr.kill(sid)
            print(f"killed {sid}")
        except Exception as e:
            print(f"kill failed: {e}")


if __name__ == "__main__":
    main()
