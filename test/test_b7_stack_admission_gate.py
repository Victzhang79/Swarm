"""B-7（27 号文）新栈准入闸 + X-M6 发现面/准入口径对账。

规则（任一违反即红）：
  ① `_MANIFEST_BACKEND`（stack_detect 检测侧认得的清单）每个 basename 必须
     【有 driver】（STACK_SPEC 派生集）或【显式 unsupported 登记】
     （integration_review._UNSUPPORTED_STACK_MANIFESTS）——不得有第三态
     （检测侧拿它定栈、driver 侧无人认领、又没人承认"不支持"）。
  ② 两表互斥：同一清单不得既在 STACK_SPEC 又在 unsupported 登记（矛盾登记）。
  ③ `find_build_files`（X-M6 扩面后）返回的每个 kind 必须【有工具链分派】或
     在 `_UNSUPPORTED_TOOLCHAIN_KINDS` 显式登记——只扩发现面不扩准入=新栈被
     静默当已知栈处理，比不扩更坏。
  ④ sandbox 未支持 kind 必须与 integration_review 的未支持栈登记对得上
     （两张"不支持"表不得各说各话）。
"""
from __future__ import annotations

import json

from swarm.brain.integration_review import (
    _UNSUPPORTED_STACK_MANIFESTS,
)
from swarm.brain.stack_detect import _MANIFEST_BACKEND
from swarm.project.sandbox_spec import (
    _UNSUPPORTED_TOOLCHAIN_KINDS,
    find_build_files,
    infer_env_spec,
)
from swarm.stacks import STACK_SPEC

_SPEC_MANIFESTS = {m for s in STACK_SPEC.values()
                   for m in (*s.root_manifests, s.module_manifest,
                             *s.module_extra_manifests)}
_UNSUPPORTED_NAMES = {name for name, _ in _UNSUPPORTED_STACK_MANIFESTS}


def test_manifest_backend_every_entry_has_driver_or_unsupported_registration():
    """准入闸 ①：检测侧认得的清单必须有主（driver 或显式不支持）。

    历史缺口实证：`manage.py` 曾只在 _MANIFEST_BACKEND（检测侧认）而无 driver
    归属 → B-7 把它收进 python.root_manifests（Django 确定性证据）。新栈再加
    清单而不走这两条路之一，本条当场红。"""
    gaps = sorted(set(_MANIFEST_BACKEND) - _SPEC_MANIFESTS - _UNSUPPORTED_NAMES)
    assert not gaps, (
        f"清单 {gaps} 在 _MANIFEST_BACKEND 但既无 STACK_SPEC driver 又无 "
        f"unsupported 显式登记（新栈准入闸：二选一，不得静默第三态）")


def test_supported_and_unsupported_registrations_are_disjoint():
    """准入闸 ②：同一清单不得既"有 driver"又"显式不支持"（矛盾登记）。"""
    both = sorted(_SPEC_MANIFESTS & _UNSUPPORTED_NAMES)
    assert not both, f"清单 {both} 同时在 STACK_SPEC 与 unsupported 登记表"


def test_find_build_files_discovers_unsupported_stacks(tmp_path):
    """X-M6：.csproj/.sln/composer.json/Gemfile 进发现面（深度内、跳过表生效）。"""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "App.csproj").write_text("<Project/>")
    (tmp_path / "App.sln").write_text("")
    (tmp_path / "composer.json").write_text("{}")
    (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "composer.json").write_text("{}")  # 跳过表生效
    bf = find_build_files(tmp_path)
    assert set(bf["csharp"]) == {"app/App.csproj", "App.sln"}, bf
    assert bf["php"] == ["composer.json"]
    assert bf["ruby"] == ["Gemfile"]


def test_infer_env_spec_registers_unsupported_kinds_machine_readably(tmp_path):
    """X-M6 准入口径：未支持栈【不装工具链】+ 机读 note + base_only 不谎称"无构建文件"。"""
    (tmp_path / "composer.json").write_text(json.dumps({"name": "x"}))
    (tmp_path / "Gemfile").write_text("source 'x'")
    spec = infer_env_spec(tmp_path)
    assert spec.base_only is True, "无支持栈工具链 → base_only"
    assert not spec.toolchains, "未支持栈绝不猜工具链（血规 2）"
    notes = spec.notes
    assert any(n.startswith("unsupported_toolchain:php:") for n in notes), notes
    assert any(n.startswith("unsupported_toolchain:ruby:") for n in notes), notes
    assert any("仅发现未支持栈构建文件" in n for n in notes), notes
    assert not any(n.startswith("无构建文件") for n in notes), \
        "发现得了构建文件就绝不允许谎称『无构建文件』（认得了但不支持 ≠ 真没有）"


def test_find_build_files_kinds_all_dispatched_or_registered(tmp_path):
    """准入闸 ③：发现面的每个 kind 必须有工具链分派或显式 unsupported 登记。

    打满全 kind 的夹具上跑真 infer_env_spec：kind→toolchain 是生产分派事实，
    新增 kind 而不接分派/登记，本条当场红（防"只扩发现面"复发）。"""
    (tmp_path / "pom.xml").write_text("<project/>")
    (tmp_path / "build.gradle").write_text("plugins {}")
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "tsc"}}))
    (tmp_path / "requirements.txt").write_text("flask")
    (tmp_path / "go.mod").write_text("module example.com/x\n")
    (tmp_path / "Cargo.toml").write_text("[package]\nname = \"x\"\n")
    (tmp_path / "Dockerfile").write_text("FROM scratch")
    (tmp_path / "composer.json").write_text("{}")
    (tmp_path / "Gemfile").write_text("source 'x'")
    (tmp_path / "App.csproj").write_text("<Project/>")
    bf = find_build_files(tmp_path)
    spec = infer_env_spec(tmp_path)
    dispatched_tools = {t.build_tool for t in spec.toolchains}
    kind_to_tool = {"maven": "maven", "gradle": "gradle", "npm": "npm",
                    "python": "pip", "go": "go", "rust": "cargo"}
    for kind in bf:
        if kind == "docker":
            continue  # docker 走 project_dockerfile 注记通道（非工具链）
        if kind in kind_to_tool:
            assert kind_to_tool[kind] in dispatched_tools, \
                f"kind {kind} 应分派工具链 {kind_to_tool[kind]} 却没装上"
        else:
            assert kind in _UNSUPPORTED_TOOLCHAIN_KINDS, \
                f"find_build_files 发现了 kind={kind} 但既无工具链分派又无 " \
                f"unsupported 显式登记（准入闸：只扩发现面=静默当已知栈，比不扩更坏）"
            assert any(n.startswith(f"unsupported_toolchain:{kind}:") for n in spec.notes), \
                f"登记的 kind={kind} 必须落机读 note"


def test_manage_py_discovers_python_toolchain(tmp_path):
    """★R2 hunter M-2★ manage.py 三面口径对账：STACK_SPEC root_manifests（B-7 准入闸）
    与 integration_review 构建面都认它，沙箱发现面【不认】= 纯 Django 工程镜像
    base_only 无 python 工具链，而 L2 侧却认为有构建面——两面漂移。发现面认它之后
    只装工具链；warmup 安全由 image_builder 的 requirements.txt basename 判定保证
    （manage.py 绝不会被当依赖清单喂 pip）。"""
    (tmp_path / "manage.py").write_text("# django\n")
    bf = find_build_files(tmp_path)
    assert "python" in bf and any(p.endswith("manage.py") for p in bf["python"]), bf
    spec = infer_env_spec(tmp_path)
    assert spec.base_only is False, "纯 Django 工程不得落入 base_only（无 python 工具链）"
    assert any(t.name == "python" for t in spec.toolchains), spec.toolchains
    assert not any(n.startswith("无构建文件") for n in spec.notes), spec.notes


def test_unsupported_toolchain_kinds_disjoint_from_dispatched():
    """准入闸 ③ 反面：已分派的 kind 不得同时躺在 unsupported 登记表（矛盾登记）。"""
    dispatched_kinds = {"maven", "gradle", "npm", "python", "go", "rust", "docker"}
    both = sorted(set(_UNSUPPORTED_TOOLCHAIN_KINDS) & dispatched_kinds)
    assert not both, f"kind {both} 既有工具链分派又在 unsupported 登记表"


def test_sandbox_unsupported_kinds_match_integration_review_registration():
    """准入闸 ④：sandbox 侧"不支持"与 integration_review 侧"不支持"同一份事实。

    integration_review 的未支持键：php/ruby/elixir/dart/scala-sbt/csharp/fsharp；
    sandbox 侧登记的 kind 必须 ⊆ 它（sandbox 发现了integration_review 不认的栈=
    L2 构建面三态与镜像工具链各说各话）。"""
    from swarm.brain.integration_review import _UNSUPPORTED_STACK_GLOBS
    ir_keys = {key for _, key in _UNSUPPORTED_STACK_MANIFESTS}
    ir_keys |= {key for _, key in _UNSUPPORTED_STACK_GLOBS}
    gaps = sorted(set(_UNSUPPORTED_TOOLCHAIN_KINDS) - ir_keys)
    assert not gaps, f"sandbox 登记的未支持 kind {gaps} 在 integration_review 无对应登记"
