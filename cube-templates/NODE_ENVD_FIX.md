# node 4c4g 镜像 envd 故障 — 诊断与修复方案

> 背景：2026-06-13 E2E 真实验证发现 node 镜像 `tpl-5084cf67e28d4f14b16e0f33`
> 的沙箱能创建但所有 run_code/upload/list_files 返回 500/502
> （`SandboxException: 500 Internal Server Error`），envd 文件系统/执行端点不通。
> 对比 Java 镜像 `tpl-431f89c0` 完全正常。Swarm 侧已加探活+熔断兜底（不再死循环），
> 但 node 任务仍无法在沙箱执行 —— 需修复镜像本身。

## 强假设（待你实测确认）

`cubesandbox-base` 的 **envd 守护进程很可能依赖 base 镜像自带的 Node.js**。
而 `Dockerfile.node` 用 nodesource 装 Node 20 时**覆盖了 base 自带的 node / 改了 PATH**，
导致 base 的 entrypoint 拉起 envd 时找不到正确的 node 或版本不兼容 → envd 启动失败。

依据：
- 5 个 Dockerfile 同 `FROM cubesandbox-base`，build 脚本参数完全一致。
- 唯一区别是各自装的工具链。只有 node 镜像装了 node。
- Java/go/rust/python 镜像不碰 node → envd 全正常；node 镜像装 node → envd 挂。

## 诊断步骤（在能 docker 的机器执行）

```bash
cd cube-templates/dockerfiles

# 1) 本地构建 node 镜像
docker build -f Dockerfile.node -t sandbox-node-debug .

# 2) 进容器，先看 base 自带的 node 在哪、装 node20 后变成什么
docker run --rm -it --entrypoint bash sandbox-node-debug -c '
  echo "=== which node / 版本 ==="; which node; node -v
  echo "=== base 是否自带独立 node (envd 可能用它) ==="; ls -la /usr/bin/node* /opt/*/node* 2>/dev/null
  echo "=== envd 二进制位置 + 是否可执行 ==="; which envd; ls -la $(which envd) 2>/dev/null
  echo "=== entrypoint 脚本 ==="; cat /cube-entrypoint.sh 2>/dev/null || cat /entrypoint.sh 2>/dev/null || echo "(未找到)"
  echo "=== 手动拉起 envd 看报什么错 ==="; envd 2>&1 | head -20 &
  sleep 3; curl -s localhost:49983/health || echo "envd :49983 不通"
'
```

重点看：
- base 是否在 `/opt` 等处自带专用 node，被 PATH 上的 node20 抢了。
- 手动跑 envd 的报错（缺库?node 版本?端口占用?）。

## 修复方案（按诊断结果选其一）

### 方案 A（最可能有效）：不覆盖 base node，用 nvm/独立路径装 node20
```dockerfile
FROM ghcr.io/tencentcloud/cubesandbox-base:2026.16
ENV DEBIAN_FRONTEND=noninteractive
# 用 NodeSource 但装到独立前缀，不动 base 自带 node / 系统 PATH 默认
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates git \
    && mkdir -p /opt/node20 \
    && curl -fsSL https://nodejs.org/dist/v20.18.0/node-v20.18.0-linux-x64.tar.xz \
       | tar -xJ -C /opt/node20 --strip-components=1 \
    && rm -rf /var/lib/apt/lists/*
# 只在 worker 用得到的地方暴露 node20，不放进 envd 启动用的默认 PATH 最前
ENV PATH="/opt/node20/bin:${PATH}"
RUN /opt/node20/bin/npm config set registry https://registry.npmmirror.com
COPY warmup/package.json /opt/warmup/package.json
RUN cd /opt/warmup && (/opt/node20/bin/npm install --no-audit --no-fund || true)
RUN /opt/node20/bin/node -v && /opt/node20/bin/npm -v || true
```
> 注：若诊断显示 envd 不依赖 node，方案 A 仍安全（独立前缀不破坏任何系统组件）。

### 方案 B：确认 envd 不依赖 node 后，保留原 apt 装法但显式恢复 base entrypoint
如果诊断显示 envd 是独立二进制（Go/Rust 写的，不依赖 node），则故障另有原因
（如 nodesource 的 apt 步骤改了 base 镜像的某关键包）。此时：
- 用 `apt-mark hold` 锁住 envd 相关包再装 node；
- 或在 Dockerfile 末尾显式 `ENTRYPOINT`/`CMD` 恢复 base 的 envd 启动命令
  （从诊断步骤看到的 entrypoint 脚本路径）。

## 验证修复（构建后，推模板前先在容器内自测 envd）

```bash
docker build -f Dockerfile.node -t sandbox-node-fixed .
docker run --rm -d --name node-test -p 49983:49983 sandbox-node-fixed
sleep 5
curl -s localhost:49983/health && echo " ✅ envd 健康" || echo " ❌ envd 仍不通"
docker rm -f node-test
```
envd /health 返回 200 再 `create-from-image`，避免再发布坏模板。

## 修复后回填

```bash
bash build-and-create-templates.sh   # 重新生成 node 模板（其它语言可不动）
# 拿到新 node template_id 后，在 Swarm WebUI「系统→沙箱→语言镜像配置」
# 的 node 验证镜像填新 id 保存即生效（落库，无需改代码/重启）。
```

## Swarm 侧已做的兜底（本次 E2E 后加）
- 借/建沙箱后 envd 健康探活，不健康弃用换新（默认重试2次）
- 运行中连续基础设施失败达阈值（默认5）熔断，worker 明确失败而非空转
- config: `sandbox_health_check` / `sandbox_health_retries` / `sandbox_fail_threshold`
即使坏镜像未修，也不会再死循环烧资源；但 node 任务要真正可用仍须修镜像。
