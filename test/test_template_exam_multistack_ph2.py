"""P-H2（27 号文 + 26 号文 G-H11）：reconcile_template_exam 多栈驱动化。

此前考卷同源是 Maven 独有（锚「权威 pom 模板」+ ```xml 围栏），npm/go/python/cargo/
gradle 的权威模板恒不被识别 → 这些栈的考卷永不同源，陈旧正断言照旧送 worker 送死
（st-26 四面矛盾死型的异栈复制品）。

锁的事实：
① 五栈模板识别（围栏 json/裸/toml/groovy/kotlin）+ 依赖抽取 + 正断言重生成；
② 负断言冲突剔除+WARNING、不冲突保留（与 Maven 同律）；
③ 规则5 机器行改写 + 「模板即真值」权威行（标签按栈）；
④ 抽取失败 fail-honest 跳过（绝不用空清单重生成考卷）；
⑤ 无 driver 落点机读告警；G-H11 告警只对表外栈（composer/bundler）；
⑥ Maven 行为逐字节不变（既有 r65d/r65e2 测试盘不动）。
"""
from __future__ import annotations

import logging
import re
from types import SimpleNamespace

import pytest

from swarm.brain import contract_utils as cu
from swarm.brain.contract_utils import (
    _exam_deps_cargo,
    _exam_deps_go,
    _exam_deps_gradle,
    _exam_deps_npm,
    _exam_deps_python,
    _extract_auth_templates,
    reconcile_template_exam,
)
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskHarness, TaskPlan


def _st(sid, *, desc, harness=None, acceptance=None):
    kw = {"harness": harness} if harness is not None else {}
    return SubTask(id=sid, description=desc,
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(create_files=[], writable=[], readable=[]),
                   acceptance_criteria=acceptance or [], **kw)


def _plan(*sts):
    return TaskPlan(subtasks=list(sts), parallel_groups=[[st.id for st in sts]])


def _block(label: str, fence: str, path: str, body: str) -> str:
    """与五个 driver 的【权威 X 模板】块同形（同一段措辞模板）。"""
    return (f"\n【权威 {label} 模板（确定性生成，原样写入 {path}；仅当项目另有"
            f"明确约定才允许在此基础上增改）】\n```{fence}\n{body}\n```")


def _harness(*vcs):
    return TaskHarness(compile_cmd="", test_cmd="",
                       verify_commands=list(vcs))


# ── ① 抽取器 × 真实渲染器（同源锁：从真 driver 产物抽，绝不手抄夹具） ──

def test_extractors_match_real_renderers():
    npm_tpl = cu._render_package_json(
        "web", [SimpleNamespace(name="react", spec="^18.2.0"),
                SimpleNamespace(name="lodash", spec="4.17.21")])
    assert _exam_deps_npm(npm_tpl) == ["react", "lodash"]

    go_tpl = cu._render_go_mod(
        "example.com/svc", "1.22",
        [SimpleNamespace(module="github.com/gin-gonic/gin", version="v1.10.0")],
        [("example.com/lib", "../lib")])
    assert _exam_deps_go(go_tpl) == ["github.com/gin-gonic/gin", "example.com/lib"]

    py_tpl = cu._render_pyproject_toml(
        "svc", [SimpleNamespace(name="flask", extras="[async]", spec=">=2.0"),
                SimpleNamespace(name="pydantic", extras="", spec=">=2")])
    assert _exam_deps_python(py_tpl) == ["flask", "pydantic"]

    cargo_tpl = cu._render_cargo_toml(
        "svc", [SimpleNamespace(name="serde", spec="1.0", features=["derive"],
                                default_features=True),
                SimpleNamespace(name="anyhow", spec="1", features=[],
                                default_features=False)],
        [("my-core", "../core")])
    assert _exam_deps_cargo(cargo_tpl) == ["serde", "anyhow", "my-core"]

    from swarm.brain.gradle_registry import ResolvedGradleDep
    for dialect in ("groovy", "kts"):
        gradle_tpl = cu._render_build_gradle(
            dialect, [ResolvedGradleDep("com.google.guava", "guava", "33.4.8-jre",
                                        "maven_central"),
                      ResolvedGradleDep("", "", None, source="explicit",
                                        verified="unjudgeable",
                                        raw="g:b-lib:1.0:test-fixtures")],
            [":services:core"])
        assert _exam_deps_gradle(gradle_tpl) == ["com.google.guava:guava:33.4.8-jre",
                                                 "g:b-lib:1.0:test-fixtures"], dialect


def test_extract_templates_all_fences():
    """五栈六形态围栏全识别（go 裸围栏；gradle 双方言）；落点路径原样捕获。"""
    desc = ("做清单" + _block("package.json", "json", "web/package.json", "{}\n")
            + _block("go.mod", "", "svc/go.mod", "module x\n")
            + _block("pyproject.toml", "toml", "svc/pyproject.toml", "[project]\n")
            + _block("Cargo.toml", "toml", "svc/Cargo.toml", "[package]\n")
            + _block("build.gradle", "groovy", "api/build.gradle", "plugins {}\n")
            + _block("build.gradle.kts", "kotlin", "api/build.gradle.kts", "plugins {}\n"))
    tpls = _extract_auth_templates(desc)
    assert [p for p, _ in tpls] == ["web/package.json", "svc/go.mod",
                                    "svc/pyproject.toml", "svc/Cargo.toml",
                                    "api/build.gradle", "api/build.gradle.kts"]


# ── ② 全栈对账行为（正断言重生成 / 负断言冲突剔除 / 规则5 改写 / 权威行） ──

def _reconciled(st):
    plan = _plan(st)
    summary = reconcile_template_exam(plan)
    assert st.id in summary
    return st


def test_npm_exam_reconciled(caplog):
    tpl = cu._render_package_json(
        "web", [SimpleNamespace(name="react", spec="^18.2.0"),
                SimpleNamespace(name="lodash", spec="4.17.21")])
    st = _st("st-1",
             desc="建 package.json" + _block("package.json", "json",
                                           "web/package.json", tpl),
             harness=_harness(
                 "grep -q '\"axios\":' web/package.json",          # 陈旧正断言 → 剔除
                 'test -z "$(grep \'"react"\' web/package.json)"',   # 与模板冲突 → 剔除+WARNING
                 'test -z "$(grep \'"jquery"\' web/package.json)"',  # 不冲突 → 保留
                 "grep -q 'export' web/src/index.ts"),              # 别的文件 → 不动
             acceptance=["web/package.json 必须声明依赖: ['axios']（缺一即整模块编译失败）",
                         "npm run build 通过"])
    with caplog.at_level(logging.WARNING):
        _reconciled(st)
    vcs = st.harness.verify_commands
    assert "grep -q '\"axios\":' web/package.json" not in vcs
    assert "grep -q '\"react\":' web/package.json" in vcs
    assert "grep -q '\"lodash\":' web/package.json" in vcs
    assert not any("react" in v and "test -z" in v for v in vcs), "冲突负断言必须剔除"
    assert any("jquery" in v for v in vcs), "不冲突负断言=禁入守卫，保留"
    assert "grep -q 'export' web/src/index.ts" in vcs
    assert any("正面矛盾" in r.message for r in caplog.records)
    acc = st.acceptance_criteria
    assert "web/package.json 必须声明依赖: ['lodash', 'react']（缺一即整模块编译失败）" in acc
    assert any("【权威 package.json 模板】（web/package.json）" in a for a in acc)
    assert "npm run build 通过" in acc


def test_go_exam_reconciled():
    tpl = cu._render_go_mod(
        "example.com/svc", "1.22",
        [SimpleNamespace(module="github.com/gin-gonic/gin", version="v1.10.0")],
        [("example.com/lib", "../lib")])
    st = _st("st-1",
             desc="建 go.mod" + _block("go.mod", "", "svc/go.mod", tpl),
             harness=_harness("grep -q 'github.com/old/dep ' svc/go.mod"),
             acceptance=["svc/go.mod 必须声明依赖: ['github.com/old/dep']（缺一即整模块编译失败）"])
    _reconciled(st)
    vcs = st.harness.verify_commands
    assert not any("old/dep" in v for v in vcs)
    assert "grep -q 'github.com/gin-gonic/gin ' svc/go.mod" in vcs
    assert "grep -q 'example.com/lib ' svc/go.mod" in vcs, "内部 module 同进考卷"
    assert ("svc/go.mod 必须声明依赖: ['example.com/lib', 'github.com/gin-gonic/gin']"
            "（缺一即整模块编译失败）") in st.acceptance_criteria


def test_python_exam_reconciled_and_assert_boundary():
    tpl = cu._render_pyproject_toml(
        "svc", [SimpleNamespace(name="flask", extras="[async]", spec=">=2.0"),
                SimpleNamespace(name="pydantic", extras="", spec=">=2")])
    st = _st("st-1",
             desc="建 pyproject" + _block("pyproject.toml", "toml",
                                        "svc/pyproject.toml", tpl),
             harness=_harness("grep -q '\"requests' svc/pyproject.toml"),
             acceptance=[])
    _reconciled(st)
    vcs = st.harness.verify_commands
    assert not any("requests" in v for v in vcs)
    flask_assert = next(v for v in vcs if "flask" in v)
    pyd_assert = next(v for v in vcs if "pydantic" in v)
    # 断言必须带条目边界符：`"pydantic"` 不得被 `"pydantic-core"` 假过
    pat = re.search(r"grep -qE '([^']+)'", pyd_assert).group(1)
    assert re.search(pat, '    "pydantic>=2",')
    assert not re.search(pat, '    "pydantic-core>=2",'), "探针窄于真断言=假过"
    assert re.search(re.search(r"grep -qE '([^']+)'", flask_assert).group(1),
                     '    "flask[async]>=2.0",')
    assert any("【权威 pyproject.toml 模板】" in a for a in st.acceptance_criteria)


def test_cargo_exam_reconciled():
    tpl = cu._render_cargo_toml(
        "svc", [SimpleNamespace(name="serde", spec="1.0", features=["derive"],
                                default_features=True)],
        [("my-core", "../core")])
    st = _st("st-1",
             desc="建 Cargo.toml" + _block("Cargo.toml", "toml",
                                         "svc/Cargo.toml", tpl),
             harness=_harness("grep -q 'tokio' svc/Cargo.toml"),
             acceptance=[])
    _reconciled(st)
    vcs = st.harness.verify_commands
    assert not any("tokio" in v for v in vcs)
    assert "grep -qE '^serde ' svc/Cargo.toml" in vcs
    assert "grep -qE '^my-core ' svc/Cargo.toml" in vcs, "path 内部依赖同进考卷"


def test_gradle_exam_reconciled_both_dialects():
    from swarm.brain.gradle_registry import ResolvedGradleDep
    for dialect, fname, fence in (("groovy", "build.gradle", "groovy"),
                                  ("kts", "build.gradle.kts", "kotlin")):
        tpl = cu._render_build_gradle(
            dialect, [ResolvedGradleDep("com.google.guava", "guava", "33.4.8-jre",
                                        "maven_central")],
            [":services:core"])
        st = _st("st-1",
                 desc="建 build 文件" + _block(fname, fence, f"api/{fname}", tpl),
                 harness=_harness(f"grep -q 'org.old:dep' api/{fname}"),
                 acceptance=[])
        _reconciled(st)
        vcs = st.harness.verify_commands
        assert not any("org.old:dep" in v for v in vcs), dialect
        assert (f"grep -qE 'com.google.guava:guava:33.4.8-jre[^A-Za-z0-9_.:-]' "
                f"api/{fname}") in vcs, dialect
        assert not any("services:core" in v for v in vcs), \
            f"project() 内部依赖不进依赖考卷（{dialect}，登记边界）"


# ── ③ fail-honest 与机读可辨 ──

def test_malformed_npm_template_skipped_fail_honest(caplog):
    """模板解析失败 → 该模板跳过+WARNING，考卷【原样保留】（绝不用空清单重生成
    零断言=比不对账更坏的假同源）。"""
    st = _st("st-1",
             desc="建 package.json" + _block("package.json", "json",
                                           "web/package.json", "{ not json !\n"),
             harness=_harness("grep -q '\"axios\":' web/package.json"),
             acceptance=[])
    plan = _plan(st)
    with caplog.at_level(logging.WARNING):
        summary = reconcile_template_exam(plan)
    assert summary == {}
    assert st.harness.verify_commands == ["grep -q '\"axios\":' web/package.json"]
    assert any("解析失败" in r.message and "fail-honest" in r.message
               for r in caplog.records)


def test_driverless_manifest_warns_machine_readable(caplog):
    """落点在 _EXAM_DRIVERS 表外（composer.json）→ 跳过+机读 WARNING（G-H11：
    缺席与「真没有」必须可分）。"""
    st = _st("st-1",
             desc="建 composer.json" + _block("composer.json", "json",
                                            "web/composer.json", "{}\n"),
             harness=_harness("grep -q 'laravel' web/composer.json"),
             acceptance=[])
    plan = _plan(st)
    with caplog.at_level(logging.WARNING):
        summary = reconcile_template_exam(plan)
    assert summary == {}
    assert any("无考卷同源 driver" in r.message for r in caplog.records)


def test_gh11_warning_only_for_driverless_stacks(tmp_path, caplog, monkeypatch):
    """G-H11 告警面收窄：npm（表内）不再刷「不支持」；composer（表外）照刷。"""
    (tmp_path / "composer.json").write_text("{}")
    st = _st("st-1",
             desc="建 composer.json" + _block("composer.json", "json",
                                            "web/composer.json", "{}\n"))
    plan = _plan(st)
    with caplog.at_level(logging.WARNING):
        cu.inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    assert any("考卷同源对账对本轮的权威模板落点" in r.message
               and "composer.json" in r.message for r in caplog.records), \
        "表外落点必须照旧告警（机读可辨）"
    caplog.clear()

    (tmp_path / "package.json").write_text('{"name": "web"}')
    # 已同源的 npm 子任务（verify 与模板一致 + 权威行已在）→ 对账零变更 →
    # 走「零对账」分支的栈支持判定——突变「支持集回退 {pom.xml}」在这里才会现行
    st2 = _st("st-1",
              desc="建 package.json" + _block(
                  "package.json", "json", "web/package.json",
                  cu._render_package_json(
                      "web", [SimpleNamespace(name="react", spec="^18")])),
              harness=_harness("grep -q '\"react\":' web/package.json"),
              acceptance=["依赖清单以 description 中【权威 package.json 模板】"
                          "（web/package.json）字面为准——模板即真值，其他验收条目"
                          "与模板冲突时以模板为准"])
    plan2 = _plan(st2)
    with caplog.at_level(logging.WARNING):
        cu.inject_build_scaffold_subtasks(plan2, str(tmp_path), None)
    assert not any("考卷同源对账对本轮的权威模板落点" in r.message
                   for r in caplog.records), \
        "npm 已在驱动表内——再刷「不支持」=假警报（告警面必须如实收窄）"


# ── R2 双透镜整改（hunter F1-F7 + reviewer F1-F3，全部先探针复现再治） ──

def test_gh11_granularity_is_manifest_not_stack(tmp_path, caplog):
    """R2 双透镜 F1：requirements.txt 属 python 栈而驱动表没有它——栈粒度判定让它
    借 python 之名压掉 G-H11 告警（degrade 键不记）。判定粒度=落点 basename
    （_EXAM_DRIVERS 的键域，driver 按清单名注册，「认不得」按同一粒度报）。"""
    st = _st("st-1",
             desc="建 requirements.txt" + _block("requirements.txt", "",
                                                 "svc/requirements.txt", "flask\n"))
    plan = _plan(st)
    with caplog.at_level(logging.WARNING):
        cu.inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    assert any("考卷同源对账对本轮的权威模板落点" in r.message
               and "requirements.txt" in r.message for r in caplog.records)


def test_gh11_prose_mention_does_not_mask_unsupported(tmp_path, caplog):
    """R2 reviewer F3：散文提到 package.json（表内）不得压掉真 unsupported 落点
    （stack.toml）的告警——判据与 reconcile 同源=实际提取的围栏块，绝非第二口
    子串扫描。"""
    st = _st("st-1",
             desc="参考 package.json 写法"
                  + _block("stack.toml", "toml", "svc/stack.toml", "x = 1\n"))
    plan = _plan(st)
    with caplog.at_level(logging.WARNING):
        cu.inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    assert any("考卷同源对账对本轮的权威模板落点" in r.message
               and "stack.toml" in r.message for r in caplog.records)


def test_npm_non_dict_dependencies_fail_honest(caplog):
    """R2 双透镜 F2/F3：dependencies 结构违例（数组/字符串）=认不得 → None →
    跳过+WARNING，考卷【原样保留】（绝不拿空清单抹掉旧断言=假同源）。"""
    for bad in ('{"dependencies": ["flask"]}', '{"dependencies": "bad"}', '[1,2]'):
        st = _st("st-1",
                 desc="建 package.json" + _block("package.json", "json",
                                                 "web/package.json", bad + "\n"),
                 harness=_harness("grep -q '\"axios\":' web/package.json"),
                 acceptance=[])
        plan = _plan(st)
        with caplog.at_level(logging.WARNING):
            summary = reconcile_template_exam(plan)
        assert summary == {}, bad
        assert st.harness.verify_commands == ["grep -q '\"axios\":' web/package.json"], bad
        assert any("解析失败" in r.message for r in caplog.records), bad
        caplog.clear()


def test_npm_absent_dependencies_is_true_empty():
    """二分另一臂：dependencies 键【缺席】=真零依赖（渲染器零依赖即整键缺席）→ []
    → 对账照常（旧内容断言剔除+零新断言+权威行），绝不能误判成 fail-honest 跳过。"""
    tpl = cu._render_package_json("web", [])   # 渲染器零依赖产物=无 dependencies 键
    assert "dependencies" not in tpl
    assert _exam_deps_npm(tpl) == []
    st = _st("st-1",
             desc="建 package.json" + _block("package.json", "json",
                                             "web/package.json", tpl),
             harness=_harness("grep -q '\"axios\":' web/package.json",
                              "npm run build"),
             acceptance=[])
    _reconciled(st)
    vcs = st.harness.verify_commands
    assert "grep -q '\"axios\":' web/package.json" not in vcs, "真零依赖：旧内容断言必须剔除"
    assert "npm run build" in vcs
    assert any("【权威 package.json 模板】" in a for a in st.acceptance_criteria)


def test_python_malformed_dependencies_fail_honest(caplog):
    """R2 双透镜 F2：dependencies 键在场但形状认不得（单行数组/非数组/体非空零
    条目）→ None → 跳过+WARNING，考卷原样保留。"""
    for bad in ('[project]\nname = "x"\ndependencies = ["flask"]\n',
                '[project]\nname = "x"\ndependencies = "bad"\n',
                '[project]\nname = "x"\ndependencies = [\n  flask, requests\n]\n'):
        assert _exam_deps_python(bad) is None, bad
        st = _st("st-1",
                 desc="建 pyproject" + _block("pyproject.toml", "toml",
                                              "svc/pyproject.toml", bad),
                 harness=_harness("grep -q '\"requests' svc/pyproject.toml"),
                 acceptance=[])
        plan = _plan(st)
        with caplog.at_level(logging.WARNING):
            summary = reconcile_template_exam(plan)
        assert summary == {}, bad
        assert st.harness.verify_commands == ["grep -q '\"requests' svc/pyproject.toml"], bad
        caplog.clear()


def test_python_absent_dependencies_is_true_empty():
    """二分另一臂：dependencies 键缺席=真零依赖 → [] → 对账照常。"""
    tpl = cu._render_pyproject_toml("svc", [])
    assert "dependencies" not in tpl
    assert _exam_deps_python(tpl) == []
    st = _st("st-1",
             desc="建 pyproject" + _block("pyproject.toml", "toml",
                                          "svc/pyproject.toml", tpl),
             harness=_harness("grep -q '\"requests' svc/pyproject.toml"),
             acceptance=[])
    _reconciled(st)
    assert st.harness.verify_commands == []
    assert any("【权威 pyproject.toml 模板】" in a for a in st.acceptance_criteria)


def test_go_bare_require_and_block_garbage_fail_honest(caplog):
    """R2 hunter F4：裸 `require y`（无版本）/块内单元行=形状认不得 → None；
    块内独立注释行合法——跳过且不被抓成 module "//"（治前隐患顺带收口）。"""
    assert _exam_deps_go("module x\ngo 1.22\nrequire y\n") is None
    assert _exam_deps_go("module x\nrequire (\n\tsingleton\n)\n") is None
    assert _exam_deps_go("module x\nrequire (\n\t// 注释\n\ta/b v1.0\n)\n") == ["a/b"]
    st = _st("st-1",
             desc="建 go.mod" + _block("go.mod", "", "svc/go.mod",
                                       "module x\ngo 1.22\nrequire y\n"),
             harness=_harness("grep -q 'gin ' svc/go.mod"),
             acceptance=[])
    plan = _plan(st)
    with caplog.at_level(logging.WARNING):
        summary = reconcile_template_exam(plan)
    assert summary == {}
    assert st.harness.verify_commands == ["grep -q 'gin ' svc/go.mod"]


def test_gradle_assert_coordinate_boundary():
    """R2 hunter F6：坐标收尾边界——`g:a:1.0` 断言不得被 `g:a:1.0-rc1`/
    `g:a-extra` 假过（裸子串断言必然假过=#8 apt_packages 族）。"""
    assertion = cu._exam_assert_gradle("com.example:lib:1.0", "api/build.gradle")
    pat = re.search(r"grep -qE '([^']+)'", assertion).group(1)
    assert re.search(pat, 'implementation "com.example:lib:1.0"')
    assert not re.search(pat, 'implementation "com.example:lib:1.0-rc1"')
    assert not re.search(pat, 'implementation "com.example:lib-extra:1.0"')


def test_reconcile_subtask_exception_records_degrade(monkeypatch, caplog):
    """R2 hunter F7：单子任务臂异常 → WARNING + 机读 degrade 键（WARNING 会淹，
    机读键不进账=降级不可辨），兄弟子任务照常对账（半变异隔离）。"""
    keys: list[str] = []
    monkeypatch.setattr(cu, "_record_degrade_safe", keys.append)

    def _raise(tpl):
        raise RuntimeError("boom")

    tpl = cu._render_package_json("web", [SimpleNamespace(name="react", spec="^18")])
    monkeypatch.setitem(cu._EXAM_DRIVERS, "package.json",
                        cu._ExamStackDriver("npm", _raise, cu._exam_assert_npm,
                                            cu._RULE5_SUFFIX_GENERIC))
    st_bad = _st("st-bad",
                 desc="建 package.json" + _block("package.json", "json",
                                                 "web/package.json", tpl),
                 harness=_harness("grep -q '\"axios\":' web/package.json"))
    good_tpl = cu._render_go_mod(
        "example.com/svc", "1.22",
        [SimpleNamespace(module="github.com/gin-gonic/gin", version="v1.10.0")], [])
    st_ok = _st("st-ok",
                desc="建 go.mod" + _block("go.mod", "", "svc/go.mod", good_tpl),
                harness=_harness("grep -q 'old/dep ' svc/go.mod"))
    plan = _plan(st_bad, st_ok)
    with caplog.at_level(logging.WARNING):
        summary = reconcile_template_exam(plan)
    assert "brain.template_exam.reconcile_failed" in keys
    assert any("考卷同源对账失败" in r.message for r in caplog.records)
    assert "st-ok" in summary, "兄弟子任务必须照常对账（半变异隔离）"
    assert "st-bad" not in summary


def test_inject_reconcile_exception_records_degrade(monkeypatch, tmp_path):
    """R2 hunter F7（整臂）：reconcile 整体抛异常 → fail-open WARNING + degrade 键，
    注入主链绝不炸。"""
    keys: list[str] = []
    monkeypatch.setattr(cu, "_record_degrade_safe", keys.append)

    def _boom(plan):
        raise RuntimeError("boom")

    monkeypatch.setattr(cu, "reconcile_template_exam", _boom)
    plan = _plan(_st("st-1", desc="普通子任务"))
    cu.inject_build_scaffold_subtasks(plan, str(tmp_path), None)   # 不炸=fail-open
    assert "brain.template_exam.reconcile_failed" in keys
