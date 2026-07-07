"""P0-6 治本行为测试：D11 / D12 / D36 / D37（worker sync/verdict 层）。

- D11：bootstrap 上传抛 TransientInfraError → _phase_prepare 向上传播（transient 退避），
       绝不被宽 except 吞成"降级本地"→在缺文件沙箱空跑。
- D12：验证循环确定性 PASS 后 Phase-4 det_ok=None（超预算/异常）→ evaluate_l1 维持 passed=True，
       不翻成 verification_not_run(False)（否则整份完成工作被 oversize 拆小重做）。
- D36：worker 在沙箱改【上下文兄弟文件】(readable)→ pull-back 用 bootstrap 标记 mtime 圈出、
       并入回传+_repaired_extra_paths（进 diff），杜绝沙箱绿但改动不落盘→cannot find symbol。
- D37：(a) 全树枚举去 head-200 硬顶、截断可观测；(b) 未声明新文件补捞按【声明目录】精确枚举
       (-maxdepth 1)，不再全树 find|head 前 N（烤源沙箱数千文件下漏新建）。

均为行为断言（断言产出/回传清单/命令形态），不 inspect.getsource；纯方法，不触真沙箱/网络。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from swarm.models.errors import TransientInfraError, classify_failure
from swarm.types import FileScope, SubTask, TaskHarness
from swarm.worker.executor import WorkerExecutor
from swarm.worker import executor_sync as _sync
from swarm.worker.l1_verdict import L1Verdict, evaluate_l1
from swarm.types import NotRunKind


# ───────────────────────── 通用假沙箱管理器 ─────────────────────────
class _FakeManager:
    """记录 run_command / sync_files_from_sandbox 调用，按命令内容分派 stdout。"""

    def __init__(self, modified=None, under=None, workspace=None):
        self._modified = modified or []          # `find -newer` 返回（D36）
        self._under = under or []                # `_list_sandbox_files_under`（D37b）
        self._workspace = workspace or []        # 全树枚举（D37a/allow_any）
        self.run_cmds: list[str] = []
        self.pullback_rel_files: list[str] | None = None

    def run_command(self, sandbox, cmd, timeout=30):
        self.run_cmds.append(cmd)
        if "-newer" in cmd:
            out = "\n".join(self._modified)
        elif "-maxdepth 1" in cmd:
            out = "\n".join(self._under)
        elif "test -f" in cmd:
            out = "__N__"                          # 删除探测：沙箱里没有（不触发，无 delete scope）
        else:
            out = "\n".join(self._workspace)
        return SimpleNamespace(stdout=out, error=None)

    def sync_files_from_sandbox(self, sandbox, local_root, rel_files, remote):
        self.pullback_rel_files = list(rel_files)
        return {"contents": {r: f"// {r}\n" for r in rel_files},
                "downloaded": len(rel_files), "skipped": 0, "errors": []}

    def _preserve_line_endings(self, path, data):
        return data

    def append_activity(self, *a, **k):
        pass


def _mk_executor(tmp_path, scope: FileScope, harness=None) -> WorkerExecutor:
    st = SubTask(id="st-p06", description="x", scope=scope,
                 harness=harness or TaskHarness(language="java"))
    ex = WorkerExecutor(subtask=st, project_path=str(tmp_path))
    ex._resolve_project_stack = lambda: {}    # 关掉 jvm 命名空间归一（无关本测试）
    return ex


# ═══════════════════════════ D11 ═══════════════════════════
def test_d11_bootstrap_transient_propagates_not_degraded(tmp_path, monkeypatch):
    """bootstrap 上传抛 TransientInfraError → _phase_prepare 向上抛（不吞成降级本地空跑）。

    旧代码：TransientInfraError 落到 `except Exception` → 非 has_source 走"降级本地"→ 返回 None
    继续在缺文件沙箱执行。新代码：专门 `except TransientInfraError: raise` → 传播。"""
    ex = _mk_executor(
        tmp_path, FileScope(writable=["a.java"]),
        harness=TaskHarness(language="java", sandbox_template="tpl-x"),
    )
    ex.project_id = "proj-1"
    ex._sandbox_has_source = False  # 确认走的是【会降级】的分支（证明 transient 绕过降级）

    # 假 config：启用沙箱、关健康探活（简化创建路径）。
    fake_cfg = SimpleNamespace(sandbox=SimpleNamespace(
        use_for_worker=True, api_url="http://sandbox", sandbox_health_check=False,
        sandbox_health_retries=0, sandbox_remote_workdir="/workspace",
    ))
    monkeypatch.setattr("swarm.worker.executor.get_config", lambda: fake_cfg)

    fake_mgr = SimpleNamespace(
        create=lambda **kw: SimpleNamespace(sandbox_id="sb-1"),
        health_check=lambda sb: True,
        kill=lambda sid: None,
        append_activity=lambda *a, **k: None,
    )
    monkeypatch.setattr("swarm.worker.sandbox.get_sandbox_manager", lambda: fake_mgr)
    monkeypatch.setattr("swarm.worker.sandbox_pool.pool_enabled", lambda: False)
    monkeypatch.setattr("swarm.worker.sandbox_pool.get_sandbox_pool", lambda: None)
    monkeypatch.setattr("swarm.tools.build_tools.set_sandbox_context", lambda *a, **k: None)
    monkeypatch.setattr(ex, "_reset_scope_to_head", lambda: None)

    async def _boom(reason):
        raise TransientInfraError(f"sandbox upload failed ({reason}): envd 5xx")
    monkeypatch.setattr(ex, "_sync_to_sandbox", _boom)

    with pytest.raises(TransientInfraError):
        asyncio.run(ex._phase_prepare())


def test_d11_transient_classified_as_transient():
    """TransientInfraError 归类 transient（→ handle_failure 退避重试同模型，不计 capability 配额）。"""
    assert classify_failure(TransientInfraError("sandbox upload failed")) == "transient"


# ═══════════════════════════ D12 ═══════════════════════════
def test_d12_det_none_keeps_prior_pass():
    """★核心：det_ok=None 且 prior 已坐实 PASS → 维持 passed=True（不翻成 verification_not_run）。"""
    prior = L1Verdict(passed=True, source="deterministic", sticky=False,
                      reason="循环内确定性通过")
    v = evaluate_l1(det_ok=None, det_details={"not_run_kind": NotRunKind.BLOCKED.value,
                                              "error": "timeout_in_verifying"},
                    verify_result=None, llm_ok=True, prior=prior, phase="phase4_final")
    assert v.passed is True, f"det=None 不应翻转已坐实的 PASS，got {v}"
    assert v.details.get("l1_decision_source") == "verification_not_run_keep_prior_pass"


def test_d12_det_none_missing_kind_keeps_prior_pass():
    """not_run_kind 缺失（预算耗尽/diff 异常路径）+ prior PASS → 仍维持 PASS。"""
    prior = L1Verdict(passed=True, source="deterministic", sticky=False)
    v = evaluate_l1(det_ok=None, det_details={}, verify_result=None,
                    llm_ok=True, prior=prior, phase="phase4_final")
    assert v.passed is True


def test_d12_det_none_prior_fail_still_kept_fail():
    """回归：prior 为 fail 时 det=None 仍维持 fail（分支①不受影响）。"""
    prior = L1Verdict(passed=False, source="deterministic", sticky=True)
    v = evaluate_l1(det_ok=None, det_details={}, verify_result=None,
                    llm_ok=True, prior=prior, phase="phase4_final")
    assert v.passed is False
    assert v.details.get("l1_decision_source") == "verification_not_run_keep_prior"


def test_d12_det_none_no_prior_still_fail_closed():
    """回归：无 prior（或 prior.passed=None）+ BLOCKED → 仍 fail-closed transient（不误放通过）。"""
    v = evaluate_l1(det_ok=None, det_details={"not_run_kind": NotRunKind.BLOCKED.value},
                    verify_result="L1_RESULT: PASS", llm_ok=True, prior=None, phase="x")
    assert v.passed is False
    assert v.source == "verification_not_run"


# ═══════════════════════════ D36 ═══════════════════════════
def test_d36_modified_sibling_pulled_back_and_in_diff_set(tmp_path):
    """worker 在沙箱改了 readable 兄弟文件 → pull-back 纳入回传 + _repaired_extra_paths（进 diff）。"""
    scope = FileScope(writable=["src/com/Main.java"], readable=["src/com/Sibling.java"])
    ex = _mk_executor(tmp_path, scope)
    ex._bootstrap_marker = ".swarm_bootstrap_marker"
    # 沙箱 `find -newer` 报：Main（writable）+ Sibling（readable 兄弟被 sed 改）都变了。
    mgr = _FakeManager(modified=["src/com/Sibling.java", "src/com/Main.java"], under=[])
    ex._sandbox = SimpleNamespace(sandbox_id="sb")
    ex._sandbox_manager = mgr

    asyncio.run(ex._sync_from_sandbox("产出"))

    # Sibling 被识别为"被改的上下文兄弟"→ 进 _repaired_extra_paths（→ diff targets）
    assert "src/com/Sibling.java" in ex._repaired_extra_paths, ex._repaired_extra_paths
    # 且真的被 pull-back（回传清单含它）
    assert "src/com/Sibling.java" in (mgr.pullback_rel_files or []), mgr.pullback_rel_files
    assert "src/com/Main.java" in (mgr.pullback_rel_files or [])


def test_d36_out_of_context_modification_not_silently_included(tmp_path):
    """worker 改了【上下文集之外】的无关文件 → 不静默纳入回传（越界交 scope 闸门，非 D36 职责）。"""
    scope = FileScope(writable=["src/com/Main.java"], readable=["src/com/Sibling.java"])
    ex = _mk_executor(tmp_path, scope)
    ex._bootstrap_marker = ".swarm_bootstrap_marker"
    # 沙箱报改了一个既非 writable 也非 readable 的无关文件
    mgr = _FakeManager(modified=["src/other/Unrelated.java"], under=[])
    ex._sandbox = SimpleNamespace(sandbox_id="sb")
    ex._sandbox_manager = mgr

    asyncio.run(ex._sync_from_sandbox("产出"))

    assert "src/other/Unrelated.java" not in ex._repaired_extra_paths
    assert "src/other/Unrelated.java" not in (mgr.pullback_rel_files or [])


def test_d36_noop_without_marker(tmp_path):
    """无 bootstrap 标记（创建失败降级）→ D36 检测 no-op，不误加、不抛。"""
    scope = FileScope(writable=["src/com/Main.java"], readable=["src/com/Sibling.java"])
    ex = _mk_executor(tmp_path, scope)
    ex._bootstrap_marker = ""  # 标记创建失败
    mgr = _FakeManager(modified=["src/com/Sibling.java"], under=[])
    ex._sandbox = SimpleNamespace(sandbox_id="sb")
    ex._sandbox_manager = mgr
    asyncio.run(ex._sync_from_sandbox("产出"))
    assert "src/com/Sibling.java" not in ex._repaired_extra_paths


# ═══════════════════════════ D37 ═══════════════════════════
def test_d37a_workspace_list_truncation_warns_and_caps(tmp_path, monkeypatch):
    """全树枚举超上限 → 截断可观测（WARN）+ 返回恰好 cap 个（不静默丢在返回值里无声）。"""
    monkeypatch.setattr(_sync, "_WORKSPACE_LIST_CAP", 3)
    ex = _mk_executor(tmp_path, FileScope(allow_any=True))
    mgr = _FakeManager(workspace=["a.java", "b.java", "c.java", "d.java"])  # 4 > cap 3
    ex._sandbox = SimpleNamespace(sandbox_id="sb")
    ex._sandbox_manager = mgr
    ex.execution_log = []
    out = ex._list_sandbox_workspace_files()
    assert len(out) == 3, out
    assert any("上限" in line and "WARN" in line for line in ex.execution_log), ex.execution_log


def test_d37a_marker_file_excluded_from_workspace_list(tmp_path):
    """全树枚举排除 bootstrap 标记文件（否则 allow_any 会把内部标记当产物拉回本地）。"""
    ex = _mk_executor(tmp_path, FileScope(allow_any=True))
    mgr = _FakeManager(workspace=["A.java", ".swarm_bootstrap_marker", "B.java"])
    ex._sandbox = SimpleNamespace(sandbox_id="sb")
    ex._sandbox_manager = mgr
    out = ex._list_sandbox_workspace_files()
    assert ".swarm_bootstrap_marker" not in out
    assert out == ["A.java", "B.java"]


def test_d37b_new_file_capture_uses_targeted_dir_enumeration(tmp_path):
    """未声明新文件补捞按【声明目录】精确枚举(-maxdepth 1)，不再全树 find|head 前 N。"""
    ex = _mk_executor(tmp_path, FileScope(writable=["src/com/Main.java"]))
    mgr = _FakeManager(under=["src/com/Helper.java"])
    ex._sandbox = SimpleNamespace(sandbox_id="sb")
    ex._sandbox_manager = mgr
    out = ex._list_sandbox_files_under(["src/com"])
    assert out == ["src/com/Helper.java"]
    # 命令形态：目标目录 + -maxdepth 1（证明是定向枚举而非全树）
    last = mgr.run_cmds[-1]
    assert "src/com" in last and "-maxdepth 1" in last, last


def test_d37b_targeted_enum_empty_dirs_noop(tmp_path):
    """无声明目录 → 定向枚举直接返回空，不发命令。"""
    ex = _mk_executor(tmp_path, FileScope())
    mgr = _FakeManager()
    ex._sandbox = SimpleNamespace(sandbox_id="sb")
    ex._sandbox_manager = mgr
    assert ex._list_sandbox_files_under([]) == []
    assert mgr.run_cmds == []
