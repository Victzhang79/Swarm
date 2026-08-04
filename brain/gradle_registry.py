"""gradle_registry：gradle 依赖版本的确定性证据层（P-H4c，27 号文「gradle 零脚手架出口」治本）。

gradle 与 maven **共用坐标体系与仓库**（Maven Central + 本地 ~/.m2）——网络/本地证据
原语直接复用 maven_registry（registry_group_for / registry_version_exists /
registry_latest_version / local_m2_groups_for / local_m2_latest_version /
bom_managed_artifacts），同受其 LOOKUP 开关门控（同义词开关：管的是「maven 仓库
查询」而非「maven 栈」，本地证据层同受约束=开关契约「关闭后=解析不到→如实丢弃」）。
本模块只造 gradle 自身的两维：

1. **工程清单证据层（零网络）**：build.gradle / build.gradle.kts 的依赖声明行——
   **regex 有界解析，绝不 eval DSL**（DSL 是图灵完备的，"解析"它=执行它）。收集序=
   根 build 文件 → 一级子目录 build 文件（按目录名 sorted，hunter R1 F-4 同律：
   iterdir 原生顺序跨机器不定）→ gradle/libs.versions.toml 版本目录**最后**（目录是
   共享默认值，直接声明优先，cr R1 #2 同律）。同名冲突=先见先收+WARNING（确定性
   选择≠静默选择，硬检查④）。
2. **resolve_gradle_deps 判定序**：
   - 显式 `g:a:v`：v 是 LLM 声明=主张非证据（R67L-B3 平移）→ 仓库核验三态
     （存在→verified / 确证查无→校正最新稳定版或如实丢弃 / 不可达→fail-open 保留
     +WARNING+verified=registry_unreachable 供 dep_versions_unverified 账收编）；
     `${...}` 属性引用不判原样保留（verified=unjudgeable）。
   - 显式 `g:a`（无版本）→ 版本链（见下）。
   - 裸名 `a`：工程清单证据（含目录）→ 组证据+版本链。
   - **版本链**：BOM 受管（platform/enforcedPlatform/mavenBom/Boot 插件自动导入证据）
     → **省略版本**（与 maven「受管不写版本」同律——写显式版本=对抗受管对齐，
     R67L-B3 的 gradle 形态）→ Central 最新稳定版 → 本地 .m2 最新 → 如实丢弃。
   - 内部模块（driver 物化 `project(":a:b")`）绝不送仓库。

★诚实边界★ regex 只认字符串字面量声明（`implementation "g:a:v"` 与 map 形态），
变量拼接/`libs.*` 别名引用读不到——读不到=该条证据缺席（交目录/仓库臂），绝不猜。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from swarm.brain import maven_registry as _mvn

logger = logging.getLogger("swarm.brain.gradle_registry")

# 依赖声明行（Groovy `implementation "g:a:v"` / Kotlin `implementation("g:a:v")`，单双
# 引号全认）。配置名集刻意从宽（L10：声明检查从宽——这些是**证据**不是处方）；
# `project(":core")` 内部引用天然不匹配（坐标串以冒号开头，第一段抓不到）。
# ★词边界（reviewer R1 CR-1）★ 前置 `(?<![\w.])`：自定义配置 `someapi "g:a:v"` 的
# 尾部 `api` 不得被当 `api` 配置的证据。
_CONFIGS = (r"(?:api|implementation|compileOnly|compileOnlyApi|runtimeOnly"
            r"|annotationProcessor|testImplementation|testCompileOnly|testRuntimeOnly"
            r"|kapt|ksp|providedCompile|providedRuntime|compile|runtime)")
_BOUND = r"(?<![\w.])"
_COORD = r"([A-Za-z0-9_.\-]+):([A-Za-z0-9_.\-]+)(?::([^\"']+))?"
_DEP_LINE_RE = re.compile(_BOUND + _CONFIGS + r"""\s*\(?\s*["']""" + _COORD)
# map 形态：implementation group: 'g', name: 'a', version: 'v'
_DEP_MAP_RE = re.compile(
    _BOUND + _CONFIGS + r"""\s*\(?\s*group\s*:\s*["']([^"']+)["']\s*,\s*name\s*:\s*["']([^"']+)["']"""
    r"""(?:\s*,\s*version\s*:\s*["']([^"']+)["'])?""")
# BOM 受管证据：platform/enforcedPlatform("g:a:v") 与 dependency-management 的 mavenBom
_PLATFORM_RE = re.compile(
    _BOUND + r"""(?:platform|enforcedPlatform)\s*\(?\s*["']""" + _COORD)
_MAVEN_BOM_RE = re.compile(_BOUND + r"""mavenBom\s*\(?\s*["']""" + _COORD)
_DM_PLUGIN = "io.spring.dependency-management"
_BOOT_PLUGIN = "org.springframework.boot"
# settings include 声明（成员清单证据的真身通道，reviewer R1 #5）。
# reviewer R2 #5：前导冒号可选——`include 'services:api'` 与 `include ':services:api'`
# 等价（gradle 官方两种都合法），只认带冒号=整条嵌套成员证据通道对无冒号写法失踪。
_INCLUDE_RE = re.compile(_BOUND + r"""include\s*\(?\s*((?:["']:?[\w:.\-]+["']\s*,?\s*)+)\)?""")
_INCLUDE_PATH_RE = re.compile(r"""["'](:?[\w:.\-]+)["']""")
# plugins 块 id 声明：version 可选，apply false=未应用（reviewer R1 #4：未应用的插件
# 不会自动导入 BOM，当受管=幻觉受管）。
# reviewer R2 HIGH-1：`id` 前挂词边界——`myid 'x' version 'y'` 这类自定义标识符的
# `id` 子串不得当插件声明（假在场=幻觉受管）。
# reviewer R2 HIGH-2 + hunter R2 HIGH：Kotlin DSL 点号链 `id("x").version("y")` 与
# `apply(false)`/`.apply(false)` 都是常见合法形态，只认 Groovy 空格形态=版本丢失
# （不导入 BOM，方向保守但错）且 apply(false) 被当真应用（幻觉受管，方向危险）。
# 连接器分两种（hunter R3 HIGH/MEDIUM 实证驱动）：
# · 点号链（Kotlin DSL）：`\s*\.\s*`——Kotlin 允许链在点前换行
#   （`id("x") version("y")\n    .apply(false)` 合法且常见），不许换行=`.apply(false)`
#   漏检=幻觉受管（误杀方向）、`.version(...)` 漏检=受管证据丢失。
# · 非点号（Groovy 空格形态）：`[ \t]+` 必须同行——Groovy 不允许裸换行续语句，
#   下行的 `version 'x'` 是另一条语句，跨行挂上=错挂版本（X1 测试锁）。
# ★尾巴 `[ \t]*` 必须折进 `\)` 的可选组里（`(?:[ \t]*\))?`）★——写成 `["'][ \t]*\)?`
# 会把连接符要吃的空格提前吃掉，可选组在当前位置失败即整体跳过（回溯栈里「跳过组」
# 比 `[ \t]*` 的回退点更近，先成功就收工），同行 `version 'x'`/`apply false` 全丢
# （probe 三连实证：带尾巴+可选组=丢；去尾巴或组变强制=好）。
_PLUGIN_DECL_TMPL = (_BOUND + r"""id[ \t]*\(?[ \t]*["']{pid}["'](?:[ \t]*\))?"""
                     r"""(?:(?:(?:\s*\.\s*)|(?:[ \t]+))version[ \t]*\(?[ \t]*["']([^"']+)["'](?:[ \t]*\))?)?"""
                     r"""((?:(?:\s*\.\s*)|(?:[ \t]+))apply[ \t]*\(?[ \t]*false[ \t]*\)?)?""")
# plugins 块头（reviewer R2 #3 + hunter R2 MEDIUM：声明判定必须限定在 plugins 块内——
# 剥注释刻意保留字符串内容，task/println 字符串里的类插件声明文本不是声明）。
# reviewer R3 MEDIUM：`plugins` 与 `{` 之间允许换行（Groovy/Kotlin DSL 合法风格），
# `[ \t]*` 会整块丢证据 → `\s*`（`_BOUND` 已挡 `buildscript` 等前缀词；`\s*` 只吃
# 空白，不会跨过 `=`/标识符误配到别的块头）。
_PLUGINS_HEAD_RE = re.compile(_BOUND + r"plugins\s*\{")

_BUILD_FILES = ("build.gradle", "build.gradle.kts")
_SETTINGS_FILES = ("settings.gradle", "settings.gradle.kts")


def _strip_comments(text: str) -> str:
    """Groovy/Kotlin 行 `//` 与块 `/* */` 注释抹成空白（**字符串内容保留**——坐标证据
    就住在字符串里；状态机尊重引号，字符串内的 `https://` 不会被当注释）。
    reviewer R1 CR-1：注释里的「假声明」（被注释掉的旧版坐标/示例）绝不能进证据层——
    先见先收会把注释版留下、给真实声明打冲突 WARNING。
    ★边界（登记）★ 字符串内容里的类坐标文本（`println('implementation "g:a:1.0"')`）
    与 slashy 正则字面量里的 `//` 仍可能命中/误判——方向保守（证据必来自仓内真实
    写过的文本，绝不臆造）。"""
    out: list[str] = []
    i, n = 0, len(text)
    state: str | None = None          # None | line | block | ' | " | ''' | """
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state == "line":
            if ch == "\n":
                state = None
                out.append(ch)
            else:
                out.append(" ")
            i += 1
        elif state == "block":
            if ch == "*" and nxt == "/":
                out.append("  ")
                i += 2
                state = None
            else:
                out.append("\n" if ch == "\n" else " ")
                i += 1
        elif state in ("'", '"'):
            out.append(ch)
            if ch == "\\" and i + 1 < n:      # 转义对原样保留（\" 不给 regex 当引号）
                out.append(text[i + 1])
                i += 2
            elif ch == state:
                state = None
                i += 1
            else:
                i += 1
        elif state in ("'''", '"""'):
            out.append(ch)
            if text.startswith(state, i + 1):
                out.append(state[0])
                out.append(state[1])
                i += 3
                state = None
            else:
                i += 1
        elif ch == "/" and nxt == "/":
            state = "line"
            out.append("  ")
            i += 2
        elif ch == "/" and nxt == "*":
            state = "block"
            out.append("  ")
            i += 2
        elif text.startswith("'''", i):
            state = "'''"
            out.append("'''")
            i += 3
        elif text.startswith('"""', i):
            state = '"""'
            out.append('"""')
            i += 3
        elif ch in ("'", '"'):
            state = ch
            out.append(ch)
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _plugins_block_bodies(text: str) -> list[str]:
    """全部 `plugins { ... }` 块的内容（花括号配对，字符串内容跳过——GString `${}`/
    字符串字面量里的花括号不参与计数，否则块边界被字符串里的 `}` 提前切断）。
    reviewer R2 #3 + hunter R2 MEDIUM：插件声明判定必须限定在 plugins 块内——
    `_strip_comments` 刻意保留字符串（坐标证据住在里面），而字符串里的类插件声明
    文本（`println("id 'x' version 'y'")`）不是声明。找不到配对闭括号 → 该块证据
    缺席+WARNING（坏清单≠没有声明，fail-honest）。"""
    bodies: list[str] = []
    for head in _PLUGINS_HEAD_RE.finditer(text):
        i = head.end()                     # 跳过 `{`，从块内容开始
        start = i
        depth = 1
        state: str | None = None           # None | ' | " | ''' | """
        n = len(text)
        while i < n and depth > 0:
            ch = text[i]
            if state in ("'", '"'):
                if ch == "\\" and i + 1 < n:
                    i += 2
                    continue
                if ch == state:
                    state = None
            elif state in ("'''", '"""'):
                if text.startswith(state, i):
                    i += 3
                    state = None
                    continue
            elif text.startswith("'''", i):
                state = "'''"
                i += 3
                continue
            elif text.startswith('"""', i):
                state = '"""'
                i += 3
                continue
            elif ch in ("'", '"'):
                state = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    bodies.append(text[start:i])
                    break
            i += 1
        if depth > 0:
            logger.warning("[gradle-registry] plugins 块闭括号配对失败，该块插件声明证据缺席")
    return bodies


def _plugin_decl(text: str, plugin_id: str) -> tuple[str | None, bool]:
    """plugins 块的 id 声明 → (version|None, 是否真应用)。
    reviewer R1 #2：判定必须用【声明形态】而非子串包含——注释提及/依赖坐标
    `implementation "io.spring.dependency-management:..."` 都不构成插件在场。
    reviewer R1 #4：`apply false` → 未应用（不会自动导入 BOM，当受管=幻觉受管）。
    reviewer R2 #4：同一插件多条声明时取【已应用】者优先（re.search 首匹配会把
    靠前的 `apply false` 行当真，盖掉后面的真应用行=幻觉不受管）；多条已应用且
    版本不一致 → WARNING + 先见先收（确定性，与清单冲突同律）。
    ★边界（登记）★ 只认 plugins 块 id 形态；`apply plugin:` 旧式与
    `alias(libs.plugins.*)` 不识别——缺席=不导入（保守方向：宁可不省版本也不错省）。
    ★边界2（reviewer R3 LOW，登记不改）★ 块【内】字符串字面量里的类插件文本仍可能
    命中——plugins DSL 语法本身禁止块内出现其它字符串，非常规输入才触达，防御备注。"""
    pat = re.compile(_PLUGIN_DECL_TMPL.format(pid=re.escape(plugin_id)))
    decls: list[tuple[str | None, bool]] = []
    for body in _plugins_block_bodies(text):
        for m in pat.finditer(body):
            decls.append((m.group(1), not bool(m.group(2))))
    if not decls:
        return None, False
    applied_versions = [v for v, ok in decls if ok]
    if applied_versions:
        distinct = {v for v in applied_versions if v}
        if len(distinct) > 1:
            logger.warning(
                "[gradle-registry] 插件 %s 多条已应用声明版本冲突 %s → 取先见者 %s",
                plugin_id, sorted(distinct), applied_versions[0])
        return applied_versions[0], True
    return decls[0][0], False


def _clean_version(v: str | None) -> str:
    """`${...}`/`$...` 属性引用不是字面版本证据 → ''（声明在、版本被接管，交版本链）。"""
    if not v:
        return ""
    v = v.strip()
    return "" if "$" in v else v


def _collect_text_specs(text: str, origin: str,
                        specs: dict[str, tuple[str, str]]) -> None:
    """一份构建文件文本的依赖声明并入 specs（先见先收+冲突 WARNING，origin 供留痕）。
    先剥注释（reviewer R1 CR-1：注释里的假声明不得进证据层）。"""
    text = _strip_comments(text)
    pairs = [(m.group(1), m.group(2), _clean_version(m.group(3)))
             for m in _DEP_LINE_RE.finditer(text)]
    pairs += [(m.group(1), m.group(2), _clean_version(m.group(3)))
              for m in _DEP_MAP_RE.finditer(text)]
    for g, a, v in pairs:
        if a in specs:
            if specs[a] != (g, v):
                logger.warning("[gradle-registry] %s 对 %s 的声明 %r 与已收录的 %r 冲突"
                               " → 保留先收者（根优先/名字序），后者被盖",
                               origin, a, (g, v), specs[a])
            continue
        specs[a] = (g, v)


def _catalog_specs(root: Path) -> dict[str, tuple[str, str]]:
    """gradle/libs.versions.toml 版本目录 → {artifact: (group, version)}（TOML=确定性
    解析，与 regex 证据层互补）。缺失→{}（本就可选）；解析失败→WARNING+{}（坏了≠
    没有，硬检查④）。"""
    ct = root / "gradle" / "libs.versions.toml"
    if not ct.is_file():
        return {}
    try:
        import tomllib
        data = tomllib.loads(ct.read_text("utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        logger.warning("[gradle-registry] %s 解析失败（%s），版本目录证据缺席", ct, exc)
        return {}
    vers_tbl = data.get("versions") if isinstance(data.get("versions"), dict) else {}
    libs = data.get("libraries")
    if not isinstance(libs, dict):
        return {}
    out: dict[str, tuple[str, str]] = {}
    for alias, entry in libs.items():
        g = a = v = None
        if isinstance(entry, str):
            parts = entry.split(":")
            if len(parts) >= 2:
                g, a = parts[0].strip(), parts[1].strip()
                v = parts[2].strip() if len(parts) > 2 else None
        elif isinstance(entry, dict):
            g, a = entry.get("group"), entry.get("name")
            ver = entry.get("version")
            if isinstance(ver, str):
                v = ver
            elif isinstance(ver, dict) and isinstance(ver.get("ref"), str):
                ref_v = vers_tbl.get(ver["ref"])
                if isinstance(ref_v, str):
                    v = ref_v
        if not (isinstance(g, str) and isinstance(a, str) and g and a):
            continue
        v = _clean_version(v if isinstance(v, str) else "")
        if a in out:
            if out[a] != (g, v):
                logger.warning("[gradle-registry] 版本目录别名 %s 对 %s 的声明 %r 与已收录"
                               "的 %r 冲突 → 保留先见者（TOML 文件序）", alias, a, (g, v), out[a])
            continue
        out[a] = (g, v)
    return out


def _settings_member_dirs(root: Path) -> list[str]:
    """settings.gradle(.kts) 的 include 声明 → 成员目录相对路径（`:a:b` → `a/b`）。
    嵌套成员清单证据的【真身通道】（reviewer R1 #5：一级子目录扫描结构性抓不到
    `services/api/build.gradle`）。读失败 → WARNING + 跳过该文件（坏了≠没有）。
    ★边界（登记）★ `project(':x').projectDir = file('...')` 重映射不识别——按默认
    约定（冒号段镜像目录）；include 多参数/换行形态全认。"""
    dirs: list[str] = []
    for name in _SETTINGS_FILES:
        ct = root / name
        if not ct.is_file():
            continue
        try:
            text = _strip_comments(ct.read_text("utf-8", errors="replace"))
        except OSError as exc:
            logger.warning("[gradle-registry] %s 读取失败（%s），成员目录证据缺席", ct, exc)
            continue
        for m in _INCLUDE_RE.finditer(text):
            for q in _INCLUDE_PATH_RE.finditer(m.group(1)):
                d = q.group(1).strip(":").replace(":", "/")
                if d and d not in dirs:
                    dirs.append(d)
    return dirs


def project_manifest_specs(project_path: str | None) -> dict[str, tuple[str, str]]:
    """工程自身 gradle 清单声明 {artifact: (group, version)}（零网络中间证据层，
    P-H3 平移；version ''=声明在但版本被属性/受管接管）。

    收集序（cr R1 #2 同律：真实声明 > 共享默认）：根 build 文件 → 一级子目录 build
    文件（目录名 sorted）→ 版本目录**最后**。与网络查询同受 maven LOOKUP 门控
    （开关契约同形）。

    ★诚实边界（cr R1 #4 同型，登记不改）★ 子目录扫描不按 settings.gradle include
    过滤——非模块目录（buildSrc 等）的声明也会进证据层，方向保守（证据必来自仓内
    真实声明，绝不臆造）。
    """
    if not project_path or not _mvn._lookup_enabled():
        return {}
    root = Path(project_path)
    try:
        if not root.is_dir():
            return {}
    except OSError:
        return {}
    specs: dict[str, tuple[str, str]] = {}

    def _read(ct: Path) -> str | None:
        try:
            return ct.read_text("utf-8", errors="replace")
        except OSError as exc:
            # 硬检查④：读失败 ≠ 没有声明
            logger.warning("[gradle-registry] %s 读取失败（%s），该清单声明证据缺席", ct, exc)
            return None

    for name in _BUILD_FILES:
        ct = root / name
        if ct.is_file():
            text = _read(ct)
            if text is not None:
                _collect_text_specs(text, f"根/{name}", specs)
    try:
        subs = sorted((e for e in root.iterdir() if e.is_dir() and not e.name.startswith(".")),
                      key=lambda e: e.name)
    except OSError as exc:
        logger.warning("[gradle-registry] %s 一级子目录枚举失败（%s），清单证据可能不完整",
                       root, exc)
        return specs
    for e in subs:
        if e.name in ("build", "gradle", ".git", ".gradle"):
            continue
        for name in _BUILD_FILES:
            ct = e / name
            if ct.is_file():
                text = _read(ct)
                if text is not None:
                    _collect_text_specs(text, f"{e.name}/{name}", specs)
    # ★嵌套成员（reviewer R1 #5）★ settings include 声明的成员目录（`services/api`）
    # 是真身通道——一级扫描抓不到的嵌套模块清单从这里进证据层（sorted 保确定性）。
    for d in sorted(_settings_member_dirs(root)):
        if "/" not in d:                      # 一级成员已被上面的扫描覆盖
            continue
        for name in _BUILD_FILES:
            ct = root / d / name
            if ct.is_file():
                text = _read(ct)
                if text is not None:
                    _collect_text_specs(text, f"{d}/{name}", specs)
    # 版本目录=共享默认，最后收（先见先收 ⇒ 只补直接声明没覆盖的）
    for a, gv in _catalog_specs(root).items():
        if a in specs:
            if specs[a] != gv:
                logger.warning("[gradle-registry] 版本目录对 %s 的声明 %r 与已收录的 %r 冲突"
                               " → 保留先收者（直接声明优先于共享默认）", a, gv, specs[a])
            continue
        specs[a] = gv
    return specs


def root_bom_managed(project_path: str | None) -> dict[str, str]:
    """根构建文件的 BOM 受管坐标 {artifactId: groupId}（platform/enforcedPlatform/
    mavenBom 声明 + dependency-management 插件在场时的 Boot 插件自动导入）。
    零证据 → {}（缺席如实，不告警——BOM 本就可选）。"""
    if not project_path or not _mvn._lookup_enabled():
        return {}
    root = Path(project_path)
    boms: list[tuple[str, str, str]] = []
    for name in _BUILD_FILES:
        ct = root / name
        if not ct.is_file():
            continue
        try:
            text = _strip_comments(ct.read_text("utf-8", errors="replace"))
        except OSError as exc:
            logger.warning("[gradle-registry] %s 读取失败（%s），BOM 证据缺席", ct, exc)
            continue
        boms += [(m.group(1), m.group(2), m.group(3) or "")
                 for m in _PLATFORM_RE.finditer(text)]
        # ★声明形态判定（reviewer R1 #2/#4）★ 子串包含会被注释/坐标误触发；
        # `apply false` 的插件不应用=不会自动导入 BOM。
        _, dm_applied = _plugin_decl(text, _DM_PLUGIN)
        if dm_applied:
            boms += [(m.group(1), m.group(2), m.group(3) or "")
                     for m in _MAVEN_BOM_RE.finditer(text)]
            boot_ver, boot_applied = _plugin_decl(text, _BOOT_PLUGIN)
            if boot_applied and boot_ver:
                boms.append(("org.springframework.boot",
                             "spring-boot-dependencies", boot_ver))
    managed: dict[str, str] = {}
    for g, a, v in boms:
        v = _clean_version(v)
        if not v:
            continue
        for art, grp in _mvn.bom_managed_artifacts(g, a, v).items():
            if art in managed and managed[art] != grp:
                logger.warning("[gradle-registry] BOM 受管冲突：%s 已受管于 %s，%s:%s:%s "
                               "又声明 %s → 保留先收者（声明序）", art, managed[art], g, a, v, grp)
                continue
            managed.setdefault(art, grp)
    return managed


@dataclass
class ResolvedGradleDep:
    """一个已解析的 gradle 依赖（implementation 配置，模块编译期）。"""
    group: str
    artifact: str
    version: str | None     # None=受管省略（写显式版本=对抗受管对齐，R67L-B3 同律）
    source: str             # project_manifest（含版本目录）/ bom_managed /
                            # maven_central / local_m2 / explicit
    verified: str = "verified"   # ≠verified 会被 dep_versions_unverified 账收编（P-C2 F-2）
    raw: str = ""           # 不判形态（${...} 属性引用）→ 原样保留的坐标串


def _latest_stable(group: str, artifact: str) -> tuple[str | None, str]:
    """最新稳定版：Central → 本地 .m2（离线兜底证据）。返回 (version|None, source)。"""
    latest = _mvn.registry_latest_version(group, artifact)
    if latest:
        return latest, "maven_central"
    latest = _mvn.local_m2_latest_version(group, artifact)
    if latest:
        return latest, "local_m2"
    return None, ""


def resolve_gradle_deps(artifacts: list, *, internal_modules: set[str] | None = None,
                        project_path: str | None = None,
                        ) -> tuple[list[ResolvedGradleDep], list[str], list[str]]:
    """契约 gradle 依赖 → (kept, internal_hit, dropped)。

    dropped 必须同时从契约/验收剔除（R53 同律：否则逼 worker 手写幻影坐标）。
    内部模块（driver 物化 project(":a:b")）绝不送仓库（cr#2/hunter#1 同律）。
    """
    internal = internal_modules or set()
    kept: list[ResolvedGradleDep] = []
    internal_hit: list[str] = []
    dropped: list[str] = []
    seen: set[tuple[str, str]] = set()
    _specs: dict | None = None
    _managed: dict | None = None

    for raw in artifacts:
        spec = str(raw).strip()
        if not spec:
            continue
        # 坐标里绝不允许引号/反斜杠（否则注入 Groovy/Kotlin 字符串=模板污染）
        if any(c in spec for c in ('"', "'", "\\")):
            dropped.append(spec)
            logger.warning("[gradle-registry] 契约依赖 %r 含引号/反斜杠（非法坐标）→ "
                           "如实丢弃", spec)
            continue
        parts = [p.strip() for p in spec.split(":")]
        if len(parts) == 1:
            group = None
            artifact = parts[0]
            version = None
            explicit = False
        else:
            group, artifact = parts[0], parts[1]
            version = parts[2] if len(parts) == 3 and parts[2] else None
            explicit = True
        key = (group or "", artifact)
        if key in seen:
            continue
        if len(parts) > 3:
            # g:a:v:classifier 等超集形态 → 不判原样保留（猜语义误杀比放过更坏）
            seen.add(key)
            kept.append(ResolvedGradleDep("", "", None, source="explicit",
                                          verified="unjudgeable", raw=spec))
            continue

        # 内部模块判定（裸名；显式坐标含 group 的一定不是内部引用——gradle 内部
        # 引用没有坐标形态，边界登记：内部只认裸标签）
        if not explicit and artifact in internal:
            seen.add(key)
            internal_hit.append(artifact)
            continue

        if explicit:
            if version is not None and "$" in version:
                # `${...}` 属性引用不判原样保留（maven 臂同律）
                seen.add(key)
                kept.append(ResolvedGradleDep(group, artifact, version, source="explicit",
                                              verified="unjudgeable"))
                continue
            if version is not None:
                exists = _mvn.registry_version_exists(group, artifact, version)
                if exists is True:
                    seen.add(key)
                    kept.append(ResolvedGradleDep(group, artifact, version,
                                                  source="explicit"))
                elif exists is False:
                    latest, lsrc = _latest_stable(group, artifact)
                    if latest:
                        logger.warning("[gradle-registry] 契约显式坐标 %s 的版本 %s 仓库确证"
                                       "查无（幻觉版本）→ 校正到最新稳定版 %s（R67L-B3 平移）",
                                       spec, version, latest)
                        seen.add(key)
                        kept.append(ResolvedGradleDep(group, artifact, latest, source=lsrc))
                    else:
                        logger.warning("[gradle-registry] 契约显式坐标 %s 的版本 %s 仓库确证"
                                       "查无且无可用稳定版 → 如实丢弃（绝不逼 worker 臆造）",
                                       spec, version)
                        dropped.append(spec)
                else:
                    # 仓库不可达 → fail-open 保留（证据缺失≠否定证据）+ 账收编
                    logger.warning("[gradle-registry] 契约显式坐标 %s 的版本 %s 仓库不可达，"
                                   "未经证实 → fail-open 保留（执行期 L1 闸兜底）", spec, version)
                    seen.add(key)
                    kept.append(ResolvedGradleDep(group, artifact, version, source="explicit",
                                                  verified="registry_unreachable"))
                continue
            # g:a 无版本 → 落入下方版本链（group 已知）

        # 裸名/无版本：工程清单证据 → BOM 受管（省略版本）→ 最新稳定版 → 如实丢弃
        if _specs is None:
            _specs = project_manifest_specs(project_path)
        if not explicit and artifact in _specs:
            g, v = _specs[artifact]
            if v:
                seen.add(key)
                kept.append(ResolvedGradleDep(g, artifact, v, source="project_manifest"))
                continue
            group = group or g      # 组证据在、版本被接管 → 走版本链
        if _managed is None:
            _managed = root_bom_managed(project_path)
        if artifact in _managed and (group is None or _managed[artifact] == group):
            seen.add(key)
            kept.append(ResolvedGradleDep(group or _managed[artifact], artifact, None,
                                          source="bom_managed"))
            continue
        if group is None:
            grp = _mvn.registry_group_for(artifact)
            if grp:
                group = grp
            else:
                local_gs = _mvn.local_m2_groups_for(artifact)
                if len(local_gs) == 1:
                    group = next(iter(local_gs))
        if group:
            latest, lsrc = _latest_stable(group, artifact)
            if latest:
                seen.add(key)
                kept.append(ResolvedGradleDep(group, artifact, latest, source=lsrc))
                continue
        dropped.append(spec)
        logger.warning("[gradle-registry] 契约依赖 %r 无法确定性解析坐标 → 如实丢弃"
                       "（绝不逼 worker 手写幻觉坐标）", spec)
    return kept, internal_hit, dropped
