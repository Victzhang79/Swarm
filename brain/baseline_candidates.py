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


# R65E6-T1 复核 MEDIUM 整改：证据判定专用 token（比 extract_req_tokens 宽）——额外纳入【数字打头
# 但含字母】的技术缩略（2fa/3des/oauth2/sha512），否则 "2FA" 这类需求判别词零 token 会被静默豁免、
# 令本闸的动机 bug 换措辞复现（复核实锤 `extract_req_tokens("支持2FA") == []`）。仍剔停用词+纯数字。
# 只用于证据闸，绝不改 extract_req_tokens（candidates 通道行为不动）。
_EVIDENCE_TOKEN_RE = re.compile(r"[A-Za-z0-9]{3,}")
_CAMEL_WORD_RE = re.compile(r"[A-Z]+[a-z0-9]*|[a-z0-9]+")


def extract_evidence_tokens(text: str) -> list[str]:
    """证据判定 token：数字/字母混合 ≥3 字符、含≥1 字母、去停用词（小写去重保序）。"""
    out: list[str] = []
    seen: set[str] = set()
    for t in _EVIDENCE_TOKEN_RE.findall(str(text or "")):
        low = t.lower()
        if low in _STOP or low in seen:
            continue
        if not any(c.isalpha() for c in low):   # 纯数字无判别力
            continue
        seen.add(low)
        out.append(low)
    return out


def _camel_initials(name: str) -> str:
    """CamelCase/snake 名 → 首字母缩略（SingleSignOnFilter→ssof / SysUserController→suc）。
    供证据 blob 让 SSO/RBAC 这类字母缩略 token 匹配到展开命名的存量（复核 HIGH：sso↔SingleSignOn）。"""
    words = _CAMEL_WORD_RE.findall(str(name or ""))
    return "".join(w[0] for w in words if w).lower()


def build_baseline_vocab(files: list[dict], symbols: list[dict]) -> str:
    """R65E6-T1：把基线【符号名/类名/文件名 stem/模块名 + 其 CamelCase 首字母缩略】拼成单一小写 blob，
    供 baseline_covered 证据判定做子串检索（token in blob）。空索引 → 空串（调用方据此 fail-open 全豁免）。
    缩略入 blob 令字母缩略需求词（sso/rbac/acl）能匹配展开命名（SingleSignOn…）——减少过严误拒（复核 HIGH）。
    缩略是【增补证据】（只会让闸更宽松=fail-open 方向），绝不引入新的误拒。"""
    parts: list[str] = []
    for sym in (symbols or []):
        if not isinstance(sym, dict):
            continue
        for k in ("symbol_name", "class_name"):
            v = str(sym.get(k) or "")
            if v:
                parts.append(v.lower())
                _ini = _camel_initials(v)
                if len(_ini) >= 3:            # 太短缩略(≤2)判别力弱、易误命中，不入
                    parts.append(_ini)
    for f in (files or []):
        if not isinstance(f, dict):
            continue
        fp = str(f.get("file_path") or "")
        if fp:
            parts.append(_basename_stem(fp))
        m = str(f.get("module_name") or "").lower()
        if m:
            parts.append(m)
    return "\n".join(parts)


def baseline_claims_missing_evidence(
    baseline_covered: list[dict] | None,
    requirement_items: list[dict] | None,
    baseline_vocab: str | None,
) -> list[str]:
    """R65E6-T1（round65e6 task 3c94e4ea 实锤）：返回【申报 baseline_covered 却在基线无任何证据】的 req-id。

    死因：planner 把 Google 2FA（req-aaecf423）申报 baseline_covered、reason 把幻觉能力嫁接真文件
    （SysUserController 真存在但无 2FA 方法；基线全库 2fa/totp/authenticator 符号=0）→ 既有闸只校验
    id∈需求清单+reason 非空 → 假申报直接过 → 2FA 静默丢出交付。

    证据判定（Option D，确定性、narrow，子agent 活体验证 catch 2FA 且 0 误拒合法存量）：
    - baseline_vocab 空（KB 不可达）→ [] 全豁免（fail-open，绝不因索引缺失误拒真申报）；
    - 需求文本 `extract_req_tokens` 零 token（纯中文无 ASCII 标识符）→ 豁免（round37 过严教训：
      通知公告类合法存量申报会被误拒）；
    - 有 token 且【全部】token 都不作子串出现在 blob → 无证据 → 列入返回（打回：极可能把新特性
      谎称存量）。悬空 id（不在 requirement_items）不在此判——另有 dangling_baseline 校验。
    """
    if not baseline_covered or not baseline_vocab:
        return []
    vocab = baseline_vocab.lower()
    text_by_id: dict[str, str] = {}
    for it in (requirement_items or []):
        if isinstance(it, dict) and it.get("id"):
            text_by_id[str(it["id"])] = str(it.get("text") or "")
    out: list[str] = []
    seen: set[str] = set()
    for entry in baseline_covered:
        if not isinstance(entry, dict):
            continue
        rid = str(entry.get("id") or "").strip()
        if not rid or rid in seen:
            continue
        text = text_by_id.get(rid)
        if text is None:          # 悬空 id → 交 dangling_baseline，本闸不重复判
            continue
        toks = extract_evidence_tokens(text)   # 宽口径：含 2fa/oauth2 等数字缩略
        if not toks:              # 纯中文/无判别 token 豁免（过严会误拒合法存量，round37 教训）
            continue
        if not any(tk in vocab for tk in toks):
            out.append(rid)
            seen.add(rid)
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
