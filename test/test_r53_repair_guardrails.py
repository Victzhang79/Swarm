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
