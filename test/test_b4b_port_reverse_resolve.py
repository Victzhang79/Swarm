#!/usr/bin/env python3
"""B-4b（27 号文 V-H2）：端口反解 —— 一次救活 Rust + 全部非 Gin Go。

## V-H2 是什么

`_FRAMEWORK_DEFAULT_PORTS` 里 **Rust 一个框架都没有**（axum/actix/rocket 全无），Go **只有
Gin** —— echo/fiber/chi/net-http 全部退化成裸 `"go"` → `port=None` → `verify_runtime` 在
"推导不全"处直接 skip → **这些栈的冒烟 100% 从未跑过**（27 号文 §1 矩阵：Rust 那行"端口表
无任何 Rust 框架"、Go 那行"只有 Gin 能跑"）。

而 `_derive_start_rust` / `_derive_start_go` **本来就在 `_ENTRY_DERIVERS` 里**，`start_cmd`
推得出。缺的只有端口。所以治法不是逐个框架往表里塞默认端口（那是猜，且永远追不完），
而是**起进程后问进程自己**。

## 反解的四条设计要点（每条都在下面有对应测试）

1. **按进程树限定** —— 沙箱里可能有别的 listener（前轮残留/sidecar）。全局扫端口会把无关
   listener 当成"应用起来了"＝假绿。
2. **工具四级降级** `ss`→`netstat`→`lsof`→`/proc/net/tcp`+`/proc/<pid>/fd`（末条零外部依赖）。
3. **多监听 fail-closed** —— 同树监听多端口（app+metrics）→ `AMBIGUOUS`，不猜。
4. **反解不出 ≠ 启动失败** —— 报 skipped，绝不把"我们没探"判成"应用崩了"。

## 诚实边界

- 已知端口路径**逐字节不变**（无反解痕迹、F4 预检在位）——本文件有对照臂锁。
- 反解路径**没有 PORT_BUSY 保护**（未知端口无从预检）；按进程树限定保证不误采残留 listener，
  代价是"端口被占起不来"退化成 `NONE` → skipped 而非明确 PORT_BUSY。
- 反解路径上**验收断言本轮不执行**：`assertion_to_probe_cmd` 在构建时把端口烤进 curl 片段
  （对 None 直接 ValueError），而反解值到运行时才有。本批不改断言层契约（那会扩面），
  断言如实降级、冒烟本体照跑。断言可执行化（改用 `$SMOKE_PORT`）留后续。
- `/proc` 路径在 macOS 上无从验证（本机无 `/proc`），本文件的真跑走的是 `lsof` 分支；
  沙箱是 Linux，`ss`/`/proc` 两条**尚未在真 Linux 上实测**——诚实记录，B-4b 后续补。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_s = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_m = importlib.util.module_from_spec(_s)
_s.loader.exec_module(_m)

from swarm.brain.nodes.runtime_smoke import (  # noqa: E402
    MARK_PORT_BUSY,
    MARK_PORT_RESOLVED,
    build_smoke_script,
    parse_smoke_markers,
)


# ══════════════════════════════════════════════
# ① 脚本生成：已知端口逐字节不变 / 未知端口才注入反解
# ══════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _shrink_resolve_window(monkeypatch):
    """C-1 后反解窗口是**恒定成本**（整窗取并集），生产默认 30s → 端到端用例逐条付 30s。

    本文件统一缩到 8s：窗口长度本身不是被测机制，但必须 **> 错时 bind 的偏移**
    （skewed 用例 5s），否则会把 C-1 那条测成"根本没看见第二个 listener"的弱命题。
    """
    monkeypatch.setenv("SWARM_SMOKE_PORT_RESOLVE_WINDOW_SEC", "8")


def test_known_port_script_has_no_resolve_traces():
    """★对照臂★ 已知端口路径必须**一点反解痕迹都没有**，且 F4 预检在位。

    治本绝不能顺手改掉 Maven/Spring 这些本来好用的路径（G9/L3 铁律）。
    """
    s = build_smoke_script("java -jar app.jar", 8080, "/actuator/health")
    assert "smoke_resolve_port" not in s
    assert MARK_PORT_RESOLVED not in s
    assert "SMOKE_PORT=8080" in s
    assert MARK_PORT_BUSY in s, "已知端口必须保留 F4 端口预检"


def test_unknown_port_script_injects_resolver_and_drops_f4():
    """未知端口 → 注入反解、摘掉 F4（无从预检）。"""
    s = build_smoke_script("cargo run --release", None, "/")
    assert "smoke_resolve_port" in s
    assert MARK_PORT_RESOLVED in s
    assert MARK_PORT_BUSY not in s, "未知端口无从预检，不该有 F4 段"


def test_resolve_block_comes_after_the_cleanup_trap():
    """★真 bug 的回归锁★ 反解段会 `exit 0`；若排在 `trap smoke_cleanup` **之前**，
    反解失败退出时**应用进程会泄漏**（写这批时自己踩了一次）。

    判据是三者的相对序：起进程 → 装 trap → 反解。
    """
    s = build_smoke_script("cargo run", None, "/")
    i_pid = s.index("SMOKE_PID=$!")
    i_trap = s.index("trap smoke_cleanup EXIT")
    i_resolve = s.index("__SMOKE_PHASE__resolve_port")
    assert i_pid < i_trap < i_resolve, (
        f"序错了（pid={i_pid} trap={i_trap} resolve={i_resolve}）——"
        "反解排在 trap 前，退出时应用进程泄漏")


def test_resolver_is_process_tree_scoped_not_global():
    """★设计要点①★ 反解必须按 `$SMOKE_PID` 的进程树限定，绝不全局扫端口。

    全局扫会把前轮残留/sidecar 的 listener 当成"应用起来了"＝假绿。
    断"接线事实"（脚本把 SMOKE_PID 传给了反解器），不断字面实现（纪律 6）。
    """
    s = build_smoke_script("go run ./cmd/api", None, "/")
    assert 'smoke_resolve_port "$SMOKE_PID"' in s, "反解未按进程树限定＝可能误采无关 listener"
    assert "smoke_pid_tree" in s, "缺进程树遍历——父不监听子监听的 wrapper 形态会漏"


def test_resolver_has_all_four_tool_tiers():
    """★设计要点②★ 四级降级齐备；末级 `/proc` 零外部依赖（最小容器唯一可用）。"""
    s = build_smoke_script("cargo run", None, "/")
    for fn in ("smoke_ports_ss", "smoke_ports_netstat", "smoke_ports_lsof",
               "smoke_ports_proc"):
        assert fn in s, f"缺 {fn} 降级层"
    assert "/proc/net/tcp" in s


# ══════════════════════════════════════════════
# ② 解析：三种反解结果机读可辨，且防应用回显伪造
# ══════════════════════════════════════════════

@pytest.mark.parametrize("raw,expect", [
    ("8080", "8080"),
    ("AMBIGUOUS:8080,9090", "AMBIGUOUS:8080,9090"),
    ("NONE", "NONE"),
])
def test_parse_port_resolved_values(raw, expect):
    p = parse_smoke_markers(f"{MARK_PORT_RESOLVED}{raw}\n__SMOKE_DONE__")
    assert p["port_resolved"] == expect


def test_parse_returns_none_when_resolve_not_used():
    """端口已知时不该有该键值——`None` 表示"本轮未走反解"，与 `NONE` 字符串是两回事。"""
    p = parse_smoke_markers("__SMOKE_PROBE__ok:200\n__SMOKE_DONE__")
    assert p["port_resolved"] is None


def test_app_echoed_fake_resolved_port_is_ignored():
    """★I-M1 同族★ 被测应用回显的伪造反解标记，不得被采信。

    若从原文而非 ctrl（剥掉应用日志区）取，应用打一行
    `__SMOKE_PORT_RESOLVED__80` 就能把探活指向别的端口。

    ★夹具必须让"只有伪造标记"★（本批教训：我第一版把真标记放在伪造**之前**，
    而 `re.search` 取首个匹配 → 从原文取也照样返回真值 → 那条断言零区分力，
    突变 M8 落地后仍全绿。现在日志区外**没有**真标记，从原文取必然读到伪造的 80。）
    """
    out = ("__SMOKE_LOG_TAIL_BEGIN__\n"
           f"{MARK_PORT_RESOLVED}80\n"          # 应用日志区内的伪造，且是全文唯一一条
           "__SMOKE_LOG_TAIL_END__\n__SMOKE_DONE__")
    assert parse_smoke_markers(out)["port_resolved"] is None, (
        "采信了应用日志区里的伪造反解标记（探活会被指向攻击者选的端口）")


# ══════════════════════════════════════════════
# ②b 闸：生产上什么 state 会走到反解分支（B-4a CRITICAL-1 的教训）
# ══════════════════════════════════════════════

def test_real_rust_project_derives_start_cmd_but_no_port(tmp_path):
    """★前提句（必须先证）★ 真 Rust 工程经**真实 `derive_runtime_smoke`** 得到
    `start_cmd` 有、`port` 无 —— 这才是反解分支在生产上的入口条件。

    不手工构造 derivation：手写 `port=None` 只能证"分支行为对"，证不了"生产会走到"。
    B-4a 的 CRITICAL-1 就是分支判据正确、单测全绿、突变也红，而**生产永不执行**。
    """
    from swarm.brain.smoke_derive import derive_runtime_smoke

    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "api"\nversion = "0.1.0"\nedition = "2021"\n\n'
        '[dependencies]\naxum = "0.7"\ntokio = { version = "1", features = ["full"] }\n',
        encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text(
        "#[tokio::main]\nasync fn main() {\n    let app = axum::Router::new();\n}\n",
        encoding="utf-8")

    d = derive_runtime_smoke({"backend": "Axum (rust)", "build": "cargo"}, str(tmp_path))
    assert d.start_cmd, f"Rust 工程应推得出 start_cmd（_derive_start_rust），实得 {d.start_cmd!r}"
    assert d.port is None, (
        f"若 Rust 已有默认端口（实得 {d.port}）→ V-H2 前提变了，本测试与闸都要重看")


@pytest.mark.parametrize("start_cmd,port,usable,reverse", [
    ("cargo run --release", None, True, True),    # Rust/非 Gin Go：反解路径
    ("go run ./cmd/api", None, True, True),
    ("java -jar app.jar", 8080, True, False),     # 端口已知：老路径，逐字节不变
    (None, None, False, False),                   # 无启动方式：死路，不是反解
    (None, 8080, False, False),                   # 有端口没启动方式：仍死路
])
def test_gate_predicates_are_production_code_not_rewritten_in_the_test(
        start_cmd, port, usable, reverse):
    """★闸本尊（调生产判据，不在测试里重写条件）★

    锁的是 `verify.py` 的判据由 `not start_cmd or port is None` 收窄成 `not start_cmd`。
    突变把它改回去 → `smoke_derivation_usable(port=None)` 变 False → 这条红。

    ★我第一版把条件在测试里重写了一遍（`rev = bool(start_cmd) and port is None`）★
    那是纯自证恒真，对"闸被改回旧判据"零区分力。判据必须来自被测模块——所以生产侧
    把它抽成了 `smoke_derivation_missing`/`_usable`/`should_reverse_resolve_port`。
    """
    from types import SimpleNamespace

    from swarm.brain.nodes.verify import (
        should_reverse_resolve_port,
        smoke_derivation_missing,
        smoke_derivation_usable,
    )

    d = SimpleNamespace(start_cmd=start_cmd, port=port)
    assert smoke_derivation_usable(d) is usable
    assert should_reverse_resolve_port(d) is reverse
    if not usable:
        assert "start_cmd" in smoke_derivation_missing(d)
    else:
        assert smoke_derivation_missing(d) == [], (
            "port 缺席不该进 missing（否则 V-H2 整条失效，Rust/非 Gin Go 继续 100% skip）")


# ══════════════════════════════════════════════
# ③ 端到端真跑：起真监听进程 → 反解 → 探活拿到 200
# ══════════════════════════════════════════════

_APP = (
    "import http.server, socketserver\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
    "    def log_message(self, *a): pass\n"
    "with socketserver.TCPServer(('127.0.0.1', 0), H) as s:\n"
    "    print('listening', s.server_address[1], flush=True)\n"
    "    s.serve_forever()\n"
)


def test_end_to_end_resolves_a_real_ephemeral_port_and_probes_it(tmp_path):
    """★V-H2 本尊★ 应用绑 **:0**（端口由内核分配，任何默认端口表都不可能猜中）→
    脚本必须反解出真端口并探活成功。

    这条就是"Rust/非 Gin Go 的冒烟从 100% skip 变成真跑"的最小实证。
    """
    app = tmp_path / "app.py"
    app.write_text(_APP, encoding="utf-8")
    script = build_smoke_script(f"{sys.executable} {app}", None, "/",
                               timeout_sec=20, workdir=str(tmp_path))
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)
    out = r.stdout

    parsed = parse_smoke_markers(out)
    assert parsed["done"], f"脚本未跑完:\n{out[-1500:]}\n{r.stderr[-500:]}"
    resolved = parsed["port_resolved"]
    assert resolved and resolved.isdigit(), (
        f"未反解出端口（得 {resolved!r}）——真监听进程必须被反解到:\n{out[-1500:]}")
    assert "ok:200" in " ".join(parsed["probe_sequence"]), (
        f"反解出端口后探活应拿到 200，实得 {parsed['probe_sequence']}")


def test_end_to_end_non_listening_process_is_none_not_a_crash_verdict(tmp_path):
    """★设计要点④★ 进程活着但不监听 → `NONE`，且**不探活**（探未知端口必假红）。

    方向很要紧：这是"我们没探"，不是"应用崩了"。上层据此判 skipped。
    """
    app = tmp_path / "noop.py"
    app.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    script = build_smoke_script(f"{sys.executable} {app}", None, "/",
                               timeout_sec=8, workdir=str(tmp_path))
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)
    parsed = parse_smoke_markers(r.stdout)
    assert parsed["port_resolved"] == "NONE", f"实得 {parsed['port_resolved']!r}"
    assert not parsed["probe_sequence"], (
        f"反解不出端口却探活了（必假红）：{parsed['probe_sequence']}")
    assert parsed["done"], "收尾标记必须照常输出（否则上层判 infra 中断而非 skipped）"


def test_end_to_end_two_listeners_is_ambiguous_not_a_guess(tmp_path):
    """★设计要点③★ 同进程树监听两个端口 → `AMBIGUOUS`，绝不挑一个猜。

    真实形态：app + metrics/admin 端口。猜错了探到 metrics 上，健康探活会假绿。
    """
    app = tmp_path / "two.py"
    app.write_text(
        "import socket, time\n"
        "a = socket.socket(); a.bind(('127.0.0.1', 0)); a.listen(1)\n"
        "b = socket.socket(); b.bind(('127.0.0.1', 0)); b.listen(1)\n"
        "time.sleep(60)\n", encoding="utf-8")
    script = build_smoke_script(f"{sys.executable} {app}", None, "/",
                               timeout_sec=8, workdir=str(tmp_path))
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)
    parsed = parse_smoke_markers(r.stdout)
    assert (parsed["port_resolved"] or "").startswith("AMBIGUOUS:"), (
        f"多监听未 fail-closed，实得 {parsed['port_resolved']!r}")
    assert not parsed["probe_sequence"], "歧义时不得探活"


def test_end_to_end_listeners_bound_at_different_times_is_ambiguous(tmp_path):
    """★C-1（reviewer 复核，CRITICAL 假过）★ 两个 listener **错时** bind 也必须 AMBIGUOUS。

    上面那条同型测试把两个 socket **背靠背**bind，于是"多监听 fail-closed"在旧实现
    （首轮见到唯一端口就 `break`）下也恒绿——夹具形状让被测命题变成了另一个更弱的命题。
    真实形态是错时的：metrics/admin 端口进程一起来就 bind，业务 server 要等 async runtime
    就绪 / DB 连上才 bind。旧实现首轮只看见 metrics → 采纳它 → 探 `/` 得 404 →
    `passed:started`，**业务 server 一次都没被探过**。

    突变判据：把 resolve 循环改回"见到单端口即 break"，本条必红（实得单个数字端口）。
    """
    app = tmp_path / "skewed.py"
    app.write_text(
        "import socket, time\n"
        # 先 bind「metrics」端口——旧实现会在首轮就采纳它
        "a = socket.socket(); a.bind(('127.0.0.1', 0)); a.listen(1)\n"
        # 业务 server 晚 5s 才 bind（> 一个轮询间隔，确保首轮看不到它）
        "time.sleep(5)\n"
        "b = socket.socket(); b.bind(('127.0.0.1', 0)); b.listen(1)\n"
        "time.sleep(60)\n", encoding="utf-8")
    script = build_smoke_script(f"{sys.executable} {app}", None, "/",
                               timeout_sec=8, workdir=str(tmp_path))
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=180)
    parsed = parse_smoke_markers(r.stdout)
    assert (parsed["port_resolved"] or "").startswith("AMBIGUOUS:"), (
        "错时 bind 的多监听被当成单监听采纳了（＝C-1 假过通道复活），"
        f"实得 {parsed['port_resolved']!r}")
    assert not parsed["probe_sequence"], "歧义时不得探活"


def test_end_to_end_single_listener_still_resolves_after_full_window(tmp_path):
    """C-1 对照臂：单监听应用**照旧**解得出端口并探活成功（整窗取并集不误杀单监听）。

    C-1 的治法是"窗口结束才裁决"，代价是恒付满窗口——这条锁住"代价只是时间、不是结论"。
    """
    app = tmp_path / "single.py"
    app.write_text(_APP, encoding="utf-8")
    script = build_smoke_script(f"{sys.executable} {app}", None, "/",
                               timeout_sec=8, workdir=str(tmp_path))
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=180)
    parsed = parse_smoke_markers(r.stdout)
    assert (parsed["port_resolved"] or "").isdigit(), (
        f"单监听反解失败，实得 {parsed['port_resolved']!r}")
    assert parsed["probe_sequence"], "解出端口后必须真探活"
    assert parsed["port_resolve_tier_answered"] not in (None, "none_answered"), (
        f"作答档未留痕（M-1）：{parsed['port_resolve_tier_answered']!r}")


def test_end_to_end_child_listens_parent_does_not(tmp_path):
    """★进程树遍历本尊★ 父进程不监听、子进程监听（wrapper 脚本 → 真 server，
    `cargo run` / `npm start` / `go run` 全是这个形态）。

    只看 `$SMOKE_PID` 自己的 socket 会**漏掉全部这类应用**。
    """
    child = tmp_path / "child.py"
    child.write_text(_APP, encoding="utf-8")
    parent = tmp_path / "parent.py"
    parent.write_text(
        f"import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(child)!r}])\n"
        "time.sleep(60)\n", encoding="utf-8")
    script = build_smoke_script(f"{sys.executable} {parent}", None, "/",
                               timeout_sec=20, workdir=str(tmp_path))
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)
    parsed = parse_smoke_markers(r.stdout)
    assert parsed["port_resolved"] and parsed["port_resolved"].isdigit(), (
        f"子进程监听未被树遍历找到（得 {parsed['port_resolved']!r}）——"
        f"wrapper→server 是最常见形态:\n{r.stdout[-1200:]}")


def test_end_to_end_dead_app_on_reverse_path_reports_exit_code(tmp_path, monkeypatch):
    """★M-4★ 反解路径应用**已退出**时，收尾块也要回显退出码（与已知端口路径同构）。

    原实现是 `if kill -0 …; then echo APP_RC alive; fi`——没有 else 分支，应用已死时
    什么都不输出 → `app_rc=None`，"崩在第几步"的归因信号整条丢失。
    这条**跑真脚本**（不是往 stdout 里塞合成标记），否则测的是解析器而非产出标记的 shell。

    突变判据：删掉 `else wait "$SMOKE_PID"; echo APP_RC $?`，本条必红。
    """
    monkeypatch.setenv("SWARM_SMOKE_PORT_RESOLVE_WINDOW_SEC", "6")
    app = tmp_path / "dies.py"
    app.write_text("import sys\nsys.stderr.write('boom\\n')\nsys.exit(3)\n", encoding="utf-8")
    script = build_smoke_script(f"{sys.executable} {app}", None, "/",
                               timeout_sec=8, workdir=str(tmp_path))
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)
    parsed = parse_smoke_markers(r.stdout)
    assert parsed["port_resolved"] == "NONE", parsed["port_resolved"]
    assert parsed["app_rc"] == 3, (
        f"应用已退出但退出码没带回来（归因信号丢失）：app_rc={parsed['app_rc']!r}")
