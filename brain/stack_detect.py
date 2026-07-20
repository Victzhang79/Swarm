"""项目技术栈/架构识别（plan 前预处理）——磁盘事实为准，确定性优先、模型仅兜底。

工程定位（治本 task 8537fa5e：tech_design 在 Thymeleaf 单体里产 Vue 死代码）：
栈是【磁盘的客观属性】，不是检索知识、更不该靠模型先验或需求文档假设。本模块：
  ① 确定性扫描磁盘明显信号（构建/清单文件 + 框架 marker + 目录结构 + 扩展名分布）→
     高置信直接出栈画像（零模型成本）；
  ② 仅当信号歧义/稀疏（置信低）才交由上层调一次大模型据【证据】裁决；
  ③ 画像按【repo 指纹】缓存进 projects.config，下次任务命中即复用，repo 变更才重测；
  ④ 与需求文档冲突一律以本探测为准。

返回的 profile 是单一事实源，供 tech_design / plan / worker 统一消费。
纯函数（不连 DB、不调 LLM），可单测；DB 缓存与模型兜底由 node 层组合。
"""
from __future__ import annotations

import hashlib
import os
import re
from collections import Counter

# ── 信号表（框架无关的探测，但内置常见框架 marker 以提高判定精度）──

# 构建/依赖清单文件 → 后端语言+构建工具
_MANIFEST_BACKEND = {
    "pom.xml": ("java", "maven"),
    "build.gradle": ("java", "gradle"),
    "build.gradle.kts": ("kotlin", "gradle"),
    "build.sbt": ("scala", "sbt"),
    "requirements.txt": ("python", "pip"),
    "pyproject.toml": ("python", "pip"),
    "setup.py": ("python", "pip"),
    "Pipfile": ("python", "pipenv"),
    "manage.py": ("python", "django-cli"),
    "go.mod": ("go", "go"),
    "Cargo.toml": ("rust", "cargo"),
    "composer.json": ("php", "composer"),
    "Gemfile": ("ruby", "bundler"),
    "mix.exs": ("elixir", "mix"),
    "pubspec.yaml": ("dart", "pub"),
}

# 后端框架 marker：在清单文件内容里出现即判定（substring 匹配，小写）
# E9-2：DB 引擎依赖词表（清单文本 ground truth；多栈对称：Java/Python/Node/Go/Ruby 驱动坐标）
_DB_DEP_MARKERS: dict[str, tuple[str, ...]] = {
    "mysql": ("mysql-connector", "mariadb-java-client", "pymysql", "mysqlclient",
              "mysql2", "go-sql-driver/mysql", "jdbc:mysql"),
    "postgres": ("org.postgresql", "postgresql</artifactId>", "psycopg", "asyncpg",
                 "node-postgres", "\"pg\"", "jackc/pgx", "jdbc:postgresql"),
}

_BACKEND_FRAMEWORK_MARKERS = {
    "spring-boot": "Spring Boot",
    "spring-webmvc": "Spring MVC",
    "springframework": "Spring",
    "quarkus": "Quarkus",
    "micronaut": "Micronaut",
    "django": "Django",
    "flask": "Flask",
    "fastapi": "FastAPI",
    "express": "Express",
    "@nestjs": "NestJS",
    "next": "Next.js",
    "gin-gonic": "Gin",
    "laravel": "Laravel",
    "symfony": "Symfony",
    "rails": "Rails",
}

# 服务端模板扩展名 → 模板引擎候选
_TEMPLATE_EXT_ENGINE = {
    ".jsp": "JSP",
    ".ftl": "FreeMarker",
    ".ftlh": "FreeMarker",
    ".vm": "Velocity",
    ".erb": "ERB",
    ".twig": "Twig",
    ".cshtml": "Razor",
    ".gohtml": "Go template",
    ".mustache": "Mustache",
    ".hbs": "Handlebars",
}
# .html 归到服务端模板需结合 templates 目录 + 后端模板依赖佐证（见下）
_TEMPLATE_EXTS = set(_TEMPLATE_EXT_ENGINE) | {".html", ".htm"}
_SPA_EXTS = {".vue": "Vue", ".svelte": "Svelte"}  # .jsx/.tsx 需配 react 依赖
_SPA_JSX_EXTS = {".jsx", ".tsx"}

# 前端模板依赖 marker（出现在后端清单 → .html 可判为服务端模板而非静态/SPA 产物）
_SERVER_TEMPLATE_DEP = {
    "thymeleaf": "Thymeleaf",
    "freemarker": "FreeMarker",
    "velocity": "Velocity",
    "jstl": "JSP/JSTL",
}

_NOISE_DIRS = {".git", "node_modules", "target", "dist", "build", ".venv",
               "__pycache__", ".idea", ".codegraph", "vendor", ".gradle"}


def compute_repo_fingerprint(project_path: str) -> str:
    """repo 指纹：构建/清单文件的(相对路径+大小) + 顶层目录名集合 的稳定哈希。

    栈相关文件（pom/package.json/go.mod...）或顶层结构变化时指纹改变 → 缓存失效重测；
    日常源码改动不动这些 → 指纹稳定、缓存命中。廉价、确定性。
    """
    parts: list[str] = []
    try:
        top = sorted(
            d for d in os.listdir(project_path)
            if os.path.isdir(os.path.join(project_path, d)) and d not in _NOISE_DIRS
        )
        parts.append("dirs:" + ",".join(top))
        for root, dirs, files in os.walk(project_path):
            dirs[:] = [d for d in dirs if d not in _NOISE_DIRS]
            rel = os.path.relpath(root, project_path)
            if rel.count(os.sep) > 3:
                dirs[:] = []
                continue
            for f in files:
                if f in _MANIFEST_BACKEND or f in ("package.json", "angular.json") \
                        or f.endswith((".csproj", "next.config.js", "vite.config.js")):
                    p = os.path.join(root, f)
                    try:
                        sz = os.path.getsize(p)
                    except OSError:
                        sz = 0
                    rp = os.path.relpath(p, project_path)
                    parts.append(f"{rp}:{sz}")
    except OSError:
        pass
    blob = "|".join(sorted(parts))
    return hashlib.sha256(blob.encode("utf-8", "ignore")).hexdigest()[:16]


def _read_text(path: str, limit: int = 20000) -> str:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read(limit).lower()
    except OSError:
        return ""


# ── 基建符号锚点：worker 常按训练惯性臆造的【基础设施类】概念 → 真实存在性钉死 ──
# 治本场景：本地模型实现新功能时引用框架"标准类"（如新版 RuoYi 的 RedisCache），但当前
# 项目变体根本没有该类（经典 Shiro 变体用 CacheUtils/EhCache）→ `cannot find symbol`/
# `package X does not exist` → 确定性 import 修复查无权威前缀（类真不存在）→ worker 反复
# 重写同一臆造类撞迭代上限死循环。解法：扫项目真实存在的基建类，钉进栈画像喂 design+worker。
# 每个概念用【类名(=文件名)】匹配，记录其真实 FQN（包名大小写敏感，故不用 _read_text 小写版）。
_INFRA_CONCEPTS: tuple[tuple[str, "callable"], ...] = (
    ("缓存", lambda n: "cache" in n.lower()),                       # CacheUtils/CacheService/RedisCache
    ("Redis/会话存储", lambda n: n.lower().startswith("redis")),     # RedisCache/RedisTemplate 封装
    ("统一响应封装", lambda n: n in (
        "AjaxResult", "R", "Result", "ApiResult", "ResponseResult", "RestResult", "ResultData")),
    ("基础实体基类", lambda n: n in ("BaseEntity", "BaseDO", "BaseDomain", "BaseModel")),
    ("鉴权/安全工具", lambda n: n in (
        "SecurityUtils", "ShiroUtils", "TokenService", "SecurityContextHolder", "LoginUser", "AuthUtils")),
    # round11 治本②：列出本项目真实 *Utils（如 CipherUtils/AlarmEncryptUtils），让模型用真有的、
    # 知其真实包名，少臆造 com.ruoyi.common.utils.SecurityUtils 这类"标准类"。catch-all 放最后，
    # named 桶先匹配(per-class break)，其余 *Utils 落此桶。
    ("项目工具类", lambda n: n.endswith("Utils") or n.endswith("Util")),
)


def _is_infra_classname(name: str) -> bool:
    """类名（=文件名去 .java）是否命中任一基建概念。"""
    return any(match(name) for _label, match in _INFRA_CONCEPTS)


# R65E8-T5：public 方法签名解析——把 grounding 从【类 FQN】升到【类 FQN + 方法签名】。
# 死因：class 级 grounding 告诉 worker "CacheUtils 存在"，但不给方法签名 → worker 凭惯性调
# `.set/.get`（裸 RedisTemplate 签名）而非真实的 `get/put/remove` → cannot find symbol 死循环。
# 确定性解析（不依赖 reranker 天花板）：单行 `public [modifiers] <ret> name(params)`（Allman 花括号
# 换行安全）→ 记 `[static ]name(params)`。排除 private/构造器/字段（无返回型双 token 判据 + 必带 `{`）。
_PUBLIC_METHOD_RE = None  # 延迟编译（模块级 re 已在别处 import）
_MAX_METHODS_PER_CLASS = 12


def _strip_comments_and_strings(text: str) -> str:
    """置空 Java 注释与字符串字面量内容（保留结构）→ 供签名正则免受"注释/字符串里的伪
    public 方法"污染（复核 F1 CONFIRMED HIGH：javadoc 示例 / 模板字符串里的 `public foo(){`
    会被当真方法签名喂给 worker、以'硬约束'权威误导——正是本特性要消灭的幻觉）。

    先剥块注释（含 javadoc）→ 行注释 → 字符串内容置空。此序对"字符串里含 // 或 /*"只会
    多剥（→漏掉真方法=可接受假阴性），绝不会漏剥出伪签名（不产假阳性）。同 lombok 探测器
    先剥注释再匹配的既有纪律。"""
    import re as _re
    text = _re.sub(r"/\*.*?\*/", " ", text or "", flags=_re.S)   # 块注释/javadoc
    text = _re.sub(r"//[^\n]*", " ", text)                        # 行注释
    text = _re.sub(r'"(?:\\.|[^"\\\n])*"', '""', text)            # 字符串字面量内容置空
    return text


def _extract_public_method_sigs(text: str) -> list[str]:
    """从 Java 类源码解析 public 方法签名 → ["[static ]name(params)", ...]，去重保序、封顶。

    纯函数、可单测。保守：先剥注释/字符串（防伪签名），只认单行签名带开花括号的具体方法
    （排除 interface `;` 声明、字段、构造器<无返回型>、private/protected）。参数折叠空白、
    单条封顶 140 字防 prefill 爆炸。"""
    import re as _re
    global _PUBLIC_METHOD_RE
    if _PUBLIC_METHOD_RE is None:
        _PUBLIC_METHOD_RE = _re.compile(
            r"\bpublic\s+([^\n(){};=]*?)\s*\(([^;{()]*)\)\s*(?:throws[\w.,\s]*?)?\{")
    text = _strip_comments_and_strings(text)
    out: list[str] = []
    seen: set[str] = set()
    for m in _PUBLIC_METHOD_RE.finditer(text or ""):
        head = m.group(1).split()
        if len(head) < 2:          # 只有一个 token = 构造器/无返回型 → 跳过
            continue
        name = head[-1]
        if not name.isidentifier():
            continue
        is_static = "static" in head[:-1]
        params = _re.sub(r"\s+", " ", m.group(2)).strip()
        sig = ("static " if is_static else "") + f"{name}({params})"
        if len(sig) > 140:
            sig = sig[:137] + "…)"
        if sig not in seen:
            seen.add(sig)
            out.append(sig)
        if len(out) >= _MAX_METHODS_PER_CLASS:
            break
    return out


def _detect_infra_symbols(
    infra_class_paths: list[str],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """扫基建类文件 → ({概念: [真实FQN,...]}, {FQN: [方法签名,...]})。供 format_stack_for_prompt
    钉死给 design+worker。

    包名大小写敏感（worker import 要原样），故用独立 case-preserving 读取，不复用 _read_text。
    每概念封顶 6 个去重 FQN，防噪声/超长 prefill。R65E8-T5：同一次读取顺带解析 public 方法签名
    （读满 20KB 以覆盖方法体），只为**保留下来的** FQN 记签名，避免为被裁掉的类白解析。
    """
    import re as _re
    by_concept: dict[str, list[str]] = {}
    methods_by_fqn: dict[str, list[str]] = {}
    seen: set[str] = set()
    pkg_re = _re.compile(r"^\s*package\s+([\w.]+)\s*;", _re.MULTILINE)
    for path in infra_class_paths[:200]:
        cls = os.path.basename(path)[:-5]  # 去 .java
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                head = f.read(20000)  # package 在文件头；20KB 覆盖 public 方法签名（T5）
        except OSError:
            continue
        m = pkg_re.search(head)
        if not m:
            continue
        fqn = f"{m.group(1)}.{cls}"
        if fqn in seen:
            continue
        seen.add(fqn)
        for label, match in _INFRA_CONCEPTS:
            if match(cls):
                lst = by_concept.setdefault(label, [])
                if len(lst) < 6:
                    lst.append(fqn)
                    sigs = _extract_public_method_sigs(head)
                    if sigs:
                        methods_by_fqn[fqn] = sigs
                break
    return by_concept, methods_by_fqn


def _detect_auth_variant(java_sample_paths: list[str], build_text: str) -> dict | None:
    """探测鉴权框架变体：Apache Shiro vs Spring Security（round11 治本②）。

    现场：基线是经典 Shiro 变体（@RequiresPermissions×17 + ShiroUtils），但不同子任务各自臆测
    变体——有的用 Spring-Security 的 @PreAuthorize("@ss.hasPermi") 还往 pom 塞 spring-boot-
    starter-security（本项目无 @ss bean），有的用 ShiroUtils——同模块鉴权分裂、SecurityUtils 臆造。
    据磁盘 ground truth 把变体钉死，format_stack_for_prompt 显式禁用另一变体的 canonical 类。
    返回 None 表示无鉴权信号（不污染画像）。
    """
    bt = (build_text or "").lower()
    shiro = springsec = 0
    if "org.apache.shiro" in bt or "shiro-spring" in bt:
        shiro += 3
    if "spring-boot-starter-security" in bt or "spring-security" in bt:
        springsec += 3
    for p in java_sample_paths[:120]:
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                txt = f.read(8000)
        except OSError:
            continue
        if "@RequiresPermissions" in txt or "ShiroUtils" in txt or "org.apache.shiro" in txt:
            shiro += 1
        if "@PreAuthorize" in txt or "@ss.hasPermi" in txt or "hasPermi(" in txt \
                or "org.springframework.security" in txt:
            springsec += 1
    if shiro == 0 and springsec == 0:
        return None
    variant = "shiro" if shiro >= springsec else "spring-security"
    return {"variant": variant, "shiro_hits": shiro, "springsec_hits": springsec}


def _detect_jvm_facts(
    project_path: str, manifest_texts: dict[str, str], java_sample_paths: list[str]
) -> dict | None:
    """JVM 系专属事实：Jakarta/Javax 命名空间 + Spring Boot 大版本 + Java 版本。

    这些是 worker 写对 import 的【硬前提】——Spring Boot 3/4 用 jakarta.*，2.x 用 javax.*；
    本地模型常按训练惯性写 javax 导致 `package javax.servlet does not exist`（实测 RuoYi
    E2E st-3 等 8 个子任务因此卡到迭代上限）。这里据磁盘 ground truth 把命名空间钉死。

    判定优先级：现存源码 import 实证 > 构建清单版本推断。源码 0 命中时回退版本推断。
    返回 None 表示非 JVM 项目（不污染非 Java 栈画像）。
    """
    # 根 pom / gradle 文本（manifest_texts 按 basename 去重，只够推版本，源码实证才是主依据）
    root_pom = _read_text(os.path.join(project_path, "pom.xml"))
    build_text = root_pom + " " + " ".join(manifest_texts.values())
    is_jvm = bool(root_pom) or any(
        k in manifest_texts for k in ("pom.xml", "build.gradle", "build.gradle.kts")
    )
    if not is_jvm:
        return None

    # ── 1) 源码实证：现存 .java 里 jakarta.* vs javax.*（servlet/persistence/validation/annotation）──
    jakarta_hits = 0
    javax_hits = 0
    for p in java_sample_paths[:120]:
        head = _read_text(p, limit=4000)  # 已小写；import 在文件头部
        jakarta_hits += head.count("import jakarta.")
        javax_hits += head.count("import javax.servlet") + head.count("import javax.persistence") \
            + head.count("import javax.validation") + head.count("import javax.annotation")

    namespace = ""
    ns_source = ""
    if jakarta_hits or javax_hits:
        namespace = "jakarta" if jakarta_hits >= javax_hits else "javax"
        ns_source = f"源码实证(jakarta×{jakarta_hits} vs javax×{javax_hits})"

    # ── 2) Spring Boot 大版本（推断命名空间的兜底 + 独立事实）──
    boot_version = ""
    m = re.search(r"<spring-boot\.version>\s*([0-9]+(?:\.[0-9]+)*)", build_text)
    if not m:
        m = re.search(r"spring-boot-starter-parent[^0-9]{0,80}?([0-9]+\.[0-9]+\.[0-9]+)", build_text)
    if not m:
        m = re.search(r"org\.springframework\.boot[:'\" ]+spring-boot[^0-9]{0,40}?([0-9]+\.[0-9]+\.[0-9]+)", build_text)
    if m:
        boot_version = m.group(1)
        if not namespace:  # 源码无实证时用版本推断：Boot ≥3 → jakarta
            try:
                major = int(boot_version.split(".")[0])
                namespace = "jakarta" if major >= 3 else "javax"
                ns_source = f"Spring Boot {boot_version} 推断"
            except ValueError:
                pass

    # ── 3) Java 版本 ──
    java_version = ""
    jm = re.search(r"<java\.version>\s*([0-9]+)", build_text) \
        or re.search(r"<maven\.compiler\.(?:source|release)>\s*([0-9]+)", build_text) \
        or re.search(r"sourcecompatibility[\s=:'\"]+(?:javaversion\.version_)?([0-9]+)", build_text)
    if jm:
        java_version = jm.group(1)

    # ── 4) Lombok 基线在位性（R65TR-T5，jakarta/javax 同型病：模型训练先验 vs 磁盘
    # ground truth）——基线无 Lombok 时交付引入 @Data 等注解=基线约定漂移：JDK≥23 默认
    # 关闭隐式注解处理必炸（回放实锤 112 处找不到符号），且无调用者的 @Data 类在
    # Lombok 失效时静默编译通过=跨模块哑弹。构建清单/源码双证。
    # 猎手 F2：裸 "lombok" 子串会被【蓄意传递排除块】骗真（企业常见：exclusion 挡三方
    # starter 传递引入 lombok，本项目根本没启用）→ 误放行=探测器自己复现要防的哑弹。
    # 先剥 XML 注释/Maven exclusion 块/Gradle exclude 行，再按真实坐标 org.projectlombok 判。
    _bt = re.sub(r"<!--.*?-->", " ", build_text, flags=re.S)
    _bt = re.sub(r"<exclusions?>.*?</exclusions?>", " ", _bt, flags=re.S)
    _bt = "\n".join(ln for ln in _bt.splitlines() if "exclude" not in ln)
    _lombok_build = "org.projectlombok" in _bt
    _lombok_src = 0
    for p in java_sample_paths[:120]:
        # 猎手 F5：锚定 `import lombok.`（防 lombokx.* 类前缀假阳）
        _lombok_src += _read_text(p, limit=4000).count("import lombok.")
    lombok_available = bool(_lombok_build or _lombok_src)
    lombok_source = (
        ("构建清单" if _lombok_build else "")
        + ("+" if _lombok_build and _lombok_src else "")
        + (f"源码实证×{_lombok_src}" if _lombok_src else "")
    ) or "基线双证均无"

    if not (namespace or boot_version or java_version):
        return None
    return {
        "servlet_namespace": namespace,        # 'jakarta' | 'javax' | ''
        "namespace_source": ns_source,
        "spring_boot_version": boot_version,
        "java_version": java_version,
        "lombok_available": lombok_available,  # R65TR-T5：基线注解处理器在位性
        "lombok_source": lombok_source,
    }


def baseline_lombok_present(project_path: str) -> bool | None:
    """R65E10-T2：基线是否【真实启用】Lombok（供契约依赖剥除做正确方向判定）。

    与 _detect_jvm_facts 内联探测【同一】识别口径（单一事实源）：剥 XML 注释/Maven exclusion 块/
    Gradle exclude 行后按真实坐标 org.projectlombok 判构建清单；再看源码 `import lombok.`。
    猎手 F2 同律：lombok 仅出现在 <exclusions>（挡传递）≠ 启用。

    返回 True/False；project_path 无效/无任何构建清单+源码可读=无法判定 → None（调用方 fail-open
    保守【不剥】，绝不把"探测不到"当"基线无 lombok"误删真在用依赖致编译断裂）。
    """
    try:
        if not os.path.isdir(project_path):
            return None
        # 有限 walk（大 monorepo 封顶）收集 pom/gradle 构建清单 + .java 采样
        build_texts: list[str] = []
        java_paths: list[str] = []
        _seen_dirs = 0
        for dirpath, dirnames, filenames in os.walk(project_path):
            _seen_dirs += 1
            if _seen_dirs > 2400:
                break
            dirnames[:] = [d for d in dirnames
                           if d not in (".git", "target", "node_modules", ".idea", "build")]
            for fn in filenames:
                if fn == "pom.xml" or fn in ("build.gradle", "build.gradle.kts"):
                    if len(build_texts) < 200:
                        build_texts.append(_read_text(os.path.join(dirpath, fn), limit=20000))
                elif fn.endswith(".java") and len(java_paths) < 200:
                    java_paths.append(os.path.join(dirpath, fn))
        if not build_texts:
            return None
        _bt = "\n".join(build_texts)
        _bt = re.sub(r"<!--.*?-->", " ", _bt, flags=re.S)
        _bt = re.sub(r"<exclusions?>.*?</exclusions?>", " ", _bt, flags=re.S)
        _bt = "\n".join(ln for ln in _bt.splitlines() if "exclude" not in ln)
        if "org.projectlombok" in _bt:
            return True
        # 源码实证：采样 .java 的 `import lombok.`（_read_text 已小写，anchor 小写）
        for jp in java_paths:
            if "import lombok." in _read_text(jp, limit=4000):
                return True
        return False
    except Exception:  # noqa: BLE001 — 探测异常=无法判定→None（调用方 fail-open）
        return None


def detect_stack_deterministic(project_path: str, max_dirs: int = 2400) -> dict:
    """确定性磁盘探测 → project_stack 画像 + 置信度 + 证据。不调 LLM、不连 DB。

    返回 dict：
      {frontend, backend, build, frontend_kind('server-template'|'spa'|'separated'|'none'),
       confidence(0-1), evidence[list[str]], signals{...}, needs_model_adjudication(bool)}
    """
    manifests: list[str] = []
    manifest_texts: dict[str, str] = {}
    ext_counts: Counter = Counter()
    tmpl_count = 0
    template_engine_hits: Counter = Counter()
    spa_files: Counter = Counter()
    jsx_count = 0
    has_angular = False
    has_next = False
    has_vite = False
    frontend_proj_dirs: list[str] = []
    java_sample_paths: list[str] = []  # 采样 .java 路径（判 javax/jakarta 命名空间用）
    infra_class_paths: list[str] = []  # 基建类 .java 路径（钉死真实可用符号，治本臆造类）
    dir_count = 0

    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in _NOISE_DIRS]
        rel = os.path.relpath(root, project_path)
        if rel == ".":
            rel = ""
        dir_count += 1
        if dir_count > max_dirs:
            break
        for f in files:
            _, ext = os.path.splitext(f)
            ext = ext.lower()
            if ext:
                ext_counts[ext] += 1
            # 采样 .java（优先 src/main/java 下的）供命名空间判定，封顶 120 个防大仓拖慢
            if ext == ".java" and len(java_sample_paths) < 120:
                if "src/main/java" in (rel.replace(os.sep, "/") + "/"):
                    java_sample_paths.append(os.path.join(root, f))
            # 顺带收集【基建类】.java 路径（类名=文件名匹配基建概念）供 _detect_infra_symbols
            # 钉死真实可用符号——治本 worker 臆造不存在的框架类（如 RedisCache）。封顶防大仓拖慢。
            if ext == ".java" and len(infra_class_paths) < 200 and _is_infra_classname(f[:-5]):
                infra_class_paths.append(os.path.join(root, f))
            low = f.lower()
            if f in _MANIFEST_BACKEND or f.endswith(".csproj"):
                manifests.append(os.path.join(rel, f) if rel else f)
                # R65TR-T5 猎手 F1：按 basename【累积】而非覆盖——多模块工程每个子模块
                # pom.xml 撞同键，last-write-wins 会静默丢弃先访问的子模块清单（依赖只
                # 声明在非根子模块时 lombok/boot 版本探测全盲）。总量封顶防超大 monorepo。
                if len(manifest_texts.get(f, "")) < 200_000:
                    manifest_texts[f] = (
                        manifest_texts.get(f, "") + " " + _read_text(os.path.join(root, f)))
            if f == "package.json":
                manifest_texts.setdefault("package.json", "")
                manifest_texts["package.json"] += _read_text(os.path.join(root, f))
                if rel:
                    frontend_proj_dirs.append(rel)
            if f == "angular.json":
                has_angular = True
            if low.startswith("next.config."):
                has_next = True
            if low.startswith("vite.config."):
                has_vite = True
            # 模板/SPA 形态计数
            if ext in _TEMPLATE_EXTS:
                tmpl_count += 1
                if ext in _TEMPLATE_EXT_ENGINE:
                    template_engine_hits[_TEMPLATE_EXT_ENGINE[ext]] += 1
            elif ext in _SPA_EXTS:
                spa_files[_SPA_EXTS[ext]] += 1
            elif ext in _SPA_JSX_EXTS:
                jsx_count += 1

    evidence: list[str] = []
    # ── 后端语言/构建/框架 ──
    backend_lang = ""
    build_tool = ""
    backend_fw = ""
    for mf in manifests:
        base = os.path.basename(mf)
        if base in _MANIFEST_BACKEND:
            backend_lang, build_tool = _MANIFEST_BACKEND[base]
            break
        if base.endswith(".csproj"):
            backend_lang, build_tool = "csharp", "dotnet"
            break
    if not backend_lang and "package.json" in manifest_texts:
        backend_lang, build_tool = "javascript/typescript", "npm"
    all_manifest_text = " ".join(manifest_texts.values())
    for marker, fw in _BACKEND_FRAMEWORK_MARKERS.items():
        if marker in all_manifest_text:
            backend_fw = fw
            break
    # E9-2（阶段E 复核 F2/RF1）：确定性 DB 面——依赖坐标/驱动名是 ground truth
    # （RuoYi 的 mysql-connector-java 就在 pom 里），此前信号在手边却被整体丢弃，
    # 导致 DB 特化技能（mysql/postgres-patterns）主路径永不挂载。通用词表可对称扩展。
    db_engines: list[str] = []
    for _db, _markers in _DB_DEP_MARKERS.items():
        if any(m in all_manifest_text for m in _markers):
            db_engines.append(_db)
    if manifests:
        evidence.append(f"构建/清单文件: {', '.join(sorted(set(manifests))[:6])}")
    if ext_counts:
        evidence.append("扩展名分布: " + " ".join(f"{e}×{n}" for e, n in ext_counts.most_common(10)))

    # ── 前端形态裁决 ──
    server_tmpl_dep = ""
    for dep, name in _SERVER_TEMPLATE_DEP.items():
        if dep in all_manifest_text:
            server_tmpl_dep = name
            break
    spa_total = sum(spa_files.values()) + (jsx_count if "react" in all_manifest_text else 0)
    has_spa = bool(spa_total or has_angular or has_next or (has_vite and "vue" in all_manifest_text))
    # 真服务端模板：有模板专用扩展名(jsp/ftl/vm...) 或 (templates 下的 .html + 后端模板依赖)
    real_server_tmpl = sum(template_engine_hits.values())
    if server_tmpl_dep and ext_counts.get(".html", 0) > 0:
        real_server_tmpl += ext_counts.get(".html", 0)
    has_server_tmpl = real_server_tmpl > 0

    frontend = ""
    frontend_kind = "none"
    if has_spa and has_server_tmpl:
        frontend_kind = "separated"
        spa_name = (spa_files.most_common(1)[0][0] if spa_files else
                    ("Angular" if has_angular else "Next.js/React" if has_next else
                     "React" if jsx_count else "SPA"))
        frontend = f"{spa_name}(独立) + 服务端模板({server_tmpl_dep or '/'.join(template_engine_hits) or 'HTML'})"
    elif has_spa:
        frontend_kind = "spa"
        if spa_files.get("Vue") or (has_vite and "vue" in all_manifest_text):
            frontend = "Vue"
        elif has_angular:
            frontend = "Angular"
        elif has_next:
            frontend = "Next.js (React)"
        elif "react" in all_manifest_text or jsx_count:
            frontend = "React"
        elif spa_files.get("Svelte"):
            frontend = "Svelte"
        else:
            frontend = "SPA(未判明具体框架)"
    elif has_server_tmpl:
        frontend_kind = "server-template"
        eng = server_tmpl_dep or (template_engine_hits.most_common(1)[0][0]
                                  if template_engine_hits else "HTML 模板")
        frontend = f"服务端模板（{eng}）"
    else:
        frontend_kind = "none"
        frontend = "无独立前端（API/后端为主，或前端未在本仓）"

    if has_server_tmpl:
        evidence.append(
            f"服务端模板信号: 引擎依赖={server_tmpl_dep or '无'}; "
            f"模板专用扩展={dict(template_engine_hits) or '无'}; .html×{ext_counts.get('.html',0)}"
        )
    evidence.append(
        f"SPA 信号: .vue×{spa_files.get('Vue',0)} svelte×{spa_files.get('Svelte',0)} "
        f"jsx/tsx×{jsx_count} angular={has_angular} next={has_next} vite={has_vite}; "
        + ("独立前端工程目录=" + ",".join(sorted(set(frontend_proj_dirs))[:4]) if frontend_proj_dirs else "无独立前端工程")
    )

    # ── 置信度 ──
    confidence = 0.5
    if backend_lang and (build_tool or backend_fw):
        confidence += 0.25
    if frontend_kind in ("server-template", "spa") and (has_server_tmpl ^ has_spa):
        confidence += 0.2  # 前端形态单一明确
    if frontend_kind == "separated":
        confidence += 0.1
    if frontend_kind == "none" and not manifests:
        confidence -= 0.3  # 啥都没扫到
    confidence = max(0.0, min(1.0, confidence))
    needs_adj = confidence < 0.65 or (has_spa and has_server_tmpl)

    # ── JVM 系专属事实：jakarta/javax 命名空间 + Boot/Java 版本（worker 写对 import 的硬前提）──
    infra_symbols, infra_symbol_methods = _detect_infra_symbols(infra_class_paths)
    if infra_symbols:
        evidence.append(
            "基建符号锚点（真实存在的基础设施类）：" + "；".join(
                f"{k}={','.join(v)}" for k, v in infra_symbols.items()
            )[:300]
        )
    jvm = _detect_jvm_facts(project_path, manifest_texts, java_sample_paths)
    if jvm and jvm.get("servlet_namespace"):
        evidence.append(
            f"JVM 命名空间: {jvm['servlet_namespace']}（{jvm.get('namespace_source','')}）"
            f"; Spring Boot={jvm.get('spring_boot_version') or '未判明'}"
            f"; Java={jvm.get('java_version') or '未判明'}"
        )
    # 鉴权变体钉死（Shiro vs Spring Security）—— round11 治本②，消除同模块鉴权分裂 + SecurityUtils 臆造
    auth = _detect_auth_variant(
        java_sample_paths, _read_text(os.path.join(project_path, "pom.xml")) + " " + " ".join(manifest_texts.values())
    )
    if auth:
        evidence.append(
            f"鉴权变体: {auth['variant']}（shiro_hits={auth['shiro_hits']}, springsec_hits={auth['springsec_hits']}）"
        )

    return {
        "frontend": frontend,
        "frontend_kind": frontend_kind,
        "backend": (f"{backend_fw} ({backend_lang})" if backend_fw else backend_lang) or "未判明",
        "build": build_tool or "未判明",
        "jvm": jvm or {},
        "auth": auth or {},
        "infra_symbols": infra_symbols,
        "infra_symbol_methods": infra_symbol_methods,
        "confidence": round(confidence, 2),
        "evidence": evidence,
        "db": db_engines,
        "signals": {
            "manifests": sorted(set(manifests))[:8],
            "server_template_files": real_server_tmpl,
            "spa_files": spa_total,
            "frontend_project_dirs": sorted(set(frontend_proj_dirs))[:4],
        },
        "needs_model_adjudication": needs_adj,
        "source": "deterministic",
    }


_STACK_KW = (
    "技术栈", "架构", "框架", "前端", "后端", "thymeleaf", "freemarker", "velocity",
    "vue", "react", "angular", "spring", "springboot", "spring boot", "shiro",
    "security", "jsp", "模板", "bootstrap", "element", "单体", "微服务", "前后端分离",
    "mybatis", "django", "flask", "express", "laravel", "stack", "framework",
)


def extract_stack_hints_from_knowledge(knowledge_context: dict | None, max_hits: int = 6) -> list[str]:
    """从已检索的项目知识库上下文里抽取【技术栈/架构】相关条目（治本 8537fa5e 续：

    我们爬了项目 wiki/规范进 KB（如"[RuoYi规范] RuoYi 是 SpringBoot+Shiro+Thymeleaf"），
    但它埋在 query-dependent 的 semantic 层 / 当作普通 norms，tech_design 没据它定栈。
    本函数把这些条目显式拎出来，作为 detect_stack 的【高优先证据源】（与磁盘事实合流）。
    纯函数：只读已注入的 knowledge_context，不发起检索、不连库。
    """
    if not isinstance(knowledge_context, dict):
        return []
    hits: list[str] = []
    seen: set[str] = set()

    def _consider(text: str, tag: str) -> None:
        if not text:
            return
        low = text.lower()
        if any(kw in low for kw in _STACK_KW):
            snip = " ".join(text.split())[:240]
            key = snip[:80]
            if key not in seen:
                seen.add(key)
                hits.append(f"[{tag}] {snip}")

    summary = knowledge_context.get("project_summary")
    if isinstance(summary, str):
        _consider(summary, "项目摘要")
    elif isinstance(summary, list) and summary:
        _consider(str(summary[0]), "项目摘要")
    for layer, tag in (("semantic", "KB语义"), ("norms", "KB规范"), ("struct", "KB结构")):
        for item in (knowledge_context.get(layer) or []):
            if len(hits) >= max_hits:
                break
            if isinstance(item, dict):
                txt = item.get("content") or item.get("title") or ""
                title = item.get("title") or ""
                _consider(f"{title} {txt}".strip(), tag)
            elif isinstance(item, str):
                _consider(item, tag)
    return hits[:max_hits]


def format_stack_for_prompt(profile: dict | None, *, include_method_sigs: bool = True) -> str:
    """把 project_stack 画像渲染成喂给 tech_design 的【权威栈指令】（磁盘优先于文档）。

    include_method_sigs（R65E9-T2）：默认 True——tech_design 逐字不变（含 public 方法签名
    载荷）。声明步（PLAN baseline_covered 申报）传 False 取【精简版】：保留能力边界硬约束
    （变体/前端形态/鉴权/基建概念 FQN），裁掉方法签名 payload——声明只需知道"有没有此能力"，
    不需要方法名，且避免大量 *Utils 签名撑爆 plan prefill。"""
    if not profile:
        return ""
    fe = profile.get("frontend", "未判明")
    be = profile.get("backend", "未判明")
    build = profile.get("build", "未判明")
    kind = profile.get("frontend_kind", "")
    conf = profile.get("confidence", 0)
    lines = [
        "【项目技术栈画像（磁盘探测 ground truth，权威；优先级高于需求文档的任何框架假设）】：",
        f"- 后端：{be}；构建：{build}",
        f"- 前端：{fe}（形态={kind}，置信={conf}）",
    ]
    if kind == "server-template":
        lines.append(
            "- 前端落地约定：新增页面【必须】是服务端模板（放项目既有 templates 目录，沿用其引擎/布局片段），"
            "由后端控制器返回视图；【禁止】生成 .vue/独立 SPA 工程文件——本项目无前端工程，那是死代码。"
        )
    elif kind == "spa":
        lines.append("- 前端落地约定：新增页面用该 SPA 框架的单文件组件/路由，落在既有前端工程目录。")
    elif kind == "separated":
        lines.append("- 前端落地约定：前后端分离，前端进 SPA 工程目录、后端只出 API；按各自既有约定落地。")
    jvm = profile.get("jvm") or {}
    ns = jvm.get("servlet_namespace")
    if ns:
        other = "javax" if ns == "jakarta" else "jakarta"
        boot = jvm.get("spring_boot_version")
        jv = jvm.get("java_version")
        ver_bits = []
        if boot:
            ver_bits.append(f"Spring Boot {boot}")
        if jv:
            ver_bits.append(f"Java {jv}")
        ver_txt = ("（" + "、".join(ver_bits) + "）") if ver_bits else ""
        lines.append(
            f"- 【命名空间·硬约束{ver_txt}】本项目 Servlet/JPA/校验/注解一律用 `{ns}.*`（如 "
            f"`{ns}.servlet.http.HttpServletRequest`、`{ns}.persistence.*`、`{ns}.validation.*`、"
            f"`{ns}.annotation.Resource`）；【严禁】写 `{other}.*` 包名——本项目 classpath 没有它，"
            f"会直接 `package {other}.servlet does not exist` 编译失败。新建模块 pom 也按此栈继承依赖。"
        )
    # R65TR-T5：Lombok 基线在位性硬约束——键缺席（老画像/回放 profile）不猜不渲染。
    if jvm.get("lombok_available") is False:
        lines.append(
            "- 【基线约定·硬约束】本项目基线【未引入 Lombok】（判据："
            f"{jvm.get('lombok_source') or '基线双证均无'}）：【禁止】使用 @Data/@Getter/"
            "@Setter/@Builder/@Slf4j 等 Lombok 注解与 `import lombok.*`，也【禁止】往任何 "
            "pom 添加 lombok 依赖——实体/DTO 的 getter/setter/构造器/logger 一律【手写】"
            "（与基线代码风格一致）。引入注解处理器属基线约定漂移：高版本 JDK 默认关闭"
            "隐式注解处理会整模块编译失败，且无调用者的注解类会静默编译通过成哑弹。"
        )
    elif jvm.get("lombok_available") is True:
        lines.append(
            f"- 【基线约定】本项目基线已用 Lombok（{jvm.get('lombok_source') or ''}）：实体/DTO "
            "可沿用既有 Lombok 注解风格，与邻近代码保持一致。"
        )
    infra = profile.get("infra_symbols") or {}
    if infra:
        _methods = profile.get("infra_symbol_methods") or {}
        lines.append(
            "- 【基建符号·硬约束】本项目真实存在的基础设施类（按概念列真实 FQN，用这些、原样 import）："
        )
        # R65E8-T5：方法签名块总量封顶（复核 F3）——防大量 *Utils 类把 CacheUtils 这类
        # 载荷签名淹没/撑爆 prefill；与既有 evidence[:300] 截断纪律对称。
        _method_budget = 1800
        for concept, fqns in infra.items():
            lines.append(f"  · {concept}：{'、'.join(fqns)}")
            # R65E8-T5：类级 FQN 不够——补 public 方法签名，杜绝方法级幻觉（调 .set/.get
            # 而非真实的 get/put/remove）。只渲染有签名的 FQN，逐类封顶已在解析期完成。
            # R65E9-T2：声明步（include_method_sigs=False）跳过方法签名 payload——只保留概念
            # FQN 作能力边界，避免精简版被大量签名撑爆。
            for _fqn in (fqns if include_method_sigs else ()):
                _sigs = _methods.get(_fqn)
                if _sigs and _method_budget > 0:
                    _short = _fqn.rsplit(".", 1)[-1]
                    _line = f"    ↳ {_short} 的 public 方法（照抄签名，别臆造方法名）：{'; '.join(_sigs)}"
                    lines.append(_line[:_method_budget + 80])  # 单行软界，避免半截被丢
                    _method_budget -= len(_line)
        # R65E9-T2：方法签名段仅在 include_method_sigs 时渲染——警告措辞随之切换，
        # 精简版不提"上列方法签名"（那侧无签名，避免引用不存在内容）。
        _sig_phrase = "【及其上列方法签名】" if include_method_sigs else ""
        _method_warn = (
            "或未列出的方法名（如在缓存类上调裸 RedisTemplate 的 set/get）"
            if include_method_sigs else "")
        lines.append(
            f"  ⚠️ 实现新功能需要缓存/响应/鉴权/基类等基础设施时，【必须】复用上面列出的本项目真实类{_sig_phrase}；"
            "【严禁】凭框架惯性臆造未列出的'标准类'（如某变体的 RedisCache 本项目可能没有）"
            f"{_method_warn}——classpath/方法没有就 "
            "`cannot find symbol`/`package 不存在` 编译失败、死循环。需要的类/方法不在列表里时，"
            "先在项目里 grep 确认它真实存在再用，绝不臆造。"
        )
    auth = profile.get("auth") or {}
    av = auth.get("variant")
    if av == "shiro":
        lines.append(
            "- 【鉴权变体·硬约束】本项目用 Apache Shiro：控制器权限注解一律 `@RequiresPermissions(\"模块:操作\")`，"
            "取当前登录用户/权限【只用上方〖基建符号·鉴权/安全工具〗里列出的本项目真实类】，绝不臆造未列出的鉴权类。"
            "【严禁】Spring Security 写法：`@PreAuthorize`、`org.springframework.security.*`、给任何 pom 加 "
            "`spring-boot-starter-security`——本项目 classpath 无 Spring Security，写了必 `cannot find symbol`"
            "/bean 注入失败。"
        )
    elif av == "spring-security":
        lines.append(
            "- 【鉴权变体·硬约束】本项目用 Spring Security：权限注解用 `@PreAuthorize(...)`，取当前用户/权限"
            "【只用上方〖基建符号·鉴权/安全工具〗里列出的本项目真实类】，绝不臆造未列出的鉴权类。"
            "【严禁】Apache Shiro 写法：`@RequiresPermissions`、`org.apache.shiro.*`——本项目 classpath 无 Shiro。"
        )
    kb_hints = profile.get("kb_stack_hints") or []
    if kb_hints:
        lines.append("- KB 已收录的项目架构知识（与磁盘事实一致，佐证）：")
        lines.extend("  · " + h for h in kb_hints[:4])
    lines.append(
        "- 若需求文档假定了与上面【不同】的框架/技术，一律以本画像（磁盘事实）为准【适配落地】，不算虚假前提、不要终止。"
    )
    return "\n".join(lines)
