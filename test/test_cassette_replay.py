#!/usr/bin/env python3
"""OFFLINE record/replay harness —— cassette_replay 端到端离线自证。

痛点（记忆 round53~61）：swarm 近 60+ 轮 live E2E 全烧在同一条确定性 plan→scaffold 流水线上。
本测试用一份【合成 cassette】（多聚合目录布局 ruoyi-alarm/alarm-api + ruoyi-biz/biz-core，
正是 round57/61 的死因几何）走完 scripts/cassette_replay.replay_cassette，断言它离线、零 LLM
地跑出一份【脚手架注入完毕且结构合法】的 plan——把"抽 plan→重放确定性流水线"这条排障回路
锁死在 CI 里，替代 $/次 的 live E2E 撞 bug。复用 test_r39_build_scaffold_inject.py 的模式。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# scripts/ 不是包——按路径加载 cassette_replay 模块（其内部自行 bootstrap `swarm`）。
_rp = Path(__file__).resolve().parent.parent / "scripts" / "cassette_replay.py"
_rp_spec = importlib.util.spec_from_file_location("cassette_replay", _rp)
cassette_replay = importlib.util.module_from_spec(_rp_spec)
# 注册进 sys.modules：dataclass + `from __future__ import annotations` 解析注解时会回查
# cls.__module__ 的 sys.modules 记录，缺失则 collection 期 AttributeError。
sys.modules["cassette_replay"] = cassette_replay
_rp_spec.loader.exec_module(cassette_replay)

from swarm.brain.plan_validator import validate_plan_structure  # noqa: E402
from swarm.types import (  # noqa: E402
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
)


def _st(sid, create=None, writable=None):
    return SubTask(id=sid, description=f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(create_files=create or [], writable=writable or []))


def _multi_aggregator_cassette(project_path: str) -> dict:
    """合成 cassette：两个聚合目录各一个子模块（ruoyi-alarm/alarm-api + ruoyi-biz/biz-core）。

    这正是 round57/61 的几何——两套聚合父 pom 必须各自被确定性建出、子模块脚手架各挂
    自己的父。cassette 的 plan 字段是 TaskPlan.model_dump()，与 extract 落盘格式一致。
    """
    # 三层几何（忠实 round62：st-root 写根 pom.xml 注册顶层聚合 → 聚合父 → 子模块）。
    # 根注册者的存在是关键：round29-A(c) 的 owner=根注册者 st-root，嵌套聚合父 ruoyi-alarm/
    # ruoyi-biz 与其子模块是【独立子层级】，R61 的 child→parent 继承边不被 owner 注册序反转
    # （正是 round62 真实几何：st-1 写根 pom，alarm-* 子模块保留对 ruoyi-alarm 的父依赖）。
    plan = TaskPlan(subtasks=[
        _st("st-root", create=["pom.xml"]),
        _st("st-1", create=["ruoyi-alarm/alarm-api/src/main/java/A.java"]),
        _st("st-2", create=["ruoyi-biz/biz-core/src/main/java/B.java"]),
    ], parallel_groups=[["st-root", "st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "alarm-api", "artifacts": ["org.projectlombok:lombok"]},
        {"module": "biz-core", "artifacts": ["org.projectlombok:lombok"]},
    ]}
    return {
        "schema": "swarm-plan-cassette/v1",
        "task_id": "synthetic-multi-agg",
        "project_path": project_path,
        "base_commit": None,
        "plan": plan.model_dump(mode="json"),
        "shared_contract": plan.shared_contract,
        "file_plan": [
            {"module": "alarm-api", "path": "ruoyi-alarm/alarm-api/src/main/java/A.java"},
            {"module": "biz-core", "path": "ruoyi-biz/biz-core/src/main/java/B.java"},
        ],
        "task_description": "synthetic multi-aggregator plan",
    }


@pytest.fixture(autouse=True)
def _stub_maven_registry(monkeypatch):
    """R53-1：坐标解析确定性打桩（单测禁联网，同 test_r39_build_scaffold_inject）。"""
    from swarm.brain import maven_registry as mr
    vers = {("org.projectlombok", "lombok"): "1.18.34"}
    monkeypatch.setattr(mr, "registry_latest_version", lambda g, a: vers.get((g, a)))
    monkeypatch.setattr(mr, "registry_group_for", lambda a: None)
    mr._http_cache.clear()


def _root_pom(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text(
        '<?xml version="1.0"?><project><groupId>com.ruoyi</groupId>'
        "<artifactId>ruoyi</artifactId><version>4.8.3</version>"
        "<packaging>pom</packaging></project>", encoding="utf-8")


def test_replay_runs_offline_and_injects_scaffolds(tmp_path):
    """端到端：合成 cassette → replay_cassette → 脚手架注入完毕、结构合法、无异常。"""
    _root_pom(tmp_path)
    cassette = _multi_aggregator_cassette(str(tmp_path))

    res = cassette_replay.replay_cassette(cassette, verbose=True)

    assert res.ok, f"离线重放不应崩溃，却崩在 {res.failed_stage}:\n{res.traceback_str}"
    assert res.failed_stage is None

    # 两个聚合父 POM 都被确定性建出（round57/61 死因的正解）。注意：这些边过 resolve_plan_
    # conflicts 后会被合法改写（共享根 pom 写者被 normalize 串行化）——故只锁"存在性 + 结构合法
    # + 无悬空依赖"这些经全流水线仍必须成立的不变量，不锁 inject 中间态的具体父子边。
    agg_ids = {st.id for st in res.plan.subtasks if st.id.startswith("st-scaffold-ruoyi-")}
    assert "st-scaffold-ruoyi-alarm" in agg_ids, f"缺 ruoyi-alarm 聚合父，实得 {agg_ids}"
    assert "st-scaffold-ruoyi-biz" in agg_ids, f"缺 ruoyi-biz 聚合父，实得 {agg_ids}"
    # 每个子模块也各有脚手架
    scaffold_ids = {st.id for st in res.plan.subtasks if st.id.startswith("st-scaffold-")}
    assert {"st-scaffold-alarm-api", "st-scaffold-biz-core"}.issubset(scaffold_ids)

    # 注入 + 冲突解决后的 plan 结构必须合法（parallel_groups 完整性 / 单一写者 / 无悬空依赖）
    r = validate_plan_structure(res.plan)
    assert r.valid, f"重放后 plan 结构校验必须通过: {r.issues}"

    # 报告面：scaffolds 机读清单 + DAG 都产出
    assert res.scaffolds, "inject 应返回非空脚手架清单"
    assert res.resolve_counts, "resolve_plan_conflicts 应返回计数字典"
    assert res.dag and all(isinstance(v, list) for v in res.dag.values())
    # DAG 不含悬空依赖（每个 depends_on 都指向真实子任务），且无自环
    ids = set(res.dag)
    for sid, deps in res.dag.items():
        assert sid not in deps, f"{sid} 自环依赖"
        for d in deps:
            assert d in ids, f"{sid} 依赖了不存在的 {d}"


def test_replay_preserves_scaffold_ordering_edges_r62(tmp_path):
    """R62-1 回归（harness 级）：replay 跑完整 elaborate 序（inject→decouple→resolve）后，
    module 脚手架→聚合父 的构建顺序边【必须存活】——它正是 round62 被 decouple 误剥的边。

    这个 harness 之前【没跑 decouple pass】，所以 round62 的「造边→删边」死链在工具里根本
    照不出来（记忆 swarm-round62-diagnosis）。现在 replay 忠实跑 decouple，本测试锁死：
    ①decouple 不得剥任何 st-scaffold-* 目标边；②module 脚手架经全流水线仍 depends_on 其聚合父。
    """
    _root_pom(tmp_path)
    cassette = _multi_aggregator_cassette(str(tmp_path))

    res = cassette_replay.replay_cassette(cassette, verbose=True)
    assert res.ok, f"重放崩在 {res.failed_stage}:\n{res.traceback_str}"

    by_id = {st.id: st for st in res.plan.subtasks}
    # module 脚手架 → 自己的聚合父，经 inject→decouple→resolve 全程仍在
    api = by_id.get("st-scaffold-alarm-api")
    core = by_id.get("st-scaffold-biz-core")
    assert api is not None and core is not None, "module 脚手架缺失"
    assert "st-scaffold-ruoyi-alarm" in (api.depends_on or []), (
        f"alarm-api 脚手架丢了对聚合父 ruoyi-alarm 的依赖（decouple 误剥=round62 死因复活），"
        f"实得 {api.depends_on}")
    assert "st-scaffold-ruoyi-biz" in (core.depends_on or []), (
        f"biz-core 脚手架丢了对聚合父 ruoyi-biz 的依赖，实得 {core.depends_on}")

    # decouple 绝不剥【目标是脚手架】的边（哪怕它零文件重叠+无契约）
    for sid, deps in res.dag.items():
        st = by_id[sid]
        # 任何仍以脚手架为源、指向另一脚手架/父的边都必须在——这里做存在性正向断言已足够
        if str(sid).startswith("st-scaffold-") and sid not in ("st-scaffold-ruoyi-alarm",
                                                                "st-scaffold-ruoyi-biz"):
            assert any(d.startswith("st-scaffold-ruoyi-") for d in deps), (
                f"{sid} 应保留对某聚合父的依赖，实得 {deps}")


def test_replay_result_reconstructs_taskplan_from_dump(tmp_path):
    """replay 从 model_dump 还原 TaskPlan（model_validate 往返），子任务数守恒 + 只增不减。"""
    _root_pom(tmp_path)
    cassette = _multi_aggregator_cassette(str(tmp_path))
    n_in = len(cassette["plan"]["subtasks"])

    res = cassette_replay.replay_cassette(cassette)

    assert res.ok
    assert isinstance(res.plan, TaskPlan)
    # 脚手架是净增（原始子任务 + 2 聚合父 + 2 子模块脚手架）
    assert len(res.plan.subtasks) > n_in
    assert {"st-1", "st-2"}.issubset({st.id for st in res.plan.subtasks}), "原始子任务不得丢失"


def test_replay_no_root_registrant_preserves_inheritance_edges_r62_rule4(tmp_path):
    """R62 收编（normalize 规则4 通道）：**无根 pom 写者**的多聚合几何——聚合父自身是唯一
    "registrant"。旧码 normalize 规则4 backstop 把聚合父当 owner、**反转清空** child→parent
    继承边（合成实测 alarm-api 依赖被抹成 []）= round62 死因经 normalize 通道复活。

    治本=规则4 REMOVE 步加 `not is_structural_scaffold_dep(owner)` 守卫：owner 是脚手架时
    保留 scaffold→scaffold 继承边，后续 ADD 由 _depends_transitively 守卫自动跳过、绝不成环。
    本测试锁死：无根写者时，子模块脚手架经全流水线仍 depends_on 各自聚合父，且图无环。
    """
    _root_pom(tmp_path)  # 磁盘有根 pom（触发规则4 gate），但计划里无人写它=无 registrant
    # 关键：不含 st-root（写 pom.xml 的子任务）——正是暴露反转的几何
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["ruoyi-alarm/alarm-api/src/main/java/A.java"]),
        _st("st-2", create=["ruoyi-biz/biz-core/src/main/java/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "alarm-api", "artifacts": ["org.projectlombok:lombok"]},
        {"module": "biz-core", "artifacts": ["org.projectlombok:lombok"]},
    ]}
    cassette = {
        "schema": "swarm-plan-cassette/v1", "task_id": "synth-no-root",
        "project_path": str(tmp_path), "base_commit": None,
        "plan": plan.model_dump(mode="json"), "shared_contract": plan.shared_contract,
        "file_plan": [
            {"module": "alarm-api", "path": "ruoyi-alarm/alarm-api/src/main/java/A.java"},
            {"module": "biz-core", "path": "ruoyi-biz/biz-core/src/main/java/B.java"}],
    }
    res = cassette_replay.replay_cassette(cassette, verbose=True)
    assert res.ok, f"重放崩在 {res.failed_stage}:\n{res.traceback_str}"
    by_id = {st.id: st for st in res.plan.subtasks}
    assert "st-scaffold-ruoyi-alarm" in (by_id["st-scaffold-alarm-api"].depends_on or []), (
        f"无根写者时 alarm-api 继承边被 normalize 规则4 反转清空（round62 复活），"
        f"实得 {by_id['st-scaffold-alarm-api'].depends_on}")
    assert "st-scaffold-ruoyi-biz" in (by_id["st-scaffold-biz-core"].depends_on or [])
    # 保留继承边 + 反向 ADD 自动跳过 → 结构合法（含无环）
    assert validate_plan_structure(res.plan).valid, "反转守卫后 plan 结构必须合法（含无环）"


def test_replay_strips_pre_injected_scaffolds_from_dispatching_cassette(tmp_path):
    """对抗复核 #2：`_strip_injected_scaffolds` 的 strip 路径必须有覆盖。真实 cassette 抽自
    DISPATCHING 已含 st-scaffold-* 子任务；本测试构造【已注入】的 cassette，断言 replay 先剥
    回 pre-scaffold（stripped>0）再重跑 inject，往返后脚手架齐备、原始子任务不丢、继承边在位。
    """
    _root_pom(tmp_path)
    base = _multi_aggregator_cassette(str(tmp_path))
    # 先跑一遍 replay 得到【已注入脚手架】的 plan，回灌成新 cassette 模拟 DISPATCHING 抽取态
    injected_plan = cassette_replay.replay_cassette(base).plan
    assert any(st.id.startswith("st-scaffold-") for st in injected_plan.subtasks)
    pre_injected = {**base, "task_id": "synth-pre-injected",
                    "plan": injected_plan.model_dump(mode="json")}

    res = cassette_replay.replay_cassette(pre_injected, verbose=True)

    assert res.ok, f"重放崩在 {res.failed_stage}:\n{res.traceback_str}"
    assert res.stripped_scaffolds > 0, "已注入 cassette 必须走 strip 路径（否则 inject 幂等跳过）"
    # 往返后脚手架重新齐备、原始业务子任务守恒、继承边重建
    ids = {st.id for st in res.plan.subtasks}
    assert {"st-1", "st-2", "st-scaffold-alarm-api", "st-scaffold-biz-core"}.issubset(ids)
    by_id = {st.id: st for st in res.plan.subtasks}
    assert "st-scaffold-ruoyi-alarm" in (by_id["st-scaffold-alarm-api"].depends_on or [])
