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


def test_blade_is_recognized_without_a_laravel_framework_marker(tmp_path):
    """★复合后缀档的接线（不是"表里有这条"）★ 上一条直接调 `_template_engine_of`；这条走完整
    `detect()`，且刻意**拿掉** `laravel/framework` marker。

    为什么必须拿掉：#18 把 addend 接通后出现了通往同一结论的**第二条路径**——`.blade.php`
    经 splitext 退成 `.php`，而 `.php` 在 `_WEBPAGE_EXTS`、文件又在 `resources/views/` 下，
    只要 marker 在，即使复合后缀表整块死掉也照样判出 `server-template（Blade）`。实测：
    `_FIXTURES["laravel"]` 那条参数化测试因此对"复合表整块失效"归零区分力（harness 逮到）。

    夹具形状是真实的：Laravel **扩展包/模块**仓（而非应用本体）依赖 `illuminate/support`
    而不是 `laravel/framework`，却同样发 blade 视图。此时复合后缀是**唯一**证据。
    """
    prof = detect(_mk(tmp_path, {
        "composer.json": json.dumps({"name": "acme/blog",
                                     "require": {"illuminate/support": "^10.0"}}),
        "src/BlogServiceProvider.php": "<?php class BlogServiceProvider {}",
        "resources/views/index.blade.php": "@extends('layouts.app')",
    }))
    assert prof["frontend_kind"] == "server-template", (
        f"没有 laravel/framework marker 时 blade 认不出来了（{prof['frontend']}）"
        "⇒ 复合后缀档没被接上，或只在有 marker 时才生效")
    assert "Blade" in prof["frontend"]


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
    这里走完整 `detect()`：清单里只有 `iterare`（含子串 `tera`）等无关包 + 一个 `.html`，
    裸子串口径会把它判成服务端模板（Tera 引擎），词边界口径不会。

    ★`.html` 必须放在模板目录下（#18 之后）★ 首版放在 `public/report.html`。#18 把判据的
    计数范围从"全仓任意 .html"收敛到"模板目录作用域"以后，`public/` 下的文件**不再是证据**
    ⇒ marker 误不误命中都判 `none` ⇒ 这条测试对裸子串突变归零区分力（改共享代码必复跑
    sibling harness，[[swarm-rerun-sibling-harnesses]]，本次由 harness 逮到）。
    放进 `views/` 后，marker 成了裁决的**唯一**剩余合取项，接线才重新可证伪。
    """
    prof = detect(_mk(tmp_path, {
        "package.json": json.dumps({"name": "ui", "dependencies": {
            "iterare": "^1.2.1", "literal-parser": "^1.0.0", "slimmer": "^1.0.0"}}),
        "src/index.js": "console.log(1)\n",
        "views/report.html": "<html>static</html>",
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


# ── ③ #18：判据的**计数范围**（注释与实现同源）─────────────────────────────────
_REQ_DRF = "Django==5.0.6\ndjangorestframework==3.15.2\n"


@pytest.mark.parametrize("stray", [
    "docs/index.html",            # 手写文档站
    "coverage/index.html",        # 覆盖率报告
    "static/dist/report.html",    # 前端构建产物
    "htmlcov/index.html",         # pytest-cov 默认输出目录
])
def test_api_only_project_is_not_flipped_by_a_stray_html(tmp_path, stray):
    """★#18 病灶本体★ DRF 纯 API 工程只要仓里**任意**位置有一个 `.html`，判据就把
    **正确答案** `none` 翻成 `server-template` + conf=0.95 + needs_adj=False。

    根因是计数范围：`:772` 的注释写"**templates 下的** .html"，实现却用
    `ext_counts[".html"]`＝全仓任意 .html。四个落点都是真实工程里必然出现的产物。

    ★为什么这条比"词边界"更根本★ `\\bdjango\\b` 确实拒得了 `djangorestframework`
    （见 test_word_boundary_is_actually_wired_into_detection），但 DRF 工程**必然**同时
    声明真的 `Django==5.0.6` ⇒ marker 由那条**合法坐标**命中 ⇒ 收窄 marker 治不了它。
    """
    prof = detect(_mk(tmp_path, {
        "requirements.txt": _REQ_DRF,
        "api/views.py": "from rest_framework import viewsets\n",
        stray: "<html>not a template</html>",
    }))
    assert prof["frontend_kind"] == "none", (
        f"{stray} 把纯 API 工程翻成了 {prof['frontend_kind']}：{prof['frontend']}")
    assert prof["signals"]["tmpl_engine_unrecognized"] is False
    assert ".vue" not in format_stack_for_prompt(prof), \
        "纯 API 工程收到了「禁止产 .vue」的服务端模板硬约束"


def test_moving_the_html_into_a_template_dir_is_what_flips_the_verdict(tmp_path):
    """★判据唯一性★ 同一份工程、同一个文件名、**只改所在目录**：`docs/` 下不算证据，
    `templates/` 下算。

    这条把"计数范围"这一个变量单独隔出来——只断"DRF 判 none"会让把整条 addend 删掉的突变
    照样绿（删了以后 Django 模板工程也判 none，但那条命题在另一个测试里）。两个方向必须
    由**同一对夹具**的差分来锁，否则中间那档（范围改成别的东西）漏得过去。
    """
    common = {"requirements.txt": "Django==5.0.6\n", "app/views.py": "from django.db import x\n"}
    outside = detect(_mk(tmp_path / "a", {**common, "docs/page.html": "<html>x</html>"}))
    inside = detect(_mk(tmp_path / "b", {**common, "templates/page.html": "<html>x</html>"}))
    assert outside["frontend_kind"] == "none", "模板目录外的 .html 被当成了模板证据"
    assert inside["frontend_kind"] == "server-template", "模板目录内的 .html 没被当成证据"
    assert "Django" in inside["frontend"]


def test_template_files_are_not_counted_twice(tmp_path):
    """★冗余计数★ `real_server_tmpl` 已含"引擎认得的文件数"，addend 只能加**认不得的**那批。

    Laravel 的 `.blade.php` 同时属于两张表（`_TEMPLATE_COMPOUND_SUFFIX` 认得它，`.php` 又在
    `_WEBPAGE_EXTS` 里）——若 addend 用"模板目录下全部网页文件"，这 2 个文件会被数成 4。
    首版实现正是那样（我自己引入的），这条测试是它的判据。
    """
    prof = detect(_laravel(tmp_path))
    assert prof["frontend_kind"] == "server-template"
    assert prof["signals"]["server_template_files"] == 2, (
        "服务端模板文件数与真实文件数不符 ⇒ 同一批文件被两个口径各数了一遍")


# ── ④ #20：四个**已存在**主流栈的假答案（兜底缺口）──────────────────────────────
def _pom_war(dep_xml: str) -> str:
    return (f"<project><modelVersion>4.0.0</modelVersion><groupId>com.acme</groupId>"
            f"<artifactId>web</artifactId><version>1.0</version><packaging>war</packaging>"
            f"<dependencies>{dep_xml}</dependencies></project>")


def _dep(group: str, artifact: str) -> str:
    return f"<dependency><groupId>{group}</groupId><artifactId>{artifact}</artifactId></dependency>"


def _jsf(tmp_path):
    """JSF/PrimeFaces：Facelets 默认扫 **war 根**（`src/main/webapp`），页面不进任何
    `templates/` 子目录；`.xhtml` 靠后缀永远认不出（静态 XHTML 同后缀）。"""
    return _mk(tmp_path, {
        "pom.xml": _pom_war(_dep("jakarta.platform", "jakarta.jakartaee-web-api")
                            + _dep("org.primefaces", "primefaces")),
        "src/main/webapp/index.xhtml": "<ui:composition xmlns:ui='jakarta.faces.facelets'/>",
        "src/main/webapp/WEB-INF/web.xml": "<web-app/>",
        "src/main/java/com/acme/UserBean.java": "package com.acme; public class UserBean {}",
    })


def _webforms(tmp_path):
    """ASP.NET Web Forms：页面散在工程根与任意业务目录，**没有**模板目录惯例。"""
    return _mk(tmp_path, {
        "App.csproj": "<Project><PropertyGroup><TargetFramework>net48</TargetFramework>"
                      "</PropertyGroup></Project>",
        "Default.aspx": "<%@ Page Language='C#' %>",
        "Account/Login.aspx": "<%@ Page Language='C#' %>",
        "Account/Login.aspx.cs": "public partial class Login {}",
    })


def _magento(tmp_path):
    """Magento 2：`<Module>/view/frontend/templates/**/*.phtml`。"""
    return _mk(tmp_path, {
        "composer.json": json.dumps({"require": {"magento/product-community-edition": "2.4.6"}}),
        "app/code/Acme/Blog/registration.php": "<?php ?>",
        "app/code/Acme/Blog/view/frontend/templates/post/list.phtml": "<?php echo $x; ?>",
    })


def _grails(tmp_path):
    """Grails：`grails-app/views/**/*.gsp`（Grails 3+ 用 Gradle）。"""
    return _mk(tmp_path, {
        "build.gradle": "plugins { id 'org.grails.grails-web' }\n",
        "grails-app/views/book/list.gsp": "<g:each in='${books}'>${it}</g:each>",
        "grails-app/controllers/BookController.groovy": "class BookController {}",
    })


_NEW_FIXTURES = {"jsf": _jsf, "webforms": _webforms, "magento": _magento, "grails": _grails}


@pytest.mark.parametrize("stack,engine", [
    ("jsf", "JSF"), ("webforms", "ASP.NET Web Forms"),
    ("magento", "PHP template"), ("grails", "GSP"),
])
def test_four_existing_stacks_no_longer_get_a_confident_wrong_answer(tmp_path, stack, engine):
    """★兜底缺口的实证★ 这四个栈**在治法上线前就已经存在**，却全部拿 `none` + conf=0.75
    + needs_adj=False ＝ 高置信错答案（连 LLM 兜底都不触发）。

    为什么它们能一起漏掉：P-C3 当初为"未收录的下一个栈"造的兜底网，和主判据表是我**同一时刻
    凭印象列的**两张表 ⇒ 缺口重合（[[swarm-fallback-must-not-share-the-gap]]）。
    四栈的缺口各自落在**不同**的表上，所以"扩后缀 + 扩目录"这句话只对了一半：
      Grails  `grails-app/views/*.gsp`     → `views` 早在目录表里 ⇒ 只缺后缀
      Magento `view/frontend/templates/*.phtml` → `templates` 早在表里 ⇒ 只缺后缀
      JSF     `src/main/webapp/*.xhtml`    → `.xhtml` 早在后缀表里 ⇒ 只缺目录 + dep marker
      WebForms `Account/Login.aspx`        → **目录法对它结构性失效**（无模板目录惯例）
    """
    prof = detect(_NEW_FIXTURES[stack](tmp_path))
    assert prof["frontend_kind"] == "server-template", (
        f"{stack} 仍判 {prof['frontend_kind']}/conf={prof['confidence']}："
        f"{prof['frontend']}")
    assert engine.lower() in prof["frontend"].lower(), prof["frontend"]


@pytest.mark.parametrize("stack", list(_NEW_FIXTURES))
def test_four_existing_stacks_get_the_no_spa_instruction(tmp_path, stack):
    """★真后果★ 与五栈那条同源：认出形态的**目的**是发出"禁止产独立 SPA"的硬约束。
    只断 `frontend_kind` 会漏掉"分类对了但指令没发"。"""
    txt = format_stack_for_prompt(detect(_NEW_FIXTURES[stack](tmp_path)))
    assert "服务端模板" in txt
    assert ".vue" in txt and "禁止" in txt, "缺少禁止产独立 SPA 工程的硬约束"


@pytest.mark.parametrize("stack", list(_NEW_FIXTURES))
def test_four_existing_stacks_never_set_unrecognized_flag(tmp_path, stack):
    """认出引擎的工程绝不该同时带"认不得"键（两档互斥，粘滞信号无信息量）。"""
    prof = detect(_NEW_FIXTURES[stack](tmp_path))
    assert prof["signals"]["tmpl_engine_unrecognized"] is False


def test_webforms_is_recognized_with_zero_template_dir_convention(tmp_path):
    """★这是 `.aspx` 必须进"一档表"而不是 `_WEBPAGE_EXTS` 的判据★

    二档（目录名 + 网页后缀 + 依赖 marker）对 WebForms **结构性失效**：它没有模板目录惯例，
    页面就躺在工程根和任意业务目录下。这条夹具里一个模板目录名都没有——若把 `.aspx` 放进
    二档，它必然判不出来。
    """
    prof = detect(_webforms(tmp_path))
    paths = {"Default.aspx", "Account/Login.aspx"}
    assert not any(seg in sd._TEMPLATE_DIR_NAMES
                   for p in paths for seg in p.lower().split("/")[:-1]), \
        "夹具自身失去了「零模板目录」这个前提，本条测试不再测它要测的东西"
    assert prof["frontend_kind"] == "server-template"


def test_webforms_coverage_does_not_depend_on_ascx_or_master(tmp_path):
    """★刻意不登记 `.ascx`/`.master` 的判据★ 两者都**不能被单独请求**（MS：user control
    "must be placed onto an aspx or Master page"）⇒ 有它们必有 `.aspx`。

    所以带全套 `.aspx` + `.ascx` + `.master` 的工程，判定只靠 `.aspx` 就已经成立——那两个
    后缀登记进表里造不出"有它/没它结论不同"的夹具＝不可证伪条目
    （同 `_TEMPLATE_COMPOUND_SUFFIX` 不登记 `.html.erb` 的判据）。
    """
    prof = detect(_mk(tmp_path, {
        "App.csproj": "<Project><PropertyGroup/></Project>",
        "Default.aspx": "<%@ Page %>",
        "Site.master": "<%@ Master %>",
        "Controls/Nav.ascx": "<%@ Control %>",
    }))
    assert prof["frontend_kind"] == "server-template"
    assert prof["signals"]["server_template_files"] == 1, (
        "判定应只由 .aspx 贡献；若这里 >1 说明 .ascx/.master 也被登记了 ⇒ 那两条现在有了"
        "行为，需各自补一条能证伪它的测试，而不是靠本条顺带背书")


def test_jsf_xhtml_in_war_root_is_only_reachable_via_the_webapp_dir_name(tmp_path):
    """★`webapp` 这一表项的可证伪性★ 同一份 pom（primefaces 在）、同一个 `.xhtml`，
    **只改所在目录**：war 根 `src/main/webapp/` 下算证据，`src/main/wwwroot/` 下不算。

    删掉 `webapp` 表项 ⇒ 第一个断言红。这条也顺带说明为什么**不**登记 `web-inf`：源码树里
    `WEB-INF` 恒在 `src/main/webapp` 之下（Maven/Gradle war 布局皆然）＝已被 webapp 支配，
    仓根出现 WEB-INF 的只有 exploded war 而 `target/build` 已在 `_NOISE_DIRS` 里剪掉。
    """
    pom = _pom_war(_dep("org.primefaces", "primefaces"))
    in_root = detect(_mk(tmp_path / "a",
                         {"pom.xml": pom, "src/main/webapp/index.xhtml": "<ui:composition/>"}))
    elsewhere = detect(_mk(tmp_path / "b",
                           {"pom.xml": pom, "src/main/wwwroot/index.xhtml": "<ui:composition/>"}))
    assert in_root["frontend_kind"] == "server-template", "war 根下的 .xhtml 没被当成证据"
    assert elsewhere["frontend_kind"] == "none", \
        "非模板目录下的 .xhtml 也被当成证据 ⇒ 目录作用域失效"


@pytest.mark.parametrize("group,artifact", [
    # 五条都是**实测存在**的真实坐标，且互不支配——每条单独出现时都必须命中。
    ("org.apache.myfaces.core", "myfaces-api"),      # MyFaces（一条盖 api/impl）
    ("jakarta.faces", "jakarta.faces-api"),          # Jakarta EE 9+
    ("org.glassfish", "javax.faces"),                # Java EE 时代（Mojarra）
    ("com.sun.faces", "jsf-api"),                    # 更早的 Mojarra 坐标
    ("org.primefaces", "primefaces"),                # ★不可省：见下方 docstring
])
def test_each_jsf_marker_is_independently_load_bearing(tmp_path, group, artifact):
    """★"枚举穷举"必须逐条可证伪★ 声称"五条互不支配"，就得每条单独出现时都能命中——否则
    被支配的那条是不可证伪的冗余项（[[swarm-enumeration-needs-authoritative-source]]）。

    `primefaces` 是最容易被误删的一条：跑在 Jakarta EE 容器上的 PrimeFaces 工程，pom 里只有
    `jakarta.jakartaee-web-api`(provided) + `primefaces`，前者的字符串**不含** `jakarta.faces`
    ⇒ 少了 `primefaces` 这条，该工程一个 marker 都命中不了。见 `_jsf` 夹具（正是这个形状）。
    """
    prof = detect(_mk(tmp_path, {
        "pom.xml": _pom_war(_dep(group, artifact)),
        "src/main/webapp/index.xhtml": "<ui:composition/>",
    }))
    assert prof["frontend_kind"] == "server-template", \
        f"{group}:{artifact} 单独出现时未被认出 ⇒ 该 marker 缺位或被别的条目支配"
    assert "JSF" in prof["frontend"]


def test_phtml_is_a_template_but_plain_php_source_is_not(tmp_path):
    """★分档★ `.phtml` 按约定**只**用于 PHP+HTML 渲染模板（Magento/Zend），无歧义 ⇒ 一档表。
    `.php` 是源码后缀，收进一档表会把纯后端 PHP 工程判成有前端模板。"""
    assert sd._template_engine_of("list.phtml") == "PHP template"
    assert sd._template_engine_of("HomeController.php") is None
    # 纯后端 PHP（无 .phtml、无 blade）不该被判成服务端模板
    prof = detect(_mk(tmp_path, {
        "composer.json": json.dumps({"require": {"slim/slim": "^4.0"}}),
        "src/Controller/UserController.php": "<?php class UserController {}",
    }))
    assert prof["frontend_kind"] == "none"


@pytest.mark.parametrize("name,engine", [
    ("Index.vbhtml", "Razor"),        # Razor 的 VB.NET 方言，与已有 .cshtml 同族
    ("Default.aspx", "ASP.NET Web Forms"),
    ("list.gsp", "GSP"),
    ("list.phtml", "PHP template"),
])
def test_newly_registered_suffixes_are_in_the_first_tier_table(name, engine):
    """一档表答"这个后缀是哪种模板"——四个新后缀都无歧义（不可能是静态资源），故进一档。"""
    assert sd._template_engine_of(name) == engine
