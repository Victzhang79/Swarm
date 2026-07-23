"""round67g 治本红灯先行：file_plan 层异 FQN 同 simple-name 跨包 create 确定性消解（T1）。

死因（task=b3659ca9 FAILED@PLAN）：VALIDATE 期 G1 ③b REJECT 异FQN同名跨包 create（AlarmLevelEnum/
AlarmTypeEnum），但 file_plan【恒定】、重试从它重拆只重新分组、batch LLM 无权删条目 → LLM renumber
原样重犯 → 层②熔断 FAILED@PLAN。治本=在【分批前】(dedupe_file_plan 后、group_into_module_batches
前) 唯一能改 file_plan 的确定性关口，用【契约 defined_in】唯一权威消解。

★create-vs-base shadow（SysMenu/SysUser）本轮【不做确定性归位】★：base 同名唯一即归位=round67c 已被
ecc 复核判 HIGH 删除的裸 basename 挑边（合法通用名新类撞无关 base 静默腐化），交 G1 ③f REJECT，独立
前沿另治（对抗复核 CRITICAL 整改）。

fail-closed 铁律（round67c 血泪）：无契约权威/契约歧义 → 绝不裸挑边，留 REJECT。栈中立：仅 JVM 类路径
命名空间（Go/Py/TS/资源天然豁免）。
"""
from swarm.brain.contract_utils import deconflict_file_plan_same_name_creates

_IF_ENUM = "ruoyi-alarm-interface/src/main/java/com/ruoyi/alarm/client/enums/AlarmLevelEnum.java"
_COMMON_ENUM = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/common/enums/AlarmLevelEnum.java"


def _fp(path, action="create", module="", depends_on=None):
    return {"path": path, "action": action, "module": module,
            "depends_on": depends_on or []}


def _contract(defined_in, section="interfaces"):
    return {section: [{"name": "AlarmLevelEnum", "defined_in": defined_in}]}


# ── T1：异 FQN 同 simple-name 跨包 create（契约权威消解）──────────────────────

def test_t1_contract_authority_dedupes_non_owner():
    """契约 defined_in 有唯一权威 → 删非 owner 副本条目（保 owner）。"""
    fp = [_fp(_IF_ENUM), _fp(_COMMON_ENUM)]
    counts = deconflict_file_plan_same_name_creates(
        fp, shared_contract=_contract(_IF_ENUM), project_path=None, base_ref=None)
    paths = {e["path"] for e in fp}
    assert _IF_ENUM in paths, "owner 被误删"
    assert _COMMON_ENUM not in paths, "非 owner 副本未剥离"
    assert counts.get("samename_creates_deduped", 0) == 1


def test_t1_dtos_section_authority_dedupes():
    """★round67g 真实场景★：枚举 defined_in 在契约 `dtos` section（非 interfaces）→ 也须读到权威。"""
    fp = [_fp(_IF_ENUM), _fp(_COMMON_ENUM)]
    contract = {"dtos": [{"name": "AlarmLevelEnum", "fields": ["P1", "P2"],
                          "defined_in": _IF_ENUM}]}
    counts = deconflict_file_plan_same_name_creates(
        fp, shared_contract=contract, project_path=None, base_ref=None)
    paths = {e["path"] for e in fp}
    assert _IF_ENUM in paths and _COMMON_ENUM not in paths, "dtos section 权威未读到"
    assert counts.get("samename_creates_deduped", 0) == 1


def test_t1_no_contract_authority_fail_closed():
    """无契约权威（枚举不在任何 section）→ fail-closed 不动，留 ③b REJECT。"""
    fp = [_fp(_IF_ENUM), _fp(_COMMON_ENUM)]
    counts = deconflict_file_plan_same_name_creates(
        fp, shared_contract={}, project_path=None, base_ref=None)
    paths = {e["path"] for e in fp}
    assert _IF_ENUM in paths and _COMMON_ENUM in paths, "无权威却挑边（腐化风险）"
    assert counts.get("samename_creates_deduped", 0) == 0


def test_t1_contract_ambiguous_fail_closed():
    """契约自身对同 simple-name 给两个不同 owner → 歧义 fail-closed。"""
    fp = [_fp(_IF_ENUM), _fp(_COMMON_ENUM)]
    contract = {"interfaces": [
        {"name": "AlarmLevelEnum", "defined_in": _IF_ENUM},
        {"name": "AlarmLevelEnum", "defined_in": _COMMON_ENUM}]}
    counts = deconflict_file_plan_same_name_creates(
        fp, shared_contract=contract, project_path=None, base_ref=None)
    assert len(fp) == 2, "契约歧义却消解"
    assert counts.get("samename_creates_deduped", 0) == 0


def test_t1_go_same_basename_not_touched():
    """Go 同 basename 异模块【合法】→ 栈中立豁免（classpath_fqn_key 返 None）。"""
    fp = [_fp("svc-a/internal/handler/handler.go"),
          _fp("svc-b/internal/handler/handler.go")]
    counts = deconflict_file_plan_same_name_creates(
        fp, shared_contract={}, project_path=None, base_ref=None)
    assert len(fp) == 2 and counts.get("samename_creates_deduped", 0) == 0


def test_t1_test_layout_exempt():
    """每模块一份 ApplicationTests 是生态惯例 → test 布局豁免。"""
    fp = [_fp("mod-a/src/test/java/com/x/AppTest.java"),
          _fp("mod-b/src/test/java/com/y/AppTest.java")]
    counts = deconflict_file_plan_same_name_creates(
        fp, shared_contract={}, project_path=None, base_ref=None)
    assert len(fp) == 2 and counts.get("samename_creates_deduped", 0) == 0


def test_t1_single_fqn_not_touched():
    """同 FQN 跨模块（非异 FQN）→ 不由本 pass 处理（交 ③ deconflict_cross_module_creates）。"""
    p = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/Foo.java"
    fp = [_fp(p), _fp(p)]   # 同路径同 FQN
    counts = deconflict_file_plan_same_name_creates(
        fp, shared_contract=_contract(p), project_path=None, base_ref=None)
    assert counts.get("samename_creates_deduped", 0) == 0


def test_t1_depends_on_resync_to_owner():
    """★复核 Hunter#2 整改★：剥非 owner 副本后，其它条目 depends_on 引被剥路径 → 改指 owner，
    不留陈旧边（否则 group_into_module_batches file 级排序回退静默丢弃）。"""
    consumer = _fp("ruoyi-alarm/src/main/java/com/ruoyi/alarm/Consumer.java",
                   depends_on=[_COMMON_ENUM])   # 消费者依赖【被剥的副本路径】
    fp = [_fp(_IF_ENUM), _fp(_COMMON_ENUM), consumer]
    deconflict_file_plan_same_name_creates(
        fp, shared_contract=_contract(_IF_ENUM), project_path=None, base_ref=None)
    assert _COMMON_ENUM not in consumer["depends_on"], "陈旧边未清"
    assert _IF_ENUM in consumer["depends_on"], "未改指 owner 落点"


def test_t1_owner_not_created_fail_closed():
    """契约权威 owner 在 file_plan 里无人 create → fail-closed 不挑边（绝不删唯一存在的副本）。"""
    other = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/other/enums/AlarmLevelEnum.java"
    fp = [_fp(_COMMON_ENUM), _fp(other)]   # 两个都不是契约 owner(_IF_ENUM)
    counts = deconflict_file_plan_same_name_creates(
        fp, shared_contract=_contract(_IF_ENUM), project_path=None, base_ref=None)
    assert len(fp) == 2, "owner 无人建却挑边"
    assert counts.get("samename_creates_deduped", 0) == 0


# ── create-vs-base shadow：本轮不归位（对抗复核 CRITICAL 整改）──────────────

def test_create_vs_base_shadow_left_for_g1_3f(monkeypatch):
    """create 撞 base 既有唯一同名异路径类 → 本轮【不归位】（base 同名挑边=round67c 已删启发式），
    交 G1 ③f REJECT。绝不静默改写 path/action 去 modify 无关 base 文件。"""
    import swarm.brain.contract_utils as cu
    base_sysmenu = "ruoyi-common/src/main/java/com/ruoyi/common/core/domain/entity/SysMenu.java"
    monkeypatch.setattr(cu, "_base_tree_listing", lambda pp, br: [base_sysmenu])
    shadow = "ruoyi-system/src/main/java/com/ruoyi/system/domain/SysMenu.java"
    fp = [_fp(shadow, action="create", module="ruoyi-system")]
    counts = deconflict_file_plan_same_name_creates(
        fp, shared_contract={}, project_path="/x", base_ref="HEAD")
    assert fp[0]["path"] == shadow and fp[0]["action"] == "create", "create-vs-base 被静默归位（腐化风险）"
    assert "base_shadow_relocated" not in counts, "T2 归位机制未撤净"
