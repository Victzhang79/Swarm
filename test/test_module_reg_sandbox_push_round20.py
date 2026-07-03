"""#11(b) 复现+治本：reconcile 的模块注册必须落到【沙箱】读的 pom。

根因（round18/19 实测）：reconcile 用纯 Python `Path.write_text` 改的是【本地 project_path】
的聚合清单；而 L1 build gate（`mvn -pl <mod>`）在【远端沙箱 /workspace】跑，读的是 bootstrap
时上传的旧副本。两份 pom 在同一次 L1 内从不互相同步 → 注册对构建【永久不可见】→ 每次
`Could not find the selected project in the reactor`（reconcile 明明 log 了"补注册 ruoyi-alarm-sdk"）。

与其它确定性 repair（import/version/goimports 全走 `_run_l1_command` 沙箱内改，对构建可见）对齐：
把 reconcile 改过的清单文件推进沙箱，令 `-pl` 当场可解析。本地模式（无沙箱）build 直接读
project_path → 安全 no-op。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import swarm.worker.l1_pipeline as l1
from swarm.worker.workspace_manifest import reconcile_workspace_manifests


class _FakeManager:
    """记录 sync_files_to_sandbox 调用并把文件真实复制进 sandbox_root（模拟远端沙箱）。"""

    def __init__(self, sandbox_root: Path):
        self.sandbox_root = sandbox_root
        self.calls: list[list[str]] = []

    def sync_files_to_sandbox(self, sandbox, local_root, rel_files, remote_root):
        self.calls.append(list(rel_files))
        uploaded = 0
        for rel in rel_files:
            src = Path(local_root) / rel
            dst = self.sandbox_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_file():
                shutil.copy2(src, dst)
                uploaded += 1
        return {"uploaded": uploaded, "errors": [], "files": list(rel_files)}


def _mk_project(root: Path) -> None:
    """RuoYi 基线：根 pom 已注册 ruoyi-alarm，但【缺】上游脚手架新建的 ruoyi-alarm-sdk。"""
    (root / "pom.xml").write_text(
        "<project>\n  <modules>\n    <module>ruoyi-alarm</module>\n"
        "  </modules>\n</project>\n", "utf-8")
    (root / "ruoyi-alarm-sdk").mkdir(parents=True)
    (root / "ruoyi-alarm-sdk" / "pom.xml").write_text(
        "<project>\n  <parent><artifactId>ruoyi</artifactId></parent>\n"
        "  <artifactId>ruoyi-alarm-sdk</artifactId>\n</project>\n", "utf-8")


def test_reconcile_registration_reaches_sandbox(tmp_path, monkeypatch):
    local = tmp_path / "local"
    local.mkdir()
    sandbox_root = tmp_path / "sandbox"
    sandbox_root.mkdir()
    _mk_project(local)
    # 沙箱起始 pom = bootstrap 上传的旧副本（同样无 sdk）——就是 build gate 会读的那份
    (sandbox_root / "pom.xml").write_text(
        (local / "pom.xml").read_text(), "utf-8")

    mgr = _FakeManager(sandbox_root)
    monkeypatch.setattr(l1, "_sandbox_ctx", lambda: (object(), mgr, "/workspace"))

    wm = reconcile_workspace_manifests(
        str(local), ["ruoyi-alarm-sdk/src/main/java/com/x/A.java"])
    # 本地已被 reconcile 注册
    assert wm["added"].get("pom.xml") == ["ruoyi-alarm-sdk"]
    assert "<module>ruoyi-alarm-sdk</module>" in (local / "pom.xml").read_text()

    # 复现根因：push 之前，沙箱 pom 仍无 sdk → mvn -pl 必 reactor not found
    assert "<module>ruoyi-alarm-sdk</module>" not in (sandbox_root / "pom.xml").read_text()

    # 治本：把 reconcile 改过的清单推进沙箱
    pushed = l1._push_manifests_to_sandbox(str(local), wm["modified_manifests"])
    assert pushed == 1
    assert mgr.calls == [["pom.xml"]]
    # 沙箱 pom 现在收到注册 → -pl ruoyi-alarm-sdk 可解析
    assert "<module>ruoyi-alarm-sdk</module>" in (sandbox_root / "pom.xml").read_text()


def test_push_is_noop_without_sandbox(tmp_path, monkeypatch):
    """本地模式（无活跃沙箱）：build 直接读 project_path，无需 push → 安全 no-op（返回 0）。"""
    local = tmp_path / "local"
    local.mkdir()
    _mk_project(local)
    monkeypatch.setattr(l1, "_sandbox_ctx", lambda: None)
    assert l1._push_manifests_to_sandbox(str(local), ["pom.xml"]) == 0


def test_push_empty_manifests_noop(tmp_path, monkeypatch):
    """reconcile 没改任何清单 → 不调用 sync（省一次 no-op 上传）。"""
    local = tmp_path / "local"
    local.mkdir()
    _mk_project(local)
    mgr = _FakeManager(tmp_path / "sandbox")
    (tmp_path / "sandbox").mkdir()
    monkeypatch.setattr(l1, "_sandbox_ctx", lambda: (object(), mgr, "/workspace"))
    assert l1._push_manifests_to_sandbox(str(local), []) == 0
    assert mgr.calls == []


def test_push_survives_sync_failure(tmp_path, monkeypatch):
    """沙箱 sync 抛错（infra 瞬时）→ helper 不致命（返回 0），交后续 build 失败分类处理。"""
    local = tmp_path / "local"
    local.mkdir()
    _mk_project(local)

    class _BoomManager:
        def sync_files_to_sandbox(self, *a, **k):
            raise RuntimeError("envd 5xx")

    monkeypatch.setattr(l1, "_sandbox_ctx", lambda: (object(), _BoomManager(), "/workspace"))
    assert l1._push_manifests_to_sandbox(str(local), ["pom.xml"]) == 0
