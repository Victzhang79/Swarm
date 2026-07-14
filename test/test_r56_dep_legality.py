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
        registry_versions=lambda ns, name: None,   # 全程不可达
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
    )
    out = new["alarm-web/pom.xml"]
    assert "<artifactId>alarm-core</artifactId>" in out and "ruoyi-alarm-core" not in out
    assert "<artifactId>alarm-api</artifactId>" in out and "ruoyi-alarm-api" not in out
    assert out.count("${project.version}") == 2, "版本引用必须原样保留（由 reactor 承接）"
    assert len(actions) == 2 and all("fix_name" in a for a in actions)


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
