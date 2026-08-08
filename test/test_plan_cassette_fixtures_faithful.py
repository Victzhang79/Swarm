"""#29-4 T-5 复核 H-2 整改锁：入库最小夹具必须**忠实于真卡带**。

## 为什么需要这个文件

第一版最小化只保留了 `id/description/difficulty/modality + scope`，`issues` 确实逐字
未变，我据此在三处 docstring 写下"判据逐字不变"。**复核实测证伪**：`warnings` 变了
（r62 14→8、r64 7→4），丢的是 R67-12「纯辅助产物却依赖 52/83 个子任务=全图汇聚点」
那一族；更坏的是 r64 **凭空造出**一条真 plan 从未有过的 zero-dir 警告
（`['ruoyi-admin']` 无代码落点）——夹具**假红**。

根因是两个被判据消费的字段被白名单漏掉：
  · `subtask.depends_on`  → R67-12 汇聚点检查读它
  · `plan.shared_contract` → 模块宇宙（`want`）的一半来源

## 这个文件锁什么

① **结构面**（无卡带也能跑，CI 上真跑）：夹具必须带 `depends_on` 与
   `plan.shared_contract`，且不得退化成空。这是"未来再有人重编白名单"时会红的那道闸。
② **保真面**（仅本机有卡带时跑）：夹具与真卡带的 `issues` **和** `warnings` 集合逐条相等。

★为什么 ① 不能只靠 ②★：卡带是 gitignore 的本地排障件，② 在 CI 上必然跳过。
若只有 ②，这次修好的东西在 CI 上就没有任何守护——正是 T-5 本身要治的那个形状。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from swarm.brain.plan_validator import validate_module_coherence
from swarm.types import TaskPlan

_FIX = Path(__file__).resolve().parent / "fixtures" / "plan_cassettes"
_CASS = Path(__file__).resolve().parents[1] / "cassettes"

# (夹具名, 源卡带名) —— 与 scripts/cassette_minimize.py 的 TARGETS 同源
_PAIRS = [
    ("round62_alarm_api_double_root.json", "01520400_final.json"),
    ("round64_toplevel_sql_weak_evidence.json",
     "f1e0f7b5-3be8-438e-8c07-fef2dc5588a6.json"),
]
_WITH_PLAN = {p[0] for p in _PAIRS}


def _verdict(cass: dict):
    plan = TaskPlan.model_validate(cass["plan"])
    return validate_module_coherence(plan, file_plan=cass.get("file_plan") or [])


@pytest.mark.parametrize("fix_name", sorted(_WITH_PLAN))
def test_fixture_retains_fields_the_verdict_consumes(fix_name):
    """★结构锁（CI 上真跑）★ 判据消费的字段一个都不许少。

    这两个字段是复核 H-2 实测出来的漏项。若有人再"精简"夹具把它们去掉，
    `issues` 仍会逐字不变（第一版就是这样过关的），只有本条会红。
    """
    d = json.loads((_FIX / fix_name).read_text(encoding="utf-8"))
    plan = d.get("plan") or {}
    sts = plan.get("subtasks") or []
    assert sts, f"{fix_name}: plan.subtasks 为空"

    n_dep = sum(1 for s in sts if s.get("depends_on"))
    assert n_dep > 0, (
        f"{fix_name}: 没有任何 subtask 带 depends_on ⇒ R67-12「全图汇聚点」检查"
        "整族无法触发。真卡带里 81/83（r62）、100/106（r64）个子任务带它。")
    # 不设具体条数下界：那会变成"凭一个数字推断另一个量"（本批已犯过一次）。
    # 判据是"这一族能被触发"，非空即足够；数量对齐由下面的保真锁负责。

    sc = plan.get("shared_contract")
    assert sc, (
        f"{fix_name}: plan.shared_contract 缺失/为空 ⇒ 模块宇宙口径变了。"
        "实测后果不是少几条 warning，而是 r64 会**凭空造出**一条真 plan 没有的"
        "zero-dir 警告（夹具假红，排查者会去改本来正确的生产代码）。")
    assert "dependencies" in sc, (
        f"{fix_name}: shared_contract 缺 dependencies 键（模块宇宙的一半来源）")


@pytest.mark.parametrize("fix_name,cass_name", _PAIRS)
def test_fixture_verdict_matches_real_cassette(fix_name, cass_name):
    """★保真锁（仅本机有卡带时）★ issues **与** warnings 都必须逐条相等。

    ★这条断言的形状是复核逼出来的★：原本我只比 `issues`，于是"判据逐字不变"这句话
    在 warnings 上是假的。凡是声称"与真实现场等价"，就必须把**全部输出面**都比过，
    而不是比其中一个自己当时想到的字段。
    """
    cf = _CASS / cass_name
    if not cf.exists():
        pytest.skip(f"CASSETTE_ABSENT:{cass_name}（本地排障件，CI 无——"
                    f"结构锁 test_fixture_retains_fields_the_verdict_consumes 在 CI 真跑）")
    real = _verdict(json.loads(cf.read_text(encoding="utf-8")))
    small = _verdict(json.loads((_FIX / fix_name).read_text(encoding="utf-8")))

    assert set(small.issues) == set(real.issues), (
        f"{fix_name}: issues 与真卡带不等。\n"
        f"只在夹具: {sorted(set(small.issues) - set(real.issues))}\n"
        f"只在卡带: {sorted(set(real.issues) - set(small.issues))}")

    rw = set(getattr(real, "warnings", []) or [])
    sw = set(getattr(small, "warnings", []) or [])
    only_fix = sw - rw
    only_cass = rw - sw
    assert not only_fix, (
        f"{fix_name}: 夹具**凭空造出** {len(only_fix)} 条真 plan 没有的 warning ⇒ "
        f"假红风险（将来该档硬化成 REJECT 时会让人去改本来正确的代码）：\n"
        + "\n".join(f"  ! {w[:180]}" for w in sorted(only_fix)))
    assert not only_cass, (
        f"{fix_name}: 夹具丢了 {len(only_cass)} 条真 plan 有的 warning ⇒ "
        f"守护面静默缩小（该档硬化成 REJECT 时夹具无法触发它）：\n"
        + "\n".join(f"  - {w[:180]}" for w in sorted(only_cass)))


def test_generator_targets_match_this_files_pairs():
    """★同源锁★ 本文件的 `_PAIRS` 必须与 `scripts/cassette_minimize.py` 的 TARGETS 一致。

    治的是"纪律条文与自动化脚本的清单必须是同一份"：生成器加了第四个夹具而本文件
    不知道，那个新夹具就完全没有保真守护，且没有任何信号。
    """
    import importlib.util
    gen = Path(__file__).resolve().parents[1] / "scripts" / "cassette_minimize.py"
    spec = importlib.util.spec_from_file_location("_cassette_minimize", gen)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    gen_pairs = {(out, src) for out, src, _desc in mod.TARGETS}
    mine = set(_PAIRS)
    # r67b 只用 file_plan、无 plan，不参与判据比对，故本文件刻意不收它——
    # 但必须**显式**声明这个差集，否则"漏收"与"刻意不收"不可分。
    intentional_gap = {("round67b_cross_module_create.json",
                        "251e05f3-7460-4578-850c-63f445766eb1.json")}
    assert gen_pairs - mine == intentional_gap, (
        f"生成器与本文件的夹具清单漂了。\n"
        f"生成器有而本文件没有: {sorted(gen_pairs - mine - intentional_gap)}\n"
        f"本文件有而生成器没有: {sorted(mine - gen_pairs)}\n"
        "新增夹具必须同时补进本文件的 _PAIRS（否则它没有保真守护）。")
