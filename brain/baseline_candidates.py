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


# R67F-P2（专项2·接地核验）：路径样 token——含 "/" 且以代码文件扩展名结尾（保守，防误把
# 中文/普通词当路径）。用于 evidence_files 缺失时从 reason 自由文本兜底提取引用路径。
_PATH_LIKE_RE = re.compile(
    r"(?<![\w./-])((?:[\w.\-]+/)+[\w.\-]+\.(?:java|kt|scala|go|py|ts|tsx|js|jsx|"
    r"vue|rs|c|cc|cpp|h|hpp|cs|rb|php|xml|sql|yml|yaml|properties))")


def _norm_index_path(p: str) -> str:
    s = str(p or "").replace("\\", "/").strip()
    return s[2:] if s.startswith("./") else s


def build_baseline_file_index(files: list[dict], symbols: list[dict]) -> dict:
    """R67F-P2（专项2）：把基线 (files, symbols) 拼成【保 per-file 结构】的接地索引——
    与 build_baseline_vocab（拍平全库单 blob，用于 T6 token 命中）互补：本索引保留
    "哪个文件有哪些符号"，供接地核验 A（路径∈索引）/ B（该路径符号命中 req token）。

    round67 #2 死型：reason 把幻觉能力嫁接真实文件（"SysUserController 已实现 2FA"），T6
    全库 blob 命中无法区分"命中 reason 所引文件"与"命中无关文件 B"→ 需要 per-file 结构。
    返回 {"files": set[归一路径], "symbols_by_file": {归一路径: 小写符号/类名 blob}}。
    空输入 → 空索引（调用方据此 fail-open 全豁免）。通用多栈：纯字符串结构无语言特判。
    """
    file_set: set[str] = set()
    for f in (files or []):
        if isinstance(f, dict):
            fp = _norm_index_path(f.get("file_path") or "")
            if fp:
                file_set.add(fp)
    sym_by_file: dict[str, list[str]] = {}
    for sym in (symbols or []):
        if not isinstance(sym, dict):
            continue
        fp = _norm_index_path(sym.get("file_path") or "")
        if not fp:
            continue
        bucket = sym_by_file.setdefault(fp, [])
        for k in ("symbol_name", "class_name"):
            v = str(sym.get(k) or "").strip().lower()
            if v:
                bucket.append(v)
                _ini = _camel_initials(v)
                if len(_ini) >= 3:
                    bucket.append(_ini)
    # basename stem + 路径段 token 也进各文件 blob（复核 Reviewer HIGH 整改）：子闸 B 改 all()
    # 后，需求词里的【包/目录域名】（handler/encrypt/sms/auth…）应能经文件路径接地，否则 all()
    # 会误杀"能力体现在包名而非符号名"的合法申报（如 Go svc-user/internal/handler/user.go 对
    # 需求词 "handler"）。路径段是语言无关字符串，栈中立。
    for fp in file_set:
        bucket = sym_by_file.setdefault(fp, [])
        bucket.append(_basename_stem(fp))
        for seg in re.split(r"[/.\-_]+", fp.lower()):
            if len(seg) >= 3:
                bucket.append(seg)
    return {"files": file_set,
            "symbols_by_file": {fp: "\n".join(toks) for fp, toks in sym_by_file.items()}}


def _match_index_path(claimed: str, file_set: set[str]) -> str | None:
    """归一后：精确命中，或某基线文件以 "/claimed" 结尾（planner 给部分路径）→ 返回命中的基线路径。
    ★不做纯 basename 匹配★（round67c 血泪：裸 basename 会误命中同名无关文件），要求 claimed
    含路径分隔或以完整文件名 suffix 命中，保守 fail-closed。"""
    c = _norm_index_path(claimed)
    if not c:
        return None
    if c in file_set:
        return c
    suffix = "/" + c
    hits = [f for f in file_set if f.endswith(suffix)]
    return hits[0] if len(hits) == 1 else None      # 多义 suffix 命中=歧义，不认（保守）


def baseline_claims_unground(
    baseline_covered: list[dict] | None,
    requirement_items: list[dict] | None,
    file_index: dict | None,
) -> list[str]:
    """R67F-P2（专项2·接地核验 A/B）：返回【申报 baseline_covered 但 reason 引用的文件经不起
    per-file 接地】的 req-id。与 T6（baseline_claims_missing_evidence，全库 blob token 命中）
    并存不同路径——T6 从不看 reason 引用的【具体文件】、且全库 blob 命中无法定位到 reason 所引
    的那个文件；本闸补上 reason→文件接地这条路径。

    死型（round67 #2，E 类静默毒交付）：planner 谎报新特性 baseline_covered，reason 把幻觉能力
    嫁接真实文件路径（"SysUserController 已实现 2FA"）→ 需求静默蒸发→假 DONE 无账。

    两子闸（对每条申报收集候选路径：优先 evidence_files，缺失则从 reason/evidence 正则兜底提）：
      A 路径∈基线索引：任一候选路径不在索引（捏造路径=最强作假信号）→ REJECT。★path 是 ASCII
        与 req 语言无关 → 纯中文 req 也走 A（捏造路径不因纯中文豁免）★。
      B 符号命中 req token：候选路径【全部】命中失败（该文件符号 blob 不含任何 req 判别 token
        = 嫁接无能力文件）→ REJECT。req 零判别 token（纯中文）→ 豁免 B（round37 过严教训），
        但 A 已挡捏造路径。
    fail-open 边界：空索引（KB 不可达）→ [] 全豁免（node 侧 record_degrade 不静默）；申报无任何
      候选路径 → 跳过（无路径可验，交 T6 token 接地）。通用多栈：无语言特判。
    """
    if not baseline_covered or not file_index:
        return []
    file_set = file_index.get("files") or set()
    sym_by_file = file_index.get("symbols_by_file") or {}
    if not file_set:
        return []                # 空索引 fail-open（node record_degrade）
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
        # 候选路径：优先显式 evidence_files，缺失从 reason/evidence 正则兜底
        ef = entry.get("evidence_files")
        cand_paths = ([str(p) for p in ef if str(p).strip()]
                      if isinstance(ef, list) and ef else [])
        if not cand_paths:
            blob = f"{entry.get('reason') or ''}\n{entry.get('evidence') or ''}"
            cand_paths = _PATH_LIKE_RE.findall(blob)
        if not cand_paths:
            continue              # 无路径引用 → 跳过（交 T6）
        # 子闸 A：任一候选路径不在索引 → 捏造 → REJECT
        matched = [_match_index_path(p, file_set) for p in cand_paths]
        if any(m is None for m in matched):
            out.append(rid)
            seen.add(rid)
            continue
        # 子闸 B：req 判别 token 须【全部】命中候选路径符号 blob 的并集；零 token（纯中文）豁免 B。
        # ★复核 Reviewer HIGH 整改★：从 any() 改 all()（镜像 T6 R67-7 硬化）——any 语义下一个巧合
        # 泛词（如真实 CRUD 符号 edit/save）会掩护缺失的判别 token（sms/otp/jwt）放行假申报，正是
        # 本专项要堵的"嫁接真实文件"死型。all() 要求每个判别 token 都在【所列证据文件】的符号/路径
        # 并集里找到证据，否则=嫁接无能力文件 → REJECT（planner 补齐 evidence_files 是诚实出路）。
        toks = extract_evidence_tokens(text)
        if not toks:
            continue              # 纯中文无判别 token → A 已过，豁免 B（防误杀 round37 教训）
        _union_blob = "\n".join((sym_by_file.get(m) or "") for m in matched if m).lower()
        if not all(tk in _union_blob for tk in toks):
            out.append(rid)       # 有判别 token 在证据文件并集里零证据 = 嫁接无能力文件
            seen.add(rid)
    return out


def baseline_claims_missing_evidence(
    baseline_covered: list[dict] | None,
    requirement_items: list[dict] | None,
    baseline_vocab: str | None,
) -> list[str]:
    """R65E6-T1（round65e6 task 3c94e4ea 实锤）：返回【申报 baseline_covered 却在基线无任何证据】的 req-id。

    死因：planner 把 Google 2FA（req-aaecf423）申报 baseline_covered、reason 把幻觉能力嫁接真文件
    （SysUserController 真存在但无 2FA 方法；基线全库 2fa/totp/authenticator 符号=0）→ 既有闸只校验
    id∈需求清单+reason 非空 → 假申报直接过 → 2FA 静默丢出交付。

    证据判定（R67-T6 分层收紧；round67 R67-7 实锤 any 语义被泛词掩护——req"JWT…Token 加入
    Redis 黑名单"里 token/redis 命中掩护 jwt 零命中 → 假申报放行需求蒸发）：
    - baseline_vocab 空（KB 不可达）→ [] 全豁免（fail-open，绝不因索引缺失误拒真申报）；
    - 需求文本零 token（纯中文无 ASCII 标识符）→ 豁免（round37 过严教训）；
    - 第一层：申报带 evidence 引文 → 引文 token 全命中 vocab=实证放行；零/部分命中=引文捏造
      （R67-15 isFrame 死型）→ 列入返回；
    - 第二层：无 evidence → 需求判别 token 须【全部】命中 vocab，部分命中即列入返回（打回引导
      补 evidence 或建子任务实现）。悬空 id 不在此判——另有 dangling_baseline 校验。
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
        # R67-T6 第一层：申报自带 evidence 引文（基线中真实存在的标识符/文件名）→ 实证放行/拒。
        # evidence 的判别 token 全部命中 vocab = 引文真实 → 放行（逃生门是实证不是口说）；
        # 有 evidence 却零/部分命中 = 引文捏造（R67-15 isFrame 死型）→ 打回。
        ev = str(entry.get("evidence") or "").strip()
        if ev:
            ev_toks = extract_evidence_tokens(ev)
            if ev_toks and all(tk in vocab for tk in ev_toks):
                continue
            out.append(rid)
            seen.add(rid)
            continue
        toks = extract_evidence_tokens(text)   # 宽口径：含 2fa/oauth2 等数字缩略
        if not toks:              # 纯中文/无判别 token 豁免（过严会误拒合法存量，round37 教训）
            continue
        # R67-T6 第二层（round67 R67-7 实锤）：无 evidence 时判别 token 须【全部】命中。
        # 旧 any 语义下 req"JWT登录…Token加入Redis黑名单"里 token/redis 泛词命中掩护了
        # jwt 零命中 → 假申报放行需求蒸发。部分命中=能力可疑，打回时引导补 evidence 引文
        # （确系存量）或建子任务实现（确系新功能）。
        if not all(tk in vocab for tk in toks):
            out.append(rid)
            seen.add(rid)
    return out


def build_planned_vocab(file_plan: list[dict] | None) -> str:
    """R65E7：把 tech_design 的 file_plan 各条目的【路径 stem + CamelCase 首字母缩略 + module +
    responsibility 文本】拼成单一小写 blob，供 requirements_missing_from_plan 做证据子串检索。
    空 file_plan → 空串（调用方据此 fail-open）。

    与 build_baseline_vocab 对称：证据来源不同（计划文件 vs 基线符号），判定口径同源
    （extract_evidence_tokens）。responsibility 文本入 blob 关键——它承载"2fa/google/sha512"这类
    需求判别词（路径 stem 只有类名 TwoFactorController，不含 "2fa"），令为某需求真排了文件时其
    token 能命中（不回归）。缩略入 blob 令字母缩略需求词（sso/rbac）匹配展开命名的规划文件。"""
    parts: list[str] = []
    for e in (file_plan or []):
        if not isinstance(e, dict):
            continue
        p = str(e.get("path") or "")
        if p:
            parts.append(_basename_stem(p))               # 小写 stem
            _raw = p.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
            _ini = _camel_initials(_raw)
            if len(_ini) >= 3:                              # 太短缩略判别力弱，不入（同 baseline_vocab）
                parts.append(_ini)
        m = str(e.get("module") or "").lower()
        if m:
            parts.append(m)
        r = str(e.get("responsibility") or "").lower()
        if r:
            parts.append(r)
    return "\n".join(parts)


def requirements_missing_from_plan(
    requirement_items: list[dict] | None,
    planned_vocab: str | None,
    baseline_vocab: str | None,
) -> list[str]:
    """R65E7（round65e7 task 044f2caa 实锤）：返回【有判别 token 却在 file_plan 未排任何文件、
    基线亦无存量证据】的 req-id（unplanned）——上游根治闸据此定向反馈设计 LLM 补排文件。

    死因：tech_design 从 PRD 原文产 file_plan、requirement_items 另路抽取，二者无覆盖交叉核验；
    2FA/SHA512 等在 183 file_plan 里 0 文件 → 无子任务能覆盖 → 只能被谎报 baseline → T1 拦 →
    恢复环无法 materialize（无文件可挂）→ 3 retry 耗尽 → FAILED@PLAN。

    判定（证据 token 与 T1 同源，narrow、栈无关）：
    - planned_vocab 或 baseline_vocab 任一空 → [] 全豁免（fail-open：缺数据绝不臆造补排工作；
      无 baseline_vocab 无从区分"新特性"与"存量能力"，逼排文件会给存量能力造重复实现）；
    - 需求 `extract_evidence_tokens` 零 token（纯中文无 ASCII 判别词）→ 豁免（round37 过严教训）；
    - token 命中 planned_vocab → 已排文件，放行；
    - 未排文件但命中 baseline_vocab → 合法存量满足，放行（不为存量能力逼排文件）；
    - 未排文件【且】非存量 → unplanned → 列入返回（逼上游补排，绝不留到下游被谎 baseline 掉）。
    """
    if not requirement_items or not planned_vocab or not baseline_vocab:
        return []
    pv = planned_vocab.lower()
    bv = baseline_vocab.lower()
    out: list[str] = []
    seen: set[str] = set()
    for it in requirement_items:
        if not isinstance(it, dict):
            continue
        rid = str(it.get("id") or "").strip()
        if not rid or rid in seen:
            continue
        toks = extract_evidence_tokens(str(it.get("text") or ""))
        if not toks:                          # 纯中文/无判别 token 豁免（过严会误报，round37 教训）
            continue
        if any(tk in pv for tk in toks):      # 已排文件 → 放行
            continue
        if any(tk in bv for tk in toks):      # 存量满足 → 放行
            continue
        out.append(rid)                        # 无文件无存量 → unplanned
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
