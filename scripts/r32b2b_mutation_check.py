#!/usr/bin/env python3
"""★32 号文批2b 突变锁★ MED 坐实 11 条治法的锁区分力验证。

纪律同 r32b2a_mutation_check.py：基线先绿 / 进程内快照还原+md5 核 / 清 pyc /
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

RUNTIME_SMOKE = ROOT / "brain" / "nodes" / "runtime_smoke.py"
DISPATCH = ROOT / "brain" / "nodes" / "dispatch.py"
ESYNC = ROOT / "worker" / "executor_sync.py"
NODES = ROOT / "brain" / "nodes" / "__init__.py"
SHARED = ROOT / "brain" / "nodes" / "shared.py"

TESTS = ["test/test_r32b2b_med_locks.py"]

MUTATIONS = [
    (
        "MUT-R1a 残缺索引降级旗标恒 False（残缺与完整同形复活，假过方向隐形）",
        RUNTIME_SMOKE,
        "    degraded = bool(build_error or walk_errors or truncated)",
        "    degraded = False  # 突变：残缺索引与完整同形",
        ["test_walk_error_flagged_and_warns",
         "test_truncation_flagged",
         "test_exception_flagged_not_silent"],
    ),
    (
        "MUT-R1b 分类器不消费 degraded 信号（新账没有消费者=没造）",
        RUNTIME_SMOKE,
        '        if isinstance(project_symbols, dict) and project_symbols.get("degraded"):',
        "        if False:  # 突变：degraded 信号不消费",
        ["test_degraded_index_machine_readable_in_details"],
    ),
    (
        "MUT-P1 非正阈值静默折 0（软掉账静默关闭+账龄 WARNING 空承诺复活）",
        DISPATCH,
        "    if v <= 0:\n"
        "        logger.warning(\n"
        '            "[DISPATCH] SWARM_REDISPATCH_HARD_WINDOWS 非正值 %d 会静默关闭软掉账（账龄 "\n'
        '            "WARNING 照打却永不兑现）——拒绝，回退默认 24；关闸请用 "\n'
        '            "SWARM_REDISPATCH_SOFT_DROP=0", v)\n'
        "        return 24",
        "    if v <= 0:\n"
        "        return 0  # 突变：非正静默折 0（软掉账静默关闭复活）",
        ["test_bad_values_warn_and_default"],
    ),
    (
        "MUT-E1a FINDING-11 通道 git 枚举异常静默置空（父 pom 不补传零留痕复活）",
        ESYNC,
        "                except Exception as _enum_exc:  # noqa: BLE001\n"
        "                    _ch, _ut = [], []\n"
        "                    logger.warning(\n"
        '                        "[SYNC] FINDING-11 补传通道 git 枚举异常 → 本轮 build-critical 清单"\n'
        '                        "补传为空（父 pom 不补传 ⇒ reactor not found 复发风险）: %s",\n'
        "                        _enum_exc)",
        "                except Exception:  # noqa: BLE001\n"
        "                    _ch, _ut = [], []  # 突变：静默置空复活",
        ["test_git_exception_warns"],
    ),
    (
        "MUT-E1b FINDING-11 通道 git rc≠0 静默（stdout 空=补传集凭空缺）",
        ESYNC,
        "                    if _ch_r.returncode != 0 or _ut_r.returncode != 0:",
        "                    if False:  # 突变：rc≠0 静默",
        ["test_git_rc_nonzero_warns"],
    ),
    (
        "MUT-E2a 回滚 prune 臂裸 pass（幽灵 module 残留全员 reactor 炸零留痕复活）",
        ESYNC,
        "                    except Exception as _prune_exc:  # noqa: BLE001\n"
        "                        # ★32 号文批2b E2★：此处曾是裸 pass——摘除失败=幽灵 <module>\n"
        "                        # 残留 root pom，兄弟沙箱复制后全员 reactor 必炸（上方注释\n"
        "                        # 自己写明该后果），而外层调用点的 WARNING 兜底看不见内层\n"
        "                        # 已吞的异常。回滚不改变终局判定的语义不变，但必须可观测。\n"
        "                        logger.warning(\n"
        '                            "[H2] root pom 幽灵 <module> 摘除异常（残留 ⇒ 兄弟沙箱 "\n'
        '                            "reactor 可能全炸，不致命）: %s", _prune_exc)',
        "                    except Exception:  # noqa: BLE001\n"
        "                        pass  # 突变：裸 pass 复活",
        ["test_prune_exception_warns_not_silent"],
    ),
    (
        "MUT-E2b 回滚 read 臂静默 continue（毒贡献残留零留痕复活）",
        ESYNC,
        "                except Exception as _read_exc:  # noqa: BLE001\n"
        "                    # ★32 号文批2b E2 sibling★：同环 read 失败曾是静默 continue——\n"
        "                    # 该清单整份不剥离 = 毒贡献残留共享树，与同函数 baseline-missing\n"
        "                    # 臂（W-2 R1 已补 WARNING）同后果同形，补同级信号。\n"
        "                    logger.warning(\n"
        '                        "[H2] 回滚跳过 %s：读本地清单失败（毒贡献可能残留共享树，"\n'
        '                        "不致命）: %s", rel, _read_exc)\n'
        "                    continue",
        "                except Exception:  # noqa: BLE001\n"
        "                    continue  # 突变：静默跳过复活",
        ["test_read_failure_warns_not_silent"],
    ),
    (
        "MUT-D1 learn_success 交付异常臂不进 _degraded（没交付被 L6 学成成功复活）",
        NODES,
        '    except Exception as exc:  # noqa: BLE001\n'
        '        logger.warning("[LEARN_SUCCESS] 产出 commit 异常(非致命): %s", exc)\n'
        "        # ★32 号文批2b D1-下半★：本臂此前只 WARNING 不进 _degraded——而 apply 全失败/\n"
        "        # apply 不完整/commit 失败三条同区失败臂都进（F5）。交付链整体异常=交付成败\n"
        "        # 未知，should_write_success 看不见 ⇒ \"没交付成功\"被 L6 学成成功（记忆毒化）。\n"
        '        _degraded.append("delivery_commit_exception")',
        '    except Exception as exc:  # noqa: BLE001\n'
        '        logger.warning("[LEARN_SUCCESS] 产出 commit 异常(非致命): %s", exc)',
        ["test_delivery_exception_enters_degraded"],
    ),
    (
        "MUT-D2b learn_success proj_path None 静默跳过 commit（零信号复活）",
        NODES,
        "            if not proj_path:\n"
        "                logger.warning(\n"
        '                    "[LEARN_SUCCESS] proj_path 解析失败(project_id=%r) → 产出 commit 整块"\n'
        '                    "跳过（交付未固化，绝非成功路径）", state.get("project_id"))\n'
        '                _degraded.append("delivery_project_path_missing")',
        "            if not proj_path:\n"
        "                pass  # 突变：静默跳过复活",
        ["test_proj_path_missing_degraded"],
    ),
    (
        "MUT-D4 merge fold 异常不进 degraded_reasons（L6/人工闸全瞎复活）",
        NODES,
        '            out["degraded_reasons"] = (\n'
        '                list(out.get("degraded_reasons") or []) + ["merge_d1_fold_failed"])',
        "            pass  # 突变：fold 异常不进 degraded_reasons",
        ["test_fold_exception_enters_degraded_reasons"],
    ),
    (
        "MUT-D4b 温和出口 fold 异常不进 degraded_reasons（第二落点，reviewer F1）",
        NODES,
        '                    # "掉了 rebase"不知道"折叠也失败了"。fail-open 方向不变。\n'
        '                    out["degraded_reasons"] = (\n'
        '                        list(out.get("degraded_reasons") or []) + ["merge_d1_fold_failed"])',
        '                    # "掉了 rebase"不知道"折叠也失败了"。fail-open 方向不变。\n'
        "                    pass  # 突变：温和出口 fold 异常不进 degraded_reasons",
        ["test_clean_accept_arm_fold_exception_degraded"],
    ),
    (
        "MUT-D2up rebase 计数表不剪枝（陈旧计数⇒语义新子任务首个 rebase 即超限复活）",
        NODES,
        "    pruned_rebase = {\n"
        "        sid: n for sid, n in (old_rebase_counts or {}).items() if _sig_unchanged(sid)\n"
        "    }",
        "    pruned_rebase = dict(old_rebase_counts or {})  # 突变：不剪枝",
        ["test_prunes_by_signature"],
    ),
    (
        "MUT-D3 batch attempts 回裸 int（非法值炸 plan 节点/非正零尝试全败复活）",
        NODES,
        '        v = int(os.environ.get("SWARM_PLAN_BATCH_MAX_ATTEMPTS", "2") or "2")\n'
        "        if v <= 0:\n"
        '            raise ValueError("non-positive")\n'
        "        return v",
        '        return int(os.environ.get("SWARM_PLAN_BATCH_MAX_ATTEMPTS", "2") or "2")'
        "  # 突变：裸 int 复活",
        ["test_resolve"],
    ),
    (
        "MUT-S2a 水平合并 verify_commands 只保 base（非 base 验收 L1 永不跑复活）",
        SHARED,
        '                verify_commands=_ulist("verify_commands"),',
        "                verify_commands=(list(base.harness.verify_commands)\n"
        "                                 if base.harness else []),  # 突变：只保 base",
        ["test_verify_commands_union_not_dropped",
         "test_harness_default_member_tolerated"],
    ),
    (
        "MUT-S2b 水平合并 contract 只保 base（worker 缺契约上下文盲改复活）",
        SHARED,
        "            contract=merged_contract,",
        "            contract=base.contract or {},  # 突变：非 base 契约丢失复活",
        ["test_contract_union_not_dropped"],
    ),
    (
        "MUT-S3 Java 测试后缀无边界（Latest.java/Contest.java 误杀剥出 scope 复活）",
        SHARED,
        '        or base in ("test.java", "tests.java")\n'
        '        or base_orig.endswith("Test.java") or base_orig.endswith("Tests.java")',
        '        or base.endswith("test.java") or base.endswith("tests.java")'
        "  # 突变：无边界误杀复活",
        ["test_boundary",
         "test_strip_unrequested_tests_keeps_latest_java"],
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
    files = (RUNTIME_SMOKE, DISPATCH, ESYNC, NODES, SHARED)
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
