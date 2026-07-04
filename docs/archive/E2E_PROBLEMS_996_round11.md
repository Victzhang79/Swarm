# E2E 第十一轮 — 996db614 预警平台 · 问题记录

> 任务: `996db614-e01f-4053-98a7-d0d55897e2f7` (RuoYi-E2E / 预警编排平台, ultra, auto_accept)
> 项目: `/Users/zhangyanrui/LLM/swarm/e2e-projects/RuoYi` (proj `5d0e9db8`)
> 启动: 2026-06-30 13:01 · main @ `5498ca6`
> 上轮: round10 CANCELLED 18/26（取消未到 MERGE，B+A 仅单测+逻辑验证）

## 开跑前置 (三清 → 重启 → 端点核验)
- ✅ **三清**: 基线 reset(e2e_reset → 0d42679, 丢 0 swarm 交付 commit, git status 干净) + swarm.log 归档(round10 2.9M → logs/archive_round10/swarm_round10.log) + sandbox_logs 61 jsonl 归档 → logs/archive_round10/ + /tmp e2e 残留清理
- ✅ **重启**: stop 旧 pid 16877(06:46 起, 仅含 6b39449)→ 新 pid 71023(13:00:57, 加载 HEAD 5498ca6 含 9dd817b+5498ca6)。⚠️旧进程缺两条治本，必须重启才生效。
- ✅ **端点核验**(round7 硬前置, 走 swarm 解析的 secret_store key 真实补全):
  - worker: MiniMax-M2.7-Pro ✓ / Qwopus3.6-27B-v2-NVFP4 ✓(0.1s) / Qwen3.6-27B-Saka-NVFP4 ✓(reasoning 模型, 小 token 预算只出 reasoning 是测试假象非故障)
  - brain: zai-org/GLM-5.2 ✓(4.3s) / moonshotai/Kimi-K2.7-Code ✓(1.2s)
- ✅ plan-quality 离线基准 2/2(RUN18/RUN19)

## 三盯 (本轮监控三只眼)
1. **eye1 任务日志(编排)**: swarm.log(fresh) + /tmp/e2e_run_996db614/{full,events,analysis}.log + **富脑日志 `logs/996db614-….log`**
2. **eye2 沙箱日志(执行)**: ~/.swarm/sandbox_logs/<sid>.jsonl(fresh)
3. **eye3 端点/路由(基建)**: `[ROUTER] 整条链均不可达` / FALLBACK 降级 / stream stall / diff=0 空产出

### ⚠️ 读日志纪律 (task id `996db614` 跨轮复用 → 同名日志会混轮)
- **per-task 富脑日志 `logs/996db614-….log` 是 append, 含 round8/9/10**：round11 起于 **line 27163**(`13:01:23 [RUNNER] workspace`)。**只读 `tail -n +27163 logs/996db614-….log`**(进程持 append fd, 不能 rotate, 故 anchor)。
- swarm.log / sandbox jsonl / `.sandbox.log` 已归档 round10 → 均 fresh, 直接读。
- 任何 grep/读取先确认时间戳 ≥ 13:01, 勿把 round10 行当本轮证据(RUN6→7 脏环境误诊教训)。

## 本轮验证的是【流程/机制 + 错误类别收敛】, 不是固定子任务
> ⚠️ 用户 2026-06-30 强调: **LLM 每轮拆分随机**, 子任务编号(st-8-1 等)跨轮不复现。
> 故**不盯"某个固定子任务是否报错/超时"**, 而盯【机制是否生效】+【错误类别是否复现】, 据流程与错误样本判治本成败。

- [ ] **#3 单实体拆分上界机制 (9dd817b)**: 观察是否还出现【任意单实体全栈切片 >6 文件 → 900s 超时空转, 且 LLM 建议拆但系统只换模型不真拆】这一**类别**。机制生效=该类超时空转不再出现(无论具体是哪个实体)。
- [ ] **两条结构主干机制 (5498ca6)**: 并行聚合态 diff 隔离(多写者不互覆盖) + 工作单元预算契合(不再派超预算单元致必超时)。看是否还有【并行 clobber】【超预算单元】这两类现象。
- [ ] **pom MERGE 并集 B+A 机制 (6b39449)**: 近完整产物(干净合并、失败=0)应交付 PARTIAL/full 而非误判 FAILED。**本轮关键 = 流程必须跑到 MERGE 才能线上验证**(round6/9/10 均未到)。
- [ ] **错误类别收敛对账** (跨轮稳定主题, 看本轮各类是否复现/减少):
  - 符号臆造类(cannot find symbol: 模型造方法/类, BaseController API 误用, 漏 getter)
  - 跨模块文件同步类(读未产出上游 404 是否级联成编译失败)
  - 端点不稳类(eye3 router 整条链不可达 outage 簇)
  - import 漏补类(Spring 注解 FQN 如 @Configuration)
  - 超时/截断类(迭代上限、900s、流式 stall)

## 残留 follow-up (本轮观察是否显形)
- O4 import-repair 缺 Spring 注解 FQN(如 @Configuration) — 宜项目派生非硬编码
- O5 孤儿沙箱死实例 liveness 回收
- Shiro `@RequiresPermissions` / RuoYi BaseController API 白名单 hint(模型臆造 getTableDataInfo/toLongArray 等)

## 进度快照 (随跑随记, 每 10min 一盯)
- **13:01** 触发 retry, ANALYZING; watcher pid 72232 fresh 起(/tmp/e2e_run_996db614/)
- **13:16** 盯#1: ANALYZE→TECH_DESIGN→REVIEW→PLAN 方案通过 → CONTRACT 阶段(SKELETON 89.6s: conventions9/constants8/consumer_map10; CONTRACT_MODULE 6/10 完, 单模块 65–312s=本地大模型固有慢, 对标 round8 O1)。eye3 router trouble=0; 沙箱未起(契约期); 无问题。
- **13:22** 盯#2: 仍 CONTRACT(~5-6/10 完, 模块 7-10 并行流式生成中, 单流 420–571s/23k–28k chunk 未 stall); 末行 13:22:41 距今 3s=live。eye3=0; 沙箱未起; 无问题。CONTRACT 偏慢(O1)是唯一现象, 非 bug。
- **13:32** 盯#3: CONTRACT 收尾 8/10 完(长尾模块 8/9/10=1042/1059/1215s, 比 round8 867–1135s 更慢); **模块5 'alarm-schedule' & 模块7 'alarm-engine' 第1次 timeout→退避重试**(韧性自愈, round7式, O2 同类)。仍 ANALYZING, live(13:31:44), eye3=0, 无沙箱。现象=CONTRACT 慢+2 重试, 自愈中非 bug; 待 5/7 完→plan/merge→dispatch。
- **13:43** 盯#4: 9/10 契约齐; module5 第3次成功(13:39:47); **只剩 module7 'alarm-engine' 在第3次(末次)尝试**(2次 timeout 后), GLM 流式 live(13:43:18)。仍 ANALYZING 未 dispatch, eye3=0, 无沙箱。详见 O1b——CONTRACT 末尾被单个最大模块卡住, 有界重试中。
- **13:52** 盯#5: **流程推进到 PLANNING**(13:45:19)。module7 第3次成功→`CONTRACT_MERGE 合并完成: 接口17 DTO58 常量8 API73 约定9 模块依赖10(10/10 模块成功)`。✅**CONTRACT union-merge 治本线上确证**: NotifyUserQueryService/TaskConfigService/TotpService 等同名多版→`并集合并(不丢方法/字段)`(正是 round7 a4f94bd 治的 keep-first 丢方法隐患, 本轮真并集)。现 `[PLAN] 拆解任务(ultra)` 分解中。eye3=0, 无沙箱, live(13:51:41)。逼近 DISPATCH。

## 问题清单 (随跑随记)
<!-- 编号 | 子任务 | 现象 | 根因(待查) | 是否新发现 -->
- **O1b [perf/resilience✓·非bug] CONTRACT 大模块生成超时→有界退避重试自愈**：最大两模块 alarm-schedule / alarm-engine(各 22 文件, TECH_DESIGN-STAGE2 标注)契约 prompt 最重→本地大模型单次生成撞超时。module5 第1(13:22)/2(13:37)次 timeout→**第3次(13:39:47)成功**(接口2/DTO9/API22);module7 第1(13:24)/2(13:39)次 timeout→末次尝试中。源码 `range(1,_STAGE2_MAX_ATTEMPTS+1)` 有界(~3 次), **非死循环、非整任务中断**, 任务全程 live。代价=CONTRACT 阶段被这 2 大模块拖慢 ~30min。**治本候选(低优)**: 大模块(>N 文件)契约生成可按子簇分批或加大单次超时预算, 减少重试空转。与 #3(coding 阶段单实体超时, 9dd817b 已治) 是不同阶段的同主题(大单元超时), CONTRACT 设计阶段尚无对应拆分。

- **14:02** 盯#6: **进 DISPATCHING 0/23**(本轮 23 子任务, round8=27)。VALIDATING_PLAN(13:55)→`T3 scope 归一: 消除同文件并发写冲突(写权唯一化+依赖产物入域)`=5498ca6 两主干 plan 层前置→DISPATCHING(13:57)。✅**st-1 脚手架 L1 通过**(LOCATING/CODING 撞迭代上限交确定性闸门; `pull-back downloaded=2 errors=0`=round7 跨模块同步生效; deterministic 通过)。eye2=1 沙箱 live, eye3=0, live(14:02:10)。watcher 一条 STATUS=ERR=看守解析瞬抖非任务错。验证窗口(#3拆分/两主干隔离)开启。

- **14:12** 盯#7: DISPATCHING **1/23**(st-1 完); st-2-2/st-4-2 L1 通过✅(deterministic); st-2-1/4-1 执行中。eye2=**5 沙箱并行** live, eye3=0, live(14:12:05)。**错误类别目前零**(无符号错/无超时/无 replan/无 HANDLE_FAILURE; 撞迭代上限交确定性闸门=正常韧性)。#3 按层拆暂未见触发(本读无 `[ELABORATE] 单实体超6文件→按层拆`), 继续观察。

- **14:22** 盯#8: DISPATCHING **5/23**; **L1 通过累计 7 / 未通过 0**(全 deterministic pass); eye2=**9 沙箱并行**, eye3=0, live(14:22:16)。**错误类别仍全零**(无符号错/超时/replan/HANDLE_FAILURE)。本轮 dispatch 迄今最干净。

- **14:32** 盯#9: DISPATCHING **8/23**, L1 通过8/未通过2。**首个 HANDLE_FAILURE(st-7-2)处理链路全治本生效**: ①确定性补依赖 A2(据项目 pom 自证坐标→st-7-2 缺 ruoyi-quartz→直接补不耗配额) ②定向恢复1/2 补模块 pom 写权 ③round6 仅重派失败子任务(8 完成态保留, 不回 PLAN)。eye2=13 沙箱, eye3=0, live。

- **14:42** 盯#10: DISPATCHING **8/23**(持平#9, 但 L1 通过 8→9, 13 沙箱 live=有真进度非停); st-7-2 重试在飞, **无新错误类别**, eye3=0, live(14:42:03)。中段大子任务+重试慢致 completed 暂滞, 下读若仍 8/23 再查。

- **14:52** 盯#11: DISPATCHING 10/23(MONITOR 完成12/失败2/剩余26), L1 通过累计23(含层拆children), 17 沙箱。eye3=2 但**均 FALLBACK 降级(Qwopus→Qwen3.6, 14:44 同一事件)非 outage=自愈benign**, 非 round8 P-2。HANDLE_FAILURE retry_alternate 重派 st-7-2+st-9(根因都指模型能力瓶颈)。live(14:52:05), 未到 MERGE。

- **15:02** 盯#12: HANDLING_FAILURE 12/23(MONITOR 完成14/失败2/剩余24, 12→14 有真进度)。新失败 st-14-2 **超时类**(batch2 撞900s→verification_not_run)。eye3=2 无增长(仍 14:44 fallback, 无新 outage)。17 沙箱 live(15:01:47)。**本轮错误类别=timeout 主导**(st-7-2/9/14-2)+符号臆造1(st-9)。未到 MERGE, 失败处理循环慢磨(dispatch 已~1h05m)。

- **15:12** 盯#13: **进度停滞 10min**(12/23, MONITOR 完成14/失败2/剩余24 同 #12)。**st-14-2 timeout 重试空转**(又 verification_not_run, 本次436s)。21 沙箱转不完成。eye3=2 稳, live(15:11:59)。⚠️**关注**: st-14-2 是层拆 child 却仍撞900s(天然大逻辑层 or 层拆粒度仍偏大)→ #3(9dd817b)对【单实体>6文件】拆, 但多实体/大逻辑层 child 仍可超时。有界重试应 escalate→PARTIAL(测 B+A); 否则看守 30min 无变化自动取消。下读重点看是否突破。

- **15:22** 盯#14: DISPATCHING 13/23(12→13 creeping)。eye3=4 **全 FALLBACK 降级(Qwopus→Qwen3.6)非 outage**(25 沙箱重压→主力忙→负载卸载, benign)。**st-14=最重模块层拆**: st-14-3 通过✅; st-14-2/4 未通过=**verification_not_run(产出大 diff 15k-17k 真写了码, 但 verify/build 闸门超时没跑成)**, 纯 timeout 非码烂; 系统**继续定点拆小 st-14-5**试更小粒度(#3 拆小阶梯在engage)。错误类别仍 timeout 主导且收敛于 st-14 重模块。

- **15:32** 盯#15: HANDLING_FAILURE 14/23, **进度解冻**(MONITOR 已完成 12→14→16→17, 剩余→21)。`定向恢复达上限(2次)仍缺依赖→落常规 retry 兜底`×2=有界升级阶梯在走(不死循环)。eye3 真 outage 0/fallback 6 全 benign。25 沙箱 live(15:31:30)。失败稳定 3, 未到 MERGE, 健康有界慢磨。

## 🏁 终态复盘 (2026-06-30 ~15:48, 人工 cancel)

**终态**: CANCELLED 14/23 (MONITOR 末: 已完成18/失败4/剩余20 工作单元)。**人工主动取消**(非 watcher 取消、非 FAILED)——判定继续=本地模型能力天花板慢磨, 边际信息低, 沙箱累积(33)+配额空转不划算。取消后沙箱全回收(孤儿清理 killed=0 无泄漏), watcher 自退。

**为何没到 DONE/MERGE — ⚠️ 原判「能力天花板」已被三只眼取证推翻（2026-06-30 复审）**:
> 当时只 grep+skim 任务日志，误判成「本地模型能力天花板」。补读**所有沙箱 jsonl + 工作树产物**后真因翻盘——主因是**系统 wiring bug**，非能力。
- **根因 A1（run-killer，系统 bug）**: st-7-2/14-2/14-4/15-1 **不是 900s 超时**——300–650s 跑完、worker 自报 pass、`errors=0`，只因引用的**兄弟域实体包不存在**而编译失败(`package …schedule.domain does not exist`/`cannot find symbol: AlarmScheduleGroup`)。域 owner 兄弟(st-14-1)早已 DONE，但 ELABORATE「剥离假依赖」(零文件重叠判据)把真实跨层 type 依赖剥了→owner 产物从未注入依赖子沙箱。**=层拆 wiring bug**。
- **根因②（quality-killer）**: 产物里子任务对 RuoYi 变体假设分裂(Shiro vs Spring-Security `@ss`)、`util/utils` 漂移、service 拆分冲突→**框架事实/契约未硬性强制**。这才是 st-9 `SecurityUtils` 臆造的诱因。
- **放大器 A2/A3/A4**: `internal_pkg_not_built` 被误当缺依赖(走 A2 补无关 maven 坐标)+当 transient 重试 + brain 已算出正解(用 ShiroUtils)却不传给重试 worker → 把上面两个根因放大成多轮空转(~16/33 沙箱耗在注定失败的重试)，是主动取消的直接诱因。
- **纯能力(B，仅少数)**: st-9 `SecurityUtils`/`CipherUtils.encrypt`/`GoogleAuthenticatorConfig.Builder` 臆造、st-7-2 `List.isNot()` babble。这些**叠在 A1 之上**——即便符号全对，没兄弟域同步仍编不过。
→ **治本批见 `/Users/zhangyanrui/.claude/plans/transient-painting-hellman.md`**（A1+根因②+A2/A3/A4 为主）。

**✅ 本轮线上确证生效的治本(价值入袋, 非单测)**:
1. **流程比任何一轮跑得远**: ANALYZE→TECH_DESIGN→REVIEW→PLAN→CONTRACT(10/10)→DISPATCH→执行14/23, 编排骨架全程无结构性崩。
2. **CONTRACT union-merge**(round7 a4f94bd): 同名接口并集不丢方法(NotifyUserQueryService/TaskConfigService/TotpService), 共享契约完整。
3. **sticky-fail 防幻觉 PASS**: st-9 编译真错维持未通过(关闭幻觉 PASS)。
4. **round6 仅重派失败/保留完成态**; **A2 确定性补依赖**(ruoyi-quartz 据 pom 自证); **定向恢复有界**(2次上限→落兜底不死循环); **B2 超预算 checkpoint 保留进度**; **跨模块 pull-back errors=0**; **端点 0 真 outage**(所有 eye3=fallback 降级=重压下负载卸载 benign, 非 round8 P-2)。
5. **CONTRACT 大模块超时退避有界自愈**(O1b: module5 第3次成功; module7 收尾)。

**错误类别收敛**: 本轮 = **timeout 主导**(st-14 family + st-7-2/9, 大切片/大逻辑层 900s 编不完) + **符号臆造 1**(st-9 SecurityUtils 框架类)。**均能力/超时类, 无新系统结构 bug**。

**📌 两个新治本候选(下次迭代)**:
- **P-2 框架类臆造**: 模型默认引用 canonical RuoYi 类(SecurityUtils)但此变体无 → cannot find symbol, import-repair 修不动。治本=注入**基线派生**的框架工具类白名单/FQN hint(非硬编码), 让模型用基线真有的类。同 round10 O4 / BaseController 白名单族。
- **P-3 缺类被当缺依赖→A2 空转(低效错路由)**: cannot-find-symbol 的【缺项目内类/方法】被归入【缺依赖】→A2 依赖修复(补 pom 坐标)对臆造的类无效→2次耗尽→落 retry→同样 compile fail→空转(沙箱累积25→33, 拖慢收敛, 本轮主动取消的直接诱因之一)。治本=HANDLE_FAILURE 区分两类符号错: 缺 jar 坐标→A2; 缺项目内类型→redecompose/换实现 hint/直接 escalate, 别耗 A2 配额空转。**有界但浪费配额+拖时**。

**结论**: round11 把 round10-after 一批治本(CONTRACT union/sticky-fail/round6/A2/定向恢复有界/端点稳)线上验证为**生效**; 唯一未线上验证的 pom MERGE 并集 B+A(6b39449) 因未到 MERGE 仍只有单测+逻辑验证。剩余瓶颈=**本地模型在大切片/框架类上的能力天花板**(非编排 bug), 及 P-2/P-3 两个确定性 hint 治本空间。

## A5/A6/A7 评估（通用性纪律下的取舍，非懒惰）
- **A6 预算 repair 饥饿 [不强修]**: 既有系统闸门已覆盖——LOCATING 硬砍预算(RUN12, executor.py:795 留给 CODING/VERIFY) + P7 fix 循环早停(executor.py:931, 60% 预算且闸门红即 bail 不烧满 900s)。"851s CODING 后 repair 0s"是 CODING 合法用满；强加固定 repair 预留会**削 CODING 预算→大子任务更易超时(反向回归)**。A1/A2/A3 已消除使其显形的注定重试 grind。故不强修(避免反治本)。
- **A5 契约模块↔布局不符 [defer]**: rule5 仅告警, round11 未因它崩(killer 是 A1/②)；真修需辨"逻辑模块 vs 物理 Maven 模块"意图, 过度工程风险。记 follow-up。
- **A7 符号扫描重复 [perf follow-up]**: VERIFYING/PRODUCING 重跑同一 4000-符号 grep(60-180s 浪费)。缓存是干净通用 perf 赢(无正确性 trade-off), 但属效率非 round11 致因。记 follow-up, 可后续单独做。

## B 类（能力 artifact·既有闸门已抓+恢复·不加 hack·通用性纪律）
- **B4/B5/isE mpty [不修, 诚实记 B 类]**: 模型 token 退化(`isE mpty`×6 文件 / `is Empty` babble / 臆造 `GoogleAuthenticatorConfig.Builder`/`CipherUtils.encrypt`) + 盲目 `perl -i 's/Builder/builder/'` 自毁。取证: 沙箱 jsonl `isEmpty` 37×对 / `is Empty` 14× / `isE mpty` 仅 1×(非确定性 pipeline 变换=非写入 bug，是模型不一致 splitting=能力)。**既有系统闸门已捕获+恢复**: L1 编译闸门抓 `cannot find symbol` → sticky-fail 不放幻觉 PASS → 换备选模型重试。故**不加定向 gate/后处理**——那是打地鼠+治标+可能误伤合法代码(违 [[swarm-fix-generic-systemic-not-whackamole]])。缓解靠 A4(诊断 hint) + 根因②(框架变体钉死降低臆造诱因) + 既有 alternate-model swap。

## 失败子任务归因 (随跑随记, 区分能力 vs 系统 bug)
- **st-7-2** ❌(重试中) **能力**: 本地模型陷入循环反复输出 `List.isNot()` 乱码(LLM 输出完全混乱) + 缺 ruoyi-quartz 依赖; 重试又撞 900s 超时(B2 分阶段编码超预算 checkpoint git add 保留进度=韧性✓)。系统侧处理正确(retry_alternate + A2 确定性补依赖 + 定向恢复 + sibling 保留)。非系统 bug。
- **st-9** ❌ **能力(符号臆造框架类)**: 模型 `import com.ruoyi.common.utils.SecurityUtils` 并调用——`SecurityUtils.java` **全基线零命中**(此 996 变体无此 canonical RuoYi 类; ruoyi-alarm pom 已依赖 ruoyi-common/framework/system, 故非缺依赖非-am 漏编)。`cannot find symbol: class SecurityUtils` 编译失败; import-repair 修不动(无法 import 不存在的类)。+ 修复尝试撞 900s 超时。**治本正向**: ✅sticky-fail `L1 不翻盘(prior fail source=compile 确定性真错误→维持未通过, 关闭幻觉 PASS)`。**治本候选**: 注入基线派生的框架工具类白名单/FQN hint(同 round10 O4 / BaseController 白名单), 让模型用基线真有的类而非 canonical RuoYi 默认类。
- **P-2 [错误类别·新显形] 符号臆造-框架类**: SecurityUtils 类不存在于此变体却被默认引用。与 round8 P-1(当时判 framework 类在 classpath, 本轮证伪: 此变体确无 SecurityUtils 源)/round10 O4(import-repair 缺 FQN) 同族。属能力, 但有确定性 hint 治本空间。
- **P-3 [系统·低效错路由·疑似治本点] 缺类被当缺依赖→A2 空转**: 15:32–15:42 进度停滞, `定向恢复达上限(2次)仍缺依赖→常规retry 兜底` 反复×3+。疑因=【cannot find symbol: 缺**类**(如臆造 SecurityoUtils)】被归为【缺**依赖**】→A2 依赖修复(据 pom 补坐标)对「不存在的类」无能为力→2 次耗尽→落 retry→retry 同样 compile fail→空转(沙箱累积 25→29)。**治本候选**: HANDLE_FAILURE 区分 cannot-find-symbol 的两类——缺依赖(jar 坐标可补, A2)vs 缺类型/缺符号(项目内不存在的类/方法, A2 无效, 应走 redecompose/换实现 hint 或直接 escalate, 别耗 A2 配额空转)。**有界**: 最终 retry 耗尽→escalate→PARTIAL, 不会死循环, 但浪费配额+拖时+沙箱累积。
- **15:42** 盯#16: DISPATCHING 14/23 **停滞~10min**(已完成17 持平#15)。eye3 真 outage 0, 沙箱 29(累积)。见 P-3。watcher 若 30min 无完成增量或失败循环达阈值会自动取消; 否则有界 escalate→PARTIAL。
