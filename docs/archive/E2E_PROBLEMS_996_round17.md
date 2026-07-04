# E2E 996 round17 复盘 — FAILED(29/57)，卡点收敛到 pom 多写者/MERGE base 语义

**终态**：FAILED（2026-07-02 05:30，完成 29/57）。跑 ~5.5h。首次一路走到 MERGE→VERIFY_L2（Fix F/P16-2 全程验证通过），但 merged patch 三次 `git apply --check` 失败，escalate 终止。

**四只只读 agent 交叉取证**（主日志 / merge_engine 代码 / 64 沙箱 / 基线产出），结论如下。

---

## 一、本轮验证通过的既有治本（无回归，正向确证）
- **Fix F（dispatch 前进保证）**：completed 0→32 从不冻结，无 head-of-line 死锁。✅
- **P16-2（VALIDATE_PLAN 瘦身+跳过）**：`plan_json 216775>120000 上限 → 跳过 LLM 软建议`，秒过（上轮 GLM 失控 25min）。✅
- **Fix 1a/1b（round16 换行损坏）**：上轮 `第 2289 行损坏` 彻底消失。✅
- **Fix 1c（护栏）**：MERGE 行首次带 `apply_ok=False`，L2 据此阻断交付，诚实标注"确定性组装缺陷非模型/集成问题"。fail-closed 生效。✅
- **union-merge / #R13-4 派发面熔断 / 三次有界 give-up**：全部按治本预期工作（give-up 防无限循环，熔断切断空转）。✅
- 本轮**模型端点无宕机**（与 round13/14 Qwopus 全程宕对比），主力 Qwopus3.6 稳定。

## 二、致命根因（确定性，非模型能力）= pom.xml 多写者 + MERGE base 不一致
与 round9「pom MERGE facet」/ round15 UPSTREAM_DOWNSTREAM 同一结构主干的**再现**，尚未闭合。

VERIFY_L2 三次一致报错：
```
git apply --check failed: 打补丁失败：pom.xml:215 补丁未应用
错误：pom.xml：补丁未应用
错误：ruoyi-alarm/pom.xml: No such file or directory
```

### 两层问题（都真实、都在同一主干）

**Layer 1 — PLAN 层（上游根，四 agent 共识）**：
- st-1 / st-23-2 / st-34-2 **三个子任务都握有 `pom.xml` + `ruoyi-alarm/pom.xml` 的写权**（VALIDATE_PLAN 00:23 已预警，但只串行化、未去重写权）。
- 任何合并策略都难收敛：05:08–05:28 rebase 振荡死循环——rebase 只在合并期临时踢掉冲突方，重生成 st-23-2 用同一 scope **确定性复现**同样重叠 diff → 又 False，跑满 replan → escalate。

**Layer 2 — MERGE 层（确定性代码 bug）**：
- **① 根 pom.xml:209/215「补丁未应用」= 同文件双块累积 apply**：`merge_engine.py:554-560`（union 分支已含全量插入）**+ `:658-662`（又把 non_conflicting hunk 当第二个 `--- a/pom.xml` 块 append 一次）**。块1 插入 N 行使行号下移，块2 hunk 头仍用 base 原始行号（`_recount_hunk_header:89` 保留 old_start）→ 上下文对不上。失败行号逐轮漂移(209→215)= stale-offset 指纹坐实。
- **② `ruoyi-alarm/pom.xml: No such file` = 新文件被当"改已存在"**：`_format_file_patch:203-206`（亲验）不判 `--- /dev/null` 就重写成 `--- a/{path}`，且不补 `new file mode`。基线 `0d42679` 确认**无 ruoyi-alarm 模块** → 该 pom 本该是新文件。

### ★未闭合的分叉（诚实记录，治本设计需对两侧都成立）
- Agent 2（代码）：worker 产 `--- /dev/null`，被 `_format_file_patch` 抹掉。
- Agent 1/3（日志+沙箱）：worker **自己**把它当已存在文件改（沙箱经 bootstrap 材化了该文件），产的本就是 `--- a/` modify。沙箱 jsonl 无原始 diff 文本（沙箱内 `git diff` exit 128），合并 diff 也没落盘 → 无法直接判定。
- **破解**：治本按"**新旧由 merge base 权威判定，非 worker 头**"设计——base 无此文件即输出新文件补丁，两种解释都覆盖。并加 Fix 0 落盘 + 复现单测拿地面真相。

## 三、放大器（B 类模型能力，闸门已正确处置，不 hack）
- **51 个 worker 跑满 900s 预算**（最大时间黑洞）；主 Agent 迭代上限撞点 80×9/95×3/100×4/110×3/130×4。
- 三次结构性 give-up 均模型幻觉：st-11-1-1（包路径/类名大小写/缺 TwoFactorAuthResult 类）、st-40（幻觉 HttpClient5 符号+超时）、st-26（`Map.is()`/`Array` typo）。
- st-1 retry 把 `<groupId>` 幻觉拼成 `<groupdId>` → Malformed POM（毒化 root pom 补丁）。
- 约 26+ 沙箱废转/退化/空转（近半算力），但**确定性闸门+sticky-fail+abandon 全部正确抓住熔断**，无需加补偿。

## 四、附带工程发现
- **merged diff 未落盘**：`verify_merged_patch_applies` 用 `delete=True` 临时文件跑完即删 → 诊断摩擦，治本应加"失败时 dump"。
- **apply_ok 原子失败假象**：pom 一处坏 → 整包 329279 字符全被 `git apply --check` 拒 → 日志显示"32 全崩"是假象，实为 pom 单点毒化全包。

## 五、治本方向（详见 plan）
守 fail-closed / 确定性 / 跨栈通用 / 非项目写死 / 一次一个 + 单测。
- **Fix 0**（诊断，先做）：apply_ok=False 时 dump merged_diff + 相关 per-writer diff 到 logs_archive。
- **复现单测**：离线构造"新模块 pom（base 缺）+ 多写者 root pom"喂 merge_diffs，拿地面真相，**不用 5h E2E**。
- **Fix ②**：merge_engine 按 merge base 权威判定新/旧——base 无此文件 → 强制输出新文件补丁（`/dev/null`+`new file mode`+`@@ -0,0`）。对分叉两侧都对。
- **Fix ①**：union 分支消除双块 emit（union 已含全量，每文件只 emit 一块）。
- **验证 E2E**：只有前述过后仍是 plan 层写权重叠致命，才评估 Layer 1（pom 写权归一到单 owner）——round15 手册标为 punt-prone，能不动就不动。
