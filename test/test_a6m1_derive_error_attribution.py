"""★32 号文 A6-M1★ 冒烟推导「代码炸了」与「如实推不出」必须机读可辨。

原缺陷：`smoke_derive` 五个 deriver 各自 `except Exception: return None, None`，异常臂与
"真推导不出"**逐字不可辨** ⇒ 归因恒说"推导不全"而真因是代码抛异常。最狠的落点在
`verify.py` 两个 migration 消费点：它们把 `migration_kind=None` 当**肯定事实**报
`no_migration_detected` 且刻意不进 degraded ⇒ 检测代码一崩，"库表迁移一次没验"被记成
"本工程无需迁移"，全链零信号。

本文件的锁按"证被接上了、不是证实现正确"组织，突变对照见文件尾注释。
"""

from __future__ import annotations

import ast
import asyncio
import os

import pytest

import swarm.brain.nodes.verify as verify_mod
import swarm.brain.smoke_derive as sd
from swarm.brain.smoke_derive import SmokeDerivation, derive_runtime_smoke
from swarm.memory.pattern_extractor import blocking_degraded_reasons

_PY_STACK = {"backend": "python"}

# 未闭合 table header —— tomllib 必抛 TOMLDecodeError（非"没有 console-script"）
_BROKEN_PYPROJECT = "[project\nname = broken\n"
_CLEAN_PYPROJECT = '[project]\nname = "clean"\nversion = "0.1.0"\n'


def _write(d, name: str, text: str) -> None:
    with open(os.path.join(str(d), name), "w", encoding="utf-8") as f:
        f.write(text)


# ═══════════════════ 1. 最强锁：零 monkeypatch，走真生产路径 ═══════════════════

def test_broken_pyproject_records_start_cmd_error_without_any_stub(tmp_path):
    """★本文件最强的一条：不用 monkeypatch★

    夹具是**真的畸形 pyproject.toml**：python 栈 + 无 manage.py（跳①）+ 无 FastAPI 实证
    （跳②）+ 无 flask import（跳③）⇒ 真生产路径走到④ tomllib 臂并**真的抛**
    TOMLDecodeError。原实现在这里 `except Exception: return None, None`，把解析崩塌伪装成
    "pyproject 里没有唯一 console-script"。

    为什么这条比 monkeypatch 强：monkeypatch 造的异常证明的是"如果抛了会记账"，本条证明
    "生产上真有一类工程会抛"——畸形/新语法 pyproject 在真实项目里就是会出现。
    """
    _write(tmp_path, "pyproject.toml", _BROKEN_PYPROJECT)
    dv = derive_runtime_smoke(_PY_STACK, str(tmp_path))

    assert dv.start_cmd is None, "fail-closed 不变：炸了仍不猜启动方式"
    assert "start_cmd" in dv.derive_errors, (
        "tomllib 解析崩塌被吞成'无 console-script' → 归因恒指向工程形态而真因是代码抛异常")
    assert "TOMLDecodeError" in dv.derive_errors["start_cmd"], (
        f"derive_errors 未带异常类型，排障拿不到线索: {dv.derive_errors}")
    # ★与 evidence 刻意分档★：evidence=推出来了凭据是这个；derive_errors=推导代码炸了。
    # 混进 evidence 消费侧就再也分不出二者（那正是原缺陷形态）。
    assert "start_cmd" not in dv.evidence, "异常不得伪装成 evidence（分档被合并即回归原缺陷）"


# ═══════════════════ 2. 四处拆掉的 swallow：各配「真抛 + 接住记账」一对 ═══════════════════

def test_swallow_gone_derive_start_cmd_raises_and_arm_records(tmp_path, monkeypatch):
    """swallow①②（`_derive_start_python` tomllib 臂 + `derive_start_cmd` 外层）。

    上半：`derive_start_cmd` **真抛**（证两层 swallow 都没了——外层若还在，畸形夹具会被
    吞成 `(None, None)` 而不抛）。下半：`derive_runtime_smoke` 的 start_cmd 臂接住并记账。
    """
    _write(tmp_path, "pyproject.toml", _BROKEN_PYPROJECT)
    with pytest.raises(Exception) as ei:      # noqa: PT011 — 只要求"抛"，类型由 tomllib 定
        sd.derive_start_cmd(_PY_STACK, str(tmp_path))
    assert "TOMLDecodeError" in type(ei.value).__name__

    assert "start_cmd" in derive_runtime_smoke(_PY_STACK, str(tmp_path)).derive_errors


def test_swallow_gone_derive_start_cmd_outer_layer_independently(tmp_path, monkeypatch):
    """单独把 `derive_start_cmd` **外层** swallow 钉住（与 tomllib 臂解耦）。

    上一条用畸形夹具同时穿两层；若某天①被改回吞异常，上一条会红但无法区分是哪一层。
    这里让 deriver 本体抛一个与 toml 无关的异常，只有外层 swallow 在场才会被吞。
    """
    def _boom(project_path, idx, framework):
        raise RuntimeError("deriver 本体崩")

    monkeypatch.setitem(sd._ENTRY_DERIVERS, "python", _boom)
    with pytest.raises(RuntimeError, match="deriver 本体崩"):
        sd.derive_start_cmd(_PY_STACK, str(tmp_path))

    dv = derive_runtime_smoke(_PY_STACK, str(tmp_path))
    assert "start_cmd" in dv.derive_errors and "RuntimeError" in dv.derive_errors["start_cmd"]


def test_swallow_gone_derive_prepare_cmd_raises_and_arm_records(tmp_path, monkeypatch):
    """swallow③（`derive_prepare_cmd`）。

    真危害：`spec_for_stack` 抛（栈表坏了）与"该栈不需要 prepare"原先返同一个值 ⇒ JVM 工程
    **不打包就去起 jar** ⇒ 冒烟报 `Unable to access jarfile`，归因指向业务代码。
    """
    def _boom(_key):
        raise RuntimeError("STACK_SPEC 查表崩")

    monkeypatch.setattr(sd, "spec_for_stack", _boom)
    with pytest.raises(RuntimeError, match="STACK_SPEC 查表崩"):
        sd.derive_prepare_cmd("java -jar target/app.jar", {"build": "maven"}, str(tmp_path))

    # 接住臂：需要 start_cmd 非 None 才进得去 prepare 推导（否则早返）。用 manage.py 夹具
    # 让 start_cmd 走真推导拿到值，再让查表抛。
    _write(tmp_path, "manage.py", "# django\n")
    dv = derive_runtime_smoke({"backend": "python", "build": "maven"}, str(tmp_path))
    assert dv.start_cmd, "夹具前提失效：start_cmd 没推出来 ⇒ prepare 臂根本没被调用（空进空出假绿）"
    assert "prepare_cmd" in dv.derive_errors, f"prepare 臂未记账: {dv.derive_errors}"


def test_swallow_gone_detect_migration_kind_raises_and_arm_records(tmp_path, monkeypatch):
    """swallow④（`detect_migration_kind`——危害最大的那个）。"""
    class _BoomIndex(sd._TreeIndex):
        @property
        def sql_dirs(self):                                # type: ignore[override]
            raise RuntimeError("索引访问崩")

        @sql_dirs.setter
        def sql_dirs(self, _v):                            # type: ignore[override]
            pass

    with pytest.raises(RuntimeError, match="索引访问崩"):
        sd.detect_migration_kind(str(tmp_path), _BoomIndex(), manifest_text="")

    monkeypatch.setattr(sd, "_build_index", lambda p: _BoomIndex())
    # _manifest_text_lower 也吃索引 → 它先炸会记 manifest_text；本锁只要求 migration 臂记上。
    monkeypatch.setattr(sd, "_manifest_text_lower", lambda p, idx: "")
    dv = derive_runtime_smoke(_PY_STACK, str(tmp_path))
    assert "migration_kind" in dv.derive_errors, (
        "检测崩塌未记账 ⇒ 下游 no_migration_detected 把'一次没验'记成'无需迁移'")
    assert dv.migration_kind is None


def test_tree_index_failure_is_recorded(tmp_path, monkeypatch):
    """finding 没点名但同族：索引建不出会让"整棵树没扫成"伪装成"这工程什么都没有"。"""
    def _boom(_p):
        raise OSError("walk 崩")

    monkeypatch.setattr(sd, "_build_index", _boom)
    dv = derive_runtime_smoke(_PY_STACK, str(tmp_path))
    assert "tree_index" in dv.derive_errors, "空索引上推导出的全 None 必须可辨于真的一无所有"


# ═══════════════════ 3. 区分力锁：如实推不出 ⇒ derive_errors 空 ═══════════════════

def test_clean_project_records_no_errors(tmp_path):
    """★缺这条，上面全部锁都可能被"恒非空"蒙过★

    合法 pyproject + 无 console-script + 无 migration 形态 = **如实推不出**。此时
    derive_errors 必须是空 dict，否则"炸了/没证据"又变回不可辨（只是反了个方向）。
    """
    _write(tmp_path, "pyproject.toml", _CLEAN_PYPROJECT)
    dv = derive_runtime_smoke(_PY_STACK, str(tmp_path))

    assert dv.start_cmd is None and dv.migration_kind is None, "夹具前提：本该什么都推不出"
    assert dv.derive_errors == {}, (
        f"如实推不出被记成推导异常 ⇒ 区分力归零，恒非空: {dv.derive_errors}")
    assert verify_mod.smoke_derive_error_fields(dv) == []
    assert verify_mod.smoke_skip_reason_for(dv) == "derivation_incomplete"
    assert verify_mod.migration_kind_undetermined(dv) is False


# ═══════════════════ 4. 误归因锁：missing ∩ errored，不是 any errored ═══════════════════

def test_skip_reason_blames_the_field_that_caused_the_skip():
    """★判据必须是 `missing ∩ errored`★

    start_cmd **如实缺**（无证据）+ migration_kind **炸了** ⇒ 导致 skip 的是 start_cmd，
    而它没炸 ⇒ reason 仍 `derivation_incomplete`。若判据写成"有没有任何字段炸"，这里会
    误报 derivation_error ⇒ 害人去查一段没病的代码（而真该查的是工程为什么没入口）。
    """
    dv = SmokeDerivation(start_cmd=None,
                         derive_errors={"migration_kind": "RuntimeError:x"})
    assert verify_mod.smoke_derivation_missing(dv) == ["start_cmd"]
    assert verify_mod.smoke_derive_error_fields(dv) == ["migration_kind"]
    assert verify_mod.smoke_skip_reason_for(dv) == "derivation_incomplete", (
        "非必需字段炸掉污染了 skip 归因（判据被写成 any-errored）")


def test_skip_reason_is_error_when_the_missing_field_itself_errored():
    """对照臂：导致 skip 的字段**自己**炸了 ⇒ derivation_error（这才该让人去查代码）。"""
    dv = SmokeDerivation(start_cmd=None,
                         derive_errors={"start_cmd": "TOMLDecodeError:x"})
    assert verify_mod.smoke_skip_reason_for(dv) == "derivation_error"


# ═══════════════════ 5. migration 两向 × 两个消费点（各测一遍） ═══════════════════

_MIG_ERR = SmokeDerivation(start_cmd="python3 app.py",
                           derive_errors={"migration_kind": "RuntimeError:索引崩"})
_MIG_CLEAN = SmokeDerivation(start_cmd="python3 app.py")


def test_migration_not_run_patch_undetermined_when_detection_errored():
    """消费点① `_migration_not_run_patch`：检测炸了 ⇒ 绝不宣称 no_migration_detected。"""
    patch = verify_mod._migration_not_run_patch(_MIG_ERR)
    assert patch["migration_verify_details"]["reason"] == "migration_kind_undetermined", (
        "检测崩塌被记成'本工程无需迁移' ⇒ 迁移面一次没验而全链零信号（A6-M1 核心危害）")


def test_migration_not_run_patch_keeps_no_migration_when_truly_absent():
    """反向：如实没检出仍是 `no_migration_detected`（常态，刻意不降级——过宽会天天报警）。"""
    patch = verify_mod._migration_not_run_patch(_MIG_CLEAN)
    assert patch["migration_verify_details"]["reason"] == "no_migration_detected"


def test_run_migration_phase_undetermined_when_detection_errored():
    """消费点② `_run_migration_phase`：**同一缺陷的两处落地，改一处＝半落地**。

    kind 为空时该函数在碰 manager/sandbox 之前就早返，故这里传 None 是安全的。
    """
    patch = asyncio.run(verify_mod._run_migration_phase(
        None, None, _MIG_ERR, {"build": "maven"}, "/tmp/x", None))
    assert patch["migration_verify_details"]["reason"] == "migration_kind_undetermined", (
        "只改了 _migration_not_run_patch 一处 ⇒ 执行通道那条路径照旧冤报'无需迁移'")


def test_run_migration_phase_keeps_no_migration_when_truly_absent():
    patch = asyncio.run(verify_mod._run_migration_phase(
        None, None, _MIG_CLEAN, {"build": "maven"}, "/tmp/x", None))
    assert patch["migration_verify_details"]["reason"] == "no_migration_detected"


# ═══════════════════ 6. 收口锁：经 `_verify_runtime_impl` 真走一遍 ═══════════════════

@pytest.fixture()
def _smoke_enabled(monkeypatch):
    """杀开关必须为开——.env 里 SWARM_RUNTIME_SMOKE_ENABLED=1 会被 conftest 加载，
    但显式删掉更稳（配置依赖假绿：本机 .env 恒在场，照不出没钉配置面的锁）。"""
    monkeypatch.delenv("SWARM_RUNTIME_SMOKE_ENABLED", raising=False)


def _run_impl(tmp_path, monkeypatch) -> dict:
    import swarm.brain.nodes as nodes_pkg
    monkeypatch.setattr(nodes_pkg, "_get_project_path", lambda pid: str(tmp_path))
    return asyncio.run(verify_mod._verify_runtime_impl({"project_id": "p1",
                                                       "project_stack": _PY_STACK}))


def test_impl_shell_appends_degraded_and_details(tmp_path, monkeypatch, _smoke_enabled):
    """★收口锁★ 薄壳统一记账：不管 core 从哪个出口返，degraded 与 details 都要落上。

    走真推导（畸形 pyproject）⇒ start_cmd 炸 ⇒ core 走 skip 出口 ⇒ 壳追加
    `smoke_derive_error:start_cmd` + 并 derive_errors 进 runtime_smoke_details。
    """
    _write(tmp_path, "pyproject.toml", _BROKEN_PYPROJECT)
    out = _run_impl(tmp_path, monkeypatch)

    _dg = out.get("degraded_reasons") or []
    assert any(str(r).startswith("smoke_derive_error:") for r in _dg), (
        f"壳没追加 smoke_derive_error ⇒ 推导崩塌在节点输出面零痕迹: {_dg}")
    assert "smoke_derive_error:start_cmd" in _dg, f"字段名没进 degraded 字符串: {_dg}"
    _details = out.get("runtime_smoke_details") or {}
    assert _details.get("derive_errors"), (
        f"derive_errors 没并进 runtime_smoke_details（gates/failure/shared/deliver 四个"
        f"既有消费者都读这个键）: {_details}")
    # ★三个信号刻意不重叠★：这条（smoke_derive_error）是**新增**字符串，下面那条
    # （skip_reason 分档）改的是**已有**字符串的取值。两条各自可证伪——同一事实过滤两遍
    # 会让任一处单独突变都仍绿（冗余防御=互相兜底=两条都不可证伪，本仓血泪）。
    assert "runtime_smoke_skipped:derivation_error" in _dg, (
        f"skip_reason 分档没落在 degraded 字符串上（那里才有消费者）: {_dg}")


def test_impl_shell_stays_silent_when_derivation_is_honest(tmp_path, monkeypatch, _smoke_enabled):
    """区分力对照：如实推不出 ⇒ 壳**不得**加 smoke_derive_error（否则恒报警=无信息）。"""
    _write(tmp_path, "pyproject.toml", _CLEAN_PYPROJECT)
    out = _run_impl(tmp_path, monkeypatch)

    _dg = out.get("degraded_reasons") or []
    assert not any(str(r).startswith("smoke_derive_error:") for r in _dg), (
        f"如实无证据也报推导异常 ⇒ 该信号恒非空、区分力归零: {_dg}")
    assert "runtime_smoke_skipped:derivation_incomplete" in _dg
    assert not (out.get("runtime_smoke_details") or {}).get("derive_errors")


def test_impl_shell_name_preserved_for_patch_object_callers():
    """`_verify_runtime_impl` 的**名字**是既有测试的挂接点
    （`test_b7_verification_coverage.py` 用 `patch.object` 按名打桩）。收口重构把业务体
    改名 `_verify_runtime_core` 时若顺手把壳也改名，那些测试会变 vacuous 绿而不是红。"""
    assert callable(getattr(verify_mod, "_verify_runtime_impl", None))
    assert asyncio.iscoroutinefunction(verify_mod._verify_runtime_impl)
    assert asyncio.iscoroutinefunction(verify_mod._verify_runtime_core)


# ═══════════════════ 7. 消费者存在锁（新账没有消费者＝没造） ═══════════════════

def test_smoke_derive_error_is_blocking_for_l6_learning():
    """★新账必须落在活轨上★

    `smoke_derive_error:*` 不在 `INFORMATIONAL_DEGRADED_PREFIXES` 白名单 ⇒ 被
    `blocking_degraded_reasons` 认作阻断 ⇒ `should_write_success` 拦 L6。
    没有这条，新键就只是又一个孤儿账（本仓 norms 层实测死 12 天跨 5+ 轮全零无信号）。
    """
    hit = ["smoke_derive_error:port"]
    assert blocking_degraded_reasons(hit) == hit, (
        "smoke_derive_error 被当信息性留痕 ⇒ 推导崩塌过的交付仍被学成成功模式")
    assert blocking_degraded_reasons(["smoke_derive_error:start_cmd/migration_kind"])


# ═══════════════════ 数字与清单同源：收口理由里的出口数机器复算 ═══════════════════

def test_core_exits_are_machine_counted():
    """`_verify_runtime_impl` docstring 声称 core 推导点之后有 8 个 return 出口——那个数字
    是收口（而非逐出口接线）的**理由**，所以它必须可复算。手抄必漂：本条改前的版本写
    "7 个"并附了 7 个行号，机器一数是 8 且**没有一个行号对得上**。
    """
    tree = ast.parse(open(verify_mod.__file__, encoding="utf-8").read())
    core = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_verify_runtime_core")
    box_line = max(n.lineno for n in ast.walk(core)
                   if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
                   and n.value.id == "_derive_box")
    nested = {m.lineno for n in ast.walk(core)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not core
              for m in ast.walk(n) if isinstance(m, ast.Return)}
    after = [n.lineno for n in ast.walk(core)
             if isinstance(n, ast.Return) and n.lineno not in nested and n.lineno > box_line]

    assert len(after) == 8, (
        f"core 推导点之后的 return 出口数变成 {len(after)}（行 {sorted(after)}）而 "
        f"`_verify_runtime_impl` docstring 仍写 8。要么同步那个数字，要么——如果你正在"
        f"逐个出口接线——停手：收口的整个理由就是出口会变。")


# ═══════════════════ 突变对照（7 条逐个已跑，基线先验绿；每条红的锁如下） ═══════════════
# 1. `detect_migration_kind` 加回**整体** swallow → 1 红：
#      test_swallow_gone_detect_migration_kind_raises_and_arm_records
#    ★这条的第一版突变我写错了★：只把函数开头两行塞进 try，而原 swallow 包的是整个函数体；
#    测试的异常在 `sql_dirs` 访问处抛，落在 try 之外 ⇒ 突变等于没施加，"仍绿"的真因是我
#    自己（"突变仍绿"三嫌疑人里的第三个）。改成模块末尾重绑吞异常包装才与原缺陷等价。
# 2. `smoke_skip_reason_for` 恒返 "derivation_incomplete" → 2 红：
#      test_skip_reason_is_error_when_the_missing_field_itself_errored
#      test_impl_shell_appends_degraded_and_details
#    （突变态下 `smoke_derive_error:start_cmd` **仍在** degraded 里 ⇒ 实测证明两个信号
#      不互相兜底，不是"同一事实过滤两遍"那种不可证伪的冗余防御）
# 3. `migration_kind_undetermined` 恒 False → 2 红（两个消费点各一条，正是"改一处＝半落地"
#    要防的形态）：test_migration_not_run_patch_* / test_run_migration_phase_*
# 4. `_note()` 不写 derive_errors 键 → 7 红（记账源头断掉 ⇒ 全链无信号）
# 5. 壳里 `_append_degraded` 删掉 → 1 红：test_impl_shell_appends_degraded_and_details
#    （只此一条 ⇒ 与 skip_reason 分档确实不重叠）
# 6. `missing & errored` 改成 `bool(errored)` → 1 红：
#      test_skip_reason_blames_the_field_that_caused_the_skip（误归因锁本命）
# 7. 给 core 在推导点之后加第 9 个 return 出口 → 1 红：test_core_exits_are_machine_counted
#    （验那条 AST 计数锁真有牙——它是"收口而非逐出口接线"这个理由的唯一守卫）
