# round63 Post-Mortem 治本登记册（2026-07-16）

三路子 agent 深读（主日志 / 沙箱日志 / 实际产物）+ 陪跑两侧账综合。task d03e4523 突破 PLAN
大关（rounds39-62 做不到），执行期死于**基线投毒死锁**，卡在 13/78。以下按影响排序，**每条写码前先调查**。

## 死因链（一句话）
worker 侧确定性 **auto-import-repair / `L1.2.1 version-repair`** 为满足幻影依赖
`spring-boot-starter-aop@4.0.6`（其 Maven 仓库视图最高=3.5.16），**改写了共享 `spring-boot.version`
属性 4.0.6→3.5.16**（还谎称"项目自身版本不碰"、并插入无 version 的虚构 starter
`spring-boot-starter-webmvc`/`-aspectj`）→ 整 reactor commons-lang3 降级 → 基座 `ruoyi-common`
（用 `org.apache.commons.lang3.Strings`=3.18+ API）编译崩 → `-am` reactor 全线
`upstream_module_broken` → 毒 pom 被 pull-back 合并 → 每个后续沙箱重新中毒=永久 →
HANDLE_FAILURE 不识别死锁、把永久基线破坏判 `transient`、无谓重试 3 次 → 卡死。

## 治本任务（ordered，P0 先）

### P0 —— 死锁触发器与守卫（直接致死）
- **T1 [version-repair 禁改共享版本属性 / 禁造 artifactId]**（RC1/RC2, T-A1/A2）：auto-import-repair
  只允许从解析出的 BOM **补缺失 `<version>`**，绝不改写既有父/共享版本属性；artifactId 必须对真实
  Spring Boot BOM 校验，虚构名（webmvc/aspectj 无 version）拒绝而非插入；不可解析的跨代依赖应
  **剪除**（像 generation-mismatch 剪 39 次那样）而非降级。**调查先**：定位 L1.2.1 version-repair /
  maven 修复代码路径，确认是它写的 3.5.16，读懂现逻辑。
- **T2 [基线完整性闸 + scope 守卫覆盖 repair 编辑]**（RC2, T-A3, 沙箱§7）：①禁止把对基线/越界
  pom（root、ruoyi-framework、ruoyi-common）的编辑 pull-back 进共享树；②确定性基线完整性闸：基线
  本可编译的共享模块变不可编译=fail-loud，定位投毒 hunk、拒合/回滚。现 scope 守卫只覆盖模型
  write_file，不覆盖 repair 路径（`scope_violations:[]` 但 pom 被改）。**调查先**：pull-back/merge 与
  scope 守卫代码。
- **T3 [MONITOR/HANDLE_FAILURE 死锁探测 + 失败分类修正]**（RC3）：≥K 个子任务共享同一
  `blocked_on_modules` 且完成数跨 ≥2 handle 周期不变 → 判死锁，升级到基线修复/replan 或 fail-loud，
  **不是 retry**；修 HANDLE_FAILURE 自相矛盾（自己诊断"不在任何子任务 scope"却仍 retry）；
  `upstream_module_broken` on 基线模块应归 `plan/environment`（需修复）而非 `transient`（退避）。
  **调查先**：HANDLE_FAILURE 决策逻辑 + failure_class 分类器。

### P1 —— 规划接地/coherence（churn 之源，即便建成也 startup 崩）
- **T4 [契约钉死共享符号 FQN + provenance 闸扩到跨子任务类型引用]**（B3, 沙箱§5, 产物§2）：
  同一实体被放进 3 个包（`.core.domain`×92 / `.domain`×21 / `.engine.domain`×1），mapper 猜错包→
  MyBatis 启动 ClassNotFound。①契约为每个共享实体/DTO 钉唯一 FQN owner，消费者拿到精确 import；
  ②G2 provenance 闸从"只覆盖契约符号"扩到**任意跨子任务 create 的类型引用**（补 depends_on+readable）；
  ③bootstrap 把 producer 真实文件/桩注入 consumer 沙箱。**调查先**：G2 现覆盖范围、契约符号模型。
- **T5 [模块 coherence 闸真接线：pom 模板含基线依赖]**（RC5）：st-5 的 ruoyi-alarm/pom.xml 漏
  `ruoyi-common` → BaseEntity/@Excel 找不到（首波失败）。**调查先=先核实真相**：主日志显示
  "Task4 模块 coherence 闸待接管归一"（疑未实现/未接线），但会话 Task#4/G1 标记已完成——查提交的
  coherence 闸是否真跑到这条 pom-模板路径，还是有接线缺口/回归。
- **T6 [契约剪除 worker 侧强制 + 幻影 DTO 消解]**（RC4, 产物§2）：①R53-1 规划期剪掉的依赖
  （spring-boot-starter-aop）被 worker 在模块 pom 重新引入=灾难源头之一，须 worker 侧禁止复入；
  ②`IAlarmTaskService` 引 `...core.domain.dto.AlarmTaskDTO`——plan 从未排产该 DTO/包 → 契约引用了
  无 producer 的类型。plan 要么排产该 DTO，要么接口绑到已产出的 AlarmTask 域类。**调查先**：契约剪除
  机制、service-interface 子任务的类型引用如何生成。

### P1 —— worker 能力（纯模型，已被闸拦但浪费预算）
- **T7 [worker 复读/退化看门狗 + 模型换挡]**（RC6, T-B1/B2, 沙箱§2）：4 worker 陷 identifier
  复读（`IllegalArgumentEx`/`LinkedHash Map`/`Exceotion`），看门狗只在 900s/迭代上限才断（每个白烧
  300-600s）。①对流式 reasoning 加 n-gram 复读探测（同标识符/句 ≥3× 立即中止 turn），不靠墙钟；
  ②退化即换模型档（记忆 round56"思考失控先换模型"——查是否只接了 brain 没接 worker loop）。**调查先**：
  worker agent loop、现看门狗、round56 规则接线。
- **T8 [上游越界破坏 fail-fast]**（T-C1, 沙箱§3/§5）：`l1_2_compile_ok:True` 但
  `l1_2_1_build_ok:False` 且错在越界上游 pom → 立即 fail-fast 交 brain（"blocked by upstream pom X"），
  别烧 95 迭代重试修不了的东西。**调查先**：worker L1 fix-loop 退出条件。

### P2 —— 卫生/低影响
- **T9 [LLM 自检降级为 advisory + compile 失败即短路自评]**（RC7, T-D1/D2）：21/34 幻觉 PASS，
  闸已全拦，但自评阶段仍烧 token 产假 ✅ 清单。compile 已失败就短路自评；把确定性 build 错回喂同一 turn。
- **T10 [核实经验/skills 层是否真注入 worker]**（沙箱§7）：34 worker 零 `experience__*` 调用——
  拔插经验层可能没接进本 worker 路径。**调查先**：skills 注入 worker 的接线。
- **T11 [Task#10 录制作用域缺口]**（陪跑发现）：录制只覆盖 set_llm_node 的 plan 族节点，漏
  tech_design/contract/extract。在 brain 图节点分派层统一打标签（每节点一次），worker 流量仍排除。

## 产物级即时症状（仅记录；本树下轮丢弃，不单列治本任务）
restore spring-boot.version→4.0.6 / 修 `.domain.*` 包漂移（mapper+XML）/ 消 AlarmTaskDTO 幻影 /
去重 Druid 3+4 starter / ruoyi-framework pom 复原。leaf 码（12/12 BaseEntity、@Excel 隔离、
builder、AES util）**确是生产级、无截断**——证明小模型在**接地充分**时能产出可用码。108 计划文件仅
落地 39（36%），整条 runnable spine（service impl/controller/api/channel impl/job/sql）因死锁未产出。

## 方法学
每条 T：**调查（读代码取证）→ 定本（test-first 红→绿）→ 对抗双复核（reviewer+silent-hunter）→
revert-check → 全量套件 PYTEST_EXIT=0 → 本地提交**。离线优先（用 cassette d03e4523 / postmortem 快照做
fixture）。绝不 mid-run、绝不猜。

---
## T1 调查结论（2026-07-16，投毒代码精确定位）
**代码**：`worker/l1_pipeline.py:_attempt_maven_version_repair`（line 557-825）。两分支不对称：
- 分支②「缺 <version>」(line 761-789) **有** `_group_family_version` 代际守卫：工程 spring-boot 家族
  钉 4.0.6、artifact 在该代不存在(仓库最高=另一代) → **剪除依赖**(generation-mismatch)。★正确★
- 分支①「版本不存在→校正」(line 582-703) **无**此守卫。round63 走此路：spring-boot-starter-aop:4.0.6
  仓库查到最高 3.5.16 → `_choose_valid_version` 挑 3.5.16 → line 683-696 `rewrite_property_version`
  把 `${spring-boot.version}` 属性 4.0.6→3.5.16 = 降级整 reactor 代际。
- line 682 注释"保留属性(项目自身版本)已拒绝"——守卫只护工程自身 `<version>`(ruoyi.version)，
  **不护 spring-boot.version 这种多依赖共享/平台 BOM 锚属性** = 缺口本体。
**治本设计**：分支①复用分支②代际守卫 + 新不变量——依赖版本来自共享属性 `${x.version}` 且 bad_ver
等于工程该家族平台钉版 → 判"依赖不属本代"→ 剪除依赖，绝不改写共享属性；version-repair 只许改依赖块
内字面 version，共享/平台 BOM 锚属性一律免碰。**测试**：函数纯可单测，合成 build_output+pom fixture
红(降级/剪错)→绿(剪依赖、属性不动)。待读 `_group_family_version`/`rewrite_property_version`/
`_choose_valid_version` 定精确改法。

### T1 完成（2026-07-16，test-first + 对抗双复核 + 全量套件）
**改动**（`worker/l1_pipeline.py` + 测试 `test/test_r63_version_repair_shared_property.py`）：
1. 抽出纯判据 `_family_generation_choice(fam, available)`（单一权威，两分支共用 → 治 round57-3
   "一个不变量两处实现只有一处对"）：家族版在仓库可用→对齐；钉在 fam 但该代无此 artifact→
   `_PRUNE_DEP` 剪除；无家族先例→None 交默认。
2. 分支①「版本不存在→校正」接上代际守卫（原缺）：`_PRUNE_DEP` 时用 `even_with_version=True`
   剪除跨代依赖（毒 dep 带 `<version>${spring-boot.version}</version>`），**绝不改写共享属性**。
3. 分支②重构为共用同一判据（行为保持，66 既有测试全绿佐证）。
4. 兜底不变量 `_dep_consumers_of_property`：属性被 ≥2 依赖引用=平台/BOM 锚 → version-repair
   拒绝降级（即便家族探测不到、属性钉在中间层父 pom 也兜住）。
**对抗双复核抓到并已治的两个真缺陷**：
- ★HIGH（两复核独立实锤 + 活体复现）★ 分支①代际剪除**漏 fail-open**：仓库不可达
  (`_reachable=False`,available=[]) 时 `_family_generation_choice` 把"证据缺失"当"确证查无"→
  一次 curl 超时就误剪合法依赖（本系统最不能犯的错）。已修：`... if _reachable else None`，
  与分支②守卫对称；加 `test_unreachable_repo_never_prunes_family_dep` 锁死。
- MEDIUM（Finding 3）家族探测只扫 root pom→属性钉在中间层父 pom 时 `_fam=None` 落回旧降级路径。
  已由 #4 兜底不变量根治（不依赖家族探测），加 `test_shared_property_not_downgraded_when_family_undetected`。
**测试**：本 T1 文件 8/8 绿；R53/JVM/P1-12 guardrail 66/66 绿（分支②重构无回归）；全量套件绿。
**基线清理**：round63 run 投毒的 `e2e-projects/RuoYi/pom.xml`+`ruoyi-framework/pom.xml` 已
`git checkout` 复原到基线 0d42679（SB 4.0.6）——`test_warmup_pom_real_ruoyi` 复绿。★这暴露了
T2 的必要性：投毒能落到基线 checkout（untracked alarm-interface/ruoyi-alarm 目录仍在）。★
**栈中立**：判据全走 groupId/release-train/version-list/属性引用计数，无 Spring/Java 名硬编码；
不变量（共享版本锚不得为单依赖降级）对 npm/Gradle/Cargo 同样成立。

### T2 调查结论（2026-07-16，投毒进树的确切通道）
**通道**：pull-back 共享清单落盘走 `sandbox._merge_manifest_with_local`→`workspace_manifest.merge_shared_manifest`。
该 merge 是**加法-only 两方并集**（只并 dependency/module 条目），自己 docstring 登记了债：
"内容级'有意删除/篡改'两方合并无法与覆盖丢失区分→被并回复活，需三方基线（登记债）"。round63 的
`<spring-boot.version>4.0.6→3.5.16`（顶层 <properties> 篡改，既非 dep 块也非 module）恰落此洞→
incoming 毒值原样穿过 merge 进共享树→整 reactor 降代死锁。H2 `_rollback_failed_manifest_footprint`
只在 L1 **FAIL** 时摘贡献，而毒子任务沙箱**裁绿**（本模块编过）→ H2 不触发→毒照进。scope 守卫
（round18 P0-B）**故意**豁免 `_repaired_extra_paths`（否则合法 module-registration 改父 pom 被误杀）
→ 篡改共享锚不被 scope 抓。**结论**：缺的正是 merge 自认的"三方基线"。

### T2 完成（2026-07-16，test-first + 对抗双复核 + 全量套件）
**设计**：补上 merge 缺的第三方基线（git HEAD），在 pull-back 落盘后加**独立三方基线闸**。判据=
「基线**既有**版本锚（顶层 <properties> 叶子 + <parent><version>）当前值≠基线值」→ 篡改 → 还原基线值
（拒毒进共享树）。**只挡篡改既有锚，放行一切加法**（新属性/依赖/module 注册）→ 结构上不会冲掉并行
兄弟的合法注册（兄弟都是加法）。用结构不变量而非"基线编译→变不可编译"的昂贵全 reactor 编译闸——
确定性、离线、栈中立（版本锚在任何清单都有；实现按 pom 精确解析，其它清单原样放行未实证面）。
**改动**：
- `workspace_manifest.py` 纯判据 `restore_baseline_version_anchors(text, baseline, rel)→(新文本, 还原清单)`
  + `_toplevel_property_map`/`_toplevel_property_values`/`_parent_version`/`_restore_property_leaf`/
  `_restore_parent_version`（均纯函数、fail-open）。
- `executor_sync.py` `_enforce_baseline_anchor_integrity` 接进 `_sync_from_sandbox` 两分支（沙箱 pull-back
  + 本地模式），持 per-project flock 读-改-写，同步修正 `_post_sync_contents`（防 diff 再把毒当产出）。
- `executor.py` 初始化 `_baseline_integrity_restored`；`executor_l1gate.py` 挂进 L1 details 可查。
**对抗双复核抓到并已治的缺陷**：
- ★HIGH（code-reviewer）★ **越界误还原**：初版对【任何】pom 既有属性篡改都还原，会把子任务
  **合法拥有**的模块 pom 里的私有属性 bump（brain 授权）也误还原、静默丢交付。已治：**scope-ownership
  豁免**——清单在子任务 writable/create scope 内=授权编辑，放行；只护"非本子任务 scope"（repair 越界
  摸到基线，正是 round63 毒的签名）。加 `test_enforce_owned_pom_property_bump_not_reverted` +
  `test_enforce_out_of_scope_property_still_reverted` 成对锁死。
- ★HIGH（silent-hunter）★ **重复叶子静默解除检测**：去重 map 遇同键异值判歧义**丢弃该键**→
  盲插式毒（round47 双 version 前例）留下重复 <key> 叶子时检测被静默解除、毒漏过。已治：改**逐值扫描**
  `_toplevel_property_values`（不去重），任一值≠基线即判篡改、全部收敛基线值并标 note。加
  `test_duplicate_current_leaf_poison_still_detected`。
- MEDIUM（silent-hunter #2/#3）**写盘失败/解码失败静默跳过**：已确证篡改却还原写盘失败=毒仍在树，
  或读取解码失败=可能漏毒——都改 `level="warning"` 留声。
- MEDIUM（silent-hunter #4 + code-reviewer 锁范围）：命中/异常日志全升 `level="warning"`（观测约定）；
  git 基线读取移出 flock（不可变历史无需锁），只锁本地读-改-写，去掉阻塞并行兄弟的隐患。
- 观测（silent-hunter #6）：`_baseline_integrity_restored` 挂进 L1 details，verdict/telemetry 可查。
**测试**：本 T2 文件 13/13 绿；manifest/sync/merge/L1/round18/CRLF/T1 定向回归 130 全绿；ruff 我新增区无
新 E501/F 类（余为既有 noqa 抑制项）。
**已知边界（登记债，非本轮 blocker）**：①三方基线本身若被前序毒污染（毒已 commit 进 HEAD）则闸静默降级
——T2 明定为 git HEAD 之上的结构兜底，非防污染 HEAD；②基线属性被**删除**（非改值）暂不重插（round63 是
改值不是删）。二者留待需要时再治。
**栈中立**：判据=版本锚差异（任何清单都有的概念）；实现按 pom 精确解析，非 pom 清单原样放行（未实证篡改
面，保守），符合"通用多栈绝不写死语言"铁律。
