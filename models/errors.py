"""失败分类 —— 区分"瞬时基础设施抖动" vs "模型能力/任务问题"的单一事实源。

背景（task 37460a5b）：worker 失败有两类，过去混在一条重试阶梯里共享配额：
  - transient（瞬时）：连接错误 / 5xx / 超时 / 限流 —— 基础设施抖动，退避后重试大概率自愈。
  - capability（能力/任务）：模型拒答 / 空 diff / scope 违规 / 编译失败 —— 换模型或换策略才有用。

37460a5b 的 st-1-1 在 02:01-02:02 连撞两次 Connection error（各 0.8s，零退避），白白
烧掉 2 次重试配额直接 escalate。正解：transient 走带退避的轻量重试且【不计入】capability
配额；capability 才走"retry → 换模型 → 升级"阶梯。

classify_failure 用 isinstance（优先）+ 字符串兜底（langchain/fallback 链路有时把 openai
异常包成普通 Exception，只剩 message）双保险，避免漏判。
"""

from __future__ import annotations

TRANSIENT = "transient"
CAPABILITY = "capability"


class TransientInfraError(Exception):
    """基础设施瞬时失败（沙箱上传/拉回失败、网络抖动等）。

    N-06/N-07：这类失败若被吞掉，会让"成功执行但同步失败"伪装成空 diff/能力失败，
    错误触发换模型降级。显式抛此异常 → classify_failure 归类 TRANSIENT →
    handle_failure 走退避重试【同模型】（基础设施抖动退避后大概率自愈），不浪费 capability 配额。
    """


# 拒答/截断标记（模型能力问题，非基础设施）——与 executor._REFUSAL_MARKERS 对齐。
_REFUSAL_MARKERS = (
    "sorry, need more steps",
    "need more steps to process",
    "i cannot",
    "i can't",
    "i'm unable",
)

# 瞬时错误的字符串特征（兜底，当 isinstance 失效时用）。
_TRANSIENT_MARKERS = (
    "connection error",
    "connection aborted",
    "connection reset",
    "internal server error",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "timed out",
    "timeout",
    "rate limit",
    "too many requests",
    "temporarily unavailable",
    "econnreset",
    "503",
    "502",
    "504",
)


def _is_transient_instance(exc: BaseException) -> bool:
    """用 isinstance 识别 openai 瞬时异常类（优先，最可靠）。"""
    try:
        from openai import (
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
            RateLimitError,
        )
    except Exception:  # noqa: BLE001 — openai 不可用时退字符串兜底
        return False
    return isinstance(
        exc, (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError)
    )


def classify_failure(err: BaseException | str | None) -> str | None:
    """把失败归类为 transient / capability / None（无法判定）。

    Args:
        err: 异常对象 或 失败文本（如 worker 的 summary / L1 raw_result）。
    Returns:
        "transient" | "capability" | None
    """
    if err is None:
        return None

    # 1) 异常对象：isinstance 优先
    if isinstance(err, BaseException):
        if isinstance(err, TransientInfraError) or _is_transient_instance(err):
            return TRANSIENT
        text = str(err)
    else:
        text = str(err)

    low = text.lower()

    # 2) 拒答/截断 → capability（必须先于 transient 判，避免 "timeout" 等词误伤）
    if any(m in low for m in _REFUSAL_MARKERS):
        return CAPABILITY

    # 2.5) 上下文超限 400 → capability（同模型重试必再超限，须换大窗口模型/收窄输入）。
    #      实测：Qwen3.5-122B(65536) 改大文件时输入+输出超限报 "maximum context length"。
    if any(
        m in low
        for m in ("maximum context length", "context length", "context_length_exceeded",
                  "too many tokens", "reduce the length", "上下文长度", "token 总数超")
    ):
        return CAPABILITY

    # 3) 瞬时特征字符串兜底
    if any(m in low for m in _TRANSIENT_MARKERS):
        return TRANSIENT

    # 4) 空 diff / scope 违规 / 编译失败 → capability
    if any(
        m in low
        for m in ("empty_diff", "empty diff", "scope_violation", "scope 违规", "compile", "编译失败")
    ):
        return CAPABILITY

    return None


def backoff_seconds(attempt: int, *, base: float = 2.0, cap: float = 8.0) -> float:
    """transient 重试的指数退避秒数：attempt 从 1 起 → 2s, 4s, 8s（上限 cap）。"""
    if attempt < 1:
        attempt = 1
    return min(base * (2 ** (attempt - 1)), cap)
