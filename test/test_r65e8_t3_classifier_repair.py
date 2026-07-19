"""R65E8-T3（round65e8 实锤·shiro-ehcache jakarta classifier）：classifier 幻觉的确定性修复。

死因链：worker 给 `org.apache.shiro:shiro-ehcache` 写了幻觉 `<classifier>jakarta</classifier>`
（仓库里 shiro-ehcache 根本没有 jakarta 分类变体，只有无 classifier 的正版）→ Maven 报
`Could not find artifact org.apache.shiro:shiro-ehcache:jar:jakarta:2.0.1`。

★旧闸盲区（本 T3 治本前）★ `_MISSING_ARTIFACT_RE` 把 classifier `jakarta` **误当成 version**
捕获 → version-repair 拿 bad_ver="jakarta" 去 `rewrite_dependency_version` 找 `<version>jakarta</version>`
→ pom 里根本没有（jakarta 在 `<classifier>` 里）→ 静默 no-op、classifier 永不被剔 →
HANDLE_FAILURE 同签名重试到 abandon（round65e8 靠 orchestrator retry_alternate 换模型
Qwopus27B→MiniMax 才手工修好，慢且非确定）。

治本：classifier 幻觉是「模型手写坐标不可靠」的又一表象，与 version-repair 同源、确定性可修——
探仓库确认 base 坐标 g:a（去 classifier）可解析 → 该 classifier 是幻觉 → 剔 `<classifier>` 标签。
"""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import swarm.worker.l1_pipeline as L  # noqa: E402
from swarm.worker.l1_parse import (  # noqa: E402
    parse_missing_artifacts,
    parse_missing_classified_artifacts,
)
from swarm.worker.l1_pipeline import (  # noqa: E402
    _attempt_maven_version_repair,
    _strip_dep_classifier,
)


_SHIRO_ERR = (
    "[ERROR] Failed to execute goal on project ruoyi-framework: Could not resolve dependencies\n"
    "[ERROR] Could not find artifact org.apache.shiro:shiro-ehcache:jar:jakarta:2.0.1 "
    "in aliyun (https://maven.aliyun.com/repository/public)\n"
)

_POM_WITH_CLASSIFIER = """<project>
  <dependencies>
    <dependency>
      <groupId>org.apache.shiro</groupId>
      <artifactId>shiro-ehcache</artifactId>
      <version>2.0.1</version>
      <classifier>jakarta</classifier>
    </dependency>
    <dependency>
      <groupId>com.other</groupId>
      <artifactId>keep-me</artifactId>
      <version>1.0</version>
      <classifier>jakarta</classifier>
    </dependency>
  </dependencies>
</project>
"""


# ── parse 层：classifier 不再被误当 version ──
def test_parse_missing_artifacts_no_longer_misparses_classifier_as_version():
    """★核心 RED★ 旧实现返回 ('org.apache.shiro','shiro-ehcache','jakarta')；修后 version 应为真值 2.0.1。"""
    got = parse_missing_artifacts(_SHIRO_ERR)
    assert ("org.apache.shiro", "shiro-ehcache", "2.0.1") in got, \
        f"classifier 形态应捕到真 version 2.0.1（不再把 classifier jakarta 当版本）；实得 {got}"
    assert ("org.apache.shiro", "shiro-ehcache", "jakarta") not in got, \
        f"classifier jakarta 绝不能被当作 version 捕获；实得 {got}"


def test_parse_missing_artifacts_plain_form_unchanged():
    """无 classifier 的普通形态解析不受回归影响。"""
    s = "Could not find artifact com.foo:bar:jar:1.2.3 in central"
    assert parse_missing_artifacts(s) == [("com.foo", "bar", "1.2.3")]


def test_parse_missing_classified_artifacts_extracts_classifier():
    got = parse_missing_classified_artifacts(_SHIRO_ERR)
    assert ("org.apache.shiro", "shiro-ehcache", "jakarta", "2.0.1") in got, \
        f"应提取 (group, artifact, classifier, version)；实得 {got}"


def test_parse_missing_classified_ignores_plain():
    """普通（无 classifier）形态不得进 classified 列表。"""
    s = "Could not find artifact com.foo:bar:jar:1.2.3 in central"
    assert parse_missing_classified_artifacts(s) == []


def test_parse_missing_classified_dedup_preserves_order():
    s = _SHIRO_ERR + _SHIRO_ERR
    got = parse_missing_classified_artifacts(s)
    assert got.count(("org.apache.shiro", "shiro-ehcache", "jakarta", "2.0.1")) == 1


# ── _strip_dep_classifier 块级编辑 ──
def test_strip_classifier_removes_only_matching_block():
    new = _strip_dep_classifier(
        _POM_WITH_CLASSIFIER, "org.apache.shiro", "shiro-ehcache", "jakarta")
    assert new is not None, "命中应返回新文本"
    # shiro-ehcache 块的 classifier 被剔
    assert "<artifactId>shiro-ehcache</artifactId>" in new
    shiro_blk = new.split("shiro-ehcache")[1].split("</dependency>")[0]
    assert "<classifier>jakarta</classifier>" not in shiro_blk, "目标块的 jakarta classifier 应被剔除"
    # version 保留
    assert "<version>2.0.1</version>" in new
    # 别的依赖（com.other:keep-me）的 classifier 不受影响
    keep_blk = new.split("keep-me")[1].split("</dependency>")[0]
    assert "<classifier>jakarta</classifier>" in keep_blk, "非目标坐标的 classifier 绝不能被误剔"


def test_strip_classifier_groupid_mismatch_skips():
    """groupId 明确不符 → 不动（防撞名误剔）。"""
    new = _strip_dep_classifier(
        _POM_WITH_CLASSIFIER, "com.wrong.group", "shiro-ehcache", "jakarta")
    assert new is None, "groupId 明确不符应整体不命中（返回 None）"


def test_strip_classifier_value_mismatch_skips():
    """块内 classifier 值不等于目标 → 不动（只剔恰为该值的幻觉 classifier）。"""
    new = _strip_dep_classifier(
        _POM_WITH_CLASSIFIER, "org.apache.shiro", "shiro-ehcache", "native")
    assert new is None, "classifier 值不匹配应不命中"


def test_strip_classifier_no_classifier_returns_none():
    pom = ("<project><dependencies><dependency>"
           "<groupId>org.apache.shiro</groupId>"
           "<artifactId>shiro-ehcache</artifactId>"
           "<version>2.0.1</version>"
           "</dependency></dependencies></project>")
    assert _strip_dep_classifier(pom, "org.apache.shiro", "shiro-ehcache", "jakarta") is None


def test_strip_classifier_empty_groupid_matches_by_artifact():
    """调用方 group 为空串时只据 artifactId 匹配（宽松但保守：值仍须恰等）。"""
    new = _strip_dep_classifier(
        _POM_WITH_CLASSIFIER, "", "shiro-ehcache", "jakarta")
    assert new is not None
    shiro_blk = new.split("shiro-ehcache")[1].split("</dependency>")[0]
    assert "<classifier>jakarta</classifier>" not in shiro_blk


# ── ★复核 HIGH 治后·分支⓪决策闸★ 只在【pinned version 有效】时剔 classifier ──
_MODULE_POM = """<project>
  <artifactId>ruoyi-framework</artifactId>
  <dependencies>
    <dependency>
      <groupId>org.apache.shiro</groupId>
      <artifactId>shiro-ehcache</artifactId>
      <version>2.0.1</version>
      <classifier>jakarta</classifier>
    </dependency>
  </dependencies>
</project>
"""


def _mk_module(d: str) -> Path:
    root = Path(d)
    (root / "pom.xml").write_text(
        "<project><groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
        "<version>4.8.3</version><modules><module>ruoyi-framework</module></modules></project>",
        encoding="utf-8")
    (root / "ruoyi-framework").mkdir(parents=True)
    (root / "ruoyi-framework" / "pom.xml").write_text(_MODULE_POM, encoding="utf-8")
    return root / "ruoyi-framework" / "pom.xml"


def test_gate_strips_classifier_when_pinned_version_valid(monkeypatch):
    """version 2.0.1 在 base 坐标可用集合里 → 错的是 classifier → 剔除 jakarta。"""
    monkeypatch.setattr(L, "_fetch_maven_versions_probe",
                        lambda g, a, p, t: (["1.13.0", "2.0.1", "2.0.2"], True))
    with tempfile.TemporaryDirectory() as d:
        pom = _mk_module(d)
        build_out = ("[ERROR] Could not find artifact "
                     "org.apache.shiro:shiro-ehcache:jar:jakarta:2.0.1\n")
        n, changed = _attempt_maven_version_repair(str(Path(d)), build_out, timeout=30)
        txt = pom.read_text("utf-8")
        assert "<classifier>jakarta</classifier>" not in txt, "版本有效→幻觉 classifier 应被剔除"
        assert "<version>2.0.1</version>" in txt, "version 保留（错的不是它）"
        assert n >= 1 and any(p.endswith("pom.xml") for p in changed)


def test_gate_keeps_classifier_when_version_invalid(monkeypatch):
    """★复核 HIGH 回归锁★ pinned version 不在可用集合里=版本错，绝不误剔合法 classifier
    （netty-tcnative:linux-x86_64 遇版本写错的场景）→ 交分支① version-repair。"""
    monkeypatch.setattr(L, "_fetch_maven_versions_probe",
                        lambda g, a, p, t: (["2.0.2", "2.0.3"], True))  # 2.0.1 不在其中
    with tempfile.TemporaryDirectory() as d:
        pom = _mk_module(d)
        build_out = ("[ERROR] Could not find artifact "
                     "org.apache.shiro:shiro-ehcache:jar:jakarta:2.0.1\n")
        _attempt_maven_version_repair(str(Path(d)), build_out, timeout=30)
        txt = pom.read_text("utf-8")
        assert "<classifier>jakarta</classifier>" in txt, \
            "★版本无效=版本错，classifier 绝不能被误剔（否则平台 classifier 遇版本错被静默剥）★"


def test_gate_fail_open_when_repo_unreachable(monkeypatch):
    """仓库不可达（reachable=False）→ 本轮不动（绝不据证据缺失剪改）。"""
    monkeypatch.setattr(L, "_fetch_maven_versions_probe",
                        lambda g, a, p, t: ([], False))
    with tempfile.TemporaryDirectory() as d:
        pom = _mk_module(d)
        build_out = ("[ERROR] Could not find artifact "
                     "org.apache.shiro:shiro-ehcache:jar:jakarta:2.0.1\n")
        _attempt_maven_version_repair(str(Path(d)), build_out, timeout=30)
        txt = pom.read_text("utf-8")
        assert "<classifier>jakarta</classifier>" in txt, "不可达时 fail-open，不剔"
