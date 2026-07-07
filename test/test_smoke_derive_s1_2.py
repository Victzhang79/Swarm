"""S1-2 行为测试：brain/smoke_derive.py 纯推导层（entrypoint/端口/health/migration）。

契约（docs/RUNTIME_SMOKE_DESIGN.md §5.3/§5.4 + task#16 指引）：
- 输入 project_stack（detect_stack 画像）+ 工作树根路径；零网络/零沙箱/零 LLM。
- 每个字段推不出 → None（fail-closed，绝不猜）；显式配置命中时 evidence 记来源文件+键。
- 数据表按框架/语言 keyed（栈词汇=证据形态，合法）；绝无 if-项目名。
- 坏配置文件容错：绝不抛异常。
"""
from __future__ import annotations

import pytest

from brain.smoke_derive import (
    SmokeDerivation,
    derive_runtime_smoke,
    detect_migration_kind,
)


def _stack(backend: str, build: str = "") -> dict:
    """按 stack_detect.detect_stack_deterministic 的真实产物形状造 project_stack。"""
    return {
        "frontend": "无独立前端（API/后端为主，或前端未在本仓）",
        "frontend_kind": "none",
        "backend": backend,
        "build": build,
        "confidence": 0.95,
        "evidence": [],
        "source": "deterministic",
    }


# ── Spring Boot（java/maven）──────────────────────────────────────────────

def _make_spring_boot(tmp_path, *, port_file=None, port_text=None, actuator=False,
                      boot_plugin=True):
    deps = ""
    if actuator:
        deps = (
            "<dependency><groupId>org.springframework.boot</groupId>"
            "<artifactId>spring-boot-starter-actuator</artifactId></dependency>"
        )
    plugin = (
        "<plugin><groupId>org.springframework.boot</groupId>"
        "<artifactId>spring-boot-maven-plugin</artifactId></plugin>"
    ) if boot_plugin else ""
    (tmp_path / "pom.xml").write_text(
        "<project><dependencies>"
        "<dependency><groupId>org.springframework.boot</groupId>"
        "<artifactId>spring-boot-starter-web</artifactId></dependency>"
        f"{deps}</dependencies><build><plugins>{plugin}</plugins></build></project>",
        encoding="utf-8",
    )
    res = tmp_path / "src" / "main" / "resources"
    res.mkdir(parents=True)
    if port_file:
        (res / port_file).write_text(port_text or "", encoding="utf-8")
    return tmp_path


def test_spring_boot_properties_explicit_port(tmp_path):
    _make_spring_boot(tmp_path, port_file="application.properties",
                      port_text="spring.application.name=demo\nserver.port=9090\n",
                      actuator=True)
    d = derive_runtime_smoke(_stack("Spring Boot (java)", "maven"), str(tmp_path))
    assert isinstance(d, SmokeDerivation)
    assert d.port == 9090
    # evidence 必须记来源文件 + 键
    assert "application.properties" in (d.evidence.get("port") or "")
    assert "server.port" in (d.evidence.get("port") or "")
    assert d.start_cmd is not None and "java -jar" in d.start_cmd and "target/" in d.start_cmd
    assert d.health_path == "/actuator/health"


def test_spring_boot_yml_explicit_port(tmp_path):
    _make_spring_boot(tmp_path, port_file="application.yml",
                      port_text="spring:\n  application:\n    name: demo\nserver:\n  port: 8081\n")
    d = derive_runtime_smoke(_stack("Spring Boot (java)", "maven"), str(tmp_path))
    assert d.port == 8081
    assert "application.yml" in (d.evidence.get("port") or "")


def test_spring_boot_yml_placeholder_default_port(tmp_path):
    _make_spring_boot(tmp_path, port_file="application.yml",
                      port_text="server:\n  port: ${SERVER_PORT:7070}\n")
    d = derive_runtime_smoke(_stack("Spring Boot (java)", "maven"), str(tmp_path))
    assert d.port == 7070


def test_spring_boot_no_config_falls_back_to_framework_default(tmp_path):
    _make_spring_boot(tmp_path)
    d = derive_runtime_smoke(_stack("Spring Boot (java)", "maven"), str(tmp_path))
    assert d.port == 8080
    assert "默认" in (d.evidence.get("port") or "") or "default" in (d.evidence.get("port") or "").lower()


def test_spring_without_boot_plugin_no_start_cmd(tmp_path):
    _make_spring_boot(tmp_path, boot_plugin=False)
    d = derive_runtime_smoke(_stack("Spring Boot (java)", "maven"), str(tmp_path))
    assert d.start_cmd is None  # 无可执行 jar 证据 → 不猜


def test_spring_boot_no_actuator_health_none(tmp_path):
    _make_spring_boot(tmp_path, actuator=False)
    d = derive_runtime_smoke(_stack("Spring Boot (java)", "maven"), str(tmp_path))
    assert d.health_path is None


def test_spring_boot_multimodule_plugin_module_targeted(tmp_path):
    # 多模块：只有 web 模块声明 boot 插件 → jar 路径应指向该模块
    (tmp_path / "pom.xml").write_text(
        "<project><modules><module>demo-web</module></modules></project>", encoding="utf-8")
    web = tmp_path / "demo-web"
    web.mkdir()
    (web / "pom.xml").write_text(
        "<project><build><plugins><plugin>"
        "<artifactId>spring-boot-maven-plugin</artifactId>"
        "</plugin></plugins></build></project>", encoding="utf-8")
    d = derive_runtime_smoke(_stack("Spring Boot (java)", "maven"), str(tmp_path))
    assert d.start_cmd is not None and "demo-web/target/" in d.start_cmd


# ── Node（express/nest 系）───────────────────────────────────────────────

def test_express_scripts_start_and_env_port(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"name":"svc","scripts":{"start":"node server.js"},"dependencies":{"express":"^4"}}',
        encoding="utf-8")
    (tmp_path / ".env").write_text("PORT=4000\nDEBUG=1\n", encoding="utf-8")
    d = derive_runtime_smoke(_stack("Express (javascript/typescript)", "npm"), str(tmp_path))
    assert d.start_cmd == "npm run start"
    assert d.port == 4000
    assert ".env" in (d.evidence.get("port") or "") and "PORT" in (d.evidence.get("port") or "")


def test_express_default_port_when_no_env(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts":{"start":"node index.js"}}', encoding="utf-8")
    d = derive_runtime_smoke(_stack("Express (javascript/typescript)", "npm"), str(tmp_path))
    assert d.port == 3000


def test_node_scripts_dev_only(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts":{"dev":"nodemon server.js"}}', encoding="utf-8")
    d = derive_runtime_smoke(_stack("Express (javascript/typescript)", "npm"), str(tmp_path))
    assert d.start_cmd == "npm run dev"


def test_node_no_start_script_none(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"name":"lib","scripts":{"build":"tsc"}}', encoding="utf-8")
    d = derive_runtime_smoke(_stack("Express (javascript/typescript)", "npm"), str(tmp_path))
    assert d.start_cmd is None


def test_node_malformed_package_json_no_crash(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {broken', encoding="utf-8")
    d = derive_runtime_smoke(_stack("Express (javascript/typescript)", "npm"), str(tmp_path))
    assert d.start_cmd is None  # 坏文件 → None，不抛


# ── Python（django/flask/fastapi/project.scripts）────────────────────────

def test_django_manage_py(tmp_path):
    (tmp_path / "manage.py").write_text("#!/usr/bin/env python\n", encoding="utf-8")
    d = derive_runtime_smoke(_stack("Django (python)", "pip"), str(tmp_path))
    assert d.start_cmd is not None and "manage.py" in d.start_cmd and "runserver" in d.start_cmd
    assert d.port == 8000


def test_flask_app_py(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask==3.0\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "from flask import Flask\napp = Flask(__name__)\n", encoding="utf-8")
    d = derive_runtime_smoke(_stack("Flask (python)", "pip"), str(tmp_path))
    assert d.start_cmd is not None and "app.py" in d.start_cmd
    assert d.port == 5000


def test_fastapi_uvicorn_main(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")
    (tmp_path / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
    d = derive_runtime_smoke(_stack("FastAPI (python)", "pip"), str(tmp_path))
    assert d.start_cmd is not None and "uvicorn" in d.start_cmd and "main:app" in d.start_cmd
    assert d.port == 8000


def test_python_no_entry_evidence_none(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    d = derive_runtime_smoke(_stack("python", "pip"), str(tmp_path))
    assert d.start_cmd is None
    assert d.port is None  # 无框架 → 默认表也不命中


def test_pyproject_project_scripts_entry(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "svc"\n[project.scripts]\nsvc = "svc.app:main"\n',
        encoding="utf-8")
    d = derive_runtime_smoke(_stack("python", "pip"), str(tmp_path))
    assert d.start_cmd is not None and "svc.app" in d.start_cmd and "main" in d.start_cmd


# ── Go / Rust ────────────────────────────────────────────────────────────

def test_go_main_package_root(tmp_path):
    (tmp_path / "go.mod").write_text("module demo\n\ngo 1.22\n", encoding="utf-8")
    (tmp_path / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
    d = derive_runtime_smoke(_stack("Gin (go)", "go"), str(tmp_path))
    assert d.start_cmd == "go run ."
    assert d.port == 8080  # gin 默认表


def test_go_no_main_package_none(tmp_path):
    (tmp_path / "go.mod").write_text("module lib\n\ngo 1.22\n", encoding="utf-8")
    (tmp_path / "util.go").write_text("package lib\n", encoding="utf-8")
    d = derive_runtime_smoke(_stack("go", "go"), str(tmp_path))
    assert d.start_cmd is None


def test_rust_src_main(tmp_path):
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "svc"\n', encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    d = derive_runtime_smoke(_stack("rust", "cargo"), str(tmp_path))
    assert d.start_cmd == "cargo run"
    assert d.port is None  # 裸语言无框架 → 端口不猜


def test_rust_lib_only_none(tmp_path):
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "lib"\n', encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("", encoding="utf-8")
    d = derive_runtime_smoke(_stack("rust", "cargo"), str(tmp_path))
    assert d.start_cmd is None


# ── 完全推不出 / 容错 ─────────────────────────────────────────────────────

def test_unknown_stack_all_none(tmp_path):
    d = derive_runtime_smoke(_stack("未判明", "未判明"), str(tmp_path))
    assert d.start_cmd is None and d.port is None
    assert d.health_path is None and d.migration_kind is None


def test_empty_stack_dict_no_crash(tmp_path):
    d = derive_runtime_smoke({}, str(tmp_path))
    assert d.start_cmd is None and d.port is None


def test_nonexistent_path_no_crash():
    d = derive_runtime_smoke(_stack("Spring Boot (java)", "maven"),
                             "/nonexistent/definitely/not/here")
    assert isinstance(d, SmokeDerivation)
    assert d.start_cmd is None
    assert d.port == 8080  # 框架默认表仍可用（不依赖磁盘）


def test_binary_garbage_config_no_crash(tmp_path):
    _make_spring_boot(tmp_path)
    res = tmp_path / "src" / "main" / "resources"
    (res / "application.yml").write_bytes(b"\x00\xff\xfe{{{{:::\x01port\x02")
    d = derive_runtime_smoke(_stack("Spring Boot (java)", "maven"), str(tmp_path))
    assert d.port == 8080  # 解析不出 → 回落默认，不抛


# ── migration_kind ───────────────────────────────────────────────────────

def test_migration_flyway_dir(tmp_path):
    mig = tmp_path / "src" / "main" / "resources" / "db" / "migration"
    mig.mkdir(parents=True)
    (mig / "V1__init.sql").write_text("create table t(id int);", encoding="utf-8")
    kind, ev = detect_migration_kind(str(tmp_path))
    assert kind == "flyway"
    assert ev and "db/migration" in ev


def test_migration_liquibase_changelog(tmp_path):
    res = tmp_path / "src" / "main" / "resources"
    res.mkdir(parents=True)
    (res / "db.changelog-master.xml").write_text("<databaseChangeLog/>", encoding="utf-8")
    kind, _ = detect_migration_kind(str(tmp_path))
    assert kind == "liquibase"


def test_migration_alembic(tmp_path):
    al = tmp_path / "alembic"
    (al / "versions").mkdir(parents=True)
    (al / "env.py").write_text("", encoding="utf-8")
    (tmp_path / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    kind, _ = detect_migration_kind(str(tmp_path))
    assert kind == "alembic"


def test_migration_prisma(tmp_path):
    (tmp_path / "prisma" / "migrations" / "20240101_init").mkdir(parents=True)
    kind, _ = detect_migration_kind(str(tmp_path))
    assert kind == "prisma"


def test_migration_golang_migrate(tmp_path):
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "0001_init.up.sql").write_text("create table t(id int);", encoding="utf-8")
    (mig / "0001_init.down.sql").write_text("drop table t;", encoding="utf-8")
    kind, _ = detect_migration_kind(str(tmp_path))
    assert kind == "golang-migrate"


def test_migration_raw_sql(tmp_path):
    db = tmp_path / "sql"
    db.mkdir()
    (db / "schema.sql").write_text("create table t(id int);", encoding="utf-8")
    kind, _ = detect_migration_kind(str(tmp_path))
    assert kind == "raw-sql"


def test_migration_none(tmp_path):
    (tmp_path / "README.md").write_text("hi", encoding="utf-8")
    kind, ev = detect_migration_kind(str(tmp_path))
    assert kind is None and ev is None


def test_migration_flows_into_derivation(tmp_path):
    _make_spring_boot(tmp_path)
    mig = tmp_path / "src" / "main" / "resources" / "db" / "migration"
    mig.mkdir(parents=True)
    (mig / "V1__init.sql").write_text("create table t(id int);", encoding="utf-8")
    d = derive_runtime_smoke(_stack("Spring Boot (java)", "maven"), str(tmp_path))
    assert d.migration_kind == "flyway"
    assert "migration_kind" in d.evidence


# ── prepare_cmd（F1：start_cmd 消费构建产物时才推导）──────────────────────

def test_maven_boot_derives_package_prepare(tmp_path):
    # F1 正例：java -jar target/*.jar 消费 jar 产物，而全链只 mvn compile 从不 package
    # → 必须推导 prepare_cmd，否则冒烟永远 no such file
    _make_spring_boot(tmp_path)
    d = derive_runtime_smoke(_stack("Spring Boot (java)", "maven"), str(tmp_path))
    assert d.start_cmd is not None and "target/*.jar" in d.start_cmd
    assert d.prepare_cmd == "mvn -q -DskipTests package"
    assert "prepare_cmd" in d.evidence  # 推导必留痕


def test_no_boot_plugin_no_prepare(tmp_path):
    # 无 boot 插件 → start_cmd None → prepare 无的放矢，必须 None
    _make_spring_boot(tmp_path, boot_plugin=False)
    d = derive_runtime_smoke(_stack("Spring Boot (java)", "maven"), str(tmp_path))
    assert d.start_cmd is None
    assert d.prepare_cmd is None


def _make_gradle_boot(tmp_path, *, wrapper: bool):
    (tmp_path / "build.gradle").write_text(
        "plugins { id 'org.springframework.boot' version '3.2.0' }\n", encoding="utf-8")
    if wrapper:
        (tmp_path / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    return tmp_path


def test_gradle_boot_prepare_uses_wrapper_when_present(tmp_path):
    _make_gradle_boot(tmp_path, wrapper=True)
    d = derive_runtime_smoke(_stack("Spring Boot (java)", "gradle"), str(tmp_path))
    assert d.start_cmd is not None and "build/libs/*.jar" in d.start_cmd
    assert d.prepare_cmd == "./gradlew bootJar -x test -q"


def test_gradle_boot_prepare_falls_back_without_wrapper(tmp_path):
    _make_gradle_boot(tmp_path, wrapper=False)
    d = derive_runtime_smoke(_stack("Spring Boot (java)", "gradle"), str(tmp_path))
    assert d.prepare_cmd == "gradle bootJar -x test -q"


def test_node_and_go_start_cmd_no_prepare(tmp_path):
    # node（npm run start 直接跑源码）/ go（go run 自建）→ prepare 恒 None：
    # 依赖缺失由运行时三分类的 dependency_missing 诚实归类，不在 prepare 面伪装
    (tmp_path / "package.json").write_text(
        '{"scripts":{"start":"node server.js"}}', encoding="utf-8")
    d = derive_runtime_smoke(_stack("Express (javascript/typescript)", "npm"), str(tmp_path))
    assert d.start_cmd == "npm run start" and d.prepare_cmd is None

    godir = tmp_path / "gosvc"
    godir.mkdir()
    (godir / "go.mod").write_text("module svc\n", encoding="utf-8")
    (godir / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")
    d2 = derive_runtime_smoke(_stack("Gin (go)", "go"), str(godir))
    assert d2.start_cmd == "go run ." and d2.prepare_cmd is None


# ── 绝不抛异常（模糊输入扫射）────────────────────────────────────────────

@pytest.mark.parametrize("stack", [
    None, {}, {"backend": None}, {"backend": 42}, {"backend": "((("},
    {"backend": "Spring Boot (java)", "build": None},
])
def test_never_raises_on_weird_stack(stack, tmp_path):
    d = derive_runtime_smoke(stack, str(tmp_path))
    assert isinstance(d, SmokeDerivation)
