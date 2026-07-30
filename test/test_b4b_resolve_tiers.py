#!/usr/bin/env python3
"""B-4b V-H2 复核 H-2：**四档端口探测逐档行为覆盖**（不需要 Linux）。

## 为什么必须有这个文件

原先只有一条 `test_resolver_has_all_four_tool_tiers`，断的是**函数名字符串在场**。
5 条端到端全在 macOS 上走 `lsof`，`ss`/`netstat`/`/proc` 三档**从未被执行过一次**。
复核用三处突变证明这批测试结构上不可能发现问题（全绿）：

| 突变 | 原结果 |
|---|---|
| 把 C-1 那个 bug **修好**（`$10`→`$8`） | 全绿——修好与不修好，一条测试都不变 |
| 掏空 `smoke_ports_proc` 函数体（保留字符串） | 全绿 |
| `ss` 档 awk 的 `pid=` 改 `zid=`（整档失配） | 全绿 |

**C-1 之所以能出厂，根因就在这里**（四条硬检查①"接线覆盖 ≠ 机制存在"＋②"测试要证被
接上了而不是实现正确"）。

## 手法

把 `_PORT_RESOLVE_FUNCS`（生产 shell 段本体）取出来，**只替换路径/PATH**：
- `ss`/`netstat`/`lsof`/`pgrep` → 塞进临时 PATH 的假二进制，吐**真实格式样本**
- `/proc/...` → 临时夹具目录（真实 17 列 `/proc/net/tcp` + `socket:[inode]` 符号链接）

函数体一字不改，所以断的是生产逻辑本身，不是复刻品。
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from swarm.brain.nodes.runtime_smoke import _PORT_RESOLVE_FUNCS


def _bin(d: Path, name: str, body: str) -> None:
    """在 d 里放一个假二进制（吐固定样本），并置可执行。"""
    p = d / name
    p.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body), encoding="utf-8")
    p.chmod(0o755)


def _run(funcs: str, snippet: str, *, path_dir: Path | None = None,
         extra_env: dict | None = None) -> str:
    """跑 `funcs + snippet`，PATH 前置 path_dir（若给）。返回 stdout.strip()。"""
    env = dict(os.environ)
    if path_dir is not None:
        # 只留假二进制目录 + 最小系统路径（awk/sed/grep/sort 等仍需真实工具）
        env["PATH"] = f"{path_dir}:/usr/bin:/bin"
    env.update(extra_env or {})
    r = subprocess.run(["bash", "-c", funcs + "\n" + snippet],
                       capture_output=True, text=True, env=env, timeout=60)
    return r.stdout.strip()


# ══════════════════════════════════════════════
# ① ss 档
# ══════════════════════════════════════════════

_SS_SAMPLE = """
    if [ "$1" = "-ltnpH" ]; then
      cat <<'EOF'
LISTEN 0      4096         0.0.0.0:8080       0.0.0.0:*    users:(("api",pid=4242,fd=7))
LISTEN 0      511        127.0.0.1:11211      0.0.0.0:*    users:(("memcached",pid=99,fd=3))
EOF
    fi
"""


def test_ss_tier_resolves_only_the_target_pids_port(tmp_path):
    """`ss` 档：按 pid 过滤出本进程的端口，无关 listener（另一个 pid）不得混入。

    突变判据：把 awk 的 `pid=` 改成别的（整档失配）→ 这条红。
    """
    b = tmp_path / "bin"; b.mkdir()
    _bin(b, "ss", _SS_SAMPLE)
    out = _run(_PORT_RESOLVE_FUNCS, 'smoke_ports_ss "4242"', path_dir=b)
    assert out == "8080", f"ss 档解析错，实得 {out!r}"
    assert _run(_PORT_RESOLVE_FUNCS, 'smoke_ports_ss "99"', path_dir=b) == "11211"


def test_ss_tier_dedups_dual_stack_same_port(tmp_path):
    """dual-stack（v4+v6 同端口两行）必须去重成一个端口，否则误判 AMBIGUOUS → 冤枉 skip。"""
    b = tmp_path / "bin"; b.mkdir()
    _bin(b, "ss", """
    if [ "$1" = "-ltnpH" ]; then
      cat <<'EOF'
LISTEN 0 4096  0.0.0.0:3000 0.0.0.0:* users:(("app",pid=7,fd=3))
LISTEN 0 4096     [::]:3000    [::]:* users:(("app",pid=7,fd=4))
EOF
    fi
    """)
    assert _run(_PORT_RESOLVE_FUNCS, 'smoke_ports_ss "7"', path_dir=b) == "3000"


# ══════════════════════════════════════════════
# ② netstat 档（含 M-3：进程名带空格）
# ══════════════════════════════════════════════

def test_netstat_tier_handles_process_names_with_spaces(tmp_path):
    """★复核 M-3★ 真实 `netstat -ltnp` 会输出 `1234/nginx: master`——`$NF` 是 `master`
    → 原判据失配。改成扫全行匹配 `(^| )pid/` 后才对。
    """
    b = tmp_path / "bin"; b.mkdir()
    _bin(b, "netstat", """
    cat <<'EOF'
Proto Recv-Q Send-Q Local Address    Foreign Address  State   PID/Program name
tcp        0      0 0.0.0.0:8080     0.0.0.0:*        LISTEN  1234/nginx: master
tcp        0      0 0.0.0.0:9000     0.0.0.0:*        LISTEN  555/plainapp
EOF
    """)
    assert _run(_PORT_RESOLVE_FUNCS, 'smoke_ports_netstat "1234"', path_dir=b) == "8080", \
        "带空格的进程名（nginx: master）失配 → netstat 档对真实输出无效"
    assert _run(_PORT_RESOLVE_FUNCS, 'smoke_ports_netstat "555"', path_dir=b) == "9000"


def test_netstat_tier_does_not_match_pid_as_substring(tmp_path):
    """`(^| )pid/` 的词边界：pid=23 不得命中 `123/other`（否则采到别人的端口＝假绿）。"""
    b = tmp_path / "bin"; b.mkdir()
    _bin(b, "netstat", """
    cat <<'EOF'
tcp 0 0 0.0.0.0:7777 0.0.0.0:* LISTEN 123/other
EOF
    """)
    assert _run(_PORT_RESOLVE_FUNCS, 'smoke_ports_netstat "23"', path_dir=b) == ""


# ══════════════════════════════════════════════
# ③ /proc 档 —— C-1 的回归锁（本仓唯一能自己保证的那条腿）
# ══════════════════════════════════════════════

def _proc_fixture(tmp_path: Path, *, hex_port: str = "1F90", inode: str = "8675309",
                  pid: str = "4242") -> tuple[Path, str]:
    """造真实形状的 `/proc` 夹具：17 列 net/tcp（st=0A）+ `<pid>/fd/3 -> socket:[inode]`。"""
    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    # 列序严格照内核 get_tcp4_sock：sl local_addr rem_addr st tx:rx tr:when retrnsmt
    #                              uid timeout inode ref pointer drops ...
    (proc / "net" / "tcp").write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt"
        "   uid  timeout inode\n"
        f"   0: 00000000:{hex_port} 00000000:0000 0A 00000000:00000000 00:00000000"
        f" 00000000  1000        0 {inode} 1 0000000000000000 100 0 0 10 0\n",
        encoding="utf-8")
    fd = proc / pid / "fd"
    fd.mkdir(parents=True)
    (fd / "3").symlink_to(f"socket:[{inode}]")
    return proc, pid


def _funcs_with_proc(proc: Path) -> str:
    """把生产函数体里的 `/proc` 路径换成夹具目录（**只换路径，逻辑一字不改**）。"""
    return (_PORT_RESOLVE_FUNCS
            .replace("/proc/net/tcp6", str(proc / "net" / "tcp6"))
            .replace("/proc/net/tcp", str(proc / "net" / "tcp"))
            .replace('/proc/"$p"/fd/*', f'{proc}/"$p"/fd/*'))


def test_proc_tier_reads_inode_column_not_the_sk_pointer(tmp_path):
    """★★C-1 回归锁★★ `/proc` 档必须取 **inode**（`_rest_line` 的 `$8`），不是 `%pK` 指针。

    原实现取 `$10` ＝绝对第 12 列 ＝ sk 指针：`kptr_restrict=1` 下恒为 16 个 0，
    放开也是十六进制指针，**永不等于十进制 inode** —— 两种配置都确定失败，不是概率性。
    后果：该档恒返空 → `NONE` → `skipped`，而它正是沙箱镜像里**唯一**可保证的一档
    （Dockerfile 不装 iproute2/net-tools/lsof）。

    突变判据：把 `$8` 改回 `$10` → 这条必须红。
    """
    proc, pid = _proc_fixture(tmp_path)          # 0x1F90 = 8080
    out = _run(_funcs_with_proc(proc), f'smoke_ports_proc "{pid}"')
    assert out == "8080", (
        f"/proc 档没解析出 8080（实得 {out!r}）——取错列了？"
        "该档是最小容器里唯一可用的腿，坏了 V-H2 对生产净收益为零")


def test_proc_tier_ignores_sockets_not_owned_by_the_pid(tmp_path):
    """只认该 pid 持有的 socket inode：别人的 listener 不得被采（假绿面）。"""
    proc, pid = _proc_fixture(tmp_path, inode="111")
    # net/tcp 里再加一条别人的 listener（inode 999，pid 的 fd 里没有它）
    tcp = proc / "net" / "tcp"
    tcp.write_text(tcp.read_text(encoding="utf-8") +
                   "   1: 00000000:0BB8 00000000:0000 0A 00000000:00000000 00:00000000"
                   " 00000000  1000        0 999 1 0000000000000000 100 0 0 10 0\n",
                   encoding="utf-8")
    out = _run(_funcs_with_proc(proc), f'smoke_ports_proc "{pid}"')
    assert out == "8080", f"采到了不属于本进程的 socket（3000=0x0BB8），实得 {out!r}"


def test_proc_tier_skips_non_listen_states(tmp_path):
    """st != 0A（如 01=ESTABLISHED）不是 listener，不得采。"""
    proc, pid = _proc_fixture(tmp_path)
    tcp = proc / "net" / "tcp"
    tcp.write_text(tcp.read_text(encoding="utf-8").replace(" 0A ", " 01 "), encoding="utf-8")
    assert _run(_funcs_with_proc(proc), f'smoke_ports_proc "{pid}"') == ""


# ══════════════════════════════════════════════
# ④ pgrep 缺席兜底 —— C-2 的回归锁
# ══════════════════════════════════════════════

def test_pid_tree_falls_back_to_proc_stat_when_pgrep_missing(tmp_path):
    """★★C-2 回归锁★★ `pgrep` 缺席时必须回落扫 `/proc/*/stat` 的 PPid。

    为什么这步是**必经**而非可选：`bash -c '<单条简单命令>'` 会 exec 优化 → `SMOKE_PID`
    就是 `cargo`/`go` 本体，而 `cargo run`/`go run` 的真监听者**恒是它们 fork 的子进程**。
    沙箱镜像不装 procps → 没兜底则树只有根 PID → V-H2 目标人群全体反解失败。

    comm 可含空格与括号（`(my app)`）→ PPid 必须从**最后一个** `)` 之后取。
    """
    proc = tmp_path / "proc"
    for pid, ppid, comm in (("100", "1", "parent"), ("200", "100", "my app"),
                            ("300", "1", "unrelated")):
        d = proc / pid
        d.mkdir(parents=True)
        (d / "stat").write_text(
            f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1 4194304 100 0 0 0 1 2 3 4\n",
            encoding="utf-8")

    funcs = _PORT_RESOLVE_FUNCS.replace("/proc/[0-9]*/stat", f"{proc}/[0-9]*/stat")
    b = tmp_path / "bin"; b.mkdir()
    _bin(b, "pgrep", 'exit 127\n')       # 行为等价于二进制缺席（无输出 + 非零）

    out = _run(funcs, 'smoke_pid_tree "100" | tr "\\n" " "', path_dir=b)
    got = set(out.split())
    assert got == {"100", "200"}, (
        f"pgrep 缺席时树遍历失效（实得 {got}）——应含子进程 200（comm 带空格），"
        "且不得含无关进程 300")


def test_pid_tree_prefers_pgrep_when_available(tmp_path):
    """pgrep 在场时用它（兜底不该抢班）。"""
    b = tmp_path / "bin"; b.mkdir()
    _bin(b, "pgrep", """
    if [ "$1" = "-P" ] && [ "$2" = "500" ]; then echo 501; echo 502; fi
    """)
    out = _run(_PORT_RESOLVE_FUNCS, 'smoke_pid_tree "500" | tr "\\n" " "', path_dir=b)
    assert set(out.split()) == {"500", "501", "502"}, out


# ══════════════════════════════════════════════
# ⑤ tier 可观测（M-1）
# ══════════════════════════════════════════════

@pytest.mark.parametrize("present,expect", [
    ("ss", "ss"), ("netstat", "netstat"), ("lsof", "lsof"),
])
def test_resolve_tier_reports_which_tool_is_used(tmp_path, present, expect):
    """★M-1★ 必须能机读"用了哪一档"——否则"反解不出"分不清是没工具还是应用没 bind。"""
    b = tmp_path / "bin"; b.mkdir()
    _bin(b, present, "exit 0\n")
    assert _run(_PORT_RESOLVE_FUNCS, "smoke_resolve_tier", path_dir=b) == expect


def test_resolve_tier_reports_none_available_when_all_four_are_missing(tmp_path):
    """四档全废 → `none_available`（据此报独立 reason，不与"应用没 bind"共用名字）。

    `/proc/net/tcp` 在 macOS 上本就不存在；Linux 上跑本测试时靠空 PATH + 不可读路径。
    """
    b = tmp_path / "bin"; b.mkdir()
    funcs = _PORT_RESOLVE_FUNCS.replace("/proc/net/tcp", str(tmp_path / "nonexistent"))
    assert _run(funcs, "smoke_resolve_tier", path_dir=b) == "none_available"
