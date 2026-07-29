"""26 号文 F-H4：技能库两条——写死 GAV 与铁律对冲 + 专才技能泛滥推送。

实测 `skills_library/java-2fa-totp-shiro.md`：
· git log 自述"治 round65e5 st-53-1 2FA 失败"（考题答案）；
· 写死 `dev.samstevens.totp:totp:1.7.1` 完整 GAV 并明令"先在 pom 追加坐标（带显式
  version）"——与铁律**绝不猜依赖坐标**正面对冲；
· 1034 次 worker_push 中 **375 次（36%）** 推了它，包括写 Mapper/写测试等无关子任务。
"""
from __future__ import annotations

import pathlib

import pytest

from swarm.experience.library import load_skills
from swarm.experience.validation import _hardcoded_dependency_versions

_LIB = pathlib.Path(__file__).resolve().parent.parent / "skills_library"


def _skills():
    return [d for d in load_skills(str(_LIB)) if d is not None]


# ══════════════════════════════════════════════
# 写死依赖坐标：入库闸
# ══════════════════════════════════════════════

@pytest.mark.parametrize("text,label", [
    ("<artifactId>totp</artifactId>\n<version>1.7.1</version>", "Maven XML"),
    ("用 dev.samstevens.totp:totp:1.7.1", "冒号式坐标"),
    ("  <version>3.12.1</version>", "裸 version 行"),
])
def test_hardcoded_version_is_detected(text, label):
    """★技能跨项目复用，无从知道目标项目的 BOM/parent 管到哪个版本★
    写死的版本号必然在某些项目里是错的，而它以"照抄"的口吻下发给 worker。"""
    assert _hardcoded_dependency_versions(text), label


@pytest.mark.parametrize("text,label", [
    ("<version>${totp.version}</version>", "占位符"),
    ("<groupId>x</groupId><artifactId>y</artifactId>", "只给坐标不给版本（正是我们要的写法）"),
    ("ports: 127.0.0.1:5432:5432", "docker 端口映射"),
    ("image: postgres:16", "镜像 tag"),
])
def test_non_coordinates_are_not_flagged(text, label):
    """★闸不能矫枉过正★：端口映射 `127.0.0.1:5432:5432` 曾被误判成坐标——
    groupId/artifactId 段必须含字母，纯数字段一律不是坐标。"""
    assert not _hardcoded_dependency_versions(text), label


def test_whole_library_is_clean():
    """全库回归闸：任何技能都不得写死第三方依赖版本。
    新增技能违反时本条会红——这是"修一类先全仓捞 sibling"的落地。"""
    bad = {}
    for f in sorted(_LIB.glob("*.md")):
        parts = f.read_text().split("---", 2)
        hits = _hardcoded_dependency_versions(parts[2] if len(parts) > 2 else f.read_text())
        if hits:
            bad[f.name] = hits[:4]
    assert not bad, f"以下技能写死了依赖版本：{bad}"


def test_gate_is_an_error_not_a_warning():
    """★铁律冲突判死而非告警★：修法很轻（去掉 version 行、指向确定性解析通道），
    降成 warning 等于默许它继续下发给 worker。"""
    import inspect

    from swarm.experience import validation
    src = inspect.getsource(validation.validate_skill_doc)
    i = src.index("_hardcoded_dependency_versions(body)")
    assert "errors.append" in src[i:i + 400]


def test_maven_skills_still_teach_the_syntax():
    """★整改不能把教学内容也删了★：两篇 Maven 技能的价值恰恰是教"何时该写 version"，
    版本号改成占位符后语法照教、数字不给。"""
    txt = (_LIB / "maven-dependency-management.md").read_text()
    assert "<version>" in txt and "必须写显式 version" in txt
    assert "绝不猜依赖坐标" in txt, "整改处必须写明理由，否则后来者会改回去"


# ══════════════════════════════════════════════
# 专才技能泛滥推送——★本轮未治，如实登记（不是遗漏）★
# ══════════════════════════════════════════════
#
# 26 号文实测：java-2fa-totp-shiro 在 1034 次 worker_push 里被推了 375 次（36%），
# 包括写 Mapper、写测试这类与 2FA 毫无关系的子任务。根因是 `_task_hit` 只是【排序】维
# 而非【过滤】维——候选少时专才技能照样填满剩余槽位。
#
# ★为什么本轮不治★（停手判据，不是遗漏）：
# 试过用"库内标签文档频率"自算专才特征（低频标签＝专属触发词，一个都没命中就剔除），
# 想避开写死名单（denylist 必然"补一个漏一个"且违反多栈中立）。但实测本库 44 篇技能里
# **几乎每篇都是"多数标签低频"**（maven-dependency-management 6/8、redis 5/5、
# hexagonal 3/3……）——DF 阈值在这个规模的库上不具判别力。继续调阈值就会打红 6 条既有
# 行为测试（"Maven 脚手架必须够到 Maven 经验"、"非 Maven 栈也不能空手"），而那些测试
# 编码的是已验证过的真实要求。
# 此时对着测试调阈值＝本仓明列的**补丁磁铁**信号（同一处反复补丁 = 停手重设计）。
#
# ★正确的下一步★：这条判据的标定需要**真实 push 日志**（finding 本身就来自 1034 次
# 真实推送的统计）。应当先给 worker_push 落一份"推了哪几篇 × 子任务是什么"的机读账，
# 用真实分布定判据，而不是在 44 篇的小库上猜阈值。
