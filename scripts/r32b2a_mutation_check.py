#!/usr/bin/env python3
"""★32 号文批2a 突变锁★ M3（并集=owner 版记假账）/ M4（C-quoted 路径零反转义）/
M5（清单判据第二份手写枚举）+ 批2a-R1（双复核整改 F1 探针同源/F2 引擎外漏斗/
F3 JSON 并集畸形闸）全部锁的区分力验证。纪律同 r32b1_mutation_check。
"""
from __future__ import annotations

import ast
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "bin" / "python"

MERGE = ROOT / "brain" / "merge_engine.py"
DIFF_APPLY = ROOT / "project" / "diff_apply.py"
NODES = ROOT / "brain" / "nodes" / "__init__.py"

TESTS = [
    "test/test_merge_m345_32.py",
    "test/test_a_batch_merge_owner_ledger.py",
    "test/test_merge_manifest_union_round9.py",
]

MUTATIONS = [
    (
        "MUT-M3 并集=owner 版退回 return None——子集写者被记 owner_drops 假账",
        MERGE,
        "        if merged is None:\n"
        "            return None\n",
        "        if merged is None or merged == \"\\n\".join(bodies[owner_sid]):\n"
        "            return None\n",
        ["test_union_new_manifest_subset_non_owner_returns_owner_lines_not_none",
         "test_union_subset_non_owner_records_unions_not_drops"],
    ),
    (
        "MUT-M4 C-quoted 路径不反转义——中文文件名毁形/前缀剥离失效复活"
        "（批2a-R1：原语已下放 project.diff_apply，落点随之迁）",
        DIFF_APPLY,
        '    if len(p) >= 2 and p.startswith(\'"\') and p.endswith(\'"\'):',
        "    if False:  # 突变：C-quoted 原样透传",
        ["test_strip_diff_path_unquotes_c_quoted_utf8",
         "test_parse_git_header_paths_c_quoted_with_space",
         "test_diff_target_files_unquotes_c_quoted",
         "test_real_git_produces_c_quoted_paths_and_funnel_handles_both",
         "test_ffud_c_quoted_header_collected_clean",
         "test_ffud_quoted_rename_unquoted_key"],
    ),
    (
        "MUT-M5a 聚合清单判据脱离 STACK_SPEC（回手写枚举，漏 package.json）",
        MERGE,
        "    return any(base == n.lower() for n in root_aggregate_manifests()) \\\n"
        "        or base.endswith(\".sln\")",
        "    return base in (\"pom.xml\", \"settings.gradle\", \"settings.gradle.kts\",\n"
        "                    \"cargo.toml\", \"go.work\") or base.endswith(\".sln\")",
        ["test_every_stack_aggregate_manifest_recognized"],
    ),
    (
        "MUT-M5b 模块清单判据脱离 STACK_SPEC（回手写枚举，漏 package.json/pyproject.toml）",
        MERGE,
        "    return any(base == n.lower() for n in module_manifest_names()) \\\n"
        "        or base.endswith((\".csproj\", \".fsproj\", \".vbproj\"))",
        "    return base in (\"pom.xml\", \"build.gradle\", \"build.gradle.kts\",\n"
        "                    \"cargo.toml\", \"go.mod\") \\\n"
        "        or base.endswith((\".csproj\", \".fsproj\", \".vbproj\"))",
        ["test_every_stack_module_manifest_recognized"],
    ),
    (
        "MUT-F1a 豁免探针回旧手写枚举（npm/python 既有模块冤判孤儿，hunter HIGH）",
        MERGE,
        "    for mf in module_manifest_names():\n"
        "        if mf in names and (md / mf).is_file():\n"
        "            return True",
        "    for mf in (\"pom.xml\", \"build.gradle\", \"build.gradle.kts\",\n"
        "               \"Cargo.toml\", \"go.mod\"):  # 突变：回旧手写枚举\n"
        "        if mf in names and (md / mf).is_file():\n"
        "            return True",
        ["test_base_has_module_skeleton_derived_set",
         "test_orphan_filter_keeps_existing_npm_module"],
    ),
    (
        "MUT-F1c 豁免探针吞 EACCES 成 False（py≥3.13 is_file/glob 语义回退，"
        "M-5 分路死代码+跨版本极性翻转，hunter MED）",
        MERGE,
        "    try:\n"
        "        names = set(os.listdir(md))\n"
        "    except (FileNotFoundError, NotADirectoryError):\n"
        "        return False",
        "    try:\n"
        "        names = set(os.listdir(md))\n"
        "    except OSError:  # 突变：PermissionError 也吞成 False\n"
        "        return False",
        ["test_base_has_module_skeleton_eacces_raises_not_false"],
    ),
    (
        "MUT-F1b 多模块磁盘臂删 npm workspaces 根探测（计划/磁盘两臂分叉）",
        NODES,
        '.get("workspaces"):\n'
        '                return True   # npm workspaces（成员显式列表）',
        '.get("workspaces"):  # 突变：永不命中\n'
        '                pass',
        ["test_detect_multimodule_npm_workspaces_root"],
    ),
    (
        "MUT-F2a files_from_unified_diff 门控回 6 字符硬前缀（quoted 头行整行蒸发，"
        "D3 闸/L1 scope 对 quoted 文件隐形）",
        DIFF_APPLY,
        "            if payload.startswith((\"b/\", '\"b/')):",
        "            if payload.startswith(\"b/\"):  # 突变：quoted 形态不再匹配",
        ["test_ffud_c_quoted_header_collected_clean"],
    ),
    (
        "MUT-F2b rename 行不反转义（quoted rename 键毁形，与正常键永不相等）",
        DIFF_APPLY,
        '_add(unquote_git_path(line[len("rename from "):].strip()))',
        '_add(line[len("rename from "):])  # 突变：rename 键毁形',
        ["test_ffud_quoted_rename_unquoted_key"],
    ),
    (
        "MUT-F3a 新文件专路删 JSON 并集校验闸（缺逗号畸形记「并集成功」交付）",
        MERGE,
        "        if not _json_union_result_valid(file_path, _plain):",
        "        if False:  # 突变：畸形 JSON 照样当并集成功",
        ["test_union_new_manifest_json_invalid_falls_back"],
    ),
    (
        "MUT-F3b modify 路径删 JSON 并集校验闸（无效 JSON 以「干净消解」交付）",
        MERGE,
        "    if is_agg and combined is not None and not _json_union_result_valid(",
        "    if False and is_agg and combined is not None and not _json_union_result_valid(",
        ["test_merge_diffs_json_bad_union_visible_not_silent"],
    ),
    (
        "MUT-F3c noop 判定也过 JSON 校验（=判序倒回 F3 在前，noop+无效角点 "
        "owner_drops 假账复活，hunter LOW-4）",
        MERGE,
        '        if merged == "\\n".join(bodies[owner_sid]):',
        '        if merged == "\\n".join(bodies[owner_sid]) and '  # 突变：noop 也过 F3 闸
        '_json_union_result_valid(file_path, "\\n".join('
        'ln[1:] if ln.startswith("+") else ln for ln in merged.split("\\n"))):',
        ["test_union_new_manifest_noop_invalid_json_still_unions_not_drops"],
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
    md5_before = {p: hashlib.md5(p.read_bytes()).hexdigest()
                  for p in (MERGE, DIFF_APPLY, NODES)}
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
        try:
            ast.parse(mutated)
        except SyntaxError as _e:
            print(f"[{i}/{len(MUTATIONS)}] {name}\n"
                  f"    ✗ 突变后源码无法编译（{_e.msg} @line {_e.lineno}）⇒ rc≠0 是假信号")
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
    md5_after = {p: hashlib.md5(p.read_bytes()).hexdigest() for p in md5_before}
    if md5_before != md5_after:
        print("✗ 文件 md5 与起跑时不一致 —— 还原不完整")
        return 1
    if failures:
        print(f"\n✗ {len(failures)} 条未达标：")
        for n, why in failures:
            print(f"  · [{why}] {n}")
        return 1
    print(f"\n✓ 全部 {len(MUTATIONS)} 条突变都被锁住，基线前后皆绿，md5 还原一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
