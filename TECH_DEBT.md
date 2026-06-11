# 技术债登记（Tech Debt Ledger）

本文件登记已知但暂未根治的技术债，按优先级排列。每项含：现象、影响、建议修法。
完成后从本表移除并在 commit 注明。

> 最近更新：2026-06（RuoYi 混编 E2E 验证后系统盘点）

---

## P1 — 影响正确性/可靠性

### 1. 本地模型流式 stall 韧性不足
- **现象**：`Qwen3.6-27B` / `MiniMax-M2.7-Pro`（ai.bit:3000 网关）偶发流式中断
  —— `No streaming chunk received for 120.0s (chunks_received=N)`，TCP 活着但不再产出。
- **影响**：worker 一轮 ReAct 卡 120s 才超时，拖慢任务；虽有 fallback 但接管慢。
- **建议**：把 langchain_openai 的 `stream_chunk_timeout` 调短（如 45s）让 fallback
  更快接管；或在 router 层加“首 token / chunk 间隔”双超时。属远端推理服务可靠性问题，
  我方只能增强容错（适配，不强求远端重建）。

### 2. 沙箱活动日志不持久
- **现象**：`/api/sandbox/{id}/logs` 的活动日志存在 manager 内存，进程重启即清空。
- **影响**：事后追查沙箱行为（如调试某次失败）时无据可查，只能靠 swarm.log。
- **建议**：把 sandbox 活动日志落库（store.py 加表）或写专用日志文件，按 task_id 可检索。

### 3. harness 把已存在文件误判为 create_files
- **现象**：E2E 用例2（给已存在的 ruoyi.js 加函数）被 Brain 规划进 `scope.create_files`，
  而非 `writable`。
- **影响**：create_files 不上传不读取（按“新建”处理），对“给现有文件追加内容”语义错误；
  靠 worker 容错才没出事。
- **建议**：Brain 规划阶段对 create_files 逐个校验本地是否已存在，存在则降级为 writable。

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
