---
id: kotlin-coroutines-flows
title: Kotlin 协程与 Flow（异步/并发）
applies_to_stacks: ["kotlin"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 46
max_chars: 1800
tags: ["kotlin", "coroutines", "async"]
---

# Kotlin 协程与 Flow

- 结构化并发：永远用生命周期绑定的 scope（如 `viewModelScope`）；禁 `GlobalScope`（泄漏、无级联取消）。
- 并行分解：`coroutineScope { val a = async{...}; val b = async{...}; T(a.await(), b.await()) }`。子任务失败不应互相取消时用 `supervisorScope`。
- Dispatchers：CPU 密集 `Dispatchers.Default`；IO 用 `Dispatchers.IO`（仅 JVM/Android，多平台回退 Default 或注入）；UI 用 `Main`。

Flow：
- 冷流 `flow { emit(...) }`；UI 状态用 `StateFlow`：`upstream.stateIn(scope, SharingStarted.WhileSubscribed(5_000), initial)`（订阅者离开后保活 5s，扛配置变更）。
- 多流合并：`combine(f1, f2, f3){ a,b,c -> State(...) }.stateIn(...)`。
- 一次性事件用 `SharedFlow`（`MutableSharedFlow` + `asSharedFlow()`），别塞进 StateFlow。
- 常用算子：搜索输入 `debounce(300).distinctUntilChanged().flatMapLatest{ repo.search(it) }.catch{ emit(empty) }`；重试指数退避用 `retryWhen { cause, attempt -> ... delay(1000L*(1 shl attempt.toInt())); attempt<3 }`。

取消：
- 长循环协作取消：循环内 `ensureActive()`（被取消则抛 `CancellationException`）。
- 清理用 `try/finally`（finally 在取消时也执行）；不可取消的释放包在 `withContext(NonCancellable){...}`。

测试：`runTest { ... advanceUntilIdle() }`；用 fake repo 暴露 `MutableStateFlow`；StateFlow 断言可用 turbine `.test { awaitItem() }`。

反模式：`GlobalScope`；在 `init{}` 里无 scope 收集 Flow；`MutableStateFlow` 装可变集合（改 `_state.update{ it.copy(list = it.list + x) }`）；捕获并吞掉 `CancellationException`（须让它传播）。
