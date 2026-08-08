"""#29-2 W-1：A2 兄弟坐标注入的【生产效力】——推进沙箱 + pull-back 并集不抹。

缺陷两段（29 号文 W-1，主 agent 已实测）：
  段① `sibling_dep_repair` 三个 `_inject_*` 全是 `Path.write_text` **纯本地写**，而
      `_attempt_build_repair` 的调用方随后用 `_run_l1_command` 重跑构建——那是**沙箱优先**的
      ⇒ 构建读的是 bootstrap 上传的旧副本 ⇒ **整个 A2 机制对生产 L1 裁决零影响**。
      对照 Maven 侧 `_inject_dependency` 在沙箱内改：同一治本在不同栈效力不等（血规 10①）。
  段② 注入路径进 `repaired_file_paths` → 下次 pull-back 把沙箱副本拉回；对**共享清单**走
      `merge_shared_manifest`，而它当时对 cargo 直接 `return incoming_text`、对 npm 只并
      workspaces 成员不并依赖 ⇒ 并行兄弟的注入被陈旧副本抹掉（R48c-1 死法换栈复发）。

★本文件的测试必须证"被接上了"而非"实现正确"（血规 10②）★：段① 的锁点是
`_attempt_build_repair` 整条生产路径上 A2 触达的清单**真的**进了 sync 调用（把那段 push
删掉就红），而不是单独调 `_push_manifests_to_sandbox` 能工作——后者在 #11(b) 的
test_module_reg_sandbox_push_round20.py 早已锁住，重复断言它对本缺陷零区分力。

诚实边界：go.mod 不在本文件的并集用例里，因为 `_is_shared_manifest('go.mod')` 为 False
⇒ 它根本不进 `merge_shared_manifest`（走裸写分支，段① 修好后沙箱副本已带注入，故不丢）。
其"共享清单分类缺席 + require 并集臂"是同一个洞的两面，登记 #29-5 W-2 同批落地；这里用
test_go_mod_not_routed_through_merge_today 把**当前事实**钉住，翻转时该测试会红=提醒同步。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import swarm.worker.l1_pipeline as l1
from swarm.worker.sandbox import _is_shared_manifest
from swarm.worker.workspace_manifest import merge_shared_manifest


class _FakeManager:
    """记录 sync 调用并把文件真实复制进 sandbox_root（模拟远端沙箱）。"""

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

    def run_command(self, sandbox, cmd, timeout=None):  # 剪枝探针用；本用例无成员可剪
        class _R:
            success = True
            stdout = ""
        return _R()


# ── A. 段①：A2 注入必须推进沙箱（生产路径接线） ──────────────────────────

_GO_MOD_ROOT = """module github.com/acme/svc

go 1.22

require github.com/sirupsen/logrus v1.9.3
"""
_GO_MOD_SIBLING = """module github.com/acme/lib

go 1.22

require github.com/google/uuid v1.6.0
"""
# go build 缺依赖报错（A2 的 _missing_deps 取证源）
_GO_BUILD_ERR = (
    "svc/main.go:7:2: no required module provides package github.com/google/uuid; "
    "to add it:\n\tgo get github.com/google/uuid\n")


def _mk_go_project(root: Path) -> None:
    """根模块缺 uuid，兄弟模块 lib 已有权威坐标 → A2 可自证注入。"""
    (root / "go.mod").write_text(_GO_MOD_ROOT, "utf-8")
    (root / "lib").mkdir(parents=True, exist_ok=True)
    (root / "lib" / "go.mod").write_text(_GO_MOD_SIBLING, "utf-8")
    (root / "main.go").write_text(
        'package main\n\nimport "github.com/google/uuid"\n\n'
        "func main() { _ = uuid.New() }\n", "utf-8")


def _run_repair(local: Path, sandbox_root: Path, monkeypatch,
                *, other_family_paths: list[str] | None = None) -> _FakeManager:
    """跑真 `_attempt_build_repair`（go 栈），返回 FakeManager 以查 sync 调用。

    `other_family_paths`：让**别的修复族**（goimports 等，生产上它们在沙箱里改）也回传
    路径。★这是推送面测试的前提★——若只有 A2 回传路径，`paths` 与 `_a2_paths` 恰好相等，
    "推 paths"与"推 _a2_paths"两种实现行为一致 ⇒ 该测试零区分力（首跑突变实测：把推送面
    换成 paths 后测试仍绿）。
    """
    mgr = _FakeManager(sandbox_root)
    monkeypatch.setattr(l1, "_sandbox_ctx", lambda: (object(), mgr, "/workspace"))
    # 只让 go 生态入选，并按需模拟"沙箱侧已修好并回传路径"的其它修复族
    _other = list(other_family_paths or [])
    monkeypatch.setattr(l1, "_repair_go", lambda *a, **k: (len(_other), _other))
    l1._attempt_build_repair(
        str(local), _GO_BUILD_ERR, ["main.go"], 60,
        project_stack={"languages": ["go"]})
    return mgr


def test_a2_injection_reaches_sandbox(tmp_path, monkeypatch):
    """治本锁：A2 注入的 go.mod 必须出现在沙箱副本里（否则构建看不见=机制零效力）。"""
    local = tmp_path / "local"
    local.mkdir()
    sandbox_root = tmp_path / "sandbox"
    sandbox_root.mkdir()
    _mk_go_project(local)
    # 沙箱起始副本 = bootstrap 上传的旧 go.mod（无 uuid）——build gate 读的就是这份
    (sandbox_root / "go.mod").write_text(_GO_MOD_ROOT, "utf-8")

    mgr = _run_repair(local, sandbox_root, monkeypatch)

    # 前提：A2 真的在本地注入了（否则本用例什么都没测到）
    local_txt = (local / "go.mod").read_text()
    assert "github.com/google/uuid" in local_txt, (
        f"前提不成立：A2 没在本地注入，本用例零区分力。go.mod=\n{local_txt}")
    # 治本断言：沙箱副本也拿到了注入 ⇒ 重跑构建（沙箱优先）看得见
    sb_txt = (sandbox_root / "go.mod").read_text()
    assert "github.com/google/uuid" in sb_txt, (
        "A2 注入未推进沙箱 ⇒ 构建读旧副本 ⇒ 整个 A2 机制对生产 L1 裁决零影响。"
        f"沙箱 go.mod=\n{sb_txt}")
    assert ["go.mod"] in mgr.calls, f"sync 调用未含 go.mod：{mgr.calls}"


def test_push_scope_excludes_non_a2_repair_paths(tmp_path, monkeypatch):
    """★推送面必须只含 A2 自己触达的路径★

    本函数其余修复族（Java import/version/symbol、goimports、cargo fix）都走
    `_run_l1_command` **在沙箱里改**——那时沙箱副本比本地新。把本地旧副本一起推上去会
    **擦掉沙箱侧的修复**（比原缺陷更坏）。故此处锁：sync 的文件集 ⊆ A2 触达集。
    """
    local = tmp_path / "local"
    local.mkdir()
    sandbox_root = tmp_path / "sandbox"
    sandbox_root.mkdir()
    _mk_go_project(local)
    (sandbox_root / "go.mod").write_text(_GO_MOD_ROOT, "utf-8")
    # 造一个"沙箱侧已修好、本地还是旧的"文件——它是别的修复族的产物，绝不该被推
    (local / "main.go").write_text("package main\n// 本地旧版\n", "utf-8")
    (sandbox_root / "main.go").write_text(
        "package main\n// 沙箱侧 goimports 已修好\n", "utf-8")

    # ★前提：让别的修复族也回传 main.go，使 paths ⊋ _a2_paths★（否则两种实现等价，零区分力）
    mgr = _run_repair(local, sandbox_root, monkeypatch,
                      other_family_paths=["main.go"])

    pushed_all = [f for call in mgr.calls for f in call]
    assert pushed_all, "前提不成立：一次 sync 都没发生"
    assert "main.go" not in pushed_all, (
        f"把非 A2 路径推进沙箱会擦掉沙箱侧修复：{pushed_all}")
    assert set(pushed_all) <= {"go.mod"}, f"推送面越界：{pushed_all}"
    # 沙箱侧修复完好无损
    assert "沙箱侧 goimports 已修好" in (sandbox_root / "main.go").read_text()


def test_no_sandbox_is_safe_noop(tmp_path, monkeypatch):
    """本地模式（无活跃沙箱）：构建直接读 project_path，无需 push，不得抛。"""
    local = tmp_path / "local"
    local.mkdir()
    _mk_go_project(local)
    monkeypatch.setattr(l1, "_sandbox_ctx", lambda: None)
    monkeypatch.setattr(l1, "_repair_go", lambda *a, **k: (0, []))
    n, paths = l1._attempt_build_repair(
        str(local), _GO_BUILD_ERR, ["main.go"], 60,
        project_stack={"languages": ["go"]})
    assert "github.com/google/uuid" in (local / "go.mod").read_text()
    assert "go.mod" in paths, f"触达路径未回传：{paths}"


def test_push_failure_does_not_swallow_into_false_pass(tmp_path, monkeypatch):
    """推送异常不致命也不吞成假通过：注入计数照常回传，构建随后如实失败。"""
    local = tmp_path / "local"
    local.mkdir()
    _mk_go_project(local)

    class _Boom:
        def sync_files_to_sandbox(self, *a, **k):
            raise RuntimeError("infra 瞬时故障")

    monkeypatch.setattr(l1, "_sandbox_ctx", lambda: (object(), _Boom(), "/workspace"))
    monkeypatch.setattr(l1, "_repair_go", lambda *a, **k: (0, []))
    n, paths = l1._attempt_build_repair(
        str(local), _GO_BUILD_ERR, ["main.go"], 60,
        project_stack={"languages": ["go"]})
    assert n >= 1 and "go.mod" in paths


# ── B. 段②：pull-back 并集不得抹掉依赖注入 ────────────────────────────────

class TestCargoDepUnion:
    """Cargo.toml 在 _SHARED_MANIFEST_BASENAMES 里 ⇒ 必走 merge_shared_manifest。"""

    _LOCAL = ('[package]\nname = "acme"\nversion = "0.1.0"\n\n'
              '[dependencies]\nserde = "1.0"\ntokio = "1.37.0"\n')
    _STALE = ('[package]\nname = "acme"\nversion = "0.1.0"\n\n'
              '[dependencies]\nserde = "1.0"\n')

    def test_cargo_is_shared_manifest(self):
        """前提：不共享则根本不进 merge，后面的断言全是空转。"""
        assert _is_shared_manifest("Cargo.toml") is True

    def test_local_only_dep_survives_stale_overwrite(self):
        merged = merge_shared_manifest(self._LOCAL, self._STALE, "Cargo.toml")
        assert "tokio" in merged, f"本地独有依赖被陈旧副本抹掉:\n{merged}"
        assert "serde" in merged

    def test_merged_stays_valid_toml(self):
        import tomllib
        merged = merge_shared_manifest(self._LOCAL, self._STALE, "Cargo.toml")
        obj = tomllib.loads(merged)  # 非法即抛（W-6 的教训：闸不得自产毒 manifest）
        assert obj["dependencies"]["tokio"] == "1.37.0"
        assert obj["dependencies"]["serde"] == "1.0"

    def test_dev_dep_does_not_leak_into_runtime_section(self):
        """★分 section 键★：dev-dependencies 的条目并进运行时 [dependencies] 会把
        构建期工具变成生产依赖（与 A2 注入侧 D14 同口径）。"""
        import tomllib
        local = ('[package]\nname = "acme"\n\n[dependencies]\nserde = "1.0"\n\n'
                 '[dev-dependencies]\ncriterion = "0.5"\n')
        stale = '[package]\nname = "acme"\n\n[dependencies]\nserde = "1.0"\n'
        merged = merge_shared_manifest(local, stale, "Cargo.toml")
        obj = tomllib.loads(merged)
        assert "criterion" in obj.get("dev-dependencies", {}), merged
        assert "criterion" not in obj.get("dependencies", {}), (
            f"dev 依赖泄漏进运行时区:\n{merged}")

    def test_section_key_places_dev_dep_beside_same_name_runtime_dep(self):
        """★section 键在做真事★：local 的 dev 区 foo 与 incoming 的运行时区 foo **同名**，
        并集必须把它放进 dev 区（而非认为"foo 已存在"跳过、也非并进运行时区覆盖版本）。
        键退化成只按名字 → 这条会红（判"已存在"直接跳过）。"""
        import tomllib
        local = '[package]\nname = "acme"\n\n[dev-dependencies]\nfoo = "2.0"\n'
        stale = '[package]\nname = "acme"\n\n[dependencies]\nfoo = "1.0"\n'
        merged = merge_shared_manifest(local, stale, "Cargo.toml")
        obj = tomllib.loads(merged)
        assert obj["dependencies"]["foo"] == "1.0", f"运行时版本被污染:\n{merged}"
        assert obj.get("dev-dependencies", {}).get("foo") == "2.0", merged

    def test_known_boundary_same_name_in_two_local_sections_under_merges(self):
        """★诚实边界（上游解析器粒度所致，非本并集的 bug）★

        复用的 `_parse_cargo` 键是**归一名单键**（`out.setdefault(name.replace("-","_"))`），
        不含 section ⇒ local 同一 crate 同时在 `[dependencies]` 与 `[dev-dependencies]` 时，
        解析器只报**先解析到的那个**，另一个对并集**不可见** ⇒ 欠并。

        方向是保守的（欠并 ≠ 误并）：最坏情况构建如实再报一次缺该 dev 依赖，A2 下轮重注；
        不会把 dev 依赖错放进运行时区、也不会覆盖版本。
        为什么不顺手改 `_parse_cargo` 加 section 进键：它是 A2 注入侧的**同一份**解析器，
        `if dep in _parse_cargo(text)` 的语义是"在任何区声明过就算已声明"——改键会同时改掉
        注入侧的判据（血规 10③：共享表可共享，消费契约后果不同必须分档），属独立改动面，
        已登记进 29 号文 findings，不在 W-1 范围内顺手做。
        本测试把**当前真实行为**钉住：哪天解析器加了 section 键，这条会红＝提醒来更新边界
        描述与上面那条测试，而不是让边界悄悄漂移。
        """
        import tomllib
        local = ('[package]\nname = "acme"\n\n[dependencies]\nfoo = "1.0"\n\n'
                 '[dev-dependencies]\nfoo = "2.0"\n')
        stale = '[package]\nname = "acme"\n\n[dependencies]\nfoo = "1.0"\n'
        from swarm.worker.sibling_dep_repair import _parse_cargo
        parsed = _parse_cargo(local)
        assert list(parsed) == ["foo"] and parsed["foo"][3] == "dependencies", (
            f"上游解析器粒度已变（键含 section 了？）→ 本边界描述需更新: {parsed}")
        merged = merge_shared_manifest(local, stale, "Cargo.toml")
        obj = tomllib.loads(merged)
        assert obj["dependencies"]["foo"] == "1.0"
        assert "foo" not in obj.get("dev-dependencies", {}), (
            f"边界已变（dev 区被并回了）→ 更新边界描述:\n{merged}")

    def test_incoming_version_wins_on_conflict(self):
        """只补缺不改值：incoming 为基，改值需三方基线才能判谁新。"""
        import tomllib
        local = '[package]\nname = "acme"\n\n[dependencies]\nserde = "1.0"\n'
        inc = '[package]\nname = "acme"\n\n[dependencies]\nserde = "2.0"\n'
        merged = merge_shared_manifest(local, inc, "Cargo.toml")
        assert tomllib.loads(merged)["dependencies"]["serde"] == "2.0"

    def test_inline_table_features_are_transplanted_whole(self):
        """D13 同口径：内联表整体移植保 features（削成 version-only 会静默改语义）。"""
        import tomllib
        local = ('[package]\nname = "acme"\n\n[dependencies]\n'
                 'tokio = { version = "1.37.0", features = ["full"] }\n')
        stale = '[package]\nname = "acme"\n\n[dependencies]\n'
        merged = merge_shared_manifest(local, stale, "Cargo.toml")
        obj = tomllib.loads(merged)
        assert obj["dependencies"]["tokio"]["features"] == ["full"], merged

    def test_dot_table_dep_is_honestly_skipped_not_mangled(self):
        """点表（多行段）无单行声明可移植 → 诚实不并（宁可构建再报缺，不产语义被削的
        manifest）。断言：不得产出削掉 features 的伪单行。"""
        import tomllib
        local = ('[package]\nname = "acme"\n\n[dependencies]\nserde = "1.0"\n\n'
                 '[dependencies.tokio]\nversion = "1.37.0"\nfeatures = ["full"]\n')
        stale = '[package]\nname = "acme"\n\n[dependencies]\nserde = "1.0"\n'
        merged = merge_shared_manifest(local, stale, "Cargo.toml")
        obj = tomllib.loads(merged)
        tok = obj.get("dependencies", {}).get("tokio")
        assert tok is None or isinstance(tok, dict), (
            f"点表被削成单行伪声明（丢 features）:\n{merged}")

    def test_no_missing_dep_is_zero_churn(self):
        """无缺失 → 原样返回 incoming（不引入 diff churn）。"""
        inc = self._STALE
        assert merge_shared_manifest(self._STALE, inc, "Cargo.toml") == inc

    # ── 复核 MEDIUM：section 头带行尾注释（合法 TOML）不得让并集变 no-op ──
    _COMMENT_INC = ('[package]\nname = "acme"\nversion = "0.1.0"\n\n'
                    '[dependencies]  # keep sorted alphabetically\nanyhow = "1.0"\n')
    _COMMENT_LOCAL = ('[package]\nname = "acme"\nversion = "0.1.0"\n\n'
                      '[dependencies]  # keep sorted alphabetically\n'
                      'anyhow = "1.0"\ntokio = "1.37.0"\n')

    def test_section_header_with_trailing_comment_still_merges(self):
        """★对抗复核提出、已独立复现★ `[dependencies]  # keep sorted` 是**合法 TOML** 且在
        真实 Cargo.toml 里常见。锚点正则以 `\\s*$` 收尾时它失配 → 走"追加新 section"的
        fallback → 重复 section → 非法 TOML → 被后置校验拦下 → **并集在完全合法的输入上
        静默变 no-op**（兄弟的依赖注入照样蒸发）。"""
        import tomllib
        merged = merge_shared_manifest(
            self._COMMENT_LOCAL, self._COMMENT_INC, "Cargo.toml")
        obj = tomllib.loads(merged)
        assert obj["dependencies"]["tokio"] == "1.37.0", (
            f"行尾注释让并集变 no-op:\n{merged}")
        assert obj["dependencies"]["anyhow"] == "1.0"

    def test_trailing_comment_is_preserved_not_eaten(self):
        """并集不得把用户的行尾注释吃掉（那是交付 diff 里的无关改动）。"""
        merged = merge_shared_manifest(
            self._COMMENT_LOCAL, self._COMMENT_INC, "Cargo.toml")
        assert "# keep sorted alphabetically" in merged, merged

    @pytest.mark.parametrize("header", [
        "[dependencies]",
        "[dependencies] # c",
        "[dependencies]\t# c",
        "[dependencies]   #",
        "[ dependencies ]  # 内空白 + 行尾注释",
        "[dependencies]\t",
    ])
    def test_anchor_accepts_all_legal_header_forms(self, header):
        """锚点覆盖面：这些都是合法 TOML 的 `[dependencies]` 头写法，一个都不能漏
        （漏一种 = 那种写法的工程上 A2/并集永久 no-op）。"""
        import tomllib
        inc = f'[package]\nname = "acme"\n\n{header}\nanyhow = "1.0"\n'
        local = inc.replace('anyhow = "1.0"\n', 'anyhow = "1.0"\ntokio = "1.37.0"\n')
        merged = merge_shared_manifest(local, inc, "Cargo.toml")
        obj = tomllib.loads(merged)
        assert obj["dependencies"].get("tokio") == "1.37.0", (
            f"头写法 {header!r} 下并集失效:\n{merged}")

    def test_identical_texts_short_circuit(self):
        assert merge_shared_manifest(self._LOCAL, self._LOCAL,
                                     "Cargo.toml") == self._LOCAL

    def test_broken_local_toml_fails_open_to_incoming(self):
        """local 畸形 → fail-open 返回 incoming（绝不把毒文本并进去）。"""
        import tomllib
        bad = '[package\nname = "acme"\n[dependencies]\nx = "1"\n'
        merged = merge_shared_manifest(bad, self._STALE, "Cargo.toml")
        tomllib.loads(merged)  # 结果必须合法
        assert "tokio" not in merged

    def test_broken_incoming_toml_fails_open(self):
        bad_inc = '[package\nname = "acme"\n'
        assert merge_shared_manifest(
            self._LOCAL, bad_inc, "Cargo.toml") == bad_inc

    def test_idempotent(self):
        m1 = merge_shared_manifest(self._LOCAL, self._STALE, "Cargo.toml")
        m2 = merge_shared_manifest(self._LOCAL, m1, "Cargo.toml")
        assert m1 == m2, f"并集非幂等:\n{m1}\n---\n{m2}"

    def test_section_header_with_inner_whitespace(self):
        """`[ dependencies ]` 是合法 TOML（批次6 R1 同坑）：不得追加第二个同名 section。"""
        import tomllib
        stale = '[package]\nname = "acme"\n\n[ dependencies ]\nserde = "1.0"\n'
        merged = merge_shared_manifest(self._LOCAL, stale, "Cargo.toml")
        tomllib.loads(merged)  # duplicate-section 会在这里抛
        assert "tokio" in merged

    def test_missing_section_in_incoming_is_created(self):
        import tomllib
        stale = '[package]\nname = "acme"\nversion = "0.1.0"\n'
        merged = merge_shared_manifest(self._LOCAL, stale, "Cargo.toml")
        obj = tomllib.loads(merged)
        assert obj["dependencies"]["tokio"] == "1.37.0", merged

    # ── 假锚点：`[dependencies]` 出现在多行字符串值里（自查实测逼出来的） ──
    _FAKE_ANCHOR_INC = ('[package]\nname = "acme"\ndescription = """\n用法：\n'
                        '[dependencies]\n在上面那节加依赖\n"""\n\n'
                        '[dependencies]\nserde = "1.0"\n')
    _FAKE_ANCHOR_LOCAL = ('[package]\nname = "acme"\ndescription = """\n用法：\n'
                          '[dependencies]\n在上面那节加依赖\n"""\n\n'
                          '[dependencies]\nserde = "1.0"\ntokio = "1.37.0"\n')

    def test_fake_anchor_in_multiline_string_does_not_corrupt_value(self):
        """★只验语法的闸看不见这一类★：正则锚点命中多行字符串【内】那行 `[dependencies]`
        ⇒ 依赖被插进 description 值 ⇒ 值被污染进交付 diff，而结果**仍是合法 TOML**。
        后置校验判"其它值一个都没变"才拦得住 ⇒ 此处断言 description 原样。"""
        import tomllib
        merged = merge_shared_manifest(
            self._FAKE_ANCHOR_LOCAL, self._FAKE_ANCHOR_INC, "Cargo.toml")
        desc = tomllib.loads(merged)["package"]["description"]
        assert "tokio" not in desc, f"description 值被污染:\n{merged}"
        assert desc == tomllib.loads(self._FAKE_ANCHOR_INC)["package"]["description"]

    def test_fake_anchor_fails_open_instead_of_claiming_merged(self):
        """伪并（声称并入但结果里没有）必须走 fail-open 返回 incoming，而不是产出一份
        「description 被改、依赖没进」的四不像。"""
        merged = merge_shared_manifest(
            self._FAKE_ANCHOR_LOCAL, self._FAKE_ANCHOR_INC, "Cargo.toml")
        assert merged == self._FAKE_ANCHOR_INC, (
            f"未 fail-open，产出了伪并结果:\n{merged}")


class TestNpmDepUnion:
    """根 package.json（含 workspaces）走 merge；子包 package.json 不共享→裸写。"""

    def _local(self, deps: dict, dev: dict | None = None) -> str:
        obj: dict = {"name": "root", "workspaces": ["packages/web"],
                     "dependencies": deps}
        if dev is not None:
            obj["devDependencies"] = dev
        return json.dumps(obj, indent=2) + "\n"

    def test_local_only_dep_survives(self):
        local = self._local({"lodash": "^4.17.21", "axios": "^1.6.8"})
        stale = self._local({"lodash": "^4.17.21"})
        merged = json.loads(merge_shared_manifest(local, stale, "package.json"))
        assert merged["dependencies"]["axios"] == "^1.6.8"
        assert merged["dependencies"]["lodash"] == "^4.17.21"

    def test_dev_dep_stays_in_dev_section(self):
        local = self._local({"lodash": "^4.17.21"}, dev={"vitest": "^1.6.0"})
        stale = self._local({"lodash": "^4.17.21"})
        merged = json.loads(merge_shared_manifest(local, stale, "package.json"))
        assert merged["devDependencies"]["vitest"] == "^1.6.0"
        assert "vitest" not in merged["dependencies"]

    @pytest.mark.parametrize("section", [
        "dependencies", "devDependencies", "peerDependencies", "optionalDependencies",
    ])
    def test_every_a2_writable_section_is_merged(self, section):
        """★并集面必须覆盖 A2 注入面★：A2 落回【来源 section】(D14)，_parse_npm 扫四个
        section ⇒ 并集少一个就漏一档。窄于注入面=血规 10① 的接线缺口。"""
        local = json.dumps({"name": "root", "workspaces": ["packages/web"],
                            section: {"pkg-x": "^1.0.0"}}, indent=2) + "\n"
        stale = json.dumps({"name": "root", "workspaces": ["packages/web"]},
                           indent=2) + "\n"
        merged = json.loads(merge_shared_manifest(local, stale, "package.json"))
        assert merged.get(section, {}).get("pkg-x") == "^1.0.0", (
            f"{section} 未参与并集 → A2 注入该区会被抹掉")

    def test_incoming_version_wins_on_conflict(self):
        local = self._local({"lodash": "^4.0.0"})
        inc = self._local({"lodash": "^4.17.21"})
        merged = json.loads(merge_shared_manifest(local, inc, "package.json"))
        assert merged["dependencies"]["lodash"] == "^4.17.21"

    def test_members_and_deps_are_independent_faces(self):
        """★两面必须独立判★：incoming 丢了 workspaces 键（worker 整体重写根清单，
        _is_shared_manifest_on_disk 专门 OR 进来兜的场景）时，依赖并集**照并**。
        若两面共用同一早返，这条会红。"""
        local = self._local({"lodash": "^4.17.21", "axios": "^1.6.8"})
        stale = json.dumps({"name": "root",
                            "dependencies": {"lodash": "^4.17.21"}}, indent=2) + "\n"
        merged = json.loads(merge_shared_manifest(local, stale, "package.json"))
        assert merged["dependencies"]["axios"] == "^1.6.8", (
            "incoming 无 workspaces 键时依赖并集失效 ⇒ 同一早返吃掉两个不相干机制")

    def test_members_union_still_works(self):
        """B7 原有面不得回归。"""
        local = json.dumps({"name": "root",
                            "workspaces": ["packages/web", "packages/api"]},
                           indent=2) + "\n"
        stale = json.dumps({"name": "root", "workspaces": ["packages/web"]},
                           indent=2) + "\n"
        merged = json.loads(merge_shared_manifest(local, stale, "package.json"))
        assert merged["workspaces"] == ["packages/web", "packages/api"]

    def test_no_missing_is_zero_churn(self):
        stale = self._local({"lodash": "^4.17.21"})
        assert merge_shared_manifest(stale, stale, "package.json") == stale

    def test_indent_preserved(self):
        """X-H3 R2：4 空格原文不被重排成 2 空格（污染交付 diff）。"""
        local = json.dumps({"name": "root", "workspaces": ["packages/web"],
                            "dependencies": {"a": "1", "b": "2"}}, indent=4) + "\n"
        stale = json.dumps({"name": "root", "workspaces": ["packages/web"],
                            "dependencies": {"a": "1"}}, indent=4) + "\n"
        merged = merge_shared_manifest(local, stale, "package.json")
        assert '\n    "name"' in merged, f"缩进被重排:\n{merged}"

    def test_malformed_section_type_is_left_alone(self):
        """incoming 该键是非对象（畸形）→ 不碰，保守 fail-open。"""
        local = self._local({"lodash": "^4.17.21"})
        stale = json.dumps({"name": "root", "workspaces": ["packages/web"],
                            "dependencies": "not-an-object"}, indent=2) + "\n"
        merged = merge_shared_manifest(local, stale, "package.json")
        assert json.loads(merged)["dependencies"] == "not-an-object"

    def test_broken_json_fails_open(self):
        local = self._local({"lodash": "^4.17.21"})
        assert merge_shared_manifest(local, "{not json", "package.json") == "{not json"

    def test_idempotent(self):
        local = self._local({"lodash": "^4.17.21", "axios": "^1.6.8"})
        stale = self._local({"lodash": "^4.17.21"})
        m1 = merge_shared_manifest(local, stale, "package.json")
        m2 = merge_shared_manifest(local, m1, "package.json")
        assert m1 == m2


# ── B2. sibling 捞：注入侧（写者）的同一假锚点缺陷 ─────────────────────────

class TestInjectSideFakeAnchor:
    """★修一类问题先全仓捞 sibling★：`_inject_cargo._insert` 用与并集侧同一套正则锚点。

    实测它有同一缺陷且**更严重**——它是【直接改用户 manifest】的写者：
      ① `description` 被污染，污染直接进交付 diff；
      ② 真依赖没注进去；
      ③ 结果仍是合法 TOML ⇒ 原 M-2 的 `tomllib.loads` 校验恒放行；
      ④ 函数返回 **True** ⇒ 调用方记 injected+=1、触发重跑，构建照旧缺同一依赖
         ⇒ repair 收敛循环空烧轮次（假成功比失败更贵）。
    这个缺陷在 W-1 半1 之前被"A2 注入对构建不可见"掩盖着；半1 让 A2 真正生效，它才开始
    咬到交付物 —— 故与 W-1 同批治。
    """

    _TEXT = ('[package]\nname = "acme"\nversion = "0.1.0"\ndescription = """\n用法：\n'
             '[dependencies]\n在上面那节加依赖\n"""\n\n[dependencies]\nserde = "1.0"\n')

    def test_inject_does_not_corrupt_multiline_string_value(self, tmp_path):
        import tomllib

        from swarm.worker.sibling_dep_repair import _inject_cargo
        p = tmp_path / "Cargo.toml"
        p.write_text(self._TEXT, "utf-8")
        before = tomllib.loads(self._TEXT)["package"]["description"]
        _inject_cargo(p, "tokio", "1.37.0")
        after_text = p.read_text()
        after = tomllib.loads(after_text)["package"]["description"]
        assert after == before, f"description 被污染并会进交付 diff:\n{after_text}"

    def test_inject_returns_false_when_it_could_not_really_inject(self, tmp_path):
        """★不许伪成功★：返回 True 而依赖没进去 ⇒ 调用方触发重跑、构建照旧缺 ⇒ 空烧。"""
        import tomllib

        from swarm.worker.sibling_dep_repair import _inject_cargo
        p = tmp_path / "Cargo.toml"
        p.write_text(self._TEXT, "utf-8")
        ok = _inject_cargo(p, "tokio", "1.37.0")
        landed = "tokio" in tomllib.loads(p.read_text()).get("dependencies", {})
        assert ok == landed, (
            f"返回值与事实不符：返回 {ok} 而实际落地 {landed}（伪成功=repair 空烧）")
        assert ok is False and landed is False

    def test_normal_inject_still_works(self, tmp_path):
        """前提锁：后置校验不得把【合法注入】冤杀（名字连字符/下划线归一那一档）。"""
        import tomllib

        from swarm.worker.sibling_dep_repair import _inject_cargo
        p = tmp_path / "Cargo.toml"
        p.write_text('[package]\nname = "acme"\n\n[dependencies]\nserde = "1.0"\n', "utf-8")
        assert _inject_cargo(p, "tokio", "1.37.0") is True
        assert tomllib.loads(p.read_text())["dependencies"]["tokio"] == "1.37.0"

    def test_empty_target_section_not_falsely_rejected(self, tmp_path):
        """★同族回归锁★：并集侧的后置校验首跑时把"incoming 有空 `[dependencies]` 段"这一
        常见形态误判成"其它值变了"（`{}` 为假 → 段被连带摘掉 → 对不上）。写者侧
        `_toml_insert_ok` 的条件写法不同，这里用**同一形状**验它没有同族缺陷。"""
        import tomllib

        from swarm.worker.sibling_dep_repair import _inject_cargo
        p = tmp_path / "Cargo.toml"
        p.write_text('[package]\nname = "acme"\n\n[dependencies]\n', "utf-8")
        assert _inject_cargo(p, "tokio", "1.37.0") is True, (
            "空 section 形态被后置校验冤杀（同族缺陷）")
        assert tomllib.loads(p.read_text())["dependencies"]["tokio"] == "1.37.0"

    def test_absent_target_section_not_falsely_rejected(self, tmp_path):
        """incoming 连 section 都没有 → 新建一个（结构由依赖条目自带，非臆造）。"""
        import tomllib

        from swarm.worker.sibling_dep_repair import _inject_cargo
        p = tmp_path / "Cargo.toml"
        p.write_text('[package]\nname = "acme"\nversion = "0.1.0"\n', "utf-8")
        assert _inject_cargo(p, "tokio", "1.37.0") is True
        assert tomllib.loads(p.read_text())["dependencies"]["tokio"] == "1.37.0"

    def test_hyphen_underscore_name_not_falsely_rejected(self, tmp_path):
        """rustc 诊断用下划线、Cargo.toml 用连字符：断言 ② 两种写法都得认。"""
        import tomllib

        from swarm.worker.sibling_dep_repair import _inject_cargo
        p = tmp_path / "Cargo.toml"
        p.write_text('[package]\nname = "acme"\n\n[dependencies]\nserde = "1.0"\n', "utf-8")
        assert _inject_cargo(p, "serde_json", ("serde-json", "1.0.117")) is True
        deps = tomllib.loads(p.read_text())["dependencies"]
        assert "serde-json" in deps or "serde_json" in deps, deps

    def test_inject_into_section_with_trailing_comment(self, tmp_path):
        """★对抗复核提出、已独立复现★ 写者侧同一锚点缺口：`[dependencies]  # keep sorted`
        下注入失配 → 追加重复 section → 非法 TOML → 后置校验拒 → **A2 在合法 manifest 上
        永久 no-op**。
        ★同时钉住因果★：复核称本次改动"把原本产毒返回 True 改成诚实拒绝 False"——实测
        `5c9e0a2^` 旧代码在同一输入下**同样**返回 False（重复 section 是语法错，旧 M-2 的
        `tomllib.loads` 也拦得住）⇒ 本次改动在这条路径上行为未变，缺口是纯既有缺口。"""
        import tomllib

        from swarm.worker.sibling_dep_repair import _inject_cargo
        p = tmp_path / "Cargo.toml"
        p.write_text('[package]\nname = "acme"\n\n'
                     '[dependencies]  # keep sorted\nanyhow = "1.0"\n', encoding="utf-8")
        assert _inject_cargo(p, "serde", "1.0") is True, "行尾注释下注入变 no-op"
        obj = tomllib.loads(p.read_text())
        assert obj["dependencies"]["serde"] == "1.0"
        assert obj["dependencies"]["anyhow"] == "1.0"
        assert "# keep sorted" in p.read_text(), "用户注释被吃掉"

    # ★夹具形状是本组的命题所在★（首跑教训：用 `serde_json`/`serde-json` 这类**单纯全 `-`
    # ↔ 全 `_` 互换**的名字，三态集合枚举**恰好覆盖得到** ⇒ 突变压不动、测试零区分力。
    # 三态真正漏的是【落地键**混用**两种分隔符、而 rustc 名不是它的单纯互换】那一档。）
    @pytest.mark.parametrize("rustc_name,manifest_key", [
        ("my_crate_name", "my-crate_name"),   # ★三态漏：混用键★
        ("a_b_c", "a-b_c"),                   # ★三态漏★
        ("serde_json", "serde-json"),         # 对照：三态覆盖得到，改法也必须放行
        ("plain", "plain"),                   # 对照：无分隔符
    ])
    def test_mixed_separator_names_not_falsely_rejected(
            self, tmp_path, rustc_name, manifest_key):
        """raw 移植路径的行为锁：兄弟 manifest 里的键可以是任意 `-`/`_` 组合，注入都得成功
        且保住 features。

        ★关于复核 MEDIUM-2 的诚实说明★ reviewer 指出名字归一的三态集合枚举对**混用**分隔符
        的键覆盖不到 —— 枚举缺口本身成立（已用矩阵实测），但**沿生产调用路径不可达**：
        `_inject_cargo` 传给 `_toml_insert_ok` 的 `name` 恒等于真正插入的那个键，故三态里
        恒含 name 自身、恒命中。因此本测试**不是**那条缺陷的锁（没有单点突变能压住它，见
        `scripts/w1_mutation_check.py` 里 W-1-p 的撤销记录），它锁的是 raw 移植这件真事。
        """
        import tomllib

        from swarm.worker.sibling_dep_repair import _inject_cargo
        p = tmp_path / "Cargo.toml"
        p.write_text('[package]\nname = "acme"\n\n[dependencies]\nanyhow = "1.0"\n',
                     encoding="utf-8")
        # raw 用【兄弟 manifest 的键写法】，name 用【rustc 诊断名】——这正是生产形态
        raw = f'{manifest_key} = {{ version = "1.0", features = ["x"] }}'
        ok = _inject_cargo(p, rustc_name, (manifest_key, "1.0", raw, "dependencies"))
        assert ok is True, f"name={rustc_name!r} 键={manifest_key!r} 被冤拒"
        deps = tomllib.loads(p.read_text())["dependencies"]
        assert manifest_key in deps, deps
        assert deps[manifest_key]["features"] == ["x"], (
            f"raw 移植没保住 features: {deps}")

    def test_raw_transplant_with_differing_separator_accepted(self, tmp_path):
        """raw 移植时【兄弟声明的写法】与【rustc 诊断名】分隔符可能不同（前者 `-`、后者 `_`）
        —— 归一必须让它通过，否则保 features 的那条路被自己的校验掐死。"""
        import tomllib

        from swarm.worker.sibling_dep_repair import _inject_cargo
        p = tmp_path / "Cargo.toml"
        p.write_text('[package]\nname = "acme"\n\n[dependencies]\nanyhow = "1.0"\n',
                     encoding="utf-8")
        raw = 'serde-json = { version = "1.0.117", features = ["preserve_order"] }'
        ok = _inject_cargo(p, "serde_json",
                           ("serde-json", "1.0.117", raw, "dependencies"))
        assert ok is True, "分隔符不同导致 raw 移植被冤拒"
        deps = tomllib.loads(p.read_text())["dependencies"]
        assert deps["serde-json"]["features"] == ["preserve_order"], deps

    def test_inline_table_features_preserved_on_inject(self, tmp_path):
        """D13 不得回归：内联表整体移植保 features。"""
        import tomllib

        from swarm.worker.sibling_dep_repair import _inject_cargo
        p = tmp_path / "Cargo.toml"
        p.write_text('[package]\nname = "acme"\n\n[dependencies]\nserde = "1.0"\n', "utf-8")
        raw = 'tokio = { version = "1.37.0", features = ["full"] }'
        assert _inject_cargo(p, "tokio", ("tokio", "1.37.0", raw, "dependencies")) is True
        obj = tomllib.loads(p.read_text())
        assert obj["dependencies"]["tokio"]["features"] == ["full"]


# ── C. 契约核验：剪枝复用的安全性 + go.mod 当前事实钉子 ────────────────────

class TestReusedPruneContract:
    """`_push_manifests_to_sandbox` 的【成员剪枝】是给 Maven reactor 造的。复用到 A2 的
    三种清单上前必须证：血规 10③「复用单一事实源 ≠ 复用其消费契约」。"""

    @pytest.mark.parametrize("rel,text", [
        ("go.mod", 'module m\n\ngo 1.22\n\nrequire github.com/a/b v1.0.0\n'),
        ("services/api/go.mod", 'module m/api\n\ngo 1.22\n'),
        ("packages/web/package.json",
         '{"name": "@acme/web", "dependencies": {"react": "^18.2.0"}}\n'),
        ("crates/core/Cargo.toml",
         '[package]\nname = "core"\n\n[dependencies]\nserde = "1.0"\n'),
    ])
    def test_a2_non_aggregate_targets_have_no_member_probes(self, rel, text):
        """这四种形态 probes 空 ⇒ 剪枝对它们是 no-op（连探针都不发）。"""
        from swarm.worker.workspace_manifest import manifest_member_probes
        assert manifest_member_probes(rel, text) == []

    def test_a2_injection_creates_no_prunable_member(self, tmp_path):
        """根清单**有**成员探针，但 A2 只注依赖不注成员 ⇒ 成员集恒等 ⇒ 剪枝候选为空。
        （剪枝资格前提①=成员不在 git 基线里。）"""
        from swarm.worker.sibling_dep_repair import _inject_cargo, _inject_npm
        from swarm.worker.workspace_manifest import manifest_member_probes
        cases = [
            ("package.json",
             json.dumps({"name": "root", "workspaces": ["packages/web", "packages/api"],
                         "dependencies": {"lodash": "^4.17.21"}}, indent=2) + "\n",
             _inject_npm, "axios", "^1.6.8"),
            ("Cargo.toml",
             '[package]\nname = "acme"\nversion = "0.1.0"\n\n'
             '[workspace]\nmembers = ["crates/core", "crates/cli"]\n\n'
             '[dependencies]\nserde = "1.0"\n',
             _inject_cargo, "tokio", "1.37.0"),
        ]
        for rel, text, inject, dep, ver in cases:
            f = tmp_path / rel
            f.write_text(text, "utf-8")
            before = {t for t, _ in manifest_member_probes(rel, text)}
            assert before, f"前提不成立：{rel} 没有成员探针，本用例零区分力"
            assert inject(f, dep, ver) is True, f"前提不成立：{rel} 注入没生效"
            after_text = f.read_text()
            assert dep in after_text
            after = {t for t, _ in manifest_member_probes(rel, after_text)}
            assert after == before, (
                f"{rel}：A2 注入改变了成员集 ⇒ 剪枝候选非空 ⇒ 复用剪枝不安全\n{after_text}")

    def test_go_mod_routed_through_merge_with_union(self):
        """#29-5 W-2（翻转本钉子，原 `test_go_mod_not_routed_through_merge_today`）：
        根 go.mod 是依赖承载共享清单 ⇒ pull-back 走 flock+并集，不再裸写；
        require 并集臂同批落地（分类档位与并集臂是同一个洞的两面，血规 10①）。
        子模块 go.mod 各 worker 独占 ⇒ 不纳入（不扩锁面）。全形态锁见
        test_w2_go_mod_shared_manifest.py。"""
        assert _is_shared_manifest("go.mod") is True
        assert _is_shared_manifest("services/api/go.mod") is False
        assert _is_shared_manifest("go.work") is True
        local = 'module m\n\nrequire (\n\tgithub.com/a/b v1.0.0\n\tgithub.com/c/d v2.0.0\n)\n'
        inc = 'module m\n\nrequire (\n\tgithub.com/a/b v1.0.0\n)\n'
        merged = merge_shared_manifest(local, inc, "go.mod")
        assert 'github.com/c/d v2.0.0' in merged  # local 独有 require 并回（原钉子断言 ==inc 盲覆盖）
        assert 'github.com/a/b v1.0.0' in merged  # incoming 既有条目原样保留
