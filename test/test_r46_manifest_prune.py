"""R46 治本回归锁：成员条目 ↔ 磁盘存在 双向镜像（prune 侧）+ L2 指纹连击守卫。

round46 实锤：
  R46-1 本地共享树聚合清单原样推进沙箱 → 沙箱缺并行兄弟模块目录 → reactor
        "Child module does not exist" 硬错 → det=None → verification_not_run 判死好产出。
  R46-2 阶梯三 revert 清目录不反注册 root pom <module> → 幽灵条目毒死 L2 集成编译。
  R46-3 同一批 D5 缺失符号指纹 4 连不变，每连白烧一次完整 L2 集成编译。
"""
from __future__ import annotations

from swarm.worker.workspace_manifest import (
    manifest_member_probes,
    prune_manifest_members,
    prune_stale_manifest_members,
    reconcile_workspace_manifests,
)

_POM = """<project>
    <groupId>com.ruoyi</groupId>
    <artifactId>ruoyi</artifactId>
    <version>4.8.3</version>
    <modules>
        <module>alarm-engine</module>
        <module>alarm-channel</module>
    </modules>
</project>
"""


class TestManifestMemberProbes:
    def test_pom_probes(self):
        pairs = manifest_member_probes("pom.xml", _POM)
        assert ("alarm-engine", "alarm-engine/pom.xml") in pairs
        assert ("alarm-channel", "alarm-channel/pom.xml") in pairs

    def test_gradle_probes(self):
        pairs = manifest_member_probes("settings.gradle", "include ':app'\ninclude ':lib:core'\n")
        assert ("app", "app") in pairs
        assert ("lib:core", "lib/core") in pairs

    def test_cargo_probes_skip_glob(self):
        text = '[workspace]\nmembers = ["core", "crates/*"]\n'
        pairs = manifest_member_probes("Cargo.toml", text)
        assert ("core", "core") in pairs
        assert all("*" not in tok for tok, _ in pairs)

    def test_gowork_probes(self):
        pairs = manifest_member_probes("go.work", "go 1.22\n\nuse ./svc\n")
        assert ("svc", "svc") in pairs

    def test_sln_not_handled(self):
        assert manifest_member_probes("app.sln", "Project(...)") == []


class TestPruneManifestMembers:
    def test_pom_removes_ghost_keeps_real(self):
        new_text, removed = prune_manifest_members(
            "pom.xml", _POM, lambda p: p.startswith("alarm-engine"))
        assert removed == ["alarm-channel"]
        assert "<module>alarm-channel</module>" not in new_text
        assert "<module>alarm-engine</module>" in new_text

    def test_fail_open_on_unknown(self):
        """探测通道故障(None)绝不误删成员。"""
        new_text, removed = prune_manifest_members("pom.xml", _POM, lambda p: None)
        assert removed == []
        assert new_text == _POM

    def test_all_exist_noop(self):
        new_text, removed = prune_manifest_members("pom.xml", _POM, lambda p: True)
        assert removed == []
        assert new_text == _POM

    def test_gradle_prune(self):
        text = "include ':app'\ninclude ':ghost'\n"
        new_text, removed = prune_manifest_members(
            "settings.gradle", text, lambda p: p == "app")
        assert removed == ["ghost"]
        assert "ghost" not in new_text and "':app'" in new_text

    def test_gowork_prune(self):
        text = "go 1.22\n\nuse ./svc\nuse ./ghost\n"
        new_text, removed = prune_manifest_members("go.work", text, lambda p: p == "svc")
        assert removed == ["ghost"]
        assert "use ./ghost" not in new_text and "use ./svc" in new_text


class TestPruneStaleOnDisk:
    def _mk_tree(self, tmp_path, ghost_entry: bool):
        (tmp_path / "alarm-engine").mkdir()
        (tmp_path / "alarm-engine" / "pom.xml").write_text(
            "<project><parent><groupId>com.ruoyi</groupId></parent>"
            "<artifactId>alarm-engine</artifactId></project>", encoding="utf-8")
        pom = _POM if ghost_entry else _POM.replace(
            "        <module>alarm-channel</module>\n", "")
        (tmp_path / "pom.xml").write_text(pom, encoding="utf-8")

    def test_r46_2_ghost_module_pruned(self, tmp_path):
        """R46-2 回归：revert 删了 alarm-channel 目录但 root pom 条目残留 → 摘除。"""
        self._mk_tree(tmp_path, ghost_entry=True)
        removed = prune_stale_manifest_members(str(tmp_path))
        assert removed.get("pom.xml") == ["alarm-channel"]
        text = (tmp_path / "pom.xml").read_text("utf-8")
        assert "alarm-channel" not in text
        assert "<module>alarm-engine</module>" in text

    def test_reconcile_returns_removed_and_idempotent(self, tmp_path):
        self._mk_tree(tmp_path, ghost_entry=True)
        r1 = reconcile_workspace_manifests(str(tmp_path))
        assert r1["removed"].get("pom.xml") == ["alarm-channel"]
        # 幂等：第二遍无事发生
        r2 = reconcile_workspace_manifests(str(tmp_path))
        assert r2["removed"] == {}
        text = (tmp_path / "pom.xml").read_text("utf-8")
        assert text.count("<module>") == 1

    def test_add_then_prune_do_not_fight(self, tmp_path):
        """add 侧刚补的真实成员绝不能被 prune 侧摘掉。"""
        self._mk_tree(tmp_path, ghost_entry=True)
        # 磁盘新增一个未注册的真实模块
        (tmp_path / "alarm-notify").mkdir()
        (tmp_path / "alarm-notify" / "pom.xml").write_text(
            "<project><parent><groupId>com.ruoyi</groupId></parent>"
            "<artifactId>alarm-notify</artifactId></project>", encoding="utf-8")
        r = reconcile_workspace_manifests(str(tmp_path))
        assert "alarm-notify" in (r["added"].get("pom.xml") or [])
        assert r["removed"].get("pom.xml") == ["alarm-channel"]
        text = (tmp_path / "pom.xml").read_text("utf-8")
        assert "<module>alarm-notify</module>" in text
        assert "alarm-channel" not in text


class TestAdversarialF2Fixes:
    """对抗复核 F2 实测腐蚀面回归锁。"""

    def test_pom_profiles_before_modules(self):
        """F2-3：profiles 块先于主 <modules>——probes/prune 必须锚定主块。"""
        pom = (
            "<project><profiles><profile><modules>\n"
            "        <module>ghost</module>\n"
            "</modules></profile></profiles>\n"
            "<modules>\n        <module>ghost</module>\n        <module>real</module>\n"
            "</modules></project>\n")
        pairs = manifest_member_probes("pom.xml", pom)
        assert ("real", "real/pom.xml") in pairs
        new_text, removed = prune_manifest_members(
            "pom.xml", pom, lambda p: p.startswith("real"))
        assert removed == ["ghost"]
        # profile 块内的同名条目原样保留（绝不误伤），主块的被摘
        assert new_text.count("<module>ghost</module>") == 1
        assert "<profile>" in new_text and "<module>real</module>" in new_text

    def test_gradle_multi_token_line_untouched(self):
        """F2-1：`include ':app', ':core'` 多 token 行整体跳过——绝不截断腐蚀。"""
        text = "include ':app', ':core'\ninclude ':ghost'\n"
        pairs = manifest_member_probes("settings.gradle", text)
        assert [t for t, _ in pairs] == ["ghost"]
        new_text, removed = prune_manifest_members(
            "settings.gradle", text, lambda p: False)
        assert removed == ["ghost"]
        assert "include ':app', ':core'" in new_text

    def test_cargo_path_dep_outside_members_untouched(self):
        """F2-2：members 数组外的同名 path 依赖绝不被删；数组内幽灵被摘。"""
        text = (
            '[dependencies]\nfoo = { path = "crates/foo" }\n\n'
            '[workspace]\nmembers = [\n    "crates/foo",\n    "core",\n]\n')
        new_text, removed = prune_manifest_members(
            "Cargo.toml", text, lambda p: p == "core")
        assert removed == ["crates/foo"]
        assert 'foo = { path = "crates/foo" }' in new_text
        assert '"core",' in new_text

    def test_gradle_comment_line_untouched(self):
        text = "// include ':old'\ninclude ':ghost'\n"
        new_text, removed = prune_manifest_members(
            "settings.gradle", text, lambda p: False)
        assert removed == ["ghost"]
        assert "// include ':old'" in new_text

    def test_reconcile_prune_false_keeps_ghost(self, tmp_path):
        """F4：L1 调用点 prune=False——只补漏不摘幽灵。"""
        (tmp_path / "alarm-engine").mkdir()
        (tmp_path / "alarm-engine" / "pom.xml").write_text(
            "<project><parent><groupId>g</groupId></parent>"
            "<artifactId>alarm-engine</artifactId></project>", encoding="utf-8")
        (tmp_path / "pom.xml").write_text(_POM, encoding="utf-8")
        r = reconcile_workspace_manifests(str(tmp_path), prune=False)
        assert r["removed"] == {}
        assert "alarm-channel" in (tmp_path / "pom.xml").read_text("utf-8")


class TestL2FpAdvance:
    def test_consecutive_and_reset(self):
        from swarm.brain.nodes.verify import _l2_fp_advance
        h, ex = _l2_fp_advance(None, "aaa")
        assert h == ["aaa"] and not ex
        h, ex = _l2_fp_advance(h, "aaa")
        assert len(h) == 2 and not ex
        # 指纹变化 → 重置
        h, ex = _l2_fp_advance(h, "bbb")
        assert h == ["bbb"] and not ex
        h, _ = _l2_fp_advance(h, "bbb")
        h, ex = _l2_fp_advance(h, "bbb")
        assert ex and len(h) == 3
