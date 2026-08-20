#!/usr/bin/env python3
"""★32 号文批3 突变锁★ MED 待核 4 条治法的锁区分力验证。

纪律同 r32b2b_mutation_check.py：基线先绿 / 进程内快照还原+md5 核 / 清 pyc /
落点 count==1 / 突变后源码 AST 可编译 / 绝不与全量或复核并发。
"""
from __future__ import annotations

import ast
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "bin" / "python"

NODES = ROOT / "brain" / "nodes" / "__init__.py"
SHARED = ROOT / "brain" / "nodes" / "shared.py"
L1P = ROOT / "worker" / "l1_pipeline.py"

TESTS = ["test/test_r32b3_med_locks.py"]

MUTATIONS = [
    (
        "MUT-D3 本地 L2 apply 失败回 False（infra 被判测试失败 ⇒ 假红连坐复活）",
        NODES,
        '        return None\n\n    import subprocess',
        "        return False  # 突变：apply 失败回 False\n\n    import subprocess",
        ["test_apply_failure_returns_none_not_false"],
    ),
    (
        "MUT-D4a 符号外科调用点回只接 ImportError（内部异常炸 plan 复活）",
        NODES,
        "        except Exception as _sym_ie:  # noqa: BLE001",
        "        except ImportError as _sym_ie:  # 突变：只接 ImportError",
        ["test_symbol_surgery_exception_falls_back"],
    ),
    (
        "MUT-D4b 缺件外科调用点回只接 ImportError（半落地复活）",
        NODES,
        "        except Exception as _fp_ie:  # noqa: BLE001",
        "        except ImportError as _fp_ie:  # 突变：只接 ImportError",
        ["test_fileplan_surgery_exception_falls_back"],
    ),
    (
        "MUT-S4a L2 命令尾巴回无界（中文尾巴吞进命令 ⇒ mvn/pytest 收垃圾参数假红复活）",
        SHARED,
        '    r"(?:[ \\t]+(?:\'[^\'\\n]*\'|\\"[^\\"\\n]*\\"|[\\x21-\\x3a\\x3c-\\x7b\\x7d-\\x7e])+)*)",',
        '    r"(?:\\s+[^\\n;|]+)?)",  # 突变：无界尾巴复活',
        ["test_corpus", "test_chinese_tail_not_in_command"],
    ),
    (
        "MUT-S4b L2 命令缺 go/cargo/gradle 族（显式要求的测试被静默 skip=假过复活）",
        SHARED,
        '    r"|go\\s+test|cargo\\s+test|(?:\\./)?gradlew?\\s+test)(?!\\w)"',
        '    r")(?!\\w)",  # 突变：三族缺失复活',
        ["test_corpus", "test_stack_spec_test_cmds_all_recognized"],
    ),
    (
        "MUT-S5a 路径臂回子串（main/x ⊂ mymain/x 段内延续误归因复活）",
        SHARED,
        "        if (f and _evidence_mentions(blob, f, is_basename=False)) or (",
        "        if (f and f in blob) or (  # 突变：路径子串复活",
        ["test_path_segment_extension_rejected"],
    ),
    (
        "MUT-S5b basename 臂回子串（Config.java ⊂ SecurityConfig.java 误归因复活）",
        SHARED,
        "                base and _base_count.get(base, 0) == 1\n"
        "                and _evidence_mentions(blob, base, is_basename=True)):",
        "                base and base in blob):  # 突变：basename 子串复活",
        ["test_substring_basename_not_attributed",
         "test_basename_ambiguous_not_used"],
    ),
    (
        "MUT-S5c basename 消歧规则删除（歧义 basename 参与匹配 ⇒ 多模块 pom 误归因复活）",
        SHARED,
        "                base and _base_count.get(base, 0) == 1\n"
        "                and _evidence_mentions(blob, base, is_basename=True)):",
        "                base\n"
        "                and _evidence_mentions(blob, base, is_basename=True)):  # 突变：消歧规则删除",
        ["test_basename_ambiguous_not_used"],
    ),
    # ── R1 整改（hunter MED-1/MED-2 + reviewer F-1/F-4）──
    (
        "MUT-R1-MED1 沙箱臂 apply 失败回 False（首选路径假红连坐复活=半落地）",
        NODES,
        "                _apply_rc, apply_out[:500],\n            )\n            return None",
        "                _apply_rc, apply_out[:500],\n            )\n            return False  # 突变",
        ["TestR1SandboxApplyAndToolMissing and test_apply_failure"],
    ),
    (
        "MUT-R1-MED2a 沙箱测试臂 tool-missing 判据失效（go/cargo 缺工具链假红复活）",
        NODES,
        "            _miss = _l2_tool_missing(test_cmd, test_out)",
        '            _miss = ""  # 突变：tool-missing 判据失效',
        ["TestR1SandboxApplyAndToolMissing and test_tool_missing_returns_none"],
    ),
    (
        "MUT-R1-MED2b 本地测试臂 tool-missing 判据失效（同上本地版）",
        NODES,
        '            _miss = _l2_tool_missing(test_cmd, (proc.stderr or "") + (proc.stdout or ""))',
        '            _miss = ""  # 突变',
        ["test_tool_missing_returns_none_not_false"],
    ),
    (
        "MUT-R1-F1 引号段臂删除（pytest -k '登录用例' 截出破引号命令 ⇒ shell 语法错假红复活）",
        SHARED,
        '    r"(?:[ \\t]+(?:\'[^\'\\n]*\'|\\"[^\\"\\n]*\\"|[\\x21-\\x3a\\x3c-\\x7b\\x7d-\\x7e])+)*)",',
        '    r"(?:[ \\t]+[\\x21-\\x3a\\x3c-\\x7b\\x7d-\\x7e]+)*)",  # 突变：引号段臂删除',
        ["test_corpus"],
    ),
    (
        "MUT-R1-F4 import_repair 归一撤销（/workspace/ 绝对形态进 changed ⇒ 下游比对全漏复活）",
        L1P,
        "            ec2, _out = _run_l1_command(scmd, project_path, timeout=20)\n"
        "            if ec2 == 0 and rel is not None:\n"
        "                changed.add(rel)",
        "            ec2, _out = _run_l1_command(scmd, project_path, timeout=20)\n"
        "            if ec2 == 0 and rel is not None:\n"
        "                changed.add(f)  # 突变：归一撤销",
        ["test_absolute_workspace_path_normalized"],
    ),
    # ── R2-close 整改（hunter H-1/H-2）──
    (
        "MUT-R2-H1 tool-missing 左边界删除（logo: not found 把真测试失败洗成 infra=fail-open）",
        NODES,
        '        if re.search(rf"(?<![\\w.-]){re.escape(_t)}: (?:command not found|not found)", out):',
        '        if re.search(rf"{re.escape(_t)}: (?:command not found|not found)", out):  # 突变',
        ["test_substring_tool_name_not_missing"],
    ),
    (
        "MUT-R2-H2 wrapper No-such-file 臂删除（./gradlew 缺失判 False=greenfield 假红空转复活）",
        NODES,
        '        if "/" in _t and re.search(',
        '        if False and "/" in _t and re.search(  # 突变',
        ["test_wrapper_no_such_file_returns_none"],
    ),
    # ── R3-close 整改（hunter F-R3-1/2/3 + R2-close 遗留死突变面 L-1→MUT-R2-H3）──
    (
        "MUT-R2-H3 dash 措辞臂删除（sh: 1: go: not found 漏判 ⇒ 假红复活=死突变面补盖）",
        NODES,
        '        if re.search(rf"(?<![\\w.-]){re.escape(_t)}: (?:command not found|not found)", out):',
        '        if re.search(rf"(?<![\\w.-]){re.escape(_t)}: (?:command not found)", out):  # 突变',
        ["test_dash_wording_not_found"],
    ),
    (
        "MUT-R3-F1 ../ 守卫删除（项目外绝对形态产毒串进 changed ⇒ git diff targets 连坐=E7①复活）",
        L1P,
        '                if _rp == ".." or _rp.startswith("../"):',
        "                if False:  # 突变：../ 守卫删除",
        ["test_out_of_project_absolute_not_registered"],
    ),
    (
        "MUT-R3-F2 绝对门控删除（vendor/workspace 相对形误剥复活=修复静默蒸发）",
        L1P,
        '    if p.startswith("/"):',
        '    if True:  # 突变：绝对门控删除',
        ["test_relative_mid_workspace_segment_not_stripped"],
    ),
    (
        "MUT-R3-F3 python→python3 别名删除（沙箱改写后缺 python3 措辞漏判 ⇒ 假红复活）",
        NODES,
        '    tools = (tool, "python3") if tool == "python" else (tool,)',
        '    tools = (tool,)  # 突变：别名删除',
        ["test_python_alias_python3_wording"],
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
    files = (NODES, SHARED, L1P)
    md5_before = {p: hashlib.md5(p.read_bytes()).hexdigest() for p in files}
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
