#!/usr/bin/env python3
"""P-C1 突变 harness（判据与前六批同源，那些自伤一开始就带上）：

  · **先验基线全绿**（只验"突变→红"会让**修得不全**的整改全绿通过：B-4b I-1 实证）；
  · **逐条**跑 should_red，每条都必须红；· `rc=5`（`-k` 选不到，如测试被重命名）**判失败**；
  · 落点唯一性检查；· 突变后源码必须仍能 `ast.parse`（否则 rc≠0 只是 collection error）。

★锁的命题★ P-C1＝"栈识别不得有第二事实源，且 unknown 回退必须响"。三条独立面：
识别覆盖面（含大小写档）· 伪造闸行为翻转 · 降级可观测（含反向粘滞锁）。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_b3_stack_spec_single_source.py",
         "test/test_scaffold_npm_go_driver_p2.py",
         "test/test_b0_non_maven_cassette_replay.py",
         "test/test_r39_build_scaffold_inject.py",
         "test/test_n2b_n3_go_prefix_and_rule5_stack.py",
         "test/test_round67l_plan_exam_truth.py"]

CU = ROOT / "brain" / "contract_utils.py"
PF = ROOT / "brain" / "plan_finisher.py"
SPEC = ROOT / "stacks" / "spec.py"
SS = ROOT / "brain" / "symbol_surgery.py"
ES = ROOT / "worker" / "executor_sync.py"

MUTATIONS = [
    # ── 识别覆盖面 ──
    (
        'P-C1：派生视图只出 maven（模拟"新栈没接进路由表"⇒ 纯 python 仓落回 Maven 兜底）',
        SPEC,
        '    for key in sorted(STACK_SPEC):\n        for name in STACK_SPEC[key].root_manifests:\n            out.append((name, key))',
        '    for key in ["maven"]:\n        for name in STACK_SPEC[key].root_manifests:\n            out.append((name, key))',
        # ★32 号文批2b 复跑逮死锁★ `test_every_spec_root_manifest_is_recognized_on_disk`
        # 自 30 号文 GS-1 起 parametrize 语料【派生自被突变函数本身】——本突变收缩语料
        # 与收缩生产识别等比例 ⇒ 该锁对本突变【结构性零区分力】（夹具从同一份被突变的
        # 心智模型来，0987fa9 引入时语料是字面量故有牙，GS-1 换派生后牙没了）。它对
        # 消费侧不落源表的漂移仍有牙（保留该测试本身），但本突变的区分力由下面两条
        # 独立语料的锁承载：字面量语料（python 清单必不被伪造 pom）+ 权威表交叉核对
        # （派生视图必须等长于 STACK_SPEC）。锁名也是落点：从期望表移除≠删测试。
        ['test_pure_python_repo_is_never_given_fabricated_pom',
         'test_root_manifests_by_stack_covers_every_spec_entry'],
    ),
    (
        'P-C1：派生视图小写化清单名（Linux 上 `Cargo.toml`/`Pipfile` 恒探不到 ⇒ 判 unknown ⇒ 塞 pom；'
        'F5 更正：原举例 Gemfile 不在 STACK_SPEC）。'
        '★本机 macOS 大小写不敏感，故落点必须选平台无关的纯函数属性——用"造 Pipfile 探 pipfile"'
        '的夹具在本机零区分力（harness 第一轮实测逮到）★',
        SPEC,
        '    for key in sorted(STACK_SPEC):\n        for name in STACK_SPEC[key].root_manifests:\n            out.append((name, key))',
        '    for key in sorted(STACK_SPEC):\n        for name in STACK_SPEC[key].root_manifests:\n            out.append((name.lower(), key))',
        ['test_root_manifests_by_stack_preserves_canonical_case'],
    ),
    (
        'P-C1：plan 路径档丢掉 root 兜底（plan 里建 requirements.txt 不再算 python 证据）',
        CU,
        '        _stk_hit = stack_of_structural_manifest(base) or stack_of_manifest(base)',
        '        _stk_hit = stack_of_structural_manifest(base)',
        ['test_plan_path_root_only_manifest_is_python_evidence'],
    ),
    # ── 伪造闸行为翻转 ──
    (
        'P-C1：伪造闸把已知非 Maven 栈也放行（回到"unknown 或任何栈都塞 pom"）',
        CU,
        '    _should = (stk == "unknown" or stk in _AGGREGATOR_SCAFFOLD_STACKS)',
        '    _should = True',
        ['test_pure_python_repo_is_never_given_fabricated_pom'],
    ),
    # （已删：旧突变「第三个消费者（plan_finisher 裸奔闸）退回窄表口径」的落点是 `_bstk`
    #  探测块——P-C1 复核 F2 实证该早返＝真 fail-open，#17 已把探测块随早返整体删除，
    #  该突变防护的代码不复存在。原测试 test_pc1_bare_pom_gate_recognizes_python_baseline
    #  已反转为 F2 形状，由本表末条 F2 突变锁。）
    # ── 降级可观测（血规 3）──
    (
        'P-C1：unknown 回退不再告警（降级静默 ⇒ php/ruby 被塞 pom 无声）',
        CU,
        '    if stk == "unknown":\n        # ★两因并列，不猜是哪个★',
        '    if stk == "unknown" and False:\n        # ★两因并列，不猜是哪个★',
        ['test_unknown_stack_fallback_is_loud'],
    ),
    (
        'P-C1 自查：歧义混栈不再有独占机读键（两种 unknown 塌成一个信号 ⇒ 读者被指向 php/ruby '
        '而真因是"plan 同时像两个栈"）',
        CU,
        '        logger.info("[SCAFFOLD-INJECT] G9 stack_unknown_cause=ambiguous_mixed：异栈清单证据 %s "',
        '        logger.info("[SCAFFOLD-INJECT] G9 异栈清单证据 %s "',
        ['test_ambiguous_mixed_stack_unknown_is_distinguishable_from_no_evidence'],
    ),
    (
        'P-C1 自查：机读键改成无条件打（粘滞 ⇒ 零证据也报歧义混栈）。★这条锁的是反向锁本身'
        '有区分力——第一版用散文子串"歧义混栈"判，因外层 WARNING 也含该四字而当场假绿★',
        CU,
        # ★落点必须选"零证据路径也挂上那个键"★ 不能改 `if _has_jvm_src` → `if True`：
        # `if not seen: return "unknown"` 在它**之前**早返，零证据输入根本到不了那一行
        # ⇒ 突变不等价（harness 第一轮实测存活，正是它该抓的"落点不等价"）。
        '        return "unknown"\n    if "maven" in seen:',
        '        logger.info("[SCAFFOLD-INJECT] G9 stack_unknown_cause=ambiguous_mixed（粘滞突变）")\n'
        '        return "unknown"\n    if "maven" in seen:',
        ['test_no_evidence_unknown_does_not_claim_mixed_stack'],
    ),
    (
        'P-C1：告警变无条件（粘滞告警＝等于没有告警，always-emit 一族）',
        CU,
        '    if stk == "unknown":\n        # ★两因并列，不猜是哪个★',
        '    if True:\n        # ★两因并列，不猜是哪个★',
        ['test_known_stack_does_not_emit_unknown_warning'],
    ),
    # ── P-C1 复核 F1：「清单不是实现证据」第四档派生视图（#16）──
    (
        'F1：symbol_surgery 改回手抄 5 条（go/cargo/gradle-kts/python 清单脚手架'
        '照旧是挂靠候选 ⇒ 幻影 ownership 复活）',
        SS,
        '_BUILD_MANIFESTS = frozenset(n.lower() for n in _build_manifest_basenames())',
        '_BUILD_MANIFESTS = frozenset({"pom.xml", "build.gradle", "build.gradle.kts",\n'
        '                              "settings.gradle", "package.json"})',
        ['test_every_build_manifest_is_not_implementation_evidence',
         'test_symbol_surgery_and_contract_utils_share_the_derived_set'],
    ),
    (
        'F1：contract_utils 改回手抄 7 条（settings.gradle/go.work 判 weak_code ⇒ '
        '聚合清单被当 flat 真源码参与物理根歧义判定）',
        CU,
        '_BUILD_MANIFESTS = frozenset(build_manifest_basenames())',
        '_BUILD_MANIFESTS = frozenset({"pom.xml", "build.gradle", "build.gradle.kts",\n'
        '                                     "Cargo.toml", "go.mod", "package.json",\n'
        '                                     "pyproject.toml"})',
        ['test_every_build_manifest_is_not_implementation_evidence',
         'test_symbol_surgery_and_contract_utils_share_the_derived_set'],
    ),
    (
        'F1：第四档复用 demote 门控（python 掉出 ⇒ pyproject.toml 脚手架照旧是挂靠候选；'
        '血规 10 第三条：两档后果不同绝不互换）。★指名不含 parametrize 那条★：它的夹具'
        '从同一张表派生——python 掉出并集后**参数格同步消失**，全绿（夹具与被测同源收缩，'
        '[[swarm-fallback-must-not-share-the-gap]] 同型；harness 实测存活）。防收缩锁是'
        'strict_superset 那条（手抄字面量 pyproject.toml/requirements.txt）。',
        SPEC,
        '    out: set[str] = set()\n    for spec in STACK_SPEC.values():\n'
        '        out.update(spec.root_manifests)',
        '    out: set[str] = set()\n    for spec in STACK_SPEC.values():\n'
        '        if not spec.aggregate_manifest:\n            continue\n'
        '        out.update(spec.root_manifests)',
        ['test_build_manifest_basenames_is_a_strict_superset_of_the_demote_tier'],
    ),
    (
        'F1：_evidence_class 的 manifest 判定整块消失（清单全判 weak_code/aux ⇒ '
        '物理根证据链被清单污染）',
        CU,
        '    if name.lower() in _BUILD_MANIFESTS_LC:\n        return _EV_MANIFEST',
        '    if False:\n        return _EV_MANIFEST',
        ['test_every_build_manifest_is_not_implementation_evidence'],
    ),
    (
        'F1：_subtask_modules 的清单过滤整块消失（纯清单脚手架子任务重新获得挂靠权重）。'
        '★指名不含反向锁★：反向锁的夹具全是真源码（过滤消失与否返回值相同），'
        '它对「过窄」方向结构性零区分力——它锁的是「过宽」（harness 实测存活）。',
        SS,
        '        if rest.strip().lower() in _BUILD_MANIFESTS:\n'
        '            continue  # 模块根构建清单：不构成"能实现符号"的证据',
        '        if False:\n'
        '            continue  # 模块根构建清单：不构成"能实现符号"的证据',
        ['test_every_build_manifest_is_not_implementation_evidence'],
    ),
    (
        'F1 near-miss：基线磁盘探测误用小写集（cargo.toml 在大小写敏感 FS 上探不到 '
        'Cargo.toml ⇒ 既有基线模块判不出）。本机 APFS 零区分力 ⇒ 锁查询形状',
        CU,
        '        for name in _BUILD_MANIFESTS\n    )',
        '        for name in _BUILD_MANIFESTS_LC\n    )',
        ['test_baseline_probe_queries_canonical_case_names'],
    ),
    # ── P-C1 复核 F2：裸奔闸 _bstk 早返＝真 fail-open（#17）──
    (
        'F2：非 Maven 基线早返复活（npm 基线 create-pom 零 verify ⇒ 零确定性验收直送 '
        'worker＝st-3-1 原病，且旧注释承诺的 VALIDATE 闸不存在）',
        PF,
        '    from swarm.brain.contract_utils import _root_gav\n'
        '    injected: dict[str, list[str]] = {}',
        '    import os as _os\n'
        '    from swarm.brain.contract_utils import _root_gav\n'
        '    if project_path and _os.path.exists(_os.path.join(project_path, "package.json")) \\\n'
        '            and not _os.path.exists(_os.path.join(project_path, "pom.xml")):\n'
        '        return {}\n'
        '    injected: dict[str, list[str]] = {}',
        ['test_naked_pom_non_maven_stack_skipped'],
    ),
    # ── P-C1 复核 R2（code-reviewer 第二轮）──
    (
        'R2-1：pom 判定退回 endswith（`xpom.xml` 被当 pom create ⇒ 注入 Maven 专属断言'
        '给非 pom 产物虚假背书；同 F1 误命中原理的漏网现场）',
        PF,
        '    return path.replace("\\\\", "/").rsplit("/", 1)[-1].strip().lower() == "pom.xml"',
        '    return path.replace("\\\\", "/").endswith("pom.xml")',
        ['test_xpom_xml_is_not_a_pom_create',
         'test_negated_grep_rewrite_does_not_touch_xpom_xml'],
    ),
    (
        'R2-2：sync 清单退回手抄表（派生断开 ⇒ spec 新增栈/清单不再到达 sync 层，'
        '「加一栈=加一条」的承诺在 sync 层落空——Pipfile 是第一件实证）',
        ES,
        '_SYNC_MANIFEST_NAMES = tuple(sorted(\n'
        '    set(build_manifest_basenames()) | set(_SYNC_MANIFEST_EXTRA)))',
        '_SYNC_MANIFEST_NAMES = tuple(sorted(\n'
        '    set(_SYNC_MANIFEST_EXTRA) | {\n'
        '        "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",\n'
        '        "settings.gradle.kts", "go.mod", "go.work", "Cargo.toml",\n'
        '        "package.json", "pyproject.toml", "setup.py", "requirements.txt"}))',
        ['test_sync_manifest_names_derives_from_the_single_source'],
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
        try:
            ast.parse(mutated)
        except SyntaxError as _e:
            print(f"[{i}/{len(MUTATIONS)}] {name}\n"
                  f"    ✗ 突变后源码无法编译（{_e.msg} @line {_e.lineno}）⇒ pytest 只会报 "
                  f"collection error，rc≠0 是假信号。")
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
