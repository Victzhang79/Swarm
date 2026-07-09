---
id: coding-standards-core
title: 通用编码规范（栈无关）
applies_to_stacks: ["*"]
applies_to_intents: ["create", "modify", "refactor"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 70
max_chars: 900
tags: ["style", "maintainability"]
---
- 默认拒绝 / fail-closed：不确定的分支不静默放行；错误显式传播，不吞异常。
- 入口对称：新增的资源/连接/句柄，在同一作用域配对释放；先写后删（先加新路径跑通再删旧）。
- 边界复校：外部输入在信任边界一次校验，内部不重复猜测。
- 小步可测：改动拆到可独立验证的单元；每个公共行为配一条测试。
- 降级可观测：任何降级/跳过都留一条 WARNING（谁、为什么、影响面），绝不静默。
- 命名随代码：与周边同风格、同缩进、同惯用法；注释解释"为什么"而非复述"做什么"。
- 最小改动：优先精确编辑（patch）而非整文件重写，别顺手改无关行。
