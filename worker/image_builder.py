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
import shlex
from dataclasses import dataclass
from pathlib import Path

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


def template_exists_in_cubemaster(template_id: str) -> bool | None:
    """探活：模板是否真实存在于 CubeMaster 的模板 store。

    用途：预处理复用判据（preprocess._phase_build_sandbox）在复用 DB 记录的
    project.config["sandbox_template"] 之前先探活——CubeMaster 模板会因 TTL 过期/
    存储清理而消失（实测 task 82f12ce4：tpl-2ebae48 及全部基础模板被清，DB 仍留记录），
    若不探活直接复用悬空引用，worker 创建沙箱必报 130404 template_not_found。

    返回：True=存在；False=确认不存在（store 里没有此 id）；None=探活本身失败
    （网络/认证错误，无法判定）——None 时调用方应保守不复用（按需重建更安全）。
    """
    import json
    import ssl
    import urllib.request

    from swarm.config import get_config

    if not template_id:
        return False
    try:
        s = get_config().sandbox
        if not getattr(s, "api_url", ""):
            # 没配 CubeMaster 端点(api_url 空)→ 无从探活，返回 None(无法判定，调用方保守不复用)。
            # 也避免 Py3.14 起 urllib Request() 对无 scheme 的 "/templates" 在构造期即抛 ValueError。
            logger.warning("template_exists_in_cubemaster(%s)：sandbox.api_url 未配置，无法探活", template_id)
            return None
        url = s.api_url.rstrip("/") + "/templates"
        headers = {"Authorization": f"Bearer {s.api_key}"} if getattr(s, "api_key", "") else {}
        req = urllib.request.Request(url, headers=headers)
        ctx = None
        if url.lower().startswith("https") and not getattr(s, "verify_ssl", True):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            data = json.loads(resp.read().decode())
        ids = {t.get("templateID") or t.get("template_id") for t in data} if isinstance(data, list) else set()
        return template_id in ids
    except Exception as exc:  # noqa: BLE001
        logger.warning("template_exists_in_cubemaster(%s) 探活失败（无法判定）: %s", template_id, exc)
        return None


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

        import paramiko
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
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
def _toolchain_install(tc: Toolchain) -> str:
    """单工具链的 apt/安装 Dockerfile 片段。"""
    if tc.name == "java":
        ver = tc.version or _JDK_DEFAULT
        return (
            f"RUN apt-get update && apt-get install -y --no-install-recommends "
            f"openjdk-{ver}-jdk maven ca-certificates && rm -rf /var/lib/apt/lists/*\n"
            f"ENV JAVA_HOME=/usr/lib/jvm/java-{ver}-openjdk-amd64\n"
            f'ENV PATH="${{JAVA_HOME}}/bin:${{PATH}}"\n'
        )
    if tc.name == "node":
        ver = tc.version or _NODE_DEFAULT
        return (
            f"RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates "
            f"&& curl -fsSL https://deb.nodesource.com/setup_{ver}.x | bash - "
            f"&& apt-get install -y --no-install-recommends nodejs && rm -rf /var/lib/apt/lists/*\n"
            f"RUN npm config set registry https://registry.npmmirror.com\n"
        )
    if tc.name == "python":
        return (
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            "python3 python3-pip ca-certificates && rm -rf /var/lib/apt/lists/*\n"
        )
    if tc.name == "go":
        return (
            "ENV GO_VERSION=1.22.5\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates "
            "&& curl -fsSL https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz | tar -C /usr/local -xz "
            "&& rm -rf /var/lib/apt/lists/*\n"
            'ENV PATH="/usr/local/go/bin:${PATH}"\n'
        )
    if tc.name == "rust":
        return (
            "RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates "
            "build-essential && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y "
            "&& rm -rf /var/lib/apt/lists/*\n"
            'ENV PATH="/root/.cargo/bin:${PATH}"\n'
        )
    return f"# (未知工具链 {tc.name}，跳过)\n"


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

    has_maven = any(t.name == "java" and t.build_tool == "maven" for t in spec.toolchains)
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


def _make_source_tarball(project_root: str | Path) -> bytes:
    """把项目源码打成 tar.gz（排除构建产物/二进制），返回字节。

    通用规则，适用任何项目。在内存里打包，由 SSH 上传到沙箱机解包进 build context。

    基线一致性（方案 B 关键）：若项目是 git 仓库，导出 **git HEAD 版**源码，使镜像内
    /workspace 的基线与 worker 运行时上传 writable 文件的基线（批次2-A 用 git HEAD）一致，
    避免"工作区未提交改动进了镜像、但 worker 覆盖的是 HEAD 版"导致镜像内文件不一致。
    非 git 仓库回退工作区当前内容。
    """
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
                    for member in src_tar.getmembers():
                        if not member.isfile():
                            continue
                        parts = member.name.split("/")
                        if any(p in _SRC_EXCLUDE_DIRS for p in parts):
                            continue
                        if Path(member.name).suffix.lower() in _SRC_EXCLUDE_EXTS:
                            continue
                        if member.size > 5 * 1024 * 1024:
                            continue
                        f = src_tar.extractfile(member)
                        if f is not None:
                            out_tar.addfile(member, f)
                return out_buf.getvalue()
        except Exception:  # noqa: BLE001 — git archive 失败回退工作区扫描
            pass

    # 非 git 仓库 / git archive 失败 → 扫工作区当前内容
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in project_root.rglob("*"):
            try:
                rel_parts = path.relative_to(project_root).parts
            except ValueError:
                continue
            if any(p in _SRC_EXCLUDE_DIRS for p in rel_parts):
                continue
            if not path.is_file():
                continue
            if path.suffix.lower() in _SRC_EXCLUDE_EXTS:
                continue
            # 跳过超大文件（>5MB，源码不应有，多半是误置的二进制/数据）
            try:
                if path.stat().st_size > 5 * 1024 * 1024:
                    continue
            except OSError:
                continue
            arcname = "/".join(path.relative_to(project_root).parts)
            tar.add(str(path), arcname=arcname)
    return buf.getvalue()


def _selftest_command(spec: EnvSpec) -> str | None:
    """按 EnvSpec 工具链推导【构建期离线编译自测】命令（通用，不写死项目模块名）。

    在镜像内的 /workspace（已 COPY 项目源码）执行，证明完整项目能离线编译。
    返回 None 表示该工具链暂无自测（不阻断发布）。
    """
    for tc in spec.toolchains:
        if tc.name == "java" and tc.build_tool == "maven":
            # -o 离线 -am 连带依赖模块 -q 安静；编译整个 reactor（聚合），不指定模块名（通用）
            return "cd /workspace && mvn -o -B -q -Dmaven.test.skip=true compile"
        if tc.name == "java" and tc.build_tool == "gradle":
            return "cd /workspace && (./gradlew --offline compileJava -q || gradle --offline compileJava -q)"
        if tc.name == "node":
            # 有 build 脚本就跑 build，否则只验证 install 后能解析
            return "cd /workspace && (npm run build --if-present || npm ci --offline || true)"
        if tc.name == "python":
            return "cd /workspace && python3 -m compileall -q ."
        if tc.name == "go":
            return "cd /workspace && go build ./... 2>&1 | head -40"
        if tc.name == "rust":
            return "cd /workspace && cargo build --offline 2>&1 | head -40"
    return None


# ──────────────────────────────────────────────
# warmup pom 生成（Maven 多模块：聚合外部依赖，排内部模块）
# ──────────────────────────────────────────────
_MAVEN_SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<settings>
  <mirrors>
    <mirror>
      <id>aliyun</id><name>Aliyun</name>
      <url>https://maven.aliyun.com/repository/public</url>
      <mirrorOf>central</mirrorOf>
    </mirror>
  </mirrors>
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
_BUILDER_VERSION = "5"  # v5: CubeSandbox 0.4.0 适配——create-from-image 加 --with-cube-ca=true
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
        for rel in sorted(found):
            try:
                with open(project_root / rel, "rb") as f:
                    h.update(rel.encode())
                    h.update(f.read())
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
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
    has_maven = any(t.name == "java" and t.build_tool == "maven" for t in spec.toolchains)
    selftest = _selftest_command(spec)

    try:
        with SSHRunner(ssh) as r:
            # 1) 传 Dockerfile
            r.put_text(dockerfile, f"{remote_dir}/Dockerfile")
            # 2) Maven settings.xml（镜像源）。warmup 现在直接对 /workspace 真项目编译预热，
            #    不再需要精简 warmup pom（v3：真项目编译才能拉全构建插件，见 generate_dockerfile）。
            if has_maven:
                r.put_text(_MAVEN_SETTINGS, f"{remote_dir}/warmup/settings.xml")
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
            logger.info("沙箱机构建镜像 %s (project=%s)", tag, spec.project_id)
            code, out, err = r.run(f"cd {shlex.quote(remote_dir)} && docker build -t {shlex.quote(tag)} . 2>&1", timeout=2400)
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
            # 6) create-from-image
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
                f"cubemastercli tpl create-from-image --image {shlex.quote(tag)} "
                f"{_node_opt}{_v040_opts}"
                f"--writable-layer-size 2G --expose-port 49983 --probe 49983 --probe-path /health 2>&1",
                timeout=300,
            )
            import re
            m = re.search(r"(tpl-[0-9a-f]+)", out)
            if not m:
                return BuildResult(False, image_tag=tag, message=f"create-from-image 未返回 template_id: {out[-300:]}")
            template_id = m.group(1)
            job_m = re.search(r"job_id:\s*([0-9a-f-]+)", out)
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

