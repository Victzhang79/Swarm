"""P1-DEBT-02 回归（SWARM_CTO_GUIDE §4）：_plan_ultra_batched 读错键 tech_design_result。

原 bug：读 state['tech_design_result']（全项目无人写）→ td 恒空 →
批间模块依赖排序(module_deps)失效 + data_model/契约注入空。
修复：键名改 tech_design；契约改取 shared_contract_draft。
本测用源码静态断言守护（避免起全 Brain 图的重量级 e2e）。
"""
import inspect

import swarm.brain.nodes as nodes_mod


def test_plan_ultra_batched_reads_correct_keys():
    src = inspect.getsource(nodes_mod._plan_ultra_batched)
    # 不应再读不存在的 tech_design_result 键
    assert 'state.get("tech_design_result")' not in src, "仍在读错键 tech_design_result（P1-DEBT-02 未修）"
    # 应读正确的 tech_design 键
    assert 'state.get("tech_design")' in src, "应读 tech_design 键"
    # 契约应从 shared_contract_draft 取（不是 td.get('shared_contract')，那恒空）
    assert 'shared_contract_draft' in src, "契约应取 state.shared_contract_draft"


def test_module_deps_sourced_from_tech_design_modules():
    """批间依赖排序的 module_deps 应来自 td['modules']（td 现在非空）。"""
    src = inspect.getsource(nodes_mod._plan_ultra_batched)
    assert 'td.get("modules")' in src, "module_deps 应来自 tech_design.modules"
