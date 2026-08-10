"""LOW 收口批双复核 R1 整改（无家族落点的两条）：

- l1_pipeline 三处 sibling 归一（hunter 3.1/3.2）：`_norm_src_path` /
  `_module_pom_for_file` / `_manifest_dir_for` 原 `lstrip("./")` 字符集把点前导目录
  （.mvn/.github/.yarn）吃成无前导——与 modified 侧（点前导保留）比对恒失败。
  统一到 `_norm_rel`（只剥字面 "./" 前缀）。
- infra/db.py `_pool_size` 越界钳位（hunter 2.1）：负值能过 int() 不触发 ValueError
  ⇒ 旧 max() 静默钳位；钳位同属降级，必须 WARNING 可观测（铁律#3 sibling 对齐）。
"""
from __future__ import annotations

import logging

import swarm.infra.db as db
import swarm.worker.l1_pipeline as l1p


# ── l1_pipeline 归一 sibling ×3 ──

def test_norm_src_path_preserves_dot_leading():
    """点前导目录不再被吃：/workspace/.mvn/wrapper/X → .mvn/wrapper/X
    （治前 lstrip("./") 字符集产出 mvn/wrapper/X，与 modified 侧比对恒失败）。"""
    assert l1p._norm_src_path("/workspace/.mvn/wrapper/M.java") == ".mvn/wrapper/M.java"
    assert l1p._norm_src_path("/workspace/.github/workflows/ci.yml") == \
        ".github/workflows/ci.yml"
    # 正常形态回归：/workspace 前缀与 ./ 前缀照剥
    assert l1p._norm_src_path("/workspace/ruoyi-alarm/src/A.java") == "ruoyi-alarm/src/A.java"
    assert l1p._norm_src_path("./mod/B.java") == "mod/B.java"


def test_module_pom_for_file_preserves_dot_leading(monkeypatch):
    """find 输出 "./pom.xml" 与点前导模块路径的归一不再被字符集 lstrip 吃掉。"""
    monkeypatch.setattr(l1p, "_run_check_split",
                        lambda *a, **k: (0, "./pom.xml\n", ""))
    assert l1p._module_pom_for_file("/proj", "x/A.java", 15) == "pom.xml"
    monkeypatch.setattr(l1p, "_run_check_split",
                        lambda *a, **k: (0, ".mvn/pom.xml\n", ""))
    assert l1p._module_pom_for_file("/proj", "x/A.java", 15) == ".mvn/pom.xml", \
        "点前导模块目录不得被吃成 mvn/pom.xml"


def test_manifest_dir_for_dot_leading(tmp_path):
    """_manifest_dir_for 对点前导目录内的改动文件能找到其清单（治前目录段被吃）。"""
    (tmp_path / ".yarn" / "releases").mkdir(parents=True)
    (tmp_path / ".yarn" / "package.json").write_text("{}", "utf-8")
    got = l1p._manifest_dir_for([".yarn/releases/plugin.js"], ("package.json",),
                                str(tmp_path))
    assert got == ".yarn", f"点前导目录的清单归属被吃: {got}"


# ── db._pool_size 越界钳位可观测 ──

def test_pool_size_negative_clamp_warns(monkeypatch, caplog):
    """负值过 int() 不触发 ValueError——钳位属降级必须 WARNING（治前 max() 静默钳位）。"""
    monkeypatch.setenv("SWARM_DB_POOL_MIN", "-5")
    monkeypatch.setenv("SWARM_DB_POOL_MAX", "-1")
    with caplog.at_level(logging.WARNING):
        pmin, pmax = db._pool_size()
    assert (pmin, pmax) == (0, 1), f"钳位语义不变: {(pmin, pmax)}"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("POOL_MIN" in m and "越界" in m for m in msgs), f"MIN 钳位无 WARNING: {msgs}"
    assert any("POOL_MAX" in m and "越界" in m for m in msgs), f"MAX 钳位无 WARNING: {msgs}"


def test_pool_size_legal_values_no_clamp_warn(monkeypatch, caplog):
    """合法值零 WARNING（防过宽告警淹没真信号）+ min>max 收敛语义不变。"""
    monkeypatch.setenv("SWARM_DB_POOL_MIN", "2")
    monkeypatch.setenv("SWARM_DB_POOL_MAX", "8")
    with caplog.at_level(logging.WARNING):
        assert db._pool_size() == (2, 8)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
