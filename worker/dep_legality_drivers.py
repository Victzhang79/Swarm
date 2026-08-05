"""R56-5 依赖合法性闸的多栈 driver 扩展（W-6）。

将 Cargo / Go / Gradle / Python 的 manifest 解析与改写隔离在独立模块，避免
`dep_legality.py` 继续膨胀为 god-file。Maven / npm driver 仍暂留在原文件，
后续可按同样模式迁移。
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from swarm.brain.cargo_registry import registry_versions as cargo_registry_versions

logger = logging.getLogger("swarm.worker.dep_legality_drivers")

# ═══════════════════════════════════════════════════════════════════
# CargoDriver
# ═══════════════════════════════════════════════════════════════════

class CargoDriver:
    """Cargo.toml driver（W-6）。

    Cargo 依赖形态：
      · 简单键值：`serde = "1.0"`
      · inline 表：`serde = { version = "1.0", features = ["derive"] }`
      · workspace 引用：`serde = { workspace = true }`（上游受管）
      · path 引用：`mylib = { path = "../mylib" }`（工作区成员）

    Cargo crate 名没有层级命名空间，故 namespace_mandatory=False；namespace 传
    workspace 根 package name，仅用于"工程前缀 + 真成员"错名修复的 root_name 信号。
    """

    stack = "cargo"
    namespace_mandatory = False
    internal_version_ref = "*"   # workspace 内部 crate 用 * 由 workspace 链接承接
    self_hosted_prefixes = ("$",)
    probe_without_namespace = True

    _DEP_LINE_RE = re.compile(
        r'^\s*([A-Za-z0-9][A-Za-z0-9_-]*)\s*=\s*(.*)$', re.MULTILINE)
    _VERSION_IN_INLINE = re.compile(
        r'(?<=[{,])\s*version\s*=\s*"([^"]+)"', re.S)

    @classmethod
    def _dep_sections_spans(cls, text: str) -> list[tuple[int, int]]:
        """[dependencies] / [dev-dependencies] / [build-dependencies] 的花括号内区间。"""
        spans: list[tuple[int, int]] = []
        for sec in ("dependencies", "dev-dependencies", "build-dependencies"):
            for m in re.finditer(rf'^\s*\[{re.escape(sec)}\]\s*$', text, re.MULTILINE):
                start = m.end()
                # 段结束 = 下一个 [section] 或文件尾
                nxt = re.search(r'^\s*\[', text[start:], re.MULTILINE)
                end = start + nxt.start() if nxt else len(text)
                spans.append((start, end))
        return spans

    def parse_deps(self, text: str) -> list[dict]:
        out: list[dict] = []
        for s, e in self._dep_sections_spans(text):
            seg = text[s:e]
            for m in self._DEP_LINE_RE.finditer(seg):
                name = m.group(1)
                rest = m.group(2).strip()
                version: str | None = None
                # inline table
                if rest.startswith("{"):
                    if "workspace" in rest and re.search(r'workspace\s*=\s*true', rest):
                        version = None   # 上游受管，等价于 Maven 无 version
                    else:
                        vm = self._VERSION_IN_INLINE.search(rest)
                        version = vm.group(1).strip() if vm else None
                else:
                    # 简单字符串/裸版本，去引号
                    version = rest.strip().strip('"').strip("'") or None
                block = m.group(0)
                out.append({"namespace": "", "name": name,
                            "version": version, "block": block})
        return out

    def _workspace_deps_block(self, root_text: str) -> str:
        m = re.search(r'\[workspace\.dependencies\](.*?)(?=^\[|\Z)', root_text,
                      re.S | re.MULTILINE)
        return m.group(1) if m else ""

    def managed_names(self, root_text: str) -> set[str]:
        return set(self._DEP_LINE_RE.findall(self._workspace_deps_block(root_text)))

    def managed_unknown(self, root_text: str) -> bool:
        return False

    def rewrite_namespace(self, block: str, namespace: str) -> str:
        # Cargo 无命名空间概念，无需改写
        return block

    def rewrite_name(self, block: str, name: str) -> str:
        return re.sub(r'^\s*([A-Za-z0-9][A-Za-z0-9_-]*)\s*(?=\s*=)',
                      name, block, count=1)

    def rewrite_version(self, block: str, version: str) -> str:
        if self._VERSION_IN_INLINE.search(block):
            return self._VERSION_IN_INLINE.sub(f'version = "{version}"', block, count=1)
        # 简单形式：替换第一个引号串
        return re.sub(r'(=\s*)["\'][^"\']+["\']', rf'\g<1>"{version}"', block, count=1)

    def root_name(self, root_text: str) -> str | None:
        m = re.search(r'^\s*name\s*=\s*"([^"]+)"', root_text, re.MULTILINE)
        return m.group(1) if m else None

    def remove(self, text: str, block: str) -> str:
        return re.sub(r"[ \t]*" + re.escape(block) + r"\s*\n?", "", text, count=1)


# ═══════════════════════════════════════════════════════════════════
# GoDriver
# ═══════════════════════════════════════════════════════════════════

class GoDriver:
    """go.mod driver（W-6）。

    Go 依赖坐标就是 module path，没有单独的 namespace；因此 namespace_mandatory=False，
    probe_without_namespace=True。工作区成员 = 主模块 path + go.work 里的成员 modules。
    幻影形态：以主模块 path 为前缀但并非工作区成员 → prune。
    """

    stack = "go"
    namespace_mandatory = False
    internal_version_ref = ""
    self_hosted_prefixes = ("$",)
    probe_without_namespace = True

    _REQUIRE_LINE_RE = re.compile(
        r'^\s*(?:require\s+)?([\w.\-/]+)\s+([^\s/]+)(?:\s*//\s*(.*))?\s*$',
        re.MULTILINE)
    _REQUIRE_BLOCK_RE = re.compile(
        r'^\s*require\s*\((.*?)\)', re.S | re.MULTILINE)

    def parse_deps(self, text: str) -> list[dict]:
        out: list[dict] = []
        # 块形态 require ( ... )
        for bm in self._REQUIRE_BLOCK_RE.finditer(text):
            for lm in self._REQUIRE_LINE_RE.finditer(bm.group(1)):
                out.append({
                    "namespace": "",
                    "name": lm.group(1).strip(),
                    "version": lm.group(2).strip(),
                    "block": lm.group(0),
                })
        # 单行形态 require example.com/mod v1.0.0
        for lm in self._REQUIRE_LINE_RE.finditer(text):
            # 若该 match 落在某个块内则跳过
            if any(bm.start() <= lm.start() < bm.end()
                   for bm in self._REQUIRE_BLOCK_RE.finditer(text)):
                continue
            # 单行形态必须带 require 前缀（否则 module 行也会被匹配）
            if not lm.group(0).lstrip().startswith("require"):
                continue
            out.append({
                "namespace": "",
                "name": lm.group(1).strip(),
                "version": lm.group(2).strip(),
                "block": lm.group(0),
            })
        return out

    def managed_names(self, root_text: str) -> set[str]:
        return set()   # Go 无 BOM 对应物

    def managed_unknown(self, root_text: str) -> bool:
        return False

    def rewrite_namespace(self, block: str, namespace: str) -> str:
        return block

    def rewrite_name(self, block: str, name: str) -> str:
        return re.sub(r'(require\s+)[\w.\-/]+', rf'\g<1>{name}', block, count=1)

    def rewrite_version(self, block: str, version: str) -> str:
        return re.sub(r'(require\s+[\w.\-/]+\s+)[^\s/]+', rf'\g<1>{version}', block, count=1)

    def root_name(self, root_text: str) -> str | None:
        m = re.search(r'^\s*module\s+([\w.\-/]+)', root_text, re.MULTILINE)
        return m.group(1).strip() if m else None

    def remove(self, text: str, block: str) -> str:
        return re.sub(r"[ \t]*" + re.escape(block) + r"\s*\n?", "", text, count=1)


# ═══════════════════════════════════════════════════════════════════
# GradleDriver
# ═══════════════════════════════════════════════════════════════════

class GradleDriver:
    """Gradle build.gradle(.kts) driver（W-6 轻量版）。

    Gradle 依赖坐标体系与 Maven 相同（group:name:version），但 manifest DSL 形态繁多：
    Groovy/Kotlin DSL、字符串/map、platform/project/path 等。本 driver 做"有界解析"：
    只识别常见字符串声明，复杂表达式/闭包 DSL 跳过（fail-open）。工作区成员识别
    `project(':module')`；外部依赖查询复用 Maven 仓库探针。
    """

    stack = "gradle"
    namespace_mandatory = False   # Gradle 依赖声明缺 group 不常见，但解析失败时 fail-open
    internal_version_ref = ""
    self_hosted_prefixes = ("$", "project(", "platform(", "fileTree(")
    probe_without_namespace = True

    # 匹配 implementation 'g:a:v' / implementation("g:a:v") / compileOnly "g:a:v" 等
    _DEP_LINE_RE = re.compile(
        r'^\s*(?:implementation|api|compileOnly|runtimeOnly|testImplementation|'
        r'testCompileOnly|testRuntimeOnly|compile|testCompile|runtime)\s*'
        r'[(\s]*["\']([^"\']+)["\']\s*[)\s]*', re.MULTILINE)
    _PROJECT_DEP_RE = re.compile(
        r'^\s*(?:implementation|api|compileOnly|runtimeOnly|testImplementation)\s*'
        r'\(?\s*project\s*\(\s*["\'](:[^"\']+)["\']\s*\)\s*\)?', re.MULTILINE)

    def parse_deps(self, text: str) -> list[dict]:
        out: list[dict] = []
        for m in self._DEP_LINE_RE.finditer(text):
            coord = m.group(1).strip()
            parts = coord.split(":")
            if len(parts) < 2:
                continue
            namespace = parts[0] if len(parts) >= 3 else ""
            name = parts[-2]
            version = parts[-1] if len(parts) >= 3 else None
            out.append({"namespace": namespace, "name": name,
                        "version": version, "block": m.group(0)})
        return out

    def managed_names(self, root_text: str) -> set[str]:
        return set()   # Gradle 无 BOM 对应物（dependencyManagement 是 Maven 概念）

    def managed_unknown(self, root_text: str) -> bool:
        return False

    def rewrite_namespace(self, block: str, namespace: str) -> str:
        # 只在 "g:a:v" 三件套形式下改 group
        return re.sub(r'(["\'])([^"\':]+)(:[^"\']+["\'])',
                      rf'\g<1>{namespace}\g<3>', block, count=1)

    def rewrite_name(self, block: str, name: str) -> str:
        return re.sub(r'(["\'])([^"\']+:)([^"\':]+)(:[^"\']+["\'])',
                      rf'\g<1>\g<2>{name}\g<4>', block, count=1)

    def rewrite_version(self, block: str, version: str) -> str:
        return re.sub(r'(["\'][^"\']+:[^"\']+:)([^"\']+)(["\'])',
                      rf'\g<1>{version}\g<3>', block, count=1)

    def root_name(self, root_text: str) -> str | None:
        m = re.search(r'rootProject\.name\s*=\s*["\']([^"\']+)["\']', root_text)
        if m:
            return m.group(1)
        m = re.search(r'application\s+\{\s*applicationId\s*["\']([^"\']+)', root_text, re.S)
        return m.group(1) if m else None

    def remove(self, text: str, block: str) -> str:
        return re.sub(r"[ \t]*" + re.escape(block) + r"\s*\n?", "", text, count=1)


# ═══════════════════════════════════════════════════════════════════
# PythonDriver
# ═══════════════════════════════════════════════════════════════════

class PythonDriver:
    """Python requirements.txt / pyproject.toml driver（W-6 轻量版）。

    requirements.txt 一行一条依赖；pyproject [project.dependencies] 同样一行一条
    PEP 508 串。我们只解析"包名 + 版本约束"的最小子集，复杂 markers/extras/url 等
    跳过（fail-open）。Python 包无工程命名空间概念，namespace_mandatory=False。
    """

    stack = "python"
    namespace_mandatory = False
    internal_version_ref = ""
    self_hosted_prefixes = ("$",)
    probe_without_namespace = True

    # PEP 508 简化：name[extra,extra]>=1.0,<2; marker
    _PKG_RE = re.compile(
        r'^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)\s*(?:\[[^\]]+\])?\s*'
        r'((?:[<>=~!]+\s*[^\s;]+\s*,?\s*)*)', re.MULTILINE)

    def parse_deps(self, text: str) -> list[dict]:
        out: list[dict] = []
        for m in self._PKG_RE.finditer(text):
            name = m.group(1).strip()
            ver = m.group(2).strip()
            # 忽略空版本（可能是无约束或只写包名）
            if not name or name.lower().startswith("http"):
                continue
            block = m.group(0)
            out.append({"namespace": "", "name": name,
                        "version": ver if ver else None, "block": block})
        return out

    def managed_names(self, root_text: str) -> set[str]:
        return set()

    def managed_unknown(self, root_text: str) -> bool:
        return False

    def rewrite_namespace(self, block: str, namespace: str) -> str:
        return block

    def rewrite_name(self, block: str, name: str) -> str:
        return re.sub(r'^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)', name, block, count=1)

    def rewrite_version(self, block: str, version: str) -> str:
        return re.sub(r'([A-Za-z0-9][A-Za-z0-9_.-]*.*?)([<>=~!]+\s*[^\s;]+)',
                      rf'\g<1>=={version}', block, count=1)

    def root_name(self, root_text: str) -> str | None:
        m = re.search(r'^\s*name\s*=\s*["\']([^"\']+)["\']', root_text, re.MULTILINE)
        return m.group(1) if m else None

    def remove(self, text: str, block: str) -> str:
        return re.sub(r"[ \t]*" + re.escape(block) + r"\s*\n?", "", text, count=1)


# ═══════════════════════════════════════════════════════════════════
# Registry 探针（按栈包装成统一契约）
# ═══════════════════════════════════════════════════════════════════

def cargo_registry_versions_list(_ns: str, name: str) -> list[str] | None:
    """Cargo registry 版本列表；None=不可达，空列表=确证查无。"""
    try:
        vers = cargo_registry_versions(name)
        if vers is None:
            return None
        return sorted(vers)
    except Exception as exc:
        logger.warning("[dep-legality·cargo] crates.io 查询异常: %s", exc)
        return None


def go_registry_versions_list(_ns: str, name: str) -> list[str] | None:
    """Go proxy 版本列表；None=不可达，空列表=确证查无。"""
    url = f"https://proxy.golang.org/{urllib.parse.quote(name, safe='')}/@v/list"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
            if resp.status != 200:
                return None
            text = resp.read().decode("utf-8", errors="replace")
        vers = [v.strip() for v in text.splitlines() if v.strip()]
        return vers
    except urllib.error.HTTPError as exc:
        if exc.code == 404 or exc.code == 410:
            return []
        return None
    except Exception as exc:
        logger.warning("[dep-legality·go] proxy 查询异常: %s", exc)
        return None


def python_registry_versions_list(_ns: str, name: str) -> list[str] | None:
    """PyPI 版本列表；None=不可达，空列表=确证查无。"""
    url = f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        releases = data.get("releases") or {}
        vers = [v for v, files in releases.items() if files]
        return vers
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        return None
    except Exception as exc:
        logger.warning("[dep-legality·python] PyPI 查询异常: %s", exc)
        return None
