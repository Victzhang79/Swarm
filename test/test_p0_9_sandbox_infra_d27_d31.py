"""P0-9 沙箱基建五连修（DEEP_READ_REGISTER D27-D31）行为测试。

D27 clean_workspace 绝不删各栈工具链/镜像烤入的 warmup 依赖缓存（.cargo/.rustup/go/.m2/.npm/...），
    只清可再生派生缓存——两张清单各栈对称，非逐语言特赦。
D28 池借出前保证剩余远端寿命 ≥ 子任务预算：优先续期(set_timeout)，不支持则校验剩余寿命，
    不足绝不复用（新建）。
D29 池桶键用 create 时 _resolve_template 的【实际解析结果】而非请求 template_id——
    模板漂移时不把错语言镜像复用给下个子任务。
D30 pull-back >上限文件 = 确定性 skip：与 transient(skipped/errors) 分账(skipped_oversize)，
    L1 闸门对它判确定性 FAIL(走失败阶梯)而非 BLOCKED transient 无限重试；上限提为 8MiB 且
    SWARM_SANDBOX_MAX_SYNC_FILE_SIZE 可配。
D31 brain L2 沙箱验证走 run_command(shell 端点，语言镜像通用)，绝不用 run_code(Jupyter 端点，
    语言镜像必 502)；infra 失败(命令没跑成)返回 None 走既有降级路径，不误判测试失败。

全部 mock SandboxManager/run_command，不连真沙箱。
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality
from swarm.worker.executor import WorkerExecutor
from swarm.worker.sandbox import SandboxManager
from swarm.worker.sandbox_pool import HotSandboxPool


# ══════════════════════════════════════════════════════════════════
# D27 — clean_workspace 工具链/缓存两张清单
# ══════════════════════════════════════════════════════════════════

def _clean_cmd() -> str:
    """跑一次 clean_workspace，捕获下发的 shell 命令。"""
    captured = {}
    mgr = SandboxManager.__new__(SandboxManager)  # 不走 __init__（不连真服务）

    def fake_run_command(sandbox, cmd, timeout=30, _skip_blacklist=False):
        captured["cmd"] = cmd
        return MagicMock(success=True, stdout="WORKSPACE_CLEANED", error=None)

    mgr.run_command = fake_run_command
    ok = mgr.clean_workspace(MagicMock(sandbox_id="sid-d27"))
    assert ok is True
    return captured["cmd"]


def _home_rm_targets(cmd: str) -> set[str]:
    """解析 `for d in <dirs>; do rm -rf "$HOME/$d"` 的目录集合。"""
    m = re.search(r"for d in (.+?); do", cmd)
    assert m, f"clean_workspace 命令缺 $HOME 目录清理循环: {cmd}"
    return set(m.group(1).split())


def test_d27_clean_workspace_never_deletes_toolchain_dirs():
    """rm -rf $HOME/<d> 目标绝不含各栈工具链安装目录/warmup 依赖缓存（各栈对称）。

    依据 cube-templates/dockerfiles（$HOME=/root）：
      Rust: /root/.cargo(cargo/rustc 可执行+镜像源 config+warmup registry)、/root/.rustup(toolchain 本体)
      Go:   GOPATH=/root/go(go/bin 已装工具 + go/pkg/mod=warmup GOMODCACHE)
      Java: /root/.m2(settings.xml+warmup 依赖)、.gradle 同理
      Node: /root/.npm(warmup 填充的下载缓存)
      通用: .config(pip 等工具配置，配置非缓存)
    """
    dirs = _home_rm_targets(_clean_cmd())
    for preserved in (".cargo", ".rustup", "go", "go/pkg", ".m2", ".gradle", ".npm", ".config", ".config/pip"):
        assert preserved not in dirs, (
            f"clean_workspace 仍在删工具链/warmup 资产目录 $HOME/{preserved}（D27 复发）: {sorted(dirs)}"
        )


def test_d27_clean_workspace_still_cleans_regenerable_caches():
    """可再生派生缓存照清（防跨项目泄漏），workdir/tmp 清理保留。"""
    cmd = _clean_cmd()
    dirs = _home_rm_targets(cmd)
    for cleanable in (".cache", "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules"):
        assert cleanable in dirs, f"可清缓存 {cleanable} 不该从清理清单消失: {sorted(dirs)}"
    assert "/workspace" in cmd and "/tmp" in cmd
    assert ".bashrc" not in cmd


def test_d27_preserve_and_cleanable_lists_disjoint():
    """两张清单结构性不相交（防未来把工具链目录塞回可清清单）。"""
    from swarm.worker.sandbox import (
        SANDBOX_CLEANABLE_HOME_DIRS,
        SANDBOX_PRESERVE_HOME_DIRS,
    )
    cleanable_roots = {d.split("/", 1)[0] for d in SANDBOX_CLEANABLE_HOME_DIRS}
    assert not (cleanable_roots & set(SANDBOX_PRESERVE_HOME_DIRS)), (
        "可清清单与绝不删清单相交"
    )


# ══════════════════════════════════════════════════════════════════
# D28/D29 — 热池：剩余寿命 / 实际解析模板 桶键
# ══════════════════════════════════════════════════════════════════

class _FakeSandbox:
    _n = 0

    def __init__(self):
        _FakeSandbox._n += 1
        self.sandbox_id = f"sbx-{_FakeSandbox._n}"


class _FakePoolManager:
    """最小 FakeManager：create/kill/run_command/meta。可选 D28/D29 扩展按需挂。"""

    def __init__(self):
        self.created: list[_FakeSandbox] = []
        self.killed: list[str] = []
        self._meta: dict[str, dict] = {}

    def create(self, template_id=None, timeout=60, *, project_id=None, task_id=None, source="manual"):
        sbx = _FakeSandbox()
        self.created.append(sbx)
        self._meta[sbx.sandbox_id] = {"project_id": project_id, "task_id": task_id, "source": source}
        return sbx

    def kill(self, sandbox_id):
        self.killed.append(sandbox_id)

    def run_command(self, sandbox, cmd, timeout=120, **kw):
        return SimpleNamespace(success=True, stdout="ok", stderr="", error=None)

    def register_sandbox_meta(self, sid, **kw):
        self._meta[sid] = kw

    def get_sandbox_meta(self, sid):
        return self._meta.get(sid)


def _pool(mgr) -> HotSandboxPool:
    return HotSandboxPool(mgr, max_idle_per_template=2, max_total=8,
                          ttl_seconds=600, idle_seconds=300)


def test_d28_pool_discards_reused_sandbox_with_insufficient_remote_lifetime():
    """剩余远端寿命 < 子任务预算 且续期不可用 → 不复用（kill+新建）。"""
    mgr = _FakePoolManager()
    # 只有剩余寿命查询（续期不可用）：所有沙箱都只剩 100s，远小于 900s 预算
    mgr.remaining_lifetime = lambda sid: 100.0
    p = _pool(mgr)
    sb1 = p.acquire("tpl-java")
    p.release(sb1, reusable=True)
    sb2 = p.acquire("tpl-java")
    assert sb2.sandbox_id != sb1.sandbox_id, (
        "剩余寿命不足的池内沙箱被借出（D28 复发：子任务中途必被远端拆卸）"
    )
    assert sb1.sandbox_id in mgr.killed


def test_d28_pool_renews_lifetime_when_renewal_supported():
    """续期 API 可用 → 复用沙箱并先续期到子任务预算。"""
    mgr = _FakePoolManager()
    extend_calls: list[tuple[str, int]] = []

    def try_extend_lifetime(sandbox, seconds):
        extend_calls.append((sandbox.sandbox_id, int(seconds)))
        return True

    mgr.try_extend_lifetime = try_extend_lifetime
    mgr.remaining_lifetime = lambda sid: 100.0  # 若不续期本该被弃用
    p = _pool(mgr)
    sb1 = p.acquire("tpl-java")
    p.release(sb1, reusable=True)
    sb2 = p.acquire("tpl-java")
    assert sb2.sandbox_id == sb1.sandbox_id, "续期成功后应正常复用"
    assert extend_calls and extend_calls[-1][0] == sb1.sandbox_id, (
        "复用借出前未尝试续期（D28：借出即应保证剩余寿命 ≥ 子任务预算）"
    )
    assert extend_calls[-1][1] >= 120


def test_d28_pool_without_lifetime_api_keeps_old_reuse_behavior():
    """回归：manager 无寿命/续期接口（旧版/mock）→ 维持原复用行为，不误杀。"""
    mgr = _FakePoolManager()
    p = _pool(mgr)
    sb1 = p.acquire("tpl-java")
    p.release(sb1, reusable=True)
    sb2 = p.acquire("tpl-java")
    assert sb2.sandbox_id == sb1.sandbox_id


def test_d29_pool_bucket_keyed_by_actual_resolved_template():
    """java 模板漂移解析成 python 镜像 → 桶键用实际值；下个 java 请求绝不复用错语言镜像。"""
    mgr = _FakePoolManager()
    # 模拟 create 自愈重解析：请求 tpl-java，实际落到 tpl-python 镜像
    mgr.get_resolved_template = lambda sid: "tpl-python"
    p = _pool(mgr)
    sb1 = p.acquire("tpl-java")
    p.release(sb1, reusable=True)
    # 桶键必须是实际解析结果，不能挂在请求键 tpl-java 下
    with p._lock:
        assert "tpl-java" not in p._pool, (
            f"漂移镜像仍挂在请求模板桶下（D29 复发：java 子任务将复用无 JDK 镜像）: {list(p._pool)}"
        )
    sb2 = p.acquire("tpl-java")
    assert sb2.sandbox_id != sb1.sandbox_id, (
        "请求 tpl-java 复用到了实际为 tpl-python 的漂移镜像（D29 复发 → mvn 127）"
    )


def test_d29_pool_reuse_preserves_resolved_template_on_reborrow():
    """无漂移正常复用不受影响；再借出不把桶键改写回请求值。"""
    mgr = _FakePoolManager()
    mgr.get_resolved_template = lambda sid: "tpl-java"  # 解析结果与请求一致
    p = _pool(mgr)
    sb1 = p.acquire("tpl-java")
    p.release(sb1, reusable=True)
    sb2 = p.acquire("tpl-java")
    assert sb2.sandbox_id == sb1.sandbox_id
    with p._lock:
        assert p._template_by_sid.get(sb2.sandbox_id) == "tpl-java"


def test_d29_manager_records_resolved_template_and_deadline():
    """SandboxManager.create 回写实际解析模板 + 远端寿命 deadline（D28/D29 记账源）。"""
    mgr = SandboxManager.__new__(SandboxManager)
    mgr.config = SimpleNamespace(api_url="", api_key="", verify_ssl=False, default_template="tpl-cfg")
    mgr._instances, mgr._sandbox_meta, mgr._fail_counts = {}, {}, {}
    from collections import deque as _dq  # noqa: F401
    mgr._sandbox_activity = {}
    mgr._resolved_templates = {}
    mgr._sandbox_deadlines = {}

    fake_sb = MagicMock(sandbox_id="sbx-d29")
    with patch("e2b_code_interpreter.Sandbox") as SB, \
         patch.object(SandboxManager, "_resolve_template", return_value="tpl-actual"), \
         patch.object(SandboxManager, "append_activity"), \
         patch("swarm.audit.audit"):
        SB.create.return_value = fake_sb
        sb = mgr.create(template_id="tpl-req", timeout=900, project_id="proj")
    assert sb is fake_sb
    assert mgr.get_resolved_template("sbx-d29") == "tpl-actual"
    remaining = mgr.remaining_lifetime("sbx-d29")
    assert remaining is not None and 0 < remaining <= 900


# ══════════════════════════════════════════════════════════════════
# D30 — 超限文件确定性 skip 分账 + 上限可配
# ══════════════════════════════════════════════════════════════════

def test_d30_max_sync_file_size_default_raised_and_env_configurable(monkeypatch):
    """默认上限 ≥ 8MiB（1MiB 会确定性 skip package-lock.json 等合法产物），env 可调。"""
    import swarm.worker.sandbox as sbmod
    assert sbmod.MAX_SYNC_FILE_SIZE >= 8 * 1024 * 1024, (
        f"MAX_SYNC_FILE_SIZE={sbmod.MAX_SYNC_FILE_SIZE} 仍是旧 1MiB 级别（D30 复发）"
    )
    monkeypatch.setenv("SWARM_SANDBOX_MAX_SYNC_FILE_SIZE", "16777216")
    assert sbmod._env_max_sync_file_size() == 16 * 1024 * 1024
    monkeypatch.setenv("SWARM_SANDBOX_MAX_SYNC_FILE_SIZE", "not-a-number")
    assert sbmod._env_max_sync_file_size() == 8 * 1024 * 1024  # 坏值回退默认


def test_d30_targeted_pullback_accounts_oversize_separately(tmp_path):
    """>上限文件 → skipped_oversize/oversize_rels 单独记账，不再混入 transient skipped。"""
    import swarm.worker.sandbox as sbmod
    mgr = SandboxManager.__new__(SandboxManager)
    mgr.config = SimpleNamespace(sandbox_remote_workdir="/workspace")
    big = b"x" * (sbmod.MAX_SYNC_FILE_SIZE + 1)
    with patch("swarm.worker.sandbox.read_file_from_sandbox", return_value=big):
        stats = mgr.sync_files_from_sandbox(
            MagicMock(sandbox_id="sbx-d30"), tmp_path, ["big/package-lock.json"], "/workspace",
        )
    assert stats.get("skipped_oversize") == 1, f"超限文件未单独记账: {stats}"
    assert stats.get("oversize_rels") == ["big/package-lock.json"]
    assert stats.get("skipped") == 0, (
        f"确定性尺寸 skip 仍计入 transient skipped（会被 L1 当 BLOCKED 无限重试）: {stats}"
    )
    assert not (tmp_path / "big/package-lock.json").exists()  # 未落盘（禁半截）


def _mk_executor() -> WorkerExecutor:
    st = SubTask(id="st-d30", description="改 A.java",
                 difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
                 scope=FileScope(writable=["A.java"]), intent="modify")
    return WorkerExecutor(subtask=st, project_path="/tmp/swarm-d30-test")


_REAL_DIFF = "--- a/A.java\n+++ b/A.java\n@@ -1 +1 @@\n-old\n+new\n"


def test_d30_gate_oversize_is_deterministic_fail_not_transient_blocked():
    """pipeline True 但 pull-back 有确定性超限 skip → 判 False（失败阶梯），绝不 BLOCKED 重试。"""
    ex = _mk_executor()
    ex._sync_skipped_count = 0
    ex._sync_error_rels = []
    ex._sync_oversize_rels = ["big/package-lock.json"]
    with patch.object(ex, "_get_git_diff", return_value=_REAL_DIFF), \
         patch("swarm.worker.l1_pipeline.run_l1_pipeline", return_value=(True, {})):
        det_ok, details = ex._deterministic_l1_gate()
    assert det_ok is False, (
        f"确定性超限 skip 应判 FAIL 走失败阶梯，got {det_ok}（None=BLOCKED 即 D30 活锁复发）: {details}"
    )
    assert "oversize" in str(details.get("reason", "")), details
    assert details.get("oversize_files") == ["big/package-lock.json"]


def test_d30_gate_transient_skip_still_blocked_regression():
    """回归：transient skip/err 仍走 BLOCKED 退避（A3 语义不变）。"""
    ex = _mk_executor()
    ex._sync_skipped_count = 1
    ex._sync_error_rels = []
    ex._sync_oversize_rels = []
    with patch.object(ex, "_get_git_diff", return_value=_REAL_DIFF), \
         patch("swarm.worker.l1_pipeline.run_l1_pipeline", return_value=(True, {})):
        det_ok, details = ex._deterministic_l1_gate()
    assert det_ok is None and details.get("not_run_kind"), details


# ══════════════════════════════════════════════════════════════════
# D31 — L2 沙箱验证 run_command + infra/测试失败区分
# ══════════════════════════════════════════════════════════════════

class _L2FakeManager:
    """L2 验证用 FakeManager：run_code 一律 502（语言镜像常态），run_command 可配。"""

    def __init__(self, *, infra_down=False, apply_rc=0, test_rc=0):
        self.infra_down = infra_down
        self.apply_rc = apply_rc
        self.test_rc = test_rc
        self.run_code_calls = 0
        self.commands: list[str] = []
        self.killed: list[str] = []

    def create(self, template_id=None, timeout=None, *, project_id=None, task_id=None, source="manual"):
        sb = MagicMock(sandbox_id="sbx-l2")
        sb.files.write = MagicMock()
        return sb

    def sync_project_to_sandbox(self, sandbox, local_root, workdir):
        return {"uploaded": 1, "skipped": 0, "errors": []}

    def run_code(self, sandbox, code, timeout=30):
        self.run_code_calls += 1
        return SimpleNamespace(stdout="", stderr="", error="Exception: 502 Bad Gateway",
                               success=False)

    def run_command(self, sandbox, cmd, timeout=120, **kw):
        self.commands.append(cmd)
        if self.infra_down:
            return SimpleNamespace(stdout="", stderr="",
                                   error="ConnectError: 502 Bad Gateway", success=False)
        if "git apply" in cmd:
            return SimpleNamespace(stdout=f"__APPLY_RC__{self.apply_rc}", stderr="",
                                   error=None, success=True)
        return SimpleNamespace(stdout=f"__RC__{self.test_rc}", stderr="", error=None, success=True)

    def kill(self, sid):
        self.killed.append(sid)


@pytest.fixture()
def _l2(monkeypatch):
    def run(mgr, diff="--- a/A\n+++ b/A\n"):
        from swarm.brain import nodes
        monkeypatch.setattr("swarm.worker.sandbox.get_sandbox_manager", lambda: mgr)
        return nodes._run_l2_in_sandbox("/tmp/proj-d31", diff, "mvn -q test",
                                        project_id="proj-d31", timeout=30)
    return run


def test_d31_l2_uses_run_command_not_run_code(_l2):
    """通过路径：全走 shell 端点，run_code(Jupyter) 一次都不能打。"""
    mgr = _L2FakeManager(apply_rc=0, test_rc=0)
    result = _l2(mgr)
    assert result is True, f"apply/test 全 0 应判通过: {result}"
    assert mgr.run_code_calls == 0, (
        "L2 沙箱验证仍在用 run_code（语言镜像无 Jupyter kernel 必 502，D31 复发）"
    )
    assert any("git apply" in c for c in mgr.commands)
    assert mgr.killed == ["sbx-l2"]


def test_d31_l2_infra_failure_returns_none_not_false(_l2):
    """infra 失败（命令没跑成/5xx）→ None 走既有降级路径，绝不误判测试失败。"""
    mgr = _L2FakeManager(infra_down=True)
    result = _l2(mgr)
    assert result is None, (
        f"infra 失败被当测试失败 got {result}（D31 复发：整任务被 502 误杀）"
    )


def test_d31_l2_real_test_failure_returns_false(_l2):
    """真测试失败（命令跑了 rc!=0）→ False（不与 infra 混淆）。"""
    mgr = _L2FakeManager(apply_rc=0, test_rc=1)
    assert _l2(mgr) is False


def test_d31_l2_apply_failure_returns_false(_l2):
    """merged_diff 打不上（apply rc!=0，确定性）→ False。"""
    mgr = _L2FakeManager(apply_rc=1)
    assert _l2(mgr) is False


def test_d31_l2_create_exception_returns_none(_l2):
    """create 抛异常（沙箱服务不可达）→ None 降级，不判失败也不上抛炸 verify 节点。"""
    mgr = _L2FakeManager()
    def boom(*a, **k):
        raise RuntimeError("sandbox api down")
    mgr.create = boom
    assert _l2(mgr) is None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
