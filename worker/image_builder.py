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


def generate_dockerfile(spec: EnvSpec) -> str:
    """EnvSpec → 项目专属 Dockerfile 文本。"""
    lines = [
        f"# 项目专属沙箱 — project={spec.project_id} deps_hash={spec.deps_hash()}",
        "# 自动生成（project/sandbox_spec.py → worker/image_builder.py）。",
        f"FROM {BASE_IMAGE}",
        "ENV DEBIAN_FRONTEND=noninteractive",
    ]
    if spec.base_only:
        lines.append("# 全新空项目：仅基础镜像，等首个任务需求分析再补装工具链。")
        lines.append("RUN echo 'base-only sandbox'")
        return "\n".join(lines) + "\n"

    for tc in spec.toolchains:
        lines.append(f"# --- toolchain: {tc.name} ({tc.build_tool}) ---")
        lines.append(_toolchain_install(tc).rstrip())

    # warmup：仅对有 dep_source 的工具链生成（批2 初版只对 maven 做完整 warmup，
    # 其它语言留 hook，后续完善——maven 是 ruoyi-e2e 试点核心）
    for tc in spec.toolchains:
        if tc.name == "java" and tc.build_tool == "maven":
            lines.append("# warmup：项目真实外部依赖（warmup pom 由构建器单独上传到 /tmp/warmup/pom.xml）")
            lines.append("COPY warmup/settings.xml /root/.m2/settings.xml")
            lines.append("COPY warmup/pom.xml /tmp/warmup/pom.xml")
            lines.append("RUN cd /tmp/warmup && (mvn -q -B -Dmaven.test.skip=true dependency:go-offline || true) "
                         "&& (mvn -q -B -Dmaven.test.skip=true compile || true) && rm -rf /tmp/warmup/target")
            lines.append("RUN cd /tmp/warmup && (mvn -o -q -B -Dmaven.test.skip=true compile "
                         "&& echo '✅ warmup 离线编译通过：.m2 已填满' "
                         "|| echo '⚠️ warmup 离线编译失败：检查镜像源') && rm -rf /tmp/warmup/target")

    lines.append("# envd 由 base entrypoint 拉起；无前台 CMD。")
    return "\n".join(lines) + "\n"


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


def build_project_image(spec: EnvSpec, project_root: str | Path,
                        ssh: SSHConfig | None = None) -> BuildResult:
    """EnvSpec + 项目根 → 在沙箱机构建专属镜像 + create-from-image → BuildResult。

    步骤：生成 Dockerfile/warmup → SSH 传文件 → docker build → envd /health 自测
         → create-from-image → 解析 template_id。
    """
    ssh = ssh or SSHConfig.from_secret_store()
    if ssh is None:
        return BuildResult(False, message="沙箱机 SSH 凭据未配置（secret_store 缺 sandbox_host_ssh_*）")

    project_root = Path(project_root)
    tag = f"sandbox-proj-{spec.project_id[:12]}:{spec.deps_hash()}"
    remote_dir = f"/tmp/swarm-build/{spec.project_id[:12]}-{spec.deps_hash()}"

    dockerfile = generate_dockerfile(spec)
    has_maven = any(t.name == "java" and t.build_tool == "maven" for t in spec.toolchains)

    try:
        with SSHRunner(ssh) as r:
            # 1) 传 Dockerfile
            r.put_text(dockerfile, f"{remote_dir}/Dockerfile")
            # 2) Maven warmup 文件
            if has_maven:
                mvn_tc = next(t for t in spec.toolchains if t.name == "java" and t.build_tool == "maven")
                warmup_pom = generate_maven_warmup_pom(project_root, mvn_tc.dep_source or "pom.xml")
                r.put_text(warmup_pom, f"{remote_dir}/warmup/pom.xml")
                r.put_text(_MAVEN_SETTINGS, f"{remote_dir}/warmup/settings.xml")
            # 3) docker build
            logger.info("沙箱机构建镜像 %s (project=%s)", tag, spec.project_id)
            code, out, err = r.run(f"cd {shlex.quote(remote_dir)} && docker build -t {shlex.quote(tag)} . 2>&1", timeout=2400)
            if code != 0:
                return BuildResult(False, image_tag=tag, message=f"docker build 失败(exit={code}): {(out + err)[-500:]}")
            # 4) envd /health 自测
            health_cmd = (
                f"cid=$(docker run -d -P {shlex.quote(tag)} 2>/dev/null); sleep 6; "
                f"port=$(docker port $cid 49983/tcp 2>/dev/null | head -1 | cut -d: -f2); "
                f"curl -fsS -m 5 localhost:$port/health >/dev/null 2>&1 && echo HEALTH_OK || echo HEALTH_FAIL; "
                f"docker rm -f $cid >/dev/null 2>&1"
            )
            code, out, _ = r.run(health_cmd, timeout=60)
            if "HEALTH_OK" not in out:
                return BuildResult(False, image_tag=tag, message=f"envd /health 自测失败，拒绝发布模板: {out[-200:]}")
            # 5) create-from-image
            code, out, err = r.run(
                f"cubemastercli tpl create-from-image --image {shlex.quote(tag)} "
                f"--writable-layer-size 2G --expose-port 49983 --probe 49983 --probe-path /health 2>&1",
                timeout=300,
            )
            import re
            m = re.search(r"(tpl-[0-9a-f]+)", out)
            if not m:
                return BuildResult(False, image_tag=tag, message=f"create-from-image 未返回 template_id: {out[-300:]}")
            template_id = m.group(1)
            logger.info("项目 %s 专属模板创建成功: %s", spec.project_id, template_id)
            return BuildResult(True, template_id=template_id, image_tag=tag,
                               message=f"构建成功，模板 {template_id}")
    except Exception as exc:  # noqa: BLE001
        return BuildResult(False, image_tag=tag, message=f"构建异常: {type(exc).__name__}: {exc}")

