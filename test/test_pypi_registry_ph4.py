"""P-H4a：pypi_registry 确定性解析 + python 脚手架 driver 接线。

锁的命题（每条都要答「哪个突变更让它红」）：
  · PEP 503 归一化（Flask/flask 同一个包；大小写比较会被同名包骗）；
  · 工程清单中间证据层（零网络）：解析对/根优先/一级子目录/失败可辨（硬检查④）；
  · 显式版本=主张非证据（P-C2 平移）：==查无确证丢弃、==预发布不误杀（全量集核验）、
    区间无可满足稳定版丢弃、不可达 fail-open（离线跑一次清空所有显式依赖=更坏）；
  · extras 不静默丢（`flask[async]` → 写出仍带 [async]）；
  · 内部模块绝不送 PyPI（同名公网包误解析）；python 内部依赖【不物化】但留契约+WARNING；
  · 接线：裸名在 registry 全程 boom 下仍进权威 pyproject 模板 ⇒ 唯一可能是清单层到达。
"""
from __future__ import annotations

import json
import logging

import pytest

from swarm.brain import pypi_registry as pr


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """默认离线：单测绝不真联网。需要网络的用例各自打桩 _http_get。"""
    monkeypatch.setenv("SWARM_PYPI_LOOKUP", "1")  # 开关开，但 _http_get 被打桩
    monkeypatch.setattr(pr, "_http_get", lambda url: None)


def _pypi_doc(*versions: str) -> str:
    return json.dumps({"info": {"name": "x", "version": versions[-1] if versions else ""},
                       "releases": {v: [] for v in versions}})


# ── 归一化与解析 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expect", [
    ("Flask", "flask"), ("foo_bar.baz", "foo-bar-baz"),
    ("SQLAlchemy", "sqlalchemy"), ("zope.interface", "zope-interface"),
])
def test_normalize_name_pep503(raw, expect):
    assert pr.normalize_name(raw) == expect


@pytest.mark.parametrize("raw,name,extras,spec", [
    ("flask", "flask", "", ""),
    ("flask>=2.0", "flask", "", ">=2.0"),
    ("flask[async]>=2.0", "flask", "[async]", ">=2.0"),
    # ★hunter R2 M-2★ marker 是条件语义载体，保留（剥了=条件依赖改写成无条件）
    ("uvicorn[standard]==0.29.0; python_version>'3.8'",
     "uvicorn", "[standard]", "==0.29.0 ; python_version>'3.8'"),
    # ★cr R2 HIGH-1★ http 前缀包名族（httpx/httpcore 是 PyPI 头部主流包）绝非 URL 行
    ("httpx", "httpx", "", ""),
    ("httpcore==2.0", "httpcore", "", "==2.0"),
])
def test_parse_dep_text_forms(raw, name, extras, spec):
    assert pr._parse_dep_text(raw) == (name, extras, spec)


@pytest.mark.parametrize("raw", ["-r base.txt", "-e .", "# comment", "git+https://x/y.git",
                                 "https://example.com/pkg.whl", ""])
def test_parse_dep_text_unparseable_returns_none(raw):
    assert pr._parse_dep_text(raw) is None


# ── 工程清单中间证据层 ────────────────────────────────────────────────

def test_manifest_specs_pyproject_and_requirements(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["flask>=3.0", "requests[security]"]\n')
    (tmp_path / "requirements.txt").write_text(
        "# 注释\n-r other.txt\nsqlalchemy==2.0.30\nuvicorn[standard]>=0.29\n")
    specs = pr.project_manifest_specs(str(tmp_path))
    assert specs["flask"] == ("", ">=3.0")
    assert specs["sqlalchemy"] == ("", "==2.0.30")
    assert specs["uvicorn"] == ("[standard]", ">=0.29")
    assert specs["requests"] == ("[security]", "")  # 裸声明+extras = 有证据、无约束（与「没声明」可辨）
    assert "-r" not in specs and "other.txt" not in specs


def test_manifest_specs_root_wins_over_subdir(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["flask>=3.0"]\n')
    sub = tmp_path / "web"
    sub.mkdir()
    (sub / "pyproject.toml").write_text('[project]\ndependencies = ["flask>=2.0"]\n')
    specs = pr.project_manifest_specs(str(tmp_path))
    assert specs["flask"] == ("", ">=3.0")  # 根优先（setdefault 语义）


def test_manifest_specs_malformed_pyproject_warns(tmp_path, caplog):
    (tmp_path / "pyproject.toml").write_text("[project\ntoml 坏掉")
    with caplog.at_level(logging.WARNING, logger="swarm.brain.pypi_registry"):
        specs = pr.project_manifest_specs(str(tmp_path))
    assert specs == {}
    assert any(r.levelno == logging.WARNING and "解析失败" in r.getMessage()
               for r in caplog.records), "解析失败必须可辨——自吞=「失败」与「真没有」塌成一个值"


def test_manifest_specs_gated_by_lookup(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["flask>=3.0"]\n')
    monkeypatch.setenv("SWARM_PYPI_LOOKUP", "0")
    assert pr.project_manifest_specs(str(tmp_path)) == {}


# ── registry 解析 ────────────────────────────────────────────────────

def test_registry_latest_filters_prerelease_and_orders_pep440(monkeypatch):
    monkeypatch.setattr(pr, "_http_get",
                        lambda url: _pypi_doc("1.2.0", "1.10.0", "2.0.0rc1", "1.9.0"))
    assert pr.registry_latest_version("x") == "1.10.0"   # PEP440 序（1.10>1.9）+ 排预发布


def test_registry_latest_none_when_only_prerelease(monkeypatch):
    monkeypatch.setattr(pr, "_http_get", lambda url: _pypi_doc("1.0.0a1", "0.9.0b2"))
    assert pr.registry_latest_version("x") is None


def test_registry_latest_offline_returns_none():
    assert pr.registry_latest_version("x") is None       # autouse 打桩 _http_get→None


def test_registry_versions_includes_prerelease(monkeypatch):
    monkeypatch.setattr(pr, "_http_get", lambda url: _pypi_doc("1.0.0", "1.1.0b1"))
    vs = pr.registry_versions("x")
    assert vs == frozenset({"1.0.0", "1.1.0b1"})
    monkeypatch.setattr(pr, "_http_get", lambda url: None)
    assert pr.registry_versions("x") is None             # 不可达与空集可辨


# ── resolve_pypi_deps：显式版本是主张不是证据 ─────────────────────────

def test_resolve_internal_never_hits_registry(monkeypatch):
    def boom(url):
        raise AssertionError("内部模块被送去 PyPI 了——同名公网包误解析通道")
    monkeypatch.setattr(pr, "_http_get", boom)
    kept, internal, dropped = pr.resolve_pypi_deps(
        ["auth-core"], internal_modules={"auth-core"}, project_path=None)
    assert kept == [] and internal == ["auth-core"] and dropped == []


def test_resolve_explicit_pin_existing_kept(monkeypatch):
    monkeypatch.setattr(pr, "_http_get", lambda url: _pypi_doc("3.0.0", "3.0.1"))
    kept, _, dropped = pr.resolve_pypi_deps(["flask==3.0.1"])
    assert [k.spec for k in kept] == ["==3.0.1"] and dropped == []


def test_resolve_explicit_pin_prerelease_not_killed(monkeypatch):
    """P-C2「版本集含预发布」：==1.1.0b1 真存在，按稳定版集判会误杀真依赖。"""
    monkeypatch.setattr(pr, "_http_get", lambda url: _pypi_doc("1.0.0", "1.1.0b1"))
    kept, _, dropped = pr.resolve_pypi_deps(["x==1.1.0b1"])
    assert [k.spec for k in kept] == ["==1.1.0b1"] and dropped == []


def test_resolve_explicit_pin_hallucinated_dropped(monkeypatch, caplog):
    monkeypatch.setattr(pr, "_http_get", lambda url: _pypi_doc("3.0.0", "3.0.1"))
    with caplog.at_level(logging.WARNING, logger="swarm.brain.pypi_registry"):
        kept, _, dropped = pr.resolve_pypi_deps(["flask==99.0.0"])
    assert kept == [] and dropped == ["flask==99.0.0"]
    # hunter#5：探针加限定词收窄——裸「确证不存在」会被未来同措辞的别条消息撞名假过
    assert any("钉版" in r.getMessage() and "确证不存在" in r.getMessage()
               for r in caplog.records)


def test_resolve_explicit_range_unsatisfiable_dropped(monkeypatch):
    monkeypatch.setattr(pr, "_http_get", lambda url: _pypi_doc("1.0.0", "1.5.0"))
    kept, _, dropped = pr.resolve_pypi_deps(["x>=9.0"])
    assert kept == [] and dropped == ["x>=9.0"]


def test_resolve_explicit_unreachable_failopen_kept():
    kept, _, dropped = pr.resolve_pypi_deps(["flask==99.0.0"])   # _http_get→None
    assert [k.verified for k in kept] == ["registry_unreachable"] and dropped == []


def test_resolve_explicit_range_unreachable_failopen_kept():
    """区间臂的不可达 fail-open 是【另一条】分支——钉版臂的锁压不到它（harness 实证）。"""
    kept, _, dropped = pr.resolve_pypi_deps(["x>=9.0"])          # _http_get→None
    assert [k.verified for k in kept] == ["registry_unreachable"] and dropped == []


def test_resolve_invalid_spec_kept_unparsed(monkeypatch):
    monkeypatch.setattr(pr, "_http_get", lambda url: _pypi_doc("1.0.0"))
    kept, _, dropped = pr.resolve_pypi_deps(["x>>1.0"])   # `>>` 是非法算符（不判原样保留）
    assert [k.verified for k in kept] == ["spec_unparsed"] and dropped == []


def test_resolve_bare_from_manifest_registry_boom(tmp_path, monkeypatch):
    """零网络中间层接线：registry 全程 boom，清单声明仍答 ⇒ 唯一可能是 manifest 层。"""
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["flask>=3.0"]\n')
    def boom(url):
        raise AssertionError("清单已答 ⇒ PyPI 不该被咨询")
    monkeypatch.setattr(pr, "_http_get", boom)
    kept, _, dropped = pr.resolve_pypi_deps(["flask"], project_path=str(tmp_path))
    assert [(k.name, k.spec, k.source) for k in kept] == [("flask", ">=3.0", "project_manifest")]
    assert dropped == []


def test_resolve_bare_from_registry_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "_http_get", lambda url: _pypi_doc("3.0.0", "3.0.1"))
    kept, _, dropped = pr.resolve_pypi_deps(["flask"], project_path=str(tmp_path))
    assert [(k.name, k.spec) for k in kept] == [("flask", ">=3.0.1")]


def test_resolve_bare_unknown_dropped(tmp_path):
    kept, _, dropped = pr.resolve_pypi_deps(["nonexistent-pkg-xyz"], project_path=str(tmp_path))
    assert kept == [] and dropped == ["nonexistent-pkg-xyz"]


def test_resolve_extras_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "_http_get", lambda url: _pypi_doc("0.29.0", "0.30.0"))
    kept, _, _ = pr.resolve_pypi_deps(["uvicorn[standard]"], project_path=str(tmp_path))
    assert kept[0].extras == "[standard]"
    # 渲染形状：name+extras+spec（静默丢 extras = 把「装异步支持」改成「没装」）
    assert f"{kept[0].name}{kept[0].extras}{kept[0].spec}" == "uvicorn[standard]>=0.30.0"


# ── driver 接线锁（经真实调用链） ─────────────────────────────────────

def _py_plan():
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan
    plan = TaskPlan(
        subtasks=[SubTask(id="st-1", description="task st-1",
                          difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=[], create_files=["svc/auth/main.py"]))],
        parallel_groups=[["st-1"]])
    plan.shared_contract = {"dependencies": [
        {"module": "auth", "artifacts": ["flask"]}]}      # 裸名（无版本）
    return plan


def test_ph4_python_manifest_spec_reaches_scaffold_via_real_caller(tmp_path, monkeypatch):
    """★接线锁（血规 10 第一条）★ registry 全程 boom；裸名仍进权威 pyproject 模板 ⇒
    唯一可能是工程清单层经真实调用链到达。把 driver 的 `project_path=project_path`
    改成 None → 本条必红（裸名被 drop ⇒ 模板里没有 flask 行）。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["flask>=3.0", "uvicorn[standard]>=0.29"]\n')
    def boom(url):
        raise AssertionError("清单已答 ⇒ PyPI 不该被咨询（接线断了才会走到这里）")
    monkeypatch.setattr(pr, "_http_get", boom)
    plan = _py_plan()
    plan.shared_contract["dependencies"][0]["artifacts"] = ["flask", "uvicorn[standard]"]
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    assert injected, "夹具没走到 python driver（零注入 ⇒ 这条测试什么也没证明）"
    scaffold = next(st for st in plan.subtasks if st.id == "st-scaffold-auth")
    assert '"flask>=3.0"' in scaffold.description, \
        "清单声明没进权威 pyproject 模板 ⇒ P-H4 证据层在生产链路上是断的"
    assert 'name = "auth"' in scaffold.description
    # extras 渲染锁：契约裸名 + 清单声明带 extras ⇒ 模板必须保留 [standard]
    # （静默丢 extras = 把「装 standard 支持」改成「没装」——换语义不是换写法）
    assert '"uvicorn[standard]>=0.29"' in scaffold.description
    # requires-python 不猜锁：根清单没声明 ⇒ 模板【省略】该字段（血规 2：下界绝不猜）
    assert "requires-python" not in scaffold.description


def test_ph4_python_internal_dep_not_materialized_but_kept_in_contract(tmp_path, monkeypatch,
                                                                       caplog):
    """内部模块：不物化进 pyproject（无确定性相对引用机制，实测 file: 按 cwd 解析）——
    但【留契约】（不剪）+ WARNING 可辨。绝不送 PyPI 误解析同名公网包。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
    def boom(url):
        raise AssertionError("内部模块被送去 PyPI 了")
    monkeypatch.setattr(pr, "_http_get", boom)
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan
    plan = TaskPlan(
        subtasks=[SubTask(id="st-1", description="t", difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=[], create_files=["svc/web/app.py"])),
                  SubTask(id="st-2", description="t", difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=[], create_files=["svc/core/db.py"]))],
        parallel_groups=[["st-1"], ["st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "web", "artifacts": ["core"]},
        {"module": "core", "artifacts": []}]}
    with caplog.at_level(logging.WARNING, logger="swarm.brain.contract_utils"):
        injected = inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    web_scaffold = next((st for st in plan.subtasks if st.id == "st-scaffold-web"), None)
    assert web_scaffold is not None, "web 模块零第三方依赖也该有清单出口（T5/T6 同律）" \
        if web_scaffold is None else ""
    # 内部依赖不物化：模板里绝没有 core 的 pip 依赖行
    assert '"core' not in web_scaffold.description
    # 但契约里保留（诚实：依赖关系真实存在）
    web_entry = next(e for e in plan.shared_contract["dependencies"] if e["module"] == "web")
    assert "core" in web_entry["artifacts"]
    # hunter#5：探针带机制编号收窄——裸「不物化」会被未来同措辞的别条消息撞名假过
    assert any("#31-P2d" in r.getMessage() and "不物化" in r.getMessage()
               for r in caplog.records)
    assert injected  # 夹具有效性


# ── P-H4a 对抗双复核 R1 整改的判别锁 ────────────────────────────────

def test_parse_dep_text_direct_ref_preserves_url():
    """★cr R-3★ direct reference 的 URL 绝不剥——剥了只剩名字=把项目钉死的来源静默换成
    公网版。specifier 位原样带 ` @ url`（下游 SpecifierSet 必判非法 → 走「不判原样
    保留」通道，渲染回清单仍是合法 PEP 508）。"""
    assert pr._parse_dep_text("requests @ https://x/r-2.0-py3-none-any.whl") == \
        ("requests", "", " @ https://x/r-2.0-py3-none-any.whl")
    assert pr._parse_dep_text("requests[security] @ https://x/r.whl ; python_version>'3.8'") == \
        ("requests", "[security]", " @ https://x/r.whl ; python_version>'3.8'")
    # URL 片段的 # 前无空格——不得被行内注释剥离误伤
    assert pr._parse_dep_text("pkg @ https://x/r.whl#sha256=abc")[2].endswith("#sha256=abc")


def test_parse_dep_text_inline_comment_stripped():
    """★cr R-4★ requirements.txt 行内注释不剥会污染 specifier → InvalidSpecifier →
    spec_unparsed 假降级（真约束白白进「不判」通道）。"""
    assert pr._parse_dep_text("flask>=2.0  # web 框架") == ("flask", "", ">=2.0")
    assert pr._parse_dep_text("requests  # 钉死") == ("requests", "", "")


def test_resolve_direct_ref_passthrough_never_consults_registry(monkeypatch):
    """★cr R-3 下游★ 契约 direct reference 原样保留（URL 是完整依赖声明，剥了=换来源）——
    走 spec_unparsed 不判通道，绝不送 registry 核验（URL 内容本就无法核验）。"""
    def boom(url):
        raise AssertionError("direct reference 不该查 registry")
    monkeypatch.setattr(pr, "_http_get", boom)
    kept, internal, dropped = pr.resolve_pypi_deps(["requests @ https://x/r-2.0.whl"])
    assert not dropped and not internal
    assert f"{kept[0].name}{kept[0].extras}{kept[0].spec}" == "requests @ https://x/r-2.0.whl"
    assert kept[0].verified == "spec_unparsed"


def test_resolve_bare_adopts_manifest_direct_ref(tmp_path, monkeypatch):
    """★cr R-3 清单侧★ 工程清单用 direct reference 钉死来源时，裸名采用【整条声明】
    （URL 保留），不是剥成裸名再去公网换版本。"""
    (tmp_path / "requirements.txt").write_text("requests @ https://internal.mirror/r-2.31.whl\n")
    def boom(url):
        raise AssertionError("清单已答 ⇒ 不该查 registry")
    monkeypatch.setattr(pr, "_http_get", boom)
    kept, _, dropped = pr.resolve_pypi_deps(["requests"], project_path=str(tmp_path))
    assert not dropped
    assert f"{kept[0].name}{kept[0].spec}" == "requests @ https://internal.mirror/r-2.31.whl"
    assert kept[0].source == "project_manifest"


def test_resolve_range_prerelease_bound_allows_prerelease(monkeypatch):
    """★cr R-5★ pip 语义：spec 含预发布界=项目显式允许预发布 → `>=1.0b1` 对只有预发布
    的真包必须可满足（只对稳定版集判会把它冤杀成幻觉）。"""
    monkeypatch.setattr(pr, "_http_get", lambda url: _pypi_doc("1.0b1", "1.0b2"))
    kept, _, dropped = pr.resolve_pypi_deps(["x>=1.0b1"])
    assert not dropped and kept[0].spec == ">=1.0b1"


def test_resolve_range_stable_bound_excludes_prerelease(monkeypatch):
    """★cr R-5 反向★ `>=2.0` 未显式允许预发布 → 只有 2.0rc1 时仍是「无可满足发布」
    （与 pip 默认不装预发布一致）——防 R-5 修法把门槛放宽过头。"""
    monkeypatch.setattr(pr, "_http_get", lambda url: _pypi_doc("2.0rc1"))
    kept, _, dropped = pr.resolve_pypi_deps(["x>=2.0"])
    assert not kept and dropped


def test_manifest_specs_setup_py_install_requires(tmp_path, monkeypatch):
    """★cr R-6★ setup.py 的 install_requires 字面量列表也是清单声明证据
    （动态拼接拿不到=诚实缺席，stack_detect 对 python_requires 同先例）。
    ★cr R2 MEDIUM-1★ 夹具必须含【中间 extras + 其后还有条目】形——非贪婪正则在
    extras 的 `']'` 提前截断会让 extras 条目及其后全部条目静默蒸发。"""
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\nsetup(\n    name='demo',\n"
        "    install_requires=['flask>=2.0', 'requests[security]', 'uvicorn'],\n)\n")
    def boom(url):
        raise AssertionError("清单已答 ⇒ 不该查 registry")
    monkeypatch.setattr(pr, "_http_get", boom)
    kept, _, dropped = pr.resolve_pypi_deps(
        ["flask", "requests", "uvicorn"], project_path=str(tmp_path))
    got = {k.name: (k.extras, k.spec) for k in kept}
    assert got == {"flask": ("", ">=2.0"), "requests": ("[security]", ""),
                   "uvicorn": ("", "")}, got
    assert not dropped


def test_ph4_python_contract_label_stays_internal_when_disk_name_differs(tmp_path, monkeypatch,
                                                                         caplog):
    """★P-H4a 复核 hunter#2★ 契约 artifact 写的是【模块标签】，磁盘 [project].name 是
    另一个名字（auth vs my-auth-service）——只认磁盘名会把内部模块送上 PyPI 误解析
    同名公网包。标签归一化后同样算内部（go driver `internal_paths.get` 同律）。
    registry boom ⇒ 走到 PyPI 即红。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
    svc = tmp_path / "svc" / "auth"
    svc.mkdir(parents=True)
    (svc / "pyproject.toml").write_text('[project]\nname = "my-auth-service"\n')
    def boom(url):
        raise AssertionError("内部模块标签被送去 PyPI 了（hunter#2 复活）")
    monkeypatch.setattr(pr, "_http_get", boom)
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan
    plan = TaskPlan(
        subtasks=[SubTask(id="st-1", description="t", difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=[], create_files=["svc/web/app.py"])),
                  SubTask(id="st-2", description="t", difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=[], create_files=["svc/auth/main.py"]))],
        parallel_groups=[["st-1"], ["st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "web", "artifacts": ["auth"]},      # 契约写的是【标签】不是磁盘名
        {"module": "auth", "artifacts": []}]}
    with caplog.at_level(logging.WARNING, logger="swarm.brain.contract_utils"):
        injected = inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    web_scaffold = next(st for st in plan.subtasks if st.id == "st-scaffold-web")
    assert '"auth"' not in web_scaffold.description
    assert any("不物化" in r.getMessage() and "auth" in r.getMessage()
               for r in caplog.records)
    assert injected  # 夹具有效性


def test_ph4_unresolved_contract_module_warns_not_silent(tmp_path, caplog):
    """★P-H4a 复核 hunter#4（硬检查④）★ 契约模块解析不出物理落点（src-layout 等未支持
    形态）→ 不注入脚手架【必须有 WARNING】——「没注入」必须与「不需要注入」机读可辨，
    静默=这层死了没人知道。src-layout 物理落点语义本身登记为诚实边界（27 号文 P-H4 剩余）。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
    plan = _py_plan()
    plan.shared_contract["dependencies"].append({"module": "ghost", "artifacts": ["flask"]})
    with caplog.at_level(logging.WARNING, logger="swarm.brain.contract_utils"):
        injected = inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    assert any("无确定物理落点" in r.getMessage() and "ghost" in r.getMessage()
               for r in caplog.records), "未解析模块零信号 = 「没注入」与「不需要注入」不可辨"
    assert injected  # auth 正常注入（部分未解析不拖死全局 + 夹具有效性）


# ── R2 双复核 findings 整改的判别锁 ─────────────────────────────────

def test_resolve_explicit_pin_pep440_equivalent_kept(monkeypatch):
    """★hunter R2 M-1 / cr R2 LOW-1★ 钉版判等必须 PEP440 语义：发布键是上传字面量
    `2.0`，契约钉 `==2.0.0`（pip 语义相等）——字面相等会把真钉版冤杀成「确证不存在」。"""
    monkeypatch.setattr(pr, "_http_get", lambda url: _pypi_doc("2.0"))
    kept, _, dropped = pr.resolve_pypi_deps(["x==2.0.0"])
    assert not dropped and kept[0].spec == "==2.0.0"


def test_resolve_marker_preserved_through_verification(monkeypatch):
    """★hunter R2 M-2★ marker 随 spec 整段保留：钉版+marker 核验用 marker 前的版本段、
    渲染整段写回（剥了=把 `pywin32; sys_platform=='win32'` 改写成无条件依赖）。"""
    monkeypatch.setattr(pr, "_http_get", lambda url: _pypi_doc("2.0"))
    kept, _, dropped = pr.resolve_pypi_deps(["x==2.0 ; sys_platform=='win32'"])
    assert not dropped and kept[0].spec == "==2.0 ; sys_platform=='win32'"


def test_resolve_bare_with_marker_keeps_marker_on_floor(monkeypatch):
    """★hunter R2 M-2 裸名臂★ 裸名+marker → registry 下限解析后 marker 不得丢。"""
    monkeypatch.setattr(pr, "_http_get", lambda url: _pypi_doc("3.1"))
    kept, _, dropped = pr.resolve_pypi_deps(["x ; python_version<'3.11'"])
    assert not dropped and kept[0].spec == ">=3.1 ; python_version<'3.11'"


def test_resolve_manifest_adoption_keeps_contract_marker(tmp_path):
    """★hunter R3 L-a★ 契约裸名+marker、清单已声明同名包：采用清单 spec 时契约 marker
    必须拼上——静默丢=把条件依赖改写成无条件。"""
    (tmp_path / "requirements.txt").write_text("x>=2.0\n")
    kept, _, dropped = pr.resolve_pypi_deps(["x ; python_version<'3.11'"],
                                            project_path=str(tmp_path))
    assert not dropped and kept[0].spec == ">=2.0 ; python_version<'3.11'"


def test_render_pyproject_escapes_double_quoted_markers(tmp_path):
    """★cr R3 HIGH★ PEP 508 marker 的规范形态带【双引号】（`python_version<"3.10"`，
    requirements 世界最常见写法）——不转义插值=「确定性生成，原样写入」的权威模板
    产出非法 TOML（worker 照写 → pip 无法解析）。清单采用臂（零网络主流链路）端到端
    锁：render 产物必须 tomllib 可解析且 dependencies 字面值逐字等于原 spec。
    突变判据：删掉 `_toml_escape` 的转义体即红。"""
    import tomllib
    from swarm.brain.contract_utils import _py_dep_block, _render_pyproject_toml
    (tmp_path / "requirements.txt").write_text('importlib-metadata; python_version<"3.10"\n')
    kept, _, dropped = pr.resolve_pypi_deps(["importlib-metadata"],
                                            project_path=str(tmp_path))
    assert not dropped and kept[0].spec == '; python_version<"3.10"'
    # CREATE 模板臂
    text = _render_pyproject_toml("demo", kept, requires_python=">=3.9")
    parsed = tomllib.loads(text)
    assert parsed["project"]["dependencies"] == ['importlib-metadata; python_version<"3.10"']
    # MODIFY 片段臂：转义形态必须在片段里（未转义的双引号会破并入目标的 TOML）
    block = _py_dep_block("pkg/pyproject.toml", kept, "demo", exists=True)
    assert 'python_version<\\"3.10\\"' in block, "MODIFY 片段未转义双引号 marker"


def test_ph4_npm_unresolved_internal_label_held_from_registry(tmp_path, monkeypatch, caplog):
    """★hunter R2 H-1 npm 臂★ 契约声明了但解析不出物理落点的模块【也是内部模块】——
    绝不送公网 registry（同名包会被物化进权威模板），也不物化 workspace:*（目标不
    存在=必炸清单），留契约 + WARNING。registry boom ⇒ 送到即红。"""
    import swarm.brain.npm_registry as nr
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    (tmp_path / "package.json").write_text('{"name":"demo","workspaces":["packages/*"]}')
    def boom(url):
        raise AssertionError("未解析内部模块被送去公网 registry 了（H-1 复活）")
    monkeypatch.setattr(nr, "_http_get", boom)
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan
    plan = TaskPlan(
        subtasks=[SubTask(id="st-1", description="t", difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=[], create_files=["packages/web/index.js"]))],
        parallel_groups=[["st-1"]])
    plan.shared_contract = {"dependencies": [
        {"module": "web", "artifacts": ["ghost"]},
        {"module": "ghost", "artifacts": []}]}
    import logging
    with caplog.at_level(logging.WARNING, logger="swarm.brain.contract_utils"):
        injected = inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    web_scaffold = next(st for st in plan.subtasks if st.id == "st-scaffold-web")
    assert "ghost" not in web_scaffold.description, "未解析内部模块被物化进权威 package.json"
    web_entry = next(e for e in plan.shared_contract["dependencies"] if e["module"] == "web")
    assert "ghost" in web_entry["artifacts"], "内部依赖边被从契约抹掉（fail-honest=留契约）"
    assert any("无物理落点" in r.getMessage() and "ghost" in r.getMessage()
               for r in caplog.records)
    assert injected  # 夹具有效性


def test_ph4_go_unresolved_internal_label_held_from_proxy(tmp_path, monkeypatch, caplog):
    """★hunter R2 H-1 go 臂★ 同 npm 臂：不送 proxy、不生成 replace（无落点=臆造路径）、
    留契约 + WARNING。"""
    import swarm.brain.go_registry as gr
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    (tmp_path / "go.mod").write_text("module example.com/demo\n\ngo 1.21\n")
    def boom(url):
        raise AssertionError("未解析内部模块被送去公网 proxy 了（H-1 复活）")
    monkeypatch.setattr(gr, "_http_get", boom)
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan
    plan = TaskPlan(
        subtasks=[SubTask(id="st-1", description="t", difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=[], create_files=["svc/web/main.go"]))],
        parallel_groups=[["st-1"]])
    plan.shared_contract = {"dependencies": [
        {"module": "web", "artifacts": ["ghost"]},
        {"module": "ghost", "artifacts": []}]}
    import logging
    with caplog.at_level(logging.WARNING, logger="swarm.brain.contract_utils"):
        injected = inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    web_scaffold = next(st for st in plan.subtasks if st.id == "st-scaffold-web")
    assert "ghost" not in web_scaffold.description, "未解析内部模块被物化进权威 go.mod"
    web_entry = next(e for e in plan.shared_contract["dependencies"] if e["module"] == "web")
    assert "ghost" in web_entry["artifacts"]
    assert any("无物理落点" in r.getMessage() and "ghost" in r.getMessage()
               for r in caplog.records)
    assert injected  # 夹具有效性


def test_ph4_python_unresolved_internal_label_never_hits_pypi(tmp_path, monkeypatch, caplog):
    """★hunter R2 H-1 python 臂（本尊实证形态）★ 公网有同名包也不许进模板——
    契约标签并入内部集后走「不物化+留契约+WARNING」通道。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
    # 公网【有】同名包——widen 缺失时这个包会被解析并物化（H-1 实证形态）
    monkeypatch.setattr(pr, "_http_get", lambda url: _pypi_doc("9.9.9"))
    import os
    monkeypatch.setenv("SWARM_PYPI_LOOKUP", "1")
    plan = _py_plan()
    plan.shared_contract["dependencies"] = [
        {"module": "auth", "artifacts": ["ghost"]},
        {"module": "ghost", "artifacts": []}]
    import logging
    with caplog.at_level(logging.WARNING, logger="swarm.brain.contract_utils"):
        injected = inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    scaf = next(st for st in plan.subtasks if st.id == "st-scaffold-auth")
    assert '"ghost' not in scaf.description, "公网同名包被物化进权威 pyproject（H-1 复活）"
    auth_entry = next(e for e in plan.shared_contract["dependencies"] if e["module"] == "auth")
    assert "ghost" in auth_entry["artifacts"]
    assert injected  # 夹具有效性


def test_dedupe_module_scaffolds_python_pure_merged_mixed_untouched():
    from swarm.brain.contract_utils import dedupe_module_scaffolds
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

    def _mk(sid, creates):
        return SubTask(id=sid, description=f"t {sid}", difficulty=SubTaskDifficulty.TRIVIAL,
                       scope=FileScope(writable=[], create_files=creates))

    s1 = _mk("st-scaffold-a", ["svc/pyproject.toml"])
    s2 = _mk("st-9", ["svc/pyproject.toml"])            # 纯清单重复（R58-3 认领撞注入器）
    m1 = _mk("st-10", ["web/pyproject.toml", "web/app.py"])
    m2 = _mk("st-11", ["web/pyproject.toml", "web/views.py"])
    plan = TaskPlan(subtasks=[s1, s2, m1, m2], parallel_groups=[])
    plan.shared_contract = {}
    merged = dedupe_module_scaffolds(plan)
    assert merged == 1
    ids = [st.id for st in plan.subtasks]
    assert "st-9" not in ids and "st-scaffold-a" in ids
    assert "st-10" in ids and "st-11" in ids, "混合代码子任务被当脚手架并掉 = 丢真工作"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
