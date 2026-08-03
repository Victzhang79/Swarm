#!/usr/bin/env python3
"""P-C3 突变 harness（判据与前八批同源，那些自伤一开始就带上）：

  · **先验基线全绿**（只验"突变→红"会让**修得不全**的整改全绿通过：B-4b I-1 实证）；
  · **逐条**跑 should_red，每条都必须红；· `rc=5`（`-k` 选不到，如测试被重命名）**判失败**；
  · 落点唯一性检查；· 突变后源码必须仍能 `ast.parse`（否则 rc≠0 只是 collection error）。

★锁的两组命题★
① **事实表扩栈**：五栈的服务端渲染必须认得出来，且认出来的**目的**（prompt 发"禁止产独立
   SPA"硬约束）必须真的达成——只锁 `frontend_kind` 会漏"分类对了但指令没发"。
② **fail-closed 方向双向**：未收录引擎必须触发"认不得"（机读键+needs_adj+WARNING），而真
   API 工程 / 真 SPA **必须不被误伤**。闸过宽的代价是每个纯 API 工程都去烧一次模型裁决、
   且收到一段误导指令——故误伤方向的突变与漏判方向一样多。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_pc3_template_engine_multistack.py",
         "test/test_stack_detect.py",
         # #19 的 (版本, 摘要) 配对守卫住在这里——不纳入则末两条突变的 `-k` 选不到，
         # 会以 rc=5「测试名选不到」判失败（而不是静默当成绿）。
         "test/test_r65tr_t5_baseline_convention.py"]

SD = ROOT / "brain" / "stack_detect.py"
PN = ROOT / "brain" / "planning_nodes.py"
LS = ROOT / "brain" / "llm_schemas.py"
EA = ROOT / "worker" / "executor_agent.py"

MUTATIONS = [
    # ── ① 事实表扩栈 ──
    (
        'P-C3：复合后缀档整块失效（`.blade.php` 退回 splitext ⇒ 判成 .php 源码，Laravel 整栈'
        '判不出前端形态）',
        SD,
        '    for suf in sorted(_TEMPLATE_COMPOUND_SUFFIX, key=len, reverse=True):\n'
        '        if low.endswith(suf):\n'
        '            return _TEMPLATE_COMPOUND_SUFFIX[suf]',
        '    for suf in ():\n'
        '        if low.endswith(suf):\n'
        '            return _TEMPLATE_COMPOUND_SUFFIX[suf]',
        # 首版还指名了 `test_template_suffix_table_covers_common_engines` → 突变后仍绿：
        # 它那些多段用例（`.html.erb`/`.html.heex`）splitext 后就是模板后缀，主表直接认得，
        # **不经复合表**。这个"仍绿"暴露的是我自己的冗余表项（已删两条），不是测试的错。
        # `test_server_rendered_stacks_are_recognized` 对 Laravel 不敏感：该夹具有
        # `laravel/framework` marker，即使没有复合后缀，`.php`+`resources/views/` 仍让
        # detect 路径判 server-template。这不是测试缺陷，是 Laravel 真实有多条路径到达结论。
        # 真正锁复合后缀接线的两条：纯函数 `_template_engine_of` + 无 marker 夹具的引擎名。
        ['test_blade_compound_suffix_is_recognized',
         'test_blade_is_recognized_without_a_laravel_framework_marker'],
    ),
    (
        'P-C3：复合表塞回冗余条目（`.html.heex` 主表已认得 ⇒ 该条目删掉没人会红＝不可证伪）',
        SD,
        '_TEMPLATE_COMPOUND_SUFFIX = {\n    ".blade.php": "Blade",\n}',
        '_TEMPLATE_COMPOUND_SUFFIX = {\n    ".blade.php": "Blade",\n    ".html.heex": "HEEx",\n}',
        ['test_compound_table_holds_only_suffixes_splitext_would_lose'],
    ),
    # ★复合表的"按长度降序"今天不可证伪★ 表里只剩一条（`.blade.php`），单元素表的排序对任何
    # 输入都无区别 ⇒ 把 `key=len, reverse=True` 删掉没有任何测试会红。不给它造假锁；等表里
    # 真出现前缀相含的两条（如假想的 `.php` 与 `.blade.php` 同时登记）时再补。这条突变改为
    # 打**返回值**（那是真有区分力的一半）。
    (
        'P-C3：复合表命中却返回错的引擎名（引擎名要进 prompt 指导 worker 沿用既有引擎，'
        '写错＝让它去用项目里没有的引擎）',
        SD,
        '        if low.endswith(suf):\n            return _TEMPLATE_COMPOUND_SUFFIX[suf]',
        '        if low.endswith(suf):\n            return "HTML"',
        ['test_blade_compound_suffix_is_recognized'],
    ),
    (
        'P-C3：go 模板后缀 `.tmpl` 从表里消失（Gin 工程回到判 none）',
        SD,
        '    ".tmpl": "Go template",\n',
        '',
        ['test_server_rendered_stacks_are_recognized',
         'test_template_suffix_table_covers_common_engines'],
    ),
    (
        'P-C3：node 模板后缀 `.ejs` 从表里消失（Express 工程回到判 none）',
        SD,
        '    ".ejs": "EJS",\n',
        '',
        ['test_server_rendered_stacks_are_recognized',
         'test_template_suffix_table_covers_common_engines'],
    ),
    (
        'P-C3：python 模板依赖 marker 消失（Django/Flask 的 templates/*.html 无从判定——'
        '这类栈模板后缀就是 .html，只能靠依赖坐标佐证）',
        SD,
        '    "django": "Django Template",\n    "jinja2": "Jinja2",\n',
        '',
        ['test_server_rendered_stacks_are_recognized'],
    ),
    (
        'P-C3：认出形态却不发"禁止产独立 SPA"硬约束（分类对了、8537fa5e 死代码病照旧）',
        SD,
        '    if kind == "server-template":\n        lines.append(',
        '    if False:\n        lines.append(',
        ['test_server_rendered_stacks_get_the_no_spa_instruction'],
    ),
    # ── marker 命中面积（误伤方向）──
    (
        'P-C3：★误伤★ marker 匹配退回裸子串（`tera`←iterate/literal、`slim`←slimmer、'
        '`django`←djangorestframework ⇒ 把静态 .html / 前端产物判成服务端模板）',
        SD,
        '        if _SERVER_TEMPLATE_DEP_RE[dep].search(all_manifest_text):   # P-C3：词边界，非裸子串',
        '        if dep in all_manifest_text:',
        # 首版只指名 `..._is_word_bounded` → 突变后仍绿：那条**直接调正则表**，测的是实现
        # 正确、不是被接上了。补了走完整 detect() 的接线测试才锁得住调用点。
        ['test_word_boundary_is_actually_wired_into_detection'],
    ),
    (
        'P-C3：★误伤★ 词边界只加左侧（`django` 仍命中 `djangorestframework`）',
        SD,
        '    dep: re.compile(r"\\b" + re.escape(dep) + r"\\b") for dep in _SERVER_TEMPLATE_DEP',
        '    dep: re.compile(r"\\b" + re.escape(dep)) for dep in _SERVER_TEMPLATE_DEP',
        ['test_dep_marker_matching_is_word_bounded'],
    ),
    (
        'P-C3：收窄 marker 时把既有 JVM 坐标弄丢（`taglibs-standard-jstlel` 从"认得"退成'
        '"认不得"＝误杀方向的静默回归）',
        SD,
        '    "jstlel": "JSP/JSTL",\n',
        '',
        ['test_word_boundary_is_zero_regression_for_jvm_markers'],
    ),
    # ── ② fail-closed 方向：漏判 ──
    (
        'P-C3：「认不得」档整块消失（未收录栈回到"真没有前端"的高置信错答案）',
        SD,
        '    elif unengined_tmpl_dir_files > 0:',
        '    elif False:',
        ['test_unrecognized_engine_is_distinguishable_from_no_frontend',
         'test_unrecognized_signal_has_a_prompt_consumer',
         'test_unrecognized_lowers_confidence_below_the_adjudication_threshold'],
    ),
    # ★刻意不锁 `or tmpl_engine_unrecognized` 那条子句★ 突变实验证明它今天不可隔离：
    # `unrecognized` 蕴含 `frontend_kind=="none"`（拿不到 +0.2/+0.1 前端加分），置信度上界
    # 0.5+0.25-0.2=0.55 恒 < 0.65 ⇒ 前一个条件恒真，删掉它没有任何测试会红。给它编一条
    # "能红"的测试＝把"哪道闸在生效"这一维从命题里抹掉（[[swarm-redundant-defense-
    # unfalsifiable]]）。改为锁**可观测的那一半**：置信度确实被压到兜底线以下。
    (
        'P-C3：认不得却不降置信（画像以 0.75 高置信定案「无前端」＝病灶本体的另一半）',
        SD,
        '    if tmpl_engine_unrecognized:\n        # P-C3：不是答案而是"没看清"，必须降到兜底线以下（needs_adj 阈值 0.65）。\n        confidence -= 0.2',
        '    if tmpl_engine_unrecognized:\n        # P-C3：不是答案而是"没看清"，必须降到兜底线以下（needs_adj 阈值 0.65）。\n        confidence -= 0.0',
        ['test_unrecognized_lowers_confidence_below_the_adjudication_threshold'],
    ),
    (
        'P-C3：机读键恒 False（下游永远收不到"认不得"，层内自吞＝外层永远不知道）',
        SD,
        '        tmpl_engine_unrecognized = True',
        '        tmpl_engine_unrecognized = False',
        ['test_unrecognized_engine_is_distinguishable_from_no_frontend',
         'test_unrecognized_signal_has_a_prompt_consumer'],
    ),
    (
        'P-C3：降级路径不留 WARNING（血规 10 第四条：机读键 + 一次 WARNING 缺一不可）',
        SD,
        '        logger.warning(\n            "[STACK-DETECT] P-C3 模板目录下有 %d 个网页文件',
        '        logger.debug(\n            "[STACK-DETECT] P-C3 模板目录下有 %d 个网页文件',
        ['test_unrecognized_engine_is_distinguishable_from_no_frontend'],
    ),
    (
        'P-C3：机读键无 prompt 消费者（新账没人消费＝没造；`none` 档仍什么都不发 ⇒ 默许'
        'LLM 规划独立 SPA 工程）',
        SD,
        '    elif (profile.get("signals") or {}).get("tmpl_engine_unrecognized"):',
        '    elif False:',
        ['test_unrecognized_signal_has_a_prompt_consumer'],
    ),
    (
        'P-C3：人读画像仍谎称"无独立前端"（机读键对了但人读面骗人——两个面都进 prompt）',
        SD,
        '        frontend = "无法判明前端形态（模板目录下有网页文件，但未识别出模板引擎）"',
        '        frontend = "无独立前端（API/后端为主，或前端未在本仓）"',
        ['test_unrecognized_engine_is_distinguishable_from_no_frontend'],
    ),
    # ── ② fail-closed 方向：误伤 ──
    (
        'P-C3：★误伤★ 闸只要"有网页文件"就报认不得（不再要求在模板目录下）⇒ 散落的 '
        'docs/coverage.html 就让纯 API 工程被判"没看清"、烧一次模型裁决 + 收到误导指令',
        SD,
        '                _segs = {s.lower() for s in rel.replace(os.sep, "/").split("/") if s}\n'
        '                if _segs & _TEMPLATE_DIR_NAMES:\n'
        '                    if len(unengined_candidates) < 200:',
        '                if True:\n'
        '                    if len(unengined_candidates) < 200:',
        # 首版还指名了 `..._real_api_only_project_...` → 突变后仍绿：那两个夹具**一个网页
        # 文件都没有**（纯 go / 纯 Maven API），去掉目录条件也改不动它们。真正锁住这条的是
        # 带散落 `docs/coverage.html` 的那个夹具。
        ['test_no_template_dir_means_no_flag_even_with_stray_html'],
    ),
    (
        'P-C3：★误伤★ 网页后缀表放宽到收 .css/.js（静态资源目录叫 views 的项目把噪声当证据）',
        SD,
        '_WEBPAGE_EXTS = frozenset({".html", ".htm", ".xhtml", ".php", ".tpl", ".shtml"})',
        '_WEBPAGE_EXTS = frozenset({".html", ".htm", ".xhtml", ".php", ".tpl", ".shtml",\n'
        '                           ".css", ".js"})',
        # 首版指名 SPA 那条 → 突变后仍绿，两重原因：① 夹具里没有 .css/.js 文件；② 即便有，
        # `has_spa` 先命中就走不到"认不得"分支。故另立一条**非 spa 非 server-template** 的
        # 纯 API 夹具（views/ 下放静态资源），那里分支真的可达。
        ['test_webpage_ext_table_excludes_asset_extensions'],
    ),
    (
        'P-C3：★误伤★ 判定序把"认不得"提到 SPA 之前（前端是 Vue、后端另有未收录引擎的邮件'
        '模板 ⇒ 形态明确的工程收到"别当没前端、先取证"的误导指令）',
        SD,
        '    elif has_spa:\n        frontend_kind = "spa"',
        '    elif has_spa and not unengined_tmpl_dir_files:\n        frontend_kind = "spa"',
        # 落点改了两次都因**不等价**恒绿：v1 选 `elif has_server_tmpl` 而夹具 unengined==0；
        # v2 把 `elif has_spa` 换成 `elif unengined>0` —— 夹具里 unengined==1，仍进同一分支
        # 设同一个值 = 等于没突变。现在这条让 unengined 抢在 SPA 前面（真实危害形状）。
        ['test_spa_wins_over_unrecognized_when_both_present'],
    ),
    (
        'P-C3：★误伤★ 扫描失败画像也带上"认不得"（连目录都没读到却发"别当没前端"，'
        '且可能把空证据推给 LLM 裁决＝幻觉画像产地）',
        SD,
        '                    "tmpl_engine_unrecognized": False},',
        '                    "tmpl_engine_unrecognized": True},',
        ['test_scan_failed_profile_has_the_same_signals_shape'],
    ),
    # ── 粘滞信号（读裁决路径时逮到的自伤）──
    (
        'P-C3：★粘滞★ 模型裁决落定后不清"认不得"标（触发它的机制已把问题答完，它还在响 ⇒ '
        'prompt 对已裁决的画像继续发"我看不清、别当没前端"）',
        PN,
        '                if (profile.get("signals") or {}).get("tmpl_engine_unrecognized"):\n'
        '                    if _kind_ok:\n'
        '                        profile["signals"]["tmpl_engine_unrecognized"] = False',
        '                if False:\n'
        '                    if _kind_ok:\n'
        '                        profile["signals"]["tmpl_engine_unrecognized"] = False',
        ['test_adjudication_clears_the_flag_so_it_is_not_sticky'],
    ),
    # ── ④ #21：MEDIUM-4/5（P-C3 复核）──────────────────────────────
    (
        'MEDIUM-4：schema validator 删除（`server_template` 下划线/中文形态穿过类型边界 ⇒ '
        'kind 不可消费 ⇒ 一条约定都不发 + 整裁决被采纳）',
        LS,
        '        if v and v not in FRONTEND_KINDS:',
        '        if False:',
        ['test_adjudication_with_non_enum_kind_is_rejected_at_the_schema'],
    ),
    (
        'MEDIUM-4：清标合取删除（漏字段 kind="" 也清标 ⇒ 「认不得」兜底提示消失，'
        '「什么都不发」钉成永久状态——缓存命中不重裁决）',
        PN,
        '                    if _kind_ok:\n'
        '                        profile["signals"]["tmpl_engine_unrecognized"] = False',
        '                    if True:\n'
        '                        profile["signals"]["tmpl_engine_unrecognized"] = False',
        ['test_adjudication_with_missing_kind_does_not_clear_the_flag'],
    ),
    (
        'MEDIUM-5：Helm chart 排除删除（`templates/_helpers.tpl` 重新误触发「认不得」⇒ '
        '带 Helm chart 的纯 API 仓白烧一轮 LLM 裁决 + prompt 发事实错误描述）',
        SD,
        '        if any(_h == "" or _cdir == _h or _cdir.startswith(_h + "/")\n'
        '               for _h in helm_chart_dirs):\n'
        '            continue',
        '        if False:\n'
        '            continue',
        ['test_helm_chart_templates_dir_is_not_flagged_unrecognized'],
    ),
    (
        'MEDIUM-5：Chart.yaml 收集删除（排除判据的证据源断了 ⇒ 同上后果）',
        SD,
        '            if f in ("Chart.yaml", "values.yaml"):',
        '            if False:',
        ['test_helm_chart_templates_dir_is_not_flagged_unrecognized'],
    ),
    # ── ③ #18：判据的**计数范围**（P-C3 复核 CRITICAL-1）──────────────────────────
    (
        '#18：addend 退回"全仓任意 .html"（DRF 纯 API 工程被一个 docs/index.html 翻成 '
        'server-template/0.95/needs_adj=False ＝ 高置信错答案，连 LLM 兜底都不触发）',
        SD,
        '    if server_tmpl_dep and unengined_tmpl_dir_files > 0:\n'
        '        real_server_tmpl += unengined_tmpl_dir_files',
        '    if server_tmpl_dep and ext_counts.get(".html", 0) > 0:\n'
        '        real_server_tmpl += ext_counts.get(".html", 0)',
        ['test_api_only_project_is_not_flipped_by_a_stray_html',
         'test_moving_the_html_into_a_template_dir_is_what_flips_the_verdict'],
    ),
    (
        '#18：★误杀方向★ addend 整块消失（真 Django/Flask 模板工程——模板后缀就是 .html、'
        '靠后缀永远认不出——回到判 none，"禁止产 .vue" 的约定发不出去）',
        SD,
        '    if server_tmpl_dep and unengined_tmpl_dir_files > 0:\n'
        '        real_server_tmpl += unengined_tmpl_dir_files',
        '    if False:\n'
        '        real_server_tmpl += unengined_tmpl_dir_files',
        ['test_moving_the_html_into_a_template_dir_is_what_flips_the_verdict',
         'test_server_rendered_stacks_are_recognized'],
    ),
    (
        '#18：重复计数复发（addend 改成"模板目录下全部网页文件、不论引擎认不认得"——`sum(hits)` '
        '已含引擎认得的那批 ⇒ Laravel 的 2 个 .blade.php 被数成 4。这是首版实现的形状）',
        SD,
        '            if _eng is None and ext in _WEBPAGE_EXTS:',
        '            if ext in _WEBPAGE_EXTS:',
        ['test_template_files_are_not_counted_twice'],
    ),
    (
        '#18：模板目录作用域整块失效（网页文件不论在哪都算证据 ⇒ 覆盖率报告/构建产物成了'
        '"服务端模板"，且纯 API 工程被拉进"认不得"去烧模型裁决）',
        SD,
        '                if _segs & _TEMPLATE_DIR_NAMES:',
        '                if True:',
        ['test_api_only_project_is_not_flipped_by_a_stray_html',
         'test_no_template_dir_means_no_flag_even_with_stray_html'],
    ),
    # ── ④ #20：四个已存在主流栈的兜底缺口 ───────────────────────────────────────
    (
        '#20：`.aspx` 从一档表消失（ASP.NET Web Forms 整栈判不出——它没有模板目录惯例，'
        '二档的"目录名+后缀+dep marker"对它结构性失效）',
        SD,
        '    ".aspx": "ASP.NET Web Forms",\n',
        '',
        ['test_four_existing_stacks_no_longer_get_a_confident_wrong_answer',
         'test_webforms_is_recognized_with_zero_template_dir_convention',
         'test_newly_registered_suffixes_are_in_the_first_tier_table'],
    ),
    (
        '#20：`.gsp` 从一档表消失（Grails 的 grails-app/views/*.gsp 回到判 none）',
        SD,
        '    ".gsp": "GSP",\n',
        '',
        ['test_four_existing_stacks_no_longer_get_a_confident_wrong_answer',
         'test_newly_registered_suffixes_are_in_the_first_tier_table'],
    ),
    (
        '#20：`.phtml` 从一档表消失（Magento 2 的 view/frontend/templates/*.phtml 回到判 none。'
        '★也证明分档选对了★ 落到二档要 dep marker 佐证，而表里没有 magento marker）',
        SD,
        '    ".phtml": "PHP template",\n',
        '',
        ['test_four_existing_stacks_no_longer_get_a_confident_wrong_answer',
         'test_phtml_is_a_template_but_plain_php_source_is_not',
         'test_newly_registered_suffixes_are_in_the_first_tier_table'],
    ),
    (
        '#20：`.vbhtml` 从一档表消失（Razor 的 VB.NET 方言——与已登记的 .cshtml 同引擎'
        '不同语言，缺了它 VB 工程整栈认不出）',
        SD,
        '    ".vbhtml": "Razor",\n',
        '',
        ['test_newly_registered_suffixes_are_in_the_first_tier_table'],
    ),
    (
        '#20：`.ascx` 塞进一档表（不可证伪的冗余条目——user control 不能被单独请求，'
        '有它必有 .aspx ⇒ 造不出"有它/没它结论不同"的夹具。同不登记 .html.erb 的判据）',
        SD,
        '    ".aspx": "ASP.NET Web Forms",\n',
        '    ".aspx": "ASP.NET Web Forms",\n    ".ascx": "ASP.NET Web Forms",\n',
        ['test_webforms_coverage_does_not_depend_on_ascx_or_master'],
    ),
    (
        '#20：`webapp` 从目录表消失（JSF 的 Facelets 默认扫 **war 根** src/main/webapp，'
        '页面不进任何 templates 子目录 ⇒ 该栈的 .xhtml 一个都不入账）',
        SD,
        '    "webapp",\n',
        '',
        ['test_four_existing_stacks_no_longer_get_a_confident_wrong_answer',
         'test_jsf_xhtml_in_war_root_is_only_reachable_via_the_webapp_dir_name'],
    ),
    (
        '#20：JSF 五条 marker 整块消失（.xhtml 靠后缀永远认不出——静态 XHTML 同后缀——'
        '只能靠依赖坐标佐证，与 Django/Askama 同性质）',
        SD,
        '    "myfaces": "JSF (Facelets)",\n    "jakarta.faces": "JSF (Facelets)",\n'
        '    "javax.faces": "JSF (Facelets)",\n    "jsf-api": "JSF (Facelets)",\n'
        '    "primefaces": "JSF (Facelets)",\n',
        '',
        ['test_four_existing_stacks_no_longer_get_a_confident_wrong_answer',
         'test_each_jsf_marker_is_independently_load_bearing'],
    ),
    (
        '#20：只删 `primefaces` 一条（★最容易被误判为冗余的一条★ 跑在 Jakarta EE 容器上的 '
        'PrimeFaces 工程 pom 里只有 jakartaee-web-api(provided) + primefaces，前者字符串'
        '**不含** jakarta.faces ⇒ 删了它该工程一个 marker 都命中不了）',
        SD,
        '    "primefaces": "JSF (Facelets)",\n',
        '',
        ['test_four_existing_stacks_no_longer_get_a_confident_wrong_answer',
         'test_each_jsf_marker_is_independently_load_bearing'],
    ),
    (
        '#20：只删 `javax.faces` 一条（Java EE 时代坐标 org.glassfish:javax.faces。'
        '"五条互不支配"这句话必须逐条可证伪，否则被支配的那条是冗余项）',
        SD,
        '    "javax.faces": "JSF (Facelets)",\n',
        '',
        ['test_each_jsf_marker_is_independently_load_bearing'],
    ),
    (
        '#20：只删 `myfaces` 一条（MyFaces 的 api/impl 由这一条同时盖住）',
        SD,
        '    "myfaces": "JSF (Facelets)",\n',
        '',
        ['test_each_jsf_marker_is_independently_load_bearing'],
    ),
    # ── ⑤ #19 配对守卫的两向区分力（(版本, 摘要) 必须同时改）────────────────────
    (
        '#19：改了事实表却**不**递增 schema 版本（已缓存项目命中缓存早返 ⇒ 治法对所有已建档'
        '项目一行不生效，且是静默 no-op：消费者 `.get()` 读缺键得 None＝假值。这是 v5 那次'
        '事故的原形态，第二次）',
        # R2-H4 起常量本体迁到 stack_detect.py（worker 同源消费），planning_nodes 只剩
        # re-export——落点同步迁，别打一个已经不住的地址。
        SD,
        '_STACK_SCHEMA_VERSION = 8',
        '_STACK_SCHEMA_VERSION = 7',
        ['test_stack_schema_version_paired_with_cached_payload'],
    ),
    (
        '#19：往目录表塞条目而摘要守卫不响（证明摘要**盖得住**新扩的三张表；顺带证明 '
        '`wwwroot` 这类非模板目录不该进表）',
        SD,
        '    "webapp",\n',
        '    "webapp",\n    "wwwroot",\n',
        ['test_stack_schema_version_paired_with_cached_payload',
         'test_jsf_xhtml_in_war_root_is_only_reachable_via_the_webapp_dir_name'],
    ),
    # ── ⑥ P-C3 复核 R2（hunter 第二轮）────────────────────────────────────
    (
        'R2-H1：根级 Helm chart 排除失效（`_h == ""` 匹配一切候选的判据被删 ⇒ chart 仓标准'
        '布局[Chart.yaml 在仓根]的 templates/*.tpl 照旧触发「认不得」白烧裁决。hunter 实测'
        '形态：同结构放 deploy/chart/ 下 unrec=False、放根 unrec=True）',
        SD,
        '        if any(_h == "" or _cdir == _h or _cdir.startswith(_h + "/")\n'
        '               for _h in helm_chart_dirs):',
        '        if any(_cdir == _h or _cdir.startswith(_h + "/")\n'
        '               for _h in helm_chart_dirs):',
        ['test_root_level_helm_chart_is_also_excluded',
         # 摘要守卫也锁得住：helm_chart_root 夹具的 verdict 从 unrec:False 翻回 True ⇒
         # 摘要变 ⇒ (8, 摘要) 配对破。两向都锁，防"测试被改废而守卫不响"。
         'test_stack_schema_version_paired_with_cached_payload'],
    ),
    (
        'R2-H2：候选封顶 200 后截断不留痕（第 201 个起静默丢弃，WARNING/evidence 把下界当真'
        '数报——血规 10④「空返回/缺席必须机读可辨」的计数版）',
        SD,
        '                    else:\n'
        '                        unengined_candidates_dropped += 1   # R2-H2：截断留痕',
        '                    else:\n'
        '                        pass',
        ['test_unengined_candidates_cap_leaves_a_trace'],
    ),
    (
        'R2-H3：裁决「半截采纳」回潮（kind 漏字段/不可消费时单独采纳 frontend 自由文本 ⇒ '
        '画像同挂「前端=Twig」与「形态=none+认不得未清」，自矛盾且按指纹写进缓存钉死）',
        PN,
        '                    "frontend": adj.frontend if _kind_ok else profile["frontend"],',
        '                    "frontend": adj.frontend,',
        ['test_adjudication_with_missing_kind_does_not_clear_the_flag'],
    ),
    (
        'R2-H4：worker 第二读取路径不过 schema 闸（_resolve_project_stack 只看指纹漂移/jvm '
        '两键 ⇒ detect_stack 未重跑的路径上旧 schema 画像继续当硬前提喂 worker prompt，注释'
        '自称「双保险」实则从未引用常量——闸存在 ≠ 接上了）',
        EA,
        '        need_disk = fp_drifted or stale_schema or not profile or not (',
        '        need_disk = fp_drifted or not profile or not (',
        ['test_worker_stack_resolution_reprobes_on_stale_schema'],
    ),
]


def _pytest(args: list[str]) -> int:
    p = subprocess.run([PY, "-m", "pytest", *TESTS, "-p", "no:warnings", "-q",
                        "--tb=no", *args], cwd=ROOT, capture_output=True, text=True)
    return p.returncode


def main() -> int:
    print("═" * 70)
    print("步骤 0：基线必须全绿")
    print("═" * 70)
    rc = _pytest([])
    if rc != 0:
        print(f"✗ 基线是红的 (exit={rc}) —— 突变结果全部无意义。先修基线。")
        return 1
    print(f"✓ 基线全绿 (exit={rc})\n")

    failures = []
    for i, (name, path, old, new, should_red) in enumerate(MUTATIONS, 1):
        src = path.read_text()
        if old not in src:
            print(f"[{i}/{len(MUTATIONS)}] {name}\n    ✗ 落点未命中（代码已漂移）")
            failures.append((name, "落点未命中"))
            continue
        if src.count(old) != 1:
            print(f"[{i}/{len(MUTATIONS)}] {name}\n"
                  f"    ✗ 落点出现 {src.count(old)} 次（非唯一，突变不等价）")
            failures.append((name, "落点非唯一"))
            continue
        mutated = src.replace(old, new, 1)
        try:
            ast.parse(mutated)
        except SyntaxError as _e:
            print(f"[{i}/{len(MUTATIONS)}] {name}\n"
                  f"    ✗ 突变后源码无法编译（{_e.msg} @line {_e.lineno}）⇒ pytest 只会报 "
                  f"collection error，rc≠0 是假信号。")
            failures.append((name, "突变产生语法错"))
            continue
        path.write_text(mutated)
        try:
            per = [(n, _pytest(["-k", n])) for n in should_red]
            missing = [n for n, r in per if r == 5]
            green = [n for n, r in per if r == 0]
            print(f"[{i}/{len(MUTATIONS)}] {name}")
            if not missing and not green:
                print(f"    ✓ 指名的 {len(per)} 条全红")
            else:
                if missing:
                    print(f"    ✗ 测试名选不到（rc=5）: {missing}")
                    failures.append((name, "测试名选不到"))
                if green:
                    print(f"    ✗ 突变后仍绿 = 零区分力: {green}")
                    failures.append((name, "突变后仍绿"))
        finally:
            path.write_text(src)

    print("\n" + "═" * 70)
    rc_r = _pytest([])
    print(f"步骤 N：还原后基线复验 exit={rc_r}")
    if rc_r != 0:
        print("✗ 还原后基线不绿 —— harness 污染了工作树")
        return 1

    if failures:
        print(f"\n✗ {len(failures)} 条未达标：")
        for n, why in failures:
            print(f"  · [{why}] {n}")
        return 1
    print(f"\n✓ 全部 {len(MUTATIONS)} 条突变都被锁住，且基线前后皆绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
