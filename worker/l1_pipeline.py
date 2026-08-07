"""Worker L1 四级验证 — 确定性 scope / compile / lint / scoped test / LLM 自检。"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swarm.project.diff_apply import files_from_unified_diff
from swarm.types import FileScope, NotRunKind, SubTask
from swarm.worker.cmd_normalize import normalize_python_cmd
from swarm.worker.output_compress import compress_tool_output, extract_error_lines

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
    parse_missing_classified_artifacts,
    parse_missing_packages,
    parse_missing_symbol_classes,
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
        body = out or ""
        m = re.search(r"__HTTP__(\d{3})\s*$", body)
        code = m.group(1) if m else ""
        # A1（worker 审计）：必须【先】解析 __HTTP__ 状态码——404 响应体常含 "Not Found"
        # 文本（repo1 历史形态），先咨询 _tool_missing 会把「仓库确证 404」吞成「工具缺失」
        # → continue → reachable 永置不了 True → 确证剪除防线（R53-2/R56-4）该腿被静默旁路。
        # 仅在【无状态码标记】（wget 兜底/命令层报错形态）时才咨询 _tool_missing。
        if not code and _tool_missing(out):
            continue
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


# R63：version-repair 两分支共用的【代际对齐/剪除】纯判据（单一权威）。
# round63 死因是"一个不变量两处实现，只有一处对"（round57-3 教训重演）：分支②「缺 version→注入」
# 早有 _group_family_version 代际守卫（R54-5），分支①「版本不存在→校正」没有 → 为满足跨代的
# spring-boot-starter-aop 把**共享** ${spring-boot.version} 4.0.6→3.5.16、整 reactor 降代。
# 抽成此纯函数后两分支共用，杜绝再次漂移。栈中立：只谈"工程为某家族钉的版本 vs 仓库可用版本"。
_PRUNE_DEP = object()   # 哨兵：该依赖属跨代混用，应剪除而非降级（绝不改写共享锚属性）


def _family_generation_choice(fam: str | None, available: list[str]):
    """依赖版本应对齐到工程家族代际，还是因跨代而剪除，还是交调用方按默认处理。

    返回：
      · 版本字符串 —— 工程家族钉在 fam 且 fam 在仓库可用 → 对齐到 fam（唯一正确目标）。
      · _PRUNE_DEP —— 工程家族钉在 fam，但该 artifact 在该代不存在（仓库最高属另一代）→
        跨代混用是集成期才炸的暗雷；如实剪除依赖（缺依赖=可归因的编译错），**绝不降级共享锚属性**。
      · None       —— 工程无该家族先例 → 交调用方走各自默认（分支①最近有效版 / 分支②最新稳定版）。
    """
    if not fam:
        return None
    return fam if fam in available else _PRUNE_DEP


_DEP_BLOCK_SCAN_RE = re.compile(r"<dependency>(.*?)</dependency>", re.S)


def _dep_consumers_of_property(pom_texts: list[str], prop: str) -> set[str]:
    """扫描 pom 文本集，收集【版本写作 ${prop} 的 <dependency> 块】的 artifactId 集合（纯函数）。

    R63：判定某 <properties> 版本条目是否被【多个依赖】共享。被 ≥2 个依赖共享 = 平台/BOM 版本
    锚（如 ${spring-boot.version}），version-repair 绝不能为满足单个依赖的版本诉求去降级它——
    那会连坐整棵 reactor 的代际（round63 死因）。只专属于单个依赖的私有属性才允许校正。
    栈中立：npm/Gradle/Cargo 的共享版本变量同理，判据都是"是否被多方引用"。
    """
    ref = "${" + prop + "}"
    arts: set[str] = set()
    for txt in pom_texts:
        for blk in _DEP_BLOCK_SCAN_RE.finditer(txt or ""):
            inner = re.sub(r"<exclusions>.*?</exclusions>", "", blk.group(1), flags=re.S)
            vm = re.search(r"<version>\s*([^<]+?)\s*</version>", inner)
            if not vm or vm.group(1).strip() != ref:
                continue
            am = re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", inner)
            if am:
                arts.add(am.group(1))
    return arts


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


def _strip_dep_classifier(text: str, group: str, artifact: str,
                          classifier: str) -> str | None:
    """R65E8-T3：剔除匹配坐标 <dependency> 块内【值恰为该幻觉 classifier】的 <classifier> 标签
    → 新文本或 None（未命中）。与 _prune_dep_blocks 对称的保守块级编辑。

    命中判据（三者皆须满足，缺一即整块保留）：artifactId 必配；有 groupId 且明确不符（非 ${…}）→ 跳过；
    块内（剥 <exclusions> 防撞名后）确有 `<classifier>{classifier}</classifier>`。只剔恰为该值的标签——
    别的合法 classifier（native/sources/linux-x86_64…）绝不误碰。version 等其余标签原样保留。
    模块级函数：测试 import 真身（禁抄本测试假绿）。"""
    hits = 0
    cls_re = re.compile(
        r"[ \t]*<classifier>\s*" + re.escape(classifier) + r"\s*</classifier>[ \t]*\n?")

    def _edit(m: "re.Match[str]") -> str:
        nonlocal hits
        blk = m.group(0)
        inner = re.sub(r"<exclusions>.*?</exclusions>", "", m.group(1), flags=re.S)
        if not re.search(r"<artifactId>\s*" + re.escape(artifact) + r"\s*</artifactId>", inner):
            return blk
        g = re.search(r"<groupId>\s*([^<\s]+)\s*</groupId>", inner)
        if group and g and not g.group(1).startswith("${") and g.group(1) != group:
            return blk
        if not cls_re.search(inner):   # 目标 classifier 只在 <exclusions> 里/根本没有 → 不动
            return blk
        new_blk = cls_re.sub("", blk, count=1)
        if new_blk != blk:
            hits += 1
        return new_blk

    new_text = re.sub(r"[ \t]*<dependency>(.*?)</dependency>\s*\n?", _edit, text, flags=re.S)
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
    """R56-5：构建**之前**对全树 manifest 施加依赖合法性不变量（state-driven，不看报错文本）。

    收敛 R53-2/R54-5/R54-6/R56-4 四条 error-driven 分支——它们全是"等构建工具报出一种新错法，
    再针对那句错误文本加一条分支"，**换个错法就漏一个**（用户点破：这就是打地鼠）。
    本闸只看 manifest 的**状态**：每条依赖必须满足「工作区成员 / 上游受管 / 仓库真实存在」
    三者之一，否则确定性处置。旧分支保留为兜底（网络抖动/边角），但问题在进构建前就已被消掉。

    fail-open 铁律：仓库不可达 → 一律放行（宁可漏判，绝不误剪合法依赖）。

    ★X-M10（27 号文 §3.2）★ 治前本函数【写死】`driver_for("maven")` + 只扫 pom.xml——
    npm/go 工程的依赖合法性**零覆盖**，且因为根本没调用 driver_for("npm")/("go")，
    连 D14 的「无 driver warn-once」都不触发 = 零覆盖还不留痕（硬检查④）。
    治法：按【manifest 在场】分派（混合工程多栈各过各的闸），无 driver 的栈显式
    driver_for 一次让 warn-once 响（零覆盖机读可辨）。

    ★W-6★ 扩展 Cargo / Go / Gradle / Python 臂，全部走 `_enforce_dep_legality_generic`
    统一骨架；registry 探针按栈分派，不可达统一 fail-open。
    """
    from swarm.worker.dep_legality import driver_for

    changed: list[str] = []
    if _read_project_file(project_path, "pom.xml", timeout=20):
        _n, _f = _enforce_dep_legality_maven(project_path, timeout)
        changed.extend(_f)
    if _read_project_file(project_path, "package.json", timeout=20):
        _n, _f = _enforce_dep_legality_npm(project_path, timeout)
        changed.extend(_f)
    if _read_project_file(project_path, "Cargo.toml", timeout=20):
        _n, _f = _enforce_dep_legality_cargo(project_path, timeout)
        changed.extend(_f)
    if _read_project_file(project_path, "go.mod", timeout=20):
        _n, _f = _enforce_dep_legality_go(project_path, timeout)
        changed.extend(_f)
    if (_read_project_file(project_path, "build.gradle", timeout=20)
            or _read_project_file(project_path, "build.gradle.kts", timeout=20)):
        _n, _f = _enforce_dep_legality_gradle(project_path, timeout)
        changed.extend(_f)
    if (_read_project_file(project_path, "requirements.txt", timeout=20)
            or _read_project_file(project_path, "pyproject.toml", timeout=20)):
        _n, _f = _enforce_dep_legality_python(project_path, timeout)
        changed.extend(_f)
    return len(set(changed)), sorted(set(changed))


def _enforce_dep_legality_maven(project_path: str, timeout: int) -> tuple[int, list[str]]:
    """Maven 臂（R56-5 原实现，X-M10 拆出为分派的一支）。"""
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


def _fetch_npm_versions_probe(name: str, project_path: str, timeout: int
                              ) -> tuple[list[str], bool]:
    """npm registry 查包 → **(versions, reachable)**，契约同 `_fetch_maven_versions_probe`：
    不可达 → ([], False)（fail-open 绝不剪）；E404 确证查无 → ([], True)（才可剪）。

    ★走 `npm view` 而非另维护一份 registry URL 清单★：沙箱内 npm 用的就是项目真实配置的
    registry（镜像/.npmrc/私服）——「权威仓库」的定义是**构建工具实际访问的那个**，
    硬编码 registry.npmjs.org 会在私有 registry 工程上把合法内部包误判"查无"（误剪）。
    """
    _ec, out = _run_l1_command(
        f"npm view {shlex.quote(name)} versions --json 2>&1", project_path,
        timeout=min(timeout, 60))
    body = out or ""
    # A1 同纪律（maven 探针同款）：必须先判 E404——404 响应体含 "Not Found"，
    # 先咨询 _tool_missing 会把「registry 确证查无」吞成「工具缺失」→ 确证剪除被旁路。
    if "E404" in body or "404 Not Found" in body:
        return [], True    # registry 确证答复"没有它"——可据以剪除的肯定证据
    if _tool_missing(body):
        return [], False
    m = re.search(r"\[.*?\]", body, re.S)
    if m:
        try:
            vers = json.loads(m.group(0))
            if isinstance(vers, list) and vers:
                return [str(v) for v in vers], True
        except (ValueError, TypeError):
            pass
    m2 = re.search(r'"(\d[^"]*)"', body)   # 单版本包：--json 输出裸字符串而非数组
    if m2:
        return [m2.group(1)], True
    return [], False


def _enforce_dep_legality_npm(project_path: str, timeout: int) -> tuple[int, list[str]]:
    """npm 臂（X-M10）：扫全树 package.json（排除 node_modules），施加同一不变量。

    与 maven 臂的消费契约分档（血规 10③，详见 NpmDriver docstring）：
    namespace 恒传 **None**——npm 的 @scope 不是工程命名空间，scoped 包可合法发布；
    幻影依赖一律由 registry 探针的确证 404 定罪，绝不走"非成员即剪"的捷径。
    """
    from swarm.worker.dep_legality import driver_for, enforce

    drv = driver_for("npm")
    if drv is None:
        return 0, []
    root_text = _read_project_file(project_path, "package.json", timeout=20)
    if not root_text:
        return 0, []
    _ec, gout, _e = _run_check_split(
        "find . -name package.json -not -path '*/node_modules/*' 2>/dev/null",
        project_path, timeout=30)
    if _ec != 0:
        logger.warning("[L1.2.1·dep-legality] npm manifest 扫描失败(ec=%s) → 本轮合法性闸未运行: %s",
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
    # 工作区成员 = 每个 package.json 的 name（monorepo/workspaces 的全部内部包）
    members: set[str] = set()
    for t in texts.values():
        _m = re.search(r'"name"\s*:\s*"([^"]+)"', t)
        if _m:
            members.add(_m.group(1))

    _cache: dict[str, list[str] | None] = {}

    def _versions(_ns: str, name: str):
        """契约同 maven 臂：不可达 → None；确证查无 → []。"""
        if name not in _cache:
            try:
                vers, reachable = _fetch_npm_versions_probe(name, project_path, timeout)
                _cache[name] = vers if (vers or reachable) else None
            except Exception as _fx:  # noqa: BLE001
                logger.warning("[L1.2.1·dep-legality] npm 仓库查询异常（按不可达 fail-open）"
                               "%s → %s", name, _fx)
                _cache[name] = None
        return _cache[name]

    new_texts, actions = enforce(
        texts, root_text=root_text, namespace=None,   # 分档①：@scope ≠ 工程命名空间
        workspace_members=members, registry_versions=_versions, driver=drv,
    )
    changed: list[str] = []
    for rel, txt in new_texts.items():
        if _write_project_file(project_path, rel, txt, timeout=20):
            changed.append(rel)
    if actions:
        logger.warning(
            "[L1.2.1·dep-legality] X-M10 构建前依赖合法性闸（npm）：处置 %d 条（%d package.json "
            "改写）——不变量=每条依赖须满足【工作区成员 / 仓库真实存在】之一：\n  %s",
            len(actions), len(changed), "\n  ".join(actions[:12]))
    return len(changed), sorted(changed)


def _enforce_dep_legality_generic(
    project_path: str,
    timeout: int,
    *,
    stack_key: str,
    manifest_name: str,
    find_exclude: str,
    registry_versions,
    namespace: str | None = None,
    workspace_members_from_texts: Any = None,
) -> tuple[int, list[str]]:
    """多栈通用臂（W-6）：Cargo / Go / Gradle / Python 共享同一 enforcement 骨架。

    调用方提供：栈键、清单名、find 排除路径、registry 查询函数、工程命名空间、
    工作区成员提取函数（接收 {rel: text} → set[str]）。其余与 maven/npm 臂同形。
    """
    from swarm.worker.dep_legality import driver_for, enforce

    drv = driver_for(stack_key)
    if drv is None:
        return 0, []
    root_text = _read_project_file(project_path, manifest_name, timeout=20)
    if not root_text:
        return 0, []
    _ec, gout, _e = _run_check_split(
        f"find . -name {manifest_name} -not -path '{find_exclude}' 2>/dev/null",
        project_path, timeout=30)
    if _ec != 0:
        logger.warning("[L1.2.1·dep-legality] %s manifest 扫描失败(ec=%s) → 本轮合法性闸未运行: %s",
                       stack_key, _ec, (_e or "")[:200])
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

    members: set[str] = set()
    if workspace_members_from_texts is not None:
        try:
            members = workspace_members_from_texts(texts)
        except Exception as exc:
            logger.warning("[L1.2.1·dep-legality] %s 工作区成员提取失败: %s", stack_key, exc)

    new_texts, actions = enforce(
        texts, root_text=root_text, namespace=namespace,
        workspace_members=members, registry_versions=registry_versions, driver=drv,
    )
    changed: list[str] = []
    for rel, txt in new_texts.items():
        if _write_project_file(project_path, rel, txt, timeout=20):
            changed.append(rel)
    if actions:
        logger.warning(
            "[L1.2.1·dep-legality] W-6 构建前依赖合法性闸（%s）：处置 %d 条（%d 清单改写）——"
            "不变量=每条依赖须满足【工作区成员 / 上游受管 / 仓库真实存在】之一：\n  %s",
            stack_key, len(actions), len(changed), "\n  ".join(actions[:12]))
    return len(changed), sorted(changed)


def _enforce_dep_legality_cargo(project_path: str, timeout: int) -> tuple[int, list[str]]:
    """Cargo 臂（W-6）。"""
    from swarm.worker.dep_legality import cargo_registry_versions_list

    def _members(texts: dict[str, str]) -> set[str]:
        # workspace members = 全部 Cargo.toml 的 [package].name
        names: set[str] = set()
        for t in texts.values():
            m = re.search(r'^\s*name\s*=\s*"([^"]+)"', t, re.MULTILINE)
            if m:
                names.add(m.group(1))
        return names

    _cache: dict[str, list[str] | None] = {}

    def _versions(_ns: str, name: str):
        if name not in _cache:
            _cache[name] = cargo_registry_versions_list(_ns, name)
        return _cache[name]

    return _enforce_dep_legality_generic(
        project_path, timeout, stack_key="cargo", manifest_name="Cargo.toml",
        find_exclude="*/target/*", registry_versions=_versions,
        workspace_members_from_texts=_members)


def _enforce_dep_legality_go(project_path: str, timeout: int) -> tuple[int, list[str]]:
    """Go 臂（W-6）。"""
    from swarm.worker.dep_legality import go_registry_versions_list

    root_text = _read_project_file(project_path, "go.mod", timeout=20)
    namespace: str | None = None
    if root_text:
        m = re.search(r'^\s*module\s+([\w.\-/]+)', root_text, re.MULTILINE)
        namespace = m.group(1).strip() if m else None

    def _members(texts: dict[str, str]) -> set[str]:
        names: set[str] = set()
        for t in texts.values():
            m = re.search(r'^\s*module\s+([\w.\-/]+)', t, re.MULTILINE)
            if m:
                names.add(m.group(1).strip())
        return names

    _cache: dict[str, list[str] | None] = {}

    def _versions(_ns: str, name: str):
        if name not in _cache:
            _cache[name] = go_registry_versions_list(_ns, name)
        return _cache[name]

    return _enforce_dep_legality_generic(
        project_path, timeout, stack_key="go", manifest_name="go.mod",
        find_exclude="*/vendor/*", registry_versions=_versions,
        namespace=namespace, workspace_members_from_texts=_members)


def _enforce_dep_legality_gradle(project_path: str, timeout: int) -> tuple[int, list[str]]:
    """Gradle 臂（W-6）。"""

    def _members(texts: dict[str, str]) -> set[str]:
        # workspace members = settings.gradle 里的 include ':x' / include("x")
        names: set[str] = set()
        for rel, t in texts.items():
            if rel.lower().startswith("settings.gradle"):
                for m in re.finditer(r"include\s*\(\s*['\"]:?([^'\"]+)['\"]\s*\)", t):
                    names.add(m.group(1).strip().lstrip(":"))
                for m in re.finditer(r"include\s+['\"]:?([^'\"]+)['\"]", t):
                    names.add(m.group(1).strip().lstrip(":"))
        return names

    _cache: dict[tuple[str, str], list[str] | None] = {}

    def _versions(ns: str, name: str):
        key = (ns, name)
        if key not in _cache:
            try:
                vers, reachable = _fetch_maven_versions_probe(ns, name, project_path, timeout)
                _cache[key] = vers if (vers or reachable) else None
            except Exception as _fx:  # noqa: BLE001
                logger.warning("[L1.2.1·dep-legality] Gradle Maven 仓库查询异常（按不可达 fail-open）"
                               "%s:%s → %s", ns, name, _fx)
                _cache[key] = None
        return _cache[key]

    return _enforce_dep_legality_generic(
        project_path, timeout, stack_key="gradle", manifest_name="build.gradle",
        find_exclude="*/build/*", registry_versions=_versions,
        workspace_members_from_texts=_members)


def _enforce_dep_legality_python(project_path: str, timeout: int) -> tuple[int, list[str]]:
    """Python 臂（W-6）：优先 requirements.txt，fallback pyproject/setup。"""
    from swarm.worker.dep_legality import python_registry_versions_list

    def _members(texts: dict[str, str]) -> set[str]:
        # Python 无严格 workspace 成员；pyproject [project].name 作为 root_name 信号
        for rel, t in texts.items():
            if rel.lower() == "pyproject.toml":
                m = re.search(r'^\s*name\s*=\s*"([^"]+)"', t, re.MULTILINE)
                if m:
                    return {m.group(1)}
        return set()

    _cache: dict[str, list[str] | None] = {}

    def _versions(_ns: str, name: str):
        if name not in _cache:
            _cache[name] = python_registry_versions_list(_ns, name)
        return _cache[name]

    # 优先 requirements.txt；没有则试 pyproject.toml
    if _read_project_file(project_path, "requirements.txt", timeout=20):
        return _enforce_dep_legality_generic(
            project_path, timeout, stack_key="python", manifest_name="requirements.txt",
            find_exclude="*/.venv/*", registry_versions=_versions,
            workspace_members_from_texts=_members)
    if _read_project_file(project_path, "pyproject.toml", timeout=20):
        return _enforce_dep_legality_generic(
            project_path, timeout, stack_key="python", manifest_name="pyproject.toml",
            find_exclude="*/.venv/*", registry_versions=_versions,
            workspace_members_from_texts=_members)
    return 0, []


def _attempt_maven_version_repair(
    project_path: str, build_output: str, timeout: int,
    evidence_out: dict | None = None,
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
    classified = parse_missing_classified_artifacts(build_output)
    if not missing and not missing_versions and not classified:
        return 0, []
    changed: set[str] = set()
    _reactor_mods: set[str] | None = None
    _proj_group: str | None = None
    # ── ⓪ classifier 幻觉（R65E8-T3，round65e8 shiro-ehcache 实锤）——早于版本对账 ──
    # `Could not find artifact org.apache.shiro:shiro-ehcache:jar:jakarta:2.0.1`：`jakarta` 是
    # <classifier>，仓库里 shiro-ehcache 根本没有 jakarta 分类变体（只有无 classifier 的正版）。
    # 旧闸把 classifier 误当 version → version-repair 全程 no-op、classifier 永不被剔 →
    # HANDLE_FAILURE 同签名重试到 abandon（round65e8 靠换模型手工修，慢且非确定）。治本：
    #   ★判据（复核 HIGH 治后收窄）★ 只有当【pinned version 本身在 base 坐标 g:a 的可用版本里】
    #   才剔 classifier——版本有效即证明"错的是 classifier 不是 version"。否则（版本查无）是版本错，
    #   交分支① version-repair 处理，绝不误剔 classifier：否则 netty-tcnative:linux-x86_64 之类
    #   合法平台 classifier 遇【版本写错】会被静默剥成无平台绑定的普通 jar（运行期 UnsatisfiedLinkError，
    #   把可归因的"Could not find artifact"换成更难查的暗雷）。
    #   ★fail-open（R56-6）★ 仓库不可达/版本查无 → 本轮不动（绝不据证据缺失剪改）。
    for group, artifact, classifier_val, _cv in classified[:8]:
        _avail, _reach = _fetch_maven_versions_probe(group, artifact, project_path, timeout)
        if not (_reach and _cv in _avail):
            continue
        art_esc = artifact.replace(".", r"\.")
        _cc, _cout, _ce = _run_check_split(
            f"grep -rl '<artifactId>{art_esc}</artifactId>' --include=pom.xml . 2>/dev/null",
            project_path, timeout=30)
        _stripped: list[str] = []
        for _pom in sorted({ln.strip() for ln in (_cout or "").splitlines() if ln.strip()}):
            _text = _read_project_file(project_path, _pom, timeout=20)
            if _text is None:
                continue
            _new = _strip_dep_classifier(_text, group, artifact, classifier_val)
            if _new is not None and _write_project_file(project_path, _pom, _new, timeout=20):
                changed.add(_pom)
                _stripped.append(_pom)
        if _stripped:
            logger.warning(
                "[L1.2.1·classifier-repair] R65E8-T3 %s:%s 声明了 classifier %r，但仓库里该 artifact "
                "在版本 %s 无此分类变体（base 坐标同版可解析）→ 剔除幻觉 classifier（%d pom）；"
                "留着它整模块解析崩、HANDLE_FAILURE 同签名空转到 abandon（round65e8 实锤）",
                group, artifact, classifier_val, _cv, len(_stripped))
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
            if _phantom_internal and evidence_out is not None:
                # R67L-B2（22号文批次2，round67l st-14 实锤）：留痕本轮判【永不可解析】的内部
                # 模块 artifactId——它往往是 plan 声明、由别的子任务生产的内部模块，只是生产者
                # 尚未 merge 进树（本沙箱 reactor 没有≠永远没有）。verify 阶段拿此账对账验收
                # 断言：若考卷 grep 必考该 artifactId，则 prune↔考卷确定性自相矛盾（剪→验收挂
                # →worker 加回→再剪，注定永败），应第一轮即判 BLOCKED 而非烧修复轮死循环。
                evidence_out.setdefault("pruned_phantom_internal", set()).add(artifact)
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
        # ★R63 治本★ 与分支②「缺 version→注入」对称：先据工程家族代际判对齐/剪除。
        # round63 死因：aop@4.0.6 在 Boot 4 不存在（仓库最高属 Boot 3 系 3.5.16），旧逻辑
        # _choose_valid_version 选 3.5.16 → 把**共享** ${spring-boot.version} 降到 3.5.16 →
        # 整 reactor 降代、基座编译崩、-am 全线连坐。共享/平台锚属性绝不因单依赖被降级：
        # 要么对齐工程家族版，要么剪除该依赖（跨代混用是集成期才炸的暗雷）。
        # ★fail-open 铁律（R56-6）★ 只有【仓库确证可达】才敢据代际差剪除依赖；仓库没连上时
        # available=[] 是"证据缺失"而非"确证查无"，据此剪除=断网即误剪合法依赖（本系统最不能犯
        # 的错）。不可达 → _gc 置 None，落回 _choose_valid_version（对空表返回 None）→ 本轮不动。
        # 与分支②的 `if not available and not _reach ... continue` 守卫对称。
        _fam = _group_family_version(project_path, group, artifact)
        _gc = _family_generation_choice(_fam, available) if _reachable else None
        if _gc is _PRUNE_DEP:
            art_esc = artifact.replace(".", r"\.")
            _pc, _pout, _pe = _run_check_split(
                f"grep -rl '<artifactId>{art_esc}</artifactId>' --include=pom.xml . 2>/dev/null",
                project_path, timeout=30)
            _cut: list[str] = []
            for _pom in sorted({ln.strip() for ln in (_pout or "").splitlines() if ln.strip()}):
                _text = _read_project_file(project_path, _pom, timeout=20)
                if _text is None:
                    continue
                _new = _prune_dep_blocks(_text, group, artifact, even_with_version=True)
                if _new is not None and _write_project_file(project_path, _pom, _new, timeout=20):
                    changed.add(_pom)
                    _cut.append(_pom)
            logger.warning(
                "[L1.2.1·generation-mismatch] R63 工程的 %s 家族钉在 %s，但 %s 版本 %s 在该代不存在"
                "（仓库可用最高稳定版=%s，属另一代）→ 剪除该依赖，绝不降级共享锚属性 ${...}"
                "（降级会连坐整 reactor 代际，正是 round63 死因）（%d pom）",
                group, _fam, artifact, bad_ver, pick_latest_stable(available) or "?", len(_cut))
            continue
        good_ver = _gc if _gc is not None else _choose_valid_version(bad_ver, available)
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
            # ★R63 治本·共享锚保护（不依赖家族探测的兜底不变量）★ version-repair 只许校正
            # 【专属于本依赖】的私有版本属性；被 ≥2 个依赖共享的平台/BOM 版本锚（如
            # ${spring-boot.version}）绝不因单个依赖的版本诉求被降级——那会连坐整棵 reactor 的
            # 代际（round63 死因）。代际守卫在家族可探测时已剪除跨代依赖；此处再兜住"家族属性钉在
            # 中间层父 pom、_group_family_version 探测不到"的拓扑：只要该属性被别的依赖引用即拒改。
            _rc, _rout, _re2 = _run_check_split(
                f"grep -rlF '${{{prop}}}' --include=pom.xml . 2>/dev/null",
                project_path, timeout=30)
            _ctexts = []
            for _cpom in sorted({ln.strip() for ln in (_rout or "").splitlines() if ln.strip()}):
                _ct = _read_project_file(project_path, _cpom, timeout=20)
                if _ct is not None:
                    _ctexts.append(_ct)
            _consumers = _dep_consumers_of_property(_ctexts, prop)
            if _consumers - {artifact}:
                logger.warning(
                    "[L1.2.1·version-repair] 属性 ${%s} 被多个依赖共享（%s）→ 拒绝为 %s 单独降级"
                    "（共享/平台版本锚降级会连坐整 reactor 代际，正是 round63 死因）；"
                    "该依赖交代际守卫/更上层防线处理", prop, sorted(_consumers), artifact)
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
        # R54-5 / R63：与分支①共用同一代际判据（_family_generation_choice，单一权威）——
        # "稳定版"只挡预发布，挡不住"版本对、代际错"（round54：Boot 4.0.6 工程被注进 Boot 3 系的
        # spring-boot-starter-aop:3.5.16）。判据抽成纯函数后两分支不再各写一份（治 round57-3 漂移）。
        _fam = _group_family_version(project_path, group, artifact)
        _choice = _family_generation_choice(_fam, available)
        if _choice is _PRUNE_DEP:
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
        # 与工程同代 → 对齐家族版（唯一正确目标）；无家族先例 → 稳定版优先（R53-3，禁 M#/RC/alpha）
        good_ver = _choice if _choice is not None else pick_latest_stable(available)
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


def _build_blocked_on_unbuilt_internal_classes(
    project_path: str, build_output: str, timeout: int
) -> set[str]:
    """H-3a（round67j 法证A 横切病灶·_build_blocked_on_unbuilt_internal 的 sibling）：
    构建失败是否【全因引用了包已在树里、但类本身尚未建出的项目内部类】。

    死因实锤（round67 task 64cb44ed，L1310）：st-50-1 的 GoogleAuthController 引
    `ISysGoogleAuthService`——其包 `com.ruoyi.system.service` 已存在（有别的类）、类正是
    st-8-1 的 create 目标还没落地 → javac 报「cannot find symbol: class C / location:
    package P」而非「package does not exist」→ 走不进 ②（unbuilt_internal 判"包已在树=
    真错 FAIL"）→ 被判 hard fail 连烧修复轮后弃修；同型整包缺失的 st-48 却判 BLOCKED 退避
    ——同一"生产者未落地"两种结局。本函数统一口径：类级缺失同判 BLOCKED（internal_pkg_not_built
    同分类同消费链），待生产者 merge 落地由 transient 重试自然消解。

    判据（保守，宁可不标也不误标，与 ② 同律）：所有「cannot find symbol: class C /
    location: package P」都满足
      ① P 是【项目自有 groupId 前缀】（内部包，非第三方/JDK）；且
      ② 当前项目树里【无任何 C.java 声明 package P】（=类确实还没被任何子任务建出；
         若树里已有 → 真编译错如 classpath/拼写问题，照常 FAIL）。
    只要有一个缺符号是第三方、或类已在树里 → 返回空集，照常 FAIL。返回被阻断的
    【包集合】（复用 blocked_on_packages 语义，brain 反查生产者按包归属零改动）。"""
    pairs = parse_missing_symbol_classes(build_output)
    if not pairs:
        return set()
    own = _project_own_packages(project_path, timeout)
    if not own:
        return set()
    blocked_pkgs: set[str] = set()
    for cls, pkg in pairs:
        if any(pkg == p.rstrip(".") or pkg.startswith(p) for p in _DEP_REPAIR_SKIP_PREFIXES):
            return set()  # JDK/servlet 命名空间 → 非生产者未落地
        if not any(pkg == g or pkg.startswith(g + ".") for g in own):
            return set()  # 第三方缺符号 → 交 dep-repair，照常 FAIL
        # 类是否已在树里——已在 = 真编译错（classpath/模块序），照常 FAIL。
        # ★复核 MEDIUM 整改：内容级声明判，非文件名判★——次级（非 public）top-level 类合法
        # 共居别的 .java 文件（class C{} 写在 Foo.java 里），--include='{cls}.java' 文件名判
        # 假阴 → 误 BLOCKED 死等永不独立建 C.java 的幽灵生产者（round19 #10 型）。改为两步：
        # 先取声明 package P 的文件集，再在其中按【类声明】内容匹配（与 sibling 包级判据同为
        # 内容判，盲区对齐消除）。
        cmd_pkg = (
            f"grep -rlE '^[[:space:]]*package[[:space:]]+{re.escape(pkg)}[[:space:]]*;' "
            f"--include='*.java' . 2>/dev/null | head -50"
        )
        _ec, pkg_files, _e = _run_check_split(cmd_pkg, project_path, timeout=min(timeout, 20))
        _flist = [f for f in (pkg_files or "").strip().splitlines() if f.strip()]
        if _flist:
            _quoted = " ".join(shlex.quote(f) for f in _flist[:50])  # R3 MED：跟随本文件 shlex 惯例
            cmd_cls = (
                f"grep -lE '\\b(class|interface|enum|record)[[:space:]]+{re.escape(cls)}\\b' "
                f"{_quoted} 2>/dev/null | head -1"
            )
            _ec2, out2, _e2 = _run_check_split(cmd_cls, project_path, timeout=min(timeout, 20))
            if (out2 or "").strip():
                return set()  # 类声明已在树（含共居次级类）→ 真错，照常 FAIL
        blocked_pkgs.add(pkg)
    return blocked_pkgs


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


# X-H8：构建输出里是否出现 JVM 源文件（javac/kotlinc 的报错恒带文件名）。用来把 Java 修复族的
# file_signal 从写死的 `True` 换成真判据——无栈画像时不再对 Go/Rust/Python 工程恒真。
_JVM_SRC_IN_TEXT_RE = re.compile(r"[\w./\\-]+\.(?:java|kt|scala)\b")


def _stack_repair_langs(project_stack: dict | None) -> set[str] | None:
    """据【权威栈画像】(detect_stack：小模型识别→大模型确认→KB 持久化) 选 repair 生态集合。

    单一事实源：adapter 选择是 project_stack 的【消费者】，与 tech_design/plan/worker prompt
    同源，不再独立按文件扩展名瞎猜语言。以 build 工具为准（maven/gradle/go/cargo/npm…，无歧义；
    避开 "javascript" 含 "java" 子串陷阱）+ 前端形态。无画像/未判明 → 返回 None，调用方回退扩展名。
    """
    if not project_stack:
        return None
    # P-C3 复核 R2-H5：SPA-like 子集走单一事实源（lazy import 防 worker→brain 顶层成环；
    # llm_schemas 仅依赖 pydantic+swarm.types，无传递重量）。
    from swarm.brain.llm_schemas import SPA_LIKE_KINDS
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
    if fe_kind in SPA_LIKE_KINDS or any(
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


# DR-PM66-C3(#113)：NVFP4 量化模型往源码写【全角 CJK 标点】（U+FF0C，/FF1A：/FF08（/FF09）
# /FF1B；/3001、/3002。等）→ javac `illegal character`。这些标点在【代码位】恒非法（字符串/注释里
# 的 CJK 合法，不在此扫）。用 POSIX bracket 直接列全角标点字面(UTF-8)，可移植(GNU/busybox/BSD)，
# 不用 GNU 扩展 `grep -P '\x{ff0c}'`（busybox 不认→漏扫）。
_FULLWIDTH_PUNCT_CHARS = "，：（）；、。！？【】｛｝"


def _scan_fullwidth_punct(project_path: str, modified, timeout: int) -> list[str]:
    """#113 诊断预扫：在【本子任务改动的源文件】里定位全角 CJK 标点，返回 `file:line:内容` 精确坐标。

    诊断【不自改】——字符串字面量里的全角标点合法（`"你好，世界"`），逐行区分字符串/注释超 grep 能力，
    自改有腐蚀字符串风险；故只【surfacing 精确坐标】喂 worker 修复轮（早于/补强 javac 的 illegal
    character，定位更准）。栈中立、只读、fail-safe（异常/无匹配→空）。"""
    mods = {_norm_src_path(f) for f in (modified or []) if str(f).strip()
            and str(f).rsplit(".", 1)[-1].lower() in ("java", "kt", "kts", "scala", "go", "ts", "tsx", "js", "cs")}
    if not mods:
        return []
    hits: list[str] = []
    for rel in sorted(mods):
        # bracket 内直接列全角字面(UTF-8)——POSIX、可移植；-n 出行号。
        # ★复核 F4 整改★：显式 `LC_ALL=C.UTF-8` 让 bracket 按【码点】匹配——否则 C/POSIX locale 下
        # grep 退化为逐字节，全角标点多字节序列被拆碎，与普通 CJK 字（本仓注释/日志遍布）的延续字节
        # 重叠 → 漏扫或误命中。只读诊断，locale 不支持时 grep 仍跑（最坏退回旧字节行为），不阻断。
        gcmd = f"LC_ALL=C.UTF-8 grep -n '[{_FULLWIDTH_PUNCT_CHARS}]' {shlex.quote(rel)} 2>/dev/null | head -20"
        try:
            ec, out, _ = _cached_scan(gcmd, project_path, timeout=min(timeout, 30))
        except Exception:  # noqa: BLE001 — 诊断失败不致命
            continue
        for line in (out or "").splitlines():
            if line.strip():
                hits.append(f"{rel}:{line.strip()[:120]}")
    return hits[:40]


def _attempt_pseudospace_repair(
    project_path: str, modified, timeout: int
) -> tuple[int, list[str]]:
    """DR-10-F2/DR-PM66-A6(#103/#114) 治本：折叠 NVFP4 量化模型注入的【标识符内伪空格】——
    `groups.is Empty()` / `groups.isE mpty()` → `groups.isEmpty()`。这类畸形令原标识符被空格切成
    两 token，逃逸 symbol-repair 的编辑距离匹配（`is Empty` 与 `isEmpty` 距离>2），且模型自纠会陷入
    复读退化循环反复烧流（round66 实证 4 次 abort 各 21-27s）。

    ★黄灯·双闸防误并★（栈中立，C 家族成员调用位）：
      ① 位置语法非法——只折【成员调用位】`.<w1><空格><w2>(`（`.word word(` 在 C 家族恒非法，
         合法代码绝不这样写；杜绝误折 `new Foo` / `return x` / 泛型 `List <String>`）。
      ② 折叠后命中项目高频——`<w1><w2>` 必须是项目现存高频符号(≥5)才折；命中不了=放弃（fail-safe，
         交编译闸+worker 修复循环兜底）。两闸同时满足才改，绝不赌。只修【本子任务改动文件】。"""
    # ★复核 F5 整改★：freq 表只扫 `*.java`/`*.kt`（gate② 判据），故 mods 诚实收窄到 JVM 家族
    # （java/kt/kts/scala）——放宽到 go/ts/cs 会让 gate② 对非 JVM 栈恒不通过=死代码/名实不符。
    mods = {_norm_src_path(f) for f in (modified or []) if str(f).strip()
            and str(f).rsplit(".", 1)[-1].lower() in ("java", "kt", "kts", "scala")}
    if not mods:
        return 0, []
    # 复用 symbol-repair 同一 freq 扫描（_cached_scan 按 cmd 缓存，命令字节相同→命中缓存零重扫）
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
    seen_fold: set[tuple[str, str, str]] = set()
    for rel in sorted(mods):
        # 闸①：抓成员调用位内部空格片段 `.<w1><空白+><w2>...(`（含 `.` 起头、`(` 收尾）
        gcmd = (f"grep -noE '\\.[A-Za-z_][A-Za-z0-9_]*[[:space:]][[:space:]]*[A-Za-z0-9_]+[[:space:]]*\\(' "
                f"{shlex.quote(rel)} 2>/dev/null | head -40")
        try:
            ec, out, _ = _cached_scan(gcmd, project_path, timeout=min(timeout, 30))
        except Exception:  # noqa: BLE001
            continue
        if ec != 0 or not out:
            continue
        for line in out.splitlines():
            # grep -n 输出 `<lineno>:<match>` —— 抽行号（★复核 F1 整改★：mutation 只落该行）
            lm = re.match(r"(\d+):", line)
            mm = re.search(r"\.([A-Za-z_][A-Za-z0-9_]*)\s+([A-Za-z0-9_]+)", line)
            if not (lm and mm):
                continue
            lineno = int(lm.group(1))
            w1, w2 = mm.group(1), mm.group(2)
            # ★复核 F2 整改★：w1 是语言关键字（尤其 `new`——`outer.new Inner(` 是合法内部类实例化，
            # 精确命中 gate①）→ 绝不折（fold 成 `newInner` 会腐蚀合法语法）。
            if w1 in _JVM_KEYWORDS:
                continue
            folded = w1 + w2
            if (rel, lineno, w1, w2) in seen_fold:
                continue
            seen_fold.add((rel, lineno, w1, w2))
            if freq.get(folded, 0) < 5:
                continue  # 闸②：折叠后非项目高频符号 → 不折（fail-safe）
            # perl 折叠该成员调用位的内部空白：`.<w1><空白+><w2>` → `.<w1><w2>`。
            # ★复核 F1 整改★：mutation 必须【定点到 gate① 报出的行号】(`$. == lineno`)——否则
            # `s///g` file-wide 会误折字符串/javadoc 里合法的 `.w1 w2(` 文本（如日志 `"call .get
            # Value(k)"`，前瞻 `(?=\()` 也拦不住带括号的字符串）。前瞻额外要求后随调用括号（成员调用位）。
            _pp = re.escape("." + w1) + r"\s+" + re.escape(w2) + r"(?=\s*\()"
            scmd = (f"perl -i.bak -pe 's#{_pp}#.{w1}{w2}#g if $. == {lineno}' {shlex.quote(rel)} "
                    f"&& rm -f {shlex.quote(rel + '.bak')}")
            ec2, _o = _run_l1_command(scmd, project_path, timeout=20)
            if ec2 == 0:
                changed.add(rel)
                logger.info("[L1.2.1·pseudospace] #103 %s: `.%s %s(`→`.%s%s(`（NVFP4 伪空格，"
                            "折叠后项目高频 %d）", rel, w1, w2, w1, w2, freq[folded])
    return len(changed), sorted(changed)


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
        # DR-PM66-A6(#114) 治本：防【两高频真符号间震荡】（StringUtils↔SpringUtils×4 不收敛：两者
        # 都高频、互为近邻，改 A→B 后 B 处又报错改 B→A）。★复核 F3 整改★：判据不是裸 `freq(name)≥5`
        # ——那会把"同一 typo 被复读复制 ≥5 次"(RuoYi 模板化 CRUD 常见)误判成真符号而漏修。真震荡的
        # 签名是【name 与 good 频次相当】(都是既有真符号)；typo→主流纠正是【good 频次远高于 name】
        # (isEmtpy(5)→isEmpty(128))。仅当 name 高频【且】good 未显著更高频(≤name×3)才判震荡跳过。
        _fn, _fg = freq.get(name, 0), freq.get(good, 0)
        if _fn >= 5 and _fg <= _fn * 3:
            logger.info("[L1.2.1·symbol-repair] #114 %r(频=%d)↔%r(频=%d) 频次相当=两高频真符号→"
                        "判震荡，不全局改名（防来回不收敛）", name, _fn, good, _fg)
            continue
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


def _missing_internal_produced_in_scope(
    scope, blocked_pkgs: set[str], blocked_cls: list[str],
    *, language_key: str | None = None, project_path: str | None = None,
    timeout: int = 60, run=None, driver_refs=None, unresolved_out: set | None = None,
) -> tuple[set[str], set[str]]:
    """R67L-B2（22号文批次2 H-3a）：缺失内部包/类中【生产者在本子任务自己 scope 内】的子集。

    scope 文件经 classpath_fqn_key（brain 侧 JVM 门控单一口径，非 JVM 布局返 None 不判，
    栈中立）解出 FQN 相对路径，与缺失包/类对账：
      - 包 P：某 scope 源码文件的包路径段 == P 的路径形 → 该包由本子任务自产；
      - 类 C（FQN 点分）：某 scope 源码文件的 FQN 路径形（去扩展名）== C → 该类由本子任务自产。
    命中=缺符号是【自己没建出】（capability，修复梯可治），不是【等上游生产者】。
    返回 (own_pkgs, own_classes)，均为点分 FQN。

    ★X-C3：非 JVM 半边（language_key/project_path/run 三者齐备时启用）★
    上面那条 `classpath_fqn_key` 通道是 **JVM-only**——它对 go/rust/node/python 布局恒返
    None ⇒ own 集恒空 ⇒ 本闸对非 JVM 栈**静默失效**。X-C3 之前非 JVM 到不了调用点（缺口
    潜伏无害），X-C3 让它们到得了，于是缺口变成真 fail-open：子任务自己漏建 `internal/svc`
    会被判 BLOCKED 去等永不到来的生产者（#10 幽灵生产者，烧满退避阶梯）。补 driver 半边
    （`l1_error_drivers.produced_in_scope`，ref↔路径是唯一栈相关部分，判据仍在本函数）。
    缺参数（老调用方）→ 只走 JVM 通道，零回归。
    """
    own_pkgs: set[str] = set()
    own_cls: set[str] = set()
    if scope is None or not (blocked_pkgs or blocked_cls):
        return own_pkgs, own_cls
    if language_key and project_path and run is not None:
        try:
            from swarm.worker.l1_error_drivers import produced_in_scope
            _files = (list(getattr(scope, "create_files", None) or [])
                      + list(getattr(scope, "writable", None) or []))
            _own, _unres = produced_in_scope(
                language_key, driver_refs or list(blocked_pkgs), _files,
                project_path, timeout, run)
            own_pkgs |= _own
            if unresolved_out is not None:
                unresolved_out.update(_unres)
            # 符号级：容器自产 ⇒ 该容器下的符号也归本子任务（容器是它建的，符号缺就是它漏建）
            # ★复核 LOW-2★ 用词干匹配而非裸 startswith——后者会让容器 `svc` 吞掉
            # `svcutil.Foo`（正是 `_stem_matches` 存在的理由，在为它建原语的地方留裸
            # startswith 就是复发种子）。符号 FQN 用栈分隔符拼，故按前缀+分隔符判。
            from swarm.worker.l1_error_drivers import driver_for as _dfor
            _sep = getattr(_dfor(language_key), "symbol_sep", ".") or "."
            for _c in blocked_cls:
                _cs = str(_c)
                if any(_cs == p or _cs.startswith(p + _sep) for p in own_pkgs):
                    own_cls.add(_cs)
        except Exception as _exc:  # noqa: BLE001 — driver 半边异常绝不阻断裁决
            # ★复核 H-3★ 原实现在此 fail-**open**（异常 → own 集空 → 判 BLOCKED），日志还写
            # "维持 BLOCKED 语义"，与 CRITICAL-2 的整改结论（归属解不出 → 不敢断言外部生产者）
            # 方向相反：同一函数两条降级路径反向。且异常吞在这里 ⇒ `unresolved_out` 永远收不到
            # ⇒ 外层 `if _unres:` 判不到 ⇒ 静默。改成把全部 blocked_pkgs 记为"归属未知"，
            # 由裁决层统一拦成 FAIL（与 unresolved 同一出口，单一方向）。
            if unresolved_out is not None:
                unresolved_out.update(str(p) for p in blocked_pkgs)
            logger.warning(
                "[L1] X-C3 步骤4 driver 半边异常 → 归属全部记为未知（裁决层将落 FAIL 修复梯，"
                "绝不据此判 BLOCKED 让 worker 去等自己）: %r", _exc)
    try:
        from swarm.brain.contract_utils import classpath_fqn_key
    except Exception:  # noqa: BLE001 — 口径源不可用时 fail-open 不判（维持原 BLOCKED 语义）
        return own_pkgs, own_cls
    files = (list(getattr(scope, "create_files", None) or [])
             + list(getattr(scope, "writable", None) or []))
    pkg_paths = {str(p).replace(".", "/") for p in blocked_pkgs}
    cls_paths = {str(c).replace(".", "/") for c in blocked_cls}
    for f in files:
        key = classpath_fqn_key(str(f))
        if not key:
            continue
        _mod, fqn_rel = key            # 形如 com/ruoyi/alarm/sender/dto/AlarmDto.java
        if "/" not in fqn_rel:
            continue
        pkg_rel, fname = fqn_rel.rsplit("/", 1)
        stem = f"{pkg_rel}/{fname.rsplit('.', 1)[0]}" if "." in fname else fqn_rel
        if pkg_rel in pkg_paths:
            own_pkgs.add(pkg_rel.replace("/", "."))
        if stem in cls_paths:
            own_cls.add(stem.replace("/", "."))
    return own_pkgs, own_cls


# L-1：stage → 日志阶段号的单一口径（原为 2 路三元，接了第三个 stage 后会把 test 标成 "2"）
_XC3_STAGE_TAG = {"build": "2.1", "compile": "2", "test": "3"}

_XC3_DISARM_EXPECTED = frozenset({"self_handled", "unregistered_stack"})


def _record_xc3_disarm(details: dict, disarm: dict, *, stage: str,
                       lang: str | None) -> None:
    """★复核 H-1★ 把 X-C3 求解器"返空的原因"落成机读账 + 一次 WARNING。

    没有它，五种返空成因（未收录栈 / java self-handled / 一条都没解析出 / 有第三方票 /
    有已在树里的）对调用方**完全同形**，于是"机制一次都没触发"与"本轮真没有内部缺失"
    机读不可分——解析器一旦漏掉某种真实形态（上一轮实测到 GOPATH 形与 bundler 形两例），
    唯一症状就是这里返空，而返空不留痕。norms 层死 12 天跨 5+ 轮全零无信号即此形状。

    `self_handled`（java 走专用链）与 `unregistered_stack`（本就不支持）是**预期**的返空，
    只落账不 WARNING；其余三种是"武装被解除"，必须响。
    """
    reason = (disarm or {}).get("reason")
    if not reason:
        return
    details["xc3_disarm"] = {"stage": stage, "reason": reason,
                             **({"ref": disarm["ref"]} if disarm.get("ref") else {})}
    if reason in _XC3_DISARM_EXPECTED:
        return
    logger.warning(
        "[L1.%s] X-C3 归因未产出 BLOCKED（栈=%s 原因=%s%s）——若这是解析器漏了该栈的真实"
        "错误形态，本条是唯一信号（返空本身与『真没有内部缺失』不可分）",
        _XC3_STAGE_TAG.get(stage, "?"), lang, reason,
        f" ref={disarm.get('ref')}" if disarm.get("ref") else "")


def decide_unbuilt_internal_verdict(
    details: dict, scope, blocked_pkgs: set[str], blocked_cls: list[str],
    *, cmd: str, stage: str, output: str = "",
    language_key: str | None = None, project_path: str | None = None,
    timeout: int = 60, run=None, driver_refs=None,
) -> bool:
    """步骤 4+5 的**共用裁决尾**：BLOCKED（返 True）还是落 FAIL 修复梯（返 False）。

    ★为什么必须是模块级共用函数（不是内联两份）★ X-C3 需要**两个**调用点：L1.2.1 build 闸
    （go/rust——它们在 L1.2 无逐文件检查器）与 L1.2 compile 闸（node/ts——`tsc --noEmit`
    在 L1.2 就 hard-fail，build 闸永不执行 ⇒ 内联在 build 闸里的 TS 分支是**死代码**，
    B-4a CRITICAL-1 原型）。步骤 4 的"worker 无权等自己"边界与步骤 5 的类级 FQN（brain 侧
    防臆造类 futile 判据的唯一输入）都是"重复实现即失守"的横切不变量——故抽成单一实现，
    两个调用点共用，测试可直接调本体。

    `stage`（`build`/`compile`）决定写哪个 ok 键：两者都必须置 **None 而非 False**——
    `l1_verdict._det_fail_source` 把 `is False` 读成 capability「编译失败」，写 False
    会让 BLOCKED 被归成能力失败去换模型（正是本机制要防的）。
    """
    if not blocked_pkgs:
        return False
    # R67L-B2（22号文批次2 H-3a，round67l st-2 实锤）：缺失包/类若【全部】在本子任务自己
    # scope 内有生产者 → 是【自己没建出】的 capability 失败，判 BLOCKED 会把可修编译错送进
    # 不可修通道、fix 循环短路（worker 无权"等"自己）。落公共 FAIL 尾进修复梯。
    # X-C3：非 JVM 栈必须传 language_key/run，否则步骤 4 只有 JVM 通道 = 对 go/rust/node/
    # python 恒空过（fail-open，去等永不到来的生产者）。
    _unres: set[str] = set()
    _own_pkgs, _own_cls = _missing_internal_produced_in_scope(
        scope, blocked_pkgs, blocked_cls,
        language_key=language_key, project_path=project_path,
        timeout=timeout, run=run, driver_refs=driver_refs, unresolved_out=_unres)
    if _unres:
        # ★复核 CRITICAL-2 的裁决半边★ 归属**解不出**时不敢断言"生产者在外部"——
        # 判 BLOCKED 就可能让 worker 去等自己（#10 幽灵生产者，烧满退避阶梯）；落 FAIL
        # 修复梯只是退回现状。两侧代价不对称，故 UNKNOWN 一律不 BLOCKED（fail-closed）。
        # L-3：账要能与 blocked 集对账 → 全量落（外加计数），不截断
        details["blocked_owner_unresolved"] = sorted(_unres)
        details["blocked_owner_unresolved_n"] = len(_unres)
        details.pop("blocked_via_error_driver", None)   # M-4：同上，不留粘滞键
        logger.warning(
            "[L1.%s] X-C3 缺失标识的归属解不出（%d/%d 条：%s）→ 不敢断言外部生产者，落 FAIL "
            "修复梯（误判 BLOCKED 会让 worker 去等自己）",
            _XC3_STAGE_TAG.get(stage, "?"), len(_unres), len(blocked_pkgs),
            sorted(_unres)[:4])
        return False
    _ext_pkgs = blocked_pkgs - _own_pkgs
    _ext_cls = [c for c in blocked_cls if c not in _own_cls]
    if not _ext_pkgs and not _ext_cls:
        details["in_scope_producer_fail"] = sorted(_own_pkgs | set(_own_cls))
        logger.warning(
            "[L1.%s] R67L-B2 缺失内部包/类的生产者全在本子任务 scope 内（自己没建出，"
            "非等上游）→ 落 FAIL 修复梯，不判 BLOCKED: %s",
            _XC3_STAGE_TAG.get(stage, "?"), sorted(blocked_pkgs)[:8])
        details.pop("blocked_via_error_driver", None)   # M-4：裁决翻转成 FAIL → 不留粘滞键
        return False
    if _own_pkgs or _own_cls:
        # 混合：自有部分留痕（FAIL 侧线索），外部部分仍 BLOCKED 等上游。
        details["in_scope_producer_suppressed"] = sorted(_own_pkgs | set(_own_cls))
    # 三个 stage 各写自己的 ok 键，一律 **None 而非 False**（`l1_verdict._det_fail_source`
    # 把 `is False` 读成 capability 失败 → BLOCKED 会被归成能力失败去换模型）。
    details[{"build": "l1_2_1_build_ok", "compile": "l1_2_compile_ok",
             "test": "l1_3_test_ok"}[stage]] = None
    details["build_blocked"] = cmd
    details["pipeline_blocked"] = "internal_pkg_not_built"
    details["not_run_kind"] = NotRunKind.BLOCKED.value
    # 结构化吐【缺哪些项目内部包】，供 brain 反查生产者子任务（按 scope/目标包归属）：
    # 生产者已被永久放弃 → 本下游不可恢复，连坐放弃而非无限 BLOCKED→replan。
    details["blocked_on_packages"] = sorted(blocked_pkgs)
    if blocked_cls:
        details["blocked_on_classes"] = blocked_cls   # H-3a 类级 futile 判据用
    # ★X-C3-A（治法 B）★ 非 JVM ref 的**路径口径**——brain 侧三个消费者内部都是"先把 Java
    # 点分 FQN 转成路径再比"，非 JVM ref 按那个口径转出来无一命中 ⇒ _prods=∅ → _futile
    # → 推不出该建啥 → _unrecoverable ⇒ 首轮连坐放弃（比 X-C3 之前更坏）。故一并吐路径，
    # 消费者优先读它、缺席回落原路 ⇒ **JVM 侧逐字节不变**（blocked_on_paths 对 java 恒缺席）。
    if language_key and project_path and run is not None:
        # X-C3-A：栈键由**裁决层**写（它有 language_key）。原先只有三个调用点各写一遍 ⇒
        # 新调用点漏写就静默丢栈 ⇒ brain 侧 `_derive_missing_type_files` 取不到扩展名 → 返 []
        # → 连坐放弃。写在这里＝与路径键同源，不可能只落一半。
        # ★复核 LOW-10★ 这里是栈键的**唯一**写点。原先三个调用点各写一遍、这里再
        # setdefault ⇒ 生产上本行是 no-op，"裁决层是权威"只在测试里成立（直调本体才走到）。
        # 三处旧写点已删：多写点＝新调用点必然漏写（本战役已因此返工三次）。
        details["blocked_via_error_driver"] = language_key
        try:
            from swarm.worker.l1_error_drivers import ref_path_stems
            _paths = ref_path_stems(language_key, driver_refs or list(blocked_pkgs),
                                    project_path, timeout, run)
            # ★这里**刻意没有** else 分支（"BLOCKED 却无路径口径"的降级账已删）★
            #
            # 这条账我加过、删过、又加过、现在再删——值得把因果写清，免得下一个人重走：
            #   · 最初删掉：理由是"词干解不出的 ref 必先在上面的 UNKNOWN 闸早返 FAIL"。
            #   · reviewer 证伪：那个推理漏了**第三态**——词干*解得出但被过滤成空*。当时
            #     `ref_path_stems` 写的是 `[s for s in stems if s]`，把 Go 根包的合法词干 `[""]`
            #     过滤没了 ⇒ UNKNOWN 闸不拦、路径键又缺席 ⇒ 静默。于是恢复。
            #   · hunter 又指它"生产不可达 + 零消费者"＝空账。
            #   ★两人都对，只是**针对的代码状态不同**★：我在 HIGH-2 那轮把过滤修了（`""` 保留），
            #     第三态就此消失，分支重新变成不可达。
            # 现按**实测**定案（穷举 go 根包/非本模块、node `./`+`../`+裸包名、rust
            # `self::`/`super::`、python 正常共 8 种形态走裁决本体）：`absent` 恒为 None，
            # 因为"没词干"与"归属未知"读的是**同一个** `ref_tree_paths` ⇒ 前者必然蕴含后者 ⇒
            # 永远在 `if _unres:` 处早返。故到得了本行就必有词干。
            # ★维持这个不变量的责任在 `ref_path_stems`：它不许再把合法词干过滤掉★
            # （`test_absent_branch_is_unreachable_by_construction` 锁它）。
            if _paths:
                details["blocked_on_paths"] = sorted(
                    {s for stems in _paths.values() for s in stems})
                _by_ref = {k: sorted(v) for k, v in _paths.items()}
                # ★复核 CRITICAL-1 配套★ 符号级 FQN（`<容器><sep><符号>`）也要有路径口径，
                # 否则 brain 的类级臂 `_pbr.get(cls)` 恒空 → 仍走 JVM-only 的
                # `_class_in_baseline` → 非 JVM 恒 futile → 首轮连坐放弃。符号的落点就是
                # **其容器**的落点（容器没建出来，符号自然也不在），故复用容器词干。
                from swarm.worker.l1_error_drivers import driver_for as _dfor2
                _sym_sep = getattr(_dfor2(language_key), "symbol_sep", ".") or "."
                for _c in blocked_cls:
                    _cs = str(_c)
                    # ★复核 HIGH-2★ 必须取**最长**匹配容器，不能"首命中即 break"：
                    # 两个 ref 互为前缀时（`app.services` 与 `app.services.user` 同批缺失）
                    # 首命中取决于 dict 插入序 ⇒ FQN `app.services.user.list_users` 可能继承
                    # `app/services` 的词干 ⇒ `_package_in_baseline` 拿**错容器**去查 →
                    # `app/services` 存在 → 返 True → futile=False「继续等」，而真容器
                    # `app/services/user` 其实不在 ⇒ 烧满退避阶梯（#10 幽灵生产者，最贵那侧）。
                    # 且结果依赖插入序＝非确定性。前缀关系下最长匹配唯一，无平局。
                    _best = None
                    for _ref in _paths:
                        # 分隔符取自 driver（Rust `::`、ESM `#`）——与产 FQN 时同源，
                        # 别在这里另猜一套（口径分叉是本批 C-1 的病根）。
                        if _cs == _ref or _cs.startswith(_ref + _sym_sep):
                            if _best is None or len(_ref) > len(_best):
                                _best = _ref
                    if _best is not None:
                        _by_ref.setdefault(_cs, sorted(_paths[_best]))
                details["blocked_on_paths_by_ref"] = _by_ref
        except Exception as _pexc:  # noqa: BLE001 — 路径口径失败绝不改变 BLOCKED 裁决
            details["blocked_on_paths_error"] = f"{type(_pexc).__name__}: {_pexc}"[:200]
            logger.warning("[L1] X-C3-A 路径口径计算异常（brain 侧回落老路）: %r", _pexc)
    logger.warning(
        "[L1.%s] 构建缺【尚未建出的项目内部包】(②跨模块/跨子任务未就绪) → 标 BLOCKED "
        "退避待生产者落地，不连坐本子任务: %s",
        _XC3_STAGE_TAG.get(stage, "?"), (output or "")[:200])
    return True


def _attempt_build_repair(
    project_path: str,
    build_output: str,
    modified: list[str],
    timeout: int,
    project_stack: dict | None = None,
    evidence_out: dict | None = None,
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

    # ★X-H8（27 号文 §3.2 HIGH）★ 这里原先写死 `eligible("java", True)`。原意是对的
    # （"错误信息里就带 .java 文件，无需 modified 列出"），但 `True` 让它变成**无条件**：
    # 无栈画像时（`stack_langs is None` → 回退 file_signal）Go/Rust/Python 工程**每轮**都要跑
    # 一遍 Java 修复族，其中 `_attempt_dependency_repair` 会**联网打 Maven Central 全文检索** ⇒
    # 白付网络往返与超时预算，且无栈画像恰恰是最该省的时候。
    # 治法：把"错误信息里带 JVM 源文件"变成**真判据**而不是常量——它既保住原意（modified 没列
    # 出也认），又不再对非 JVM 工程恒真。判据只读已有的 build_output，零额外探测。
    _jvm_signal = bool(
        any(f.endswith((".java", ".kt", ".scala")) for f in mods)
        or _JVM_SRC_IN_TEXT_RE.search(build_output or "")
    )
    if eligible("java", _jvm_signal):
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
            _accum(_attempt_maven_version_repair(
                project_path, build_output, timeout, evidence_out=evidence_out))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[L1.2.1·repair] Maven version-repair 异常(跳过): %s", exc)
        # #103/#114：NVFP4 伪空格标识符（`is Empty`）→ 成员调用位空格折叠（双闸防误并）。放在
        # symbol-repair【前】——折叠后 `isEmpty` 变合法，避免逃逸编辑距离匹配 + 复读退化循环烧流。
        try:
            _accum(_attempt_pseudospace_repair(project_path, modified, timeout))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[L1.2.1·repair] pseudospace-repair 异常(跳过): %s", exc)
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
        # ★#29-2 W-1★ A2 的三个 _inject_*（npm/cargo/go）是**纯本地** `Path.write_text`，
        # 而本函数的调用方在修复后用 `_run_l1_command` 重跑构建，那是**沙箱优先**的
        # （见其 docstring："若有活跃沙箱上下文 → 在沙箱里跑"）⇒ 构建读的是 bootstrap
        # 上传的旧副本 ⇒ **整个 A2 机制对生产 L1 裁决零影响**（本地改了、构建看不见）。
        # 对照：Maven 侧 `_inject_dependency` 在沙箱内改，同一治本在不同栈效力不等
        #（血规 10①：机制造对了却只接了一个栈的调用点）。
        # 治法＝与 reconcile 的 #11(b) 同款：本地写完把**这些**清单推进沙箱。
        # ★推送面必须只含 A2 自己触达的路径★：本函数其余修复族（Java import/version/
        # symbol、goimports、cargo fix）都走 `_run_l1_command` **在沙箱里改**，那时沙箱
        # 副本比本地新——把本地旧副本推上去会**擦掉沙箱侧的修复**（比原缺陷更坏）。
        # 故此处单独收集 A2 路径，不复用 `paths`。
        _a2_paths: list[str] = []
        for lang, file_signal, _fn in adapters:
            stack_key = _sib_stack.get(lang)
            if stack_key and eligible(lang, file_signal):
                try:
                    _n, _fs = repair_from_sibling_manifests(
                        project_path, build_output, mods, stack_key)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[L1.2.1·repair] A2 %s sibling-dep 异常(跳过): %s",
                                 stack_key, exc)
                    continue
                _accum((_n, _fs))
                for _f in _fs:
                    if _f and _f not in _a2_paths:
                        _a2_paths.append(_f)
        if _a2_paths:
            try:
                _pushed = _push_manifests_to_sandbox(project_path, _a2_paths)
                if _pushed:
                    logger.info(
                        "[L1.2.1·repair] A2 sibling-dep 注入的 %d 份清单已推进沙箱"
                        "（构建沙箱优先，不推则本地注入对构建不可见）", _pushed)
                else:
                    # 无活跃沙箱（本地模式）＝构建直接读 project_path，本就可见，非降级。
                    # 有沙箱但推送失败 → _push_manifests_to_sandbox 内部已告警；此处
                    # 不吞成静默：构建随后会因缺依赖继续失败，交既有失败分类处理。
                    logger.debug("[L1.2.1·repair] A2 清单未推送（本地模式或推送未成功）")
            except Exception as exc:  # noqa: BLE001 — 推送异常不致命（构建仍会如实失败）
                logger.warning("[L1.2.1·repair] A2 清单推进沙箱异常（本轮注入对构建"
                               "可能不可见）: %s", exc)
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
# ★#29-2 W-7（编号统一见下）★ 签名命令改为**平台中立**：不再用 `stat`，只用 POSIX `cksum`。
#
# 编号统一：本机制历史上有三个标签——commit 055883a 的 message 叫它 W-2、本处代码注释叫
# W-5，而同一 commit message 里的 W-5 是另一件事。以 29 号文的 **W-7** 为准，旧标签仅作
# grep 线索保留（三个标签指同一处＝历史包袱，不再新增）。
#
# ★原缺陷（v0.9.74 引入的回归，已本机等价实验坐实）★
# 原实现按 `sys.platform`（**本机**）选 stat 语法：darwin → BSD `stat -f`，否则 GNU `stat -c`。
# 但 `_run_check_split` 是**沙箱优先**的，沙箱是 Linux ⇒ 开发机 macOS 上跑出来的命令带 BSD
# 语法却在 Linux 里执行 ⇒ `stat` 报错被 `2>/dev/null` 吞掉 ⇒ xargs 无输出 ⇒ 空输入喂给
# `cksum` ⇒ 签名恒为 **`4294967295 0`**（实测：改文件前后一字不变）⇒ 缓存**永不失效**。
# 关键咬合点：下面"签名拿不到 → 不缓存"的兜底只认**空串**，而空输入的 cksum 是**非空常量**
# ⇒ 兜底被完全绕过（这正是"降级路径必须机读可辨"那条硬检查的反例：失败态与成功态同形）。
# 危害面：5 个消费者全是 JVM 全树符号扫描且位于 repair 收敛循环内 —— `_attempt_symbol_repair`
# 会拿**过期频次表**做改名决策，#114 反震荡判据的前提（频次表反映当前树）随之失效。
#
# 治法不是"按执行环境选 stat 语法"（那还得判有没有沙箱，判错就复发同一类），而是**把平台
# 依赖整条删掉**：`cksum` 是 POSIX 工具，GNU/BSD 都有且输出同构（每文件一行"校验和 大小
# 文件名"），故 `find -print0 | xargs -0 cksum | sort | cksum` 在两侧行为一致。
# 本机实测它能区分：内容改动 / 文件删除 / 改名（后两者是 stat 版也能覆盖的面，不退化）。
# 代价＝读一遍源文件字节；相对它保护的 60-120s 全树 grep 可忽略。
_SCAN_CACHE: dict[tuple[str, str], tuple[str, tuple[int, str, str]]] = {}
# 空输入的 `cksum` 输出（GNU/BSD 同值）。它同时是"树里没有源文件"与"命令整条失败"的取值
# ⇒ 两者机读不可辨 ⇒ 一律当【无证据】处理，绝不据它缓存（fail-closed 方向：宁可多扫一次，
# 绝不返回可能陈旧的符号表）。
_EMPTY_CKSUM = "4294967295 0"
_SCAN_SIG_CMD = (
    "find . \\( -name '*.java' -o -name '*.kt' -o -name '*.scala' \\) -print0 2>/dev/null "
    "| xargs -0 cksum 2>/dev/null | sort | cksum"
)


def _scan_sig_command() -> str:
    """签名命令。平台中立 ⇒ 无分支：本机与沙箱用同一条（原按 sys.platform 分叉正是 W-7 根因）。"""
    return _SCAN_SIG_CMD


def _cached_scan(scan_cmd: str, project_path: str, timeout: int = 60) -> tuple[int, str, str]:
    """带文件状态签名失效的 _run_check_split 包装，专给只读全树符号/包扫描省重复预算（A7）。"""
    try:
        _sec, sig_out, _e = _run_check_split(_scan_sig_command(), project_path, timeout=min(timeout, 15))
        sig = (sig_out or "").strip()
    except Exception:  # noqa: BLE001
        sig = ""  # 签名拿不到 → 不缓存，照常扫描（安全兜底，绝不返回可能陈旧的结果）
    # ★W-7★ 空 cksum 与真失败同值 → 视为无签名。这一条必须与上面的 `sig = ""` 并列存在：
    # 异常路径给空串，而"命令成功但输出是空 cksum"走的是**正常返回**路径，两者来源不同。
    if sig == _EMPTY_CKSUM:
        # ★#29-2 对抗复核（hunter finding 3）★ 原为 `logger.debug` ⇒ **生产不可见**：
        # `config/settings.py` 默认 `log_level="INFO"`，且 `.env`/env_registry 无覆盖项
        # （两处已实测）。而这条分支同时意味着"签名命令可能整条失效"——正是 W-7 症状复发的
        # 唯一信号。降级本身是安全的（不缓存，不产错结果），但**诊断信号被吞**：排查
        # `_attempt_symbol_repair` 拿陈旧频次表误改名时无日志可查。故升为 WARNING。
        # 良性情形（树内真没有 JVM 源文件）也会打 —— 刻意接受：宁可对良性多打一条，
        # 也不让真故障静默（本仓「降级路径至少一次 WARNING」的纪律方向）。
        logger.warning(
            "[L1·A7] 全树签名为空 cksum(%s)——树内无 JVM 源文件或签名命令失效，两者不可辨"
            "→ 本次不缓存（宁可重扫，绝不用可能陈旧的符号表）: %s", sig, project_path)
        sig = ""
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



# ★X-H2 复核 HIGH-2★ 沙箱 `find` 的依赖树剪枝片段。**必须与本地兜底的
# `_SRC_EXCLUDE_DIRS_FOR_DERIVE` 同口径**——M-3 的整改当初只落在本地分支，而**生产＝沙箱**，
# 于是 `node_modules/**` 里深度≤3 的 `package.json`/`*.csproj` 仍会让 `has()` 判 True 且
# `_manifest_dir` 指进依赖树 ⇒ `cd node_modules/foo && dotnet build` ⇒ 127 → BLOCKED，
# 正是本批立项要杀的死循环。
_FIND_PRUNE = "\\( -name node_modules -o -name vendor -o -name third_party -o -name target -o -name build -o -name dist -o -name .git -o -name .venv -o -name venv -o -name __pycache__ -o -name .tox -o -name site-packages -o -name .mypy_cache -o -name .pytest_cache -o -name .eggs \\) -prune -o "


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
                f"find {remote} -maxdepth 3 {_FIND_PRUNE}"
                f"\\( {names} \\) -print -quit 2>/dev/null | head -1",
                timeout=20,
            )
            present = bool((cr.stdout or "").strip())
            _MANIFEST_PRESENT_CACHE[_key] = present
            return present
        except Exception:  # noqa: BLE001
            return False  # 异常不缓存：保守 False 且下次重探（与旧行为一致）
    # ★本地兜底必须与沙箱分支同口径（X-H1 实测发现两者不一致）★
    # 沙箱侧是 `find -maxdepth 3 \( -name a -o -name b \)`：**递归到深度 3** 且 `-name` **支持
    # glob**。原本地实现是 `os.path.isfile(root/m)`——只看工程根、且把 `*.csproj` 当字面名 ⇒
    #   · `tools/go.mod`（子目录清单）本地判 False、沙箱判 True；
    #   · `*.csproj`/`*.sln` 本地**永远**False ⇒ C# 整栈在本地路径零构建闸。
    # 两个环境对同一棵树给出不同答案＝测试在本地绿而生产另一套行为（本战役反复吃的形态）。
    _root = Path(project_path)

    def _ok(p: Path) -> bool:
        # ★复核 M-3★ 与 `_manifest_dir` 同口径排除依赖树/产物目录。两个探针若不一致，
        # 会出现 `has("*.csproj")` 从 `node_modules/**` 判 True、而 `_manifest_dir` 返 None ⇒
        # `at()` 退回根级命令 ⇒ `dotnet build` 在没有工程文件的根上跑 ⇒ 127 → BLOCKED，
        # 正是本批要治的死循环。
        if not p.is_file():
            return False
        try:
            rel = p.relative_to(_root)
        except ValueError:
            return False
        return not any(seg in _SRC_EXCLUDE_DIRS_FOR_DERIVE for seg in rel.parts)

    for m in manifests:
        if _ok(_root / m):                    # 根级（含字面名）
            return True
        if any(ch in m for ch in "*?["):
            if any(_ok(x) for x in _root.glob(m)):
                return True
        for _d in range(1, 3):                # 深度 2..3，与沙箱 maxdepth 3 对齐
            if any(_ok(x) for x in _root.glob("/".join(["*"] * _d) + "/" + m)):
                return True
    return False


# ── 基础设施/工具瞬时错误识别（A-P1-09）──
# Go/Rust/Java lint 旧实现"非0退出 + 任意 stderr 即 has_error"，把【无网拉依赖、工具缺失、
# 文件锁、磁盘满、系统资源】等瞬时基础设施/工具故障误判成"代码能力失败"→ 触发错误降级
# (换更弱模型/abandon)。修复：lint 输出命中下列【明确属基础设施/工具】的标记时，判 skip
# (非 error)。只收"明确非代码问题"的标记——通用编译错误(模型引错符号)仍算真错误，不放过。
_LINT_INFRA_MARKERS: tuple[str, ...] = (
    # ── X-H2 复核 HIGH-1：本批给 go/rust/npm 新增了**真跑测试**的面（改前一律 test_skipped
    # ＝通过），而这几类失败**不是代码能力问题**，原表一条都没覆盖（实测全判 CODE）：
    # ★命中面积必须先估★ 这些 node 错误码是**裸子串**，很容易误配：`enotfound` 会命中
    # Python 的 `ModuleNotFoundError`（小写后是 `modul·enotfound·error`）⇒ 把"缺内部模块"
    # 误判成 infra ⇒ X-C3 的 BLOCKED 归因整条失效（本仓自己的测试当场抓到）。
    # 故一律带 node 的实际前缀/上下文，不用裸码。
    "econnrefused ",                    # node 打的是 `connect ECONNREFUSED 1.2.3.4:5432`
    "econnrefused\n", "getaddrinfo enotfound", "eai_again",
    "but --offline was specified",      # cargo --offline 在冷沙箱（未预热）必失败
    "failed to launch the browser",     # playwright/puppeteer 缺 chromium
    "executable doesn't exist at",      # 同上（浏览器二进制不在镜像里）
    "no usable sandbox",                # chromium 沙箱权限
    "cannot find main module",          # go：命令在错目录跑（工具/布局问题，非代码错）
    "could not find `cargo.toml`",      # cargo：同上
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
    # DR-04-F3 治本：去掉裸 `": not found"` 子串——它与测试断言里 echo 的 `<X>: not found`
    # 同形，会把真实 verify/build 失败(worker 没产出，assert 打印 `artifact: not found`)误判成
    # infra 瞬时故障→BLOCKED transient 反复退避重试直到配额耗尽才 abandon，真因被掩盖。dash/sh
    # 报"命令缺失"的形态锚定 shell 前缀，改用 _SHELL_NOT_FOUND_RE 精确匹配(见下)。
    "command not found", "executable file not found",
    "is not recognized as an internal or external command",
)


# DR-04-F3：dash/sh/busybox 报"命令缺失"的形态是 `<shell>[: 行号]: <cmd>: not found`——必须锚定
# shell 名/绝对路径前缀，绝不用裸 `": not found"`（会命中 `artifact: not found` 这类断言 echo）。
_SHELL_NOT_FOUND_RE = re.compile(
    r"(?mi)^(?:/\S+|sh|bash|dash|ash|zsh|ksh|csh|tcsh|fish|busybox)"
    r"(?::\s*\d+)?:\s+\S.*?:\s*not found\s*$"
)


def _is_infra_failure(text: str) -> bool:
    """lint/编译输出是否为基础设施/工具瞬时故障(非代码能力问题)。"""
    if not text:
        return False
    low = text.lower()
    if any(mk in low for mk in _LINT_INFRA_MARKERS):
        return True
    # shell 缺命令(工具未装)——锚定前缀，不误命中断言 echo 的 `<X>: not found`
    return bool(_SHELL_NOT_FOUND_RE.search(text))


def _is_npm_test_without_script(test_cmd: str, project_path: str) -> bool:
    """W-7：harness 显式下发 `npm test` 但 package.json 没有 `scripts.test` 时，
    不应执行命令后拿 `Missing script:` 误判成 infra 故障，而要提前按 `test_skipped` 处理。"""
    if not test_cmd:
        return False
    cmd = test_cmd.strip()
    # 识别 `npm test ...`（含前缀 cd / npx 等暂不支持，但 harness 下发基本都是裸 npm test）
    if not cmd.startswith("npm test") and not cmd.startswith("npx npm test"):
        return False
    return not _npm_has_test_script(project_path)


# 构建/测试命令 → 该命令运行所【必需的工程描述文件】。缺这些文件时命令必然失败
# (如 mvn 无 pom.xml、npm 无 package.json)，应优雅跳过而非误判为产出不合格。
_BUILD_TOOL_MANIFESTS: dict[str, tuple[str, ...]] = {
    "mvn": ("pom.xml",),
    # ★X-H6★ wrapper 形态必须在表里：`./mvnw`/`./gradlew` 是**最常见**的 JVM 构建入口
    # （Spring Boot 生态默认），漏了它们等于对整类 wrapper 工程放行"明知必失败"的命令。
    "./mvnw": ("pom.xml",),
    "mvnw": ("pom.xml",),
    "gradle": ("build.gradle", "build.gradle.kts", "settings.gradle"),
    "./gradlew": ("build.gradle", "build.gradle.kts", "settings.gradle"),
    "gradlew": ("build.gradle", "build.gradle.kts", "settings.gradle"),
    "npm": ("package.json",),
    "yarn": ("package.json",),
    "pnpm": ("package.json",),
    "npx": ("package.json",),
    "bun": ("package.json",),
    "tsc": ("tsconfig.json", "package.json"),
    "go": ("go.mod",),
    "cargo": ("Cargo.toml",),
    # 以下是 X-H6 点名的"漏了半个世界"——空项目下原先全 applicable=True ⇒ 明知必失败仍执行
    # → 127 → BLOCKED 死循环（与 X-C1/X-C2 同族）。
    "dotnet": ("*.csproj", "*.sln", "*.fsproj"),
    "msbuild": ("*.csproj", "*.sln"),
    "sbt": ("build.sbt",),
    "composer": ("composer.json",),
    "bundle": ("Gemfile",),
    "rake": ("Rakefile", "Gemfile"),
    "mix": ("mix.exs",),
    "poetry": ("pyproject.toml",),
    "pipenv": ("Pipfile",),
    "flutter": ("pubspec.yaml",),
    "dart": ("pubspec.yaml",),
    "make": ("Makefile", "makefile", "GNUmakefile"),
    "cmake": ("CMakeLists.txt",),
    "swift": ("Package.swift",),
}

# ★X-H6 的另一半★ 这些工具**本就不需要工程清单**（直接跑解释器/测试器），故"未知工具放行"
# 对它们是**正确**的，不能一并 fail-closed。显式登记，好让"真未知"与"已知无需清单"可分——
# 否则要么误杀 python/pytest，要么继续对 dotnet/sbt 放行（原实现选了后者）。
_NO_MANIFEST_TOOLS: frozenset[str] = frozenset({
    "python", "python3", "py", "pytest", "tox", "nox", "unittest",
    "sh", "bash", "zsh", "env", "true", "echo", "cd", "test",
    "javac", "kotlinc", "scalac", "rustc", "gcc", "g++", "clang", "clang++",
    "node", "deno", "ruby", "php", "perl", "elixir", "erl", "java",
    "grep", "ls", "cat", "find", "awk", "sed",
})


def _manifest_dir_for(mods: list[str], names: tuple[str, ...], project_path: str,
                      evidence: dict | None = None) -> str | None:
    """从**改动文件**向上找最近的清单目录（工程根相对；根返 `""`）；定不了返 None。

    ★复核 CRITICAL-1 的治法★ 原 `_manifest_dir` 按"全树最短清单路径"挑目录，**完全不看
    `modified` 在哪**。实测后果（沙箱分支，即生产路径）：
      monorepo `backend/pyproject.toml`，子任务改 `scripts/deploy.py`（真语法错）
      → `cd backend && compileall …` → **rc=0** ⇒ 改动文件根本没进编译面 ⇒ 静默假过。
    go 同型且锚点任意：`svc/go.mod` + `tools/go.mod`，改 `tools/broken.go` → 锚到 `svc`
    （按路径长度挑的）。python 改前**没有分支**（留 `build_skipped` 痕），go 改前在根上跑会
    **大声失败**（BLOCKED 可重试）—— 锚定把"响的失败"换成了"静默的通过"，方向反了。

    正确判据是 monorepo 的标准解析：**每个改动文件向上走，找最近的清单**。
      · 所有改动文件收敛到**同一个**清单目录 → 锚它（精确）
      · 改动跨多个清单 → 不猜：回退根级命令 + 落机读键（`evidence["spans"]`），让"闸只覆盖了
        一部分"这件事可被发现，而不是静默选一个
      · 改动文件之上一个清单都没有 → None（调用方退回根级，保持原行为）
    """
    dirs: set[str] = set()
    unresolved: list[str] = []
    for f in mods:
        rel = str(f).replace("\\", "/").lstrip("./").lstrip("/")
        if not rel:
            continue
        parts = rel.split("/")[:-1]           # 去掉文件名，只留目录段
        found: str | None = None
        for i in range(len(parts), -1, -1):   # 由深到浅：最近的清单胜
            d = "/".join(parts[:i])
            if any(_project_file_exists(f"{d}/{n}" if d else n, project_path)
                   for n in names if "*" not in n):
                found = d
                break
            # glob 形态（`*.csproj`）：本地/沙箱都得列目录，走 _manifest_dir 的单目录探测
            if any("*" in n for n in names) and _dir_has_glob(d, names, project_path):
                found = d
                break
        if found is None:
            unresolved.append(rel)
        else:
            dirs.add(found)
    if isinstance(evidence, dict):
        if unresolved:
            evidence["uncovered"] = sorted(unresolved)[:8]
        if len(dirs) > 1:
            evidence["spans"] = sorted(dirs)[:8]
    if len(dirs) == 1:
        return next(iter(dirs))
    if len(dirs) > 1:
        return ""                              # 跨多个清单 → 根级（并已落 spans 账）
    return None


def _dir_has_glob(d: str, names: tuple[str, ...], project_path: str) -> bool:
    """指定目录下是否有匹配 glob 的清单（`*.csproj`/`*.sln`）。沙箱优先。"""
    globs = [n for n in names if "*" in n]
    if not globs:
        return False
    ctx = _sandbox_ctx()
    if ctx is not None:
        sandbox, manager, remote = ctx
        target = f"{remote}/{d}" if d else remote
        pat = " -o ".join(f"-name {shlex.quote(g)}" for g in globs)
        try:
            cr = manager.run_command(
                sandbox,
                f"find {shlex.quote(target)} -maxdepth 1 \\( {pat} \\) -print -quit "
                f"2>/dev/null | head -1", timeout=20)
            return bool((cr.stdout or "").strip())
        except Exception:  # noqa: BLE001
            return False
    root = Path(project_path) / d if d else Path(project_path)
    return any(p.is_file() for g in globs for p in root.glob(g))


def _manifest_dir(names: tuple[str, ...], project_path: str) -> str | None:
    """清单所在**目录**（工程根相对；根返 `""`）；找不到返 None。沙箱优先。

    ★为什么需要它（X-H1 跨栈污染）★ `_manifest_present` 只回答"深度≤3 内**有没有**"，而派生的
    命令要在**某个目录**里跑。实测污染形态：Maven 单体里有个 `tools/go.mod`，子任务只改了
    `.go` ⇒ 旧实现下发 `go build ./...` 并在**工程根**执行 ⇒ 根没有 go.mod，命令必失败。
    有了目录就能 `cd tools && go build ./...`，或在只允许根级时如实放弃。
    """
    ctx = _sandbox_ctx()
    if ctx is not None:
        sandbox, manager, remote = ctx
        _names = " -o ".join(f"-name {shlex.quote(n)}" for n in names)
        try:
            cr = manager.run_command(
                sandbox,
                f"cd {shlex.quote(remote)} && find . -maxdepth 3 {_FIND_PRUNE}"
                f"\\( {_names} \\) "
                f"-print 2>/dev/null | sed 's|^\\./||' | "
                f"awk '{{print gsub(/\\//,\"/\"), length($0), $0}}' | sort -n -k1,1 -k2,2 | "
                f"head -1 | cut -d' ' -f3-",
                timeout=20)
            rel = (cr.stdout or "").strip()
            if not rel:
                return None
            return rel.rsplit("/", 1)[0] if "/" in rel else ""
        except Exception:  # noqa: BLE001 — 探测失败当找不到（不猜目录）
            return None
    root = Path(project_path)
    best: str | None = None
    for n in names:
        for p in sorted(root.rglob(n)):
            try:
                rel = "/".join(p.relative_to(root).parts)
            except ValueError:
                continue
            if any(seg in _SRC_EXCLUDE_DIRS_FOR_DERIVE for seg in rel.split("/")):
                continue
            if best is None or (rel.count("/"), len(rel)) < (best.count("/"), len(best)):
                best = rel
    if best is None:
        return None
    return best.rsplit("/", 1)[0] if "/" in best else ""


# 派生构建命令时忽略的目录（依赖树/产物里的清单不是本工程的构建入口）
_SRC_EXCLUDE_DIRS_FOR_DERIVE = frozenset({
    "node_modules", "vendor", "third_party", "target", "build", "dist",
    ".git", ".venv", "venv", "__pycache__", "testdata", "example", "examples",
})


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
            # #37：`classes` 编译主源集全部 JVM 语言(Kotlin/Scala/Java)——旧 compileJava 对
            # .kt/.scala 编译零源→假过，或任务不存在→冤杀。classes 由任一 JVM 语言插件创建。
            _drv = _build_driver_for("gradle")
            _cmd = _drv.build_cmd if _drv and _drv.build_cmd else (
                "./gradlew -q classes" if has("gradlew") else "gradle -q classes")
            # ★W-4★ Gradle 子任务按改动模块收窄：子目录 build.gradle 时用 `-p <dir>`，
            # 避免整项目 classes 把无关模块错误归到本子任务。
            _d = _manifest_dir_for(
                mods, ("build.gradle", "build.gradle.kts",
                       "settings.gradle", "settings.gradle.kts"), project_path)
            if _d and _SAFE_REL_DIR_RE.match(_d):
                _cmd = re.sub(r"^(./gradlew|gradle)(\s+)",
                              rf"\1 -p {shlex.quote(_d)}\2", _cmd)
            return _cmd
        if build == "maven" or has("pom.xml"):
            # ★BRAIN-001/W-3★ 命令字面量从 STACK_SPEC 取；`_scope_maven_command` 负责按
            # modified 收窄到 -pl <module> -am。
            _drv = _build_driver_for("maven")
            if _drv and _drv.build_cmd:
                return _drv.build_cmd
            return "mvn -q compile"  # _scope_maven_command 据 modified 收窄到 -pl <module> -am
    def _per_file(cmd: str, exts: tuple[str, ...]) -> str:
        files = [f for f in mods if f.endswith(exts)]
        if not files:
            return ""
        quoted = " ".join(shlex.quote(f) for f in files[:100])
        return f"for f in {quoted}; do {cmd} \"$f\" || exit 1; done"

    def at(names: tuple[str, ...], cmd: str) -> str:
        """把命令锚到**清单所在目录**（X-H1 跨栈污染治法）。

        根级清单 → 原样返回；子目录清单 → `cd <dir> && <cmd>`；找不到清单 → `''`（不臆造）。
        实测污染形态：Maven 单体里有个 `tools/go.mod` 且只改了 `.go` ⇒ 旧实现在**工程根**
        下发 `go build ./...` ⇒ 根无 go.mod，必失败。
        """
        # ★复核 CRITICAL-1★ 锚点必须由 **modified** 反查（改动文件向上找最近清单），
        # 不能用"全树最短清单路径"——后者会让闸编译一棵与改动无关的子树并 rc=0（静默假过）。
        _ev: dict = {}
        d = _manifest_dir_for(mods, names, project_path, evidence=_ev)
        if _ev.get("spans"):
            logger.warning(
                "[L1] 改动跨多个 %s 清单目录（%s）→ 不猜锚点，按工程根跑；闸可能只覆盖一部分",
                names[0], _ev["spans"])
        if _ev.get("uncovered"):
            # ★锚点必须**覆盖住改动文件**，否则闸跑了也是白跑（rc=0 但改动没进编译面）★
            # 有文件落在锚点之外时退到**工程根**——根级命令覆盖面最大（如 compileall 会连
            # `scripts/` 一起编），而锚到某个子目录反而把这些文件排除掉。
            logger.warning(
                "[L1] 这些改动文件不在 %s 清单目录之下（%s）→ 构建闸退到工程根跑，"
                "避免锚到子目录把它们排除在编译面之外", names[0], _ev["uncovered"])
            return cmd
        if d is None:
            # 改动文件之上一个清单都没有 → 退回全树探测（保持原行为），但已落上面那条账
            d = _manifest_dir(names, project_path)
        if not d:
            # ★`None`（定位不出）与 `""`（就在根）都走根级命令★
            # 调用点已经用 `has()` 确认清单**存在**；若 `_manifest_dir` 又说定位不出，那是两个
            # 探针不一致（探测瞬时失败/mock 只打了一个）。此时退回"按根跑"＝**原行为**，
            # 而不是返 `''`：后者会把闸整块关掉（"跳过＝通过"，正是本批在治的假过），
            # 代价不对称——错锚目录只是 127 → BLOCKED 可重试，没闸是坏产物直接放行。
            # 只有**positively 知道**清单在子目录时才改锚，故污染治法不受影响。
            return cmd
        if not _SAFE_REL_DIR_RE.match(d):
            # 目录名来自工程树（外部输入）→ 形态不安全就不拼进命令（S-5 同源判据）
            logger.warning("[L1] 清单所在目录名形态不安全，放弃派生构建命令: %r", d)
            return ""
        return f"cd {shlex.quote(d)} && {cmd}"

    if ext(".go") and (build == "go" or has("go.mod", "go.work")):
        # N-2：`go.work` 多模块仓——`go build ./...` 在 work 根即可编译全部 use 模块
        _drv = _build_driver_for("go")
        _cmd = _drv.build_cmd if _drv and _drv.build_cmd else "go build ./..."
        if has("go.work"):
            return at(("go.work",), _cmd)
        return at(("go.mod",), _cmd)
    if ext(".rs") and (build == "cargo" or has("Cargo.toml")):
        _drv = _build_driver_for("cargo")
        _cmd = _drv.build_cmd if _drv and _drv.build_cmd else "cargo build -q"
        return at(("Cargo.toml",), _cmd)
    # X-H1：前端不能只认 `.ts/.tsx`+tsconfig —— npm 工程改 `.js/.jsx/.vue` 原先**零构建闸**。
    # 优先 tsc（有 tsconfig 时它是最强的确定性类型闸），否则退到 package.json 的 build 脚本。
    if ext(".ts", ".tsx") and has("tsconfig.json"):
        return at(("tsconfig.json",), "tsc --noEmit")
    if ext(".ts", ".tsx", ".js", ".jsx", ".vue", ".mjs", ".cjs"):
        if has("tsconfig.json"):
            return at(("tsconfig.json",), "tsc --noEmit")
        if _npm_has_build_script(project_path):
            return at(("package.json",), "npm run build --if-present")
    # N-4：python 原先**没有任何分支** ⇒ python 工程零构建闸（＝27 号文 V-C1 的 python 行）。
    # `compileall` 是唯一跨 python 工程通用且零依赖的确定性编译闸（语法/字节码级）；
    # 它不查 import（那由 L1.3 的 X-C3 第三调用点兜），但比"什么都不跑"强得多。
    if ext(".py") and (build in ("pip", "poetry", "uv", "python")
                       or has("pyproject.toml", "setup.py", "requirements.txt", "Pipfile")):
        # ★用户拍板：只编译**改动文件**★（与 PHP/Ruby 同口径）
        # 演进史值得留：最初是 `compileall -q .`（整树）→ 复核 M-2 指出它钻进 `.venv`
        # （实测一个 py2 语法文件就让整个闸 rc=1）→ 加 `-x` 排除表 → 复核又指出**排除表是
        # 补不完的黑名单**：linter/parser/formatter 工程会**刻意 ship 坏语法夹具**
        # （`tests/fixtures/bad_syntax.py`），而"刻意坏语法"的目录名没有通用约定，换个名就复发
        # （本仓纪律明确反对 denylist 式打补丁）。
        # ★只碰 worker 自己改的文件＝可证不误杀★：坏语法夹具/依赖树/`.tox`/`site-packages`
        # 全都不在面内，且不需要维护任何排除表。
        # 诚实边界：**跨文件的 import 错抓不到**——那本就不是 compileall 的能力
        # （`py_compile` 只做语法/字节码级），由 L1.3 的 X-C3 第三调用点（真 import 时的
        # `ModuleNotFoundError` 归因）承担。
        return _per_file("python3 -m compileall -q", (".py",))
    # X-H1：C#/PHP/Ruby/Elixir/Dart —— 原先全返 ''。只在**清单在场**时出命令（纪律 2 不臆造）。
    if ext(".cs") and has("*.csproj", "*.sln"):
        return at(("*.csproj", "*.sln"), "dotnet build --nologo -v q")
    # ★复核 C-1★ PHP/Ruby 原写法 `php -l $(git ls-files '*.php' | head -200)` 是**必然假过**，
    # 实测两个独立缺陷叠加：
    #   ① 沙箱 `/workspace` **不是 git 仓库**（`.git` 在 `_SRC_EXCLUDE_DIRS`、`git archive` 也不带）
    #      ⇒ `git ls-files` 失败、命令替换为空；
    #   ② `ruby -c` / `php -l` **零参数时读 stdin** ⇒ 空 stdin ⇒ 打印 `Syntax OK`、**退出 0**。
    #   ③ 更糟：`ruby -c a.rb b.rb` **只检查 a.rb**（实测 b.rb 有语法错仍 rc=0）——即便在 git 仓库里，
    #      `head -200` 也只查了一个文件。
    # 后果比改动前**更坏**：改动前 derive 返 `''` → 闸跳过 + 留 `build_skipped` 痕；改后
    # `build_command_derived` 置位、闸"跑了"、退出 0、**没有任何机读键说明什么都没查**
    # ⇒ worker 交任意破 PHP/Ruby 都能拿 L1 PASS（硬检查④）。
    # 治法：不依赖 git、不依赖"全项目枚举"，直接逐个检查**本子任务改动的那些文件**——
    # 那正是 L1 该管的范围，且确定性、有界、无截断（`|| exit 1` 保证任一失败即失败）。

    if ext(".php") and has("composer.json"):
        return _per_file("php -l", (".php",))
    if ext(".rb") and has("Gemfile"):
        return _per_file("ruby -c", (".rb",))
    if ext(".ex", ".exs") and has("mix.exs"):
        # ★复核 M-5★ 不加 `--warnings-as-errors`：既有工程一片 warning 会让闸每轮判死，且把
        # "代码质量"混进"能不能编译"（其余栈的 `go build`/`cargo build`/`dotnet build` 都不这么做）。
        return at(("mix.exs",), "mix compile")
    if ext(".dart") and has("pubspec.yaml"):
        # 同上：`dart analyze` 默认对 warning 也非零退出 → 显式关掉致命化
        return at(("pubspec.yaml",), "dart analyze --no-fatal-warnings")
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


# 包装前缀分两类，**后续词元的语义不同**（复核 M-7 整改时踩过）：
#   · `_CMD_PATH_ARG_PREFIX`：下一个非选项词元是**路径/赋值**，不是命令（`cd sub`、`env A=B`）
#   · `_CMD_EXEC_PREFIX`：下一个非选项词元**就是命令**（`sh -c "mvn …"`、`sudo mvn`）
# 混为一谈会把 `sh -c 'mvn -q compile'` 里的 `mvn` 当成实参跳过 ⇒ 落"未知工具放行"。
_CMD_PATH_ARG_PREFIX = frozenset({"cd", "pushd", "env"})
_CMD_EXEC_PREFIX = frozenset({"sh", "bash", "zsh", "dash", "sudo", "time", "nice",
                              "nohup", "xargs", "command"})
_CMD_PREFIX_NOISE = _CMD_PATH_ARG_PREFIX | _CMD_EXEC_PREFIX
# ★复核 MED-1★ shell 控制结构不是"工具"。本批自产的 PHP/Ruby 命令形如
# `for f in a.php; do php -l "$f" || exit 1; done` ⇒ 词元解析会返 `for` ⇒ 不在
# `_NO_MANIFEST_TOOLS` 里 ⇒ **每个 PHP/Ruby 子任务都刷一条"请把 `for` 登记进表"的 WARNING**，
# 污染的正是本批新建的那条告警通道（自伤）。
_SHELL_KEYWORDS = frozenset({"for", "while", "until", "if", "case", "do", "done",
                             "then", "fi", "esac", "in", "select", "function", "{", "}"})


def _effective_tool_token(tokens: list[str]) -> str:
    """跳过 shell 包装前缀/控制结构，取**真正的构建工具**词元。

    ★复核 M-7★ 原实现直接用 `tokens[0]`，而 `cd`/`sh`/`env` 都在"无需清单"白名单里 ⇒
    实测 `cd sub && mvn -q compile` / `sh -c "mvn -q compile"` 在空项目上都判 applicable=True
    ⇒ X-H6 那道闸被一个前缀绕过。而本批的 `at()` 自己**会产出** `cd <dir> && <cmd>`，属自伤。

    ★复核 MED-1/2/3 的三处整改★
    1. shell 控制结构（`for`/`do`/`if`…）不是工具：本批自产的 PHP/Ruby 命令是 `for f in …; do
       php -l …; done`，原实现返 `for` ⇒ 每个 PHP/Ruby 子任务刷一条"请登记 `for`"的 WARNING，
       污染本批自己新建的告警通道。
    2. **不再"宁选表里有的词元"**：原实现在 `cd <arg>` 之后若发现 `<arg>` 恰在工具表里就返它 ⇒
       实测 `cd mvn && npm ci` → tool=`mvn`（目录名撞工具名）⇒ node 工程无 pom ⇒ 判不适用 ⇒
       `test_skipped`＝跳过即通过（fail-open）。位置真相优先：`cd` 的实参永远是路径，不是工具。
    3. `&&`/`;`/`|` 紧贴词元时（`cd sub&&dotnet build`）先按分隔符切开——原实现只按空白 split，
       实测得到 `build` ⇒ 落"未知工具放行"。
    """
    import re as _re

    # 先把整条命令重新拆一遍：既拆引号（`sh -c "mvn …"`），也拆紧贴的连接符
    try:
        raw = " ".join(tokens)
        raw = _re.sub(r"(&&|\|\||;|\|)", r" \1 ", raw)
        flat = shlex.split(raw)
        # `sh -c "mvn -q compile"`：shlex 把引号里的整条命令当**一个**词元 ⇒ 必须再拆一层，
        # 否则返回的"工具名"是 `mvn -q compile` 这种带空格的串，查表必然落空。
        _re2: list[str] = []
        for _x in flat:
            _re2.extend(_x.split() if " " in _x else [_x])
        flat = _re2
    except ValueError:
        flat = list(tokens)
    if not flat:
        return tokens[0] if tokens else ""

    skip_arg = False
    in_loop_head = False
    for tok in flat:
        t = tok.strip().strip("\"'")
        if not t or t in ("&&", "||", ";", "|", "-c", "-lc", "--", "-l"):
            continue
        base = t.rsplit("/", 1)[-1]
        if base in ("for", "while", "until", "select"):
            # `for f in a.php b.php; do <cmd> …` —— 循环头里的**变量名与列表**都不是工具
            # （原实现只跳 `for`，于是返回了循环变量 `f`）。跳到 `do` 之后再找真命令。
            in_loop_head = True
            skip_arg = False
            continue
        if in_loop_head:
            if base == "do":
                in_loop_head = False
            continue
        if base in _SHELL_KEYWORDS:
            skip_arg = False
            continue
        if base in _CMD_EXEC_PREFIX:
            # `sh -c "mvn …"` / `sudo mvn`：后面**就是命令**，不跳
            continue
        if base in _CMD_PATH_ARG_PREFIX:
            skip_arg = True          # `cd <dir>` / `env A=B` 的下一个词是路径/赋值
            continue
        if skip_arg:
            # ★位置真相优先★ `cd` 的实参永远是路径（哪怕它叫 `mvn`），绝不当工具
            if not t.startswith("-"):
                skip_arg = False
            continue
        if "=" in t and not t.startswith("-"):
            continue                 # `FOO=1 cmd` 的赋值段
        return t
    return flat[0]


def _build_cmd_applicable(command: str, project_path: str) -> bool:
    """判断 build/test 命令的工具链工程文件是否存在(沙箱优先)。

    缺工程文件(mvn 无 pom / npm 无 package.json)时命令必失败，此时应跳过该闸门，
    不能把"工具不适用"误判成"产出不合格"。返回 True=可执行；False=应跳过。
    """
    tokens = command.strip().split()
    if not tokens:
        return False
    tool = _effective_tool_token(tokens)
    manifests = _BUILD_TOOL_MANIFESTS.get(tool)
    if not manifests:
        # ★X-H6★ 未知工具仍放行（`python`/`pytest` 这类本就不需要清单，fail-closed 会误杀整类），
        # 但**必须可观测**：原实现连一行日志都没有，于是 `dotnet build`/`sbt compile` 在空项目上
        # 被判 applicable → 真去跑 → 127 → BLOCKED → 每轮撞同一个"命令不存在"（硬检查④：
        # 降级路径至少一次 WARNING）。已知无需清单的工具（`_NO_MANIFEST_TOOLS`）不响，
        # 免得把正常路径刷成噪声；**真未知**的响一次，好让"表该补了"这件事被看见。
        _base = tool.rsplit("/", 1)[-1].lstrip("./")
        if _base not in _NO_MANIFEST_TOOLS:
            logger.warning(
                "[L1] 构建/测试命令的工具 %r 不在清单表里（_BUILD_TOOL_MANIFESTS）也不在"
                "『无需清单』白名单里 → 放行执行。若该工具其实需要工程清单，缺清单时会 127 → "
                "BLOCKED 空转：请把它登记进表（X-H6）: %s", tool, command[:120])
        return True
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
            f"find {remote} -maxdepth 3 {_FIND_PRUNE}"
                f"\\( {names} \\) -print -quit 2>/dev/null | head -1",
            timeout=20,
        )
        return bool((cr.stdout or "").strip())
    # ★复核 HIGH-3★ 本地兜底原是 `any(root.rglob(m))`：**无深度上限、无排除表**，与同文件
    # `_manifest_present` 刚对齐的沙箱口径（maxdepth 3 + 剪依赖树）**反向分叉**。实测后果：
    #   · 深度 7 的 `pom.xml` → `_manifest_present`=False（对）而这里 True ⇒ 判适用 → 在根上跑
    #     `mvn` → 127 → BLOCKED；
    #   · `node_modules/**/Vendored.csproj` → 这里 True ⇒ harness 的 `dotnet build` 照跑 → 127。
    # 即本批立项要杀的死循环，被漏掉的这一个调用点原样保留（硬检查①：数调用点，一个不落）。
    # 直接复用 `_manifest_present` —— 一份实现，不可能再分叉。
    return _manifest_present(tuple(manifests), project_path)



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


def _created_files_in_diff(diff: str) -> set[str]:
    """#31-P1：从 unified diff 解析【新建文件】集（`new file mode` / `--- /dev/null` 标记）。

    栈中立纯文本；复用 split_diff_by_file 的分段口径（与 apply 落盘同源）。修改形态
    （无新建标记）不计入——create_file 若以 modify 出现说明文件本就存在，由调用方
    on-disk 探测豁免（modify 的前提即文件在盘）。解析异常 → 空集（fail-open：调用方
    据空集配合 on-disk/exempt，绝不因解析失败误判遗漏）。"""
    created: set[str] = set()
    try:
        from swarm.project.diff_apply import split_diff_by_file
        for files, text in split_diff_by_file(diff or ""):
            if "new file mode" in text or "--- /dev/null" in text:
                for f in files:
                    if f:
                        created.add(f)
    except Exception:  # noqa: BLE001 — 解析异常 fail-open（见 docstring）
        return set()
    return created


def missing_created_files(
    create_files: list[str] | None,
    diff: str,
    *,
    exists: "callable | None" = None,
    exempt: set[str] | None = None,
) -> list[str]:
    """#31-P1：子任务【必建文件】完整性核验——返回【确凿遗漏】的 create_files 子集。

    治本 #31（round46 st-38-1）：子任务声明产 N 个文件只产 M<N 个，缺的类【无本地
    引用】→ 本地 compile 不炸 → L1 假绿 → 假 DONE → 下游连坐。既有 empty_diff 闸只抓
    "整子任务零产出"（all-or-nothing），抓不住"非空 diff 但缺部分 create_file"。本函数补刀。

    ★只核验 create_files（语义=必建，types.py:121），永不核验 writable★（语义=可改非
    义务，盲核验会误杀合法未改——#31 原始担忧）。

    遗漏判据（fail-closed 仅在确凿时）：某 create_file
      ① 不在本轮 diff 新建集（_created_files_in_diff），且
      ② 不在白名单 exempt（H1 权威模板落盘 / 确定性修复触达路径，wiring 提供），且
      ③ exists(rel) 探测为【不在盘】（兄弟已产/收尾器孤儿/基线已产 → 在盘即豁免）。

    fail-open 铁律（同 baseline_lombok_present）：create_files 空 / exists 未提供 /
    exists 探测抛异常 → 该文件【不判遗漏】。只有 exists 明确返回 False（确凿不在盘）
    且 ①② 均不豁免时才计入遗漏。路径匹配复用 _scope_match（路径段对齐 + 容忍仓库根
    前缀）。纯函数、栈中立、可单测。
    """
    creates = [str(f) for f in (create_files or []) if str(f).strip()]
    if not creates:
        return []                      # fail-open：无必建声明
    created = _created_files_in_diff(diff or "")
    exempt_set = {str(e) for e in (exempt or []) if str(e).strip()}
    missing: list[str] = []
    for cf in creates:
        # ① 本轮 diff 已新建该文件
        if any(_scope_match(df, cf) for df in created):
            continue
        # ② 白名单豁免（H1 模板 / repaired；双向 _scope_match 容忍根前缀写法差异）
        if any(_scope_match(e, cf) or _scope_match(cf, e) for e in exempt_set):
            continue
        # ③ 磁盘已存在（兄弟/收尾器孤儿/基线已产）——on-disk 即豁免
        if exists is None:
            continue                   # fail-open：无探测器 → 不判遗漏
        try:
            if exists(cf):
                continue
        except Exception:  # noqa: BLE001 — 探测异常 fail-open：绝不因探测失败误杀
            continue
        missing.append(cf)             # 确凿：diff 无 + 非豁免 + 盘上无
    return missing


# ════════════════ #31-P2：声明依赖坐标完整性（栈中立 verifier registry）════════════════
#
# 治本 #31/#35（round65e13/round65e2 实锤）：scaffold 子任务的 contract["dependencies"] 声明
# 模块 manifest【必须声明】的坐标（同源已剪枝，见 contract_utils.resolve_scaffold_artifacts），但
# worker 产出的 manifest 实际缺声明 → 下游兄弟构建期读不出 → 连坐。create_files 闸只核验文件在，
# 核验不到"文件在但缺声明坐标"。本组函数补刀。
#
# ★栈中立★：manifest basename → verifier；verifier 是【stack-特化 LEAF】（解析 pom vs package.json
# vs go.mod 天生不同），但 dispatch 层（registry）中立，未知栈 → 无 verifier → 跳（fail-open no-op）。
# ★匹配按尾名★：artifactId / package-name / module-path，忽略 group + version（免疫 BOM 受管 /
# 版本范围 / ${project.version} / workspace:* → 绝不因版本写法差异误杀）。


def _maven_declared_artifact_ids(text: str) -> set[str]:
    """pom 的【direct 运行时依赖】artifactId 集（复用 workspace_manifest._maven_direct_deps，
    已排除 parent/dependencyManagement/build）。受管块里的坐标不算 direct 声明。"""
    from swarm.worker.workspace_manifest import _maven_direct_deps
    return {a for (_g, a, _v) in _maven_direct_deps(text or "") if a}


_GRADLE_DEP_RE = re.compile(
    r"""(?:implementation|api|compileOnly|compileOnlyApi|runtimeOnly|testImplementation|"""
    r"""testRuntimeOnly|annotationProcessor|kapt|ksp|classpath|developmentOnly)\s*"""
    r"""[\(\s]\s*['"]([^'"]+)['"]""")


def _gradle_declared_deps(text: str) -> set[str]:
    """build.gradle(.kts) 的依赖 artifactId 集（best-effort：配置关键字后引号内 g:a:v → 取 a）。"""
    out: set[str] = set()
    for coord in _GRADLE_DEP_RE.findall(text or ""):
        parts = [p for p in coord.split(":") if p]
        if len(parts) >= 2:
            out.add(parts[1])
        elif parts:
            out.add(parts[0])
    return out


def _npm_declared_deps(text: str) -> set[str]:
    """package.json 的依赖包名集（dependencies + dev/peer/optional）。JSON 解析异常 → 由调用方 fail-open。"""
    import json as _json
    data = _json.loads(text or "")
    if not isinstance(data, dict):
        return set()
    names: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        sect = data.get(key)
        if isinstance(sect, dict):
            names |= {str(n).strip() for n in sect if str(n).strip()}
    return names


_GO_REQUIRE_BLOCK_RE = re.compile(r"require\s*\((.*?)\)", re.S)
_GO_REQUIRE_LINE_RE = re.compile(r"(?m)^\s*require\s+(\S+)\s+\S+")


def _go_declared_modules(text: str) -> set[str]:
    """go.mod 的 require 模块路径集（块形态 + 单行形态）。只收形似模块路径（含 / 或 .）的
    token——排除块开括号 `(` 等噪声（复核 LOW：单行正则会误捕 `require (` 的 `(`）。"""
    out: set[str] = set()
    t = text or ""
    for blk in _GO_REQUIRE_BLOCK_RE.findall(t):
        for line in blk.splitlines():
            s = line.strip()
            if not s or s.startswith("//"):
                continue
            parts = s.split()
            if parts and ("/" in parts[0] or "." in parts[0]):
                out.add(parts[0])
    for m in _GO_REQUIRE_LINE_RE.finditer(t):
        g = m.group(1)
        if "/" in g or "." in g:
            out.add(g)
    return out


def _manifest_complete(backend: str, text: str) -> bool:
    """结构完整性哨兵（治复核 HIGH：防【截断但非空】的 manifest 被当"零依赖"误杀）。

    正则型 verifier（maven/go）依赖【匹配到闭合标记】才产出坐标；若文本在开/闭标记之间被截断
    （沙箱 cat 输出被 buffer 截、传输中断而进程仍 exit 0），findall 返回空≠"真无依赖"，会把契约
    全部坐标误判缺失→冤杀合法产出（Phase1 F1b 同类，换个入口复发）。故【只有结构完整才有资格
    断言某坐标缺失】；不完整（截断/畸形）→ 视作读不到 → 跳（fail-open）。
    只做廉价栈特化终止符/括号平衡校验，宁可放过截断也绝不误杀。"""
    t = text or ""
    if backend == "maven":
        return bool(re.search(r"</project\s*>", t))          # pom 必闭合根标签
    if backend == "go":
        # go.mod 必有 module 声明；require 块括号平衡（截断在块中 → 左多于右 → 不完整）
        return ("module " in t) and (t.count("(") <= t.count(")"))
    if backend == "cargo":
        return ("[package]" in t) or ("[dependencies]" in t)  # 真 Cargo.toml 必有 [package]
    # npm：json.loads 自身校验截断（抛异常→上层 fail-open 跳）；gradle：逐行弹性解析，无可靠
    # 终止符，截断前的条目仍识别（截断后条目若被契约要求会误判——2b 接 gradle scaffold 前补测）。
    return True


def _cargo_declared_deps(text: str) -> set[str]:
    """Cargo.toml 的依赖 crate 名集（[dependencies]/[dev-dependencies]/[build-dependencies] 表键
    + [dependencies.foo] 子表名）。"""
    out: set[str] = set()
    in_deps = False
    for raw in (text or "").splitlines():
        s = raw.strip()
        if s.startswith("["):
            sub = re.match(r"\[(?:dev-|build-)?dependencies\.([A-Za-z0-9_\-]+)\]", s)
            if sub:
                out.add(sub.group(1))
                in_deps = False
                continue
            in_deps = s in ("[dependencies]", "[dev-dependencies]", "[build-dependencies]")
            continue
        if in_deps and "=" in s and not s.startswith("#"):
            name = s.split("=", 1)[0].strip().strip('"').strip("'")
            if name:
                out.add(name)
    return out


# manifest basename → (backend, verifier)。stack-特化 LEAF 经中立 registry 分派；未知栈 → miss → 跳。
_DEP_VERIFIER_BY_MANIFEST: dict[str, tuple[str, "callable"]] = {
    "pom.xml": ("maven", _maven_declared_artifact_ids),
    "build.gradle": ("gradle", _gradle_declared_deps),
    "build.gradle.kts": ("gradle", _gradle_declared_deps),
    "package.json": ("npm", _npm_declared_deps),
    "go.mod": ("go", _go_declared_modules),
    "Cargo.toml": ("cargo", _cargo_declared_deps),
}


def _coord_name_for(backend: str, spec: str) -> str:
    """契约坐标 → 匹配用【尾名】（与 verifier 返回的名空间对齐，忽略 group + version）。
    maven/gradle: 'g:a[:v]' → a（裸名 → 名本身）；npm/go/cargo: 名即坐标本身（npm 保 @scope/pkg，
    go 保完整 module path，cargo 保 crate 名）。"""
    s = str(spec).strip()
    if backend in ("maven", "gradle"):
        parts = [p for p in s.split(":") if p]
        if len(parts) >= 2:
            return parts[1]
        return parts[0] if parts else ""
    return s


def _manifest_belongs_to_module(manifest_rel: str, module: str) -> bool:
    """manifest 是否属于本 contract entry 的 module（R1：绝不跨模块/跨子任务核验）。
    module 空 → True（scaffold 子任务 1:1，无从区分即放行给下方读/解析闸把关）。"""
    m = str(manifest_rel).replace("\\", "/").strip("/")
    mod = str(module).replace("\\", "/").strip("/")
    if not mod:
        return True
    d = m.rsplit("/", 1)[0] if "/" in m else ""
    return d == mod or d.endswith("/" + mod) or m.startswith(mod + "/")


def missing_declared_dependencies(
    contract_deps: "list | None",
    scope_manifests: "list[str] | None",
    *,
    read: "callable | None" = None,
    exempt: set[str] | None = None,
) -> list[dict]:
    """#31-P2：子任务【声明依赖坐标】完整性核验——返回【确凿缺失】坐标 [{manifest,coordinate,module}]。

    事实源 = subtask.contract["dependencies"]（list[{module, artifacts:[spec...]}]，scaffold 注入器
    写入，同源已剪枝 → 永不索要模板没写的坐标，#35 陷阱天然免疫）。

    缺失判据（fail-closed 仅在确凿时）：某坐标 C（属 module M 的 manifest）
      ① manifest 在本子任务 scope 且 basename 有 verifier（未知栈 → 跳），且
      ② manifest 属于 M（_manifest_belongs_to_module；跨模块 → 跳），且
      ③ manifest 非白名单豁免（H1 模板/repaired），且
      ④ read(manifest) 返回【真文本】（None/空/纯空白 → 跳，绝不当"零依赖"误杀），且
      ⑤ verifier(text) 成功返回坐标名集，且 C 的尾名 ∉ 该集。

    fail-open 铁律：contract_deps 空 / read 未提供或抛异常 / 读不到/空 / 未知栈 / 跨模块 →
    一律【不判缺失】。★匹配忽略 group+version★（尾名比对）。纯函数、栈中立、可单测。
    """
    entries = [e for e in (contract_deps if isinstance(contract_deps, list) else [])
               if isinstance(e, dict) and [a for a in (e.get("artifacts") or []) if str(a).strip()]]
    if not entries or read is None:
        return []
    manifests = [str(m) for m in (scope_manifests or []) if str(m).strip()]
    if not manifests:
        return []
    exempt_set = {str(e) for e in (exempt or []) if str(e).strip()}

    def _exempted(man: str) -> bool:
        return any(_scope_match(e, man) or _scope_match(man, e) for e in exempt_set)

    # scope 里【有 verifier 且未豁免】的候选 manifest。用于 R58-1 改名容错：若全 scope 只有唯一
    # 候选、契约也只有唯一 entry，则按【scope 独占归属】直接信任（scaffold 恒 1:1），不靠猜标签。
    _known = [m for m in manifests
              if _DEP_VERIFIER_BY_MANIFEST.get(m.replace("\\", "/").rsplit("/", 1)[-1])
              and not _exempted(m)]
    _sole_pair = (len(entries) == 1 and len(_known) == 1)

    missing: list[dict] = []
    for entry in entries:
        module = str(entry.get("module") or "").strip()
        # 复核 HIGH：有【物理目录 dir】（injector 写的 ground truth）→ 严格按物理目录归属
        # （R58-1 改名下正确，且 dir 不匹配就是真不匹配，绝不被 sole_pair 覆盖）；无 dir
        # （老 checkpoint）→ 标签匹配，且 sole_pair 时按 scope 独占归属信任（改名容错）。
        _dir = str(entry.get("dir") or "").strip()
        target = _dir or module
        arts = [str(a).strip() for a in (entry.get("artifacts") or []) if str(a).strip()]
        for man in manifests:
            base = man.replace("\\", "/").rsplit("/", 1)[-1]
            spec = _DEP_VERIFIER_BY_MANIFEST.get(base)
            if not spec:
                continue                                   # 未知栈 → 跳（fail-open）
            if _exempted(man):
                continue                                   # 白名单豁免
            # 归属判定：有 dir → 严格物理匹配；无 dir → 唯一配对信任 or 标签匹配
            if _dir:
                if not _manifest_belongs_to_module(man, _dir):
                    continue                               # R1：物理目录确不匹配 → 跳
            elif not (_sole_pair or _manifest_belongs_to_module(man, target)):
                continue                                   # R1：确非本模块 → 跳
            backend, verifier = spec
            try:
                text = read(man)
            except Exception:  # noqa: BLE001 — 读异常 fail-open：绝不因探测失败误杀
                continue
            if not text or not str(text).strip():
                continue                                   # 读不到/空 → 跳（不当零依赖）
            if not _manifest_complete(backend, str(text)):
                continue                                   # 截断/畸形 → 跳（不当零依赖，防误杀）
            try:
                present = verifier(str(text))
            except Exception:  # noqa: BLE001 — 解析异常 fail-open
                continue
            if present is None:
                continue
            present_names = {str(p).strip() for p in present if str(p).strip()}
            for coord in arts:
                name = _coord_name_for(backend, coord)
                if name and name not in present_names:
                    missing.append({"manifest": man, "coordinate": coord, "module": module})
    return missing


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


def _compile_files(project_path: str, files: list[str], *, timeout: int = 60,
                   raw_out: dict | None = None) -> tuple[bool, str]:
    """返回 (ok, 人读消息)。

    `raw_out`（X-C3）：可选 out 参数，把**未截断**的工具原始输出放进 `raw_out["text"]`。
    ★为什么必须分账★ 第二个返回值给 worker 看、被截到 1000 字符；而 X-C3 的"全或无"判据
    （有一条第三方缺失 → 全盘不标 BLOCKED）读的是**同一份文本**——截断若正好切掉那条第三方
    缺失行，求解器只看见内部缺失 → 误判 BLOCKED = fail-open。故分类器必须吃全文。
    """
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

    js_ts = [f for f in files if f.endswith((".ts", ".tsx", ".js", ".jsx", ".vue"))]
    # tsc --noEmit 需要【完整工程树+node_modules】才能解析 import → 走沙箱优先(A-P1-10)。
    # 沙箱模式下 package.json 不在本地，用 _manifest_present 沙箱感知判定。
    if js_ts and _manifest_present(("package.json",), project_path):
        # ★X-M8（27 号文 §3.2）★ tsc 解析不了 .vue SFC（单文件组件要 vue-tsc）——治前
        # 触发集连 .vue 都不含：Vue 工程的 .vue 改动**零类型闸**。有 .vue 时先试
        # `vue-tsc`（Vue 官方维护的 tsc 封装，覆盖 .vue+.ts 超集）；项目没装 vue-tsc
        # → 退 tsc + WARNING（.vue 无类型覆盖=降级，必须机读可观测，血规 10④）。
        _used_vue_tsc = False
        if any(f.endswith(".vue") for f in js_ts):
            try:
                _vrc, _vout, _verr = _run_check_split(
                    "npx --no-install vue-tsc --noEmit --pretty false",
                    project_path, timeout=timeout)
                _vcomb = (_vout or "") + (("\n" + _verr) if _verr else "")
                if isinstance(raw_out, dict):
                    raw_out["text"] = _vcomb      # X-C3：分类器吃全文
                if _tool_missing(_vcomb) or _is_infra_failure(_vcomb):
                    logger.warning(
                        "[L1.2] X-M8 项目缺 vue-tsc（或基础设施瞬时错误）→ .vue 改动"
                        "**无类型覆盖**，退 tsc（只覆盖 .ts/.js）: %s", _vcomb[:200])
                elif _vrc != 0:
                    # 与 tsc 同口径（A2 fail-closed）：非 infra 的 vue-tsc 失败即编译不过
                    return False, (_vcomb.strip()[:1000] or f"vue-tsc failed rc={_vrc}")
                else:
                    _used_vue_tsc = True
            except FileNotFoundError as exc:
                logger.warning("[L1.2] X-M8 vue-tsc 执行工具缺失，退 tsc（.vue 无类型覆盖）: %s", exc)
        if not _used_vue_tsc:
            try:
                rc, out, err = _run_check_split("npx tsc --noEmit --pretty false", project_path, timeout=timeout)
                combined = (out or "") + (("\n" + err) if err else "")
                # 基础设施/工具瞬时错误(无网装 typescript、tsc 缺失)不算编译失败(A-P1-09)
                if isinstance(raw_out, dict):
                    raw_out["text"] = combined      # X-C3：分类器吃全文（人读消息才截断）
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

# ★X-M2（27 号文 §3.2）★ 包声明对账按栈分派：后缀 → (源根正则, 声明行正则)。
# Kotlin 与 Java 包声明语义全同（`package a.b.c`、路径反推包），但①源根惯例是
# src/main|test/kotlin（官方也支持混放 java 根）②声明行**无分号**——共用 `_PKG_DECL_RE`
#（要求 `;`）会对全部 .kt 抽不到声明 ⇒ 对账静默跳过，与"没接上"同效。
# 刻意不含 go：`package main` 是**包名**不是导入路径，与目录无确定性对应
# （包名≠末段目录名合法且常见）——判据不确定就 fail-honest 不猜（纪律 2）。
_PKG_DECL_RULES: dict[str, tuple[re.Pattern, re.Pattern]] = {
    ".java": (re.compile(r"(?:^|/)(?:src/main/java|src/test/java|java)/(?P<rel>.+\.java)$"),
              _PKG_DECL_RE),
    ".kt": (re.compile(r"(?:^|/)(?:src/main/kotlin|src/test/kotlin|src/main/java|src/test/java)"
                       r"/(?P<rel>.+\.kt)$"),
            re.compile(r"^\+\s*package\s+([A-Za-z_][\w.]*?)\s*;?\s*(?://.*)?$")),
}


# 成对定界符——任何有块结构的语言都有（Java/JS/Go/Rust/C/C#/PHP/Velocity/JSON…）；
# 纯缩进语言（Python/YAML）天然平衡，本闸对其恒不命中（不误杀）。
_BALANCE_PAIRS = (("{", "}"), ("(", ")"), ("[", "]"))


_LITERAL_RE = re.compile(
    r'"(?:\\.|[^"\\])*"'      # 双引号串
    r"|'(?:\\.|[^'\\])*'"     # 单引号串
    r"|//[^\n]*"                # 行注释（C 族）
    r"|#[^\n]*"                 # 行注释（shell/py/ruby）
)


def _strip_literals(line: str) -> str:
    """剔除字符串字面量与行注释——`logger.info("}")` 这类会给定界符收支造成假差额。"""
    return _LITERAL_RE.sub("", line)


def _truncated_artifacts(diff: str) -> list[dict]:
    """★被截断销毁的产物必须在 L1 拦住（26 号文 C-1）★

    round67m2 实测 st-8：修复轮撞 Agent 迭代上限 50 被强行截断，文件停在**半个标识符**上
    （`return new ToStringBuilder(this,To` + 末行无换行标记），base 105 行的
    最后两个闭合括号连同方法体一起被删。而四道 L1 闸全瞎：
      · `.vm` 是 resource 不进 javac；
      · `test_cmd=null → l1_3_test_ok=true`；
      · 4 条 verify_commands 全是文件【顶部】的存在性 grep（尾部被砍一条都不会失败）。
    若任务未 escalate，此文件会进 merge，L2 同样不碰 .vm、L3 不跑代码生成器 →
    **一路交付到用户手里**。

    判据（确定性、栈中立、零外部工具）：本次变更是否**改变了文件的成对定界符收支**。
      · 比的是【变更前后的差额之差】而不是绝对平衡——hunk 窗口本就可能只覆盖半个块
        （st-8 的窗口里 old 侧就是 -1），要求窗口内绝对平衡会把所有局部改动误杀。
        合法改动无论怎么增删，都不该改变整个文件的收支：delta(new) - delta(old) == 0。
      · 字符串字面量与行注释先剔除——`logger.info("}")` 这类会造成假差额。
      · 纯缩进语言（Python/YAML）天然无定界符 → 差额恒 0，本闸对其不命中（不误杀）。
    返回 [{file, pair, delta, evidence}]；无法解析/无变化 → []。
    """
    out: list[dict] = []
    try:
        # ★split_diff_by_file 返回 list[(paths:list[str], text)] 而非 dict★
        # 初版按 dict 写 `.items()` → 每次都抛进下方 fail-open 的 except，
        # **闸从未生效而测试若只断言"不误杀"就会全绿**——正是本轮反复栽的假绿形态。
        from swarm.project.diff_apply import split_diff_by_file
        for _paths, fdiff in (split_diff_by_file(diff) or []):
            fpath = (_paths or ["?"])[-1]
            old_lines: list[str] = []
            new_lines: list[str] = []
            no_newline = False
            for ln in fdiff.splitlines():
                if ln.startswith("\\"):
                    no_newline = True
                    continue
                if ln.startswith(("+++", "---", "diff --git", "index ", "@@",
                                  "new file", "deleted file", "similarity",
                                  "rename ", "old mode", "new mode")):
                    continue
                if ln.startswith("+"):
                    new_lines.append(ln[1:])
                elif ln.startswith("-"):
                    old_lines.append(ln[1:])
                elif ln.startswith(" "):
                    old_lines.append(ln[1:])
                    new_lines.append(ln[1:])
            if not new_lines:
                continue          # 纯删除文件：不是"截断"，由别的闸管
            _old_c = [_strip_literals(x) for x in old_lines]
            _new_c = [_strip_literals(x) for x in new_lines]
            for opener, closer in _BALANCE_PAIRS:
                _old_delta = sum(x.count(opener) - x.count(closer) for x in _old_c)
                _new_delta = sum(x.count(opener) - x.count(closer) for x in _new_c)
                _shift = _new_delta - _old_delta
                if _shift:
                    out.append({
                        "file": fpath,
                        "pair": opener + closer,
                        "delta": _shift,
                        "evidence": ("末行无换行符（典型的写到一半被掐断）"
                                     if no_newline else "本次变更改变了定界符收支"),
                    })
                    break
    except Exception:  # noqa: BLE001 — 纯文本启发式，解析失败绝不阻断（fail-open）
        return []
    return out


def _package_decl_mismatches(diff: str) -> list[dict]:
    """E6①：diff 内【新建 JVM 源文件】的包声明与源根路径反推包比对（X-M2 起按
    `_PKG_DECL_RULES` 分派：.java / .kt；go 等无确定性路径↔包对应的栈刻意不猜）。

    返回不符清单 [{file, declared, expected}]。路径不含该栈源根标记（正则未命中）或
    抽不到声明行 → 跳过（保守，不误杀非常规布局）。纯文本零外部工具。"""
    out: list[dict] = []
    try:
        from swarm.project.diff_apply import split_diff_by_file
        for files, text in split_diff_by_file(diff or ""):
            if "--- /dev/null" not in text and "new file mode" not in text:
                continue
            for f in files:
                _norm = str(f).replace("\\", "/")
                _ext = "." + _norm.rsplit(".", 1)[-1] if "." in _norm else ""
                rule = _PKG_DECL_RULES.get(_ext)
                if rule is None:
                    continue
                _root_re, _decl_re = rule
                _rm = _root_re.search(_norm)
                if not _rm:
                    continue
                dotted = _rm.group("rel")[: -len(_ext)].replace("/", ".")
                if "." not in dotted:
                    continue
                expected = dotted.rsplit(".", 1)[0]
                declared = None
                for ln in text.splitlines():
                    m = _decl_re.match(ln)
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

def l1_self_review_enabled() -> bool:
    """R63-T9①：L1.4 LLM 自检总闸——默认关闭，env 显式 opt-in。

    round63 实锤：自检结论【从不影响 verdict】（本 pipeline 到 L1.4 后无论自检结论
    恒 return True；executor 仲裁的 llm_ok 来自 pipeline 确定性返回值而非 self_review），
    却在每个通过的子任务上烧 1 次 worker LLM 调用产假 ✅ 清单（21/34 幻觉 PASS 同族
    模型盖章）。默认翻转为关闭；SWARM_WORKER_L1_SELF_REVIEW=true 显式恢复旧行为。
    executor Phase-4 与本函数共用此闸（关闭时连 LLM 句柄都不取）。
    """
    return os.environ.get(
        "SWARM_WORKER_L1_SELF_REVIEW", "false").strip().lower() in ("true", "1", "yes")


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

def _project_file_exists(rel: str, project_path: str) -> bool:
    """**指定相对路径**的文件是否存在（沙箱优先）。

    与 `_manifest_present` 的区别（别混用，X-H2 实测踩过）：那个是"树里深度≤3 内**任意位置**
    有没有叫这个名字的清单"，本地兜底只查**工程根下同名文件**；而 scoped 测试探测要问的是
    "`svc/user_test.go` 这个**具体路径**在不在"。用前者会漏掉一切子目录里的测试文件。
    """
    r = str(rel or "").replace("\\", "/").lstrip("/")
    if not r or ".." in r.split("/"):
        return False
    ctx = _sandbox_ctx()
    if ctx is not None:
        sandbox, manager, remote = ctx
        try:
            cr = manager.run_command(
                sandbox, f"test -f {shlex.quote(remote + '/' + r)} && echo y", timeout=20)
            return bool((cr.stdout or "").strip())
        except Exception:  # noqa: BLE001 — 探测失败保守当不存在（不猜测试命令）
            return False
    return os.path.isfile(os.path.join(project_path, r))


# X-H1：清单所在目录要拼进 shell 命令，形态白名单（与 image_builder._SAFE_SUBDIR_RE 同源判据：
# 仓库内容即攻击者可控，故是"允许什么"而非"禁止什么"；`\Z` 而非 `$`，防尾随换行绕过）。
_SAFE_REL_DIR_RE = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9._][A-Za-z0-9._/-]*\Z")


def _npm_has_build_script(project_path: str) -> bool:
    """根 package.json 是否真有 `scripts.build`（纪律 2：没有就不下发 `npm run build`）。

    注：命令用 `--if-present` 已能容忍缺失（退出 0），但那等于"闸门静默不跑"＝假过；
    这里显式查一次，好让"没有 build 脚本"走到返回 `''` 的诚实分支。
    """
    import json
    txt = _read_project_file(project_path, "package.json")
    if not txt:
        return False
    try:
        return bool((json.loads(txt) or {}).get("scripts", {}).get("build"))
    except Exception:  # noqa: BLE001
        return False


def _npm_has_test_script(project_path: str) -> bool:
    """根 package.json 是否真有 `scripts.test`（且不是 npm init 那句占位符）。

    ★为什么必须查★ 纪律 2（绝不猜）：`npm test` 在无 `scripts.test` 时报
    `Missing script: "test"` 退出 1 ⇒ 我们会把"项目没测试"误判成"测试失败"。
    npm init 生成的默认值是 `echo "Error: no test specified" && exit 1`——它**存在但必失败**，
    等价于没有，必须一起排除。
    """
    import json
    txt = _read_project_file(project_path, "package.json")
    if not txt:
        return False
    try:
        scripts = (json.loads(txt) or {}).get("scripts") or {}
    except Exception:  # noqa: BLE001 — 读不出就当没有（fail-honest，不猜）
        return False
    t = str(scripts.get("test") or "").strip()
    return bool(t) and "no test specified" not in t


def _guess_test_cmd(project_path: str, modified: list[str]) -> str | None:
    """无 harness.test_command 时按栈猜一条 scoped 测试命令；猜不出返 None。

    ★X-H2（27 号文 §3.2 HIGH）★ 原实现**只认 `.py`**，其余栈一律返 None ⇒ L1.3 落
    `l1_3_test_ok=True` + `test_skipped` ⇒ **跳过＝通过**，测试面对 npm/go/rust 整类不存在。

    ★三条硬约束★
    1. **绝不臆造命令**（纪律 2）：只在有**确定性证据**（真有测试文件 / 真有 `scripts.test`）
       时才出命令。猜错的代价是把"没测试"误判成"测试失败"，比不猜更坏。
    2. **JVM 刻意不猜**：`brain/nodes/shared.py` 给 java 的 `test_command` 是**故意留空**的
       （S1 注释："RuoYi 等项目常无测试依赖，强跑必失败"）。在这里补 `mvn test` 会绕过那个
       决定，且直接打在唯一跑过 E2E 的栈上。要改得连同 S1 一起改，不在本函数私自放行。
    3. **清单探测走沙箱优先**：`_manifest_present`/`_read_project_file` 而非 `os.path.isfile`
       ——沙箱模式下本地树只有 pull-back 的文件，用本地判存在会漏（`_derive_full_build_command`
       的 A4 治本同源教训）。
    """
    mods = [str(f) for f in (modified or []) if str(f).strip()]

    # ① scoped：与改动源码同名的测试文件（最省、最准）
    for fp in mods:
        base = Path(fp).stem
        if fp.endswith(".py"):
            for c in (f"tests/test_{base}.py", f"test/test_{base}.py", f"test_{base}.py"):
                if _project_file_exists(c, project_path):
                    # ★M-8★ 路径段来自 plan 授权的 `modified`，**必须 quote 才进 shell**：
                    # 文件名含空格/shell 元字符是合法的，裸拼会被 shell 切成两个参数 ⇒
                    # pytest 报 file not found ⇒ 非 infra ⇒ 硬 FAIL。同文件两个 sibling
                    # （`:1570` 全角标点扫、`:1616` 伪空格扫）与 `_anchor`（本函数尾部，
                    # `cd {shlex.quote(d)} && …`）早已 quote，
                    # 此处原是唯一漏网＝27 号文 M-8 说的"判据不对称"。
                    # 目录段固定为 `tests/`/`test/`/根，`Path.stem` 不含 `/` ⇒ 无穿越面，
                    # quote 即足（与 go 臂的分档理由不同，见下）。
                    return f"python -m pytest -q {shlex.quote(c)}"
        elif fp.endswith(".go"):
            # Go 的测试与被测源**同目录同包**：`svc/user.go` → `svc/user_test.go`
            _dir = fp.rsplit("/", 1)[0] if "/" in fp else "."
            _cand = f"{_dir}/{base}_test.go" if _dir != "." else f"{base}_test.go"
            if _project_file_exists(_cand, project_path):
                if _dir == ".":
                    return "go test ./..."
                # ★M-8★ `_dir` 是 `modified` 的目录段**原样**进 shell，必须 quote。
                # ★为什么这里是 quote 而不是 `_SAFE_REL_DIR_RE` 白名单★
                # 穿越面已被**上游**拦掉：`_project_file_exists` 对 `_cand` 判
                # `".." in r.split("/")` 且 `lstrip("/")`，而 `_cand` 与 `_dir` 同源
                # ⇒ 带 `..`／绝对路径的 fp 在候选探测那一步就返 False，走不到这里。
                # 于是白名单唯一还活着的一格是"空格/元字符"，而对这一格 quote 严格更优：
                # 白名单会把它退化成整仓兜底（丢掉 scoped 的省时与精准），quote 则照常
                # 出正确的 scoped 命令。`go test` 的 pattern 由它**自己**解析，shell 只需
                # 把整串当**一个** argv 交给它。（`go test` 对含空格目录到底成不成功，本机
                # 无 go 工具链未实测——但那是 go 该如实报的错；不 quote 则 shell 先把它切成
                # `./my` + `svc/...` 两个 argv，报的错与真因无关、必被误诊。）
                return f"go test {shlex.quote(f'./{_dir}/...')}"
        elif fp.endswith((".ts", ".tsx", ".js", ".jsx")):
            _d = fp.rsplit("/", 1)[0] + "/" if "/" in fp else ""
            for c in (f"{_d}{base}.test.ts", f"{_d}{base}.spec.ts", f"{_d}{base}.test.tsx",
                      f"{_d}{base}.test.js", f"{_d}{base}.spec.js",
                      f"{_d}__tests__/{base}.test.ts", f"{_d}__tests__/{base}.test.js"):
                if _project_file_exists(c, project_path):
                    # 有测试文件仍要求 `scripts.test` 在场——否则 `npm test` 必报 Missing script。
                    # ★复核 H-3★ 这里原来是 `return ... else None`：没有 test 脚本时**直接 return
                    # None**，把后面 go/rust/python 的工程级兜底整块掐死 ⇒ 混栈子任务
                    # （同时改了 `.ts` 与 `.go`）静默退回 `test_skipped`＝跳过即通过，正是 X-H2
                    # 要治的假过。改成只在**真能出命令**时 return，否则继续往下找。
                    if _npm_has_test_script(project_path):
                        return "npm test --silent"
                    break
        elif fp.endswith(".rs"):
            # Rust 单元测试常内联在源文件里（`#[cfg(test)] mod tests`），文件级探测不可靠；
            # 交下面的工程级兜底（cargo test 对无测试的 crate 是 0 退出，安全）。
            pass

    # ② 工程级兜底：只在有确定性证据时出命令
    # ★复核 C-2★ 这条原先**没有语言守卫**且排在最前 ⇒ 任何深度≤3 的 `pyproject.toml`
    # （含 `tools/`、`node_modules/**`）都会劫持**所有栈**的测试闸：java 子任务、前端子任务
    # 全被下发根级 `pytest` ⇒ 收集不到用例 rc=5 ⇒ `t_ec == 0` 为假 ⇒ 非 infra ⇒ **硬 FAIL**，
    # sticky 且换模型重试同死。而多栈仓（`backend/pyproject.toml` + `frontend/`）正是本战役的
    # 目标形态。加守卫：只有真改了 `.py` 才走 python 兜底。
    # ★复核 CRITICAL-2★ 工程级兜底必须**锚定**：探针已改成递归（深度≤3），若命令仍在**工程根**
    # 跑，`backend/pyproject.toml` 这种形态会得到根级 pytest ⇒ 收集不到用例 rc=5 ⇒ 非 infra ⇒
    # **硬 FAIL**（sticky，换模型同死）。改前是 `os.path.isfile(root/…)` 只看根 → 返 None →
    # `test_skipped`，所以这是本批新造的判死面。锚点同样由 modified 反查。
    # ★BRAIN-001/W-3★ 工程级测试命令从 `worker/stack_drivers.TEST_DRIVERS` 派生，事实源
    # 是 `stacks.spec.STACK_SPEC`。命令字面量不再硬编码在本函数内。
    def _anchor(names: tuple[str, ...], cmd: str) -> str:
        d = _manifest_dir_for(mods, names, project_path)
        if d is None:
            d = _manifest_dir(names, project_path)
        if not d:
            return cmd
        if not _SAFE_REL_DIR_RE.match(d):
            return cmd
        return f"cd {shlex.quote(d)} && {cmd}"

    for stack_key in ("python", "go", "cargo", "npm"):
        drv = _test_driver_for(stack_key)
        if not drv:
            continue
        if not _manifest_present(drv.anchor_manifests, project_path):
            continue
        if not any(f.endswith(_ext_for_lang(drv.lang)) for f in mods):
            continue
        # npm 还需要 scripts.test 在场
        if stack_key == "npm" and not _npm_has_test_script(project_path):
            continue
        return _anchor(drv.anchor_manifests, drv.test_cmd)
    return None


def _test_driver_for(stack_key: str):
    """BRAIN-001/W-3：测试命令单一事实源入口。"""
    from swarm.worker.stack_drivers import test_driver
    return test_driver(stack_key)


def _build_driver_for(stack_key: str):
    """BRAIN-001/W-3：构建命令单一事实源入口。"""
    from swarm.worker.stack_drivers import build_driver
    return build_driver(stack_key)


def _ext_for_lang(lang: str) -> tuple[str, ...]:
    """语言 → 该语言常见源文件后缀（用于工程级兜底守卫）。"""
    return {
        "python": (".py",),
        "go": (".go",),
        "rust": (".rs",),
        "node": (".ts", ".tsx", ".js", ".jsx", ".vue"),
    }.get(lang, ())




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


_UPSTREAM_GOAL_RE = re.compile(
    r"\b(compile|test-compile|test|package|verify|install|deploy)\b")


# ★Maven 命令的【唯一】词元判据与改写口径（X-C1 治本）★
# 病灶：`_scope_maven_command` 用裸子串 `"mvn" in command` 判、用 `str.replace("mvn", …)` 改，
# 无词边界。实测被改烂的形态（每一条都会让命令不存在或参数畸形 → exit 127 →
# `_is_infra_failure` 判 True → BLOCKED **无限退避重试、每轮同死、零诊断线索** → 烧穿预算连坐）：
#     ./mvnw -q compile        → ./mvn -pl m -amw -q compile     ← 命令不存在 + 垃圾参数
#     sh mvnw verify           → sh mvn -pl m -amw verify
#     npm run build:mvn        → npm run build:mvn -pl m -am     ← 与 Maven 毫无关系
#     docker run mvn-builder…  → docker run mvn -pl m -am-builder…
# `./mvnw` 是 Spring Boot 生态默认形态；RuoYi 基线恰好不用 wrapper，所以这条从未暴露。
# ★不对称铁证★：兄弟函数 `_reactorize_verify_command` 早已显式挡掉 mvnw，其注释更是明写
# "_scope 的裸 .replace 会破坏语法（放大既有缺陷）"——**知道病灶在哪，却只在自己这半边绕开**。
# 治法不是再加一处守卫，是把判据与改写收敛成本模块两个函数，三个调用面共用（DRY + 单一事实源）。
# 前后排除面都必须含 `:`（还有前置的 `=`）：`npm run build:mvn` 的 mvn 前是冒号、
# `yarn mvn:check` 后是冒号、`TOOL=mvn` 是赋值右值——**都不是"要执行 mvn"**。
# 实证过程本身就是教训：初版只排 `\w./-` → `build:mvn` 漏；补前置 `:` → `mvn:check` 漏；
# 补后置 `:` → `mvn.cmd` 漏。脚本名/任务名/扩展名里嵌工具名是极常见形态，判据必须两侧对称。
# 前置刻意**不排** `/` 与 `.`：`/usr/bin/mvn` 是合法调用（替换只动 mvn 词元，路径前缀保留），
# 而 `./mvnw` 由 `_MVNW_RE` 专门挡——挡 wrapper 不该靠"顺带把带路径的 mvn 也挡了"。
_MVN_TOKEN_RE = re.compile(r"(?<![\w:=-])mvn(?![\w.:-])")
_MVNW_RE = re.compile(r"(?<![\w.])\.?/?mvnw(?![\w-])")


def is_plain_mvn_command(command: str) -> bool:
    """该命令是否为【可安全改写】的裸 mvn 调用。

    三条排除，任一成立即不可改写（原样返回比改烂强得多——改烂是 127 空转，
    不改只是少一层模块收窄）：
      · Maven wrapper（`mvnw` / `./mvnw` / `sh mvnw`）——它不是 `mvn`，替换必毁；
      · 出现多次 mvn 词元（复合命令）——`count=1` 的替换会改错那一个；
      · 根本没有 mvn 词元（`npm run build:mvn` 这类只是字符串里带 mvn）。
    """
    cmd = command or ""
    if _MVNW_RE.search(cmd):
        return False
    return len(_MVN_TOKEN_RE.findall(cmd)) == 1


def is_maven_family_command(command: str) -> bool:
    """该命令是不是 **Maven 系构建**（`mvn` 或 `./mvnw` 皆算）。

    ★与 `is_plain_mvn_command` 是【两档】判据，绝不可互相顶替（对抗复核 HIGH，血泪同族
      "复用单一事实源 ≠ 复用其消费契约"）★

    | 判据 | 问的问题 | 判错的后果 |
    |---|---|---|
    | `is_plain_mvn_command` | 能不能**安全替换 mvn 词元** | 判宽=命令被改烂 |
    | 本函数 | 这是不是一次 **Maven 构建** | 判窄=Maven 专属修复/闸门对 wrapper 工程整类失效 |

    实锤：`_ensure_reactor_am` 只在 `-pl <targets>` 之后插 `-am`，**不碰命令头**，对
    `./mvnw` 完全安全。一度把它的守卫收敛到 `is_plain_mvn_command` → wrapper 工程
    `./mvnw compile -pl mod-a` 不再补 `-am` → 解析不到 reactor 兄弟 → **假阴性烧正确代码**
    （round65e10 st-1 死因① 原样复活），且恰好砸在这个 patch 本要拯救的人群头上。
    """
    cmd = command or ""
    return bool(_MVN_TOKEN_RE.search(cmd) or _MVNW_RE.search(cmd))


def _sub_mvn_token(command: str, replacement: str) -> str:
    """把【唯一那个 mvn 词元】替换掉——词边界感知，绝不碰 mvnw / 子串。

    调用前必须先过 `is_plain_mvn_command`（本函数不重复判，避免两处判据漂移）。
    """
    return _MVN_TOKEN_RE.sub(replacement, command, count=1)


def _ensure_reactor_am(command: str) -> str:
    """R65E10-T1（round65e10 st-1 死因①）：命令【已含 -pl <targets>】但缺 -am 且目标需上游产物
    → 在 -pl <targets> 之后注入 -am；否则原样。

    死因：plan 自撰 verify `mvn compile -pl <mod> -q`（无 -am）解析不到 reactor 兄弟
    （com.<proj>:ruoyi-common:jar → Could not find artifact）→ 假阴性烧正确代码。R65E8-T1 只治了
    `cd <mod> && mvn` 裸形，`_scope_maven_command`/reactorize cd-branch 都把"已 -pl"当"已 reactor
    感知"原样返回 → 已 -pl 缺 -am 形永不修。此纯函数补该形，供两处共用（DRY，与 build/test 对称）。

    保守边界：无 -pl / 非 mvn / 已 -am(含 --also-make 长形) / 不需上游的目标(validate/clean/help)
    → 原样（validate 不加 -am 守 P0-B 不连坐 sibling，与既有 needs_upstream 口径一致）。
    """
    if "-pl" not in command or not is_maven_family_command(command):
        # ★X-C1 分档（对抗复核 HIGH）★ 这里问的是"是不是 Maven 构建"，不是"能不能替换词元"。
        # 本函数只在 `-pl <targets>` 后插 `-am`，不碰命令头 → 对 `./mvnw` 同样安全且同样必要。
        # 用 `is_plain_mvn_command` 门控会让 wrapper 工程整类丢掉 -am 修复（死因① 复活）。
        return command
    # 已带 -am / --also-make → 不重复注入
    if re.search(r"(?:^|\s)-am\b", command) or "--also-make" in command:
        return command
    # ★复核 HIGH★ goal 判定必须【先剥掉 -pl <targets>】再扫——否则模块名含 test/package/install/
    # deploy 等词元子串（如 `-pl ruoyi-quartz-test`）会假触发 needs_upstream→纯 validate 被误加 -am→
    # 连坐 sibling 违 P0-B（D5(a) 不变量）。剥 -pl 段只为 goal 检测，注入仍作用于原命令。
    _goal_scan = re.sub(r"-pl\s+\S+", " ", command)
    if not _UPSTREAM_GOAL_RE.search(_goal_scan):
        return command  # validate/clean 等不需上游产物 → 不加 -am
    # -pl 后的 targets（module 列表，逗号/无空格）之后插 -am
    out = re.sub(r"(-pl\s+\S+)", r"\1 -am", command, count=1)
    if out != command:
        # ★复核 MED★ 补齐必留痕（与兄弟 R65E8-T1 归一日志对称）——否则"修复是否触发"在 swarm.log
        # 不可见，正是 round65e8/e10 难诊断的审计盲区。
        logger.info(
            "[L1.3.5] R65E10-T1 已 -pl 缺 -am 补齐：%r → %r"
            "（无 -am 解析不到 reactor 兄弟=假阴性烧正确代码，round65e10 st-1 死因①）",
            command, out)
    return out


def _scope_maven_command(command: str, project_path: str, modified: list[str],
                         details: dict | None = None, phase: str = "") -> str:
    """多模块 Maven：把整 reactor 的 mvn 命令改写成只编【改动所在模块】(-pl <mod> -am)。

    RuoYi 等多模块工程根 pom 聚合 6 个模块，整 reactor `mvn compile` 需要所有模块
    源码齐备(而 worker 只同步改动模块) → reactor 失败。正确做法是 -pl 限定改动模块、
    -am 连带构建其依赖的上游模块。已含 -pl 的命令补齐 -am（R65E10-T1）；非 mvn 命令原样返回。

    details/phase（X-C1 复核 MED-2）：跳过收窄时落**机读账**，见下。可缺省（旧调用点不变）。
    """
    if "-pl" in (command or "") and is_maven_family_command(command):
        # R65E10-T1：已 -pl 不再盲目原样——若 upstream 目标缺 -am 则补（治 round65e10 st-1 死因①：
        # `mvn compile -pl <mod> -q` 无 -am→sibling 解析不到→假阴性）。已 -am/非 upstream→原样。
        # ★这一分支必须在 is_plain 门之【前】（对抗复核 HIGH）★：-am 补齐不改命令头，对
        # wrapper 安全且必要；放在门后会让 `./mvnw compile -pl mod-a` 整类丢掉该修复。
        return _ensure_reactor_am(command)
    if not is_plain_mvn_command(command):
        # X-C1：wrapper / 复合 / 多词元 → 原样返回（原实现在此把命令改烂）。
        # ★留痕必须覆盖【全部会被跳过收窄的 mvn 形态】，不只 wrapper（复核 MED-2）★
        # 旧行为"改烂"至少会 127 炸出来；新行为是**看起来正常地失败**——整 reactor 缺
        # 兄弟模块源码 → L1 FAIL，而日志里没有一句说"模块收窄被跳过了"。这正是本仓
        # 反复吃亏的"降级无痕"。故：含 mvn 词元却不可改写 → WARNING + details 机读键。
        _cmd = command or ""
        if _MVN_TOKEN_RE.search(_cmd) or _MVNW_RE.search(_cmd):
            _reason = ("wrapper" if _MVNW_RE.search(_cmd)
                       else ("multi_token" if len(_MVN_TOKEN_RE.findall(_cmd)) > 1
                             else "not_plain"))
            logger.warning(
                "[L1] Maven 模块收窄跳过（reason=%s，该命令将以整 reactor 跑；"
                "多模块工程可能因缺兄弟模块源码假阴性失败）：%s", _reason, _cmd[:160])
            if isinstance(details, dict):
                details.setdefault("maven_scope_skipped", []).append(
                    {"phase": phase or "?", "reason": _reason, "cmd": _cmd[:200]})
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
        scoped = _sub_mvn_token(command, f"mvn -f {self_scaffold[0]}/pom.xml")
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
    return _sub_mvn_token(command, f"mvn -pl {pl}{am}")


# R65E8-T1（round65e8 task b4f2fcda PARTIAL 82/124 死因）：cd 进子模块目录裸跑 mvn 的验收命令归一。
_CD_MVN_RE = re.compile(r'^\s*cd\s+(?P<dir>[^\s&|;]+)\s*&&\s*(?P<rest>.*\bmvn\b.*)$', re.DOTALL)


def _reactorize_verify_command(command: str, project_path: str, pl_basis: list[str],
                               details: dict | None = None) -> str:
    """R65E8-T1：L1.3.5 验收命令 reactor 归一——治"cd 子模块裸 mvn 假阴性烧正确代码"。

    死因（round65e8 终态坐实）：LLM 授的 acceptance_criteria 常写 `cd <module> && mvn <goal>`——cd 进
    子模块目录裸跑 mvn（无 -pl/-am），Maven 解析不到 reactor 兄弟（com.<proj>:*）→ 假阴性 fail【本身
    编译通过、带 -am reactor 构建也成功】的正确代码 → 烧光该子任务重试预算 → abandon → 连坐 38>阈值
    31 → 计划覆灭 PARTIAL/REJECT。根因=不对称：build_cmd/test_cmd 都过 _scope_maven_command 收窄，唯
    verify_commands 裸跑；且 `cd <module> &&` 前缀隔离模块，连 -pl -am 都够不着。

    归一（确定性、栈感知仅 Maven、极保守——复核 HIGH/MED/LOW 整改后）：
    - `cd <已注册 reactor 模块> && <单条干净 mvn goal>`（无 -pl/-f）→ 剥 cd、改写为工程根
      `mvn -pl <module>[ -am] <goal>`（-am 仅对需上游产物的目标加，validate/clean 不加，守 P0-B）；
    - 真·无 cd 的单条裸 mvn → 交 _scope_maven_command（与 build/test 对称，用同源 R50-3 过滤基 pl_basis）；
    - 复合命令(&&/;)/Maven wrapper(mvnw)/多 mvn 调用/`..` traversal/非规范 cd/cd 非注册目录/已 -pl/-f/
      非 Maven → 一律原样（绝不臆改：子串替换会破坏复合命令语法，非规范 cd 交 _scope 会 -pl 错配 cwd）。

    pl_basis：调用方须传【已按 R50-3 过滤 repaired_file_paths 的 -pl 圈定基】（与 build/test 同源；复核
    HIGH：裸 modified 会把 repair 触达的外模块拖进 -pl→脚手架被别人在飞坏代码连坐=本 patch 要杀的病复发）。

    X-M1（27 号文 B-5 BuildDriver 验收臂）：非 Maven 命令交 `l1_verify_drivers`
    栈驱动层归一（gradle cd+wrapper / npm 根裸 script / go.work 根裸 go test 三形，
    同「只改 positively-known」契约）；下方 Maven 臂一字不动（唯一跑过 E2E 的栈）。
    """
    if not command:
        return command
    if not is_maven_family_command(command):
        # X-M1（R1 hunter F1）：非 Maven 系直进驱动层——判据与 _scope/_ensure 同源
        # （治前用 "mvn" 子串粗筛，`npm run test-mvn` 这类词元不含 mvn 的命令会被
        # 子串挡在 Maven 臂里原样返回=归一覆盖漏面；反向含 mvn 词元的非构建命令
        # 仍由 Maven 臂内 family 闸原样放行，路由结果两形等价）。
        from swarm.worker.l1_verify_drivers import VerifyIO, normalize_verify_command
        return normalize_verify_command(
            command, project_path, pl_basis,
            VerifyIO(read_file=_read_project_file,
                     file_exists=_project_file_exists,
                     anchor_for=_manifest_dir_for),
            details=details)
    m = _CD_MVN_RE.match(command)
    if m is None:
        # 非规范形若仍含 cd（`;` 分隔 / 带空格引号目录 / env 前缀）→ _scope 不懂 cd 会 -pl 错配 cwd → 原样（MED2）
        if re.search(r'(?:^|\s|&|;)cd\s', command):
            return command
        # X-C1 治本后：判据统一走 `is_plain_mvn_command`（本函数原有的 mvnw 守卫是当年
        # 复核在这半边打的补丁，其注释已明写"_scope 的裸 .replace 会破坏语法"——
        # 病灶在 _scope 却只在这里绕开。现在两处同源，绝不再各自维护一套。）
        # ★分档（对抗复核 HIGH）★：Maven 系但不可改写词元（wrapper / 已 -pl）仍要进
        # `_scope_maven_command`——它内部先走 -am 补齐（对 wrapper 安全且必要），
        # 再走 is_plain 门做词元收窄。在这里提前 return 会把 -am 修复整类砍掉。
        if not is_maven_family_command(command):
            return command
        return _scope_maven_command(command, project_path, pl_basis, phase="verify")
    _dir = m.group("dir").strip().strip('"\'').replace("\\", "/")
    _dir = _dir.removeprefix("./").rstrip("/")
    _rest = m.group("rest").strip()
    # MED1/LOW：只改写【单条干净 mvn 调用】——复合/wrapper/多 mvn/`..` traversal/非 mvn 起手 → 原样。
    # ★X-C1 复核 HIGH-1：这里曾是【第四处】自建判据（`\bmvn\b` 计数），与 _sub_mvn_token 口径不同★
    # `\bmvn\b` 认 `mvn.cmd`/`mvn:check`/`mvn-wrapper` 是完整词、`_MVN_TOKEN_RE` 不认 → 判"可改写"
    # 进来后 sub 成 no-op，而函数已决定改写 → **返回剥掉 cd 前缀的 _rest**：
    #   "cd mod-a && mvn.cmd -q compile" → "mvn.cmd -q compile"（作用域从模块目录变成工程根）
    # 比旧病灶更毒：旧的产垃圾参数会 127 炸出来，这个"看起来合法"且日志还宣称归一成功。
    # 现在与 _scope/_ensure 同源——`is_plain_mvn_command` 是唯一判据，`_sub_mvn_token` 不再可能 no-op。
    if ("&&" in _rest or ";" in _rest
            or not is_plain_mvn_command(_rest) or not _MVN_TOKEN_RE.match(_rest)
            or ".." in _dir.split("/")):
        return command
    if re.search(r"-f\s", _rest):
        return command   # 已 -f scoped → 勿臆改
    if "-pl" in _rest:
        # R65E10-T1：cd-branch 同样的"已 -pl 缺 -am"盲区——保守只补 -am，【保留 cd 前缀原样】
        # （对整条命令 apply helper，仅在 -pl 后插 -am；不臆改其余结构）。已 -am/非 upstream→原样。
        return _ensure_reactor_am(command)
    try:
        registered = set(_maven_modules(project_path).values())
    except Exception:  # noqa: BLE001 — 读模块失败保守原样，绝不炸 L1 主链
        return command
    if _dir not in registered:
        return command   # cd 进非注册目录（独立工程/脚本目录）→ 原样，绝不臆改
    needs_upstream = bool(
        re.search(r"\b(compile|test-compile|test|package|verify|install|deploy)\b", _rest))
    am = " -am" if needs_upstream else ""
    scoped = _sub_mvn_token(_rest, f"mvn -pl {_dir}{am}")   # 与 _scope 同源的词边界改写
    logger.info(
        "[L1.3.5] R65E8-T1 验收命令 reactor 归一：%r → %r"
        "（cd 子模块裸 mvn 解析不到 reactor 兄弟=假阴性烧正确代码，round65e8 连坐清盘死因）",
        command, scoped)
    return scoped


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
# A3（19号文）：补 py——Python 整树测试形态（pytest FAILED 行/traceback `File "x.py"`）的
# 报错文件抽取同样走本通道做 scope 归属；对 JVM/Go 等栈纯增量（构建输出提及 .py 才抽取），
# 裸文件名噪声仍由 _build_error_files 的「必须含 /」过滤拦住。
_ERR_FILE_RE = re.compile(r"([A-Za-z0-9_./\-]+\.(?:java|kt|scala|go|rs|ts|tsx|js|vue|xml|py))")


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
    """从构建输出抽【报错所在模块】：pom 解析错的 `The project G:art` + 编译错的文件路径段。

    ★X-M3 消费契约（27 号文 §3.2，消费点契约分析定案）★ 本函数【刻意】只认 JVM 形态
    （`The project G:art` / `…/src/main|test/…` 路径段）——go/npm 等栈的错误没有权威、
    文档化的"模块名"行格式，凭印象造正则=纪律 2 禁止的臆造（造错比恒空更坏：会把别人的
    错归到自己模块头上=假 FAIL，或反之假 BLOCKED）。恒空在两个消费点上都是【安全方向】：
      ① `_build_error_is_upstream` 模块回退道：`own`（-pl，非 Maven 恒空）或本函数为空
        → channel="none" → return False（不判上游）=fail-closed，宁可烧自己修复轮也
        不假 BLOCKED；且文件级两道（scope/file_disjoint）在模块回退【之前】，`_ERR_FILE_RE`
        已覆盖 .go/.ts/.js/.vue/.py 等 → 非 JVM 的归属主力是文件级，模块回退只是 JVM 兜底。
      ② 上游证据装配（:5784）：本函数为空时 `_bof`（文件集）仍可非空；两皆空有
        R67L-B2 死端闸（判上游却吐不出证据 → 落回 FAIL，不假 BLOCKED 空等）。
    判定通道由 `upstream_judge_channel` 留痕（猎手 F4），"none" 机读可辨。
    若日后要给非 JVM 栈加模块级归属：先拿**真实捕获**的该栈构建输出取证错误形态
    （OFFLINE_REPLAY/cassette），按栈分派进表，不在此凭印象扩正则。
    """
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
    """从构建输出抽【报错的源文件】(归一化模块相对路径)。

    ★复核 R3-1（hunter R3 实跑复现）★：先剔除项目外帧——traceback 深入第三方库/
    标准库（requests/pandas 等库密集项目常态，异常在库内爆发）时，库路径帧无 plan
    内 owner 可交，留着既把「自己的坏参数/坏数据在库内爆」误启成 upstream 归属阶梯
    甩锅（L1.3 A3：非测试源码帧触发），又污染 blocked_on_files 噪声。L1.2.1 build
    闸同享本函数，编译错名义文件皆工程文件，剔除无害。栈中立：只认安装目录段，
    不写死语言。
    """
    out: set[str] = set()
    for m in _ERR_FILE_RE.finditer(build_output or ""):
        f = _norm_src_path(m.group(1))
        if "/" in f or f.endswith("pom.xml"):  # 过滤裸文件名噪声，保留真实路径
            if not _is_external_frame(f):  # R3-1：项目外帧不参与归属判定
                out.add(f)
    return out


# A3 复核 R3-1（hunter 实跑复现）：第三方/标准库 traceback 帧的已知依赖安装目录段
# （栈中立清单）。库文件无 plan 内 owner——不是"兄弟产物源码"，留着既误启 upstream
# 归属阶梯甩锅（自己的坏参数在库内爆发=库密集项目常态），又污染 blocked_on_files 噪声。
_EXTERNAL_FRAME_SEGS = ("site-packages", "dist-packages", "node_modules")


def _is_external_frame(rel: str) -> bool:
    """报错路径是否为【项目外】第三方/标准库文件（依赖安装目录/系统库路径）。"""
    r = str(rel or "").replace("\\", "/").lstrip("./").lower()
    if not r:
        return False
    parts = r.split("/")
    if any(seg in parts for seg in _EXTERNAL_FRAME_SEGS):
        return True
    return (
        r.startswith(("usr/lib/", "usr/local/lib/"))
        or "go/pkg/mod/" in r            # Go module cache
        or ".cargo/registry/" in r       # Rust 依赖缓存
        or ".m2/repository" in r         # Maven 本地仓库
        or ".gradle/caches" in r         # Gradle 依赖缓存
    )


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


_RX_MVN_MISSING_ARTIFACT = re.compile(
    r"Could not find artifact\s+[\w.\-]+:([\w.\-]+):(?:[\w.\-]+:)?([\w.${}\-]+)?", re.I)
_RX_POM_PARENT_BLOCK = re.compile(r"<parent>.*?</parent>", re.S | re.I)
_RX_POM_OWN_VERSION = re.compile(r"<version>\s*([^<\s]+)\s*</version>", re.I)


def _module_declared_version(project_path: str, rel: str) -> str | None:
    """取模块 pom 自身声明的 <version>（剥 <parent> 块后的首个；模块未声明=继承父版本
    → 回退根 pom 同法）。属性形态（${...}）/读不到 → None=不可判定。"""
    from pathlib import Path as _P
    for pom_rel in (f"{rel}/pom.xml", "pom.xml"):
        try:
            text = (_P(project_path) / pom_rel).read_text("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        m = _RX_POM_OWN_VERSION.search(_RX_POM_PARENT_BLOCK.sub("", text))
        if m:
            v = m.group(1).strip()
            return None if "${" in v else v
    return None


def _unresolved_internal_module_poms(build_output: str, project_path: str) -> set[str]:
    """R63-T8②：依赖解析级错误（`Could not find artifact G:A:...`）不含任何源文件路径，
    _build_error_files 恒空 → upstream 判定盲区。若缺失坐标的 artifactId 对应【工作区已
    注册模块】（_maven_modules 目录末段），则这是内部模块产物未就绪/被毒化——返回其
    `<模块目录>/pom.xml` 集合供文件级归属判定与 blocked_on 结构化输出。第三方坐标
    （无对应注册模块）返回空集：那是 dep-repair 防线④ 的领域，绝不冒充 upstream。

    复核 R-MED 加固：缺失坐标的【版本】与目标模块自身声明版本字面不符（错误要 9.9.9、
    模块真身 3.8.7）→ 不是"模块未就绪"而是【引用方 pom 写了幻觉版本】（round47 已知
    故障类，病灶多半在本子任务自己的 pom）→ 不映射，留 fix 循环源头修，绝不把健康
    兄弟模块诬告成 upstream。版本不可判定（属性形态/读不到）→ 保守仍映射。"""
    if not build_output or not project_path:
        return set()
    arts: dict[str, str | None] = {}
    for m in _RX_MVN_MISSING_ARTIFACT.finditer(build_output):
        arts.setdefault(m.group(1), (m.group(2) or "").strip() or None)
    if not arts:
        return set()
    try:
        mods = _maven_modules(project_path)
    except Exception as exc:  # noqa: BLE001 — 树读取失败 → 无从映射，如实空集
        # 猎手 F2：这不是"确无内部模块"而是"无从判定"——必须可观测，否则②在读树
        # 故障时静默退回治本前旧行为（硬 FAIL 烧迭代）而无人察觉。
        logger.warning(
            "[L1.2.1] R63-T8② 坐标→模块映射读树失败（fail-open 空集，upstream 判定"
            "退回旧启发式）：project=%s err=%r", project_path, exc)
        return set()
    out: set[str] = set()
    for rel in mods:
        art = rel.split("/")[-1]
        if art not in arts:
            continue
        want = arts[art]
        have = _module_declared_version(project_path, rel) if want else None
        if want and have and "${" not in want and want != have:
            logger.warning(
                "[L1.2.1] R63-T8② 缺失坐标 %s:%s 与模块真身版本 %s 不符——判为引用方"
                "幻觉版本（非模块未就绪），不映射 upstream，留 fix 循环源头修",
                art, want, have)
            continue
        out.add(f"{rel}/pom.xml")
    return out


def _build_error_is_upstream(build_output: str, build_cmd: str,
                             modified: list[str] | None = None,
                             scope=None, project_path: str | None = None,
                             evidence_out: dict | None = None) -> bool:
    """构建错是否【非本子任务写的代码造成】（→ 标 BLOCKED 交 owner 修，不连坐本子任务）。

    R63-T8①：优先【scope 写权归属】确定性判据——报错文件与本子任务 FileScope 写权集
    （writable/create/delete）的归属是结构事实，比「是否在本轮 diff」更准：round63 形态
    （空 diff 轮 / 无 -pl 的全量构建命令）下旧启发式双双失灵 → 掉进 build_failed 硬 FAIL
    烧修复轮（st-8 三阶段各撞 95 迭代）。只要有一个报错文件在写权集内 → False（自己的错
    源头修，绝不推给上游）；全部无写权 → True。allow_any scope 无「写权外」概念，让位
    旧启发式。scope 未传（老调用方）→ 旧行为原样。

    旧启发式（保留兜底）：文件级 disjoint（报错文件 vs modified）→ 模块级（-pl vs 报错模块）。
    猎手 F4：判据来源写入 evidence_out["channel"]（scope/scope_error_fallback/file_disjoint/
    module_fallback/none），供调用点落进 details——下次同类死锁复盘不用再大海捞针。
    """
    def _ch(name: str) -> None:
        if evidence_out is not None:
            evidence_out["channel"] = name

    errs_files = _build_error_files(build_output)
    if project_path:
        errs_files = errs_files | _unresolved_internal_module_poms(build_output, project_path)
    if (errs_files and scope is not None
            and not getattr(scope, "allow_any", False)):
        try:
            _hit = any(scope.is_writable(f) for f in errs_files)
            _ch("scope")
            return not _hit
        except Exception as exc:  # noqa: BLE001 — scope 判定异常 → 落回旧启发式，绝不误 BLOCKED
            # 猎手 F1：必须可观测——scope 通道若因 scope 对象形态变化而失效，
            # 静默降级=退回 T8 治本前旧行为且无人察觉，直到下一次 95 迭代烧穿。
            logger.warning(
                "[L1.2.1] R63-T8① scope 写权判定异常（fail-open 落回旧启发式）："
                "scope_type=%s err=%r", type(scope).__name__, exc)
            _ch("scope_error_fallback")
    mods = {_norm_src_path(f) for f in (modified or []) if str(f).strip()}
    if errs_files and mods:
        if evidence_out is not None and evidence_out.get("channel") != "scope_error_fallback":
            _ch("file_disjoint")
        return errs_files.isdisjoint(mods)
    # 回退：模块级
    own = _pl_modules_from_cmd(build_cmd)
    errs = _build_error_modules(build_output)
    if not own or not errs:
        if evidence_out is not None and "channel" not in evidence_out:
            _ch("none")
        return False
    if evidence_out is not None and evidence_out.get("channel") != "scope_error_fallback":
        _ch("module_fallback")
    return own.isdisjoint(errs)


def _cmd_references_path(cmd: str, rel: str) -> bool:
    """路径是否以【完整 token 边界】出现在命令文本中。复核 CONFIRMED：裸子串会把
    `xmod-a/pom.xml.bak` / `a/mod-a/pom.xml` 误判成 `mod-a/pom.xml`——嵌套模块复用
    叶名是常见 Maven 布局，误判=误跳过合法断言=假绿。"""
    if not rel:
        return False
    return re.search(
        r"(^|[\s'\"=(])" + re.escape(rel) + r"($|[\s'\")])", cmd or "") is not None


def _is_h1_content_assert(cmd: str, rels) -> bool:
    """R65D-T2④：命令是否为「针对 H1 覆写文件的内容断言」。

    判定收窄到两形态交集（grep 正断言 / test -z|-n "$(grep…)" 负断言）×（命令文本
    以 token 边界引用被覆写文件路径）——构建命令（mvn validate 等）、工具类检查
    （test -f）、针对其他文件的断言全部不命中，绝不放大成 verify 面的免死金牌。
    复合命令（&&/;/|）不命中（fail-open：解析不了就照常执行，保考卷牙齿）。
    """
    c = (cmd or "").strip()
    if not (c.startswith("grep ")
            or re.match(r'^test\s+-[zn]\s+["\']?\$\(\s*grep', c)):
        return False
    return any(_cmd_references_path(c, r) for r in rels)


def _pom_structure_violations(pom_text: str, rel: str) -> list[str]:
    """#29-B（round65e12 死因·确定性兜底闸）：校验 worker 写的 Maven pom 是否结构合法。

    死因：worker 把 ruoyi-framework/pom.xml 写成 `<group>`（非 `<groupId>`）+丢 parent.groupId
    → `Malformed POM`/`parent.groupId is missing` → 毒 reactor → 下游 upstream_module_broken 连坐 10。
    既有机制只在 mvn compile 时才暴露（已烧沙箱+连坐后）。本闸在写后即校，拦在毒化前。

    校验（Maven 硬要求，栈中立仅 pom）：①XML 良构 ②有 <artifactId> ③groupId 可解析（自身
    <groupId> 或 <parent><groupId>）④<parent> 若在则 groupId/artifactId/version 齐全。
    非 *pom.xml → []（不误伤）。返回违规描述列表（空=合法）。"""
    if not str(rel).endswith("pom.xml"):
        return []
    import xml.etree.ElementTree as _ET
    text = (pom_text or "").strip()
    if not text:
        return []  # 空/读失败交调用方，不在此判死
    try:
        root = _ET.fromstring(text)
    except _ET.ParseError as e:
        return [f"{rel}: pom XML 解析失败（malformed，Maven 读不出）: {e}"]

    def _local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]  # 剥命名空间 {uri}tag → tag

    if _local(root.tag) != "project":
        return [f"{rel}: 根元素非 <project>（实为 <{_local(root.tag)}>）"]
    children = {_local(c.tag): c for c in root}
    viol: list[str] = []
    has_artifact = "artifactId" in children
    if not has_artifact:
        viol.append(f"{rel}: 缺 <artifactId>（Maven 必需）")
    parent = children.get("parent")
    parent_groupid = parent_version = False
    if parent is not None:
        pkids = {_local(c.tag) for c in parent}
        for req in ("groupId", "artifactId", "version"):
            if req not in pkids:
                viol.append(f"{rel}: <parent> 缺 <{req}>（parent 坐标不全，Maven 解析失败）")
        parent_groupid = "groupId" in pkids
        parent_version = "version" in pkids
    # groupId 可解析：自身 <groupId> 或 parent 继承
    if "groupId" not in children and not parent_groupid:
        _hint = "（疑把 <groupId> 误写成 <group> 等标签）" if "group" in children else ""
        viol.append(f"{rel}: groupId 不可解析（自身无 <groupId> 且 parent 未提供）{_hint}")
    # version 可解析：自身 <version> 或 parent 继承（复核 MED：Maven 无 version 同样 'version is missing'）
    if "version" not in children and not parent_version:
        viol.append(f"{rel}: version 不可解析（自身无 <version> 且 parent 未提供）")
    return viol


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
    template_enforced_rels: dict[str, str] | None = None,
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
        template_enforced_rels: R65D-T2④——H1 权威模板确定性覆写过的文件（内容即模板），
            形如 `{rel: 模板内容}`（X-M9 复核整改：注解曾是 `set[str]` 类型谎报，
            消费侧 6327 行起一直按 dict.items() 取内容做温差重比，本次仅修正注解）。
            对这些文件的内容断言（grep / test -z "$(grep…)"）是旧考卷考新模板，
            跳过并记 details["verify_skipped_h1"]（round65d st-26 冤案兜底；断言同源
            由规划期 reconcile_template_exam 保证）。其余 verify 命令照常执行。
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
    # ── L1.1c 结构截断闸（26 号文 C-1）──
    # 撞 Agent 迭代上限被强行截断的半成品此前一路放行到交付（实测 st-8）。
    # 与 L1.1b 同构：确定性、栈中立、纯文本零外部工具、判死带 reason 进重试 prompt。
    _trunc = _truncated_artifacts(diff)
    details["l1_1c_not_truncated"] = not _trunc
    if _trunc:
        details["truncated_artifacts"] = _trunc[:10]
        details["reason"] = "truncated_artifact"
        details["note"] = (
            "产物结构不完整（成对定界符收支从平衡变为不平衡）——多半是写到一半被掐断："
            + "；".join(f"{t['file']} {t['pair']} 差 {t['delta']}（{t['evidence']}）"
                       for t in _trunc[:3])
            + "。请重新完整输出该文件，绝不要留半个方法/半个标识符。")
        logger.warning("[L1] L1.1c 结构截断闸判死：%s", details["note"][:300])
        return False, details

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

    # ── L1.1c pom 结构闸（#29-B，round65e12 死因）──
    # worker 改动的 pom.xml 先校 Maven 必备坐标（groupId 可解析/artifactId/parent 完整/XML 良构），
    # 不合格当轮判死+带证据回灌——拦在毒化 reactor 之前（既有机制要到 mvn compile 才暴露=已连坐）。
    _pom_viol: list[str] = []
    _tpl_enforced = template_enforced_rels or {}
    for _rel in modified:
        # template_enforced 的 pom 由 H1 确定性模板覆写（模板天生结构合法）→ 校 worker 的
        # pre-overwrite 版本无意义（round65e12 死因 framework pom【非】template_enforced，仍被本闸拦）。
        if str(_rel).endswith("pom.xml") and str(_rel) not in _tpl_enforced:
            _ptxt = _read_project_file(project_path, str(_rel))
            if _ptxt is not None:  # 读失败(None)不误判，交后续 build 闸
                _pom_viol.extend(_pom_structure_violations(_ptxt, str(_rel)))
            else:
                # 复核（hunter）：改动的 pom 读不出（沙箱 cat 失败/瞬时）→ 本闸静默不校=盲区。
                # 不误判死（交 build 闸兜底），但【留痕】令"闸真跑过"与"闸没跑"可分。
                logger.warning("[L1.1c] #29-B 改动的 pom 读取失败，本轮结构闸跳过该文件"
                               "（交后续 build 闸兜底）: %s", _rel)
    details["l1_1c_pom_structure_ok"] = not _pom_viol
    if _pom_viol:
        details["pom_structure_violations"] = _pom_viol
        details["reason"] = "pom_structure_invalid"
        details["note"] = "; ".join(_pom_viol[:5])
        logger.warning("[L1.1c] #29-B pom 结构闸拦截毒 pom（拦在毒化 reactor 前）: %s", _pom_viol[:5])
        return False, details

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
    _compile_raw: dict = {}
    compile_ok, compile_msg = _compile_files(
        project_path, modified, timeout=timeout, raw_out=_compile_raw)
    details["l1_2_compile_ok"] = compile_ok
    details["compile_message"] = compile_msg
    if not compile_ok:
        # ★X-C3 第二个调用点（27 号文 §3.2）★ 没有它，node/ts 的 driver 就是死代码：
        # `_compile_files` 对 .ts 跑 `npx tsc --noEmit`，TS2307「Cannot find module
        # './routes/users'」→ rc≠0 且非 infra → 本处 hard-fail 早返 ⇒ 下面 L1.2.1 build 闸
        # （X-C3 的另一个调用点）**永不执行**。于是"引用了别的子任务还没建出的内部模块"这一
        # 头号死法在 node/ts 上原样保留：落 compile capability FAIL → 烧修复轮 → abandon →
        # 连坐。★这正是 B-4a CRITICAL-1（"治本接在哪条路上"必须用真实路由函数走一遍）的同族★，
        # 该批已为它赔过一整批，故本处按"数调用点、一个不落"补齐。
        # 判据与裁决全部复用 build 闸同一实现（`decide_unbuilt_internal_verdict`），
        # 分类器吃**未截断**原文（`_compile_raw`）——compile_msg 截到 1000 字符可能正好切掉
        # 那条第三方缺失行，"全或无"就会 fail-open 误标 BLOCKED。
        try:
            from swarm.brain.nodes.runtime_smoke import normalize_language_key
            from swarm.worker.l1_error_drivers import blocked_on_unbuilt_internal
            _c_lang = normalize_language_key((project_stack or {}).get("backend"))
            _c_text = _compile_raw.get("text") or compile_msg or ""
            _c_refs: list = []
            _c_disarm: dict = {}
            _c_pkgs, _c_syms = blocked_on_unbuilt_internal(
                _c_lang, _c_text, project_path, timeout, _run_check_split,
                refs_out=_c_refs, disarm_out=_c_disarm)
            _record_xc3_disarm(details, _c_disarm, stage="compile", lang=_c_lang)
            if _c_pkgs:
                if decide_unbuilt_internal_verdict(
                        details, getattr(subtask, "scope", None), _c_pkgs, _c_syms,
                        cmd="(L1.2 compile)", stage="compile", output=_c_text,
                        language_key=_c_lang, project_path=project_path,
                        timeout=timeout, run=_run_check_split,
                        driver_refs=_c_refs):
                    logger.warning(
                        "[L1.2] X-C3 %s 栈编译闸缺【尚未建出的项目内部标识】(error driver 判据)"
                        " → 与 JVM build 闸同口径标 BLOCKED 退避: containers=%s symbols=%s",
                        _c_lang, sorted(_c_pkgs)[:6], _c_syms[:6])
                    return True, details
        except Exception as _xc3_exc:  # noqa: BLE001 — 归因失败绝不改变原判（照常 FAIL）
            # ★复核 MED-4★ 原为 debug 级 + 零机读键 ⇒ X-C3 在 compile 闸整体死掉时与
            # "真没有内部缺失"不可分（硬检查四："空返回/缺席必须机读可辨"，norms 层实测
            # 死 12 天跨 5+ 轮全零无信号就是这么来的）。降级路径至少一个机读键 + 一次 WARNING。
            details["xc3_compile_attrib_error"] = f"{type(_xc3_exc).__name__}: {_xc3_exc}"[:200]
            logger.warning(
                "[L1.2] X-C3 编译闸归因异常（本轮不判 BLOCKED，照常 FAIL）: %r", _xc3_exc)
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
    # ★X-C2 复核 CRITICAL-1（治法 D，用户拍板）★ 判据不能只是"harness 没给命令"，还要
    # "harness 给的命令对**这个工程**适用"。`_infer_harness`（brain/nodes/shared.py）对整个
    # JVM 族恒发 `mvn -q compile`——它那一层只有 task_description + scope，**拿不到工程事实**
    # （有没有 pom / build.gradle），所以这不是疏忽而是信息上限。后果：gradle 工程收到 mvn 命令
    # → `_build_cmd_applicable` 实测 False → build 闸**整块跳过** ⇒ 零构建闸（改坏了也没人拦）。
    # 而下面的 `_derive_full_build_command` 判据是**真实清单 + 改动文件语言**（确定性），实测对
    # 同一个 gradle 工程给出正确的 `./gradlew -q classes` —— 正确答案一直在，只是被 harness 的
    # 兜底值挡住了。
    # ★maven 零回归可证★：`mvn` 对有 pom 的工程 applicable 恒 True ⇒ 第二个条件恒假 ⇒
    # derive 不被调用 ⇒ 唯一跑过 E2E 的栈路径逐字节不变（`build_command_derived` 也不会置位）。
    if not build_cmd or not _build_cmd_applicable(build_cmd, project_path):
        _derived = _derive_full_build_command(project_path, modified, project_stack)
        if _derived and _derived != build_cmd:
            if build_cmd:
                # 覆盖了 harness 的命令 ⇒ 必须留痕（否则"闸跑的是哪条命令"无从追溯）
                details["build_command_overridden"] = {"harness": build_cmd,
                                                       "derived": _derived}
                logger.warning(
                    "[L1.2.1] harness 下发的构建命令对本工程不适用（缺对应清单）→ 按真实清单"
                    "改派: %r → %r（X-C2 治法 D：否则 build 闸整块跳过＝零构建闸）",
                    build_cmd, _derived)
            build_cmd = _derived
            details["build_command_derived"] = build_cmd
    if build_cmd:
        # 治本(round8)：先把改动涉及的内部子模块补注册进根 pom <modules>(对账被跨子任务冲掉的
        # 注册)，再 scope——否则 _maven_modules 扫不到该模块、无法 -pl 收窄，退回全 reactor 必死。
        try:
            from swarm.worker.workspace_manifest import reconcile_workspace_manifests
            # F4：L1 在【活动共享树】上只补漏不摘幽灵——owner 先行登记(contract_utils 规则4)
            # 的模块目录物化在后，此时 prune 会误摘；幽灵清理留给 L2/交付两处定格树。
            # C3：本调用点不在任何 flock 内，与并行兄弟 pull-back 的锁内清单合并互踩
            # （lost-update）→ use_lock=True 把读-改-写整段收进 _ProjectGitFlock。
            _wm = reconcile_workspace_manifests(
                project_path, modified, prune=False, use_lock=True)
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
        build_cmd = _scope_maven_command(build_cmd, project_path, _pl_basis,
                                         details=details, phase="build")
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
    # R67L-B2（22号文批次2）：确定性修复轮的【剪账】——phantom-dep prune 剪掉的 plan 声明
    # 内部模块 artifactId 集（跨收敛轮累积），供 L1.3.5 验收阶段对账考卷断言（见 verify 段）。
    _repair_evidence: dict = {}
    if build_cmd and _build_cmd_applicable(build_cmd, project_path):
        if _deadline_blocked("build"):
            return True, details
        # R56-5：构建**之前**先过依赖合法性闸——坏坐标在进 Maven 前就被消掉（state-driven），
        # 而不是等它炸出 `Could not resolve` 再按错误文本逐形态打补丁（error-driven=打地鼠）。
        # ★X-C1 分档（对抗复核 MED）★ 判据从 `startswith("mvn")` 换成 Maven 系词元判断：
        # 旧判据下 `./mvnw …`（Spring Boot 生态默认形态）与 `cd m && mvn …` 都不 startswith
        # "mvn" → wrapper 工程**整类**绕过 R58-2 parent 字面量闸与 R56-5 依赖合法性闸，
        # 少两道确定性闸且零留痕。这两道闸都只读改工程 pom、与命令怎么写无关，
        # 唯一正确的门控就是"这是不是一次 Maven 构建"。
        if is_maven_family_command(build_cmd):
            try:
                # R58-2：parent 版本必须是字面量——它比依赖合法性更早、更致命（parent 解析不了
                # 连 pom 都读不出，谈不上依赖）。故排在合法性闸**之前**。
                _pv_n, _pv_files = _enforce_parent_version_literals(project_path, timeout)
                if _pv_files:
                    _rfp = details.setdefault("repaired_file_paths", [])
                    for _f in _pv_files:
                        if _f not in _rfp:
                            _rfp.append(_f)
            except Exception as _pv_exc:  # noqa: BLE001 —— 闸门自身绝不阻断构建
                logger.warning("[L1.2.1·parent-version] 字面量闸异常（跳过，不阻断）: %s", _pv_exc)
        # ★X-M10★ dep_legality 不再绑 Maven 命令族——闸内部按 manifest 在场分派
        # （pom/package.json/go.mod 各过各的），混合工程（java+npm 前后端分离）在 maven
        # 构建轮里 npm 侧同样过闸；无 driver 的栈 warn-once（D14 零覆盖可辨）。
        try:
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
        # A4：失败签名/no-progress 比对的机读键——未压缩错误行集，与人读展示分账。
        details["build_error_lines"] = extract_error_lines(b_out)
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
                            project_path, b_out, modified, timeout, project_stack,
                            evidence_out=_repair_evidence,
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
                # A4：rerun 后同步刷新机读键（签名吃最新错误行集，非首轮残留）。
                details["build_error_lines"] = extract_error_lines(b_out)
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
                # R63-T8①：传真 scope 做写权归属确定性判据（旧 diff/-pl 启发式在空 diff/
                # 无 -pl 形态下双双失灵）；project_path 供②坐标→pom 映射补依赖解析级盲区。
                _up_ev: dict = {}
                if _build_error_is_upstream(b_out, build_cmd, modified,
                                            scope=getattr(subtask, "scope", None),
                                            project_path=project_path,
                                            evidence_out=_up_ev):
                    # 结构化吐【阻断在哪些上游模块/文件】，供 brain 反查生产者子任务：若生产者已被
                    # 永久放弃(阶梯三打桩/revert)，则本下游不可恢复，应连坐放弃而非无限 replan。
                    _pom_blocked = _unresolved_internal_module_poms(b_out, project_path)
                    _bom = sorted(
                        _build_error_modules(b_out)
                        | {p.rsplit("/pom.xml", 1)[0] for p in _pom_blocked})
                    _bof = sorted(
                        _build_error_files(b_out) | _pom_blocked)
                    # 猎手 F4：判据通道留痕（scope/file_disjoint/module_fallback），复盘免捞
                    details["upstream_judge_channel"] = _up_ev.get("channel") or "unknown"
                    if not _bom and not _bof:
                        # R67L-B2（22号文批次2）：判归上游却吐不出任何可指名模块/文件
                        #（blocked_on 全空）=死端判词——没有可等的生产者，BLOCKED 退避只会
                        # 烧满重试/熔断配额空转。落回 FAIL 修复梯（fail-honest），留痕可观测。
                        details["upstream_deadend_no_evidence"] = True
                        logger.warning(
                            "[L1.2.1] R67L-B2 构建错判归上游但 blocked_on 全空（死端判词，"
                            "无可等的生产者）→ 不判 BLOCKED，落 FAIL 修复梯: %s",
                            (b_out or "")[:200])
                    else:
                        details["l1_2_1_build_ok"] = None
                        details["build_blocked"] = build_cmd
                        details["pipeline_blocked"] = "upstream_module_broken"
                        details["not_run_kind"] = NotRunKind.BLOCKED.value
                        details["blocked_on_modules"] = _bom
                        details["blocked_on_files"] = _bof
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
                # H-3a：【类级】判据——包已在树里但类未建出（cannot find symbol: class C /
                # location: package P）同属"生产者未落地"，统一 BLOCKED 退避口径
                # （round67 st-50-1 hard fail vs st-48 BLOCKED 的口径分裂，64cb44ed 实锤）。
                # A5（19_worker_flow_audit）：两判据【都跑】取证据并集——旧 `if not
                # _blocked_pkgs` 互斥消费会在「缺内部包 P1 + 缺包在树类 P2.C」同现时静默丢掉
                # 类级证据，brain 侧类级 futile 判据（_package_in_baseline 对该形态恒真失效）
                # 拿不到 C → 臆造类无法判 futile → BLOCKED 阶梯烧满而非快失败。
                _blocked_cls_pkgs = _build_blocked_on_unbuilt_internal_classes(
                    project_path, b_out, timeout)
                _blocked_cls: list[str] = []
                if _blocked_cls_pkgs:
                    # ★复核 HIGH 配套★：类级命中必须吐 blocked_on_classes（FQN 点分）——
                    # 包级 futile 判据（_package_in_baseline）对"包在树类未建出"恒真失效，
                    # brain 侧 _blocked_pkg_unrecoverable 需类级树判据防臆造类烧满阶梯。
                    _blocked_cls = sorted({
                        f"{p}.{c}" for c, p in parse_missing_symbol_classes(b_out)
                        if p in _blocked_cls_pkgs})
                _blocked_pkgs = _blocked_pkgs | _blocked_cls_pkgs
                # ★X-C3（27 号文 §3.2 CRITICAL）★ 上面两条判据的正则锚死 `.java` + Maven
                # `[行,列]` / javac 三行组 → Go `no required module provides package`、
                # TS `TS2307`、Rust `E0432`、Python `ModuleNotFoundError` **全部识别不出**
                # → 拿不到 internal_pkg_not_built → 落 build_failed capability 硬 FAIL →
                # 烧修复轮 → abandon → 连坐。**这正是 round38/round67 在 Java 上花十几轮治的
                # 头号死法，换栈完全复发。**
                # 走 `l1_error_drivers.ERROR_DRIVERS`（栈驱动层，判据与 JVM 同律：有第三方缺失
                # 或已在树里 → 全盘不标）。★JVM 恒走上面的专用链★——它的 driver 是 self-handled、
                # 通用求解器对 java 恒返空，故 **Java 路径逐字节不变**（唯一跑过 E2E 的栈）。
                _xc3_lang: str | None = None
                _xc3_refs: list = []
                if not _blocked_pkgs:
                    # ★复核 MED-5★ 必须包 try：本段有 import + 沙箱探针 + 正则，异常会逃出
                    # run_l1_pipeline（相邻的 #113 全角预扫、module-reg 对账、L1.2 同族调用点
                    # 都包了 try——本处原先是这条约定上的唯一裸奔者）。
                    try:
                        from swarm.brain.nodes.runtime_smoke import normalize_language_key
                        from swarm.worker.l1_error_drivers import (
                            blocked_on_unbuilt_internal as _xc3_solve,
                        )
                        _xc3_lang = normalize_language_key(
                            (project_stack or {}).get("backend"))
                        _xc3_disarm: dict = {}
                        _xc3_pkgs, _xc3_syms = _xc3_solve(
                            _xc3_lang, b_out, project_path, timeout, _run_check_split,
                            refs_out=_xc3_refs, disarm_out=_xc3_disarm)
                        _record_xc3_disarm(details, _xc3_disarm, stage="build",
                                           lang=_xc3_lang)
                        if _xc3_pkgs:
                            _blocked_pkgs = _xc3_pkgs
                            _blocked_cls = _xc3_syms
                            logger.warning(
                                "[L1.2.1] X-C3 %s 栈缺【尚未建出的项目内部标识】(error driver "
                                "判据) → 与 JVM 同口径标 BLOCKED 退避: containers=%s symbols=%s",
                                _xc3_lang, sorted(_xc3_pkgs)[:6], _xc3_syms[:6])
                    except Exception as _xc3_bexc:  # noqa: BLE001 — 归因失败不改原判
                        details["xc3_build_attrib_error"] = (
                            f"{type(_xc3_bexc).__name__}: {_xc3_bexc}")[:200]
                        logger.warning(
                            "[L1.2.1] X-C3 构建闸归因异常（本轮不判 BLOCKED，照常 FAIL）: %r",
                            _xc3_bexc)
                if decide_unbuilt_internal_verdict(
                        details, getattr(subtask, "scope", None),
                        _blocked_pkgs, _blocked_cls,
                        cmd=build_cmd, stage="build", output=b_out,
                        language_key=_xc3_lang, project_path=project_path,
                        timeout=timeout, run=_run_check_split,
                        driver_refs=_xc3_refs):
                    return True, details
                details["build_failed"] = build_cmd
                # #113 诊断：构建失败时预扫改动源文件的全角 CJK 标点（NVFP4 腐坏，javac illegal
                # character 常见根因）→ 精确坐标进 details，喂 worker 修复轮定位更准（只读，不自改）。
                try:
                    _fw = _scan_fullwidth_punct(project_path, modified, timeout)
                    if _fw:
                        details["fullwidth_punct_positions"] = _fw
                        logger.warning("[L1.2.1] #113 改动源文件含全角 CJK 标点（疑 NVFP4 腐坏，"
                                       "javac illegal character 根因）: %s", _fw[:5])
                except Exception as _fwexc:  # noqa: BLE001 — 诊断失败不致命
                    logger.debug("[L1.2.1] #113 全角标点预扫异常(跳过): %s", _fwexc)
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
        test_cmd = _scope_maven_command(test_cmd, project_path, _pl_t,
                                       details=details, phase="test")
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
    elif _is_npm_test_without_script(test_cmd, project_path):
        # ★W-7★ harness 显式 `npm test` 但项目无 scripts.test → 提前按 skipped 处理，
        # 避免运行后 `Missing script:` 被 _is_infra_failure 误判成 infra 故障重试。
        details["l1_3_test_ok"] = True
        details["test_skipped"] = f"package.json 无 test 脚本，跳过测试: {test_cmd}"
        logger.info("[L1.3] %s", details["test_skipped"])
    else:
        t_ec, t_out = _run_l1_command(test_cmd, project_path, timeout=_stage_timeout(timeout, deadline))
        test_ok = t_ec == 0
        details["l1_3_test_ok"] = test_ok
        # 智能压缩：提取关键失败信号行（FAILED/Error/Traceback/assert），
        # 替代盲目硬截断 —— 避免丢失位于输出末尾的 pytest 失败摘要。
        details["test_output"] = compress_tool_output(t_out, max_chars=1500)
        # A4：失败签名机读键——未压缩错误行集，与人读展示分账。
        details["test_error_lines"] = extract_error_lines(t_out)
        if t_ec == 124:
            details["test_output"] = "test timeout"
        # ★X-H2 复核 CRITICAL-2 配套★ pytest 的 rc=5 是"**一个用例都没收集到**"，按 pytest 自己的
        # 约定它不是失败。本批把测试面扩到多栈后，"猜出来的命令跑在没有用例的目录上"会拿到 rc=5
        # ⇒ 旧判据 `t_ec == 0` 为假 ⇒ 非 infra ⇒ **硬 FAIL 且 sticky**（换模型重试同死）。
        # 这是"猜错了命令"而非"代码坏了"，正确归类是"没测到"⇒ 与 test_skipped 同档。
        if (not test_ok) and t_ec == 5 and "pytest" in (test_cmd or ""):
            details["l1_3_test_ok"] = True
            details["test_skipped"] = f"pytest 未收集到用例（rc=5，非失败）: {test_cmd}"
            details["test_no_tests_collected"] = test_cmd
            logger.warning(
                "[L1.3] 猜出的测试命令在该目录下收集到 0 个用例（rc=5）→ 按『没测到』处理，"
                "不判死（X-H2：把猜错命令误判成代码失败会 sticky 硬 FAIL）: %s", test_cmd)
            test_ok = True
        if not test_ok:
            # TD2606：测试命中 infra 瞬时故障(网络/工具/资源) → BLOCKED 转 transient 重试，不误判
            # capability(错换模型)。与 L1.2.1 build gate 对称。timeout(124)按真失败处理(不放过)。
            if t_ec != 124 and _is_infra_failure(t_out):
                details["l1_3_test_ok"] = None
                details["test_blocked"] = test_cmd
                details["pipeline_blocked"] = "test_infra_failure"
                details["not_run_kind"] = NotRunKind.BLOCKED.value
                return True, details
            # ★X-C3 第三个调用点（复核 C-2）★ python 的 `ModuleNotFoundError` **只**在真 import
            # 时出现：`py_compile`/`compileall`（brain 给 python 的默认 build_command）都只做
            # 语法检查、缺 import 恒 rc=0（实测）。于是 PythonErrorDriver 原先在两个调用点都
            # 不可达＝死代码，而 registry 把 python 报成"已覆盖"——与本批已赔过一整批的
            # node/ts 死代码完全同型（硬检查①"接线覆盖 ≠ 机制存在"）。
            # 放在 infra 判据之后、scope 归属阶梯之前：缺内部模块既非 infra，也不该被
            # "报错文件全在写权外"的启发式先吞掉（那会丢结构化的 blocked_on_packages）。
            try:
                from swarm.brain.nodes.runtime_smoke import normalize_language_key
                from swarm.worker.l1_error_drivers import blocked_on_unbuilt_internal
                _t_lang = normalize_language_key((project_stack or {}).get("backend"))
                _t_refs: list = []
                _t_disarm: dict = {}
                _t_pkgs, _t_syms = blocked_on_unbuilt_internal(
                    _t_lang, t_out, project_path, timeout, _run_check_split,
                    refs_out=_t_refs, disarm_out=_t_disarm)
                _record_xc3_disarm(details, _t_disarm, stage="test", lang=_t_lang)
                if _t_pkgs:
                    if decide_unbuilt_internal_verdict(
                            details, getattr(subtask, "scope", None), _t_pkgs, _t_syms,
                            cmd=test_cmd, stage="test", output=t_out,
                            language_key=_t_lang, project_path=project_path,
                            timeout=timeout, run=_run_check_split,
                            driver_refs=_t_refs):
                        logger.warning(
                            "[L1.3] X-C3 %s 栈测试闸缺【尚未建出的项目内部标识】→ 与 build 闸"
                            "同口径标 BLOCKED 退避: containers=%s", _t_lang,
                            sorted(_t_pkgs)[:6])
                        return True, details
            except Exception as _t_xc3:  # noqa: BLE001 — 归因失败不改原判（照常 FAIL）
                details["xc3_test_attrib_error"] = f"{type(_t_xc3).__name__}: {_t_xc3}"[:200]
                logger.warning(
                    "[L1.3] X-C3 测试闸归因异常（本轮不判 BLOCKED，照常 FAIL）: %r", _t_xc3)
            # A3（19_worker_flow_audit）：test 闸补 scope 归属阶梯，与 build(P0-B/R63-T8①)/
            # lint(D33) 三闸口径统一。整树测试形态（无 harness.test_command 时 _guess_test_cmd
            # 兜底全量）下，兄弟子任务/基线的坏测试会连坐本子任务 hard FAIL（source=test
            # sticky 永不翻盘 → 换模型重试同死 → 阶梯烧穿）。报错文件全在写权集外 → 标 BLOCKED
            # 交 owner 退避，不烧本子任务修复轮。判据内部 fail-open（提取不到报错文件/判定
            # 异常 → False=不揽 upstream），只改"有正向证据全在写权外"的归类。
            # ★复核 H-1（hunter 实跑复现）★：test 与 build 有本质因果不对称——编译错报错的
            # 文件就是要修的文件（因果同体），测试失败报错的文件（测试文件）常常不是要修的
            # 文件（源码才是）。纯断言失败（traceback 只有测试文件帧）时「自己的回归打破
            # baseline 测试」与「baseline 测试本身坏」确定性不可分 → 一律归自己（fail-closed，
            # 保住修复轮拿 test_output 修自己回归的正确归因）。仅当报错文件含【写权集外非
            # 测试源码帧】（兄弟产物的源码）才启用归属阶梯。诚实边界：baseline 坏测试
            # （测试文件帧独占）不再享受连坐豁免——该形态 sticky hard FAIL 照旧。
            _up_ev_t: dict = {}
            _t_err_files = _build_error_files(t_out)
            try:
                from swarm.brain.nodes.shared import _is_test_file_path as _is_test_f

                # ★复核 R2-1（hunter R2 实跑复现）★：共享判据 `_is_test_file_path` 的目录
                # 分支要求前导斜杠（"/tests/" in pl），本闸输入是归一化相对路径 → 目录分支
                # 整体失效，tests/helpers.py、conftest.py 等测试辅助帧被误当"源码帧"，
                # 甩锅通道经 fixture/helper 链（pytest 失败的常态组成）复活。入口归一补
                # 前导斜杠 + conftest.py（pytest fixture 事实标准，任何层级）basename 特判。
                # 共享函数的相对路径盲区（A7 scope 剔除同病）已登记后续批 sibling。
                def _is_testish(f: str) -> bool:
                    rel = str(f).replace("\\", "/").lstrip("./")
                    return (_is_test_f("/" + rel)
                            or rel.rsplit("/", 1)[-1] == "conftest.py")

                _has_src_frame = any(not _is_testish(f) for f in _t_err_files)
            except Exception as _it_exc:  # noqa: BLE001
                # 判据不可用 → 不启用阶梯（fail-closed 归自己），但必须可观测
                logger.warning("[L1.3] A3 测试文件判据导入失败（归属阶梯本轮不启用）: %r", _it_exc)
                _has_src_frame = False
            if (_has_src_frame
                    and _build_error_is_upstream(t_out, test_cmd, modified,
                                                 scope=getattr(subtask, "scope", None),
                                                 project_path=project_path,
                                                 evidence_out=_up_ev_t)):
                details["l1_3_test_ok"] = None
                details["test_blocked"] = test_cmd
                details["pipeline_blocked"] = "upstream_module_broken"
                details["not_run_kind"] = NotRunKind.BLOCKED.value
                details["blocked_on_files"] = sorted(_t_err_files)
                # 复核 MEDIUM：补模块粒度账——brain `_producers_of` 的 mods 通道按顶层目录段
                # 反查生产者（只吐 blocked_on_files 时生产者链接无输入）。与 build 闸同源
                # （_build_error_modules layout 感知）；Python 扁平布局抽不出模块 → 如实空集。
                details["blocked_on_modules"] = sorted(_build_error_modules(t_out))
                details["upstream_judge_channel"] = _up_ev_t.get("channel") or "unknown"
                logger.warning(
                    "[L1.3] A3 测试失败报错文件全在本子任务写权集外（兄弟/基线坏测试连坐）"
                    "→ 标 BLOCKED 交 owner，不连坐本子任务: %s", (t_out or "")[:200],
                )
                return True, details
            return False, details

    # ── L1.3.5 harness 验收命令（verify_commands）——
    # Brain 为每条验收标准编写的烟雾测试/断言，硬阻断。这是"产出是否合格"的
    # 确定性证据，杜绝 LLM 口头自报合格。
    verify_cmds = list(getattr(harness, "verify_commands", []) or []) if harness else []
    if verify_cmds:
        if _deadline_blocked("verify_commands"):
            return True, details
        # R65E8-T1 复核 HIGH：验收命令 reactor 归一的 -pl 圈定基须与 build/test 同源——过滤掉 repair
        # 通道触达的外模块（R50-3），否则脚手架被别人在飞坏代码连坐（正是本 patch 要杀的病）。
        _rfp_v = {str(x).lstrip("./").lstrip("/")
                  for x in (details.get("repaired_file_paths") or [])}
        _pl_v = [f for f in modified
                 if str(f).lstrip("./").lstrip("/") not in _rfp_v] or modified
        # R65D-T2④：H1 覆写文件的内容断言跳过面。猎手 CRITICAL 整改：跳过判据不是
        # 「rel 曾被 H1 覆写」而是「verify 此刻内容仍等于模板」——同一 run 内 R56-5
        # 依赖合法性/version-repair 可能在 H1 之后合法改写该 pom（温差窗口），跨迭代
        # 集合也只增不减；内容一旦偏离模板，断言立即恢复牙齿，绝不留假绿窗口。
        _h1_tpl: dict[str, str] = {}
        if isinstance(template_enforced_rels, dict):
            _h1_tpl = {str(r).replace("\\", "/").lstrip("/"): t
                       for r, t in template_enforced_rels.items() if r and t}
        _h1_live: dict[str, bool] = {}

        def _h1_still_template(rel: str) -> bool:
            if rel not in _h1_live:
                _cur = _read_project_file(project_path, rel, timeout=15)
                _h1_live[rel] = (_cur is not None
                                 and _cur.strip() == _h1_tpl[rel].strip())
                if not _h1_live[rel]:
                    logger.info(
                        "[L1.3.5] R65D-T2 %s 在 H1 覆写后被后续机制改写（内容≠模板）"
                        "→ 对它的内容断言恢复照常执行", rel)
            return _h1_live[rel]

        verify_results = []
        for vc in verify_cmds:
            _hit = next((r for r in _h1_tpl
                         if _is_h1_content_assert(vc, {r})), None)
            if _hit is not None and _h1_still_template(_hit):
                # 文件内容确证=权威模板——对它的内容断言要么同义反复要么是陈旧卷
                # （round65d st-26：grep jackson 考 okhttp 模板必死=冤案）。
                # 跳过+机读留痕，绝不静默；构建/工具类命令不在此列。
                details.setdefault("verify_skipped_h1", []).append(vc)
                logger.info(
                    "[L1.3.5] R65D-T2 跳过 H1 权威模板覆写文件的内容断言"
                    "（模板即真值，旧考卷不考新模板）: %s", vc)
                continue
            # R65E8-T1：验收命令 reactor 归一（与 build_cmd/test_cmd 对称）——治 `cd 子模块 && 裸 mvn`
            # 解析不到 reactor 兄弟的假阴性（round65e8 烧正确代码重试预算→abandon 连坐清盘死因）。
            _vc_run = _reactorize_verify_command(vc, project_path, _pl_v, details)
            v_ec, v_out = _run_l1_command(
                _vc_run, project_path, timeout=_stage_timeout(timeout, deadline))
            ok = v_ec == 0
            verify_results.append({
                "cmd": vc, "cmd_run": _vc_run, "ok": ok,
                "output": compress_tool_output(v_out, max_chars=500),
            })
            if not ok:
                details["verify_commands"] = verify_results
                # TD2606：验收命令命中 infra 瞬时故障 → BLOCKED 转 transient 重试（与 build/test 对称）。
                # DR-04-F7：★对抗双复核裁定原改动（去 `v_ec != 124`）引入 bounded 回归，撤销★——
                # verify_commands 常是集成/冒烟轮询(轮询健康端点直到就绪)；被验代码有真实 bug 致端点
                # 永不就绪时，轮询会反复打印 `connection refused` 直到外层超时(124)。若让 124 过
                # _is_infra_failure，这类【真 capability 失败】会因输出含 timeout/refused 标记被误判
                # BLOCKED transient 退避烧配额、稀释真死因可观测性。故 124 显式排除在 infra 外、直判
                # verify_failed(capability 阶梯)——挂死的 verify 是 fail-closed 安全方向。
                if v_ec != 124 and _is_infra_failure(v_out):
                    details["pipeline_blocked"] = "verify_infra_failure"
                    details["not_run_kind"] = NotRunKind.BLOCKED.value
                    return True, details
                # R67L-B2（22号文批次2，round67l st-14 烧 4 轮实锤）：phantom-dep prune 剪掉的
                # 【plan 声明内部模块】若被本子任务验收命令断言（grep <artifactId>）→ prune 与
                # 考卷确定性自相矛盾：剪→验收挂→worker 加回→下轮再剪，注定永败。该模块的
                # 生产者（别的子任务）尚未 merge 进树=等上游，第一轮即判 BLOCKED 交 brain
                # （C9 动态边等生产者落地；生产者死=既有 futility 连坐放弃通道）——st-14 run-4
                # 自然进化到的正确处置提前到 run-1，修复轮零白烧。
                _pruned_pi = set(_repair_evidence.get("pruned_phantom_internal") or ())
                if _pruned_pi:
                    # 复核 L-5：artifactId 全形匹配（id 字符集 [\w.-] 外边界），防短名
                    # 子串误关联长名依赖（"alarm" 不得命中 "ruoyi-alarm-interface"）。
                    # 终扫 LOW：空串防御——空 pattern 在任意边界命中会全卷误判冲突。
                    _conflict = sorted(
                        a for a in _pruned_pi if a
                        and any(re.search(r"(?<![\w.-])" + re.escape(a) + r"(?![\w.-])",
                                          str(c)) for c in verify_cmds))
                    if _conflict:
                        details["pipeline_blocked"] = "upstream_module_broken"
                        details["not_run_kind"] = NotRunKind.BLOCKED.value
                        details["blocked_on_modules"] = _conflict
                        details["prune_acceptance_conflict"] = _conflict
                        logger.warning(
                            "[L1.3.5] R67L-B2 prune 判【永不可解析】剪掉的内部模块 %s 被本任务"
                            "验收断言必考 → prune↔考卷自相矛盾（注定永败），判 BLOCKED 等生产者"
                            "落地（生产者死则 brain 连坐放弃），不烧修复轮", _conflict)
                        return True, details
                details["verify_failed"] = vc
                return False, details
        details["verify_commands"] = verify_results

    # C4（阶段6，登记册 §五）：非空 diff 但既无 test_command 也无 verify_commands 的
    # 子任务——确定性验证面只剩编译（test skip 判过），语义正确性零覆盖。打 needs_review
    # 标记（deliver/人工闸可见）。
    # ★P-C1 复核 F2 ②'★ 旧注释自称"阻断语义由 det+llm conflict 分支承担"——**默认配置下
    # 该分支不存在**：`deterministic_llm_conflict`（l1_verdict.py）要求 `llm_ok is False`，
    # 而 LLM 自检 R63-T9 默认关闭（`_self_review_llm` 返 None ⇒ executor 侧 `llm_ok`
    # 保持 True 初值）⇒ conflict 永不触发。本标记在默认配置下**没有任何下游阻断**，
    # 止于 needs_review 可观测——create-pom 零验收直送 merge 的正经闸是规划期
    # `ensure_pom_create_min_acceptance`（R67L-B3⑤），不在本层。
    # 猎手 HIGH 整改（R65D-T2④伴生）：判据用【实际执行】的 verify 结果，不用 harness
    # 原始清单——全部内容断言被 H1 跳过时清单非空但零命令真跑，语义正确性零覆盖，
    # 必须打 needs_review（原样放行=假绿盲区）。
    _executed_verify = list(details.get("verify_commands") or [])
    if modified and not (getattr(harness, "test_command", "") if harness else "") \
            and not _executed_verify:
        details["needs_review"] = ("verify_all_skipped_h1"
                                   if details.get("verify_skipped_h1")
                                   else "no_test_or_verify_commands")

    # ── L1.4 LLM 自检（可选，不硬阻断） ──
    self_review_enabled = l1_self_review_enabled()
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
    elif "self_review" in details:
        # T9 猎手 F3（既有 clobber）：deadline 耗尽分支已写入更具体的
        # worker_deadline_exhausted 原因——不许被下面的通用 disabled 文案覆盖，
        # 否则 opt-in 的操作员会被误导去查 env 而不是查预算。
        pass
    elif not self_review_enabled:
        details["self_review"] = {
            "status": "disabled",
            "reason": "SWARM_WORKER_L1_SELF_REVIEW 未开启"
                      "（R63-T9 默认关闭：advisory 结论从不影响 verdict）",
        }
    else:
        details["self_review"] = {"status": "skipped", "reason": "llm not provided"}

    return True, details
