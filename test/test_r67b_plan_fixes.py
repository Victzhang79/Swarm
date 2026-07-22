"""round67b 深读治本用例（红灯先行）。

R67B-T1：file_plan 跨模块 create 归属重规范化（plan_batch.renormalize_cross_module_creates）
  round67b 死因：2FA 切片 create 公共工具类进 ruoyi-common/（架构正确），module 标签却写
  ruoyi-system → G1 违①双根 REJECT → 反馈打回 PLAN 而 LLM 结构性改不了 file_plan 归属
  → 3 轮空转 rejected。#40 豁免只认 modify 既有文件，create 进既有基线模块目录是缺口，
  且它可确定性自愈（模块=物理路径铁律，标签只是标签）。

R67B-T2：STAGE2 显式零改造出口（planning_nodes._stage2_zero_change_plausible）
  round67b 实锤：k3 对 ruoyi-generator 9 次返回结构良好 {"file_plan": []} + 充分理由
  （磁盘既有生成器完整可复用），全部被"file_plan 为空"判失败 → 3 轮外科白烧 → degraded
  假故障。诚实空申报必须有合法出口（fail-closed 保持：新模块/解析失败仍按失败）。
"""
import json
from pathlib import Path

import pytest

from swarm.brain.plan_batch import renormalize_cross_module_creates
from swarm.brain.planning_nodes import _stage2_zero_change_plausible


def _mk_baseline(tmp_path, mods=("ruoyi-common", "ruoyi-system")):
    for m in mods:
        d = tmp_path / m
        d.mkdir(parents=True, exist_ok=True)
        (d / "pom.xml").write_text("<project/>", encoding="utf-8")
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    return str(tmp_path)


def _fp(module, path, action="create"):
    return {"path": path, "action": action, "module": module}


# ── R67B-T1 ──────────────────────────────────────────────────────────────

def test_t1_create_into_existing_baseline_module_relabeled(tmp_path):
    """round67b 真实形态：create 落既有基线模块目录 → 标签归位到物理归属。"""
    proj = _mk_baseline(tmp_path)
    fp = [
        _fp("ruoyi-system", "ruoyi-system/src/main/java/com/ruoyi/system/service/ISysGoogleAuthService.java"),
        _fp("ruoyi-system", "ruoyi-system/src/main/java/com/ruoyi/system/service/impl/SysGoogleAuthServiceImpl.java"),
        _fp("ruoyi-system", "ruoyi-common/src/main/java/com/ruoyi/common/utils/AesUtils.java"),
        _fp("ruoyi-system", "ruoyi-common/src/main/java/com/ruoyi/common/utils/TotpUtils.java"),
    ]
    moved = renormalize_cross_module_creates(fp, proj)
    assert moved, "跨模块 create 应被重规范化"
    assert {e["module"] for e in fp if "Utils" in e["path"]} == {"ruoyi-common"}
    assert all(e["module"] == "ruoyi-system" for e in fp if e["path"].startswith("ruoyi-system/"))


def test_t1_new_dir_stays_for_g1(tmp_path):
    """fail-closed：目标根不是既有基线构建单元（新目录）→ 不动，真歧义留 G1 打回。"""
    proj = _mk_baseline(tmp_path, mods=("mod-a",))
    fp = [
        _fp("mod-a", "mod-a/src/main/java/com/x/A.java"),
        _fp("mod-a", "brand-new/src/main/java/com/x/B.java"),
    ]
    moved = renormalize_cross_module_creates(fp, proj)
    assert moved == {}
    assert fp[1]["module"] == "mod-a"


def test_t1_own_root_indeterminate_stays(tmp_path):
    """fail-closed：模块自有根不可判（无同名根且无多数根）→ 不动。"""
    proj = _mk_baseline(tmp_path)
    fp = [
        _fp("feature-x", "ruoyi-common/src/main/java/com/x/A.java"),
        _fp("feature-x", "ruoyi-system/src/main/java/com/x/B.java"),
    ]
    moved = renormalize_cross_module_creates(fp, proj)
    assert moved == {}
    assert {e["module"] for e in fp} == {"feature-x"}


def test_t1_majority_own_root_fallback(tmp_path):
    """标签≠目录名（R58-1）时按多数根定自有根，少数侧 create 归位。"""
    proj = _mk_baseline(tmp_path, mods=("ruoyi-alarm", "ruoyi-common"))
    fp = [
        _fp("alarm-biz", "ruoyi-alarm/src/main/java/com/r/a/S1.java"),
        _fp("alarm-biz", "ruoyi-alarm/src/main/java/com/r/a/S2.java"),
        _fp("alarm-biz", "ruoyi-alarm/src/main/java/com/r/a/S3.java"),
        _fp("alarm-biz", "ruoyi-common/src/main/java/com/r/c/U.java"),
    ]
    moved = renormalize_cross_module_creates(fp, proj)
    assert moved
    assert fp[3]["module"] == "ruoyi-common"
    assert all(e["module"] == "alarm-biz" for e in fp[:3])


def test_t1_modify_not_touched(tmp_path):
    """仅动 create；modify 既有文件由 G1 #40 豁免处理，不越权改标签。"""
    proj = _mk_baseline(tmp_path, mods=("ruoyi-admin", "ruoyi-system"))
    fp = [
        _fp("ruoyi-system", "ruoyi-system/src/main/java/com/r/s/A.java"),
        _fp("ruoyi-system", "ruoyi-system/src/main/java/com/r/s/B.java"),
        _fp("ruoyi-system", "ruoyi-admin/src/main/java/com/r/w/C.java", action="modify"),
    ]
    moved = renormalize_cross_module_creates(fp, proj)
    assert moved == {}
    assert fp[2]["module"] == "ruoyi-system"


def test_t1_target_label_reuses_existing_module(tmp_path):
    """目标根已有单根模块 → 复用其标签而非造新名。"""
    proj = _mk_baseline(tmp_path)
    fp = [
        _fp("common-core", "ruoyi-common/src/main/java/com/r/c/Base.java"),
        _fp("ruoyi-system", "ruoyi-system/src/main/java/com/r/s/A.java"),
        _fp("ruoyi-system", "ruoyi-system/src/main/java/com/r/s/B.java"),
        _fp("ruoyi-system", "ruoyi-common/src/main/java/com/r/c/U.java"),
    ]
    moved = renormalize_cross_module_creates(fp, proj)
    assert moved
    assert fp[3]["module"] == "common-core"


def test_t1_target_label_collision_stays(tmp_path):
    """fail-closed：目标标签已被指向其它物理根的模块占用 → 不动。"""
    proj = _mk_baseline(tmp_path, mods=("ruoyi-common", "ruoyi-system", "other2"))
    fp = [
        _fp("ruoyi-common", "other2/src/main/java/com/o/X.java"),   # 标签被占且指向别根
        _fp("ruoyi-system", "ruoyi-system/src/main/java/com/r/s/A.java"),
        _fp("ruoyi-system", "ruoyi-system/src/main/java/com/r/s/B.java"),
        _fp("ruoyi-system", "ruoyi-common/src/main/java/com/r/c/U.java"),
    ]
    moved = renormalize_cross_module_creates(fp, proj)
    assert moved == {}
    assert fp[3]["module"] == "ruoyi-system"


def test_t1_idempotent(tmp_path):
    proj = _mk_baseline(tmp_path)
    fp = [
        _fp("ruoyi-system", "ruoyi-system/src/main/java/com/r/s/A.java"),
        _fp("ruoyi-system", "ruoyi-system/src/main/java/com/r/s/B.java"),
        _fp("ruoyi-system", "ruoyi-common/src/main/java/com/r/c/U.java"),
    ]
    assert renormalize_cross_module_creates(fp, proj)
    assert renormalize_cross_module_creates(fp, proj) == {}


def test_t1_no_project_path_noop():
    fp = [_fp("m", "m/src/main/java/A.java")]
    assert renormalize_cross_module_creates(fp, None) == {}


_CASSETTE = Path(__file__).resolve().parent.parent / "cassettes" / "251e05f3-7460-4578-850c-63f445766eb1.json"


@pytest.mark.skipif(not _CASSETTE.exists(), reason="round67b cassette 不在本机")
def test_t1_round67b_real_plan_heals(tmp_path):
    """真 plan 回放：round67b 死因 file_plan 经重规范化后 ruoyi-system 单根、工具类归 common。"""
    d = json.loads(_CASSETTE.read_text())
    fp = (d.get("state") or d).get("file_plan") or []
    proj = _mk_baseline(
        tmp_path, mods=("ruoyi-common", "ruoyi-system", "ruoyi-admin",
                        "ruoyi-framework", "ruoyi-quartz", "ruoyi-generator"))
    moved = renormalize_cross_module_creates(fp, proj)
    assert any("AesUtils" in p for ps in moved.values() for p in ps)
    assert any("TotpUtils" in p for ps in moved.values() for p in ps)
    # 治后：ruoyi-system 名下不再有 ruoyi-common/ 下的 create
    assert not [e for e in fp
                if isinstance(e, dict) and e.get("module") == "ruoyi-system"
                and str(e.get("path", "")).startswith("ruoyi-common/")
                and str(e.get("action", "")).lower() == "create"]


# ── R67B-T2 ──────────────────────────────────────────────────────────────

def test_t2_zero_change_plausible_existing_module(tmp_path):
    d = tmp_path / "ruoyi-generator"
    d.mkdir()
    (d / "pom.xml").write_text("<project/>", encoding="utf-8")
    assert _stage2_zero_change_plausible("ruoyi-generator", str(tmp_path)) is True


def test_t2_zero_change_failclosed():
    assert _stage2_zero_change_plausible("ruoyi-generator", None) is False
    assert _stage2_zero_change_plausible("", "/tmp") is False


def test_t2_zero_change_missing_or_empty_module(tmp_path):
    assert _stage2_zero_change_plausible("no-such-mod", str(tmp_path)) is False
    (tmp_path / "empty-mod").mkdir()
    assert _stage2_zero_change_plausible("empty-mod", str(tmp_path)) is False


def test_t2_zero_change_dir_without_manifest_failclosed(tmp_path):
    """hunter④ 整改后语义：判据=钉扎 base 里的既有【构建单元】（同 G1/#40 权威口径），
    有文件但无构建清单的目录不算——申报零改造须物理构建单元证据，fail-closed。"""
    d = tmp_path / "flat-mod"
    d.mkdir()
    (d / "readme.txt").write_text("x", encoding="utf-8")
    assert _stage2_zero_change_plausible("flat-mod", str(tmp_path)) is False
