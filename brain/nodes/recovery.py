"""brain/nodes/recovery.py — 恢复/阻断分析纯函数簇（round24 A7 从 nodes/__init__ 首拆）。

内聚簇 A：worker 失败后的【确定性、零 LLM】恢复决策所依赖的纯路径/依赖图分析。
自包含（仅 stdlib + WorkerOutput），不反向 import nodes/__init__（守 A6 破的环）。
可 patch 符号仍经 nodes/__init__ re-export 保 `swarm.brain.nodes.X` 可寻址；但簇内互调
（_blocked_pkg_unrecoverable → _package_in_baseline / _is_missing_dependency_failure →
_det_of）走本模块 global，故测试要 patch 本模块（swarm.brain.nodes.recovery.X）。
"""

from __future__ import annotations

import json
import os

from swarm.types import WorkerOutput

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
_INTERNAL_BLOCKED_KINDS = ("internal_pkg_not_built", "upstream_module_broken")


def _det_of(out) -> dict:
    """统一取 worker 失败结果的 l1_details（§3.2：委托 shared.l1_details_of 单一实现，本地名保 seam）。"""
    from swarm.brain.nodes.shared import l1_details_of
    return l1_details_of(out)


def _producers_of(plan_obj, packages, modules) -> set[str]:
    """反查【生产某内部包/某模块的子任务 id】：按 plan 子任务 scope.writable 文件路径归属匹配。

    治本 replan 死循环关键：下游因引用上游模块/包而 BLOCKED 时，跨模块 import 依赖的 depends_on
    在 plan 期常拿不到（见 l1_pipeline 自注），无法靠 depends_on 反查上游。改用运行时 worker 吐出的
    blocked_on_packages/modules，按【谁的 scope.writable 落在该模块目录 / 含该包目录段】归属到生产者
    子任务。通用跨栈、非项目写死（纯路径归属，不含任何硬编码 FQN/模块名）。"""
    out: set[str] = set()
    pkg_paths = ["/".join(p.split(".")) for p in (packages or []) if p]
    mods = {str(m).strip().strip("/") for m in (modules or []) if str(m).strip()}
    for s in getattr(plan_obj, "subtasks", []):
        scope = getattr(s, "scope", None)
        writ = list(getattr(scope, "writable", []) or []) if scope else []
        for f in writ:
            fn = str(f).replace("\\", "/").lstrip("./")
            top = fn.split("/", 1)[0]
            if top in mods:
                out.add(s.id)
                break
            if any(("/" + pp + "/") in ("/" + fn) for pp in pkg_paths):
                out.add(s.id)
                break
    return out


def _package_in_baseline(project_path: str | None, pkg: str) -> bool:
    """点分包名 pkg 是否已存在于【基线项目树】任一模块 src 下（确定性、零 LLM）。

    #R13-2 治本关键：worker 臆造一个基线里根本不存在的包(如 com.ruoyi.common.core.redis)时，
    L1 会误判 internal_pkg_not_built(transient，等一个【永不会来的生产者】)，白烧整条重试阶梯。
    但"BLOCKED on X 且 plan 无生产者"不足以判臆造——X 可能是【基线已有、只是沙箱漏同步】的包，
    那种应继续 transient 等待、绝不硬失败。故用本函数做【假阳性护栏】：只有 X 既无 plan 生产者、
    【又不在基线树里】才判为臆造(永不可满足)。纯路径匹配、通用跨栈、非项目写死。
    扫描失败/无路径 → 保守返回 True(当作【存在】→ 不硬失败)，宁可多等也不误杀。"""
    if not project_path or not pkg:
        return True  # 无从判定 → 保守当【存在】，不据此硬失败
    rel = pkg.replace(".", "/").strip("/")
    if not rel:
        return True
    try:
        for root, dirs, _files in os.walk(project_path):
            # 剪枝构建产物/VCS/依赖目录，控制开销
            dirs[:] = [d for d in dirs
                       if d not in (".git", "target", "build", "dist", "out",
                                    "node_modules", ".gradle", ".idea")]
            if root.replace(os.sep, "/").endswith("/" + rel):
                return True
    except OSError:
        return True  # 扫描异常 → 保守当【存在】，避免误杀
    return False


def _blocked_pkg_unrecoverable(
    blocked_pkgs, producers, unsat, completed_ok, pending, project_path, self_id,
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
    return bool(blocked_pkgs) and not any(
        _package_in_baseline(project_path, p) for p in blocked_pkgs
    )


def _is_missing_dependency_failure(subtask_results: dict, failed_ids: list) -> bool:
    """失败详情里是否命中"缺符号/缺依赖"编译特征（确定性、零 LLM）。
    排除 internal_pkg_not_built/upstream_module_broken——那是【内部产物未就绪】非缺外部 jar，
    走 A2 补依赖必空烧(见 _INTERNAL_BLOCKED_KINDS 注释)。"""
    for fid in failed_ids:
        det = _det_of(subtask_results.get(fid))
        if isinstance(det, dict) and det.get("pipeline_blocked") in _INTERNAL_BLOCKED_KINDS:
            continue  # 内部/上游未就绪 → 不该触发 A2 maven 补依赖
        try:
            blob = json.dumps(det, ensure_ascii=False).lower()
        except (TypeError, ValueError):
            blob = str(det).lower()
        if any(p in blob for p in _MISSING_DEP_PATTERNS):
            return True
    return False
