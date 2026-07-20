#!/usr/bin/env python3
"""#31-P1：子任务【必建文件】完整性闸——test-first 红测。

真根（治本 #31）：子任务【声明的交付物】(scope.create_files=必建) 没被逐项核验是否真产出。
实证 round46 st-38-1：声明产 3 个接口类（Google2FAController/SysLoginService/UserRealm…）
只产 1 个，缺的接口类【无本地引用】→ 本地 compile 不炸 → L1 全绿 → 假 DONE → 下游连坐。

设计（A/B/C 已审批）：
- A 声明来源：只用 scope.create_files（语义=必建，types.py:121）；★永不核验 writable★
  （语义=可改非义务，盲核验误杀合法未改）。
- B 核验点：L1 per-subtask，紧邻 empty_diff 闸作其超集加强。
- C 防误杀 + fail-open：遗漏判据 = create_file【既不在本轮 diff 新建集(new file mode/
  --- /dev/null)、又不在项目盘上】；白名单豁免（H1 模板/repaired/finisher 孤儿在盘）；
  探测不到/空 create_files → 不判死，只在确凿"声明必建 X 但 diff 无且盘上无"才 fail-closed。

纯函数红测（不用 live/cassette_replay）：missing_created_files + 接线（_deterministic_l1_gate）。
"""
from __future__ import annotations

from unittest.mock import patch

from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality
from swarm.worker.executor import WorkerExecutor


# ── 测试夹具 ──

def _new_file_diff(*rels: str) -> str:
    """构造【新建文件】unified diff（带 new file mode + --- /dev/null 标记）。"""
    parts = []
    for rel in rels:
        parts.append(
            f"diff --git a/{rel} b/{rel}\n"
            "new file mode 100644\n"
            "index 0000000..1111111\n"
            f"--- /dev/null\n"
            f"+++ b/{rel}\n"
            "@@ -0,0 +1,1 @@\n"
            "+package x;\n"
        )
    return "".join(parts)


def _modify_diff(rel: str) -> str:
    """构造【修改现有文件】unified diff（无新建标记）。"""
    return f"--- a/{rel}\n+++ b/{rel}\n@@ -1 +1 @@\n-old\n+new\n"


# ════════════════ 纯函数：missing_created_files ════════════════

def _mcf(create_files, diff, *, on_disk=(), exempt=None):
    """薄封装：on_disk 用集合语义构造 exists 回调。"""
    from swarm.worker.l1_pipeline import missing_created_files
    disk = set(on_disk)
    return missing_created_files(
        create_files, diff, exists=lambda p: p in disk, exempt=exempt)


def test_31_partial_creation_flags_missing():
    """#31 核心红测：声明产 A/B/C，只产 A（B/C 既不在 diff 也不在盘）→ 遗漏={B,C}。"""
    creates = ["com/x/A.java", "com/x/B.java", "com/x/C.java"]
    diff = _new_file_diff("com/x/A.java")
    missing = _mcf(creates, diff, on_disk={"com/x/A.java"})
    assert set(missing) == {"com/x/B.java", "com/x/C.java"}, missing


def test_all_created_no_missing():
    """全部声明必建文件都在 diff 新建集 → 无遗漏。"""
    creates = ["a.java", "b.java"]
    diff = _new_file_diff("a.java", "b.java")
    assert _mcf(creates, diff) == []


def test_diff_root_prefix_tolerated():
    """diff 路径带仓库根前缀（repo/src/...）仍与 scope 相对路径匹配（复用 _scope_match）。"""
    creates = ["src/main/java/x/A.java"]
    diff = _new_file_diff("repo/src/main/java/x/A.java")
    assert _mcf(creates, diff) == []


# ── 白名单豁免（不判遗漏） ──

def test_on_disk_sibling_or_finisher_exempt():
    """create_file 不在 diff 但【已在盘】（兄弟/收尾器孤儿已产/基线）→ 不判遗漏。"""
    creates = ["x/Orphan.java"]
    missing = _mcf(creates, "", on_disk={"x/Orphan.java"})
    assert missing == []


def test_h1_template_or_repaired_exempt():
    """create_file 在 exempt 集（H1 权威模板落盘 / 确定性修复触达）→ 不判遗漏。"""
    creates = ["mod/pom.xml"]
    missing = _mcf(creates, "", on_disk=set(), exempt={"mod/pom.xml"})
    assert missing == []


# ── fail-open 铁律 ──

def test_empty_create_files_fail_open():
    """空 create_files（无必建声明）→ 恒空遗漏（不判死）。"""
    assert _mcf([], _new_file_diff("a.java")) == []
    assert _mcf(None, "") == []


def test_no_exists_probe_fail_open():
    """无 exists 探测器（探测不到磁盘）→ 绝不判遗漏（fail-open）。"""
    from swarm.worker.l1_pipeline import missing_created_files
    # exists=None：探测不到盘 → 不判死
    assert missing_created_files(["a.java"], "", exists=None) == []


def test_exists_probe_raises_fail_open():
    """exists 探测器抛异常 → 该文件不判遗漏（fail-open，绝不因探测失败误杀）。"""
    from swarm.worker.l1_pipeline import missing_created_files

    def _boom(_p):
        raise OSError("sandbox unreachable")

    assert missing_created_files(["a.java"], "", exists=_boom) == []


def test_diff_parse_failure_fail_open():
    """diff 解析异常 → 空新建集 fail-open（但仍会走 on-disk/exempt；此处盘上有→放行）。"""
    creates = ["a.java"]
    # 传入畸形/None diff：解析新建集为空，但 a.java 在盘 → 不判遗漏
    assert _mcf(creates, None, on_disk={"a.java"}) == []


def test_modify_status_not_counted_as_created_but_on_disk_exempts():
    """create_file 在 diff 里是 modify 形态（无新建标记）→ 不计新建集；但 modify 必然在盘→豁免。"""
    creates = ["x/Pre.java"]
    diff = _modify_diff("x/Pre.java")
    # 未提供 on_disk：modify 说明文件本就存在，但纯函数无盘信息 → exists=set() 返 False。
    # 现实接线里 on-disk 探测会命中（modify 的前提是文件存在）→ 此处显式给 on_disk 复现现实。
    assert _mcf(creates, diff, on_disk={"x/Pre.java"}) == []


# ════════════════ 接线：_deterministic_l1_gate ════════════════

def _mk_executor(scope: FileScope, *, project_path="/tmp/swarm-p1-test") -> WorkerExecutor:
    st = SubTask(
        id="st-p1-1",
        description="新建 3 个接口类",
        difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=scope,
        intent="create",
    )
    return WorkerExecutor(subtask=st, project_path=project_path)


def test_wiring_partial_creation_fails_capability():
    """接线红测：非空 diff 只产 A、B/C 缺（盘上无）→ 确定性闸判 False，
    reason=declared_create_files_missing，归因自身 capability（非上游 BLOCKED）。"""
    scope = FileScope(create_files=["x/A.java", "x/B.java", "x/C.java"])
    ex = _mk_executor(scope)
    diff = _new_file_diff("x/A.java")
    # on-disk 探测确凿返回"不在盘"(__N__)——B/C 既不在 diff 新建集也不在盘 → 判死
    with patch.object(ex, "_get_git_diff", return_value=diff), \
            patch("swarm.worker.l1_pipeline._run_check_split",
                  return_value=(1, "__N__", "")):
        det_ok, details = ex._deterministic_l1_gate()
    assert det_ok is False, details
    assert details.get("reason") == "declared_create_files_missing", details
    assert set(details.get("missing_create_files") or []) == {"x/B.java", "x/C.java"}, details
    # 归因：确定性 fail（capability），不是 BLOCKED（None）——不与上游连坐混
    assert details.get("not_run_kind") is None, details


def test_wiring_empty_diff_still_goes_empty_diff_gate():
    """与 empty_diff 协调：全空 diff 仍走 empty_diff 闸（reason=empty_diff_but_changes_expected），
    新闸不双重判死/不抢归因。"""
    scope = FileScope(create_files=["x/A.java", "x/B.java"])
    ex = _mk_executor(scope)
    with patch.object(ex, "_get_git_diff", return_value="(无变更)"):
        det_ok, details = ex._deterministic_l1_gate()
    assert det_ok is False, details
    assert details.get("reason") == "empty_diff_but_changes_expected", details


def test_wiring_all_created_on_disk_not_flagged():
    """接线：声明必建文件全部已在盘（on-disk 命中）→ 新闸不判 missing，
    放行进入后续 pipeline（本测只验不被新闸拦，patch pipeline 短路避免真构建）。"""
    scope = FileScope(create_files=["x/A.java"])
    ex = _mk_executor(scope)
    diff = _new_file_diff("x/A.java")
    # A 在 diff 新建集 → 不 missing；直接短路 run_l1_pipeline 以免真构建
    with patch.object(ex, "_get_git_diff", return_value=diff), \
            patch("swarm.worker.l1_pipeline.run_l1_pipeline",
                  return_value=(True, {"deterministic_gate": "pass"})):
        det_ok, details = ex._deterministic_l1_gate()
    # 未被新闸拦（reason 不是 declared_create_files_missing）
    assert details.get("reason") != "declared_create_files_missing", details


# ════════════════ 对抗复核整改 F1a：再拆窄化 create_files（治误杀） ════════════════

class _FakeLLM:
    def __init__(self, content: str):
        self._content = content

    async def ainvoke(self, _messages):
        class _R:
            pass
        r = _R()
        r.content = self._content
        return r


def _mk_parent(create, writable=None, readable=None):
    return SubTask(
        id="st-9",
        description="大子任务：新建 A/B/C 三个类",
        difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(create_files=create, writable=writable or [], readable=readable or []),
        est_context_tokens=300_000,
    )


async def test_f1a_resplit_narrows_create_files_no_false_kill():
    """F1a 治根因红测：父 create_files=[A,B,C]，LLM 分派 sub1=[A]/sub2=[B]，C 无主。
    → sub1.create=[A]、sub2.create=[B,C]（B 认领+C 无主归末子块）；各子块只产自己片，
    #31 完整性闸【不】误杀（旧行为：子块全量继承 [A,B,C]，只产 A → 冤杀 B/C）。"""
    import json

    from swarm.brain import planning_nodes as pn

    parent = _mk_parent(["x/A.java", "x/B.java", "x/C.java"], readable=["x/R.java"])
    fake = _FakeLLM(json.dumps({"subtasks": [
        {"description": "建 A", "writable_files": [], "create_files": ["x/A.java"],
         "readable_files": ["x/R.java"], "est_context_tokens": 100000},
        {"description": "建 B", "writable_files": [], "create_files": ["x/B.java"],
         "readable_files": [], "est_context_tokens": 100000},
    ]}))
    with patch.object(pn, "_get_brain_llm", return_value=fake):
        kids = await pn._resplit_subtask(parent, {}, 150_000)

    assert len(kids) == 2, kids
    assert kids[0].scope.create_files == ["x/A.java"], kids[0].scope.create_files
    # C 无主 → 归串行末子块（sub2）；B 由 sub2 认领
    assert set(kids[1].scope.create_files) == {"x/B.java", "x/C.java"}, kids[1].scope.create_files
    # 每个必建文件恰好归一个子块（无重叠）
    assert kids[0].scope.create_files[0] not in kids[1].scope.create_files
    # 完整性闸不误杀：sub1 只产 A（自己那片）→ missing=[]
    diff1 = _new_file_diff("x/A.java")
    assert _mcf(kids[0].scope.create_files, diff1) == []


async def test_f1a_unclaimed_all_go_to_last_child():
    """F1a：LLM 完全没给 create_files 分派 → 全部无主 → 归串行末子块，earlier 子块不被
    charge（不会被完整性闸问及）→ 杜绝一文件挂多子块 + 无主文件蒸发。"""
    import json

    from swarm.brain import planning_nodes as pn

    parent = _mk_parent(["x/A.java", "x/B.java"], writable=["x/W.java"])
    fake = _FakeLLM(json.dumps({"subtasks": [
        {"description": "改 W part1", "writable_files": ["x/W.java"], "est_context_tokens": 100000},
        {"description": "改 W part2", "writable_files": ["x/W.java"], "est_context_tokens": 100000},
    ]}))
    with patch.object(pn, "_get_brain_llm", return_value=fake):
        kids = await pn._resplit_subtask(parent, {}, 150_000)

    assert kids[0].scope.create_files == [], kids[0].scope.create_files
    assert set(kids[-1].scope.create_files) == {"x/A.java", "x/B.java"}, kids[-1].scope.create_files


# ════════════════ 对抗复核整改 F1b：探测失败/脏 sync 不误杀 ════════════════

def test_f1b_dirty_sync_skips_gate():
    """F1b：本轮 sync 不干净（有 error/skip/oversize）→ on-disk 视图不可信 → 完整性闸
    fail-open 跳过（不判 declared_create_files_missing），details 盖 create_files_check_skipped。"""
    scope = FileScope(create_files=["x/A.java", "x/B.java"])
    ex = _mk_executor(scope)
    ex._sync_error_rels = ["x/somefile.java"]   # 脏 sync
    diff = _new_file_diff("x/A.java")            # 只产 A，B 缺
    with patch.object(ex, "_get_git_diff", return_value=diff), \
            patch("swarm.worker.l1_pipeline.run_l1_pipeline",
                  return_value=(True, {"deterministic_gate": "pass"})):
        _det_ok, details = ex._deterministic_l1_gate()
    # 未被完整性闸判死（脏 sync 下探测不可信）
    assert details.get("reason") != "declared_create_files_missing", details
    assert details.get("create_files_check_skipped") == "dirty_sync", details


def test_f1b_probe_inconclusive_fail_open():
    """F1b 加固：on-disk 探测无定论（infra/超时——无 __X__/__N__ 标记）→ 纯函数 fail-open
    不判该文件遗漏（绝不把探测失败当'确凿不在盘'冤杀合法已产文件）。"""
    from swarm.worker.l1_pipeline import missing_created_files

    def _inconclusive(_p):
        raise RuntimeError("on-disk 探测无定论(ec=124 timeout)")

    assert missing_created_files(["x/A.java"], "", exists=_inconclusive) == []


# ════════════════ 对抗复核整改 F2：闸失效可观测 ════════════════

def test_f2_gate_error_is_observable():
    """F2：完整性核验异常 → fail-open 但 details 盖 create_files_check_error（非静默）——
    '闸没跑成'字节级可辨于'闸跑了没缺'，防 import/逻辑回归让 #31 保护静默复活。"""
    scope = FileScope(create_files=["x/A.java"])
    ex = _mk_executor(scope)
    diff = _new_file_diff("x/other.java")   # 非空 diff（过 empty_diff）
    with patch.object(ex, "_get_git_diff", return_value=diff), \
            patch("swarm.worker.l1_pipeline.missing_created_files",
                  side_effect=RuntimeError("boom-regression")), \
            patch("swarm.worker.l1_pipeline.run_l1_pipeline",
                  return_value=(True, {"deterministic_gate": "pass"})):
        _det_ok, details = ex._deterministic_l1_gate()
    assert "create_files_check_error" in details, details
    assert "boom-regression" in details["create_files_check_error"], details
    # fail-open：未误判 missing
    assert details.get("reason") != "declared_create_files_missing", details


# ════════════════ 对抗复核整改 F3：判死信号带具体文件名 ════════════════

def test_f3_retry_digest_lists_missing_files():
    """F3：重试 fix prompt 的 digest 必须逐字列出缺失文件名（照 scope_violations 处理），
    否则模型只知'缺了必建文件'不知缺哪个 → 烧光 fix 轮/改错文件。"""
    scope = FileScope(create_files=["x/A.java"])
    ex = _mk_executor(scope)
    digest = ex._l1_failure_digest({
        "reason": "declared_create_files_missing",
        "missing_create_files": ["x/B.java", "x/C.java"],
        "note": "…",
    })
    assert "x/B.java" in digest and "x/C.java" in digest, digest
    assert "必建文件未产出" in digest, digest


def test_f3_det_fail_reason_lists_missing_files():
    """F3：机读账 _det_fail_reason 列出具体缺失文件（brain 侧装填 retry_guidance 的数据源）。"""
    from swarm.worker.l1_verdict import _det_fail_reason

    r = _det_fail_reason({
        "reason": "declared_create_files_missing",
        "missing_create_files": ["x/B.java", "x/C.java"],
    })
    assert "declared_create_files_missing" in r, r
    assert "x/B.java" in r and "x/C.java" in r, r
