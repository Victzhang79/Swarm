"""阶段E.9（双复核治理批）：hunter/reviewer CONFIRMED 条目行为锁。

HF1/RF6 G10 更正：各批独立 LLM 调用非同上下文重复——恢复每批注入（旧 helper off-by-one
   实为零注入）。锁在 test_phase_e_batch3_arch。
HF2/RF1 G6 DB 信号死接线：detect_stack 丢弃清单里的驱动坐标 → 确定性 db 面 + selector 主通道。
RF2/RF3 push 无框架粒度：FastAPI 项目 push django-security / Vue 项目挂 react 丢 vue →
   框架词元双向前缀提权 + push 门槛（框架命中或语言前缀）。
RF14 G2×G3 通配层死刑 → 末位通配保底。RF5 wmt=0 全关。HF6/RF4/RF7 preview 诚实化+钳制。
HF3 imported 缺 desc 可诊断。HF5 disabled DB 行遮蔽内置。RF10 enabled fail-closed。
"""

from __future__ import annotations

from swarm.experience.models import SkillDoc
from swarm.experience.selector import (
    profile_terms_from_project_stack,
    select_skills,
    stack_langs_from_project_stack,
)


def _skill(sid, *, stacks=("*",), priority=50, target=("worker",), summary="s",
           tags=()):
    return SkillDoc(id=sid, title=sid, body="x" * 200, priority=priority,
                    applies_to_stacks=tuple(stacks), target=tuple(target),
                    summary=summary, tags=tuple(tags))


# ─────────────── HF2/RF1：DB 面 ───────────────


def test_e9_stack_detect_emits_db_facet(tmp_path):
    from swarm.brain.stack_detect import detect_stack_deterministic
    (tmp_path / "pom.xml").write_text(
        "<project><dependencies><dependency>"
        "<groupId>mysql</groupId><artifactId>mysql-connector-java</artifactId>"
        "</dependency></dependencies></project>")
    (tmp_path / "src").mkdir()
    prof = detect_stack_deterministic(str(tmp_path))
    assert prof.get("db") == ["mysql"], (
        "驱动坐标就在清单里（RuoYi 的 mysql-connector-java）——信号在手边却被丢弃，"
        "导致 DB 特化技能主路径永不挂载（复核 HF2/RF1 实证）")


def test_e9_selector_reads_db_facet():
    langs = stack_langs_from_project_stack(
        {"frontend": "Thymeleaf", "backend": "Spring Boot 2.x (java)",
         "build": "maven", "db": ["mysql"]})
    assert "mysql" in langs and "postgres" not in langs


# ─────────────── RF2/RF3：框架粒度 ───────────────


def test_e9_push_framework_hit_beats_alphabetical():
    import swarm.experience.service as svc
    skills = [
        _skill("django-security", stacks=("python",), priority=50),
        _skill("fastapi-patterns", stacks=("python",), priority=50),
    ]
    orig = svc._merged_skills
    svc._merged_skills = lambda dirs: skills
    try:
        class _Sub:
            id = "st-1"
            intent = "create"
        pushes, pulls = svc.select_worker_push_pull(
            _Sub(), {"frontend": "", "backend": "FastAPI (python)", "build": "pip"})
    finally:
        svc._merged_skills = orig
    assert pushes and pushes[0].id == "fastapi-patterns" and \
        "django-security" not in {p.id for p in pushes}, (
        "语言级栈轴 + id 字母序 tiebreak 会把 django-security 全文 push 给 FastAPI"
        "项目（复核 RF2 实证）——框架词元命中必须先于字母序")


def test_e9_vue_project_keeps_vue_over_react():
    skills = [
        _skill("react-patterns", stacks=("node",), priority=50),
        _skill("vue-patterns", stacks=("node",), priority=48),
    ]
    terms = profile_terms_from_project_stack(
        {"frontend": "Vue3 (独立前端)", "backend": "Spring Boot (java)",
         "build": "maven"})
    picked = select_skills(skills, stack_langs={"node", "java"}, intent="create",
                           phase="code", target="worker", budget_chars=10**9,
                           max_k=1, profile_terms=terms)
    assert [s.id for s in picked] == ["vue-patterns"], (
        "vue/react 同映射 node 层内按 priority+字母序会给 Vue 项目挂 React 经验"
        "（复核 RF3 实证）")


# ─────────────── RF14：通配保底 ───────────────


def test_e9_wildcard_floor_when_specialized_saturate():
    skills = [_skill(f"java-s{i}", stacks=("java",), priority=60 - i)
              for i in range(4)] + [_skill("error-handling", priority=55)]
    picked = select_skills(skills, stack_langs={"java"}, intent="create",
                           phase="code", target="worker", budget_chars=10**9,
                           max_k=3)
    assert any("*" in s.applies_to_stacks for s in picked), (
        "G2(3 位)×G3(特化绝对优先) 会让主流栈项目的通配层（error-handling/api-design/"
        "imported 全部）一条都不可达（复核 RF14）——末位保底一条最优通配")


# ─────────────── RF5：wmt=0 全关 ───────────────


def test_e9_zero_max_tools_disables_push_too(monkeypatch):
    import swarm.experience.service as svc
    monkeypatch.setattr(svc, "_merged_skills",
                        lambda dirs: [_skill("java-coding-standards",
                                             stacks=("java",), priority=60)])
    from swarm.config.settings import get_config
    monkeypatch.setattr(get_config().skills, "worker_max_tools", 0)

    class _Sub:
        id = "st-1"
        intent = "create"
    pushes, pulls = svc.select_worker_push_pull(
        _Sub(), {"frontend": "", "backend": "Spring (java)", "build": "maven"})
    assert pushes == [] and pulls == [], (
        "『0 = 不挂经验工具』的配置承诺不能静默漂移成『只关 pull』（复核 RF5）")


# ─────────────── HF6/RF4：preview 诚实化 ───────────────


def test_e9_preview_reports_enabled_flags():
    from swarm.experience.service import preview_mount_surfaces
    doc = _skill("p1", stacks=("python",))
    object.__setattr__(doc, "enabled", False)
    out = preview_mount_surfaces(doc)
    assert out.get("doc_enabled") is False and "layer_enabled" in out \
        and "pool_size" in out, (
        "disabled 保存/层开关关/库空 与『真不匹配』必须可区分（复核 HF6）")
    assert "note" in out, "单栈模拟 vs 多栈现实的排位漂移必须明示（复核 RF4）"
    assert all("displaces" not in s for s in out["surfaces"]), (
        "displaces 在 push 情形算错且前端不渲染=又错又死（复核 RF9），已删")


# ─────────────── HF3/RF10/HF5 ───────────────


def test_e9_imported_missing_desc_diagnosable():
    from swarm.experience.validation import validate_skill_text
    r = validate_skill_text("---\nname: ecc-x\n---\n正文内容。")
    assert not r.ok and any("description" in e for e in r.errors), (
        "G11 新拒因落到『未知解析错误』=用户不知所云（复核 HF3）")


def test_e9_enabled_parse_fail_closed():
    from swarm.experience.library import parse_skill_text
    base = "---\nid: a\ntitle: A\ndescription: 当你在做 X 时调用\ntarget: [worker]\nenabled: {v}\n---\n- x\n"
    for v in ('"off"', '"disabled"', "off", "false", "0"):
        doc = parse_skill_text(base.format(v=v))
        assert doc is not None and doc.enabled is False, (
            f"enabled: {v} 必须按 disabled——手滑加引号不能把策展下架件悄悄放回"
            "（复核 RF10：拔插开关 fail-closed）")
    doc = parse_skill_text(base.format(v="true"))
    assert doc is not None and doc.enabled is True


def test_e9_disabled_db_override_shadows_builtin(monkeypatch):
    import swarm.experience.service as svc
    from swarm.config import skill_store
    builtin = _skill("redis-patterns", stacks=("*",), priority=50)
    db_disabled = _skill("redis-patterns", stacks=("*",), priority=50)
    object.__setattr__(db_disabled, "enabled", False)
    monkeypatch.setattr(svc, "_load_cached", lambda dirs: [builtin])
    monkeypatch.setattr(skill_store, "get_enabled_docs", lambda *a, **k: [db_disabled])
    merged = svc._merged_skills(["skills_library"])
    hit = [d for d in merged if d.id == "redis-patterns"]
    assert hit and hit[0].enabled is False, (
        "用户禁用同 id 的 DB 覆盖件后，旧行为让内置原版静默复活（复核 HF5）——"
        "disabled 行必须遮蔽内置，选择器统一排除")
