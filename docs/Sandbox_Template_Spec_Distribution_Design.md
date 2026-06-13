# 沙箱模板规格声明与配置包分发 设计文档（草案 v0.1）

> 状态：**草案，待确认**（本文档用渐进明细方式推进，文末「待确认疑问」需 CTO 拍板后才进入施工）
> 关联：Q3_CubeSandbox_Caching_Report.md（依赖烤进模板）、本轮 E2E 验证发现
> 触发：E2E 实测发现现有 Java 模板 JDK 21/SB 2.5.15 与 ruoyi-e2e 真实需求(JDK 17/SB 4.0.6)
> 错配，"通用拍脑袋模板"导致依赖缓存基本不命中、Q3 优化对该项目失效。

---

## 一、问题陈述（为什么要做）

### 1.1 现状的断层
当前沙箱模板的生成与使用之间存在**职责与信息的断层**：

- **项目人员**知道项目需要什么环境（语言、JDK 版本、依赖、包），但**不该也无权**去 Cube 集群构建镜像。
- **沙箱管理员**能构建镜像（docker build + cubemastercli create-from-image），但**不知道**某个项目到底要什么环境。
- 中间缺一个**规格传递载体**：现在靠 `cube-templates/` 里手工维护的「通用」Dockerfile + warmup 清单，是"拍脑袋"的，不绑定任何真实项目。

### 1.2 实测暴露的真实后果（ruoyi-e2e）
| 维度 | ruoyi-e2e 真实需求 | 现有 Java 模板 | 后果 |
|---|---|---|---|
| JDK | 17 | 21 | 向后兼容能编译，但不严谨 |
| Spring Boot | 4.0.6 | warmup 预热 2.5.15 | **跨大版本，缓存基本不命中** |
| warmup 来源 | 应是项目真实多模块 pom（7个） | 通用猜测的单一 pom | 首次全量 mvn compile 仍公网下载整个 SB 4.x 栈 |

→ **Q3"依赖烤进镜像"优化对 ruoyi-e2e 实质失效**。根因：模板 warmup 没用项目的真实依赖清单。

### 1.3 目标
建立一条**项目人员声明 → 系统生成精准配置包 → 管理员构建 → 回填**的闭环，使：
- 项目人员在 WebUI 声明真实环境规格（或系统自动从项目 pom/package.json 推断）；
- 系统生成**绑定该项目真实依赖**的模板配置包（Dockerfile + warmup 清单 + 构建说明），可下载；
- 管理员按包构建，得到 template_id 回填（落库，已实现）；
- 模板 warmup 命中率高，依赖缓存真正生效。

---

## 二、用户流程（渐进明细 · L0 骨架）

```
[项目人员 · WebUI]
  1. 在「沙箱模板」页声明规格：
     - 语言 + 版本（如 Java 17 / Node 20）
     - 依赖来源：① 自动从本项目 pom.xml/package.json 推断（推荐）
                  ② 手工填依赖清单
     - 用途：exec(2c2g) / verify(4c4g)
  2. 点「生成配置包」→ 系统产出 tpl-spec 包（.tar.gz / .zip）：
       Dockerfile + warmup 清单(项目真实依赖) + README(构建命令) + spec.json(元数据)
  3. 下载配置包，发给沙箱管理员（IM/邮件，系统外）

[沙箱管理员 · Cube 集群]
  4. 解包 → docker build → create-from-image（包内 README 有现成命令）
  5. 构建前自测 envd /health（吸取 node 镜像教训）
  6. 得到 template_id

[项目人员 · WebUI]
  7. 在「语言镜像配置」填入 template_id（落库，已实现）→ 保存即生效
```

## 三、渐进明细 · L1（关键环节细化）

### 3.1 依赖推断（核心价值点）
"自动从项目推断依赖"是本功能区别于"手工填模板"的关键，也是 #2 实测缺的那一环：

- **Java/Maven**：聚合多模块（读根 pom 的 `<modules>` + 各子模块 pom），抽取
  `<java.version>`/`<spring-boot.version>`/依赖坐标 → 生成 warmup 聚合 pom（执行 `dependency:go-offline`）。
- **Node**：读 package.json 的 dependencies/devDependencies → warmup `npm ci`。
- **Python**：读 requirements.txt / pyproject.toml → warmup `pip download`。
- **Go/Rust**：go.mod / Cargo.toml → `go mod download` / `cargo fetch`。

> 关键约束：warmup **必须用项目真实清单**，不能再用通用猜测集（这正是 ruoyi-e2e 缓存失效的根因）。

### 3.2 配置包内容（spec 包结构）
```
tpl-spec-<project>-<lang>-<purpose>.tar.gz
  ├── Dockerfile              # FROM cubesandbox-base + 装指定版本工具链 + warmup
  ├── warmup/                 # 项目真实依赖清单(pom/package.json/...)
  ├── spec.json              # 元数据: project, lang, version, purpose, deps_hash, 生成时间
  ├── README.md              # 管理员构建命令 + envd 自测步骤(防再发 node 故障)
  └── build.sh               # docker build + create-from-image + envd /health 自测一键脚本
```

### 3.3 与现有资产的关系（不重造轮子）
- 复用现有 `cube-templates/dockerfiles/Dockerfile.<lang>` 作**模板骨架**，参数化版本/warmup。
- 复用 `build-and-create-templates.sh` 的 create-from-image 逻辑，下沉进包内 build.sh。
- 落库回填已实现（sandbox_templates 表 + WebUI）——本功能只补"生成配置包"前半段。

---

## 四、不做什么（范围边界）
- **不**让 Swarm 直接连 Cube 集群构建镜像（职责分离：构建是管理员的事，且集群凭证不该进 Swarm）。
- **不**自动推送镜像 / 自动回填 template_id（管理员人工确认环节保留，安全）。
- **不**做镜像版本管理/CI 周期重建（后续增强，本期只做"按需生成配置包"）。

---

## 五、A 部分（立即修，不依赖本功能）

与 B（本功能）解耦，**先单独修 ruoyi-e2e 的 Java 模板**，立即恢复缓存有效性：
1. `Dockerfile.java`：JDK 21 → **JDK 17**（对齐项目 `<java.version>17`）。
2. `warmup/pom.xml`：用 **ruoyi-e2e 真实多模块依赖**（SB 4.0.6 栈）替换通用 SB 2.5.15。
3. 重打 Java 模板 + envd /health 自测 + 回填新 template_id。

> A 是 B 的"手工版预演"——把 A 做对，B 就是把这套手工流程产品化。

---

## 六、待确认疑问（CTO 已拍板 → 转为决策记录）

1. **依赖推断实现位置** → ✅ **后端端点 + WebUI 下载，项目人员自助**（项目人员无服务器权限，只能自助）。
2. **Java 多模块推断深度** → ✅ **聚合所有子模块依赖坐标**（warmup 目的是填满 .m2，不保留结构）。
3. **配置包不含项目源码** → ✅ 确认，只含真实依赖清单。
4. **spec 包格式** → .tar.gz（管理员在 Linux 集群）。
5. **目标版本** → ✅ **围绕 ruoyi-e2e 当前真实版本**：Spring Boot **4.0.6** + JDK **17** + Shiro 2.2.0 +
   MyBatis 4.0.1 + Druid(SB4 starter) + mysql-connector-j + jakarta.servlet + thymeleaf/quartz/poi/
   velocity/oshi/kaptcha/yauaa/springdoc 等（详见 §八 真实依赖清单）。
6. **优先级** → ✅ **先处理好本功能**（其它待办靠后）。
7. **exec + verify 都做** → ✅ 两个配置包都生成。
8. **git** → ✅ **不需要**。沙箱无远端推代码能力，配置包/镜像都不装 git（diff 走 difflib，已验证）。

---

## 七、整个流程的卡点分析（核心：该怎么打出"所需"的镜像配置包）

顺着「从项目 → 能用的镜像」全链路，找出每个卡点：

### 卡点①【依赖源错位】warmup 用通用猜测，不用项目真实 pom —— 根本卡点
实测：通用 warmup(SB 2.5.15 + jjwt + 旧坐标) vs ruoyi-e2e 真实(SB 4.0.6 + Shiro 2.2.0 +
jakarta + 新坐标)，命中率 < 20%。**修法：warmup 清单必须从项目真实 pom 派生。**

### 卡点②【多模块依赖分散】不能只读一个 pom
ruoyi-e2e = 7 个 pom（根 + 6 子模块），依赖分散在各子模块，版本靠根 pom 的
`<properties>` + `dependencyManagement` 解析。**修法：聚合根 pom 属性 + 所有子模块 dependencies。**

### 卡点③【warmup 怎么"打全"】最关键的技术卡点
光复制项目 pom 进镜像跑 `dependency:go-offline` **会失败**：ruoyi 子模块互相依赖
(ruoyi-admin → ruoyi-framework → ...)，warmup 时这些**内部模块尚未构建/安装**，
go-offline 找不到内部模块坐标而报错。
**修法（关键）**：生成一个**独立的 warmup 聚合 pom**，规则：
   - 继承项目根 pom 的 `<properties>`（拿到 SB/Shiro/druid 等真实版本）；
   - 只列**外部第三方依赖**（spring-boot-starter-*、shiro-*、druid、mybatis、poi...）；
   - **排除项目内部模块**（ruoyi-common/framework/system/... 这些 artifactId）；
   - `dependency:go-offline -Dmaven.test.skip` 把外部依赖拉满进 .m2。
   - 这样运行时沙箱里项目自己的模块现编现连，外部依赖全走本地缓存。

### 卡点④【工具链版本】JDK + Maven 版本要对齐
JDK 17（项目）≠ 21（现镜像）；SB 4.x/Maven 需较新 maven。**修法：Dockerfile 参数化 JDK 版本。**

### 卡点⑤【验证缺失】打完镜像无人验证 warmup 真生效
直到真跑任务才发现没命中。**修法：配置包内 build.sh 末尾加自测**——
   构建后在容器内对 warmup pom 跑 `mvn -o compile`(离线)，**离线能过 = .m2 真填满了**；
   再加 envd /health 自测（防 node 那种 envd 故障）。

### 卡点⑥【exec vs verify 该装什么不同】
- **verify(4c4g)**：全工具链 + 全 warmup（编译/测试用，要快要全）。
- **exec(2c2g)**：工具链即可，warmup 可精简（写代码为主，未必跑全量编译）。
  但为简单稳妥，**初期 exec 也带同样 warmup**（CoW 克隆，磁盘成本可接受），后续再分化。

---

## 八、ruoyi-e2e 真实依赖清单（A 部分 warmup 直接用）

根 pom `<properties>` 真实版本：
```
ruoyi.version=4.8.3   java.version=17   spring-boot.version=4.0.6
shiro.version=2.2.0   mybatis-spring-boot.version=4.0.1   druid.version=1.2.28
pagehelper.boot.version=4.1.0   fastjson.version=1.2.83   poi.version=4.1.2
velocity.version=2.3   springdoc.version=3.0.3   oshi.version=7.3.0
commons.io.version=2.22.0   kaptcha.version=2.3.3   yauaa.version=8.1.1
```
外部第三方依赖（warmup 拉这些，排除 ruoyi-* 内部模块）：
spring-boot-starter-{webmvc,validation,aspectj,quartz,thymeleaf}、
spring-boot-dependencies(BOM)、spring-boot-maven-plugin、
shiro-{core,web,spring,ehcache}、thymeleaf-extras-shiro、
druid-spring-boot-4-starter、mybatis-spring-boot-starter、pagehelper-spring-boot-starter、
mysql-connector-j、jakarta.servlet-api、fastjson、jackson-databind、commons-io、commons-lang3、
poi-ooxml、velocity-engine-core、oshi-core、kaptcha、yauaa、springdoc-openapi-starter-webmvc-ui、
spring-context-support、spring-web。

---

## 九、施工顺序（确认后）

### A 部分（立即，手工修 ruoyi-e2e Java 模板）
1. `Dockerfile.java`：JDK 21 → **17**（apt 装 openjdk-17-jdk）。
2. 重写 `warmup/pom.xml`：用 §八 真实清单（SB 4.0.6 属性 + 外部依赖，排除 ruoyi-* 内部模块）。
3. 末尾自测：`mvn -o compile` 离线编译 warmup pom 验证 .m2 填满 + envd /health。
4. （管理员）重打 Java 模板 → 回填新 template_id。

### B 部分（产品化 A 的流程）
- **B 批1**：依赖推断引擎 `sandbox/spec_infer.py`——读 project pom（多模块聚合，排内部模块）/
  package.json/requirements → 生成 warmup 清单 + 探测 JDK/语言版本。纯逻辑 + 单测。
- **B 批2**：配置包生成 `POST /api/sandbox/templates/spec`——参数化 Dockerfile + warmup +
  build.sh(含 mvn -o 自测 + envd /health) + README + spec.json，打 .tar.gz。exec+verify 各一包。
- **B 批3**：WebUI「生成配置包/下载」入口（系统级沙箱页，项目人员自助）。
- **B 批4**：端到端走查——声明 → 下载 → 模拟管理员构建 → 回填 → ruoyi-e2e 真实跑通(mvn 离线命中)。
