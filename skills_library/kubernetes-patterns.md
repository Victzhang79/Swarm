---
id: kubernetes-patterns
title: Kubernetes 部署与编排模式
description: "当你在编写或审查 K8s Deployment/Job YAML，配置 liveness/readiness/startupProbe、requests/limits、securityContext、RBAC 最小权限时调用，返回生产级配置要点与 kubectl 排障速查。"
applies_to_stacks: ["*"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["plan", "code"]
target: ["worker", "planner"]
priority: 42
max_chars: 1800
tags: ["kubernetes", "ops", "deployment"]
---

生产级 K8s 工作负载要点（编写/审查/排障 YAML 时用）。

**Deployment 必备**
- 镜像固定不可变 tag（semver 或 `@sha256:`），禁 `:latest`
- `strategy: RollingUpdate` + `maxUnavailable: 0`（不低于期望副本）
- 生产副本 `replicas>=2`；`terminationGracePeriodSeconds: 30` 优雅退出

**三种探针**（配 `failureThreshold × periodSeconds` 算超时）
| 探针 | 失败动作 | 用于 |
|---|---|---|
| startupProbe | 杀慢启动容器 | 慢启动应用（JVM/Python）|
| livenessProbe | 重启容器 | 死锁/挂起检测 |
| readinessProbe | 摘出 Service 端点 | 临时不可用（DB 重连）|
- 慢启动用 startupProbe，勿用 `initialDelaySeconds` 硬等（竞态）
- readiness 用独立 `/ready`（查 DB/缓存），liveness 用 `/health`

**资源**：`requests` 与 `limits` 都必须设（缺则调度不可控/OOM 驱逐）。requests 供调度，limits 触发限流/杀。HPA 依赖 requests 算利用率。

**安全上下文**
- `runAsNonRoot: true` + 显式 `runAsUser`
- `allowPrivilegeEscalation: false`、`readOnlyRootFilesystem: true`（配 emptyDir 挂可写 /tmp）
- `capabilities.drop: [ALL]`

**Secret/Config**：敏感值走 Secret（原生仅 base64 非加密，生产用 Sealed Secrets/External Secrets Operator）；非敏感走 ConfigMap，禁明文密码入 ConfigMap。

**RBAC 最小权限**：应用不调 K8s API 则 `automountServiceAccountToken: false`；需调用才建 Role（非 ClusterRole）+ RoleBinding，按 `resourceNames` 收窄；禁给应用 cluster-admin。每应用专属 SA，不用 default。

**Job**：`restartPolicy: OnFailure`/`Never`（禁 Always→死循环）；设 `backoffLimit`、`ttlSecondsAfterFinished`。CronJob 加 `concurrencyPolicy: Forbid`。

**弹性**：HPA（CPU 70%/内存 80%，`minReplicas>=2`）；关键服务加 PodDisruptionBudget（`minAvailable>=1`，禁 0）。

**排障速查**
```
kubectl describe pod <p>        # 事件/退出码/OOMKilled
kubectl logs <p> --previous     # 崩溃前日志(CrashLoop)
kubectl rollout undo deploy/<d> # 回滚
kubectl apply -f x.yaml --dry-run=server
```
CrashLoop→--previous+退出码；ImagePull→tag/私仓凭证；Pending→资源/亲和/污点。
