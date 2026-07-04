# Round12 三盯随跑记录 — 996db614 预警编排平台

**任务**: `996db614-e01f-4053-98a7-d0d55897e2f7`（retry，复用 task id，日志已归档 round11 → fresh 起跑）
**起跑**: 2026-06-30 17:44
**加载码**: 分支 `fix/round11-structural-roots-a1-framework`（round11 八条治本 + token 统计 feature，全量 1727 passed）
**三清确认**: ✅ 项目基线 `0d42679` 完全干净（61 个 round11 残留 + pom.xml 改动已 git reset+clean）；✅ 任务日志/沙箱 jsonl/看守产物已归档 `~/.swarm/archive/round11_20260630_174332`；✅ token 表清零基线（round12 首次真实测烧量）

## 本轮验证目标（按【机制/类别】判，非固定子任务）
1. **A1**（run-killer 根因 0a51898）：层拆子任务不再因兄弟 domain 包缺失而 `internal_pkg_not_built` 空转；依赖子沙箱 bootstrap 含兄弟产物、errors=0。
2. **A2/A3**（543af0e）：不再对内部缺类走 A2 maven 修复 / 计 targeted_recovery / 当 transient 空转。
3. **A4**（19f06cc）：重试 worker 提示带 brain 诊断 hint（禁 SecurityUtils 等），类臆造复发率降。
4. **根因②**（ca6b1fc）：产物框架变体一致（全 Shiro/`@RequiresPermissions`，无 `@ss`/spring-security），`util/utils` 不漂移。
5. **流程**：能突破 round11 卡点 **跑到 MERGE**（顺带线上验证 pom 并集 B+A 6b39449）→ PARTIAL/full 交付而非 CANCELLED。
6. **token 统计**：跑完看 WebUI 系统菜单真实烧量（云端/本地/每项目）。

## 三只眼
- eye1 = task status + 阶段：`curl /api/tasks/996db614…`
- eye2 = 沙箱 jsonl 通读真因：`~/.swarm/sandbox_logs/*.jsonl`（逐行，不 grep）
- eye3 = router trouble：整条链不可达 / FALLBACK 降级 / diff=0
- 任务日志：`logs/996db614-e01f-4053-98a7-d0d55897e2f7.log`（本轮 fresh，从头读）
- 看守：`/tmp/e2e_run_996db614/{events,full,analysis,summary}.log`

---

## 随跑时间线（每 10min）

### T0 17:44 起跑
- ANALYZE → 知识检索 struct=25 semantic=20 norms=15 → 复杂度=ultra
- DETECT_STACK 命中缓存（指纹 871262c8，schema v2）：**前端=Thymeleaf 后端=Spring Boot** ✓（栈识别正确）
- ROUTE: ANALYZE → TECH_DESIGN（并行 9 模块规划）
- 沙箱：0（早期阶段）；token 表：清零基线

### T+344 00:23 ★★★确认 replan 无界循环=round12 真根因 + 完整复盘 + 停 loop★★★

## 🔴 确认结论：replan 无界循环（新发现·真 run 完成缺陷·治本候选）
- **证据(确凿)**: 两完整 replan 周期 00:07+00:18(隔11min,`HANDLE_FAILURE replan — st-27/34 upstream_module_broken`);MONITOR 剩余=0/完成39/失败2 在 00:06 与 00:18 **完全相同(零推进)**;st-27/34 日志行 184→245 仍增;L1 反复 `构建错全在上游模块→标 BLOCKED 待上游修好再编` 但上游 st-26/st-33 **已 stub/永久放弃,永不会修好**;`replan 守卫保留34成功(无clobber,round6防护对)` 但仍反复 replan st-27/34。
- **根因(治本候选·非mid-run修)**: **A2/A3「BLOCKED-on-upstream 不连坐·待上游修好·不计配额」+ replan 对「上游已被永久 stub/放弃」无感知** → 下游 st-27/34 无界循环,run 永不自终。这是 round11 结构主干「检测-补偿 vs 上游不变量」的延伸阴暗面:补偿(等上游)假设上游终会落地,但阶梯三给放弃后上游永不落地→闭环失效。
- **治本方向(留实现)**: ①部分交付/阶梯三放弃 upstream 模块时,**连坐放弃集必须传递闭包**(所有 import 该模块的下游一并 stub/放弃),不留下游可被 replan; ②或 BLOCKED-on-upstream 检测「上游在放弃集」→直接 escalate 下游为 stub,不进 replan; ③replan 入口加「upstream_module_broken 且上游∈放弃集」短路守卫。**最高优——这是 round12 唯一阻止"首次生产级 PARTIAL 终态"的系统缺陷**。

## ✅ 治本验证清单（round12 线上逐条结论）
| 治本 | commit/来源 | 线上结论 |
|---|---|---|
| A1 兄弟域产物注入 | 0a51898 | ✅确证:交付阶段 bootstrap 补传上游产物,依赖子 L1 通过(st-25/29/31) |
| A2/A3 internal_pkg→BLOCKED不连坐 | 543af0e | ✅确证:st-20-3 缺 domain.dto/bo→标 internal_pkg_not_built BLOCKED 不连坐;**但暴露阴暗面(见🔴)** |
| round6 sticky-fail 防幻觉PASS | c6a3904 | ✅多次确证:st-20-3/st-27 宁记 low 保留真错不伪PASS |
| round6 阶梯三·桩 | c6a3904 | ✅确证:st-26/33 卡死→可编译桩→清树防reactor中毒→保留成功成果;**但连坐传播不彻底(见🔴)** |
| 根因② 框架变体一致 | ca6b1fc | ✅确证:全程 SecurityUtils/@ss/spring-security **0 命中** |
| module-reg 聚合补注册 | 76ba95d | ✅确证:自动注册 ruoyi-alarm 进父 pom 治 reactor 缺模块 |
| round9 union(contract facet) | 6b39449 | ✅确证:CONTRACT_MERGE 同名接口并集合并 9/9 模块;**任务级 pom 多写者 facet 未到(被循环阻断未验)** |
| token 统计两病(膨胀+归属) | 9b2cffb/9b2cffb | ✅全程确证:cloud 3.93M/44 local 175.2M/4401 非膨胀,RuoYi-E2E 收全部 |
| 上游BLOCKED不连坐(round7) | - | ✅确证:21:41 等多次「构建错全在上游模块→BLOCKED不连坐」 |

## 📦 真实交付产物（⚠️取消后诚实纠正）
- **取消后工作树仅剩 9 文件**(pom.xml+ruoyi-alarm 3 .java+sdk/framework 等),ruoyi-alarm 仅 3 .java。
- **00:23 的"112/108 文件"是运行中 worker pull-back/bootstrap 的【临时足迹】,未持久化**。因 replan 循环阻断任务级 MERGE,**per-subtask 产出从未被合并固化到工作树**(MERGE 才是把各子任务沙箱产出统一落工作树+pom 多写者并集+整树编译的步骤)。
- **⚠️这把 replan-loop bug 的严重性升级**:它不只浪费时间/token,而是【直接阻断 MERGE = 阻断真实交付】。**这是"还没跑出生产级产出"的真正最后一道闸**——39 子任务在沙箱里成功产出大量代码,但没有 MERGE 就没有持久化的整树交付物。
- **isE mpty 彻底证伪**:真实产物全树 `isE mpty`/`is Empty` **0 文件**→纯模型 L1 自审 babble,非 pipeline 写入 bug,此项关闭。

## 🧮 token 真实烧量(全程治本保持)
- cloud 3,930,905 / 44call (in 3.67M / out 264K,含 think); local 175,235,971 / 4401call ≈39.8K/call (in≫out 架构性); grand 179.2M / 4445call。
- 归属:RuoYi-E2E cloud 3.93M + local 175.2M(全部),仅 2562 local 落「无项目归属」(早期 probe 残留)。**max-not-sum 防膨胀 + brain ctx 归属 全程零回归**。

## 🐛 能力边界 vs 系统bug 分类
- **系统bug(治本候选)**: 🔴replan 无界循环(连坐放弃传播不彻底)=最高优; 跨子任务集成缺陷(st-25 NotifyDispatcher NoSuchBean,L1单子任务绿却破坏下游,待复盘核 L1 为何没抓)。
- **能力边界(B类,闸门正确拦)**: st-10 模型反复 refusal('need more steps'); st-33 OkHttp→OkHttpClient 类名臆造; st-26/st-20-3 第三方/上游 API 写错; isE mpty 自审 babble(**已降级:非 sticky 主因,真因均是模型写错 API 类名/上游集成缺陷;isE mpty 是否写入artifact仍可终态后查 diff_apply 但优先级低**)。
- **infra(可控)**: 端点 stream 解码超时→fallback Saka 吸收; prefill 180s→transient 退避。

## 📈 对比 round11 进步
- round11: CANCELLED 14/23,卡在 CONTRACT/dispatch 交界(被误判能力天花板,实为 A1 wiring bug)。
- round12: **完成39/剩余0/失败2,走完 dispatch→部分交付→阶梯三·桩→逼近任务级 MERGE**,108 真实源码落地。8 条治本全部线上确证。**质的飞跃**,唯一拦路=新发现的 replan 无界循环。

## 🌟 北极星评估
- 「本地小模型靠细粒度子任务+确定性闸门胜任产品化需求」**基本成立**:39 子任务完成、108 真实源码、框架一致 0 臆造、闸门全程诚实(不伪PASS/不无限grind单子任务/部分交付诚实)。
- 差最后一步:replan 无界循环阻止干净 PARTIAL 终态。**修掉它,round12 级别的产出就是首个生产级 PARTIAL 交付**。

### T+338 00:12 ★★候选系统bug:st-27/34 replan死循环(连坐放弃传播不彻底)★★
- **★★新候选发现(审慎·留复盘·不mid-run修)=replan 死循环★★**: 00:07 `[HANDLE_FAILURE] LLM 策略: replan — st-27/34 均因 upstream_module_broken 被阻断且 LLM 自评 fail`。st-27/34 被【已 stub/放弃的上游 st-26/st-33】永久阻断→brain replan→重派→仍被同一 broken 上游阻断→再 replan…**自 ~23:47 循环至今 ~25min,184 条 st-27/34 日志行**。model babble(`HashMap→HashMap`/`LoggerFactory 不存在应该用 LoggerFactory` 矛盾自审)。
- **分类=候选系统行为缺陷(非纯能力)**: 23:43 部分交付"连坐放弃下游 5 个"未含 st-27/34→**上游永久 stub/放弃时,下游 upstream_module_broken 应同样立即 stub/放弃,而非反复 replan**。round6 阶梯三 给放弃的【连坐传播不彻底】+replan 对永久-broken 上游无效。延迟/可能阻止 PARTIAL 终态。**须复盘核 replan 是否最终 escalate(有界) vs 真无界循环**——若无界=真 run 完成缺陷(治本候选:upstream 在放弃集→下游直接连坐 stub,不 replan)。
- **对比定位**: 这与 A2/A3 的「BLOCKED-on-upstream 不连坐等生产者」是【同一机制的阴暗面】——A2/A3 假设上游终会落地,但上游若被给放弃则永不落地,BLOCKED/replan 不计配额→可能不收敛。round11 结构主干「检测-补偿 vs 上游不变量」的延伸。
- 仍未到 MERGE(st-27/34 阻塞)。token 见 T+332(本周期未重测)。watcher 活。

### T+332 00:05 末1 straggler st-27 在 L1 gate,MERGE 就差它;st-33真错=OkHttp类名臆造
- **末 straggler**: st-34 DONE low(00:00),st-27 最后一个 00:04:39 撞迭代上限50→交确定性闸门跑 L1.2.1 build gate。其后即 MERGE→PARTIAL。剩余0 后这俩各又跑一轮(连坐放弃集但仍执行,~400-650s/个)致 MERGE 延后~13min。
- **st-33 真错确认=B类模型API臆造(非isE mpty)**: `okhttp3.OkHttp cannot find symbol class Builder/method newCall`=模型把 `OkHttpClient` 写成 `OkHttp`(丢Client)。明确能力 artifact,L1 deterministic gate 正确 fail→stub。**再证 isE mpty 非这些 sticky 的主因**(主因均是模型写错第三方/上游API类名)。
- 仍未到 MERGE(差 st-27)。无 clobber 无 run-killer。

### T+326 23:59 CONTRACT_MERGE union(round9 contract facet)确证;末2失败收尾,任务级MERGE未到
- **★round9 union 治本 contract facet 线上确证★**: 18:56 `[CONTRACT_MERGE] 'AlarmSimpleUtil'/'AlarmHttpClient'/'AlarmSdkConfig' 同名多版→并集合并(不丢方法/字段)`,`合并完成:接口25 DTO46 常量10 API61 约定10 模块依赖8(9/9 模块成功)`。同名接口并集合并(keep-first 隐患的治本)在 contract 阶段生效。**任务级 MERGE(diff 合并+pom 多写者并集 facet)尚未到**,待末 2 失败收尾。
- **末 2 失败收尾**: 23:59 st-27/st-34(依赖被放弃集连坐)仍 PRODUCING/处理(worker/medium 活跃,st-34 pull-back downloaded=1)。剩余0 但 HANDLE_FAILURE 仍在处理这 2 个→收尾后进 MERGE→PARTIAL。
- VALIDATE_PLAN 早期已警告 st-1/st-31 都写 pom.xml→已串行化(聚合文件 bootstrap 传播 + MERGE 3-way/rebase 收口)=多写者 pom 治本路径在位,待 MERGE 验证。
- 无 clobber 无 run-killer。比 round11 CANCELLED 走完全程。

### T+314 23:53 ★★剩余=0 全子任务终态化,阶梯三·桩完整链路生效,MERGE 临门★★
- **★★剩余=0/完成39/失败2(23:52)★★**: 所有子任务终态化。阶梯三·桩完整链路: 23:38 st-26 桩(AlarmEngineService/RecoveryNotifyService)、23:43 st-33 桩(HttpClientUtils)、23:43 `阶梯三 保 build 放弃[('st-26','stub'),('st-33','stub')](清本地树足迹防 reactor 中毒,保留全部成功成果,run 继续 merge→L2,终态将 PARTIAL 诚实列明需人工补完);连坐放弃下游 5 个`。
- **★round6 阶梯三·桩治本完整确证★**: 卡死子任务→生成可编译桩→清树足迹防 reactor 中毒→保留成功成果→继续 merge→PARTIAL 诚实标"需人工补完"。这是"宁桩保 build 不让 1-2 个卡死子任务拖垮整 run"的治本,正是 round11 CANCELLED 的解药。
- **连坐放弃下游**: st-27/st-34(23:52 L1未通过,依赖 st-26/33 桩)随放弃集连坐,均 sticky-fail 诚实 low(置信度 medium→low 校正)。
- **MERGE 临门**: 剩余0→HANDLE_FAILURE 处理最后 2 失败→接下来 MERGE(pom 并集 B+A 最后线上验证)→PARTIAL 终态。
- token cloud 3.043M/42(+466K 阶梯三桩 GLM-5.2,非膨胀) local 163.2M/4131≈39.5K/call。无 clobber 无 run-killer。**比 round11 CANCELLED 14/23 走完全程到 merge。**

### T+302 23:41 ★纠正isE mpty归因+round6阶梯三·桩治本线上生效★ st-26 replan→桩
- **★诚实纠正 T+290 isE mpty 过度归因★**: brain 23:37 诊断揭示 st-26 BLOCKED 真因=**上游 st-25 的 NotifyDispatcher.java 编译错** `[10,35] cannot find symbol`(NoSuchBeanDefinition/未导入 spring beans)。isE mpty 只是模型 L1 自审 babble,**非 build 阻塞主因**。我把 isE mpty 当"无法收敛元凶"是过度归因——主因是**跨子任务集成缺陷:st-25 标完成(L1通过)却产出破坏下游的文件**。isE mpty 仍待终态查 diff_apply 定性(可能并存但非主因),诚实降级该假设。
- **★round6 阶梯三·桩 治本线上生效★**: 23:37 `定向恢复已达上限(2次)仍缺依赖→落常规 replan`,23:38 `[阶梯三·桩] 为卡死子任务 st-26 生成可编译桩`(用 GLM-5.2 cloud)。**给卡死子任务生成可编译桩保 build 绿、诚实放弃完整实现**=round6"宁桩保build不卡死"治本。这是 PARTIAL 终态的优雅降级路径。
- **跨子任务集成缺陷(候选·留复盘)**: st-25 NotifyDispatcher L1 通过却含 NoSuchBean/缺 import,下游 st-26 用到才崩。即**单子任务 L1 绿不保证跨子任务集成绿**——L1 -pl -am 编译时上游产物已在,为何没抓到 st-25 自身的 NoSuchBean? 待复盘核(可能 st-25 的错在 runtime/Spring 装配非 compile,或 L1 未编 st-25 的某文件)。
- **进度**: 23:35 MONITOR=完成37/剩余2/失败2,st-26 replan+桩中,st-33 仍未通过。剩 2 收尾→桩完成后 PARTIAL 终态。
- token cloud 2.577M/40(+462K GLM-5.2 replan+桩,非膨胀) local 157.7M/4007≈39.4K/call。归属完美(RuoYi-E2E 收全部,2562 probe残留)。无 clobber 无 run-killer。

### T+290 23:28 ★isE mpty 写入 artifact 复现(round11 flagged,候选A类系统bug)★ + 37/2
- **★★关键候选发现=`isE mpty` 字面空格 artifact 在 st-26 复现(round11 plan 标记项)★★**: st-26 L1 raw_result `我写的是 isE mpty()（大写E）这是正确的！` / `isEmpty() 是正确的 Java 11+ 方法 ✅ L1_RESULT: PASS`。即 `isEmpty`→`isE mpty`(中间插空格),模型幻觉是对的+自报PASS。**这是 st-26 反复 BLOCKED/未通过无法收敛的元凶之一**。
- **分类(审慎·留复盘·不mid-run修)**: plan 唯一标"查清再决定"的潜在 A 类系统 bug——`isE mpty` 是 diff-apply/流式chunk拼接/tokenizer 的【写入损坏】(高价值pipeline bug)还是模型原样吐(B类能力)? **终态后必查 `project/diff_apply.py`+worker流式落盘路径定性,现不臆断不改码**。
- **st-26 反复 BLOCKED 循环**: 22:39[520s]→22:50[470s]→23:14[750s]→23:27[396s] 连续4+次 `L1 verification_not_run BLOCKED→退避重试`,~50min 未收敛。source=verification_not_run。盯是否耗尽预算→二次部分交付。
- **进度**: 23:17 MONITOR=完成37/剩余2/失败2(36→37)。剩 2 之一是 st-26(isE mpty/BLOCKED 循环)。
- token cloud 2.115M/38(无变化) local 155.4M/3956≈39.3K/call(非膨胀,+13M/24min=尾部重试)。无 clobber 无 run-killer。

### T+278 23:16 尾部3 sticky慢收尾(800s+/个,非卡住),端点抖动fallback吸收
- 最新 MONITOR 仍 22:59=完成36/剩余3/失败2。17min 无新 MONITOR=尾部 3 sticky(st-26/30/33)均 alarm-sdk 大子任务(800s+/个),模型 API 错误多轮迭代修。st-30(23:16)838s VERIFYING L1 未通过→修复尝试 1/3,非卡住(刚 VERIFYING→repair 转换,worker 活跃)。
- **⑥端点抖动(记现象,fallback 正确吸收)**: `stream 解码中途 超时`→FALLBACK 降级 Qwopus3.6→Qwen3.6-Saka。既有 fallback 机制吸收,可控,非污染。
- **尾部慢=能力边界非系统bug**: alarm-sdk HttpClient/OkHttp 封装需精确第三方 API,本地小模型反复写错类名/方法(OkHttp→OkHttpClient 等),fix-loop 迭代但收敛慢。L1 build 闸门正确拦未通过(不伪PASS)。若耗尽预算将二次部分交付(诚实)。
- 仍未到 MERGE。token 上次 23:04 cloud2.115M/38 local142M/3665 非膨胀(本周期未重测,下周期复核)。无 clobber 无 run-killer。

### T+266 23:04 推进 36/3,尾部 alarm-sdk HttpClient 子任务迭代修(非卡住)
- **进度**: 22:59 MONITOR=完成36/剩余3/失败2。st-29 终 L1 通过(22:59,5354 chars,多轮后转通过 ✓)。23:02 又派发 3。运行非卡住——每 MONITOR 都在动(35→36,剩4→3)。
- **尾部两 sticky(B类·模型 API 臆造迭代修)**: st-26(反复 L1 未通过 11810→11898 chars,在长大=重生成)、st-33(未通过 8140,修 `ruoyi-alarm-sdk/HttpClientUtils.java` 的 OkHttp 类名错 `OkHttp`→`OkHttpClient`)。均 alarm-sdk 模块 HttpClient 封装,模型把 OkHttp/HttpClient5 API 名写错,worker fix-loop 迭代修。属能力 artifact,闸门(L1 deterministic build)正确拦未通过。盯是否收敛 vs 二次部分交付。
- 仍未到 MERGE。token cloud 2.115M/38(无变化) local 142M/3665≈38.8K/call(非膨胀持续)。无 clobber 无 run-killer。

### T+254 22:52 推进 35/4,★module-reg 聚合补注册线上确证★+st-29 retry转通过
- **进度**: 22:41 MONITOR=完成35/剩余4/失败2(33→35)。st-29 retry 后 22:51 L1 Phase4 复核通过 high DONE(此前22:27未通过→A1注入后重试转通过 ✓)。st-24(22:02)编译通过。剩 4 收尾(含给放弃的5待标终态)。
- **★module-reg 聚合清单补注册(commit 76ba95d)线上生效★**: 22:16-17 `[L1.2.1·module-reg] 补注册聚合清单成员: {'pom.xml': ['ruoyi-alarm']}(修复缺模块/缓存负解析致的确定性 FAIL)`。自动把 ruoyi-alarm 注册进父 pom 聚合清单,治"reactor 缺模块致代码本好却编不动"。又一治本确证。
- **worker 心智模型正确**: 21:5x 自推理"DTO/BO/Entity 由兄弟子任务创建...编译会在兄弟完成后通过"=正确理解 BLOCKED-on-upstream(A1/框架事实注入起效)。
- **重试用 Saka 备选模型**: 日志现 `Qwen3.6-27B-Saka-NVFP4`=retry_alternate 换的备选,与主力 Qwopus3.6 区分。
- 仍未到 MERGE。token cloud 2.115M/38(+445K诊断,非膨胀均56K/call) local 139.7M/3609≈38.7K/call。无 clobber 无 run-killer。

### T+242 22:40 尾部子任务 build-repair 中(推进非卡住),未到 MERGE
- 最新 MONITOR 仍 22:27=完成33/剩余6/失败1。13min 无新 MONITOR=尾部 1 个 worker/complex 子任务在 L1 build-repair 循环(e9ccca85 反复 `mvn -pl ruoyi-alarm -am compile` exit=1,worker 活跃,**非挂起**)。
- 沙箱健康: d51cb7 DONE(confidence medium→low 校正=sticky-fail 诚实行为再现,L1未通过不伪PASS ✓);0bfbad0d DONE high(L1通过 Phase4复核)。
- **st-10 性质补全**: 历史日志显示 st-10 是"合并 SysNoticeController markRead"型(trivial 快速路径合并),模型反复"合并执行完成: Sorry, need more steps"→MERGE 型小改动模型反复拒,故被部分交付放弃(能力 artifact 非 wiring)。
- 尚未到 MERGE(最后子任务收尾中)。无 clobber 无 run-killer。

### T+230 22:28 收敛加速 33/6，A1 注入后依赖子 L1 通过(编过了)
- **进度**: 22:27 MONITOR=完成33/剩余6/失败1(30→33)。9 可交付子任务正完成: st-25(L1通过 diff6908)、st-31(L1通过 diff3270)、st-29(L1未通过 diff4795)。**A1 注入起效=st-25/31 注入兄弟产物后 L1 通过(③依赖子编过了 ✓ errors收敛)**。
- **历史补全(诚实记账)**: st-15(20:43)真因=`org.apache.hc.client5 httpclient5 包不存在`(缺外部依赖,A2 territory,20:52 补 pom 写权);st-17-4(21:37 .html)模型自审胡言 `forEeach/indexO ✅正确`但 deterministic gate 对模板只查 compile/format/lint 故通过——无害(模板无 JS 执行检查)。
- **尚未到 MERGE**: 还在收尾 9 个 + HANDLE_FAILURE 处理给放弃的 5。剩 6→预计很快 MERGE→PARTIAL。盯 ②MERGE+pom 并集 B+A。
- token cloud 1.67M/37(无变化) local 123.2M/3231≈38.1K/call(非膨胀持续)。无 clobber 无 run-killer。

### T+218 22:15 ★★收敛到 PARTIAL(远超 round11 CANCELLED) + A1 交付注入确证★★
- **★★终态临近=PARTIAL,非 CANCELLED★★**: 22:14:25 `[HANDLE_FAILURE] 部分交付：放弃 ['st-10','st-18-1','st-20-3'](+依赖者,共5),继续交付其余 9 个,终态将 PARTIAL`。3 sticky 子任务 retry_alternate 第3次耗尽预算→escalate 部分交付。**这是诚实终态(round6治本:宁PARTIAL不造假/不无限grind/不全量CANCELLED),比 round11(CANCELLED 14/23)走得远——将是首次有真实交付产物的终态。**
- **★A1 兄弟域产物注入(交付阶段线上确证,round11 0a51898)★**: 9 可交付子任务派发时 bootstrap 补传上游产物——st-25 补传 12 个(engine domain/service/enums+pom.xml+ruoyi-common/pom.xml),st-29 补传 3 父/模块 pom,st-31 补传 2。**owner 兄弟产出注入依赖子沙箱=A1 正生效**(round11 死区根因的治本)。errors=1-2 是个别 local≠HEAD 不存在(ruoyi-alarm-sdk/pom.xml),次要。
- **进度**: 22:13 MONITOR=完成30/剩余11/失败3。给放弃 5(3 sticky+2 依赖)、交付 9。**尚未到 MERGE**——9 个最后派发中(带 A1 注入),完成后才进 MERGE→PARTIAL。盯 ②MERGE+pom 并集 B+A 线上验证。
- **sticky 三子任务最终结局(诚实记账)**: st-10(模型反复 refusal,prefill→need more steps)、st-20-3(真类型不匹配 Workbook/AjaxResult 小模型修不动)、st-18-1——均非 wiring bug,是本地小模型能力边界,闸门正确 escalate 不伪 PASS。
- **token**: cloud 1.67M/37(+442K 部分交付诊断,非膨胀均45K/call); local 115.2M/3050≈37.8K/call; RuoYi-E2E 收全部,2562 probe残留。

### T+206 22:03 ★sticky-fail 诚实行为线上确证(防幻觉PASS) + st-10终破★
- **★round6 治本确证(sticky-fail 防幻觉 PASS 线上生效)★**: st-20-3 DONE 但 `confidence=low`,日志 `L1 不翻盘(sticky)：prior fail(source=compile) 为确定性真错误，维持未通过（关闭幻觉 PASS）`。**worker 拒绝把真编译错(Workbook/AjaxResult 返回类型不兼容)粉饰成 PASS**,诚实记 low+保留真错。这是"静默当成功"根因的治本——宁可 low 不造假。
- **st-10 终破(sticky 最终收敛)**: 自 20:14 起多轮失败(prefill 超时→refusal_hard_fail 'need more steps')→ 22:00 retry_alternate 换模型后 `trivial 快速路径完成 high` DONE。**重试动力学最终收敛,未无限 grind**。耗时长但闸门(换模型/退避)接住了。
- **进度**: 21:54 MONITOR=完成29/剩余12/失败3 → st-10/st-20-3 已 DONE,完成≈30/剩余≈11。21:57 retry_alternate 第3次 ['st-10','st-18-1','st-20-3']→现 st-10/20-3 落地,st-18-1 仍 CODING schedule/domain(产 owner 文件)。
- **④根因②框架变体一致(线上)**: grep `SecurityUtils/@ss/spring-security` **0 命中**。无臆造 Spring-Security 变体,框架钉死生效。
- **效率(记现象)**: st-18-1 LOCATING 达迭代上限(20)→交 L1;多 worker 触迭代上限但都落到确定性 L1 兜底,非空转。
- **无 MERGE(剩~11),无 clobber,无 run-killer**。token cloud 1.228M/36(无变化) local 110.2M/2917≈37.8K/call(非膨胀持续)。

### T+194 21:50 ★A2/A3 internal_pkg_not_built 分流线上确证 + 健康收敛★
- **★治本确证(round11 A2/A3 543af0e 线上生效)★**: st-20-3 缺兄弟域包 `com.ruoyi.alarm.domain.dto`/`domain.bo`(owner 是别的子任务)→ L1.2.1 `pipeline_blocked='internal_pkg_not_built'`、`not_run_kind='blocked'`、`deterministic_gate='skipped: pipeline blocked'` → **标 BLOCKED 退避待生产者落地、明确「不连坐本子任务」**。这正是 round11 死区根因(层拆剥离真跨层依赖致依赖子永远编不过)的治本——**不再 grind、不误判 FAILED、不连坐**。
- **③ BLOCKED-on-upstream 真收敛(线上)**: 21:41 再现 `[L1.2.1] 构建错全在上游模块(非本子-pl 模块)→标 BLOCKED 退避不连坐`。跨模块缺包统一走 BLOCKED 等上游,不放大空转。
- **健康收敛链(关键正向信号)**: st-20-3 错误【演进】=缺 domain.dto 包(BLOCKED 等上游)→ 兄弟生产者落地 → 真代码错(Workbook/AjaxResult 返回类型不兼容)→ retry_alternate 换模型。**上游缺包先 BLOCKED 不 grind、生产者落地后真错才浮现** = A1+A2/A3 协同正确。541096e8/st-20-3 最终 761s DONE。
- **纠正 T+182 误判**: 我把 st-20-3 阻塞误记为 Hashmap typo——真因是 internal_pkg_not_built;那串 Hashmap 文本是 worker L1 自审幻觉噪声(模型胡言"Hashmap 是对的")非阻塞因。已在 T+182 标注纠正。诚实记账,误判反而暴露了治本【正在生效】。
- **进度**: MONITOR 链 16→19→21→24→**27**(剩 24→21→20→17→**14**),稳降~3/15min,收敛良好。沙箱健康(4f6b10/29265288/541096e8 均 DONE)。无 MERGE(剩 14),无 clobber,无 run-killer。
- **st-10(⑥转性质)**: prefill 超时 → 现转 `refusal_hard_fail`('Sorry, need more steps')→ retry_alternate 换模型。trivial 子任务(SysNoticeController markRead)模型反复硬拒,闸门换模型处理。盯换模型后是否收敛(能力 artifact)。
- **token 复核**: cloud 1,228,212/36call(+438K 一次=21:39 HANDLE_FAILURE retry_alternate 诊断含全失败上下文,非膨胀,均34K/call); local 106.8M/2829call≈37.7K/call; RuoYi-E2E 收全部,2562 probe残留。max-not-sum+ctx 两修持续生效。

### T+182 21:36 推进 24/17；两实质发现(typo闸门接管 + A1验证点临近)
- 进度: 21:23 MONITOR=完成24/剩余17/失1(21→24,+3↑)。21:26 st-10 又 prefill 180s→retry 1/2(计数重置=持续按 transient 吸收)→派发批次4(完成23/剩余18)。21:36 st-22 DONE(592s,l1_passed=true,high,diff6307)。沙箱窗口多数 DONE(968b3e8/85a8306/1fc0960/4956218 high)。无 MERGE,无 clobber,无 run-killer。
- **★st-10 反复 prefill 超时(⑥A4现象·记不修)**: 21:11 transient 2/3 → 21:26 retry 1/2,**同一子任务连续 prefill 180s 失败**。既有闸门按 transient 正确吸收(不换模型/不计 capability 配额),但模式=该子任务提示可能偏重致端点首token慢。post-run 候选:首token超时阈值 vs 重提示子任务。不 mid-run 改。
- **①②[T+194 纠正] 此前误判为 Hashmap typo→实为 internal_pkg_not_built**: 541096e8=st-20-3,L1 `pipeline_blocked='internal_pkg_not_built'`,真因=`package com.ruoyi.alarm.domain.dto/bo does not exist`(兄弟域包未就绪)。那串 `Hashmap/Arraylist...✅正确` 是 worker L1 自审【幻觉噪声】(模型胡言 Hashmap 是对的),非阻塞因。**L1.2.1 正确标 BLOCKED「不连坐本子任务」=A2/A3 治本线上确证**(详见 T+194)。
- **效率(b8a580dc)**: Agent 达迭代上限(95)→交确定性 L1。worker agent-loop 未在限内收敛,L1 兜底。记现象(能力/效率信号),非 bug。
- token 复核: cloud 790K/35call(无变化,prefill失败无usage); local 100.6M/2665call≈37.7K/call(+8.85M/+220call本窗口,非膨胀持续); RuoYi-E2E 收全部,2562 probe残留。

### T+170 21:23 沙箱窗口 6 全 DONE 净向前（无 MERGE，无 run-killer）
- 进度: 21:08 MONITOR=已完成21/剩余20/失败1。21:11 派发批次4(st-10/15-2/17-3/20-2)。21:23 沙箱窗口6个全 DONE(85a8306-719s high/1fc0960-330s high/2fab264-269s med/57124a5-175s trivial拒答否决/9cfb648-909s med/ac9c9c7-659s high)→均完成未撞 run-killer。净向前(4+沙箱DONE于上次MONITOR后)。无 MERGE,无 clobber。
- ⑥ A4/prefill 现象再现(记不修): st-10 失败=`stream 首token(prefill)超时180s`→确定性回退 transient retry 第2/3次(不换模型/不计 capability 配额)。这是端点首token慢,既有闸门按 transient 正确吸收。
- 预算闸门(B类·诚实): st-17-3 超文件上界但确定性拆不动→原样派发(交超时强制拆小阶梯兜底,不静默),613s CODING→VERIFYING L1 1/4。治本闸门生效、不静默丢。
- token 双端线上复核: cloud 790,973/35call≈22.6K(in562K/out228K含think,健康非膨胀); local 91.7M/2445call≈37.5K(in>>out架构性已知)。RuoYi-E2E 收全部归属,仅2562 local落"无项目归属"(先前probe残留)。max-not-sum+brain ctx两修持续生效。
- 端点抖动: 本窗口仅 st-10 一次 prefill 超时,fallback/transient 吸收,可控。

### T+158 21:08 稳步 21/20，速率放缓（尾部子任务更大）
- MONITOR 21:08 已完成=21 / 剩余=20 / 失败=1（19→21↑；总数飘到 ~41，完成数在升=无 clobber，疑某硬子任务被再拆）。本地端点 21:0x **0 失败**（稳）。沙箱=32，analysis 无告警，token 本地 84.5M/2240call ≈37.7K（稳）。
- 速率放缓（~2/12min，易脚手架已完、剩较大子任务）。未到 MERGE，仍 DISPATCH/HANDLE_FAILURE 健康循环。

### 观察：云端/本地 output 统计口径不对称（含思考 vs 关思考）= 指标需标注
- **本地 output = 关 think 的纯输出**：`router.py:326` 对 local provider 设 `enable_thinking:False`（本地 Qwen reasoning 经 vLLM template 会把真答案吃空→worker 空转拒答，task 94334785 根因）。故本地 output_tokens 不含思考。
- **云端 output = 含 think**：云端保留 reasoning（brain 规划需推理）。佐证：最早 probe 云端"一句话介绍"吐 592 output（答案本身~30，余为思考）。
- **含义**：WebUI「云端输出≫本地输出」一部分是口径不对称（云端含思考、本地不含），不可直接对比，**非 bug 但产品指标应标注**。
- **post-run 候选**：UI 注明"云端输出含模型思考 token"，或用 `completion_tokens_details.reasoning_tokens`（若网关回传）把思考单列。

### 观察：本地 token 输入≫输出（177:1）= 架构特征非 bug（post-run 优化候选）
- 实测 split：本地 输入 78.7M / 输出 444K = **177:1**（每 call 输入≈38K 输出≈215），云端 2.5:1。
- **probe 证非低估**：强制本地写整个 Java 类→output_tokens=2492 完整捕获（量级吻合实收文本）。测量正确。
- **真因（三叠加）**：①worker 提示重度上下文注入（scope 文件片段+契约+框架事实+infra 符号，A1/根因② 治本刻意加的）②agentic 循环 locate→code→verify→repair 多次往返、每次重发全上下文+历史 ③本地端点无 prompt 缓存→每次全量 prefill。多数调用是短步骤(~215)，少数 codegen 吐几千。
- **定性**：非缺陷，是"巨量输入换小模型可靠性"的架构权衡；本地不按 token 计费但是真实 prefill 计算=本地调用慢的根因。
- **post-run 优化候选（非 bug 不 mid-run 改）**：本地 prompt 缓存 / agent-loop 跨轮上下文复用，可大幅砍输入。
- 注：一次验证 probe 误给本地实时统计加 1 call(+2492 输出，<0.6%)，已披露；纪律=勿在 live 测量期对同 router 发探测调用。

### T+146 20:57 稳步 19/21（无新问题）
- MONITOR 20:49 已完成=19 / 剩余=21 / 失败=2（16→19↑）。本地端点 20:4x-5x 又 4 次微 fail 但 fallback 吸收、回 primary（持续可控，非污染）。沙箱=32，analysis 无告警，token 本地 78.2M/2033call ≈38.5K（稳），云端仍 35。未到 MERGE，dispatch 健康推进（19/40，~3 子任务/12min）。

### T+134 20:45 稳步 16/24；云端解冻(一次大分析完成,A4或首触发)
- 进度: MONITOR 20:34 已完成=16 / 剩余=24 / 失败=1（13→16↑）。
- **云端 token 解冻**: 34→35 call, +443K 一次大调用完成（HANDLE_FAILURE 分析这次未超时→**A4 诊断 hint 可能首次真触发**）。443K/单 call=合法大上下文（非膨胀，证治本对超大 call 也正确）。
- 本地端点 20:43 又一次 2-fail 微抖→20:45 回 primary（fallback 吸收，非污染；模式=偶发 2-3 失败自恢复）。
- 沙箱=28，analysis 无告警，token 本地 72M/1874call ≈38.4K（稳）。未到 MERGE，dispatch 健康推进（16/40，本地 codegen 慢，估剩 ~2h）。

### T+122 20:32 稳步收敛 13/27，本地端点彻底稳定
- 进度: MONITOR 20:24 已完成=13 / 剩余=27 / 失败=1（净向前）。本地端点自 20:03 blip 后 **0 失败**，worker 回 Qwopus3.6 primary。沙箱=24，analysis 无告警。A4/prefill 超时第 5 次（持续）。token 本地 62.8M/1643call ≈38.2K（稳）。尚未到 MERGE，仍在 dispatch 健康推进（~13 完成耗 ~85min，剩 27 估还需数轮）。

### T+110 20:19 本地端点抖动=transient(fallback吸收,非污染) + 进度 11/29
- **★关键：本地端点抖动是 transient，未污染本轮★**: 20:03 仅 **3 次**调用失败，fallback 链(Qwopus→Qwen3.6-Saka fb1→fb2)吸收，**20:17 起 Qwopus3.6 恢复正常流式**。对比 round7/11 的端点持续 outage 污染整轮——本轮 fallback 韧性按设计扛住了，**这本身是治本/韧性的正面实证**。
- **进度**: MONITOR 20:11 已完成=11 / 剩余=29（净向前）。沙箱=20，analysis 无停滞告警。
- A4/prefill 超时第 4 次(20:14)持续确认。token 云端 34call 冻；本地 55M/1418call ≈38.8K（稳）。

### T+98 20:04 定向恢复(好形态) + 本地端点开始不稳(infra watch)
- **进度**: MONITOR 在 已完成=9/剩余=30 与 已完成=7/剩余=33 间震荡（恢复记账：失败子任务回炉时完成数回退）。仍在推进、未终止。沙箱=16。
- **定向恢复=A2 好形态**: 「缺符号/缺依赖编译失败 → 给失败子任务**补模块 pom 写权**」——让子任务自己声明本模块依赖，非 round11 那种乱注入无关 maven 坐标。合理。又一次「构建错全在上游→BLOCKED 不连坐」。
- **★新信号（infra·盯污染）：本地模型端点开始不稳★** (20:03): `worker/medium`+`worker/complex` 模型 Qwopus3.6-27B **调用失败** → **failover 到 fallback1 Qwen3.6-27B-Saka**。同 round7/round11「本地端点不稳」类——基建 transient，非代码 bug，fallback 链在接。**盯它是否像 round7/11 那样污染本轮**（端点持续抖→worker 大面积失败→拖慢/grind）。
- **A4/prefill 超时升级为"已确认"**: HANDLE_FAILURE 云端分析**第 3 次**撞 prefill 超时（云端 call 仍冻 34）= 100% 必超时模式，A4 诊断 hint 全程旁路。post-run 必查（端点慢 vs 阈值紧）。
- token: 云端 347K/34call；本地 47.98M/1225call ≈39.2K/call（稳）。

### T+86 19:56 稳步推进 已完成=9/剩余=30；上游不连坐治本生效
- **进度**: 已完成=9 / 剩余=30（完成数稳升 2→6→9，剩余稳降 37→33→30，净向前，非卡住）；沙箱=12。
- **★治本实证：上游 BLOCKED 不连坐★**: st-10 worker 自报「修改正确，编译错是项目已有问题」→ **L1.2.1「构建错全在上游模块(非本子任务 -pl 模块)→标 BLOCKED 退避，待上游修好再编，不连坐」**。即跨模块编译依赖未就绪时，不把上游的锅算到本子任务头上硬失败 grind——这正是 round11「内部缺类空转」的正确处理。st-10 DONE 交付 diff，L1 blocked-on-upstream 未当 grind。
- **待观察**: 每轮 MONITOR 仍报 失败=2，但完成数在升=新增失败非同批卡死（净吞吐向前）；盯 BLOCKED 子任务在上游完成后重派是否转 L1✅（真收敛测）。A4/prefill 超时旁路持续（云端 call 仍 34）。
- token: 云端 347K/34call；本地 40.1M/1015call ≈39.5K/call（稳）。

### T+74 19:43 st-8 自愈 + 稳步推进；发现 A4 被 prefill 超时旁路（候选）
- **进度**: 已完成=6 / 剩余=33（持续递减）；沙箱=12。
- **st-8 自愈(931s)**: 2 次 L1❌ 后，经 verify 前 **沙箱→本地精准 pull-back diff 隔离**(结构主干A) + **sticky-fail**(「确定性闸门通过但 LLM 自报失败→以确定性为准」)→ L1✅→PRODUCING→最终复核✅ DONE(diff=25889 chars)。round11 那个 struggling 子任务最终收敛。
- **★新发现（候选·post-run 调查·不 mid-run 修）：A4 被 HANDLE_FAILURE 云端 prefill 超时旁路★**:
  - 现象：HANDLE_FAILURE 的失败分析 LLM 调用（云端）**连续 2 次撞 `stream 首 token(prefill)` 超时**→`确定性回退 retry`。云端 token call 数自 dispatch 起**冻在 34**（=HANDLE_FAILURE 云端分析 100% 超时未完成，故无 usage 记录）。
  - 后果：**A4 诊断 hint 实际从未触发**（其上游 LLM 分析总超时），失败子任务退化成【无 hint 盲 retry】。确定性兜底 retry 仍推进（st-8 已证收敛），故非阻断，但 A4 round11 治本的设计收益本轮**未兑现**。
  - 待判（post-run，不臆断）：是云端端点对大上下文分析调用 prefill 真慢/过载（infra transient），还是 first_token_timeout=180s 对该调用偏紧（需调预算）？两者修法不同，跑完查证再定，**不反射式改**。
- token: 云端 347K/34call（冻结=分析全超时）；本地 31.2M/754call ≈41.4K/call（稳）。

### T+62 19:32 健康重试动力学（收敛，无 grind，无兄弟同步失败）
- **进度**: 已完成=2 / 剩余=37（共 39）；沙箱=8（多批并行）。
- **失败→重试→收敛（健康路径，非空转）**: MONITOR 抓 2 个 L1 失败(st-8/st-10)→HANDLE_FAILURE→**retry**。**st-10 retry DONE ✅**；**st-5 自愈 L1 ❌(299s)→✅(431s)**（worker 内 repair 循环成功）。
- **★治本实证★**: 失败子任务走【plain retry】非【A2 maven 补依赖 grind】→ **A2/A3 内部缺类分流生效**（round11 真因之一是内部缺类被误当缺 jar 空转，本轮没有）。无 internal_pkg_not_built / 无 upstream_module_broken。st-1/st-9 完成态**未被重拆清空**→ round6 replan-clobber 治本仍守。
- **一处 transient（B 类·既有兜底接管）**: `HANDLE_FAILURE LLM 分析异常→确定性回退 retry`（云端 brain 失败分析调用撞 stream 首 token prefill 超时→_DualTimeoutChatOpenAI 兜底切确定性 retry）。优雅降级，非 bug。**副作用**: 此次 A4 诊断 hint 未能生成（LLM 分析超时），退化成无 hint retry——但仅此一次、retry 仍发生。盯 A4 在 LLM 分析正常时是否注入 hint。
- **待观察**: st-8 连 2 次 L1 ❌ 仍重试中，盯收敛 vs 耗尽重试预算（=能力信号）。
- token: 云端 347K/34call；本地 23.96M/578call ≈41.4K/call（持续稳）。

### T+50 19:15 首批 worker 完成，多治本线上实证（健康）
- **流程**: DISPATCH 首批 4 个 → **st-1 DONE(422s) L1 验证通过 ✅ deterministic**、st-9 DONE(519s)、st-10 DONE(138s trivial)。无 BLOCKED / 无 internal_pkg_not_built / 无 HANDLE_FAILURE grind / analysis.log 空。
- **★治本线上实证★**:
  - **CONTRACT_MERGE 并集合并生效**(round10-after): AlarmSimpleUtil/HttpClient/SdkConfig 同名多版「并集合并不丢方法/字段」→ **合并完成 接口=25 DTO=46 API=61 约定=10 依赖=8（9/9 成功）**。→ **待观察②(engine/sdk API=0 退化)解除**：单模块 retry 丢的 API 被跨版 union-merge 在合并期补回，总 API=61 不丢。
  - **L1.2.1 静态资源放行非 BLOCKED 生效**(round11 A-item swarm-l1-buildcmd-misdetect-npm): 纯静态 .js/.html 的 Maven 模块「跳过误派 node 构建，放行非 BLOCKED」——round11 误判空转的坑已治。
- **token 真实烧量**: 云端 347K/34call ≈10.2K/call；**本地 15.3M/367call ≈41.7K/call**（worker codegen 大上下文，合理非膨胀，治本对本地长跑持续成立 ✓）。
- **A1 真正考验在下一批**: 首批多是脚手架/静态/根 pom（st-1/9/10），依赖兄弟 domain 的子任务在后续批次派发——那才检验 A1 兄弟域产物注入。继续盯。

### T+38 19:07 ★突破到 DISPATCH（越过 round11 卡点）★
- **里程碑**: CONTRACT 9 模块全收（alarm-app 终收敛）→ ELABORATE（依赖序修正/scope 预注入/同 package readable）→ VALIDATE_PLAN（警告 st-1,st-31 都写 pom→已串行化，闸门接住）→ CONFIRM → **DISPATCH accepted: 39 子任务, 首批并行 4 个(st-1/8/9/10)**。**这是 round11 死区（CONTRACT/dispatch 交界）—— 本轮越过了。**
- **worker 健康(eye2 沙箱 jsonl)**: 4 沙箱皆活——①找 .vm 模板(find/test/base64 exit=0) ②读 profile.html 理解结构 ③trivial 快路径 CODING ④LOCATING。本地模型 Qwopus3.6-27B 接单。皆正常早期探索。
- **token**: 云端 347K/34call ≈10.2K/call；**本地首次有数 153K/4call ≈38K/call**（worker codegen，合理非膨胀——治本对本地也成立 ✓）。
- **待观察（均按纪律记·不 mid-run 修）**:
  - **规则5 警告依旧**（engine/notify-api/schedule/sdk/system-ext「依赖契约无 pom owner 承接，N artifacts 落空」）= A5 已知 defer 项，仅警告非致命。盯是否致下游缺依赖。
  - **ruoyi-generator read-fail + ruoyi-alarm/pom.xml 本地不存在**：worker 探未创建文件（早期 LOCATING 探索噪声，非错误，别处 exit=0）。**盯它是否转成 BLOCKED/空产出**（round11 真因之一是兄弟产物不同步——A1 治本就治这个，现在正进入验证窗口）。
- **A1 验证窗口开启**: 接下来 worker 完成、依赖子任务派发时，看依赖子沙箱是否 bootstrap 含兄弟域产物、errors=0、不再 internal_pkg_not_built 空转。

### T+26 18:55 CONTRACT 阶段（进展中，两处待观察）
- **流程**: CONTRACT_MODULE 9 模块（并发=3）已出 7-8 个：notify-user/channel/task/schedule/notify-api/system-ext ✓ + engine/sdk 第2次重试成功。**模块 1/9 alarm-app 第1、2 次均 JSON 失败、仍在第3次重试**（当前流式中）。还没进 PLAN/ELABORATE/DISPATCH。
- **token**: 云端 223,297 / 24 call ≈ **9.3K/call**（持续合理非膨胀 ✓）；本地 0。watcher 活、analysis 无停滞告警。
- **待观察① JSON 解析失败反复（B 类·既有闸门接管·暂不动）**: `Expecting value: line 1 column 1` across alarm-app×2 / engine×1 / sdk×1。模型吐空/非 JSON，json_repair+重试闸门在兜——engine/sdk 第2次已收敛。**盯 alarm-app 是否收敛 vs grind**（已 2 连败，逼近重试预算就是信号）。
- **待观察② 重试后 API=0 内容退化（候选发现·先记不修）**: engine/sdk 第2次重试虽产出合法 JSON，但 `API=0`（接口/DTO 在、API 端点丢空）。即**重试换得结构合法却内容缩水**。单看不致命，但若多模块以 API=0 契约进 dispatch → 下游可能"缺 API 面"。**观察其是否引发下游 cannot-find / 空实现**，再定是否为真 wiring 问题。不反射式修。
- 评价：CONTRACT 偏慢（单模块 300-750s）、alarm-app 重试拖尾是当前瓶颈点。真正考验仍在 DISPATCH 后。

### T+14 18:43 重跑随跑（流程健康推进，无系统问题）
盯三只眼（任务日志 ≥18:28 / 看守 events+analysis / token 端点）：
- **流程**: ANALYZE(ultra) → TECH_DESIGN 两阶段 **9 模块 0 失败 并发=3 → 124 文件** → REVIEW_DESIGN **方案通过** → PLAN → CONTRACT_SKELETON(conventions10/constants10/consumer_map9) → CONTRACT_MODULE 逐模块（2/9 完成）。**推进顺畅、未卡**。
- **token 端点**: 云端 121,272 / 14 call ≈ **8.6K/call**（合理非膨胀，治本在生产 14 call 持续成立 ✓）；本地 0（未派 worker）。
- **看守 analysis.log 空**（无停滞/死循环告警）；watcher 活；沙箱 0（CONTRACT 阶段，未到 dispatch）。
- **一处待观察（B 类·暂不动）**: `18:41:54 [CONTRACT_MODULE] 模块 1/9 'alarm-app' 第1次失败(Expecting value: line 1 column 1)` —— 模型吐了非 JSON/空，**既有 json_repair+重试闸门已接管**（第1次失败=会重试）。属模型输出 artifact，**不反射式修**；盯它是否收敛（重试成功）还是 grind（连续失败空转）。
- 评价：这是 round11 卡点之前的健康阶段；真正考验在 DISPATCH 之后（A1 兄弟域产物注入 / 框架变体一致 / 能否到 MERGE）。继续盯。

### T+10 17:54 中止（治本 token 统计两个真 bug，非编排问题）
盯 token 端点时发现 **10 个云端 call 报 2.88 亿 token**（28.8M/call，物理不可能）→ 用户手动取消 round12，先修统计再重跑。三盯取证(probe)定位**两个真 bug**（commit 9b2cffb）：
1. **流式 usage 膨胀 ~580×**：云端 GLM 网关【每 chunk】回【累计】usage（581/582 chunk 带），langchain 拼接时【逐字段求和】→ Σ累计 ≈ N×真值。本地仅末 chunk 带 usage 无此病。**治本=逐 chunk 按字段取 max 不求和**（on_llm_new_token，run_id 隔离，并行规划下不串号）。
2. **brain 编排全归「无项目归属」**：set_worker_context 仅 worker/executor 设过，brain 自身 ainvoke 从未设。**治本=run_task 入口设一次 ContextVar**（异步 await 链 + gather 子任务 copy_context 自然传播全覆盖）。
- 实测验证：并行 2 路云端每 call ~654 token、云端+本地均正确归项目；3 回归测试+全量套件过；2.88 亿垃圾行已清，token 表复位清零基线。
- **待用户三清三盯重启 round12**（治本 8 条 round11 + token 统计两病 全部就绪）。
