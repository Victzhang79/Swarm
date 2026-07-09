---
id: cpp-coding-standards
title: C++ 编码规范（Core Guidelines）
description: "当你在写 C++17/20/23 代码、涉及 unique_ptr/RAII 资源管理、Rule of Five、enum class 或 scoped_lock 并发加锁时调用，返回 Core Guidelines 六大主线的规则速查。"
applies_to_stacks: ["cpp"]
applies_to_intents: ["*"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 50
max_chars: 1800
tags: ["cpp", "style", "best-practice"]
---

现代 C++（17/20/23）规范，源自 Core Guidelines。六大主线：RAII 管理资源、默认不可变、类型安全、表达意图、最小复杂度、值语义优先。

资源管理
- 禁裸 `new`/`delete` 与 `malloc`/`free`；所有权用 `unique_ptr`（默认）或 `shared_ptr`（需共享才用），构造用 `make_unique`/`make_shared`。
- 裸指针 `T*` 一律非拥有、仅作观察。
- 管理原生资源的类走 RAII，遵守 Rule of Zero（能不写特殊成员就不写）或 Rule of Five（写了一个就补齐五个）。

不可变与初始化
- 默认 `const`/`constexpr`，成员函数默认 `const`，入参传 `const&`。
- 对象声明即初始化，优先 `{}` 初始化；禁魔法数，用具名常量。
- 指针空值用 `nullptr`；禁 C 风格强转（用 `static_cast` 等），禁 `const_cast` 去 const；禁窄化/有损转换、禁有无符号混算。

函数与接口
- 一函数一逻辑操作，短小；接口显式、强类型；参数少。
- 输出用返回值，多返回值打包成 struct；绝不返回局部对象的指针/引用。
- 廉价类型按值传，昂贵类型 `const&`，move-sink 按值传。

类与枚举
- 有不变量用 `class`，成员各自独立用 `struct`；单参构造标 `explicit`。
- 多态基类析构：public virtual 或 protected 非 virtual；虚函数只标 `virtual`/`override`/`final` 之一；构造/析构里不调虚函数。
- 用 `enum class` 而非裸 `enum`，枚举值不用 ALL_CAPS。

并发
- RAII 锁（`lock_guard`/`scoped_lock`），且必须具名（`std::lock_guard<std::mutex> lock(m);`）。
- 多互斥用 `std::scoped_lock` 一次锁定防死锁；`cv.wait` 必带条件谓词；持锁时不调外部回调；不用 `volatile` 做同步。

其他
- 错误：抛自定义异常类型，按值抛、按引用接；禁空 catch、禁用异常做流程控制。
- 模板用 concept 约束（`std::integral` 等）。
- 头文件用 include guard 且自包含；头文件全局作用域禁 `using namespace`。
- 优先标准库容器/字符串；输出用 `'\n'` 不用 `std::endl`；无度量不谈性能、不过早优化。
