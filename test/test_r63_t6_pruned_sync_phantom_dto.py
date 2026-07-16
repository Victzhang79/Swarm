"""T6（round63）：契约剪除同源传播（治"验收逼 worker 复入"）+ 幻影 DTO 消解。

round63 死因（register T6 调查结论，cassette 实锤）：
① st-5 验收标准要求声明含 spring-boot-starter-aop 的 20 项（"缺一即整模块 mvn compile
  失败"），而权威模板只有 19 项——R53-1"模板/契约/验收三处同源剔除"只实现在**脚手架子任务**
  自己的三处；shared_contract.dependencies 本身从未被剪、normalize 规则5 的验收 note 用的是
  **未解析原始 artifacts** → worker 是被验收+契约逼着把被剪依赖复入 pom 的（非自作主张）。
  治：PLAN 期统一 resolve 一次并回写 entry.artifacts=kept，dropped 落 pruned_artifacts
  持久账本（随契约下发 worker=负面知识）。此后一切消费面同源。"禁复入"字面执行有害
  （误剪→防线④救不回=永久缺依赖），不采纳；防线④按真实 import+Central 反查注入本就合法。
② AlarmTaskDTO 在契约 dtos/apis（带完整 fields）且被接口签名引用，但 plan 零 create 文件、
  零语料提及——dtos 是软符号 C1 只警不闸，worker 实现接口时只能臆造包名（8× "package
  com.ruoyi.alarm.core.domain.dto does not exist"）。治：_domicile_contract_symbols 扩展——
  被 interfaces[].signature / apis 引用的无主 dtos 条目与硬符号同等安置成真产出文件
  （T4 pin 随后钉 defined_in）。孤立无引用的 dto 不安置（宁缺勿滥）。
"""
from __future__ import annotations

import pytest
import swarm.brain.maven_registry as mr
from swarm.brain.contract_utils import (
    inject_build_scaffold_subtasks,
    normalize_plan_scopes,
    prune_contract_dependencies,
)
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

ROOT_POM = (
    '<?xml version="1.0"?><project><groupId>com.ruoyi</groupId>'
    "<artifactId>ruoyi</artifactId><version>4.8.3</version>"
    "<packaging>pom</packaging></project>")


def _st(sid, *, create=None, writable=None, readable=None, desc=None, acc=None):
    return SubTask(id=sid, description=desc or f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(create_files=create or [], writable=writable or [],
                                   readable=readable or []),
                   acceptance_criteria=acc or [])


def _plan(subs, shared_contract=None):
    plan = TaskPlan(subtasks=subs, parallel_groups=[[s.id for s in subs]])
    plan.shared_contract = shared_contract or {}
    return plan


@pytest.fixture()
def proj(tmp_path, monkeypatch):
    (tmp_path / "pom.xml").write_text(ROOT_POM, encoding="utf-8")
    # 离线：lombok 可解析（Central 反查有唯一 group+版本），aop 解析不到（模拟 round63 被剪）
    monkeypatch.setattr(mr, "bom_managed_artifacts", lambda g, a, v: {})
    monkeypatch.setattr(
        mr, "registry_group_for",
        lambda a: "org.projectlombok" if a == "lombok" else None)
    monkeypatch.setattr(
        mr, "registry_latest_version",
        lambda g, a: "1.18.30" if a == "lombok" else None)
    mr._http_cache.clear()
    return str(tmp_path)


# ───────────────────── ① 契约剪除同源传播 ─────────────────────

def test_prune_rewrites_entry_to_kept_and_records_ledger(proj):
    """核心：entry.artifacts 回写为 kept；dropped 落 pruned_artifacts 账本。"""
    plan = _plan([_st("st-5", create=["ruoyi-alarm/pom.xml",
                                      "ruoyi-alarm/src/main/java/A.java"])],
                 {"dependencies": [{"module": "ruoyi-alarm",
                                    "artifacts": ["lombok", "spring-boot-starter-aop"]}]})
    pruned = prune_contract_dependencies(plan, proj)
    entry = plan.shared_contract["dependencies"][0]
    assert not any("aop" in a for a in entry["artifacts"]), "被剪依赖必须从共享契约消失"
    assert any("lombok" in a for a in entry["artifacts"])
    ledger = plan.shared_contract.get("pruned_artifacts") or {}
    assert "spring-boot-starter-aop" in (ledger.get("ruoyi-alarm") or [])
    assert pruned


def test_prune_idempotent(proj):
    plan = _plan([_st("st-5", create=["ruoyi-alarm/pom.xml"])],
                 {"dependencies": [{"module": "ruoyi-alarm",
                                    "artifacts": ["lombok", "spring-boot-starter-aop"]}]})
    prune_contract_dependencies(plan, proj)
    snap = repr(plan.shared_contract)
    prune_contract_dependencies(plan, proj)
    assert repr(plan.shared_contract) == snap, "幂等：二跑不再变"


def test_prune_failopen_on_resolver_error(proj, monkeypatch):
    """解析器异常 ≠ 全部不可解析——绝不把契约剪空（fail-open 不动 + 留痕）。"""
    plan = _plan([_st("st-5", create=["ruoyi-alarm/pom.xml"])],
                 {"dependencies": [{"module": "ruoyi-alarm",
                                    "artifacts": ["lombok", "spring-boot-starter-aop"]}]})
    def _boom(*a, **k):
        raise RuntimeError("resolver down")
    monkeypatch.setattr(mr, "resolve_artifacts", _boom)
    prune_contract_dependencies(plan, proj)
    entry = plan.shared_contract["dependencies"][0]
    assert "spring-boot-starter-aop" in entry["artifacts"], "解析器坏了绝不误剪契约"
    assert "pruned_artifacts" not in plan.shared_contract


def test_round63_st5_acceptance_no_longer_demands_pruned(proj):
    """★round63 死型端到端★：注入+归一后，owner 的验收标准绝不再要求被剪依赖
    （规则5 note 读的是已剪 entry → 与模板同源，worker 不再被逼复入）。"""
    st5 = _st("st-5", create=["ruoyi-alarm/pom.xml",
                              "ruoyi-alarm/src/main/java/com/ruoyi/alarm/A.java"])
    plan = _plan([st5], {"dependencies": [{"module": "ruoyi-alarm",
                                           "artifacts": ["lombok",
                                                         "spring-boot-starter-aop"]}]})
    inject_build_scaffold_subtasks(plan, proj)
    normalize_plan_scopes(plan.subtasks, project_path=proj)
    blob = st5.description + " ".join(st5.acceptance_criteria)
    assert "spring-boot-starter-aop" not in blob, \
        "验收/模板任何一处要求被剪依赖=逼 worker 复入（round63 死型）"
    ledger = plan.shared_contract.get("pruned_artifacts") or {}
    assert "spring-boot-starter-aop" in (ledger.get("ruoyi-alarm") or [])


def test_fully_pruned_module_still_gets_scaffold(proj):
    """全部 artifacts 被剪空的模块仍须有 pom 脚手架出口（防 T5-F1 换壳）。"""
    plan = _plan([_st("st-6", create=["ruoyi-alarm/src/main/java/A.java"])],
                 {"dependencies": [{"module": "ruoyi-alarm",
                                    "artifacts": ["spring-boot-starter-aop"]}]})
    inject_build_scaffold_subtasks(plan, proj)
    scaf = next((s for s in plan.subtasks if s.id == "st-scaffold-ruoyi-alarm"), None)
    assert scaf is not None, "契约依赖全剪空 ≠ 模块不需要 pom"


# ───────────────────── ② 幻影 DTO 消解 ─────────────────────

_CONTRACT_PHANTOM = {
    "interfaces": [{"name": "IAlarmTaskService", "module": "ruoyi-alarm",
                    "signature": "AlarmTaskDTO getTask(Long id); void save(AlarmTaskDTO dto)"}],
    "dtos": [
        {"name": "AlarmTaskDTO", "module": "ruoyi-alarm",
         "fields": ["Long taskId", "String taskName"]},
        {"name": "OrphanUnusedDTO", "module": "ruoyi-alarm", "fields": ["Long x"]},
    ],
}


def _run_domicile(plan, contract, proj):
    from swarm.brain.plan_finisher import _domicile_contract_symbols
    return _domicile_contract_symbols(plan, contract, proj, "任务描述", file_plan=None)


def test_phantom_dto_referenced_by_signature_domiciled(proj):
    """★round63 幻影本体★：dtos 条目被接口签名引用、无文件无语料 → 安置为真产出文件。"""
    plan = _plan([_st("st-1", create=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/Engine.java"],
                      desc="实现引擎")], _CONTRACT_PHANTOM)
    dom = _run_domicile(plan, _CONTRACT_PHANTOM, proj)
    placed = [s for sids in dom.values() for s in sids]
    assert "AlarmTaskDTO" in placed, "被签名引用的无主 dto 必须安置（否则 worker 臆造包名）"
    all_creates = [f for st in plan.subtasks for f in (st.scope.create_files or [])]
    assert any(f.rsplit("/", 1)[-1].startswith("AlarmTaskDTO.") for f in all_creates)


def test_unreferenced_unowned_dto_not_domiciled(proj):
    """宁缺勿滥：孤立无引用的无主 dto 不安置（交 C1 warn），绝不批量造文件。"""
    plan = _plan([_st("st-1", create=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/Engine.java"],
                      desc="实现引擎")], _CONTRACT_PHANTOM)
    dom = _run_domicile(plan, _CONTRACT_PHANTOM, proj)
    placed = [s for sids in dom.values() for s in sids]
    assert "OrphanUnusedDTO" not in placed


def test_owned_dto_untouched(proj):
    """已有 producer 文件的 dto 绝不重复安置。"""
    contract = {
        "interfaces": [{"name": "ISvc", "module": "ruoyi-alarm",
                        "signature": "AlarmRequest send(AlarmRequest r)"}],
        "dtos": [{"name": "AlarmRequest", "module": "ruoyi-alarm", "fields": ["String a"]}],
    }
    plan = _plan([_st("st-1", create=[
        "ruoyi-alarm/src/main/java/com/ruoyi/alarm/dto/AlarmRequest.java"])], contract)
    dom = _run_domicile(plan, contract, proj)
    placed = [s for sids in dom.values() for s in sids]
    assert "AlarmRequest" not in placed


# ───────────────────── 对抗复核回归锁（hunter） ─────────────────────

def test_prune_partial_failure_atomic(proj, monkeypatch):
    """★hunter#F1 HIGH（实证）★：第 2 个 entry 解析抛异常 → 第 1 个也不得被剪
    （暂存未提交=真·保持原样），账本为空。"""
    plan = _plan([_st("st-5", create=["mod-a/pom.xml"])],
                 {"dependencies": [
                     {"module": "mod-a", "artifacts": ["lombok", "spring-boot-starter-aop"]},
                     {"module": "mod-b", "artifacts": ["lombok"]}]})
    calls = {"n": 0}
    real = mr.resolve_artifacts
    def _flaky(pp, arts, **kw):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("boom on 2nd entry")
        return real(pp, arts, **kw)
    monkeypatch.setattr(mr, "resolve_artifacts", _flaky)
    assert prune_contract_dependencies(plan, proj) == {}
    entry_a = plan.shared_contract["dependencies"][0]
    assert "spring-boot-starter-aop" in entry_a["artifacts"], \
        "部分失败必须整批放弃，前面的 entry 不得已被剪（半应用谎报反模式）"
    assert "pruned_artifacts" not in plan.shared_contract


def test_prune_mass_drop_refused_as_degraded(proj, caplog):
    """hunter#F2：dropped 占比>50%%且≥3（断网形态）→ 拒剪 + WARNING（绝不把网络故障
    当'不可解析'永久剪进权威契约）。"""
    import logging as _logging
    plan = _plan([_st("st-5", create=["mod-a/pom.xml"])],
                 {"dependencies": [{"module": "mod-a",
                                    "artifacts": ["ghost-one", "ghost-two", "ghost-three"]}]})
    with caplog.at_level(_logging.WARNING):
        assert prune_contract_dependencies(plan, proj) == {}
    assert plan.shared_contract["dependencies"][0]["artifacts"] == [
        "ghost-one", "ghost-two", "ghost-three"]
    assert any("拒绝同源剪除" in r.message for r in caplog.records)


def test_prune_ledger_note_present(proj):
    """hunter#F3：账本必须带自释义 note（负面知识框定，防 worker 把账本读成待声明清单）。"""
    plan = _plan([_st("st-5", create=["mod-a/pom.xml"])],
                 {"dependencies": [{"module": "mod-a",
                                    "artifacts": ["lombok", "spring-boot-starter-aop"]}]})
    prune_contract_dependencies(plan, proj)
    note = plan.shared_contract.get("pruned_artifacts_note") or ""
    assert "请勿" in note and "pruned_artifacts" in note


def test_prune_transient_drop_recovers_next_round(proj, monkeypatch):
    """★复核 MED（跨轮破坏性别名）★：replan 不再生契约（同 dict 对象跨轮复用），
    瞬时误剪必须可复议——下一轮解析器恢复 → 契约从 artifacts_pre_prune 原始清单复原、撤账。"""
    plan = _plan([_st("st-5", create=["mod-a/pom.xml"])],
                 {"dependencies": [{"module": "mod-a",
                                    "artifacts": ["lombok", "spring-boot-starter-aop"]}]})
    # 第 1 轮：aop 解析不到（fixture 默认）→ 被剪+记账
    prune_contract_dependencies(plan, proj)
    entry = plan.shared_contract["dependencies"][0]
    assert "spring-boot-starter-aop" not in entry["artifacts"]
    assert entry["artifacts_pre_prune"] == ["lombok", "spring-boot-starter-aop"]
    # 第 2 轮（模拟 replan 复用同一契约对象）：解析器恢复，aop 可解析
    monkeypatch.setattr(mr, "registry_group_for",
                        lambda a: {"lombok": "org.projectlombok",
                                   "spring-boot-starter-aop": "org.springframework.boot"}.get(a))
    monkeypatch.setattr(mr, "registry_latest_version", lambda g, a: "9.9.9")
    prune_contract_dependencies(plan, proj)
    assert "spring-boot-starter-aop" in entry["artifacts"], \
        "瞬时误剪必须在解析器恢复后的下一轮自动复原（不可永久不可复议）"
    assert "mod-a" not in (plan.shared_contract.get("pruned_artifacts") or {}), "复原须撤账"
