#!/usr/bin/env python3
"""★32 号文批4 突变锁★ LOW 收口治法的锁区分力验证。

纪律同 r32b3_mutation_check.py：基线先绿 / 进程内快照还原+md5 核 / 清 pyc /
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

SHARED = ROOT / "brain" / "nodes" / "shared.py"
DISPATCH = ROOT / "brain" / "nodes" / "dispatch.py"
EXEC = ROOT / "worker" / "executor_sync.py"
SECSCAN = ROOT / "worker" / "security_scan.py"
PLANCORE = ROOT / "brain" / "nodes" / "planning_core.py"
NODES = ROOT / "brain" / "nodes" / "__init__.py"
RSMOKE = ROOT / "brain" / "nodes" / "runtime_smoke.py"
L1P = ROOT / "worker" / "l1_pipeline.py"

TESTS = ["test/test_r32b4_low_locks.py"]

MUTATIONS = [
    # ── shared.py ──
    (
        "MUT-S8 关键词边界删除（react⊂reactor 撞 node 臂 / spring⊂springframework 误判复活）",
        SHARED,
        '                if re.search(rf"(?<![a-z]){re.escape(k.strip())}(?![a-z])", text):',
        "                if k.strip() in text:  # 突变：边界删除退回子串",
        ["test_reactor_not_node", "test_springframework_oneword_falls_generic"],
    ),
    (
        "MUT-S5bak 右界回退（Config.java 在 Config.java.bak 里假命中=误归因复活）",
        SHARED,
        '        pat = r"(?<![\\w])" + re.escape(needle) + r"(?![\\w.])"',
        '        pat = r"(?<![\\w])" + re.escape(needle) + r"(?![\\w])"  # 突变',
        ["test_bak_suffix_rejected_both_arms"],
    ),
    (
        "MUT-F2a mvn 中间相位/旗标形删除（mvn clean test 不被识别 ⇒ 显式要求的测试静默 skip）",
        SHARED,
        "(?:\\./)?mvnw?(?:[ \\t]+(?:-\\S+(?:[ \\t]+[^\\s-]\\S*)?|clean|install|package|verify|compile))*[ \\t]+test",
        "mvn\\s+test",
        ["test_mvn_phase_before_test", "test_mvn_flag_before_test",
         "test_value_flag_forms_recognized"],
    ),
    (
        "MUT-F2b npm run 形删除（npm run test 不被识别 ⇒ 同上假过）",
        SHARED,
        "npm\\s+(?:run[ \\t]+)?test",
        "npm\\s+test",
        ["test_npm_run_test"],
    ),
    (
        "MUT-F2c 跳测旗标后滤删除（mvn -DskipTests test 被识别 ⇒ rc=0 零测试洗成 passed=假过）",
        SHARED,
        "        if match and not _has_skip_test_flag(match.group(1)):",
        "        if match:  # 突变：跳测旗标后滤删除",
        ["test_skip_test_flags_not_recognized"],
    ),
    (
        "MUT-F3 后缀歧义闸删除（短 needle 在长路径证据里假命中 ⇒ 误归因定向重派复活）",
        SHARED,
        "        if (f and not _suffix_ambiguous(f)",
        "        if (f  # 突变：后缀歧义闸删除",
        ["test_long_path_evidence_attributes_only_long"],
    ),
    (
        "MUT-LOW2 scope 路径归一撤销（./src/A.java 与 src/A.java 两个键 ⇒ 唯一性统计错键复活）",
        SHARED,
        "            f = _norm_scope_path(str(f).strip())",
        "            f = str(f).strip()  # 突变：归一撤销",
        ["test_dot_slash_and_plain_same_key"],
    ),
    (
        "MUT-LOW2b 空白剥离丢失（_norm_scope_path 不剥空白 ⇒ 同源不同键病对空白形态重开）",
        SHARED,
        "            f = _norm_scope_path(str(f).strip())",
        "            f = _norm_scope_path(f)  # 突变：strip 丢失",
        ["test_whitespace_padded_entry_same_key"],
    ),
    (
        "MUT-S6b parallel_groups 重建删除（合并后分组错位 ⇒ 调度串行化/错组，AST 锁必须逮到）",
        SHARED,
        "    return _rebuild_plan(plan, merged_subs).model_copy(\n"
        "        update={\"parallel_groups\": [[i] for i in new_ids]})",
        "    return _rebuild_plan(plan, merged_subs)  # 突变：分组重建删除",
        ["test_rebuild_via_single_source"],
    ),
    (
        "MUT-S7 裸 `并` 分隔符回退（并行/并发/并且/并存 拦腰切断 ⇒ 意图与文件名拆句误归类）",
        SHARED,
        "并(?!行|发|且|存)",
        "并",
        ["test_bingxing_not_split"],
    ),
    (
        "MUT-S9 清空失败降级静默（冻结石雕 harness 测试门没拆掉零信号复活）",
        SHARED,
        "                logging.getLogger(__name__).warning(\n"
        "                    \"[PLAN] 测试剔除：harness.test_command 清空失败",
        "                logging.getLogger(__name__).debug(  # 突变：降级静默\n"
        "                    \"[PLAN] 测试剔除：harness.test_command 清空失败",
        ["test_frozen_harness_warns"],
    ),
    (
        "MUT-S6 B-1 单源撤销（TaskPlan 重建不走 _rebuild_plan ⇒ 四张 plan 级账静默丢失）",
        SHARED,
        "    from swarm.brain.planning_nodes import _rebuild_plan",
        "    _rebuild_plan = None  # 突变：B-1 单源撤销",
        ["test_rebuild_via_single_source"],
    ),
    # ── dispatch.py ──
    (
        "MUT-P2 空值守卫删除（ROLL_FACTOR 空串静默折 0=滚动闸被悄悄关掉复活）",
        DISPATCH,
        "        if not _raw_roll:",
        "        if False:  # 突变：空值守卫删除",
        ["test_empty_and_negative_guards_wired"],
    ),
    (
        "MUT-P4 漏斗撤销（C-quoted 带空格路径捕出垃圾串复活）",
        DISPATCH,
        "        new_path = strip_diff_path(_new_raw)",
        "        new_path = _new_raw.strip()  # 突变：漏斗撤销",
        ["test_deleted_modified_added_quoted"],
    ),
    (
        "MUT-P4b payload 前缀门控删除（hunk 体 `++ x`/`-- x` 内容行伪装头行 ⇒ 幽灵 FileChange 复活）",
        DISPATCH,
        "        if not (_new_raw.startswith((\"b/\", '\"b/'))\n"
        "                or _new_raw.split(\"\\t\", 1)[0].strip() == \"/dev/null\"):\n"
        "            continue  # hunk 体内容行假头（\"++ x\" 渲染形）——非 b//dev/null payload 拒收",
        "        pass  # 突变：payload 门控删除",
        ["test_hunk_body_fake_headers_rejected"],
    ),
    (
        "MUT-P5 无 loop 降级静默（回灌排不上=知识库静默滞后零信号复活）",
        DISPATCH,
        "            logger.warning(\n"
        "                \"[DISPATCH] 知识库回灌跳过：无运行中的事件循环（fire-and-forget 无法调度）\")",
        "            pass  # 突变：降级静默",
        ["test_no_running_loop_warns"],
    ),
    # ── executor_sync.py ──
    (
        "MUT-E3 逐条隔离回退（一条坏条目静默中断后续全部兄弟产物复活）",
        EXEC,
        "                except Exception:  # noqa: BLE001 — 单条失败不连坐兄弟条目\n"
        "                    _e3_fail += 1",
        "                except Exception:  # noqa: BLE001\n"
        "                    break  # 突变：单条失败连坐兄弟",
        ["test_one_bad_entry_does_not_block_siblings"],
    ),
    (
        "MUT-E3ROOT 根路径异常 WARNING 删除（整段并入静默缺席=零信号复活）",
        EXEC,
        '        if _root is None and getattr(scope, "upstream_artifacts", None):',
        '        if False and getattr(scope, "upstream_artifacts", None):  # 突变',
        ["test_root_none_warns_when_artifacts_pending"],
    ),
    (
        "MUT-E4 warning 级别丢失（达上限信号淹没在 INFO=与恰好 cap 个不可辨复活）",
        EXEC,
        '                f"H-exec1 目录内枚举达上限 {_WORKSPACE_LIST_CAP} → 可能漏新建文件",\n'
        '                level="warning",',
        '                f"H-exec1 目录内枚举达上限 {_WORKSPACE_LIST_CAP} → 可能漏新建文件",  # 突变',
        ["test_cap_logs_at_warning_level"],
    ),
    # ── funnel 两处 ──
    (
        "MUT-FUN1 security_scan 漏斗撤销（quoted 路径反转义丢失复活）",
        SECSCAN,
        "    p = strip_diff_path(header_line[4:])",
        "    p = header_line[4:].strip()  # 突变：漏斗撤销",
        ["test_quoted_space_path"],
    ),
    (
        "MUT-FUN2 planning_core _cur_file 漏斗撤销（分桶键带引号=盘侧动作打错文件复活）",
        PLANCORE,
        "            _cur_file = strip_diff_path(_ln[4:])",
        "            _cur_file = _ln[4:].strip()  # 突变：漏斗撤销",
        ["test_quoted_header_bucket_key"],
    ),
    # ── planning_core A10 簇 ──
    (
        "MUT-A10a parse_failed 键删除（空 files 静默 None 不可辨复活）",
        PLANCORE,
        '        _record_degrade_safe_pc("brain.stub_gen.parse_failed")',
        "        pass  # 突变：机读键删除",
        ["test_empty_files_records_parse_failed"],
    ),
    (
        "MUT-A10b empty_written 键删除（全拒/全写失败静默 None 不可辨复活）",
        PLANCORE,
        '        _record_degrade_safe_pc("brain.stub_gen.empty_written")',
        "        pass  # 突变：机读键删除",
        ["test_all_skipped_records_file_skipped_and_empty_written"],
    ),
    (
        "MUT-A10c verified_sibling import 删行（NameError≠ImportError：抛纪律断=锁红）",
        PLANCORE,
        "    from swarm.brain.nodes.dispatch import _changes_from_diff\n"
        "    for s in subtasks:",
        "    for s in subtasks:",
        ["test_verified_sibling_files_raises"],
    ),
    (
        "MUT-A10d clean_residue import 删行（同上 sibling）",
        PLANCORE,
        "    from swarm.brain.nodes.dispatch import _changes_from_diff\n"
        "    try:",
        "    try:",
        ["test_clean_stub_residue_raises"],
    ),
    # ── nodes/__init__.py ──
    (
        "MUT-D3 spawn infra 回 False（fork 失败判测试失败 ⇒ 假红 replan 空转复活）",
        NODES,
        '"[VERIFY_L2] 本地测试进程启动失败（infra，非代码失败）→ 降级 LLM 兜底: %s", exc)\n'
        "        return None",
        '"[VERIFY_L2] 本地测试进程启动失败（infra，非代码失败）→ 降级 LLM 兜底: %s", exc)\n'
        "        return False  # 突变",
        ["test_oserror_returns_none_with_warning"],
    ),
    (
        "MUT-D4 精确匹配回子串（App.java 命中 App.java.bak 头行 ⇒ 注错段引导 worker 复活）",
        NODES,
        "                    if (_old and _old in _touched) or (_new and _new in _touched):",
        "                    if any(f in seg[:200] for f in _touched):  # 突变：子串判据复活",
        ["test_exact_header_match_wired"],
    ),
    (
        "MUT-D5 adversarial_round 重置删除（上一轮对抗验证轮次当本轮覆盖上报复活）",
        NODES,
        '        "adversarial_verify_round": 0,\n',
        "",
        ["test_rev_out_resets_all"],
    ),
    (
        "MUT-D5b skipped 重置值极性反转（None→False=「跑过且未跳过」的说谎值复活）",
        NODES,
        '        "runtime_smoke_skipped": None,\n',
        '        "runtime_smoke_skipped": False,  # 突变\n',
        ["test_rev_out_resets_all"],
    ),
    (
        "MUT-D6 soft_sig 清位删除（SIMPLE 轮软审签名跨轮粘滞复活）",
        NODES,
        '            "plan_soft_review_sig": "",\n',
        "",
        ["test_simple_return_has_both_keys"],
    ),
    (
        "MUT-D6b soft_sig 清位值极性反转（""→陈值占位=跨轮粘滞复活，键在场锁抓不住）",
        NODES,
        '            "plan_soft_review_sig": "",\n',
        '            "plan_soft_review_sig": "stale",  # 突变\n',
        ["test_simple_return_has_both_keys"],
    ),
    # ── runtime_smoke.py ──
    (
        "MUT-O3 package.json 证据臂删除（裸目录撞名第三方包 ⇒ 冤判 code_error 硬拦交付复活）",
        RSMOKE,
        '                    f"{name}/package.json" in paths',
        "                    True  # 突变：证据臂删除",
        ["test_bare_dir_not_evidence"],
    ),
    (
        "MUT-R3 resolve 预算删除（反解段吃光缓冲 ⇒ run_command 超时误归因 not_executed 复活）",
        RSMOKE,
        "    resolve_budget = _resolve_positive_int_env(\n"
        "        PORT_RESOLVE_WINDOW_ENV, PORT_RESOLVE_WINDOW_SEC) if probe_port is None else 0",
        "    resolve_budget = 0  # 突变：预算删除",
        ["test_resolve_budget_in_timeout"],
    ),
    (
        "MUT-R2 docstring 词表漂移（产出集对账锁必须逮到）",
        RSMOKE,
        "      failed：code_error\n",
        "      failed：code_error | not_executed  # 突变：词表漂移\n",
        ["test_docstring_matches_produced_set"],
    ),
    (
        "MUT-R2b 裸赋值形 classification 漂移（_reason 形词表/锁同源同漏=MED-1 病灶复活）",
        RSMOKE,
        '            _reason = "port_ambiguous"',
        '            _reason = "port_ambiguous_drift"  # 突变',
        ["test_docstring_matches_produced_set"],
    ),
    (
        "MUT-R2c 关键字喂参形（两通道全失明 ⇒ 总数对账锁必须逮到，否则 MED-1 对新形态重开）",
        RSMOKE,
        '                "skipped", _reason,\n'
        '                f"端口推导不出且反解未得唯一端口（{_pr}）：{_detail}——未探活，"\n'
        '                "按环境/推导缺口跳过（fail-closed，绝不把\'我们没探\'判成启动失败）",',
        '                status="skipped", classification=_reason,  # 突变：第三种喂参形\n'
        '                message=f"端口推导不出且反解未得唯一端口（{_pr}）：{_detail}——未探活，"\n'
        '                "按环境/推导缺口跳过（fail-closed，绝不把\'我们没探\'判成启动失败）",',
        ["test_docstring_matches_produced_set"],
    ),
    # ── l1_pipeline.py ──
    (
        "MUT-L2 workdir 写死回退（自定义 workdir 沙箱产出剥不动=归一静默失效复活）",
        L1P,
        '        p = re.sub(r"^.*?" + re.escape(_sandbox_workdir()) + "/", "", p)',
        '        p = re.sub(r"^.*?/workspace/", "", p)  # 突变：写死回退',
        ["test_custom_workdir_stripped"],
    ),
    (
        "MUT-L2b 模块缺失归一 sibling 写死回退（_norm_add 自定义 workdir 剥不动 ⇒ 垃圾模块名进 blocked_on_modules）",
        L1P,
        '        for prefix in (_sandbox_workdir() + "/", "./"):',
        '        for prefix in ("/workspace/", "./"):  # 突变：sibling 写死回退',
        ["test_norm_add_uses_config_workdir"],
    ),
    (
        "MUT-L2c 根 workdir 极性边回退（workdir=\"/\" ⇒ 空串前缀恒真=全量误剥复活）",
        L1P,
        '        wd = (get_config().sandbox.sandbox_remote_workdir or "/workspace").rstrip("/")\n'
        '        return wd or "/workspace"',
        '        return (get_config().sandbox.sandbox_remote_workdir or "/workspace").rstrip("/")  # 突变',
        ["test_root_only_workdir_falls_back"],
    ),
    (
        "MUT-R4 relpath 优先回退（~/workspace/proj 中段误剥 proj/src/… 错路径复活）",
        L1P,
        '                if not (_rp == ".." or _rp.startswith("../")):',
        '                if "/workspace/" not in _fpos and not (_rp == ".." or _rp.startswith("../")):  # 突变',
        ["test_mid_workspace_segment_relpathed"],
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
    files = (SHARED, DISPATCH, EXEC, SECSCAN, PLANCORE, NODES, RSMOKE, L1P)
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
