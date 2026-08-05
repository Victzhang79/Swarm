"""L1 构建/测试命令的统一驱动注册表（BRAIN-001/W-3）。

把散落在 `l1_pipeline._derive_full_build_command`、`_guess_test_cmd`、
`executor_sync._module_source_files` 里的各栈字面量集中到 STACK_SPEC 的单一事实源，
再通过本模块派生 driver 视图。新增栈/改命令时只改 `stacks/spec.py`，L1/L2 口径自动一致。
"""
from __future__ import annotations

from dataclasses import dataclass

from swarm.stacks.spec import STACK_SPEC, StackSpec


@dataclass(frozen=True)
class BuildDriver:
    """构建驱动：承载命令模板、模块锚点、源扩展名与排除目录。"""

    stack_key: str
    lang: str
    build_cmd: str          # 整工程（L2/集成复核）命令
    command_tokens: tuple[str, ...]  # 在 build_command 字符串里匹配该栈的词元
    source_exts: tuple[str, ...]
    source_exclude_dirs: tuple[str, ...]
    anchor_manifests: tuple[str, ...]
    shares_classpath_namespace: bool = False


@dataclass(frozen=True)
class TestDriver:
    """测试驱动：工程级测试命令（scoped 测试仍由调用方按改动文件包裹）。"""

    stack_key: str
    lang: str
    test_cmd: str
    anchor_manifests: tuple[str, ...]


def _command_tokens(spec: StackSpec) -> tuple[str, ...]:
    """build_command 字符串里识别该栈的词元。保持与原 `_STACK_DRIVERS` 一致，
    含词边界正则（go 需防 car【go】误中）。"""
    base = {
        "maven": ("mvn",),
        "gradle": ("gradle",),
        "npm": ("npm ", "pnpm", "yarn", "tsc", "npx tsc"),
        "go": (r"re:(?<![\w/.-])go(?:\s|$)",),
        "cargo": ("cargo",),
        "python": (),  # python compileall 不走模块源码树收集
    }
    return base.get(spec.key, (spec.key,))


def _to_driver(spec: StackSpec) -> BuildDriver:
    # 模块锚点优先用 module_manifest；有 .kts 等别名时也纳入，与 STACK_SPEC 派生视图一致。
    anchors = [spec.module_manifest]
    anchors.extend(spec.module_extra_manifests)
    # go/npm 等聚合清单同时可作为模块锚点（go.work/package.json 既聚合又模块）
    if spec.aggregate_manifest and spec.aggregate_manifest not in anchors:
        anchors.append(spec.aggregate_manifest)
    for a in spec.aggregate_extra_manifests:
        if a not in anchors:
            anchors.append(a)
    return BuildDriver(
        stack_key=spec.key,
        lang=spec.lang,
        build_cmd=spec.whole_project_build_cmd,
        command_tokens=_command_tokens(spec),
        source_exts=spec.source_exts,
        source_exclude_dirs=spec.source_exclude_dirs,
        anchor_manifests=tuple(anchors),
        shares_classpath_namespace=spec.shares_classpath_namespace,
    )


def _to_test_driver(spec: StackSpec) -> TestDriver | None:
    if not spec.test_cmd:
        return None
    anchors = [spec.module_manifest]
    anchors.extend(spec.module_extra_manifests)
    if spec.aggregate_manifest and spec.aggregate_manifest not in anchors:
        anchors.append(spec.aggregate_manifest)
    for a in spec.aggregate_extra_manifests:
        if a not in anchors:
            anchors.append(a)
    return TestDriver(
        stack_key=spec.key,
        lang=spec.lang,
        test_cmd=spec.test_cmd,
        anchor_manifests=tuple(anchors),
    )


# 启动时从 STACK_SPEC 构造，保证一份事实源。
BUILD_DRIVERS: dict[str, BuildDriver] = {
    spec.key: _to_driver(spec) for spec in STACK_SPEC.values()
}
TEST_DRIVERS: dict[str, TestDriver] = {
    spec.key: driver for spec in STACK_SPEC.values()
    if (driver := _to_test_driver(spec)) is not None
}


def build_driver(stack_key: str | None) -> BuildDriver | None:
    """栈键 → 构建驱动。未收录返回 None（fail-closed：不臆造命令）。"""
    if not stack_key:
        return None
    return BUILD_DRIVERS.get(stack_key.strip().lower())


def test_driver(stack_key: str | None) -> TestDriver | None:
    """栈键 → 测试驱动。未收录或 test_cmd 留空返回 None。"""
    if not stack_key:
        return None
    return TEST_DRIVERS.get(stack_key.strip().lower())


def source_exts_for(stack_key: str | None) -> tuple[str, ...]:
    """未收录栈返回空元组——调用方据此知道"本表未收录该栈源码扩展名"。"""
    drv = build_driver(stack_key)
    return drv.source_exts if drv else ()
