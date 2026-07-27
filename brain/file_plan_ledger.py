"""H-6（SPEC_h6_file_plan_reconciliation）：file_plan 裁决账 + 对账收缩总闸（独立叶簇）。

自 contract_utils.py 拆出（战役级终扫 reviewer MEDIUM，纪律#9 god-file 不再喂肥）：
仅依赖 contract_utils 的 _norm_scope_path / classpath_fqn_key 两个原语，自包含成簇。
contract_utils 经 PEP 562 __getattr__ 惰性 re-export 保可寻址（既有调用点/测试零改动；
其内部 3 处调用改函数级 import——LOAD_GLOBAL 不经 __getattr__）。

机制全貌见 21 号文批次8 与 SPEC_h6：裁决账 append-only（BrainState
file_plan_adjudications，monotonic 登记，REVISE/replan 新周期清空重推导）；
写入方=#101/层③/R67G 预消解/CVB 归位；消费方=reconcile（PLAN 重拆前）+ attach 前置核。
"""
from __future__ import annotations

import logging

from swarm.brain.contract_utils import _norm_scope_path, classpath_fqn_key

logger = logging.getLogger(__name__)


# ── H-6（SPEC_h6_file_plan_reconciliation）：file_plan 裁决账 + 对账收缩总闸 ─────────────
def _record_adjudication(ledger: list | None, *, pass_name: str, action: str,
                         path: str, owner_path: str | None, round_no: int = 0) -> None:
    """H-6 裁决账追加（append-only）。调用方=各确定性 pass 做出 strip/relocate/dedupe 裁决处。

    (action, path) 去重保首次——同一违例跨 retry 轮被同一 pass 重判是常态（幂等重放前提），
    不重复入账胀账。path 归一化（_norm_scope_path 同源）后入账；空 path 不入。ledger=None
    =调用方没接线（离线评测等）→ no-op，绝不炸。"""
    if ledger is None:
        return
    _p = _norm_scope_path(path)
    if not _p:
        return
    _o = _norm_scope_path(owner_path) if owner_path else None
    for e in ledger:
        if e.get("action") == action and e.get("path") == _p:
            # 闸门 R2 reviewer LOW④（与 R1④ 同根）：空 owner 先行不得遮挡后续有效 owner——
            # 去重保首次但允许 owner 被非空值修正，否则 adjudicated_path_set 对该 fqn 的
            # 膨胀收缩永久失明（depends_on 改指也会丢目标）。
            if _o and not e.get("owner_path"):
                e["owner_path"] = _o
            return
    ledger.append({"round": round_no, "pass": pass_name, "action": action,
                   "path": _p, "owner_path": _o})


def adjudicated_path_set(adjudications: list | None) -> tuple[set[str], dict[str, str]]:
    """H-6 消费侧共用：裁决账 → (精确路径集, JVM fqn→owner 映射)。

    fqn 映射=「同串」判据（同 simple-name 同包）：#101 同 FQN 跨模块副本、层③/R67G 异包
    副本的同 fqn 变体路径（重拆/L2 补排可能换个目录重发明）都算复活面；同名不同包=
    不同 fqn 天然放行（复核点名：粘滞误拒合法新文件防的就是这个）。非 JVM 路径
    classpath_fqn_key=None → 只受精确路径集约束（同名跨包合法，栈中立）。"""
    paths: set[str] = set()
    fqns: dict[str, str] = {}
    for adj in (adjudications or []):
        if not isinstance(adj, dict):
            continue
        p = _norm_scope_path(str(adj.get("path") or ""))
        if not p:
            continue
        paths.add(p)
        key = classpath_fqn_key(p)
        if key:
            # 批次8 闸门 reviewer MEDIUM：owner 为空串也 setdefault 会占位——后续同 fqn 的
            # 有效 owner 永远无法修正，膨胀收缩对该 fqn 永久失明。空 owner 不入映射
            # （精确路径集仍约束该 path 本身）。
            _o = _norm_scope_path(str(adj.get("owner_path") or ""))
            if _o:
                fqns.setdefault(key[1], _o)
    return paths, fqns


def reconcile_file_plan_ledger(file_plan: list | None,
                               adjudications: list | None) -> dict[str, int]:
    """H-6 对账收缩总闸（PLAN 重拆前 first thing）：裁决重放 + 膨胀收缩，幂等。

    1) 裁决重放：file_plan 里仍残留/已复活的【精确路径】create 条目（checkpoint 回退/
       上一轮漏回写面）→ 删，depends_on 改指 owner（与 _strip_file_plan_create_entries 同构）。
    2) 膨胀收缩：同串（JVM 同 fqn）变体路径的 create 条目——除 owner 落点本身外 → 删
       （重拆/挂靠/L2 补排的复活面在此关死）。owner 落点豁免：owner 是唯一合法 create 者。
    3) 对账留痕：每条删除打 INFO 带（路径, 裁决来源 pass, 轮次）；计数返回（可观测）。
    bare-str 条目视作 create（与 _strip_file_plan_create_entries 同口径）。幂等：重放两次
    =一次（删除后无条目可再删）。"""
    counts = {"adjudications_replayed": 0, "new_entries_shrunk": 0}
    if not file_plan or not adjudications:
        return counts
    strip_paths, strip_fqns = adjudicated_path_set(adjudications)
    if not strip_paths:
        return counts
    _pass_of = {_norm_scope_path(str(a.get("path") or "")): (a.get("pass"), a.get("round"))
                for a in adjudications if isinstance(a, dict)}
    removed: set[int] = set()
    removed_to_owner: dict[str, str] = {}
    for e in file_plan:
        if isinstance(e, dict):
            _p = _norm_scope_path(str(e.get("path") or ""))
            _act = str(e.get("action") or "create")
        else:
            _p, _act = _norm_scope_path(str(e)), "create"   # bare-str 视作 create（同 H-1 口径）
        if _act != "create" or not _p:
            continue
        _kind = _owner = None
        if _p in strip_paths:
            _kind, _owner = "adjudications_replayed", _pass_owner(adjudications, _p)
        else:
            _k = classpath_fqn_key(_p)
            if _k and _k[1] in strip_fqns:
                _cand = strip_fqns[_k[1]]
                if _cand and _cand != _p:   # owner 落点本身豁免（唯一合法 create 者）
                    _kind, _owner = "new_entries_shrunk", _cand
        if not _kind:
            continue
        removed.add(id(e))
        if _owner:
            removed_to_owner[_p] = _owner
        counts[_kind] += 1
        _src = _pass_of.get(_p) or (None, None)
        logger.info(
            "[FILEPLAN-LEDGER] H-6 %s：删除 file_plan create 条目 %s（裁决 pass=%s 轮次=%s "
            "→ owner=%s；已裁决违例不随重拆复活）",
            _kind, _p, _src[0], _src[1], _owner)
    if removed:
        file_plan[:] = [e for e in file_plan if id(e) not in removed]
        for e in file_plan:      # depends_on resync：引用被删路径 → 改指 owner（去重）
            if not isinstance(e, dict) or not e.get("depends_on"):
                continue
            _new: list = []
            _chg = False
            for d in (e.get("depends_on") or []):
                _own = removed_to_owner.get(_norm_scope_path(str(d)))
                if _own:
                    _chg = True
                    if _own not in _new:
                        _new.append(_own)
                elif d not in _new:
                    _new.append(d)
            if _chg:
                e["depends_on"] = _new
    return counts


def _pass_owner(adjudications: list, norm_path: str) -> str | None:
    """裁决账里某 path 的 owner 落点（重放改指用）。"""
    for a in adjudications:
        if isinstance(a, dict) and _norm_scope_path(str(a.get("path") or "")) == norm_path:
            _o = _norm_scope_path(str(a.get("owner_path") or ""))
            return _o or None
    return None


