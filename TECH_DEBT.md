# 技术债登记（Tech Debt Ledger）

本文件登记已知但暂未根治的技术债，按优先级排列。每项含：现象、影响、建议修法。
完成后从本表移除并在 commit 注明。

> 最近更新：2026-06-26（整体走读 · 四视角并行深审 · 根因收敛）

---

## 2026-06-26 整体走读登记 — 静默成功根因 + 40 项系统性债

> 来源：四个独立子审计并行读通 brain(13.7k)/worker(8.8k)/knowledge+models+config(9.7k)/
> 全树跨切面。**四视角独立收敛到同一根因。** ✅ = 已亲自复核原文；其余为子审计报告，
> 落地前需在 plan 阶段逐条复核 file:line。ID 形如 TD2606-X# 供 plan/commit 引用。

> ### 清偿状态（2026-06-26，Waves 0–4 + 遗漏复查，13 commit，全程 1533 passed）
> 经三视角独立遗漏复查（审 aa0f8c9..HEAD），下列标签已据复查结论校正（含 1 处自引入损坏已修）。
>
> **已修复 (FIXED)**：A1–A8 · B1 · B3 · B5/C5/M5 · B11 · B12 · B14 · B15 · B17 · B18 ·
> C1 · C2 · C6 · C8 · C14 · C16。
> **遗漏复查追加修复 (FOLLOW-UP FIXED)**：
> ① B18 自引入 P0：revision 误把 `normalize_plan_scopes`/`resolve_plan_conflicts` 返回值(bool/dict)
> 赋回 plan → state["plan"] 损坏成 dict。改为只调 resolve_plan_conflicts(原地变更)弃返回值 + 加测试。
> ② A5 旁路：plan_generation_failed 闸门只在 confirm，非 ULTRA 走 validate→dispatch 绕过 →
> after_validate 补 plan_generation_failed→confirm。③ B1：analyze 的 key_risks=list[dict] 会触
> ValidationError 静默降级 MEDIUM → _coerce_risks 逐元素转字符串强容忍。④ worker：test/verify
> 命中 infra 故障改 BLOCKED(转 transient，与 build gate 对称，原误判 capability)。
> **核查后判定已充分处理 (ALREADY-OK，校正后)**：C3（run_security_scan 在 AUDIT 意图任务真触发；
> 普通功能任务仍无安全闸门——可接受非死代码）· C13（think/fence/逐对象 salvage 真鲁棒）。
> **遗漏复查追加修复·批2 (FOLLOW-UP FIXED)**：
> ⑤ B16 双连接泄漏：6 个长生命周期 store（behavior/norms/structure/updater/memory/semantic）的
> `connect()` 加幂等守卫 `if self._conn(/_client) is not None: return`，重复 connect 不再丢弃旧连接。
> ⑥ C10 降级污染：`should_write_success` 加 `degraded_reasons` 非空 → 不写 L6 成功模式（不阻断交付
> 本身避免误伤无测试 docs 任务，但绝不学成可复用成功模式；+测试）。
> ⑦ B20 worker 端：`_resolve_project_stack` 用廉价 `compute_repo_fingerprint` 比对缓存指纹，
> 漂移（栈迁移）则整画像重探，不再盲信旧前后端裁决喂 worker。
> ⑧ C7 pool 临时沙箱孤儿：`_create_and_return` 包 try/except——create 成功后记账/注册若抛异常，
> 立即 kill 刚建的沙箱再抛，杜绝调用方拿不到引用的远端孤儿泄漏。
> ⑨ C4 烤源沙箱抹源：项目专属镜像把源码烤进 /workspace 且【不含 .git】，池复用前 clean_workspace
> 的 `rm -rf /workspace` 抹掉源码（下任务缺源编译失败），仅保留又带入上任务改动破坏隔离。两难。
> 故烤源沙箱(`_sandbox_has_source`)【不回池】(kill_sandbox 设 reusable=False)，每任务从缓存镜像
> 创建新容器（镜像层仍含源码+.m2/warmup，启动快）→ 杜绝抹源与污染两种 bug。+测试。
> ⑩ B8 L2 集成失败连坐全表：`_l2_failure_state` 原把 subtask_results 全部 key 标失败 →
> handle_failure 全量 replan，40/41 个 L1 通过的子任务被推倒重来。改为据 integration_review
> 编译输出把失败【归因】到具体子任务（写权文件出现在编译错误里 → 该文件写者），handle_failure
> 走【定向恢复】只重做归因子任务、保留成功兄弟（复用 replan 守卫模式）；归因不了才回退全量
> replan（与现状一致，绝不误连坐）。replan_count 共用熔断。+测试。
> ⑪ C9 修复只活在沙箱：L1 闸门 sandbox-first 修复（version-repair/import-repair/goimports…）
> 改的是沙箱文件，但 `_sync_from_sandbox` 只 pull-back 写权 scope、`_get_git_diff` 也只 diff
> scope 内文件 → scope 外被修复文件（典型：父 pom 版本号）回不到本地、缺席 merged_diff →
> brain 端 L2 集成在干净树上重炸。改：repair 函数返回触达文件【路径】(非仅计数) → run_l1_pipeline
> 透传 `repaired_file_paths` → executor 累积 `_repaired_extra_paths`，并入 pull-back 清单与
> git diff targets（含 difflib 基线），per-file provenance 收口两棵真值树。+测试。
> **部分处理 / 标签校正 (PARTIAL，剩余)**：
> B19（机制在但**默认仍 fail-open**：未设 env 时 Fernet key 从公开 DB URI 派生，安全是双 opt-in；
> 改默认会破坏现有部署，属【部署策略决策】留运维拍板，非代码缺陷）。
> **留待专门设计/大改 (DEFERRED)**：~~B8 · C9~~ → 已于 2026-06-26 清偿（见上 ⑩⑪），DEFERRED 清空。
> **方法固有近似/低危 latent (WON'T-FIX，理由校正)**：B13 · C12 · C15 · C18 ·
> C11（**形状校验已有**：dict/title/content/tag 白名单/priority clamp；仅语义真伪无法离线证伪）·
> C17（当前安全的真因是**每 asyncio.Task 各持 ContextVar 副本**，非"显式传 project_id"——
> query_knowledge_base 只从 ContextVar 读 project_id，勿移除该 ContextVar）。

### §0 根因主线（THE root cause）

**系统没有"未验证模型输出"与"可信内部状态"之间的类型边界；其裁决器把"验证没跑"
（`det_ok is None` / 异常跳过 / infra 串匹配 / 空 scope / 散文验收）当作软通过、退化为
信模型自报。** 因为每个节点都是 catch-log-continue，任一阶段失败都会在下游退化成"看
起来像成功"。两个多月的补丁史（version-repair / symbol 锚点 / override fallback /
Central 兜底 / `_is_infra_failure` 串表）全是把*某一个*"没验证"场景一次一个地拖回"真
验证了"那栏——补的是裁决器的**输入**，没改它 fail-**open** 的**默认值**。

**一刀根治**：裁决器默认翻成 fail-closed，把"验证是否真跑过"变成一等带类型信号
`VerificationOutcome ∈ {VERIFIED_PASS, VERIFIED_FAIL, NOT_RUN(reason)}`；`NOT_RUN`
当 FAIL（除非运维 allowlist），永不退化为信 LLM 自报。配 Pydantic schema 边界让规划链
不能静默错形。此改使整张 whack-a-mole 串表从"正确性关键"降级为"便利性优化"。

### §A CRITICAL — 静默成功核心链（fail-open 默认值的各条入口）

- **TD2606-A1 ✅ 裁决器 fail-open 总开关**：`worker/executor.py:256-267` 分支③
  `det_ok is None → passed = bool(llm_ok)`。无确定性证据时把"成功"判给模型自报。所有
  下列入口最终汇入此处。**根因，非补丁。**
- **TD2606-A2 ✅ 自检解析失败→视为通过**：`worker/l1_pipeline.py:1188-1195` JSON 解析
  失败/异常 → `{"passed":True,"skipped":True}`。且 `test/test_l1_pipeline.py:232`
  `test_self_review_llm_exception_graceful` **断言 passed is True**，把静默成功写成契约。
- **TD2606-A3 确定性闸门吞异常→返回 None**：`worker/executor.py:2390-2391`
  `except Exception: return None` → 唯一硬正确性检查点崩溃后删证据、落回 A1。
- **TD2606-A4 `_is_infra_failure` 串表丢真实编译错**：`worker/l1_pipeline.py:559-585`
  30 条硬编码串；构建非零退出含 `command not found`/`: not found`/`504` 等即当 infra 跳过
  闸门。这就是 whack-a-mole 机制本体——一张不断增长的"已知坏输出"查找表。
- **TD2606-A5 规划 LLM 失败→造空 scope "无验证"巨任务**：`brain/nodes/__init__.py:685-703`
  transient 规划错不失败，造一个空 FileScope、验收写字面"无验证"的任务 → 空 scope 使
  `expects_changes=false` → 无东西可检 → 落回 A1。降级规划静默绕过整条验证链。
- **TD2606-A6 merge rebase 超限 escalate 信号被丢**：`brain/nodes/__init__.py:2208-2234`
  + `brain/graph.py:239-263`。设 `failure_escalated=True` 但没设 `merge_conflicts`/
  `rebase_subtask_ids` → `after_merge` 路由去 verify_l2，escalate 无边承载 → 可进
  MERGE↔VERIFY_L2↔HANDLE_FAILURE 死循环烧到 recursion_limit。
- **TD2606-A7 错误"成功"零校验写进 L6 记忆毒化回路**：`brain/learn_store.py:91-169`、
  `memory/pattern_extractor.py:15-27`、`memory/store.py:580-655,799-805`。
  `should_write_success` 只查"非部分交付+复杂度档"，**不查 L1/L2/L3 真过 / 人审 ACCEPT**
  （信号就在 state 里没被调用）；被复用越多 reuse_count 越高、衰减越慢 → 错误 pattern 越
  永久。有正反馈、无解药（除手动 dismiss）的自毒化。
- **TD2606-A8 路由 fallback 可终止在不存在的模型**：`models/router.py:288-318,434-458`。
  `_get_provider_for_model` 不校验模型存在、不与 capability_store 交叉可达性校验，只
  `logger.warning`；死模型运行期错又易被判 transient → 重试同一死模型烧光预算。

### §B HIGH — 失败漂成成功 / 状态失序 / 并发安全

- **TD2606-B1 LLM JSON 全程无 schema 校验**：`brain/nodes/shared.py:_parse_json_from_llm`
  返回裸 dict，~12 处 `planning_nodes.py` 直接 `.get()/float()/Complexity(...)`，下个没见
  过的形状抛异常或静默错解。确定性泄漏类的根因边界。
- **TD2606-B2 裁决从散文 regex+子串投票**：`worker/executor.py:2309-2328`
  `re.search("L1_RESULT:(PASS|FAIL)")` 失败后数 `pass/fail/通过/失败` 子串——"This test
  will fail tomorrow" 翻盘。
- **TD2606-B3 ASSESS 失败静默沿用它本该纠正的低估**：`brain/planning_nodes.py:295-297`
  `return state.get("complexity", MEDIUM)`，且 state 无"ASSESS 被跳过"信号。
- **TD2606-B4 沙箱创建失败→静默降级本地执行→构建闸门消失**：`worker/executor.py:580-582`
  通用模板沙箱挂掉落本地，mvn/go/cargo 不存在 → command not found → 被 A4 跳过 → 假通。
- **TD2606-B5 并发共享一棵 git 工作树损坏 diff 基线**：`worker/executor.py:1237-1321,
  1743-1816`、`worker/sandbox.py:1093-1154`。dispatch `asyncio.gather` 真并发，pull-back
  写回 + `git add -N` 改共享 index + flock 只锁 reset 一步不锁 pull-back-write/diff-read
  窗口 → 06-23 记忆"疑似跨子任务同步 bug"的结构性真因。**设计让并发写者共享可变工作树。**
- **TD2606-B6 fix 循环无 no-progress 检测**：`worker/executor.py:740-811`。模型每轮吐同一
  `cannot find symbol` 会烧光每个 fix round 产同一 diff，只有 timeout 能停。开环修复。
- **TD2606-B7 构建闸门 manifest find 深度3 把"找不到 pom"当 build PASS**：
  `worker/l1_pipeline.py:603-640,1330-1370`（`:1367` elif 分支）。"工具不适用"与"定位不到
  清单"被混为一谈，都判绿。
- **TD2606-B8 ✅FIXED(2026-06-26) L2 失败连坐全部 subtask→replan 清空全部成功成果**：`brain/nodes/verify.py:
  248-255` `failed_ids = list(subtask_results.keys())`。40/41 成功+1 集成问题 → 全量重建，
  击穿"保留成功兄弟"guard。L2 失败无法定位到具体文件/子任务。
- **TD2606-B9 capability 探测 transient 把 probed 降级 default 且永不恢复**：
  `models/prober.py:342-346`、`models/capability_store.py:204` 无条件 upsert、`router.py:419`
  过滤 supports_multimodal=True → 一次 5xx 期探测把多模态/上下文窗口能力永久抹掉。
- **TD2606-B10 decay_weight 在排序公式里被约掉**：`memory/store.py:442-471,467,494-506,
  671-685`。`effective_weight/decay_weight = factor^(age/occ)`，锚点权重对排序完全无效，
  "常遇错题重振"设计是哑的；reinforce 不查 dismissed 状态致两道 guard 失配。
- **TD2606-B11 嵌入退化 BM25-only 不告知调用方**：`knowledge/retriever.py:394-408`、
  `knowledge/service.py:197`（仅总异常才置 retrieval_failed）→ Brain 以为有语义召回实则
  只关键词召回，照常规划。与 L5/L6 的零向量 fail-safe 不对称。
- **TD2606-B12 PG checkpointer 挂→静默降级 MemorySaver 破多副本 resume**：
  `brain/graph.py:597-628`。多副本下一个副本 PG init 失败，人闸 interrupt 不可跨副本恢复，
  任务永久孤儿在 CONFIRMING/DELIVERING，仅 warning 不拒绝不告警。
- **TD2606-B13 transient/capability 误分类靠散文串匹配**：`brain/nodes/__init__.py:1922-1975`
  capability 失败含 timeout 字样被判 transient（不进 capability 预算不换模型）反之亦然；
  载荷决策建在 LLM 自吐文本的脆弱匹配上。
- **TD2606-B14 单跑 worker fire-and-forget 无 kill 杆**：`worker/runner.py:178`
  `create_task` 句柄丢弃，卡死的单跑持沙箱直到自身 finally，无外部恢复。
- **TD2606-B15 reset_sandbox_manager 不重置 pool 单例**：`worker/sandbox.py:144-152`
  杀 manager 但 pool 账本仍指死 manager、borrowed 不减 → 配置 reload 后每次 acquire 退化
  throwaway 临时沙箱、持续 churn。
- **TD2606-B16 各 store 逐调用裸 PG 连接绕过连接池**：`knowledge/retriever.py:280`、
  config/sandbox_store、command_blacklist_store、secret_store、auth/store、capability_store、
  project/store 各自 `psycopg.connect`，并发下击穿 max_size；长生命周期 store 的 connect()
  无幂等守卫，双连接泄漏。
- **TD2606-B17 dedupe_subtasks 只在批量 ultra 路径，单发 plan 路径无去重**：
  `brain/plan_batch.py:249-293,370`；单发 `brain/nodes/__init__.py:632-665`。RUN6"重复脚手架
  子任务"根因仍可达于非批量路径——去重是 batch-merge 副产品而非全局不变量。
- **TD2606-B18 revision 计划绕过 validate_plan 与全部 scope 归一**：`brain/graph.py:399`
  `revision→dispatch` 直连，跳过 validate_plan/normalize/enrich/resolve_conflicts；LLM 解析失败
  造空 scope（无 allow_any）→ worker 啥都不能写 → 静默空 diff。人工介入路径反而验证最弱。
- **TD2606-B19 secret key 未设时由公开 DB URI 派生 + 生产校验是 opt-in**：
  `config/settings.py:688-733`、`config/secret_store.py:51-86`。`SWARM_ENV` 没设则跳过校验，
  Fernet 根 key 从默认公开 DSN 派生（代码自承"DB dump+repo 即可解密一切"）。安全姿态藏在
  两个 opt-in flag 之后。
- **TD2606-B20 stale project_stack JSONB 缓存在栈变更后仍被用**：`project/store.py:39`、
  `worker/executor.py:964-974`。schemaless config JSONB 无 version/staleness 字段，栈迁移
  （javax→jakarta / 加前端）未重跑 detect_stack 则旧画像当硬前提喂 worker。

### §C MEDIUM — 环境错配 / 资源泄漏 / 覆盖空洞

- **TD2606-C1 中文拒答裸子串误判**：`worker/executor.py:67-112` `"无法"/"抱歉"` 等片段
  作子串匹配，正常验证散文"原代码无法处理空值已修复"被判拒答→sticky 硬失败。重蹈英文路径
  早修过的同一类 bug，且此处更毒（不可翻盘）。
- **TD2606-C2 DEBUG 闸门跑本地 subprocess 而非沙箱→非 Python 栈永远保守失败**：
  `worker/executor.py:2393-2430` `subprocess.run(cwd=project_path)` 本地无工具链 → 永远 except
  保守失败。非 DEBUG 路径已用 sandbox-first，DEBUG 闸门分叉退化。
- **TD2606-C3 security_scan 在 worker 生命周期中从未被调用**：`worker/security_scan.py`
  精心 fail-closed 的 secret/SAST 机器从 worker 视角是死代码。需确认是否在 deliver 路径接上；
  若否则产出无任何安全闸门。
- **TD2606-C4 clean_workspace 抹掉项目镜像烤进的源码**：`worker/sandbox.py:607-641` 池复用
  清理 `find /workspace -mindepth 1 -delete`，对 `image_builder.py:282-288` 烤源进 /workspace
  的项目专属镜像 = 删掉地基源码，下次 acquire 空 workspace 只增量上传 → 缺兄弟编译失败。
- **TD2606-C5 git add -N 改共享 index 副作用**：`worker/executor.py:1774-1787` 并发下一个
  worker 的 intent-to-add 泄漏进另一个的 diff/status，污染 scope 越权检查。注释称"无副作用"实
  为真 index 写。
- **TD2606-C6 maven -pl 模块由 `f.split("/")[0]` 推导，嵌套模块错**：`worker/l1_pipeline.py:
  1262-1272` 对 `ruoyi-modules/ruoyi-system/...` 取到聚合器而非叶子 → 跑全 reactor 但只同步了
  改动模块源 → 失败；且"构建哪个模块"与"上传哪个模块源"用不同解析逻辑会不一致。
- **TD2606-C7 pool 临时沙箱异常路径泄漏**：`worker/sandbox_pool.py:228-270` create-failure /
  temp 分支引用被丢且未 release → 临时 sid 不被 kill 不进清理对账。中等把握。
- **TD2606-C8 malformed 非空 diff 解析到 0 文件→PASS**：`worker/l1_pipeline.py:1312-1317`
  含垃圾无 `+++ b/` 头的非空 diff，`empty_diff` 检测（查空串）放过它进 run_l1_pipeline → 无
  harness 项目判 True。
- **TD2606-C9 ✅FIXED(2026-06-26) fix 轮间本地↔沙箱只部分同步**：`worker/executor.py:757-758,1585-1646` 仅 JVM
  修复回传，无通用 local→sandbox 再同步 → 两棵真值树（本地 diff/scope vs 沙箱 compile/test）
  按修复类型 ad-hoc 同步，可静默分叉。
- **TD2606-C10 L2 散文验收→零测试通过且 degraded 标记不 gate**：`brain/nodes/verify.py:
  113-125` 返回 l2_passed=True + degraded_reasons，但无下游闸门读它 → 降级对 accept 闸门不可见。
- **TD2606-C11 norms_inference 未校验 LLM 输出当 norms 注入**：`knowledge/norms_inference.py:
  133-199,234-250` 本地模型臆造"StringUtils.isBlank 存在，务必复用"成高信号指令注入每个 worker
  prompt。A7 的 norms 版。
- **TD2606-C12 context 探测把下界当权威窗口持久化**：`models/prober.py:151-205` 网关接受超大
  请求时读 prompt_tokens 当窗口（实为下界，可能被服务端截断）→ 下游预算低估；且每次探测发
  ~1.2MB 体真计费。
- **TD2606-C13 positional JSON 提取**：`worker/security_scan.py:200`、`knowledge/
  norms_inference.py:153` `find("{")…rfind("}")` 遇散文里嵌套花括号即破。
- **TD2606-C14 资源/生命周期泄漏**：MemoryDecay `while True` 无 stop（`memory/decay.py:407`，
  api/app on_shutdown 不杀）；image_builder 失败无 finally 清 `/tmp/swarm-build` + `docker run
  -d` 探测容器（`worker/image_builder.py:660,666-782`）；updater.close 不取消在飞
  `_depgraph_tasks`（`knowledge/updater.py:443,277`）。
- **TD2606-C15 lossy regex 符号重索引使 Layer A 漂离真值**：`knowledge/updater.py:889-957`
  仅 Python 用 AST，Java/Kotlin/Go/TS 行 regex 漏多行签名/注解/泛型，每次增量删-重插累积漂移，
  consistency 只比 mtime 不比符号保真度。
- **TD2606-C16 reload_config 不刷 secret/sandbox 缓存**：`config/settings.py:737-750`、
  `config/secret_store.py:38-41`。.env 改 + reload 后旧 key 缓存最长 30s，新 base_url 配旧 key。
- **TD2606-C17 ContextVar 跨项目隔离潜在脚手枪（当前安全）**：`knowledge/service.py:44-54`、
  `brain/nodes/dispatch.py:246`。当前靠"检索显式传 project_id 参数"幸存；若未来同 loop 并发跑
  不同项目 Brain 任务且不各自包 asyncio.Task，`_current_project_id` last-writer-wins 即串。
- **TD2606-C18 co-occurrence boost 把巨提交噪声当信号**：`knowledge/retriever.py:657-694`、
  `knowledge/behavior_store.py:159-197` 一次大重构产 780 对 co-occurrence，使无关文件"常一起改"。

### §D 测试理论（test theater）

212 个测试文件、90 个用 mock。L1 裁决测试只喂 `det_ok=True/False/None` 验真值表，**从没把
真的坏构建跑过真实流水线断言 FAIL**；构建闸门（mvn/go/cargo 经 `_run_l1_command`）与
`_is_infra_failure` 跳过路径无任何"喂真坏构建断言 FAIL"的测试。最坏：`test/test_l1_pipeline.py:
232` 把静默成功 encode 成契约。**这套 1000+ 测试抓不到 silent-failure 这一类——因为它们 encode
了它。** 落地 fail-closed 时必须同步把这类测试反向（NOT_RUN→断言 FAIL）。

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
- **M1** ✅已修(commit 见 git log) worker/executor.py `_run_failing_test_gate` 异常 `return True` → 改 `return False`(保守失败，不把"验证不了"误判"已修复")。
- **M2** ✅已修 SWARM_WORKSPACE_ROOT 进程级 os.environ 并发覆盖 → tools/paths.py 改 ContextVar 隔离(set_workspace_root，保留 os.environ 兼容子进程)，runner/worker.runner 同步改。
- **M3** ⏳已评估暂留 worker 大量同步 subprocess(git baseline/diff/reset、l1_pipeline npx tsc)跑在事件循环线程并发阻塞；_reset_scope_to_head 持 flock 卡整进程。判定：这是【性能/并发优化，非正确性 bug】(不产出错误结果，只是高并发慢)，包 to_thread 涉及大量调用点、回归风险高 → 留待真有并发性能瓶颈时专门处理(优于现在大面积低收益改动)。
- **M5** ✅已修 secret_store.get_secret decrypt 失败与 miss 同等静默回退 → 拆开：miss 静默(预期)，decrypt 失败(key 轮换/密文损坏)升级 logger.warning 显式告警。
- **M6** ✅已修 多处 detail=f"...{e}" 透传 → 内部基础设施错误(project/sandbox/task 创建销毁 6 处)泛化对外消息 + logger.error(exc_info) 记详情；用户输入纠错类(无效正则/目录路径/not found 4 处)保留。
- **M8** ✅已修 /api/auth/login 无限流 + 用户不存在跳过 PBKDF2(计时侧信道) → ① _LoginThrottle(用户名+IP 失败计数，5 次/5 分窗口锁 5 分，429+Retry-After)；② authenticate 用户不存在也跑 _DUMMY_PASSWORD_HASH 的等价 PBKDF2(常量时间)。

### 低危 / 清理
- brain/merge_engine.py:26 auto_resolved 死字段；:413-441 并发插入顺序不确定(当干净合并)。
- worker/sandbox.py:1251-1281 遗留 SandboxPool(带泄漏)应删除。
- models/prober.py:162 给每个模型发 ~1.2MB 探测体，被接受时实际计费 → 缩小探测体。
- api/app.py 用已废弃 @app.on_event，未来 FastAPI 会移除 → 迁移 lifespan。

### 已有 WALKTHROUGH_REPORT.md 中仍未修(knowledge/memory/project 域)
随机向量嵌入兜底、hash() 做 Qdrant point ID(PYTHONHASHSEED 随机化)、L5 批量衰减漏 occurrence_boost、retrieve_for_brain 检索副作用自增权重(违 CQRS)、retry_pending_embeddings 无自动调度。

