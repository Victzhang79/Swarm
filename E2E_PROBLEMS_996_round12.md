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

### T+10 17:54 中止（治本 token 统计两个真 bug，非编排问题）
盯 token 端点时发现 **10 个云端 call 报 2.88 亿 token**（28.8M/call，物理不可能）→ 用户手动取消 round12，先修统计再重跑。三盯取证(probe)定位**两个真 bug**（commit 9b2cffb）：
1. **流式 usage 膨胀 ~580×**：云端 GLM 网关【每 chunk】回【累计】usage（581/582 chunk 带），langchain 拼接时【逐字段求和】→ Σ累计 ≈ N×真值。本地仅末 chunk 带 usage 无此病。**治本=逐 chunk 按字段取 max 不求和**（on_llm_new_token，run_id 隔离，并行规划下不串号）。
2. **brain 编排全归「无项目归属」**：set_worker_context 仅 worker/executor 设过，brain 自身 ainvoke 从未设。**治本=run_task 入口设一次 ContextVar**（异步 await 链 + gather 子任务 copy_context 自然传播全覆盖）。
- 实测验证：并行 2 路云端每 call ~654 token、云端+本地均正确归项目；3 回归测试+全量套件过；2.88 亿垃圾行已清，token 表复位清零基线。
- **待用户三清三盯重启 round12**（治本 8 条 round11 + token 统计两病 全部就绪）。
