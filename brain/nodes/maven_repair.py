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
# ★R65E-T4（round65e5 st-53-1 D1）★ 包名【叶】上的通用子包词——它们零区分度，单独命中
# artifactId 会误绑（`dev.samstevens.totp.generator` 的 `generator` 撞内部模块 `ruoyi-generator`）。
# 去掉后 groupId 前缀锚（anchor1）仍能命中 groupId 惯例库（zxing.core←com.google.zxing:core），
# 不伤真匹配。
_MAVEN_LEAF_NOISE = frozenset({
    "generator", "generators", "util", "utils", "core", "api", "apis", "common", "commons",
    "service", "services", "client", "clients", "impl", "internal", "model", "models",
    "config", "configuration", "base", "support", "helper", "helpers", "exception", "exceptions",
    "annotation", "annotations", "spi", "type", "types", "constant", "constants", "factory",
})
_MISSING_PKG_BRAIN_RE = re.compile(
    r"(?:程序包|package)\s+([\w.]+)\s+(?:不存在|does not exist)", re.I)
_DEP_BLOCK_RE = re.compile(r"<dependency>([\s\S]*?)</dependency>", re.I)
_ARTIFACT_RE = re.compile(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", re.I)
_GROUP_RE = re.compile(r"<groupId>\s*([^<\s]+)\s*</groupId>", re.I)


def _pkg_match_tokens(pkg: str) -> list[str]:
    """从缺失包名提取可匹配 Maven artifactId/groupId 的辨识 token（去通用段、去数字后缀变体）。
    org.quartz→['quartz']；okhttp3.x→['okhttp3','okhttp']；com.fasterxml.jackson.databind→['fasterxml','jackson','databind']。"""
    raw: list[str] = []
    for s in [s for s in pkg.split(".") if s]:
        if s in _MAVEN_GENERIC_SEG or len(s) <= 2:
            continue
        if s not in raw:
            raw.append(s)
        st = s.rstrip("0123456789")
        if st and st != s and st not in raw:
            raw.append(st)
    # ★复核 HIGH 整改★ 叶噪词零区分度予以去除——但【仅当去后仍留辨识 token】。否则短包唯一
    # token 被清空、丢失真自证匹配：org.apache.commons.io→仅 'commons'、org.springframework.core→
    # 仅 'core'、javax.annotation→仅 'annotation' 都会被误清成空集。留 ≥1 才去（generator 因
    # totp/samstevens 仍在而被去，误绑照治；commons/core 作唯一 token 时保留，真匹配不伤）。
    non_noise = [t for t in raw if t not in _MAVEN_LEAF_NOISE]
    return non_noise if non_noise else raw


def _dep_provides_pkg(pkg: str, gid: str, aid: str) -> bool:
    """候选 <dependency>(groupId=gid, artifactId=aid) 是否【提供】缺失包 pkg。锚定匹配，杜绝子串误命中：
    - anchor1：Maven 惯例——包名以 groupId 为前缀（`org.quartz.*`←`org.quartz`；`com.google.zxing.*`
      ←`com.google.zxing`）。内部模块 groupId(`com.ruoyi`) 绝不前缀外部包(`dev.samstevens.totp.*`)。
    - anchor2：辨识 token（去通用段/去叶噪）命中 artifactId 的【整段】(按 -/_/. 切)——治 `okhttp3`←
      `okhttp` 这类非 groupId 惯例；整段匹配杜绝 `generator` 子串撞 `ruoyi-generator`。"""
    p = (pkg or "").lower()
    g = (gid or "").lower().strip()
    if g and (p == g or p.startswith(g + ".")):
        return True
    toks = _pkg_match_tokens(pkg)
    if not toks:
        return False
    aid_segs = {s for s in re.split(r"[-_.]", (aid or "").lower()) if s}
    return any(t.lower() in aid_segs for t in toks)


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
    匹配走 `_dep_provides_pkg`（groupId 前缀锚 / artifactId 整段锚）；多命中取 artifactId 最短。
    ★复核 HIGH 整改★ 绝不在此提前 `if not toks: return None`——anchor1(groupId 前缀)不需要 toks，
    早退会让 `javax.annotation`/`org.springframework.core` 这类【段全落叶噪/通用】的包永不进 anchor1、
    静默拒修真依赖。统一交 `_dep_provides_pkg` 判（其 anchor2 内部已 `if not toks: return False` 安全兜底）。"""
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
            if aid and _dep_provides_pkg(pkg, gid.group(1) if gid else "", aid.group(1)):
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
                # ★复核 MEDIUM 整改（降级可观测）★ 提取到缺失包却找不到自证坐标 = 要么真无（外部
                # 未 provision），要么匹配拒了——必须留痕，否则运维无法把"确定性拒修"与"本就无坐标"
                # 区分，静默吞掉 Finding-1 类误拒。这也是 D2 外部未 provision 信号的锚点。
                logger.info("[DEP-REPAIR] A2 未能为缺失包 %r 找到自证坐标（项目其它 pom 未声明）→ "
                            "跳过注入（不臆造；很可能是未 provision 的外部依赖，交 replan/人工）", pkg)
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


def _stack_driver_keys(project_stack: dict | None) -> list[str]:
    """从 stack 画像解析 dep-repair driver 候选键（inject/classify **共用**，口径不得分叉）。

    6.9-RF3：detect_stack 真实画像字段是 build（值=maven/gradle/pip/go/cargo…，_MANIFEST_BACKEND
    口径）——旧键列表 ("build_system","backend","primary") 没有一个存在于真实 schema，正常 E2E
    （画像必在）时 A2 治本被静默关闭、只有无画像才恢复旧行为，与设计意图恰好相反（复核活体实证）。
    build_system 保留兼容测试/外部注入。"""
    _keys: list[str] = []
    if isinstance(project_stack, dict):
        for k in ("build", "build_system"):
            v = str(project_stack.get(k) or "").strip().lower()
            if v:
                _keys.append(v)
        # backend 值是自由文本（如 "Spring Boot 2.x (java)"）——不能整串当键，
        # 按 driver 键做子串探测兜底（"maven" in backend 之类），仍是确定性分发。
        _backend = str(project_stack.get("backend") or "").strip().lower()
        if _backend:
            _keys.extend(dk for dk in _DEP_REPAIR_DRIVERS if dk in _backend)
    if not _keys:
        _keys = ["maven"]  # 无画像时保留旧行为（Maven 是既有唯一实现）
    return _keys


def inject_missing_deps_for_stack(project_stack: dict | None, project_path: str | None,
                                  granted: dict, subtask_results: dict) -> dict:
    """按项目栈分发缺依赖确定性补全 driver。未覆盖栈返回 {}（loud，不静默）。"""
    _keys = _stack_driver_keys(project_stack)
    for k in _keys:
        drv = _DEP_REPAIR_DRIVERS.get(k)
        if drv is not None:
            return drv(project_path, granted, subtask_results)
    logger.warning(
        "[DEP-REPAIR] F9 栈 %s 暂无缺依赖补全 driver（Maven-only 现状），跳过确定性注入"
        "（可观测缺口，缺依赖交常规重试阶梯）", _keys[:3])
    return {}


def _classify_missing_maven_deps(project_path: str | None, granted: dict,
                                 subtask_results: dict) -> dict:
    """D2（round65e5 st-53-1）：把每个已授权失败子任务的缺失包分成【可自证补全 provisionable】与
    【全仓无坐标 unprovisioned】两类（确定性、**不 mutate**）。

    unprovisioned = 项目全仓 + 依赖 pom 均无提供该包的 <dependency> 坐标（`_find_maven_dep_for_pkg`
    返 None）→ 极可能是臆造 import/API（st-53-1 `dev.samstevens.totp.generator` 幻觉子包实锤：jar 在
    classpath、子包不存在），或真未 provision 的外部库。两者都**不该**靠"补依赖 + 授 pom 写权 + 重派"
    救——补依赖无坐标可补、授 pom 写权反诱发小模型手改基线 pom 腐化。调用方据此给"改代码别加依赖"guidance。"""
    out: dict = {}
    if not project_path:
        return out
    for sid, mod_pom in (granted or {}).items():
        from swarm.brain.nodes.shared import l1_details_of
        det = l1_details_of((subtask_results or {}).get(sid))  # §3.2 收敛
        blob = det.get("build_output") if isinstance(det.get("build_output"), str) else ""
        if not blob:
            try:
                blob = json.dumps(det, ensure_ascii=False)
            except (TypeError, ValueError):
                blob = str(det)
        prov: list[str] = []
        unprov: list[str] = []
        for pkg in _extract_missing_pkgs(blob):
            if _find_maven_dep_for_pkg(project_path, pkg, mod_pom):
                prov.append(pkg)
            else:
                unprov.append(pkg)
        if prov or unprov:
            out[sid] = {"provisionable": prov, "unprovisioned": unprov}
    return out


# D2：缺失包分类 driver（provisionable vs unprovisioned），与补全 driver 同键空间/同分发口径。
_CLASSIFY_DRIVERS = {
    "maven": _classify_missing_maven_deps,
}


def classify_missing_deps_for_stack(project_stack: dict | None, project_path: str | None,
                                    granted: dict, subtask_results: dict) -> dict:
    """D2：按项目栈分发缺失包分类 driver（provisionable vs unprovisioned）。未覆盖栈返回 {}（loud）。"""
    _keys = _stack_driver_keys(project_stack)
    for k in _keys:
        drv = _CLASSIFY_DRIVERS.get(k)
        if drv is not None:
            return drv(project_path, granted, subtask_results)
    # ★复核 F2 整改★ loud no-op 配 record_degrade（degrade 约定）：非 Maven 栈 D2 静默缺席时
    # /api/metrics 亦可见，不只埋在 swarm.log。
    from swarm.infra.degrade import record_degrade
    record_degrade("brain.dep_classify.stack_uncovered")
    logger.warning(
        "[DEP-REPAIR] D2 栈 %s 暂无缺失包分类 driver（Maven-only 现状），跳过分类"
        "（可观测缺口，退回既有恢复行为）", _keys[:3])
    return {}
