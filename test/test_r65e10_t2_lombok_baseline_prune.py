"""R65E10-T2（round65e10 FAILED@执行期 四路定案·死因②）：基线无 Lombok 时，契约依赖里的
lombok 被 H1 权威 pom 模板确定性注入 pom → 撞 T5 grounding 派生的验收 `! grep -rq 'lombok'
<module-dir>/`（禁令，【正确侧】：基线 0 lombok）→ 每轮确定性不可赢 → st-1 head-of-line 连坐全 92。

矛盾双源（四路定案）：
- 错侧=lombok 进 pom：prompts.py:222 硬编码示例 `org.projectlombok:lombok` + P7"宁多勿漏"steer
  planner 把 lombok 写进 shared_contract.dependencies → _generate 模块 pom 模板(contract_utils:404)
  从契约 artifacts 确定性渲染进 pom。
- 对侧=`! grep -rq lombok`：来自 T5 grounding"基线无 lombok→禁用"，planner 据此写的验收禁令。

治（源头·确定性·正确方向）：基线 lombok_available=False 时，PLAN 期（模板生成前）从
shared_contract.dependencies 剥除 lombok 坐标 → 模板/pom 不含 lombok → 禁令验收通过 →
交付与基线约定一致（手写 getter，无 lombok）。禁令继续守"代码不得用 lombok"。fail-open：
无法判定基线（路径缺/异常）→ 保守【不剥】（假定在位，绝不误删真在用 lombok 致编译断裂）。
"""
from __future__ import annotations

from swarm.brain.contract_utils import prune_baseline_absent_dependencies
from swarm.brain.stack_detect import baseline_lombok_present
from swarm.types import TaskPlan


def _write(p, txt):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(txt, encoding="utf-8")


_ROOT_POM_NO_LOMBOK = """<?xml version="1.0"?>
<project><modelVersion>4.0.0</modelVersion>
  <groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId><version>4.8.3</version>
  <dependencies>
    <dependency><groupId>org.apache.shiro</groupId><artifactId>shiro-core</artifactId></dependency>
  </dependencies>
</project>"""

_ROOT_POM_WITH_LOMBOK = """<?xml version="1.0"?>
<project><modelVersion>4.0.0</modelVersion>
  <groupId>com.acme</groupId><artifactId>acme</artifactId><version>1.0</version>
  <dependencies>
    <dependency><groupId>org.projectlombok</groupId><artifactId>lombok</artifactId></dependency>
  </dependencies>
</project>"""

_ROOT_POM_LOMBOK_ONLY_IN_EXCLUSION = """<?xml version="1.0"?>
<project><modelVersion>4.0.0</modelVersion>
  <groupId>com.acme</groupId><artifactId>acme</artifactId><version>1.0</version>
  <dependencies>
    <dependency><groupId>x</groupId><artifactId>some-starter</artifactId>
      <exclusions><exclusion><groupId>org.projectlombok</groupId><artifactId>lombok</artifactId></exclusion></exclusions>
    </dependency>
  </dependencies>
</project>"""


# ── baseline_lombok_present ──
def test_baseline_no_lombok_false(tmp_path):
    _write(tmp_path / "pom.xml", _ROOT_POM_NO_LOMBOK)
    assert baseline_lombok_present(str(tmp_path)) is False


def test_baseline_with_lombok_true(tmp_path):
    _write(tmp_path / "pom.xml", _ROOT_POM_WITH_LOMBOK)
    assert baseline_lombok_present(str(tmp_path)) is True


def test_baseline_lombok_only_in_exclusion_is_false(tmp_path):
    """猎手 F2 同律：lombok 只出现在 <exclusions>（挡传递）≠ 基线启用 → False。"""
    _write(tmp_path / "pom.xml", _ROOT_POM_LOMBOK_ONLY_IN_EXCLUSION)
    assert baseline_lombok_present(str(tmp_path)) is False


def test_baseline_lombok_via_java_import(tmp_path):
    _write(tmp_path / "pom.xml", _ROOT_POM_NO_LOMBOK)
    _write(tmp_path / "src/main/java/A.java", "import lombok.Data;\n@Data class A {}")
    assert baseline_lombok_present(str(tmp_path)) is True


def test_baseline_bad_path_none(tmp_path):
    """路径无 pom → None（无法判定，调用方 fail-open 不剥）。"""
    assert baseline_lombok_present(str(tmp_path / "nonexistent")) is None


# ── prune_baseline_absent_dependencies ──
def _plan_with_lombok():
    p = TaskPlan(subtasks=[], parallel_groups=[])
    p.shared_contract = {"dependencies": [
        {"module": "ruoyi-alarm-interface",
         "artifacts": ["org.projectlombok:lombok", "cn.hutool:hutool-all",
                       "com.squareup.okhttp3:okhttp"]},
    ]}
    return p


def test_prune_strips_lombok_when_baseline_absent(tmp_path):
    """★核心 RED★ 基线无 lombok → 从契约 artifacts 剥除 lombok，保留其余。"""
    _write(tmp_path / "pom.xml", _ROOT_POM_NO_LOMBOK)
    p = _plan_with_lombok()
    dropped = prune_baseline_absent_dependencies(p, str(tmp_path))
    arts = p.shared_contract["dependencies"][0]["artifacts"]
    assert not any("lombok" in a for a in arts), f"lombok 应被剥除: {arts}"
    assert "cn.hutool:hutool-all" in arts and "com.squareup.okhttp3:okhttp" in arts
    assert any("lombok" in a for a in dropped.get("ruoyi-alarm-interface", []))


def test_prune_keeps_lombok_when_baseline_present(tmp_path):
    """基线用 lombok → 不剥（真需要）。"""
    _write(tmp_path / "pom.xml", _ROOT_POM_WITH_LOMBOK)
    p = _plan_with_lombok()
    prune_baseline_absent_dependencies(p, str(tmp_path))
    arts = p.shared_contract["dependencies"][0]["artifacts"]
    assert any("lombok" in a for a in arts), "基线在用 lombok 时不得剥"


def test_prune_fail_open_bad_path(tmp_path):
    """基线无法判定（无 pom）→ 保守不剥（绝不误删真在用 lombok 致编译断裂）。"""
    p = _plan_with_lombok()
    prune_baseline_absent_dependencies(p, str(tmp_path / "nope"))
    arts = p.shared_contract["dependencies"][0]["artifacts"]
    assert any("lombok" in a for a in arts), "无法判定基线时 fail-open 保留"


def test_prune_no_contract_noop(tmp_path):
    _write(tmp_path / "pom.xml", _ROOT_POM_NO_LOMBOK)
    p = TaskPlan(subtasks=[], parallel_groups=[])
    assert prune_baseline_absent_dependencies(p, str(tmp_path)) == {}


def test_prune_cleans_existing_pre_prune_snapshot(tmp_path):
    """★复核 HIGH 回归锁★ 前轮已建 artifacts_pre_prune（含 lombok）→ 本剥除【也清它】，
    否则 prune_contract_dependencies 的"可解析复原"分支会从快照把 lombok 复活。"""
    _write(tmp_path / "pom.xml", _ROOT_POM_NO_LOMBOK)
    p = _plan_with_lombok()
    p.shared_contract["dependencies"][0]["artifacts_pre_prune"] = [
        "org.projectlombok:lombok", "cn.hutool:hutool-all"]
    prune_baseline_absent_dependencies(p, str(tmp_path))
    entry = p.shared_contract["dependencies"][0]
    assert not any("lombok" in a for a in entry.get("artifacts_pre_prune", [])), \
        "既有快照的 lombok 必须同步清除（防下游复原源复活）"


def test_widened_matcher_strips_lombok_substring_artifact(tmp_path):
    """★复核回归锁★ 匹配与 `! grep -rq lombok` 同语义（子串/大小写不敏感）——
    com.foo:lombok-utils / org.projectlombok:Lombok 进 pom 同样触发禁令，须一并剥。"""
    _write(tmp_path / "pom.xml", _ROOT_POM_NO_LOMBOK)
    p = TaskPlan(subtasks=[], parallel_groups=[])
    p.shared_contract = {"dependencies": [
        {"module": "m", "artifacts": ["com.foo:lombok-utils", "org.projectlombok:Lombok",
                                      "cn.hutool:hutool-all"]}]}
    prune_baseline_absent_dependencies(p, str(tmp_path))
    arts = p.shared_contract["dependencies"][0]["artifacts"]
    assert arts == ["cn.hutool:hutool-all"], f"含 lombok 子串坐标应全剥: {arts}"


def test_full_pipeline_no_lombok_restore_across_passes(tmp_path):
    """★复核 HIGH 核心回归锁★ 完整咽喉两遍（inject 两遍/外科重试）后 lombok 绝不复活。
    模拟真实序：prune_baseline → prune_contract_dependencies（可解析复原分支），重复两遍。"""
    from swarm.brain.contract_utils import (
        prune_baseline_absent_dependencies as _pb,
        prune_contract_dependencies as _pc,
    )
    _write(tmp_path / "pom.xml", _ROOT_POM_NO_LOMBOK)
    p = _plan_with_lombok()

    def _one_pass():
        _pb(p, str(tmp_path))           # T2 先（复核 HIGH 顺序）
        try:
            _pc(p, str(tmp_path))       # 再 T6（可解析复原不得复活 lombok）
        except Exception:
            pass                        # 解析器可能网络不可达；本测只验 lombok 不复活
        return p.shared_contract["dependencies"][0]["artifacts"]

    a1 = _one_pass()
    a2 = _one_pass()
    assert not any("lombok" in a for a in a1), f"pass1 lombok 应绝迹: {a1}"
    assert not any("lombok" in a for a in a2), f"pass2 lombok 不得复活（复核 HIGH）: {a2}"


def test_undeterminable_records_degrade(tmp_path, monkeypatch):
    """★复核 MED 回归锁★ 基线无法判定（None）→ record_degrade（探测失败 vs 真无 lombok 可分）。"""
    cats = []
    # prune 内 `from swarm.brain.stack_detect import baseline_lombok_present` → patch 源模块
    monkeypatch.setattr("swarm.brain.stack_detect.baseline_lombok_present", lambda _p: None)
    monkeypatch.setattr("swarm.infra.degrade.record_degrade",
                        lambda c, *a, **k: cats.append(c))
    p = _plan_with_lombok()
    prune_baseline_absent_dependencies(p, str(tmp_path))
    assert any("lombok_baseline_undeterminable" in c for c in cats), \
        f"None 路径必须 record_degrade: {cats}"
    # fail-open：无法判定 → 不剥
    assert any("lombok" in a for a in p.shared_contract["dependencies"][0]["artifacts"])
