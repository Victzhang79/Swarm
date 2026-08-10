#!/usr/bin/env python3
"""R40-3（round40 治本批）—— worker 经验注入 pull→push 挪移。

取证（round39/40 两轮 tool-telemetry）：experience__ pull 工具调用恒 0（round40
36 条遥测零命中；query_knowledge_base 正常用）→ 小模型不接"可选离散工具"是用法
问题非坏。按 runbook §5d 拍板落地：
  - push 从 top-1 扩到 top-K（SWARM_SKILLS_WORKER_PUSH_K，默认 2；E9-3 栈特化+
    框架相关门槛逐条保留，通配仍不 push）；
  - pull 工具默认关（SWARM_SKILLS_WORKER_PULL_ENABLED=0，实证死重量还占 worker
    工具槽位）；开关回退旧行为；
  - E9-5 承诺不变：worker_max_tools=0 = worker 侧经验全关。
select_worker_push_pull 返回形状 (push_list, pull_list)。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.experience.models import SkillDoc  # noqa: E402


def _skill(sid, stacks=("*",), priority=50, body=None):
    return SkillDoc(id=sid, title=sid,
                    body=body or (f"BODY-{sid.upper()} guidance " * 10),
                    priority=priority, applies_to_stacks=tuple(stacks),
                    target=("worker",), summary=f"{sid} summary")


class _Sub:
    id = "st-1"
    intent = "create"


_JAVA_STACK = {"frontend": "", "backend": "Spring Boot (java)", "build": "maven"}


def test_default_pull_disabled_no_tools(monkeypatch):
    import swarm.experience.service as svc
    monkeypatch.setattr(svc, "_merged_skills", lambda dirs: [
        _skill("java-coding-standards", stacks=("java",), priority=60),
        _skill("java-testing", stacks=("java",), priority=55),
        _skill("error-handling", priority=50),
    ])
    from swarm.experience.service import build_worker_experience_tools
    assert build_worker_experience_tools(_Sub(), _JAVA_STACK) == [], (
        "pull 默认关（两轮实证调用恒 0，纯占 worker 工具槽）")


def test_push_topk_two_fulltexts(monkeypatch):
    import swarm.experience.service as svc
    monkeypatch.setattr(svc, "_merged_skills", lambda dirs: [
        _skill("java-coding-standards", stacks=("java",), priority=60,
               body="UNIQ-STD-LINE"),
        _skill("java-testing", stacks=("java",), priority=55, body="UNIQ-TEST-LINE"),
        _skill("error-handling", priority=50, body="UNIQ-WILD-LINE"),
    ])
    pushes, pulls = svc.select_worker_push_pull(_Sub(), _JAVA_STACK)
    assert [s.id for s in pushes] == ["java-coding-standards", "java-testing"], (
        "push 扩到 top-K（默认 2），栈特化+相关门槛逐条过")
    assert pulls == [], "pull 默认关"
    block = svc.worker_skills_block(_Sub(), _JAVA_STACK)
    assert "UNIQ-STD-LINE" in block and "UNIQ-TEST-LINE" in block, "K 条全文都进 prompt"
    assert "experience__" not in block, "pull 关时不渲染工具目录（目录与工具一一对应）"


def test_wildcard_never_pushed(monkeypatch):
    import swarm.experience.service as svc
    monkeypatch.setattr(svc, "_merged_skills", lambda dirs: [
        _skill("java-coding-standards", stacks=("java",), priority=60),
        _skill("error-handling", priority=99),
    ])
    pushes, _ = svc.select_worker_push_pull(_Sub(), _JAVA_STACK)
    assert [s.id for s in pushes] == ["java-coding-standards"], (
        "通配技能泛化建议不值得无条件占 prefill（E9-3 门槛保留）")


def test_pull_optin_restores_tools(monkeypatch):
    import swarm.experience.service as svc
    from swarm.config.settings import get_config
    monkeypatch.setattr(get_config().skills, "worker_pull_enabled", True)
    monkeypatch.setattr(svc, "_merged_skills", lambda dirs: [
        _skill("java-coding-standards", stacks=("java",), priority=60),
        _skill("java-testing", stacks=("java",), priority=55),
        _skill("error-handling", priority=50),
        _skill("api-design", priority=45),
    ])
    pushes, pulls = svc.select_worker_push_pull(_Sub(), _JAVA_STACK)
    assert pushes and pulls, "开关回退：pull 侧恢复"
    assert not ({s.id for s in pushes} & {s.id for s in pulls}), "push/pull 不重叠"
    assert len(pulls) <= get_config().skills.worker_max_tools
    tools = svc.build_worker_experience_tools(_Sub(), _JAVA_STACK)
    assert tools, "pull 开时离散工具恢复注册"


def test_wmt_zero_still_all_off(monkeypatch):
    import swarm.experience.service as svc
    from swarm.config.settings import get_config
    monkeypatch.setattr(get_config().skills, "worker_max_tools", 0)
    monkeypatch.setattr(svc, "_merged_skills", lambda dirs: [
        _skill("java-coding-standards", stacks=("java",), priority=60)])
    pushes, pulls = svc.select_worker_push_pull(_Sub(), _JAVA_STACK)
    assert pushes == [] and pulls == [], (
        "E9-5 承诺不漂移：worker_max_tools=0 = worker 侧经验全关（含 push）")


def test_worker_push_telemetry_carries_subtask_digest(monkeypatch, caplog):
    """LOW 收口 #37：push 遥测补子任务词元+scope 文件摘要——push 判据标定需事后回放
    「当时这个子任务在写什么」，只记 subtask_id 无法复算判据输入面。接线锁：删掉
    logger.info 或 terms/files 任一字段，本条即红。"""
    import logging

    import swarm.experience.service as svc
    from swarm.types import FileScope

    class _SubD:
        id = "st-9"
        intent = "create"
        description = "实现 Alarm Mapper 持久层"
        scope = FileScope(
            writable=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/mapper/AlarmMapper.java"])

    monkeypatch.setattr(svc, "_merged_skills", lambda dirs: [
        _skill("java-coding-standards", stacks=("java",), priority=60)])
    with caplog.at_level(logging.INFO):
        block = svc.worker_skills_block(_SubD(), _JAVA_STACK)
    assert block, "前置：确有 push 发生才有遥测行"
    line = next((r.getMessage() for r in caplog.records
                 if "skills-telemetry" in r.getMessage()
                 and "worker_push" in r.getMessage()), "")
    assert line, "push 遥测行必须在（删掉 logger.info 本条即红）"
    assert "st-9" in line and "java-coding-standards" in line
    assert "terms=" in line and "mapper" in line, \
        f"子任务词元（_pushable 判据输入面）必须进遥测: {line}"
    assert "files=" in line and "AlarmMapper.java" in line, \
        f"scope 文件摘要必须进遥测: {line}"


def test_worker_push_telemetry_failure_never_kills_block(monkeypatch, caplog):
    """★复核 R1（hunter）★遥测组装异常 → WARNING+跳过遥测，技能块【照常渲染】——
    pushes 已算好，异常形状 scope 之类绝不能连累整块经验丢失（治前靠外层 try
    兜底=整块静默降级为空）。"""
    import logging

    import swarm.experience.service as svc
    monkeypatch.setattr(svc, "_merged_skills", lambda dirs: [
        _skill("java-coding-standards", stacks=("java",), priority=60)])
    def _boom(*a, **k):
        raise RuntimeError("log pipe broken")
    monkeypatch.setattr(svc.logger, "info", _boom)   # 遥测行自身炸
    with caplog.at_level(logging.WARNING):
        block = svc.worker_skills_block(_Sub(), _JAVA_STACK)
    assert block, "遥测异常绝不拖垮技能块本体"
    assert any("遥测组装失败" in r.getMessage() for r in caplog.records), \
        "遥测降级必须 WARNING 可观测（铁律#3）"
