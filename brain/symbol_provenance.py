"""T4（round63 治本）：契约符号权威落点钉死 + 跨子任务类型引用 provenance 布线。

round63 死因（ROUND63_POSTMORTEM_TREATMENT_REGISTER.md·T4 调查结论）：plan 自洽——
file_plan/scope 给每个共享实体唯一物理落点，但权威**从未下发**：
- 契约符号只有裸名+构建模块名（contract_symbols_with_module），零 FQN/路径 → worker
  首发 import 全靠臆造（AlarmRobot 三包共存、AlarmSendLog 10× "package does not exist"）；
- AlarmSendLog 根本不在契约里（纯跨子任务实体），而 G2（wire_readable_provenance）只在
  consumer **已把 producer 文件列进 readable** 时才补依赖边 → "语料引用了跨子任务类型但
  scope 零声明"当前零覆盖，consumer 沙箱里既无文件也无落点信息。

治本（确定性、栈中立、plan 期，两个正交 pass）：
1. pin_contract_symbol_paths：从 plan 的 create_files∪writable（仅 code 文件）给
   shared_contract 的类型条目（interfaces/types/dtos）回填 ``defined_in``=权威物理路径。
   落点由 basename_symbol_match tier0/1 消歧（复用 C1 惯例等价口径，装饰前缀弱通道不参与
   钉死）；同符号多落点=计划内漂移 → 不钉+WARNING（surfaced，绝不静默挑一个）。字段名用
   defined_in 而非 path——apis 条目的 path 是 URL 语义（CONTRACT_MODULE schema），绝不可撞。
   栈中立：钉的是仓库相对路径，包名/模块名由 worker 按自己栈的惯例从路径推导。
2. wire_created_type_references：consumer 语料（description+acceptance_criteria+contract，
   与 C1 unowned_contract_symbols 同语料面）**区分大小写**词边界命中【跨子任务 create 的
   唯一 code stem】或【已钉契约符号名】→ 把 producer 路径补进 consumer 的
   readable+upstream_artifacts。依赖边不在此加——本 pass 之后既有 G2 按 readable∩create
   补边（复用其环守卫/歧义产者防护，不造第二套加边逻辑）。加边类动作用强判据：
   区分大小写 + stem 长度≥4 + 噪音表——比 C1 的 owner 判据（宽松无害）更严，因为这里的
   产出是真实的 scope/依赖变更。fan-out 超帽（通用名爆炸）→ 跳过+WARNING（fail-open 可观测）。

③（register）producer 真实文件进 consumer 沙箱**无需新机制**：readable 布好后，既有
executor_sync 上传（readable∈scope_files）+ seed 闸 fail-closed（缺产物判 BLOCKED）自动生效。
"""
from __future__ import annotations

import json
import logging
import re

from swarm.brain.contract_utils import _norm_scope_path
from swarm.brain.plan_validator import basename_symbol_match

logger = logging.getLogger(__name__)

# 与 align_readable_to_producer 的 _NON_CODE 同族再加数据/配置类——清单/资源/数据文件不是
# 类型落点，也不该被当作"被引用的跨子任务类型"布线（pom.xml/package.json 人人引用）。
_NON_CODE_EXT = frozenset({
    "xml", "yml", "yaml", "properties", "sql", "md", "html", "htm", "css",
    "scss", "sass", "less", "json", "txt", "csv", "ini", "conf", "cfg",
    "toml", "lock", "svg", "png", "jpg", "jpeg", "gif", "ico",
})

# 布线通道的 stem 噪音表：语料里高频出现却几乎不指向"某个具体跨子任务类型"的通用名。
# 宁缺勿滥——漏布线退回 round63 前行为（worker 猜），误布线会加边收窄并行度/污染 scope。
_STEM_NOISE = frozenset({
    "main", "test", "tests", "index", "app", "application", "util", "utils",
    "config", "common", "base", "core", "init", "setup", "types", "type",
    "constants", "const", "readme", "build", "settings", "module", "modules",
    "package", "pom", "helper", "helpers", "model", "models", "data",
    "__init__", "mod", "lib",
})

# 只钉"类型类"契约条目；apis 的 path 是 URL 语义绝不可撞，fields/methods 是成员名非类型。
_PIN_KEYS = ("interfaces", "types", "dtos")

_MIN_STEM_LEN = 4


def _stem(path: str) -> str:
    return path.rsplit("/", 1)[-1].split(".", 1)[0]


def _is_code_path(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    if "." not in base:
        return False
    return base.rsplit(".", 1)[-1].lower() not in _NON_CODE_EXT


def _scope_paths(st, *, include_writable: bool) -> list[str]:
    sc = getattr(st, "scope", None)
    out = [str(f) for f in (getattr(sc, "create_files", None) or [])]
    if include_writable:
        out += [str(f) for f in (getattr(sc, "writable", None) or [])]
    return [_norm_scope_path(f) for f in out]


def _location_index(plan, *, include_writable: bool) -> dict[str, set[str]]:
    """stem → 归一化落点集合（仅 code 文件）。多落点=计划内漂移，由调用方裁决。"""
    idx: dict[str, set[str]] = {}
    for st in (getattr(plan, "subtasks", None) or []):
        for p in _scope_paths(st, include_writable=include_writable):
            if _is_code_path(p):
                idx.setdefault(_stem(p), set()).add(p)
    return idx


def pin_contract_symbol_paths(plan) -> int:
    """①契约类型条目回填 defined_in=唯一权威落点。返回本次新钉条数（幂等重跑=0）。"""
    sc = getattr(plan, "shared_contract", None)
    subs = getattr(plan, "subtasks", None) or []
    if not isinstance(sc, dict) or not sc or not subs:
        return 0
    idx = _location_index(plan, include_writable=True)
    if not idx:
        return 0
    pinned = 0
    tier2_only: list[str] = []   # hunter#3：仅弱通道命中而钉不上的符号要留痕，不许全静默
    full_miss: list[str] = []    # R64-T4：任何通道零命中=真·命名漂移（此前完全静默，round64
                                 # 实测 30/56 漂移全走此分支，下游 R48b-1 被迫为幻影名造重复文件）
    for key in _PIN_KEYS:
        val = sc.get(key)
        if not isinstance(val, list):
            continue
        for item in val:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if len(name) < 3:
                continue
            # 强度消歧（R43 F1 同理）：先收最优 tier 的全部候选；tier2（装饰前缀弱通道）
            # 不参与钉死——"宁误勿漏"通道适合 owner 对账，不适合当权威落点。
            best_tier: int | None = None
            cand: set[str] = set()
            has_tier2 = False
            for stem, paths in idx.items():
                t = basename_symbol_match(stem, name)
                if t < 0:
                    continue
                if t > 1:
                    has_tier2 = True
                    continue
                if best_tier is None or t < best_tier:
                    best_tier, cand = t, set(paths)
                elif t == best_tier:
                    cand |= paths
            if not cand:
                if has_tier2:
                    tier2_only.append(name)
                else:
                    full_miss.append(name)
                continue
            if len(cand) > 1:
                # 条目 module（构建模块目录名）可消歧：只留该模块目录下的落点
                mod = str(item.get("module") or "").strip().strip("/")
                if mod:
                    under = {p for p in cand if p.startswith(mod + "/")}
                    if len(under) == 1:
                        cand = under
            if len(cand) != 1:
                logger.warning(
                    "[T4] 契约符号 %s 命中 %d 个不同落点（计划内同符号多处安置=漂移前兆）"
                    "→ 不钉 defined_in（绝不静默挑一个），落点请查: %s",
                    name, len(cand), sorted(cand)[:4])
                continue
            path = next(iter(cand))
            if item.get("defined_in") != path:
                item["defined_in"] = path
                pinned += 1
    if tier2_only:
        logger.info(
            "[T4] %d 个契约符号仅装饰前缀弱等价命中文件（tier2 不参与钉死）→ 未钉 defined_in，"
            "worker 对其仍靠事后符号接地: %s", len(tier2_only), tier2_only[:5])
    if full_miss:
        logger.info(
            "[R64-T4] %d 个契约符号在计划内【零 basename 命中】（契约↔file_plan 命名漂移，"
            "源头已在契约 prompt 注入文件清单预防；此留痕=round65 漂移残量观察面）: %s",
            len(full_miss), full_miss[:8])
    return pinned


def detect_contract_classname_divergences(plan) -> list[dict]:
    """round67e Phase 2（类治）：契约类名 file-path 分叉的【结构化】检测——
    reconcile_contract_symbol_paths（确定性对齐）的共用真值源，与 pin_contract_symbol_paths
    的 tier2_only 同一判据原语（basename_symbol_match），绝不造并列副本。

    死型：契约 interfaces/types/dtos 条目 name=X（ScheduleStrategyService）在 plan 内【无 tier0/1
    精确/惯例命中】、却【恰有一个 code 文件 V】按 tier2（装饰前缀，AlarmX endswith X）命中，且【恰一个
    owner 子任务 create 该 V】→ 分叉候选（消费方按契约 import X、只建了 V → L2 cannot find symbol）。
    多命中 tier2 / 多 owner create / 仅 writable（无 create owner）→ 歧义，不返回（reconcile fail-closed）。

    纯结构检测，零磁盘：方向判定/棕地/撞名闸全在 reconcile（单一 blast 边界，检测只报"谁疑似漂"）。
    返回 [{"key","item","symbol":X,"owner":st,"v_path","v_stem"}]。
    """
    sc = getattr(plan, "shared_contract", None)
    subs = getattr(plan, "subtasks", None) or []
    if not isinstance(sc, dict) or not sc or not subs:
        return []
    full_idx = _location_index(plan, include_writable=True)
    if not full_idx:
        return []
    creators: dict[str, list] = {}   # create 落点 → 建它的子任务们（仅 create，rename 的是 create 权）
    for st in subs:
        for p in _scope_paths(st, include_writable=False):
            creators.setdefault(p, []).append(st)
    out: list[dict] = []
    for key in _PIN_KEYS:
        val = sc.get(key)
        if not isinstance(val, list):
            continue
        for item in val:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if len(name) < 3:
                continue
            has_exact = False
            tier2: list[tuple[str, str]] = []      # (stem, path)
            for stem, paths in full_idx.items():
                t = basename_symbol_match(stem, name)
                if t < 0:
                    continue
                if t <= 1:
                    has_exact = True               # 已有精确/惯例落点=正确命名/owned，无事
                    break
                if stem != name:                   # tier2 装饰前缀
                    tier2.extend((stem, p) for p in paths if _is_code_path(p))
            if has_exact or len(tier2) != 1:
                continue                           # 无 tier2 或 多命中歧义 → 不动
            v_stem, v_path = tier2[0]
            owners = creators.get(v_path) or []
            if len(owners) != 1:
                continue                           # 无 create owner / 多 owner create → 歧义
            out.append({"key": key, "item": item, "symbol": name,
                        "owner": owners[0], "v_path": v_path, "v_stem": v_stem})
    # ★round-2 hunter HIGH（Finding A）★：一个物理文件 V 可 tier2 命中【多个】契约名（AlarmScheduleStrategyService
    # 同时命中 ScheduleStrategyService 与 StrategyService，都≥8 字符装饰边界）→ 两 div 共享同一 v_path/owner，
    # 该文件的真名歧义。与"多 owner/多 tier2 候选"同属歧义 → fail-closed 丢弃全部共享 v_path 的 div（绝不挑一个；
    # 否则 reconcile 处理 div1 改名后 div2 空改却仍钉 defined_in=幻影 pin，且 re-detect 也看不到=自掩盖静默死 L2）。
    from collections import Counter as _Counter
    _vc = _Counter(o["v_path"] for o in out)
    return [o for o in out if _vc[o["v_path"]] == 1]


def wire_created_type_references(plan) -> dict[str, list]:
    """②跨子任务类型引用 → producer 路径布进 consumer readable+upstream_artifacts。

    通道 a：已钉契约符号名（pin_contract_symbol_paths 产出的 defined_in）；
    通道 b：跨子任务 create 的唯一 code stem（round63 真死因 AlarmSendLog 不在契约里）。
    依赖边交本 pass 之后的既有 G2（wire_readable_provenance）。
    返回 {"wired": [(sid, path)], "skipped_ambiguous": [...], "skipped_fanout": [...]}。
    """
    res: dict[str, list] = {"wired": [], "skipped_ambiguous": [], "skipped_fanout": []}
    subs = list(getattr(plan, "subtasks", None) or [])
    if len(subs) < 2:
        return res

    # 歧义判定面 = create∪writable（与 pin 一致）：同 stem 在 create 与 writable 各一处
    # 不同落点，同样是漂移，绝不挑一个布线。
    full_idx = _location_index(plan, include_writable=True)
    create_idx = _location_index(plan, include_writable=False)

    # token → 唯一落点。通道 b 先收（create 才有 G2 依赖边语义），通道 a 覆盖/补充
    # （契约符号名可能 ≠ 文件 stem，如 AlarmTaskService ↔ IAlarmTaskService.java）。
    targets: dict[str, str] = {}
    ambiguous_hit: set[str] = set()
    noise_excluded: set[str] = set()   # hunter#3：被噪音表/长度排除的真实跨子任务 stem
    for stem, paths in create_idx.items():
        if len(stem) < _MIN_STEM_LEN or stem.lower() in _STEM_NOISE:
            noise_excluded.add(stem)
            continue
        if len(full_idx.get(stem) or paths) > 1:
            ambiguous_hit.add(stem)
            continue
        targets[stem] = next(iter(paths))
    sc = getattr(plan, "shared_contract", None)
    if isinstance(sc, dict):
        for key in _PIN_KEYS:
            for item in (sc.get(key) or []) if isinstance(sc.get(key), list) else []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                pin = str(item.get("defined_in") or "").strip()
                if pin and len(name) >= _MIN_STEM_LEN and name.lower() not in _STEM_NOISE:
                    targets[name] = pin

    # consumer 语料（原大小写，与 C1 同来源面；加边类动作用区分大小写强判据）
    corpus: dict[str, str] = {}
    own: dict[str, set[str]] = {}
    for st in subs:
        sc_ = getattr(st, "scope", None)
        corpus[st.id] = (
            (getattr(st, "description", "") or "") + " "
            + " ".join(getattr(st, "acceptance_criteria", None) or []) + " "
            + json.dumps(getattr(st, "contract", None) or {}, ensure_ascii=False))
        own[st.id] = {
            _norm_scope_path(str(f)) for f in (
                list(getattr(sc_, "create_files", None) or [])
                + list(getattr(sc_, "writable", None) or [])
                + list(getattr(sc_, "readable", None) or []))}

    fanout_cap = max(8, len(subs) // 3)
    referenced_ambiguous: list[str] = []
    for token in sorted(ambiguous_hit):
        pat = re.compile(r"(?<![0-9A-Za-z_])" + re.escape(token) + r"(?![0-9A-Za-z_])")
        if any(pat.search(txt) for txt in corpus.values()):
            referenced_ambiguous.append(token)
    if referenced_ambiguous:
        res["skipped_ambiguous"] = referenced_ambiguous
        logger.warning(
            "[T4] %d 个被引用的跨子任务类型存在多个不同落点（计划内漂移）→ 跳过布线"
            "（绝不挑任意落点），请查: %s",
            len(referenced_ambiguous), referenced_ambiguous[:5])
    # hunter#3：真实类型撞噪音表/长度阈（如实体就叫 Model/Data）被排除且确有引用时留痕——
    # 宁缺勿滥是设计意图，但"该布没布"要能从日志里查出来，不许全静默。
    referenced_noise = [
        t for t in sorted(noise_excluded)
        if any(re.search(r"(?<![0-9A-Za-z_])" + re.escape(t) + r"(?![0-9A-Za-z_])", txt)
               for txt in corpus.values())]
    if referenced_noise:
        logger.debug(
            "[T4] %d 个跨子任务 stem 被噪音表/长度阈排除但语料确有引用（设计上宁缺勿滥，"
            "不布线；如系真实类型请改名或靠事后符号接地）: %s",
            len(referenced_noise), referenced_noise[:8])

    for token in sorted(targets):
        path = targets[token]
        pat = re.compile(r"(?<![0-9A-Za-z_])" + re.escape(token) + r"(?![0-9A-Za-z_])")
        hits = [st for st in subs
                if path not in own[st.id] and pat.search(corpus[st.id])]
        if not hits:
            continue
        if len(hits) > fanout_cap:
            res["skipped_fanout"].append(token)
            logger.warning(
                "[T4] 类型 %s 被 %d 个子任务语料引用（>帽 %d，疑通用名）→ 跳过布线"
                "（fail-open：worker 仍可靠既有符号接地事后纠错）", token, len(hits), fanout_cap)
            continue
        for st in hits:
            sc_ = st.scope
            sc_.readable = list(sc_.readable or []) + [path]
            ua = list(getattr(sc_, "upstream_artifacts", None) or [])
            if path not in ua:
                sc_.upstream_artifacts = ua + [path]
            own[st.id].add(path)
            res["wired"].append((st.id, path))
    # 复核 R1（MEDIUM CONFIRMED）：契约通道钉的落点可能只在某子任务的 writable（上游脚手架
    # 已建），G2 的 produced_by 只索引 create_files → 布了 readable 但无人给 consumer 加
    # depends_on 修改者的边 = 可能读到改造前的旧文件（seed 闸只拦"缺文件"不拦"旧文件"）。
    # 不在此擅自加边（同文件多 writer 时"producer"无良定义，交 normalize 的写序机制）——
    # 但 fail-open 必须可观测：WARNING 留痕，L1 编译闸兜底。
    _create_paths = {p for ps in create_idx.values() for p in ps}
    _unordered = sorted({p for _sid, p in res["wired"] if p not in _create_paths})
    if _unordered:
        logger.warning(
            "[T4] %d 个已布线落点仅存在于 writable（无 create producer，G2 不加依赖边）→ "
            "consumer 与修改者可能同波并行、读到改造前旧文件（L1 编译闸兜底）: %s",
            len(_unordered), _unordered[:5])
    return res
