"""R53-1：Maven 坐标确定性解析（基线 → reactor → 权威仓库）。

## 为什么必须有这一层（round53 实锤，不是防御性编程）

契约层（大脑）会自由引入**基线之外**的第三方依赖（hutool-all / fastjson2 / lombok /
okhttp / jjwt…）。R47-2 立下铁律：**绝不伪造 groupId**（round47 死于模板把裸 artifact
回退成 `com.ruoyi:spring-boot-starter-web` 幽灵坐标，毒化整个 reactor）。但旧实现只留了
两条路——伪造(禁) 或 **省略**，于是：

1. 权威 pom 模板把每个模块的**每个**依赖都省略 → 新模块 pom = 空壳（round53：10/10 模块）；
2. 而脚手架验收标准仍写着"声明契约 dependencies **全部** artifacts" → **模板与验收自相矛盾**，
   worker 只能自己手写坐标（round52 的 replan LLM 明确抱怨过这条矛盾，它是对的）；
3. worker 手写 → 写出无 `<version>` 的坐标，甚至臆造 groupId（round53 实锤：
   `com.ruoyi:alarm-interface` 与 `com.alarm.platform:alarm-interface` 同一幻影两个 groupId）
   → `'dependencies.dependency.version' for …:jar is missing` → **Maven 连 reactor 都读不出**
   → 全体 worker 构建闸 BLOCKED → 编译验证失效 → 任务全灭。

本模块给出**第三条路：不伪造、也不省略——去权威仓库解析**。解析不到就如实丢弃（调用方须
连同验收标准一起丢弃，杜绝"逼 worker 造假"的矛盾）。离线/查不通 → 全部退回"省略"，与旧行为
一致（fail-honest：宁可缺依赖=可归因可修的编译错，绝不猜坐标=全 reactor 连坐）。

## 受管(managed)判定为什么必须算 BOM 传递闭包

`<version>` 写不写不是风格问题：父级 dependencyManagement **管得到**就不该写（写死会覆盖
工程统一版本），**管不到**就必须写（否则 pom 根本读不出）。RuoYi 基线 import 了
`spring-boot-dependencies` BOM——lombok/mysql-connector-j/slf4j/quartz 都被它管着（无 version
合法），而 hutool-all/fastjson2 **不在其中**（无 version 即致命）。只看根 pom 显式
dependencyManagement 会把这两类混为一谈，所以必须把 import 型 BOM 的受管集也拉进来。
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("swarm.brain.maven_registry")

# 网络超时短而硬：规划期不容许被仓库拖死；查不通=退回"省略"，不阻断。
_HTTP_TIMEOUT_S = float(os.getenv("SWARM_MAVEN_LOOKUP_TIMEOUT_S", "8"))
_SEARCH_URL = "https://search.maven.org/solrsearch/select"
_METADATA_MIRRORS = (
    "https://maven.aliyun.com/repository/public/{gpath}/{artifact}/maven-metadata.xml",
    "https://repo1.maven.org/maven2/{gpath}/{artifact}/maven-metadata.xml",
)
_BOM_MIRRORS = (
    "https://maven.aliyun.com/repository/public/{gpath}/{artifact}/{version}/{artifact}-{version}.pom",
    "https://repo1.maven.org/maven2/{gpath}/{artifact}/{version}/{artifact}-{version}.pom",
)

# 预发布版本词元：注入依赖必须落在稳定版上（M2/RC1/alpha 会把下游拖进不可复现的坑）
_PRERELEASE = re.compile(
    r"(?i)(?:^|[.\-_])(?:snapshot|alpha|beta|rc\d*|m\d+|cr\d+|ea|preview|pre|dev)(?:[.\-_]|\d|$)")

_http_cache: dict[str, str | None] = {}


def _lookup_enabled() -> bool:
    """SWARM_MAVEN_LOOKUP=0 → 关闭仓库联网解析（单测默认关：绝不让测试依赖网络/被 Central 拖慢，
    也杜绝"网络好就绿、离线就红"的假绿）。关闭后行为 = 解析不到 → 如实省略（旧行为）。"""
    return os.getenv("SWARM_MAVEN_LOOKUP", "1").strip().lower() not in ("0", "false", "no")


def _http_get(url: str) -> str | None:
    """GET 文本；任何失败（离线/超时/404）→ None。结果缓存（规划期同一 artifact 会被多模块问到）。"""
    if not _lookup_enabled():
        return None
    if url in _http_cache:
        return _http_cache[url]
    text: str | None = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "swarm-maven-resolver"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
            if 200 <= getattr(resp, "status", 200) < 300:
                text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        logger.debug("[maven-registry] GET %s 失败: %s", url, exc)
    _http_cache[url] = text
    return text


def _strip_comments(xml: str) -> str:
    return re.sub(r"<!--.*?-->", "", xml, flags=re.S)


def _stable_versions(versions: list[str]) -> list[str]:
    return [v for v in versions if not _PRERELEASE.search(v)]


def _ver_key(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-_]", v)[:4])


# ── 基线索引（纯文本，零网络） ──────────────────────────────────────────────
@dataclass
class BaselineIndex:
    # known=False 表示【根 pom 都读不到】——此时无从判断某依赖是否受管，就**没有资格**
    # 断言"它无版本必炸"→ 保持旧行为（原样保留坐标，不写版本），绝不把好依赖也丢光。
    # 丢弃只在【确知不受管且版本解析不到】时才成立（那是可证的必炸，留着会让整树读不出）。
    known: bool = False
    # 受管集是否**完整**：只要有一个 import 型 BOM 没拉到（离线/私服不可达），我们就不知道
    # 它管了哪些坐标 → **没有资格断言某依赖"不受管"** → 丢弃规则必须让路（退回旧行为）。
    # 否则一次网络抖动就会把 lombok/spring-boot-starter-* 这类真受管依赖全部误丢 → 空壳 pom。
    managed_complete: bool = True
    project_group: str | None = None
    module_artifacts: set[str] = field(default_factory=set)   # reactor 内部模块 artifactId
    # 受管集必须**带权威 groupId**（artifact → group）：父级/BOM 本就写着坐标，拿它当
    # groupId 的第一权威既准确又零网络——只存名字会退化成"还得去仓库反查"，离线即丢依赖。
    managed: dict[str, str] = field(default_factory=dict)
    dep_groups: dict[str, set[str]] = field(default_factory=dict)  # artifactId → 基线出现过的 groupId 证据
    bom_imports: list[tuple[str, str, str]] = field(default_factory=list)  # (g, a, v) import 型 BOM

    def is_module(self, artifact: str) -> bool:
        return artifact in self.module_artifacts

    def is_managed(self, artifact: str) -> bool:
        return artifact in self.managed


def _resolve_props(text: str, value: str) -> str:
    """把 ${prop} 展开成 <properties> 里的字面值（单层足够：BOM 版本惯例即单层）。"""
    m = re.fullmatch(r"\$\{([^}]+)\}", value.strip())
    if not m:
        return value.strip()
    prop = re.escape(m.group(1))
    pm = re.search(rf"<{prop}>\s*([^<\s]+)\s*</{prop}>", text)
    return pm.group(1) if pm else value.strip()


def index_baseline(project_path: str, *, with_bom: bool = True) -> BaselineIndex:
    """扫描基线 poms（根 + 单层子模块）建立坐标索引。with_bom=False 时跳过 BOM 联网展开。"""
    idx = BaselineIndex()
    root = Path(project_path)
    root_pom = root / "pom.xml"
    idx.known = root_pom.is_file()
    poms: list[Path] = [root_pom] if idx.known else []
    try:
        poms += sorted(root.glob("*/pom.xml"))
    except OSError:
        pass

    for i, pom in enumerate(poms):
        try:
            txt = _strip_comments(pom.read_text("utf-8", errors="replace"))
        except OSError:
            continue
        body = re.sub(r"<parent>.*?</parent>", "", txt, flags=re.S)
        body = re.sub(r"<dependencyManagement>.*?</dependencyManagement>", "", body, flags=re.S)
        body = re.sub(r"<dependencies>.*?</dependencies>", "", body, flags=re.S)
        body = re.sub(r"<build>.*?</build>", "", body, flags=re.S)
        own = re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", body)
        if own:
            idx.module_artifacts.add(own.group(1))
        if i == 0:
            og = re.search(r"<groupId>\s*([^<\s]+)\s*</groupId>", body)
            idx.project_group = og.group(1) if og else None
            for m in re.findall(r"<module>\s*([^<\s]+)\s*</module>", txt):
                idx.module_artifacts.add(m.rstrip("/").rsplit("/", 1)[-1])
            mgmt = re.search(r"<dependencyManagement>(.*?)</dependencyManagement>", txt, re.S)
            if mgmt:
                for blk in re.finditer(r"<dependency>(.*?)</dependency>", mgmt.group(1), re.S):
                    b = re.sub(r"<exclusions>.*?</exclusions>", "", blk.group(1), flags=re.S)
                    a = re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", b)
                    if not a:
                        continue
                    g = re.search(r"<groupId>\s*([^<\s]+)\s*</groupId>", b)
                    if g and not g.group(1).startswith("${"):
                        idx.managed.setdefault(a.group(1), g.group(1))
                    v = re.search(r"<version>\s*([^<]+?)\s*</version>", b)
                    if g and v and re.search(r"<scope>\s*import\s*</scope>", b):
                        idx.bom_imports.append(
                            (g.group(1), a.group(1), _resolve_props(txt, v.group(1))))
        for blk in re.finditer(r"<dependency>(.*?)</dependency>", txt, re.S):
            b = re.sub(r"<exclusions>.*?</exclusions>", "", blk.group(1), flags=re.S)
            a = re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", b)
            g = re.search(r"<groupId>\s*([^<\s]+)\s*</groupId>", b)
            if a and g and not g.group(1).startswith("${"):
                idx.dep_groups.setdefault(a.group(1), set()).add(g.group(1))

    if with_bom:
        for g, a, v in list(idx.bom_imports):
            got = bom_managed_artifacts(g, a, v)
            if not got:
                # 拉不到这张 BOM → 受管集不完整 → 丧失"断言某依赖不受管"的资格（见 managed_complete）
                idx.managed_complete = False
                continue
            for art, grp in got.items():
                idx.managed.setdefault(art, grp)   # 根 pom 显式声明优先于 BOM（就近覆盖）
    return idx


def bom_managed_artifacts(group: str, artifact: str, version: str) -> dict[str, str]:
    """import 型 BOM 的受管坐标 {artifactId: groupId}：本地 ~/.m2 优先（离线可用），再走仓库镜像。"""
    if not (group and artifact and version) or version.startswith("${"):
        return {}
    local = (Path.home() / ".m2" / "repository" / Path(*group.split("."))
             / artifact / version / f"{artifact}-{version}.pom")
    text: str | None = None
    try:
        if local.is_file():
            text = local.read_text("utf-8", errors="replace")
    except OSError:
        text = None
    if text is None:
        gpath = group.replace(".", "/")
        for tpl in _BOM_MIRRORS:
            text = _http_get(tpl.format(gpath=gpath, artifact=artifact, version=version))
            if text:
                break
    if not text:
        logger.warning(
            "[maven-registry] BOM %s:%s:%s 取不到（离线？）→ 其受管集未知，"
            "相关依赖将按【不受管】处理（写显式版本，宁可显式也不产出读不出的 pom）",
            group, artifact, version)
        return {}
    mgmt = re.search(r"<dependencyManagement>(.*?)</dependencyManagement>",
                     _strip_comments(text), re.S)
    scope = mgmt.group(1) if mgmt else _strip_comments(text)
    out: dict[str, str] = {}
    for blk in re.finditer(r"<dependency>(.*?)</dependency>", scope, re.S):
        b = re.sub(r"<exclusions>.*?</exclusions>", "", blk.group(1), flags=re.S)
        a = re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", b)
        g = re.search(r"<groupId>\s*([^<\s]+)\s*</groupId>", b)
        if a and g and not g.group(1).startswith("${"):
            out.setdefault(a.group(1), g.group(1))
    return out


# ── 注册中心解析（网络，失败即 None） ────────────────────────────────────────
def local_m2_groups_for(artifact: str) -> set[str]:
    """本地 ~/.m2 里**真实存在**该 artifact 的 groupId 集合（目录结构即证据，零网络）。"""
    root = Path.home() / ".m2" / "repository"
    found: set[str] = set()
    try:
        for d in root.rglob(artifact):
            if not d.is_dir():
                continue
            # 版本目录里有同名 .pom 才算数（排除同名中间目录）
            if not any((v / f"{artifact}-{v.name}.pom").is_file()
                       for v in d.iterdir() if v.is_dir()):
                continue
            found.add(".".join(d.relative_to(root).parts[:-1]))
    except OSError:
        return set()
    return found


def registry_group_for(artifact: str) -> str | None:
    """按 artifactId 反查 groupId。唯一 → 采信；多候选 → 用**本地 ~/.m2 证据**消歧；仍不唯一 → None。

    R54-1（round54 实测）：Central 上 `okhttp` 有 20 个 groupId 候选、`mybatis-plus-boot-starter` 9 个、
    `hutool-all` 3 个（fork / 镜像 / shaded 重打包满天飞）→ 原来的"一有歧义就全丢"把**常用依赖也丢光**，
    模板偏薄，只能靠 worker 手写 + L1 事后补救（多烧一轮构建）。
    消歧证据必须是**事实而非偏好**：本地仓库里真实存在哪个 group 的这个 artifact——那是本机生态
    真正在用的那一个（同时天然离线可用）。本地也不唯一 → 仍然 None（绝不猜）。
    """
    if not _lookup_enabled():
        return None
    q = urllib.parse.urlencode({"q": f'a:"{artifact}"', "rows": "20", "wt": "json"})
    raw = _http_get(f"{_SEARCH_URL}?{q}")
    groups: set[str] = set()
    if raw:
        try:
            docs = (json.loads(raw).get("response") or {}).get("docs") or []
            groups = {d.get("g") for d in docs if d.get("a") == artifact and d.get("g")}
        except (ValueError, AttributeError):
            groups = set()
    if len(groups) == 1:
        return groups.pop()

    local = local_m2_groups_for(artifact)
    if groups:
        overlap = local & groups          # 本地有 ∩ Central 有 → 最强证据
        if len(overlap) == 1:
            g = overlap.pop()
            logger.info("[maven-registry] R54-1 %r Central 有 %d 个 groupId 候选 → 用本地 m2 证据"
                        "消歧为 %s（本机生态真实在用）", artifact, len(groups), g)
            return g
        logger.info("[maven-registry] %r 在 Central 有 %d 个 groupId 候选、本地 m2 %d 个 → "
                    "仍不唯一，存疑弃用（不猜）", artifact, len(groups), len(local))
        return None
    # Central 查不通（离线）→ 纯本地证据唯一即可采信
    if len(local) == 1:
        g = local.pop()
        logger.info("[maven-registry] R54-1 Central 不可达 → 用本地 m2 唯一证据解析 %r → %s",
                    artifact, g)
        return g
    return None


def local_m2_latest_version(group: str, artifact: str) -> str | None:
    """本地 ~/.m2 已有的最新稳定版。

    为什么必须先问本地：规划期联网若抖动/被墙，坐标解析全线失败 → 依赖被丢弃 → 又退回
    空壳 pom（正是本次要治的病）。本地仓库里**已经存在**的版本是"确定能构建"的最强证据，
    比 Central 最新版更保险（不会引入未下载过的版本）。与网络查询同受 SWARM_MAVEN_LOOKUP
    开关约束，保证单测确定性。"""
    if not _lookup_enabled():
        return None
    d = Path.home() / ".m2" / "repository" / Path(*group.split(".")) / artifact
    try:
        if not d.is_dir():
            return None
        vers = [p.name for p in d.iterdir()
                if p.is_dir() and (p / f"{artifact}-{p.name}.pom").is_file()]
    except OSError:
        return None
    stable = _stable_versions(vers)
    return max(stable, key=_ver_key) if stable else None


def registry_latest_version(group: str, artifact: str) -> str | None:
    """版本解析：本地 ~/.m2（确定能构建）→ maven-metadata（aliyun→Central）。查不到 → None。"""
    local = local_m2_latest_version(group, artifact)
    if local:
        return local
    gpath = group.replace(".", "/")
    for tpl in _METADATA_MIRRORS:
        raw = _http_get(tpl.format(gpath=gpath, artifact=artifact))
        if not raw:
            continue
        versions = [v.strip() for v in re.findall(r"<version>([^<]+)</version>", raw) if v.strip()]
        stable = _stable_versions(versions)
        if stable:
            rel = re.search(r"<release>\s*([^<\s]+)\s*</release>", raw)
            if rel and rel.group(1) in stable:
                return rel.group(1)
            return max(stable, key=_ver_key)
    return None


# ── 对外主入口 ──────────────────────────────────────────────────────────────
@dataclass
class ResolvedDep:
    group: str
    artifact: str
    version: str | None   # None = 父级受管，按 Maven 惯例不写版本
    source: str           # baseline | reactor | registry | explicit


def resolve_artifacts(project_path: str, artifacts: list[str],
                      idx: BaselineIndex | None = None,
                      ) -> tuple[list[ResolvedDep], list[str]]:
    """把契约 artifacts（裸名或 g:a[:v]）解析成可写入 pom 的坐标。

    返回 (kept, dropped)。**dropped 必须同时从契约/验收标准里剔除**——否则又造出
    "模板没有、验收却要求"的矛盾，逼 worker 手写幻影坐标（R53 死因）。

    判定序（每步都有权威证据，无一步靠猜）：
      1. 显式 `g:a[:v]` → 直采（版本缺省则按下面 3 补）。
      2. groupId：基线依赖块证据（唯一第三方 group）→ reactor 内部模块（工程 group）
         → Central 按 artifactId 反查（唯一才采信）→ 都不成 → drop。
      3. version：父级受管（含 import BOM 传递闭包）→ 不写；reactor 兄弟模块 → `${project.version}`；
         其余 → 仓库最新稳定版；查不到 → drop（**绝不产出无版本又无人管的依赖**：那会让
         Maven 连 reactor 都读不出，是比缺依赖严重一个数量级的全局故障）。
    """
    index = idx if idx is not None else index_baseline(project_path)
    kept: list[ResolvedDep] = []
    dropped: list[str] = []
    seen: set[tuple[str, str]] = set()

    for raw in artifacts:
        spec = str(raw).strip()
        if not spec:
            continue
        group: str | None = None
        version: str | None = None
        source = "registry"
        if ":" in spec:
            parts = [p.strip() for p in spec.split(":")]
            group, artifact = parts[0], parts[1]
            version = parts[2] if len(parts) > 2 and parts[2] else None
            source = "explicit"
        else:
            artifact = spec
            evidence = {g for g in index.dep_groups.get(artifact, set())
                        if g != index.project_group}
            if index.is_managed(artifact):
                # 父级/BOM 的受管块本就写着权威坐标 → groupId 第一权威，零网络、零歧义
                group, source = index.managed[artifact], "baseline"
            elif len(evidence) == 1:
                group, source = evidence.pop(), "baseline"
            elif index.is_module(artifact) and index.project_group:
                group, source = index.project_group, "reactor"
            elif not evidence:
                group = registry_group_for(artifact)
                source = "registry"
        if not group:
            dropped.append(spec)
            continue

        if version is None:
            if index.is_managed(artifact):
                version = None            # 父级（含 BOM）管得到 → 按惯例不写
            elif index.is_module(artifact) and group == index.project_group:
                version = "${project.version}"   # reactor 兄弟：与父同版，确定性且不写死
            elif not (index.known and index.managed_complete):
                # 基线未知 / 受管集不完整（BOM 拉不到）→ 无资格判"必炸" → 保持旧行为（不写版本）
                version = None
            else:
                version = registry_latest_version(group, artifact)
                if not version:
                    # 确知不受管 + 版本解析不到 = **可证必炸**（pom 解析期错，整 reactor 读不出）→
                    # 如实丢弃。安全性来自下游：worker 的 L1 防线④会按源码里**真实的 import**
                    # 去 Central 反查坐标把依赖补回来（带版本），所以"丢"不等于"永远缺"；而留一个
                    # 无版本又无人管的坐标，是可证的全局连坐（三轮实测：整棵 reactor 读不出）。
                    dropped.append(spec)
                    continue
        if (group, artifact) in seen:
            continue
        seen.add((group, artifact))
        kept.append(ResolvedDep(group=group, artifact=artifact, version=version, source=source))

    if dropped:
        logger.warning(
            "[maven-registry] R53-1 %d 个契约依赖无法确定性解析坐标/版本 → 如实丢弃"
            "（同时从验收标准剔除，绝不逼 worker 手写臆造坐标）: %s",
            len(dropped), dropped)
    return kept, dropped
