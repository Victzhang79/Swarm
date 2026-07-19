"""L1 修复面的【纯解析/纯文本重写】叶簇（god-file 轻拆，从 l1_pipeline.py 抽出）。

只含无副作用、可单测、不碰磁盘/沙箱/网络的纯函数：编译输出解析（缺包/缺 artifact/缺 version）、
版本号可比较键、最近有效版本选择、JVM 命名空间与 pom 版本的块级文本重写。这些函数原散在 3237 行
的 l1_pipeline.py 里，与大量沙箱命令逻辑混杂——抽成叶模块（【不反向 import】l1_pipeline）后单元
可寻址、可读性升。l1_pipeline 末尾 re-export 全部符号，既有 `from ...l1_pipeline import <fn>`
调用点（executor_sync / 测试）零改动。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


# ── JVM 命名空间确定性重写（jakarta ↔ javax） ──
_JAKARTA_MOVED_PREFIXES: tuple[str, ...] = (
    "servlet", "persistence", "validation", "ws.rs", "websocket", "ejb",
    "enterprise", "inject", "faces", "jms", "mail", "jws", "batch", "el",
    "interceptor", "decorator", "xml.bind", "xml.soap", "xml.ws", "json",
)
# javax.annotation.* 仅这些符号迁到 jakarta.annotation；javax.annotation.processing 留在 JDK。
_JAKARTA_MOVED_EXACT: tuple[str, ...] = (
    "annotation.Resource", "annotation.Resources", "annotation.PostConstruct",
    "annotation.PreDestroy", "annotation.Priority", "annotation.Generated",
    "annotation.ManagedBean", "annotation.security.RolesAllowed",
    "annotation.security.PermitAll", "annotation.security.DenyAll",
    "annotation.security.DeclareRoles", "annotation.security.RunAs",
    "annotation.sql.DataSourceDefinition",
)


def rewrite_jvm_namespace(text: str, target_ns: str) -> tuple[str, int]:
    """把【写错的】Jakarta EE 命名空间确定性改成项目真实命名空间。

    target_ns='jakarta'（项目是 Spring Boot ≥3）→ 把 javax.{moved} 改成 jakarta.{moved}；
    target_ns='javax'（项目是 Spring Boot 2.x）→ 反向。其余 javax.*（JDK 自带）一律不动。
    替换的是【点号包前缀】，import 与全限定用法一并覆盖（比只改 import 更稳）。
    返回 (新文本, 改动次数)。纯函数、可单测、不碰磁盘。
    """
    if target_ns not in ("jakarta", "javax") or not text:
        return text, 0
    other = "javax" if target_ns == "jakarta" else "jakarta"
    n = 0
    for suf in _JAKARTA_MOVED_PREFIXES + _JAKARTA_MOVED_EXACT:
        a = f"{other}.{suf}"
        if a in text:
            n += text.count(a)
            text = text.replace(a, f"{target_ns}.{suf}")
    return text, n


# ── 编译输出解析：缺包 / 缺 artifact / 缺 version ──
_MISSING_PKG_RE = re.compile(
    r"([^\s\[]+\.java):\[\d+,\d+\]\s*package\s+([\w.]+)\s+does not exist"
)


def parse_missing_packages(build_output: str) -> list[tuple[str, str]]:
    """从编译输出解析 (出错文件, 不存在的包) 对，去重保序。纯函数、可单测。"""
    if not build_output:
        return []
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for m in _MISSING_PKG_RE.finditer(build_output):
        key = (m.group(1), m.group(2))
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


_MISSING_ARTIFACT_RE = re.compile(
    r"(?:Could not find artifact|Failure to find|Could not resolve dependencies for[^\n]*?)\s*"
    r"([A-Za-z0-9_.\-]+):([A-Za-z0-9_.\-]+):(?:jar|pom|war)"
    # ★R65E8-T3★ 可选 classifier（Maven 坐标 g:a:type[:classifier]:version）——跳过它取**真正的**
    # version。旧实现无此段 → `shiro-ehcache:jar:jakarta:2.0.1` 把 classifier `jakarta` 误当 version
    # 捕获 → version-repair 拿 bad_ver="jakarta" 全程 no-op、classifier 永不被剔（round65e8 死因）。
    # 贪婪回溯保证无 classifier 的普通形态 `jar:2.0.1` 仍正确取 version=2.0.1。
    r"(?::[A-Za-z0-9_.\-]+)?"
    r":([A-Za-z0-9_.\-]+)"
)


# ★R65E8-T3★ 另一半：**带 classifier** 的缺失坐标，专门提取 classifier 供确定性剔除。
# 只认「Could not find artifact / Failure to find」两种明确 artifact-解析形态；version 段须以数字
# 起（与 classifier 消歧：`jar:classifier:version` 两段 vs `jar:version` 一段）。
_MISSING_CLASSIFIED_ARTIFACT_RE = re.compile(
    r"(?:Could not find artifact|Failure to find)\s*"
    r"([A-Za-z0-9_.\-]+):([A-Za-z0-9_.\-]+):(?:jar|pom|war):"   # 与分支① version-repair 打包型对齐
    r"([A-Za-z0-9_.\-]+):"          # classifier（如 jakarta / native）
    r"([0-9][A-Za-z0-9_.\-]*)"      # version（须以数字起，消歧）
)


def _ver_key(v: str) -> tuple:
    """版本号 → 可比较元组（数字段按整数比，非数字段按字符串），用于版本排序/取最近。"""
    parts = re.split(r"[.\-_]", v.strip())
    key: list = []
    for p in parts:
        if p.isdigit():
            key.append((1, int(p), ""))
        else:
            m = re.match(r"(\d+)(.*)", p)
            if m:
                key.append((1, int(m.group(1)), m.group(2)))
            else:
                key.append((0, 0, p))
    return tuple(key)


def parse_missing_artifacts(build_output: str) -> list[tuple[str, str, str]]:
    """从 mvn build 输出解析【拉取不到的 artifact】=(groupId, artifactId, version)，去重保序。"""
    seen: set[tuple[str, str, str]] = set()
    out: list[tuple[str, str, str]] = []
    for g, a, v in _MISSING_ARTIFACT_RE.findall(build_output or ""):
        key = (g, a, v)
        if key not in seen and g and a and v:
            seen.add(key)
            out.append(key)
    return out


# 另一类形态：模型给依赖【根本没写 <version>】且父 dependencyManagement 也不管它 →
# `'dependencies.dependency.version' for G:A:jar is missing`（pom 解析期错，早于 artifact 解析）。
_MISSING_VERSION_RE = re.compile(
    r"'dependencies\.dependency\.version' for "
    r"([A-Za-z0-9_.\-]+):([A-Za-z0-9_.\-]+):(?:jar|pom|war|zip|maven-plugin|ejb|bundle)"
    r"\b[^\n]*?\bis missing"
)


def parse_missing_versions(build_output: str) -> list[tuple[str, str]]:
    """解析【缺 <version> 元素】的依赖 =(groupId, artifactId)，去重保序。纯函数、可单测。"""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for g, a in _MISSING_VERSION_RE.findall(build_output or ""):
        if (g, a) not in seen and g and a:
            seen.add((g, a))
            out.append((g, a))
    return out


def parse_missing_classified_artifacts(
    build_output: str,
) -> list[tuple[str, str, str, str]]:
    """解析【带 classifier 且拉不到】的坐标 =(groupId, artifactId, classifier, version)，去重保序。

    R65E8-T3：worker 给依赖写了仓库里不存在的分类变体（如 shiro-ehcache 的幻觉
    `jakarta` classifier）→ `Could not find artifact g:a:jar:jakarta:2.0.1`。纯函数、可单测。
    """
    seen: set[tuple[str, str, str, str]] = set()
    out: list[tuple[str, str, str, str]] = []
    for g, a, c, v in _MISSING_CLASSIFIED_ARTIFACT_RE.findall(build_output or ""):
        key = (g, a, c, v)
        if key not in seen and all(key):
            seen.add(key)
            out.append(key)
    return out


# R53-3（round49b/50/50b/51/52 **五轮**复现）：注入版本必须优先落在**稳定版**上。
# 旧实现一律 max(available)，而 maven-metadata 的最高版常是里程碑/预发布：实测注入过
# spring-boot-starter-aop:4.0.0-M2、shiro-core:3.0.0-alpha-1、commons-collections4:4.5.0-M3、
# spring-security-core:7.1.0-RC1 —— 与工程基线（RuoYi 4.8.3 / Spring Boot 2.x 系）根本不兼容，
# L1 侧"修好了"，L2 集成期真炸；更毒的是对抗复核随后把这些版本算成 **worker 擅自硬编码**，
# worker 依言删掉 → version-repair 立刻再注入 → 反复打回死循环（round50b 实锤三轮）。
# 稳定版一个都没有时才退回预发布（那是该 artifact 的真实现状，不是我们瞎选）。
_PRERELEASE_RE = re.compile(
    r"(?i)(?:^|[.\-_])(?:snapshot|alpha|beta|rc\d*|m\d+|cr\d+|ea|preview|pre|dev)(?:[.\-_]|\d|$)")


def stable_versions(available: list[str]) -> list[str]:
    """过滤掉预发布/里程碑版本；全是预发布则原样返回（不制造空集）。"""
    stable = [v for v in available if not _PRERELEASE_RE.search(v)]
    return stable or list(available)


def pick_latest_stable(available: list[str]) -> str | None:
    """取最新**稳定**版（version-repair 注入缺失 <version> 的唯一入口）。"""
    pool = stable_versions(available)
    return max(pool, key=_ver_key) if pool else None


def _choose_valid_version(bad: str, available: list[str]) -> str | None:
    """选最近的有效版本：≤目标的最高**稳定**版；若无（目标比所有都低）→ 最高稳定版。"""
    if not available or bad in available:
        return None
    pool = stable_versions(available)
    bk = _ver_key(bad)
    le = [v for v in pool if _ver_key(v) <= bk]
    pick = max(le, key=_ver_key) if le else max(pool, key=_ver_key)
    return pick if pick != bad else None


# ── D32 治本：版本校正只允许发生在【声明目标 artifactId 的 <dependency> 块】内 ──
# 旧实现对"含该版本字符串的任何 pom"的所有含 version 字样的行做 `s#>bad<#>good<#g` 全局串
# 替换——模型给第三方依赖顺手写了项目自身版本号时，根/模块 pom 的 project/parent <version>
# 会被连坐改成第三方版本 → reactor 内部依赖解析崩，且损坏经 repaired_file_paths 持久化。
# 现改为纯函数块级重写：<dependency>…</dependency>（含 dependencyManagement 内嵌套的同名块）
# 且声明该 artifactId 的块内的字面 <version> 才改；版本经 ${prop} 属性引用 → 返回属性名由调
# 用方去【属性定义标签】处校正（仅该标签）；Maven 保留属性（project.*/parent.*/revision 等
# =项目自身版本）绝不校正（fail-closed）。
_DEP_BLOCK_RE = re.compile(r"<dependency>.*?</dependency>", re.DOTALL)
_DEP_VERSION_PROP_RE = re.compile(r"<version>\s*\$\{([^}]+)\}\s*</version>")
# CI-friendly versions（revision/sha1/changelist）与 project.*/parent.*/pom.* 都指向项目自身
# 版本坐标——校正它们等于改项目版本，属 D32 要杜绝的越界。
_MAVEN_RESERVED_PROP_PREFIXES = ("project.", "parent.", "pom.")
_MAVEN_RESERVED_PROPS = frozenset({"revision", "sha1", "changelist", "version"})


def _is_reserved_maven_property(prop: str) -> bool:
    p = (prop or "").strip()
    return p.startswith(_MAVEN_RESERVED_PROP_PREFIXES) or p in _MAVEN_RESERVED_PROPS


def rewrite_dependency_version(
    pom_text: str, artifact: str, bad_ver: str, good_ver: str
) -> tuple[str, list[str]]:
    """在 pom 文本内做【块级】版本校正（纯函数，可单测）。

    只有声明 `artifact` 的 <dependency> 块内的字面 <version>bad</version> 被改成 good；
    project/parent/其它依赖的 version 标签一个字符不动。块内版本写作 ${prop} → 不改块本身，
    把属性名收进返回值交调用方去属性定义处校正；保留属性（项目自身版本）不返回。
    返回 (新文本, 需要在属性定义处校正的属性名列表)。
    """
    props: list[str] = []
    art_re = re.compile(r"<artifactId>\s*" + re.escape(artifact) + r"\s*</artifactId>")
    ver_re = re.compile(r"(<version>\s*)" + re.escape(bad_ver) + r"(\s*</version>)")

    def _sub(m: re.Match) -> str:
        block = m.group(0)
        if not art_re.search(block):
            return block
        pm = _DEP_VERSION_PROP_RE.search(block)
        if pm:
            prop = pm.group(1).strip()
            if _is_reserved_maven_property(prop):
                # ${project.version}/${revision} 等 = 项目自身版本，绝不校正（fail-closed）
                logger.warning(
                    "[L1.2.1·version-repair] 依赖 %s 的版本引用 Maven 保留属性 ${%s}"
                    "（项目自身版本坐标）→ 拒绝校正，交上层防线处理", artifact, prop,
                )
            else:
                props.append(prop)
            return block
        return ver_re.sub(lambda vm: vm.group(1) + good_ver + vm.group(2), block)

    return _DEP_BLOCK_RE.sub(_sub, pom_text), props


def rewrite_property_version(pom_text: str, prop: str, bad_ver: str, good_ver: str) -> str:
    """把 <prop>bad</prop> 属性定义校正为 good——只碰这个属性标签（纯函数，可单测）。"""
    tag = re.escape(prop)
    pat = re.compile(rf"(<{tag}>\s*){re.escape(bad_ver)}(\s*</{tag}>)")
    return pat.sub(lambda m: m.group(1) + good_ver + m.group(2), pom_text)
