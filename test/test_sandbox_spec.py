"""项目环境规格推断引擎单测 — project/sandbox_spec.py。

固化核心规则（docs/Project_Scoped_Sandbox_Design.md §4.5）：
按构建文件判工具链（非扩展名）、多模块 Maven 聚合、纯静态资源不装 node、混编取并集、空项目 base_only。
用临时目录造场景，无外部依赖。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.project.sandbox_spec import EnvSpec, find_build_files, infer_env_spec


def _write(p: Path, content: str = "{}") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_maven_multimodule_detects_java17(tmp_path):
    """多模块 Maven → java/17/maven，聚合所有子模块 pom。"""
    _write(tmp_path / "pom.xml", """<project><properties>
        <java.version>17</java.version></properties>
        <modules><module>mod-a</module><module>mod-b</module></modules></project>""")
    _write(tmp_path / "mod-a" / "pom.xml")
    _write(tmp_path / "mod-b" / "pom.xml")
    spec = infer_env_spec(tmp_path, "p1")
    assert not spec.base_only
    assert len(spec.toolchains) == 1
    tc = spec.toolchains[0]
    assert tc.name == "java" and tc.version == "17" and tc.build_tool == "maven"
    assert tc.extra["module_count"] == 3
    assert tc.dep_source == "pom.xml"  # 根 pom
    print("  ✅ 多模块 Maven → java/17, 聚合3个pom")


def test_static_js_no_node(tmp_path):
    """纯静态 .js（无 package.json）→ 不装 node（按构建文件非扩展名）。"""
    _write(tmp_path / "static" / "app.js", "console.log(1)")
    _write(tmp_path / "static" / "lib.js", "var x=1")
    _write(tmp_path / "index.html", "<html></html>")
    spec = infer_env_spec(tmp_path, "p2")
    assert spec.base_only  # 无任何构建文件
    assert not any(t.name == "node" for t in spec.toolchains)
    print("  ✅ 纯静态 js/html → 不装 node (base_only)")


def test_package_json_without_build_script_no_node(tmp_path):
    """有 package.json 但无 build/test/start 脚本 → 视为静态资源，不装 node。"""
    _write(tmp_path / "package.json", json.dumps({"name": "x", "scripts": {"lint": "eslint"}}))
    spec = infer_env_spec(tmp_path, "p3")
    assert not any(t.name == "node" for t in spec.toolchains)
    assert any("静态资源" in n for n in spec.notes)
    print("  ✅ package.json 无 build 脚本 → 不装 node")


def test_package_json_with_build_installs_node(tmp_path):
    """有 package.json 且含 build 脚本 → 装 node。"""
    _write(tmp_path / "package.json", json.dumps(
        {"name": "x", "scripts": {"build": "vite build"}, "engines": {"node": ">=20"}}))
    spec = infer_env_spec(tmp_path, "p4")
    node = next((t for t in spec.toolchains if t.name == "node"), None)
    assert node is not None and node.version == "20"
    print("  ✅ package.json 有 build 脚本 → 装 node/20")


def test_mixed_stack_union(tmp_path):
    """混编：pom.xml + package.json(有build) → 工具链取并集 java+node。"""
    _write(tmp_path / "pom.xml", "<project><properties><java.version>17</java.version></properties></project>")
    _write(tmp_path / "frontend" / "package.json", json.dumps({"scripts": {"build": "x"}}))
    spec = infer_env_spec(tmp_path, "p5")
    names = {t.name for t in spec.toolchains}
    assert names == {"java", "node"}, f"期望 java+node 并集, 得到 {names}"
    print("  ✅ 混编 pom+package.json → java+node 并集")


def test_empty_project_base_only(tmp_path):
    """全新空项目（无构建文件）→ base_only。"""
    _write(tmp_path / "README.md", "# new project")
    spec = infer_env_spec(tmp_path, "p6")
    assert spec.base_only
    assert not spec.toolchains
    assert any("全新项目" in n for n in spec.notes)
    print("  ✅ 空项目 → base_only, 等需求分析补装")


def test_python_go_rust(tmp_path):
    """单语言探测：python/go/rust。"""
    _write(tmp_path / "requirements.txt", "flask\n")
    spec = infer_env_spec(tmp_path, "p7")
    assert any(t.name == "python" and t.build_tool == "pip" for t in spec.toolchains)
    print("  ✅ requirements.txt → python/pip")


def test_deps_hash_stable_and_sensitive(tmp_path):
    """deps_hash：同规格稳定，依赖变则变。"""
    _write(tmp_path / "go.mod", "module x\ngo 1.22\n")
    s1 = infer_env_spec(tmp_path, "p8")
    s2 = infer_env_spec(tmp_path, "p8")
    assert s1.deps_hash() == s2.deps_hash()  # 稳定
    _write(tmp_path / "pom.xml", "<project></project>")  # 加 maven
    s3 = infer_env_spec(tmp_path, "p8")
    assert s3.deps_hash() != s1.deps_hash()  # 敏感
    print("  ✅ deps_hash 稳定且对依赖变化敏感")


if __name__ == "__main__":
    import tempfile
    for fn in [test_maven_multimodule_detects_java17, test_static_js_no_node,
               test_package_json_without_build_script_no_node, test_package_json_with_build_installs_node,
               test_mixed_stack_union, test_empty_project_base_only, test_python_go_rust,
               test_deps_hash_stable_and_sensitive]:
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))
    print("\n✅ sandbox_spec 全部测试通过")
