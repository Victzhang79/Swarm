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
