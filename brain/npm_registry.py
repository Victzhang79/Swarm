"""#31-Phase2b：npm 依赖版本确定性解析（本地 node_modules → 官方 registry → 镜像）。

## 为什么必须有这一层（与 maven_registry 同因，栈中立铺开）

契约层（大脑）为 npm 工程注入脚手架时，会自由引入第三方 npm 包（axios / lodash /
express …）。package.json 的 dependencies **必须带版本区间**（npm 无 Maven 那种父级
dependencyManagement 统一托管——每个第三方包都得自己写 `^x.y.z`，漏写=`npm install`
装不上/装成不可复现的漂移版）。若脚手架把版本省了或让 worker 自己填，小模型要么臆造一个
不存在的版本（`^99.0.0`）要么写 `latest`（不可复现），与 R47/R53 的病同源。

本模块给出确定性第三条路：**不臆造、不 latest——去权威 registry 解析真实最新稳定版**。
解析不到就如实丢弃（调用方须连同验收标准一起丢弃，杜绝"模板没有、验收却要求"逼 worker
造假的矛盾）。离线/查不通 → 丢弃（fail-honest：宁可缺一个可归因可补的依赖，绝不写死一个
装不上的版本让 `npm install` 整体失败连坐全模块）。

## 内部（workspace）包不走 registry

monorepo 内部包（同 workspace 的兄弟 package）用 `workspace:*` 协议引用，**绝不**去
registry 查版本（它们根本不在 registry 上）。内部包名由调用方从兄弟 package.json 的
`name` 字段读出（事实来源）传入 internal_names；据此把内部/第三方分流。

## 版本区间用 `^`（caret）

npm 生态默认 caret（`^1.2.3` = 允许兼容更新，锁大版本），与 `npm init` 行为一致。
解析出的是**具体最新稳定版**，加 `^` 前缀写进 package.json（可复现下限 + 生态惯例上限）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from swarm.brain.dep_http_cache import text_cache_lookup, text_cache_store

logger = logging.getLogger("swarm.brain.npm_registry")

# 网络超时短而硬：规划期不容许被 registry 拖死；查不通=丢弃，不阻断。
_HTTP_TIMEOUT_S = float(os.getenv("SWARM_NPM_LOOKUP_TIMEOUT_S", "8"))
# 官方 registry 优先，npmmirror（淘宝镜像）兜底（国内可达性，与 maven aliyun 镜像对称）。
_REGISTRY_MIRRORS = (
    "https://registry.npmjs.org/{pkg}",
    "https://registry.npmmirror.com/{pkg}",
)

# semver 预发布：注入依赖必须落在稳定版（`1.2.3-beta.1` / `-rc.0` / `-next.5` 会把下游
# 拖进不可复现的坑）。稳定版 = 主体 `x.y.z` 之后无 `-<prerelease>` 段。
_PRERELEASE = re.compile(r"-(?:alpha|beta|rc|next|canary|dev|pre|snapshot|nightly|experimental)",
                         re.IGNORECASE)
# 语义化版本主体（允许 `x`、`x.y`、`x.y.z`，忽略 build 元数据 `+…`）。
_SEMVER_CORE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?")

_http_cache: dict[str, str | None] = {}
# ★P-C2 复核 F-1★ `_http_cache` 里 `None` 条目的到期时刻（与它平行，不并入——那个 dict 的
# 值形状是承重契约，见 brain/dep_http_cache.py 模块 docstring）。
_http_neg_until: dict[str, float] = {}


def _lookup_enabled() -> bool:
    """SWARM_NPM_LOOKUP=0 → 关闭 registry 联网解析（单测默认关：绝不让测试依赖网络/被
    registry 拖慢，也杜绝"网络好就绿、离线就红"的假绿）。关闭后 = 解析不到 → 如实丢弃。"""
    return os.getenv("SWARM_NPM_LOOKUP", "1").strip().lower() not in ("0", "false", "no")


def _http_get(url: str) -> str | None:
    """GET 文本；任何失败（离线/超时/404）→ None。结果缓存（规划期同一包会被多模块问到）。"""
    if not _lookup_enabled():
        return None
    # ★P-C2 复核 F-1（go/npm/maven 三处同型，一起改）★ 原实现永久缓存 `None`：一次抖动
    # 把该包钉成"查不到"，网络恢复后不重试。npm 侧后果最隐蔽——fail-open 是设计好的正确
    # 行为、日志逐字相同，于是同一个幻觉版本在后续所有任务里都免检通过。
    # 策略（TTL 负缓存，兼顾 F-3 的代价放大）见 brain/dep_http_cache.py。
    hit, cached = text_cache_lookup(_http_cache, _http_neg_until, url)
    if hit:
        return cached
    text: str | None = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "swarm-npm-resolver"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
            if 200 <= getattr(resp, "status", 200) < 300:
                text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        logger.debug("[npm-registry] GET %s 失败: %s", url, exc)
    text_cache_store(_http_cache, _http_neg_until, url, text)
    return text


def _is_stable(version: str) -> bool:
    return bool(version) and not _PRERELEASE.search(version) and "-" not in version.split("+")[0]


def _ver_key(v: str) -> tuple:
    m = _SEMVER_CORE.match(v.strip())
    if not m:
        return (0, 0, 0)
    return tuple(int(g) if g and g.isdigit() else 0 for g in m.groups())


def _encode_pkg(pkg: str) -> str:
    """registry URL 路径编码：scoped 包 `@scope/name` 的 `/` 必须转义成 `%2f`。"""
    return urllib.parse.quote(pkg, safe="@")


# ── 本地证据（零网络） ──────────────────────────────────────────────────────
def local_node_modules_version(project_path: str, pkg: str) -> str | None:
    """本地 node_modules 里**已安装**的该包版本（package.json version = 确定能装的最强证据，
    零网络）。规划期联网若抖动/被墙，本地已装版本比 registry 最新版更保险（不引入未下载版本）。

    与网络查询同受 SWARM_NPM_LOOKUP 开关约束，保证单测确定性。"""
    if not _lookup_enabled() or not project_path:
        return None
    # scoped 包 `@scope/name` 在 node_modules 下就是 `@scope/name/` 子目录，Path 天然处理。
    pj = Path(project_path) / "node_modules" / pkg / "package.json"
    try:
        if not pj.is_file():
            return None
        data = json.loads(pj.read_text("utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    ver = data.get("version") if isinstance(data, dict) else None
    return ver if isinstance(ver, str) and _is_stable(ver) else None


def registry_latest_version(pkg: str, project_path: str | None = None) -> str | None:
    """版本解析：本地 node_modules（确定能装）→ registry dist-tags.latest（过滤预发布，
    非稳定则回退全量 versions 里的最大稳定版）→ 镜像兜底。查不到 → None（绝不臆造/latest）。"""
    if project_path:
        local = local_node_modules_version(project_path, pkg)
        if local:
            return local
    if not _lookup_enabled():
        return None
    encoded = _encode_pkg(pkg)
    for tpl in _REGISTRY_MIRRORS:
        raw = _http_get(tpl.format(pkg=encoded))
        if not raw:
            continue
        try:
            doc = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(doc, dict):
            continue
        # 首选 dist-tags.latest（npm 官方"最新稳定"指针）——但仍防御性过滤预发布
        # （私服/镜像上偶有把 latest 指到 prerelease 的脏数据）。
        latest = ((doc.get("dist-tags") or {}) if isinstance(doc.get("dist-tags"), dict)
                  else {}).get("latest")
        if isinstance(latest, str) and _is_stable(latest):
            return latest
        # latest 缺失/非稳定 → 从全量 versions 取最大稳定版。
        versions = doc.get("versions")
        if isinstance(versions, dict):
            stable = [v for v in versions if isinstance(v, str) and _is_stable(v)]
            if stable:
                return max(stable, key=_ver_key)
    return None


def registry_all_versions(pkg: str) -> frozenset[str] | None:
    """该包 registry 上**全部**已发布版本号（含预发布）。

    ★返回值三态，调用方必须分辨（P-C2 的核心）★
      · `frozenset(...)` 非空 —— 拿到权威版本集，可据此判"某区间是否可满足"；
      · `None` —— registry **不可达**（离线/超时/包不存在都归此），**证据缺失≠否定证据**
        （R56-6）⇒ 调用方必须 fail-open 保留 LLM 主张 + 留痕，绝不据此判幻觉；
      · `frozenset()` 空集 —— 拿到文档但 `versions` 字段为空/畸形，同样按不可达处置
        （已在下方归一成 None，不让"空"与"真没有"塌成一个值）。

    与 `registry_latest_version` 刻意分开：那个答"该写哪个版本"（只要稳定版），本函数答
    "这个版本存在过吗"（**必须含预发布**——LLM 写 `1.2.3-rc.1` 时它确实存在，不该判幻觉；
    我们不主动注入预发布是另一档决定，见模块 docstring）。
    """
    if not _lookup_enabled():
        return None
    encoded = _encode_pkg(pkg)
    for tpl in _REGISTRY_MIRRORS:
        raw = _http_get(tpl.format(pkg=encoded))
        if not raw:
            continue
        try:
            doc = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(doc, dict):
            continue
        versions = doc.get("versions")
        if isinstance(versions, dict) and versions:
            return frozenset(v for v in versions if isinstance(v, str) and v)
    return None


# ── 显式区间的可满足性判定（P-C2：R67L-B3 口径平移） ────────────────────────
# ★不判的形态（Maven 侧 `${...}` 属性引用的对应物）★ 这些不是"版本主张"，是**协议/别名**，
# 拿版本集去判它们只会误杀：workspace 协议、本地路径、git/tarball URL、npm 别名、
# GitHub 简写、Yarn 的 portal/patch 协议。
_NON_REGISTRY_PROTOCOLS = ("workspace:", "file:", "link:", "git+", "git:", "http://",
                           "https://", "npm:", "github:", "portal:", "patch:")
# dist-tag 与通配：语法合法但**不可复现**（模块 docstring 明列 `latest` 为要治的病之一）。
_DIST_TAGS = ("latest", "next", "*", "x", "canary", "beta", "alpha", "rc")
# 我们**有能力**判定的简单区间形态。复合区间（`||`、`<`、连字符区间、空格并列）交给
# "不判"分支——无 semver 库时猜语义的风险是**误杀真依赖**，比放过一个幻觉更坏。
_SIMPLE_RANGE = re.compile(r"^(\^|~|>=|=|>|v)?\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?"
                           r"(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")


def _range_kind(spec: str) -> str:
    """显式 spec 的可判性分类 → `protocol` | `dist_tag` | `simple` | `complex`。"""
    s = (spec or "").strip()
    if not s:
        return "dist_tag"
    low = s.lower()
    if low.startswith(_NON_REGISTRY_PROTOCOLS):
        return "protocol"
    if low in _DIST_TAGS:
        return "dist_tag"
    return "simple" if _SIMPLE_RANGE.match(s) else "complex"


def _range_is_satisfiable(spec: str, versions: frozenset[str]) -> bool:
    """简单区间 `spec` 是否被 `versions` 里任一已发布版本满足。

    ★只在 `_range_kind == "simple"` 时调用★ 语义按 npm semver：
      · 无前缀 / `=` / `v` → **精确**匹配（`1.2.3` 必须真有 1.2.3）；缺位补零式前缀匹配
        （`1.2` 视作 `1.2.x`，`1` 视作 `1.x` —— npm 对 `1.2` 的语义正是 `>=1.2.0 <1.3.0`）；
      · `^` → 同 major 且 ≥ floor；**major 0 特例**：`^0.2.3` 是 `>=0.2.3 <0.3.0`（semver
        规定 0.x 的次版本视作破坏性），故要求同 major.minor；`^0.0.3` 要求精确 0.0.3；
        ★缺位＝通配（R1）★ `^0` = `<1.0.0`（任意 0.x.y）、`^0.0` = `<0.1.0`——上界由**已声明**
        段位里的最左非零段决定，未声明的段位不施加相等约束；
      · `~` → 同 major.minor 且 ≥ floor（`~1.2` = `>=1.2.0 <1.3.0`）；★`~1` 只锁 major★
        （npm 语义等同 `^1`），别拿补出来的 0 当 minor 约束（R1）；
      · `>=` / `>` → ≥（>）floor 即可。

    ★诚实边界：预发布尾段不参与比较★ 复用 `_SEMVER_CORE` 只取三元组，故 `1.2.3-rc.1` 与
    `1.2.3` 在本函数眼里同值。三种越界都朝**判可满足**（不判幻觉）倾斜：LLM 写
    `1.2.3-rc.1` 而仓库只有 `1.2.3`、写 `^1.2.3` 而仓库只有 `1.2.4-rc.1`（npm 的 caret
    实际不匹配预发布）等。**方向是刻意的**——本函数的返回值只用来"判是否幻觉"，假阴性
    （漏判一个幻觉）由执行期 L1 dep-legality 兜底，假阳性（误杀真依赖）无人兜底。真要收紧
    得引入完整 semver 比较，那是另一档决定，别顺手把这里改严。
    """
    m = _SIMPLE_RANGE.match((spec or "").strip())
    if not m:
        return True                      # 防御：非简单形态不该走到这里，宁可放行不误杀
    op = (m.group(1) or "").strip()
    maj, mnr, pat = m.group(2), m.group(3), m.group(4)
    floor = (int(maj), int(mnr or 0), int(pat or 0))
    # ★P-C2 复核 R1★ `prec` = **声明了几段**（1/2/3）。npm semver 里缺位是**通配**（X-range），
    # 不是 0：`~18` = `>=18.0.0 <19.0.0`，`^0` = `>=0.0.0 <1.0.0`。上面 `floor` 的补零作为
    # **下界**是对的，但绝不能当成对**未声明段位**的相等约束——原实现的 `^`/`~` 两臂正是这么用的
    # （`~18` 要求 `cur[1] == 0` ⇒ `18.3.1` 判 False）。`""`/`=` 臂早就按 `pat is not None` /
    # `mnr is not None` 分了档，`^`/`~` 漏了，**同函数内两种口径**。
    # 后果比"丢弃"重：判 False ⇒ 走"确证幻觉"分支 ⇒ 校正成 latest ⇒ `express@~4` 变 `^5.1.0`
    # ⇒ npm install 成功但 worker 按 4.x API 写码＝静默跨大版本漂移。
    prec = 3 if pat is not None else (2 if mnr is not None else 1)
    for v in versions:
        vm = _SEMVER_CORE.match(v.strip())
        if not vm:
            continue
        cur = tuple(int(g) if g and g.isdigit() else 0 for g in vm.groups())
        if op in ("", "=", "v"):
            # 缺位 = 前缀区间；写全 x.y.z = 精确（按 semver 三元组比，忽略 build 元数据 `+…`）
            if pat is not None:
                if cur == floor:                       # `1.2.3` = 精确
                    return True
            elif mnr is not None:
                if cur[0] == floor[0] and cur[1] == floor[1]:   # `1.2` = `1.2.x`
                    return True
            elif cur[0] == floor[0]:                   # `1` = `1.x`
                return True
        elif op == "^":
            # caret 的上界由**最左非零段**决定，而"最左非零"只在**已声明**的段位里找（R1）。
            if prec == 1:
                if cur[0] == floor[0]:                     # `^1`=`1.x` / `^0`=`0.x`（<1.0.0）
                    return True
            elif floor[0] == 0:
                # 0.x：次版本即破坏性；0.0.z 更严（精确）
                if floor[1] == 0:
                    if prec == 2:
                        if cur[0] == 0 and cur[1] == 0:    # `^0.0` = `>=0.0.0 <0.1.0`
                            return True
                    elif cur == floor:                     # `^0.0.3` = 精确
                        return True
                elif cur[0] == 0 and cur[1] == floor[1] and cur >= floor:
                    return True
            elif cur[0] == floor[0] and cur >= floor:
                return True
        elif op == "~":
            # `~1` 退化为同 major（npm 语义等同 `^1`）；写了两段以上才锁 major.minor。
            if prec == 1:
                if cur[0] == floor[0]:
                    return True
            elif cur[0] == floor[0] and cur[1] == floor[1] and cur >= floor:
                return True
        elif op == ">=":
            # `>=1` ≡ `>=1.0.0`：补零的 floor 正是下界本身，缺位无需特殊处理。
            if cur >= floor:
                return True
        elif op == ">":
            # ★P-C2 复核 F-4★ 缺位时 `>` **不能**拿补零的 floor 当下界。npm 官方原文：
            # "The comparator `>1` is equivalent to `>=2.0.0`"，并明列 `1.0.1`/`1.1.0`
            # 为不匹配。原实现 `cur > floor` 把 `>1` 当成 `>1.0.0` ⇒ `1.0.1` 判 True（错）。
            # 语义：`>` 作用于**整个已声明的 X-range**，即"超过该 range 的上界"⇒ 下界 =
            # 最右已声明段 +1、更细的段归零。`>1`→`>=2.0.0`、`>1.2`→`>=1.3.0`。
            # ★R1 收了 `^`/`~` 却漏了这一臂——同函数内第三种口径（commit b194e79 自伤）★
            # 诚实边界：`>1` ≡ `>=2.0.0` 是官方**明文**；`>1.2`→`>=1.3.0` 官方无明文，
            # 是按 desugaring 表（`1.2 := >=1.2.0 <1.3.0-0`）与 `>1` 同规则推出的。
            if prec == 1:
                if cur >= (floor[0] + 1, 0, 0):
                    return True
            elif prec == 2:
                if cur >= (floor[0], floor[1] + 1, 0):
                    return True
            elif cur > floor:
                return True
    return False


# ── 对外主入口 ──────────────────────────────────────────────────────────────
@dataclass
class ResolvedNpmDep:
    name: str
    spec: str      # 写入 package.json 的版本区间：内部=workspace:* / 第三方=^x.y.z
    source: str    # workspace | local | registry | explicit
    # ★P-C2 复核 F-2★ P-C2 闸对这一条**实际做到了什么**。三种结局原先全塌成
    # `source="explicit"`：① 探测确证存在、② registry 不可达 fail-open 保留（闸没起作用）、
    # ③ dist-tag 解析不到/协议·复合区间不判。塌成一个值的后果不是"标签不好看"——
    # 国内环境 registry/proxy 常不可达时闸会**整轮静默失效**，而交付物与闸正常时逐字相同。
    # ★为什么不改 `source` 的取值而另开字段★ `source` 的既有消费者（模板渲染、15 处测试断言）
    # 要的是"版本从哪来"，与"验没验过"是两个问题；复用单一事实源 ≠ 复用其消费契约，
    # 后果不同必须分档（血规 10 第三条）。
    #   verified   —— 有确定性证据（registry 版本集可满足 / 解析自 registry / 本地 node_modules）
    #   unverified —— 证据不完整，fail-open 保留了 LLM 主张（执行期 L1 兜底）
    #   unjudgeable—— 刻意不判（协议/别名/复合区间；判它们的风险是误杀）
    verified: str = "verified"


def _split_name_range(raw: str) -> tuple[str, str | None]:
    """把 `axios` / `axios@^1.6.0` / `@scope/pkg@1.2.3` 拆成 (name, explicit_range|None)。
    scoped 包首字符 `@` 不算分隔符——只认包名之后的 `@`。"""
    s = str(raw).strip()
    if not s:
        return "", None
    scoped = s.startswith("@")
    body = s[1:] if scoped else s
    if "@" in body:
        name_part, _, ver = body.partition("@")
        name = ("@" + name_part) if scoped else name_part
        return name.strip(), (ver.strip() or None)
    return s, None


def resolve_npm_deps(project_path: str | None, specs: list[str],
                     internal_names: set[str] | None = None,
                     ) -> tuple[list[ResolvedNpmDep], list[str]]:
    """把契约 npm 依赖（裸名或 name@range）解析成可写入 package.json 的 (name, range)。

    返回 (kept, dropped)。**dropped 必须同时从契约/验收标准剔除**——否则又造出"模板没有、
    验收却要求"的矛盾，逼 worker 手写臆造版本（R53 家族病）。

    判定序（每步都有权威证据，无一步靠猜）：
      1. 内部 workspace 包（name ∈ internal_names）→ `workspace:*`（零网络，兄弟包不在 registry）。
      2. 显式 `name@range` → **按 P-C2 验证后**采用（旧行为"直采/尊重之"已废，见下）：
         · dist-tag/通配（`latest`/`*`）→ 解析成具体稳定版（不可复现是模块要治的病）；
         · 协议/别名（`workspace:` `file:` `git+` `npm:` …）/ 复合区间 → **不判**，原样保留；
         · 简单区间 → 取全量版本集判可满足性：不可满足＝确证幻觉 → 校正/如实丢弃；
           registry 不可达 → **fail-open 保留**（R56-6 证据缺失≠否定证据）。
      3. 裸名 → 本地 node_modules 版本 → registry 最新稳定版 → 加 `^` 前缀。查不到 → drop。
    """
    internal = internal_names or set()
    kept: list[ResolvedNpmDep] = []
    dropped: list[str] = []
    seen: set[str] = set()

    for raw in specs:
        name, explicit = _split_name_range(raw)
        if not name or name in seen:
            continue
        if name in internal:
            seen.add(name)
            kept.append(ResolvedNpmDep(name=name, spec="workspace:*", source="workspace"))
            continue
        if explicit:
            # ★P-C2（27 号文 §3.1）：R67L-B3 口径平移★ 旧实现一句"契约已给定，尊重之"直采，
            # 零验证 ⇒ `axios@^99.0.0` / `lodash@nonsense` 原样烤进**权威 package.json 模板**
            # 要 worker"原样写入"，而模板即真值 worker 无权改 → `npm install` 整包装不上。
            # **规划期自己在猜坐标 = 正面违反血规 2**。Maven 侧 R67L-B3 早已定论"显式版本是
            # 待验证的主张，绝非证据"，此处补齐。
            _kind = _range_kind(explicit)
            if _kind == "dist_tag":
                # `latest`/`*`/`next`＝语法合法但**不可复现**（模块 docstring 明列为要治的病）。
                # 它不需要全量版本集——直接解析具体版本即可；解析不到则保留原样（不可达时
                # 丢弃才是误杀，而我们没有能力把它变成可复现的东西，如实留痕）。
                _lv = registry_latest_version(name, project_path)
                seen.add(name)
                if _lv:
                    logger.warning("[npm-registry] P-C2 %s@%s 是不可复现的 dist-tag/通配 → "
                                   "解析到具体最新稳定版 ^%s（LLM 声明非证据）", name, explicit, _lv)
                    kept.append(ResolvedNpmDep(name=name, spec=f"^{_lv}", source="registry"))
                else:
                    logger.warning("[npm-registry] P-C2 %s@%s 是不可复现的 dist-tag/通配，但"
                                   "registry 不可达无法解析成具体版本 → fail-open 保留原样"
                                   "（执行期 L1 合法性闸兜底）", name, explicit)
                    kept.append(ResolvedNpmDep(name=name, spec=explicit, source="explicit",
                                               verified="unverified"))
                continue
            if _kind in ("protocol", "complex"):
                # 不判（Maven `${...}` 的对应物）：协议/别名不是版本主张；复合区间无 semver 库
                # 判不准，**猜语义的风险是误杀真依赖**，比放过一个幻觉更坏（fail-open 但留痕）。
                logger.info("[npm-registry] P-C2 %s@%s 属【%s】形态，不做可满足性判定"
                            "（保留原样；执行期 L1 dep-legality 兜底）", name, explicit, _kind)
                seen.add(name)
                kept.append(ResolvedNpmDep(name=name, spec=explicit, source="explicit",
                                           verified="unjudgeable"))
                continue
            _vers = registry_all_versions(name)
            if _vers is None:
                # registry 不可达 → fail-open 保留（R56-6 证据缺失≠否定证据），必须留痕。
                logger.warning("[npm-registry] P-C2 %s@%s 未经证实（registry 不可达/包查不到）"
                               " → fail-open 保留 LLM 主张（执行期 L1 合法性闸兜底）",
                               name, explicit)
                seen.add(name)
                kept.append(ResolvedNpmDep(name=name, spec=explicit, source="explicit",
                                           verified="unverified"))
                continue
            if not _range_is_satisfiable(explicit, _vers):
                # 区间不可满足＝**确证幻觉**（版本集是权威证据，已排除不可达）。
                _why = "仓库确证无任何版本可满足"
                _latest = registry_latest_version(name, project_path)
                if _latest:
                    logger.warning("[npm-registry] P-C2 %s@%s %s → 校正到最新稳定版 ^%s"
                                   "（LLM 声明非证据）", name, explicit, _why, _latest)
                    seen.add(name)
                    kept.append(ResolvedNpmDep(name=name, spec=f"^{_latest}", source="registry"))
                else:
                    logger.warning("[npm-registry] P-C2 %s@%s %s 且无可用稳定版 → 如实丢弃"
                                   "（绝不逼 worker 臆造；调用方须同时从验收剔除）",
                                   name, explicit, _why)
                    dropped.append(str(raw).strip())
                continue
            seen.add(name)
            kept.append(ResolvedNpmDep(name=name, spec=explicit, source="explicit"))
            continue
        ver = registry_latest_version(name, project_path)
        if not ver:
            dropped.append(str(raw).strip())
            continue
        seen.add(name)
        source = "local" if (project_path and local_node_modules_version(project_path, name)
                             == ver) else "registry"
        kept.append(ResolvedNpmDep(name=name, spec=f"^{ver}", source=source))

    if dropped:
        logger.warning(
            "[npm-registry] #31-P2b %d 个契约 npm 依赖无法确定性解析版本 → 如实丢弃"
            "（同时从验收标准剔除，绝不逼 worker 手写臆造版本）: %s",
            len(dropped), dropped)
    return kept, dropped
