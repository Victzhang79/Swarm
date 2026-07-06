#!/usr/bin/env python3
"""Swarm 远程沙箱集成测试 — 严格验证 CubeSandbox 集成

测试范围：
  1. SandboxManager 基础功能（创建/执行/销毁）
  2. CodeResult 解析
  3. 远程文件操作（读/写/执行）
  4. build_tools 沙箱模式集成
  5. WorkerExecutor 沙箱生命周期
  6. 极端场景（超长输出、错误处理、并发安全）

所有测试必须连接到远程 CubeSandbox (192.168.60.106) 实际执行。
"""

import importlib.util
import sys
import time
import traceback
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ── 集成测试门控 ──────────────────────────────────────────
# 这些是连接真实 CubeSandbox 的集成测试（慢、需外部 infra、会装 pip 包）。
# 默认在沙箱不可达时整体 skip，避免 (1) 无 infra 时报错噪音 (2) 误判为单元测试。
# 强制运行：设 SWARM_RUN_SANDBOX_IT=1（CI 的集成阶段用）。
def _sandbox_reachable() -> bool:
    import os
    if os.environ.get("SWARM_RUN_SANDBOX_IT") == "1":
        return True
    try:
        from swarm.config.settings import get_config

        cfg = get_config()
        api_url = getattr(cfg.sandbox, "api_url", "") or ""
        if not api_url:
            return False
        import httpx

        # 探测 envd/健康端点（短超时，不可达即跳过）
        base = api_url.rstrip("/")
        resp = httpx.get(base, timeout=3.0)
        return resp.status_code < 500
    except Exception:
        return False


import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(
    not _sandbox_reachable(),
    reason="CubeSandbox 不可达（设 SWARM_RUN_SANDBOX_IT=1 或配置 sandbox.api_url 后运行集成测试）",
)


# ── 代码解释器(Jupyter / run_code)能力探测 ──────────────────────────────
# 2026-06-29 实证：CubeMaster 现存模板是【按项目烤的 shell 镜像】，不跑 e2b 代码解释器
# (Jupyter kernel) → run_code 打到 openresty 502。而 swarm 实路径(L1 pipeline 编译/构建)
# 全走 run_command(shell)，run_code 仅遗留 helper run_in_sandbox(全代码库无人调用)。故
# run_code 类集成测试在【无代码解释器模板】时 skip（有 Jupyter 模板时仍照常校验），不让
# 模板能力缺口伪装成 swarm 失败。
_CI_SUPPORTED: bool | None = None


def _code_interpreter_supported() -> bool:
    global _CI_SUPPORTED
    if _CI_SUPPORTED is not None:
        return _CI_SUPPORTED
    if not _sandbox_reachable():
        _CI_SUPPORTED = False
        return False
    try:
        from swarm.worker.sandbox import SandboxManager

        m = SandboxManager()
        sb = m.create(timeout=60)
        try:
            r = m.run_code(sb, "print('ci_probe')")
            _CI_SUPPORTED = bool(getattr(r, "success", False) and "ci_probe" in (r.stdout or ""))
        finally:
            m.kill(sb.sandbox_id)
    except Exception:  # noqa: BLE001
        _CI_SUPPORTED = False
    return _CI_SUPPORTED


requires_code_interpreter = pytest.mark.skipif(
    not _code_interpreter_supported(),
    reason="当前 CubeMaster 模板无代码解释器(Jupyter/run_code)；swarm 实路径用 run_command(shell)，"
           "run_code 为遗留(run_in_sandbox 无人调用)，跳过 run_code 集成校验",
)


def separator(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


@requires_code_interpreter
def test_1_sandbox_manager_basic():
    """测试 1: SandboxManager 创建/执行/销毁"""
    separator("TEST 1: SandboxManager 基础生命周期")

    from swarm.worker.sandbox import SandboxManager

    manager = SandboxManager()

    # 创建
    print("  [1.1] 创建沙箱...")
    sandbox = manager.create()
    sid = sandbox.sandbox_id
    print(f"  ✅ 沙箱已创建: {sid}")
    print(f"  活跃沙箱数: {manager.active_count}")
    assert manager.active_count == 1
    assert sid in manager.active_ids

    # 执行简单代码
    print("  [1.2] 执行简单 Python...")
    result = manager.run_code(sandbox, "print('Hello from SandboxManager!')")
    print(f"  stdout: {result.stdout}")
    print(f"  success: {result.success}")
    assert result.success
    assert "Hello from SandboxManager!" in result.stdout

    # 裸表达式求值
    print("  [1.3] 裸表达式求值...")
    result = manager.run_code(sandbox, "2**10")
    print(f"  text: {result.text}")
    assert result.text == "1024"

    # 销毁
    print("  [1.4] 销毁沙箱...")
    manager.kill(sid)
    print(f"  ✅ 沙箱已销毁，活跃数: {manager.active_count}")
    assert manager.active_count == 0

    print("  ✅ TEST 1 PASSED")


@requires_code_interpreter
def test_2_code_result_parsing():
    """测试 2: CodeResult 各种输出解析"""
    separator("TEST 2: CodeResult 解析")

    from swarm.worker.sandbox import SandboxManager

    manager = SandboxManager()
    sandbox = manager.create()

    try:
        # stdout 输出
        print("  [2.1] 解析 stdout 输出...")
        r = manager.run_code(sandbox, "print('line1'); print('line2')")
        print(f"  stdout: {r.stdout!r}")
        assert "line1" in r.stdout and "line2" in r.stdout
        assert r.success

        # stderr 输出
        print("  [2.2] 解析 stderr 输出...")
        r = manager.run_code(sandbox, "import sys; print('err_msg', file=sys.stderr)")
        print(f"  stderr: {r.stderr!r}")
        assert "err_msg" in r.stderr

        # 错误输出
        print("  [2.3] 解析执行错误...")
        r = manager.run_code(sandbox, "raise ValueError('test error')")
        print(f"  error: {r.error!r}")
        print(f"  success: {r.success}")
        assert not r.success
        assert r.error is not None

        # 混合输出
        print("  [2.4] 解析混合 stdout + 表达式...")
        r = manager.run_code(sandbox, "x = 42; print(f'x={x}'); x * 2")
        print(f"  stdout: {r.stdout!r}, text: {r.text!r}")
        assert "x=42" in r.stdout
        assert "84" in r.text

        print("  ✅ TEST 2 PASSED")
    finally:
        manager.kill_all()


@requires_code_interpreter
def test_3_remote_file_operations():
    """测试 3: 远程沙箱中的文件操作"""
    separator("TEST 3: 远程文件操作")

    from swarm.worker.sandbox import SandboxManager

    manager = SandboxManager()
    sandbox = manager.create()

    try:
        # 写文件
        print("  [3.1] 写文件...")
        r = manager.run_code(sandbox, """
with open('/tmp/swarm_test.py', 'w') as f:
    f.write('def hello():\\n    return "world"\\n')
print('file written')
""")
        print(f"  stdout: {r.stdout}")
        assert "file written" in r.stdout

        # 读文件
        print("  [3.2] 读文件...")
        r = manager.run_code(sandbox, """
with open('/tmp/swarm_test.py', 'r') as f:
    content = f.read()
print(f'content: {content!r}')
""")
        print(f"  stdout: {r.stdout}")
        assert "def hello" in r.stdout

        # 执行刚写的代码
        print("  [3.3] import 并执行写的代码...")
        r = manager.run_code(sandbox, """
import sys; sys.path.insert(0, '/tmp')
from swarm_test import hello
print(f'result: {hello()}')
""")
        print(f"  stdout: {r.stdout}")
        assert "result: world" in r.stdout

        # 检查远程环境
        print("  [3.4] 检查远程环境信息...")
        r = manager.run_code(sandbox, """
import platform, sys, os
print(f'OS: {platform.system()} {platform.release()}')
print(f'Python: {sys.version}')
print(f'CWD: {os.getcwd()}')
print(f'CPU: {platform.processor() or platform.machine()}')
""")
        print(f"  {r.stdout}")
        assert r.success

        print("  ✅ TEST 3 PASSED")
    finally:
        manager.kill_all()


@requires_code_interpreter
def test_4_sandbox_process_execution():
    """测试 4: 远程沙箱中的进程执行（subprocess）"""
    separator("TEST 4: 远程进程执行")

    from swarm.worker.sandbox import SandboxManager

    manager = SandboxManager()
    sandbox = manager.create()

    try:
        # subprocess 执行
        print("  [4.1] subprocess.run 执行 shell 命令...")
        r = manager.run_code(sandbox, """
import subprocess
result = subprocess.run(['echo', 'hello from subprocess'], capture_output=True, text=True)
print(result.stdout)
print(f'exit_code={result.returncode}')
""")
        print(f"  stdout: {r.stdout}")
        assert "hello from subprocess" in r.stdout
        assert "exit_code=0" in r.stdout

        # pip 检查
        print("  [4.2] 检查远程 Python 包...")
        r = manager.run_code(sandbox, """
import subprocess
result = subprocess.run(['pip', 'list'], capture_output=True, text=True, timeout=30)
# 只显示前 5 行
lines = result.stdout.strip().split('\\n')[:5]
for line in lines:
    print(line)
""")
        print(f"  stdout: {r.stdout}")
        assert r.success

        # 安装包测试
        print("  [4.3] 远程安装轻量包...")
        r = manager.run_code(sandbox, """
import subprocess
result = subprocess.run(['pip', 'install', 'requests', '-q'], capture_output=True, text=True, timeout=120)
print(f'install exit: {result.returncode}')
# 验证
import requests
print(f'requests version: {requests.__version__}')
""", timeout=180)
        print(f"  stdout: {r.stdout}")
        assert "install exit: 0" in r.stdout
        assert "requests version" in r.stdout

        print("  ✅ TEST 4 PASSED")
    finally:
        manager.kill_all()


def test_5_build_tools_sandbox_mode():
    """测试 5: build_tools 的沙箱模式集成"""
    separator("TEST 5: build_tools 沙箱模式")

    from swarm.tools.build_tools import (
        _run,
        clear_sandbox_context,
        get_sandbox_context,
        set_sandbox_context,
    )
    from swarm.worker.sandbox import SandboxManager

    # 5.1 无沙箱时应走本地模式
    print("  [5.1] 无沙箱上下文 → 本地模式...")
    clear_sandbox_context()
    sbx, mgr = get_sandbox_context()
    assert sbx is None
    print("  ✅ 无沙箱上下文确认")

    # 5.2 设置沙箱上下文
    print("  [5.2] 设置沙箱上下文...")
    manager = SandboxManager()
    sandbox = manager.create()
    set_sandbox_context(sandbox, manager)

    sbx2, mgr2 = get_sandbox_context()
    assert sbx2 is sandbox
    assert mgr2 is manager
    print("  ✅ 沙箱上下文设置成功")

    # 5.3 _run 自动走沙箱
    print("  [5.3] _run() 自动路由到沙箱...")
    result = _run("echo 'hello from sandbox _run'")
    print(f"  result: {result[:200]}")
    # build_tools._run 沙箱成功输出格式为 "✅ (sandbox 0)\n<body>"（exit_hint=0 表示退出码 0）。
    assert "(sandbox 0)" in result, f"期望沙箱成功标记 (sandbox 0)，实际: {result[:120]}"
    assert "hello from sandbox _run" in result
    print("  ✅ _run 沙箱路由成功")

    # 5.4 清除后应回退到本地（不测试本地执行以免安全风险）
    print("  [5.4] 清除沙箱上下文...")
    clear_sandbox_context()
    sbx3, _ = get_sandbox_context()
    assert sbx3 is None
    print("  ✅ 清除成功")

    # 清理
    manager.kill_all()
    print("  ✅ TEST 5 PASSED")


# 批5：test_6（WorkerExecutor.create_sandbox/run_in_sandbox 遗留集成路径）随死代码一并删除——
# 生产主流程 prepare 直连沙箱池/manager（run_command），run_code 仅语言镜像 502 的 Jupyter 遗留。


@requires_code_interpreter
def test_7_error_and_edge_cases():
    """测试 7: 错误处理和极端场景"""
    separator("TEST 7: 错误处理与极端场景")

    from swarm.worker.sandbox import SandboxManager

    manager = SandboxManager()
    sandbox = manager.create()

    try:
        # 语法错误
        print("  [7.1] 语法错误处理...")
        r = manager.run_code(sandbox, "def broken(")
        print(f"  success: {r.success}, error: {r.error[:100] if r.error else 'None'}")
        assert not r.success

        # 运行时错误
        print("  [7.2] 运行时错误处理...")
        r = manager.run_code(sandbox, "1/0")
        print(f"  success: {r.success}, error: {r.error[:100] if r.error else 'None'}")
        assert not r.success

        # 大量输出
        print("  [7.3] 大量输出...")
        r = manager.run_code(sandbox, "for i in range(100): print(f'line {i}')")
        line_count = len(r.stdout.split('\n'))
        print(f"  输出行数: {line_count}")
        assert line_count >= 100

        # 长时间运行（5秒）
        print("  [7.4] 长时间运行（5秒）...")
        start = time.time()
        r = manager.run_code(sandbox, "import time; time.sleep(5); print('done')", timeout=15)
        elapsed = time.time() - start
        print(f"  耗时: {elapsed:.1f}s, success: {r.success}")
        assert r.success
        assert "done" in r.stdout
        assert elapsed < 20  # 给一些缓冲

        # Unicode
        print("  [7.5] Unicode 输出...")
        r = manager.run_code(sandbox, "print('中文测试 🐝 你好世界')")
        print(f"  stdout: {r.stdout}")
        assert "中文测试" in r.stdout
        assert "🐝" in r.stdout

        # 多沙箱并存
        print("  [7.6] 多沙箱并存...")
        sandbox2 = manager.create()
        r1 = manager.run_code(sandbox, "print('sandbox1')")
        r2 = manager.run_code(sandbox2, "print('sandbox2')")
        print(f"  sandbox1: {r1.stdout}")
        print(f"  sandbox2: {r2.stdout}")
        assert "sandbox1" in r1.stdout
        assert "sandbox2" in r2.stdout
        manager.kill(sandbox2.sandbox_id)

        # kill_all
        print("  [7.7] kill_all 清理...")
        manager.kill_all()
        assert manager.active_count == 0
        print("  ✅ kill_all 成功")

        print("  ✅ TEST 7 PASSED")
    except Exception:
        manager.kill_all()
        raise


def test_8_smoke_reimport():
    """测试 8: 所有模块 re-import 一致性"""
    separator("TEST 8: 全模块 re-import")

    modules_ok = True
    for name in [
        "swarm.types", "swarm.config", "swarm.models",
        "swarm.tools", "swarm.worker", "swarm.brain",
        "swarm.knowledge", "swarm.memory",
    ]:
        try:
            __import__(name)
            print(f"  ✅ {name}")
        except Exception as e:
            print(f"  ❌ {name}: {e}")
            modules_ok = False

    # 专门验证 sandbox 相关导出
    from swarm.worker import CodeResult, SandboxConfig, SandboxManager
    print(f"  ✅ SandboxManager: {SandboxManager}")
    print(f"  ✅ SandboxConfig:  {SandboxConfig}")
    print(f"  ✅ CodeResult:     {CodeResult}")
    # SandboxPool 已移除(P2 死桩清理)，真正生效的是 HotSandboxPool(worker/sandbox_pool.py)

    from swarm.tools.build_tools import (
        clear_sandbox_context,
        get_sandbox_context,
        set_sandbox_context,
    )
    print(f"  ✅ set_sandbox_context:   {set_sandbox_context}")
    print(f"  ✅ get_sandbox_context:   {get_sandbox_context}")
    print(f"  ✅ clear_sandbox_context: {clear_sandbox_context}")

    assert modules_ok, "Some modules failed to import"
    print("  ✅ TEST 8 PASSED")


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main():
    print("\n" + "🐝" * 20)
    print("  Swarm 远程沙箱 (CubeSandbox) 集成测试")
    print("  目标: 192.168.60.106:3000")
    print("🐝" * 20)

    tests = [
        ("SandboxManager 基础生命周期", test_1_sandbox_manager_basic),
        ("CodeResult 解析", test_2_code_result_parsing),
        ("远程文件操作", test_3_remote_file_operations),
        ("远程进程执行", test_4_sandbox_process_execution),
        ("build_tools 沙箱模式", test_5_build_tools_sandbox_mode),
        ("错误处理与极端场景", test_7_error_and_edge_cases),
        ("全模块 re-import", test_8_smoke_reimport),
    ]

    results: list[tuple[str, bool, str]] = []

    for name, test_fn in tests:
        start = time.time()
        try:
            test_fn()
            elapsed = time.time() - start
            results.append((name, True, f"{elapsed:.1f}s"))
        except Exception as e:
            elapsed = time.time() - start
            tb = traceback.format_exc()
            results.append((name, False, f"{elapsed:.1f}s | {str(e)[:200]}\n{tb[-500:]}"))

    # ── 汇总 ──
    print("\n" + "=" * 60)
    print("  📊 测试结果汇总")
    print("=" * 60)

    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)

    for name, ok, detail in results:
        icon = "✅" if ok else "❌"
        print(f"  {icon} {name} ({detail})")

    print(f"\n  总计: {len(results)} 项 | 通过: {passed} | 失败: {failed}")

    if failed == 0:
        print("\n  🎉 全部通过！远程沙箱集成验证完成。")
    else:
        print(f"\n  ⚠️ {failed} 项失败，需修复。")
        sys.exit(1)


if __name__ == "__main__":
    main()
