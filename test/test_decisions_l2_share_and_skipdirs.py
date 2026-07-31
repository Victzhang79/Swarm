#!/usr/bin/env python3
"""用户拍板的决定 2 / 决定 4（27 号文 M-1/M-6 与 X-C2 遗留）。

- **决定 2**：`integration_review._detect_build_cmd_generic`（L2）与
  `l1_pipeline._derive_full_build_command`（L1）是同职责 sibling，各写一份 if 链 ⇒ 必然漂移。
  治法＝**共享事实、各自投影**：栈→整工程命令搬进 `stacks.STACK_SPEC.whole_project_build_cmd`，
  L2 只做"匹配根清单 → 取该栈命令"；L1 保留自己的 scope 逻辑（锚定 / 只编改动文件）。
- **决定 4**：`.yarn`/`third_party`/`vendor` 等依赖树里的清单不再算构建入口。
"""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.integration_review import _detect_build_cmd_generic  # noqa: E402
from swarm.stacks import STACK_SPEC  # noqa: E402


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


# ══════════════════════════════════════════════
# 决定 2：L2 与 L1 共享 STACK_SPEC，L2 行为逐字不变
# ══════════════════════════════════════════════

_L2_EQUIV = [
    ("maven", {"pom.xml": "<project/>"}, "mvn -q -DskipTests compile"),
    ("gradle", {"settings.gradle": "x"},
     "./gradlew -q classes 2>/dev/null || gradle -q classes"),
    ("gradle-kts", {"build.gradle.kts": "x"},
     "./gradlew -q classes 2>/dev/null || gradle -q classes"),
    ("go.mod", {"go.mod": "module x"}, "go build ./..."),
    ("go.work", {"go.work": "use ./a"}, "go build ./..."),
    ("cargo", {"Cargo.toml": "[package]"}, "cargo build -q"),
    ("npm+build", {"package.json": '{"scripts":{"build":"vite"}}'}, "npm run build"),
    ("npm+tsc", {"package.json": "{}", "tsconfig.json": "{}"},
     "npx tsc --noEmit --pretty false"),
    ("npm 纯 JS", {"package.json": "{}"}, None),
    ("pyproject", {"pyproject.toml": "[project]"}, "python -m compileall -q ."),
    ("requirements", {"requirements.txt": "flask"}, "python -m compileall -q ."),
    ("纯 docs", {"README.md": "x"}, None),
]


@pytest.mark.parametrize("name,files,want", _L2_EQUIV, ids=[c[0] for c in _L2_EQUIV])
def test_d2_l2_command_is_byte_equivalent_after_sharing(name, files, want, tmp_path):
    """★决定 2 的立论：L2 行为**逐字等价**★ 命令串是从旧 if 链原样搬进 STACK_SPEC 的，
    所以这十二种形态一个都不许变——尤其 maven（唯一跑过 E2E 的栈）。"""
    assert _detect_build_cmd_generic(str(_tree(tmp_path, files))) == want


def test_d2_l2_reads_the_shared_table_not_its_own_chain():
    """结构断言：L2 必须**从表里取**命令，而不是自己再写一遍 if 链。
    判据＝把表里某个栈的命令改掉，L2 的输出必须跟着变（下面那条突变锁它）。
    这里先断"表里真有这些事实"，否则"共享"是空话。"""
    for key, want in (("maven", "mvn -q -DskipTests compile"),
                      ("go", "go build ./..."),
                      ("cargo", "cargo build -q"),
                      ("python", "python -m compileall -q .")):
        assert STACK_SPEC[key].whole_project_build_cmd == want, f"{key} 的事实不在表里"
    # npm 刻意留空：它是条件式（要读 package.json 内容），不是纯静态事实
    assert STACK_SPEC["npm"].whole_project_build_cmd == ""


def test_d2_table_gap_is_observable(tmp_path, caplog):
    """表里有该栈却没给整工程命令 ⇒ 必须 WARNING（硬检查④），不静默当"没有构建"。"""
    import logging

    import swarm.brain.integration_review as ir
    from swarm.stacks.spec import StackSpec
    stub = StackSpec(key="maven", lang="java", root_manifests=("pom.xml",),
                     module_manifest="pom.xml", aggregate_manifest="pom.xml",
                     aggregate_field="<modules>", source_exts=(".java",),
                     whole_project_build_cmd="")          # ← 事实缺失
    real = dict(STACK_SPEC)
    real["maven"] = stub
    _tree(tmp_path, {"pom.xml": "<project/>"})
    import swarm.stacks as _st
    orig = _st.STACK_SPEC
    _st.STACK_SPEC = real
    try:
        with caplog.at_level(logging.WARNING):
            assert ir._detect_build_cmd_generic(str(tmp_path)) is None
    finally:
        _st.STACK_SPEC = orig
    assert any("whole_project_build_cmd" in r.message for r in caplog.records), \
        "表缺项被静默吞掉 ⇒ L2 会当成『没有构建』跳过编译"


# ══════════════════════════════════════════════
# 决定 4：依赖树里的清单不算构建入口
# ══════════════════════════════════════════════

_D4_DECOYS = ["node_modules/x", "vendor/x", "third_party/x", ".yarn/cache/x",
              ".tox/py39", "bower_components/x", ".pnpm-store/x"]


@pytest.mark.parametrize("decoy", _D4_DECOYS)
def test_d4_dependency_tree_manifests_are_not_build_entrypoints(decoy, tmp_path):
    """★决定 4（用户拍板）★ 本会话已实测同型危害：`node_modules/**/pyproject.toml` 劫持了
    **所有栈**的测试闸、`node_modules/**/*.csproj` 让 `dotnet build` 在错目录跑 → 127 → BLOCKED。
    ★判据按**目录语义**列，不按"是否隐藏"★——CLAUDE.md 血泪：拿"隐藏目录=噪声"做镜像 tarball
    剔除，把 `.mvn/wrapper`、`.yarn/releases` 剔没了。"""
    from swarm.project.sandbox_spec import find_build_files
    root = _tree(tmp_path, {f"{decoy}/package.json": '{"scripts":{"build":"x"}}',
                            f"{decoy}/pyproject.toml": "[project]"})
    bf = find_build_files(root)
    assert not bf.get("npm"), f"{decoy} 里的 package.json 被当成构建入口"
    assert not bf.get("python"), f"{decoy} 里的 pyproject.toml 被当成构建入口"


def test_d4_real_workspace_manifests_still_found(tmp_path):
    """反向臂：真工程的清单一个都不许漏（别把发现面整个关掉）。"""
    from swarm.project.sandbox_spec import find_build_files, infer_env_spec
    root = _tree(tmp_path, {
        "package.json": '{"name":"r","private":true,"workspaces":["packages/*"]}',
        "packages/web/package.json": '{"name":"w","scripts":{"build":"vite"}}',
        "node_modules/dep/package.json": '{"scripts":{"build":"x"}}',
    })
    assert find_build_files(root).get("npm") == ["package.json",
                                                "packages/web/package.json"]
    env = infer_env_spec(str(root), project_id="p")
    assert [t.name for t in env.toolchains] == ["node"]


def test_d4_yarn_releases_still_ships_in_image():
    """★`.yarn` 进 `_SKIP_DIRS` 不许影响镜像内容★ 两张表的**消费契约不同**：
    `sandbox_spec._SKIP_DIRS` 只管"哪个清单算构建入口"，镜像带什么文件由
    `image_builder` 侧决定。若把两者混为一谈，`.yarn/releases`（yarn 自己的发行版，
    属**构建工具本体**）会被剔掉 ⇒ yarn 起不来（与本��役 C-2 保留 wrapper jar 同一判据）。"""
    from swarm.project.sandbox_spec import _SKIP_DIRS
    from swarm.worker.image_builder import _SRC_EXCLUDE_DIRS
    assert ".yarn" in _SKIP_DIRS, "决定 4 没落地"
    assert ".yarn" not in _SRC_EXCLUDE_DIRS, \
        "`.yarn` 被加进了**镜像**排除表 ⇒ yarn 发行版进不了镜像 ⇒ yarn 起不来"
