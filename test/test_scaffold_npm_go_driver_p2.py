#!/usr/bin/env python3
"""#31-Phase2b/2c：npm/go 脚手架 driver 端到端注入（栈中立铺开，Maven 路径零改动）。

治本（G9 铺开）：round39 起脚手架注入只认 Maven（只造 pom.xml）；npm/go 工程规则5 落空模块
此前无任何确定性构建清单出口 → 回到派 worker 手写 package.json/go.mod + 臆造依赖版本的 R47/R53
病。本 driver 给 npm/go 补等价 per-module 清单脚手架：版本经 registry 确定性解析（绝不臆造），
内部包/module 走 workspace:*/replace（零网络）。

纪律：registry 联网全打桩（monkeypatch _http_get），绝不真联网；红线复核=R12 不臆造版本、
内部包不查 registry；Maven 检测优先级不被破坏（有 pom 证据仍走 Maven）。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain import contract_utils as cu  # noqa: E402
from swarm.brain import go_registry as gr  # noqa: E402
from swarm.brain import npm_registry as nr  # noqa: E402
from swarm.brain.contract_utils import inject_build_scaffold_subtasks  # noqa: E402
from swarm.brain.plan_validator import validate_plan_structure  # noqa: E402
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan  # noqa: E402


def _st(sid, create=None, writable=None):
    return SubTask(id=sid, description=f"task {sid}", difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=writable or [], create_files=create or []))


# ═══════════════════════ npm driver ═══════════════════════

def _npm_plan():
    # 单段模块标签（core/web）落在 packages/<label>/ 物理目录（module→dir 由 scope 源码自证，
    # 与 Maven 契约模块名同口径：标签是单段名，物理目录可含前缀）
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["packages/core/src/index.ts"]),
        _st("st-2", create=["packages/web/src/app.ts"]),
    ], parallel_groups=[["st-1"], ["st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "core", "artifacts": ["axios"]},
        # web 依赖第三方 lodash + 内部 core（用其 npm 名/标签引用）
        {"module": "web", "artifacts": ["lodash", "core"]},
    ]}
    return plan


@pytest.fixture
def _npm_project(tmp_path, monkeypatch):
    # 棕地 npm workspace 根（有根 package.json → 栈检测=npm）
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "root", "private": True, "workspaces": ["packages/*"]}),
        encoding="utf-8")
    monkeypatch.setenv("SWARM_NPM_LOOKUP", "1")
    nr._http_cache.clear()

    def fake_get(url):
        if "axios" in url:
            return json.dumps({"dist-tags": {"latest": "1.6.8"}})
        if "lodash" in url:
            return json.dumps({"dist-tags": {"latest": "4.17.21"}})
        return None

    monkeypatch.setattr(nr, "_http_get", fake_get)
    return tmp_path


def test_npm_scaffold_injected_with_resolved_versions(_npm_project):
    plan = _npm_plan()
    injected = inject_build_scaffold_subtasks(plan, str(_npm_project))
    mods = {e["module"] for e in injected}
    assert mods == {"core", "web"}
    assert all(e["stack"] == "npm" for e in injected)

    web_sid = next(e["subtask_id"] for e in injected if e["module"] == "web")
    web = next(st for st in plan.subtasks if st.id == web_sid)
    # 权威 package.json 模板嵌进 description（CREATE，基线无该包 package.json）
    assert "packages/web/package.json" in web.scope.create_files
    assert "权威 package.json 模板" in web.description
    tpl = web.description.split("```json\n", 1)[1].split("\n```", 1)[0]
    body = json.loads(tpl)
    # 第三方 lodash 解析出 ^4.17.21（绝不臆造）
    assert body["dependencies"]["lodash"] == "^4.17.21"
    # 内部 core 包 → workspace:*（零网络，绝不查 registry）
    assert body["dependencies"]["core"] == "workspace:*"


def test_npm_scaffold_owns_manifest_and_wires_deps(_npm_project):
    plan = _npm_plan()
    injected = inject_build_scaffold_subtasks(plan, str(_npm_project))
    core_sid = next(e["subtask_id"] for e in injected if e["module"] == "core")
    # 写代码子任务 depends_on 脚手架
    st1 = next(st for st in plan.subtasks if st.id == "st-1")
    assert core_sid in st1.depends_on
    # 结构合法（全员入组、无环）
    validate_plan_structure(plan)


def test_npm_unresolvable_dep_dropped_not_guessed(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text(json.dumps({"workspaces": ["m/*"]}), encoding="utf-8")
    monkeypatch.setenv("SWARM_NPM_LOOKUP", "1")
    nr._http_cache.clear()
    monkeypatch.setattr(nr, "_http_get", lambda url: None)  # 全查不到
    plan = TaskPlan(subtasks=[_st("st-1", create=["m/pkga/src/i.ts"])],
                    parallel_groups=[["st-1"]])
    plan.shared_contract = {"dependencies": [{"module": "pkga", "artifacts": ["ghost-pkg-xyz"]}]}
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path))
    sid = injected[0]["subtask_id"]
    st = next(s for s in plan.subtasks if s.id == sid)
    tpl = json.loads(st.description.split("```json\n", 1)[1].split("\n```", 1)[0])
    # R12：查不到版本 → drop，模板里没有它（绝不臆造 ^x.y.z）
    assert "ghost-pkg-xyz" not in tpl.get("dependencies", {})
    assert injected[0]["artifacts"] == []


# ═══════════════════════ go driver ═══════════════════════

def _go_plan():
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["svc/auth/main.go"]),
        _st("st-2", create=["svc/gateway/main.go"]),
    ], parallel_groups=[["st-1"], ["st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "auth", "artifacts": ["github.com/golang-jwt/jwt/v5"]},
        # gateway 依赖第三方 gin + 内部 auth（用模块标签引用）
        {"module": "gateway", "artifacts": ["github.com/gin-gonic/gin", "auth"]},
    ]}
    return plan


@pytest.fixture
def _go_project(tmp_path, monkeypatch):
    # 棕地 go workspace 根（根 go.mod → 栈=go，且供推导内部 import 路径 <root>/<reldir>）
    (tmp_path / "go.mod").write_text("module example.com/app\n\ngo 1.22\n", encoding="utf-8")
    monkeypatch.setenv("SWARM_GO_LOOKUP", "1")
    monkeypatch.setenv("GOPATH", str(tmp_path / "_empty_gopath"))
    gr._http_cache.clear()

    def fake_get(url):
        if "gin-gonic/gin" in url:
            return json.dumps({"Version": "v1.9.1"})
        if "golang-jwt" in url:
            return json.dumps({"Version": "v5.2.0"})
        return None

    monkeypatch.setattr(gr, "_http_get", fake_get)
    return tmp_path


def test_go_scaffold_injected_with_resolved_versions_and_replace(_go_project):
    plan = _go_plan()
    injected = inject_build_scaffold_subtasks(plan, str(_go_project))
    mods = {e["module"] for e in injected}
    assert mods == {"auth", "gateway"}
    assert all(e["stack"] == "go" for e in injected)

    gw_sid = next(e["subtask_id"] for e in injected if e["module"] == "gateway")
    gw = next(st for st in plan.subtasks if st.id == gw_sid)
    assert "svc/gateway/go.mod" in gw.scope.create_files
    assert "权威 go.mod 模板" in gw.description
    tpl = gw.description.split("```\n", 1)[1].rsplit("\n```", 1)[0]
    # 本模块 import 路径 = 根 module + reldir（惯例推导，非臆造）
    assert "module example.com/app/svc/gateway" in tpl
    assert "go 1.22" in tpl  # 读根 go 指令真值
    # 第三方 gin 解析出 v1.9.1（绝不臆造/伪版本）
    assert "github.com/gin-gonic/gin v1.9.1" in tpl
    # 内部 auth → replace 到规范 import 路径 + 相对路径（绝不裸标签）
    assert "replace example.com/app/svc/auth => ../auth" in tpl
    assert "replace svc/auth" not in tpl  # 裸标签绝不泄进 go.mod


def test_go_scaffold_wires_and_validates(_go_project):
    plan = _go_plan()
    injected = inject_build_scaffold_subtasks(plan, str(_go_project))
    auth_sid = next(e["subtask_id"] for e in injected if e["module"] == "auth")
    st1 = next(st for st in plan.subtasks if st.id == "st-1")
    assert auth_sid in st1.depends_on
    validate_plan_structure(plan)


def test_go_no_root_gomod_skips_scaffold(tmp_path, monkeypatch):
    """无根 go.mod → 内部 import 路径不可推导 → 跳过该 go.mod 脚手架（绝不臆造假 module 路径）。"""
    monkeypatch.setenv("SWARM_GO_LOOKUP", "0")
    # 计划里放一个 go.mod scope 路径让栈检测=go，但根无 go.mod
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["services/asvc/main.go"]),
        _st("st-0", create=["services/other/go.mod"]),  # 让栈检测认出 go
    ], parallel_groups=[["st-0"], ["st-1"]])
    plan.shared_contract = {"dependencies": [{"module": "asvc", "artifacts": []}]}
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path))
    # asvc 无根 go.mod → 无法推导 import 路径 → 不注入（fail-open，绝不造假路径）
    assert not any(e["module"] == "asvc" for e in injected)


# ═══════════════════════ B-0：栈 × driver 满矩阵 ═══════════════════════
#
# 27 号文 §4.3 实测：6244 个测试函数里真正多栈 parametrize 的只有 7 块，**没有一个测栈
# 分发路由本身**。下面这条就是补那一格：根清单 → 检出栈 → **该由谁造清单**，逐格锁死。
#
# 为什么是"满"矩阵：`STACK_SPEC` 全部栈 + unknown（无证据）都必须有一格，且由
# `test_driver_matrix_covers_every_registered_stack` 强制——新增一栈时若不给它一格，
# 那条会红。这挡的是"新栈悄悄漏出矩阵"（B-7 新栈准入闸的雏形）。

# (matrix_id, 根清单名, 根清单内容, 契约模块标签, 模块源码相对路径, 期望清单 basename 或 None)
#
# ★"契约模块标签"这一列是复核 HIGH-2 的整改★ 原先所有行都写死标签 `mod-a`，而 python 行的
# 目录是 `mod_a`（下划线）→ 标签对不上；更致命的是原 python 源码路径 `pkg/mod_a/__init__.py`
# **无标准源码布局段** → `_module_physical_dirs` 恒返 `{}` → `injected==[]` 与栈路由**无关地
# 恒成立**。实测：把 `pyproject.toml` 从栈识别表摘掉（真实故障"根清单没接进路由表"；当年是
# `contract_utils._MANIFEST_TO_STACK`，P-C1 已删该第二事实源，现落点＝`stacks/spec.py` 的
# `STACK_SPEC["python"].root_manifests`）→ 纯 python 仓落进 `(True,'unknown')` Maven 兜底，
# 而那一格照旧绿——正是该格
# 准入闸 docstring 逐字声称要防的事。对照组：摘 settings.gradle → 只有 gradle 格红；
# 摘 Cargo.toml → 只有 cargo 格红。
_DRIVER_MATRIX = [
    ("maven", "pom.xml",
     "<project><groupId>g</groupId><artifactId>root</artifactId><version>1.0</version></project>",
     "mod-a", "mod-a/src/main/java/com/demo/A.java", "pom.xml"),
    ("npm", "package.json", '{"name":"root","private":true,"workspaces":["packages/*"]}',
     "mod-a", "packages/mod-a/src/index.ts", "package.json"),
    ("go", "go.mod", "module example.com/app\n\ngo 1.22\n",
     "mod-a", "svc/mod-a/main.go", "go.mod"),
    # ★P-H4 诚实边界★ 认出来了却零脚手架出口 —— 期望值写 None 是**如实记录现状**，
    # 不是"应该如此"。B-6 给这栈补 driver 那天，这格要改成对应清单名。
    # （python 格已于 P-H4a、cargo 格已于 P-H4b 从 None 改为各自清单名——driver 已落地）
    ("gradle", "settings.gradle", "include ':mod-a'\n",
     "mod-a", "mod-a/src/main/kotlin/com/demo/A.kt", None),
    ("cargo", "Cargo.toml", '[workspace]\nmembers = ["crates/mod-a"]\n',
     "mod-a", "crates/mod-a/src/lib.rs", "Cargo.toml"),
    # python 用 `services/<mod>/` 多服务布局：标准 `src/<pkg>/` 与 `pkg/<mod>/` 都解析不出
    # 物理落点（"src"/"pkg" 都在 `_SRC_LAYOUT_SEGMENTS` 里会被当布局段剥掉——"pkg" 是当年为
    # Go 的 cmd/internal/pkg 塞进去的，27 号文 P-M4 已把那张表标成补丁磁铁）。
    ("python", "pyproject.toml", '[project]\nname = "root"\nversion = "0.1.0"\n',
     "mod_a", "services/mod_a/service.py", "pyproject.toml"),
    # 无任何清单证据 → 保守回退 Maven（back-compat，下游 R57-1 pom 取证二次把关）
    ("unknown", None, None, "mod-a", "mod-a/src/main/java/com/demo/A.java", "pom.xml"),
]


def test_driver_matrix_covers_every_registered_stack():
    """准入闸：`STACK_SPEC` 里每个栈都必须在矩阵里有一格（含 unknown 兜底格）。

    这条防的是"新增一栈只在事实表加一行，分发路由的期望却没人声明"——那样新栈会静默
    走进某个默认分支（本仓 unknown→Maven 的兜底恰好会吞下它），而矩阵全绿。
    """
    from swarm.stacks import STACK_SPEC

    covered = {row[0] for row in _DRIVER_MATRIX}
    missing = sorted(set(STACK_SPEC) - covered)
    assert not missing, f"新栈 {missing} 未在 driver 矩阵登记期望值（会静默走兜底分支）"
    assert "unknown" in covered, "缺 unknown 格：无清单证据的兜底方向必须被锁死"


@pytest.mark.parametrize("stack,manifest,content,label,src,expect_manifest", _DRIVER_MATRIX,
                         ids=[r[0] for r in _DRIVER_MATRIX])
def test_scaffold_driver_dispatch_matrix(tmp_path, monkeypatch, stack, manifest, content,
                                         label, src, expect_manifest):
    """满矩阵：根清单决定栈，栈决定**谁造清单**，且**绝不跨栈污染**。

    两条最要紧的断言：
    - **正向前提**（复核 HIGH-2 整改）：`_module_physical_dirs` 必须非空。没有它，
      `expect_manifest is None` 的格子就是"什么都没发生"——而那既是期望值、**也是任何故障
      的表现**（栈路由整个坏掉也长这样）。有了它，那些格断的才是"模块确实解析出来了，
      只是**没人给它建清单**"。这正是本批教训 2（夹具形状决定测的是哪条命题）的同型复发。
    - **零跨栈污染**：非 Maven 栈一个 `pom.xml` 都不许出现（L4/L8 血泪：幻影 pom 写权、
      根聚合闸放过 `settings.gradle`/`go.work`，是本仓反复复发的一族）。
    """
    monkeypatch.setenv("SWARM_NPM_LOOKUP", "0")   # 离线：解析不到就如实丢弃，绝不臆造
    monkeypatch.setenv("SWARM_GO_LOOKUP", "0")
    monkeypatch.setenv("SWARM_CARGO_LOOKUP", "0")   # P-H4b：cargo 同纪律
    if manifest:
        (tmp_path / manifest).write_text(content, encoding="utf-8")

    plan = TaskPlan(subtasks=[_st("st-1", create=[src])], parallel_groups=[["st-1"]])
    plan.shared_contract = {"dependencies": [{"module": label, "artifacts": []}]}

    # ★正向前提：模块得先解析得出物理落点★（见 docstring）
    dirs = cu._module_physical_dirs(plan, str(tmp_path))
    assert label in dirs, (
        f"{stack}: 契约模块 {label!r} 解析不出物理落点（实得 {dirs}）→ 本行任何"
        f"'没注入'的结论都与栈路由无关地恒真，测不出东西。修夹具路径/标签，别改断言")

    injected = inject_build_scaffold_subtasks(plan, str(tmp_path))
    created = [f for st in plan.subtasks
               for f in (list(st.scope.create_files) + list(st.scope.writable))]

    if expect_manifest is None:
        assert injected == [], (
            f"{stack}: 该栈目前无脚手架 driver，却注入了 {injected}"
            "（若刚给它补了 driver，请把矩阵这一格从 None 改成清单名）")
    else:
        assert injected, f"{stack}: 期望注入 {expect_manifest} 脚手架，实得空"
        assert any(f.rsplit("/", 1)[-1] == expect_manifest for f in created), (
            f"{stack}: 期望造出 {expect_manifest}，实得 {created}")

    if stack not in ("maven", "unknown"):
        assert not any(f.rsplit("/", 1)[-1] == "pom.xml" for f in created), (
            f"{stack}: Maven 产物泄漏进异栈工程（L4/L8 复发）：{created}")


# ═══════════════════════ Maven 优先级回归 ═══════════════════════

def test_maven_still_wins_over_npm_when_pom_present(tmp_path, monkeypatch):
    """混栈护栏回归：有 pom 证据 → 仍走 Maven pom 脚手架，绝不被 npm driver 抢走。"""
    (tmp_path / "pom.xml").write_text("<project><groupId>g</groupId>"
                                      "<artifactId>root</artifactId><version>1.0</version></project>",
                                      encoding="utf-8")
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/src/main/java/A.java"]),
        _st("st-2", create=["mod-a/src/main/java/B.java"]),
        _st("st-3", create=["frontend/app.ts"]),  # 混入前端 .ts
    ], parallel_groups=[["st-1"], ["st-2", "st-3"]])
    plan.shared_contract = {"dependencies": [
        {"module": "mod-a", "artifacts": ["org.projectlombok:lombok"]},
    ]}
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path))
    # Maven 脚手架建 pom.xml，不是 package.json（有 pom 证据混栈优先保 Maven）
    a = next(e for e in injected if e["module"] == "mod-a")
    assert a.get("stack") != "npm"
    sc = next(st for st in plan.subtasks if st.id == a["subtask_id"])
    assert any("pom.xml" in f for f in (sc.scope.create_files + sc.scope.writable))


# ═══════════════════════ 对抗双复核整改回归 ═══════════════════════

def test_p2_npm_owned_internal_never_hits_public_registry(_npm_project, monkeypatch):
    """cr#2/hunter#1 回归：内部包 core 的 package.json 已被子任务认领（不在 unclaimed）→ 内部标识
    仍必须从【全物理模块集】取，令 web 对 core 的依赖=workspace:*，绝不被当同名公网包解析。"""
    # core 的 package.json 被 st-core 认领（不进 unclaimed entries）
    plan = TaskPlan(subtasks=[
        _st("st-core", create=["packages/core/package.json", "packages/core/src/i.ts"]),
        _st("st-web", create=["packages/web/src/app.ts"]),
    ], parallel_groups=[["st-core"], ["st-web"]])
    plan.shared_contract = {"dependencies": [
        {"module": "core", "artifacts": ["axios"]},
        {"module": "web", "artifacts": ["core", "lodash"]},
    ]}
    # registry 对 "core" 也会返回一个真版本——证明我们【没有】去查它
    def fake_get(url):
        if "/core" in url:
            return json.dumps({"dist-tags": {"latest": "9.9.9"}})  # 无关公网包，绝不该采用
        if "lodash" in url:
            return json.dumps({"dist-tags": {"latest": "4.17.21"}})
        if "axios" in url:
            return json.dumps({"dist-tags": {"latest": "1.6.8"}})
        return None
    monkeypatch.setattr(nr, "_http_get", fake_get)
    injected = inject_build_scaffold_subtasks(plan, str(_npm_project))
    # web 是 unclaimed → 注入脚手架；core 已认领 → 不注入（走 owner-backfill）
    assert {e["module"] for e in injected} == {"web"}
    web = next(st for st in plan.subtasks if st.id == injected[0]["subtask_id"])
    tpl = json.loads(web.description.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert tpl["dependencies"]["core"] == "workspace:*", "内部包绝不被当公网包(^9.9.9)"
    assert tpl["dependencies"]["lodash"] == "^4.17.21"


def test_p2_npm_owner_backfill(_npm_project):
    """cr#1 回归：子任务自认领 package.json → 确定性清单块必须 backfill 进 owner description
    （有 owner≠有模板，防 owner 手写臆造版本），且不另注入 st-scaffold。"""
    plan = TaskPlan(subtasks=[
        _st("st-core", create=["packages/core/package.json", "packages/core/src/i.ts"]),
    ], parallel_groups=[["st-core"]])
    plan.shared_contract = {"dependencies": [{"module": "core", "artifacts": ["axios"]}]}
    injected = inject_build_scaffold_subtasks(plan, str(_npm_project))
    assert injected == [], "已认领 → 不另注入脚手架"
    st = next(s for s in plan.subtasks if s.id == "st-core")
    assert "权威 package.json 模板" in st.description, "owner 拿到确定性模板"
    assert "axios" in st.description and "1.6.8" in st.description


def test_p2_npm_dropped_dep_pruned_from_shared_contract(tmp_path, monkeypatch):
    """hunter#3 回归：解析不到的依赖必须从 plan.shared_contract 同源剪除并记 pruned_artifacts 账
    （否则只读契约仍'要求'它=模板没有验收却要求的 round63 考卷矛盾）。"""
    (tmp_path / "package.json").write_text(json.dumps({"workspaces": ["m/*"]}), encoding="utf-8")
    monkeypatch.setenv("SWARM_NPM_LOOKUP", "1")
    nr._http_cache.clear()
    monkeypatch.setattr(nr, "_http_get",
                        lambda url: json.dumps({"dist-tags": {"latest": "1.0.0"}})
                        if "keep-pkg" in url else None)
    plan = TaskPlan(subtasks=[_st("st-1", create=["m/svc/src/i.ts"])], parallel_groups=[["st-1"]])
    plan.shared_contract = {"dependencies": [
        {"module": "svc", "artifacts": ["keep-pkg", "ghost-unresolvable"]}]}
    inject_build_scaffold_subtasks(plan, str(tmp_path))
    entry = next(e for e in plan.shared_contract["dependencies"] if e["module"] == "svc")
    assert entry["artifacts"] == ["keep-pkg"], "dropped 依赖从契约同源剪除"
    # dict 账本（与 Maven 同 schema）：{module: [dropped]}
    assert plan.shared_contract.get("pruned_artifacts", {}).get("svc") == ["ghost-unresolvable"]


def test_p2_go_modify_path_surfaces_replace(_go_project):
    """cr#3 回归：既有 go.mod（MODIFY 路径）+ 仅内部依赖 → replace 指令必须落进指引块，
    绝不像旧版只在 CREATE 落而 MODIFY 整段丢。"""
    # gateway/go.mod 预先存在（MODIFY），未被子任务认领
    (_go_project / "svc" / "gateway").mkdir(parents=True)
    (_go_project / "svc" / "gateway" / "go.mod").write_text(
        "module example.com/app/svc/gateway\n\ngo 1.22\n", encoding="utf-8")
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["svc/auth/main.go"]),
        _st("st-2", create=["svc/gateway/handler.go"]),  # 源码，不认领 go.mod
    ], parallel_groups=[["st-1"], ["st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "auth", "artifacts": []},
        {"module": "gateway", "artifacts": ["auth"]},  # 仅内部依赖
    ]}
    injected = inject_build_scaffold_subtasks(plan, str(_go_project))
    gw = next(e for e in injected if e["module"] == "gateway")
    assert gw["manifest_exists"] is True, "MODIFY 路径"
    st = next(s for s in plan.subtasks if s.id == gw["subtask_id"])
    assert "replace example.com/app/svc/auth => ../auth" in st.description, "MODIFY 也落 replace"


def test_p2_pruned_artifacts_dict_schema_matches_maven(tmp_path, monkeypatch):
    """hunter NEW HIGH 回归：pruned_artifacts 必须是 dict {module:[dropped]}（与 Maven
    prune_contract_dependencies 同形），绝不用 list 撞它的 dict → 否则跨栈 replan 轮互撞
    （Maven 侧崩 / npm 侧静默丢账）。"""
    (tmp_path / "package.json").write_text(json.dumps({"workspaces": ["m/*"]}), encoding="utf-8")
    monkeypatch.setenv("SWARM_NPM_LOOKUP", "1")
    nr._http_cache.clear()
    monkeypatch.setattr(nr, "_http_get", lambda url: None)  # 全 drop
    plan = TaskPlan(subtasks=[_st("st-1", create=["m/svc/src/i.ts"])], parallel_groups=[["st-1"]])
    plan.shared_contract = {"dependencies": [{"module": "svc", "artifacts": ["ghost"]}]}
    inject_build_scaffold_subtasks(plan, str(tmp_path))
    led = plan.shared_contract.get("pruned_artifacts")
    assert isinstance(led, dict), f"必须是 dict（Maven 同 schema），实为 {type(led).__name__}"
    assert led.get("svc") == ["ghost"]
    # 与 Maven 账本自释义 note 同键
    assert "pruned_artifacts_note" in plan.shared_contract


def test_p2_pruned_artifacts_self_heals_when_resolvable(tmp_path, monkeypatch):
    """撤账语义（与 Maven 同）：上一轮被剪的依赖本轮可解析 → 从账本撤除、契约复原。"""
    (tmp_path / "package.json").write_text(json.dumps({"workspaces": ["m/*"]}), encoding="utf-8")
    monkeypatch.setenv("SWARM_NPM_LOOKUP", "1")
    nr._http_cache.clear()
    # 预置一条陈旧账（模拟上一轮 drop），本轮该依赖可解析
    plan = TaskPlan(subtasks=[_st("st-1", create=["m/svc/src/i.ts"])], parallel_groups=[["st-1"]])
    plan.shared_contract = {"dependencies": [{"module": "svc", "artifacts": ["axios"]}],
                            "pruned_artifacts": {"svc": ["axios"]}}
    monkeypatch.setattr(nr, "_http_get", lambda url: json.dumps({"dist-tags": {"latest": "1.6.8"}}))
    inject_build_scaffold_subtasks(plan, str(tmp_path))
    # svc 本轮全解析 → 账本撤除该条（空则整键删）
    assert "svc" not in plan.shared_contract.get("pruned_artifacts", {})


def test_p2_upsert_non_bridging_orphan_sentinel(_npm_project):
    """hunter NEW MEDIUM 回归：description 含孤儿起始 sentinel（无配对结束，模拟外部截断）时，
    下一轮 upsert 绝不桥接吞掉后续良构块的中间内容。"""
    from swarm.brain.contract_utils import _upsert_owner_manifest_block
    from types import SimpleNamespace
    mr = "packages/x/package.json"
    # 孤儿起始 + 一段合法内容（无结束 sentinel）
    owner = SimpleNamespace(description=f"原始描述\n<!--#31P2 {mr}-->孤儿块无结束标签")
    _upsert_owner_manifest_block(owner, mr, "\n【块V2】body")
    # 孤儿保留（无害陈旧），新块良构追加，原始描述与孤儿内容都未被误删
    assert "原始描述" in owner.description
    assert "孤儿块无结束标签" in owner.description
    assert "【块V2】body" in owner.description
    # 再来一轮：良构块 strip+重贴，孤儿仍在，绝不塌缩误删
    before = owner.description
    _upsert_owner_manifest_block(owner, mr, "\n【块V2】body")  # 同块 → 幂等
    assert "原始描述" in owner.description and "孤儿块无结束标签" in owner.description
    assert owner.description.count("【块V2】body") == 1, "良构块不重复"
