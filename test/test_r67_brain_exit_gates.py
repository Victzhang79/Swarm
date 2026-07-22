"""R67 大脑出口闸红测试（round67 task=64cb44ed 三路深读定案）。

T1（R67-1/R67-2 真根）：P5 dedupe_file_plan 按 basename 全局静默剪除——
  · 12 个 per-entity add/edit.html（Thymeleaf 每实体一套惯例）被当"同名重复"剪掉，
    6 实体新增/编辑页无人承接=运行期必炸（假完整，三层闸全盲）；
  · duty 域 3 个 Controller 剪错方向（存活侧路由前缀与模板/菜单硬编码不一致→3 页 404）。
  治本：P5 收权到【仅完全同路径】去重；跨路径同名不再静默剪，交下游确定性闸裁决
  （同 FQN → #110 REJECT + #101 契约权威消解；异 FQN 同名 → R67-T1b REJECT 打回带双路径反馈）。

T1b（R67-2 判据升级，round66 复盘遗留"同 basename 跨物理根或跨包"）：同名 JVM 源码类被
  多子任务在【不同包】各自 create = 疑似同一逻辑类的重复设计（Spring bean 名冲突/路由分裂/
  语义漂移温床）。无契约权威可确定性消解 → fail-closed 打回，绝不静默挑边（P5 旧行为反面）。

T2（backstop）：validate_file_plan_ownership 的分母经 normalized_file_plan_paths 与 P5 同源
  ——P5 剪谁、闸就看不见谁（审计跟着剪除者走=账被抹）。T1 收权后闸自动恢复视力：
  资源孪生件失主必须报 issue。
"""
from __future__ import annotations

from swarm.brain.plan_batch import dedupe_file_plan
from swarm.brain.plan_validator import (
    validate_file_plan_ownership,
    validate_module_coherence,
)
from swarm.types import FileScope, SubTask, TaskHarness, TaskPlan


def _fp(path: str, **kw) -> dict:
    return {"path": path, "action": "create", "responsibility": "r", **kw}


def _st(sid, *, create=None, writable=None, readable=None, depends=None, desc="d", lang="java"):
    return SubTask(
        id=sid, description=desc,
        scope=FileScope(writable=writable or [], create_files=create or [], readable=readable or []),
        harness=TaskHarness(language=lang), depends_on=depends or [],
    )


# ── T1a：P5 收权——跨路径同名绝不静默剪 ──────────────────────────────────────────
def test_t1a_resource_twins_survive_dedupe():
    """round67 实锤：6 实体 add/edit.html 被剪。资源文件目录即命名空间，同名≠重复。"""
    fp = [
        _fp("ruoyi-admin/src/main/resources/templates/alarm/task/add.html"),
        _fp("ruoyi-admin/src/main/resources/templates/alarm/robot/add.html"),
        _fp("ruoyi-admin/src/main/resources/templates/duty/strategy/add.html"),
    ]
    out = dedupe_file_plan(fp)
    assert len(out) == 3, [x["path"] for x in out]


def test_t1a_jvm_source_twins_survive_dedupe():
    """跨路径同名源码也不在 P5 静默剪（duty 域剪错方向实锤）——交 #110/#101/T1b 裁决。"""
    fp = [
        _fp("ruoyi-alarm/src/main/java/com/ruoyi/alarm/task/controller/AlarmTaskController.java"),
        _fp("ruoyi-admin/src/main/java/com/ruoyi/web/controller/alarm/AlarmTaskController.java"),
    ]
    out = dedupe_file_plan(fp)
    assert len(out) == 2, [x["path"] for x in out]


def test_t1a_exact_path_dup_still_removed():
    fp = [_fp("m/src/main/java/com/a/Foo.java"), _fp("m/src/main/java/com/a/Foo.java")]
    assert len(dedupe_file_plan(fp)) == 1


def test_t1a_per_module_manifests_all_kept():
    """P1-6 保障不回退：每模块清单天然多份。"""
    fp = [_fp("moduleA/pom.xml"), _fp("moduleB/pom.xml"),
          _fp("web/package.json"), _fp("admin/package.json")]
    assert len(dedupe_file_plan(fp)) == 4


# ── T1b：同名异 FQN 跨包 create → REJECT（无权威绝不静默挑边）─────────────────────
_ALARM_CTRL = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/task/controller/AlarmTaskController.java"
_ADMIN_CTRL = "ruoyi-admin/src/main/java/com/ruoyi/web/controller/alarm/AlarmTaskController.java"


def test_t1b_same_basename_cross_package_rejected():
    plan = TaskPlan(subtasks=[_st("st-a", create=[_ALARM_CTRL]),
                              _st("st-b", create=[_ADMIN_CTRL])])
    res = validate_module_coherence(plan)
    assert not res.valid, "同名异 FQN 跨包重复 create 未被拦截（round67 R67-2 复发）"
    assert any("AlarmTaskController" in i for i in res.issues), res.issues


def test_t1b_same_basename_same_package_single_module_ok():
    """同一文件只有一个创建者 → 不误伤。"""
    plan = TaskPlan(subtasks=[_st("st-a", create=[_ALARM_CTRL])])
    assert validate_module_coherence(plan).valid


def test_t1b_distinct_basename_ok():
    a = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/A.java"
    b = "ruoyi-admin/src/main/java/com/ruoyi/web/B.java"
    plan = TaskPlan(subtasks=[_st("st-a", create=[a]), _st("st-b", create=[b])])
    assert validate_module_coherence(plan).valid


def test_t1b_test_layout_same_basename_not_flagged():
    """每模块一个 ApplicationTests 是生态惯例（test classpath 每模块独立）——绝不误杀。"""
    a = "mod-a/src/test/java/com/a/ApplicationTests.java"
    b = "mod-b/src/test/java/com/b/ApplicationTests.java"
    plan = TaskPlan(subtasks=[_st("st-a", create=[a]), _st("st-b", create=[b])])
    assert validate_module_coherence(plan).valid, "test 布局同名类被误判"


def test_t1b_resources_same_basename_not_flagged():
    """资源文件（非类路径命名空间）同名合法——由 T1a 保留、不入 T1b 判定。"""
    a = "ruoyi-admin/src/main/resources/templates/alarm/task/add.html"
    b = "ruoyi-admin/src/main/resources/templates/alarm/robot/add.html"
    plan = TaskPlan(subtasks=[_st("st-a", create=[a]), _st("st-b", create=[b])])
    assert validate_module_coherence(plan).valid


def test_t1b_go_same_basename_not_flagged():
    """非 JVM：Go 同名文件跨模块合法（import 由模块限定）。"""
    plan = TaskPlan(subtasks=[
        _st("st-a", create=["svc-a/internal/handler/handler.go"], lang="go"),
        _st("st-b", create=["svc-b/internal/handler/handler.go"], lang="go")])
    assert validate_module_coherence(plan).valid


def test_t1b_same_fqn_not_double_reported():
    """同 FQN 跨模块已由 #110（③）报——T1b（③b）不重复报同一组（一事一账）。"""
    a = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/appkey/domain/AlarmAppSecret.java"
    b = "ruoyi-admin/src/main/java/com/ruoyi/alarm/appkey/domain/AlarmAppSecret.java"
    plan = TaskPlan(subtasks=[_st("st-a", create=[a]), _st("st-b", create=[b])])
    res = validate_module_coherence(plan)
    assert not res.valid
    hits = [i for i in res.issues if "AlarmAppSecret" in i]
    assert len(hits) == 1, f"同 FQN 组被 ③ 与 ③b 双重报账: {hits}"


# ── T2：ownership 闸恢复视力（P5 剪谁闸就瞎谁 → 收权后必须看见）────────────────────
def test_t2_unowned_resource_twin_reported():
    """round67 实锤复现：robot/add.html 在 file_plan 无人承接 → 必须报 issue（旧行为：
    P5 先把它剪出分母 → 闸静默放行 → 假完整直穿三层闸）。"""
    file_plan = [
        _fp("ruoyi-admin/src/main/resources/templates/alarm/task/add.html"),
        _fp("ruoyi-admin/src/main/resources/templates/alarm/robot/add.html"),
    ]
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["ruoyi-admin/src/main/resources/templates/alarm/task/add.html"]),
        _st("st-2", create=["ruoyi-admin/src/main/java/com/ruoyi/web/X.java"]),
    ])
    res = validate_file_plan_ownership(plan, file_plan)
    assert any("robot/add.html" in i for i in res.issues), \
        f"失主资源孪生件未被归属闸看见（账被 P5 抹掉）: {res.issues}"


def test_t2_all_owned_passes():
    file_plan = [
        _fp("ruoyi-admin/src/main/resources/templates/alarm/task/add.html"),
        _fp("ruoyi-admin/src/main/resources/templates/alarm/robot/add.html"),
    ]
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["ruoyi-admin/src/main/resources/templates/alarm/task/add.html"]),
        _st("st-2", create=["ruoyi-admin/src/main/resources/templates/alarm/robot/add.html"]),
    ])
    assert validate_file_plan_ownership(plan, file_plan).valid


# ── T3（R67-3）：跨簇路由双实现 REJECT ────────────────────────────────────────────
def _mk(sid, create, desc):
    return _st(sid, create=create, desc=desc)


_N_CTRL = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/notify/controller/NotifyController.java"
_O_CTRL = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/orchestration/controller/AlarmOrchestrationController.java"


def test_t3_cross_cluster_route_double_claim_rejected():
    """round67 R67-3 实锤：st-34-2 与 st-43 各自新建 Controller 重复声明 POST /notify/simple
    等 4 路由 → Spring ambiguous mapping 真启动必炸。规划期必须打回。"""
    plan = TaskPlan(subtasks=[
        _mk("st-34-2", [_N_CTRL], "新建 NotifyController，提供 POST /notify/simple、/notify/compose 接口"),
        _mk("st-43", [_O_CTRL], "新建 AlarmOrchestrationController，实现 POST /notify/simple、/notify/compose"),
    ])
    res = validate_module_coherence(plan)
    assert not res.valid, "跨簇路由双实现未被拦截"
    assert any("/notify/simple" in i for i in res.issues), res.issues


def test_t3_same_cluster_split_siblings_not_flagged():
    """deep-copy 拆分兄弟共享描述文本（st-38-1/st-38-3 互见对方路由）——同簇不判。"""
    a = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/schedule/controller/DutyStrategyController.java"
    b = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/schedule/controller/DutyHolidayPlanController.java"
    shared = "值班管理：/alarm/strategy、/alarm/holiday、/alarm/snapshot 三面 Controller 与视图"
    plan = TaskPlan(subtasks=[_mk("st-38-1", [a], shared), _mk("st-38-3", [b], shared)])
    assert validate_module_coherence(plan).valid, "同簇拆分兄弟被误判路由冲突"


def test_t3_consumer_reference_not_flagged():
    """消费者（渠道实现/SDK）在 desc 提到 API 路径但不 create 路由处理器文件——不判。"""
    svc = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/channel/service/SlackNotifyServiceImpl.java"
    plan = TaskPlan(subtasks=[
        _mk("st-34-2", [_N_CTRL], "新建 NotifyController，提供 POST /notify/apns/send_voip"),
        _mk("st-31-1", [svc], "Slack 渠道实现，服务 /notify/apns/send_voip 发送链路"),
    ])
    assert validate_module_coherence(plan).valid, "消费者引用路由被误判为双实现"


def test_t3_disjoint_routes_not_flagged():
    plan = TaskPlan(subtasks=[
        _mk("st-a", [_N_CTRL], "提供 POST /notify/simple"),
        _mk("st-b", [_O_CTRL], "提供 POST /orchestration/rules"),
    ])
    assert validate_module_coherence(plan).valid


# ── T4（R67-4/5）：消费边补齐——自然语言里的消费关系确定性成边 ─────────────────────
from swarm.brain.plan_finisher import (  # noqa: E402
    wire_described_dependency_tokens,
    wire_symbol_consumption_edges,
)


def test_t4a_described_st_dependency_becomes_edge():
    """round67 R67-4 实锤：st-48 描述明写"依赖 st-1"但 depends_on=[] → 首批必 BLOCKED。"""
    producer = _st("st-1", create=["pom.xml"])
    consumer = _st("st-48", create=["ruoyi-admin/src/main/java/com/ruoyi/web/C.java"],
                   desc="控制器注入发送记录 Service（依赖 st-1 的 pom 装配）")
    plan = TaskPlan(subtasks=[producer, consumer])
    added = wire_described_dependency_tokens(plan)
    assert "st-1" in (consumer.depends_on or []), added


def test_t4a_unknown_st_id_ignored():
    consumer = _st("st-2", desc="依赖 st-99 的产物")
    plan = TaskPlan(subtasks=[_st("st-1"), consumer])
    wire_described_dependency_tokens(plan)
    assert "st-99" not in (consumer.depends_on or [])


def test_t4a_cycle_guarded():
    a = _st("st-1", desc="依赖 st-2", depends=[])
    b = _st("st-2", depends=["st-1"])
    plan = TaskPlan(subtasks=[a, b])
    wire_described_dependency_tokens(plan)
    assert "st-2" not in (a.depends_on or []), "成环边不得添加"


def test_t4a_existing_edge_idempotent():
    a = _st("st-2", desc="依赖 st-1", depends=["st-1"])
    plan = TaskPlan(subtasks=[_st("st-1"), a])
    added = wire_described_dependency_tokens(plan)
    assert (a.depends_on or []).count("st-1") == 1
    assert not added


def test_t4b_symbol_reference_becomes_edge():
    """round67 R67-5 实锤：st-50-1 要注入 ISysGoogleAuthService（st-8-1 创建）但零边
    零 readable → worker 只能臆造签名。符号级缺边扫描补边+readable。"""
    svc = "ruoyi-system/src/main/java/com/ruoyi/system/service/ISysGoogleAuthService.java"
    producer = _st("st-8-1", create=[svc])
    consumer = _st("st-50-1", create=["ruoyi-admin/src/main/java/com/ruoyi/web/GoogleAuthController.java"],
                   desc="登录控制器调用 ISysGoogleAuthService 校验动态码")
    plan = TaskPlan(subtasks=[producer, consumer])
    wire_symbol_consumption_edges(plan)
    assert "st-8-1" in (consumer.depends_on or [])
    assert any(svc in r for r in (consumer.scope.readable or [])), "补边须同时补 readable 指向产物"


def test_t4b_ambiguous_creator_skipped():
    """同名符号多创建者 → 歧义不猜（护栏随 G2）。"""
    a = _st("st-a", create=["m1/src/main/java/com/a/FooService.java"])
    b = _st("st-b", create=["m2/src/main/java/com/b/FooService.java"])
    consumer = _st("st-c", desc="调用 FooService 完成计算")
    plan = TaskPlan(subtasks=[a, b, consumer])
    wire_symbol_consumption_edges(plan)
    assert not (consumer.depends_on or [])


def test_t4b_self_creator_not_self_edge():
    st = _st("st-a", create=["m/src/main/java/com/a/BarService.java"],
             desc="实现 BarService 主逻辑")
    plan = TaskPlan(subtasks=[st, _st("st-b")])
    wire_symbol_consumption_edges(plan)
    assert not (st.depends_on or [])


def test_t4b_cycle_guarded():
    svc = "m/src/main/java/com/a/BazService.java"
    producer = _st("st-p", create=[svc], depends=["st-c"])
    consumer = _st("st-c", desc="消费 BazService")
    plan = TaskPlan(subtasks=[producer, consumer])
    wire_symbol_consumption_edges(plan)
    assert "st-p" not in (consumer.depends_on or []), "成环边不得添加"


def test_t4b_plain_words_not_matched():
    """普通英文单词/单驼峰词（Controller/Thymeleaf）不构成符号引用——≥2 大写字母门槛。"""
    producer = _st("st-p", create=["m/src/main/java/com/a/Controller.java"])
    consumer = _st("st-c", desc="a Controller with Thymeleaf view")
    plan = TaskPlan(subtasks=[producer, consumer])
    wire_symbol_consumption_edges(plan)
    assert not (consumer.depends_on or [])


# ── T5a（R67-6）：拆分账重切——孩子不得继承指向兄弟分区文件的 readable/ua ──────────
def test_t5a_split_children_account_resliced():
    """round67 实锤：st-21 拆分簇整账 deep-copy 继承 → st-21-1（首批）的 ua 里躺着
    st-21-2..6 才会创建的 Mapper=账与序矛盾（seed 闸永久死等）+ 兄弟互读成环 40 边。
    治本：拆分时先剔全部兄弟分区文件，时序合法部分由 A1 按批序重新加回。"""
    from swarm.brain.planning_nodes import _split_oversized_by_files
    creates = [
        # 两个 Controller 锚点 → core 按特性拆成【平行 leaf】（fan-out，无先后序）
        "m/src/main/java/com/x/g/GroupController.java",
        "m/src/main/java/com/x/g/GroupService.java",
        "m/src/main/java/com/x/g/GroupMapper.java",
        "m/src/main/java/com/x/h/HolidayController.java",
        "m/src/main/java/com/x/h/HolidayService.java",
        "m/src/main/java/com/x/h/HolidayMapper.java",
    ]
    st = _st("st-21", create=list(creates),
             readable=["pom.xml"] + list(creates),         # 父 readable 混入自身 create（T4 布线污染形态）
             desc="值班域实现")
    st.scope.upstream_artifacts = list(creates)            # 父 ua 混入自身全部 create
    children = _split_oversized_by_files(st, max_files=4)
    assert len(children) >= 2, "夹具未触发拆分"
    by_id = {c.id: c for c in children}
    owner_of = {p: c.id for c in children for p in (c.scope.create_files or [])}
    for ch in children:
        own = set(ch.scope.create_files or []) | set(ch.scope.writable or [])
        dep_closure = set()
        stack = list(ch.depends_on or [])
        while stack:
            d = stack.pop()
            if d in dep_closure or d not in by_id:
                continue
            dep_closure.add(d)
            stack.extend(by_id[d].depends_on or [])
        upstream_files = {p for d in dep_closure for p in (by_id[d].scope.create_files or [])}
        for coll, label in ((set(ch.scope.upstream_artifacts or []), "ua"),
                            (set(ch.scope.readable or []), "readable")):
            bad = {p for p in coll if p in owner_of
                   and owner_of[p] != ch.id and p not in upstream_files}
            assert not bad, (
                f"{ch.id} 的 {label} 指向非上游兄弟的分区文件（账与序矛盾/互读成环源）: "
                f"{sorted(x.rsplit('/', 1)[-1] for x in bad)}")
        assert own.isdisjoint(set(ch.scope.upstream_artifacts or [])), \
            f"{ch.id} 的 ua 含自身 create（自我死等）"
        assert "pom.xml" in set(ch.scope.readable or []), "非兄弟分区的外部 readable（基线）不得被误剔"


# ── T6（R67-7）：baseline 申报分层证据闸 ─────────────────────────────────────────
from swarm.brain.baseline_candidates import baseline_claims_missing_evidence  # noqa: E402

_REQS = [
    {"id": "req-jwt", "text": "JWT登录/注销：无状态认证，注销后Token加入Redis黑名单。"},
    {"id": "req-dict", "text": "字典管理模块：字典与字典数据管理。"},
    {"id": "req-cfg", "text": "参数配置管理，支持 refreshCache 缓存刷新。"},
]
# 基线 vocab：有 token/redis/refreshcache/sysloginservice，无 jwt（round67 实锤形态）
_VOCAB = "sysloginservice token redistokencache refreshcache sysconfigcontroller"


def test_t6_generic_token_no_longer_masks_missing_capability():
    """R67-7 实锤：jwt 零命中但 token/redis 泛词命中→旧 any 语义放行假申报。新语义=无
    evidence 时全部判别 token 须命中。"""
    claims = [{"id": "req-jwt", "reason": "Shiro 会话等价满足"}]
    missing = baseline_claims_missing_evidence(claims, _REQS, _VOCAB)
    assert "req-jwt" in missing, "jwt 缺失被 token/redis 泛词掩护（any 语义假放行）"


def test_t6_all_tokens_hit_passes():
    claims = [{"id": "req-cfg", "reason": "SysConfigController.refreshCache 已有"}]
    assert baseline_claims_missing_evidence(claims, _REQS, _VOCAB) == []


def test_t6_evidence_field_verified_pass():
    """带 evidence 引文且在基线 vocab 真实命中 → 放行（逃生门实证而非口说）。"""
    claims = [{"id": "req-jwt", "reason": "登录鉴权由既有机制满足",
               "evidence": "SysLoginService"}]
    assert baseline_claims_missing_evidence(claims, _REQS, _VOCAB) == []


def test_t6_evidence_field_miss_rejected():
    claims = [{"id": "req-jwt", "reason": "有 JwtBlacklistFilter",
               "evidence": "JwtBlacklistFilter"}]
    assert "req-jwt" in baseline_claims_missing_evidence(claims, _REQS, _VOCAB)


def test_t6_pure_chinese_exempt_unchanged():
    claims = [{"id": "req-dict", "reason": "基线字典模块齐全"}]
    assert baseline_claims_missing_evidence(claims, _REQS, _VOCAB) == []


def test_t6_no_vocab_fail_open_unchanged():
    claims = [{"id": "req-jwt", "reason": "r"}]
    assert baseline_claims_missing_evidence(claims, _REQS, None) == []


# ── T7a（R67-8）：考卷矛盾泛化——desc 禁令 vs 注入依赖对账 REJECT ──────────────────
def test_t7a_universal_ban_vs_template_deps_rejected():
    """R67-8 实锤 st-1：文字禁"任何第三方运行时依赖，仅用 JDK 标准库"，紧随的权威 pom
    模板却注入 ruoyi-quartz/ruoyi-system → worker 怎么写都违反考卷一侧。"""
    desc = (
        "实现零依赖 SDK：不引入 Spring/Shiro/Lombok/任何第三方运行时依赖，仅用 JDK 标准库。\n"
        "【权威 pom 模板】\n<dependencies>\n"
        "<dependency><groupId>com.ruoyi</groupId><artifactId>ruoyi-quartz</artifactId></dependency>\n"
        "<dependency><groupId>com.ruoyi</groupId><artifactId>ruoyi-system</artifactId></dependency>\n"
        "</dependencies>")
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["sdk/src/main/java/com/ruoyi/sdk/AlarmClient.java"], desc=desc),
        _st("st-2", create=["m/src/main/java/com/a/B.java"])])
    res = validate_module_coherence(plan)
    assert not res.valid, "考卷自相矛盾（全称禁令 vs 模板注入依赖）未被拦截"
    assert any("st-1" in i and ("矛盾" in i or "禁" in i) for i in res.issues), res.issues


def test_t7a_specific_ban_only_conflicts_matching_artifact():
    """特定禁令（禁 Lombok）只与同名坐标冲突——模板注其它依赖不误判。"""
    desc = ("本模块禁止使用 Lombok。\n【权威 pom 模板】\n<dependencies>\n"
            "<dependency><groupId>com.ruoyi</groupId><artifactId>ruoyi-common</artifactId></dependency>\n"
            "</dependencies>")
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["m/src/main/java/com/a/A.java"], desc=desc),
        _st("st-2", create=["m2/src/main/java/com/b/B.java"])])
    assert validate_module_coherence(plan).valid, "特定禁令被误判为与无关依赖冲突"


def test_t7a_specific_ban_matching_artifact_rejected():
    desc = ("本模块禁止使用 Lombok，手写 getter。\n【权威 pom 模板】\n<dependencies>\n"
            "<dependency><groupId>org.projectlombok</groupId><artifactId>lombok</artifactId></dependency>\n"
            "</dependencies>")
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["m/src/main/java/com/a/A.java"], desc=desc),
        _st("st-2", create=["m2/src/main/java/com/b/B.java"])])
    assert not validate_module_coherence(plan).valid


def test_t7a_ac_coordinate_vs_jdk_only_rejected():
    """R67-8 st-8-1 形态：desc 要求仅用 JDK 手写 TOTP，AC 却强制声明 googleauth 坐标。"""
    st = _st("st-8-1", create=["m/src/main/java/com/a/Totp.java"],
             desc="仅用 JDK javax.crypto.Mac 手写 TOTP，不引入任何第三方运行时依赖。")
    st.acceptance_criteria = ["pom.xml 必须声明 com.warrenstrange:googleauth:1.5.0"]
    plan = TaskPlan(subtasks=[st, _st("st-2", create=["m2/src/main/java/com/b/B.java"])])
    assert not validate_module_coherence(plan).valid


def test_t7a_no_ban_no_reject():
    desc = ("正常实现。\n【权威 pom 模板】\n<dependencies>\n"
            "<dependency><groupId>com.ruoyi</groupId><artifactId>ruoyi-common</artifactId></dependency>\n"
            "</dependencies>")
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["m/src/main/java/com/a/A.java"], desc=desc),
        _st("st-2", create=["m2/src/main/java/com/b/B.java"])])
    assert validate_module_coherence(plan).valid


# ── T7b（R67-9）：负断言锚定扩 sibling——裸词禁令/BRE 交替分支 ─────────────────────
from swarm.brain.contract_utils import _anchor_forbidden_import_in_cmd  # noqa: E402


def test_t7b_bare_word_ban_on_java_target_anchored():
    """`! grep -rl 'lombok' <java目录>` 裸词命中注释"不使用 lombok"即假阳杀——须锚 import。"""
    cmd = "! grep -rl 'lombok' ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/"
    out = _anchor_forbidden_import_in_cmd(cmd)
    assert "import" in out and "lombok" in out, out
    assert out != cmd


def test_t7b_bare_word_ban_on_nonjava_target_untouched():
    """非 Java 源码目标（html/js）的裸词禁令不锚 import（语义不适用，fail-open）。"""
    cmd = "! grep -r 'vue' ruoyi-admin/src/main/resources/templates/alarm/"
    assert _anchor_forbidden_import_in_cmd(cmd) == cmd


def test_t7b_bre_alternation_pkg_branch_anchored():
    r"""BRE `\|` 交替分支此前按 `|` 硬切解析碎裂 → 逐支解析后包前缀支应被锚定。"""
    cmd = r"! grep -rE 'javax\.|lombok\.' m/src/main/java/"
    out = _anchor_forbidden_import_in_cmd(cmd)
    assert out.count("import") >= 2, out


def test_t7b_positive_assert_untouched():
    cmd = "grep -q 'import com.ruoyi.framework' m/src/main/java/A.java"
    assert _anchor_forbidden_import_in_cmd(cmd) == cmd


# ── T8（R67-10）：create 撞基线复校降 modify（规则0 逆向）─────────────────────────
def test_t8_create_hitting_base_tree_demoted_to_writable(monkeypatch):
    """R67-10 实锤：st-contract-generator 把基线已有 GenTable.java 当 create（覆写风险）；
    st-7-1/st-8-1 把既有模块 pom 入 create（口径漂移）。基线存在 → 降级 writable(modify)。"""
    import swarm.brain.contract_utils as cu
    base = ["ruoyi-generator/src/main/java/com/ruoyi/generator/util/GenTable.java",
            "ruoyi-quartz/pom.xml", "pom.xml"]
    monkeypatch.setattr(cu, "_base_tree_listing", lambda *a, **k: list(base))
    st = _st("st-c", create=[
        "ruoyi-generator/src/main/java/com/ruoyi/generator/util/GenTable.java",
        "ruoyi-generator/src/main/java/com/ruoyi/generator/util/NewThing.java"])
    plan = TaskPlan(subtasks=[st, _st("st-x", create=["m/src/main/java/com/a/B.java"])])
    cu.normalize_plan_scopes(plan, project_path="/fake", base_ref="HEAD")
    assert "ruoyi-generator/src/main/java/com/ruoyi/generator/util/GenTable.java" \
        not in (st.scope.create_files or []), "基线已有文件不得保留在 create_files（覆写风险）"
    assert "ruoyi-generator/src/main/java/com/ruoyi/generator/util/GenTable.java" \
        in (st.scope.writable or []), "应降级为 writable(modify)"
    assert "ruoyi-generator/src/main/java/com/ruoyi/generator/util/NewThing.java" \
        in (st.scope.create_files or []), "真新建不受影响"


def test_t8_no_base_tree_fail_open(monkeypatch):
    import swarm.brain.contract_utils as cu
    monkeypatch.setattr(cu, "_base_tree_listing", lambda *a, **k: None)
    st = _st("st-c", create=["m/src/main/java/com/a/A.java"])
    plan = TaskPlan(subtasks=[st, _st("st-x")])
    cu.normalize_plan_scopes(plan, project_path="/fake", base_ref="HEAD")
    assert "m/src/main/java/com/a/A.java" in (st.scope.create_files or [])


# ── T9（R67-13）：trivial×多源码文件 难度 bump sibling ────────────────────────────
def test_t9_trivial_many_source_creates_bumped():
    """R67-13 实锤 st-17：标 trivial 却 5 个 create 源码文件（实体簇）——trivial 单发路径
    塞不下多文件，低估会路由弱档白烧。≥3 个类路径源码 create → 提 MEDIUM。"""
    from swarm.brain.contract_utils import bump_scaffold_difficulty
    from swarm.types import SubTaskDifficulty
    st = _st("st-17", create=[f"m/src/main/java/com/x/E{i}.java" for i in range(5)])
    st.difficulty = SubTaskDifficulty.TRIVIAL
    plan = TaskPlan(subtasks=[st])
    n = bump_scaffold_difficulty(plan)
    assert st.difficulty == SubTaskDifficulty.MEDIUM, "trivial 多源码文件未被提档"
    assert n == 1


def test_t9_trivial_single_module_pom_stays_trivial():
    """R62-Task6 收窄不回退：模块 pom 单文件模板落盘=真 trivial。"""
    from swarm.brain.contract_utils import bump_scaffold_difficulty
    from swarm.types import SubTaskDifficulty
    st = _st("st-s", create=["mod-a/pom.xml"])
    st.difficulty = SubTaskDifficulty.TRIVIAL
    bump_scaffold_difficulty(TaskPlan(subtasks=[st]))
    assert st.difficulty == SubTaskDifficulty.TRIVIAL


def test_t9_trivial_two_files_stays_trivial():
    from swarm.brain.contract_utils import bump_scaffold_difficulty
    from swarm.types import SubTaskDifficulty
    st = _st("st-s", create=["m/src/main/java/com/x/A.java", "m/src/main/java/com/x/B.java"])
    st.difficulty = SubTaskDifficulty.TRIVIAL
    bump_scaffold_difficulty(TaskPlan(subtasks=[st]))
    assert st.difficulty == SubTaskDifficulty.TRIVIAL


# ── 对抗双复核整改回归（round67 收口）────────────────────────────────────────────
def test_review_t7a_soft_preference_not_contradiction():
    """复核 HIGH 整改：软偏好+显式例外（尽量仅用 JDK…如确有必要可少量引入）≠硬禁令。"""
    desc = ("实现 SDK：尽量仅用 JDK 标准库以降低耦合，如确有必要可少量引入基础依赖。\n"
            "【权威 pom 模板】\n<dependencies>\n"
            "<dependency><groupId>com.ruoyi</groupId><artifactId>ruoyi-quartz</artifactId></dependency>\n"
            "</dependencies>")
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["sdk/src/main/java/com/ruoyi/sdk/A.java"], desc=desc),
        _st("st-2", create=["m/src/main/java/com/a/B.java"])])
    assert validate_module_coherence(plan).valid, "软化措辞被误判硬禁令"


def test_review_t7a_hyphenated_ban_list_no_truncation():
    """回归实锤（plan_inject 夹具 st-26）：具名清单含连字符（ruoyi-common/...）不得截断成
    'ruoyi' 误配一切 ruoyi-* 坐标。"""
    desc = ("pom.xml 声明零 RuoYi 模块依赖（不引入 ruoyi-common/ruoyi-framework/ruoyi-system 等），"
            "仅引入 jackson-databind。\n【权威 pom 模板】\n<dependencies>\n"
            "<dependency><groupId>com.ruoyi</groupId><artifactId>ruoyi-alarm</artifactId></dependency>\n"
            "</dependencies>")
    plan = TaskPlan(subtasks=[
        _st("st-26", create=["iface/src/main/java/com/ruoyi/iface/A.java"], desc=desc),
        _st("st-2", create=["m/src/main/java/com/a/B.java"])])
    assert validate_module_coherence(plan).valid, "连字符清单被截断误配（st-26 假矛盾复发）"


def test_review_t3_single_route_overlap_not_rejected():
    """复核 HIGH 整改：handler 子任务 desc 引用他人单条既有路由（消费引用）≠双实现；
    仅同对跨簇共享 ≥2 条路由才 REJECT（真双实现整组端点相交）。"""
    a = _st("st-12", create=["m/src/main/java/com/x/SysConfigController.java"],
            desc="新建 SysConfigController 提供 GET /system/config/list")
    b = _st("st-34", create=["n/src/main/java/com/y/NotifyController.java"],
            desc="新建 NotifyController 提供 /notify/simple；启动时调用 /system/config/list 拉取开关")
    plan = TaskPlan(subtasks=[a, b])
    assert validate_module_coherence(plan).valid, "单路由消费引用被误判双实现"


def test_review_t7b_mixed_case_bare_word_anchored():
    r"""复核 MED 整改：`lombok\|Lombok` 大写支同锚（不锚则裸子串命中注释假阳杀）。"""
    from swarm.brain.contract_utils import _anchor_forbidden_import_in_cmd
    cmd = r"! grep -r 'lombok\|Lombok' ruoyi-alarm/src/main/java/com/ruoyi/alarm/"
    out = _anchor_forbidden_import_in_cmd(cmd)
    assert out.count("import") >= 2, out


def test_hunter_f2_resplit_children_ua_resliced(monkeypatch):
    """hunter F2：_resplit_subtask 子块不得继承指向兄弟待建文件的 ua（同 T5a 病族）。"""
    import asyncio
    import json as _json
    import swarm.brain.planning_nodes as pn
    creates = ["m/src/main/java/com/x/A.java", "m/src/main/java/com/x/B.java"]
    payload = {"subtasks": [
        {"description": "a", "create_files": [creates[0]], "writable_files": [],
         "readable_files": [], "est_context_tokens": 10000},
        {"description": "b", "create_files": [creates[1]], "writable_files": [],
         "readable_files": [], "est_context_tokens": 10000},
    ]}

    class _FakeResp:
        content = _json.dumps(payload, ensure_ascii=False)

    class _FakeLLM:
        async def ainvoke(self, *_a, **_k):
            return _FakeResp()

    monkeypatch.setattr(pn, "_get_brain_llm", lambda: _FakeLLM())
    st = _st("st-9", create=list(creates), desc="d")
    st.scope.upstream_artifacts = list(creates) + ["pom.xml"]
    children = asyncio.run(pn._resplit_subtask(st, {}, budget=40000))
    assert len(children) == 2, [c.id for c in children]
    for ch in children:
        ua = set(ch.scope.upstream_artifacts or [])
        assert not (ua & set(creates)), f"{ch.id} ua 含兄弟/自身待建文件: {ua & set(creates)}"
        assert "pom.xml" in ua, "非待建集的外部 ua 不得误剔"


def test_review2_t7a_softened_first_sentence_no_mask_later_hard_ban():
    """二轮复核 STILL-BROKEN 整改：前句软化命中不得掩护后句独立硬禁令（finditer 逐命中判）。"""
    desc = ("模块内部尽量仅用 JDK 完成简单工具函数（如确有必要可少量引入）。\n"
            "另：对外发布 SDK 零第三方依赖，任何第三方运行时依赖都不得出现。\n"
            "【权威 pom 模板】\n<dependencies>\n"
            "<dependency><groupId>com.ruoyi</groupId><artifactId>ruoyi-quartz</artifactId></dependency>\n"
            "</dependencies>")
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["sdk/src/main/java/com/ruoyi/sdk/A.java"], desc=desc),
        _st("st-2", create=["m/src/main/java/com/a/B.java"])])
    assert not validate_module_coherence(plan).valid, "后句硬禁令被前句软化命中掩护（短路回归）"


def test_review2_t7a_softened_specific_ban_not_contradiction():
    """hunter(b) 二轮整改：软偏好+具名依赖（尽量不使用 Lombok…可少量引入）≠硬矛盾。"""
    desc = ("本模块尽量不使用 Lombok，如确有必要可少量引入。\n【权威 pom 模板】\n<dependencies>\n"
            "<dependency><groupId>org.projectlombok</groupId><artifactId>lombok</artifactId></dependency>\n"
            "</dependencies>")
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["m/src/main/java/com/a/A.java"], desc=desc),
        _st("st-2", create=["m2/src/main/java/com/b/B.java"])])
    assert validate_module_coherence(plan).valid, "软化具名禁令被误判硬矛盾"


def test_review2_smoke_ambiguous_mapping_is_code_error():
    """hunter(a) 二轮整改：Spring Ambiguous mapping 必须判 code_error failed（此前被吞成
    skipped/inconclusive=③c 承诺的运行期兜底不存在）。"""
    from swarm.brain.nodes.runtime_smoke import classify_smoke_outcome
    res = classify_smoke_outcome(
        app_rc="exited",
        log_tail="IllegalStateException: Ambiguous mapping. Cannot map 'notifyController'",
        probe_sequence=[], language_key="java")
    assert res.status == "failed" and res.classification == "code_error", (
        res.status, res.classification)


def test_review2_route_weak_overlap_warned_not_silent():
    """hunter(a) 整改：单条相交降 warn 留痕（此前静默丢弃，docstring 承诺与实现不一致）。"""
    a = _st("st-12", create=["m/src/main/java/com/x/SysConfigController.java"],
            desc="新建 SysConfigController 提供 GET /system/config/list")
    b = _st("st-34", create=["n/src/main/java/com/y/NotifyController.java"],
            desc="新建 NotifyController 提供 /notify/simple；启动时调用 /system/config/list 拉取开关")
    res = validate_module_coherence(TaskPlan(subtasks=[a, b]))
    assert res.valid
    assert any("/system/config/list" in w for w in res.warnings), res.warnings
