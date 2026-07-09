---
id: coding-standards-core
title: 通用编码规范（栈无关）
description: "当你在写或重构任意语言代码、需要 fail-closed 默认拒绝、资源配对释放、边界一次校验、降级留 WARNING 等工程纪律时调用，返回栈无关的编码底线清单。"
applies_to_stacks: ["*"]
applies_to_intents: ["create", "modify", "refactor"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 45  # G7：与 L2 _CORE_RULES 重叠去重后让出高位（70 永占 3 工具位第 1 位）
max_chars: 900
tags: ["style", "maintainability"]
---
- 默认拒绝 / fail-closed：不确定的分支不静默放行，宁可显式失败。
- 入口对称：新增的资源/连接/句柄，在同一作用域配对释放；先写后删（先加新路径跑通再删旧）。
- 边界复校：外部输入在信任边界一次校验，内部不重复猜测。
- 小步可测：改动拆到可独立验证的单元；每个公共行为配一条测试。
- 降级可观测：任何降级/跳过都留一条 WARNING（谁、为什么、影响面），绝不静默。
- 精确编辑：优先 patch 精确编辑而非整文件重写。

（G7 去重说明：Scope 内改动、风格与周边一致、异常显式上抛不吞——这些铁律已由系统
L2 规范层常注入 worker prompt，本技能不复读，只保留 L2 没有的工程纪律。）
