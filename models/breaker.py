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
  SWARM_BREAKER_COOLDOWN_S（冷却秒，默认 120；也是半开探针悬挂 TTL）

★作用域（阶段2 复核 F-F）：当前仅 brain 规划面接线（_invoke_llm_abortable 三个调用点：
分批规划/单发规划/对抗复核）。worker 面走 with_fallbacks 不经此处——健康信号只反映
规划流量，snapshot() 不含 worker 证据；若将来接观测面板，须区分"验证过健康"与
"近期无证据"（worker 接线留给阶段4 时间账单遍化统一做）。★
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
    __slots__ = ("consecutive_failures", "opened_at", "probing", "probe_started_at",
                 "recent")

    def __init__(self) -> None:
        self.consecutive_failures = 0
        # ★E-H2：滑窗成败序列 [(ts, ok)]——"连续"在并发下不成立★
        self.recent: list[tuple[float, bool]] = []
        self.opened_at: float | None = None  # None=closed
        self.probing = False  # half-open 探针在飞
        self.probe_started_at = 0.0  # 探针放行时刻（TTL 自愈用）


_lock = threading.Lock()
_states: dict[str, _BState] = {}


# 滑窗容量上限——防病理高频调用把列表撑爆（窗口按时间裁剪，这里是内存兜底）。
_WINDOW_MAX = 200


def _prune_window(st: "_BState", now: float) -> None:
    """裁掉窗口外（早于一个冷却期）的成败记录 + 容量兜底。"""
    _lo = now - _cooldown_s()
    st.recent = [r for r in st.recent if r[0] >= _lo][-_WINDOW_MAX:]


def _window_counts(st: "_BState") -> tuple[int, int]:
    """窗口内 (失败数, 成功数)。"""
    _f = sum(1 for _, ok in st.recent if not ok)
    return _f, len(st.recent) - _f


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
        now = time.monotonic()
        if now - st.opened_at < _cooldown_s():
            return False
        if st.probing:
            # 阶段2 复核 F-A（CRITICAL）：探针生命周期只靠 record_success/record_failure
            # 归还——调用方在非超时类异常（CancelledError 兄弟取消/TaskTokenLimitExceeded/
            # API 错误）下两者都不触发，probing 裸 bool 无人清=该模型静默永久禁用。
            # 纵深防御：探针悬挂超过一个冷却期 → TTL 自愈，重新放行（调用侧另有归还，见
            # _invoke_llm_abortable 的 release_probe；此处兜调用侧失职/未来新调用面）。
            if now - st.probe_started_at < _cooldown_s():
                return False  # 半开只放一个探针
            logger.warning(
                "[breaker] 模型 %s 半开探针悬挂超冷却期（%.0fs）未归还——TTL 自愈重新放行"
                "（探针方大概率被取消/异常中断）", key, _cooldown_s())
        st.probing = True
        st.probe_started_at = now
        logger.info("[breaker] 模型 %s 冷却期满 → half-open 放行探针", key)
        return True


def is_open(key: str) -> bool:
    """只读探询：open 且冷却未满 → True（【不】预约探针）。

    F-F（阶段4，登记册 §三/§四）：worker 面 with_fallbacks 链组装用——open 中的模型排到
    链尾（不删除：全 open 时仍按序尝试），健康 fallback 先上。half-open 探针语义仍归
    allow()（brain 面 _invoke_llm_abortable 专用），此处绝不占探针名额。"""
    if not key or _threshold() <= 0:
        return False
    with _lock:
        st = _states.get(key)
        if st is None or st.opened_at is None:
            return False
        return (time.monotonic() - st.opened_at) < _cooldown_s()


def record_success(key: str) -> None:
    if not key:
        return
    with _lock:
        st = _states.get(key)
        if st is None:
            # ★成功也必须建状态（E-H2）★：否则窗口里只记得下失败——首次成功被丢弃会让
            # 交替成败序列算成"失败多一个"，50/50 的模型被误熔断（实测踩到）。
            # 状态本身只是几个计数，按模型名有界。
            st = _BState()
            _states[key] = st
        if st.opened_at is not None and not st.probing:
            # 对称面（E-H1）：熔断前在飞的调用成功返回，不代表模型已恢复——
            # "一次早发成功即提前复位"会让刚熔断的死模型立刻被重新打满。
            # 只清失败计数（它确实证明了那一刻还有成功），不动 open 状态。
            logger.info(
                "[breaker] 模型 %s 熔断期内收到【熔断前在飞】调用的成功回报"
                "（非半开探针）→ 不提前复位，冷却照走", key)
            st.consecutive_failures = 0
            _sn = time.monotonic()
            _prune_window(st, _sn)
            st.recent.append((_sn, True))
            return
        if st.opened_at is not None:
            logger.info("[breaker] 模型 %s 探针成功 → 熔断闭合复位", key)
        st.consecutive_failures = 0
        _now = time.monotonic()
        _prune_window(st, _now)
        st.recent.append((_now, True))     # E-H2：成功也进窗口，否则窗口里只剩失败
        st.opened_at = None
        st.probing = False


def release_probe(key: str) -> None:
    """归还探针但【不计成败】（阶段2 复核 F-A）：探针调用被取消/预算耗尽/非超时类异常
    中断时，结果未知——既不该 record_failure（把取消当模型失败会误延冷却），更不能不管
    （probing 卡 True=永久禁用）。归还后 opened_at 不动：冷却已满则下一次 allow() 立即
    放行新探针。closed 态/无状态时为无害 no-op（调用方在任意异常路径统一调用）。"""
    if not key:
        return
    with _lock:
        st = _states.get(key)
        if st is None or not st.probing:
            return
        st.probing = False
        logger.info("[breaker] 模型 %s 探针中断（取消/非超时类异常）→ 归还探针不计成败", key)


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
            # ★只有【真拿到探针名额】的调用才算探针结果（26 号文 E-H1）★
            # 原判据只看 `opened_at is not None`，于是**熔断之前就已在飞**的并发调用
            # （straggler）失败落到这里，被当成"探针失败"：① 冷却被无限续期
            # （每个 straggler 都重置 opened_at）；② 日志谎报"探针失败 → 重新熔断"，
            # 而探针**从未被放行**。生产日志实证：open 那行之后紧跟"探针失败"，中间没有
            # "冷却期满→放行探针"——round67m 复盘把 k3 事故压缩成"三连超时"很可能就是被
            # 这条假日志误导的。
            if not st.probing:
                logger.info(
                    "[breaker] 模型 %s 熔断期内收到【熔断前在飞】调用的失败回报"
                    "（非半开探针，未持探针名额）→ 不重计冷却、不改判（straggler）", key)
                return
            # half-open 探针失败 → 重新 open 冷却重计
            st.opened_at = time.monotonic()
            st.probing = False
            logger.warning("[breaker] 模型 %s 探针失败 → 重新熔断（冷却 %.0fs）",
                           key, _cooldown_s())
            return
        st.consecutive_failures += 1
        _now = time.monotonic()
        _prune_window(st, _now)
        st.recent.append((_now, False))
        _fails, _oks = _window_counts(st)
        # ★判据从"严格连续"改成"滑窗内失败占优"（26 号文 E-H2）★
        # `consecutive_failures` 是**进程级**计数，而调用是并发交织的：实测 12 次失败 +
        # 6 次成功交错到达 → 每次成功都把计数清零 → `consecutive_failures=0` **永不熔断**；
        # 反向地，一次饱和里 3 个并发 stall 连着回来就立刻 open。**既误伤又漏检**，
        # 且因为状态是进程级的，跨任务还会互相影响。
        # 新判据：窗口（=一个冷却期）内失败数达阈值 **且** 失败多于成功才熔断——
        # 交织场景下失败真的占优时才断，偶发并发抖动不误断。
        # 旧计数保留：它仍是"连着挂"的强信号，任一成立即熔断（不削弱既有防护）。
        # ★滑窗判据需要【足够样本】才生效（否则把误伤换个方向再犯一遍）★
        # 3 次失败 + 1 次成功也满足"失败占优"，但那是小样本噪声——既有语义"一次成功
        # 证明模型还活着"在这个规模上是对的（test_success_resets_breaker_counter 编码的
        # 正是它）。样本不足时退回严格连续判据（保守），够了才用比例判据抓交织场景。
        _enough = len(st.recent) >= 2 * _threshold()
        if st.consecutive_failures >= _threshold() or (
                _enough and _fails >= _threshold() and _fails > _oks):
            st.opened_at = _now
            st.probing = False
            st.recent.clear()
            logger.warning(
                "[breaker] 模型 %s 超时/stall 熔断开启（连续 %d 次；近 %.0fs 窗口内 "
                "%d 失败 / %d 成功）→ 冷却 %.0fs 内直接走备，不再对死模型烧墙钟全款",
                key, st.consecutive_failures, _cooldown_s(), _fails, _oks, _cooldown_s())


def snapshot() -> dict:
    """观测用：key → {failures, open, probing}。"""
    with _lock:
        return {
            k: {"consecutive_failures": s.consecutive_failures,
                "open": s.opened_at is not None, "probing": s.probing}
            for k, s in _states.items()
        }
