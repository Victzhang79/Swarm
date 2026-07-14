"""R53-3/5/6 治本锁：确定性修复层的三条护栏（五轮日志实锤）。

- R53-3 版本注入必须落稳定版：实测注入过 spring-boot-starter-aop:4.0.0-M2 /
  shiro-core:3.0.0-alpha-1 / commons-collections4:4.5.0-M3 / spring-security-core:7.1.0-RC1
  → L1 侧"修好了"，L2 集成真炸；更毒的是对抗复核随后把这些版本算成 worker 擅自硬编码。
- R53-5 symbol-repair 绝不跨角色改写：`IAlarmBotService→alarmBotService`（距=2）、
  `super→user`（距=2 频=425）——确定性修复主动把代码改坏，再被连坐层结算成 63 个子任务放弃。
- R53-6 "备选模型"必须排除**实跑主力池**：三轮 retry_alternate 全派回刚挂的同一个模型。
"""
from __future__ import annotations

from swarm.worker.l1_parse import _choose_valid_version, pick_latest_stable, stable_versions
from swarm.worker.l1_pipeline import _JVM_KEYWORDS, _SYMBOL_ERR_RE, _same_role


# ── R53-3：稳定版优先 ────────────────────────────────────────────────────────
def test_pick_latest_stable_skips_milestone_and_rc():
    avail = ["2.7.18", "3.0.0-alpha-1", "3.2.5", "4.0.0-M2", "4.0.0-RC1", "4.5.0-M3"]
    assert pick_latest_stable(avail) == "3.2.5", "最高版是里程碑 → 必须落回最新稳定版"


def test_stable_filter_falls_back_when_only_prereleases_exist():
    """全是预发布 → 原样返回（那是该 artifact 的真实现状，不是我们瞎选）。"""
    avail = ["1.0.0-M1", "1.0.0-RC2"]
    assert stable_versions(avail) == avail
    assert pick_latest_stable(avail) == "1.0.0-RC2"


def test_choose_valid_version_never_lands_on_prerelease():
    """版本写错的校正路径同样只落稳定版（旧实现 max() 会选中 4.0.0-M2）。"""
    avail = ["3.1.0", "3.2.5", "4.0.0-M2"]
    assert _choose_valid_version("9.9.9", avail) == "3.2.5"
    assert _choose_valid_version("3.1.5", avail) == "3.1.0", "≤目标的最高稳定版"


# ── R53-5：symbol-repair 角色/关键字护栏 ─────────────────────────────────────
def test_symbol_error_regex_captures_role():
    """编译器已给出角色（class/variable/method）——旧正则用非捕获组把它丢了。"""
    out = _SYMBOL_ERR_RE.findall(
        "/x/A.java:[12,5] cannot find symbol\n  symbol:   class IAlarmBotService\n")
    assert out == [("/x/A.java", "class", "IAlarmBotService")]


def test_type_never_rewritten_to_variable_name():
    """★头号锁★ 类型名绝不能被改写成小驼峰变量名（round50b：IAlarmBotService→alarmBotService）。"""
    assert not _same_role("class", "IAlarmBotService", "alarmBotService")
    assert not _same_role("class", "AlarmBot", "alarmBot")
    assert _same_role("class", "AlarmBot", "AlarmBots"), "类型→类型仍允许（真拼写错要能修）"


def test_variable_never_promoted_to_type_name():
    assert not _same_role("variable", "alarmBot", "AlarmBot")
    assert _same_role("method", "isEmtpy", "isEmpty"), "同角色的真 typo 仍必须能修"


def test_language_keywords_are_never_rewrite_targets_or_sources():
    """`super→user`（round52 实锤）：关键字是用法错，不是拼写错，改它只会更坏。"""
    assert "super" in _JVM_KEYWORDS and "class" in _JVM_KEYWORDS and "this" in _JVM_KEYWORDS


# ── R53-6：备选模型必须排除实跑主力池 ────────────────────────────────────────
def test_alternate_excludes_actual_parallel_pool(monkeypatch):
    """三轮 retry_alternate 全换回刚挂的模型（日志照打"使用备选模型"）→ 恢复阶梯形同虚设。"""
    from swarm.models import router as R

    r = R.ModelRouter.__new__(R.ModelRouter)
    r.config = type("C", (), {"routing_trivial": "Saka"})()
    monkeypatch.setattr(R.ModelRouter, "_resolve_route",
                        lambda self, d, m="text": ("MiniMax", ["Qwopus", "Kimi", "Saka"]))

    class _W:
        worker_parallel_pool = ["Qwopus"]          # 实跑主力（池轮转派的就是它）

    monkeypatch.setattr(R, "get_config", lambda: type("G", (), {"worker": _W()})(), raising=False)
    monkeypatch.setitem(__import__("sys").modules, "swarm.config",
                        type("M", (), {"get_config": lambda: type("G", (), {"worker": _W()})()}))

    cands = r._alternate_candidates("medium")
    assert "Qwopus" not in cands, "★实跑主力绝不能当自己的备选★"
    assert "Saka" not in cands, "trivial 档主力仍被排除（换模型≠降级到最弱档）"
    assert cands == ["Kimi"]


# ── R54-6：reactor 内部模块被安上臆造 groupId（round54 实锤，逃过 R53-2） ────────
POM_WRONG_GROUP = """<project>
    <artifactId>alarm-schedule</artifactId>
    <dependencies>
        <dependency>
            <groupId>com.alarm</groupId>
            <artifactId>alarm-core</artifactId>
            <version>4.8.3</version>
        </dependency>
        <dependency>
            <groupId>cn.hutool</groupId>
            <artifactId>hutool-all</artifactId>
            <version>5.8.47</version>
        </dependency>
    </dependencies>
</project>
"""


def test_reactor_module_dep_group_is_rewritten_to_project_group():
    """★ artifactId 是 reactor 成员 → groupId 只能是工程自己的（模块由本工程构建）。

    round54 实锤：`com.alarm:alarm-core` → Maven 当外部依赖去远程仓库拉 →
    `Could not find artifact com.alarm:alarm-core:jar:4.8.3` → 整模块解析失败。
    它**有** version、artifactId **确实是**真模块 → 逃过 R53-2 的幻影剪除。
    """
    from swarm.worker.l1_pipeline import _fix_reactor_dep_group

    out = _fix_reactor_dep_group(POM_WRONG_GROUP, "alarm-core", "com.ruoyi", {"alarm-core"})
    assert out is not None
    assert "<groupId>com.ruoyi</groupId>" in out
    assert "com.alarm" not in out, "臆造 groupId 必须被改掉"
    assert "cn.hutool" in out and "5.8.47" in out, "真第三方依赖不得被误伤"


def test_reactor_group_fix_is_idempotent_and_skips_correct_ones():
    from swarm.worker.l1_pipeline import _fix_reactor_dep_group

    good = POM_WRONG_GROUP.replace("com.alarm", "com.ruoyi")
    assert _fix_reactor_dep_group(good, "alarm-core", "com.ruoyi", {"alarm-core"}) is None, \
        "已正确 → 不动（幂等）"
    # fail-closed 自守门：不是 reactor 成员 → 绝不改写（否则本函数就成了伪造坐标的工具）
    assert _fix_reactor_dep_group(
        POM_WRONG_GROUP, "hutool-all", "com.ruoyi", {"alarm-core"}) is None, \
        "第三方 artifact 绝不能被安上工程 groupId（那正是 R47-2 禁的伪造）"


# ── R54-5：稳定 ≠ 兼容（版本必须与工程同代对齐） ─────────────────────────────
ROOT_POM_BOOT4 = """<project>
    <groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId><version>4.8.3</version>
    <properties><spring-boot.version>4.0.6</spring-boot.version></properties>
    <dependencyManagement><dependencies>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-dependencies</artifactId>
            <version>${spring-boot.version}</version>
            <type>pom</type><scope>import</scope>
        </dependency>
    </dependencies></dependencyManagement>
</project>
"""


def test_group_family_version_resolves_property(tmp_path, monkeypatch):
    """工程为某 groupId 钉的版本（含 ${prop} 展开）= 注入时唯一正确的对齐目标。"""
    import swarm.worker.l1_pipeline as lp
    (tmp_path / "pom.xml").write_text(ROOT_POM_BOOT4, encoding="utf-8")
    monkeypatch.setattr(lp, "_read_project_file",
                        lambda p, rel, timeout=20: (tmp_path / rel).read_text("utf-8")
                        if (tmp_path / rel).is_file() else None)
    assert lp._group_family_version(str(tmp_path), "org.springframework.boot") == "4.0.6"
    assert lp._group_family_version(str(tmp_path), "cn.hutool") is None, "无先例 → None（走最新稳定版）"


# ── R56-3：同 groupId ≠ 同发布列车（round56 活体误伤，修正 R54-5） ─────────────
def test_umbrella_group_never_triggers_generation_prune():
    """★ `com.alibaba` 是伞形 groupId：druid(1.2.28) / easyexcel(4.0.3) / fastjson 彼此无版本关系。

    round56 活体误伤：R54-5 拿"工程 com.alibaba 钉在 1.2.28"判定 easyexcel(4.0.3) 跨代 →
    **把合法依赖直接剪掉**（代码用到它就编译失败）。判据必须收紧为【同发布列车】。
    """
    from swarm.worker.l1_pipeline import _same_release_train

    assert _same_release_train("spring-boot-starter-aop", "spring-boot-dependencies"), \
        "同列车（共享 spring-boot 前缀）→ 版本必须对齐"
    assert not _same_release_train("easyexcel", "druid-spring-boot-4-starter"), \
        "★不同产品线绝不能被当成同一代（那会剪掉合法依赖）"
    assert not _same_release_train("fastjson2", "fastjson"), "只共享 1 段词元不算同列车"


def test_family_version_skips_unrelated_artifact_in_same_group(tmp_path, monkeypatch):
    """伞形 group 下，目标 artifact 与已钉 artifact 无公共前缀 → 不返回家族版本（走最新稳定版）。"""
    import swarm.worker.l1_pipeline as lp

    (tmp_path / "pom.xml").write_text(
        "<project><dependencyManagement><dependencies>"
        "<dependency><groupId>com.alibaba</groupId>"
        "<artifactId>druid-spring-boot-4-starter</artifactId>"
        "<version>1.2.28</version></dependency>"
        "</dependencies></dependencyManagement></project>", encoding="utf-8")
    monkeypatch.setattr(lp, "_read_project_file",
                        lambda p, rel, timeout=20: (tmp_path / rel).read_text("utf-8")
                        if (tmp_path / rel).is_file() else None)

    assert lp._group_family_version(str(tmp_path), "com.alibaba", "easyexcel") is None, \
        "★easyexcel 与 druid 不同列车 → 不得对齐到 1.2.28（否则合法依赖被剪）"
    assert lp._group_family_version(
        str(tmp_path), "com.alibaba", "druid-spring-boot-starter") == "1.2.28", \
        "同列车（druid-spring-boot-*）→ 正常对齐"


# ── R56-4：**有 version 的**幻影坐标（round56 实锤，从所有闸门缝里钻过去） ──────
POM_PHANTOM_WITH_VERSION = """<project>
    <artifactId>ruoyi-alarm-admin</artifactId>
    <dependencies>
        <dependency>
            <groupId>com.ruoyi</groupId>
            <artifactId>ruoyi-alarm-system</artifactId>
            <version>4.8.3</version>
        </dependency>
        <dependency>
            <groupId>com.ruoyi</groupId>
            <artifactId>ruoyi-common</artifactId>
            <version>4.8.3</version>
        </dependency>
    </dependencies>
</project>
"""


def test_prune_can_cut_phantom_that_carries_a_version():
    """★ 有 version 的幻影必须能剪 ★

    round56 实锤：`com.ruoyi:ruoyi-alarm-system:4.8.3` —— 用工程自己的 groupId（R54-6 无从改）、
    带着 version（R53-2 不剪）、仓库查无（version-repair 静默跳过）→ `Could not resolve
    dependencies` → 整个模块解析失败、连坐下游。工程模块从不在远程仓库里 = **可证永不可解析**。
    """
    from swarm.worker.l1_pipeline import _prune_dep_blocks

    out = _prune_dep_blocks(POM_PHANTOM_WITH_VERSION, "com.ruoyi", "ruoyi-alarm-system",
                            even_with_version=True)
    assert out is not None and "ruoyi-alarm-system" not in out, "幻影模块坐标必须被剪除"
    assert "ruoyi-common" in out, "真 reactor 模块依赖不得被误伤"


def test_default_prune_still_refuses_to_cut_versioned_deps():
    """默认（保守）仍只剪无版本的——带版本的坏依赖顶多局部解析失败，不扩大打击面。"""
    from swarm.worker.l1_pipeline import _prune_dep_blocks

    assert _prune_dep_blocks(
        POM_PHANTOM_WITH_VERSION, "com.ruoyi", "ruoyi-alarm-system") is None, \
        "未显式开 even_with_version → 有版本的依赖绝不动"
