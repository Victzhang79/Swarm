"""A7（阶段3.5，登记册 §二）：确定性 baseline 候选通道——纯函数零 LLM。

病理：覆盖闸唯一存量依据=语义检索 top-12 文件再按字母序截 25 个（_format_project_structure），
预处理建好的 kb_file_index/kb_symbol_index 从不喂给覆盖闸——棕地底座需求（现有代码已满足、
无需新代码）结构上无申报出口，round37 实证 16 条 RuoYi 底座需求 baseline_covered 全程 0。

治本：对每条需求条目做【确定性索引匹配】（token→符号/类/文件名打分），产出候选申报清单
注入 PLAN prompt——LLM 只需【确认】候选（reason 必须指向清单中的可对账文件），而非在
看不见存量的情况下凭空申报。匹配不到=不出候选（fail-open 诚实降级，绝不臆造）。

口径：只抽 ASCII 标识符 token（≥3 字符）匹配代码符号——中文需求文本与英文代码符号的
对齐靠需求中出现的类名/接口名/术语（SysUser、2FA、login…）；纯中文无标识符的条目
不出候选（宁缺毋滥，臆造候选会诱导推卸型假申报=R34-8 病理）。通用多栈：零语言特判。
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
# 高频泛词不参与匹配（几乎所有代码库都命中，无判别力）
_STOP = {
    "the", "and", "for", "with", "api", "get", "set", "add", "new", "del",
    "web", "app", "http", "https", "json", "xml", "html", "url", "uri",
    "int", "str", "list", "map", "type", "data", "info", "test", "impl",
}


def extract_req_tokens(text: str) -> list[str]:
    """需求文本 → 判别性 ASCII token（小写去重保序）。"""
    out: list[str] = []
    seen: set[str] = set()
    for t in _TOKEN_RE.findall(str(text or "")):
        low = t.lower()
        if low in _STOP or low in seen:
            continue
        seen.add(low)
        out.append(low)
    return out


def _basename_stem(path: str) -> str:
    base = str(path or "").replace("\\", "/").rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0].lower()


def build_baseline_candidates(
    requirement_items: list[dict],
    files: list[dict],
    symbols: list[dict],
    *,
    max_per_req: int = 3,
    max_total: int = 120,
) -> list[dict]:
    """确定性候选构造：[{id, text, candidates: [{"file", "symbol"}...]}]。

    打分（token 对 各字段小写包含匹配，token≥4 才允许子串、==恒允许）：
      符号名 ==3/⊂2；类名 ==2/⊂1；文件名 stem ==2/⊂1；module_name ==1。
    单条目取分数最高的 max_per_req 个文件级候选；总量 max_total 封顶（超帽按条目序
    截断——候选是提示非闸门，截断无害）。files/symbols 空或条目无 token → []。
    """
    if not requirement_items or (not files and not symbols):
        return []
    # 预索引：file_path → 累积证据
    out: list[dict] = []
    total = 0
    for it in requirement_items:
        if not isinstance(it, dict):
            continue
        rid = str(it.get("id") or "").strip()
        text = str(it.get("text") or "")
        if not rid:
            continue
        tokens = extract_req_tokens(text)
        if not tokens:
            continue
        score_by_file: dict[str, float] = {}
        best_symbol: dict[str, str] = {}

        def _hit(token: str, value: str) -> int:
            v = str(value or "").lower()
            if not v:
                return 0
            if token == v:
                return 2
            if len(token) >= 4 and token in v:
                return 1
            return 0

        for sym in symbols:
            fp = str(sym.get("file_path") or "")
            if not fp:
                continue
            sname = str(sym.get("symbol_name") or "")
            cname = str(sym.get("class_name") or "")
            for tk in tokens:
                s = 0.0
                h = _hit(tk, sname)
                if h:
                    s += 1.5 * h  # ==3 / ⊂1.5
                h = _hit(tk, cname)
                if h:
                    s += 1.0 * h
                if s > 0:
                    score_by_file[fp] = score_by_file.get(fp, 0.0) + s
                    if fp not in best_symbol and sname:
                        best_symbol[fp] = sname
        for f in files:
            fp = str(f.get("file_path") or "")
            if not fp:
                continue
            stem = _basename_stem(fp)
            mod = str(f.get("module_name") or "").lower()
            for tk in tokens:
                h = _hit(tk, stem)
                if h:
                    score_by_file[fp] = score_by_file.get(fp, 0.0) + 1.0 * h
                if mod and tk == mod:
                    score_by_file[fp] = score_by_file.get(fp, 0.0) + 1.0
        ranked = sorted(score_by_file.items(), key=lambda kv: (-kv[1], kv[0]))
        cands = [{"file": fp, "symbol": best_symbol.get(fp, "")}
                 for fp, sc in ranked[:max_per_req] if sc >= 2.0]
        if not cands:
            continue
        out.append({"id": rid, "text": text[:120], "candidates": cands})
        total += 1
        if total >= max_total:
            break
    return out


def baseline_candidates_prompt_block(candidates: list[dict], *,
                                     truncated: bool = False) -> str:
    """候选申报清单 → PLAN prompt 注入块。空=空串（零噪声）。

    纪律：只许申报清单内条目；reason 必须指向清单列出的可对账文件；现有实现不满足
    则照常拆子任务，绝不推卸。truncated=True（索引清单达 4000/8000 上界被截断，
    阶段3.9 复核 F4）：「清单外不要申报」会把大仓的合法存量申报从"少提示"升级为
    "主动禁止"——改为自述截断并放开（申报仍须给可核实位置，validate 接地校验兜底）。

    措辞如实（复核 R-F9）：validate 侧现只校验 id∈需求清单+reason 非空，reason→文件
    的接地核验尚未实现（阶段6 D8 验收有牙补）——不再声称"系统会对账核验"。
    """
    if not candidates:
        return ""
    lines = []
    for c in candidates:
        refs = "; ".join(
            f"{d['file']}" + (f"（{d['symbol']}）" if d.get("symbol") else "")
            for d in (c.get("candidates") or []))
        lines.append(f"- {c['id']} {c.get('text', '')} → 存量疑似: {refs}")
    if truncated:
        _outside = (
            "注意：代码索引清单已达上界被截断——清单外的条目【允许】申报"
            " baseline_covered，但 reason 必须给出可核实的具体文件路径+满足方式，"
            "不确定就照常拆子任务。\n")
    else:
        _outside = "清单外的条目不要凭空申报 baseline_covered。\n"
    return (
        "\n\n## 存量候选对账清单（确定性代码索引检索所得——棕地申报通道）\n"
        "以下需求条目在现有代码索引中检索到疑似已实现的存量位置。请逐条核对：\n"
        "(a) 现有实现【确已满足】该需求 → 将其列入顶层 \"baseline_covered\"："
        "[{\"id\": \"req-xxxxxxxx\", \"reason\": \"<指向下面列出的具体文件+如何满足>\"}]，"
        "reason 必须引用清单中的文件路径；\n"
        "(b) 现有实现不满足/仅部分满足 → 照常拆子任务实现并 covers 该条目，绝不因"
        "存在相似代码就跳过实现。\n"
        + _outside
        + "\n".join(lines) + "\n")
