"""P-H4c：gradle_registry 确定性解析 + gradle 脚手架 driver 接线。

锁的命题（每条都要答「哪个突变更让它红」）：
  · 工程清单证据层（零网络）：Groovy/Kotlin 字符串与 map 形态、${...} 版本剥离、
    project(":x") 内部引用不收录、根→成员(sorted)→版本目录的优先级与冲突 WARNING；
  · 版本目录 libs.versions.toml：字符串/表/version.ref 形态、共享默认最后收；
  · BOM 受管：platform/enforcedPlatform/mavenBom/Boot 插件自动导入证据 → 省略版本
    （写显式版本=对抗受管对齐，R67L-B3 gradle 形态）；
  · 显式 g:a:v=主张非证据（R67L-B3 平移）：存在→verified、查无→校正/丢弃、
    不可达→fail-open+账收编、${...}/classifier 超集→不判原样保留；
  · 内部模块绝不送仓库；【物化 project(":a:b")】（冒号段镜像目录嵌套）；
  · DSL 方言跟磁盘证据（模块已有 > 根 kts > Groovy 默认）；held 不送仓库不生成引用；
  · 接线：裸名在仓库全程 boom 下仍进权威 build 文件 ⇒ 唯一可能是清单层到达。
"""
from __future__ import annotations

import logging

import pytest

from swarm.brain import gradle_registry as gr
from swarm.brain import maven_registry as _mvn


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """默认离线：单测绝不真联网/真读 ~/.m2。maven 原语全部打桩，各用例按需覆写。"""
    monkeypatch.setenv("SWARM_MAVEN_LOOKUP", "1")  # 开关开，但网络/本地原语被打桩
    monkeypatch.setattr(_mvn, "registry_group_for", lambda a: None)
    monkeypatch.setattr(_mvn, "registry_version_exists", lambda g, a, v: None)
    monkeypatch.setattr(_mvn, "registry_latest_version", lambda g, a: None)
    monkeypatch.setattr(_mvn, "local_m2_groups_for", lambda a: set())
    monkeypatch.setattr(_mvn, "local_m2_latest_version", lambda g, a: None)
    monkeypatch.setattr(_mvn, "bom_managed_artifacts", lambda g, a, v: {})


# ── 工程清单证据层（零网络） ──────────────────────────────────────────

def test_manifest_specs_groovy_and_kts_string_forms(tmp_path):
    (tmp_path / "build.gradle").write_text(
        "dependencies {\n"
        '    implementation "org.springframework:spring-web:6.1.0"\n'
        "    api 'com.google.guava:guava:33.0.0-jre'\n"
        '    testImplementation "org.junit.jupiter:junit-jupiter:5.10.0"\n'
        '    implementation project(":core")\n'        # 内部引用绝不进版本证据
        "}\n")
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "build.gradle.kts").write_text(
        'dependencies {\n    implementation("io.ktor:ktor-server-core:2.3.7")\n}\n')
    specs = gr.project_manifest_specs(str(tmp_path))
    assert specs["spring-web"] == ("org.springframework", "6.1.0")
    assert specs["guava"] == ("com.google.guava", "33.0.0-jre")
    assert specs["junit-jupiter"] == ("org.junit.jupiter", "5.10.0")   # 配置名从宽=证据
    assert specs["ktor-server-core"] == ("io.ktor", "2.3.7")
    assert "core" not in specs


def test_manifest_specs_map_form_and_property_version(tmp_path):
    (tmp_path / "build.gradle").write_text(
        "dependencies {\n"
        "    implementation group: 'com.fasterxml.jackson.core', name: 'jackson-databind', "
        "version: '2.17.0'\n"
        '    implementation "org.example:managed-lib:${libVer}"\n'     # 属性接管 → 版本 ''
        "}\n")
    specs = gr.project_manifest_specs(str(tmp_path))
    assert specs["jackson-databind"] == ("com.fasterxml.jackson.core", "2.17.0")
    assert specs["managed-lib"] == ("org.example", ""), "${...} 不是字面版本证据"


def test_manifest_specs_priority_root_member_catalog(tmp_path, caplog):
    """根 > 成员（名字序先见先收）> 版本目录（共享默认最后收）；冲突必须 WARNING。"""
    (tmp_path / "build.gradle").write_text(
        'dependencies {\n    implementation "g:root-lib:1.0"\n}\n')
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "build.gradle").write_text(
        'dependencies {\n    implementation "g:a-lib:2.0"\n'
        '    implementation "g:conflict-lib:3.0"\n}\n')
    (tmp_path / "z").mkdir()
    (tmp_path / "z" / "build.gradle").write_text(
        'dependencies {\n    implementation "g:conflict-lib:9.9"\n}\n')
    (tmp_path / "gradle").mkdir()
    (tmp_path / "gradle" / "libs.versions.toml").write_text(
        '[libraries]\ncat-lib = "g:cat-lib:4.0"\nconflict-lib = "g:conflict-lib:5.0"\n')
    with caplog.at_level(logging.WARNING, logger="swarm.brain.gradle_registry"):
        specs = gr.project_manifest_specs(str(tmp_path))
    assert specs["root-lib"] == ("g", "1.0")
    assert specs["a-lib"] == ("g", "2.0")
    assert specs["cat-lib"] == ("g", "4.0"), "版本目录兜底直接声明没覆盖的"
    assert specs["conflict-lib"] == ("g", "3.0"), "成员 a/ 先见先收（名字序），目录与 z/ 都被盖"
    assert sum("conflict-lib" in r.getMessage() for r in caplog.records) >= 2, \
        "两处冲突（z/ 与目录）都必须留痕（硬检查④）"


def test_manifest_specs_catalog_forms(tmp_path):
    (tmp_path / "gradle").mkdir()
    (tmp_path / "gradle" / "libs.versions.toml").write_text(
        '[versions]\nspring = "6.1.0"\n'
        '[libraries]\n'
        'str-lib = "g:str-lib:1.0"\n'
        'tbl-lib = { group = "g2", name = "tbl-lib", version = "2.0" }\n'
        'ref-lib = { group = "g3", name = "ref-lib", version = { ref = "spring" } }\n'
        'noversion-lib = "g4:noversion-lib"\n')
    specs = gr.project_manifest_specs(str(tmp_path))
    assert specs["str-lib"] == ("g", "1.0")
    assert specs["tbl-lib"] == ("g2", "2.0")
    assert specs["ref-lib"] == ("g3", "6.1.0"), "version.ref 必须经 [versions] 表解析"
    assert specs["noversion-lib"] == ("g4", ""), "无版本条目=组证据，版本交版本链"


def test_manifest_specs_malformed_catalog_warns_not_silent(tmp_path, caplog):
    (tmp_path / "gradle").mkdir()
    (tmp_path / "gradle" / "libs.versions.toml").write_text("[libraries\nbroken = =")
    with caplog.at_level(logging.WARNING, logger="swarm.brain.gradle_registry"):
        specs = gr.project_manifest_specs(str(tmp_path))
    assert specs == {}
    assert any("解析失败" in r.getMessage() for r in caplog.records)


def test_manifest_specs_gated_by_lookup(monkeypatch, tmp_path):
    (tmp_path / "build.gradle").write_text(
        'dependencies {\n    implementation "g:a-lib:1.0"\n}\n')
    monkeypatch.setenv("SWARM_MAVEN_LOOKUP", "0")
    assert gr.project_manifest_specs(str(tmp_path)) == {}
    assert gr.root_bom_managed(str(tmp_path)) == {}


def test_manifest_specs_comments_not_evidence(tmp_path, caplog):
    """★reviewer R1 CR-1★ 注释里的「假声明」绝不进证据层——否则先见先收会留下注释版
    旧坐标、给真实声明打冲突 WARNING（假证据盖真证据+假警报）。"""
    (tmp_path / "build.gradle").write_text(
        '// implementation "g:commented-out:1.0"\n'
        '/* implementation "g:block-commented:2.0" */\n'
        '// platform("g:fake-bom:9.9")\n'
        'dependencies {\n    implementation "g:real-lib:4.0"  // trailing note\n}\n')
    with caplog.at_level(logging.WARNING, logger="swarm.brain.gradle_registry"):
        specs = gr.project_manifest_specs(str(tmp_path))
    assert specs == {"real-lib": ("g", "4.0")}
    assert not caplog.records, "注释假声明与真实声明之间不得有假冲突 WARNING"


def test_manifest_specs_custom_config_word_boundary(tmp_path):
    """★reviewer R1 CR-1★ 自定义配置 `someapi`/`myimplementation` 的尾部不得被当
    `api`/`implementation` 配置的证据（词边界）。"""
    (tmp_path / "build.gradle").write_text(
        'dependencies {\n    someapi "g:custom-conf:1.0"\n'
        '    myimplementation "g:custom-impl:2.0"\n'
        '    api "g:real-api:3.0"\n}\n')
    specs = gr.project_manifest_specs(str(tmp_path))
    assert specs == {"real-api": ("g", "3.0")}


def test_strip_comments_keeps_strings_and_urls():
    """剥注释的状态机尊重引号：字符串内容（坐标证据的家）原样保留，`https://` 不被
    当行注释；转义对原样保留。"""
    text = ('dependencies {\n    implementation "g:a:1.0" // note\n'
            '    maven { url = "https://repo.example.com/maven" }\n}\n')
    stripped = gr._strip_comments(text)
    assert '"g:a:1.0"' in stripped, "坐标字符串被误剥=证据层全灭"
    assert "note" not in stripped
    assert "https://repo.example.com/maven" in stripped, "URL 被当注释剥掉"


def test_manifest_specs_nested_member_via_settings_include(tmp_path):
    """★reviewer R1 #5★ 嵌套模块（services/api）的清单经 settings include 真身通道
    进证据层（一级扫描结构性抓不到）。"""
    (tmp_path / "settings.gradle").write_text(
        "include ':services:api', ':services:core'\n")
    (tmp_path / "services" / "api").mkdir(parents=True)
    (tmp_path / "services" / "api" / "build.gradle").write_text(
        'dependencies {\n    implementation "com.google.guava:guava:33.0.0-jre"\n}\n')
    specs = gr.project_manifest_specs(str(tmp_path))
    assert specs["guava"] == ("com.google.guava", "33.0.0-jre")


def test_bom_dm_plugin_requires_declaration_form(tmp_path, monkeypatch):
    """★reviewer R1 #2★ 子串提及（注释/依赖坐标）不构成 dependency-management 插件
    在场——判定必须走 plugins 块 id 声明形态。"""
    monkeypatch.setattr(_mvn, "bom_managed_artifacts",
                        lambda g, a, v: {"x-lib": "g-x"})
    (tmp_path / "build.gradle").write_text(
        '// io.spring.dependency-management plugin would go here\n'
        "plugins {\n    id 'org.springframework.boot' version '3.2.5'\n}\n"
        'dependencies {\n'
        '    implementation "io.spring.dependency-management:dm-docs:1.0"\n}\n')
    assert gr.root_bom_managed(str(tmp_path)) == {}, \
        "注释提及+坐标提及都不构成插件在场（幻觉受管=版本被静默省略）"


def test_bom_boot_apply_false_not_managed(tmp_path, monkeypatch):
    """★reviewer R1 #4 / hunter R1 M★ `apply false` 的 Boot 插件不应用 → 不自动导入
    Boot BOM（幻觉受管=版本被静默省略）。"""
    monkeypatch.setattr(_mvn, "bom_managed_artifacts",
                        lambda g, a, v: {"x-lib": "g-x"})
    (tmp_path / "build.gradle").write_text(
        "plugins {\n    id 'org.springframework.boot' version '3.2.5' apply false\n"
        "    id 'io.spring.dependency-management' version '1.1.4'\n}\n")
    assert gr.root_bom_managed(str(tmp_path)) == {}
    # 对照：去掉 apply false → 导入（同一证据源，方向可辨）
    (tmp_path / "build.gradle").write_text(
        "plugins {\n    id 'org.springframework.boot' version '3.2.5'\n"
        "    id 'io.spring.dependency-management' version '1.1.4'\n}\n")
    assert gr.root_bom_managed(str(tmp_path)) == {"x-lib": "g-x"}


def test_bom_boot_plugin_decl_word_boundary(tmp_path, monkeypatch):
    """★reviewer R2 HIGH-1★ 自定义标识符 `myid` 尾部的 `id` 子串不得当插件声明
    （假在场=幻觉受管，版本被静默省略）。"""
    monkeypatch.setattr(_mvn, "bom_managed_artifacts",
                        lambda g, a, v: {"x-lib": "g-x"})
    (tmp_path / "build.gradle").write_text(
        "plugins {\n    myid 'org.springframework.boot' version '3.2.5'\n"
        "    id 'io.spring.dependency-management' version '1.1.4'\n}\n")
    assert gr.root_bom_managed(str(tmp_path)) == {}, \
        "myid 的 id 子串不构成 Boot 插件声明"


def test_bom_boot_kotlin_dsl_forms(tmp_path, monkeypatch):
    """★reviewer R2 HIGH-2 + hunter R2 HIGH★ Kotlin DSL：点号链
    `id("x").version("y")` 要拿到版本；`apply(false)`/`.apply(false)` 要判未应用——
    只认 Groovy 空格形态=版本丢失（保守但错）且 apply(false) 被当真应用（幻觉受管，
    方向危险）。"""
    monkeypatch.setattr(_mvn, "bom_managed_artifacts",
                        lambda g, a, v: {"x-lib": "g-x"})
    # 点号链 .apply(false) → 不导入
    (tmp_path / "build.gradle.kts").write_text(
        'plugins {\n    id("org.springframework.boot").version("3.2.5").apply(false)\n'
        '    id("io.spring.dependency-management").version("1.1.4")\n}\n')
    assert gr.root_bom_managed(str(tmp_path)) == {}
    # 空格形态 apply(false) → 不导入（hunter probe 形态）
    (tmp_path / "build.gradle.kts").write_text(
        'plugins { id("org.springframework.boot") version("3.2.5") apply(false)\n'
        '    id("io.spring.dependency-management") version("1.1.4") }\n')
    assert gr.root_bom_managed(str(tmp_path)) == {}
    # 对照：去掉 apply(false) → 导入（版本经点号链拿到，同一证据源方向可辨）
    (tmp_path / "build.gradle.kts").write_text(
        'plugins {\n    id("org.springframework.boot").version("3.2.5")\n'
        '    id("io.spring.dependency-management").version("1.1.4")\n}\n')
    assert gr.root_bom_managed(str(tmp_path)) == {"x-lib": "g-x"}


def test_bom_boot_string_literal_not_a_decl(tmp_path, monkeypatch):
    """★reviewer R2 #3 + hunter R2 MEDIUM★ 判定限定 plugins 块——字符串字面量里的
    类插件声明文本不是声明（_strip_comments 刻意保留字符串，坐标证据住在里面）。"""
    monkeypatch.setattr(_mvn, "bom_managed_artifacts",
                        lambda g, a, v: {"x-lib": "g-x"})
    (tmp_path / "build.gradle").write_text(
        'def note = "id \'org.springframework.boot\' version \'3.2.5\'"\n'
        "plugins {\n    id 'io.spring.dependency-management' version '1.1.4'\n}\n")
    assert gr.root_bom_managed(str(tmp_path)) == {}, \
        "字符串字面量里的插件声明文本不构成 Boot 插件在场"


def test_bom_boot_applied_decl_wins_over_apply_false(tmp_path, monkeypatch):
    """★reviewer R2 #4★ 同一插件多条声明：已应用者优先——re.search 首匹配会把靠前的
    `apply false` 行当真，盖掉后面的真应用行（幻觉不受管=受管对齐被写版本号污染）。"""
    monkeypatch.setattr(_mvn, "bom_managed_artifacts",
                        lambda g, a, v: {"x-lib": "g-x"})
    (tmp_path / "build.gradle").write_text(
        "plugins {\n    id 'org.springframework.boot' version '3.2.5' apply false\n"
        "    id 'org.springframework.boot' version '3.2.6'\n"
        "    id 'io.spring.dependency-management' version '1.1.4'\n}\n")
    assert gr.root_bom_managed(str(tmp_path)) == {"x-lib": "g-x"}


def test_manifest_specs_include_without_leading_colon(tmp_path):
    """★reviewer R2 #5★ `include 'services:api'`（无前导冒号）与带冒号写法等价——
    只认带冒号=嵌套成员证据通道对无冒号写法整层失踪。"""
    (tmp_path / "settings.gradle").write_text("include 'services:api'\n")
    (tmp_path / "services" / "api").mkdir(parents=True)
    (tmp_path / "services" / "api" / "build.gradle").write_text(
        'dependencies {\n    implementation "com.google.guava:guava:33.0.0-jre"\n}\n')
    specs = gr.project_manifest_specs(str(tmp_path))
    assert specs["guava"] == ("com.google.guava", "33.0.0-jre")


def test_ph4c_gradle_dialect_plan_writable_channel(tmp_path, monkeypatch):
    """★reviewer R2 #6★ plan 方言证据收 create_files ∪ writable——清单只声明在
    writable 时方言仍跟 plan（根 Groovy 不得盖过 plan 的 kts 意图）。
    （scope=None 臂无法测试：SubTask.scope 是 pydantic 必填字段，生产构造不出 None，
    getattr 防御与 contract_utils 既有 normalize 惯例一致，诚实登记。）"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    from swarm.types import FileScope
    (tmp_path / "settings.gradle").write_text("include ':services:api'\n")
    monkeypatch.setattr(_mvn, "registry_group_for", lambda a: "com.google.guava")
    monkeypatch.setattr(_mvn, "registry_latest_version", lambda g, a: "33.4.8-jre")
    plan = _gradle_plan()
    st1 = plan.subtasks[0]
    st1.scope = FileScope(writable=["services/api/build.gradle.kts"],
                          create_files=list(st1.scope.create_files or []))
    inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    all_desc = "\n".join(str(getattr(st, "description", "") or "") for st in plan.subtasks)
    assert "```kotlin" in all_desc, \
        "writable 里声明的 kts 清单没进方言证据（被根 Groovy 盖过）"
    assert 'implementation("com.google.guava:guava:33.4.8-jre")' in all_desc


def test_plugin_decl_no_cross_line_version_attach():
    """连接器只许横向空白（[ \\t]）——块内下行的 `version 'x'` 是另一条语句，
    不得跨行挂到上方插件声明头上（`\\s*` 含换行=错挂版本）。"""
    text = "plugins {\n    id 'org.springframework.boot'\n    version '3.2.5'\n}\n"
    assert gr._plugin_decl(text, "org.springframework.boot") == (None, True)


def test_plugins_block_head_accepts_newline(tmp_path, monkeypatch):
    """★reviewer R3 MEDIUM★ `plugins` 与 `{` 之间换行是 DSL 合法风格——`[ \\t]*`
    块头会把整块证据丢掉（BOM 受管判定全失效，版本被错写/错丢）。"""
    monkeypatch.setattr(_mvn, "bom_managed_artifacts",
                        lambda g, a, v: {"x-lib": "g-x"})
    (tmp_path / "build.gradle").write_text(
        "plugins\n{\n    id 'org.springframework.boot' version '3.2.5'\n"
        "    id 'io.spring.dependency-management' version '1.1.4'\n}\n")
    assert gr.root_bom_managed(str(tmp_path)) == {"x-lib": "g-x"}


def test_bom_boot_kotlin_chain_multiline(tmp_path, monkeypatch):
    """★hunter R3 HIGH/MEDIUM★ Kotlin 链允许在点前换行：跨行 `.apply(false)` 必须
    判未应用（漏检=幻觉受管，误杀方向）；跨行 `.version(...)` 必须拿到版本。
    对照组=非点号的跨行 version 绝不挂（X1 同盘两方向可辨）。"""
    monkeypatch.setattr(_mvn, "bom_managed_artifacts",
                        lambda g, a, v: {"x-lib": "g-x"})
    # 跨行 .apply(false) → 不导入
    (tmp_path / "build.gradle.kts").write_text(
        'plugins {\n    id("org.springframework.boot") version("3.2.5")\n'
        '        .apply(false)\n'
        '    id("io.spring.dependency-management") version("1.1.4")\n}\n')
    assert gr.root_bom_managed(str(tmp_path)) == {}
    # 跨行 .version(...) 拿到版本 + 无 apply(false) → 导入
    (tmp_path / "build.gradle.kts").write_text(
        'plugins {\n    id("org.springframework.boot")\n'
        '        .version("3.2.5")\n'
        '    id("io.spring.dependency-management") version("1.1.4")\n}\n')
    assert gr.root_bom_managed(str(tmp_path)) == {"x-lib": "g-x"}


def test_render_includes_repositories_block():
    """★reviewer R1 #3★ CREATE 模板必须带 repositories——gradle 零默认仓库，greenfield
    缺它=自败脚手架（验收的 gradlew dependencies 必炸）。"""
    from swarm.brain.contract_utils import _render_build_gradle
    dep = gr.ResolvedGradleDep("com.google.guava", "guava", "33.0.0-jre", "maven_central")
    for dialect in ("groovy", "kts"):
        text = _render_build_gradle(dialect, [dep], [])
        assert "repositories {" in text and "mavenCentral()" in text


def test_gradle_dialect_follows_plan_create_files(tmp_path):
    """★reviewer R1 #6★ 根 Groovy 而 plan 在该模块明确建 .kts → 方言跟 plan 事实
    （根证据不能盖 plan）；磁盘已有仍最优先。"""
    from swarm.brain.contract_utils import _gradle_dialect
    (tmp_path / "settings.gradle").write_text("include ':services:api'\n")   # 根 Groovy
    assert _gradle_dialect(str(tmp_path), "services/api",
                           {"services/api/build.gradle.kts"}) == "kts"
    assert _gradle_dialect(str(tmp_path), "services/api",
                           {"services/api/build.gradle"}) == "groovy"
    (tmp_path / "services" / "api").mkdir(parents=True)
    (tmp_path / "services" / "api" / "build.gradle.kts").write_text("")
    assert _gradle_dialect(str(tmp_path), "services/api",
                           {"services/api/build.gradle"}) == "kts", "磁盘事实 > plan 声明"


def test_record_unverified_ledger_consumes_raw_coord():
    """★hunter R1 HIGH★ raw（classifier 超集等不判形态）进 dep_versions_unverified 账
    时必须带原坐标——记成 `?@?`=账在但认不出是谁（go 侧同型血泪）。"""
    from swarm.brain.contract_utils import _record_unverified_deps
    dep = gr.ResolvedGradleDep("", "", None, source="explicit",
                               verified="unjudgeable", raw="g:b-lib:1.0:test-fixtures")
    out: dict = {}
    _record_unverified_deps(out, "api", [dep])
    assert out["api"] == ["g:b-lib:1.0:test-fixtures@<无版本>(unjudgeable)"]


def test_ph4c_gradle_raw_unjudgeable_in_template_and_contract(tmp_path, monkeypatch):
    """raw 依赖进模板【且】进契约终单（模板有、契约没有=账物不符的另一方向）。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    (tmp_path / "settings.gradle").write_text("include ':services:api'\n")
    plan = _gradle_plan(artifacts=("g:b-lib:1.0:test-fixtures",))
    inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    scaffold = next(st for st in plan.subtasks if st.id == "st-scaffold-api")
    assert 'implementation "g:b-lib:1.0:test-fixtures"' in scaffold.description
    api_entry = next(e for e in plan.shared_contract["dependencies"] if e["module"] == "api")
    assert "g:b-lib:1.0:test-fixtures" in api_entry["artifacts"]


# ── BOM 受管（R67L-B3 gradle 形态：受管=省略版本） ─────────────────────

def test_bom_managed_platform_and_boot_autoload(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(_mvn, "bom_managed_artifacts",
                        lambda g, a, v: calls.append((g, a, v)) or {"x-lib": "g-x"})
    (tmp_path / "build.gradle").write_text(
        "plugins {\n    id 'org.springframework.boot' version '3.2.5'\n"
        "    id 'io.spring.dependency-management' version '1.1.4'\n}\n"
        "dependencies {\n"
        '    implementation platform("com.fasterxml.jackson:jackson-bom:2.17.0")\n'
        "}\n")
    managed = gr.root_bom_managed(str(tmp_path))
    assert managed == {"x-lib": "g-x"}
    assert ("com.fasterxml.jackson", "jackson-bom", "2.17.0") in calls
    assert ("org.springframework.boot", "spring-boot-dependencies", "3.2.5") in calls, \
        "Boot 插件+dependency-management 在场 → Boot BOM 自动导入是确定性证据"


def test_bom_managed_requires_dm_plugin_for_boot_autoload(tmp_path, monkeypatch):
    """只有 Boot 插件、没有 dependency-management 插件 → gradle 不会自动受管 → 不猜。"""
    monkeypatch.setattr(_mvn, "bom_managed_artifacts",
                        lambda g, a, v: {"x-lib": "g-x"})
    (tmp_path / "build.gradle").write_text(
        "plugins {\n    id 'org.springframework.boot' version '3.2.5'\n}\n")
    assert gr.root_bom_managed(str(tmp_path)) == {}, \
        "dependency-management 缺席 → Boot BOM 自动导入不成立（猜=幻觉受管）"


def test_resolve_bare_bom_managed_omits_version(tmp_path, monkeypatch):
    """受管坐标写【无版本】声明——写显式版本=对抗受管对齐（R67L-B3 gradle 形态）。"""
    (tmp_path / "build.gradle").write_text(
        'dependencies {\n    implementation platform("g-x:x-bom:1.0")\n}\n')
    monkeypatch.setattr(_mvn, "bom_managed_artifacts",
                        lambda g, a, v: {"x-lib": "g-x"} if a == "x-bom" else {})
    def boom(a):
        raise AssertionError("受管已答 ⇒ Central 反查不该被咨询")
    monkeypatch.setattr(_mvn, "registry_group_for", boom)
    kept, _, dropped = gr.resolve_gradle_deps(["x-lib"], project_path=str(tmp_path))
    assert [(k.group, k.artifact, k.version, k.source) for k in kept] == [
        ("g-x", "x-lib", None, "bom_managed")]
    assert dropped == []


# ── resolve：显式 g:a[:v] = 主张非证据（R67L-B3 平移） ─────────────────

def test_resolve_explicit_pin_verified(monkeypatch):
    monkeypatch.setattr(_mvn, "registry_version_exists", lambda g, a, v: True)
    kept, _, dropped = gr.resolve_gradle_deps(["com.google.guava:guava:33.0.0-jre"])
    assert [(k.group, k.artifact, k.version, k.source, k.verified) for k in kept] == [
        ("com.google.guava", "guava", "33.0.0-jre", "explicit", "verified")]
    assert dropped == []


def test_resolve_explicit_hallucinated_corrects_to_latest(monkeypatch, caplog):
    monkeypatch.setattr(_mvn, "registry_version_exists", lambda g, a, v: False)
    monkeypatch.setattr(_mvn, "registry_latest_version", lambda g, a: "33.4.8-jre")
    with caplog.at_level(logging.WARNING, logger="swarm.brain.gradle_registry"):
        kept, _, dropped = gr.resolve_gradle_deps(["com.google.guava:guava:99.0"])
    assert [(k.artifact, k.version, k.source) for k in kept] == [
        ("guava", "33.4.8-jre", "maven_central")]
    assert dropped == []
    assert any("幻觉版本" in r.getMessage() for r in caplog.records)


def test_resolve_explicit_hallucinated_no_latest_dropped(monkeypatch):
    monkeypatch.setattr(_mvn, "registry_version_exists", lambda g, a, v: False)
    kept, _, dropped = gr.resolve_gradle_deps(["g:x-lib:99.0"])
    assert kept == [] and dropped == ["g:x-lib:99.0"]


def test_resolve_explicit_unreachable_failopen_kept(monkeypatch, caplog):
    monkeypatch.setattr(_mvn, "registry_version_exists", lambda g, a, v: None)
    with caplog.at_level(logging.WARNING, logger="swarm.brain.gradle_registry"):
        kept, _, dropped = gr.resolve_gradle_deps(["g:x-lib:1.0"])
    assert [(k.artifact, k.version, k.verified) for k in kept] == [
        ("x-lib", "1.0", "registry_unreachable")]
    assert dropped == []
    assert any("不可达" in r.getMessage() for r in caplog.records)


def test_resolve_explicit_no_version_resolves_latest(monkeypatch):
    monkeypatch.setattr(_mvn, "registry_latest_version",
                        lambda g, a: "2.3.7" if a == "ktor-server-core" else None)
    kept, _, dropped = gr.resolve_gradle_deps(["io.ktor:ktor-server-core"])
    assert [(k.group, k.artifact, k.version) for k in kept] == [
        ("io.ktor", "ktor-server-core", "2.3.7")]
    assert dropped == []


def test_resolve_unjudgeable_forms_kept_verbatim(monkeypatch):
    """${...} 属性引用与 classifier 超集形态 → 不判原样保留（猜语义误杀比放过更坏）。"""
    def boom(g, a, v=None):
        raise AssertionError("不判形态不该咨询仓库")
    monkeypatch.setattr(_mvn, "registry_version_exists", boom)
    monkeypatch.setattr(_mvn, "registry_latest_version", lambda g, a: (_ for _ in ()).throw(
        AssertionError("不判形态不该咨询仓库")))
    kept, _, dropped = gr.resolve_gradle_deps(
        ["g:a-lib:${libVer}", "g:b-lib:1.0:test-fixtures"])
    assert kept[0].raw == "" and kept[0].version == "${libVer}" \
        and kept[0].verified == "unjudgeable"
    assert kept[1].raw == "g:b-lib:1.0:test-fixtures" and kept[1].verified == "unjudgeable"
    assert dropped == []


def test_resolve_illegal_coord_chars_dropped(caplog):
    """引号/反斜杠坐标=模板注入面 → 如实丢弃 + WARNING（绝不进 Groovy/Kotlin 字符串）。"""
    with caplog.at_level(logging.WARNING, logger="swarm.brain.gradle_registry"):
        kept, _, dropped = gr.resolve_gradle_deps(['g:evil":1.0'])
    assert kept == [] and dropped == ['g:evil":1.0']
    assert any("非法坐标" in r.getMessage() for r in caplog.records)


# ── resolve：裸名判定序（清单 → BOM → Central → 本地 .m2） ─────────────

def test_resolve_bare_manifest_wins_zero_network(tmp_path, monkeypatch):
    (tmp_path / "build.gradle").write_text(
        'dependencies {\n    implementation "com.google.guava:guava:33.0.0-jre"\n}\n')
    def boom(a):
        raise AssertionError("清单已答 ⇒ Central 不该被咨询")
    monkeypatch.setattr(_mvn, "registry_group_for", boom)
    monkeypatch.setattr(_mvn, "registry_latest_version",
                        lambda g, a: (_ for _ in ()).throw(AssertionError("清单已答")))
    kept, _, dropped = gr.resolve_gradle_deps(["guava"], project_path=str(tmp_path))
    assert [(k.group, k.artifact, k.version, k.source) for k in kept] == [
        ("com.google.guava", "guava", "33.0.0-jre", "project_manifest")]
    assert dropped == []


def test_resolve_bare_registry_unique_group_then_latest(monkeypatch):
    monkeypatch.setattr(_mvn, "registry_group_for", lambda a: "io.ktor")
    monkeypatch.setattr(_mvn, "registry_latest_version", lambda g, a: "2.3.7")
    kept, _, dropped = gr.resolve_gradle_deps(["ktor-server-core"])
    assert [(k.group, k.artifact, k.version, k.source) for k in kept] == [
        ("io.ktor", "ktor-server-core", "2.3.7", "maven_central")]
    assert dropped == []


def test_resolve_bare_local_m2_fallback(monkeypatch):
    """Central 反查不到 → 本地 .m2 唯一 group + 最新版（离线兜底证据，node_modules 同型）。"""
    monkeypatch.setattr(_mvn, "local_m2_groups_for", lambda a: {"com.acme"})
    monkeypatch.setattr(_mvn, "local_m2_latest_version", lambda g, a: "0.9.1")
    kept, _, dropped = gr.resolve_gradle_deps(["acme-lib"])
    assert [(k.group, k.artifact, k.version, k.source) for k in kept] == [
        ("com.acme", "acme-lib", "0.9.1", "local_m2")]
    assert dropped == []


def test_resolve_bare_unresolvable_dropped(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="swarm.brain.gradle_registry"):
        kept, _, dropped = gr.resolve_gradle_deps(["ghost-lib"])
    assert kept == [] and dropped == ["ghost-lib"]
    assert any("如实丢弃" in r.getMessage() for r in caplog.records)


def test_resolve_internal_never_hits_registry(monkeypatch):
    def boom(a):
        raise AssertionError("内部模块被送去仓库反查了")
    monkeypatch.setattr(_mvn, "registry_group_for", boom)
    kept, internal, dropped = gr.resolve_gradle_deps(
        ["core", "auth"], internal_modules={"core", "auth"})
    assert kept == [] and sorted(internal) == ["auth", "core"] and dropped == []


# ── 模板渲染与方言 ────────────────────────────────────────────────────

def test_render_build_gradle_groovy_vs_kts():
    from swarm.brain.contract_utils import _render_build_gradle
    dep = gr.ResolvedGradleDep("com.google.guava", "guava", "33.0.0-jre", "maven_central")
    managed = gr.ResolvedGradleDep("org.springframework", "spring-web", None, "bom_managed")
    groovy = _render_build_gradle("groovy", [dep, managed], [":services:core"])
    assert 'id \'java\'' in groovy
    assert 'implementation "com.google.guava:guava:33.0.0-jre"' in groovy
    assert 'implementation "org.springframework:spring-web"' in groovy, "受管=无版本坐标"
    assert 'implementation project(":services:core")' in groovy
    kts = _render_build_gradle("kts", [dep, managed], [":services:core"])
    assert "\n    java\n" in kts
    assert 'implementation("com.google.guava:guava:33.0.0-jre")' in kts
    assert 'implementation(project(":services:core"))' in kts


def test_gradle_dialect_evidence_chain(tmp_path):
    from swarm.brain.contract_utils import _gradle_dialect
    # 模块已有清单 → 从其扩展名
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "build.gradle.kts").write_text("")
    assert _gradle_dialect(str(tmp_path), "a") == "kts"
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "build.gradle").write_text("")
    (tmp_path / "settings.gradle.kts").write_text("")   # 根 kts 证据盖不过模块已有 groovy
    assert _gradle_dialect(str(tmp_path), "b") == "groovy"
    # greenfield 模块 → 根 kts 证据
    assert _gradle_dialect(str(tmp_path), "newmod") == "kts"
    # 零证据 → Groovy 默认
    assert _gradle_dialect(None, "x") == "groovy"


# ── driver 接线锁（经真实调用链） ─────────────────────────────────────

def _gradle_plan(*, artifacts=("guava",), modules=("api",), layout="services"):
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan
    plan = TaskPlan(
        subtasks=[SubTask(id="st-1", description="task st-1",
                          difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=[],
                                          create_files=[f"{layout}/api/src/main/java"
                                                        f"/com/demo/A.java"]))],
        parallel_groups=[["st-1"]])
    deps = [{"module": m, "artifacts": list(artifacts) if m == "api" else []}
            for m in modules]
    plan.shared_contract = {"dependencies": deps}
    return plan


def test_ph4c_gradle_manifest_spec_reaches_scaffold_via_real_caller(tmp_path, monkeypatch):
    """★接线锁（血规 10 第一条）★ 仓库全程 boom；裸名仍进权威 build.gradle ⇒ 唯一可能
    是工程清单层经真实调用链到达。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    (tmp_path / "settings.gradle").write_text("include ':services:api'\n")
    (tmp_path / "build.gradle").write_text(
        'dependencies {\n    implementation "com.google.guava:guava:33.0.0-jre"\n}\n')
    def boom(a):
        raise AssertionError("清单已答 ⇒ Central 不该被咨询（接线断了才会走到这里）")
    monkeypatch.setattr(_mvn, "registry_group_for", boom)
    monkeypatch.setattr(_mvn, "registry_latest_version",
                        lambda g, a: (_ for _ in ()).throw(AssertionError("清单已答")))
    plan = _gradle_plan(artifacts=("guava",))
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    assert injected, "夹具没走到 gradle driver（零注入 ⇒ 这条测试什么也没证明）"
    scaffold = next(st for st in plan.subtasks if st.id == "st-scaffold-api")
    assert 'implementation "com.google.guava:guava:33.0.0-jre"' in scaffold.description, \
        "清单声明没进权威 build.gradle 模板 ⇒ P-H4c 证据层在生产链路上是断的"
    assert any(st.scope.create_files == ["services/api/build.gradle"]
               for st in plan.subtasks if st.id == "st-scaffold-api")
    # 验收措辞含 gradlew 依赖解析闸
    assert any("gradlew" in ac and "compileClasspath" in ac
               for ac in scaffold.acceptance_criteria)


def test_ph4c_gradle_internal_dep_materialized_as_project(tmp_path, monkeypatch):
    """内部模块【物化 project(":a:b")】（冒号段镜像目录嵌套）；且绝不送仓库。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    (tmp_path / "settings.gradle").write_text(
        "include ':services:api'\ninclude ':services:core'\n")
    def boom(a):
        raise AssertionError("内部模块被送去 Central 反查了")
    monkeypatch.setattr(_mvn, "registry_group_for", boom)
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan
    plan = TaskPlan(
        subtasks=[SubTask(id="st-1", description="t", difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=[], create_files=[
                              "services/api/src/main/java/com/demo/A.java"])),
                  SubTask(id="st-2", description="t", difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=[], create_files=[
                              "services/core/src/main/java/com/demo/B.java"]))],
        parallel_groups=[["st-1"], ["st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "api", "artifacts": ["core"]},
        {"module": "core", "artifacts": []}]}
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    assert injected, "夹具没走到 gradle driver"
    api_scaffold = next(st for st in plan.subtasks if st.id == "st-scaffold-api")
    assert 'implementation project(":services:core")' in api_scaffold.description
    api_entry = next(e for e in plan.shared_contract["dependencies"] if e["module"] == "api")
    assert "core" in api_entry["artifacts"]


def test_ph4c_gradle_unresolved_internal_label_held(tmp_path, monkeypatch, caplog):
    """★hunter R2 H-1 同律★ 契约声明了但解析不出物理落点的模块：held 扣下——不送
    仓库、不生成 project 引用（无落点=臆造路径），留契约 + WARNING 机读可辨。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    (tmp_path / "settings.gradle").write_text("include ':services:api'\n")
    hits: list[str] = []
    monkeypatch.setattr(_mvn, "registry_group_for",
                        lambda a: hits.append(a) or None)
    plan = _gradle_plan(artifacts=("ghost",))
    plan.shared_contract["dependencies"].append({"module": "ghost", "artifacts": []})
    with caplog.at_level(logging.WARNING, logger="swarm.brain.contract_utils"):
        inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    api_scaffold = next(st for st in plan.subtasks if st.id == "st-scaffold-api")
    assert "ghost" not in api_scaffold.description
    assert not any("ghost" in u for u in hits), "held 模块被送去仓库反查了"
    assert any("#31-P2f" in r.getMessage() and "无物理落点" in r.getMessage()
               for r in caplog.records)


def test_ph4c_gradle_explicit_hallucinated_version_never_in_template(tmp_path, monkeypatch):
    """R67L-B3 平移的生产链路锁：`guava:99.0` 未经核验烤进权威模板=派 worker 去失败。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    (tmp_path / "settings.gradle").write_text("include ':services:api'\n")
    monkeypatch.setattr(_mvn, "registry_version_exists", lambda g, a, v: False)
    monkeypatch.setattr(_mvn, "registry_latest_version", lambda g, a: "33.4.8-jre")
    plan = _gradle_plan(artifacts=("com.google.guava:guava:99.0",))
    inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    scaffold = next(st for st in plan.subtasks if st.id == "st-scaffold-api")
    assert "99.0" not in scaffold.description
    assert 'implementation "com.google.guava:guava:33.4.8-jre"' in scaffold.description


def test_ph4c_gradle_kts_dialect_reaches_scaffold(tmp_path, monkeypatch):
    """根 kts 证据 → greenfield 模块建 build.gradle.kts（Kotlin DSL 形态）。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    (tmp_path / "settings.gradle.kts").write_text('include(":services:api")\n')
    (tmp_path / "build.gradle.kts").write_text(
        'dependencies {\n    implementation("io.ktor:ktor-server-core:2.3.7")\n}\n')
    monkeypatch.setattr(_mvn, "registry_group_for",
                        lambda a: (_ for _ in ()).throw(AssertionError("清单已答")))
    plan = _gradle_plan(artifacts=("ktor-server-core",))
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    assert injected
    scaffold = next(st for st in plan.subtasks if st.id == "st-scaffold-api")
    assert 'implementation("io.ktor:ktor-server-core:2.3.7")' in scaffold.description
    assert scaffold.scope.create_files == ["services/api/build.gradle.kts"]


def test_ph4c_gradle_label_vs_dir_name_internal_still_materialized(tmp_path, monkeypatch):
    """契约 artifact 写【模块标签】而目录名不同（标签 `my-core` vs 目录 `services/core`）：
    内部判定认标签（不送仓库），且 project 引用物化到【目录名】工程路径——归一删除=
    引用静默丢失（留契约但清单里没有，gradlew 解析必然炸）。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    (tmp_path / "settings.gradle").write_text(
        "include ':services:api'\ninclude ':services:core'\n")
    def boom(a):
        raise AssertionError("内部模块（标签形）被送去 Central 反查了")
    monkeypatch.setattr(_mvn, "registry_group_for", boom)
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan
    plan = TaskPlan(
        subtasks=[SubTask(id="st-1", description="t", difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=[], create_files=[
                              "services/api/src/main/java/com/demo/A.java"])),
                  SubTask(id="st-2", description="t", difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=[], create_files=[
                              "services/core/src/main/java/com/demo/B.java"]))],
        parallel_groups=[["st-1"], ["st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "my-api", "artifacts": ["my-core"]},
        {"module": "my-core", "artifacts": []}]}
    # 标签 my-api/my-core 也要能解析出物理落点（dirs 以标签为键）——直接给 driver 视角
    dirs = {"my-api": "services/api", "my-core": "services/core"}
    import swarm.brain.contract_utils as cu
    monkeypatch.setattr(cu, "_module_physical_dirs", lambda *a, **k: dirs)
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    assert injected, "夹具没走到 gradle driver"
    api_scaffold = next(st for st in plan.subtasks if st.id == "st-scaffold-my-api")
    assert 'implementation project(":services:core")' in api_scaffold.description, \
        "标签≠目录名时 project 引用必须物化到目录名工程路径（归一被删=静默丢失）"


def test_ph4c_gradle_owner_backfill_path(tmp_path, monkeypatch):
    """cr#1 同律：build.gradle 已被 owner 子任务认领 → backfill 嵌块，不另立 scaffold。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    (tmp_path / "settings.gradle").write_text("include ':services:api'\n")
    monkeypatch.setattr(_mvn, "registry_group_for", lambda a: "com.google.guava")
    monkeypatch.setattr(_mvn, "registry_latest_version", lambda g, a: "33.4.8-jre")
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan
    plan = TaskPlan(
        subtasks=[SubTask(id="st-1", description="task st-1",
                          difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=[],
                                          create_files=["services/api/build.gradle",
                                                        "services/api/src/main/java"
                                                        "/com/demo/A.java"]))],
        parallel_groups=[["st-1"]])
    plan.shared_contract = {"dependencies": [{"module": "api", "artifacts": ["guava"]}]}
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    assert not any(st.id == "st-scaffold-api" for st in plan.subtasks), \
        "owner 已认领就该 backfill，不该另立 scaffold"
    st1 = next(st for st in plan.subtasks if st.id == "st-1")
    assert 'implementation "com.google.guava:guava:33.4.8-jre"' in st1.description, \
        "确定性清单块没嵌进 owner description"
