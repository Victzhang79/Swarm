"""批次3 治本回归（deep_read_findings/19_worker_flow_audit.md）：A2~A10。

A2：TDD GREEN 侧 124/126/_is_infra_failure 当 capability fail（RED 侧同事件却归 None）
    → brain 换模型重试本已正确的修复。修法=GREEN 侧同口径三态 None，消费侧标
    failing_test_infra_failure BLOCKED/transient。

A3：L1.3 test 闸无 scope 归属阶梯（build 有 upstream、lint 有 D33 划分）→ 兄弟子任务
    坏测试连坐本子任务 hard FAIL。修法=报错文件全在写权集外 → BLOCKED 交 owner。

A4：_failure_signature 建立在压缩后的 build_output 上（省略区间差异不可见）→ 收敛中的
    修复被误判 no-progress 早停。修法=签名改吃未压缩错误行集（机读键与人读展示分账）。

A5：包级/类级 BLOCKED 判据互斥消费（类级只在包级未命中时跑）+ 类级判据不剥 ANSI。
    修法=两判据都跑取并集；parse 入口剥 ANSI。

A6：trivial refusal 分支的 sync/produce 步不受保护，异常会覆盖 refusal 判决。
    修法=包 try，异常保底返回 refusal 标记的 WorkerOutput。

A7：_normalize_scope_create_files 跨层 import 在 try 外。修法=挪进 try。

A8：_parse_l1_result 弱自报裸子串判 fail（"no errors found" 误判）。修法=共用 _FAIL_WORD_RE。

A9：scope 归一化失败静默 return。修法=补 WARNING。

A10：refusal 循环无专属早停（verify 连续拒答烧满修复轮）。修法=连续 2 轮 refusal 计入早停。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _make_gate_executor():
    """构造最小 WorkerExecutor（intent=debug + failing_test_command）供 L1 闸单测。"""
    from swarm.types import (
        FileScope, SubTask, SubTaskDifficulty, TaskHarness, TaskIntent,
    )
    from swarm.worker.executor import WorkerExecutor

    st = SubTask(
        id="st-dbg", description="debug", difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=["a/A.java"], create_files=[]),
        intent=TaskIntent.DEBUG,
        harness=TaskHarness(failing_test_command="python -m pytest test_bug.py -q"),
    )
    return WorkerExecutor(subtask=st, project_path="/tmp/fake_project")


# ── A2：GREEN 侧 infra 三态 None ─────────────────────────────────────────────

def test_a2_green_124_timeout_returns_unknown():
    """沙箱/命令超时(124)：RED 侧同事件归 None 不计红证，GREEN 侧不得冒充'未修复'。"""
    ex = _make_gate_executor()
    with patch("swarm.worker.l1_pipeline._run_l1_command", return_value=(124, "killed")):
        ok, detail = ex._run_failing_test_gate("python -m pytest test_bug.py -q")
    assert ok is None, f"124 必须归三态 None（A2 infra 分流），实际: {ok}"
    assert "not counted as green" in detail


def test_a2_green_126_blacklist_returns_unknown():
    """命令层黑名单拦截(126)：同 124，归 None。"""
    ex = _make_gate_executor()
    with patch("swarm.worker.l1_pipeline._run_l1_command", return_value=(126, "blocked")):
        ok, _ = ex._run_failing_test_gate("python -m pytest test_bug.py -q")
    assert ok is None, f"126 必须归三态 None（A2 infra 分流），实际: {ok}"


def test_a2_green_infra_marker_returns_unknown():
    """输出命中 _is_infra_failure 标记（网络/工具瞬时故障）→ None，不判 capability。"""
    ex = _make_gate_executor()
    with patch(
        "swarm.worker.l1_pipeline._run_l1_command",
        return_value=(1, "Could not transfer artifact: connection refused by proxy"),
    ):
        ok, _ = ex._run_failing_test_gate("python -m pytest test_bug.py -q")
    assert ok is None, f"infra 标记必须归三态 None（A2），实际: {ok}"


def test_a2_green_real_failure_still_false():
    """真测试断言失败（非零退出且无 infra 标记）→ 仍判 False（不误放进 None）。"""
    ex = _make_gate_executor()
    with patch(
        "swarm.worker.l1_pipeline._run_l1_command",
        return_value=(1, "FAILED test_bug.py::test_x - AssertionError: boom"),
    ):
        ok, detail = ex._run_failing_test_gate("python -m pytest test_bug.py -q")
    assert ok is False, f"真失败不得被 infra 分流吞掉: {ok}"
    assert "exit_code=1" in detail


def test_a2_green_pass_still_true():
    ex = _make_gate_executor()
    with patch("swarm.worker.l1_pipeline._run_l1_command", return_value=(0, "1 passed")):
        ok, _ = ex._run_failing_test_gate("python -m pytest test_bug.py -q")
    assert ok is True


def test_a2_consumption_none_forces_l1_failed(monkeypatch):
    """★复核 C-1★：通用 L1 全过(l1_passed=True) + failing_test infra 抖动(None) →
    消费分支必须把 l1_passed 压成 False（None=未知≠通过）+ pipeline_blocked/transient
    三件套——治前保留 True=未验证的"修复"被 brain 计成功静默交付。"""
    import asyncio

    ex = _make_gate_executor()

    async def _noop(*a, **k):
        return None

    async def _fake_agent(*a, **k):
        return "SUMMARY: done"

    monkeypatch.setattr(ex, "_sync_from_sandbox", _noop)
    monkeypatch.setattr(ex, "_run_agent", _fake_agent)
    monkeypatch.setattr(ex, "_get_git_diff", lambda *a, **k: "")
    monkeypatch.setattr(ex, "_deterministic_l1_gate", lambda: (True, {}))
    monkeypatch.setattr(ex, "_run_failing_test_gate",
                        lambda cmd: (None, "infra/timeout, raw exit=124"))
    monkeypatch.setattr(ex, "_rollback_failed_manifest_footprint", lambda *a, **k: None)

    out = asyncio.run(ex._phase_produce(True, {}, None))
    assert out.l1_passed is False, "infra 未知不得保留 l1_passed=True（C-1 fail-open）"
    det = out.l1_details
    assert det.get("pipeline_blocked") == "failing_test_infra_failure"
    assert det.get("not_run_kind") == "blocked"
    assert det.get("failure_class") == "transient"
    assert det.get("debug_failing_test_passed") is None


# ── A3：test 闸 scope 归属阶梯 ────────────────────────────────────────────────

_PYTEST_SIBLING_FAIL = """\
============================= test session starts ==============================
collected 12 items
tests/test_alarm.py F
=============================== FAILURES ===================================
______________________________ test_alarm_send ______________________________
tests/test_alarm.py:12: in test_alarm_send
    assert send("x") == 1
src/alarm/sender.py:40: in send
    raise RuntimeError("boom")
RuntimeError: boom
=========================== 1 failed, 11 passed ============================
"""


def test_a3_pytest_failed_files_extracted():
    """A3 前提：pytest 整树输出必须能抽出 .py 报错文件（_ERR_FILE_RE 补 py 前恒空→阶梯永不触发）。"""
    from swarm.worker.l1_pipeline import _build_error_files

    files = _build_error_files(_PYTEST_SIBLING_FAIL)
    assert "tests/test_alarm.py" in files, f"FAILED 行测试文件必须入集: {sorted(files)}"
    assert "src/alarm/sender.py" in files, f"traceback 源文件必须入集: {sorted(files)}"


def test_a3_sibling_test_failure_attributed_upstream():
    """报错文件全在写权集外（兄弟子任务的测试+源码）→ 判 upstream（BLOCKED 交 owner），不连坐。"""
    from swarm.types import FileScope
    from swarm.worker.l1_pipeline import _build_error_is_upstream

    scope = FileScope(writable=["src/notify/sender.py"], create_files=[])
    ev: dict = {}
    assert _build_error_is_upstream(
        _PYTEST_SIBLING_FAIL, "python -m pytest -q --maxfail=1",
        modified=["src/notify/sender.py"], scope=scope, evidence_out=ev,
    ), "兄弟坏测试连坐必须判 upstream（A3 归属阶梯）"
    assert ev.get("channel") == "scope"


def test_a3_own_test_failure_not_upstream():
    """报错文件含写权集内文件（自己该修的失败）→ 绝不揽 upstream（fail-open 不误放）。"""
    from swarm.types import FileScope
    from swarm.worker.l1_pipeline import _build_error_is_upstream

    out = _PYTEST_SIBLING_FAIL.replace("src/alarm/sender.py", "src/notify/sender.py")
    scope = FileScope(writable=["src/notify/sender.py"], create_files=[])
    assert not _build_error_is_upstream(
        out, "python -m pytest -q", modified=["src/notify/sender.py"], scope=scope,
    ), "写权集内的报错文件必须判自己修（不误 BLOCKED）"


def test_a3_unparseable_output_not_upstream():
    """提取不到报错文件的输出（如纯断言摘要）→ 不揽 upstream，保留旧 hard FAIL 行为。"""
    from swarm.types import FileScope
    from swarm.worker.l1_pipeline import _build_error_is_upstream

    scope = FileScope(writable=["src/notify/sender.py"], create_files=[])
    assert not _build_error_is_upstream(
        "1 failed, 11 passed", "python -m pytest -q",
        modified=["src/notify/sender.py"], scope=scope,
    )


def _run_pipeline_with_test_out(tmp_path, monkeypatch, test_out: str):
    """公共骨架：run_l1_pipeline 跑到 L1.3 test 闸，_run_l1_command 按命令分流。"""
    import swarm.worker.l1_pipeline as _lp
    from swarm.types import (
        FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskHarness,
    )

    src = tmp_path / "src/notify"
    src.mkdir(parents=True)
    (src / "sender.py").write_text("def notify(x):\n    return 'ok'\n")

    def _fake_run(cmd, *a, **k):
        if "pytest" in str(cmd):
            return (1, test_out)
        return (0, "")

    monkeypatch.setattr(_lp, "_run_l1_command", _fake_run)
    st = SubTask(
        id="st-t", description="x", difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(writable=["src/notify/sender.py"]),
        harness=TaskHarness(test_command="python -m pytest -q"),
    )
    diff = (
        "diff --git a/src/notify/sender.py b/src/notify/sender.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/notify/sender.py\n"
        "+++ b/src/notify/sender.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    return _lp.run_l1_pipeline(str(tmp_path), st, diff=diff)


def test_a3_own_regression_pure_assertion_not_upstream(tmp_path, monkeypatch):
    """★复核 H-1★：自己的改动打破 baseline 测试（纯断言失败、traceback 只有测试
    文件帧）→ 绝不甩锅 upstream（治前标 BLOCKED 烧满阶梯，正确归因=自己 hard FAIL
    拿 test_output 修回归）。"""
    pure_assertion = (
        "=============================== FAILURES ===================================\n"
        "______________________________ test_notify ______________________________\n"
        "tests/test_notify.py:20: in test_notify\n"
        "    assert notify('x') == 'ok'\n"
        "AssertionError\n"
        "=========================== 1 failed ============================\n"
    )
    ok, details = _run_pipeline_with_test_out(tmp_path, monkeypatch, pure_assertion)
    assert ok is False, "自己的回归必须 hard FAIL 交修复轮（不甩锅 upstream）"
    assert details.get("pipeline_blocked") != "upstream_module_broken", details
    assert details.get("l1_3_test_ok") is False


def test_a3_sibling_source_frame_triggers_upstream(tmp_path, monkeypatch):
    """报错文件含写权集外【源码帧】（兄弟产物的源码）→ 归属阶梯正常触发 BLOCKED，
    且补 blocked_on_modules 账（复核 MEDIUM，brain _producers_of 消费通道）。"""
    sibling_src = _PYTEST_SIBLING_FAIL  # 含 tests/test_alarm.py + src/alarm/sender.py 帧
    ok, details = _run_pipeline_with_test_out(tmp_path, monkeypatch, sibling_src)
    assert ok is True, "BLOCKED 契约：ok=True + pipeline_blocked 置位"
    assert details.get("pipeline_blocked") == "upstream_module_broken", details
    assert details.get("l1_3_test_ok") is None
    assert "src/alarm/sender.py" in (details.get("blocked_on_files") or [])
    assert "blocked_on_modules" in details, "模块粒度账必须落（brain 生产者反查通道）"


def test_a3_fixture_helper_frames_not_upstream(tmp_path, monkeypatch):
    """★复核 R2-1★：失败经 fixture/helper 链爆发（traceback 只有 tests/helpers.py +
    tests/test_notify.py + conftest.py 帧，无真源码帧）→ 测试辅助帧不得误当源码帧
    启用阶梯甩锅（治前 _is_test_file_path 相对路径盲区让其整体漏判）。"""
    fixture_fail = (
        "=============================== FAILURES ===================================\n"
        "______________________________ test_notify ______________________________\n"
        "tests/test_notify.py:20: in test_notify\n"
        "    assert make_notifier('x').send() == 'ok'\n"
        "tests/helpers.py:33: in make_notifier\n"
        "    return Notifier(cfg)\n"
        "tests/conftest.py:12: in cfg\n"
        "    return load()\n"
        "AssertionError\n"
        "=========================== 1 failed ============================\n"
    )
    ok, details = _run_pipeline_with_test_out(tmp_path, monkeypatch, fixture_fail)
    assert ok is False, "fixture/helper 帧独占的失败必须归自己（R2-1 甩锅通道）"
    assert details.get("pipeline_blocked") != "upstream_module_broken", details
    assert details.get("l1_3_test_ok") is False


def test_a3_external_library_frames_not_upstream(tmp_path, monkeypatch):
    """★复核 R3-1（hunter R3 实跑复现）★：失败在第三方库内爆发（traceback = 测试帧 +
    site-packages 库帧，零工程源码帧——requests/pandas 库密集项目常态）→ 库帧无 plan
    内 owner 可交，必须归自己 hard FAIL；治前库帧被误当"写权集外源码帧"启用阶梯甩锅。
    且 blocked_on_files 不得携带库帧噪声（该路径不触发阶梯时本来不落账，双保险断言
    _build_error_files 层面剔除）。"""
    lib_fail = (
        "=============================== FAILURES ===================================\n"
        "______________________________ test_notify ______________________________\n"
        "tests/test_notify.py:20: in test_notify\n"
        "    resp = requests.get(bad_url)\n"
        "venv/lib/python3.11/site-packages/requests/api.py:73: in get\n"
        "    return request('get', url, **kwargs)\n"
        "venv/lib/python3.11/site-packages/requests/sessions.py:589: in request\n"
        "    raise InvalidURL('bad')\n"
        "requests.exceptions.InvalidURL: bad\n"
        "=========================== 1 failed ============================\n"
    )
    ok, details = _run_pipeline_with_test_out(tmp_path, monkeypatch, lib_fail)
    assert ok is False, "第三方库帧独占的失败必须归自己（R3-1 甩锅通道）"
    assert details.get("pipeline_blocked") != "upstream_module_broken", details
    assert details.get("l1_3_test_ok") is False


def test_a3_external_frames_stripped_from_error_files():
    """R3-1 单元面：_build_error_files 必须剔除项目外帧（安装目录段），工程帧保留。"""
    from swarm.worker.l1_pipeline import _build_error_files

    out = (
        "tests/test_a.py:1: in t\n    pass\n"
        "venv/lib/python3.11/site-packages/requests/api.py:73: in get\n    pass\n"
        "/usr/lib/python3.11/json/__init__.py:10: in loads\n    pass\n"
        "frontend/node_modules/react/index.js:5: x\n"
        "src/alarm/sender.py:40: in send\n    raise RuntimeError\n"
        "/go/pkg/mod/github.com/x/y@v1.0.0/z.go:9: y\n"
        "/home/u/.cargo/registry/src/index.crates.io/serde-1.0/src/de.rs:9: z\n"
        "/home/u/.m2/repository/com/x/y/1.0/Y.java:9: w\n"
    )
    files = _build_error_files(out)
    assert "tests/test_a.py" in files
    assert "src/alarm/sender.py" in files
    assert not any("site-packages" in f or "node_modules" in f for f in files), files
    assert not any(f.startswith("usr/lib/") for f in files), files
    assert not any("pkg/mod/" in f or "registry" in f or ".m2/" in f for f in files), files


# ── A4：失败签名与压缩展示分账 ───────────────────────────────────────────────

def test_a4_extract_error_lines_uncompressed():
    """机读键必须含【压缩展示会省略掉】的错误行（A4 病根=签名吃压缩后展示串）。"""
    from swarm.worker.output_compress import compress_tool_output, extract_error_lines

    filler = "\n".join(f"noise line {i}" for i in range(200))
    raw = f"head\nERROR src/a.py:1 boom A\n{filler}\nERROR src/b.py:2 boom B\ntail"
    shown = compress_tool_output(raw, max_chars=200)
    assert "省略" in shown, "前提：展示串确实压缩省略了中段"
    lines = extract_error_lines(raw)
    assert lines == ["ERROR src/a.py:1 boom A", "ERROR src/b.py:2 boom B"], lines


def test_a4_signature_uses_machine_keys_not_display():
    """差异只落在压缩省略区时：旧行为同签名误杀 no-progress；新行为（机读键）必须异签名。"""
    from swarm.worker.executor_l1gate import _L1GateMixin

    filler = "\n".join(f"noise line {i}" for i in range(300))
    raw_r1 = f"ERROR src/a.py:1 boom A\nERROR src/b.py:2 boom B\n{filler}"
    raw_r2 = f"ERROR src/b.py:2 boom B\n{filler}"  # 本轮修掉了 a.py 的错=有真进展
    from swarm.worker.output_compress import compress_tool_output
    d1 = {"build_output": compress_tool_output(raw_r1, max_chars=200),
          "build_error_lines": ["ERROR src/a.py:1 boom A", "ERROR src/b.py:2 boom B"]}
    d2 = {"build_output": compress_tool_output(raw_r2, max_chars=200),
          "build_error_lines": ["ERROR src/b.py:2 boom B"]}
    assert _L1GateMixin._failure_signature(d1) != _L1GateMixin._failure_signature(d2), \
        "真进展（错误集合收缩）必须产生异签名（A4）"


def test_a4_signature_stable_across_omission_jitter():
    """同一失败、省略行数抖动：机读键相同 → 签名稳定（不误判有进展反复烧修复轮）。"""
    from swarm.worker.executor_l1gate import _L1GateMixin

    d1 = {"build_error_lines": ["ERROR src/a.py:1 boom"],
          "build_output": "head\nERROR src/a.py:1 boom\n... [省略 100 行] ...\ntail"}
    d2 = {"build_error_lines": ["ERROR src/a.py:1 boom"],
          "build_output": "head\nERROR src/a.py:1 boom\n... [省略 88 行] ...\ntail"}
    assert _L1GateMixin._failure_signature(d1) == _L1GateMixin._failure_signature(d2)


def test_a4_signature_fallback_display_keys_strip_placeholder():
    """无机读键（旧 checkpoint/老调用方）→ 回退展示键且占位行数字被剥，行为不回归。"""
    from swarm.worker.executor_l1gate import _L1GateMixin

    d1 = {"build_output": "ERROR x\n... [省略 100 行] ..."}
    d2 = {"build_output": "ERROR x\n... [省略 42 行] ..."}
    assert _L1GateMixin._failure_signature(d1) == _L1GateMixin._failure_signature(d2)
    assert _L1GateMixin._failure_signature({"build_output": "ERROR x"}) != \
        _L1GateMixin._failure_signature({"build_output": "ERROR y"})


# ── A5：BLOCKED 证据并集 + 剥 ANSI ───────────────────────────────────────────

def test_a5_parse_missing_symbol_classes_strips_ansi():
    """带色/并行构建输出（ANSI 转义插进三行组）必须仍能解析出类级证据。"""
    from swarm.worker.l1_parse import parse_missing_symbol_classes

    colored = (
        "\x1b[1;31m[ERROR]\x1b[m src/A.java:[12,45] cannot find symbol\n"
        "\x1b[1;31m[ERROR]\x1b[m   symbol:   class ISysGoogleAuthService\n"
        "\x1b[1;31m[ERROR]\x1b[m   location: package com.ruoyi.system.service\n"
    )
    assert parse_missing_symbol_classes(colored) == [
        ("ISysGoogleAuthService", "com.ruoyi.system.service")], \
        "ANSI 未剥会让类级判据静默失配（A5）"


def test_a5_blocked_evidence_union(tmp_path, monkeypatch):
    """★A5 主治★「缺内部包 P1 + 缺包在树类 P2.C」同现：包级与类级判据都跑，
    blocked_on_packages=并集、blocked_on_classes 不丢 C（治前互斥消费丢类级证据）。"""
    import swarm.worker.l1_pipeline as _lp
    from swarm.types import (
        FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskHarness,
    )

    # 树：com.x.service 包在（有 Other），但 IGoogleService 未建；com.x.dto 整包未建
    svc = tmp_path / "src/main/java/com/x/service"
    svc.mkdir(parents=True)
    (svc / "Other.java").write_text(
        "package com.x.service;\npublic interface Other {}\n")
    (tmp_path / "pom.xml").write_text("<project/>\n")

    combined = (
        "[ERROR] src/main/java/com/x/web/C2.java:[5,10] package com.x.dto does not exist\n"
        "[ERROR] src/main/java/com/x/web/C2.java:[12,45] cannot find symbol\n"
        "[ERROR]   symbol:   class IGoogleService\n"
        "[ERROR]   location: package com.x.service\n"
    )
    monkeypatch.setattr(_lp, "_project_own_packages", lambda *a, **k: {"com.x"})
    monkeypatch.setattr(_lp, "_run_l1_command", lambda *a, **k: (1, combined))
    monkeypatch.setattr(_lp, "_enforce_parent_version_literals", lambda *a, **k: (0, []))
    monkeypatch.setattr(_lp, "_enforce_dep_legality", lambda *a, **k: (0, []))

    st = SubTask(
        id="st-1", description="x", difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(writable=["src/main/java/com/x/web/C2.java"]),
        harness=TaskHarness(build_command="mvn compile"),
    )
    diff = (
        "diff --git a/src/main/java/com/x/web/C2.java b/src/main/java/com/x/web/C2.java\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/main/java/com/x/web/C2.java\n"
        "+++ b/src/main/java/com/x/web/C2.java\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    ok, details = _lp.run_l1_pipeline(str(tmp_path), st, diff=diff)
    assert ok is True, "BLOCKED 契约：ok=True + pipeline_blocked 置位"
    assert details.get("pipeline_blocked") == "internal_pkg_not_built", details
    assert details.get("blocked_on_packages") == ["com.x.dto", "com.x.service"], \
        f"包级∪类级证据必须并集（治前只含 com.x.dto）: {details.get('blocked_on_packages')}"
    assert details.get("blocked_on_classes") == ["com.x.service.IGoogleService"], \
        f"类级证据丢失=brain 侧 futile 判据拿不到 C（A5 死因）: {details.get('blocked_on_classes')}"


# ── A6：refusal 分支 produce 步异常保护 ──────────────────────────────────────

def test_a6_refusal_produce_exception_preserves_verdict(monkeypatch):
    """★A6 主治★ refusal 判死后 produce agent 再抛异常 → 保底 WorkerOutput 必须
    保 l1_decision_source=refusal_hard_fail（不劣化成通用执行异常丢 force_strong 信号）。"""
    import asyncio

    import swarm.worker.executor as _ex_mod
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality
    from swarm.worker.executor import WorkerExecutor

    st = SubTask(
        id="st-t", description="改一行", difficulty=SubTaskDifficulty.TRIVIAL,
        modality=SubTaskModality.TEXT,
        scope=FileScope(writable=["a/A.java"]),
    )
    ex = WorkerExecutor(subtask=st, project_path="/tmp/fake_project")

    async def _fake_run_agent(prompt, step=None, **kw):
        if step == "trivial-combined":
            return "Sorry, need more steps"  # 拒答标记
        if step == "produce":
            raise RuntimeError("sandbox flaked")  # 产出步沙箱抖动
        return ""

    async def _noop_sync(*a, **k):
        return None

    monkeypatch.setattr(ex, "_run_agent", _fake_run_agent)
    monkeypatch.setattr(ex, "_sync_from_sandbox", _noop_sync)
    # 主模型即最强（routing_complex 为空）→ 不走最强模型内部重试分支
    monkeypatch.setattr(
        _ex_mod, "get_config",
        lambda: type("C", (), {"model": type("M", (), {"routing_complex": None})()})(),
    )

    out = asyncio.run(ex._run_trivial_fast())
    assert out.l1_passed is False
    assert out.l1_details.get("l1_decision_source") == "refusal_hard_fail", \
        f"refusal 判决必须保留（A6）: {out.l1_details}"
    assert "raw_refusal" in out.l1_details
    assert "sandbox flaked" in out.summary


# ── A7/A9：scope 归一化降级面 ────────────────────────────────────────────────

def test_a7_brain_import_failure_survives(tmp_path, monkeypatch):
    """★A7★ brain 模块不可导入时归一化不再炸——fail-open 跳过测试文件剔除 + WARNING。"""
    import builtins

    from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality
    from swarm.worker.executor import WorkerExecutor

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("x")
    real_import = builtins.__import__

    def _boom(name, *a, **k):
        if name.startswith("swarm.brain"):
            raise ImportError("brain module unavailable")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _boom)
    st = SubTask(
        id="st-a7", description="修改 A.java 加一个字段",
        difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
        scope=FileScope(writable=["src/A.java"], create_files=["tests/test_a.py"]),
    )
    ex = WorkerExecutor(subtask=st, project_path=str(tmp_path))  # 治前此处 ImportError 炸构造
    assert any("测试文件剔除失败" in e for e in ex.execution_log), \
        "import 失败必须 WARNING 留痕（fail-closed 可观测纪律）"


def test_a9_frozen_scope_normalization_warns(tmp_path, monkeypatch):
    """★A9★ scope 不可变（赋值抛异常）→ 归一化放弃但必须 WARNING（治前静默 return）。"""
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality
    from swarm.worker.executor import WorkerExecutor

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "A.java").write_text("// exists")
    st = SubTask(
        id="st-a9", description="改代码",
        difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
        scope=FileScope(writable=[], create_files=["src/A.java"]),
    )
    ex = WorkerExecutor(subtask=st, project_path=str(tmp_path))
    scope = ex.effective_scope

    class _Frozen:
        def __setattr__(self, k, v):
            raise AttributeError("frozen")

    frozen = _Frozen()
    object.__setattr__(frozen, "writable", [])
    object.__setattr__(frozen, "create_files", ["src/A.java"])
    monkeypatch.setattr(ex, "effective_scope", frozen)
    ex._normalize_scope_create_files()
    assert any("scope 归一化失败" in e for e in ex.execution_log), \
        "归一化失败静默 return 违反降级可观测纪律（A9）"


# ── A8：弱自报共用 _FAIL_WORD_RE ─────────────────────────────────────────────

def test_a8_no_errors_found_not_misread_as_fail():
    """★A8★ "no errors found" 含裸子串 error——治前误判 fail 多烧一轮修复轮。"""
    from swarm.worker.executor_l1gate import _L1GateMixin

    passed, details = _L1GateMixin._parse_l1_result(
        None, "Verification complete: no errors found, 0 failures. 编译通过，测试通过。")
    assert passed is True, f"'no errors found' 不得误读为 fail: {details}"
    assert details["llm_self_report"] == "pass"


def test_a8_real_failure_words_still_fail():
    """真失败词（词边界命中）仍判 fail——共用正则不得软化判定。"""
    from swarm.worker.executor_l1gate import _L1GateMixin

    passed, _ = _L1GateMixin._parse_l1_result(None, "3 tests failed, build error at line 5")
    assert passed is False
    passed2, _ = _L1GateMixin._parse_l1_result(None, "验证失败：编译错误")
    assert passed2 is False


def test_a8_cjk_adjacent_english_fail_word(monkeypatch):
    """★复核 M-1★：中英混排"仍有error"——Python \\b 把 CJK 算 \\w 致左词边界失配，
    CJK 邻接补丁正则必须兜住（治前漏判 → det_ok=None 时幻觉 PASS）。"""
    from swarm.worker.executor_l1gate import _L1GateMixin

    passed, _ = _L1GateMixin._parse_l1_result(None, "编译通过，但仍有error")
    assert passed is False, "CJK 邻接英文失败词必须判 fail（M-1）"


def test_a8_zero_failed_summary_not_misread():
    """★复核 LOW-1★：pytest 标准摘要 "12 passed, 0 failed" 不得误读 fail。"""
    from swarm.worker.executor_l1gate import _L1GateMixin

    passed, _ = _L1GateMixin._parse_l1_result(None, "12 passed, 0 failed")
    assert passed is True


def test_a8_negation_strip_does_not_hide_real_failure():
    """否定剥离只作用"数量词+失败名词"紧邻形态——真失败句不得被剥成假过。"""
    from swarm.worker.executor_l1gate import _L1GateMixin

    passed, _ = _L1GateMixin._parse_l1_result(
        None, "0 failures expected but 3 errors occurred")
    assert passed is False


# ── A10：refusal 连续两轮计入早停 ────────────────────────────────────────────

def test_a10_consecutive_refusal_early_stops(monkeypatch):
    """★A10★ det_ok=None 形态 verify agent 连续拒答：治前烧满 fix 轮（max_fix_rounds+1 次
    verify 调用），治后第 2 轮即早停（verify 调用恰 2 次、fix 步 0 次）。"""
    import asyncio

    from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality
    from swarm.worker.executor import WorkerExecutor

    st = SubTask(
        id="st-a10", description="x",
        difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
        scope=FileScope(writable=["a/A.java"]),
    )
    ex = WorkerExecutor(subtask=st, project_path="/tmp/fake_project")
    ex.max_fix_rounds = 5
    calls: list[str] = []

    async def _fake_run_agent(prompt, step=None, **kw):
        calls.append(step or "?")
        return "Sorry, need more steps"  # 每轮拒答

    monkeypatch.setattr(ex, "_run_agent", _fake_run_agent)
    monkeypatch.setattr(ex, "_deterministic_l1_gate", lambda: (None, {}))

    l1_passed, _, _ = asyncio.run(ex._phase_verify_loop())
    assert l1_passed is False
    verify_calls = [s for s in calls if s.startswith("verify-")]
    fix_calls = [s for s in calls if s.startswith("fix-")]
    assert len(verify_calls) == 2, \
        f"连续 2 轮拒答必须早停（治前跑满 {ex.max_fix_rounds + 1} 轮）: {calls}"
    assert len(fix_calls) <= 1, \
        f"早停后不得再烧 fix 步（首轮拒答的 fix-0 是设计内代价）: {calls}"
