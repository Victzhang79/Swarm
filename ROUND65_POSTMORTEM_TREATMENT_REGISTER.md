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
