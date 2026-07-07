"""P1-12：深读登记册 D32/D33/D34 —— L1/L3 闸门三条治本的行为测试。

D32 Maven version-repair 全局串替换越界改写项目自身版本 →
    只允许在【声明目标 artifactId 的 <dependency> 块】内替换；属性引用只改该属性定义。
D33 整树 lint(go vet/clippy)把兄弟/存量问题连坐硬阻断 →
    lint error 按归属划分：scope 内(本子任务改动文件)才阻断，scope 外/无法归属降级告警。
D34 L3 push 失败 fail-open 在默认 ref 上跑 pipeline 假绿 →
    push 失败 fail-closed(l3_passed=None 跳过而非 True)；apply 走临时 index base 树口径
    (round29 _apply_check_against_base 同源)，pull-back 材化的 untracked 文件不再撞
    "already exists"。

全部行为测试，不做源码字符串断言。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# ══════════════════ D32：Maven version-repair 越界替换 ══════════════════

_ROOT_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project>
    <modelVersion>4.0.0</modelVersion>
    <groupId>com.acme</groupId>
    <artifactId>acme-parent</artifactId>
    <version>3.8.7</version>
    <packaging>pom</packaging>
    <properties>
        <thirdparty.version>9.9.9</thirdparty.version>
    </properties>
    <modules>
        <module>acme-module</module>
    </modules>
</project>
"""

_MODULE_POM_LITERAL = """<?xml version="1.0" encoding="UTF-8"?>
<project>
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>com.acme</groupId>
        <artifactId>acme-parent</artifactId>
        <version>3.8.7</version>
    </parent>
    <artifactId>acme-module</artifactId>
    <dependencies>
        <dependency>
            <groupId>com.thirdparty</groupId>
            <artifactId>foo</artifactId>
            <version>3.8.7</version>
        </dependency>
        <dependency>
            <groupId>com.other</groupId>
            <artifactId>bar</artifactId>
            <version>3.8.7</version>
        </dependency>
    </dependencies>
</project>
"""

_MODULE_POM_PROPERTY = """<?xml version="1.0" encoding="UTF-8"?>
<project>
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>com.acme</groupId>
        <artifactId>acme-parent</artifactId>
        <version>3.8.7</version>
    </parent>
    <artifactId>acme-module</artifactId>
    <dependencies>
        <dependency>
            <groupId>com.thirdparty</groupId>
            <artifactId>foo</artifactId>
            <version>${thirdparty.version}</version>
        </dependency>
    </dependencies>
</project>
"""


def _setup_maven_project(tmp: str, module_pom: str) -> None:
    (Path(tmp) / "pom.xml").write_text(_ROOT_POM, encoding="utf-8")
    mod = Path(tmp) / "acme-module"
    mod.mkdir()
    (mod / "pom.xml").write_text(module_pom, encoding="utf-8")


def test_d32_version_repair_spares_project_own_version(monkeypatch):
    """模型给第三方依赖顺手写了项目自身版本号(3.8.7)触发校正 →
    只有该依赖的 <dependency> 块被改；根 pom 项目版本 / 模块 parent 版本 /
    同版本号的无关依赖(bar，构建没报它缺失)一律不碰。"""
    import swarm.worker.l1_pipeline as lp

    with tempfile.TemporaryDirectory() as tmp:
        _setup_maven_project(tmp, _MODULE_POM_LITERAL)
        monkeypatch.setattr(
            lp, "_fetch_maven_versions",
            lambda g, a, p, t: ["3.0.0", "3.5.0"] if a == "foo" else [],
        )
        build_out = (
            "[ERROR] Failed to execute goal: Could not find artifact "
            "com.thirdparty:foo:jar:3.8.7 in public"
        )
        n, changed = lp._attempt_maven_version_repair(tmp, build_out, timeout=30)

        root = (Path(tmp) / "pom.xml").read_text(encoding="utf-8")
        module = (Path(tmp) / "acme-module" / "pom.xml").read_text(encoding="utf-8")

        # 目标依赖块被校正
        assert "<version>3.5.0</version>" in module, f"目标依赖版本未校正: {module}"
        # ★项目自身版本绝不能被连坐改写★
        assert "<version>3.8.7</version>" in root, f"根 pom 项目版本被越界改写: {root}"
        assert root.count("3.5.0") == 0, f"根 pom 不应出现校正版本: {root}"
        # 模块 parent 版本不被碰
        parent_seg = module.split("</parent>")[0]
        assert "<version>3.8.7</version>" in parent_seg, f"parent 版本被越界改写: {module}"
        # 同版本号但构建没报缺失的无关依赖 bar 不被碰
        bar_seg = module.split("<artifactId>bar</artifactId>")[1]
        assert bar_seg.lstrip().startswith("<version>3.8.7</version>"), \
            f"无关依赖 bar 的版本被越界改写: {module}"
        assert n >= 1 and changed


def test_d32_version_repair_property_indirection_still_works(monkeypatch):
    """依赖版本写作 ${thirdparty.version} → 校正发生在该属性定义处(且仅该属性标签)，
    根 pom 项目版本仍不被碰。(回归护栏：块级修复不能丢掉属性间接层的能力。)"""
    import swarm.worker.l1_pipeline as lp

    with tempfile.TemporaryDirectory() as tmp:
        _setup_maven_project(tmp, _MODULE_POM_PROPERTY)
        monkeypatch.setattr(
            lp, "_fetch_maven_versions",
            lambda g, a, p, t: ["9.0.0", "9.5.0"] if a == "foo" else [],
        )
        build_out = "Could not find artifact com.thirdparty:foo:jar:9.9.9 in public"
        n, changed = lp._attempt_maven_version_repair(tmp, build_out, timeout=30)

        root = (Path(tmp) / "pom.xml").read_text(encoding="utf-8")
        module = (Path(tmp) / "acme-module" / "pom.xml").read_text(encoding="utf-8")

        assert "<thirdparty.version>9.5.0</thirdparty.version>" in root, \
            f"属性定义未被校正: {root}"
        assert "<version>3.8.7</version>" in root, "根 pom 项目版本被越界改写"
        # 依赖块保持属性引用不变
        assert "<version>${thirdparty.version}</version>" in module
        assert n >= 1 and changed


def test_d32_rewrite_dependency_version_pure_function():
    """纯函数契约：只改声明目标 artifactId 的 <dependency> 块；
    dependencyManagement 内同样生效；其它块/标签一个字符不动。"""
    from swarm.worker.l1_pipeline import rewrite_dependency_version

    pom = (
        "<project>\n"
        "  <version>1.2.3</version>\n"
        "  <dependencyManagement><dependencies>\n"
        "    <dependency>\n"
        "      <groupId>g</groupId><artifactId>target</artifactId>\n"
        "      <version>1.2.3</version>\n"
        "    </dependency>\n"
        "    <dependency>\n"
        "      <groupId>g</groupId><artifactId>innocent</artifactId>\n"
        "      <version>1.2.3</version>\n"
        "    </dependency>\n"
        "  </dependencies></dependencyManagement>\n"
        "</project>\n"
    )
    out, props = rewrite_dependency_version(pom, "target", "1.2.3", "1.2.0")
    assert props == []
    # 项目自身 version 不动
    assert out.startswith("<project>\n  <version>1.2.3</version>")
    # target 的受管块被改，innocent 的不动
    assert out.count("<version>1.2.0</version>") == 1
    assert out.count("<version>1.2.3</version>") == 2  # project + innocent


def test_d32_rewrite_dependency_version_skips_reserved_property():
    """依赖版本引用 ${project.version}/${revision} 等保留属性=项目自身版本 →
    绝不返回该属性去校正(fail-closed)，pom 原样。"""
    from swarm.worker.l1_pipeline import rewrite_dependency_version

    pom = (
        "<project><version>2.0.0</version><dependencies>\n"
        "  <dependency><groupId>g</groupId><artifactId>target</artifactId>\n"
        "    <version>${project.version}</version></dependency>\n"
        "  <dependency><groupId>g</groupId><artifactId>t2</artifactId>\n"
        "    <version>${revision}</version></dependency>\n"
        "</dependencies></project>\n"
    )
    out, props = rewrite_dependency_version(pom, "target", "2.0.0", "1.9.0")
    assert out == pom and props == []
    out2, props2 = rewrite_dependency_version(pom, "t2", "2.0.0", "1.9.0")
    assert out2 == pom and props2 == []


# ══════════════════ D33：lint 闸门归属过滤 ══════════════════

def _make_subtask(writable=None):
    from swarm.types import FileScope, SubTask, SubTaskDifficulty

    return SubTask(
        id="sub-1",
        description="test",
        difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=writable or ["hello.py"], readable=writable or ["hello.py"]),
    )


_SIMPLE_DIFF = "--- a/hello.py\n+++ b/hello.py\n@@ -1 +1 @@\n-old\n+new\n"


def _run_pipeline_with_lint(monkeypatch, tmp: str, lint_ret):
    import swarm.worker.l1_pipeline as lp

    (Path(tmp) / "hello.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(lp, "_lint_files", lambda pp, files, timeout=60: lint_ret)
    monkeypatch.setenv("SWARM_WORKER_L1_FORMAT", "false")
    monkeypatch.delenv("SWARM_WORKER_L1_LINT", raising=False)
    monkeypatch.delenv("SWARM_WORKER_L1_LINT_GATE", raising=False)
    return lp.run_l1_pipeline(tmp, _make_subtask(), _SIMPLE_DIFF)


def test_d33_sibling_lint_error_does_not_block(monkeypatch):
    """整树 lint(go vet/clippy)报的 error 全在兄弟/存量文件 → 不阻断本子任务，
    降级告警可观测。(改前：任何 lint error 一律硬阻断=兄弟连坐。)"""
    sibling_issue = {
        "file": "othermodule/sibling.go", "line": 3,
        "code": "govet", "message": "undefined: Foo", "severity": "error",
    }
    with tempfile.TemporaryDirectory() as tmp:
        ok, details = _run_pipeline_with_lint(
            monkeypatch, tmp, (True, "go vet fail", [sibling_issue]))
    assert ok is True, f"兄弟文件 lint error 不应阻断本子任务: {details.get('lint')}"
    lint = details["lint"]
    assert lint.get("gated") is False
    assert lint.get("error_issues_out_of_scope"), "scope 外 error 必须被记录(可观测)"


def test_d33_own_file_lint_error_still_blocks(monkeypatch):
    """归属本子任务改动文件的 lint error 仍硬阻断(闸门不放水)。"""
    own_issue = {
        "file": "hello.py", "line": 1,
        "code": "invalid-syntax", "message": "syntax error", "severity": "error",
    }
    with tempfile.TemporaryDirectory() as tmp:
        ok, details = _run_pipeline_with_lint(
            monkeypatch, tmp, (True, "ruff fail", [own_issue]))
    assert ok is False, "scope 内 lint error 必须仍然硬阻断"
    assert details["lint"].get("gated") is True


def test_d33_own_file_error_blocks_even_with_abs_path(monkeypatch):
    """eslint 等吐绝对路径(沙箱/本地前缀)也要归属到本子任务文件并阻断。"""
    own_issue_abs = {
        "file": "/workspace/hello.py", "line": 1,
        "code": "no-undef", "message": "x is not defined", "severity": "error",
    }
    with tempfile.TemporaryDirectory() as tmp:
        ok, details = _run_pipeline_with_lint(
            monkeypatch, tmp, (True, "eslint fail", [own_issue_abs]))
    assert ok is False, "绝对路径归属到本子任务文件也必须阻断"
    assert details["lint"].get("gated") is True


def test_d33_unattributed_lint_error_downgrades_observably(monkeypatch, caplog):
    """解析不出文件路径的 lint 硬错误(配置错/工具输出异常) → 降级告警不阻断，
    但必须可观测(记录+日志)，绝不静默丢。"""
    import logging

    no_file_issue = {
        "file": "", "line": None,
        "code": "clippy", "message": "error: could not compile `app`", "severity": "error",
    }
    # 全量套件下其它测试可能改动 logging 配置（父链任意一级 propagate=False 都会让
    # caplog 抓空）——不赌 propagate 链，把 caplog 的 handler 直接挂到模块 logger 上，
    # 彻底隔离测试次序污染。
    _lp_logger = logging.getLogger("swarm.worker.l1_pipeline")
    monkeypatch.setattr(_lp_logger, "disabled", False)
    _lp_logger.addHandler(caplog.handler)
    try:
        with tempfile.TemporaryDirectory() as tmp, \
                caplog.at_level(logging.WARNING, logger="swarm.worker.l1_pipeline"):
            ok, details = _run_pipeline_with_lint(
                monkeypatch, tmp, (True, "clippy fail", [no_file_issue]))
    finally:
        _lp_logger.removeHandler(caplog.handler)
    assert ok is True, "无法归属的 lint error 应降级告警不阻断"
    lint = details["lint"]
    assert lint.get("gated") is False
    assert lint.get("error_issues_unattributed"), "无法归属的 error 必须被记录"
    assert any("lint" in r.message.lower() or "归属" in r.message for r in caplog.records), \
        "降级必须有 warning 日志可观测"


def test_d33_clippy_arrow_line_backfills_file():
    """clippy 人类输出的 `--> src/main.rs:5:9` 定位行要回填到前一条 issue，
    否则 Rust 的归属判定永远无文件可依。"""
    import swarm.worker.l1_pipeline as lp

    with patch.object(lp, "_find_tool", lambda name: f"/usr/bin/{name}"), \
         patch.object(lp, "_manifest_present", lambda m, p: True), \
         patch.object(lp, "_sandbox_ctx", lambda: None), \
         patch.object(lp, "_run_check_split",
                      lambda cmd, pp, timeout=60: (
                          1, "",
                          "error: unused variable `x`\n"
                          "  --> src/main.rs:5:9\n"
                          "warning: generated 1 warning\n",
                      )):
        err, msgs, issues = lp._lint_rust("/proj", ["src/main.rs"], timeout=20)
    assert err is True
    assert issues and issues[0]["file"] == "src/main.rs" and issues[0]["line"] == 5


# ══════════════════ D34：L3 push fail-closed + base 树 apply ══════════════════

def _l3_state():
    from swarm.types import Complexity

    return {
        "complexity": Complexity.COMPLEX,
        "merged_diff": "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n+new\n",
        "task_id": "task-d34",
        "project_id": "proj-1",
        "task_description": "test",
    }


def test_d34_verify_l3_push_failure_fail_closed():
    """push 失败绝不回退默认 ref 跑 pipeline(main 本来就绿=假绿)。
    infra 失败按"未执行"上报：l3_passed=None + l3_skipped，不伪装 True 也不伪装
    False(误触发 HANDLE_FAILURE 把 infra 归因成验证失败)。"""
    from swarm.brain.nodes.verify import verify_l3

    with patch("swarm.brain.l3_gitlab.gitlab_configured", return_value=True), \
         patch("swarm.brain.l3_gitlab.l3_push_enabled", return_value=True), \
         patch("swarm.brain.nodes._get_project_path", return_value="/tmp/proj"), \
         patch("swarm.brain.l3_gitlab.push_merged_diff_branch",
               return_value=(None, "git apply failed: f.py already exists")), \
         patch("swarm.brain.l3_gitlab.trigger_and_poll_pipeline",
               return_value=(True, "pipeline green on main")) as mock_trigger:
        out = asyncio.run(verify_l3(_l3_state()))

    assert out["l3_passed"] is not True, "push 失败后 l3 绝不能被判通过(假绿)"
    assert out["l3_passed"] is None and out.get("l3_skipped") is True, \
        f"push infra 失败应按未执行(None+skipped)上报: {out}"
    mock_trigger.assert_not_called()
    assert "push" in (out.get("l3_message") or "").lower()


def test_d34_verify_l3_no_project_path_fail_closed():
    """push 开启但项目路径不可得 → 同样 fail-closed 跳过，不在默认 ref 上假绿。"""
    from swarm.brain.nodes.verify import verify_l3

    with patch("swarm.brain.l3_gitlab.gitlab_configured", return_value=True), \
         patch("swarm.brain.l3_gitlab.l3_push_enabled", return_value=True), \
         patch("swarm.brain.nodes._get_project_path", return_value=None), \
         patch("swarm.brain.l3_gitlab.trigger_and_poll_pipeline",
               return_value=(True, "green")) as mock_trigger:
        out = asyncio.run(verify_l3(_l3_state()))

    assert out["l3_passed"] is None and out.get("l3_skipped") is True
    mock_trigger.assert_not_called()


def _git(repo: str, *args: str, env: dict | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=60,
        env={**os.environ, **(env or {})},
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


_IDENT = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _init_repo(tmp: Path) -> tuple[str, str]:
    repo = tmp / "repo"
    repo.mkdir()
    _git(str(repo), "init")
    _git(str(repo), "symbolic-ref", "HEAD", "refs/heads/main")
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    _git(str(repo), "add", "-A")
    _git(str(repo), "commit", "-m", "base", env=_IDENT)
    bare = tmp / "remote.git"
    _git(str(tmp), "init", "--bare", str(bare))
    return str(repo), str(bare)


_NEW_FILE_DIFF = (
    "diff --git a/new.txt b/new.txt\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/new.txt\n"
    "@@ -0,0 +1 @@\n"
    "+hello from l3\n"
)


def test_d34_push_survives_untracked_pullback_files(monkeypatch, tmp_path):
    """pull-back 已把 merged_diff 要新建的文件材化进工作树(untracked) →
    push 的 apply 必须走 base 树口径(round29 同源)不撞 "already exists"，
    且完全不污染工作树/当前分支。(改前：工作树 checkout -B + apply 必败。)"""
    import swarm.brain.l3_gitlab as g

    repo, bare = _init_repo(tmp_path)
    # 模拟 pull-back 材化：untracked 同名文件已在工作树
    (Path(repo) / "new.txt").write_text("stale pullback content\n", encoding="utf-8")
    monkeypatch.setattr(g, "_git_push_remote_url", lambda: bare)

    branch, err = g.push_merged_diff_branch(repo, _NEW_FILE_DIFF, "task-d34", base_ref="main")
    assert branch, f"untracked 材化文件不应让 L3 push 失败: {err}"

    # 远端拿到的分支内容 = merged_diff 的内容(基于纯净 base 树)
    sha = _git(bare, "rev-parse", f"refs/heads/{branch}")
    assert _git(bare, "show", f"{sha}:new.txt") == "hello from l3"
    # 工作树零污染：untracked 文件原样、当前分支未动
    assert (Path(repo) / "new.txt").read_text(encoding="utf-8") == "stale pullback content\n"
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_d34_push_uses_pinned_base_commit(monkeypatch, tmp_path):
    """传入钉扎 base_commit(merged_diff 的生成基线) → push 的提交以它为父，
    与 round29 "校验/应用基线与生成基线同源" 口径对齐。"""
    import swarm.brain.l3_gitlab as g

    repo, bare = _init_repo(tmp_path)
    c1 = _git(repo, "rev-parse", "HEAD")
    (Path(repo) / "b.txt").write_text("second\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "second", env=_IDENT)
    monkeypatch.setattr(g, "_git_push_remote_url", lambda: bare)

    branch, err = g.push_merged_diff_branch(
        repo, _NEW_FILE_DIFF, "task-d34b", base_ref="main", base_commit=c1)
    assert branch, err
    sha = _git(bare, "rev-parse", f"refs/heads/{branch}")
    assert _git(bare, "rev-parse", f"{sha}^") == c1, "push 提交必须以钉扎 base 为父"


def test_d34_push_escaping_diff_rejected(monkeypatch, tmp_path):
    """越界路径 diff fail-closed 拒绝(不再经 apply_git_diff 后防线必须补回)。"""
    import swarm.brain.l3_gitlab as g

    repo, bare = _init_repo(tmp_path)
    monkeypatch.setattr(g, "_git_push_remote_url", lambda: bare)
    evil = (
        "diff --git a/../evil.txt b/../evil.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/../evil.txt\n"
        "@@ -0,0 +1 @@\n"
        "+evil\n"
    )
    branch, err = g.push_merged_diff_branch(repo, evil, "task-d34c", base_ref="main")
    assert branch is None and err


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
