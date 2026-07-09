"""阶段7 批1（登记册 §八阶段7）：F3 配置面冻结行为锁。

113→206 个 SWARM_* 直读开关散布全仓，每轮 E2E 跑的是"从未整体验证的配置组合"。
冻结面：①代码里每个 SWARM_* 必须登记进 config/env_registry.py（新增不登记=红）；
②登记册不留死条目（双向）；③dev/e2e/prod 三 profile 存在且只引用已登记开关。
"""

from __future__ import annotations

import re
from pathlib import Path

from swarm.config.env_registry import REGISTERED_ENVS

_ROOT = Path(__file__).resolve().parent.parent
_SCAN_DIRS = ("brain", "worker", "models", "project", "api", "tools", "infra",
              "config", "experience")
_ENV_RE = re.compile(r"SWARM_[A-Z0-9_]+")


def _scan_code_envs() -> set[str]:
    found: set[str] = set()
    files = [p for d in _SCAN_DIRS for p in (_ROOT / d).rglob("*.py")]
    files += [_ROOT / "types.py", _ROOT / "audit.py"]
    for p in files:
        if not p.exists() or "env_registry" in p.name:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found.update(_ENV_RE.findall(text))
    return found


def test_f3_every_code_env_is_registered():
    missing = sorted(_scan_code_envs() - set(REGISTERED_ENVS))
    assert not missing, (
        f"新增 SWARM_* 开关未登记进 config/env_registry.py：{missing}——"
        "未登记开关=从未整体验证的配置组合的又一来源；登记一行（值=file:line）即可")


def test_f3_registry_has_no_stale_entries():
    stale = sorted(set(REGISTERED_ENVS) - _scan_code_envs())
    assert not stale, (
        f"登记册存在代码里已消失的死条目：{stale}——冻结面必须与代码双向同步")


def test_f3_three_profiles_exist_and_only_reference_registered():
    prof_dir = _ROOT / "config" / "profiles"
    for name in ("dev.env", "e2e.env", "prod.env"):
        p = prof_dir / name
        assert p.exists(), f"缺 profile：config/profiles/{name}（冻结的推荐配置组合）"
        unknown = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if key.startswith("SWARM_") and key not in REGISTERED_ENVS:
                unknown.append(key)
        assert not unknown, f"{name} 引用了未登记开关：{unknown}"


# ─────────────── F2：degraded 机读汇总 ───────────────


def test_f2_degraded_summary_by_prefix():
    from swarm.brain.runner import build_degraded_summary
    s = build_degraded_summary([
        "requirements_extract:rejected=3(...)",
        "requirements_extract:source_truncated",
        "acceptance_skipped:login_failed",
    ])
    assert s == {"requirements_extract": 2, "acceptance_skipped": 1}, (
        "E2E 判读脚本要一眼回答'这轮降级了什么、各多少次'——按机制前缀聚合")


def test_f2_result_payload_carries_summary_and_detail():
    from swarm.brain.runner import _build_result_payload
    out = _build_result_payload({
        "merged_diff": "+x\n",
        "degraded_reasons": ["a:1", "a:2", "b:x"],
    })
    assert out["degraded_summary"] == {"a": 2, "b": 1}
    assert out["degraded_reasons"] == ["a:1", "a:2", "b:x"], "明细照留（人工审读）"


def test_f2_no_degraded_no_keys():
    from swarm.brain.runner import _build_result_payload
    out = _build_result_payload({"merged_diff": "+x\n"})
    assert "degraded_summary" not in out and "degraded_reasons" not in out
