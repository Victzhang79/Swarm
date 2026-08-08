"""#29-5 W-8：dep_legality 的 gradle / python(requirements) 两臂曾是死代码——

修复前（29 号文 W-8，本批起跑时已用 spy 夹具逐条复测坐实）：
① gradle 臂 `_members` 只认 settings.gradle*，而 find 只捞 build.gradle
   ⇒ settings 永不在提取器输入里 ⇒ members 恒空；
② python 臂选 requirements.txt 时，`_members` 只在 rel=="pyproject.toml" 时返回，
   而 texts 全是 requirements ⇒ members 恒空；
③ 纯 Kotlin DSL 工程：分派条件命中 .kts，但 generic 臂 manifest_name="build.gradle"
   ⇒ root_text=None 早返，一条 find 都不发；
而 `dep_legality.classify` 第一行即 `if not workspace_members: return "legal"`（fail-open）
⇒ members 恒空 = 每条依赖无条件判 legal = 两臂对任何输入零处置（硬检查①教科书形态）。

治法：generic 臂 `manifest_name` 支持别名元组 + 新增 `member_source_names`（成员事实源
由 generic 臂单独读入提取器，不进 enforce 改写集）；提取异常 / 有证据却解析为零
落机读键 [members_extraction_failed] / [members_empty] + WARNING。

区分力设计（判据：把修复整块回退，对应测试必须红）：
- 每个栈的清单都放【工作区成员依赖 + 幻影依赖】各一条，registry 全部确证查无：
  · 臂死掉（①②③任一回退）⇒ 幻影存活 ⇒ 红；
  · members 提取死掉 ⇒ 成员依赖也被当幻影剪掉 ⇒ 红。
- 本文件第一条四栈用例 supersede 了 test_r56_dep_legality.py 原 :594
  `test_l1_pipeline_enforces_cargo_go_gradle_python_arms`——那条断言 `n == 0 and changed == []`
  且 find 桩只回 package.json，臂真跑与臂死掉返回值一模一样（零区分力），已删除。
"""
import logging
import re as _re

import pytest

import swarm.worker.dep_legality as dl
import swarm.worker.l1_pipeline as lp


# ── 忠实接线桩：find 真按 -name/-not -path 过滤（旧 r56 桩恒回 package.json = 臂全空转）──

def _wire_project(monkeypatch, files: dict, *, spy_extractor: dict | None = None,
                  unreadable: "set[str] | None" = None):
    """把 l1_pipeline 的沙箱读写/扫描桩成本地 dict；find 按命令里的 -name 忠实过滤。

    spy_extractor: 传一个 dict 则把 generic 臂的 members 提取器包一层，
    记录每次提取器收到的输入 rel 清单到 spy_extractor["inputs"]（可令其 raise）。
    unreadable: 这些 rel 存在（test -f 为真）但读取失败（返 None）——复核 F5 形态。
    """
    unreadable = unreadable or set()
    monkeypatch.setattr(lp, "_read_project_file",
                        lambda pp, rel, timeout=20:
                        None if rel in unreadable else files.get(rel))
    written: dict = {}
    monkeypatch.setattr(lp, "_write_project_file",
                        lambda pp, rel, content, timeout=20:
                        (files.__setitem__(rel, content),
                         written.__setitem__(rel, content), True)[-1])

    def _fake_check(cmd, pp, timeout=60):
        if cmd.startswith("test -f "):
            import shlex as _sh
            rel = _sh.split(cmd, posix=True)[2]
            return (0, "", "") if rel in files else (1, "", "")
        names = _re.findall(r"-name\s+(\S+)", cmd)
        frags = _re.findall(r"-not -path '\*([^']+)\*'", cmd)
        hits = [r for r in sorted(files)
                if any(r.rsplit("/", 1)[-1] == n for n in names)
                and not any(f in "/" + r for f in frags)]
        return (0, "".join(f"{r}\n" for r in hits), "")

    monkeypatch.setattr(lp, "_run_check_split", _fake_check)

    if spy_extractor is not None:
        orig_generic = lp._enforce_dep_legality_generic

        def _spy(project_path, timeout, **kw):
            wmt = kw.get("workspace_members_from_texts")
            if wmt is not None:
                def _wrapped(texts):
                    spy_extractor.setdefault("inputs", []).append(sorted(texts))
                    if spy_extractor.get("raise"):
                        raise RuntimeError("夹具注入的提取失败")
                    return wmt(texts)
                kw["workspace_members_from_texts"] = _wrapped
            return orig_generic(project_path, timeout, **kw)

        monkeypatch.setattr(lp, "_enforce_dep_legality_generic", _spy)
    return written


def _registries_all_empty(monkeypatch):
    """四栈 registry 全部【确证查无】（[] / ([], True)）——非 None（None=不可达 fail-open）。"""
    monkeypatch.setattr(dl, "cargo_registry_versions_list", lambda _ns, _n: [])
    monkeypatch.setattr(dl, "go_registry_versions_list", lambda _ns, _n: [])
    monkeypatch.setattr(dl, "python_registry_versions_list", lambda _ns, _n: [])
    monkeypatch.setattr(lp, "_fetch_maven_versions_probe",
                        lambda ns, name, pp, timeout: ([], True))


# ── 四栈区分力主用例（supersede r56 原 :594）──

def test_four_arms_prune_phantoms_keep_workspace_members(monkeypatch):
    """每栈一条成员依赖 + 一条幻影依赖；registry 确证查无 ⇒ 成员留、幻影剪。

    突变判据：gradle/python 臂回退 member_source_names ⇒ 成员依赖也被剪 ⇒ 红；
    臂整体死掉 ⇒ 幻影存活 ⇒ 红。"""
    files = {
        "Cargo.toml": '[package]\nname = "svc"\n[dependencies]\nlib = "1.0"\nghost-crate = "9.9"\n',
        "lib/Cargo.toml": '[package]\nname = "lib"\n',
        "go.mod": ("module example.com/shop\n\nrequire example.com/lib v1.0.0\n"
                   "require example.com/phantom v9.9.9\n"),
        "lib/go.mod": "module example.com/lib\n\ngo 1.21\n",
        "settings.gradle": "include ':app'\n",
        "build.gradle": ("dependencies {\n    implementation 'com.example:app:1.0.0'\n"
                         "    implementation 'com.example:phantom:9.9.9'\n}\n"),
        "requirements.txt": "myapp==1.0.0\nphantom-pkg==99.99\n",
        "pyproject.toml": '[project]\nname = "myapp"\n',
    }
    _wire_project(monkeypatch, files)
    _registries_all_empty(monkeypatch)

    n, changed = lp._enforce_dep_legality("/tmp/x", 60)

    assert n == 4 and changed == ["Cargo.toml", "build.gradle", "go.mod", "requirements.txt"]
    # 成员依赖必须存活（members 提取真跑了才做得到——registry 对它们也是查无）
    assert 'lib = "1.0"' in files["Cargo.toml"]
    assert "example.com/lib v1.0.0" in files["go.mod"]
    assert "com.example:app:1.0.0" in files["build.gradle"]
    assert "myapp==1.0.0" in files["requirements.txt"]
    # 幻影必须被剪
    assert "ghost-crate" not in files["Cargo.toml"]
    assert "example.com/phantom" not in files["go.mod"]
    assert "com.example:phantom" not in files["build.gradle"]
    assert "phantom-pkg" not in files["requirements.txt"]


# ── 治法接线锁：成员事实源必须真进了提取器输入 ──

def test_gradle_members_extractor_receives_settings_source(monkeypatch):
    """接线锁：settings.gradle 必须在 members 提取器的输入里（治前恒不在 ⇒ 死代码）。"""
    files = {
        "settings.gradle": "include ':app'\n",
        "build.gradle": "dependencies {\n    implementation 'com.example:phantom:9.9.9'\n}\n",
    }
    spy: dict = {}
    _wire_project(monkeypatch, files, spy_extractor=spy)
    _registries_all_empty(monkeypatch)

    n, changed = lp._enforce_dep_legality("/tmp/x", 60)

    assert spy.get("inputs"), "gradle 臂的 members 提取器根本没被调用"
    assert any("settings.gradle" in rels for rels in spy["inputs"]), \
        f"settings.gradle 不在提取器输入里: {spy['inputs']}"
    assert n == 1 and "com.example:phantom" not in files["build.gradle"]


def test_python_requirements_arm_reads_pyproject_member_source(monkeypatch):
    """接线锁：requirements 模式下 pyproject.toml 必须进提取器输入（治前恒不在）。"""
    files = {
        "requirements.txt": "myapp==1.0.0\nphantom-pkg==99.99\n",
        "pyproject.toml": '[project]\nname = "myapp"\n',
    }
    spy: dict = {}
    _wire_project(monkeypatch, files, spy_extractor=spy)
    _registries_all_empty(monkeypatch)

    n, changed = lp._enforce_dep_legality("/tmp/x", 60)

    assert spy.get("inputs"), "python 臂的 members 提取器根本没被调用"
    assert any("pyproject.toml" in rels for rels in spy["inputs"]), \
        f"pyproject.toml 不在提取器输入里: {spy['inputs']}"
    assert n == 1 and changed == ["requirements.txt"]
    assert "myapp==1.0.0" in files["requirements.txt"]
    assert "phantom-pkg" not in files["requirements.txt"]


# ── ③ 纯 Kotlin DSL：别名 manifest_name 让臂真跑 ──

def test_gradle_kts_only_project_arm_runs_and_prunes(monkeypatch):
    """纯 .kts 工程：臂必须真跑（治前 root_text=None 早返，一条 find 都不发）。

    突变判据：manifest_name 回退单名 "build.gradle" ⇒ find 捞不到 .kts ⇒ 幻影存活 ⇒ 红。"""
    files = {
        "settings.gradle.kts": 'include(":app")\n',
        "build.gradle.kts": ("dependencies {\n    implementation(\"com.example:app:1.0.0\")\n"
                             "    implementation(\"com.example:phantom:9.9.9\")\n}\n"),
    }
    _wire_project(monkeypatch, files)
    _registries_all_empty(monkeypatch)

    n, changed = lp._enforce_dep_legality("/tmp/x", 60)

    assert n == 1 and changed == ["build.gradle.kts"]
    assert "com.example:app:1.0.0" in files["build.gradle.kts"]
    assert "com.example:phantom" not in files["build.gradle.kts"]


def test_gradle_mixed_groovy_and_kts_both_scanned(monkeypatch):
    """别名双臂：groovy 与 kts 清单同仓时都要进 enforce 改写集。"""
    files = {
        "settings.gradle": "include ':app'\n",
        "build.gradle": "dependencies {\n    implementation 'com.example:phantom-a:9.9.9'\n}\n",
        "app/build.gradle.kts": ("dependencies {\n"
                                 "    implementation(\"com.example:phantom-b:9.9.9\")\n}\n"),
    }
    _wire_project(monkeypatch, files)
    _registries_all_empty(monkeypatch)

    n, changed = lp._enforce_dep_legality("/tmp/x", 60)

    assert n == 2 and changed == ["app/build.gradle.kts", "build.gradle"]
    assert "phantom-a" not in files["build.gradle"]
    assert "phantom-b" not in files["app/build.gradle.kts"]


# ── 机读键 + WARNING（硬检查④：缺席必须机读可辨）──

def test_members_extraction_exception_warns_machine_key_and_fails_open(monkeypatch, caplog):
    """提取器抛异常 ⇒ [members_extraction_failed] WARNING + fail-open（幻影存活，诚实不剪）。"""
    files = {
        "settings.gradle": "include ':app'\n",
        "build.gradle": "dependencies {\n    implementation 'com.example:phantom:9.9.9'\n}\n",
    }
    _wire_project(monkeypatch, files, spy_extractor={"raise": True})
    _registries_all_empty(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="swarm.worker.l1_pipeline"):
        n, changed = lp._enforce_dep_legality("/tmp/x", 60)

    assert (n, changed) == (0, [])
    assert "com.example:phantom" in files["build.gradle"], "fail-open 被翻成误剪"
    assert any("[members_extraction_failed]" in r.message for r in caplog.records), \
        "提取异常必须落机读键，否则 fail-open 静默"


def test_gradle_settings_include_unparsed_warns_members_empty(monkeypatch, caplog):
    """settings 里有 include 语句形态却解析为零 ⇒ [members_empty] WARNING（不静默 fail-open）。"""
    files = {
        # include() 是语句形态（过语句正则）但无任何引号串 ⇒ 解析为零
        "settings.gradle": "rootProject.name = 'x'\ninclude()\n",
        "build.gradle": "dependencies {\n    implementation 'com.example:phantom:9.9.9'\n}\n",
    }
    _wire_project(monkeypatch, files)
    _registries_all_empty(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="swarm.worker.l1_pipeline"):
        n, _changed = lp._enforce_dep_legality("/tmp/x", 60)

    assert n == 0, "members 空 ⇒ fail-open ⇒ 幻影存活（诚实）"
    assert any("[members_empty]" in r.message for r in caplog.records)


def test_python_pyproject_without_name_warns_members_empty(monkeypatch, caplog):
    """pyproject 有 [project] 段却解析不出 name ⇒ [members_empty] WARNING。"""
    files = {
        "requirements.txt": "phantom-pkg==99.99\n",
        "pyproject.toml": '[project]\ndescription = "no name here"\n',
    }
    _wire_project(monkeypatch, files)
    _registries_all_empty(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="swarm.worker.l1_pipeline"):
        n, _changed = lp._enforce_dep_legality("/tmp/x", 60)

    assert n == 0
    assert any("[members_empty]" in r.message for r in caplog.records)


# ── 诚实边界：registry 不可达（None）⇒ fail-open 零动作不炸（原 r56 烟雾意图的诚实版）──

def test_registry_unreachable_fails_open_without_crash(monkeypatch):
    """registry 不可达 ⇒ 幻影也放行（证据缺失≠否定证据），臂跑完无异常。"""
    files = {
        "settings.gradle": "include ':app'\n",
        "build.gradle": "dependencies {\n    implementation 'com.example:phantom:9.9.9'\n}\n",
        "requirements.txt": "phantom-pkg==99.99\n",
        "pyproject.toml": '[project]\nname = "myapp"\n',
    }
    _wire_project(monkeypatch, files)
    monkeypatch.setattr(dl, "python_registry_versions_list", lambda _ns, _n: None)
    monkeypatch.setattr(lp, "_fetch_maven_versions_probe",
                        lambda ns, name, pp, timeout: ([], False))

    n, changed = lp._enforce_dep_legality("/tmp/x", 60)

    assert (n, changed) == (0, [])
    assert "phantom" in files["build.gradle"]
    assert "phantom-pkg" in files["requirements.txt"]


# ═══════════════════════════════════════════════════════════════════
# 双复核 R1 回归锁（reviewer + hunter 各自独立实测逮到的形态）
# ═══════════════════════════════════════════════════════════════════

def test_gradle_include_multi_arg_groovy(monkeypatch):
    """复核 F1【HIGH 实测】：`include ':app', ':lib'` 必须解析出【全部】成员——
    治前只抓第一个 ⇒ members={'app'} 非空 ⇒ 闸被武装 ⇒ 真兄弟 lib 依赖被 prune。"""
    files = {
        "settings.gradle": "include ':app', ':lib'\n",
        "build.gradle": ("dependencies {\n    implementation 'com.example:app:1.0.0'\n"
                         "    implementation 'com.example:lib:1.0.0'\n"
                         "    implementation 'com.example:phantom:9.9.9'\n}\n"),
    }
    _wire_project(monkeypatch, files)
    _registries_all_empty(monkeypatch)

    n, _changed = lp._enforce_dep_legality("/tmp/x", 60)

    assert n == 1
    assert "com.example:app:1.0.0" in files["build.gradle"], "真成员 app 被冤剪"
    assert "com.example:lib:1.0.0" in files["build.gradle"], "真成员 lib 被冤剪（F1 本体）"
    assert "com.example:phantom" not in files["build.gradle"]


def test_gradle_include_multi_arg_kts(monkeypatch):
    """复核 F2：Kotlin `include(":app", ":lib")` 同样全量解析。"""
    files = {
        "settings.gradle.kts": 'include(":app", ":lib")\n',
        "build.gradle.kts": ("dependencies {\n    implementation(\"com.example:app:1.0.0\")\n"
                             "    implementation(\"com.example:lib:1.0.0\")\n"
                             "    implementation(\"com.example:phantom:9.9.9\")\n}\n"),
    }
    _wire_project(monkeypatch, files)
    _registries_all_empty(monkeypatch)

    n, _changed = lp._enforce_dep_legality("/tmp/x", 60)

    assert n == 1
    assert "com.example:app:1.0.0" in files["build.gradle.kts"]
    assert "com.example:lib:1.0.0" in files["build.gradle.kts"]
    assert "com.example:phantom" not in files["build.gradle.kts"]


def test_gradle_include_nested_registers_last_segment(monkeypatch):
    """复核 R1：嵌套 `include ':a:b'` ⇒ 依赖坐标按末段写（gradle 默认 artifact 名），
    `com.example:b:1.0.0` 必须被认成成员。"""
    files = {
        "settings.gradle": "include ':a:b'\n",
        "build.gradle": ("dependencies {\n    implementation 'com.example:b:1.0.0'\n"
                         "    implementation 'com.example:phantom:9.9.9'\n}\n"),
    }
    _wire_project(monkeypatch, files)
    _registries_all_empty(monkeypatch)

    n, _changed = lp._enforce_dep_legality("/tmp/x", 60)

    assert n == 1
    assert "com.example:b:1.0.0" in files["build.gradle"], "嵌套成员末段名被冤剪"
    assert "com.example:phantom" not in files["build.gradle"]


def test_gradle_commented_include_not_a_member(monkeypatch):
    """复核 F6：`// include ':ghost'` 注释不得产假成员 ⇒ ghost 依赖照常被剪。"""
    files = {
        "settings.gradle": "include ':app'\n// include ':ghost'\n/* include ':phantom2' */\n",
        "build.gradle": ("dependencies {\n    implementation 'com.example:app:1.0.0'\n"
                         "    implementation 'com.example:ghost:1.0.0'\n}\n"),
    }
    _wire_project(monkeypatch, files)
    _registries_all_empty(monkeypatch)

    n, _changed = lp._enforce_dep_legality("/tmp/x", 60)

    assert n == 1
    assert "com.example:app:1.0.0" in files["build.gradle"]
    assert "com.example:ghost" not in files["build.gradle"], "注释里的假成员成了免剪金牌"


def test_gradle_includebuild_does_not_false_warn(monkeypatch, caplog):
    """复核 R4a：`includeBuild('../other')` 复合构建不是 include 语句 ⇒ 不报 [members_empty]。"""
    files = {
        "settings.gradle": "includeBuild('../other')\n",
        "build.gradle": "dependencies {\n    implementation 'com.example:phantom:9.9.9'\n}\n",
    }
    _wire_project(monkeypatch, files)
    _registries_all_empty(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="swarm.worker.l1_pipeline"):
        lp._enforce_dep_legality("/tmp/x", 60)

    assert not any("[members_empty]" in r.message for r in caplog.records), \
        "includeBuild 被误当 include ⇒ 每次 L1 误报"


def test_python_single_quoted_name_via_tomllib(monkeypatch, caplog):
    """复核 R4b：合法 TOML literal string `name = 'myapp'`（单引号）必须解析出 name——
    行正则漏 ⇒ [members_empty] 误报 + members 空 fail-open。"""
    files = {
        "requirements.txt": "myapp==1.0.0\nphantom-pkg==99.99\n",
        "pyproject.toml": "[project]\nname = 'myapp'\n",
    }
    _wire_project(monkeypatch, files)
    _registries_all_empty(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="swarm.worker.l1_pipeline"):
        n, _changed = lp._enforce_dep_legality("/tmp/x", 60)

    assert n == 1
    assert "myapp==1.0.0" in files["requirements.txt"], "单引号 name 没解析 ⇒ 自身包被冤剪"
    assert "phantom-pkg" not in files["requirements.txt"]
    assert not any("[members_empty]" in r.message for r in caplog.records)


def test_python_monorepo_all_pyproject_names_are_members(monkeypatch):
    """复核 F3/R3：pyproject 模式下【所有】pyproject 的 name 都是成员——
    apps/web 按目录序排在根前也不得让根名/子包名丢失。"""
    files = {
        # 无 requirements.txt ⇒ pyproject 模式（find 递归捞全部 pyproject.toml）
        "pyproject.toml": ('[project]\nname = "root-app"\n'
                           'dependencies = ["web", "phantom-pkg-xyz"]\n'),
        "apps/web/pyproject.toml": '[project]\nname = "web"\n',
    }
    _wire_project(monkeypatch, files)
    _registries_all_empty(monkeypatch)

    n, changed = lp._enforce_dep_legality("/tmp/x", 60)

    assert n == 1 and changed == ["pyproject.toml"]
    assert '"web"' in files["pyproject.toml"], "子包名被冤剪（目录序抽签复发）"
    assert "phantom-pkg-xyz" not in files["pyproject.toml"]


def test_multiline_declaration_prune_leaves_no_dangling_paren(monkeypatch):
    """复核 R2【实测】：多行声明 prune 后不得留悬挂 `)`（行尾收窄的能力回退——
    旧病正则恰好把 `\\n)` 吃进 block 一并删除）。"""
    files = {
        "settings.gradle": "include ':app'\n",
        "build.gradle": ("dependencies {\n    implementation 'com.example:app:1.0.0'\n"
                         "    implementation(\n        \"com.example:phantom:9.9.9\"\n    )\n}\n"),
    }
    _wire_project(monkeypatch, files)
    _registries_all_empty(monkeypatch)

    n, _changed = lp._enforce_dep_legality("/tmp/x", 60)

    assert n == 1
    out = files["build.gradle"]
    assert "phantom" not in out
    assert out.count("(") == out.count(")"), f"悬挂括号残留 ⇒ manifest 非法:\n{out}"
    assert "com.example:app:1.0.0" in out


def test_member_source_unreadable_warns_and_flags(monkeypatch, caplog):
    """复核 F5：成员事实源【存在但读失败】⇒ [member_source_unreadable] WARNING +
    details 台账记账（与文件不存在=合法单模块的静默区分）。"""
    files = {
        "settings.gradle": "include ':app'\n",   # 存在（test -f 真）
        "build.gradle": "dependencies {\n    implementation 'com.example:phantom:9.9.9'\n}\n",
    }
    _wire_project(monkeypatch, files, unreadable={"settings.gradle"})
    _registries_all_empty(monkeypatch)
    details: dict = {}

    with caplog.at_level(logging.WARNING, logger="swarm.worker.l1_pipeline"):
        n, _changed = lp._enforce_dep_legality("/tmp/x", 60, details)

    assert n == 0, "成员源读失败 ⇒ fail-open ⇒ 幻影存活（诚实）"
    assert any("[member_source_unreadable]" in r.message for r in caplog.records)
    assert details.get("dep_legality_members_unresolved") == ["gradle"]


def test_members_unresolved_lands_in_details_ledger(monkeypatch):
    """复核 F4（硬检查④）：members 取证失败必须落【有消费者的】机读账——
    L1 details 台账键 dep_legality_members_unresolved。"""
    files = {
        "settings.gradle": "include ':app'\n",
        "build.gradle": "dependencies {\n    implementation 'com.example:phantom:9.9.9'\n}\n",
    }
    _wire_project(monkeypatch, files, spy_extractor={"raise": True})
    _registries_all_empty(monkeypatch)
    details: dict = {}

    lp._enforce_dep_legality("/tmp/x", 60, details)

    assert details.get("dep_legality_members_unresolved") == ["gradle"]


# ═══════════════════════════════════════════════════════════════════
# 复核 R2 回归锁（reviewer 独立对抗复验逮到的级联逃逸族）
# ═══════════════════════════════════════════════════════════════════

def test_gradle_two_consecutive_indented_phantoms_both_pruned(monkeypatch):
    """复核 N1【HIGH 实测】：remove() 尾部 `\s*\n?` 吃掉下一条依赖的前导缩进 ⇒
    后续 block（从原文含缩进捕获）定位失败 ⇒ enforce 静默 continue ⇒ 幻影逃逸。
    两条连续缩进幻影必须【都】剪掉。"""
    files = {
        "settings.gradle": "include ':app'\n",
        "build.gradle": ("dependencies {\n"
                         "    implementation 'com.example:ghost1:9.9.9'\n"
                         "    implementation 'com.example:ghost2:9.9.9'\n"
                         "    implementation 'com.example:app:1.0.0'\n}\n"),
    }
    _wire_project(monkeypatch, files)
    _registries_all_empty(monkeypatch)

    n, _changed = lp._enforce_dep_legality("/tmp/x", 60)

    assert n == 1
    assert "ghost1" not in files["build.gradle"], "第一条幻影没剪掉"
    assert "ghost2" not in files["build.gradle"], "第二条幻影逃逸（N1 本体）"
    assert "com.example:app:1.0.0" in files["build.gradle"]


def test_go_require_block_two_consecutive_phantoms_both_pruned(monkeypatch):
    """复核 N1 的 go 常态形态（require 块内 tab 缩进，自 W-6 起在生产）。"""
    files = {
        "go.mod": ("module example.com/shop\n\ngo 1.21\n\nrequire (\n"
                   "\tgithub.com/x/ghost1 v9.9.9\n"
                   "\tgithub.com/x/ghost2 v9.9.9\n"
                   "\texample.com/lib v1.0.0\n)\n"),
        "lib/go.mod": "module example.com/lib\n\ngo 1.21\n",
    }
    _wire_project(monkeypatch, files)
    _registries_all_empty(monkeypatch)

    n, _changed = lp._enforce_dep_legality("/tmp/x", 60)

    assert n == 1
    assert "ghost1" not in files["go.mod"]
    assert "ghost2" not in files["go.mod"], "块内第二条幻影逃逸（N1 的 go 形态）"
    assert "example.com/lib v1.0.0" in files["go.mod"]


def test_cargo_two_consecutive_indented_phantoms_both_pruned(monkeypatch):
    """复核 N1 的 cargo 形态（缩进 TOML 合法但少见）。"""
    files = {
        "Cargo.toml": ('[package]\nname = "svc"\n\n[dependencies]\n'
                       '  ghost-crate-1 = "9.9"\n  ghost-crate-2 = "9.9"\n  lib = "1.0"\n'),
        "lib/Cargo.toml": '[package]\nname = "lib"\n',
    }
    _wire_project(monkeypatch, files)
    _registries_all_empty(monkeypatch)

    n, _changed = lp._enforce_dep_legality("/tmp/x", 60)

    assert n == 1
    assert "ghost-crate-1" not in files["Cargo.toml"]
    assert "ghost-crate-2" not in files["Cargo.toml"], "第二条幻影逃逸（N1 的 cargo 形态）"
    assert 'lib = "1.0"' in files["Cargo.toml"]


def test_empty_member_source_is_readable_not_unreadable(monkeypatch, caplog):
    """复核 N2【实测】：零字节 settings.gradle（合法）读回 "" 而非 None——
    不得误报 [member_source_unreadable]、不得误落台账（空文件=已取证为零，非未取证）。"""
    files = {
        "settings.gradle": "",   # 零字节，合法
        "build.gradle": "dependencies {\n    implementation 'com.example:phantom:9.9.9'\n}\n",
    }
    _wire_project(monkeypatch, files)
    _registries_all_empty(monkeypatch)
    details: dict = {}

    with caplog.at_level(logging.WARNING, logger="swarm.worker.l1_pipeline"):
        lp._enforce_dep_legality("/tmp/x", 60, details)

    assert not any("[member_source_unreadable]" in r.message for r in caplog.records), \
        "零字节成员源被误报为读取失败"
    assert "dep_legality_members_unresolved" not in details, "零字节成员源被误记账"
