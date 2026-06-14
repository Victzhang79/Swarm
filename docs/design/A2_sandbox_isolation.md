# DESIGN DOC: A2 — 沙箱级隔离 enforcement

> 状态：**v1.1 已完成（2026-06-14）@ v0.7.1**，三批全部实施验证
> 作者：CTO ｜ 日期：2026-06-14 ｜ 基线：v0.7.0 @ e2b910b
> 关联：docs/ARCHITECTURE_ROADMAP.md A2（P0，REVISED 12.10/12.18）

---

## 0. 一句话目标

把"代码执行的隔离"从【应用层协作式拦截】下沉到【沙箱层强制 enforcement】，
使不可信代码（LLM 生成、潜在恶意）无法越权访问文件、网络、其它项目/租户的数据，
即使绕过应用层工具直接 syscall 也拦得住。

---

## 1. 现状复核（基于真实代码）

A2 关切"隔离强度"。复核后，当前隔离**全在应用层，无沙箱层 enforcement**：

### 1.1 文件 scope —— 🟡 应用层协作式，可绕过
- `ScopeGuard`（tools/scope_guard.py）用 `ContextVar` + 工具内部**自觉**调 `require_writable/readable`。
- **绕过点**：LLM 让沙箱内进程**直接** `open()/os.system("rm ...")`/shell 重定向，不经过受 scope 检查的工具 → scope 完全不拦。
- `_scope_violations`（l1_pipeline.py）是 **L1 阶段事后检查 diff**，不阻止写入，只在验证时报违规。

### 1.2 命令执行 —— 🔴 无白名单
- `run_command`（sandbox.py:592）走 SDK `commands.run`，**无命令白名单/黑名单**。沙箱内可跑任意命令（含 curl 外网、读系统文件等）。

### 1.3 沙箱复用与跨项目隔离 —— 🟡 有清理但不彻底
- 热池 `acquire(template_id, project_id=...)` **按 template 分桶复用，不按 project 隔离**：A 项目归还的沙箱可能被 B 项目 acquire。
- **已有缓解**：`_clean_workspace`（归还时清 + 取用前再清，失败则弃用新建）—— 双保险。
- **缺口**：`clean_workspace` 只清 `/workspace`，**不清** `/tmp`、`$HOME`、pip/npm 全局缓存、环境变量、后台进程残留 → 跨项目可能泄漏这些位置的数据。

### 1.4 沙箱边界本身 —— 🟢 CubeSandbox 容器（实测：隔离到位）
- 执行环境是远程 CubeSandbox（容器），不是本地进程。
- **实测结果（2026-06-14，scripts/a2_sandbox_boundary_probe.py 真沙箱探测）**：
  - 身份：`uid=1000(user)` **非 root**，非特权。
  - 容器：cgroup `/default/<template>_0` + 存在 `/.dockerenv` → 真容器，按 template 分 cgroup。
  - 资源：2 CPU、**内存硬限 2GB**（cgroup memory.max=2097152000）、ulimit 进程数 7701 → 资源限额生效。
  - 逃逸：`/proc/1/root` → **Permission denied**，看不到宿主根。
  - 网络（关键）：
    - 内网控制面 192.168.60.106:3000 → **unreachable**
    - 内网 PG :5432 → **blocked**
    - 公网 api.github.com → **blocked**（且沙箱内未装 curl/wget/nc，只有 python3）
    - DNS 可解析但实际连接被封（纵深防御）
  - 文件：探测时 HOME/tmp 干净。
- **结论**：CubeSandbox 的网络/逃逸/资源隔离**已相当到位**——沙箱内上不了公网、碰不到内网控制面/PG、非 root、有资源硬限、看不到宿主。
  **这推翻了原"逃逸/网络边界未知=高危"的担忧。A2 真实工作量大幅缩小。**

### 1.5 真实威胁分级（实测后修正）
| 威胁 | 实测后状态 | 严重度 |
|------|---------|--------|
| 沙箱内任意代码搞坏自己沙箱 | 容器隔离 + 资源硬限，损害局限单沙箱 | 低（可接受） |
| 跨项目数据泄漏（复用沙箱） | workspace 已清，但 /tmp /home 缓存未清 | **中（仍需修）** |
| 沙箱逃逸到宿主/控制面 | 实测：/proc/1/root 拒绝、控制面 unreachable | ~~高~~ → **已被 CubeSandbox 隔离** |
| 沙箱访问内网/公网 | 实测：PG/控制面/公网全 blocked | ~~高~~ → **已被网络策略隔离** |
| 资源耗尽（fork bomb / OOM 邻居） | 实测：cgroup 内存 2GB 硬限 + ulimit | ~~中~~ → **已被 cgroup 限额** |
| **应用层越权（用户A看/管用户B的沙箱）** | RBAC 项目级未对沙箱操作 enforce（待查） | **中（生产级要求，本轮新增焦点）** |

---

## 2. 已确认决议（2026-06-14 拍板 + 实测修正）

| # | 疑问 | 决议 |
|---|------|------|
| **Q1** | A2 程度 | **生产级 + 角色分权**（不是"内部可信就不做"）：系统级配置仅管理员；**项目级用户只能查看/管理自己任务的沙箱**。威胁模型从"防外部恶意"重定为"**最小权限 + 防误操作 + 防越权看别人**"。 |
| **Q2** | 先实测边界 | **已完成**（scripts/a2_sandbox_boundary_probe.py）：CubeSandbox 网络/逃逸/资源隔离实测到位（见 §1.4）。沙箱层安全无需我们补——A2 焦点收敛到**应用层越权 + 跨项目清理 + 命令拦截**。 |
| **Q3** | 跨项目泄漏 | **C**：默认扩展 clean_workspace（清 /tmp /home 缓存），留 per-project 不复用开关给高隔离场景。 |
| **Q4** | 命令拦截 | **基础安全黑名单**：内置一批默认危险命令规则 + **管理员可在 WebUI 配置**（落库可视化可配，符合"配置落库+WebUI 可配+保存即生效"偏好）。防误操作，诚实标注非防恶意。 |
| **Q5** | Docker 关系 | **澄清**：Docker 化指 **Swarm 项目自身**以容器启动，**不涉及 CubeSandbox**（独立公网服务器）。A2 全是应用层工作，与 Docker 不耦合。 |

### 2.1 实测结论对范围的影响（重要）
CubeSandbox 沙箱层隔离实测到位（非 root / 容器 cgroup / 2GB 内存硬限 / 控制面+PG+公网全 blocked / 看不到宿主）。
**因此 A2 不需要做"沙箱级 enforcement"（沙箱本就隔离好了）**，真实工作收敛为应用层三件事：
1. **RBAC 项目级 enforce 沙箱操作**（核心：用户只能动自己项目的沙箱）—— §1.5 新焦点
2. **跨项目清理扩展**（/tmp /home）+ per-project 隔离开关
3. **命令黑名单**（管理员 WebUI 可配 + 内置默认）

### 2.2 应用层越权缺口（实查确认）
`api/routers/sandbox.py` 端点（create / DELETE {sid} / cleanup / {sid}/files / {sid}/logs）**无 RBAC 依赖**：
任何登录用户传任意 sandbox_id 即可删除/读取/查看他人沙箱。`sandbox_status` 的 project_id 过滤是**可选非强制**。
→ 这是生产级"项目级用户只能管自己沙箱"的真实缺口，A2 批1 修它。

---

## 3. 渐进明细（已确认，可实施）

> 三批各自独立可上线/回滚。实测已证沙箱层安全，本节全是应用层加固。

### 批 1：沙箱操作 RBAC 项目级 enforce（核心：用户只能管自己项目的沙箱）✅ 完成 @ v0.7.1
1. [x] 复用现有 RBAC：deps.py / _shared.py（_require_user/_require_perm）+ auth.store（user_can_on_project/list_user_project_ids），零造轮子。
2. [x] sandbox 资源端点项目级鉴权：DELETE / files / files/content / logs → _require_sandbox_access（按沙箱所属 project 校验；无归属仅 admin）；create → 对目标项目 task:create。
3. [x] sandbox_status 按当前用户可见项目过滤（admin 看全部）。
4. [x] 系统级操作（cleanup / pool toggle / pool / pool/reap / orphans）→ _require_admin。
5. [x] RBAC-off 时 _require_user 返回 anonymous admin，开箱即用不破。
6. [x] 验证：test_a2_sandbox_rbac.py 5 用例（admin全权/成员自项目/跨项目403/无归属仅admin/require_admin拒非admin）；87 sandbox+rbac+auth 回归绿。

### 批 2：跨项目清理扩展 + per-project 隔离开关（Q3=C）✅ 完成 @ v0.7.1
1. [x] clean_workspace 扩展：清 /workspace + /tmp + $HOME 缓存（.cache/.npm/.cargo/.gradle 等），保留 shell 配置。
2. [x] SandboxConfig.isolate_per_project 配置（默认 false）；HotSandboxPool._bucket_key 隔离开启时桶键含 project。
3. [x] release 从 manager meta 读回 project_id 算桶键，与 acquire 一致（防回错桶破坏隔离）。
4. [x] 验证：test_a2_isolation_cleanup.py 3 用例（默认只按template/隔离时按project分桶/清理命令覆盖tmp+home不删shell配置）。

### 批 3：命令安全黑名单（管理员 WebUI 可配 + 内置默认）✅ 完成 @ v0.7.1
1. [x] config/command_blacklist_store.py：规则落 PG 表 + 内置 8 条默认危险规则（首次建表 seed）+ TTL 缓存 + fail-open。
2. [x] run_command 执行前 check_command 拦截 + 审计 + 留痕；_skip_blacklist 供 clean_workspace 等内部命令跳过。
3. [x] 管理员 CRUD API（仅 admin）：GET/POST/toggle/DELETE /api/sandbox/command-blacklist（内置规则不可删只可停用）。
4. [x] 并入 init_db + startup 建表。诚实标注：防误操作，恶意由沙箱层隔离兜底（已实测）。
5. [x] 验证：test_a2_command_blacklist.py 5 用例（seed/拦截rm-rf根+forkbomb/不误伤正常命令/管理员CRUD立即生效+内置不可删）。

---

## 4. 不做什么（范围边界）

- **不做沙箱级 enforcement**（网络/逃逸/资源）——实测证明 CubeSandbox 已隔离到位，无需补。
- **不自己改 CubeSandbox 镜像/网络策略**——它是独立公网服务器，隔离已达标。
- **不做命令白名单**（限制 LLM 不现实）；黑名单仅防误操作，诚实标注。
- **不把应用层 scope 当安全边界**——它是协作式便利层；真正的安全边界是 CubeSandbox 容器（已实测）。

---

## 5. 风险

| 风险 | 缓解 |
|------|------|
| RBAC enforce 破坏 RBAC-off 开箱即用 | 复用现有 rbac_enabled 开关，off 时放行（保 CI/单机）|
| clean_workspace 扩展误删系统文件致沙箱坏 | 保守白名单清理范围 + 失败弃用新建（已有机制）|
| 黑名单误伤正常构建命令 | 规则精准（针对性危险模式）+ 管理员可调 + 充分测试 |
| 黑名单被误当作防恶意 | 文档/UI 诚实标注"防误操作；恶意由沙箱层隔离兜底" |

---

*v1.0 已确认（边界实测支撑）。下一步：批 1（沙箱操作 RBAC enforce），先复核现有 RBAC 机制再动手。
探测脚本 scripts/a2_sandbox_boundary_probe.py 保留为运维工具（重测边界用）。*
