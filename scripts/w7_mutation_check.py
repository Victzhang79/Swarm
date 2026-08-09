#!/usr/bin/env python3
"""#29-2 W-7 突变 harness（判据与前批同源）：

  · **先验基线全绿**；· **逐条**跑 should_red；`rc=5` 判失败；· 落点唯一性 + 落点失效数；
  · 突变后源码必须仍能 `ast.parse`；每条突变前与还原后都清 pyc；
  · harness 改磁盘源码——**绝不进带超时的循环、绝不与全量并发、跑完看 git status**。

★锁的命题★ 全树扫描签名必须**真的随文件变化**，缓存才会失效：
  ① 签名命令不得按本机平台分叉（沙箱优先执行 ⇒ 本机 macOS 生成 BSD 语法在 Linux 失效）；
  ② 不得依赖 `stat`（GNU/BSD 分叉源）；
  ③ 空 cksum（`4294967295 0`）与命令失败同值 ⇒ 必须当【无签名】不缓存——原兜底只认空串，
     被这个**非空常量**完整绕过，这才是 W-7 能咬到生产的咬合点。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_w7_scan_signature_platform_neutral.py",
         "test/test_a7_scan_cache.py"]

L1 = ROOT / "worker" / "l1_pipeline.py"

_OLD_GNU = (
    '_SCAN_SIG_CMD = (\n'
    '    "find . \\\\( -name \'*.java\' -o -name \'*.kt\' -o -name \'*.scala\' \\\\) -exec cksum {} + "\n'
    '    "2>/dev/null | sort | cksum"\n'
    ')'
)

MUTATIONS = [
    (
        "W-7-a：签名命令退回按 sys.platform 选 stat 语法（复现原缺陷：本机语法 vs 沙箱执行错配）",
        L1,
        'def _scan_sig_command() -> str:\n    """签名命令。平台中立 ⇒ 无分支：本机与沙箱用同一条（原按 sys.platform 分叉正是 W-7 根因）。"""\n    return _SCAN_SIG_CMD',
        'def _scan_sig_command() -> str:\n    _bsd = _SCAN_SIG_CMD.replace("-exec cksum {} +", "-exec stat -f \'%N|%z|%m\' {} +")\n    _gnu = _SCAN_SIG_CMD.replace("-exec cksum {} +", "-exec stat -c \'%n|%s|%Y\' {} +")\n    return _bsd if sys.platform == "darwin" else _gnu',
        ["test_signature_command_has_no_platform_branch",
         "test_signature_command_uses_no_stat"],
    ),
    (
        "W-7-b：签名命令写死 GNU stat（在本机 BSD 上恒产空 cksum＝W-7 在沙箱里的实际取值）",
        L1,
        _OLD_GNU,
        _OLD_GNU.replace("-exec cksum {} + ",
                         "-exec stat -c '%n|%s|%Y' {} + "),
        ["test_signature_is_not_the_empty_cksum_on_a_real_tree",
         "test_signature_changes_when_content_changes",
         "test_end_to_end_cache_invalidates_on_real_file_change"],
    ),
    (
        "W-7-c：空 cksum 兜底删除（原病：非空常量一路通过 ⇒ 缓存永不失效）",
        L1,
        '    if sig == _EMPTY_CKSUM:',
        '    if False:  # 突变：空 cksum 兜底删除',
        ["test_empty_cksum_disables_caching",
         "test_empty_cksum_with_trailing_whitespace_also_disabled"],
    ),
    (
        "W-7-c3：空 cksum 降级退回 logger.debug（生产默认 INFO ⇒ W-7 复发时唯一信号被吞）",
        L1,
        '        logger.warning(\n'
        '            "[L1·A7] 全树签名为空 cksum(%s)——树内无 JVM 源文件或签名命令失效，两者不可辨"',
        '        logger.debug(\n'
        '            "[L1·A7] 全树签名为空 cksum(%s)——树内无 JVM 源文件或签名命令失效，两者不可辨"',
        ["test_empty_cksum_degradation_is_visible_at_default_log_level"],
    ),
    (
        "W-7-d：空 cksum 判据放到 strip 之前（真实 shell 输出带尾换行 ⇒ 该档从旁路溜过）",
        L1,
        '        sig = (sig_out or "").strip()',
        '        sig = (sig_out or "")',
        ["test_empty_cksum_with_trailing_whitespace_also_disabled"],
    ),
    (
        "W-7-c2：_EMPTY_CKSUM 常量写错（兜底比对不上 ⇒ 空签名被当有效签名 ⇒ 缓存永不失效；"
        "该常量是硬编码字面量，必须由本平台 cksum 自证而非靠记忆的参考值）",
        L1,
        '_EMPTY_CKSUM = "4294967295 0"',
        '_EMPTY_CKSUM = "0 0"',
        # 只有自证那条该红。首列时我把 `test_signature_is_not_the_empty_cksum_on_a_real_tree`
        # 也列进来，被 harness 判"仍绿"——它是对的：那条断言 `sig != _EMPTY_CKSUM`，常量被改错
        # 后它拿**错的常量**去比，反而**恒真通过**。这正是"常量自证"必须独立成一条测试的理由：
        # 所有引用该常量的断言都会跟着常量一起错，只有跟**外部事实**（本平台 cksum 实际输出）
        # 对账的那条才抓得住。
        ["test_empty_cksum_constant_matches_this_platform"],
    ),
    (
        "W-7-e：签名口径扩到所有文件（改 README 就让 JVM 符号表缓存失效 ⇒ 缓存形同不存在）",
        L1,
        '''    "find . \\\\( -name '*.java' -o -name '*.kt' -o -name '*.scala' \\\\) -exec cksum {} + "''',
        '''    "find . -type f -exec cksum {} + "''',
        ["test_signature_ignores_non_jvm_files"],
    ),
    (
        "W-7-f：签名口径漏 kotlin/scala（多栈 JVM 工程的符号表不失效）",
        L1,
        '''    "find . \\\\( -name '*.java' -o -name '*.kt' -o -name '*.scala' \\\\) -exec cksum {} + "''',
        '''    "find . -name '*.java' -exec cksum {} + "''',
        ["test_signature_covers_kotlin_and_scala"],
    ),
    (
        "W-7-g：缓存整条拆掉（反向锁：只验「不缓存」的测试会让这条也全绿）",
        L1,
        '    key = (project_path, scan_cmd)\n    if sig:\n        cached = _SCAN_CACHE.get(key)',
        '    key = (project_path, scan_cmd)\n    if False:\n        cached = _SCAN_CACHE.get(key)',
        ["test_real_signature_still_enables_caching",
         "test_cached_scan_reuses_when_files_unchanged"],
    ),
]


def _pytest(extra: list[str]) -> int:
    return subprocess.run(
        [PY, "-m", "pytest", *TESTS, "-p", "no:warnings", "-q", "--tb=no", *extra],
        cwd=ROOT, capture_output=True, text=True).returncode


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
        print(f"✗ 基线是红的 (exit={rc}) —— 先修基线。")
        return 1
    print(f"✓ 基线全绿 (exit={rc})\n")

    failures: list[tuple[str, str]] = []
    landing_failed = 0
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
