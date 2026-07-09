"""阶段3.6 F8+A10②（登记册 §六/§二）：批候选需求子集注入 + 横切条目全批可认领。

F8 病理：ULTRA 每批全量注入 500 条需求清单（~11万字符/批）×12 批×重试轮=prompt
双线性膨胀（饱和推手）。治本：确定性预分桶——条目只注入 affinity>0 的模块批；
横切/不可路由条目（0 affinity）注入【所有】批并明示"若本批子任务天然承担请声明
covers"（A10② 横切需求任务级认领出口）。不变量：任一条目至少出现在一个批。

A10① 取证更正：plan_batch_failed 禁 topup 的死角第一腿已被阶段0 A4（validate 检出
失败模块打回 PLAN 走 U2 缓存补齐重试）解除——部分启用 topup 反而白烧重试轮
（topup 修不了缺模块）。真缺口=横切认领，本批治。
"""

from __future__ import annotations

import json

from swarm.brain.plan_batch import bucket_requirement_items

_BATCHES = [
    ("alarm-sdk", [{"path": "alarm-sdk/src/AlarmService.java"}]),
    ("用户中心", [{"path": "user-center/src/UserController.java"}]),
]


def _items():
    return [
        {"id": "req-a1", "text": "alarm-sdk 模块提供告警发送 SDK 能力"},      # → alarm-sdk
        {"id": "req-b1", "text": "用户中心支持按部门筛选用户列表"},            # → 用户中心（中文名）
        {"id": "req-c1", "text": "系统全部接口需要幂等与操作日志"},            # 横切（0 affinity）
        {"id": "req-d1", "text": "UserController 增加导出接口"},              # → 用户中心（文件 stem）
    ]


def test_bucketing_routes_by_module_name_and_file_stem():
    by_mod, cross = bucket_requirement_items(_items(), _BATCHES)
    a_ids = {it["id"] for it in by_mod.get("alarm-sdk", [])}
    u_ids = {it["id"] for it in by_mod.get("用户中心", [])}
    assert "req-a1" in a_ids and "req-a1" not in u_ids, "外模块条目不注入本批（省 prompt）"
    assert "req-b1" in u_ids and "req-b1" not in a_ids, "中文模块名子串必须可路由"
    assert "req-d1" in u_ids, "文件名 stem 命中必须可路由"


def test_cross_cutting_goes_everywhere():
    by_mod, cross = bucket_requirement_items(_items(), _BATCHES)
    assert [c["id"] for c in cross] == ["req-c1"], "0 affinity=横切"
    # 不变量：任一条目至少出现在一个批（cross 进全部批）
    all_ids = {it["id"] for its in by_mod.values() for it in its} | {c["id"] for c in cross}
    assert all_ids == {"req-a1", "req-b1", "req-c1", "req-d1"}


def test_empty_inputs_safe():
    by_mod, cross = bucket_requirement_items([], _BATCHES)
    assert by_mod == {} and cross == []
    by_mod, cross = bucket_requirement_items(_items(), [])
    assert by_mod == {} and [c["id"] for c in cross] == [
        "req-a1", "req-b1", "req-c1", "req-d1"], "无批信息=全部按横切处理（安全回退）"


# ─────────────── 调用点：每批 prompt 只含本批候选 + 横切 ───────────────

class _R:
    def __init__(self, content):
        self.content = content


class _CaptureLLM:
    def __init__(self):
        self.prompts: list[str] = []

    async def ainvoke(self, msgs):
        user = msgs[-1]["content"]
        self.prompts.append(user)
        if "'alarm-sdk'" in user:
            f = "alarm-sdk/src/AlarmService.java"
        else:
            f = "user-center/src/UserController.java"
        return _R(json.dumps({"subtasks": [{
            "id": "st-1", "description": f"impl {f}",
            "scope": {"create_files": [f], "writable": [], "readable": []},
        }]}, ensure_ascii=False))


async def test_ultra_batched_injects_bucketed_subset(monkeypatch):
    monkeypatch.setenv("SWARM_PLAN_BATCH_TIMEOUT", "5")
    monkeypatch.setenv("SWARM_PLAN_BATCH_MAX_ATTEMPTS", "1")
    import swarm.brain.nodes as _nodes
    monkeypatch.setattr(_nodes, "_get_brain_fallback_llm", lambda: None)
    from swarm.brain.nodes import _plan_ultra_batched
    llm = _CaptureLLM()
    state = {
        "tech_design": {"modules": [
            {"name": "alarm-sdk", "depends_on": []},
            {"name": "user-center", "depends_on": []},
        ]},
        "shared_contract_draft": {},
        "project_id": "",
        "requirement_items": [
            {"id": "req-a1", "text": "alarm-sdk 模块提供告警发送 SDK 能力"},
            {"id": "req-b1", "text": "user-center 支持按部门筛选用户列表"},
            {"id": "req-c1", "text": "系统全部接口需要幂等与操作日志"},
        ],
    }
    file_plan = [
        {"path": "alarm-sdk/src/AlarmService.java", "module": "alarm-sdk", "action": "create"},
        {"path": "user-center/src/UserController.java", "module": "user-center", "action": "create"},
    ]
    await _plan_ultra_batched(llm, state, "需求", {}, "", file_plan)
    p_alarm = next(p for p in llm.prompts if "'alarm-sdk'" in p)
    assert "req-a1" in p_alarm, "本批条目必须注入"
    assert "req-b1" not in p_alarm, "外模块条目不得注入（F8 省 prompt 双线性膨胀）"
    assert "req-c1" in p_alarm and "横切" in p_alarm, (
        "横切条目注入所有批并明示可任务级认领（A10②）")
