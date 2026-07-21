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


def _lookup_enabled() -> bool:
    """SWARM_NPM_LOOKUP=0 → 关闭 registry 联网解析（单测默认关：绝不让测试依赖网络/被
    registry 拖慢，也杜绝"网络好就绿、离线就红"的假绿）。关闭后 = 解析不到 → 如实丢弃。"""
    return os.getenv("SWARM_NPM_LOOKUP", "1").strip().lower() not in ("0", "false", "no")


def _http_get(url: str) -> str | None:
    """GET 文本；任何失败（离线/超时/404）→ None。结果缓存（规划期同一包会被多模块问到）。"""
    if not _lookup_enabled():
        return None
    if url in _http_cache:
        return _http_cache[url]
    text: str | None = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "swarm-npm-resolver"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
            if 200 <= getattr(resp, "status", 200) < 300:
                text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        logger.debug("[npm-registry] GET %s 失败: %s", url, exc)
    _http_cache[url] = text
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


# ── 对外主入口 ──────────────────────────────────────────────────────────────
@dataclass
class ResolvedNpmDep:
    name: str
    spec: str      # 写入 package.json 的版本区间：内部=workspace:* / 第三方=^x.y.z
    source: str    # workspace | local | registry | explicit


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
      2. 显式 `name@range` → 直采该 range（LLM/契约已给定，尊重之）。
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
