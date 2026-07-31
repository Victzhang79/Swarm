"""项目环境规格推断 — 从项目构建文件推断"完整拉起项目所需的沙箱环境"。

设计依据：docs/Project_Scoped_Sandbox_Design.md §4.5。
核心原则：按【构建描述文件】判断工具链，不靠文件扩展名
（ruoyi-e2e 有 90 个 .js 但都是静态资源，不需 node 工具链）。

用途：
- 预处理 ANALYZING 阶段：扫描已有项目 → EnvSpec → 批2 生成项目专属沙箱镜像。
- 全新空项目：无构建文件 → base_only=True，等首个任务需求分析再补装。

纯逻辑、无 IO 副作用（除读项目文件），便于单测。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 构建文件 → 工具链标识
_MAVEN_POM = "pom.xml"
_GRADLE = ("build.gradle", "build.gradle.kts")
_NPM = "package.json"
_PY_REQ = ("requirements.txt", "pyproject.toml", "setup.py", "Pipfile")
_GO_MOD = "go.mod"
_CARGO = "Cargo.toml"
_DOCKER = ("Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yaml")

# 扫描时跳过的目录（与 preprocess EXCLUDED_DIRS 对齐核心项）
_SKIP_DIRS = {
    "node_modules", "target", "build", "dist", ".git", ".idea", ".vscode",
    "__pycache__", ".venv", "venv", "vendor", ".gradle", ".mvn",
}


@dataclass
class Toolchain:
    """单个工具链需求。"""
    name: str                      # java / node / python / go / rust
    version: str | None = None     # 探测到的版本（如 java 17），None=用默认
    build_tool: str | None = None  # maven / gradle / npm / pip / go / cargo
    dep_source: str | None = None  # 相对项目根的依赖清单路径（pom.xml / package.json...）
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class EnvSpec:
    """项目环境规格 —— 批2 据此生成 Dockerfile + warmup。"""
    project_id: str = ""
    base_only: bool = False                       # True=无构建文件，仅基础镜像
    toolchains: list[Toolchain] = field(default_factory=list)
    project_dockerfile: str | None = None         # 项目自带 Dockerfile 相对路径（最优先）
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "base_only": self.base_only,
            "project_dockerfile": self.project_dockerfile,
            "toolchains": [
                {"name": t.name, "version": t.version, "build_tool": t.build_tool,
                 "dep_source": t.dep_source, "extra": t.extra}
                for t in self.toolchains
            ],
            "notes": self.notes,
        }

    def deps_hash(self) -> str:
        """规格指纹 —— 依赖/工具链变了才重建镜像（批2 缓存判断）。

        ★`notes` 不进指纹★ 它是**诊断文本**（"因为某清单读不出才保守装 node"这类），改一个字
        就会让指纹变化 → 触发一次多分钟的镜像重建，而镜像内容一模一样。本批给 `_infer_npm`
        加了截断/解析失败/子包命中三类 note 之后这条尤其要紧：诊断信息越详细，误触发越频繁。
        `project_id` 同理排除（同一工程不同 id 的镜像内容相同，且它已在 tag/路径里）。
        """
        import hashlib
        import json
        payload = json.dumps(
            {k: v for k, v in self.to_dict().items() if k not in ("notes", "project_id")},
            sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ──────────────────────────────────────────────
# 构建文件发现
# ──────────────────────────────────────────────
_FIND_MAX_DEPTH = 3          # 构建文件发现的深度上限（`find_build_files` 默认值，单一事实源）


def _npm_depth_ceiling_hint(root: Path, found_pkgs: list[str]) -> list[str]:
    """复核 M-1：工程里是否存在**超出深度上限**、因此没被发现的 package.json。

    只在"一个带脚本的清单都没找到"时调用，用来把"真静态资源"与"漏发现"两种成因分开——
    后者的正确文案是"可能漏发现"，指向深度上限，而不是把人指去查 st-10。
    """
    known = set(found_pkgs or [])
    out: list[str] = []
    try:
        # ★复核 HIGH-4★ `rglob` 顺序＝OS scandir 顺序 ⇒ 不排序的话同一棵树在不同机器上
        # 给出不同的 hint 列表（它进 note 文本，人读时会以为工程变了）。排序即确定。
        for p in sorted(root.rglob("package.json")):
            rel_parts = p.relative_to(root).parts
            if any(seg in _SKIP_DIRS for seg in rel_parts):
                continue
            if len(rel_parts) <= _FIND_MAX_DEPTH:
                continue
            rel = "/".join(rel_parts)
            if rel not in known:
                out.append(rel)
                if len(out) >= 5:
                    break
    except OSError:
        return []
    return out


def find_build_files(project_path: str | Path,
                     max_depth: int = _FIND_MAX_DEPTH) -> dict[str, list[str]]:
    """扫描项目，按类型归集构建文件（相对路径）。限制深度避免扫到依赖目录深处。"""
    root = Path(project_path)
    found: dict[str, list[str]] = {}

    def _add(kind: str, rel: str) -> None:
        found.setdefault(kind, []).append(rel)

    for path in root.rglob("*"):
        # 深度限制
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if len(rel_parts) > max_depth:
            continue
        if any(p in _SKIP_DIRS for p in rel_parts):
            continue
        if not path.is_file():
            continue
        name = path.name
        rel = str(path.relative_to(root))
        if name == _MAVEN_POM:
            _add("maven", rel)
        elif name in _GRADLE:
            _add("gradle", rel)
        elif name == _NPM:
            _add("npm", rel)
        elif name in _PY_REQ:
            _add("python", rel)
        elif name == _GO_MOD:
            _add("go", rel)
        elif name == _CARGO:
            _add("rust", rel)
        elif name in _DOCKER:
            _add("docker", rel)
    return found


# ──────────────────────────────────────────────
# 各工具链推断
# ──────────────────────────────────────────────
def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _infer_maven(root: Path, poms: list[str]) -> Toolchain:
    """聚合多模块 Maven，探 JDK 版本。dep_source 指向根 pom（批2 据此聚合外部依赖、排内部模块）。"""
    # 根 pom = 路径最短的那个
    root_pom = min(poms, key=lambda p: (p.count("/") + p.count("\\"), len(p)))
    java_version: str | None = None
    try:
        tree = ET.parse(root / root_pom)
        props = None
        for child in tree.getroot():
            if _strip_ns(child.tag) == "properties":
                props = child
                break
        if props is not None:
            for p in props:
                tag = _strip_ns(p.tag)
                if tag in ("java.version", "maven.compiler.source", "maven.compiler.target"):
                    if p.text and p.text.strip().isdigit():
                        java_version = p.text.strip()
                        break
    except Exception:  # noqa: BLE001 — 解析失败用默认版本
        pass
    return Toolchain(
        name="java", version=java_version, build_tool="maven",
        dep_source=root_pom,
        extra={"module_poms": poms, "module_count": len(poms)},
    )


_NPM_BUILD_SCRIPTS = ("build", "test", "start")
_NPM_SCAN_CAP = 200          # 巨型 monorepo 防爆：只读最浅的 N 个清单（够判"要不要装 node"）


def _note(notes: list[str] | None, msg: str) -> None:
    """把降级/截断原因写进 `EnvSpec.notes`（本模块无 logger，notes 是唯一可观测通道）。"""
    if notes is not None and msg not in notes:
        notes.append(msg)


def _infer_npm(root: Path, pkgs: list[str], notes: list[str] | None = None
               ) -> Toolchain | None:
    """**已发现的**任一 package.json 含 build/test/start script 即装 node；纯静态资源不装。

    ★注意覆盖面边界（复核 M-1）★ "已发现的"＝`find_build_files` 给的集合，而它有**深度上限**
    （`_FIND_MAX_DEPTH`=3）。`packages/@scope/web/package.json` 这类 4 段路径根本进不来 ⇒ 本函数
    看不到它的 scripts ⇒ 仍会返 None。调用方据 `_npm_depth_ceiling_hint` 把这种情形与"真静态
    资源"分开落 note（两者的排查方向完全不同）。放宽深度上限属独立改动（会拖慢全仓扫描）。

    ★N-1（27 号文 §7.5 R-1）★ 原实现**只读根** `package.json` 的 scripts。而 npm workspaces
    的根常常只有 `{name, private, workspaces}`、构建脚本全在各子包（turbo/nx 之外的常态形态）
    ⇒ `has_build=False` ⇒ 返 None ⇒ **镜像里根本不装 node** ⇒ 任何 npm 命令 127 →
    BLOCKED 无限退避（每轮重试撞同一个"命令不存在"）。与 X-C2（Gradle 工程镜像没装 gradle）
    同型换栈。

    ★刻意保留的既有行为★："无任何构建脚本 → 不装 node"是 st-10 的治法（Maven 单体里的
    Thymeleaf/admin 静态资源带个 package.json，装 node 纯浪费且会误派 npm 构建）。扫全部清单
    只是把判据从"根有没有"放宽到"**有没有**"，静态资源那条结论不变。

    `dep_source` 决定 npm warmup 在哪个目录跑 `npm ci`：
      · 根声明了 `workspaces`（依赖提升到根）或根自己有脚本 → 根清单；
      · 否则 → **最浅的那个有脚本的清单**（单前端在子目录的常见形态，如 `ui/package.json`）。
    """
    import json

    def _read(rel: str) -> dict | None:
        try:
            data = json.loads((root / rel).read_text(encoding="utf-8", errors="ignore"))
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001 — 单个清单读不出不代表整体结论
            return None

    # ★复核 M-4★ 排序键必须含路径本身：仅按 (深度, 长度) 排时，等长清单的先后由
    # `Path.rglob` 顺序（＝OS scandir 顺序）决定 ⇒ 同一个仓库在不同机器上得出不同的
    # `dep_source` ⇒ 它进 `deps_hash()` → `compute_project_fingerprint` ⇒ 无谓的镜像重建，
    # 且双前端仓库每次在不同前端跑 `npm ci`。加 `p` 作末位键即确定。
    ordered = sorted(pkgs, key=lambda p: (p.count("/") + p.count("\\"), len(p), p))
    root_pkg = ordered[0]
    scanned = ordered[:_NPM_SCAN_CAP]

    node_version: str | None = None
    with_scripts: list[str] = []
    unreadable_paths: list[str] = []
    root_declares_workspaces = False
    for rel in scanned:
        data = _read(rel)
        if data is None:
            unreadable_paths.append(rel)
            continue
        scripts = data.get("scripts") or {}
        if isinstance(scripts, dict) and any(scripts.get(k) for k in _NPM_BUILD_SCRIPTS):
            with_scripts.append(rel)
        if rel == root_pkg and data.get("workspaces"):
            root_declares_workspaces = True
        if node_version is None:
            engines = data.get("engines") or {}
            if isinstance(engines, dict) and engines.get("node"):
                m = re.search(r"(\d+)", str(engines["node"]))
                node_version = m.group(1) if m else None

    if len(ordered) > _NPM_SCAN_CAP:
        # ★复核 M-2★ 截断必须可观测：被截掉的清单里若恰好只有它有 scripts，结论就从
        # "装 node"翻成"不装 node"——而"不装 node"正是本批要治的 127 死循环。
        _note(notes, f"package.json 数量 {len(ordered)} 超过扫描上限 {_NPM_SCAN_CAP}，"
                     f"仅据最浅的 {_NPM_SCAN_CAP} 个判定 node 工具链（可能漏判）")
    if not with_scripts:
        if unreadable_paths:
            # ★复核 M-3★ 解析失败保守装 node（维持原行为）——但必须留痕：否则"因为某个清单
            # 读不出才装的 node"与"真 node 工程"不可分。且语义比原来宽（原先只有**根**读不出
            # 才装，现在任一读不出都装）⇒ Maven 单体里一个损坏的 vendored package.json 就会
            # 把 st-10 的病招回来，故这条账必须能被人看见。
            _note(notes, f"package.json 解析失败 {len(unreadable_paths)} 个"
                         f"（{unreadable_paths[:2]}）→ 保守装 node（可能是多余的）")
            return Toolchain(name="node", version=node_version, build_tool="npm",
                             dep_source=root_pkg)
        if unreadable_paths:
            _note(notes, f"package.json 解析失败 {len(unreadable_paths)} 个"
                         f"（{unreadable_paths[:2]}）")
        return None  # 纯静态资源，无需 node 工具链（st-10 治法，刻意保留）

    if unreadable_paths:
        # ★复核 MED-1★ 原先只在"一个带脚本的都没找到"那条分支留痕；`with_scripts` 非空时
        # 读不出的清单被静默丢弃 ⇒ 那个子包的 scripts/engines 没参与判定（可能漏了 node 版本、
        # 也可能漏了唯一的 build），而外部看不出发生过这件事。
        _note(notes, f"package.json 解析失败 {len(unreadable_paths)} 个"
                     f"（{unreadable_paths[:2]}）→ 其 scripts/engines 未参与判定")
    if root_declares_workspaces or root_pkg in with_scripts:
        dep = root_pkg          # workspaces 的依赖提升到根，warmup 必须在根跑
    else:
        dep = with_scripts[0]   # 已按深度排序 ⇒ 最浅的那个有脚本的清单
    # ★复核 MED-2★ 装 node 的**正常**路径原先零留痕 ⇒ 运维分不清"根有 build"与"只有子包有
    # build"（后者正是 N-1 的形态，也是本批唯一改动的判据面）。无条件落一条决定依据。
    if root_pkg not in with_scripts:
        _note(notes, f"node 工具链据**子包**脚本判定（根 package.json 无 build/test/start）："
                     f"命中 {with_scripts[:3]}，warmup 目录={dep}")
    return Toolchain(name="node", version=node_version, build_tool="npm", dep_source=dep)


def _infer_simple(name: str, build_tool: str, root: Path, files: list[str]) -> Toolchain:
    src = min(files, key=lambda p: (p.count("/") + p.count("\\"), len(p)))
    return Toolchain(name=name, build_tool=build_tool, dep_source=src)


# ──────────────────────────────────────────────
# 顶层推断
# ──────────────────────────────────────────────
def infer_env_spec(project_path: str | Path, project_id: str = "") -> EnvSpec:
    """项目路径 → EnvSpec。混编取工具链并集；全新空项目 base_only。"""
    root = Path(project_path)
    bf = find_build_files(root)
    spec = EnvSpec(project_id=project_id)

    # 项目自带 Dockerfile 最准 —— 标注但仍推断工具链（供 warmup 参考）
    if "docker" in bf:
        dockerfiles = [f for f in bf["docker"] if Path(f).name == "Dockerfile"]
        if dockerfiles:
            spec.project_dockerfile = min(dockerfiles, key=len)
            spec.notes.append(f"项目自带 Dockerfile: {spec.project_dockerfile}（可优先复用）")

    if "maven" in bf:
        spec.toolchains.append(_infer_maven(root, bf["maven"]))
    if "gradle" in bf:
        spec.toolchains.append(_infer_simple("java", "gradle", root, bf["gradle"]))
    if "npm" in bf:
        tc = _infer_npm(root, bf["npm"], notes=spec.notes)
        if tc:
            spec.toolchains.append(tc)
        else:
            # ★复核 M-1★ 原文案只说"视为静态资源"，把未来的排查者指向 st-10，而真凶可能是
            # `find_build_files` 的**深度上限**（默认 3）——`packages/@scope/web/package.json`
            # 这类 4 段路径根本没被发现过。两种成因必须在文案里可分。
            _deep = _npm_depth_ceiling_hint(root, bf["npm"])
            if _deep:
                spec.notes.append(
                    f"未发现任何带 build/test/start 的 package.json，但工程内存在更深层的 "
                    f"package.json（{_deep[:2]}，超出 find_build_files 深度上限 "
                    f"{_FIND_MAX_DEPTH}）→ 可能是**漏发现**而非静态资源；不装 node 会让 npm "
                    f"命令 127")
            else:
                spec.notes.append(
                    "package.json 无 build/test/start 脚本 → 视为静态资源，不装 node")
    if "python" in bf:
        spec.toolchains.append(_infer_simple("python", "pip", root, bf["python"]))
    if "go" in bf:
        spec.toolchains.append(_infer_simple("go", "go", root, bf["go"]))
    if "rust" in bf:
        spec.toolchains.append(_infer_simple("rust", "cargo", root, bf["rust"]))

    if not spec.toolchains:
        spec.base_only = True
        spec.notes.append("无构建文件 → 基础镜像；全新项目等首个任务需求分析再补装工具链")

    return spec
