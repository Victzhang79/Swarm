"""provider API key 多槽 + 配额感知轮换（单一事实源）。

## 病灶

`_resolve_api_key` 每个 provider 只认**一把** key：额度耗尽（429 / 配额类 403）后，
该 provider 的每一次调用都必然失败，直到人工换 key。而额度往往过几天自动恢复——
"换掉"这个动作既需要人守着，也丢掉了旧 key 恢复后的价值。

## 设计

- **多槽零迁移**：`provider_api_key:<pid>` 就是槽 1（既有命名不动），
  `provider_api_key:<pid>#2` / `#3` … 是备用槽。没配备用槽＝退化成今天的行为。
- **冷却而非淘汰**：某槽命中配额形态 → 进冷却（默认 6h，`SWARM_KEY_ROTATION_COOLDOWN_S`），
  自动切下一个可用槽；冷却期满自动回到候选池。**额度恢复后不需要任何人工动作。**
- **绝不静默无 key**：全部槽都在冷却时，返回**最早到期**的那把并打 WARNING
  （fail-open：有把过期概率的 key 也好过没有 key——后者是确定失败）。

## 与既有机制的分工（刻意不另造一套）

| 机制 | 管什么 | 粒度 |
|---|---|---|
| `models/breaker.py` | 模型**超时/stall** 类基建故障 | model 名 |
| `is_auth_shaped_error` | 凭据/权限**配置错**（401/403 无效 key）→ 升 ERROR 等人修 | 全流量 |
| 本模块 | **配额耗尽**（429 / 配额类 403）→ 换一把 key 继续 | provider × key 槽 |

三者判据互斥：配额形态先判（它有专属出路＝换 key），剩下的才落 auth/transient。
冷却语义与 breaker 同构（时间窗 + 自动回池），故障面观测统一走 `/api/metrics` 降级面。

栈中立、无 IO：本模块只做选槽与冷却记账，密钥读写仍归 `config.secret_store`。
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# ── 配额形态判据（与 is_auth_shaped_error 互斥：那条管"配置错"，这条管"额度没了"）──
# 只取【首行】+ 数字码词边界——纪律同 models/errors.py：供应商业务错误码
# （errcode=42900）、端口/trace id 裸子串会误判，误伤半径大（会白白把好 key 冷却掉）。
import re as _re

_QUOTA_CODE_RE = _re.compile(r"(?<![0-9])429(?![0-9])")
_QUOTA_MARKERS = (
    "insufficient_quota", "insufficient quota", "quota exceeded", "quota_exceeded",
    "exceeded your current quota", "rate limit", "rate_limit", "ratelimit",
    "too many requests", "billing", "insufficient balance", "余额不足", "配额",
    "credit", "out of credits",
)


def is_quota_shaped_error(exc: BaseException | str) -> bool:
    """额度/限流类错误判形——**有专属出路（换一把 key）**，故与 auth 类分开判。

    刻意包含 429 限流：限流与配额耗尽在"这把 key 现在用不了"这件事上是同一后果，
    换把 key 继续跑比原地退避更有价值；冷却期满自动回池，短时限流不会长期损失该槽。
    """
    first = (str(exc).splitlines() or [""])[0].lower()
    return bool(_QUOTA_CODE_RE.search(first)) or any(k in first for k in _QUOTA_MARKERS)


# ── 冷却台账（进程级内存态；重启清零＝自然恢复，与 breaker 同律）──
_lock = threading.Lock()
_cooling: dict[str, float] = {}        # "<pid>#<slot>" -> 冷却截止 monotonic
_rotations: dict[str, int] = {}        # "<pid>" -> 轮换次数（观测用）

_DEFAULT_COOLDOWN_S = 6 * 3600


def _cooldown_s() -> float:
    """冷却时长（秒）。默认 6h——额度通常按小时/天恢复，太短会反复撞墙、太长会白等。"""
    try:
        v = float(os.environ.get("SWARM_KEY_ROTATION_COOLDOWN_S", "") or _DEFAULT_COOLDOWN_S)
        return v if v > 0 else _DEFAULT_COOLDOWN_S
    except ValueError:
        return _DEFAULT_COOLDOWN_S


def _slot_key(provider_id: str, slot: int) -> str:
    return f"{provider_id}#{slot}"


def secret_name(provider_id: str, slot: int) -> str:
    """槽 → secret_store 键名。★槽 1 就是既有命名，零迁移★。"""
    return (f"provider_api_key:{provider_id}" if slot <= 1
            else f"provider_api_key:{provider_id}#{slot}")


def reset_for_tests() -> None:
    with _lock:
        _cooling.clear()
        _rotations.clear()


def mark_exhausted(provider_id: str, slot: int, reason: str = "") -> None:
    """把某槽标记为配额耗尽 → 进冷却。幂等（重复标记只刷新截止时间）。"""
    if not provider_id or slot <= 0:
        return
    with _lock:
        _cooling[_slot_key(provider_id, slot)] = time.monotonic() + _cooldown_s()
        _rotations[provider_id] = _rotations.get(provider_id, 0) + 1
    logger.warning(
        "[key-rotation] provider=%s 槽%d 命中配额形态 → 冷却 %.0fs 后自动回池，"
        "本次调用起改用下一个可用槽（额度恢复无需人工干预）: %s",
        provider_id, slot, _cooldown_s(), (reason or "")[:160])
    try:
        from swarm.infra.degrade import record_degrade
        record_degrade(f"models.key_rotation.exhausted:{provider_id}")
    except Exception:  # noqa: BLE001 — 观测绝不阻断
        pass


def _is_cooling(provider_id: str, slot: int, now: float) -> float:
    """返回该槽剩余冷却秒数（≤0 = 可用）。"""
    return _cooling.get(_slot_key(provider_id, slot), 0.0) - now


def select_slot(provider_id: str, available_slots: list[int]) -> int | None:
    """在可用槽里选一个：优先【不在冷却】的最小槽；全在冷却 → 最早到期的那个。

    ★绝不返回 None-when-有槽★：全冷却时也给一把（fail-open）——一把可能过期的 key
    好过没有 key，后者是**确定**失败；而"全都在冷却"这件事必须 WARNING 可见。
    """
    if not available_slots:
        return None
    now = time.monotonic()
    with _lock:
        usable = [s for s in sorted(available_slots) if _is_cooling(provider_id, s, now) <= 0]
        if usable:
            return usable[0]
        soonest = min(available_slots, key=lambda s: _cooling.get(_slot_key(provider_id, s), 0.0))
        remain = _cooling.get(_slot_key(provider_id, soonest), 0.0) - now
    logger.warning(
        "[key-rotation] provider=%s 全部 %d 个 key 槽都在冷却中 → 仍用最早到期的槽%d"
        "（剩余 %.0fs）：宁可试一把可能过期的，也不能没有 key",
        provider_id, len(available_slots), soonest, max(0.0, remain))
    return soonest


def snapshot() -> dict:
    """观测用：{cooling: {slot_key: 剩余秒}, rotations: {pid: 次数}}。"""
    now = time.monotonic()
    with _lock:
        return {
            "cooling": {k: round(max(0.0, v - now), 1) for k, v in _cooling.items()
                        if v - now > 0},
            "rotations": dict(_rotations),
        }
