"""#29-5 W-2：依赖承载清单共享保护的判据更正（「是否列成员」→「是否多写者写依赖」）
+ go.mod require 并集臂。

判据层（sandbox.py `_is_shared_manifest`）：根 go.mod 与根 package.json（无论有无
workspaces）都是 N 个并行 worker（sibling_dep_repair A2 注入）写依赖条目的目标 ⇒
纳入共享清单（flock+并集）；嵌套 go.mod / 嵌套 package.json 维持原档（各 worker
独占，不扩锁面）。
合并层（workspace_manifest.py `_merge_go_manifest`）：分类翻转后 go.mod 才路由进
`merge_shared_manifest`——require 并集臂与分类档位是同一个洞的两面，同批落地
（血规 10①），否则只剩「锁+盲覆盖」仍丢修复（R48c-1 死法换栈复发）。
"""

import logging

import pytest

from swarm.worker.sandbox import _is_shared_manifest, _is_shared_manifest_on_disk
from swarm.worker.workspace_manifest import merge_shared_manifest


# ── 判据层：分类档位 ────────────────────────────────────────────────────────


class TestGoModClassification:
    """根 go.mod 入共享集；嵌套 go.mod 维持独占档（不扩锁面）。"""

    def test_root_go_mod_is_shared(self):
        assert _is_shared_manifest("go.mod") is True
        # content 参数对 go.mod 无意义（依赖承载与内容无关），带上也不改结论
        assert _is_shared_manifest("go.mod", "module m\n") is True

    def test_nested_go_mod_not_shared(self):
        """多模块工程的子模块 go.mod 各 worker 独占 ⇒ 不纳入（「是否多写者」分档）。"""
        assert _is_shared_manifest("services/api/go.mod") is False
        assert _is_shared_manifest("services/api/go.mod", "module m/api\n") is False

    def test_on_disk_propagates_path_tier(self, tmp_path):
        """on_disk 版委托纯 rel 版：根 go.mod 路径档短路（磁盘无该文件也 True）。"""
        assert _is_shared_manifest_on_disk("go.mod", tmp_path) is True
        assert _is_shared_manifest_on_disk("services/api/go.mod", tmp_path) is False


class TestRootPackageJsonClassification:
    """根 package.json 路径档：单包工程（无 workspaces）同样多写者 ⇒ 一律共享。

    （B7 内容判定的完整翻转面在 test_batch4_sandbox.py 的 B7 用例，这里只锁
    「根档与内容无关」这条新命题。）"""

    @pytest.mark.parametrize("content", [
        None,                                        # 纯 rel 旧调用面
        '{"name": "app", "dependencies": {"a": "1"}}',  # 单包工程（W-2 前不受保护）
        '{"name": "m", "workspaces": ["packages/*"]}',  # 聚合根（B7 原 True 档）
        b"{oops",                                    # 非法 JSON：路径档优先，不解析
    ])
    def test_root_package_json_always_shared(self, content):
        assert _is_shared_manifest("package.json", content) is True

    def test_root_package_json_on_disk_short_circuits(self, tmp_path):
        """根档纯 rel 即 True ⇒ on_disk 不读文件内容（单包工程磁盘内容无关）。"""
        (tmp_path / "package.json").write_text('{"name": "app"}', "utf-8")
        assert _is_shared_manifest_on_disk("package.json", tmp_path) is True


# ── 合并层：go.mod require 并集臂 ──────────────────────────────────────────


_LOCAL_BLOCK = (
    "module example.com/app\n\ngo 1.22\n\n"
    "require (\n\tgithub.com/a/a v1.0.0\n\tgithub.com/b/b v2.0.0\n)\n"
)
_INC_BLOCK = "module example.com/app\n\ngo 1.22\n\nrequire (\n\tgithub.com/a/a v1.0.0\n)\n"


class TestGoModUnion:
    def test_block_union_merges_local_only_require(self):
        """核心疗效：local 独有的 require 并回 incoming 的 block（原死法=盲覆盖蒸发）。"""
        merged = merge_shared_manifest(_LOCAL_BLOCK, _INC_BLOCK, "go.mod")
        assert "github.com/b/b v2.0.0" in merged
        assert "github.com/a/a v1.0.0" in merged  # incoming 既有条目原样保留
        # 并入位置=既有 require block 内（与写者 _inject_go 同形状），不臆造新结构
        assert merged.count("require (") == 1

    def test_no_block_appends_single_line_requires(self):
        """incoming 无 require block → 逐条追加单行 require（与写者 else 分支同形状）。"""
        inc = "module example.com/app\n\ngo 1.22\n"
        merged = merge_shared_manifest(_LOCAL_BLOCK, inc, "go.mod")
        assert "require github.com/a/a v1.0.0\n" in merged
        assert "require github.com/b/b v2.0.0\n" in merged

    def test_version_conflict_keeps_incoming(self, caplog):
        """版本冲突保留 incoming（只补缺不改值，与 npm/cargo 同口径）。
        ★区分力在 WARNING 上★：冲突必须走 skip 分支【安静】返回 incoming——若退化成
        「插入重复 require 再被端状态对账打掉」，返回值相同但会打对账 WARNING（实测
        突变 e：删 skip 后返回值不变、仅 WARNING 有别 ⇒ 无此断言则该突变仍绿）。"""
        local = "module m\n\nrequire github.com/a/a v9.9.9\n"
        inc = "module m\n\nrequire github.com/a/a v1.0.0\n"
        with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
            merged = merge_shared_manifest(local, inc, "go.mod")
        assert merged == inc
        assert not any("对账不过" in r.message for r in caplog.records), \
            "正常版本冲突必须走 skip 安静返回，不得退化到对账兜底: " \
            f"{[r.message for r in caplog.records]}"

    def test_replace_accompanied_not_merged_warns(self, caplog):
        """replace 伴随（本地模块）坐标不可移植 ⇒ 诚实不并 + WARNING（缺席可辨）。"""
        local = ("module m\n\nrequire github.com/x/y v1.0.0\n\n"
                 "replace github.com/x/y => ../y\n")
        inc = "module m\n\ngo 1.22\n"
        with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
            merged = merge_shared_manifest(local, inc, "go.mod")
        assert merged == inc
        assert any("不可移植" in r.message for r in caplog.records), \
            f"replace 伴随不并必须留 WARNING: {[r.message for r in caplog.records]}"

    def test_indirect_comment_not_carried_and_conflict_on_existing(self):
        """`// indirect` 注释不移植（tidy 自行重算）；incoming 已有同 mod（带注释）
        ⇒ 冲突保留 incoming。"""
        local = "module m\n\nrequire (\n\tgithub.com/x/y v1.2.3 // indirect\n)\n"
        inc = "module m\n\nrequire (\n\tgithub.com/x/y v1.2.3 // indirect\n)\n"
        assert merge_shared_manifest(local, inc, "go.mod") == inc
        # incoming 无该 mod ⇒ 并入，注释不带走
        inc2 = "module m\n\ngo 1.22\n"
        merged = merge_shared_manifest(local, inc2, "go.mod")
        assert "github.com/x/y v1.2.3" in merged
        assert "indirect" not in merged

    def test_exclude_block_is_not_a_require_source(self):
        """exclude 块不算声明来源（写者 _parse_go 同口径）⇒ 无可并，原样返回。"""
        local = "module m\n\nexclude github.com/x/y v1.0.0\n"
        inc = "module m\n\ngo 1.22\n"
        assert merge_shared_manifest(local, inc, "go.mod") == inc

    def test_nothing_to_merge_returns_incoming_verbatim(self):
        """local 的 require 全在 incoming ⇒ 原样返回（零 diff churn）。"""
        assert merge_shared_manifest(_INC_BLOCK, _LOCAL_BLOCK, "go.mod") == _LOCAL_BLOCK
        # local 无 require 区 → 原样返回
        assert merge_shared_manifest("module m\n", _INC_BLOCK, "go.mod") == _INC_BLOCK

    def test_identical_inputs_short_circuit(self):
        assert merge_shared_manifest(_LOCAL_BLOCK, _LOCAL_BLOCK, "go.mod") == _LOCAL_BLOCK

    def test_dangling_require_block_fail_open(self, caplog):
        """incoming 的 `require (` 悬在 EOF 未闭合（畸形）⇒ fail-open 不碰 + WARNING。"""
        bad = "module m\n\nrequire ("
        with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
            merged = merge_shared_manifest(_LOCAL_BLOCK, bad, "go.mod")
        assert merged == bad
        assert any("悬在 EOF" in r.message for r in caplog.records)

    def test_endstate_reconciliation_fail_open(self, monkeypatch, caplog):
        """端状态对账：重解析 merged 摘掉并入键后与 incoming 对不上 ⇒ fail-open 返
        incoming + WARNING（诚实不并优于产毒/伪并）。用 monkeypatch 构造对账失败面
        （真实文本几乎构造不出——它防的是未来改动/病态输入）。"""
        from swarm.worker import sibling_dep_repair
        real = sibling_dep_repair._parse_go

        def fake(text):
            if text in (_LOCAL_BLOCK, _INC_BLOCK):
                return real(text)
            return {}  # merged 重解析被污染 ⇒ _stripped={} ≠ inc ⇒ 对账必须失败

        monkeypatch.setattr(sibling_dep_repair, "_parse_go", fake)
        with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
            merged = merge_shared_manifest(_LOCAL_BLOCK, _INC_BLOCK, "go.mod")
        assert merged == _INC_BLOCK, "对账不过必须 fail-open 返回 incoming"
        assert any("对账不过" in r.message for r in caplog.records), \
            f"对账失败必须留 WARNING: {[r.message for r in caplog.records]}"

    def test_parse_exception_fail_open(self, monkeypatch, caplog):
        """解析层异常 ⇒ fail-open 回退旧行为（盲覆盖）+ WARNING，绝不炸 pull-back。"""
        from swarm.worker import sibling_dep_repair

        def boom(text):
            raise RuntimeError("模拟解析崩溃")

        monkeypatch.setattr(sibling_dep_repair, "_parse_go", boom)
        with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
            merged = merge_shared_manifest(_LOCAL_BLOCK, _INC_BLOCK, "go.mod")
        assert merged == _INC_BLOCK
        assert any("fail-open" in r.message for r in caplog.records)

    def test_nested_rel_path_also_merges(self):
        """合并臂是纯文本合并不看路径——嵌套 rel 也走臂（分类器保证它不会被路由
        进来；臂自身不设防，与 cargo 臂同例）。"""
        merged = merge_shared_manifest(_LOCAL_BLOCK, _INC_BLOCK, "services/api/go.mod")
        assert "github.com/b/b v2.0.0" in merged


# ── R1 双复核整改面 ────────────────────────────────────────────────────────


class TestParseBlindSpotWarns:
    """hunter F2：`_parse_go` 合法语法盲区 ⇒ 「解析为空」与「真没有 require」必须机读可分。"""

    def test_trailing_comment_require_block_warns(self, caplog):
        """`require ( // 尾随注释` 是合法 go.mod 而解析返 {} ⇒ 不并 + WARNING
        （此前 local 全部 require 被盲覆盖蒸发且零信号）。"""
        local = "module m\n\nrequire ( // pinned\n\tgithub.com/b/b v2.0.0\n)\n"
        with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
            merged = merge_shared_manifest(local, _INC_BLOCK, "go.mod")
        assert merged == _INC_BLOCK
        assert any("解析为空" in r.message for r in caplog.records), \
            f"解析盲区必须留 WARNING: {[r.message for r in caplog.records]}"

    def test_no_slash_module_path_warns(self, caplog):
        """无斜杠 module path（`require mymod v1.0.0`）同盲区 ⇒ WARNING。"""
        local = "module m\n\nrequire mymod v1.0.0\n"
        with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
            merged = merge_shared_manifest(local, _INC_BLOCK, "go.mod")
        assert merged == _INC_BLOCK
        assert any("解析为空" in r.message for r in caplog.records)

    def test_genuinely_requireless_local_is_quiet(self, caplog):
        """local 真没有 require ⇒ 安静返回 incoming（「真没有」不得误报）。
        R2 hunter N1：判据必须是行首 require 语句——注释含 "require" 字样的变体
        也不得冤报（子串判据的实测误报面）。"""
        for local in ("module m\n\ngo 1.22\n",
                      "module m\n\n// require block intentionally empty\n"):
            with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
                merged = merge_shared_manifest(local, _INC_BLOCK, "go.mod")
            assert merged == _INC_BLOCK
            assert not any("解析为空" in r.message for r in caplog.records), \
                f"注释含 require 字样不得冤报: {local!r}"
            caplog.clear()


class TestExcludeContradictionWarns:
    """reviewer LOW-1：并入键命中 incoming 的 exclude ⇒ require+exclude 必败，留 WARNING。"""

    def test_exclude_hit_still_merges_but_warns(self, caplog):
        local = "module m\n\nrequire github.com/x/y v2.0.0\n"
        inc = "module m\n\nexclude github.com/x/y v2.0.0\n"
        with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
            merged = merge_shared_manifest(local, inc, "go.mod")
        assert "require github.com/x/y v2.0.0" in merged  # 加法-only 语义不变
        assert any("exclude" in r.message and "github.com/x/y" in r.message
                   for r in caplog.records), \
            f"exclude 矛盾必须留 WARNING: {[r.message for r in caplog.records]}"

    def test_exclude_block_form_also_seen(self, caplog):
        """exclude 块内形态同样被增益扫描看见。"""
        local = "module m\n\nrequire github.com/x/y v2.0.0\n"
        inc = "module m\n\nexclude (\n\tgithub.com/x/y v2.0.0\n)\n"
        with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
            merge_shared_manifest(local, inc, "go.mod")
        assert any("exclude" in r.message for r in caplog.records)

    def test_exclude_different_version_is_quiet(self, caplog):
        """R2 hunter N2：exclude 按【版本】排除——`exclude x/y v1` + 并入 `x/y v2`
        完全合法 ⇒ 绝不冤报「必败」（版本粒度丢失的实测误报面）。仍并入。"""
        local = "module m\n\nrequire github.com/x/y v2.0.0\n"
        inc = "module m\n\nexclude github.com/x/y v1.0.0\n"
        with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
            merged = merge_shared_manifest(local, inc, "go.mod")
        assert "require github.com/x/y v2.0.0" in merged
        assert not any("exclude" in r.message for r in caplog.records), \
            f"不同版本不得冤报: {[r.message for r in caplog.records]}"


class TestBootstrapSnapshotDerivesFromClassifier:
    """reviewer MEDIUM-1 = hunter F1（两票同根）：bootstrap 根清单枚举从分类器派生——
    分类器判 True 的根文件必须进快照，否则 H2 baseline=None 静默 skip。"""

    @staticmethod
    def _run_sync(tmp_path):
        import asyncio
        from swarm.worker.executor_sync import _SandboxSyncMixin

        class _Scope:
            create_files: list = []
            writable: list = []
            readable: list = []

        class _Host:
            project_path = str(tmp_path)
            effective_scope = _Scope()
            _sandbox = None
            _sandbox_manager = None

            class subtask:
                scope = _Scope()

            def _log(self, msg):
                pass

            def _snapshot_scope_local(self, root):
                return {}

        host = _Host()
        asyncio.run(_SandboxSyncMixin._sync_to_sandbox(host, "test"))
        return host._manifest_baseline_snapshot

    def test_root_go_mod_and_package_json_in_snapshot(self, tmp_path):
        """接线锁：根 go.mod/根 package.json（scope 不含它们）经派生枚举进快照。
        （旧手抄 tuple 漏 go.mod ⇒ 本用例当时必红。）"""
        (tmp_path / "go.mod").write_text("module m\n\ngo 1.22\n", "utf-8")
        (tmp_path / "package.json").write_text('{"name": "app"}\n', "utf-8")
        (tmp_path / "app.py").write_text("print(1)\n", "utf-8")
        snap = self._run_sync(tmp_path)
        assert snap.get("go.mod") == "module m\n\ngo 1.22\n"
        assert snap.get("package.json") == '{"name": "app"}\n'
        assert "app.py" not in snap, "非清单文件绝不进快照"

    def test_h2_missing_baseline_warns_and_keeps_file(self, tmp_path, caplog):
        """hunter F1②：H2 对快照未覆盖的清单 skip 时必须留 WARNING（缺席可辨），
        且 skip 语义不变（宁可漏回滚绝不误删）。"""
        import subprocess as sp
        sp.run(["git", "init", "-q", str(tmp_path)], check=True)
        (tmp_path / "go.mod").write_text("module m\n", "utf-8")
        sp.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
        sp.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c",
                "user.name=t", "commit", "-qm", "base"], check=True)
        worker_txt = "module m\n\nrequire github.com/x/y v1.0.0\n"
        (tmp_path / "go.mod").write_text(worker_txt, "utf-8")

        class _Host:
            project_path = str(tmp_path)
            base_ref = None
            _post_sync_contents = {"go.mod": worker_txt}
            _manifest_baseline_snapshot = {}  # 快照未覆盖（整改前的枚举漏项形态）

            class subtask:
                class scope:
                    create_files = ["go.mod"]
                    writable = []

            def _log(self, msg):
                pass

        from swarm.worker.executor_sync import _SandboxSyncMixin
        with caplog.at_level(logging.WARNING, logger="swarm.worker.executor_sync"):
            _SandboxSyncMixin._rollback_failed_manifest_footprint(_Host(), {})
        assert any("无此清单基线" in r.message for r in caplog.records), \
            f"baseline 缺失 skip 必须留 WARNING: {[r.message for r in caplog.records]}"
        assert (tmp_path / "go.mod").read_text("utf-8") == worker_txt, \
            "skip 语义不变：无法归因绝不动文件"

    def test_h2_dot_slash_create_files_still_rollback_deleted(self, tmp_path):
        """★复核 R1 HIGH（reviewer 坐实）★create_files 带 "./" 前缀（LLM 常见写法）时
        _own_creates 与 rels/manifests 口径必须同源——治前 rels 已 _norm_rel（剥 "./"）
        而 _own_creates 只 lstrip("/") ⇒ 「本任务创建」判定失败 ⇒ 真 FAIL 子任务新建
        清单残留共享树（F7 改 rels 漏此消费者=半落地族）。"""
        import subprocess as sp
        sp.run(["git", "init", "-q", str(tmp_path)], check=True)
        (tmp_path / "seed.txt").write_text("seed\n", "utf-8")
        sp.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
        sp.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c",
                "user.name=t", "commit", "-qm", "base"], check=True)
        worker_txt = '{"name": "app", "dependencies": {"x": "1.0.0"}}\n'
        (tmp_path / "package.json").write_text(worker_txt, "utf-8")  # worker 新建

        class _Host:
            project_path = str(tmp_path)
            base_ref = None
            _post_sync_contents = {"package.json": worker_txt}
            _manifest_baseline_snapshot = {"package.json": ""}   # bootstrap 时不存在

            class subtask:
                class scope:
                    create_files = ["./package.json"]            # ★带 "./" 前缀
                    writable = []

            def _log(self, msg):
                pass

        from swarm.worker.executor_sync import _SandboxSyncMixin
        _SandboxSyncMixin._rollback_failed_manifest_footprint(_Host(), {})
        assert not (tmp_path / "package.json").exists(), \
            "本任务新建（带 ./ 前缀声明）的清单必须被回滚删除（治前口径 mismatch 残留）"
