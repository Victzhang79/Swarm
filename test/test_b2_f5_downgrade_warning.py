#!/usr/bin/env python3
"""30 号文批15 B-2+F-5 锁：降级/失败路径留痕 debug→WARNING 的行为级抽验。

登记册处方=级别提升（10 处：dispatch×5 / merge_engine / runner×2 / verify / selector），
明确否决「全仓扫 logger.debug 的结构守卫」（违纪律 6，量小人检+登记）。
本文件只对【两个可廉价触发】的降级路径补行为锁（WARNING 级断言）：
- merge_engine：git merge-file 不可用 → 降级 python merge3（行级三路，语义不同）必须 WARNING；
- selector：G3 候选截断必须 WARNING（原注释自称「截断必须可观测」而 debug 在
  生产 INFO 下不可见=零留痕，注释与实现自相矛盾族）。
其余 8 处（dispatch 滚动循环深位/runner 轮询点/verify 续期）按登记册=人检+登记，
不造重型夹具（dispatch 滚动循环夹具成本远超级别改动风险）。
"""
from __future__ import annotations

import logging
import subprocess

import swarm.brain.merge_engine as me
from swarm.experience.models import SkillDoc
from swarm.experience.selector import select_skills


def _skill(sid: str, prio: int = 50) -> SkillDoc:
    return SkillDoc(id=sid, title=sid, body=f"body-{sid}", priority=prio)


def test_git_merge_file_unavailable_warns_and_falls_back(monkeypatch, caplog):
    """B-2 主实例 merge_engine:583：git merge-file 不可用/超时 → 降级 python merge3
    必须 WARNING 留痕（不同语义的合并结果绝不无痕），且真落到 python 合并产物。"""

    def _boom(*a, **k):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(me.subprocess, "run", _boom)
    with caplog.at_level(logging.WARNING):
        merged, clean = me.three_way_merge_text("base\n", "ours\n", "theirs\n")
    hits = [r for r in caplog.records
            if r.levelno >= logging.WARNING and "merge-file" in r.getMessage()]
    assert hits, "git merge-file 不可用降级 python merge3 必须 WARNING（生产 INFO 可见）"
    assert isinstance(merged, str) and merged, "降级仍须产出合并结果（python merge3）"


def test_git_merge_file_timeout_also_warns(monkeypatch, caplog):
    """B-2 finding 点名的失败场景：大文件撞 10s timeout → 降级同样 WARNING。"""

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=10)

    monkeypatch.setattr(me.subprocess, "run", _timeout)
    with caplog.at_level(logging.WARNING):
        me.three_way_merge_text("b\n", "o\n", "t\n")
    assert any(r.levelno >= logging.WARNING and "merge-file" in r.getMessage()
               for r in caplog.records), "merge-file 超时降级必须 WARNING 留痕"


def test_selector_truncation_logged_info_with_dropped_ids(caplog):
    """F-5：候选截断必须【生产可见】（INFO 起步）且带 dropped 名单——「配了但从未生效」
    与「没配」在生产日志上必须可分。★hunter HIGH 纠正★：截断是常态裁剪（49 技能 vs
    max_k=3~9），WARNING=噪声洪泛淹真信号（噪声即静默）——INFO 既可见又不成洪。
    断言同时钉 logger name（防别模块同词日志污染出假绿，hunter LOW#5）。"""
    skills = [_skill(f"sk-{i}", prio=i) for i in range(5)]
    with caplog.at_level(logging.INFO):
        picked = select_skills(skills, stack_langs=set(), intent="", phase="",
                               target="worker", budget_chars=100_000, max_k=2)
    assert len(picked) == 2
    hits = [r for r in caplog.records
            if r.name == "swarm.experience.selector"
            and r.levelno >= logging.INFO and "截断" in r.getMessage()]
    assert hits, "G3 截断必须 INFO+ 留痕（debug 在生产 INFO 下不可见=零留痕）"
    msg = hits[0].getMessage()
    assert "dropped" in msg, "截留痕必须带 dropped 名单（哪几条被截掉机读可辨）"
    # hunter HIGH 反向半：常态截断不得 WARNING（噪声洪泛方向也锁死）
    assert not [r for r in caplog.records
                if r.name == "swarm.experience.selector"
                and r.levelno >= logging.WARNING and "截断" in r.getMessage()], \
        "常态截断不得 WARNING（49 技能×max_k=3~9 下每任务都触发=洪泛）"


def test_selector_no_truncation_no_log(caplog):
    """反向锁：候选不超 max_k 时零截断日志（闸不得误伤正常路径）。"""
    skills = [_skill(f"sk-{i}") for i in range(2)]
    with caplog.at_level(logging.DEBUG):
        picked = select_skills(skills, stack_langs=set(), intent="", phase="",
                               target="worker", budget_chars=100_000, max_k=5)
    assert len(picked) == 2
    assert not [r for r in caplog.records
                if r.name == "swarm.experience.selector" and "截断" in r.getMessage()]
