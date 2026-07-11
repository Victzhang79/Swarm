#!/usr/bin/env python3
"""R40-4（round40 治本批）—— 多模块 Maven 的 start_cmd 消歧。

取证：round40 冒烟 skipped「推导不全缺 start_cmd」——RuoYi 根 pom（packaging=pom
聚合器）在 <build><plugins> 直接声明 spring-boot-maven-plugin（版本管控惯用法），
与 ruoyi-admin（packaging=jar 真可执行模块）同时命中 → 撞 _derive_start_jvm
「多命中=歧义不猜」护栏 → 四层验证第四层常年空转。
确定性消歧（manifest 证据，不猜）：
  - <packaging>pom</packaging> 聚合器结构上产不出可执行 jar → 排除；
  - 只在 <pluginManagement> 提及（版本钉子非可执行声明）→ 不算命中；
  - 消歧后仍多个 jar 模块命中 → 照旧 None（真歧义 fail-closed 不拔）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.smoke_derive import derive_runtime_smoke  # noqa: E402

_PLUGIN = ("<plugin><groupId>org.springframework.boot</groupId>"
           "<artifactId>spring-boot-maven-plugin</artifactId></plugin>")


def _stack():
    return {"frontend": "服务端模板（Thymeleaf）", "frontend_kind": "server",
            "backend": "Spring Boot (java)", "build": "maven",
            "confidence": 0.95, "evidence": [], "source": "deterministic"}


def _pom(packaging: str, plugins: str = "", plugin_mgmt: str = "") -> str:
    return f"""<project><modelVersion>4.0.0</modelVersion>
<groupId>g</groupId><artifactId>a</artifactId><version>1</version>
<packaging>{packaging}</packaging>
<build>{f'<pluginManagement><plugins>{plugin_mgmt}</plugins></pluginManagement>' if plugin_mgmt else ''}
<plugins>{plugins}</plugins></build></project>"""


def test_ruoyi_shape_aggregator_excluded(tmp_path):
    """根聚合器 pom 直接声明插件（RuoYi 真形态）→ 排除，唯一 jar 模块胜出。"""
    (tmp_path / "pom.xml").write_text(_pom("pom", plugins=_PLUGIN), encoding="utf-8")
    (tmp_path / "ruoyi-admin").mkdir()
    (tmp_path / "ruoyi-admin/pom.xml").write_text(
        _pom("jar", plugins=_PLUGIN), encoding="utf-8")
    (tmp_path / "ruoyi-admin/src/main/resources").mkdir(parents=True)
    (tmp_path / "ruoyi-admin/src/main/resources/application.yml").write_text(
        "server:\n  port: 80\n", encoding="utf-8")
    d = derive_runtime_smoke(_stack(), str(tmp_path))
    assert d.start_cmd is not None and "ruoyi-admin/target/" in d.start_cmd, (
        f"聚合器排除后应唯一命中 ruoyi-admin，got {d.start_cmd!r}")
    assert "ruoyi-admin/pom.xml" in (d.evidence.get("start_cmd") or "")


def test_plugin_management_only_not_counted(tmp_path):
    """只在 pluginManagement 提及=版本钉子非可执行声明 → 不算命中。"""
    (tmp_path / "pom.xml").write_text(_pom("pom"), encoding="utf-8")
    (tmp_path / "mod-lib").mkdir()
    (tmp_path / "mod-lib/pom.xml").write_text(
        _pom("jar", plugin_mgmt=_PLUGIN), encoding="utf-8")
    (tmp_path / "mod-app").mkdir()
    (tmp_path / "mod-app/pom.xml").write_text(
        _pom("jar", plugins=_PLUGIN), encoding="utf-8")
    d = derive_runtime_smoke(_stack(), str(tmp_path))
    assert d.start_cmd is not None and "mod-app/target/" in d.start_cmd


def test_true_ambiguity_still_fail_closed(tmp_path):
    """两个 jar 模块都真声明插件 → 真歧义照旧 None（护栏牙齿不拔）。"""
    for m in ("app-a", "app-b"):
        (tmp_path / m).mkdir()
        (tmp_path / m / "pom.xml").write_text(
            _pom("jar", plugins=_PLUGIN), encoding="utf-8")
    d = derive_runtime_smoke(_stack(), str(tmp_path))
    assert d.start_cmd is None
