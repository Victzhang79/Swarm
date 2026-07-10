#!/usr/bin/env python3
"""主题I（round38c）—— 外部深审 2 CRITICAL 安全批。

I-SEC-1：项目路径 alias 归一（唯一性靠 PG ON CONFLICT (path) 字符串比较，尾斜杠/
./ 段/symlink alias 可把同一物理目录注册成多项目=绕过 D16 多租户越权）。
I-SEC-2：沙箱启用但创建失败时默认 fail-closed 拒绝降级宿主机执行（LLM 任意命令
逃出隔离直接跑在 brain 宿主机=安全边界破坏）。
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ══════════════ I-SEC-1 ══════════════

def test_sec1_alias_forms_canonicalize_to_same_path(tmp_path):
    from swarm.api.routers.project import _canonicalize_project_path
    real = tmp_path / "proj"
    real.mkdir()
    canonical = _canonicalize_project_path(str(real))
    assert _canonicalize_project_path(str(real) + "/") == canonical, "尾斜杠 alias"
    assert _canonicalize_project_path(f"{tmp_path}/./proj") == canonical, "./ 段 alias"
    assert _canonicalize_project_path(f"{tmp_path}/x/../proj") == canonical, ".. 段 alias"
    link = tmp_path / "lnk"
    os.symlink(real, link)
    assert _canonicalize_project_path(str(link)) == canonical, (
        "symlink alias 必须归一——否则同一物理目录注册成多项目，绕过 D16 冲突检测"
        "与成员授权模型（多租户越权）")
    assert _canonicalize_project_path("") == "" and _canonicalize_project_path(None) == ""


# ══════════════ I-SEC-2 ══════════════

def _executor(tmp_path):
    from swarm.types import FileScope, SubTask
    from swarm.worker.executor import WorkerExecutor
    st = SubTask(id="st-sec", description="t",
                 scope=FileScope(writable=["a.py"], create_files=[]))
    return WorkerExecutor(st, project_path=str(tmp_path), project_id="p1", task_id="t1")


async def test_sec2_sandbox_create_failure_fail_closed(tmp_path, monkeypatch):
    """沙箱启用+创建失败+默认配置 → 必须抛错拒绝宿主机执行，绝不静默降级。"""
    from swarm.config.settings import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg.sandbox, "use_for_worker", True, raising=False)
    monkeypatch.setattr(cfg.sandbox, "api_url", "http://sandbox.invalid:9", raising=False)
    monkeypatch.setattr(cfg.sandbox, "allow_local_fallback", False, raising=False)

    import swarm.worker.sandbox as sbx
    def _boom():
        raise ValueError("simulated sandbox create failure (non-transient)")
    monkeypatch.setattr(sbx, "get_sandbox_manager", _boom)

    ex = _executor(tmp_path)
    ex.start_time = __import__("time").monotonic()
    with pytest.raises(RuntimeError, match="fail-closed|拒绝"):
        await ex._phase_prepare()


async def test_sec2_explicit_optin_preserves_local_fallback(tmp_path, monkeypatch):
    """显式 SWARM_SANDBOX_ALLOW_LOCAL_FALLBACK=true 才保留旧降级（单机开发场景）。"""
    from swarm.config.settings import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg.sandbox, "use_for_worker", True, raising=False)
    monkeypatch.setattr(cfg.sandbox, "api_url", "http://sandbox.invalid:9", raising=False)
    monkeypatch.setattr(cfg.sandbox, "allow_local_fallback", True, raising=False)

    import swarm.worker.sandbox as sbx
    def _boom():
        raise ValueError("simulated sandbox create failure")
    monkeypatch.setattr(sbx, "get_sandbox_manager", _boom)

    ex = _executor(tmp_path)
    ex.start_time = __import__("time").monotonic()
    out = await ex._phase_prepare()  # 不抛=降级本地继续（agent 创建等后续照旧）
    assert out is None or hasattr(out, "subtask_id")


def test_sec2_default_is_fail_closed():
    from swarm.config.settings import SandboxConfig
    assert SandboxConfig().allow_local_fallback is False, (
        "默认必须 fail-closed——静默降级=LLM 命令逃出沙箱隔离跑在宿主机")


if __name__ == "__main__":
    print("run via pytest")
