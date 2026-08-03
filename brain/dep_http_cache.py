"""依赖坐标解析的 HTTP 文本缓存策略：成功永久缓存，失败（`None`）短 TTL 负缓存。

★为什么负缓存要有 TTL，而不是二选一（P-C2 复核 F-1 + F-3，两条互为张力）★

两个方向的后果都真实存在，必须**同时**防住：

  · **永久缓存 `None`**（go/npm/maven `_http_get` 的原实现）⇒ 一次网络抖动就把该坐标
    永久钉成"查不到"，网络恢复后再不重试。实测：模拟抖动 2 次后网络完全恢复，
    第二次询问 `累计网络调用=2`（一次网都没发）⇒ `resolve_go_deps` 照旧 `dropped`。
    最坏的地方是它**和"registry 真没这个包"长得一模一样**：同一条 WARNING 措辞
    （"无法确定性解析版本"）、同一条 `dropped` 路径，没有任何键区分"三小时前抖过一次"。
    npm 侧更隐蔽——fail-open 是设计好的正确行为，日志逐字相同，于是幻觉版本免检通过。

  · **完全不缓存 `None`**（F5 对 `_probe_cache` 的做法，照搬到这里会出事）⇒
    `resolve_*_deps` 是 **per-module** 调用（`_inject_go_scaffolds` 的 `for entry in mods_all`），
    `seen` 去重只在单次调用内生效；叠上 F1 把"首个镜像不可达即早返"改成"两个镜像都问"，
    代价 = 8s 超时 × 镜像数 × 模块数 × 依赖数，纯等待可达分钟级。而它不报错、不改结论，
    只表现为"规划偶尔特别慢"，归因会落到 LLM 或 DB 上。

TTL 负缓存两头都收：抖动后 TTL 一过自然重试（不再永久误杀），TTL 内不重复烧网络（不放大代价）。

★为什么不做成类★ 三个 registry 的 `_http_cache` 是模块级 plain dict，全仓 26 处测试用
`.clear()` 直接操作它，还有一处（`test_pc2_explicit_version_is_a_claim.py:493-497`）直接
写入 `None` 并断言其值——那是"两个缓存不得混用"的承重夹具。换成类会一次废掉这些契约，
所以这里只提供**作用于两个 dict 的纯函数**：值形状保持 `str | None` 不变，TTL 另存平行 dict。

★诚实边界★ 手工塞进 `cache[k] = None` 而没有对应 TTL 记录的条目（测试夹具的写法）一律
按"已过期"处理 ⇒ 重新联网核验。方向是"宁可多验一次，不要凭一个来历不明的 None 误杀"。
"""
from __future__ import annotations

import os
import time

# 负缓存存活秒数。取 60s 的理由：够短，让一次抖动的影响在同一次规划内就能自愈；够长，
# 覆盖单次 plan 里 per-module 反复问同一坐标的窗口（那正是 F-3 的代价来源）。
NEG_TTL_S = float(os.getenv("SWARM_DEP_LOOKUP_NEG_TTL_S", "60"))


def text_cache_lookup(
    cache: dict[str, str | None],
    neg_until: dict[str, float],
    key: str,
) -> tuple[bool, str | None]:
    """查缓存。返回 `(是否命中, 值)`。

    成功文本永久命中；`None` 仅在 TTL 内算命中，过期/无 TTL 记录 → **不命中**（重新联网）。
    过期条目就地清掉，免得两个 dict 无界增长。
    """
    if key not in cache:
        return False, None
    val = cache[key]
    if val is not None:
        return True, val
    if neg_until.get(key, 0.0) > time.monotonic():
        return True, None          # 负缓存仍在有效期内：不重复烧网络（F-3）
    cache.pop(key, None)           # 过期（或来历不明）的 None：丢掉，让调用方重新核验（F-1）
    neg_until.pop(key, None)
    return False, None


def text_cache_store(
    cache: dict[str, str | None],
    neg_until: dict[str, float],
    key: str,
    value: str | None,
) -> None:
    """写缓存。成功文本永久保留；`None` 记一条 TTL 到期时刻。"""
    cache[key] = value
    if value is None:
        neg_until[key] = time.monotonic() + NEG_TTL_S
    else:
        neg_until.pop(key, None)   # 成功即清掉旧的负记录，避免陈旧 TTL 影响后续判定
