#!/usr/bin/env python3
"""R65E2-T1（#83）：考卷同源 reconcile 必须识别 `! grep` 负断言（round65e2 主死因）。

round65e2 死因（task dda3f99f，PARTIAL 4/83）：st-1（ruoyi-alarm-interface"零RuoYi依赖SDK模块"）
plan 自相矛盾——verify#1 `! grep -qE '<artifactId>ruoyi-(common|...)</artifactId>' pom`（断言无 ruoyi 依赖）
↔ 权威 pom 模板 <dependencies> 含 ruoyi-common/framework + verify#3/#4 `grep -q '...ruoyi-common...'`
（断言必须有）。verify#1↔verify#3 数学上不可同真=确定性死局→模块骨架盲重试 4-5 次烧 28min 后 revert
→连坐 76→L2 失败→PARTIAL。

病根：`reconcile_template_exam`（R65D-T2 #61 考卷同源对账）的负断言识别只认 `test -z/-n "$(grep…)"`，
**不认 `! grep` 形式**——`_is_pom_content_assert` 对 `! grep …`（以 `!` 开头，非 `grep ` 开头）返回 False
→ verify#1 走 `not _is_pom_content_assert` 分支被无条件 KEPT，从不进负断言冲突逻辑；同时模板一致的
verify#3/#4 也 KEPT → 矛盾存活派 worker 送死。

治本：`! grep <pom内容>` 与既有 `test -z "$(grep…)"` 同为「排除断言」，须同样处理——pattern 命中
权威模板（=模板要求该依赖存在，负断言却禁止）时剔除+留痕，令考卷与模板同源自洽。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.contract_utils import (  # noqa: E402
    _is_pom_content_assert,
    reconcile_template_exam,
)
from swarm.types import (  # noqa: E402
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskHarness,
    TaskPlan,
)

_POM = "ruoyi-alarm-interface/pom.xml"
# 权威 pom 模板：含 slf4j + ruoyi-common + ruoyi-framework（T5 从 readable 证据注入）
_TPL = """<?xml version="1.0"?>
<project><modelVersion>4.0.0</modelVersion>
  <artifactId>ruoyi-alarm-interface</artifactId>
  <dependencies>
    <dependency><groupId>org.slf4j</groupId><artifactId>slf4j-api</artifactId></dependency>
    <dependency><groupId>com.ruoyi</groupId><artifactId>ruoyi-common</artifactId></dependency>
    <dependency><groupId>com.ruoyi</groupId><artifactId>ruoyi-framework</artifactId></dependency>
  </dependencies>
</project>"""
_DESC = (
    "创建 ruoyi-alarm-interface 模块 pom.xml。该模块为零 RuoYi 依赖的独立 SDK 模块。\n"
    f"【权威 pom 模板（确定性生成，原样写入 {_POM}）】\n```xml\n{_TPL}\n```\n"
)

# st-1 实际四条 verify（忠实复现）
_VERIFY = [
    # verify#1：负断言 `! grep` 排除整个 ruoyi 家族（描述"零依赖"的守卫）
    f"! grep -qE '<artifactId>ruoyi-(common|system|framework|admin)</artifactId>' {_POM}",
    f"grep -q '<artifactId>slf4j-api</artifactId>' {_POM}",
    f"grep -q '<artifactId>ruoyi-common</artifactId>' {_POM}",       # verify#3：与#1互斥
    f"grep -q '<artifactId>ruoyi-framework</artifactId>' {_POM}",    # verify#4：与#1互斥
]


def _st1():
    h = TaskHarness(verify_commands=list(_VERIFY))
    sc = FileScope(writable=[], readable=[], create_files=[_POM])
    return SubTask(id="st-1", description=_DESC, difficulty=SubTaskDifficulty.COMPLEX,
                   modality=SubTaskModality.TEXT, scope=sc, harness=h,
                   acceptance_criteria=["pom.xml 中不包含 ruoyi-common 依赖",
                                        f"{_POM} 必须声明依赖: ['ruoyi-common', 'ruoyi-framework', 'slf4j-api']"])


def _plan(st):
    p = TaskPlan(subtasks=[st], parallel_groups=[[st.id]])
    p.shared_contract = {"dependencies": [{"module": "ruoyi-alarm-interface"}]}
    return p


# ── ① 单元：`! grep <pom内容>` 必须被识别为 pom-content 断言（GAP 本体）──

def test_is_pom_content_assert_recognizes_neg_grep():
    neg = f"! grep -qE '<artifactId>ruoyi-(common|system)</artifactId>' {_POM}"
    assert _is_pom_content_assert(neg, _POM), \
        "`! grep <pom>` 必须被识别为 pom-content 断言（否则被无条件 KEPT 绕过 reconcile，round65e2 死因）"


# ── ② 主治：reconcile 后 `! grep ruoyi-family` 与模板正断言不得共存（矛盾必消解）──

def test_reconcile_drops_neg_grep_conflicting_with_template():
    st = _st1()
    p = _plan(st)
    reconcile_template_exam(p)   # 直接改 st.harness
    vcs = list(st.harness.verify_commands)
    # 模板含 ruoyi-common → 正断言保留；与之互斥的 `! grep ruoyi-(...common...)` 负断言必须被剔除
    has_pos = any("grep -q '<artifactId>ruoyi-common</artifactId>'" in v for v in vcs)
    has_neg = any(v.strip().startswith("! grep") and "ruoyi-" in v for v in vcs)
    assert has_pos, f"模板依赖的正断言应保留: {vcs}"
    assert not has_neg, \
        f"与模板正面矛盾的 `! grep ruoyi-family` 负断言必须被剔除（考卷同源自洽，round65e2 死因）: {vcs}"


# ── ③ 保护：与模板【不冲突】的 `! grep` 负断言（禁入依赖守卫）必须保留 ──

def test_reconcile_keeps_nonconflicting_neg_grep():
    st = _st1()
    # 加一条禁入 log4j 的负断言——模板里没有 log4j，不冲突，应保留（最后一道牙齿）
    st.harness.verify_commands = list(_VERIFY) + [
        f"! grep -q '<artifactId>log4j</artifactId>' {_POM}"]
    p = _plan(st)
    reconcile_template_exam(p)
    vcs = list(st.harness.verify_commands)
    assert any("log4j" in v and v.strip().startswith("! grep") for v in vcs), \
        f"与模板不冲突的禁入负断言（log4j 不在模板）必须保留（禁入守卫最后一道牙齿）: {vcs}"


# ── ④ 猎手 F1 回归锁：<exclusions> 内的禁入 artifactId 不得被当成"必须有"的依赖 ──

def test_exclusions_not_treated_as_required_dep():
    from swarm.brain.contract_utils import _template_dep_artifacts
    tpl_excl = """<project><dependencies>
      <dependency><groupId>com.ruoyi</groupId><artifactId>ruoyi-common</artifactId>
        <exclusions><exclusion><artifactId>log4j</artifactId></exclusion></exclusions>
      </dependency>
    </dependencies></project>"""
    deps = _template_dep_artifacts(tpl_excl)
    assert "ruoyi-common" in deps, f"直接依赖应识别: {deps}"
    assert "log4j" not in deps, \
        f"<exclusions> 内的禁入 artifactId 绝不能被当成必须依赖（猎手 F1）: {deps}"
    # 且：禁 log4j 的 `! grep` 守卫不得被误剔（模板并不真含 log4j 依赖）
    pom = "svc/pom.xml"
    desc = (f"【权威 pom 模板（原样写入 {pom}）】\n```xml\n"
            + tpl_excl.replace("<project>", "<project>\n") + "\n```\n")
    h = TaskHarness(verify_commands=[f"! grep -q '<artifactId>log4j</artifactId>' {pom}"])
    sc = FileScope(writable=[], readable=[], create_files=[pom])
    st = SubTask(id="st-x", description=desc, difficulty=SubTaskDifficulty.MEDIUM,
                 modality=SubTaskModality.TEXT, scope=sc, harness=h)
    p = TaskPlan(subtasks=[st], parallel_groups=[["st-x"]])
    p.shared_contract = {"dependencies": [{"module": "svc"}]}
    reconcile_template_exam(p)
    assert any("log4j" in v and v.strip().startswith("! grep") for v in st.harness.verify_commands), \
        f"禁 log4j 守卫（模板仅 exclude 非 require log4j）必须保留（猎手 F1）: {st.harness.verify_commands}"


# ── ⑤ 猎手 F4 回归锁：acceptance NL "不包含 X 依赖"（X 是模板要求依赖）必须被剔除 ──

def test_acceptance_nl_exclusion_of_required_dep_dropped():
    st = _st1()
    p = _plan(st)
    reconcile_template_exam(p)
    acc = st.acceptance_criteria
    bad = [a for a in acc if ("不包含" in a and "ruoyi-common" in a)]
    assert not bad, \
        f"acceptance NL '不包含 ruoyi-common'（模板要求该依赖）必须被剔除（猎手 F4，防下游导回矛盾）: {acc}"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
