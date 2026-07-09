---
id: docker-patterns
title: Docker 容器化与编排模式
applies_to_stacks: ["*"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 45
max_chars: 1800
tags: ["docker", "ops", "container"]
---

# Docker 容器化模式

## Dockerfile 要点
- 多阶段构建：deps → build → 精简 runtime；生产镜像不带构建工具/dev 依赖。
- 版本 tag 钉死（如 `node:22.12-alpine`），禁用 `:latest`。
- 先 COPY 依赖清单再装依赖，再 COPY 源码→吃满层缓存；源码变更不重装依赖。
- 建非 root 用户并 `USER` 切换；COPY 产物时 `--chown`。
- 加 `HEALTHCHECK`；不把密钥写进镜像层。

## Compose 本地栈
- 应用+DB+缓存+辅助服务（如本地邮件）一套起；同网络下按服务名解析（`db:5432`、`redis:6379`）。
- 依赖就绪用 `depends_on: {condition: service_healthy}` + 被依赖服务配 `healthcheck`。
- 源码 bind mount 热更新时，用匿名卷 `/app/node_modules` 保护容器内依赖不被宿主覆盖。
- override 文件放 dev-only 设置（调试端口、debug 日志）自动加载；生产用显式 `-f compose.yml -f compose.prod.yml`。

## 网络与暴露
- 分网隔离：前端网/后端网，DB 只挂后端网，前端不可直连。
- 端口按需暴露：`127.0.0.1:5432:5432` 只给宿主；生产索性省略 ports，仅容器网内可达。

## 数据卷
- 命名卷持久化 DB 数据（`pgdata:/var/lib/...`）；容器是临时的，无卷=重启丢数据。
- 初始化脚本挂到 `/docker-entrypoint-initdb.d/`。

## 安全加固
- compose 加：`no-new-privileges:true`、`read_only: true` + `tmpfs` 写目录、`cap_drop: [ALL]` 按需 `cap_add`。
- 密钥走 `env_file`（.env 不入库）或 Docker secrets，绝不硬编码进镜像。

## .dockerignore
- 排除 `node_modules .git .env* dist coverage *.log tests/ Dockerfile* compose*.yml`，减小上下文+防泄露。

## 反模式
- 生产直接裸跑 compose（应上 K8s/编排）、无卷存数据、跑 root、用 `:latest`、单容器塞多服务、把密钥写进 compose。
