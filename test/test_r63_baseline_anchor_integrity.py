"""T2（round63 死锁触发器结构性兜底）：三方基线·共享版本锚不可篡改。

round63 死因：worker 侧 version-repair 把根 pom 顶层 <properties> 的共享版本锚
spring-boot.version 4.0.6→3.5.16（内容级篡改，既非 dependency 也非 module 条目）→
merge_shared_manifest（加法-only 两方并集）原样放行毒值 → 整 reactor 降代 → 死锁。

T1 已在 version-repair 源头禁改共享锚；T2 是【独立三方基线闸】：pull-back 落盘后用 git HEAD
基线校验既有版本锚是否被篡改，命中即还原基线值（拒毒进共享树）。本测试锁死两层：
  ① 纯判据 restore_baseline_version_anchors（还原篡改 / 放行加法 / 栈无关口径）；
  ② executor._enforce_baseline_anchor_integrity 在真 git 仓 pull-back 后的兜底行为。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from swarm.worker.executor_sync import _SandboxSyncMixin
from swarm.worker.workspace_manifest import (
    restore_baseline_version_anchors,
)

# ──────────────────────── fixtures ────────────────────────

_BASELINE_ROOT_POM = """\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <groupId>com.ruoyi</groupId>
    <artifactId>ruoyi</artifactId>
    <version>3.8.6</version>
    <packaging>pom</packaging>
    <properties>
        <spring-boot.version>4.0.6</spring-boot.version>
        <druid.version>1.2.20</druid.version>
        <java.version>17</java.version>
    </properties>
    <modules>
        <module>ruoyi-common</module>
        <module>ruoyi-framework</module>
    </modules>
    <dependencyManagement>
        <dependencies>
            <dependency>
                <groupId>org.springframework.boot</groupId>
                <artifactId>spring-boot-dependencies</artifactId>
                <version>${spring-boot.version}</version>
                <type>pom</type>
                <scope>import</scope>
            </dependency>
        </dependencies>
    </dependencyManagement>
</project>
"""

# round63 毒版：spring-boot.version 被降级 4.0.6→3.5.16（其它一切不变）。
_POISONED_ROOT_POM = _BASELINE_ROOT_POM.replace(
    "<spring-boot.version>4.0.6</spring-boot.version>",
    "<spring-boot.version>3.5.16</spring-boot.version>",
)


# ──────────────────────── 纯判据：还原篡改 ────────────────────────

def test_property_anchor_mutation_restored():
    """round63 本体：共享版本锚被降级 → 还原基线值，登记 from/to。"""
    new_text, restored = restore_baseline_version_anchors(
        _POISONED_ROOT_POM, _BASELINE_ROOT_POM, "pom.xml")
    assert "<spring-boot.version>4.0.6</spring-boot.version>" in new_text
    assert "3.5.16" not in new_text
    assert restored == [
        {"anchor": "property:spring-boot.version", "from": "3.5.16", "to": "4.0.6"}
    ]


def test_parent_version_mutation_restored():
    """继承的平台版本锚 <parent><version> 被篡改 → 还原基线值。"""
    base = (
        "<project><parent><groupId>org.springframework.boot</groupId>"
        "<artifactId>spring-boot-starter-parent</artifactId>"
        "<version>3.4.0</version></parent>"
        "<properties><x.version>1.0</x.version></properties></project>"
    )
    cur = base.replace("<version>3.4.0</version>", "<version>3.1.0</version>")
    new_text, restored = restore_baseline_version_anchors(cur, base, "pom.xml")
    assert "<version>3.4.0</version>" in new_text
    assert "3.1.0" not in new_text
    assert {"anchor": "parent.version", "from": "3.1.0", "to": "3.4.0"} in restored


# ──────────────────────── 纯判据：放行加法（绝不误挡/误改） ────────────────────────

def test_pure_additions_untouched():
    """加法（新属性 + 新依赖 + 新 module），既有锚不动 → 零还原、文本恒等。

    这是不会冲掉并行兄弟合法注册的保证：兄弟的贡献都是加法。"""
    added = _BASELINE_ROOT_POM.replace(
        "<java.version>17</java.version>",
        "<java.version>17</java.version>\n        "
        "<mybatis.version>3.5.16</mybatis.version>",
    ).replace(
        "<module>ruoyi-framework</module>",
        "<module>ruoyi-framework</module>\n        <module>ruoyi-alarm</module>",
    )
    new_text, restored = restore_baseline_version_anchors(
        added, _BASELINE_ROOT_POM, "pom.xml")
    assert restored == []
    assert new_text == added  # 加法一律不动
    assert "<module>ruoyi-alarm</module>" in new_text
    assert "<mybatis.version>3.5.16</mybatis.version>" in new_text


def test_new_property_is_addition_not_mutation():
    """当前新增了基线本无的属性键 → 不是篡改，不还原。"""
    cur = _BASELINE_ROOT_POM.replace(
        "<druid.version>1.2.20</druid.version>",
        "<druid.version>1.2.20</druid.version>\n        "
        "<brand.new.version>9.9.9</brand.new.version>",
    )
    new_text, restored = restore_baseline_version_anchors(
        cur, _BASELINE_ROOT_POM, "pom.xml")
    assert restored == []
    assert "<brand.new.version>9.9.9</brand.new.version>" in new_text


def test_non_pom_passthrough():
    """非 pom 清单未实证篡改面 → 原样返回（保守，不臆造语义）。"""
    txt = '{"dependencies": {"react": "18.0.0"}}'
    base = '{"dependencies": {"react": "19.0.0"}}'
    new_text, restored = restore_baseline_version_anchors(txt, base, "package.json")
    assert restored == []
    assert new_text == txt


def test_empty_baseline_returns_unchanged():
    """无基线（新建文件 baseline="") → 无既有锚可护，原样返回。"""
    new_text, restored = restore_baseline_version_anchors(
        _POISONED_ROOT_POM, "", "pom.xml")
    assert restored == []
    assert new_text == _POISONED_ROOT_POM


def test_profile_same_key_not_touched():
    """同名属性在 <profiles> 里（条件区）→ 只还原顶层，profile 区不动。"""
    base = (
        "<project><properties><sb.version>4.0.6</sb.version></properties>"
        "<profiles><profile><properties>"
        "<sb.version>2.0.0</sb.version></properties></profile></profiles></project>"
    )
    cur = (
        "<project><properties><sb.version>3.5.16</sb.version></properties>"
        "<profiles><profile><properties>"
        "<sb.version>2.0.0</sb.version></properties></profile></profiles></project>"
    )
    new_text, restored = restore_baseline_version_anchors(cur, base, "pom.xml")
    # 顶层还原为 4.0.6
    assert new_text.index("<sb.version>4.0.6</sb.version>") < new_text.index("<profiles>")
    # profile 区的 2.0.0 保持不动
    assert "<sb.version>2.0.0</sb.version>" in new_text
    assert restored == [
        {"anchor": "property:sb.version", "from": "3.5.16", "to": "4.0.6"}
    ]


# ──────────────────────── executor 兜底：真 git 仓 pull-back 后 ────────────────────────

class _FakeExec(_SandboxSyncMixin):
    """最小执行体：仅承载 _enforce_baseline_anchor_integrity 依赖的状态/方法。"""

    def __init__(self, project_path: str):
        self.project_path = project_path
        self.base_ref = None
        self._post_sync_contents: dict = {}
        self._baseline_integrity_restored: list = []  # 真 executor.__init__ 亦初始化
        self.logs: list[str] = []

    def _log(self, msg: str, level: str = "info") -> None:
        self.logs.append((level, msg))


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True, text=True)


def _mk_git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "pom.xml").write_text(_BASELINE_ROOT_POM, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")
    return root


def test_enforce_restores_poisoned_root_pom(tmp_path):
    """pull-back 已把毒 pom 写到本地共享树 → 兜底闸还原基线锚 + fail-loud 登记 + 修正快照。"""
    root = _mk_git_repo(tmp_path)
    # 模拟 pull-back 落盘的毒版 + 快照
    (root / "pom.xml").write_text(_POISONED_ROOT_POM, encoding="utf-8")
    ex = _FakeExec(str(root))
    ex._post_sync_contents = {"pom.xml": _POISONED_ROOT_POM}

    ex._enforce_baseline_anchor_integrity(Path(root), "test")

    on_disk = (root / "pom.xml").read_text(encoding="utf-8")
    assert "<spring-boot.version>4.0.6</spring-boot.version>" in on_disk
    assert "3.5.16" not in on_disk
    # 快照同步修正（避免 diff 再把毒当产出）
    assert "3.5.16" not in ex._post_sync_contents["pom.xml"]
    # fail-loud 登记
    assert ex._baseline_integrity_restored == [
        {"file": "pom.xml", "anchor": "property:spring-boot.version",
         "from": "3.5.16", "to": "4.0.6"}
    ]
    # fail-loud：命中必走 warning 级（观测约定，非 info）
    assert any(lvl == "warning" and "T2 基线完整性闸" in m for lvl, m in ex.logs)


def test_enforce_additive_module_registration_not_reverted(tmp_path):
    """并行兄弟对 root pom 的加法（新 <module> 注册）无锚篡改 → 兜底不动、零登记。

    证明本闸只挡篡改、绝不冲掉兄弟的合法注册（last-write-wins clobber 由别处治）。"""
    root = _mk_git_repo(tmp_path)
    added = _BASELINE_ROOT_POM.replace(
        "<module>ruoyi-framework</module>",
        "<module>ruoyi-framework</module>\n        <module>ruoyi-alarm</module>",
    )
    (root / "pom.xml").write_text(added, encoding="utf-8")
    ex = _FakeExec(str(root))
    ex._post_sync_contents = {"pom.xml": added}

    ex._enforce_baseline_anchor_integrity(Path(root), "test")

    on_disk = (root / "pom.xml").read_text(encoding="utf-8")
    assert "<module>ruoyi-alarm</module>" in on_disk  # 兄弟注册保留
    assert ex._baseline_integrity_restored == []


def test_enforce_untracked_file_skipped(tmp_path):
    """基线树无此文件（本子任务新建 pom）→ 无既有锚，fail-open 跳过，不动。"""
    root = _mk_git_repo(tmp_path)
    newmod = root / "ruoyi-alarm"
    newmod.mkdir()
    (newmod / "pom.xml").write_text(_POISONED_ROOT_POM, encoding="utf-8")
    ex = _FakeExec(str(root))
    ex._post_sync_contents = {"ruoyi-alarm/pom.xml": _POISONED_ROOT_POM}

    ex._enforce_baseline_anchor_integrity(Path(root), "test")

    # 未跟踪文件不被基线锚闸触碰（其内容治理走别的机制）
    assert (newmod / "pom.xml").read_text(encoding="utf-8") == _POISONED_ROOT_POM
    assert ex._baseline_integrity_restored == []


# ──────── 复核整改①：重复叶子毒不静默解除检测（silent-hunter #1） ────────

def test_duplicate_current_leaf_poison_still_detected():
    """盲插式毒留下【重复 <key> 叶子】(一真一毒) → 逐值扫描仍判篡改、全部收敛基线值、
    并标 note；绝不因去重歧义静默漏护（round47 双 version 前例的防线）。"""
    base = ("<project><properties>"
            "<sb.version>4.0.6</sb.version></properties></project>")
    # 毒把值改小并又盲插一份（两个顶层 <sb.version> 叶子：4.0.6 与 3.5.16）
    cur = ("<project><properties>"
           "<sb.version>3.5.16</sb.version>"
           "<sb.version>4.0.6</sb.version></properties></project>")
    new_text, restored = restore_baseline_version_anchors(cur, base, "pom.xml")
    assert "3.5.16" not in new_text
    assert new_text.count("<sb.version>4.0.6</sb.version>") == 2  # 全收敛基线值
    assert len(restored) == 1
    assert restored[0]["anchor"] == "property:sb.version"
    assert restored[0]["to"] == "4.0.6"
    assert restored[0].get("note") == "multiple-current-leaves"


# ──────── 复核整改②：本子任务有写权的清单放行（code-reviewer HIGH） ────────

class _Scope:
    def __init__(self, writable=None, create_files=None):
        self.writable = writable or []
        self.create_files = create_files or []


def test_enforce_owned_pom_property_bump_not_reverted(tmp_path):
    """清单在本子任务 writable scope 内=brain 授权编辑（如规划的版本 bump）→ 放行，
    绝不误还原。防"合法交付被静默丢弃"（code-reviewer HIGH 的假阳性路径）。"""
    root = _mk_git_repo(tmp_path)
    bumped = _BASELINE_ROOT_POM.replace(
        "<druid.version>1.2.20</druid.version>",
        "<druid.version>1.2.21</druid.version>",
    )
    (root / "pom.xml").write_text(bumped, encoding="utf-8")
    ex = _FakeExec(str(root))
    ex.effective_scope = _Scope(writable=["pom.xml"])  # 本子任务有写权
    ex._post_sync_contents = {"pom.xml": bumped}

    ex._enforce_baseline_anchor_integrity(Path(root), "test")

    on_disk = (root / "pom.xml").read_text(encoding="utf-8")
    assert "<druid.version>1.2.21</druid.version>" in on_disk  # 授权编辑保留
    assert ex._baseline_integrity_restored == []


def test_enforce_out_of_scope_property_still_reverted(tmp_path):
    """同样的属性篡改，但清单【不在】本子任务 scope（repair 越界摸到基线）→ 仍还原。
    与上一条成对：判据是"有无写权"而非"改没改"，精准区分越界毒 vs 授权交付。"""
    root = _mk_git_repo(tmp_path)
    (root / "pom.xml").write_text(_POISONED_ROOT_POM, encoding="utf-8")
    ex = _FakeExec(str(root))
    ex.effective_scope = _Scope(writable=["ruoyi-alarm/pom.xml"])  # 只授权别的文件
    ex._post_sync_contents = {"pom.xml": _POISONED_ROOT_POM}

    ex._enforce_baseline_anchor_integrity(Path(root), "test")

    on_disk = (root / "pom.xml").read_text(encoding="utf-8")
    assert "<spring-boot.version>4.0.6</spring-boot.version>" in on_disk
    assert len(ex._baseline_integrity_restored) == 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
