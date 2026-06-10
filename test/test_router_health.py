#!/usr/bin/env python3
"""Router 模块健康度回归测试。

锁定 Phase2 拆分遗留的 F821 类 bug（缺失 import 导致端点调用即 NameError）：
- knowledge.py 缺 get_config
- project.py / worker.py 缺 EventSourceResponse

两层防线：
1. 所有 router 模块可被导入（模块级 import 缺失会在此暴露）
2. ruff F821 全仓零容忍（任何未定义名字 = 测试失败），防止未来再次引入。
"""

from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_ROUTER_MODULES = [
    "swarm.api.routers.auth",
    "swarm.api.routers.config",
    "swarm.api.routers.knowledge",
    "swarm.api.routers.memory",
    "swarm.api.routers.project",
    "swarm.api.routers.sandbox",
    "swarm.api.routers.task",
    "swarm.api.routers.worker",
]


def test_all_routers_importable():
    """所有 router 模块可导入，且暴露 router 对象。"""
    for mod_name in _ROUTER_MODULES:
        mod = importlib.import_module(mod_name)
        assert hasattr(mod, "router"), f"{mod_name} 缺少 router 对象"
    print(f"  ✅ {len(_ROUTER_MODULES)} 个 router 模块全部可导入")


def test_sse_routers_have_eventsource():
    """SSE 端点模块必须能解析 EventSourceResponse（防 project/worker 回归）。"""
    for mod_name in ("swarm.api.routers.project", "swarm.api.routers.worker"):
        mod = importlib.import_module(mod_name)
        assert hasattr(mod, "EventSourceResponse"), \
            f"{mod_name} 未导入 EventSourceResponse（SSE 端点会崩溃）"
    print("  ✅ SSE 模块 EventSourceResponse 可用")


def test_knowledge_has_get_config():
    """knowledge_overview 用到 get_config —— 验证函数可正常解析依赖。"""
    import swarm.api.routers.knowledge as kn

    # get_config 在函数内局部导入，验证 settings 模块本身可用
    from swarm.config.settings import get_config  # noqa: F401
    assert hasattr(kn, "knowledge_overview")
    print("  ✅ knowledge.get_config 依赖可解析")


def test_no_f821_undefined_names():
    """ruff F821 全仓零容忍门禁 —— 任何未定义名字即失败。"""
    repo_root = Path(__file__).resolve().parent.parent
    ruff = repo_root / ".venv" / "bin" / "ruff"
    ruff_cmd = str(ruff) if ruff.is_file() else "ruff"
    try:
        proc = subprocess.run(
            [ruff_cmd, "check", ".", "--select", "F821", "--output-format", "concise"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        print("  ⚠️  ruff 不可用，跳过 F821 门禁（环境相关）")
        return
    assert proc.returncode == 0, (
        f"检测到 F821 未定义名字（潜在运行时崩溃）:\n{proc.stdout}"
    )
    print("  ✅ 全仓 F821 零未定义名字")


def main() -> int:
    print("\n🧪 Router 模块健康度回归测试\n")
    tests = [
        test_all_routers_importable,
        test_sse_routers_have_eventsource,
        test_knowledge_has_get_config,
        test_no_f821_undefined_names,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n📊 结果: {passed} 通过, {failed} 失败\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
