#!/usr/bin/env python3
"""X-M9（H1 权威模板落盘多栈化）突变 harness（判据与 pm_pl harness 同源）：

  · 先验基线全绿；· 逐条跑 should_red；· rc=5 判失败；· 落点唯一性；
  · 突变后 ast.parse；· 绝不进超时循环、绝不与全量并发；· 突变/还原后清 pyc。

★锁的命题★：非 pom 权威模板确定性 write-through（npm/go/python 臂）；已知清单集
STACK_SPEC 派生（认不得 fail-closed）；package.json 形状校验（截取错位不覆写）；
CREATE-only 守约（MODIFY 覆写=clobber 铁律）。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_xm9_h1_template_multistack.py"]

GATE = ROOT / "worker" / "executor_l1gate.py"

MUTATIONS = [
    (
        "XM9-a：标记识别回退 pom-only（五栈模板回到零落盘，主治机制删掉必须红）",
        GATE,
        '        for m in re.finditer(\n'
        '                r"【权威 [^】]*?模板（[^】]*?原样写入 ([^\\s;；)）】]+)", desc):',
        '        for m in re.finditer(\n'
        '                r"【权威 pom 模板（[^】]*?原样写入 ([^\\s;；)）】]+)", desc):'
        '  # 突变：回退 pom-only',
        ["test_npm_template_written_through",
         "test_go_mod_template_bare_fence",
         "test_pyproject_toml_fence"],
    ),
    (
        "XM9-b：已知清单闸整删（认不得的 Makefile 也被确定性覆写=fail-open，"
        "栈中立边界失守）",
        GATE,
        "            if base not in _H1_KNOWN_MANIFEST_BASENAMES:",
        "            if False:  # 突变：已知清单闸整删",
        ["test_unknown_manifest_fail_closed"],
    ),
    (
        "XM9-c：package.json 形状校验删除（非法 JSON / 非 dict 都覆写="
        "fail-closed 校验失守）",
        GATE,
        '    if base == "package.json":\n'
        "        try:\n"
        "            return isinstance(json.loads(tpl), dict)  # 渲染器恒产 dict（`{\"name\":…}`）\n"
        "        except ValueError:\n"
        "            return False",
        '    if base == "package.json":\n'
        "        return True  # 突变：JSON 形状校验删除",
        ["test_malformed_package_json_not_written",
         "test_package_json_array_rejected"],
    ),
    (
        "XM9-d：CREATE-only 守约删除（writable 非空也覆写=MODIFY clobber，"
        "R41-F5 铁律失守）",
        GATE,
        "            if _writable:",
        "            if False:  # 突变：writable 守约删除",
        ["test_modify_form_not_written"],
    ),
    (
        "XM9-e：落点回退 basename 首命中（同 basename 双清单互相覆写，"
        "hunter R1 CRITICAL 复活）",
        GATE,
        "            rel = marker_rel",
        '            rel = next(f for f in creates if f.rsplit("/", 1)[-1] == base)'
        "  # 突变：回退 basename 落点",
        ["test_same_basename_two_manifests_each_written"],
    ),
    (
        "XM9-f：候选匹配回退 basename（backend 的模板写进 frontend，"
        "reviewer R1 MEDIUM-2 复活）",
        GATE,
        "            if marker_rel not in creates:",
        '            if not any(f.rsplit("/", 1)[-1] == base for f in creates):'
        "  # 突变：回退 basename 匹配",
        ["test_marker_path_mismatch_not_written"],
    ),
    (
        "XM9-g：pyproject 形状校验删除（任意非空文本都覆写，"
        "reviewer R1 MEDIUM-3 复活）",
        GATE,
        '    if base == "pyproject.toml":\n'
        '        return tpl.startswith("[project]")            # _render_pyproject_toml 首行',
        '    if base == "pyproject.toml":\n'
        "        return True  # 突变：pyproject 形状校验删除",
        ["test_non_toml_pyproject_rejected"],
    ),
]


def _pytest(args: list[str]) -> int:
    p = subprocess.run([PY, "-m", "pytest", *TESTS, "-p", "no:warnings", "-q",
                        "--tb=no", *args], cwd=ROOT, capture_output=True, text=True)
    return p.returncode


def _clear_pyc(path: Path) -> None:
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
