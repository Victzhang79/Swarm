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


class TaskTokenLimitExceeded(Exception):
    """单任务 token 预算耗尽（§九 TaskLedger 单点闸/节点边界闸共用）。

    阶段1 自 brain/runner.py 迁入（runner re-export 保兼容）：ledger 在 models 层
    预留失败时抛出，不能反向 import brain。usage dict 带归因：task_id/total/
    limit_effective/reserved，阶段烧穿另带 stage/stage_spent/stage_limit。
    runner 捕获后走 salvage→PARTIAL（E5 语义接口）。
    """

    def __init__(self, usage: dict):
        self.usage = usage or {}
        super().__init__(f"token limit exceeded: {self.usage.get('total')}")


class TransientInfraError(Exception):
    """基础设施瞬时失败（沙箱上传/拉回失败、网络抖动等）。

    N-06/N-07：这类失败若被吞掉，会让"成功执行但同步失败"伪装成空 diff/能力失败，
    错误触发换模型降级。显式抛此异常 → classify_failure 归类 TRANSIENT →
    handle_failure 走退避重试【同模型】（基础设施抖动退避后大概率自愈），不浪费 capability 配额。
    """


class StreamDegenerationError(Exception):
    """R63-T7：流式输出复读退化（同标识符/句在滑窗内高密度重复，见 models/degeneration.py）。

    与 TransientInfraError 语义相反：这是【模型能力问题】——同一模型同一上下文
    大概率复现（round63 st-2-1-1-2 跨 4 次重启反复复读），退避重试同模型只会白烧。
    classify_failure 按 isinstance 归 CAPABILITY → 非链尾由 with_fallbacks 同请求内
    切下一模型；链尾冒泡为子任务失败 + l1_decision_source=degeneration_hard_fail →
    brain FINDING-12 通路 force_strong 升最强模型重派。

    铁律：message 首行绝不含 timeout/stall/connect 等 transient 关键词——否则
    _breaker_error_transient 会把它喂进熔断（健康模型被摘）、字符串兜底分类会误归
    transient。复读证据（可能含任意 token）只放 evidence 与次行。
    """

    def __init__(self, message: str, *, evidence: dict | None = None) -> None:
        self.evidence = dict(evidence or {})
        super().__init__(message)


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
    # B8（2026-07-09 登记册）：本系统自身错误文案大量中文（墙钟超时/看门狗/路由层），
    # 全英文 marker 匹配不到 → 误入 capability 阶梯烧配额换模型。补中文瞬时特征。
    "超时",
    "连接错误",
    "连接中断",
    "连接重置",
    "连接被重置",
    "服务不可用",
    "暂时不可用",
    "限流",
    "请求过多",
    "网关错误",
    # 复核 H4：不收"稍后重试/服务繁忙"——它们是叙述性客套话，free-form summary 兜底
    # 分类会把确定性 capability 失败误判 transient；只收具体故障特征词。
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
        # R63-T7：复读退化必须先于一切 transient 判据——它的 evidence/message 次行可能
        # 含任意复读 token（哪怕撞上 "timeout" 字样也绝不能归 transient 退避同模型）。
        if isinstance(err, StreamDegenerationError):
            return CAPABILITY
        # 阶段0 复核 H2（2026-07-09）：裸 TimeoutError/asyncio.TimeoutError 的 str() 为空，
        # 文本特征全绕过 → 最典型的超时形态被判 None。TimeoutError 天然是基建瞬时
        # （asyncio.TimeoutError/socket.timeout 自 3.10 起均为其别名/子类）。
        if isinstance(err, (TransientInfraError, TimeoutError)) or _is_transient_instance(err):
            return TRANSIENT
        text = str(err)
    else:
        text = str(err)

    low = text.lower()

    # 2) 拒答/截断 → capability（必须先于 transient 判，避免 "timeout" 等词误伤）
    if any(m in low for m in _REFUSAL_MARKERS):
        return CAPABILITY

    # 2.5) 上下文超限 400 → capability（同模型重试必再超限，须换大窗口模型/收窄输入）。
    #      实测：已下线 Qwen3.5-122B(65536) 改大文件时输入+输出超限报 "maximum context length"。
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
