"""P-C3：模板引擎表多栈化 + 「认不得」与「真没有」机读可辨。

★被锁的两组命题★
① **事实表扩栈**：Django/Flask/Express/Gin/Laravel 的服务端渲染必须被认出来（此前五栈
   全判 `frontend_kind="none"`，其中三栈 conf=0.75/needs_adj=False ＝**高置信错答案**，
   连 LLM 兜底都不触发）。后果不是"引擎名不对"，而是 `format_stack_for_prompt` 在
   `none` 档**一条前端约定都不发** ⇒ LLM 自由规划独立 SPA 工程 ⇒ task 8537fa5e 的死代码病。
② **fail-closed 方向**：`frontend_kind="none"` 原先把"真没有前端"（合法答案）与"有前端但
   我不认得引擎"（扫描失败）塌成同一个值。血规 10 第四条要求两者机读可辨 ⇒ 新增
   `signals.tmpl_engine_unrecognized` + 强制 needs_adj + WARNING + prompt 侧消费者。
   **这一档的两个方向都要锁**：未收录引擎必须触发，而真 API 工程 / 真 SPA 必须不被误伤。
"""
from __future__ import annotations

import json
import logging
import os

import pytest

from swarm.brain import stack_detect as sd
from swarm.brain.stack_detect import detect_stack_deterministic as detect
from swarm.brain.stack_detect import format_stack_for_prompt


def _mk(tmp_path, files: dict[str, str]) -> str:
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return str(tmp_path)


# ── 五栈夹具（每个都是该栈**真实**的服务端渲染布局，不是构造出的合成形状）──────────
def _django(tmp_path):
    return _mk(tmp_path, {
        "requirements.txt": "Django==5.0.1\npsycopg2-binary==2.9.9\n",
        "manage.py": "import django\n",
        "app/views.py": "from django.shortcuts import render\n",
        "app/templates/app/index.html": "{% extends 'base.html' %}{% block c %}hi{% endblock %}",
        "app/templates/base.html": "<html>{% block c %}{% endblock %}</html>",
    })


def _flask(tmp_path):
    return _mk(tmp_path, {
        "requirements.txt": "Flask==3.0.0\nJinja2==3.1.3\n",
        "app.py": "from flask import render_template\n",
        "templates/index.html": "{% extends 'base.html' %}",
        "templates/base.html": "<html></html>",
    })


def _express(tmp_path):
    return _mk(tmp_path, {
        "package.json": json.dumps({"name": "web",
                                    "dependencies": {"express": "^4.18.2", "ejs": "^3.1.9"}}),
        "server.js": "app.set('view engine','ejs')\n",
        "views/index.ejs": "<%= title %>",
        "views/layout.ejs": "<html><%- body %></html>",
    })


def _gin(tmp_path):
    return _mk(tmp_path, {
        "go.mod": "module example.com/app\n\ngo 1.21\n\nrequire github.com/gin-gonic/gin v1.9.1\n",
        "main.go": "package main\nfunc main(){}\n",
        "templates/index.tmpl": "{{ .title }}",
        "templates/layout.tmpl": '{{ template "content" . }}',
    })


def _laravel(tmp_path):
    return _mk(tmp_path, {
        "composer.json": json.dumps({"require": {"laravel/framework": "^10.0"}}),
        "artisan": "#!/usr/bin/env php\n",
        "app/Http/Controllers/HomeController.php": "<?php class HomeController {}",
        "resources/views/welcome.blade.php": "@extends('layouts.app')",
        "resources/views/layouts/app.blade.php": "@yield('content')",
    })


_FIXTURES = {"django": _django, "flask": _flask, "express": _express,
             "gin": _gin, "laravel": _laravel}


# ── ① 事实表扩栈 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("stack,engine", [
    ("django", "Django"), ("flask", "Jinja2"), ("express", "EJS"),
    ("gin", "Go template"), ("laravel", "Blade"),
])
def test_server_rendered_stacks_are_recognized(tmp_path, stack, engine):
    """五栈的服务端渲染必须判成 server-template，且引擎名出现在人读画像里。

    ★为什么同时断 kind 和引擎名★ 只断 kind 会让"认出是模板但引擎名张冠李戴"通过，而引擎名
    要进 prompt 指导 worker 沿用既有引擎（写错＝让它去用项目里没有的引擎）。
    """
    prof = detect(_FIXTURES[stack](tmp_path))
    assert prof["frontend_kind"] == "server-template", prof["frontend"]
    assert engine.lower() in prof["frontend"].lower(), prof["frontend"]


@pytest.mark.parametrize("stack", list(_FIXTURES))
def test_server_rendered_stacks_get_the_no_spa_instruction(tmp_path, stack):
    """★这才是 P-C3 的真后果★ 认出形态的**目的**是让 prompt 发出"禁止产独立 SPA"的硬约束。

    只断 `frontend_kind` 会漏掉"分类对了但指令没发"的形态——8537fa5e 的死代码病正是缺这句。
    """
    txt = format_stack_for_prompt(detect(_FIXTURES[stack](tmp_path)))
    assert "服务端模板" in txt
    assert ".vue" in txt and "禁止" in txt, "缺少禁止产独立 SPA 工程的硬约束"


def test_blade_compound_suffix_is_recognized(tmp_path):
    """`.blade.php` 必须走复合后缀档——`os.path.splitext` 只取最后一段会得到 `.php`
    （当成 PHP 源码），这是 Laravel 整栈判不出前端形态的直接原因。"""
    assert sd._template_engine_of("welcome.blade.php") == "Blade"
    assert sd._template_engine_of("app.blade.php") == "Blade"
    # 纯 .php 不是模板（不能因为 Laravel 就把所有 PHP 源码当模板）
    assert sd._template_engine_of("HomeController.php") is None


@pytest.mark.parametrize("name,engine", [
    # ★这些多段后缀**不经**复合表★ splitext 取最后一段就已经是模板后缀，主表直接认得。
    # 把它们登记进复合表是冗余条目（删掉没有任何测试会红），故刻意不登记。
    ("index.html.erb", "ERB"),          # Rails
    ("show.html.twig", "Twig"),         # Symfony
    ("home.html.heex", "HEEx"),         # Phoenix
    # 单段后缀：各栈主流引擎
    ("index.tmpl", "Go template"),
    ("page.ejs", "EJS"),
    ("layout.pug", "Pug"),
    ("mail.j2", "Jinja2"),
    ("view.cshtml", "Razor"),
])
def test_template_suffix_table_covers_common_engines(name, engine):
    assert sd._template_engine_of(name) == engine


def test_compound_table_holds_only_suffixes_splitext_would_lose():
    """★复合表只收"splitext 之后会失去模板身份"的形态★ 否则就是不可证伪的冗余条目。

    判据可机验：对表里每个键，`splitext` 的最后一段**不该**在主表里——若在，说明主表已经
    认得它，这条复合登记删掉不会改变任何行为。
    """
    assert sd._TEMPLATE_COMPOUND_SUFFIX, "表不该为空（至少 .blade.php 必须在）"
    for suf in sd._TEMPLATE_COMPOUND_SUFFIX:
        last = os.path.splitext(suf)[1]
        assert last not in sd._TEMPLATE_EXT_ENGINE, (
            f"{suf!r} 的 splitext 尾段 {last!r} 主表已认得 ⇒ 这条复合登记是冗余条目")


def test_dep_marker_matching_is_word_bounded(tmp_path):
    """★marker 命中面积★ 扩表引入了短 marker，裸子串会把无关包当模板引擎依赖。

    实测误命中：`tera`←`iterate`/`literal`、`slim`←`slimmer`、`django`←`djangorestframework`。
    误命中的后果是把静态 `.html`/前端产物判成服务端模板 ⇒ 给一个真 SPA 工程发"禁止产 SPA"。
    """
    for dep, text in [("tera", "iterate-value literal-parser"),
                      ("slim", "slimmer"),
                      ("django", "djangorestframework")]:
        assert dep in text or dep in text.replace("-", ""), "夹具本身应含该子串"
        assert not sd._SERVER_TEMPLATE_DEP_RE[dep].search(text), \
            f"{dep!r} 在 {text!r} 上误命中（裸子串口径的病）"


def test_word_boundary_is_actually_wired_into_detection(tmp_path):
    """★证"被接上了"而不是"实现正确"★ 上一条直接调正则表，**换不出**调用点仍用裸 `in` 的病。

    突变实验实证：把调用点从 `_SERVER_TEMPLATE_DEP_RE[dep].search(...)` 改回
    `dep in all_manifest_text`，上一条照样绿——它测的是表，不是接线。
    这里走完整 `detect()`：清单里只有 `iterare`（含子串 `tera`）等无关包 + 一个静态 `.html`，
    裸子串口径会把它判成服务端模板（Tera 引擎），词边界口径不会。
    """
    prof = detect(_mk(tmp_path, {
        "package.json": json.dumps({"name": "ui", "dependencies": {
            "iterare": "^1.2.1", "literal-parser": "^1.0.0", "slimmer": "^1.0.0"}}),
        "src/index.js": "console.log(1)\n",
        "public/report.html": "<html>static</html>",
    }))
    assert "服务端模板" not in prof["frontend"], \
        f"无关包被当成模板引擎依赖（裸子串口径的病）: {prof['frontend']}"
    assert prof["frontend_kind"] != "server-template"


@pytest.mark.parametrize("text,expect", [
    ("spring-boot-starter-thymeleaf", "thymeleaf"),
    ("spring-boot-starter-freemarker", "freemarker"),
    ("velocity-engine-core", "velocity"),
    ("jakarta.servlet.jsp.jstl", "jstl"),
    # 真实 Maven 坐标：旧的裸子串靠 `jstl` 顺带命中它，改词边界后必须**单独登记**，
    # 否则这类工程从"认得"退成"认不得"＝误杀方向的静默回归。
    ("org.apache.taglibs:taglibs-standard-jstlel", "jstlel"),
])
def test_word_boundary_is_zero_regression_for_jvm_markers(text, expect):
    """词边界改造对既有 JVM marker 必须零回归（收窄很容易顺手把老栈弄丢）。"""
    assert sd._SERVER_TEMPLATE_DEP_RE[expect].search(text.lower())


def test_thymeleaf_baseline_profile_unchanged(tmp_path):
    """唯一跑过 E2E 的栈（Spring Boot + Thymeleaf）必须逐字不变。"""
    prof = detect(_mk(tmp_path, {
        "pom.xml": "<project><dependencies><dependency><artifactId>"
                   "spring-boot-starter-thymeleaf</artifactId></dependency></dependencies></project>",
        "src/main/java/com/x/App.java": "package com.x; public class App {}",
        "src/main/resources/templates/index.html": "<html xmlns:th></html>",
    }))
    assert prof["frontend_kind"] == "server-template"
    assert "Thymeleaf" in prof["frontend"]
    assert prof["signals"]["tmpl_engine_unrecognized"] is False


# ── ② fail-closed 方向：「认不得」与「真没有」 ────────────────────────────────
def test_unrecognized_engine_is_distinguishable_from_no_frontend(tmp_path, caplog):
    """★病灶本体（第二层）★ 模板目录有网页文件却认不出引擎 ⇒ 必须与"真没有前端"可辨。

    锁四件：机读键 True · 强制 needs_adj · 一条 WARNING · 人读画像不谎称"无独立前端"。
    夹具用 Haskell/Yesod（Hamlet 引擎**刻意不收录**）——用真未收录的栈，而不是把已收录的
    引擎从表里抠掉，才能测到"下一个栈撞上来"的真实形态。
    """
    path = _mk(tmp_path, {
        "app.cabal": "build-depends: base, yesod\n",
        "src/Main.hs": "main = return ()\n",
        "templates/home.html": "<html>#{title}</html>",
        "templates/layout.html": "<html>^{widget}</html>",
    })
    with caplog.at_level(logging.WARNING, logger="swarm.brain.stack_detect"):
        prof = detect(path)
    assert prof["signals"]["tmpl_engine_unrecognized"] is True
    assert prof["signals"]["unengined_template_dir_files"] == 2
    assert prof["needs_model_adjudication"] is True, "认不得必须交模型裁决，绝不高置信定案"
    assert "无独立前端" not in prof["frontend"], "认不得 ≠ 没有，人读面也不许谎称"
    assert [r for r in caplog.records if "P-C3" in r.getMessage()], "降级路径必须留 WARNING"


@pytest.mark.parametrize("files", [
    # 纯 go API：没有任何模板目录
    {"go.mod": "module example.com/api\n\ngo 1.21\n\nrequire github.com/gin-gonic/gin v1.9.1\n",
     "main.go": "package main\nfunc main(){}\n",
     "internal/handler/user.go": "package handler\n"},
    # 纯 Maven API
    {"pom.xml": "<project><dependencies><dependency><artifactId>spring-boot-starter-web"
                "</artifactId></dependency></dependencies></project>",
     "src/main/java/com/x/App.java": "package com.x; public class App {}",
     "src/main/java/com/x/UserController.java": "package com.x; public class UserController {}"},
])
def test_real_api_only_project_is_not_flagged_unrecognized(tmp_path, files):
    """★误伤方向★ 真的没有前端时，`none` 是**正确答案**，不许被新闸拉进"认不得"。

    这条与上一条是同一档闸的两个方向：闸过宽 ⇒ 每个纯 API 工程都被判"没看清"、都去
    烧一次模型裁决，且 prompt 会发一段"别当没前端"的误导指令。
    """
    prof = detect(_mk(tmp_path, files))
    assert prof["frontend_kind"] == "none"
    assert prof["signals"]["tmpl_engine_unrecognized"] is False
    assert prof["needs_model_adjudication"] is False
    assert "无独立前端" in prof["frontend"]
    assert "前端形态未判明" not in format_stack_for_prompt(prof)


def test_vue_spa_with_views_dir_is_not_flagged_unrecognized(tmp_path):
    """`src/views/*.vue` 是 SPA 的常见布局，目录名恰好撞上模板目录惯例名。

    ★这是"目录名是弱证据"的实证★ 若拿目录名单独判服务端模板，这个工程会被判成
    server-template 并收到"禁止产 .vue"的硬约束——正好禁掉它本来的形态。

    ★夹具必须编码风险，否则突变改不动它★ 首版夹具只有 `.vue` + 根 `index.html`：
    `_WEBPAGE_EXTS` 放宽到收 `.css/.js` 的突变**照样绿**（夹具里没有这两类文件）。
    现在 `src/views/` 下刻意放了 `.css`/`.js`（SPA 工程的真实形态）——它们绝不是"网页文件
    证据"，收进来就是把噪声当证据。
    """
    prof = detect(_mk(tmp_path, {
        "package.json": json.dumps({"name": "ui", "dependencies": {"vue": "^3.4.0"}}),
        "src/views/Home.vue": "<template><div/></template>",
        "src/views/About.vue": "<template><div/></template>",
        "src/views/home.css": ".x{}",
        "src/views/helper.js": "export const x=1",
        "index.html": "<html><div id=app></div></html>",
    }))
    assert prof["frontend_kind"] == "spa"
    assert prof["signals"]["tmpl_engine_unrecognized"] is False


def test_webpage_ext_table_excludes_asset_extensions(tmp_path):
    """★误伤方向·独立入口★ 纯 API 工程在 `views/` 下放静态资源（.js/.css）是真实形态。

    上一条测的是 SPA（`has_spa` 先命中 ⇒ 走不到"认不得"分支，锁不住后缀表放宽）。这一条
    是**非 spa 非 server-template** 的工程，"认不得"分支真的可达，才锁得住后缀表的宽度。
    """
    prof = detect(_mk(tmp_path, {
        "go.mod": "module example.com/api\n\ngo 1.21\n",
        "main.go": "package main\nfunc main(){}\n",
        "views/js/app.js": "console.log(1)",
        "views/css/main.css": "body{}",
    }))
    assert prof["signals"]["tmpl_engine_unrecognized"] is False, \
        "静态资源被当成网页文件证据 ⇒ 纯 API 工程被判「没看清」"
    assert prof["needs_model_adjudication"] is False


def test_spa_wins_over_unrecognized_when_both_present(tmp_path):
    """★判定序★ SPA 证据（.vue）与"模板目录下认不出的网页文件"并存时，**SPA 先判**。

    真实形态：全栈仓的前端是 Vue，同时后端有 `templates/email.html`（邮件模板，引擎未收录）。
    若把"认不得"提到 SPA 之前，这个工程会收到"别当没前端、先取证"的误导指令，而它的前端
    形态其实是明确的。
    """
    prof = detect(_mk(tmp_path, {
        "package.json": json.dumps({"name": "ui", "dependencies": {"vue": "^3.4.0"}}),
        "src/views/Home.vue": "<template><div/></template>",
        "templates/email.html": "<html>#{unknown}</html>",
    }))
    assert prof["frontend_kind"] in ("spa", "separated"), prof["frontend"]
    assert prof["signals"]["tmpl_engine_unrecognized"] is False
    assert "前端形态未判明" not in format_stack_for_prompt(prof)


def test_unrecognized_signal_has_a_prompt_consumer(tmp_path):
    """★新账必须有消费者（血规 10 第四条）★ 机读键的消费者＝prompt 里那段"别当没前端"。

    `kind == "none"` 此前在 prompt 侧**什么都不发**：真没前端时不发是对的，认不出引擎时
    不发＝默许 LLM 规划独立 SPA 工程。把机读键接上 prompt，不确定性才真的传下去了。
    """
    prof = detect(_mk(tmp_path, {
        "app.cabal": "build-depends: base, yesod\n",
        "templates/home.html": "<html>#{title}</html>",
    }))
    txt = format_stack_for_prompt(prof)
    assert "前端形态未判明" in txt
    assert "禁止" in txt and "SPA" in txt, "必须明确禁止「据此认定无前端并新建 SPA 工程」"


def test_unrecognized_lowers_confidence_below_the_adjudication_threshold(tmp_path):
    """"认不得"必须把置信度压到兜底线（0.65）以下——即便后端证据齐全。

    ★关于冗余★ 生产代码里 needs_adj 还**显式**读了机读键（不只靠这笔算术）。那条 `or`
    今天无法被突变隔离：`unrecognized` 蕴含 `frontend_kind=="none"`，拿不到前端加分，上界
    0.5+0.25-0.2=0.55 恒 < 0.65 ⇒ 前一个条件恒真。故此处锁**可观测的那一半**（置信度确实
    降了），并在生产代码注释里记下推导，而不是编一条"能红"的测试给那条 `or` 造假锁
    （[[swarm-redundant-defense-unfalsifiable]]）。
    """
    prof = detect(_mk(tmp_path, {
        # 后端证据齐全 → 基线 0.75（go.mod 在 _MANIFEST_BACKEND 里，不吃 -0.3）
        "go.mod": "module example.com/app\n\ngo 1.21\n\nrequire github.com/gin-gonic/gin v1.9.1\n",
        "main.go": "package main\nfunc main(){}\n",
        "templates/home.html": "<html>{{unknown_engine}}</html>",
    }))
    assert prof["signals"]["tmpl_engine_unrecognized"] is True
    assert prof["confidence"] < 0.65, (
        f"置信度 {prof['confidence']} 未跌破兜底线 ⇒ 高置信错答案原样复发")
    assert prof["needs_model_adjudication"] is True


def test_no_template_dir_means_no_flag_even_with_stray_html(tmp_path):
    """散落的 `.html`（如根目录 README 附带的静态页）不在模板目录 ⇒ 不触发。

    闸的证据是【模板目录 + 网页文件】的合取；只有后者就报"认不得"会把大量普通仓拉进兜底。
    """
    prof = detect(_mk(tmp_path, {
        "go.mod": "module example.com/api\n\ngo 1.21\n",
        "main.go": "package main\nfunc main(){}\n",
        "docs/coverage.html": "<html>report</html>",
    }))
    assert prof["signals"]["tmpl_engine_unrecognized"] is False
    assert prof["needs_model_adjudication"] is False


def test_recognized_engine_never_sets_unrecognized_flag(tmp_path):
    """认出引擎的工程绝不该同时带"认不得"键（两档互斥，粘滞信号无信息量）。"""
    for stack in _FIXTURES:
        d = tmp_path / stack
        d.mkdir()
        prof = detect(_FIXTURES[stack](d))
        assert prof["frontend_kind"] == "server-template"
        assert prof["signals"]["tmpl_engine_unrecognized"] is False, stack


def test_scan_failed_profile_has_the_same_signals_shape(tmp_path):
    """扫描失败画像的 signals 形状必须与正常画像**一致**（键在、且为 False）。

    两个理由，都不是洁癖：① 下游直读 `signals["tmpl_engine_unrecognized"]` 不该 KeyError；
    ② 语义上扫描失败是"什么都没看到"，不是"看到模板但认不出引擎"——若给 True，prompt 会
    发出"别当没前端"的提示，而此时连目录都没读到，那提示是凭空的。
    并且**绝不能碰** `_scan_failed_profile` 那条"needs_model_adjudication=False"（空证据
    交给 LLM 裁决＝幻觉画像的产地，正是 task 8537fa5e 的死因）。
    """
    prof = detect(str(tmp_path / "does-not-exist"))
    assert prof["scan_failed"] is True
    assert prof["frontend_kind"] == "none"
    assert prof["signals"]["tmpl_engine_unrecognized"] is False
    assert prof["signals"]["unengined_template_dir_files"] == 0
    assert prof["needs_model_adjudication"] is False, "空证据绝不交 LLM 裁决"
    assert "前端形态未判明" not in format_stack_for_prompt(prof)


@pytest.mark.asyncio
async def test_adjudication_clears_the_flag_so_it_is_not_sticky(tmp_path, monkeypatch):
    """★粘滞信号★ 机读键的唯一用途是把不确定性抬到模型裁决前；裁决落定后必须清掉。

    不清的后果（自己造的病，读裁决路径时逮到）：`profile.update(...)` 不碰 `signals` ⇒ 模型
    已裁决 `frontend_kind="none"`（真没前端，裁决正确），键仍 True ⇒ prompt 继续发"我看不清、
    别当没前端"。触发它的机制已经把它答完了，它还在响。
    """
    from swarm.brain import planning_nodes as pn

    class _Resp:
        content = json.dumps({"frontend": "无独立前端（纯 API 服务）", "frontend_kind": "none",
                              "backend": "Yesod (haskell)", "build": "cabal", "confidence": 0.8})

    class _LLM:
        async def ainvoke(self, msgs):
            return _Resp()

    proj = _mk(tmp_path, {
        "app.cabal": "build-depends: base, yesod\n",
        "templates/home.html": "<html>#{title}</html>",
    })
    monkeypatch.setattr(pn, "_get_brain_llm", lambda *a, **kw: _LLM())
    # 路径解析走 project store（DB），这里打桩——本条命题是"裁决落定后清标"，不是路径解析。
    monkeypatch.setattr(pn, "_resolve_project_path", lambda state: proj)
    # 确定性初判必须先带上标（否则测的不是清标而是"本来就没标"）
    assert detect(proj)["signals"]["tmpl_engine_unrecognized"] is True

    state = {"project_id": "p-pc3", "task_id": "t-pc3", "knowledge_context": None}
    out = await pn.detect_stack(state)
    prof = out["project_stack"]
    assert prof["signals"]["tmpl_engine_unrecognized"] is False, "裁决后仍粘滞"
    assert prof["signals"]["tmpl_engine_unrecognized_adjudicated"] is True, "清标要留痕"
    assert "前端形态未判明" not in format_stack_for_prompt(prof)


def test_template_dir_names_are_stack_neutral():
    """★纪律 1★ 目录名表必须是跨栈惯例，不许出现某栈/某项目专属路径。"""
    assert "templates" in sd._TEMPLATE_DIR_NAMES and "views" in sd._TEMPLATE_DIR_NAMES
    for name in sd._TEMPLATE_DIR_NAMES:
        assert "/" not in name and os.sep not in name, f"{name!r} 是路径片段而非目录名"
        assert name == name.lower(), f"{name!r} 必须小写（比较前已 lower）"
