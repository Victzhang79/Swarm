"""生产就绪度离线评测基准——对【E2E 生成的项目产物】客观认证"是不是真生产级完整"。

痛点:E2E 一轮 ~$3000、~小时级,跑完只能靠"感觉"判断生成的项目是不是完整生产级。
RUN20 生成 RuoYi 告警平台暴露真实缺陷(覆盖不全 / 悬空符号 / 错用规范),却要肉眼逐文件
才发现。本基准把【产物快照 + 原始 plan】固化成夹具,对其做 4 维静态分析(全离线、零 LLM、
秒级),客观打分卡 + 缺陷清单,作为"完完全全真真正正完成"的客观合格证。

四维:
  1. 覆盖度(coverage):plan.subtasks.scope.create_files 的"应建文件全集" vs 产物快照"实际
     存在文件",算覆盖率 + 列缺失,并按层(domain/mapper/xml/service/serviceImpl/controller/
     html/sql/...)归类报告"哪些实体缺哪层"。
  2. 分层完整性(layering):对每个业务实体(从 domain/*.java 推),核 RuoYi 标准 6 层是否齐
     (domain + mapper接口 + mapper XML + service接口 + serviceImpl + controller)。缺层列出。
  3. 规范合规(convention):静态正则扫 java——
     - @PreAuthorize / import org.springframework.security → 违例(经典若依是 Shiro
       @RequiresPermissions / com.ruoyi.* 包)。
     - controller 是否 extends BaseController(RuoYi 约定)。
  4. 悬空符号(dangling,静态近似编译检查):扫每个 java 的 import com.ruoyi.* 与跨包类型引用,
     对照"快照内实际存在的类 + ruoyi-common 白名单"。引用了既不在产物、也不在白名单的 com.ruoyi
     类 → 悬空符号违例(廉价抓"引用不存在的项目类",能抓 RedisCache / 错包路径 DTO)。
     说明:这是静态近似,非真 javac——抓不到 Matcher.tail 误用、方法签名不符、泛型擦除等,仅抓
     "符号根本不存在"这一类(见报告诚实声明)。

用法:
    python3 test/benchmark/production_readiness/production_readiness_bench.py   # 跑全部夹具,打分卡
    python3 -m pytest test/test_production_readiness_bench.py                   # CI 回归
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES_DIR = os.path.join(_HERE, "fixtures")
MANIFEST = os.path.join(_HERE, "manifest.json")

# ---------------------------------------------------------------------------
# 文件分层归类:把仓库相对路径映射到 RuoYi 标准层名(供覆盖度+分层完整性共用)。
# ---------------------------------------------------------------------------

def classify_layer(rel: str) -> str:
    """把一个文件路径归到一个层。返回层名(coverage/layering 报告用)。"""
    r = rel.replace("\\", "/")
    if r.endswith(".sql"):
        return "sql"
    if r.endswith(".html"):
        return "html"
    if r.endswith("pom.xml"):
        return "pom"
    if r.endswith(".xml") and "/mapper/" in r:
        return "mapper_xml"
    if not r.endswith(".java"):
        return "other"
    # java 按目录/命名定层
    if "/domain/" in r:
        return "domain"
    if "/mapper/" in r:           # mapper 接口(java)
        return "mapper"
    if "/service/impl/" in r or "/serviceimpl/" in r.lower():
        return "serviceImpl"
    if "/service/" in r:
        return "service"
    if "/controller/" in r:
        return "controller"
    if "/config/" in r:
        return "config"
    if "/interceptor/" in r:
        return "interceptor"
    if "/task/" in r:
        return "task"
    return "other_java"


# RuoYi 标准业务实体的 6 个分层(分层完整性维度核查的层)。
ENTITY_LAYERS = ("domain", "mapper", "mapper_xml", "service", "serviceImpl", "controller")


# ---------------------------------------------------------------------------
# 工具:plan / 快照 加载
# ---------------------------------------------------------------------------

def load_expected_files(plan_path: str) -> list[str]:
    """从 plan 所有 subtasks.scope.create_files 汇成"应建文件全集"(去重、归一斜杠)。"""
    raw = json.load(open(plan_path, encoding="utf-8"))
    out: set[str] = set()
    for s in raw.get("subtasks", []):
        for f in (s.get("scope") or {}).get("create_files") or []:
            out.add(f.replace("\\", "/").lstrip("./"))
    return sorted(out)


def scan_snapshot(snapshot_dir: str) -> list[str]:
    """递归列出快照里所有相关产物文件,返回相对 snapshot 根的路径。"""
    out: list[str] = []
    for root, _dirs, files in os.walk(snapshot_dir):
        if os.sep + "target" + os.sep in root + os.sep:
            continue
        for fn in files:
            if fn.endswith((".java", ".xml", ".html", ".sql")) or fn == ".gitkeep":
                rel = os.path.relpath(os.path.join(root, fn), snapshot_dir)
                out.append(rel.replace("\\", "/"))
    return sorted(out)


def load_whitelist(path: str) -> set[str]:
    if not path or not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as fh:
        return {ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")}


# ---------------------------------------------------------------------------
# 维度 1:覆盖度
# ---------------------------------------------------------------------------

@dataclass
class CoverageResult:
    expected: int
    present: int
    missing: list[str]
    coverage_pct: float
    missing_by_layer: dict          # layer -> [missing files]
    present_by_layer: dict          # layer -> count


def dim_coverage(expected: list[str], snapshot_files: list[str]) -> CoverageResult:
    snap = set(snapshot_files)
    present = [f for f in expected if f in snap]
    missing = [f for f in expected if f not in snap]
    by_layer_missing: dict = {}
    by_layer_present: dict = {}
    for f in missing:
        by_layer_missing.setdefault(classify_layer(f), []).append(f)
    for f in present:
        ly = classify_layer(f)
        by_layer_present[ly] = by_layer_present.get(ly, 0) + 1
    pct = round(100.0 * len(present) / len(expected), 1) if expected else 0.0
    return CoverageResult(
        expected=len(expected), present=len(present), missing=missing,
        coverage_pct=pct, missing_by_layer=by_layer_missing,
        present_by_layer=by_layer_present,
    )


# ---------------------------------------------------------------------------
# 维度 2:分层完整性(以快照实际落盘的 domain 实体为锚)
# ---------------------------------------------------------------------------

@dataclass
class LayeringResult:
    entities: dict                  # entity -> {layer: bool}
    incomplete: dict                # entity -> [missing layers]


_DOMAIN_RE = re.compile(r"/domain/([A-Za-z0-9_]+)\.java$")


def _entity_layer_present(entity: str, snapshot_files: list[str]) -> dict:
    """对一个实体,核 6 个标准层是否在快照中存在(按命名约定匹配)。"""
    found = {ly: False for ly in ENTITY_LAYERS}
    for f in snapshot_files:
        ly = classify_layer(f)
        base = os.path.basename(f)
        if ly == "domain" and base == f"{entity}.java":
            found["domain"] = True
        elif ly == "mapper" and base == f"{entity}Mapper.java":
            found["mapper"] = True
        elif ly == "mapper_xml" and base == f"{entity}Mapper.xml":
            found["mapper_xml"] = True
        elif ly == "service" and base in (f"I{entity}Service.java", f"{entity}Service.java"):
            found["service"] = True
        elif ly == "serviceImpl" and base == f"{entity}ServiceImpl.java":
            found["serviceImpl"] = True
        elif ly == "controller" and base == f"{entity}Controller.java":
            found["controller"] = True
    return found


def dim_layering(snapshot_files: list[str]) -> LayeringResult:
    entities: dict = {}
    incomplete: dict = {}
    domains = sorted({m.group(1) for f in snapshot_files
                      if (m := _DOMAIN_RE.search("/" + f.lstrip("/")))})
    for ent in domains:
        layers = _entity_layer_present(ent, snapshot_files)
        entities[ent] = layers
        missing = [ly for ly, ok in layers.items() if not ok]
        if missing:
            incomplete[ent] = missing
    return LayeringResult(entities=entities, incomplete=incomplete)


# ---------------------------------------------------------------------------
# 维度 3:规范合规(静态正则扫 java)
# ---------------------------------------------------------------------------

@dataclass
class ConventionResult:
    preauthorize: list[str]         # 用了 @PreAuthorize 的文件
    spring_security: list[str]      # import org.springframework.security 的文件
    controller_no_basecontroller: list[str]
    violations: int = 0


_PREAUTH_RE = re.compile(r"@PreAuthorize\b")
_SPRING_SEC_RE = re.compile(r"import\s+org\.springframework\.security")
_CLASS_DECL_RE = re.compile(r"\bclass\s+\w+")
_EXTENDS_BASECTRL_RE = re.compile(r"extends\s+BaseController\b")


def dim_convention(snapshot_dir: str, snapshot_files: list[str]) -> ConventionResult:
    preauth: list[str] = []
    springsec: list[str] = []
    ctrl_nobase: list[str] = []
    for rel in snapshot_files:
        if not rel.endswith(".java"):
            continue
        text = open(os.path.join(snapshot_dir, rel), encoding="utf-8", errors="replace").read()
        if _PREAUTH_RE.search(text):
            preauth.append(rel)
        if _SPRING_SEC_RE.search(text):
            springsec.append(rel)
        # controller 约定:文件名 *Controller.java 且声明了 class,但不 extends BaseController
        if os.path.basename(rel).endswith("Controller.java") and _CLASS_DECL_RE.search(text):
            if not _EXTENDS_BASECTRL_RE.search(text):
                ctrl_nobase.append(rel)
    res = ConventionResult(preauthorize=preauth, spring_security=springsec,
                           controller_no_basecontroller=ctrl_nobase)
    res.violations = len(preauth) + len(springsec) + len(ctrl_nobase)
    return res


# ---------------------------------------------------------------------------
# 维度 4:悬空符号(静态近似编译检查)
# ---------------------------------------------------------------------------

@dataclass
class DanglingResult:
    # (引用文件, 悬空的 com.ruoyi 全限定名)
    dangling: list[tuple]
    produced_classes: int
    whitelist_classes: int


_IMPORT_RE = re.compile(r"^\s*import\s+(static\s+)?(com\.ruoyi\.[A-Za-z0-9_.]+);", re.M)
_PKG_RE = re.compile(r"^\s*package\s+([A-Za-z0-9_.]+);", re.M)


def _produced_class_fqns(snapshot_dir: str, snapshot_files: list[str]) -> set[str]:
    """快照里实际定义的所有 java 类的全限定名(package + 文件名)。"""
    fqns: set[str] = set()
    for rel in snapshot_files:
        if not rel.endswith(".java") or os.path.basename(rel) == ".gitkeep":
            continue
        text = open(os.path.join(snapshot_dir, rel), encoding="utf-8", errors="replace").read()
        m = _PKG_RE.search(text)
        if not m:
            continue
        cls = os.path.basename(rel)[:-len(".java")]
        fqns.add(f"{m.group(1)}.{cls}")
    return fqns


def dim_dangling(snapshot_dir: str, snapshot_files: list[str], whitelist: set[str]) -> DanglingResult:
    produced = _produced_class_fqns(snapshot_dir, snapshot_files)
    dangling: list[tuple] = []
    for rel in snapshot_files:
        if not rel.endswith(".java"):
            continue
        text = open(os.path.join(snapshot_dir, rel), encoding="utf-8", errors="replace").read()
        for m in _IMPORT_RE.finditer(text):
            fqn = m.group(2)
            # 通配 import com.ruoyi.x.* 跳过(无法精确比对单类)
            if fqn.endswith(".*"):
                continue
            if fqn in produced or fqn in whitelist:
                continue
            dangling.append((rel, fqn))
    return DanglingResult(dangling=dangling, produced_classes=len(produced),
                          whitelist_classes=len(whitelist))


# ---------------------------------------------------------------------------
# 单夹具运行
# ---------------------------------------------------------------------------

@dataclass
class BenchResult:
    run: str
    coverage: CoverageResult
    layering: LayeringResult
    convention: ConventionResult
    dangling: DanglingResult
    snapshot_counts: dict = field(default_factory=dict)
    expectations_met: bool = True
    notes: list[str] = field(default_factory=list)


def run_fixture(entry: dict) -> BenchResult:
    plan_path = os.path.join(FIXTURES_DIR, entry["plan"])
    snapshot_dir = os.path.join(FIXTURES_DIR, entry["snapshot"])
    whitelist = load_whitelist(os.path.join(FIXTURES_DIR, entry["whitelist"])) \
        if entry.get("whitelist") else set()

    expected = load_expected_files(plan_path)
    snap_files = scan_snapshot(snapshot_dir)

    cov = dim_coverage(expected, snap_files)
    lay = dim_layering(snap_files)
    con = dim_convention(snapshot_dir, snap_files)
    dan = dim_dangling(snapshot_dir, snap_files, whitelist)

    counts = {
        "java": sum(1 for f in snap_files if f.endswith(".java")),
        "xml": sum(1 for f in snap_files if f.endswith(".xml")),
        "html": sum(1 for f in snap_files if f.endswith(".html")),
        "sql": sum(1 for f in snap_files if f.endswith(".sql")),
    }

    res = BenchResult(run=entry.get("run", "?"), coverage=cov, layering=lay,
                      convention=con, dangling=dan, snapshot_counts=counts)

    # 期望核对(以 manifest 声明的期望区间/布尔为准——据实测设,不硬编死数字)
    exp = entry.get("expect", {})
    if "coverage_lt" in exp and not (cov.coverage_pct < exp["coverage_lt"]):
        res.expectations_met = False
        res.notes.append(f"覆盖率期望 <{exp['coverage_lt']}% 实得 {cov.coverage_pct}%")
    if "coverage_ge" in exp and not (cov.coverage_pct >= exp["coverage_ge"]):
        res.expectations_met = False
        res.notes.append(f"覆盖率期望 >={exp['coverage_ge']}% 实得 {cov.coverage_pct}%")
    if "missing_layers" in exp:
        miss_layers = set(cov.missing_by_layer)
        for need in exp["missing_layers"]:
            if need not in miss_layers:
                res.expectations_met = False
                res.notes.append(f"覆盖维度期望缺层含 '{need}' 但未抓到")
    if exp.get("dangling_min"):
        if len(dan.dangling) < exp["dangling_min"]:
            res.expectations_met = False
            res.notes.append(f"悬空符号期望 >={exp['dangling_min']} 实得 {len(dan.dangling)}")
    if exp.get("preauthorize_min"):
        if len(con.preauthorize) < exp["preauthorize_min"]:
            res.expectations_met = False
            res.notes.append(f"@PreAuthorize 期望 >={exp['preauthorize_min']} 实得 {len(con.preauthorize)}")
    if exp.get("incomplete_entities_min"):
        if len(lay.incomplete) < exp["incomplete_entities_min"]:
            res.expectations_met = False
            res.notes.append(f"缺层实体期望 >={exp['incomplete_entities_min']} 实得 {len(lay.incomplete)}")
    return res


def run_all() -> list[BenchResult]:
    manifest = json.load(open(MANIFEST, encoding="utf-8"))
    return [run_fixture(e) for e in manifest["fixtures"]]


def total_defects(r: BenchResult) -> int:
    """该夹具抓到的"真实缺陷"总数(覆盖缺失 + 缺层 + 规范违例 + 悬空)。"""
    return (len(r.coverage.missing) + len(r.layering.incomplete)
            + r.convention.violations + len(r.dangling.dangling))


def write_report(results: list[BenchResult], path: str) -> None:
    out = []
    for r in results:
        out.append({
            "run": r.run,
            "snapshot_counts": r.snapshot_counts,
            "coverage": {
                "expected": r.coverage.expected,
                "present": r.coverage.present,
                "coverage_pct": r.coverage.coverage_pct,
                "missing_by_layer": {k: len(v) for k, v in r.coverage.missing_by_layer.items()},
                "missing": r.coverage.missing,
            },
            "layering": {
                "entities_total": len(r.layering.entities),
                "incomplete": r.layering.incomplete,
            },
            "convention": {
                "preauthorize": r.convention.preauthorize,
                "spring_security": r.convention.spring_security,
                "controller_no_basecontroller": r.convention.controller_no_basecontroller,
            },
            "dangling": {
                "produced_classes": r.dangling.produced_classes,
                "whitelist_classes": r.dangling.whitelist_classes,
                "dangling": [{"file": f, "symbol": s} for f, s in r.dangling.dangling],
            },
            "total_defects": total_defects(r),
            "expectations_met": r.expectations_met,
            "notes": r.notes,
        })
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"fixtures": out}, fh, ensure_ascii=False, indent=2)


def _scorecard(results: list[BenchResult]) -> str:
    L = ["", "=" * 80, "生产就绪度离线评测基准 (Production-Readiness Bench)", "=" * 80]
    passed = 0
    for r in results:
        ok = r.expectations_met
        passed += ok
        mark = "PASS" if ok else "FAIL"
        c = r.coverage
        L.append(f"[{mark}] {r.run}  快照 java={r.snapshot_counts['java']} "
                 f"xml={r.snapshot_counts['xml']} html={r.snapshot_counts['html']} "
                 f"sql={r.snapshot_counts['sql']}")
        L.append("  --- 维度1 覆盖度 ---")
        L.append(f"      覆盖率 {c.coverage_pct}%  ({c.present}/{c.expected} 应建文件存在)")
        if c.missing_by_layer:
            for ly in sorted(c.missing_by_layer):
                fs = c.missing_by_layer[ly]
                L.append(f"      缺 {ly}: {len(fs)} -> {', '.join(os.path.basename(x) for x in fs)}")
        L.append("  --- 维度2 分层完整性 ---")
        L.append(f"      实体 {len(r.layering.entities)} 个,缺层 {len(r.layering.incomplete)} 个")
        for ent, miss in sorted(r.layering.incomplete.items()):
            L.append(f"      {ent}: 缺 {', '.join(miss)}")
        L.append("  --- 维度3 规范合规 ---")
        L.append(f"      @PreAuthorize={len(r.convention.preauthorize)}  "
                 f"spring.security={len(r.convention.spring_security)}  "
                 f"controller未extends BaseController={len(r.convention.controller_no_basecontroller)}")
        for f in r.convention.preauthorize:
            L.append(f"      ✗ @PreAuthorize: {f}")
        for f in r.convention.spring_security:
            L.append(f"      ✗ spring.security import: {f}")
        for f in r.convention.controller_no_basecontroller:
            L.append(f"      ✗ ctrl 未 extends BaseController: {os.path.basename(f)}")
        L.append("  --- 维度4 悬空符号(静态近似) ---")
        L.append(f"      产物类 {r.dangling.produced_classes} + 白名单 {r.dangling.whitelist_classes}"
                 f" -> 悬空 {len(r.dangling.dangling)}")
        for f, s in r.dangling.dangling:
            L.append(f"      ✗ {os.path.basename(f)} 引用不存在符号: {s}")
        L.append(f"  >>> 本夹具共抓到真实缺陷 {total_defects(r)} 处")
        for n in r.notes:
            L.append(f"      ⚠ 期望未达成: {n}")
        L.append("-" * 80)
    L.append(f"通过 {passed}/{len(results)} 夹具")
    L.append("=" * 80)
    return "\n".join(L)


if __name__ == "__main__":
    results = run_all()
    print(_scorecard(results))
    report_path = os.path.join(_HERE, "report.json")
    write_report(results, report_path)
    print(f"\n报告已写入 {report_path}")
    failed = [r for r in results if not r.expectations_met]
    raise SystemExit(1 if failed else 0)
