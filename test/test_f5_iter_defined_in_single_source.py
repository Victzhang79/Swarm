"""LOW 收口 F5：契约 defined_in 扫描骨架收敛 `_iter_contract_defined_in` 的接线锁+相等锁。

同文件四份手写拷贝（①deconflict_cross_module_creates / ②contract_owner_ledger_block /
③_contract_owner_authority / ④deconflict_create_vs_base_modify_shadow）已收敛共享扫描
单一事实源——其中两份曾各自独立掉过一次队（G-H9、#29-8 M-7，只扫 interfaces 漏 dtos）。
接线锁判据：把 helper 整块换掉，四个消费者行为必须跟着变（helper 删掉/改坏 → 全红）；
patch 打定义模块（__globals__ 所在=contract_utils 本模块）防 vacuous 绿，每条带 seen 锁。
消费契约（键形状/冲突策略）刻意不收敛——四者的输出形状差异是分档设计（纪律 10）。
"""
import swarm.brain.contract_utils as cu
from swarm.types import FileScope, SubTask, TaskHarness, TaskPlan

_ALARM = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/appkey/domain/AlarmAppSecret.java"
_ADMIN = "ruoyi-admin/src/main/java/com/ruoyi/alarm/appkey/domain/AlarmAppSecret.java"
_ALARM_FQN = "com/ruoyi/alarm/appkey/domain/AlarmAppSecret.java"
_BASE_SYSMENU = "ruoyi-common/src/main/java/com/ruoyi/common/core/domain/entity/SysMenu.java"
_SHADOW_SYSMENU = "ruoyi-system/src/main/java/com/ruoyi/system/domain/SysMenu.java"
_SYSMENU_FQN = "com/ruoyi/common/core/domain/entity/SysMenu.java"


def _st(sid, *, create=None, writable=None, depends=None, lang="java"):
    return SubTask(
        id=sid, description="d",
        scope=FileScope(writable=writable or [], create_files=create or [], readable=[]),
        harness=TaskHarness(language=lang), depends_on=depends or [],
    )


def _fake_iter(triples, seen, tiers=None):
    """★31 号文 A1-H3 复核 HIGH-1★ stub 必须跟真签名同步接收 `require_module_root`，
    并把该档位【记下来】——否则 stub 与真函数签名分叉时 TypeError（本次实测），
    或更坏：stub 宽容地吞掉 kwarg ⇒ "哪个消费者要不要模块根"这一维从被测命题里消失。
    `tiers` 收集各消费者传的档位，供下面的档位锁断言（新增能力，非仅修签名）。"""
    def _impl(contract, *, require_module_root=True):
        seen.append(contract)
        if tiers is not None:
            tiers.append(require_module_root)
        return iter(list(triples))
    return _impl


# ── 接线锁 ×4：helper 换假 → 消费者输出必须反映假输入（空契约也有产出=走了 helper）──

def test_wiring_3_owner_authority_uses_helper(monkeypatch):
    seen: list = []
    monkeypatch.setattr(cu, "_iter_contract_defined_in", _fake_iter([
        ("mod-a", "com/x/Foo.java", "mod-a/src/main/java/com/x/Foo.java"),
        ("mod-b", "com/y/Foo.java", "mod-b/src/main/java/com/y/Foo.java"),   # 同 base 两 owner=歧义
    ], seen))
    owners, ambiguous = cu._contract_owner_authority({})   # 空契约也有产出=走了 helper
    assert seen, "③没调 helper（vacuous 绿锁）"
    assert owners == {"foo.java": "com/y/Foo.java"}, f"last-wins 消费契约变了: {owners}"
    assert ambiguous == {"foo.java"}, f"歧义集消费契约变了: {ambiguous}"


def test_wiring_2_ledger_block_uses_helper(monkeypatch):
    seen: list = []
    monkeypatch.setattr(cu, "_iter_contract_defined_in", _fake_iter([
        ("ruoyi-alarm", _ALARM_FQN, _ALARM),
    ], seen))
    block = cu.contract_owner_ledger_block(None)           # 无契约也有产出=走了 helper
    assert seen, "②没调 helper（vacuous 绿锁）"
    assert "AlarmAppSecret" in block, f"②的禁写块未反映 helper 输入: {block[:200]}"


def test_wiring_1_cross_module_creates_uses_helper(monkeypatch):
    seen: list = []
    monkeypatch.setattr(cu, "_iter_contract_defined_in", _fake_iter([
        ("ruoyi-alarm", _ALARM_FQN, _ALARM),   # 契约权威：owner=ruoyi-alarm 模块
    ], seen))
    owner = _st("st-owner", create=[_ALARM])
    dup = _st("st-dup", create=[_ADMIN])
    plan = TaskPlan(subtasks=[owner, dup], shared_contract={})   # 空契约也消解=走了 helper
    n = cu.deconflict_cross_module_creates(plan)
    assert seen, "①没调 helper（vacuous 绿锁）"
    assert n == 1, f"①未按 helper 的权威消解跨模块重复 create: n={n}"
    assert _ADMIN not in (dup.scope.create_files or [])
    assert "st-owner" in (dup.depends_on or [])


def test_wiring_4_cvb_shadow_uses_helper(monkeypatch):
    seen: list = []
    monkeypatch.setattr(cu, "_base_tree_listing", lambda *a, **k: [_BASE_SYSMENU])
    monkeypatch.setattr(cu, "_iter_contract_defined_in", _fake_iter([
        ("ruoyi-common", _SYSMENU_FQN, _BASE_SYSMENU),   # 契约声明 defined_in=base 真身
    ], seen))
    plan = TaskPlan(subtasks=[_st("st-16-1", create=[_SHADOW_SYSMENU])])
    plan.shared_contract = {}                              # 空契约也归位=走了 helper
    n = cu.deconflict_create_vs_base_modify_shadow(
        plan, [{"path": _SHADOW_SYSMENU, "action": "create", "module": ""}],
        project_path="/x", base_ref="HEAD")
    assert seen, "④没调 helper（vacuous 绿锁）"
    assert n == 1, f"④未按 helper 的契约权威归位影子: n={n}"
    assert _SHADOW_SYSMENU not in (plan.subtasks[0].scope.create_files or [])
    assert _BASE_SYSMENU in (plan.subtasks[0].scope.writable or [])


# ── 逐元素相等锁：golden 契约 → helper 与③的产出逐键逐值等于手录期望 ──

def _golden_contract():
    return {
        "interfaces": [
            {"name": "AlarmService", "defined_in":
                "ruoyi-alarm/src/main/java/com/ruoyi/alarm/service/AlarmService.java"},
            "not-a-dict",                                    # 非 dict 条目 → 跳过
            {"name": "NoDefinedIn"},                         # 无 defined_in → 跳过
        ],
        "dtos": [
            {"name": "AlarmLevelEnum", "defined_in":
                " ruoyi-alarm/src/main/java/com/ruoyi/alarm/enums/AlarmLevelEnum.java "},
            # ★带空白 padding——helper 统一 strip 的【刻意加宽】：①③④从拒绝变接受，钉死
            {"name": "AlarmLevelEnum", "defined_in":
                "ruoyi-other/src/main/java/com/ruoyi/other/enums/AlarmLevelEnum.java"},
            # 同 base 两 owner=歧义（③的消费契约）
            {"name": "GoThing", "defined_in": "cmd/foo/main.go"},   # 非 JVM → 跳过
        ],
        "types": "not-a-list",                               # 非 list section → 跳过
    }


def test_golden_helper_output_elementwise_equal():
    triples = list(cu._iter_contract_defined_in(_golden_contract()))
    assert triples == [
        ("ruoyi-alarm", "com/ruoyi/alarm/service/AlarmService.java",
         "ruoyi-alarm/src/main/java/com/ruoyi/alarm/service/AlarmService.java"),
        ("ruoyi-alarm", "com/ruoyi/alarm/enums/AlarmLevelEnum.java",
         "ruoyi-alarm/src/main/java/com/ruoyi/alarm/enums/AlarmLevelEnum.java"),
        ("ruoyi-other", "com/ruoyi/other/enums/AlarmLevelEnum.java",
         "ruoyi-other/src/main/java/com/ruoyi/other/enums/AlarmLevelEnum.java"),
    ], f"扫描骨架产出与手录期望不等（含 padding 刻意加宽条）: {triples}"


def test_golden_owner_authority_elementwise_equal():
    owners, ambiguous = cu._contract_owner_authority(_golden_contract())
    assert owners == {
        "alarmservice.java": "com/ruoyi/alarm/service/AlarmService.java",
        "alarmlevelenum.java": "com/ruoyi/other/enums/AlarmLevelEnum.java",   # last-wins
    }, f"③映射与手录期望不等: {owners}"
    assert ambiguous == {"alarmlevelenum.java"}, f"③歧义集与手录期望不等: {ambiguous}"


# ── ★31 号文 A1-H3 复核 HIGH-1：档位锁（共享骨架 / 消费契约分档）★ ──

def test_tier_only_cross_module_requires_module_root(monkeypatch):
    """四消费者里**只有** ①deconflict_cross_module_creates 需要物理模块根
    （#110 判「同 FQN 跨【模块】」，`owner_mod[_fqn]=_mod` 真用 module 元素）；
    另三处把 module 元素丢弃，判的是 classpath 级 simple-name，与模块边界无关。

    为什么这条锁必须存在：A1-H3 把 ③b/③f 两个【闸】放宽到根级 src（不要求模块根）后，
    若解算器/预防台账仍要求模块根 ⇒ 根级 src 上「闸 REJECT 而确定性清闸通道结构性不存在」
    ⇒ 打回 PLAN → LLM 重产 → 解算器仍失明 → 熔断 FAILED@PLAN。
    档位一旦被谁改回 True，本锁立刻红。
    """
    plan = TaskPlan(subtasks=[_st("st-1", create=[_ALARM]), _st("st-2", create=[_ADMIN])])

    checks = [
        ("① cross_module_creates(#110)", True,
         lambda: cu.deconflict_cross_module_creates(plan)),
        ("② owner_ledger_block", False,
         lambda: cu.contract_owner_ledger_block(None)),
        ("③ _contract_owner_authority", False,
         lambda: cu._contract_owner_authority({})),
    ]
    for label, want_tier, call in checks:
        seen: list = []
        tiers: list = []
        monkeypatch.setattr(cu, "_iter_contract_defined_in",
                            _fake_iter([("mod-a", _ALARM_FQN, _ALARM)], seen, tiers))
        call()
        assert seen, f"{label} 没调 helper（vacuous 绿）"
        assert tiers and all(t is want_tier for t in tiers), (
            f"{label} 的 require_module_root 应为 {want_tier}，实得 {tiers}——"
            "档位错=要么 #110 误判单模块工程「跨模块」，要么根级 src 解算器失明造死循环")


def test_tier_false_covers_root_level_src():
    """档位 False 必须真的多覆盖根级 src（否则档位形参是装饰）。
    同一份契约、两个档位，产出条数必须不同。"""
    contract = {"types": [{"name": "SysUser", "module": "app",
                           "defined_in": "src/main/java/com/x/system/SysUser.java"}]}
    strict = list(cu._iter_contract_defined_in(contract, require_module_root=True))
    loose = list(cu._iter_contract_defined_in(contract, require_module_root=False))
    assert strict == [], f"根级 src 在 require_module_root=True 下必须扫不到（病灶来源）: {strict}"
    assert len(loose) == 1, f"require_module_root=False 必须覆盖根级 src: {loose}"
    _mod, _fqn, _di = loose[0]
    assert _mod == "", f"根级 src 的模块根＝空串（根模块，合法单模块值），实得 {_mod!r}"
    assert _fqn == "com/x/system/SysUser.java", f"包限定键不对: {_fqn}"


# ── R1（hunter）：异常形状=「认不得」≠「真没有」——聚合计数一次 WARNING ──

def test_anomaly_shapes_warn_once(caplog):
    """非 list section / 非 dict 条目=上游契约畸形——静默跳过会让四消费者只看到
    「空契约」且零信号（helper 收敛前四份拷贝同样静默，本锁防复活）。合法条目照常产出。"""
    import logging
    with caplog.at_level(logging.WARNING):
        triples = list(cu._iter_contract_defined_in({
            "interfaces": "not-a-list",                                  # 异常 1
            "dtos": ["not-a-dict",                                       # 异常 2
                     {"name": "Ok", "defined_in":
                         "ruoyi-alarm/src/main/java/com/ruoyi/alarm/service/AlarmService.java"}],
        }))
    assert triples == [("ruoyi-alarm", "com/ruoyi/alarm/service/AlarmService.java",
                        "ruoyi-alarm/src/main/java/com/ruoyi/alarm/service/AlarmService.java")]
    warns = [r for r in caplog.records if "异常形状" in r.getMessage()]
    assert len(warns) == 1 and "2" in warns[0].getMessage(), \
        f"异常形状必须聚合一次 WARNING 且计数正确: {[r.getMessage() for r in caplog.records]}"
