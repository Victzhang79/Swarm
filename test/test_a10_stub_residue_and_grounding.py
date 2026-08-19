"""32 号文 A10 治本锁：阶梯三 stub 路残留清理（M1）+ 桩坐标接地闸自证（M2）。

两条都走【真入口】断行为，不断实现细节（纪律 6）：
- M1 锁：驱动 `_give_up_preserve_build`，断"非桩产出的足迹残留真被清出本地树，
  且桩产出本体仍在"。突变（删掉清理调用/去掉 protected）必红。
- M2 锁：驱动 `_strip_ungrounded_manifest_coords`，夹具形状必须编码【生产序】=
  桩已写盘。这是本条的命门：桩未写盘的夹具会让闸看起来一直是对的（原缺陷正是
  被这种夹具形状掩盖的）。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from swarm.types import Confidence, WorkerOutput

BASE_POM = """<project>
  <modelVersion>4.0.0</modelVersion>
  <dependencies>
    <dependency>
      <groupId>org.apache.maven</groupId>
      <artifactId>maven-core</artifactId>
      <version>4.8.3</version>
    </dependency>
  </dependencies>
</project>
"""


def _git(d: Path, *args: str):
    return subprocess.run(["git", "-C", str(d), *args],
                          capture_output=True, text=True)


def _mk_repo(tmp_path: Path) -> tuple[Path, str]:
    """真 git 仓 + 一个 base commit（tracked pom）。返回 (路径, base_sha)。"""
    d = tmp_path / "proj"
    d.mkdir()
    _git(d, "init", "-q", ".")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    (d / "pom.xml").write_text(BASE_POM, encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "base")
    return d, _git(d, "rev-parse", "HEAD").stdout.strip()


# ──────────────────────────── M2：接地闸不得被桩自证 ────────────────────────────

STUB_MOD_POM = BASE_POM.replace("4.8.3", "3.8.7")   # 3.8.7 在 base 全仓不存在
STUB_DIFF_POM = (
    "diff --git a/mod/pom.xml b/mod/pom.xml\n"
    "--- /dev/null\n+++ b/mod/pom.xml\n@@ -0,0 +1,1 @@\n"
    "+      <version>3.8.7</version>\n"
)


def test_m2_grounding_gate_not_selfvalidated_by_stub_own_write(tmp_path):
    """★夹具形状是命门★ 桩【已写盘】（生产序 write→check）时仍须剥离臆造坐标。

    原缺陷：证据集 os.walk 扫当前树 ⇒ 把桩刚写的 mod/pom.xml 里的 3.8.7 当"基线已存在"
    放行。桩未写盘的夹具下闸恒正确，照不出这个病——故本用例必须把那份 pom 落到盘上。
    """
    from swarm.brain.nodes.planning_core import _strip_ungrounded_manifest_coords

    d, _ = _mk_repo(tmp_path)
    (d / "mod").mkdir()
    (d / "mod" / "pom.xml").write_text(STUB_MOD_POM, encoding="utf-8")  # ★生产序★

    out = _strip_ungrounded_manifest_coords(STUB_DIFF_POM, str(d), "st-1")
    assert "3.8.7" not in (out or ""), (
        "桩自己写盘的臆造坐标必须仍被剥离——证据集不得包含【正在被审的这份补丁】涉及的文件"
    )


def test_m2_sibling_passed_l1_version_still_grounded(tmp_path):
    """反向锁（防治法过头）：**已过 L1** 兄弟引入的新版本仍算接地，绝不误剥。

    这条钉住"排除未验证状态"而非"只读 git base"的选型——后者会把兄弟【已过确定性闸】
    的合法新增坐标误判臆造，闸过宽使用者就会绕开它。突变成 base-only 时本条必红。
    """
    from swarm.brain.nodes.planning_core import _strip_ungrounded_manifest_coords

    d, _ = _mk_repo(tmp_path)
    # 兄弟子任务已在另一模块引入 7.7.7，且【L1 已通过】⇒ 经过确定性闸 ⇒ 真证据
    (d / "sib").mkdir()
    (d / "sib" / "pom.xml").write_text(BASE_POM.replace("4.8.3", "7.7.7"),
                                       encoding="utf-8")
    diff = STUB_DIFF_POM.replace("3.8.7", "7.7.7")

    out = _strip_ungrounded_manifest_coords(
        diff, str(d), "st-1", verified_files={"sib/pom.xml"})
    assert "7.7.7" in (out or ""), (
        "已过 L1 兄弟引入的版本属真接地证据，不得剥离（否则闸过宽、使用者会绕开）"
    )


def test_m2_dirty_unverified_sibling_manifest_is_not_evidence(tmp_path):
    """★复核 MED-1 整改的核心区分锁★ 未过 L1 的脏清单**不得**充当接地证据。

    失败形态（复核实跑坐实初版治法不充分）：X 的 worker 越权/腐化写脏了 `mod-a/pom.xml`
    并在里面留下臆造版本；桩为 `mod-b/pom.xml` 产出**同一个**版本。`mod-a/pom.xml` 不在
    桩 diff 里 ⇒ 初版"只排除补丁自身路径"的治法放行它 ⇒ 臆造坐标经旁路自证。
    新不变量：坐标只能靠【已验证】状态接地（与 base 一致，或已过 L1 兄弟的产物）。

    与上一条唯一差别 = 该脏清单是否在 `verified_files` 里——故两条合起来锁住"验证与否"
    这一维本身，而不只是"自己与别人"。
    """
    from swarm.brain.nodes.planning_core import _strip_ungrounded_manifest_coords

    d, _ = _mk_repo(tmp_path)
    (d / "mod-a").mkdir()
    # 未过 L1（不在 verified_files）的脏清单，里面有臆造 9.9.9
    (d / "mod-a" / "pom.xml").write_text(BASE_POM.replace("4.8.3", "9.9.9"),
                                        encoding="utf-8")
    # 桩为另一个模块产出同一个版本
    diff = ("diff --git a/mod-b/pom.xml b/mod-b/pom.xml\n"
            "--- /dev/null\n+++ b/mod-b/pom.xml\n@@ -0,0 +1,1 @@\n"
            "+      <version>9.9.9</version>\n")

    out = _strip_ungrounded_manifest_coords(diff, str(d), "st-1", verified_files=set())
    assert "9.9.9" not in (out or ""), (
        "未过 L1 的脏清单不得当接地证据——否则臆造坐标可经【别的未验证脏文件】旁路自证"
    )


def test_m2_baseline_version_untouched(tmp_path):
    """基线里真存在的版本不受影响（零回归）。"""
    from swarm.brain.nodes.planning_core import _strip_ungrounded_manifest_coords

    d, _ = _mk_repo(tmp_path)
    (d / "mod").mkdir()
    (d / "mod" / "pom.xml").write_text(BASE_POM, encoding="utf-8")
    diff = STUB_DIFF_POM.replace("3.8.7", "4.8.3")

    out = _strip_ungrounded_manifest_coords(diff, str(d), "st-1")
    assert "4.8.3" in (out or "")


# ──────────────────── M1：stub 路必须清非桩产出的足迹残留 ────────────────────

class _Resp:
    def __init__(self, content: str):
        self.content = content


class _StubLLM:
    """只产出代码桩，【不】产出 pom——复刻真实形态：pom 不在桩可写面内。"""

    async def ainvoke(self, _msgs):
        return _Resp(json.dumps({"files": {
            "mod/src/main/java/A.java": "package mod;\npublic class A {}\n",
        }}))


class _Scope:
    def __init__(self, create=(), writable=(), upstream=()):
        self.create_files = list(create)
        self.writable = list(writable)
        self.upstream_artifacts = list(upstream)
        self.readable = []


class _ST:
    def __init__(self, sid, scope, deps=()):
        self.id = sid
        self.scope = scope
        self.depends_on = list(deps)
        self.description = "建 mod"
        self.contract = None
        self.retry_guidance = ""
        self.difficulty = None


class _Plan:
    def __init__(self, subs):
        self.subtasks = list(subs)


@pytest.mark.asyncio
async def test_m1_stub_path_cleans_non_code_residue_keeps_stub(tmp_path):
    """核心锁：pom 残留（tracked，被改脏）清回 base；桩产出的 .java 留在树上。

    形状要点：
      · pom.xml 是 **tracked** 且被改脏 —— 正是 `_grant_module_pom_writable` 授权后
        brain/worker 改脏模块清单的真实形态（清理＝checkout 回 base，不是删文件）。
      · pom 进 scope.writable 但【不在】下游 upstream_artifacts ⇒ 不进 `_required`
        ⇒ 桩不写它 ⇒ 落在"既不打桩也不进 diff"的缺口里。
      · 下游 st-2 依赖 st-1 ⇒ depended=True ⇒ 走 stub 路（非 revert 路）。
    """
    from swarm.brain.nodes.planning_core import _give_up_preserve_build

    d, base_sha = _mk_repo(tmp_path)
    # 模块 pom 先入 base（tracked），再被改脏（模拟授写权后被改）
    (d / "mod").mkdir()
    (d / "mod" / "pom.xml").write_text(BASE_POM, encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "mod pom")
    base_sha = _git(d, "rev-parse", "HEAD").stdout.strip()
    (d / "mod" / "pom.xml").write_text(
        BASE_POM.replace("<groupId>", "<group>"), encoding="utf-8")  # 腐化形态

    up = _ST("st-1", _Scope(create=["mod/src/main/java/A.java"],
                            writable=["mod/pom.xml"]))
    down = _ST("st-2", _Scope(create=["other/B.java"]), deps=["st-1"])
    state = {"plan": _Plan([up, down]), "base_commit": base_sha,
             "project_id": "p-a10", "subtask_results": {},
             "give_up_isolated_ids": [], "abandoned_subtask_ids": []}

    # project_path 经 _proj_path_from_state(state) 查 store——测试不起 DB，直接钉路径。
    # 缺这一步 ⇒ path=None ⇒ 桩恒返 None ⇒ 落 revert 路 ⇒ 本用例测不到 M1（夹具失效）。
    with patch("swarm.brain.nodes._get_brain_llm", return_value=_StubLLM()), \
            patch("swarm.brain.nodes.planning_core._proj_path_from_state",
                  return_value=str(d)):
        out = await _give_up_preserve_build(state, ["st-1"])

    assert out is not None
    wo = out["subtask_results"]["st-1"]
    det = wo.l1_details or {}
    assert det.get("give_up_mode") == "stub", (
        f"前提未成立：本用例要求走 stub 路，实际 {det.get('give_up_mode')}"
        "（走了 revert 就测不到 M1，属夹具失效必须修夹具）"
    )
    # ① 残留真被清出本地树（腐化的 <group> 回到 base 的 <groupId>）
    pom_now = (d / "mod" / "pom.xml").read_text(encoding="utf-8")
    assert "<group>" not in pom_now, (
        "桩路必须清掉非桩产出的足迹残留——腐化的模块 pom 仍在树上会毒 L2/reactor，"
        "且五处脏树判据全按 merged_diff 取集看不见它"
    )
    assert "<groupId>" in pom_now
    # ② 桩产出本体绝不被自己的清理删掉（protected 口径同源）
    assert (d / "mod" / "src" / "main" / "java" / "A.java").is_file(), (
        "桩写的代码文件被清理误删——protected 必须护住桩产出"
    )
    # ③ 机读账在场且列出被清文件
    assert "mod/pom.xml" in (det.get("stub_residue_cleaned") or []), (
        f"清理必须留机读账，实得 l1_details={det}"
    )


@pytest.mark.asyncio
async def test_m1_downstream_declared_manifest_is_stubbed_not_cleaned(tmp_path):
    """边界锁：下游显式声明的清单（进 `_required`）由桩产出，绝不被当残留清掉。

    这是 R65C-T3 的既有承诺（种子闸硬要求），治法不得破它。
    """
    from swarm.brain.nodes.planning_core import _give_up_preserve_build

    class _LLMWithPom:
        async def ainvoke(self, _msgs):
            return _Resp(json.dumps({"files": {
                "mod/src/main/java/A.java": "package mod;\npublic class A {}\n",
                # 用 base 已存在的版本，免被接地闸剥离（本用例不测接地）
                "mod/pom.xml": BASE_POM,
            }}))

    d, base_sha = _mk_repo(tmp_path)
    up = _ST("st-1", _Scope(create=["mod/src/main/java/A.java", "mod/pom.xml"]))
    down = _ST("st-2", _Scope(create=["other/B.java"],
                              upstream=["mod/pom.xml"]), deps=["st-1"])
    state = {"plan": _Plan([up, down]), "base_commit": base_sha,
             "project_id": "p-a10", "subtask_results": {},
             "give_up_isolated_ids": [], "abandoned_subtask_ids": []}

    with patch("swarm.brain.nodes._get_brain_llm", return_value=_LLMWithPom()), \
            patch("swarm.brain.nodes.planning_core._proj_path_from_state",
                  return_value=str(d)):
        out = await _give_up_preserve_build(state, ["st-1"])

    assert out is not None
    wo = out["subtask_results"]["st-1"]
    assert (wo.l1_details or {}).get("give_up_mode") == "stub"
    assert (d / "mod" / "pom.xml").is_file(), (
        "下游声明的清单是桩的硬覆盖目标，绝不能被残留清理删掉（否则下游种子闸永堵）"
    )
    assert "mod/pom.xml" in (wo.diff or ""), "该清单必须在桩 diff 里（下游 provenance）"


@pytest.mark.asyncio
async def test_m1_binary_marked_stub_output_still_protected(tmp_path):
    """★复核"不确定项1"整改锁★ `.gitattributes` 标 `-diff` 的桩产出仍须被护住。

    坐实过的失败形态：git 对 `-diff` 路径只输出 `Binary files ... differ`、无 `+++ b/` 行
    ⇒ 从 diff 反推写盘集会漏掉它 ⇒ 它不被 protected ⇒ **被残留清理删掉自己的产出**。
    且**部分标记比全部标记更危险**（全漏时清理整块跳过；部分漏时那一个被删）。
    治法＝`_generate_compile_stub` 直接返回真实 `written`，调用方绝不从 diff 反推。
    """
    from swarm.brain.nodes.planning_core import _give_up_preserve_build

    class _LLMTwoFiles:
        async def ainvoke(self, _msgs):
            return _Resp(json.dumps({"files": {
                "mod/src/main/java/A.java": "package mod;\npublic class A {}\n",
                "mod/pom.xml": BASE_POM,
            }}))

    d, base_sha = _mk_repo(tmp_path)
    # ★把桩要写的清单标成 -diff（git 只出 "Binary files differ"，无 +++ 行）★
    (d / ".gitattributes").write_text("mod/pom.xml -diff\n", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "gitattributes")
    base_sha = _git(d, "rev-parse", "HEAD").stdout.strip()

    up = _ST("st-1", _Scope(create=["mod/src/main/java/A.java", "mod/pom.xml"]))
    down = _ST("st-2", _Scope(create=["other/B.java"],
                              upstream=["mod/pom.xml"]), deps=["st-1"])
    state = {"plan": _Plan([up, down]), "base_commit": base_sha,
             "project_id": "p-a10", "subtask_results": {},
             "give_up_isolated_ids": [], "abandoned_subtask_ids": []}

    with patch("swarm.brain.nodes._get_brain_llm", return_value=_LLMTwoFiles()), \
            patch("swarm.brain.nodes.planning_core._proj_path_from_state",
                  return_value=str(d)):
        out = await _give_up_preserve_build(state, ["st-1"])

    assert out is not None
    wo = out["subtask_results"]["st-1"]
    assert (wo.l1_details or {}).get("give_up_mode") == "stub", (
        f"前提未成立：要求走 stub 路，实际 {(wo.l1_details or {}).get('give_up_mode')}"
    )
    assert (d / "mod" / "pom.xml").is_file(), (
        "被 .gitattributes 标 -diff 的桩产出仍须被 protected 护住——从 diff 反推写盘集"
        "会漏掉它并把它删掉（写盘集必须来自 _generate_compile_stub 的真实 written）"
    )


@pytest.mark.asyncio
async def test_m1_residue_revert_failure_reaches_degraded_reasons(tmp_path):
    """★复核 LOW-4 整改锁★ 清理失败必须走到 `degraded_reasons` 且落**阻断**档。

    此前删掉那三行 append 代码，5 条测试全绿——这一跳没有锁。
    本条驱动真入口 + 让清理器返回 revert_failed，断三件事：机读键在 l1_details、
    前缀进 out["degraded_reasons"]、且被 `blocking_degraded_reasons` 认作阻断（拦 L6）。
    """
    from swarm.brain.nodes import planning_core as pc
    from swarm.memory.pattern_extractor import blocking_degraded_reasons

    d, base_sha = _mk_repo(tmp_path)
    up = _ST("st-1", _Scope(create=["mod/src/main/java/A.java"],
                            writable=["mod/pom.xml"]))
    down = _ST("st-2", _Scope(create=["other/B.java"]), deps=["st-1"])
    state = {"plan": _Plan([up, down]), "base_commit": base_sha,
             "project_id": "p-a10", "subtask_results": {},
             "give_up_isolated_ids": [], "abandoned_subtask_ids": []}

    _real = pc._local_tree_revert_subtask

    def _fake_revert(project_path, st, protected_files=None, base_ref=None,
                     extra_files=None):
        # 只对 st-1 的残留清理注入失败，其它调用（桩内部回退等）走真实实现
        if getattr(st, "id", None) == "st-1":
            return {"reverted": [], "removed": [], "revert_failed": ["mod/pom.xml"],
                    "skipped_protected": []}
        return _real(project_path, st, protected_files=protected_files,
                     base_ref=base_ref, extra_files=extra_files)

    with patch("swarm.brain.nodes._get_brain_llm", return_value=_StubLLM()), \
            patch("swarm.brain.nodes.planning_core._proj_path_from_state",
                  return_value=str(d)), \
            patch.object(pc, "_local_tree_revert_subtask", _fake_revert):
        out = await _give_up_preserve_build_ref()(state, ["st-1"])

    assert out is not None
    det = out["subtask_results"]["st-1"].l1_details or {}
    assert det.get("stub_residue_revert_failed") == ["mod/pom.xml"], (
        f"清理失败必须留机读键，实得 {det}"
    )
    reasons = out.get("degraded_reasons") or []
    hit = [r for r in reasons if r.startswith("stub_residue_revert_failed:")]
    assert hit, f"清理失败必须进 degraded_reasons（reducer 通道），实得 {reasons}"
    assert blocking_degraded_reasons(hit) == hit, (
        "该前缀必须落【阻断】档（不在信息性白名单）——否则'残留毒树'仍会被学成成功模式"
    )


def _give_up_preserve_build_ref():
    """取生产函数引用（延迟到调用时，确保拿到 patch 后的模块态）。"""
    from swarm.brain.nodes.planning_core import _give_up_preserve_build
    return _give_up_preserve_build


@pytest.mark.asyncio
async def test_m1_out_of_scope_write_is_also_cleaned(tmp_path):
    """★复核 MED-2 整改锁★ X 的【越权写入】（scope 外）也必须被清。

    round67l st-14 实锤形态：X 的 scope 只声明 `mod/pom.xml`，worker 却越权写了
    **根 pom**（钉版本 / 注册不存在模块）⇒ `_subtask_footprint` 纯 scope 驱动够不着 ⇒
    桩路零清理者（runner 终态清扫又因 `l1_passed=True` 豁免桩）。
    治法＝清扫面并入 X 自身失败 diff 解析出的路径（`extra_files=`，runner F1 同款）。
    突变掉 `extra_files=` 实参时本条必红。
    """
    from swarm.brain.nodes.planning_core import _give_up_preserve_build

    d, base_sha = _mk_repo(tmp_path)
    # 根 pom 已在 base（tracked）；X 越权把它改脏（scope 里【没有】声明它）
    (d / "pom.xml").write_text(BASE_POM.replace("<groupId>", "<group>"),
                               encoding="utf-8")
    # X 的失败 diff 记录了这次越权写入（这是清扫面的唯一线索）
    x_failed = WorkerOutput(
        subtask_id="st-1",
        diff=("diff --git a/pom.xml b/pom.xml\n--- a/pom.xml\n+++ b/pom.xml\n"
              "@@ -1,1 +1,1 @@\n-<project>\n+<project><group>x</group>\n"),
        summary="越权写根 pom 后失败", l1_passed=False, l1_details={},
        confidence=Confidence.LOW)

    up = _ST("st-1", _Scope(create=["mod/src/main/java/A.java"]))   # scope 无根 pom
    down = _ST("st-2", _Scope(create=["other/B.java"]), deps=["st-1"])
    state = {"plan": _Plan([up, down]), "base_commit": base_sha,
             "project_id": "p-a10", "subtask_results": {"st-1": x_failed},
             "give_up_isolated_ids": [], "abandoned_subtask_ids": []}

    with patch("swarm.brain.nodes._get_brain_llm", return_value=_StubLLM()), \
            patch("swarm.brain.nodes.planning_core._proj_path_from_state",
                  return_value=str(d)):
        out = await _give_up_preserve_build(state, ["st-1"])

    assert out is not None
    det = out["subtask_results"]["st-1"].l1_details or {}
    assert det.get("give_up_mode") == "stub", (
        f"前提未成立：要求走 stub 路，实际 {det.get('give_up_mode')}"
    )
    assert "<group>" not in (d / "pom.xml").read_text(encoding="utf-8"), (
        "X 越权写脏的根 pom 必须被清回 base——scope 驱动的 footprint 够不着它，"
        "必须靠 extra_files（X 自身失败 diff 解析）纳入清扫面"
    )
    assert "pom.xml" in (det.get("stub_residue_cleaned") or []), (
        f"越权写入被清后必须留机读账，实得 {det}"
    )


def test_r2_previous_round_stub_cannot_launder_coords(tmp_path):
    """★复核 R2-HIGH2 锁★ **上一轮的桩**不得给本轮桩的同一臆造版本背书（跨轮洗白）。

    桩的 `l1_passed=True` 是阶梯三为"解锁下游"刻意硬写的**合成值**（零编译零验收）。
    若证据面认它，上一轮桩留在盘上的臆造坐标就成了本轮桩的"已验证证据"——而
    `stub_written` 只减得掉**本轮**写盘集、够不着跨轮（实测：放行）。
    判据用既有单一口径 `give_up_mode`/`given_up`。
    """
    from swarm.brain.nodes.planning_core import (
        _strip_ungrounded_manifest_coords,
        _verified_sibling_files,
    )

    d, _ = _mk_repo(tmp_path)
    # 上一轮桩 st-0：产出 mod-a/pom.xml（含臆造 3.8.7，盘上还在），l1_passed=True + give_up_mode
    (d / "mod-a").mkdir()
    (d / "mod-a" / "pom.xml").write_text(BASE_POM.replace("4.8.3", "3.8.7"),
                                         encoding="utf-8")
    prev_stub = WorkerOutput(
        subtask_id="st-0",
        diff=("diff --git a/mod-a/pom.xml b/mod-a/pom.xml\n--- /dev/null\n"
              "+++ b/mod-a/pom.xml\n@@ -0,0 +1,1 @@\n+  <version>3.8.7</version>\n"),
        summary="上一轮桩", l1_passed=True,
        l1_details={"given_up": True, "give_up_mode": "stub"},
        confidence=Confidence.LOW)
    st0 = _ST("st-0", _Scope(create=["zzz/other.java"]))   # scope 面隔离
    stx = _ST("st-1", _Scope(create=["mod-b/pom.xml"]))

    vf = _verified_sibling_files([st0, stx], {"st-0": prev_stub},
                                exclude_ids={"st-1"}, face="evidence")
    assert vf == set(), f"give-up 产出绝不能进证据面，实得 {vf}"

    # 本轮桩写 mod-b/pom.xml，同一臆造版本
    (d / "mod-b").mkdir()
    (d / "mod-b" / "pom.xml").write_text(BASE_POM.replace("4.8.3", "3.8.7"),
                                         encoding="utf-8")
    cur = ("diff --git a/mod-b/pom.xml b/mod-b/pom.xml\n--- /dev/null\n"
           "+++ b/mod-b/pom.xml\n@@ -0,0 +1,1 @@\n+  <version>3.8.7</version>\n")
    out = _strip_ungrounded_manifest_coords(cur, str(d), "st-1", verified_files=vf,
                                           stub_written=["mod-b/pom.xml"])
    assert "3.8.7" not in (out or ""), (
        "上一轮桩的产出不得给本轮桩洗白同一臆造坐标（桩的 l1_passed 是合成值）"
    )


def test_v4f5_validate_downgraded_sibling_is_not_evidence(tmp_path):
    """★v4 复核 F5 锁★ L1 被降级成 `mvn validate` 的兄弟不得充当接地证据。

    第四个"被当已验证"的同型面（前三个：scope 声明 / 桩合成 l1_passed / give-up 产出）。
    脚手架窗口刻意把 build_cmd 降级成 validate（真编译交 L2 兜），故**本轮源码未经编译**，
    `l1_passed=True` 但**无** give-up 标记 ⇒ 旧判据放它进证据面。
    判据用现成机读键 `build_cmd_downgraded_to_validate`（worker 写、runner 已消费）。

    诚实边界（写进锁的注释以免后人误读）：`mvn validate` 会解析 POM/parent，但未必拉依赖
    artifact，故"它能否为不存在的坐标背书"未实测确证。这里按**分档缺口**处理——宁可少认
    证据也不放行臆造坐标。
    """
    from swarm.brain.nodes.planning_core import _verified_sibling_files

    downgraded = WorkerOutput(
        subtask_id="st-0",
        diff=("diff --git a/sib/pom.xml b/sib/pom.xml\n--- /dev/null\n"
              "+++ b/sib/pom.xml\n@@ -0,0 +1,1 @@\n+  <version>5.5.5</version>\n"),
        summary="L1 降级 validate", l1_passed=True,
        l1_details={"build_cmd_downgraded_to_validate": True,
                    "validate_unverified_sources": ["sib/A.java"]},
        confidence=Confidence.MEDIUM)
    st0 = _ST("st-0", _Scope(create=["sib/pom.xml"]))
    stx = _ST("st-1", _Scope(create=["mod/pom.xml"]))

    vf = _verified_sibling_files([st0, stx], {"st-0": downgraded},
                                exclude_ids={"st-1"}, face="evidence")
    assert vf == set(), (
        f"L1 降级 validate 的兄弟未经真编译，不得进证据面。实得 {vf}"
    )
    # 反向：protect 面仍须保留它（它的产物是真交付物，清理不得删）
    vp = _verified_sibling_files([st0, stx], {"st-0": downgraded},
                                exclude_ids={"st-1"}, face="protect")
    assert "sib/pom.xml" in vp, f"protect 面必须保留其产物，实得 {sorted(vp)}"


def test_r2_protect_face_still_keeps_previous_stub_output(tmp_path):
    """★分档反向锁★ protect 面**必须**保留 give-up 产出——上一轮桩的产物是真交付物。

    与上一条同源同夹具、唯一差别＝face。证据面剔 give-up、保护面保留 give-up：
    两个消费点后果不同（少护会误删交付物，多认会被洗白），故必须分档而非共用一个集合。
    """
    from swarm.brain.nodes.planning_core import _verified_sibling_files

    prev_stub = WorkerOutput(
        subtask_id="st-0",
        diff=("diff --git a/mod-a/pom.xml b/mod-a/pom.xml\n--- /dev/null\n"
              "+++ b/mod-a/pom.xml\n@@ -0,0 +1,1 @@\n+  <version>3.8.7</version>\n"),
        summary="上一轮桩", l1_passed=True,
        l1_details={"given_up": True, "give_up_mode": "stub"},
        confidence=Confidence.LOW)
    st0 = _ST("st-0", _Scope(create=["zzz/other.java"]))
    stx = _ST("st-1", _Scope(create=["mod-b/pom.xml"]))

    vp = _verified_sibling_files([st0, stx], {"st-0": prev_stub},
                                exclude_ids={"st-1"}, face="protect")
    assert "mod-a/pom.xml" in vp, (
        "protect 面必须含 give-up 产出——上一轮桩的产物在 merged_diff 里、是真交付物，"
        f"本轮清理绝不能删它。实得 {sorted(vp)}"
    )


def test_r2_selfcheck_does_not_false_positive_when_stub_equals_base(tmp_path, caplog):
    """★复核 R2-② 锁（R1 双复核整改：换掉无牙断言）★ 桩写的内容恰等于 base ⇒ 真不脏 ⇒ 不得降档。

    我加的脏面自检原实现只看"桩写过却不在 dirty 里"就 fail-closed，而"内容恰等于 base"
    是**合法**情形（实测：把 base 里真实存在的 4.8.3 也剥掉了）。判据必须问 git 要 base
    版内容比对，才能区分"真不脏"与"查询故障"。

    ★为什么原断言没牙（已用突变实跑坐实）★ 原来断的是"`4.8.3` 还在输出里"，而降档动作
    （`_verified` 清空 + 全候选进 `_dirty`）之后所有候选**改读 base 版**，而 base 里
    就有 4.8.3 ⇒ 自检触发与不触发，断言结果**完全相同**。突变（拆掉 (a)/(b) 区分、
    一见不脏就 fail-closed）跑下来：该锁绿、**整文件 26 条锁一条都没红**。
    要断的是**降档动作本身**，不是最终版本号：故本条改为构造一份"只在工作树里、
    经 `verified_files` 背书"的版本——降档会清空 `_verified` ⇒ 它改读 base ⇒ 该版本
    从证据集消失；不降档则留存。这样两条路的可观测结果才真正不同。
    """
    import logging

    from swarm.brain.nodes.planning_core import _strip_ungrounded_manifest_coords

    d, _ = _mk_repo(tmp_path)
    # base 里再放一份 lib/pom.xml（5.5.5），随后工作树改成 7.7.7 并**由兄弟背书为已验证**
    (d / "lib").mkdir()
    (d / "lib" / "pom.xml").write_text(BASE_POM.replace("4.8.3", "5.5.5"),
                                       encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "add lib")
    base_sha = _git(d, "rev-parse", "HEAD").stdout.strip()
    (d / "lib" / "pom.xml").write_text(BASE_POM.replace("4.8.3", "7.7.7"),
                                       encoding="utf-8")
    # 桩"写"了根 pom.xml 但内容与 base 逐字相同 ⇒ 它真不脏（自检的 (a) 情形）
    (d / "pom.xml").write_text(BASE_POM, encoding="utf-8")
    # 桩引用了 7.7.7（只存在于**已验证**的工作树里，base 里是 5.5.5）
    diff = ("diff --git a/mod/pom.xml b/mod/pom.xml\n--- /dev/null\n"
            "+++ b/mod/pom.xml\n@@ -0,0 +1,1 @@\n"
            "+      <version>7.7.7</version>\n")

    with caplog.at_level(logging.INFO, logger="swarm.brain.nodes.planning_core"):
        out = _strip_ungrounded_manifest_coords(
            diff, str(d), "st-1", verified_files={"lib/pom.xml"},
            stub_written=["pom.xml"], base_ref=base_sha)

    # ① 行为面：不降档 ⇒ 已验证兄弟产物（工作树 7.7.7）仍是证据 ⇒ 桩的 7.7.7 留存。
    #    若误判降档：_verified 被清空 ⇒ lib/pom.xml 改读 base(5.5.5) ⇒ 7.7.7 被剥。
    assert "7.7.7" in _added_versions(out), (
        "★降档动作被误触发★ 桩写的内容恰等于 base 是**合法**情形，不得判'查询不可信'；"
        "误判会清空 _verified ⇒ 已过 L1 兄弟引入的真版本改读 base 后消失 ⇒ 合法坐标被剥。"
        f"实得新增行版本={_added_versions(out)}"
    )
    # ② 接线面：断"走的是哪条分支"（这一维原断言完全没有）
    assert "与 base 逐字相同" in caplog.text, (
        f"必须命中自检 (a) 分支的 INFO（真不脏、不降档）。实得日志={caplog.text[-800:]}"
    )
    assert "脏面查询不可信" not in caplog.text, (
        "★区分力★ 这是 (b) 分支的 ERROR；它出现即说明自检误判成了故障"
    )


def test_r2_selfcheck_does_fail_close_when_query_really_untrustworthy(tmp_path, caplog):
    """配对锁（反向）：桩写的内容**不等于** base 却不脏 ⇒ 查询真不可信 ⇒ 必须降档。

    只有上面那条时，"把自检整块删掉（永不降档）"也全绿。两条合起来才把
    (a) 不降档 / (b) 降档 两条路各钉一次——这是 R2-② 整改的完整命题。
    """
    import logging

    from swarm.brain.nodes import planning_core as pc

    d, _ = _mk_repo(tmp_path)
    (d / "lib").mkdir()
    (d / "lib" / "pom.xml").write_text(BASE_POM.replace("4.8.3", "5.5.5"),
                                       encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "add lib")
    base_sha = _git(d, "rev-parse", "HEAD").stdout.strip()
    (d / "lib" / "pom.xml").write_text(BASE_POM.replace("4.8.3", "7.7.7"),
                                       encoding="utf-8")
    # 桩写的根 pom **不等于** base（4.8.3 → 8.8.8）⇒ 它必然脏；
    # 而脏面查询被打成"吞失败返空集"⇒ 自检必须判 (b) 不可信并降档。
    (d / "pom.xml").write_text(BASE_POM.replace("4.8.3", "8.8.8"), encoding="utf-8")
    diff = ("diff --git a/mod/pom.xml b/mod/pom.xml\n--- /dev/null\n"
            "+++ b/mod/pom.xml\n@@ -0,0 +1,1 @@\n"
            "+      <version>7.7.7</version>\n")

    with patch("swarm.git_base.uncommitted_changed_files", lambda p, f: []), \
            caplog.at_level(logging.INFO, logger="swarm.brain.nodes.planning_core"):
        out = pc._strip_ungrounded_manifest_coords(
            diff, str(d), "st-1", verified_files={"lib/pom.xml"},
            stub_written=["pom.xml"], base_ref=base_sha)

    assert "脏面查询不可信" in caplog.text, (
        f"桩写盘且与 base 不同却不在 dirty 里 ⇒ 必须判 (b) 并降档。实得={caplog.text[-800:]}"
    )
    assert "7.7.7" not in _added_versions(out), (
        "★降档必须真生效★ 降档＝_verified 清空 + 全候选按脏 ⇒ lib/pom.xml 改读 base"
        "（5.5.5）⇒ 只存在于未验证工作树的 7.7.7 拿不到背书，必须被剥。"
        f"实得新增行版本={_added_versions(out)}"
    )


def test_r2_strip_syncs_to_disk_not_just_diff(tmp_path):
    """★复核 R2-③ 锁★ 逐行剥离必须**同步落盘**，否则 L2 按本地树构建仍中毒。

    接地闸从诞生起只改 diff 文本，桩写盘那份仍带臆造坐标 ⇒ merged_diff 干净而
    `integration_review` 原地 reset+apply **按本地树**编译 ⇒ 照样中毒；且本批新加的
    `_kept` 保护把这份脏清单结构性护住，谁也清不掉。正是本批要治的"验证树≠交付面"。

    ★夹具改动记录（R1 二轮整改）★ 原夹具用**新建**型桩（`--- /dev/null`，base 里无该清单）。
    R1 二轮把"零 base 证据清单整份不采纳"下沉到两臂共用后，那种形状**不再走逐行剥离**
    （整份丢弃 ⇒ 盘上文件被删 ⇒ 原断言 `read_text` 直接 FileNotFoundError）。
    `_sync_disk` 这套机制**依然存在且依然需要锁**，只是适用面收窄为【有 base 证据的清单】
    ——即 modify 型桩：base 里已有该清单，桩往里加了一个臆造坐标。故把夹具换成 modify 型，
    保住这条锁真正测的东西（剥离与落盘同源），而不是把断言改弱去迁就新行为。
    """
    from swarm.brain.nodes.planning_core import _strip_ungrounded_manifest_coords

    d, _ = _mk_repo(tmp_path)
    # ★modify 型★：base 里**已有** mod/pom.xml（4.8.3），故它有 base 证据、不被整份丢弃
    (d / "mod").mkdir()
    (d / "mod" / "pom.xml").write_text(BASE_POM, encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "base has mod pom")
    base_sha = _git(d, "rev-parse", "HEAD").stdout.strip()
    # 桩往里加了一个臆造坐标 3.8.7。
    # ★盘上那行与 diff 里那行必须逐字同源★ `_sync_disk` 按**整行精确匹配**删除
    # （它刻意不重跑判据，见其 docstring 的 F1 整改），缩进差一个空格就静默不删、
    # 这条锁会以"落盘没生效"假失败——故两侧都从同一个 `_added_line` 派生。
    _added_line = "    <dependency><version>3.8.7</version></dependency>"
    (d / "mod" / "pom.xml").write_text(
        BASE_POM.replace("  </dependencies>", f"{_added_line}\n  </dependencies>"),
        encoding="utf-8")
    diff = ("diff --git a/mod/pom.xml b/mod/pom.xml\n--- a/mod/pom.xml\n"
            "+++ b/mod/pom.xml\n@@ -8,0 +9,1 @@\n"
            f"+{_added_line}\n")

    out = _strip_ungrounded_manifest_coords(
        diff, str(d), "st-1", verified_files=set(),
        stub_written=["mod/pom.xml"], base_ref=base_sha)
    assert "3.8.7" not in _added_versions(out), "diff 侧应已剥离"
    _disk = (d / "mod" / "pom.xml").read_text(encoding="utf-8")
    assert "3.8.7" not in _disk, (
        "盘上那份也必须同步剥离——否则 diff 干净、本地树仍带臆造坐标，L2 按本地树构建中毒"
        f"（验证树≠交付面，正是本批主题）。实得盘上内容：\n{_disk}"
    )
    # 反向：有 base 证据的清单**不得**被整份丢弃（那是 modify 型该走的路）
    assert (d / "mod" / "pom.xml").is_file(), (
        "★modify 型清单有 base 证据，必须走逐行剥离而非整份不采纳★"
    )
    assert "4.8.3" in _disk, "base 里真实存在的坐标必须留在盘上（绝不连坐）"


_VER_RE = __import__("re").compile(r"<version>\s*([^<>\s]+)\s*</version>", __import__("re").I)


def _added_versions(diff_text: str | None) -> set[str]:
    """diff **新增行**里的 version 值集合。

    ★为什么必须有这个 helper★ 剥离只作用于 `+` 行；而"桩改写了含合法版本的那一行"这种形状
    会在 diff 里同时留下一条含同一版本的 `-` 旧行 ⇒ 断"整个 diff 文本里有没有这个串"**恒真**，
    突变照绿（本会话实测踩过：日志明写 `剥离=['3.2.0','9.9.9']` 而断言仍通过）。
    """
    out: set[str] = set()
    for ln in (diff_text or "").splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            m = _VER_RE.search(ln)
            if m:
                out.add(m.group(1))
    return out


MOD_BASE_POM = """<project>
  <parent>
    <version>1.0.0</version>
  </parent>
  <dependencies>
    <dependency>
      <version>2.5.0</version>
    </dependency>
  </dependencies>
</project>
"""


def test_v4f1_modify_type_stub_keeps_base_coords_on_disk(tmp_path):
    """★v4 复核 F1 锁（缺的那个形状）★ **桩改写既有清单**时，base 合法坐标不得被删。

    此前 20 条锁的桩清单要么是**新建**（diff 全 `+` 行）、要么**内容恰等于 base**（无可剥），
    没有一条覆盖"桩改写既有清单、diff 里带 context 行"——F1 就藏在这个没被构造过的形状里。

    两个病根（都已实测）：
      ① `_sync_disk` 初版对**盘上每一行**重施判据，而 diff 侧只对 `+` 行施判据 ⇒ base 原有的
         `<parent><version>` 与既有依赖版本（在 diff 里是 context 行）被从盘上删掉
         ——正是 D3 铁律点名的"毁 `<parent>` 让整棵 reactor 解析期崩"。
      ② 那份清单自己是 dirty ⇒ 被整份剔出证据集 ⇒ 它**自己 base 里的合法坐标**也失去证据
         资格 ⇒ 连真坐标一起判"查无实据"。治法＝脏清单改读 **base 版**当证据。
    """
    from swarm.brain.nodes.planning_core import _strip_ungrounded_manifest_coords

    d, _ = _mk_repo(tmp_path)
    (d / "mod").mkdir()
    (d / "mod" / "pom.xml").write_text(MOD_BASE_POM, encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "mod base")
    base_sha = _git(d, "rev-parse", "HEAD").stdout.strip()
    # 桩改写：保留原有两个合法坐标，追加臆造 3.8.7
    (d / "mod" / "pom.xml").write_text(
        MOD_BASE_POM.replace(
            "  </dependencies>",
            "    <dependency>\n      <version>3.8.7</version>\n    </dependency>\n"
            "  </dependencies>"),
        encoding="utf-8")
    diff = _git(d, "--no-pager", "diff", base_sha, "--", "mod/pom.xml").stdout
    assert "3.8.7" in diff, "夹具前提：臆造坐标应出现在 diff 的新增行里"

    out = _strip_ungrounded_manifest_coords(
        diff, str(d), "st-1", verified_files=set(),
        stub_written=["mod/pom.xml"], base_ref=base_sha)

    assert "3.8.7" not in (out or ""), "臆造坐标仍须被剥离"
    disk = (d / "mod" / "pom.xml").read_text(encoding="utf-8")
    assert "1.0.0" in disk and "<parent>" in disk, (
        "base 里的 <parent><version> 绝不能被落盘剥离删掉（D3 铁律：毁 parent = reactor 崩）"
    )
    assert "2.5.0" in disk, "base 里既有依赖版本也不能被删"
    assert "3.8.7" not in disk, "盘上那份臆造坐标应同步剥离（验证树=交付面）"


def test_v4f1_sync_disk_only_removes_lines_the_diff_dropped(tmp_path):
    """★F1 判据同源锁★ 落盘只删【diff 侧真被剥掉的那几行】，绝不重跑判据。

    区分力：若落盘侧重跑"不在 _known 就删"的判据，则 base 里合法但不在 _known 的行也会被删。
    本条构造一个 `_known` 里没有、但**不在 diff 新增行**里的合法坐标（context 行），
    断它在盘上活着。
    """
    from swarm.brain.nodes.planning_core import _strip_ungrounded_manifest_coords

    d, _ = _mk_repo(tmp_path)
    (d / "mod").mkdir()
    (d / "mod" / "pom.xml").write_text(MOD_BASE_POM, encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "mod base")
    base_sha = _git(d, "rev-parse", "HEAD").stdout.strip()
    (d / "mod" / "pom.xml").write_text(
        MOD_BASE_POM.replace("2.5.0", "2.5.0").replace(
            "  </dependencies>",
            "    <dependency>\n      <version>7.7.7</version>\n    </dependency>\n"
            "  </dependencies>"),
        encoding="utf-8")
    diff = _git(d, "--no-pager", "diff", base_sha, "--", "mod/pom.xml").stdout

    _strip_ungrounded_manifest_coords(
        diff, str(d), "st-1", verified_files=set(),
        stub_written=["mod/pom.xml"], base_ref=base_sha)
    disk = (d / "mod" / "pom.xml").read_text(encoding="utf-8")
    assert "2.5.0" in disk, "context 行的合法坐标必须留在盘上（落盘判据不得重跑）"
    assert "1.0.0" in disk


def test_v4_legit_coord_on_added_line_survives(tmp_path):
    """★关键形状锁（F1b + F2 的承重锁）★ 合法坐标出现在 **`+` 行**时必须活下来。

    为什么必须单独构造这个形状：前面所有夹具里 base 的合法坐标都是 **context 行**，
    而剥离只作用于 `+` 行 ⇒ 无论"脏清单读 base"和"git show 带 ./"这两条治法在不在，
    合法坐标都活着 ⇒ 两条治法**互相兜底、都不可证伪**（实测两条突变皆绿）。
    本条让桩**改写**含合法版本的那一行（缩进变化即可），使它在 diff 里成为 `+` 行：
      · 治法在：脏清单读 base ⇒ 1.1.1 进 `_known` ⇒ 保留。
      · 任一治法坏：`_known` 拿不到 1.1.1 ⇒ 被判查无实据 ⇒ 剥掉（本条红）。
    """
    from swarm.brain.nodes.planning_core import _strip_ungrounded_manifest_coords

    d, _ = _mk_repo(tmp_path)
    (d / "mod").mkdir()
    (d / "mod" / "pom.xml").write_text(
        "<project>\n  <dependencies>\n    <dependency>\n"
        "      <version>1.1.1</version>\n"
        "    </dependency>\n  </dependencies>\n</project>\n", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "mod base")
    base_sha = _git(d, "rev-parse", "HEAD").stdout.strip()
    # 桩改写：把含合法 1.1.1 的那行**改了缩进**（⇒ 它在 diff 里变成 -/+ 对），并追加臆造
    (d / "mod" / "pom.xml").write_text(
        "<project>\n  <dependencies>\n    <dependency>\n"
        "        <version>1.1.1</version>\n"          # 缩进变了 ⇒ + 行
        "    </dependency>\n"
        "    <dependency>\n      <version>8.8.8</version>\n    </dependency>\n"
        "  </dependencies>\n</project>\n", encoding="utf-8")
    diff = _git(d, "--no-pager", "diff", base_sha, "--", "mod/pom.xml").stdout
    _added = [ln for ln in diff.splitlines()
              if ln.startswith("+") and not ln.startswith("+++")]
    assert any("1.1.1" in ln for ln in _added), (
        f"夹具前提失效：合法坐标必须出现在 + 行，实得新增行 {_added}"
    )

    out = _strip_ungrounded_manifest_coords(
        diff, str(d), "st-1", verified_files=set(),
        stub_written=["mod/pom.xml"], base_ref=base_sha)
    assert "1.1.1" in _added_versions(out), (
        "base 里真实存在的坐标即便出现在 + 行也不得被剥离——脏清单必须读 base 版当证据。"
        f"实得新增行版本={_added_versions(out)}"
    )
    assert "8.8.8" not in _added_versions(out), "臆造坐标仍须剥离"
    disk = (d / "mod" / "pom.xml").read_text(encoding="utf-8")
    assert "1.1.1" in disk, "盘上合法坐标也不能被删"
    assert "8.8.8" not in disk, "盘上臆造坐标须同步剥离"


def test_v4f2_git_show_uses_dot_prefix_for_subdir_project(tmp_path):
    """★v4 复核 F2 锁★ `project_path` 是仓库**子目录**时，base 内容读取仍须成功。

    `git show <rev>:<path>` 的 path 是**仓根相对**，而闸内 rel 是 project_path 相对。
    不带 `./` 时 git 直接致命错误（实测 rc=128：`'backend/pom.xml' 路徑存在，但不是
    'pom.xml'`，git 自己提示应写 `<rev>:./pom.xml`）⇒ base 读不到 ⇒ 证据面塌成空
    ⇒ fail-closed 误剥真坐标。
    """
    from swarm.brain.nodes.planning_core import _strip_ungrounded_manifest_coords

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    be = repo / "backend"
    be.mkdir()
    # ★两个条件必须同时满足才有区分力★：① project_path 是**子目录**（否则不带 ./ 也对齐、
    # 突变无害）；② 合法坐标落在 **`+` 行**（否则剥离只作用于 + 行、它无论如何都活着）。
    # 少任一条这条锁就是 vacuous 绿——两条都是实测踩出来的。
    (be / "pom.xml").write_text(
        "<project>\n  <dependencies>\n    <dependency>\n"
        "      <version>3.2.0</version>\n"
        "    </dependency>\n  </dependencies>\n</project>\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # 桩改写含合法 3.2.0 的那一行（改缩进 ⇒ 它成为 + 行）+ 追加臆造 9.9.9
    (be / "pom.xml").write_text(
        "<project>\n  <dependencies>\n    <dependency>\n"
        "        <version>3.2.0</version>\n"
        "    </dependency>\n"
        "    <dependency>\n      <version>9.9.9</version>\n    </dependency>\n"
        "  </dependencies>\n</project>\n", encoding="utf-8")
    diff = _git(repo, "--no-pager", "diff", base_sha, "--", "backend/pom.xml").stdout
    diff = diff.replace("a/backend/pom.xml", "a/pom.xml").replace(
        "b/backend/pom.xml", "b/pom.xml")   # 闸内口径=project_path 相对
    _added = [ln for ln in diff.splitlines()
              if ln.startswith("+") and not ln.startswith("+++")]
    assert any("3.2.0" in ln for ln in _added), (
        f"夹具前提失效：合法坐标必须在 + 行，实得 {_added}"
    )

    out = _strip_ungrounded_manifest_coords(
        diff, str(be), "st-1", verified_files=set(),
        stub_written=["pom.xml"], base_ref=base_sha)
    # ★断言只看 `+` 行★ 改缩进会让 diff 里同时留一条含该版本的 `-` 旧行，
    # 断"整个 diff 文本里有没有这个串"会恒真（我第一版就这么写错，突变照绿）。
    assert _added_versions(out) and "3.2.0" in _added_versions(out), (
        "子目录工程里 base 真坐标不得被误剥——`git show <rev>:<path>` 的 path 是仓根相对，"
        "而闸内 rel 是 project_path 相对，不带 ./ 时 git 致命错误 ⇒ base 读不到 ⇒ 证据面塌空"
        f" ⇒ fail-closed 误剥真坐标。实得新增行版本={_added_versions(out)}"
    )
    assert "9.9.9" not in _added_versions(out), "臆造坐标仍须剥离"


def test_v4f7_selfcheck_covers_manifests_outside_walk_candidates(tmp_path, monkeypatch):
    """★v4 复核 F7 锁★ 桩写的清单落在 walk 够不着的位置时，自检仍须覆盖它。

    `_cands` 的 walk **跳 dot 目录**且有 `[:80]` 目录截断。若自检判据依赖 `_cands` 成员
    资格，桩写在 `.build/pom.xml` 这类位置的清单就**结构性零覆盖**——脏面查询真坏了也
    照不出来（本条即那个形状）。桩写的清单必然存在，逐个查即可，不该依赖 walk 结果。
    """
    from swarm.brain.nodes import planning_core as pc

    d, base_sha = _mk_repo(tmp_path)
    # X 越权把【根 pom】写脏并留下臆造 6.6.6（它在 _cands 里，会被当证据源读）
    (d / "pom.xml").write_text(BASE_POM.replace("4.8.3", "6.6.6"), encoding="utf-8")
    # 桩只写 dot 目录里的清单 —— walk 跳 dot 目录 ⇒ 它不在 _cands 里
    hidden = d / ".build"
    hidden.mkdir()
    (hidden / "pom.xml").write_text(BASE_POM.replace("4.8.3", "6.6.6"),
                                    encoding="utf-8")
    diff = ("diff --git a/.build/pom.xml b/.build/pom.xml\n--- /dev/null\n"
            "+++ b/.build/pom.xml\n@@ -0,0 +1,1 @@\n"
            "+      <version>6.6.6</version>\n")

    # 脏面查询坏掉（callee 把自己所有失败吞成空集——它的真实行为）
    monkeypatch.setattr("swarm.git_base.uncommitted_changed_files", lambda p, f: [])
    out = pc._strip_ungrounded_manifest_coords(
        diff, str(d), "st-1", verified_files=set(),
        stub_written=[".build/pom.xml"], base_ref=base_sha)
    # 治法在：自检发现"桩写的清单不在 dirty 里"⇒ 判查询不可信 ⇒ 全按未验证 ⇒ 读 base
    #         ⇒ 根 pom base 里只有 4.8.3 ⇒ 6.6.6 拿不到背书 ⇒ 剥离。
    # 治法坏（自检依赖 _cands 成员资格）：_selfcheck 空 ⇒ 不降档 ⇒ 根 pom 按"未改动"读
    #         工作树 ⇒ 6.6.6 进 _known ⇒ 桩的 6.6.6 被自己写脏的根 pom 背书放行。
    assert "6.6.6" not in _added_versions(out), (
        "桩写在 walk 够不着位置（dot 目录/[:80] 截断）的清单同样要被自检覆盖——否则脏面查询"
        "坏掉时无人发现，X 写脏的清单就能给臆造坐标背书。"
        f"实得新增行版本={_added_versions(out)}"
    )


def test_dirty_root_pom_reads_base_and_still_strips_ungrounded(tmp_path):
    """脏根 pom 改读 base 版：桩的臆造坐标仍被剥，而 `${}` 引用不动。

    ★改名记录（R1 双复核整改）★ 本条原名 `test_med3_all_candidates_excluded_is_
    fail_closed_not_silent_pass`，以 fail-closed 臂命名，但**从来没走到那条臂**——
    已用 `--log-cli-level=INFO` 实跑坐实：它命中的是"接地证据集对 1 份【本轮改脏】清单
    改读 base 版"+ 正常路径 WARNING，fail-closed 臂独有的 ERROR 串命中 0 次。
    成因＝(b) 子治法"脏清单读 base 版"落地后，根 pom 的 base 里有 4.8.3 ⇒ `_known`
    非空 ⇒ 早于它的这条锁被静默改道到正常路径（"改共享代码必复跑上一批 harness"的
    变体：改的是治法的补救动作，受害的是上一版为它写的锁）。
    它测到的价值是真的（脏清单读 base + `${}` 豁免），故按真实语义改名保留；
    真正的 fail-closed 锁见下面 `test_r1_*` 两条。
    """
    from swarm.brain.nodes.planning_core import _strip_ungrounded_manifest_coords

    d, _ = _mk_repo(tmp_path)
    # 唯一的既有清单（根 pom）被改脏且未验证 ⇒ 该清单改读 base 版（base 里有 4.8.3）
    (d / "pom.xml").write_text(BASE_POM.replace("4.8.3", "9.9.9"), encoding="utf-8")
    diff = ("diff --git a/mod/pom.xml b/mod/pom.xml\n--- /dev/null\n"
            "+++ b/mod/pom.xml\n@@ -0,0 +1,2 @@\n"
            "+  <version>3.8.7</version>\n"
            "+  <version>${project.version}</version>\n")

    out = _strip_ungrounded_manifest_coords(diff, str(d), "st-1", verified_files=set())
    assert "3.8.7" not in _added_versions(out), (
        "桩的臆造坐标必须被剥（base 里没有 3.8.7）"
    )
    assert "9.9.9" not in _added_versions(out), (
        "★关键区分★ 脏工作树里的 9.9.9 不得成为证据——它是本轮未验证内容"
    )
    assert "${project.version}" in (out or ""), (
        "${} 是引用不是坐标，任何档位都不该剥离它"
    )


def test_med2_untrustworthy_dirty_query_is_fail_closed(tmp_path):
    """★复核 MED-2 整改锁★ 脏面查询不可信（callee 把失败吞成空集）⇒ fail-closed。

    `uncommitted_changed_files` 把 git 超时 / rc!=0 / OSError **全部吞成 `[]` 且不抛**
    （git_base.py），所以闸里的 except 只接得住 ImportError；真实失败面走"正常返回空集"
    ⇒ `_dirty` 恒空 ⇒ 全部候选被当已验证 ⇒ 闸静默退回原缺陷。
    自检判据：桩刚 write_text 过的清单**必然脏**，它落在候选里却不在 dirty 里 ⇒ 结果不可信。
    """
    from swarm.brain.nodes import planning_core as pc

    d, _ = _mk_repo(tmp_path)
    (d / "mod").mkdir()
    (d / "mod" / "pom.xml").write_text(BASE_POM.replace("4.8.3", "3.8.7"),
                                       encoding="utf-8")
    diff = ("diff --git a/mod/pom.xml b/mod/pom.xml\n--- /dev/null\n"
            "+++ b/mod/pom.xml\n@@ -0,0 +1,1 @@\n+  <version>3.8.7</version>\n")

    # 模拟 callee 任何内部失败（超时/rc!=0/OSError/口径失配）——它一律返空集、不抛
    with patch("swarm.git_base.uncommitted_changed_files", lambda p, f: []):
        out = pc._strip_ungrounded_manifest_coords(
            diff, str(d), "st-1", verified_files=set(),
            stub_written=["mod/pom.xml"])
    assert "3.8.7" not in (out or ""), (
        "脏面查询不可信时必须 fail-closed——桩刚写盘的清单必然脏，它不在 dirty 集里就说明"
        "查询结果不可用（callee 把失败吞成空集），此时绝不能让未验证树状态给臆造坐标背书"
    )


def _passed_wo(sid: str, diff: str = "") -> WorkerOutput:
    """已过 L1 的兄弟产出（HIGH-1/MED-4 三条锁的共同前提：这个取值域此前从未被构造过）。"""
    return WorkerOutput(subtask_id=sid, diff=diff, summary="ok", l1_passed=True,
                        l1_details={}, confidence=Confidence.HIGH)


@pytest.mark.asyncio
async def test_h1_sibling_scope_declaration_is_not_evidence(tmp_path):
    """★复核 HIGH-1 区分力锁★ 兄弟 scope【声明】了桩要写的清单但**没真写** ⇒ 仍须剥离。

    坐实过的失败形态：`_verified_sibling_files` 把已过 L1 兄弟的 **scope 声明**当"已验证
    产物"，而 scope 只是"**允许**谁写"。重叠在生产上真实存在
    （`_grant_module_pom_writable` docstring：owner 可能是已 DONE 脚手架，二者都写同一清单）
    ⇒ 桩自己刚写的臆造坐标落进 verified ⇒ 自证放行（A10-M2 换扇门复发）。
    两组唯一差别＝兄弟 l1_passed，实测 False 剥离 / True 放行。
    """
    from swarm.brain.nodes.planning_core import _give_up_preserve_build

    class _LLMFakeVer:
        async def ainvoke(self, _msgs):
            return _Resp(json.dumps({"files": {
                "mod/src/main/java/A.java": "package mod;\npublic class A {}\n",
                "mod/pom.xml": BASE_POM.replace("4.8.3", "3.8.7"),  # 臆造
            }}))

    d, base_sha = _mk_repo(tmp_path)
    # st-0：已 DONE 的脚手架，scope【声明】同一份清单，但其 diff 为空（没真写）
    scaf = _ST("st-0", _Scope(create=["mod/pom.xml"]))
    up = _ST("st-1", _Scope(create=["mod/src/main/java/A.java", "mod/pom.xml"]))
    down = _ST("st-2", _Scope(create=["other/B.java"],
                              upstream=["mod/pom.xml"]), deps=["st-1"])
    state = {"plan": _Plan([scaf, up, down]), "base_commit": base_sha,
             "project_id": "p-a10",
             "subtask_results": {"st-0": _passed_wo("st-0")},   # ★兄弟已过 L1★
             "give_up_isolated_ids": [], "abandoned_subtask_ids": []}

    with patch("swarm.brain.nodes._get_brain_llm", return_value=_LLMFakeVer()), \
            patch("swarm.brain.nodes.planning_core._proj_path_from_state",
                  return_value=str(d)):
        out = await _give_up_preserve_build(state, ["st-1"])

    assert out is not None
    wo = out["subtask_results"]["st-1"]
    assert "3.8.7" not in (wo.diff or ""), (
        "兄弟 scope【声明】不等于'盘上这份内容已验证'——拿它当证据会让桩自己给自己背书"
        "（证据面必须 include_scope=False 且减掉桩本轮写盘集）"
    )


@pytest.mark.asyncio
async def test_h1_stub_overwrite_voids_sibling_diff_evidence(tmp_path):
    """★HIGH-1 第二道（`_verified -= stub_written` 的独立锁）★
    兄弟已过 L1 且其**实际 diff** 写过同一路径，但桩**覆写**了它 ⇒ 盘上内容是桩的 ⇒ 不算证据。

    为什么必须单独有这条：`include_scope=False`（剔 scope 面）与 `_verified -= stub_written`
    是两层防御，只要兄弟 diff 面不含该路径，前者就已挡住 ⇒ 后者恒 no-op ⇒ 删掉它测试仍绿
    （"冗余防御=互相兜底=两条都不可证伪"）。本条让兄弟 diff **确实**含该路径，
    使 stub_written 那一减成为唯一防线，突变掉它即红。
    """
    from swarm.brain.nodes.planning_core import _give_up_preserve_build

    class _LLMOverwrite:
        async def ainvoke(self, _msgs):
            return _Resp(json.dumps({"files": {
                "mod/src/main/java/A.java": "package mod;\npublic class A {}\n",
                "mod/pom.xml": BASE_POM.replace("4.8.3", "3.8.7"),   # 桩覆写 + 臆造
            }}))

    d, base_sha = _mk_repo(tmp_path)
    # 兄弟 st-0 已过 L1，其【实际 diff】写过 mod/pom.xml（曾经是它的合法产物）
    sib_diff = ("diff --git a/mod/pom.xml b/mod/pom.xml\n--- /dev/null\n"
                "+++ b/mod/pom.xml\n@@ -0,0 +1,1 @@\n+  <version>4.8.3</version>\n")
    scaf = _ST("st-0", _Scope(create=["other/C.java"]))   # scope 面故意不含它（隔离 include_scope）
    up = _ST("st-1", _Scope(create=["mod/src/main/java/A.java", "mod/pom.xml"]))
    down = _ST("st-2", _Scope(create=["other/B.java"],
                              upstream=["mod/pom.xml"]), deps=["st-1"])
    state = {"plan": _Plan([scaf, up, down]), "base_commit": base_sha,
             "project_id": "p-a10",
             "subtask_results": {"st-0": _passed_wo("st-0", diff=sib_diff)},
             "give_up_isolated_ids": [], "abandoned_subtask_ids": []}

    with patch("swarm.brain.nodes._get_brain_llm", return_value=_LLMOverwrite()), \
            patch("swarm.brain.nodes.planning_core._proj_path_from_state",
                  return_value=str(d)):
        out = await _give_up_preserve_build(state, ["st-1"])

    assert out is not None
    wo = out["subtask_results"]["st-1"]
    assert "3.8.7" not in (wo.diff or ""), (
        "桩覆写了兄弟曾产出的路径 ⇒ 盘上那份内容已是桩的，兄弟的 diff 账不再为它背书"
        "（证据面必须减掉桩本轮写盘集）"
    )


@pytest.mark.asyncio
async def test_med4_sibling_actual_diff_version_is_evidence_via_real_entry(tmp_path):
    """★复核 MED-4 接线锁★ 走**真入口**：兄弟已过 L1 且其【实际 diff】引入的版本算证据。

    此前反向锁是直接给闸传 `verified_files=`，**绕过了调用点** ⇒ 把调用点实参整块删掉
    （证据面退成 base-only）时 10 条全绿。本条经 `_give_up_preserve_build` 驱动，
    锁住"调用点真的把已过 L1 兄弟的 diff 面传进闸了"。
    """
    from swarm.brain.nodes.planning_core import _give_up_preserve_build

    class _LLMSibVer:
        async def ainvoke(self, _msgs):
            return _Resp(json.dumps({"files": {
                "mod/src/main/java/A.java": "package mod;\npublic class A {}\n",
                # 引用兄弟已引入的 7.7.7（合法接地，不该被剥）
                "mod/pom.xml": BASE_POM.replace("4.8.3", "7.7.7"),
            }}))

    d, base_sha = _mk_repo(tmp_path)
    # 兄弟 st-0 已过 L1，其【实际 diff】写了 sib/pom.xml 并引入 7.7.7；盘上也有那份内容
    (d / "sib").mkdir()
    (d / "sib" / "pom.xml").write_text(BASE_POM.replace("4.8.3", "7.7.7"),
                                       encoding="utf-8")
    sib_diff = ("diff --git a/sib/pom.xml b/sib/pom.xml\n--- /dev/null\n"
                "+++ b/sib/pom.xml\n@@ -0,0 +1,1 @@\n+  <version>7.7.7</version>\n")
    scaf = _ST("st-0", _Scope(create=["sib/pom.xml"]))
    up = _ST("st-1", _Scope(create=["mod/src/main/java/A.java", "mod/pom.xml"]))
    down = _ST("st-2", _Scope(create=["other/B.java"],
                              upstream=["mod/pom.xml"]), deps=["st-1"])
    state = {"plan": _Plan([scaf, up, down]), "base_commit": base_sha,
             "project_id": "p-a10",
             "subtask_results": {"st-0": _passed_wo("st-0", diff=sib_diff)},
             "give_up_isolated_ids": [], "abandoned_subtask_ids": []}

    with patch("swarm.brain.nodes._get_brain_llm", return_value=_LLMSibVer()), \
            patch("swarm.brain.nodes.planning_core._proj_path_from_state",
                  return_value=str(d)):
        out = await _give_up_preserve_build(state, ["st-1"])

    assert out is not None
    wo = out["subtask_results"]["st-1"]
    assert (wo.l1_details or {}).get("give_up_mode") == "stub", (
        f"前提未成立：要求走 stub 路，实际 {(wo.l1_details or {}).get('give_up_mode')}"
    )
    assert "7.7.7" in (wo.diff or ""), (
        "已过 L1 兄弟【实际 diff】引入的版本是真接地证据——调用点必须把该面传进闸"
        "（突变掉 verified_files= 实参 / 把 include_scope 面算错时本条必红）"
    )


@pytest.mark.asyncio
async def test_med4_protected_covers_sibling_diff_only_artifact(tmp_path):
    """★复核 MED-4 protected 接线锁★ 兄弟产物**只在 diff 不在 scope** 且落在清扫面内 ⇒ 不得删。

    清扫面已扩到 scope 外（extra_files），故 protected 必须同步含【兄弟实际 diff 路径】，
    否则会误删兄弟越权但有效的产物。突变掉 protected 的 diff 面这一维时本条必红。
    """
    from swarm.brain.nodes.planning_core import _give_up_preserve_build

    d, base_sha = _mk_repo(tmp_path)
    # 兄弟 st-0 已过 L1，越权写了 shared/util.txt（**不在**它的 scope 声明里）
    (d / "shared").mkdir()
    (d / "shared" / "util.txt").write_text("sibling valid output\n", encoding="utf-8")
    sib_diff = ("diff --git a/shared/util.txt b/shared/util.txt\n--- /dev/null\n"
                "+++ b/shared/util.txt\n@@ -0,0 +1,1 @@\n+sibling valid output\n")
    scaf = _ST("st-0", _Scope(create=["other/C.java"]))       # scope 里没有 shared/util.txt
    # X 的失败 diff 也碰过同一个文件 ⇒ 进 X 的 extra 清扫面 ⇒ 若无保护就会被清掉
    x_failed = WorkerOutput(
        subtask_id="st-1",
        diff=("diff --git a/shared/util.txt b/shared/util.txt\n--- a/shared/util.txt\n"
              "+++ b/shared/util.txt\n@@ -1,1 +1,1 @@\n-old\n+x touched\n"),
        summary="也写过该文件后失败", l1_passed=False, l1_details={},
        confidence=Confidence.LOW)
    up = _ST("st-1", _Scope(create=["mod/src/main/java/A.java"]))
    down = _ST("st-2", _Scope(create=["other/B.java"]), deps=["st-1"])
    state = {"plan": _Plan([scaf, up, down]), "base_commit": base_sha,
             "project_id": "p-a10",
             "subtask_results": {"st-0": _passed_wo("st-0", diff=sib_diff),
                                 "st-1": x_failed},
             "give_up_isolated_ids": [], "abandoned_subtask_ids": []}

    with patch("swarm.brain.nodes._get_brain_llm", return_value=_StubLLM()), \
            patch("swarm.brain.nodes.planning_core._proj_path_from_state",
                  return_value=str(d)):
        out = await _give_up_preserve_build(state, ["st-1"])

    assert out is not None
    assert (d / "shared" / "util.txt").is_file(), (
        "兄弟已过 L1 的产物（只在其 diff、不在其 scope）落在 X 清扫面内时必须被 protected "
        "护住——清扫面扩到 scope 外后，纯 scope 保护会误删它"
    )
    assert "sibling valid output" in (d / "shared" / "util.txt").read_text(
        encoding="utf-8"), "兄弟产物内容不得被回退"


@pytest.mark.asyncio
async def test_m1_empty_write_set_leaves_machine_readable_key(tmp_path):
    """★"空返回必须机读可辨"锁★ 写盘集为空时跳过清理，但必须留机读键。

    `files_from_unified_diff` 返空是**正常返回不抛异常**——初版实现只在 except 里留
    WARNING，空集路径既无日志也无机读键 ⇒ 清理静默 no-op 而账面看不出（norms 层死 12 天
    同型）。现在契约是"跳过必须写 `stub_residue_skipped`"。突变删该键时本条必红。
    """
    from swarm.brain.nodes import planning_core as pc

    d, base_sha = _mk_repo(tmp_path)
    up = _ST("st-1", _Scope(create=["mod/src/main/java/A.java"],
                            writable=["mod/pom.xml"]))
    down = _ST("st-2", _Scope(create=["other/B.java"]), deps=["st-1"])
    state = {"plan": _Plan([up, down]), "base_commit": base_sha,
             "project_id": "p-a10", "subtask_results": {},
             "give_up_isolated_ids": [], "abandoned_subtask_ids": []}

    async def _stub_no_written(*a, **k):
        # 桩产出了 diff 但写盘集为空＝上游契约被破坏（本该不可能，故必须机读可辨）
        return "diff --git a/x b/x\n--- /dev/null\n+++ b/x\n@@ -0,0 +1 @@\n+x\n", []

    with patch.object(pc, "_proj_path_from_state", return_value=str(d)), \
            patch.object(pc, "_generate_compile_stub", _stub_no_written):
        out = await pc._give_up_preserve_build(state, ["st-1"])

    assert out is not None
    det = out["subtask_results"]["st-1"].l1_details or {}
    assert det.get("stub_residue_skipped") == "empty_written_set", (
        f"跳过清理必须机读可辨（否则静默 no-op 无人知道），实得 {det}"
    )


# ─────────────── R4：点前缀路径归一（`lstrip("./")` 剥字符集合而非前缀）───────────────


@pytest.mark.asyncio
async def test_r4_dot_prefixed_out_of_scope_write_is_cleaned(tmp_path):
    """★R4 主锁：点前缀路径的越权写入也必须被真清出树★

    与 `test_m1_out_of_scope_write_is_also_cleaned` 同形状，只把落点从 `pom.xml` 换成
    **`.mvn/jvm.config`**（JVM 工程真实且承重的文件）。换这一个字符就跨过了缺陷边界：

    `lstrip("./")` 剥的是**字符集合**不是前缀 ⇒ `.mvn/jvm.config` → `mvn/jvm.config`。
    该串被 `_local_tree_revert_subtask` 直接拿去访问盘与 git（`git checkout -- rel`、
    `root / rel`）⇒ `ls-files` 不匹配(tracked=False) → `abs_f.is_file()` 也 False →
    删除分支那个 `if` **没有 else** ⇒ **静默零清理，连 revert_failed 都不记**：残留留在
    树上毒 L2/reactor，而交付面（五处脏树判据取 merged_diff 集）完全看不见。

    形状要点（缺一条这条锁就换了命题）：
      · `.mvn/jvm.config` **不在** base ⇒ untracked ⇒ 走**删除**分支（`is_file()` 那条），
        正是"缺席不可辨"咬人的那条；tracked 的 checkout 分支 rc!=0 至少会记 revert_failed。
      · scope 里【不】声明它 ⇒ 只能靠 `extra_files`（X 自身失败 diff）进清扫面，
        即被测的那条归一链：`_clean_stub_residue._norm` → `extra_files` → `:630` 归一 →
        append 进 `_footprint` → 盘访问。
      · 下游依赖 st-1 ⇒ 走 stub 路。
    """
    from swarm.brain.nodes.planning_core import _give_up_preserve_build

    d, base_sha = _mk_repo(tmp_path)
    # X 越权写了 .mvn/jvm.config（untracked：base 里没有它），scope 无声明
    (d / ".mvn").mkdir()
    (d / ".mvn" / "jvm.config").write_text("-Xmx9999g\n", encoding="utf-8")
    x_failed = WorkerOutput(
        subtask_id="st-1",
        diff=("diff --git a/.mvn/jvm.config b/.mvn/jvm.config\n"
              "--- /dev/null\n+++ b/.mvn/jvm.config\n"
              "@@ -0,0 +1 @@\n+-Xmx9999g\n"),
        summary="越权写 .mvn/jvm.config 后失败", l1_passed=False, l1_details={},
        confidence=Confidence.LOW)

    up = _ST("st-1", _Scope(create=["mod/src/main/java/A.java"]))
    down = _ST("st-2", _Scope(create=["other/B.java"]), deps=["st-1"])
    state = {"plan": _Plan([up, down]), "base_commit": base_sha,
             "project_id": "p-a10", "subtask_results": {"st-1": x_failed},
             "give_up_isolated_ids": [], "abandoned_subtask_ids": []}

    with patch("swarm.brain.nodes._get_brain_llm", return_value=_StubLLM()), \
            patch("swarm.brain.nodes.planning_core._proj_path_from_state",
                  return_value=str(d)):
        out = await _give_up_preserve_build(state, ["st-1"])

    assert out is not None
    det = out["subtask_results"]["st-1"].l1_details or {}
    assert det.get("give_up_mode") == "stub", (
        f"前提未成立：要求走 stub 路，实际 {det.get('give_up_mode')}"
    )
    assert not (d / ".mvn" / "jvm.config").exists(), (
        "点前缀的越权写入必须被真清出树：归一若用 lstrip('./') 会削成 "
        "'mvn/jvm.config'（盘上不存在）⇒ tracked=False 且 is_file()=False ⇒ "
        "静默零清理，残留毒 build 而交付面无痕"
    )
    assert ".mvn/jvm.config" in (det.get("stub_residue_cleaned") or []), (
        f"清掉了必须留机读账，且账里的路径必须是【未被削过的】原始相对路径，实得 {det}"
    )


# ─────────────── R1（A10-M2 双复核整改）：fail-closed 臂真被走到 + 整份不采纳 ───────────────

GREENFIELD_MOD_POM = """<project>
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>com.acme</groupId>
    <artifactId>acme-parent</artifactId>
    <version>3.2.0</version>
  </parent>
  <artifactId>acme-mod</artifactId>
  <version>1.0.0</version>
</project>
"""

GREENFIELD_MOD_DIFF = (
    "diff --git a/mod/pom.xml b/mod/pom.xml\n"
    "--- /dev/null\n+++ b/mod/pom.xml\n@@ -0,0 +1,9 @@\n"
    "+<project>\n"
    "+  <modelVersion>4.0.0</modelVersion>\n"
    "+  <parent>\n"
    "+    <groupId>com.acme</groupId>\n"
    "+    <artifactId>acme-parent</artifactId>\n"
    "+    <version>3.2.0</version>\n"
    "+  </parent>\n"
    "+  <artifactId>acme-mod</artifactId>\n"
    "+  <version>1.0.0</version>\n"
    "+</project>\n"
)


def _mk_greenfield_repo(tmp_path: Path) -> tuple[Path, str]:
    """真 git 仓，base 里**没有任何构建清单**（greenfield）。

    ★这个夹具形状是 R1 的命门★ `_mk_repo` 的 base 里有根 pom（含 4.8.3）⇒ `_known`
    永不为空 ⇒ fail-closed 臂结构性不可达。26 条老锁全用 `_mk_repo`，这就是那条臂
    ERROR 串命中 0 次的原因（以它命名的锁背的是正常路径）。
    """
    d = tmp_path / "gf"
    d.mkdir()
    _git(d, "init", "-q", ".")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    (d / "README.md").write_text("greenfield\n", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "base")
    return d, _git(d, "rev-parse", "HEAD").stdout.strip()


def test_r1_failclosed_arm_is_actually_reached(tmp_path, caplog):
    """★R1 锁①★ greenfield 夹具必须真正进入 fail-closed 臂（断该臂**独有**的 ERROR 串）。

    HIGH 的实证：26 条老锁里这条臂的独有 ERROR 串"接地证据集【全空】"命中 **0** 次，
    而正常路径 WARNING 命中 13 次 —— 以那道闸命名的锁背的全是正常路径。
    本条把"哪条臂"这一维焊进断言：断 `_no_verified_evidence` degrade 键 + 独有 ERROR 串，
    两者都只在这条臂上出现。
    """
    import logging

    from swarm.brain.nodes.planning_core import _strip_ungrounded_manifest_coords
    from swarm.infra.degrade import degrade_counts, reset_degrade_counts

    d, base_sha = _mk_greenfield_repo(tmp_path)
    (d / "mod").mkdir()
    (d / "mod" / "pom.xml").write_text(GREENFIELD_MOD_POM, encoding="utf-8")  # 生产序

    reset_degrade_counts()
    try:
        with caplog.at_level(logging.INFO, logger="swarm.brain.nodes.planning_core"):
            _strip_ungrounded_manifest_coords(
                GREENFIELD_MOD_DIFF, str(d), "st-1", verified_files=set(),
                stub_written=["mod/pom.xml"], base_ref=base_sha)
        _keys = degrade_counts()
        assert _keys.get("brain.stub_grounding.no_verified_evidence", 0) >= 1, (
            "★必须真走 fail-closed 臂★ 该臂独有的 degrade 键未记账 ⇒ 夹具没到那条臂"
            f"（老锁全是这个毛病）。实得键={_keys}"
        )
    finally:
        reset_degrade_counts()
    _txt = caplog.text
    assert "接地证据集【全空】" in _txt, (
        f"该臂独有的 ERROR 串必须出现（这是'哪条臂'的唯一机读判据）。实得日志={_txt[-800:]}"
    )
    assert "查无实据 → 已剥离该行" not in _txt, (
        "★区分力★ 这是正常路径的 WARNING；它出现说明夹具又滑回正常路径了"
    )


def test_r1_zero_base_evidence_manifest_is_dropped_whole_not_gutted(tmp_path):
    """★R1 锁②★ 零 base 证据的清单【整份不采纳】，绝不产出剥空 version 的残骸。

    MED 实证（已复现）：剥离式治法把 `<parent><version>3.2.0` 与模块自身 `<version>1.0.0`
    一起剥掉 —— 两者都"查无实据"，因为 greenfield 下这文件在 base 里根本不存在 ⇒ 盘上
    剩一个有 `<parent>` 却无 `<version>` 的 pom＝Maven 解析不了＝D3 铁律点名的 reactor
    解析期崩。复核首选处方"剥离前把本文件 base 版坐标纳入 _known"在此不适用：
    `_base_text_of` 返 None，没有 base 坐标可纳入。故按用户拍板方案②整份不采纳。

    ★diff 侧与盘侧必须同时到位★ 只断一侧会放过"diff 与盘不一致"的第三种状态
    （merged_diff 说没这文件、本地树里却有，而 L2 按本地树构建）——那比现状更坏。
    """
    from swarm.brain.nodes.planning_core import _strip_ungrounded_manifest_coords

    d, base_sha = _mk_greenfield_repo(tmp_path)
    (d / "mod").mkdir()
    _mp = d / "mod" / "pom.xml"
    _mp.write_text(GREENFIELD_MOD_POM, encoding="utf-8")

    out = _strip_ungrounded_manifest_coords(
        GREENFIELD_MOD_DIFF, str(d), "st-1", verified_files=set(),
        stub_written=["mod/pom.xml"], base_ref=base_sha)

    # ① 绝不留"有 <parent> 却无 <version>"的残骸（这是缺陷的直接形状）
    _disk = _mp.read_text(encoding="utf-8") if _mp.is_file() else ""
    assert not ("<parent>" in _disk and "3.2.0" not in _disk), (
        f"★盘上是 Maven 解析不了的 pom★ 有 <parent> 却无 <version>：\n{_disk}"
    )
    assert not ("<parent>" in (out or "") and "3.2.0" not in (out or "")), (
        f"★diff 侧同形状残骸★\n{out}"
    )
    # ② 整份不采纳＝两侧都不再有它
    assert not _mp.is_file(), (
        "零证据清单必须从本地树删除——留着它 L2 就会按本地树构建一份未经接地的清单"
    )
    assert "mod/pom.xml" not in (out or ""), (
        f"diff 侧必须整段移除（含 ---/+++/@@ 全部 hunk），实得：\n{out}"
    )


def test_r1_diff_and_disk_stay_consistent_when_disk_delete_fails(tmp_path):
    """★R1 锁③★ 删盘失败必须机读留痕（独立 degrade 键），不得静默。

    删盘失败 ⇒ diff 侧已移除而本地树仍有该文件＝"diff 与盘不一致"，后果与
    `sync_disk_failed` 同类（L2 按本地树构建中毒）但成因不同，故必须**独立键**：
    共用一个键会让两种故障在账上分不开（血规 10④：缺席/降级必须机读可辨）。
    """
    from swarm.brain.nodes.planning_core import _strip_ungrounded_manifest_coords
    from swarm.infra.degrade import degrade_counts, reset_degrade_counts

    d, base_sha = _mk_greenfield_repo(tmp_path)
    (d / "mod").mkdir()
    (d / "mod" / "pom.xml").write_text(GREENFIELD_MOD_POM, encoding="utf-8")

    import os as _os_mod
    _real_remove = _os_mod.remove

    def _boom(path, *a, **k):
        if str(path).endswith("mod/pom.xml"):
            raise OSError("夹具注入：删盘失败（只读挂载/权限）")
        return _real_remove(path, *a, **k)

    reset_degrade_counts()
    try:
        with patch("os.remove", _boom):
            _strip_ungrounded_manifest_coords(
                GREENFIELD_MOD_DIFF, str(d), "st-1", verified_files=set(),
                stub_written=["mod/pom.xml"], base_ref=base_sha)
        _keys = degrade_counts()
        assert _keys.get("brain.stub_grounding.drop_manifest_failed", 0) >= 1, (
            "删盘失败必须记【独立】degrade 键——与 sync_disk_failed 共用会让两种故障"
            f"在账上分不开。实得键={_keys}"
        )
        assert "brain.stub_grounding.sync_disk_failed" not in _keys, (
            "★区分力★ 不得复用 sync_disk_failed 键（那是逐行剥离落盘失败的账）"
        )
    finally:
        reset_degrade_counts()


# ── R1 二轮：同型缺陷在【正常路径】上也存在（我第一版只接 fail-closed 臂＝半落地）──

def test_r1_normal_path_also_drops_zero_evidence_manifest_whole(tmp_path):
    """★R1 锁④★ `_known` **非空**（正常路径）时，零 base 证据清单同样必须整份不采纳。

    ★这条是自查逮到的半落地★ 我第一版只把"整份不采纳"接在 fail-closed 臂上，
    探针实证正常路径**一字不差地**复现同一残骸：base 有干净根 pom（4.8.3）⇒ `_known` 非空
    ⇒ 走正常路径 ⇒ 桩新建 `mod/pom.xml` 的 `<parent><version>3.2.0` 与自身 `<version>1.0.0`
    双双被逐行剥离 ⇒ 盘上剩有 `<parent>` 却无 `<version>` 的 Maven 解析不了的 pom。

    缺陷根在"**零 base 证据的清单逐行剥离没有安全网**"——与走哪条臂无关，故判据必须
    两臂共用。「修复必须真到得了生产」「治法只落一半」是本项目反复出现的族。

    夹具与 `test_r1_zero_base_evidence_*` 的唯一差别＝base 里**有**一份干净根 pom
    （它就是把执行推到正常路径的那一个字）。
    """
    from swarm.brain.nodes.planning_core import _strip_ungrounded_manifest_coords

    d, base_sha = _mk_repo(tmp_path)      # ★base 有根 pom(4.8.3) ⇒ _known 非空 ⇒ 正常路径
    (d / "mod").mkdir()
    _mp = d / "mod" / "pom.xml"
    _mp.write_text(GREENFIELD_MOD_POM, encoding="utf-8")

    out = _strip_ungrounded_manifest_coords(
        GREENFIELD_MOD_DIFF, str(d), "st-1", verified_files=set(),
        stub_written=["mod/pom.xml"], base_ref=base_sha)

    _disk = _mp.read_text(encoding="utf-8") if _mp.is_file() else ""
    assert not ("<parent>" in _disk and "3.2.0" not in _disk), (
        f"★正常路径同型残骸★ 有 <parent> 却无 <version>：\n{_disk}"
    )
    assert not ("<parent>" in (out or "") and "3.2.0" not in (out or "")), (
        f"★正常路径 diff 侧同型残骸★\n{out}"
    )
    assert not _mp.is_file(), "零 base 证据清单在正常路径上同样必须从本地树删除"
    assert "mod/pom.xml" not in (out or ""), (
        f"正常路径 diff 侧也必须整段移除，实得：\n{out}"
    )


def test_r1_whole_drop_does_not_misfire_on_fully_grounded_new_manifest(tmp_path):
    """★R1 锁⑤（误杀边界）★ 新建清单若坐标**全部有实据**，绝不能被整份丢弃。

    "零 base 证据"单独**不足以**判丢——桩把 base 里真实存在的坐标照抄进新建清单是
    完全合法的产出，一行都不会被剥、文件毫无问题。此时整份丢弃＝冤杀，后果具体：
    下游 `upstream_artifacts` 声明依赖该清单 ⇒ 种子闸缺一个必 BLOCKED 永堵
    （R65C-T3 就是为这个才让桩对声明项让路）。
    故判据是"零 base 证据 **∧ 至少一行会被剥**"，缺后半段就是过宽的闸——
    而"过宽的闸使用者会绕开"是本文件已有反向锁盯着的既定教训。
    """
    from swarm.brain.nodes.planning_core import _strip_ungrounded_manifest_coords

    d, _ = _mk_repo(tmp_path)
    # base 根 pom 补一个 <parent><version>3.2.0，让它成为**有实据**的坐标
    (d / "pom.xml").write_text(
        BASE_POM.replace(
            "<modelVersion>4.0.0</modelVersion>",
            "<modelVersion>4.0.0</modelVersion>\n"
            "  <parent><version>3.2.0</version></parent>"),
        encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "base+parent")
    base_sha = _git(d, "rev-parse", "HEAD").stdout.strip()

    (d / "mod").mkdir()
    _mp = d / "mod" / "pom.xml"
    # 新建清单：坐标全部照抄 base 真实存在的（3.2.0 / 4.8.3）+ 一个 ${} 引用
    _good = ("<project>\n  <parent><version>3.2.0</version></parent>\n"
             "  <version>${project.version}</version>\n"
             "  <dependency><version>4.8.3</version></dependency>\n</project>\n")
    _mp.write_text(_good, encoding="utf-8")
    _diff = ("diff --git a/mod/pom.xml b/mod/pom.xml\n--- /dev/null\n"
             "+++ b/mod/pom.xml\n@@ -0,0 +1,5 @@\n"
             + "".join(f"+{ln}\n" for ln in _good.rstrip("\n").splitlines()))

    out = _strip_ungrounded_manifest_coords(
        _diff, str(d), "st-1", verified_files=set(),
        stub_written=["mod/pom.xml"], base_ref=base_sha)

    assert _mp.is_file(), (
        "★误杀★ 坐标全部有实据的新建清单被整份删掉了——下游种子闸会因缺产物永久 BLOCKED"
    )
    assert "mod/pom.xml" in (out or ""), f"★误杀★ diff 侧整段被移除，实得：\n{out}"
    assert {"3.2.0", "4.8.3"} <= _added_versions(out), (
        f"有实据的坐标必须原样保留，实得新增行版本={_added_versions(out)}"
    )
    assert "${project.version}" in (out or ""), "${} 引用任何档位都不该动"


def test_r1_nothing_to_drop_returns_diff_verbatim(tmp_path):
    """★R1 锁⑥★ 无可丢弃时 diff 必须**逐字**原样返回，绝不因空操作被悄悄改写。

    `_drop_whole_manifests` 内部用 `split_diff_by_file` 拆段再拼接，而那个函数的既有契约
    是**丢掉"提取不到文件的段"**（前言/噪声段）——无损重组不是它的承诺。已实测：
    带前言的 diff 拆完再拼，前言两行凭空消失。
    所以"没有可丢的"必须走**早返原文**，不能让它空跑一趟拆分。

    为什么这条值得单独锁：它是纯防御性早返，删掉它**不会打红任何既有锁**（突变 MUT8
    实测 rc=0、零红）——"冗余防御=互相兜底=两条都不可证伪"正是本项目点名的坑，
    故给这条防御配一条能证伪它的锁。
    """
    from swarm.brain.nodes.planning_core import _strip_ungrounded_manifest_coords

    d, base_sha = _mk_repo(tmp_path)
    # 桩只改源码、完全不碰构建清单 ⇒ 没有任何零证据清单可丢（rels 空）
    # 且刻意带一个**前言段**（提取不到文件的行），它是"是否被悄悄改写"的探针
    _preamble = "上游拼接留下的前言行（无文件头）\n"
    diff = (_preamble
            + "diff --git a/mod/src/A.java b/mod/src/A.java\n"
            "--- /dev/null\n+++ b/mod/src/A.java\n@@ -0,0 +1,1 @@\n"
            "+class A {}\n")

    out = _strip_ungrounded_manifest_coords(
        diff, str(d), "st-1", verified_files=set(),
        stub_written=["mod/src/A.java"], base_ref=base_sha)

    assert out == diff, (
        "★无可丢弃时必须逐字原样返回★ 实际被改写了——空操作走了拆分再拼接，"
        f"`split_diff_by_file` 把前言段吞掉了。\n期望:\n{diff!r}\n实得:\n{out!r}"
    )
    assert _preamble in (out or ""), "前言段必须仍在（这是被悄悄改写的直接证据）"


# ─────────── R1 三轮：modify 型【替换对】—— 判据要求零 base 证据 ⇒ 结构性抓不到 ───────────

_PAIR_PARENT_VER = "    <version>3.2.0</version>"
_PAIR_BASE_POM = f"""<project>
  <parent>
    <groupId>com.acme</groupId>
    <artifactId>acme-parent</artifactId>
{_PAIR_PARENT_VER}
  </parent>
  <artifactId>mod</artifactId>
  <version>1.0.0</version>
  <dependencies>
    <dependency>
      <groupId>org.x</groupId>
      <artifactId>y</artifactId>
      <version>2.5.0</version>
    </dependency>
  </dependencies>
</project>
"""


def _mk_repo_with(tmp_path: Path, name: str, pom_text: str) -> tuple[Path, str]:
    d = tmp_path / name
    d.mkdir()
    _git(d, "init", "-q", ".")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    (d / "pom.xml").write_text(pom_text, encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "base")
    return d, _git(d, "rev-parse", "HEAD").stdout.strip()


def _parent_block_legal(pom: str) -> bool:
    """`<parent>` 块里必须有 `<version>`——Maven 硬要求，缺了 reactor 解析期就崩。"""
    if "<parent>" not in pom:
        return True
    return "<version>" in pom.split("</parent>", 1)[0]


def _parent_version(pom: str) -> str | None:
    """`<parent>` 块里的 version 值；没有则 None。

    ★为什么必须按【块】取而不是 `"2.5.0" in pom`★ 本夹具里 2.5.0 同时是既有依赖的版本
    ⇒ 子串断言会被**依赖那处**满足，与 parent 那处无关。实测代价：反向锁②原本用子串断言，
    突变（判据忽略 known ⇒ 冤杀合法坐标）下**仍绿＝没牙**，正是"断言区分力"那条坑。
    """
    if "<parent>" not in pom:
        return None
    _head = pom.split("</parent>", 1)[0]
    _m = re.search(r"<version>\s*([^<>\s]+)\s*</version>", _head)
    return _m.group(1) if _m else None


def _run_gate_two_faces(tmp_path: Path, stub_pom: str) -> tuple[str, str, str]:
    """驱动真闸，返回 (本地验证树盘上 pom, 交付面落地 pom, 闸返回的 diff)。

    ★为什么必须两棵树★ 生产里"本地验证树"（L2 按它构建）与"交付面"（merged_diff 落到
    别处）是**两个独立面**，本批治的缺陷正是二者可以各自中毒。只断一面会漏掉另一面
    ——R1 一轮就是只治了一条臂。树B 刻意从 base 干净副本重建，模拟交付面。
    """
    from swarm.brain.merge_engine import merge_diffs
    from swarm.brain.nodes.planning_core import (
        _git_diff_for_paths,
        _strip_ungrounded_manifest_coords,
    )
    from swarm.project.diff_apply import apply_git_diff

    a, sha_a = _mk_repo_with(tmp_path, "faceA", _PAIR_BASE_POM)
    (a / "pom.xml").write_text(stub_pom, encoding="utf-8")      # 生产序：桩先写盘
    _diff = _git_diff_for_paths(str(a), ["pom.xml"], base_ref=sha_a)
    assert _diff.strip(), "夹具前提：git diff 必须产出内容，否则本锁空过"
    out = _strip_ungrounded_manifest_coords(
        _diff, str(a), "st-1", verified_files=set(),
        stub_written=["pom.xml"], base_ref=sha_a) or ""
    disk_a = (a / "pom.xml").read_text(encoding="utf-8")

    b, _ = _mk_repo_with(tmp_path, "faceB", _PAIR_BASE_POM)
    if out.strip():
        _mr = merge_diffs([("st-1", out)])
        _m = (_mr if isinstance(_mr, str)
              else _mr.get("merged_diff") if isinstance(_mr, dict)
              else getattr(_mr, "merged_diff", "")) or ""
        if _m.strip():
            apply_git_diff(str(b), _m)
    return disk_a, (b / "pom.xml").read_text(encoding="utf-8"), out


def test_r1_modify_stub_version_replacement_keeps_base_coord_on_both_faces(tmp_path):
    """★R1 三轮锁①★ modify 型桩替换版本行 ⇒ 两面都必须留下【合法】pom，且保住 base 坐标。

    ★这条缺陷为什么逃过了 R1 前两轮★ 治法 `_drop_whole_manifests` 的判据要求
    **零 base 证据**（＝桩新建的文件），而 modify 型改的文件 base 里**有**它 ⇒ 判据
    结构性抓不到，逐行剥离原样跑。实测两面同时中毒：
    - 盘侧：`_sync_disk` 精确删掉桩写的那行 ⇒ `<parent>` 无 `<version>`；
    - 交付面：只丢 `+` 行而 `-<version>3.2.0` **仍在** ⇒ 陈旧 hunk 计数被 merge 的
      `_recount_hunk_header` 重算成 well-formed ⇒ **apply 成功** ⇒ 落地后 base 那行被删。
    即 R1 治的"Maven 解析不了的残骸"在 modify 型上原样存在。

    治法＝`_strip_ungrounded_lines` 认【紧邻的 `-`/`+` 替换对】：整对丢掉（diff 侧 ⇒ base
    那行原地留住）、盘侧**还原成 `-` 行内容**而非删除。
    """
    stub = _PAIR_BASE_POM.replace(_PAIR_PARENT_VER, "    <version>3.3.0</version>")
    disk_a, disk_b, out = _run_gate_two_faces(tmp_path, stub)

    assert _parent_block_legal(disk_a), (
        f"★本地验证树上是 Maven 解析不了的 pom★ <parent> 无 <version>：\n{disk_a}"
    )
    assert _parent_block_legal(disk_b), (
        f"★交付面落地后是 Maven 解析不了的 pom★（只断盘侧会漏掉这一面）：\n{disk_b}"
        f"\n--- 闸返回的 diff ---\n{out}"
    )
    # 臆造坐标必须没了，base 坐标必须还在——两个方向都断，缺一个就可能被"整份删掉"蒙过去
    assert "3.3.0" not in disk_a and "3.3.0" not in disk_b, (
        f"臆造版本仍在场（接地闸本职失效）\nA:\n{disk_a}\nB:\n{disk_b}"
    )
    # 按 parent 块取值（子串断言会被文件里别处的同名版本满足 ⇒ 零区分力，见 `_parent_version`）
    assert _parent_version(disk_a) == "3.2.0", (
        f"base 的 parent 版本被删/改了（就是本缺陷的形状）：实得="
        f"{_parent_version(disk_a)}\n{disk_a}"
    )
    assert _parent_version(disk_b) == "3.2.0", (
        f"交付面丢了 base 的 parent 版本：实得={_parent_version(disk_b)}\n{disk_b}"
        f"\n--- 闸返回 ---\n{out}"
    )


def test_r1_pair_repair_does_not_misfire_on_legit_base_coord(tmp_path):
    """★R1 三轮锁②·反向★ 桩换成的版本【base 里真实存在】时不得剥离（防冤杀）。

    与锁①配对：只有锁①时，把判据写成"凡 modify 型替换一律丢整对"也能全绿，
    而那会把合法的版本变更也抹掉。这条钉住"查得到实据就放行"这一维。
    """
    stub = _PAIR_BASE_POM.replace(_PAIR_PARENT_VER, "    <version>2.5.0</version>")
    disk_a, disk_b, out = _run_gate_two_faces(tmp_path, stub)

    assert _parent_block_legal(disk_a) and _parent_block_legal(disk_b), (
        f"两面都必须合法\nA:\n{disk_a}\nB:\n{disk_b}"
    )
    # ★必须按 parent 块取值★ 子串 `"2.5.0" in disk_a` 会被**既有依赖那处**满足 ⇒ 零区分力
    #（实测：突变"判据忽略 known"下那种写法仍绿）。
    assert _parent_version(disk_a) == "2.5.0", (
        "★冤杀★ 2.5.0 在 base 里真实存在（既有依赖用的就是它）⇒ 查有实据 ⇒ 不得剥离，"
        f"parent 版本应仍是 2.5.0。实得 parent 版本={_parent_version(disk_a)}\n"
        f"盘上：\n{disk_a}\n--- 闸返回 ---\n{out}"
    )
    assert _parent_version(disk_b) == "2.5.0", (
        f"交付面 parent 版本被改动：实得={_parent_version(disk_b)}\n{disk_b}"
    )


def test_r1_pure_addition_still_deletes_line_not_restores(tmp_path):
    """★R1 三轮锁③★ 纯新增行（无配对 `-` 行）必须仍走【删档】，不得被替换档带跑。

    区分力所在：`_strip_ungrounded_lines` 有删/替换两档，若把两档写成一档（一律还原），
    纯新增的臆造依赖就会被"还原"成某一行而留在盘上 ⇒ 接地闸本职失效。
    """
    stub = _PAIR_BASE_POM.replace(
        "  </dependencies>",
        "    <dependency>\n      <groupId>org.z</groupId>\n"
        "      <artifactId>w</artifactId>\n      <version>9.9.9</version>\n"
        "    </dependency>\n  </dependencies>")
    disk_a, disk_b, out = _run_gate_two_faces(tmp_path, stub)

    assert "9.9.9" not in disk_a, (
        f"★纯新增的臆造版本必须被删掉（删档）★ 实得盘上：\n{disk_a}"
    )
    assert _parent_block_legal(disk_a) and _parent_block_legal(disk_b), (
        f"两面都必须合法\nA:\n{disk_a}\nB:\n{disk_b}"
    )
    assert "3.2.0" in disk_a and "2.5.0" in disk_a, (
        f"既有合法坐标（parent 3.2.0 / 依赖 2.5.0）不得被牵连：\n{disk_a}"
    )


def test_r1_pair_criterion_is_adjacent_only_not_whole_hunk():
    """★R1 三轮锁④★ 配对判据只认【紧邻】前一行，不得放宽成"同 hunk 任意 `-<version>`"。

    放宽的危害：一个 hunk 里既删掉旧依赖（`-<version>1.1.1`）又在别处新增臆造版本
    （`+<version>9.9.9`）时，宽判据会把那条不相干的 `-` 行也丢掉＝改写桩的真实意图
    （那处删除是桩故意做的）。宁窄不宽：认不出配对就退回"只丢 `+` 行"的既有语义。

    直接驱动纯函数（无 IO），断的是判据本身而非某个夹具下的巧合。
    """
    from swarm.brain.nodes.planning_core import _strip_ungrounded_lines

    # `-1.1.1` 与 `+9.9.9` 之间隔着一个 context 行 ⇒ 不是替换对
    _diff = (
        "diff --git a/pom.xml b/pom.xml\n"
        "--- a/pom.xml\n+++ b/pom.xml\n"
        "@@ -1,5 +1,5 @@\n"
        "-      <version>1.1.1</version>\n"
        "       <artifactId>keep</artifactId>\n"
        "+      <version>9.9.9</version>\n"
    )
    _kept, _dropped, _disk = _strip_ungrounded_lines(_diff, set())

    assert "9.9.9" in str(_dropped), f"臆造版本必须被剥，实得 dropped={_dropped}"
    assert "-      <version>1.1.1</version>\n" in _kept, (
        "★判据过宽★ 不相干的删除行被一起丢了——那是桩故意删的，丢掉等于改写它的意图。"
        f"\n实得 kept:\n{_kept}"
    )
    # 无配对 ⇒ 盘侧动作必须是删档（None），不是替换档
    # （HIGH-2 整改后 `_disk` 按文件分桶：{diff 头路径 → {行 → 动作}}）
    assert _disk.get("pom.xml", {}).get("      <version>9.9.9</version>", "missing") is None, (
        f"无配对时盘侧必须走删档（None），实得 {_disk!r}"
    )

    # 紧邻则必须认成替换对（同一函数的另一半，防"两档只实现了一档"）
    _diff2 = (
        "diff --git a/pom.xml b/pom.xml\n"
        "--- a/pom.xml\n+++ b/pom.xml\n"
        "@@ -1,3 +1,3 @@\n"
        "-      <version>1.1.1</version>\n"
        "+      <version>9.9.9</version>\n"
    )
    _kept2, _dropped2, _disk2 = _strip_ungrounded_lines(_diff2, set())
    assert "<version>1.1.1</version>" not in _kept2, (
        f"★替换对必须整对丢掉★ `-` 行仍在 ⇒ 应用后 base 那行被删。实得:\n{_kept2}"
    )
    assert _disk2.get("pom.xml", {}).get("      <version>9.9.9</version>") == "      <version>1.1.1</version>", (
        f"盘侧必须还原成 base 那行（替换档），实得 {_disk2!r}"
    )


# ── 32 号文双复核 HIGH-2：盘侧动作表必须带【文件】这一维 ──────────────
#
# 原实现 `_disk` 以裸行文本为键 ⇒ 桩在两份清单里写【同一】臆造版本（同步升版本是桩的
# 典型形态）且动作档不同（modify 型=还原成各自的 base 行 / 纯新增=删除）时，dict 后写
# 覆盖先写：轻则 A 文件被还原成 B 的 base 版本（臆造坐标换个来源照样落地），重则「删除」
# 盖掉「还原」⇒ `<parent>` 无 `<version>` 的 Maven 残骸——正是本闸存在要防的那一类。

_HIGH2_MOD_BASE = (
    "<project>\n  <parent>\n    <groupId>com.acme</groupId>\n"
    "    <artifactId>mod-parent</artifactId>\n      <version>4.4.4</version>\n"
    "  </parent>\n  <artifactId>mod</artifactId>\n</project>\n"
)
_HIGH2_FAKE_LINE = "      <version>9.9.9</version>"   # 两份清单里【逐字相同】的桩行
_HIGH2_MOD_PARENT_VER = "      <version>4.4.4</version>"


def test_high2_disk_actions_are_bucketed_per_file():
    """★HIGH-2 锁①·直接驱动★ 同一臆造版本行出现在两份文件、动作档不同 ⇒ 两桶各存各的。

    手造跨文件 diff：mod/pom.xml 是替换对（还原档），pom.xml 是纯新增（删档）。
    旧扁平 dict 下后写覆盖先写，两档必丢一档；嵌套结构下两档并存。
    """
    from swarm.brain.nodes.planning_core import _strip_ungrounded_lines

    _diff = (
        "diff --git a/mod/pom.xml b/mod/pom.xml\n"
        "--- a/mod/pom.xml\n+++ b/mod/pom.xml\n"
        "@@ -1,3 +1,3 @@\n"
        "-      <version>4.4.4</version>\n"
        "+      <version>9.9.9</version>\n"
        "diff --git a/pom.xml b/pom.xml\n"
        "--- a/pom.xml\n+++ b/pom.xml\n"
        "@@ -1,2 +1,3 @@\n"
        "       <artifactId>keep</artifactId>\n"
        "+      <version>9.9.9</version>\n"
    )
    _kept, _dropped, _disk = _strip_ungrounded_lines(_diff, set())

    assert set(_disk) == {"mod/pom.xml", "pom.xml"}, (
        f"★撞键★ 两份文件的动作必须分桶各存，实得键={sorted(_disk)}"
    )
    assert _disk["mod/pom.xml"].get(_HIGH2_FAKE_LINE) == "      <version>4.4.4</version>", (
        f"mod/pom.xml 必须走还原档（还原成它【自己】的 base 行），实得 {_disk!r}"
    )
    assert _disk["pom.xml"].get(_HIGH2_FAKE_LINE, "missing") is None, (
        f"pom.xml 必须走删档（None），实得 {_disk!r}"
    )
    assert "9.9.9" not in _kept, f"diff 侧两份文件的臆造版本都必须剥掉：\n{_kept}"


def _mk_repo_two_manifests(tmp_path: Path, name: str) -> tuple[Path, str]:
    """真 git 仓 + 两份清单的 base commit（根 pom + mod/pom.xml）。返回 (路径, base_sha)。"""
    d = tmp_path / name
    (d / "mod").mkdir(parents=True)
    _git(d, "init", "-q", ".")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    (d / "pom.xml").write_text(_PAIR_BASE_POM, encoding="utf-8")
    (d / "mod" / "pom.xml").write_text(_HIGH2_MOD_BASE, encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "base")
    return d, _git(d, "rev-parse", "HEAD").stdout.strip()


def test_high2_same_fake_version_in_two_manifests_each_gets_its_own_tier(tmp_path):
    """★HIGH-2 锁②·端到端★ 桩把【同一个】臆造版本写进两份清单（动作档不同）⇒ 各按各档治。

    形态＝HIGH-2 点名的撞键现场：
    - mod/pom.xml：modify 型替换 parent 版本行 4.4.4 → 9.9.9（还原档）；
    - 根 pom.xml：纯新增一个带 9.9.9 的依赖（删档）。
    两条 `+      <version>9.9.9</version>` 逐字相同。旧扁平 dict 后写覆盖先写：
    - 「删档」盖「还原档」⇒ mod/pom.xml 的 9.9.9 行被**删掉** ⇒ `<parent>` 无
      `<version>` 的 Maven 解析不了的残骸（本闸存在要防的那一类）；
    - 反向「还原档」盖「删档」⇒ 根 pom 被注入 mod 的 base 版本 4.4.4（臆造坐标换个
      来源照样落地）。
    两个方向都断：mod 必须还原成 4.4.4，根 pom 必须删掉 9.9.9 且【不得】出现 4.4.4。
    """
    from swarm.brain.merge_engine import merge_diffs
    from swarm.brain.nodes.planning_core import (
        _git_diff_for_paths,
        _strip_ungrounded_manifest_coords,
    )
    from swarm.infra.degrade import degrade_counts, reset_degrade_counts
    from swarm.project.diff_apply import apply_git_diff

    a, sha = _mk_repo_two_manifests(tmp_path, "faceA")
    # 生产序：桩先写盘，再对盘取 diff
    _root_stub = _PAIR_BASE_POM.replace(
        "  </dependencies>",
        "    <dependency>\n      <groupId>org.fake</groupId>\n"
        "      <artifactId>ghost</artifactId>\n"
        f"{_HIGH2_FAKE_LINE}\n    </dependency>\n  </dependencies>")
    assert _root_stub != _PAIR_BASE_POM, "夹具前提：根 pom 必须真被桩改"
    (a / "pom.xml").write_text(_root_stub, encoding="utf-8")
    _mod_stub = _HIGH2_MOD_BASE.replace(_HIGH2_MOD_PARENT_VER, _HIGH2_FAKE_LINE)
    assert _mod_stub != _HIGH2_MOD_BASE, "夹具前提：mod/pom.xml 必须真被桩改"
    (a / "mod" / "pom.xml").write_text(_mod_stub, encoding="utf-8")

    _diff = _git_diff_for_paths(str(a), ["pom.xml", "mod/pom.xml"], base_ref=sha)
    assert _diff.count(_HIGH2_FAKE_LINE) == 2, (
        "夹具前提：两份清单的桩行必须同现在 diff 里（撞键前提），实得：\n" + _diff
    )
    reset_degrade_counts()
    try:
        out = _strip_ungrounded_manifest_coords(
            _diff, str(a), "st-1", verified_files=set(),
            stub_written=["pom.xml", "mod/pom.xml"], base_ref=sha) or ""

        disk_root = (a / "pom.xml").read_text(encoding="utf-8")
        disk_mod = (a / "mod" / "pom.xml").read_text(encoding="utf-8")

        # ⓪ 正常路径不得误报哑映射（`_matched_any` 只在真匹配后置位；删了它＝
        # 同步成功也哭狼，与「缺席必须机读可辨」同族的反向形态）
        assert "brain.stub_grounding.sync_disk_unmapped" not in degrade_counts(), (
            f"两份 stub 文件都匹配上了键，不得报哑映射。实得键={degrade_counts()}"
        )
    finally:
        reset_degrade_counts()

    # ① 撞键的重则方向：mod/pom.xml 不得剩「有 <parent> 却无 <version>」的残骸
    assert _parent_block_legal(disk_mod), (
        f"★「删档」盖掉了「还原档」★ mod/pom.xml 剩 Maven 解析不了的残骸：\n{disk_mod}"
    )
    # ② mod 必须还原成它【自己】的 base 版本（不是被删、不是别的文件的版本）
    assert _parent_version(disk_mod) == "4.4.4", (
        f"mod/pom.xml 必须走还原档回到 4.4.4，实得={_parent_version(disk_mod)}\n{disk_mod}"
    )
    # ③ 撞键的轻则方向：根 pom 不得被注入 mod 的 base 版本
    assert "4.4.4" not in disk_root, (
        f"★「还原档」盖掉了「删档」★ 根 pom 被注入别的文件的 base 版本：\n{disk_root}"
    )
    # ④ 臆造版本两面全灭；根的合法坐标不连坐
    assert "9.9.9" not in disk_root and "9.9.9" not in disk_mod, (
        f"臆造版本仍在盘上\n根:\n{disk_root}\nmod:\n{disk_mod}"
    )
    assert "3.2.0" in disk_root and "2.5.0" in disk_root, (
        f"根 pom 既有合法坐标不得被牵连：\n{disk_root}"
    )
    # ⑤ diff 侧与交付面同型断言（只断盘侧会漏掉交付面，R1 一轮的教训）
    assert "9.9.9" not in out, f"闸返回的 diff 仍带臆造版本：\n{out}"
    b, _ = _mk_repo_two_manifests(tmp_path, "faceB")
    if out.strip():
        _mr = merge_diffs([("st-1", out)])
        _m = (_mr if isinstance(_mr, str)
              else _mr.get("merged_diff") if isinstance(_mr, dict)
              else getattr(_mr, "merged_diff", "")) or ""
        if _m.strip():
            apply_git_diff(str(b), _m)
    del_root = (b / "pom.xml").read_text(encoding="utf-8")
    del_mod = (b / "mod" / "pom.xml").read_text(encoding="utf-8")
    assert _parent_block_legal(del_mod) and _parent_version(del_mod) == "4.4.4", (
        f"交付面 mod/pom.xml 必须合法且回到 4.4.4：\n{del_mod}\n--- 闸返回 ---\n{out}"
    )
    assert "9.9.9" not in del_root and "4.4.4" not in del_root, (
        f"交付面根 pom 中毒：\n{del_root}\n--- 闸返回 ---\n{out}"
    )


def test_high2_unmapped_sync_is_machine_readable(tmp_path):
    """★HIGH-2 锁③★ 有剥离动作却无任何 stub 文件匹配上键 ⇒ 哑映射必须 fail-loud。

    形态＝stub_written 清单与 diff 实际触及的文件不一致（清单漂移/归一口径漂移），
    落盘同步整体失效——正是 R2-③ 要防的形态复活。与 sync_disk_failed 同后果
    （残留留树、L2 按本地树构建中毒）但成因不同 ⇒ 必须独立键 + 盘上残留确实还在
    （断后果真发生了，不是只断键）。
    """
    from swarm.brain.nodes.planning_core import (
        _git_diff_for_paths,
        _strip_ungrounded_manifest_coords,
    )
    from swarm.infra.degrade import degrade_counts, reset_degrade_counts

    a, sha = _mk_repo_two_manifests(tmp_path, "faceA")
    _mod_stub = _HIGH2_MOD_BASE.replace(_HIGH2_MOD_PARENT_VER, _HIGH2_FAKE_LINE)
    (a / "mod" / "pom.xml").write_text(_mod_stub, encoding="utf-8")
    _diff = _git_diff_for_paths(str(a), ["mod/pom.xml"], base_ref=sha)
    assert _HIGH2_FAKE_LINE in _diff, "夹具前提：diff 必须含桩行"

    reset_degrade_counts()
    try:
        # stub_written 只报了根 pom（漂移），与 diff 实际触及的 mod/pom.xml 对不上
        _strip_ungrounded_manifest_coords(
            _diff, str(a), "st-1", verified_files=set(),
            stub_written=["pom.xml"], base_ref=sha)
        _keys = degrade_counts()
        assert _keys.get("brain.stub_grounding.sync_disk_unmapped", 0) >= 1, (
            "有剥离动作却无一 stub 文件匹配 ⇒ 必须记哑映射独立键（R2-③ 形态复活"
            f"会静悄悄）。实得键={_keys}"
        )
        assert "brain.stub_grounding.sync_disk_failed" not in _keys, (
            "★区分力★ 不得复用 sync_disk_failed 键（那是写盘 OSError 的账）"
        )
    finally:
        reset_degrade_counts()
    # 后果断言：同步整体失效 ⇒ 盘上的臆造坐标确实还在（这正是必须 loud 的原因）
    disk_mod = (a / "mod" / "pom.xml").read_text(encoding="utf-8")
    assert "9.9.9" in disk_mod, (
        "★锁空过★ 哑映射形态下盘上应仍留臆造坐标（同步没发生），"
        "否则本锁断的'后果'根本没发生：\n" + disk_mod
    )


# ── 32 号文双复核 reviewer MED#2：成组 diff 的配对必须按位（FIFO），不得交叉 ──
#
# 探针实证（旧实现）：git 对连续多行修改产出「先全部 `-`、再全部 `+`」的按位替换
# 语义，而旧判据只取 `_kept[-1]`（队尾）⇒ 成组时映射交叉（8.8.8→2.2.2 / 9.9.9→1.1.1）
# ⇒ `_sync_disk` 把两处 base 版本互换写盘 ⇒ 依赖 A 顶 B 的版本＝臆造坐标经互换通道落地。
# 治法＝FIFO 配对队列 + 任何其他行冲刷窗口。交替形态队列恒只有一个元素 ⇒ 行为不变。

def test_med2_grouped_pairing_is_positional_not_crossed():
    """★MED#2 锁①·直接驱动★ 三个场景钉死配对窗口语义。

    (a) 成组 `- - + +` ⇒ 按位配对（8.8.8↔1.1.1 / 9.9.9↔2.2.2），两条 `-` 都不采纳；
    (b) 保留的 `+` 行（known 版本）冲刷窗口 ⇒ 后续被剥的 `+` 走删档，且
        `-1.1.1`/`+2.5.0` 这对合法替换原样保留（git 按位：known 那行自己吃掉了 `-` 位）；
    (c) 非 version 的 `-` 行冲刷窗口 ⇒ 删档，且 `-1.1.1` 仍在（它是桩的真实删除意图）。
    """
    from swarm.brain.nodes.planning_core import _strip_ungrounded_lines

    # (a) 成组：旧实现交叉（8.8.8→2.2.2），按位必须 8.8.8→1.1.1
    _g = (
        "diff --git a/pom.xml b/pom.xml\n--- a/pom.xml\n+++ b/pom.xml\n"
        "@@ -1,4 +1,4 @@\n"
        "-        <version>1.1.1</version>\n-        <version>2.2.2</version>\n"
        "+        <version>8.8.8</version>\n+        <version>9.9.9</version>\n"
    )
    _kept, _dropped, _disk = _strip_ungrounded_lines(_g, set())
    assert _disk.get("pom.xml", {}).get("        <version>8.8.8</version>") == "        <version>1.1.1</version>", (
        f"★按位配对失败★ 第 1 个 `+` 必须配第 1 个 `-`（1.1.1），实得 {_disk!r}"
    )
    assert _disk.get("pom.xml", {}).get("        <version>9.9.9</version>") == "        <version>2.2.2</version>", (
        f"★按位配对失败★ 第 2 个 `+` 必须配第 2 个 `-`（2.2.2），实得 {_disk!r}"
    )
    assert "<version>1.1.1</version>" not in _kept and "<version>2.2.2</version>" not in _kept, (
        f"配掉的两条 `-` 行都不得留在 diff 里（base 那行原地留住）：\n{_kept}"
    )

    # (b) 保留的 `+` 行冲刷窗口（2.5.0 在 known ⇒ 保留）
    _b = (
        "diff --git a/pom.xml b/pom.xml\n--- a/pom.xml\n+++ b/pom.xml\n"
        "@@ -1,2 +1,3 @@\n"
        "-        <version>1.1.1</version>\n+        <version>2.5.0</version>\n"
        "+        <version>9.9.9</version>\n"
    )
    _kept_b, _, _disk_b = _strip_ungrounded_lines(_b, {"2.5.0"})
    assert _disk_b.get("pom.xml", {}).get("        <version>9.9.9</version>", "missing") is None, (
        "★窗口没冲刷★ 保留的 `+2.5.0` 在 git 按位语义下已吃掉 `-1.1.1` 那个位置，"
        f"后面的 `+9.9.9` 是纯新增 ⇒ 必须走删档，实得 {_disk_b!r}"
    )
    assert "-        <version>1.1.1</version>\n" in _kept_b and "+        <version>2.5.0</version>\n" in _kept_b, (
        f"合法替换对（1.1.1→2.5.0，known）必须原样留在 diff 里：\n{_kept_b}"
    )

    # (c) 非 version 的 `-` 行冲刷窗口
    _c = (
        "diff --git a/pom.xml b/pom.xml\n--- a/pom.xml\n+++ b/pom.xml\n"
        "@@ -1,3 +1,2 @@\n"
        "-        <version>1.1.1</version>\n-        <artifactId>old</artifactId>\n"
        "+        <version>9.9.9</version>\n"
    )
    _kept_c, _, _disk_c = _strip_ungrounded_lines(_c, set())
    assert _disk_c.get("pom.xml", {}).get("        <version>9.9.9</version>", "missing") is None, (
        "★窗口没冲刷★ 隔着非 version 的 `-` 行不得配对（宁窄不宽，那是桩的真实删除意图），"
        f"实得 {_disk_c!r}"
    )
    assert "-        <version>1.1.1</version>\n" in _kept_c, (
        f"未配对的 `-1.1.1` 是真实删除意图，必须留在 diff 里：\n{_kept_c}"
    )


_MED2_ADJ_BASE = (
    "<project>\n  <artifactId>mod</artifactId>\n  <dependencies>\n"
    "    <dependency>\n      <groupId>org.x</groupId>\n      <artifactId>y</artifactId>\n"
    "      <version>1.1.1</version>\n      <version>2.2.2</version>\n"
    "    </dependency>\n  </dependencies>\n</project>\n"
)


def test_med2_grouped_stub_restores_each_position_to_its_own_base_version(tmp_path):
    """★MED#2 锁②·端到端★ 桩把 base 里【物理相邻】的两个版本行成组换成臆造版本
    ⇒ 盘上两个位置必须各还原各的 base 版本，不得互换。

    夹具说明（如实）：两条 `<version>` 物理相邻在 Maven 里不是合法形态，但本闸防的是
    **桩写**的 pom——桩可以构造相邻版本行；且判据是纯文本行级，不依赖 XML 合法性。
    旧实现（队尾取配）下盘上位置 1 被还原成 2.2.2、位置 2 被还原成 1.1.1＝互换。
    """
    from swarm.brain.nodes.planning_core import (
        _git_diff_for_paths,
        _strip_ungrounded_manifest_coords,
    )

    a, sha = _mk_repo_with(tmp_path, "faceA", _MED2_ADJ_BASE)
    _stub = _MED2_ADJ_BASE.replace(
        "      <version>1.1.1</version>\n      <version>2.2.2</version>",
        "      <version>8.8.8</version>\n      <version>9.9.9</version>")
    assert _stub != _MED2_ADJ_BASE, "夹具前提：桩必须真改了相邻两行"
    (a / "pom.xml").write_text(_stub, encoding="utf-8")
    _diff = _git_diff_for_paths(str(a), ["pom.xml"], base_ref=sha)
    assert "-      <version>1.1.1</version>\n-      <version>2.2.2</version>\n" in _diff, (
        "夹具前提：git 必须产出成组形态（先全部 `-`）——否则本锁没测到成组分支：\n" + _diff
    )
    out = _strip_ungrounded_manifest_coords(
        _diff, str(a), "st-1", verified_files=set(),
        stub_written=["pom.xml"], base_ref=sha) or ""

    disk = (a / "pom.xml").read_text(encoding="utf-8")
    _lines = [ln for ln in disk.splitlines() if "<version>" in ln]
    assert _lines == ["      <version>1.1.1</version>", "      <version>2.2.2</version>"], (
        f"★互换落盘★ 两个位置必须各还原各的（1.1.1 在前 2.2.2 在后），实得 {_lines}\n{disk}"
    )
    assert "8.8.8" not in disk and "9.9.9" not in disk, f"臆造版本仍在盘上：\n{disk}"
    assert "8.8.8" not in out and "9.9.9" not in out, f"闸返回的 diff 仍带臆造版本：\n{out}"
