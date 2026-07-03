#!/usr/bin/env python3
"""#13 沙箱 git 无 HEAD → Rust cargo-fix 触达清单枚举【降级可观测】（round20 治本）回归测试。

排查结论（#13）：交付主链不依赖沙箱 git —— pull-back 按 scope 文件清单 + find 枚举，
交付 diff 在本地 project_path(真仓、有 HEAD)生成，scope 外修复经 repair 函数显式返回路径传播。
沙箱 /workspace 由 `git archive HEAD` 烤成【无 .git】→ 沙箱内 `git diff HEAD` 必失败（by design），
但这只影响 worker LLM agent 的信息性 bash 调用，不吞交付产物。

唯一真·静默降级点 = `_repair_rust` 用沙箱内 `git diff --name-only` 枚举 cargo fix 的 scope 外触达
文件；沙箱无 .git → 非 0 → 原先静默 touched=[]。本治本把该降级【可观测】（logger.warning），
不改交付路径。本套验证：① git 可用→触达清单正常；② git 不可用(非0)→touched=[] 且发 WARNING；
③ cargo 工具缺失→优雅跳过(0,[])，不误报 git 告警。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.worker import l1_pipeline as lp  # noqa: E402


def _patch_run(monkeypatch_pairs):
    """按命令子串路由的假 _run_l1_command。monkeypatch_pairs: list[(substr, (ec, out))]。"""
    def fake(cmd, project_path, timeout=0):  # noqa: ARG001
        for substr, ret in monkeypatch_pairs:
            if substr in cmd:
                return ret
        return (0, "")
    return fake


def test_rust_repair_git_available(monkeypatch):
    monkeypatch.setattr(lp, "_run_l1_command", _patch_run([
        ("cargo fix", (0, "Fixed 2 warnings in crate")),
        ("git diff --name-only", (0, "src/lib.rs\nCargo.lock\n")),
    ]))
    mark, touched = lp._repair_rust("/fake/proj", timeout=120)
    assert mark == 1
    assert touched == ["src/lib.rs", "Cargo.lock"]
    print("  ✅ ① git 可用 → cargo fix 触达清单正常回传（含 scope 外 Cargo.lock）")


def test_rust_repair_git_unavailable_observable(monkeypatch):
    monkeypatch.setattr(lp, "_run_l1_command", _patch_run([
        ("cargo fix", (0, "Fixed 1 warning in crate")),
        # 沙箱无 .git → git diff 报 not a git repository，非 0 退出
        ("git diff --name-only", (128, "fatal: not a git repository")),
    ]))
    # 直接监听模块 logger.warning，不依赖 caplog 的全局 propagation（避免测序脆弱）
    warnings: list[str] = []
    orig_warning = lp.logger.warning

    def _spy(msg, *args, **kw):
        try:
            warnings.append(msg % args if args else str(msg))
        except Exception:  # noqa: BLE001
            warnings.append(str(msg))
        return orig_warning(msg, *args, **kw)

    monkeypatch.setattr(lp.logger, "warning", _spy)
    mark, touched = lp._repair_rust("/fake/proj", timeout=120)
    assert mark == 1          # cargo fix 仍算已尝试修复
    assert touched == []      # 无法枚举 → 空（但不静默）
    assert any("触达清单枚举不可用" in w for w in warnings), \
        "git 不可用必须发 WARNING（降级可观测）"
    print("  ✅ ② git 不可用(沙箱无.git) → touched=[] 且发 WARNING（不静默丢弃）")


def test_rust_repair_cargo_missing_no_false_warning(monkeypatch):
    monkeypatch.setattr(lp, "_run_l1_command", _patch_run([
        ("cargo fix", (127, "cargo: command not found")),
    ]))
    warnings: list[str] = []
    monkeypatch.setattr(lp.logger, "warning",
                        lambda msg, *a, **k: warnings.append(str(msg)))
    mark, touched = lp._repair_rust("/fake/proj", timeout=120)
    assert (mark, touched) == (0, [])   # 工具缺失优雅跳过
    assert not any("触达清单枚举不可用" in w for w in warnings), \
        "cargo 缺失时不应触发 git 枚举告警"
    print("  ✅ ③ cargo 缺失 → 优雅跳过(0,[])，不误报 git 告警")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
