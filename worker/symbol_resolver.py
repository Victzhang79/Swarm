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
    status: str                              # resolved | planned | absent
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
) -> list[SymbolHint]:
    """组装提示(纯函数)。
    resolved: {符号名: [codegraph 查到的真实 FQN]}（调用方据 codegraph 预取，已按精确名过滤）。
    plan_create_files: 整个 plan 将创建的文件全集 → 判断"是 sibling 子任务将建(等它)"。
    """
    plan_files = plan_create_files or []
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
        for ms in missing:
            if ms.kind not in ("class", "interface", "enum"):
                continue
            try:
                rows = await indexer.query_symbols_by_name(project_id, ms.name)
            except Exception:  # noqa: BLE001
                rows = []
            fqns: list[str] = []
            for r in rows or []:
                # ILIKE 模糊 → 精确名过滤（symbol_name 或 class_name 精确等于）
                if r.get("symbol_name") == ms.name or r.get("class_name") == ms.name:
                    fqn = file_path_to_fqn(r.get("file_path") or "")
                    if fqn:
                        fqns.append(fqn)
            if fqns:
                resolved[ms.name] = fqns
        hints = build_symbol_hints(missing, resolved, plan_create_files)
        return format_symbol_hints(hints)
    except Exception:  # noqa: BLE001
        return ""
