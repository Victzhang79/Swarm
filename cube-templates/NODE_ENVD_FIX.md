# node 4c4g 镜像 envd 故障 — 诊断记录与结论（已上机实测更新）

> 背景：2026-06-13 E2E 验证发现 node 模板 `tpl-5084cf67e28d4f14b16e0f33` 的某个沙箱实例
> (`991544a6`) 所有 run_code/upload/list_files 返回 500/502，10 分钟死循环 186 次。
>
> **更新（2026-06-13 上沙箱机 <沙箱机内网IP> 实测后）**：下面"强假设"已被实测**推翻**，
> 保留作为排查记录，结论见 §结论。

## ❌ 原强假设（已推翻）
> ~~cubesandbox-base 的 envd 依赖 base 自带 Node.js，nodesource 装 node20 覆盖了它导致 envd 挂。~~

## ✅ 上机实测结果（推翻假设）
在沙箱机直接进容器对比 node 与 java 镜像：
```
# node 镜像 sandbox-node:v1
/usr/bin/envd  10457272 字节   node v20.20.2 (/usr/bin/node)
# java 镜像 sandbox-java:v1
/usr/bin/envd  10457272 字节   无 node
```
- **envd 是独立二进制（10MB），node/java 镜像里完全相同**，不依赖 Node.js。
- 两镜像里手动跑 `envd` 启动行为一致（cgroup 警告→fallback→正常），envd 二进制无差异。
- 新打的 `sandbox-java:v2` envd /health 实测通过；create-from-image 正常。

→ **node 镜像静态内容与 envd 二进制均正常**，"nodesource 覆盖 node" 的假设不成立。

## 结论（诚实修正）
那次 node 沙箱 `991544a6` 的 500/502 **更可能是该沙箱实例的运行时偶发故障**
（资源/调度/网络抖动），**不是 node 镜像本身损坏**。证据：
- envd 二进制和 java 完全相同且能正常启动；
- 同一 node 模板此前也成功跑过任务（历史任务列表里有 node 任务 DONE）。

## 已有兜底（本轮 E2E 后加，对任何沙箱偶发故障都生效）
`worker/sandbox.py` + `executor.py`（commit 25664d6）：
- **envd 健康探活**：借/建沙箱后跑 echo 标记验证 envd，不健康弃用换新（默认重试2次）；
- **连续失败熔断**：5xx/连接错误连续达阈值（默认5）抛 SandboxUnhealthyError，
  worker 明确失败而非空转死循环；
- config: `sandbox_health_check` / `sandbox_health_retries` / `sandbox_fail_threshold`。

即此类偶发故障**不会再导致 10 分钟死循环烧资源**——探活拦截 + 熔断兜底。

## 待办
- 用新 Swarm（含探活+熔断）跑 node 任务复测，确认 node 模板现在可正常工作（验证偶发，非持续故障）。
- 若复测仍频繁失败，再深查 CubeSandbox 对 node 模板的调度/资源配置（而非镜像内容）。

## 镜像构建参考（已验证可用，沙箱机 <沙箱机内网IP>）
- base 镜像机器上 tag 为 `:latest`（非 `:2026.16`，Dockerfile 构建时需对齐）。
- 构建：`docker build -f Dockerfile.<lang> -t sandbox-<lang>:vN dockerfiles/`
- 自测：`docker run -d -p 49983:49983 <img>` → `curl localhost:49983/health`
- 建模板：`cubemastercli tpl create-from-image --image <img> --writable-layer-size 2G --expose-port 49983 --probe 49983 --probe-path /health`
