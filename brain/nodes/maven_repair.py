"""brain/nodes/maven_repair.py — Maven 缺失依赖确定性补全簇（round24 god-file 拆解·簇B-1）。

治本 A2：定向恢复给失败子任务模块 pom 写权后，小模型仍常不把缺的依赖加进去（实测 RuoYi
st-31：用 org.quartz 但 ruoyi-alarm/pom.xml 没声明 → 2 次定向恢复耗尽 → 落全量 replan 砸掉
30 个成功）。这里在【授权后立即】确定性补：从编译错误取缺失包 → 在项目【其它 pom】里找声明了
它的 <dependency> 块（项目自己用过=权威坐标）→ 注入失败模块 pom。项目从没用过该包 → 查无、
不动（不臆造坐标）。

自包含（仅 stdlib + WorkerOutput），不反向 import nodes/__init__（守 A6 破的环）。均为纯函数、
无测试 patch，经 nodes/__init__ re-export 保 swarm.brain.nodes.X 可寻址。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from swarm.types import WorkerOutput

import logging

logger = logging.getLogger(__name__)

_MAVEN_GENERIC_SEG = {"org", "com", "net", "io", "cn", "www", "java", "javax",
                      "jakarta", "apache", "springframework", "google"}
_MISSING_PKG_BRAIN_RE = re.compile(
    r"(?:程序包|package)\s+([\w.]+)\s+(?:不存在|does not exist)", re.I)
_DEP_BLOCK_RE = re.compile(r"<dependency>([\s\S]*?)</dependency>", re.I)
_ARTIFACT_RE = re.compile(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", re.I)
_GROUP_RE = re.compile(r"<groupId>\s*([^<\s]+)\s*</groupId>", re.I)


def _pkg_match_tokens(pkg: str) -> list[str]:
    """从缺失包名提取可匹配 Maven artifactId/groupId 的辨识 token（去通用段、去数字后缀变体）。
    org.quartz→['quartz']；okhttp3.x→['okhttp3','okhttp']；com.fasterxml.jackson.databind→['fasterxml','jackson','databind']。"""
    toks: list[str] = []
    for s in [s for s in pkg.split(".") if s]:
        if s in _MAVEN_GENERIC_SEG or len(s) <= 2:
            continue
        if s not in toks:
            toks.append(s)
        st = s.rstrip("0123456789")
        if st and st != s and st not in toks:
            toks.append(st)
    return toks


def _extract_missing_pkgs(blob: str) -> list[str]:
    """从编译错误文本解析缺失包名（确定性）。"""
    seen: set = set()
    out: list[str] = []
    for m in _MISSING_PKG_BRAIN_RE.finditer(blob or ""):
        p = m.group(1)
        # 不强求含 "."：okhttp3 这类单段包名也是合法缺失包（实测 st-17）。正则上下文
        # （程序包 X 不存在）已足够特定，X 必是包名。
        if p and len(p) >= 3 and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _iter_project_poms(project_path: str, limit: int = 80) -> list:
    skip = {"target", "node_modules", ".git", "build", "dist", ".gradle", ".idea"}
    out: list = []
    try:
        for p in Path(project_path).rglob("pom.xml"):
            if any(part in skip for part in p.relative_to(project_path).parts):
                continue
            out.append(p)
            if len(out) >= limit:
                break
    except OSError:
        pass
    return out


def _find_maven_dep_for_pkg(project_path: str, pkg: str, exclude_pom_rel: str) -> str | None:
    """在项目【其它 pom】找声明了能提供该缺失包的 <dependency> 块（项目自证坐标，不臆造）。
    辨识 token 命中 artifactId/groupId；多命中取 artifactId 最短（最贴近）。返回 <dependency> 块文本或 None。"""
    toks = _pkg_match_tokens(pkg)
    if not toks:
        return None
    try:
        excl = (Path(project_path) / exclude_pom_rel).resolve() if exclude_pom_rel else None
    except OSError:
        excl = None
    cands: list[tuple[int, str]] = []
    for pom in _iter_project_poms(project_path):
        try:
            if excl and pom.resolve() == excl:
                continue
            text = pom.read_text("utf-8", errors="ignore")
        except OSError:
            continue
        for m in _DEP_BLOCK_RE.finditer(text):
            block = m.group(0)
            aid = _ARTIFACT_RE.search(block)
            gid = _GROUP_RE.search(block)
            hay = f"{gid.group(1) if gid else ''} {aid.group(1) if aid else ''}".lower()
            if aid and any(t.lower() in hay for t in toks):
                cands.append((len(aid.group(1)), block.strip()))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0][1]


def _inject_dep_into_pom(pom_path: Path, dep_block: str) -> bool:
    """把 <dependency> 块注入 pom 最后一个 <dependencies>（模块项目级，通常在 dependencyManagement 之后）。
    已声明同 artifactId 则跳过。无 <dependencies> 段则保守不动（不新建段，免破坏结构）。返回是否改动。"""
    try:
        text = pom_path.read_text("utf-8", errors="ignore")
    except OSError:
        return False
    aid_m = _ARTIFACT_RE.search(dep_block)
    if aid_m and re.search(r"<artifactId>\s*" + re.escape(aid_m.group(1)) + r"\s*</artifactId>", text):
        return False
    idx = text.rfind("</dependencies>")
    if idx == -1:
        return False
    inject = "        " + dep_block.strip() + "\n    "
    try:
        pom_path.write_text(text[:idx] + inject + text[idx:], encoding="utf-8")
        return True
    except OSError:
        return False


def _inject_missing_maven_deps(project_path: str | None, granted: dict, subtask_results: dict) -> dict:
    """治本 A2：授权后据项目自身 pom 把缺失包对应的 <dependency> 直接补进失败模块 pom。
    返回 {sid: [已补 artifactId]}。让重派的 worker 直接编过，不再耗尽定向恢复配额→不触发全量 replan。"""
    if not project_path:
        return {}
    injected: dict = {}
    for sid, mod_pom in (granted or {}).items():
        from swarm.brain.nodes.shared import l1_details_of
        det = l1_details_of((subtask_results or {}).get(sid))  # §3.2 收敛
        blob = det.get("build_output") if isinstance(det.get("build_output"), str) else ""
        if not blob:
            try:
                blob = json.dumps(det, ensure_ascii=False)
            except (TypeError, ValueError):
                blob = str(det)
        added: list = []
        for pkg in _extract_missing_pkgs(blob):
            dep = _find_maven_dep_for_pkg(project_path, pkg, mod_pom)
            if not dep:
                continue
            if _inject_dep_into_pom(Path(project_path) / mod_pom, dep):
                a = _ARTIFACT_RE.search(dep)
                added.append(a.group(1) if a else pkg)
        if added:
            injected[sid] = added
    return injected


# ── F9（阶段6，登记册 §七）：缺依赖确定性补全 per-stack driver 化 ──
# 恢复阶梯主体（planning_core 拆小/revert/stub）本就栈无关；唯【缺依赖注入】这层
# 此前 Maven 写死。抽分发面：按 stack_detect 画像选 driver；未覆盖栈显式留痕
# no-op（可观测缺口，非静默），新增栈=注册一个 driver 函数，不改调用方。
_DEP_REPAIR_DRIVERS = {
    # stack key（stack_detect 画像的 build_system/backend 口径）→ driver
    "maven": _inject_missing_maven_deps,
}


def inject_missing_deps_for_stack(project_stack: dict | None, project_path: str | None,
                                  granted: dict, subtask_results: dict) -> dict:
    """按项目栈分发缺依赖确定性补全 driver。未覆盖栈返回 {}（loud，不静默）。"""
    _keys = []
    if isinstance(project_stack, dict):
        for k in ("build_system", "backend", "primary"):
            v = str(project_stack.get(k) or "").strip().lower()
            if v:
                _keys.append(v)
    if not _keys:
        _keys = ["maven"]  # 无画像时保留旧行为（Maven 是既有唯一实现）
    for k in _keys:
        drv = _DEP_REPAIR_DRIVERS.get(k)
        if drv is not None:
            return drv(project_path, granted, subtask_results)
    logger.warning(
        "[DEP-REPAIR] F9 栈 %s 暂无缺依赖补全 driver（Maven-only 现状），跳过确定性注入"
        "（可观测缺口，缺依赖交常规重试阶梯）", _keys[:3])
    return {}
