"""进程级模型熔断记忆（深读登记册 2026-07-09 §三 B3，阶段2.2）。

病理：router 无跨调用健康状态——primary 已知死掉（饱和/宕），每次调用仍先对它烧满
墙钟全款（300s×input 计费），再切备。§九 B)："熔断记忆：连续 k 次超时/stall 的端点
进冷却，期内直接走备——省掉每次 300s×全款的撞墙钱。"

设计（用户拍板口径：云端商业 provider 同端点换模型可信 → 键=模型名而非端点）：
  - 按 key（模型名）记连续失败数；达阈值（默认 3）→ open，冷却期（默认 120s）内
    allow()=False（调用方直接走备）。
  - 冷却期满 → half-open：放行【一个】探针调用；探针成功 → closed 复位；
    探针失败 → 重新 open（冷却重计）。
  - 成功任意一次 → 连续失败清零。
  - 仅对"有备可走"的调用方生效（无备时 allow 恒 True——熔断不能把唯一出路也关了，
    fail-open 对称性）。该判断由调用方负责（breaker 本身只答健康状态）。

进程级内存态（重启清零=自然半开）；线程安全；env 可调：
  SWARM_BREAKER_THRESHOLD（连续失败阈值，默认 3；0=禁用熔断）
  SWARM_BREAKER_COOLDOWN_S（冷却秒，默认 120）
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)


def _threshold() -> int:
    try:
        v = int(os.environ.get("SWARM_BREAKER_THRESHOLD", "3") or "3")
        return max(0, v)
    except ValueError:
        return 3


def _cooldown_s() -> float:
    try:
        v = float(os.environ.get("SWARM_BREAKER_COOLDOWN_S", "120") or "120")
        return v if v > 0 else 120.0
    except ValueError:
        return 120.0


class _BState:
    __slots__ = ("consecutive_failures", "opened_at", "probing")

    def __init__(self) -> None:
        self.consecutive_failures = 0
        self.opened_at: float | None = None  # None=closed
        self.probing = False  # half-open 探针在飞


_lock = threading.Lock()
_states: dict[str, _BState] = {}


def _reset_for_tests() -> None:
    with _lock:
        _states.clear()


def allow(key: str) -> bool:
    """该模型当前是否可发起调用。closed=True；open 冷却内=False；
    冷却满=half-open 放行一个探针（并发到达的其余调用仍 False）。"""
    if not key or _threshold() <= 0:
        return True
    with _lock:
        st = _states.get(key)
        if st is None or st.opened_at is None:
            return True
        if time.monotonic() - st.opened_at < _cooldown_s():
            return False
        if st.probing:
            return False  # 半开只放一个探针
        st.probing = True
        logger.info("[breaker] 模型 %s 冷却期满 → half-open 放行探针", key)
        return True


def record_success(key: str) -> None:
    if not key:
        return
    with _lock:
        st = _states.get(key)
        if st is None:
            return
        if st.opened_at is not None:
            logger.info("[breaker] 模型 %s 探针成功 → 熔断闭合复位", key)
        st.consecutive_failures = 0
        st.opened_at = None
        st.probing = False


def record_failure(key: str) -> None:
    """记一次超时/stall 类失败（transient 基建形态；capability 失败不该喂这里）。"""
    if not key or _threshold() <= 0:
        return
    with _lock:
        st = _states.get(key)
        if st is None:
            st = _BState()
            _states[key] = st
        if st.opened_at is not None:
            # half-open 探针失败 → 重新 open 冷却重计
            st.opened_at = time.monotonic()
            st.probing = False
            logger.warning("[breaker] 模型 %s 探针失败 → 重新熔断（冷却 %.0fs）",
                           key, _cooldown_s())
            return
        st.consecutive_failures += 1
        if st.consecutive_failures >= _threshold():
            st.opened_at = time.monotonic()
            st.probing = False
            logger.warning(
                "[breaker] 模型 %s 连续 %d 次超时/stall → 熔断开启（冷却 %.0fs 内直接走备，"
                "不再对死模型烧墙钟全款）", key, st.consecutive_failures, _cooldown_s())


def snapshot() -> dict:
    """观测用：key → {failures, open, probing}。"""
    with _lock:
        return {
            k: {"consecutive_failures": s.consecutive_failures,
                "open": s.opened_at is not None, "probing": s.probing}
            for k, s in _states.items()
        }
