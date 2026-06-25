#!/usr/bin/env python3
"""Maven 依赖版本不存在 → 确定性校正（防线③）纯函数单测。

治本场景：worker 实现新功能时凭空写了不存在的依赖版本号（如 googleauth:1.5.2，
实际最高 1.5.0）→ mvn 任何仓库拉不到 → build-repair 撞墙到迭代上限 → L1 死循环。
本测试钉住版本解析/比较/选择/artifact 解析的真值，确保校正逻辑模型无关、可复现。
"""
from __future__ import annotations

from swarm.worker.l1_pipeline import (
    _choose_valid_version,
    _ver_key,
    parse_missing_artifacts,
)


# ── 版本号比较 ──
def test_ver_key_numeric_ordering():
    assert _ver_key("1.5.0") < _ver_key("1.5.2")
    assert _ver_key("1.5.0") < _ver_key("1.10.0")  # 数字段按整数比，非字典序
    assert _ver_key("2.0.0") > _ver_key("1.9.9")


def test_ver_key_handles_qualifiers():
    # 带后缀（RELEASE/Final 等）不抛异常，可比较
    assert _ver_key("1.0.0") < _ver_key("1.0.1")
    _ver_key("1.0.0-RELEASE")  # 不抛
    _ver_key("4.0.6")  # 不抛


# ── 选最近有效版本 ──
def test_choose_picks_highest_le_target():
    # googleauth 真实案例：写 1.5.2，可用最高 1.5.0 → 选 1.5.0（≤目标最高）
    avail = ["0.4.3", "1.0.0", "1.4.0", "1.5.0"]
    assert _choose_valid_version("1.5.2", avail) == "1.5.0"


def test_choose_returns_none_when_version_exists():
    # 版本其实存在 → 不是版本问题，返回 None（绝不误修）
    avail = ["1.4.0", "1.5.0", "1.5.2"]
    assert _choose_valid_version("1.5.2", avail) is None


def test_choose_returns_none_when_no_available():
    assert _choose_valid_version("1.5.2", []) is None


def test_choose_falls_back_to_highest_when_target_below_all():
    # 目标比所有可用都低 → 取最高可用（让构建至少能拉到）
    avail = ["2.0.0", "2.1.0", "2.2.0"]
    assert _choose_valid_version("1.0.0", avail) == "2.2.0"


def test_choose_exact_middle_version():
    avail = ["1.0.0", "1.1.0", "1.2.0", "1.3.0"]
    assert _choose_valid_version("1.2.5", avail) == "1.2.0"


# ── build 输出解析 artifact ──
def test_parse_could_not_find_artifact():
    out = ("[ERROR] Failed to execute goal on project ruoyi-system: Could not resolve "
           "dependencies for project com.ruoyi:ruoyi-system:jar:4.8.3: Could not find "
           "artifact com.warrenstrange:googleauth:jar:1.5.2 in public")
    arts = parse_missing_artifacts(out)
    assert ("com.warrenstrange", "googleauth", "1.5.2") in arts


def test_parse_failure_to_find():
    out = ("Failure to find com.warrenstrange:googleauth:jar:1.5.2 in "
           "https://maven.aliyun.com/repository/public was cached in the local repository")
    arts = parse_missing_artifacts(out)
    assert ("com.warrenstrange", "googleauth", "1.5.2") in arts


def test_parse_dedupes():
    out = ("Could not find artifact a.b:c:jar:1.0\n"
           "Could not find artifact a.b:c:jar:1.0\n"
           "Could not find artifact x.y:z:jar:2.0")
    arts = parse_missing_artifacts(out)
    assert arts.count(("a.b", "c", "1.0")) == 1
    assert ("x.y", "z", "2.0") in arts


def test_parse_empty_on_clean_output():
    assert parse_missing_artifacts("BUILD SUCCESS") == []
    assert parse_missing_artifacts("") == []


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ {fn.__name__}: {e}")
            fails += 1
    sys.exit(1 if fails else 0)
