"""P6（round37b）：契约合并同模块同名接口自并去重 — 行为测试。

定案依据 memory/swarm-e2e-round37-postmortem 新发现：CONTRACT_MERGE 同名接口爆炸
（IAlarmTaskService "alarm-core 并入 alarm-core" 并 8 次）——U1/U3 bisect 把一个模块拆成
~a/~b 子批，各自生成该模块共享接口 → 同模块同名重复。治本=区分【同模块自并（边界重叠/
bisect 产物）】vs【跨模块真多版】：同模块仍并集保方法（安全），日志聚合成一条边界重叠告警。

栈无关：抽象 module/interface 名，无框架词汇。
"""

from __future__ import annotations

from swarm.brain.planning_nodes import _merge_module_contracts


def _iface(name, module, sig):
    return {"name": name, "module": module, "signature": sig}


def test_intra_module_same_name_interface_dedupes_to_one():
    """同模块同名接口（bisect ~a/~b 各生成一遍）→ 去重为一条。"""
    slices = [
        {"interfaces": [_iface("ISvc", "core", "a()")]},
        {"interfaces": [_iface("ISvc", "core", "a()")]},  # 同模块自并
    ]
    merged = _merge_module_contracts({}, slices)
    ifaces = [i for i in merged["interfaces"] if i["name"] == "ISvc"]
    assert len(ifaces) == 1, "同模块同名接口去重为一条（非爆炸多条）"


def test_intra_module_union_preserves_all_methods():
    """同模块自并仍并集——不同签名的方法都进共享契约，不丢方法。"""
    slices = [
        {"interfaces": [_iface("ISvc", "core", "a()")]},
        {"interfaces": [_iface("ISvc", "core", "b()")]},  # 同模块，签名有出入
    ]
    merged = _merge_module_contracts({}, slices)
    sig = [i for i in merged["interfaces"] if i["name"] == "ISvc"][0]["signature"]
    assert "a()" in sig and "b()" in sig, "并集保方法不丢（防 worker cannot-find-method）"


def test_cross_module_same_name_still_unions():
    """语义演进（阶段6 D10）：跨模块同名=不同契约，各自独立成条（合并键 (module,name)）——
    旧全局并集把跨模块同名接口强行合体（module 归属取首版），round37 实测 168→148
    接口爆炸自并来源。两模块方法都在（各自条目内），不丢方法的意图保留。"""
    slices = [
        {"interfaces": [_iface("ISvc", "core", "a()")]},
        {"interfaces": [_iface("ISvc", "web", "b()")]},   # 跨模块
    ]
    merged = _merge_module_contracts({}, slices)
    ifaces = [i for i in merged["interfaces"] if i["name"] == "ISvc"]
    assert len(ifaces) == 2
    _sigs = " ".join(str(i.get("signature")) for i in ifaces)
    assert "a()" in _sigs and "b()" in _sigs


def test_intra_module_boundary_overlap_warning():
    """同模块自并聚合成【一条】边界重叠告警（surface 真信号、去 churn）。"""
    import logging
    slices = [
        {"interfaces": [_iface("ISvc", "core", "a()")]},
        {"interfaces": [_iface("ISvc", "core", "b()")]},
        {"interfaces": [_iface("ISvc", "core", "c()")]},
    ]
    # 乱序鲁棒（house 惯例，见 test_merge_apply_check_base_tree_round29）：全套件里有测试用
    # logging.disable / 祖先 logger propagate=False 污染全局日志态，依赖传播到 root 的 caplog 会
    # 抓空。改为把捕获 handler 直接挂到【发射 logger】(swarm.brain.planning_nodes) 上——对
    # propagate/root 配置免疫；logging.disable 仍须复位（它在 emit 源头全局闸断）。
    logging.disable(logging.NOTSET)
    _lg = logging.getLogger("swarm.brain.planning_nodes")
    _records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            _records.append(record)

    _h = _Capture(level=logging.WARNING)
    _lvl, _disabled = _lg.level, _lg.disabled
    _lg.addHandler(_h)
    _lg.setLevel(logging.NOTSET)
    _lg.disabled = False
    try:
        _merge_module_contracts({}, slices)
    finally:
        _lg.removeHandler(_h)
        _lg.setLevel(_lvl)
        _lg.disabled = _disabled
    overlap_warns = [r for r in _records if "P6 模块边界重叠" in r.getMessage()]
    assert len(overlap_warns) == 1, "同模块自并聚合成一条告警，不逐次 churn"
