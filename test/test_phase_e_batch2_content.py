"""阶段E 批2（登记册 §七b）：内容可执行性治理——G5/G6/G7 行为锁。

G5 死件+矛盾件：git-workflow phases=["produce"]（全系统只查 code/plan=永不选中）且
   内容（push/tag/PR）与无 .git 沙箱全面矛盾；docker/k8s/deployment 教 worker 写
   scope 外文件（Dockerfile/K8s YAML）→ scope_guard 拒绝白烧迭代 → enabled:false。
G6 通配 niche 稀释选择面：cost-aware-llm-pipeline/content-hash-cache/regex-vs-llm
   任何项目都候选 → disabled；mysql+postgres 双通配必双挂（必吃一份错库建议）→
   栈标签互斥（探测出才挂，探不出都不挂——画像无 DB 面时宁缺勿错）。
G7 重复注入占位：coding-standards-core 与 L2 _CORE_RULES 重叠且 priority 70 永占
   3 个工具位之一 → 去重让位（正文只留 L2 没有的独有纪律，priority 降）。
"""

from __future__ import annotations

from swarm.experience.library import load_skills, parse_skill_text
from swarm.experience.models import SkillDoc
from swarm.experience.selector import select_skills, stack_langs_from_project_stack


def _load():
    return {d.id: d for d in load_skills("skills_library")}


# ─────────────── enabled 机制 ───────────────


def test_enabled_field_parsed_and_selector_excludes():
    doc = parse_skill_text(
        "---\nid: a\ntitle: A\ndescription: 当你在做 X 时调用\ntarget: [worker]\n"
        "enabled: false\n---\n- x\n")
    assert doc is not None and doc.enabled is False, (
        "enabled 是文件级拔插开关——解析必须保留 doc（前端/审计可见），选择器负责排除")
    picked = select_skills([doc], stack_langs=set(), intent="create", phase="code",
                           target="worker", budget_chars=10**9, max_k=5)
    assert picked == [], "disabled 技能绝不进任何注入面/工具面"


def test_enabled_defaults_true():
    doc = parse_skill_text(
        "---\nid: a\ntitle: A\ndescription: 当你在做 X 时调用\ntarget: [worker]\n---\n- x\n")
    assert doc is not None and doc.enabled is True


# ─────────────── G5：死件/矛盾件下架 ───────────────


def test_g5_contradictory_infra_skills_disabled():
    docs = _load()
    for sid in ("git-workflow", "docker-patterns", "kubernetes-patterns",
                "deployment-patterns"):
        assert docs[sid].enabled is False, (
            f"{sid}：git-workflow 教 push/tag 而沙箱无 .git（round20#13）；infra 三件教"
            "写 Dockerfile/K8s YAML=scope 外文件，scope_guard 必拒白烧迭代——默认下架，"
            "正文改写为 scope 内指导前不回架")


# ─────────────── G6：niche 通配收敛 + DB 互斥 ───────────────


def test_g6_niche_wildcards_disabled():
    docs = _load()
    for sid in ("cost-aware-llm-pipeline", "content-hash-cache-pattern",
                "regex-vs-llm-structured-text"):
        assert docs[sid].enabled is False, (
            f"{sid}：stacks=['*'] 的 niche 技能任何 create/modify 都候选（栈探测失败时"
            "实测入选）——与绝大多数产品化任务无关，稀释 3 个工具位")


def test_g6_db_skills_stack_scoped_not_wildcard():
    docs = _load()
    assert "*" not in docs["mysql-patterns"].applies_to_stacks
    assert "*" not in docs["postgres-patterns"].applies_to_stacks, (
        "双 DB 通配=任何项目双挂必吃一份错库建议——改栈标签互斥挂载")


def test_g6_db_detected_from_profile_text():
    langs = stack_langs_from_project_stack(
        {"frontend": "Thymeleaf", "backend": "Spring Boot 2.x (java) + MySQL",
         "build": "maven"})
    assert "mysql" in langs and "postgres" not in langs, (
        "画像文本提及 MySQL → 只挂 mysql-patterns（互斥）")


def test_g6_no_db_in_profile_mounts_neither():
    skills = list(_load().values())
    langs = stack_langs_from_project_stack(
        {"frontend": "Thymeleaf", "backend": "Spring Boot 2.x (java)",
         "build": "maven"})
    picked = select_skills(skills, stack_langs=langs, intent="create", phase="code",
                           target="worker", budget_chars=10**9, max_k=50)
    ids = {s.id for s in picked}
    assert not ({"mysql-patterns", "postgres-patterns"} & ids), (
        "画像探不出 DB → 都不挂（宁缺勿错，错库建议是负资产）")


# ─────────────── G7：重复注入让位 ───────────────


def test_g7_coding_standards_core_deduped_and_demoted():
    docs = _load()
    core = docs["coding-standards-core"]
    assert core.priority < 70, (
        "priority 70 使其永占 worker 3 个工具位的第 1 位——与 L2 _CORE_RULES 重叠的"
        "内容不配这个位置")
    # L2 已常注入的铁律不该在技能正文里复读（去重后只留 L2 没有的独有纪律）
    for dup_marker in ("顺手改无关", "与周边同风格", "不吞异常"):
        assert dup_marker not in core.body, (
            f"『{dup_marker}』已由 worker/coding_standards.py L2 _CORE_RULES 常注入——"
            "技能正文复读=白占 pull 正文预算")
    assert "fail-closed" in core.body or "默认拒绝" in core.body, (
        "L2 没有的独有纪律（默认拒绝/入口对称/降级可观测）必须保留")
