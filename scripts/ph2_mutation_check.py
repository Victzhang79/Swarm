#!/usr/bin/env python3
"""P-H2（reconcile_template_exam 多栈驱动化）突变 harness（判据与前批同源）：

  · **先验基线全绿**；· **逐条**跑 should_red；· `rc=5` **判失败**；
  · 落点唯一性检查；· 突变后源码必须仍能 `ast.parse`（try/except SyntaxError）；
  · harness 改磁盘源码——**绝不进带超时的循环、绝不与全量并发、跑完看 git status**；
  · 突变写入后与还原后都清被突变模块的 pyc（CPython 整秒粒度 mtime 陈旧坑）。

★锁的命题★ P-H2：考卷同源对账从 Maven 独有到六栈驱动表分派（G-H11 收口）。
突变压：模板识别多栈化/驱动表接线/npm 抽取 fail-honest/python 断言边界符/
go 内部 module 同考/cargo path 依赖同考/gradle raw 全坐标与 project() 边界/
G-H11 告警面收窄/表外落点机读可辨。
R2 增（双透镜整改）：G-H11 粒度=落点 basename（j）/python·npm·go「认不得 vs 真没有」
二分（k·l·m）/gradle 断言收尾边界（n）/G-H11 机制整删（o）/异常臂 degrade 键（p）。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_template_exam_multistack_ph2.py",
         "test/test_r65d_t2_plan_exam_coherence.py",
         "test/test_r65e2_t1_verify_neg_grep_reconcile.py"]

CU = ROOT / "brain" / "contract_utils.py"

MUTATIONS = [
    (
        "P-H2-a：npm 臂从驱动表除名（机制全对但接线断——npm 考卷回到永不同源，"
        "硬检查①接线覆盖）",
        CU,
        '    "package.json": _ExamStackDriver("npm", _exam_deps_npm, _exam_assert_npm,\n'
        '                                     _RULE5_SUFFIX_GENERIC),\n',
        "",
        ["test_npm_exam_reconciled"],
    ),
    (
        "P-H2-b：模板识别退回 pom-only（多栈围栏识别断=五栈模板恒不被认出，"
        "G-H11 整批复活）",
        CU,
        'r"【权威 [^】]*?模板（[^】]*?原样写入 ([^\\s;；)）】]+)[^】]*】\\n```[a-zA-Z]*\\n'
        '(.*?)\\n?```"',
        'r"【权威 pom 模板（[^】]*?原样写入 ([^\\s;；)）】]+)[^】]*】\\n```xml\\n'
        '(.*?)\\n?```"',
        ["test_extract_templates_all_fences"],
    ),
    (
        "P-H2-c：抽取失败 fail-open（None 被 or [] 吃掉=用空清单把考卷重生成零断言，"
        "比不对账更坏的假同源）",
        CU,
        "                deps = _drv.extract(tpl)\n"
        "                if deps is None:",
        "                deps = _drv.extract(tpl) or []  # 突变：fail-open\n"
        "                if deps is None:",
        ["test_malformed_npm_template_skipped_fail_honest"],
    ),
    (
        "P-H2-d：python 断言边界符删除（`\"pydantic\"` 被 `\"pydantic-core\"` 假过——"
        "探针窄于真断言族）",
        CU,
        "    return f\"grep -qE '\\\"{_re.escape(dep)}[\\\"\\\\[<>=!~ ;]' {path}\"",
        "    return f\"grep -q '\\\"{dep}' {path}\"  # 突变：边界符删除",
        ["test_python_exam_reconciled_and_assert_boundary"],
    ),
    (
        "P-H2-e：go 内部 module 从考卷排除（`require x v0.0.0` 不考=模板即真值"
        "被砍一半，内部依赖缺失零信号）",
        CU,
        "        if m and m.group(1) not in out:\n"
        "            out.append(m.group(1))\n"
        "    return out\n\n\ndef _exam_deps_python",
        '        if m and " v0.0.0" in s:  # 突变：内部 module 排除\n'
        "            continue\n"
        "        if m and m.group(1) not in out:\n"
        "            out.append(m.group(1))\n"
        "    return out\n\n\ndef _exam_deps_python",
        ["test_go_exam_reconciled"],
    ),
    (
        "P-H2-f：cargo path 内部依赖从考卷排除（表形态 `{ path = ... }` 不考=内部"
        "crate 缺失零信号）",
        CU,
        '            m = _re.match(r"([A-Za-z0-9_\\-]+)\\s*=", s)\n'
        "            if m is None:\n"
        "                return None\n"
        "            if m.group(1) not in out:",
        '            m = _re.match(r"([A-Za-z0-9_\\-]+)\\s*=", s)\n'
        "            if m is None:\n"
        "                return None\n"
        '            if m and "path" in s:  # 突变：path 依赖排除\n'
        "                continue\n"
        "            if m.group(1) not in out:",
        ["test_cargo_exam_reconciled"],
    ),
    (
        "P-H2-g：gradle raw 版本尾巴丢弃（classifier 超集坐标只断 g:a=raw 考卷失真，"
        "P-H4c raw 边界复活）",
        CU,
        '    return [f"{g}:{a}:{v}" if v else f"{g}:{a}" for a, (g, v) in specs.items()]',
        '    return [f"{g}:{a}" for a, (g, v) in specs.items()]  # 突变：版本尾巴丢弃',
        ["test_extractors_match_real_renderers"],
    ),
    (
        "P-H2-h：G-H11 告警面不收窄（支持集回退 {pom.xml}=npm/go/python/cargo/gradle "
        "照旧刷「不支持」假警报——告警面必须如实收窄）",
        CU,
        "            _unsup = sorted(_tpl_mfs - set(_EXAM_DRIVERS))",
        '            _unsup = sorted(_tpl_mfs - {"pom.xml"})  # 突变：告警面不收窄',
        ["test_gh11_warning_only_for_driverless_stacks"],
    ),
    (
        "P-H2-i：表外落点静默跳过（WARNING 降 DEBUG=「认不得」与「真没有」塌回一个值，"
        "硬检查④）",
        CU,
        '                    logger.warning(\n'
        '                        "[R65D-T2] %s 权威模板落点 %s 无考卷同源 driver'
        '（_EXAM_DRIVERS 未收录）"\n'
        '                        " → 该模板跳过（G-H11 机读可辨）", st.id, pom)',
        '                    logger.debug(\n'
        '                        "[R65D-T2] %s 权威模板落点 %s 无考卷同源 driver'
        '（_EXAM_DRIVERS 未收录）"\n'
        '                        " → 该模板跳过（G-H11 机读可辨）", st.id, pom)',
        ["test_driverless_manifest_warns_machine_readable"],
    ),
    # ── R2 双透镜整改批（hunter F1-F7 + reviewer F1-F3）──
    (
        "P-H2-j：G-H11 粒度回退栈粒度（requirements.txt 借 python 栈之名压掉告警="
        "degrade 键不记，双透镜 F1）",
        CU,
        "            _unsup = sorted(_tpl_mfs - set(_EXAM_DRIVERS))",
        '            _unsup = sorted(_tpl_mfs - set(_EXAM_DRIVERS)'
        ' - {"requirements.txt"})  # 突变：requirements.txt 豁免',
        ["test_gh11_granularity_is_manifest_not_stack"],
    ),
    (
        "P-H2-k：python 二分删除（有键但形状认不得塌成真没有=空清单抹掉旧考卷，"
        "双透镜 F2）",
        CU,
        '    if not m:\n'
        '        if _re.search(r"(?m)^dependencies\\s*=", tpl or ""):\n'
        '            return None   # 有键但形状认不得（单行/非数组）≠ 真没有\n'
        '        return []',
        '    if not m:\n'
        '        return []  # 突变：二分删除',
        ["test_python_malformed_dependencies_fail_honest"],
    ),
    (
        "P-H2-l：npm 结构违例塌成真没有（dependencies 非 dict → []=空清单抹掉旧考卷，"
        "双透镜 F2/F3）",
        CU,
        "    if not isinstance(deps, dict):\n"
        "        return None\n"
        "    return [str(k) for k in deps]",
        "    if not isinstance(deps, dict):\n"
        "        return []  # 突变：结构违例塌成真没有\n"
        "    return [str(k) for k in deps]",
        ["test_npm_non_dict_dependencies_fail_honest"],
    ),
    (
        "P-H2-m：go 裸 require 静默丢弃（无版本 require 行=形状认不得，静默丢弃="
        "依赖从考卷消失零信号，hunter F4）",
        CU,
        '            m = _re.match(r"require\\s+(\\S+)\\s+\\S+", s)\n'
        "            if m is None:\n"
        "                return None   # 裸 `require x`（无版本）=形状认不得",
        '            m = _re.match(r"require\\s+(\\S+)\\s+\\S+", s)\n'
        "            if m is None:\n"
        "                continue   # 突变：裸 require 静默丢弃",
        ["test_go_bare_require_and_block_garbage_fail_honest"],
    ),
    (
        "P-H2-n：gradle 断言收尾边界删除（`g:a:1.0` 被 `g:a:1.0-rc1` 假过="
        "裸子串族，hunter F6）",
        CU,
        '    return f"grep -qE \'{dep}[^A-Za-z0-9_.:-]\' {path}"',
        '    return f"grep -q \'{dep}\' {path}"  # 突变：收尾边界删除',
        ["test_gradle_assert_coordinate_boundary",
         "test_gradle_exam_reconciled_both_dialects"],
    ),
    (
        "P-H2-o：G-H11 机制整删（`if _unsup:` 恒假=告警+degrade 双通道全灭，"
        "硬检查①：机制删掉必须有测试红）",
        CU,
        "            if _unsup:",
        "            if False:  # 突变：G-H11 机制整删",
        ["test_gh11_warning_only_for_driverless_stacks",
         "test_gh11_granularity_is_manifest_not_stack",
         "test_gh11_prose_mention_does_not_mask_unsupported"],
    ),
    (
        "P-H2-p：对账异常臂 degrade 键删除（只剩 WARNING=降级不可辨，hunter F7）",
        CU,
        "        except Exception:  # noqa: BLE001 — 单子任务畸形绝不拖垮全计划的对账\n"
        '            _record_degrade_safe("brain.template_exam.reconcile_failed")',
        "        except Exception:  # noqa: BLE001 — 单子任务畸形绝不拖垮全计划的对账\n"
        "            pass  # 突变：degrade 键删除",
        ["test_reconcile_subtask_exception_records_degrade"],
    ),
]


def _pytest(args: list[str]) -> int:
    p = subprocess.run([PY, "-m", "pytest", *TESTS, "-p", "no:warnings", "-q",
                        "--tb=no", *args], cwd=ROOT, capture_output=True, text=True)
    return p.returncode


def _clear_pyc(path: Path) -> None:
    """删被突变模块的 pyc——CPython 的 pyc 失效判据是【整秒】粒度 mtime：相邻两条突变
    落在同一秒时，第二条突变写完 pyc 仍被判有效 ⇒ 子进程跑的是【上一条】的代码。
    每条突变前与还原后都必须清。"""
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
            try:
                ast.parse(path.read_text())
            except SyntaxError:
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
