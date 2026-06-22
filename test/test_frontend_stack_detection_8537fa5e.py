"""治本 task 8537fa5e：tech_design 技术栈误判（在服务端模板单体里产 SPA 死代码）。

根因：项目结构扫描器只采样后端分层，从不看前端/构建事实 → tech_design LLM 缺栈事实 →
用训练先验 + 需求文档框架假设硬判 → 产出与项目实际栈不符的死代码。

修复（框架无关）：_gather_project_facts 客观采集【磁盘事实】——扩展名分布 + 构建清单 +
前端形态信号(服务端模板 vs SPA 组件) + 独立前端工程信号 + 样例文件，交大模型判定真实栈，
并声明磁盘事实优先于需求文档的框架假设。本测守住"事实被如实采集且可供 LLM 判定"，
不在代码里写死任何具体框架。
"""
from __future__ import annotations

import os

from swarm.brain.planning_nodes import _gather_project_facts


def _mk(tmp, rel, content="x"):
    p = os.path.join(tmp, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(content)


def test_surfaces_disk_facts_header_and_priority():
    """无论什么栈，都输出"磁盘事实/ground truth/优先于需求文档"的判定框架。"""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        _mk(t, "src/main/java/com/x/Foo.java")
        out = _gather_project_facts(t)
    assert "磁盘事实" in out and "ground truth" in out
    assert "真实技术栈" in out
    assert "优先" in out and "需求文档" in out


def test_serverside_template_signal_counted(tmp_path):
    """服务端模板栈：.html 计入"服务端模板"信号、SPA 计 0、无独立前端工程。"""
    t = str(tmp_path)
    _mk(t, "admin/src/main/resources/templates/x/list.html", "<html/>")
    _mk(t, "admin/src/main/resources/templates/index.html", "<html/>")
    _mk(t, "system/src/main/java/com/x/FooController.java")
    out = _gather_project_facts(t)
    assert "服务端模板文件" in out
    assert "SPA 组件文件(.vue/.jsx/.tsx/.svelte)共 0" in out
    assert "pom" not in out.lower() or ".java" in out  # 扩展名分布出现
    # 不写死框架名：输出里不应硬编 'Thymeleaf=' 这类裁决
    assert "本项目前端=" not in out


def test_spa_signal_and_frontend_project(tmp_path):
    """SPA 栈：.vue 计入 SPA 信号、独立前端工程(package.json)被识别。"""
    t = str(tmp_path)
    _mk(t, "ui/package.json", '{"dependencies":{"vue":"^3"}}')
    _mk(t, "ui/src/views/x.vue", "<template/>")
    _mk(t, "ui/src/api/x.js", "export default {}")
    out = _gather_project_facts(t)
    assert "SPA 组件文件" in out
    assert "独立前端工程目录" in out and "ui" in out
    assert "package.json" in out  # 构建清单被采集


def test_ext_histogram_present(tmp_path):
    t = str(tmp_path)
    for i in range(3):
        _mk(t, f"src/a{i}.py")
    _mk(t, "pyproject.toml", "[project]")
    out = _gather_project_facts(t)
    assert ".py×3" in out
    assert "pyproject.toml" in out


def test_no_hardcoded_framework_names(tmp_path):
    """治本核查：函数体不再对单一框架写死指令（不出现 RuoYi/AjaxResult 之类项目专有词）。"""
    t = str(tmp_path)
    _mk(t, "src/main/resources/templates/a.html", "<html/>")
    out = _gather_project_facts(t)
    for banned in ("ruoyi-admin", "AjaxResult", "TableDataInfo", "@Controller"):
        assert banned not in out, f"输出不应写死 {banned}"


def test_none_path_graceful():
    assert "无项目路径" in _gather_project_facts(None)


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
