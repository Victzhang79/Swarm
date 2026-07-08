"""R33 收尾治本批（E2E_ROUND33_REGISTER.md）：U3 bisect-on-timeout + R32-4 抽取容量。

U3（R33-1）：内容特异性确定性慢批（alarm-core#1/3 三轮 6 次超时无一成功）——批超时
   耗尽重试后【对半切分重试】，有界两轮（最小 1/4 批），半批独立记账/入缓存/保序。
R32-4（用户拍板 2026-07-08）：MAX_ITEMS 上限 env 可调（SWARM_EXTRACT_MAX_ITEMS 默认
   100，保留抽取失控熔断语义）+ 截断按 kind 优先级（functional/api/data > page/other）
   而非到达序——真实 PRD 三轮实测 74/96/88 条合格 vs 旧上限 60，防失控阀切的是真需求。
"""

from __future__ import annotations

import json

from swarm.brain.nodes import _plan_ultra_batched
from swarm.brain.requirements_extract import validate_requirement_items


def _fp(path, resp="r"):
    return {"path": path, "action": "create", "responsibility": resp}


def _payload(tag, n=1):
    return json.dumps({"subtasks": [
        {"id": f"st-{tag}-{i}", "description": f"{tag} 工作 {i}",
         "scope": {"writable": [f"{tag}/f{i}"], "readable": []}}
        for i in range(n)
    ]})


def _state(extra=None):
    return {"tech_design": {}, "shared_contract_draft": {}, "project_id": "",
            **(extra or {})}


class _RouteLLM:
    """按 prompt 中 `模块 'name'` 路由；timeout_mods 中的名字 sleep 超时。"""

    def __init__(self, payloads: dict, timeout_mods=()):
        self.payloads = payloads
        self.timeout_mods = set(timeout_mods)
        self.calls: dict[str, int] = {}

    async def ainvoke(self, messages):
        prompt = messages[-1]["content"]
        for mod, payload in self.payloads.items():
            if f"模块 '{mod}'" in prompt:
                self.calls[mod] = self.calls.get(mod, 0) + 1
                if mod in self.timeout_mods:
                    import asyncio
                    await asyncio.sleep(9)
                return type("R", (), {"content": payload})()
        raise AssertionError(f"prompt 未命中已知模块: {prompt[:200]}")


async def _run(llm, state, file_plan, monkeypatch):
    monkeypatch.setenv("SWARM_PLAN_BATCH_TIMEOUT", "2")
    monkeypatch.setenv("SWARM_PLAN_BATCH_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("SWARM_PLAN_BATCH_MAX_FILES", "20")
    # R35-A：本组测 bisect/timeout 记账，与切备正交——禁用切备（否则超时后切真 Kimi
    # 端点报错→归类 error 非 timeout，掩盖 bisect 路径）。切备本身另见 test_llm_abortable_failover_r35a。
    import swarm.brain.nodes as _nodes
    monkeypatch.setattr(_nodes, "_get_brain_fallback_llm", lambda: None)
    return await _plan_ultra_batched(llm, state, "需求", {}, "", file_plan)


# ─────────────── U3: bisect-on-timeout ───────────────

async def test_timeout_batch_bisected_and_recovers(monkeypatch):
    """整批超时 → 对半切分重试 → 两半各自成功 → plan 完整、零失败记账。"""
    files = [_fp(f"slow/f{i}.txt") for i in range(4)]
    llm = _RouteLLM({"slow": _payload("whole"),
                     "slow~a": _payload("ha", 2), "slow~b": _payload("hb", 1)},
                    timeout_mods={"slow"})
    plan, failed, _bl, cache = await _run(llm, _state(), files, monkeypatch)
    assert failed == [], f"半批成功后不得留失败记账: {failed}"
    assert len(plan.subtasks) == 3
    assert llm.calls.get("slow") == 1 and llm.calls.get("slow~a") == 1
    assert any(v.get("module") == "slow~a" for v in cache.values()), "半批独立入缓存"


async def test_bisect_partial_failure_accounts_only_failed_half(monkeypatch):
    """一半成功一半仍超时 → 只有失败半批进记账，成功半批产出保留。"""
    files = [_fp(f"slow/f{i}.txt") for i in range(4)]
    llm = _RouteLLM({"slow": _payload("w"), "slow~a": _payload("ha", 2),
                     "slow~b": _payload("hb"),
                     # 第二轮 bisect：~b 只剩 2 文件再切
                     "slow~b~a": _payload("hba"), "slow~b~b": _payload("hbb")},
                    timeout_mods={"slow", "slow~b", "slow~b~b"})
    plan, failed, _bl, _c = await _run(llm, _state(), files, monkeypatch)
    # ~a 成功(2子任务)；~b 超时→二轮切分：~b~a 成功，~b~b 仍超时（1 文件不再切）
    assert [m["name"] for m in failed] == ["slow~b~b"]
    ids = {st.id for st in plan.subtasks}
    assert len(ids) == 3  # ha×2 + hba×1（merge 全局重编号后唯一）


async def test_bisect_bounded_single_file_not_split(monkeypatch):
    """1 文件批超时 → 不可再切，正常记账 timeout（递归有界）。"""
    files = [_fp("tiny/only.txt")]
    llm = _RouteLLM({"tiny": _payload("t")}, timeout_mods={"tiny"})
    import pytest
    with pytest.raises(RuntimeError):
        # 唯一批失败 → 既有"全部批失败"RuntimeError 语义不变
        await _run(llm, _state(), files, monkeypatch)


async def test_bisect_halves_no_duplicate_scaffold(monkeypatch):
    """新模块首子批被 bisect 后，scaffold 提示只进 ~a 半批。"""
    files = [_fp(f"newmod/f{i}.txt") for i in range(4)]
    captured: dict = {}

    class _Cap:
        async def ainvoke(self, messages):
            p = messages[-1]["content"]
            for mod in ("newmod", "newmod~a", "newmod~b"):
                if f"模块 '{mod}'" in p:
                    captured[mod] = p
                    if mod == "newmod":
                        import asyncio
                        await asyncio.sleep(9)
                    return type("R", (), {"content": _payload(mod.replace("~", "_"))})()
            raise AssertionError("no match")

    _p, failed, _b, _c = await _run(_Cap(), _state(), files, monkeypatch)
    assert failed == []
    assert "脚手架" in captured["newmod~a"]
    assert "脚手架" not in captured["newmod~b"], "半批 ~b 不得重复触发脚手架"


# ─────────────── R32-4: 抽取容量 env + kind 优先级截断 ───────────────

_SRC = "系统需要功能甲。系统需要页面乙。系统需要接口丙。系统需要数据丁。系统需要其他戊。"


def _raw(text, kind, quote):
    return {"text": text, "kind": kind, "source_quote": quote}


def test_max_items_default_100(monkeypatch):
    monkeypatch.delenv("SWARM_EXTRACT_MAX_ITEMS", raising=False)
    raw = [_raw(f"功能条目{i}", "functional", "系统需要功能甲") for i in range(120)]
    items, rejected = validate_requirement_items(raw, _SRC)
    assert len(items) == 100, "默认上限 100（三轮实测 74/96/88 条合格，60 切真需求）"
    assert sum(1 for r in rejected if r["reason"] == "over_limit") == 20


def test_max_items_env_override(monkeypatch):
    monkeypatch.setenv("SWARM_EXTRACT_MAX_ITEMS", "5")
    raw = [_raw(f"功能条目{i}", "functional", "系统需要功能甲") for i in range(8)]
    items, rejected = validate_requirement_items(raw, _SRC)
    assert len(items) == 5
    monkeypatch.setenv("SWARM_EXTRACT_MAX_ITEMS", "abc")  # 非法值→回退默认不炸
    items2, _ = validate_requirement_items(raw, _SRC)
    assert len(items2) == 8


def test_over_limit_drops_low_priority_kinds_first(monkeypatch):
    """截断按 kind 优先级：functional/api/data 优先收留，page/other 先被截。"""
    monkeypatch.setenv("SWARM_EXTRACT_MAX_ITEMS", "3")
    raw = [
        _raw("页面乙条目", "page", "系统需要页面乙"),
        _raw("功能甲条目", "functional", "系统需要功能甲"),
        _raw("其他戊条目", "other", "系统需要其他戊"),
        _raw("接口丙条目", "api", "系统需要接口丙"),
        _raw("数据丁条目", "data", "系统需要数据丁"),
    ]
    items, rejected = validate_requirement_items(raw, _SRC)
    kinds = [i["kind"] for i in items]
    assert sorted(kinds) == ["api", "data", "functional"], f"高优先 kind 必须收留: {kinds}"
    dropped = {r["text_head"] for r in rejected if r["reason"] == "over_limit"}
    assert dropped == {"页面乙条目", "其他戊条目"}


def test_within_limit_preserves_arrival_order(monkeypatch):
    """未超限时零行为变化：输出保持到达序（ID 是内容 hash 与序无关，但可读性保序）。"""
    monkeypatch.delenv("SWARM_EXTRACT_MAX_ITEMS", raising=False)
    raw = [
        _raw("页面乙条目", "page", "系统需要页面乙"),
        _raw("功能甲条目", "functional", "系统需要功能甲"),
    ]
    items, rejected = validate_requirement_items(raw, _SRC)
    assert [i["text"] for i in items] == ["页面乙条目", "功能甲条目"]
    assert rejected == []


async def test_bisect_valve_off_restores_whole_batch_accounting(monkeypatch):
    """泄压阀：SWARM_PLAN_BATCH_BISECT=0 → 回退整批 timeout 记账旧行为。"""
    monkeypatch.setenv("SWARM_PLAN_BATCH_BISECT", "0")
    files = [_fp(f"slow/f{i}.txt") for i in range(4)] + [_fp("ok/ok_main.txt")]
    llm = _RouteLLM({"slow": _payload("w"), "ok": _payload("ok")},
                    timeout_mods={"slow"})
    _p, failed, _b, _c = await _run(llm, _state(), files, monkeypatch)
    assert [m["name"] for m in failed] == ["slow"] and failed[0]["files"] == 4
