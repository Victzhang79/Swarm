# Swarm 团队试用指南

## 前置

1. `bash setup.sh` 或 `bash setup.sh --dev`
2. `bash scripts/start-services.sh`（Qdrant + API）
3. 浏览器打开 http://localhost:8420

## 第一天：单 Worker（Phase 0）

1. 添加项目 → 指向本地 git 仓库
2. **Worker** Tab：小范围 scope 试跑 trivial 任务
3. CLI：`swarm worker-run "..." -p <pid> --writable src/foo.py --watch`
4. 验收：`bash scripts/run_milestone_check.sh <pid> 0`（Accept ≥60%）

## 第二天：Brain 全链路（Phase 1）

1. 运行预处理（CodeGraph → Qdrant → 知识库）
2. **任务** Tab 创建任务，或 `swarm submit -p <pid> --watch`
3. 审核 Diff → 通过 / 修订 / 拒绝
4. 验收：`bash scripts/run_milestone_check.sh <pid> 1`

## 第三天：知识 + 记忆（Phase 2–4）

- **知识库**：符号 / 语义检索、Harness 规范、行为热点
- **记忆**：L1 画像 JSON、错题 / 成功模式
- approve 后观察记忆 Tab 自动刷新

## 运维

```bash
bash scripts/restart-api.sh   # 改代码 / .env 后
bash scripts/stop-api.sh
tail -f swarm.log
```

## 可选配置

| 变量 | 用途 |
|------|------|
| `SWARM_API_KEY` | 启用 API Key 鉴权 |
| `SWARM_MAX_TASK_TOKENS` | 单任务 token 估算上限 |
| `SWARM_GITLAB_*` | L3 GitLab Pipeline 验证 |
| `SWARM_LANGSMITH_*` | 追踪 |

## 报告问题

- System Tab 查看 Accept 率、token、学习趋势
- `GET /api/milestones` 查看历史 benchmark
- LangSmith 项目 `swarm-dev`
