"""R56-5：依赖坐标**单一合法性闸**——把打地鼠收敛成一条**栈中立**的不变量。

## 为什么必须收敛（用户点破：「我们又开始打地鼠了吗？」——是的）

同一个病（LLM 写出**不可能被解析的依赖坐标**），此前按**症状形态**修了四遍：

| 补丁 | 症状形态 | 触发方式 |
|---|---|---|
| R53-2 | 幻影坐标**无 version** | 解析构建工具的报错文本 |
| R54-5 | 版本**跨代** | 解析构建工具的报错文本 |
| R54-6 | 命名空间**编错** | 解析构建工具的报错文本 |
| R56-4 | 幻影坐标**有 version** | 解析构建工具的报错文本 |

四者全是 **error-driven**：等构建工具报出一种新错法，再针对那句错误文本加一条分支。
**换个错法就漏一个** —— 这就是打地鼠的定义。

## 不变量（state-driven，**不看构建工具报什么错**，也**不绑定任何一种栈**）

一条依赖坐标**合法** ⟺ 满足以下三者之一：

1. **工作区成员**：依赖名是本工程自己的模块 → 命名空间必须是工程命名空间（否则确定性改回），
   版本由工作区承接。
   （Maven=reactor module；Cargo=workspace members；Go=go.work/主模块子包；npm=workspaces）
2. **上游受管**：依赖名在工程的集中版本管理处声明 → **不写版本**。
   （Maven=dependencyManagement/BOM；Cargo=`[workspace.dependencies]`；npm=无对应 → 恒 False）
3. **仓库真实存在**：坐标在**权威制品仓库**里有可用版本 → 带可解析的显式版本。
   （Maven=Central；Cargo=crates.io；npm=npm registry；Go=proxy.golang.org）

三条都不满足 = **可证永不可解析** → 确定性处置（能修则修，不能修则剪除 + 响亮日志）。

**为什么坏坐标必须在构建之前死**：缺依赖 = 可归因的**局部编译错**；
坏坐标 = **manifest 解析期崩塌**，会连坐整个工作区（Maven 的 `Could not resolve` 让整棵
reactor 读不出来，于是**每个** worker 的构建闸都报"错在上游模块" → 全员 BLOCKED）。

## fail-open 铁律（比闸本身更重要）

剪除是**不可逆**动作，必须建立在**肯定证据**（仓库确证"没有它"）之上，
**绝不能**建立在**证据缺失**（仓库没连上）之上——否则沙箱一断网就把全工程合法依赖剪光。
`registry_versions` 契约：**不可达返回 `None`**、确证查无返回 `[]`。遇 `None` 一律判合法。
（R56-6：旧取数层两种情况都返回 `[]`，这条铁律形同虚设，已在取数层分开。）

## 多栈扩展方式

栈相关的只有三件事：**怎么解析 manifest / 怎么改写命名空间 / 怎么删一条依赖**——它们是
`ManifestDriver`。不变量（`classify`）与编排（`enforce`）**零栈耦合**。
**新栈 = 注册一个 driver**，不改闸。
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

from swarm.worker.dep_legality_drivers import (
    CargoDriver,
    GoDriver,
    GradleDriver,
    PythonDriver,
    cargo_registry_versions_list,
    go_registry_versions_list,
    python_registry_versions_list,
)

logger = logging.getLogger("swarm.worker.dep_legality")

# 版本值若是构建工具的属性/变量引用（Maven `${...}`、Gradle `$var`）→ 由工程自身承接，不去查仓库。
_VAR_REF_PREFIXES = ("${", "$")


class ManifestDriver(Protocol):
    """一种依赖清单格式的**栈相关**读写能力（不变量本身与栈无关）。"""

    stack: str
    namespace_mandatory: bool
    """D9：本栈依赖声明是否**强制**命名空间非空（Maven groupId 必填，缺失=模型构建期
    必崩连坐 reactor）。True → classify 对"缺命名空间且非成员"走独立 prune 判定，
    绝不落入"registry 不可达 fail-open"。False（如 npm 无 scope 包属常态）→ 不适用。"""

    def parse_deps(self, text: str) -> list[dict]:
        """→ [{namespace, name, version, block}]。`block` 是可在原文里唯一定位的原始片段。
        **不得**包含集中版本管理块里的条目（那是版本表，不是本模块的实际依赖）。"""
        ...

    def managed_names(self, root_text: str) -> set[str]:
        """工程集中版本管理处显式声明的依赖名（本地文本证据，零网络）。"""
        ...

    def managed_unknown(self, root_text: str) -> bool:
        """受管集是否**不完整**（如 Maven import 型 BOM 的传递闭包未拉取）。
        True → 判"缺版本即非法"时必须让路（fail-open）。"""
        ...

    def rewrite_namespace(self, block: str, namespace: str) -> str:
        """把一条依赖的命名空间改写为工程命名空间（Maven=groupId）。"""
        ...

    def rewrite_version(self, block: str, version: str) -> str:
        """把一条依赖的硬编码版本改写为给定版本引用（Maven=<version> 标签）。
        D10：fix_namespace 修好坐标后，内部成员版本必须与 reactor 同步。"""
        ...

    internal_version_ref: str
    """D10：本栈"内部成员版本与 reactor 同步"的引用写法（Maven=`${project.version}`）。"""

    def rewrite_name(self, block: str, name: str) -> str:
        """把一条依赖的名字改写为真工作区成员名（Maven=artifactId）。R57-2。"""
        ...

    def root_name(self, root_text: str) -> str | None:
        """工程根自身的名字（Maven=根 pom 的 artifactId）——用于识别"工程前缀"。"""
        ...

    def remove(self, text: str, block: str) -> str:
        """从 manifest 文本里删除这条依赖（连同其周边空白）。"""
        ...


class MavenDriver:
    """Maven/pom.xml driver（第一个 driver；Cargo/npm/Go 按同协议注册即可接入本闸）。"""

    stack = "maven"
    namespace_mandatory = True   # D9：<dependency> 缺 groupId 必崩模型构建

    @staticmethod
    def _strip(text: str) -> str:
        return re.sub(r"<!--.*?-->", "", text, flags=re.S)

    @staticmethod
    def _skip_spans(text: str) -> list[tuple[int, int]]:
        """必须跳过的区间：注释（里面的 <dependency> 是被注释掉的，不是真依赖）+
        dependencyManagement（那是"版本表"不是本模块依赖，当依赖校验会误剪）+
        build（D14：<build><plugins> 内 plugin 自己的 <dependency> 是插件依赖，不是
        工程依赖——参与判定会按工程坐标规则误剪/误改插件声明）。"""
        spans = [(m.start(), m.end()) for m in re.finditer(r"<!--.*?-->", text, re.S)]
        spans += [(m.start(), m.end()) for m in
                  re.finditer(r"<dependencyManagement>.*?</dependencyManagement>", text, re.S)]
        spans += [(m.start(), m.end()) for m in
                  re.finditer(r"<build>.*?</build>", text, re.S)]
        return spans

    def parse_deps(self, text: str) -> list[dict]:
        """★在**原文**上定位依赖块★：`block` 必须能在原文里唯一定位，否则 enforce 改不动它。
        （旧实现在"去注释副本"上切块 → 块内含行内注释时，block 在原文里根本找不到 → 判了却改不了。）
        字段解析时才去注释/exclusions。"""
        spans = self._skip_spans(text)
        out: list[dict] = []
        for blk in re.finditer(r"<dependency>.*?</dependency>", text, re.S):
            if any(s <= blk.start() < e for s, e in spans):
                continue   # 落在注释里 / 受管版本表里 → 不是本模块的真实依赖
            inner = self._strip(blk.group(0))
            inner = re.sub(r"<exclusions>.*?</exclusions>", "", inner, flags=re.S)
            g = re.search(r"<groupId>\s*([^<\s]+)\s*</groupId>", inner)
            a = re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", inner)
            v = re.search(r"<version>\s*([^<]+?)\s*</version>", inner)
            if not a:
                continue
            out.append({
                "namespace": g.group(1) if g else "",
                "name": a.group(1),
                "version": v.group(1).strip() if v else None,
                "block": blk.group(0),
            })
        return out

    def _managed_block(self, root_text: str) -> str:
        m = re.search(r"<dependencyManagement>(.*?)</dependencyManagement>",
                      self._strip(root_text), re.S)
        return m.group(1) if m else ""

    def managed_names(self, root_text: str) -> set[str]:
        return set(re.findall(r"<artifactId>\s*([^<\s]+)\s*</artifactId>",
                              self._managed_block(root_text)))

    def managed_unknown(self, root_text: str) -> bool:
        # import 型 BOM → 传递受管集未拉取 → 受管集不完整 → 缺版本不敢判非法
        return bool(re.search(r"<scope>\s*import\s*</scope>", self._managed_block(root_text)))

    def rewrite_namespace(self, block: str, namespace: str) -> str:
        if re.search(r"<groupId>", block):
            return re.sub(r"(<groupId>\s*)[^<\s]+(\s*</groupId>)",
                          rf"\g<1>{namespace}\g<2>", block, count=1)
        # 无 groupId（少见）→ 补一个，绝不留空命名空间
        return block.replace("<dependency>", f"<dependency><groupId>{namespace}</groupId>", 1)

    def rewrite_name(self, block: str, name: str) -> str:
        return re.sub(r"(<artifactId>\s*)[^<\s]+(\s*</artifactId>)",
                      rf"\g<1>{name}\g<2>", block, count=1)

    internal_version_ref = "${project.version}"   # D10：内部成员版本与 reactor 同步

    def rewrite_version(self, block: str, version: str) -> str:
        # 仅替换既有 <version> 标签；无标签=版本受管上游，不动（调用方只在确有硬编码版本时调）
        return re.sub(r"(<version>\s*)[^<]+?(\s*</version>)",
                      rf"\g<1>{version}\g<2>", block, count=1)

    def root_name(self, root_text: str) -> str | None:
        """根 pom 自身 artifactId（剥掉 parent / 依赖 / 受管块后的第一个）。"""
        body = self._strip(root_text)
        body = re.sub(r"<parent>.*?</parent>", "", body, flags=re.S)
        body = re.sub(r"<dependencyManagement>.*?</dependencyManagement>", "", body, flags=re.S)
        body = re.sub(r"<dependencies>.*?</dependencies>", "", body, flags=re.S)
        body = re.sub(r"<build>.*?</build>", "", body, flags=re.S)
        m = re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", body)
        return m.group(1) if m else None

    def remove(self, text: str, block: str) -> str:
        return re.sub(r"[ \t]*" + re.escape(block) + r"\s*\n?", "", text, count=1)


class NpmDriver:
    """npm/package.json driver（X-M10，27 号文 §3.2）。

    ★复用不变量 ≠ 复用消费契约（血规 10③）——npm 与 Maven 的三处分档★：
      ① namespace=scope，但**调用方必须传 namespace=None**：`@scope` 不是工程命名空间。
        Maven 的「工程模块从不在远程仓库」在 npm **不成立**——scoped 包可以合法发布在
        registry（私有/公开）。若按 Maven 契约传 namespace，规则②（用工程命名空间但非
        成员 → 直接 prune，不查仓库）会**误剪合法已发布 scoped 包**。幻影 scoped 依赖
        由规则④的 registry 探针兜底（E404 确证查无才剪）。
      ② 版本值是 semver range——classify 只验「包存在」（仓库有任何版本），**不验 range
        可满足性**：`^99.0.0` 判 legal 是刻意边界（npm install 的 ETARGET 是可归因局部错，
        不会像坏坐标那样 manifest 解析期连坐全工作区）。
      ③ 无集中版本管理（overrides/resolutions 是覆盖表不是 BOM）→ managed_names 恒空、
        managed_unknown 恒 False（缺版本即非法的判定在 npm 不适用——npm 依赖必带 range）。
    """

    stack = "npm"
    namespace_mandatory = False   # npm 无 scope 包属常态（D9 不适用）

    # 版本值由工程自身承接、无需查仓库的前缀（registry 里本来就没有，查=必 E404=误剪）
    self_hosted_prefixes = ("${", "$", "file:", "link:", "git+", "git:",
                            "http://", "https://", "workspace:", "portal:")

    internal_version_ref = "*"   # npm workspaces 内部引用：星号 range 由 workspaces 链接承接
    # npm 的名字即完整坐标（无 scope 包是常态）——空 namespace 也要送探针；
    # Maven 坐标须 group:artifact 成对，缺 groupId 不查（D9）。两概念两档，不混用。
    probe_without_namespace = True

    _SECTIONS = ("dependencies", "devDependencies", "optionalDependencies")
    _ENTRY_RE = re.compile(r'"((?:@[\w.\-]+/)?[\w.\-]+)"\s*:\s*"([^"]+)"')
    _KEY_RE = re.compile(r'"((?:@[\w.\-]+/)?[\w.\-]+)"(\s*:)')

    @classmethod
    def _section_spans(cls, text: str) -> list[tuple[int, int]]:
        """各 dependencies 段的【花括号内】区间。花括号配平且尊重字符串字面量
        （段内值都是字符串，但字符串里可能含 `{`/`}`/`\\"`——不识别字符串会配错平）。"""
        spans: list[tuple[int, int]] = []
        for sec in cls._SECTIONS:
            m = re.search(rf'"{sec}"\s*:\s*\{{', text)
            if not m:
                continue
            depth, i = 1, m.end()
            in_str = esc = False
            while i < len(text) and depth:
                c = text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                elif c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                i += 1
            if depth == 0:
                spans.append((m.end(), i - 1))
        return spans

    def parse_deps(self, text: str) -> list[dict]:
        """→ [{namespace=scope(可空), name=含 scope 全名, version=range 原文, block}]。

        ★block 带逗号上下文★：JSON 删条目必须连逗号一起处置——尾随有逗号 → 带上；
        无（末条目）→ 带【前导】逗号。否则 remove 后留下 `, }` / `{ ,` = 非法 JSON，
        修依赖修出 manifest 解析崩塌（与坏坐标同罪）。
        已知边界（诚实登记）：同名同版本条目同时出现在 dependencies 与 devDependencies
        时 block 文本相同 → enforce 只处置首个、第二个按「已被带走」跳过（日志可辨）。
        """
        out: list[dict] = []
        for s, e in self._section_spans(text):
            seg = text[s:e]
            for m in self._ENTRY_RE.finditer(seg):
                name, ver = m.group(1), m.group(2)
                a_start, a_end = s + m.start(), s + m.end()
                tm = re.match(r"\s*,", text[a_end:a_end + 64])
                if tm:
                    block = text[a_start:a_end + tm.end()]
                else:
                    pm = re.search(r",\s*$", text[:a_start])
                    block = (text[pm.start():a_end] if pm
                             else text[a_start:a_end])
                ns = name.split("/", 1)[0] if name.startswith("@") and "/" in name else ""
                out.append({"namespace": ns, "name": name,
                            "version": ver.strip(), "block": block})
        return out

    def managed_names(self, root_text: str) -> set[str]:
        return set()   # npm 无 BOM 对应物（分档③）

    def managed_unknown(self, root_text: str) -> bool:
        return False

    def rewrite_namespace(self, block: str, namespace: str) -> str:
        # 替换 key 的 scope 段（调用方传 namespace=None 时本路径不会被 classify 触发）
        return re.sub(r'"@[\w.\-]+/', f'"{namespace}/', block, count=1)

    def rewrite_name(self, block: str, name: str) -> str:
        return self._KEY_RE.sub(rf'"{name}"\g<2>', block, count=1)

    def rewrite_version(self, block: str, version: str) -> str:
        return re.sub(r'(:\s*")[^"]+(")', rf"\g<1>{version}\g<2>", block, count=1)

    def root_name(self, root_text: str) -> str | None:
        m = re.search(r'"name"\s*:\s*"([^"]+)"', root_text)
        return m.group(1) if m else None

    def remove(self, text: str, block: str) -> str:
        # block 已含逗号上下文（parse_deps 契约）——连同周边空白删，JSON 仍然合法
        return re.sub(r"[ \t]*" + re.escape(block) + r"\s*\n?", "", text, count=1)


DRIVERS: dict[str, ManifestDriver] = {
    "maven": MavenDriver(),
    "npm": NpmDriver(),
    "cargo": CargoDriver(),
    "go": GoDriver(),
    "gradle": GradleDriver(),
    "python": PythonDriver(),
}
"""栈 → driver。**新栈 = 在此注册一个 driver**（实现 ManifestDriver 协议），闸本身不动。"""


_driver_absent_warned: set[str] = set()


def driver_for(stack: str) -> ManifestDriver | None:
    """按栈取 driver；没有 driver 的栈 → None（调用方跳过本闸，绝不用别的栈的规则硬套）。
    D14：None 路径 warn-once——无 driver=本闸对该栈零覆盖，静默跳过会与"已校验"混同。"""
    key = (stack or "").strip().lower()
    drv = DRIVERS.get(key)
    if drv is None and key and key not in _driver_absent_warned:
        _driver_absent_warned.add(key)
        logger.warning("[dep-legality] 栈 '%s' 无注册 driver → 合法性闸对该栈零覆盖（跳过，非同已校验）", key)
    return drv


def _resolve_prefixed_member(name: str, workspace_members: set[str],
                             root_name: str | None) -> str | None:
    """R57-2：名字写错成「工程前缀 + 真成员名」时，**唯一**地还原出真成员；有歧义则 None。

    round57 实锤：LLM 把 5 个兄弟模块全写成 `ruoyi-alarm-core` / `ruoyi-alarm-engine`…
    （工程根 artifactId 就叫 `ruoyi`）。它们**全是真实工作区成员**，只是多了个前缀。
    旧闸只会 prune → alarm-web 失去全部兄弟依赖 → 编译期满屏 cannot-find-symbol。

    ★铁律★ 只接受**零歧义**的还原：剥掉前缀后**恰好命中一个**成员才改名；
    命中 0 个或 >1 个 → 返回 None（交回 prune）。**绝不猜**——接错模块比缺依赖更毒。
    """
    if not name:
        return None
    cands = {m for m in workspace_members if m and m != name and name.endswith(f"-{m}")}
    if root_name:
        # 最强证据：前缀恰好是工程根名（`{root}-{member}`）
        strict = {m for m in cands if name == f"{root_name}-{m}"}
        if len(strict) == 1:
            return next(iter(strict))
    # ★DR-05-F4(#90) 整改★：删除宽松单候选兜底——它只保证"候选数=1"，不保证被剥掉的前缀真是
    # 工程前缀。外部依赖 `jackson-core`/`spring-core`（endswith `-core`）在 workspace 有 `core`
    # 成员时会被误改名成内部坐标（groupId 仍 com.fasterxml.jackson.core）→ 既非真外部又非合法内部
    # → manifest 解析崩塌连坐 reactor（违"绝不猜"铁律，注释亦自称"只接受零歧义还原"）。只认
    # `{root}-{member}` 强证据，无则 None（交回 prune/上游，误接错模块比缺依赖更毒）。
    return None


def classify(dep: dict, *, namespace: str | None, workspace_members: set[str],
             managed: set[str], managed_unknown: bool,
             registry_versions, root_name: str | None = None,
             namespace_mandatory: bool = False,
             self_hosted_prefixes: tuple[str, ...] = _VAR_REF_PREFIXES,
             probe_without_namespace: bool = False) -> tuple[str, str]:
    """判定一条依赖的合法性 → (verdict, reason)。**纯函数，零栈耦合。**

    verdict ∈ {legal, fix_namespace, fix_name, prune}
      · legal          —— 满足三条之一，不动
      · fix_namespace  —— 名字是工作区成员，命名空间却写成外部的 → 改回工程命名空间
      · fix_name       —— 名字是「工程前缀 + 真成员」→ 确定性改名到真成员（R57-2）
      · prune          —— **可证永不可解析** → 剪除

    ★处置优先级铁律★ **剪除是最后手段，能修则修**。合法性闸此前只有 prune 一条出路，
    把"可确定性修复的名字错"降级成了"不可逆的删依赖"（round57 实锤：一次剪光 5 条真兄弟依赖）。

    registry_versions(namespace, name) -> list[str] | None
      · list  —— 仓库**确证**答复：这些是可用版本（空列表 = 确证"查无此物"）
      · None  —— 仓库**没连上**（网络/工具故障）→ **一律 fail-open 判 legal**
      ★证据缺失 ≠ 否定证据★ 误剪一条合法依赖 ≫ 漏过一条坏坐标（后者下游还有闸，前者直接毁产物）。

    self_hosted_prefixes（X-M10）：版本值由【工程自身承接】、无需查仓库的前缀——
    Maven 是 `${...}` 属性引用；npm 还有 `file:`/`link:`/`git+:`/`workspace:` 等本地/
    协议引用（这类 dep 在 registry 里**本来就没有**，拿探针查=必 E404=误剪合法依赖）。
    """
    ns, name, ver = dep["namespace"], dep["name"], dep["version"]

    # ★fail-open 保险★ 工作区成员集为空 = 根本没读到任何 manifest（读失败/树异常），
    # 此时"不是工作区成员"是**证据缺失**而非否定证据 → 规则②会无条件剪除真兄弟依赖 → 一律放行。
    if not workspace_members:
        return "legal", "工作区成员集为空（未能取证）→ fail-open，绝不据此剪除"

    # ① 工作区成员：名字是本工程自己的模块
    if name in workspace_members:
        if namespace and ns and ns != namespace:
            return "fix_namespace", (f"{name} 是工作区内部模块，命名空间却写成外部 '{ns}'"
                                     f"（工程模块从不在远程仓库里，此坐标永远拉不到）")
        # D9：成员缺命名空间（本栈强制非空）→ 确定性补工程命名空间
        # （rewrite_namespace 已支持"无 groupId 补一个"）；工程命名空间未知则无法确定性修，
        # 留 legal（不猜——但此形态 Maven 解析期仍会崩，属诚实边界）。
        if namespace and not ns and namespace_mandatory:
            return "fix_namespace", (f"{name} 是工作区内部模块却缺命名空间（本栈强制非空，"
                                     f"manifest 解析期即崩）→ 确定性补工程命名空间")
        return "legal", "工作区成员"

    # ②a 名字写错成「工程前缀 + 真成员」（R57-2）→ 确定性改名（**优先于剪除**：能修则修）
    _real = _resolve_prefixed_member(name, workspace_members, root_name)
    if _real:
        return "fix_name", (f"{name} 不是工作区成员，但剥掉工程前缀后**唯一**命中真成员 "
                            f"'{_real}'（意图明确、零歧义）→ 确定性改名，绝不剪除真兄弟依赖")

    # ② 用了工程命名空间、却不是工作区成员 → 幻影模块（不论有无版本，仓库里永远没有它）
    if namespace and ns == namespace:
        return "prune", (f"用工程命名空间 '{ns}' 但 {name} 不是工作区成员"
                         f"（工程模块从不在远程仓库里）→ 可证永不可解析")

    # ②b（D9）命名空间缺失且本栈强制非空 → 独立判定，绝不落入 ③/④ 的
    # "registry_versions(ns, name) if ns else None → 仓库不可达 fail-open"：
    # 缺 groupId 是 Maven 模型构建期必崩（'dependencies.dependency.groupId is missing'
    # 连坐整棵 reactor）——本闸自宣要杀的正是这类，却在 ③/④ 系统性放行。非成员 →
    # 坐标无法确定性补全（纪律：绝不猜依赖坐标）→ 可证永不可解析。成员已在 ① 修复。
    if namespace_mandatory and not ns:
        return "prune", (f"缺命名空间（本栈强制非空，manifest 解析期即崩）且 {name} "
                         f"非工作区成员，坐标不可猜 → 可证永不可解析")

    # ③ 无版本：必须被上游集中管理
    if ver is None:
        if name in managed or managed_unknown:
            return "legal", "上游受管（或受管集未知 → fail-open 不误判）"
        vers = (registry_versions(ns, name)
                   if (ns or probe_without_namespace) else None)
        if vers is None:
            return "legal", "仓库不可达 → fail-open（宁可放行，绝不误剪）"
        if not vers:
            return "prune", f"{ns}:{name} 仓库确证查无任何版本，且上游不受管 → 可证永不可解析"
        # 仓库有，但上游不管它 → 缺版本会让 manifest 解析期就炸（比缺依赖严重一个数量级）
        return "legal", "仓库存在但上游不受管 → 交 version-repair 注入显式版本"

    # ④ 有版本：变量引用/协议引用交由工程自身承接（X-M10：前缀表随 driver 分档——
    # npm 的 file:/git+/workspace: 在 registry 里本来就没有，拿探针查=必 E404=误剪）
    if ver.startswith(self_hosted_prefixes):
        return "legal", "版本走属性/变量/协议引用"

    vers = (registry_versions(ns, name)
                   if (ns or probe_without_namespace) else None)
    if vers is None:
        return "legal", "仓库不可达 → fail-open"
    if not vers:
        return "prune", f"{ns}:{name} 仓库确证查无任何版本 → 可证永不可解析"
    return "legal", "仓库存在"


def enforce(manifest_texts: dict[str, str], *, root_text: str, namespace: str | None,
            workspace_members: set[str], registry_versions,
            driver: ManifestDriver, root_name: str | None = None,
            ) -> tuple[dict[str, str], list[str]]:
    """对全工作区 manifest 施加不变量 → (改写后的文本, 处置说明)。纯函数：可确定性单测、可离线。

    ★处置必须**真的落到文本上**★：改写/删除若没命中（正则失配、块重复），**绝不**登记为已处置——
    否则上层以为修了、实则原样进构建（静默失败：宣称成功、实际每次失败）。
    driver 必填（D14）：缺省落 maven 是地雷——忘传 driver 的非 maven 工程会被 maven 规则硬套。
    """
    drv = driver
    managed = drv.managed_names(root_text)
    managed_unknown = drv.managed_unknown(root_text)
    if root_name is None and hasattr(drv, "root_name"):
        root_name = drv.root_name(root_text)   # 工程根名自证（识别"工程前缀 + 真成员"的错名）
    _prefixes = getattr(drv, "self_hosted_prefixes", _VAR_REF_PREFIXES)  # X-M10：随栈分档
    _probe_no_ns = getattr(drv, "probe_without_namespace", False)    # 同上
    new_texts: dict[str, str] = {}
    actions: list[str] = []

    for rel, text in manifest_texts.items():
        cur = text
        for dep in drv.parse_deps(text):
            verdict, why = classify(
                dep, namespace=namespace, workspace_members=workspace_members,
                managed=managed, managed_unknown=managed_unknown,
                registry_versions=registry_versions, root_name=root_name,
                namespace_mandatory=getattr(drv, "namespace_mandatory", False),
                self_hosted_prefixes=_prefixes,
                probe_without_namespace=_probe_no_ns)
            if verdict == "legal":
                continue
            blk = dep["block"]
            if blk not in cur:
                if blk not in text:
                    # 块在原文里就定位不到（如块内含注释——解析走的是去注释副本）→ **响亮报告**：
                    # 判出了问题却动不了手，必须留下痕迹，否则运维只看到构建反复同一个错、毫无线索。
                    logger.warning(
                        "[dep-legality] %s: %s:%s 判为 %s，但该依赖块在原文里定位不到"
                        "（块内含注释？）→ 未处置（如实报告，绝不谎报已修）",
                        rel, dep["namespace"], dep["name"], verdict)
                # 否则：同名块已被前一条处置带走（同一 manifest 内重复声明）→ 静默跳过是对的
                continue
            before = cur
            if verdict == "fix_namespace" and namespace:
                _new_blk = drv.rewrite_namespace(blk, namespace)
                # D10：三面同步——坐标修回工程命名空间后，硬编码版本（臆造/漂移）仍会让
                # reactor 解析不到 → 同步为工程版本引用；变量引用/受管无版本不动。
                _ver = dep.get("version")
                if _ver and not _ver.startswith(_prefixes):
                    _ref = getattr(drv, "internal_version_ref", None)
                    if _ref:
                        _new_blk = drv.rewrite_version(_new_blk, _ref)
                cur = cur.replace(blk, _new_blk, 1)
            elif verdict == "fix_name":
                _real = _resolve_prefixed_member(dep["name"], workspace_members, root_name)
                if not _real:      # 理论上不可能（classify 刚判过）——但绝不盲改
                    continue
                _new_blk = drv.rewrite_name(blk, _real)
                # 批次6 R1（reviewer HIGH）：与 fix_namespace 对称——名字修回真成员后，
                # 残留的硬编码外部版本同样让 reactor 解析失败（修对名字留下错版本=
                # 同根病的另一半）→ 同步为工程版本引用；变量引用/受管无版本不动。
                _ver = dep.get("version")
                if _ver and not _ver.startswith(_prefixes):
                    _ref = getattr(drv, "internal_version_ref", None)
                    if _ref:
                        _new_blk = drv.rewrite_version(_new_blk, _ref)
                cur = cur.replace(blk, _new_blk, 1)
            elif verdict == "prune":
                cur = drv.remove(cur, blk)
            if cur == before:
                # 没能真的改动文本 → 如实响亮报告，绝不静默当成"已修复"
                logger.warning("[dep-legality] %s: %s:%s 判为 %s 但改写未命中文本（跳过，不谎报）",
                               rel, dep["namespace"], dep["name"], verdict)
                continue
            actions.append(f"[{verdict}] {rel}: {dep['namespace']}:{dep['name']}"
                           f"{':' + dep['version'] if dep['version'] else ''}（{why}）")
        if cur != text:
            new_texts[rel] = cur
    return new_texts, actions


# 兼容旧调用点/测试：Maven 的 manifest 解析（新代码请走 driver）
def parse_deps(text: str) -> list[dict]:
    return DRIVERS["maven"].parse_deps(text)
