---
id: regex-vs-llm-structured-text
title: 结构化文本解析：正则 vs LLM 选型
description: "当你要解析格式重复的结构化文本（表单/发票/题库）并纠结用正则还是 LLM 抽取时调用，返回「正则抽取+置信度打分+LLM 兜底」三段管线的选型决策与落地要点。"
enabled: false  # 阶段E 下架：niche 通配稀释选择面（G6）
applies_to_stacks: ["*"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code"]
target: ["worker"]
priority: 40
max_chars: 1800
tags: ["parsing", "decision", "text"]
---

# 结构化文本解析：正则 vs LLM

核心洞察：格式重复的结构化文本（表单、发票、题库、文档），正则能确定性、零成本地覆盖 95-98%；把昂贵的 LLM 调用只留给低置信度边角。

## 选型决策
- 格式一致且重复（>90% 走同一模式）→ 先写正则；正则覆盖 <95% 时才对边角追加 LLM。
- 自由格式、高度多变 → 直接用 LLM。
- 别把全部文本丢给 LLM（贵且慢），也别用正则硬啃自由文本。

## 推荐管线（三段）
1. 正则抽取：finditer 提取结构，返回不可变对象（frozen dataclass/元组），清洗步骤只返回新实例、绝不原地改。
2. 置信度打分：对每条抽取结果程序化打分并标记问题，例如
   ```python
   score = 1.0
   if len(choices) < 3: score -= 0.3          # 字段偏少
   if not answer:       score -= 0.5          # 关键字段缺失
   if len(text) < 10:   score -= 0.2          # 内容过短
   ```
   低于阈值（如 0.95）的进入复核队列。
3. LLM 兜底：仅对被标记的条目调用最便宜的小模型，让它「返回修正后的 JSON，或回 CORRECT 表示无误」，其余条目直接放行。

## 落地要点
- 先有正则基线（哪怕不完美）再迭代，比一开始追求完美更快收敛。
- 用置信度打分「程序化」决定谁需要 LLM，别靠感觉。
- 记录指标：正则命中率、LLM 调用数、边角占比，用真实数据回调阈值。
- 边界用例先写测试：畸形输入、缺字段、编码异常。
- 典型收益：410 条量级下正则命中 ~98%，LLM 仅需 ~5 次，相比全量 LLM 省约 95% 成本。
