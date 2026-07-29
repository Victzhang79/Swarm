"""prompt 文本裁剪——**结构边界对齐 + 截断显式化**（26 号文 C-9 治本）。

## 病灶

规划期把需求原文喂给模型时到处是 `task_desc[:2000]` 这样的裸切片。实测（round67m2）：

  · STAGE1 拿到完整 ~12200 字符需求；决定"这个模块要建哪些文件"的 STAGE2 只拿到
    **前 2000 字符**——需求→设计第一跳丢约 85%。
  · 切点落在 markdown 表格中间：`| 渠道类型 | Slack / 企` 就是实测的结尾。模型收到的
    不是"被截短的需求"，而是**一份看起来完整、实则在表格中途断掉的畸形文档**——它无从
    知道后面还有内容，只能按残缺信息设计，且残行本身还是误导性证据。

后果不是"少看了点东西"：决定建哪些文件的节点看不到需求的 85%，下游每一道闸都只能对着
这份残缺设计做一致性检查——闸判得再准，也判不出"该建的东西压根没进设计"。

## 判据

裸切片的两个错都必须治，只治一个仍然错：

  ① **切在结构边界上**：优先段落（空行）→ 行 → 句末标点；绝不切在行中间。宁可比 limit
     少几十字，也不产出半行 markdown 表格 / 半个 JSON 片段。
  ② **截断必须显式告诉模型**：附上"原文共 N 字符，此处只展示前 M 字符"的尾注。模型知道
     自己看到的是节选，才会在设计里留出余量、才可能在 STAGE2 说"这个模块我信息不足"；
     不知道就只会自信地按残文设计。

栈中立：纯文本层面，不含任何语言/框架假设。
"""

from __future__ import annotations

# 段落 > 行 > 句末：从最强的结构边界依次回退。
# 只在"离 limit 不太远"（默认 30%）的范围内找边界——否则为了对齐边界丢掉大半内容，
# 得不偿失，此时宁可硬切并如实标注。
_BOUNDARY_SEARCH_RATIO = 0.30


def clip_for_prompt(
    text: str | None,
    limit: int,
    *,
    what: str = "原文",
    boundary_ratio: float = _BOUNDARY_SEARCH_RATIO,
) -> str:
    """把 text 裁到 limit 以内：切在结构边界上，并在结尾显式标注截断。

    - 未超长 → 原样返回（零成本、零标注，绝不给完整文本挂"节选"帽子）。
    - 超长 → 在 [limit*(1-ratio), limit] 区间内找最靠后的段落/行/句末边界切开，
      拼上尾注。找不到边界（例如整段无换行的长文）→ 硬切，尾注照挂。
    """
    s = str(text or "")
    if limit <= 0 or len(s) <= limit:
        return s

    window_start = max(0, int(limit * (1.0 - max(0.0, min(1.0, boundary_ratio)))))
    head = s[:limit]
    cut = -1
    # ① 段落边界（空行）——markdown 表格、列表、代码块都在段落内部，段落边界最安全
    for sep in ("\n\n", "\r\n\r\n"):
        idx = head.rfind(sep)
        if idx >= window_start:
            cut = max(cut, idx)
    # ② 行边界
    if cut < 0:
        idx = head.rfind("\n")
        if idx >= window_start:
            cut = idx
    # ③ 句末标点（中英文都认；中文需求没有空格分词，只靠句号系列）
    if cut < 0:
        for punct in ("。", "！", "？", ".\n", ". ", "；", ";"):
            idx = head.rfind(punct)
            if idx >= window_start:
                cut = max(cut, idx + len(punct))
    if cut < 0:
        cut = limit  # 无任何边界可用（如超长单行）→ 硬切，但尾注仍如实说明

    shown = s[:cut].rstrip()
    return (
        f"{shown}\n\n"
        f"（⚠️ {what}共 {len(s)} 字符，此处只展示前 {len(shown)} 字符，后续内容未展示。"
        f"若判断本模块的设计依赖未展示部分，请在输出中明确说明信息不足，"
        f"**绝不要**凭猜测补全。）"
    )
