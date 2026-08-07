#!/usr/bin/env python3
"""P-H5 突变 harness（判据与前批同源）：

  · **先验基线全绿**；· **逐条**跑 should_red；· `rc=5` **判失败**；
  · 落点唯一性检查；· 突变后源码必须仍能 `ast.parse`。

★锁的命题★ P-H5（27 号文）：CONTRACT_MODULE_SYSTEM 的 dependencies 指引按栈注入——
核心=【默认不再是 Maven】（未判明栈给栈中立指引）；证据两路并集（detect_stack build
字段 + base 树清单）；package.json 不在 _MANIFEST_BACKEND 是单一事实源的刻意缺口，
补充表只此一条。五条突变分别压：兜底回 Maven / 调用点占位符不替换 / 树扫描删除 /
build 分派删除 / package.json 补充删除。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_contract_staged.py"]

PN = ROOT / "brain" / "planning_nodes.py"
PROMPTS = ROOT / "brain" / "prompts.py"

MUTATIONS = [
    (
        "P-H5a：未判明栈的兜底回 Maven（P-H5 的核心修复就是【默认不再是 Maven】——"
        "兜底一回 Maven，npm 工程照旧被引导产 Maven 坐标全 drop）",
        PN,
        '        return _CONTRACT_DEP_GUIDANCE_GENERIC, ["generic"]',
        '        return _CONTRACT_DEP_GUIDANCE["maven"], ["maven"]',
        ["test_ph5_unknown_stack_gets_generic_not_maven"],
    ),
    (
        "P-H5b：调用点不替换占位符（指引整段消失、裸占位符进 prompt——接线覆盖≠机制存在）",
        PN,
        '    _module_system = CONTRACT_MODULE_SYSTEM.replace("@@DEP_GUIDANCE@@", _dep_guidance)',
        "    _module_system = CONTRACT_MODULE_SYSTEM",
        ["test_ph5_npm_project_system_prompt_has_no_maven_leak"],
    ),
    (
        "P-H5c：base 树清单扫描被摘（build 字段缺席/未判明时第二证据源归零——"
        "detect_stack 没跑/没判出来的项目只剩栈中立指引）",
        PN,
        "    for p in tree or []:",
        "    for p in []:",
        ["test_ph5_tree_manifests_compensate_missing_build_field"],
    ),
    (
        "P-H5d：build 字段分派被摘（npm 工程拿到栈中立指引而非 npm 指引——"
        "映射表虽不再害它，但 npm 专属坐标形态与「绝不写 Maven 坐标」警示全丢）",
        PN,
        '    _dispatch(build_raw)',
        "    pass  # _dispatch(build_raw)",
        ["test_ph5_guidance_dispatch_by_build_field",
         "test_ph5_npm_project_system_prompt_has_no_maven_leak"],
    ),
    (
        "P-H5e：package.json 补充条目被摘（_MANIFEST_BACKEND 不含 package.json 是"
        "单一事实源的刻意缺口——补充删了，纯前端/全栈 npm 仓在树扫描里隐形）",
        PN,
        "            kk = _TREE_EXTRA_MANIFEST_KEYS.get(base)",
        "            kk = None",
        ["test_ph5_tree_manifests_compensate_missing_build_field"],
    ),
    (
        "P-H5f：坐标形态行分派被摘（双复核 R1-2：sbt/composer 等已知栈只剩 generic——"
        "「已知栈缺指引」与「真未判明」退回不可辨）",
        PN,
        "        elif build in _CONTRACT_DEP_GUIDANCE_FORMS and build not in forms:",
        "        elif False:",
        ["test_ph5_known_stack_without_section_gets_form_line"],
    ),
    (
        "P-H5i：FORMS 表删 sbt 条目（枚举缺口锁的区分力证明——它锁的是【表与单一事实源"
        "同步】这个静态命题，分派突变压不到它，删表条目必须它红）",
        PN,
        '    "sbt": "Scala/sbt：用 groupId % artifactId 坐标（Maven 坐标族）",\n',
        "",
        ["test_ph5_every_detectable_build_has_guidance_or_form",
         "test_ph5_known_stack_without_section_gets_form_line"],
    ),
    (
        "P-H5g：枚举缺口 WARNING 降成 DEBUG（已知栈无档静默退 generic ⇒ 缺口永远没人补，"
        "硬检查④）",
        PN,
        '            logger.warning("[CONTRACT_DESIGN] P-H5 build=%r 无专属指引/形态行（枚举缺口），"',
        '            logger.debug("[CONTRACT_DESIGN] P-H5 build=%r 无专属指引/形态行（枚举缺口），"',
        ["test_ph5_unmappable_build_warns_visibly"],
    ),
    (
        "P-H5h：plan prompt 的 dependencies 示例退回 Maven-only（双复核 R1-3/hunter 6b："
        "contract_design 治好后 plan prompt 是残留污染源，全路径都过它）",
        PROMPTS,
        '"artifacts": ["本栈原生依赖坐标（Maven=groupId:artifactId / npm=包名 / go=module 路径 / PyPI=包名 / cargo=crate 名）"]',
        '"artifacts": ["groupId:artifactId", "org.projectlombok:lombok"]',
        ["test_ph5_plan_prompt_dependencies_example_is_stack_neutral"],
    ),
    (
        "P-H5j：PLAN_BATCH_SYSTEM 的 P7 示例退回 Java/Node-only（复核 R2：第三个 plan 级 "
        "prompt 是我自查漏掉的 sibling——go/rust 工程无形态指引）",
        PROMPTS,
        "坐标按本栈原生形态（Java 如 lombok/各 starter、Node 如运行时+类型依赖、"
        "Go 如 gin/gorm 等 module 路径、Python 如 sqlalchemy/pydantic、Rust 如 serde/tokio）",
        "Java 如 lombok/各 starter，Node 如运行时+类型依赖）",
        ["test_ph5_plan_prompt_dependencies_example_is_stack_neutral"],
    ),
]


def _pytest(args: list[str]) -> int:
    p = subprocess.run([PY, "-m", "pytest", *TESTS, "-p", "no:warnings", "-q",
                        "--tb=no", *args], cwd=ROOT, capture_output=True, text=True)
    return p.returncode



def _clear_pyc(path: Path) -> None:
    """删被突变模块的 pyc（T-2，#29-3 统一补齐）。

    CPython 判 pyc 是否有效看的是源码 **mtime（整秒粒度）+ 字节数**。故当「等长突变
    （len(old)==len(new)）」与「同秒写入」同时成立时，第二条突变写完，pyc 仍被判有效
    ⇒ 子进程加载的是【上一条】的字节码。双向危害：既造"突变后仍绿"（冤报测试没牙），
    也造"红的是上一条"（假背书——这条锁其实没被验证）。
    每条突变前与还原后都必须清。
    """
    cache = path.parent / "__pycache__"
    if cache.is_dir():
        for f in cache.glob(path.stem + ".*.pyc"):
            try:
                f.unlink()
            except OSError:
                pass

def main() -> int:
    print("═" * 70)
    print("步骤 0：基线必须全绿")
    print("═" * 70)
    rc = _pytest([])
    if rc != 0:
        print(f"✗ 基线是红的 (exit={rc}) —— 突变结果全部无意义。先修基线。")
        return 1
    print(f"✓ 基线全绿 (exit={rc})\n")

    failures = []
    for i, (name, path, old, new, should_red) in enumerate(MUTATIONS, 1):
        src = path.read_text()
        if old not in src:
            print(f"[{i}/{len(MUTATIONS)}] {name}\n    ✗ 落点未命中（代码已漂移）")
            failures.append((name, "落点未命中"))
            continue
        if src.count(old) != 1:
            print(f"[{i}/{len(MUTATIONS)}] {name}\n"
                  f"    ✗ 落点出现 {src.count(old)} 次（非唯一，突变不等价）")
            failures.append((name, "落点非唯一"))
            continue
        path.write_text(src.replace(old, new, 1))
        _clear_pyc(path)
        try:
            if path.suffix == ".py" and ast.parse(path.read_text()) is None:
                print(f"[{i}/{len(MUTATIONS)}] {name}\n    ✗ 突变后 ast.parse 失败")
                failures.append((name, "突变后不可解析"))
                continue
            per = [(n, _pytest(["-k", n])) for n in should_red]
            missing = [n for n, r in per if r == 5]
            green = [n for n, r in per if r == 0]
            print(f"[{i}/{len(MUTATIONS)}] {name}")
            if not missing and not green:
                print(f"    ✓ 指名的 {len(per)} 条全红")
            else:
                if missing:
                    print(f"    ✗ 测试名选不到（rc=5，重命名/typo）: {missing}")
                    failures.append((name, "测试名选不到"))
                if green:
                    print(f"    ✗ 突变后仍绿 = 零区分力: {green}")
                    failures.append((name, "突变后仍绿"))
        finally:
            path.write_text(src)
            _clear_pyc(path)

    print("\n" + "═" * 70)
    rc_r = _pytest([])
    print(f"步骤 N：还原后基线复验 exit={rc_r}")
    if rc_r != 0:
        print("✗ 还原后基线不绿 —— harness 污染了工作树")
        return 1
    if failures:
        print(f"\n✗ {len(failures)} 条未达标：")
        for n, why in failures:
            print(f"  · [{why}] {n}")
        return 1
    print(f"\n✓ 全部 {len(MUTATIONS)} 条突变都被锁住，且基线前后皆绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
