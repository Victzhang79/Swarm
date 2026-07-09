---
id: python-patterns
title: Python 惯用写法与最佳实践
applies_to_stacks: ["python"]
applies_to_intents: ["*"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 50
max_chars: 1100
tags: ["python", "idiom", "best-practice"]
---
- 类型注解：公共函数签名带完整类型；用内置泛型（list/dict）与 `X | None`，通过 ruff + mypy。
- 显式优于隐式：避免魔法；禁可变默认参数（`def f(x=[])` → 用 `None` 哨兵再内部建）。
- EAFP + 精确异常：捕获具体异常类型，不用裸 `except:`；错误显式传播，不静默吞。
- 资源管理：文件/连接/锁一律 `with`（上下文管理器）确保释放。
- 推导式适度：简单变换用列表/字典推导；复杂逻辑拆成显式循环保可读。
- 数据结构：偏好 dataclass / NamedTuple 表达结构化数据，别到处传裸 dict。
- 纯逻辑与 I/O 分离：核心逻辑不直接读写外部资源，便于单测。
- 路径用 pathlib、时间用 datetime（带时区意识）、枚举用 Enum；别重造轮子。
- 遵循 PEP 8 由格式化/lint 工具兜底，你专注正确性与清晰度。
