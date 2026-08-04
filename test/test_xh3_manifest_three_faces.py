"""X-H3（27 号文 B-5）：ManifestDriver add/prune/probes 三面同源——npm 与 .sln 补面。

治前（27 号文 X-H3）：`workspace_manifest` 的 add/prune 不对称——
  · npm workspaces【显式列表】形态三面全空：新子包永不进 workspaces[] →
    `npm ci` 装不到 → import 确定性失败（glob 形态自愈，本就不碰）；
  · .sln 有 add（_reconcile_dotnet_sln）无 probes/prune：只增不减 → 幽灵工程
    条目 → msbuild 硬错。

本文件锁：①npm probes（显式收、glob/否定剔、成员包自身文件不算聚合清单）；
②npm prune（幽灵摘、glob 保、object 形、坏 JSON 不产出更坏文件）；
③npm add（容器只凭既有显式条目父目录推断，绝不猜约定目录名）；
④.sln probes/prune（工程文件后缀已知才收；摘块连同 GUID 配置行；同名工程
不同路径不撞键）；⑤三面同源矩阵锁（有成员的清单 probes 非空 ∧ prune 有效 ∧
add 在册）；⑥prune_stale 候选接线（package.json / .sln 进候选）。
"""
from __future__ import annotations

import json

from swarm.worker.workspace_manifest import (
    _reconcile_npm,
    manifest_member_probes,
    prune_manifest_members,
    prune_stale_manifest_members,
    reconcile_workspace_manifests,
)

# ── ① npm probes ────────────────────────────────────────────────────────────

def test_npm_probes_explicit_only_glob_and_negation_skipped():
    text = json.dumps({"workspaces": ["packages/web", "packages/*",
                                      "!packages/legacy", "./apps/api"]})
    probes = manifest_member_probes("package.json", text)
    assert ("packages/web", "packages/web/package.json") in probes
    assert ("apps/api", "apps/api/package.json") in probes
    assert not any("*" in t or "legacy" in t for t, _ in probes), \
        "glob/否定条目绝不进 probes（glob 进 probes=prune 会把它当幽灵摘掉，灾变向）"


def test_npm_probes_member_package_own_manifest_is_not_aggregate():
    """成员包自己的 package.json 不是聚合清单（workspaces 只在根有聚合语义）。"""
    text = json.dumps({"workspaces": ["packages/web"]})
    assert manifest_member_probes("packages/web/package.json", text) == []


def test_npm_probes_object_form_and_missing_field():
    assert manifest_member_probes(
        "package.json", json.dumps({"workspaces": {"packages": ["a"]}})) == [
        ("a", "a/package.json")]
    assert manifest_member_probes("package.json", json.dumps({"name": "x"})) == []
    assert manifest_member_probes("package.json", "{broken") == []


# ── ② npm prune ─────────────────────────────────────────────────────────────

def test_npm_prune_removes_ghost_keeps_globs():
    text = json.dumps({"workspaces": ["packages/web", "packages/*", "apps/api"]})
    new_text, removed = prune_manifest_members(
        "package.json", text,
        lambda p: False if p.startswith("packages/web") else None)
    assert removed == ["packages/web"]
    ws = json.loads(new_text)["workspaces"]
    assert "packages/*" in ws and "apps/api" in ws and "packages/web" not in ws


def test_npm_prune_object_form_and_non_member_regions_untouched():
    text = json.dumps({"workspaces": {"packages": ["a", "b"], "nohoist": ["x"]}})
    new_text, removed = prune_manifest_members(
        "package.json", text, lambda p: False if p == "a/package.json" else None)
    assert removed == ["a"]
    obj = json.loads(new_text)
    assert obj["workspaces"]["packages"] == ["b"]
    assert obj["workspaces"]["nohoist"] == ["x"], "非成员区不得动"


def test_npm_prune_corrupt_json_preserved_not_corrupted_further():
    bad = '{"workspaces": ["packages/web",'
    new_text, removed = prune_manifest_members("package.json", bad, lambda p: False)
    assert removed == [] and new_text == bad, "解析失败必须原文保留（绝不产出损坏 JSON）"


# ── ③ npm add（_reconcile_npm）───────────────────────────────────────────────

def test_npm_add_infers_container_from_explicit_entries(tmp_path):
    """新子包进 workspaces；幂等。"""
    (tmp_path / "package.json").write_text(json.dumps(
        {"name": "mono", "workspaces": ["packages/web"]}))
    for p in ("packages/web", "packages/alarm"):
        (tmp_path / p).mkdir(parents=True)
        (tmp_path / p / "package.json").write_text("{}")
    out = reconcile_workspace_manifests(str(tmp_path))
    assert out["added"] == {"package.json": ["packages/alarm"]}, out
    assert reconcile_workspace_manifests(str(tmp_path))["added"] == {}, "非幂等"


def test_npm_add_never_guesses_conventional_container_names(tmp_path):
    """★血规 2 锁★ 容器证据只来自既有显式条目——显式成员在根（"web"）时，
    packages/ 下的新包【不得】被当成员（"packages/apps 是约定容器"是臆测）。"""
    (tmp_path / "package.json").write_text(json.dumps({"workspaces": ["web"]}))
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text("{}")
    (tmp_path / "packages" / "new").mkdir(parents=True)
    (tmp_path / "packages" / "new" / "package.json").write_text("{}")
    assert reconcile_workspace_manifests(str(tmp_path))["added"] == {}


def test_npm_add_skips_pure_glob_and_glob_covered(tmp_path):
    """纯 glob=无容器证据不加；glob 已覆盖=自愈范围不加。"""
    (tmp_path / "package.json").write_text(json.dumps(
        {"workspaces": ["apps/*", "packages/web"]}))
    for p in ("apps/x", "packages/web", "packages/new"):
        (tmp_path / p).mkdir(parents=True)
        (tmp_path / p / "package.json").write_text("{}")
    out = reconcile_workspace_manifests(str(tmp_path))
    assert out["added"] == {"package.json": ["packages/new"]}, out  # apps/x 被 glob 覆盖


def test_npm_add_never_creates_workspaces_field(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "single"}))
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "package.json").write_text("{}")
    assert reconcile_workspace_manifests(str(tmp_path))["added"] == {}
    assert json.loads((tmp_path / "package.json").read_text()) == {"name": "single"}


# ── ④ .sln probes/prune ─────────────────────────────────────────────────────

_SLN = (
    'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "Web", '
    '"src\\Web\\Web.csproj", "{AAAA-1}"\nEndProject\n'
    'Project("{2150E333-8FDC-42A3-9474-1A3956D46DE8}") = "infra", "infra", "{F0F0}"\n'
    'EndProject\n'
    'Global\n'
    '\tGlobalSection(ProjectConfigurationPlatforms) = postSolution\n'
    '\t\t{AAAA-1}.Debug|Any CPU.ActiveCfg = Debug|Any CPU\n'
    '\t\t{AAAA-1}.Debug|Any CPU.Build.0 = Debug|Any CPU\n'
    '\tEndGlobalSection\nEndGlobal\n'
)


def test_sln_probes_project_files_only_not_solution_folders():
    assert manifest_member_probes("App.sln", _SLN) == [
        ("src/Web/Web.csproj", "src/Web/Web.csproj")], \
        "解决方案文件夹（path=名字无文件）绝不进 probes"


def test_sln_prune_removes_block_and_guid_config_lines():
    new_text, removed = prune_manifest_members("App.sln", _SLN, lambda p: False)
    assert removed == ["src/Web/Web.csproj"]
    assert "Web.csproj" not in new_text
    assert "{AAAA-1}" not in new_text, "只摘块不摘 GUID 配置行=悬挂引用"
    assert "GlobalSection(ProjectConfigurationPlatforms)" in new_text, "段结构必须保留"
    assert "infra" in new_text, "解决方案文件夹不得误伤"


def test_sln_prune_same_name_projects_distinct_paths_no_collision():
    """token=归一化路径（非工程名）：两个同名 Utils 只摘幽灵那个。"""
    sln = (
        'Project("{T}") = "Utils", "a\\Utils.csproj", "{G1}"\nEndProject\n'
        'Project("{T}") = "Utils", "b\\Utils.csproj", "{G2}"\nEndProject\n'
        'Global\n\tGlobalSection(ProjectConfigurationPlatforms) = postSolution\n'
        '\t\t{G1}.Debug|Any CPU.ActiveCfg = Debug|Any CPU\n'
        '\t\t{G2}.Debug|Any CPU.ActiveCfg = Debug|Any CPU\n'
        '\tEndGlobalSection\nEndGlobal\n')
    assert len(manifest_member_probes("App.sln", sln)) == 2
    new_text, removed = prune_manifest_members(
        "App.sln", sln, lambda p: False if p.startswith("a/") else True)
    assert removed == ["a/Utils.csproj"]
    assert "b\\Utils.csproj" in new_text and "{G2}" in new_text
    assert "{G1}" not in new_text


def test_sln_prune_matches_forward_slash_written_paths():
    """.sln 里也有写正斜杠的工程路径（少见但合法）——两种写法都摘得到。"""
    sln = ('Project("{T}") = "Web", "src/Web/Web.csproj", "{G9}"\nEndProject\n'
           'Global\n\tGlobalSection(ProjectConfigurationPlatforms) = postSolution\n'
           '\t\t{G9}.Debug|Any CPU.ActiveCfg = Debug|Any CPU\n'
           '\tEndGlobalSection\nEndGlobal\n')
    new_text, removed = prune_manifest_members("App.sln", sln, lambda p: False)
    assert removed == ["src/Web/Web.csproj"] and "{G9}" not in new_text


# ── ⑤ 三面同源矩阵锁 ──────────────────────────────────────────────────────────

def test_three_faces_cover_same_manifests():
    """★三面同源锁★ 每个被收编的聚合清单：probes 非空 ∧ prune 真摘 ∧ add 在册
    （`_RECONCILE_DISPATCH` 注册表有对应 _reconcile_*）。缺一=又一棵 X-H3 半边树。"""
    from swarm.worker import workspace_manifest as wm

    cases = {
        "package.json": json.dumps({"workspaces": ["packages/web"]}),
        "App.sln": _SLN,
        "go.work": "go 1.21\n\nuse (\n\t./a\n)\n",
        "Cargo.toml": '[workspace]\nmembers = [\n    "a",\n]\n',
        "settings.gradle": "include 'a'\n",
        "pom.xml": "<project><modules><module>a</module></modules></project>",
    }
    for rel, text in cases.items():
        probes = manifest_member_probes(rel, text)
        assert probes, f"{rel}: probes 空（三面缺第一面）"
        new_text, removed = prune_manifest_members(rel, text, lambda p: False)
        assert removed and new_text != text, f"{rel}: prune 无效（三面缺第二面）"
    # add 面：断【注册表】（单一事实源就是给测试用的，纪律 6）——禁 getsource 扫实现。
    dispatch_names = {fn.__name__ for fn in wm._RECONCILE_DISPATCH}
    for fn in ("_reconcile_maven", "_reconcile_gradle", "_reconcile_cargo",
               "_reconcile_dotnet_sln", "_reconcile_go_work", "_reconcile_npm"):
        assert fn in dispatch_names, f"{fn} 未在 _RECONCILE_DISPATCH（add 面缺接线）"


# ── ⑥ prune_stale 候选接线 ────────────────────────────────────────────────────

def test_prune_stale_picks_up_package_json_and_sln(tmp_path):
    """本地树 prune 入口必须把 package.json 与 .sln 纳入候选（机制存在≠接线覆盖）。"""
    (tmp_path / "package.json").write_text(json.dumps(
        {"workspaces": ["packages/ghost"]}))
    (tmp_path / "App.sln").write_text(_SLN)
    removed = prune_stale_manifest_members(str(tmp_path))
    assert removed.get("package.json") == ["packages/ghost"], removed
    assert removed.get("App.sln") == ["src/Web/Web.csproj"], removed
    # 写盘幂等：再跑一遍零摘除
    assert prune_stale_manifest_members(str(tmp_path)) == {}


# ── R2 双复核整改锁 ─────────────────────────────────────────────────────────

def test_npm_entries_path_escape_rejected_three_faces():
    """★R2 reviewer HIGH★ `../sibling`、`/abs` 条目三面拒收：probes 不吐、
    prune 不匹配、add 不把根目录外路径写进 workspaces。"""
    text = json.dumps({"workspaces": ["../sibling", "/abs/evil", "packages/web"]})
    probes = manifest_member_probes("package.json", text)
    assert probes == [("packages/web", "packages/web/package.json")], probes
    # add 面：逃逸条目不产生容器证据（../sibling 的父目录 ".." 被拒）
    new_text, removed = prune_manifest_members(
        "package.json", text, lambda p: False)
    assert removed == ["packages/web"] or removed == [], removed
    ws = json.loads(new_text)["workspaces"]
    assert "../sibling" in ws and "/abs/evil" in ws, \
        "逃逸条目被拒收后【保留原文】（不臆删用户数据），只是机制不碰它"


def test_npm_add_root_container_fail_closed(tmp_path):
    """★R2 reviewer MEDIUM 锁★ 顶层显式条目（"web"）⇒ 容器=根，但根目录的
    package.json 子目录鱼龙混杂（工具包/scripts），凭「有一员在根」推断「根级
    新包皆是成员」误杀面大 → fail-closed 不登记（多层容器照常对账，见上）。"""
    (tmp_path / "package.json").write_text(json.dumps({"workspaces": ["web"]}))
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text("{}")
    (tmp_path / "tools").mkdir()  # 根级工具包——绝不是 workspace 成员
    (tmp_path / "tools" / "package.json").write_text("{}")
    assert reconcile_workspace_manifests(str(tmp_path))["added"] == {}


def test_sln_uppercase_suffix_probed(tmp_path):
    """★R2 reviewer HIGH★ Windows 生态 .CSPROJ 大写后缀：probes 收、add 侧同口径。"""
    sln = ('Project("{T}") = "Web", "src\\Web\\Web.CSPROJ", "{G9}"\nEndProject\n')
    assert manifest_member_probes("App.sln", sln) == [
        ("src/Web/Web.CSPROJ", "src/Web/Web.CSPROJ")]
    # add 侧（_reconcile_dotnet_sln）同口径：大写 .CSPROJ 被发现并登记
    (tmp_path / "App.sln").write_text(
        'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "A", "a\\A.csproj", "{G1}"\n'
        'EndProject\nGlobal\n'
        '\tGlobalSection(ProjectConfigurationPlatforms) = postSolution\n'
        '\tEndGlobalSection\nEndGlobal\n')
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "A.csproj").write_text("<Project/>")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "B.CSPROJ").write_text("<Project/>")
    out = reconcile_workspace_manifests(str(tmp_path))
    added = out.get("added") or {}
    assert any("B" in str(m) for m in added.get("App.sln", [])), added


def test_npm_members_only_tier_still_warns_on_demote():
    """★R2 hunter CRITICAL★ _reconcile_npm 只补成员注册；demote 收回整文件写权时
    scripts/dependencies 编辑无兜底 → 聚合档必须仍判【不安全】（WARNING 照刷）。
    对照面：maven 聚合清单是纯结构文件 → 有 reconcile 即安全。"""
    from swarm.stacks.spec import demote_safety_net
    assert demote_safety_net("package.json", "npm") == (False, "aggregate")
    assert demote_safety_net("pom.xml", "maven") == (True, "aggregate")
    assert demote_safety_net("packages/a/package.json", "npm") == (True, "module")


def test_strip_worker_contribs_npm_and_sln_arms():
    """★R2 hunter HIGH★ H2 回滚的 strip 不再是 pom 独占：FAIL 子任务对根
    package.json/.sln 的新增贡献确定性摘除（glob/既有成员不动）。"""
    from swarm.worker.workspace_manifest import strip_worker_manifest_contribs
    head = json.dumps({"workspaces": ["packages/a"]})
    worker = json.dumps({"workspaces": ["packages/a", "packages/b", "packages/*"]})
    local = json.dumps({"workspaces": ["packages/a", "packages/b", "packages/*"]})
    new_text, n = strip_worker_manifest_contribs(local, worker, head, "package.json")
    assert n == 1
    assert json.loads(new_text)["workspaces"] == ["packages/a", "packages/*"]
    # .sln 臂：worker 新增的 Project 块 + GUID 配置行一起摘
    head_sln = 'Global\nEndGlobal\n'
    worker_sln = _SLN
    new_sln, n2 = strip_worker_manifest_contribs(_SLN, worker_sln, head_sln, "App.sln")
    assert n2 == 1
    assert "Web.csproj" not in new_sln and "{AAAA-1}" not in new_sln


def test_npm_json_indent_preserved_on_round_trip(tmp_path):
    """★R2 hunter MEDIUM★ 4 空格缩进的原文件 round-trip 后不被重排成 2 空格。"""
    original = '{\n    "name": "mono",\n    "workspaces": [\n        "packages/web"\n    ]\n}\n'
    (tmp_path / "package.json").write_text(original)
    (tmp_path / "packages" / "web").mkdir(parents=True)
    (tmp_path / "packages" / "web" / "package.json").write_text("{}")
    (tmp_path / "packages" / "alarm").mkdir()
    (tmp_path / "packages" / "alarm" / "package.json").write_text("{}")
    out = reconcile_workspace_manifests(str(tmp_path))
    assert out["added"] == {"package.json": ["packages/alarm"]}
    after = (tmp_path / "package.json").read_text()
    assert '\n    "workspaces"' in after, f"4 空格缩进被重排成 2 空格:\n{after}"
    assert '"packages/alarm"' in after


def test_npm_prune_parse_failure_warns(caplog):
    """★R2 hunter MEDIUM★ JSON 解析失败 ≠ 「无幽灵」——removed=[] 之外必须有信号。"""
    import logging
    with caplog.at_level(logging.WARNING):
        new_text, removed = prune_manifest_members(
            "package.json", '{"workspaces": ["a",', lambda p: False)
    assert removed == [] and new_text == '{"workspaces": ["a",'
    assert any("JSON 解析失败" in r.getMessage() for r in caplog.records), caplog.text
