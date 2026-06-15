"""批1 回归：项目专属沙箱镜像自带完整源码（方案 B）。

验证 image_builder：
- Dockerfile 装 git（消除 worker `git diff` 127）
- src_included=True 时 COPY project_src/ /workspace/（编译闭包完整）
- _make_source_tarball 通用排除构建产物（不针对任何项目）
- _selftest_command 按 EnvSpec 工具链推导离线编译自测（不写死模块名）
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from swarm.project.sandbox_spec import EnvSpec, Toolchain
from swarm.worker.image_builder import (
    _make_source_tarball,
    _selftest_command,
    generate_dockerfile,
)


def _maven_spec():
    return EnvSpec(project_id="proj123456789", toolchains=[
        Toolchain(name="java", version="17", build_tool="maven", dep_source="pom.xml"),
    ])


def test_dockerfile_installs_git():
    df = generate_dockerfile(_maven_spec())
    assert "install" in df and "git" in df, "Dockerfile 应安装 git"


def test_dockerfile_copies_source_when_included():
    df = generate_dockerfile(_maven_spec(), src_included=True)
    assert "COPY project_src/ /workspace/" in df, "src_included 应 COPY 源码进 /workspace"


def test_dockerfile_no_source_when_not_included():
    df = generate_dockerfile(_maven_spec(), src_included=False)
    assert "COPY project_src/" not in df


def test_base_only_still_has_git_no_source():
    df = generate_dockerfile(EnvSpec(project_id="p", base_only=True), src_included=True)
    assert "git" in df, "空项目镜像也装 git"
    assert "COPY project_src/" not in df, "base_only 不 COPY 源码"


def test_source_tarball_excludes_build_artifacts(tmp_path):
    # 造一个含源码 + 构建产物的项目
    (tmp_path / "src" / "main").mkdir(parents=True)
    (tmp_path / "src" / "main" / "App.java").write_text("class App {}")
    (tmp_path / "pom.xml").write_text("<project/>")
    # 构建产物（应被排除）
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "App.class").write_text("BINARY")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("module")

    data = _make_source_tarball(tmp_path)
    names = set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = set(tar.getnames())
    assert "src/main/App.java" in names, "源码应保留"
    assert "pom.xml" in names, "构建描述文件应保留"
    assert not any("target" in n for n in names), "target/ 应排除"
    assert not any(".git" in n for n in names), ".git/ 应排除"
    assert not any("node_modules" in n for n in names), "node_modules/ 应排除"
    assert not any(n.endswith(".class") for n in names), ".class 应排除"


def test_selftest_command_maven_generic():
    """maven 自测命令通用：编译整个 reactor，不写死项目模块名。"""
    cmd = _selftest_command(_maven_spec())
    assert cmd and "mvn -o" in cmd and "compile" in cmd
    # 不应包含任何具体项目模块名（如 ruoyi-common）
    assert "ruoyi" not in cmd.lower(), "自测命令不应写死任何项目的模块名"


def test_selftest_command_per_toolchain():
    py = EnvSpec(project_id="p", toolchains=[Toolchain(name="python", build_tool="pip", dep_source="requirements.txt")])
    assert "compileall" in (_selftest_command(py) or "")
    node = EnvSpec(project_id="p", toolchains=[Toolchain(name="node", build_tool="npm", dep_source="package.json")])
    assert "npm" in (_selftest_command(node) or "")
    go = EnvSpec(project_id="p", toolchains=[Toolchain(name="go", build_tool="go", dep_source="go.mod")])
    assert "go build" in (_selftest_command(go) or "")


def test_selftest_none_for_base_only():
    assert _selftest_command(EnvSpec(project_id="p", base_only=True)) is None


def test_source_tarball_uses_git_head_not_workdir(tmp_path):
    """方案 B 基线一致性：git 仓库导出 HEAD 版，工作区脏改动/untracked 排除。

    避免"工作区未提交改动进镜像、worker 覆盖的是 HEAD 版"导致镜像内文件不一致。
    """
    import subprocess as sp

    def git(*a):
        sp.run(["git", *a], cwd=tmp_path, capture_output=True, check=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "A.java").write_text("class A { /* HEAD */ }")
    (tmp_path / "pom.xml").write_text("<project/>")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    # 工作区弄脏 + untracked
    (tmp_path / "A.java").write_text("class A { /* DIRTY */ }")
    (tmp_path / "B.java").write_text("class B {}")

    data = _make_source_tarball(tmp_path)
    contents, names = {}, []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            names.append(m.name)
            if m.name == "A.java":
                contents["A.java"] = tar.extractfile(m).read().decode()
    assert "HEAD" in contents["A.java"], "应导出 git HEAD 版"
    assert "DIRTY" not in contents["A.java"], "不应含工作区未提交改动"
    assert "B.java" not in names, "untracked 文件不应进 HEAD 归档"


if __name__ == "__main__":
    import tempfile
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        import inspect
        if "tmp_path" in inspect.signature(fn).parameters:
            with tempfile.TemporaryDirectory() as d:
                fn(Path(d))
        else:
            fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== 批1 image_builder 源码进镜像: {len(fns)}/{len(fns)} passed ===")
