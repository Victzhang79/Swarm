# DESIGN: 项目专属沙箱预置完整源码（方案 B · 编译闭包根治）

> 状态：**草案，待 CTO 确认**（渐进明细 + 权衡 + 待确认疑问表）
> 触发：ruoyi-e2e 试点实测——worker 在沙箱跑 `mvn compile`（编译整个 ruoyi-common 模块），
> 但 `_sync_to_sandbox` 只上传 scope 圈定的 25 个文件（utils 子集 + pom），模块内其它源文件
> （`com.ruoyi.common.config`、`SysDictData` 等）未上传 → `cannot find symbol` → L1 永远过不了。
> 沿用：`Project_Scoped_Sandbox_Design.md` 的"项目专属沙箱=完整拉起项目"本意。

---

## 一、根因（沙箱实测铁证，非臆测）

连沙箱机（192.168.60.106）实测 tpl-b4546ea0fd134f2fa81f3757（sandbox-java:v2，RuoYi 当前专属模板）：

| 项 | 实测 | 结论 |
|---|---|---|
| .m2 依赖 | spring-boot 4.0.6 / mybatis / aliyun 源，133M / 309 jar | ✅ 依赖基本够，模板没大问题 |
| git | `NO_GIT` | 真 bug（worker `git diff` command not found） |
| **/workspace** | **不存在（运行时 sync 才建）** | **空的——编译时只有 scope 子集，缺模块其它源文件** |
| `mvn compile` | `package com.ruoyi.common.config does not exist` / `SysDictData cannot find symbol` | **真命门：scope 上传子集 ≠ maven 模块编译闭包** |

**核心矛盾**：worker 精准上传 scope 子集（设计为最小化传输），却跑**全模块** `mvn compile`（需要模块全部源文件）。两者粒度不匹配 → 内部符号找不到 → 编译必败。

## 二、方案 B 核心思想

**项目专属沙箱镜像构建时，把整个项目源码 COPY 进 /workspace（连同 .m2 warmup）。**
worker 运行时只上传"被修改的 scope 文件"覆盖到已有完整项目上 → `mvn compile` 永远有完整闭包。
这正是 `Project_Scoped_Sandbox_Design.md` 说的"沙箱能完整拉起这个项目"。

```
[镜像构建期 image_builder]
  FROM cubesandbox-base
  + 装工具链(JDK17+Maven) + git           ← 新增 git（修 C-1）
  + COPY .m2 warmup（已有，外部依赖离线）
  + COPY 整个项目源码 → /workspace          ← 新增（核心）
  + mvn -o compile 自测（验证闭包完整）      ← 新增（构建期就证明能编译）

[任务执行期 worker]
  沙箱 /workspace 已有完整项目源码（镜像自带）
  worker 只上传被改/新建的 scope 文件 → 覆盖到 /workspace 对应位置
  mvn -pl <module> -am -o compile           ← 永远有完整闭包
  pull-back 改动文件 → difflib 出 diff（不依赖 git）
```

## 三、渐进明细（L1 实施步骤）

### 3.1 image_builder 增强（worker/image_builder.py）
1. **Dockerfile 装 git**：`_toolchain_install` 或基础层加 `apt-get install -y git`。
2. **COPY 项目源码进 /workspace**：
   - 构建期把项目源码（排除 .git/target/node_modules/build 等）打包上传到沙箱机 build context；
   - Dockerfile `COPY project_src/ /workspace/`。
3. **构建期自测升级**：从"warmup pom 离线编译"升级为"**真项目 `mvn -o -pl <核心模块> -am compile` 离线编译通过**"——构建期就证明 /workspace 完整项目能离线编译，否则拒绝发布模板（envd health + compile 双闸门）。
4. **deps_hash 纳入源码指纹**：源码变了要重建。但纯源码变动频繁——用 **源码树 hash（排 target）** 作为 image tag 的一部分，依赖未变只重打源码层（docker 层缓存复用 .m2 层，秒级）。

### 3.2 worker 上传逻辑适配（worker/executor.py `_sync_to_sandbox`）
- 现状：精准上传 scope 子集到空 /workspace。
- 改后：沙箱 /workspace 已有完整项目（镜像自带）→ worker 上传**仅覆盖被改/新建文件**（scope writable+create_files），readable 文件不必传（镜像已有）。
- 配合批次2-A（干净上传 git HEAD 版）：覆盖文件用 HEAD 版，杜绝脏叠加。
- **workspace reset（批次2-B）语义调整**：镜像自带源码=HEAD 基线，沙箱内 reset 改为"用镜像内原版覆盖"或直接复用镜像层（每个沙箱从模板派生即是干净 HEAD）。

### 3.3 mvn 命令规范（修 C-4，提示词层）
worker verify prompt / harness 默认 build_command 用 `mvn -o -pl <module> -am -q compile`（离线+指定模块+依赖模块），不用裸 `mvn compile`（全 reactor 慢）也不用 `mvn compile <module>`（lifecycle 报错）。

### 3.4 启用开关
- `config.sandbox.project_scoped_enabled` → 改默认 True，或为 RuoYi 项目显式启用并触发重建。

## 四、关键设计决策

### 4.1 项目源码进镜像 vs 运行时全量 sync
选**进镜像**：① 模板派生沙箱秒级就绪（源码已在层里，不用每次传整项目）② docker 层缓存：.m2 层和工具链层不变，只源码层重打 ③ 多沙箱串/并行共享同一模板，源码一致。
运行时全量 sync 整项目=每个沙箱都传几百文件，慢且重复。

### 4.2 源码变更如何重建
- 预处理时构建初版模板（含当时源码快照）。
- 用户项目源码更新（如 git pull）→ 预处理重跑 → 源码树 hash 变 → 重打源码层（.m2 层缓存复用，快）。
- worker 任务产出的改动**不回写镜像**（镜像是 HEAD 基线），改动走 pull-back→本地→difflib diff，由 Brain 合并/审核。

### 4.3 git 装进镜像但仍不依赖它做 diff
装 git 是为了：① 消除 worker agent 偶发调 `git diff` 的 127 错误 ② 构建期可用 git 算源码树 hash。
**但 L1/产出 diff 仍走 difflib**（worker 不依赖沙箱 git 状态），git 仅作环境完整性兜底。

### 4.4 向后兼容
- `project_scoped_enabled=False` 或项目无专属模板 → 回退旧通用池（executor 已实现回退）。
- 试点 RuoYi 走新路，其它项目不受影响。

## 五、待确认疑问表（CTO 拍板）

| # | 疑问 | 已定（CTO 拍板：按工程建议）| 通用化说明 |
|---|------|---------|------|
| Q1 | 源码进镜像的范围 | 排除 .git/target/build/node_modules/dist/.gradle + 二进制，其余全进 | 通用排除规则（复用 preprocess EXCLUDED_DIRS），不针对任何项目 |
| Q2 | 构建期自测编译范围 | 按 EnvSpec 工具链通用自测：maven→`mvn -o -am compile`（聚合）、node→`npm run build`、py→import 自测… 由 spec 推导，非硬编码模块名 | **不写死任何项目的模块名**；编译范围由该项目 EnvSpec 决定 |
| Q3 | project_scoped_enabled 默认值 | 启用为**通用默认路径**：所有项目预处理都走"精准构建专属沙箱"。无构建文件的空项目→base_only 不预构建（等首任务需求分析） | 这是主流程能力，非试点开关；RuoYi 只是第一个验证样本 |
| Q4 | 源码层重建触发 | 通用："源码树 hash（排构建产物）+ deps_hash" 双指纹，任一变则重建对应层 | 适用所有项目，docker 层缓存复用不变层 |
| Q5 | worker reset 语义 | 镜像自带源码即干净基线，每个派生沙箱天然干净 → 取消沙箱内 git reset | 通用简化，所有项目受益 |

### 5.1 关键约束（CTO 强调）：构建期间任务仅入池，不启动
- 项目预处理 → Brain 拿知识库+上下文判断 → **精准构建该项目所需沙箱模板**（每个项目不同，耗时有长有短）。
- **专属沙箱模板就绪前，该项目的任务只能"入池等待"，不能启动执行**（复用已有"仅入池不立即执行"机制）。
- 模板就绪 → 放行池中任务。项目状态机：`PREPROCESSING → BUILDING_SANDBOX(任务可入池) → READY(放行执行)`。
- **状态通知**：构建耗时不定，必须做好进度/状态通知（preprocess SSE 推送 phase=building_sandbox + 进度 + 预计/已耗时），前端可见，任务入池有明确提示。

## 六、施工顺序（确认后）
- **批1**：image_builder 装 git + COPY 项目源码进 /workspace + 构建期真项目离线编译自测。单测 generate_dockerfile 含 COPY/git。
- **批2**：为 RuoYi 重建专属模板（含完整源码），连沙箱机实跑构建，验证镜像内 `mvn -o -pl ruoyi-common -am compile` exit=0。
- **批3**：worker `_sync_to_sandbox` 适配（只覆盖 scope 改动，不传 readable）；mvn 命令规范化；reset 语义调整。
- **批4**：为 RuoYi 启用 project_scoped + E2E 跑 trivial/medium 任务到 DONE，任务日志+沙箱日志双观测，确认编译闭包完整、真实产出。
- **批5**：测不同难度任务，稳定后考虑全局启用。
