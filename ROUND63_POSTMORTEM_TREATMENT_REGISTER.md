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

---
## T3 调查结论（2026-07-16，死锁通道逐层实锤）

**死锁实况（postmortem 主日志）**：MONITOR 完成数 13 冻结跨 3 个 handle 周期（10:30/10:47/11:03 均
`剩余=67, 已完成=13, 失败=4`），同一批 `['st-11-4','st-7-1','st-8','st-2-1-1-2']`。其中 3 个
`pipeline_blocked=upstream_module_broken, blocked_on_modules=['ruoyi-common']`（12 次上报全同签名），
第 4 个（st-2-1-1-2）前两轮是 coding 超时（非 blocked）。HANDLE_FAILURE 走 transient 退避 1/3→2/3→3/3
（10:32/10:48/11:05），每周期 4 worker 全管线白跑 ~16min，直至 run 终止。

**自相矛盾原文实锤**：LLM 三轮诊断全对——"构建系统自动导入修复机制修改了根 pom.xml 和
ruoyi-framework/pom.xml → ruoyi-common 编译崩 → 阻塞所有依赖者"，且明写"**ruoyi-common 是预置模块，
其依赖声明不在任何子任务范围内**"（=无人能修），却给 strategy=retry，理由是"重试时**若**环境 POM
已恢复正确状态，编译应能通过"——寄望环境自愈的无据重试。

**为什么现有闸全没接住（逐层）**：
1. `worker/l1_verdict.py:305`：一切 BLOCKED 统一 `failure_class="transient"`（fail-closed 设计本意
   是交 brain 退避），worker 无 git/plan 视野，无法分辨"上游产物未就绪（真 transient）"vs"基线模块
   被永久破坏（无人会修）"。
2. `failure.py` 早段 `_INTERNAL_BLOCKED_KINDS` 拦截（702-879）三臂全空过：`_producers_of` 对基线模块
   （无子任务 scope 落在 ruoyi-common）返回空 → 非 dep_hit/prod_hit；`_blocked_pkg_unrecoverable`
   要求 blocked_pkgs 非空且**不在**工作树（ruoyi-common 包在树里、只是编译崩）→ 非 futile；
   C9 补边要求有 active 生产者 → 跳过。基线模块破坏=结构性无 owner，恰好落在三臂之间的空洞。
3. B2 失败指纹短路被混批拆台：`_all_blocked` 要求批内**全部** transient 都带 pipeline_blocked，
   `_sig_skip/_sig_exhausted` 用 `min(_blk_counts)`——st-2-1-1-2（同一死锁的**受害者**，超时非 blocked）
   把整批判据永久解除武装。批级判据被单个搭车者拆台=结构性缺口。
4. A2 终身派发熔断（6 次）最终会停，但每周期 ~16min×4 worker，代价 2h+ 才到底，且终局=abandon，
   不含修复臂。

**治本设计（与 T1/T2 构成三层防线，本条为"毒已在共享树"时的 brain 侧修复臂+死锁判决）**：
- 判据用结构不用状态（round59 铁律）：`blocked_on_modules` 含【git HEAD 基线已存在的模块】
  （`git cat-file -e HEAD:<module>`，栈中立）且 plan 无任何生产者 → 基线破坏，非 transient——
  **逐 fid 判定**，天然免疫混批拆台（缺口 3）。
- 修复臂（先于放弃）：确定性基线锚修复扫描 `sweep_baseline_anchor_poison`——对项目树内 git 基线
  已存在的共享清单逐个对照 HEAD 还原既有版本锚篡改（复用 T2 纯函数，加法放行、突变还原），
  豁免 plan 子任务 writable/create 覆盖的清单（计划授权面）。还原>0 → 毒已出树，重派失败子任务
  （输入真变了，重试有据；徒劳重试不计 capability 配额），`baseline_repair_rounds` 计数封顶
  （max_retries，防修了又被投毒的无界循环）。
- fail-loud 终局：扫描无可还原（破坏非锚投毒/HEAD 本身坏）或轮次耗尽 → 判死锁，并入既有
  `_unrecoverable` 连坐放弃通道（诚实 PARTIAL），绝不 transient 无望等待。
- worker 侧 fc=transient 保持不变（worker 无判定视野），brain 侧结构性再分类=register 处方
  "`upstream_module_broken` on 基线模块应归 plan/environment"的落地形态。

### T3 完成（2026-07-16）

**改动**：
- `brain/nodes/recovery.py`：`_module_in_git_baseline`（`git cat-file -e HEAD:<module>`，栈中立，
  异常 fail-open False + WARNING 留痕）+ `sweep_baseline_anchor_poison`（对照 HEAD 还原共享清单
  既有版本锚篡改，复用 T2 纯函数；豁免 plan 授权面；返回 `(restored, scan_errors)` 区分扫净/扫瞎；
  基线读取锁外、读-改-写在 `_ProjectGitFlock` 内与 worker pull-back 同锁）。
- `brain/nodes/failure.py`：早段 `_INTERNAL_BLOCKED_KINDS` 拦截链新增第四臂（`elif _bmods and
  not _prods` 且模块在 git 基线）——**逐 fid 判定**（免疫混批拆台）；命中后修复臂优先：还原>0 →
  重派（基线阻断者不计配额、搭车者按惯例 +1）+ `baseline_repair_rounds` 封顶 max_retries；
  扫净无可还原/轮次耗尽 → 并入 `_unrecoverable` 连坐放弃（诚实 PARTIAL）；扫瞎（scan_errors>0
  且 0 还原）→ 不判死锁不耗轮次，WARNING 回落既有阶梯。
- `brain/state.py`：`baseline_repair_rounds: int` 声明 + "monotonic" 生命周期（任务级标量，同
  `replan_count` 不受 D08 按子任务剪枝；跨 replan 保留是刻意——毒是树属性非 plan 属性）。

**对抗双复核（reviewer + silent-hunter）全治**：
- reviewer HIGH#1：修复臂 early-return 吞同批已判 `_unrecoverable`（真死上游）裁决=白跑整周期 →
  同一 return 里照常 `_transitive_abandon` 连坐放弃（镜像 _selfheal 块先例）；_selfheal 者随批
  重派（自愈推迟一轮，受修复臂轮次上限约束，注释明示权衡）。锁 `test_mixed_unrecoverable_verdict_preserved`。
- hunter#1 HIGH：扫瞎与扫净不可区分 → 误把"scanner 坏"判成"树干净"而放弃（方向性错误）→
  sweep 返回 scan_errors + 盲扫回落既有阶梯不判死锁。锁 `test_scan_blind_not_misjudged_as_deadlock`。
- hunter#2 HIGH：`_module_in_git_baseline` git 异常静默 False=T3 臂无痕解除武装 → WARNING 留痕
  （非仓库/不在 HEAD 走 returncode≠0 不刷屏）。锁 `test_module_in_git_baseline_git_error_failopen`。
- hunter#5 MEDIUM-HIGH：混批搭车者重试计数既不加也不清=无限白拿免费重试绕阶梯 → 搭车者 +1、
  仅基线阻断者豁免归零。锁 `test_straggler_retry_counter_increments`。
- reviewer LOW#2：owned 归一化 `lstrip("./")` 字符集过剥（吃 .mvn 段首点）→ 显式前缀剥离。
- 双方 cleared：拦截链 elif 序无互踩/plan 回写全路径/轮次账本免疫 D08 剪枝/并发窗口（dispatch
  gather 全收后才进 HANDLE_FAILURE + 同 flock）/worker.sandbox import 无副作用风险。

**register 三判据落地对照**：①"≥K 共享同一 blocked_on_modules + 完成数冻结"→ 用更强的结构判据
（基线模块+无生产者）在**首个周期**即拦截，不必等 2 周期冻结（round63 每周期 16min×4）；②"自诊
不在任何子任务 scope 却 retry"→ 确定性拦截先于 LLM 策略，自相矛盾通道整体旁路；③"upstream_module_broken
on 基线模块 ≠ transient"→ brain 侧结构性再分类（worker 侧 fc 保持——worker 无 git/plan 视野）。

**测试**：test_r63_deadlock_baseline_repair.py 19/19（结构判据 3 + 修复臂 6 + handle_failure 集成
5 + 复核回归锁 5）；revert-check 两层（撤 failure 接线=6 集成红；撤 recovery=ImportError）；
定向回归 12 文件 PYTEST_EXIT=0；全量套件 PYTEST_EXIT=0。

**已知边界（登记债）**：①修复臂只还原版本锚（pom 精确解析，非 pom 清单放行）——非锚形态的基线
破坏走 fail-loud 放弃而非修复，方向保守正确；②`_ProjectGitFlock` 无超时为既有行为（hunter bonus，
T3 只是新增调用方，不在本条治）；③_selfheal 与基线破坏同批时自愈推迟一轮（有界：修复臂轮次上限）。

## T4 调查结论（2026-07-16，包漂移因果链三方实锤）

**症状复核**（logs_archive/round63_postmortem + cassettes/d03e4523）：生成代码里 `AlarmRobot`
同时存在 `com.ruoyi.alarm.core.domain`(19) 与 `com.ruoyi.alarm.domain`(2)；`AlarmSendLog` 被
消费侧按 `com.ruoyi.alarm.domain` 引用（10× "package does not exist"），而 producer 真实落点
是 `engine/domain`。另 8× `.core.domain.dto 不存在` = 幻影 AlarmTaskDTO（T6 项）。

**关键翻案：plan 本身是自洽的。** cassette file_plan+scope 给每个实体唯一物理落点
（`AlarmTask/AlarmRobot/AlarmTaskChannel → core/domain`@st-6-1-1，`AlarmSendLog → engine/domain`
@st-7）。权威存在，但**从未下发**——漂移发生在 worker 首发 import 臆造，不是 plan 不一致。

**逐层缺口（file:line 实锤）**：
1. 契约符号零 FQN/路径：`contract_symbols_with_module`(brain/contract_utils.py:2044) 条目只有
   裸名+构建模块目录名；CONTRACT_MODULE schema(planning_nodes.py:1678) 从未要求包路径。worker
   拿到的契约 JSON（worker/prompts.py:176-203 D51 合成）同样无落点 → 首发 import 全靠猜，
   纠错只有编译报错后 symbol_resolver(worker/symbol_resolver.py:60,73) 的事后 best-effort
   （codegraph 恰好已索引才有效）。
2. C1 对账只看 basename：`unowned_contract_symbols`(brain/plan_validator.py:474) 语料词边界 +
   basename 惯例等价，"路径形状不影响过闸"（plan_finisher.py:131 注释自认）→ 同实体三包共存
   对 C1/C2 结构性不可见。
3. G2 自愈有触发前提：`wire_readable_provenance`(contract_utils.py:1346) 只在 consumer **已把
   producer 文件列进 readable** 时补 depends_on。st-14 语料引用 AlarmSendLog 但
   deps=['st-1']、readable 无该文件 → G2 全盲。"引用了未声明 readable 的跨子任务类型"当前
   **零覆盖**（全仓无 plan 期类型引用反查机制）。
4. **AlarmSendLog/AlarmScheduleStrategy 根本不在 shared_contract**——纯跨子任务实体。只钉契约
   符号治不了 round63 真死因；register ②"扩到任意跨子任务 create 的类型引用"是必做通道。
5. ③注入机制已存在无需新建：readable→executor_sync 上传(worker/executor_sync.py:103)→seed 闸
   fail-closed(worker/executor.py:579)。缺的只是②把文件布进 readable。

**治本设计（确定性、栈中立、plan 期，新文件 brain/symbol_provenance.py 防 god-file）**：
- ①钉落点不求 LLM：从 plan create_files/writable（code 文件、唯一落点、按 basename_symbol_match
  tier0/1 消歧、module 字段辅助消歧）给 shared_contract 条目回填 `defined_in`=权威物理路径（不占 apis 的 path=URL 键）（栈中立：
  路径→包名由 worker 按栈推导）。同符号多落点=计划内漂移 → WARNING 不钉（surfaced 不静默）。
- ②语料引用布线：任意跨子任务 create 的唯一 code stem（len≥4、噪音表外、区分大小写词边界）
  命中 consumer 语料（description+AC+contract，与 C1 同语料面）→ 把 producer 路径补进 consumer
  readable+upstream_artifacts；随后**既有 G2** 补 depends_on（复用其环守卫，不造第二套加边逻辑）。
  fan-out 超帽（通用名爆炸）→ 跳过+WARNING。契约符号通道同法（符号名→①钉住的 path）。
- ③=②布线后既有 bootstrap/seed 闸自动生效；worker prompt 契约段加一句"path 为权威落点，
  import 由 path 推导勿臆造"。
- 接线点：ELABORATE 末尾、G2 调用（planning_nodes.py:2421）之前；plan/shared_contract 变更
  回写 state（dispatch.py:528 读 state["shared_contract"] 优先，不回写=白钉）。

### T4 完成（2026-07-16，test-first + 对抗双复核 + 全量套件）

**变更**：
- `brain/symbol_provenance.py`（新文件，防 god-file）：`pin_contract_symbol_paths`（契约
  interfaces/types/dtos 条目回填 defined_in=唯一权威落点；basename_symbol_match tier0/1 消歧
  +module 辅助；多落点=漂移不钉+WARNING；tier2-only 留痕 INFO）+ `wire_created_type_references`
  （区分大小写词边界语料命中跨子任务 create 唯一 code stem / 已钉契约符号 → readable+
  upstream_artifacts 布线；歧义/fan-out 超帽/writable-only 无序各自 WARNING；噪音表被引用 DEBUG）。
- `brain/planning_nodes.py`：elaborate 里 G2 前接线（pin/wire 分开 try+exc_info，import 出 try
  fail-loud）；out["plan"] 回写条件扩 _t4_pinned/_t4_wired/**_prov_added**（顺治 G2 补边靠就地
  变异侥幸存活的旧账）；_t4_pinned>0 回写 out["shared_contract"]（dispatch.py:528 state 键优先）。
- `worker/prompts.py`：契约含 defined_in 时注入"import 由该路径推导勿臆造"指令（无则不注入）；
  D51 合成时子任务 contract 整键覆盖顶掉 defined_in → 条目浅拷贝按符号名回填+WARNING
  （绝不就地变异 state 对象）。
- `test/test_r63_t4_symbol_location_pin.py`：26 测试（含 round63 真死因 AlarmSendLog 端到端复现）。

**对抗双复核（全治+测试锁）**：hunter#1 HIGH（pin 提交后 wire 抛=半应用状态谎报"跳过"→分开
try/各自留痕/半应用 WARNING，锁 test_elaborate_wire_exception_half_applied_trace）；hunter#2
（import 出 try，模块级回归 fail-loud）；hunter#3（tier2-only/噪音表被引用留痕，锁 2 测试）；
hunter#4（exc_info=True）；hunter#5（D51 覆盖顶掉 defined_in→条目级回填，锁
test_prompt_shadowed_pin_keys_restore_defined_in）；reviewer R1 MEDIUM CONFIRMED（writable-only
落点布线后 G2 不加边=静默无序→WARNING 留痕，锁 test_wire_writable_only_pin_warns_unordered；
不擅自加边：同文件多 writer 时 producer 无良定义，写序交 normalize，L1 编译闸兜底）；R2（register
字段名笔误 path→defined_in）。

**对 register 三子项的兑现**：①钉 FQN owner=确定性 defined_in（不求 LLM 产 FQN，从 plan 落点
推导，栈中立钉路径）；②G2 扩跨子任务类型引用=语料引用→readable 布线→既有 G2 补边（AlarmSendLog
不在契约也覆盖）；③producer 文件进 consumer 沙箱=②布好 readable 后既有 executor_sync 上传+seed
闸 fail-closed 自动生效，零新机制。**已知留观**：writable-only 落点无依赖序（WARNING 可查）；
语料反向提及（"不要动 X"）会多布一条无害 readable；fan-out 帽 max(8, n/3)。
revert-check：撤接线→恰 2 集成测试红；撤模块→collection ImportError。

## T5 调查结论（2026-07-16，coherence 闸真相核实 + pom 基线依赖缺口实锤）

**核实结果：闸已实现已接线，"待接管"是文档滞后。** `validate_module_coherence`
（brain/plan_validator.py:656，G1/cc7be64）已接线 validate_plan（nodes/__init__.py:2630-2665，
杀开关 SWARM_MODULE_COHERENCE_GATE 默认开），硬判①模块散多目录②多模块塌同目录。round63
日志"Task4 模块 coherence 闸待接管归一"来自 plan_finisher 闸落地**前**的前瞻注释未回填——
本轮已改措辞。但该闸语义=物理落点 coherence，与 pom 依赖内容**正交**，挡不住 T5 真死因。

**真缺口（cassette 实锤）**：st-5 描述文字明说"一次性声明…ruoyi-common、ruoyi-framework…"，
但"权威 pom 模板（原样写入）"的 XML 里两者皆无——模板 <dependencies> 唯一来源=契约
`dependencies[].artifacts`（LLM 只报第三方 GAV，从不报内部模块；prompt schema 也没要求），
无任何机制推导"新模块引用基线模块 → pom 须声明"；plan 期无此闸，事后只有 worker 编译失败后的
L1 防线④（30+ 次"程序包 com.ruoyi.common.core.domain 不存在"烧完预算才触发）。推导证据 plan
里现成：ruoyi-alarm 子任务 readable→ruoyi-common code 文件 108 次、alarm-interface 111 次。

### T5 完成（2026-07-16，test-first + 对抗双复核 + 全量套件）

**变更**（brain/contract_utils.py + brain/plan_finisher.py 措辞 + 15 测试）：
- `derive_internal_module_deps`：模块子任务跨模块 readable **code** 文件=编译依赖证据（栈中立
  证据面）；plan 兄弟互指=双向剪+WARNING（注入必成环）；新兄弟（无 pom）=显式
  `group:名:${project.version}`；基线/既有目录经 `_baseline_module_artifact` 过滤（无 pom/
  packaging pom|war|ear/spring-boot 可执行件不可依赖，R58-1 形态依真 artifactId）。
- 单次推导（hunter#F4）传入两个注入点（scaffold 循环 + R58-3 owner 嵌入），
  `_merge_internal_deps` 按 artifactId 去重并入契约 artifacts → 既有 resolve_scaffold_artifacts
  （坐标解析单一权威不变）。

**对抗双复核（2 blocker 全治+锁）**：
- hunter#F2 **CRITICAL**（实证）：根 GAV 解析不到时新兄弟退化裸名 → maven_registry Central
  反查把**本工程新模块名**解析成不相干真实构件（org.ow2.jasmine:alarm-interface）=伪造坐标盖
  权威章（R47-2 变体）→ 治：rg 缺失不注入+响亮 WARNING（锁 test_sibling_without_root_gav_not_bare_name）。
- hunter#F1 **HIGH**（实证，round63 死型本体）：契约条目 artifacts 空（模块只需基线库）被
  unclaimed_contract_deps 剪掉 → 推导结果无注入出口、pom 无人建，INFO 却谎报"已并入"→ 治：
  补零 artifacts 脚手架条目 + owner 迭代面扩到 derived-only 模块 + INFO 措辞校准
  （锁 test_empty_artifacts_module_still_gets_scaffold_with_internal_dep）。
- hunter#F3（OSError≠不可依赖，WARNING 区分设计内过滤）、reviewer MED（_owner_mod 最长前缀，
  嵌套目录误归属）、盲区测试（R58-1 混合形态/嵌套 dirs/_merge 去重表）全补锁。
**已知留观**：孤儿模块脚手架路径（_inject_orphan_module_scaffolds resolved=[] by design，
L1 防线④兜底）不含推导依赖（hunter#F5，存量设计）；两级嵌套基线布局继承单层扫描假设（存量）；
样例引用会多注入无害 lib 依赖（上限=多一条可解析的基线库坐标）。
revert-check：撤实现→收集期 ImportError+死型集成测试红；恢复→绿。

## T6 调查结论（2026-07-16，"复入"翻案+幻影机制实锤）

**①翻案：worker 复入被剪依赖不是自作主张，是被规划期自相矛盾逼的。** cassette 实锤：st-5
验收标准[2]要求声明**含 spring-boot-starter-aop 的 20 项**（"缺一即整模块 mvn compile 失败"），
权威模板只有 19 项（aop 被 R53-1 剪）。因果：R53-1"三处同源剔除"只实现在脚手架子任务自己的
三处（contract_utils.py:1382-1426）；shared_contract.dependencies 本身从未被剪（worker D51
契约照样带 aop）；normalize 规则5 验收 note（contract_utils.py:2216）用**未解析原始 artifacts**。
dropped 无持久账本（仅 WARNING）。worker 侧防线④（l1_pipeline.py:1119-1183）按**真实
import+Central FQCN 反查**注入（受管不写版本/不受管取稳定版）——是比规划期解析更强的证据。
★register 原文"须 worker 侧禁止复入"不采纳★：禁复入会把解析器误剪变成永久缺依赖死锁；
治因=消除逼迫（同源传播）+账本负面知识，防线④保留为误剪救生索。

**②幻影 AlarmTaskDTO 机制**：在契约 dtos/apis（带完整 fields）且被 IAlarmTaskService 签名
引用，但 plan 零 create 文件、**全 plan 语料零提及**——纯契约生成期幻影。dtos 是软符号
（C1 _HARD 不含 dtos，只 warn 不闸）；契约合并从不校验签名引用类型∈dtos∪已知集；plan 期
无"契约引用类型必须有 producer"闸。worker 实现接口只能臆造包名（8×"package …core.domain.dto
does not exist"）。

### T6 完成（2026-07-16，test-first + 对抗双复核 + 全量套件）

**变更**：
- `prune_contract_dependencies`（contract_utils.py）：PLAN 期（脚手架注入前）统一 resolve，
  entry.artifacts 回写 kept（保原 spec 串），dropped 落 `pruned_artifacts` 账本+自释义 note
  （随 D51 下发 worker=负面知识）。此后模板/规则5 验收/worker 契约三面同源——st-5"验收逼
  复入"死型从根消失（锁 test_round63_st5_acceptance_no_longer_demands_pruned）。
- T5 synthesis 集合扩到 artifacts 全空模块（原生空/被剪空都保 pom 出口）。
- `_domicile_contract_symbols` 扩展：被 interfaces[].signature/apis 词边界引用的**无主 dtos**
  与硬符号同等安置成真产出文件（T4 pin 接力钉 defined_in）；孤立无引用 dto 不安置。

**对抗双复核（全治+锁）**：hunter#F1 HIGH（实证：部分 entry 变异后 except 谎报"保持原样"
——T4 同型反模式→暂存区原子提交，锁 test_prune_partial_failure_atomic）；hunter#F2
（断网=大面积 dropped 与真不可解析不可区分→占比>50%且≥3 拒剪+WARNING，锁
test_prune_mass_drop_refused_as_degraded）；hunter#F3（账本裸 dict 进 worker prompt 语义
歧义→自释义 note，锁 test_prune_ledger_note_present）；reviewer MED（跨轮破坏性别名：
契约对象 replan 复用，瞬时误剪永久不可复议→artifacts_pre_prune 快照每轮重解析+按轮重建
账本+复原撤账，锁 test_prune_transient_drop_recovers_next_round）。reviewer verified：
账本键不入 contract_symbols 白名单（C1/L2 不受污染）；prune 后同对象随 PLAN return 进
state（dispatch 读到已剪契约）；T4 pin 在 domicile 之后跑（新安置 DTO 正常钉 defined_in）。
**已知留观**：worker CODING 手写新增依赖无契约白名单闸（防线④/dep_legality 管可解析性
不管授权——调查判定授权闸弊大于利，留观）；_http_cache 进程本地，checkpoint 恢复后首轮
解析可能瞬时 miss（有 pre_prune 复议兜底）。13 测试。
