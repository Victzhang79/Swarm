#!/usr/bin/env python3
"""#29-2 W-1 突变 harness（判据与前批同源）：

  · **先验基线全绿**（只验"突变→红"会让修得不全的整改全绿通过）；
  · **逐条**跑 should_red；`rc=5` **判失败**（测试名选不到＝锁不存在）；
  · 落点唯一性检查 + **落点失效数**单独计数（漂移会静默零覆盖）；
  · 突变后源码必须仍能 `ast.parse`；每条突变前与还原后都清 pyc（整秒 mtime 陈旧假绿）；
  · harness 改磁盘源码——**绝不进带超时的循环、绝不与全量并发、跑完看 git status**。

★锁的命题★
  段①（l1_pipeline）：A2 兄弟坐标注入必须推进沙箱，否则「构建沙箱优先」⇒ 机制生产零效力；
    且推送面**只含 A2 触达路径**（复用 paths 会把本地旧副本推上去擦掉沙箱侧修复=更坏）。
  段②（workspace_manifest）：pull-back 的并集必须覆盖 cargo/npm 依赖条目，否则并行兄弟的
    注入被陈旧副本抹掉（R48c-1 死法换栈复发）；分 section 键、只补缺不改值、点表诚实不并、
    产出必过 TOML 校验、npm 成员/依赖两面独立判。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_w1_a2_sandbox_push_and_merge.py",
         "test/test_r46_manifest_prune.py",
         "test/test_batch4_sandbox.py",
         "test/test_sibling_dep_repair.py"]

L1 = ROOT / "worker" / "l1_pipeline.py"
WM = ROOT / "worker" / "workspace_manifest.py"

MUTATIONS = [
    # ── 段①：推进沙箱的接线 ──────────────────────────────────────────────
    (
        "W-1-a：A2 清单不推进沙箱（复现原缺陷：本地改了、构建读旧副本 ⇒ 机制零效力）",
        L1,
        "        if _a2_paths:\n            try:\n                _pushed = _push_manifests_to_sandbox(project_path, _a2_paths)",
        "        if False:  # 突变：不推进沙箱\n            try:\n                _pushed = _push_manifests_to_sandbox(project_path, _a2_paths)",
        ["test_a2_injection_reaches_sandbox"],
    ),
    (
        "W-1-b：推送面复用 paths（把非 A2 路径的本地旧副本推上去 ⇒ 擦掉沙箱侧修复）",
        L1,
        "                _pushed = _push_manifests_to_sandbox(project_path, _a2_paths)",
        "                _pushed = _push_manifests_to_sandbox(project_path, paths)",
        ["test_push_scope_excludes_non_a2_repair_paths"],
    ),
    # ── 段②：dispatch 接线 ──────────────────────────────────────────────
    (
        "W-1-c：cargo 不路由进依赖并集（复现原缺陷 return incoming_text）",
        WM,
        "        if name == \"cargo.toml\":\n            # 两侧全等 → 无可并（提前返回省一次解析；不影响语义）\n            if local_text == incoming_text:\n                return incoming_text\n            return _merge_cargo_manifest(local_text, incoming_text, rel_path)",
        "        if name == \"cargo.toml\":\n            return incoming_text",
        ["TestCargoDepUnion"],
    ),
    (
        "W-1-d：npm 依赖并集整段删除（只并 workspaces 成员＝原状态，A2 注入照丢）",
        WM,
        "        for _sec in _NPM_DEP_SECTIONS:",
        "        for _sec in ():  # 突变：依赖并集不跑",
        ["TestNpmDepUnion and test_local_only_dep_survives",
         "TestNpmDepUnion and test_dev_dep_stays_in_dev_section",
         "test_every_a2_writable_section_is_merged"],
    ),
    # ── 段②：口径正确性 ─────────────────────────────────────────────────
    (
        "W-1-e：npm 并集面收窄成只 dependencies（窄于 A2 注入面 ⇒ dev/peer/optional 漏档）",
        WM,
        "_NPM_DEP_SECTIONS = (\"dependencies\", \"devDependencies\",\n                     \"peerDependencies\", \"optionalDependencies\")",
        "_NPM_DEP_SECTIONS = (\"dependencies\",)",
        ["test_every_a2_writable_section_is_merged",
         "TestNpmDepUnion and test_dev_dep_stays_in_dev_section"],
    ),
    (
        "W-1-f：npm 两面共用早返（incoming 丢 workspaces 键时依赖并集连带失效）",
        WM,
        "        changed = False\n        # ── ① 成员并集（B7 原有面）──\n        loc_ws = _ws_list(loc)\n        inc_ws = _ws_list(inc)\n        if loc_ws and inc_ws is not None:",
        "        changed = False\n        # ── ① 成员并集（B7 原有面）──\n        loc_ws = _ws_list(loc)\n        inc_ws = _ws_list(inc)\n        if not loc_ws or inc_ws is None:\n            return incoming_text\n        if loc_ws and inc_ws is not None:",
        ["test_members_and_deps_are_independent_faces"],
    ),
    (
        "W-1-g：npm 并集改成覆盖 incoming 版本（只补缺不改值被破 ⇒ 陈旧版本回灌）",
        WM,
        "            _miss = {k: v for k, v in _lsec.items()\n                     if isinstance(k, str) and isinstance(v, str) and k not in _isec}",
        "            _miss = {k: v for k, v in _lsec.items()\n                     if isinstance(k, str) and isinstance(v, str)}",
        ["TestNpmDepUnion and test_incoming_version_wins_on_conflict"],
    ),
    (
        # 首跑教训：原突变往 inc_keys 里多塞一个 ("dependencies", k) 不构成压力——local 侧
        # 的键是 ("dev-dependencies", foo)，两边都不相交，行为不变仍绿。真正等价于"键退化成
        # 只按名字"的突变必须落在**查表处**：把带 section 的精确匹配换成按名字的存在性判断。
        "W-1-h：cargo 并集键退化成只按名字（dev 依赖被判「已存在」跳过 ⇒ 该区注入永远并不回）",
        WM,
        "            if (_sec, _norm) in inc_keys:\n                continue",
        "            if any(_n == _norm for _s, _n in inc_keys):\n                continue",
        ["test_section_key_places_dev_dep_beside_same_name_runtime_dep"],
    ),
    # ★W-1-i 已撤销，理由如实记在这里（不是"漏了"，是**做不到**）★
    # 原本想压 cargo 侧"只补缺不改值"：把去重判据 `if (_sec, _norm) in inc_keys: continue`
    # 突变成 `if False`。实测**仍绿**，且原因是结构性的：TOML 禁止同表重复键，所以"跳过去重"
    # 必然在同一 section 里插出第二个同名键 ⇒ 产出非法 TOML ⇒ 被 tomllib 解析那关拦住 ⇒
    # fail-open 返回 incoming ⇒ 断言（incoming 版本胜出）照样成立。
    # 也就是说该属性由**两条独立机制**共同保证，且去重失效的后果是 fail-open（诚实不并）
    # 而非"静默改值"——不存在能让它产出错结果的单点突变。硬造一个"覆盖写"突变需要新增
    # 本设计里根本没有的覆盖路径，那是压一段不存在的代码。
    # 故：`TestCargoDepUnion::test_incoming_version_wins_on_conflict` 保留（它锁的是真实
    # 用户可见属性），但**不宣称**它有突变背书。npm 侧同属性有真突变（W-1-g）——那边是
    # dict.update，跳过去重会真的覆盖值，后果不同，故那条压得住。

    (
        "W-1-j：cargo 点表不再诚实跳过（削成 version-only 伪单行 ⇒ 静默丢 features）",
        WM,
        "            if not _raw:\n                # 点表/无可移植版本形态：无单行原始声明可移植 → 诚实丢弃\n                skipped_dot += 1\n                continue",
        "            if not _raw:\n                _raw = f\"{_name} = \\\"{_ver}\\\"\"",
        ["test_dot_table_dep_is_honestly_skipped_not_mangled"],
    ),
    (
        "W-1-k：cargo section 头正则不容忍内空白（`[ dependencies ]` ⇒ 追加重复 section）",
        WM,
        "            m = re.search(rf'^\\s*\\[\\s*{re.escape(_sec)}\\s*\\]\\s*$', merged, re.M)",
        "            m = re.search(rf'^\\s*\\[{re.escape(_sec)}\\]\\s*$', merged, re.M)",
        ["test_section_header_with_inner_whitespace"],
    ),
    (
        # 首跑教训：原本这里是"删掉 tomllib.loads 校验"，但当时那道闸**不可独立证伪**——
        # 唯一能产非法 TOML 的输入需要同时施加 W-1-k 的突变（重复 section），单点突变压不到。
        # 自查又发现真缺陷：假锚点（`[dependencies]` 出现在多行字符串值里）会让插入落进字符串
        # ⇒ 值被污染 + 依赖没并进去，而结果**仍是合法 TOML** ⇒ 只验语法的闸恒放行。
        # 于是校验升级成【端状态对账】，本条突变压它：对账删掉 ⇒ 伪并结果直接产出。
        "W-1-l：cargo 端状态对账删除（假锚点伪并放行：description 被污染 + 依赖没并进去）",
        WM,
        "        if _stripped != _inc_obj:",
        "        if False:  # 突变：端状态对账删除",
        ["test_fake_anchor_in_multiline_string_does_not_corrupt_value",
         "test_fake_anchor_fails_open_instead_of_claiming_merged"],
    ),
    (
        "W-1-l2：写者侧后置校验退回只验语法（_inject_cargo 污染用户 description + 伪成功）",
        ROOT / "worker" / "sibling_dep_repair.py",
        "    if not _toml_insert_ok(text, new, section, name):",
        "    if False:  # 突变：退回只验语法（旧 M-2 行为）",
        ["TestInjectSideFakeAnchor and test_inject_does_not_corrupt_multiline_string_value",
         "TestInjectSideFakeAnchor and test_inject_returns_false_when_it_could_not_really_inject"],
    ),
    (
        "W-1-l3：写者侧后置校验的空 section 判据换成真假判（合法注入被冤杀）",
        ROOT / "worker" / "sibling_dep_repair.py",
        "    if not stripped[section] and not isinstance(old_obj.get(section), dict):",
        "    if not stripped[section] and not old_obj.get(section):",
        ["test_empty_target_section_not_falsely_rejected"],
    ),
    (
        "W-1-m：cargo 取证换自写正则（不复用写者解析器 ⇒ 口径分叉，内联表 features 丢）",
        WM,
        "        loc = _parse_cargo(local_text)",
        "        loc = {m.group(1): (m.group(1), m.group(2).strip('\"'), None, \"dependencies\")\n               for m in re.finditer(r'^\\s*([A-Za-z0-9_-]+)\\s*=\\s*(\"[^\"]+\")\\s*$', local_text, re.M)}",
        ["test_inline_table_features_are_transplanted_whole"],
    ),
]


def _pytest(extra: list[str]) -> int:
    return subprocess.run(
        [PY, "-m", "pytest", *TESTS, "-p", "no:warnings", "-q", "--tb=no", *extra],
        cwd=ROOT, capture_output=True, text=True).returncode


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
    print("步骤 0：基线必须全绿（基线是红的话所有突变结果都无意义）")
    print("═" * 70)
    rc = _pytest([])
    if rc != 0:
        print(f"✗ 基线是红的 (exit={rc}) —— 先修基线。")
        return 1
    print(f"✓ 基线全绿 (exit={rc})\n")

    failures: list[tuple[str, str]] = []
    landing_failed = 0          # ★落点失效单独计数：漂移会静默零覆盖★
    for i, (name, path, old, new, should_red) in enumerate(MUTATIONS, 1):
        src = path.read_text()
        if old not in src:
            print(f"[{i}/{len(MUTATIONS)}] {name}\n    ✗ 落点未命中（代码已漂移）")
            failures.append((name, "落点未命中"))
            landing_failed += 1
            continue
        if src.count(old) != 1:
            print(f"[{i}/{len(MUTATIONS)}] {name}\n"
                  f"    ✗ 落点出现 {src.count(old)} 次（非唯一，突变不等价）")
            failures.append((name, "落点非唯一"))
            landing_failed += 1
            continue
        path.write_text(src.replace(old, new, 1))
        _clear_pyc(path)
        try:
            try:
                ast.parse(path.read_text())
            except SyntaxError as exc:
                print(f"[{i}/{len(MUTATIONS)}] {name}\n    ✗ 突变后 ast.parse 失败: {exc}")
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
    print(f"落点失效数：{landing_failed}（>0 即有突变从未真正施加）")
    if failures:
        print(f"\n✗ {len(failures)} 条未达标：")
        for n, why in failures:
            print(f"  · [{why}] {n}")
        return 1
    print(f"\n✓ 全部 {len(MUTATIONS)} 条突变都被锁住，且基线前后皆绿，落点零失效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
