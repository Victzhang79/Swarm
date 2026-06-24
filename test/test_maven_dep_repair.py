#!/usr/bin/env python3
"""治本 A2：缺依赖确定性补全（定向恢复授权后据项目自身 pom 自证坐标注入失败模块 pom）。

背景：小模型即便拿到 pom 写权也常不会把缺的依赖加进去（实测 RuoYi st-31：用 org.quartz 但
ruoyi-alarm/pom.xml 没声明 → 2 次定向恢复耗尽 → 落全量 replan 砸掉 30 个成功子任务）。这里把
"加依赖"从靠小模型改成确定性：从编译错误取缺失包 → 项目其它 pom 里找声明了它的 <dependency> →
注入失败模块 pom。项目从没用过的包 → 查无、不臆造坐标。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import swarm.brain.nodes as N


def test_pkg_match_tokens():
    assert N._pkg_match_tokens("org.quartz") == ["quartz"]
    assert N._pkg_match_tokens("okhttp3") == ["okhttp3", "okhttp"]
    # 通用段(org/com)+短段被剔除
    toks = N._pkg_match_tokens("com.fasterxml.jackson.databind")
    assert "jackson" in toks and "databind" in toks and "com" not in toks


def test_extract_missing_pkgs_zh_and_en():
    blob = ("[ERROR] A.java:[5,17] 程序包 org.quartz 不存在\n"
            "[ERROR] B.java:[3,1] package okhttp3 does not exist\n"
            "[ERROR] C.java: 程序包 org.quartz 不存在")  # 去重
    pkgs = N._extract_missing_pkgs(blob)
    assert pkgs == ["org.quartz", "okhttp3"]


def _mini_project() -> str:
    """造一个含【兄弟 pom 声明了 quartz】的迷你 maven 工程。"""
    d = tempfile.mkdtemp()
    root = Path(d)
    # 兄弟模块 scheduler 声明 quartz（自证坐标源）
    (root / "scheduler").mkdir()
    (root / "scheduler" / "pom.xml").write_text(
        "<project><dependencies>\n"
        "  <dependency><groupId>org.springframework.boot</groupId>"
        "<artifactId>spring-boot-starter-quartz</artifactId></dependency>\n"
        "</dependencies></project>\n")
    # 失败模块 alarm：有 dependencies 段但缺 quartz
    (root / "alarm").mkdir()
    (root / "alarm" / "pom.xml").write_text(
        "<project><dependencies>\n"
        "  <dependency><groupId>com.x</groupId><artifactId>common</artifactId></dependency>\n"
        "</dependencies></project>\n")
    return d


def test_find_and_inject_dep_self_evidenced():
    d = _mini_project()
    # 自证解析：org.quartz → 兄弟 pom 里的 spring-boot-starter-quartz（排除 alarm 自身）
    dep = N._find_maven_dep_for_pkg(d, "org.quartz", "alarm/pom.xml")
    assert dep and "quartz" in dep.lower()
    alarm_pom = Path(d) / "alarm" / "pom.xml"
    assert N._inject_dep_into_pom(alarm_pom, dep) is True
    assert "quartz" in alarm_pom.read_text().lower()
    # 幂等：再注入同 artifactId 不重复
    assert N._inject_dep_into_pom(alarm_pom, dep) is False


def test_no_fabrication_for_unknown_package():
    """项目从没用过的包 → 查无权威坐标 → 返回 None，绝不臆造依赖。"""
    d = _mini_project()
    assert N._find_maven_dep_for_pkg(d, "com.example.totallymadeup", "alarm/pom.xml") is None


def test_inject_skips_when_no_dependencies_section():
    """无 <dependencies> 段的 pom → 保守不动（不新建段，免破坏结构）。"""
    d = tempfile.mkdtemp()
    pom = Path(d) / "pom.xml"
    pom.write_text("<project><artifactId>x</artifactId></project>\n")
    dep = "<dependency><groupId>g</groupId><artifactId>a</artifactId></dependency>"
    assert N._inject_dep_into_pom(pom, dep) is False


def test_orchestrator_injects_from_build_output():
    """端到端：据 subtask_results 的 build_output 把缺失依赖补进 granted 的模块 pom。"""
    d = _mini_project()
    granted = {"st-31": "alarm/pom.xml"}
    subtask_results = {"st-31": {"l1_details": {
        "build_output": "[ERROR] AlarmUpgradeTask.java:[5,17] 程序包 org.quartz 不存在"}}}
    injected = N._inject_missing_maven_deps(d, granted, subtask_results)
    assert "st-31" in injected and injected["st-31"]
    assert "quartz" in (Path(d) / "alarm" / "pom.xml").read_text().lower()


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    bad = 0
    for fn in fns:
        try:
            fn(); print(f"  ✅ {fn.__name__}")
        except AssertionError as e:
            bad += 1; print(f"  ❌ {fn.__name__}: {e}")
    sys.exit(1 if bad else 0)
