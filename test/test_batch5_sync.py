"""批次5（19号文 C5/C7–C15 + 21号文 W-1/W-2）行为测试。

覆盖：untracked 探测入 flock / 上传 errors 对称账 L1 fail-closed / pom add 侧 span 锚定 /
strip 外部依赖碰撞 WARNING / difflib 删除+二进制表达 / D36 新子目录前缀补捞 /
截断 WARNING+排序确定化 / 清单名栈中立 / 目录泛匹配 src 锚定 / CRLF 基线同源 /
.sln 确定化 / go.work 块形式 probe+prune / allow_any 枚举非 transient 升格。
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import subprocess
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.models.errors import TransientInfraError  # noqa: E402
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskHarness  # noqa: E402
from swarm.worker import executor_sync as esync  # noqa: E402
from swarm.worker import workspace_manifest as wm  # noqa: E402
from swarm.worker.executor import WorkerExecutor  # noqa: E402


def _mk(scope: FileScope, project_path: str = "/tmp/swarm-b5-test",
        build_command: str = "") -> WorkerExecutor:
    st = SubTask(
        id="st-b5", description="批次5", difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT, scope=scope,
        harness=TaskHarness(language="java", build_command=build_command),
    )
    return WorkerExecutor(subtask=st, project_path=project_path)


# ─── C5：untracked 探测必须在与 add -N/diff 同一把 flock 内 ───


def test_c5_untracked_probe_inside_flock(tmp_path):
    """ls-files 探测（读共享 index）与 add -N/diff 必须同锁——兄弟 add -N 占位瞬态
    会让锁外探测误判 tracked → 锁内 diff 时文件纯 untracked → diff 空 → merge 丢文件。"""
    (tmp_path / ".git").mkdir()
    (tmp_path / "A.java").write_text("class A {}")
    ex = _mk(FileScope(writable=["A.java"], create_files=["B.java"]),
             project_path=str(tmp_path))

    events: list[tuple[str, bool]] = []
    lock_held = {"v": False}

    class _RecFlock:
        def __init__(self, root):
            self.root = root

        def __enter__(self):
            lock_held["v"] = True
            return self

        def __exit__(self, *a):
            lock_held["v"] = False
            return False

    real_run = subprocess.run

    def fake_run(cmd, **kw):
        if isinstance(cmd, list) and "git" in cmd[0:1]:
            events.append((" ".join(cmd[:4]), lock_held["v"]))

            class _R:
                returncode = 0
                stdout = b""
                stderr = b""
            return _R()
        return real_run(cmd, **kw)

    with patch.object(esync, "_ProjectGitFlock", _RecFlock), \
         patch("subprocess.run", fake_run):
        ex._try_local_git_diff()

    probe_events = [(c, held) for c, held in events if "ls-files" in c]
    assert probe_events, "ls-files 探测应发生"
    assert all(held for _, held in probe_events), (
        f"ls-files 探测必须在 flock 内（兄弟 add -N 瞬态防误判）: {probe_events}")


# ─── C7：上传 errors 对称账 → L1 拒 PASS 降 BLOCKED ───

_REAL_DIFF = "--- a/A.java\n+++ b/A.java\n@@ -1 +1 @@\n-old\n+new\n"


def test_c7_gate_downgrades_true_when_upload_errored():
    """bootstrap 上传逐文件失败 → agent 在缺文件沙箱跑 → 拒 PASS 降 BLOCKED（对称 A3）。"""
    ex = _mk(FileScope(writable=["A.java"]))
    ex._sync_skipped_count = 0
    ex._sync_error_rels = []
    ex._upload_error_rels = ["A.java: write timeout"]
    with patch.object(ex, "_get_git_diff", return_value=_REAL_DIFF), \
         patch("swarm.worker.l1_pipeline.run_l1_pipeline", return_value=(True, {})):
        det_ok, details = ex._deterministic_l1_gate()
    assert det_ok is None, f"上传不完整时不得判 True，got {det_ok} {details}"
    assert details.get("upload_errors") == 1, details
    assert "upload" in details.get("deterministic_gate", ""), details


def test_c7_gate_passes_when_upload_clean():
    """回归：上传干净（默认空账）→ pipeline True 正常判 True。"""
    ex = _mk(FileScope(writable=["A.java"]))
    ex._sync_skipped_count = 0
    ex._sync_error_rels = []
    ex._upload_error_rels = []
    with patch.object(ex, "_get_git_diff", return_value=_REAL_DIFF), \
         patch("swarm.worker.l1_pipeline.run_l1_pipeline", return_value=(True, {})):
        det_ok, details = ex._deterministic_l1_gate()
    assert det_ok is True, details


# ─── C8：pom add 侧 span 锚定（profiles 先于主 modules） ───


def test_c8_add_side_registers_into_main_modules_not_profile(tmp_path):
    """<profiles> 块先于主 <modules> 时，新模块必须注册进主块，不得进 profile 块。"""
    pom = tmp_path / "pom.xml"
    pom.write_text(
        "<project>\n"
        "  <profiles>\n"
        "    <profile>\n"
        "      <modules>\n"
        "        <module>prof-mod</module>\n"
        "      </modules>\n"
        "    </profile>\n"
        "  </profiles>\n"
        "  <modules>\n"
        "    <module>existing</module>\n"
        "  </modules>\n"
        "</project>\n")
    (tmp_path / "existing").mkdir()
    newmod = tmp_path / "newmod"
    newmod.mkdir()
    (newmod / "pom.xml").write_text(
        "<project><parent><artifactId>root</artifactId></parent></project>")

    modified, added = wm._reconcile_maven(tmp_path, [])
    assert modified, "新模块应被注册"
    text = pom.read_text()
    main_span = text.index("\n  <modules>")  # 行首恰两空格=主块（profile 块六空格不命中）
    assert "<module>newmod</module>" in text[main_span:], (
        f"新模块必须进主 modules 块:\n{text}")
    profile_region = text[:main_span]
    assert "newmod" not in profile_region, (
        f"新模块不得进 profiles 块（默认构建不含）:\n{text}")
    assert "<module>prof-mod</module>" in profile_region  # 既有内容不腐化


# ─── C9：strip 外部依赖碰撞 WARNING，内部模块依赖不告警 ───

_HEAD_POM = (
    "<project><groupId>com.demo</groupId>\n"
    "<modules><module>mod-a</module></modules>\n"
    "<dependencies></dependencies></project>")


def test_c9_strip_external_dep_warns(caplog):
    worker = _HEAD_POM.replace(
        "<dependencies></dependencies>",
        "<dependencies><dependency><groupId>org.x</groupId>"
        "<artifactId>ext-lib</artifactId></dependency></dependencies>")
    local = worker  # 共享树当前含该外部依赖（worker 或兄弟加的三方文本不可区分）
    with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
        new_text, removed = wm.strip_worker_manifest_contribs(
            local, worker, _HEAD_POM, "pom.xml")
    assert removed == 1
    assert "ext-lib" not in new_text
    assert any("外部依赖" in r.message and "ext-lib" in r.message
               for r in caplog.records), caplog.text


def test_c9_strip_internal_module_dep_no_external_warning(caplog):
    worker = _HEAD_POM.replace(
        "<dependencies></dependencies>",
        "<dependencies><dependency><groupId>com.demo</groupId>"
        "<artifactId>mod-a</artifactId></dependency></dependencies>")
    with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
        new_text, removed = wm.strip_worker_manifest_contribs(
            worker, worker, _HEAD_POM, "pom.xml")
    assert removed == 1
    assert not any("外部依赖" in r.message for r in caplog.records), caplog.text


# ─── C10：difflib 删除 +++ /dev/null；二进制剔出 diff ───


def test_c10_difflib_expresses_deletion(tmp_path):
    ex = _mk(FileScope(writable=["a.txt", "gone.txt"]), project_path=str(tmp_path))
    ex._pre_sync_contents = {"a.txt": "old\n", "gone.txt": "to-be-deleted\n"}
    ex._post_sync_contents = {"a.txt": "new\n"}
    with patch.object(ex, "_try_local_git_diff", return_value=None):
        diff = ex._get_git_diff()
    assert "gone.txt" in diff, diff
    assert "+++ /dev/null" in diff, f"删除必须 +++ /dev/null 形态（git apply 可消费）:\n{diff}"
    assert "-to-be-deleted" in diff, diff


def test_c10_binary_change_excluded_from_diff_with_warning(tmp_path):
    """文本→二进制/不可读迁移（pre 有文本、post 为 None）= 二进制变更——无字节构造不了
    binary patch，必须剔出 diff + WARNING，绝不把非法行混进 diff 毒 git apply。"""
    ex = _mk(FileScope(writable=["bin.dat", "a.txt"]), project_path=str(tmp_path))
    ex._pre_sync_contents = {"bin.dat": "was-text\n", "a.txt": "x\n"}
    ex._post_sync_contents = {"bin.dat": None, "a.txt": "y\n"}
    logs: list[str] = []
    with patch.object(ex, "_try_local_git_diff", return_value=None), \
         patch.object(ex, "_log", lambda msg, level=None: logs.append(msg)):
        diff = ex._get_git_diff()
    assert "二进制文件变更" not in diff, (
        f"非法行混进 diff 会让 git apply 整体报'补丁损坏'连坐全部 hunk:\n{diff}")
    assert any("二进制" in m and "bin.dat" in m for m in logs), logs


# ─── C11：D36 交集放宽（新建子目录前缀匹配） ───


def test_c11_new_subdirectory_file_matches_ctx_prefix():
    ctx = {"ruoyi-admin/src/main/java/com/ruoyi/web/controller/A.java"}
    dirs = {"ruoyi-admin/src/main/java/com/ruoyi/web/controller"}
    # 新建 impl/ 子包里的文件：不在 ctx 精确集，但在目录前缀下 → 必须捞到
    assert esync._in_ctx_or_under_dirs(
        "ruoyi-admin/src/main/java/com/ruoyi/web/controller/impl/B.java", ctx, dirs)
    # 精确命中照旧
    assert esync._in_ctx_or_under_dirs(
        "ruoyi-admin/src/main/java/com/ruoyi/web/controller/A.java", ctx, dirs)
    # 无关树不匹配
    assert not esync._in_ctx_or_under_dirs(
        "other-module/src/main/java/com/x/C.java", ctx, dirs)


# ─── C13：截断 WARNING + 排序确定化 ───


def test_c13_module_source_cap_warns_and_deterministic(tmp_path, monkeypatch):
    mod = tmp_path / "mod"
    (mod / "src" / "main" / "java").mkdir(parents=True)
    (mod / "pom.xml").write_text("<project/>")
    for n in ("A", "B", "C"):
        (mod / "src" / "main" / "java" / f"{n}.java").write_text(f"class {n} {{}}")
    ex = _mk(FileScope(writable=["mod/src/main/java/A.java"]),
             project_path=str(tmp_path), build_command="mvn compile")
    monkeypatch.setattr(esync, "_MODULE_SRC_CAP", 2)
    logs: list[tuple[str, str | None]] = []
    with patch.object(ex, "_log", lambda msg, level=None: logs.append((msg, level))):
        out1 = ex._module_source_files()
        out2 = ex._module_source_files()
    assert len(out1) == 2
    assert out1 == out2, "排序确定化：两次收集必须一致（旧 rglob 顺序随机漏不同文件）"
    assert out1 == sorted(out1)
    assert any(lv == "warning" and "超上限" in m for m, lv in logs), logs


def test_c13_manifest_cap_warns_and_deterministic(tmp_path, monkeypatch):
    (tmp_path / "pom.xml").write_text("<project/>")
    for i in range(4):
        d = tmp_path / f"m{i}"
        d.mkdir()
        (d / "pom.xml").write_text("<project/>")
    ex = _mk(FileScope(readable=["m0/pom.xml"]), project_path=str(tmp_path))
    monkeypatch.setattr(esync, "_MANIFEST_CAP", 2)
    logs: list[tuple[str, str | None]] = []
    with patch.object(ex, "_log", lambda msg, level=None: logs.append((msg, level))):
        out1 = ex._build_manifest_files()
        out2 = ex._build_manifest_files()
    assert out1 == out2, "排序确定化"
    assert any(lv == "warning" and "超上限" in m for m, lv in logs), logs


# ─── C14：清单名栈中立 + go/npm 模块源码驱动 ───


def test_c14_manifest_names_cover_all_stacks():
    names = esync._SYNC_MANIFEST_NAMES
    for n in ("pom.xml", "go.mod", "go.work", "Cargo.toml",
              "package.json", "pyproject.toml"):
        assert n in names, f"{n} 必须在始终补传清单集（reactor-not-found 换栈复发防线）"


def test_c14_go_module_source_files_skip_vendor(tmp_path):
    mod = tmp_path / "svc"
    mod.mkdir()
    (mod / "go.mod").write_text("module demo/svc")
    (mod / "main.go").write_text("package main")
    (mod / "util.go").write_text("package main")
    (mod / "vendor" / "dep").mkdir(parents=True)
    (mod / "vendor" / "dep" / "v.go").write_text("package dep")
    ex = _mk(FileScope(writable=["svc/main.go"]),
             project_path=str(tmp_path), build_command="go build ./...")
    out = ex._module_source_files()
    assert "svc/main.go" in out and "svc/util.go" in out, out
    assert not any("vendor" in p for p in out), out


def test_c14_npm_module_source_files_skip_node_modules(tmp_path):
    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    (web / "package.json").write_text("{}")
    (web / "src" / "index.ts").write_text("export {}")
    (web / "node_modules" / "lib").mkdir(parents=True)
    (web / "node_modules" / "lib" / "x.js").write_text("module.exports = {}")
    ex = _mk(FileScope(writable=["web/src/index.ts"]),
             project_path=str(tmp_path), build_command="npm run build")
    out = ex._module_source_files()
    assert "web/src/index.ts" in out, out
    assert not any("node_modules" in p for p in out), out


# ─── C15：src 锚定不误伤 build 包 / CRLF 同源 / .sln 确定化 ───


def test_c15_build_package_dir_not_excluded(tmp_path):
    """com/x/build/ 是合法包目录——target/build 排除只锚 src/ 之前的段。"""
    mod = tmp_path / "mod"
    (mod / "src" / "main" / "java" / "com" / "x" / "build").mkdir(parents=True)
    (mod / "pom.xml").write_text("<project/>")
    (mod / "src" / "main" / "java" / "com" / "x" / "build" / "Helper.java").write_text(
        "class Helper {}")
    (mod / "target").mkdir()
    (mod / "target" / "Gen.java").write_text("class Gen {}")  # 产物仍须排除
    ex = _mk(FileScope(writable=["mod/src/main/java/com/x/build/Helper.java"]),
             project_path=str(tmp_path), build_command="mvn compile")
    out = ex._module_source_files()
    assert "mod/src/main/java/com/x/build/Helper.java" in out, out
    assert not any(p.startswith("mod/target") for p in out), out


def test_c15_git_baseline_preserves_crlf(tmp_path):
    ex = _mk(FileScope(writable=["a.txt"]), project_path=str(tmp_path))

    class _R:
        returncode = 0
        stdout = b"line1\r\nline2\r\n"
        stderr = b""

    with patch("subprocess.run", return_value=_R()):
        text = ex._git_baseline_text(tmp_path, "a.txt")
    assert text == "line1\r\nline2\r\n", (
        f"基线必须与 diff 侧 CRLF 同源（text=True 会转 LF 造伪变更）: {text!r}")


def test_c15_dotnet_sln_pick_deterministic(tmp_path):
    (tmp_path / "b.sln").write_text("Microsoft Visual Studio Solution File\nGlobal\nEndGlobal\n")
    (tmp_path / "a.sln").write_text("Microsoft Visual Studio Solution File\nGlobal\nEndGlobal\n")
    read_order: list[str] = []
    real_read = wm._read

    def rec_read(p):
        if str(p).endswith(".sln"):
            read_order.append(str(p))
        return real_read(p)

    with patch.object(wm, "_read", rec_read):
        wm._reconcile_dotnet_sln(tmp_path, [])
    assert read_order and read_order[0].endswith("a.sln"), (
        f"多 .sln 必须排序取首（glob 顺序不定不可复现）: {read_order}")


# ─── W-1：go.work 块形式 probe + prune ───

_GOWORK_BLOCK = 'go 1.21\n\nuse (\n\t./a\n\t./b\n)\n'


def test_w1_probe_block_form_captures_all_members():
    probes = wm.manifest_member_probes("go.work", _GOWORK_BLOCK)
    toks = {t for t, _ in probes}
    assert {"a", "b"} <= toks, f"块形式必须逐行捕全（旧正则只捕首成员）: {probes}"


def test_w1_prune_ghost_from_block_form():
    new_text, removed = wm.prune_manifest_members(
        "go.work", _GOWORK_BLOCK, lambda probe: False if probe == "b" else True)
    assert removed == ["b"], removed
    assert "./a" in new_text, new_text
    assert "./b" not in new_text, new_text
    assert "use" in new_text  # a 仍在，块保留


def test_w1_prune_emptied_block_removed():
    new_text, removed = wm.prune_manifest_members(
        "go.work", _GOWORK_BLOCK, lambda probe: False)
    assert set(removed) == {"a", "b"}, removed
    assert "use (" not in new_text, f"摘空的 use 块应整体移除（防空块解析错）:\n{new_text}"


def test_w1_prune_single_line_form_still_works():
    text = "go 1.21\n\nuse ./a\nuse ./b\n"
    new_text, removed = wm.prune_manifest_members(
        "go.work", text, lambda probe: False if probe == "b" else True)
    assert removed == ["b"], removed
    assert "use ./a" in new_text and "./b" not in new_text, new_text


# ─── W-2：allow_any 枚举非 transient 异常升格 transient ───

def test_w2_non_transient_enum_error_mapped_to_transient(tmp_path):
    scope = FileScope(allow_any=True)
    ex = _mk(scope, project_path=str(tmp_path))
    ex._sandbox = object()
    ex._sandbox_manager = object()
    with patch.object(ex, "_list_sandbox_workspace_files",
                      side_effect=ValueError("确定性故障")):
        try:
            asyncio.run(ex._sync_from_sandbox("test"))
        except TransientInfraError:
            pass
        else:  # pragma: no cover
            raise AssertionError(
                "非 transient 枚举异常必须升格 TransientInfraError（绝不静默当无可写文件蒸发产物）")


# ─── R1 整改回归：hunter F1/F2/F3 + reviewer F-3 ───


def test_r1_f1_readable_and_manifest_never_fake_deleted(tmp_path):
    """hunter F1/reviewer F-1 负向：pre 键域=运行时超集（writable∪readable∪构建清单∪
    模块源码），post 仅 writable——readable/清单绝不得被写成 +++ /dev/null 假删除
    （merge 侧 git apply 会真删上下文树=删库级假 diff）。"""
    ex = _mk(FileScope(writable=["src/A.java"], readable=["ctx/Helper.java"]),
             project_path=str(tmp_path))
    ex._pre_sync_contents = {
        "src/A.java": "old\n",
        "ctx/Helper.java": "class Helper {}\n",   # readable 上下文，从不回传
        "pom.xml": "<project/>\n",                # 构建清单，从不回传
    }
    ex._post_sync_contents = {"src/A.java": "new\n"}
    with patch.object(ex, "_try_local_git_diff", return_value=None):
        diff = ex._get_git_diff()
    assert "Helper.java" not in diff, f"readable 不得被假删除:\n{diff}"
    assert "pom.xml" not in diff, f"构建清单不得被假删除:\n{diff}"
    assert "+++ /dev/null" not in diff, diff


def test_r1_f1_declared_delete_still_expressed(tmp_path):
    """正向对照：scope.delete_files 声明删除的文件，pre 有 post 无 → 仍出 /dev/null hunk。"""
    ex = _mk(FileScope(writable=["src/A.java"], delete_files=["old/Dead.java"]),
             project_path=str(tmp_path))
    ex._pre_sync_contents = {"src/A.java": "x\n", "old/Dead.java": "class Dead {}\n"}
    ex._post_sync_contents = {"src/A.java": "x\n"}
    with patch.object(ex, "_try_local_git_diff", return_value=None):
        diff = ex._get_git_diff()
    assert "old/Dead.java" in diff and "+++ /dev/null" in diff, diff


def test_r1_f2_cargo_command_selects_rust_driver(tmp_path):
    """hunter F2/reviewer F-2：'cargo build'/'cargo test' 不得被 go 词元截胡
    （car【go 】子串），Rust 模块源码必须收集得到。"""
    mod = tmp_path / "crate"
    (mod / "src").mkdir(parents=True)
    (mod / "Cargo.toml").write_text("[package]\nname='crate'")
    (mod / "src" / "main.rs").write_text("fn main() {}")
    (mod / "src" / "lib_util.rs").write_text("pub fn u() {}")
    for cmd in ("cargo build", "cargo test", "cargo build --release"):
        ex = _mk(FileScope(writable=["crate/src/main.rs"]),
                 project_path=str(tmp_path), build_command=cmd)
        out = ex._module_source_files()
        assert "crate/src/main.rs" in out and "crate/src/lib_util.rs" in out, (
            f"{cmd!r} 驱动选择错误（疑似被 go 截胡返回空）: {out}")


def test_r1_f3_child_pom_internal_dep_no_external_warning(caplog):
    """reviewer F-3：子模块 pom（自身无 groupId、parent 继承）——内部依赖（g=parent
    groupId）被摘不得误报外部碰撞 WARNING；groupId 提取须回退 parent 块而非抓到
    第一个 dependency 的 group。"""
    head = (
        "<project><parent><groupId>com.demo</groupId>"
        "<artifactId>root</artifactId></parent>\n"
        "<artifactId>mod-a</artifactId>\n"
        "<dependencies><dependency><groupId>org.x</groupId>"
        "<artifactId>unrelated</artifactId></dependency></dependencies></project>")
    worker = head.replace(
        "<dependencies>",
        "<dependencies><dependency><groupId>com.demo</groupId>"
        "<artifactId>mod-b</artifactId></dependency>")
    with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
        new_text, removed = wm.strip_worker_manifest_contribs(
            worker, worker, head, "mod-a/pom.xml")
    assert removed == 1
    assert not any("外部依赖" in r.message for r in caplog.records), caplog.text


def test_r1_f3_child_pom_external_dep_still_warns(caplog):
    """对照：同形态子 pom 摘真外部依赖（org.y）→ WARNING 仍打（parent group 不误纳）。"""
    head = (
        "<project><parent><groupId>com.demo</groupId>"
        "<artifactId>root</artifactId></parent>\n"
        "<artifactId>mod-a</artifactId>\n"
        "<dependencies></dependencies></project>")
    worker = head.replace(
        "<dependencies></dependencies>",
        "<dependencies><dependency><groupId>org.y</groupId>"
        "<artifactId>ext-lib</artifactId></dependency></dependencies>")
    with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
        new_text, removed = wm.strip_worker_manifest_contribs(
            worker, worker, head, "mod-a/pom.xml")
    assert removed == 1
    assert any("外部依赖" in r.message and "ext-lib" in r.message
               for r in caplog.records), caplog.text


def test_r1_f3_upload_deterministic_error_fails_not_blocked():
    """hunter F3：上传错误含确定性类别（本地文件不存在）→ 判 FAIL 走失败阶梯，
    绝不当 transient BLOCKED 烧配额空转（D30 对称臂）。"""
    ex = _mk(FileScope(writable=["A.java"]))
    ex._sync_skipped_count = 0
    ex._sync_error_rels = []
    ex._upload_error_rels = ["A.java: 本地文件不存在"]
    with patch.object(ex, "_get_git_diff", return_value=_REAL_DIFF), \
         patch("swarm.worker.l1_pipeline.run_l1_pipeline", return_value=(True, {})):
        det_ok, details = ex._deterministic_l1_gate()
    assert det_ok is False, f"确定性上传失败必须判 FAIL（非 BLOCKED 空转）: {det_ok} {details}"
    assert details.get("reason") == "upload_deterministic_missing", details


def test_r1_f3_upload_transient_error_still_blocked():
    """对照：网络/写入类上传失败（非确定性文案）→ 仍降 BLOCKED 退避重试。"""
    ex = _mk(FileScope(writable=["A.java"]))
    ex._sync_skipped_count = 0
    ex._sync_error_rels = []
    ex._upload_error_rels = ["A.java: connection reset by peer"]
    with patch.object(ex, "_get_git_diff", return_value=_REAL_DIFF), \
         patch("swarm.worker.l1_pipeline.run_l1_pipeline", return_value=(True, {})):
        det_ok, details = ex._deterministic_l1_gate()
    assert det_ok is None, details
    assert details.get("upload_errors") == 1, details


def test_r2_dotfile_declared_delete_expressed(tmp_path):
    """hunter R2-1：delete_files=[".env"] 点文件——lstrip("./") 字符集语义会吃掉前导点
    （".env"→"env"）导致声明删除静默不表达；改 "./" 前缀剥离后必须出 /dev/null hunk。"""
    ex = _mk(FileScope(writable=["a.txt"], delete_files=[".env"]),
             project_path=str(tmp_path))
    ex._pre_sync_contents = {"a.txt": "x\n", ".env": "SECRET=1\n"}
    ex._post_sync_contents = {"a.txt": "x\n"}
    with patch.object(ex, "_try_local_git_diff", return_value=None):
        diff = ex._get_git_diff()
    assert ".env" in diff and "+++ /dev/null" in diff, diff
