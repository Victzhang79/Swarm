"""#29-B（round65e12 死因·确定性兜底闸）：worker 写坏 pom（`<group>` 非 `<groupId>`、丢
parent.groupId、XML 截断）→ 毒化 reactor → 下游 upstream_module_broken 连坐 10。既有机制只在
`mvn compile` 时才暴露成 build error（已烧沙箱+连坐后）。

治：L1.1c 确定性 pom 结构闸——worker 改动的 *pom.xml* 先校 Maven 必备坐标（自身或 parent 可解析
groupId、有 artifactId、parent 若在则 groupId/artifactId/version 齐全、XML 良构），不合格【当轮判死
+ 带证据回灌重试】，拦在毒化 reactor 之前。栈中立仅 Maven pom 生效。
"""
from __future__ import annotations

from swarm.worker.l1_pipeline import _pom_structure_violations

_GOOD = """<?xml version="1.0"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId><version>4.8.3</version>
  </parent>
  <artifactId>ruoyi-framework</artifactId>
  <dependencies/>
</project>"""


def test_good_pom_no_violation():
    assert _pom_structure_violations(_GOOD, "ruoyi-framework/pom.xml") == []


# ── ★round65e12 死因铁证★ <group> 非 <groupId> + 丢 parent.groupId ──
def test_group_typo_and_missing_parent_groupid_flagged():
    bad = """<?xml version="1.0"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <artifactId>ruoyi</artifactId><version>4.8.3</version>
  </parent>
  <artifactId>ruoyi-framework</artifactId>
  <group>com.ruoyi</group>
</project>"""
    v = _pom_structure_violations(bad, "ruoyi-framework/pom.xml")
    assert v, "round65e12 死因 pom 必须被拦"
    assert any("groupId" in x for x in v)


def test_malformed_xml_flagged():
    bad = "<project><artifactId>x</artifactId"  # 截断，未闭合
    v = _pom_structure_violations(bad, "m/pom.xml")
    assert v and any("XML" in x or "解析" in x for x in v)


def test_missing_artifactid_flagged():
    bad = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <groupId>com.x</groupId><version>1.0</version></project>"""
    v = _pom_structure_violations(bad, "m/pom.xml")
    assert any("artifactId" in x for x in v)


def test_own_groupid_no_parent_ok():
    """自身有 groupId、无 parent → 合法（顶层工程 pom）。"""
    ok = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <groupId>com.x</groupId><artifactId>x</artifactId><version>1.0</version></project>"""
    assert _pom_structure_violations(ok, "pom.xml") == []


def test_missing_version_flagged():
    """★复核 MED★ 自身无 <version> 且无 parent → Maven 'version is missing'，须拦。"""
    bad = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <groupId>com.x</groupId><artifactId>x</artifactId></project>"""
    v = _pom_structure_violations(bad, "m/pom.xml")
    assert any("version" in x for x in v)


def test_version_inherited_from_parent_ok():
    """version 由 parent 继承（自身无 <version>）→ 合法（如 _GOOD）。"""
    assert _pom_structure_violations(_GOOD, "ruoyi-framework/pom.xml") == []


def test_parent_incomplete_flagged():
    """parent 存在但缺 version → Maven 解析失败，须拦。"""
    bad = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <parent><groupId>com.x</groupId><artifactId>p</artifactId></parent>
  <artifactId>c</artifactId></project>"""
    v = _pom_structure_violations(bad, "c/pom.xml")
    assert any("parent" in x.lower() for x in v)


# ── 不误伤 & 范围 ──
def test_non_pom_returns_empty():
    """非 pom 文件不校（本闸只管 Maven pom）。"""
    assert _pom_structure_violations("whatever", "src/App.java") == []


def test_empty_text_no_crash():
    # 读文件失败(None)/空 由调用方处理；空串不炸
    assert isinstance(_pom_structure_violations("", "m/pom.xml"), list)
