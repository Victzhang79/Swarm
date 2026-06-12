# Q3 调研报告：CubeSandbox 依赖缓存能力 & 最佳实现路径

> 调研对象：https://github.com/tencentcloud/CubeSandbox （v0.3.1, 2026-06）
> 目标：根治"全新沙箱首个 Java 任务 `mvn compile` 下载全量依赖耗时数分钟"的冷启动慢问题。

---

## 一、CubeSandbox 架构关键事实（与缓存相关）

| 能力 | 事实 | 对缓存的意义 |
|------|------|-------------|
| 底座 | RustVMM + KVM MicroVM，**每沙箱独立内核** | 不是容器共享内核，缓存要进"镜像层"或"卷" |
| 冷启动 | **资源池预热 + 快照克隆**，<60ms | 启动本身极快；慢的是【依赖下载】不是【启动】 |
| 文件系统 | **XFS reflink Copy-on-Write** | 模板→沙箱是 CoW 克隆，模板里有什么沙箱就"零成本"有什么 |
| 模板 | OCI 镜像 → `cubemastercli tpl create-from-image` | **可把依赖烤进自定义镜像** ← 核心 |
| 状态管理(v0.3.0) | **snapshot / clone / rollback** SDK 原语 | 运行态沙箱可存快照、克隆、回滚 |
| 协议 | 镜像内需 `envd` 守护进程(:49983) | 自定义镜像必须含 envd（FROM cubesandbox-base 或 COPY --from） |
| E2B 兼容 | Drop-in，换 URL 即可 | 现有 e2b SDK 代码不用改 |

---

## 二、三条候选路径评估

### 路径 A（强烈推荐）：依赖烤进自定义模板镜像
把 `.m2`(Maven)、`node_modules`/npm 缓存、go module cache、cargo registry 等
**预下载好，构建进每语言的自定义 OCI 镜像**，再 `tpl create-from-image`。

```dockerfile
# 例：Java 模板（4c4g）
FROM ghcr.io/tencentcloud/cubesandbox-base:2026.16
RUN apt-get update && apt-get install -y --no-install-recommends \
      openjdk-21-jdk maven && rm -rf /var/lib/apt/lists/*
# 关键：预热常用依赖进 ~/.m2（构建期联网下载一次，固化进镜像层）
COPY warmup-pom.xml /tmp/warmup/pom.xml
RUN cd /tmp/warmup && mvn -q dependency:go-offline || true
# 之后每个从此模板克隆的沙箱，~/.m2 已含常用依赖，mvn compile 走本地缓存
```
- **优点**：CoW 克隆 → 每个沙箱"免费"拥有预热的 .m2；首个 Java 任务从数分钟→秒级；
  无需运行时联网下载；与现有 e2b SDK / 池化完全兼容（只换 template_id）。
- **缺点**：镜像变大（.m2 常用依赖 ~1-2GB）；依赖更新需重建镜像（可周期性 CI 重建）。
- **落地成本**：写 5 个 Dockerfile（py/node/java/go/rust）+ warmup 清单 + 一次构建推送。

### 路径 B（互补）：共享只读依赖卷
把宿主机的 `.m2`/`node_modules` 缓存目录作为**只读卷挂进沙箱**。
- **现状**：CubeSandbox 文档未见明确的"任意宿主目录挂载"API（E2B 模型偏向镜像而非 bind mount）；
  需确认 Cubelet 是否支持 volume 挂载。**不确定，需在你的部署上验证。**
- **结论**：作为路径 A 的潜在补充，但**不作首选**（机制不明 + 跨 MicroVM 共享只读卷有隔离/一致性考量）。

### 路径 C（重型，暂不需要）：内网 Artifactory/Nexus 镜像代理
起一个内网 Maven/npm 代理，沙箱的包管理器指向它。
- **优点**：依赖永远最新、不占镜像体积、多语言统一。
- **缺点**：要额外运维一个服务；首次仍需"代理→沙箱"传输（比本地 .m2 慢，但比公网快）。
- **结论**：规模化后值得做；当前阶段路径 A 更快见效。**可作为路径 A 的上游**
  （镜像 warmup 时走 Nexus 拉，沙箱运行时走本地 .m2，二者不冲突）。

---

## 三、推荐实现路径（结论）

**主线：路径 A（依赖烤进 4c4g 自定义模板）**，分三步：

1. **为 5 种语言各建一个含依赖缓存的自定义模板**
   - 基于你已建的 4c4g 模板规格，FROM `cubesandbox-base` + 装工具链 + warmup 依赖。
   - Java 用 `mvn dependency:go-offline`；Node 用 `npm ci` 一份典型 package.json；
     Go `go mod download`；Rust `cargo fetch`。
   - `cubemastercli tpl create-from-image` 产出新 template_id。

2. **Swarm 侧切模板**（代码已就绪，只改配置）
   - `.env` 路由表把各语言 template 指向新的"带缓存"模板 id。
   - 我们的 `template_for_language()` + 池化已按语言选模板，**零代码改动**，只换 id。

3. **验证**：跑 RuoYi Java 任务，确认首个 `mvn compile` 从数分钟→秒级、无公网下载。

**可选增强（后续）**：路径 C（内网 Nexus）作为镜像 warmup 的上游 + 运行时兜底；
路径 B（只读卷）待确认 Cubelet 是否支持后评估。

---

## 四、需要你拍板/提供的

1. **谁来建镜像**：自定义镜像构建需要 docker + 推到 Cube 集群可达的 registry。
   这一步在你的 CubeSandbox 宿主/CI 上做（我无法直接访问你的集群）。我可以：
   - 写好 5 个 Dockerfile + warmup 清单 + `tpl create-from-image` 命令脚本，你执行；
   - 或你给我集群/registry 的访问方式，我远程操作。
2. **warmup 依赖清单**：Java 取 RuoYi 的根 pom 依赖即可；其他语言给个典型项目的依赖清单。
3. **是否要内网 Nexus**（路径 C）：现在不必，规模化再说。

> 调研用 Hermes 自带 web_extract/web_search 完成，未安装 agent-reach（自带能力足够）。
