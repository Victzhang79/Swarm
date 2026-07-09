---
id: golang-testing
title: Go 测试模式（表驱动·TDD·基准）
applies_to_stacks: ["go"]
applies_to_intents: ["create", "debug"]
applies_to_phases: ["code"]
target: ["worker"]
priority: 48
max_chars: 1800
tags: ["go", "testing", "tdd"]
---

# Go 测试模式速查

## TDD 红绿循环
- RED:先写失败测试,占位实现 `panic("not implemented")`,`go test` 必须先看到 FAIL(退出码证 RED)。
- GREEN:写最小实现让测试过。
- REFACTOR:保持绿的前提下改结构。

## 表驱动(标准姿势)
```go
tests := []struct{ name string; a,b,want int; wantErr bool }{
    {"pos", 2, 3, 5, false},
    {"bad", 0, 0, 0, true},
}
for _, tt := range tests {
    tt := tt // 捕获循环变量
    t.Run(tt.name, func(t *testing.T){
        got, err := Add(tt.a, tt.b)
        if tt.wantErr { if err==nil { t.Error("want err") }; return }
        if err!=nil { t.Fatalf("unexpected: %v", err) }
        if got!=tt.want { t.Errorf("got %d want %d", got, tt.want) }
    })
}
```
- 结构体深比用 `reflect.DeepEqual`;独立用例加 `t.Parallel()`。

## 辅助与清理
- 辅助函数首行 `t.Helper()`(错误定位到调用处)。
- 资源清理用 `t.Cleanup(func(){...})`;临时目录/文件用 `t.TempDir()`(自动回收)。

## Mock(基于接口)
- 依赖抽成接口;测试用字段函数式 mock:`type Mock struct{ GetFunc func(id string)(*User,error) }`,方法体转调字段。避免过度 mock,能集成测就集成测。

## 基准与模糊
- 基准:`for i:=0;i<b.N;i++`,setup 后 `b.ResetTimer()`;`go test -bench=. -benchmem` 看 ns/op 与 allocs/op。
- 模糊(1.18+):`f.Add(seed...)` 播种,`f.Fuzz(func(t,in){...})` 断言性质(如可逆);`go test -fuzz=Fx -fuzztime=30s`。

## HTTP handler
- `httptest.NewRequest` + `httptest.NewRecorder`,调 `ServeHTTP` 后断言 `w.Code` / `w.Body`。

## 命令与覆盖
- `go test -race ./...`;`-run TestX/Sub` 定向;`-count=10` 抓 flaky。
- 覆盖:`-coverprofile=c.out` → `go tool cover -func=c.out`;一般 80%+,核心逻辑更高。

## 纪律
- 测行为不测私有实现(走公开 API);勿用 `time.Sleep`,用 channel/条件等待;flaky 必修不放任;错误路径必测。
