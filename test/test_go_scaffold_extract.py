"""go_scaffold 叶簇拆分（纪律#9 god-file 不再喂肥）等值锁。

判据（记忆：纯结构改动必须加逐元素相等锁——「行为不变」的说法会带着手滑
一起穿过复核）：本批是【纯移动】，锁三件套：
  ① re-export 逐元素对象同一性（contract_utils 的名字与新模块本体是同一对象）；
  ② 注册表接线（_P2_SCAFFOLD_DRIVERS["go"] 直指新模块本体——拆断接线=driver
     表指向旧壳）；
  ③ 共享助手归属（_go_relpath 因 npm 侧共享留 contract_utils，不进新模块——
     防"顺手搬走"造成 npm 侧断链）。
行为锁由既有 test_n2b_n3 / test_b0_non_maven_cassette_replay /
test_template_exam_multistack_ph2 / test_xm9_h1_template_multistack /
test_r39_build_scaffold_inject 承担（它们经 contract_utils 取函数=同时锁
re-export 可寻址）。
"""
from __future__ import annotations

_CLUSTER = ("_go_root_module_path", "_go_root_directive", "_go_work_use_dirs",
            "_go_module_path_prefix", "_go_module_path", "_render_go_mod",
            "_go_dep_block", "_inject_go_scaffolds")


def test_reexport_object_identity():
    """逐元素相等锁：contract_utils 的 8 个名字与 go_scaffold 本体同一对象。"""
    import swarm.brain.contract_utils as cu
    import swarm.brain.go_scaffold as gs
    for name in _CLUSTER:
        assert getattr(cu, name) is getattr(gs, name), \
            f"{name}：re-export 不是同一对象（拆成了壳/副本）"


def test_p2_driver_registry_wiring():
    """注册表接线锁：go driver 直指新模块本体（机制存在≠接线覆盖）。"""
    import swarm.brain.contract_utils as cu
    import swarm.brain.go_scaffold as gs
    assert cu._P2_SCAFFOLD_DRIVERS["go"] is gs._inject_go_scaffolds
    assert "go" in cu._MODULE_SCAFFOLD_DRIVER_STACKS


def test_go_relpath_stays_shared_in_contract_utils():
    """_go_relpath 是 npm 侧（contract_utils 内非 go 调用点）共享助手——
    必须留在 contract_utils 且不被新模块吞并。"""
    import swarm.brain.contract_utils as cu
    import swarm.brain.go_scaffold as gs
    assert callable(cu._go_relpath)
    assert "_go_relpath" not in vars(gs), "共享助手被顺手搬走=npm 侧断链风险"
