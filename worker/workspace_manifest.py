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
    project_path: str, modified: list[str] | None = None, prune: bool = True
) -> dict:
    """对账项目内所有【显式成员列表】型聚合清单，使其枚举磁盘上真实存在的成员模块。

    确定性、幂等、模型无关。返回:
        {"modified_manifests": [清单相对路径...],
         "added": {清单相对路径: [新增成员标识...]},
         "removed": {清单相对路径: [被摘幽灵成员...]}}
    `modified` 仅作候选提示，真正驱动是磁盘 ground-truth 扫描，故传 None 也正确。
    任一生态的对账抛错都被隔离吞掉(增益层不可拖垮主流程)，其它生态照常对账。
    `prune=False` 只补漏不摘幽灵——L1 调用点必须传 False（对抗复核 F4：活动共享树上
    contract_utils 规则 4 让 root pom owner 先行登记全部新模块，目录物化在后；此时
    prune 会把先行登记误当幽灵摘掉，且与 pull-back flock 不是同一把锁存在 lost-update）。
    L2(integration_review，reset+apply 定格树)/交付(learn_success，锁内)两处用默认 True。
    """
    root = Path(project_path)
    if not root.is_dir():
        return {"modified_manifests": [], "added": {}, "removed": {}}
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
    # R46-2：add 侧补漏后跑 prune 侧摘幽灵（目录已不存在的成员条目会毒死 reactor/构建）。
    # 双向镜像同一 ground truth，幂等：add 只加真实存在的，prune 只摘真实不存在的，互不打架。
    removed = prune_stale_manifest_members(project_path) if prune else {}
    for k in removed:
        if k not in modified_manifests:
            modified_manifests.append(k)
    return {"modified_manifests": modified_manifests, "added": added, "removed": removed}


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


# ══════════════════ R46 治本：成员条目 ↔ 磁盘存在 双向镜像（prune 侧）══════════════════
# round46 实锤两面同根：
#   R46-1  reconcile 按【本地共享树】注册的聚合清单被原样推进【沙箱】——沙箱里没有并行兄弟
#          模块目录 → Maven reactor "Child module does not exist" 硬错 → 构建根本跑不起来
#          → det=None → verification_not_run 判死好产出（脚手架 pom 本身完全正确）。
#   R46-2  阶梯三 revert 清了模块目录足迹，但 root pom 的 <module> 条目没反注册 → 幽灵条目
#          把 L2 集成编译毒死（revert 本意恰是"防 reactor 中毒"）。
# 治本不变量：显式成员清单的条目必须与【目标树】上真实存在的成员双向镜像——add 侧(上方
# reconcile)补漏，prune 侧(本节)摘幽灵。probe 语义统一为「相对清单所在目录的存在性探针」，
# 存在性判定由调用方注入(本地 FS / 沙箱批量 test -e)，核心解析只此一份、多栈复用。
# 保守边界与 add 侧一致：.sln 不碰(格式复杂)；glob 型成员不碰；member_exists 返回 None
# (未知，如沙箱探测失败)一律保留条目 fail-open——绝不因探测通道故障误删成员。

def _pom_modules_span(text: str) -> tuple[int, int] | None:
    """首个【不在 <profiles> 内】的 <modules> 块 span（含标签）。

    对抗复核 F2-3：profiles 块可先于主 <modules> 出现，全文首匹配会把探测/剪除
    整体打在 profile 块上（主块幽灵永不被剪 + profile 被误清空）。probes 与 prune
    都必须锚定同一个主块 span。
    """
    pspans = [m.span() for m in re.finditer(r"<profiles>.*?</profiles>", text, re.S)]
    for m in re.finditer(r"<modules>.*?</modules>", text, re.S):
        if not any(a <= m.start() < b for a, b in pspans):
            return m.span()
    return None


def manifest_member_probes(rel_path: str, text: str) -> list[tuple[str, str]]:
    """解析清单的显式成员 → [(成员原始 token, 存在性探针相对路径)]。

    探针相对【清单所在目录】：Maven=<module 值>/pom.xml（reactor 两者缺一即硬错）；
    Gradle include ':a:b'=a/b 目录；Cargo 显式 member=目录；go.work use=目录。
    未识别的清单类型返回 []（调用方自然跳过剪枝）。
    保守边界（对抗复核 F2）：pom 只看主 <modules> 块（profiles 不碰）；Gradle 只收
    【单 token 独占一行】的 include（多工程单行 include 整行跳过，避免截断腐蚀）；
    Cargo 只在 members 数组 span 内取显式项。
    """
    name = rel_path.rsplit("/", 1)[-1]
    out: list[tuple[str, str]] = []
    if name == "pom.xml":
        span = _pom_modules_span(text)
        if span:
            for m in re.findall(r"<module>\s*([^<\s]+)\s*</module>", text[span[0]:span[1]]):
                out.append((m, f"{m.rstrip('/')}/pom.xml"))
    elif name in ("settings.gradle", "settings.gradle.kts"):
        if not _GRADLE_DYNAMIC.search(text):
            # 仅单 token 独占一行的 include；`include ':a', ':b'` 多 token 行不收
            for m in re.finditer(
                    r"(?m)^[ \t]*include[ \t]*\(?[ \t]*['\"]:?([\w:.-]+)['\"][ \t]*\)?[ \t]*$",
                    text):
                tok = m.group(1)
                out.append((tok, tok.replace(":", "/")))
    elif name == "Cargo.toml":
        marr = re.search(r"members\s*=\s*\[(.*?)\]", text, re.S)
        if marr and "#" not in marr.group(1):
            for e in re.findall(r"['\"]([^'\"]+)['\"]", marr.group(1)):
                if "*" not in e:  # glob 成员自愈，不碰
                    out.append((e, e.rstrip("/")))
    elif name == "go.work":
        for m in re.finditer(r"use\s+(?:\(\s*)?\.?/?([^\s()]+)", text):
            tok = m.group(1)
            out.append((tok, tok.strip("/")))
    return out


def _sub_in_span(text: str, span: tuple[int, int], pat: re.Pattern) -> tuple[str, int]:
    """只在 span 切片内做 count=1 删除，重组全文 → (新文本, 命中数)。"""
    seg, n = pat.subn("", text[span[0]:span[1]], count=1)
    if not n:
        return text, 0
    return text[:span[0]] + seg + text[span[1]:], n


def prune_manifest_members(rel_path: str, text: str, member_exists) -> tuple[str, list[str]]:
    """按存在性摘除清单中的幽灵成员条目 → (新文本, 被摘成员 token 列表)。

    member_exists(probe_rel) -> bool | None：True=存在保留；False=幽灵摘除；
    None=未知保留（fail-open）。仅逐条目做行级/标签级删除，绝不重排既有结构；
    删除严格限定在 probes 同一 span 内（对抗复核 F2：全文匹配曾实测腐蚀
    Gradle 多工程行 / Cargo path 依赖 / pom profiles 块）。
    """
    removed: list[str] = []
    new_text = text
    name = rel_path.rsplit("/", 1)[-1]
    for tok, probe in manifest_member_probes(rel_path, text):
        exists = member_exists(probe)
        if exists is not False:
            continue
        n = 0
        if name == "pom.xml":
            span = _pom_modules_span(new_text)
            if span:
                pat = re.compile(
                    r"[ \t]*<module>\s*" + re.escape(tok) + r"\s*</module>[ \t]*\r?\n?")
                new_text, n = _sub_in_span(new_text, span, pat)
        elif name in ("settings.gradle", "settings.gradle.kts"):
            # 整行锚定：只删「单 token 独占一行」形态，多 token 行/注释行天然不匹配
            pat = re.compile(
                r"(?m)^[ \t]*include[ \t]*\(?[ \t]*['\"]:?" + re.escape(tok)
                + r"['\"][ \t]*\)?[ \t]*$\n?")
            new_text, n = pat.subn("", new_text, count=1)
        elif name == "Cargo.toml":
            marr = re.search(r"members\s*=\s*\[(.*?)\]", new_text, re.S)
            if marr:
                pat = re.compile(r"[ \t]*['\"]" + re.escape(tok) + r"['\"]\s*,?[ \t]*\r?\n?")
                new_text, n = _sub_in_span(new_text, marr.span(1), pat)
        elif name == "go.work":
            pat = re.compile(r"(?m)^[ \t]*use[ \t]+\.?/?" + re.escape(tok) + r"[ \t]*$\n?")
            new_text, n = pat.subn("", new_text, count=1)
        if n and tok not in removed:
            removed.append(tok)
    return new_text, removed


def prune_stale_manifest_members(project_path: str) -> dict[str, list[str]]:
    """本地树 prune 入口：对磁盘上的聚合清单摘除目录已不存在的幽灵成员。

    与 reconcile_workspace_manifests(add 侧)配对，在 ①L1 ②L2 ③交付 同三处生效——
    R46-2 revert 幽灵、以及任何"目录没了条目还在"的残留都在下一次对账被确定性自愈。
    返回 {清单相对路径: [被摘成员...]}；任何异常整体吞掉（增益层不可拖垮主流程）。
    """
    root = Path(project_path)
    if not root.is_dir():
        return {}
    removed_all: dict[str, list[str]] = {}
    try:
        cands: list[Path] = [d / "pom.xml" for d in _maven_aggregators(root)]
        for n in ("settings.gradle", "settings.gradle.kts", "Cargo.toml", "go.work"):
            p = root / n
            if p.is_file():
                cands.append(p)
        for mf in cands:
            text = _read(mf)
            if text is None:
                continue
            rel = _rel(root, mf)

            def _exists(probe: str, _base: Path = mf.parent) -> bool:
                return (_base / probe).exists()

            new_text, removed = prune_manifest_members(rel, text, _exists)
            if not removed:
                continue
            try:
                mf.write_text(new_text, encoding="utf-8")
            except OSError:
                continue
            removed_all[rel] = removed
            logger.info(
                "[workspace-manifest] prune 摘除幽灵成员（目录已不存在，条目残留会毒死"
                "构建/reactor）: %s ← %s", rel, removed)
    except Exception as exc:  # noqa: BLE001 — 增益层：prune 失败不影响主流程
        logger.debug("[workspace-manifest] prune 跳过(异常,不致命): %s", exc)
    return removed_all


# ══════════════ R48c-1：共享清单 pull-back 并集合并（防陈旧副本覆盖丢修复）══════════════
# round48c 实锤：st-20 的防线④把 spring-data-redis 注入 ruoyi-system/pom.xml 并按 C9 回传
# 本地（10:29）；随后并行子任务的 pull-back 携【bootstrap 时的基线旧副本】盲覆盖（11:59
# mtime、内容=基线）→ 修复静默蒸发 → 全部下游子任务在同一缺包上 BLOCKED 空转。flock 只
# 串行化写、不防陈旧内容 last-write-wins——治本=共享清单写盘并集合并：以 incoming（本 worker
# 的有意编辑）为基，把 local 已有而 incoming 缺失的 <dependency>(按 g:a)/<module> 条目并回。
# 加法-only：绝不删 incoming 内容；解析异常 fail-open 原样返回 incoming（回退旧行为）。

def _pom_region_spans(text: str) -> dict[str, list[tuple[int, int]]]:
    """pom 分区 span：dm / profiles / build（复核 C：profile·插件依赖是条件/工具面，
    并集绝不跨区搬运——搬进主区=条件依赖变无条件、插件依赖污染编译 classpath）。"""
    return {
        "dm": [m.span() for m in re.finditer(
            r"<dependencyManagement>.*?</dependencyManagement>", text, re.S)],
        "profiles": [m.span() for m in re.finditer(
            r"<profiles>.*?</profiles>", text, re.S)],
        "build": [m.span() for m in re.finditer(
            r"<build>.*?</build>", text, re.S)],
    }


def _pom_dep_blocks(text: str) -> list[tuple[tuple[str, str], str, str]]:
    """pom 的 <dependency> 块 → [((g,a), 块文本, 区域)]，区域∈{"plain","dm"}。

    profiles/build(插件) 内的块【整体跳过】（不收集也不计键，复核 C）。"""
    out = []
    spans = _pom_region_spans(text)
    for m in re.finditer(r"<dependency>(.*?)</dependency>", text, re.S):
        if any(s <= m.start() < e for sp in (spans["profiles"], spans["build"])
               for s, e in sp):
            continue
        inner = re.sub(r"<exclusions>.*?</exclusions>", "", m.group(1), flags=re.S)
        g = re.search(r"<groupId>\s*([^<\s]+)\s*</groupId>", inner)
        a = re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", inner)
        if not (g and a):
            continue
        region = "dm" if any(
            s <= m.start() < e for s, e in spans["dm"]) else "plain"
        out.append(((g.group(1), a.group(1)), m.group(0), region))
    return out


def merge_shared_manifest(local_text: str, incoming_text: str, rel_path: str,
                          base_dir: "Path | None" = None) -> str:
    """共享清单并集合并：incoming 为基 + local 独有的依赖/成员条目并回 → 合并文本。

    仅 Maven pom 做依赖/成员并集（丢失面已 live 实证）；其它清单类型原样返回
    incoming（保守——gradle/cargo 未实证丢失面，盲并有语义风险）。任何异常
    fail-open 返回 incoming。
    复核 B：依赖键=(g,a,区域) 分账——dm 条目绝不挡 classpath 修复（RuoYi 根 pom
    是巨型 dm，跨区混同=原 live 缺陷的残留半径）。复核 C：profiles/build 插件
    依赖整体不参与（不收集/不插入其区）。复核 4：modules 并回带存在性校验
    （base_dir 提供时，目录已不存在的幽灵成员不复活）。加法-only 已知取舍：
    内容级"有意删除"会被并回复活（两方合并无法与覆盖丢失区分，需三方基线——
    登记债）；文件级删除走 delete_files 专路不受影响。
    """
    try:
        name = rel_path.rsplit("/", 1)[-1].lower()
        if name != "pom.xml" or local_text == incoming_text:
            return incoming_text
        merged = incoming_text
        inc_plain = {ga for ga, _, r in _pom_dep_blocks(incoming_text) if r == "plain"}
        inc_dm = {ga for ga, _, r in _pom_dep_blocks(incoming_text) if r == "dm"}
        add_plain: list[str] = []
        add_dm: list[str] = []
        for ga, blk, region in _pom_dep_blocks(local_text):
            if region == "plain" and ga not in inc_plain:
                add_plain.append(blk)
                inc_plain.add(ga)
            elif region == "dm" and ga not in inc_dm:
                add_dm.append(blk)
                inc_dm.add(ga)
        if add_plain:
            # 并入 incoming 首个【主区】</dependencies> 之前（排除 dm/profiles/build）
            spans = _pom_region_spans(merged)
            _excl = spans["dm"] + spans["profiles"] + spans["build"]
            for m in re.finditer(r"</dependencies>", merged):
                if not any(s <= m.start() < e for s, e in _excl):
                    ins = "".join(f"        {b}\n" for b in add_plain)
                    merged = merged[:m.start()] + ins + merged[m.start():]
                    break
            else:
                add_plain = []  # incoming 无主依赖区 → 保守不并（避免臆造结构/落错区）
        if add_dm:
            m2 = re.search(
                r"<dependencyManagement>.*?(</dependencies>)", merged, re.S)
            if m2:
                ins = "".join(f"            {b}\n" for b in add_dm)
                merged = merged[:m2.start(1)] + ins + merged[m2.start(1):]
            else:
                add_dm = []
        # <modules> 成员并集（主块口径与 prune 同锚点；存在性校验防幽灵复活）
        add_mods: list[str] = []
        loc_span = _pom_modules_span(local_text)
        inc_span = _pom_modules_span(merged)
        if loc_span and inc_span:
            loc_mods = re.findall(
                r"<module>\s*([^<\s]+)\s*</module>", local_text[loc_span[0]:loc_span[1]])
            inc_mods = set(re.findall(
                r"<module>\s*([^<\s]+)\s*</module>", merged[inc_span[0]:inc_span[1]]))
            add_mods = [x for x in loc_mods if x not in inc_mods]
            if add_mods and base_dir is not None:
                add_mods = [x for x in add_mods
                            if (base_dir / x.rstrip("/") / "pom.xml").is_file()]
            if add_mods:
                ins_at = merged.index("</modules>", inc_span[0])
                ins = "".join(f"        <module>{x}</module>\n" for x in add_mods)
                merged = merged[:ins_at] + ins + merged[ins_at:]
        if add_plain or add_dm or add_mods:
            logger.info(
                "[workspace-manifest] R48c-1 共享清单并集合并 %s：并回 local 独有 "
                "dependency %d 个 + dm %d 个 + module %d 个（陈旧副本覆盖丢修复面）",
                rel_path, len(add_plain), len(add_dm), len(add_mods))
        return merged
    except Exception as exc:  # noqa: BLE001 — fail-open 回退旧行为（盲覆盖）
        logger.warning("[workspace-manifest] R48c-1 合并异常 fail-open: %s", exc)
        return incoming_text
