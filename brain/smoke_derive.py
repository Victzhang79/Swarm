"""S1-2：栈感知 entrypoint/端口/migration 证据推导——纯函数层（task#16）。

工程定位（docs/RUNTIME_SMOKE_DESIGN.md §5.3/§5.4 + "给 task#16 的实现指引"）：
  运行时冒烟（verify_runtime）需要知道"怎么启动、探哪个端口、有没有 migration"。
  这些是【磁盘的客观属性】，与 detect_stack 同族：确定性推导、零模型成本。本模块：
  ① 输入 = project_stack 画像（detect_stack 产物，第一分派键，勿重测栈）+ 工作树根路径
     （只读本地文件 IO 读 manifest/配置，纯函数对文件系统的只读依赖）；
  ② 输出 = SmokeDerivation{start_cmd, port, health_path, migration_kind, evidence}；
  ③ fail-closed：每个字段推不出 → None，绝不猜；上层拿 None 走 skipped+degraded；
  ④ 显式配置命中时 evidence 记录【来源文件+键】，供三分类归因/UI 留痕；
  ⑤ 通用多栈铁律：只允许【按框架/语言 keyed 的数据表】（表条目含栈词汇=证据形态，合法），
     绝无 if-项目名/写死单项目路径；
  ⑥ 零网络/零沙箱 IO/零 LLM，可完全离线单测；任何坏输入/坏配置文件容错返 None，绝不抛。

推导优先级（§5.3）：
  端口：①项目配置文件显式端口（按框架 keyed 的 文件名+键名 数据表）→ ②框架默认端口表。
  entrypoint：manifest 声明证据（boot 插件/scripts.start/go main/cargo bin/…），无证据 → None。
  健康端点：manifest 依赖 marker（actuator/smallrye-health/terminus…），无证据 → None
           （上层退化为纯 TCP 探活）。
  migration：目录/manifest 形态数据表（flyway/liquibase/alembic/prisma/golang-migrate/raw-sql）。

YAML 裁决：pyyaml 非本仓声明依赖（是 langchain 的传递依赖，venv 内可用）——故 try-import
使用、失败回退【保守的缩进跟踪行级解析】，宁可推不出返 None，不引新依赖。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

# 复用勿复制：栈清单/框架 marker 的单一权威在 stack_detect（设计文档 §5.1 点名可 import）
from swarm.brain.stack_detect import _BACKEND_FRAMEWORK_MARKERS, _MANIFEST_BACKEND, _NOISE_DIRS

# ══════════════════════════════ 数据表（唯一允许含栈词汇的地方）══════════════════════════════

# 框架 → 默认端口（canonical 键 = _BACKEND_FRAMEWORK_MARKERS 值的小写）
_FRAMEWORK_DEFAULT_PORTS: dict[str, int] = {
    "spring boot": 8080,
    "spring mvc": 8080,
    "spring": 8080,
    "quarkus": 8080,
    "micronaut": 8080,
    "django": 8000,
    "flask": 5000,
    "fastapi": 8000,        # uvicorn 默认
    "express": 3000,
    "nestjs": 3000,
    "next.js": 3000,
    "gin": 8080,
    "laravel": 8000,        # php artisan serve 默认
    "symfony": 8000,
    "rails": 3000,
}

# 显式端口配置源：(适用框架集|None=任意栈, 配置文件名, 解析格式, 键)。
# 按序尝试——框架专属配置先于通用 .env；命中即止（首个显式证据胜出）。
_PORT_CONFIG_SOURCES: tuple[tuple[frozenset[str] | None, str, str, Any], ...] = (
    (frozenset({"spring boot", "spring mvc", "spring"}),
     "application.properties", "properties", "server.port"),
    (frozenset({"spring boot", "spring mvc", "spring"}),
     "application.yml", "yaml", ("server", "port")),
    (frozenset({"spring boot", "spring mvc", "spring"}),
     "application.yaml", "yaml", ("server", "port")),
    (frozenset({"quarkus"}), "application.properties", "properties", "quarkus.http.port"),
    (frozenset({"quarkus"}), "application.yml", "yaml", ("quarkus", "http", "port")),
    (frozenset({"micronaut"}), "application.yml", "yaml", ("micronaut", "server", "port")),
    (frozenset({"flask"}), ".env", "env", "FLASK_RUN_PORT"),
    (None, ".env", "env", "PORT"),
)

# 健康端点：manifest 依赖 marker（小写 substring）→ 路径。无 marker → None（上层 TCP 探活）。
_HEALTH_ENDPOINT_MARKERS: tuple[tuple[str, str], ...] = (
    ("spring-boot-starter-actuator", "/actuator/health"),
    ("quarkus-smallrye-health", "/q/health"),
    ("micronaut-management", "/health"),
    ("@nestjs/terminus", "/health"),
)

# prepare 命令数据表（F1）：仅当 start_cmd 消费【构建产物】时才需要 prepare——
# L2 全链只跑编译（mvn compile / gradle build 类），从不 package/bootJar，
# `java -jar target/*.jar` 若不先产 jar 必然 no such file。
# 条目 = (start_cmd 内产物路径标记, wrapper 文件名|None, 有 wrapper 命令, 无 wrapper 命令)。
# node/python/go/rust 不入表：go run/cargo run 自带构建，node/python 直接跑源码
#（其依赖缺失由运行时冒烟三分类的 dependency_missing 诚实归类，不在 prepare 面伪装）。
_PREPARE_RULES: tuple[tuple[str, str | None, str, str], ...] = (
    ("target/*.jar", None, "", "mvn -q -DskipTests package"),
    ("build/libs/*.jar", "gradlew", "./gradlew bootJar -x test -q", "gradle bootJar -x test -q"),
)

# migration 目录/文件形态（§5.4）。检测按此顺序，首中即止。
_MIGRATION_DIR_SUFFIX_FLYWAY = "db/migration"          # classpath 约定目录（含 *.sql）
_MIGRATION_CHANGELOG_PREFIX = "db.changelog"           # liquibase changelog 文件名形态
_MIGRATION_CHANGELOG_EXTS = (".xml", ".yaml", ".yml", ".json")
_MIGRATION_RAW_SQL_DIRS = frozenset({"sql", "db", "database", "migrations", "migration"})

# 走查时会读取内容的清单文件名（= stack_detect 清单表 + 前端清单）
_MANIFEST_NAMES = frozenset(_MANIFEST_BACKEND) | {"package.json", "build.gradle.kts"}

# 走查索引关心的其他文件名（entrypoint/端口/migration 证据）
_INTERESTING_NAMES = _MANIFEST_NAMES | {
    "application.properties", "application.yml", "application.yaml", ".env",
    "alembic.ini", "env.py", "main.py", "app.py", "wsgi.py", "run.py",
    "artisan", "Procfile",
}

_KNOWN_LANGS = frozenset(
    {lang for lang, _ in _MANIFEST_BACKEND.values()} | {"csharp", "javascript/typescript"}
)
_CANONICAL_FRAMEWORKS = frozenset(v.lower() for v in _BACKEND_FRAMEWORK_MARKERS.values())

_MAX_WALK_DIRS = 3000
_MAX_LIST_PER_KEY = 24


@dataclass(frozen=True)
class SmokeDerivation:
    """推导结果。字段推不出=None（fail-closed）；evidence 按字段名记来源。

    prepare_cmd（F1）：start_cmd 需要构建产物时的产物构建命令（如 mvn package）；
    None = start_cmd 自包含（go run / npm start / python3 …），无需 prepare。
    """
    start_cmd: str | None = None
    prepare_cmd: str | None = None
    port: int | None = None
    health_path: str | None = None
    migration_kind: str | None = None
    evidence: dict[str, str] = field(default_factory=dict)


# ══════════════════════════════ 工作树只读索引 ══════════════════════════════

class _TreeIndex:
    """一次有界 os.walk 建索引，各推导器共享（避免多次全树扫描）。"""

    def __init__(self) -> None:
        self.files_by_name: dict[str, list[str]] = {}   # basename → [relpath...]（浅→深）
        self.sql_dirs: dict[str, list[str]] = {}        # 含 .sql 文件的目录 relpath → [文件名]
        self.up_sql_dirs: set[str] = set()              # 含 *.up.sql 的目录 relpath
        self.changelog_files: list[str] = []            # liquibase changelog 形态文件 relpath
        self.dirs: set[str] = set()                     # 目录 relpath（posix，含空串=根）


def _build_index(project_path: str) -> _TreeIndex:
    idx = _TreeIndex()
    dir_count = 0
    try:
        for root, dirs, files in os.walk(project_path):
            dirs[:] = sorted(d for d in dirs if d not in _NOISE_DIRS)
            dir_count += 1
            if dir_count > _MAX_WALK_DIRS:
                break
            rel = os.path.relpath(root, project_path)
            rel = "" if rel == "." else rel.replace(os.sep, "/")
            idx.dirs.add(rel)
            for f in files:
                rp = f"{rel}/{f}" if rel else f
                if f in _INTERESTING_NAMES:
                    lst = idx.files_by_name.setdefault(f, [])
                    if len(lst) < _MAX_LIST_PER_KEY:
                        lst.append(rp)
                low = f.lower()
                if low.endswith(".sql"):
                    lst = idx.sql_dirs.setdefault(rel, [])
                    if len(lst) < _MAX_LIST_PER_KEY:
                        lst.append(f)
                    if low.endswith(".up.sql"):
                        idx.up_sql_dirs.add(rel)
                if low.startswith(_MIGRATION_CHANGELOG_PREFIX) and low.endswith(_MIGRATION_CHANGELOG_EXTS):
                    if len(idx.changelog_files) < _MAX_LIST_PER_KEY:
                        idx.changelog_files.append(rp)
    except OSError:
        pass
    # 浅路径优先（根配置 > 深层测试夹具）
    for lst in idx.files_by_name.values():
        lst.sort(key=lambda p: (p.count("/"), p))
    return idx


def _read(project_path: str, relpath: str, limit: int = 200_000) -> str:
    """case-preserving 读取（stack_detect._read_text 会小写化，键名/模块名要原样，故自备）。"""
    try:
        with open(os.path.join(project_path, relpath), encoding="utf-8", errors="ignore") as f:
            return f.read(limit)
    except OSError:
        return ""


def _manifest_text_lower(project_path: str, idx: _TreeIndex) -> str:
    parts = []
    for name in sorted(_MANIFEST_NAMES):
        for rp in idx.files_by_name.get(name, [])[:8]:
            parts.append(_read(project_path, rp, limit=40_000))
    return " ".join(parts).lower()


# ══════════════════════════════ backend 画像解析 ══════════════════════════════

def _split_backend(project_stack: Any) -> tuple[str, str]:
    """project_stack.backend（如 "Spring Boot (java)" / "python" / "未判明"）→ (框架小写, 语言小写)。

    形状来源：stack_detect.detect_stack_deterministic 的 backend 字段构造式
    `f"{backend_fw} ({backend_lang})" if backend_fw else backend_lang`。推不出 → ("", "")。
    """
    if not isinstance(project_stack, dict):
        return "", ""
    backend = project_stack.get("backend")
    if not isinstance(backend, str) or not backend.strip():
        return "", ""
    m = re.match(r"^(.+?)\s*\(([^()]+)\)\s*$", backend.strip())
    if m:
        fw = m.group(1).strip().lower()
        lang = m.group(2).strip().lower()
        return (fw if fw in _CANONICAL_FRAMEWORKS else fw), lang
    bare = backend.strip().lower()
    if bare in _KNOWN_LANGS:
        return "", bare
    if bare in _CANONICAL_FRAMEWORKS:
        return bare, ""
    return "", ""       # "未判明"等 → 全空，下游 fail-closed


# ══════════════════════════════ 端口推导 ══════════════════════════════

_PLACEHOLDER_DEFAULT_RE = re.compile(r"\$\{[^:{}]+:(\d{2,5})\}")


def _coerce_port(value: Any) -> int | None:
    """配置值 → 端口 int。支持裸数字与 ${VAR:default} 占位符默认值；其余（纯占位/表达式）→ None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 < value < 65536 else None
    if not isinstance(value, str):
        return None
    v = value.strip().strip("'\"")
    if v.isdigit():
        p = int(v)
        return p if 0 < p < 65536 else None
    m = _PLACEHOLDER_DEFAULT_RE.search(v)
    if m:
        p = int(m.group(1))
        return p if 0 < p < 65536 else None
    return None


def _yaml_lookup(text: str, keypath: tuple[str, ...]) -> Any:
    """yml 取键：优先 pyyaml（venv 传递依赖），不可用/解析失败回退缩进跟踪行级解析。"""
    try:
        import yaml  # noqa: PLC0415 —— 非声明依赖，按裁决 try-import
        for doc in yaml.safe_load_all(text):
            cur: Any = doc
            for k in keypath:
                if not isinstance(cur, dict):
                    cur = None
                    break
                cur = cur.get(k)
            if cur is not None:
                return cur
        return None
    except Exception:
        return _yaml_indent_lookup(text, keypath)


def _yaml_indent_lookup(text: str, keypath: tuple[str, ...]) -> str | None:
    """保守回退：缩进栈跟踪的行级 yml 键路径提取。解析不了的形态一律 None（宁缺勿猜）。"""
    stack: list[tuple[int, str]] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "---":
            stack = []
            continue
        m = re.match(r"^(\s*)([A-Za-z0-9_.\-]+):\s*(.*?)\s*$", raw)
        if not m:
            continue
        indent = len(m.group(1).expandtabs(2))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, m.group(2)))
        if tuple(k for _, k in stack) == keypath and m.group(3):
            return m.group(3)
    return None


def _properties_lookup(text: str, key: str) -> str | None:
    m = re.search(rf"^[ \t]*{re.escape(key)}[ \t]*[=:][ \t]*(\S+)", text, re.MULTILINE)
    return m.group(1) if m else None


def _env_lookup(text: str, key: str) -> str | None:
    m = re.search(rf"^[ \t]*(?:export[ \t]+)?{re.escape(key)}[ \t]*=[ \t]*(\S+)", text, re.MULTILINE)
    return m.group(1) if m else None


def derive_port(project_stack: Any, project_path: str,
                idx: _TreeIndex | None = None) -> tuple[int | None, str | None]:
    """端口推导 → (port|None, evidence|None)。①配置文件显式端口 ②框架默认表 ③None。"""
    framework, _lang = _split_backend(project_stack)
    idx = idx if idx is not None else _build_index(project_path)
    for frameworks, filename, fmt, key in _PORT_CONFIG_SOURCES:
        if frameworks is not None and framework not in frameworks:
            continue
        for rp in idx.files_by_name.get(filename, []):
            text = _read(project_path, rp)
            if not text:
                continue
            if fmt == "properties":
                raw = _properties_lookup(text, key)
                key_label = key
            elif fmt == "yaml":
                raw = _yaml_lookup(text, key)
                key_label = ".".join(key)
            else:  # env
                raw = _env_lookup(text, key)
                key_label = key
            port = _coerce_port(raw)
            if port is not None:
                return port, f"{rp}: {key_label}={raw}".strip()
    default = _FRAMEWORK_DEFAULT_PORTS.get(framework)
    if default is not None:
        return default, f"框架默认端口表: {framework} → {default}"
    return None, None


# ══════════════════════════════ entrypoint 推导（按语言 keyed）══════════════════════════════

def _derive_start_jvm(project_path: str, idx: _TreeIndex, framework: str) -> tuple[str | None, str | None]:
    # Maven：声明 spring-boot-maven-plugin 的 pom → 可执行 jar 证据（多模块取声明模块）。
    # R40-4 消歧（round40：RuoYi 根聚合器 pom 直接声明插件+ruoyi-admin 双命中 → 歧义
    # 护栏误判 → 冒烟常年 skipped）：①<packaging>pom</packaging> 聚合器结构上产不出
    # 可执行 jar → 排除；②只在 <pluginManagement> 提及=版本钉子非可执行声明 → 剥掉
    # 再搜。消歧后仍多命中=真歧义照旧 None（fail-closed 不拔）。
    import re as _re

    def _executable_boot_pom(rp: str) -> bool:
        text = _read(project_path, rp, limit=120_000)
        if "spring-boot-maven-plugin" not in text:
            return False
        if _re.search(r"<packaging>\s*pom\s*</packaging>", text):
            return False  # 聚合器
        stripped = _re.sub(r"<pluginManagement>.*?</pluginManagement>", "",
                           text, flags=_re.S)
        return "spring-boot-maven-plugin" in stripped

    boot_poms = [rp for rp in idx.files_by_name.get("pom.xml", [])
                 if _executable_boot_pom(rp)]
    if len(boot_poms) == 1:
        moddir = os.path.dirname(boot_poms[0]).replace(os.sep, "/")
        prefix = f"{moddir}/" if moddir else ""
        return f"java -jar {prefix}target/*.jar", f"{boot_poms[0]}: spring-boot-maven-plugin"
    if len(boot_poms) > 1:
        return None, None  # 多个可执行模块，歧义不猜（fail-closed）
    # Gradle：org.springframework.boot 插件 → bootJar 产物
    boot_gradles = [
        rp for name in ("build.gradle", "build.gradle.kts")
        for rp in idx.files_by_name.get(name, [])
        if "org.springframework.boot" in _read(project_path, rp, limit=120_000)
    ]
    if len(boot_gradles) == 1:
        moddir = os.path.dirname(boot_gradles[0]).replace(os.sep, "/")
        prefix = f"{moddir}/" if moddir else ""
        return f"java -jar {prefix}build/libs/*.jar", f"{boot_gradles[0]}: org.springframework.boot 插件"
    return None, None


def _derive_start_node(project_path: str, idx: _TreeIndex, framework: str) -> tuple[str | None, str | None]:
    if "package.json" not in idx.files_by_name or "package.json" not in idx.files_by_name["package.json"]:
        return None, None  # 只认工作树根清单
    try:
        pkg = json.loads(_read(project_path, "package.json"))
    except (ValueError, TypeError):
        return None, None  # 坏 JSON → 容错 None
    scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
    if not isinstance(scripts, dict):
        return None, None
    for name in ("start", "dev"):
        if isinstance(scripts.get(name), str) and scripts[name].strip():
            return f"npm run {name}", f"package.json: scripts.{name}"
    return None, None


_FASTAPI_APP_RE = re.compile(r"^([A-Za-z_]\w*)\s*=\s*FastAPI\s*\(", re.MULTILINE)


def _derive_start_python(project_path: str, idx: _TreeIndex, framework: str) -> tuple[str | None, str | None]:
    # ① Django：manage.py 是 Django CLI 专属清单（_MANIFEST_BACKEND 收录形态）
    if "manage.py" in idx.files_by_name.get("manage.py", []):
        return "python3 manage.py runserver", "manage.py 存在（Django CLI 清单）"
    # ② FastAPI：常见入口文件里 `<var> = FastAPI(` 实证 → uvicorn module:attr
    for entry in ("main.py", "app.py"):
        for rp in idx.files_by_name.get(entry, [])[:4]:
            if rp.count("/") > 1:
                continue  # 只认根/一级目录入口，深层文件不算入口证据
            text = _read(project_path, rp, limit=40_000)
            m = _FASTAPI_APP_RE.search(text)
            if m:
                module = rp[:-3].replace("/", ".")
                return (f"python3 -m uvicorn {module}:{m.group(1)} --host 0.0.0.0",
                        f"{rp}: {m.group(1)} = FastAPI(...)")
    # ③ Flask：根级入口文件 + flask import 实证
    for entry in ("app.py", "main.py", "wsgi.py", "run.py"):
        if entry in idx.files_by_name.get(entry, []):
            text = _read(project_path, entry, limit=40_000)
            if re.search(r"^\s*(?:from|import)\s+flask\b", text, re.MULTILINE | re.IGNORECASE):
                return f"python3 {entry}", f"{entry}: import flask 实证"
    # ④ pyproject [project.scripts]：唯一 console-script 时按其 entry point 直调
    if "pyproject.toml" in idx.files_by_name.get("pyproject.toml", []):
        try:
            import tomllib
            data = tomllib.loads(_read(project_path, "pyproject.toml"))
            scripts = (data.get("project") or {}).get("scripts") or {}
            if isinstance(scripts, dict) and len(scripts) == 1:
                name, target = next(iter(scripts.items()))
                m = re.match(r"^([\w.]+):([\w.]+)$", str(target).strip())
                if m:
                    return (f'python3 -c "from {m.group(1)} import {m.group(2)}; {m.group(2)}()"',
                            f"pyproject.toml [project.scripts]: {name} = {target}")
        except Exception:
            return None, None
    return None, None


def _derive_start_go(project_path: str, idx: _TreeIndex, framework: str) -> tuple[str | None, str | None]:
    if "go.mod" not in idx.files_by_name.get("go.mod", []):
        return None, None
    # 根目录 main 包实证
    try:
        root_go = sorted(f for f in os.listdir(project_path) if f.endswith(".go"))[:12]
    except OSError:
        root_go = []
    for f in root_go:
        if re.search(r"^\s*package\s+main\b", _read(project_path, f, limit=4_000), re.MULTILINE):
            return "go run .", f"go.mod + {f}(package main)"
    # cmd/<x>/main.go 约定：唯一时可定
    cmd_mains = sorted(
        d for d in idx.dirs
        if re.fullmatch(r"cmd/[^/]+", d)
        and os.path.isfile(os.path.join(project_path, d, "main.go"))
    )
    if len(cmd_mains) == 1:
        return f"go run ./{cmd_mains[0]}", f"go.mod + {cmd_mains[0]}/main.go(package main 约定)"
    return None, None


def _derive_start_rust(project_path: str, idx: _TreeIndex, framework: str) -> tuple[str | None, str | None]:
    if "Cargo.toml" not in idx.files_by_name.get("Cargo.toml", []):
        return None, None
    if os.path.isfile(os.path.join(project_path, "src", "main.rs")):
        return "cargo run", "Cargo.toml + src/main.rs"
    if "[[bin]]" in _read(project_path, "Cargo.toml", limit=40_000):
        return "cargo run", "Cargo.toml: [[bin]] 声明"
    return None, None


def _derive_start_ruby(project_path: str, idx: _TreeIndex, framework: str) -> tuple[str | None, str | None]:
    if "Gemfile" in idx.files_by_name.get("Gemfile", []):
        if re.search(r"gem\s+['\"]rails['\"]", _read(project_path, "Gemfile", limit=40_000)):
            return "bundle exec rails server", "Gemfile: gem 'rails'"
    return None, None


def _derive_start_php(project_path: str, idx: _TreeIndex, framework: str) -> tuple[str | None, str | None]:
    if "artisan" in idx.files_by_name.get("artisan", []):
        return "php artisan serve --host=0.0.0.0", "artisan 存在（Laravel CLI 清单）"
    return None, None


# 语言 → entrypoint 推导器（数据表分派，绝无项目特判）
_ENTRY_DERIVERS: dict[str, Any] = {
    "java": _derive_start_jvm,
    "kotlin": _derive_start_jvm,
    "scala": _derive_start_jvm,
    "javascript/typescript": _derive_start_node,
    "python": _derive_start_python,
    "go": _derive_start_go,
    "rust": _derive_start_rust,
    "ruby": _derive_start_ruby,
    "php": _derive_start_php,
}


def derive_start_cmd(project_stack: Any, project_path: str,
                     idx: _TreeIndex | None = None) -> tuple[str | None, str | None]:
    """entrypoint 推导 → (start_cmd|None, evidence|None)。全部基于 manifest 证据，无证据不猜。"""
    framework, lang = _split_backend(project_stack)
    deriver = _ENTRY_DERIVERS.get(lang)
    if deriver is None:
        return None, None
    idx = idx if idx is not None else _build_index(project_path)
    try:
        return deriver(project_path, idx, framework)
    except Exception:
        return None, None


def derive_prepare_cmd(start_cmd: str | None, project_path: str) -> tuple[str | None, str | None]:
    """prepare 命令推导（F1）→ (prepare_cmd|None, evidence|None)。

    仅当 start_cmd 消费构建产物（_PREPARE_RULES 数据表标记）时才推导；
    标记本身来自本文件 entrypoint 数据表的产物路径，确定性闭环。
    wrapper 只认工作树根（gradlew 约定位置）。任何异常容错返 None（绝不抛）。
    """
    if not start_cmd:
        return None, None
    try:
        for marker, wrapper, wrapped_cmd, bare_cmd in _PREPARE_RULES:
            if marker not in start_cmd:
                continue
            if wrapper and os.path.isfile(os.path.join(project_path, wrapper)):
                return wrapped_cmd, f"start_cmd 消费构建产物({marker}) + {wrapper} 存在"
            return bare_cmd, f"start_cmd 消费构建产物({marker})"
        return None, None
    except Exception:
        return None, None


# ══════════════════════════════ health_path 推导 ══════════════════════════════

def derive_health_path(project_path: str, idx: _TreeIndex | None = None,
                       manifest_text: str | None = None) -> tuple[str | None, str | None]:
    """健康端点推导 → (path|None, evidence|None)。只认 manifest 依赖 marker，无证据 → None。"""
    idx = idx if idx is not None else _build_index(project_path)
    text = manifest_text if manifest_text is not None else _manifest_text_lower(project_path, idx)
    for marker, path in _HEALTH_ENDPOINT_MARKERS:
        if marker in text:
            return path, f"manifest 依赖 marker: {marker} → {path}"
    return None, None


# ══════════════════════════════ migration_kind 检测（§5.4 全新面）══════════════════════════════

def detect_migration_kind(project_path: str, idx: _TreeIndex | None = None,
                          manifest_text: str | None = None) -> tuple[str | None, str | None]:
    """migration 形态检测 → (kind|None, evidence|None)。目录/manifest 形态数据表，首中即止。"""
    try:
        idx = idx if idx is not None else _build_index(project_path)
        text = manifest_text if manifest_text is not None else _manifest_text_lower(project_path, idx)

        # ① flyway：classpath 约定目录 db/migration/*.sql，或构建清单声明 flyway
        for d, files in sorted(idx.sql_dirs.items()):
            if d.endswith(_MIGRATION_DIR_SUFFIX_FLYWAY):
                return "flyway", f"{d}/ 含 SQL 迁移: {', '.join(sorted(files)[:3])}"
        if "flyway" in text:
            return "flyway", "构建清单声明 flyway 依赖/插件"

        # ② liquibase：changelog 文件形态，或构建清单声明
        if idx.changelog_files:
            return "liquibase", f"changelog 文件: {sorted(idx.changelog_files)[0]}"
        if "liquibase" in text:
            return "liquibase", "构建清单声明 liquibase 依赖/插件"

        # ③ alembic：alembic.ini + <env.py 所在目录>/versions/
        if idx.files_by_name.get("alembic.ini"):
            for env_rp in idx.files_by_name.get("env.py", []):
                envdir = os.path.dirname(env_rp).replace(os.sep, "/")
                versions = f"{envdir}/versions" if envdir else "versions"
                if versions in idx.dirs:
                    return "alembic", f"alembic.ini + {versions}/"

        # ④ prisma：prisma/migrations/ 目录形态
        for d in sorted(idx.dirs):
            if d.endswith("prisma/migrations") or d == "prisma/migrations":
                return "prisma", f"{d}/ 目录存在"

        # ⑤ golang-migrate：migrations 目录内 *.up.sql 配对形态
        for d in sorted(idx.up_sql_dirs):
            if os.path.basename(d or ".") in ("migrations", "migration"):
                return "golang-migrate", f"{d}/ 含 *.up.sql 迁移对"

        # ⑥ raw sql：常见迁移目录下的裸 .sql（最弱证据，垫底）
        for d, files in sorted(idx.sql_dirs.items()):
            parts = d.split("/") if d else []
            if parts and parts[-1].lower() in _MIGRATION_RAW_SQL_DIRS:
                return "raw-sql", f"{d}/ 含 SQL: {', '.join(sorted(files)[:3])}"

        return None, None
    except Exception:
        return None, None


# ══════════════════════════════ 主入口 ══════════════════════════════

def derive_runtime_smoke(project_stack: Any, project_path: str) -> SmokeDerivation:
    """冒烟推导主入口：project_stack 画像 + 工作树根 → SmokeDerivation（各字段独立容错）。

    纯函数（只读文件 IO），零网络/零沙箱/零 LLM。任何字段推不出 → None；绝不抛异常。
    """
    evidence: dict[str, str] = {}
    try:
        idx = _build_index(str(project_path or ""))
    except Exception:
        idx = _TreeIndex()
    try:
        manifest_text = _manifest_text_lower(str(project_path or ""), idx)
    except Exception:
        manifest_text = ""

    try:
        start_cmd, ev = derive_start_cmd(project_stack, str(project_path or ""), idx)
        if ev:
            evidence["start_cmd"] = ev
    except Exception:
        start_cmd = None
    try:
        prepare_cmd, ev = derive_prepare_cmd(start_cmd, str(project_path or ""))
        if ev:
            evidence["prepare_cmd"] = ev
    except Exception:
        prepare_cmd = None
    try:
        port, ev = derive_port(project_stack, str(project_path or ""), idx)
        if ev:
            evidence["port"] = ev
    except Exception:
        port = None
    try:
        health_path, ev = derive_health_path(str(project_path or ""), idx, manifest_text)
        if ev:
            evidence["health_path"] = ev
    except Exception:
        health_path = None
    try:
        migration_kind, ev = detect_migration_kind(str(project_path or ""), idx, manifest_text)
        if ev:
            evidence["migration_kind"] = ev
    except Exception:
        migration_kind = None

    return SmokeDerivation(
        start_cmd=start_cmd,
        prepare_cmd=prepare_cmd,
        port=port,
        health_path=health_path,
        migration_kind=migration_kind,
        evidence=evidence,
    )
