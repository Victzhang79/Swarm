"""阶段E 批4（登记册 §七b）：G9 前端添加路径质量闸——API 侧行为锁。

G9 质量闸只挡恶意不挡平庸：表单默认全通配 + priority 可 100 → 一条平庸用户技能必占
   worker 工具面第 1 位；无挂载预览/试运行；克隆生成 id+'-custom' 双份占两位。
治：①挂载预览端点（保存前展示会出现在哪些 栈×意图 的注入面及 push/pull 排位）；
   ②priority>80 准入闸 warning（前端据此二次确认）；③克隆改同 id override（JS 侧，
   DB 同 id 覆盖内置=只挂一份）；④worker 缺 description 升 error（批1 已做）。
"""

from __future__ import annotations

import pytest
import swarm.experience.validation as validation
from fastapi.testclient import TestClient

from swarm.api.app import app

client = TestClient(app)

_GOOD = {
    "id": "preview-probe-skill", "title": "预览探针技能",
    "description": "当你在写 Python 数据层时调用：返回探针规则",
    "body": "- 规则一：显式事务边界。\n- 规则二：禁可变默认参数。\n- 规则三：类型注解齐全。",
    "applies_to_stacks": ["python"], "applies_to_intents": ["create"],
    "applies_to_phases": ["code"], "target": ["worker"], "priority": 99, "max_chars": 1200,
}


@pytest.fixture(autouse=True)
def _no_llm_judge(monkeypatch):
    monkeypatch.setattr(validation, "_admit_judge_enabled", lambda: False)


def test_g9_preview_endpoint_reports_mount_surfaces():
    r = client.post("/api/skills/preview", json={"skill": _GOOD})
    assert r.status_code == 200, r.text
    data = r.json()
    surfaces = data.get("surfaces") or []
    assert surfaces, "挂载预览必须返回 栈×意图 面清单（保存前可见影响面）"
    hit = [s for s in surfaces
           if s.get("stack") == "python" and s.get("intent") == "create"]
    assert hit and hit[0].get("mounted") is True, (
        "priority 99 的 python 技能在 python×create 面必然挂载——预览必须如实展示")
    assert hit[0].get("mode") in ("push", "pull")
    assert "rank" in hit[0]


def test_g9_preview_wildcard_probes_representative_stacks():
    wide = {**_GOOD, "id": "wide-probe", "applies_to_stacks": ["*"], "priority": 100}
    r = client.post("/api/skills/preview", json={"skill": wide})
    assert r.status_code == 200
    stacks = {s.get("stack") for s in r.json().get("surfaces") or []}
    assert len(stacks) >= 3, (
        "通配技能预览必须跨代表性栈探测——用户要看到'这条会占每个栈的工具面'")


def test_g9_priority_over_80_gets_admission_warning():
    from swarm.experience.models import SkillDoc
    from swarm.experience.validation import validate_skill_doc
    doc = SkillDoc(id="p99", title="t", body="x" * 80, priority=99,
                   target=("worker",), summary="当你在做 X 时调用")
    r = validate_skill_doc(doc, use_llm_judge=False)
    assert r.ok, "高 priority 不是 error（用户有权置顶）"
    assert any("priority" in w for w in r.warnings), (
        "priority>80 必占各工具面头位——准入闸必须 warning（前端据此二次确认），"
        "否则一条平庸技能静默挤掉全部策展技能")


def test_g9_clone_uses_same_id_override():
    js = open("api/static/js/tabs/skills.js", encoding="utf-8").read()
    assert "'-custom'" not in js and '"-custom"' not in js, (
        "克隆生成 id+'-custom' 会与原件双份占两个工具位——DB 同 id 覆盖内置"
        "（_merged_skills 语义）才是 override 正道")
