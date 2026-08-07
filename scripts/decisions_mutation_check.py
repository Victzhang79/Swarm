#!/usr/bin/env python3
"""用户拍板的决定 2/4 突变 harness（判据与前三批同源，那些自伤一开始就带上）：

  · **先验基线全绿**；· **逐条**跑 should_red，每条都必须红；
  · `rc=5`（`-k` 选不到，如测试被重命名）**判失败**；· 落点唯一性检查。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_decisions_l2_share_and_skipdirs.py",
         "test/test_sandbox_spec.py", "test/test_b0_workspace_fixture_matrix.py",
         "test/test_l2_compile_failloud_round21.py",
         "test/test_b4a_build_surface_tristate.py"]

IR = ROOT / "brain" / "integration_review.py"
SPEC = ROOT / "stacks" / "spec.py"
SBX = ROOT / "project" / "sandbox_spec.py"

MUTATIONS = [
    (
        '决定 2：L2 不再读共享表（退回自己写 if 链 ⇒ 与 L1 必然漂移）',
        IR,
        '        if spec.whole_project_build_cmd:',
        '        if False:',
        ['test_d2_l2_command_is_byte_equivalent_after_sharing'],
    ),
    (
        '决定 2：表里 maven 的命令被改（L2 必须跟着变，否则不是真共享）',
        SPEC,
        '        whole_project_build_cmd="mvn -q -DskipTests compile",',
        '        whole_project_build_cmd="mvn -q WRONG compile",',
        ['test_d2_l2_command_is_byte_equivalent_after_sharing'],
    ),
    (
        '决定 2：表缺项被静默吞掉（L2 当成『没有构建』跳过编译）',
        IR,
        '            "[integration_review] STACK_SPEC[\'%s\'] 匹配到根清单但 whole_project_build_cmd 为空"',
        '            "",',
        ['test_d2_table_gap_is_observable'],
    ),
    (
        # ★落点于 2026-08-01（P-C1 批复跑）修正★ 原打 `project/sandbox_spec.py` 的 `_SKIP_DIRS`
        # 字面。N-2b/N-3 批（c2941b2）把依赖树那半提到 `stacks.DEPENDENCY_TREE_DIRS` 单独命名
        # （消费契约分档），该字面随之搬家 ⇒ 本条落点失效、报"落点未命中"。
        # ★这正是"改共享代码必复跑兄弟 harness"要抓的东西——而上批收尾我报"六 harness 全锁"
        # 时没核到这一条（自伤守卫尽责了，是我漏读）。落点跟着单一事实源走。
        '决定 4：依赖树目录不再进 _SKIP_DIRS（node_modules/vendor 里的清单又算构建入口）',
        SPEC,
        '    "node_modules", "vendor", "third_party", "third-party", "3rdparty",',
        '',
        ['test_d4_dependency_tree_manifests_are_not_build_entrypoints'],
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
        mutated = src.replace(old, new, 1)
        # ★复核 H-5 的治法★ 突变后的源码必须**仍能编译**。否则 pytest 拿到的是 collection
        # error，rc≠0 被本 harness 当成"该红的红了"—— 那什么也没证明（实测本批 6/22 条落点因
        # 丢了缩进而产生 IndentationError，整个 X-H1/N-2/N-4 组的"全锁"结论都是虚的）。
        # 判据放在这里而不是靠人肉检查：它是"突变落点与被测命题不等价"这类假绿的通用闸。
        try:
            ast.parse(mutated)
        except SyntaxError as _e:
            print(f"[{i}/{len(MUTATIONS)}] {name}\n"
                  f"    ✗ 突变后源码无法编译（{_e.msg} @line {_e.lineno}）⇒ pytest 只会报 "
                  f"collection error，rc≠0 是假信号。落点多半丢了缩进/截断了多行语句。")
            failures.append((name, "突变产生语法错"))
            continue
        path.write_text(mutated)
        _clear_pyc(path)
        try:
            per = [(n, _pytest(["-k", n])) for n in should_red]
            missing = [n for n, r in per if r == 5]
            green = [n for n, r in per if r == 0]
            print(f"[{i}/{len(MUTATIONS)}] {name}")
            if not missing and not green:
                print(f"    ✓ 指名的 {len(per)} 条全红")
            else:
                if missing:
                    print(f"    ✗ 测试名选不到（rc=5）: {missing}")
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
