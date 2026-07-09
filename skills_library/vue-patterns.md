---
id: vue-patterns
title: Vue 开发模式（Composition API/响应式）
applies_to_stacks: ["node"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 48
max_chars: 1800
tags: ["node", "vue", "component"]
---

# Vue 3 开发模式（Composition API）

统一用 `<script setup lang="ts">`，禁 Options API/mixins（用 composable 替代）。

## 组件
- SFC 顺序：imports → props/emits → composables → 本地 state → computed → methods → watch → 生命周期。
- 容器组件管数据/副作用；展示组件只收 props、emit 事件，不碰 store/API。
- props 用类型化 `withDefaults(defineProps<Props>(), {...})`；布尔用 `isX/hasX/canX`；禁改 props，改用 emit 或 `defineModel()`（3.4+）。
- 事件用类型化 `defineEmits<{...}>()`；模板 kebab-case，脚本 camelCase。

## Composable
- 必须 `use` 前缀，返回响应式（ref/computed/reactive），入参用 `MaybeRef` + `toValue()`。
- 副作用在 `onUnmounted`/`onWatcherCleanup()` 清理；禁模块级副作用。

## 状态
- 本地 `ref/reactive`；跨组件 provide/inject；全局用 Pinia Setup Store（`defineStore("x", () => {...})`）。异步 action 必须覆盖 loading/成功/错误。

## 模板/性能
- `v-for` 用稳定 `:key`（业务 id，禁 index）；禁同元素 `v-if`+`v-for`，改 computed 过滤数组。
- 频繁切换用 `v-show`；大对象整体替换用 `shallowRef`；非关键路由懒加载 `() => import()`；`v-memo`/`<KeepAlive>` 按需。

## 3.5+
- 响应式 props 解构可用；但 watch 需 getter：`watch(() => count, ...)`。
- 模板引用用 `useTemplateRef("name")`；SSR 稳定 id 用 `useId()`。

## 反模式
- `v-html` 用户内容=XSS（须净化）；`reactive()` 存可替换 state（改用 ref）；watcher 无清理=泄漏/竞态。

## 测试
- Vitest + Vue Test Utils；`mount` 后断言渲染文本与 `emitted()`；Pinia 用 `setActivePinia(createPinia())`。
