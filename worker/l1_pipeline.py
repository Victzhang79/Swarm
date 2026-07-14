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
#
# god-file 轻拆：上述纯解析/纯文本重写函数（rewrite_jvm_namespace / parse_missing_* / _ver_key /
# _choose_valid_version / rewrite_dependency_version / rewrite_property_version 等）已抽到
# worker/l1_parse.py（无副作用叶簇，【不反向 import】本模块）。此处 re-export 保持既有
# `from swarm.worker.l1_pipeline import <fn>` 调用点（executor_sync / 测试）零改动、可寻址。
from swarm.worker.l1_parse import (  # noqa: F401  (re-export，供既有调用点)
    _choose_valid_version,
    pick_latest_stable,
    _is_reserved_maven_property,
    _ver_key,
    parse_missing_artifacts,
    parse_missing_packages,
    parse_missing_versions,
    rewrite_dependency_version,
    rewrite_jvm_namespace,
    rewrite_property_version,
)


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


# 另一类形态：模型给依赖【根本没写 <version>】且父 dependencyManagement 也不管它 →
# `'dependencies.dependency.version' for G:A:jar is missing`（pom 解析期错，早于 artifact 解析）。
# 与「版本写错」是同一【模型手写依赖坐标不可靠】问题类的不同表象——统一在依赖对账里处理，
# 不再逐错加正则（避免 §0 的 whack-a-mole）。


def _fetch_maven_versions_probe(
    group: str, artifact: str, project_path: str, timeout: int
) -> tuple[list[str], bool]:
    """查仓库真实可用版本 → **(versions, reachable)**。

    ★为什么必须返回 reachable★（R56-6 治本，round56 后自审揪出）：
    「仓库**确证**查无此 artifact」与「仓库**根本没连上**（断网/curl 缺失/两仓 5xx）」在旧实现里
    **同样返回 []**——于是所有"空列表 ⇒ 坐标不可解析 ⇒ 剪除"的判定，在**沙箱一断网时会把全工程
    的合法第三方依赖全部剪光**。这正是本系统最不能犯的错（误剪合法依赖 ≫ 漏过坏坐标：后者下游
    还有闸，前者直接毁产物）。剪除是**不可逆**动作，必须建立在**肯定证据**（仓库确证 404）之上，
    绝不能建立在**证据缺失**（没连上）之上。

    reachable=True 的判据（二者其一，须是**肯定**证据）：
      · 取到了版本列表（不论哪个仓库、curl 还是 wget）；
      · HTTP 状态码确证为 404（仓库答复了："我这儿没有它"）。
    只要没有任何仓库给出肯定证据 → reachable=False → 调用方一律 fail-open（放行，绝不剪）。
    """
    gpath = group.replace(".", "/")
    urls = [
        f"https://maven.aliyun.com/repository/public/{gpath}/{artifact}/maven-metadata.xml",
        f"https://repo1.maven.org/maven2/{gpath}/{artifact}/maven-metadata.xml",
    ]
    reachable = False
    for url in urls:
        # -w 把 HTTP 码贴在正文尾部：区分「404=确证没有」与「000/5xx/超时=没连上」的唯一手段。
        # wget 兜底不带状态码 → 只能用于**肯定**结论（拿到版本），拿不到时不敢断言"仓库没有"。
        cmd = (f"curl -s -m 15 -w '\\n__HTTP__%{{http_code}}' {shlex.quote(url)} 2>/dev/null "
               f"|| wget -qO- -T 15 {shlex.quote(url)} 2>/dev/null")
        _ec, out = _run_l1_command(cmd, project_path, timeout=min(timeout, 30))
        if _tool_missing(out):
            continue
        body = out or ""
        m = re.search(r"__HTTP__(\d{3})\s*$", body)
        code = m.group(1) if m else ""
        versions = re.findall(r"<version>([^<]+)</version>", body)
        if versions:
            return [v.strip() for v in versions if v.strip()], True
        if code == "404":
            reachable = True   # 仓库确证答复"没有它"——这才是可据以剪除的肯定证据
        # 000（连不上）/5xx（仓库故障）/无状态码（wget 路径） → 不构成任何结论，试下一个仓库
    return [], reachable


def _fetch_maven_versions(group: str, artifact: str, project_path: str, timeout: int) -> list[str]:
    """兼容旧调用点：只要版本列表（空 = 没拿到，**不区分**查无与不可达）。

    ⚠️ 任何要据"空列表"做**剪除/否定**判定的调用点，必须改用 `_fetch_maven_versions_probe`
    并检查 reachable——否则断网即误剪（见 probe 的文档）。
    """
    versions, _reachable = _fetch_maven_versions_probe(group, artifact, project_path, timeout)
    return versions




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


def _inject_dep_version_in_blocks(
        text: str, group: str, artifact: str, version: str) -> str | None:
    """R47-3：仅在 <dependency> 块内为匹配依赖注入缺失 <version> → 新文本或 None。

    块内已有 <version>、artifactId 不匹配、groupId 明确不匹配 → 原样保留（幂等）。
    工程/<parent> 的 artifactId 声明不在 <dependency> 块内，天然免疫误插（旧 perl
    盲插正是在工程自身声明旁再插一个 → Duplicated tag Non-parseable，round47 实锤）。
    复核 F2：匹配前剥 <exclusions>（exclusion 撞名会把 version 插进 exclusions 块）；
    复核 F5：groupId 是 ${属性} 引用时不可字面比对 → 放行到 artifactId 匹配（fail-open）。
    模块级函数：测试必须 import 真身（禁"抄本测试"假绿）。"""
    hits = 0

    def _fix(m: "re.Match[str]") -> str:
        nonlocal hits
        blk = m.group(0)
        inner = re.sub(r"<exclusions>.*?</exclusions>", "", m.group(1), flags=re.S)
        if "<version>" in inner:
            return blk
        if not re.search(
                r"<artifactId>\s*" + re.escape(artifact) + r"\s*</artifactId>", inner):
            return blk
        g = re.search(r"<groupId>\s*([^<\s]+)\s*</groupId>", inner)
        if group and g and not g.group(1).startswith("${") and g.group(1) != group:
            return blk
        # 插入点锚定【exclusions 之外】的 artifactId 出现（撞名 exclusion 排前时
        # 首次出现在 exclusions 内，盲取首个会把 version 插进 exclusions 块）
        exc_spans = [mm.span() for mm in re.finditer(
            r"<exclusions>.*?</exclusions>", blk, re.S)]
        for am in re.finditer(
                r"<artifactId>\s*" + re.escape(artifact) + r"\s*</artifactId>", blk):
            if any(s <= am.start() < e for s, e in exc_spans):
                continue
            hits += 1
            return (blk[:am.end()]
                    + f"\n            <version>{version}</version>" + blk[am.end():])
        return blk

    new_text = re.sub(r"<dependency>(.*?)</dependency>", _fix, text, flags=re.S)
    return new_text if hits else None


def _reactor_artifacts(project_path: str) -> set[str]:
    """reactor 内部模块 artifactId 集合。纯文本确定性。

    ★必须**递归**走 <module>★（治 round46 "reactor missing-child" 一族）：Maven 多级 reactor
    里，中间聚合模块（如 `ruoyi-modules/pom.xml`）会再声明自己的 <modules>——只扫根 pom 会漏掉
    全部孙模块。漏掉的后果不是"少修一点"，而是**合法性闸规则②把依赖它们的合法兄弟依赖当幻影剪除**
    （该规则以"仓库里永远没有工程模块"为由无条件剪，**没有 fail-open 出口**）→ 误剪真依赖。

    每个 pom 同时贡献两个名字：目录名（兜底，子 pom 读不到时仍认成员）与它自己的 artifactId
    （权威，目录名与 artifactId 常不一致）。读取有上限，防病态深树把预算读穿。
    """
    import posixpath

    mods: set[str] = set()
    seen: set[str] = set()
    stack: list[str] = ["pom.xml"]
    while stack and len(seen) < 80:
        rel = stack.pop()
        if rel in seen:
            continue
        seen.add(rel)
        txt = _read_project_file(project_path, rel, timeout=20) or ""
        if not txt:
            continue
        txt = re.sub(r"<!--.*?-->", "", txt, flags=re.S)
        body = re.sub(r"<parent>.*?</parent>", "", txt, flags=re.S)
        body = re.sub(r"<dependencyManagement>.*?</dependencyManagement>", "", body, flags=re.S)
        body = re.sub(r"<dependencies>.*?</dependencies>", "", body, flags=re.S)
        own = re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", body)
        if own:
            mods.add(own.group(1))
        base = posixpath.dirname(rel)
        for m in re.findall(r"<module>\s*([^<\s]+)\s*</module>", txt):
            m = m.strip().rstrip("/")
            mods.add(m.rsplit("/", 1)[-1])           # 目录名兜底
            child = posixpath.normpath(posixpath.join(base, m))
            child_pom = child if child.endswith(".xml") else posixpath.join(child, "pom.xml")
            if not child_pom.startswith(".."):        # 绝不越出工程树
                stack.append(child_pom)
    return mods


def _same_release_train(a1: str, a2: str) -> bool:
    """两个 artifactId 是否属同一发布列车（共享 ≥2 段公共前缀词元）。

    spring-boot-starter-aop ↔ spring-boot-dependencies → 共享 ["spring","boot"] → True
    easyexcel ↔ druid-spring-boot-4-starter            → 无公共前缀              → False
    """
    t1 = a1.lower().split("-")
    t2 = a2.lower().split("-")
    n = 0
    for x, y in zip(t1, t2):
        if x != y:
            break
        n += 1
    return n >= 2


def _group_family_version(project_path: str, group: str, artifact: str = "") -> str | None:
    """R54-5：工程里【同 groupId 家族】已经在用的版本（root pom 证据，${prop} 展开）。

    round54 实锤：`spring-boot-starter-aop` 在 Spring Boot 4 里**已不存在**（改名 aspectj），
    L1 去 Central 找"最新稳定版"找到的是 **Boot 3 系的 3.5.16**，注进了 Boot 4.0.6 的工程 →
    跨大版本混用（Spring 6 vs 7）。**稳定 ≠ 与本工程兼容**：版本闸只挡住了预发布，挡不住"版本
    对、代际错"。工程自己已经为该 groupId 钉过一个版本（这里是 spring-boot.version=4.0.6），
    那才是唯一正确的对齐目标。返回 None = 该 group 在工程里没有先例（按最新稳定版走，旧行为）。

    R56-3（round56 活体误伤，修正 R54-5）：**同 groupId ≠ 同一个发布列车**。`com.alibaba` 是伞形
    groupId——底下住着 druid(1.2.28)、easyexcel(4.0.3)、fastjson…**彼此毫无版本关系**。原实现拿
    "工程里 com.alibaba 钉在 1.2.28"去判定 easyexcel(4.0.3) "跨代"，把一个**合法依赖直接剪掉**
    （代码用到它就编译失败）。判据收紧为【同发布列车】：目标 artifactId 与已钉 artifactId 必须共享
    有意义的公共前缀（≥2 段词元，如 spring-boot-starter-aop ↔ spring-boot-dependencies 共享
    "spring-boot"）。无共享前缀 = 不同产品线 → 不对齐（按最新稳定版注入，旧行为）。
    """
    txt = _read_project_file(project_path, "pom.xml", timeout=20) or ""
    txt = re.sub(r"<!--.*?-->", "", txt, flags=re.S)
    for blk in re.finditer(r"<dependency>(.*?)</dependency>", txt, re.S):
        b = re.sub(r"<exclusions>.*?</exclusions>", "", blk.group(1), flags=re.S)
        g = re.search(r"<groupId>\s*([^<\s]+)\s*</groupId>", b)
        a = re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", b)
        v = re.search(r"<version>\s*([^<]+?)\s*</version>", b)
        if not (g and v and g.group(1) == group):
            continue
        # R56-3：同发布列车才算"家族"——artifactId 须共享 ≥2 段公共前缀词元
        if artifact and a and not _same_release_train(artifact, a.group(1)):
            continue
        val = v.group(1).strip()
        m = re.fullmatch(r"\$\{([^}]+)\}", val)
        if not m:
            return val
        prop = re.escape(m.group(1))
        pm = re.search(rf"<{prop}>\s*([^<\s]+)\s*</{prop}>", txt)
        if pm:
            return pm.group(1)
    return None


def _project_group(project_path: str) -> str | None:
    """工程自身 groupId（根 pom 坐标区，剥 parent/依赖/构建块后的首个 groupId）。"""
    txt = _read_project_file(project_path, "pom.xml", timeout=20) or ""
    txt = re.sub(r"<!--.*?-->", "", txt, flags=re.S)
    body = re.sub(r"<parent>.*?</parent>", "", txt, flags=re.S)
    body = re.sub(r"<dependencyManagement>.*?</dependencyManagement>", "", body, flags=re.S)
    body = re.sub(r"<dependencies>.*?</dependencies>", "", body, flags=re.S)
    body = re.sub(r"<build>.*?</build>", "", body, flags=re.S)
    m = re.search(r"<groupId>\s*([^<\s]+)\s*</groupId>", body)
    return m.group(1) if m else None


def _fix_reactor_dep_group(text: str, artifact: str, project_group: str,
                           reactor_mods: set[str] | None = None) -> str | None:
    """R54-6：把【reactor 内部模块】依赖的臆造 groupId 改回工程自己的 → 新文本或 None。

    round54 实锤：`alarm-schedule/pom.xml` 依赖兄弟模块写成 `com.alarm:alarm-core`（工程真实
    groupId 是 com.ruoyi）→ Maven 当成外部依赖去远程仓库拉 → `Could not find artifact
    com.alarm:alarm-core:jar:4.8.3` → 整个模块解析失败。

    这是幻影坐标的第三种形态，**逃过 R53-2**（它只剪"无 version 且非 reactor 模块"的）：
    此处**有** version、artifactId **确实是** reactor 模块，只有 groupId 是编的。判据是硬的、
    零歧义：artifactId 是 reactor 成员 → 它的 groupId 只能是工程 groupId（模块由本工程构建，
    不可能来自任何外部 group）。故直接改写，不猜、不删。
    """
    if not (artifact and project_group):
        return None
    # fail-closed 自守门：artifact **必须**被证明是 reactor 成员才允许改写 groupId。
    # 只靠调用方守门 → 本函数一旦被别处误用就成了"给第三方 artifact 安上工程 groupId"的
    # 伪造器（正是 R47-2 铁律禁的、round47 毒死整棵树的那件事）。
    if reactor_mods is not None and artifact not in reactor_mods:
        return None
    hits = 0

    def _fix(m: "re.Match[str]") -> str:
        nonlocal hits
        blk = m.group(0)
        inner = re.sub(r"<exclusions>.*?</exclusions>", "", m.group(1), flags=re.S)
        if not re.search(r"<artifactId>\s*" + re.escape(artifact) + r"\s*</artifactId>", inner):
            return blk
        g = re.search(r"<groupId>\s*([^<\s]+)\s*</groupId>", inner)
        if not g or g.group(1).startswith("${") or g.group(1) == project_group:
            return blk
        hits += 1
        return blk.replace(f"<groupId>{g.group(1)}</groupId>",
                           f"<groupId>{project_group}</groupId>", 1)

    new_text = re.sub(r"<dependency>(.*?)</dependency>", _fix, text, flags=re.S)
    return new_text if hits else None


def _prune_dep_blocks(text: str, group: str, artifact: str,
                      even_with_version: bool = False) -> str | None:
    """R53-2：剪除【无 <version> 且匹配坐标】的 <dependency> 块 → 新文本或 None（未命中）。

    与 _inject_dep_version_in_blocks 严格对称：块内已有 <version> / artifactId 不匹配 /
    groupId 明确不匹配 → 原样保留。剥 <exclusions> 防撞名误剪。只剪无版本的那一类——
    有版本的坏依赖顶多解析失败（可归因），无版本又无人管的会让 Maven 连 reactor 都读不出（全局）。
    模块级函数：测试 import 真身（禁抄本测试假绿）。"""
    hits = 0

    def _cut(m: "re.Match[str]") -> str:
        nonlocal hits
        blk = m.group(0)
        inner = re.sub(r"<exclusions>.*?</exclusions>", "", m.group(1), flags=re.S)
        # R56-4：默认只剪无版本的（保守）；even_with_version=True 时连带有版本的一起剪——
        # 用于【可证永不可解析】的坐标（工程 groupId 但非 reactor 模块 / 仓库查无任何版本）。
        if "<version>" in inner and not even_with_version:
            return blk
        if not re.search(r"<artifactId>\s*" + re.escape(artifact) + r"\s*</artifactId>", inner):
            return blk
        g = re.search(r"<groupId>\s*([^<\s]+)\s*</groupId>", inner)
        if group and g and not g.group(1).startswith("${") and g.group(1) != group:
            return blk
        hits += 1
        return ""

    new_text = re.sub(r"[ \t]*<dependency>(.*?)</dependency>\s*\n?", _cut, text, flags=re.S)
    return new_text if hits else None



def _fix_parent_version_literal(text: str, root_text: str) -> str | None:
    """R58-2：把 `<parent><version>` 里的**属性引用**还原成字面量 → 新文本；无需改则 None。

    ★Maven 硬规则★ parent 的版本**必须是字面量**：属性定义在**父 pom 里**，而 Maven 解析
    parent 坐标时**还没加载父 pom**（先有鸡还是先有蛋）→ `${x.version}` 永远解析不了。

    round58 死因实锤：
        [FATAL] Non-resolvable parent POM for com.ruoyi:alarm-api:${ruoyi.version}:
                Could not find artifact com.ruoyi:ruoyi:pom:${ruoyi.version}
    这是 **pom 解析期**崩塌 → 整棵 reactor 读不出 → 全员构建闸 BLOCKED（round51-53 同一死法）。

    fail-open：根 pom 拿不到**字面** version（继承 GAV 等）→ 不动（绝不猜版本）。
    只动 <parent> 块内的 version——依赖块里的 `${...}` 版本是**合法的**，误改会毁掉工程统一版本。
    """
    m = re.search(r"<parent>(.*?)</parent>", text, re.S)
    if not m:
        return None
    inner = m.group(1)
    vm = re.search(r"<version>\s*(\$\{[^}]+\})\s*</version>", inner)
    if not vm:
        return None   # 已是字面量（或没写 version）→ 一个字符都不动
    rv = re.search(r"<version>\s*([^<${\s][^<]*?)\s*</version>",
                   re.sub(r"<(dependencies|dependencyManagement|build|parent)>.*?</\1>", "",
                          re.sub(r"<!--.*?-->", "", root_text, flags=re.S), flags=re.S))
    if not rv:
        return None   # 根 pom 无字面 version → fail-open，绝不猜
    fixed_inner = inner.replace(vm.group(0), f"<version>{rv.group(1).strip()}</version>", 1)
    return text.replace(m.group(0), f"<parent>{fixed_inner}</parent>", 1)


def _enforce_parent_version_literals(project_path: str, timeout: int) -> tuple[int, list[str]]:
    """R58-2：构建前把全树 pom 的 parent 属性版本还原成字面量（比依赖合法性闸更早、更致命）。"""
    root_text = _read_project_file(project_path, "pom.xml", timeout=20)
    if not root_text:
        return 0, []
    _ec, gout, _e = _run_check_split(
        "find . -name pom.xml -not -path '*/target/*' 2>/dev/null", project_path, timeout=30)
    if _ec != 0:
        logger.warning("[L1.2.1·parent-version] manifest 扫描失败(ec=%s) → 本轮闸未运行", _ec)
        return 0, []
    changed: list[str] = []
    for rel in sorted({ln.strip().lstrip("./") for ln in (gout or "").splitlines() if ln.strip()})[:60]:
        if rel == "pom.xml":
            continue   # 根 pom 的 parent（若有）指向工程外，不归本闸管
        t = _read_project_file(project_path, rel, timeout=20)
        if not t:
            continue
        new = _fix_parent_version_literal(t, root_text)
        if new and _write_project_file(project_path, rel, new, timeout=20):
            changed.append(rel)
    if changed:
        logger.warning(
            "[L1.2.1·parent-version] R58-2 %d 个 pom 的 <parent><version> 是属性引用 → 还原为字面量：%s"
            "\n  Maven 解析 parent 时还没加载父 pom，属性永远解析不了 → pom 解析期崩塌、整棵 reactor "
            "读不出、全员构建闸 BLOCKED（round58 实锤死因）", len(changed), changed[:8])
    return len(changed), changed


def _enforce_dep_legality(project_path: str, timeout: int) -> tuple[int, list[str]]:
    """R56-5：构建**之前**对全树 pom 施加依赖合法性不变量（state-driven，不看 Maven 报什么错）。

    收敛 R53-2/R54-5/R54-6/R56-4 四条 error-driven 分支——它们全是"等 Maven 报出一种新错法，
    再针对那句错误文本加一条分支"，**换个错法就漏一个**（用户点破：这就是打地鼠）。
    本闸只看 pom 的**状态**：每条依赖必须满足「reactor 模块 / 父级受管 / 仓库真实存在」三者之一，
    否则确定性处置。旧分支保留为兜底（网络抖动/边角），但问题在进 Maven 之前就已被消掉。

    fail-open 铁律：仓库不可达 → 一律放行（宁可漏判，绝不误剪合法依赖）。
    """
    from swarm.worker.dep_legality import driver_for, enforce

    drv = driver_for("maven")   # 新栈=注册 driver（dep_legality.DRIVERS），闸与不变量本身零栈耦合
    if drv is None:
        return 0, []
    root_text = _read_project_file(project_path, "pom.xml", timeout=20)
    if not root_text:
        return 0, []
    _ec, gout, _e = _run_check_split(
        "find . -name pom.xml -not -path '*/target/*' 2>/dev/null", project_path, timeout=30)
    if _ec != 0:
        # 扫不到 ≠ 没有——沉默返回会让"闸本轮压根没跑"伪装成"扫完没问题"
        logger.warning("[L1.2.1·dep-legality] manifest 扫描失败(ec=%s) → 本轮合法性闸未运行: %s",
                       _ec, (_e or "")[:200])
        return 0, []
    rels = sorted({ln.strip().lstrip("./") for ln in (gout or "").splitlines() if ln.strip()})
    if not rels:
        return 0, []
    texts: dict[str, str] = {}
    for rel in rels[:60]:
        t = _read_project_file(project_path, rel, timeout=20)
        if t:
            texts[rel] = t
    if not texts:
        return 0, []

    _cache: dict[tuple[str, str], list[str] | None] = {}

    def _versions(group: str, artifact: str):
        """契约：**不可达 → None**（fail-open，绝不据此剪除）；确证查无 → []。
        R56-6：旧实现把两者都返回 []，断网即把全工程合法依赖剪光——证据缺失 ≠ 否定证据。"""
        key = (group, artifact)
        if key not in _cache:
            try:
                vers, reachable = _fetch_maven_versions_probe(
                    group, artifact, project_path, timeout)
                _cache[key] = vers if (vers or reachable) else None
            except Exception as _fx:  # noqa: BLE001 —— 取数层自身故障同样按"不可达"处理
                # 但**必须响亮**：若取数层有恒抛的 bug，静默吞掉会让规则③（仓库真实存在）
                # 永久失效——闸照常播报"处置 N 条"，最关键的一条规则却已悄悄瘫痪。
                logger.warning("[L1.2.1·dep-legality] 仓库查询异常（按不可达 fail-open）"
                               "%s:%s → %s", group, artifact, _fx)
                _cache[key] = None
        return _cache[key]

    new_texts, actions = enforce(
        texts, root_text=root_text, namespace=_project_group(project_path),
        workspace_members=_reactor_artifacts(project_path), registry_versions=_versions,
        driver=drv,
    )
    changed: list[str] = []
    for rel, txt in new_texts.items():
        if _write_project_file(project_path, rel, txt, timeout=20):
            changed.append(rel)
    if actions:
        logger.warning(
            "[L1.2.1·dep-legality] R56-5 构建前依赖合法性闸：处置 %d 条（%d pom 改写）——"
            "不变量=每条依赖须满足【reactor 模块 / 父级受管 / 仓库真实存在】三者之一：\n  %s",
            len(actions), len(changed), "\n  ".join(actions[:12]))
    return len(changed), sorted(changed)


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
    _reactor_mods: set[str] | None = None
    _proj_group: str | None = None
    # ── ① 版本写错/不存在 → 校正 ──
    for group, artifact, bad_ver in missing[:8]:
        # R54-6（round54 实锤）：`Could not find artifact com.alarm:alarm-core:jar:4.8.3` ——
        # artifact 其实是 **reactor 内部模块**，只是 groupId 被 LLM 编错（工程真身 com.ruoyi）→
        # Maven 当外部依赖去远程仓库拉 → 整模块解析失败。这类**绝不能**走"查仓库校正版本"
        # （仓库里本就不该有它），必须把 groupId 改回工程自己的：artifactId 是 reactor 成员 →
        # 其 groupId 只能是工程 groupId（模块由本工程构建，不可能来自任何外部 group），零歧义。
        if _reactor_mods is None:
            _reactor_mods = _reactor_artifacts(project_path)
            _proj_group = _project_group(project_path)
        if artifact in _reactor_mods and _proj_group and group != _proj_group:
            art_esc = artifact.replace(".", r"\.")
            _ec, _gout, _ge = _run_check_split(
                f"grep -rl '<artifactId>{art_esc}</artifactId>' --include=pom.xml . 2>/dev/null",
                project_path, timeout=30)
            _fixed: list[str] = []
            for _pom in sorted({ln.strip() for ln in (_gout or "").splitlines() if ln.strip()}):
                _text = _read_project_file(project_path, _pom, timeout=20)
                if _text is None:
                    continue
                _new = _fix_reactor_dep_group(_text, artifact, _proj_group, _reactor_mods)
                if _new is not None and _write_project_file(project_path, _pom, _new, timeout=20):
                    changed.add(_pom)
                    _fixed.append(_pom)
            logger.warning(
                "[L1.2.1·reactor-group] R54-6 %s 是 reactor 内部模块，依赖却写成外部 groupId %r "
                "→ 改回工程 groupId %r（%d pom）；仓库里本就没有它，校正版本无从谈起",
                artifact, group, _proj_group, len(_fixed))
            continue
        # R56-4（round56 实锤）：**有 version 的幻影坐标**——从所有既有闸门的缝里钻过去：
        #   · R53-2 只剪【无 version】的；这类有 version
        #   · R54-6 只改【groupId 编错】的；`com.ruoyi:ruoyi-alarm-system` 的 groupId 是**对的**
        #   · version-repair 查不到任何可用版本 → 静默跳过（"交其它防线"，但没有其它防线）
        # 实测两种形态，都是【可证永不可解析】：
        #   ① `com.ruoyi:ruoyi-alarm-system:4.8.3` —— 用工程自己的 groupId，但它**不是 reactor 模块**
        #      （本轮压根没这个模块）→ 工程模块从来不在远程仓库里，此坐标永远拉不到；
        #   ② `com.github.aerogear:aerogear-otp-java:1.1.0` —— 仓库里**查无任何版本**（artifact 本身不存在）。
        # 留着它 → `Could not resolve dependencies` → 整个模块解析失败、连坐下游。剪除 + 响亮日志：
        # 缺依赖是可归因的编译错，幻影坐标是模块级的解析崩塌。
        _is_reactor = artifact in (_reactor_mods or set())
        # ★R57-3（round57 near-miss 实锤）★ reactor 成员**永远不许**据"仓库查无"剪除——
        # 工程模块本来就不在远程仓库里，查无是**正常**的，不是罪证。实测 `com.ruoyi:ruoyi`
        # （工程根模块自己）走到了第三方分支、被判"仓库确证查无 → 确定性剪除"，只因当时
        # 恰好没有 pom 声明它（0 pom）才没酿成删除合法依赖。同一条不变量在 dep_legality
        # 的规则①里挡住了，这条老分支却漏了——**打地鼠遗产：一个不变量两处实现，只有一处对。**
        if _is_reactor:
            continue   # 它的版本由 reactor 承接（真缺 version 由上面的 R53-2 分支注入）
        _phantom_internal = (_proj_group and group == _proj_group and not _is_reactor)
        # 幻影内部模块无需查仓库（工程模块从不在远程仓库里）；其余去查——但 R56-6 铁律：
        # 只有【仓库确证查无】(reachable=True 且空) 才敢剪；【仓库没连上】绝不剪（证据缺失≠否定证据，
        # 否则沙箱一断网就把全工程合法依赖剪光）。
        if _phantom_internal:
            available, _reachable = [], True
        else:
            available, _reachable = _fetch_maven_versions_probe(
                group, artifact, project_path, timeout)
        if _phantom_internal or (_reachable and not available):
            _why = ("用工程 groupId 但非 reactor 模块（工程模块从不在远程仓库）"
                    if _phantom_internal else "仓库确证查无该 artifact 的任何版本")
            art_esc = artifact.replace(".", r"\.")
            _gc, _gout, _ge = _run_check_split(
                f"grep -rl '<artifactId>{art_esc}</artifactId>' --include=pom.xml . 2>/dev/null",
                project_path, timeout=30)
            _cut2: list[str] = []
            for _pom in sorted({ln.strip() for ln in (_gout or "").splitlines() if ln.strip()}):
                _text = _read_project_file(project_path, _pom, timeout=20)
                if _text is None:
                    continue
                _new = _prune_dep_blocks(_text, group, artifact, even_with_version=True)
                if _new is not None and _write_project_file(project_path, _pom, _new, timeout=20):
                    changed.add(_pom)
                    _cut2.append(_pom)
            logger.warning(
                "[L1.2.1·phantom-dep] R56-4 %s:%s 永不可解析（%s）→ 确定性剪除（%d pom）；"
                "留着它整个模块都解析不了（Could not resolve → 连坐下游）",
                group, artifact, _why, len(_cut2))
            continue
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
            pick_latest_stable(available) or "?", good_ver, len(poms),
            sorted(prop_names) or "-",
        )
    # ── ② 缺 <version> 元素 → 注入有效版本 ──
    _reactor: set[str] | None = None
    for group, artifact in missing_versions[:8]:
        # R56-6：这里的分支会**剪除依赖**（不可逆）→ 必须区分「仓库确证查无」与「仓库没连上」。
        # 用旧口（空列表兼表两义）等于断网即误剪全工程合法依赖。
        available, _reach = _fetch_maven_versions_probe(group, artifact, project_path, timeout)
        if _reactor is None:
            _reactor = _reactor_artifacts(project_path)
        # 真 reactor 兄弟模块靠**本地证据**判定（不需要仓库）→ 断网也照常注 ${project.version}；
        # 只有"要据仓库空结果去剪除"的路径才受不可达影响 → fail-open 跳过。
        if not available and not _reach and artifact not in _reactor:
            logger.warning("[L1.2.1·phantom-dep] %s:%s 仓库不可达（非确证查无）→ 本轮不处置"
                           "（fail-open：宁可漏判，绝不误剪合法依赖）", group, artifact)
            continue
        if not available:
            # R53-2 治本（round53 实锤死因）：旧实现在这里静默 continue，注释写"交其它防线"
            # ——**根本没有其它防线**。仓库查无此 artifact 且父级不管、又无 version →
            # `'dependencies.dependency.version' for G:A:jar is missing` 是 **pom 解析期**错：
            # Maven 连 reactor 都读不出 → 此后每个 worker 的构建闸都判"错在上游模块"BLOCKED
            # → 编译验证全线失效 → 整任务陪跑到熔断（round53 实测：契约臆造的幻影模块
            # alarm-interface，两个 worker 各编一个 groupId 写进 pom，全树 8/80 后判死）。
            # 治法与根 pom 幻影 <module> 剪枝对称：
            #   · 真 reactor 兄弟模块（父级漏管）→ 注 ${project.version}（与父同版，确定性）
            #   · 仓库查无此物 + 非 reactor 模块 = **幻影坐标，永不可解析** → 确定性剪除 + 响亮日志
            # 剪掉后若代码真需要它 → 报 cannot-find-symbol（可归因、可修的局部编译错），
            # 远优于让整棵树读不出（全局连坐）。
            if _reactor is None:
                _reactor = _reactor_artifacts(project_path)
            _is_module = artifact in _reactor
            art_esc = artifact.replace(".", r"\.")
            _gc, _gout, _ge = _run_check_split(
                f"grep -rl '<artifactId>{art_esc}</artifactId>' --include=pom.xml . 2>/dev/null",
                project_path, timeout=30)
            _poms = sorted({ln.strip() for ln in (_gout or "").splitlines() if ln.strip()})
            _touched: list[str] = []
            for _pom in _poms:
                _text = _read_project_file(project_path, _pom, timeout=20)
                if _text is None:
                    continue
                _new = (_inject_dep_version_in_blocks(_text, group, artifact, "${project.version}")
                        if _is_module else _prune_dep_blocks(_text, group, artifact))
                if _new is not None and _write_project_file(project_path, _pom, _new, timeout=20):
                    changed.add(_pom)
                    _touched.append(_pom)
            if _is_module:
                logger.warning(
                    "[L1.2.1·version-repair] %s:%s 是 reactor 内部模块但父级未受管且无 version"
                    "（pom 解析期硬错）→ 注入 ${project.version}（%d pom）",
                    group, artifact, len(_touched))
            else:
                logger.warning(
                    "[L1.2.1·phantom-dep] R53-2 %s:%s 仓库查无此 artifact 且非 reactor 模块、"
                    "又无 <version> → **幻影坐标，永不可解析**，确定性剪除（%d pom）。"
                    "留着它整个 reactor 都读不出（全员 BLOCKED）；剪掉后若代码真需要 → "
                    "报可归因的 cannot-find-symbol",
                    group, artifact, len(_touched))
            continue
        # R54-5：先与【工程同 groupId 家族已钉的版本】对齐——"稳定版"只挡预发布，挡不住
        # "版本对、代际错"（round54：Boot 4.0.6 工程被注进 Boot 3 系的 spring-boot-starter-aop:3.5.16）。
        _fam = _group_family_version(project_path, group, artifact)
        if _fam:
            if _fam in available:
                good_ver = _fam                      # 与工程同代 → 唯一正确的对齐目标
            else:
                # 工程用的是该 group 的 X 代，而这个 artifact 在 X 代**不存在**（典型：Boot 4 删掉
                # 了 starter-aop）→ 注入任何"可用版本"都是跨代混用（更隐蔽的毒）。如实剪除：
                # 缺依赖 = 可归因的编译错，跨代依赖 = 运行期/集成期才炸的暗雷。
                art_esc = artifact.replace(".", r"\.")
                _gc, _gout, _ge = _run_check_split(
                    f"grep -rl '<artifactId>{art_esc}</artifactId>' --include=pom.xml . 2>/dev/null",
                    project_path, timeout=30)
                _cut: list[str] = []
                for _pom in sorted({ln.strip() for ln in (_gout or "").splitlines() if ln.strip()}):
                    _text = _read_project_file(project_path, _pom, timeout=20)
                    if _text is None:
                        continue
                    _new = _prune_dep_blocks(_text, group, artifact)
                    if _new is not None and _write_project_file(project_path, _pom, _new, timeout=20):
                        changed.add(_pom)
                        _cut.append(_pom)
                logger.warning(
                    "[L1.2.1·generation-mismatch] R54-5 工程的 %s 家族钉在 %s，但 %s 在该代不存在"
                    "（仓库可用最高稳定版=%s，属另一代）→ 剪除该依赖（跨代混用是集成期才炸的暗雷；"
                    "缺依赖是可归因的编译错）（%d pom）",
                    group, _fam, artifact, pick_latest_stable(available) or "?", len(_cut))
                continue
        else:
            good_ver = pick_latest_stable(available)   # R53-3：稳定版优先（禁 M#/RC/alpha 毒树）
        if not good_ver:
            continue
        art_esc = artifact.replace(".", r"\.")
        gcmd = (
            f"grep -rl '<artifactId>{art_esc}</artifactId>' --include=pom.xml . 2>/dev/null"
        )
        _ec, gout, _err = _run_check_split(gcmd, project_path, timeout=30)
        poms = sorted({line.strip() for line in (gout or "").splitlines() if line.strip()})
        for pom in poms:
            # 只在【无 dependencyManagement 的模块 pom】注入：父 pom 的受管块本就带版本，
            # 误插会造双 version。
            _gc, gmgmt, _ge = _run_check_split(
                f"grep -c '<dependencyManagement>' {shlex.quote(pom)}", project_path, timeout=10
            )
            if (gmgmt or "").strip() not in ("", "0"):
                continue
            # R47-3 治本：旧 perl 盲插会命中【项目自身 artifactId 行】（grep -rl 把
            # "工程叫这个名"的 pom 也算声明者），在工程 <version> 旁再插一个 →
            # Duplicated tag: 'version'，整 pom Non-parseable 毒化 reactor；且不查
            # 依赖块内是否已有 version，不幂等。改为 Python 侧块级精准注入：只在
            # <dependency> 块内、artifactId 匹配、（有 groupId 时）groupId 匹配、
            # 且块内无 <version> 时插入——工程/parent 声明在块外天然不碰，天然幂等。
            text = _read_project_file(project_path, pom, timeout=20)
            if text is None:
                continue
            new_text = _inject_dep_version_in_blocks(text, group, artifact, good_ver)
            if new_text is not None and _write_project_file(
                    project_path, pom, new_text, timeout=20):
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
            version = pick_latest_stable(available)   # R53-3：稳定版优先
            if not version:
                continue
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


# R53-5（round50b/52 实锤）：编译器已经告诉我们缺失符号的**角色**（method/class/variable），
# 旧正则却用【非捕获组】把它丢掉 → 类型名与变量名混在同一个候选池里按编辑距离改写 →
# `IAlarmBotService→alarmBotService`（距=2）、`AlarmBot→alarmBot`（距=1）、`super→user`、
# `Constants→constant` —— 确定性修复**主动把代码改坏**，随后编译报 `cannot find symbol:
# class alarmBotService`，子任务被判死、连坐放弃 63 个。现在把角色捕获出来并强制同角色改写。
_SYMBOL_ERR_RE = re.compile(
    r"([A-Za-z0-9_./\-]+\.(?:java|kt|scala)):\[\d+,\d+\][^\n]*cannot find symbol[^\n]*\n"
    r"[^\n]*symbol:\s*(method|class|variable)\s+([A-Za-z_][A-Za-z0-9_]*)"
)

# 语言关键字绝不是"拼错的项目符号"：`cannot find symbol: variable super` 是用法错，
# 把 super 改成 user（距=2 频=425，round52 实锤）只会把代码改得更坏。fail-closed 跳过。
_JVM_KEYWORDS = frozenset("""
abstract assert boolean break byte case catch char class const continue default do double else
enum extends final finally float for goto if implements import instanceof int interface long
native new package private protected public return short static strictfp super switch
synchronized this throw throws transient try void volatile while var record sealed permits
true false null object fun val suspend companion
""".split())


# JVM 生态命名惯例（栈内事实标准，非项目写死）：类型=大驼峰，方法/变量=小驼峰。
# 角色不同 = 语义不同实体，编辑距离再近也绝不可互改。
def _same_role(kind: str, name: str, cand: str) -> bool:
    """候选与缺失符号必须**同角色**才允许改写（类型只能改成类型）。"""
    if not name or not cand:
        return False
    if kind == "class":
        return cand[:1].isupper()          # 类型 → 只接受类型形态候选
    return not cand[:1].isupper() or name[:1].isupper()  # 方法/变量 → 不许被抬成类型形态


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
    for fpath, kind, name in errs:
        rel = _norm_src_path(fpath)
        if mods and rel not in mods and not any(rel.endswith(m) or m.endswith(rel) for m in mods):
            continue  # 别人的文件，不动
        if (rel, name) in seen:
            continue
        seen.add((rel, name))
        if name in _JVM_KEYWORDS:
            logger.info("[L1.2.1·symbol-repair] R53-5 %r 是语言关键字（用法错，非拼写错）→ 不改写", name)
            continue
        cands = [(w, _edit_distance(name, w)) for w in freq
                 if w != name and freq[w] >= 5 and abs(len(w) - len(name)) <= 2
                 and w not in _JVM_KEYWORDS
                 and _same_role(kind, name, w)]   # R53-5：绝不跨角色改写（类型≠变量）
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
        # R46-1 治本：本地共享树的聚合清单可能已注册【并行兄弟子任务】拉回的模块，而这些
        # 模块目录在【本沙箱】不存在——原样推进会让 reactor "Child module does not exist"
        # 硬错，构建根本跑不起来 → det=None → verification_not_run 判死好产出。推送前剪枝，
        # 三重保守闸（对抗复核 F1/F3 整改）：
        #   ① 基线闸：只有【相对 git HEAD 基线新增】的成员才有剪枝资格——bootstrap 是 scope
        #     稀疏上传（非全树），基线成员在沙箱缺席是上传策略使然，剪掉会经 repaired_file_paths
        #     的 push→pull-back 回路把反注册扩散进权威树与交付 diff（F1 最高危）。基线读不到
        #     （非 git 等）→ 该清单整体不剪。
        #   ② 双态探针：沙箱逐项回显 OK/NO；行缺失=未知=保留（F3：单态 [ -e ]&&echo 无法区分
        #     「测过不存在」与「输出丢行」，fail-open 契约会被击穿）。
        #   ③ 剪枝副本从临时镜像目录同步（用后即删，F6），绝不回写本地共享树。
        src_root = project_path
        _mirror: str | None = None
        try:
            from swarm.worker.workspace_manifest import (
                manifest_member_probes, prune_manifest_members,
            )
            import subprocess as _sp
            probe_map: dict[str, list[tuple[str, str]]] = {}
            baseline_members: dict[str, set] = {}
            all_probes: list[str] = []
            for rel in rels:
                text = (_P(project_path) / rel).read_text("utf-8", errors="ignore")
                pairs = manifest_member_probes(rel, text)
                if not pairs:
                    continue
                # ① 基线成员集：git HEAD 里同清单的成员 token（读不到 → None 哨兵=整体不剪）
                try:
                    _git = _sp.run(
                        ["git", "-C", project_path, "show", f"HEAD:{rel}"],
                        capture_output=True, text=True, timeout=10)
                    if _git.returncode == 0:
                        baseline_members[rel] = {
                            t for t, _ in manifest_member_probes(rel, _git.stdout)}
                    else:
                        continue  # 基线不可知 → 本清单不剪（fail-open）
                except Exception:  # noqa: BLE001
                    continue
                base = rel.rsplit("/", 1)[0] + "/" if "/" in rel else ""
                probe_map[rel] = pairs
                # 只探测有剪枝资格（非基线）的成员，省探针
                all_probes.extend(
                    base + p for t, p in pairs if t not in baseline_members[rel])
            if all_probes:
                import shlex as _shlex
                _q = " ".join(_shlex.quote(p) for p in sorted(set(all_probes)))
                cr = manager.run_command(
                    sandbox,
                    f'cd {remote} && for p in {_q}; do if [ -e "$p" ]; then echo "OK $p"; '
                    f'else echo "NO $p"; fi; done; true',
                    timeout=30,
                )
                if getattr(cr, "success", False):
                    _state: dict[str, bool] = {}
                    for ln in (cr.stdout or "").splitlines():
                        ln = ln.strip()
                        if ln.startswith("OK "):
                            _state[ln[3:]] = True
                        elif ln.startswith("NO "):
                            _state[ln[3:]] = False
                    import tempfile as _tmp
                    for rel, pairs in probe_map.items():
                        text = (_P(project_path) / rel).read_text("utf-8", errors="ignore")
                        base = rel.rsplit("/", 1)[0] + "/" if "/" in rel else ""
                        _bl = baseline_members[rel]
                        _tok_probe = {p: t for t, p in pairs}

                        def _exists(p, _b=base, _bl=_bl, _tp=_tok_probe):
                            if _tp.get(p) in _bl:
                                return True  # ① 基线成员恒保留
                            return _state.get(_b + p)  # ② 双态；缺行=None=保留

                        new_text, removed = prune_manifest_members(rel, text, _exists)
                        if not removed:
                            continue
                        if _mirror is None:
                            _mirror = _tmp.mkdtemp(prefix="swarm-manifest-prune-")
                            # 未剪枝的清单也镜像原文，保证单一 src_root 一次同步
                            for r2 in rels:
                                dst0 = _P(_mirror) / r2
                                dst0.parent.mkdir(parents=True, exist_ok=True)
                                dst0.write_text(
                                    (_P(project_path) / r2).read_text("utf-8", errors="ignore"),
                                    encoding="utf-8")
                        (_P(_mirror) / rel).write_text(new_text, encoding="utf-8")
                        logger.info(
                            "[L1.2.1·module-reg] 推送前按沙箱 ground truth 剪枝 %s：摘除"
                            "【基线外且沙箱不存在】的成员 %s（防 reactor missing-child "
                            "硬错误判死本子任务；基线成员恒保留）", rel, removed)
                    if _mirror is not None:
                        src_root = _mirror
        except Exception as _pexc:  # noqa: BLE001 — 剪枝失败回退原样推送（旧行为）
            logger.warning("[L1.2.1·module-reg] 沙箱剪枝跳过(不致命,原样推送): %s", _pexc)
            src_root = project_path
        try:
            stats = manager.sync_files_to_sandbox(sandbox, src_root, rels, remote)
        finally:
            if _mirror is not None:
                import shutil as _sh
                _sh.rmtree(_mirror, ignore_errors=True)
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


def _prune_manifest_cache_negatives() -> None:
    """C11（阶段4，登记册 §四）：run 入口只清【负缓存】——presence=True 在沙箱生命周期
    内不会自发失效（manifest 不会被删；key 已含 sandbox_id，换沙箱天然隔离），跨 run
    复用省每 run 5-8 趟沙箱 find；False 可能因脚手架/补注册在 run 间落盘而过期，逐 run
    重探（D57 的防负缓存 stale 语义原样保留）。"""
    global _MANIFEST_PRESENT_CACHE
    # 4.9 复核 T10：顺带丢弃非当前 GEN 的键——中途 invalidate 后旧 GEN 正项永不命中
    # （key 含 GEN），保留=纯泄漏。
    _MANIFEST_PRESENT_CACHE = {
        k: v for k, v in _MANIFEST_PRESENT_CACHE.items()
        if v and k[0] == _MANIFEST_CACHE_GEN}


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

    # E3（round38c 主题E，register #31）：非编译数据文件确定性语法校验。此前只产
    # .md/.sql/.yml/.properties/.html 的子任务除 L1.1 scope 检查外零确定性面（本函数
    # fall-through 恒 True）＝结构性假绿通道。v1 补 json/yaml/xml 三类纯 parse 校验
    # （stdlib/PyYAML，栈无关零外部工具）；.sql/.properties/.html 无普适确定性 parser，
    # 诚实登记为边界（靠 L2/验收面兜）。文件不在本地/解析器缺失按 infra 口径跳过。
    _data_ok, _data_msg = _validate_data_files(project_path, files)
    if not _data_ok:
        return False, _data_msg

    return True, "compile ok"


def _validate_downgrade_unverified_sources(build_cmd: str, modified: list) -> list[str]:
    """D3c：命中「脚手架 validate 降级」形态（mvn -f <mod>/pom.xml … validate）且
    modified 含 JVM 源码 → 返回本轮未经编译的源码清单（空=无降级/无源码）。"""
    if not ("validate" in (build_cmd or "") and " -f " in f" {build_cmd} "):
        return []
    return [str(f) for f in (modified or [])
            if str(f).endswith((".java", ".kt", ".scala"))]


_PKG_DECL_RE = re.compile(r"^\+\s*package\s+([A-Za-z_][\w.]*)\s*;")


def _package_decl_mismatches(diff: str) -> list[dict]:
    """E6①：diff 内【新建 .java】的包声明与 src/main|test/java 路径反推包比对。

    返回不符清单 [{file, declared, expected}]。路径不含 java 源根标记（file_path_to_fqn
    返回 None）或抽不到声明行 → 跳过（保守，不误杀非常规布局）。纯文本零外部工具。"""
    out: list[dict] = []
    try:
        from swarm.project.diff_apply import split_diff_by_file
        from swarm.worker.symbol_resolver import file_path_to_fqn
        for files, text in split_diff_by_file(diff or ""):
            if "--- /dev/null" not in text and "new file mode" not in text:
                continue
            for f in files:
                if not f.endswith(".java"):
                    continue
                fqn = file_path_to_fqn(f)
                if not fqn or "." not in fqn:
                    continue
                expected = fqn.rsplit(".", 1)[0]
                declared = None
                for ln in text.splitlines():
                    m = _PKG_DECL_RE.match(ln)
                    if m:
                        declared = m.group(1)
                        break
                if declared and declared != expected:
                    out.append({"file": f, "declared": declared, "expected": expected})
    except Exception as exc:  # noqa: BLE001 — 对账是增强闸，异常不阻断 L1 主链
        logger.debug("[L1.1b] 包声明对账异常(跳过): %s", exc)
    return out


def _validate_data_files(project_path: str, files: list[str]) -> tuple[bool, str]:
    """json/yaml/xml 语法确定性校验（E3）。失败返回 (False, 归因文本)。"""
    import json as _json
    from pathlib import Path as _P
    for f in files:
        lf = _P(project_path) / f
        if not lf.is_file():
            continue  # 沙箱模式未 pull-back 等 → 跳过（非能力失败口径）
        try:
            text = lf.read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            if f.endswith(".json"):
                # 复核 C-1 次级面：JSONC 家族（tsconfig*/jsconfig/.eslintrc.json/.jsonc）
                # 合法含注释与尾逗号，json.loads 必炸——已知家族豁免（保守不误杀）
                _base = f.rsplit("/", 1)[-1]
                if (_base.startswith(("tsconfig", "jsconfig"))
                        or _base == ".eslintrc.json" or f.endswith(".jsonc")):
                    continue
                _json.loads(text or "null")
            elif f.endswith((".yml", ".yaml")):
                try:
                    import yaml as _yaml
                except ImportError:
                    continue  # 解析器缺失=infra，跳过闸门（loud 由上层日志承担）
                # 复核 C-1（CONFIRMED）：Spring Boot application.yml 的 `---` 多文档
                # profile 是标准写法，safe_load 单文档必炸=确定性误杀、fix 循环会教
                # 模型删掉合法 `---` 过闸——必须 safe_load_all
                list(_yaml.safe_load_all(text))
            elif f.endswith(".xml"):
                import xml.etree.ElementTree as _ET
                _ET.fromstring(text.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — parse 失败即语法坏
            return False, f"数据文件语法校验失败 {f}: {type(exc).__name__}: {str(exc)[:300]}"
    return True, "data files ok"


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
            # 4.9 复核 T7：oversize 拆小信号（_is_timeout_oversize_failure 消费）在
            # 产生处落 marker——不靠 Phase-4 A5 布尔快照的偶然对齐续命。
            details.setdefault("error", "timeout_in_verifying")
            logger.warning("[L1] worker 预算耗尽（阶段=%s）→ BLOCKED，不再白跑", stage)
            return True
        return False

    if _deadline_blocked("entry"):
        return True, details

    # D57+C11：新一次 L1 run 只清负缓存（True 在同沙箱生命周期内恒真，跨 run 复用；
    # False 可能过期逐 run 重探）——同沙箱多 run 不再每次重付 5-8 趟沙箱 find。
    _prune_manifest_cache_negatives()

    # ── L1.1 scope 检查 ──
    violations = _scope_violations(diff, subtask.scope, extra_allowed=extra_writable_paths)
    details["l1_1_scope_ok"] = not violations
    details["scope_violations"] = violations
    if violations:
        return False, details

    # ── L1.1b 包声明↔目录对账（E6①，round38c 主题E）──
    # 新建 .java 的 `package X;` 与 src/main/java 路径反推包不符时，maven-compiler
    # 不报错（class 落错包），毒发在下游子任务 import 时（producer-gate 不对称——
    # 既有机制全在 import 消费侧修复）。确定性闸：不符即 fail，worker 当轮改对。
    _pkg_mis = _package_decl_mismatches(diff)
    details["l1_1b_package_decl_ok"] = not _pkg_mis
    if _pkg_mis:
        details["package_decl_mismatches"] = _pkg_mis
        # 复核 C-3（CONFIRMED）：必须设 reason——_l1_failure_digest 经 `[确定性闸门]
        # {reason}: {note}` 出口把证据带进重试 prompt，且 reason 在 _failure_signature
        # 键集内（no-progress 早停可触发）；只写 note 则 worker 全盲+签名恒空=盲烧
        # 满 fix 轮再被 brain 重派确定性复死。
        details["reason"] = "package_decl_mismatch"
        details["note"] = "; ".join(
            f"{m['file']}: 声明 package {m['declared']} ≠ 路径推定 {m['expected']}"
            for m in _pkg_mis[:5])
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
    # 4.9 复核 T6（CONFIRMED）：compile/lint 段此前无查点——deadline 在 entry 后过期
    # 仍可越线跑 5-10 分钟（整树 lint 240s+/逐文件 30s×20）。
    if _deadline_blocked("compile"):
        return True, details
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
            # F4：L1 在【活动共享树】上只补漏不摘幽灵——owner 先行登记(contract_utils 规则4)
            # 的模块目录物化在后，此时 prune 会误摘；幽灵清理留给 L2/交付两处定格树。
            _wm = reconcile_workspace_manifests(project_path, modified, prune=False)
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
        # R50-3（r49b/r50/r50b 三轮脚手架连败真因）：-pl 推导只用【本子任务真实
        # 产出】。repair 通道（D2 版本对账/module-reg/依赖注入）触达的外模块清单混进
        # modified 会把外模块拖进 -pl → 脚手架被别人模块的在飞坏代码连坐判死（"构建
        # 错全在上游模块"豁免只对 -pl 外模块生效，被拖进 -pl 即失效）。repaired 文件
        # 照常推送沙箱/回传本地，只是不参与 -pl 圈定。全被过滤（纯 repair 轮）退回原集。
        _rfp_set = {str(x).lstrip("./").lstrip("/")
                    for x in (details.get("repaired_file_paths") or [])}
        _pl_basis = [f for f in modified
                     if str(f).lstrip("./").lstrip("/") not in _rfp_set] or modified
        build_cmd = _scope_maven_command(build_cmd, project_path, _pl_basis)
        # D3c（round38c 主题D 分流）：脚手架窗口 validate 降级【可见性】——validate 不编译
        # 源码，scaffold 子任务同批新建 .java 时这些源码零编译即 l1_passed=True。降级本身
        # 是 R34-6/Death B 的故意治法（脚手架契约=模块良构可注册；真编译由 L2 reactor
        # compile 兜，D1 注册合成后必含该模块），但此前无任何机读痕迹——补标记供
        # evaluate/L2/复盘消费，杜绝「validate PASS」被读作「编译 PASS」。
        _src_unverified = _validate_downgrade_unverified_sources(build_cmd, modified)
        if _src_unverified:
            details["build_cmd_downgraded_to_validate"] = True
            details["validate_unverified_sources"] = _src_unverified[:20]
            logger.warning(
                "[L1.2.1] D3c 脚手架 validate 降级：%d 个源码文件本轮未经编译"
                "（真编译由注册后的 L2 reactor compile 兜）: %s",
                len(_src_unverified), _src_unverified[:5])
    if build_cmd and _build_cmd_applicable(build_cmd, project_path):
        if _deadline_blocked("build"):
            return True, details
        # R56-5：构建**之前**先过依赖合法性闸——坏坐标在进 Maven 前就被消掉（state-driven），
        # 而不是等它炸出 `Could not resolve` 再按错误文本逐形态打补丁（error-driven=打地鼠）。
        if str(build_cmd).lstrip().startswith("mvn"):
            try:
                # R58-2：parent 版本必须是字面量——它比依赖合法性更早、更致命（parent 解析不了
                # 连 pom 都读不出，谈不上依赖）。故排在合法性闸**之前**。
                _pv_n, _pv_files = _enforce_parent_version_literals(project_path, timeout)
                if _pv_files:
                    _rfp = details.setdefault("repaired_file_paths", [])
                    for _f in _pv_files:
                        if _f not in _rfp:
                            _rfp.append(_f)
                _dl_n, _dl_files = _enforce_dep_legality(project_path, timeout)
                if _dl_files:
                    _rfp = details.setdefault("repaired_file_paths", [])
                    for _f in _dl_files:
                        if _f not in _rfp:
                            _rfp.append(_f)   # 随 pull-back 回传本地，否则修复只活在沙箱
            except Exception as _dl_exc:  # noqa: BLE001 —— 闸门自身绝不阻断构建
                logger.warning("[L1.2.1·dep-legality] 合法性闸异常（跳过，不阻断）: %s", _dl_exc)
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
        # R50-3 同源：test 的 -pl 圈定同样只用真实产出
        _rfp_t = {str(x).lstrip("./").lstrip("/")
                  for x in (details.get("repaired_file_paths") or [])}
        _pl_t = [f for f in modified
                 if str(f).lstrip("./").lstrip("/") not in _rfp_t] or modified
        test_cmd = _scope_maven_command(test_cmd, project_path, _pl_t)
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
        t_ec, t_out = _run_l1_command(test_cmd, project_path, timeout=_stage_timeout(timeout, deadline))
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

    # C4（阶段6，登记册 §五）：非空 diff 但既无 test_command 也无 verify_commands 的
    # 子任务——确定性验证面只剩编译（test skip 判过），语义正确性零覆盖。打 needs_review
    # 标记（deliver/人工闸可见；阻断语义由 det+llm conflict 分支承担——Phase-4 自检判
    # False 时 evaluate_l1 已 fail，见 deterministic_llm_conflict）。
    if modified and not (getattr(harness, "test_command", "") if harness else "") \
            and not (list(getattr(harness, "verify_commands", []) or []) if harness else []):
        details["needs_review"] = "no_test_or_verify_commands"

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
