"""通用 workspace/聚合清单对账（确定性、幂等、模型无关）。

多模块工程的【聚合清单】都须枚举所有成员模块：
  - Maven   : 根 pom.xml `<modules><module>`
  - Gradle  : settings.gradle(.kts) `include`
  - Rust    : 根 Cargo.toml `[workspace] members`
  - .NET    : *.sln `Project(...)` 条目
  - Go      : go.work `use ./x`（多模块工作区）

并行子任务各自在【独立沙箱】里改这个共享清单 → pull-back 整文件覆盖 → 后注册的把先注册的
【冲掉】(last-write-wins) → 成员丢失 → reactor/构建找不到该模块 → 确定性失败（与代码无关）。
逐子任务打地鼠赢不了这个并发竞态。

治本 = 不打地鼠，而是【对账 ground truth】：磁盘上真实存在哪些成员模块目录(各有自己的成员清单
文件)，就让聚合清单枚举哪些。三处复用同一核心：
  ① 子任务 L1 构建闸门(沙箱内，使其能据成员 -pl/收窄构建)；
  ② L2 集成验证(合并库 apply 后、构建前，使集成构建不因被冲掉的清单【假失败】)；
  ③ 交付 commit 前(合并库上，把对账结果写进交付产物，持久化、杜绝 race 残留)。

仅处理【显式成员列表】型清单——glob 型(Node `"workspaces": ["packages/*"]`、pnpm、Python
`pyproject` workspace globs)会自愈，不碰。保守、绝不臆造结构：聚合清单不存在/格式异常/疑似
【动态枚举】一律跳过，绝不创建新清单、绝不改写既有非成员区。全程无 LLM、幂等、可复现。
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# 遍历时跳过的重目录（构建产物/依赖/VCS），避免误把它们当成员或拖慢扫描。
_SKIP_DIRS = {
    "target", "build", "out", "bin", "obj", "dist", "node_modules",
    ".git", ".idea", ".vscode", ".gradle", ".mvn", "vendor", "__pycache__",
}


def reconcile_workspace_manifests(
    project_path: str, modified: list[str] | None = None
) -> dict:
    """对账项目内所有【显式成员列表】型聚合清单，使其枚举磁盘上真实存在的成员模块。

    确定性、幂等、模型无关。返回:
        {"modified_manifests": [清单相对路径...],
         "added": {清单相对路径: [新增成员标识...]}}
    `modified` 仅作候选提示，真正驱动是磁盘 ground-truth 扫描，故传 None 也正确。
    任一生态的对账抛错都被隔离吞掉(增益层不可拖垮主流程)，其它生态照常对账。
    """
    root = Path(project_path)
    if not root.is_dir():
        return {"modified_manifests": [], "added": {}}
    hint = [str(m or "") for m in (modified or [])]
    modified_manifests: list[str] = []
    added: dict[str, list[str]] = {}
    for fn in (_reconcile_maven, _reconcile_maven_dep_versions, _reconcile_gradle,
               _reconcile_cargo, _reconcile_dotnet_sln, _reconcile_go_work):
        try:
            mods, adds = fn(root, hint)
        except Exception as exc:  # noqa: BLE001 —— 增益层：单生态失败不影响其它与主流程
            logger.debug("[workspace-manifest] %s 对账跳过(异常,不致命): %s", fn.__name__, exc)
            continue
        for m in mods:
            if m not in modified_manifests:
                modified_manifests.append(m)
        for k, v in adds.items():
            if v:
                added.setdefault(k, []).extend(v)
    return {"modified_manifests": modified_manifests, "added": added}


def _rel(root: Path, p: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return p.name


def _safe_subdirs(d: Path) -> list[Path]:
    """d 的直接子目录(跳过重目录/隐藏目录)。"""
    out: list[Path] = []
    try:
        for c in d.iterdir():
            if c.is_dir() and c.name not in _SKIP_DIRS and not c.name.startswith("."):
                out.append(c)
    except OSError:
        pass
    return out


def _read(p: Path) -> str | None:
    try:
        return p.read_text("utf-8", errors="ignore")
    except OSError:
        return None


# ───────────────────────────── Maven ─────────────────────────────
def _maven_aggregators(root: Path) -> list[Path]:
    """所有【聚合器】pom(含 <modules> 块)目录。覆盖根 + 嵌套聚合器。"""
    out: list[Path] = []
    stack = [root]
    while stack:
        d = stack.pop()
        pom = d / "pom.xml"
        if pom.is_file():
            t = _read(pom) or ""
            if re.search(r"<modules>.*?</modules>", t, re.S):
                out.append(d)
        stack.extend(_safe_subdirs(d))
    return out


def _reconcile_maven(root: Path, hint: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """对每个聚合器 pom：其直接子目录里【声明 <parent> 的子模块】须列入 <modules>。"""
    modified: list[str] = []
    added: dict[str, list[str]] = {}
    for agg in _maven_aggregators(root):
        pom = agg / "pom.xml"
        text = _read(pom)
        if text is None:
            continue
        mblock = re.search(r"<modules>(.*?)</modules>", text, re.S)
        if not mblock:
            continue
        registered = set(re.findall(r"<module>\s*([^<\s]+)\s*</module>", mblock.group(1)))
        new_members: list[str] = []
        for child in _safe_subdirs(agg):
            name = child.name
            if name in registered:
                continue
            cpom = child / "pom.xml"
            if not cpom.is_file():
                continue
            ctext = _read(cpom) or ""
            # 仅注册【本工程子模块】(声明 <parent ...>，含自闭合 <parent/>)；独立工程目录不碰
            if "<parent" not in ctext:
                continue
            new_members.append(name)
            registered.add(name)
        if not new_members:
            continue
        insert = "".join(f"        <module>{m}</module>\n" for m in new_members)
        new_text = text.replace("</modules>", insert + "    </modules>", 1)
        try:
            pom.write_text(new_text, encoding="utf-8")
        except OSError:
            continue
        rel = _rel(root, pom)
        modified.append(rel)
        added[rel] = new_members
    return modified, added


# ───────────────── Maven dependencyManagement 版本对账（D2）──────────────────
def _tag(text: str, tag: str) -> str | None:
    """抽首个 <tag>值</tag>（值非空、单行）。"""
    m = re.search(rf"<{tag}>\s*([^<\s][^<]*?)\s*</{tag}>", text)
    return m.group(1).strip() if m else None


def _all_poms(root: Path) -> list[Path]:
    out: list[Path] = []
    stack = [root]
    while stack:
        d = stack.pop()
        pom = d / "pom.xml"
        if pom.is_file():
            out.append(pom)
        stack.extend(_safe_subdirs(d))
    return out


def _maven_pom_coords(text: str) -> tuple[str, str, str] | None:
    """模块【自身】坐标 (groupId, artifactId, version)——version/groupId 缺省时继承 <parent>。

    先剥离 parent/dependencyManagement/dependencies/build 块，避免误取嵌套的 artifactId。
    """
    parent = re.search(r"<parent>(.*?)</parent>", text, re.S)
    pblock = parent.group(1) if parent else ""
    body = (text[:parent.start()] + text[parent.end():]) if parent else text
    body = re.sub(r"<dependencyManagement>.*?</dependencyManagement>", "", body, flags=re.S)
    body = re.sub(r"<dependencies>.*?</dependencies>", "", body, flags=re.S)
    body = re.sub(r"<build>.*?</build>", "", body, flags=re.S)
    artifact = _tag(body, "artifactId")
    if not artifact:
        return None
    group = _tag(body, "groupId") or _tag(pblock, "groupId")
    version = _tag(body, "version") or _tag(pblock, "version")
    if not (group and version):
        return None
    return (group, artifact, version)


def _maven_direct_deps(text: str) -> list[tuple[str, str, bool]]:
    """模块的【运行时依赖】(g, a, 是否带 version)——排除 parent/dependencyManagement/build。"""
    t = re.sub(r"<parent>.*?</parent>", "", text, flags=re.S)
    t = re.sub(r"<dependencyManagement>.*?</dependencyManagement>", "", t, flags=re.S)
    t = re.sub(r"<build>.*?</build>", "", t, flags=re.S)
    out: list[tuple[str, str, bool]] = []
    for dblock in re.findall(r"<dependencies>(.*?)</dependencies>", t, re.S):
        for dep in re.findall(r"<dependency>(.*?)</dependency>", dblock, re.S):
            g, a = _tag(dep, "groupId"), _tag(dep, "artifactId")
            if g and a:
                out.append((g, a, bool(_tag(dep, "version"))))
    return out


def _managed_pairs(text: str) -> set[tuple[str, str]]:
    """该 pom 的 <dependencyManagement> 已管理的 (groupId, artifactId) 集合。"""
    pairs: set[tuple[str, str]] = set()
    dm = re.search(r"<dependencyManagement>(.*?)</dependencyManagement>", text, re.S)
    if dm:
        for dep in re.findall(r"<dependency>(.*?)</dependency>", dm.group(1), re.S):
            g, a = _tag(dep, "groupId"), _tag(dep, "artifactId")
            if g and a:
                pairs.add((g, a))
    return pairs


def _reconcile_maven_dep_versions(root: Path, hint: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """把【本工程子模块】(声明 <parent>) 的 g:a:version 补进聚合器 root 的 <dependencyManagement>。

    治本 round18 §3：模块间内部依赖常【缺省 version】(如 ruoyi-admin 依赖 ruoyi-alarm 不写版本)，
    root dependencyManagement 又未声明其版本 → reactor 解析失败 → compile 失败，且无机制补回。
    据磁盘 ground-truth 补版本(= 模块自身/继承的项目版本)，使任何版本缺省的内部依赖可解析。
    保守：仅补进【已存在】的 <dependencyManagement><dependencies> 块，绝不臆造该块(无块交闸门 fail-closed)。
    确定性、幂等(已管理的 g:a 跳过)、模型无关。
    """
    modified: list[str] = []
    added: dict[str, list[str]] = {}
    for agg in _maven_aggregators(root):
        pom = agg / "pom.xml"
        text = _read(pom)
        if text is None:
            continue
        dm = re.search(
            r"(<dependencyManagement>\s*<dependencies>)(.*?)(</dependencies>\s*</dependencyManagement>)",
            text, re.S,
        )
        if not dm:
            continue  # 无 depMgmt 块 → 保守跳过（不臆造结构）
        managed = {
            (g, a) for g, a in (
                (_tag(d, "groupId"), _tag(d, "artifactId"))
                for d in re.findall(r"<dependency>(.*?)</dependency>", dm.group(2), re.S)
            ) if g and a
        }
        new_entries: list[tuple[str, str, str]] = []
        for cpom in _all_poms(agg):
            if cpom == pom:
                continue
            ctext = _read(cpom) or ""
            if "<parent" not in ctext:  # 仅【本工程子模块】(独立工程不碰)
                continue
            coords = _maven_pom_coords(ctext)
            if not coords:
                continue
            g, a, v = coords
            if (g, a) in managed:
                continue
            managed.add((g, a))
            new_entries.append((g, a, v))
        if not new_entries:
            continue
        insert = "".join(
            f"      <dependency>\n        <groupId>{g}</groupId>\n"
            f"        <artifactId>{a}</artifactId>\n        <version>{v}</version>\n"
            f"      </dependency>\n"
            for g, a, v in new_entries
        )
        new_text = text[:dm.start(3)] + insert + text[dm.start(3):]
        try:
            pom.write_text(new_text, encoding="utf-8")
        except OSError:
            continue
        rel = _rel(root, pom)
        modified.append(rel)
        added[rel] = [f"{g}:{a}:{v}" for g, a, v in new_entries]
    return modified, added


def missing_intra_project_module_versions(project_path: str) -> list[str]:
    """交付前版本完整性闸门：返回【内部模块依赖但版本无处可得】的清单（非空 → fail-closed）。

    内部模块依赖 = 某模块 pom 的运行时 <dependency> 的 (groupId, artifactId) 命中本工程另一模块坐标。
    "版本无处可得" = 该 dependency 未写 <version> 且未被任一聚合器 dependencyManagement 覆盖
    → reactor 解析必失败。仅管辖【内部模块】，外部依赖(版本策略交 BOM/用户)不碰。返回 "模块pom → g:a" 列表。
    """
    root = Path(project_path)
    if not root.is_dir():
        return []
    poms = _all_poms(root)
    internal: set[tuple[str, str]] = set()
    managed: set[tuple[str, str]] = set()
    for p in poms:
        t = _read(p)
        if t is None:
            continue
        c = _maven_pom_coords(t)
        if c:
            internal.add((c[0], c[1]))
        managed |= _managed_pairs(t)
    missing: list[str] = []
    for p in poms:
        t = _read(p)
        if t is None:
            continue
        for g, a, has_v in _maven_direct_deps(t):
            if (g, a) in internal and not has_v and (g, a) not in managed:
                missing.append(f"{_rel(root, p)} → {g}:{a}")
    return missing


# ───────────────────────────── Gradle ─────────────────────────────
# 动态枚举(脚本里自己遍历目录注册)启发式——命中则【跳过】，不擅自加 include 致重复。
_GRADLE_DYNAMIC = re.compile(
    r"\beachDir\b|\blistFiles\b|\brootDir\b|\bfileTree\b|file\s*\(|\.list\s*\(|"
    r"FileTree|subprojects\s*\{|allprojects\s*\{", re.I,
)


def _reconcile_gradle(root: Path, hint: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """settings.gradle(.kts)：根直接子项目(有 build.gradle(.kts))须 include。仅处理顶层。"""
    settings = None
    for cand in ("settings.gradle", "settings.gradle.kts"):
        p = root / cand
        if p.is_file():
            settings = p
            break
    if settings is None:
        return [], {}
    text = _read(settings)
    if text is None:
        return [], {}
    # 动态枚举的 settings 不碰(避免 include 重复/语义改变)
    if _GRADLE_DYNAMIC.search(text):
        return [], {}
    included = set()
    for m in re.finditer(r"include\s*\(?\s*['\"]:?([\w:.-]+)['\"]", text):
        # include ':a:b' → 顶层段 'a'
        included.add(m.group(1).split(":", 1)[0])
    is_kts = settings.suffix == ".kts"
    new_members: list[str] = []
    add_lines: list[str] = []
    for child in _safe_subdirs(root):
        if child.name in included:
            continue
        if not ((child / "build.gradle").is_file() or (child / "build.gradle.kts").is_file()):
            continue
        new_members.append(child.name)
        add_lines.append(
            f'include(":{child.name}")' if is_kts else f"include ':{child.name}'"
        )
    if not new_members:
        return [], {}
    new_text = text.rstrip("\n") + "\n" + "\n".join(add_lines) + "\n"
    try:
        settings.write_text(new_text, encoding="utf-8")
    except OSError:
        return [], {}
    rel = _rel(root, settings)
    return [rel], {rel: new_members}


# ───────────────────────────── Cargo (Rust) ─────────────────────────────
def _reconcile_cargo(root: Path, hint: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """根 Cargo.toml [workspace] members：磁盘上的 crate(有 [package] 的 Cargo.toml)须列入。

    既有 glob 成员(如 "crates/*")覆盖到的目录【跳过】；仅补未被任何条目覆盖的显式路径。
    """
    cargo = root / "Cargo.toml"
    if not cargo.is_file():
        return [], {}
    text = _read(cargo)
    if text is None or "[workspace]" not in text:
        return [], {}
    marr = re.search(r"members\s*=\s*\[(.*?)\]", text, re.S)
    if not marr:
        return [], {}
    # 保守治本：members 数组内含【行内注释】时跳过——重排数组会丢注释且破坏幂等。常见的无注释
    # 数组照常对账；带注释的留给人工(罕见)。绝不为了补成员而吞掉用户注释。
    if "#" in marr.group(1):
        logger.debug("[workspace-manifest] Cargo members 含注释，跳过(避免丢注释/破坏幂等)")
        return [], {}
    entries = re.findall(r"['\"]([^'\"]+)['\"]", marr.group(1))
    globs = [e for e in entries if "*" in e]
    explicit = {e.rstrip("/") for e in entries if "*" not in e}

    def _glob_covered(relpath: str) -> bool:
        for g in globs:
            # 简化：把 glob 段按 '*' 拆成前后缀做匹配（覆盖 "crates/*"、"*/sub" 常见形态）
            gx = g.rstrip("/")
            if "/*" in gx:
                prefix = gx.split("*", 1)[0].rstrip("/")
                # crates/* 覆盖 crates/<single-seg>
                if relpath.startswith(prefix + "/") and "/" not in relpath[len(prefix) + 1:]:
                    return True
        return False

    new_members: list[str] = []
    # 仅扫顶层 + 一层子目录(crates/ 惯例)，找含 [package] 的 Cargo.toml
    search_roots = [root] + _safe_subdirs(root)
    seen_dirs: set[str] = set()
    for base in search_roots:
        for child in _safe_subdirs(base):
            ctoml = child / "Cargo.toml"
            if not ctoml.is_file():
                continue
            ctext = _read(ctoml) or ""
            if "[package]" not in ctext:
                continue
            relpath = _rel(root, child)
            if relpath in seen_dirs:
                continue
            seen_dirs.add(relpath)
            if relpath in explicit or _glob_covered(relpath):
                continue
            new_members.append(relpath)
    if not new_members:
        return [], {}
    inner = marr.group(1)
    add_str = "".join(f'    "{m}",\n' for m in new_members)
    if inner.strip():
        # 既有项规整成尾部带逗号，再追加新项(保守、不破坏既有缩进/注释结构)
        new_inner = inner.rstrip().rstrip(",") + ",\n" + add_str
    else:
        new_inner = "\n" + add_str
    new_arr = f"members = [{new_inner}]"
    new_text = text[:marr.start()] + new_arr + text[marr.end():]
    try:
        cargo.write_text(new_text, encoding="utf-8")
    except OSError:
        return [], {}
    rel = _rel(root, cargo)
    return [rel], {rel: new_members}


# ───────────────────────────── .NET (.sln) ─────────────────────────────
_SLN_TYPE_GUID = {
    ".csproj": "FAE04EC0-301F-11D3-BF4B-00C04F79EFBC",
    ".fsproj": "F2A71F9B-5D33-465A-A702-920D77279786",
    ".vbproj": "F184B08F-C81C-45F6-A57F-5ABD9991F28F",
}
_SLN_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # 确定性 GUID 命名空间


def _reconcile_dotnet_sln(root: Path, hint: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """*.sln：磁盘上的 *.csproj/*.fsproj/*.vbproj 须有 Project(...) 条目 + 构建配置。

    GUID 由项目相对路径确定性派生(uuid5)——可复现、幂等。格式异常/无 Global 段一律跳过。
    """
    slns = [p for p in root.glob("*.sln") if p.is_file()]
    if not slns:
        return [], {}
    sln = slns[0]
    text = _read(sln)
    if text is None or "\nGlobal" not in ("\n" + text):
        return [], {}
    # 已引用的工程路径(归一: 反斜杠→正斜杠、小写)
    referenced = set()
    for m in re.finditer(r'Project\("\{[^}]+\}"\)\s*=\s*"[^"]*",\s*"([^"]+)"', text):
        referenced.add(m.group(1).replace("\\", "/").lower())

    proj_files: list[Path] = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            for c in d.iterdir():
                if c.is_dir() and c.name not in _SKIP_DIRS and not c.name.startswith("."):
                    stack.append(c)
                elif c.is_file() and c.suffix in _SLN_TYPE_GUID:
                    proj_files.append(c)
        except OSError:
            pass

    new_members: list[str] = []
    proj_blocks: list[str] = []
    cfg_lines: list[str] = []
    for proj in proj_files:
        relp = _rel(root, proj)
        if relp.lower() in referenced:
            continue
        name = proj.stem
        type_guid = _SLN_TYPE_GUID[proj.suffix]
        proj_guid = str(uuid.uuid5(_SLN_NS, relp.lower())).upper()
        win_path = relp.replace("/", "\\")
        proj_blocks.append(
            f'Project("{{{type_guid}}}") = "{name}", "{win_path}", "{{{proj_guid}}}"\n'
            f"EndProject\n"
        )
        for cfg in ("Debug", "Release"):
            cfg_lines.append(
                f"\t\t{{{proj_guid}}}.{cfg}|Any CPU.ActiveCfg = {cfg}|Any CPU\n"
                f"\t\t{{{proj_guid}}}.{cfg}|Any CPU.Build.0 = {cfg}|Any CPU\n"
            )
        new_members.append(name)
    if not new_members:
        return [], {}
    # 保守治本：缺 ProjectConfigurationPlatforms 段时【整体跳过】，绝不只插 Project 块而漏配置行
    # ——后者会产出"有工程无构建配置"的【损坏 .sln】(VS/msbuild 构建确定性失败)，比缺工程更糟。
    cfg_section = re.search(
        r"(GlobalSection\(ProjectConfigurationPlatforms\)[^\n]*\n)", text
    )
    if not cfg_section:
        logger.debug("[workspace-manifest] .sln 缺 ProjectConfigurationPlatforms 段，跳过(避免产出损坏 sln)")
        return [], {}
    # Project 块插到首个 "Global" 前；配置行插到 ProjectConfigurationPlatforms 段内
    new_text = text.replace("\nGlobal", "\n" + "".join(proj_blocks) + "Global", 1)
    cfg_section = re.search(
        r"(GlobalSection\(ProjectConfigurationPlatforms\)[^\n]*\n)", new_text
    )
    idx = cfg_section.end()
    new_text = new_text[:idx] + "".join(cfg_lines) + new_text[idx:]
    try:
        sln.write_text(new_text, encoding="utf-8")
    except OSError:
        return [], {}
    rel = _rel(root, sln)
    return [rel], {rel: new_members}


# ───────────────────────────── Go (go.work) ─────────────────────────────
def _reconcile_go_work(root: Path, hint: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """go.work：磁盘上含 go.mod 的目录须有 `use ./dir`。仅对【既有】go.work 对账；
    绝不创建 go.work(单模块库无须工作区，擅自建会改变构建语义)。"""
    gowork = root / "go.work"
    if not gowork.is_file():
        return [], {}
    text = _read(gowork)
    if text is None:
        return [], {}
    used = set()
    for m in re.finditer(r"use\s+(?:\(\s*)?\.?/?([^\s()]+)", text):
        used.add(m.group(1).strip("/"))
    new_members: list[str] = []
    add_lines: list[str] = []
    for child in _safe_subdirs(root):
        rels = _rel(root, child)
        if rels in used or child.name in used:
            continue
        if not (child / "go.mod").is_file():
            continue
        new_members.append(rels)
        add_lines.append(f"use ./{rels}")
    if not new_members:
        return [], {}
    new_text = text.rstrip("\n") + "\n" + "\n".join(add_lines) + "\n"
    try:
        gowork.write_text(new_text, encoding="utf-8")
    except OSError:
        return [], {}
    rel = _rel(root, gowork)
    return [rel], {rel: new_members}
