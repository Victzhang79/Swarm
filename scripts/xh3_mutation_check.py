#!/usr/bin/env python3
"""X-H3（npm/.sln ManifestDriver 三面同源）突变 harness（判据与 b7/xm9 harness 同源）：

  · 先验基线全绿；· 逐条跑 should_red；· rc=5 判失败；· 落点唯一性；
  · 突变后 ast.parse；· 绝不进超时循环、绝不与全量并发；· 突变/还原后清 pyc。

★锁的命题★：npm probes 面 · npm prune 面 · _reconcile_npm 接线 · glob 绝不进显式
（灾变向：glob 被当幽灵摘掉）· .sln probes 面 · .sln GUID 配置行清理 ·
prune_stale 候选接线（package.json）· 容器绝不臆测（血规 2）。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_xh3_manifest_three_faces.py",
         "test/test_b0_workspace_fixture_matrix.py",
         "test/test_b3_stack_spec_single_source.py",
         "test/test_workspace_manifest_reconcile.py",
         "test/test_r46_manifest_prune.py"]

WM = ROOT / "worker" / "workspace_manifest.py"

MUTATIONS = [
    (
        "XH3-a：npm probes 面删除（显式列表成员回到不可探→prune/add 对账失明）",
        WM,
        '    if name == "package.json" and "/" not in rel_path:\n'
        '        # X-H3：仅根 package.json 的 workspaces 有聚合语义',
        '    if False and name == "package.json":  # 突变：npm probes 面删除\n'
        '        # X-H3：仅根 package.json 的 workspaces 有聚合语义',
        ["test_npm_probes_explicit_only_glob_and_negation_skipped"],
    ),
    (
        "XH3-b：npm prune 面删除（幽灵子包条目永不摘→npm ci 装不到的镜像毒）",
        WM,
        '        elif name == "package.json" and "/" not in rel_path:',
        '        elif False:  # 突变：npm prune 面删除',
        ["test_npm_prune_removes_ghost_keeps_globs"],
    ),
    (
        "XH3-c：_reconcile_npm 未接进 dispatch（add 面机制存在但接线缺席="
        "血规 10① 复发）",
        WM,
        "    _reconcile_cargo, _reconcile_dotnet_sln, _reconcile_go_work, _reconcile_npm,",
        "    _reconcile_cargo, _reconcile_dotnet_sln, _reconcile_go_work,"
        "  # 突变：npm reconcile 摘除",
        ["test_npm_add_infers_container_from_explicit_entries",
         "test_three_faces_cover_same_manifests"],
    ),
    (
        "XH3-d：glob 当显式条目（灾变向——glob 探针路径含 * 恒不存在→"
        "prune 把 packages/* 当幽灵摘掉=聚合清单被毁）",
        WM,
        '        if "*" in e or e.strip().startswith("!"):',
        '        if e.strip().startswith("!"):  # 突变：glob 混入显式',
        ["test_npm_probes_explicit_only_glob_and_negation_skipped"],
    ),
    (
        "XH3-e：.sln GUID 配置行清理删除（只摘 Project 块→"
        "ProjectConfigurationPlatforms 留悬挂 GUID 引用）",
        WM,
        '                new_text = re.sub(\n'
        '                    r"(?m)^[ \\t]*\\{" + re.escape(guid) + r"\\}\\.[^\\n]*\\r?\\n?",\n'
        '                    "", new_text)',
        '                pass  # 突变：GUID 配置行清理删除',
        ["test_sln_prune_removes_block_and_guid_config_lines"],
    ),
    (
        "XH3-f：package.json 摘出 prune_stale 候选（本地树 prune 入口对 npm 失明）",
        WM,
        '        for n in ("settings.gradle", "settings.gradle.kts", "Cargo.toml", "go.work",\n'
        '                  "package.json"):',
        '        for n in ("settings.gradle", "settings.gradle.kts", "Cargo.toml", "go.work"):'
        '  # 突变：package.json 候选摘除',
        ["test_prune_stale_picks_up_package_json_and_sln"],
    ),
    (
        "XH3-g：容器臆测（凭印象写死 packages/apps=血规 2 复发；无容器证据的"
        "新包被静默当成员）",
        WM,
        '    containers = {e.rsplit("/", 1)[0] for e in explicit if "/" in e}',
        '    containers = {"packages", "apps"}  # 突变：臆测约定容器',
        ["test_npm_add_never_guesses_conventional_container_names"],
    ),
    (
        "XH3-h：.sln probes 面删除（幽灵工程条目不可探→msbuild 硬错无人摘）",
        WM,
        '    if name.lower().endswith(".sln"):',
        '    if False and name.lower().endswith(".sln"):  # 突变：sln probes 面删除',
        ["test_sln_probes_project_files_only_not_solution_folders"],
    ),
    # ── R2 双复核整改锁（XH3-i..n）─────────────────────────────────────────
    (
        "XH3-i：路径逃逸拒收删除（../sibling、/abs 条目三面放行→"
        "add 面把根目录外路径写进 workspaces）",
        WM,
        '    if e.startswith("/") or ".." in e.split("/"):',
        '    if False:  # 突变：路径逃逸放行',
        ["test_npm_entries_path_escape_rejected_three_faces"],
    ),
    (
        "XH3-j：.sln 工程后缀大小写归一删除（Windows 生态 .CSPROJ 大写后缀"
        "跌出 probes→幽灵工程不可探）",
        WM,
        '            if Path(proj_path).suffix.lower() not in _SLN_TYPE_GUID:',
        '            if Path(proj_path).suffix not in _SLN_TYPE_GUID:'
        '  # 突变：后缀大小写不归一',
        ["test_sln_uppercase_suffix_probed"],
    ),
    (
        "XH3-k：members_only 降档摘除（npm 聚合档 demote 误判安全→"
        "scripts/dependencies 编辑无兜底 WARNING 熄声=复用单一事实源"
        "≠复用消费契约 复发）",
        ROOT / "stacks" / "spec.py",
        '        safe = (spec.has_aggregate_reconcile\n'
        '                and not spec.aggregate_reconcile_members_only)',
        '        safe = spec.has_aggregate_reconcile  # 突变：members_only 降档摘除',
        ["test_npm_members_only_tier_still_warns_on_demote"],
    ),
    (
        "XH3-l：strip npm 臂删除（H2 回滚对根 package.json 失明→"
        "FAIL 子任务新增成员残留=旧实现复发且只剩 WARNING）",
        WM,
        '        if _name == "package.json" and "/" not in rel_path:',
        '        if False and _name == "package.json":  # 突变：strip npm 臂删除',
        ["test_strip_worker_contribs_npm_and_sln_arms"],
    ),
    (
        "XH3-m：JSON 缩进探测摘除（4 空格原文件 round-trip 后被重排成 2 空格"
        "=无关 diff 噪声）",
        WM,
        '            json.dumps(obj, indent=_detect_json_indent(text), ensure_ascii=False) + "\\n",',
        '            json.dumps(obj, indent=2, ensure_ascii=False) + "\\n",'
        '  # 突变：缩进恒 2',
        ["test_npm_json_indent_preserved_on_round_trip"],
    ),
    (
        "XH3-n：根容器 fail-closed 放开（顶层条目 ⇒ 根级新包全被当成员登记"
        "=误杀面大的臆测方向）",
        WM,
        '    containers = {e.rsplit("/", 1)[0] for e in explicit if "/" in e}',
        '    containers = {e.rsplit("/", 1)[0] if "/" in e else "" for e in explicit}'
        '  # 突变：根容器放开',
        ["test_npm_add_root_container_fail_closed"],
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
