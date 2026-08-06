"""R56-5 依赖合法性闸的多栈 driver 扩展（W-6）。

将 Cargo / Go / Gradle / Python 的 manifest 解析与改写隔离在独立模块，避免
`dep_legality.py` 继续膨胀为 god-file。Maven / npm driver 仍暂留在原文件，
后续可按同样模式迁移。
"""
from __future__ import annotations

import json
import logging
import re
import tomllib
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
    # ★#29-2 W-6★ 本正则**只许用于 requirements.txt**（一行一条 PEP 508 串）。
    # 曾配 re.MULTILINE 直接扫 pyproject.toml 全文 ⇒ 把 TOML 的「键 = 值」当
    # 「包名 + 版本约束」：实测标准 PEP 621 文件解析出 9 条"依赖"全是 TOML 键
    # （name/version/description/requires-python/build-backend/line-length/dev...），
    # 而真依赖 flask/requests/pydantic/pytest/ruff **一条没解析到**。
    # 后果非确定性且严重：哪些键被判 prune 取决于该键名在 PyPI 是否恰好是真包
    # （实测 `requires`/`version`/`dependencies` 有包→存活；`name`/`description`/
    # `build-backend`/`requires-python`/`dev`/`line-length` 查无→**判 prune 删掉**）。
    # 删掉 [project].name 与 build-backend 后工程无法构建；某些排布下删除还会切断
    # 字符串字面量令文件不再是合法 TOML —— 而这道闸的存在理由（dep_legality.py:31-33
    # 「坏坐标 = manifest 解析期崩塌会连坐整个工作区」）**正是它自己制造的故障**。
    _PKG_RE = re.compile(
        r'^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)\s*(?:\[[^\]]+\])?\s*'
        r'((?:[<>=~!]+\s*[^\s;]+\s*,?\s*)*)', re.MULTILINE)

    # PEP 508 串（数组元素内）→ 包名 / 版本约束。extras、marker、url 形态在此剥离。
    _SPEC_RE = re.compile(
        r'^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*'
        r'((?:[<>=~!]=?\s*[^\s,;]+\s*,?\s*)*)')

    # TOML **意图**信号（与"是否解析成功"正交）：
    #   ① 独占一行的表头 `[project]` / `[tool.ruff]` —— PEP 508 绝不会让 `[` 开头
    #      （extras 写在包名之后：`pkg[extra]>=1`），故这是无歧义信号；
    #   ② 行首 `键 = 值`（单个 `=`，排除 PEP 508 的 `==`）。
    _TOML_TABLE_RE = re.compile(r'^\s*\[[^\]]+\]\s*$', re.MULTILINE)
    _TOML_KV_RE = re.compile(r'^[ \t]*[A-Za-z_][A-Za-z0-9_.-]*[ \t]*=[ \t]*(?!=)',
                             re.MULTILINE)

    @classmethod
    def _looks_like_toml(cls, text: str) -> bool:
        """文本【意图】是 TOML 吗（不管它是否合法）。

        ★为什么必须与 `_is_pyproject` 分开★：`_is_pyproject` 失败有两种完全不同的原因
        ——「这是 requirements.txt」与「这是**坏掉的** pyproject.toml」。把两者混为一谈
        会让畸形 TOML 落进行正则，**原样复发 W-6**（实测：坏 pyproject 被解析出
        `name` / `dependencies` 两条"依赖"）。兜底路径不能与主判据共用同一个缺口。
        """
        return bool(cls._TOML_TABLE_RE.search(text) or cls._TOML_KV_RE.search(text))

    @classmethod
    def _is_pyproject(cls, text: str) -> bool:
        """能被 tomllib 解析【且】含 pyproject 的权威顶层表之一。"""
        try:
            data = tomllib.loads(text)
        except Exception:
            return False
        return any(k in data for k in ("project", "build-system", "tool"))

    def parse_deps(self, text: str) -> list[dict]:
        if self._is_pyproject(text):
            return self._parse_pyproject(text)
        if self._looks_like_toml(text):
            # 意图是 TOML 但解析不出 ⇒ **绝不**退化成行正则去猜（那正是 W-6 本体）。
            # fail-honest：如实丢弃并留 WARNING（纪律：解析不出→丢弃，绝不臆造）。
            logger.warning(
                "[dep-legality·python] manifest 形似 TOML 但 tomllib 解析失败 → "
                "本轮不处置任何依赖（fail-honest：绝不用行正则把 TOML 键当包名，"
                "那会剪掉 name/description/build-backend 等元数据并毁掉 manifest）")
            return []
        return self._parse_requirements(text)

    def _parse_pyproject(self, text: str) -> list[dict]:
        """tomllib 真解析，**只取** [project].dependencies 与 optional-dependencies 数组元素。

        `block` 取【带引号的数组元素原文】（如 `"flask>=3.0"`）——必须能在原文里唯一定位，
        否则 enforce() 的 remove/rewrite 会命中别处（那是比不解析更坏的结局）。
        同一字面量在文件里出现多次 ⇒ 定位不唯一 ⇒ **丢弃该条**（fail-honest，
        绝不赌它删的是哪一处）。
        """
        try:
            data = tomllib.loads(text)
        except Exception as exc:   # 走到这里说明 _is_pyproject 之后文本变了；防御性
            logger.warning("[dep-legality·python] pyproject.toml 解析失败 → 本轮不处置"
                           "任何依赖（fail-honest，绝不用行正则猜 TOML）: %s", exc)
            return []
        proj = data.get("project")
        if not isinstance(proj, dict):
            return []
        specs: list[str] = []
        _deps = proj.get("dependencies")
        if isinstance(_deps, list):
            specs.extend(s for s in _deps if isinstance(s, str))
        _opt = proj.get("optional-dependencies")
        if isinstance(_opt, dict):
            for _grp in _opt.values():
                if isinstance(_grp, list):
                    specs.extend(s for s in _grp if isinstance(s, str))
        out: list[dict] = []
        seen: set[str] = set()
        for spec in specs:
            if spec in seen:
                continue        # 同一 spec 在多个 group 里重复声明 → 只处置一次
            seen.add(spec)
            # url/vcs 依赖（`pkg @ git+https://...`）：PyPI 里本来就没有，探针必查无
            # ⇒ 会被误剪。整条跳过（与 npm 的 file:/git+ 前缀分档同理）。
            if "@" in spec:
                continue
            m = self._SPEC_RE.match(spec)
            if not m:
                continue
            name = m.group(1).strip()
            ver = (m.group(2) or "").strip().rstrip(",").strip()
            if not name:
                continue
            # 唯一定位：数组元素在原文里的带引号形态
            block = None
            for q in ('"', "'"):
                cand = f"{q}{spec}{q}"
                if text.count(cand) == 1:
                    block = cand
                    break
            if block is None:
                logger.warning("[dep-legality·python] 依赖 %r 在 pyproject.toml 里定位不唯一"
                               "（出现 0 或多次）→ 不处置该条（绝不赌改哪一处）", spec)
                continue
            out.append({"namespace": "", "name": name,
                        "version": ver or None, "block": block})
        return out

    def _parse_requirements(self, text: str) -> list[dict]:
        """requirements.txt：一行一条 PEP 508 串（行正则的**唯一**合法用途）。"""
        out: list[dict] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # pip 指令行（-r/-e/--index-url 等）与 url 依赖：不是可探针的包坐标
            if line.startswith("-") or "@" in line or "://" in line:
                continue
            m = self._SPEC_RE.match(line)
            if not m:
                continue
            name = m.group(1).strip()
            ver = (m.group(2) or "").strip().rstrip(",").strip()
            if not name or name.lower().startswith("http"):
                continue
            if text.count(raw) != 1:
                logger.warning("[dep-legality·python] requirements 行 %r 定位不唯一 → 不处置",
                               line)
                continue
            out.append({"namespace": "", "name": name,
                        "version": ver or None, "block": raw})
        return out

    def managed_names(self, root_text: str) -> set[str]:
        return set()

    def managed_unknown(self, root_text: str) -> bool:
        return False

    def rewrite_namespace(self, block: str, namespace: str) -> str:
        return block

    @staticmethod
    def _unquote(block: str) -> tuple[str, str]:
        """block → (引号, 裸 spec)。pyproject 的 block 带引号，requirements 的不带。"""
        if len(block) >= 2 and block[0] == block[-1] and block[0] in ('"', "'"):
            return block[0], block[1:-1]
        return "", block

    def rewrite_name(self, block: str, name: str) -> str:
        q, spec = self._unquote(block)
        new = re.sub(r'^\s*([A-Za-z0-9][A-Za-z0-9._-]*)', name, spec, count=1)
        return f"{q}{new}{q}"

    def rewrite_version(self, block: str, version: str) -> str:
        q, spec = self._unquote(block)
        new = re.sub(r'([A-Za-z0-9][A-Za-z0-9._-]*.*?)([<>=~!]+\s*[^\s;]+)',
                     rf'\g<1>=={version}', spec, count=1)
        return f"{q}{new}{q}"

    def root_name(self, root_text: str) -> str | None:
        """[project].name —— 走 tomllib，不用行正则。

        行正则有两个坑，都会把错的工程名喂给成员/前缀判定（进而误判 fix_name/prune）：
          ① 命中 `[tool.*]` 下的同名 `name =` 键（取到别人的名字）；
          ② 畸形 TOML 上跨行匹配出垃圾（实测坏文件取到 `unterminated\\ndependencies = [`）。
        故与 parse_deps 用**同一套两层判别**：形似 TOML 而解析不出 → None（不猜）。
        """
        if self._is_pyproject(root_text):
            try:
                proj = tomllib.loads(root_text).get("project")
            except Exception:  # noqa: BLE001 — 解析不出就当没有根名（不猜）
                return None
            if isinstance(proj, dict):
                n = proj.get("name")
                return n if isinstance(n, str) and n else None
            return None
        if self._looks_like_toml(root_text):
            return None      # 坏 TOML：绝不用行正则猜工程名
        # requirements.txt 等非 TOML manifest 本就没有"工程名"概念
        m = re.search(r'^[ \t]*name[ \t]*=[ \t]*["\']([^"\'\n]+)["\']',
                      root_text, re.MULTILINE)
        return m.group(1) if m else None

    def remove(self, text: str, block: str) -> str:
        """删一条依赖。pyproject 的 block 是数组元素 ⇒ 连同其后逗号/换行一起删，
        绝不留下 `[, "b"]` 这种非法 TOML；requirements 的 block 是整行 ⇒ 删行。"""
        q, _spec = self._unquote(block)
        if q:
            # 数组元素：吃掉前导空白 + 元素 + 尾随逗号与空白（含换行）
            pat = r"[ \t]*" + re.escape(block) + r"[ \t]*,?[ \t]*\n?"
            return re.sub(pat, "", text, count=1)
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
