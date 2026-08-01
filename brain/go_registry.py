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
# P-C2 专用伪版本判别：**两种形态都要认**。
#   · `v1.2.3-0.20240101000000-abcdef123456`（有前置 tag，带 `-<N>.` 段）
#   · `v0.0.0-20240101000000-abcdef123456`  （无前置 tag，**无** `-<N>.` 段，最常见）
# ★为什么不复用 `_PSEUDO`（血规 10 第三条：复用事实源≠复用消费契约）★ `_PSEUDO` 只认第一种，
# 它唯一的消费者 `_is_stable` 靠末句"主体含 `-` 即非稳定"把第二种一并兜住了，所以那边的窄口径
# 从来不是 bug。而本档的后果**相反**：判漏 ⇒ 把真实可用的伪版本送去存在性探测，proxy 对它
# 常不作答 → 当成幻觉校正掉 = 误杀。故另立一条覆盖两形态的模式，`_PSEUDO` 保持不动。
_PSEUDO_ANY = re.compile(r"-(?:\d+\.)?\d{14}-[0-9a-f]{12}(?:\+incompatible)?$", re.IGNORECASE)

_http_cache: dict[str, str | None] = {}
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
    if url in _http_cache:
        return _http_cache[url]
    text: str | None = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "swarm-go-resolver"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
            if 200 <= getattr(resp, "status", 200) < 300:
                text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        logger.debug("[go-registry] GET %s 失败: %s", url, exc)
    _http_cache[url] = text
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
    _probe_cache[_key] = out
    return out


def proxy_version_exists(mod: str, ver: str) -> bool | None:
    """该 module 的该版本在 proxy 上是否存在 → True / False（确证查无）/ None（不可达）。

    多镜像语义：任一镜像答 True → True；**全部**镜像答 False → False（确证）；
    只要有一个不可达且无人答 True → None（宁可未证实，绝不据不完整证据判幻觉）。
    """
    enc, encv = _encode_mod(mod), _encode_mod(ver)
    saw_false = False
    for tpl in _PROXY_INFO_MIRRORS:
        got = _http_probe(tpl.format(mod=enc, ver=encv))
        if got is True:
            return True
        if got is False:
            saw_false = True
        else:
            return None            # 有镜像不可达 → 证据不完整
    return False if saw_false else None


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


def _split_mod_version(raw: str) -> tuple[str, str | None]:
    """把 `github.com/x/y` / `github.com/x/y@v1.2.3` 拆成 (module, explicit_version|None)。"""
    s = str(raw).strip()
    if "@" in s:
        mod, _, ver = s.partition("@")
        return mod.strip(), (ver.strip() or None)
    return s, None


def resolve_go_deps(specs: list[str], internal_modules: set[str] | None = None,
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
      3. 裸 module → 本地 cache → proxy `/@latest`。查不到 → drop。
    """
    internal_set = internal_modules or set()
    kept: list[ResolvedGoDep] = []
    internal: list[str] = []
    dropped: list[str] = []
    seen: set[str] = set()

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
                # 不判（Maven `${...}` 的对应物）：伪版本编码的是 commit（proxy 常不列）、
                # 分支名/`latest`/裸 SHA 都不是"版本主张"。判它们只会误杀。
                logger.info("[go-registry] P-C2 %s@%s 非规范 semver tag（伪版本/分支/SHA）"
                            " → 不做存在性判定，保留原样（执行期 L1 dep-legality 兜底）",
                            mod, explicit)
                seen.add(mod)
                kept.append(ResolvedGoDep(module=mod, version=explicit, source="explicit"))
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
            if _exists is None:
                # R56-6：证据缺失≠否定证据 → fail-open 保留，但必须留痕。
                logger.warning("[go-registry] P-C2 %s@%s 未经证实（proxy 不可达）→ fail-open "
                               "保留 LLM 主张（执行期 L1 合法性闸兜底）", mod, explicit)
            seen.add(mod)
            kept.append(ResolvedGoDep(module=mod, version=explicit, source="explicit"))
            continue
        ver = proxy_latest_version(mod)
        if not ver:
            dropped.append(str(raw).strip())
            continue
        seen.add(mod)
        source = "local" if local_module_cache_version(mod) == ver else "proxy"
        kept.append(ResolvedGoDep(module=mod, version=ver, source=source))

    if dropped:
        logger.warning(
            "[go-registry] #31-P2c %d 个契约 Go 依赖无法确定性解析版本 → 如实丢弃"
            "（同时从验收标准剔除，绝不逼 worker 手写臆造版本）: %s",
            len(dropped), dropped)
    return kept, internal, dropped
