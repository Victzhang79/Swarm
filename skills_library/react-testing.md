---
id: react-testing
title: React 测试（Testing Library/行为断言）
description: "当你在用 Testing Library 测 React 组件（getByRole 可访问查询、userEvent 交互、MSW 网络 mock、renderHook 测 hook）时调用，返回行为断言规则、查询优先级与反模式清单。"
applies_to_stacks: ["node"]
applies_to_intents: ["create", "debug"]
applies_to_phases: ["code"]
target: ["worker"]
priority: 45
max_chars: 1800
tags: ["node", "react", "testing"]
---

以行为为中心的 React 组件/hook 测试。

核心原则：测用户所见所为，不测实现细节
- 用生产同款 provider 渲染，经可访问查询（role/label）+ `userEvent` 交互，断言可见输出与可观测副作用（回调触发/请求发出）。
- 禁：窥探 state/props/被调 hook、mock React 本身、断言渲染次数或 DOM 结构。

Runner 选择：Vite/Remix 用 Vitest；Next/CRA 用 Jest；需真实浏览器引擎（布局/动画/滚动/拖拽/iframe）用 Playwright CT。选一条，别混。

查询优先级（自上而下）
1. `getByRole`/`getByLabelText`/`getByText`/`getByPlaceholderText`
2. `getByAltText`/`getByTitle`
3. `getByTestId`（最后手段）
- `getBy*` 无匹配抛错；`queryBy*` 返回 null（断言不存在）；`findBy*` 异步（等元素出现）。

交互与异步
- `const user = userEvent.setup()` 每测一次，所有 `user.*` 都要 `await`；优先 `userEvent` 而非 `fireEvent`。
- 异步用 `await screen.findByText(...)`、`await waitFor(() => expect(spy).toHaveBeenCalled())`、`waitForElementToBeRemoved`。绝不用 `setTimeout`+断言（flaky）。

网络 mock（MSW，在网络层）
- `setupServer(...handlers)`，`beforeAll listen({onUnhandledRequest:"error"})`（未 mock 请求要显式 fail）、`afterEach resetHandlers`、`afterAll close`。
- 单测覆写用 `server.use(...)` 模拟 500 等错误路径。

hook 测试：`renderHook`；改状态调用包 `act()`；只经公开 API 测；用 context 的 hook 传 `wrapper`，且 `QueryClient` 在测试内实例化一次（放 wrapper 闭包内会每次渲染重置缓存→flaky）。

provider 与可访问性
- 抽 `renderWithProviders`（QueryClient retry:false + Theme + Router）复用。
- 交互组件跑 `axe`（jest-axe/vitest-axe）断言无 a11y 违规。

反模式：`container.querySelector`、断言渲染次数、`jest.mock("react")`、默认 mock 子组件、忽略 `act()` 警告、跨测共享可变状态。

覆盖率参考：纯工具≥90%、hook≥85%、展示组件≥80%、容器≥70%（黄金路径+错误态）、页面走 E2E。TDD：RED（先写失败测试并验证因正确原因失败）→GREEN（最小实现）→REFACTOR。
