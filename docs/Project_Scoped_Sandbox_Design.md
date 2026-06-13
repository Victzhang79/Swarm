# 从通用沙箱池到项目级定制沙箱 架构演进设计（草案 v0.1）

> 状态：**草案，待 CTO 确认**（渐进明细 + 权衡 + 卡点 + 迁移路径，确认后施工）
> 触发：A 部分实测发现"按语言分通用模板"对混编项目(ruoyi-e2e: Java+JS+SQL+...)削足适履；
> 已获得沙箱机权限 → 预处理时即可按项目分析结果构建专属沙箱。
> 取代：Sandbox_Template_Spec_Distribution_Design.md 的"前端下载配置包"方案（已废弃，见 §六）。

---

## 一、范式转变（核心洞察）

| 维度 | 旧：通用沙箱池 | 新：项目级定制沙箱 |
|---|---|---|
| 粒度 | 按语言 5×2 个通用模板 | **每个项目一个专属模板** |
| 依赖 warmup | 通用猜测（命中率<20%，实测） | **项目真实依赖（100%命中）** |
| 语言 | 硬分类，混编项目尴尬 | **不分语言，装齐项目所需全部工具链** |
| exec/verify | 强行按 build/test 分流（实测失效） | **不需分流，一个沙箱跑到底** |
| 构建时机 | 预先批量打 10 个 | **预处理 ANALYZING 阶段，按分析结果构建** |
| 选择逻辑 | executor 按 harness.language 选 | **读 project.config.sandbox_template** |

核心：把"模板"从**语言维度**重构到**项目维度**。沙箱的目的不是"装某语言"，
而是**"能完整拉起这个项目"**——ruoyi-e2e 的专属沙箱 = JDK17+Maven+它的真实.m2，
混编项目则一个沙箱里装齐 Java+Node+... 所有它用到的工具链。

## 二、为什么现在能做（前提已就绪）

1. **预处理已分析项目**：`preprocess.py` 产出 `language_breakdown`（语言分布）；
   ANALYZING 阶段有 LLM 摘要 → 此时已知项目需要什么环境。
2. **Project 模型已预留口子**：`project.config` 注释明确写"项目级配置（模型偏好、**沙箱模板**等）"。
3. **沙箱池天然支持**：池按 `template_id` 分桶（`_bucket_key`），项目级只需各项目 template_id 不同，
   **池机制无需大改**——每个项目的沙箱自然进各自的桶。
4. **有沙箱机权限**：能在预处理时调 docker build + create-from-image（不再需要"发配置包给管理员"）。

## 三、目标流程（渐进明细 L0）

```
[项目预处理 · ANALYZING 阶段后]
  1. 分析项目 → 得到环境规格(语言集 + 各语言依赖清单 + 版本)
     - Java: 聚合多模块 pom → JDK版本 + 外部依赖(排内部模块)
     - Node: package.json → node版本 + deps
     - 混编: 取并集，一个沙箱装齐
  2. 生成项目专属 Dockerfile + warmup（复用 A 的依赖推断逻辑）
  3. 在沙箱机 docker build sandbox-proj-<project_id>:vN + envd自测
  4. create-from-image → 项目专属 template_id
  5. 写入 project.config["sandbox_template"] = tpl-xxx（落库）
  6. 预处理完成，项目 READY

[任务执行]
  7. executor 选模板：优先 project.config["sandbox_template"]，
     回退到旧的 template_for_language（平滑兼容）
  8. 一个项目专属沙箱跑到底（不分 exec/verify）
```

## 四、关键设计决策（待确认 → 见 §五疑问）

### 4.1 不分语言：一个项目一个沙箱，装齐所需工具链
混编项目(如 ruoyi-e2e)的专属沙箱 = JDK17 + Maven + (若前端需构建)Node + ...
取项目 `language_breakdown` 里**占比有意义**的语言对应工具链的并集。

### 4.2 取消 exec/verify 分流
项目级沙箱按"完整拉起项目"构建（相当于现在的 verify 4c4g 全量）。
写代码和编译验证用同一个沙箱。简化掉之前失效的分流逻辑（问题#3）。
> 资源：项目沙箱用 4c4g（验证级）。若项目多导致资源紧，再引入"空闲回收"（已有 pool TTL）。

### 4.3 构建触发与缓存
- **首次预处理**触发构建（耗时几分钟，warmup 下载依赖——已验证 ruoyi-e2e Java 842s）。
- 项目依赖未变 → 复用已有专属模板（按 deps_hash 判断，变了才重建）。
- 构建在沙箱机异步进行，预处理 ANALYZING 阶段等待或后台完成。

### 4.4 向后兼容
`project.config["sandbox_template"]` 为空时回退 `template_for_language`（旧通用池）。
旧项目不受影响，新项目走定制。平滑迁移，不强制重建所有项目。

## 五、关键决策（CTO 已拍板）

1. **构建时机与入池衔接** → ✅ 预处理 ANALYZING 后**构建项目专属沙箱**；因构建耗时长（分钟级），
   **构建完成前不允许执行任务，任务可入池等待**（复用已有"仅入池不立即执行"机制）；
   沙箱就绪后放行池中任务。项目状态：PREPROCESSING → (构建中,任务可入池) → READY(放行执行)。
2. **沙箱机调用** → ✅ **沙箱机 SSH 凭据写入 Swarm 配置**（secret_store 加密），
   后端经 SSH 在沙箱机执行 docker build + create-from-image。初期最简方案，方便调整。
3. **环境判断（按构建文件，非文件扩展名）** → ✅ 预处理时按**构建描述文件**判断需要的工具链，
   力求准确（见 §4.5 判断规则）。**全新空项目**（无构建文件）→ 从**需求的渐进明细**中推断
   架构/环境要求（首个任务的需求分析阶段决定装什么）。
4. **资源回收** → 项目沙箱模板按 LRU/TTL 回收久未用的（复用 pool 回收机制），后续完善。
5. **迁移节奏** → ✅ **先拿 ruoyi-e2e 做基础试点**，走通"预处理→构建→入池→执行→产出"全套，验证后再推广。
6. **旧通用池** → 定制沙箱稳定前保留兜底（project.config 无模板时回退 template_for_language）。

### 4.5 环境判断规则（按构建文件，工程经验完善）
不靠文件扩展名（ruoyi-e2e 有 90 个 .js 但都是静态资源，不需 node 工具链），靠**构建描述文件**：

| 构建文件存在 | 装的工具链 | 依赖 warmup 来源 |
|---|---|---|
| `pom.xml` / `build.gradle` | JDK(读 java.version) + Maven/Gradle | 聚合多模块 pom 外部依赖(排内部模块) |
| `package.json` (有 build/test script) | Node(读 engines 或默认 LTS) + npm/pnpm | package.json deps |
| `requirements.txt`/`pyproject.toml` | Python | pip download |
| `go.mod` | Go | go mod download |
| `Cargo.toml` | Rust | cargo fetch |
| `Dockerfile`/`docker-compose.yml` | 直接复用项目自带（最准） | 项目自己定义 |
| 仅静态文件(.js/.html/.css 无 package.json) | 不装构建工具链（静态资源无需构建） | 无 |
| `.sql` / `.sh` | 不单独装（基础镜像已有 shell；DB 看是否需运行时） | 无 |

混编 = 多个构建文件并存 → 工具链取**并集**（如 pom.xml + package.json with build → JDK+Maven+Node）。
**全新空项目**（无任何构建文件）→ 不预构建，等首个任务需求渐进明细确定技术栈后再构建/补装。

## 六、全新项目的处理（无构建文件）
ruoyi-e2e 是"已有项目"试点。对**全新空项目**（用户说"写个 XX"）：
- 预处理无构建文件可分析 → 沙箱用**基础镜像**（cubesandbox-base + 常用工具链）起步；
- 首个任务的 Brain 需求分析（渐进明细）确定技术栈（如"用 FastAPI 写个 API"→ Python）；
- 据此**补装/重建**项目沙箱，再执行。
- 即：已有项目"预处理时定环境"，全新项目"首个需求分析时定环境"。

---

## 七、待确认疑问（无）— 决策已齐，进入施工



## 八、废弃说明
`Sandbox_Template_Spec_Distribution_Design.md` 的"项目人员 WebUI 声明 → 下载配置包 →
发管理员构建"流程**废弃**——因已获沙箱机权限，构建可由系统在预处理时自动完成，
无需人工传递配置包。但其**依赖推断核心逻辑**（读项目真实 pom → 生成 warmup，排内部模块）
**保留并复用**到本方案的步骤 2。

## 七、施工顺序（确认后）
- **批1**：依赖推断引擎 `project/sandbox_spec.py`——读项目（多语言/多模块）→ 环境规格(语言集+依赖+版本)。纯逻辑+单测。复用 A 的 Java 多模块聚合经验。
- **批2**：项目沙箱构建器——规格 → Dockerfile+warmup → 沙箱机 build+create-from-image → template_id。含 deps_hash 缓存判断。
- **批3**：接入预处理 ANALYZING 阶段 + 写 project.config + executor 优先读项目模板。
- **批4**：ruoyi-e2e 试点 E2E——预处理触发构建专属沙箱 → 跑任务验证（混编：Java编译离线命中 + 若有前端任务也在同沙箱）。
- **批5**：稳定后清理旧通用池/exec-verify 分流（可选）。
