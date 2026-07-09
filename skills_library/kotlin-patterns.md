---
id: kotlin-patterns
title: Kotlin 惯用写法与最佳实践
applies_to_stacks: ["kotlin"]
applies_to_intents: ["*"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 50
max_chars: 1800
tags: ["kotlin", "idiom", "best-practice"]
---

# Kotlin 惯用写法

- Null 安全：默认用非空类型；取值用 `?.`/`?:`，找不到就 `?: throw`；禁 `!!` 强解包。
- 不可变优先：`val` 而非 `var`；`data class` + `copy()` 做更新；用 `listOf`/`mapOf` 不可变集合。
- 表达式体：单表达式函数用 `= ...`；`when` 当表达式用（要求穷尽，`else` 兜底）。
- 类型建模：`data class` 表值对象；`@JvmInline value class` 做零开销类型安全包装（`init { require(...) }` 校验）；`sealed class`/`sealed interface` 表受限层级，配穷尽 `when`（如 Result Success/Failure/Loading）。
- 作用域函数：`let`（可空转换）/`apply`（配置返回自身）/`also`（副作用）/`run`/`with`（带接收者执行）。禁嵌套，改用安全链 `a?.b?.c?.let{}`。
- 扩展函数：无继承加行为；私有扩展别污染全局命名空间。
- 委托：`by lazy` 懒加载；`Delegates.observable`；接口委托 `class X(d: I) : I by d` 只覆写需增强的方法。
- 错误处理：域操作用 `runCatching`/`Result` 的 `map`/`getOrElse`；前置条件用 `require`（参数）/`check`（状态），带清晰消息；不要用异常做正常控制流（改可空返回或 Result）。
- 集合：链式 `filter`/`map`/`sortedBy`/`groupBy`/`associateBy`/`partition`；大集合多步操作用 `asSequence()` 惰性求值。
- DSL：`@DslMarker` + lambda 接收者（`init: T.() -> Unit`）做类型安全 builder。

速查：`val`>`var` | `data class`/`value class`/`sealed` | 表达式 `when` | `?.`/`?:` | `copy()` | `require`/`check` | 扩展函数 | `by` 委托 | `sequence` 惰性。

反模式：`!!` 强解包；可变 data class（`var` 字段）；异常做控制流；深嵌套作用域函数。
