"""R65D-T2（round65d 主治之一）：plan 子任务自洽闸——考卷必须与权威模板同源。

round65d 死因链第①层（三路交叉印证定案，task b583df8f st-26）：
- 10:25:05 第一遍 T5 从 readable 证据单向推出 interface→alarm（反向依赖）→ R58-3
  把含 `com.ruoyi:ruoyi-alarm` 的权威模板烤进 st-26 description；
- 10:39:44 第二遍 T5 推导结果里该边已消失，但 R58-3 幂等守卫「描述里已有模板→跳过」
  把第一遍的陈旧毒模板冻结（MODIFY 形态守卫更是完全失效：st-42 铁律块被重复追加×2）；
- verify_commands（LLM 按自己的 description 写：grep jackson/httpclient）与
  acceptance#4（规则5 用契约 arts 确定性追加）与模板（契约∪T5 合并）三个来源从无对账
  → worker 徒手写出 jackson∪okhttp 并集 pom（矛盾卷唯一最优解）被 H1 模板覆写销毁，
  再被自家旧考卷 `grep -q jackson` 杀死 → HANDLE_FAILURE 掉账 → 94 任务饿死全场。

治本四刀（本文件锁定 brain 侧三刀；worker 侧 H1 同源见 test_r65d_t2_h1_exam_source.py）：
① 模板 upsert：重注入时刷新陈旧模板块（CREATE 权威模板/MODIFY 铁律+片段两形态都幂等替换）；
② T5 推导边方向校验：目标模块 pom 生产者在消费者下游（新模块 pom 尚不存在且拓扑上
   后于消费者落地）→ 该边注定解析不了/成环 → 剪除 + fail-loud WARNING；
③ reconcile_template_exam：凡 description 带【权威 pom 模板·原样写入】的子任务，
   针对该 pom 的内容断言（grep / test -z "$(grep…)"）一律以模板为真值重写——
   旧断言剔除（与模板矛盾者 WARNING 留痕）、模板依赖逐条生成确定性断言、
   规则5 机器验收行改写为模板依赖清单、追加"模板即真值"权威验收行。
   接线唯一咽喉=inject_build_scaffold_subtasks 末端（两遍注入+外科重试路径全覆盖）。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from swarm.brain.contract_utils import (
    derive_internal_module_deps,
    inject_build_scaffold_subtasks,
)
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskHarness, TaskPlan

_ROOT_POM = (
    '<?xml version="1.0"?><project><groupId>com.ruoyi</groupId>'
    "<artifactId>ruoyi</artifactId><version>4.8.3</version>"
    "<packaging>pom</packaging>"
    "<modules><module>ruoyi-common</module></modules></project>")

_COMMON_POM = (
    '<?xml version="1.0"?><project>'
    "<parent><groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
    "<version>4.8.3</version></parent>"
    "<artifactId>ruoyi-common</artifactId></project>")

_BASE_ENTITY = "ruoyi-common/src/main/java/com/ruoyi/common/core/domain/BaseEntity.java"


def _st(sid, *, create=None, writable=None, readable=None, desc=None,
        depends=None, harness=None, acceptance=None):
    kw = {"harness": harness} if harness is not None else {}
    return SubTask(id=sid, description=desc or f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(create_files=create or [], writable=writable or [],
                                   readable=readable or []),
                   depends_on=depends or [],
                   acceptance_criteria=acceptance or [], **kw)


def _plan(subs, deps_entries):
    plan = TaskPlan(subtasks=subs, parallel_groups=[[s.id for s in subs]])
    plan.shared_contract = {"dependencies": deps_entries}
    return plan


def _mk_repo(tmp_path, extra=None):
    (tmp_path / "pom.xml").write_text(_ROOT_POM, encoding="utf-8")
    (tmp_path / "ruoyi-common").mkdir()
    (tmp_path / "ruoyi-common" / "pom.xml").write_text(_COMMON_POM, encoding="utf-8")
    for name, pom_text in (extra or {}).items():
        (tmp_path / name).mkdir()
        (tmp_path / name / "pom.xml").write_text(pom_text, encoding="utf-8")
    return str(tmp_path)


_STALE_AUTH_BLOCK = (
    "\n【权威 pom 模板（确定性生成，原样写入 mod-a/pom.xml；parent 版本必须是**字面量**，"
    "绝不可写成 ${...} 属性引用——历史遍产物）】\n```xml\n"
    '<?xml version="1.0" encoding="UTF-8"?>\n<project>\n    <dependencies>\n'
    "        <dependency>\n            <groupId>com.ruoyi</groupId>\n"
    "            <artifactId>stale-phantom-dep</artifactId>\n"
    "        </dependency>\n    </dependencies>\n</project>\n```")


# ───────────────────── ① 模板 upsert：陈旧模板刷新 ─────────────────────

def test_stale_owner_template_refreshed_on_reinject(tmp_path):
    """★st-26 死因本体★：owner 描述里已有第一遍的陈旧模板（含幻影依赖），第二遍
    注入必须【刷新】而不是跳过——终版推导才是真值，绝不让 pass-1 毒模板冻结上车。"""
    proj = _mk_repo(tmp_path)
    owner = _st("st-o", create=["mod-a/pom.xml",
                                "mod-a/src/main/java/com/x/A.java"],
                readable=[_BASE_ENTITY],
                desc="创建 mod-a 模块脚手架。" + _STALE_AUTH_BLOCK)
    plan = _plan([owner], [{"module": "mod-a", "artifacts": []}])
    inject_build_scaffold_subtasks(plan, proj)
    assert owner.description.count("【权威 pom 模板") == 1, \
        f"模板块必须 upsert 不追加，got:\n{owner.description}"
    assert "stale-phantom-dep" not in owner.description, \
        "陈旧模板（第一遍幻影依赖）必须被终版模板替换"
    assert "<artifactId>ruoyi-common</artifactId>" in owner.description, \
        "终版模板应含本遍 T5 推导出的内部依赖"


def test_modify_form_iron_rule_not_duplicated(tmp_path):
    """st-42 实锤：既有 pom（MODIFY 形态）第二遍注入盲目追加 → 铁律块×2 片段块×2。
    守卫只认「权威 pom 模板」字样，MODIFY 块不含该字样=守卫完全失效。必须 upsert。"""
    mod_pom = ('<?xml version="1.0"?><project>'
               "<parent><groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
               "<version>4.8.3</version></parent>"
               "<artifactId>mod-b</artifactId></project>")
    proj = _mk_repo(tmp_path, extra={"mod-b": mod_pom})
    stale_iron = (
        "\n【既有 pom 修改铁律（mod-b/pom.xml 已存在）】只做最小增量修改：绝不整体替换/"
        "重写该文件，绝不删除既有依赖/插件/属性，绝不改动既有 parent 声明"
        "（parent 版本若需写必须是**字面量**，绝不可写成 ${{...}} 属性引用）。")
    owner = _st("st-o", writable=["mod-b/pom.xml"],
                create=["mod-b/src/main/java/com/x/B.java"],
                readable=[_BASE_ENTITY],
                desc="补齐 mod-b 构建。" + stale_iron)
    plan = _plan([owner], [{"module": "mod-b", "artifacts": []}])
    inject_build_scaffold_subtasks(plan, proj)
    assert owner.description.count("既有 pom 修改铁律") == 1, \
        f"MODIFY 形态重注入必须 upsert 不重复追加，got:\n{owner.description}"


def test_scaffold_subtask_stale_template_refreshed(tmp_path):
    """脚手架子任务（st-scaffold-*）第二遍注入同样必须刷新陈旧模板（existing_ids
    幂等跳过的是"重复创建子任务"，不是"放过陈旧模板"）。"""
    proj = _mk_repo(tmp_path)
    scaffold = _st("st-scaffold-mod-a", create=["mod-a/pom.xml"],
                   desc="【构建脚手架】为模块 mod-a 创建构建文件 mod-a/pom.xml：…"
                        + _STALE_AUTH_BLOCK)
    coder = _st("st-c", create=["mod-a/src/main/java/com/x/A.java"],
                readable=[_BASE_ENTITY], depends=["st-scaffold-mod-a"])
    plan = _plan([scaffold, coder], [{"module": "mod-a", "artifacts": []}])
    inject_build_scaffold_subtasks(plan, proj)
    assert scaffold.description.count("【权威 pom 模板") == 1
    assert "stale-phantom-dep" not in scaffold.description
    assert "<artifactId>ruoyi-common</artifactId>" in scaffold.description


# ───────────────────── ② T5 推导边方向校验 ─────────────────────

def test_t5_reverse_edge_dropped_when_producer_downstream(tmp_path, caplog):
    """★st-26 反向依赖形态★：mod-api（SDK，pom 生产者零依赖=临界路径入口）的子任务
    readable 里出现 mod-impl 代码 → 旧行为直接推 api→impl 依赖。但 mod-impl 的 pom
    生产者【传递依赖】mod-api 的写者——api 构建时 impl 的 pom 必然还不存在，该依赖
    注定解析不了（成环形态）→ 必须剪除 + WARNING fail-loud。"""
    proj = _mk_repo(tmp_path)
    api_pom = _st("st-api-pom", create=["mod-api/pom.xml"])
    api_code = _st("st-api-code",
                   create=["mod-api/src/main/java/com/x/IChannel.java"],
                   readable=["mod-impl/src/main/java/com/x/Impl.java"],
                   depends=["st-api-pom"])
    impl_pom = _st("st-impl-pom", create=["mod-impl/pom.xml"],
                   depends=["st-api-pom"])
    impl_code = _st("st-impl-code",
                    create=["mod-impl/src/main/java/com/x/Impl.java"],
                    depends=["st-impl-pom", "st-api-code"])
    plan = _plan([api_pom, api_code, impl_pom, impl_code],
                 [{"module": "mod-api", "artifacts": []},
                  {"module": "mod-impl", "artifacts": []}])
    dirs = {"mod-api": "mod-api", "mod-impl": "mod-impl"}
    with caplog.at_level(logging.WARNING):
        derived = derive_internal_module_deps(plan, dirs, proj)
    assert not any("mod-impl" in d for d in (derived.get("mod-api") or [])), \
        f"目标模块 pom 生产者在消费者下游 → 反向边必须剪除, got {derived}"
    assert any("T5" in r.message and "mod-impl" in r.message
               for r in caplog.records), "剪除必须 fail-loud（WARNING 留痕）"


def test_t5_forward_edge_kept_when_producer_upstream(tmp_path):
    """对照面：正向边（消费者的证据目标模块 pom 生产者在其上游/无依赖）照常注入——
    方向校验绝不误杀合法内部依赖（test_derive_plan_sibling_one_way 锁的旧行为不回归）。"""
    proj = _mk_repo(tmp_path)
    api_pom = _st("st-api-pom", create=["mod-api/pom.xml"])
    api_code = _st("st-api-code",
                   create=["mod-api/src/main/java/com/x/IChannel.java"],
                   depends=["st-api-pom"])
    impl_pom = _st("st-impl-pom", create=["mod-impl/pom.xml"],
                   depends=["st-api-pom"])
    impl_code = _st("st-impl-code",
                    create=["mod-impl/src/main/java/com/x/Impl.java"],
                    readable=["mod-api/src/main/java/com/x/IChannel.java"],
                    depends=["st-impl-pom", "st-api-code"])
    plan = _plan([api_pom, api_code, impl_pom, impl_code],
                 [{"module": "mod-api", "artifacts": []},
                  {"module": "mod-impl", "artifacts": []}])
    dirs = {"mod-api": "mod-api", "mod-impl": "mod-impl"}
    derived = derive_internal_module_deps(plan, dirs, proj)
    assert "com.ruoyi:mod-api:${project.version}" in (derived.get("mod-impl") or []), \
        f"正向内部依赖不可误杀, got {derived}"


# ───────────────────── ③ reconcile_template_exam ─────────────────────

_TPL_OKHTTP = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<project>\n    <modelVersion>4.0.0</modelVersion>\n"
    "    <parent>\n        <groupId>com.ruoyi</groupId>\n"
    "        <artifactId>ruoyi</artifactId>\n        <version>4.8.3</version>\n"
    "    </parent>\n    <artifactId>mod-a</artifactId>\n"
    "    <packaging>jar</packaging>\n    <dependencies>\n"
    "        <dependency>\n            <groupId>com.squareup.okhttp3</groupId>\n"
    "            <artifactId>okhttp</artifactId>\n"
    "            <version>5.4.0</version>\n        </dependency>\n"
    "        <dependency>\n            <groupId>com.alibaba.fastjson2</groupId>\n"
    "            <artifactId>fastjson2</artifactId>\n        </dependency>\n"
    "    </dependencies>\n</project>")


def _st_with_template(verify, acceptance):
    desc = ("创建 mod-a 模块脚手架：pom.xml 仅引入 jackson-databind、HttpClient5 等基础库。"
            "\n【权威 pom 模板（确定性生成，原样写入 mod-a/pom.xml；parent 版本必须是"
            f"**字面量**）】\n```xml\n{_TPL_OKHTTP}\n```")
    return _st("st-26x", create=["mod-a/pom.xml"], desc=desc,
               harness=TaskHarness(verify_commands=verify),
               acceptance=acceptance)


def test_reconcile_rewrites_contradictory_verify_from_template():
    """★st-26 考卷本体★：LLM 旧考卷 grep jackson/httpclient 考确定性模板必死——
    reconcile 后针对该 pom 的内容断言必须全部由模板重新生成（同源），旧断言剔除。"""
    from swarm.brain.contract_utils import reconcile_template_exam
    st = _st_with_template(
        verify=["grep -q 'jackson' mod-a/pom.xml",
                "grep -q 'httpclient' mod-a/pom.xml",
                "test -z \"$(grep -E 'ruoyi-common|ruoyi-framework' mod-a/pom.xml)\""],
        acceptance=["mvn validate -pl mod-a 构建成功"])
    plan = TaskPlan(subtasks=[st], parallel_groups=[["st-26x"]])
    summary = reconcile_template_exam(plan)
    vcs = st.harness.verify_commands
    assert not any("jackson" in v for v in vcs), f"旧矛盾断言必须剔除: {vcs}"
    assert not any("httpclient" in v for v in vcs), f"旧矛盾断言必须剔除: {vcs}"
    assert any("<artifactId>okhttp</artifactId>" in v for v in vcs), \
        f"模板依赖必须逐条生成确定性断言: {vcs}"
    assert any("<artifactId>fastjson2</artifactId>" in v for v in vcs), vcs
    assert summary.get("st-26x", {}).get("dropped_verify"), "剔除必须机读留痕"


def test_reconcile_contradicting_negative_assert_warns(caplog):
    """负断言与模板矛盾（test -z 考的 pattern 在模板里存在）＝四面矛盾现形——
    剔除之外必须 WARNING 留痕（这是规划期抓出 st-26 死局的信号面）。"""
    from swarm.brain.contract_utils import reconcile_template_exam
    st = _st_with_template(
        verify=["test -z \"$(grep 'okhttp' mod-a/pom.xml)\""],
        acceptance=[])
    plan = TaskPlan(subtasks=[st], parallel_groups=[["st-26x"]])
    with caplog.at_level(logging.WARNING):
        reconcile_template_exam(plan)
    assert any("okhttp" in r.message and "矛盾" in r.message
               for r in caplog.records), \
        "负断言与模板正面冲突必须 WARNING（规划期自曝矛盾，绝不留给 worker 送死）"


def test_reconcile_keeps_non_content_commands_and_other_files():
    """非内容断言（构建命令）与针对其他文件的断言绝不误动。"""
    from swarm.brain.contract_utils import reconcile_template_exam
    st = _st_with_template(
        verify=["mvn -q validate -pl mod-a",
                "grep -q 'class Engine' mod-a/src/main/java/Engine.java"],
        acceptance=[])
    plan = TaskPlan(subtasks=[st], parallel_groups=[["st-26x"]])
    reconcile_template_exam(plan)
    vcs = st.harness.verify_commands
    assert "mvn -q validate -pl mod-a" in vcs, "构建命令必须保留"
    assert "grep -q 'class Engine' mod-a/src/main/java/Engine.java" in vcs, \
        "其他文件的内容断言必须保留"


def test_reconcile_rewrites_rule5_acceptance_and_appends_authority():
    """规则5 机器验收行（"必须声明依赖: […]"）必须改写为模板依赖清单（同源），
    并追加"模板即真值"权威验收行；全程幂等。"""
    from swarm.brain.contract_utils import reconcile_template_exam
    st = _st_with_template(
        verify=[],
        acceptance=[
            "mod-a/pom.xml 必须声明依赖: ['jackson-databind', 'httpclient5']"
            "（缺一即整模块 mvn compile 失败）",
            "pom.xml 中声明了 jackson-databind 和 httpclient5 依赖",
        ])
    plan = TaskPlan(subtasks=[st], parallel_groups=[["st-26x"]])
    reconcile_template_exam(plan)
    acc = st.acceptance_criteria
    rule5 = [a for a in acc if "必须声明依赖" in a]
    assert len(rule5) == 1 and "okhttp" in rule5[0] and "jackson" not in rule5[0], \
        f"规则5 机器行必须按模板重写: {acc}"
    assert any("权威 pom 模板" in a and "为准" in a for a in acc), \
        f"必须追加模板即真值权威验收行: {acc}"
    before = list(acc)
    reconcile_template_exam(plan)
    assert st.acceptance_criteria == before, "reconcile 必须幂等"


def test_inject_chokepoint_runs_reconcile(tmp_path):
    """接线面：inject_build_scaffold_subtasks 末端必须自动跑 reconcile——两遍注入
    与外科重试路径全走此咽喉，考卷同源不靠调用方自觉。"""
    proj = _mk_repo(tmp_path)
    owner = _st("st-o", create=["mod-a/pom.xml",
                                "mod-a/src/main/java/com/x/A.java"],
                readable=[_BASE_ENTITY],
                desc="创建 mod-a 模块脚手架。",
                harness=TaskHarness(
                    verify_commands=["grep -q 'jackson' mod-a/pom.xml"]))
    plan = _plan([owner], [{"module": "mod-a", "artifacts": []}])
    inject_build_scaffold_subtasks(plan, proj)
    assert "【权威 pom 模板" in owner.description, "前置：owner 拿到模板"
    vcs = owner.harness.verify_commands
    assert not any("jackson" in v for v in vcs), \
        f"注入咽喉必须同步 reconcile 考卷: {vcs}"
    assert any("<artifactId>ruoyi-common</artifactId>" in v for v in vcs), vcs


def test_reconcile_keeps_noncontradicting_negative_assert():
    """★猎手 CRITICAL 锁★：与模板不冲突的负断言（禁入依赖守卫）必须保留——
    它是模板被后续机制改写时的最后一道牙齿，剔除=禁入不变量无人看守。"""
    from swarm.brain.contract_utils import reconcile_template_exam
    guard = "test -z \"$(grep -E 'ruoyi-common|ruoyi-framework' mod-a/pom.xml)\""
    st = _st_with_template(verify=[guard], acceptance=[])
    plan = TaskPlan(subtasks=[st], parallel_groups=[["st-26x"]])
    reconcile_template_exam(plan)
    assert guard in st.harness.verify_commands, \
        f"不冲突的负断言（禁入守卫）必须保留: {st.harness.verify_commands}"


def test_reconcile_boundary_no_false_capture_of_sibling_pom():
    """★复核 CONFIRMED 锁★：针对相似名兄弟文件（a/mod-a/pom.xml 嵌套复用叶名）的
    断言绝不被误吞进本 pom 的重生成面。"""
    from swarm.brain.contract_utils import reconcile_template_exam
    sibling = "grep -q 'jackson' a/mod-a/pom.xml"
    st = _st_with_template(verify=[sibling], acceptance=[])
    plan = TaskPlan(subtasks=[st], parallel_groups=[["st-26x"]])
    reconcile_template_exam(plan)
    assert sibling in st.harness.verify_commands, \
        f"兄弟文件断言必须原样保留: {st.harness.verify_commands}"


def test_extract_template_path_excludes_fullwidth_paren():
    """★复核 CONFIRMED 锁★：聚合父/孤儿脚手架措辞「原样写入 {pom}）】」（无限定语）
    的路径捕获不得把全角）粘进路径。"""
    from swarm.brain.contract_utils import _extract_auth_templates
    desc = ("【构建脚手架】…\n【权威 pom 模板（确定性生成，原样写入 agg/pom.xml）】"
            "\n```xml\n<project><artifactId>agg</artifactId></project>\n```")
    tpls = _extract_auth_templates(desc)
    assert tpls and tpls[0][0] == "agg/pom.xml", f"路径被污染: {tpls}"


def test_multi_pom_owner_second_pass_idempotent(tmp_path, caplog):
    """★猎手 MED 锁★：owner 拥有两个模块 pom 时，第二遍注入必须位置无关幂等——
    strip+append 的顺序抖动绝不触发"刷新"WARNING 刷屏（噪声淹没真漂移信号）。"""
    proj = _mk_repo(tmp_path)
    owner = _st("st-o", create=["mod-a/pom.xml", "mod-b/pom.xml",
                                "mod-a/src/main/java/A.java",
                                "mod-b/src/main/java/B.java"],
                readable=[_BASE_ENTITY],
                desc="创建双模块脚手架。")
    plan = _plan([owner], [{"module": "mod-a", "artifacts": []},
                           {"module": "mod-b", "artifacts": []}])
    inject_build_scaffold_subtasks(plan, proj)
    assert owner.description.count("【权威 pom 模板") == 2, "前置：两块模板都注入"
    desc_after_first = owner.description
    with caplog.at_level(logging.WARNING):
        inject_build_scaffold_subtasks(plan, proj)
    assert owner.description.count("【权威 pom 模板") == 2
    assert not any("机器块与本遍确定性产物不一致" in r.message
                   for r in caplog.records), \
        "内容一致仅顺序不同时绝不可谎报刷新（WARNING 只留给真漂移）"
    assert sorted(desc_after_first.split("\n")) == sorted(
        owner.description.split("\n")), "第二遍不得改变内容集合"


def test_contract_declared_reverse_dep_pruned(tmp_path, caplog):
    """★猎手 HIGH 锁★：契约自声明的反向兄弟依赖走同一方向判据剪除——
    只剪推导通道，st-26 死型会换契约通道复活（_merge_internal_deps 契约优先）。"""
    proj = _mk_repo(tmp_path)
    api_pom = _st("st-api-pom", create=["mod-api/pom.xml"])
    api_code = _st("st-api-code",
                   create=["mod-api/src/main/java/com/x/IChannel.java"],
                   depends=["st-api-pom"])
    impl_pom = _st("st-impl-pom", create=["mod-impl/pom.xml"],
                   depends=["st-api-pom"])
    impl_code = _st("st-impl-code",
                    create=["mod-impl/src/main/java/com/x/Impl.java"],
                    depends=["st-impl-pom", "st-api-code"])
    plan = _plan(
        [api_pom, api_code, impl_pom, impl_code],
        # 带版本的坐标形态（round65d 实锤：${project.version} 内部坐标能通过 R53-1
        # 可解析性剪除存活进模板——无版本形态已被 R53-1 拦下，方向剪除治的是这一形态）
        [{"module": "mod-api", "artifacts": ["com.ruoyi:mod-impl:${project.version}"]},
         {"module": "mod-impl", "artifacts": []}])
    with caplog.at_level(logging.WARNING):
        inject_build_scaffold_subtasks(plan, proj)
    assert "mod-impl" not in api_pom.description, \
        f"契约声明的反向依赖必须被剪除，绝不进 mod-api 模板:\n{api_pom.description}"
    assert any("契约声明的反向内部依赖剪除" in r.message for r in caplog.records), \
        "剪除必须 fail-loud"


# ───────────────────── fixture 重放：st-26 原件 ─────────────────────

def test_fixture_st26_exam_coherent_after_reconcile():
    """round65d 原件重放：st-26（jackson 考卷 × okhttp 模板四面矛盾）经 reconcile 后
    考卷与模板同源——worker 拿到的将是一张可及格的卷子。"""
    from swarm.brain.contract_utils import reconcile_template_exam
    fx = Path(__file__).resolve().parent / "fixtures" / "plan_b583.json"
    data = json.loads(fx.read_text(encoding="utf-8"))
    plan = TaskPlan(**data["plan"])
    st26 = next(s for s in plan.subtasks if s.id == "st-26")
    assert any("jackson" in v for v in st26.harness.verify_commands), \
        "前置：fixture 原件确实带矛盾考卷"
    reconcile_template_exam(plan)
    vcs = st26.harness.verify_commands
    assert not any("jackson" in v or "httpclient" in v for v in vcs), \
        f"st-26 矛盾考卷必须被模板同源重写: {vcs}"
    assert any("<artifactId>okhttp</artifactId>" in v for v in vcs), vcs
    # 模板里实际烤进的每个依赖都必须有对应断言（含第一遍 T5 的 ruoyi-alarm——
    # 考卷同源以 description 里的字面模板为准，模板对错由 upsert 刷新负责）
    tpl_deps = re.findall(
        r"<artifactId>([^<]+)</artifactId>",
        st26.description.split("<dependencies>", 1)[1].split("</dependencies>", 1)[0])
    for dep in tpl_deps:
        assert any(f"<artifactId>{dep}</artifactId>" in v for v in vcs), \
            f"模板依赖 {dep} 缺确定性断言: {vcs}"
    acc = st26.acceptance_criteria
    rule5 = [a for a in acc if "必须声明依赖" in a]
    assert rule5 and all("okhttp" in a for a in rule5), f"规则5 行必须与模板同源: {acc}"
