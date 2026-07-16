"""R63-T9②：fix 轮 turn 连续性——上一轮产码 agent 对话的结构保持裁剪。

round63 实锤：fix 循环每轮 `_run_agent` 都是全新单条 human 消息（无对话累积），模型
看不到自己上一轮改了什么、也看不到当时的推理，只能靠 C7 记忆块（有损摘要）+ 重新
read_file 自救——同一 cannot find symbol 反复重探（st-8 撞 95 迭代）。治本＝把上一轮
产码对话（裁剪后）作为历史前缀延续进本轮 ainvoke，让确定性 build 错在【同一对话】
里回喂（register T9「把确定性 build 错回喂同一 turn」）。

裁剪必须保持 OpenAI 消息序列结构合法（栈无关，纯消息层）：
  · assistant(tool_calls) 与其 tool 结果必须成组保留/成组丢弃——拆散配对会被严格
    推理服务器直接拒；tool_call 的 args 绝不截断（截了 JSON 就废）；
  · 首消息必须是 human（部分服务器拒 AI 开头的序列）——裁掉后用占位 human 顶位；
  · 绝不原地修改传入消息（telemetry/上游还握着引用）——一律 model_copy 出副本。
预算超限从最旧组整组丢弃（最近的工具活动最相关），至少保留最后一组；仅剩一组仍
超预算（巨型 write_file args 不可截断）→ 放弃 carry 返回 None（宁缺勿滥，回退全新
单消息轮，兜底仍是 C7 记忆块）。
"""
from __future__ import annotations

import logging

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

logger = logging.getLogger("swarm.worker.turn_continuity")

_TRUNC_MARK = "\n…[R63-T9 已裁剪，原 {n} 字符]"
CARRY_STUB_TEXT = (
    "（历史对话前缀已按预算裁剪；以下是你此前在本子任务中工作记录的尾部，"
    "供延续参考——以最后一条消息的指示为准。）"
)


def _clip(msg: BaseMessage, keep: int) -> BaseMessage:
    """内容超长时返回截断副本；非 str 内容（多模态块）不动——宁缺勿滥。"""
    c = getattr(msg, "content", None)
    if isinstance(c, str) and len(c) > keep:
        return msg.model_copy(
            update={"content": c[:keep] + _TRUNC_MARK.format(n=len(c))})
    return msg


# 复核 R-MED（PLAUSIBLE）：reasoning 模型把整段思维链聚进 additional_kwargs——已核实
# langchain_openai 出站序列化【不会】回发这些键（只发 content/name/tool_calls），wire
# 上不超预算；但携带副本里剥掉它们让存量更小、预算账严格成立，且对未来会回发
# additional_kwargs 的序列化器免疫。纵深防御，剥的是副本，原件不动。
_BULK_KWARGS = ("reasoning_content", "reasoning")


def _strip_bulk_kwargs(msg: BaseMessage) -> BaseMessage:
    ak = getattr(msg, "additional_kwargs", None) or {}
    if any(k in ak for k in _BULK_KWARGS):
        return msg.model_copy(update={
            "additional_kwargs": {k: v for k, v in ak.items()
                                  if k not in _BULK_KWARGS}})
    return msg


def _msg_chars(msg: BaseMessage) -> int:
    """预算口径：content + tool_calls args（write_file 的 args 是整份文件内容，
    只数 content 会严重低估，round47 类幻觉毒化教训的对偶——账要真实）。

    已核实的不变量（T9 猎手 F4）：langchain_openai 序列化以 `.tool_calls` 优先
    （additional_kwargs 里的 provider 原始 tool_calls 仅在 .tool_calls 为空时才用），
    而本模块的输入全部来自 LangGraph 归一化解析后的消息——两边同源，账实相符。
    str(args) 是 json.dumps 的近似（引号/转义有偏差），预算是软上限非硬保证。"""
    c = getattr(msg, "content", "")
    n = len(c) if isinstance(c, str) else len(str(c))
    for tc in (getattr(msg, "tool_calls", None) or []):
        n += len(str(tc.get("args", "")))
    return n


def trim_carry_messages(
    messages: list | None,
    *,
    budget_chars: int = 24000,
    tool_keep_chars: int = 800,
    ai_keep_chars: int = 2400,
    human_keep_chars: int = 2400,
) -> list[BaseMessage] | None:
    """裁剪上一轮对话为可延续历史前缀。返回 None 表示放弃 carry（调用方回退
    全新单消息轮）；返回列表保证：首条 human、tool 配对完整、不含 System。"""
    if not messages:
        return None
    # System 剔除：agent 每次 ainvoke 自带 system prompt，历史里再夹一份会混淆。
    msgs = [m for m in messages
            if isinstance(m, BaseMessage) and not isinstance(m, SystemMessage)]
    clipped: list[BaseMessage] = []
    for m in msgs:
        if isinstance(m, ToolMessage):
            clipped.append(_clip(m, tool_keep_chars))
        elif isinstance(m, AIMessage):
            clipped.append(_strip_bulk_kwargs(_clip(m, ai_keep_chars)))
        elif isinstance(m, HumanMessage):
            clipped.append(_clip(m, human_keep_chars))
        else:
            clipped.append(m)

    # 分组：Human/AI 开新组；Tool 附到当前组；开头孤儿 tool（配不上 AI）丢弃。
    groups: list[list[BaseMessage]] = []
    for m in clipped:
        if isinstance(m, ToolMessage):
            if groups:
                groups[-1].append(m)
        else:
            groups.append([m])
    if not groups:
        return None

    def _total(gs: list[list[BaseMessage]]) -> int:
        return sum(_msg_chars(m) for g in gs for m in g)

    while len(groups) > 1 and _total(groups) > budget_chars:
        groups.pop(0)
    if _total(groups) > budget_chars:
        # 仅剩一组仍超预算：二次收紧 content（args 不可动），仍超 → 放弃。
        groups[0] = [_clip(m, max(120, tool_keep_chars // 4)) for m in groups[0]]
        if _total(groups) > budget_chars:
            logger.info(
                "R63-T9 turn 连续性：最新对话组超预算(%d chars > %d)且不可再裁"
                "（tool_call args 不可截断），本轮放弃 carry 回退全新单消息轮",
                _total(groups), budget_chars,
            )
            return None

    out = [m for g in groups for m in g]
    if not out:
        return None
    if not isinstance(out[0], HumanMessage):
        out.insert(0, HumanMessage(content=CARRY_STUB_TEXT))
    return out
