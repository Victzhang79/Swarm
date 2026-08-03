"""P-H4：PyPI 依赖版本确定性解析（工程自身清单声明 → 官方 PyPI JSON API）。

## 为什么必须有这一层（与 npm_registry 同因，栈中立铺开）

契约层为 python 工程注入脚手架时，pyproject.toml 的 `[project] dependencies` **每条都得
带版本约束**（pip 生态无父级托管；漏写=装成不可复现漂移版，让 worker 自己填=臆造
`flask==99.0`——R47/R53 同源病）。本模块给出确定性第三条路：**不臆造、不裸装——
工程自身清单已声明的用声明（零网络），否则去 PyPI 解析真实最新稳定版**。解析不到
如实丢弃（调用方须连同验收标准一起丢弃）。离线/查不通 → 丢弃（fail-honest）。

## 内部模块不走 PyPI

monorepo 内部模块（兄弟包）**绝不**去 PyPI 查（它们不在 PyPI 上）。由调用方按目录
事实传入 internal_modules（PEP 503 归一化后的名字集）据此分流；本函数只把它们从
第三方解析里摘出来返回，**不**物化进清单——pyproject 没有确定性的相对路径内部引用
机制（PEP 508 `file:` 相对引用按 pip 调用时 cwd 解析，2026-08 实测），uv/poetry 各有
私有协议而 STACK_SPEC 对 python aggregate 刻意 None（收录任何一种都是猜）。

## 版本约束形态：下限钉死 `>=x.y.z`

解析出的是**具体最新稳定版**，写 `>=` 下限（可复现下限 + 允许补丁/小版本更新，与应用
工程惯例一致；`~=` 兼容release 上限语义随打包者意图漂移，不替项目做主）。
稳定版 = PEP 440 非 pre/dev release（packaging 库权威判定）。
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

logger = logging.getLogger("swarm.brain.pypi_registry")

# 网络超时短而硬：规划期不容许被 registry 拖死；查不通=丢弃，不阻断。
_HTTP_TIMEOUT_S = float(os.getenv("SWARM_PYPI_LOOKUP_TIMEOUT_S", "8"))
_PYPI_JSON = "https://pypi.org/pypi/{pkg}/json"

_http_cache: dict[str, str | None] = {}
# 失败(None)条目的到期时刻（与 _http_cache 平行不并入，见 dep_http_cache 模块 docstring）
_http_neg_until: dict[str, float] = {}

# PEP 503 归一化：小写 + 连续 -_. 折叠成单个 -
_NORMALIZE_RE = re.compile(r"[-_.]+")
# PEP 508 依赖串粗拆：name[extras] specifier（; marker 段先剥）
_DEP_RE = re.compile(
    r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(\[[^\]]*\])?\s*([^;]*)")


def _lookup_enabled() -> bool:
    """SWARM_PYPI_LOOKUP=0 → 关闭 PyPI 联网解析（单测默认关：绝不让测试依赖网络/假绿）。
    关闭后 = 解析不到 → 如实丢弃（与 npm/go/maven 三栈 LOOKUP 开关同契约——含本地证据层：
    开关口径是「关闭后=解析不到→如实丢弃」，工程清单读取同受其约束）。
    ★注释里绝不写其它开关的裸词头——env_registry 扫描会把散文词头当成未登记开关★。"""
    return os.getenv("SWARM_PYPI_LOOKUP", "1").strip().lower() not in ("0", "false", "no")


def normalize_name(name: str) -> str:
    """PEP 503 归一化名（`Foo_Bar.baz` → `foo-bar-baz`）——清单声明、PyPI URL、内部
    模块名比较全部走它，绝不拿原始大小写比（`Flask` 与 `flask` 是同一个包）。"""
    return _NORMALIZE_RE.sub("-", (name or "").strip().lower())


def _http_get(url: str) -> str | None:
    """GET 文本；任何失败（离线/超时/404）→ None。TTL 负缓存（P-C2 复核 F-1 同型：
    永久缓存 None 会把一次抖动钉成永久「查不到」）。"""
    if not _lookup_enabled():
        return None
    hit, cached = text_cache_lookup(_http_cache, _http_neg_until, url)
    if hit:
        return cached
    text: str | None = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "swarm-pypi-resolver"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
            if 200 <= getattr(resp, "status", 200) < 300:
                text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        logger.debug("[pypi-registry] GET %s 失败: %s", url, exc)
    text_cache_store(_http_cache, _http_neg_until, url, text)
    return text


def _pypi_json(name: str) -> dict | None:
    """PyPI JSON API 响应（None=不可达/不存在/非法——三态在调用方用 versions 集区分）。"""
    text = _http_get(_PYPI_JSON.format(pkg=urllib.parse.quote(normalize_name(name))))
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        logger.warning("[pypi-registry] %s 的 PyPI 响应不是合法 JSON（按不可达处理）", name)
        return None
    return data if isinstance(data, dict) else None


def registry_versions(name: str) -> frozenset[str] | None:
    """该包的全部发布版本集 → frozenset（**含预发布**，供显式版本主张核验）；None=不可达。

    空 frozenset=包存在但零发布（确证无版本可用）——与「不可达」机读可辨。
    """
    data = _pypi_json(name)
    if data is None:
        return None
    rel = data.get("releases")
    return frozenset(rel) if isinstance(rel, dict) else frozenset()


def _stable_versions(data: dict) -> list[str]:
    """PEP 440 稳定版（非 pre/dev），按版本序升序。非法版本串如实跳过（不猜）。"""
    from packaging.version import InvalidVersion, Version
    rel = data.get("releases")
    if not isinstance(rel, dict):
        return []
    out = []
    for v in rel:
        try:
            pv = Version(v)
        except InvalidVersion:
            continue
        if not pv.is_prerelease and not pv.is_devrelease:
            out.append((pv, v))
    out.sort(key=lambda t: t[0])
    return [v for _, v in out]


def registry_latest_version(name: str) -> str | None:
    """PyPI 最新稳定版；查不到（离线/包不存在/零稳定版）→ None（调用方如实丢弃）。"""
    data = _pypi_json(name)
    if data is None:
        return None
    stables = _stable_versions(data)
    return stables[-1] if stables else None


def _parse_dep_text(text: str) -> tuple[str, str, str] | None:
    """PEP 508 依赖串 → (归一化名, extras段原文, specifier原文)。剥 `; marker` 段。

    ★extras（`flask[async]`）与 environment marker（`; python_version<'3.11'`）都必须
    保留★——静默丢掉会把「装异步支持」改成「没装」、把「仅 win32 的条件依赖」改成
    「无条件依赖」，都是换语义不是换写法（hunter R2 M-2）。marker 随 specifier 原文
    返回（`spec ; marker` 形态），核验只拿 marker 前的版本段、渲染时整段写回（合法
    PEP 508）。direct reference（`name @ url`）**不剥 URL**：specifier 位原样带
    ` @ url` 段返回——下游 SpecifierSet 必判它非法 → 走「不判原样保留」通道，渲染回
    清单仍是合法 PEP 508（剥掉 URL 只留名字＝把项目钉死的来源静默换成公网版，cr R-3）。
    无法解析（纯 URL 行、-r/-e 选项行）→ None（调用方跳过，绝不硬猜名字）。"""
    line = (text or "").strip()
    if not line or line.startswith(("#", "-", "git+", ".")):
        return None
    if line.startswith(("http://", "https://")):
        # 裸 URL 行。★cr R2 HIGH-1★ 绝不按 "http" 前缀判——httpx/httpcore/httplib2
        # 是 PyPI 头部主流包名，前缀闸会把它们系统性误杀成「不可解析」。
        return None
    if " #" in line:
        # requirements.txt 行内注释（`flask>=2.0  # web 框架`）——不剥会污染 specifier
        # → InvalidSpecifier → spec_unparsed 假降级（cr R-4）。PEP 508 URL 片段的 `#`
        # 前无空格（`pkg @ http://x#sha256=…`），用「空格+#」判注释不误伤。
        line = line.split(" #", 1)[0].strip()
        if not line:
            return None
    core, sep, marker = line.partition(";")
    m = _DEP_RE.match(core)
    if not m:
        return None
    name = normalize_name(m.group(1))
    if not name:
        return None
    spec = (m.group(3) or "").strip()
    if spec.startswith("@"):
        # direct reference：补回 `@` 前的空格——渲染 `name+extras+spec` 时还原成
        # 合法 PEP 508 `name @ url`（剥成 `name@ url` 虽合法但丑且不像原文）
        spec = " " + spec
    if sep and marker.strip():
        # environment marker 保留（语义载体）：核验只用 marker 前的版本段，渲染整段写回
        spec = f"{spec} ; {marker.strip()}" if spec else f"; {marker.strip()}"
    return name, (m.group(2) or ""), spec


def project_manifest_specs(project_path: str | None) -> dict[str, tuple[str, str]]:
    """工程自身清单声明 {归一化名: (extras, specifier)}（零网络中间证据层，P-H3 平移）。

    证据源：根 + 一级子目录的 `pyproject.toml`（`[project] dependencies`）、
    `requirements.txt`（逐行）与 `setup.py`（`install_requires` 字面量列表）。specifier 原文采用（工程是自身依赖的权威声明者）；
    extras 保留（`requests[security]` 静默丢 extras=换语义）。裸声明 → ("", "")，
    与「没声明」可辨。与网络查询同受 SWARM_PYPI_LOOKUP 门控（开关契约「关闭后=
    解析不到→如实丢弃」，与 npm project_manifest_specs 同形）。
    """
    if not project_path or not _lookup_enabled():
        return {}
    root = Path(project_path)
    try:
        if not root.is_dir():
            return {}
    except OSError:
        return {}
    specs: dict[str, tuple[str, str]] = {}

    def _read_manifest(mdir: Path) -> None:
        pj = mdir / "pyproject.toml"
        if pj.is_file():
            try:
                import tomllib
                data = tomllib.loads(pj.read_text("utf-8", errors="replace"))
            except (OSError, ValueError) as exc:
                # 硬检查④：解析失败 ≠ 没有声明
                logger.warning("[pypi-registry] %s 解析失败（%s），该清单声明证据缺席", pj, exc)
                data = None
            deps = ((data or {}).get("project") or {}).get("dependencies")
            if isinstance(deps, list):
                for d in deps:
                    if isinstance(d, str):
                        parsed = _parse_dep_text(d)
                        if parsed and parsed[0] not in specs:
                            specs[parsed[0]] = (parsed[1], parsed[2])
        rq = mdir / "requirements.txt"
        if rq.is_file():
            try:
                lines = rq.read_text("utf-8", errors="replace").splitlines()
            except OSError as exc:
                logger.warning("[pypi-registry] %s 读取失败（%s），该清单声明证据缺席", rq, exc)
                lines = []
            for ln in lines:
                parsed = _parse_dep_text(ln)
                if parsed and parsed[0] not in specs:
                    specs[parsed[0]] = (parsed[1], parsed[2])
        sp = mdir / "setup.py"
        if sp.is_file():
            # setup.py 是代码不是数据——正则只取【字面量列表】的 install_requires，
            # 动态拼出来的拿不到=诚实缺席（stack_detect 对 python_requires 同先例，cr R-6）。
            try:
                sp_text = sp.read_text("utf-8", errors="replace")
            except OSError as exc:
                logger.warning("[pypi-registry] %s 读取失败（%s），该清单声明证据缺席", sp, exc)
                sp_text = ""
            # ★cr R2 MEDIUM-1★ 闭括号必须锚定后续字符——非贪婪 `.*?` 会在 extras 的
            # `']'`（`'requests[security]'` 内部）提前截断，extras 条目及其后全部条目
            # 静默蒸发。`\]\s*[,)\n]` 要求真闭括号后跟逗号/右括号/换行，extras 的 `]`
            # 后是引号不匹配 → 自动回溯到真闭括号。
            m_ir = re.search(r"install_requires\s*=\s*\[(.*?)\]\s*[,)\n]", sp_text, re.S)
            if m_ir:
                for item in re.findall(r"""['"]([^'"]+)['"]""", m_ir.group(1)):
                    parsed = _parse_dep_text(item)
                    if parsed and parsed[0] not in specs:
                        specs[parsed[0]] = (parsed[1], parsed[2])

    _read_manifest(root)
    try:
        # iterdir 而非 glob：py3.13 实测 Path.glob 静默吞 OSError 返 []（P-H3 教训）——
        # 「枚举失败」必须与「真没有」可辨
        subs = [e for e in root.iterdir() if e.is_dir() and not e.name.startswith(".")]
    except OSError as exc:
        logger.warning("[pypi-registry] %s 一级子目录枚举失败（%s），清单证据可能不完整",
                       root, exc)
        return specs
    for e in subs:
        if e.name in ("node_modules", ".venv", "venv", "__pycache__", "dist", "build"):
            continue
        _read_manifest(e)
    return specs


@dataclass
class ResolvedPyDep:
    """一个已解析的 PyPI 依赖（spec=写入 dependencies 的版本约束原文）。"""
    name: str          # PEP 503 归一化名
    spec: str          # ">=3.0.1" / "==2.1.0" / ""（裸名=仅内部场景，第三方必有约束）
    source: str        # project_manifest / registry / explicit
    verified: str = "verified"   # ≠verified 会被 dep_versions_unverified 账收编（P-C2 F-2）
    extras: str = ""   # extras 段原文（"[async]"）——渲染 `name+extras+spec`，绝不静默丢


def _split_spec(raw: str) -> tuple[str, str, str] | None:
    return _parse_dep_text(raw)


def resolve_pypi_deps(
    specs: list[str],
    internal_modules: set[str] | None = None,
    project_path: str | None = None,
) -> tuple[list[ResolvedPyDep], list[str], list[str]]:
    """把契约 python 依赖（裸名或 name+specifier）解析成可写进 dependencies 的 (name, spec)。

    返回 (kept, internal, dropped)。**dropped 必须同时从契约/验收标准剔除**（R53 家族病）。
    判定序（每步都有权威证据，无一步靠猜）：
      1. 内部模块（归一化名 ∈ internal_modules）→ 入 internal 返回（**不**物化——见模块
         docstring「内部模块不走 PyPI」；绝不送 PyPI 误解析同名公网包）。
      2. 显式 specifier → **按 P-C2 验证后**采用（显式版本是待验证的主张，绝非证据）：
         · specifier 非法（`@url`/乱写）→ **不判**，原样保留 verified=spec_unparsed；
         · registry 不可达 → **fail-open 保留**（R56-6 证据缺失≠否定证据）；
         · 空集/无可满足发布（pip 语义：spec 含预发布界才纳入预发布）→ 确证幻觉 → 如实丢弃。
      3. 裸名 → 工程自身清单声明（零网络）→ PyPI 最新稳定版 → 写 `>=` 下限。
         查不到 → drop。
    """
    internal = {normalize_name(m) for m in (internal_modules or set())}
    kept: list[ResolvedPyDep] = []
    internal_hit: list[str] = []
    dropped: list[str] = []
    seen: set[str] = set()
    _manifest_specs: dict[str, str] | None = None

    for raw in specs:
        parsed = _split_spec(raw)
        if not parsed:
            dropped.append(str(raw))
            continue
        name, extras, spec = parsed
        if not name or name in seen:
            continue
        if name in internal:
            seen.add(name)
            internal_hit.append(name)
            continue
        # marker（`; python_version<…`）是条件语义载体：核验只用 marker 前的版本段，
        # 保留进清单的 spec 整段带 marker（剥了=把条件依赖改写成无条件，hunter R2 M-2）。
        spec_core, _msep, marker = spec.partition(";")
        spec_core, marker = spec_core.strip(), marker.strip()
        if spec_core:
            from packaging.version import InvalidVersion, Version
            # ★P-C2 平移★ 显式 specifier 是主张不是证据——`flask==99.0` 零验证直采会烤进
            # 权威模板要 worker「原样写入」→ `pip install` 整包装不上。
            exact = re.fullmatch(r"==(?!=)\s*([^*\s]+)", spec_core)
            if exact:
                # 精确钉版：存在性核验用【全量版本集（含预发布）】——P-C2「版本集含预发布」
                # 同律：`==1.0b1` 是真存在的预发布，按稳定版集判会误杀。
                versions = registry_versions(name)
                if versions is None:
                    seen.add(name)   # 不可达 → fail-open 保留（证据缺失≠否定证据）
                    kept.append(ResolvedPyDep(name=name, spec=spec, source="explicit",
                                              verified="registry_unreachable", extras=extras))
                    continue
                # ★hunter R2 M-1★ 判等必须 PEP440 语义（`2.0 == 2.0.0`）——发布键是上传
                # 字面量，字面相等会把真钉版冤杀成「确证不存在」（与区间臂 Version 语义对称）。
                pin = exact.group(1).strip()
                try:
                    pin_v = Version(pin)
                except InvalidVersion:
                    pin_v = None
                if pin_v is not None:
                    legal: set = set()
                    for v in versions:
                        try:
                            legal.add(Version(v))
                        except InvalidVersion:
                            continue
                    exists = pin_v in legal
                else:
                    exists = pin in versions
                if exists:
                    seen.add(name)
                    kept.append(ResolvedPyDep(name=name, spec=spec, source="explicit",
                                              extras=extras))
                    continue
                dropped.append(str(raw))
                logger.warning(
                    "[pypi-registry] 契约显式钉版 %s 在 PyPI 发布集中确证不存在 → "
                    "如实丢弃（绝不逼 worker 手写幻觉版本）", raw)
                continue
            try:
                from packaging.specifiers import InvalidSpecifier, SpecifierSet
                ss = SpecifierSet(spec_core)
            except InvalidSpecifier:
                # 语法非法（复合乱写/协议串/` @ url`）→ 不判原样保留（npm 复合区间同律）
                seen.add(name)
                kept.append(ResolvedPyDep(name=name, spec=spec, source="explicit",
                                          verified="spec_unparsed", extras=extras))
                continue
            data = _pypi_json(name)
            if data is None:
                # 不可达 → fail-open 保留（离线跑一次就清空所有显式依赖=比原 bug 更坏）
                seen.add(name)
                kept.append(ResolvedPyDep(name=name, spec=spec, source="explicit",
                                          verified="registry_unreachable", extras=extras))
                continue
            # ★pip 语义（cr R-5）★ 可满足性必须拿【全量发布集（含预发布）】判：
            # `Version("1.0b2") in SpecifierSet(">=1.0b1")` = True（spec 含预发布界
            # = 项目显式允许预发布），而 `>=2.0` 不会意外放进 `2.0rc1`（未显式允许）。
            # 只对稳定版集判会把「只有预发布的真包 + 显式预发布界」冤杀成幻觉。
            rel = data.get("releases")
            satisfiable = []
            for v in (rel if isinstance(rel, dict) else ()):
                try:
                    if Version(v) in ss:
                        satisfiable.append(v)
                except InvalidVersion:
                    continue
            if satisfiable:
                seen.add(name)
                kept.append(ResolvedPyDep(name=name, spec=spec, source="explicit",
                                          extras=extras))
            else:
                # 确证无可满足发布=幻觉 → 如实丢弃
                dropped.append(str(raw))
                logger.warning(
                    "[pypi-registry] 契约显式依赖 %s 无任何可满足发布（PyPI 全量发布集"
                    "含预发布、pip 语义确证）→ 如实丢弃（绝不逼 worker 手写幻觉版本）", raw)
            continue
        # 裸名（或仅 marker）：工程自身清单声明 → registry 最新稳定版；marker 整段保留
        if _manifest_specs is None:
            _manifest_specs = project_manifest_specs(project_path)
        declared = _manifest_specs.get(name)
        if declared is not None:
            d_extras, d_spec = declared
            if marker and ";" not in d_spec:
                # ★hunter R3 L-a★ 契约裸名+marker、清单已声明同名包：契约 marker 必须
                # 拼到清单 spec 上——静默丢=把「仅某平台的条件依赖」改写成无条件。
                d_spec = f"{d_spec} ; {marker}" if d_spec else f"; {marker}"
            seen.add(name)
            kept.append(ResolvedPyDep(name=name, spec=d_spec, source="project_manifest",
                                      extras=extras or d_extras))
            continue
        latest = registry_latest_version(name)
        if latest:
            seen.add(name)
            kept.append(ResolvedPyDep(
                name=name, spec=f">={latest}" + (f" ; {marker}" if marker else ""),
                source="registry", extras=extras))
            continue
        dropped.append(str(raw))
    return kept, internal_hit, dropped
