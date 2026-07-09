---
id: hexagonal-architecture
title: 六边形架构（端口与适配器）
applies_to_stacks: ["*"]
applies_to_intents: ["create", "refactor"]
applies_to_phases: ["plan", "code"]
target: ["worker", "planner"]
priority: 45
max_chars: 1800
tags: ["architecture", "ddd", "ports-adapters"]
---

让业务逻辑独立于框架/传输/持久化。核心依赖抽象端口，适配器在边缘实现端口。

**分层**
- Domain：实体/值对象/业务规则，零框架 import，不依赖任何外部
- Application（用例）：编排领域行为与工作流步骤
- Inbound ports：应用能做什么（命令/查询/用例接口）
- Outbound ports：应用需要什么（Repository/Gateway/事件发布/Clock/UUID）
- Adapters：端口的基础设施与投递实现（HTTP 控制器、DB 仓储、队列消费者、SDK 包装）
- Composition root：唯一装配点，把具体适配器注入用例

**依赖方向永远向内**：Adapters→Application→端口接口；Domain→仅领域抽象→无外部。Outbound 端口接口置于 application 层，基础设施适配器实现之。

**实现步骤**
1. 定义用例边界，含明确 input/output DTO；传输细节（req/context/job 载荷）挡在边界外
2. 先定义 outbound 端口——每个副作用建一个端口，按能力建模而非技术
3. 用例经构造注入端口，校验应用不变量、协调领域规则、返回纯数据
4. 边缘建适配器：inbound 转协议输入为用例输入，outbound 映射到 ORM/API；映射留适配器内
5. 组合根集中装配（禁散落的全局单例/服务定位器）

**测试按边界**
- Domain：纯业务规则，无 mock/框架
- 用例单测：outbound 端口用 fake，断言业务结果与端口交互
- Adapter 契约测试：端口级共享契约套件跑各实现
- Adapter 集成测试：真基础设施（DB/API/队列）验序列化/schema/重试/超时
- E2E：inbound→用例→outbound 关键旅程

**反模式**：领域实体 import ORM/框架/SDK；用例直读 req/res/队列元数据；用例直接返 DB 行不映射；适配器互相直调不走端口；装配散落成隐藏单例。

**渐进迁移**（禁大爆炸重写）：选高频变更、低爆炸半径的单一纵切；抽用例边界→围现有基础设施加 outbound 端口→把编排从控制器移入用例→旧适配器改为委托新用例→补特征化测试锁行为→逐切推进，每切留可回滚开关。

要点：不可变转换（返新值不改共享态）；跨边界翻译错误（infra→application/domain）；语言/框架特性只留适配器。
