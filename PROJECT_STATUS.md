# Swarm 项目状态总览 (PROJECT_STATUS.md)

> 维护：随重大进展更新。最后更新：2026-06-17
> 这是项目的"单一事实源"状态文档——当前做到哪、怎么验证、怎么推送、还差什么。

---

## 一、项目本质

**范式**：大脑(Brain，云端大模型)拆解 + 小手(Worker，本地小模型)执行。
- 核心假设：小模型做手【无真实能力上限】，前提是 Brain 把任务拆细碎明确 + 喂足知识库上下文 + 给合理步数。
- worker 失败别归因"模型能力上限"（偷懒），真因几乎总是：拆解层信息没喂到位（scope/契约/周边代码/目标）或步数耗尽。
- 真实需求是【产品经理式】的（只说功能不点文件），系统须自己定位要动/新建哪些文件。

**最终交付**：Docker 多容器；编码阶段在本机跑。

---

## 二、模型路由（重要——当前配置有偏差，见 §五 待办 T3）

### 设计意图（用户明确）
- **Worker 执行就用本地小模型并行**，不该跑云端：
  - 主力并行：`Qwen3.6-40B-Claude-4.6-NVFP4` + `MiniMax-M2.7-Pro`（本地，两个一起干）
  - 次级回退：`Qwen3.6-27B-Saka-NVFP4`（前两个不通时）
  - 最终兜底：`Qwen3.5-122B-A10B-NVFP4`（注意上下文仅 64K，须控制输入规模）
- **max_workers=4** 的本意：本地真有 4 个小模型槽，4 个小模型一起干。测试阶段可少点。
- Brain（编排）用云端大模型：`Pro/zai-org/GLM-5.1`，兜底 `moonshotai/Kimi-K2.6`。

### 当前配置偏差（config/settings.py L109-117）
- ❌ `routing_complex = "Pro/zai-org/GLM-5.1"` —— complex 子任务被派给【云端大模型】，违背"worker 用本地小模型"。
- ❌ `routing_complex_fallback = "moonshotai/Kimi-K2.6"` —— 该模型 **403 private 不可用**（坏兜底）。
- ❌ `worker_fallback = "Qwen3.5"`（名字不全，应为 Qwen3.5-122B-A10B-NVFP4）。
- 后果（task fd5470e0 实证）：st-26(complex) 派给云端 GLM → 600s 超时空产出；retry 派给 Kimi → 403。

### 模型端点
- 本地推理：`http://ai.bit:3000`（192.168.60.143，Open WebUI v0.9.6 网关挂多 vLLM），Bearer token。
- 云端：SiliconFlow `https://api.siliconflow.cn/v1`。

---

## 三、已完成（按 commit，本地 4 个未 push）

| commit | 内容 | 验证 |
|--------|------|------|
| 3348c4b~94dd06e | v0.8.6-0.8.8：需求转化层/事实根基/ground truth 定位（已 push，已 tag） | 947-975 测 |
| b21d862 | 走查报告 S1-S5/H1-H9/M4/M7 安全可靠性修复 | 955 测 |
| e78b51b | TECH_DEBT 中危 M1/M2/M5/M6/M8 | 971 测 |
| fd6378e | 任务描述严格必填（前端+后端双层） | 真实 curl 四情况 |
| **86f803a** | **fact_check 不把附件/示例代码当被点名文件**（修 ultra 被虚假前提冤杀） | 975 测 + e2e fact_issues 3→0 |
| **3c5267b** | **PLAN 按模块分批拆解**（解决 125 文件单次拆解卡死） | 985 测 + e2e 实证 |
| **6116b0a** | **tech_design 两阶段产出**（解决单次生成上百文件方案卡死） | +3 测 + e2e 6模块86文件无卡死 |
| **c2466bd** | **分批治本 P1-P6**：按模块分批+垂直切片（替代 10% 机械切） | 991 测 |

### ultra 链路当前能力（e2e task fd5470e0 实证）
- ✅ INGEST 消化 PRD 附件 → ANALYZE 判 ultra → tech_design 两阶段(6模块→86文件,无卡死)
- ✅ PLAN 按模块分批(进度日志:批次/百分比/LLM耗时) → 42 子任务 → 进 dispatch → worker 并发执行
- ✅ 多个 worker L1 通过、有真实 diff 产出落盘（st-1/st-20/st-36 编译通过）

---

## 四、当前 TODO（按优先级，未做）

### T1【最高】shared_contract 跨文件协调（真问题）
- **问题**：分批后每个 worker 独立拆/独立写，没有共享契约 → 接口对不上。
  - 铁证(fd5470e0)：INotifyService 被 st-2(channel) 和 st-29(engine) 各建一次；NotifyServiceFactory 与 NotifyStrategyFactory 功能重复。
- **本质**：多个模型的产出无法天然达成一致。需要【契约先行】：先确定共享接口/DTO/常量，所有 worker 遵守同一份契约。
- 部分已有：dedupe_file_plan(P5 同名去重)、跨子任务上下文注入前序产出签名。但不够。
- **待设计**：见 §六 设计议题 D1。

### T2【最高】模型路由 + 并行 worker（用户明确意图）
- 改 routing：worker 各档首选都用【本地小模型】，不跑云端。complex 也用本地（Qwen3.6-40B-Claude / MiniMax-M2.7-Pro），兜底 Qwen3.6-27B-Saka → Qwen3.5-122B-A10B(64K)。
- 移除坏兜底 moonshotai/Kimi-K2.6(403 private)。
- 本地两小模型【并行】分派（不同 worker 用不同小模型，分散负载）。
- WebUI 呈现：模型路由配置可视化 + 各 worker 当前用哪个模型。
- **待设计**：见 §六 设计议题 D2。

### T3【高】同文件并发编辑冲突
- 理想：不同 worker 编辑不同文件，最后合并。需保证分批/分模块时【同一文件不被两个并发 worker 同时写】。
- 当前按模块分批已大幅降低（同模块文件在同批/同 worker），但跨模块共享文件(如契约接口)仍有风险。
- **待设计**：见 §六 设计议题 D3。

### T4【中】worker 执行层稳定性
- complex 子任务撞 50 迭代上限 / 600s 超时空产出（st-26）——部分是 T2 路由跑偏（云端慢）导致，T2 修完复测。
- stream_chunk_timeout=45s 看门狗偶发误杀慢首 token。

### T5【中】PRD 覆盖完整性（fd5470e0 VALIDATE_PLAN 暴露）
- 缺：DB DDL 建表脚本、前端页面、代码生成器、安全需求(AES/SHA512/JWT黑名单)、PRD 3.4 的 5 个发送接口。
- 根因：tech_design 模块划分偏后端，前端/SQL/安全模块没充分展开。

---

## 五、验证方法（务必遵守）

### 重启服务（用最新代码）
```bash
# 1. 杀旧进程
lsof -ti:8420 | xargs kill -9 2>/dev/null
# 2. 脱离 hermes 进程独立启动（关键：start_new_session，绝不挂 hermes 下）
cd /Users/zhangyanrui/LLM/swarm/swarm && source .venv/bin/activate && \
  PYTHONUNBUFFERED=1 .venv/bin/uvicorn swarm.api.app:app --host 0.0.0.0 --port 8420 --log-level info
# 3. 确认 health version
curl -s http://localhost:8420/api/health   # 看 version 字段
```

### 跑测试
```bash
cd /Users/zhangyanrui/LLM/swarm/swarm && source .venv/bin/activate
python -m pytest test/ -p no:cacheprovider --ignore=test/test_sandbox_integration.py 2>&1 | grep -E "passed|failed"
# test_sandbox_integration.py 需真沙箱,常规回归 --ignore 掉
# test_sandbox_pool::test_reaper_kills_expired 计时敏感,全量并发偶发失败,单跑可过
```
- **测试铁律**：接触真实存储的测试须 _test_ 隔离名 + 清理，绝不用生产标识符 set/delete。
- conftest.py 默认 SWARM_RBAC_ENABLED=false。默认登录 admin/swarm。

### 盯 e2e 任务（抽丝剥茧找 bug）
- swarm.log：`grep "task=<id前8位>" /Users/zhangyanrui/LLM/swarm/swarm/swarm.log`
- 沙箱日志：`~/.swarm/sandbox_logs/<sandbox_id>.jsonl`
- 关键看点：每子任务 L1 通过/失败、diff 字符数（5=空产出）、拒答/超时、MONITOR 剩余/失败数、模型路由 `role=worker/本地|云端`。
- 卡住主动 cancel：`POST /api/tasks/{id}/cancel`（服务被并发占满时 cancel 会超时 → 直接重启服务止血）。
- 跑验证前【必重启 swarm + 确认 health version】。

### e2e 建任务（PRD 附件式产品经理需求）
```python
# 上传附件用 Python urllib multipart（curl 引号易踩坑），POST /api/uploads → 拿 path
# 建任务 POST /api/projects/{pid}/tasks {description, auto_accept:true, uploaded_files:[path], auto_confirm_vision:true}
# RuoYi-E2E 项目 pid=5d0e9db8-d000-40f6-8df9-a929ea3c4712, 路径 /Users/zhangyanrui/LLM/swarm/e2e-projects/RuoYi
# 起始干净 commit: 0d42679bc255（回滚锚点）
```
- 跑前回滚 RuoYi：`git reset --hard 0d42679bc255 && git clean -fd <新增模块目录>`

---

## 六、设计议题（架构级改动，先出 DESIGN 拍板再编码）

### D1 shared_contract 契约先行（对应 T1）
思路：tech_design 阶段1 产出模块清单时，【同时产出跨模块共享契约】（核心接口/DTO/常量/API 路径）。
PLAN 分批时把契约作为【只读上下文】注入每个 worker，所有 worker 遵守同一份。
合并时校验契约一致性。需考虑：契约由谁定（Brain 阶段1）、worker 如何引用、冲突如何检测。

### D2 worker 本地小模型并行路由（对应 T2）
- routing 各档首选改本地小模型；并发分派时轮转/负载均衡到本地多模型槽。
- 兜底链：本地主(Qwen3.6-40B-Claude/MiniMax) → 本地次(Qwen3.6-27B-Saka) → 本地兜底(Qwen3.5-122B-A10B,64K需控输入)。
- 移除 403 的 Kimi。WebUI 可视化路由 + worker 模型占用。

### D3 同文件并发编辑（对应 T3）
- 分派前检测：并发批次内 writable 文件集【无交集】才并行；有交集的串行。
- 理想态：不同 worker 改不同文件 → 各自 diff → 合并（已有 merge_engine）。
- 共享契约文件应在契约阶段一次性定稿，不进 worker 并发写。

---

## 七、推送工作流

- **commit 时机**：每个治本修复 + 测试通过后【本地 commit】（带任务关联说明）。
- **push/tag 时机**：由用户拍板（常"全做完再推"），agent 不擅自 push。
- 版本号语义化，三处同步（__init__/pyproject/runtime health）；多批次特性中途用 patch(0.8.x)，整特性齐了才 minor(0.9.0)，不中途跳 minor。
- GitHub：`github.com/Victzhang79/Swarm`，默认分支 **main**，本机已配专用 ed25519 密钥（不支持密码推送）。
- push 前：全量回归 + 密钥扫描（git diff | grep sk-/Bearer）。
- 当前【4 个 commit 未 push】：86f803a / 3c5267b / 6116b0a / c2466bd（等用户拍板）。
- DEVLOG.md 已 gitignore。

---

## 八、关键文件索引

| 文件 | 职责 |
|------|------|
| `brain/planning_nodes.py` | tech_design(含两阶段 _tech_design_staged)、事实核验、虚假前提边界 |
| `brain/plan_batch.py` | 分批工具：group_into_module_batches/dedupe_file_plan/merge_subtask_batches/拓扑排序 |
| `brain/nodes/__init__.py` | plan 节点(含 _plan_ultra_batched 按模块分批)、handle_failure、learn_success |
| `brain/prompts.py` | PLAN_BATCH_SYSTEM(垂直切片)、TECH_DESIGN_STAGE1/2 |
| `config/settings.py` | 模型路由配置(L95-131)、provider 端点 |
| `models/router.py` | ModelRouter：get_brain_llm/get_llm_for_subtask、stream/timeout |
| `brain/nodes/dispatch.py` | 并发派发(max 4)、跨子任务上下文注入、failed_subtask_ids |
| `worker/executor.py` | worker 执行：定位/编码/L1 验证、B2 分阶段编码 |
| `types.py` | FileScope(is_writable/_path_scope_match) |
| DESIGN_plan_batch_decompose.md | 分批拆解完整设计 + P1-P6 + e2e 发现 |
