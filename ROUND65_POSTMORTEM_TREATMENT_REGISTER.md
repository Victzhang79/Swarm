# Round65 复盘 + 治本登记册（2026-07-17）

## 一、轮次事实

- task `8cc0c907-9676-4b0a-a59f-63b350bd3fce`，2026-07-17 00:14 起跑，00:33 用户拍板取消（存活 ~20min）。
- 载荷 = v0.9.57 + round64 治本 6 笔本地提交；录制 `cassettes/round65/llm-78906.jsonl`（4 次调用全录）。
- 死于 TECH_DESIGN：stage1 219s 产出 **2 个物理模块**（`ruoyi-alarm` est_files=92 + `ruoyi-alarm-sdk` est_files=12）；
  sdk 53.5s 完成；`ruoyi-alarm` 单流 ~28.6k chunk 在 500s 超时截断（模型健康、内容稳产、未 stall），
  第 1 次重试同构进行中即被取消——3 次必然全超时 → 整模块 file_plan 丢失 → 确定性死亡。

## 二、定性（用户质疑「只有 2 模块不合理」的答案）

1. **云端没病**：stage1 输出质量好（fact_issues 三条全有据：Thymeleaf 探测正确/识别前轮残留建议归并/JWT 存疑待核）。
   只切 2 模块是**我方 STAGE1 提示词逼的**（「一个功能域尽量落一个模块、勿按子功能拆多模块」——
   round44/57「逻辑模块≠物理路径」治本的矫正面）。功能没丢，全挤进了一个 92 文件的大模块。
2. **真病根 = stage2 无分片**：单模块单次 LLM 调用枚举全部文件，文件数↑→响应长度↑→撞单调用超时，
   重试同构必复现。提示词方向（少物理模块）本身是对的，管线必须扛得住大模块。
3. **新发现：知识层跨轮残留**（stage1 自己检索出来的）：同 project_id 下 Qdrant 540 个 alarm 点 +
   PG 286 个 alarm 符号 / 68 个文件，20+ 种互相冲突的模块布局 = 多个失败轮碎片堆叠。
   写入源 = `dispatch._feedback_to_knowledge`（子任务 DONE 后无条件回灌，同轮内检索新文件所需，不能砍）；
   三清/基线重置只清磁盘，知识层是盲区——round47「毒残留被当权威」的知识层变体。

## 三、治本账

| # | 内容 | 状态 | 提交 |
|---|---|---|---|
| T1 | stage2 大模块 file_plan **分批续写协议**（批上限 30/排除清单续写/空批-0新增确定性收敛/批失败只重试该批/超时与 finish_reason 截断自适应缩批/触顶 incomplete 机读账/失败预算双轨 连续3+累计8） | ✅ | `edf548f` |
| T1 复核 | 对抗双复核 2 批：reviewer 1 CONFIRMED HIGH（off-schema 假收敛）+ 猎手 4 CONFIRMED HIGH（冲突复读静默/触顶不机读/终身预算惩罚大模块/json_repair 幻影残路径）+ 3 MED（est_files 解析/50% 阈值盲区/路径别名）——全治全锁（14 测试） | ✅ | 同上 |
| T2 | 知识层跨轮残留治本：`store.purge_project_knowledge`（外科清 kb_* 9 表，单一事实源常量与 delete_project 共用；保 projects/task_records/mem_* 经验层）+ `scripts/e2e_purge_project_knowledge.py`（PG+Qdrant 配对清理+POST /preprocess 重建等就绪，fail-loud）+ reset 脚本第 6 步接线（gitignored 本地工具）+ runbook §2 | ✅ 待复核收尾 | 本条目提交 |
| T3 | 推演 + round65b 起跑 | 待 T1/T2 全绿 | — |

## 四、记录性取舍与残量观察面（round65b 盯）

- **每模块 +1 次空批确认调用**（复核 R-2）：确定性完备 > 省一次短调用，故意保留。
- **50% 完备性阈值盲区**（猎手 F5）：1-49% 欠产出无 WARNING，靠下游覆盖闸兜底——这是本机制的探测天花板，成功日志已带 est 原值供对账。
- **无元数据的静默截断**（猎手 F7 残量）：finish_reason 拦截是尽力而为面；录制带已含 finish_reason（R64-T6），round65b 抽查。
- **模块级墙钟上限**从 3×500s 升到最多 (10批+8失败)×500s（复核 R-3）：有界、批多为产出型短调用，接受。
- gather 无 return_exceptions（猎手 F8，既往债）→ harness task #47 排下一批。
- round65b 观察面新增：`[TECH_DESIGN-STAGE2] 批 N → +K 文件` 分批收敛曲线；stage1 不再检索到 alarm 残碎路径；`stage2_incomplete_modules` 应恒空。

---

# Round65b 段（2026-07-17 01:27-02:51，task e00bc84c）

## 疗效确认（两大治本 live 全兑现）
- T1：ruoyi-alarm 163 文件（自估 95）9 批/11 次调用零超时收敛；空批确认超时 1 次被双轨预算+缩批正确吸收；无触顶无 incomplete。interface 模块 8 文件自估全中。
- T2：purge-kb 首跑清 PG 6929 行 + Qdrant + preprocess READY 轮询正确识别；ANALYZE 检索 struct=25/semantic=20（重建基线）+ mistakes=5/successes=5（经验层健在）。
- 进展面：tech_design→CONTRACT_MERGE（2/2）→REVIEW 通过→PLAN-BATCH 10 批启动——死点比 round64/65 深两层。

## 死因（新前沿）
FAILED@PLANNING `token_budget_exceeded`（02:51，无已完成子任务）：R38-A 预算按【模块数】
弹性（2 模块→1.1M，plan 顶格 577.5k），而 T1 分批协议+「少物理模块」导向后规划成本按
【文件数】走——171 文件 = 13 次 stage2 调用 + 10 个 plan 批（输出 42 万 token 为大头），
plan 阶段 spent 555k 烧穿顶格，10 批只跑完 ~6 批即 hopeless。round64 未死此处纯因它模块多
预算大。**T1 治超时把成本挪进了预算模型的盲区**。

## 治本
- R65B-T1：文件规模二级弹性——STAGE2 聚合后 widen_budget(base+per_module×n+
  per_planned_file×files)，新配置 max_task_tokens_per_planned_file=4000（0=关，
  SWARM_MAX_TASK_TOKENS_PER_PLANNED_FILE 可调）。标定=round65b 需求 ≈850k 反推 4k/文件
  （171 文件→1.784M→plan 顶格 936k）。决策记录：续批提示瘦身**不做**（cloud out 422k vs
  in 198k，输入瘦身收益低且有路径规律丢失风险）。

## R65B-T2/T3 段（源码嵌入 + 检索基线诚实化）
- T2 purge 曝光先天缺口：preprocess 只嵌符号签名，源码全文层历史上靠失败轮 worker 碰文件顺带长出（连同幻影）。治本=preprocess 逐文件 reindex_file_atomic（与增量同管线同语义）+ 资产/三方件栈中立排除（static/vendored/minified，实测 135/624）+ 服务级 vs 单文件异常分类 + readiness/purge 脚本双闸（猎手 4 CONFIRMED 全治）。live 实测：489 文件 7806 chunks 重建 READY。
- ★重定基线（非静默降标）★：旧 0.75 地板标定于不可复现偏置态（0.955=纯业务 Java 子集）；诚实可复现 KB 上中文查询被模板抢占稠密候选，实测 0.364/0.500。新地板 0.30/0.42 贴基线守回归；0.75 目标随 R65B-T3 战役（真混合候选并集：bm25_only_search∪稠密→融合重排 + 类型加权 + gold 集复审，task #51）达成后回调。
- 复核记录：曾设计项目级语义道代际 prune，猎手实证与增量 updater 竞态（会删并发新鲜 chunk）→ 改逐文件 write-then-prune，项目级原语不提供。

## round65c 段（首入执行深水区：死于执行层连坐，规划层三死点全过）
- 进展面：R64-EVIDENCE sql/ 降权✓ G1 覆盖重试+外科补齐✓ T8 上游 fail-fast 4 次命中✓ 首次增量 MERGE 零冲突✓ L2 终态闸✓——round65/65b 两个死点 live 全过。
- 死因二定案（双缺陷合谋 → 102/107 连坐放弃 → 空派发被读成「全部完成」→ 假交付）：
  - **#52 pom 权威模板双毒株**：(a) `_inject_templates_into_pom_owners` 对**既有** pom 也无条件注入「原样写入」全量模板（主入口 1595-1615 有 CREATE-only 闸，owner 通道没有；`_deterministic_pom_template` 从不读模块基线 pom）→ worker 拿模板整体覆写毒化 reactor；(b) `MERGED_DUP_DELIM` 机器注记连同 dup 模板围栏原样拼进 description，只在签名路径剥离，worker 出口裸奔。
  - **#53 连坐失守三连**：st-1-2 0.8s BLOCKED 于 #12 种子闸（stub 没铺 alarm-interface/pom.xml）→ failure.py 把 stub 完成的 give-up 也算死上游 → `_transitive_abandon` 102/107 → dispatch_remaining=[] → after_monitor 打「全部完成」→ 带病 MERGE → L2 挡下假交付。
- 治本八刀（全 test-first，锁 test_r65c_plan_governance.py 10 只）：
  - #52：owner 通道既有 pom 改「最小增量修改铁律」文本+依赖片段（**零可解析依赖也必发护栏**——猎手 CONFIRMED HIGH）；聚合器 exists 措辞闸；dedupe 只剥**尾部**围栏块（循环剥净不误伤正文）；worker 双出口 `strip_machine_annotations`（导入失败 fail-open 且 logger.error 留痕）。
  - #53：修① 死上游豁免收紧为 **give_up_mode=="stub" 且 l1_passed** 才豁免（reviewer CRITICAL：裸 l1_passed 会把 revert 占位也豁免，round12/13 连坐判官翻红实证→已治+revert 对照锁）；修③ 连坐规模闸 `>max(10, 25%×计划)` → escalate 而非静默清盘（带机读 degraded_reason）；修④ after_monitor 有放弃时打「PARTIAL 交付，绝非全部完成」WARNING；修⑤ monitor 三本账（L1过/失败/放弃）。
- 质量闸：双复核逮 1 CRITICAL + 1 CONFIRMED HIGH 全治；全量 4666/0/0（skip 5）；revert-check 红面 7/10（余 3=旧行为对照锁，设计使然）→ 复绿 27/0/0。
- 遗留登记：#54 stub provenance 完备性（种子闸要求的 upstream_artifacts 覆盖）；LOW×2（owner 注入措辞 parent-version 仅 CREATE 相关=表述瑕疵；l1_pipeline.py:2980 自检提示词用原始 description=非写文件路径）。

## 既往债清偿批（round65d 起跑前，用户拍板「先清债再评估起跑信心」）
- 首次 round65d 起跑（task 31989311）在规划早期（~2min）被用户叫停取消：登记册尚有
  #47/#49/#51/#54 未清。本批四债全清后再做起跑条件整体评估。
- **#47 stage2 gather 兄弟连坐**：`_gen_one_module` 的 try 覆盖 LLM 调用，但
  `_await_token_admission` 在 except 块之外——它一抛就逃逸出协程，gather 无
  return_exceptions → 整个 stage2 崩、健康兄弟模块产出全丢。治=return_exceptions
  隔离+逃逸异常映射 `unhandled:` 失败走 stage2_failed_modules 对账（CancelledError
  关停语义原样上抛）。锁=test_staged_unhandled_escape_does_not_kill_siblings。
- **#49 无模块回退零 widen**：`if not modules:` 直接 return 在两级弹性之前——回退
  路径其实有规模信号（stage1 自带 file_plan 长度）。治=回退路径按
  base+per_planned_file×files 放宽（窄 try 隔离）；既有「无 per-file 配置不放宽」
  锁语义收窄如实改述。锁=test_tech_design_fallback_widens_by_filecount。
- **#54 梯三桩 provenance 完备性**（round65c 死链触发端）：`_generate_compile_stub`
  的 _CODE_EXT 过滤只写代码文件，下游种子闸要求的 pom/配置永远缺席→桩「成功」但
  下游永堵（#53 修①后还会反复撞闸烧预算）。治=桩硬覆盖目标=代码文件∪【下游
  upstream_artifacts 声明∩上游足迹】（非代码仅声明才纳入，不乱碰构建文件原则保住）
  +写后完备性闸：required 有缺→清半桩回退 revert 诚实连坐。复核确认顺手治了潜在
  豁免洞（不完整桩以前会被 #53 stub 豁免误放行）。
  猎手二轮逮 2 CONFIRMED HIGH 已整改：①revert_failed 信号被丢弃（半桩毒树账面写
  「已清」）→l1_details.revert_failed 机读留痕+摘要如实+ERROR 硬告警；②required
  集用弱归一器（'./'/ 反斜杠漂移=R41 实证真病→闸静默 no-op 零留痕）→两侧过权威
  _norm_scope_path+声明/匹配计数留痕。
- **#51 检索质量战役·第一阶段**：真混合候选并集落地（BM25 关键词臂独立供给候选，
  SWARM_KB_HYBRID_UNION_SCROLL_LIMIT=5000，0=关）+bench 测量口径修正（query_terms
  原来传整句=BM25 维度一直无效测量，改生产同款 _extract_keywords）。实测
  0.500/0.364→**0.591/0.432**；池上限 10000 全库覆盖反而更差（0.545/0.386）=瓶颈
  已转移到 rerank 池内噪声竞争（sql/模板富中文块挤 gold）。地板上调 0.38/0.50。
  第二阶段（gold 集复审+类型加权，防 22 题基准过拟合）留 #51 继续，非起跑阻断。
- 猎手三轮（并集/整改面专审）再逮 2 CONFIRMED 已整改：①HIGH=并集候选 score 占位 0.0
  会被既有 semantic_score_threshold 旋钮（一开）整臂静默过滤（范畴错误：拿向量阈值
  筛无稠密分的候选）→ kw_union 标记豁免+锁；②连坐放弃下游 pop 后 revert_failed
  无迹可查 → ERROR 硬告警+degraded_reasons（reducer 通道）机读账
  cascade_revert_failed:<id>:<files>+锁。LOW 留档：l1_details.revert_failed 目前无
  下游消费者（L2 只能间接兜真编译毒），后续可接 runner 终态摘要。

## R65B-T3 二阶段结论（gold 复审+类型加权面，2026-07-17）
- **gold 复审（真弹）**：22 题集里 3 条子句引用 306/320/335.md——旧偏置 KB 的外采文档，
  项目里根本不存在=永不可满足的死子句。改写为仓内真实证据（Mapper.xml / DataScopeAspect /
  ShiroConfig anon 链）。实测 Hit@5 0.591→**0.636**、Recall@5 0.432→**0.455**（连续 3 轮稳定）。
- **两项负结果如实记录**：
  ① rerank×hybrid blend（终排归一加权 ensemble）：为救「gold 以全池最高 hybrid_score 仍被
    rerank 踢出 top-5」（r-05 实测）而做，但全权重扫描（0.2/0.4/0.6）均整体有害
    （0.636→0.545）——hybrid 分在 sql/模板噪声上同样虚高。按「无实证价值不上船」删除。
  ② 关键词臂每文件多样性 cap（防 sql dump 单文件霸榜）：bench 中性（misses 不变），
    保留为结构性护栏（param 默认 None，并集调用点 cap=3）。
- **剩余 5 MISS 定案**：reranker 模型对中文概念查询系统性偏好文案富集块（rr=0.96 给
  main.html 噪声）+查询歧义（startPage 在 50+ 文件出现，IDF 天然低）。属 reranker 模型
  评估/换装工作面——编排层继续调参=对 22 题过拟合，明确不做。
- 战役累计：0.500/0.364 →（一阶段并集+bench 口径修正 0.591/0.432）→（二阶段 gold 复审
  0.636/0.455）。地板 0.54/0.40。0.75 目标移交 reranker 模型评估任务（非起跑阻断）。

## round65d 终局（2026-07-17，task b583df8f，PARTIAL 1/94 @执行期31min）
- 规划期零打回穿越（#52 三模式 pom 注入首验✓/覆盖矩阵 98/98✓/pom 写权唯一✓）；执行期 317 模型调用全本地零云端。
- 死因链：st-26 trivial 判死无 reason（validate exit=0 后 79ms 静默 False）→ HANDLE_FAILURE 4 失败仅处置 3，st-26 静默丢弃永不重派 → C9 汇流单点 94 任务饿死 → R13-4/R46-3/DELIVER 三级止损全对 → 诚实 PARTIAL。
- 新登记：#60 主治 HANDLE_FAILURE 处置完备性铁律 / #57 DAG 消费者→生产者补边+生产者优先调度 / #58 观测缺口+st-26 判死点 / #59 plan 注入离线调试通道（大脑离线闸）。
- 复用资产：checkpoint plan（94 st，base=0d42679）+ cassettes/round65d 全程录制。

## round65d 三路交叉印证终版定案（2026-07-17，方法论首用即破案）
四方对齐（A=swarm.log 全读/B=沙箱 jsonl 逐事件/C=产出+plan 对抗复核/主线程复审），死因链五层：
1.【规划期埋雷】st-26 四面矛盾：desc(jackson+httpclient5)↔R58-3 模板(okhttp+fastjson2+lombok+slf4j+反向 ruoyi-alarm 依赖)↔verify(grep jackson/httpclient)↔acceptance(3条 jackson 系 vs 4条 okhttp 系互斥)。T5 10:39:44 明知互耗环"双向都不注入"，模板通道仍注入反向依赖。
2.【执行期冤杀】worker 117s 交出 jackson∪okhttp 并集 pom=矛盾卷唯一最优解（沙箱 ev15）→H1 覆写销毁（ev17）→R56-5 剪幻影依赖（ev51）→mvn validate exit=0（ev52）→grep jackson exit=1 判死（ev53，l1_pipeline.py:3986 静默 return False）。H1×R56-5×verify 三机制互咬=round60 拆台死型。
3.【处置黑洞】10:45 HANDLE_FAILURE 4 失败只处置 3，st-26 掉账零日志；10:59 被冻"完成态"；同次恢复宣称重派 4 实派 0（C9 边自阻，补写权/换模型处方全落空）；25min 无告警至 R13-4。
4.【交付倒挂】冻结完成态的 L1-fail 产物进 MERGE(4 diff)→毒树四株落盘（SCOPE_OBJECTION 拒工书当源码/SysLoginController 必死 typo/LoginService 无依赖 import/8 控制器 import 缺失模块）；round36 自愈把 java.util.Map 误诊内部类型下毒指令。
5.【模型无罪】worker 全程有效（st-26 并集解/AlarmApp 一次成型/2FA 单调收敛），11/14 撞迭代上限仍产出连贯实现；73 WARNING 0 ERROR；止损链 R13-4/R46-3(指纹 25035cd3×3)/DELIVER 拒放行全对；执行期 317 调用全本地零云端。
治本登记 8 案（treat 顺序）：#61 子任务自洽闸+H1 verify 同源【主治】→ #60 处置完备性铁律+L1-fail 禁入完成态+处方核销【主治】→ #62 交付面闸 MERGE 只收 l1_passed → #57 DAG 消费边+任务书空心(54/94 upstream 空)+blocked_on=[] → #63 round36 stdlib 误诊+retry_guidance 污染 → #58 观测批(verify_failed 不出沙箱/拦截幻觉 PASS 冤案措辞/bootstrap WARN 口径/401 静默降级) → #64 效率批(批门闩滚动派发/迭代预算/ledger 取消结算/连坐先查可派发) → #59 plan 注入端+SWARM_BRAIN_OFFLINE（工具，可先行）。
复现资产：scratchpad/plan_b583.json（94 st fixture）+ cassettes/round65d 全程录制 + 沙箱 jsonl 已归档待三清。

## #61 R65D-T2 已治（2026-07-17，本地提交）

**取证修正登记描述**（比登记时更深一层）：反向依赖非"T5 明知互耗仍注入"——round65d 全程互指
WARNING 零触发。真相=跨遍陈旧模板冻结：10:25:05 第一遍 T5 单向 readable 证据推出
interface→alarm 烤进 st-26 模板；10:39:44 第二遍推导该边已消失，但 R58-3 守卫
「描述含'权威 pom 模板'→跳过」把毒模板冻结；MODIFY 形态守卫更是完全失效（st-42 铁律块
×2 重复追加，fixture 实锤）。考卷矛盾源头=契约（okhttp 系）与 plan_batch LLM（jackson 系）
两份云端产物从无对账：模板跟契约、verify 跟 LLM 描述、acceptance#4 规则5 用契约、#3 用 LLM。

**治本四刀+双复核整改（全部测试锁定，test_r65d_t2_plan_exam_coherence.py 19 条 +
test_r65d_t2_h1_exam_source.py 8 条 + fixture 重放 test/fixtures/plan_b583.json）**：
1. 模板 upsert（_strip_machine_pom_blocks/_upsert_owner_pom_block）：CREATE 权威模板/
   MODIFY 铁律+片段两形态幂等替换，陈旧刷新 WARNING fail-loud；多 pom owner 位置无关幂等
   （猎手 MED：顺序抖动 WARNING 刷屏）。
2. T5 反向边剪除（_reverse_internal_edge_producer）：目标模块 pom 生产者在消费方传递下游
   → 剪+WARNING；契约通道同判据（_prune_reverse_contract_internal_deps，猎手 HIGH：
   带 ${project.version} 的契约坐标能过 R53-1 可解析闸，剪推导不剪契约=换通道复活）；
   正向单向依赖对照锁不误杀。
3. 考卷同源 reconcile_template_exam：正断言剔除+模板依赖逐条重生成；负断言与模板矛盾→
   剔除+WARNING（st-26 死局规划期现形），不矛盾→保留（猎手 CRITICAL：禁入守卫是模板被
   后续机制改写时最后的牙齿）；规则5 验收行改写+「模板即真值」权威行；R58-1 名≠目录时
   唯一模板子任务兜底匹配（复核 LOW）；每子任务暂存区+独立 try/except 零半变异（猎手 MED）；
   接线唯一咽喉=inject_build_scaffold_subtasks 包装（两遍注入+外科重试全覆盖）。
4. worker H1 同源兜底：H1 登记 rel→模板映射，verify 时点【重比内容仍=模板】才跳过内容断言
   （猎手 CRITICAL：R56-5/version-repair 在 H1 后改写=温差窗口，内容偏离断言立即恢复牙齿）；
   verify_skipped_h1 机读留痕；全跳过打 needs_review=verify_all_skipped_h1（猎手 HIGH 盲区）；
   路径 token 边界匹配（复核 CONFIRMED：xmod-a/pom.xml.bak/嵌套叶名串扰）；无 H1 时旧考卷
   牙齿原样（对照锁）。
5. 附带：_extract_auth_templates 全角）路径污染（复核 CONFIRMED，聚合父/孤儿措辞）。

质量闸：RED 14→GREEN；双复核 reviewer(1H/2M/1L)+hunter(2C/2H/3M/1L) 全整改全锁；
revert-check 22 红/复原 24 绿（2 常绿=旧行为对照锁）；全量套件绿；lint 零新增。
st-26 若重跑本代码：模板第二遍刷新剔除反向依赖→考卷从模板重生成（okhttp 系断言）→
worker 抄模板必过闸；即便旧 plan 直入，H1 覆写后旧 grep jackson 被跳过+留痕不再冤杀。

## #60 R65D-T1 已治（2026-07-17，本地提交）

**病灶钉死（比登记更准的代码级真身）**：
- 掉账本体=round36 自愈分支 `_healed` 早退 return：只回队已愈项，同批其余失败（st-26
  verify-fail、仅补 C9 边的 st-30-1/31）failed_subtask_ids 清零+不回队+失败 result 滞留
  ——对照 _unrecoverable 分支有「其余失败放回重派」对称段，自愈分支缺失；
- "冻成完成态"=A2 定向恢复 `_kept`（保留 N 个完成态日志）把 subtask_results 里所有
  非本轮失败条目当完成态计数，滞留的 L1-fail 僵尸 result 被数进去；
- "宣称重派 4 实派 0"=重派确实进队，但依赖链穿过 st-26 僵尸节点永不可就绪，无人核销。

**治本三面**：
1. 根修：_healed return 补 `_sh_leftover` 对称回队+result 出账（与 _unrecoverable 同律；
   重试计数原样保留=同 _unrecoverable 惯例，猎手 F5 记 B 类观察不改）；
2. 铁律 `audit_failure_disposition`（唯一咽喉=handle_failure 包装，先于 plan 回传）：
   显式清空失败集时，入口 fid 必须∈ 失败∪重派∪放弃∪give_up（复核 CRITICAL：阶梯三
   settled-with-product 是终局，不认桩=毁桩+复活无界循环，双复核独立实锤）；replan/
   escalate 交棒整体豁免（复核 HIGH：误报会给健康 replan 永久打死因签名进 append-only
   degraded_reasons）；缺账→ERROR+强制回队+result 出账+failure_disposition_leak 机读；
3. 处方核销：回队者传递依赖命中 已放弃（非桩）∪【历史僵尸】（有失败 result 不在队不在
   失败集未终局，猎手 F3——st-26 形态本体跨轮残留时核销面也要看得见）→ ERROR +
   recovery_prescription_unsatisfiable 机读，只告警不改队列。
   审计自身异常=ERROR+failure_disposition_audit_error 机读（安全网下线绝不静默）。

质量闸：RED 3→GREEN 9；双复核 reviewer(1C/1H/1L·两条 live 复现)+hunter(1C/1M/1LM/2清白)
全整改全锁；give-up 阶梯/round36/round29 恢复面回归 50/50；revert-check 红；全量绿。

## #62 R65D-T3 已治（2026-07-17，本地提交）

MERGE 病灶：merge() 组装 subtask_diffs 无条件收 subtask_results 全部 diff，从不查
l1_passed——round65d 冻结的 L1-fail 三僵尸(4 diff/17801 chars)照单合入=毒树四株落盘。

治本：
1. 交付面闸：组装前剔除 l1_passed=False 输出（shared.l1_passed 单一事实源，give-up
   桩 l1_passed=True 不受影响）；被剔者 D7 孤儿同口径入账（并入 abandoned+pop 完成态+
   degraded_reasons merge_rejected_l1_fail:<sid>）+ ERROR 足迹日志（毒株文件落点可审计，
   round47 读树取证面）；足迹审计整块 best-effort（猎手 LOW-MED：import 收进 try）。
2. 剔除规模闸（猎手 HIGH CONFIRMED+复核 MED 合并）：全员被剔（空 diff 会被
   merge_diffs([]) 判 success→COMPLEX 确定性检查全跳→裸 LLM 可给空交付盖章自动放行）
   或超阈值 max(10,25%×计划) → escalate 人工 fail-closed（verification_failure=
   merge_l1_reject_mass），落点在 failure_escalated=False 粘滞清理之后（自查逮到的
   时序 bug：入账块先于清理行，escalate 会被清掉）。
3. 复核核实非死代码：#60 铁律 fail-open 轮/replan-escalate 豁免路径仍可漏僵尸到
   MERGE，本闸是必要第二层；rebase 重入/入账不被后续分支冲掉/L6 不学假成功全核实。

复核战果：reviewer APPROVE(1M/1L)；hunter 1 HIGH CONFIRMED(空交付盖章链)+1 LOW-MED+
1 LOW(dict 缺键无活体生产者，fail-closed 属仓规先例)。
质量闸：RED 1→GREEN 7；相邻 merge 面回归 49/49；revert-check 红；全量绿。

## #57 R65D-W2 已治（2026-07-17，本地提交）

治本两面：
1. 消费边下推：plan_finisher 新步 derive_consumer_depends_edges（算法收敛=G2
   wire_readable_provenance 单一事实源，复核 MED 去双实现漂移；收尾器末端再跑一遍
   接住 elaborate 期 G2 看不到的 readable 增量）。fixture 实测 +176 边/70 消费者，
   94 节点零环（"13 根"为 live 事发态，fixture 终版本就 2 根——docstring 已如实化，
   复核 LOW）。
2. 调度扇出优先：get_dispatch_batch._prio 升级 (tier, -fanout)——高位生产者同层先派
   （静态扇出≈动态扇出，复核核实无害）；B6 清单恒第一/Fix F retry 语义不变。
★猎手 CRITICAL（带复现）连带治本：消费边织密图激活休眠缺口——_transitive_abandon
四个调用点仅 R65C-T2 一处有规模闸，重试耗尽部分交付/T3 修复臂混批/自愈混批三条
未设防路径可让单个高扇出生产者一笔静默连坐全场（复现 31/31=100%）。治=
mass_abandon_cap 下沉 planning_core 单一事实源，四点全接（超阈值一律 escalate 人工，
mass_abandon_gate 机读）★。hunter MED（finisher 返回值被 PLAN 节点丢弃=机读账
aspirational）记 follow-up；reviewer MED（互指 readable 边方向依列表序，G2 继承
特性非新回归）记注释级。
质量闸：RED 8→GREEN 13（含规模闸联动 2 条）；调度面 405/405；恢复面 56/56。

## #63 R65D-T4 已治（2026-07-17，本地提交）

毒树第一株治本：_derive_missing_type_files 分支③把无 import 证据的 JDK 类型
（Map——缺的就是 import 所以才无证据！）误诊自造内部类型→下"新建 Map.java"指令→
worker 拒工书落盘。治=①_JDK_COMMON_TYPES 名单过滤邻近共现+无证据两路（显式
import 证据指向 blocked 包仍放行=真自定义同名类型不误伤）；②改道：缺失类全是
标准库→注入补 import 指导按自愈配额重派（治得了的病绝不连坐放弃）。
retry_guidance 通道分离面：毒源已断（误诊+create_files 毒文件声明均不再产生），
通道分离属结构卫生爆炸半径大→按"既有闸门能抓→诚实记 B 类"惯例登记 follow-up 不强做。

## #64 R65D-T6 已治（2026-07-17，本地提交）

四子项处置：
1. ★批门闩→滚动补位★（dispatch.py）：asyncio.wait 泵——批内任一完成即用
   get_dispatch_batch（滚动 completed 视图）选新就绪者补位（predecessor/upstream 注入
   同批前口径，_ua_changed 并入 plan 回写闸）。护栏：任一失败/异常立即停滚收批
   （HANDLE_FAILURE/R13-4 批间熔断节奏原样，BLOCKED 也停=保守正确，双复核核实）；
   补位总量封顶 max_concurrent×SWARM_DISPATCH_ROLL_FACTOR；_oversized_by_files 不滚。
   ★★应急回滚开关：SWARM_DISPATCH_ROLL_FACTOR=0（字面 0）恢复旧批门闩语义；非法值
   ERROR 留痕回默认 3★★。双复核双双逮到：滚动者绕过 subtask_dispatch_totals 终身账
   （A2 硬熔断失明，round48c 11 连派死型复活面，hunter CRITICAL 复现）+use_alternate
   不消费（粘滞劫持路由回归）→ 派发账改用全 spawn 集 _spawned_ids。
2. 规划侧 C7 在飞预留作用域（nodes/__init__.py _invoke_llm_abortable 唯一咽喉）：
   round65d 09:52/09:57 两条 TTL 泄漏实锤=作用域此前只接 worker 侧——正常返回
   settle_leaked=False，异常/取消立即按中止结算（零 30min 虚增窗口）。复核核实：
   两 return 全包、与 on_llm_error 正常结算互斥无双记、ContextVar 不还原无害。
3. 迭代预算自适应：记 B 类——executor 已有 nfiles 自适应+force_strong boost，round65d
   撞顶者止损链全对；盲目抬顶=每次尝试更贵，待下轮遥测（撞顶者 diff 质量 vs 步数曲线）
   定参再动。
4. 连坐重试先查可派发性：covered——#57 消费边（依赖闸前置扣住）+#60 处方核销
   （recovery_prescription_unsatisfiable 机读）已结构性化解，不另设闸。
质量闸：RED 2→GREEN 9（含双复核锁 3 条）；dispatch/phase3/FixF/D23 回归 44/44；
revert-check 红；全量绿。

## #58 R65D-W3 已治（2026-07-17，本地提交）

六项观测缺口+双复核整改：
① _det_fail_reason 判死机读 reason 单一提取点（verify_failed/reason+note/scope/
  pipeline_blocked/★compile_message——复核 HIGH：单文件编译闸真错误在此，build_output
  结构性缺席，漏读=最常见判死形态照旧空 reason★/build_output/test_output；畸形 details
  兜底自报 reason_extraction_failed，猎手 HIGH：观测代码绝不反噬 Phase-4）；
② 去"拦截幻觉 PASS"冤案措辞——_det_conflict_log_line 单一生成点（行为级可测，
  替代 getsource 守卫——猎手裁定原测试违反本仓"禁 getsource"仓规，已改写）；
③ HANDLE_FAILURE 恒 INFO 处置总账行（入口→重派/放弃/保留失败，全出口含 replan/
  escalate；审计自身异常轮补最小账——猎手 MED：审计崩的那轮恰最需要总账）；
④ clean_upload tracked 空集分级：greenfield probe（git ls-files，to_thread 卸载——
  猎手 CONFIRMED HIGH：直调 subprocess 阻塞事件循环违反本文件自述铁律）→INFO；
  仓库确有 tracked=git 故障→WARN；probe 失败按可疑 WARN（fail-safe 方向复核核实）；
⑤ P1 外科主备双失败 auth 类（401/403/坏 key）升 ERROR（配置错需 ops；round65d 复盘
  401×5 淹没在 WARNING 噪声）——仅日志级别，全仓无 level 消费者（猎手核实无误 page 面）；
⑥ MANIFEST-SYNTH 无尾换行跳过时点名受损模块（口径与主体合成判据同源
  _NEW_MODULE_POM_RE+<parent>，复核 MED：宽松口径=假警报；检测器自身失败自报
  "损失面未知≠零损失"，猎手 MED：错误变空列表死型）。
质量闸：RED 5→GREEN 7；manifest/处置/恢复面回归 26/26；revert-check 红；全量绿。

## #59 R65D-T5 已治（2026-07-17，本地提交）

**plan 注入端**：录制 cassette（cassette_extract 产物）直入 DISPATCH 的 worker 阶段离线
调试通道——执行期编排 bug 复现不再重烧云端规划期。

**通道形态**（test_r65d_t5_plan_inject.py 22 条锁定）：
1. 入口：POST create_task 带 `injected_plan`（TaskCreateRequest 新字段），总闸
   SWARM_PLAN_INJECT_ENABLE 默认关死（403）；轻校验 schema/非空。落库 task_records.
   injected_plan JSONB（_TASK_SELECT 第 29 列，light 行刻意排除并注释招牌）。
2. runner 深校验 prepare_injected_state 五道 fail-closed 闸：schema / 空 plan /
   base_commit 一致性（单侧缺失同罪，报错带重置指引）/ 治疗 pass fail-open 吞异常
   升闸（logger exc_info 捕获，猎手 HIGH）/ 重推导后 validate_plan_structure。
3. 治后形态重推导（绝不原样回放）：strip 旧脚手架（单一事实源收编 cassette_replay）
   → finish_plan_deterministic（#61 考卷同源+#57 消费边）→ resolve_plan_conflicts
   （异常归一 plan_inject_rederive_failed，猎手 MEDIUM）。fixture 实测 st-26 毒考卷
   （jackson grep）被同源重写；边 420→407（#61 反向剪除合法减边——边总数非治后判据）。
4. 图入口：aupdate_state(as_node="confirm", human_decision=ACCEPT) → 校验
   next==('dispatch',) 不符 fail-loud → astream(None) 续跑。实测 MemorySaver 通过；
   复核确认 LangGraph abulk_update_state 对 PG/Memory checkpointer 同构。
5. SWARM_BRAIN_OFFLINE=1：闸在 ModelRouter.get_brain_llm/get_brain_fallback_llm
   【构造点】raise BrainOfflineError（取证：brain 有 20+ 直接 ainvoke 调用点，
   _invoke_llm_abortable 非唯一咽喉；构造点才是单点）——调用方走既有降级
   （HANDLE_FAILURE strategy=retry 默认/fallback getter 返 None/外科 topup 让路）。
6. 工具：scripts/plan_inject_submit.py（e2e token，✗ 约定覆盖 HTTP/连接/超时/非 JSON）。

**双复核处置**（reviewer=正确性/时序/状态机 + hunter=静默失败，全 CONFIRMED 已治）：
- reviewer HIGH：注入拒绝路径缺 FAILED 终态三件套（R38-E 机读账/audit/站内通知）→
  抽 _reject_plan_inject helper 同口径补齐+锁。
- hunter HIGH×2：①治疗 pass fail-open 吞异常→毒 plan 带成功账开跑（round65d 冻结
  陈旧模板死型）→ exc_info 警报升闸 plan_inject_rederive_degraded+锁；②闸3 在
  _stream_brain_events 深处触发走 generic FAILED，error 无机读码 → PlanInjectError
  message 自带 code 前缀+锁。
- hunter MEDIUM×2：resolve 无包裹裸冒泡→归一 rederive_failed+锁；strip 后零重注入
  →不武断拒绝（无异常≠静默失败）但 WARNING 点名。
- hunter INFO：外科 topup 裸 _get_brain_llm 与兄弟护栏不对称 → 让路全量重拆+锁。
- 权限口径（reviewer MEDIUM，拍板记录）：开闸后 task:create 即可注入=环境级安全模型
  （单运维调试/E2E 开启、用完即关），docstring 已改为如实口径，不做用户级权限。
**B类遗留**：①Web UI renderTaskDetail 不渲染 task.error（先在，注入排障靠 API/日志/
DB——前端补渲染另立小项）；②SWARM_BRAIN_OFFLINE 是全局开关，调试后遗留 .env 会让
普通任务 brain 全降级（构造点每次 ERROR [BRAIN-OFFLINE] 可见，e2e 起跑自检建议加
grep 项）。
**质量闸**：22 测试 GREEN；定向回归 68 绿；revert-check 红 8/复原 22 绿；全量见提交。
**用法**（round65d 回放）：重置基线到 0d42679 → SWARM_PLAN_INJECT_ENABLE=1 [+
SWARM_BRAIN_OFFLINE=1] restart-api → plan_inject_submit.py test/fixtures/plan_b583.json
--project <pid>。绝不 retry 旧任务 b583df8f 本体。

## #69 R65REPLAY-T4：上游账↔依赖序对账——幽灵死等账死锁治本（回放轮主死因）

**定案因果链**（三路交叉印证+代码取证，全文 logs_archive/process/round65d_replay_mirrors/journal.md）：
R63-T4 符号布线按语料文本引用布 readable+upstream_artifacts 不查方向
（symbol_provenance.py:263-266，fixture 固有非拆分引入）→ st-11-1(XML) 账含下游
st-11-2..5 才创建的 4 个 Mapper 接口 → W2/T5 规划期见环只跳边不清账
（plan_finisher.py 旧 482-486）→ seed 闸 fail-closed 只看账不知生产者∈自己传递下游
（executor.py:667）→ 父任务 18:05 首攻即 BLOCKED；预算闸拆分 scope deep-copy 继承
（planning_nodes.py:3281）1 死等复制成 4 → 18 派发 14 同签名 → A2/B2/规模闸 64min
三闸会师 → 连坐 72 → PARTIAL 24/97。st-17-1 同构第二发。

**治本**（栈中立，纯 DAG/路径逻辑）：
1. plan_finisher.reconcile_upstream_account：ua 条目的计划内生产者【全部】为自身或
   （传递）依赖本任务 → 从 ua+readable 剔除 + WARNING + 机读账 + 权威路径文本提示
   注入 context_snippets（信息通道保留，只掐死 seed 死等语义）。生产者口径
   create∪writable；路径 _norm_scope_path 归一；歧义/基线不动；写者无关全量重算；幂等。
2. 接线①收尾器末端（W2 消费边之后——能成的边先成，生产者转正则账合法保留；live 规划
   与注入回放共用）；②dispatch 派发前兜底（B1 之前，兜执行期账写者），plan 显式 emit
   两处（空批早退+末端）。

**双复核处置**（reviewer=APPROVE 零 finding；hunter 3H/3M/1L 逐条裁决）：
- F1 HIGH CONFIRMED：owner 口径 create-only 看不见上游 writable 修改者→冤剔真上游
  → 口径扩 create∪writable + 锁（test_writable_upstream_producer_kept）。
- F2 HIGH CONFIRMED：无路径归一，'./'/反斜杠漂移让幽灵漏网（R41 先例）→
  _norm_scope_path 单一口径源 + 锁（test_path_drift_normalized_still_removed）。
- F3 HIGH 部分成立：剔 readable 后引用型产出（如 XML 引接口 FQN）失去权威路径=从死等
  退化成盲猜 → 采纳方案 b：剔除路径以文本提示注入 context_snippets + 锁。
- F4 MED 裁 no-change：reconcile 必须在 B1 前且不得 B1 后重跑（B1 只注入完成态+存在
  文件零死等风险；事后重跑可能剔掉合法注入让 bootstrap 漏传）——定序裁决落注释。
- F5 MED CONFIRMED：docstring 按写者点名不实（梯三桩实无 ua 直写；failure.py
  _amend_scope_with_missing_files 才是真第三写者）→ 改为"写者无关全量结构重算"不变量表述。
- F6 MED CONFIRMED：机读账零消费者+fail-open 静默 → 失败落 out 标记 → live 调用点
  （nodes/__init__.py）进 degraded_reasons"upstream_account_reconcile_failed"；注入路径
  _FailOpenAlarm 对 exc_info WARNING 自动升闸；plan_inject 消费账落 INFO + 锁。
- F7：测试盲区按 F1/F2/F3/F6 补 4 锁；dispatch 全节点集成/旧 checkpoint 恢复两项接受
  （helper 全 plan 重扫+调用点无条件=reviewer 已验结构性覆盖）。

**质量闸**：15 测试 GREEN（含 fixture 级：cassette 重推导后 st-11-1 4 Mapper 幽灵账+
st-17-1 Impl 账全清）；revert-check 红；定向 155+ 绿；全量 4786/0/0（整改前基线）+
整改后全量见提交。注：首轮全量 4 失败=当时 .env 回放双闸污染（BRAIN_OFFLINE/
PLAN_INJECT_ENABLE），非本案回归——双闸已删+restart-api（round65e 前置检查项已了）。

## #66 R65REPLAY-T1：消费边=软序边——连坐不穿透/死产者可越过（回放反噬面治本）+ #58 reason 修正

**定性修订（实证驱动）**：fixture 取证 st-23-1 的 plan 语料完全不含 DutySnapshot——引用是
worker 执行期自己写的，规划期无证据可布边 →"证据面扩展"处方作废，该场景归 C9 动态边+
重派兑现（#70/#71）。本案主体=反噬面：C 路反事实实测 #57 消费边把 st-11 死点连坐闭包
15→72（仅去 st-2 上 4 条边闭包 68→11）。

**治本（栈中立，零簿记）**：types.edge_is_soft 结构性判软——
软 ⇔ producer 产出∩consumer.ua=∅ 且 producer.create∩readable≠∅ 且 producer.writable∩readable=∅。
存储标记方案被否（重拆 remap/rebuild/注入重推导必漏，st-2 软边被 remap 成→分片即实锤）。
三处语义：连坐闭包不穿透（_transitive_abandon）/死产者软边就绪可越过（get_ready_tasks+
get_dispatch_batch._is_ready）/活产者照常排序。R51-1/T5 先例（completed 优先）原样保留。
#58 附案：_det_fail_reason build 失败优先引 build_output 首错误行（"compile_fail: compile ok"
自相矛盾绝迹）。

**双复核处置**（reviewer=APPROVE 1M/1L；hunter 2H/2M/2L 全裁决）：
- hunter F1 HIGH CONFIRMED：writable 生产者死亡→盘上留旧版，R49-2 只查存在性兜不住，
  越过=静默陈旧读 → 软判收紧 readable∩producer.writable 有交=硬 + 锁。
- hunter F2 HIGH CONFIRMED：failure.py _dep_hit 第 4 消费点未接软边——无关死软边把
  "等活生产者"误判永久放弃 → 只算硬依赖 + 锁。
- hunter F3 MED CONFIRMED：软边豁免零留痕（规模闸 escalate 率下降不可审计）→
  _transitive_abandon WARNING 点名豁免者。
- hunter F4 MED CONFIRMED：build_output 空时 build_failed（=构建命令）冒充诊断 →
  如实报"(构建无输出捕获) cmd=…" + 锁。
- hunter F5 LOW：错误行识别 ERROR/error: 双字面量漏多栈口径 → 复用 output_compress
  _SIGNAL_RE（含退回）。hunter F6 LOW：归一双实现平价锁 test_norm_path_parity。
- reviewer MED（接受为有界权衡，观察项）：LLM authored 真依赖若只有 readable 交集（规则2
  回填 readable 不写 ua）会判软→死产者时多烧一次有界尝试（B2 同签名短路+耗尽路径兜底，
  无无界路径）——round65e 观察面：留意"软边越过后 L1 失败"的比率。
- W2 两个规模闸测试按语义演进改硬消费（ua）形态——"大额连坐必须 escalate"不变量保留
  （reviewer INFO 独立确认：W2/G2 边源在生产中本就不写 ua，级联不再成形是意图效果）。

**质量闸**：19 测试（软边 15+W3 reason 2+平价 1+回放缩微 1）；revert-check 5 红；定向
45+99 绿；全量见提交。

## #71 R65REPLAY-T6 + #72 R65REPLAY-T7：终态账务守恒 + 终态诚实清扫（回放轮软掉账/幽灵件双治）

**#71 软掉账**（C 路独家：audit 33 派发 vs results 28，st-2/st-23-1/st-17-1/st-27/st-11-1
旧 id 五例）：HANDLE_FAILURE 重派 pop subtask_results 后调度未兑现（#70）→ 终态无失败无
完成的"第五态"。裁决：不动 pop 语义（presence 被广泛消费，重派意图合法；兑现本体归 #70）
——治=_failed_machine_account 终态对账：subtask_dispatch_totals（单调终身账）有记录/
results 无/非放弃 → tu["dispatched_unaccounted"]+WARNING（plan 外旧 id 同列）。

**#72 幽灵件**（C 路 mvn 实证：有账 24 产物 100% 编译过、账外 15 件=盘上全部编译破坏源）：
治=_sweep_unverified_footprints 终态诚实清扫——PARTIAL/FAILED 结算前对"派发过且非
l1_passed"的计划内子任务跑 _local_tree_revert_subtask（H2 同源：tracked→checkout 钉扎
base/untracked→删除）。protected=完成者 diff 真账∪scope 声明（_files_owned_by_completed
同源守卫）。边界：pull-back 不动（B2 续作/取证）；DONE/CANCELLED 不清扫（跟进批 #73）。

**双复核处置**（reviewer WARNING 2H/2M/1L + hunter 1C/3H/3M/1LM，合流裁决）：
- ★hunter CRITICAL：重启孤儿对账裸 FAILED 零账零扫 → orphaned_on_restart 改走
  _salvage_partial_from_checkpoint（checkpoint state→governor 终态：有产物诚实 PARTIAL+
  对账+清扫），salvage 异常回退旧裸 FAILED——重启语义从"一律 FAILED"升级为"诚实收编"★。
- 双 HIGH（两家同报）：run_task 泛 except state=None 双闸失效 → best-effort 取
  _accumulated_state（NameError 回退）+ 清扫接线。
- reviewer HIGH：同步 git 子进程卡单进程事件环 → 三调用点+泛 except 全部
  asyncio.to_thread 卸载。
- hunter F1/reviewer LOW：protected 纯 diff 解析漏保（同内容 rename 无 hunk/多写者 pom）
  → ∪ _files_owned_by_completed（scope 声明）双保险+锁。
- hunter F5：plan 外旧 id 无 scope 可清 → out["unsweepable"] 留痕入账+锁（账与扫帚的
  分歧必可见）。
- hunter F6+reviewer MED：单子任务失败不丢已扫账（per-sid try/except+sweep_errors）+
  revert_failed 非零入 sweep_errors（"清了无人知道"绝迹）+锁。
- hunter F8：base_commit 缺失 → WARNING（HEAD 回退半径大，留痕供审计）。
- 刻意不并入（登记 #73 跟进批）：DONE 工作树残留/CANCELLED 不清扫/resume 两处泛
  except/run_task 泛 except 行为级测试。

**质量闸**：10 测试（#71 3+#72 7，含临时 git repo 实测 checkout/删除/protected/
unsweepable/mid-loop 隔离）；revert-check 5 红；终态家族定向 35+37 绿；ruff 阻断级 0；
全量见提交。

## #70 R65REPLAY-T5：滚动补位 BLOCKED 预检秒退解冻（调度饿死案·定性修正后收缩治疗）

**定性修正（治疗中取证推翻原处方一半）**：回放三波批被 [st-27+分片族] 垄断的直接原因
不是 retry 池排序——C 路实证 st-2 被 #57 消费边挂上对必死分片族的依赖，被依赖闸正当
扣住，就绪集本就只剩 retry 家族。原处方 a)（同指纹降调度序）b)（retry 池内排序）与
病因不符，如实作废（修根因非打地鼠）；根源已由 #69+#66 治除。

**实治=原处方 c)**（独立实锤：18:42 后补位零触发、末段 26min 并发≈1）：滚动补位
_any_bad 一票冻结细化——seed 闸【预检】秒退不冻结补位，判据=blocked_stage=="preflight"
专属标记（seed 闸 l1_details 新增；worker/executor.py）。BLOCKED 者照常收批交
HANDLE_FAILURE（C9/重派/R13-4 节奏全不变）；真失败/异常仍立即冻结。

**双向复核处置**（hunter 单 agent 双向，1H/1M/2L）：
- F1 HIGH CONFIRMED：not_run_kind=blocked+failure_class=transient 对被昂贵路径共用
  （烧满预算超时 BLOCKED/真 build ≥5min 后 internal_pkg_not_built/R50-2 跳回滚的树
  改动路径）→ 豁免收窄为 preflight 专属标记（唯一零成本产出点）+ 昂贵 BLOCKED 冻结锁。
- F2 MED：随 F1 收窄消解（补位风暴上限=count 非 cost 的暴露面只剩零成本预检）。
- F4 LOW-MED 接受为架构固有（早到好消息触发的补位不被晚到坏消息追溯取消——成功完成
  同性质，_roll_budget 封顶非 runaway；timing 竞态测试易脆不写，此处文档留痕）。
- F3 LOW 既存面（transient 批混龄走能力阶梯）记录不动。
- 异常路径核验（_oc 为 Exception → _det_b={} → 恒冻结）✓；HANDLE_FAILURE/R13-4 节奏
  核验零绕过 ✓。

**质量闸**：3 新测试（豁免/真失败对照/昂贵 BLOCKED 冻结）共 11 项滚动族全绿；
revert-check 红；dispatch 家族定向 42 绿；全量见提交。

## #68 R65REPLAY-T3：depMgmt 只登被依赖者——应用壳不再被声明成可依赖件

**定性双修正**：①C 路"st-1-1 越权改根 pom"→ 代码取证纠正：根 pom 改动来自合法系统
机制（L1.2.1 module-reg reconcile_workspace_manifests → repaired_file_paths 通道，
round8 治本），scope 闸没失守，"越权闸"处方作废。②毒株作者=D2
_reconcile_maven_dep_versions（round18 治本）无差别把全部带 <parent> 子模块登进根
depMgmt——无人依赖的应用壳 ruoyi-admin 被声明成可依赖件（错误对外契约，存活至终态树）。

**治本**：收窄为只登【被本聚合树内其它模块运行时依赖（_maven_direct_deps）引用】的
(g,a)——与 round18 初衷（内部依赖版本可解析）精确对齐，过宽即病。Maven 通道内的
Maven 语义判断（机制分栈实现、判据结构中立，不违多栈铁律）。

**双向复核处置**（hunter 单 agent，1M/1L/4 non-finding 全实证）：
- MED（伴生债入 #73⑤）：depMgmt 无 prune 对偶——引用者被 H2 回滚/replan 剥离后已登记
  条目无人剔（<module> 成员有 R46-2 prune，depMgmt 没有）。
- LOW 记录：收窄过滤与 L2 版本闸共享 _maven_direct_deps primitive（改它双侧同降级）。
- 实证确认：增量语义健在（引用者 pom 后建，L1/L2/交付三卡点重扫自然补登）；漏登被
  missing_intra_project_module_versions L2 闸响亮 fail-closed（非静默）；test-scope/
  profiles 引用形态被正确捕获；Gradle/Cargo/.NET/Go 通道无同病（它们只做构建成员
  注册，应用壳本就该登）；互依应用壳互登=结构正确且被 Maven 环检测自限。

**质量闸**：2 新测试（应用壳不入账/幂等）；revert-check 红；manifest 家族定向 52 绿；
全量见提交。
