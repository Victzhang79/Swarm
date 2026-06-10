"""工具输出智能压缩 — 借鉴 headroom 思路：提取关键信号行替代盲目硬截断。

设计动机
--------
原先 L1 pipeline 对 test/compile 输出统一做 ``[:1500]`` 硬截断。问题在于
pytest 等工具会把最关键的失败摘要（FAILED / Error / assert 行、错误回溯）
放在**输出末尾**，而冗长的收集日志、warnings、passed 进度条堆在前面。
盲目取前 1500 字符往往恰好丢掉真正有用的错误信息，迫使 LLM 在缺失关键
上下文的情况下修复，浪费修复轮次。

headroom 的核心洞察是"压缩而非截断"：识别内容里的高价值信号，优先保留，
丢弃可预测的噪声。本模块用确定性规则（无需 LLM、零额外延迟）实现这一思路：

1. 保留头部少量行（命令/环境上下文）
2. 全程提取命中关键信号正则的行（错误、失败、回溯、断言）
3. 保留尾部若干行（工具摘要通常在此）
4. 用占位标记被省略的行数，保证可追溯

对纯文本无明显信号时优雅回退到"头 + 尾"窗口截断。
"""

from __future__ import annotations

import re

# 关键信号正则 —— 命中行视为高价值，优先保留。
# 覆盖 pytest / unittest / ruff / tsc / eslint / 通用 traceback。
_SIGNAL_PATTERNS = [
    r"\bFAILED\b",
    r"\bERROR\b",
    r"\bError\b",
    r"\bFAIL\b",
    r"Traceback \(most recent call last\)",
    r"^\s*File \"",
    r"\bAssertionError\b",
    r"^\s*assert\b",
    r"\bException\b",
    r"error TS\d+",          # tsc
    r"error\[",              # rust/通用
    r"^E\s",                 # pytest 错误行前缀
    r"^E\d{3}",              # ruff error code (E9xx)
    r"^F\d{3}",              # ruff F4xx
    r"\bpassed\b.*\bfailed\b",  # pytest 摘要行
    r"\b\d+ failed\b",
    r"\b\d+ error",
    r"SyntaxError",
    r"IndentationError",
    r"ImportError",
    r"ModuleNotFoundError",
    r"NameError",
    r"TypeError",
    r"ValueError",
]

_SIGNAL_RE = re.compile("|".join(f"(?:{p})" for p in _SIGNAL_PATTERNS))


def compress_tool_output(
    text: str,
    *,
    max_chars: int = 1500,
    head_lines: int = 8,
    tail_lines: int = 12,
) -> str:
    """压缩工具输出，优先保留关键信号行。

    Args:
        text: 原始工具输出。
        max_chars: 压缩后目标上限（软约束，命中信号行优先）。
        head_lines: 头部保留行数。
        tail_lines: 尾部保留行数。

    Returns:
        压缩后的字符串。短输出原样返回；长输出按
        头部 + 信号行 + 尾部 组装，并用占位标记省略区间。
    """
    if not text:
        return text
    if len(text) <= max_chars:
        return text

    lines = text.splitlines()
    n = len(lines)

    # 极短行数但超长（单行巨串）—— 退化为头尾字符窗口。
    if n <= head_lines + tail_lines:
        head_chars = max_chars // 2
        tail_chars = max_chars - head_chars
        omitted = len(text) - head_chars - tail_chars
        return (
            text[:head_chars]
            + f"\n... [压缩省略 {omitted} 字符] ...\n"
            + text[-tail_chars:]
        )

    head_idx = set(range(min(head_lines, n)))
    tail_idx = set(range(max(0, n - tail_lines), n))
    signal_idx = {i for i, ln in enumerate(lines) if _SIGNAL_RE.search(ln)}

    keep_idx = head_idx | tail_idx | signal_idx

    # 组装：按原顺序输出，连续被省略的行折叠为一个占位标记。
    out: list[str] = []
    omitted_run = 0
    for i in range(n):
        if i in keep_idx:
            if omitted_run:
                out.append(f"... [省略 {omitted_run} 行] ...")
                omitted_run = 0
            out.append(lines[i])
        else:
            omitted_run += 1
    if omitted_run:
        out.append(f"... [省略 {omitted_run} 行] ...")

    result = "\n".join(out)

    # 信号行过多仍可能超限 —— 二次保护：保头 + 保尾字符窗口。
    if len(result) > max_chars * 2:
        head_chars = max_chars
        tail_chars = max_chars
        omitted = len(result) - head_chars - tail_chars
        result = (
            result[:head_chars]
            + f"\n... [二次压缩省略 {omitted} 字符] ...\n"
            + result[-tail_chars:]
        )

    return result
