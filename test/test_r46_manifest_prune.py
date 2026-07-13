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


class TestR48cMergeSharedManifest:
    """R48c-1：pull-back 并集合并——陈旧副本绝不冲掉他人已落盘的依赖/成员。"""

    _LOCAL = """<project>
    <modules>
        <module>ruoyi-system</module>
        <module>alarm-core</module>
    </modules>
    <dependencies>
        <dependency>
            <groupId>org.springframework.data</groupId>
            <artifactId>spring-data-redis</artifactId>
            <version>2.5.0</version>
        </dependency>
    </dependencies>
</project>
"""
    _STALE = """<project>
    <modules>
        <module>ruoyi-system</module>
    </modules>
    <dependencies>
        <dependency>
            <groupId>cn.hutool</groupId>
            <artifactId>hutool-all</artifactId>
        </dependency>
    </dependencies>
</project>
"""

    def test_round48c_repair_survives_stale_overwrite(self):
        from swarm.worker.workspace_manifest import merge_shared_manifest
        merged = merge_shared_manifest(self._LOCAL, self._STALE, "ruoyi-system/pom.xml")
        # incoming 的新依赖保留 + local 的修复依赖并回 + local 的模块成员并回
        assert "hutool-all" in merged
        assert "spring-data-redis" in merged, "防线④修复绝不被陈旧副本冲掉"
        assert "<module>alarm-core</module>" in merged

    def test_incoming_wins_on_same_ga(self):
        from swarm.worker.workspace_manifest import merge_shared_manifest
        local = self._LOCAL.replace("2.5.0", "9.9.9")
        inc = self._LOCAL  # 同 g:a 已存在 → 不重复并入
        merged = merge_shared_manifest(local, inc, "pom.xml")
        assert merged.count("spring-data-redis") == 1
        assert "9.9.9" not in merged

    def test_non_pom_passthrough(self):
        from swarm.worker.workspace_manifest import merge_shared_manifest
        assert merge_shared_manifest("include ':a'", "include ':b'",
                                     "settings.gradle") == "include ':b'"

    def test_incoming_without_dep_section_conservative(self):
        from swarm.worker.workspace_manifest import merge_shared_manifest
        inc = "<project><modules><module>m</module></modules></project>"
        merged = merge_shared_manifest(self._LOCAL, inc, "pom.xml")
        assert "spring-data-redis" not in merged, "incoming 无依赖区不臆造结构"

    def test_dm_block_merged_into_dm(self):
        from swarm.worker.workspace_manifest import merge_shared_manifest
        local = ("<project><dependencyManagement><dependencies><dependency>"
                 "<groupId>g</groupId><artifactId>managed-x</artifactId>"
                 "<version>1</version></dependency></dependencies>"
                 "</dependencyManagement></project>")
        inc = ("<project><dependencyManagement><dependencies><dependency>"
               "<groupId>g</groupId><artifactId>managed-y</artifactId>"
               "<version>2</version></dependency></dependencies>"
               "</dependencyManagement></project>")
        merged = merge_shared_manifest(local, inc, "pom.xml")
        assert "managed-x" in merged and "managed-y" in merged
        dm = merged.split("<dependencyManagement>")[1]
        assert "managed-x" in dm, "dm 块并回 dm 区，不落顶层依赖区"

    def test_idempotent(self):
        from swarm.worker.workspace_manifest import merge_shared_manifest
        m1 = merge_shared_manifest(self._LOCAL, self._STALE, "pom.xml")
        m2 = merge_shared_manifest(self._LOCAL, m1, "pom.xml")
        assert m1 == m2


class TestR48cReviewFixes:
    """R48c-1 对抗复核 A/B/C/4/6 整改回归锁。"""

    def test_b_dm_entry_does_not_block_plain_repair(self):
        """复核 B：incoming 仅 dm 里有 X → local 主区的 X 修复必须并回主区。"""
        from swarm.worker.workspace_manifest import merge_shared_manifest
        local = ("<project><dependencies><dependency>"
                 "<groupId>org.springframework.data</groupId>"
                 "<artifactId>spring-data-redis</artifactId>"
                 "</dependency></dependencies></project>")
        inc = ("<project><dependencyManagement><dependencies><dependency>"
               "<groupId>org.springframework.data</groupId>"
               "<artifactId>spring-data-redis</artifactId><version>2.5.0</version>"
               "</dependency></dependencies></dependencyManagement>"
               "<dependencies><dependency><groupId>x</groupId>"
               "<artifactId>y</artifactId></dependency></dependencies></project>")
        merged = merge_shared_manifest(local, inc, "pom.xml")
        # 主区必须出现 spring-data-redis（dm 声明版本不提供 classpath）
        plain = merged.split("</dependencyManagement>")[1]
        assert "spring-data-redis" in plain

    def test_c_profile_deps_not_collected_nor_polluted(self):
        """复核 C：profile 依赖不外溢主区；插入点不落 profile 区。"""
        from swarm.worker.workspace_manifest import merge_shared_manifest
        local = ("<project><profiles><profile><dependencies><dependency>"
                 "<groupId>p</groupId><artifactId>prof-only</artifactId>"
                 "</dependency></dependencies></profile></profiles></project>")
        inc = ("<project><dependencies><dependency><groupId>x</groupId>"
               "<artifactId>y</artifactId></dependency></dependencies></project>")
        merged = merge_shared_manifest(local, inc, "pom.xml")
        assert "prof-only" not in merged, "profile 条件依赖绝不搬进主区"
        # incoming 无主区、只有 profile 区 → 修复依赖绝不插进 profile
        local2 = ("<project><dependencies><dependency><groupId>g</groupId>"
                  "<artifactId>fix-dep</artifactId></dependency></dependencies></project>")
        inc2 = ("<project><profiles><profile><dependencies><dependency>"
                "<groupId>p</groupId><artifactId>q</artifactId>"
                "</dependency></dependencies></profile></profiles></project>")
        merged2 = merge_shared_manifest(local2, inc2, "pom.xml")
        assert "fix-dep" not in merged2, "无主区保守不并，绝不落 profile"

    def test_4_ghost_module_not_resurrected(self, tmp_path):
        """复核 4：目录已不存在的幽灵成员不经并集复活（base_dir 存在性校验）。"""
        from swarm.worker.workspace_manifest import merge_shared_manifest
        (tmp_path / "alive").mkdir()
        (tmp_path / "alive" / "pom.xml").write_text("<project/>", "utf-8")
        local = ("<project><modules><module>alive</module>"
                 "<module>ghost</module></modules></project>")
        inc = "<project><modules><module>base</module></modules></project>"
        merged = merge_shared_manifest(local, inc, "pom.xml", base_dir=tmp_path)
        assert "<module>alive</module>" in merged
        assert "ghost" not in merged, "幽灵成员（无目录）绝不并回"

    def test_6_crlf_preserved_through_merge(self, tmp_path):
        """复核 6：CRLF local 经 bytes 读取不被剥行尾（== 短路命中）。"""
        pom = tmp_path / "pom.xml"
        text = "<project>\r\n<modules>\r\n</modules>\r\n</project>\r\n"
        pom.write_bytes(text.encode("utf-8"))
        from swarm.worker.sandbox import SandboxManager
        out = SandboxManager._merge_manifest_with_local(
            pom, "pom.xml", text.encode("utf-8"))
        assert out == text.encode("utf-8"), "同内容必须原样短路（bytes 精确）"

    def test_a_diff_reset_merges_shared_manifest(self):
        """复核 A：主干A 自产出重置对共享清单走同一并集内核（陈旧快照不冲修复）。"""
        import inspect
        from swarm.worker import executor_sync
        src = inspect.getsource(executor_sync)
        seg = src[src.index("主干A 治本（并行子任务共享聚合态）"):]
        seg = seg[:seg.index("if untracked:")]
        assert "merge_shared_manifest" in seg, "diff-time 重置写点必须走并集合并"
        assert "_is_shared_manifest" in seg
