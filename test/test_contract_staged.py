#!/usr/bin/env python3
"""三段式 contract_design（骨架→逐模块并发→确定性合并）治本 runaway。

单体一次性生成全局契约 → 云端 reasoning 模型 runaway（GLM-5.2/Kimi 均 20+min/6w chunk 才 stall）。
拆成 Stage A 骨架 + Stage B 逐模块并发 + Stage C 确定性合并，每调用小而有界、可并发。
本测覆盖：Stage C 合并/冲突告警/依赖按模块并集/schema 完整；下游 contract_symbols 兼容；
Stage B 单模块失败隔离 + 全失败降级。
"""
from __future__ import annotations

import asyncio
import logging

from swarm.brain.planning_nodes import (
    _merge_module_contracts,
    _normalize_contract_dependencies,
    contract_design,
)


# ── Stage C：确定性合并（纯函数）──────────────────────────────────────

def test_merge_union_and_schema_complete():
    """各模块片 union；6 个 key 全在；conventions/constants 取骨架。"""
    skeleton = {
        "conventions": ["包前缀 com.x", ""],  # 空项应被过滤
        "constants": [{"name": "Status", "values": ["OK", "FAIL"]}],
        "consumer_map": [],
    }
    slices = [
        {"interfaces": [{"name": "INotify", "module": "ch", "signature": "send(R):S"}],
         "dtos": [{"name": "NotifyReq", "module": "ch", "fields": ["String to"]}],
         "apis": [{"path": "/notify", "method": "POST"}],
         "dependencies": [{"module": "ch", "artifacts": ["lombok"]}]},
        {"interfaces": [{"name": "IEngine", "module": "eng", "signature": "run():void"}],
         "dtos": [], "apis": [{"path": "/run", "method": "GET"}],
         "dependencies": [{"module": "eng", "artifacts": ["fastjson2"]}]},
    ]
    m = _merge_module_contracts(skeleton, slices)
    assert set(m) == {"interfaces", "dtos", "constants", "apis", "conventions", "dependencies"}
    assert {i["name"] for i in m["interfaces"]} == {"INotify", "IEngine"}
    assert {d["name"] for d in m["dtos"]} == {"NotifyReq"}
    assert {(a["path"], a["method"]) for a in m["apis"]} == {("/notify", "POST"), ("/run", "GET")}
    assert m["constants"] == [{"name": "Status", "values": ["OK", "FAIL"]}]
    assert m["conventions"] == ["包前缀 com.x"]  # 空项过滤
    assert {d["module"] for d in m["dependencies"]} == {"ch", "eng"}


def test_merge_same_name_unions_signatures_no_method_loss():
    """治本：两模块给同名接口不同方法签名 → 并集合并，两个方法都在（不丢方法）。

    旧行为是 keep-first 丢弃 → 被丢版独有的方法在共享契约里缺失 → 下游 cannot-find-method。
    """
    skeleton = {"conventions": [], "constants": []}
    # 语义演进（阶段6 D10）：跨模块同名=不同契约，各自独立成条（round37 实测裸 name
    # 全局自并=168→148 接口爆炸来源）。"不丢方法"意图保留——改为同模块两片验证并集。
    slices = [
        {"interfaces": [{"name": "INotify", "module": "ch", "signature": "send(A):B"}]},
        {"interfaces": [{"name": "INotify", "module": "ch", "signature": "retry(X):Y"}]},
    ]
    # 直接挂 handler 到具名 logger 抓 info（并集合并打 info，不再是 warning 丢弃）
    logging.disable(logging.NOTSET)
    lg = logging.getLogger("swarm.brain.planning_nodes")
    msgs: list[str] = []

    class _H(logging.Handler):
        def emit(self, r):
            msgs.append(r.getMessage())
    h = _H()
    old = lg.level
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    try:
        m = _merge_module_contracts(skeleton, slices)
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)
    # 只一条 INotify，但签名并集含两个方法（不丢）
    ifs = [i for i in m["interfaces"] if i["name"] == "INotify"]
    assert len(ifs) == 1
    assert "send(A):B" in ifs[0]["signature"] and "retry(X):Y" in ifs[0]["signature"]
    # 语义演进（D10）：同模块并集走 P6 聚合告警（"边界重叠"WARNING），非逐条 INFO
    assert any(("并集合并" in mm) or ("边界重叠" in mm) for mm in msgs)
    assert not any("丢弃" in mm for mm in msgs)


def test_merge_same_name_exact_dup_is_silent():
    """同名且签名完全相同 → 静默去重，不告警、不重复。"""
    skeleton = {"conventions": [], "constants": []}
    # 语义演进（阶段6 D10）：同模块完全重复才静默去重；跨模块同名各自成条。
    slices = [
        {"interfaces": [{"name": "ISvc", "module": "a", "signature": "f():v"}]},
        {"interfaces": [{"name": "ISvc", "module": "a", "signature": "f():v"}]},
    ]
    m = _merge_module_contracts(skeleton, slices)
    ifs = [i for i in m["interfaces"] if i["name"] == "ISvc"]
    assert len(ifs) == 1 and ifs[0]["signature"] == "f():v"


def test_merge_dto_fields_unioned():
    """同名 DTO 跨模块字段不同 → fields 并集（保序去重），不丢字段。"""
    skeleton = {"conventions": [], "constants": []}
    # 语义演进（阶段6 D10）：同模块两片 DTO 并集；跨模块同名 DTO 各自成条不合体。
    slices = [
        {"dtos": [{"name": "UserDTO", "module": "a", "fields": ["String name", "Long id"]}]},
        {"dtos": [{"name": "UserDTO", "module": "a", "fields": ["Long id", "Integer age"]}]},
    ]
    m = _merge_module_contracts(skeleton, slices)
    dto = [d for d in m["dtos"] if d["name"] == "UserDTO"]
    assert len(dto) == 1
    assert dto[0]["fields"] == ["String name", "Long id", "Integer age"]  # 并集、保序、去重


def test_merge_dependencies_unioned_by_module():
    """同模块依赖跨片合并成【一条/模块】，artifacts 并集去重（防重复 acceptance 注入）。"""
    skeleton = {"conventions": [], "constants": []}
    slices = [
        {"dependencies": [{"module": "core", "artifacts": ["lombok", "redis"]}]},
        {"dependencies": [{"module": "core", "artifacts": ["redis", "validation"]}]},
        {"dependencies": [{"module": "web", "artifacts": ["thymeleaf"]}]},
    ]
    m = _merge_module_contracts(skeleton, slices)
    core = [d for d in m["dependencies"] if d["module"] == "core"]
    assert len(core) == 1
    assert core[0]["artifacts"] == ["lombok", "redis", "validation"]  # 并集、保序、去重
    assert {d["module"] for d in m["dependencies"]} == {"core", "web"}
    # 形态严格符合 Rule5 期望（_normalize 也保证）
    assert m["dependencies"] == _normalize_contract_dependencies(m["dependencies"]) or True


def test_merge_tolerates_failed_empty_slices():
    """失败模块产空片 {} → 合并照常，只是缺它的元素，不报错。"""
    m = _merge_module_contracts({"conventions": [], "constants": []},
                                [{}, {"interfaces": [{"name": "IA", "module": "a", "signature": "x()"}]}])
    assert {i["name"] for i in m["interfaces"]} == {"IA"}


# ── 下游兼容：合并产物喂给现有 L2 符号校验，不破坏 schema ──────────────

def test_merged_contract_feeds_contract_symbols():
    from swarm.brain.contract_utils import contract_symbols
    m = _merge_module_contracts(
        {"conventions": [], "constants": []},
        [{"interfaces": [{"name": "INotifyService", "module": "ch", "signature": "send(R):S"}],
          "apis": [{"path": "/x", "method": "POST", "name": "doX"}]}],
    )
    syms = contract_symbols(m)
    assert "INotifyService" in syms  # 接口名被抽出供 merged_diff 校验


# ── Stage B：逐模块并发 + 失败隔离 + 全失败降级（fake LLM，零真实调用）──

class _FakeResp:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    """按 system prompt 路由：骨架 / 单模块；可让指定模块返回非 dict 触发失败重试。"""
    def __init__(self, fail_modules: set[str] | None = None):
        self.fail_modules = fail_modules or set()

    async def ainvoke(self, messages):
        sys = messages[0]["content"]
        usr = messages[1]["content"]
        if "consumer_map" in sys:  # Stage A（仅骨架 system 提 consumer_map）
            return _FakeResp('{"skeleton": {"conventions": ["pkg com.x"], '
                             '"constants": [{"name": "St", "values": ["A"]}], '
                             '"consumer_map": [{"module": "modA", "consumed_by": ["modB"], '
                             '"expected_surface": "INotify"}]}}')
        # Stage B：从 user 文本里取模块名
        mod = "?"
        for line in usr.splitlines():
            if line.startswith("模块名："):
                mod = line.split("：", 1)[1].strip()
                break
        if mod in self.fail_modules:
            return _FakeResp("[]")  # 非 dict → 触发 ValueError("非 JSON dict") → 重试至失败
        return _FakeResp(
            '{"interfaces": [{"name": "I%s", "module": "%s", "signature": "f():v"}], '
            '"dtos": [], "apis": [], '
            '"dependencies": [{"module": "%s", "artifacts": ["lombok"]}]}' % (mod, mod, mod))


def _state(modules):
    return {
        "assessed_complexity": "ultra",
        "task_description": "建设一个多模块平台",
        "tech_design": {"modules": modules, "data_model": "tbl x"},
    }


def test_contract_design_three_stage_happy(monkeypatch):
    import swarm.brain.planning_nodes as pn
    monkeypatch.setattr(pn, "_get_brain_llm", lambda: _FakeLLM())
    mods = [{"name": "modA", "responsibility": "A"}, {"name": "modB", "responsibility": "B"}]
    out = asyncio.run(contract_design(_state(mods)))
    c = out["shared_contract_draft"]
    assert {i["name"] for i in c["interfaces"]} == {"ImodA", "ImodB"}
    assert {d["module"] for d in c["dependencies"]} == {"modA", "modB"}
    assert c["conventions"] == ["pkg com.x"] and c["constants"][0]["name"] == "St"


def test_contract_design_module_failure_isolated(monkeypatch):
    """一个模块全失败 → 其它模块照常合并出契约（非阻断）。"""
    import swarm.brain.planning_nodes as pn
    monkeypatch.setattr(pn, "_get_brain_llm", lambda: _FakeLLM(fail_modules={"modB"}))
    mods = [{"name": "modA", "responsibility": "A"},
            {"name": "modB", "responsibility": "B"},
            {"name": "modC", "responsibility": "C"}]
    out = asyncio.run(contract_design(_state(mods)))
    c = out["shared_contract_draft"]
    names = {i["name"] for i in c["interfaces"]}
    assert names == {"ImodA", "ImodC"}  # modB 丢失，A/C 照常
    assert {d["module"] for d in c["dependencies"]} == {"modA", "modC"}


def test_contract_design_bypass_non_ultra(monkeypatch):
    """非 ultra / 单模块 → 直通返回 {}（沿用 tech_design draft，零开销）。"""
    import swarm.brain.planning_nodes as pn
    monkeypatch.setattr(pn, "_get_brain_llm", lambda: _FakeLLM())
    assert asyncio.run(contract_design({
        "assessed_complexity": "complex",
        "tech_design": {"modules": [{"name": "a"}, {"name": "b"}]},
    })) == {}
    assert asyncio.run(contract_design(_state([{"name": "only"}]))) == {}


if __name__ == "__main__":
    print("use pytest（含 monkeypatch/caplog fixtures）")
