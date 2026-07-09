---
id: rust-testing
title: Rust 测试（单元/集成/文档测试）
description: "当你在写 Rust 测试（#[cfg(test)] 单元测试、tests/ 集成测试、#[tokio::test] 异步、rstest 参数化、proptest 属性测试、mockall）时调用，返回 TDD 红绿流程与断言规则速查。"
applies_to_stacks: ["rust"]
applies_to_intents: ["create", "debug"]
applies_to_phases: ["code"]
target: ["worker"]
priority: 48
max_chars: 1800
tags: ["rust", "testing", "tdd"]
---

# Rust 测试

## TDD 循环
RED（先写失败测试，实现体先 `todo!()`，`cargo test` 见 panic 证 RED）→ GREEN（最小实现通过）→ REFACTOR（保持绿）。

## 单元测试
- 同文件内 `#[cfg(test)] mod tests { use super::*; ... }`。
- 断言优先 `assert_eq!`（错误信息更好），可加自定义消息；浮点比较用 `.abs() < f64::EPSILON`。
- 测 `Result`：`assert!(r.is_err())` + `matches!(err, ErrKind::_)`；成功路径让测试返回 `Result<(), Box<dyn Error>>` 用 `?`。
- 测 panic 用 `#[should_panic(expected = "...")]`，但能测 `is_err()` 就别用 should_panic。

## 集成测试
- 放 `tests/` 目录，每个文件是独立测试二进制；共享工具放 `tests/common/mod.rs`；只能用 crate 公共 API。

## 异步
`#[tokio::test]` + `.await`；超时测试用 `tokio::time::timeout(dur, op).await`。

## 进阶工具
- 参数化：`rstest` 的 `#[case(...)]` + `#[fixture]`。
- 属性测试：`proptest!{}` 做往返/不变量（如 sort 保长度且有序），可自定义 Strategy。
- Mock：`mockall::automock` trait，`expect_x().with(eq(..)).times(1).returning(..)`。
- 文档测试：`///` 里写可执行 ` ``` ` 示例（不跑用 `no_run`）。
- 基准：criterion（`harness = false`），`b.iter(|| f(black_box(x)))`。

## 覆盖率
`cargo llvm-cov --fail-under-lines 80`；关键业务逻辑 100%、公共 API 90%+、一般 80%+，生成/FFI 排除。

## 纪律
- 测行为非实现；测试独立无共享可变态；测名描述场景。
- 禁 `sleep()`（用 channel/barrier/`tokio::time::pause()`）；别忽略 flaky（修或隔离）；别漏错误路径。
