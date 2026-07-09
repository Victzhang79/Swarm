"""Worker L1 四级验证 — 确定性 scope / compile / lint / scoped test / LLM 自检。"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swarm.project.diff_apply import files_from_unified_diff
from swarm.types import FileScope, NotRunKind, SubTask
from swarm.worker.cmd_normalize import normalize_python_cmd
from swarm.worker.output_compress import compress_tool_output

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)


# ── 包名前缀写错的两道确定性防线（治本：本地模型写错 import 前缀 → `package X does not
#    exist` → 复读死循环到迭代上限，是"任务卡死被误判成模型能力不足→换模型"的通用机制）──
#
# 防线①（通用·权威）：_attempt_import_repair —— build 失败后，据【项目自身现存 import】
#   推导同后缀包的权威前缀并改对。不含任何硬编码包名/框架、不限项目语言生态：servlet.http
#   在本项目权威前缀是 jakarta 还是 javax，由项目源码自己说了算。这是真正的"治本"。
#
# 防线②（可选·零成本快路径）：rewrite_jvm_namespace —— Jakarta EE 整包迁移在现代 Spring
#   项目里极普遍，pull-back 时按已知迁移表【前置】改对，省一次失败构建。仅是优化，不是
#   治本依据；只收【整包迁移、与 JDK 无重叠】的前缀，杜绝误改仍属 JDK 的 javax.*
# （javax.sql/crypto/net/naming/xml.parsers/xml.transform/transaction.xa/annotation.processing…）。
# transaction（与 JDK javax.transaction.xa 重叠）、annotation（与 javax.annotation.processing
# 重叠）故意不用裸前缀，改走下面的精确符号清单。
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


def _attempt_import_repair(
    project_path: str, build_output: str, timeout: int
) -> tuple[int, list[str]]:
    """治本·通用：据【项目自身现存 import】确定性修正模块写错的包名前缀。

    返回 (改动文件数, 改动文件相对路径列表)。TD2606-C9：路径列表供调用方把【沙箱里】
    被修复的文件（可能在子任务写权 scope 之外，如父 pom）回传本地，杜绝两棵真值树静默分叉。

    不含任何硬编码框架/包名、不限具体项目：对每个编不过的 `package P.suffix does not exist`，
    在项目已有源码里查同 suffix 的【权威前缀】（如 servlet.http 在本项目权威前缀=jakarta），
    若与写错的前缀不同 → 把出错文件里该前缀替换成权威前缀，交调用方重跑构建确认。
    项目从未用过该 suffix（查无权威前缀）→ 不动（那是缺依赖问题，非前缀写错，绝不误修）。
    沙箱优先：grep/sed 都走 _run_check_split/_run_l1_command，对真实完整树操作。
    """
    pairs = parse_missing_packages(build_output)
    if not pairs:
        return 0, []
    by_pkg: dict[str, set[str]] = {}
    for f, p in pairs:
        by_pkg.setdefault(p, set()).add(f)
    changed: set[str] = set()
    for pkg, files in list(by_pkg.items())[:12]:
        if "." not in pkg:
            continue
        first, suffix = pkg.split(".", 1)
        suf_re = suffix.replace(".", r"\.")
        # 项目现存源码里同 suffix 的权威前缀（按出现次数取主导）
        gcmd = (
            f"grep -rhoE 'import [A-Za-z0-9_]+\\.{suf_re}\\.' --include='*.java' . "
            f"2>/dev/null | sort | uniq -c | sort -rn | head -8"
        )
        _ec, gout, _err = _run_check_split(gcmd, project_path, timeout=30)
        counts: dict[str, int] = {}
        for line in (gout or "").splitlines():
            mm = re.search(r"(\d+)\s+import ([A-Za-z0-9_]+)\." + suf_re + r"\.", line)
            if mm:
                counts[mm.group(2)] = counts.get(mm.group(2), 0) + int(mm.group(1))
        counts.pop(first, None)  # 写错的前缀不能当权威
        if not counts:
            continue  # 项目没用过该 suffix → 缺依赖而非前缀错，不动
        canonical = max(counts, key=lambda k: counts[k])
        for f in sorted(files):
            # -i.bak 形式在 GNU(沙箱 Linux) 与 BSD(本地 macOS) sed 上行为一致，改完删 .bak
            import shlex
            _qf = shlex.quote(f)  # R23-4：文件名安全引用（含 '/$()/; 不破坏引号边界）
            scmd = (
                f"sed -i.bak 's#{first}\\.{suf_re}#{canonical}.{suffix}#g' {_qf} "
                f"&& rm -f {shlex.quote(f + '.bak')}"
            )
            ec2, _out = _run_l1_command(scmd, project_path, timeout=20)
            if ec2 == 0:
                changed.add(f)
        logger.info(
            "[L1.2.1·import-repair] %s.%s → %s.%s（项目权威前缀，据现存源码推导，%d 文件）",
            first, suffix, canonical, suffix, len(files),
        )
    return len(changed), sorted(changed)


# ── 防线③（通用·确定性）：Maven 依赖版本不存在 → 自动校正到最近的有效版本 ──
# 治本场景：worker 实现新功能时引入第三方依赖，但【凭空写了不存在的版本号】（如
# com.warrenstrange:googleauth:1.5.2，实际最高仅 1.5.0）→ mvn 任何仓库都拉不到 →
# `Could not find artifact` → build-repair 救不回（一直用同一错版本撞墙到迭代上限）→
# L1 fail 死循环。这是"任务卡死被误判成模型弱"的又一通用机制，与 import 前缀同源。
# 解法（模型无关）：从仓库 maven-metadata 列出该 artifact 真实可用版本，若写的版本不在
# 其中 → 选【≤目标的最高版本，否则最高可用版本】，把 pom 里该版本号改对 → 重跑确认。
_MISSING_ARTIFACT_RE = re.compile(
    r"(?:Could not find artifact|Failure to find|Could not resolve dependencies for[^\n]*?)\s*"
    r"([A-Za-z0-9_.\-]+):([A-Za-z0-9_.\-]+):(?:jar|pom|war):([A-Za-z0-9_.\-]+)"
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
# 与「版本写错」是同一【模型手写依赖坐标不可靠】问题类的不同表象——统一在依赖对账里处理，
# 不再逐错加正则（避免 §0 的 whack-a-mole）。
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


def _fetch_maven_versions(group: str, artifact: str, project_path: str, timeout: int) -> list[str]:
    """查仓库 maven-metadata.xml 列出 artifact 的真实可用版本（aliyun→Central 兜底）。

    在沙箱内跑 curl/wget（沙箱有出网）。任一仓库取到非空版本即返回；都取不到返回 []。
    """
    gpath = group.replace(".", "/")
    urls = [
        f"https://maven.aliyun.com/repository/public/{gpath}/{artifact}/maven-metadata.xml",
        f"https://repo1.maven.org/maven2/{gpath}/{artifact}/maven-metadata.xml",
    ]
    for url in urls:
        cmd = f"curl -s -m 15 {shlex.quote(url)} 2>/dev/null || wget -qO- -T 15 {shlex.quote(url)} 2>/dev/null"
        _ec, out = _run_l1_command(cmd, project_path, timeout=min(timeout, 30))
        if _tool_missing(out):
            continue
        versions = re.findall(r"<version>([^<]+)</version>", out or "")
        if versions:
            return [v.strip() for v in versions if v.strip()]
    return []


def _choose_valid_version(bad: str, available: list[str]) -> str | None:
    """选最近的有效版本：≤目标的最高版本；若无（目标比所有都低）→ 最高可用版本。"""
    if not available or bad in available:
        return None
    bk = _ver_key(bad)
    le = [v for v in available if _ver_key(v) <= bk]
    pick = max(le, key=_ver_key) if le else max(available, key=_ver_key)
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


def _read_project_file(project_path: str, rel: str, timeout: int = 20) -> str | None:
    """读项目内文件文本（沙箱优先，与其它确定性检查同通道）。失败返回 None。"""
    ec, out, _err = _run_check_split(f"cat {shlex.quote(rel)}", project_path, timeout=timeout)
    return out if ec == 0 else None


def _write_project_file(project_path: str, rel: str, content: str, timeout: int = 20) -> bool:
    """写项目内文件文本（沙箱优先）。base64 管道传内容，杜绝 shell 转义/换行损坏。"""
    import base64 as _b64
    b64 = _b64.b64encode(content.encode("utf-8")).decode("ascii")
    ec, out = _run_l1_command(
        f"printf %s {shlex.quote(b64)} | base64 -d > {shlex.quote(rel)}",
        project_path, timeout=timeout,
    )
    if ec != 0:
        logger.warning("[L1.2.1·version-repair] 写回 %s 失败(ec=%s): %s", rel, ec, (out or "")[:200])
    return ec == 0


def _attempt_maven_version_repair(
    project_path: str, build_output: str, timeout: int
) -> tuple[int, list[str]]:
    """治本·通用：pom 依赖【版本】对账——统一处理「模型手写依赖坐标不可靠」整类机械错。

    覆盖两种表象（同一问题类，不再逐错加正则）：
      ① 版本写错/不存在（`Could not find artifact G:A:jar:V`）→ 校正为最近有效版本。
      ② 根本没写 <version>（`'dependencies.dependency.version' for G:A is missing`）→ 注入
         一个有效版本（从仓库 metadata 取最新；仅在【无 dependencyManagement 的模块 pom】注入，
         避免误碰父 pom 受管块产生双 version）。

    返回 (改动 pom 数, 改动 pom 相对路径列表)。TD2606-C9：父 pom 常在子任务写权 scope
    之外，被修复后必须随路径回传本地，否则修复只活在沙箱、merged_diff 缺失 → 集成重炸。

    安全自证：只在 build 已失败时触发；改完调用方重跑构建，修错（不可用版本/双 version）则重跑
    仍失败=绝不制造假通过。
    """
    missing = parse_missing_artifacts(build_output)
    missing_versions = parse_missing_versions(build_output)
    if not missing and not missing_versions:
        return 0, []
    changed: set[str] = set()
    # ── ① 版本写错/不存在 → 校正 ──
    for group, artifact, bad_ver in missing[:8]:
        available = _fetch_maven_versions(group, artifact, project_path, timeout)
        good_ver = _choose_valid_version(bad_ver, available)
        if not good_ver:
            continue  # 版本其实存在（别的网络问题）或查不到可用版本 → 不动，绝不误修
        # D32 治本：候选只取【声明该 artifactId】的 pom；替换只发生在该 <dependency> 块内。
        # （旧实现把"含该版本字符串的任何 pom"也列为候选并做含 version 行的全局串替换——模型
        # 顺手写项目自身版本号时 project/parent <version> 被连坐改写，reactor 解析崩。）
        gcmd = (
            f"grep -rl '<artifactId>{re.escape(artifact)}</artifactId>' --include=pom.xml . 2>/dev/null"
        )
        _ec, gout, _err = _run_check_split(gcmd, project_path, timeout=30)
        poms = sorted({line.strip() for line in (gout or "").splitlines() if line.strip()})
        if not poms:
            continue
        prop_names: set[str] = set()
        for pom in poms:
            text = _read_project_file(project_path, pom, timeout=20)
            if text is None:
                logger.warning("[L1.2.1·version-repair] 读取 %s 失败，跳过该 pom（不盲改）", pom)
                continue
            new_text, props = rewrite_dependency_version(text, artifact, bad_ver, good_ver)
            prop_names.update(props)
            if new_text != text and _write_project_file(project_path, pom, new_text, timeout=20):
                changed.add(pom)
        # 版本经 ${prop} 属性引用 → 去【定义该属性】的 pom（常为父 pom）校正该属性标签本身。
        # 只改这一个标签；保留属性(项目自身版本)已在 rewrite_dependency_version 内拒绝。
        for prop in sorted(prop_names):
            if not re.fullmatch(r"[A-Za-z0-9_.\-]+", prop):
                logger.warning(
                    "[L1.2.1·version-repair] 属性名 %r 含意外字符 → fail-closed 跳过", prop)
                continue
            pcmd = f"grep -rl '<{prop}>' --include=pom.xml . 2>/dev/null"
            _pc, pout, _pe = _run_check_split(pcmd, project_path, timeout=30)
            for ppom in sorted({ln.strip() for ln in (pout or "").splitlines() if ln.strip()}):
                text = _read_project_file(project_path, ppom, timeout=20)
                if text is None:
                    continue
                new_text = rewrite_property_version(text, prop, bad_ver, good_ver)
                if new_text != text and _write_project_file(project_path, ppom, new_text, timeout=20):
                    changed.add(ppom)
        logger.info(
            "[L1.2.1·version-repair] %s:%s 版本 %s 不存在（仓库可用最高=%s）→ 校正为 %s"
            "（声明 pom %d 个，属性引用 %s，仅依赖块/属性定义标签，项目自身版本不碰）",
            group, artifact, bad_ver,
            max(available, key=_ver_key) if available else "?", good_ver, len(poms),
            sorted(prop_names) or "-",
        )
    # ── ② 缺 <version> 元素 → 注入有效版本 ──
    for group, artifact in missing_versions[:8]:
        available = _fetch_maven_versions(group, artifact, project_path, timeout)
        if not available:
            continue  # 查不到可用版本 → 不动（缺依赖而非缺版本，交其它防线/定向恢复）
        good_ver = max(available, key=_ver_key)
        art_esc = artifact.replace(".", r"\.")
        gcmd = (
            f"grep -rl '<artifactId>{art_esc}</artifactId>' --include=pom.xml . 2>/dev/null"
        )
        _ec, gout, _err = _run_check_split(gcmd, project_path, timeout=30)
        poms = sorted({line.strip() for line in (gout or "").splitlines() if line.strip()})
        for pom in poms:
            # 只在【无 dependencyManagement 的模块 pom】注入：父 pom 的受管块本就带版本，
            # 误插会造双 version。模块 pom 里该 artifactId 唯一，插在其后安全。
            _gc, gmgmt, _ge = _run_check_split(
                f"grep -c '<dependencyManagement>' {shlex.quote(pom)}", project_path, timeout=10
            )
            if (gmgmt or "").strip() not in ("", "0"):
                continue
            # 在该 artifact 的 <artifactId> 行后插入 <version>（模块 pom 内唯一，安全）。
            # 用 perl（GNU/BSD/沙箱皆一致，避开 sed a\ 在 BSD 上不可用），\Q\E 字面转义。
            scmd = (
                f"perl -i.bak -pe "
                f"'s#(<artifactId>\\Q{artifact}\\E</artifactId>)#$1\\n            "
                f"<version>{good_ver}</version>#' {shlex.quote(pom)} && rm -f {shlex.quote(pom + '.bak')}"
            )
            ec2, _o = _run_l1_command(scmd, project_path, timeout=20)
            if ec2 == 0:
                changed.add(pom)
        logger.info(
            "[L1.2.1·version-repair] %s:%s 缺 <version> → 注入 %s（%d pom，受管 pom 跳过）",
            group, artifact, good_ver, len(poms),
        )
    return len(changed), sorted(changed)


# ── 防线④（通用·确定性）：缺第三方依赖声明 → 据 import 反查坐标补进 module pom ──
# 治本场景（996db614 实测头号 package-does-not-exist，~137/213）：worker 实现功能时 import 了
# 第三方库（jjwt/redis/fastjson2/quartz/hutool…）但模块 pom 没声明该依赖 → `package P does
# not exist` → 整文件编不过 → 下游 cannot-find-symbol 级联 → 复读死循环到迭代上限。这与
# import 前缀错(import-repair)/版本错(version-repair)同源——都是「模型手写依赖坐标不可靠」，
# 但表象是【整个依赖没声明】。import-repair 明确不碰它（"项目没用过该 suffix=缺依赖，不动"）。
#
# 解法（模型无关、非 Java 写死之外的"Maven 生态事实标准"）：对每个缺失的第三方包 P，
#   1) 从出错文件的 import 行取一个具体 FQCN（如 io.jsonwebtoken.Jwts）；
#   2) Maven Central 全文类检索 fc:<FQCN> → 提供该类的 (groupId, artifactId)（groupId 必须是
#      P 的前缀，杜绝错配）——这是注册中心权威事实，臆造的类查无结果→自动跳过（天然过滤幻觉）；
#   3) 该 artifact 若被父 dependencyManagement 受管 → 注入无 version 依赖（继承）；否则取 Central
#      maven-metadata 最新版注入；
#   4) 注入到出错文件所属 module pom 的 <dependencies>（已声明则跳过），交调用方重跑确认。
# 安全自证：只在 build 已失败时触发；坐标查无/groupId 不匹配/已声明 → 不动；修错（坐标/版本不
# 兼容）则重跑仍失败=绝不假通过。与 ③-A 收敛循环协同：补依赖→重跑→新浮现的符号错再被 typo 修。
#
# 排除：java./javax./jakarta./sun. 是 JDK 自带或 servlet 命名空间问题（rewrite_jvm_namespace 治），
# 项目【自有 groupId】前缀是【内部包未就绪】(②依赖拓扑)，都不在"缺第三方依赖"范围。
_DEP_REPAIR_SKIP_PREFIXES = ("java.", "javax.", "jakarta.", "sun.", "com.sun.", "jdk.")


def _project_own_packages(project_path: str, timeout: int = 20) -> set[str]:
    """项目【自有包根】：据【源码自身声明的 package】取前 2 段前缀（com.ruoyi 等）。

    硬判据=源码事实，而非 pom <groupId>——pom 的 groupId 含一堆【第三方依赖】的 group（如
    com.alibaba/org.springframework/org.apache.shiro 在父 dependencyManagement + 各模块 deps 都现身），
    据 pom group 会把第三方误判成"自有"→ 缺第三方包(fastjson2 等)被当内部包误 BLOCKED、还不补依赖。
    项目【自己 build】的包必由其 .java `package` 声明（com.ruoyi.**），第三方包只被 import 从不被
    本项目源码声明（io.jsonwebtoken/com.alibaba.fastjson2 无任何 .java 声明它）——这才是内部 vs 第三方
    的可靠分界。返回出现在 ≥2 个源文件的 2 段包根集合（滤噪）。"""
    cmd = (
        "grep -rhoE '^[[:space:]]*package[[:space:]]+[A-Za-z0-9_.]+' --include='*.java' . 2>/dev/null "
        "| sed -E 's/^[[:space:]]*package[[:space:]]+//' "
        "| awk -F. 'NF>=2{print $1\".\"$2}' | sort | uniq -c | sort -rn | head -10"
    )
    _ec, out, _e = _cached_scan(cmd, project_path, timeout=timeout)  # A7：只读全树扫描，按文件签名缓存
    groups: set[str] = set()
    for line in (out or "").splitlines():
        m = re.match(r"\s*(\d+)\s+([A-Za-z0-9_.]+)", line)
        # 任何被【项目源码 package 声明】的 2 段包根即项目自有（项目自己 build 它）。哪怕只 1 个
        # 文件声明也算——源码 package 声明=定义性证据，无"第三方 group 混进来"的噪声（第三方只被
        # import 从不被本项目源码声明）。
        if m and int(m.group(1)) >= 1:
            groups.add(m.group(2))
    return groups


def _fqcn_for_missing_pkg(project_path: str, rel_file: str, pkg: str, timeout: int) -> str | None:
    """从出错文件的 import 行取该缺失包下的一个【具体 FQCN】（io.jsonwebtoken.Jwts 等）。

    通配 `import P.*;` 无具体类 → 返回 None（无法精确反查，交契约/其它防线）。"""
    pe = re.escape(pkg)
    cmd = f"grep -hoE 'import +(static +)?{pe}\\.[A-Za-z_][A-Za-z0-9_.]*' {shlex.quote(rel_file)} 2>/dev/null | head -4"
    _ec, out, _e = _run_check_split(cmd, project_path, timeout=min(timeout, 20))
    for line in (out or "").splitlines():
        m = re.search(rf"import\s+(?:static\s+)?({pe}\.[A-Za-z_][A-Za-z0-9_.]*)", line)
        if m:
            fqcn = m.group(1)
            leaf = fqcn.rsplit(".", 1)[-1]
            if leaf and leaf[0].isupper():  # 取到的是类名(大写开头)，非子包/通配
                return fqcn
    return None


def _resolve_artifact_via_central(
    fqcn: str, pkg: str, project_path: str, timeout: int
) -> tuple[str, str] | None:
    """Maven Central 全文类检索 fc:<FQCN> → 提供该类的 (groupId, artifactId)。

    只接受 groupId 是【缺失包 pkg 的前缀】的结果（杜绝同名类错配到无关库）；偏好非
    -bom/-parent/-tests 的实体 artifact。查无/无网/groupId 不匹配 → None（臆造类天然在此被滤掉）。"""
    url = (
        "https://search.maven.org/solrsearch/select?"
        f"q=fc:{fqcn}&rows=15&wt=json"
    )
    cmd = f"curl -s -m 15 {shlex.quote(url)} 2>/dev/null || wget -qO- -T 15 {shlex.quote(url)} 2>/dev/null"
    _ec, out = _run_l1_command(cmd, project_path, timeout=min(timeout, 30))
    if _tool_missing(out) or not (out or "").strip():
        return None
    try:
        docs = (json.loads(out).get("response", {}) or {}).get("docs", []) or []
    except (ValueError, TypeError):
        return None
    cands: list[tuple[str, str]] = []
    for d in docs:
        g, a = d.get("g"), d.get("a")
        if not g or not a:
            continue
        # groupId 必须是 pkg 前缀（pkg==g 或 pkg 以 g. 开头），否则同名类错配到无关库
        if pkg == g or pkg.startswith(g + "."):
            cands.append((g, a))
    # 偏好实体 artifact（排除 bom/parent/tests/dependencies 聚合件）
    def _rank(ga: tuple[str, str]) -> tuple:
        a = ga[1].lower()
        bad = any(t in a for t in ("-bom", "-parent", "-tests", "-test", "dependencies"))
        return (1 if bad else 0, len(a))
    cands.sort(key=_rank)
    return cands[0] if cands else None


def _module_pom_for_file(project_path: str, rel_file: str, timeout: int) -> str | None:
    """从出错文件向上找最近的 module pom.xml（归一化模块相对路径）。"""
    d = rel_file.rsplit("/", 1)[0] if "/" in rel_file else "."
    cmd = (
        f'd={shlex.quote(d)}; while [ -n "$d" ] && [ "$d" != "." ] && [ "$d" != "/" ]; do '
        f'[ -f "$d/pom.xml" ] && echo "$d/pom.xml" && break; d=$(dirname "$d"); done; '
        f'[ -f "./pom.xml" ] && [ -z "$d" -o "$d" = "." ] && echo "pom.xml"'
    )
    _ec, out, _e = _run_check_split(cmd, project_path, timeout=min(timeout, 15))
    for line in (out or "").splitlines():
        line = line.strip().lstrip("./") or line.strip()
        if line.endswith("pom.xml"):
            return line
    return None


def _pom_declares_artifact(project_path: str, pom: str, artifact: str, timeout: int) -> bool:
    """module pom 是否已声明该 artifactId（避免重复注入）。"""
    cmd = f"grep -c '<artifactId>{re.escape(artifact)}</artifactId>' {shlex.quote(pom)} 2>/dev/null"
    _ec, out, _e = _run_check_split(cmd, project_path, timeout=min(timeout, 10))
    return (out or "").strip() not in ("", "0")


def _artifact_is_managed(project_path: str, artifact: str, timeout: int) -> bool:
    """该 artifactId 是否在某 pom 的 <dependencyManagement> 受管（→ 注入无 version 继承）。"""
    cmd = (
        "for p in $(grep -rl '<dependencyManagement>' --include=pom.xml . 2>/dev/null); do "
        f"awk '/<dependencyManagement>/,/<\\/dependencyManagement>/' \"$p\"; done "
        f"| grep -c '<artifactId>{re.escape(artifact)}</artifactId>'"
    )
    _ec, out, _e = _run_check_split(cmd, project_path, timeout=min(timeout, 20))
    return (out or "").strip() not in ("", "0")


# 运行时伴生件后缀约定：主件常是 `-api`/`-core`（仅编译期接口），运行时还需 `-impl`/`-runtime`
# 及 JSON 绑定 `-jackson`/`-jaxb`，否则编译过但 L2/L3 运行期 ClassNotFound（jjwt 实测：仅 jjwt-api
# 编译过、运行 Jwts.builder() 即炸，需 jjwt-impl+jjwt-jackson）。只取这几个【无歧义】伴生后缀，
# 不含 `-gson`（与 jackson 二选一，避免双 JSON 绑定冲突）——通用约定，非硬编码具体库。
_RUNTIME_COMPANION_SUFFIXES = ("impl", "runtime", "jackson", "jaxb")


def _resolve_artifact_family(
    group: str, primary: str, project_path: str, timeout: int
) -> list[str]:
    """主 artifact 的【运行时伴生件】（jjwt-api → jjwt-impl/jjwt-jackson）。

    据 artifactId 基名（去 `-api`/`-core` 后缀）+ 运行时伴生后缀约定，查同 groupId 下确实存在的
    伴生件。通用（任何 api/impl 拆分库），无硬编码库表。主件非 -api/-core → 不像拆分库，返回 []。"""
    base = primary
    for suf in ("-api", "-core"):
        if primary.endswith(suf):
            base = primary[: -len(suf)]
            break
    if base == primary:
        return []
    url = f"https://search.maven.org/solrsearch/select?q=g:%22{group}%22&rows=40&wt=json"
    cmd = f"curl -s -m 15 {shlex.quote(url)} 2>/dev/null || wget -qO- -T 15 {shlex.quote(url)} 2>/dev/null"
    _ec, out = _run_l1_command(cmd, project_path, timeout=min(timeout, 30))
    if _tool_missing(out) or not (out or "").strip():
        return []
    try:
        docs = (json.loads(out).get("response", {}) or {}).get("docs", []) or []
    except (ValueError, TypeError):
        return []
    present = {d.get("a") for d in docs if d.get("a")}
    return [f"{base}-{s}" for s in _RUNTIME_COMPANION_SUFFIXES if f"{base}-{s}" in present]


def _inject_dependency(
    project_path: str, pom: str, group: str, artifact: str, version: str | None,
    timeout: int, scope: str | None = None,
) -> bool:
    """在 module pom 的【最后一个 </dependencies>】前插入 <dependency>（受管则无 version）。

    最后一个 </dependencies> 即常规依赖块（dependencyManagement 内的 </dependencies> 在其之前），
    模块 pom 多无 depMgmt 故唯一即正确。perl -0777 整文件 + 贪婪 .* 命中最后一处。scope 非空则带
    `<scope>`（运行时伴生件用 runtime）。"""
    ver_line = f"<version>{version}</version>" if version else ""
    scope_line = f"<scope>{scope}</scope>" if scope else ""
    block = (
        f"<dependency><groupId>{group}</groupId>"
        f"<artifactId>{artifact}</artifactId>{ver_line}{scope_line}</dependency>"
    )
    # 贪婪匹配到最后一个 </dependencies>，在其前插入；无 </dependencies> 则不改（返回非0→跳过）
    cmd = (
        f"grep -q '</dependencies>' {shlex.quote(pom)} && perl -0777 -i.bak -pe "
        f"'s#(.*)</dependencies>#$1    {block}\\n    </dependencies>#s' {shlex.quote(pom)} "
        f"&& rm -f {shlex.quote(pom + '.bak')}"
    )
    ec, _o = _run_l1_command(cmd, project_path, timeout=min(timeout, 20))
    return ec == 0


def _attempt_dependency_repair(
    project_path: str, build_output: str, modified: list[str], timeout: int
) -> tuple[int, list[str]]:
    """治本·通用：缺第三方依赖声明 → 据 import 反查 Maven 坐标补进 module pom。见上方 防线④ 注释。

    只修【本子任务文件】里缺的第三方包（别人的交其 owner/拓扑修，配合文件级归属与 ② 依赖拓扑）。
    返回 (改动 pom 数, 改动 pom 相对路径列表)，TD2606-C9 供回传（module pom 可能在写权 scope 外）。"""
    pairs = parse_missing_packages(build_output)
    if not pairs:
        return 0, []
    mods = {_norm_src_path(f) for f in (modified or []) if str(f).strip()}
    own = _project_own_packages(project_path, timeout)
    want: dict[str, set[str]] = {}
    for f, pkg in pairs:
        rel = _norm_src_path(f)
        if mods and rel not in mods and not any(rel.endswith(m) or m.endswith(rel) for m in mods):
            continue  # 别人的文件
        if any(pkg == p.rstrip(".") or pkg.startswith(p) for p in _DEP_REPAIR_SKIP_PREFIXES):
            continue  # JDK / servlet 命名空间，非缺依赖
        if any(pkg == g or pkg.startswith(g + ".") for g in own):
            continue  # 项目自有 group → 内部包未就绪(②)，非缺第三方依赖
        want.setdefault(pkg, set()).add(rel)
    if not want:
        return 0, []
    changed: set[str] = set()
    for pkg, files in list(want.items())[:8]:
        first = sorted(files)[0]
        fqcn = _fqcn_for_missing_pkg(project_path, first, pkg, timeout)
        if not fqcn:
            continue  # 通配 import / 取不到具体类 → 不赌
        coord = _resolve_artifact_via_central(fqcn, pkg, project_path, timeout)
        if not coord:
            continue  # 坐标查无（臆造类 / 无网）→ 不动
        group, artifact = coord
        managed = _artifact_is_managed(project_path, artifact, timeout)
        version: str | None = None
        if not managed:
            available = _fetch_maven_versions(group, artifact, project_path, timeout)
            if not available:
                continue  # 既不受管又查不到版本 → 不赌
            version = max(available, key=_ver_key)
        # 运行时伴生件（jjwt-api → jjwt-impl/jjwt-jackson）：同版本、runtime scope，杜绝"编译过
        # 但运行期 ClassNotFound"。受管(version=None)则伴生件也无 version 继承。
        family = _resolve_artifact_family(group, artifact, project_path, timeout)
        for f in sorted(files):
            pom = _module_pom_for_file(project_path, f, timeout)
            if not pom:
                continue
            if _pom_declares_artifact(project_path, pom, artifact, timeout):
                continue  # 已声明（可能上一轮/别处补过）
            if _inject_dependency(project_path, pom, group, artifact, version, timeout):
                changed.add(pom)
                for sib in family:
                    if not _pom_declares_artifact(project_path, pom, sib, timeout):
                        _inject_dependency(
                            project_path, pom, group, sib, version, timeout, scope="runtime"
                        )
        logger.info(
            "[L1.2.1·dep-repair] %s → %s:%s%s 注入 module pom（据 import 反查 Maven Central%s）",
            pkg, group, artifact, (":" + version) if version else "(受管,继承版本)",
            f"，+运行时伴生件 {family}" if family else "",
        )
    return len(changed), sorted(changed)


def _build_blocked_on_unbuilt_internal(
    project_path: str, build_output: str, timeout: int
) -> set[str]:
    """构建失败是否【全因引用了尚未建出的项目内部包】(②跨模块/跨子任务未就绪)。

    返回【被阻断的内部缺包集合】：非空=是②类阻断（集合即缺的内部包，供 brain 反查生产者
    子任务、判其是否已被永久放弃）；空集=非②类（照常 FAIL）。

    治本场景（996db614 实测 ~70/213）：子任务 A 引用 `com.ruoyi.alarm.sender.dto` 等【别的子任务
    还没建出的内部包】→ `package does not exist`。这不是 A 的能力问题，也无法由 A 修（包归别人建）；
    plan 时拿不到 A 的 import 故无法确定性预先 depends_on。治本=worker 把它识别为 BLOCKED 退避，
    待生产者子任务落地（merge 进项目树）后由 transient 重试自然消解，不烧 A 的修复轮 / 不误判
    capability 换模型 / 不 escalate 清空已成功成果。

    判据（保守，宁可不标也不误标）：所有 `package P does not exist` 的 P 都满足
      ① P 是【项目自有 groupId 前缀】(内部包，非第三方——第三方交 dep-repair 防线④)；且
      ② 当前项目树里【无任何 .java 声明 package P】(=确实还没被任何子任务建出)。
    只要有一个缺包是第三方、或已在树里(=真编译错如包名拼错) → 返回 False，照常 FAIL。"""
    pairs = parse_missing_packages(build_output)
    if not pairs:
        return set()
    own = _project_own_packages(project_path, timeout)
    if not own:
        return set()
    internal_pkgs: set[str] = set()
    for _f, pkg in pairs:
        if any(pkg == p.rstrip(".") or pkg.startswith(p) for p in _DEP_REPAIR_SKIP_PREFIXES):
            return set()  # JDK/servlet 命名空间问题，非②
        if not any(pkg == g or pkg.startswith(g + ".") for g in own):
            return set()  # 有第三方缺包 → 交 dep-repair，不是纯②
        internal_pkgs.add(pkg)
    if not internal_pkgs:
        return set()
    for pkg in internal_pkgs:
        cmd = (
            f"grep -rlE '^[[:space:]]*package[[:space:]]+{re.escape(pkg)}[[:space:]]*;' "
            f"--include='*.java' . 2>/dev/null | head -1"
        )
        _ec, out, _e = _run_check_split(cmd, project_path, timeout=min(timeout, 20))
        if (out or "").strip():
            return set()  # 该内部包已在树里却报 does not exist → 真错(非未就绪)，照常 FAIL
    return internal_pkgs


def _tool_missing(out: str) -> bool:
    """命令输出是否表明【工具本身缺失】（→ 优雅跳过，不当作修复失败）。"""
    low = (out or "").lower()
    return any(
        m in low for m in (
            "command not found", "not found", "executable file not found",
            "no such file or directory", "is not recognized",
            "could not determine executable", "npm err", "cannot find module 'eslint'",
        )
    )


# ── 跨生态确定性构建修复：每个生态委托其【事实标准 autofix】，按文件类型 dispatch ──
# 通用框架（非 Java 细节才是可推广的部分）：build 失败 → 按出错/改动文件语言路由到对应
# adapter → 套用该生态权威 autofix → 调用方重跑构建确认。混合项目按扩展名逐语言并行修。
# 安全性自证：只在 build 已失败时触发，且必须重跑通过才算修好；工具缺失一律优雅跳过。
_TS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".vue", ".mjs", ".cjs", ".mts", ".cts")


def _repair_go(project_path: str, go_files: list[str], timeout: int) -> tuple[int, list[str]]:
    """Go：goimports -w —— 事实标准，自动增删/解析 import。工具缺失则跳过。

    返回 (修复文件数, 文件相对路径列表)，TD2606-C9 供回传。"""
    if not go_files:
        return 0, []
    touched = list(go_files[:50])
    files = " ".join(shlex.quote(f) for f in touched)
    ec, out = _run_l1_command(f"goimports -w {files}", project_path, timeout=min(timeout, 120))
    if ec != 0 and _tool_missing(out):
        logger.info("[L1.2.1·repair] goimports 不可用，跳过 Go import 修复")
        return 0, []
    if ec == 0:
        logger.info("[L1.2.1·repair] goimports -w 修复 %d 个 .go 文件 import", len(touched))
        return len(touched), touched
    return 0, []


def _repair_rust(project_path: str, timeout: int) -> tuple[int, list[str]]:
    """Rust：cargo fix —— 自动套用 rustc 机器可应用建议（含 use 路径）。crate 级。

    返回 (修复标记, 路径列表)。治本(TD2606-C9 收尾)：cargo fix 是 crate 级、可能改到【子任务
    写权 scope 之外】的同 crate 兄弟文件/Cargo.lock；故用 `git diff --name-only` 取其实际触达的
    文件集回传，杜绝"修复只活在沙箱"——与 pom 清单同类治本。git 不可用则优雅降级为空列表。"""
    cmd = "cargo fix --allow-dirty --allow-no-vcs --edition-idioms -q 2>&1"
    ec, out = _run_l1_command(cmd, project_path, timeout=max(timeout, 240))
    if _tool_missing(out):
        logger.info("[L1.2.1·repair] cargo 不可用，跳过 Rust 修复")
        return 0, []
    # cargo fix 可能因冲突非 0 退出，但已应用的建议仍写盘；交重跑构建仲裁
    touched: list[str] = []
    try:
        d_ec, d_out = _run_l1_command(
            "git diff --name-only", project_path, timeout=min(timeout, 60)
        )
        if d_ec == 0:
            touched = [ln.strip() for ln in (d_out or "").splitlines() if ln.strip()][:100]
        else:
            # #13 治本·降级可观测：沙箱 /workspace 由 `git archive HEAD` 烤成→【无 .git】→
            # `git diff` 非 0（"not a git repository"）。此时无法枚举 cargo fix 触达的【scope 外】
            # 文件(Cargo.lock/同 crate 兄弟)→它们不会被 pull-back 强制回传。显式告警杜绝静默丢弃
            # （crate 内 src 仍在 scope 内正常回传；cargo fix 幂等，下轮构建可重导）。
            logger.warning(
                "[L1.2.1·repair] Rust 触达清单枚举不可用（git diff 退出码 %s，沙箱无 .git）→ "
                "cargo fix 的 scope 外改动(Cargo.lock/兄弟文件)本轮不强制回传；"
                "如需可靠传播请把它们纳入子任务 scope",
                d_ec,
            )
    except Exception:  # noqa: BLE001 —— 取触达清单失败不致命，退化为空列表
        touched = []
    logger.info("[L1.2.1·repair] cargo fix 已尝试套用 rustc 建议（exit=%s, 触达 %d 文件）",
                ec, len(touched))
    return 1, touched


def _repair_ts(project_path: str, ts_files: list[str], timeout: int) -> tuple[int, list[str]]:
    """TS/JS/Vue/前端：eslint --fix —— 自动修 import/order、可修复规则。需项目本地 eslint+config。

    返回 (修复文件数, 文件相对路径列表)，TD2606-C9 供回传。"""
    if not ts_files:
        return 0, []
    touched = list(ts_files[:60])
    files = " ".join(shlex.quote(f) for f in touched)
    # --no-install：只用项目本地 eslint，绝不联网装；缺失则报错→识别为工具缺失跳过
    ec, out = _run_l1_command(
        f"npx --no-install eslint --fix {files} 2>&1", project_path, timeout=min(timeout, 180)
    )
    if _tool_missing(out) or "no eslint configuration" in (out or "").lower():
        logger.info("[L1.2.1·repair] eslint 不可用/无配置，跳过 TS/JS 修复")
        return 0, []
    # eslint exit 0=干净 1=仍有不可自动修的错误（但可修的已写盘）→ 都算"已尝试修"
    if ec in (0, 1):
        logger.info("[L1.2.1·repair] eslint --fix 修复 %d 个 TS/JS/Vue 文件", len(touched))
        return len(touched), touched
    return 0, []


def _stack_repair_langs(project_stack: dict | None) -> set[str] | None:
    """据【权威栈画像】(detect_stack：小模型识别→大模型确认→KB 持久化) 选 repair 生态集合。

    单一事实源：adapter 选择是 project_stack 的【消费者】，与 tech_design/plan/worker prompt
    同源，不再独立按文件扩展名瞎猜语言。以 build 工具为准（maven/gradle/go/cargo/npm…，无歧义；
    避开 "javascript" 含 "java" 子串陷阱）+ 前端形态。无画像/未判明 → 返回 None，调用方回退扩展名。
    """
    if not project_stack:
        return None
    build = (project_stack.get("build") or "").strip().lower()
    fe = (project_stack.get("frontend") or "").lower()
    fe_kind = (project_stack.get("frontend_kind") or "").lower()
    langs: set[str] = set()
    if build in ("maven", "gradle", "sbt"):
        langs.add("java")
    if build == "go":
        langs.add("go")
    if build == "cargo":
        langs.add("rust")
    if build in ("npm", "yarn", "pnpm"):
        langs.add("ts")
    if fe_kind in ("spa", "separated") or any(
        x in fe for x in ("vue", "react", "angular", "svelte", "next")
    ):
        langs.add("ts")
    return langs or None


_SYMBOL_ERR_RE = re.compile(
    r"([A-Za-z0-9_./\-]+\.(?:java|kt|scala)):\[\d+,\d+\][^\n]*cannot find symbol[^\n]*\n"
    r"[^\n]*symbol:\s*(?:method|class|variable)\s+([A-Za-z_][A-Za-z0-9_]*)"
)


def _edit_distance(a: str, b: str, cap: int = 3) -> int:
    """Levenshtein，超 cap 提前返回 cap+1（够判近邻即可，省算）。"""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = i
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
            best = min(best, cur[-1])
        if best > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _attempt_symbol_repair(
    project_path: str, build_output: str, modified: list[str], timeout: int
) -> tuple[int, list[str]]:
    """治本·通用：模型臆造/拼错的方法/类名（isEmtpy→isEmpty、StringBufffer→StringBuffer 等）→
    据【项目自身现存符号】按编辑距离纠到最近的真实符号。与 import-repair 同源：真理取自项目用法，
    无硬编码符号表，任何 .java/.kt/.scala 皆可（非 Java 写死、非 RuoYi 专用）。改完调用方重跑确认。

    安全：仅当存在【唯一近邻】（编辑距离≤2、项目内高频≥5、≠原名）才改，歧义则放弃；只修【本子任务
    改动的文件】（别人的文件交其 owner，配合文件级归属）；改错重跑仍失败=绝不假通过。
    """
    clean = re.sub(r"\x1b\[[0-9;]*m", "", build_output or "")
    errs = _SYMBOL_ERR_RE.findall(clean)
    if not errs:
        return 0, []
    mods = {_norm_src_path(f) for f in (modified or []) if str(f).strip()}
    mcmd = ("grep -rhoE '[A-Za-z_][A-Za-z0-9_]+' --include='*.java' --include='*.kt' . "
            "2>/dev/null | sort | uniq -c | sort -rn | head -4000")
    _ec, gout, _e = _cached_scan(mcmd, project_path, timeout=min(timeout, 60))
    freq: dict[str, int] = {}
    for line in (gout or "").splitlines():
        m = re.match(r"\s*(\d+)\s+(\S+)", line)
        if m:
            freq[m.group(2)] = int(m.group(1))
    if not freq:
        return 0, []
    changed: set[str] = set()
    seen: set[tuple] = set()
    for fpath, name in errs:
        rel = _norm_src_path(fpath)
        if mods and rel not in mods and not any(rel.endswith(m) or m.endswith(rel) for m in mods):
            continue  # 别人的文件，不动
        if (rel, name) in seen:
            continue
        seen.add((rel, name))
        cands = [(w, _edit_distance(name, w)) for w in freq
                 if w != name and freq[w] >= 5 and abs(len(w) - len(name)) <= 2]
        cands = [(w, d) for w, d in cands if d <= 2]
        if not cands:
            continue
        best_d = min(d for _w, d in cands)
        top = [w for w, d in cands if d == best_d]
        if len(top) != 1:
            continue  # 歧义近邻，不赌
        good = top[0]
        scmd = (f"perl -i.bak -pe 's#\\b{re.escape(name)}\\b#{good}#g' {shlex.quote(rel)} "
                f"&& rm -f {shlex.quote(rel + '.bak')}")
        ec2, _o = _run_l1_command(scmd, project_path, timeout=20)
        if ec2 == 0:
            changed.add(rel)
            logger.info("[L1.2.1·symbol-repair] %s: %s→%s（项目近邻 距=%d 频=%d）",
                        rel, name, good, best_d, freq[good])
    return len(changed), sorted(changed)


def plan_internal_import_drift_rewrites(
    file_missing_imports: dict[str, list[tuple[str, str]]],
    class_internal_packages: dict[str, set[str]],
) -> list[tuple[str, str, str]]:
    """#9 漂移 import 重写【纯规划器】(无 IO，易测)。

    入参：
      file_missing_imports: {出错文件: [(缺失内部包 P, 被引类 C), ...]}——每个 (P,C) 表示该文件
        `import P.C;` 引了一个【树里不存在的内部包 P】。
      class_internal_packages: {类名 C: {C 在项目树里真实声明所在的内部包集合}}。
    出参：[(文件, "P.C", "R.C"), ...] 确定性重写指令。

    判据（fail-closed）：候选 = C 的真实内部包 - {P}。**唯一**候选 R 才产出重写；
    零候选（类真不存在=未就绪/臆造）或多候选（同名类多处=歧义）→ 不重写，交回 BLOCKED/快失败。
    通用跨栈、非项目写死（纯集合运算，不含任何硬编码包名/FQN）。"""
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for rel_file, imports in file_missing_imports.items():
        for pkg, cls in imports:
            cands = {r for r in class_internal_packages.get(cls, set()) if r and r != pkg}
            if len(cands) != 1:
                continue  # 零解/多解 → fail-closed
            real = next(iter(cands))
            key = (rel_file, f"{pkg}.{cls}", f"{real}.{cls}")
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out


def _imported_classes_from_pkg(
    project_path: str, rel_file: str, pkg: str, timeout: int
) -> list[str]:
    """抽 rel_file 里【直接 import 缺失包 pkg 下的具体类名】（去重保序）。

    只取 `import pkg.C;` / `import static pkg.C.X;` 里紧跟 pkg 的段 C 且【大写开头=类名】；
    `import pkg.sub....`（小写子包）与 `import pkg.*;`（通配无具体类）都不取——无法精确定位。"""
    pe = re.escape(pkg)
    cmd = (
        f"grep -hoE 'import +(static +)?{pe}\\.[A-Za-z_][A-Za-z0-9_.]*' {shlex.quote(rel_file)} "
        f"2>/dev/null | head -20"
    )
    _ec, out, _e = _run_check_split(cmd, project_path, timeout=min(timeout, 20))
    classes: list[str] = []
    seen: set[str] = set()
    for line in (out or "").splitlines():
        m = re.search(rf"import\s+(?:static\s+)?{pe}\.([A-Za-z_][A-Za-z0-9_]*)", line)
        if m:
            cls = m.group(1)
            if cls and cls[0].isupper() and cls not in seen:
                seen.add(cls)
                classes.append(cls)
    return classes


def _internal_packages_declaring_class(
    project_path: str, cls: str, own: set[str], timeout: int
) -> set[str]:
    """查【类 cls 在项目树里真实声明所在的内部包集合】（据 <cls>.java 路径反推包，排除测试树）。

    RuoYi/Java 惯例：一公开类一文件、文件名=类名 → 路径即权威包。只保留【项目自有前缀】的包
    （own），第三方/JDK 不算。多处声明 → 返回多元素集合，交规划器 fail-closed。"""
    from swarm.worker.symbol_resolver import file_path_to_fqn
    cmd = (
        f"grep -rlE '(class|interface|enum|record)[[:space:]]+{re.escape(cls)}"
        f"([^A-Za-z0-9_]|$)' --include={shlex.quote(cls + '.java')} . 2>/dev/null | head -20"
    )
    _ec, out, _e = _run_check_split(cmd, project_path, timeout=min(timeout, 30))
    pkgs: set[str] = set()
    for line in (out or "").splitlines():
        path = line.strip()
        if not path:
            continue
        norm = path.replace("\\", "/")
        if "/src/test/" in norm or "/test/java/" in norm:
            continue  # 测试树的包不算生产包
        fqn = file_path_to_fqn(norm)
        if not fqn or "." not in fqn:
            continue
        pkg = fqn.rsplit(".", 1)[0]
        if any(pkg == g or pkg.startswith(g + ".") for g in own):
            pkgs.add(pkg)
    return pkgs


def _attempt_internal_import_drift_repair(
    project_path: str, build_output: str, timeout: int
) -> tuple[int, list[str]]:
    """#9 治本（Candidate B）：跨 feature 包布局漂移 → 据类真实内部包确定性重写 import。

    现象（round19 头号交付天花板）：脚手架/生产者把类落在【扁平】`com.ruoyi.alarm.domain`，
    消费者独立猜成【嵌套】`com.ruoyi.alarm.robot.domain` → javac `package P does not exist`。
    旧路径把它当 internal_pkg_not_built BLOCKED、等一个永不到来的生产者（#10 幽灵生产者），
    慢磨整条 transient 阶梯才 abandon。这里在判 BLOCKED 前：对每个【自有前缀的缺失内部包 P】，
    取出错文件里 `import P.C;` 的类 C，查 C 在树里的【真实内部包 R】，唯一解 → 重写 P.C→R.C，
    交调用方重跑确认。零解（真未就绪/臆造）或多解（歧义）→ 不动，交回 BLOCKED/#10 快失败。

    与 import/symbol-repair 同源：真理取自项目实际产出、无硬编码、跨 feature 通用、非项目写死；
    只改【出错的消费者文件自身】（别人的文件交其 owner）。SWARM_WORKER_IMPORT_DRIFT_REPAIR=false 可关。
    """
    if os.environ.get(
        "SWARM_WORKER_IMPORT_DRIFT_REPAIR", "true"
    ).lower() in ("false", "0", "no"):
        return 0, []
    pairs = parse_missing_packages(build_output)
    if not pairs:
        return 0, []
    own = _project_own_packages(project_path, timeout)
    if not own:
        return 0, []
    # 1) 只保留【内部缺包】(自有前缀、非 JDK/servlet/第三方)，按出错文件归组
    missing_by_file: dict[str, set[str]] = {}
    for f, p in pairs:
        if any(p == pre.rstrip(".") or p.startswith(pre) for pre in _DEP_REPAIR_SKIP_PREFIXES):
            continue  # JDK/servlet 命名空间，交 jvm-namespace/import-repair
        if not any(p == g or p.startswith(g + ".") for g in own):
            continue  # 第三方缺包，交 dep-repair
        missing_by_file.setdefault(_norm_src_path(f), set()).add(p)
    if not missing_by_file:
        return 0, []
    # 2) 抽每个错文件里【引用缺包的 import 具体类】
    file_missing_imports: dict[str, list[tuple[str, str]]] = {}
    wanted: set[str] = set()
    for rel, pkgs in missing_by_file.items():
        imps: list[tuple[str, str]] = []
        for pkg in sorted(pkgs):
            for cls in _imported_classes_from_pkg(project_path, rel, pkg, timeout):
                imps.append((pkg, cls))
                wanted.add(cls)
        if imps:
            file_missing_imports[rel] = imps
    if not wanted:
        return 0, []
    # 3) 查每个被引类的真实内部包
    class_pkgs: dict[str, set[str]] = {
        cls: _internal_packages_declaring_class(project_path, cls, own, timeout)
        for cls in sorted(wanted)
    }
    # 4) 规划唯一解重写
    rewrites = plan_internal_import_drift_rewrites(file_missing_imports, class_pkgs)
    if not rewrites:
        return 0, []
    # 5) 沙箱优先应用 perl 全字替换 old_fqn→new_fqn（\Q..\E 转义点，\b 收尾防误伤更长类名）
    changed: set[str] = set()
    for rel, old, new in rewrites:
        scmd = (
            f"perl -i.bak -pe 's#\\Q{old}\\E\\b#{new}#g' {shlex.quote(rel)} && rm -f {shlex.quote(rel + '.bak')}"
        )
        ec2, _o = _run_l1_command(scmd, project_path, timeout=20)
        if ec2 == 0:
            changed.add(rel)
            logger.info(
                "[L1.2.1·import-drift] %s: %s → %s（类真实内部包，据树实证，#9 漂移治本）",
                rel, old, new,
            )
    return len(changed), sorted(changed)


def _attempt_build_repair(
    project_path: str,
    build_output: str,
    modified: list[str],
    timeout: int,
    project_stack: dict | None = None,
) -> tuple[int, list[str]]:
    """跨生态确定性构建修复 dispatcher。返回 (触达文件数, 触达文件相对路径列表)。

    触达数 >0 调用方重跑构建确认；路径列表（TD2606-C9）供调用方把【沙箱里】被修复的文件
    （含子任务写权 scope 之外的，如父 pom）回传本地，杜绝本地 diff 与沙箱编译两棵真值树分叉。

    生态集合由【权威栈画像 project_stack】决定（单一事实源；detect_stack 已小模型识别→大模型
    确认→KB 持久化，含混合项目/低置信模型兜底）；无画像时回退按 modified 扩展名。每个生态委托
    其事实标准 autofix：Java=项目源码自证前缀、Go=goimports、Rust=cargo fix、TS/前端=eslint --fix。
    任一生态工具缺失 → 该生态优雅跳过，不影响其它。
    """
    mods = [str(f).strip() for f in (modified or []) if str(f).strip()]
    go_files = [f for f in mods if f.endswith(".go")]
    ts_files = [f for f in mods if f.endswith(_TS_EXTS)]
    has_rust_files = any(f.endswith(".rs") for f in mods)
    stack_langs = _stack_repair_langs(project_stack)

    def eligible(lang: str, file_signal: bool) -> bool:
        # 有权威画像 → 以栈为准；无画像 → 回退该语言的文件扩展名信号
        return (lang in stack_langs) if stack_langs is not None else file_signal

    total = 0
    paths: list[str] = []

    def _accum(result: tuple[int, list[str]]) -> None:
        nonlocal total
        n, fs = result
        total += n
        for f in fs:
            if f and f not in paths:
                paths.append(f)

    # Java：错误信息里就带 .java 文件，无需 modified 列出 → file_signal=True
    if eligible("java", True):
        try:
            _accum(_attempt_import_repair(project_path, build_output, timeout))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[L1.2.1·repair] Java import-repair 异常(跳过): %s", exc)
        # #9 治本：跨 feature 包布局漂移（import 嵌套/错内部包，类实际在别的内部包）→ 据类真实
        # 内部包确定性重写 import。放在前缀 import-repair 之后、dep-repair 之前：先把【内部包漂移】
        # 重定向到真实产出包，剩下真缺的第三方再交 dep-repair；避免漂移内部包被误当"未就绪"BLOCKED
        # 等一个永不到来的生产者（#10 幽灵生产者）。唯一解才改、零解/歧义 fail-closed 交回 BLOCKED。
        try:
            _accum(_attempt_internal_import_drift_repair(project_path, build_output, timeout))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[L1.2.1·repair] Java import-drift-repair 异常(跳过): %s", exc)
        # 缺第三方依赖声明（import 了库但 module pom 没声明）→ 据 import 反查坐标补进 pom。
        # 放在 import 前缀修复之后、版本对账之前：先把"整个依赖没声明"补齐，版本问题再对账。
        # SWARM_WORKER_DEP_REPAIR=false 可关（仅此一类，留逃生阀）。
        if os.environ.get("SWARM_WORKER_DEP_REPAIR", "true").lower() not in ("false", "0", "no"):
            try:
                _accum(_attempt_dependency_repair(project_path, build_output, modified, timeout))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[L1.2.1·repair] dependency-repair 异常(跳过): %s", exc)
        # Maven 依赖版本不存在（worker 凭空写错版本号）→ 校正到最近有效版本
        try:
            _accum(_attempt_maven_version_repair(project_path, build_output, timeout))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[L1.2.1·repair] Maven version-repair 异常(跳过): %s", exc)
        # 模型臆造/拼错的方法/类名（isEmtpy→isEmpty 等）→ 据项目现存符号按编辑距离纠近邻
        try:
            _accum(_attempt_symbol_repair(project_path, build_output, modified, timeout))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[L1.2.1·repair] symbol-repair 异常(跳过): %s", exc)
    adapters = (
        ("go", bool(go_files), lambda: _repair_go(project_path, go_files, timeout)),
        ("rust", has_rust_files, lambda: _repair_rust(project_path, timeout)),
        ("ts", bool(ts_files), lambda: _repair_ts(project_path, ts_files, timeout)),
    )
    for lang, file_signal, fn in adapters:
        if eligible(lang, file_signal) and (file_signal or lang == "rust"):
            try:
                _accum(fn())
            except Exception as exc:  # noqa: BLE001
                logger.debug("[L1.2.1·repair] %s adapter 异常(跳过): %s", lang, exc)

    # 治本 A2 多栈：从项目【自身兄弟 manifest】找缺失依赖的权威坐标注入到缺它的 manifest
    # （Go/npm/Cargo 等价 Maven brain 侧 _inject_missing_maven_deps）。与上面工具级 adapter
    # （goimports/cargo fix/eslint）互补：那些解决"import 写法/格式"，这里解决"整个依赖没声明"。
    # 只用项目自证坐标、绝不臆造版本、非项目写死；触达 manifest 经 (count,paths) 回传(C9)。
    # 同 SWARM_WORKER_DEP_REPAIR 逃生阀（与 Java 侧 dependency-repair 同闸）。
    if os.environ.get("SWARM_WORKER_DEP_REPAIR", "true").lower() not in ("false", "0", "no"):
        from swarm.worker.sibling_dep_repair import repair_from_sibling_manifests
        _sib_stack = {"ts": "npm", "rust": "cargo", "go": "go"}
        for lang, file_signal, _fn in adapters:
            stack_key = _sib_stack.get(lang)
            if stack_key and eligible(lang, file_signal):
                try:
                    _accum(repair_from_sibling_manifests(project_path, build_output, mods, stack_key))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[L1.2.1·repair] A2 %s sibling-dep 异常(跳过): %s", stack_key, exc)
    return total, paths


# audit #37/#38：编译/lint 每次最多处理的文件数。原为硬编码 20，大变更集会遗漏后续
# 文件的编译/lint 错误。改为可配（SWARM_WORKER_L1_MAX_FILES，默认 20），并在截断时告警。
def _max_files_per_check() -> int:
    try:
        return max(1, int(os.environ.get("SWARM_WORKER_L1_MAX_FILES", "20")))
    except ValueError:
        return 20


def _max_build_repair_rounds() -> int:
    """确定性构建修复的【幂等收敛循环】最大轮数（SWARM_WORKER_BUILD_REPAIR_ROUNDS，默认 4）。

    编译器错误掩蔽是级联的：一遍 repair 修掉可见 typo/缺 import 后，rerun 才暴露原先被上游
    错误掩蔽的下一批 cannot-find-symbol。需多轮「修→重跑→再修」直到收敛。纯确定性、单调
    （perl 改了不会被自己改回），有界即可，4 轮足以吃下实测最深级联，又不会空转太久。"""
    try:
        return max(1, int(os.environ.get("SWARM_WORKER_BUILD_REPAIR_ROUNDS", "4")))
    except ValueError:
        return 4


def _max_build_repair_seconds() -> float:
    """收敛循环【墙钟上界】秒（SWARM_WORKER_BUILD_REPAIR_MAX_SECONDS，默认 900）。

    轮数上界之外再加墙钟闸：每轮含一次全量 mvn 重跑（可达 300s）+ 网络反查，最坏 4 轮可逼近
    20min，跑在同步确定性闸门里、worker 总预算无从中途打断 → 加墙钟硬上界防 runaway（默认 900s
    够 1-2 次正常收敛重跑，又封死病态空转）。一旦超界即停，交后续 fail/BLOCKED 分类。"""
    try:
        return max(60.0, float(os.environ.get("SWARM_WORKER_BUILD_REPAIR_MAX_SECONDS", "900")))
    except ValueError:
        return 900.0


def _repair_loop_budget(deadline: float | None) -> float:
    """C1（阶段4，登记册 §四）：repair 收敛循环墙钟 = min(独立墙钟上界, worker 剩余预算)。

    此前独立 900s 与 worker 总预算解耦——A5 只在闸门入口查一次布尔快照，进门后
    build 300s + repair 900s×每轮全量重跑可达 ~35min，预算无从中途打断。"""
    cap = _max_build_repair_seconds()
    if deadline is None:
        return cap
    return max(0.0, min(cap, deadline - _time.monotonic()))


def _stage_timeout(base: int, deadline: float | None) -> int:
    """C1：阶段命令超时钳到剩余预算（不再 max(timeout,300) 冲破 deadline）。
    下限 60s 保命令本身可用；deadline 已过的情形由各阶段前置检查拦截，不到这里。"""
    if deadline is None:
        return int(base)
    return max(60, min(int(base), int(deadline - _time.monotonic())))


def _cap_files(files: list[str], kind: str) -> list[str]:
    """按上限截断文件列表；截断时告警（避免静默遗漏后续文件的检查）。"""
    cap = _max_files_per_check()
    if len(files) > cap:
        logger.warning(
            "[L1] %s 文件数 %d 超过上限 %d，仅检查前 %d 个（其余未覆盖，可调 "
            "SWARM_WORKER_L1_MAX_FILES）", kind, len(files), cap, cap,
        )
        return files[:cap]
    return files


def _run_l1_command(command: str, project_path: str, timeout: int = 120) -> tuple[int, str]:
    """L1 命令执行器：沙箱优先(sandbox-first)。

    若有活跃沙箱上下文 → 在沙箱里跑(那里有 mvn/java/go/cargo 等工具链)，
    否则本地 subprocess。返回 (exit_code, output)。

    这是 L1 确定性闸门跑 build/test/verify 的统一入口——保证 Java/Go/Rust 等
    需要工具链的命令在沙箱里真实执行(本机通常没装这些工具链)。
    """
    sandbox = manager = None
    try:
        from swarm.tools.build_tools import get_sandbox_context
        sandbox, manager = get_sandbox_context()
    except Exception:  # noqa: BLE001
        sandbox = manager = None

    if sandbox is not None and manager is not None and hasattr(manager, "run_command"):
        # 沙箱里跑：cd 到远程工作目录
        try:
            from swarm.config.settings import get_config
            remote = get_config().sandbox.sandbox_remote_workdir
        except Exception:  # noqa: BLE001
            remote = "/workspace"
        cr = manager.run_command(sandbox, f"cd {remote} && {command}", timeout=timeout)
        out = (cr.stdout or "") + (("\n" + cr.stderr) if cr.stderr else "")
        # run_command 成功 success=True；失败时 error 形如 exit_code=N
        if cr.success:
            return 0, out
        ec = 1
        if cr.error and "exit_code=" in cr.error:
            try:
                ec = int(cr.error.split("exit_code=")[1].split()[0])
            except (ValueError, IndexError):
                ec = 1
        return ec, out + (f"\n{cr.error}" if cr.error else "")

    # 本地兜底
    # 复核 R23-3 治本：本地兜底在【宿主机 shell】跑 Brain 下发命令，必须过命令黑名单(与
    # build_tools._run_local 对称)，否则沙箱降级/ContextVar 丢失时隔离边界消失。黑名单本身
    # fail-closed 回退内置基线；此处不可用/被拦 → 直接判失败(126)，不裸跑到宿主机。
    try:
        from swarm.config import command_blacklist_store
        _allowed, _reason = command_blacklist_store.check_command_hardened(command)
    except Exception as _bexc:  # noqa: BLE001
        return 126, f"命令黑名单校验失败，本地兜底拒绝执行(fail-closed): {_bexc}"
    if not _allowed:
        return 126, f"命令被黑名单拦截(本地兜底不放行): {_reason}"
    try:
        proc = subprocess.run(
            normalize_python_cmd(command, py_bin=_python_bin()), cwd=project_path, shell=True,
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "command timeout"
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


# ── 沙箱优先的确定性检查执行（A-P1-10）──
# compile/lint 旧实现一律本地 subprocess：沙箱模式下本地只 pull-back 了【可写文件】，
# 工程其余部分(依赖/兄弟源码/manifest)不在本地 → 整树工具(tsc/go vet/cargo clippy/
# eslint/checkstyle)在【部分树】上跑出假 PASS(找不到东西→exit 0)或假错(解析不到 import)。
# 修复：把这些"需要完整工程树+目标工具链"的检查走与 _run_l1_command 同款沙箱优先，
# 在沙箱里对真实完整树执行；无沙箱才本地兜底。(逐文件的 py_compile/ruff/格式化器仍
# 本地——可写文件已 pull-back，逐文件检查本地即正确，且 ruff 是本仓工具未必在目标沙箱。)

def _sandbox_ctx() -> tuple[Any, Any, str] | None:
    """返回 (sandbox, manager, remote_workdir) 或 None(无活跃沙箱)。"""
    try:
        from swarm.tools.build_tools import get_sandbox_context
        sandbox, manager = get_sandbox_context()
    except Exception:  # noqa: BLE001
        return None
    if sandbox is None or manager is None or not hasattr(manager, "run_command"):
        return None
    try:
        from swarm.config.settings import get_config
        remote = get_config().sandbox.sandbox_remote_workdir
    except Exception:  # noqa: BLE001
        remote = "/workspace"
    return sandbox, manager, remote


def _push_manifests_to_sandbox(project_path: str, manifests: list[str]) -> int:
    """把 reconcile 在【本地 project_path】改过的聚合清单推进【沙箱】，返回上传成功数。

    治本 #11(b)（round18/19 实测头号交付卡点）：模块注册 reconcile 走纯 Python
    `Path.write_text`，改的是本地 project_path 的 pom；而 build gate（`mvn -pl <mod>`）在
    远端沙箱跑，读的是 bootstrap 上传的旧副本。两份在同一次 L1 内从不同步 → 注册对构建
    【永久不可见】→ `Could not find the selected project in the reactor`（reconcile 明明
    log 了"补注册"）。其它确定性 repair（import/version/goimports 全走 `_run_l1_command`
    在沙箱内改）本就对构建可见；唯独 reconcile 是本地写的例外——这里把它对齐：推进沙箱。

    无活跃沙箱（本地模式）→ build 直接读 project_path，无需 push，安全返回 0。
    sync 失败（infra 瞬时）不致命：不推进则 build 会 reactor-not-found，交后续构建失败
    分类（含 _is_infra_failure 退避）处理，不在此吞成假通过。
    """
    if not manifests:
        return 0
    ctx = _sandbox_ctx()
    if ctx is None:
        return 0
    sandbox, manager, remote = ctx
    if not hasattr(manager, "sync_files_to_sandbox"):
        return 0
    try:
        from pathlib import Path as _P
        rels = [m for m in manifests if (_P(project_path) / m).is_file()]
        if not rels:
            return 0
        stats = manager.sync_files_to_sandbox(sandbox, project_path, rels, remote)
        uploaded = int((stats or {}).get("uploaded", 0))
        if uploaded:
            # D57：沙箱清单集变化（本函数是 L1 中途唯一新增清单的路径）→ 失效在场性缓存
            _invalidate_manifest_cache()
            logger.info(
                "[L1.2.1·module-reg] 已把 reconcile 注册的聚合清单推进沙箱 %d 个"
                "（令 -pl 当场可解析，杜绝 reactor not-found）: %s", uploaded, rels,
            )
        for _err in ((stats or {}).get("errors") or [])[:3]:
            logger.warning("[L1.2.1·module-reg] 清单推进沙箱警告: %s", _err)
        return uploaded
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[L1.2.1·module-reg] 清单推进沙箱失败(不致命,交 build 失败分类): %s", exc)
        return 0


def _run_check_split(shell_cmd: str, project_path: str, timeout: int = 60) -> tuple[int, str, str]:
    """运行确定性检查命令，沙箱优先，返回 (exit_code, stdout, stderr)。

    stdout/stderr 保持分离(不像 _run_l1_command 合并)，以便结构化解析 eslint/tsc 的
    JSON 输出。活跃沙箱 → cd 远程工作目录在【完整真实树】上执行；否则本地兜底。
    """
    ctx = _sandbox_ctx()
    if ctx is not None:
        sandbox, manager, remote = ctx
        cr = manager.run_command(sandbox, f"cd {remote} && {shell_cmd}", timeout=timeout)
        out, err = (cr.stdout or ""), (cr.stderr or "")
        if cr.success:
            return 0, out, err
        ec = 1
        if cr.error and "exit_code=" in cr.error:
            try:
                ec = int(cr.error.split("exit_code=")[1].split()[0])
            except (ValueError, IndexError):
                ec = 1
        if cr.error and not err:
            err = cr.error
        return ec, out, err
    # 本地兜底
    # 复核 R23-3 治本(对称补齐)：本地兜底同样在【宿主机 shell】跑 Brain 下发的检查命令
    # (tsc/eslint/go vet…)，必须过命令黑名单(与 _run_l1_command / build_tools._run_local
    # 对称)，否则沙箱降级/ContextVar 丢失时隔离边界消失。normalize 可能改写命令，故对
    # 【真正传给 shell 的命令串】校验(消除 check/run 口径漂移)；不可用/被拦 → fail-closed 126。
    exec_cmd = normalize_python_cmd(shell_cmd, py_bin=_python_bin())
    try:
        from swarm.config import command_blacklist_store
        _allowed, _reason = command_blacklist_store.check_command_hardened(exec_cmd)
    except Exception as _bexc:  # noqa: BLE001
        return 126, "", f"命令黑名单校验失败，本地兜底拒绝执行(fail-closed): {_bexc}"
    if not _allowed:
        return 126, "", f"命令被黑名单拦截(本地兜底不放行): {_reason}"
    try:
        proc = subprocess.run(
            exec_cmd, cwd=project_path, shell=True,
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, (proc.stdout or ""), (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "", "command timeout"
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


# A7(round11)：缓存只读的项目【符号/包】全树扫描。VERIFYING/PRODUCING 等多阶段会重跑同一条
# 60-120s 的 `grep -r … --include='*.java'` 大扫描（取证：同一沙箱 4000-符号 grep 重复 3×，
# 纯烧预算）。按【源文件 size+mtime 签名】缓存：任一 .java/.kt/.scala 变动→签名变→自动失效重扫，
# 无陈旧风险（不会拿过期符号表去改代码）。通用于任何 JVM 栈，无正确性 trade-off。
_SCAN_CACHE: dict[tuple[str, str], tuple[str, tuple[int, str, str]]] = {}
_SCAN_SIG_CMD = (
    "find . \\( -name '*.java' -o -name '*.kt' -o -name '*.scala' \\) -print0 2>/dev/null "
    "| xargs -0 stat -c '%n|%s|%Y' 2>/dev/null | sort | cksum"
)


def _cached_scan(scan_cmd: str, project_path: str, timeout: int = 60) -> tuple[int, str, str]:
    """带文件状态签名失效的 _run_check_split 包装，专给只读全树符号/包扫描省重复预算（A7）。"""
    try:
        _sec, sig_out, _e = _run_check_split(_SCAN_SIG_CMD, project_path, timeout=min(timeout, 15))
        sig = (sig_out or "").strip()
    except Exception:  # noqa: BLE001
        sig = ""  # 签名拿不到 → 不缓存，照常扫描（安全兜底，绝不返回可能陈旧的结果）
    key = (project_path, scan_cmd)
    if sig:
        cached = _SCAN_CACHE.get(key)
        if cached and cached[0] == sig:
            return cached[1]
    result = _run_check_split(scan_cmd, project_path, timeout=timeout)
    if sig:
        if len(_SCAN_CACHE) > 32:   # 有界，防长进程多沙箱累积
            _SCAN_CACHE.clear()
        _SCAN_CACHE[key] = (sig, result)
    return result


# D57：manifest 在场性【单次 L1 run 内】缓存——单次 run_l1_pipeline 会对多组 manifest
# 做 5-8 趟沙箱 find（每趟一次远端往返）。代号（generation）在 run_l1_pipeline 入口与
# _push_manifests_to_sandbox（唯一会在 L1 中途新增沙箱清单的路径）处自增失效；探测异常
# 不缓存（保持旧的保守 False 且下次重探）。本地路径 os.path.isfile 极廉，不缓存。
_MANIFEST_CACHE_GEN = 0
_MANIFEST_PRESENT_CACHE: dict[tuple, bool] = {}


def _invalidate_manifest_cache() -> None:
    global _MANIFEST_CACHE_GEN
    _MANIFEST_CACHE_GEN += 1
    _MANIFEST_PRESENT_CACHE.clear()


def _manifest_present(manifests: tuple[str, ...], project_path: str) -> bool:
    """工程 manifest(go.mod/Cargo.toml/package.json…)是否存在，沙箱优先。

    沙箱模式下本地只有可写文件，manifest 多半不在本地——旧的 os.path.isfile(本地)
    会误判"无 manifest 而跳过 lint"。沙箱里在远程工作目录(深度 3 内)查。
    D57：同一次 L1 run 内同 (sandbox, manifests) 探测结果缓存（见 _invalidate_manifest_cache）。
    """
    ctx = _sandbox_ctx()
    if ctx is not None:
        sandbox, manager, remote = ctx
        _key = (_MANIFEST_CACHE_GEN, getattr(sandbox, "sandbox_id", id(sandbox)), tuple(manifests))
        cached = _MANIFEST_PRESENT_CACHE.get(_key)
        if cached is not None:
            return cached
        names = " -o ".join(f"-name {shlex.quote(m)}" for m in manifests)
        try:
            cr = manager.run_command(
                sandbox,
                f"find {remote} -maxdepth 3 \\( {names} \\) -print -quit 2>/dev/null | head -1",
                timeout=20,
            )
            present = bool((cr.stdout or "").strip())
            _MANIFEST_PRESENT_CACHE[_key] = present
            return present
        except Exception:  # noqa: BLE001
            return False  # 异常不缓存：保守 False 且下次重探（与旧行为一致）
    return any(os.path.isfile(os.path.join(project_path, m)) for m in manifests)


# ── 基础设施/工具瞬时错误识别（A-P1-09）──
# Go/Rust/Java lint 旧实现"非0退出 + 任意 stderr 即 has_error"，把【无网拉依赖、工具缺失、
# 文件锁、磁盘满、系统资源】等瞬时基础设施/工具故障误判成"代码能力失败"→ 触发错误降级
# (换更弱模型/abandon)。修复：lint 输出命中下列【明确属基础设施/工具】的标记时，判 skip
# (非 error)。只收"明确非代码问题"的标记——通用编译错误(模型引错符号)仍算真错误，不放过。
_LINT_INFRA_MARKERS: tuple[str, ...] = (
    # 网络/拉依赖
    "dial tcp", "connection refused", "connection reset", "i/o timeout",
    "tls handshake timeout", "network is unreachable", "could not resolve host",
    "temporary failure in name resolution", "no such host", "proxyconnect",
    "502 bad gateway", "503 service", "504 gateway", "timeout was reached",
    "operation timed out", "error sending request",
    "go: downloading", "go: download", "reading https://", "could not download",
    "failed to download", "failed to fetch", "spurious network error",
    "registry index was not found", "unable to get packages",
    # 文件锁/并发
    "blocking waiting for file lock", "waiting for file lock",
    # 系统资源
    "no space left on device", "read-only file system", "cannot allocate memory",
    "out of memory", "disk quota exceeded", "too many open files",
    # 工具本身缺失(目标沙箱未必装 go/cargo/checkstyle/eslint)
    "command not found", "executable file not found", ": not found",
    "is not recognized as an internal or external command",
)


def _is_infra_failure(text: str) -> bool:
    """lint/编译输出是否为基础设施/工具瞬时故障(非代码能力问题)。"""
    if not text:
        return False
    low = text.lower()
    return any(mk in low for mk in _LINT_INFRA_MARKERS)


# 构建/测试命令 → 该命令运行所【必需的工程描述文件】。缺这些文件时命令必然失败
# (如 mvn 无 pom.xml、npm 无 package.json)，应优雅跳过而非误判为产出不合格。
_BUILD_TOOL_MANIFESTS: dict[str, tuple[str, ...]] = {
    "mvn": ("pom.xml",),
    "gradle": ("build.gradle", "build.gradle.kts", "settings.gradle"),
    "./gradlew": ("build.gradle", "build.gradle.kts", "settings.gradle"),
    "npm": ("package.json",),
    "yarn": ("package.json",),
    "pnpm": ("package.json",),
    "npx": ("package.json",),
    "go": ("go.mod",),
    "cargo": ("Cargo.toml",),
}


def _derive_full_build_command(
    project_path: str, modified: list[str], project_stack: dict | None
) -> str:
    """根因#1 通用版（范式化，非 Java/mvn 写死）：子任务改了某栈源码、但 Brain 没下发
    build_command 时，据【权威栈画像 project_stack.build / 工程清单 + 改动文件语言】派生该栈的
    【全量构建】命令，让生产者 L1 闸门与下游一样强——任何栈皆然（Java-maven/gradle、Go、Rust、
    前端 TS）。单文件语法检查（_compile_files）抓不到需全工程上下文才暴露的类型/跨文件/符号错；
    全量构建才能在【能改它的生产者】当场抓当场修，不漏到无权修的下游。

    命令的工程文件可用性由 _build_cmd_applicable 兜底把关；无匹配栈返回 ''（不臆造）。
    """
    import os
    mods = [str(f).strip() for f in (modified or []) if str(f).strip()]
    if not mods:
        return ""
    build = ((project_stack or {}).get("build") or "").strip().lower()

    def has(*names: str) -> bool:
        # A4 治本：沙箱模式本地树只有 pull-back 的可写文件，根 manifest(pom/go.mod/…)不在
        # 本地——旧的 os.path.isfile(本地) 会漏判 → derive 返回 "" → build 闸门跳过 → 假绿。
        # 改走沙箱优先的 _manifest_present（与 lint/_build_cmd_applicable 同源），跨栈一致。
        return _manifest_present(tuple(names), project_path)

    def ext(*exts: str) -> bool:
        return any(f.endswith(exts) for f in mods)

    if ext(".java", ".kt", ".scala"):
        if build == "gradle" or (not build and not has("pom.xml")
                                 and has("build.gradle", "build.gradle.kts")):
            return "./gradlew -q compileJava" if has("gradlew") else "gradle -q compileJava"
        if build == "maven" or has("pom.xml"):
            return "mvn -q compile"  # _scope_maven_command 据 modified 收窄到 -pl <module> -am
    if ext(".go") and (build == "go" or has("go.mod")):
        return "go build ./..."
    if ext(".rs") and (build == "cargo" or has("Cargo.toml")):
        return "cargo build -q"
    if ext(".ts", ".tsx") and has("tsconfig.json"):
        return "tsc --noEmit"
    # round18 P2 治本：纯 pom/无可编译源码子任务——"无 Java 即判负"会返回 None→维持 prior 未通过
    # →BLOCKED 空转（st-30 变体 5065fe04/st-29-2 现场，产物其实 mvn validate 通过）。改走
    # `mvn validate` 给真确定性校验（pom 结构 + reactor 可解析性）——版本缺失/reactor 断裂会
    # 如实 fail（fail-closed）。仅当【无任何可编译源码】且改动含 pom.xml 时兜底，不抢 compile。
    if (
        not ext(".java", ".kt", ".scala", ".go", ".rs", ".ts", ".tsx")
        and any(f.replace("\\", "/").rsplit("/", 1)[-1] == "pom.xml" for f in mods)
        and (build == "maven" or has("pom.xml"))
    ):
        return "mvn -q validate"  # _scope_maven_command 据 modified 收窄到 -pl <module> -am
    return ""


def _build_cmd_applicable(command: str, project_path: str) -> bool:
    """判断 build/test 命令的工具链工程文件是否存在(沙箱优先)。

    缺工程文件(mvn 无 pom / npm 无 package.json)时命令必失败，此时应跳过该闸门，
    不能把"工具不适用"误判成"产出不合格"。返回 True=可执行；False=应跳过。
    """
    tokens = command.strip().split()
    if not tokens:
        return False
    tool = tokens[0]
    manifests = _BUILD_TOOL_MANIFESTS.get(tool)
    if not manifests:
        return True  # 未知工具(如直接 python/pytest)不做工程文件校验，照常跑
    # 沙箱优先：在远程工作目录递归找工程文件
    sandbox = manager = None
    try:
        from swarm.tools.build_tools import get_sandbox_context
        sandbox, manager = get_sandbox_context()
    except Exception:  # noqa: BLE001
        sandbox = manager = None
    if sandbox is not None and manager is not None and hasattr(manager, "run_command"):
        try:
            from swarm.config.settings import get_config
            remote = get_config().sandbox.sandbox_remote_workdir
        except Exception:  # noqa: BLE001
            remote = "/workspace"
        # 任一 manifest 在 workspace 下存在即视为适用
        names = " -o ".join(f"-name {shlex.quote(m)}" for m in manifests)
        cr = manager.run_command(
            sandbox,
            f"find {remote} -maxdepth 3 \\( {names} \\) -print -quit 2>/dev/null | head -1",
            timeout=20,
        )
        return bool((cr.stdout or "").strip())
    # 本地兜底
    from pathlib import Path as _P
    root = _P(project_path)
    return any(any(root.rglob(m)) for m in manifests)



def _scope_match(fp: str, w: str) -> bool:
    """路径感知的 scope 匹配（audit #31 修复）。

    旧实现 `fp.endswith(w) or w.endswith(fp)` 是任意字符后缀匹配，会误放行：
    scope `main.py` 放行 `src/main.py`、scope `src/main.py` 放行 `2src/main.py` 等。
    新规则按【路径段】对齐，避免子串误判：
      1. 规范化(去 ./、统一 /)；
      2. 完全相等 → 匹配；
      3. w 以 / 结尾(目录 scope) → fp 在该目录下 → 匹配；
      4. fp 以 w 结尾且边界是路径分隔符(w 是 fp 的完整尾部路径段序列) → 匹配
         (容忍 diff 路径带仓库根前缀，如 scope 'src/a.py' 匹配 'repo/src/a.py')。
    """
    def norm(p: str) -> str:
        p = p.strip().replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        return p.strip("/")

    f, ww = norm(fp), norm(w)
    if not f or not ww:
        return False
    if f == ww:
        return True
    # 目录 scope：w 原始以 / 结尾，或作为 f 的祖先目录段
    if f.startswith(ww + "/"):
        return True
    # fp 带额外根前缀：仅当 w 本身是【多段路径】(含 /) 时容忍根前缀对齐，
    # 避免单段 basename(如 'main.py') 尾匹配任意目录下同名文件(audit #31 核心)。
    if "/" in ww and f.endswith("/" + ww):
        return True
    return False


def _scope_violations(
    diff: str, scope: FileScope, extra_allowed: set[str] | None = None
) -> list[str]:
    modified = files_from_unified_diff(diff)
    # 可写权限 = writable + create_files + delete_files（FileScope 契约，见 is_writable）。
    # bug 修复(task 9da731ab)：原仅检查 writable，把【新建文件】(create_files)误判越权 →
    # tech_design file_plan 含新建文件的任务必然 L1 失败 → replan 死循环。create_files 是合法可写。
    allowed = set(scope.writable or []) | set(getattr(scope, "create_files", []) or []) \
        | set(getattr(scope, "delete_files", []) or [])
    # round18 P0-B 治本：确定性修复机制(module-registration 自愈 / version-repair)合法触达的
    # scope 外文件(典型：父/根 pom)由 executor._repaired_extra_paths 透传进来。它们【非 worker
    # 越权写命令】——真机制见 test_l1_scope_repaired_paths_round18：VERIFYING 时 scope 复核先于
    # 注册跑(3 文件 scope_ok=True)，但注册把 pom 记入 repaired → Phase4 的 _get_git_diff 把 pom
    # 纳入 diff(4 文件) → 若不排除，Phase4 scope 复核见 pom 越 scope → 整份判死误杀有效产出。
    # 故 scope 只按 worker 实际写命令判定，排除确定性修复触达的路径（fail-closed：worker 自己
    # 越权的 scope 外文件不在 repaired 集合，仍被抓）。
    if extra_allowed:
        allowed |= {p for p in extra_allowed if p}
    if not allowed:
        return []
    violations = []
    for fp in modified:
        if not any(_scope_match(fp, w) for w in allowed):
            violations.append(fp)
    return violations


def _python_bin() -> str:
    """寻找可用的 Python 解释器。

    优先级：项目 .venv > 当前运行解释器(sys.executable) > python3 > python。
    用 sys.executable 而非裸 python3，确保拿到带项目依赖(pytest 等)的解释器，
    避免命中系统 python3(无 pytest)导致测试误判失败。
    """
    import sys
    if getattr(sys, "executable", ""):
        return sys.executable
    for name in ("python3", "python"):
        if shutil.which(name):
            return name
    return "python"  # 回退，让后续报错自然暴露


def _compile_files(project_path: str, files: list[str], *, timeout: int = 60) -> tuple[bool, str]:
    py_files = [f for f in files if f.endswith(".py")]
    if py_files:
        py_bin = _python_bin()
        cmd = f"{py_bin} -m py_compile " + " ".join(shlex.quote(f) for f in _cap_files(py_files, "py_compile"))
        try:
            proc = subprocess.run(
                cmd,
                cwd=project_path,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if proc.returncode != 0:
                return False, proc.stderr or proc.stdout or "py_compile failed"
        except Exception as exc:
            # audit #10：保留完整 traceback 便于诊断编译为何失败（原仅 str(exc) 丢栈）
            logger.warning("[L1.2] py_compile 执行异常: %s", exc, exc_info=True)
            return False, f"py_compile execution error: {exc}"

    js_ts = [f for f in files if f.endswith((".ts", ".tsx", ".js", ".jsx"))]
    # tsc --noEmit 需要【完整工程树+node_modules】才能解析 import → 走沙箱优先(A-P1-10)。
    # 沙箱模式下 package.json 不在本地，用 _manifest_present 沙箱感知判定。
    if js_ts and _manifest_present(("package.json",), project_path):
        try:
            rc, out, err = _run_check_split("npx tsc --noEmit --pretty false", project_path, timeout=timeout)
            combined = (out or "") + (("\n" + err) if err else "")
            # 基础设施/工具瞬时错误(无网装 typescript、tsc 缺失)不算编译失败(A-P1-09)
            if rc != 0 and _is_infra_failure(combined):
                logger.warning("[L1.2] tsc 基础设施/工具瞬时错误，跳过编译闸门(非能力失败): %s", combined[:200])
            elif rc != 0:
                # A2 治本(fail-closed)：任何【非 infra】的 tsc 失败都判编译不过——
                # 不再依赖字面 "error TS" 子串。解析错误/声明错误/本地化(中文)输出/自定义报错
                # 都不含该串，旧代码会落到末尾 return True 静默假绿。rc!=0 且非 infra = 真失败。
                return False, (combined.strip()[:1000] or f"tsc failed rc={rc}")
        except Exception as exc:
            # R23-2 治本：tsc 执行【异常】旧代码只 log 后落到末尾 return True 假绿。区分：
            # 明确 infra（npx/tsc 缺失、无网装 typescript）→ 跳过闸门(非能力失败)；其余(超时/意外崩溃)
            # → fail-closed 判不过（超时可能掩盖真 hang，不能当编译通过）。
            _exc_txt = f"{type(exc).__name__}: {exc}"
            # FileNotFoundError=工具/命令缺失(npx/node 不在)=明确 infra；再叠加文本模式判定。
            if isinstance(exc, FileNotFoundError) or _is_infra_failure(_exc_txt):
                logger.warning("[L1.2] tsc 工具/基础设施异常，跳过编译闸门(非能力失败): %s", exc)
            else:
                logger.warning("[L1.2] tsc 执行异常(非 infra)，fail-closed 判未通过: %s", exc)
                return False, f"tsc 执行异常: {_exc_txt}"[:1000]

    return True, "compile ok"


# ── L1.2.5 lint 阶段 ──

def _find_ruff_bin() -> str | None:
    """查找 ruff 可执行文件，找不到返回 None。"""
    # 优先用 venv 内的 ruff
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".venv", "bin", "ruff"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    # 系统 PATH
    found = shutil.which("ruff")
    if found:
        return found
    return None


def _find_tool(name: str) -> str | None:
    """通用工具探测（shutil.which），找不到返回 None。"""
    return shutil.which(name)


# ── 语言分派: per-linter 辅助 ──

def _lint_python(project_path: str, py_files: list[str], *, timeout: int = 60) -> tuple[bool, list[str], list[dict]]:
    """Python: ruff check。返回 (has_error, messages, issues)。"""
    has_error = False
    messages: list[str] = []
    issues: list[dict] = []

    ruff_bin = _find_ruff_bin()
    if not ruff_bin:
        messages.append("ruff 未安装，跳过 Python lint")
        return has_error, messages, issues

    for fp in _cap_files(py_files, "pyflakes"):
        try:
            proc = subprocess.run(
                [ruff_bin, "check", fp, "--output-format=json"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            # ruff 退出码: 0=无问题, 1=有问题, 2=运行错误
            if proc.returncode == 2:
                messages.append(f"ruff 运行错误({fp}): {proc.stderr[:200]}")
                continue
            if proc.stdout.strip():
                try:
                    findings = json.loads(proc.stdout)
                except json.JSONDecodeError:
                    findings = []
                for item in findings:
                    # ruff JSON: code 可能是 str("F401"/"invalid-syntax") 或旧版 dict{value}
                    raw_code = item.get("code")
                    if isinstance(raw_code, dict):
                        rule_code = raw_code.get("value", "") or ""
                    else:
                        rule_code = raw_code or ""
                    issue_entry = {
                        "file": fp,
                        "line": item.get("location", {}).get("row"),
                        "code": rule_code,
                        "message": item.get("message", ""),
                    }
                    # 优先用 ruff 自报的 severity；否则按代码前缀判定。
                    # invalid-syntax / E9(语法) / F4(导入*等致命) / F82(未定义名) 视为 error。
                    ruff_sev = (item.get("severity") or "").lower()
                    is_error = (
                        ruff_sev == "error"
                        or rule_code == "invalid-syntax"
                        or rule_code.startswith(("E9", "F4", "F82", "F7"))
                    )
                    if is_error:
                        issue_entry["severity"] = "error"
                        has_error = True
                    else:
                        issue_entry["severity"] = "warning"
                    issues.append(issue_entry)
        except subprocess.TimeoutExpired:
            messages.append(f"ruff 超时({fp})")
        except Exception as exc:
            messages.append(f"ruff 跳过({fp}): {exc}")
    return has_error, messages, issues


def _lint_js_ts(project_path: str, js_ts: list[str], *, timeout: int = 60) -> tuple[bool, list[str], list[dict]]:
    """JS/TS: eslint（有配置才跑）。返回 (has_error, messages, issues)。"""
    has_error = False
    messages: list[str] = []
    issues: list[dict] = []

    # eslint 需完整工程(node_modules/共享配置)→ 沙箱优先(A-P1-10)。沙箱模式下配置文件
    # 不在本地，用 _manifest_present 沙箱感知判定。
    has_eslint_config = _manifest_present(
        (".eslintrc.js", ".eslintrc.json", ".eslintrc.yml", ".eslintrc", "eslint.config.js"),
        project_path,
    )
    if not has_eslint_config:
        messages.append("项目无 eslint 配置，跳过 JS/TS lint")
        return has_error, messages, issues

    try:
        rc, out, err = _run_check_split(
            "npx eslint --format json " + " ".join(shlex.quote(f) for f in _cap_files(js_ts, "eslint")),
            project_path,
            timeout=timeout,
        )
        # eslint 退出码: 0=无问题, 1=有问题, 2=运行错误
        if rc == 124:
            messages.append("eslint 超时")
        elif rc != 0 and _is_infra_failure((err or "") + (out or "")):
            # 基础设施/工具瞬时错误(无网装 eslint、网络拉插件)→ skip 非 error(A-P1-09)
            messages.append(f"eslint 基础设施/工具瞬时错误，跳过(非能力失败): {(err or out)[:200]}")
        elif rc == 2 and not out.strip():
            messages.append(f"eslint 运行错误: {err[:200]}")
        elif out.strip():
            try:
                eslint_results = json.loads(out)
                for file_result in eslint_results:
                    for msg in file_result.get("messages", []):
                        sev = "error" if msg.get("severity") == 2 else "warning"
                        issues.append({
                            "file": file_result.get("filePath", ""),
                            "line": msg.get("line"),
                            "code": msg.get("ruleId", ""),
                            "message": msg.get("message", ""),
                            "severity": sev,
                        })
                        if sev == "error":
                            has_error = True
            except json.JSONDecodeError:
                messages.append("eslint 输出解析失败")
    except subprocess.TimeoutExpired:
        messages.append("eslint 超时")
    except Exception as exc:
        messages.append(f"eslint 跳过: {exc}")
    return has_error, messages, issues


def _lint_line_based(
    project_path: str,
    *,
    tool: str,
    lang: str,
    label: str,
    command: str,
    timeout: int,
    parse_line,
    manifest: tuple[str, ...] | None = None,
    manifest_hint: str = "",
    only_error_if_issues: bool = False,
    sandbox_precheck: bool = False,
) -> tuple[bool, list[str], list[dict]]:
    """Go/Rust/Java 系【整工程 check → 解析 stderr 行 → error issues】的公共模板（round24 A3）。

    三语言共享：工具在场守卫(沙箱优先 A-P1-10)、manifest 守卫、_run_check_split、
    rc==124 超时 / _is_infra_failure skip(A-P1-09) / 解析分支。差异经参数注入：
      - tool/lang/label：工具名 / 语言名(skip 文案) / 命令标签(超时/跳过文案)
      - command：整工程检查命令
      - parse_line(line)->dict|None：逐行解析成 issue（None=跳过该行，如 rust 摘要行）
      - manifest/manifest_hint：需在场的工程清单文件（Java 无 manifest 走 sandbox_precheck）
      - only_error_if_issues：True=仅当解析出 issue 才 has_error(Rust)；False=进解析分支即 error(Go/Java)
      - sandbox_precheck：沙箱内先探工具在场(Java checkstyle 多半未装，省 exit 127 白跑)
    """
    has_error = False
    messages: list[str] = []
    issues: list[dict] = []

    if _sandbox_ctx() is None and not _find_tool(tool):
        messages.append(f"{tool} 未安装，跳过 {lang} lint")
        return has_error, messages, issues

    if manifest and not _manifest_present(manifest, project_path):
        messages.append(f"项目无 {manifest_hint}，跳过 {lang} lint")
        return has_error, messages, issues

    if sandbox_precheck and _sandbox_ctx() is not None:
        _pc, _po = _run_l1_command(
            f"command -v {tool} >/dev/null 2>&1 && echo __HAS__ || echo __NO__",
            project_path, timeout=15,
        )
        if "__HAS__" not in (_po or ""):
            messages.append(f"{tool} 未安装(沙箱)，跳过 {lang} lint")
            return has_error, messages, issues

    try:
        rc, out, err = _run_check_split(command, project_path, timeout=timeout)
        err_output = (err or "").strip() or (out or "").strip()
        if rc == 124:
            messages.append(f"{label} 超时")
        elif rc != 0 and _is_infra_failure(err_output):
            # 无网/工具缺失等基础设施瞬时错误 → skip 非 error(A-P1-09)，避免错误降级
            messages.append(f"{label} 基础设施/工具瞬时错误，跳过(非能力失败): {err_output[:200]}")
        elif rc != 0 and err_output:
            for line in err_output.splitlines():
                line = line.strip()
                if not line:
                    continue
                entry = parse_line(line)
                if entry is not None:
                    issues.append(entry)
            has_error = bool(issues) if only_error_if_issues else True
    except Exception as exc:
        messages.append(f"{label} 跳过: {exc}")
    return has_error, messages, issues


def _lint_go(project_path: str, go_files: list[str], *, timeout: int = 60) -> tuple[bool, list[str], list[dict]]:
    """Go: go vet ./...（在 project_path 跑；非0退出且有 error 输出才算 has_error）。"""
    def _parse(line: str) -> dict:
        entry: dict = {"file": "", "line": None, "code": "govet", "message": line, "severity": "error"}
        # 尝试解析 file:line:col: message 格式
        parts = line.split(":")
        if len(parts) >= 2:
            entry["file"] = parts[0]
            try:
                entry["line"] = int(parts[1])
            except ValueError:
                pass
        return entry

    return _lint_line_based(
        project_path, tool="go", lang="Go", label="go vet", command="go vet ./...",
        timeout=timeout, parse_line=_parse, manifest=("go.mod",), manifest_hint="go.mod",
    )


def _lint_rust(project_path: str, rs_files: list[str], *, timeout: int = 60) -> tuple[bool, list[str], list[dict]]:
    """Rust: cargo clippy -- -D warnings（clippy 把 warning 当 error）。"""
    # D33：clippy 人类输出是多行体——"error: …" 行不带路径，定位在后续 "--> src/x.rs:5:9"
    # 行。不回填 file 则 Rust 的 lint 归属判定永远无路可依（整树 clippy 的兄弟/存量问题
    # 无法与本子任务区分）。闭包保存最近一条 issue 引用，遇 --> 行就地回填。
    _last: list[dict | None] = [None]
    _arrow_re = re.compile(r"^-+>\s*([^\s:]+):(\d+)")

    def _parse(line: str) -> dict | None:
        am = _arrow_re.match(line)
        if am:
            last = _last[0]
            if last is not None and not last.get("file"):
                last["file"] = am.group(1)
                try:
                    last["line"] = int(am.group(2))
                except ValueError:
                    pass
            return None
        # 跳过摘要行
        if line.startswith("warning: generated") or line.startswith("error: aborting"):
            return None
        if ": error[" in line or ": warning[" in line or line.startswith("error:"):
            entry: dict = {
                "file": "", "line": None, "code": "clippy", "message": line,
                "severity": "error",  # -D warnings => all warnings are errors
            }
            # 尝试解析 file:line:col 格式。Rust 输出: src/main.rs:2:5: error[E0425]: ...
            for prefix in line.split(": "):
                parts = prefix.split(":")
                if len(parts) >= 2:
                    try:
                        int(parts[1])
                        entry["file"] = parts[0]
                        entry["line"] = int(parts[1])
                        break
                    except ValueError:
                        continue
            _last[0] = entry
            return entry
        return None

    return _lint_line_based(
        project_path, tool="cargo", lang="Rust", label="cargo clippy",
        command="cargo clippy -- -D warnings", timeout=timeout, parse_line=_parse,
        manifest=("Cargo.toml",), manifest_hint="Cargo.toml", only_error_if_issues=True,
    )


def _lint_java(project_path: str, java_files: list[str], *, timeout: int = 60) -> tuple[bool, list[str], list[dict]]:
    """Java/Kotlin: checkstyle（找不到 checkstyle 就 skip，不报错）。"""
    def _parse(line: str) -> dict:
        entry: dict = {"file": "", "line": None, "code": "checkstyle", "message": line, "severity": "error"}
        # 尝试解析 [ERROR] file:line:col: message 格式
        m = re.match(r"\[(?:ERROR|WARN)\]\s+(.+?):(\d+)", line)
        if m:
            entry["file"] = m.group(1)
            entry["line"] = int(m.group(2))
        return entry

    cmd = "checkstyle " + " ".join(shlex.quote(f) for f in _cap_files(java_files, "checkstyle"))
    return _lint_line_based(
        project_path, tool="checkstyle", lang="Java", label="checkstyle", command=cmd,
        timeout=timeout, parse_line=_parse, sandbox_precheck=True,
        # P2-1：命令未带 -c 配置时 CLI 必非 0 退出——only_error_if_issues=False 会把
        # "工具自身跑不起来"当代码硬阻断（误杀，此前靠多数环境没装 checkstyle 掩盖）。
        # True=只有真解析出 issue 才算错，工具故障走不阻断路径。
        only_error_if_issues=True,
    )


def _lint_files(project_path: str, files: list[str], *, timeout: int = 60) -> tuple[bool, str, list[dict]]:
    """对修改的文件跑 lint（按语言分派矩阵），返回 (has_error, message, issues)。

    语言分派：
    - Python (.py): ruff check
    - JS/TS (.js/.jsx/.ts/.tsx): eslint（项目有配置才跑）
    - Go (.go): go vet ./...（无 go.mod 跳过）
    - Rust (.rs): cargo clippy -- -D warnings（无 Cargo.toml 跳过）
    - Java/Kotlin (.java/.kt): checkstyle（找不到工具则跳过）
    - lint 工具不可用时优雅跳过，绝不让缺工具导致崩溃或误判失败
    """
    issues: list[dict] = []
    has_error = False
    messages: list[str] = []

    # ── 按语言分组 ──
    lang_groups: dict[str, list[str]] = {
        "python": [],
        "js_ts": [],
        "go": [],
        "rust": [],
        "java": [],
    }
    for f in files:
        if f.endswith(".py"):
            lang_groups["python"].append(f)
        elif f.endswith((".ts", ".tsx", ".js", ".jsx")):
            lang_groups["js_ts"].append(f)
        elif f.endswith(".go"):
            lang_groups["go"].append(f)
        elif f.endswith(".rs"):
            lang_groups["rust"].append(f)
        elif f.endswith((".java", ".kt")):
            lang_groups["java"].append(f)

    # ── Python: ruff check ──
    py_files = lang_groups["python"]
    if py_files:
        py_err, py_msgs, py_issues = _lint_python(project_path, py_files, timeout=timeout)
        has_error = has_error or py_err
        messages.extend(py_msgs)
        issues.extend(py_issues)

    # ── JS/TS: eslint ──
    js_ts = lang_groups["js_ts"]
    if js_ts:
        js_err, js_msgs, js_issues = _lint_js_ts(project_path, js_ts, timeout=timeout)
        has_error = has_error or js_err
        messages.extend(js_msgs)
        issues.extend(js_issues)

    # ── Go: go vet ──
    go_files = lang_groups["go"]
    if go_files:
        go_err, go_msgs, go_issues = _lint_go(project_path, go_files, timeout=timeout)
        has_error = has_error or go_err
        messages.extend(go_msgs)
        issues.extend(go_issues)

    # ── Rust: cargo clippy ──
    rs_files = lang_groups["rust"]
    if rs_files:
        rs_err, rs_msgs, rs_issues = _lint_rust(project_path, rs_files, timeout=timeout)
        has_error = has_error or rs_err
        messages.extend(rs_msgs)
        issues.extend(rs_issues)

    # ── Java/Kotlin: checkstyle ──
    java_files = lang_groups["java"]
    if java_files:
        java_err, java_msgs, java_issues = _lint_java(project_path, java_files, timeout=timeout)
        has_error = has_error or java_err
        messages.extend(java_msgs)
        issues.extend(java_issues)

    summary = "; ".join(messages) if messages else "lint ok"
    return has_error, summary, issues


# ── D33 治本：lint error 归属划分（跨栈统一，不做五份复制粘贴）──
# go vet ./... / cargo clippy 是【整树】检查：沙箱树里任何兄弟子任务的坏代码、基线存量
# warning（clippy -D warnings 下几乎必有）都会让本子任务 lint 硬 FAIL → capability 误判换
# 模型 / Rust 项目所有子任务永久 lint 死锁。build 闸门早有 upstream/internal 归属阶梯，
# lint 一条没有。这里对齐：各栈 linter 产出的 issue 统一带 file 字段（ruff/eslint/checkstyle
# 按传入文件、go vet 按 file:line 前缀、clippy 按 --> 定位行回填），闸门只对【归属本子任务
# 改动文件】的 error 硬阻断；scope 外（兄弟/存量）与无法归属（配置错/输出异常）的降级为
# 告警记录——可观测、绝不静默丢，也绝不连坐。

def _normalize_lint_path(p: str, project_path: str) -> str:
    """归一 lint issue 的文件路径：去本地项目前缀 / ./ 前缀 / 反斜杠，便于跨栈比对。"""
    q = (p or "").strip().replace("\\", "/")
    if not q:
        return ""
    pp = (project_path or "").rstrip("/")
    if pp and q.startswith(pp + "/"):
        q = q[len(pp) + 1:]
    while q.startswith("./"):
        q = q[2:]
    return q


def _split_lint_errors_by_scope(
    error_issues: list[dict], modified: list[str], project_path: str
) -> tuple[list[dict], list[dict], list[dict]]:
    """把 lint error 按归属划成 (scope 内, scope 外, 无法归属)。

    匹配语义：归一后相等，或一侧是另一侧的【路径后缀】（容忍 eslint 吐绝对路径/沙箱
    远程前缀、以及子目录内跑的 linter 吐相对模块路径）。歧义偏向 scope 内（fail-closed：
    宁可阻断自己也不放走真错误）。
    """
    mods = {m for m in (_normalize_lint_path(m, project_path) for m in (modified or [])) if m}
    in_scope: list[dict] = []
    out_scope: list[dict] = []
    unattributed: list[dict] = []
    for it in error_issues:
        f = _normalize_lint_path(str(it.get("file") or ""), project_path)
        if not f:
            unattributed.append(it)
            continue
        hit = any(
            f == m or f.endswith("/" + m) or m.endswith("/" + f)
            for m in mods
        )
        (in_scope if hit else out_scope).append(it)
    return in_scope, out_scope, unattributed


# ── L1.4 LLM 自检阶段 ──

_SELF_REVIEW_PROMPT = """\
你是一位严格的代码审查员。请对以下代码变更进行自检，检查：
1. 是否完整实现了子任务目标
2. 边界情况是否处理
3. 是否违反约束（如 scope 越权、硬编码密钥等）
4. 代码风格一致性

子任务描述：
{description}

可写范围：
{writable}

变更 diff：
{diff}

请严格按照以下 JSON 格式回答（不要输出其他内容）：
{{"passed": true/false, "issues": ["问题1", "问题2"]}}
如果未发现实质性问题，passed 为 true，issues 为空列表。
"""


def _run_self_review(
    llm: BaseChatModel,
    subtask: SubTask,
    diff: str,
    *,
    timeout: int = 60,
) -> dict[str, Any]:
    """LLM 自检：调用 LLM 审查代码变更，返回 {passed, issues, raw}。"""
    prompt = _SELF_REVIEW_PROMPT.format(
        description=subtask.description,
        writable=", ".join(subtask.scope.writable or []),
        diff=diff[:4000],  # 截断避免超长
    )
    text = ""  # 预初始化避免 except 中未绑定
    try:
        from langchain_core.messages import HumanMessage
        response = llm.invoke([HumanMessage(content=prompt)])
        text = getattr(response, "content", str(response))
        # 提取 JSON（兼容 markdown 代码块包裹）
        json_str = text.strip()
        if "```" in json_str:
            # 取代码块内容
            parts = json_str.split("```")
            for p in parts:
                p = p.strip()
                if p.startswith("{"):
                    json_str = p
                    break
        # 去掉可能的语言标记
        if json_str.startswith("json"):
            json_str = json_str[4:].strip()
        result = json.loads(json_str)
        # #E：缺 passed 键 → fail-closed（与下方 JSON 解析失败分支一致）：passed=None+skipped，
        # 不默认 True 把"没给结论"当审查通过。
        if "passed" not in result:
            logger.warning("[L1.4] LLM 自检 JSON 缺 passed 字段，跳过自检（passed=None，标记 skipped，不计入 PASS）")
            return {"passed": None, "skipped": True, "skip_reason": "missing_passed_field",
                    "issues": result.get("issues", []) if isinstance(result.get("issues"), list) else [],
                    "raw": text[:500]}
        passed = bool(result.get("passed"))
        issues = result.get("issues", [])
        if not isinstance(issues, list):
            issues = [str(issues)]
        return {"passed": passed, "issues": issues, "raw": text[:500]}
    except json.JSONDecodeError:
        # fail-closed（TD2606-A2）：自检无法解析时【绝不当 passed=True】。自检本就非阻塞（仅
        # advisory），但解析失败必须 passed=None + skipped，让下游明确「未审查」而非「审查通过」，
        # 杜绝静默把「没跑成」计入 PASS 信号。
        logger.warning("[L1.4] LLM 自检输出非标准 JSON，跳过自检（passed=None，标记 skipped，不计入 PASS）")
        return {"passed": None, "skipped": True, "skip_reason": "json_parse_error", "issues": [], "raw": text[:500] or "json parse error"}
    except Exception as exc:
        logger.warning("[L1.4] LLM 自检异常，跳过自检（passed=None，标记 skipped，不计入 PASS）: %s", exc)
        return {"passed": None, "skipped": True, "skip_reason": f"exception: {exc}", "issues": [], "raw": f"self_review skipped: {exc}"}


# ── 主流水线 ──

def _guess_test_cmd(project_path: str, modified: list[str]) -> str | None:
    for fp in modified:
        base = Path(fp).stem
        if fp.endswith(".py"):
            candidates = [
                f"tests/test_{base}.py",
                f"test/test_{base}.py",
                f"test_{base}.py",
            ]
            for c in candidates:
                if os.path.isfile(os.path.join(project_path, c)):
                    return f"python -m pytest -q {c}"
    if os.path.isfile(os.path.join(project_path, "pyproject.toml")):
        return "python -m pytest -q --maxfail=1"
    return None




def _maven_modules(project_path: str) -> dict[str, str]:
    """返回 {模块相对路径: 模块相对路径} 映射，【递归】读各级 pom 的 <module>（含嵌套叶子）。

    TD2606-C6：原只读根 pom 直接子模块、键取末段名 → 嵌套工程
    （ruoyi-modules/ruoyi-system）只能匹配到聚合器 `ruoyi-modules`，而 `mvn -pl ruoyi-modules`
    要构建其全部兄弟子模块的源码（worker 只同步了改动模块）→ 反应堆失败。递归到叶子并按
    完整相对路径匹配，才能 -pl 精确限定到改动所在的叶子模块。
    """
    from pathlib import Path as _P
    import re
    root = _P(project_path)
    result: dict[str, str] = {}

    def _walk(rel: str, depth: int) -> None:
        if depth > 6:  # 防御异常深度/环
            return
        pom = (root / rel / "pom.xml") if rel else (root / "pom.xml")
        if not pom.is_file():
            return
        try:
            text = pom.read_text("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return
        for m in re.findall(r"<module>\s*([^<\s]+)\s*</module>", text):
            child = f"{rel}/{m}".strip("/").rstrip("/") if rel else m.rstrip("/")
            if child and child not in result:
                result[child] = child
                _walk(child, depth + 1)

    try:
        _walk("", 0)
    except Exception:  # noqa: BLE001
        return {}
    return result


def _reconcile_maven_module_registration(project_path: str, modified: list[str]) -> list[str]:
    """治本(round8)：确保改动涉及的【内部子模块】已注册进根 pom <modules>。

    Maven 专项入口，委托通用对账器(workspace_manifest)的 Maven 核心——所有生态(Maven/Gradle/
    Cargo/.NET/Go)的聚合清单对账收口在一处，杜绝逐生态/逐调用点各写一份漂移。返回新注册的
    模块目录名列表(扁平化，向后兼容旧签名)。详见 workspace_manifest._reconcile_maven 文档。
    """
    from pathlib import Path as _P
    from swarm.worker.workspace_manifest import _reconcile_maven
    _mods, _added = _reconcile_maven(_P(project_path), [str(m or "") for m in (modified or [])])
    out: list[str] = []
    for members in _added.values():
        out.extend(members)
    return out


def _scope_maven_command(command: str, project_path: str, modified: list[str]) -> str:
    """多模块 Maven：把整 reactor 的 mvn 命令改写成只编【改动所在模块】(-pl <mod> -am)。

    RuoYi 等多模块工程根 pom 聚合 6 个模块，整 reactor `mvn compile` 需要所有模块
    源码齐备(而 worker 只同步改动模块) → reactor 失败。正确做法是 -pl 限定改动模块、
    -am 连带构建其依赖的上游模块。已含 -pl 的命令不动；非 mvn 命令原样返回。
    """
    if "mvn" not in command or "-pl" in command:
        return command
    modules = _maven_modules(project_path)
    if not modules:
        return command
    registered = set(modules.values())
    # TD2606-C6：按【最长模块路径前缀】匹配改动文件 → 命中最深叶子模块（而非首段聚合器）。
    paths = sorted(registered, key=len, reverse=True)
    hit: list[str] = []
    for f in modified:
        fp = str(f).strip().lstrip("/")
        for mp in paths:
            if fp == mp or fp.startswith(mp + "/"):
                if mp not in hit:
                    hit.append(mp)
                break  # 命中最深模块即止（paths 已按长度降序）
    # D3 治本(Fix E)：改动落在【有自己 pom.xml 但未注册进 reactor】的孤儿模块 → 整仓 fallback
    # 会静默跳过其 .java 编译 → L1/L2 双双假 PASS。显式并进 -pl，让 mvn 报 "not found in reactor"
    # (fail-closed 暴露未注册)，而非静默放行。真·根级文件(无所属模块 pom)不受影响。
    orphans: list[str] = []
    for f in modified:
        d = _owning_module_dir(project_path, str(f))
        if d and d not in registered and d not in orphans:
            orphans.append(d)
    # R34-6 治本：D3 Fix E（孤儿强制 -pl 曝光未注册）与 round29 A(c)（注册后于脚手架）
    # 在【脚手架子任务自建新模块】窗口期结构性互斥——本子任务正创建的模块此时未注册
    # 是设计使然（registrant owner 依赖脚手架，注册在后），Fix E 却判它 reactor 必死
    # → 4 沙箱同因耗尽 escalate（round34 实证致死）。判据（确定性）：模块自己的清单
    # 在本次 modified 集里 = 本子任务就是该模块脚手架 → 用清单本地构建
    # （mvn -f <mod>/pom.xml，不进 reactor 无需注册）。修改【既有】未注册模块（清单
    # 不在 modified）的孤儿 Fix E 语义原样保留（fail-closed 曝光真漏注册）。
    # 判据边界（复核 LOW#B 澄清）："模块 pom ∈ modified" 涵盖【新建模块 pom】与
    # 【直接编辑既有孤儿 pom】两种——两者都改用 -f 直接构建该模块（前者是脚手架窗口，
    # 后者本就在改该 pom，-f 直验比 Fix E 的"曝光未注册"更贴切）；仅【pom 未被触碰的
    # 既有孤儿】仍走 -pl fail-closed。通用不变量=自建/自改模块的验证不得依赖"他人稍后
    # 才提供"的注册状态，各栈命令推导处同理。
    _modified_norm = {str(f).strip().lstrip("/") for f in modified}
    self_scaffold = [o for o in orphans if f"{o}/pom.xml" in _modified_norm]
    orphans = [o for o in orphans if o not in self_scaffold]
    targets = hit + [o for o in orphans if o not in hit]
    if self_scaffold and not targets:
        # 纯脚手架子任务：清单本地构建（多新模块极罕见取首个，其余由各自验证轮兜底）。
        # ★hunter 实证 Death B：-f 丢 -am，自建模块若依赖 sibling(com.<proj>:*)，新沙箱
        # .m2 未装这些产物 → "Could not resolve dependencies" 换个死法。脚手架的验证契约
        # 是"模块良构可注册"，validate 校验 pom 结构+parent 链解析、不解析 <dependencies>、
        # 不需 sibling 产物——正是脚手架该验的范围（模块代码真编译由注册后的内容子任务经
        # reactor -pl -am 拉齐 sibling 完成，round34 计划 acceptance 本就用 `mvn validate -f`）。
        # 故需上游产物的目标(compile/test/package/…)降级 validate；validate/clean 等原样。★
        scoped = command.replace("mvn", f"mvn -f {self_scaffold[0]}/pom.xml", 1)
        if re.search(r"\b(compile|test-compile|test|package|verify|install|deploy)\b", scoped):
            scoped = re.sub(
                r"\b(compile|test-compile|test|package|verify|install|deploy)\b",
                "validate", scoped, count=1)
        return scoped
    if self_scaffold and targets:
        # 复核 LOW#A：混合子任务（自建新模块 + 改既有注册模块）——单条 mvn 命令无法既
        # -pl reactor 又 -f 本地。保留 reactor 验注册模块，但自建模块【不静默排除】：
        # 高可见 WARNING 留痕（fail-loud，杜绝其 .java 未验证却读作 PASS）。此形态罕见
        # （脚手架子任务通常隔离，R34-6 前提），命中即提示计划拆分应把脚手架独立成子任务。
        logger.warning(
            "[L1] 混合子任务同时自建模块 %s 与改动注册模块 %s——本轮 reactor 只验后者，"
            "自建模块的独立编译未纳入本命令（建议 plan 将脚手架拆为独立子任务）",
            self_scaffold, targets)
    if not targets:
        return command  # 无模块归属(根级文件) → 整仓 fallback 正确
    pl = ",".join(targets)
    # D5(a) 治本(修 f4c1a40 引入的 drag-down)：validate 是【模块级弱校验】——只校本模块 pom 结构 +
    # parent 链，不需上游模块的编译产物。若加 -am 会连带构建上游 reactor，纯 pom 子任务就会因【无关
    # sibling 的缺陷】被判 hard-FAIL(违背 P0-B"不连坐 sibling")。故 validate（及 clean/help 等不产
    # 物、不依赖上游产物的目标）【不加 -am】；compile/test/package 等真需上游产物的目标保留 -am。
    needs_upstream = bool(
        re.search(r"\b(compile|test-compile|test|package|verify|install|deploy)\b", command)
    )
    am = " -am" if needs_upstream else ""
    # 插到 mvn 之后：mvn <args> → mvn -pl <pl> [-am] <args>
    return command.replace("mvn", f"mvn -pl {pl}{am}", 1)


def _owning_module_dir(project_path: str, rel: str) -> str:
    """改动文件 rel 的【最近所属模块目录】(含 pom.xml 的最近祖先目录，相对 project)。

    从最深父目录向上找首个含 pom.xml 的目录；无(根级文件)→返回 ""。用于 D3 判断改动是否落在
    某个模块内(据此判断该模块是否已注册进 reactor)。
    """
    from pathlib import Path as _P
    parts = str(rel).strip("/").split("/")
    for i in range(len(parts) - 1, 0, -1):
        d = "/".join(parts[:i])
        if (_P(project_path) / d / "pom.xml").is_file():
            return d
    return ""


# ── P0-B/根因#3：构建错误归属判定（文件级——本子任务改动文件 vs 别人的文件）──
# 文件级比模块级更精准：RuoYi-alarm 一个模块里几十个子任务各写不同文件，全量 mvn 编译时
# 别人的坏文件会炸本子任务的 build；按【报错文件是否在本子任务改动集】判定，把"不是我写的
# 文件的错"标 BLOCKED 交文件 owner 去修（owner 在自己的全量闸门会抓到，见根因#1），不连坐。
_POM_ERR_MODULE_RE = re.compile(r"The project [\w.\-]+:([\w.\-]+):")
_COMPILE_ERR_PATH_RE = re.compile(r"(?:^|[ /])([A-Za-z0-9_.\-]+)/src/(?:main|test)/")
_ERR_FILE_RE = re.compile(r"([A-Za-z0-9_./\-]+\.(?:java|kt|scala|go|rs|ts|tsx|js|vue|xml))")


def _pl_modules_from_cmd(build_cmd: str) -> set[str]:
    """从闸门命令里抽本子任务【自己的】模块（-pl <a,b> 的各段末路径名）。"""
    m = re.search(r"-pl\s+(\S+)", build_cmd or "")
    if not m:
        return set()
    out: set[str] = set()
    for seg in m.group(1).split(","):
        seg = seg.strip().lstrip("!").strip("/")
        if seg:
            out.add(seg.split("/")[-1])
    return out


def _build_error_modules(build_output: str) -> set[str]:
    """从构建输出抽【报错所在模块】：pom 解析错的 `The project G:art` + 编译错的文件路径段。"""
    mods = {m.group(1) for m in _POM_ERR_MODULE_RE.finditer(build_output or "")}
    mods |= {m.group(1) for m in _COMPILE_ERR_PATH_RE.finditer(build_output or "")}
    return {x for x in mods if x}


def _norm_src_path(p: str) -> str:
    """归一化源路径为模块相对（去 /workspace/ 前缀与 ./）：/workspace/ruoyi-alarm/src/.../X.java
    → ruoyi-alarm/src/.../X.java，便于与子任务 modified 相对路径比对。"""
    p = str(p).strip().replace("\\", "/")
    p = re.sub(r"^.*?/workspace/", "", p)
    return p.lstrip("./").lstrip("/")


def _build_error_files(build_output: str) -> set[str]:
    """从构建输出抽【报错的源文件】(归一化模块相对路径)。"""
    out: set[str] = set()
    for m in _ERR_FILE_RE.finditer(build_output or ""):
        f = _norm_src_path(m.group(1))
        if "/" in f or f.endswith("pom.xml"):  # 过滤裸文件名噪声，保留真实路径
            out.add(f)
    return out


_RX_MAVEN_CHILD_MODULE = re.compile(
    r"Child module\s+(\S+?)(?:/pom\.xml)?\s+of\s+\S+\s+does not exist", re.I)
# 捕获整行余部（可含逗号分隔的多模块列表——`-pl dirA,dirB` 双缺时 Maven 一行列全；
# 猎人#3：窄字符类只抓首个会静默丢其余模块）。`@` 是 Maven 错误行尾锚，先截掉。
_RX_MAVEN_REACTOR_NOT_FOUND = re.compile(
    r"Could not find the selected project in the reactor:?\s*([^\n@]+)", re.I)
_RX_GRADLE_PROJECT_DIR = re.compile(r"Project directory\s+'([^']+)'\s+does not exist", re.I)
_RX_CARGO_WS_MEMBER = re.compile(
    r"failed to load manifest for workspace member\s+`([^`]+?)`", re.I)
# 宽松取 directory 后首 token：go 措辞既有 "directory X does not exist" 也有
# "directory X listed in go.work does not exist"（token 与 does-not-exist 不相邻）。
_RX_GO_DIR_MISSING = re.compile(r"directory\s+(\S+)", re.I)
_RX_MODULE_TOKEN = re.compile(r"[\w./:\\-]+")


def _build_error_is_reactor_missing_module(build_output: str | None) -> set[str]:
    """构建错是否【工作区清单注册了不存在的模块】（注册先于脚手架落地，round29 A 症状类）。

    返回缺失模块的【项目相对目录】集合（空集=非此症状）。这是结构性依赖序问题（plan 期
    registrant/scaffold 边向），非本子任务能力问题——调用方标 BLOCKED + 结构化
    blocked_on_modules，交 brain 定点重排（failure.py 序修复阶梯）。跨栈通用、非项目写死。
    """
    if not build_output:
        return set()
    out: set[str] = set()

    def _norm_add(raw: str) -> None:
        raw = (raw or "").strip().strip(",;").rstrip("/")
        if not raw or not _RX_MODULE_TOKEN.fullmatch(raw):
            return
        if ":" in raw and "/" not in raw:
            raw = raw.split(":")[-1]          # maven 坐标 groupId:artifactId → artifactId
        for prefix in ("/workspace/", "/repo/", "./"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
        raw = raw.replace("\\", "/").lstrip("/")
        for mf in ("/pom.xml", "/Cargo.toml", "/build.gradle", "/go.mod"):
            if raw.endswith(mf):
                raw = raw[: -len(mf)]
        if raw and raw not in (".", ".."):
            out.add(raw)

    for m in _RX_MAVEN_CHILD_MODULE.finditer(build_output):
        _norm_add(m.group(1))
    for m in _RX_MAVEN_REACTOR_NOT_FOUND.finditer(build_output):
        for item in re.split(r"[,\s]+", m.group(1)):   # 多模块列表逐个收，不丢第二个及以后
            _norm_add(item)
    for m in _RX_GRADLE_PROJECT_DIR.finditer(build_output):
        _norm_add(m.group(1))
    for m in _RX_CARGO_WS_MEMBER.finditer(build_output):
        _norm_add(m.group(1))
    # Go workspace：行内同现 go.work 与 does not exist 即判（顺序无关——复核#8：措辞里
    # go.work 常在 does not exist 之前，单向 .* 正则会静默永不匹配）。
    for line in build_output.splitlines():
        low = line.lower()
        if "go.work" in low and "does not exist" in low:
            m = _RX_GO_DIR_MISSING.search(line)
            if m:
                _norm_add(m.group(1))
    return out


def _build_error_is_upstream(build_output: str, build_cmd: str,
                             modified: list[str] | None = None) -> bool:
    """构建错是否【非本子任务写的代码造成】（→ 标 BLOCKED 交 owner 修，不连坐本子任务）。

    优先【文件级】（根因#3）：报错文件全部不在本子任务 modified 改动集 → True；只要有一个报错
    文件是本子任务改的 → False（自己有错，不放过，由根因#1 的全量闸门在源头当场修）。
    无 modified 信息时回退【模块级】（-pl 模块 vs 报错模块），向后兼容。
    """
    errs_files = _build_error_files(build_output)
    mods = {_norm_src_path(f) for f in (modified or []) if str(f).strip()}
    if errs_files and mods:
        return errs_files.isdisjoint(mods)
    # 回退：模块级
    own = _pl_modules_from_cmd(build_cmd)
    errs = _build_error_modules(build_output)
    if not own or not errs:
        return False
    return own.isdisjoint(errs)


def run_l1_pipeline(
    project_path: str,
    subtask: SubTask,
    diff: str,
    *,
    timeout: int = 120,
    llm: BaseChatModel | None = None,
    project_stack: dict | None = None,
    extra_writable_paths: set[str] | None = None,
    deadline: float | None = None,
) -> tuple[bool, dict[str, Any]]:
    """L1.1 scope → L1.2 compile → L1.2.5 lint → L1.3 scoped test → L1.4 LLM 自检。

    ★契约——勿裸用 bool 返回值（CODEWALK 根因B）：所有 BLOCKED 路径（malformed_diff_zero_files /
    build_infra_failure / upstream_module_broken / internal_pkg_not_built / build_manifest_missing /
    test_infra_failure / verify_infra_failure）都返回 ok=True 且 details["pipeline_blocked"] 置位——
    语义是"跑通了能跑的、但该验证的环节被阻塞"，不是 PASS。调用方【必须】复核
    details.get("pipeline_blocked")（executor 侧 _deterministic_l1_gate 把 ok∧blocked 降级为
    None/BLOCKED 走 transient 重试）。新调用方裸用返回值即假绿。契约由
    test_l1_pipeline_blocked_contract.py 锁定。

    Args:
        project_path: 项目根目录
        subtask: 子任务定义
        diff: 变更 diff
        timeout: 各阶段超时秒数
        llm: 可选 LLM 句柄，用于 L1.4 自检阶段；不传则自检跳过
        project_stack: 权威栈画像（detect_stack 产）；驱动构建失败时的跨生态 repair adapter 选择
        extra_writable_paths: round18 P0-B——确定性修复机制合法触达的 scope 外文件
            (executor._repaired_extra_paths，如 module-registration 自愈改的父 pom)，
            scope 复核时视为允许，避免把非 worker 越权写的修复文件误判越权整份判死。
    """
    details: dict[str, Any] = {"pipeline": "L1.1-L1.4"}

    # C1（阶段4）：worker 总预算 deadline（monotonic 绝对时刻）——每个昂贵阶段前查剩余，
    # 耗尽即走既有 BLOCKED 契约（ok=True + pipeline_blocked，executor 侧降 None/BLOCKED
    # 走重试），绝不白跑 35min 也绝不假 PASS。deadline=None=legacy 调用方零回归。
    def _deadline_blocked(stage: str) -> bool:
        if deadline is not None and _time.monotonic() >= deadline:
            details["pipeline_blocked"] = "worker_deadline_exhausted"
            details["not_run_kind"] = NotRunKind.BLOCKED.value
            details["deadline_stage"] = stage
            logger.warning("[L1] worker 预算耗尽（阶段=%s）→ BLOCKED，不再白跑", stage)
            return True
        return False

    if _deadline_blocked("entry"):
        return True, details

    # D57：新一次 L1 run = 新一代 manifest 在场性缓存（run 内 5-8 趟沙箱 find 收敛为
    # 每组 manifests 至多一趟；跨 run 不留 stale）。
    _invalidate_manifest_cache()

    # ── L1.1 scope 检查 ──
    violations = _scope_violations(diff, subtask.scope, extra_allowed=extra_writable_paths)
    details["l1_1_scope_ok"] = not violations
    details["scope_violations"] = violations
    if violations:
        return False, details

    modified = files_from_unified_diff(diff)
    details["modified_files"] = modified

    harness = getattr(subtask, "harness", None)
    # N-19：空 diff 短路只有在【没有任何确定性验收命令】时才成立。原代码只看 verify_commands，
    # 忽略 build_command/test_command → "无 diff 但 acceptance=跑测试" 的任务会不跑测试直接 PASS。
    _has_verify = bool(getattr(harness, "verify_commands", None)) if harness else False
    _has_build = bool(getattr(harness, "build_command", "")) if harness else False
    _has_test = bool(getattr(harness, "test_command", "")) if harness else False
    if not modified and not (_has_verify or _has_build or _has_test):
        details["l1_2_compile_ok"] = True
        details["lint"] = {"status": "skipped", "reason": "no files"}
        details["l1_3_test_ok"] = True
        # fail-closed：区分「真空 diff」(BENIGN no-op) 与「非空 diff 却解析到 0 文件」
        # （malformed diff，TD2606-C8/H4：垃圾输出 / 无 +++ b/ 头 → 看似有产出实则无法验证）。
        if (diff or "").strip():
            details["note"] = "diff 非空但解析到 0 个文件（疑似 malformed diff），无法验证"
            details["pipeline_blocked"] = "malformed_diff_zero_files"
            details["not_run_kind"] = NotRunKind.BLOCKED.value
        else:
            details["note"] = "no diff changes"
            details["not_run_kind"] = NotRunKind.BENIGN.value
        return True, details

    # ── L1.2 编译(语法) ──
    compile_ok, compile_msg = _compile_files(project_path, modified, timeout=timeout)
    details["l1_2_compile_ok"] = compile_ok
    details["compile_message"] = compile_msg
    if not compile_ok:
        return False, details

    # ── L1.2.1 harness.build_command 编译闸门（Java/Go/Rust 等需工具链语言）──
    # _compile_files 仅覆盖 py/js 语法检查；Java(mvn)/Go(go build)/Rust(cargo)
    # 的真实编译靠 Brain 编写的 harness.build_command，在沙箱里跑(那里有工具链)。
    # 这是补齐 5 语言生产级编译验证的关键——杜绝"Java 改坏了但确定性层不知道"。
    build_cmd = getattr(harness, "build_command", "") if harness else ""
    # 根因#1（producer-gate 不对称，996db614 实测 7h replan 雪崩的头号真因）：
    # _compile_files 的【单文件 javac】抓不到需全类路径才暴露的类型/跨文件错（如
    # `String[] cannot be converted to Long[]`、臆造方法签名）。这类错会从【能改它的生产者
    # 子任务】（其 L1 仅跑了弱 javac）漏过，到【无权修复它的下游子任务】跑全量 `mvn -am` 时才
    # 炸——下游修不动别人的文件 → 无限 replan → escalate → FAILED。
    # 治本：子任务改了 .java 但 brain 没下发 build_command 时，确定性派生【全量 mvn 编译】，
    # 让生产者闸门与下游一样强，把错堵在源头当场修。
    if not build_cmd:
        build_cmd = _derive_full_build_command(project_path, modified, project_stack)
        if build_cmd:
            details["build_command_derived"] = build_cmd
    if build_cmd:
        # 治本(round8)：先把改动涉及的内部子模块补注册进根 pom <modules>(对账被跨子任务冲掉的
        # 注册)，再 scope——否则 _maven_modules 扫不到该模块、无法 -pl 收窄，退回全 reactor 必死。
        try:
            from swarm.worker.workspace_manifest import reconcile_workspace_manifests
            _wm = reconcile_workspace_manifests(project_path, modified)
            _manifests = _wm.get("modified_manifests") or []
            if _manifests:
                logger.info(
                    "[L1.2.1·module-reg] 补注册聚合清单成员(Maven/Gradle/Cargo/.NET/Go): %s"
                    "（修复缺模块/缓存负解析致的确定性 FAIL）", _wm.get("added"),
                )
                details["module_registration_added"] = _wm.get("added")
                # 治本关键(round8 自审补漏)：补注册改的是【聚合清单】(根 pom / settings.gradle /
                # Cargo.toml / .sln / go.work)，它们【不在本子任务写权 scope 内】。必须登记进
                # repaired_file_paths，否则 executor 的 pull-back 只回传 scope 内文件 → 注册只活在
                # 【本沙箱】→ 下游子任务在干净沙箱基于 HEAD(仍缺注册)重建 → 毒复发、治本不级联。
                # 挂到 repaired_file_paths 使其回传本地 + 计入 diff，持久化到权威库。
                _rfp = details.setdefault("repaired_file_paths", [])
                for _mf in _manifests:
                    if _mf not in _rfp:
                        _rfp.append(_mf)
                # 治本 #11(b)：reconcile 改的是【本地】清单，但 build gate 在【远端沙箱】读
                # bootstrap 上传的旧副本 → 注册对构建不可见（reactor not-found）。必须把改过的
                # 清单推进沙箱（与 import/version repair 沙箱优先对齐），否则本地注册白改。
                _pushed = _push_manifests_to_sandbox(project_path, _manifests)
                if _pushed:
                    details["module_registration_pushed"] = _pushed
        except Exception as _exc:  # noqa: BLE001
            logger.debug("[L1.2.1·module-reg] 对账异常(跳过): %s", _exc)
        build_cmd = _scope_maven_command(build_cmd, project_path, modified)
    if build_cmd and _build_cmd_applicable(build_cmd, project_path):
        if _deadline_blocked("build"):
            return True, details
        logger.info("[L1.2.1] 执行构建闸门: %s", build_cmd)
        b_ec, b_out = _run_l1_command(
            build_cmd, project_path, timeout=_stage_timeout(max(timeout, 300), deadline))
        build_ok = b_ec == 0
        details["l1_2_1_build_ok"] = build_ok
        details["build_command"] = build_cmd
        details["build_output"] = compress_tool_output(b_out, max_chars=1500)
        logger.info("[L1.2.1] 构建闸门结果: exit=%s ok=%s", b_ec, build_ok)
        if not build_ok:
            # 治本·通用：据项目自身惯例确定性修正写错的包名前缀/拼错符号后【重跑】构建确认。
            # 安全性自证——只在构建已失败时触发，且必须重跑通过才算修好，修错了重跑仍失败=
            # 不会制造假通过。SWARM_WORKER_IMPORT_REPAIR=false 可关。
            #
            # 根因#③（996db614 实测：531 cannot find symbol 仅确定性纠掉 17）：编译器错误
            # 掩蔽是【级联】的——一遍 repair 修掉可见的 typo/缺 import 后，rerun 才会暴露原先
            # 被上游错误掩蔽的下一批 cannot-find-symbol（实证：一个子任务的 isEmtpy 散落 6 文件，
            # 单发只纠到 1 个，残余漏到慢 LLM 修复循环 → 模型反复写回同一 typo → 撞 900s 预算
            # 超时 → FAILED）。治本：把确定性 repair 跑成【幂等收敛循环】——修→重跑→再修，直到
            # 构建通过或某轮【零新增修复】（卡死，交后续 infra/upstream/fail 处理）。纯确定性、
            # 单调收敛（perl 改了不会被自己改回，故不会震荡）、有界（默认 4 轮），全程无 LLM 介入，
            # 把整条 typo 级联在【能改它的生产者】当场吃完，不漏到无权修的下游/慢循环。
            repair_on = os.environ.get(
                "SWARM_WORKER_IMPORT_REPAIR", "true"
            ).lower() not in ("false", "0", "no")
            repaired_paths: list[str] = []
            if repair_on:
                _loop_t0 = _time.monotonic()
                # C1：repair 墙钟钳到 worker 剩余预算（独立 900s 是 35min runaway 主推手）
                _loop_budget = _repair_loop_budget(deadline)
                for _rr in range(_max_build_repair_rounds()):
                    if _time.monotonic() - _loop_t0 >= _loop_budget:
                        logger.warning(
                            "[L1.2.1] 确定性收敛循环达墙钟上界 %.0fs（已修 %d 文件），停止，交后续分类",
                            _loop_budget, len(repaired_paths),
                        )
                        break
                    try:
                        n_round, paths_round = _attempt_build_repair(
                            project_path, b_out, modified, timeout, project_stack
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("[L1.2.1] build-repair 跳过(异常,不致命): %s", exc)
                        break
                    if not n_round:
                        break  # 本轮零新增修复 → 已收敛或卡死，停止空转重跑
                    for p in paths_round:
                        if p and p not in repaired_paths:
                            repaired_paths.append(p)
                    logger.info(
                        "[L1.2.1] 确定性修复第 %d 轮触达 %d 文件，重跑构建闸门", _rr + 1, n_round
                    )
                    b_ec, b_out = _run_l1_command(
                        build_cmd, project_path,
                        timeout=_stage_timeout(max(timeout, 300), deadline)
                    )
                    build_ok = b_ec == 0
                    if build_ok:
                        break
            if repaired_paths:
                details["l1_2_1_build_ok"] = build_ok
                details["build_output"] = compress_tool_output(b_out, max_chars=1500)
                details["import_repaired_files"] = len(repaired_paths)
                # TD2606-C9：把【沙箱里】被修复的文件相对路径透传给 executor，使其无论文件
                # 是否在子任务写权 scope 内都回传本地 + 计入 diff，杜绝两棵真值树静默分叉。
                # 治本(自审补漏)：必须【并集合并】而非覆盖——构建前的聚合清单对账已把 pom.xml 等
                # 清单写进 repaired_file_paths；若此处直接赋值会把它们【冲掉】→ 清单不回传 → 治本
                # 在"既补注册又触发修复"的常见失败路径上被悄悄废掉。
                _existing_rfp = details.get("repaired_file_paths") or []
                details["repaired_file_paths"] = list(
                    dict.fromkeys([*_existing_rfp, *repaired_paths])
                )
                logger.info(
                    "[L1.2.1] 确定性收敛修复累计 %d 文件后构建: ok=%s",
                    len(repaired_paths), build_ok,
                )
            if not build_ok:
                # fail-closed 但不误判 capability：构建非零退出若命中网络/工具/资源 infra 瞬时故障，
                # 不是代码能力失败 → 标 BLOCKED 走 transient 退避重试（耗尽才硬 FAIL），不错换模型。
                if _is_infra_failure(b_out):
                    details["l1_2_1_build_ok"] = None
                    details["build_blocked"] = build_cmd
                    details["pipeline_blocked"] = "build_infra_failure"
                    details["not_run_kind"] = NotRunKind.BLOCKED.value
                    logger.warning(
                        "[L1.2.1] 构建命中 infra 瞬时故障，标 BLOCKED 转 transient 重试: %s",
                        (b_out or "")[:200],
                    )
                    return True, details
                # round29 A：工作区清单注册了【树里不存在的模块】（Child module … does not exist /
                # reactor not found）= plan 期「注册先于脚手架」依赖序连反的确定性症状，非本子任务
                # 能力问题（重试/换模型都治不了）。标 BLOCKED + 结构化 blocked_on_modules，交 brain
                # 定点重排（failure.py 序修复阶梯插 registrant-after-scaffold 规范边后重派）。
                # 置于 upstream 归属判定之前：本症状特征串无歧义，且 upstream 判定可能因报错文件
                # 全在别处而抢先吞掉它、丢失结构化模块信息。
                _missing_mods = _build_error_is_reactor_missing_module(b_out)
                if _missing_mods:
                    details["l1_2_1_build_ok"] = None
                    details["build_blocked"] = build_cmd
                    details["pipeline_blocked"] = "module_registered_before_scaffold"
                    details["not_run_kind"] = NotRunKind.BLOCKED.value
                    details["blocked_on_modules"] = sorted(_missing_mods)
                    logger.warning(
                        "[L1.2.1] 清单注册的模块在树里不存在（注册先于脚手架，依赖序问题）→ 标 "
                        "BLOCKED 交 brain 定点重排: %s | %s",
                        sorted(_missing_mods), (b_out or "")[:200],
                    )
                    return True, details
                # P0-B：错误归属——构建错若【全在本子任务模块之外的上游模块】（如 -pl ruoyi-alarm
                # 但报错在 ruoyi-generator 的坏 pom），是上游子任务没收尾，非本子任务能力问题。
                # 标 BLOCKED 交退避重试（待上游 pom 被其 owner/对账修好），不烧本子任务修复轮——
                # 杜绝一个坏 pom 经 -am reactor 连坐拖死十几个无辜子任务（996db614 实测）。
                if _build_error_is_upstream(b_out, build_cmd, modified):
                    details["l1_2_1_build_ok"] = None
                    details["build_blocked"] = build_cmd
                    details["pipeline_blocked"] = "upstream_module_broken"
                    details["not_run_kind"] = NotRunKind.BLOCKED.value
                    # 结构化吐【阻断在哪些上游模块/文件】，供 brain 反查生产者子任务：若生产者已被
                    # 永久放弃(阶梯三打桩/revert)，则本下游不可恢复，应连坐放弃而非无限 replan。
                    details["blocked_on_modules"] = sorted(_build_error_modules(b_out))
                    details["blocked_on_files"] = sorted(_build_error_files(b_out))
                    logger.warning(
                        "[L1.2.1] 构建错全在上游模块(非本子任务 -pl 模块) → 标 BLOCKED 退避，"
                        "待上游修好再编，不连坐本子任务: %s", (b_out or "")[:200],
                    )
                    return True, details
                # 根因#②（996db614 实测 ~70/213）：构建缺【尚未建出的项目内部包】（别的子任务
                # 还没产出 com.ruoyi.alarm.sender.dto 等）→ 非本子任务能力问题、本子任务也无权建
                # 那些包。标 BLOCKED 退避，待生产者子任务落地（merge 进树）后由 transient 重试自然
                # 消解，不烧本子任务修复轮 / 不误判 capability 换模型 / 不 escalate 清空已成功成果。
                # 保守判据见 _build_blocked_on_unbuilt_internal（有第三方缺包/包已在树里→照常 FAIL）。
                _blocked_pkgs = _build_blocked_on_unbuilt_internal(project_path, b_out, timeout)
                if _blocked_pkgs:
                    details["l1_2_1_build_ok"] = None
                    details["build_blocked"] = build_cmd
                    details["pipeline_blocked"] = "internal_pkg_not_built"
                    details["not_run_kind"] = NotRunKind.BLOCKED.value
                    # 结构化吐【缺哪些项目内部包】，供 brain 反查生产者子任务（按 scope/目标包归属）：
                    # 生产者已被永久放弃 → 本下游不可恢复，连坐放弃而非无限 BLOCKED→replan。
                    details["blocked_on_packages"] = sorted(_blocked_pkgs)
                    logger.warning(
                        "[L1.2.1] 构建缺【尚未建出的项目内部包】(②跨模块/跨子任务未就绪) → 标 "
                        "BLOCKED 退避待生产者落地，不连坐本子任务: %s", (b_out or "")[:200],
                    )
                    return True, details
                details["build_failed"] = build_cmd
                return False, details
    elif build_cmd:
        # 治本(st-10 npm 误判空转，996db614 实测)：Brain 给【纯静态资源子任务】(只改 .html/.js/.css/
        # .vm 等服务端资源、无可编译源)误派了 node 构建(npm/yarn/pnpm/npx)，但项目是 Maven 单体
        # (有 pom、无 package.json)——这些是 Thymeleaf/admin 静态资源，根本无 npm 工程、也【不会有
        # upstream 建出 package.json】。旧逻辑标 BLOCKED → 每轮重试再撞同一探测、永远空转(代码其实
        # 没问题)，还每轮白烧一次 HANDLE_FAILURE 的云模型调用。治本：仅当【node 构建工具 + 无可编译源
        # + 项目无 package.json + 是 Maven 项目(有 pom)】这一【根本不匹配】组合时，判【无需构建】放行
        # (走 scope+lint 即过)，绝不碰 ② 的合法 BLOCKED(.java 等可编译源缺 pom，pom 可由 upstream 建出)。
        _node_tools = {"npm", "yarn", "pnpm", "npx"}
        _tool = build_cmd.strip().split()[0] if build_cmd.strip() else ""
        _has_compilable = any(
            str(f).endswith((".java", ".kt", ".scala", ".go", ".rs", ".ts", ".tsx", ".vue"))
            for f in (modified or [])
        )
        if (
            _tool in _node_tools
            and not _has_compilable
            and not _manifest_present(("package.json",), project_path)
            and _manifest_present(("pom.xml",), project_path)
        ):
            details["l1_2_1_build_ok"] = True
            details["build_skipped"] = (
                f"纯静态资源子任务(无可编译源)，Maven 项目无 npm 工程 → 跳过误派的 node 构建: {build_cmd}"
            )
            details["build_command_skipped_reason"] = "node_build_on_maven_static_resource"
            logger.info(
                "[L1.2.1] 纯静态资源(无可编译源)+Maven 项目无 package.json → 跳过误派的 node 构建"
                "(放行非 BLOCKED，杜绝 st-10 式空转): %s", build_cmd,
            )
            # 不 return：继续走 format/lint 闸门，由 scope+lint 把关
        else:
            # Brain 指定了 build_command（即【期望】这是可构建项目），但工程清单(pom/go.mod/...)在同步后
            # 的树里定位不到 → 本应构建却跑不起来。fail-closed：标 BLOCKED（TD2606-B7），不再静默当
            # 「跳过=通过」。多因模块源同步不全/清单未上传 → 交裁决器走 transient 重试。
            details["l1_2_1_build_ok"] = None
            details["build_skipped"] = f"期望构建但无法定位工程清单: {build_cmd}"
            details["pipeline_blocked"] = "build_manifest_missing"
            details["not_run_kind"] = NotRunKind.BLOCKED.value
            logger.warning("[L1.2.1] 期望构建但无对应工程文件，标 BLOCKED 转 transient 重试: %s", build_cmd)
            return True, details

    # ── L1.2.0 自动格式化（L0 闸门）──
    # 在 lint 之前先确定性格式化改动文件：把"风格"从模型负担降级为系统自动行为。
    # SWARM_WORKER_L1_FORMAT=false 可关闭。工具缺失优雅 skip，绝不阻断。
    format_enabled = os.environ.get("SWARM_WORKER_L1_FORMAT", "true").lower() not in ("false", "0", "no")
    if format_enabled and modified:
        try:
            from swarm.worker.format_gate import format_files

            fmt_result = format_files(project_path, modified, timeout=timeout)
            details["format"] = fmt_result
        except Exception as exc:  # noqa: BLE001
            # 格式化失败绝不阻断主流程（纯锦上添花层）
            logger.debug("L0 format 跳过(异常): %s", exc)
            details["format"] = {"status": "skipped", "error": str(exc)}

    # ── L1.2.5 lint ──
    lint_enabled = os.environ.get("SWARM_WORKER_L1_LINT", "true").lower() not in ("false", "0", "no")
    if lint_enabled:
        lint_has_error, lint_msg, lint_issues = _lint_files(project_path, modified, timeout=timeout)
        details["lint"] = {
            "status": "error" if lint_has_error else "ok",
            "message": lint_msg,
            "issues": lint_issues,
            "has_error": lint_has_error,
        }
        if lint_has_error:
            # 语法级 lint error（ruff E9xx/F4xx、eslint error）是确定性真错误，
            # 默认硬阻断流水线（确定性断言优于事后告警）。
            # SWARM_WORKER_L1_LINT_GATE=false 可回退到旧的"仅警告不阻断"行为。
            gate_enabled = os.environ.get(
                "SWARM_WORKER_L1_LINT_GATE", "true"
            ).lower() not in ("false", "0", "no")
            error_issues = [i for i in lint_issues if i.get("severity") == "error"]
            details["lint"]["error_issues"] = error_issues
            if gate_enabled:
                # D33 治本：整树 lint(go vet/clippy)会把兄弟子任务/基线存量问题算到本子任务
                # 头上。按归属划分：只有 error 落在本子任务改动文件上才硬阻断；scope 外与
                # 无法归属的（配置错/工具输出异常）降级为告警——可观测，绝不静默丢、绝不连坐。
                in_scope, out_scope, unattributed = _split_lint_errors_by_scope(
                    error_issues, modified, project_path)
                details["lint"]["error_issues_in_scope"] = in_scope
                details["lint"]["error_issues_out_of_scope"] = out_scope
                details["lint"]["error_issues_unattributed"] = unattributed
                if in_scope:
                    details["lint"]["note"] = "lint 语法级 error(归属本子任务改动文件)硬阻断流水线"
                    details["lint"]["gated"] = True
                    if out_scope or unattributed:
                        logger.warning(
                            "[L1.2.5] lint 阻断之外另有 scope 外 error %d 条 / 无法归属 %d 条"
                            "（兄弟/存量/配置问题，不计入本子任务）",
                            len(out_scope), len(unattributed),
                        )
                    return False, details
                details["lint"]["gated"] = False
                details["lint"]["note"] = (
                    "lint error 均不归属本子任务改动文件(兄弟/存量/无法归属) → 降级告警不阻断(D33)"
                )
                logger.warning(
                    "[L1.2.5] lint error %d 条均在本子任务改动文件之外"
                    "(scope 外=%d, 无法归属=%d) → 不连坐阻断，降级告警。样例: %s",
                    len(error_issues), len(out_scope), len(unattributed),
                    [
                        f"{i.get('file') or '?'}: {str(i.get('message') or '')[:80]}"
                        for i in (out_scope + unattributed)[:3]
                    ],
                )
            else:
                details["lint"]["note"] = "lint error 仅作警告（SWARM_WORKER_L1_LINT_GATE=false）"
                details["lint"]["gated"] = False
                # audit #27：lint gate 被显式关闭时本应阻断的 error 被放行，属安全护栏降级，
                # 必须在日志可见（否则误配置导致 lint 静默失效无人察觉）。
                if error_issues:
                    logger.warning(
                        "[L1.2.5] lint gate 已关闭(SWARM_WORKER_L1_LINT_GATE=false)，"
                        "%d 个语法级 lint error 未阻断流水线", len(error_issues),
                    )
    else:
        details["lint"] = {"status": "disabled", "reason": "SWARM_WORKER_L1_LINT=false"}
        # audit #27：lint 整体禁用是确定性护栏降级，日志留痕。
        logger.warning("[L1.2.5] L1 lint 已禁用(SWARM_WORKER_L1_LINT=false) — 确定性 lint 校验不生效")

    # ── L1.3 scoped test ──
    if _deadline_blocked("test"):
        return True, details
    # 优先用 Brain 编排的 harness.test_command（精心编写、确定性）；
    # 没有 harness 时才回退到启发式 _guess_test_cmd。（harness 已在上方取得）
    harness_test = getattr(harness, "test_command", "") if harness else ""
    test_cmd = harness_test or _guess_test_cmd(project_path, modified)
    if test_cmd:
        test_cmd = _scope_maven_command(test_cmd, project_path, modified)
    details["test_cmd"] = test_cmd
    details["test_cmd_source"] = "harness" if harness_test else "heuristic"
    if not test_cmd:
        details["l1_3_test_ok"] = True
        details["test_skipped"] = True
    elif not _build_cmd_applicable(test_cmd, project_path):
        # 测试工具的工程文件缺失(npm test 无 package.json 等)→ 跳过，不误判失败
        details["l1_3_test_ok"] = True
        details["test_skipped"] = f"工程文件缺失，跳过测试: {test_cmd}"
        logger.info("[L1.3] 跳过测试(无对应工程文件): %s", test_cmd)
    else:
        t_ec, t_out = _run_l1_command(test_cmd, project_path, timeout=timeout)
        test_ok = t_ec == 0
        details["l1_3_test_ok"] = test_ok
        # 智能压缩：提取关键失败信号行（FAILED/Error/Traceback/assert），
        # 替代盲目硬截断 —— 避免丢失位于输出末尾的 pytest 失败摘要。
        details["test_output"] = compress_tool_output(t_out, max_chars=1500)
        if t_ec == 124:
            details["test_output"] = "test timeout"
        if not test_ok:
            # TD2606：测试命中 infra 瞬时故障(网络/工具/资源) → BLOCKED 转 transient 重试，不误判
            # capability(错换模型)。与 L1.2.1 build gate 对称。timeout(124)按真失败处理(不放过)。
            if t_ec != 124 and _is_infra_failure(t_out):
                details["l1_3_test_ok"] = None
                details["test_blocked"] = test_cmd
                details["pipeline_blocked"] = "test_infra_failure"
                details["not_run_kind"] = NotRunKind.BLOCKED.value
                return True, details
            return False, details

    # ── L1.3.5 harness 验收命令（verify_commands）——
    # Brain 为每条验收标准编写的烟雾测试/断言，硬阻断。这是"产出是否合格"的
    # 确定性证据，杜绝 LLM 口头自报合格。
    verify_cmds = list(getattr(harness, "verify_commands", []) or []) if harness else []
    if verify_cmds:
        if _deadline_blocked("verify_commands"):
            return True, details
        verify_results = []
        for vc in verify_cmds:
            v_ec, v_out = _run_l1_command(
                vc, project_path, timeout=_stage_timeout(timeout, deadline))
            ok = v_ec == 0
            verify_results.append({
                "cmd": vc, "ok": ok,
                "output": compress_tool_output(v_out, max_chars=500),
            })
            if not ok:
                details["verify_commands"] = verify_results
                # TD2606：验收命令命中 infra 瞬时故障 → BLOCKED 转 transient 重试（与 build/test 对称）。
                if v_ec != 124 and _is_infra_failure(v_out):
                    details["pipeline_blocked"] = "verify_infra_failure"
                    details["not_run_kind"] = NotRunKind.BLOCKED.value
                    return True, details
                details["verify_failed"] = vc
                return False, details
        details["verify_commands"] = verify_results

    # ── L1.4 LLM 自检（可选，不硬阻断） ──
    self_review_enabled = os.environ.get("SWARM_WORKER_L1_SELF_REVIEW", "true").lower() not in ("false", "0", "no")
    # C1：自检是 advisory——预算耗尽只跳过自检（不 BLOCKED 整个已通过的确定性结论）
    if deadline is not None and _time.monotonic() >= deadline:
        self_review_enabled = False
        details["self_review"] = {"skipped": True, "reason": "worker_deadline_exhausted"}
    if self_review_enabled and llm is not None:
        review_result = _run_self_review(llm, subtask, diff, timeout=timeout)
        details["self_review"] = review_result
        if review_result.get("skipped"):
            # 自检未能执行（解析失败/异常）——非阻塞，但明确标注「未审查」，不计入 PASS 信号。
            details["self_review"]["note"] = "LLM 自检未能执行（skipped），不计入 PASS 信号"
        elif review_result.get("passed") is False:
            # 自检发现问题，仅作为警告，不硬阻断
            details["self_review"]["note"] = "LLM 自检发现潜在问题，作为警告（不阻断）"
    elif not self_review_enabled:
        details["self_review"] = {"status": "disabled", "reason": "SWARM_WORKER_L1_SELF_REVIEW=false"}
    else:
        details["self_review"] = {"status": "skipped", "reason": "llm not provided"}

    return True, details
