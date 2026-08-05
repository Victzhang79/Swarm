"""R56-5/R56-6 治本锁：依赖坐标**单一合法性闸**（栈中立不变量 + 可注册 driver）。

用户点破："我们又开始打地鼠了吗？"——是的。同一个病（LLM 写出不可能被解析的坐标），
按症状形态修了四遍（R53-2 无版本幻影 / R54-5 跨代 / R54-6 命名空间编错 / R56-4 有版本幻影），
四者全是 **error-driven**（解析构建工具的报错文本 → 针对那句错法加分支）→ **换个错法就漏一个**。

本闸是 **state-driven**：构建前扫全工作区 manifest，每条依赖必须满足
【工作区成员 / 上游受管 / 仓库真实存在】三者之一，否则确定性处置。**不看构建工具报什么错**。

★R56-6（自审揪出的命门）★ fail-open 铁律必须**贯通到取数层**：
"仓库确证查无" 与 "仓库没连上" 若都返回空列表 → 沙箱一断网就把全工程合法依赖剪光。
证据缺失 ≠ 否定证据。剪除是不可逆动作，只能建立在肯定证据上。
"""
from __future__ import annotations

from swarm.worker.dep_legality import DRIVERS, classify, enforce, parse_deps

ROOT = """<project>
    <groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId><version>4.8.3</version>
    <modules><module>ruoyi-common</module><module>alarm-core</module></modules>
    <dependencyManagement><dependencies>
        <dependency><groupId>com.alibaba</groupId>
            <artifactId>druid-spring-boot-4-starter</artifactId><version>1.2.28</version></dependency>
    </dependencies></dependencyManagement>
</project>
"""
MEMBERS = {"ruoyi-common", "alarm-core", "ruoyi"}
NS = "com.ruoyi"


def _reg(known: dict):
    """仓库桩：**确证**答复（有 → 版本列表；无 → 空列表）。不可达用 `lambda *_: None` 单独构造。"""
    return lambda ns, name: known.get((ns, name), [])


def _classify(dep, *, registry, managed=frozenset(), managed_unknown=False):
    return classify(dep, namespace=NS, workspace_members=MEMBERS, managed=set(managed),
                    managed_unknown=managed_unknown, registry_versions=registry)


def test_workspace_member_with_wrong_namespace_is_fixed_not_pruned():
    """R54-6 的形态：名字是真模块、命名空间编错 → 改回工程命名空间（绝不剪除真依赖）。"""
    dep = {"namespace": "com.company.alarm", "name": "alarm-core", "version": None, "block": ""}
    v, why = _classify(dep, registry=_reg({}))
    assert v == "fix_namespace", why


def test_phantom_internal_module_is_pruned_regardless_of_version():
    """R53-2 + R56-4 两形态合一：工程命名空间但非工作区成员 → 永不可解析（有无版本都剪）。"""
    for ver in (None, "4.8.3"):
        dep = {"namespace": NS, "name": "ruoyi-alarm-system", "version": ver, "block": ""}
        v, why = _classify(dep, registry=_reg({}))
        assert v == "prune", f"version={ver} 时应剪除：{why}"


def test_third_party_absent_from_registry_is_pruned():
    """仓库**确证**查无任何版本（如臆造的 aerogear-otp-java:1.1.0）→ 永不可解析 → 剪除。"""
    dep = {"namespace": "com.github.aerogear", "name": "aerogear-otp-java",
           "version": "1.1.0", "block": ""}
    v, _ = _classify(dep, registry=_reg({}))
    assert v == "prune"


def test_registry_unreachable_never_prunes():
    """★fail-open 铁律★ 仓库不可达（None）→ 一律放行，绝不误剪合法依赖。"""
    dep = {"namespace": "cn.hutool", "name": "hutool-all", "version": "5.8.47", "block": ""}
    v, why = _classify(dep, registry=lambda ns, name: None)
    assert v == "legal", why
    dep_nov = {"namespace": "cn.hutool", "name": "hutool-all", "version": None, "block": ""}
    assert _classify(dep_nov, registry=lambda ns, name: None)[0] == "legal"


def test_managed_upstream_makes_versionless_dep_legal():
    """上游受管（含 BOM 受管集未知）→ 无版本的依赖一律放行（不误判非法）。"""
    dep = {"namespace": "org.springframework.boot", "name": "spring-boot-starter-web",
           "version": None, "block": ""}
    assert _classify(dep, registry=_reg({}), managed_unknown=True)[0] == "legal"
    assert _classify(dep, registry=_reg({}),
                     managed={"spring-boot-starter-web"})[0] == "legal"


def test_variable_version_ref_is_legal():
    """版本走 ${...} 属性引用 → 由工程自身承接，不去查仓库（查了必然查无 → 会误剪）。"""
    dep = {"namespace": "cn.hutool", "name": "hutool-all",
           "version": "${hutool.version}", "block": ""}

    def _boom(ns, name):
        raise AssertionError("变量引用的版本不该去查仓库")
    assert _classify(dep, registry=_boom)[0] == "legal"


def test_enforce_rewrites_whole_tree_deterministically():
    """端到端：一棵含三种病灶的树 → 幻影剪除、命名空间改回、合法依赖分毫不动。"""
    pom = """<project>
    <artifactId>alarm-core</artifactId>
    <dependencies>
        <dependency><groupId>com.ruoyi</groupId><artifactId>ruoyi-alarm-system</artifactId><version>4.8.3</version></dependency>
        <dependency><groupId>com.company.alarm</groupId><artifactId>ruoyi-common</artifactId><version>4.8.3</version></dependency>
        <dependency><groupId>cn.hutool</groupId><artifactId>hutool-all</artifactId><version>5.8.47</version></dependency>
    </dependencies>
</project>
"""
    new, actions = enforce(
        {"alarm-core/pom.xml": pom}, root_text=ROOT, namespace=NS, workspace_members=MEMBERS,
        registry_versions=_reg({("cn.hutool", "hutool-all"): ["5.8.47"]}),
        driver=DRIVERS["maven"],
    )
    out = new["alarm-core/pom.xml"]
    assert "ruoyi-alarm-system" not in out, "幻影模块（有 version）必须剪除"
    assert "com.company.alarm" not in out and "ruoyi-common" in out, "真模块只改命名空间，绝不剪除"
    assert "hutool-all" in out and "5.8.47" in out, "合法第三方依赖分毫不动"
    assert len(actions) == 2 and any("prune" in a for a in actions) \
        and any("fix_namespace" in a for a in actions)


def test_enforce_never_touches_anything_when_registry_unreachable():
    """★断网演练（R56-6 真身）★ 仓库全程不可达 → 除**可证**幻影外，一条依赖都不许动。

    旧实现里"确证查无"与"没连上"都是空列表 → 这棵树的 hutool/easyexcel 会被全部剪光。
    """
    pom = """<project>
    <artifactId>alarm-core</artifactId>
    <dependencies>
        <dependency><groupId>cn.hutool</groupId><artifactId>hutool-all</artifactId><version>5.8.47</version></dependency>
        <dependency><groupId>com.alibaba</groupId><artifactId>easyexcel</artifactId><version>4.0.3</version></dependency>
        <dependency><groupId>com.ruoyi</groupId><artifactId>ruoyi-phantom</artifactId><version>4.8.3</version></dependency>
    </dependencies>
</project>
"""
    new, actions = enforce(
        {"alarm-core/pom.xml": pom}, root_text=ROOT, namespace=NS, workspace_members=MEMBERS,
        registry_versions=lambda ns, name: None,
        driver=DRIVERS["maven"],   # 全程不可达
    )
    out = new["alarm-core/pom.xml"]
    assert "hutool-all" in out and "easyexcel" in out, "断网时绝不许剪合法第三方依赖"
    assert "ruoyi-phantom" not in out, "幻影模块是**本地可证**的（无需仓库），仍须剪除"
    assert len(actions) == 1 and "prune" in actions[0]


def test_parse_deps_ignores_dependency_management_block():
    """受管块是"版本表"不是本模块依赖——混进来会把版本表也当依赖校验（误剪风险）。"""
    assert parse_deps(ROOT) == [], "root pom 只有 dependencyManagement，没有真实依赖"


def test_gate_is_stack_neutral_new_stack_registers_a_driver():
    """★通用性锁★ 不变量与编排零栈耦合：注册一个玩具 driver 即可让本闸管别的栈。

    锁死"别为某一栈写死"——Cargo/npm/Go 接入应当只是实现 ManifestDriver，不改 classify/enforce。
    """
    class TomlishDriver:
        stack = "tomlish"

        def parse_deps(self, text):
            out = []
            for line in text.splitlines():
                if "=" not in line:
                    continue
                name, ver = (p.strip() for p in line.split("=", 1))
                out.append({"namespace": "crates", "name": name,
                            "version": ver or None, "block": line})
            return out

        def managed_names(self, root_text):
            return set()

        def managed_unknown(self, root_text):
            return False

        def rewrite_namespace(self, block, namespace):
            return block

        def remove(self, text, block):
            return "\n".join(ln for ln in text.splitlines() if ln != block)

    DRIVERS["tomlish"] = TomlishDriver()
    try:
        manifest = "serde = 1.0.2\nphantom-crate = 9.9.9"
        new, actions = enforce(
            {"Cargo.tomlish": manifest}, root_text="", namespace="local",
            workspace_members={"app"},   # 非空：空集=未能取证 → 全闸 fail-open（另有专测）
            registry_versions=_reg({("crates", "serde"): ["1.0.2"]}),
            driver=DRIVERS["tomlish"],
        )
        out = new["Cargo.tomlish"]
        assert "serde" in out, "仓库确证存在的依赖不许动"
        assert "phantom-crate" not in out, "仓库确证查无 → 同一条不变量照样生效（无需改闸）"
        assert len(actions) == 1
    finally:
        DRIVERS.pop("tomlish", None)


def test_empty_workspace_members_fails_open_entirely():
    """★保险丝★ 工作区成员集为空 = 根本没读到 manifest（读失败/树异常）→ 一条都不许动。

    规则②（工程命名空间 + 非工作区成员 → 幻影）**没有 fail-open 出口**（它以"工程模块从不在
    远程仓库里"为由无条件剪）。若成员集因取证失败而为空，它会把**全部真兄弟依赖**当幻影剪光。
    "不是成员" 在成员集为空时是**证据缺失**，不是否定证据。
    """
    pom = """<project><artifactId>x</artifactId><dependencies>
        <dependency><groupId>com.ruoyi</groupId><artifactId>ruoyi-common</artifactId></dependency>
    </dependencies></project>"""
    new, actions = enforce(
        {"x/pom.xml": pom}, root_text="", namespace=NS, workspace_members=set(),
        registry_versions=_reg({}),
        driver=DRIVERS["maven"],
    )
    assert not new and not actions, "成员集为空时必须整体 fail-open，绝不剪除任何依赖"


def test_commented_out_dependency_is_ignored_and_real_one_with_inline_comment_is_handled():
    """块内含行内注释的**真**依赖必须能被处置；被注释掉的依赖必须**不**被当成真依赖。

    旧实现在"去注释副本"上切块 → block 在原文里定位不到 → 判了却改不动（静默无效）。
    """
    pom = """<project><artifactId>x</artifactId><dependencies>
        <!-- <dependency><groupId>com.ruoyi</groupId><artifactId>old-thing</artifactId></dependency> -->
        <dependency><!-- 历史遗留 --><groupId>com.ruoyi</groupId><artifactId>ruoyi-phantom</artifactId><version>1.0</version></dependency>
    </dependencies></project>"""
    new, actions = enforce(
        {"x/pom.xml": pom}, root_text=ROOT, namespace=NS, workspace_members=MEMBERS,
        registry_versions=_reg({}),
        driver=DRIVERS["maven"],
    )
    out = new["x/pom.xml"]
    assert "ruoyi-phantom" not in out, "含行内注释的真幻影依赖必须被真正剪掉（不能只判不改）"
    assert "old-thing" in out, "被注释掉的依赖不是真依赖，不该被当成处置对象"
    assert len(actions) == 1


# ── R57-2：名字写错（加了工程前缀）→ 必须**改名**，不是剪除 ──────────────────

def test_sibling_with_project_prefix_is_renamed_not_pruned():
    """★R57-2 P0（round57 实锤）★ LLM 给兄弟模块 artifactId 统一加了工程前缀 → 必须确定性改名。

    实锤：alarm-web/pom.xml 依赖 5 个兄弟模块，全写成 `ruoyi-alarm-core` / `ruoyi-alarm-engine`…
    （根 artifactId 就叫 ruoyi）。旧闸判"永不可解析"**正确**，但把 5 条**真实兄弟依赖全剪光**
    → alarm-web 编译期满屏 cannot-find-symbol。这是把"可确定性修复的错"降级成了"不可逆的剪除"。

    铁律：**剪除是最后手段，能修则修**。判据必须零歧义——去掉工程前缀后**唯一**命中一个工作区成员。
    """
    for real in ("alarm-core", "alarm-engine", "alarm-schedule", "alarm-security", "alarm-api"):
        dep = {"namespace": NS, "name": f"ruoyi-{real}",
               "version": "${project.version}", "block": ""}
        v, why = classify(dep, namespace=NS,
                          workspace_members={"ruoyi", *[
                              "alarm-core", "alarm-engine", "alarm-schedule",
                              "alarm-security", "alarm-api"]},
                          managed=set(), managed_unknown=False,
                          registry_versions=_reg({}), root_name="ruoyi")
        assert v == "fix_name", f"{dep['name']} 应改名到 {real}，而不是 {v}：{why}"
        assert real in why


def test_ambiguous_near_miss_name_is_pruned_never_guessed():
    """去掉前缀后命中**多个**成员（或零个）→ 一律剪除，**绝不猜**（猜错=接错模块，比缺依赖更毒）。"""
    dep = {"namespace": NS, "name": "ruoyi-common", "version": None, "block": ""}
    # 工作区里没有 common → 无候选 → 剪
    v, _ = classify(dep, namespace=NS, workspace_members={"ruoyi", "alarm-core"},
                    managed=set(), managed_unknown=False,
                    registry_versions=_reg({}), root_name="ruoyi")
    assert v == "prune"


def test_enforce_renames_sibling_deps_end_to_end():
    """端到端：alarm-web 的 5 条兄弟依赖被改名（而非剪除），版本引用原样保留。"""
    pom = """<project>
    <artifactId>alarm-web</artifactId>
    <dependencies>
        <dependency><groupId>com.ruoyi</groupId><artifactId>ruoyi-alarm-core</artifactId><version>${project.version}</version></dependency>
        <dependency><groupId>com.ruoyi</groupId><artifactId>ruoyi-alarm-api</artifactId><version>${project.version}</version></dependency>
    </dependencies>
</project>
"""
    root = ("<project><groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
            "<modules><module>alarm-core</module><module>alarm-api</module>"
            "<module>alarm-web</module></modules></project>")
    new, actions = enforce(
        {"alarm-web/pom.xml": pom}, root_text=root, namespace=NS,
        workspace_members={"ruoyi", "alarm-core", "alarm-api", "alarm-web"},
        registry_versions=_reg({}), root_name="ruoyi",
        driver=DRIVERS["maven"],
    )
    out = new["alarm-web/pom.xml"]
    assert "<artifactId>alarm-core</artifactId>" in out and "ruoyi-alarm-core" not in out
    assert "<artifactId>alarm-api</artifactId>" in out and "ruoyi-alarm-api" not in out
    assert out.count("${project.version}") == 2, "版本引用必须原样保留（由 reactor 承接）"
    assert len(actions) == 2 and all("fix_name" in a for a in actions)


def test_enforce_fix_name_syncs_hardcoded_version():
    """批次6 R1（reviewer HIGH）：fix_name 与 fix_namespace 对称——改名修回真成员后，
    残留硬编码外部版本同样让 reactor 解析失败 → 同步为 ${project.version}。"""
    pom = """<project>
    <artifactId>alarm-web</artifactId>
    <dependencies>
        <dependency><groupId>com.ruoyi</groupId><artifactId>ruoyi-alarm-core</artifactId><version>1.0.0</version></dependency>
    </dependencies>
</project>
"""
    root = ("<project><groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
            "<modules><module>alarm-core</module><module>alarm-web</module></modules></project>")
    new, actions = enforce(
        {"alarm-web/pom.xml": pom}, root_text=root, namespace=NS,
        workspace_members={"ruoyi", "alarm-core", "alarm-web"},
        registry_versions=_reg({}), root_name="ruoyi",
        driver=DRIVERS["maven"],
    )
    out = new["alarm-web/pom.xml"]
    assert "<artifactId>alarm-core</artifactId>" in out, "fix_name 改名必须生效"
    assert "<version>1.0.0</version>" not in out, "硬编码外部版本绝不留存（reactor 解析失败源）"
    assert "${project.version}" in out, "版本必须同步为工程版本引用（与 fix_namespace 对称）"


def test_reactor_member_is_never_pruned_by_registry_evidence():
    """★R57-3（round57 near-miss）★ reactor 成员在远程仓库里查无是**正常的**，不是罪证。

    实锤：`com.ruoyi:ruoyi`（工程根模块自己）走进第三方分支 → "仓库确证查无 → 确定性剪除"，
    只因当时恰好没有 pom 声明它（0 pom）才没删掉合法依赖。
    规则①（工作区成员优先）必须在**任何**查仓库的判定之前短路——工程模块从不由仓库解析。
    """
    def _boom(ns, name):
        raise AssertionError("工作区成员绝不该去查仓库（查了必然查无 → 必然误剪）")

    for ver in (None, "4.8.3", "${project.version}"):
        dep = {"namespace": NS, "name": "ruoyi", "version": ver, "block": ""}
        v, why = classify(dep, namespace=NS, workspace_members=MEMBERS, managed=set(),
                          managed_unknown=False, registry_versions=_boom)
        assert v == "legal", f"reactor 成员(version={ver}) 必须直接判合法：{why}"


# ── X-M10（27 号文 §3.2）：npm driver + 调用方按 manifest 分派 ─────────────

NPM_ROOT = """{
  "name": "@acme/shop",
  "workspaces": ["packages/*"],
  "dependencies": {
    "vue": "^3.4.0",
    "lodash-fake-xyz": "^1.0.0",
    "@acme/web": "*",
    "@acme/lib": "^2.0.0",
    "mytool": "file:../tools/mytool"
  },
  "devDependencies": {
    "typescript": "~5.5.0"
  }
}
"""
NPM_MEMBER = '{"name": "@acme/web", "dependencies": {"vue": "^3.4.0"}}'
NPM_MEMBERS = {"@acme/shop", "@acme/web"}


def _npm_reg(known: dict, reachable: bool = True):
    """npm 仓库桩：known → 版本列表；否则 []（确证查无）。reachable=False → None（没连上）。"""
    if not reachable:
        return lambda ns, name: None
    return lambda ns, name: known.get(name, [])


def test_xm10_npm_parse_deps_sections_scopes_and_blocks():
    deps = DRIVERS["npm"].parse_deps(NPM_ROOT)
    by_name = {d["name"]: d for d in deps}
    assert set(by_name) == {"vue", "lodash-fake-xyz", "@acme/web", "@acme/lib",
                            "mytool", "typescript"}
    assert by_name["@acme/lib"]["namespace"] == "@acme"
    assert by_name["vue"]["namespace"] == ""
    assert by_name["typescript"]["version"] == "~5.5.0"
    # block 必须能在原文定位（enforce 的改写/删除全靠它）
    for d in deps:
        assert d["block"] in NPM_ROOT, d["name"]


def test_xm10_npm_remove_keeps_json_valid_middle_and_last():
    import json as _json
    drv = DRIVERS["npm"]
    deps = {d["name"]: d for d in drv.parse_deps(NPM_ROOT)}
    # 删中间条目（vue）
    t1 = drv.remove(NPM_ROOT, deps["vue"]["block"])
    _json.loads(t1)
    # 删 dependencies 末条目（mytool，后面是 devDependencies → 必须连前导逗号处置）
    t2 = drv.remove(NPM_ROOT, deps["mytool"]["block"])
    parsed = _json.loads(t2)
    assert "mytool" not in parsed["dependencies"]
    assert "@acme/lib" in parsed["dependencies"]


def test_xm10_npm_enforce_prunes_phantom_keeps_legit():
    """端到端：幻影包（registry 确证查无）→ 剪；真包/工作区成员/已发布 scoped → 留。"""
    reg = _npm_reg({"vue": ["3.4.0"], "@acme/lib": ["2.0.0"], "typescript": ["5.5.0"]})
    new_texts, actions = enforce(
        {"package.json": NPM_ROOT, "packages/web/package.json": NPM_MEMBER},
        root_text=NPM_ROOT, namespace=None, workspace_members=NPM_MEMBERS,
        registry_versions=reg, driver=DRIVERS["npm"])
    import json as _json
    parsed = _json.loads(new_texts["package.json"])
    assert "lodash-fake-xyz" not in parsed["dependencies"], "幻影包必须被剪"
    assert parsed["dependencies"]["vue"] == "^3.4.0"
    assert parsed["dependencies"]["@acme/web"] == "*", "工作区成员不动"
    assert parsed["dependencies"]["@acme/lib"] == "^2.0.0", \
        "已发布 scoped 包不动（分档①：@scope ≠ 工程命名空间，规则②不得误剪）"
    assert any("prune" in a for a in actions)


def test_xm10_npm_registry_unreachable_never_prunes():
    """fail-open 铁律贯通 npm 臂：registry 没连上（None）→ 一条都不动。"""
    new_texts, actions = enforce(
        {"package.json": NPM_ROOT}, root_text=NPM_ROOT, namespace=None,
        workspace_members=NPM_MEMBERS,
        registry_versions=_npm_reg({}, reachable=False), driver=DRIVERS["npm"])
    assert new_texts == {} and actions == []


def test_xm10_npm_protocol_versions_never_probed():
    """file:/link:/git+/workspace: 版本由工程自身承接——registry 里本来就没有，
    拿探针查=必 E404=误剪合法本地依赖（self_hosted_prefixes 分档）。"""
    probed: list[str] = []

    def _reg(ns, name):
        probed.append(name)
        return []   # 即便"确证查无"也不许剪 file: dep

    new_texts, actions = enforce(
        {"package.json": NPM_ROOT}, root_text=NPM_ROOT, namespace=None,
        workspace_members=NPM_MEMBERS, registry_versions=_reg, driver=DRIVERS["npm"])
    import json as _json
    assert _json.loads(new_texts.get("package.json", NPM_ROOT))["dependencies"]["mytool"] \
        == "file:../tools/mytool"
    assert "mytool" not in probed, "协议引用版本绝不送探针"


# ── 调用方（l1_pipeline）分派接线 ──


def _wire_npm_project(monkeypatch, files: dict, npm_views: dict):
    """把 l1_pipeline 的沙箱读写/扫描/探针全部桩成本地 dict。
    npm_views: 包名 → `npm view` 输出（未登记的包给 E404）。"""
    import shlex as _shlex

    import swarm.worker.l1_pipeline as lp

    monkeypatch.setattr(lp, "_read_project_file",
                        lambda pp, rel, timeout=20: files.get(rel))
    written: dict = {}
    monkeypatch.setattr(lp, "_write_project_file",
                        lambda pp, rel, content, timeout=20: (files.__setitem__(rel, content),
                                                              written.__setitem__(rel, content),
                                                              True)[-1])
    monkeypatch.setattr(lp, "_run_check_split",
                        lambda cmd, pp, timeout=60:
                        (0, "".join(f"{r}\n" for r in files if r.endswith("package.json")), ""))

    def _fake_l1(cmd, pp, timeout=60):
        for name, out in npm_views.items():
            if _shlex.quote(name) in cmd:
                return (0, out)
        return (0, "npm error code E404\nnpm error 404 Not Found")

    monkeypatch.setattr(lp, "_run_l1_command", _fake_l1)
    return lp, written


def test_xm10_gate_dispatches_to_npm_by_manifest_presence(monkeypatch):
    """接线锁：无 pom、只有 package.json 的工程 → npm 臂真跑（幻影被剪）。
    突变「分派器删掉 npm 分支 / driver 从 DRIVERS 摘除」→ 本条红。"""
    pkg = ('{"name": "shop", "dependencies": {"phantom-xyz-abc": "^1.0.0", '
           '"vue": "^3.0.0", "@acme/lib": "^2.0.0"}}')
    files = {"package.json": pkg}
    lp, written = _wire_npm_project(monkeypatch, files,
                                    {"vue": '["3.0.0"]', "@acme/lib": '["2.0.0"]'})
    n, changed = lp._enforce_dep_legality("/tmp/x", 60)
    assert n == 1 and changed == ["package.json"]
    import json as _json
    deps = _json.loads(files["package.json"])["dependencies"]
    assert "phantom-xyz-abc" not in deps
    assert deps["vue"] == "^3.0.0"


def test_xm10_gate_keeps_published_scoped_package(monkeypatch):
    """分档①接线锁：已发布 scoped 包（registry 有版本、非工作区成员）必须留——
    突变「npm 臂 namespace=None 回传 @scope」→ 规则② 不查探针直接剪 → 本条红。"""
    pkg = '{"name": "@acme/shop", "dependencies": {"@acme/lib": "^2.0.0"}}'
    files = {"package.json": pkg}
    lp, written = _wire_npm_project(monkeypatch, files, {"@acme/lib": '["2.0.0"]'})
    n, changed = lp._enforce_dep_legality("/tmp/x", 60)
    assert n == 0 and changed == [], f"已发布 scoped 包被误处置: {changed}"
    import json as _json
    assert _json.loads(files["package.json"])["dependencies"]["@acme/lib"] == "^2.0.0"


def test_xm10_unknown_stack_warns_driver_absent(monkeypatch, caplog):
    """无 driver 的栈：零覆盖必须机读可辨（D14 warn-once），绝不与「已校验」混同。"""
    import logging

    import swarm.worker.dep_legality as dl
    dl._driver_absent_warned.discard("php")   # warn-once 是模块态，顺序无关化
    with caplog.at_level(logging.WARNING):
        drv = dl.driver_for("php")
    assert drv is None
    assert any("无注册 driver" in r.message and "'php'" in r.message
               for r in caplog.records), f"零覆盖必须 warn: {[r.message for r in caplog.records]}"


def test_xm10_npm_probe_contract():
    """探针契约：E404→([],True) 可剪；拿到版本→(vers,True)；工具缺失→([],False) fail-open。"""
    import swarm.worker.l1_pipeline as lp

    def _probe(out):
        orig = lp._run_l1_command
        lp._run_l1_command = lambda cmd, pp, timeout=60: (0, out)
        try:
            return lp._fetch_npm_versions_probe("x", "/tmp", 30)
        finally:
            lp._run_l1_command = orig

    assert _probe("npm error code E404\n404 Not Found") == ([], True)
    assert _probe('["1.0.0","1.1.0"]') == (["1.0.0", "1.1.0"], True)
    assert _probe('"2.3.4"') == (["2.3.4"], True)          # 单版本=裸字符串形态
    assert _probe("sh: npm: command not found") == ([], False)
    assert _probe("npm error network ETIMEDOUT") == ([], False)


# ── W-6：Cargo / Go / Gradle / Python driver 注册与解析 ───────────────────

def test_cargo_driver_parses_and_prunes_phantom():
    from swarm.worker.dep_legality import DRIVERS, enforce
    drv = DRIVERS["cargo"]
    manifest = '[dependencies]\nserde = "1.0.2"\nphantom-crate = "9.9.9"\n'
    new, actions = enforce(
        {"Cargo.toml": manifest}, root_text="", namespace="",
        workspace_members={"app"}, registry_versions=lambda _ns, n: ["1.0.2"] if n == "serde" else [],
        driver=drv,
    )
    out = new["Cargo.toml"]
    assert "serde" in out, "仓库确证存在的 crate 不许动"
    assert "phantom-crate" not in out, "仓库确证查无 → 剪除"
    assert len(actions) == 1 and "prune" in actions[0]


def test_cargo_driver_workspace_member_legal():
    from swarm.worker.dep_legality import DRIVERS, enforce
    drv = DRIVERS["cargo"]
    manifest = '[dependencies]\napp = { path = "../app" }\n'
    new, actions = enforce(
        {"Cargo.toml": manifest}, root_text="", namespace="",
        workspace_members={"app"}, registry_versions=lambda _ns, n: None,
        driver=drv,
    )
    assert not new and not actions, "path/workspace 成员依赖应判 legal"


def test_go_driver_parses_require_and_prunes_phantom():
    from swarm.worker.dep_legality import DRIVERS, enforce
    drv = DRIVERS["go"]
    manifest = 'module example.com/shop\n\nrequire (\n\texample.com/lib v1.0.0\n\texample.com/phantom v9.9.9\n)\n'
    new, actions = enforce(
        {"go.mod": manifest}, root_text=manifest, namespace="example.com/shop",
        workspace_members={"example.com/shop", "example.com/lib"},
        registry_versions=lambda _ns, n: ["v1.0.0"] if n == "example.com/lib" else [],
        driver=drv,
    )
    out = new["go.mod"]
    assert "example.com/lib" in out
    assert "example.com/phantom" not in out
    assert len(actions) == 1 and "prune" in actions[0]


def test_gradle_driver_parses_string_dependency():
    from swarm.worker.dep_legality import DRIVERS, enforce
    drv = DRIVERS["gradle"]
    manifest = "dependencies {\n    implementation 'com.example:lib:1.0.0'\n}\n"
    new, actions = enforce(
        {"build.gradle": manifest}, root_text="", namespace="",
        workspace_members={"app"}, registry_versions=lambda _ns, n: ["1.0.0"] if n == "lib" else [],
        driver=drv,
    )
    assert not new and not actions, "仓库存在的 Gradle 依赖应 legal"


def test_python_driver_parses_requirements():
    from swarm.worker.dep_legality import DRIVERS, enforce
    drv = DRIVERS["python"]
    manifest = "requests==2.31.0\nphantom-pkg==99.99\n"
    new, actions = enforce(
        {"requirements.txt": manifest}, root_text="", namespace="",
        workspace_members={"myapp"}, registry_versions=lambda _ns, n: ["2.31.0"] if n == "requests" else [],
        driver=drv,
    )
    out = new["requirements.txt"]
    assert "requests" in out
    assert "phantom-pkg" not in out
    assert len(actions) == 1 and "prune" in actions[0]


def test_l1_pipeline_enforces_cargo_go_gradle_python_arms(monkeypatch):
    """W-6：_enforce_dep_legality 对 Cargo/Go/Gradle/Python 都有执行臂。"""
    import swarm.worker.l1_pipeline as lp
    files = {
        "Cargo.toml": '[package]\nname = "svc"\n[dependencies]\nserde = "1.0.0"\n',
        "go.mod": "module example.com/shop\n\nrequire example.com/lib v1.0.0\n",
        "build.gradle": "dependencies { implementation 'com.example:lib:1.0.0' }\n",
        "requirements.txt": "requests==2.31.0\n",
    }
    lp, written = _wire_npm_project(monkeypatch, files, {})
    # registry unreachable -> fail-open，但各臂必须被调用到（无异常）
    n, changed = lp._enforce_dep_legality("/tmp/x", 60)
    assert n == 0 and changed == []
