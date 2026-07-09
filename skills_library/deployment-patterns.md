---
id: deployment-patterns
title: 部署与发布模式（CI/CD·健康检查·回滚）
description: "当你在配置 CI/CD 流水线、选滚动/蓝绿/金丝雀发布、写 /health 健康检查与 liveness/readiness 探针或设计回滚预案时调用，返回发布策略对比与上线前检查清单。"
enabled: false  # 阶段E 下架：教 CI/CD/发布操作，超出沙箱交付边界（G5）
applies_to_stacks: ["*"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["plan", "code"]
target: ["worker", "planner"]
priority: 45
max_chars: 1800
tags: ["deployment", "cicd", "ops"]
---

# 部署与发布模式

## 发布策略
- 滚动（默认）：逐实例替换，零停机，但新旧版本并存→改动必须向后兼容。
- 蓝绿：两套环境原子切流量，回滚=切回旧环境；代价 2x 资源。用于零容错关键服务。
- 金丝雀：先放小比例流量到新版，看指标再逐步放量；需流量切分+监控。用于高流量/高风险。

## CI/CD 流水线
- PR：lint → typecheck → 单测 → 集成测 → 预览部署。
- 合主干：上述 + 构建镜像 → 部署 staging → 冒烟 → 生产。
- 任一门失败即阻断；镜像 tag 用 commit sha（可追溯、可回滚）。

## 健康检查
- `/health` 返回轻量 200；`/health/detailed` 聚合依赖探针（DB/缓存/外部 API），全 ok 才 200，否则 503+degraded，附 version/uptime。
- 探针分工：liveness（活着否，失败重启）、readiness（能收流量否，失败摘流量）、startup（慢启动宽限，避免误杀）。

## 环境配置（12-Factor）
- 所有配置走环境变量，绝不写进代码/仓库；密钥由密钥管理器注入。
- 启动即校验配置 schema，缺失/非法立即 fail-fast，别等运行时崩。

## 回滚
- 保证上一个镜像/构件已 tag 且可用；一条命令回退到上一版本。
- DB 迁移必须向后兼容（先加后删、可逆），否则回滚会坏。
- 用特性开关关闭新功能，避免为回滚而重新部署。

## 上线前清单
- 应用：测试全绿、无硬编码密钥、日志结构化且不含 PII、健康端点有意义。
- 基建：镜像版本钉死、配置启动校验、设资源上限、SSL/TLS、水平扩缩容。
- 监控：导出请求率/延迟/错误率指标、错误率阈值告警、日志可检索、健康端点存活监控。
- 安全：依赖 CVE 扫描、CORS 白名单、公网端点限流、安全响应头（CSP/HSTS/X-Frame-Options）。
- 运维：回滚预案已演练、迁移按生产规模数据测过、常见故障有 runbook。
