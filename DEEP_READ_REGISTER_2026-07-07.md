# 深度代码审读登记册 — 2026-07-07

方法：8 只并行 agent 逐行深读全部源码（约 4.8 万行，忽略所有 .md 与代码注释，只从代码行为推导）+ 主协调者对高危项亲核坐实。范围覆盖 brain 编排 / planning 规划 / worker 执行器 / L1+merge / 沙箱基建 / API+auth+project / infra+models+knowledge / 跨子系统契约。

亲核已坐实（一锤定音）：`'+++ /dev/null'[6:].strip()=='ev/null'`、`'st-1-10'<'st-1-2'==True`、dispatch.py:443 无 AUDIT 豁免、runner.py:363 truthiness 不下发空列表、upgrade_module_lock:251 异常路径锁泄漏、_surgical_replan_reset:727 记账表只清部分。

标注：CONFIRMED=证据链闭合；PLAUSIBLE=机制成立但触发频率/边界未完全坐实。

**修复进度（每条修完在条目末尾追加 ✅已修 行，含批次/落点/测试）**：
- ✅ P0 全清（10 批 30 条）：P0-1 D01/D23/D24/D25 · P0-2 D02 · P0-3 D03-D06 · P0-4 D07/D26 · P0-5 D08-D10 · P0-6 D11/D12/D36/D37 · P0-7 D13 · P0-8 D14/D15 · P0-9 D27-D31 · P0-10 D16/D19/D20/D21 —— 全部主循环 git diff 核对+亲自重跑测试通过，未 commit
- ✅ P1-11 D17/D18/D22（API 交付/事务/流：approve 隐式 apply 失败阻断 · SSE/WS 补 cancelled+result 并入 complete · create 单 INSERT/成员失败补偿删除）—— 主循环 git diff 核对+亲跑 20 新测+130 定向回归绿，未 commit
- ✅ P1-12 D32/D33/D34（L1 闸门跨栈对称：version-repair 块级重写限依赖块+保留属性 fail-closed · lint 五栈统一归属过滤 scope 外/无法归属降级可观测 · L3 push 失败 fail-closed=None+临时 index 钉扎 base 树口径）—— 主循环 git diff 核对+亲跑 14 新测+106 定向回归绿，未 commit
- ✅ P1-13 D35/D38-D47（正确性收尾 11 条+1 sibling：家族分类词边界/父路径分组 · 选主心跳失主 fail-closed · KB stale/failed 对账 · 调度缓存命中复核 DB · retry 走统一准入 · 幽灵清理分页 · test 路径口径统一 · add -N finally×2处 · 模板脚本 cid 空拒发 · 上传 body 上限+分块判超 · compare_digest/status 裁剪/.env 文件锁/preprocess 去写死模型名）—— 主循环 git diff 核对+亲跑 40 新测+235 定向回归绿，未 commit
- ✅ P2-14 D48-D60（深度优化批 13 条：鉴权/流/config 卸线程 · 列表分页+轻列 · plan 瘦身三处 · 契约派发时合成（plan 不再内联 N 份 shared，旧 checkpoint 幂等兼容）· tar 批量上传+回退 · L1 闸门/git 卸线程 · LLM/HTTP 客户端值键缓存 · MR 同步卸线程 · recovery 树索引 memo（阴性必新鲜确认）· 重复扫描安全子集 · BLPOP+准入 next-retry · 装饰配置删/补 SSOT/_NOTIFY_STATUSES 补两终态 · urllib 局部 opener+learn_store 守卫）—— 主循环核对+亲跑 49 新测+98 前序套件绿，未 commit
- ★ **14 批 60 条全清（2026-07-07）** → 收官复核已完成：
  - **全量回归**：2822 用例 0 失败（5 个既有 run_code 遗留 skip）。首轮暴露 4 项已治：①plan 节点写未声明 state 键 parallel_groups（LangGraph 静默丢=D10 修剪蒸发）→ brain/state.py 补声明；②test_cto_rewalk_fixes 源码守卫适配 D48 的 _require_user_async；③④两个测试次序 flake 加固（d33 caplog 直挂 handler 不赌 propagate 链；init_pg 测试 delenv 隔离 .env 灌入的 REQUIRE_PG_CHECKPOINTER）。
  - **code-reviewer**：APPROVE with fixes（0 CRITICAL/HIGH，1 MEDIUM 已治）：D15 给默认 DSN 字面量加 ?connect_timeout=10 恰是 secret_store 弱回退密钥派生种子→升级静默轮换密钥旧密文解不开。治本=_derive_key_seeds_from_db 主种子取 URI 去 query 归一形态+旧完整 URI 种子作 MultiFernet 解密回退（两代密文都解得开，未来 DSN 化妆改动不再轮换）。测试 test_secret_store_key_seed_stability.py（4 用例，test-first）。
  - **对抗 hunter**：1 CONFIRMED + 1 SUSPECTED 均已治：①（CONFIRMED，带 git apply 实证）D03 删除专路多写者盲拼 hunks 绕过冲突机制——良性双删除拼出重复 hunk 必败 apply、delete-vs-modify 把 + 行拼进 /dev/null 段且误标"组装缺陷"。治本=删除专路内分型：纯删除 hunk 按 (old_start,body) 去重合成单份；含修改写者→如实上报 delete-vs-modify MergeConflict 交冲突机制；同 range 不同 body（基线分叉）也报冲突。测试 test_merge_engine_diff_parse_p0_3.py 追加 2 用例（test-first，改前 FAIL）。②（SUSPECTED）D51 旧 checkpoint 已烤 contract 在 replan 改 shared 后遮蔽新值——无 provenance 无法区分合法 override，按 hunter 建议加防御性 info 日志（prompts.py 覆盖键漂移留痕），不改行为。
  - 复核整改后终轮全量回归：**2828 用例 0 失败 0 错误**（5 个既有 run_code 遗留 skip）——★深读战役全部收官（2026-07-07），全部未 commit，commit/tag 待用户拍板★

---

## P0 — 主流程断点 / 静默丢产物 / 致命正确性

### D01 [CONFIRMED · 三方独立命中 · 已亲核] AUDIT 子任务在 dispatch 被无条件判失败——审计意图整链路结构性死路
- 生产：`brain/nodes/audit_node.py:63,96,121` 审计通过=空 diff + l1_passed=True
- 消费：`brain/nodes/dispatch.py:443` `if not _diff_has_changes(diff) or not l1_passed` → failed_ids；`_diff_has_changes("")` 恒 False（`shared.py:564`）；全链 grep 无 intent==AUDIT 豁免
- 同根：纯删除子任务（diff 只有 `-` 行 + `+++ /dev/null`）同样无 `+` 行 → 同样被判失败
- 场景：任何 AUDIT 任务或纯删除子任务 → 必进 failed → handle_failure 反复重跑同样空 diff → 耗尽 max_retries → abandon/escalate。审计意图在本图上无成功终态。
- 附带：`WorkerOutput.audit_findings`（types.py:497）零结构化消费者，findings 只以 repr 混进 SSE
- 修复：dispatch 成功判据改为"预期变更类型"驱动而非"存在 + 行"。引入 `_subtask_produced_expected(worker_output, subtask)`：intent==AUDIT → 以 l1_passed 为准（空 diff 合法）；scope 声明纯 delete_files → diff 含 `+++ /dev/null` 或 `-` 行即算有效；否则维持现判据。audit_findings 接入 merge/交付摘要结构化消费。
- 验证：AUDIT 子任务干净扫描 → 终态 DONE；纯删除子任务 → 不进 failed_ids。加行为测试覆盖三类。
- **✅已修（P0-1，2026-07-07，未commit）**：`shared.py` 新增 `_diff_has_deletions`/`_is_pure_delete_scope`/`_subtask_produced_expected`（TaskIntent.AUDIT→以 l1_passed 为准、scope 纯删除→认删除迹象，意图驱动零语言/后缀写死，fail-closed）；`dispatch.py:443` 判据替换。测试 `test/test_d01_dispatch_expected_change.py`，主循环 git diff+重跑核对通过。

### D02 [CONFIRMED · 已亲核] plan 后升级的 ModuleLock 在异常路径泄漏——内存兜底下同项目永久死锁
- `infra/redis_client.py:234-253` upgrade_module_lock：acquire 新锁 → `release()` 旧锁(:251) → 返回新锁
- `brain/runner.py:562-574` 在 `_stream_brain_events` 内 `module_lock = upgrade_module_lock(...)`；新锁引用只在局部变量，仅正常 return(:578) 经元组传回调用方(:1096/1237/1351)
- 场景：plan 节点后任意异常（GraphRecursionError/TaskWallclockExceeded/TaskTokenLimitExceeded/TaskLockLost/节点异常）→ 调用方 module_lock 仍指向已被 release 的旧锁 → finally 的 release 是 no-op → 升级后新锁无人释放。Redis 模式锁最长 1h（TTL）；Redis 关闭走进程内 threading.Lock 兜底则永久持有至进程重启，同项目后续任务全部被拒。
- 修复：把锁生命周期移出局部变量。方案：用可变容器（如 `lock_holder = {"lock": module_lock}`）传引用，升级时原地替换；或 `_stream_brain_events` 用 try/finally 保证异常时也把当前锁经 out-param 传回。调用方 finally 释放 holder 里的最终锁。
- 验证：注入 plan 后异常 → 断言升级后的新锁被释放（Redis key 删除 / _LOCAL_LOCKS 释放）。
- **✅已修（P0-2，2026-07-07，未commit）**：`runner.py` `_stream_brain_events` 参数改 `lock_holder: dict` 可变容器，升级后原地写回 `lock_holder["lock"]`；三处调用方（run_task/resume_task/resume_planning）try 前建 holder、finally 释放 holder 内最终锁——异常路径升级锁不再泄漏。测试 `test/test_module_lock_upgrade_leak_d02.py`，核对通过。

### D03 [CONFIRMED · 已亲核] MERGE 引擎把删除文件补丁解析成伪路径 "ev/null"——删除操作整体蒸发
- `brain/merge_engine.py:158-165` `path = plus[6:].strip()` 假定 `+++ b/` 恒 6 字符；`'+++ /dev/null'[6:].strip()=='ev/null'`（已亲核）
- 链路：所有删除补丁归并到 `by_file["ev/null"]` → base_reader 为 None → 判新文件 → `_new_side_lines` 丢弃 `-` 行 → `_format_file_patch` 返回 ""(:252) → 删除消失，零日志
- 双重坐实：即便侥幸进 merged_diff，`project/diff_apply.py:332` 用 `os.path.exists` 过滤掉已删文件 → 永不 git add commit
- 修复：`_parse_file_patch` 识别 `+++ /dev/null`（及 `--- /dev/null`）为删除/新建哨兵，正确设 file_path 为 `---` 侧路径并标记删除意图；`_format_file_patch` 对删除产出 `+++ /dev/null` 头 + 全 `-` 行；diff_apply 对删除走 `git rm` 而非 add 过滤。
- 验证：单文件删除子任务经 merge → merged_diff 含正确删除段；apply 后文件不存在且被 commit。
- **✅已修（P0-3，2026-07-07，未commit）**：`merge_engine.py` 新增 `_strip_diff_path`/`_parse_git_header_paths`（识别 `/dev/null` 哨兵取 `---` 侧真路径）+ `_FilePatch.is_deletion` + `_format_deletion_patch`（产出 `+++ /dev/null` 头全 `-` 行删除段）——删除补丁不再蒸发。测试 `test/test_merge_engine_diff_parse_p0_3.py`，核对通过。

### D04 [CONFIRMED] MERGE 3-way 链式合并只折叠冲突参与者——同文件第三写者的非冲突 hunk 静默丢弃
- `brain/merge_engine.py:753` `subtask_ids = list(dict.fromkeys(h.subtask_id for h in conflict_hunks))`；`:775-789` 只链 conflict 参与者；`:913-924` resolved 后 continue 不输出其它 hunk
- 场景：文件 F 被 A/B/C 改，A/B 重叠进冲突集，C 不重叠；union 失败走 3-way，versions 含三方但 three_way 只链 A/B → merged_text 不含 C → merged_diff 缺 C 的产物，无冲突无日志。L2 编译可能仍过 → 全绿交付缺内容。
- 修复：3-way 分支的输出必须覆盖该文件全部 hunk。resolved_text 计算后，把非冲突写者（C）的 hunk 在 resolved 基础上再做一次 union/apply；或先对全部写者做 3-way 链而非只冲突子集。
- 验证：构造 A/B 冲突 + C 非冲突同文件 → 断言 merged_diff 含 C 的改动。
- **✅已修（P0-3，2026-07-07，未commit）**：`_try_three_way_resolve` 改为折叠该文件**全部** hunk（all_hunks）而非仅冲突参与者——第三写者非冲突 hunk 不再丢。测试同 `test_merge_engine_diff_parse_p0_3.py`，核对通过。

### D05 [CONFIRMED] MERGE hunk 体内 `--- ` 开头的删除行被当文件边界，截断 hunk 并伪造文件段
- `brain/merge_engine.py:116-118` `_split_raw_diffs` 边界判定不看下一行；`:172` `_parse_file_patch` 同样在任意 `--- ` 行终止 hunk
- 场景：删除一行以 `-- ` 开头的内容（SQL/Lua/Haskell 注释 `-- comment` → diff 行 `--- comment`）→ 文件段被从 hunk 中间切开，后半丢弃或挂 unknown；`_recount_hunk_header` 按截断 body 重算头 → well-formed 但内容错。RuoYi 有大量 .sql 脚本。
- 对照：`project/diff_apply.py:229-238` `split_diff_by_file` 已对同问题防护（要求下一行是 `+++`），merge_engine 两处无此防护。
- 修复：merge_engine 的两处边界判定同步 diff_apply 的"下一行须为 `+++ `"守卫。
- 验证：含 `--- comment` 删除行的 diff 经 merge → hunk 完整。
- **✅已修（P0-3，2026-07-07，未commit）**：`_split_raw_diffs` 与 `_parse_file_patch` 两处边界判定补"下一行须为 `+++ `"守卫（对齐 diff_apply 既有防护）。测试同 `test_merge_engine_diff_parse_p0_3.py`，核对通过。

### D06 [CONFIRMED] MERGE 静默丢弃 rename 与二进制补丁
- `brain/merge_engine.py:137-198` `_parse_file_patch` 只识别 `---/+++/@@/diff --git`；纯 rename 段（无 hunk）→ 返回 None 丢弃；rename+edit → 按新路径当新文件重建被截断且旧路径不删；`GIT binary patch` → 返回 None 丢弃
- 对照：`project/diff_apply.py:52-60` `files_from_unified_diff` 认识 rename——scope 层认识、merge 层不认识
- 修复：`_parse_file_patch` 识别 `rename from/to` 与 `GIT binary patch` 段并正确传递（rename 产出删旧+建新，二进制走 `git apply --binary` 旁路，pull-back 侧 `git diff` 加 `--binary`）。
- 验证：rename 子任务 + 二进制变更子任务经 merge → 产物不丢。
- **✅已修（P0-3，2026-07-07，未commit）**：`_parse_file_patch` 识别 rename/`GIT binary patch` 段标记为 `passthrough`，原文透传进 merged_diff 并按段去重，不再静默丢弃。测试同 `test_merge_engine_diff_parse_p0_3.py`，核对通过。

### D07 [CONFIRMED · 已亲核] task_records.merge_conflicts 只写不清——恢复后干净合并仍被 /apply-diff 永久 409
- brain 内侧已修：merge 节点每轮清 state `out["merge_conflicts"]=[]`（nodes/__init__.py:1648）
- 回写漏：`brain/runner.py:363-365` `if merge_conflicts:` truthiness，空列表永不下发（store.py:781 用 `is not None` 本支持清空，但 runner 从不给机会）；`_handle_post_run` 走同一函数同病；`retry_task`（:1682）重置多字段但不含 merge_conflicts/abandoned_subtasks
- 消费：`api/routers/task.py:727-742` `merge_conflicts` 非空即 409
- 场景：第 1 轮冲突入库 → 恢复 → 第 2 轮干净合并（state 清 DB 没清）→ DONE 但 /apply-diff 永远 409；重跑继承旧冲突
- 修复：`_sync_task_from_state` 对 merge_conflicts 改 `is not None` 下发（含空列表清空）；retry_task 重置补 merge_conflicts=[]、abandoned_subtasks=0。
- 验证：模拟冲突轮→干净轮 → DB merge_conflicts 被清空，apply-diff 放行。
- **✅已修（P0-4，2026-07-07，未commit）**：`runner.py` `_sync_task_from_state` 对 merge_conflicts 改 `is not None` 下发（空列表可清库）；`retry_task` 重置补 `merge_conflicts=[]`、`abandoned_subtasks=0`。测试 `test/test_runner_delivery_accounting_d07_d26.py`，核对通过。

### D08 [CONFIRMED · 已亲核] replan 记账表只清 4 张中的部分——id 复用致新子任务被旧账饿死/误弃
- `brain/nodes/__init__.py:727-735` `_surgical_replan_reset` 返回键含 subtask_results/dispatch_remaining/failed_subtask_ids/targeted_recovery_counts/failure_escalated，缺 `subtask_retry_counts`、`subtask_redecompose_count`、`abandoned_subtask_ids`、`give_up_isolated_ids`；plan() 两条 return（:782,:998）也不清
- replan 后 id 复用是默认情形（merge_subtask_batches 顺序重编 st-N）
- 场景：陈旧 subtask_retry_counts → 新 st-3 首败即 `_next>max_retries` 跳过重试落 escalate（failure.py:543）；陈旧 redecompose_count>=1 → 阶梯二对新子任务永拒（planning_core.py:287）；陈旧 abandoned/give_up id 命中新子任务 → get_dispatch_batch 排除 → 永不派发 → after_monitor 判"全不可派发"提前 MERGE 假 PARTIAL
- 修复：`_surgical_replan_reset` 补清这四张表（按签名保留纪律同 targeted_recovery_counts：签名一致才继承，否则清）。
- 验证：replan 后新 st-3 有完整重试预算、可被阶梯二拆小、不被旧 abandoned 排除。
- **✅已修（P0-5，2026-07-07，未commit）**：`_surgical_replan_reset` 补清 `subtask_retry_counts`/`subtask_redecompose_count`/`abandoned_subtask_ids`/`give_up_isolated_ids` 四张表（新增 `_sig_unchanged` 按签名保留纪律：签名一致才继承旧账，两条 return 路径同步）。测试 `test/test_p0_5_replan_accounting_d08_d10.py`，核对通过。

### D09 [CONFIRMED] VALIDATE→PLAN 重试是盲重试——校验失败原因从不回灌 PLAN
- `brain/nodes/__init__.py:810-820` plan() 只读 `replan_feedback`；validate_plan 的结构性/P6b issues 只写 `plan_validation_issues`(:1085,:1180) 无处回灌 replan_feedback；`brain/graph.py:184` after_validate 失败 → increment_retry → plan，LLM 看不到原因
- 场景：LLM 产结构坏计划 → 校验失败 → 盲重生成大概率同样坏 → 烧光 MAX_PLAN_RETRY=3 → confirm → auto 下 REJECT fail-fast 任务终止。P6b 补齐缺功能同理盲补。
- 修复：validate_plan 失败时把 issues 摘要写入 `replan_feedback`（或新增 `plan_validation_feedback` 并在 plan() prompt 注入），让重试携带具体失败原因。
- 验证：校验失败后 plan prompt 含上轮 issues 文本。
- **✅已修（P0-5，2026-07-07，未commit）**：validate_plan 失败时经 `_format_validation_feedback` 把 issues 摘要写入新 state 键 `plan_validation_feedback`（已在 `state.py:88` BrainState 声明——LangGraph 未声明键会被静默丢弃），plan() prompt 注入该反馈。测试同 `test_p0_5_replan_accounting_d08_d10.py`，核对通过。

### D10 [CONFIRMED] 确定性去重删子任务后不同步 parallel_groups——校验硬失败叠加盲重试成死循环
- `brain/nodes/__init__.py:877-882` 单发路径对 subtasks 跑 dedupe_subtasks 但 parallel_groups 原样进 TaskPlan；`contract_utils.py:1186` dedupe_module_scaffolds 同样重建 subtasks 不动 groups
- `brain/plan_validator.py:104-115` groups 引用被删 id → 硬失败
- 场景：LLM 吐重复 create 签名子任务 + parallel_groups + 未触发串行化 → 确定性去重 → 校验失败 → 叠加 D09 盲重试 → 死循环至 confirm/REJECT
- 修复：任何重建 subtasks 的路径（dedupe_subtasks/dedupe_module_scaffolds）同步从 parallel_groups 剔除已删 id、清空成员为空的组。
- 验证：去重删子任务后 parallel_groups 无悬空引用，校验通过。
- **✅已修（P0-5，2026-07-07，未commit）**：`plan_batch.py` 新增 `prune_parallel_groups(groups, valid_ids)`（剔悬空 id+清空组）；plan() 去重路径与 `contract_utils.dedupe_module_scaffolds` 重建 subtasks 后同步调用。测试同 `test_p0_5_replan_accounting_d08_d10.py`，核对通过。

### D11 [CONFIRMED] worker bootstrap 上传失败被宽 except 吞掉——在缺文件沙箱上空跑
- `worker/executor_sync.py:678-685` 上传失败抛 TransientInfraError（意图 transient 退避）；但 `worker/executor.py:474` 调用点被外层 `except Exception`(:487) 捕获 → 非 has_source 时走 :511 "降级本地"，而 set_sandbox_context 已在 :469 调用、self._sandbox 非 None → agent/L1/sync 全打到 bootstrap 不完整沙箱
- 场景：scope 30 文件传成 20（envd 间歇 5xx 未达熔断 5）→ agent 对缺文件树空跑 → 空 diff → 被判 capability 失败换模型（正是要防的反模式）
- 对称缺口：pull-back 侧有 fail-closed 闸门（A3），上传侧没有
- 修复：`_phase_prepare` 对 TransientInfraError 不吞、向上传播为 transient；或上传后加 fail-closed 完整性校验（已传文件数 == scope 应传数），缺失即 BLOCKED transient 而非降级。
- 验证：注入部分上传失败 → 任务归 transient 重试，不在缺文件沙箱执行。
- **✅已修（P0-6，2026-07-07，未commit）**：`executor.py` `_phase_prepare` 在宽 `except Exception` 之前插入 `except TransientInfraError: raise`——bootstrap 上传/seed 预检的瞬时基础设施失败向上传播归类 transient 退避重试同模型，不再被吞成"降级本地"在缺文件沙箱空跑（与 pull-back 侧 A3 fail-closed 对称）。测试 `test/test_p0_6_worker_sync_d11_d12_d36_d37.py`，主循环核对通过。

### D12 [CONFIRMED] Phase-4 闸门 None(BLOCKED) 时丢弃已坐实的确定性 PASS——整份完成工作被作废重做
- `worker/l1_verdict.py:268-290` 分支③ `det_ok is None` 时只处理 `prior.passed is False`，`prior.passed is True` 落到 verification_not_run → passed=False
- None 入口多：worker 预算耗尽（executor_l1gate.py:157）、_get_git_diff 异常(:166)、pipeline 异常(:247)、pull-back skip/err(:232)
- 场景：验证循环确定性通过 → Phase-4 produce 后超预算 → l1_passed=False + timeout marker → brain `_TIMEOUT_OVERSIZE_MARKERS`(planning_core.py:344) 当 oversize 拆小重跑。diff 已回传但整工作作废。
- 修复：分支③ `prior.passed is True` 时保留 PASS（det_ok=None 是"本轮没重新确定"而非"否定"，不应翻转已坐实结论）。
- 验证：确定性 PASS 后 Phase-4 超预算 → verdict 维持 passed=True。
- **✅已修（P0-6，2026-07-07，未commit）**：`l1_verdict.py` `evaluate_l1` 分支③补 `prior.passed is True` → 维持 passed=True（`l1_decision_source=verification_not_run_keep_prior_pass`），与 prior-False 分支对称：det=None 一律维持 prior 已坐实结论不翻盘。无 prior 时仍 fail-closed（回归测试保住）。测试同上，核对通过。

### D13 [CONFIRMED] Qdrant point ID 不含 project_id——跨项目同路径 chunk 互相覆盖删除
- `knowledge/semantic_index.py:44-61` `make_point_id = uuid5(file_path:start_line)`，project_id 不参与；全项目共用单集合 swarm_kb；写入点 semantic_index.py:315 + preprocess.py:1324
- 场景：项目 A/B 存在同相对路径同起始行 chunk（pom.xml:1、README:1、同脚手架 src 路径在多项目部署几乎必然）→ B 索引 upsert 同 ID → payload.project_id 被替换为 B → A 的该 chunk 从 A 检索静默消失且 A 侧 prune/delete（带 project_id 过滤）无法回收。直接命中"内网多用户多项目单进程"目标拓扑。
- 修复：ID key 加 project_id `uuid5(f"{project_id}:{file_path}:{start_line}")`，一次全量重索引收敛旧点。
- 验证：两项目同路径 chunk 各自独立可检索、互不覆盖。
- **✅已修（P0-7，2026-07-07，未commit）**：`make_point_id` 签名加 project_id（key=`uuid5(project_id:file_path:start_line)`，content 仍不参与保 A-P1-19 两路径去重）；空/缺 project_id → warning+ValueError fail-closed（`index_chunks`/`_store_vectors_qdrant` 双入口对称守卫，绝不静默退回旧口径混写）。全仓核实：生成侧仅 semantic_index.py:361 + preprocess.py:1334 两处均同步；删除/回收侧（prune_file_stale/delete_by_file/delete_by_project/reconcile_orphan_points/preprocess 末尾 prune）全按 payload 过滤不按 ID 反推——旧口径孤儿点随现有 reindex/prune 确定性收敛，无需一次性脚本。测试 `test/test_p0_7_qdrant_point_id_d13.py`（9 项，fake Qdrant）+ 更新 `test_ctodebt_batch_ef.py`，主循环核对+重跑 73 项 knowledge 回归全绿。

### D14 [CONFIRMED] Redis 客户端零超时 + renew 内联 brain 事件循环——Redis 慢挂起卡死整个进程
- `infra/redis_client.py:78` from_url 无 socket_timeout/socket_connect_timeout（默认 None=无限等）；所有 acquire/renew/release/rpush/lpop 同步阻塞
- `brain/runner.py:461` module_lock.renew() 在每个 LangGraph 事件同步调用（同函数 store.update_task 已卸线程池，renew 留循环内）
- 场景：Redis 网络黑洞（丢包/挂起，非 refused）→ r.eval 或冷却重探 ping 无限阻塞 → brain 事件循环整体停摆，所有任务/SSE/API 一起挂。A-P1-13 冷却重探只治 refused 快失败。
- 修复：`from_url(..., socket_connect_timeout=2, socket_timeout=3)`；renew 卸线程池或降频（非每事件）。
- 验证：模拟 Redis 挂起 → 事件循环不冻结，超时快失败。
- **✅已修（P0-8，2026-07-07，未commit）**：`redis_client.py` from_url 加 socket_connect_timeout=2s/socket_timeout=3s（env `SWARM_REDIS_SOCKET_*_TIMEOUT_SEC` 可调，非法/≤0 回退安全默认，配置无法回到无限等）；新增 `RenewPacer`（间隔=TTL/10 默认 360s，`SWARM_LOCK_RENEW_INTERVAL_SEC` 可覆盖，换锁重置计时不补 renew）；`runner.py` renew 点改 `pacer.due() and not await asyncio.to_thread(renew)`——降频+卸线程池，renew False→TaskLockLost fail-fast 语义原样保留。测试 `test/test_p0_8_infra_timeouts_d14_d15.py`，主循环核对+重跑 82 项回归全绿。

### D15 [CONFIRMED] 知识层全部直连 psycopg 无 connect_timeout + 无 fut.result timeout——PG 黑洞时 KB loop 永久卡死、worker 线程无限阻塞
- 直连无 timeout：`knowledge/structure_index.py:134`、`behavior_store.py:95`、`norms_store.py:73`、`updater.py:268`、`infra/coordination.py:82`；默认 DSN 不含 connect_timeout（settings.py:84）
- 放大：`knowledge/service.py:101-117` get_retriever 在锁内 connect_all()→挂起则锁永不释放后续检索全排队；`service.py:97` `_run_on_kb_loop` 的 `fut.result()` 无 timeout（tools/knowledge_tools.py:54、executor_prompts.py:173 worker 线程调用方全无限等）
- 场景：PG 端口丢包 → 首次检索起 KB loop 永久挂 + 每个调检索工具的 worker 线程泄漏阻塞
- 修复：所有直连补 connect_timeout；默认 DSN 加 connect_timeout；`_run_on_kb_loop` 的 fut.result 加 timeout；get_retriever 的 connect_all 移出锁或加超时。
- 验证：模拟 PG 挂起 → 检索超时快失败，锁与线程不泄漏。
- **✅已修（P0-8，2026-07-07，未commit）**：`infra/db.py` 新增单一取值点 `pg_connect_timeout_kwargs()`（复用 `SWARM_DB_CONNECT_TIMEOUT` 默认 10s）；settings 默认 DSN 追加 `?connect_timeout=10`；全仓 grep 捞齐直连点——登记册 5 处 + 漏列的 memory/store.py、knowledge/consistency.py、infra/checkpoint_gc.py、api/app.py 健康检查×3、六个启动建表点全部补齐（scripts/init_db.py 交互式 CLI 核对后不改）。`knowledge/service.py` `_run_on_kb_loop` fut.result 加 300s 上限（`SWARM_KB_SYNC_TIMEOUT_SEC`，超时 fut.cancel 不留僵尸+抛明确 TimeoutError）；`get_retriever` connect_all 改 asyncio.wait_for 60s（锁内挂起有界），顺手修半初始化 bug（连成功才发布单例，失败可重试）；`knowledge_tools.py` 捕 TimeoutError 显式失败文本。测试同上，核对通过。

---

## P1 — 正确性 / 竞态 / 事务边界 / 多用户隔离

### D16 [CONFIRMED] 跨用户项目劫持：create_project path 冲突走 ON CONFLICT DO UPDATE 无成员校验
- `project/store.py:277-300` + `api/routers/project.py:141-158`：任何持 project:create 者提交已存在 path → 静默 UPDATE 受害项目 name/description、merge config（含 sandbox_template）、返回完整项目行，无成员资格校验
- 连带：路由随后 set_project_member 用本地新 uuid 而非返回的 project["id"](:151) → 成员行挂在不存在项目（永久孤儿）；后台 preprocess 对不存在项目跑 → FK violation 仅日志。非恶意重复添加同路径也会静默改写既有项目且创建者拿不到访问权
- 修复：create_project path 冲突改为"存在即校验调用者成员资格，非成员拒绝/返回冲突错误"，禁止静默 UPDATE 他人项目；set_project_member 用 `project["id"]`；preprocess 用真实 project id。
- 验证：用户 B 提交 A 的 path → 拒绝或需授权，A 项目不被改。
- **✅已修（P0-10，2026-07-07，未commit）**：store 层 `ON CONFLICT (path) DO NOTHING` + 抛 `ProjectPathConflictError(existing)`（P1-23 冲突合并语义废止）；路由 `_caller_may_reuse_existing_project`（admin/成员幂等复用不改写，其他人 409 且不泄露既存项目字段，成员查询失败 fail-closed）；成员行/preprocess 一律用 `project["id"]`。测试 `test/test_p0_10_multiuser_isolation_d16_d19_d20_d21.py`（旧 `test_create_project_conflict.py` 已改钉新语义）。

### D17 [CONFIRMED] approve 隐式 apply 失败被吞——任务 DONE 但 diff 未落盘
- `api/routers/task.py:656-681` `should_apply = apply_diff_flag or (not sandbox_first and merged_diff)`；隐式 apply（非 sandbox_first）git apply 失败时，422/回滚只在 apply_diff_flag 为真触发(:669) → 失败被无视，resume("accept") 照常 DONE，工作区无变更，唯一线索是响应体 apply_diff.ok=false
- 修复：apply 失败（无论显式/隐式）一律阻断 accept 推进，返回错误或置任务为待人工。
- 验证：注入 git apply 失败 → 任务不 DONE。
- **✅已修（P1-11，2026-07-07，未commit）**：approve 的 422/回滚判定去掉 `apply_diff_flag` 前置条件——apply 失败无论显式/隐式一律 422 + 回滚认领状态（任务留在原审核态可重试），不触发 resume（假 DONE 窗口关闭）；正常 apply 成功路径不受影响。测试 `test/test_p1_11_api_delivery_txn_stream_d17_d18_d22.py`（注入 apply 失败 → 422 + 不 resume + 状态回滚，显式/隐式/成功三态全钉）。

### D18 [CONFIRMED] cancel 后 SSE/WS 流永不终止 + result 事件死协议
- cancel 只 emit `step:"cancelled"`（runner.py:1113/1252/1614），SSE/WS break 集合仅 complete/error/awaiting_review（task.py:277/352）→ cancelled 被归 progress 后永久挂起每 30s 心跳
- `step:"result"` 终态载荷永不可达：complete 先于 result 发布（runner.py:809/932），SSE/WS 在 complete 即 break → 携 merged_diff/l3 的 result 事件送不到，task.py:265/339 的 result 映射是死代码
- 修复：break 集合加 "cancelled"；调整发布顺序让 result 先于 complete，或把终态载荷并入 complete 事件。
- 验证：cancel 任务 → 订阅端收到终止；result 载荷可达。
- **✅已修（P1-11，2026-07-07，未commit）**：①SSE/WS break 集合对称补 "cancelled"（cancelled 事件送达后立即终止流，不再永久心跳挂起）；②result 死协议选【终态载荷并入 complete 事件】——依据消费者取证：CLI（cli/__init__.py:203-211）原生消费 complete 事件内的 result 键、WebUI（tasks.js）从未经 SSE 收到过 result（今日即死代码）且靠 complete 后 REST 重载详情，故并入 complete 对下游零破坏、载荷真正可达；runner 三处终态 emit（正常 DONE/PARTIAL + governor 抢救 PARTIAL）并入 result 后删独立 `step:"result"` 发布，task.py SSE/WS 的 result 映射死代码同步删除。测试 `test/test_p1_11_api_delivery_txn_stream_d17_d18_d22.py`（cancelled 哨兵法证终止 + complete 携 result 载荷 + 无独立 result 事件；`test_tb_token_partial_salvage.py` 改钉新协议）。

### D19 [CONFIRMED] 改密不吊销 token——被盗 token 改密后仍有效
- `api/routers/auth.py:205-233` + `auth/store.py:156-163` update_user_password 只改 password_hash，不触 token_hash/token_revoked
- 修复：改密时轮换/吊销现有 token。
- 验证：改密后旧 token 认证失败。
- **✅已修（P0-10，2026-07-07，未commit）**：`update_user_password` 同一 UPDATE 原子置 `token_revoked=true`（复用 P0-SEC-01 机制，覆盖本人改密/管理员重置/bootstrap 全调用方）；change-password 端点改密后 `rotate_user_token` 铸新 token（与登录 F1 同语义）经响应体+HttpOnly Cookie 下发，本会话续用、被盗 token 即死。

### D20 [CONFIRMED] 并发预处理竞态：无 in-flight 守卫，Qdrant 代际互删
- `api/routers/project.py:233-271` trigger_preprocess 无"已 PREPROCESSING"检查，reset+spawn 无条件；两次触发 → 两个 preprocess 并发：kb_symbol_index delete-then-insert 交错，Qdrant write-then-prune 按 index_generation 清"非本代际"→后完成方删光先完成方刚写的向量（preprocess.py:1347）
- 超时路径：wait_for 取消后 to_thread 线程继续跑继续写 upsert_progress → 项目已 ERROR 后 phase 又被写回
- 修复：trigger 加 in-flight 守卫（DB 状态 CAS 或分布式锁），已在跑则拒绝/排队；超时后设取消标志让 to_thread 阶段自查退出。
- 验证：并发触发 → 只有一个执行，KB 无非确定性丢失。
- **✅已修（P0-10，2026-07-07，未commit）**：store 新增 `claim_preprocess_slot`（单事务 CAS：非 PREPROCESSING 或 updated_at 超【总超时+10min】stale 窗口才认领 + 同事务重置进度行，EPQ 下并发双触发只有一个赢）；trigger 端点认领失败 409，create 路由同守卫堵"创建后立刻手动 trigger"窗口；preprocess 超时/异常置 per-project threading.Event（入口捕获对象持有，防注册表清理后误判），`_store_vectors_qdrant` 顶部/批边界/prune 前自查退出且取消后不写进度、不 prune（防部分写入删旧代际）。

### D21 [CONFIRMED] 级联删除漏表 + 上传文件无 GC
- `project/store.py:462-516` delete_project 级联不含 swarm_project_members → 成员行永久残留进 list_user_project_ids 白名单；`workspace/uploads/<batch>/` 与 task_records.uploaded_files 全仓无删除路径 → 无限累积
- 修复：级联删除补 swarm_project_members；delete_task/delete_project 清理对应 uploads 目录；加启动/周期 GC 扫孤儿上传。
- 验证：删项目后成员行清零、上传文件清理。
- **✅已修（P0-10，2026-07-07，未commit）**：级联补 `swarm_project_members` + 全表核对顺手补 `kb_mr_history`（其余含 project_id 表均已在册/task_audit_log 故意保留）；`delete_task`/`delete_project` 删行后清 uploads 文件（`is_within_root` 防穿越、仍被其它任务引用则跳过、只 rmdir 空批次目录）；`gc_orphan_upload_batches`（无引用+超龄 SWARM_UPLOADS_GC_DAYS=7 才删，双前缀引用复核宁留不误删）挂进既有每日 `_run_kb_prune_once` 调度。

### D22 [CONFIRMED] 创建链路部分写入无回滚
- `api/routers/task.py:162-190` create_task 与 update_task 两条 autocommit，第二条失败 → SUBMITTED 行残留且未入调度队列，长稳进程永久卡死至重启 reconcile；`project.py:141-158` create_project 与 set_project_member 非原子 → 孤儿项目
- 修复：两步并入单事务，或第二步失败时补偿删除第一步；或 create_task 后立即入队再补字段。
- 验证：注入第二步失败 → 无残留孤儿。
- **✅已修（P1-11，2026-07-07，未commit）**：①task 侧选【两步并单事务的最简形态=单条 INSERT】——store `create_task` 增可选 status/thread_id/auto_accept/queue_priority（None=DDL 默认，唯一调用方是路由，向后兼容），路由把初始状态+执行 meta 随 create 一次原子落库并删除第二条 update_task（两步窗口不复存在；优于补偿删除[补偿自身可失败且 delete_task 会连带清用户 uploads]和先入队后补字段[meta 未持久化窗口仍在]）；②project 侧 set_project_member 移出 create try 块，失败则补偿删除刚建项目（不留创建者不可见的孤儿），补偿删除失败/未生效均 error 日志留痕（项目 id 可追溯），两态均对外 500；D16/P0-10 的冲突语义与 claim_preprocess_slot 逻辑未动。测试 `test/test_p1_11_api_delivery_txn_stream_d17_d18_d22.py`（真 PG 单 INSERT 全 meta 落库 + 路由无第二条 UPDATE + pooled 态 + 成员失败补偿删除/补偿失败可观测/成功路径不误伤）。

### D23 [CONFIRMED] 失败结果滞留 subtask_results 被当"已完成"满足下游依赖——下游提前派发
- `brain/nodes/dispatch.py:287` `completed_ids = set(subtask_results.keys())` + `types.py:387` `_is_ready` 只查 completed_ids 不查 l1_passed
- 场景：_redecompose_timeout_subtasks（planning_core.py:431）把不可拆超时失败留在 failed_subtask_ids 且不 pop subtask_results → 下轮 dispatch 该 id 在 completed_ids → 依赖它的下游判"依赖满足"提前开跑（上游从未成功）→ BLOCKED/编译失败白烧
- 修复：completed_ids 只计 l1_passed 为真的结果（消费单一事实源 shared.py:44）；依赖闸门 _is_ready 校验 L1 通过。
- 验证：上游 L1 未过 → 下游不 ready。
- **✅已修（P0-1，2026-07-07，未commit）**：`shared.py` 新增 `completed_l1_ids(subtask_results)`（只计 `l1_passed()` 为真，消费单一事实源）；`dispatch.py:288` 与 `graph.py` after_monitor 熔断改用该口径。测试 `test/test_d23_dep_gate_l1.py`，核对通过。

### D24 [CONFIRMED] 契约失败 retry 分支空转 no-op——不重派不清结果，第二轮误归因全量重跑
- `brain/nodes/failure.py:161-197` 契约分支只自增计数返回 failed_subtask_ids，既不 pop subtask_results 也不加回 dispatch_remaining（其它 retry 分支都做）；dispatch 早退分支(:323) 不回填 failed_subtask_ids（违反 always-emit 契约）→ monitor 读残留 failed 再进 handle_failure，此时 verification_failure 已清 None → 走常规能力阶梯对 L1 全过的输出误诊断 pop 全部重派
- 修复：契约分支 pop 相关 subtask_results 并加回 dispatch_remaining；dispatch 早退分支始终回填 failed_subtask_ids（空也回填）。
- 验证：契约失败 → 只重做相关子任务，不全量重跑。
- **✅已修（P0-1，2026-07-07，未commit）**：`failure.py` 契约 retry 分支补 pop 相关 subtask_results + 加回 dispatch_remaining + 返回 `failed_subtask_ids=[]`；`dispatch.py` 早退分支恒回填 failed_subtask_ids（守 always-emit 契约）。测试 `test/test_d24_contract_retry_requeue.py`，核对通过。

### D25 [CONFIRMED] 悬空依赖经 #R13-4 熔断进 MERGE 假 DONE——终态账本不看 dispatch_remaining
- `brain/graph.py:241-265` after_monitor 熔断→merge（纯路由不写 state）；`brain/gates.py:24-45` partial_delivery_ids 只并 abandoned∪give_up∪rebase_dropped；runner.py:782 终态同口径
- 场景：滞留源于悬空依赖（阶梯二/预算闸门 _remap_dependents_to_terminals 遗漏、rebase/replan 后 dep 指向不存在 id，这些变异在 validate_plan 之后不再校验）→ abandoned/give_up 全空 → 已完成部分过 L2 → 终态 DONE，N 个从未执行子任务静默吞掉，还被 LEARN_SUCCESS 学成成功
- 修复：终态判定与 can_auto_accept_delivery 检查 dispatch_remaining 非空 → 非空则至少 PARTIAL；after_monitor 熔断前把滞留 remaining 显式并入 partial/未交付集。
- 验证：悬空依赖滞留 → 终态 PARTIAL 非 DONE。
- **✅已修（P0-1，2026-07-07，未commit）**：`gates.py` `partial_delivery_ids` 并集补 `dispatch_remaining`——悬空依赖滞留的未执行子任务计入未交付集，终态至少 PARTIAL 不再假 DONE。测试 `test/test_d25_terminal_remaining_partial.py`，核对通过。

### D26 [CONFIRMED] _sync_task_from_state 喂节点增量 output 当全量 state——三本账中途系统性错
- `brain/runner.py:489` on_chain_end 把节点 output（增量）传给按全量 state 写的 _sync_task_from_state
- (a) abandoned 周期清零：dispatch output 恒含 subtask_results 不含 abandoned_subtask_ids → runner.py:344 每次 dispatch 算 _ab=0 写库，handle_failure 刚放弃的 N 个下个 dispatch 归零
- (b) completed 无 plan 过滤：dispatch/merge output 无 plan → _plan_subtask_ids None → 数全部累积结果（含 replan 后旧 id）且夹紧失效 → completed > subtask_count
- 修复：_sync_task_from_state 从全量 snapshot.values 取账（abandoned/completed/plan），而非节点增量 output；或在 runner 层合并增量到全量后再 sync。
- 验证：中途 DB abandoned/completed 与 state 一致。
- **✅已修（P0-4，2026-07-07，未commit）**：`runner.py` `on_chain_end` 维护 `_accumulated_state`（DB plan 播种 + 逐事件 `.update(output)` 合并增量），`_sync_task_from_state` 改喂全量累积快照——abandoned 不再被 dispatch 周期清零、completed 有 plan 过滤与夹紧。测试同 `test_runner_delivery_accounting_d07_d26.py`，核对通过。

### D27 [CONFIRMED] worker clean_workspace 删掉 Rust/Go/npm 工具链与缓存——任务在坏沙箱烧预算
- `worker/sandbox.py:736` cache_dirs 含 `.cargo go/pkg .npm node_modules` → `rm -rf $HOME/$d`；而 Dockerfile.rust 把 rustup/cargo 装进 /root/.cargo、Dockerfile.go 用 /root/go/pkg/mod、$HOME=/root；sandbox.py:733 明确特赦 .m2 却漏了这几个
- 场景：rust 沙箱经池归还/取用清理一次 → cargo/rustc 可执行被删 → 后续 cargo 127；健康探针 echo ok 照过 → 坏沙箱烧完预算。go/node 退化全量重下
- 修复：cache_dirs 移除工具链安装目录（.cargo/bin、go 本体），只清可再生的下载缓存子目录（.cargo/registry cache、go/pkg/mod/cache 可选保留），与 .m2 特赦对称。
- 验证：清理后 cargo/rustc/go 仍可执行。
- **✅已修（P0-9，2026-07-07，未commit）**：`sandbox.py` 新增两张通用清单——`SANDBOX_PRESERVE_HOME_DIRS`（绝不删：.cargo/.rustup/go/.m2/.gradle/.npm/.config，判据=工具链安装/配置+warmup 烤入资产，经 cube-templates Dockerfile 逐栈核实）与 `SANDBOX_CLEANABLE_HOME_DIRS`（可清：.cache/__pycache__/.pytest_cache/.mypy_cache/node_modules，判据=运行期再生派生缓存）；clean_workspace 由可清清单派生并防御性过滤（可清项落入保护清单即剔除）。各栈对称，无单语言补丁。测试 `test/test_p0_9_sandbox_infra_d27_d31.py`+修正 `test_a2_isolation_cleanup.py`（原断言 .npm 被清=旧 bug 行为），主循环核对通过。

### D28 [CONFIRMED] 沙箱远端生命周期锚定创建时刻从不续期——池龄大的沙箱必被中途拆卸
- `worker/sandbox.py:609-620` create(timeout=max(max_execution_time,120)) 默认 900s 起算，全仓无续期；`sandbox_pool.py:52` pool_ttl_seconds=600<900 允许借出创建后 599s 的沙箱，剩余远端寿命 ~300s 而子任务预算 900s
- 场景：复用沙箱构建中途被远端拆卸 → run_command 连环 5xx → 5 次熔断 SandboxUnhealthyError 子任务失败（即"900s 拆卸断流"）
- 修复：加远端续期（周期 set_timeout/keepalive）；或池借出前校验剩余寿命 >= 子任务预算，不足则不复用新建。
- 验证：长跑子任务不因沙箱到期中断。
- **✅已修（P0-9，2026-07-07，未commit）**：组合方案②+①——manager 记 `_sandbox_deadlines`（create 请求发起时刻+timeout，保守偏早）+ 新增 `remaining_lifetime`/`try_extend_lifetime`（e2b SDK set_timeout 实存，成功才刷新 deadline，不臆造成功）；pool acquire 健康探针后加 `_ensure_remote_lifetime`（预算与 executor 同源 max(max_execution_time,120)）：先试续期→失败校验剩余寿命→不足 kill+新建；manager 无寿命记账（旧版/mock/跨进程）→放行不误杀。测试同上，核对通过。风险留痕：若服务端不支持 set_timeout 则池复用退化为常新建（fail-safe，看 [D28] 日志确认）。

### D29 [CONFIRMED] 沙箱池桶键=请求 template_id 但 create 每次自愈重解析——桶内镜像异质/配错语言
- `worker/sandbox_pool.py:169,222,265` 记账用调用方 template_id；实际镜像由 sandbox.py:619 _resolve_template 在 create 时决定不回写池；sandbox.py:404 配置模板不在 READY 时 `pick=ready[0]` 任选
- 场景：java 模板被回收 → create 落到任意 READY（可能 python）→ echo 探活过、桶键仍 java → 后续 java 子任务复用到无 JDK 镜像 mvn 127
- 修复：池桶键用 _resolve_template 的实际解析结果（真实镜像 id）而非请求 template_id；解析结果回写池条目。
- 验证：模板漂移时不复用错语言镜像。
- **✅已修（P0-9，2026-07-07，未commit）**：manager create 回写 `_resolved_templates[sid]`（_resolve_template 实际结果）+ 新增 `get_resolved_template`；pool `_create_and_return` 桶键改用实际解析镜像（`actual_tpl or requested`，漂移记 [D29] warning），acquire 复用借出改 `setdefault` 不再用请求值改写实际记账——漂移镜像挂真实桶，java 请求匹配不上→fail-closed 新建。测试同上，核对通过。

### D30 [CONFIRMED] pull-back >1MiB 文件确定性 skip 但 L1 当 transient BLOCKED 重试——确定性活锁
- `worker/sandbox.py:86` MAX_SYNC_FILE_SIZE=1MiB + `:1242` 超限 skipped++ 无 error；`executor_l1gate.py:232` skipped>0 判 None(BLOCKED) 走 transient 退避
- 场景：合法产出 >1MiB writable 文件（package-lock.json 常超 1MiB、生成的 SQL/数据文件）→ 每次重试同 skip → 永远 BLOCKED 至配额耗尽，误归因 transient；上传侧同上限该文件也进不了沙箱
- 修复：提高/移除大文件上限（分块传输），或 skip 大文件时区分"确定性 skip"不当 transient（记为完成而非 BLOCKED）。
- 验证：>1MiB 产物文件正确同步或不造成无限重试。
- **✅已修（P0-9，2026-07-07，未commit）**：上限 1MiB→8MiB 默认（`SWARM_SANDBOX_MAX_SYNC_FILE_SIZE` 可配，坏值回退；依据=envd 端点 60s 超时下 8MiB 秒级、base64 兜底膨胀后仍在限内、覆盖常见 package-lock），上传/下载共用同一常量两侧对称；超限 skip 单独记 `skipped_oversize`/`oversize_rels` 不再混 transient；l1gate 新分支：pipeline 绿但有超限 skip→**确定性 FAIL**（reason=pullback_oversize_deterministic_skip，走失败阶梯），不 BLOCKED 活锁也不静默丢产物；transient skip/err 仍走原 BLOCKED（A3 语义保留有回归测试）。测试同上，核对通过。

### D31 [CONFIRMED] L2 沙箱验证用 run_code（Jupyter 端点）打自建语言镜像必 502 且把 infra 失败当测试失败
- `brain/nodes/__init__.py:1842-1863` _run_l2_in_sandbox 用 manager.run_code 跑 apply/test，apply_result.error→return False；sandbox.py:929 自述语言镜像无 kernel run_code 502 是常态；对比 _run_reactor_build_in_sandbox(:1903) 已改 run_command 区分 ran/ok，_run_l2_in_sandbox 是漏改旧路径；create 不传 template(:1821) 落 default
- 修复：_run_l2_in_sandbox 改 run_command，区分 infra 失败（不判测试失败）与真测试失败；create 传正确 template。
- 验证：语言镜像上 L2 验证不因 502 误判失败。
- **✅已修（P0-9，2026-07-07，未commit）**：`_run_l2_in_sandbox` 对齐 reactor 版——patch 经 envd 文件端点写 /tmp，apply/test 全走 run_command+退出码 marker 口径，run_code 一次不打；返回契约改 `bool|None`：命令没跑成（infra）→None 走 verify.py 既有降级链（本地 L2→LLM）不误判测试失败，rc≠0→False 真失败；create 与 reactor 同款传 project_id 项目匹配镜像。只动该函数体（god-file 纪律）。测试同上，核对通过。

### D32 [CONFIRMED] Maven version-repair 的 sed 重写同版本字符串的无关标签（含项目自身版本）
- `worker/l1_pipeline.py:277-294` 候选 pom = 声明该 artifactId ∪ 含该版本字符串任何 pom；对每个候选所有含 version 行做 `s#>{bad}<#>{good}<#g`
- 场景：模型给新依赖顺手写项目自身版本号（RuoYi 3.8.7）→ 坐标不存在触发修复 → 根/各模块 pom 的 `<version>`（项目版本、parent 版本）全被改成第三方"最近有效版本"→ reactor 内部依赖解析崩，损坏经 repaired_file_paths 持久化进真值树
- 修复：只在声明该 artifactId 的 `<dependency>` 块内替换版本，不碰 project/parent version。
- 验证：修复第三方依赖版本不影响项目自身版本标签。
- **✅已修（P1-12，2026-07-07，未commit）**：sed 全局串替换改为纯函数块级重写 `rewrite_dependency_version`——候选只取声明该 artifactId 的 pom，替换只发生在该 `<dependency>` 块内（含 dependencyManagement 嵌套块）；版本经 `${prop}` 属性引用 → 只校正该属性定义标签（`rewrite_property_version`），Maven 保留属性（project.*/parent.*/revision/sha1/changelist）fail-closed 拒绝；读写走沙箱优先通道（cat + base64 管道回写）。跨栈自查：npm/cargo/go（sibling_dep_repair）均为结构化/锚点注入无同类越界，其余 sed/perl 站点是单文件标识符/import 前缀改写非版本字面量类。测试 test_p1_12_l1l3_gates_d32_d33_d34.py::test_d32_version_repair_spares_project_own_version（改前 FAIL 复现根 pom 项目版本被连坐改写）+ 属性间接层回归护栏 + 纯函数契约/保留属性用例。

### D33 [CONFIRMED] lint 闸门整树运行且无归属判定——go vet/clippy 把存量与兄弟问题连坐硬阻断
- `worker/l1_pipeline.py:1850-1899` `go vet ./...`/`cargo clippy -- -D warnings` 整工程；`:2674` lint error 硬阻断；build 闸门有 upstream/internal/infra 归属阶梯(:2537)，lint 一条没有
- 场景：沙箱树任何兄弟坏代码或基线存量 warning（clippy -D warnings 下几乎必有）→ 本子任务 lint 硬 FAIL → capability 误判换模型；Rust 项目等于所有子任务永久 lint 死锁
- 修复：lint 闸门加归属过滤（只对本子任务 scope 内文件的 lint 问题阻断），或对整树 lint 降级为告警不阻断。
- 验证：兄弟/存量 lint 问题不阻断本子任务。
- **✅已修（P1-12，2026-07-07，未commit）**：闸门处新增跨栈统一归属划分 `_split_lint_errors_by_scope`（归一路径+双向后缀匹配，五栈 linter 的 issue.file 统一消费非五份复制）——只有 error 归属本子任务改动文件才硬阻断；scope 外（兄弟/存量）与无法归属（配置错/输出异常）降级 warning 日志+details 记录（error_issues_out_of_scope/unattributed），可观测不静默；clippy 人类输出补 `-->` 定位行回填 file（否则 Rust 归属永远无路可依）。测试同文件 test_d33_sibling_lint_error_does_not_block（改前 FAIL：兄弟 go 文件 error 硬阻断）+ 本文件 error 仍阻断（含绝对路径归属）+ 无法归属降级可观测 + clippy 回填用例。

### D34 [CONFIRMED] L3 push 失败即 fail-open——在无 diff 的默认分支跑 pipeline 当 L3 通过
- `brain/nodes/verify.py:210-229` push_merged_diff_branch 失败仅 warning，ref 保持默认(main)→ pipeline(main) 本来就绿 → l3_passed=True 未测任何变更；且 push 失败是常态：l3_gitlab.py:126 在工作树 checkout -B + apply_git_diff，pull-back 材化的 untracked 新文件不被 checkout 清 → create 补丁 already exists apply 必败（round29 _apply_check_against_base 同类基线分叉 L3 未同步修）
- 修复：push 失败必须 fail-closed（l3 视为未通过或跳过而非通过）；apply 走 base 树口径同 round29 修复。
- 验证：push 失败 → l3_passed 非 True。
- **✅已修（P1-12，2026-07-07，未commit）**：两半齐修——①verify.py push 失败/项目路径不可得 → fail-closed 直接返回 l3_passed=None+l3_skipped（infra 按"未执行"上报，不假绿也不伪装 False 误触发 HANDLE_FAILURE，语义与 gates/graph 三态一致），绝不回退默认 ref 跑 pipeline；②push_merged_diff_branch 改 round29 同口径 git 底层管道：临时 index read-tree 钉扎 base（新增 base_commit 参数，verify 传 state.base_commit 与 diff 生成基线同源）→ apply --cached --ignore-whitespace → write-tree/commit-tree → push --force（scratch 分支覆盖语义），工作树/真 index 零改动，pull-back 材化的 untracked 文件不再撞 already exists；越界预检 diff_paths_escape_root 补回。测试同文件 test_d34_verify_l3_push_failure_fail_closed（改前 FAIL：push 失败仍在 main 上跑 pipeline 判 l3_passed=True）+ untracked 材化文件下 push 成功且工作树零污染 + 钉扎 base 为父 + 越界拒绝。

### D35 [PLAUSIBLE] 家族拆分 upstream/leaf/downstream 分类 fail-closed 不成立
- `brain/planning_nodes.py:2352-2366` 家族目录外 core 文件凡不命中消费者后缀白名单一律归 upstream 首批先建；真消费者若叫 NotifyManager/NotifyScheduler/NotifyJob → 先于 leaf 编译 cannot find symbol；`_is_upstream_shared` startswith("Base") 误判 BaseballXxx；`_detect_parallel_impls` 按目录 basename 分组把 a/impl 与 b/impl 并成一家族
- 修复：消费者识别改为基于依赖/引用分析而非命名后缀白名单；basename 分组带父路径区分。
- 验证：非白名单命名的消费者被正确归 downstream。
- **✅已修（P1-13，2026-07-07，未commit，两半坐实修复+一半取证后如实保留）**：①坐实并修 Base/Abstract 前缀误判——新 `_has_camel_word_prefix` 按 CamelCase 词边界匹配（BaseballScoreService/Abstraction/Basement 不再误判共享抽象，BaseballXxx 恢复参与 leaf 家族）；②坐实并修 basename 并组——`_detect_parallel_impls` 分组键改【完整父路径】（a/impl 与 b/impl 不再并成 6-leaf 假家族），目录信号仍看最后一段，孤儿 `_parent_dir` 已删；③白名单半取证结论：**"改依赖/引用分析"在此 seam 不可实现**——该函数消费的是【规划期待创建文件的路径】（greenfield fan-out 是主场景，round18 st-16 即新建文件，无内容可做引用分析）；且"未知 extra 一律 fail-closed 归 downstream"经推演**不更安全**：家族目录外的未知 core 更常见形态是共享类型（DTO/消息类，如 NotifyMessage，非 Base/I 命名不被 `_is_upstream_shared` 拦截），归 downstream 会让【全部 leaf】编不过（爆炸半径 = 整个家族），归 upstream 误置消费者只炸上游一批——upstream 默认是较小爆炸半径侧，"消费者归 downstream 安全"判据只对【命中消费者后缀名】的文件成立（该路径既有代码已覆盖）。遵守铁律不新增命名白名单，残留风险（白名单外命名的真消费者归 upstream 先建→cannot find symbol→走失败阶梯，非静默）如实记录。测试 test/test_p1_13_d35_d40_d41_brain.py 4 用例（改前 3 FAIL：Base 词边界/Baseball leaf/跨父路径并组；单目录家族回归护栏改前即绿）。

### D36 [CONFIRMED] worker 与 brain 上传/回传清单不对称：改既有 readable/兄弟文件不回传
- 上传集（executor_sync.py:85）含 readable∪manifests∪整模块源码；回传集(:690) 只有 writable∪create∪repaired∪同目录新建；worker 经 run_command(sed) 改 readable/兄弟文件让沙箱编译过 → L1 沙箱裁绿 → 不回传不进 diff → 集成期 cannot find symbol
- 修复：回传枚举覆盖沙箱内所有被修改的 tracked 文件（沙箱 git diff 或 mtime 对比），而非仅 writable/create 白名单。
- 验证：worker 改 readable 文件 → 变更回传。
- **✅已修（P0-6，2026-07-07，未commit）**：bootstrap 上传后在沙箱用其**自身时钟** touch 标记文件（`_touch_bootstrap_marker`，规避时钟偏移），pull-back 时 `find -newer` 圈出被改文件（`_list_sandbox_modified_files`，纯 mtime 栈无关、不依赖沙箱 .git），与【上下文集】(readable∪整模块源码−writable/create，`_context_sibling_rels`) 求交后并入 `_repaired_extra_paths`→回传+进 diff+scope 放行；集合外改动不静默纳入（仍交 scope 闸门）。标记创建失败降级 no-op 不阻断主链。测试同上，核对通过。

### D37 [CONFIRMED] find head-200 + size-2000k 静默截断产物枚举
- `worker/executor_sync.py:869-874` `find|head -200` 且 ≥2MB 被 -size 滤，无 error/skip 信号；(a) allow_any/greenfield pull-back(:712) >200 文件第 201+ 静默丢；(b) H-exec1 未声明新文件补捞(:734) 烤源沙箱 /workspace 数千文件 find 前 200 轮不到新建 → 假绿门补丁近似随机失效
- 修复：去掉 head 上限（或大幅提高并对截断记 warning）；新文件补捞用精确路径查询而非全树 find 前 N。
- 验证：>200 产物文件不丢；烤源沙箱新文件可靠补捞。
- **✅已修（P0-6，2026-07-07，未commit）**：(a) `_list_sandbox_workspace_files` 上限 200→`SWARM_WORKSPACE_LIST_CAP`（默认 5000，钳 [200,100000]），`head -{cap+1}` 探测截断、达上限即 WARN 可观测；(b) H-exec1 补捞改 `_list_sandbox_files_under`（只在声明文件父目录 `-maxdepth 1` 精确枚举，shlex.quote 防注入），规模与全仓无关、烤源沙箱可靠命中同包新建。测试同上，核对通过。

### D38 [CONFIRMED] 选主只启动时做一次不校验存活——多副本永久双 leader
- `infra/coordination.py:85-87` try_acquire_leadership 对 key in _held 直接早退 True 不看 _conn.closed（is_held 修了早退没修）；`api/app.py:1060` 抢主启动 5 调度器即 return，此后永不验主无心跳
- 场景：PG 重启/闪断 → advisory lock 服务端释放 → 副本 B 抢主启全部调度器，A 仍在跑无路径发现失主 → 双消费（仅多副本部署触发）
- 修复：leadership 加周期续期心跳 + 失主检测（校验 _conn 存活），失主则停调度器。
- 验证：模拟 PG 重启 → 旧 leader 让位不双跑。
- **✅已修（P1-13，2026-07-07，未commit）**：三处齐修——①coordination.try_acquire_leadership 早退补连接存活前提（`key in _held and conn 非 closed` 才 True；连接断落 _ensure_conn 清 _held+重连+新会话真实重抢锁，与 is_held 口径对齐）；②新增 `verify_leadership`（基类默认退化 is_held；PG 实现带 `SELECT 1` 探活——半开连接（对象自称 open 但服务端已断）判失主、清标记、弃死连接）+ SchedulerLeadership.still_leader；③app._run_schedulers_with_leadership 启动调度器后**不再 return**：进入 leader 心跳看门狗（间隔 env SWARM_LEADER_HEARTBEAT_SEC 默认 30s 钳 [0.05,3600]，非法回退默认），失主/心跳异常均 fail-closed → logger.critical + `_stop_leader_schedulers`（task/KB 走干净停止面 stop_task_scheduler/shutdown_kb_scheduler，decay/consistency/kb-prune 用启动前后 _APP_BG_TASKS 快照差集精确 cancel）→ 回候选循环重新竞选（单进程 PG 闪断自愈：重连重抢重启，闪断窗口任务留队列）；无协调后端（单机降级）保持原"恒 leader 即 return"零回归。测试 test/test_p1_13_d38_d42_d43_d44_infra_worker.py 6 用例（改前 5 FAIL：早退谎报/verify 不存在×3/看门狗永不停；conn 存活早退护栏改前即绿），test_a1_scheduler_leadership 回归绿。

### D39 [CONFIRMED] kb_update_events 卡死 processing/failed 无恢复——知识增量静默丢失
- `knowledge/updater.py:716-731` 出队即置 processing，进程崩溃后永停 processing，全仓无"stale processing 重置 pending"对账；failed 无重试；auto_reprocess_hours 默认 0 关、consistency 默认 repair=False
- 修复：startup 把 processing 且 created_at 超阈值的行重置 pending（事件幂等重放安全）；failed 行加有界重试。
- 验证：重启后 stale processing 被重置并重放。
- **✅已修（P1-13，2026-07-07，未commit）**：DDL 幂等加列 `retry_count`/`claimed_at`（claim 时落 claimed_at——staleness 按【处理时长】判而非入队龄，积压久的队列不误伤刚被认领的行）；新 `reconcile_stuck_events`：①processing 且 COALESCE(claimed_at,created_at) 超阈值、额度未尽 → 重置 pending + retry_count+1（有界，防毒事件反复崩进程的无限循环）；②同上额度耗尽 → 显式转 failed + error_message（可观测终态不空转）；③failed 额度未尽且上次处置超阈值 → 有界重试回 pending，耗尽保持 failed。经 `_maybe_reconcile_stuck`（60s 节流，首调必跑=startup 对账）挂在 process_pending_events 头部（调度器每 5s 轮询自然触发，无需改 app 接线），对账异常不阻断正常消费。阈值/上限走 env：SWARM_KB_STALE_PROCESSING_SEC（默认 300 钳 [1,86400]）/SWARM_KB_FAILED_MAX_RETRIES（默认 3 钳 [0,100]），非法值回退默认。测试 test/test_p1_13_d39_d45_d46_d47_api_kb.py 2 用例（真 PG：五态行矩阵重置/耗尽/新鲜不动/failed 有界重试 + claim 落 claimed_at；改前 FAIL=方法不存在），test_kb_scheduler/test_updater 回归绿。

### D40 [PLAUSIBLE] 调度器 _resolve_exec_meta 缓存命中绕过 fail-closed 状态检查
- `brain/scheduler.py:115-117` _pending_meta 命中直接返回不查 DB status；"只认 SUBMITTED"治本(:120) 只覆盖缓存缺失路径；任务排队期被 cancel（DB CANCELLED 缓存仍在）→ 出队照常 run_task（是否双跑取决于 run_task 内是否再校验）
- 修复：缓存命中路径也查 DB status，非 SUBMITTED 丢弃。
- 验证：排队中被 cancel 的任务出队不执行。
- **✅已修（P1-13，2026-07-07，未commit，PLAUSIBLE 取证坐实）**：取证确认 run_task 内**无**兜底 DB status 校验（只查 _task_running 内存去重），排队期被 cancel 的任务出队会真跑——坐实。修复：_resolve_exec_meta 的 DB status 复核（只认 SUBMITTED）对【缓存命中】路径同样生效；确认非 SUBMITTED 时一并 pop 陈旧 meta 防泄漏；DB 读失败 fail-closed 丢弃本次出队但**不删 meta**（状态未知；任务仍 SUBMITTED，自愈排水/重启对账会补，不静默丢）。既有缓存命中测试（test_scheduler_meta_rebuild::test_resolve_uses_in_memory_meta_when_present）按新契约补 get_task mock。测试 test/test_p1_13_d35_d40_d41_brain.py 3 用例（改前 2 FAIL：CANCELLED 缓存命中照常放行/DB 错误放行；SUBMITTED 护栏改前即绿），test_scheduler_meta_rebuild/test_lifecycle_review_batch1 回归绿。

### D41 [PLAUSIBLE] retry_task 绕过调度器准入超卖并发
- `brain/runner.py:1695-1701` retry_task 直接 await run_task 不走 scheduler.submit_task，不占 _inflight 槽不受 MAX_CONCURRENT_TASKS 与项目沙箱就绪闸门约束；对照 reconcile 走 submit_task(:1495) 口径不一
- 修复：retry_task 走 submit_task 统一准入。
- 验证：批量重跑受并发上限约束。
- **✅已修（P1-13，2026-07-07，未commit，PLAUSIBLE 取证坐实）**：调用方取证——唯一生产调用链 api/routers/task.py retry_task_endpoint → retry_task_background（fire-and-forget，不依赖同步等待结果），改"入队"语义完全兼容。修复：retry_task 重置字段后，若调度器消费循环在跑（新 scheduler.is_consumer_running()）→ scheduler.submit_task 统一准入（占 _inflight 槽、受 MAX_CONCURRENT_TASKS 与项目沙箱就绪闸门约束，保留任务原 queue_priority；auto_accept=None 按 run_task 同口径解析 env SWARM_AUTO_ACCEPT 后传 bool）；调度器未运行（CLI/测试/未启动）→ 保留直跑 run_task 兜底（那些环境无准入面，入队无人消费=静默丢任务，取舍写明）。测试 test/test_p1_13_d35_d40_d41_brain.py 2 用例（改前均 FAIL：绕过调度器直跑/兜底分支不存在），test_b6_review_fixes（retry 清 base_commit）/test_runner 回归绿。

### D42 [CONFIRMED] 池"幽灵清理"只取列表首页——后页存活沙箱误判幽灵遗忘不杀
- `worker/sandbox_pool.py:393-397` items=next_items() 只首页；`:429` 不在 alive_ids 即剔账本且不 kill
- 场景：服务端沙箱总数超一页 → idle 条目落后页被当幽灵剔除 → 远端活沙箱无人管吃配额至 900s 自毁，池退化持续新建
- 修复：分页拉全量 alive 列表再判幽灵。
- 验证：多页沙箱下 idle 条目不被误清。
- **✅已修（P1-13，2026-07-07，未commit）**：_server_alive_ids 分页 API 改 `while paginator.has_next` 拉【全量】；页数安全上限（env SWARM_POOL_LIST_MAX_PAGES 默认 50 钳 [1,1000]，非法回退默认）防 SDK 分页异常死循环；达上限仍未穷尽 → fail-closed 返回 None（本轮跳过幽灵清理）+ WARN，绝不拿半截列表误清存活沙箱；旧 SDK 直接 `.sandboxes` 列表形态零回归。测试 test/test_p1_13_d38_d42_d43_d44_infra_worker.py 3 用例（改前 2 FAIL：只取首页/上限不 fail-closed），test_sandbox_pool 回归绿。

### D43 [CONFIRMED] worker _is_test_path 子串误伤 latest_/contest_/greatest_
- `worker/executor.py:248` `"test_" in 文件名` 命中 latest_ 等 → 文件被从 writable/create 剔除无权写 → 交付静默不完整；brain 侧 shared.py:578 用 startswith("test_") 正确，两处分叉
- 修复：executor 改 startswith("test_") 或用路径段精确匹配，与 brain 侧统一。
- 验证：latest_foo.py 不被误判测试文件。
- **✅已修（P1-13，2026-07-07，未commit）**：不写第三种口径——executor 的本地嵌套 `_is_test_path` 副本直接删除，改 lazy import 消费 brain 权威实现 `shared._is_test_file_path`（basename startswith("test_") 路径段精确匹配；单一事实源杜绝再分叉；worker lazy import brain 已有 stack_detect/planning_nodes 先例，同进程无环无额外开销）。顺带获得 brain 口径的 `_test.go`/`.spec.js` 等后缀覆盖。全仓 sibling 扫描：`"test_" in` 裸子串仅此一处。测试 test/test_p1_13_d38_d42_d43_d44_infra_worker.py 2 用例（改前均 FAIL：latest_/contest_ 被剔出 scope、go/spec 后缀不识别），test_strip_unrequested_tests/test_p1_1_scope_normalize 回归绿。

### D44 [CONFIRMED] git add -N 占位异常路径遗留——对称清理不在 finally
- `worker/executor_sync.py:1028-1042` flock 内 add -N → diff(timeout=60) → restore，裸写顺序；diff 抛异常落 :1067 except 返回 None，restore 跳过，intent-to-add 残留共享真仓 index（遗漏项#3 修了 happy path 漏了异常路径）
- 修复：restore --staged 放 finally 保证异常也清。
- 验证：注入 diff 超时 → index 无残留 -N。
- **✅已修（P1-13，2026-07-07，未commit，含 sibling）**：executor_sync._try_local_git_diff 的 `restore --staged` 移入 try/finally（仍在同一把 _ProjectGitFlock 内原子；清理自身失败降级 WARN 可观测不吞主异常）。★全仓 sibling 扫描命中同形第二处★：brain/nodes/planning_core._git_diff_for_paths（阶梯三 stub 打桩 diff）同样 add -N → diff → reset 裸写序，同修 reset 入 finally。测试 test/test_p1_13_d38_d42_d43_d44_infra_worker.py 3 用例（真 git 仓注入 diff TimeoutExpired：改前 2 FAIL 均残留 intent-to-add；happy path diff 含新文件+index 干净护栏改前即绿），test_clean_upload_intent_add_round29/test_wave3_gitlock/test_planning_core_helpers 回归绿。

### D45 [CONFIRMED] 模板构建脚本 health 闸门在"容器起不来"最坏信号下被整体跳过
- `cube-templates/build-and-create-templates.sh:63-71` cid 为空（entrypoint 崩/docker run 失败）时跳过 /health 直接 push+create_tpl 坏镜像照发；对照 image_builder.py:769 同款 fail-closed；`:53` tpl 为空时 grep 空模式匹配所有行误报成功
- 修复：cid 为空视为 health FAIL 拒发；tpl 空时不跑 grep。
- 验证：起不来的镜像不发模板。
- **✅已修（P1-13，2026-07-07，未commit）**：①cid 为空（docker run 失败/entrypoint 秒崩）→ 显式 health FAIL：`<envd自测失败:容器未启动>` + continue，不 push 不 create（对齐 image_builder.py fail-closed 口径）；②create_tpl 解析不到 tpl-id → 直接返回 `<建失败:no-template-id>`，不再 `grep ""` 空模式匹配所有行误报 READY/输出空 template_id；③顺带把 `declare -A` 改普通索引数组（按 LANGS 下标对齐，行为等价）——bash4+ 专属语法在 macOS 自带 bash3.2 直接语法崩，也使行为测试可在本机跑。**行为测试做到了（无需人工验证例外）**：test/test_p1_13_d39_d45_d46_d47_api_kb.py 2 用例——python subprocess 以 stub docker/curl/cubemastercli/sleep（PATH 前插）驱动【整脚本】跑 5 语言：改前 FAIL（rc=2 declare 语法崩；语义上旧码 cid 空照发+空 tpl 报成功），改后断言 push/create 零调用、RESULT 行全部显式失败标记。bash -n 语法检查通过。

### D46 [PLAUSIBLE] 上传 body DoS：先解析全 body、size 缺失先读后判
- `api/routers/upload.py:97-146` request.form() 在任何大小校验前解析完整 multipart 无全局上限；UploadFile.size 缺失时 read() 整个读进内存后才判 60MB
- 修复：加全局 body 上限中间件；流式读带增量大小校验。
- 验证：超大 body 被早拒。
- **✅已修（P1-13，2026-07-07，未commit）**：①`_enforce_body_limit` 在 request.form()（解析完整 multipart）之前预检：Content-Length 超上限 → 413；**无 Content-Length（chunked）→ 411 fail-closed 拒绝**（取舍写明：浏览器/httpx/curl 的 multipart 上传总带 Content-Length，支持无长度流式需换流式 parser+增量配额，当前 ≤60MB/批的上传面不值该复杂度，宁显式拒绝不承担无界解析；Content-Length 由 uvicorn 强制执行，谎报小值只被截断不构成绕过）；上限 env SWARM_UPLOAD_MAX_BODY_BYTES（默认=批次总上限+8MB 封包余量，非法回退默认，钳下限 1KB）；②size 缺失/谎报防线：`_read_upload_limited` 1MB 分块读+增量校验 min(单文件上限, 批次剩余额度)，超限即断不再消费流（read 不支持带参的鸭子对象退化整读后判限，仍有上限兜底）。测试同文件 5 用例（改前均 FAIL：413 预检不存在/411 不存在/env 回退不存在/增量断读不存在），test_c8_c9_upload_demo_gate/test_clean_upload/test_ingest_lfi 回归绿。

### D47 [CONFIRMED] 无鉴权/越权信息泄漏与非常量时间比较（杂项安全）
- `api/auth.py:80` legacy API key 用 `==` 非常量时间比较；`api/routers/sandbox.py:203-213` /api/sandbox/status 把 api_url/proxy_base/default_template 返回给任意认证用户（含 viewer）与"proxy 内部基建不外泄"矛盾；`config.py:279-353` PUT /api/config 并发写 .env 读改写无锁 last-write-wins 丢键；`preprocess.py:1489,1511` 硬编码模型名无视路由配置
- 修复：legacy key 用 hmac.compare_digest；status 端点按角色裁剪内部字段；.env 写加文件锁；模型名走路由配置。
- 验证：viewer 拿不到内部 URL；并发 config 写不丢键。
- **✅已修（P1-13，2026-07-07，未commit，四子项全修）**：(a) auth.resolve_user legacy key 改 `hmac.compare_digest`（utf-8 bytes），行为平价测试锁回归（时序特性单测不可观测，如实注明）；(b) /api/sandbox/status 的 config 块按角色裁剪：admin 全量，非 admin 只给布尔状态（api_url_configured/proxy_configured/default_template_configured + use_for_worker）——前端 system.js 有 `|| '-'` 兜底优雅降级；(c) 新 `config.settings.env_file_lock`（fcntl.flock 于 `.env.lock` sidecar——锁 sidecar 因 atomic_write_env os.replace 换 inode；跨线程+跨进程），**全部 5 个 .env 读改写点接线**：PUT /api/config、PUT /api/routing、_persist_env_updates、密钥迁移 _clear_plaintext_keys_from_env（executor 线程=真跨线程写者）、sandbox pool toggle，读→改→写→失败回滚全程持锁（回滚在锁内防插队写被回滚覆盖）；(d) preprocess._call_local_llm_impl 两处硬编码模型名改路由配置：本地槽 `model_config.worker_primary`（默认值恰=原硬编码 MiniMax-M2.7-Pro，默认配置零行为变化）、云端回退槽 `model_config.brain_primary`（原硬编码 "Pro/zai-org/GLM-5.1" 已与配置默认 GLM-5.2 脱节，正是无视路由实证）。测试同文件 8 用例（改前 7 FAIL：非 admin 裁剪/锁不存在/持锁期间 _persist_env_updates 与 update_config 均绕锁写盘/两处模型名写死；legacy 行为平价护栏改前即绿），test_d3_config_reload_rollback/test_a2_sandbox_rbac/test_audit_group2_security/test_routing_local_workers 回归绿。

---

## P2 — 深度优化 / 热路径

### D48 [CONFIRMED] async 端点在事件循环做同步 PG 查询（系统性）
- `api/_shared.py:198-214` _require_perm→user_can_on_project 两条同步查询直接在事件循环；全部路由鉴权同模式；SSE _stream_reauthorized(task.py:41) 每连接每 30s 同步 get_user_by_token+成员查询；config.py:550/701 secret_store.set_secret 与 _persist_env_updates 同步；sandbox.py:334 toggle 同类
- 影响：PG 延迟抖动 × 数十活跃 SSE → 事件循环冻结全站故障（单进程）
- 修复：鉴权查询卸 run_in_executor 或用异步连接池；SSE 重认证降频。
- **✅已修（P2-14，2026-07-07，未commit，部分修复+残留）**：①收口=_shared 新增 `_require_perm_async`/`_require_user_async`（同一函数经 asyncio.to_thread，语义逐字节一致，HTTPException 穿透）；②高频面全部接线：任务列表/get_task/get_task_logs/sandbox_status/metrics/notifications×3（含 `_accessible_project_ids_or_none` 卸线程）+ SSE 建流鉴权（新 `_require_task_access_async`）；③SSE 连接后重认证：stream_task/stream_task_logs 均改 `asyncio.to_thread(_stream_reauthorized,…)` + 间隔 env 可配 `SWARM_SSE_REAUTH_INTERVAL_S`（默认 30s；stream_task_logs 原每 5s 重校降频到同源 30s——失权断流延迟上限=该间隔，与 stream_task 既有节奏对齐；下限钳 5s、非法回退默认）；④config.py 同步点全卸线程：PUT /api/config 整段持锁读改写+reload 包 `_apply_env_updates` 闭包 to_thread、PUT /api/routing 的 secret_store 写循环+`_persist_env_updates`、embed/rerank 两处 set_secret+persist、notify persist；⑤sandbox pool toggle 持锁读改写+reload 闭包 to_thread（flock 获取/释放同线程完成，互斥语义不变）。**残留（如实）**：低频一发端点（create/delete/approve 等人工操作面，约百个调用点）鉴权仍同步在环上——全量改造需逐点加 await，漏一处=鉴权静默绕过，风险>收益，本批只做轮询/流式/config 热面。测试 test_p2_14_d48_d49_api.py（卸线程线程断言/HTTPException 穿透/间隔 env+下限+非法回退，改前 seam 不存在必 FAIL）；回归 test_c6_stream_reauth/test_d1_sse_cookie_auth/test_cancel_and_logstream/test_sse_bounded_fanout/test_dual_stream_timeout/test_d3_config_reload_rollback/P1-13 三件套全绿。

### D49 [CONFIRMED] sandbox_status 非 admin N+1 阻塞 + 任务列表无分页拖全量 diff
- `api/routers/sandbox.py:183-201` 每沙箱一条同步 get_task 在列表推导；`task.py:89` list_tasks 无 LIMIT 且 _TASK_SELECT 含 merged_diff（MB 级）/plan/l3_result/token_usage → 长寿项目每次轮询搬全部历史 diff
- 修复：sandbox_status 批量查 creator；list_tasks 加分页且列表视图不选 merged_diff 等重字段（详情单独取）。
- **✅已修（P2-14，2026-07-07，未commit）**：①消费者取证（web tasks.js/project.js 只用 id/status/description/complexity+数组长度；cli task_list 只用 id/status/description；无测试断言列表 shape；store.list_tasks 内部调用者 delete_project 需 uploaded_files、runner 级联取消只需 id/status）→ store 新增 `_TASK_SELECT_LIGHT`/`_row_to_task_light`/`list_tasks_light(limit/offset)`（剔除 merged_diff/plan/l3_result/token_usage/merge_conflicts/ingest_draft 六个重字段，其余键名/解析口径与全量一致；`list_tasks` 原函数保留，内部调用者不动=零行为变化）；②GET /tasks 端点接 light + 分页（limit/offset 可选，缺省 `SWARM_TASK_LIST_DEFAULT_LIMIT` 默认 500 取大保 UI 全量预期，命中默认上限记 WARN 提示翻页；detail 仍走 GET /api/tasks/{id} 全量）；③sandbox_status N+1：store 新增 `get_task_creators(ids)` 一条 `id=ANY(%s)` 只取 (id,created_by_user_id)，可见性过滤的角色+创建者在【一次线程内批量预取】；批量失败 fail-closed 回退原逐沙箱 `_task_creator`（专项测试覆盖回退结论不变）。已知取舍：deleteProject 的确认计数在 >500 任务项目下按截断列表计（仅提示文案，非破坏）。测试 test_p2_14_d48_d49_api.py 6 用例（light 行映射/端点分页传参+不再调全量/批量一次+get_task 零调用+非 admin 可见性结论不变+D47b 裁剪护栏/批量失败回退）；回归 test_a2_sandbox_rbac/test_task_lifecycle/test_cascade_cancel/test_ctodebt_rbac_sweep（1 处源码断言按新收口等价更新）全绿。

### D50 [CONFIRMED] plan JSON 全量注入失败处理/学习 LLM prompt（token 灾难漏改三处）
- `brain/nodes/failure.py:334`、`nodes/__init__.py:2389,2482` plan_obj.model_dump_json(indent=2) 全量 plan（含每子任务 ~42K contract 内联副本）进 handle_failure/learn LLM prompt；validate_plan 已用 slim_plan_json_for_llm_validation 瘦身（原载荷 ~1MB/260K token 把推理模型拖进 25min runaway），这三处漏改，handle_failure 是失败循环高频节点
- 修复：三处复用 slim 瘦身函数。
- **✅已修（P2-14，2026-07-07，未commit）**：plan_validator 新增 `slim_plan_json_or_empty(plan_obj)`（None/非模型安全；slim 自身异常 fail-closed 回退旧全量 model_dump_json——宁可 prompt 大也不丢失败分析输入，再不行才 "{}"），三处（failure.py handle_failure LLM 分析、nodes/__init__ learn_success、learn_failure）统一替换旧 `model_dump_json(indent=2)` 全量注入。测试 test_p2_14_d50_d56_d60_brain.py：helper 三态单测 + learn_failure node 级 prompt 捕获（contract/context_snippets 巨块不进 prompt、计划结构仍在，改前必 FAIL）；handle_failure 与 learn_* 为同一 helper 同一调用形态，回归 test_handle_failure_llm_down_17/test_learn_chain 绿。

### D51 [CONFIRMED] 共享契约 N 份深拷贝——plan 体积病灶
- `contract_utils.py:147-157` shared_contract merge 进每个子任务 contract → 50+ 子任务 × ~42K ≈ MB 级 plan，每 checkpoint 序列化、每 worker prompt 携带；slim 函数本身是此病灶的补丁
- 修复：契约单份引用，派发时注入而非 enrich 进每个 subtask.contract。
- **✅已修（P2-14，2026-07-07，未commit，方案a=派发时合成）**：取证结论=无任何消费者强依赖预 merge（worker prompt 本就把 shared 走独立 key；L2/integration/inject_api_knowledge 全走 plan/state 级 shared_contract；plan_validator 的 overlap 软 warn 反而因预 merge 恒 vacuous；replan/resplit 从不补 enrich）。改动：①plan 节点两处（SIMPLE 快速路径 + 主路径）不再调 enrich_plan_with_shared_contract——N 份 ~42K 内联副本不再进 plan/每次 checkpoint 序列化；②worker/prompts.build_worker_prompt 派发时现场合成 subtask_contract=dict(shared) 后 update(st.contract)（与旧 enrich merge 语义逐字节一致，precedence 不变）；③enrich 函数保留（merge 语义单一参照+测试消费者），docstring 注明。**checkpoint 向后兼容证明**：旧 checkpoint 恢复出的 subtask.contract 已含 shared 副本 → 派发面再合成为同键同值覆盖=幂等，worker 可见契约不变（专项测试锁定）；新旧 prompt 逐字节等价由 test_d51_prompt_equivalent_to_old_enrich_path 锁定。已知行为差：plan_validator overlap 软 warn 复活为有意义信号（warn-only 不阻断，属修复非回归）。测试 test_p2_14_d51_contract_dispatch.py 5 用例（合成完整性/新旧等价/旧 checkpoint 幂等/无 shared 平价/model_dump 只含 1 份 shared——改前为 1+N 份必 FAIL）；回归 test_p1_p2_p3_path/test_validate_plan_slim_p16_2/contract×4 套件/plan_batch×2/worker prompt 套件全绿。

### D52 [CONFIRMED] 逐文件同步是最大热点——应改 tar 批量
- 上传 sandbox.py:1167-1199 每文件 _ensure_remote_dir + files.write，N 文件 ≥2N 往返；拉回每文件 download_url HTTPS GET 每次新建 SSL context(:205)；整模块 ≤800 文件 ≈1600 串行往返
- 修复：本地打 tar 一次上传沙箱内解包 / 沙箱 tar czf 一次下载，O(N)→O(1)。
- **✅已修（P2-14，2026-07-07，未commit，上传全量 tar 化+拉回连接面复用，部分修复+残留）**：①新 `SandboxManager._tar_batch_upload`：本地内存 tar.gz 一次 write → 沙箱内 `mkdir -p && tar -xzf && tar -tzf 逐条 -e 校验`（O(1) 往返；MISSING 非空/err/异常一律 False）→ 临时包清理 best-effort；②`sync_files_to_sandbox`（targeted，worker bootstrap 热路径）与 `sync_project_to_sandbox`（整项目）都改为：先做与旧路径完全同口径的越界/缺失校验记账，合法条目 ≥4 个且 `SWARM_SANDBOX_TAR_SYNC`（默认开，可 false 杀开关）→ tar 批量；tar 任一步失败 fail-closed 回退原逐文件路径（专项测试锁定回退后仍全部上传）；<4 个文件走逐文件（tar 固定 3 次往返开销不划算）；③拉回侧：download_url 直读的 SSL context 进程级复用（verify/no-verify 各一份，原每文件 GET 重建 create_default_context）。**残留（如实）**：拉回未 tar 化——每文件 MAX_SYNC_FILE_SIZE 确定性跳过记账（D30）、_preserve_line_endings 行尾保留、contents={rel:text} 逐文件语义与 tar 流式解包冲突，改造会动 diff 正确性红线，本批只做连接面复用。测试 test_p2_14_d52_d53_worker.py 5 用例（单 tar 上传+tar 内容完整性+解包校验命令/校验缺文件回退/杀开关+小批量/校验记账口径/SSL ctx 复用）；回归 test_clean_upload×2/test_module_reg_sandbox_push/test_ctodebt_bootstrap_propagation/test_sandbox_template_resolve/P0-9 全绿。

### D53 [CONFIRMED] L1 确定性闸门同步阻塞冻结事件循环
- `worker/executor.py:645/766/959` 同步 _deterministic_l1_gate→run_l1_pipeline（同步 HTTP，build timeout 可达 900s）期间全部并发 worker/brain/SSE/看守心跳停摆；_try_local_git_diff 的 git 子进程 + flock LOCK_EX 也在环上；ls-files(:994) 无 timeout
- 修复：L1 pipeline 与 git 操作卸线程池；ls-files 加 timeout。
- **✅已修（P2-14，2026-07-07，未commit）**：取证=Phase3 循环内/Phase4/trivial 三处 `_deterministic_l1_gate()`、Phase4 带 LLM 自检的 `run_l1_pipeline`、三处 `_parse_produce_result`（内含 _get_git_diff 的 git 子进程+per-project flock）全部同步跑在事件循环上（round27 只卸过 _reset_scope_to_head）。全部改 `await asyncio.to_thread(…)`——整个函数调用作为整体进一个线程，flock 获取/释放在同一线程内完成、_ProjectGitFlock 互斥语义不变；executor_sync 的 `ls-files --error-unmatch` 探测补 timeout=30（原无超时，git 挂死占死线程）。测试 test_p2_14_d52_d53_worker.py：trivial 路径闸门阻塞 0.3s 期间心跳持续跳动+闸门线程≠loop 线程（改前 loop 冻结/同线程必 FAIL）、ls-files timeout 参数 spy；回归 test_a5_gate_budget_guard/test_executor_l1_assert/P0-6/P1-13-infra-worker 全绿。

### D54 [CONFIRMED] 每次调用重建 LLM/HTTP 客户端
- `models/router.py:295-342` get_chat_model 每调用 new ChatOpenAI（含 httpx 池）；embed_client.py:122 每次 embed 新建 AsyncClient、:77 requests 无 Session；reranker.py:82/107 每次 rerank 新建 Client；memory.py:110/knowledge.py:162 每请求新建 Store+connect+close
- 修复：按 (provider,model,params) 缓存 LLM 实例（callbacks 经 config 传）；embed/rerank 用长连接 client；Store 池化。
- **✅已修（P2-14，2026-07-07，未commit，部分修复+残留）**：①ChatModel 实例缓存（models/router）：模块级 `_CHAT_MODEL_CACHE` + 线程锁，键=【全部影响行为的值】(provider id/kind/base_url/api_key/重试数/model/temperature/max_tokens/wallclock/timeout/first_token/inter_chunk + callbacks 值指纹)——**热更失效靠值键化**：PUT /api/routing 改了任何行为参数键即不同、自然取新实例（无需 reload 钩子，语义级失效）；callbacks 指纹化只认仓内 ModelInvocationLogger（按构造参数无状态），外部/测试回调 fail-closed 不缓存绝不错共享；内部 _UsageRecorder 按 run_id 键控、跨并发共享安全；超 64 项整体清空防无界；`clear_chat_model_cache()` 运维/测试钩子。②embed_client：sync 路径进程级 requests.Session（requests.post 被 patch 时让位 mock 保测试 seam）；async 路径【按事件循环】缓存 AsyncClient（httpx 连接池绑定创建 loop，跨 loop 错共享会炸——按 loop 键控+已关 loop 惰性清理），获取失败回退一次性 client。③reranker：进程级共享 httpx.Client + nullcontext（with 退出不关）；`httpx.Client` 被 monkeypatch 时走一次性构造（等价旧行为，mock 可注入）；构造失败回退一次性。**残留（如实）**：memory.py/knowledge.py 的 MemoryStore/StructureIndexer 每请求 connect/close 未池化——已是请求内显式 close 的有界模式，引入 async 连接池需管 loop 亲和/关停顺序，超本批风险预算。测试 test_p2_14_d54_d55_clients.py 7 用例（同参同实例/参数变化新实例/base_url+api_key 值变=热更失效/已知回调命中缓存+未知回调不缓存/Session 单例/AsyncClient 同 loop 复用+跨 loop 隔离/共享 rerank client 不被 with 关闭）；回归 test_routing_local_workers/multilevel_fallback/wave_r_router/test_b3_embed_rerank_usage/test_reranker_formats/test_router_health/test_i1_model_tier 全绿。

### D55 [CONFIRMED] GitLab MR 同步事件循环内最多 100+ 次同步 HTTP
- `knowledge/mr_history.py:57-129` sync httpx.Client 每 MR 一次 /changes，被 app.py:1044 await 直调阻塞事件循环
- 修复：卸线程池或改 AsyncClient。
- **✅已修（P2-14，2026-07-07，未commit）**：sync_mr_history_from_gitlab 的两处同步 httpx（MR 列表 + 每 MR /changes，最多 1+limit 次）全部 `await asyncio.to_thread(client.get, …)`（顺序 await，无并发共享；行为/错误分类/落库逻辑不变）。测试 test_p2_14_d54_d55_clients.py::test_d55_mr_sync_does_not_freeze_event_loop：假 client 每 get 同步 sleep 0.15s，心跳协程在 ~0.45s 阻塞窗内持续跳动 ≥20 tick 且 2 MR 全部落库（改前同步 get 冻结 loop，心跳只能跳个位数，必 FAIL）。

### D56 [CONFIRMED] recovery _package_in_baseline 每 blocked 包 os.walk 整树无 memo
- `brain/nodes/recovery.py:134-158` handle_failure 每轮每失败子任务每包重扫整个项目树（大仓+多失败时显著热点，在 async 节点调用链）
- 修复：项目树索引一次 memo，按包名查。
- **✅已修（P2-14，2026-07-07，未commit）**：`_baseline_dir_roots` 一次 os.walk（与旧实现完全同剪枝口径）收集全部目录 posix 路径集合 + 按 project_path memo；`_package_in_baseline` 改集合后缀匹配（谓词与旧逐次 walk 的 endswith 等价）。失效语义**方向性收紧**：阳性（存在→继续等，保守方向）可吃 30s TTL 缓存；阴性（不存在→可能触发 abandon，危险方向）必须以 ≤1s 新鲜索引确认（同一 handle_failure 轮内突发共享一次 walk，跨秒阴性强制重扫——杜绝 stale 缓存漏看刚 apply 落地的包而误判臆造→误 abandon）；walk OSError 不缓存、照旧保守返回 True。测试 test_p2_14_d50_d56_d60_brain.py 4 用例（正确性平价/多阳性查询共享 1 次 walk[改前每查一走必 FAIL]/缓存后落地的新包仍被新鲜重扫看见/OSError 保守）；回归 test_recovery_extraction/test_targeted_recovery×2 绿。

### D57 [CONFIRMED] 多处重复全量扫描/探测
- token 闸门每节点 store.get_task + 全量 diff 重估（runner.py:501）；tech_design 每次两遍 os.walk（planning_nodes.py:442/547）review×3/replan 重复；validate 软建议 LLM 每轮重算不缓存（nodes/__init__.py:1114）；manifest 在场性每调用一趟沙箱 find（l1_pipeline.py:1350，单次 L1 内 5-8 趟）；base_reader 每文件一次 git show（可换 cat-file --batch）；merge 同批 diff 解析两遍；单成功子任务至少 3 次全量构建（executor.py:766-787）
- 修复：按内容/指纹缓存增量化；base 树一次性读；构建结果复用。
- **✅已修（P2-14，2026-07-07，未commit，安全高收益子集，逐项判定）**：**做了**——①tech_design 双 walk：`_gather_project_facts` 120s TTL memo（advisory 事实，成功才缓存）+ `_verify_named_files_exist` 30s TTL memo（correctness-relevant 的存在性判定用短窗；规划期树静态，review×3/replan 短窗重入不再整树 walk+git ls-files；缓存返回浅拷贝防原地污染）；②manifest 在场性：`_manifest_present` 按 (代号, sandbox_id, manifests) 单次 L1 run 内缓存——run_l1_pipeline 入口与 `_push_manifests_to_sandbox`（L1 中途唯一新增沙箱清单的路径）自增代号失效，探测异常不缓存（保守 False+下次重探，同旧行为）；③base_reader：**已有** 单次 merge 内 memo（round21 对抗审计已加 `_cache`，88×git fork→每文件一次），达成"至少进程内 memo"要求。**跳过（理由）**——cat-file --batch：持久子进程生命周期管理（挂死/泄漏/编码协议）超本批风险预算，现 memo 已消重复 fork；token 闸门每节点重估：预算闸门必须看新鲜 token/diff 账，缓存会让超支晚发现=放水；validate 软建议 LLM 缓存：LLM 非确定 + replan 后 plan 变键必失效，命中率低而 stale 软建议有误导面；merge 同批 diff 解析两遍：merge_engine 核心解析路径（P0-3 刚修过），重构风险>收益；3 次全量构建：闸门语义——Phase3 循环内闸、Phase4 确定性复核、Phase4 LLM pipeline 是 W1.2 单一仲裁器的三份【独立证据】，复用结果=证据链塌缩会放水（sticky/翻盘语义漂移）。测试 test_p2_14_d57_scans.py 4 用例（facts 单 walk+TTL 老化重扫/verify memo+判定正确性+按 desc 键控+浅拷贝/manifest 同 run 缓存+代号失效重探/异常不缓存）；回归 test_tech_design_fact_check/test_tech_design_staged/test_planning_nodes/L1 blocked 契约/P1-12 全绿。

### D58 [CONFIRMED] 队列/事件轮询可事件化
- redis_client.py:298 dequeue 每次 3 个 LPOP，scheduler 2s 轮询，KB 5s 轮询 → 可用 BLPOP 阻塞式；准入闸门 sleep(3.0) 在消费循环内造成队头阻塞（scheduler.py:274）
- 修复：BLPOP 多 key；准入按任务记 next-retry 非全局 sleep。
- **✅已修（P2-14，2026-07-07，未commit，部分修复+残留）**：①`TaskQueue.dequeue_blocking(timeout)`：BLPOP 三个优先级 key 一次往返（key 顺序=优先级顺序），队列空在 Redis 侧等 ≤2s、enqueue 即刻唤醒；**必须在线程池调**（scheduler 侧 `await asyncio.to_thread(…)`），timeout 钳 ≤2s 保循环可中断（stop/P1-13 失主停调度器一个 timeout 内生效，不闷死）；Redis 异常 fail-closed 回退原非阻塞 dequeue（逐 key LPOP）；内存 fallback 模式 `supports_blocking()=False` 走原路径+原 _wakeup 轮询（行为不变）。BLPOP 路径本轮已等待过则跳过尾部 _wakeup 等待（不叠加空闲延迟）。②准入闸门去队头阻塞：留池任务记 `_admission_next_retry[task]=now+3s`（同一任务就绪检查节奏 ≥3s 不变、_MAX_ADMISSION_RETRIES×3s 语义不变），**不再全局 sleep(3.0)**——未到期任务出队即回队尾（不检查/不计数），后队就绪任务照常流动；防热旋：一轮内第二次遇到同一未到期任务（=队列只剩等待项）短睡 0.5s，派发成功/队列空清轮。不饿死后队：未到期项直接回尾不占检查配额。**残留（如实）**：KB 5s 轮询未事件化（KBScheduler 独立子系统，改造面大收益低）。测试 test_p2_14_d58_d59_infra.py 4 用例（BLPOP 单往返+key 序+小超时/异常回退 LPOP/内存模式语义/整循环级：队头未就绪任务不阻塞后队就绪任务——改前全局 sleep(3.0) 必 FAIL 且 next-retry+计数=1 锁节奏）；回归 test_scheduler×4/test_a1_scheduler_leadership/test_d24_contract_retry_requeue 全绿。

### D59 [CONFIRMED] 装饰性/失效配置与枚举
- `config/settings.py:389` worker.memory_limit/disk_limit 定义即终点从未接进 sandbox create；`:572` index_update_timeout 同；`types.py:27` TaskStatus 缺 VERIFYING_L3/CLARIFYING/DESIGN_REVIEW/POOLED（真 SSOT 是 task_states.py），枚举纯装饰；`project/store.py:959` _NOTIFY_STATUSES 漏 PARTIAL/CANCELLED（该查询路径对两终态不产通知）
- 修复：接线资源上限或删定义；TaskStatus 补全或统一到 task_states；_NOTIFY_STATUSES 补 PARTIAL/CANCELLED。
- **✅已修（P2-14，2026-07-07，未commit）**：①接线 vs 删的取证：e2b/CubeMaster `Sandbox.create` 参数面只有 template/timeout/metadata/request_timeout（资源规格烤在模板镜像里），**不支持**每沙箱 memory/disk 上限 → 无法接线；全仓 grep（api/cli/web/docs/test）零消费者 → 删 worker.memory_limit/disk_limit 与 kb.index_update_timeout 定义+原位注释注明依据（BaseSettings extra=ignore，.env 残留 SWARM_WORKER_MEMORY_LIMIT 不炸，测试锁定）。②TaskStatus 补全 POOLED/CLARIFYING/DESIGN_REVIEW/VERIFYING_L3 四成员（SSOT 对齐测试：ACTIVE_DB_STATUSES∪TERMINAL_STATES ⊆ TaskStatus 值集；is_terminal/is_successful 判定不受新增影响）——保持既有导入面，未强行把枚举定义搬进 task_states（叶子模块不 import pydantic 生态是其设计约束）。③_NOTIFY_STATUSES 改从 task_states.TERMINAL_STATES 派生（补上 PARTIAL/CANCELLED 两终态，终态集合演进不再漂移）；_task_event_type 新增 task_partial/task_cancelled 事件类型（行为修正，单独测试），前端 notifications.js 补两 case 的标签/药丸（未知类型本有 default 兜底，非 admin 面零破坏）。测试 test_p2_14_d58_d59_infra.py 3 用例；回归 test_task_states/test_ws_notify 绿。

### D60 [CONFIRMED] YuqueSource 全局改写 urllib opener + learn_store finally NameError 掩盖异常
- `knowledge/ingest/sources.py:373` install_opener 装进程全局默认，此后任何 urllib 合法跨 host 30x 被拒；`brain/learn_store.py:125-192` store 构造抛异常时 finally close 抛 NameError 顶替原异常
- 修复：改局部 opener.open；finally 前判 store 已绑定。
- **✅已修（P2-14，2026-07-07，未commit）**：①YuqueSource._get_json 弃 install_opener（进程全局默认 opener 被装上'拒绝跨 host 30x'后，进程内任何无关 urllib 调用的合法跨 host 重定向都会被拒）→ 局部 `build_opener(_NoCrossHostRedirect)` + 模块级 `_guarded_open` seam（单测注入假响应，等价旧 urlopen patch 面；test_kb_ingest 4 处 monkeypatch 同步迁移到新 seam）；防护语义不变（跨 host 30x 仍拒）。②learn_store persist_learn_success/persist_learn_failure：store 先绑定 None + finally `if store is not None: await store.close()`——改前 MemoryStore() 构造抛异常时 finally 引用未绑定名抛 UnboundLocalError 顶替原始 error dict（HEAD 版独立加载实证：RuntimeError('ctor-boom') → 调用方看到 UnboundLocalError）。测试 test_p2_14_d50_d56_d60_brain.py：全局 opener 哨兵不被改写（改前 seam 不存在必 FAIL）+ 两函数构造异常返回含原始错误的 dict（改前 UnboundLocalError 必 FAIL，已用 HEAD 版模块留证）；回归 test_kb_ingest/test_cto_rewalk_fixes（'build_opener' 源码断言仍真）/test_b7_learn_persist_lock 全绿。

---

## 主流程连通性结论

主干 submit→DONE 各边可通（scheduler→ingest→analyze→detect_stack→tech_design→plan→elaborate→validate→dispatch⇄monitor→merge→verify_l2→verify_l3→deliver→learn→DONE），条件边无漏分支，失败环各阶梯有确定性熔断。但：
- **两类任务结构性到不了 DONE**：AUDIT 意图（D01）、含纯删除子任务（D01 同根）
- **一条假 DONE 边**：悬空依赖滞留经熔断进 MERGE，终态账本不闻不问（D25）
- **静默丢产物断点簇**（well-formed 骗过所有下游护栏）：merge 三方丢块 D04、删除蒸发 D03、`--- ` 边界 D05、rename/二进制 D06、apply_hunk 零校验 D-merge、worker 改 readable 不回传 D36、find 截断 D37、Qdrant 覆盖 D13
- **交付通道断**：merge_conflicts 只写不清封死 apply（D07）、approve 静默丢产物（D17）、cancel 流泄漏（D18）
- **锁泄漏死锁**：模块锁异常路径泄漏（D02）
- **多用户隔离破口**：项目劫持（D16）、改密不吊销（D19）、Qdrant 跨项目覆盖（D13）
