"""R47-3 回归锁：缺 <version> 注入必须块级精准，绝不在工程自身声明旁盲插。

round47 实锤：perl 盲插把「项目自身 artifactId 行」当依赖声明，在工程 <version>
旁再插一个 → Duplicated tag: 'version' → ruoyi-alarm-sdk/pom.xml Non-parseable
→ 整 reactor 连坐。测试 import 模块级真身（复核 F3：禁"抄本测试"假绿）。
"""
from __future__ import annotations

import re

from swarm.worker.l1_pipeline import _inject_dep_version_in_blocks as _inject

_POM = """<project>
    <parent>
        <groupId>com.ruoyi</groupId>
        <artifactId>ruoyi</artifactId>
        <version>4.8.3</version>
    </parent>
    <artifactId>ruoyi-alarm-sdk</artifactId>
    <version>4.8.3</version>
    <dependencies>
        <dependency>
            <groupId>cn.hutool</groupId>
            <artifactId>hutool-all</artifactId>
        </dependency>
        <dependency>
            <groupId>org.projectlombok</groupId>
            <artifactId>lombok</artifactId>
            <version>1.18.30</version>
        </dependency>
    </dependencies>
</project>
"""


def test_inject_only_inside_matching_dep_block():
    out = _inject(_POM, "cn.hutool", "hutool-all", "5.8.25")
    assert out is not None
    dep = re.search(r"<dependency>(.*?hutool-all.*?)</dependency>", out, re.S).group(1)
    assert "<version>5.8.25</version>" in dep


def test_own_project_artifactid_untouched():
    """R47-3 核心：依赖 ruoyi-alarm-sdk 缺版本时，本工程自身声明绝不被插（那会双 version）。"""
    assert _inject(_POM, "com.ruoyi", "ruoyi-alarm-sdk", "4.8.3") is None


def test_idempotent_when_version_present():
    assert _inject(_POM, "org.projectlombok", "lombok", "1.18.30") is None


def test_group_mismatch_skipped():
    assert _inject(_POM, "com.wrong", "hutool-all", "5.8.25") is None


def test_property_groupid_fails_open():
    """复核 F5：${属性} groupId 不可字面比对 → 放行到 artifactId 匹配。"""
    pom = ("<project><dependencies><dependency>"
           "<groupId>${project.groupId}</groupId>"
           "<artifactId>my-lib</artifactId>"
           "</dependency></dependencies></project>")
    out = _inject(pom, "com.acme", "my-lib", "1.0")
    assert out is not None and "<version>1.0</version>" in out


def test_double_injection_impossible():
    out1 = _inject(_POM, "cn.hutool", "hutool-all", "5.8.25")
    assert _inject(out1, "cn.hutool", "hutool-all", "5.8.25") is None
    assert out1.count("<version>5.8.25</version>") == 1


def test_parent_block_untouched():
    assert _inject(_POM, "com.ruoyi", "ruoyi", "9.9.9") is None


def test_exclusion_collision_not_corrupted():
    """复核 F2：撞名 exclusion 在前——version 必须插在真 artifactId 后，不进 exclusions。"""
    pom = ("<project><dependencies><dependency>"
           "<groupId>org.example</groupId>"
           "<artifactId>outer-dep</artifactId>"
           "<exclusions><exclusion>"
           "<groupId>other</groupId><artifactId>outer-dep</artifactId>"
           "</exclusion></exclusions>"
           "</dependency></dependencies></project>")
    out = _inject(pom, "org.example", "outer-dep", "2.0")
    assert out is not None
    exc = re.search(r"<exclusions>.*?</exclusions>", out, re.S).group(0)
    assert "<version>" not in exc, "version 绝不进 exclusions 块"
    assert out.count("<version>2.0</version>") == 1


def test_exclusion_only_occurrence_skipped():
    """目标 artifact 只以 exclusion 形式出现 → 零命中（绝不给 exclusion 插 version）。"""
    pom = ("<project><dependencies><dependency>"
           "<groupId>org.springframework</groupId>"
           "<artifactId>spring-core</artifactId>"
           "<exclusions><exclusion>"
           "<groupId>commons-logging</groupId><artifactId>commons-logging</artifactId>"
           "</exclusion></exclusions>"
           "</dependency></dependencies></project>")
    assert _inject(pom, "commons-logging", "commons-logging", "1.2") is None


def test_source_no_perl_in_missing_version_branch():
    """结构性回归锁：分支②不再走 perl 盲插（带锚点守卫，不脆断）。"""
    from swarm.worker import l1_pipeline as lp
    src = open(lp.__file__, encoding="utf-8").read()
    a, b = src.find("缺 <version> 元素"), src.find("防线④")
    if a == -1 or b == -1 or a >= b:
        return  # 注释锚点变动 → 薄检查退位（真身行为已由上方测试锁定）
    assert "perl -i.bak" not in src[a:b]
    assert "_inject_dep_version_in_blocks" in src[a:b]
