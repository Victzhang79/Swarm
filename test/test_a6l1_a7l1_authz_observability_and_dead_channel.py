"""32 号文 A6-L1 + A7-L1。

## A6-L1 权限链四处吞异常零日志（极性正确，缺可观测性）

`api/routers/sandbox.py` 三处（`_sandbox_owner_info` / `_task_creator` /
`_can_see_sandbox` 的成员角色查询）+ 同族第四处 `api/routers/project.py`
`_caller_may_reuse_existing_project`。四处**极性全部正确**（DB 挂时拒绝而非放行），
缺陷纯在可观测性：PG 抖动 / `get_task` 超时时，项目成员对**自己创建的**沙箱吃 403，
而服务端日志**零线索**——403 文案说"仅管理员/项目管理员/任务创建者可访问"，与真因
"DB 查不到创建者"毫无关系 ⇒ 运维会去查 RBAC 配置，而根因在 DB 连接。

★治法的区分力是核心，不是"加个日志"那么简单★
每一处的"失败取值"都与一个**完全正常的情形**取值相同：
| 落点 | 故障态 | 与之同值的**常态**（绝不能告警） |
|---|---|---|
| `_sandbox_owner_info` | 列表拉不到 → `(None,None)` | 沙箱本来无归属（走 try 外 return） |
| `_task_creator` | `get_task` 抛 → `None` | 任务本来没 `created_by_user_id`（try 内正常 return） |
| `_can_see_sandbox` | 角色查询抛 → `None` | **合法的非该项目成员**（同一行取值一模一样） |
故告警必须**只在 except 臂**。第三处最险：故障与"非成员"在下一行取值完全相同，
不在异常臂留痕的话，两者在日志上永远不可辨（同 A7-M1「第三个原因＝正常情形，刻意
不记键不打日志」的判据，那条正是本仓的区分力范例）。

## A7-L1 `_QUOTA_MARKER_RES` 死通道

恒空 tuple + 活着的消费者 `any(r.search(first) for r in ...)` ⇒ 分支恒不执行。
删两处；★并把"为何不加词判"的论证从**常量**上移到**函数体里有人会想加判据的那个位置**★
——findings 的核心担忧正是"论证挂在常量上、下一个维护者看的是函数体"。
"""
from __future__ import annotations

import logging

import pytest

_SBX = "swarm.api.routers.sandbox"


class _User:
    def __init__(self, uid="u1", role="developer"):
        self.id = uid
        self.global_role = role


@pytest.fixture(autouse=True)
def _clean_degrade():
    from swarm.infra.degrade import reset_degrade_counts

    reset_degrade_counts()
    yield
    reset_degrade_counts()


def _degrade_keys(prefix: str) -> list[str]:
    from swarm.infra.degrade import degrade_counts

    return [k for k in degrade_counts() if k.startswith(prefix)]


# ══════════ A6-L1 ① _sandbox_owner_info：拉列表失败必须留痕 ══════════

def test_owner_info_failure_is_observable(monkeypatch, caplog):
    """服务端列表拉不到 → WARNING + 机读键（此前 `pass` 零线索）。"""
    import swarm.api.routers.sandbox as sbx

    class _Mgr:
        def get_sandbox_meta(self, _sid):
            return {}

    def _boom():
        raise RuntimeError("sandbox api unreachable")

    # 打在 sandbox.py **自己持有的** _app 引用上：`import swarm.api.app as _app` 在测试
    # 上下文里会取到被包 __init__ 遮蔽的 FastAPI 实例（非模块），patch 到那上面不生效
    monkeypatch.setattr(sbx._app, "_fetch_sandbox_list_from_server", _boom)
    with caplog.at_level(logging.WARNING):
        got = sbx._sandbox_owner_info(_Mgr(), "sb-1")

    assert got == (None, None), f"极性必须不变（归属未知⇒仅 admin 可见）。实得 {got}"
    assert any("归属查询失败" in r.getMessage() for r in caplog.records), \
        f"必须留 WARNING，实得 {[r.getMessage()[:60] for r in caplog.records]}"
    assert _degrade_keys("api.sandbox.owner_info_lookup_failed"), \
        "必须有机读键（WARNING 只能人读且要有人正好在看）"


def test_owner_info_normal_absence_is_silent(monkeypatch, caplog):
    """★区分力锁★ 沙箱**本来无归属**（列表正常但没这条）→ 零告警。

    没有这条，"常态被刷成告警"就无人拦——而那会让真故障淹在噪声里。
    """
    import swarm.api.routers.sandbox as sbx

    class _Mgr:
        def get_sandbox_meta(self, _sid):
            return {}

    monkeypatch.setattr(sbx._app, "_fetch_sandbox_list_from_server",
                        lambda: [{"id": "other"}])
    with caplog.at_level(logging.WARNING):
        got = sbx._sandbox_owner_info(_Mgr(), "sb-1")

    assert got == (None, None)
    assert not [r for r in caplog.records if "归属查询失败" in r.getMessage()], \
        "列表正常、只是没这条沙箱＝常态，绝不能告警"
    assert not _degrade_keys("api.sandbox.owner_info_lookup_failed")


# ══════════ A6-L1 ② _task_creator：三级权限第三级依据 ══════════

def test_task_creator_db_failure_is_observable(monkeypatch, caplog):
    """`get_task` 抛 → WARNING + 机读键。

    这是三处里最会咬人的一处：第三级权限依据恰好是这个值 ⇒ PG 抖动时项目成员对
    **自己创建的**沙箱吃 403。
    """
    import swarm.api.routers.sandbox as sbx
    import swarm.project.store as store

    def _boom(_tid):
        raise RuntimeError("PG connection refused")

    monkeypatch.setattr(store, "get_task", _boom)
    with caplog.at_level(logging.WARNING):
        got = sbx._task_creator("t-1")

    assert got is None, "极性必须不变（创建者未知⇒fail-closed）"
    assert any("任务创建者查询失败" in r.getMessage() for r in caplog.records), \
        f"实得 {[r.getMessage()[:60] for r in caplog.records]}"
    assert _degrade_keys("api.sandbox.task_creator_lookup_failed")


def test_task_creator_legit_missing_field_is_silent(monkeypatch, caplog):
    """★区分力锁★ 任务**本来没有** created_by_user_id（历史行/系统任务）→ 零告警。"""
    import swarm.api.routers.sandbox as sbx
    import swarm.project.store as store

    monkeypatch.setattr(store, "get_task", lambda _tid: {"id": "t-1"})
    with caplog.at_level(logging.WARNING):
        got = sbx._task_creator("t-1")

    assert got is None
    assert not [r for r in caplog.records if "任务创建者查询失败" in r.getMessage()], \
        "字段本来就没有＝常态（历史行/系统任务），绝不能告警"
    assert not _degrade_keys("api.sandbox.task_creator_lookup_failed")


def test_task_creator_no_task_id_is_silent(caplog):
    """无 task_id（无归属沙箱）→ 早退，零告警。"""
    import swarm.api.routers.sandbox as sbx

    with caplog.at_level(logging.WARNING):
        assert sbx._task_creator(None) is None
    assert not caplog.records, f"早退路径不该有任何日志：{[r.getMessage() for r in caplog.records]}"


# ══════════ A6-L1 ③ _can_see_sandbox：最险的那处区分力 ══════════

def test_member_role_db_failure_is_observable(monkeypatch, caplog):
    """★本批区分力核心★ 角色查询抛 → WARNING + 机读键，且判定仍 False。

    ★为什么这处最险★ 异常后 `member_role = None` 与"**合法的**非该项目成员"在下一行
    取值**完全相同** ⇒ 不在异常臂留痕，两种情形在日志上永远不可辨。
    """
    import swarm.api.routers.sandbox as sbx
    import swarm.auth.store as auth_store

    def _boom(_pid, _uid):
        raise RuntimeError("PG timeout")

    monkeypatch.setattr(auth_store, "get_project_member_role", _boom)
    with caplog.at_level(logging.WARNING):
        allowed = sbx._can_see_sandbox(_User(), "p-1", "u1")

    assert allowed is False, "★极性必须不变★ DB 挂时拒绝而非放行（fail-closed）"
    assert any("项目成员角色查询失败" in r.getMessage() for r in caplog.records), \
        f"实得 {[r.getMessage()[:60] for r in caplog.records]}"
    assert _degrade_keys("api.sandbox.member_role_lookup_failed")


def test_legit_non_member_is_silent(monkeypatch, caplog):
    """★区分力锁·与上一条成对★ 合法非成员被拒＝常态 → 零告警。

    这一对是本批最重要的配对：两条断言的**返回值完全相同**（都 False），
    唯一差别是日志/机读键。若治法把告警写在 `if member_role is None` 那一行
    （而非 except 臂内），这条必红。
    """
    import swarm.api.routers.sandbox as sbx
    import swarm.auth.store as auth_store

    monkeypatch.setattr(auth_store, "get_project_member_role", lambda _p, _u: None)
    with caplog.at_level(logging.WARNING):
        allowed = sbx._can_see_sandbox(_User(), "p-1", "u1")

    assert allowed is False, "非成员本就该被拒"
    assert not [r for r in caplog.records if "项目成员角色查询失败" in r.getMessage()], \
        "★合法非成员是常态★ 告警必须只在 except 臂，否则每个越权探测都刷 WARNING"
    assert not _degrade_keys("api.sandbox.member_role_lookup_failed")


def test_admin_and_owner_paths_unaffected(monkeypatch, caplog):
    """admin / 项目 owner 放行路径不受影响（观测改动不碰判定）。"""
    import swarm.api.routers.sandbox as sbx
    import swarm.auth.store as auth_store
    from swarm.auth.rbac import Role

    with caplog.at_level(logging.WARNING):
        assert sbx._can_see_sandbox(_User(role=Role.ADMIN.value), None, None) is True
        monkeypatch.setattr(auth_store, "get_project_member_role",
                            lambda _p, _u: Role.OWNER.value)
        assert sbx._can_see_sandbox(_User(), "p-1", None) is True
    assert not caplog.records, f"放行路径不该有告警：{[r.getMessage() for r in caplog.records]}"


# ══════════ A6-L1 ④ 同族第四处：project 复用鉴权 ══════════

def test_project_reuse_authz_failure_is_observable(monkeypatch, caplog):
    """`_caller_may_reuse_existing_project` 的 DB 抖动臂同样留痕。

    ★这处是 findings 自己列的"同族"，不在它点名的三处里★——本仓纪律「修一类问题先全仓
    捞 sibling」，四处同批治，否则下轮又是一条"补一个漏一个"。
    """
    import swarm.api.routers.project as proj
    import swarm.auth.store as auth_store

    def _boom(_pid, _uid):
        raise RuntimeError("PG down")

    monkeypatch.setattr(auth_store, "get_project_member_role", _boom)
    with caplog.at_level(logging.WARNING):
        allowed = proj._caller_may_reuse_existing_project(_User(), "p-1")

    assert allowed is False, "★极性必须不变★（跨用户项目劫持防线，fail-closed）"
    assert any("复用鉴权的成员角色查询失败" in r.getMessage() for r in caplog.records), \
        f"实得 {[r.getMessage()[:70] for r in caplog.records]}"
    assert _degrade_keys("api.project.reuse_member_role_lookup_failed")


def test_project_reuse_legit_non_member_is_silent(monkeypatch, caplog):
    """★区分力★ 合法非成员 → 零告警（与上一条同返回值，只差留痕）。"""
    import swarm.api.routers.project as proj
    import swarm.auth.store as auth_store

    monkeypatch.setattr(auth_store, "get_project_member_role", lambda _p, _u: None)
    with caplog.at_level(logging.WARNING):
        assert proj._caller_may_reuse_existing_project(_User(), "p-1") is False
    assert not [r for r in caplog.records if "复用鉴权" in r.getMessage()]
    assert not _degrade_keys("api.project.reuse_member_role_lookup_failed")


def test_degrade_helper_never_breaks_authz(monkeypatch):
    """★observability 绝不反噬鉴权★ 计数面炸掉时权限判定仍须正常返回。

    方向刻意与被观测的三处相反：那三处是 fail-closed（拒绝），这个 helper 是
    fail-safe（吞异常）——计数失败绝不能让鉴权崩成 500。
    """
    import swarm.api.routers.sandbox as sbx
    import swarm.infra.degrade as dg

    def _boom(_c):
        raise RuntimeError("counter exploded")

    monkeypatch.setattr(dg, "record_degrade", _boom)
    sbx._degrade("whatever")          # 不得抛

    # 且真实鉴权路径在计数面炸掉时仍正常 fail-closed
    import swarm.auth.store as auth_store
    monkeypatch.setattr(auth_store, "get_project_member_role",
                        lambda _p, _u: (_ for _ in ()).throw(RuntimeError("db")))
    assert sbx._can_see_sandbox(_User(), "p-1", "u1") is False


# ══════════ A7-L1 死通道 ══════════

def test_dead_quota_regex_channel_removed():
    """恒空常量与它的消费者都必须消失（留一个＝残骸照旧误导）。"""
    import swarm.models.key_rotation as kr

    assert not hasattr(kr, "_QUOTA_MARKER_RES"), (
        "_QUOTA_MARKER_RES 回潮＝恒空 tuple + 恒不执行的消费者，读代码的人会以为"
        "还有一条正则通道并往里加词，而那条通道当年是被复核证伪掉的（见 "
        "is_quota_shaped_error 函数体内的论证）"
    )


@pytest.mark.parametrize("text,want", [
    # 真配额/限流形态（必须仍判 True）
    ("insufficient_quota", True),
    ("Error: quota exceeded for organization", True),
    ("HTTP 429 Too Many Requests", True),
    ("余额不足，请充值", True),
    # ── 三道排除（必须仍判 False）──
    # ★★这四条必须让【两种信号同现】，否则判据 vacuous★★
    # 首跑教训：原语料只有 `invalid_api_key provided`，而它后面三道肯定判据**一个都不
    # 命中** ⇒ 把凭据排除整块删掉它照旧返 False ⇒ 突变全绿、判据实际零覆盖（本批 ①c
    # 「判据前件不成立＝看起来覆盖了其实没有」的同型，突变实验当场照出来）。
    # 真形态：openai SDK 把整个响应体压在首行 ⇒ 凭据错与 429/quota 字样**同时出现**，
    # 那才是"三道排除必须前置于肯定判据"这条设计真正在解决的场景。
    ("HTTP 429 invalid_api_key", False),              # 凭据排除必须压过 429
    ("401 invalid api key: quota exceeded", False),   # 凭据排除必须压过 quota 词
    ("connection reset by peer, 429", False),         # 瞬时排除必须压过 429
    ("read timeout=429", False),                      # 同上（findings 记的实测误判样本）
    # ★复核实测语料：这些**正常**错误曾被裸词判误伤，误判代价=冷却健康 key 6 小时★
    ("L1 gate failed: rate limit config test in RateLimitTest", False),
    ("BillingServiceImpl.java: cannot find symbol", False),
    ("model credit-scoring-7b not found", False),
    ("dependency com.example:credit-core:1.0 unresolved", False),
    ("用户配额管理模块生成失败", False),
])
def test_quota_shaped_verdicts_unchanged_after_dead_code_removal(text, want):
    """★行为不变锁★ 删死通道不得改任何判定。

    语料直接取自 findings 记录的**复核实测**误判样本（7/11 条正常错误被裸词判命中），
    不是我另造的——这些正是"判宽代价不对称"的证据本体。
    """
    from swarm.models.key_rotation import is_quota_shaped_error

    assert is_quota_shaped_error(text) is want, (
        f"判定变了：{text!r} 期望 {want}。删死代码绝不能改判定；若确要改，"
        f"先读 is_quota_shaped_error 函数体里那段代价不对称的论证"
    )
