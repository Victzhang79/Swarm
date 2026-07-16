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
