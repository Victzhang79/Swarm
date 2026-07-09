"""DB 技能存储的转换 + loader 合并逻辑（不需要真 PG，mock 掉 DB 读取）。"""
from __future__ import annotations

import swarm.config.skill_store as store
import swarm.experience.service as svc
from swarm.experience.models import SkillDoc


def test_row_to_dict_parses_arrays_and_bad_json():
    row = ("x", "T", "d", "body", '["python","java"]', '["create"]', '["code"]',
           '["worker"]', 60, 1500, '["t1"]', True, "user")
    d = store._row_to_dict(row)
    assert d["applies_to_stacks"] == ["python", "java"]
    assert d["target"] == ["worker"] and d["priority"] == 60 and d["enabled"] is True
    # 坏 JSON → 回退默认
    bad = ("y", "T", "d", "b", "not-json", "[", "x", "", 50, 1200, "", False, "import")
    d2 = store._row_to_dict(bad)
    assert d2["applies_to_stacks"] == ["*"] and d2["target"] == ["worker"]
    assert d2["enabled"] is False


def test_get_enabled_docs_filters_and_converts(monkeypatch):
    rows = [
        {"id": "a", "title": "A", "description": "da", "body": "ba", "enabled": True,
         "applies_to_stacks": ["python"], "applies_to_intents": ["*"],
         "applies_to_phases": ["code"], "target": ["worker"], "priority": 50,
         "max_chars": 1200, "tags": ["x"]},
        {"id": "b", "title": "B", "description": "", "body": "bb", "enabled": False,
         "applies_to_stacks": ["*"], "applies_to_intents": ["*"], "applies_to_phases": ["*"],
         "target": ["worker"], "priority": 50, "max_chars": 1200, "tags": []},
    ]
    monkeypatch.setattr(store, "get_all", lambda *a, **k: rows)
    docs = store.get_enabled_docs()
    assert [d.id for d in docs] == ["a"]          # disabled 被过滤
    assert docs[0].applies_to_stacks == ("python",) and docs[0].summary == "da"


def test_merged_skills_db_overrides_and_adds(monkeypatch):
    import swarm.config.skill_store as store_mod

    def fake_enabled():
        return [
            SkillDoc(id="coding-standards-core", title="DB 覆盖版", body="db body",
                     target=("worker",)),                       # 覆盖同 id 内置种子
            SkillDoc(id="user-custom-skill", title="用户自建", body="x", target=("worker",)),
        ]
    monkeypatch.setattr(store_mod, "get_enabled_docs", fake_enabled)
    svc.invalidate_cache()
    merged = svc._merged_skills(["skills_library"])
    by_id = {d.id: d for d in merged}
    assert by_id["coding-standards-core"].title == "DB 覆盖版"   # DB 优先
    assert "user-custom-skill" in by_id                          # DB-only 新增
    assert "api-design" in by_id                                 # 内置种子仍在


def test_merged_skills_db_error_falls_back_to_seeds(monkeypatch):
    import swarm.config.skill_store as store_mod

    def boom():
        raise RuntimeError("no DB")
    monkeypatch.setattr(store_mod, "get_enabled_docs", boom)
    svc.invalidate_cache()
    merged = svc._merged_skills(["skills_library"])
    assert len(merged) >= 40 and "api-design" in {d.id for d in merged}  # 纯内置种子
