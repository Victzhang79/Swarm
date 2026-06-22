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
            low = f.lower()
            if f in _MANIFEST_BACKEND or f.endswith(".csproj"):
                manifests.append(os.path.join(rel, f) if rel else f)
                manifest_texts[f] = _read_text(os.path.join(root, f))
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

    return {
        "frontend": frontend,
        "frontend_kind": frontend_kind,
        "backend": (f"{backend_fw} ({backend_lang})" if backend_fw else backend_lang) or "未判明",
        "build": build_tool or "未判明",
        "confidence": round(confidence, 2),
        "evidence": evidence,
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


def format_stack_for_prompt(profile: dict | None) -> str:
    """把 project_stack 画像渲染成喂给 tech_design 的【权威栈指令】（磁盘优先于文档）。"""
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
    kb_hints = profile.get("kb_stack_hints") or []
    if kb_hints:
        lines.append("- KB 已收录的项目架构知识（与磁盘事实一致，佐证）：")
        lines.extend("  · " + h for h in kb_hints[:4])
    lines.append(
        "- 若需求文档假定了与上面【不同】的框架/技术，一律以本画像（磁盘事实）为准【适配落地】，不算虚假前提、不要终止。"
    )
    return "\n".join(lines)
