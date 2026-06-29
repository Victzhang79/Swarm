#!/usr/bin/env python3
"""round8 治本回归：_reconcile_maven_module_registration 把被跨子任务冲掉的内部子模块
补注册进根 pom <modules>，让 reactor 能构建它（杜绝 ruoyi-admin 找不到 ruoyi-alarm:jar 的
确定性 FAIL + 缓存负解析）。"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from swarm.worker.l1_pipeline import _maven_modules, _reconcile_maven_module_registration

_ROOT_POM = """<project>
  <groupId>com.ruoyi</groupId>
  <artifactId>ruoyi</artifactId>
  <version>4.8.3</version>
  <packaging>pom</packaging>
  <modules>
    <module>ruoyi-admin</module>
    <module>ruoyi-common</module>
    <module>alarm-interface</module>
  </modules>
</project>
"""

_CHILD_POM = """<project>
  <parent>
    <groupId>com.ruoyi</groupId>
    <artifactId>ruoyi</artifactId>
    <version>4.8.3</version>
  </parent>
  <artifactId>{aid}</artifactId>
</project>
"""

_STANDALONE_POM = """<project>
  <groupId>org.other</groupId>
  <artifactId>standalone-thing</artifactId>
  <version>1.0</version>
</project>
"""


def _make_project() -> str:
    d = tempfile.mkdtemp(prefix="swarm_reactor_")
    root = Path(d)
    (root / "pom.xml").write_text(_ROOT_POM, encoding="utf-8")
    # 已注册模块
    for m in ("ruoyi-admin", "ruoyi-common", "alarm-interface"):
        (root / m).mkdir()
        (root / m / "pom.xml").write_text(_CHILD_POM.format(aid=m), encoding="utf-8")
    # 未注册但确属本工程子模块（有 <parent>）— 应被补注册
    (root / "ruoyi-alarm").mkdir()
    (root / "ruoyi-alarm" / "pom.xml").write_text(_CHILD_POM.format(aid="ruoyi-alarm"), encoding="utf-8")
    # 未注册的独立工程（无 <parent>）— 不应被碰
    (root / "vendor-lib").mkdir()
    (root / "vendor-lib" / "pom.xml").write_text(_STANDALONE_POM, encoding="utf-8")
    return d


def test_registers_missing_child_module():
    d = _make_project()
    # _maven_modules 一开始扫不到 ruoyi-alarm（这正是 _scope 无法 -pl 收窄的原因）
    assert "ruoyi-alarm" not in _maven_modules(d)
    added = _reconcile_maven_module_registration(d, ["ruoyi-alarm/src/main/java/com/ruoyi/alarm/X.java"])
    assert added == ["ruoyi-alarm"], added
    root_text = (Path(d) / "pom.xml").read_text()
    assert "<module>ruoyi-alarm</module>" in root_text
    # 注册后 _maven_modules 能扫到 → _scope_maven_command 即可 -pl 收窄
    assert "ruoyi-alarm" in _maven_modules(d)


def test_does_not_touch_standalone_or_registered():
    d = _make_project()
    added = _reconcile_maven_module_registration(d, ["ruoyi-alarm/src/X.java"])
    root_text = (Path(d) / "pom.xml").read_text()
    # 独立工程(无 <parent>)不注册
    assert "<module>vendor-lib</module>" not in root_text
    # 已注册的不重复
    assert root_text.count("<module>ruoyi-common</module>") == 1
    assert "vendor-lib" not in added


def test_idempotent():
    d = _make_project()
    first = _reconcile_maven_module_registration(d, ["ruoyi-alarm/src/X.java"])
    assert first == ["ruoyi-alarm"]
    second = _reconcile_maven_module_registration(d, ["ruoyi-alarm/src/X.java"])
    assert second == [], "二次对账应无新增(幂等)"
    # <modules> 里只一条 ruoyi-alarm
    root_text = (Path(d) / "pom.xml").read_text()
    assert root_text.count("<module>ruoyi-alarm</module>") == 1


def test_no_modules_block_skips():
    """根 pom 非聚合器(无 <modules>)→ 不擅自改结构。"""
    d = tempfile.mkdtemp(prefix="swarm_reactor_")
    (Path(d) / "pom.xml").write_text("<project><artifactId>solo</artifactId></project>", encoding="utf-8")
    assert _reconcile_maven_module_registration(d, ["x/Y.java"]) == []


if __name__ == "__main__":
    test_registers_missing_child_module()
    test_does_not_touch_standalone_or_registered()
    test_idempotent()
    test_no_modules_block_skips()
    print("ok")
