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
# ★C1-B：扫描面必须覆盖全部含 SWARM_* 的包★
# 26 号文复核实证：原表漏了 knowledge/ memory/ cli/ auth/ observability/ 与两个根级
# 模块，导致 **26 个开关逃过登记而测试全绿**，其中含 SWARM_INGEST_FEISHU_APP_SECRET /
# SWARM_INGEST_TENCENT_CLIENT_SECRET / SWARM_INGEST_TENCENT_ACCESS_TOKEN 三个凭据类
# ——"有强制测试"本身给了虚假的安全感，而闸的扫描面才是它的真实覆盖。
_SCAN_DIRS = ("brain", "worker", "models", "project", "api", "tools", "infra",
              "config", "experience", "knowledge", "memory", "cli", "auth",
              "observability")
_ENV_RE = re.compile(r"SWARM_[A-Z0-9_]+")
# 文案里的通配写法（`请检查 SWARM_KB_EMBED_* 配置`）会被正则截成 `SWARM_KB_EMBED_`——
# 真变量名不会以下划线结尾，据此排除，避免把假变量登记进册（C1-B 扩扫描面时暴露）。
_ENV_FALSE_POSITIVE = re.compile(r"_$")


def _scan_code_envs(*, skip_comments: bool = False) -> set[str]:
    found: set[str] = set()
    files = [p for d in _SCAN_DIRS for p in (_ROOT / d).rglob("*.py")]
    # ★根级模块整体纳入（复核 MEDIUM-8）★：原先逐个补根级文件（types.py/audit.py），
    # 而 tracing.py / logging_config.py 就漏在外面——实测 4 个真开关因此逃逸且测试全绿
    # （SWARM_BRAIN_RECURSION_LIMIT / SWARM_LANGSMITH_*_TIMEOUT_MS / SWARM_PER_TASK_LOGS）。
    # 逐个补是打地鼠，allowlist 才是根因：改成"根级 *.py 全扫"。
    files += list(_ROOT.glob("*.py"))
    for p in files:
        # 排除登记册【自身】（复核 LOW-1）：把它算进"代码扫描结果"会让反向 stale 检查
        # 自满足——任何写进册的键都能在册里被找到，于是"死条目"永不报警，双向同步实为单向。
        if not p.exists() or "env_registry" in p.name:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # ★两个方向的过滤【刻意不对称】★（本轮实证）：
        #   正向"有没有新开关没登记" → 注释里举例写的 `SWARM_X` 不是新开关，跳过注释；
        #     （本轮连踩两次：docstring 表格示例、行内注释示例）
        #   反向"登记的还在不在"     → 注释里提到就说明它还在（13 个既有条目的字面量本就
        #     只出现在注释里），跳过注释会把它们全误判成死条目。
        # 初版把两侧做成对称的，反向检查当场误报——对称是直觉，语义才是判据。
        code = ("\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
                if skip_comments else text)
        found.update(m for m in _ENV_RE.findall(code)
                     if not _ENV_FALSE_POSITIVE.search(m))
    return found


def test_f3_every_code_env_is_registered():
    missing = sorted(_scan_code_envs(skip_comments=True) - set(REGISTERED_ENVS))
    assert not missing, (
        f"新增 SWARM_* 开关未登记进 config/env_registry.py：{missing}——"
        "未登记开关=从未整体验证的配置组合的又一来源；登记一行（值=file:line）即可")


def test_f3_registry_has_no_stale_entries():
    # 登记册里以 `_` 结尾的条目是 **env_prefix 登记**（如 `SWARM_DB_` → settings.py 的
    # BaseSettings env_prefix），不是变量名；代码扫描侧已按同一规则排除（_ENV_FALSE_POSITIVE），
    # 反向检查必须对称排除，否则前缀条目会被误判成"代码里已消失"。
    registered = {k for k in REGISTERED_ENVS if not _ENV_FALSE_POSITIVE.search(k)}
    stale = sorted(registered - _scan_code_envs())
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
