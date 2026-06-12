# CubeSandbox 依赖预热模板镜像

把常用依赖（.m2 / npm / go mod / cargo / pip）**烤进自定义 OCI 镜像**，
再 `cubemastercli tpl create-from-image` 生成模板。CubeSandbox 用 XFS reflink CoW
克隆，每个从模板启动的沙箱"零成本"拥有这些缓存 → 首个编译任务从数分钟降到秒级。

## 机制（为什么这样做）
- CubeSandbox 与沙箱通过镜像内 `envd`(:49983) 通信 → 自定义镜像必须含 envd。
  最简单：`FROM ghcr.io/tencentcloud/cubesandbox-base`（已预装 envd + 通用 entrypoint）。
- 依赖在【构建期】联网下载一次，固化进镜像层；运行期沙箱走本地缓存，不再公网下载。

## 用法（在你的 CubeSandbox 集群/能 docker build 且 registry 集群可达的机器上）

```bash
# 1) 改 build-and-create-templates.sh 顶部的 REGISTRY 为你集群可达的镜像仓库
# 2) 一键构建 5 镜像 + 创建 5 模板
bash build-and-create-templates.sh

# 3) 脚本结束会打印 5 个新 template_id，填回 Swarm 的 .env:
#    SWARM_SANDBOX_TEMPLATE_JAVA=tpl-xxxx  等（见末尾"回填"段）
```

## 目录
```
dockerfiles/
  Dockerfile.python   + warmup/requirements.txt
  Dockerfile.node     + warmup/package.json
  Dockerfile.java     + warmup/pom.xml
  Dockerfile.go       + warmup/go.mod
  Dockerfile.rust     + warmup/Cargo.toml
build-and-create-templates.sh   # 构建+推送+create-from-image
```

## 维护
- 依赖更新：改对应 warmup 清单 → 重跑 build 脚本 → 换新 template_id。
- 建议挂 CI 周期重建（每周/依赖变更触发），保持缓存新鲜。
- 镜像体积：Java .m2 常用依赖 ~1-2GB；磁盘 `/data/cubelet` 需 ≥200GB（多模板）。

## 回填到 Swarm（零代码，只换 id）
Swarm 已按语言选模板（`config/settings.py: template_for_language`）。
拿到新 id 后，在 Swarm 的 `.env` 设：
```
SWARM_SANDBOX_TEMPLATE_PYTHON=tpl-新python
SWARM_SANDBOX_TEMPLATE_NODE=tpl-新node
SWARM_SANDBOX_TEMPLATE_JAVA=tpl-新java
SWARM_SANDBOX_TEMPLATE_GO=tpl-新go
SWARM_SANDBOX_TEMPLATE_RUST=tpl-新rust
```
重启 API 即生效，沙箱池会用新模板预热。
