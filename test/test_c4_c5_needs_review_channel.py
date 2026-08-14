#!/usr/bin/env python3
"""30 号文批10 C-4+C-5 锁：needs_review 通道同批（test 半边判据改实跑 + 覆盖面截断机读化）。

- C-4：L1 收尾 needs_review 判据的 test 半边原读 harness 原始属性（`harness.test_command`
  非空即当"有测试"）——三个跳过出口（清单缺失 / npm 无 scripts.test / pytest rc=5
  零用例收集）恰是"给了命令却一次没跑"的中间态：l1_3_test_ok=True 放行 +
  needs_review 零标记（或错分类）⇒ 语义零覆盖而 brain 终态账干净。治法=判据改
  `_ran_test = test_cmd 且非 test_skipped`；跳过出口落机读 `test_skip_reason`
  （直接写共享枚举完整字面量——双复核 R1 hunter M：kind→reason 映射表是三处
  同步点，rc=5 分支实测被错分类成兜底 reason，已收编成判序第三档）；rc=5 消费
  既有 `test_no_tests_collected` 键。细分 reason 落共享常量 NEEDS_REVIEW_REASONS。
- C-5：`_cap_files` 截断原只打日志零机读键（本仓口径日志不算通道）⇒ 超 20 文件子任务
  第 21 个起编译/lint 全无却 PASS。治法=截断写 `details["coverage_capped"][kind]`，
  消费侧接既有 needs_review 通道（`needs_review="coverage_capped"`），不新造账。
- 共享：reason 集提 types.NEEDS_REVIEW_REASONS 单一事实源，runner 终态账改消费它
  （原字面二元组，新 reason 加了没人读=空账）。
"""
from __future__ import annotations

import sys

import pytest

from swarm.types import NEEDS_REVIEW_REASONS, FileScope, SubTask, SubTaskDifficulty, TaskHarness
from swarm.worker.l1_pipeline import (
    _cap_files,
    _compile_files,
    _lint_java,
    _lint_js_ts,
    _lint_python,
    run_l1_pipeline,
)

_DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"


def _subtask(harness: TaskHarness | None = None, writable=None) -> SubTask:
    kw = {}
    if harness is not None:
        kw["harness"] = harness
    return SubTask(
        id="st-c4c5", description="改 a.py", difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=writable or ["a.py"], readable=[]),
        intent="modify", **kw)


# ─── C-4：中间态「给了命令但运行时被跳过」必须打 needs_review ───

def test_manifest_missing_skip_marks_needs_review(tmp_path):
    """C-4 主实证面：harness 下发 npm test 而工程无 package.json → 测试被跳过，
    此前零标记。现在必须打细分 reason 且落在共享常量集里。"""
    (tmp_path / "a.py").write_text("x = 1\n")
    st = _subtask(TaskHarness(language="node", test_command="npm test"))
    ok, details = run_l1_pipeline(str(tmp_path), st, _DIFF, llm=None)
    assert details.get("test_skip_reason") == "test_skipped_manifest_missing"
    assert details.get("needs_review") == "test_skipped_manifest_missing", (
        "给了命令但一次没跑=语义零覆盖——原判据读 harness 原始属性当『有测试』（C-4 原病）")
    assert details["needs_review"] in NEEDS_REVIEW_REASONS, "reason 必须在共享枚举内"


def test_no_npm_script_skip_marks_needs_review(tmp_path):
    """C-4 第二中间态（W-7 出口）：package.json 在但无 scripts.test → 跳过必须标记。"""
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "package.json").write_text('{"name": "p", "scripts": {}}\n')
    st = _subtask(TaskHarness(language="node", test_command="npm test"))
    ok, details = run_l1_pipeline(str(tmp_path), st, _DIFF, llm=None)
    assert details.get("test_skip_reason") == "test_skipped_no_npm_script"
    assert details.get("needs_review") == "test_skipped_no_npm_script"
    assert details["needs_review"] in NEEDS_REVIEW_REASONS


def test_no_command_still_marks_no_test_or_verify(tmp_path):
    """回归：完全没给命令的出口 reason 不变（旧行为不破）。"""
    (tmp_path / "a.py").write_text("x = 1\n")
    ok, details = run_l1_pipeline(str(tmp_path), _subtask(), _DIFF, llm=None)
    assert details.get("needs_review") == "no_test_or_verify_commands"


def test_actually_ran_test_does_not_mark(tmp_path):
    """反方向锁：测试【真跑过】（命令适用且执行）→ 绝不打 needs_review（防冤报）。
    用解释器自带 pytest 跑一个真通过的用例——_ran_test 为 True 时判据整体不触发。"""
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    st = _subtask(TaskHarness(language="python",
                              test_command=f"{sys.executable} -m pytest -q"),
                  writable=["a.py", "test_ok.py"])
    ok, details = run_l1_pipeline(str(tmp_path), st, _DIFF, llm=None)
    assert details.get("test_cmd"), "前置：本轮必须有 test 命令"
    assert not details.get("test_skipped"), f"前置：测试必须真跑（{details.get('test_skipped')}）"
    assert details.get("needs_review") is None, \
        f"真跑过测试还打 needs_review=冤报: {details.get('needs_review')}"


def test_pytest_rc5_no_tests_collected_marks_needs_review(tmp_path):
    """C-4 第三中间态（双复核 R1 hunter M 坐实面）：pytest rc=5（命令适用但该目录
    零用例收集）此前写 test_skipped 却不写任何机读 reason ⇒ 被兜底错分类成
    no_test_or_verify_commands（reason 语义错位）。治法=消费既有
    test_no_tests_collected 键判到专属 reason（共享枚举内）。"""
    (tmp_path / "a.py").write_text("x = 1\n")
    st = _subtask(TaskHarness(language="python",
                              test_command=f"{sys.executable} -m pytest -q"))
    ok, details = run_l1_pipeline(str(tmp_path), st, _DIFF, llm=None)
    assert details.get("test_no_tests_collected"), (
        f"前置：本夹具必须真触发 rc=5 分支: skipped={details.get('test_skipped')}")
    assert details.get("needs_review") == "test_skipped_no_tests_collected", (
        f"rc=5 零用例收集必须分类到专属 reason 而非兜底: {details.get('needs_review')}")
    assert details["needs_review"] in NEEDS_REVIEW_REASONS


def test_unmapped_skip_reason_warns_and_falls_back(tmp_path, monkeypatch, caplog):
    """枚举外 reason 反向锁：写侧若产出 NEEDS_REVIEW_REASONS 之外的字面量，
    收尾判据必须 WARNING + 回落兜底 reason（fail-closed），绝不静默信任未知值。"""
    import logging

    import swarm.worker.l1_pipeline as l1p
    # 行为面构造：把共享常量临时缩掉目标项 ⇒ 写侧照写原值而判据视作枚举外值，
    # 等价于"未来有人写了新 reason 忘了登记"的形态（生产无此路径，缩枚举即夹具）。
    monkeypatch.setattr(l1p, "NEEDS_REVIEW_REASONS",
                        tuple(r for r in NEEDS_REVIEW_REASONS
                              if r != "test_skipped_manifest_missing"))
    (tmp_path / "a.py").write_text("x = 1\n")
    st = _subtask(TaskHarness(language="node", test_command="npm test"))
    with caplog.at_level(logging.WARNING):
        ok, details = run_l1_pipeline(str(tmp_path), st, _DIFF, llm=None)
    assert details.get("test_skip_reason") == "test_skipped_manifest_missing", "前置：跳过出口仍写原值"
    assert details.get("needs_review") == "no_test_or_verify_commands", (
        "枚举外 reason 必须回落兜底（fail-closed），不得原样信任")
    assert any("NEEDS_REVIEW_REASONS" in r.message for r in caplog.records), (
        "枚举外 reason 必须打 WARNING——静默回落=写侧新 reason 漏登记无人发现")


# ─── C-5：_cap_files 截断落机读键 + 四调用点接线 ───

def test_cap_files_writes_coverage_capped(monkeypatch):
    """C-5 单元面：截断时 details 落 coverage_capped[kind]={total, checked}；
    未超限时绝不写（防 always-emit 冤报）。"""
    monkeypatch.setenv("SWARM_WORKER_L1_MAX_FILES", "3")
    d: dict = {}
    out = _cap_files(["a.py", "b.py", "c.py", "d.py"], "py_compile", details=d)
    assert out == ["a.py", "b.py", "c.py"]
    assert d["coverage_capped"]["py_compile"] == {"total": 4, "checked": 3}
    d2: dict = {}
    assert _cap_files(["a.py"], "py_compile", details=d2) == ["a.py"]
    assert "coverage_capped" not in d2


def test_cap_files_details_is_required():
    """双复核 R1 hunter L2 折入锁：details 是必填 kw-only——未来新调用点漏传
    必须当场 TypeError（fail-loud），绝不允许默认 None 让截断账静默消失。"""
    with pytest.raises(TypeError):
        _cap_files(["a.py"], "py_compile")


@pytest.mark.parametrize("kind", ["py_compile", "pyflakes", "eslint", "checkstyle"])
def test_cap_call_site_threads_details(tmp_path, monkeypatch, kind):
    """C-5 接线锁：四个检查器调用点必须把 details 传到 _cap_files（少一个=该栈的
    截断照旧静默）。行为验证：各检查器跑一遍超限输入，details 必须落对应 kind 的账。
    工具缺席/失败不影响——cap 在命令构造/循环头已先落账。"""
    monkeypatch.setenv("SWARM_WORKER_L1_MAX_FILES", "3")
    d: dict = {}
    if kind == "py_compile":
        for i in range(4):
            (tmp_path / f"m{i}.py").write_text(f"x = {i}\n")
        _compile_files(str(tmp_path), [f"m{i}.py" for i in range(4)], details=d)
    elif kind == "pyflakes":
        import swarm.worker.l1_pipeline as l1p
        monkeypatch.setattr(l1p, "_find_ruff_bin", lambda: sys.executable)
        _lint_python(str(tmp_path), [f"m{i}.py" for i in range(4)], details=d)
    elif kind == "eslint":
        (tmp_path / ".eslintrc.json").write_text("{}\n")
        _lint_js_ts(str(tmp_path), [f"m{i}.js" for i in range(4)], details=d)
    else:
        _lint_java(str(tmp_path), [f"M{i}.java" for i in range(4)], details=d)
    assert d.get("coverage_capped", {}).get(kind) == {"total": 4, "checked": 3}, (
        f"{kind} 调用点没把 details 传到 _cap_files——该检查器截断照旧零机读键: {d}")


def test_coverage_capped_consumed_by_needs_review(tmp_path, monkeypatch):
    """C-5 消费面：测试真跑过（no_test_* 不触发）但覆盖面被截断 →
    needs_review='coverage_capped'（接既有通道，不新造账）。"""
    monkeypatch.setenv("SWARM_WORKER_L1_MAX_FILES", "3")
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    files = [f"m{i}.py" for i in range(4)] + ["test_ok.py"]
    for i in range(4):
        (tmp_path / f"m{i}.py").write_text(f"x = {i}\n")
    st = _subtask(TaskHarness(language="python",
                              test_command=f"{sys.executable} -m pytest -q"),
                  writable=files)
    diff = "".join(f"--- a/{f}\n+++ b/{f}\n@@ -1 +1 @@\n-x = {i}\n+x = {i + 1}\n"
                   for i, f in enumerate(files[:4]))
    ok, details = run_l1_pipeline(str(tmp_path), st, diff, llm=None)
    assert details.get("coverage_capped"), f"前置：覆盖面必须真被截断: {details.keys()}"
    assert not details.get("test_skipped"), "前置：测试必须真跑"
    assert details.get("needs_review") == "coverage_capped", (
        f"覆盖截断必须接 needs_review 通道: {details.get('needs_review')}")


# ─── 共享常量：runner 终态账消费面 ───

def test_runner_terminal_account_consumes_shared_constant():
    """runner._failed_machine_account 的 reason 白名单必须消费 NEEDS_REVIEW_REASONS
    共享常量——行为锁：每个共享 reason 的子任务都必须进终态未核验账
    （回到字面二元组=新 reason 静默漏账，本测试红）。"""
    from swarm.brain.runner import _failed_machine_account

    for reason in NEEDS_REVIEW_REASONS:
        state = {
            # l2_passed 缺席(None)=verify_l2 从未执行，终态未核验账才聚合
            "subtask_results": {
                "st-x": {"l1_details": {"needs_review": reason}},
            },
        }
        tu = _failed_machine_account("task-c4c5", state, "test_probe")
        nl = (tu.get("acceptance_unverified") or {}).get("nl_acceptance_only") or []
        assert "st-x" in nl, f"reason={reason} 未进终态未核验账——消费侧白名单漏项"


def test_runner_terminal_account_ignores_unknown_reason():
    """反方向锁：枚举外的 reason 不进终态账（白名单语义不松动——
    共享常量之外的字面量不被误收）。"""
    from swarm.brain.runner import _failed_machine_account

    state = {"subtask_results": {
        "st-x": {"l1_details": {"needs_review": "some_future_unregistered_reason"}}}}
    tu = _failed_machine_account("task-c4c5", state, "test_probe")
    nl = (tu.get("acceptance_unverified") or {}).get("nl_acceptance_only") or []
    assert "st-x" not in nl
