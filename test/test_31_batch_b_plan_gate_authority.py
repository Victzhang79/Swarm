"""31 号文批B：plan 放行权威三条（A1-H1 死循环 / A1-H2 冤杀 / A1-H3 结构性失明）。

三条都在**规划期确定性闸**上，方向各不相同，故锁的形状也不同：

- **A1-H1（确定性死循环）**：`basename_symbol_match` 无条件先剥 `Impl` 再比 ⇒ 契约符号
  自身叫 `XxxImpl` 时匹配不上自己的文件 ⇒ C1 判无主 ⇒ 收尾器造【同路径】重复 create ⇒
  结构闸硬失败 ⇒ 打回 PLAN ⇒ 收尾器确定性重复同一步 ⇒ 烧穿 MAX_PLAN_RETRY（换模型也没用）。
- **A1-H2（冤杀，极性反转）**：`evaluate_probe_result` 判 `auth != "none"` 即 False，而
  生成侧/executable 集判定都认 bearer ⇒ bearer 断言被生成、被执行、证据被收割，最后以
  "auth 不是 none"这个与产品无关的理由判败 ⇒ 硬拦交付且归因到写者子任务。
- **A1-H3（结构性失明）**：③b/③f 用 `classpath_fqn_key`（要求物理模块根）当"是 JVM 类路径
  源码"的门控 ⇒ 根级 `src/main/java/...`（标准单模块 Spring Boot）恒返 None ⇒ 两闸在该布局
  上等于不存在。E2E 基线 RuoYi 是多模块，结构上不可能暴露这个面。

突变判据（逐个跑、每次 git status、突变后清 pyc）：
1. 删 `basename_symbol_match` 里新增的原样精确比较 ⇒ A1-H1 组全红。
2. `evaluate_probe_result` 的 auth 集改回 `!= "none"` ⇒ A1-H2 happy path 锁红。
3. 摘掉 verify.py 传的 `login_ok=` 实参 ⇒ A1-H2 端到端锁红（证接线，不只证函数）。
4. ③b / ③f 的 `jvm_classpath_ns_key` 换回 `classpath_fqn_key` ⇒ A1-H3 根级 src 组全红。
"""
from __future__ import annotations

import subprocess

import pytest

from swarm.brain.acceptance_spec import evaluate_probe_result
from swarm.brain.plan_validator import (
    _created_class_shadows_base,
    _cross_package_same_basename_creates,
    basename_symbol_match,
    unowned_contract_symbols,
    validate_contract_ownership,
    validate_module_coherence,
)
from swarm.types import FileScope, SubTask, TaskPlan

_J = "m/src/main/java/com/x/"


def _plan(create_by_st: dict[str, list[str]], descs: dict[str, str] | None = None) -> TaskPlan:
    """真 pydantic 模型建 plan。

    ★为什么不用 MagicMock/duck-typing★：`unowned_contract_symbols(plan, symbols)` 第一步是
    `getattr(plan, "subtasks", None) or []`，喂错类型会**静默 return []**＝空跑全绿。
    施治期实测踩过一次（31 号文 A1 报告的证据命令也踩了同一个：实参顺序反了 ⇒ 四个场景
    全是空跑），故这里一律用真模型。
    """
    subs = []
    for sid, files in create_by_st.items():
        subs.append(SubTask(
            id=sid,
            description=(descs or {}).get(sid, "实现业务逻辑（中文散文，刻意不含符号名）"),
            scope=FileScope(create_files=list(files), writable=[]),
            acceptance_criteria=["编译通过"],
        ))
    return TaskPlan(subtasks=subs, parallel_groups=[[s.id for s in subs]])


# ═══════════════════════ A1-H1：Impl 自匹配 ═══════════════════════

@pytest.mark.parametrize("stem,sym,want,why", [
    ("AlarmSenderImpl", "AlarmSenderImpl", 0, "★病灶本尊★符号自身以 Impl 结尾时必须匹配自己"),
    ("FooImpl", "FooImpl", 0, "同上，最小形态"),
    ("AlarmTaskServiceImpl", "AlarmTaskService", 0, "R42 原始意图：文件 Impl / 符号接口名"),
    ("FooImpl", "Foo", 0, "R42 最小形态"),
    ("Impl", "Impl", 0, "len<=4 不剥离的边界，治前治后都应 0"),
    ("Foo", "Foo", 0, "普通精确同名"),
    ("Bar", "FooImpl", -1, "真不相关必须仍 -1（区分力：别把闸放宽成恒匹配）"),
    ("IFooService", "FooService", 1, "I 前缀惯例等价仍 tier 1（未被新分支抢走）"),
    ("user_service", "UserService", 1, "P-M2 snake↔Camel 仍 tier 1"),
])
def test_a1h1_basename_symbol_match_tiers(stem, sym, want, why):
    got = basename_symbol_match(stem, sym)
    assert got == want, f"m({stem!r},{sym!r})={got}，应为 {want}——{why}"


def test_a1h1_impl_symbol_owns_its_own_file_end_to_end():
    """端到端：plan 已 create `<Sym>Impl.java`、契约硬键含 `<Sym>Impl`、desc **不含**该符号
    （语料通道刻意不命中，只留文件通道）⇒ C1 必须判 owned。

    ★夹具形状决定命题唯一性★：desc 里若出现符号名，语料通道会命中，这条测试就变成
    "语料通道能工作"而不是"文件通道能认 Impl 自名"——正是本 bug 逃逸至今的原因。
    """
    p = _plan({"st-1": [_J + "AlarmSenderImpl.java"]})
    assert unowned_contract_symbols(p, ["AlarmSenderImpl"]) == [], (
        "AlarmSenderImpl.java 必须认领符号 AlarmSenderImpl（治前 basename 通道返 -1 ⇒ 判无主）")
    sc = {"interfaces": [{"name": "AlarmSenderImpl", "module": "m",
                          "defined_in": _J + "AlarmSenderImpl.java"}]}
    res = validate_contract_ownership(p, sc)
    assert res.valid is True, f"C1 应放行，实得 issues={getattr(res, 'issues', None)}"


@pytest.mark.parametrize("stem,sym,want,why", [
    ("alarm_sender_impl", "AlarmSenderImpl", 1, "★复核 MEDIUM-1★snake_case 自名（Go/Py/Rust/Ruby）"),
    ("alarm-sender-impl", "AlarmSenderImpl", 1, "kebab-case 自名（JS 生态）"),
    ("alarm_task_service_impl", "AlarmTaskService", 1, "snake + R42 意图（剥离后词序列等）"),
    ("alarm_sender", "AlarmSender", 1, "P-M2 本体不受影响"),
    ("alarm_user_service", "UserService", -1, "装饰变体仍不认（豁免半径不得失控，F3 族）"),
])
def test_a1h1_snake_kebab_impl_self_match(stem, sym, want, why):
    """★多栈中立（纪律1）★ 治法只做 JVM 命名形态就是只治了一半。

    病灶同源、换了命名惯例：Impl 剥离在词序列通道【之前】改写 stem ⇒
    `alarm_sender_impl` 被剥成 `alarm_sender_` → ['alarm','sender']，
    而符号 `AlarmSenderImpl` 是 ['alarm','sender','impl'] ⇒ 恒 -1。
    危害比 JVM 那条**更安静**：非 JVM 栈没有 ③b/③f（按设计 JVM 门控），无闸兜底，
    且不撞同路径 ⇒ 结构闸放行 ⇒ 收尾器在同目录造第二个文件定义同一个类，
    T4 pin 把 defined_in 钉到幻影文件上 ⇒ 静默进交付。
    """
    got = basename_symbol_match(stem, sym)
    assert got == want, f"m({stem!r},{sym!r})={got}，应为 {want}——{why}"


def test_a1h1_snake_impl_end_to_end_no_phantom_file():
    """端到端：snake 栈上 `*_impl.py` 必须认领 `*Impl` 符号，收尾器不得造幻影文件。"""
    p = _plan({"st-1": ["m/svc/alarm_sender_impl.py"]})
    assert unowned_contract_symbols(p, ["AlarmSenderImpl"]) == [], (
        "alarm_sender_impl.py 必须认领 AlarmSenderImpl（治前 -1 ⇒ 判无主 ⇒ 造幻影）")


def test_a1h1_r42_intent_unchanged_end_to_end():
    """反向锁：R42 语义（契约=接口名、文件=Impl）必须逐字节不变。
    没有这条，把剥离整块删掉也能让上面那条绿（区分力）。"""
    p = _plan({"st-1": [_J + "AlarmTaskServiceImpl.java"]})
    assert unowned_contract_symbols(p, ["AlarmTaskService"]) == [], (
        "R42：AlarmTaskServiceImpl.java 必须仍认领接口符号 AlarmTaskService")


def test_a1h1_twin_symbols_each_own_their_file():
    """双胞胎消歧方向：契约同含 `Foo` 与 `FooImpl`、两文件都在计划 ⇒ 两符号各归其位。
    治前 FooImpl.java 剥离后只命中 Foo（与 Foo.java 抢同一符号）⇒ **FooImpl** 被报无主。"""
    p = _plan({"st-1": [_J + "AlarmSender.java", _J + "AlarmSenderImpl.java"]})
    assert unowned_contract_symbols(p, ["AlarmSender", "AlarmSenderImpl"]) == [], (
        "两文件都在计划时两符号都应 owned")


def test_a1h1_twin_reports_the_symbol_that_truly_lacks_a_file():
    """只有 Impl 文件在计划时，被报无主的必须是**接口**（真没有文件的那个），
    而不是 Impl。治前报的是 Impl＝把 LLM 指向错误的修复方向。"""
    p = _plan({"st-1": [_J + "AlarmSenderImpl.java"]})
    assert unowned_contract_symbols(p, ["AlarmSender", "AlarmSenderImpl"]) == ["AlarmSender"], (
        "应只报 AlarmSender（接口无文件）；报 AlarmSenderImpl 会让 LLM 去改一个已经正确的落点")


def test_a1h1_no_duplicate_create_from_domicile():
    """★死循环的闭环锁★：C1 判 owned 后，收尾器不得再为该符号造 create
    （治前造出与 st-1 **同路径**的 create ⇒ validate_plan_structure 硬失败 ⇒ 打回 ⇒
    收尾器确定性重复 ⇒ 烧穿 MAX_PLAN_RETRY）。"""
    from swarm.brain.plan_finisher import _domicile_contract_symbols
    p = _plan({"st-1": [_J + "AlarmSenderImpl.java"]})
    sc = {"interfaces": [{"name": "AlarmSenderImpl", "module": "m",
                          "defined_in": _J + "AlarmSenderImpl.java"}]}
    before = {st.id: list(st.scope.create_files) for st in p.subtasks}
    placed = _domicile_contract_symbols(p, sc, None, "实现告警发送")
    assert placed == {}, (
        f"符号已 owned，收尾器不该再安置它，实得 {placed}"
        "（治前安置会造出与 st-1 同路径的 create ⇒ 结构闸硬失败 ⇒ 确定性死循环）")
    after = {st.id: list(st.scope.create_files) for st in p.subtasks}
    paths = [f for fs in after.values() for f in fs]
    assert len(paths) == len(set(paths)), f"出现重复 create 路径（跨子任务同路径必冲突）: {paths}"
    assert len(p.subtasks) == len(before), (
        f"不应为已 owned 的符号新建子任务，子任务数 {len(before)}→{len(p.subtasks)}")


# ═══════════════════════ A1-H2：bearer 冤杀 ═══════════════════════

def _bearer_spec(status: list[int] | None = None) -> dict:
    return {"id": "as-1", "req_id": "r-1", "kind": "http_probe", "auth": "bearer",
            "request": {"method": "GET", "path": "/api/me"},
            "expect": {"status": status or [200]}}


def test_a1h2_bearer_happy_path_passes():
    """★病灶本尊★：登录成功 + HTTP 200 的 bearer 断言必须判 pass。
    治前返 `passed=False, reason="auth='bearer' != 'none'"`——一个与被测产品毫无关系的理由，
    却一路走到 gates 阻断 auto_accept。全仓治前**无一条 bearer 正向锁**（这正是它逃逸的原因）。"""
    res = evaluate_probe_result(_bearer_spec(), 200, "ok", login_ok=True)
    assert res["passed"] is True, f"bearer+200+登录成功必须 pass，实得 {res}"


def test_a1h2_bearer_real_failure_still_fails_conclusively():
    """区分力：真失败仍须结论性判败。没有这条，把 bearer 判成恒 pass 也能让上一条绿。"""
    res = evaluate_probe_result(_bearer_spec(), 500, "boom", login_ok=True)
    assert res["passed"] is False, f"bearer+500 必须结论性判败，实得 {res}"
    assert "500" in res["reason"], f"理由必须指向真实状态码而非 auth，实得 {res['reason']!r}"


@pytest.mark.parametrize("login_ok", [None, False])
def test_a1h2_bearer_without_login_confirmation_is_inconclusive(login_ok):
    """跨帧不变量收进本函数（fail-closed）：调用方未确认登录成功时，bearer 既不判 pass
    （登录坏时 401 可能被当成"符合期待"）也不判 fail（冤杀写者子任务）⇒ 三值的 None。"""
    res = evaluate_probe_result(_bearer_spec(), 200, "ok", login_ok=login_ok)
    assert res["passed"] is None, (
        f"login_ok={login_ok!r} 时 bearer 必须 inconclusive，实得 {res}")


def test_a1h2_manual_auth_never_green():
    """既有锁语义保持：manual 永不判 pass（治法收窄 auth 集时不得把 manual 一起放进去）。"""
    spec = _bearer_spec()
    spec["auth"] = "manual"
    assert evaluate_probe_result(spec, 200, "ok", login_ok=True)["passed"] is False
    spec2 = _bearer_spec()
    spec2["kind"] = "manual"
    assert evaluate_probe_result(spec2, 200, "ok", login_ok=True)["passed"] is False


def test_a1h2_auth_set_is_single_source_shared_with_generator():
    """生成侧与判定侧必须消费**同一个**常量。治前两侧各写字面量 ⇒ 生成侧认 bearer、
    判定侧判死 ⇒ 断言被生成、被执行、证据被收割，最后以无关理由判败。

    行为锁（非 getsource）：把 bearer 从可判定集移除的突变会让本条与生成侧同时红。
    """
    from swarm.brain.acceptance_spec import _AUTO_EVALUABLE_AUTH, assertion_to_probe_cmd
    assert set(_AUTO_EVALUABLE_AUTH) == {"none", "bearer"}, (
        f"可自动判定集漂移: {_AUTO_EVALUABLE_AUTH}")
    # 生成侧：bearer 必须真能生成执行片段（且带 Authorization 头）
    cmd = assertion_to_probe_cmd(_bearer_spec(), 8080)
    assert "Authorization" in cmd and "Bearer" in cmd, (
        f"生成侧必须为 bearer 拼出 Authorization 头，实得 {cmd[:200]!r}")


@pytest.mark.parametrize("login_mark,code,want_passed,want_failed", [
    ("__ACCEPT_LOGIN__:ok", 200, True, None),        # ★冤杀本尊：治前是 False★
    ("__ACCEPT_LOGIN__:ok", 500, False, True),       # 真失败照常判败
    ("__ACCEPT_LOGIN__:empty", 401, None, None),     # 登录 infra 坏 → inconclusive
])
def test_a1h2_end_to_end_accept_phase_verdict(login_mark, code, want_passed, want_failed):
    """★端到端接线锁★：证 `login_ok=` 实参真的被传下去了，而不只是函数本身能工作。
    突变判据：摘掉 verify.py 传的 login_ok 实参 ⇒ 第一行（登录 ok + 200）必红。"""
    from swarm.brain.nodes.verify import _accept_phase_verdict
    body = "Ym9vbQ==" if code >= 500 else "b2s="
    out = _accept_phase_verdict(
        [_bearer_spec()], {"auth_login_available": True}, "passed",
        f"{login_mark}\n__ACCEPT_RESULT__as-1__{code}\n__ACCEPT_BODY__as-1__{body}\n")
    assert out.get("acceptance_passed") is want_passed, (
        f"{login_mark} + {code} → acceptance_passed 应为 {want_passed}，实得 "
        f"{out.get('acceptance_passed')}；details={out.get('acceptance_details')}")
    assert bool(out.get("_failed")) is bool(want_failed), (
        f"_failed 应为 {want_failed}，实得 {out.get('_failed')}")


def test_a1h2_login_mark_absent_is_inconclusive_not_failure():
    """★复核 MEDIUM-1（缺席不可辨 → fail-closed）★

    原判据只认负标记 `:empty`，于是【两个标记都缺席】被算成"登录没失败" ⇒ login_ok=True
    ⇒ bearer 裸打 401 被判**结论性 fail** + `_failed=True`，归因写者子任务白烧重试。
    而缺席有两条真实产地，都不代表登录成功：
      ① `_need_login` 只在某条 bearer 的 assertion_to_probe_cmd **成功**时才置 True，
         全抛异常 ⇒ 登录段根本不插进脚本，而 auth_login_available 早已 True；
      ② 沙箱执行在登录段之后、断言段之前被截断。
    生成侧恒 echo `ok` 或 `empty` 之一 ⇒ 缺席≠成功。
    """
    from swarm.brain.nodes.verify import _accept_phase_verdict
    out = _accept_phase_verdict(
        [_bearer_spec()], {"auth_login_available": True}, "passed",
        "__ACCEPT_RESULT__as-1__401\n__ACCEPT_BODY__as-1__eA==\n")   # 无任何登录标记
    assert out.get("acceptance_passed") is None, (
        f"登录标记缺席时 bearer 必须 inconclusive（缺席≠成功），实得 {out.get('acceptance_passed')}")
    assert not out.get("_failed"), "绝不判 _failed（那会把 infra/生成缺陷归因到写者子任务）"
    assert out.get("_degraded") == "acceptance_skipped:login_mark_absent", (
        f"三态必须各自成账（判读的人据此分辨去查什么），实得 {out.get('_degraded')!r}")


def test_a1h2_login_states_are_distinguishable_in_the_ledger():
    """区分力：三态的 `_degraded` 值必须互不相同——否则"登录跑了但 token 空"与
    "登录段压根没跑"在账上不可分，判读的人会去查错地方。"""
    from swarm.brain.nodes.verify import _accept_phase_verdict
    G = {"auth_login_available": True}
    empty = _accept_phase_verdict([_bearer_spec()], G, "passed",
                                  "__ACCEPT_LOGIN__:empty\n__ACCEPT_RESULT__as-1__401\n"
                                  "__ACCEPT_BODY__as-1__eA==\n")
    absent = _accept_phase_verdict([_bearer_spec()], G, "passed",
                                   "__ACCEPT_RESULT__as-1__401\n__ACCEPT_BODY__as-1__eA==\n")
    infra = _accept_phase_verdict([_bearer_spec()], G, "passed",
                                  "__ACCEPT_LOGIN__:ok\n__ACCEPT_RESULT__as-1__000\n"
                                  "__ACCEPT_BODY__as-1__eA==\n")
    marks = {empty.get("_degraded"), absent.get("_degraded"), infra.get("_degraded")}
    assert len(marks) == 3, f"三态账必须互异，实得 {marks}"
    assert all(m for m in marks), f"三态都必须落账（None=缺席不可辨），实得 {marks}"


# ═══════════ A1-H3：③b/③f 对根级 src 单模块布局的结构性失明 ═══════════

def _git_repo(tmp_path, files: dict[str, str]) -> tuple[str, str]:
    """建真 git 仓 + 一次 commit，返回 (path, ref)。

    ★夹具必须是真 git 仓★：③f 经 `_base_tree_listing(project_path, base_ref)` 读 base 树，
    非 git 目录会走"真无 base 树"分支（返 None → 静默跳过）⇒ 测试变成"greenfield 不误伤"
    这条**另一个**命题，而不是"根级 src 能查出 shadow"。本仓已立档的假绿形态
    （非 git 目录测 git archive 路径）。
    """
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    run = lambda *a: subprocess.run(a, cwd=tmp_path, capture_output=True, text=True, check=True)  # noqa: E731
    run("git", "init", "-q")
    run("git", "add", "-A")
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    ref = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                         capture_output=True, text=True, check=True).stdout.strip()
    return str(tmp_path), ref


@pytest.mark.parametrize("prefix,label", [
    ("", "★根级 src 单模块（标准 Spring Boot；治前结构性零防护）★"),
    ("m/", "多模块（治前已能拦，治后必须不变）"),
])
def test_a1h3_cross_package_same_basename_detected_in_both_layouts(prefix, label):
    """③b：两个同名异包 create（Spring bean 名默认 simple name ⇒ 并存启动即
    ConflictingBeanDefinitionException，编译期与 L1 隔离编译都查不出）。

    治前 `classpath_fqn_key` 要求物理模块根 ⇒ 根级 src 恒返 None ⇒ 同一违例在多模块上
    REJECT、在根级 src 上放行。E2E 基线 RuoYi 是多模块，故该失明面历轮不可能暴露。
    """
    p = _plan({"st-1": [f"{prefix}src/main/java/com/x/a/AppConfig.java"],
               "st-2": [f"{prefix}src/main/java/com/x/b/AppConfig.java"]})
    assert bool(_cross_package_same_basename_creates(p)), f"③b 应命中——{label}"
    assert validate_module_coherence(p).valid is False, f"G1 应 REJECT——{label}"


@pytest.mark.parametrize("paths,why", [
    (["src/test/java/com/x/a/AppTests.java", "src/test/java/com/x/b/AppTests.java"],
     "test 布局豁免（每模块一份 ApplicationTests 是生态惯例）"),
    (["src/a/foo.go", "src/b/foo.go"], "非 JVM 天然豁免（栈中立）"),
    (["src/main/java/A.java", "src/main/java/B.java"],
     "默认包无包路径 → punt（jvm_ns_tail 判据，非本闸职责）"),
    (["m1/src/main/java/com/x/A.java", "m2/src/main/java/com/x/A.java"],
     "同 FQN 跨模块 = 1 个 distinct FQN → 交 ③(#110) 报账，此处结构性不重复报"),
])
def test_a1h3_cross_package_gate_does_not_overreach(paths, why):
    """冤杀反向锁：换谓词只放开"不要求模块根"这一点，其余豁免面逐条不变。
    没有这组，把门控改成恒 True 也能让上面那条绿（区分力）。"""
    p = _plan({f"st-{i}": [f] for i, f in enumerate(paths)})
    assert not _cross_package_same_basename_creates(p), f"③b 不应命中——{why}"


@pytest.mark.parametrize("prefix,create_sub,label", [
    ("", "src/main/java/com/x/tool/GenController.java",
     "★根级 src（round67c st-51 死型；治前原样放行）★"),
    ("m/", "m/src/main/java/com/x/tool/GenController.java", "多模块（治后不变）"),
])
def test_a1h3_created_class_shadows_base_detected_in_both_layouts(tmp_path, prefix, create_sub, label):
    """③f：create 的类 simple name 撞 base 树已存在的同名异路径类。
    round67c 实锤：st-51 create tool/GenController.java 撞 base generator/controller/
    GenController.java → @Controller bean 名默认 simple name → 启动即冲突（编译期查不出）。"""
    proj, ref = _git_repo(tmp_path, {
        f"{prefix}src/main/java/com/x/gen/GenController.java":
            "package com.x.gen;\npublic class GenController {}\n"})
    p = _plan({"st-1": [create_sub]})
    hit = _created_class_shadows_base(p, proj, ref)
    assert isinstance(hit, dict) and hit, f"③f 应命中——{label}，实得 {hit!r}"
    assert validate_module_coherence(p, project_path=proj, base_ref=ref).valid is False, (
        f"G1 应 REJECT——{label}")


def test_a1h3_shadow_gate_ignores_brand_new_class(tmp_path):
    """冤杀反向锁：与 base 无同名冲突的全新类必须放行。"""
    proj, ref = _git_repo(tmp_path, {
        "src/main/java/com/x/gen/GenController.java":
            "package com.x.gen;\npublic class GenController {}\n"})
    p = _plan({"st-1": ["src/main/java/com/x/tool/BrandNewThing.java"]})
    assert not _created_class_shadows_base(p, proj, ref), "全新类不得判 shadow"


def test_a1h3_shadow_gate_skips_same_path(tmp_path):
    """撞同路径＝规则0 已降级为 modify，不该到本闸（否则把"改既有文件"误报成 shadow）。"""
    proj, ref = _git_repo(tmp_path, {
        "src/main/java/com/x/gen/GenController.java":
            "package com.x.gen;\npublic class GenController {}\n"})
    p = _plan({"st-1": ["src/main/java/com/x/gen/GenController.java"]})
    assert not _created_class_shadows_base(p, proj, ref), "同路径不是 shadow"


def test_a1h3_shadow_gate_greenfield_no_base_tree():
    """greenfield（无 project_path/base_ref）→ 静默跳过，不误伤。"""
    p = _plan({"st-1": ["src/main/java/com/x/a/Foo.java"]})
    assert _created_class_shadows_base(p, None, None) == {}


@pytest.mark.parametrize("prefix,label", [
    ("", "根级 src 单模块"),
    ("m/", "多模块"),
])
def test_a1h3_solver_can_clear_the_gate_in_both_layouts(tmp_path, prefix, label):
    """★复核 HIGH-1（本 diff 自伤，最重的一条）★

    A1-H3 把 ③b/③f 两个【闸】换成不要求模块根的谓词，若不同时换它们的【解算器】与
    【预防台账】（仍锁 classpath_fqn_key ⇒ 根级 src 恒 None），净效果是：
    闸开始 REJECT，而确定性清闸通道**结构性不存在** ⇒ 打回 PLAN ⇒ LLM 重产 ⇒
    解算器仍失明 ⇒ 原样重犯 ⇒ 同签名两轮不收敛熔断 FAILED@PLAN。
    即修掉 A1-H1 一个确定性死循环的同一批，在另一类布局上新造一个。

    锁的形状必须是「解算器归位 ≥1 **且** G1 valid=True」——只断言闸命中的锁抓不到这条
    （闸确实命中了，问题在出口面）。原 35 条新锁一条都不会红，b6/b7 突变也碰不到解算器。
    """
    from swarm.brain.contract_utils import deconflict_same_name_cross_package_creates
    a = f"{prefix}src/main/java/com/x/util/AesUtils.java"
    b = f"{prefix}src/main/java/com/x/crypto/AesUtils.java"
    contract = {"interfaces": [
        {"name": "AesUtils", "module": prefix.rstrip("/") or "app", "defined_in": a}]}
    p = _plan({"st-1": [a], "st-2": [b]})
    p.shared_contract = contract

    merged = deconflict_same_name_cross_package_creates(p, contract)
    assert merged >= 1, (
        f"{label}：契约 defined_in 已给唯一权威 owner，层③解算器必须确定性归一；"
        f"归一=0 而闸会 REJECT ⇒ 打回后 LLM 无从修 ⇒ 确定性死循环")
    assert validate_module_coherence(p).valid is True, (
        f"{label}：解算器归位后 G1 必须放行（闸无由再报）")


def test_a1h3_shadow_solver_clears_gate_on_root_src(tmp_path):
    """③f 半边同理：file_plan 显式 action=modify（信号1）+ 契约声明 base 真身（信号3）
    ⇒ 解算器必须把幻觉 create 归位成 modify，两布局一致。"""
    from swarm.brain.contract_utils import deconflict_create_vs_base_modify_shadow
    base_p = "src/main/java/com/x/system/SysUser.java"
    halluc = "src/main/java/com/x/alarm/SysUser.java"
    proj, ref = _git_repo(tmp_path, {base_p: "package com.x.system;\npublic class SysUser {}\n"})
    p = _plan({"st-1": [halluc]})
    p.shared_contract = {"types": [{"name": "SysUser", "module": "app", "defined_in": base_p}]}

    moved = deconflict_create_vs_base_modify_shadow(
        p, [{"path": base_p, "action": "modify"}], proj, ref)

    assert moved >= 1, (
        "根级 src 上 ③f 解算器必须能归位（治前解算器锁 classpath_fqn_key ⇒ 恒 0 ⇒ "
        "闸 REJECT 无解 ⇒ 确定性死循环）")
    assert validate_module_coherence(p, project_path=proj, base_ref=ref).valid is True, (
        "归位后 G1 必须放行")


def test_a1h3_path_normalization_is_single_source(tmp_path):
    """★复核 MEDIUM-2★ ③f 内原有三套归一（base 索引存原文 / create 侧手写剥 './' /
    比对跨两套）。畸形路径下手写版与 tree 不等 ⇒ 撞同路径早返不触发 ⇒ 落 shadow 判定
    ⇒ 把【改同一个文件】误报成 create-vs-base shadow（冤杀）。

    夹具用带 './' 与尾斜杠的畸形形态——正是两套归一分歧的取值域。
    """
    base_p = "src/main/java/com/x/gen/GenController.java"
    proj, ref = _git_repo(tmp_path, {base_p: "package com.x.gen;\npublic class GenController {}\n"})
    for variant in (f"./{base_p}", base_p, f".//{base_p}"):
        p = _plan({"st-1": [variant]})
        hit = _created_class_shadows_base(p, proj, ref)
        assert not hit, (
            f"{variant!r} 归一后就是 base 既有路径（撞同路径＝规则0 已降级 modify），"
            f"不得报 shadow，实得 {hit!r}")


def test_a1h3_ns_key_shared_by_both_shadow_sides():
    """★同源锁★：③f 的 base 索引侧与 create 侧必须用**同一个**谓词。
    一侧换一侧不换 ⇒ 口径分叉（base 有命中而 create 判不出，或反之）比不换更坏。

    行为锁：构造 base 与 create 都是根级 src 的情形——只有两侧同源才可能命中。
    （若只换 base 侧，create 侧返 None ⇒ 不命中；只换 create 侧，base 索引为空 ⇒ 不命中。）
    """
    from swarm.brain.contract_utils import (
        classpath_fqn_key,
        jvm_classpath_ns_key,
        jvm_compilable_layout,
    )
    root = "src/main/java/com/x/gen/GenController.java"
    assert classpath_fqn_key(root) is None, "前提：旧谓词对根级 src 恒返 None（病灶来源）"
    assert jvm_classpath_ns_key(root) == "com/x/gen/GenController.java", (
        "新谓词必须给出包限定键（不要求模块根）")
    # 与既有单一事实源同口径（三者共用 _jvm_ns_tail，不可能分叉）
    for path in (root, "m/src/main/java/com/x/A.java", "src/a/foo.go",
                 "src/main/java/A.java", "src/main/resources/x.xml"):
        assert (jvm_classpath_ns_key(path) is not None) == jvm_compilable_layout(path), (
            f"{path}: jvm_classpath_ns_key 与 jvm_compilable_layout 判据分叉了")
