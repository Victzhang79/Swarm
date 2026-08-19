"""brain/nodes/recovery.py — 恢复/阻断分析纯函数簇（round24 A7 从 nodes/__init__ 首拆）。

内聚簇 A：worker 失败后的【确定性、零 LLM】恢复决策所依赖的纯路径/依赖图分析。
自包含（仅 stdlib + WorkerOutput + planning_core 的归一函数），不反向 import nodes/__init__（守 A6 破的环）。
可 patch 符号仍经 nodes/__init__ re-export 保 `swarm.brain.nodes.X` 可寻址；但簇内互调
（_blocked_pkg_unrecoverable → _package_in_baseline / _is_missing_dependency_failure →
_det_of）走本模块 global，故测试要 patch 本模块（swarm.brain.nodes.recovery.X）。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess

from swarm.types import WorkerOutput
# 判据 C 清扫：路径归一比较形单一事实源。无环：planning_core 模块级只依赖
# brain.state / nodes.shared / types，不回指本模块（A6 环规不破）。
from swarm.brain.nodes.planning_core import _norm_rel_cmp

logger = logging.getLogger(__name__)

_MISSING_DEP_PATTERNS = (
    "cannot find symbol",      # javac (en)
    "找不到符号",               # javac (zh)
    "程序包",                   # javac (zh): "程序包 xxx 不存在"
    "package does not exist",  # javac (en): "package xxx does not exist"
    "cannot find package",     # go
    "unresolved import",       # rust / python 工具链
    "no module named",         # python ImportError
    "module not found",        # node
)


# A2/A3 治本(round11)：这些 pipeline_blocked 是【项目内/上游子任务产物尚未就绪】(非缺外部
# jar)——L1 已标 BLOCKED 待生产者落地由 transient 重试自然消解。但其 build_output 含 "cannot
# find symbol"/"程序包…不存在"，会被 _MISSING_DEP_PATTERNS 误命中 → 错进 A2 定向恢复(补无关
# maven 坐标 + 重置重试计数致多轮空转, round11 ~16/33 沙箱白耗)。A2 只该治【真·缺外部 jar】，
# 故这两类一律排除。根因(缺兄弟域产物注入)由 A1 在 plan 层 readable 修复。
# round29 A 补第三类：module_registered_before_scaffold（清单注册的模块目录尚不存在=依赖序
# 结构问题），同理由排除出 A2（补外部 jar 治不了；由 failure.py 序修复阶梯定点重排处理）。
_INTERNAL_BLOCKED_KINDS = ("internal_pkg_not_built", "upstream_module_broken",
                           "module_registered_before_scaffold")


def _det_of(out) -> dict:
    """统一取 worker 失败结果的 l1_details（§3.2：委托 shared.l1_details_of 单一实现，本地名保 seam）。"""
    from swarm.brain.nodes.shared import l1_details_of
    return l1_details_of(out)


def _producers_of(plan_obj, packages, modules, paths=None) -> set[str]:
    """反查【生产某内部包/某模块的子任务 id】：按 plan 子任务 scope.writable 文件路径归属匹配。

    治本 replan 死循环关键：下游因引用上游模块/包而 BLOCKED 时，跨模块 import 依赖的 depends_on
    在 plan 期常拿不到（见 l1_pipeline 自注），无法靠 depends_on 反查上游。改用运行时 worker 吐出的
    blocked_on_packages/modules，按【谁的 scope.writable 落在该模块目录 / 含该包目录段】归属到生产者
    子任务。通用跨栈、非项目写死（纯路径归属，不含任何硬编码 FQN/模块名）。

    ★X-C3-A（治法 B）★ `paths` = worker 侧 `blocked_on_paths`（**已是路径口径**的词干集）。
    下面那句 `"/".join(p.split("."))` 是 **Java 点分 FQN 专属**转换：非 JVM 的 ref 按它转出来
    无一命中（`github.com/a/s/internal/svc`→`github/com/…`、`./routes/users`、`crate::svc`
    原样、`app.services.user`→只当目录段比而 `user` 其实是**文件**）⇒ 反查不到生产者 ⇒
    `_futile=True` → 推不出该建啥 → `_unrecoverable` ⇒ **首轮连坐放弃**（比 X-C3 之前更坏）。
    给了 `paths` 就用它，**缺席逐字节走老路**（java 恒缺席 ⇒ 唯一跑过 E2E 的栈零改动）。"""
    out: set[str] = set()
    # ★复核 HIGH-2★ `""` 是 Go 根包的合法词干，`if str(p).strip()` 会把它滤掉 ⇒ 根包整类
    # 回落点分口径。用 `is not None` 判在场，只剥 `/`（保留空串这个哨兵）。
    _use_paths = ([str(p).strip("/") for p in paths] if paths is not None else [])
    _root_stem = "" in _use_paths
    _use_paths = [p for p in _use_paths if p]
    _has_paths = bool(_use_paths) or _root_stem
    pkg_paths = (_use_paths if _has_paths
                 else ["/".join(p.split(".")) for p in (packages or []) if p])
    mods = {str(m).strip().strip("/") for m in (modules or []) if str(m).strip()}

    def _hit(fn: str, pp: str) -> bool:
        # 目录段命中（两种口径共用）：`internal/svc` ⊂ `internal/svc/user.go`
        if ("/" + pp + "/") in ("/" + fn):
            return True
        # ★路径口径专属★：词干也可以是**文件**（python `app/services/user` →
        # `app/services/user.py`、TS `src/routes/users` → `src/routes/users.ts`）。
        # 老的点分口径下词干恒是包目录，故此分支只对 paths 开（不改 JVM 行为）。
        return _has_paths and fn.rsplit(".", 1)[0] == pp

    for s in getattr(plan_obj, "subtasks", []):
        scope = getattr(s, "scope", None)
        writ = list(getattr(scope, "writable", []) or []) if scope else []
        for f in writ:
            fn = _norm_rel_cmp(f)
            top = fn.split("/", 1)[0]
            if top in mods:
                out.add(s.id)
                break
            # 根词干（Go 根包）：只认**工程根直下**的源文件（不吃子目录，否则整棵树都算）
            if _root_stem and "/" not in fn:
                out.add(s.id)
                break
            if any(_hit(fn, pp) for pp in pkg_paths):
                out.add(s.id)
                break
    return out


# ── round29 A：模块「注册先于脚手架」依赖序症状（worker l1_pipeline 分类器发出）──
_MODULE_ORDER_BLOCKED_KIND = "module_registered_before_scaffold"

# 工作区级注册清单（模块注册落在这些文件里）。跨栈通用、非项目写死。
_ROOT_MANIFESTS = ("pom.xml", "settings.gradle", "settings.gradle.kts", "Cargo.toml", "go.work")

# 模块自身的清单文件名（脚手架子任务 = 创建 <module>/<manifest> 者）。
_MODULE_MANIFESTS = ("pom.xml", "build.gradle", "build.gradle.kts", "Cargo.toml", "go.mod",
                     "package.json")
_MODULE_MANIFESTS_LOWER = tuple(m.lower() for m in _MODULE_MANIFESTS)


def _module_order_violation_modules(subtask_results: dict, failed_ids: list) -> set[str]:
    """失败集里被 worker 标为「注册先于脚手架」的缺失模块目录并集（空集=非此症状）。"""
    mods: set[str] = set()
    for fid in failed_ids or []:
        det = _det_of(subtask_results.get(fid))
        if det.get("pipeline_blocked") == _MODULE_ORDER_BLOCKED_KIND:
            mods.update(
                str(m).replace("\\", "/").strip().strip("/")
                for m in (det.get("blocked_on_modules") or []) if str(m).strip()
            )
    return {m for m in mods if m}


def _scaffold_subtask_of_module(plan_obj, module: str):
    """定位模块 <module> 的脚手架子任务（create_files 含 <module>/<清单>），无则 None。

    归一化鲁棒（猎人#2 整改）：大小写不敏感 + 目录【后缀】互相匹配——worker 报的模块目录相对
    构建 cwd（如 "crates/util"），plan 里可能带更深前缀（"backend/crates/util"），反之亦然。
    """
    mod = module.rstrip("/").lower()
    if not mod:
        return None
    for s in getattr(plan_obj, "subtasks", []) or []:
        scope = getattr(s, "scope", None)
        creates = list(getattr(scope, "create_files", []) or []) if scope else []
        for cf in creates:
            fn = _norm_rel_cmp(cf).lower()
            if "/" not in fn:
                continue
            d, base = fn.rsplit("/", 1)
            if base not in _MODULE_MANIFESTS_LOWER:   # fn 已整体 lower，清单集需同口径
                continue
            if d == mod or d.endswith("/" + mod) or mod.endswith("/" + d):
                return s
    return None


def _root_manifest_registrants(plan_obj) -> list:
    """定位【工作区根清单】写者（注册模块的子任务）：writable/create 含根清单文件。"""
    out = []
    for s in getattr(plan_obj, "subtasks", []) or []:
        scope = getattr(s, "scope", None)
        if scope is None:
            continue
        w = (set(getattr(scope, "writable", []) or [])
             | set(getattr(scope, "create_files", []) or []))
        if any(_norm_rel_cmp(f) in _ROOT_MANIFESTS for f in w):
            out.append(s)
    return out


# D56：项目树目录索引 memo——handle_failure 每轮每失败子任务每 blocked 包都调
# _package_in_baseline，旧实现每次 os.walk 整棵项目树（大仓 + 多失败 = 显著热点，且在
# async 节点调用链上）。改为一次 walk 建目录索引、按包名后缀匹配查询。
# 失效策略：短 TTL（apply/merge 会改项目树，宁可短 TTL 重扫也绝不永久缓存错判）；
# walk 抛 OSError 时【不缓存】，调用方照旧保守返回 True。
_BASELINE_INDEX_TTL_S = 30.0
# 阴性判定（包不在树 → 可能触发 abandon）容忍的最大索引年龄：同一 handle_failure 轮内的
# 突发查询共享一次 walk，跨轮/跨秒的阴性必须新扫确认——stale 缓存漏看刚 apply 落地的包
# 会把"该等"误判成"臆造"，方向性危险；阳性（存在 → 继续等）本就是保守方向，可吃 TTL 缓存。
_BASELINE_NEG_FRESH_S = 1.0
# project_path -> (built_monotonic, 全部目录的 posix 规范化绝对路径集合)
_BASELINE_DIR_INDEX: dict[str, tuple[float, frozenset[str]]] = {}


def _baseline_dir_roots(project_path: str, *, max_age_s: float) -> frozenset[str]:
    """walk 项目树收集全部目录路径（与旧 walk 同剪枝口径）；max_age_s 内 memo。OSError 上抛。"""
    import time
    now = time.monotonic()
    cached = _BASELINE_DIR_INDEX.get(project_path)
    if cached is not None and (now - cached[0]) < max_age_s:
        return cached[1]
    roots: set[str] = set()
    for root, dirs, _files in os.walk(project_path):
        # 剪枝构建产物/VCS/依赖目录，控制开销（与旧实现完全同口径）
        dirs[:] = [d for d in dirs
                   if d not in (".git", "target", "build", "dist", "out",
                                "node_modules", ".gradle", ".idea")]
        roots.add(root.replace(os.sep, "/"))
    frozen = frozenset(roots)
    _BASELINE_DIR_INDEX[project_path] = (now, frozen)
    return frozen


def _package_in_baseline(project_path: str | None, pkg: str,
                         path_stems=None) -> bool:
    """点分包名 pkg 是否已存在于【基线项目树】任一模块 src 下（确定性、零 LLM）。

    ★X-C3-A（治法 B）★ `path_stems` = 该 ref 的**路径口径**词干（worker 侧
    `blocked_on_paths_by_ref[ref]`）。下面 `pkg.replace(".", "/")` 是 Java 点分专属：
    go 的 `github.com/a/s/internal/svc` 会被转成 `github/com/…` ⇒ 目录**确实存在**也判 False
    ⇒ 假阳性护栏失效 ⇒ 判"臆造/永不可满足" ⇒ 首轮连坐放弃。给了词干就用词干，
    **缺席逐字节走老路**（java 恒缺席）。词干可能指向**文件**（python/TS），故除目录集外
    再查一次文件存在性。

    #R13-2 治本关键：worker 臆造一个基线里根本不存在的包(如 com.ruoyi.common.core.redis)时，
    L1 会误判 internal_pkg_not_built(transient，等一个【永不会来的生产者】)，白烧整条重试阶梯。
    但"BLOCKED on X 且 plan 无生产者"不足以判臆造——X 可能是【基线已有、只是沙箱漏同步】的包，
    那种应继续 transient 等待、绝不硬失败。故用本函数做【假阳性护栏】：只有 X 既无 plan 生产者、
    【又不在基线树里】才判为臆造(永不可满足)。纯路径匹配、通用跨栈、非项目写死。
    扫描失败/无路径 → 保守返回 True(当作【存在】→ 不硬失败)，宁可多等也不误杀。
    D56：目录集合经 _baseline_dir_roots 一次 walk + 短 TTL memo，判定谓词与旧逐次 walk
    完全等价（同剪枝、同 endswith 后缀匹配）。"""
    if not project_path or not pkg:
        return True  # 无从判定 → 保守当【存在】，不据此硬失败
    # 复核 HIGH-2：`""`（Go 根包）是合法词干，不能被 `if str(s).strip()` 滤掉
    _stems = ([str(s).strip("/") for s in path_stems] if path_stems is not None else [])
    if _stems:
        # 路径口径：词干可指向目录**或**文件（`app/services/user` → `user.py`）。
        # 任一命中即"在树里"（保守方向＝继续等，与老路一致）。
        import os as _os
        for _s in _stems:
            if _s == "":
                # Go 根包：工程根直下有任何源文件即"在树里"（保守方向＝继续等）
                try:
                    return any(_os.path.isfile(_os.path.join(project_path, f))
                               for f in _os.listdir(project_path))
                except OSError:
                    return True
            _abs = _os.path.join(project_path, _s)
            if _os.path.isdir(_abs):
                return True
            _d, _b = _os.path.split(_abs)
            try:
                if _b and _os.path.isdir(_d) and any(
                        f == _b or f.rsplit(".", 1)[0] == _b
                        for f in _os.listdir(_d)):
                    return True
            except OSError:
                return True   # 扫不动 → 保守当【存在】，绝不据此硬失败
        return False
    rel = pkg.replace(".", "/").strip("/")
    if not rel:
        return True
    suffix = "/" + rel
    try:
        roots = _baseline_dir_roots(project_path, max_age_s=_BASELINE_INDEX_TTL_S)
        if any(r.endswith(suffix) for r in roots):
            return True  # 阳性=继续等，保守方向，允许吃 TTL 缓存
        # 阴性可能触发 abandon → 必须以【新鲜】索引确认（≤1s 视为同轮突发共享）
        roots = _baseline_dir_roots(project_path, max_age_s=_BASELINE_NEG_FRESH_S)
    except OSError:
        return True  # 扫描异常 → 保守当【存在】，避免误杀（不缓存，下次重试）
    return any(r.endswith(suffix) for r in roots)


def _module_in_git_baseline(project_path: str | None, module: str) -> bool:
    """模块目录是否存在于 git 基线(HEAD)树——判「基线模块」的结构性判据（T3/round63）。

    基线模块=项目基线自带、非本计划任何子任务生产的模块。它的构建破坏没有 plan 内 owner，
    transient 重试是无望等待（round63 实锤：LLM 自己诊断"预置模块、不在任何子任务范围内"
    却仍 retry 三周期）。工作树存在性判不了这个——脚手架新建的模块也在工作树；HEAD 才是
    "谁属于基线"的唯一权威。git 不可用/非仓库/异常 → False（fail-open：不触发 T3 拦截，
    回落既有行为）。栈中立（纯目录存在性，不含任何清单格式假设）。
    """
    if not project_path or not module:
        return False
    rel = str(module).replace("\\", "/").strip().strip("/")
    if not rel or rel in (".", "..") or rel.startswith("../"):
        return False
    try:
        r = subprocess.run(
            ["git", "-C", str(project_path), "cat-file", "-e", f"HEAD:{rel}"],
            capture_output=True, timeout=15,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        # hunter#2：异常≠"非基线"。静默 False 会让 T3 臂对真基线破坏整体解除武装、无痕退回
        # round63 无望 transient 循环。留 WARNING 痕（非仓库/模块真不在 HEAD 走 returncode≠0，
        # 不进此支、不刷屏）；方向仍 fail-open False——绝不因 git 抖动误判死锁。
        logger.warning(
            "[T3] 基线模块判定 git 异常（%s: %s）→ fail-open 视为非基线模块"
            "（本轮 T3 死锁臂对该模块失效，回落既有 transient 行为）", rel, e,
        )
        return False


# T3 修复臂扫描的目录剪枝（与 _baseline_dir_roots 同口径：构建产物/VCS/依赖目录不进）。
_SWEEP_PRUNE_DIRS = (".git", "target", "build", "dist", "out",
                     "node_modules", ".gradle", ".idea")


def sweep_baseline_anchor_poison(
    project_path: str | None, plan_obj,
) -> tuple[list[dict], int]:
    """确定性基线锚修复扫描（T3 round63 死锁治本·brain 侧修复臂）。

    对项目树内【git 基线(HEAD)已存在的共享清单】逐个对照基线，还原「既有版本锚篡改」——
    复用 T2 纯函数 restore_baseline_version_anchors：只还原既有锚的突变，纯加法（新属性/
    新依赖/新模块注册）一律放行，结构上绝不冲掉并行兄弟的合法注册。豁免任何 plan 子任务
    writable/create_files 覆盖的清单（计划授权编辑面，T2 HIGH#1 同款豁免）。

    与 T1/T2 的分工：T1 禁 repair 产毒（源头）、T2 禁毒经 pull-back 进共享树（通道）、
    本函数治「毒已在共享树」（round63 遗留态/未覆盖通道）——三层防线的最后修复臂。

    返回 (restored, scan_errors)：restored=修复登记 [{"file", "anchor", "from", "to"}]；
    scan_errors=扫描期异常计数（git 失败/读盘失败/解码失败）。hunter#1：调用方必须区分
    「扫净（restored=[] 且 scan_errors=0）」与「扫瞎（scan_errors>0）」——后者不得据以
    判死锁放弃（scanner 坏 ≠ 树干净）。单文件异常跳过（fail-open），已还原项保留。
    写盘经 per-project flock 串行化（与 worker pull-back 同一把锁，防并发互踩）。
    """
    if not project_path or plan_obj is None:
        return [], 0
    from swarm.worker.git_flock import _ProjectGitFlock
    from swarm.worker.sandbox import _is_shared_manifest_on_disk
    from swarm.worker.workspace_manifest import restore_baseline_version_anchors

    root = str(project_path)
    scan_errors = 0

    # plan 授权编辑面（writable ∪ create_files，归一化 posix 相对路径）。
    # 复核 LOW#2：前缀剥离用显式判断，不用 lstrip("./") 字符集剥（会吃掉 .mvn 类段首点）。
    owned: set[str] = set()
    for s in getattr(plan_obj, "subtasks", []) or []:
        sc = getattr(s, "scope", None)
        if sc is None:
            continue
        for f in (list(getattr(sc, "writable", []) or [])
                  + list(getattr(sc, "create_files", []) or [])):
            p = str(f).replace("\\", "/")
            p = p[2:] if p.startswith("./") else p
            owned.add(p.strip("/"))

    # 候选：工作树内共享清单（剪枝口径与 _baseline_dir_roots 一致）
    candidates: list[str] = []
    try:
        for droot, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in _SWEEP_PRUNE_DIRS]
            for fn in files:
                rel = os.path.relpath(os.path.join(droot, fn), root).replace(os.sep, "/")
                # B7/#29-5 W-2：on_disk 版判定（单一事实源分类器）——npm workspaces 聚合根
                # 按内容纳入扫描；根 go.mod/根 package.json 自 W-2 起按「多写者写依赖」
                # 根档纳入。名实边界（reviewer R1 LOW-1）：锚还原驱动当前仅 pom.xml 消费，
                # npm/go 面暂无实际行为（预留同判据口径）。
                if _is_shared_manifest_on_disk(rel, root):
                    candidates.append(rel)
    except OSError as e:
        logger.warning("[T3] 基线锚修复扫描无法遍历项目树（%s）→ 本轮扫描盲", e)
        return [], 1

    # 基线读取（不可变已提交历史）在锁外批量完成，锁只护本地读-改-写
    work: list[tuple[str, str]] = []
    for rel in sorted(candidates):
        if rel in owned:
            continue  # 计划授权面：brain 无权对齐基线（可能是合法交付）
        try:
            r = subprocess.run(
                ["git", "-C", root, "show", f"HEAD:{rel}"],
                capture_output=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as e:
            scan_errors += 1
            logger.warning("[T3] 基线锚修复扫描读 %s 基线失败（%s）→ 该文件本轮盲", rel, e)
            continue
        if r.returncode != 0 or not r.stdout:
            continue  # 不在基线（计划新建清单）→ 加法产物，放行
        try:
            baseline = r.stdout.decode("utf-8")
        except UnicodeDecodeError:
            scan_errors += 1
            continue
        work.append((rel, baseline))

    restored: list[dict] = []
    with _ProjectGitFlock(root):
        for rel, baseline in work:
            fp = os.path.join(root, rel)
            try:
                with open(fp, encoding="utf-8") as fh:
                    cur = fh.read()
            except (OSError, UnicodeDecodeError) as e:
                scan_errors += 1
                logger.warning("[T3] 基线锚修复扫描读工作树 %s 失败（%s）→ 该文件本轮盲", rel, e)
                continue
            new_text, items = restore_baseline_version_anchors(cur, baseline, rel)
            if not items:
                continue
            try:
                with open(fp, "w", encoding="utf-8") as fh:
                    fh.write(new_text)
            except OSError as e:
                scan_errors += 1
                logger.warning(
                    "[T3] 基线锚修复扫描检出 %s 的锚篡改 %s 但【还原写盘失败·毒仍在树】: %s",
                    rel, items, e,
                )
                continue
            for it in items:
                restored.append({"file": rel, **it})
    return restored, scan_errors


def _class_in_baseline(project_path: str | None, class_fqn: str) -> bool:
    """H-3a 配套（复核 HIGH 整改）：类 FQN（pkg.Cls 点分）是否已在基线树——【类级】BLOCKED
    的 futile 假阳性护栏。类级 BLOCKED（internal_pkg_not_built 的"包在树、类未建出"子型）
    的包必然在树 → _package_in_baseline 恒 True → futile 永假 → 臆造类引用烧满整条重试阶梯
    （round19 #10 幽灵生产者慢磨复发）。判据=包目录下 {Cls}.java 存在 或 目录内任一 .java
    含该类声明（共居次级类，与 L1 侧内容级判据对齐）。无从判定/异常 → 保守 True（当存在→
    不硬失败），同 _package_in_baseline 纪律；阴性方向用新鲜索引。"""
    if not project_path or not class_fqn or "." not in class_fqn:
        return True
    pkg, cls = class_fqn.rsplit(".", 1)
    rel = pkg.replace(".", "/").strip("/")
    if not rel or not cls:
        return True
    suffix = "/" + rel
    import re as _re
    decl = _re.compile(r"\b(?:class|interface|enum|record)\s+" + _re.escape(cls) + r"\b")
    try:
        # 阴性判定可能触发 abandon → 用新鲜索引（同 _package_in_baseline 阴性纪律）
        roots = _baseline_dir_roots(project_path, max_age_s=_BASELINE_NEG_FRESH_S)
        for r in roots:
            if not r.endswith(suffix):
                continue
            for fn in os.listdir(r):
                if not fn.endswith(".java"):
                    continue
                if fn == cls + ".java":
                    return True
                try:
                    with open(os.path.join(r, fn), encoding="utf-8", errors="ignore") as fh:
                        if decl.search(fh.read()):
                            return True
                except OSError:
                    return True  # 单文件读失败 → 保守当【存在】
        return False
    except OSError:
        return True  # 扫描异常 → 保守当【存在】，避免误杀


def _blocked_pkg_unrecoverable(
    blocked_pkgs, producers, unsat, completed_ok, pending, project_path, self_id,
    blocked_classes=None, paths_by_ref=None,
) -> bool:
    """阻断在内部包的子任务，是否【永不可满足】= 全部生产者已终结 且 包仍不在工作树。

    #10 治本（round19 st-38 慢磨 ~1h 的缺口）：快失败原判据只认【完全无生产者】(_hallucinated)，
    但 `_producers_of` 按路径/模块松归属，会把一个【已完成、却产了别的包名(#9 漂移)】的子任务
    误算作生产者 → 判"有生产者、transient 可恢复" → 白磨完整升级阶梯。此处把"无生产者"泛化为
    【无 active 生产者】：生产者已 abandoned 或已成功完成(不再重派)即 settled；仍 pending/在飞/
    未跑 = active、继续等（保住合法跨模块等待，不打地鼠松紧 _producers_of）。

    active 生产者存在 → 返回 False（继续 transient 等待）。全部 settled 时，仅当【阻断包一个都
    不在工作树】才判不可恢复 True——包在树(仅漏 seed，#12 域)→ False，交 #12 重 seed，杜绝越权
    误 abandon。self_id 从生产者集剔除（阻断子任务自身不能自证 active）。纯路径、跨栈、非项目写死。"""
    _prods = {p for p in (producers or set()) if p and p != self_id}
    _pending = set(pending or set())
    _done = set(completed_ok or set())
    _unsat = set(unsat or set())

    def _settled(p: str) -> bool:
        if p in _unsat:                       # 已放弃 → 终结
            return True
        return p in _done and p not in _pending  # 已成功完成且不再重派 → 终结

    if any(not _settled(p) for p in _prods):  # 仍有 active 生产者 → 该等，别误杀
        return False
    # ★H-3a 复核 HIGH 整改★：类级 BLOCKED（L1 吐了 blocked_on_classes=包在树、类未建出）
    # 时包级判据结构性失效（包必在树→恒 False→臆造类烧满阶梯）——改用类级树判据：
    # 全部缺类都不在基线树才判不可满足；任一在树（仅漏 seed/同步）→ False 继续等。
    # X-C3-A：每个 ref 各带自己的路径词干（缺席 → 该 ref 走老的点分口径）
    _pbr = paths_by_ref or {}
    _cls = [c for c in (blocked_classes or []) if c]
    if _cls:
        # ★X-C3-A 复核 CRITICAL-1★ 这条类级臂原先**抢先 return**，把整个符号级通道挡在
        # 新口径之外：`_class_in_baseline` 是 JVM-only（`endswith(".java")` + `\b(class|
        # interface|enum|record)\s+`），非 JVM 符号恒返 False ⇒ futile=True ⇒ 而 `_prods`
        # 已被本批修成非空 ⇒ 走 `_unrecoverable` 而非 `_selfheal` ⇒ **首轮连坐放弃**。
        # 覆盖面不是边角：四个非 JVM driver **全部**产符号级 ref（Go `undefined:`、
        # TS TS2305、Rust `no X in Y`/E0425、Python `cannot import name`），且 Go 那条
        # driver 的注释自己写着该形态"比 Java 更常见（同包多文件是 Go 的常态组织方式）"。
        #
        # 治法与包级同构：符号的**容器**若能用路径口径判在树里 → 继续等（保守方向，与
        # `_class_in_baseline` 阳性同义）；容器判不出 → 落回 JVM 类级判据（java 原样）。
        # ★复核 MED-4★ 逐个类各自选口径，**绝不因"有些类没词干"就把它们整条剔除**：
        # 被剔的恰是可能投 False 票（"在树里→继续等"）的那些 ⇒ futile 从 False 翻成 True
        # ⇒ 该等的变成判永不可满足 → 连坐放弃（方向翻转，最贵那侧）。
        # 有词干 → 按路径口径判其容器；无词干 → 回落 JVM 类级判据（java 原样）。
        def _cls_in_tree(c: str) -> bool:
            _st = _pbr.get(c)
            if _st:
                return _package_in_baseline(project_path, c, path_stems=_st)
            return _class_in_baseline(project_path, c)

        return not any(_cls_in_tree(c) for c in _cls)
    return bool(blocked_pkgs) and not any(
        _package_in_baseline(project_path, p, path_stems=_pbr.get(p))
        for p in blocked_pkgs
    )


def _is_missing_dependency_failure(subtask_results: dict, failed_ids: list) -> bool:
    """失败详情里是否命中"缺符号/缺依赖"编译特征（确定性、零 LLM）。
    排除 internal_pkg_not_built/upstream_module_broken——那是【内部产物未就绪】非缺外部 jar，
    走 A2 补依赖必空烧(见 _INTERNAL_BLOCKED_KINDS 注释)。"""
    for fid in failed_ids:
        det = _det_of(subtask_results.get(fid))
        if isinstance(det, dict) and det.get("pipeline_blocked") in _INTERNAL_BLOCKED_KINDS:
            continue  # 内部/上游未就绪 → 不该触发 A2 maven 补依赖
        # #78 DR-02-F5 治本：对抗复核打回的失败 l1_details 带 adversarial_critique 键——复核 Java
        # 代码时评语自然含"找不到符号/程序包不存在"字面，会被 _MISSING_DEP_PATTERNS 误命中 → 错走
        # A2 补 pom/注无关 maven 坐标 + 空烧 targeted_recovery 配额，而非按评语修质量缺陷。带此键=
        # 质量打回非缺依赖，直接跳过（正常缺依赖失败无此键，不受影响）。
        if isinstance(det, dict) and det.get("adversarial_critique"):
            continue
        try:
            blob = json.dumps(det, ensure_ascii=False).lower()
        except (TypeError, ValueError):
            blob = str(det).lower()
        if any(p in blob for p in _MISSING_DEP_PATTERNS):
            return True
    return False
