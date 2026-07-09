---
id: react-patterns
title: React 开发模式（Hooks/组件/状态）
description: "当你在写 React 组件涉及 Hooks 纪律、状态放置（useState/Context/Zustand/TanStack Query）、RSC Server/Client 边界、Suspense 与 form actions 时调用，返回组件设计规则与反模式对照。"
applies_to_stacks: ["node"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 50
max_chars: 1800
tags: ["node", "react", "component"]
---

React 18/19 惯用模式，构建健壮、可访问、高性能的组件树。

核心原则
- render 是 props/state 的纯函数：派生值在 render 中直接算，别用 `useEffect`+`setState` 存派生态（多一轮渲染、易失同步）。
- 副作用（网络/订阅/mutation）只放事件处理器或 `useEffect`，绝不在 render body。
- 组合优于继承：用 `children`、render prop、组件 props 组合。

Hooks 纪律
- 只在顶层调用，禁条件调用；每个订阅/定时器/监听器都 cleanup。
- 新态依赖旧态用函数式更新 `setX(prev => …)`。
- 默认不 memo，`useMemo`/`useCallback` 仅在 profiler 或依赖链证明有必要时加。
- 同一 hook 序列在 2+ 组件重复才抽自定义 hook。

状态位置决策
- 单组件用 → 组件内 `useState`；父+少数后代 → 提升到最近公共祖先。
- 远端分支且低频读（主题/鉴权/locale）→ Context；高频跨树更新 → 外部 store（Zustand/Jotai/Redux Toolkit）。
- 来自服务端 → 服务端状态库（TanStack Query/SWR/RSC fetch）。多数页面不需要 Context 或全局 store。

Server/Client 组件（RSC）
- Server 组件默认、可 async、不发自身 JS；Client 组件用 `"use client"` 显式 opt-in。
- Server→Client 传可序列化 props 或 `children`；Client→Server 经 `<form action>` 或事件调用 Server Action。
- 禁在 Client 文件 `import` Server 组件，用 `children` 组合。

Suspense + 错误边界
- Suspense 边界贴近数据（非路由根），渐进呈现。
- 错误边界仅捕获 render/生命周期/构造中的错误，捕不到事件处理器与 async。

表单与数据获取
- 新代码优先 React 19 form actions（`useActionState`）；值驱动其他 UI/实时校验时用受控输入；复杂表单用 React Hook Form/TanStack Form。
- 应用数据别用 `useEffect`+`fetch`（竞态/无缓存/无重试）；用 RSC `await fetch`、TanStack Query 或 SWR。

性能与可访问性
- `React.memo` 仅当组件高频重渲染且 props 通常不变且渲染确实昂贵时用。拆分 Context（一 concern 一 context）避免渲染级联。
- 列表用稳定 `key`（DB id 非数组下标）；长列表虚拟化。
- 优先语义 HTML（`<button>`/`<a>`/`<nav>`）再考虑 `role`；表单输入必须有 label；路由/弹窗切换时管理焦点。
