# 技术债登记（Tech Debt Ledger）

本文件登记已知但暂未根治的技术债，按优先级排列。每项含：现象、影响、建议修法。
完成后从本表移除并在 commit 注明。

> 最近更新：2026-06（多接入点重构 + 通知渠道 + 发布前 CTO 走查）

---

## P1 — 影响正确性/可靠性

> ✅ 本节 3 项已于 2026-06 全部根治，见文末「已根治」。原文留作记录。

### 1. 本地模型流式 stall 韧性不足 ✅ 已修
- **现象**：`Qwen3.6-27B` / `MiniMax-M2.7-Pro`（ai.bit:3000 网关）偶发流式中断
  —— `No streaming chunk received for 120.0s (chunks_received=N)`，TCP 活着但不再产出。
- **影响**：worker 一轮 ReAct 卡 120s 才超时，拖慢任务；虽有 fallback 但接管慢。
- **修复**：`ModelConfig.stream_chunk_timeout`（默认 45s）配置化，两个 provider 都传给
  ChatOpenAI，远端 stall 时尽早中断 → with_fallbacks 更快接管。

### 2. 沙箱活动日志不持久 ✅ 已修
- **现象**：`/api/sandbox/{id}/logs` 的活动日志存在 manager 内存，进程重启即清空。
- **影响**：事后追查沙箱行为（如调试某次失败）时无据可查，只能靠 swarm.log。
- **修复**：`append_activity` 写穿到 `~/.swarm/sandbox_logs/<sid>.jsonl`（追加写、不碰
  DB 热路径）；`get_activity` 内存缺失时从 JSONL 读回。重启/kill 后仍可追溯。

### 3. harness 把已存在文件误判为 create_files ✅ 已修
- **现象**：E2E 用例2（给已存在的 ruoyi.js 加函数）被 Brain 规划进 `scope.create_files`。
- **影响**：create_files 不上传不读取（按"新建"处理），对"给现有文件追加内容"语义错误。
- **修复**：WorkerExecutor 启动即 `_normalize_scope_create_files`：本地已存在的
  create_files 项降级为 writable。幂等、无副作用。

---

## P1(新增) — 语义检索因无嵌入服务而退化

### 7. 无可用 embedding 服务 → 语义检索退化为随机向量 ✅ 已修
- **现象**：`_embed_texts` 三级回退全不可用 → 落到随机向量；query 端 SemanticIndexer/
  MemoryStore 用零向量占位 → 语义检索是噪声。
- **修复（2026-06，用户提供专用服务）**：接入 ai.bit:8082/v1(bge-m3 embedding,OpenAI
  兼容) + ai.bit:8081/rerank(bge-reranker-v2-m3, {query,texts}→[{index,score}])。
  新增 `knowledge/embed_client.py` 统一客户端(sync+async,按 batch≤32 分批避 422)；
  SemanticIndexer/MemoryStore/preprocess/reranker 全切真服务。配置 `SWARM_KB_EMBED_
  BASE_URL`/`SWARM_KB_RERANK_URL`(默认指向 ai.bit)。实测：预处理 3525 符号真向量入
  Qdrant(无 422/无随机回退)；检索"字符串判空"→StringUtils 0.61、"日期格式化"→
  DateUtils 0.92，相关性恢复正常。

---

## P2 — 工程化/可维护性

### 4. 风格债：ruff E501 行超长 254 处
- **现象**：`ruff check .` 报 254 个 line-too-long、64 个 E402（多为有意的惰性导入）。
- **影响**：纯风格，不影响运行；但拉低信噪比。
- **建议**：择期统一 `ruff format` + 针对性放宽/重排；E402 中确属惰性导入的加 `# noqa: E402`。

### 5. 测试内未用变量（F841 共 5 处）
- **现象**：test/ 下若干 `d = ...` 等赋值后未用。
- **影响**：测试可读性，无功能影响。
- **建议**：清理或改 `_`。

### 6. mvn 首次编译无依赖缓存
- **现象**：全新池沙箱 `.m2` 为空，首个 Java 任务 `mvn compile` 需下载全量依赖（数分钟）。
- **影响**：首个 Java 任务慢；池复用后 `.m2` 预热则快（实测复用后 2-3s）。
- **建议**：模板镜像预置常见依赖的 `.m2` 缓存，或挂载共享只读 `.m2` 卷。

---

## 已根治（2026-06 E2E 修复，留档备查）
- L1 确定性闸门补齐 Java/Go/Rust 编译（原只编 py/js）
- 沙箱同步构建清单（pom/gradle/go.mod）+ 编译型语言同步整模块源码
- webui 读沙箱文件/列目录改 shell 端点（原走 Jupyter → 语言镜像 502）
- 工具输出硬上限防 ReAct 上下文爆炸（196k 顶穿）
- diff 基线改用 git HEAD（防本地工作副本被前序运行污染 → 假通+重试死循环）
- diff 比较前归一化行尾 CRLF→LF（防整文件 churn 垃圾 diff）
- 多模块 Maven 构建按改动模块限定 `-pl <mod> -am`
- 构建/测试闸门工程文件缺失时优雅跳过（不误判产出不合格）
- 空 diff + 期望有产出 → 确定性判失败（杜绝“没干活”假 DONE）
- trivial 迭代上限 12→30 + 撞 recursion 上限优雅交确定性闸门裁决
- embedding 端点改用配置 local_base_url（原硬编码 localhost:3000）
- **池化后孤儿沙箱泄漏**（2026-06 修）：①硬重启/崩溃跳过 shutdown drain → 启动
  时 `_sweep_startup_orphans` 清扫残留；②kill_by_task 不告知池 → `pool.forget`
  对账 borrowed 计数 + 清死引用；③trivial 路径脏沙箱误标 reusable=True → 显式设
  `_l1_passed_flag`；④孤儿检测器误判 pool-idle 为孤儿 → 排除 source=pool-idle。

## 已根治（2026-06 多接入点 + 通知渠道发布，留档备查）
- **模型 provider 写死 + 按名猜路由**：重构为多接入点（provider 一等公民），
  `provider_for_model` 按显式 `model_providers` 映射路由，启发式仅兜底；老扁平字段
  由 `_effective_providers()` 合成两接入点，零迁移向后兼容；10 个常用云端预置目录。
- **设置 tab 配置可设性**：热池开关、模型接入点、通知渠道全部从只读/env-only 改为
  Web 可视化增删改 + 即时生效（写 .env + reload + 内置 id 同步回写老字段保 /api/models 兼容）。
- **外部通知单一注入点**：`store.create_notification` 写库后触发 hook（解耦，store 不依赖
  httpx），`api/notify.dispatch_notification` 按 enabled+事件过滤推送多渠道；hook 用
  `run_coroutine_threadsafe` 投主 loop（避免 skill#16 跨 loop 陷阱）+ future 异常回调可见。
- **审批事件接入统一通知**：approve/revise/reject 原只发旧单 webhook notify()，未进新多渠道；
  改为同时走 store.create_notification（→ 铃铛 + hook 多渠道）+ 保留旧 notify() 兼容；
  NOTIFY_EVENT_TYPES 补 task_approved/revised/rejected 供 UI 订阅。
- **沙箱集成测试断言漂移**：`test_5_build_tools_sandbox_mode` 断言旧输出格式 `"sandbox exit
  code 0"`，而 build_tools._run 实际输出 `"✅ (sandbox 0)"`（格式演进后测试没跟上）→ 真跑假
  失败。修正断言为 `(sandbox 0)`。注：reachability 门禁(`_sandbox_reachable` + pytestmark
  skipif)其实早已存在并正确，原 TECH_DEBT#8 对"缺门禁"的判断有误——真因是断言漂移。
- **Q4 交互式渐进规划 Agent**：plan 阶段原"一次性 LLM 出全量 DAG，零澄清/方案/评审"过于简单。
  在 brain LangGraph 增量加规划子图（clarify 多轮自适应澄清≤5 / assess 澄清后定级 /
  tech_design 技术方案+接口先行 / review_design 人工评审+打回≤3 / elaborate 上下文预算
  150k+INVEST 自检），微任务极速通道，依赖驱动并行(已有)，LangSmith 上报，规划产物持久化
  可追溯，前端多轮问答/评审/回看 UI。详见 docs/Q4_Planning_Agent_Design.md。
  踩坑留记：LangGraph interrupt 节点 resume 会从头重跑——interrupt 前的 LLM 调用须确定，
  否则二次判断会丢用户答复（测试用计数器 mock 复现过假失败，确定性 mock 即通过）。


## 走查报告 2026-06-17 — 中/低危待办（严重+高危已修，见 swarm_走查报告 勾选）

> 已修：S1-S5 全部、H1-H9 全部、M4(pool min>max)、M7(项目根敏感目录黑名单)。
> 下列为中危需较大改动/观测性优化 + 低危清理，排期处理：

### 中危（M）
- **M1** worker/executor.py:1873-1909 失败测试闸门在沙箱 kill 后本地 shell=True 跑，异常 `return True` 当 PASS → 未修 bug 被误报已修。修法：本地降级路径异常应 return False(保守失败)，非 Python 栈不在本地跑。
- **M2** runner.py:40-45 把 SWARM_WORKSPACE_ROOT 写进进程级 os.environ，并发跑不同项目互相覆盖工作根。修法：改 ContextVar 或随任务传参（同 S1 思路）。
- **M3** worker 大量同步 subprocess.run(git baseline/diff/reset、l1_pipeline npx tsc)跑在事件循环线程，并发互相阻塞；_reset_scope_to_head 持 flock 卡整进程。修法：to_thread 包裹 + 细化锁粒度。
- **M5** config/secret_store.py:151-164 decrypt 失败(如 key 轮换)与"不存在"同等当 None 静默回退 .env 旧值，难排查。修法：区分 decrypt 异常与 miss，前者告警。
- **M6** 多处 detail=f"...{str(e)}" 把内部异常/路径/DB 错误透给客户端(sandbox.py:245 等)。修法：对外返回泛化消息，详情仅记日志。
- **M8** /api/auth/login 无限流/锁定，配合默认账户可暴破；auth/store.py:294 用户不存在跳过 PBKDF2 → 计时侧信道枚举用户名。修法：登录失败计数+锁定；用户不存在也跑等价耗时 PBKDF2(常量时间)。

### 低危 / 清理
- brain/merge_engine.py:26 auto_resolved 死字段；:413-441 并发插入顺序不确定(当干净合并)。
- worker/sandbox.py:1251-1281 遗留 SandboxPool(带泄漏)应删除。
- models/prober.py:162 给每个模型发 ~1.2MB 探测体，被接受时实际计费 → 缩小探测体。
- api/app.py 用已废弃 @app.on_event，未来 FastAPI 会移除 → 迁移 lifespan。

### 已有 WALKTHROUGH_REPORT.md 中仍未修(knowledge/memory/project 域)
随机向量嵌入兜底、hash() 做 Qdrant point ID(PYTHONHASHSEED 随机化)、L5 批量衰减漏 occurrence_boost、retrieve_for_brain 检索副作用自增权重(违 CQRS)、retry_pending_embeddings 无自动调度。

