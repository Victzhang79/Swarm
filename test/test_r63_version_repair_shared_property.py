"""R63 治本锁：version-repair 绝不因单个依赖的版本诉求降级【共享/平台版本锚属性】。

round63 死因实锤（cassette d03e4523，执行期卡 13/78）：
    工程基线 Spring Boot 4.0.6（root pom `spring-boot.version=4.0.6`）。契约把
    `spring-boot-starter-aop` 引进模块 pom（版本写作 `${spring-boot.version}`）。但 aop 在
    Boot 4 已被移除（改名 aspectj），Maven 报 `Could not find artifact
    org.springframework.boot:spring-boot-starter-aop:jar:4.0.6`，仓库该 artifact 最高只到
    Boot 3 系 3.5.16。

    version-repair **分支①「版本不存在→校正」** 走到 `_choose_valid_version(4.0.6, [3.5.16])`
    挑了 3.5.16，再经 `rewrite_property_version` 把**共享**属性 `${spring-boot.version}`
    4.0.6→3.5.16 —— 一个依赖的诉求，把**整 reactor** 降了一个大版本代际 → 基座
    `ruoyi-common`（用 commons-lang3 3.18+ 的 `Strings` API）编译崩 → `-am` 全线 BLOCKED →
    毒 pom 被 pull-back 合并 → 每个后续沙箱重新中毒=永久死锁。

    根因是**两分支不对称**：分支②「缺 <version>→注入」早有 `_group_family_version` 代际守卫
    （工程家族钉在 X 代、artifact 在该代不存在 → 剪除依赖，见 R54-5）；分支①**没有**这条守卫。
    "一个不变量两处实现，只有一处对"——正是 round57-3 教训的原样重演。

治本（栈中立·机制级，不写死任何 artifact 名）：
  · 抽出纯判据 `_family_generation_choice(fam, available)`，两分支共用（单一权威）：
      工程家族钉在 fam 且 fam 在仓库可用 → 对齐到 fam；
      钉在 fam 但 artifact 在该代不存在 → 剪除依赖（跨代混用是集成期才炸的暗雷）；
      工程无该家族先例 → 交调用方按默认（分支①最近有效版 / 分支②最新稳定版）。
  · 依赖版本经**共享**属性 `${x.version}` 引用、且解析器判定应剪除时 → 剪依赖，**共享锚属性一律不碰**。

该不变量非 Java 专属：npm 的共享 version、Gradle 的 `ext`、Cargo workspace 版本同理——确定性
依赖修复绝不能为满足单个依赖去改写被多方共享的版本锚。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import swarm.worker.l1_pipeline as L
from swarm.worker.l1_pipeline import (
    _PRUNE_DEP,
    _attempt_maven_version_repair,
    _dep_consumers_of_property,
    _family_generation_choice,
)


# ── 纯判据：代际对齐 vs 剪除 vs 交默认（单一权威，两分支共用） ──────────────────
def test_family_choice_aligns_when_family_version_available():
    """工程家族钉在 4.0.6 且仓库有 4.0.6 → 对齐到 4.0.6（唯一正确目标）。"""
    assert _family_generation_choice("4.0.6", ["3.5.16", "4.0.6"]) == "4.0.6"


def test_family_choice_prunes_when_artifact_absent_in_that_generation():
    """★round63 死因判据★ 家族钉在 4.0.6，但 artifact 在该代不存在（仓库最高=另一代 3.5.16）
    → 判剪除，**绝不降级共享锚属性**。"""
    assert _family_generation_choice("4.0.6", ["3.5.15", "3.5.16"]) is _PRUNE_DEP


def test_family_choice_defers_when_no_family_precedent():
    """工程无该家族先例 → None（交调用方走各自默认版本策略，保留旧行为）。"""
    assert _family_generation_choice(None, ["3.5.16"]) is None
    assert _family_generation_choice("", ["3.5.16"]) is None


# ── 端到端：round63 毒场景（真 grep + 真文件 I/O，仅桩版本探针） ────────────────
_ROOT_POM_BOOT4 = """<project>
  <groupId>com.ruoyi</groupId>
  <artifactId>ruoyi</artifactId>
  <version>4.8.3</version>
  <packaging>pom</packaging>
  <properties>
    <spring-boot.version>4.0.6</spring-boot.version>
  </properties>
  <modules>
    <module>ruoyi-framework</module>
  </modules>
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-dependencies</artifactId>
        <version>${spring-boot.version}</version>
        <type>pom</type>
        <scope>import</scope>
      </dependency>
    </dependencies>
  </dependencyManagement>
</project>
"""

_FRAMEWORK_POM_AOP = """<project>
  <parent>
    <groupId>com.ruoyi</groupId>
    <artifactId>ruoyi</artifactId>
    <version>4.8.3</version>
  </parent>
  <artifactId>ruoyi-framework</artifactId>
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-aop</artifactId>
      <version>${spring-boot.version}</version>
    </dependency>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
      <version>${spring-boot.version}</version>
    </dependency>
  </dependencies>
</project>
"""


def _mk_project(d: str) -> tuple[Path, Path]:
    root = Path(d)
    (root / "pom.xml").write_text(_ROOT_POM_BOOT4, encoding="utf-8")
    (root / "ruoyi-framework").mkdir(parents=True)
    (root / "ruoyi-framework" / "pom.xml").write_text(_FRAMEWORK_POM_AOP, encoding="utf-8")
    return root / "pom.xml", root / "ruoyi-framework" / "pom.xml"


def test_cross_generation_dep_is_pruned_and_shared_property_untouched(monkeypatch):
    """★头号锁★ aop@4.0.6 在 Boot 4 不存在（仓库只到 Boot 3 系 3.5.16）→
    剪除该依赖，**共享 ${spring-boot.version} 一个字符都不能动**。

    RED（治本前）：分支①选 3.5.16、把 spring-boot.version 改成 3.5.16（整 reactor 降代）。
    """
    # aop 只在 Boot 3 系存在；探针如实返回（版本对、代际错——稳定版闸挡不住）。
    monkeypatch.setattr(L, "_fetch_maven_versions_probe",
                        lambda g, a, p, t: (["3.5.15", "3.5.16"], True))
    with tempfile.TemporaryDirectory() as d:
        root_pom, fw_pom = _mk_project(d)
        build_out = ("[ERROR] Could not find artifact "
                     "org.springframework.boot:spring-boot-starter-aop:jar:4.0.6\n")
        n, changed = _attempt_maven_version_repair(str(Path(d)), build_out, timeout=30)

        root_txt = root_pom.read_text("utf-8")
        fw_txt = fw_pom.read_text("utf-8")
        # 共享平台锚属性绝不被降级
        assert "<spring-boot.version>4.0.6</spring-boot.version>" in root_txt, \
            "★共享版本锚属性绝不能因单依赖被降级★"
        assert "3.5.16" not in root_txt, "root pom 不得出现降代版本"
        # 跨代依赖被剪除
        assert "spring-boot-starter-aop" not in fw_txt, "跨代 artifact 必须被剪除"
        # 同代合法依赖不得误伤
        assert "spring-boot-starter-web" in fw_txt, "同代合法依赖不得被误剪"
        assert n >= 1 and any(p.endswith("pom.xml") for p in changed)


def test_wrong_literal_version_aligns_to_family_generation(monkeypatch):
    """依赖版本写错但工程家族钉了同代有效版 → 对齐家族版（不降级、不剪除）。"""
    monkeypatch.setattr(L, "_fetch_maven_versions_probe",
                        lambda g, a, p, t: (["3.5.16", "4.0.6"], True))
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "pom.xml").write_text(_ROOT_POM_BOOT4, encoding="utf-8")
        (root / "ruoyi-framework").mkdir(parents=True)
        # 这次 aop 存在于 4.0.6，但某依赖块写了错误字面量 3.9.9
        (root / "ruoyi-framework" / "pom.xml").write_text(
            "<project><artifactId>ruoyi-framework</artifactId><dependencies>"
            "<dependency><groupId>org.springframework.boot</groupId>"
            "<artifactId>spring-boot-starter-web</artifactId>"
            "<version>3.9.9</version></dependency>"
            "</dependencies></project>", encoding="utf-8")
        build_out = ("[ERROR] Could not find artifact "
                     "org.springframework.boot:spring-boot-starter-web:jar:3.9.9\n")
        _attempt_maven_version_repair(str(root), build_out, timeout=30)
        fw_txt = (root / "ruoyi-framework" / "pom.xml").read_text("utf-8")
        assert "<version>4.0.6</version>" in fw_txt, "★对齐工程家族代际（4.0.6），非仓库最近版★"
        assert "3.9.9" not in fw_txt


def test_unreachable_repo_never_prunes_family_dep(monkeypatch):
    """★fail-open 铁律★ 仓库不可达（探针 reachable=False）时，绝不能据代际差剪除依赖——
    available=[] 是"证据缺失"而非"确证查无"，断网即误剪是本系统最不能犯的错。

    RED（代际守卫误接反例）：`_family_generation_choice("4.0.6", [])` 恒返回剪除信号 →
    工程有家族先例时，一次 curl 超时就把合法依赖删了。
    """
    # 探针不可达：空列表 + reachable=False（curl/wget 双失败的真实返回形态）。
    monkeypatch.setattr(L, "_fetch_maven_versions_probe",
                        lambda g, a, p, t: ([], False))
    with tempfile.TemporaryDirectory() as d:
        root_pom, fw_pom = _mk_project(d)
        build_out = ("[ERROR] Could not find artifact "
                     "org.springframework.boot:spring-boot-starter-aop:jar:4.0.6\n")
        n, changed = _attempt_maven_version_repair(str(Path(d)), build_out, timeout=30)
        fw_txt = fw_pom.read_text("utf-8")
        root_txt = root_pom.read_text("utf-8")
        assert "spring-boot-starter-aop" in fw_txt, "★仓库不可达绝不剪合法依赖（fail-open）★"
        assert "<spring-boot.version>4.0.6</spring-boot.version>" in root_txt
        assert n == 0 and changed == [], "不可达时本轮零改动"


# ── 共享锚兜底：家族探测不到（属性钉在中间层父 pom）时，仍禁降级共享属性 ──────────
def test_dep_consumers_of_property_detects_shared_vs_private():
    """纯判据：版本写作 ${prop} 的依赖 artifactId 集合——≥2 个 = 共享平台锚。"""
    pom = ("<project><dependencies>"
           "<dependency><artifactId>acme-core</artifactId><version>${acme.version}</version></dependency>"
           "<dependency><artifactId>acme-util</artifactId><version>${acme.version}</version></dependency>"
           "<dependency><artifactId>solo</artifactId><version>${solo.version}</version></dependency>"
           "<dependency><artifactId>lit</artifactId><version>9.9</version></dependency>"
           "</dependencies></project>")
    assert _dep_consumers_of_property([pom], "acme.version") == {"acme-core", "acme-util"}
    assert _dep_consumers_of_property([pom], "solo.version") == {"solo"}, "私有属性=单一消费者"
    assert _dep_consumers_of_property([pom], "absent.version") == set()


def test_shared_property_not_downgraded_when_family_undetected(monkeypatch):
    """★兜底锁★ 工程 root 无该 group 依赖先例（_group_family_version→None，如属性钉在中间层
    父 pom），但该属性被 2 个依赖共享 → version-repair 仍必须拒绝降级共享属性。

    RED（仅有代际守卫时）：_fam=None → 落回 _choose_valid_version 选 1.9 → 把 ${acme.version}
    2.0→1.9，连坐 acme-util。
    """
    monkeypatch.setattr(L, "_fetch_maven_versions_probe",
                        lambda g, a, p, t: (["1.5", "1.9"], True))
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # root 定义共享属性，但**没有任何 com.acme 依赖声明** → _group_family_version 探测不到
        (root / "pom.xml").write_text(
            "<project><groupId>com.corp</groupId><artifactId>app</artifactId><version>1.0</version>"
            "<packaging>pom</packaging>"
            "<properties><acme.version>2.0</acme.version></properties>"
            "<modules><module>mod</module></modules></project>", encoding="utf-8")
        (root / "mod").mkdir()
        (root / "mod" / "pom.xml").write_text(
            "<project><artifactId>mod</artifactId><dependencies>"
            "<dependency><groupId>com.acme</groupId><artifactId>acme-core</artifactId>"
            "<version>${acme.version}</version></dependency>"
            "<dependency><groupId>com.acme</groupId><artifactId>acme-util</artifactId>"
            "<version>${acme.version}</version></dependency>"
            "</dependencies></project>", encoding="utf-8")
        build_out = "[ERROR] Could not find artifact com.acme:acme-core:jar:2.0\n"
        _attempt_maven_version_repair(str(root), build_out, timeout=30)
        root_txt = (root / "pom.xml").read_text("utf-8")
        assert "<acme.version>2.0</acme.version>" in root_txt, \
            "★共享属性被 ≥2 依赖引用 → 绝不因单依赖降级（兜底不变量）★"
        assert "1.9" not in root_txt
