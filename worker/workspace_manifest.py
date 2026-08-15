"""通用 workspace/聚合清单对账（确定性、幂等、模型无关）。

多模块工程的【聚合清单】都须枚举所有成员模块：
  - Maven   : 根 pom.xml `<modules><module>`
  - Gradle  : settings.gradle(.kts) `include`
  - Rust    : 根 Cargo.toml `[workspace] members`
  - .NET    : *.sln `Project(...)` 条目
  - Go      : go.work `use ./x`（多模块工作区）
  - npm     : 根 package.json `workspaces`【显式列表】形态（X-H3，27 号文 B-5）

并行子任务各自在【独立沙箱】里改这个共享清单 → pull-back 整文件覆盖 → 后注册的把先注册的
【冲掉】(last-write-wins) → 成员丢失 → reactor/构建找不到该模块 → 确定性失败（与代码无关）。
逐子任务打地鼠赢不了这个并发竞态。

治本 = 不打地鼠，而是【对账 ground truth】：磁盘上真实存在哪些成员模块目录(各有自己的成员清单
文件)，就让聚合清单枚举哪些。三处复用同一核心：
  ① 子任务 L1 构建闸门(沙箱内，使其能据成员 -pl/收窄构建)；
  ② L2 集成验证(合并库 apply 后、构建前，使集成构建不因被冲掉的清单【假失败】)；
  ③ 交付 commit 前(合并库上，把对账结果写进交付产物，持久化、杜绝 race 残留)。

仅处理【显式成员列表】型清单/条目——glob 条目(Node `"workspaces": ["packages/*"]`、pnpm、
Python `pyproject` workspace globs)会自愈，不碰；但 npm workspaces 的**显式列表**形态
（`["packages/web", "packages/api"]`）【不自愈】——新子包永不进列表 → `npm ci` 装不到
（X-H3 实证），故显式条目同样三面（add/prune/probes）收编。保守、绝不臆造结构：聚合清单
不存在/格式异常/疑似【动态枚举】一律跳过，绝不创建新清单、绝不改写既有非成员区。
全程无 LLM、幂等、可复现。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# 遍历时跳过的重目录（构建产物/依赖/VCS），避免误把它们当成员或拖慢扫描。
_SKIP_DIRS = {
    "target", "build", "out", "bin", "obj", "dist", "node_modules",
    ".git", ".idea", ".vscode", ".gradle", ".mvn", "vendor", "__pycache__",
}


def reconcile_workspace_manifests(
    project_path: str, modified: list[str] | None = None, prune: bool = True,
    use_lock: bool = False,
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
    prune 会把先行登记误当幽灵摘掉）。
    L2(integration_review，reset+apply 定格树)/交付(learn_success，锁内)两处用默认 True。

    C3（worker 审计 HIGH）：`use_lock=True` 时【读-改-写整段】收进 _ProjectGitFlock——
    L1 调用点在活动共享树上执行且不在任何 flock 内，与并行兄弟 pull-back 的共享清单合并
    （sandbox.py 锁内）互踩会 lost-update（F4 只关了 prune 半边，add 侧照旧裸写）。
    已持锁调用点（L2 的 F2 工作树段/交付临界区）必须保持 use_lock=False——fcntl flock
    同进程【异 fd】不可重入，重复取锁即自死锁。
    """
    root = Path(project_path)
    if not root.is_dir():
        return {"modified_manifests": [], "added": {}, "removed": {}}
    if use_lock:
        from swarm.worker.git_flock import _ProjectGitFlock
        with _ProjectGitFlock(root):
            return _reconcile_manifests_unlocked(root, modified, prune)
    return _reconcile_manifests_unlocked(root, modified, prune)


def _reconcile_manifests_unlocked(
    root: Path, modified: list[str] | None, prune: bool
) -> dict:
    """reconcile 本体（调用方负责锁语义，见 reconcile_workspace_manifests docstring）。

    add 面分派表=模块级 `_RECONCILE_DISPATCH`（★X-H3 R2★ 单一事实源给测试断
    「接线事实」用——禁 getsource 扫函数体，纪律 6）。"""
    hint = [str(m or "") for m in (modified or [])]
    modified_manifests: list[str] = []
    added: dict[str, list[str]] = {}
    for fn in _RECONCILE_DISPATCH:
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
    removed = prune_stale_manifest_members(str(root)) if prune else {}
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
        # C8（19号文）：add 侧复用 _pom_modules_span（F2-3 只治了 prune 侧）——全文首匹配
        # 在 <profiles> 先于主 <modules> 时会把新模块注册进 profile 块，默认构建缺模块。
        span = _pom_modules_span(text)
        if span is None:
            continue
        mblock_inner = text[span[0]:span[1]]
        registered = set(re.findall(r"<module>\s*([^<\s]+)\s*</module>", mblock_inner))
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
        # 插入点同样锚定 span 内的 </modules>（不可全文 replace 首命中——理由同 C8 上注）。
        close_idx = text.rindex("</modules>", span[0], span[1])
        new_text = text[:close_idx] + insert + "    " + text[close_idx:]
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
    R65REPLAY-T3(回放实锤 com.ruoyi:ruoyi-admin:4.8.3 入根 depMgmt 存活至终态树)：
    只补【被本工程其它模块运行时依赖引用】的 (g,a)——旧行为无差别登记全部带 <parent>
    的子模块，把无人依赖的应用壳(assembly/launcher)也声明成可依赖件=错误对外契约。
    治本初衷(round18 内部依赖版本可解析)只需要被引用者，过宽即病。
    确定性、幂等(已管理的 g:a 跳过)、模型无关。
    """
    modified: list[str] = []
    added: dict[str, list[str]] = {}
    for agg in _maven_aggregators(root):
        pom = agg / "pom.xml"
        text = _read(pom)
        if text is None:
            continue
        # 本聚合树内全部模块的运行时依赖引用集(g,a)——depMgmt 只为它们服务
        referenced: set[tuple[str, str]] = set()
        for _rp in _all_poms(agg):
            _rt = _read(_rp) or ""
            for _g, _a, _hv in _maven_direct_deps(_rt):
                if _g and _a:
                    referenced.add((_g, _a))
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
            if (g, a) not in referenced:
                continue  # 无人依赖(应用壳/launcher) → 不登记为可依赖件
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
# ★批23 C-6b#5★：`file(` 从裸子串收紧为【include 邻近上下文】（_GRADLE_DYNAMIC_INCLUDE）——
# settings.gradle 里 `file('gradle.properties')` 读配置是合法静态写法，裸子串把它冤判动态
# ⇒ reconcile 不补 include 且 merge 保守直通=成员蒸发（误杀方向，fail 向不新鲜）。
# ★批26 收口（批23 R1 reviewer L-3 / hunter F6 登记债）★：`rootDir` 从裸标记收紧为
# 【迭代基座邻近上下文】两形：`rootDir.<each|list|walk|traverse>…`（直迭代）与
# `File(rootDir,…).<each|…>`（构造器包裹一行形态，如 `new File(rootDir,'libs')
# .eachFile{}`；纯构造 `projectDir = new File(rootDir,'legacy/a')` 无迭代方法不命中）。
# 静态写法 `file("$rootDir/legacy/a")`/`rootDir.absolutePath` 不再冤判动态。
# ★刻意让渡的召回尾巴（批26 R1 reviewer MEDIUM-1 / hunter MEDIUM-2 证伪「召回零损」
# 后如实登记）★：【别名两行形态】——`def d = rootDir`（或 `def d = new File(rootDir,
# 'modules')`）后另起行 `d.eachFile{}`/`d.traverse{}`——regex 级不可判（需别名流
# 分析），旧裸标记只靠「赋值行碰巧含 rootDir」偶然覆盖。另：构造器参数含嵌套括号的
# 形态（`new File(rootDir, sub('x')).eachFile{}`）因 `[^)]*` 停在首个 `)` 同让渡
# （现实频率极低，登记不收）。让渡后果方向如实写明：
# 真动态文件漏判 ⇒ 按静态处理 ⇒ reconcile 补 include/merge 并成员（Gradle 对重复
# include 容错，烈度低于构建硬错），且误判侧有 reconcile WARNING 机读可辨。
# 与 fileTree 保留理由同族不同解：fileTree 静态用法罕见 ⇒ 召回优先保裸标记；
# rootDir 静态用法高频 ⇒ 收紧优先+登记让渡。同族形态两边取舍相反是【各自静态/
# 动态频率】的刻意决定，非自相矛盾。
# 形态矩阵权威载体=test_batch26_lead_locks.py（静态 2 形态+动态 7 形态+让渡钉
# 1 函数 2 形态，突变双向验红）。
# ★过宽面登记（批26 R2 hunter LOW-e，非回归不阻塞）★：构造器邻近式的 each 前缀
# 同罩 `eachLine`（`new File(rootDir,'gradle.properties').eachLine{}` 逐行读配置=
# 静态用法冤判动态）——旧裸 rootDir 标记对该行同判动态 ⇒ 非回归；误杀侧有
# reconcile WARNING 机读可辨，登记不收（细分 eachLine/eachFile 的前缀表收益
# 不抵误伤面重估成本）。
# ★刻意保留的已知尾巴★：`fileTree` 维持裸标记不收——收紧到迭代邻近会丢「先赋值
# 后遍历」（`def t = fileTree(...); t.each{}`）形态的召回，且 fileTree 在
# settings.gradle 的静态用法（相对 build.gradle 高频区）罕见 ⇒ 冤判成本低于召回
# 损失；误杀侧有 reconcile WARNING 机读可辨。有行为锁钉住，顺手收紧会红。
_GRADLE_DYNAMIC = re.compile(
    r"\beachDir\b|\blistFiles\b|\brootDir\s*\.\s*(?:each|list|walk|traverse)|"
    r"\bFile\s*\(\s*rootDir[^)]*\)\s*\.\s*(?:each|list|walk|traverse)|"
    r"\bfileTree\b|\.list\s*\(|FileTree|subprojects\s*\{|allprojects\s*\{", re.I,
)
# include 邻近上下文里的 file(/new File( 才算动态枚举信号（`include file(...)`/
# `include(new File(...))` 形态）；其余位置的 file( 是静态文件读取，不是成员枚举。
# R1 hunter F5：include 与 file( 之间隔块注释（`include /* c */ file('x')`）也是动态，
# 收紧不能把这个旧裸 file( 抓得到的形态放进来。
_GRADLE_DYNAMIC_INCLUDE = re.compile(
    r"\binclude\s*\(?\s*(?:/\*[^\n]*?\*/\s*)?(?:new\s+)?file\s*\(", re.I,
)


def _gradle_dynamic_hit(text: str) -> "re.Match | None":
    """settings.gradle 动态枚举判定【单一事实源】（批23 C-6b#5）：裸标记 ∪ include 邻近
    file(。reconcile/probes/merge 三面必须都走它——三面各持判据=同一文件三种结论
    （共享正则各自改=漂移，C-6 同源纪律）。
    ★R1 hunter F3★：`//` 行注释剥除下沉进本体（批8 R1 只在 merge 面剥）——注释里写
    `// include file('x')` 不得冤判动态，三面预处理同源才配称单一事实源。
    ★诚实边界（R2 双透镜同条）★：剥除正则不认字符串字面量——同行形状
    `def u = "https://x"; include file('gen')` 会从字符串内的 `//` 截断，动态信号被抹
    （三面一致漏判，发生率=同行多语句+字符串含 //+动态 include 三巧合，极低；字符串
    感知剥离复杂度不值）。多行块注释隔断（include /*\n*/ file(）亦不覆盖。"""
    t = re.sub(r"(?m)//[^\n]*", "", text)
    return _GRADLE_DYNAMIC.search(t) or _GRADLE_DYNAMIC_INCLUDE.search(t)


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
    # 动态枚举的 settings 不碰(避免 include 重复/语义改变)——判定走 _gradle_dynamic_hit 单一事实源
    if _gradle_dynamic_hit(text):
        # 批23 R1（reviewer L-3）：动态跳过必须机读可辨——此前完全静默，任何残留误杀
        # 都被这个出口放大成零信号。批26 后静态冤判残余面=fileTree 裸标记（刻意保留，
        # 权衡见 _GRADLE_DYNAMIC 注释；rootDir 债已收口）。
        logger.warning(
            "[workspace-manifest] %s 命中动态枚举启发式，reconcile 跳过补 include"
            "（静态写法冤判的残余面与刻意保留档见 _GRADLE_DYNAMIC 注释）",
            _rel(root, settings))
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
    # C15（19号文）：glob 顺序不定 → 多 .sln 时排序取首（确定性，可复现）。
    slns = sorted(p for p in root.glob("*.sln") if p.is_file())
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
                elif c.is_file() and c.suffix.lower() in _SLN_TYPE_GUID:
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
        type_guid = _SLN_TYPE_GUID[proj.suffix.lower()]
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
    _use_seq: list[str] = []  # 归一后的【有序全量】条目（去重检测用，与 used 同轮收集）

    def _norm_use(entry: str) -> str:
        e = entry.split("//", 1)[0].strip().strip('"')
        if e.startswith("./"):
            e = e[2:]
        return e.strip("/")

    # C4（worker 审计 HIGH）：先提取 `use ( ... )` 块【逐行】收集成员——`go work use` 默认产
    # 块形式，旧正则对每块只捕获首成员 → 第 2+ 成员被误判"未注册" → 文件尾追加重复 use →
    # go 对 workspace 重复目录硬错（fatal），reconcile 把合法 go.work 改坏成确定性构建失败，
    # 且坏文件经 repaired_file_paths 回传毒进权威库。与 _pom_modules_span 同思路（块锚定）。
    for blk in re.finditer(r"use\s*\((.*?)\)", text, re.S):
        for line in blk.group(1).splitlines():
            e = _norm_use(line)
            if e:
                used.add(e)
                _use_seq.append(e)
    block_free = re.sub(r"use\s*\(.*?\)", "", text, flags=re.S)
    # 单行形式 `use ./x`（在剔除块后的文本上匹配，防块头 "use (" 干扰）
    for m in re.finditer(r"(?m)^\s*use\s+([^\s()]+)", block_free):
        e = _norm_use(m.group(1))
        if e:
            used.add(e)
            _use_seq.append(e)
    # ★批23 C-6b#1★：_norm_use 把 `./svc`/`svc` 归一成同键——【比较面】这正确（go 语义
    # 等价）；但同一文件两种写法【并存】时归一让 add/prune/strip/merge 四面都看不见重复，
    # 而 go 对 workspace 重复目录硬错（fatal，与 C4 同死法）。检测归一碰撞：WARNING +
    # 自愈去重（保留首现行、删后续重复行），绝不静默吞掉写法分叉。
    deduped: list[str] = []
    if len(_use_seq) != len(used):
        _dups = sorted({e for e in _use_seq if _use_seq.count(e) > 1})
        logger.warning(
            "[workspace-manifest] C-6b#1 %s 检出写法分叉重复 use 成员 %s（`./svc`/`svc` 归一"
            "同键，go 对重复 use 目录 fatal）", _rel(root, gowork), ",".join(_dups))
        for _dup in _dups:
            # 行级整行匹配（块内裸行 / 单行 use / 引号包裹 / 尾斜杠全形态；批26 起
            # prune_manifest_members 的 go.work 摘除臂已同药补齐引号/尾斜杠/行注释/
            # `\r` 容忍——摘除面与本去重面判据对齐，残留形态不再分叉）。
            # ★R1 hunter F1★：尾部 `\r?$` 行尾锚不可省——缺它时前缀兄弟行
            # （svc2/sub/svc、replace 块的 `./svc => ...`）被吃前缀，且 `_hits[1:]` 会把
            # 真重复行全删=成员蒸发（本批要治的死法换方向复发）。
            _pat = re.compile(
                r"(?m)^[ \t]*(?:use[ \t]+)?[\"']?\.?/?" + re.escape(_dup)
                + r"/?[\"']?[ \t]*(?://[^\n]*)?[ \t]*\r?$\n?")
            _hits = list(_pat.finditer(text))
            if len(_hits) > 1:
                for _m in reversed(_hits[1:]):  # 保留首现，删后续
                    text = text[:_m.start()] + text[_m.end():]
                deduped.append(_dup)
            else:
                # R1 hunter F2：检出≠自愈——行形态未覆盖（如内联 `use (./svc)`）时绝不
                # 谎报「已去重」，机读可辨降级（modified_manifests 也不虚报）。
                logger.warning(
                    "[workspace-manifest] C-6b#1 %s 成员 %r 碰撞检出但行形态未覆盖，"
                    "未自愈（需人工/下轮处理）", _rel(root, gowork), _dup)
        if deduped:
            # 去重可能掏空 `use ( )` 残块（go 解析器对空块报错风险）——与 strip 臂同法清理
            text = re.sub(r"(?m)^[ \t]*use\s*\(\s*\)\r?\n?", "", text)
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
    if not new_members and not deduped:
        return [], {}
    new_text = text.rstrip("\n") + "\n"
    if new_members:
        new_text += "\n".join(add_lines) + "\n"
    try:
        gowork.write_text(new_text, encoding="utf-8")
    except OSError:
        return [], {}
    rel = _rel(root, gowork)
    return [rel], {rel: new_members}


# ─────────────────── npm（workspaces 显式列表形态，X-H3 27 号文 B-5）───────────────────
def _reconcile_npm(root: Path, hint: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """根 package.json workspaces【显式列表】：同容器内磁盘新包须有条目。

    X-H3 实证：显式列表形态（`["packages/web", "packages/api"]`）【不自愈】——
    新子包永不进列表 → `npm ci` 装不到 → import 确定性失败（glob 形态自愈，不碰）。
    容器推断【只】凭既有显式条目的父目录（"packages/web" → 容器 "packages"；顶层
    条目 "web" → 容器=根），绝不臆测约定目录名（血规 2）。已被 glob 条目覆盖的
    目录不加（自愈范围）。无 workspaces 字段=单包工程，绝不擅自创建（与 go.work
    「绝不创建」同哲学）。JSON round-trip 写回（缩进 2，_merge_npm_workspaces 先例）。
    """
    pkg = root / "package.json"
    if not pkg.is_file():
        return [], {}
    text = _read(pkg)
    if text is None:
        return [], {}
    entries = _npm_workspaces_entries(text)
    if entries is None:
        return [], {}
    explicit = _npm_explicit_members(text)
    if not explicit:
        # 纯 glob/空列表：glob 自愈无缺口（不是「认不得」），无容器证据也不猜
        # 约定目录名。留 debug 痕——运维排查「reconcile 怎么没动」时可辨（R2 hunter）。
        logger.debug("[workspace-manifest] npm workspaces 纯 glob/空列表 ⇒ "
                     "无显式成员对账缺口（glob 自愈），跳过 add 面")
        return [], {}
    globs = [e for e in entries if "*" in e]
    containers = {e.rsplit("/", 1)[0] for e in explicit if "/" in e}
    if any("/" not in e for e in explicit):
        # ★R2 reviewer MEDIUM：顶层显式条目（"web"）的容器=根——但根目录的
        # package.json 子目录鱼龙混杂（scripts/e2e/docs 工具包），凭「有一员在根」
        # 推断「根的 package.json 子目录皆是成员」误杀面太大 → fail-closed 不加。
        logger.debug(
            "[workspace-manifest] npm workspaces 含顶层条目 ⇒ 容器=根，根级新包"
            "不做自动登记（误杀面大，fail-closed；多层容器照常对账）")
    listed = set(explicit)
    new_members: list[str] = []
    for c in sorted(containers):
        base = root / c
        for child in _safe_subdirs(base):
            rel = _rel(root, child)
            if rel in listed or rel in containers:
                continue
            if any(_npm_glob_covers(g, rel) for g in globs):
                continue  # glob 已覆盖=自愈范围
            if not (child / "package.json").is_file():
                continue
            new_members.append(rel)
    if not new_members:
        return [], {}
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return [], {}
    ws = obj.get("workspaces")
    target = ws.get("packages") if isinstance(ws, dict) else ws
    if not isinstance(target, list):
        return [], {}
    target.extend(sorted(new_members))
    try:
        pkg.write_text(
            json.dumps(obj, indent=_detect_json_indent(text), ensure_ascii=False) + "\n",
            encoding="utf-8")
    except OSError as _exc:
        logger.debug("[workspace-manifest] npm workspaces 写回失败（不致命）: %s", _exc)
        return [], {}
    return ["package.json"], {"package.json": sorted(new_members)}


# ★add 面分派单一事实源（X-H3 R2）★ `_reconcile_manifests_unlocked` 的唯一驱动；
# 测试断「接线事实」就断这里（纪律 6：单一事实源就是给测试用的），禁 getsource。
_RECONCILE_DISPATCH = (
    _reconcile_maven, _reconcile_maven_dep_versions, _reconcile_gradle,
    _reconcile_cargo, _reconcile_dotnet_sln, _reconcile_go_work, _reconcile_npm,
)


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
# 保守边界与 add 侧一致：glob 型成员不碰；member_exists 返回 None
# (未知，如沙箱探测失败)一律保留条目 fail-open——绝不因探测通道故障误删成员。
# ★X-H3（27 号文 B-5）★ 原「.sln 不碰（格式复杂）」边界已收编：.sln 与 npm 显式
# 列表的 probes/prune 已落地（三面同源——add 早有的栈只补 probes/prune 两面）；
# .sln 仍只收工程文件后缀已知的 Project，解决方案文件夹/URL 工程不碰。

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


# ── npm workspaces 显式条目解析（X-H3：add/prune/probes 三面共享同一解析，绝不各写一份）──
def _npm_workspaces_entries(text: str) -> list[str] | None:
    """package.json 的 workspaces 成员条目（数组形或 {"packages": [...]} 形）。

    返回 None=无 workspaces 字段/JSON 解析失败（调用方自然跳过）；返回列表【含】
    glob/否定条目（调用方按形态分档）。仅根 package.json 的 workspaces 有聚合语义。
    """
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    ws = obj.get("workspaces")
    if isinstance(ws, dict):
        ws = ws.get("packages")
    if not isinstance(ws, list):
        return None
    return [e for e in ws if isinstance(e, str)]


def _npm_norm_entry(entry: str) -> str:
    """workspaces 条目归一化：剥 ./ 前缀与首尾 /，反斜杠归一（与 go.work _norm_use 同思路）。

    ★路径逃逸拒收（X-H3 R2 reviewer HIGH）★：含 `..` 段或以 `/` 开头的条目返回 ""——
    恶意/破损 package.json 的 `"workspaces": ["../sibling"]` 会让 add 面把根目录外
    路径写进 workspaces（已实测复现）。三面（probes/prune/add）同走本函数，一处拒收
    三面免疫。"""
    e = entry.strip().replace("\\", "/")
    if e.startswith("./"):
        e = e[2:]
    # 绝对路径判定必须先于 strip("/")——"/abs/evil" 先剥首 / 会伪装成合法相对路径
    if e.startswith("/") or ".." in e.split("/"):
        return ""
    return e.strip("/")


def _npm_explicit_members(text: str) -> list[str]:
    """workspaces 的【显式成员】条目（归一化后）。glob（含 *）与 yarn 否定（! 前缀）
    条目剔除——前者自愈不碰、后者语义复杂不臆解。"""
    out: list[str] = []
    for e in _npm_workspaces_entries(text) or []:
        if "*" in e or e.strip().startswith("!"):
            continue
        e = _npm_norm_entry(e)
        if e:
            out.append(e)
    return out


def _npm_glob_covers(glob_entry: str, rel: str) -> bool:
    """workspaces glob 条目是否覆盖成员目录 rel（`**`=跨层，`*`=单层）。"""
    pat = re.escape(_npm_norm_entry(glob_entry))
    pat = pat.replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.match("^" + pat + "$", rel) is not None


def _detect_json_indent(text: str) -> "int | str":
    """探测 JSON 文件的既有缩进（X-H3 R2 hunter：round-trip 保真，别把 4 空格/tab
    的原文件全量重排成 2 空格污染交付 diff）。探测失败回退 2（npm 主流约定）。"""
    for line in text.splitlines()[1:20]:
        stripped = line.lstrip(" \t")
        if not stripped:
            continue
        lead = line[:len(line) - len(stripped)]
        if lead:
            return "\t" if "\t" in lead else len(lead)
    return 2


# .sln 的 Project 块头行（捕获：工程名 / 工程文件路径 / 工程 GUID——prune 侧需要 GUID
# 清理 ProjectConfigurationPlatforms 里的配置行，add 侧 reconcile 只用路径）。
_SLN_PROJECT_RE = re.compile(
    r'Project\("\{[^}]+\}"\)\s*=\s*"([^"]*)",\s*"([^"]+)",\s*"\{([^}]+)\}"')


def _sln_path_pattern(norm_path: str) -> str:
    """归一化工程路径 → 匹配两种斜杠写法的正则片段（.sln 惯例用反斜杠）。"""
    return "[\\\\/]".join(re.escape(seg) for seg in norm_path.split("/"))


def manifest_member_probes(rel_path: str, text: str) -> list[tuple[str, str]]:
    """解析清单的显式成员 → [(成员原始 token, 存在性探针相对路径)]。

    探针相对【清单所在目录】：Maven=<module 值>/pom.xml（reactor 两者缺一即硬错）；
    Gradle include ':a:b'=a/b 目录；Cargo 显式 member=目录；go.work use=目录；
    npm workspaces 显式条目=目录（探针=<条目>/package.json）；.sln Project=工程文件本身。
    未识别的清单类型返回 []（调用方自然跳过剪枝）。
    保守边界（对抗复核 F2）：pom 只看主 <modules> 块（profiles 不碰）；Gradle 只收
    【单 token 独占一行】的 include（多工程单行 include 整行跳过，避免截断腐蚀）；
    Cargo 只在 members 数组 span 内取显式项；npm 只收【根】package.json 的显式条目
    （glob/否定条目不碰）；.sln 只收工程文件后缀已知的 Project（解决方案文件夹/
    URL 网站工程不碰）。
    """
    name = rel_path.rsplit("/", 1)[-1]
    out: list[tuple[str, str]] = []
    if name == "package.json" and "/" not in rel_path:
        # X-H3：仅根 package.json 的 workspaces 有聚合语义（成员包自己的同名文件
        # 不是聚合清单）；glob/否定条目已被 _npm_explicit_members 剔除。
        for e in _npm_explicit_members(text):
            out.append((e, f"{e}/package.json"))
        return out
    if name.lower().endswith(".sln"):
        # X-H3：token=归一化工程路径（不用工程名——同名工程不同路径会撞键，基线
        # 对账会冤杀其一）。探针=工程文件本身（.sln 引用的是文件而非目录）。
        for m in _SLN_PROJECT_RE.finditer(text):
            proj_path = m.group(2).replace("\\", "/")
            if "://" in proj_path:
                continue  # 网站工程（URL 路径）非磁盘成员
            # 大小写不敏感（X-H3 R2 reviewer HIGH：Windows 生态常见 Web.CSPROJ；
            # add 侧 _reconcile_dotnet_sln 同口径，两面不得分叉）
            if Path(proj_path).suffix.lower() not in _SLN_TYPE_GUID:
                continue  # 解决方案文件夹（path=名字无文件）/未知工程类型
            out.append((proj_path, proj_path))
        return out
    if name == "pom.xml":
        span = _pom_modules_span(text)
        if span:
            for m in re.findall(r"<module>\s*([^<\s]+)\s*</module>", text[span[0]:span[1]]):
                out.append((m, f"{m.rstrip('/')}/pom.xml"))
    elif name in ("settings.gradle", "settings.gradle.kts"):
        if not _gradle_dynamic_hit(text):
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
        # W-1（21号文）：块形式逐行解析（与 C4 add 侧同思路）——旧正则 `use\s+(?:\(\s*)?...`
        # 对 `use ( ... )` 块只捕首成员，第 2+ 成员永远不进 probes → 幽灵成员永远摘不掉
        # → go build 对幽灵 use 硬错 → 修复轮空转。
        # hunter F6：先剥 `//` 行注释再解析——注释里的字面 `use ( ./old )` 不进候选。
        text = re.sub(r"(?m)//[^\n]*", "", text)

        def _norm_use(entry: str) -> str:
            e = entry.split("//", 1)[0].strip().strip('"')
            if e.startswith("./"):
                e = e[2:]
            return e.strip("/")

        for blk in re.finditer(r"use\s*\((.*?)\)", text, re.S):
            for line in blk.group(1).splitlines():
                e = _norm_use(line)
                if e:
                    out.append((e, e))
        block_free = re.sub(r"use\s*\(.*?\)", "", text, flags=re.S)
        for m in re.finditer(r"(?m)^\s*use\s+([^\s()]+)", block_free):
            e = _norm_use(m.group(1))
            if e:
                out.append((e, e))
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
    if name == "package.json" and "/" not in rel_path:
        # ★R2 hunter★ npm 的 probes 返回 [] 有两种成因：无 workspaces 字段（合法，
        # 非聚合清单）vs JSON 损坏（幽灵可能漏摘）。removed=[] 时两者不可分 =
        # 「缺席必须机读可辨」失守。解析失败 → fail-open 原文返回 + WARNING。
        try:
            json.loads(text)
        except (ValueError, TypeError) as _jexc:
            logger.warning(
                "[workspace-manifest] npm workspaces prune 跳过：%s JSON 解析失败"
                "（幽灵成员可能漏摘；文件保留原文未动）: %s", rel_path, _jexc)
            return text, []
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
        elif name == "package.json" and "/" not in rel_path:
            # X-H3：JSON round-trip（_merge_npm_workspaces 已有先例）——文本手术对
            # JSON 数组 brittle，round-trip 确定性且只动 workspaces 列表。解析失败/
            # 形态缺席 → n=0 保留（绝不产出损坏 JSON）。缩进探测原文保真 + 末尾换行。
            try:
                _obj = json.loads(new_text)
            except (ValueError, TypeError):
                # 入口处已做整文件解析闸（损坏即 fail-open + WARNING 返回）——
                # 走到这里 JSON 必合法；本 try 只是防御 round-trip 中间态。
                _obj = None
            if isinstance(_obj, dict):
                _ws = _obj.get("workspaces")
                _lst = _ws.get("packages") if isinstance(_ws, dict) else _ws
                if isinstance(_lst, list):
                    _kept = [x for x in _lst
                             if not (isinstance(x, str) and _npm_norm_entry(x) == tok)]
                    if len(_kept) != len(_lst):
                        if isinstance(_ws, dict):
                            _ws["packages"] = _kept
                        else:
                            _obj["workspaces"] = _kept
                        new_text = (json.dumps(
                            _obj, indent=_detect_json_indent(text),
                            ensure_ascii=False) + "\n")
                        n = 1
        elif name.lower().endswith(".sln"):
            # X-H3：摘 Project...EndProject 整块 + ProjectConfigurationPlatforms 里
            # 该工程 GUID 的全部配置行（只摘块会把 GUID 配置留成悬挂引用）。tok=
            # 归一化工程路径，匹配两种斜杠。NestedProjects 的 GUID 悬挂引用 VS/msbuild
            # 容忍（仅影响解决方案树展示），保守不碰。
            pat = re.compile(
                r'(?m)^Project\("\{[^}]+\}"\)\s*=\s*"[^"]*",\s*"'
                + _sln_path_pattern(tok)
                + r'",\s*"\{([^}]+)\}"[^\n]*\r?\n[ \t]*EndProject\r?\n?')
            m = pat.search(new_text)
            if m:
                guid = m.group(1)
                new_text = new_text[:m.start()] + new_text[m.end():]
                new_text = re.sub(
                    r"(?m)^[ \t]*\{" + re.escape(guid) + r"\}\.[^\n]*\r?\n?",
                    "", new_text)
                n = 1
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
            # W-1（21号文）：先块内裸行删（`use ( ... )` 块是 go work use 默认产物，删除
            # 正则必须落在块 span 内匹配无 use 前缀的裸行——旧正则要求行首带 use，块内
            # 裸行永不命中 hits=0），再单行形式删。删除限定块 span（F2 同纪律）。
            for m in re.finditer(r"use\s*\((.*?)\)", new_text, re.S):
                # 批23 R1 hunter F1 sibling：尾部 `\r?$` 行尾锚——缺它时 `svc2`/`sub/svc`
                # 等前缀兄弟行被吃前缀（摘除 svc 残留 "2\n" 损坏 go.work）。
                # ★批26★：引号/尾斜杠容忍从 reconcile dedup 臂同药平移（go.work 词法
                # 允许引号字符串，读径 _norm_use 本就剥引号归一 ⇒ 摘除臂必须认同一
                # 形态，否则引号行残留成永久幽灵成员）。
                pat = re.compile(
                    r"(?m)^[ \t]*[\"']?\.?/?" + re.escape(tok)
                    + r"/?[\"']?[ \t]*(?://[^\n]*)?[ \t]*\r?$\n?")
                new_text, n = _sub_in_span(new_text, m.span(1), pat)
                if n:
                    break
            if not n:
                # 单行臂同药（批26）：引号/尾斜杠/行注释/`\r` 行尾与块臂对齐——
                # 治前 `use "./svc/" // 注释`（CRLF）四形态全不匹配=残留。
                pat = re.compile(
                    r"(?m)^[ \t]*use[ \t]+[\"']?\.?/?" + re.escape(tok)
                    + r"/?[\"']?[ \t]*(?://[^\n]*)?[ \t]*\r?$\n?")
                new_text, n = pat.subn("", new_text, count=1)
            # 摘空的 `use ( )` 残块整体移除（go.work 解析器对空块报错风险，防御性清理；
            # \s 含换行，跨行空块同匹配；行首锚定防误删 // 注释里的字面 "use ()"）
            new_text = re.sub(r"(?m)^[ \t]*use\s*\(\s*\)\r?\n?", "", new_text)
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
        for n in ("settings.gradle", "settings.gradle.kts", "Cargo.toml", "go.work",
                  "package.json"):
            p = root / n
            if p.is_file():
                cands.append(p)
        # X-H3：.sln 与 reconcile 同约定（C15：多 sln 排序取首，确定性可复现）
        _slns = sorted(p for p in root.glob("*.sln") if p.is_file())
        if _slns:
            cands.append(_slns[0])
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


# ════════════ T2（round63 死锁触发器结构性兜底）：三方基线·共享版本锚不可篡改 ════════════
# merge_shared_manifest 是【加法-only 两方并集】，自己登记了"内容级篡改无三方基线无法与覆盖
# 区分→被并回复活"的债。round63 正踩此洞：version-repair 把根 pom 顶层 <properties> 的
# 【共享版本锚】spring-boot.version 4.0.6→3.5.16（属于内容级篡改，既非 dependency 也非 module
# 条目）→ merge 原样放行 incoming 毒值 → 整 reactor 降代死锁。T1 已在 version-repair 源头禁此
# 改写；T2 是【独立三方基线闸】：pull-back 落盘后，用 git HEAD 基线校验 worker/repair 是否篡改
# 了【基线既有】的版本锚，命中即还原基线值（拒毒进共享树）。判据栈无关（版本锚在任何清单都有），
# 实现按 pom 精确解析；其它清单原样返回（未实证篡改面，保守）。fail-open。

# 顶层 <properties> 叶子属性 `<key>value</key>`（单行、value 不含尖括号）。
_PROP_LEAF_RE = re.compile(r"<([A-Za-z_][\w.\-]*)>([^<>]*)</\1>")


def _toplevel_property_map(text: str) -> dict[str, str]:
    """收集 pom 顶层 <properties>（排除 profiles/build 区）叶子属性 {key: value}。

    同名多值（跨块冲突/歧义）→ 剔除该键（宁可漏护绝不误改）。任何异常回空 dict。"""
    try:
        spans = _pom_region_spans(text)
        excl = spans["profiles"] + spans["build"]
        result: dict[str, str] = {}
        ambiguous: set[str] = set()
        for pm in re.finditer(r"<properties>(.*?)</properties>", text, re.S):
            if any(s <= pm.start() < e for s, e in excl):
                continue
            for m in _PROP_LEAF_RE.finditer(pm.group(1)):
                k, v = m.group(1), m.group(2).strip()
                if k in result and result[k] != v:
                    ambiguous.add(k)
                else:
                    result.setdefault(k, v)
        for k in ambiguous:
            result.pop(k, None)
        return result
    except Exception:  # noqa: BLE001
        return {}


def _toplevel_property_values(text: str, key: str) -> list[str]:
    """当前文本里某属性 key 在顶层 <properties>（排除 profiles/build）的【所有】叶子值。

    刻意不去重：复核实锤——盲插式毒会留【重复 <key> 叶子】（round47 双 version 前例），
    去重 map 会因歧义丢弃该键→检测被静默解除。逐值扫描才能对"有一个值≠基线"判篡改。"""
    spans = _pom_region_spans(text)
    excl = spans["profiles"] + spans["build"]
    pat = re.compile(r"<" + re.escape(key) + r">([^<>]*)</" + re.escape(key) + r">")
    vals: list[str] = []
    for m in pat.finditer(text):
        if any(s <= m.start() < e for s, e in excl):
            continue
        vals.append(m.group(1).strip())
    return vals


def _parent_version(text: str) -> str | None:
    """首个 <parent> 块内的 <version> 值（继承的平台/BOM 版本锚）；无则 None。"""
    try:
        pm = re.search(r"<parent>(.*?)</parent>", text, re.S)
        if not pm:
            return None
        vm = re.search(r"<version>\s*([^<]+?)\s*</version>", pm.group(1))
        return vm.group(1).strip() if vm else None
    except Exception:  # noqa: BLE001
        return None


def _restore_property_leaf(text: str, key: str, baseval: str) -> tuple[str, int]:
    """把顶层（非 profiles/build 区）属性 <key>…</key> 的值改回 baseval。返回 (新文本, 改动数)。"""
    spans = _pom_region_spans(text)
    excl = spans["profiles"] + spans["build"]
    pat = re.compile(r"<" + re.escape(key) + r">[^<>]*</" + re.escape(key) + r">")
    out: list[str] = []
    last = 0
    count = 0
    for m in pat.finditer(text):
        if any(s <= m.start() < e for s, e in excl):
            continue
        out.append(text[last:m.start()])
        out.append(f"<{key}>{baseval}</{key}>")
        last = m.end()
        count += 1
    out.append(text[last:])
    return "".join(out), count


def _restore_parent_version(text: str, baseval: str) -> tuple[str, int]:
    """把首个 <parent> 块内的 <version> 值改回 baseval。返回 (新文本, 改动数)。"""
    pm = re.search(r"<parent>(.*?)</parent>", text, re.S)
    if not pm:
        return text, 0
    inner = pm.group(1)
    new_inner, n = re.subn(
        r"<version>\s*[^<]+?\s*</version>",
        f"<version>{baseval}</version>", inner, count=1)
    if not n:
        return text, 0
    return text[:pm.start(1)] + new_inner + text[pm.end(1):], n


def restore_baseline_version_anchors(
        text: str, baseline_text: str, rel_path: str,
) -> tuple[str, list[dict]]:
    """三方基线闸：把 worker/repair 对【基线既有】版本锚的【篡改】还原为基线值。

    护住的锚：①顶层 <properties> 叶子属性（round63 死因本体：spring-boot.version）；
    ②<parent><version>（继承的平台版本）。判据=「基线里存在该锚且当前值≠基线值」→ 还原基线值。
    【加法】(新属性/新依赖/新模块、基线本无的键) 一律不动——只挡篡改既有锚，不挡合法扩充。
    仅 pom；其它清单原样返回（未实证篡改面）。任何异常 fail-open 返回原文。
    返回 (新文本, [{anchor, from, to}])。"""
    try:
        if rel_path.rsplit("/", 1)[-1].lower() != "pom.xml":
            return text, []
        if not baseline_text:
            return text, []
        base_props = _toplevel_property_map(baseline_text)
        restorations: list[dict] = []
        new_text = text
        for key, bval in base_props.items():
            # 逐值扫描（非去重 map）：任一顶层叶子值≠基线值即篡改。这样【重复叶子毒】
            # (盲插双 <key>) 也被捕获，不因歧义静默解除检测（silent-hunter #1）。
            cur_vals = _toplevel_property_values(new_text, key)
            if not cur_vals or all(v == bval for v in cur_vals):
                continue  # 键被删/全等基线 → 非值篡改，跳过（删除是别的形态，登记债）
            new_text, n = _restore_property_leaf(new_text, key, bval)
            if n:
                differing = next(v for v in cur_vals if v != bval)
                entry = {"anchor": f"property:{key}", "from": differing, "to": bval}
                if len(cur_vals) > 1:
                    entry["note"] = "multiple-current-leaves"  # 盲插重复叶子已一并收敛
                restorations.append(entry)
        b_pv = _parent_version(baseline_text)
        c_pv = _parent_version(new_text)
        if b_pv and c_pv and b_pv != c_pv:
            new_text, n = _restore_parent_version(new_text, b_pv)
            if n:
                restorations.append(
                    {"anchor": "parent.version", "from": c_pv, "to": b_pv})
        return new_text, restorations
    except Exception as exc:  # noqa: BLE001 — fail-open：绝不因解析异常阻断 pull-back
        logger.warning(
            "[workspace-manifest] T2 三方基线锚校验异常 fail-open: %s", exc)
        return text, []


# npm 依赖承载 section（#29-2 W-1 后半）：口径与 sibling_dep_repair._parse_npm 的
# 扫描集**逐字一致**——A2 注入落回【来源 section】(D14)，并集面窄于注入面就会漏。
_NPM_DEP_SECTIONS = ("dependencies", "devDependencies",
                     "peerDependencies", "optionalDependencies")


def _merge_npm_manifest(local_text: str, incoming_text: str, rel_path: str,
                        base_dir: Path | None = None) -> str:
    """B7（19号文）+ #29-2 W-1 后半：npm 聚合清单并集——local 独有的 workspaces 成员
    【与依赖条目】并回 incoming（与 pom <modules>+<dependencies> 双并集同构：并行
    worker 各注册一个子包/各补一个依赖，陈旧副本盲覆盖会丢）。

    支持两种 workspaces 形态：数组形（npm/yarn 经典）与 {"packages": [...]} 对象形
    （yarn 扩展）。仅当两侧都有成员列表时才并成员；incoming 无 workspaces 键 → 成员
    面保守跳过（不臆造结构，与 pom 侧"无主依赖区不并"同取舍），但**依赖面照并**——
    ★两面必须独立判★：原实现在 `not loc_ws or inc_ws is None` 处直接早返 incoming，
    若也把依赖并集挂在该早返之后，则"worker 整体重写根 package.json 丢掉 workspaces
    键"这一 _is_shared_manifest_on_disk 专门 OR 进来兜的场景里，依赖并集会连带失效
    （同一早返吃掉两个不相干的机制＝血规 10① 的接线覆盖缺口）。

    依赖并集键 = (section, 包名)：同名包可合法同时在 dependencies 与 devDependencies
    （前者运行时、后者构建期），跨 section 混同会把 dev 依赖并进运行时。版本冲突时
    **保留 incoming 的版本**（incoming 为基，本函数只做"补缺"不做"改值"——改值需三方
    基线才能判谁新，见下方加法-only 债）。
    合并仅在有真实缺失时发生 → 无缺失原样返回 incoming（零 diff churn）；有缺失时整
    文件经 JSON 重序列化（缩进探测保真）——格式归一是已知取舍，换确定性并集。
    加法-only 同 pom 侧债（内容级有意删除会被并回复活）。任何异常 fail-open 返回
    incoming。
    """
    import json as _json

    try:
        loc = _json.loads(local_text)
        inc = _json.loads(incoming_text)
        if not isinstance(loc, dict) or not isinstance(inc, dict):
            return incoming_text

        def _ws_list(obj: dict) -> "list | None":
            w = obj.get("workspaces")
            if isinstance(w, list):
                return w
            if isinstance(w, dict) and isinstance(w.get("packages"), list):
                return w["packages"]
            return None

        changed = False
        # ── ① 成员并集（B7 原有面）──
        loc_ws = _ws_list(loc)
        inc_ws = _ws_list(inc)
        if loc_ws and inc_ws is not None:
            missing = [x for x in loc_ws if x not in inc_ws]
            if missing:
                inc_ws.extend(missing)
                changed = True
                logger.info(
                    "[workspace-manifest] B7 npm workspaces 并集合并 %s：并回 local "
                    "独有成员 %d 个（陈旧副本覆盖丢注册面）", rel_path, len(missing))
        # ── ② 依赖并集（#29-2 W-1 后半）──
        for _sec in _NPM_DEP_SECTIONS:
            _lsec = loc.get(_sec)
            if not isinstance(_lsec, dict) or not _lsec:
                continue
            _isec = inc.get(_sec)
            if _isec is None:
                # incoming 无该 section：local 有则整段并回（A2 的 setdefault 也会新建
                # 该 section，故"不臆造结构"在这里不适用——结构是依赖条目自带的）。
                _isec = {}
            elif not isinstance(_isec, dict):
                continue  # incoming 该键是非对象（畸形）→ 不碰，保守
            _miss = {k: v for k, v in _lsec.items()
                     if isinstance(k, str) and isinstance(v, str) and k not in _isec}
            if not _miss:
                continue
            _isec.update(_miss)
            inc[_sec] = _isec
            changed = True
            logger.info(
                "[workspace-manifest] #29-2 npm 依赖并集合并 %s [%s]：并回 local 独有 "
                "%d 条（A2 兄弟坐标注入被陈旧副本盲覆盖会蒸发）: %s",
                rel_path, _sec, len(_miss), ",".join(sorted(_miss)))
        if not changed:
            return incoming_text
        # X-H3 R2：缩进探测 incoming 原文保真（4 空格/tab 文件不被重排成 2 空格）
        return _json.dumps(
            inc, ensure_ascii=False, indent=_detect_json_indent(incoming_text)) + "\n"
    except Exception as exc:  # noqa: BLE001 — fail-open 回退旧行为（盲覆盖）
        logger.warning("[workspace-manifest] B7 npm 合并异常 fail-open %s: %s", rel_path, exc)
        return incoming_text


def _merge_cargo_manifest(local_text: str, incoming_text: str, rel_path: str,
                          base_dir: Path | None = None) -> str:
    """#29-2 W-1 后半：Cargo.toml 依赖条目并集——local 独有的依赖并回 incoming。

    Cargo.toml 在 `_SHARED_MANIFEST_BASENAMES` 里（basename 命中即共享清单），故
    pull-back 走 merge 路径；而本函数之前不存在 ⇒ `merge_shared_manifest` 对它
    `return incoming_text` ⇒ 并行兄弟的 A2 注入被陈旧副本抹掉（R48c-1 死法换栈复发）。

    ★取证源复用 sibling_dep_repair._parse_cargo（写者的同一份解析器），不手抄第二份★：
    它已处理平表/内联表/点表/`workspace = true` 四形态并归一 crate 名（rustc 用下划线、
    manifest 常用连字符）。自己写正则＝口径与写者分叉，A2 注了而并集认不出就照丢
    （血规 10③ 的同族：复用单一事实源）。
    键 = (section, 归一名)：dev-dependencies 的条目绝不并进运行时 [dependencies]（与
    A2 注入侧 D14 同口径）。版本冲突保留 incoming（只补缺不改值，同 npm 侧）。

    诚实边界：点表形态（`[dependencies.foo]` 多行段）的 raw 是 None ⇒ 无法安全重建单行
    声明（丢 features 会静默改语义），这类缺失**不并**，只计数告警——宁可让构建如实
    再报一次缺依赖（A2 下轮会重注），不产出语义被削的 manifest。
    合并结果过 tomllib 校验，不合法即 fail-open 返回 incoming（绝不产出毒 manifest：
    这正是 W-6 的教训——闸自己制造它要防的"解析期崩塌连坐整个工作区"）。
    """
    try:
        import tomllib

        from swarm.worker.sibling_dep_repair import _parse_cargo, toml_section_anchor
        loc = _parse_cargo(local_text)
        if not loc:
            return incoming_text
        inc = _parse_cargo(incoming_text)
        # 键含 section：同名 crate 可合法同时在 dependencies 与 dev-dependencies
        inc_keys = {(v[3], k) for k, v in inc.items()}
        merged = incoming_text
        added: list[str] = []
        skipped_dot = 0
        for _norm, (_name, _ver, _raw, _sec) in sorted(loc.items()):
            if (_sec, _norm) in inc_keys:
                continue
            if not _raw:
                # 点表/无可移植版本形态：无单行原始声明可移植 → 诚实丢弃
                skipped_dot += 1
                continue
            # #29-2 复核：与注入侧共用 `toml_section_anchor`（含"行尾注释"合法形态）。
            # 两侧各写一份正则 ⇒ 必然漂移（本次就是漂在同一处：都漏了行尾注释）。
            m = toml_section_anchor(merged, _sec)
            if m:
                idx = (merged.index("\n", m.end()) + 1
                       if "\n" in merged[m.end():] else len(merged))
                merged = merged[:idx] + _raw.rstrip("\n") + "\n" + merged[idx:]
            else:
                # incoming 无该 section 区 → 追加（结构由依赖条目自带，非臆造）
                merged = merged.rstrip("\n") + f"\n\n[{_sec}]\n{_raw.rstrip(chr(10))}\n"
            inc_keys.add((_sec, _norm))
            added.append(f"{_sec}/{_name}")
        if skipped_dot:
            logger.warning(
                "[workspace-manifest] #29-2 cargo 依赖并集 %s：%d 条点表/无版本形态"
                "无单行声明可移植 → 诚实不并（构建会如实再报缺依赖，A2 下轮重注）",
                rel_path, skipped_dot)
        if not added:
            return incoming_text
        # ★后置校验判【事实】而非【语法】★（自查实测逼出来的设计）
        # 插入锚点是正则找 `[section]` 行 —— 若该行出现在**多行字符串值**里（`description`
        # 写用法说明是常见形态），锚点就是假的：依赖被插进字符串里 ⇒ ① description 值被
        # 污染进交付物 ② 真依赖根本没并进去 ③ **产出仍是合法 TOML**，只验 `tomllib.loads`
        # 的闸完全看不见（这也正是那道闸原本不可独立证伪的原因——冗余防御互相兜底）。
        # 故校验升级为三条后置断言：合法 + 声称并入的条目**真的**在目标 section 里 +
        # **其它值一个都没变**。任一不成立 → fail-open 返回 incoming（诚实不并优于产毒/伪并）。
        try:
            _inc_obj = tomllib.loads(incoming_text)
            _new_obj = tomllib.loads(merged)
        except Exception as _texc:  # noqa: BLE001
            logger.warning(
                "[workspace-manifest] #29-2 cargo 依赖并集产出非法 TOML → fail-open "
                "放弃合并 %s: %s", rel_path, _texc)
            return incoming_text
        # ★一条判据，不是两条互相兜底的★：把"声称并入的键真的在"与"其它值没变"写成两个
        # 独立 if 时，假锚点场景**两条同时触发** ⇒ 任一单独突变都仍绿 ⇒ 两条都不可证伪
        # （冗余防御=互相兜底，本仓已登记的教训）。故收敛成一条**端状态对账**：
        # 摘掉声称新增的键之后，必须与 incoming 逐键相等。它严格强于"键存在"检查——
        # 插进字符串会让那个值变、插对了则键在且别处不变，两种情形都被这一条覆盖。
        # 具体是"键没落地"还是"别处被改"只作日志诊断，不再各自设闸。
        _added_keys = {(_s, _n) for _s, _n in
                       (a.split("/", 1) for a in added)}
        _stripped = {k: (dict(v) if isinstance(v, dict) else v)
                     for k, v in _new_obj.items()}
        for _sec2, _name2 in _added_keys:
            if isinstance(_stripped.get(_sec2), dict):
                _stripped[_sec2].pop(_name2, None)
                # 只有 incoming **根本没有**该 section 时才连 section 一起摘（我们新建了它）。
                # 判据必须是【键是否存在】而非【值真假】：incoming 有一个**空** `[dependencies]`
                # 段是常见形态（`{}` 为假），按真假判会把它一起摘掉 ⇒ 与 incoming 对不上 ⇒
                # 合法合并被自己的校验冤杀（首跑实测：内联表用例当场红）。
                if not _stripped[_sec2] and _sec2 not in _inc_obj:
                    _stripped.pop(_sec2, None)
        if _stripped != _inc_obj:
            _absent = [f"{s}/{n}" for s, n in sorted(_added_keys)
                       if n not in (_new_obj.get(s) or {})]
            logger.warning(
                "[workspace-manifest] #29-2 cargo 依赖并集端状态对账不过 → fail-open "
                "放弃合并 %s（文本级插入落错位置，如 `[section]` 那行出现在多行字符串值里）"
                "；声称并入却不在结果里的条目: %s", rel_path, _absent or "无")
            return incoming_text
        logger.info(
            "[workspace-manifest] #29-2 cargo 依赖并集合并 %s：并回 local 独有 %d 条"
            "（A2 兄弟坐标注入被陈旧副本盲覆盖会蒸发）: %s",
            rel_path, len(added), ",".join(added))
        return merged
    except Exception as exc:  # noqa: BLE001 — fail-open 回退旧行为（盲覆盖）
        logger.warning("[workspace-manifest] #29-2 cargo 合并异常 fail-open %s: %s", rel_path, exc)
        return incoming_text


def _go_excluded_mods(text: str) -> dict[str, set[str]]:
    """go.mod 的 exclude 声明 {module: {排除版本集}}（单行 + 块内两形态）。

    ★增益层告警专用（#29-5 W-2 R1 reviewer LOW-1）★：不作合并判定依据——与
    `_parse_go` 是独立最小扫描（它不看 exclude），口径漂移后果=少一条 WARNING，
    绝不改合并行为。尾随注释等病态形态漏扫同属「少告警」无害方向。
    ★R2 hunter N2：版本粒度必须保留★——exclude 是按【版本】排除的，
    `exclude x/y v1` + `require x/y v2` 完全合法；只按 module 告警会冤报「必败」
    （误报=「缺席可辨」的镜像破产：狼来了 ⇒ 真信号被一起忽略）。
    """
    out: dict[str, set[str]] = {}
    in_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if in_block:
            if line.startswith(")"):
                in_block = False
            else:
                m = re.match(r"([^\s()]+/[^\s()]+)\s+(v[^\s]+)", line)
                if m:
                    out.setdefault(m.group(1), set()).add(m.group(2))
            continue
        if re.match(r"exclude\s*\(\s*$", line):
            in_block = True
            continue
        m = re.match(r"exclude\s+([^\s()]+/[^\s()]+)\s+(v[^\s]+)", line)
        if m:
            out.setdefault(m.group(1), set()).add(m.group(2))
    return out


def _merge_go_manifest(local_text: str, incoming_text: str, rel_path: str,
                       base_dir: Path | None = None) -> str:
    """#29-5 W-2 后半：go.mod require 并集——local 独有的 require 并回 incoming。

    分类翻转（根 go.mod 入共享集，sandbox.py `_is_shared_manifest`）后本臂才可达——
    分类档位与本臂是同一个洞的两面，同批落地（血规 10①：先数调用点，不留死代码）。
    此前 go.mod 完全不进 `merge_shared_manifest`，pull-back 走裸写分支（不取 flock、
    不并集）⇒ 并行兄弟的 A2 注入被陈旧副本抹掉（R48c-1 死法在 Go 栈一件治本都没接）。

    ★取证源复用 sibling_dep_repair._parse_go（写者的同一份解析器），不手抄第二份★：
    它已处理单行/block 两形态、replace/exclude 块不算声明，并把 replace 左侧出现的
    require 版本置 None（本地模块伴随 replace 的坐标不可移植）。键 = module path；
    版本冲突保留 incoming（只补缺不改值，与 npm/cargo 同口径）。

    诚实边界：ver=None（replace 伴随）的 local 独有条目**不并**，只计数告警——只注
    require 不带伴随 replace 拉取必败（写者侧同口径），宁可构建如实再报缺依赖
    （A2 下轮会重注），不产出必败的 manifest。
    插入形状与写者 `_inject_go` 完全一致：incoming 有 `require (` block → 插块内
    （`\\tmod ver\\n`）；无 block → 追加单行 `require mod ver`。`// indirect` 注释
    不移植（go 工具链 tidy 时自行重算，丢注释不改语义）。
    后置校验=一条端状态对账（同 cargo 臂，冗余防御不可独立证伪的教训）：重解析
    merged，摘掉声称并入的 mod 后必须与 `_parse_go(incoming)` 逐键相等——插错位置/
    改到别处都被这一条覆盖。任一不成立 → fail-open 返回 incoming（诚实不并优于产毒）。
    """
    try:
        from swarm.worker.sibling_dep_repair import _parse_go
        loc = _parse_go(local_text)
        if not loc:
            # ★#29-5 W-2 R1（hunter F2 实跑坐实）：缺席可辨★——`_parse_go` 对合法
            # 语法有盲区（`require ( // 尾随注释` 的块开括号 `\s*$` 锚尾不认、
            # 无斜杠 module path 的 `require mymod v1.0.0` 不匹配 `_GO_DEP_LINE_RE`），
            # 此时 local 的【全部】require 被盲覆盖蒸发（R48c-1 死法）而零信号——
            # 「local 解析为空」与「local 真没有 require」必须机读可分。
            # （修 `_parse_go` 认尾随注释=写者侧解析器口径变更，需单独评审，登记债。）
            # R2 hunter N1：判据必须是【行首 require 语句】而非子串——注释里含
            # "require" 字样（`// require block intentionally empty`）会冤报。
            if re.search(r"(?m)^\s*require[\s(]", local_text):
                logger.warning(
                    "[workspace-manifest] #29-5 go.mod 依赖并集 %s：local 含 require "
                    "字样但解析为空（疑似解析盲区：尾随注释块/无斜杠 module path）→ "
                    "本轮不并，local 独有条目将盲覆盖丢失", rel_path)
            return incoming_text
        inc = _parse_go(incoming_text)
        _inc_excluded = _go_excluded_mods(incoming_text)
        merged = incoming_text
        added: list[str] = []
        skipped_replace = 0
        for mod in sorted(loc):
            if mod in inc:
                continue  # incoming 已有（含版本不同）→ 保留 incoming，只补缺不改值
            ver = loc[mod]
            if not ver:
                # replace 伴随（本地模块）坐标不可移植 → 诚实不并
                skipped_replace += 1
                continue
            if mod in _inc_excluded and ver in _inc_excluded[mod]:
                # reviewer LOW-1（实跑坐实）：并入的 mod@ver 在 incoming 的 exclude 里
                # ⇒ require+exclude 同模块同版本=必败 manifest。仍并（加法-only 语义
                # 不变，让 go build 把矛盾如实报出来），但必须留机读痕迹。
                # 写者 _inject_go 有同型洞（族问题，登记债）。
                # R2 hunter N2：判据到【版本】粒度——exclude 按版本排除，
                # 不同版本（exclude v1 + require v2）合法，绝不冤报。
                logger.warning(
                    "[workspace-manifest] #29-5 go.mod 依赖并集 %s：并入的 %s %s 命中 "
                    "incoming 的 exclude ⇒ require+exclude 同模块同版本必败（如实并入，"
                    "交构建报错暴露；写者侧同型洞登记债）", rel_path, mod, ver)
            m = re.search(r"^\s*require\s*\(\s*$", merged, re.M)
            if m:
                nl = merged.find("\n", m.end())
                if nl == -1:
                    # `require (` 悬在 EOF 未闭合 = 畸形 manifest → fail-open 不碰
                    # （写者 _inject_go 同形 fail-closed，合并侧 fail-open 回旧行为）
                    logger.warning(
                        "[workspace-manifest] #29-5 go.mod 依赖并集 %s：incoming 的 "
                        "require block 悬在 EOF 未闭合（畸形）→ fail-open 放弃合并", rel_path)
                    return incoming_text
                merged = merged[:nl + 1] + f"\t{mod} {ver}\n" + merged[nl + 1:]
            else:  # 无 block → 追加单行 require（与写者同形状）
                merged = merged.rstrip("\n") + f"\nrequire {mod} {ver}\n"
            added.append(mod)
        if skipped_replace:
            logger.warning(
                "[workspace-manifest] #29-5 go.mod 依赖并集 %s：%d 条 replace 伴随"
                "（本地模块）坐标不可移植 → 诚实不并（构建会如实再报缺依赖，A2 下轮重注）",
                rel_path, skipped_replace)
        if not added:
            return incoming_text
        # 端状态对账：摘掉声称并入的 mod 后，必须与 incoming 逐键相等。
        _added = set(added)
        _stripped = {k: v for k, v in _parse_go(merged).items() if k not in _added}
        if _stripped != inc:
            _absent = [x for x in added if x not in _parse_go(merged)]
            logger.warning(
                "[workspace-manifest] #29-5 go.mod 依赖并集端状态对账不过 → fail-open "
                "放弃合并 %s（文本级插入改动了 require 区之外的内容）；声称并入却不在"
                "结果里的条目: %s", rel_path, _absent or "无")
            return incoming_text
        logger.info(
            "[workspace-manifest] #29-5 go.mod 依赖并集合并 %s：并回 local 独有 %d 条"
            "（A2 兄弟坐标注入被陈旧副本盲覆盖会蒸发）: %s",
            rel_path, len(added), ",".join(added))
        return merged
    except Exception as exc:  # noqa: BLE001 — fail-open 回退旧行为（盲覆盖）
        logger.warning("[workspace-manifest] #29-5 go.mod 合并异常 fail-open %s: %s", rel_path, exc)
        return incoming_text


def _merge_pom_manifest(local_text: str, incoming_text: str, rel_path: str,
                        base_dir: Path | None = None) -> str:
    """Maven pom：依赖（plain/dm 分账）+ `<modules>` 成员并集（R48c-1 原臂，C-6 抽为注册表项）。

    复核 B：依赖键=(g,a,区域) 分账——dm 条目绝不挡 classpath 修复（RuoYi 根 pom
    是巨型 dm，跨区混同=原 live 缺陷的残留半径）。复核 C：profiles/build 插件
    依赖整体不参与（不收集/不插入其区）。复核 4：modules 并回带存在性校验
    （base_dir 提供时，目录已不存在的幽灵成员不复活）。
    """
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


_GRADLE_INCLUDE_STMT_RE = re.compile(r"(?m)^[ \t]*include[ \t]*\(?([^\n)]*)\)?[ \t]*$")
_GRADLE_INCLUDE_TOKEN_RE = re.compile(r"['\"]:?([\w:.-]+)['\"]")


def _gradle_include_tokens(text: str) -> list[str]:
    """merge 面专用的 include token 提取（比 `manifest_member_probes` 宽，批8 R1 reviewer MEDIUM）。

    probes 的「单 token 独占一行」边界是给【删除】面（prune/strip）的：多 token 行不删
    只是残留，方向安全；merge 是【加法】面，跳过 = 并行兄弟的注册整份蒸发（复用单一
    事实源≠复用其消费契约：同一份解析，删/加两面的错误方向相反）。本函数支持
    多 token 单行（`include ':a', ':b'`）与行尾注释；先剥 `//` 注释再逐语句取引号
    token，消费方自行去重 ⇒ 不产生重复 include。
    """
    stripped = re.sub(r"(?m)//[^\n]*", "", text)
    out: list[str] = []
    for m in _GRADLE_INCLUDE_STMT_RE.finditer(stripped):
        out.extend(_GRADLE_INCLUDE_TOKEN_RE.findall(m.group(1)))
    return out


def _merge_gradle_settings_manifest(local_text: str, incoming_text: str, rel_path: str,
                                    base_dir: Path | None = None) -> str:
    """settings.gradle(.kts)：★只并 include 成员列表，绝不并依赖区★（30 号文 C-6）。

    依赖区是 Groovy/Kotlin DSL（可含变量/函数调用），文本级并集无法保证语义——这条
    老顾虑对【依赖区】站得住、照留；但它从未论证过【成员列表】：`include` 与 Maven
    `<modules>` 结构同构，不并 = 并行兄弟的 `include ':mod'` 被陈旧副本整份蒸发
    （R48c-1 last-write-wins 换个 basename 复发，实测 merged==incoming）。
    成员解析用 merge 面专用的 `_gradle_include_tokens`（多 token 行/行尾注释都收——
    probes 的单行边界是删除面契约，加法面跳过=丢注册，批8 R1 reviewer MEDIUM）。
    """
    # 动态枚举（fileTree/变量/函数 include）两侧任一命中即保守直通——文本级加法
    # 可能改变其语义或造成重复 include（与 reconcile 侧同判据）。
    # ★批8 R1 hunter + 批23 R1 hunter F3★：剥 `//` 注释已下沉进 `_gradle_dynamic_hit`
    # 本体（三面同源），merge 面不再私持预处理。
    hit = _gradle_dynamic_hit(local_text) or _gradle_dynamic_hit(incoming_text)
    if hit:
        logger.warning(
            "[workspace-manifest] C-6 %s 含动态 include 枚举（命中子串 %r），成员并集"
            "保守直通（文本级加法对动态枚举有语义风险）", rel_path, hit.group(0))
        return incoming_text
    inc = set(_gradle_include_tokens(incoming_text))
    add = [tok for tok in _gradle_include_tokens(local_text) if tok not in inc]
    if not add:
        return incoming_text
    is_kts = rel_path.lower().endswith(".kts")
    lines = [(f'include(":{t}")' if is_kts else f"include ':{t}'") for t in add]
    merged = incoming_text.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
    logger.info(
        "[workspace-manifest] C-6 gradle settings 成员并集 %s：并回 local 独有 "
        "include %d 条（兄弟子任务模块注册被陈旧副本盲覆盖会整份蒸发）: %s",
        rel_path, len(add), ",".join(add))
    return merged


def _merge_go_work_manifest(local_text: str, incoming_text: str, rel_path: str,
                            base_dir: Path | None = None) -> str:
    """go.work：只并 `use` 成员（30 号文 C-6）。

    `use` 列表与 Maven `<modules>` 结构同构（注册目录集），不并 = 并行兄弟的
    `use ./svc` 整份蒸发 → 下一轮 `go build ./...` 找不到该模块。解析复用
    `manifest_member_probes`（块/单行两形态、剥注释，与 add/prune 同源）。
    追加单行 `use ./x` 到块形式文件合法（go.work 允许混合形态；reconcile 侧 :623 同法）。
    """
    inc = {tok for tok, _ in manifest_member_probes(rel_path, incoming_text)}
    add = [tok for tok, _ in manifest_member_probes(rel_path, local_text)
           if tok not in inc]
    if not add:
        return incoming_text
    lines = [f"use ./{t}" for t in add]
    merged = incoming_text.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
    logger.info(
        "[workspace-manifest] C-6 go.work 成员并集 %s：并回 local 独有 use %d 条"
        "（兄弟子任务模块注册被陈旧副本盲覆盖会整份蒸发）: %s",
        rel_path, len(add), ",".join(add))
    return merged


def _merge_sln_manifest(local_text: str, incoming_text: str, rel_path: str,
                        base_dir: Path | None = None) -> str:
    """*.sln：只并 Project 对（工程块 + 构建配置行）（30 号文 C-6）。

    成员键=归一化工程路径（与 probes/strip 同源，大小写不敏感——Windows 生态
    Web.CSPROJ 常见）。Project 块从 local 原文逐块搬运（保留 local 自带 GUID/名称），
    配置行按 reconcile 同格式补两档（Debug/Release|Any CPU）。
    ★缺 ProjectConfigurationPlatforms 段整体不并★（与 reconcile :558 同治本：
    只插 Project 块漏配置行=「有工程无构建配置」的损坏 .sln，VS/msbuild 确定性失败）。
    """
    inc = {tok.lower() for tok, _ in manifest_member_probes(rel_path, incoming_text)}
    loc_paths = {tok.lower(): tok for tok, _ in manifest_member_probes(rel_path, local_text)}
    add = [loc_paths[k] for k in loc_paths if k not in inc]
    # ★批8 R1 hunter★ local 里【非已知后缀/URL 工程】的 Project（解决方案文件夹等）
    # probes 不收 ⇒ 不并——这是与 probes 同契约的刻意边界，但跳过必须留痕（缺席
    # 机读可辨），不能让「加法-only」承诺静默缺一角。★必须在 `if not add` 早返之前
    # 计数★——local 只新增了文件夹而没有可并工程时，早返会把留痕一并跳过。
    _skipped_unknown = 0
    for m in _SLN_PROJECT_RE.finditer(local_text):
        _pp = m.group(2).replace("\\", "/")
        if "://" in _pp or Path(_pp).suffix.lower() not in _SLN_TYPE_GUID:
            if _pp.lower() not in inc:
                _skipped_unknown += 1
    if _skipped_unknown:
        logger.warning(
            "[workspace-manifest] C-6 %s：local 有 %d 个非已知类型/URL 工程条目"
            "（解决方案文件夹等）按契约不并（如需保留请人工合入）",
            rel_path, _skipped_unknown)
    if not add:
        return incoming_text
    cfg_section = re.search(
        r"(GlobalSection\(ProjectConfigurationPlatforms\)[^\n]*\n)", incoming_text)
    if "\nGlobal" not in ("\n" + incoming_text) or not cfg_section:
        logger.warning(
            "[workspace-manifest] C-6 %s 缺 Global/ProjectConfigurationPlatforms 段，"
            ".sln 成员并集整体不并（绝不只插 Project 块漏配置行=产出损坏 sln）", rel_path)
        return incoming_text
    # ★批8 R1 hunter HIGH★ GUID 判重：local 块 GUID 与 incoming 既有块撞（不同工程
    # 同 GUID）时搬运会产出【GUID 重复】的无效 .sln——fail-closed 跳过该块+WARNING，
    # 宁缺不产坏文件。
    inc_guids = {m.group(1).upper() for m in re.finditer(
        r'Project\("\{[^}]+\}"\)\s*=\s*"[^"]*",\s*"[^"]+",\s*"(\{[^}]+\})"',
        incoming_text)}
    blocks: list[str] = []
    cfg_lines: list[str] = []
    want = {a.lower() for a in add}
    skipped_guid: list[str] = []
    for m in re.finditer(
            r'Project\("\{[^}]+\}"\)\s*=\s*"[^"]*",\s*"([^"]+)",\s*"(\{[^}]+\})"'
            r"[^\n]*\nEndProject\n?", local_text):
        norm = m.group(1).replace("\\", "/")
        if norm.lower() not in want:
            continue
        guid = m.group(2)
        if guid.upper() in inc_guids:
            skipped_guid.append(norm)
            continue
        blocks.append(m.group(0))
        for cfg in ("Debug", "Release"):
            cfg_lines.append(
                f"\t\t{guid}.{cfg}|Any CPU.ActiveCfg = {cfg}|Any CPU\n"
                f"\t\t{guid}.{cfg}|Any CPU.Build.0 = {cfg}|Any CPU\n"
            )
    if skipped_guid:
        logger.warning(
            "[workspace-manifest] C-6 %s：%d 个 local 工程块 GUID 与 incoming 撞车，"
            "fail-closed 跳过不并（搬运会产出 GUID 重复的无效 .sln）: %s",
            rel_path, len(skipped_guid), skipped_guid)
    if not blocks:
        return incoming_text
    merged = incoming_text.replace("\nGlobal", "\n" + "".join(blocks) + "Global", 1)
    cfg2 = re.search(
        r"(GlobalSection\(ProjectConfigurationPlatforms\)[^\n]*\n)", merged)
    idx = cfg2.end()
    merged = merged[:idx] + "".join(cfg_lines) + merged[idx:]
    logger.info(
        "[workspace-manifest] C-6 .sln 成员并集 %s：并回 local 独有 Project %d 个"
        "（兄弟子任务工程注册被陈旧副本盲覆盖会整份蒸发）: %s",
        rel_path, len(blocks), ",".join(add))
    return merged


def _merge_conservative_passthrough(local_text: str, incoming_text: str, rel_path: str,
                                    base_dir: Path | None = None) -> str:
    """build.gradle(.kts) 的【刻意保守直通】（30 号文 C-6）——注册进 `_MERGERS` 是为了
    让「不合并」成为【显式登记的决定】而非静默 fall-through。

    不并的理由（老 docstring 顾虑，C-6 复核判定站得住）：其依赖区是 Groovy/Kotlin DSL
    （可含变量/函数调用/apply from），文本级并集无法保证语义 ⇒ 盲并可产语法坏文件。
    它没有 `include` 成员列表（那在 settings.gradle），故无成员可并。内容分叉时
    留 INFO 可观测（不并≠无损失面——依赖区分叉的损失如实登记，留给依赖修复环路
    下轮按 ground-truth 重注，好过产出语义被削的坏文件）。
    """
    if local_text != incoming_text:
        logger.info(
            "[workspace-manifest] C-6 %s 内容分叉，按登记保守直通不并（Groovy/Kotlin DSL "
            "依赖区文本级并集无语义保证；依赖缺失由修复环路下轮按 ground-truth 重注）",
            rel_path)
    return incoming_text


# ★30 号文 C-6★ 合并器分派【显式注册表】——键=小写 basename（`.sln` 为后缀特例键）。
# 治前分派是 if 链 + 静默 `return incoming_text`：路由集（`_is_shared_manifest` 为 True
# 的 basename）7+3 项而分派只认 4 项 ⇒ settings.gradle/go.work/.sln 的成员条目在
# pull-back 时整份丢失（last-write-wins 换 basename 复发，实测 merged==incoming）。
_MERGERS: dict[str, Callable[[str, str, str, "Path | None"], str]] = {
    "pom.xml": _merge_pom_manifest,                    # 依赖(plain/dm 分账)+成员
    "package.json": _merge_npm_manifest,               # 成员+依赖
    "cargo.toml": _merge_cargo_manifest,               # 依赖
    "go.mod": _merge_go_manifest,                      # require 依赖（#29-5 W-2）
    "settings.gradle": _merge_gradle_settings_manifest,    # 只并 include 成员
    "settings.gradle.kts": _merge_gradle_settings_manifest,
    "go.work": _merge_go_work_manifest,                # 只并 use 成员
    ".sln": _merge_sln_manifest,                       # 只并 Project 对（后缀特例键）
    # 刻意保守直通档（登记的不并，理由见 `_merge_conservative_passthrough`）
    "build.gradle": _merge_conservative_passthrough,
    "build.gradle.kts": _merge_conservative_passthrough,
}


def _assert_mergers_cover_routing() -> None:
    """C-6 导入期闸：路由集 ⊆ `_MERGERS`——加共享清单路由却忘加合并臂，缺一个直接
    ImportError（治前静默 `return incoming` = 成员整份丢失零信号）。

    ★批23 C-6b#3③ 行为化★：治前闸体手抄镜像路由集（常量 ∪ 字面量 go.mod/package.json
    /.sln）——镜像与 `_is_shared_manifest` 函数体是两套枚举，函数体改分支条件时镜像
    静默 stale（「为漏项造的兜底网不能用同一份枚举编」族）。现改为【行为探针】：
    候选名逐个过真函数 `_is_shared_manifest`——
      · 正向：路由为真而无臂 → ImportError（原职）；
      · 反向 1：常量成员/探针形状【不再路由】→ ImportError（函数体与常量分叉当场炸）；
      · 反向 2：`_MERGERS` 有臂但函数不路由 → ImportError（死臂=另一条漂移方向）。
    诚实边界：探针形状集枚举分支【形状】（basename 档直接用常量本身=与函数同源；
    非 basename 档=根 go.mod/根 package.json/.sln 后缀三形状 + 嵌套 package.json 内容
    分支两枚带 content 探针）——新增【形状全新】的分支需同批加探针形状，闸无法自发现。
    「只改名单/条件随行为自动跟随」仅对路径档成立（R1 hunter F4：内容分支判据腐化
    曾被 docstring 过度声称覆盖，现由带 content 探针直接钉住）。
    """
    from swarm.worker.sandbox import _SHARED_MANIFEST_BASENAMES, _is_shared_manifest
    probes = {b.lower() for b in _SHARED_MANIFEST_BASENAMES} | {
        "go.mod", "package.json", "__probe__.sln"}

    def _arm_key(p: str) -> str:
        return ".sln" if p.endswith(".sln") else p

    unrouted = sorted(p for p in probes if not _is_shared_manifest(p))
    # R1 hunter F4：嵌套 package.json【内容分支】行为探针——路径档探针覆盖不到
    # workspaces 键判据，该分支腐化=嵌套 monorepo 静默失去 flock/merge 保护。
    if not _is_shared_manifest("pkg/package.json", '{"workspaces": []}'):
        unrouted.append("pkg/package.json(workspaces 内容分支)")
    over_routed: list[str] = []
    if _is_shared_manifest("pkg/package.json", "{}"):
        over_routed.append("pkg/package.json(无 workspaces 却路由)")
    missing = sorted(
        p for p in probes
        if _is_shared_manifest(p) and _arm_key(p) not in _MERGERS)
    dead_arms = sorted(
        k for k in _MERGERS
        if not _is_shared_manifest("__probe__.sln" if k == ".sln" else k))
    if unrouted or missing or dead_arms or over_routed:
        raise ImportError(
            f"C-6：共享清单路由与合并臂漂移——不再路由的常量/探针 {unrouted}，"
            f"不该路由却路由 {over_routed}，有路由无合并臂 {missing}，"
            f"有臂但函数不路由(死臂) {dead_arms}。"
            f"`_is_shared_manifest` 改路由必须同批对齐 `_MERGERS`（含刻意保守直通档），"
            f"缺臂=pull-back 盲覆盖、并行兄弟的成员注册整份蒸发（R48c-1 换 basename 复发）")


_assert_mergers_cover_routing()


def merge_shared_manifest(local_text: str, incoming_text: str, rel_path: str,
                          base_dir: "Path | None" = None) -> str:
    """共享清单并集合并：incoming 为基 + local 独有的依赖/成员条目并回 → 合并文本。

    ★30 号文 C-6：分派走 `_MERGERS` 显式注册表 + 导入期断言路由集⊆表★。
    治前 if 链只认 4 个 basename，其余静默 `return incoming_text` ⇒ settings.gradle
    的 include / go.work 的 use / .sln 的 Project 在 pull-back 时整份蒸发（flock 正确
    串行化之后，丢失发生在锁内的合并逻辑里）。build.gradle(.kts) 注册为【刻意保守
    直通】（Groovy/Kotlin DSL 依赖区文本级并集无语义保证）——不并是登记的决定。
    加法-only 已知取舍：内容级"有意删除"会被并回复活（两方合并无法与覆盖丢失区分，
    需三方基线——登记债）；文件级删除走 delete_files 专路不受影响。异常 fail-open。
    """
    try:
        if local_text == incoming_text:
            return incoming_text  # 两侧全等 → 无可并（省一次解析；不影响语义）
        name = rel_path.rsplit("/", 1)[-1].lower()
        merger = _MERGERS.get(name)
        if merger is None and name.endswith(".sln"):
            merger = _MERGERS[".sln"]
        if merger is None:
            # 导入期闸保证路由集⊆表 ⇒ 到这里的名字是路由集外的调用（嵌套清单等）。
            # 保守直通+WARNING 留痕，绝不静默（硬检查④：缺席必须机读可辨）。
            logger.warning(
                "[workspace-manifest] C-6 %s 不在 _MERGERS 注册表（表外调用）→ 保守直通",
                rel_path)
            return incoming_text
        return merger(local_text, incoming_text, rel_path, base_dir)
    except Exception as exc:  # noqa: BLE001 — fail-open 回退旧行为（盲覆盖）
        # ★批8 R1 hunter HIGH★ 异常 fail-open 必须与【政策性直通】（动态枚举/表外
        # 调用/登记保守档的 WARNING）机读可区分：级别升 ERROR + exc_type 结构化键——
        # 异常=合并臂有 bug，local 贡献整份丢失，运维按 WARNING 噪音漏看就是丢数据。
        logger.error(
            "[workspace-manifest] R48c-1 合并异常 fail-open %s（exc_type=%s）——"
            "这是【合并臂异常】而非登记保守直通，local 贡献已整份丢失，必须修合并臂: %s",
            rel_path, type(exc).__name__, exc)
        return incoming_text

# ══════════════ H2（round48c 深读）：FAIL 子任务共享清单贡献外科摘除 ══════════════
# L1 最终未通过的子任务，其对共享清单的【新增条目】必须从本地共享树摘除——盲用 HEAD
# 恢复会冲掉并行他人的合法注册（正是 R48c-1 要防的 clobber），故做减法外科：
# 本 worker 版本相对 HEAD 新增的 dependency(g,a,区域)/module 条目 → 从当前 local 文本
# 中逐块删除。他人贡献的同名条目碰撞面：极罕见且下一轮 reconcile/dep-repair 会按
# ground truth 补回（加法侧幂等），correctness 优先。

def strip_worker_manifest_contribs(
        local_text: str, worker_text: str, head_text: str, rel_path: str,
) -> tuple[str, int]:
    """从 local 摘除【worker 相对 HEAD 新增】的清单条目 → (新文本, 摘除数)。

    Maven pom（毒性实证面）+ npm workspaces 显式成员 + .sln Project 条目
    （X-H3 R2 hunter HIGH：FAIL 子任务对根 package.json/.sln 的新增贡献残留
    本地共享树——旧实现非 pom 恒原样返回且零信号）。其它清单返回原文。fail-open。
    """
    _name = rel_path.rsplit("/", 1)[-1]
    try:
        if _name == "package.json" and "/" not in rel_path:
            # npm：worker 相对 HEAD 新增的显式成员 → 从 local 摘除（复用 prune 臂，
            # 三面同源）。glob/否定条目不参与（probes 本就不收）。
            _added = (set(_npm_explicit_members(worker_text))
                      - set(_npm_explicit_members(head_text)))
            if not _added:
                return local_text, 0
            removed_n = 0
            new_text = local_text
            for tok in sorted(_added):
                new_text, _rm = prune_manifest_members(
                    rel_path, new_text, lambda p, _t=tok: False if p == f"{_t}/package.json" else None)
                removed_n += len(_rm)
            if removed_n:
                logger.info(
                    "[workspace-manifest] H2 npm workspaces 摘除 FAIL 子任务新增成员 %s"
                    "（残留会让 npm ci 去装从未合入的包）: %s", sorted(_added), rel_path)
            return new_text, removed_n
        if _name.lower().endswith(".sln"):
            # .sln：worker 相对 HEAD 新增的工程路径 → 从 local 摘 Project 块+GUID 配置行
            _head_paths = {p for _, p in manifest_member_probes(rel_path, head_text)}
            _added = [p for _, p in manifest_member_probes(rel_path, worker_text)
                      if p not in _head_paths]
            if not _added:
                return local_text, 0
            removed_n = 0
            new_text = local_text
            for tok in sorted(_added):
                new_text, _rm = prune_manifest_members(
                    rel_path, new_text, lambda p, _t=tok: False if p == _t else None)
                removed_n += len(_rm)
            if removed_n:
                logger.info(
                    "[workspace-manifest] H2 .sln 摘除 FAIL 子任务新增工程 %s"
                    "（残留幽灵工程条目会让 msbuild 硬错）: %s", sorted(_added), rel_path)
            return new_text, removed_n
        if _name.lower() != "pom.xml":
            # ★缺席可辨（R2 hunter）★ 非 pom/npm/.sln 的共享清单 H2 剥离未实现——
            # 至少留 WARNING（settings.gradle/Cargo.toml/go.work 的残留形态登记）。
            # ★#29-5 W-2 R1 文案纠假（hunter F1③：假兜底比没兜底更糟）★：
            # 「交 reconcile/prune 对账自愈」只对【成员类】条目成立（pom modules/
            # npm workspaces/go.work use/.sln Project 有 prune 臂）；【依赖类】条目
            # （go.mod require/build.gradle implementation/Cargo.toml [dependencies]）
            # 没有任何摘除臂 ⇒ 残留是【永久】的，绝无自愈——登记债。
            logger.warning(
                "[workspace-manifest] H2 回滚：%s 的 worker 贡献剥离未实现（仅 "
                "pom/package.json/.sln 有臂）→ 依赖类条目（require/implementation/"
                "[dependencies]）残留是永久的（无摘除臂，登记债）；仅成员类条目可交 "
                "reconcile/prune 对账自愈",
                rel_path)
            return local_text, 0
        head_deps = {(ga, r) for ga, _, r in _pom_dep_blocks(head_text)}
        added = [(ga, r) for ga, _, r in _pom_dep_blocks(worker_text)
                 if (ga, r) not in head_deps]
        head_span = _pom_modules_span(head_text)
        head_mods = set(re.findall(
            r"<module>\s*([^<\s]+)\s*</module>",
            head_text[head_span[0]:head_span[1]]) if head_span else [])
        w_span = _pom_modules_span(worker_text)
        added_mods = [m for m in (re.findall(
            r"<module>\s*([^<\s]+)\s*</module>",
            worker_text[w_span[0]:w_span[1]]) if w_span else [])
            if m not in head_mods]
        if not added and not added_mods:
            return local_text, 0
        new_text, removed = local_text, 0
        add_set = set(added)
        # 逐块扫 local，删除命中 (ga,region) 的块（span 递减序删，避免位移失效）
        hits = [(m.span(), (ga, r)) for m, ga, r in _iter_dep_blocks_with_span(new_text)
                if (ga, r) in add_set]
        removed_external: list[str] = []
        # C9（19号文）：内部/外部依赖分账——内部模块依赖（g=工程自身 groupId 或 a=模块名）
        # 被摘后 reconcile add 会确定性补回；外部依赖无任何补回路径，若并行兄弟加了同一
        # (g,a,region)，此处连带把兄弟那份也摘了 → 本地共享树蒸发 → 下游 bootstrap 缺包
        # BLOCKED 级联（R48c 同型）。三方文本无法区分"本 worker 贡献"与"兄弟同加"（诚实
        # 边界），至少碰撞面（外部依赖被摘）打 WARNING 可观测。
        # reviewer F-3：工程 groupId 提取——子模块 pom 自身不声明 groupId（parent 继承），
        # 剥掉 parent 后首个 <groupId> 会落在某个 dependency 里（随机外部 group，双向误分类）。
        # 正确口径：先取 project 直属（<dependencies> 等段之前的 region），取不到回退
        # parent 块内的 groupId（子模块的有效工程 group）。
        _head_wo_parent = re.sub(r"<parent>.*?</parent>", "", head_text, flags=re.S)
        _head_region = re.split(
            r"<(?:dependencies|dependencyManagement|build|properties|profiles)\b",
            _head_wo_parent, maxsplit=1)[0]
        _pg = re.search(r"<groupId>\s*([^<\s]+)\s*</groupId>", _head_region)
        if not _pg:
            _pm = re.search(r"<parent>(.*?)</parent>", head_text, re.S)
            _pg = (re.search(r"<groupId>\s*([^<\s]+)\s*</groupId>", _pm.group(1))
                   if _pm else None)
        head_project_group = _pg.group(1) if _pg else None
        for (s0, e0), _key in sorted(hits, key=lambda x: -x[0][0]):
            # 连同尾随换行/前导缩进一起删
            line_start = new_text.rfind("\n", 0, s0) + 1
            if new_text[line_start:s0].strip() == "":
                s0 = line_start
            if e0 < len(new_text) and new_text[e0:e0 + 1] == "\n":
                e0 += 1
            new_text = new_text[:s0] + new_text[e0:]
            removed += 1
            _g, _a = _key[0]
            if _g != head_project_group and _a not in head_mods:
                removed_external.append(f"{_g}:{_a}({_key[1]})")
        if removed_external:
            logger.warning(
                "[workspace-manifest] C9 H2 摘除命中外部依赖 %s（reconcile add 只补内部模块，"
                "外部依赖无确定性补回路径；若并行兄弟加了同坐标依赖已被连带摘除，"
                "下游缺包需重跑兄弟子任务自愈）: %s",
                rel_path, ", ".join(sorted(removed_external)))
        for m in added_mods:
            span = _pom_modules_span(new_text)
            if not span:
                break
            pat = re.compile(
                r"[ \t]*<module>\s*" + re.escape(m) + r"\s*</module>[ \t]*\r?\n?")
            new_text2, n = _sub_in_span(new_text, span, pat)
            if n:
                new_text = new_text2
                removed += n
        return new_text, removed
    except Exception as exc:  # noqa: BLE001 — fail-open 原样返回
        logger.warning("[workspace-manifest] H2 摘除异常 fail-open: %s", exc)
        return local_text, 0


def _iter_dep_blocks_with_span(text: str):
    """(match, (g,a), region) 迭代器——与 _pom_dep_blocks 同口径但带 span。"""
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
        yield m, (g.group(1), a.group(1)), region
