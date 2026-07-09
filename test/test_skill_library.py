"""经验拔插层 P0 · loader 测试（experience/library.py）。

覆盖：native 扁平 *.md 解析、imported <name>/SKILL.md（只 name/description）零编辑消费、
缺字段/坏 YAML/空正文/无 frontmatter 一律跳过（fail-open）、路由字段缺省宽默认、
数值容错、重复 id 保留先出现、多目录合并靠前优先、README 等文档静默跳过。
"""
from __future__ import annotations

from pathlib import Path

from swarm.experience.library import (
    load_skills,
    load_skills_from,
    parse_skill_text,
)


def _md(fm: str, body: str = "- x") -> str:
    """紧凑构造 `---\\nfm\\n---\\nbody\\n` 的技能文本，避免超长行。"""
    return f"---\n{fm}\n---\n{body}\n"


def _write(path, fm: str, body: str = "- x") -> None:
    path.write_text(_md(fm, body), encoding="utf-8")


_NATIVE = """\
---
id: my-skill
title: 我的技能
applies_to_stacks: ["python", "java"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code"]
target: ["worker", "planner"]
priority: 80
max_chars: 500
tags: ["idiom", "x"]
---
- 条目一
- 条目二
"""

_IMPORTED = """\
---
name: python-patterns
description: Pythonic idioms and best practices.
metadata:
  origin: ECC
---
# Python Development Patterns
- readable code
"""


def test_native_parse_all_fields():
    doc = parse_skill_text(_NATIVE, source_path="my-skill.md")
    assert doc is not None
    assert doc.id == "my-skill"
    assert doc.title == "我的技能"
    assert doc.applies_to_stacks == ("python", "java")
    assert doc.applies_to_intents == ("create", "modify")
    assert doc.applies_to_phases == ("code",)
    assert doc.target == ("worker", "planner")
    assert doc.priority == 80
    assert doc.max_chars == 500
    assert doc.tags == ("idiom", "x")
    assert doc.imported is False
    assert "条目一" in doc.body


def test_imported_skill_md_broad_defaults():
    """只有 name/description 的第三方 SKILL.md 零编辑可消费：路由落宽默认 + imported=True。"""
    doc = parse_skill_text(_IMPORTED, source_path="python-patterns/SKILL.md")
    assert doc is not None
    assert doc.id == "python-patterns"          # 来自 name
    assert doc.title == "python-patterns"        # 无 title → 回退 name
    assert doc.summary.startswith("Pythonic")    # 来自 description
    assert doc.applies_to_stacks == ("*",)
    assert doc.applies_to_intents == ("*",)
    assert doc.applies_to_phases == ("*",)
    assert doc.target == ("worker",)             # DEFAULT_TARGET
    assert doc.imported is True


def test_id_from_fallback_when_no_id_or_name():
    text = "---\ndescription: only desc\n---\n- body\n"
    # 无 id/name，但给了 fallback（如 <dir>/SKILL.md 的目录名）
    doc = parse_skill_text(text, source_path="x/SKILL.md", fallback_id="cool-skill")
    assert doc is not None and doc.id == "cool-skill"
    # 无 fallback → 跳过
    assert parse_skill_text(text, source_path="x/SKILL.md") is None


def test_defaults_when_routing_absent():
    text = "---\nid: a\ntitle: A\n---\n- x\n"
    doc = parse_skill_text(text)
    assert doc is not None
    assert doc.applies_to_stacks == ("*",)
    assert doc.applies_to_intents == ("*",)
    assert doc.applies_to_phases == ("*",)
    assert doc.priority == 50 and doc.max_chars == 1200
    assert doc.imported is True  # 无任何路由字段声明


def test_numeric_coercion_bad_values_fall_back():
    text = _md("id: a\ntitle: A\ntarget: [worker]\npriority: not-a-number\nmax_chars: []")
    doc = parse_skill_text(text)
    assert doc is not None
    assert doc.priority == 50 and doc.max_chars == 1200
    assert doc.imported is False  # 声明了 target → native


def test_missing_frontmatter_skipped():
    assert parse_skill_text("# just markdown\nno frontmatter\n") is None


def test_broken_yaml_skipped():
    bad = "---\nid: a\ntitle: [unclosed\n---\n- x\n"
    assert parse_skill_text(bad) is None


def test_empty_body_skipped():
    assert parse_skill_text("---\nid: a\ntitle: A\n---\n   \n") is None


def test_frontmatter_not_a_mapping_skipped():
    assert parse_skill_text("---\n- just\n- a\n- list\n---\nbody\n") is None


def test_capped_body_truncates():
    text = "---\nid: a\ntitle: A\nmax_chars: 20\n---\n" + ("x" * 200) + "\n"
    doc = parse_skill_text(text)
    assert doc is not None
    capped = doc.capped_body()
    assert "经验预算裁剪" in capped
    assert len(capped) < 200


def test_load_skills_missing_dir_returns_empty():
    assert load_skills("/no/such/skills/dir/xyz") == []


def test_load_skills_dir_and_dedup(tmp_path: Path):
    _write(tmp_path / "a.md", "id: dup\ntitle: A\ntarget: [worker]", "- a")
    _write(tmp_path / "b.md", "id: dup\ntitle: B\ntarget: [worker]", "- b")
    _write(tmp_path / "c.md", "id: other\ntitle: C\ntarget: [worker]", "- c")
    # README 与坏文件不应进结果，也不应报错
    (tmp_path / "README.md").write_text("# doc\nno frontmatter\n", encoding="utf-8")
    _write(tmp_path / "broken.md", "id: x\ntitle: [bad", "- x")
    docs = load_skills(tmp_path)
    ids = [d.id for d in docs]
    assert ids == ["dup", "other"]           # 按 id 排序；dup 保留先出现（a.md）
    assert next(d for d in docs if d.id == "dup").title == "A"


def test_load_skills_imported_dir_layout(tmp_path: Path):
    """<name>/SKILL.md 布局：无 name 时 id 取父目录名。"""
    d = tmp_path / "great-skill"
    d.mkdir()
    (d / "SKILL.md").write_text("---\ndescription: d\n---\n- body\n", encoding="utf-8")
    docs = load_skills(tmp_path)
    assert [x.id for x in docs] == ["great-skill"]
    assert docs[0].imported is True


def test_load_skills_from_earlier_dir_wins(tmp_path: Path):
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    d1.mkdir()
    d2.mkdir()
    _write(d1 / "s.md", "id: shared\ntitle: FROM_D1\ntarget: [worker]", "- a")
    _write(d2 / "s.md", "id: shared\ntitle: FROM_D2\ntarget: [worker]", "- b")
    _write(d2 / "extra.md", "id: extra\ntitle: E\ntarget: [worker]", "- c")
    docs = load_skills_from([d1, d2])
    by_id = {d.id: d for d in docs}
    assert by_id["shared"].title == "FROM_D1"   # 靠前目录覆盖
    assert "extra" in by_id


def test_bad_byte_file_isolated_not_whole_dir(tmp_path: Path):
    """复核发现：非 UTF-8 文件曾让整目录变空（UnicodeDecodeError 非 OSError）。
    现在只跳过坏文件，同目录好技能照常加载。"""
    _write(tmp_path / "aa-good.md", "id: good\ntitle: G\ntarget: [worker]", "- ok")
    (tmp_path / "zz-bad.md").write_bytes(b"---\nid: bad\n---\n\xff\xfe not utf8\n")
    docs = load_skills(tmp_path)
    assert [d.id for d in docs] == ["good"]           # 坏文件被隔离，好文件存活
    docs2 = load_skills_from([tmp_path])
    assert [d.id for d in docs2] == ["good"]


def test_seed_library_loads():
    """内置种子库应能全部解析（回归：种子 frontmatter 合法）。"""
    from swarm.config.settings import PROJECT_ROOT

    docs = load_skills(PROJECT_ROOT / "skills_library")
    ids = {d.id for d in docs}
    assert {"coding-standards-core", "api-design", "python-patterns", "springboot-patterns"} <= ids
    for d in docs:
        assert d.imported is False  # 种子全是 native（显式路由）
