"""#29-2 W-6 — 依赖合法性闸把 pyproject.toml 的【TOML 键】当成依赖包名，剪掉工程元数据。

缺陷：`PythonDriver._PKG_RE` 配 `re.MULTILINE` 直接扫 pyproject.toml 全文，
把「键 = 值」当「包名 + 版本约束」。实测标准 PEP 621 文件解析出 9 条"依赖"
全是 TOML 键（name / version / description / requires-python / build-backend /
dependencies / dev / line-length / requires），而**真依赖一条没解析到**。

后果非确定性且严重：哪些键被判 prune 取决于该键名在 PyPI 是否恰好是真包
（实测 `requires`(26 版本) / `version`(2) / `dependencies`(55) 存活；
`name` / `description` / `build-backend` / `requires-python` / `dev` /
`line-length` 查无 → **判 prune 删掉**）。删掉 `[project].name` 与
`build-backend` 后工程无法构建；某些排布下删除还会切断字符串字面量，令文件
不再是合法 TOML —— 而这道闸的存在理由（`dep_legality.py:31-33`：
「坏坐标 = manifest 解析期崩塌会连坐整个工作区」）**正是它自己制造的故障**。

判据的可达性已取证（不是纸面推演）：
  · `python_registry_versions_list` 是 **Swarm 宿主进程**里的 `urllib.urlopen`
    直连 pypi.org（不在沙箱内）—— 本机实测可达，故 prune 判定真的会发生；
  · 破坏面只在 pyproject 臂。requirements-only 工程的 `workspace_members` 为空
    （`_members()` 只从 pyproject 取根名），`classify` 首行 fail-open 保险
    （`dep_legality.py:405`）令整闸不动 —— 这也是本缺陷此前没在 E2E 上暴露的原因。

治法：pyproject 走 `tomllib` 真解析，只取 `[project].dependencies` 与
`optional-dependencies` 数组元素，按 PEP 508 剥包名；行正则**只许**用于
requirements.txt；解析不出 → 返空 + WARNING（fail-honest，绝不臆造）。
"""

from __future__ import annotations

import tomllib

import pytest
from swarm.worker.dep_legality import DRIVERS, enforce

DRV = DRIVERS["python"]

PEP621 = '''[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "myapp"
version = "0.1.0"
description = "A sample application, with a comma"
requires-python = ">=3.11"
dependencies = [
    "flask>=3.0",
    "requests>=2.31,<3",
    "pydantic[email]>=2.5",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "ruff"]

[tool.ruff]
line-length = 100
'''

# 这些是 pyproject 里的 TOML 键，绝不是依赖包名
TOML_KEYS = ("name", "version", "description", "requires-python",
             "build-backend", "requires", "dependencies", "dev", "line-length")

REAL_DEPS = {"flask", "requests", "pydantic", "pytest", "ruff"}


def _registry(known: set[str]):
    """假 registry：known 里的返回版本列表，其余返 []（＝确证查无 → prune）。"""
    return lambda _ns, name: (["1.0.0"] if name in known else [])


# ══════════════════════════════════════════════════════════
# A) 缺陷本体：解析结果必须是真依赖，不是 TOML 键
# ══════════════════════════════════════════════════════════

def test_pyproject_parse_yields_real_deps_only():
    """★核心：解析出的名字集合 == 真依赖集合（修复前是 9 个 TOML 键、真依赖 0 条）★"""
    got = {d["name"] for d in DRV.parse_deps(PEP621)}
    assert got == REAL_DEPS, (
        f"解析结果与真依赖不符\n  多出(TOML 键被当包名): {sorted(got - REAL_DEPS)}\n"
        f"  漏掉(真依赖没解析到): {sorted(REAL_DEPS - got)}")


@pytest.mark.parametrize("key", TOML_KEYS)
def test_no_toml_key_is_ever_parsed_as_dependency(key):
    """逐个 TOML 键断言不被当依赖 —— 参数化后哪个键回归立刻可读。"""
    names = {d["name"] for d in DRV.parse_deps(PEP621)}
    assert key not in names, f"TOML 键 {key!r} 被当成依赖包名"


def test_parsed_versions_are_pep508_constraints():
    """版本位取的是 PEP 508 约束，不是 TOML 的 `= 值`（修复前是 `= \"myapp\"` 这种）。"""
    by_name = {d["name"]: d["version"] for d in DRV.parse_deps(PEP621)}
    assert by_name["flask"] == ">=3.0"
    assert by_name["requests"] == ">=2.31,<3"
    assert by_name["pydantic"] == ">=2.5", "extras 未被剥离"
    assert by_name["ruff"] is None, "无约束应为 None"
    for name, ver in by_name.items():
        assert not (ver or "").startswith("="), f"{name}: 版本位是 TOML 赋值残留: {ver!r}"


def test_blocks_are_uniquely_locatable_in_source():
    """block 必须能在原文里【唯一】定位——否则 enforce 的 remove/rewrite 会命中别处
    （那比不解析更坏：静默改错地方）。"""
    for d in DRV.parse_deps(PEP621):
        assert PEP621.count(d["block"]) == 1, (
            f"{d['name']}: block {d['block']!r} 在原文里出现 {PEP621.count(d['block'])} 次")


def test_optional_dependencies_are_included():
    """optional-dependencies 也要纳入（它们同样进构建，坏坐标同样炸）。"""
    names = {d["name"] for d in DRV.parse_deps(PEP621)}
    assert {"pytest", "ruff"} <= names, "optional-dependencies 未被解析"


# 同一 spec 字面量出现两次：一次在 dependencies、一次在 optional-dependencies。
# TOML 合法（两个不同数组），但 `"phantom-xyz789"` 这个**字符串在原文里出现 2 次**
# ⇒ 用它当 block 定位就无法确定改的是哪一处。
DUP_LITERAL = '''[project]
name = "myapp"
dependencies = ["phantom-xyz789", "flask>=3.0"]

[project.optional-dependencies]
dev = ["phantom-xyz789"]
'''


def test_ambiguous_block_is_dropped_not_guessed():
    """★定位不唯一 → 丢弃该条（fail-honest），绝不赌改哪一处★

    remove/rewrite 靠 block 字符串在原文里定位。字面量重复时 `replace(..., 1)` 会
    命中【第一处】——那可能不是判定所依据的那一处。改错地方比不改坏得多：
    它会静默删掉另一个 group 里的合法声明。
    """
    deps = DRV.parse_deps(DUP_LITERAL)
    names = [d["name"] for d in deps]
    assert "phantom-xyz789" not in names, (
        f"字面量在原文出现 2 次仍被处置（会改错地方）: {names}")
    assert "flask" in names, "唯一定位的依赖不该被牵连丢弃"


def test_ambiguous_literal_survives_enforce_untouched():
    """接线闭环：定位不唯一的条目在 enforce 全链上必须一字不动。

    registry 故意只认 flask ⇒ phantom 若被处置就会被 prune。
    """
    out, actions = _enforce(DUP_LITERAL, {"flask"})
    assert actions == [], f"定位不唯一的条目被处置了: {actions}"
    assert out == DUP_LITERAL, "文件被改动"
    parsed = tomllib.loads(out)
    assert parsed["project"]["optional-dependencies"]["dev"] == ["phantom-xyz789"], \
        "另一个 group 里的声明被误删（这正是赌错地方的后果）"


# url/vcs 依赖：PEP 621 合法形态（`pkg @ git+https://...`），但 PyPI 里没有这个名字
# ⇒ 探针必答"查无" ⇒ 若不跳过就会被判 prune 剪掉一条**完全合法**的依赖。
URL_DEP = '''[project]
name = "myapp"
dependencies = [
    "flask>=3.0",
    "mylib @ git+https://example.com/mylib.git@v1.2.3",
    "otherlib @ https://example.com/otherlib-1.0.tar.gz",
]
'''


def test_url_and_vcs_deps_are_never_pruned():
    """★url/vcs 依赖不得被剪★（与 npm 的 file:/link:/git+ 前缀分档同理）

    这类依赖的坐标由 URL 自证，registry 探针对它们**必然**查无——拿"查无"当
    否定证据就会误剪合法依赖。危害是不可逆的（删依赖）。
    """
    names = {d["name"] for d in DRV.parse_deps(URL_DEP)}
    assert "mylib" not in names and "otherlib" not in names, (
        f"url/vcs 依赖进入了探针判定（会被误剪）: {sorted(names)}")
    assert "flask" in names, "普通依赖不该被牵连"
    out, actions = _enforce(URL_DEP, {"flask"})
    assert actions == [], f"url/vcs 依赖被处置: {actions}"
    parsed = tomllib.loads(out)
    deps = parsed["project"]["dependencies"]
    assert any("git+https" in s for s in deps), "vcs 依赖被删"
    assert any("otherlib-1.0.tar.gz" in s for s in deps), "url 依赖被删"


# ══════════════════════════════════════════════════════════
# B) 破坏面：真 enforce() 链跑完，manifest 必须仍合法且元数据完好
# ══════════════════════════════════════════════════════════

def _enforce(text: str, known: set[str], members=frozenset({"myapp"})):
    new, actions = enforce(
        {"pyproject.toml": text}, root_text=text, namespace=None,
        workspace_members=set(members), registry_versions=_registry(known),
        driver=DRV)
    return new.get("pyproject.toml", text), actions


def test_enforce_never_touches_project_metadata():
    """★缺陷的实际危害面：元数据/build-backend/tool 配置一个都不许被删★

    known 故意只含真依赖 ⇒ 所有 TOML 键（若被误当依赖）都会 registry 查无 → prune。
    这个夹具形状是【最大暴露】：修复前它会删掉 6 个键。
    """
    out, _actions = _enforce(PEP621, REAL_DEPS)
    parsed = tomllib.loads(out)          # 首先必须仍是合法 TOML
    proj = parsed["project"]
    assert proj["name"] == "myapp"
    assert proj["version"] == "0.1.0"
    assert proj["description"].startswith("A sample application")
    assert proj["requires-python"] == ">=3.11"
    assert parsed["build-system"]["build-backend"] == "hatchling.build"
    assert parsed["build-system"]["requires"] == ["hatchling"]
    assert parsed["tool"]["ruff"]["line-length"] == 100
    assert sorted(proj["dependencies"]) == sorted(
        ["flask>=3.0", "requests>=2.31,<3", "pydantic[email]>=2.5"])
    assert proj["optional-dependencies"]["dev"] == ["pytest>=8.0", "ruff"]


def test_enforce_on_clean_project_is_a_noop():
    """真依赖全在 registry ⇒ 零处置（修复前会有 6 条 prune）。"""
    _out, actions = _enforce(PEP621, REAL_DEPS)
    assert actions == [], f"干净工程被处置了: {actions}"


def test_enforce_prunes_only_the_phantom_dep():
    """区分力：真有臆造包时要剪【它】，且只剪它。

    缺了这条，"什么都不剪"的实现会让上面所有测试全绿（闸变成 no-op 也算通过）。
    """
    text = PEP621.replace(
        '    "flask>=3.0",',
        '    "flask>=3.0",\n    "totally-not-a-real-pkg-xyz789>=1.0",')
    out, actions = _enforce(text, REAL_DEPS)
    assert len(actions) == 1, f"处置条数不对: {actions}"
    assert "totally-not-a-real-pkg-xyz789" in actions[0]
    parsed = tomllib.loads(out)
    deps = parsed["project"]["dependencies"]
    assert not any("totally-not-a-real" in s for s in deps), "臆造包没被剪"
    assert "flask>=3.0" in deps and "requests>=2.31,<3" in deps, "真依赖被误剪"


@pytest.mark.parametrize("shape,desc", [
    ('dependencies = ["phantom-xyz789"]', "唯一一条依赖被剪 → 空数组"),
    ('dependencies = ["flask>=3.0", "phantom-xyz789", "requests"]', "inline 数组剪中间"),
    ('dependencies = ["phantom-xyz789", "flask>=3.0"]', "inline 数组剪首条"),
    ('dependencies = ["flask>=3.0", "phantom-xyz789"]', "inline 数组剪末条"),
    ('dependencies = [\n  "flask>=3.0",\n  "phantom-xyz789",\n]', "多行数组带尾逗号"),
    ('dependencies = [\n  "phantom-xyz789",\n  "flask>=3.0"\n]', "多行数组无尾逗号剪首条"),
])
def test_prune_keeps_toml_valid_across_array_shapes(shape, desc):
    """★剪除后文件必须仍是合法 TOML —— 逐个数组排布取证★

    这是本 finding 的核心危害（闸自己制造 manifest 解析期崩塌）。数组排布是
    真实工程里千差万别的一维，逐形态锁住比只测一种排布可信。
    """
    text = f'[project]\nname = "myapp"\n{shape}\n'
    out, _actions = _enforce(text, {"flask", "requests"})
    try:
        parsed = tomllib.loads(out)
    except tomllib.TOMLDecodeError as exc:
        pytest.fail(f"{desc}: 剪除后不再是合法 TOML: {exc}\n--- 产物 ---\n{out}")
    deps = parsed["project"].get("dependencies", [])
    assert not any("phantom" in s for s in deps), f"{desc}: 臆造包没剪"
    assert parsed["project"]["name"] == "myapp", f"{desc}: 元数据被牵连"
    if "flask" in shape:
        assert any("flask" in s for s in deps), f"{desc}: 真依赖被误剪"


# ══════════════════════════════════════════════════════════
# C) fail-honest：解析不出就丢弃，绝不退化成行正则
# ══════════════════════════════════════════════════════════

BROKEN_TOML = '''[project]
name = "unterminated
dependencies = ["flask>=3.0"]
'''


def test_broken_toml_yields_nothing_not_toml_keys():
    """★兜底路径不得与主判据共用缺口★

    「tomllib 解析失败」有两种完全不同的原因：这是 requirements.txt，或这是
    **坏掉的** pyproject.toml。混为一谈会让畸形 TOML 落进行正则 → **原样复发 W-6**
    （实测坏 pyproject 被解析出 name / dependencies 两条"依赖"）。
    故判别式分两层：先试 tomllib，再看「是否形似 TOML」。
    """
    deps = DRV.parse_deps(BROKEN_TOML)
    names = {d["name"] for d in deps}
    assert not (names & set(TOML_KEYS)), (
        f"畸形 TOML 仍把键当依赖（兜底路径复发 W-6）: {sorted(names)}")
    assert deps == [], f"形似 TOML 但解析不出时应返空: {deps}"


def test_broken_toml_enforce_is_noop():
    """畸形 manifest 上闸完全不动手（绝不"边解析不出边剪东西"）。"""
    out, actions = _enforce(BROKEN_TOML, set())
    assert actions == [] and out == BROKEN_TOML, f"畸形文件被改动: {actions}"


@pytest.mark.parametrize("text", [
    "flask>=3.0",
    "pkg == 1.0",                          # PEP 508 允许 == 前后空格
    "pkg ==1.0",
    "pkg[extra] >= 1.0",
    'flask>=3.0; python_version >= "3.11"',
    "requests~=2.31",
    "pkg!=1.0",
    "pkg<=2,>=1",
    "-r other.txt",
    "--index-url https://example.com/simple",
    "# only a comment",
])
def test_requirements_forms_are_not_mistaken_for_toml(text):
    """★判别式反向区分力：真 requirements 行不得被判成"形似 TOML"★

    否则它们会走 fail-honest 分支被整体丢弃 ⇒ requirements 臂静默失效
    （闸看着还在、实际零处置＝比报错更坏）。
    `pkg == 1.0` 是重点：PEP 508 允许 `==` 前后空格，与 TOML 的 `键 = 值` 形近。
    """
    assert not DRV._looks_like_toml(text), f"requirements 行被误判为 TOML: {text!r}"


@pytest.mark.parametrize("text", [
    '[project]\nname = "x"',
    "line-length = 100",
    "[tool.ruff]",
    'name = "broken',
    "[build-system]\nrequires = ['hatchling']",
])
def test_toml_intent_is_detected_even_when_unparseable(text):
    """判别式正向：形似 TOML 的必须被认出（认不出就会落进行正则＝W-6 复发）。"""
    assert DRV._looks_like_toml(text), f"TOML 意图未被识别: {text!r}"


# ══════════════════════════════════════════════════════════
# D) requirements.txt 臂不回归（行正则的唯一合法用途）
# ══════════════════════════════════════════════════════════

REQ = """# comment
flask>=3.0
phantom-xyz789==1.0
-r other.txt
pkg @ git+https://example.com/p.git
requests>=2.31
"""


def test_requirements_parse_unchanged():
    got = {d["name"] for d in DRV.parse_deps(REQ)}
    assert got == {"flask", "phantom-xyz789", "requests"}, got


def test_requirements_skips_pip_directives_and_url_deps():
    """`-r`/`--index-url` 是 pip 指令不是包；url/vcs 依赖在 PyPI 本来就没有
    （拿探针查必查无 ⇒ 会被误剪，与 npm 的 file:/git+ 分档同理）。"""
    names = {d["name"] for d in DRV.parse_deps(REQ)}
    assert "-r" not in names and "pkg" not in names
    new, _a = enforce({"requirements.txt": REQ}, root_text=REQ, namespace=None,
                      workspace_members={"myapp"},
                      registry_versions=_registry({"flask", "requests"}), driver=DRV)
    out = new.get("requirements.txt", REQ)
    assert "-r other.txt" in out, "pip 指令行被误删"
    assert "git+https://example.com/p.git" in out, "url 依赖被误删"


def test_requirements_prune_removes_only_that_line():
    new, actions = enforce(
        {"requirements.txt": REQ}, root_text=REQ, namespace=None,
        workspace_members={"myapp"},
        registry_versions=_registry({"flask", "requests"}), driver=DRV)
    out = new["requirements.txt"]
    assert len(actions) == 1 and "phantom-xyz789" in actions[0]
    assert "phantom-xyz789" not in out
    assert "flask>=3.0" in out and "requests>=2.31" in out


def test_empty_members_fail_open_preserved():
    """★既有 fail-open 保险不得被本次改动破坏（dep_legality.py:405）★

    工作区成员集为空＝没读到任何 manifest（证据缺失≠否定证据）→ 整闸不动。
    生产上 requirements-only 工程正是这个形态，这也是本缺陷长期没在 E2E 暴露的原因。
    """
    new, actions = enforce(
        {"requirements.txt": REQ}, root_text=REQ, namespace=None,
        workspace_members=set(),
        registry_versions=_registry({"flask"}), driver=DRV)
    assert not actions and not new, f"members 空时闸仍动手: {actions}"


# ══════════════════════════════════════════════════════════
# E) root_name：不得把 [tool.*] 下的同名键当工程名
# ══════════════════════════════════════════════════════════

def test_root_name_from_project_table_only():
    assert DRV.root_name(PEP621) == "myapp"


def test_root_name_ignores_tool_section_name_key():
    """行正则会命中第一个 `name =`——若 [tool.*] 在前就取错工程名，
    进而让 fix_name/成员判定整体跑偏。"""
    text = '[tool.poetry.something]\nname = "not-the-project"\n\n[project]\nname = "realapp"\n'
    assert DRV.root_name(text) == "realapp", "取到了 [tool.*] 下的 name"


def test_root_name_none_on_broken_toml():
    """解析不出 → None（不猜）。"""
    assert DRV.root_name(BROKEN_TOML) is None


# ══════════════════════════════════════════════════════════
# F) registry 探针在【宿主进程】——判据可达性的承重前提
# ══════════════════════════════════════════════════════════

def test_registry_probe_returns_empty_list_on_404_meaning_confirmed_absent():
    """★prune 的承重前提：探针把 404 翻成 `[]`（确证查无），不可达翻成 None★

    这个区分是整道闸 fail-open/fail-closed 的分界。若哪天 404 也返 None，
    闸就永不 prune（静默失效）；若不可达返 [] 就会误剪一切（灾难）。
    不打真网络：直接构造 HTTPError/URLError 验映射。
    """
    import urllib.error

    from swarm.worker import dep_legality_drivers as dd

    def _raise_404(*_a, **_k):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    def _raise_unreachable(*_a, **_k):
        raise urllib.error.URLError("no route to host")

    _orig = dd.urllib.request.urlopen
    try:
        dd.urllib.request.urlopen = _raise_404
        assert dd.python_registry_versions_list("", "nope") == [], "404 必须翻成确证查无"
        dd.urllib.request.urlopen = _raise_unreachable
        assert dd.python_registry_versions_list("", "nope") is None, "不可达必须翻成 None"
    finally:
        dd.urllib.request.urlopen = _orig
