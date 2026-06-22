"""技术栈识别确定性探测单测（纯函数，不连 DB / 不调 LLM）。

治本 task 8537fa5e：栈是磁盘客观属性，确定性优先识别，杜绝"RuoYi=Vue"先验/文档假设误判。
"""
from __future__ import annotations

import os

from swarm.brain.stack_detect import (
    compute_repo_fingerprint,
    detect_stack_deterministic,
    format_stack_for_prompt,
)


def _mk(tmp, rel, content="x"):
    p = os.path.join(tmp, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(content)


def test_thymeleaf_monolith(tmp_path):
    """经典单体：Maven + Spring + thymeleaf 依赖 + templates/*.html，判服务端模板、禁 Vue。"""
    t = str(tmp_path)
    _mk(t, "pom.xml", "<project><dependency>spring-boot-starter-thymeleaf</dependency></project>")
    _mk(t, "src/main/resources/templates/sys/user/list.html", "<html/>")
    _mk(t, "src/main/resources/templates/index.html", "<html/>")
    _mk(t, "src/main/java/com/x/UserController.java", "class X{}")
    p = detect_stack_deterministic(t)
    assert p["frontend_kind"] == "server-template"
    assert "Thymeleaf" in p["frontend"]
    assert p["backend"].lower().startswith("spring") or "java" in p["backend"].lower()
    assert p["build"] == "maven"
    assert p["confidence"] >= 0.65
    fp = format_stack_for_prompt(p)
    assert "禁止】生成 .vue" in fp and "ground truth" in fp


def test_vue_spa(tmp_path):
    t = str(tmp_path)
    _mk(t, "package.json", '{"dependencies":{"vue":"^3","vite":"^5"}}')
    _mk(t, "vite.config.js", "export default {}")
    _mk(t, "src/views/Home.vue", "<template/>")
    p = detect_stack_deterministic(t)
    assert p["frontend_kind"] == "spa"
    assert p["frontend"] == "Vue"


def test_separated_frontend_backend(tmp_path):
    """前后端分离：后端 templates + 独立 ruoyi-ui Vue 工程 → separated，不禁 Vue。"""
    t = str(tmp_path)
    _mk(t, "pom.xml", "<project>thymeleaf</project>")
    _mk(t, "ruoyi-admin/src/main/resources/templates/index.html", "<html/>")
    _mk(t, "ruoyi-ui/package.json", '{"dependencies":{"vue":"^2"}}')
    _mk(t, "ruoyi-ui/src/views/x.vue", "<template/>")
    p = detect_stack_deterministic(t)
    assert p["frontend_kind"] == "separated"
    fp = format_stack_for_prompt(p)
    assert "禁止】生成 .vue" not in fp


def test_django(tmp_path):
    t = str(tmp_path)
    _mk(t, "manage.py", "import django")
    _mk(t, "requirements.txt", "django>=4\npsycopg2")
    _mk(t, "app/templates/app/index.html", "{% block %}")
    p = detect_stack_deterministic(t)
    assert "python" in p["backend"].lower() or "Django" in p["backend"]


def test_react_jsx(tmp_path):
    t = str(tmp_path)
    _mk(t, "package.json", '{"dependencies":{"react":"^18","next":"14"}}')
    _mk(t, "next.config.js", "module.exports={}")
    _mk(t, "pages/index.tsx", "export default ()=>null")
    p = detect_stack_deterministic(t)
    assert p["frontend_kind"] == "spa"
    assert "React" in p["frontend"] or "Next" in p["frontend"]


def test_low_confidence_flags_adjudication(tmp_path):
    """啥都没扫到 → 低置信 → 标记需模型兜底。"""
    t = str(tmp_path)
    _mk(t, "readme.txt", "hello")
    p = detect_stack_deterministic(t)
    assert p["needs_model_adjudication"] is True


def test_fingerprint_stable_then_changes(tmp_path):
    t = str(tmp_path)
    _mk(t, "pom.xml", "<project/>")
    _mk(t, "src/main/java/X.java", "class X{}")
    fp1 = compute_repo_fingerprint(t)
    # 普通源码改动不改指纹
    _mk(t, "src/main/java/Y.java", "class Y{}")
    assert compute_repo_fingerprint(t) == fp1
    # 新增构建清单（栈相关）→ 指纹变
    _mk(t, "ruoyi-ui/package.json", '{"vue":"3"}')
    assert compute_repo_fingerprint(t) != fp1


def test_none_profile_format_empty():
    assert format_stack_for_prompt(None) == ""


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
