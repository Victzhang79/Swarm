"""B-2b（30 号文批24）：告警节流原语。

病灶：热路径上的同一 WARNING 在故障期会【按周期/按子任务】反复打——PG 宕期
dispatch #77 水位读/回写与 runner /progress 轮询点成对洗版（批15 把它们从 debug
升 WARNING 方向是对的，hunter 评估「真实可用性事件，方向正确不阻塞」，但量级
不加节制=噪声洪泛淹真信号，噪声即静默）。

语义契约：
- 同一 key 距上次放行 < interval ⇒ `throttled()` 返回 None（本次不告警）；
- 否则放行，并返回【自上次放行以来被抑制的同类条数】（首次放行=0）——故障期
  保留周期性心跳，且条数不丢账（机读：`suppress_suffix` 片段）；
- key 必须是【站点常量字符串】（如 "dispatch.progress_write"），绝不携带
  task_id 等无界维度——`_STATE` 常驻内存按 key 累积，无界 key = 内存泄漏。
"""

from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
# key → [上次放行时刻(monotonic), 自上次放行以来被抑制的条数]
_STATE: dict[str, list] = {}

DEFAULT_INTERVAL_S = 60.0


def throttled(key: str, interval: float = DEFAULT_INTERVAL_S, *,
              _now: float | None = None) -> int | None:
    """告警闸门：返回 None=本次不告警；返回 int=告警，值为期间被抑制的同类条数。"""
    now = time.monotonic() if _now is None else _now
    with _LOCK:
        st = _STATE.get(key)
        if st is None or now - st[0] >= interval:
            suppressed = 0 if st is None else st[1]
            _STATE[key] = [now, 0]
            return suppressed
        st[1] += 1
        return None


def suppress_suffix(suppressed: int) -> str:
    """放行条尾部的机读片段：有抑制则带计数（条数不丢账），无则空串。"""
    return f"（告警节流：距上条同类已抑制 {suppressed} 条）" if suppressed > 0 else ""


def _reset() -> None:
    """测试隔离专用：清空全部节流状态（节流键跨用例常驻，生产代码绝不调用）。"""
    with _LOCK:
        _STATE.clear()
