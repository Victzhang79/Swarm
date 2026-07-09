---
id: rust-patterns
title: Rust 惯用写法与最佳实践
applies_to_stacks: ["rust"]
applies_to_intents: ["*"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 50
max_chars: 1800
tags: ["rust", "idiom", "best-practice"]
---

# Rust 惯用写法

## 所有权
- 不需所有权时传 `&T`/`&[T]`，别为绕借用检查器 `clone()`。
- 借/拥有二选一时用 `Cow<'_, str>`：无修改则 `Borrowed` 零成本。

## 错误处理
- 库用 `thiserror` 定义结构化 enum 错误（`#[from]` 转换）；应用用 `anyhow` + `.with_context(...)`/`bail!`。
- 用 `?` 传播，生产代码禁 `unwrap()`；`Option` 用组合子链（`.find().map()`）而非嵌套 match。
- 库函数别返回 `Box<dyn Error>`；`Result` 上加 `#[must_use]`。

## 类型建模
- 状态建模成 enum，让非法态不可表达；业务枚举穷尽匹配，禁 `_` 通配（新增变体强制处理）。
- newtype 包裹原始类型防参数混淆（`struct UserId(u64)`）。
- 边界处"解析而非校验"，把非结构化输入转成类型化结构体。

## trait/泛型
- 入参收泛型（`impl Read`/`T: Bound`），返回具体类型。
- 异构集合/插件用 `Box<dyn Trait>` 动态分发；性能路径用泛型单态化。
- 复杂构造用 builder 模式。

## 迭代器
- 优先迭代器链（`filter/map/collect`）而非手写循环。
- `collect()` 带类型标注；收集 `Result<Vec<_>>` 遇错短路。

## 并发
- 共享可变态用 `Arc<Mutex<T>>`，`lock().expect("poisoned")`。
- 消息传递用 `mpsc::sync_channel(n)`（有界背压），发送端 drop 令 rx 终止。
- 异步用 tokio；禁在 async 里 `std::thread::sleep`（阻塞执行器，改 `tokio::time::sleep().await`）；超时用 `tokio::time::timeout`。

## unsafe/模块
- unsafe 仅限 FFI/性能热点，必带 `// SAFETY:` 注释与文档不变量；禁用来绕借用检查器。
- 按领域组织模块（非按类型）；`pub(crate)` 收窄可见面，`lib.rs` re-export 公共 API。

## 工具
`cargo check`/`clippy -- -D warnings`/`fmt --check`/`test`/`audit`。
