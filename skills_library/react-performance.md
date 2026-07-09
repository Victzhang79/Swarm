---
id: react-performance
title: React 性能优化（重渲染/记忆化/懒加载）
applies_to_stacks: ["node"]
applies_to_intents: ["modify", "refactor"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 45
max_chars: 1800
tags: ["node", "react", "performance"]
---

React 18/19 与 Next.js 性能优化，按优先级排查。

1 消除 waterfall（最关键）——每个串行 `await` 加一次全网络延迟
- 便宜的同步条件（props/env/flag）先判、再 await 远端。
- await 延后到真正使用的分支。
- 独立请求用 `Promise.all` 并行；部分依赖则先发起 promise、按需再 await。
- Server 组件把兄弟 await 拆成子组件，React 自动并行；Suspense 贴近数据做流式（预留骨架防抖动）。

2 包体积（关键）
- 直接 import 具体路径，别走 barrel `index.ts`（可省 200-800ms 首屏 JS）。
- 动态 import 路径要静态可分析（禁模板字符串拼路径）；重组件用 `dynamic()`，客户端专属加 `ssr:false`。
- 第三方脚本（分析/日志/客服）hydration 后再加载；按角色/条件 `await import()`；hover/focus 预加载。

3 服务端（高）
- 每个 `"use server"` 都是公开端点，内部必须自行鉴权+授权，别信调用方 gating。
- 同请求去重用 `React.cache()`；跨请求静态数据用 LRU/`unstable_cache`；静态 I/O 提到模块作用域。
- 禁服务端可变模块级状态（跨请求共享=竞态），用请求作用域存储；只序列化 Client 真正需要的字段。

4 客户端取数（中高）：共享数据用 SWR/TanStack Query 去重；全局监听器单例共享；scroll 用 `{passive:true}`；localStorage 带 `version` 且体积小。

5 重渲染（中）
- 仅在回调用到的 state 别订阅（改用 `store.getState()`）。
- 派生值在 render 直接算，别 `useEffect`+`setState`。
- 订阅派生布尔而非原值（`s => s.cart.length>0`）；effect 依赖用原始值而非对象。
- 非原语默认 props 提到组件外（`const EMPTY=[]`）；昂贵初值用惰性 `useState(() => …)`；禁在组件内定义组件。
- 非紧急更新用 `startTransition`；昂贵渲染用 `useDeferredValue`。

6 渲染（中）：长列表 `content-visibility:auto`；条件渲染用三元而非 `&&`（`0` 会渲染成文本）；hoist 静态 JSX；tab/手风琴用 `<Activity>` 代替 mount/unmount。

7 JS 微优化：`Map`/`Set` 做 O(1) 查找；循环外缓存 `arr.length` 与 RegExp；`filter().map()` 合并单遍；min/max 用循环而非 `sort()`。

注：项目启用 React Compiler 后手写 memo 降为 review-only。LCP←waterfall/包体积；INP←重渲染/JS；CLS←渲染。
