"""#31-Phase2c：Go module 版本确定性解析（本地 module cache → proxy.golang.org → 镜像）。

## 为什么必须有这一层（与 maven/npm registry 同因，栈中立铺开）

契约层为 Go 工程注入脚手架时会引入第三方 module（github.com/gin-gonic/gin …）。go.mod
的 require 指令**必须带版本**（`require github.com/x/y v1.2.3`；无版本 go 直接拒绝解析，
`go build` 全灭）。若脚手架省版本或让 worker 自己填，小模型要么臆造不存在的版本要么写
`latest`（go.mod 不接受 latest 字面量）——与 R47/R53 病同源。

本模块给确定性第三条路：**不臆造——去 Go module proxy（GOPROXY 协议）解析真实最新版**。
`GET <proxy>/<module>/@latest` 返回 `{"Version":"v1.2.3","Time":...}`（proxy 已按 semver
选最新稳定版）。解析不到就如实丢弃（fail-honest：宁可缺一个可归因可补的 require，绝不写死
一个拉不到的版本让 `go mod download` 整体失败连坐全模块）。

## 内部 module 不走 proxy

同 workspace/repo 内部 module（go.work 里 use 的兄弟 module）用 `replace` 指向本地相对
路径，**绝不**去 proxy 查（它们没发布）。内部 module 路径由调用方从兄弟 go.mod 的
`module` 行读出（事实来源）传入 internal_modules；据此把内部/第三方分流。

## 版本前缀 `v`

Go 版本恒以 `v` 打头（`v1.2.3`）。proxy 返回的已是规范 `vX.Y.Z`，直接写进 require。
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from swarm.brain.dep_http_cache import text_cache_lookup, text_cache_store

logger = logging.getLogger("swarm.brain.go_registry")

# 网络超时短而硬：规划期不容许被 proxy 拖死；查不通=丢弃，不阻断。
_HTTP_TIMEOUT_S = float(os.getenv("SWARM_GO_LOOKUP_TIMEOUT_S", "8"))
# 官方 proxy 优先，goproxy.cn（七牛镜像）兜底（国内可达性，与 maven aliyun / npm npmmirror 对称）。
_PROXY_MIRRORS = (
    "https://proxy.golang.org/{mod}/@latest",
    "https://goproxy.cn/{mod}/@latest",
)
# P-C2：单版本存在性端点（GOPROXY 协议 `/@v/<version>.info`）。与 `/@latest` 分开——
# 那个答"最新是哪个"，这个答"这个版本存在过吗"，后果不同（判错＝误杀真依赖 / 放过幻觉）。
_PROXY_INFO_MIRRORS = (
    "https://proxy.golang.org/{mod}/@v/{ver}.info",
    "https://goproxy.cn/{mod}/@v/{ver}.info",
)

# Go 预发布/伪版本：注入依赖必须落在正式 tag（`v0.0.0-<timestamp>-<hash>` 伪版本、
# `-alpha`/`-beta`/`-rc` 预发布会把下游拖进不可复现的坑）。
_PRERELEASE = re.compile(r"-(?:alpha|beta|rc|pre|dev|snapshot|next)", re.IGNORECASE)
# 伪版本（pseudo-version）：`vX.Y.Z-0.YYYYMMDDHHMMSS-abcdef123456` —— 主体后带
# `-<数字>.<14位时间戳>-<12位hash>`，proxy 对未打 tag 的 module 会返回这类；不可复现，排除。
_PSEUDO = re.compile(r"-\d+\.\d{14}-[0-9a-f]{12}$", re.IGNORECASE)
_SEMVER_CORE = re.compile(r"^v(\d+)(?:\.(\d+))?(?:\.(\d+))?")
# P-C2 可判形态：规范 `vX.Y.Z`（可带 `-prerelease` / `+incompatible`）。go.mod 的 require
# 只接受这种；分支名/`latest`/裸 commit SHA 都进"不判"分支（判它们只会误杀）。
_JUDGEABLE_VERSION = re.compile(r"^v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+incompatible)?$")
# ★P-C2 复核 R3★ 还有一档介于"不判"与"可判"之间：**写不进 go.mod 的形态**。
# `latest` / 分支名 / 裸 SHA 在 go.mod 的 require 里是**语法错误**（`go build` 解析期全灭，
# 连坐全模块），与"伪版本"同性质（不可复现）但**不能**简单原样保留——保留等于把解析错误
# 烤进权威模板。治法：先尝试 `proxy_latest_version` 校正到可解析的稳定版；校正不到
# （离线/不可达）→ **如实丢弃**（fail-honest，血规 2 方向）。npm 的 `latest` 是合法语法
# （装最新），go 的不是 ⇒ 两栈同型不同治，不对称是对的。
_UNGO_MODDABLE_VERSION = re.compile(
    r"^(?:latest|master|main|[0-9a-f]{7,40})$")
# P-C2 专用伪版本判别：**Go 官方定义三种形态，三种都要认**。
# 权威来源：`cmd/go` 文档 "Pseudo-versions"（go.dev/ref/mod#pseudo-versions）——形态由
# **base version（该 commit 之前最近的 tag）** 决定：
#   ① 无 base tag             `vX.0.0-<ts>-<hash>`            （最常见，如 v0.0.0-2024…）
#   ② base 是**预发布**版      `vX.Y.Z-<pre>.0.<ts>-<hash>`    （如 v1.2.3-beta.0.2024…）
#   ③ base 是**正式**版        `vX.Y.(Z+1)-0.<ts>-<hash>`      （patch 递增 + `-0.` 段）
# ★P-C2 复核 F2：原注释写"两种形态都要认"并据此收口，漏了 ②★ 那不是笔误而是**枚举本身错了**
# ——我为修窄口径新造这张表时凭印象列了两种，而官方是三种。② 在 `golang.org/x/*`、`k8s.io/*`
# 的 beta/rc 系里大量出现。漏判的后果是**误杀**：`v1.2.3-beta.0.…` 能过 `_JUDGEABLE_VERSION`
# ⇒ 被送去存在性探测 ⇒ 在线且 proxy 确证查无时判"幻觉版本"校正掉（离线才侥幸 fail-open）。
# ⇒ [[swarm-enumeration-needs-authoritative-source]]：声称"穷举全部形态"必须指出权威来源。
# ★为什么不复用 `_PSEUDO`（血规 10 第三条：复用事实源≠复用消费契约）★ `_PSEUDO` 只认 ③，
# 它唯一的消费者 `_is_stable` 靠末句"主体含 `-` 即非稳定"把 ①② 一并兜住了，所以那边的窄口径
# 从来不是 bug。而本档的后果**相反**（判漏＝误杀），故另立一条覆盖三形态的模式，`_PSEUDO` 不动。
# ★P-C2 复核 F-5：放宽过头了★ 上一版前缀写成 `(?:[0-9A-Za-z.-]+\.)?`（任意串 + 点），
# 比官方三形态宽得多，成了跳闸通道——实测这三个都被当成伪版本从而**跳过存在性核验**：
#   `v1.2.3-beta.7.<ts>-<hash>`（`.0.` 变 `.7.`）、`v1.2.3-totally.made.up.<ts>-<hash>`、
#   `v1.2.3-ABCDEF.<ts>-ABCDEF123456`（大写 hash，go 自己直接拒）。
# 形态 ② 的 `.0.` **是规范的一部分**（base 是预发布时，pseudo 在其后补 `.0.`），不是任意段。
# 收成：可选的 `<pre>`（点分段，各段 `[0-9A-Za-z-]+`）后必须紧跟字面 `.0.`。
# 另：`re.IGNORECASE` 对 hash 段是**错的**——go 只接受小写 12 位 hex，大写该走"不判"分支
# 交给存在性核验，而非被当成合法伪版本放行。故整条去掉 IGNORECASE。
# 三形态（go.dev/ref/mod#pseudo-versions，本轮实测核对）：
#   ① `vX.0.0-<ts>-<hash>`                  无 base tag        → 前缀段缺席
#   ② `vX.Y.Z-<pre>.0.<ts>-<hash>`          base 是预发布版    → 前缀段 = `<pre>.0.`
#   ③ `vX.Y.(Z+1)-0.<ts>-<hash>`            base 是正式版      → 前缀段 = `0.`
_PSEUDO_ANY = re.compile(
    r"-(?:(?:[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*\.)?0\.)?\d{14}-[0-9a-f]{12}"
    r"(?:\+incompatible)?$")

_http_cache: dict[str, str | None] = {}
# ★P-C2 复核 F-1★ `_http_cache` 里 `None` 条目的到期时刻（`time.monotonic()` 基准）。
# 与 `_http_cache` **平行**而非合并进去：那个 dict 的值形状 `str | None` 是承重契约
# （`test_pc2_explicit_version_is_a_claim.py:493-497` 直接写入 None 并断言其值，用来锁
# "文本缓存与探测缓存不得混用"）。策略见 brain/dep_http_cache.py。
_http_neg_until: dict[str, float] = {}
# 探测缓存与文本缓存**分开**：值域不同（三态 bool|None vs 文本|None），混用会让
# "404 确证" 与 "取到空文本" 撞进同一个键（P-C2）。
_probe_cache: dict[str, bool | None] = {}


def _lookup_enabled() -> bool:
    """SWARM_GO_LOOKUP=0 → 关闭 proxy 联网解析（单测默认关：绝不让测试依赖网络，
    也杜绝"网络好就绿、离线就红"的假绿）。关闭后 = 解析不到 → 如实丢弃。"""
    return os.getenv("SWARM_GO_LOOKUP", "1").strip().lower() not in ("0", "false", "no")


def _http_get(url: str) -> str | None:
    """GET 文本；任何失败（离线/超时/404）→ None。结果缓存（规划期同一 module 会被多处问到）。"""
    if not _lookup_enabled():
        return None
    # ★P-C2 复核 F-1★ 原实现 `if url in _http_cache: return ...` + 末尾无条件
    # `_http_cache[url] = text` ⇒ **`None` 被永久缓存**，一次抖动把该坐标钉死（详见
    # brain/dep_http_cache.py 的模块 docstring：两个方向的实测后果与为什么用 TTL）。
    # F5 治 `_probe_cache` 时漏了同文件这个兄弟函数（纪律 5：修一类先全仓捞 sibling）。
    hit, cached = text_cache_lookup(_http_cache, _http_neg_until, url)
    if hit:
        return cached
    text: str | None = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "swarm-go-resolver"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
            if 200 <= getattr(resp, "status", 200) < 300:
                text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        logger.debug("[go-registry] GET %s 失败: %s", url, exc)
    text_cache_store(_http_cache, _http_neg_until, url, text)
    return text


def _http_probe(url: str) -> bool | None:
    """存在性探测 → `True`（2xx）/ `False`（**404/410 确证不存在**）/ `None`（不可达）。

    ★为什么不能复用 `_http_get`★ 它把"404 包不存在"与"离线/超时"都返 `None` ⇒ 拿它判存在性，
    离线跑一次就会把**所有**显式版本判成幻觉、批量误杀真依赖（P-C2 治理里最危险的方向）。
    三态是硬要求：只有 `False` 才允许判幻觉，`None` 必须 fail-open（R56-6 证据缺失≠否定证据）。
    """
    if not _lookup_enabled():
        return None
    _key = f"probe::{url}"
    if _key in _probe_cache:
        return _probe_cache[_key]
    out: bool | None = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "swarm-go-resolver"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
            out = 200 <= getattr(resp, "status", 200) < 300
    except urllib.error.HTTPError as exc:      # 必须在 URLError 之前——它是其子类
        out = False if getattr(exc, "code", None) in (404, 410) else None
        logger.debug("[go-registry] PROBE %s → HTTP %s", url, getattr(exc, "code", "?"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        logger.debug("[go-registry] PROBE %s 不可达: %s", url, exc)
    # ★P-C2 复核 F5★ `None` **不入缓存**。原实现无条件 `_probe_cache[_key] = out`，而命中判据是
    # `if _key in _probe_cache`（`None` 也算命中）＋生产侧无任何清理点＋brain 是长驻进程
    # ⇒ 一次网络抖动就把该 module@version **永久**钉成"不可达"，此后再不重验，
    # 且它长得和"真的一直不可达"一模一样（血规 10 第四条：缺席必须机读可辨）。
    # 缓存的目的是省掉重复 I/O，不是记住失败——只缓存**确定性结论**（True/False）。
    if out is not None:
        _probe_cache[_key] = out
    return out


def proxy_version_exists(mod: str, ver: str) -> bool | None:
    """该 module 的该版本在 proxy 上是否存在 → True（存在）/ None（不可达或 proxy 不提供）。

    ★P-C2 复核 R4★ go proxy 的 404/410 语义是**"proxy 不提供，可能别处有"**（go.dev/ref/mod：
    "the requested module or version is not available on the proxy, but it may be found
    elsewhere"），与 npm registry 的 404（权威"包不存在"）**不同**。go 命令拿到 404 的动作
    是 fallback direct，不是判定不存在。

    原实现把"两镜像都 404"升格成"确证查无"（`False`），后果是私有 module（不在
    `internal_modules` 里、属别的 repo）被误判幻觉丢弃——治前该显式版本原样保留、worker
    侧设了 `GOPRIVATE` 就能拉到，这是一个真实存在过的能力被本批干掉。

    ⇒ `False` 这一档**取消**：proxy 不提供 ≠ 包不存在。所有"不是 True"的结局都归 `None`
    （不可达），由调用方 fail-open 保留 + 记 `unverified`（机读账）。真幻觉版本（公共 proxy
    上真不存在的版本）会被保留——但 worker 侧 `go mod download` 会失败，归因清楚；误杀
    （丢弃真依赖）比假过（保留幻觉版本）更糟。
    """
    enc, encv = _encode_mod(mod), _encode_mod(ver)
    saw_unreachable = False
    for tpl in _PROXY_INFO_MIRRORS:
        got = _http_probe(tpl.format(mod=enc, ver=encv))
        if got is True:
            return True
        # ★R4★ 404/410（`got is False`）与不可达（`got is None`）同归"proxy 不提供/不可达"，
        # 都不再升格成"确证不存在"。go proxy 的 404 语义是"可能别处有"，不是"包不存在"。
        saw_unreachable = True
        continue
    return None                    # 无一镜像能证实存在 → 证据不完整，绝不据此判幻觉


def _is_stable(version: str) -> bool:
    return (bool(version) and version.startswith("v")
            and not _PRERELEASE.search(version) and not _PSEUDO.search(version)
            and "-" not in version.split("+")[0])


def _ver_key(v: str) -> tuple:
    m = _SEMVER_CORE.match(v.strip())
    if not m:
        return (0, 0, 0)
    return tuple(int(g) if g and g.isdigit() else 0 for g in m.groups())


def _encode_mod(mod: str) -> str:
    """GOPROXY 协议要求 module 路径中的**大写字母**转成 `!<小写>`（避免大小写不敏感文件系统
    冲突）。如 `github.com/Azure/x` → `github.com/!azure/x`。"""
    return re.sub(r"[A-Z]", lambda m: "!" + m.group(0).lower(), mod)


# ── 本地证据（零网络） ──────────────────────────────────────────────────────
def local_module_cache_version(mod: str) -> str | None:
    """本地 Go module cache 里**已下载**的该 module 最新稳定版（`$GOPATH/pkg/mod/<mod>@<ver>`
    目录即证据，零网络）。规划期联网若抖动/被墙，本地已下载版本比 proxy 最新更保险。

    cache 目录名对大写用 `!<lower>` 转义（与 proxy 一致）。同受 SWARM_GO_LOOKUP 约束。"""
    if not _lookup_enabled():
        return None
    gopath = os.getenv("GOPATH") or str(Path.home() / "go")
    mod_root = Path(gopath) / "pkg" / "mod"
    enc = _encode_mod(mod)
    # cache 布局：pkg/mod/<enc-mod>@<version>/ —— 用父目录 glob 匹配 `<leaf>@*`
    parent = mod_root / Path(*enc.split("/")[:-1]) if "/" in enc else mod_root
    leaf = enc.rsplit("/", 1)[-1]
    try:
        if not parent.is_dir():
            return None
        vers = [p.name.split("@", 1)[1] for p in parent.iterdir()
                if p.is_dir() and p.name.startswith(leaf + "@") and "@" in p.name]
    except OSError:
        return None
    stable = [v for v in vers if _is_stable(v)]
    return max(stable, key=_ver_key) if stable else None


def project_go_mod_requires(project_path: str) -> dict[str, str]:
    """工程**自己的** go.mod 里 require 钉的版本（mod → version，零网络证据）。

    ★P-H3（27 号文）★ 治前裸 module 解析只认 `$GOPATH/pkg/mod`（已下载）与 proxy（联网）——
    E2E 沙箱是新 clone（cache 空）、proxy 一抖，契约依赖被**如实丢弃**，而工程自己 go.mod
    的 require 行就是钉死的可解析版本（比 proxy 最新更贴合本工程）。
    读根 + 单层子目录 go.mod（与 maven `index_baseline`「根+单层」同形状；go.work 深层
    成员不在面内=诚实边界）。根优先。`// indirect` 行同样收录——版本钉是真实的，
    被解析的契约依赖本就是直接依赖，采用其钉版不改变直接/间接语义。
    与 `local_module_cache_version` 同契约：同受 SWARM_GO_LOOKUP 门控——开关文档口径是
    「关闭后=解析不到→如实丢弃」，本层不门控就会在离线模式里静默破约。
    """
    if not _lookup_enabled():
        return {}
    out: dict[str, str] = {}
    root = Path(project_path)
    mods: list[Path] = []
    try:
        # ★py<3.13 的 pathlib.is_file 只吞 ENOENT/ENOTDIR 等，EACCES 照抛（3.13+ 起
        # 全吞 OSError 返 False）——CI py3.12 实测：目录 0o000 时本行 uncaught 炸穿。
        # 「根清单在不在」判定失败与「真没有根清单」必须可辨（硬检查④）→ 落 WARNING。
        if (root / "go.mod").is_file():
            mods.append(root / "go.mod")
    except OSError:
        logger.warning("[go-registry] P-H3 根 go.mod 存在性判定失败（目录不可读），"
                       "按无根清单降级: %s", root)
    try:
        # ★Path.glob 会静默吞 OSError 返回 []（实测）——异常≠缺席（硬检查④），
        # 枚举必须用会抛的 iterdir，失败与「真没有子目录 go.mod」才可辨
        mods += sorted(d / "go.mod" for d in root.iterdir()
                       if d.is_dir() and (d / "go.mod").is_file())
    except OSError:
        logger.warning("[go-registry] P-H3 子目录 go.mod 枚举失败（目录不可读），"
                       "仅采根 go.mod: %s", root)
    _REQ_LINE = re.compile(r"^([\w.\-/]+)\s+(v[\w.\-+]+)\s*(?://.*)?$")
    _REQUIRE_OPEN = re.compile(r"^require\s*\(\s*(?://.*)?$")
    _OTHER_OPEN = re.compile(r"^(?:exclude|replace|retract)\s*\(\s*(?://.*)?$")
    _BLOCK_CLOSE = re.compile(r"^\)\s*(?://.*)?$")   # R2-2：`) // 注释` 也是合法块结束
    for gm in mods:
        try:
            txt = gm.read_text("utf-8", errors="replace")
        except OSError:
            # 异常≠缺席（硬检查④）：读不出与「真没有 require」必须可辨
            logger.warning("[go-registry] P-H3 go.mod 读取失败，其 require 钉版不参与取证: %s", gm)
            continue
        # require 两种形态：单行 `require mod vX.Y.Z` 与块 `require ( … )`。
        # ★复核 R1-1★ 前缀剥除法补不了 exclude 块——剥掉 `exclude (` 后块内行与 require
        # 块内行逐字相同（`mod vX.Y.Z`），坏版本/下架版本（retract）会冒充钉版。故小状态机：
        # 只收 require 上下文里的行，其他指令块（exclude/replace/retract）整段跳过，
        # 其余行（module/go/toolchain/裸行）一律不收。
        in_require = False
        in_other = False
        for line in txt.splitlines():
            s = line.strip()
            if not s or s.startswith("//"):
                continue
            if in_require:
                if _BLOCK_CLOSE.match(s):
                    in_require = False
                    continue
                candidate = s
            elif in_other:
                if _BLOCK_CLOSE.match(s):
                    in_other = False
                continue
            elif _REQUIRE_OPEN.match(s):
                in_require = True
                continue
            elif _OTHER_OPEN.match(s):
                in_other = True
                continue
            elif s[:7] == "require" and len(s) > 7 and s[7].isspace():
                # R2-1：directive 与 module 间可以是 tab/多空格（gofmt 会归一，
                # 手写/LLM 写的 go.mod 不保证），剥前缀必须容忍任意空白
                candidate = s[7:].lstrip()
            else:
                # module/go/toolchain/单行 exclude/retract/replace（后者尾部 `=>` 或
                # 无前缀版本本就过不了 _REQ_LINE，这里一并挡）——非 require 上下文绝不收
                continue
            m = _REQ_LINE.match(candidate)
            if m:
                out.setdefault(m.group(1), m.group(2))
    return out


def proxy_latest_version(mod: str) -> str | None:
    """版本解析：本地 module cache（确定能拉）→ proxy `/@latest`（过滤伪版本/预发布）→ 镜像。
    查不到/仅伪版本 → None（绝不臆造/latest 字面量）。"""
    local = local_module_cache_version(mod)
    if local:
        return local
    if not _lookup_enabled():
        return None
    enc = _encode_mod(mod)
    for tpl in _PROXY_MIRRORS:
        raw = _http_get(tpl.format(mod=enc))
        if not raw:
            continue
        try:
            doc = json.loads(raw)
        except ValueError:
            continue
        ver = doc.get("Version") if isinstance(doc, dict) else None
        # proxy 对未打 tag 的 module 返回伪版本 → 不可复现，拒采（宁缺）。
        if isinstance(ver, str) and _is_stable(ver):
            return ver
    return None


# ── 对外主入口 ──────────────────────────────────────────────────────────────
@dataclass
class ResolvedGoDep:
    module: str
    version: str        # require 版本：`vX.Y.Z`
    source: str         # local | proxy | explicit
    # ★P-C2 复核 F-2★ 见 npm_registry.ResolvedNpmDep.verified 的同款注释（两栈同口径）。
    # go 侧 fail-open 尤其容易整轮静默失效：F1 收紧后 `False` 要求"全部镜像都答得上"，
    # 而 `proxy.golang.org` 在国内常不可达 ⇒ `proxy_version_exists` 永不返 False
    # ⇒ 闸对该部署全程无效，唯一信号是每依赖一条 WARNING（而纪律 #106 禁止解析 swarm.log）。
    #   verified / unverified / unjudgeable —— 同 npm 三档
    verified: str = "verified"


def _split_mod_version(raw: str) -> tuple[str, str | None]:
    """把 `github.com/x/y` / `github.com/x/y@v1.2.3` 拆成 (module, explicit_version|None)。"""
    s = str(raw).strip()
    if "@" in s:
        mod, _, ver = s.partition("@")
        return mod.strip(), (ver.strip() or None)
    return s, None


def resolve_go_deps(specs: list[str], internal_modules: set[str] | None = None,
                    project_path: str | None = None,
                    ) -> tuple[list[ResolvedGoDep], list[str], list[str]]:
    """把契约 Go 依赖（module 路径或 mod@ver）解析成 require 项。

    返回 (kept, internal, dropped)：
      - kept：第三方 require（带解析出的版本）；
      - internal：内部 module 路径（调用方据此生成 `replace <mod> => <相对路径>`，零网络）；
      - dropped：解析不到版本的第三方（**必须同时从契约/验收剔除**，杜绝逼 worker 造假）。

    判定序（每步有权威证据，无一步靠猜）：
      1. 内部 module（∈ internal_modules）→ internal（replace 指向本地兄弟，绝不查 proxy）。
      2. 显式 `mod@ver` → **按 P-C2 验证后**采用（旧行为"直采/契约已给定"已废）：
         · 非规范 semver tag（伪版本 `v0.0.0-<ts>-<hash>` / 分支名 / 裸 SHA）→ **不判**，原样
           保留（它们是真实可用形态，判必然 404 → 误杀）；
         · 规范 `vX.Y.Z(-pre)(+incompatible)` → proxy `/@v/<ver>.info` 探测：确证查无＝幻觉
           → 校正到 `/@latest` 或如实丢弃；**不可达 → fail-open 保留**（R56-6）。
      3. 裸 module → 本地 cache → **工程自身 go.mod require 钉版**（P-H3，零网络）
         → proxy `/@latest`。查不到 → drop。
    """
    internal_set = internal_modules or set()
    kept: list[ResolvedGoDep] = []
    internal: list[str] = []
    dropped: list[str] = []
    seen: set[str] = set()
    _go_mod_pins: dict[str, str] | None = None

    for raw in specs:
        mod, explicit = _split_mod_version(raw)
        if not mod or mod in seen:
            continue
        if mod in internal_set:
            seen.add(mod)
            internal.append(mod)
            continue
        if explicit:
            # ★P-C2（27 号文 §3.1）：R67L-B3 口径平移★ 旧实现一句"契约已给定"直采，零验证
            # ⇒ 臆造版本原样烤进**权威 go.mod 模板**要 worker 原样写入 → `go mod download`
            # 整体失败连坐全模块。**规划期自己在猜坐标 = 正面违反血规 2**。
            _exp = explicit.strip()
            if not _JUDGEABLE_VERSION.match(_exp) or _PSEUDO_ANY.search(_exp):
                # ★P-C2 复核 R3★ `latest`/分支名/裸 SHA 在 go.mod 里是**语法错误**（go build
                # 解析期全灭），与伪版本同性质（不可复现）但**不能**原样保留——保留等于把
                # 解析错误烤进权威模板。治法：先尝试校正到可解析稳定版；校正不到 → 如实丢弃。
                if _UNGO_MODDABLE_VERSION.match(_exp):
                    _lv = proxy_latest_version(mod)
                    if _lv:
                        logger.warning("[go-registry] P-C2-R3 %s@%s 是写不进 go.mod 的形态"
                                       "（latest/分支名/裸 SHA）→ 校正到可解析稳定版 %s",
                                       mod, explicit, _lv)
                        seen.add(mod)
                        kept.append(ResolvedGoDep(module=mod, version=_lv, source="proxy",
                                                  verified="verified"))
                    else:
                        logger.warning("[go-registry] P-C2-R3 %s@%s 是写不进 go.mod 的形态，"
                                       "且 proxy 不可达无法校正 → 如实丢弃（绝不把解析错误"
                                       "烤进权威 go.mod）", mod, explicit)
                        dropped.append(str(raw).strip())
                    continue
                # 伪版本（`v0.0.0-<ts>-<hash>`）：真实可用形态，判必然 404 → 误杀，原样保留。
                # ★P-C2 复核 R2★ 伪版本无下游兜底（go 的 L1 dep-legality 仍无 driver——
                # X-M10 后调用方已按 manifest 分派，go 触发 warn-once 零覆盖可辨），
                # 止于 WARNING。判它必然 404 → 误杀，故宁可放行。
                logger.info("[go-registry] P-C2 %s@%s 伪版本（真实可用形态）→ 不做存在性判定，"
                            "保留原样（无下游兜底，止于 WARNING——npm/go 的 L1 dep-legality "
                            "是空转，见 27 号文 P-C2 R2）",
                            mod, explicit)
                seen.add(mod)
                kept.append(ResolvedGoDep(module=mod, version=explicit, source="explicit",
                                          verified="unjudgeable"))
                continue
            _exists = proxy_version_exists(mod, explicit)
            if _exists is False:
                _latest = proxy_latest_version(mod)
                if _latest:
                    logger.warning("[go-registry] P-C2 %s@%s proxy 确证查无该版本（幻觉版本）"
                                   " → 校正到 %s（LLM 声明非证据）", mod, explicit, _latest)
                    seen.add(mod)
                    kept.append(ResolvedGoDep(module=mod, version=_latest, source="proxy"))
                else:
                    logger.warning("[go-registry] P-C2 %s@%s proxy 确证查无且无可用版本 → 如实丢弃"
                                   "（绝不逼 worker 臆造；调用方须同时从验收剔除）", mod, explicit)
                    dropped.append(str(raw).strip())
                continue
            _unverified = _exists is None
            if _unverified:
                # R56-6：证据缺失≠否定证据 → fail-open 保留，但必须留痕。
                # ★P-C2 复核 R2★ go 侧无下游兜底（go 的 L1 dep-legality 仍无 driver），止于 WARNING。
                logger.warning("[go-registry] P-C2 %s@%s 未经证实（proxy 不可达）→ fail-open "
                               "保留 LLM 主张（无下游兜底，止于 WARNING——npm/go 的 L1 "
                               "dep-legality 是空转，见 27 号文 P-C2 R2）", mod, explicit)
            seen.add(mod)
            kept.append(ResolvedGoDep(
                module=mod, version=explicit, source="explicit",
                # `_exists is True` 才算验过；None＝证据不完整（F-2：这两种结局原先不可区分）
                verified="unverified" if _unverified else "verified"))
            continue
        # ★P-H3★ 裸 module 判定序：本地 cache（已下载）→ 工程自身 go.mod require 钉版
        # （同仓零网络）→ proxy。治前中间这层不存在：新 clone 沙箱（cache 空）+ proxy
        # 抖动 ⇒ 契约依赖被如实丢弃，答案却写在同仓 go.mod 里。
        _cached = local_module_cache_version(mod)
        if _cached:
            seen.add(mod)
            kept.append(ResolvedGoDep(module=mod, version=_cached, source="local"))
            continue
        if project_path and _go_mod_pins is None:
            _go_mod_pins = project_go_mod_requires(project_path)
        _pinned = (_go_mod_pins or {}).get(mod)
        if _pinned:
            # ★诚实边界（双复核 R1-3/R2-2）★钉版=具体版本且本工程曾可 build，证据强度
            # 高于 npm 的区间声明；但 go.mod 若被上一轮 LLM 污染，本层照样保留——
            # go 侧 L1 仍无 dep-legality driver（warn-once 可辨，X-M10），止于此边界。
            seen.add(mod)
            kept.append(ResolvedGoDep(module=mod, version=_pinned,
                                      source="go_mod", verified="verified"))
            continue
        ver = proxy_latest_version(mod)
        if not ver:
            dropped.append(str(raw).strip())
            continue
        seen.add(mod)
        kept.append(ResolvedGoDep(module=mod, version=ver, source="proxy"))

    if dropped:
        logger.warning(
            "[go-registry] #31-P2c %d 个契约 Go 依赖无法确定性解析版本 → 如实丢弃"
            "（同时从验收标准剔除，绝不逼 worker 手写臆造版本）: %s",
            len(dropped), dropped)
    return kept, internal, dropped
