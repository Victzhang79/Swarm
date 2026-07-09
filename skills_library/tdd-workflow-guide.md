---
id: tdd-workflow-guide
title: 测试驱动工作法（指南形态）
applies_to_stacks: ["*"]
applies_to_intents: ["create", "debug"]
applies_to_phases: ["code"]
target: ["worker"]
priority: 50
max_chars: 900
tags: ["testing", "tdd"]
---
这是工作法指南（非红绿闸；闸门由系统确定性执行）：
- 先写失败测试再实现：新功能先落一条表达期望行为的测试并确认它当前失败（红），再写最小实现让它通过（绿），最后重构保持绿。
- 修 bug 先复现：先写一条能稳定复现该 bug 的失败用例，再动手修；修完该用例转绿即回归证据。
- 测行为非实现：断言可观察的输入→输出/副作用，别绑死内部结构（否则重构即误报）。
- 边界与异常：覆盖空/极值/并发/错误路径，不只测 happy path。
- 一次一个失败原因：单测聚焦一个行为，失败信息要能一眼定位。
- 与既有测试同风格：沿用本项目测试框架、命名与目录约定。
