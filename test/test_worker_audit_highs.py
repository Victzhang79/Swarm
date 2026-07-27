"""worker 审计 HIGH 簇治本（deep_read_findings/19_worker_flow_audit.md，对抗核验 5 CONFIRMED
+ 3 PARTIAL 小修）。

C1  allow_any 枚举通道故障 vs 真空 workspace 三态分流（故障→TransientInfraError，
    与 N-06/N-07 同款；绝不静默 [] 把 greenfield 产物蒸发成"无变更"）。
F3  三条 find 枚举的尺寸谓词与 MAX_SYNC_FILE_SIZE 同源 + 孪生 oversize 探针节入
    D30 账（旧 2000k 写死+静默滤=阈值倒挂+丢件零留痕）。
B1  clean_workspace 的 WORKSPACE_CLEANED 标记闩死在 workdir 清理段（`&&`），
    清理失败绝不打标记（脏沙箱回池假成功）。
C3  reconcile_workspace_manifests(use_lock=True) 读-改-写整段入 _ProjectGitFlock。
C4  go.work 块形式 `use ( ... )` 逐行解析（旧正则每块只捕获首成员→追加重复 use
    →go 硬错毒权威库）。
D1/D2/D3  security_scan `_mark_ran` 全部移到"输出成功解析"之后（gitleaks P2-2 姿势
    捞平 9 个 sibling）；gitleaks/trufflehog 解析失败返回 None 落兜底链。
C2  _scope_files 并入 upstream_artifacts（通用池与 baked 分支同口径）。
A1  仓库探针先解析 __HTTP__ 状态码再咨询 _tool_missing（404 体含 "Not Found" 不再
    吞掉确证剪除证据）。
B3  镜像依赖指纹与 tarball 同源（git HEAD blob），杜绝"永不重建"闩锁。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.models.errors import TransientInfraError  # noqa: E402
from swarm.worker.executor_sync import (  # noqa: E402
    _OVERSIZE_SECTION_MARKER,
    _SandboxSyncMixin,
    _find_size_kib,
)


class _Result:
    def __init__(self, stdout="", error=None):
        self.stdout = stdout
        self.stderr = ""
        self.error = error


class _EnumExec(_SandboxSyncMixin):
    """最小可用宿主：只带 _list_sandbox_workspace_files 依赖的属性。"""

    def __init__(self, result: _Result):
        self._sandbox = object()
        self._result = result
        self._enum_oversize_rels: list[str] = []
        self.captured_cmds: list[str] = []
        self.logs: list[tuple[str, str]] = []
        outer = self

        class _Mgr:
            def run_command(self, sandbox, cmd, timeout=30):
                outer.captured_cmds.append(cmd)
                return outer._result

        self._sandbox_manager = _Mgr()

    def _log(self, msg, level="info"):
        self.logs.append((level, str(msg)))


# ── C1：三态分流 ──────────────────────────────────────────────────────────────

def test_c1_channel_error_raises_transient():
    ex = _EnumExec(_Result(stdout="", error="gateway 502"))
    with pytest.raises(TransientInfraError):
        ex._list_sandbox_workspace_files()


def test_c1_missing_marker_raises_transient():
    """输出无节标记=cd 失败/命令中断——绝不与真空 workspace 混同。"""
    ex = _EnumExec(_Result(stdout=""))
    with pytest.raises(TransientInfraError):
        ex._list_sandbox_workspace_files()


def test_c1_truly_empty_workspace_returns_empty():
    """真空 workspace：命令完成（节标记在场）但零文件 → 诚实空清单，不抛。"""
    ex = _EnumExec(_Result(stdout=f"{_OVERSIZE_SECTION_MARKER}\n"))
    assert ex._list_sandbox_workspace_files() == []


# ── F3：oversize 探针入账 + 阈值同源 ─────────────────────────────────────────

def test_f3_oversize_section_accounted():
    out = f"src/a.java\nsrc/b.java\n{_OVERSIZE_SECTION_MARKER}\ndata/huge_dump.sql\n"
    ex = _EnumExec(_Result(stdout=out))
    files = ex._list_sandbox_workspace_files()
    assert files == ["src/a.java", "src/b.java"]
    assert ex._enum_oversize_rels == ["data/huge_dump.sql"], \
        "被尺寸谓词排除的文件必须入 oversize 账（L1 fail-closed 消费）"
    assert any(lv == "warning" for lv, _ in ex.logs), "oversize 必须 warning 可观测"


def test_f3_find_threshold_derived_from_max_sync_size():
    """find 谓词阈值与 MAX_SYNC_FILE_SIZE 同源（消除 2MB/8MiB 双权威倒挂），
    且命令带孪生 `! -size` 探针节。"""
    from swarm.worker.sandbox import MAX_SYNC_FILE_SIZE
    ex = _EnumExec(_Result(stdout=f"{_OVERSIZE_SECTION_MARKER}\n"))
    ex._list_sandbox_workspace_files()
    cmd = ex.captured_cmds[0]
    kib = MAX_SYNC_FILE_SIZE // 1024
    assert f"-size -{kib}k" in cmd
    assert f"! -size -{kib}k" in cmd, "必须带孪生 oversize 探针"
    assert _find_size_kib() == kib


# ── B1：clean_workspace 标记闩死在 workdir 清理段 ─────────────────────────────

def _clean_workspace_via_local_shell(tmp_path: Path, workdir: Path) -> tuple[bool, str]:
    """把 clean_workspace 的远程命令在本地 sh 执行【安全前缀】（截至标记 echo，
    只触碰我们自己的 workdir），模拟远端语义；success 模拟真实整串（尾部 `; true`
    恒 rc=0），ok 判据与实现同源（success + 标记在场）。"""
    from swarm.worker.sandbox import SandboxManager

    captured: dict = {}

    class _Fake:
        def run_command(self, sandbox, cmd, timeout=45, _skip_blacklist=False):
            captured["cmd"] = cmd
            head, sep, _tail = cmd.partition("echo WORKSPACE_CLEANED")
            assert sep, "命令必须含标记 echo"
            proc = subprocess.run(
                ["sh", "-c", head + "echo WORKSPACE_CLEANED"],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "HOME": str(tmp_path / "home")},
            )
            return SimpleNamespace(success=True, stdout=proc.stdout, error=None)

    fake = _Fake()
    ok = SandboxManager.clean_workspace(
        fake, SimpleNamespace(sandbox_id="sb-x"), workdir=str(workdir))
    return ok, captured["cmd"]


def test_b1_clean_success_emits_marker(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "left_over.txt").write_text("x")
    ok, cmd = _clean_workspace_via_local_shell(tmp_path, ws)
    assert ok is True
    assert list(ws.iterdir()) == [], "workdir 必须真被清空"
    # /tmp 与 $HOME 缓存清理仍保留（置于标记之后 best-effort）
    assert "find /tmp" in cmd.split("echo WORKSPACE_CLEANED", 1)[1]


def test_b1_clean_failure_no_marker_no_fake_success(tmp_path):
    """workdir 清理失败（不可删条目）→ 标记绝不出现 → ok=False（B1：脏沙箱不回池）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    locked = ws / "locked"
    locked.mkdir()
    (locked / "child.txt").write_text("x")
    locked.chmod(0o555)  # 目录只读 → rm -rf 其子文件失败（非 root）
    try:
        ok, _ = _clean_workspace_via_local_shell(tmp_path, ws)
        assert ok is False, "清理失败还打 WORKSPACE_CLEANED = B1 假成功回归"
    finally:
        locked.chmod(0o755)


# ── C4：go.work 块解析 ────────────────────────────────────────────────────────

def _go_project(tmp_path: Path, work_text: str, mods: list[str]) -> Path:
    root = tmp_path / "gp"
    root.mkdir()
    (root / "go.work").write_text(work_text)
    for m in mods:
        d = root / m
        d.mkdir(parents=True, exist_ok=True)
        (d / "go.mod").write_text(f"module example.com/{m}\n\ngo 1.22\n")
    return root


def test_c4_block_members_all_recognized_idempotent(tmp_path):
    """块形式全员已注册 → reconcile 零改动（旧版把第 2+ 成员误判未注册追加重复 use）。"""
    from swarm.worker.workspace_manifest import reconcile_workspace_manifests
    root = _go_project(tmp_path, "go 1.22\n\nuse (\n\t./a\n\t./b\n\t./c\n)\n",
                       ["a", "b", "c"])
    r = reconcile_workspace_manifests(str(root))
    assert r["added"] == {}, f"块内成员被误判未注册: {r}"
    text = (root / "go.work").read_text()
    assert text.count("./b") == 1, "绝不许追加重复 use（go 对重复目录硬错）"


def test_c4_block_plus_missing_member_added_once(tmp_path):
    from swarm.worker.workspace_manifest import reconcile_workspace_manifests
    root = _go_project(tmp_path, "go 1.22\n\nuse (\n\t./a\n\t./b\n)\n",
                       ["a", "b", "c"])
    r1 = reconcile_workspace_manifests(str(root))
    assert r1["added"].get("go.work") == ["c"]
    r2 = reconcile_workspace_manifests(str(root))
    assert r2["added"] == {}, "二次 reconcile 必须幂等"
    assert (root / "go.work").read_text().count("use ./c") == 1


# ── C3：reconcile use_lock 排队 ───────────────────────────────────────────────

def test_c3_reconcile_waits_for_project_flock(tmp_path):
    from swarm.worker.git_flock import _ProjectGitFlock
    from swarm.worker.workspace_manifest import reconcile_workspace_manifests
    root = _go_project(tmp_path, "go 1.22\n\nuse ./a\n", ["a", "b"])
    release_at = {"t": 0.0}

    def holder():
        with _ProjectGitFlock(root):
            time.sleep(0.6)
            release_at["t"] = time.monotonic()

    th = threading.Thread(target=holder)
    th.start()
    time.sleep(0.15)
    r = reconcile_workspace_manifests(str(root), use_lock=True)
    th.join()
    assert time.monotonic() >= release_at["t"] > 0, \
        "use_lock=True 必须排队等 flock（无锁 RMW 会冲掉兄弟锁内改动）"
    assert r["added"].get("go.work") == ["b"]


# ── D1/D2/D3：security_scan 哨兵归位 ─────────────────────────────────────────

def _sec(monkeypatch, which_map: dict[str, str | None]):
    import swarm.worker.security_scan as sec
    monkeypatch.setattr(sec.shutil, "which", lambda n: which_map.get(n))
    return sec


def test_d1_bandit_crash_output_keeps_sentinel(monkeypatch, tmp_path):
    sec = _sec(monkeypatch, {"bandit": "/bin/bandit"})
    monkeypatch.setattr(sec, "_run_tool", lambda cmd, cwd, timeout=120: (2, "fatal crash", ""))
    ctx = sec._ScanContext()
    assert sec._sast_python(str(tmp_path), ctx=ctx) == []
    assert ctx.scanner_ran is False, "工具跑挂（输出不可解析）绝不解除 fail-closed 哨兵"


def test_d1_bandit_parsed_output_marks_ran(monkeypatch, tmp_path):
    sec = _sec(monkeypatch, {"bandit": "/bin/bandit"})
    monkeypatch.setattr(sec, "_run_tool",
                        lambda cmd, cwd, timeout=120: (0, '{"results": []}', ""))
    ctx = sec._ScanContext()
    assert sec._sast_python(str(tmp_path), ctx=ctx) == []
    assert ctx.scanner_ran is True


def test_d1_spotbugs_bad_xml_keeps_sentinel(monkeypatch, tmp_path):
    sec = _sec(monkeypatch, {"spotbugs": "/bin/spotbugs"})
    monkeypatch.setattr(sec, "_run_tool", lambda cmd, cwd, timeout=300: (1, "not-xml", ""))
    ctx = sec._ScanContext()
    assert sec._sast_java(str(tmp_path), ctx=ctx) == []
    assert ctx.scanner_ran is False


def test_d2_dep_java_missing_report_keeps_sentinel(monkeypatch, tmp_path):
    sec = _sec(monkeypatch, {"dependency-check": "/bin/dc"})
    monkeypatch.setattr(sec, "_run_tool", lambda cmd, cwd, timeout=300: (0, "", ""))
    ctx = sec._ScanContext()
    assert sec._dep_java(str(tmp_path), ctx=ctx) == []
    assert ctx.scanner_ran is False, "报告缺失（起跑即崩）时置位=依赖漏洞 0 覆盖假绿"


def test_d3_gitleaks_unreadable_report_falls_through(monkeypatch, tmp_path):
    """gitleaks 报告不可读 → None（落 trufflehog/内置兜底），不再 [] 短路整条密钥链。"""
    sec = _sec(monkeypatch, {"gitleaks": "/bin/gitleaks"})
    monkeypatch.setattr(sec, "_run_tool", lambda cmd, cwd, timeout=180: (0, "", ""))
    ctx = sec._ScanContext()
    assert sec._secret_gitleaks(str(tmp_path), ctx=ctx) is None
    assert ctx.scanner_ran is False


def test_d3_secret_chain_reaches_builtin_when_tools_fail(monkeypatch, tmp_path):
    """gitleaks 解析失败 + trufflehog 跑挂 → 内置正则兜底必须运行（返回 list 非 None）。"""
    sec = _sec(monkeypatch, {"gitleaks": "/bin/gitleaks", "trufflehog": "/bin/th"})
    monkeypatch.setattr(sec, "_run_tool", lambda cmd, cwd, timeout=180: (1, "garbage", ""))
    (tmp_path / "app.py").write_text('password = "hunter2-not-a-real-secret"\n')
    ctx = sec._ScanContext()
    out = sec._run_secret_scan(str(tmp_path), ctx=ctx)
    assert isinstance(out, list), "兜底链必须走到内置正则"
    assert ctx.scanner_ran is False, "内置兜底不算真实扫描器（A-P0-2 语义保持）"


# ── C2：upstream_artifacts 并入通用池上传清单 ─────────────────────────────────

class _ScopeExec(_SandboxSyncMixin):
    def __init__(self, project_path: str, scope):
        self.project_path = project_path
        self.effective_scope = scope

    def _build_manifest_files(self):
        return []

    def _module_source_files(self):
        return []


def test_c2_upstream_artifacts_join_upload_list(tmp_path):
    (tmp_path / "mapper").mkdir()
    (tmp_path / "mapper" / "UserMapper.xml").write_text("<mapper/>")
    scope = SimpleNamespace(
        readable=["a.java"], writable=[], create_files=[], delete_files=[],
        upstream_artifacts=["mapper/UserMapper.xml", "mapper/Missing.xml"],
    )
    files = _ScopeExec(str(tmp_path), scope)._scope_files()
    assert "mapper/UserMapper.xml" in files, \
        "通用池分支必须消费 upstream_artifacts（与 baked 分支同口径）"
    assert "mapper/Missing.xml" not in files, "本地不存在的条目跳过（防 FileNotFoundError）"


# ── A1：探针先解状态码再咨询 _tool_missing ────────────────────────────────────

def test_a1_http_404_with_not_found_body_still_confirms(monkeypatch):
    import swarm.worker.l1_pipeline as lp
    monkeypatch.setattr(
        lp, "_run_l1_command",
        lambda cmd, project_path, timeout=30: (0, "<html>404 Not Found</html>\n__HTTP__404"))
    versions, reachable = lp._fetch_maven_versions_probe("com.x", "ghost", ".", 30)
    assert versions == []
    assert reachable is True, \
        "404 响应体含 'Not Found' 被 _tool_missing 吞掉=确证剪除防线旁路（A1）"


def test_a1_tool_missing_without_status_still_skipped(monkeypatch):
    import swarm.worker.l1_pipeline as lp
    monkeypatch.setattr(
        lp, "_run_l1_command",
        lambda cmd, project_path, timeout=30: (127, "sh: curl: command not found"))
    versions, reachable = lp._fetch_maven_versions_probe("com.x", "ghost", ".", 30)
    assert versions == [] and reachable is False


# ── B3：指纹与 tarball（git HEAD）同源 ────────────────────────────────────────

def test_b3_fingerprint_tracks_head_not_worktree(tmp_path):
    from swarm.worker.image_builder import _dependency_fingerprint
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "pom.xml").write_text("<project><v>1</v></project>")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=root, check=True)
    fp_base = _dependency_fingerprint(root)
    (root / "pom.xml").write_text("<project><v>2-uncommitted</v></project>")
    assert _dependency_fingerprint(root) == fp_base, \
        "未提交改动不得进指纹（tarball 取 HEAD，异源=永不重建闩锁）"
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "bump"], cwd=root, check=True)
    assert _dependency_fingerprint(root) != fp_base, "提交后 HEAD 变→指纹必须变"
