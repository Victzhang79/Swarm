"""Batch 1：本地项目上传远程沙箱前的凭据与符号链接安全闸。"""

from __future__ import annotations

import io
import os
import tarfile

import pytest

from swarm.config.settings import SandboxConfig
from swarm.worker.sandbox import CodeResult, SandboxManager


class _FakeFiles:
    def __init__(self) -> None:
        self.written: dict[str, bytes] = {}

    def write(self, path: str, data: bytes | str) -> None:
        self.written[path] = data if isinstance(data, bytes) else data.encode()

    def make_dir(self, path: str) -> None:
        return None


class _FakeSandbox:
    sandbox_id = "sb-sync-security"

    def __init__(self) -> None:
        self.files = _FakeFiles()


def _manager(monkeypatch) -> SandboxManager:
    # 本用例只验证同步公共接口；隔离构造期的真实 sidecar/secret-store 外部依赖。
    monkeypatch.setattr(SandboxManager, "_setup_env", lambda self: None)
    monkeypatch.setattr(SandboxManager, "_init_sidecar", lambda self: None)
    return SandboxManager(SandboxConfig(api_url="http://sandbox.invalid", default_template="tpl"))


def _uploaded_business_paths(sandbox: _FakeSandbox) -> set[str]:
    tar_payloads = [data for path, data in sandbox.files.written.items() if path.endswith(".tar.gz")]
    if tar_payloads:
        with tarfile.open(fileobj=io.BytesIO(tar_payloads[-1]), mode="r:gz") as archive:
            return {member.name for member in archive.getmembers()}
    prefix = "/workspace/"
    return {
        path[len(prefix):]
        for path in sandbox.files.written
        if path.startswith(prefix)
    }


@pytest.mark.parametrize("tar_enabled", [True, False], ids=["tar", "per-file"])
def test_full_sync_excludes_credentials_and_symlinks_but_keeps_build_assets(
    tmp_path, monkeypatch, tar_enabled,
):
    """全量上传的 tar/逐文件两通道共用安全裁决，绝不外送凭据或跟随 symlink。"""
    project = tmp_path / "project"
    project.mkdir()
    keep = {
        ".env.example": "TOKEN=placeholder\n",
        ".yarnrc.yml": "nodeLinker: node-modules\n",
        ".bazelrc": "build --color=yes\n",
        ".buckconfig": "[project]\n  ignore = .git\n",
        ".swiftlint.yml": "disabled_rules: [trailing_whitespace]\n",
        ".mvn/wrapper/maven-wrapper.properties": "distributionUrl=https://example.invalid/maven.zip\n",
        ".yarn/releases/yarn-4.cjs": "// build tool\n",
        "src/main.py": "print('ok')\n",
    }
    drop = {
        ".env": "TOKEN=real-secret\n",
        ".npmrc": "//registry.invalid/:_authToken=real-secret\n",
        "id_rsa": "private-key-material\n",
        ".ssh/config": "IdentityFile ~/.ssh/id_rsa\n",
        "server.key": "-----BEGIN " + "PRIVATE KEY-----\nreal-material\n",
        "src/disguised.py": (
            "API_" + "KEY = '" + "sk-" + "proj-aaaaaaaaaaaaaaaaaaaaaaaaaaaa'\n"
        ),
    }
    for rel, text in {**keep, **drop}.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must-never-leave-host\n", encoding="utf-8")
    link = project / "src" / "outside-link.txt"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("当前平台不支持符号链接")

    monkeypatch.setenv("SWARM_SANDBOX_TAR_SYNC", "true" if tar_enabled else "false")
    manager = _manager(monkeypatch)
    sandbox = _FakeSandbox()
    monkeypatch.setattr(
        manager,
        "run_command",
        lambda sandbox, command, timeout=120, **kwargs: CodeResult(stdout="", success=True),
    )

    stats = manager.sync_project_to_sandbox(sandbox, project, remote_root="/workspace")
    uploaded = _uploaded_business_paths(sandbox)

    assert set(keep) <= uploaded
    assert not (set(drop) & uploaded), f"凭据文件进入远程沙箱：{sorted(set(drop) & uploaded)}"
    assert "src/outside-link.txt" not in uploaded, "符号链接不应进入远程沙箱"
    assert b"must-never-leave-host" not in b"".join(sandbox.files.written.values())
    assert stats["complete"] is False  # 唯一阻断项是无法安全跟随的 symlink
    assert stats["security_skip_reasons"] == {
        "sensitive_filename": 3,
        "credential_dir": 1,
        "secret_content:Private Key": 1,
        "secret_content:OpenAI project key": 1,
        "outside_root": 1,
    }
    assert {item["path"] for item in stats["excluded_paths"]} == {
        ".env", ".npmrc", "id_rsa", ".ssh/config",
    }
    assert {item["path"] for item in stats["blocked_paths"]} == {
        "server.key", "src/disguised.py", "src/outside-link.txt",
    }


def test_full_sync_fails_closed_when_credential_guard_is_unavailable(
    tmp_path, monkeypatch, caplog,
):
    """安全判据异常时不把“判不出”当“安全”，并留下机读拒绝原因。"""
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('safe-looking')\n", encoding="utf-8")

    from swarm.knowledge import ingest_guard

    def unavailable(_rel: str) -> str | None:
        raise RuntimeError("guard unavailable")

    monkeypatch.setattr(ingest_guard, "explicit_credential_reject_reason", unavailable)
    monkeypatch.setenv("SWARM_SANDBOX_TAR_SYNC", "false")
    manager = _manager(monkeypatch)
    sandbox = _FakeSandbox()

    stats = manager.sync_project_to_sandbox(sandbox, project, remote_root="/workspace")

    assert stats["uploaded"] == 0
    assert stats["complete"] is False
    assert stats["security_skip_reasons"] == {"credential_guard_unavailable": 1}
    assert stats["blocked_paths"] == [
        {"path": "main.py", "reason": "credential_guard_unavailable"}
    ]
    assert "fail-closed" in caplog.text


def test_full_sync_fails_closed_when_content_scanner_is_unavailable(
    tmp_path, monkeypatch,
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('safe-looking')\n", encoding="utf-8")

    from swarm.knowledge import ingest_guard

    def unavailable(_text: str):
        raise RuntimeError("scanner unavailable")

    monkeypatch.setattr(ingest_guard, "content_secret_hits", unavailable)
    monkeypatch.setenv("SWARM_SANDBOX_TAR_SYNC", "false")
    sandbox = _FakeSandbox()
    stats = _manager(monkeypatch).sync_project_to_sandbox(
        sandbox, project, remote_root="/workspace",
    )

    assert stats["complete"] is False
    assert stats["blocked_paths"] == [
        {"path": "main.py", "reason": "content_guard_unavailable"}
    ]


def test_full_sync_fails_closed_when_content_scanner_contract_is_invalid(
    tmp_path, monkeypatch,
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('safe-looking')\n", encoding="utf-8")
    monkeypatch.setattr(
        "swarm.knowledge.ingest_guard.content_secret_hits", lambda _text: "not-a-hit-list",
    )
    monkeypatch.setenv("SWARM_SANDBOX_TAR_SYNC", "false")

    stats = _manager(monkeypatch).sync_project_to_sandbox(
        _FakeSandbox(), project, remote_root="/workspace",
    )

    assert stats["complete"] is False
    assert stats["blocked_paths"] == [
        {"path": "main.py", "reason": "content_guard_invalid"}
    ]


def test_targeted_sync_blocks_credentials_but_copies_verified_internal_symlink(
    tmp_path, monkeypatch,
):
    """精准上传阻断凭据；根内 symlink 按已验证目标字节安全复制。"""
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project / "safe.py").write_text("TOKEN = '${TOKEN}'\n", encoding="utf-8")
    (project / "secret.py").write_text(
        "payload = '-----BEGIN " + "OPENSSH PRIVATE KEY-----'\n", encoding="utf-8",
    )
    (project / ".env").write_text("TOKEN=real-secret\n", encoding="utf-8")
    try:
        os.symlink(project / "main.py", project / "main-link.py")
    except OSError:
        pytest.skip("当前平台不支持符号链接")

    monkeypatch.setenv("SWARM_SANDBOX_TAR_SYNC", "false")
    manager = _manager(monkeypatch)
    sandbox = _FakeSandbox()

    stats = manager.sync_files_to_sandbox(
        sandbox, project, ["main.py", "safe.py", "secret.py", ".env", "main-link.py"],
        remote_root="/workspace",
    )

    assert _uploaded_business_paths(sandbox) == {"main.py", "safe.py", "main-link.py"}
    assert stats["complete"] is False
    assert stats["security_skip_reasons"] == {
        "secret_content:Private Key": 1,
        "sensitive_filename": 1,
    }
    assert {item["path"] for item in stats["blocked_paths"]} == {
        "secret.py", ".env",
    }


def test_safe_sync_returns_explicit_complete_contract(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setenv("SWARM_SANDBOX_TAR_SYNC", "false")

    sandbox = _FakeSandbox()
    stats = _manager(monkeypatch).sync_project_to_sandbox(
        sandbox, project, remote_root="/workspace",
    )

    assert stats["complete"] is True
    assert stats["blocked_paths"] == []


def test_full_sync_secret_content_and_large_make_snapshot_incomplete(
    tmp_path, monkeypatch,
):
    """内容密钥/大文件可能是构建输入，省略后不能声称全量快照完整。"""
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project / ".env").write_text("TOKEN=real-secret\n", encoding="utf-8")
    (project / "server.key").write_text(
        "-----BEGIN " + "PRIVATE KEY-----\nreal-material\n", encoding="utf-8",
    )
    (project / "large.dat").write_bytes(b"x" * 32)
    monkeypatch.setattr("swarm.worker.sandbox.MAX_SYNC_FILE_SIZE", 16)
    monkeypatch.setenv("SWARM_SANDBOX_TAR_SYNC", "false")

    sandbox = _FakeSandbox()
    stats = _manager(monkeypatch).sync_project_to_sandbox(
        sandbox, project, remote_root="/workspace",
    )

    assert stats["complete"] is False
    assert stats["excluded_paths"] == [
        {"path": ".env", "reason": "sensitive_filename"},
    ]
    assert {item["path"] for item in stats["blocked_paths"]} == {
        "server.key", "large.dat",
    }
    assert _uploaded_business_paths(sandbox) == {"main.py"}


def test_targeted_large_requested_file_is_incomplete(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "large.dat").write_bytes(b"x" * 32)
    monkeypatch.setattr("swarm.worker.sandbox.MAX_SYNC_FILE_SIZE", 16)

    stats = _manager(monkeypatch).sync_files_to_sandbox(
        _FakeSandbox(), project, ["large.dat"], remote_root="/workspace",
    )

    assert stats["complete"] is False
    assert stats["blocked_paths"] == [{"path": "large.dat", "reason": "large"}]


def test_internal_symlink_is_uploaded_from_verified_target_snapshot(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "real.py").write_text("print('inside')\n", encoding="utf-8")
    try:
        os.symlink("real.py", project / "alias.py")
    except OSError:
        pytest.skip("当前平台不支持符号链接")
    monkeypatch.setenv("SWARM_SANDBOX_TAR_SYNC", "false")
    sandbox = _FakeSandbox()

    stats = _manager(monkeypatch).sync_project_to_sandbox(
        sandbox, project, remote_root="/workspace",
    )

    assert stats["complete"] is True
    assert _uploaded_business_paths(sandbox) == {"real.py", "alias.py"}
    assert sandbox.files.written["/workspace/alias.py"] == b"print('inside')\n"


@pytest.mark.parametrize("targeted", [False, True], ids=["full", "targeted"])
@pytest.mark.parametrize("tar_enabled", [False, True], ids=["per-file", "tar"])
def test_internal_symlink_cannot_alias_explicit_credential_target(
    tmp_path, monkeypatch, targeted, tar_enabled,
):
    """普通链接名不能把根内 .env 的低熵口令绕过路径凭据闸上传。"""
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text(
        "DATABASE_" + "PASSWORD=hunter2\n", encoding="utf-8",
    )
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project / ".ssh").mkdir()
    (project / ".ssh" / "config").write_text(
        "Host internal\n  PasswordAuthentication yes\n", encoding="utf-8",
    )
    try:
        os.symlink(".env", project / "config.txt")
        os.symlink(".ssh/config", project / "ssh-config.txt")
    except OSError:
        pytest.skip("当前平台不支持符号链接")
    monkeypatch.setenv("SWARM_SANDBOX_TAR_SYNC", "true" if tar_enabled else "false")
    sandbox = _FakeSandbox()
    manager = _manager(monkeypatch)
    monkeypatch.setattr(manager, "_TAR_SYNC_MIN_FILES", 1)
    monkeypatch.setattr(
        manager,
        "run_command",
        lambda sandbox, command, timeout=120, **kwargs: CodeResult(
            stdout="", success=True,
        ),
    )

    if targeted:
        stats = manager.sync_files_to_sandbox(
            sandbox, project, ["main.py", "config.txt", "ssh-config.txt"],
            remote_root="/workspace",
        )
    else:
        stats = manager.sync_project_to_sandbox(
            sandbox, project, remote_root="/workspace",
        )

    assert _uploaded_business_paths(sandbox) == {"main.py"}
    assert b"DATABASE_" + b"PASSWORD=hunter2" not in b"".join(
        sandbox.files.written.values()
    )
    assert stats["complete"] is False
    assert {item["path"] for item in stats["blocked_paths"]} == {
        "config.txt", "ssh-config.txt",
    }
    assert {item["reason"] for item in stats["blocked_paths"]} == {
        "symlink_target_sensitive_filename", "symlink_target_credential_dir",
    }


@pytest.mark.parametrize("suffix", [".png", ".zip"])
def test_full_sync_excluded_binary_never_becomes_blocking_input(
    tmp_path, monkeypatch, suffix,
):
    """全量同步的显式扩展名排除必须先于大小与内容扫描。"""
    project = tmp_path / "project"
    project.mkdir()
    payload = b"-----BEGIN " + b"PRIVATE KEY-----\n" + (b"x" * 64)
    (project / f"asset{suffix}").write_bytes(payload)
    monkeypatch.setattr("swarm.worker.sandbox.MAX_SYNC_FILE_SIZE", 16)
    monkeypatch.setenv("SWARM_SANDBOX_TAR_SYNC", "false")

    stats = _manager(monkeypatch).sync_project_to_sandbox(
        _FakeSandbox(), project, remote_root="/workspace",
    )

    assert stats["complete"] is True
    assert stats["blocked_paths"] == []


def test_symlink_inside_excluded_directory_is_ignored(tmp_path, monkeypatch):
    project = tmp_path / "project"
    excluded = project / "node_modules" / "pkg"
    excluded.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    try:
        os.symlink(outside, excluded / "escape.txt")
    except OSError:
        pytest.skip("当前平台不支持符号链接")
    monkeypatch.setenv("SWARM_SANDBOX_TAR_SYNC", "false")

    stats = _manager(monkeypatch).sync_project_to_sandbox(
        _FakeSandbox(), project, remote_root="/workspace",
    )

    assert stats["complete"] is True
    assert stats["blocked_paths"] == []


def test_path_replaced_after_policy_check_never_leaks_outside_bytes(
    tmp_path, monkeypatch,
):
    """路径裁决后被替换成根外 symlink，fd 读取必须拒绝，不能读到攻击者目标。"""
    project = tmp_path / "project"
    project.mkdir()
    victim = project / "victim.txt"
    victim.write_text("safe\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("must-never-upload\n", encoding="utf-8")
    from swarm.knowledge import ingest_guard

    original = ingest_guard.explicit_credential_reject_reason

    def replace_after_check(rel: str):
        result = original(rel)
        if rel == "victim.txt":
            victim.unlink()
            os.symlink(outside, victim)
        return result

    monkeypatch.setattr(
        ingest_guard, "explicit_credential_reject_reason", replace_after_check)
    monkeypatch.setenv("SWARM_SANDBOX_TAR_SYNC", "false")
    sandbox = _FakeSandbox()

    stats = _manager(monkeypatch).sync_project_to_sandbox(
        sandbox, project, remote_root="/workspace",
    )

    assert stats["complete"] is False
    assert stats["blocked_paths"] == [
        {"path": "victim.txt", "reason": "outside_root"}
    ]
    assert b"must-never-upload" not in b"".join(sandbox.files.written.values())
