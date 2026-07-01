# E2E 随盯日志 · RuoYi-E2E 996 预警编排平台 · ROUND13

> **验证目标**：round13 治本（斩断 replan 无界循环 —— 不可满足子任务集单一事实源 + 传递闭包 + 单调熔断）线上确证。
> 治本 5 改动 A/B/C/D/E 已实现 + 全量 1736 passed，本轮 E2E 验【机制/类别】非固定子任务。
> **本轮关键判读**（按 [[swarm-e2e-watch-process-not-fixed-subtasks]]）：
> 1. **★循环斩断★**：上游阶梯三打桩/放弃后，依赖它的下游**一次性进 abandoned**，HANDLE_FAILURE 对该类失败**零 replan**，不再 churn `replan — upstream_module_broken`。
> 2. **能自终**：合理时间到 **PARTIAL 终态**（不再需人工取消）+ **走完任务级 MERGE**（pom 多写者并集 facet round9 最后一面线上验证）。
> 3. **诚实交付**：终态 PARTIAL 列明 stub/放弃子任务需人工补完，其余整树编译过。
> 4. **8 条旧治本无回归** + token 双端非膨胀。
> **纪律**：观察优先先分类、记本文件、不 mid-run 改码；新在上（prepend 到 T0 后）。

---

## ★★★ 终态复盘 · FAILED 4/26 · 06:28（2h27m）· 不是干净的胜利 ★★★

> **一句话**：round13 修对了它瞄准的那条 replan-clobber 无界循环（已验证），但 run 仍 **FAILED**——经一条**同根的新路径**：give_up_preserve（阶梯三）后 `MONITOR→DISPATCH` 在"34 个剩余"上**空转撞 recursion_limit 300**，**从未到 MERGE**。且全程被**主力模型端点宕**污染。**我们仍未跑出一次生产级产出。**

### 治本 5 改动验证清单（逐条诚实）
- **A（worker 结构化吐 blocked_on_packages）= ✅ LIVE**：st-9 L1 detail 确证含 `blocked_on_packages:['com.ruoyi.common.core.redis']`。
- **B（下游∈放弃集→短路连坐）= ⚠️ 触发但未被真正压测**：阶梯三时 `连坐放弃下游 0 个`——st-6/9/26 的下游不在剩余可达集 / 臆造包 `_producers_of`=∅。**"有下游"的核心场景本轮没遇到**，B 的正例未验证。
- **C（stub 连坐）= ✅ 正确执行**：阶梯三 `[('st-6','stub'),('st-9','revert'),('st-26','stub')]`，stub 5+4 文件"下游可编译需人工补完"，revert 清足迹防 reactor 中毒。
- **D（派发读放弃集）= ⚠️ 疑似放大了新 bug**：D 让"下游∈放弃集"never-ready，但 dispatch_remaining 仍列着它们 → `get_dispatch_batch`=[] 而 `after_monitor` 仍路由 DISPATCH → **空转**（见 #R13-4）。
- **E（单调熔断兜底）= 未触发**：ladder 在 ≤3 就转阶梯三，E 没机会上场；且 E 只管 HANDLE_FAILURE 不管 MONITOR→DISPATCH 空转。

### ★新根因 #R13-4（本轮最重要，治本未竟）：MONITOR→DISPATCH 无"不可推进→终态"转换★
- **机制**：`brain/graph.py:238` `after_monitor`：`if dispatch_remaining: return "dispatch"`——只要剩余非空就派发，**无"有无可派发/能否推进"判断**。`dispatch.py:267` `if not to_dispatch: return {dispatch_remaining 原样}`——一个不出活、不排空。给 build 放弃后 34 剩余全 not-ready（依赖已 revert/abandoned 的上游）→ `get_dispatch_batch`=[] → MONITOR→DISPATCH→MONITOR **紧致空转**（06:27:37 一秒 9+ 次）→ 撞 `recursion_limit 300` → **FAILED（非 PARTIAL）**。
- **定性**：这是 round12 **同一结构根**（不可满足子任务集无终态转换）的**派发面**。round13 修了 **replan 面**（HANDLE_FAILURE 不再无界 replan），但**派发面**（MONITOR→DISPATCH 对"剩余但不可派发"无终态熔断）**遗留**，且被 D 暴露成纯空转。**打地鼠警示再现：修一面、另一面浮现。**
- **治本方向（下一轮）**：`after_monitor` 增第三态——`dispatch_remaining 非空 但 get_dispatch_batch=[] 且无 worker 在飞 → MERGE/DELIVER（PARTIAL 诚实交付已完成成果）`，绝不再 DISPATCH 空转。这是 round13 replan 面治本的**派发面对偶**，应同源补齐。

### 其它确证 / 候选
- **#R13-2（真次因）**：基线确无 `com.ruoyi.common.core.redis`/RedisCache，模型 3 次 retry 始终重写同一臆造 import → L1 误判 `internal_pkg_not_built`（transient，等永不到的生产者）。**治本方向**：L1 区分"基线无此包且无任何子任务产出它"→ 判 **hard-fail**（直接进阶梯三），而非 transient-blocked 空耗 ladder。连 [[swarm-e2e-996-hallucinated-depver]]。
- **#R13-3 = 良性**：worker/medium+complex 的主力 `Qwopus3.6-27B-v2-NVFP4` 端点 `400 Model not found` → fallback 链（Saka→MiniMax）正常兜住（③override 治本 ✅）。
- **#R13-1**：L1 scope 双闸不一致（import-repair 改越界 framework pom，L1.1 豁免/Phase4 严判），首批 st-1 中招；后被 timeout 掩盖未复现，仍是隐患。
- **★污染源（必须复述）：主力模型 `Qwopus3.6-27B-v2-NVFP4` 自 ~05:32 全程宕（~56min 未恢复）★**。每子任务被迫烧满 retry 配额（3×900s）跑弱 fallback → 失败暴增 + **图步数暴涨更快撞 recursion_limit**。**本轮非公平能力测试**。

### 旧治本无回归（线上确证）
- round6 replan-clobber 防护 = ✅ **强确证**（保留成功 st-25 贯穿，对比 round12 清 34）。round9 union-merge = ✅（接口 22 并集不丢方法）。A1 兄弟域注入 / A2-A3 internal_pkg→BLOCKED 不连坐 / ELABORATE 层拆+下游重映射 / pom 多写者串行化 / 脚手架置根 / 工作单元预算闸门 = 均线上可见生效。token 双端非膨胀（cloud Δ4.74M/local Δ40.8M，2h27m，归属 RuoYi-E2E 干净）。

### 对比 round12 & 北极星
- round12=**replan 无界 churn 3h→人工 CANCELLED**（完成 39）。round13=**ladder 有界、阶梯三正确，但 give_up 后派发空转→recursion_limit→FAILED**（完成 4，2h27m **自终非人工**）。**机制进步明确**（无界 replan 被斩、能自走到阶梯三、保成功），**但仍 0 次到 MERGE、0 次生产级 PARTIAL**。completed 仅 4 严重受主力模型宕拖累。
- **诚实结论**：round13 **对其瞄准目标=成功**，但 ①暴露同根派发面新缺陷 #R13-4（治本未竟）②主力模型宕污染产品测试 ③#R13-2 臆造包误分类空耗。**不是干净胜利。**

### 下一步建议（待用户拍板，未改码）
1. **修主力模型端点** → 三清重跑，才是公平的"能否生产级"测试。
2. **#R13-4 派发面终态熔断**（after_monitor 第三态）——round13 replan 面治本的对偶，同源补齐，治"give_up 后空转撞 recursion_limit"。
3. **#R13-2** L1 对"基线无且无生产者的包"判 hard-fail 不判 transient-blocked。
4. （可选）recursion_limit 由 300 上调只是缓解非治本，真因是 #R13-4 缺终态转换。

---

## 时间线（newest-at-top）

### T+135 · 06:19 · 第3次retry批完成仍失败→阶梯三临近 + ★round13-A 结构化输出确证 LIVE★
- **第3次 retry 批（06:04→06:19）完成，st-9/st-26 仍失败**：
  - st-9：925s 超时 + `com.ruoyi.common.core.redis does not exist`（LogoutBlacklistFilter 反复 import 臆造包）。**L1 detail 确证 round13-A live**：`pipeline_blocked: internal_pkg_not_built` + `blocked_on_packages: ['com.ruoyi.common.core.redis']` + `failure_class: transient`——**改动 A 结构化吐缺包已生效** ✅。（B 短路不触发：无任何子任务产出该臆造包→`_producers_of`=∅；故走 ladder→阶梯三兜底，符合预期。）
  - st-26：`Could not find the selected project in the reactor: ruoyi-alarm-sdk`——模块未注册进 reactor（module_registration_added 尝试加 ruoyi-alarm-sdk 到 pom 但 build 仍找不到）。属 reactor 缺模块面。
  - **→ 下一 HANDLE_FAILURE（第4 cycle，_deepest>3）必跌阶梯三 give_up_preserve_build**。
- **#R13-2 确认为真驱动**：模型 3 次 retry **始终不修**臆造 import（每次重写同样的 LogoutBlacklistFilter import 不存在的 redis 包）→ 但**被 ladder 有界兜住**（≤3→阶梯三）。
- **⚠️ 主力模型仍宕**（06:18 Qwopus 400，已~46min 未恢复）。**用户未重启**（loop 继续）。同 3 子任务反复失败=弱 fallback 模型 + 模型臆造叠加。
- **观察：watcher 未自动取消**——completed=1 停滞~49min 超 30min 阈值，但 stall 检测 keyed on state-change，DISPATCHING↔HANDLE_FAILURE 每~15min 振荡→重置 no-change 计时→停滞检测失效。属看守工具盲区（非 swarm core），不影响治本判定。13 产物文件（pull-back 足迹）；18 沙箱。
- **判读**：**round13-A 确证 live、replan 有界确证、阶梯三临近**。核心治本基本验完，剩 阶梯三 escalation + 终态 PARTIAL/MERGE。但**主力模型宕全程污染产品质量**——本轮即便走通也是"弱模型 PARTIAL"，非真生产级。继续盯阶梯三 + 终态。

### T+125 · 06:06 · ★定性确证：replan守卫retry 代码层有界(≤3→阶梯三)，非round12无界★ + 主力模型仍宕
- **★核心结论：replan 守卫 retry **代码层硬有界**，round12 facet③ 隐患**未复现**★**（读码确认 `brain/nodes/__init__.py:2586`）：
  - `_deepest <= _max_retries+1`（=**≤3**）→ retry（第 N 次，第3次 forced_alternate 换备选模型）；
  - `_deepest > 3`（第4次）→ 跌入 **阶梯二（定点拆小，单失败时）→ 阶梯三 `_give_up_preserve_build`（stub/revert 保 build）→ 兜底 escalate 且【完整保留成功成果，绝不全量 replan clobber】**。
  - 当前 **第 3 次**（06:03 换备选模型）→ **下一失败 cycle 必跌入阶梯三**。这正是 round6+round13 设计：**有界、保成功、不 clobber**。**round12 的无界 full-replan-clobber 在此结构上被杜绝**（st-25 全程保留）。✅
  - ⇒ 即将到达 round13-B/E 真正验证点：st-6/st-9/st-26 进 阶梯三 stub/revert→若有下游依赖它们，看是否**一次性 abandoned 零 replan**。
- **⚠️ 但主力模型 Qwopus3.6-27B-v2-NVFP4 仍宕**（06:06 仍 400 Model not found→Saka fallback）。**这是污染源**：同 3 子任务反复失败很可能是**弱 fallback 模型**所致（非治本逻辑、非纯能力天花板）。若主力在线，st-6/9/26 或许在撞 阶梯三 前就 pass。
- **completed=1（st-25）停滞 ~36min**；18 沙箱；watcher 活、analysis 空（看守 stall 阈值 8失败/30min，completed 05:30 跳过后计时或将触发自动取消）。
- **★给用户的决策点★**：主力本地模型端点宕。选项：(a) **重启主力模型服务** → 公平测产品质量（北极星=主力 Qwopus 胜任）；(b) **任其跑** → 验证治本机制能否有界自终 PARTIAL（机制已基本确证，产品质量会因弱模型偏低）。**治本逻辑本身已读码+实测确证有界，无需为此改码。**
- **判读**：**round13 治本核心(有界 ladder + 保成功 + 不 clobber)已确证**；replan 无界病未复现。剩余是【主力模型宕的 infra 变量】+【能否走到阶梯三→MERGE→PARTIAL】。继续盯终态。

### T+115 · 05:54 · ★关键岔口：主力模型端点宕(fallback兜住) + replan守卫retry 第2次同3子任务不收敛★
- **#R13-3 定性=良性(fallback 治本生效) 但暴露 infra 退化**：
  - router fallback 链正常：`worker/complex Qwopus3.6-27B-v2 →400 Model not found→ fallback1 Qwen3.6-27B-Saka →(stall 30s)→ fallback2 MiniMax-M2.7-Pro`；worker/medium 同样 fallback 到 Saka。**[[swarm-e2e-996-hallucinated-depver]] ③override 无 fallback 治本线上确证。** ✅
  - **⚠️ 但主力本地模型 `Qwopus3.6-27B-v2-NVFP4` 自 ~05:32 起持续 `400 Model not found`**（complex+medium 双角色）——**主力模型端点宕，全程降级跑 fallback（Saka/MiniMax，能力更弱）**。这是 **infra/端点问题非 swarm 代码**，但很可能正在放大 st-6/st-9/st-26 的失败（用更弱模型重做）。**影响测试公平性，需告知用户。**（[[swarm-northstar-smallmodel]] 本应主力 Qwopus 胜任，现主力缺席）
- **⚠️ replan 守卫降级 retry 已到「第 2 次」，同 3 子任务不收敛**：
  - 第4次 HANDLE_FAILURE（05:47）：`replan 守卫生效 — 保留成功 st-25，仅重做 ['st-6','st-9','st-26']（第 2 次），不全量重规划`。st-26 加入失败集。
  - st-6（过大 timeout 拆不动）、st-9（#R13-2 redis 臆造，RedisTemplate 引导未奏效）、st-26 反复失败。
  - **★这是 round12 facet③ 的核心隐患正在浮现：「replan 守卫 第 N 次」是否有界？★** round13-E 单调熔断应兜底。**下一 tick 决定性**：若出现 第3/4/5 次无限递增且不升阶梯三 → round12 病在新触发器（模型宕致失败）下复现；若 capped 并升阶梯三/give-up-preserve → round13-E 生效。
- **completed 仍=1**（st-25）。15 沙箱；watcher 活、analysis 空。
- **判读**：**进入 round13 治本的真正压力测试**——但被「主力模型端点宕」这一 infra 变量污染（失败可能部分是弱 fallback 模型所致，非纯治本逻辑）。**下轮务必判定：(a) replan守卫retry 是否有界；(b) 主力模型是否恢复。** 若 replan 守卫无限递增→需告知用户（可能 round12 病复现 + 模型宕双重）。

### T+102 · 05:41 · ★completed 破 0=1 + round6 治本强确证(保留成功子任务)★ + 2 新信号
- **★completed=1/26★**：st-25（NotifyDispatcher）L1 通过 ✅（虽 Agent 900s 超时，但确定性闸门 compile ok→通过）。**破 0**。
- **★第3次 HANDLE_FAILURE（05:32）= round6 治本强确证★**：LLM 建议 replan，但 **「replan 守卫生效 — 保留 1 个成功子任务 ['st-25']，仅重做失败 ['st-6','st-9']（第 1 次），不全量重规划」** → retry。**对比 round12**：round12 此处会清空已完成态（清 34）；round13 **保留 st-25 成功**。round6 replan-clobber 治本**线上强确证无回归**。✅
  - bound 现状：定向恢复 1/2、replan（全量）1/2、replan守卫降级retry「第 1 次」——三条 ladder 各有计数器，均未撞顶，**仍有界**。
- **#R13-2 确认复发**：st-9 编译失败根因（LLM 自述）= 引用不存在的 `com.ruoyi.common.core.redis.RedisCache 包 (package does not exist)` + 违规改根 pom.xml。LLM 本次改进引导「改用 **RedisTemplate**（真实 Spring 类）或正确路径」——比上次「用不存在的 RedisCache」**更准**。**下轮看 st-9 是否靠 RedisTemplate 修通**（若是→#R13-2 靠 LLM 引导自愈；若仍臆造→真卡点）。
- **★新信号 #R13-3（记录待观察）：worker/medium 模型端点 400 Model not found★**：`role=worker/medium 模型=Qwopus3.6-27B-v2-NVFP4 调用失败(可能触发 fallback): Error code: 400 - {'detail':'Model not found'}`。medium 角色的本地模型端点报模型不存在。**待观察 fallback 是否兜住**（兜住=仅告警无害；兜不住=worker/medium 子任务跑不了）。连 [[swarm-e2e-996-hallucinated-depver]] ③override 模型无 fallback 治本，核实是否生效。
- **新信号：st-6 过大超时卡点**：6 文件含 AES/数据权限/Excel/Controller，`超文件上界但确定性拆不动→原样派发(交超时强制拆小)`。若确定性拆不动且每次都 900s 超时→潜在重试空转驱动（工作单元 fits-budget 不变量的边角）。下轮看 st-6 是否被超时强制拆小或持续超时。
- **token 健康非膨胀**：cloud Δ=**3.35M**（call 53）/ local Δ=**27.4M**（call 638），100min 合理。12 沙箱；watcher 活、analysis 空。
- **判读**：**核心治本(round6 保留成功 + 有界 ladder)线上确证；completed 破 0 推进中。** 真正 round13-B/E 验证点（阶梯三放弃→下游连坐）仍未到（无子任务进 abandoned）。下轮重点：st-6/st-9 修通否、各 ladder 是否撞顶升阶梯三、medium-model-404 fallback。

### T+89 · 05:29 · 后置 replan 批次在飞、前向推进（st-25 BUILD SUCCESS）
- **status=DISPATCHING 0/26**，自 05:11 replan 后**无新 HANDLE_FAILURE**，9 沙箱活跃。
- **★前向进展★**：st-25（=round12 集成缺陷的 NotifyDispatcher 子任务）`编码完成 **BUILD SUCCESS** ✅` → L1 验证 1/4；st-9 撞迭代上限 110 → L1 验证；st-6 在飞。**首批 post-replan worker 正常出活，非空转。**
- **completed=0 解释**：本批 worker 仍在执行/刚进 L1 验证（单 worker 720-848s），尚未落 完成态——属正常在途，非卡死。
- **bound 状态未变**：定向恢复 1/2、replan 1/2，均未撞顶；阶梯三/abandon 仍未触发（round13-B/E 待验）。
- **eye3**：watcher 活、analysis 空。
- **判读**：健康在途。**下轮重点：st-25/st-9 L1 是否通过→completed 破 0；若再失败是否撞 replan 2/2 顶并升阶梯三。**

### T+75 · 05:16 · 第2次 HANDLE_FAILURE=replan(1/2,有界) + ★候选issue #R13-2 臆造包★ + ⚠️0完成
- **第2次 HANDLE_FAILURE（05:10）**：st-1/st-11-1/st-12 **再次全失败**（st-1 diff=0 timeout、st-11-1 diff=0 timeout、st-12 diff=5493 仍 internal_pkg）→ 策略=**replan（第 1/2 次）** → ROUTE PLAN → 重规划 28→**26** 子任务 → 重派（st-25/st-6…）。
- **★replan 定性：有界、合法，非 round12 病★**：
  - 两条独立 ladder 各有硬上限：**定向恢复 1/2、replan 1/2**（均未撞顶）。round12 病灶=**无界** replan on downstream-of-abandoned；此处是对**真计划缺陷**的有界 replan。**round13-B/E 尚未被触发**（需先发生 阶梯三 放弃，当前还没任何子任务进 abandoned）。
  - ⚠️ **真正判定点仍在前方**：定向恢复+replan 各撞满 2 后是否正确升 阶梯三/give-up-preserve（这才是 round13 核心）。
- **★候选issue #R13-2（careful 记录，连 [[swarm-e2e-996-hallucinated-depver]]）：臆造不存在的包被误判 internal_pkg_not_built★**
  - 实证：`git ls-files` 确认 **本 RuoYi 基线无 RedisCache.java / 无 com/ruoyi/common/core/redis 目录 / 无任何 redis util**。
  - worker 写 LogoutBlacklistFilter 引 `com.ruoyi.common.core.redis`（**基线根本不存在**，模型臆造）→ L1 误判为「②跨模块未就绪 internal_pkg_not_built」标 BLOCKED 等生产者——**但无任何子任务会产出它，等不到**。
  - LLM HANDLE_FAILURE 又误导：「用项目已有的 RedisCache」——**RedisCache 同样不存在** → replan 注入的规避指引是**徒劳**，下次大概率再失败。
  - **风险**：这是真实的失败驱动器（非能力随机），可能耗掉 定向恢复/replan 的有界配额却始终修不动 st-12。**但归因须谨慎**：连 [[swarm-e2e-996-hallucinated-depver]] 的 infra 符号锚点治本——是覆盖盲区（redis util 不在锚点表）还是模型偶发臆造，待 ASSESS。**不 mid-run 改。**
- **⚠️ 黄旗：completed=0 @ 75min**。首批脚手架/早期子任务(st-1/11-1/12) fail→recover→fail 空转，下游无法推进。对比 round12 同期亦慢，但 0 完成需盯紧是否能突破。
- **#R13-1 跟踪**：st-1 第2次 diff=0（timeout_in_coding），未再触发 framework-pom scope（这次没产出）。scope 双闸问题暂未复现但因 timeout 掩盖。
- **eye2/eye3**：9 沙箱；沙箱多条 404（TokenFilter.java/RedisCache.java read 404=跨模块文件不在 workspace，部分是臆造路径本就不存在）；watcher 活、analysis 空。
- **判读**：replan 有界推进中，治本核心机制(B/E)未到验证点。**下轮重点：定向恢复/replan 配额是否撞顶并正确升阶梯三 + completed 能否破 0 + #R13-2 是否成为修不动的卡点。**

### T+62 · 05:03 · ★首个 HANDLE_FAILURE：有界定向恢复、不 clobber、无无界 replan（治本正向信号）★
- **HANDLE_FAILURE（04:54）处理 3 失败**（st-1 scope违规 / st-11-1 超时致编译错 / st-12 internal_pkg_not_built）。
- **★关键：LLM 建议 `replan`，但确定性处理器覆盖为「定向恢复（阶梯二）第 1/2 次」★**：补模块 pom 写权（st-1→ruoyi-alarm/pom、st-11-1→ruoyi-system/pom、st-12→ruoyi-framework/pom）+ 重置重试计数，**仅重派 3 失败子任务、不进 PLAN、不清完成态全表** → ROUTE retry_alternate。
  - **对比 round12**：round12 此处会无界 replan churn（守卫降级 retry 不增 replan_count、计数周期清零）。round13 现在走**有界**「第 N/2 次」定向恢复——**round6 replan-clobber 治本 + 有界阶梯二 holding，暂无无界 replan**。✅
  - **★bound 验证点★**：定向恢复硬上限 2 次 → 之后应升 阶梯三（stub/revert）或 round13-B 短路连坐。**下轮核实：若 st-1/st-12 第 2 次仍失败，是否正确升阶梯三/连坐 abandon，而非无限定向恢复。**
- **#R13-1 跟踪**：定向恢复给 st-1 补的是 `ruoyi-alarm/pom.xml` 写权，**非**它违规的 `ruoyi-framework/pom.xml`。bootstrap 已补传 framework pom（本地≠HEAD）。st-1 retry 进行中（CODING，撞 LOCATING 上限 20）。**若 import-repair 再触达 framework pom→可能再 scope 判败**（#R13-1 未根除，仅观察）。
- **token 健康非膨胀**：cloud Δ=**1.90M**（call 41）/ local Δ=**17.48M**（call 402），62min 烧量合理、归属 RuoYi-E2E 干净。
- **eye2/eye3**：6 沙箱；watcher 活、analysis 空。status API 本 tick 瞬时空（server 忙），watcher records HANDLING_FAILURE→DISPATCHING。
- **判读**：**replan 治本首个实战正向信号——有界、不 clobber。** 真正的判定在「定向恢复撞满 2 次后是否有界升阶梯三/连坐」。继续盯。

### T+49 · 04:50 · DISPATCH 起 + ★候选系统bug：scope 双闸不一致★（非 replan 焦点）
- **status=DISPATCHING 0/28**，3 沙箱活跃，worker/complex 主力 Qwopus3.6-27B-v2 并行轮转中。
- **★候选系统bug #R13-1（careful 记录，待证）：L1 scope 双闸自相矛盾★**
  - st-1（根 pom 脚手架，declared writable= pom.xml/ruoyi-admin/pom.xml/ruoyi-alarm/pom.xml）跑 665s 撞迭代上限 95。
  - **L1.2.1 确定性 import-repair 自己改了 `ruoyi-framework/pom.xml`**（补缺依赖，repaired_file_paths 含 ./ruoyi-framework/pom.xml）→ **build 通过**（`mvn -pl ruoyi-admin,ruoyi-alarm -am compile` ok）。
  - **L1.1 scope 闸**：`scope_violations=[] l1_1_scope_ok=True`（**豁免了 import-repair 的文件**）。
  - **Phase4 最终复核**（产出后 04:49:43）：`未通过 ❌ 来源=scope 违规: ['ruoyi-framework/pom.xml']` → 置信 high→low，**l1_passed=false**。
  - **定性**：子任务本身做对了（build 绿），却被【框架自己的 import-repair 改了越界文件 + Phase4 scope 闸严判同一文件】判败。**两道 scope 闸对「import-repair 触达的文件算不算越界」判定不一致**——这是系统 wiring 不一致（非模型能力），独立于 replan 循环。
  - **待观察**：HANDLE_FAILURE 如何处置 st-1？retry 是否仍每轮被 import-repair 触达同一 framework pom→同样 scope 判败→形成**新的一类重试空转**？还是放过/连坐？**下轮重点跟 st-1。暂不改码（用户纪律：不 mid-run 改）。**
- **旧治本可见生效**：st-12 `构建缺尚未建出的项目内部包(②跨模块未就绪)→标 BLOCKED 退避待生产者落地，不连坐本子任务`（A2/A3 治本生效）。
- **watcher ERR 两次（04:44/04:48）= api_status curl 瞬时失败**，随即恢复 DISPATCHING，非真问题。analysis.log 空。
- **判读**：replan 验证点临近（首批失败开始进 HANDLE_FAILURE）。继续盯 st-1 处置 + pom owner 承接 + 是否出现 replan。

### T+37 · 04:38 · 计划验证期（健康）+ 一个待观察项（pom owner 落空告警）
- **status=VALIDATING_PLAN，子任务 28**（PLAN 期 31→28 精炼）。PLANNING↔VALIDATING_PLAN 在 04:30/04:32/04:35 来回——**这是 plan-time 校验迭代，非 post-dispatch 的 replan 失败循环**，正常。
- **旧治本可见生效**：
  - **ELABORATE 层拆**：st-19（11 文件）→ 分层拆 4 批（core3+web1，Controller 锚点1，单特性java不拆穿）+ 下游依赖 st-19→st-19-4 **重映射防悬空**（[[swarm-elaborate-truncate-p6b-redecompose]] / 单实体超文件上界按层拆 治本生效）。
  - **pom 多写者串行化**：VALIDATE_PLAN 警告 4 子任务写 pom.xml [st-1,st-10,st-20,st-32] → 已串行化 + MERGE 3-way/rebase 收口（round9 facet）。
  - 脚手架置根 + SQL 实体跑最后 + 脚手架难度 trivial→MEDIUM（避单发拒答）+ scope 片段预注入。
- **⚠️ 待观察项（非确诊问题，先记录）**：`[normalize] 规则5` 对 **9 个模块**（alarm-app/channel/notify-user/schedule/task/engine/api/sdk/sys-ext）告警「依赖契约无 pom owner 承接（5-16 artifacts 落空）——请确认有脚手架子任务建 alarm-X/pom.xml」。**判读**：这是 advisory 告警，若确有脚手架子任务建各模块 pom 则无害；若无→执行期这些模块缺依赖会 build 失败。**DISPATCH 后重点验证：这些模块 pom 是否被某脚手架子任务真实创建**。暂不归因、不改码。
- **eye2/eye3**：沙箱仍 0（DISPATCH 未起）；watcher 活、analysis 空。
- **判读**：仍 pre-dispatch，replan 验证点未到。继续盯，下轮重点看 DISPATCH 起没起 + pom owner 落空是否被脚手架承接。

### T+25 · 04:26 · 契约合并完成 + 旧治本(union-merge)线上确证（无问题）
- **status→PLANNING**（ANALYZING→PLANNING 转换）；[PLAN] 拆解任务（复杂度=ultra）进行中。
- **CONTRACT_MERGE 完成**：接口=**22** DTO=39 常量=7 API=77 约定=8 模块依赖=9（**9/9 模块成功**）。
- **★旧治本 round9 union-merge facet 线上确证★**：日志多条 `同名多版 → 并集合并(不丢方法/字段)`（NotifyApiService/TwoFactorAuthService/PasswordStrategyService/AlarmTaskService 等）——印证模型仍重复吐同名接口、而 union-merge 正确取并集不丢方法（[[swarm-contract-merge-keepfirst-2026-06-29]] 的治本生效，非 keep-first 丢方法）。**8 条旧治本之一无回归。**
- **eye2/eye3**：沙箱 0（DISPATCH 未起）；watcher PID 76921 活、analysis.log 空（0 问题）。
- **判读**：规划链全程健康。**replan 验证点仍未到**（待 DISPATCH→执行→失败处理）。继续盯。

### T+13 · 04:14 · 规划期健康推进（无问题）
- **status=ANALYZING 0/0**（brain 仍在 PLAN/CONTRACT 子阶段，未到 DISPATCH，API 顶层状态正常滞后）。
- **TECH_DESIGN 完成**（04:07）：两阶段 **9 模块 / 112 文件 / 0 失败**（并发=3）；fact_issues=3 → REVIEW→PLAN **方案通过**。
- **CONTRACT 链推进中**：CONTRACT_SKELETON（conventions=8/constants=7/consumer_map=9，64.9s）→ CONTRACT_MODULE **5/9 完成**（alarm-channel 接口17/DTO5/API16、alarm-app、alarm-notify-user、alarm-schedule、alarm-task），模块 6-9 GLM-5.2 流式生成中（未 stall，单模块 50-220s）。
- **eye3**：router 健康（GLM-5.2 流式无 stall），watcher PID 76921 活、analysis.log 空（0 问题）。
- **判读**：规划质量与 round12 同档（9 模块拆分合理）。**replan 循环验证点尚未到达**——它只在 DISPATCH→HANDLE_FAILURE 期暴露，当前一切正常，继续盯。

### T0 · 04:01:34 CST · ROUND13 启动 ✅
- **三清完成**：① round12 全证据归档 `~/.swarm/round12_archive_20260701_040053/`（swarm.log 10M + 92 sandbox jsonl + task .log + watcher dir）→ 三只眼只读 round13；② API 重启 PID 65159→**75402** 加载 round13 新码（已校验 `_transitive_abandon`/`_producers_of`/`get_dispatch_batch(abandoned=)`/`get_ready_tasks(abandoned=)`/l1 `_build_blocked` 返回 set 全部 live）；③ `e2e_run.sh retry 996db614` 基线干净（HEAD `0d42679`，移除 round12 残留 4 项 ruoyi-alarm*/templates/sign，git status 空）+ plan-quality **2/2** 通过。
- **触发**：retry 996db614（auto_accept=true 无人值守），task 接受。
- **看守**：e2e_run 内 nohup 未起 → 手动 `nohup e2e_watch.sh` 重起 **PID 76921**，full.log 增量抓取中。
- **管线起步正常**：INGEST（草稿 6241 字/0 错）→ ANALYZE（复杂度 **ultra**，知识检索 struct25/sem20/norms15/mistakes5/successes5）→ DETECT_STACK **缓存命中**（指纹 871262c8，前端=Thymeleaf 服务端模板 / 后端=Spring Boot java）→ ROUTE **ANALYZE → TECH_DESIGN**。
- **token 基线（cumulative，含 round12 历史）**：cloud total **7,571,606**（call 62）/ local total **218,582,737**（call 5516）/ grand **226,154,343**。round13 增量 = 后续读数 − 此基线。
- **判读**：暂无问题。等待 TECH_DESIGN → plan 分解 → DISPATCH。**核心待观察 = 尾部子任务遇 upstream 阶梯三放弃后是否一次性连坐 abandoned（零 replan churn），对比 round12 的 ~3h 无界循环。**
