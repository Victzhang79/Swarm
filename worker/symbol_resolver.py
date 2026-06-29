"""编译错误符号接地 — 把 javac 的 `cannot find symbol` 解析成 codegraph FQN 修复提示。

治本(RUN20 主导缺陷类):本地 40B 常在【猜的包路径/接口名】上引用类——
  - `com.ruoyi.alarm.dto.CallbackRequest`(真实在 `com.ruoyi.alarm.domain.dto`)
  - `IAlarmEngineService`(真实是 `engine.service.AlarmEngineService`)
→ javac 报 `cannot find symbol: class X` → 40B 拿到这个错只会【再猜一次】→ 死循环。

gap 不在【检测】(javac 已检测),在【worker 不知道正确的包/名去修】。本模块把缺失符号
拿去 codegraph(structure_index)查真实 FQN，产出"X 真实位置: <FQN>，改 import"的解析提示，
追加进 worker 的编译修复反馈——把"无解的 cannot find symbol"变成"照着改 import 即可"。

设计：解析/反推/组装提示全为【纯函数】(无 IO，易测)；异步 DB 查询由 resolve_and_format
薄包一层。只【增】修复提示，不改既有编译/修复判定逻辑。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# javac/maven `cannot find symbol` 后续的 `symbol: class X` 行。兼容多空格、interface/enum。
_SYMBOL_RE = re.compile(
    r"symbol:\s*(?P<kind>class|interface|enum|method|variable)\s+(?P<name>[A-Za-z_]\w*)",
)
# Java 源码根标记：.../src/main/java/<pkg path>/<Class>.java
_JAVA_ROOT_RE = re.compile(r"(?:^|/)(?:src/main/java|src/test/java|java)/(?P<rel>.+\.java)$")


@dataclass
class MissingSymbol:
    kind: str        # class | interface | enum | method | variable
    name: str


@dataclass
class SymbolHint:
    name: str
    status: str                              # resolved | planned | absent | unverified
    real_fqns: list[str] = field(default_factory=list)   # codegraph 查到的真实 FQN
    planned_paths: list[str] = field(default_factory=list)  # plan 里将创建它的文件
    message: str = ""


def parse_missing_symbols(build_output: str) -> list[MissingSymbol]:
    """从编译输出抽取 `cannot find symbol` 点名的符号(去重，保序)。只取类型符号供 FQN 解析。"""
    if not build_output:
        return []
    seen: set[tuple[str, str]] = set()
    out: list[MissingSymbol] = []
    for m in _SYMBOL_RE.finditer(build_output):
        kind, name = m.group("kind"), m.group("name")
        key = (kind, name)
        if key in seen:
            continue
        seen.add(key)
        out.append(MissingSymbol(kind=kind, name=name))
    return out


def file_path_to_fqn(file_path: str) -> str | None:
    """`ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/dto/CallbackRequest.java`
    → `com.ruoyi.alarm.domain.dto.CallbackRequest`。非 java 源或无法识别返回 None。"""
    if not file_path:
        return None
    norm = file_path.replace("\\", "/")
    m = _JAVA_ROOT_RE.search(norm)
    if not m:
        return None
    rel = m.group("rel")[: -len(".java")]
    return rel.replace("/", ".")


def build_symbol_hints(
    missing: list[MissingSymbol],
    resolved: dict[str, list[str]],
    plan_create_files: list[str] | None = None,
    query_failed: set[str] | None = None,
) -> list[SymbolHint]:
    """组装提示(纯函数)。
    resolved: {符号名: [codegraph 查到的真实 FQN]}（调用方据 codegraph 预取，已按精确名过滤）。
    plan_create_files: 整个 plan 将创建的文件全集 → 判断"是 sibling 子任务将建(等它)"。
    query_failed: codegraph 查询【失败(异常)】的符号名集合 → 与"查到=不存在"严格区分。

    A-P1-11：codegraph 查询失败(DB 未连/超时)旧实现把 rows 当空 → 误判"符号在整个项目
    中不存在"→ 反向误导模型臆造新类。修复：查询失败的符号标 status=unverified，措辞改为
    "无法核实(查询失败)"，绝不断言不存在。仅【真查到为空】才判 absent。
    """
    plan_files = plan_create_files or []
    failed = query_failed or set()
    hints: list[SymbolHint] = []
    for ms in missing:
        if ms.kind not in ("class", "interface", "enum"):
            continue  # method/variable 错不靠 FQN 解析（多为签名/拼写）
        name = ms.name
        fqns = [f for f in dict.fromkeys(resolved.get(name, [])) if f]
        planned = [
            p for p in plan_files
            if p.endswith(".java") and p.replace("\\", "/").rsplit("/", 1)[-1] == f"{name}.java"
        ]
        if fqns:
            hints.append(SymbolHint(
                name=name, status="resolved", real_fqns=fqns,
                message=(f"符号 `{name}` 不在你引用的包，真实位置: "
                         + " / ".join(f"`{f}`" for f in fqns)
                         + "。修正 import/全限定名为真实 FQN，勿臆造包路径。"),
            ))
        elif planned:
            hints.append(SymbolHint(
                name=name, status="planned", planned_paths=planned,
                message=(f"符号 `{name}` 由其它子任务创建于 {planned}（尚未就绪）。"
                         "确保依赖该子任务先完成，勿自行臆造。"),
            ))
        elif name in failed:
            # 查询失败 ≠ 不存在：不下"项目中不存在"结论，避免反向误导模型臆造新类。
            hints.append(SymbolHint(
                name=name, status="unverified",
                message=(f"符号 `{name}` 无法核实（codegraph 查询失败，非「不存在」）。"
                         "请优先在项目中搜索其真实包路径后再引用，切勿据此臆造新类或包名。"),
            ))
        else:
            hints.append(SymbolHint(
                name=name, status="absent",
                message=(f"符号 `{name}` 在整个项目中不存在（codegraph 与 plan 均无）。"
                         "需新建该类，或改用项目中已存在的等价类——切勿照训练记忆臆造不存在的 API。"),
            ))
    return hints


def format_symbol_hints(hints: list[SymbolHint]) -> str:
    """渲染成可追加进 worker 修复上下文的文本块。无提示返回空串。"""
    if not hints:
        return ""
    lines = ["【符号接地提示】编译报 `cannot find symbol`，以下为 codegraph 解析结果（照此修，勿再猜包名）："]
    for h in hints:
        lines.append(f"  - {h.message}")
    return "\n".join(lines)


# ── P5：臆造【方法】接地（symbol-repair 的近邻纠错接不住——无项目近邻；codegraph 也跳过 method）──
# 治本(996db614 实测 18×900s 主因之一)：模型在【真实存在的类】上调用【不存在的方法】
# （如 java.util.Base64.Encoder.encodeToByte，真方法 encodeToString/encode）→ javac
# `cannot find symbol: method X / location: class C`。worker 拿到原始错只会再臆造一个方法 →
# fix-loop 烧满 900s。gap 在【worker 不知道类 C 的真实方法集】。解法：沙箱内 javap C 取真实
# 方法，告诉模型"C 真实方法有 [...]，X 不存在，从中选"。纯解析为纯函数(易测)，javap 执行由
# 调用方(executor，持沙箱)薄包一层。

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_METHOD_SYM_RE = re.compile(r"symbol:\s*method\s+([A-Za-z_]\w*)\s*\(")
_LOC_CLASS_RE = re.compile(r"location:\s*(?:class|interface)\s+([\w.$]+)")
_LOC_VAR_RE = re.compile(r"location:\s*variable\s+\w+\s+of\s+type\s+([\w.$]+)")


def parse_missing_methods(build_output: str) -> list[tuple[str, str]]:
    """从编译输出抽 `cannot find symbol: method X / location: class C`（或 variable of type T）→
    [(method_name, owner_class_fqn)]，去重保序。纯函数。"""
    if not build_output:
        return []
    lines = _ANSI_RE.sub("", build_output).split("\n")
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for i, line in enumerate(lines):
        ms = _METHOD_SYM_RE.search(line)
        if not ms:
            continue
        method = ms.group(1)
        klass = None
        for j in range(i + 1, min(i + 3, len(lines))):
            cm = _LOC_CLASS_RE.search(lines[j])
            if cm:
                klass = cm.group(1)
                break
            vm = _LOC_VAR_RE.search(lines[j])
            if vm:
                klass = vm.group(1)
                break
        if klass and (method, klass) not in seen:
            seen.add((method, klass))
            out.append((method, klass))
    return out


def to_javap_class_name(fqn: str) -> str:
    """`java.util.Base64.Encoder`(javac 点分嵌套) → `java.util.Base64$Encoder`(javap 二进制名)。

    包段(小写首字母)保留点；首个类段(大写首)之后的段用 $ 连接（内部类）。"""
    parts = fqn.split(".")
    out: list[str] = []
    in_class = False
    for p in parts:
        if not p:
            continue
        if not in_class and p[:1].isupper():
            in_class = True
            out.append(p)
        elif in_class:
            out[-1] = out[-1] + "$" + p
        else:
            out.append(p)
    return ".".join(out)


_JAVAP_METHOD_RE = re.compile(r"([A-Za-z_]\w*)\s*\(")
_JAVAP_NOISE = frozenset({"class", "interface", "enum", "if", "for", "while", "switch", "catch"})


def parse_javap_methods(javap_output: str) -> list[str]:
    """从 javap 输出抽公有方法名（去重保序）。纯函数。"""
    if not javap_output:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in javap_output.split("\n"):
        if "(" not in line:
            continue
        m = _JAVAP_METHOD_RE.search(line)
        if m:
            name = m.group(1)
            if name not in seen and name not in _JAVAP_NOISE:
                seen.add(name)
                out.append(name)
    return out


def build_method_grounding(probed: list[tuple[str, str, list[str]]]) -> str:
    """组装方法接地提示。probed: [(臆造方法, 类, 该类真实方法集)]。无可用返回空串。纯函数。"""
    blocks = []
    for method, klass, real in probed:
        if not real:
            continue
        shown = ", ".join(real[:30])
        blocks.append(
            f"  - 类 `{klass}` 没有方法 `{method}`（你臆造了它）。其真实方法有: [{shown}]。"
            f"从中选语义最接近的真实方法，勿再臆造。"
        )
    if not blocks:
        return ""
    return "【方法接地提示】你在真实存在的类上调用了不存在的方法（javap 实证）：\n" + "\n".join(blocks)


async def resolve_and_format(
    build_output: str,
    project_id: str,
    indexer,
    plan_create_files: list[str] | None = None,
) -> str:
    """异步编排：解析缺失符号 → codegraph 查 FQN → 组装提示文本。

    indexer: 具备 async query_symbols_by_name(project_id, name) 的 StructureIndexer。
    任一步异常都吞掉返回空串（接地提示是【增益】，绝不能因它让修复回路崩）。
    """
    try:
        missing = parse_missing_symbols(build_output)
        if not missing:
            return ""
        resolved: dict[str, list[str]] = {}
        query_failed: set[str] = set()  # A-P1-11：查询失败(异常)的符号，与"查到为空"区分
        for ms in missing:
            if ms.kind not in ("class", "interface", "enum"):
                continue
            try:
                rows = await indexer.query_symbols_by_name(project_id, ms.name)
            except Exception:  # noqa: BLE001
                # 查询失败 ≠ 符号不存在：标记失败，下游措辞为"无法核实"而非"不存在"
                query_failed.add(ms.name)
                continue
            fqns: list[str] = []
            for r in rows or []:
                # ILIKE 模糊 → 精确名过滤（symbol_name 或 class_name 精确等于）
                if r.get("symbol_name") == ms.name or r.get("class_name") == ms.name:
                    fqn = file_path_to_fqn(r.get("file_path") or "")
                    if fqn:
                        fqns.append(fqn)
            if fqns:
                resolved[ms.name] = fqns
        hints = build_symbol_hints(missing, resolved, plan_create_files, query_failed)
        return format_symbol_hints(hints)
    except Exception:  # noqa: BLE001
        return ""
