---
id: golang-patterns
title: Go 惯用写法与最佳实践
description: "当你在写 Go 代码、涉及 errors.Is/As 与 %w 包裹错误、context 超时取消、errgroup 并发防泄漏或 functional options 时调用，返回 Go 惯用法与工具闸清单。"
applies_to_stacks: ["go"]
applies_to_intents: ["*"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 50
max_chars: 1800
tags: ["go", "idiom", "best-practice"]
---

# Go 惯用写法速查

## 核心原则
- 清晰胜过聪明；happy path 不缩进，错误先 return early。
- 零值可用：`sync.Mutex`/`bytes.Buffer` 无需初始化；map/chan 必须 `make` 否则 nil 写入 panic。
- 接受接口、返回具体类型；接口定义在使用方(消费者)包，越小越好(单方法优先)。

## 错误处理
- 包裹带上下文并保留链:`fmt.Errorf("load %s: %w", path, err)`。
- 判定用 `errors.Is`(哨兵) / `errors.As`(类型);哨兵用 `var ErrNotFound = errors.New(...)`。
- 绝不用 `_` 吞错;确需忽略(best-effort 清理)写 `_ = f.Close()` 并注明。
- 不用 panic 做控制流。

## 并发
- `context.Context` 作第一个参数(勿塞进 struct);带超时 `ctx, cancel := context.WithTimeout(...); defer cancel()`。
- worker pool:`sync.WaitGroup` + `for range jobs`,发完 `close(results)`。
- 多 goroutine 协同用 `errgroup.WithContext`,任一出错即取消。
- 防泄漏:发送用 buffered chan 或 `select { case ch<-v: case <-ctx.Done(): }`。
- 优雅关闭:`signal.Notify` 收 SIGINT/SIGTERM,再 `server.Shutdown(ctx)`。

## 结构与包
- 依赖注入替代包级全局态与 `init()` 建连;`NewServer(deps)` 显式传入。
- 可选配置用 functional options:`type Option func(*S)` + `NewServer(addr, opts...)`。
- 组合优先(struct 嵌入)而非继承。
- 包名短、小写、无下划线,勿加冗余后缀(避免 `userService`)。

## 性能
- 已知长度先预分配:`make([]T, 0, len(src))`。
- 循环拼字符串用 `strings.Builder` 或直接 `strings.Join`,勿 `+=`。
- 高频临时对象用 `sync.Pool`,取出后 `Reset` 再 `Put`。

## 工具闸
- 提交前:`gofmt -w . && goimports -w .`,`go vet ./...`,`go test -race -cover ./...`。
- lint 开启 errcheck/staticcheck/ineffassign/unused/govet(含 shadow)。
- 收敛判据:同一 struct 的方法接收者别混用值/指针,保持一致。
