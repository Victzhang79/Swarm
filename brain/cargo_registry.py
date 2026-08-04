"""P-H4b：crates.io 依赖版本确定性解析（工程自身清单声明/Cargo.lock → 官方 crates.io API）。

## 为什么必须有这一层（与 pypi/npm_registry 同因，栈中立铺开）

契约层为 cargo 工程注入脚手架时，成员 `Cargo.toml` 的 `[dependencies]` **每条都得带
版本约束**（crates 生态无父级托管；漏写=装成不可复现漂移版，让 worker 自己填=臆造
`tokio = "99"`——R47/R53 同源病）。本模块给出确定性第三条路：**不臆造、不裸装——
工程自身清单/Cargo.lock 已声明的用声明（零网络），否则去 crates.io 解析真实最新稳定版**。
解析不到如实丢弃（调用方须连同验收标准一起丢弃）。离线/查不通 → 丢弃（fail-honest）。

## 内部 crate 不走 crates.io（与 go replace 同型，与 python 不同）

workspace 内部 crate **绝不**去 crates.io 查（同名公网 crate 会被误物化）。与 python 的
「不物化」**不同**：cargo 的 `path = "../rel"` 相对引用按**清单所在目录**解析（确定性，
不是 pip `file:` 的 cwd 语义），所以内部依赖**物化**为 path 依赖——由调用方（driver）
按目录事实生成，本模块只把内部名从第三方解析里摘出来返回。

## 版本约束形态：写最新稳定版字面量（cargo 默认 caret 语义）

cargo 里裸 `"1.38.0"` 就是 caret（`>=1.38.0, <2.0.0`）——写解析出的具体最新稳定版字面量
即得「可复现下限 + 兼容更新」，与 `cargo add` 行为一致。稳定版取 crates.io 响应的
权威字段 `crate.max_stable_version`（**不做本地 semver 排序**——yanked/预发布口径
以 registry 为准）。yanked 版本对新需求不可选（cargo 只给已在 Cargo.lock 里的放行），
本模块生成的是新清单 → yanked 一律不算可用（判「存在」与「可满足」都剔除）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from swarm.brain.dep_http_cache import text_cache_lookup, text_cache_store

logger = logging.getLogger("swarm.brain.cargo_registry")

# 网络超时短而硬：规划期不容许被 registry 拖死；查不通=丢弃，不阻断。
_HTTP_TIMEOUT_S = float(os.getenv("SWARM_CARGO_LOOKUP_TIMEOUT_S", "8"))
_CRATES_API = "https://crates.io/api/v1/crates/{name}"

_http_cache: dict[str, str | None] = {}
# 失败(None)条目的到期时刻（与 _http_cache 平行不并入，见 dep_http_cache 模块 docstring）
_http_neg_until: dict[str, float] = {}

# crate 名合法字符集：ASCII 字母数字 + -/_，首字符必须字母数字（crates.io 硬约束）
_CRATE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_CRATE_INVALID_RUN = re.compile(r"[^A-Za-z0-9_-]+")
# 契约依赖串粗拆：`tokio` / `tokio@1.38` / `tokio[full,macros]@1`（features 从宽认——
# 声明检查从宽、坐标源从严，L10 原则）
_DEP_RE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9_-]*)\s*(?:\[([^\]]*)\])?\s*(?:@\s*([^@]+))?\s*$")
# 单个 comparator 的有界语法：op 可省（=caret），版本段 1~3 段 + 可选 `.*` 通配 + 可选预发布
_COMP_RE = re.compile(
    r"^(>=|<=|>|<|=|~|\^)?\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?(\.\*)?"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?\s*$")


def _lookup_enabled() -> bool:
    """SWARM_CARGO_LOOKUP=0 → 关闭 crates.io 联网解析（单测默认关：绝不让测试依赖网络/
    假绿）。关闭后 = 解析不到 → 如实丢弃（与 npm/go/maven/pypi 四栈 LOOKUP 开关同契约——
    含本地证据层：开关口径是「关闭后=解析不到→如实丢弃」，清单/Cargo.lock 读取同受约束）。
    ★注释里绝不写其它开关的裸词头——env_registry 扫描会把散文词头当成未登记开关★。"""
    return os.getenv("SWARM_CARGO_LOOKUP", "1").strip().lower() not in ("0", "false", "no")


def normalize_crate_name(label: str) -> str:
    """契约模块标签 → crate 名约定（greenfield 新建包的确定性命名，与 _py_module_name 同立场：
    磁盘 [package].name 才是事实来源，归一化只是我们给**新建包**自定的约定）。
    小写 + 非法字符 run 折叠成单个 `-`；折叠结果仍不合法（空/首字符非法）→ 原样返回
    stripped（拿它做判等键，绝不静默改成一个歪曲名）。"""
    s = (label or "").strip().lower()
    if not s:
        return ""
    n = _CRATE_INVALID_RUN.sub("-", s).strip("-_")
    return n if _CRATE_NAME_RE.match(n) else s


def _http_get(url: str) -> str | None:
    """GET 文本；任何失败（离线/超时/404）→ None。TTL 负缓存（P-C2 复核 F-1 同型）。
    crates.io 强制要求 User-Agent（缺失=403，官方 crawler policy）。"""
    if not _lookup_enabled():
        return None
    hit, cached = text_cache_lookup(_http_cache, _http_neg_until, url)
    if hit:
        return cached
    text: str | None = None
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "swarm-cargo-resolver (swarm planner)"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
            if 200 <= getattr(resp, "status", 200) < 300:
                text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        logger.debug("[cargo-registry] GET %s 失败: %s", url, exc)
    text_cache_store(_http_cache, _http_neg_until, url, text)
    return text


def _crate_json(name: str) -> dict | None:
    """crates.io API 响应（None=不可达/不存在/非法——三态在调用方用 versions 集区分）。"""
    text = _http_get(_CRATES_API.format(name=urllib.parse.quote(name)))
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        logger.warning("[cargo-registry] %s 的 crates.io 响应不是合法 JSON（按不可达处理）", name)
        return None
    return data if isinstance(data, dict) else None


def registry_versions(name: str) -> frozenset[str] | None:
    """该 crate 的全部**可用**版本集（**含预发布、剔除 yanked**——yanked 对新需求不可选）；
    None=不可达。空 frozenset=crate 存在但零可用版本（确证）——与「不可达」机读可辨。"""
    data = _crate_json(name)
    if data is None:
        return None
    vers = data.get("versions")
    if not isinstance(vers, list):
        return frozenset()
    return frozenset(
        v["num"] for v in vers
        if isinstance(v, dict) and isinstance(v.get("num"), str) and not v.get("yanked"))


def registry_latest_version(name: str) -> str | None:
    """crates.io 最新稳定版（权威字段 max_stable_version，已排除 yanked/预发布——
    不在本地重排 semver）；查不到（离线/crate 不存在/零稳定版）→ None（调用方如实丢弃）。"""
    data = _crate_json(name)
    if data is None:
        return None
    crate = data.get("crate")
    if not isinstance(crate, dict):
        return None
    v = crate.get("max_stable_version")
    return v.strip() if isinstance(v, str) and v.strip() else None


# ══════════════════════════════════════════════════════════════
# cargo semver 有界求值（★不复用 npm 的★：bare 字面量 npm=精确、cargo=caret，
# 语义正面冲突——「复用单一事实源≠复用消费契约」。与 npm 同立场：超集语法不判，
# 猜语义误杀真依赖比放过幻觉更坏）
# ══════════════════════════════════════════════════════════════

def _parse_version(v: str) -> tuple[tuple[int, int, int], str] | None:
    """`1.2.3[-pre][+build]` → ((major,minor,patch), prerelease)；非法 → None（不猜）。"""
    core, _, _build = (v or "").strip().partition("+")
    core, _, pre = core.partition("-")
    m = re.fullmatch(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", core)
    if not m:
        return None
    nums = (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))
    return nums, pre


def _ver_cmp(a: tuple[tuple[int, int, int], str], b: tuple[tuple[int, int, int], str]) -> int:
    """semver 全序比较（含预发布段，cr R1 #1：只看数字三元组会让 `>1.5.0-alpha` 判不出
    `1.5.0-beta`）。规则：数字三元组先比；相等时【正式版 > 任何预发布】；两边都是预发布
    按段比——数字段 < 字母段、数字段按数值、字母段按字典序、前缀短者小。"""
    an, ap = a
    bn, bp = b
    if an != bn:
        return -1 if an < bn else 1
    if ap == bp:
        return 0
    if not ap:
        return 1                      # 正式版 > 预发布
    if not bp:
        return -1
    asegs, bsegs = ap.split("."), bp.split(".")
    for x, y in zip(asegs, bsegs):
        if x == y:
            continue
        xn, yn = x.isdigit(), y.isdigit()
        if xn and yn:
            return -1 if int(x) < int(y) else 1
        if xn != yn:
            return -1 if xn else 1    # 数字段 < 字母段
        return -1 if x < y else 1
    return -1 if len(asegs) < len(bsegs) else (1 if len(asegs) > len(bsegs) else 0)


def _caret_bounds(major: int, minor: int, patch: int, declared: int) -> tuple[tuple, tuple]:
    """caret（含 bare 字面量）兼容区间 [lower, upper)。上界由**最左非零段**决定，
    只在【已声明】的段位里找（`1.2` → <2.0.0；`0.2.3` → <0.3.0；`0.0.3` → <0.0.4；
    `0.0` → <0.1.0；`0` → <1.0.0）。"""
    lower = (major, minor, patch)
    if major > 0:
        return lower, (major + 1, 0, 0)
    if declared >= 2 and minor > 0:
        return lower, (0, minor + 1, 0)
    if declared >= 3:
        return lower, (0, 0, patch + 1)
    if declared == 2:      # 0.0 → <0.1.0
        return lower, (0, 1, 0)
    return lower, (1, 0, 0)  # 0 → <1.0.0（`*` 走 wildcard_any 臂不到这里）


def _comparator_matches(comp: str, ver: tuple[tuple[int, int, int], str]) -> bool | None:
    """单个 comparator 是否匹配版本 ver（ver 已由调用方按预发布规则过滤）。
    返回 None=语法超出有界集（调用方转「不判」）。
    ★cr R1 #1★ 下界比较必须走 `_ver_cmp`（含预发布段）——只看数字三元组会把
    `>1.5.0-alpha` 对 `1.5.0-beta` 判 False（同三元组）、把 `=1.2.3-alpha` 对
    `1.2.3` 判 True（预发布被无视），两个方向都错。"""
    m = _COMP_RE.match(comp.strip())
    if not m:
        return None
    op, g1, g2, g3, wild, pre = m.groups()
    declared = 1 + (g2 is not None) + (g3 is not None)
    major, minor, patch = int(g1), int(g2 or 0), int(g3 or 0)
    bound_pre = pre or ""
    vnum = ver[0]
    if wild:
        # `1.2.*` = >=1.2.0 <1.3.0；`1.*` = >=1.0.0 <2.0.0（下界天然满足：vnum≥0）。
        # `1.2.3.*` 是非法形态（_range_kind 已拦，防御性 None → 调用方转「不判」）。
        if declared == 2:
            return (major, minor) == vnum[:2]
        if declared == 1:
            return major == vnum[0]
        return None
    if op in (None, "", "^"):
        lo, hi = _caret_bounds(major, minor, patch, declared)
        return _ver_cmp(ver, (lo, bound_pre)) >= 0 and _ver_cmp(ver, (hi, "")) < 0
    if op == "~":
        if declared == 1:
            return major == vnum[0] and _ver_cmp(ver, ((major, 0, 0), bound_pre)) >= 0
        return (vnum[:2] == (major, minor)
                and _ver_cmp(ver, ((major, minor, patch), bound_pre)) >= 0)
    if op == "=":
        if declared == 3:
            return vnum == (major, minor, patch) and ver[1] == bound_pre
        if declared == 2:
            return vnum[:2] == (major, minor)
        return vnum[0] == major
    bound = ((major, minor, patch), bound_pre)
    cmp = _ver_cmp(ver, bound)
    if op == ">=":
        return cmp >= 0
    if op == ">":
        return cmp > 0
    if op == "<=":
        return cmp <= 0
    if op == "<":
        return cmp < 0
    return None


def _range_kind(req: str) -> str:
    """显式版本主张的形态分档（判定序的机读依据）：
      · wildcard_any：`*`（语法合法但不可复现 → 解析成具体稳定版）；
      · protocol：`git:` 等带协议/来源串（不是版本主张 → 不判原样保留）；
      · simple：逗号分隔的 comparator 全在有界语法内 → 可满足性判定；
      · complex：其余（cargo 无 `||`，含 `||`/乱写一律落这档）→ 不判原样保留。
    """
    r = (req or "").strip()
    if not r:
        return "complex"
    if r == "*":
        return "wildcard_any"
    if ":" in r:
        return "protocol"
    comps = [c for c in (p.strip() for p in r.split(",")) if c]
    for c in comps:
        m = _COMP_RE.match(c)
        if not m:
            return "complex"
        if m.group(5) and m.group(4) is not None:
            return "complex"      # `1.2.3.*` 非法形态（通配只能占次版本/补丁位）
    if comps:
        return "simple"
    return "complex"


def _range_is_satisfiable(req: str, versions: frozenset[str]) -> bool:
    """★只在 `_range_kind == "simple"` 时调用★ cargo semver 语义：逗号=AND；
    预发布版本只在【某个 comparator 带同 major.minor.patch 的预发布标签】时才参与
    （`>=1.0.0-alpha` 放进 1.0.0-beta，而 `>=1.0` 不会意外放进 1.0.1-rc.1）。"""
    comps = [c for c in (p.strip() for p in req.split(",")) if c]
    pre_cores: set[tuple[int, int, int]] = set()
    for c in comps:
        m = _COMP_RE.match(c)
        if m and m.group(6):
            pre_cores.add((int(m.group(2)), int(m.group(3) or 0), int(m.group(4) or 0)))
    for v in versions:
        parsed = _parse_version(v)
        if parsed is None:
            continue
        nums, vpre = parsed
        if vpre and nums not in pre_cores:
            continue                      # 预发布：无同核心预发布界 → 不参与
        if all(_comparator_matches(c, parsed) is True for c in comps):
            return True
    return False


def _parse_dep_text(text: str) -> tuple[str, tuple[str, ...], str] | None:
    """契约 cargo 依赖串 → (crate名, features, 显式req|"")
    （`tokio` / `tokio@1.38` / `tokio[full,macros]@^1` 全认——声明检查从宽 L10）；
    解析不出 → None（调用方如实丢弃，绝不硬猜名字）。"""
    m = _DEP_RE.match(text or "")
    if not m:
        return None
    name = m.group(1)
    feats = tuple(f.strip() for f in (m.group(2) or "").split(",") if f.strip())
    req = (m.group(3) or "").strip()
    return name, feats, req


def _dep_value_to_spec(name: str, v):
    """[dependencies] 条目值 → (features, default_features_false, req)。req=None = 跳过
    （path/git/workspace 继承）。

    `{ workspace = true }` 成员条目**跳过**（L10 从宽：不因此拒绝整份清单）——workspace
    继承的版本证据由根 [workspace.dependencies] 直接收集覆盖（harness P-H4b-j 实证：
    成员侧反解臂删掉后测试全绿=冗余防御，根先收让反解结果在 specs 里永不可达）。
    成员级 features 增量是【那个成员】的声明，不进工程级证据。path/git 表不是
    crates.io 版本主张 → 跳过（内部/path 由 driver 自己物化）。
    ★`default-features = false` 必须保留★（hunter R1 F-2：静默丢=重新打开默认特性，
    编译产物/传递依赖全变——换语义不是换写法，与 features 同档）；`optional = true`
    是成员级 feature 接线（隐含特性定义随成员走）——跳过的是 optional **标志本身**，
    版本仍是合法证据（cr R2 对话定案：同一 crate 在别的成员 optional ≠ 新模块不该
    普通依赖它——契约要了它就该写成普通依赖；若连版本证据也跳过，bare 名只会落到
    lock/registry 臂照样被写成普通依赖，reviewer 设想的「不打开」两个臂都达不成，
    反而丢掉「本工程真编译过的版本」这层证据）。
    """
    if isinstance(v, str):
        return (), False, v.strip()
    if not isinstance(v, dict):
        return (), False, None
    if v.get("workspace") is True:
        return (), False, None
    if "path" in v or "git" in v:
        return (), False, None
    req = v.get("version")
    feats = v.get("features")
    feat_t = tuple(f for f in feats if isinstance(f, str)) if isinstance(feats, list) else ()
    no_default = v.get("default-features") is False
    if isinstance(req, str) and req.strip():
        return feat_t, no_default, req.strip()
    return (), False, None


def project_manifest_specs(project_path: str | None) -> dict[str, tuple[tuple[str, ...], bool, str]]:
    """工程自身清单声明 {crate名: (features, default_features_false, req)}（零网络中间
    证据层，P-H3 平移）。

    证据源：根 Cargo.toml 的 [dependencies] → 一级子目录 Cargo.toml 的 [dependencies]
    （按目录名排序）→ 根 [workspace.dependencies]（**最后**——继承默认值，真实声明
    优先，cr R1 #2）。`{workspace=true}` 一律跳过（继承证据=ws 段本身，成员侧反解臂
    已被 harness 实证为冗余后删除）。req 原文采用（工程是自身依赖的权威声明者）；
    features 与 default-features=false 保留（静默丢=换语义）。同名冲突=先见先收 +
    WARNING（hunter R1 F-4：iterdir 原生顺序不定 ⇒ 跨机器 plan 不稳定，故子目录
    按名排序）。根包 [dependencies] 排在成员之前是刻意的确定性选择（hunter R2 观察
    登记：workspace 根同时是 [package] 时「成员声明更近」也说得通，但两个方向都是
    真实声明，判定成本远超收益——确定性+冲突 WARNING 即足够）。与网络查询同受
    LOOKUP 门控（开关契约「关闭后=解析不到→如实丢弃」，与 pypi project_manifest_specs
    同形）。

    ★诚实边界（cr R1 #4，登记不改）★ 子目录扫描不按 workspace.members 过滤——
    examples/vendor 等非成员目录的 Cargo.toml 声明也会进证据层。方向是保守的：
    最坏情况=版本证据来自仓内真实存在过的声明（绝不臆造），且 members 支持 glob
    （`crates/*`）与 exclude，判定成本远超收益。
    """
    if not project_path or not _lookup_enabled():
        return {}
    root = Path(project_path)
    try:
        if not root.is_dir():
            return {}
    except OSError:
        return {}
    specs: dict[str, tuple[tuple[str, ...], bool, str]] = {}

    def _read_toml(ct: Path):
        try:
            import tomllib
            return tomllib.loads(ct.read_text("utf-8", errors="replace"))
        except (OSError, ValueError) as exc:
            # 硬检查④：解析失败 ≠ 没有声明
            logger.warning("[cargo-registry] %s 解析失败（%s），该清单声明证据缺席", ct, exc)
            return None

    def _collect(deps, *, origin: str) -> None:
        if not isinstance(deps, dict):
            return
        for n, v in deps.items():
            feat, no_default, req = _dep_value_to_spec(n, v)
            if req is None:
                continue
            if n in specs:
                if specs[n] != (feat, no_default, req):
                    # 确定性（根优先+子目录名序）但不同声明被盖必须留痕（硬检查④）
                    logger.warning("[cargo-registry] %s 对 %s 的声明 %r 与已收录的 %r 冲突"
                                   " → 保留先收者（根优先/名字序），后者被盖",
                                   origin, n, (feat, no_default, req), specs[n])
                continue
            specs[n] = (feat, no_default, req)

    root_data = _read_toml(root / "Cargo.toml") if (root / "Cargo.toml").is_file() else None
    root_ws: dict = {}
    if isinstance(root_data, dict):
        ws = root_data.get("workspace") or {}
        if isinstance(ws, dict) and isinstance(ws.get("dependencies"), dict):
            root_ws = ws["dependencies"]
    # ★优先级（cr R1 #2）：真实声明 > 继承默认值★ 根 [dependencies] → 成员
    # [dependencies]（名字序）→ 根 [workspace.dependencies] **最后**。workspace 段是
    # 继承默认（成员可用自己的声明覆盖），把它先收会让「成员的 serde="1.0"」被
    # 「workspace 默认 serde="2.0"」盖住=优先级倒置。成员 `{workspace=true}` 一律跳过
    # （继承证据=ws 段本身；成员侧反解臂已被 harness 实证为冗余后删除）。
    if isinstance(root_data, dict):
        _collect(root_data.get("dependencies"), origin="根[dependencies]")
    try:
        # iterdir 而非 glob：py3.13 实测 Path.glob 静默吞 OSError 返 []（P-H3 教训）——
        # 「枚举失败」必须与「真没有」可辨。★sorted★：原生顺序跨机器/进程不定，
        # 同名冲突时「先见先收」会把顺序不确定性变成 plan 不确定性（hunter R1 F-4）。
        subs = sorted((e for e in root.iterdir() if e.is_dir() and not e.name.startswith(".")),
                      key=lambda e: e.name)
    except OSError as exc:
        logger.warning("[cargo-registry] %s 一级子目录枚举失败（%s），清单证据可能不完整",
                       root, exc)
        return specs
    for e in subs:
        if e.name in ("target", "node_modules", ".git"):
            continue
        if not (e / "Cargo.toml").is_file():
            continue
        sub_data = _read_toml(e / "Cargo.toml")
        if isinstance(sub_data, dict):
            _collect(sub_data.get("dependencies"), origin=f"{e.name}/Cargo.toml")
    _collect(root_ws, origin="根[workspace.dependencies]")   # 继承默认兜底，最后收
    return specs


def cargo_lock_versions(project_path: str | None) -> dict[str, str]:
    """Cargo.lock 的 {crate名: 版本}（零网络证据层：「曾经真装上过的版本」，与 npm 的
    node_modules 臂同型）。lock 缺失 → {}（缺席如实，不告警——lock 本就可选）；
    解析失败 → WARNING + {}（「坏了」与「没有」机读可辨）。

    ★多版本共存（lock 常态：`bitflags 1.x` 与 `2.x` 并存）★ 取【最高稳定版】（semver
    排序，确定性——与文件出现顺序无关）+ WARNING（hunter R1 F-1：静默取先见者=把
    lock 排序巧合当语义）；全是预发布 → 放弃该条证据（fail-honest，交下游 registry）。
    """
    if not project_path or not _lookup_enabled():
        return {}
    lk = Path(project_path) / "Cargo.lock"
    if not lk.is_file():
        return {}
    try:
        import tomllib
        data = tomllib.loads(lk.read_text("utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        logger.warning("[cargo-registry] %s 解析失败（%s），lock 证据缺席", lk, exc)
        return {}
    by_name: dict[str, list[str]] = {}
    for pkg in (data.get("package") or []):
        if isinstance(pkg, dict) and isinstance(pkg.get("name"), str) \
                and isinstance(pkg.get("version"), str):
            by_name.setdefault(pkg["name"], [])
            if pkg["version"] not in by_name[pkg["name"]]:
                by_name[pkg["name"]].append(pkg["version"])
    out: dict[str, str] = {}
    for name, vers in by_name.items():
        if len(vers) == 1:
            out[name] = vers[0]
            continue
        parsed = [(p, v) for v in vers if (p := _parse_version(v)) is not None]
        stable = [(nums, v) for (nums, pre), v in parsed if not pre]
        if not stable:
            logger.warning("[cargo-registry] %s 在 Cargo.lock 有 %d 个版本且全是预发布"
                           " → 放弃该条 lock 证据（fail-honest，交下游 registry）: %s",
                           name, len(vers), vers)
            continue
        best = max(stable, key=lambda t: t[0])
        logger.warning("[cargo-registry] %s 在 Cargo.lock 有 %d 个版本共存 %s → 取最高"
                       "稳定版 %s（确定性选择，与 lock 文件顺序无关）",
                       name, len(vers), sorted(vers), best[1])
        out[name] = best[1]
    return out


@dataclass
class ResolvedCargoDep:
    """一个已解析的 cargo 依赖（spec=写入 [dependencies] 的版本约束原文）。"""
    name: str          # crate 名（磁盘 [package].name 或契约 crate 名）
    spec: str          # "1.38.0"（裸字面量=caret）/ "=1.2.3" / ">=1.2, <2" 原文
    source: str        # project_manifest / cargo_lock / registry / explicit
    verified: str = "verified"   # ≠verified 会被 dep_versions_unverified 账收编（P-C2 F-2）
    features: tuple[str, ...] = field(default_factory=tuple)  # 静默丢=换语义（extras 同律）
    default_features: bool = True   # False=清单声明了 default-features=false（hunter R1
    # F-2：重新打开默认特性=换语义；只有工程清单证据层能携带它，契约串无法表达）


def resolve_cargo_deps(
    specs: list[str],
    internal_modules: set[str] | None = None,
    project_path: str | None = None,
) -> tuple[list[ResolvedCargoDep], list[str], list[str]]:
    """把契约 cargo 依赖（裸名 / name@req / name[features]@req）解析成可写进
    [dependencies] 的 (name, spec, features)。

    返回 (kept, internal, dropped)。**dropped 必须同时从契约/验收标准剔除**（R53 家族病）。
    判定序（每步都有权威证据，无一步靠猜）：
      1. 内部 crate（名 ∈ internal_modules）→ 入 internal 返回（driver 物化 path 依赖；
         绝不送 crates.io 误解析同名公网 crate）。
      2. 显式 `name@req` → **按 P-C2 验证后**采用（显式版本是待验证的主张，绝非证据）：
         · `*` → 解析成具体最新稳定版（不可复现是模块要治的病）；
         · 协议/来源串、复合乱写 → **不判**，原样保留（猜语义误杀比放过幻觉更坏）；
         · 简单区间 → 取全量可用版本集判可满足性：不可满足 → 校正到最新稳定版/如实丢弃；
           registry 不可达 → **fail-open 保留**（R56-6 证据缺失≠否定证据）。
      3. 裸名 → 工程自身清单声明（含 workspace 反解，零网络）→ Cargo.lock 版本
         → crates.io 最新稳定版 → 写字面量（caret 语义）。查不到 → drop。
    """
    internal = set(internal_modules or set())
    kept: list[ResolvedCargoDep] = []
    internal_hit: list[str] = []
    dropped: list[str] = []
    seen: set[str] = set()
    _manifest_specs: dict | None = None
    _lock_vers: dict | None = None

    for raw in specs:
        parsed = _parse_dep_text(raw)
        if not parsed:
            dropped.append(str(raw))
            continue
        name, feats, req = parsed
        if not name or name in seen:
            continue
        if name in internal:
            seen.add(name)
            internal_hit.append(name)
            continue
        if req:
            kind = _range_kind(req)
            if kind == "wildcard_any":
                # `*` ＝语法合法但**不可复现**（npm dist-tag 同型）。解析成具体最新稳定版；
                # 解析不到则保留原样（不可达时丢弃才是误杀，fail-open 留痕）。
                lv = registry_latest_version(name)
                seen.add(name)
                if lv:
                    logger.warning("[cargo-registry] P-C2 %s@* 不可复现 → 解析到具体最新"
                                   "稳定版 %s（LLM 声明非证据）", name, lv)
                    kept.append(ResolvedCargoDep(name=name, spec=lv, source="registry",
                                                 features=feats))
                else:
                    logger.warning("[cargo-registry] P-C2 %s@* 不可复现但 registry 不可达 → "
                                   "fail-open 保留原样（止于 WARNING）", name)
                    kept.append(ResolvedCargoDep(name=name, spec=req, source="explicit",
                                                 verified="unverified", features=feats))
                continue
            if kind in ("protocol", "complex"):
                # 不判（Maven `${...}` 的对应物）：协议/来源不是版本主张；超集语法猜语义
                # 的风险是误杀真依赖，比放过一个幻觉更坏（fail-open 但留痕）。
                logger.info("[cargo-registry] P-C2 %s@%s 属【%s】形态，不做可满足性判定"
                            "（保留原样，止于 WARNING）", name, req, kind)
                seen.add(name)
                kept.append(ResolvedCargoDep(name=name, spec=req, source="explicit",
                                             verified="unjudgeable", features=feats))
                continue
            versions = registry_versions(name)
            if versions is None:
                # 不可达 → fail-open 保留（离线跑一次就清空所有显式依赖=比原 bug 更坏）
                seen.add(name)
                kept.append(ResolvedCargoDep(name=name, spec=req, source="explicit",
                                             verified="registry_unreachable", features=feats))
                continue
            if _range_is_satisfiable(req, versions):
                seen.add(name)
                kept.append(ResolvedCargoDep(name=name, spec=req, source="explicit",
                                             features=feats))
                continue
            # 确证无可满足版本=幻觉 → 有最新稳定版则校正，无则如实丢弃（npm 臂同律）
            latest = registry_latest_version(name)
            if latest:
                logger.warning("[cargo-registry] 契约显式依赖 %s@%s 无任何可满足版本"
                               "（crates.io 全量可用版本集含预发布、剔除 yanked 确证）→ "
                               "校正到最新稳定版 %s（LLM 声明非证据）", name, req, latest)
                seen.add(name)
                kept.append(ResolvedCargoDep(name=name, spec=latest, source="registry",
                                             features=feats))
            else:
                dropped.append(str(raw))
                logger.warning("[cargo-registry] 契约显式依赖 %s@%s 无任何可满足版本且"
                               "无可用稳定版 → 如实丢弃（绝不逼 worker 手写幻觉版本）",
                               name, req)
            continue
        # 裸名：工程清单声明（含 workspace 反解）→ Cargo.lock → crates.io 最新稳定版
        if _manifest_specs is None:
            _manifest_specs = project_manifest_specs(project_path)
        declared = _manifest_specs.get(name)
        if declared is not None:
            d_feats, d_no_default, d_req = declared
            seen.add(name)
            kept.append(ResolvedCargoDep(name=name, spec=d_req, source="project_manifest",
                                         features=feats or d_feats,
                                         default_features=not d_no_default))
            continue
        if _lock_vers is None:
            _lock_vers = cargo_lock_versions(project_path)
        locked = _lock_vers.get(name)
        if locked:
            # lock 是「曾经真装上过的版本」证据（node_modules 臂同型）——写字面量=caret，
            # 允许兼容更新（与 cargo 对 lock 版求兼容更新的行为一致）。
            seen.add(name)
            kept.append(ResolvedCargoDep(name=name, spec=locked, source="cargo_lock",
                                         features=feats))
            continue
        latest = registry_latest_version(name)
        if latest:
            seen.add(name)
            kept.append(ResolvedCargoDep(name=name, spec=latest, source="registry",
                                         features=feats))
            continue
        dropped.append(str(raw))
    return kept, internal_hit, dropped
