---
id: frontend-patterns
title: 前端开发模式（React/Next.js/组件化）
applies_to_stacks: ["node"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 50
max_chars: 1800
tags: ["frontend", "react", "component"]
---

# 前端开发模式（React / Next.js）

## 组件
- 组合优于继承：小组件拼装（Card / CardHeader / CardBody），别靠继承。
- 复合组件：共享态放 Context，子组件用 useContext 取；context 为空时抛错，防越界使用。
- 状态提升到最近公共父级；纯展示组件用 `React.memo`。

## 自定义 Hook
- 抽复用逻辑：`useToggle`、`useDebounce`、`useQuery`。
- 异步 Hook 陷阱：把 fetcher/options 存进 ref 并在 effect 里同步，`refetch` 的 `useCallback` 依赖留空——否则每次渲染新建函数，effect 反复触发成无限请求。
- effect 清理：`useDebounce` 里 `return () => clearTimeout(handler)`。

## 状态管理
- 简单用 `useState`；跨组件复杂态用 `useReducer + Context`，reducer 纯函数、`{...state}` 返回新对象。

## 性能
- `useMemo` 缓存昂贵计算；排序先复制 `[...arr].sort()`（sort 原地改）。
- 传给子组件的函数用 `useCallback`。
- 重组件 `lazy(() => import())` + `Suspense` 兜底骨架。
- 长列表用虚拟化（只渲染可见行 + overscan）。

## 表单
- 受控输入 + 提交前校验，错误信息按字段存 `errors`，`onChange` 用函数式更新 `setForm(p => ({...p, name: v}))`。

## 健壮性与无障碍
- `ErrorBoundary`（class + `getDerivedStateFromError`）兜住渲染崩溃，提供「重试」。
- 键盘导航：ArrowUp/Down/Enter/Escape，容器给 `role`/`aria-*`。
- 焦点管理：Modal 打开存 `document.activeElement`，关闭还原；`role="dialog" aria-modal`。

## 反模式
- 内联函数/对象当 effect/Hook 依赖导致重跑循环；排序原地改数组；重组件不拆分懒加载。
