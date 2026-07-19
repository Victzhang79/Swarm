"""R65E7-L2 上游根治（action 侧）：_ensure_file_plan_covers_requirements 定向补排文件。

漏排需求（有判别 token、file_plan 无落点、基线亦无）→ 定向反馈设计 LLM 补排文件 → 合并进 file_plan
→ plan_batch 自然为其建子任务。fail-open 全程（泄压阀关/无需求/无 unplanned/LLM 失败/无有效产出→
原样返回不阻断，留 L1 兜底+覆盖闸）。栈感知 system prompt 禁栈外技术。
"""
from __future__ import annotations

import json

import pytest

from swarm.brain.baseline_candidates import build_baseline_vocab
from swarm.brain.nodes import (
    _ensure_file_plan_covers_requirements,
    _merge_designed_file_plan,
)

REQ_2FA = "req-4067d7fb"


class _Resp:
    def __init__(self, content):
        self.content = content


class _LLM:
    def __init__(self, content):
        self._c = content
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        return _Resp(self._c)


class _BoomLLM:
    async def ainvoke(self, messages):
        raise RuntimeError("llm down")


def _items():
    return [{"id": REQ_2FA, "text": "支持Google 2FA双因素认证，包含绑定/解绑/验证。"}]


def _file_plan():
    # 多模块（如真 round65e7）：alarm + admin 均在 file_plan → 2FA 落 ruoyi-admin 属既有模块
    return [{"path": "ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/AlarmTask.java",
             "module": "ruoyi-alarm", "responsibility": "预警任务实体"},
            {"path": "ruoyi-admin/src/main/java/com/ruoyi/web/controller/SysUserController.java",
             "module": "ruoyi-admin", "responsibility": "用户管理控制器"}]


def _vocab_no_2fa():
    return build_baseline_vocab(
        [{"file_path": "ruoyi-common/utils/poi/ExcelUtil.java", "module_name": "ruoyi-common"}],
        [{"file_path": "ruoyi-common/utils/poi/ExcelUtil.java",
          "symbol_name": "importExcel", "class_name": "ExcelUtil"}])


def _patch_vocab(monkeypatch, vocab):
    async def _fake(_state):
        return vocab
    monkeypatch.setattr("swarm.brain.nodes._baseline_vocab_for", _fake)


# ── _merge_designed_file_plan（确定性核） ──
def test_merge_drops_invalid_and_dupes():
    fp = _file_plan()
    new = [
        {"path": "ruoyi-admin/.../TwoFactorController.java", "module": "ruoyi-admin", "responsibility": "2FA"},
        {"path": "", "module": "x", "responsibility": "缺路径"},              # 越权：无 path
        {"path": "x.java", "module": "", "responsibility": "缺模块"},          # 越权：无 module
        {"path": "ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/AlarmTask.java",  # 重复既有
         "module": "ruoyi-alarm", "responsibility": "dup"},
    ]
    merged, added, dropped = _merge_designed_file_plan(fp, new)
    assert added == 1, f"只应并入 1 个有效新文件；added={added}"
    assert dropped == 3, f"3 个越权/重复条目应被剔；dropped={dropped}"
    assert len(merged) == len(fp) + 1
    assert any("TwoFactorController" in e["path"] for e in merged)
    assert merged[-1].get("action") == "create", "新条目应带 action=create"


def test_merge_drops_phantom_module():
    """★复核 MED 锁★ allowed_modules 给定时，LLM 臆造的新模块名条目被剔（防 phantom 模块 coherence 违例）。"""
    fp = _file_plan()
    new = [
        {"path": "ruoyi-admin/.../TwoFactorController.java", "module": "ruoyi-admin", "responsibility": "2FA"},
        {"path": "ruoyi-newphantom/.../X.java", "module": "ruoyi-newphantom", "responsibility": "臆造新模块"},
    ]
    merged, added, dropped = _merge_designed_file_plan(fp, new, allowed_modules={"ruoyi-alarm", "ruoyi-admin"})
    assert added == 1 and dropped == 1, f"臆造模块应被剔；added={added} dropped={dropped}"
    assert not any("phantom" in e["path"] for e in merged)


def test_merge_empty_new_noop():
    fp = _file_plan()
    merged, added, dropped = _merge_designed_file_plan(fp, [])
    assert added == 0 and dropped == 0 and merged == fp


# ── _ensure_file_plan_covers_requirements（编排） ──
@pytest.mark.asyncio
async def test_unplanned_2fa_gets_files(monkeypatch):
    """★RED 核★ 2FA 漏排 → 设计 LLM 补 TwoFactorController → file_plan 增广、augmented=True。"""
    _patch_vocab(monkeypatch, _vocab_no_2fa())
    llm = _LLM(json.dumps({"file_plan": [
        {"path": "ruoyi-admin/src/main/java/com/ruoyi/web/controller/TwoFactorController.java",
         "module": "ruoyi-admin", "responsibility": "Google 2FA 绑定/解绑/验证"}]}))
    state = {"requirement_items": _items(), "project_stack": "Java/Thymeleaf 服务端模板", "project_id": "p"}
    fp, aug = await _ensure_file_plan_covers_requirements(state, llm, _file_plan())
    assert aug is True, "2FA 漏排应触发补排"
    assert llm.calls == 1, "应恰调一次设计 LLM"
    assert any("TwoFactor" in e["path"] for e in fp), f"补排后 file_plan 应含 2FA 文件；fp={fp}"


@pytest.mark.asyncio
async def test_all_planned_fast_path_no_llm(monkeypatch):
    """全覆盖（2FA 已在 file_plan）→ 不调 LLM、augmented=False（快路径，绝大多数轮）。"""
    _patch_vocab(monkeypatch, _vocab_no_2fa())
    fp_with_2fa = _file_plan() + [{
        "path": "ruoyi-admin/.../TwoFactorController.java", "module": "ruoyi-admin",
        "responsibility": "Google 2FA 双因素认证"}]
    llm = _LLM("{}")
    state = {"requirement_items": _items(), "project_id": "p"}
    fp, aug = await _ensure_file_plan_covers_requirements(state, llm, fp_with_2fa)
    assert aug is False and llm.calls == 0, "全覆盖不该调 LLM"
    assert fp == fp_with_2fa


@pytest.mark.asyncio
async def test_env_off_skips(monkeypatch):
    monkeypatch.setenv("SWARM_PLAN_COVERAGE_DESIGN", "0")
    _patch_vocab(monkeypatch, _vocab_no_2fa())
    llm = _LLM("{}")
    fp, aug = await _ensure_file_plan_covers_requirements(
        {"requirement_items": _items(), "project_id": "p"}, llm, _file_plan())
    assert aug is False and llm.calls == 0, "泄压阀关时不触发"


@pytest.mark.asyncio
async def test_llm_failure_fail_open(monkeypatch):
    """设计 LLM 挂（无备用）→ fail-open，原样返回 augmented=False，不阻断规划。"""
    _patch_vocab(monkeypatch, _vocab_no_2fa())
    fp0 = _file_plan()
    fp, aug = await _ensure_file_plan_covers_requirements(
        {"requirement_items": _items(), "project_id": "p"}, _BoomLLM(), fp0)
    assert aug is False and fp == fp0, "LLM 失败应 fail-open 不改 file_plan"


@pytest.mark.asyncio
async def test_llm_empty_output_no_files(monkeypatch):
    """LLM 返回空/无有效文件 → augmented=False（不产出就不动 file_plan）。"""
    _patch_vocab(monkeypatch, _vocab_no_2fa())
    llm = _LLM(json.dumps({"file_plan": [{"path": "", "module": ""}]}))
    fp0 = _file_plan()
    fp, aug = await _ensure_file_plan_covers_requirements(
        {"requirement_items": _items(), "project_id": "p"}, llm, fp0)
    assert aug is False and fp == fp0


@pytest.mark.asyncio
async def test_empty_file_plan_simple_path_skips(monkeypatch):
    """简单/中等路径无 tech_design → file_plan 空 → 跳过（不无中生有补排）。"""
    _patch_vocab(monkeypatch, _vocab_no_2fa())
    llm = _LLM("{}")
    fp, aug = await _ensure_file_plan_covers_requirements(
        {"requirement_items": _items(), "project_id": "p"}, llm, [])
    assert aug is False and llm.calls == 0 and fp == []


@pytest.mark.asyncio
async def test_empty_baseline_vocab_fail_open(monkeypatch):
    """baseline_vocab 空（KB 不可达）→ requirements_missing_from_plan 全豁免 → 不调 LLM。"""
    _patch_vocab(monkeypatch, "")
    llm = _LLM("{}")
    fp0 = _file_plan()
    fp, aug = await _ensure_file_plan_covers_requirements(
        {"requirement_items": _items(), "project_id": "p"}, llm, fp0)
    assert aug is False and llm.calls == 0 and fp == fp0
