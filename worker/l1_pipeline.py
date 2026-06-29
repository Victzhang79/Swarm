"""Worker L1 四级验证 — 确定性 scope / compile / lint / scoped test / LLM 自检。"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swarm.project.diff_apply import files_from_unified_diff
from swarm.types import FileScope, NotRunKind, SubTask
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
            scmd = (
                f"sed -i.bak 's#{first}\\.{suf_re}#{canonical}.{suffix}#g' '{f}' "
                f"&& rm -f '{f}.bak'"
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
        cmd = f"curl -s -m 15 '{url}' 2>/dev/null || wget -qO- -T 15 '{url}' 2>/dev/null"
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
        # 定位声明该 artifact 的 pom + 持有该版本号的属性 pom（可能是父 pom）
        gcmd = (
            f"grep -rl '<artifactId>{re.escape(artifact)}</artifactId>' --include=pom.xml . 2>/dev/null; "
            f"grep -rlE '<[^>]*[Vv]ersion>{re.escape(bad_ver)}</[^>]*[Vv]ersion>' --include=pom.xml . 2>/dev/null"
        )
        _ec, gout, _err = _run_check_split(gcmd, project_path, timeout=30)
        poms = sorted({line.strip() for line in (gout or "").splitlines() if line.strip()})
        if not poms:
            continue
        bv = bad_ver.replace(".", r"\.")
        for pom in poms:
            # 只在含 'version' 的标签行替换 >bad<→>good<，避免误改同值的非版本标签
            scmd = (
                f"sed -i.bak -E '/[Vv]ersion>/ s#>{bv}<#>{good_ver}<#g' '{pom}' "
                f"&& rm -f '{pom}.bak'"
            )
            ec2, _out = _run_l1_command(scmd, project_path, timeout=20)
            if ec2 == 0:
                changed.add(pom)
        logger.info(
            "[L1.2.1·version-repair] %s:%s 版本 %s 不存在（仓库可用最高=%s）→ 校正为 %s（%d pom）",
            group, artifact, bad_ver,
            max(available, key=_ver_key) if available else "?", good_ver, len(poms),
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
                f"grep -c '<dependencyManagement>' '{pom}'", project_path, timeout=10
            )
            if (gmgmt or "").strip() not in ("", "0"):
                continue
            # 在该 artifact 的 <artifactId> 行后插入 <version>（模块 pom 内唯一，安全）。
            # 用 perl（GNU/BSD/沙箱皆一致，避开 sed a\ 在 BSD 上不可用），\Q\E 字面转义。
            scmd = (
                f"perl -i.bak -pe "
                f"'s#(<artifactId>\\Q{artifact}\\E</artifactId>)#$1\\n            "
                f"<version>{good_ver}</version>#' '{pom}' && rm -f '{pom}.bak'"
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
    _ec, out, _e = _run_check_split(cmd, project_path, timeout=timeout)
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
    cmd = f"grep -hoE 'import +(static +)?{pe}\\.[A-Za-z_][A-Za-z0-9_.]*' '{rel_file}' 2>/dev/null | head -4"
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
    cmd = f"curl -s -m 15 '{url}' 2>/dev/null || wget -qO- -T 15 '{url}' 2>/dev/null"
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
        f'd="{d}"; while [ -n "$d" ] && [ "$d" != "." ] && [ "$d" != "/" ]; do '
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
    cmd = f"grep -c '<artifactId>{re.escape(artifact)}</artifactId>' '{pom}' 2>/dev/null"
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
    cmd = f"curl -s -m 15 '{url}' 2>/dev/null || wget -qO- -T 15 '{url}' 2>/dev/null"
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
        f"grep -q '</dependencies>' '{pom}' && perl -0777 -i.bak -pe "
        f"'s#(.*)</dependencies>#$1    {block}\\n    </dependencies>#s' '{pom}' "
        f"&& rm -f '{pom}.bak'"
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
) -> bool:
    """构建失败是否【全因引用了尚未建出的项目内部包】(②跨模块/跨子任务未就绪)。

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
        return False
    own = _project_own_packages(project_path, timeout)
    if not own:
        return False
    internal_pkgs: set[str] = set()
    for _f, pkg in pairs:
        if any(pkg == p.rstrip(".") or pkg.startswith(p) for p in _DEP_REPAIR_SKIP_PREFIXES):
            return False  # JDK/servlet 命名空间问题，非②
        if not any(pkg == g or pkg.startswith(g + ".") for g in own):
            return False  # 有第三方缺包 → 交 dep-repair，不是纯②
        internal_pkgs.add(pkg)
    if not internal_pkgs:
        return False
    for pkg in internal_pkgs:
        cmd = (
            f"grep -rlE '^[[:space:]]*package[[:space:]]+{re.escape(pkg)}[[:space:]]*;' "
            f"--include='*.java' . 2>/dev/null | head -1"
        )
        _ec, out, _e = _run_check_split(cmd, project_path, timeout=min(timeout, 20))
        if (out or "").strip():
            return False  # 该内部包已在树里却报 does not exist → 真错(非未就绪)，照常 FAIL
    return True


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
    files = " ".join(f"'{f}'" for f in touched)
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

    返回 (修复标记, 路径列表)。cargo fix 修改的具体文件集不可知（crate 级），故路径列表
    为空——TD2606-C9 残留：依赖 rust crate 源在子任务写权 scope 内（pull-back 已覆盖）。"""
    cmd = "cargo fix --allow-dirty --allow-no-vcs --edition-idioms -q 2>&1"
    ec, out = _run_l1_command(cmd, project_path, timeout=max(timeout, 240))
    if _tool_missing(out):
        logger.info("[L1.2.1·repair] cargo 不可用，跳过 Rust 修复")
        return 0, []
    # cargo fix 可能因冲突非 0 退出，但已应用的建议仍写盘；交重跑构建仲裁
    logger.info("[L1.2.1·repair] cargo fix 已尝试套用 rustc 建议（exit=%s）", ec)
    return 1, []


def _repair_ts(project_path: str, ts_files: list[str], timeout: int) -> tuple[int, list[str]]:
    """TS/JS/Vue/前端：eslint --fix —— 自动修 import/order、可修复规则。需项目本地 eslint+config。

    返回 (修复文件数, 文件相对路径列表)，TD2606-C9 供回传。"""
    if not ts_files:
        return 0, []
    touched = list(ts_files[:60])
    files = " ".join(f"'{f}'" for f in touched)
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
    _ec, gout, _e = _run_check_split(mcmd, project_path, timeout=min(timeout, 60))
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
        scmd = (f"perl -i.bak -pe 's#\\b{re.escape(name)}\\b#{good}#g' '{rel}' "
                f"&& rm -f '{rel}.bak'")
        ec2, _o = _run_l1_command(scmd, project_path, timeout=20)
        if ec2 == 0:
            changed.add(rel)
            logger.info("[L1.2.1·symbol-repair] %s: %s→%s（项目近邻 距=%d 频=%d）",
                        rel, name, good, best_d, freq[good])
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
    try:
        proc = subprocess.run(
            _normalize_python_cmd(command), cwd=project_path, shell=True,
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
    try:
        proc = subprocess.run(
            _normalize_python_cmd(shell_cmd), cwd=project_path, shell=True,
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, (proc.stdout or ""), (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "", "command timeout"
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def _manifest_present(manifests: tuple[str, ...], project_path: str) -> bool:
    """工程 manifest(go.mod/Cargo.toml/package.json…)是否存在，沙箱优先。

    沙箱模式下本地只有可写文件，manifest 多半不在本地——旧的 os.path.isfile(本地)
    会误判"无 manifest 而跳过 lint"。沙箱里在远程工作目录(深度 3 内)查。
    """
    ctx = _sandbox_ctx()
    if ctx is not None:
        sandbox, manager, remote = ctx
        names = " -o ".join(f"-name {m!r}" for m in manifests)
        try:
            cr = manager.run_command(
                sandbox,
                f"find {remote} -maxdepth 3 \\( {names} \\) -print -quit 2>/dev/null | head -1",
                timeout=20,
            )
            return bool((cr.stdout or "").strip())
        except Exception:  # noqa: BLE001
            return False
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
        return any(os.path.isfile(os.path.join(project_path, n)) for n in names)

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
        names = " -o ".join(f"-name {m!r}" for m in manifests)
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


def _scope_violations(diff: str, scope: FileScope) -> list[str]:
    modified = files_from_unified_diff(diff)
    # 可写权限 = writable + create_files + delete_files（FileScope 契约，见 is_writable）。
    # bug 修复(task 9da731ab)：原仅检查 writable，把【新建文件】(create_files)误判越权 →
    # tech_design file_plan 含新建文件的任务必然 L1 失败 → replan 死循环。create_files 是合法可写。
    allowed = set(scope.writable or []) | set(getattr(scope, "create_files", []) or []) \
        | set(getattr(scope, "delete_files", []) or [])
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
        cmd = f"{py_bin} -m py_compile " + " ".join(f'"{f}"' for f in _cap_files(py_files, "py_compile"))
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
            elif rc != 0 and "error TS" in combined:
                return False, combined.strip()[:1000]
        except Exception as exc:
            # audit #11：tsc 编译失败可能掩盖真实编译错误，从 debug 升 warning（生产可见）
            logger.warning("[L1.2] tsc 编译跳过（异常）: %s", exc)

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
            "npx eslint --format json " + " ".join(f'"{f}"' for f in _cap_files(js_ts, "eslint")),
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


def _lint_go(project_path: str, go_files: list[str], *, timeout: int = 60) -> tuple[bool, list[str], list[dict]]:
    """Go: go vet ./...（在 project_path 跑；非0退出且有 error 输出才算 has_error）。"""
    has_error = False
    messages: list[str] = []
    issues: list[dict] = []

    # go vet ./... 需完整 module 树+工具链 → 沙箱优先(A-P1-10)。无沙箱才要求本地有 go。
    if _sandbox_ctx() is None and not _find_tool("go"):
        messages.append("go 未安装，跳过 Go lint")
        return has_error, messages, issues

    # 无 go.mod 时 go vet 无法跑，跳过（沙箱感知判定，对齐 eslint 无配置则跳过的风格）
    if not _manifest_present(("go.mod",), project_path):
        messages.append("项目无 go.mod，跳过 Go lint")
        return has_error, messages, issues

    try:
        rc, out, err = _run_check_split("go vet ./...", project_path, timeout=timeout)
        err_output = (err or "").strip() or (out or "").strip()
        if rc == 124:
            messages.append("go vet 超时")
        elif rc != 0 and _is_infra_failure(err_output):
            # 无网拉依赖/工具缺失等基础设施瞬时错误 → skip 非 error(A-P1-09)，避免错误降级
            messages.append(f"go vet 基础设施/工具瞬时错误，跳过(非能力失败): {err_output[:200]}")
        elif rc != 0 and err_output:
            for line in err_output.splitlines():
                line = line.strip()
                if not line:
                    continue
                issue_entry: dict = {
                    "file": "",
                    "line": None,
                    "code": "govet",
                    "message": line,
                    "severity": "error",
                }
                # 尝试解析 file:line:col: message 格式
                parts = line.split(":")
                if len(parts) >= 2:
                    issue_entry["file"] = parts[0]
                    try:
                        issue_entry["line"] = int(parts[1])
                    except ValueError:
                        pass
                issues.append(issue_entry)
            has_error = True
    except Exception as exc:
        messages.append(f"go vet 跳过: {exc}")
    return has_error, messages, issues


def _lint_rust(project_path: str, rs_files: list[str], *, timeout: int = 60) -> tuple[bool, list[str], list[dict]]:
    """Rust: cargo clippy -- -D warnings（clippy 把 warning 当 error）。"""
    has_error = False
    messages: list[str] = []
    issues: list[dict] = []

    # cargo clippy 需完整 crate 树+工具链 → 沙箱优先(A-P1-10)。无沙箱才要求本地有 cargo。
    if _sandbox_ctx() is None and not _find_tool("cargo"):
        messages.append("cargo 未安装，跳过 Rust lint")
        return has_error, messages, issues

    # 无 Cargo.toml 时 cargo clippy 无法跑，跳过（沙箱感知判定）
    if not _manifest_present(("Cargo.toml",), project_path):
        messages.append("项目无 Cargo.toml，跳过 Rust lint")
        return has_error, messages, issues

    try:
        rc, out, err = _run_check_split("cargo clippy -- -D warnings", project_path, timeout=timeout)
        err_output = (err or "").strip() or (out or "").strip()
        if rc == 124:
            messages.append("cargo clippy 超时")
        elif rc != 0 and _is_infra_failure(err_output):
            # 无网拉 crate/文件锁/工具缺失等基础设施瞬时错误 → skip 非 error(A-P1-09)
            messages.append(f"cargo clippy 基础设施/工具瞬时错误，跳过(非能力失败): {err_output[:200]}")
        elif rc != 0 and err_output:
            for line in err_output.splitlines():
                line = line.strip()
                if not line:
                    continue
                # 跳过摘要行
                if line.startswith("warning: generated") or line.startswith("error: aborting"):
                    continue
                if ": error[" in line or ": warning[" in line or line.startswith("error:"):
                    issue_entry: dict = {
                        "file": "",
                        "line": None,
                        "code": "clippy",
                        "message": line,
                        "severity": "error",  # -D warnings => all warnings are errors
                    }
                    # 尝试解析 file:line:col 格式
                    # Rust 输出: src/main.rs:2:5: error[E0425]: ...
                    for prefix in line.split(": "):
                        parts = prefix.split(":")
                        if len(parts) >= 2:
                            try:
                                int(parts[1])
                                issue_entry["file"] = parts[0]
                                issue_entry["line"] = int(parts[1])
                                break
                            except ValueError:
                                continue
                    issues.append(issue_entry)
            if issues:
                has_error = True
    except Exception as exc:
        messages.append(f"cargo clippy 跳过: {exc}")
    return has_error, messages, issues


def _lint_java(project_path: str, java_files: list[str], *, timeout: int = 60) -> tuple[bool, list[str], list[dict]]:
    """Java/Kotlin: checkstyle（找不到 checkstyle 就 skip，不报错）。"""
    has_error = False
    messages: list[str] = []
    issues: list[dict] = []

    # checkstyle 沙箱优先(A-P1-10)。无沙箱才要求本地有 checkstyle；沙箱里多半未装 →
    # 命中 "command not found" 走基础设施 skip。
    if _sandbox_ctx() is None and not _find_tool("checkstyle"):
        messages.append("checkstyle 未安装，跳过 Java lint")
        return has_error, messages, issues

    # P2-F：沙箱里多半未装 checkstyle——开跑前先廉价探在场，缺则直接 skip，省掉注定 exit 127
    # 的命令（减少白跑往返与日志噪声；996db614 每个过编的子任务都白敲一次 checkstyle）。
    if _sandbox_ctx() is not None:
        _pc, _po = _run_l1_command(
            "command -v checkstyle >/dev/null 2>&1 && echo __HAS__ || echo __NO__",
            project_path, timeout=15,
        )
        if "__HAS__" not in (_po or ""):
            messages.append("checkstyle 未安装(沙箱)，跳过 Java lint")
            return has_error, messages, issues

    try:
        cmd = "checkstyle " + " ".join(f'"{f}"' for f in _cap_files(java_files, "checkstyle"))
        rc, out, err = _run_check_split(cmd, project_path, timeout=timeout)
        err_output = (err or "").strip() or (out or "").strip()
        if rc == 124:
            messages.append("checkstyle 超时")
        elif rc != 0 and _is_infra_failure(err_output):
            # 工具缺失/无网等基础设施瞬时错误 → skip 非 error(A-P1-09)
            messages.append(f"checkstyle 基础设施/工具瞬时错误，跳过(非能力失败): {err_output[:200]}")
        elif rc != 0 and err_output:
            for line in err_output.splitlines():
                line = line.strip()
                if not line:
                    continue
                issue_entry: dict = {
                    "file": "",
                    "line": None,
                    "code": "checkstyle",
                    "message": line,
                    "severity": "error",
                }
                # 尝试解析 [ERROR] file:line:col: message 格式
                import re
                m = re.match(r"\[(?:ERROR|WARN)\]\s+(.+?):(\d+)", line)
                if m:
                    issue_entry["file"] = m.group(1)
                    issue_entry["line"] = int(m.group(2))
                issues.append(issue_entry)
            has_error = True
    except Exception as exc:
        messages.append(f"checkstyle 跳过: {exc}")
    return has_error, messages, issues


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
        passed = bool(result.get("passed", True))
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


def _normalize_python_cmd(cmd: str) -> str:
    """把命令里的裸 `python`/`python3` 归一到本机可用解释器。

    Brain/LLM 生成的 harness 常写 `python ...`，但本机(确定性闸门运行处)可能只有
    `python3`(macOS 常见)。沙箱里 python 存在，本地却 command not found —— 这类
    环境漂移会让确定性验证误判。统一替换前缀。
    """
    if not cmd:
        return cmd
    py = _python_bin()
    if py == "python":
        return cmd
    # 替换行首或 && / ; / | 后的裸 python（不动 python3 已正确的情况）
    import re
    return re.sub(r"(^|[\s;&|])python(?=\s)", lambda m: f"{m.group(1)}{py}", cmd)


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
    # TD2606-C6：按【最长模块路径前缀】匹配改动文件 → 命中最深叶子模块（而非首段聚合器）。
    paths = sorted(modules.values(), key=len, reverse=True)
    hit: list[str] = []
    for f in modified:
        fp = str(f).strip().lstrip("/")
        for mp in paths:
            if fp == mp or fp.startswith(mp + "/"):
                if mp not in hit:
                    hit.append(mp)
                break  # 命中最深模块即止（paths 已按长度降序）
    if not hit:
        return command
    pl = ",".join(hit)
    # 插到 mvn 之后：mvn <args> → mvn -pl <pl> -am <args>
    return command.replace("mvn", f"mvn -pl {pl} -am", 1)


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
) -> tuple[bool, dict[str, Any]]:
    """L1.1 scope → L1.2 compile → L1.2.5 lint → L1.3 scoped test → L1.4 LLM 自检。

    Args:
        project_path: 项目根目录
        subtask: 子任务定义
        diff: 变更 diff
        timeout: 各阶段超时秒数
        llm: 可选 LLM 句柄，用于 L1.4 自检阶段；不传则自检跳过
        project_stack: 权威栈画像（detect_stack 产）；驱动构建失败时的跨生态 repair adapter 选择
    """
    details: dict[str, Any] = {"pipeline": "L1.1-L1.4"}

    # ── L1.1 scope 检查 ──
    violations = _scope_violations(diff, subtask.scope)
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
        build_cmd = _scope_maven_command(build_cmd, project_path, modified)
    if build_cmd and _build_cmd_applicable(build_cmd, project_path):
        logger.info("[L1.2.1] 执行构建闸门: %s", build_cmd)
        b_ec, b_out = _run_l1_command(build_cmd, project_path, timeout=max(timeout, 300))
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
                _loop_budget = _max_build_repair_seconds()
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
                        build_cmd, project_path, timeout=max(timeout, 300)
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
                details["repaired_file_paths"] = repaired_paths
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
                # P0-B：错误归属——构建错若【全在本子任务模块之外的上游模块】（如 -pl ruoyi-alarm
                # 但报错在 ruoyi-generator 的坏 pom），是上游子任务没收尾，非本子任务能力问题。
                # 标 BLOCKED 交退避重试（待上游 pom 被其 owner/对账修好），不烧本子任务修复轮——
                # 杜绝一个坏 pom 经 -am reactor 连坐拖死十几个无辜子任务（996db614 实测）。
                if _build_error_is_upstream(b_out, build_cmd, modified):
                    details["l1_2_1_build_ok"] = None
                    details["build_blocked"] = build_cmd
                    details["pipeline_blocked"] = "upstream_module_broken"
                    details["not_run_kind"] = NotRunKind.BLOCKED.value
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
                if _build_blocked_on_unbuilt_internal(project_path, b_out, timeout):
                    details["l1_2_1_build_ok"] = None
                    details["build_blocked"] = build_cmd
                    details["pipeline_blocked"] = "internal_pkg_not_built"
                    details["not_run_kind"] = NotRunKind.BLOCKED.value
                    logger.warning(
                        "[L1.2.1] 构建缺【尚未建出的项目内部包】(②跨模块/跨子任务未就绪) → 标 "
                        "BLOCKED 退避待生产者落地，不连坐本子任务: %s", (b_out or "")[:200],
                    )
                    return True, details
                details["build_failed"] = build_cmd
                return False, details
    elif build_cmd:
        # Brain 指定了 build_command（即【期望】这是可构建项目），但工程清单(pom/go.mod/...)在同步后
        # 的树里定位不到 → 本应构建却跑不起来。fail-closed：标 BLOCKED（TD2606-B7），不再静默当
        # 「跳过=通过」。多因模块源同步不全/清单未上传 → 交裁决器走 transient 重试。
        # 注：_build_cmd_applicable 的 find -maxdepth 3 本身偏浅（深 monorepo 会漏），Wave 4 修
        # 该定位逻辑以降低误标 BLOCKED；当前先 fail-closed（重试有上限，绝不静默通过）。
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
                details["lint"]["note"] = "lint 语法级 error 硬阻断流水线"
                details["lint"]["gated"] = True
                return False, details
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
        verify_results = []
        for vc in verify_cmds:
            v_ec, v_out = _run_l1_command(vc, project_path, timeout=timeout)
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
